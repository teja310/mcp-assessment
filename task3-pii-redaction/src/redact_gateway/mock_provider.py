"""Mock OpenAI-compatible provider for demos and tests.

`app.state` knobs:
  pieces      list[str] streamed as deltas (PII deliberately split across them)
  mode        "ok" | "429" | "500" | "drop" (cut stream before [DONE]) | "no_finish"
  n           number of choices (each streams the same pieces)
  gate        optional asyncio.Event; the stream pauses after `gate_after`
              pieces until it is set (lets tests prove the gateway forwards
              early chunks before the provider has finished)

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

DEFAULT_PIECES = [
    "Sure! Reach me at jo", "hn.doe@exa", "mple.com or call. ",
    "My SSN is 123-4", "5-6789 and card 4111 11", "11 1111 1111", ", thanks.",
]


def create_mock_provider() -> Starlette:
    async def completions(request: Request) -> Response:
        st = request.app.state
        body = await request.json()
        if st.mode == "429":
            return JSONResponse({"error": {"message": "Rate limit reached for org-SECRET123"}}, status_code=429)
        if st.mode == "500":
            return Response("Traceback (most recent call last):\n  File \"/srv/internal/model.py\"", status_code=500)
        cid, created, model = "chatcmpl-mock", int(time.time()), body.get("model", "mock-model")

        if not body.get("stream"):
            return JSONResponse({
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": i, "message": {"role": "assistant", "content": "".join(st.pieces)},
                             "finish_reason": "stop"} for i in range(st.n)],
            })

        def ev(choices: list[dict]) -> bytes:
            return ("data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                           "model": model, "choices": choices}) + "\n\n").encode()

        async def gen():
            yield ev([{"index": i, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
                      for i in range(st.n)])
            for k, piece in enumerate(st.pieces):
                if st.gate is not None and k == st.gate_after:
                    await st.gate.wait()
                for i in range(st.n):
                    yield ev([{"index": i, "delta": {"content": piece}, "finish_reason": None}])
                await asyncio.sleep(0)
            if st.mode == "drop":
                return
            if st.mode != "no_finish":
                yield ev([{"index": i, "delta": {}, "finish_reason": "stop"} for i in range(st.n)])
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])
    app.state.pieces = list(DEFAULT_PIECES)
    app.state.mode = "ok"
    app.state.n = 1
    app.state.gate = None
    app.state.gate_after = 0
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_mock_provider(), host="127.0.0.1", port=9100, log_level="warning")
