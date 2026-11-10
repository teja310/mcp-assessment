"""Token counting for the pre-flight rate-limit estimate.

The estimate only has to be close and preferably high: after each call the
reservation is corrected to the provider's reported usage. But a low
estimate lets a tenant over-run its budget before the correction lands, and
characters/4 is low exactly where it matters (measured with o200k_base):

    text                      tiktoken   chars/4
    '你好世界' x 50                100        50   (CJK: 2x under)
    '{"a": [1,2,3]}' x 20          200        70   (JSON: ~3x under)
    'x' x 400                       50       100   (repetitive: over)

`tiktoken` is an optional dependency (`pip install -e ".[tokenizer]"`).
Its vocabulary file is downloaded once on first use and cached, so the
"auto" mode falls back to chars/4 (with a warning) if it can't be loaded.
o200k_base is OpenAI's tokenizer; other providers' tokenizers differ, so
for them it is a closer approximation than chars/4, not an exact count.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable

logger = logging.getLogger("llm_router")

TokenCounter = Callable[[str], int]


def count_chars(text: str) -> int:
    return math.ceil(len(text) / 4)


def make_counter(kind: str = "auto", encoding: str = "o200k_base") -> tuple[str, TokenCounter]:
    """Return (name of the counter in use, counter). kind: auto | tiktoken | chars."""
    if kind not in ("auto", "tiktoken", "chars"):
        raise ValueError(f"unknown tokenizer {kind!r}; use auto, tiktoken or chars")
    if kind == "chars":
        return "chars/4", count_chars
    try:
        import tiktoken

        enc = tiktoken.get_encoding(encoding)
    except Exception as exc:  # not installed, or vocabulary download failed
        if kind == "tiktoken":
            raise
        logger.warning("tiktoken unavailable (%s: %s); estimating tokens as chars/4", type(exc).__name__, exc)
        return "chars/4", count_chars

    def count(text: str) -> int:
        # Special-token text in user input is counted as ordinary text.
        return len(enc.encode(text, disallowed_special=()))

    return f"tiktoken:{encoding}", count
