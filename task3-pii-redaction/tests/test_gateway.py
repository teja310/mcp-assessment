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


# --- tool-call arguments (OpenAI) --------------------------------------------------

from redact_gateway.mock_provider import DEFAULT_TOOL_PIECES  # noqa: E402

TOOL_RAW = ["jane@example.org", "4111111111111111", "(555) 123-4567", "@example.org", "123-4567"]
TOOL_EXPECTED = {"to": "[REDACTED]", "card": "[REDACTED]", "phone": "[REDACTED]", "n": 3}


def tool_args_by_choice(events: list[Any]) -> dict[int, str]:
    out: dict[int, str] = {}
    for e in events:
        if isinstance(e, dict):
            for c in e.get("choices", []):
                for tc in c.get("delta", {}).get("tool_calls") or []:
                    out[c["index"]] = out.get(c["index"], "") + (tc.get("function", {}).get("arguments") or "")
    return out


async def test_stream_tool_call_arguments_redacted_and_valid_json(env) -> None:
    client, st = env
    st.tool_pieces = list(DEFAULT_TOOL_PIECES)
    st.n = 2
    r = await client.post("/v1/chat/completions", json=req(n=2))
    events = parse_sse(r.content)
    assert content_by_choice(events) == {0: EXPECTED, 1: EXPECTED}
    args = tool_args_by_choice(events)
    assert {i: json.loads(a) for i, a in args.items()} == {0: TOOL_EXPECTED, 1: TOOL_EXPECTED}
    for frag in TOOL_RAW + ["41111111", "11111111"]:
        assert frag not in r.text, frag
    first_tc = next(tc for e in events if isinstance(e, dict) for c in e["choices"]
                    for tc in c["delta"].get("tool_calls") or [])
    assert first_tc["id"] == "call_1" and first_tc["function"]["name"] == "send"


async def test_tool_arguments_flushed_when_stream_drops(env) -> None:
    client, st = env
    st.mode = "drop"
    st.tool_pieces = ['{"to": "a@b', '.io"}']
    events = parse_sse((await client.post("/v1/chat/completions", json=req())).content)
    assert json.loads(tool_args_by_choice(events)[0]) == {"to": "[REDACTED]"}
    assert events[-1]["error"]["type"] == "upstream_error"


async def test_non_streaming_tool_call_arguments_redacted(env) -> None:
    client, st = env
    st.tool_pieces = list(DEFAULT_TOOL_PIECES)
    msg = (await client.post("/v1/chat/completions", json=req(stream=False))).json()["choices"][0]["message"]
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == TOOL_EXPECTED


# --- Anthropic Messages API --------------------------------------------------------


def parse_named_sse(raw: bytes) -> list[tuple[str | None, Any]]:
    out = []
    for block in raw.decode().split("\n\n"):
        if not block.strip():
            continue
        name, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((name, data))
    return out


def anthropic_req(stream: bool = True) -> dict[str, Any]:
    return {"model": "claude-x", "max_tokens": 100, "stream": stream,
            "messages": [{"role": "user", "content": "hi"}]}


async def test_anthropic_stream_redacts_text_and_tool_input(env) -> None:
    client, st = env
    st.tool_pieces = list(DEFAULT_TOOL_PIECES)
    r = await client.post("/v1/messages", json=anthropic_req(),
                          headers={"x-api-key": "client-key", "anthropic-version": "2023-06-01"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_named_sse(r.content)
    for name, data in events:
        assert name == data["type"], "every event keeps its SSE event name"
    text = "".join(d["delta"]["text"] for n, d in events if n == "content_block_delta" and d["index"] == 0)
    tool = "".join(d["delta"]["partial_json"] for n, d in events if n == "content_block_delta" and d["index"] == 1)
    assert text == EXPECTED
    assert json.loads(tool) == TOOL_EXPECTED
    names = [n for n, _ in events]
    assert names[0] == "message_start" and names[-1] == "message_stop" and "ping" in names
    # Held text is released *before* its block's content_block_stop.
    for idx in (0, 1):
        stop = next(i for i, (n, d) in enumerate(events) if n == "content_block_stop" and d["index"] == idx)
        assert all(not (n == "content_block_delta" and d["index"] == idx) for n, d in events[stop:])
    for frag in RAW_PII_FRAGMENTS + TOOL_RAW:
        assert frag not in r.text, frag
    # Provider got the gateway's key, not the client's; version passed through.
    path, headers = st.requests[-1]
    assert path == "/v1/messages" and headers["anthropic-version"] == "2023-06-01"
    assert headers.get("x-api-key") is None  # the env fixture configures no Anthropic key
    assert "client-key" not in json.dumps(headers)


async def test_anthropic_non_streaming_redacted(env) -> None:
    client, st = env
    st.tool_pieces = list(DEFAULT_TOOL_PIECES)
    msg = (await client.post("/v1/messages", json=anthropic_req(stream=False))).json()
    assert msg["content"][0]["text"] == EXPECTED
    assert msg["content"][1]["input"] == TOOL_EXPECTED


async def test_anthropic_truncated_stream_reports_error(env) -> None:
    client, st = env
    st.mode = "drop"
    st.pieces = ["ssn 123-45-", "6789"]
    r = await client.post("/v1/messages", json=anthropic_req())
    events = parse_named_sse(r.content)
    text = "".join(d["delta"]["text"] for n, d in events if n == "content_block_delta")
    assert text == "ssn [REDACTED]" and "6789" not in r.text
    assert events[-1][0] == "error" and events[-1][1]["error"]["type"] == "api_error"


async def test_anthropic_upstream_error_sanitized(env) -> None:
    client, st = env
    st.mode = "429"
    r = await client.post("/v1/messages", json=anthropic_req())
    assert r.status_code == 429 and "SECRET" not in r.text


async def test_anthropic_key_sent_upstream() -> None:
    provider = create_mock_provider()
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=provider))
    app = create_app("http://provider/v1", client=upstream, anthropic_api_key="sk-ant-gateway")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            await c.post("/v1/messages", json=anthropic_req(stream=False), headers={"x-api-key": "client"})
    await upstream.aclose()
    assert provider.state.requests[-1][1]["x-api-key"] == "sk-ant-gateway"


