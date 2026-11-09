"""LLM gateway endpoint: POST /v1/chat/completions (OpenAI-compatible).

Streaming path (`"stream": true`):

    client -> gateway -> provider
    provider SSE  --aiter_lines-->  parse event  --> per-choice StreamRedactor
                                                 --> re-serialize --> client

Nothing accumulates beyond one SSE line plus each redactor's small holdback,
and every event is yielded the moment it's processed, so time to first
token is the provider's TTFT plus at most one partial word.

Non-streaming path: the full JSON body is redacted with `redact_text`.

Upstream failures never leak provider bodies, headers or stack traces to
the client: they become a fixed gateway error payload; details go to the log.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from redact_gateway.redactor import StreamRedactor, redact_text

logger = logging.getLogger("redact_gateway")


def gateway_error(status: int, err_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"type": err_type, "message": message, "request_id": uuid.uuid4().hex}},
        status_code=status,
    )


def _sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


class SSERedactingRelay:
    """Rewrites an OpenAI-style chat.completion.chunk SSE stream."""

    def __init__(self) -> None:
        self.redactors: dict[int, StreamRedactor] = {}
        self.finished: set[int] = set()
        self.template: dict[str, Any] | None = None

    def _redactor(self, idx: int) -> StreamRedactor:
        return self.redactors.setdefault(idx, StreamRedactor())

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
                content = delta.get("content")
                out = self._redactor(idx).feed(content) if isinstance(content, str) else ""
                if choice.get("finish_reason") is not None:
                    out += self._redactor(idx).flush()
                    self.finished.add(idx)
                if isinstance(content, str) or out:
                    delta["content"] = out
        return _sse(json.dumps(chunk, separators=(",", ":")))

    def drain(self) -> bytes:
        """Release text still held for choices that never sent finish_reason."""
        out = b""
        for idx, r in self.redactors.items():
            if idx in self.finished:
                continue
            text = r.flush()
            if text:
                chunk = {**(self.template or {"object": "chat.completion.chunk"}),
                         "choices": [{"index": idx, "delta": {"content": text}, "finish_reason": None}]}
                out += _sse(json.dumps(chunk, separators=(",", ":")))
        return out


def create_app(upstream_base_url: str, upstream_api_key: str | None = None,
               client: httpx.AsyncClient | None = None) -> Starlette:
    url = upstream_base_url.rstrip("/") + "/chat/completions"
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

    def upstream_headers() -> dict[str, str]:
        # The client's credentials are not forwarded; the gateway holds the
        # provider key.
        h = {"content-type": "application/json"}
        if upstream_api_key:
            h["authorization"] = f"Bearer {upstream_api_key}"
        return h

    def upstream_failure(status: int) -> JSONResponse:
        if status == 429:
            return gateway_error(429, "rate_limited", "The model provider is rate limiting requests. Retry later.")
        if 400 <= status < 500:
            return gateway_error(400, "invalid_request", "The model provider rejected the request.")
        return gateway_error(502, "upstream_error", "The model provider returned an error.")

    async def completions(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return gateway_error(400, "invalid_request", "Request body must be JSON.")
        if not isinstance(body, dict):
            return gateway_error(400, "invalid_request", "Request body must be a JSON object.")

        http: httpx.AsyncClient = request.app.state.client
        req = http.build_request("POST", url, json=body, headers=upstream_headers())
        try:
            upstream = await http.send(req, stream=True)
        except httpx.HTTPError as exc:
            logger.error("upstream request failed: %s: %s", type(exc).__name__, exc)
            return gateway_error(502, "upstream_unavailable", "The model provider is unavailable.")

        if upstream.status_code != 200:
            detail = (await upstream.aread())[:500]
            await upstream.aclose()
            logger.error("upstream status %s: %r", upstream.status_code, detail)
            return upstream_failure(upstream.status_code)

        if not body.get("stream"):
            try:
                data = json.loads(await upstream.aread())
            except ValueError:
                return gateway_error(502, "upstream_error", "The model provider returned an invalid response.")
            finally:
                await upstream.aclose()
            for choice in data.get("choices") or []:
                msg = choice.get("message") if isinstance(choice, dict) else None
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    msg["content"] = redact_text(msg["content"])
            return JSONResponse(data)

        async def relay() -> AsyncIterator[bytes]:
            r = SSERedactingRelay()
            data_lines: list[str] = []
            saw_done = False
            try:
                async for line in upstream.aiter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].removeprefix(" "))
                        continue
                    if line.strip() == "" and data_lines:
                        payload = "\n".join(data_lines)
                        data_lines = []
                        if payload.strip() == "[DONE]":
                            saw_done = True
                            yield r.drain() + _sse("[DONE]")
                            break
                        yield r.rewrite(payload)
                    # Comments (":"), "event:", "id:", "retry:" lines carry no
                    # model text; dropping them keeps the client stream clean.
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
                yield _sse(json.dumps({"error": {"type": "upstream_error",
                                                 "message": "The model provider stream ended unexpectedly."}}))

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    return Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])], lifespan=lifespan)
