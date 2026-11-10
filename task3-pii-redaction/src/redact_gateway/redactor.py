"""PII detection and an incremental (streaming) redactor.

The streaming problem: a model emits text in small deltas, so an email can
arrive as "john.d" | "oe@exa" | "mple.com". Redacting each delta on its own
leaks; buffering the whole response kills time-to-first-token.

The approach: **release text up to the last hard delimiter.**

Every pattern below is built only from "candidate" characters (letters,
digits, `.` `_` `%` `+` `-` `@` `(` `)`), plus single spaces *after a digit
or `)`* for numbers like "4111 1111 1111 1111" or "(555) 123-4567". No
match can therefore span a character outside that set (a "hard delimiter":
most whitespace, punctuation like `,;:!?"'`, newlines). So once a hard delimiter has
arrived, everything before it is final and can be matched, redacted and
emitted. Only the trailing run that might still be part of a match is held.
In ordinary prose that is the last, partial word.

Memory is bounded: if the held run grows past `max_hold` characters (an
unbroken token or a long run of space-separated digits), it is
force-flushed, keeping the last `tail` characters and never cutting through
a match. That bounds per-stream state at roughly max_hold + one delta.
"""

from __future__ import annotations

import json
import re

REDACTED = "[REDACTED]"
_REDACTED_JSON = json.dumps(REDACTED)  # used where a bare JSON value was redacted

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
    # SSN without dashes ("123456789", "123 45 6789"). Nine digits alone are
    # far too common (order numbers, IDs), so this only counts as PII when an
    # SSN keyword appears shortly before it; see _is_pii.
    r"|(?P<ssn_bare>(?<![\d-])\d{3}(?P<s0> ?)\d{2}(?P=s0)\d{4}(?![\d-]))"
    # North American phone numbers. A separator or parentheses are required:
    # bare 10-digit runs are too often IDs.
    r"|(?P<phone>(?<![\w+-])(?:\+1[ .-]?)?(?:\(\d{3}\) ?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\d-]))"
    # International numbers in +CC form, 8-15 digits in total (E.164).
    r"|(?P<phone_intl>(?<![\w+])\+[2-9]\d{0,2}(?:[ .-]\d{1,5}){2,5}(?!\d))"
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

_CANDIDATE_PUNCT = frozenset("._%+-@()")

# An SSN keyword within this many characters before a bare 9-digit number
# makes it an SSN ("SSN: 123456789", "social security number is 123 45 6789").
SSN_CONTEXT = 40
_SSN_KEYWORD = re.compile(r"(?i)(?<![a-z])(?:ssn|social[\s_-]*security|soc\.?\s*sec)(?![a-z])")


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


def _is_pii(m: re.Match[str], before: str) -> bool:
    """`before` is the text preceding the match (at least SSN_CONTEXT chars
    when available, including text already released by a stream)."""
    kind = m.lastgroup
    if kind == "card":
        # Luhn keeps order numbers, timestamps and phone-ish digit runs from
        # being redacted as card numbers.
        return luhn_valid(re.sub(r"\D", "", m.group()))
    if kind == "ssn_bare":
        return _SSN_KEYWORD.search(before[-SSN_CONTEXT:]) is not None
    if kind == "phone_intl":
        return 8 <= sum(c.isdigit() for c in m.group()) <= 15
    return True


class JsonStringState:
    """Tracks whether a position in a JSON text is inside a string literal,
    across stream chunks. Used so a redaction never breaks the JSON: inside a
    string the PII becomes [REDACTED]; a bare value (e.g. a card number
    written as a JSON number) becomes the string "[REDACTED]"."""

    __slots__ = ("in_string", "escaped")

    def __init__(self) -> None:
        self.in_string = False
        self.escaped = False

    def advance(self, s: str) -> None:
        for ch in s:
            if self.escaped:
                self.escaped = False
            elif self.in_string and ch == "\\":
                self.escaped = True
            elif ch == '"':
                self.in_string = not self.in_string