# --- optional client authentication -----------------------------------------------


@pytest.fixture
async def authed() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    provider = create_mock_provider()
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=provider))
    app = create_app("http://provider/v1", "sk-gateway", client=upstream, api_keys=["team-key-1", "team-key-2"])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            yield c, provider.state
    await upstream.aclose()


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages"])
@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"}, {"x-api-key": "wrong"},
                                     {"authorization": "Basic team-key-1"}])
async def test_auth_rejects_missing_or_wrong_key(authed, path: str, headers: dict[str, str]) -> None:
    client, st = authed
    r = await client.post(path, json=req(), headers=headers)
    assert r.status_code == 401 and r.json()["error"]["type"] == "unauthorized"
    assert r.headers["www-authenticate"] == "Bearer"
    assert st.requests == []  # provider never contacted


@pytest.mark.parametrize("headers", [{"authorization": "Bearer team-key-1"}, {"x-api-key": "team-key-2"}])
async def test_auth_accepts_valid_key_and_does_not_forward_it(authed, headers: dict[str, str]) -> None:
    client, st = authed
    r = await client.post("/v1/chat/completions", json=req(stream=False), headers=headers)
    assert r.status_code == 200
    forwarded = json.dumps(st.requests[-1][1])
    assert "team-key" not in forwarded and st.requests[-1][1]["authorization"] == "Bearer sk-gateway"


async def test_anthropic_first_tokens_before_provider_finishes() -> None:
    provider = create_mock_provider()
    provider.state.pieces = ["Hello there ", "friend, ", "a@b.io ", "bye."]
    provider.state.gate = asyncio.Event()
    provider.state.gate_after = 2
    p_port, g_port = _free_port(), _free_port()
    servers = [await _serve(provider, p_port),
               await _serve(create_app(f"http://127.0.0.1:{p_port}/v1"), g_port)]
    try:
        async with httpx.AsyncClient() as c:
            async with c.stream("POST", f"http://127.0.0.1:{g_port}/v1/messages", json=anthropic_req()) as r:
                seen = ""
                lines = r.aiter_lines()

                def take(line: str) -> str:
                    if line.startswith("data: {"):
                        d = json.loads(line[6:])
                        if d["type"] == "content_block_delta":
                            return d["delta"].get("text", "")
                    return ""

                async def read_until_friend() -> None:
                    nonlocal seen
                    async for line in lines:
                        seen += take(line)
                        if "friend," in seen:
                            return

                await asyncio.wait_for(read_until_friend(), timeout=5)
                assert seen == "Hello there friend, "
                provider.state.gate.set()
                async for line in lines:
                    seen += take(line)
        assert seen == "Hello there friend, [REDACTED] bye."
    finally:
        for server, task in servers:
            server.should_exit = True
            await task


# --- default-deny: every string field of an OpenAI delta is redacted ------------------
# Found in a live xAI run: Grok streams its reasoning in delta.reasoning_content,
# which quoted the user's PII and was passed through unredacted.

from redact_gateway.app import SSERedactingRelay, redact_openai_response  # noqa: E402

REASONING_PIECES = ["The user gave jane.d", "oe@example.org and SSN ", "123456789 so I will", " save it."]


def field_by_choice(events: list[Any], field: str) -> str:
    return "".join(c["delta"].get(field) or "" for e in events if isinstance(e, dict) for c in e["choices"])


