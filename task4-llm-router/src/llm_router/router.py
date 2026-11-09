"""Model router: rate limit -> primary -> (on 429/timeout) secondary.

    reserve tokens (SQLite)        -> 429 rate_limit_exceeded + Retry-After
    primary, hard deadline 3000ms  -> success: commit real usage, return
        429 / timeout / 5xx / connection error -> failover
        other 4xx                  -> no failover (the request is bad; the
                                      secondary would reject it too)
    secondary, own deadline        -> success: commit real usage, return
    both failed                    -> release reservation, standardized error

Timeout semantics: `asyncio.timeout()` wraps the *whole* provider call
(connect, send, response headers AND body). httpx's own timeouts are
per-phase, so a provider that sends headers quickly and then trickles the
body would never trip them. When the deadline fires the in-flight request is
cancelled and its connection discarded, so a late primary response can never
be used or double-counted, and the tenant's budget is charged exactly once
whichever provider answers.

Every error leaving this module is one of the fixed payloads in
`ERRORS`. Upstream bodies, status lines, hostnames and Python exceptions go
to the log with the request_id, never to the client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from llm_router.limiter import RateLimited, RequestTooLarge, Reservation, SqliteTokenLimiter

logger = logging.getLogger("llm_router")

ERRORS: dict[str, tuple[int, str]] = {
    "invalid_request": (400, "The request is invalid."),
    "authentication_error": (401, "Missing or invalid API key."),
    "request_too_large": (413, "The request exceeds the per-minute token budget."),
    "rate_limit_exceeded": (429, "Token rate limit exceeded for this API key."),
    "upstream_error": (502, "The model provider returned an error."),
    "upstream_unavailable": (503, "All model providers are currently unavailable."),
    "internal_error": (500, "Internal gateway error."),
}


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str | None = None
    model: str | None = None  # override body["model"] for this provider

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


@dataclass
class RouterResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


def error_response(err_type: str, request_id: str, headers: dict[str, str] | None = None,
                   message: str | None = None) -> RouterResponse:
    status, default_message = ERRORS[err_type]
    return RouterResponse(
        status,
        {"error": {"type": err_type, "message": message or default_message, "request_id": request_id}},
        {"x-request-id": request_id, **(headers or {})},
    )


class _Failover(Exception):
    """Provider failed in a way the next provider might not."""


class _Fatal(Exception):
    """Provider rejected the request itself; don't try another provider."""

    def __init__(self, err_type: str) -> None:
        self.err_type = err_type


def estimate_tokens(body: dict[str, Any], default_max_tokens: int) -> int:
    """Conservative pre-flight cost: ~4 chars/token for the prompt plus a
    small per-message overhead, plus the completion budget (max_tokens).
    Corrected to the provider's reported usage after the call."""
    chars = 0
    for m in body["messages"]:
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif content is not None:
            chars += len(json.dumps(content))
    prompt = math.ceil(chars / 4) + 4 * len(body["messages"])
    return prompt + int(body.get("max_tokens") or default_max_tokens)


def validate(body: Any) -> str | None:
    if not isinstance(body, dict):
        return "Request body must be a JSON object."
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs or not all(isinstance(m, dict) for m in msgs):
        return '"messages" must be a non-empty array of objects.'
    mt = body.get("max_tokens")
    if mt is not None and (isinstance(mt, bool) or not isinstance(mt, int) or mt <= 0):
        return '"max_tokens" must be a positive integer.'
    if body.get("stream"):
        return "Streaming is not supported by this endpoint."
    return None


class ModelRouter:
    def __init__(
        self,
        limiter: SqliteTokenLimiter,
        primary: Provider,
        secondary: Provider,
        client: httpx.AsyncClient,
        primary_timeout: float = 3.0,
        secondary_timeout: float = 10.0,
        default_max_tokens: int = 1024,
        extra_reserve_tokens: int = 0,
    ) -> None:
        self.limiter = limiter
        self.providers = [(primary, primary_timeout), (secondary, secondary_timeout)]
        self.client = client
        self.default_max_tokens = default_max_tokens
        # Headroom for reasoning models: their hidden reasoning tokens count in
        # usage.total_tokens but are not bounded by max_tokens, so the plain
        # estimate can be far too low (seen live: max_tokens=50, usage=335).
        self.extra_reserve_tokens = max(0, extra_reserve_tokens)

    async def _call(self, provider: Provider, timeout: float, body: dict[str, Any], rid: str) -> dict[str, Any]:
        payload = {**body, "model": provider.model} if provider.model else body
        headers = {"x-request-id": rid}
        if provider.api_key:
            headers["authorization"] = f"Bearer {provider.api_key}"
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                resp = await self.client.post(provider.url, json=payload, headers=headers)
                raw = await resp.aread()
        except TimeoutError:
            logger.warning("[%s] %s timed out after %.0fms", rid, provider.name, timeout * 1000)
            raise _Failover from None
        except httpx.HTTPError as exc:
            logger.warning("[%s] %s transport error: %s: %s", rid, provider.name, type(exc).__name__, exc)
            raise _Failover from None
        elapsed = (time.monotonic() - started) * 1000
        if resp.status_code == 200:
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except ValueError:
                logger.warning("[%s] %s returned invalid JSON (%.0fms)", rid, provider.name, elapsed)
                raise _Failover from None
            logger.info("[%s] %s ok (%.0fms)", rid, provider.name, elapsed)
            return data
        logger.warning("[%s] %s HTTP %s (%.0fms): %r", rid, provider.name, resp.status_code, elapsed, raw[:300])
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Failover
        if resp.status_code in (400, 404, 413, 422):
            raise _Fatal("invalid_request")
        raise _Fatal("upstream_error")  # 401/403 etc.: our provider credentials, not the client's fault

    async def complete(self, api_key: str, body: Any) -> RouterResponse:
        rid = uuid.uuid4().hex
        if problem := validate(body):
            return error_response("invalid_request", rid, message=problem)

        estimate = estimate_tokens(body, self.default_max_tokens) + self.extra_reserve_tokens
        try:
            reservation: Reservation = await asyncio.to_thread(self.limiter.reserve, api_key, estimate)
        except RequestTooLarge:
            return error_response("request_too_large", rid)
        except RateLimited as rl:
            retry = str(max(1, math.ceil(rl.retry_after)))
            return error_response("rate_limit_exceeded", rid, {"retry-after": retry})

        committed = False
        try:
            for i, (provider, timeout) in enumerate(self.providers):
                try:
                    data = await self._call(provider, timeout, body, rid)
                except _Failover:
                    continue
                except _Fatal as fatal:
                    return error_response(fatal.err_type, rid)
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                actual = usage.get("total_tokens")
                actual = actual if isinstance(actual, int) and not isinstance(actual, bool) else estimate
                await asyncio.to_thread(self.limiter.commit, reservation, actual)
                committed = True
                return RouterResponse(200, data, {"x-request-id": rid,
                                                  "x-gateway-route": "primary" if i == 0 else "fallback"})
            return error_response("upstream_unavailable", rid, {"retry-after": "1"})
        finally:
            if not committed:
                # Failed, rejected or cancelled (client went away): nothing
                # was consumed, so give the budget back. Shielded so a
                # cancellation can't abort the release halfway.
                await asyncio.shield(asyncio.to_thread(self.limiter.release, reservation))
