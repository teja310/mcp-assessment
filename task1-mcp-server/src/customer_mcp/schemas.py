r"""Pydantic v2 input models for each tool.

Each model is the single source of truth for both wire validation (used
directly by the `on_call_tool` handler in `server.py`) and the JSON Schema
advertised to clients in `tools/list` (via `model_json_schema()`), so the
two can never drift apart.

`regex_engine="python-re"` on both models: pydantic-core's default Rust
regex engine doesn't support `\A`/`\Z` anchors (it raises at class-build
time). Python's `re` module does, and `\A`/`\Z` -- rather than `^`/`$` --
matter here: Python's `$` matches just before a trailing newline, which
would let "CUST-12345\n" slip through the pattern check.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, field_validator

CUSTOMER_ID_PATTERN = r"\ACUST-\d{5}\Z"

# Business caps, enforced in addition to basic type/shape checks.
MAX_REFUND_AMOUNT = 1_000_000.0
REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 500


class GetCustomerRecordInput(BaseModel):
    """Input for the `get_customer_record` tool."""

    model_config = ConfigDict(extra="forbid", strict=True, regex_engine="python-re")

    customer_id: str = Field(
        pattern=CUSTOMER_ID_PATTERN,
        description="Customer identifier, format CUST-XXXXX (5 digits).",
    )


class TriggerRefundInput(BaseModel):
    """Input for the `trigger_refund` tool.

    `amount` decimal-places policy: values with more than 2 decimal places
    are REJECTED (not silently rounded) -- a caller asking to refund
    "19.999" almost certainly made a mistake, and silently rounding it
    would hide that. See the README's "Design decisions" section.
    """

    model_config = ConfigDict(extra="forbid", strict=True, regex_engine="python-re")

    customer_id: str = Field(
        pattern=CUSTOMER_ID_PATTERN,
        description="Customer identifier, format CUST-XXXXX (5 digits).",
    )
    amount: float = Field(
        gt=0,
        le=MAX_REFUND_AMOUNT,
        description="Refund amount in account currency units. Positive, finite, at most 2 decimal places.",
    )
    reason: str = Field(
        max_length=REASON_MAX_LENGTH,
        description="Human-readable refund reason. At least 10 characters after trimming whitespace.",
    )

    @field_validator("amount")
    @classmethod
    def _amount_must_be_finite_and_at_most_2dp(cls, v: float) -> float:
        # `strict=True` + `le=MAX_REFUND_AMOUNT` already reject inf/-inf/nan
        # (a NaN comparison is always False, so `le` fails), but that's an
        # incidental side effect of the range check rather than a documented
        # finiteness check -- make it explicit so the intent survives a
        # future change to the bound.
        if not math.isfinite(v):
            raise ValueError("amount must be a finite number")
        if round(v, 2) != v:
            raise ValueError("amount must have at most 2 decimal places")
        return v

    @field_validator("reason")
    @classmethod
    def _reason_stripped_length(cls, v: str) -> str:
        stripped = v.strip()
        if len(stripped) < REASON_MIN_LENGTH:
            raise ValueError(f"reason must be at least {REASON_MIN_LENGTH} characters after trimming whitespace")
        return v
