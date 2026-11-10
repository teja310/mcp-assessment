from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from llm_router.app import create_app
from llm_router.limiter import SqliteTokenLimiter
from llm_router.mock_provider import create_mock_provider
from llm_router.router import ModelRouter, Provider

KEY = "tenant-key-1"
LEAK_MARKERS = ["INTERNAL", "Traceback", "secret_infer", "/opt/", "/internal/", "primary.local",
                "secondary.local", "httpx", "Exception"]


def body(max_tokens: int = 100, **kw: Any) -> dict[str, Any]:
    return {"model": "gpt-x", "messages": [{"role": "user", "content": "hi there"}], "max_tokens": max_tokens, **kw}


class Env:
    def __init__(self, tmp_path: Path, primary_timeout: float = 0.3, limit: int = 50_000) -> None:
        self.primary = create_mock_provider("primary")
        self.secondary = create_mock_provider("secondary")
        self.client = httpx.AsyncClient(mounts={
            "http://primary.local": httpx.ASGITransport(app=self.primary),
            "http://secondary.local": httpx.ASGITransport(app=self.secondary),
        })
        self.limiter = SqliteTokenLimiter(tmp_path / "router.db", limit=limit)
        self.router = ModelRouter(
            self.limiter,
            Provider("primary", "http://primary.local/v1"),
            Provider("secondary", "http://secondary.local/v1", model="backup-model"),
            self.client,
            primary_timeout=primary_timeout,
            secondary_timeout=1.0,
        )

    async def close(self) -> None:
        await self.client.aclose()
        self.limiter.close()


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e = Env(tmp_path)
    yield e
    await e.close()


def assert_standard_error(r, err_type: str, status: int) -> None:
    assert r.status == status, r.body
    assert set(r.body) == {"error"}
    assert set(r.body["error"]) == {"type", "message", "request_id"}
    assert r.body["error"]["type"] == err_type
    text = str(r.body) + str(r.headers)
    for marker in LEAK_MARKERS:
        assert marker not in text, f"leaked {marker!r}: {text}"


# --- routing ---------------------------------------------------------------------


async def test_primary_success_secondary_untouched(env: Env) -> None:
    r = await env.router.complete(KEY, body())
    assert r.status == 200 and r.headers["x-gateway-route"] == "primary"
    assert r.body["choices"][0]["message"]["content"] == "hello from primary"
    assert env.secondary.state.calls == 0


@pytest.mark.parametrize("mode", ["429", "hang", "500", "garbage"])
async def test_failover_to_secondary(env: Env, mode: str) -> None:
    env.primary.state.mode = mode
    r = await env.router.complete(KEY, body())
    assert r.status == 200 and r.headers["x-gateway-route"] == "fallback"
    assert r.body["choices"][0]["message"]["content"] == "hello from secondary"
    assert env.secondary.state.last_body["model"] == "backup-model"  # per-provider model override


