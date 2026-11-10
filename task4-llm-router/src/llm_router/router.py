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

Streaming (`"stream": true`): failover is allowed only while nothing has
been sent to the client. The provider deadline covers connect, response
headers and the FIRST SSE event. Once the first event has arrived, the
router is committed to that provider: events are relayed as they arrive,
and a later failure ends the stream with a standardized error event (it
can't switch provider halfway through an answer). The router always asks
for `stream_options.include_usage` so it can charge real usage; if the
client didn't ask for it, the usage-only chunk is removed before relaying.
If the stream breaks or the client disconnects, the estimate is charged
(the provider has most likely billed tokens already).

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
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn

import httpx

from llm_router.limiter import RateLimited, RequestTooLarge, Reservation, SqliteTokenLimiter
from llm_router.tokens import TokenCounter, count_chars

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


@dataclass
class RouterStream:
    """A started stream: headers are final, `body` yields SSE bytes."""

    headers: dict[str, str]
    body: AsyncIterator[bytes]
    status: int = 200


def _sse_error(err_type: str, request_id: str) -> bytes:
    _, message = ERRORS[err_type]
    payload = {"error": {"type": err_type, "message": message, "request_id": request_id}}
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _sse_events(resp: httpx.Response) -> AsyncIterator[str]:
    """Yield each SSE event's data payload. Comments and other fields are
    dropped (OpenAI-style streams carry everything in `data:`)."""
    data: list[str] = []
    async for line in resp.aiter_lines():
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line == "" and data:
            yield "\n".join(data)
            data = []
    if data:
        yield "\n".join(data)


def _int_tokens(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


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


def estimate_tokens(body: dict[str, Any], default_max_tokens: int, count: TokenCounter = count_chars) -> int:
    """Pre-flight cost: prompt tokens (message contents, plus tool
    definitions, which providers bill as prompt) + a small per-message
    overhead + the completion budget (max_tokens). Corrected to the
    provider's reported usage after the call. See tokens.py for `count`."""
    prompt = 0
    for m in body["messages"]:
        content = m.get("content")
        if isinstance(content, str):
            prompt += count(content)
        elif content is not None:
            prompt += count(json.dumps(content))
    if isinstance(body.get("tools"), list):
        prompt += count(json.dumps(body["tools"]))
    prompt += 4 * len(body["messages"])
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
    if "stream" in body and not isinstance(body["stream"], bool):
        return '"stream" must be a boolean.'
    if "stream_options" in body and not isinstance(body["stream_options"], dict):
        return '"stream_options" must be an object.'
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
        count_tokens: TokenCounter = count_chars,
    ) -> None:
        self.limiter = limiter
        self.providers = [(primary, primary_timeout), (secondary, secondary_timeout)]
        self.client = client
        self.default_max_tokens = default_max_tokens
        # Headroom for reasoning models: their hidden reasoning tokens count in
        # usage.total_tokens but are not bounded by max_tokens, so the plain
        # estimate can be far too low (seen live: max_tokens=50, usage=335).
        self.extra_reserve_tokens = max(0, extra_reserve_tokens)
        self.count_tokens = count_tokens

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
        if resp.status_code != 200:
            self._classify_failure(provider, resp.status_code, raw, elapsed, rid)  # always raises
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except ValueError:
            logger.warning("[%s] %s returned invalid JSON (%.0fms)", rid, provider.name, elapsed)
            raise _Failover from None
        logger.info("[%s] %s ok (%.0fms)", rid, provider.name, elapsed)
        return data

    @staticmethod
    def _classify_failure(provider: Provider, status: int, raw: bytes, elapsed: float, rid: str) -> NoReturn:
        logger.warning("[%s] %s HTTP %s (%.0fms): %r", rid, provider.name, status, elapsed, raw[:300])
        if status == 429 or status >= 500:
            raise _Failover
        if status in (400, 404, 413, 422):
            raise _Fatal("invalid_request")
        raise _Fatal("upstream_error")  # 401/403 etc.: our provider credentials, not the client's fault

    async def _open_stream(self, provider: Provider, timeout: float, body: dict[str, Any],
                           rid: str) -> tuple[httpx.Response, AsyncIterator[str], str]:
        """Start a streaming call and wait for its first event, all under the
        deadline. Returns (response, remaining events, first event)."""
        payload = {**body, "stream": True,
                   "stream_options": {**(body.get("stream_options") or {}), "include_usage": True}}
        if provider.model:
            payload["model"] = provider.model
        headers = {"x-request-id": rid}
        if provider.api_key:
            headers["authorization"] = f"Bearer {provider.api_key}"
        started = time.monotonic()
        resp: httpx.Response | None = None
        try:
            async with asyncio.timeout(timeout):
                resp = await self.client.send(
                    self.client.build_request("POST", provider.url, json=payload, headers=headers), stream=True)
                if resp.status_code != 200:
                    raw = await resp.aread()
                    self._classify_failure(provider, resp.status_code, raw, (time.monotonic() - started) * 1000, rid)
                events = _sse_events(resp)
                first = await anext(events, None)
            if first is None:
                logger.warning("[%s] %s stream ended before any event", rid, provider.name)
                raise _Failover
            if first.strip() != "[DONE]":
                try:
                    if not isinstance(json.loads(first), dict):
                        raise ValueError("not an object")
                except ValueError:
                    logger.warning("[%s] %s sent an invalid first event", rid, provider.name)
                    raise _Failover from None
        except TimeoutError:
            logger.warning("[%s] %s stream: no first event within %.0fms", rid, provider.name, timeout * 1000)
            await self._discard(resp)
            raise _Failover from None
        except httpx.HTTPError as exc:
            logger.warning("[%s] %s stream transport error: %s: %s", rid, provider.name, type(exc).__name__, exc)
            await self._discard(resp)
            raise _Failover from None
        except (_Failover, _Fatal):
            await self._discard(resp)
            raise
        logger.info("[%s] %s stream started (first event after %.0fms)", rid, provider.name,
                    (time.monotonic() - started) * 1000)
        return resp, events, first

    @staticmethod
    async def _discard(resp: httpx.Response | None) -> None:
        if resp is not None:
            await resp.aclose()

    async def _relay(self, resp: httpx.Response, events: AsyncIterator[str], first: str,
                     reservation: Reservation, estimate: int, rid: str,
                     client_wants_usage: bool) -> AsyncIterator[bytes]:
        actual: int | None = None
        try:
            payload: str | None = first
            saw_done = False
            while payload is not None:
                if payload.strip() == "[DONE]":
                    saw_done = True
                    yield b"data: [DONE]\n\n"
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    chunk = None
                if isinstance(chunk, dict) and "usage" in chunk:
                    usage = chunk["usage"]
                    if isinstance(usage, dict) and _int_tokens(usage.get("total_tokens")) is not None:
                        actual = usage["total_tokens"]
                    if not client_wants_usage:
                        if chunk.get("choices") == []:
                            payload = await anext(events, None)  # usage-only chunk the client didn't ask for
                            continue
                        del chunk["usage"]
                        payload = json.dumps(chunk, separators=(",", ":"))
                yield f"data: {payload}\n\n".encode()
                payload = await anext(events, None)
            if not saw_done:
                logger.warning("[%s] stream ended without [DONE]", rid)
                yield _sse_error("upstream_error", rid)
        except httpx.HTTPError as exc:
            logger.warning("[%s] stream broke: %s: %s", rid, type(exc).__name__, exc)
            yield _sse_error("upstream_error", rid)
        finally:
            charge = actual if actual is not None else estimate
            # Shielded: a client disconnect cancels this generator, but the
            # books must still be closed and the upstream connection released.
            await asyncio.shield(self._close_stream(resp, reservation, charge))

    async def _close_stream(self, resp: httpx.Response, reservation: Reservation, charge: int) -> None:
        try:
            await resp.aclose()
        finally:
            await asyncio.to_thread(self.limiter.commit, reservation, charge)

    async def _complete_stream(self, body: dict[str, Any], rid: str, estimate: int,
                               reservation: Reservation) -> RouterResponse | RouterStream:
        handed_off = False
        try:
            for i, (provider, timeout) in enumerate(self.providers):
                try:
                    resp, events, first = await self._open_stream(provider, timeout, body, rid)
                except _Failover:
                    continue
                except _Fatal as fatal:
                    return error_response(fatal.err_type, rid)
                wants_usage = bool((body.get("stream_options") or {}).get("include_usage"))
                stream = RouterStream(
                    {"x-request-id": rid, "x-gateway-route": "primary" if i == 0 else "fallback",
                     "cache-control": "no-cache"},
                    self._relay(resp, events, first, reservation, estimate, rid, wants_usage),
                )
                handed_off = True  # the relay now owns the reservation and the connection
                return stream
            return error_response("upstream_unavailable", rid, {"retry-after": "1"})
        finally:
            if not handed_off:
                await asyncio.shield(asyncio.to_thread(self.limiter.release, reservation))

    async def complete(self, api_key: str, body: Any) -> RouterResponse | RouterStream:
        rid = uuid.uuid4().hex
        if problem := validate(body):
            return error_response("invalid_request", rid, message=problem)

        estimate = estimate_tokens(body, self.default_max_tokens, self.count_tokens) + self.extra_reserve_tokens
        try:
            reservation: Reservation = await asyncio.to_thread(self.limiter.reserve, api_key, estimate)
        except RequestTooLarge:
            return error_response("request_too_large", rid)
        except RateLimited as rl:
            retry = str(max(1, math.ceil(rl.retry_after)))
            return error_response("rate_limit_exceeded", rid, {"retry-after": retry})

        if body.get("stream"):
            return await self._complete_stream(body, rid, estimate, reservation)

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
