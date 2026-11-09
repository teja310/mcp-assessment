"""Mock OpenAI-compatible provider with a controllable failure mode.

`app.state.mode`:
  ok          200 with a completion and usage.total_tokens = app.state.usage
  429         rate limited (body contains a fake internal secret)
  500         internal error with a fake stack trace in the body
  400         rejects the request
  hang        sleeps `app.state.delay` seconds before answering
  slow_body   sends headers immediately, then stalls `delay` before the body
  garbage     200 with a non-JSON body

`app.state.calls` counts requests received; `app.state.completed` counts
responses fully sent (a cancelled/timed-out request never completes).

Standalone: python -m llm_router.mock_provider <port> <name> [mode]
"""

from __future__ import annotations

import asyncio
import json
import sys

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


def create_mock_provider(name: str = "mock", mode: str = "ok") -> Starlette:
    async def completions(request: Request) -> Response:
        st = request.app.state
        st.calls += 1
        body = await request.json()
        st.last_body = body
        ok = {
            "id": f"cmpl-{name}-{st.calls}", "object": "chat.completion", "model": body.get("model", name),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"hello from {name}"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": st.usage - 10, "total_tokens": st.usage},
        }
        if st.mode == "429":
            return JSONResponse({"error": "quota for org-INTERNAL-7731 exceeded"}, status_code=429)
        if st.mode == "500":
            return Response('Traceback (most recent call last):\n  File "/opt/provider/secret_infer.py", line 9',
                            status_code=500)
        if st.mode == "400":
            return JSONResponse({"error": "unknown parameter 'foo' at /internal/validator"}, status_code=400)
        if st.mode == "garbage":
            return Response("<html>oops</html>", status_code=200)
        if st.mode == "hang":
            await asyncio.sleep(st.delay)
        if st.mode == "slow_body":
            async def trickle():
                # Headers now, then one whitespace byte every 0.5s: never
                # idle long enough to trip a per-read timeout, yet the whole
                # response takes `delay` seconds.
                yield b'{"id": "partial", '
                for _ in range(int(st.delay / 0.5)):
                    await asyncio.sleep(0.5)
                    yield b" "
                yield json.dumps(ok)[1:].encode()
                st.completed += 1
            return StreamingResponse(trickle(), media_type="application/json")
        st.completed += 1
        return JSONResponse(ok)

    app = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])
    app.state.mode = mode
    app.state.delay = 5.0
    app.state.usage = 42
    app.state.calls = 0
    app.state.completed = 0
    app.state.last_body = None
    return app


if __name__ == "__main__":
    import uvicorn

    port, name = int(sys.argv[1]), sys.argv[2]
    uvicorn.run(create_mock_provider(name, sys.argv[3] if len(sys.argv) > 3 else "ok"),
                host="127.0.0.1", port=port, log_level="warning")
