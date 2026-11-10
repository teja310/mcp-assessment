"""Mock OpenAI-compatible provider for demos and tests.

`app.state` knobs:
  pieces      list[str] streamed as deltas (PII deliberately split across them)
  mode        "ok" | "429" | "500" | "drop" (cut stream before [DONE]) | "no_finish"
  n           number of choices (each streams the same pieces)
  gate        optional asyncio.Event; the stream pauses after `gate_after`
              pieces until it is set (lets tests prove the gateway forwards
              early chunks before the provider has finished)
  tool_pieces optional list[str]: after the text, stream a tool call whose
              JSON arguments arrive in these pieces (PII split across them)
  reasoning_pieces  optional list[str] streamed first as delta.reasoning_content
              (the field xAI/Grok and DeepSeek use for visible reasoning)
  requests    list of (path, headers) received, for header assertions

Serves both POST /v1/chat/completions (OpenAI) and POST /v1/messages
(Anthropic Messages, same pieces as text / input_json_delta).

Run standalone: python -m redact_gateway.mock_provider (port 9100)
"""

from __future__ import annotations

import asyncio
import json
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

DEFAULT_TOOL_PIECES = [
    '{"to": "jane', '@example.org", "card": 41111111', '11111111, "phone": "(555) 12', '3-4567", "n": 3}',
]

DEFAULT_PIECES = [
    "Sure! Reach me at jo", "hn.doe@exa", "mple.com or call. ",
    "My SSN is 123-4", "5-6789 and card 4111 11", "11 1111 1111", ", thanks.",
]


def create_mock_provider() -> Starlette:
    async def completions(request: Request) -> Response:
        st = request.app.state
        body = await request.json()
        st.requests.append((request.url.path, dict(request.headers)))
        if st.mode == "429":
            return JSONResponse({"error": {"message": "Rate limit reached for org-SECRET123"}}, status_code=429)
        if st.mode == "500":
            return Response("Traceback (most recent call last):\n  File \"/srv/internal/model.py\"", status_code=500)
        cid, created, model = "chatcmpl-mock", int(time.time()), body.get("model", "mock-model")

        if not body.get("stream"):
            message: dict = {"role": "assistant", "content": "".join(st.pieces)}
            if st.reasoning_pieces:
                message["reasoning_content"] = "".join(st.reasoning_pieces)
            if st.tool_pieces:
                message["tool_calls"] = [{"id": "call_1", "type": "function",
                                          "function": {"name": "send", "arguments": "".join(st.tool_pieces)}}]
            return JSONResponse({
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": i, "message": message, "finish_reason": "stop"} for i in range(st.n)],
            })

        def ev(choices: list[dict]) -> bytes:
            return ("data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                           "model": model, "choices": choices}) + "\n\n").encode()

        async def gen():
            yield ev([{"index": i, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
                      for i in range(st.n)])
            for piece in st.reasoning_pieces or []:
                for i in range(st.n):
                    yield ev([{"index": i, "delta": {"reasoning_content": piece}, "finish_reason": None}])
            for k, piece in enumerate(st.pieces):
                if st.gate is not None and k == st.gate_after:
                    await st.gate.wait()
                for i in range(st.n):
                    yield ev([{"index": i, "delta": {"content": piece}, "finish_reason": None}])
                await asyncio.sleep(0)
            for k, piece in enumerate(st.tool_pieces or []):
                first = {"id": "call_1", "type": "function", "function": {"name": "send", "arguments": ""}}
                for i in range(st.n):
                    tc = {"index": 0, **(first if k == 0 else {}), "function": {"arguments": piece}}
                    if k == 0:
                        tc["function"]["name"] = "send"
                    yield ev([{"index": i, "delta": {"tool_calls": [tc]}, "finish_reason": None}])
            if st.mode == "drop":
                return
            if st.mode != "no_finish":
                yield ev([{"index": i, "delta": {}, "finish_reason": "stop"} for i in range(st.n)])
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def messages(request: Request) -> Response:
        st = request.app.state
        body = await request.json()
        st.requests.append((request.url.path, dict(request.headers)))
        if st.mode == "429":
            return JSONResponse({"type": "error", "error": {"type": "rate_limit_error", "message": "org-SECRET123"}},
                                status_code=429)
        model = body.get("model", "mock-model")
        if not body.get("stream"):
            content: list[dict] = [{"type": "text", "text": "".join(st.pieces)}]
            if st.tool_pieces:
                content.append({"type": "tool_use", "id": "toolu_1", "name": "send",
                                "input": json.loads("".join(st.tool_pieces))})
            return JSONResponse({"id": "msg_mock", "type": "message", "role": "assistant", "model": model,
                                 "content": content, "stop_reason": "end_turn",
                                 "usage": {"input_tokens": 5, "output_tokens": 20}})

        def ev(name: str, data: dict) -> bytes:
            return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()

        async def gen():
            yield ev("message_start", {"type": "message_start", "message": {
                "id": "msg_mock", "type": "message", "role": "assistant", "model": model, "content": [],
                "stop_reason": None, "usage": {"input_tokens": 5, "output_tokens": 1}}})
            yield ev("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}})
            yield b"event: ping\ndata: {\"type\": \"ping\"}\n\n"
            for k, piece in enumerate(st.pieces):
                if st.gate is not None and k == st.gate_after:
                    await st.gate.wait()
                yield ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                 "delta": {"type": "text_delta", "text": piece}})
                await asyncio.sleep(0)
            if st.mode == "drop":
                return
            yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            if st.tool_pieces:
                yield ev("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
                    "type": "tool_use", "id": "toolu_1", "name": "send", "input": {}}})
                for piece in st.tool_pieces:
                    yield ev("content_block_delta", {"type": "content_block_delta", "index": 1,
                                                     "delta": {"type": "input_json_delta", "partial_json": piece}})
                yield ev("content_block_stop", {"type": "content_block_stop", "index": 1})
            yield ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                       "usage": {"output_tokens": 20}})
            yield ev("message_stop", {"type": "message_stop"})

        return StreamingResponse(gen(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"]),
                            Route("/v1/messages", messages, methods=["POST"])])
    app.state.pieces = list(DEFAULT_PIECES)
    app.state.tool_pieces = None
    app.state.reasoning_pieces = None
    app.state.requests = []
    app.state.mode = "ok"
    app.state.n = 1
    app.state.gate = None
    app.state.gate_after = 0
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_mock_provider(), host="127.0.0.1", port=9100, log_level="warning")