def _apply(text: str, matches: list[re.Match[str]], end: int, context: str = "",
           json_state: JsonStringState | None = None) -> str:
    """Rebuild text[:end], replacing PII matches that lie wholly within it.

    `context` is text that came before `text` (for SSN keyword lookback).
    With `json_state`, `text` is treated as a fragment of JSON and the state
    is advanced over it."""
    out: list[str] = []
    pos = 0
    for m in matches:
        if m.end() > end:
            break
        start = m.start()
        if not _is_pii(m, context[-SSN_CONTEXT:] + text[max(0, start - SSN_CONTEXT):start]):
            continue
        out.append(text[pos:start])
        replacement = REDACTED
        if json_state is not None:
            json_state.advance(text[pos:start])
            if json_state.escaped:
                # The match begins with the tail of an escape such as "\n" or
                # "\u0040": keep that escape intact, or the JSON breaks.
                keep = 5 if text[start] == "u" else 1
                if m.end() - start <= keep:
                    out.append(m.group())
                    json_state.advance(m.group())
                    pos = m.end()
                    continue
                out.append(text[start : start + keep])
                json_state.advance(text[start : start + keep])
            elif not json_state.in_string:
                replacement = _REDACTED_JSON
            # Matches contain no quote or backslash, so the state is unchanged
            # by the rest of the matched text.
        out.append(replacement)
        pos = m.end()
    if json_state is not None:
        json_state.advance(text[pos:end])
    out.append(text[pos:end])
    return "".join(out)


def redact_text(text: str, context: str = "") -> str:
    """Redact a complete string (non-streaming responses, and the reference
    the streaming redactor is tested against). `context`: text preceding it,
    e.g. a JSON key name, used for the SSN keyword lookback."""
    return _apply(text, list(PII_PATTERN.finditer(text)), len(text), context)


def redact_json_text(text: str) -> str:
    """Redact a complete JSON document given as text (e.g. tool-call
    arguments) so that the result is still valid JSON if the input was."""
    return _apply(text, list(PII_PATTERN.finditer(text)), len(text), "", JsonStringState())


def redact_value(value: object, key: str = "") -> object:
    """Redact every string, and every number that looks like PII, inside a
    parsed JSON value (e.g. Anthropic tool_use `input`). Keys are kept."""
    if isinstance(value, str):
        return redact_text(value, key)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return REDACTED if redact_text(str(value), key) != str(value) else value
    if isinstance(value, list):
        return [redact_value(v, key) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(v, str(k)) for k, v in value.items()}
    return value


def _is_candidate(ch: str) -> bool:
    return ch.isalnum() or ch in _CANDIDATE_PUNCT


class StreamRedactor:
    """Feed deltas in, get redacted text out. One instance per stream (per
    choice index). Not thread-safe; it doesn't need to be."""

    def __init__(self, max_hold: int = 2048, tail: int = 512, json_mode: bool = False) -> None:
        # tail >= MAX_MATCH_LEN guarantees a forced flush never emits the
        # start of a valid match whose end hasn't arrived yet.
        if tail >= max_hold:
            raise ValueError("tail must be smaller than max_hold")
        self.max_hold = max_hold
        self.tail = tail
        self._buf = ""
        self._context = ""  # last SSN_CONTEXT chars already released (original text)
        # json_mode: the stream is a JSON document (tool-call arguments);
        # redactions keep it valid JSON.
        self._json = JsonStringState() if json_mode else None

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
            elif ch == " " and i >= 2 and (buf[i - 2].isdigit() or buf[i - 2] == ")"):
                i -= 1  # space inside a possible card/phone number: soft
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
            out = self._release(region, list(PII_PATTERN.finditer(region)), cut)
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
        out = self._release(buf, matches, cut)
        self._buf = buf[cut:]
        return out

    def _release(self, text: str, matches: list[re.Match[str]], end: int) -> str:
        out = _apply(text, matches, end, self._context, self._json)
        self._context = (self._context + text[:end])[-SSN_CONTEXT:]
        return out

    def flush(self) -> str:
        """End of stream: redact and release everything still held."""
        buf = self._buf
        out = self._release(buf, list(PII_PATTERN.finditer(buf)), len(buf))
        self._buf = ""
        return out
