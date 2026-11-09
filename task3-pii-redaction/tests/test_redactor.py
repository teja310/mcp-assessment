from __future__ import annotations

import random
import time

import pytest

from redact_gateway.redactor import MAX_MATCH_LEN, REDACTED, StreamRedactor, luhn_valid, redact_text

EMAIL = "john.doe+billing@mail.example.co.uk"
SSN = "123-45-6789"
CARD_SPACED = "4111 1111 1111 1111"
CARD_DASHED = "5500-0000-0000-0004"
CARD_PLAIN = "378282246310005"  # Amex, 15 digits

SAMPLES = [
    f"Contact {EMAIL} today.",
    f"SSN: {SSN}, card {CARD_SPACED}; alt {CARD_DASHED} and {CARD_PLAIN}!",
    f"Email:{EMAIL},SSN:{SSN}\nCard ({CARD_SPACED}) end",
    f"Order 1234 5678 9012 3456 is not a valid card (Luhn), call 555-123-4567.",
    "No PII here at all, just a normal sentence with numbers 42 and 3.14.",
    f"{EMAIL}",  # PII at the very end of the stream, no trailing delimiter
    f"Unicode café ünïcode {EMAIL}… done",
    f"1 2 3 4 5 6 7 8 9 {CARD_SPACED} 10 11",
]

RAW_PII = [EMAIL, SSN, CARD_SPACED, CARD_DASHED, CARD_PLAIN]


def stream(text: str, pieces: list[int], r: StreamRedactor | None = None) -> str:
    r = r or StreamRedactor()
    out, pos = [], 0
    for size in pieces:
        out.append(r.feed(text[pos : pos + size]))
        pos += size
    out.append(r.feed(text[pos:]))
    out.append(r.flush())
    return "".join(out)


# --- batch redaction -----------------------------------------------------------


@pytest.mark.parametrize("pii", RAW_PII)
def test_each_pattern_redacted(pii: str) -> None:
    assert redact_text(f"x {pii} y") == f"x {REDACTED} y"


@pytest.mark.parametrize(
    "text",
    [
        "Order 1234 5678 9012 3456",  # fails Luhn
        "call 555-123-4567",
        "version 1.2.3 and user@localhost",  # no TLD
        "12-345-6789",  # wrong SSN grouping
        "123-45-67890",  # too many digits
    ],
)
def test_non_pii_left_alone(text: str) -> None:
    assert redact_text(text) == text


def test_luhn() -> None:
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112")


# --- streaming == batch, for every way of splitting the stream --------------------


@pytest.mark.parametrize("text", SAMPLES)
def test_every_single_split_point(text: str) -> None:
    expected = redact_text(text)
    for k in range(len(text) + 1):
        assert stream(text, [k]) == expected, f"split at {k}"


@pytest.mark.parametrize("text", SAMPLES)
def test_char_by_char(text: str) -> None:
    assert stream(text, [1] * len(text)) == redact_text(text)


@pytest.mark.parametrize("text", SAMPLES)
def test_random_chunkings(text: str) -> None:
    rng = random.Random(1234)
    expected = redact_text(text)
    for _ in range(300):
        pieces = [rng.randint(1, 8) for _ in range(len(text))]
        assert stream(text, pieces) == expected


@pytest.mark.parametrize("text", SAMPLES)
def test_no_raw_pii_in_any_emitted_piece(text: str) -> None:
    r = StreamRedactor()
    emitted = "".join(r.feed(ch) for ch in text) + r.flush()
    for pii in RAW_PII:
        assert pii not in emitted


# --- latency: text is released as soon as it's safe -------------------------------


def test_words_released_at_next_delimiter() -> None:
    r = StreamRedactor()
    assert r.feed("Hello wor") == "Hello "  # only the partial word is held
    assert r.feed("ld, how") == "world, "
    assert r.flush() == "how"


def test_partial_email_is_held_not_leaked() -> None:
    r = StreamRedactor()
    assert r.feed("mail john.d") == "mail "
    assert r.feed("oe@exa") == ""
    assert r.feed("mple.com now") == f"{REDACTED} "


def test_partial_card_with_spaces_is_held() -> None:
    r = StreamRedactor()
    assert r.feed("card 4111 1111") == "card "
    assert r.feed(" 1111 1111.") == ""  # "." could still be part of an email: held
    assert r.feed(" ok") == f"{REDACTED}. "


# --- memory bound ----------------------------------------------------------------


def test_held_buffer_is_bounded_on_unbroken_input() -> None:
    r = StreamRedactor(max_hold=200, tail=64)
    out = []
    for _ in range(5000):
        out.append(r.feed("abc"))
        assert r.held <= 200
    out.append(r.flush())
    assert "".join(out) == "abc" * 5000


def test_forced_flush_never_splits_pii() -> None:
    r = StreamRedactor(max_hold=200, tail=64)
    for offset in range(150, 260, 7):  # slide the SSN across the forced-cut point
        r = StreamRedactor(max_hold=200, tail=64)
        text = "x" * offset + "." + SSN + "." + "y" * 300
        emitted = "".join(r.feed(c) for c in text) + r.flush()
        assert SSN not in emitted and REDACTED in emitted, offset
        assert emitted == redact_text(text), offset


ADVERSARIAL_SHAPES = {
    "letters": "a" * 200_000,
    "digits": "1" * 200_000,
    "dotted": "a." * 100_000,
    "at_signs": "a@" * 100_000,
    "domain_labels": "x@" + "ab." * 66_000,
    "dashes": "1-" * 100_000,
}


@pytest.mark.parametrize("shape", list(ADVERSARIAL_SHAPES))
def test_matching_is_linear_time_on_adversarial_input(shape: str) -> None:
    """ReDoS regression. With unbounded quantifiers, 40k unbroken letters
    took 1.2s and grew quadratically (200k would take about 30s). Bounded,
    200k characters of every shape must finish well under a second, in both
    the non-streaming and the streaming path."""
    text = ADVERSARIAL_SHAPES[shape]
    t0 = time.perf_counter()
    redact_text(text)
    batch = time.perf_counter() - t0

    r = StreamRedactor()
    t0 = time.perf_counter()
    for i in range(0, len(text), 64):
        r.feed(text[i : i + 64])
    r.flush()
    streaming = time.perf_counter() - t0
    assert batch < 1.0 and streaming < 2.0, f"{shape}: batch {batch:.2f}s, streaming {streaming:.2f}s"


def test_longest_valid_email_survives_forced_flush_with_defaults() -> None:
    """A maximum-length email (409 chars) arriving inside an unbroken run that
    forces flushes must still be redacted whole at the default settings."""
    email = "l" * 64 + "@" + ".".join(["d" * 63] * 5) + "." + "c" * 24
    assert len(email) == MAX_MATCH_LEN and redact_text(email) == REDACTED
    for prefix_len in (1500, 1700, 1900, 2047):
        text = "x" * prefix_len + "_" + email + " tail"
        r = StreamRedactor()
        out = "".join(r.feed(text[i : i + 7]) for i in range(0, len(text), 7)) + r.flush()
        assert email not in out and out == redact_text(text), prefix_len


def test_absurdly_long_email_token_still_bounded() -> None:
    r = StreamRedactor(max_hold=200, tail=64)
    for _ in range(2000):
        r.feed("a")
        assert r.held <= 200
    r.feed("@example.com ")
    assert r.held <= 200
