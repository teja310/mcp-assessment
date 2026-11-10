"""Real-client smoke test.

    official MCP SDK client  --Streamable HTTP-->  gateway  -->  official MCP SDK server

Nothing hand-written on either end: the client is `mcp.client` (with its own
session handshake, SSE parsing, Mcp-Session-Id handling and DELETE on
close), and the downstream is a `mcp.server.lowlevel.Server` served through
`streamable_http_app()`. All three run in uvicorn on real ports.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import mcp_types as types
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError

from mcp_gateway.app import create_app
from mcp_gateway.auth import TokenStore

ADMIN, VIEWER = "admin-token-123", "viewer-token-456"


def build_downstream(tool_calls: list[str]) -> Server[None]:
    async def list_tools(ctx: object, params: object) -> types.ListToolsResult:
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        return types.ListToolsResult(tools=[
            types.Tool(name="get_weather", description="weather", inputSchema=schema),
            types.Tool(name="admin_reset_key", description="admin only", inputSchema=schema),
        ])

    async def call_tool(ctx: object, params: types.CallToolRequestParams) -> types.CallToolResult:
        tool_calls.append(params.name)
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps({"ran": params.name}))])

    return Server[None](name="real-downstream", version="1.0", on_list_tools=list_tools, on_call_tool=call_tool)


class RecordHTTP:
    """ASGI wrapper recording every HTTP request the downstream receives."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.requests: list[tuple[str, bool]] = []  # (method, had Authorization header)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            self.requests.append((scope["method"], b"authorization" in headers))
        await self.app(scope, receive, send)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve(app: Any, port: int) -> tuple[uvicorn.Server, asyncio.Task]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started
    return server, task


@pytest.fixture
async def stack() -> AsyncIterator[dict[str, Any]]:
    tool_calls: list[str] = []
    downstream = RecordHTTP(build_downstream(tool_calls).streamable_http_app(host="127.0.0.1"))
    d_port, g_port = _free_port(), _free_port()
    servers = [await _serve(downstream, d_port)]
    servers.append(await _serve(
        create_app(f"http://127.0.0.1:{d_port}/mcp", TokenStore({ADMIN: "admin", VIEWER: "viewer"})), g_port))
    try:
        yield {"url": f"http://127.0.0.1:{g_port}/mcp", "tool_calls": tool_calls, "downstream": downstream}
    finally:
        for server, task in reversed(servers):
            server.should_exit = True
            await task


async def _session(url: str, token: str):
    """Yield an initialized official-SDK ClientSession through the gateway."""
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
    async with http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def test_viewer_via_official_client(stack: dict[str, Any]) -> None:
    async for s in _session(stack["url"], VIEWER):
        tools = await s.list_tools()  # forwarded transparently
        assert {t.name for t in tools.tools} == {"get_weather", "admin_reset_key"}

        ok = await s.call_tool("get_weather", {"x": "Paris"})
        assert json.loads(ok.content[0].text) == {"ran": "get_weather"}

        with pytest.raises(MCPError) as denied:
            await s.call_tool("admin_reset_key", {"x": "k1"})
        assert denied.value.code == -32001 and "Unauthorized Tool Call" in str(denied.value)

        # Session still usable after a denial.
        assert (await s.call_tool("get_weather", {"x": "Oslo"})).is_error is not True

    assert stack["tool_calls"] == ["get_weather", "get_weather"], "admin call must never reach downstream"
    methods = [m for m, _ in stack["downstream"].requests]
    assert "POST" in methods and "DELETE" in methods  # SDK client's session teardown went through the gateway
    assert not any(had_auth for _, had_auth in stack["downstream"].requests), "client token leaked downstream"


async def test_admin_via_official_client(stack: dict[str, Any]) -> None:
    async for s in _session(stack["url"], ADMIN):
        res = await s.call_tool("admin_reset_key", {"x": "k1"})
        assert json.loads(res.content[0].text) == {"ran": "admin_reset_key"}
    assert stack["tool_calls"] == ["admin_reset_key"]


async def test_bad_token_rejected_before_downstream(stack: dict[str, Any]) -> None:
    with pytest.raises(BaseException) as exc_info:
        async for _ in _session(stack["url"], "not-a-token"):
            pass
    err = exc_info.value
    while isinstance(err, BaseExceptionGroup):  # unwrap the SDK's task-group wrapping
        err = err.exceptions[0]
    assert isinstance(err, MCPError) and err.code == -32003 and "missing or invalid bearer token" in str(err)
    assert stack["downstream"].requests == []


# --- signed JWTs + tools/list filtering, with the official client and server -------

import time as _time  # noqa: E402

import jwt as _jwt  # noqa: E402

from mcp_gateway.auth import JwtVerifier, MultiAuthenticator  # noqa: E402

_SECRET = "k" * 40


def _jwt_for(role: str) -> str:
    now = int(_time.time())
    return _jwt.encode({"sub": f"{role}-user", "role": role, "aud": "mcp-gateway", "iat": now, "exp": now + 300},
                       _SECRET, algorithm="HS256")


@pytest.fixture
async def jwt_stack() -> AsyncIterator[dict[str, Any]]:
    tool_calls: list[str] = []
    downstream = RecordHTTP(build_downstream(tool_calls).streamable_http_app(host="127.0.0.1"))
    d_port, g_port = _free_port(), _free_port()
    auth = MultiAuthenticator([TokenStore({VIEWER: "viewer"}), JwtVerifier(secret=_SECRET, audience="mcp-gateway")])
    servers = [await _serve(downstream, d_port)]
    servers.append(await _serve(create_app(f"http://127.0.0.1:{d_port}/mcp", auth, filter_tools_list=True), g_port))
    try:
        yield {"url": f"http://127.0.0.1:{g_port}/mcp", "tool_calls": tool_calls}
    finally:
        for server, task in reversed(servers):
            server.should_exit = True
            await task


async def test_official_client_with_jwt_and_filtered_tool_list(jwt_stack: dict[str, Any]) -> None:
    async for s in _session(jwt_stack["url"], _jwt_for("viewer")):
        assert {t.name for t in (await s.list_tools()).tools} == {"get_weather"}  # admin tool hidden
        with pytest.raises(MCPError) as denied:  # and still denied if called by name
            await s.call_tool("admin_reset_key", {"x": "k1"})
        assert denied.value.code == -32001
    async for s in _session(jwt_stack["url"], _jwt_for("admin")):
        assert {t.name for t in (await s.list_tools()).tools} == {"get_weather", "admin_reset_key"}
        res = await s.call_tool("admin_reset_key", {"x": "k1"})
        assert json.loads(res.content[0].text) == {"ran": "admin_reset_key"}
    async for s in _session(jwt_stack["url"], VIEWER):  # static tokens still work alongside JWTs
        assert {t.name for t in (await s.list_tools()).tools} == {"get_weather"}
    assert jwt_stack["tool_calls"] == ["admin_reset_key"]