async def test_connection_error_fails_over(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.router.providers[0] = (Provider("primary", "http://127.0.0.1:1/v1"), 0.5)
    try:
        r = await e.router.complete(KEY, body())
        assert r.status == 200 and r.headers["x-gateway-route"] == "fallback"
    finally:
        await e.close()


async def test_client_error_does_not_fail_over(env: Env) -> None:
    env.primary.state.mode = "400"
    r = await env.router.complete(KEY, body())
    assert_standard_error(r, "invalid_request", 400)
    assert env.secondary.state.calls == 0
    assert env.limiter.usage(KEY) == 0


async def test_timeout_cancels_primary_and_never_uses_late_answer(env: Env) -> None:
    env.primary.state.mode = "hang"
    env.primary.state.delay = 1.0
    t0 = time.monotonic()
    r = await env.router.complete(KEY, body())
    elapsed = time.monotonic() - t0
    assert r.headers["x-gateway-route"] == "fallback"
    assert 0.3 <= elapsed < 0.9, elapsed
    await asyncio.sleep(1.2)  # well past when the primary would have answered
    assert env.primary.state.completed == 0, "primary request must have been cancelled"


async def test_both_providers_down_is_standardized(env: Env) -> None:
    env.primary.state.mode = "500"
    env.secondary.state.mode = "429"
    r = await env.router.complete(KEY, body())
    assert_standard_error(r, "upstream_unavailable", 503)
    assert r.headers["retry-after"] == "1"
    assert env.limiter.usage(KEY) == 0, "failed request must not consume budget"


# --- rate limiting through the router ----------------------------------------------


async def test_actual_usage_is_committed_not_estimate(env: Env) -> None:
    env.primary.state.usage = 77
    await env.router.complete(KEY, body(max_tokens=5000))
    assert env.limiter.usage(KEY) == 77


async def test_extra_reserve_tokens_cover_reasoning_usage(tmp_path: Path) -> None:
    """A reasoning model reported usage=335 for max_tokens=50 (live xAI run).
    Without headroom, two such requests pass a 400-token budget and the tenant
    ends at 670. With EXTRA_RESERVE_TOKENS the second is refused up front."""
    for extra, second_status, final_usage in [(0, 200, 670), (300, 429, 335)]:
        (tmp_path / f"x{extra}").mkdir()
        e = Env(tmp_path / f"x{extra}", limit=400)
        e.router.extra_reserve_tokens = extra
        try:
            e.primary.state.usage = 335
            assert (await e.router.complete(KEY, body(max_tokens=50))).status == 200
            assert (await e.router.complete(KEY, body(max_tokens=50))).status == second_status
            assert e.limiter.usage(KEY) == final_usage
        finally:
            await e.close()


async def test_rate_limited_tenant_gets_429_and_providers_not_called(tmp_path: Path) -> None:
    e = Env(tmp_path, limit=2_000)
    try:
        e.primary.state.usage = 1_500
        assert (await e.router.complete(KEY, body(max_tokens=1_000))).status == 200
        r = await e.router.complete(KEY, body(max_tokens=1_000))
        assert_standard_error(r, "rate_limit_exceeded", 429)
        assert 1 <= int(r.headers["retry-after"]) <= 60
        assert e.primary.state.calls == 1
        assert (await e.router.complete("other-tenant", body(max_tokens=1_000))).status == 200
    finally:
        await e.close()


async def test_request_bigger_than_budget_is_413(env: Env) -> None:
    r = await env.router.complete(KEY, body(max_tokens=60_000))
    assert_standard_error(r, "request_too_large", 413)


async def test_failover_charges_budget_once(env: Env) -> None:
    env.primary.state.mode = "429"
    env.secondary.state.usage = 300
    await env.router.complete(KEY, body())
    assert env.limiter.usage(KEY) == 300


async def test_cancelled_request_releases_reservation(env: Env) -> None:
    env.primary.state.mode = "hang"
    env.primary.state.delay = 5.0
    task = asyncio.create_task(env.router.complete(KEY, body(max_tokens=10_000)))
    await asyncio.sleep(0.1)
    assert env.limiter.usage(KEY) > 10_000  # reserved while in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert env.limiter.usage(KEY) == 0


async def test_concurrent_requests_admitted_exactly(tmp_path: Path) -> None:
    """20 concurrent requests, each estimated at ~1,004 tokens, against a
    10k budget while the primary is slow: exactly 9 admitted."""
    e = Env(tmp_path, primary_timeout=0.2, limit=10_000)
    try:
        e.primary.state.mode = "hang"
        e.primary.state.delay = 0.5
        e.secondary.state.usage = 1_000
        results = await asyncio.gather(*(e.router.complete(KEY, body(max_tokens=1_000)) for _ in range(20)))
        statuses = sorted(r.status for r in results)
        assert statuses.count(200) == 9 and statuses.count(429) == 11, statuses
        assert all(r.headers["x-gateway-route"] == "fallback" for r in results if r.status == 200)
        assert e.limiter.usage(KEY) == 9_000
    finally:
        await e.close()


@pytest.mark.parametrize(
    "bad",
    [[], {"messages": []}, {"messages": "hi"}, {"messages": [{"role": "user"}], "max_tokens": 0},
     {"messages": [{"role": "user"}], "max_tokens": True}, {"messages": [{"role": "user"}], "stream": "yes"},
     {"messages": [{"role": "user"}], "stream": True, "stream_options": "usage"}],
)
async def test_invalid_requests(env: Env, bad: Any) -> None:
    r = await env.router.complete(KEY, bad)
    assert_standard_error(r, "invalid_request", 400)
    assert env.primary.state.calls == 0


# --- HTTP layer ------------------------------------------------------------------


async def test_http_auth_and_sanitized_internal_error(env: Env) -> None:
    app = create_app(env.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        r = await c.post("/v1/chat/completions", json=body())
        assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
        r = await c.post("/v1/chat/completions", json=body(), headers={"authorization": f"Bearer {KEY}"})
        assert r.status_code == 200 and r.headers["x-request-id"]
        r = await c.post("/v1/chat/completions", content=b"{bad", headers={"authorization": f"Bearer {KEY}"})
        assert r.status_code == 400

        async def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("db exploded at /var/lib/secret.db")

        env.router.complete = boom  # type: ignore[method-assign]
        r = await c.post("/v1/chat/completions", json=body(), headers={"authorization": f"Bearer {KEY}"})
        assert r.status_code == 500
        assert r.json()["error"]["type"] == "internal_error"
        assert "exploded" not in r.text and "secret.db" not in r.text


# --- real sockets: the spec's 3000ms deadline, and headers-then-stall ------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


@pytest.mark.parametrize("mode", ["hang", "slow_body"])
async def test_default_3000ms_deadline_over_real_http(tmp_path: Path, mode: str) -> None:
    """Default timeout (3.0s, the spec value). `slow_body` sends status and
    headers immediately and then stalls: httpx's per-phase timeouts alone
    wouldn't catch that; the whole-call deadline must."""
    primary, secondary = create_mock_provider("primary", mode), create_mock_provider("secondary")
    primary.state.delay = 6.0
    pp, sp = _free_port(), _free_port()
    servers = [await _serve(primary, pp), await _serve(secondary, sp)]
    limiter = SqliteTokenLimiter(tmp_path / "r.db")
    client = httpx.AsyncClient(timeout=30.0)
    router = ModelRouter(limiter, Provider("primary", f"http://127.0.0.1:{pp}/v1"),
                         Provider("secondary", f"http://127.0.0.1:{sp}/v1"), client)
    try:
        t0 = time.monotonic()
        r = await router.complete(KEY, body())
        elapsed = time.monotonic() - t0
        assert r.status == 200 and r.headers["x-gateway-route"] == "fallback"
        assert 2.95 <= elapsed < 3.6, elapsed
    finally:
        await client.aclose()
        limiter.close()
        for server, task in servers:
            server.should_exit = True
            server.force_exit = True
            await task


# --- streaming ----------------------------------------------------------------------

from llm_router.router import RouterStream, estimate_tokens  # noqa: E402


async def collect(r: Any) -> tuple[list[Any], str]:
    """Drain a RouterStream; return (parsed events, raw text)."""
    assert isinstance(r, RouterStream), r
    raw = b"".join([chunk async for chunk in r.body]).decode()
    events = []
    for block in raw.split("\n\n"):
        if block.startswith("data: "):
            data = block[6:]
            events.append(data if data == "[DONE]" else json.loads(data))
    return events, raw


def text_of(events: list[Any]) -> str:
    return "".join(c["delta"].get("content") or "" for e in events if isinstance(e, dict)
                   for c in e.get("choices", []))


async def test_stream_from_primary_charges_real_usage(env: Env) -> None:
    env.primary.state.usage = 77
    r = await env.router.complete(KEY, body(stream=True))
    assert r.headers["x-gateway-route"] == "primary"
    events, _ = await collect(r)
    assert text_of(events) == "hello from primary" and events[-1] == "[DONE]"
    # The router asked for usage, the client didn't: the usage-only chunk is removed.
    assert env.primary.state.last_body["stream_options"] == {"include_usage": True}
    assert not any(isinstance(e, dict) and "usage" in e for e in events)
    assert all(e["choices"] for e in events if isinstance(e, dict))  # no empty usage-only chunk either
    assert env.limiter.usage(KEY) == 77
    assert env.secondary.state.calls == 0


async def test_stream_usage_forwarded_when_client_asks(env: Env) -> None:
    events, _ = await collect(await env.router.complete(
        KEY, body(stream=True, stream_options={"include_usage": True})))
    usage_chunks = [e for e in events if isinstance(e, dict) and e.get("usage")]
    assert len(usage_chunks) == 1 and usage_chunks[0]["choices"] == []


@pytest.mark.parametrize("mode", ["429", "500", "hang", "stall_first", "garbage"])
async def test_stream_fails_over_before_first_byte(env: Env, mode: str) -> None:
    env.primary.state.mode = mode
    t0 = time.monotonic()
    r = await env.router.complete(KEY, body(stream=True))
    assert r.headers["x-gateway-route"] == "fallback", mode
    events, _ = await collect(r)
    assert text_of(events) == "hello from secondary"
    assert time.monotonic() - t0 < 2.0  # primary deadline is 0.3s in this fixture
    assert env.secondary.state.last_body["model"] == "backup-model"


async def test_stream_client_error_does_not_fail_over(env: Env) -> None:
    env.primary.state.mode = "400"
    r = await env.router.complete(KEY, body(stream=True))
    assert_standard_error(r, "invalid_request", 400)
    assert env.secondary.state.calls == 0 and env.limiter.usage(KEY) == 0


async def test_stream_both_down_releases_budget(env: Env) -> None:
    env.primary.state.mode = env.secondary.state.mode = "500"
    r = await env.router.complete(KEY, body(stream=True))
    assert_standard_error(r, "upstream_unavailable", 503)
    assert env.limiter.usage(KEY) == 0


async def test_stream_break_after_first_byte_ends_with_error_no_failover(env: Env) -> None:
    env.primary.state.mode = "break"
    r = await env.router.complete(KEY, body(stream=True))
    events, raw = await collect(r)
    assert text_of(events) == "hello "  # what was already relayed stays relayed
    err = events[-1]["error"]
    assert err["type"] == "upstream_error" and set(err) == {"type", "message", "request_id"}
    for marker in LEAK_MARKERS:
        assert marker not in raw
    assert env.secondary.state.calls == 0  # can't switch provider mid-answer
    assert env.limiter.usage(KEY) == estimate_tokens(body(stream=True), 1024)  # no usage reported: estimate


async def test_stream_client_disconnect_commits_estimate(env: Env) -> None:
    r = await env.router.complete(KEY, body(stream=True, max_tokens=500))
    first = await anext(r.body)
    assert first.startswith(b"data: ")
    await r.body.aclose()  # client went away mid-stream
    assert env.limiter.usage(KEY) == estimate_tokens(body(max_tokens=500), 1024)


async def test_stream_rate_limit_applies_before_streaming(tmp_path: Path) -> None:
    e = Env(tmp_path, limit=300)
    try:
        e.primary.state.usage = 250
        events, _ = await collect(await e.router.complete(KEY, body(stream=True, max_tokens=100)))
        assert events[-1] == "[DONE]"
        r = await e.router.complete(KEY, body(stream=True, max_tokens=100))
        assert_standard_error(r, "rate_limit_exceeded", 429)
        assert e.primary.state.calls == 1
    finally:
        await e.close()


async def test_stream_events_relayed_as_they_arrive_over_real_http(tmp_path: Path) -> None:
    """Real sockets end to end: provider pauses 0.6s between content events;
    the first content must reach the client long before the stream ends."""
    primary = create_mock_provider("primary", "slow_stream")
    primary.state.delay = 0.6
    secondary = create_mock_provider("secondary")
    ports = [_free_port() for _ in range(3)]
    limiter = SqliteTokenLimiter(tmp_path / "r.db", limit=50_000)
    client = httpx.AsyncClient(timeout=10)
    router = ModelRouter(limiter, Provider("primary", f"http://127.0.0.1:{ports[0]}/v1"),
                         Provider("secondary", f"http://127.0.0.1:{ports[1]}/v1"), client)
    servers = [await _serve(primary, ports[0]), await _serve(secondary, ports[1]),
               await _serve(create_app(router), ports[2])]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            t0 = time.monotonic()
            arrivals = []
            async with c.stream("POST", f"http://127.0.0.1:{ports[2]}/v1/chat/completions",
                                headers={"authorization": f"Bearer {KEY}"}, json=body(stream=True)) as r:
                assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
                assert r.headers["x-gateway-route"] == "primary"
                async for line in r.aiter_lines():
                    if line.startswith("data: {") and json.loads(line[6:])["choices"]:
                        piece = json.loads(line[6:])["choices"][0]["delta"].get("content")
                        if piece:
                            arrivals.append((piece, time.monotonic() - t0))
        assert [p for p, _ in arrivals] == ["hello ", "from ", "primary"]
        assert arrivals[0][1] < 1.0 and arrivals[-1][1] > 1.5, arrivals  # incremental, not buffered
    finally:
        for server, task in servers:
            server.should_exit = True
            await task
        await client.aclose()
        limiter.close()


async def test_stream_headers_then_stall_fails_over_over_real_http(tmp_path: Path) -> None:
    """The deadline must cover the first EVENT, not just the response
    headers: this primary sends 200 + headers at once, then nothing for 5s.
    (In-process transports buffer whole responses, so only real sockets
    can show the difference.)"""
    primary = create_mock_provider("primary", "stall_first")
    secondary = create_mock_provider("secondary")
    ports = [_free_port(), _free_port()]
    limiter = SqliteTokenLimiter(tmp_path / "r.db", limit=50_000)
    client = httpx.AsyncClient(timeout=10)
    router = ModelRouter(limiter, Provider("primary", f"http://127.0.0.1:{ports[0]}/v1"),
                         Provider("secondary", f"http://127.0.0.1:{ports[1]}/v1"), client, primary_timeout=0.5)
    servers = [await _serve(primary, ports[0]), await _serve(secondary, ports[1])]
    try:
        t0 = time.monotonic()
        r = await router.complete(KEY, body(stream=True))
        events, _ = await collect(r)
        assert r.headers["x-gateway-route"] == "fallback"
        assert text_of(events) == "hello from secondary"
        assert time.monotonic() - t0 < 2.0
    finally:
        for server, task in servers:
            server.should_exit = True
            await task
        await client.aclose()
        limiter.close()


# --- token estimate -------------------------------------------------------------------

from llm_router.tokens import count_chars, make_counter  # noqa: E402

try:
    _TK_NAME, _TK = make_counter("tiktoken")
except Exception:  # not installed / vocabulary not downloadable here
    _TK = None


@pytest.mark.skipif(_TK is None, reason="tiktoken vocabulary unavailable")
def test_tiktoken_counts_where_chars_div_4_underestimates() -> None:
    cjk, js = "你好世界" * 50, '{"a": [1,2,3]}' * 20
    assert _TK(cjk) == 100 and count_chars(cjk) == 50
    assert _TK(js) == 200 and count_chars(js) == 70
    assert _TK("<|endoftext|>") > 0  # special-token text in user input is just text


@pytest.mark.skipif(_TK is None, reason="tiktoken vocabulary unavailable")
def test_estimate_uses_configured_counter_and_counts_tools() -> None:
    b = {"messages": [{"role": "user", "content": "你好世界" * 50}], "max_tokens": 10}
    assert estimate_tokens(b, 1024, _TK) == 100 + 4 + 10
    assert estimate_tokens(b, 1024) == 50 + 4 + 10  # chars/4 default unchanged
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    assert estimate_tokens({**b, "tools": tools}, 1024, _TK) > estimate_tokens(b, 1024, _TK)


def test_auto_counter_falls_back_when_tiktoken_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_tiktoken(name: str, *a: Any, **kw: Any) -> Any:
        if name == "tiktoken":
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_tiktoken)
    name, counter = make_counter("auto")
    assert name == "chars/4" and counter("abcdefgh") == 2
    with pytest.raises(ImportError):
        make_counter("tiktoken")  # explicitly requested: fail loudly, don't silently degrade
    with pytest.raises(ValueError):
        make_counter("bogus")


@pytest.mark.skipif(_TK is None, reason="tiktoken vocabulary unavailable")
async def test_router_reserves_with_tiktoken(tmp_path: Path) -> None:
    """A CJK prompt of 160 real tokens: chars/4 reserves 85 and admits it
    under a 150-token budget; with tiktoken (165) it is refused up front."""
    b = {"messages": [{"role": "user", "content": "你好世界" * 80}], "max_tokens": 1}
    assert _TK("你好世界" * 80) == 160
    for counter, expected in [(count_chars, 200), (_TK, 413)]:
        sub = tmp_path / str(expected)
        sub.mkdir()
        e = Env(sub, limit=150)
        try:
            e.router.count_tokens = counter
            r = await e.router.complete(KEY, b)
            assert r.status == expected, (counter, r.body)
        finally:
            await e.close()


# --- tenant allowlist ---------------------------------------------------------------


async def test_unknown_tenant_key_rejected_before_limiter(env: Env) -> None:
    app = create_app(env.router, api_keys=["tenant-key-1", "tenant-key-2"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        for key in ["minted-key-123", "tenant-key-1x", "TENANT-KEY-1"]:
            r = await c.post("/v1/chat/completions", headers={"authorization": f"Bearer {key}"}, json=body())
            assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
            assert env.limiter.usage(key) == 0  # never reached the limiter
        for key in ["tenant-key-1", "tenant-key-2"]:
            r = await c.post("/v1/chat/completions", headers={"authorization": f"Bearer {key}"}, json=body())
            assert r.status_code == 200
    assert env.primary.state.calls == 2
