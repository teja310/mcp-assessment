"""LLM gateway endpoints with PII redaction.

  POST /v1/chat/completions   OpenAI-compatible (Chat Completions)
  POST /v1/messages           Anthropic-compatible (Messages API)

Streaming path (`"stream": true`):

    client -> gateway -> provider
    provider SSE  --aiter_lines-->  parse event  --> per-block StreamRedactor
                                                 --> re-serialize --> client

Nothing accumulates beyond one SSE event plus each redactor's small
holdback, and every event is yielded the moment it's processed, so time to
first token is the provider's TTFT plus at most one partial word.

What is redacted:
  - assistant text (OpenAI `delta.content`, Anthropic `text_delta`)
  - EVERY other string field of an OpenAI delta/message except `role`
    (default-deny): e.g. `reasoning_content` (xAI/Grok, DeepSeek),
    `reasoning`, `refusal`, and fields providers add later. A live xAI run
    leaked PII through `reasoning_content` before this rule existed.
  - Anthropic `thinking_delta` text
  - tool-call arguments (OpenAI `tool_calls[].function.arguments` and legacy
    `function_call.arguments`, Anthropic
    `input_json_delta.partial_json` / `tool_use.input`), in JSON mode so the
    redacted arguments are still valid JSON

Non-streaming responses are redacted with the batch functions.

Upstream failures never leak provider bodies, headers or stack traces to
the client: they become a fixed gateway error payload; details go to the log.

Optional client authentication: when `api_keys` is given, every request
needs `Authorization: Bearer <key>` or `x-api-key: <key>`. Keys are held as
SHA-256 digests, compared in constant time, and never forwarded upstream.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from redact_gateway.redactor import StreamRedactor, redact_json_text, redact_text, redact_value

logger = logging.getLogger("redact_gateway")

DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


def gateway_error(status: int, err_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"type": err_type, "message": message, "request_id": uuid.uuid4().hex}},
        status_code=status,
    )


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _sse(data: str, event: str | None = None) -> bytes:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {data}\n\n".encode()


# --- OpenAI Chat Completions stream ----------------------------------------------


# Delta/message string fields that are protocol metadata, not model output.
_OPENAI_PASSTHROUGH_FIELDS = frozenset({"role"})


class SSERedactingRelay:
    """Rewrites an OpenAI-style chat.completion.chunk SSE stream.

    Default-deny: every string field of a delta (except `role`) gets its own
    stream redactor per choice, so text in fields like `reasoning_content`
    is redacted too, including fields this code has never heard of."""

    def __init__(self) -> None:
        # (choice index, field name) -> redactor; "content" is one such field.
        self.redactors: dict[tuple[int, str], StreamRedactor] = {}
        # (choice index, tool-call index or "function_call") -> JSON-mode redactor
        self.tool_redactors: dict[int, dict[int | str, StreamRedactor]] = {}
        self.finished: set[int] = set()
        self.template: dict[str, Any] | None = None

    def _redactor(self, idx: int, field: str = "content") -> StreamRedactor:
        return self.redactors.setdefault((idx, field), StreamRedactor())

    def _flush_fields(self, idx: int) -> dict[str, str]:
        out = {}
        for (i, field), r in self.redactors.items():
            if i == idx:
                text = r.flush()
                if text:
                    out[field] = text
        return out

    def _tool_redactor(self, idx: int, tool_idx: int | str) -> StreamRedactor:
        return self.tool_redactors.setdefault(idx, {}).setdefault(tool_idx, StreamRedactor(json_mode=True))

    def _flush_tools(self, idx: int, delta: dict[str, Any]) -> None:
        """Put held tool/function-call argument text into `delta`."""
        for tool_idx, r in self.tool_redactors.get(idx, {}).items():
            text = r.flush()
            if not text:
                continue
            if tool_idx == "function_call":
                fc = delta.setdefault("function_call", {})
                fc["arguments"] = fc.get("arguments", "") + text
            else:
                delta.setdefault("tool_calls", []).append({"index": tool_idx, "function": {"arguments": text}})

    def rewrite(self, payload: str) -> bytes:
        try:
            chunk = json.loads(payload)
        except ValueError:
            # Not JSON: we can't know what's inside, so don't pass it through
            # verbatim. Treat it as text and redact it.
            return _sse(redact_text(payload))
        if not isinstance(chunk, dict):
            return _sse(redact_text(payload))
        choices = chunk.get("choices")
        if isinstance(choices, list):
            self.template = {k: chunk[k] for k in ("id", "object", "created", "model") if k in chunk}
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                idx = choice.get("index", 0)
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                for field, value in list(delta.items()):
                    if isinstance(value, str) and field not in _OPENAI_PASSTHROUGH_FIELDS:
                        delta[field] = self._redactor(idx, field).feed(value)

                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        fn = tc.get("function") if isinstance(tc, dict) else None
                        if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                            fn["arguments"] = self._tool_redactor(idx, tc.get("index", 0)).feed(fn["arguments"])
                fc = delta.get("function_call")
                if isinstance(fc, dict) and isinstance(fc.get("arguments"), str):
                    fc["arguments"] = self._tool_redactor(idx, "function_call").feed(fc["arguments"])

                if choice.get("finish_reason") is not None:
                    for field, text in self._flush_fields(idx).items():
                        delta[field] = (delta.get(field) or "") + text
                    self._flush_tools(idx, delta)
                    self.finished.add(idx)
        return _sse(_dumps(chunk))

    def drain(self) -> bytes:
        """Release text still held for choices that never sent finish_reason."""
        out = b""
        for idx in sorted({i for i, _ in self.redactors} | set(self.tool_redactors)):
            if idx in self.finished:
                continue
            delta: dict[str, Any] = self._flush_fields(idx)
            self._flush_tools(idx, delta)
            if delta:
                chunk = {**(self.template or {"object": "chat.completion.chunk"}),
                         "choices": [{"index": idx, "delta": delta, "finish_reason": None}]}
                out += _sse(_dumps(chunk))
        return out


def redact_openai_response(data: Any) -> Any:
    for choice in (data.get("choices") if isinstance(data, dict) else None) or []:
        msg = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(msg, dict):
            continue
        for field, value in msg.items():
            if isinstance(value, str) and field not in _OPENAI_PASSTHROUGH_FIELDS:
                msg[field] = redact_text(value)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn["arguments"] = redact_json_text(fn["arguments"])
        fc = msg.get("function_call")
        if isinstance(fc, dict) and isinstance(fc.get("arguments"), str):
            fc["arguments"] = redact_json_text(fc["arguments"])
    return data


# --- Anthropic Messages stream -----------------------------------------------------

# delta type -> the field holding model-generated text
_ANTHROPIC_DELTA_FIELDS = {"text_delta": "text", "thinking_delta": "thinking", "input_json_delta": "partial_json"}
# content block type -> the delta type its stream uses
_ANTHROPIC_BLOCK_DELTAS = {"text": "text_delta", "thinking": "thinking_delta",
                           "tool_use": "input_json_delta", "server_tool_use": "input_json_delta"}


# Opaque or structural fields: changing them would break the protocol
# (signatures are verified by the API; encrypted blobs are round-tripped).
_ANTHROPIC_OPAQUE_FIELDS = frozenset({"type", "id", "name", "tool_use_id", "signature",
                                      "encrypted_content", "encrypted_index", "data", "index"})


def _redact_tree(value: Any, key: str = "") -> Any:
    """redact_value, but opaque fields are left untouched at every depth."""
    if isinstance(value, dict):
        return {k: v if k in _ANTHROPIC_OPAQUE_FIELDS else _redact_tree(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_tree(v, key) for v in value]
    return redact_value(value, key)


def _redact_other(obj: dict[str, Any]) -> None:
    """Default-deny for block/delta types without a dedicated redactor (e.g.
    citations_delta, web search results): redact every non-opaque field."""
    obj.update(_redact_tree(obj))


class AnthropicRedactingRelay:
    """Rewrites an Anthropic Messages SSE stream (message_start,
    content_block_start/delta/stop, message_delta, message_stop)."""

    def __init__(self) -> None:
        self.blocks: dict[int, tuple[str, StreamRedactor]] = {}  # index -> (delta type, redactor)
        self.stopped: set[int] = set()

    def _block(self, idx: int, delta_type: str) -> StreamRedactor:
        if idx not in self.blocks:
            self.blocks[idx] = (delta_type, StreamRedactor(json_mode=delta_type == "input_json_delta"))
        return self.blocks[idx][1]

    def _flush_event(self, idx: int) -> bytes:
        if idx not in self.blocks:
            return b""
        delta_type, r = self.blocks[idx]
        text = r.flush()
        if not text:
            return b""
        ev = {"type": "content_block_delta", "index": idx,
              "delta": {"type": delta_type, _ANTHROPIC_DELTA_FIELDS[delta_type]: text}}
        return _sse(_dumps(ev), "content_block_delta")

    def rewrite(self, event: str | None, payload: str) -> bytes:
        try:
            data = json.loads(payload)
        except ValueError:
            return _sse(redact_text(payload), event)
        if not isinstance(data, dict):
            return _sse(redact_text(payload), event)
        kind = data.get("type")
        idx = data.get("index", 0)
        prefix = b""
        if kind == "content_block_start" and isinstance(data.get("content_block"), dict):
            block = data["content_block"]
            delta_type = _ANTHROPIC_BLOCK_DELTAS.get(block.get("type"))
            if delta_type:
                r = self._block(idx, delta_type)
                for field in ("text", "thinking"):
                    if isinstance(block.get(field), str):
                        block[field] = r.feed(block[field])
                if "input" in block:
                    block["input"] = redact_value(block["input"])
            else:
                _redact_other(block)
        elif kind == "content_block_delta" and isinstance(data.get("delta"), dict):
            delta = data["delta"]
            field = _ANTHROPIC_DELTA_FIELDS.get(delta.get("type"))
            if field and isinstance(delta.get(field), str):
                delta[field] = self._block(idx, delta["type"]).feed(delta[field])
            elif not field:
                _redact_other(delta)
        elif kind == "content_block_stop":
            prefix = self._flush_event(idx)
            self.stopped.add(idx)
        elif kind == "message_start" and isinstance(data.get("message"), dict):
            redact_anthropic_response(data["message"])
        return prefix + _sse(_dumps(data), event)

    def drain(self) -> bytes:
        return b"".join(self._flush_event(i) for i in sorted(self.blocks) if i not in self.stopped)


def redact_anthropic_response(msg: Any) -> Any:
    for block in (msg.get("content") if isinstance(msg, dict) else None) or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in _ANTHROPIC_BLOCK_DELTAS:
            for field in ("text", "thinking"):
                if isinstance(block.get(field), str):
                    block[field] = redact_text(block[field])
            if "input" in block:
                block["input"] = redact_value(block["input"])
            if isinstance(block.get("citations"), list):
                block["citations"] = _redact_tree(block["citations"])
        else:
            _redact_other(block)
    return msg


# --- SSE parsing --------------------------------------------------------------------


async def iter_sse(upstream: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
    """Yield (event name, data payload) per SSE event. Comments and id/retry
    lines carry no model text and are dropped."""
    event: str | None = None
    data_lines: list[str] = []
    async for line in upstream.aiter_lines():
        if line.startswith("data:"):
            data_lines.append(line[5:].removeprefix(" "))
        elif line.startswith("event:"):
            event = line[6:].strip() or None
        elif line.strip() == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = None, []
    if data_lines:
        yield event, "\n".join(data_lines)


# --- app -----------------------------------------------------------------------------


def create_app(upstream_base_url: str, upstream_api_key: str | None = None,
               client: httpx.AsyncClient | None = None, *,
               anthropic_base_url: str | None = None, anthropic_api_key: str | None = None,
               api_keys: Iterable[str] | None = None) -> Starlette:
    openai_url = upstream_base_url.rstrip("/") + "/chat/completions"
    anthropic_url = (anthropic_base_url or upstream_base_url).rstrip("/") + "/messages"
    key_digests = [hashlib.sha256(k.encode()).digest() for k in (api_keys or []) if k]
    owns_client = client is None

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)
        )
        try:
            yield
        finally:
            if owns_client:
                await app.state.client.aclose()

    def authorized(request: Request) -> bool:
        if not key_digests:
            return True
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip() if scheme.lower() == "bearer" else request.headers.get("x-api-key", "")
        if not token:
            return False
        digest = hashlib.sha256(token.encode()).digest()
        # Compare against every key so timing doesn't reveal which one matched.
        return sum(hmac.compare_digest(digest, d) for d in key_digests) > 0

    def upstream_failure(status: int) -> JSONResponse:
        if status == 429:
            return gateway_error(429, "rate_limited", "The model provider is rate limiting requests. Retry later.")
        if 400 <= status < 500:
            return gateway_error(400, "invalid_request", "The model provider rejected the request.")
        return gateway_error(502, "upstream_error", "The model provider returned an error.")

    async def read_body(request: Request) -> dict[str, Any] | JSONResponse:
        if not authorized(request):
            resp = gateway_error(401, "unauthorized", "Missing or invalid API key.")
            resp.headers["www-authenticate"] = "Bearer"
            return resp
        try:
            body = await request.json()
        except ValueError:
            return gateway_error(400, "invalid_request", "Request body must be JSON.")
        if not isinstance(body, dict):
            return gateway_error(400, "invalid_request", "Request body must be a JSON object.")
        return body

    async def open_upstream(request: Request, url: str, body: dict[str, Any],
                            headers: dict[str, str]) -> httpx.Response | JSONResponse:
        http: httpx.AsyncClient = request.app.state.client
        try:
            upstream = await http.send(http.build_request("POST", url, json=body, headers=headers), stream=True)
        except httpx.HTTPError as exc:
            logger.error("upstream request failed: %s: %s", type(exc).__name__, exc)
            return gateway_error(502, "upstream_unavailable", "The model provider is unavailable.")
        if upstream.status_code != 200:
            detail = (await upstream.aread())[:500]
            await upstream.aclose()
            logger.error("upstream status %s: %r", upstream.status_code, detail)
            return upstream_failure(upstream.status_code)
        return upstream

    async def read_json(upstream: httpx.Response) -> Any:
        try:
            return json.loads(await upstream.aread())
        except ValueError:
            return None
        finally:
            await upstream.aclose()

    def stream_response(gen: AsyncIterator[bytes]) -> StreamingResponse:
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

    async def completions(request: Request) -> Response:
        body = await read_body(request)
        if isinstance(body, Response):
            return body
        # The client's credentials are not forwarded; the gateway holds the
        # provider key.
        headers = {"content-type": "application/json"}
        if upstream_api_key:
            headers["authorization"] = f"Bearer {upstream_api_key}"
        upstream = await open_upstream(request, openai_url, body, headers)
        if isinstance(upstream, Response):
            return upstream

        if not body.get("stream"):
            data = await read_json(upstream)
            if not isinstance(data, dict):
                return gateway_error(502, "upstream_error", "The model provider returned an invalid response.")
            return JSONResponse(redact_openai_response(data))

        async def relay() -> AsyncIterator[bytes]:
            r = SSERedactingRelay()
            saw_done = False
            try:
                async for _event, payload in iter_sse(upstream):
                    if payload.strip() == "[DONE]":
                        saw_done = True
                        yield r.drain() + _sse("[DONE]")
                        break
                    yield r.rewrite(payload)
            except httpx.HTTPError as exc:
                logger.error("upstream stream broke: %s: %s", type(exc).__name__, exc)
            finally:
                await upstream.aclose()
            if not saw_done:
                # Abrupt end: release (redacted) held text, then tell the
                # client the stream was cut short, without internals.
                tail = r.drain()
                if tail:
                    yield tail
                yield _sse(_dumps({"error": {"type": "upstream_error",
                                             "message": "The model provider stream ended unexpectedly."}}))

        return stream_response(relay())

    async def messages(request: Request) -> Response:
        body = await read_body(request)
        if isinstance(body, Response):
            return body
        headers = {"content-type": "application/json",
                   "anthropic-version": request.headers.get("anthropic-version", DEFAULT_ANTHROPIC_VERSION)}
        if "anthropic-beta" in request.headers:
            headers["anthropic-beta"] = request.headers["anthropic-beta"]
        if anthropic_api_key:
            headers["x-api-key"] = anthropic_api_key
        upstream = await open_upstream(request, anthropic_url, body, headers)
        if isinstance(upstream, Response):
            return upstream

        if not body.get("stream"):
            data = await read_json(upstream)
            if not isinstance(data, dict):
                return gateway_error(502, "upstream_error", "The model provider returned an invalid response.")
            return JSONResponse(redact_anthropic_response(data))

        async def relay() -> AsyncIterator[bytes]:
            r = AnthropicRedactingRelay()
            saw_stop = False
            try:
                async for event, payload in iter_sse(upstream):
                    out = r.rewrite(event, payload)
                    if event == "message_stop":
                        saw_stop = True
                        out = r.drain() + out
                    yield out
                    if saw_stop:
                        break
            except httpx.HTTPError as exc:
                logger.error("upstream stream broke: %s: %s", type(exc).__name__, exc)
            finally:
                await upstream.aclose()
            if not saw_stop:
                tail = r.drain()
                if tail:
                    yield tail
                yield _sse(_dumps({"type": "error", "error": {
                    "type": "api_error", "message": "The model provider stream ended unexpectedly."}}), "error")

        return stream_response(relay())

    return Starlette(routes=[
        Route("/v1/chat/completions", completions, methods=["POST"]),
        Route("/v1/messages", messages, methods=["POST"]),
    ], lifespan=lifespan)