async def test_stream_reasoning_content_redacted(env) -> None:
    client, st = env
    st.reasoning_pieces = list(REASONING_PIECES)
    r = await client.post("/v1/chat/completions", json=req())
    events = parse_sse(r.content)
    assert field_by_choice(events, "reasoning_content") == "The user gave [REDACTED] and SSN [REDACTED] so I will save it."
    assert content_by_choice(events) == {0: EXPECTED}
    for frag in ["jane.d", "oe@example", "123456789"]:
        assert frag not in r.text, frag


async def test_non_streaming_reasoning_content_redacted(env) -> None:
    client, st = env
    st.reasoning_pieces = list(REASONING_PIECES)
    msg = (await client.post("/v1/chat/completions", json=req(stream=False))).json()["choices"][0]["message"]
    assert "jane.doe" not in msg["reasoning_content"] and "123456789" not in msg["reasoning_content"]
    assert msg["role"] == "assistant"


def _chunk(delta: dict[str, Any], finish: str | None = None) -> str:
    return json.dumps({"id": "c", "object": "chat.completion.chunk",
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


def test_unknown_future_field_is_redacted_and_role_kept() -> None:
    r = SSERedactingRelay()
    out = b"".join([
        r.rewrite(_chunk({"role": "assistant", "brand_new_text": "mail a@b"})),
        r.rewrite(_chunk({"brand_new_text": ".io now"})),
        r.rewrite(_chunk({}, finish="stop")),
    ])
    events = parse_sse(out)
    assert field_by_choice(events, "brand_new_text") == "mail [REDACTED] now"
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "a@b" not in out.decode()


def test_legacy_function_call_arguments_redacted_as_json() -> None:
    r = SSERedactingRelay()
    out = b"".join([
        r.rewrite(_chunk({"function_call": {"name": "f", "arguments": '{"card": 41111111'}})),
        r.rewrite(_chunk({"function_call": {"arguments": "11111111}"}})),
        r.rewrite(_chunk({}, finish="function_call")),
    ])
    args = "".join((c["delta"].get("function_call") or {}).get("arguments", "")
                   for e in parse_sse(out) if isinstance(e, dict) for c in e["choices"])
    assert json.loads(args) == {"card": "[REDACTED]"}
    msg = {"choices": [{"message": {"role": "assistant", "refusal": "no, a@b.io",
                                    "function_call": {"name": "f", "arguments": '{"e": "a@b.io"}'}}}]}
    m = redact_openai_response(msg)["choices"][0]["message"]
    assert m["refusal"] == "no, [REDACTED]" and json.loads(m["function_call"]["arguments"]) == {"e": "[REDACTED]"}


from redact_gateway.app import AnthropicRedactingRelay, redact_anthropic_response  # noqa: E402


def test_anthropic_unknown_deltas_and_blocks_redacted_opaque_fields_kept() -> None:
    r = AnthropicRedactingRelay()
    sig = "EqQBCkYIARgCKkA+sig/with+base64=="
    out = b"".join([
        r.rewrite("content_block_start", json.dumps({"type": "content_block_start", "index": 0,
                  "content_block": {"type": "text", "text": ""}})),
        r.rewrite("content_block_delta", json.dumps({"type": "content_block_delta", "index": 0, "delta": {
            "type": "citations_delta", "citation": {"type": "char_location", "cited_text": "call (555) 123-4567",
                                                    "document_index": 0, "start_char_index": 3}}})),
        r.rewrite("content_block_delta", json.dumps({"type": "content_block_delta", "index": 0,
                  "delta": {"type": "signature_delta", "signature": sig}})),
        r.rewrite("content_block_start", json.dumps({"type": "content_block_start", "index": 1, "content_block": {
            "type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
            "content": [{"type": "web_search_result", "title": "Contact a@b.io", "url": "https://x.io",
                         "encrypted_content": "ENC+a@b.io/opaque"}]}})),
    ]).decode()
    events = parse_named_sse(out.encode())
    cited = events[1][1]["delta"]["citation"]
    assert cited["cited_text"] == "call [REDACTED]" and cited["start_char_index"] == 3
    assert events[2][1]["delta"]["signature"] == sig  # verified by the API: untouched
    result = events[3][1]["content_block"]["content"][0]
    assert result["title"] == "Contact [REDACTED]" and result["encrypted_content"] == "ENC+a@b.io/opaque"
    assert events[3][1]["content_block"]["tool_use_id"] == "srvtoolu_1"


def test_anthropic_non_streaming_citations_and_unknown_blocks() -> None:
    msg = {"content": [
        {"type": "text", "text": "see doc", "citations": [{"type": "char_location", "cited_text": "SSN 123-45-6789"}]},
        {"type": "future_block", "id": "b1", "body": "mail a@b.io"},
    ]}
    out = redact_anthropic_response(msg)["content"]
    assert out[0]["citations"][0]["cited_text"] == "SSN [REDACTED]"
    assert out[1] == {"type": "future_block", "id": "b1", "body": "mail [REDACTED]"}
