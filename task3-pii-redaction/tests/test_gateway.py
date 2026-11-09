from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn

from redact_gateway.app import create_app
from redact_gateway.mock_provider import DEFAULT_PIECES, create_mock_provider
from redact_gateway.redactor import redact_text

FULL_TEXT = "".join(DEFAULT_PIECES)
EXPECTED = redact_text(FULL_TEXT)
RAW_PII_FRAGMENTS = ["john.doe", "@example", "123-45", "6789", "4111 1111", "1111 1111"]


def parse_sse(raw: bytes) -> list[Any]:
    events = []
    for block in raw.decode().split("\n\n"):
        if block.startswith("data: "):
            data = block[6:]
            events.append(data if data == "[DONE]" else json.loads(data))
    return events


def content_by_choice(events: list[Any]) -> dict[int, str]:
    out: dict[int, str] = {}
    for e in events:
        if isinstance(e, dict):
            for c in e.get("choices", []):
                out[c["index"]] = out.get(c["index"], "") + (c.get("delta", {}).get("content") or "")
    return out


@pytest.fixture
async def env() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    provider = create_mock_provider()
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=provider))
    app = create_app("http://provider/v1", "sk-gateway", client=upstream)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            yield c, provider.state
    await upstream.aclose()


def req(stream: bool = True, **kw: Any) -> dict[str, Any]:
    return {"model": "m", "stream": stream, "messages": [{"role": "user", "content": "hi"}], **kw}


async def test_stream_redacts_pii_split_across_chunks(env) -> None:
    client, _ = env
    r = await client.post("/v1/chat/completions", json=req())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.content)
    assert events[-1] == "[DONE]"
    assert content_by_choice(events) == {0: EXPECTED}
    assert "[REDACTED]" in EXPECTED
    for frag in RAW_PII_FRAGMENTS:
        assert frag not in r.text, f"raw PII fragment {frag!r} on the wire"


async def test_stream_preserves_chunk_metadata_and_finish(env) -> None:
    client, _ = env
    events = parse_sse((await client.post("/v1/chat/completions", json=req())).content)
    chunks = [e for e in events if isinstance(e, dict)]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert all(c["id"] == "chatcmpl-mock" and c["object"] == "chat.completion.chunk" for c in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


async def test_multiple_choices_redacted_independently(env) -> None:
    client, st = env
    st.n = 3
    events = parse_sse((await client.post("/v1/chat/completions", json=req(n=3))).content)
    assert content_by_choice(events) == {0: EXPECTED, 1: EXPECTED, 2: EXPECTED}


async def test_held_text_flushed_when_provider_omits_finish_reason(env) -> None:
    client, st = env
    st.mode = "no_finish"
    st.pieces = ["mail me: a@b", ".io"]  # ends mid-candidate-run: must still be flushed
    events = parse_sse((await client.post("/v1/chat/completions", json=req())).content)
    assert content_by_choice(events) == {0: "mail me: [REDACTED]"}
    assert events[-1] == "[DONE]"


async def test_truncated_stream_flushes_redacted_and_reports_error(env) -> None:
    client, st = env
    st.mode = "drop"
    st.pieces = ["ssn 123-45-", "6789"]
    r = await client.post("/v1/chat/completions", json=req())
    events = parse_sse(r.content)
    assert content_by_choice(events) == {0: "ssn [REDACTED]"}
    assert events[-1]["error"]["type"] == "upstream_error"
    assert "6789" not in r.text


async def test_non_streaming_response_redacted(env) -> None:
    client, _ = env
    r = await client.post("/v1/chat/completions", json=req(stream=False))
    assert r.json()["choices"][0]["message"]["content"] == EXPECTED


@pytest.mark.parametrize("mode, status, err_type", [("429", 429, "rate_limited"), ("500", 502, "upstream_error")])
async def test_upstream_errors_are_sanitized(env, mode: str, status: int, err_type: str) -> None:
    client, st = env
    st.mode = mode
    r = await client.post("/v1/chat/completions", json=req())
    assert r.status_code == status
    body = r.json()
    assert set(body) == {"error"} and set(body["error"]) == {"type", "message", "request_id"}
    assert body["error"]["type"] == err_type
    assert "Traceback" not in r.text and "SECRET" not in r.text and "/srv" not in r.text


async def test_upstream_unreachable() -> None:
    app = create_app("http://127.0.0.1:1/v1")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            r = await c.post("/v1/chat/completions", json=req())
    assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_unavailable"


async def test_invalid_json_body(env) -> None:
    client, _ = env
    r = await client.post("/v1/chat/completions", content=b"{nope")
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request"


# --- TTFT: proven over real sockets -----------------------------------------------
# httpx.ASGITransport buffers whole responses, so it can't show streaming.
# Here both servers run in uvicorn on real ports in this event loop. The
# provider sends its first pieces, then BLOCKS until the test has received
# them through the gateway. A gateway that buffered the response would
# deadlock and this test would time out.


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve(app, port: int) -> tuple[uvicorn.Server, asyncio.Task]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


async def test_first_tokens_reach_client_before_provider_finishes() -> None:
    provider = create_mock_provider()
    provider.state.pieces = ["Hello there ", "friend, ", "a@b.io ", "bye."]
    provider.state.gate = asyncio.Event()
    provider.state.gate_after = 2  # pause after "Hello there " and "friend, "
    p_port, g_port = _free_port(), _free_port()
    servers = [await _serve(provider, p_port),
               await _serve(create_app(f"http://127.0.0.1:{p_port}/v1"), g_port)]
    try:
        async with httpx.AsyncClient() as c:
            async with c.stream("POST", f"http://127.0.0.1:{g_port}/v1/chat/completions", json=req()) as r:
                seen = ""
                lines = r.aiter_lines()

                async def read_until_friend() -> None:
                    nonlocal seen
                    async for line in lines:
                        if line.startswith("data: {"):
                            for ch in json.loads(line[6:])["choices"]:
                                seen += ch["delta"].get("content") or ""
                        if "friend," in seen:
                            return

                await asyncio.wait_for(read_until_friend(), timeout=5)
                assert seen == "Hello there friend, "  # arrived while the provider is still blocked
                provider.state.gate.set()
                async for line in lines:
                    if line.startswith("data: {"):
                        for ch in json.loads(line[6:])["choices"]:
                            seen += ch["delta"].get("content") or ""
        assert seen == "Hello there friend, [REDACTED] bye."
    finally:
        for server, task in servers:
            server.should_exit = True
            await task
