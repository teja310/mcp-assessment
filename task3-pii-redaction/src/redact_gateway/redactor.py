"""PII detection and an incremental (streaming) redactor.

The streaming problem: a model emits text in small deltas, so an email can
arrive as "john.d" | "oe@exa" | "mple.com". Redacting each delta on its own
leaks; buffering the whole response kills time-to-first-token.

The approach: **release text up to the last hard delimiter.**

Every pattern below is built only from "candidate" characters (letters,
digits, `.` `_` `%` `+` `-` `@`), plus single spaces *between digits* for
card numbers like "4111 1111 1111 1111". No match can therefore span a
character outside that set (a "hard delimiter": most whitespace,
punctuation like `,;:!?()"'`, newlines). So once a hard delimiter has
arrived, everything before it is final and can be matched, redacted and
emitted. Only the trailing run that might still be part of a match is held.
In ordinary prose that is the last, partial word.

Memory is bounded: if the held run grows past `max_hold` characters (an
unbroken token or a long run of space-separated digits), it is
force-flushed, keeping the last `tail` characters and never cutting through
a match. That bounds per-stream state at roughly max_hold + one delta.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Order matters for overlapping alternatives: SSN before card.
#
# Every quantifier is bounded (RFC 5321 limits: local part <= 64, DNS label
# <= 63). With unbounded `+`, each start position in an unbroken run like
# "aaaa..." scanned to the end of the run before failing: O(n^2), i.e. 40k
# characters took 1.2s and 400k would take minutes (a ReDoS). Bounded, each
# start position does O(1) work, so matching is linear.
# Longest possible match: 64 + 1 + 5*(63+1) + 24 = 409 chars -> MAX_MATCH_LEN.
PII_PATTERN = re.compile(
    r"(?P<email>[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,4}\.[A-Za-z]{2,24})"
    r"|(?P<ssn>(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-]))"
    # Real card layouts only: 13-19 contiguous digits, 4-4-4-4(-3), or Amex
    # 4-6-5, with one consistent separator. A loose "digits with optional
    # separators" pattern would swallow neighbouring numbers ("9 4111 1111
    # 1111 1111") into one Luhn-invalid run and miss the real card.
    r"|(?P<card>(?<![\d-])(?:"
    r"\d{13,19}"
    r"|\d{4}(?P<s1>[ -])\d{4}(?P=s1)\d{4}(?P=s1)\d{4}(?:(?P=s1)\d{3})?"
    r"|\d{4}(?P<s2>[ -])\d{6}(?P=s2)\d{5}"
    r")(?!\d))"
)

MAX_MATCH_LEN = 409  # longest string PII_PATTERN can match (email); see above

_CANDIDATE_PUNCT = frozenset("._%+-@")


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _is_pii(m: re.Match[str]) -> bool:
    if m.lastgroup == "card":
        # Luhn keeps order numbers, timestamps and phone-ish digit runs from
        # being redacted as card numbers.
        return luhn_valid(re.sub(r"\D", "", m.group()))
    return True


def _apply(text: str, matches: list[re.Match[str]], end: int) -> str:
    """Rebuild text[:end], replacing PII matches that lie wholly within it."""
    out: list[str] = []
    pos = 0
    for m in matches:
        if m.end() > end:
            break
        if _is_pii(m):
            out.append(text[pos : m.start()])
            out.append(REDACTED)
            pos = m.end()
    out.append(text[pos:end])
    return "".join(out)


def redact_text(text: str) -> str:
    """Redact a complete string (non-streaming responses, and the reference
    the streaming redactor is tested against)."""
    return _apply(text, list(PII_PATTERN.finditer(text)), len(text))


def _is_candidate(ch: str) -> bool:
    return ch.isalnum() or ch in _CANDIDATE_PUNCT


class StreamRedactor:
    """Feed deltas in, get redacted text out. One instance per stream (per
    choice index). Not thread-safe; it doesn't need to be."""

    def __init__(self, max_hold: int = 2048, tail: int = 512) -> None:
        # tail >= MAX_MATCH_LEN guarantees a forced flush never emits the
        # start of a valid match whose end hasn't arrived yet.
        if tail >= max_hold:
            raise ValueError("tail must be smaller than max_hold")
        self.max_hold = max_hold
        self.tail = tail
        self._buf = ""

    @property
    def held(self) -> int:
        return len(self._buf)

    def _safe_cut(self) -> int:
        """Index just past the last hard delimiter (0 if none)."""
        buf = self._buf
        i = len(buf)
        while i > 0:
            ch = buf[i - 1]
            if _is_candidate(ch):
                i -= 1
            elif ch == " " and i >= 2 and buf[i - 2].isdigit():
                i -= 1  # space inside a possible card number: soft
            else:
                return i
        return 0

    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        self._buf += delta
        cut = self._safe_cut()
        if cut:
            # buf[:cut] ends in a hard delimiter, so no match inside it can
            # extend further: matching it in isolation is exact.
            region = self._buf[:cut]
            out = _apply(region, list(PII_PATTERN.finditer(region)), cut)
            self._buf = self._buf[cut:]
        else:
            out = ""
        if len(self._buf) > self.max_hold:
            out += self._force_flush()
        return out

    def _force_flush(self) -> str:
        buf = self._buf
        matches = list(PII_PATTERN.finditer(buf))
        cut = len(buf) - self.tail
        for m in matches:
            if m.start() < cut < m.end():
                # Never split a match (matches don't overlap). A match longer
                # than `tail` (impossible with the defaults, since
                # tail >= MAX_MATCH_LEN; only reachable with a custom small
                # tail) is redacted whole instead, so the buffer always
                # shrinks and memory stays bounded.
                cut = m.end() if m.end() - m.start() > self.tail else m.start()
                break
        out = _apply(buf, matches, cut)
        self._buf = buf[cut:]
        return out

    def flush(self) -> str:
        """End of stream: redact and release everything still held."""
        out = redact_text(self._buf)
        self._buf = ""
        return out
