from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from mcp_gateway.app import create_app
from mcp_gateway.auth import TokenStore, is_admin_tool
from mcp_gateway.mock_downstream import create_mock_app

ADMIN = "admin-token-123"
VIEWER = "viewer-token-456"
TOKENS = {ADMIN: "admin", VIEWER: "viewer"}


class Harness:
    def __init__(self, client: httpx.AsyncClient, downstream_received: list[dict[str, Any]]) -> None:
        self.client = client
        self.received = downstream_received

    async def post(self, payload: Any, token: str | None = VIEWER, raw: bytes | None = None) -> httpx.Response:
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        content = raw if raw is not None else json.dumps(payload).encode()
        return await self.client.post("/mcp", content=content, headers=headers)

    def downstream_methods(self) -> list[str | None]:
        return [r["message"].get("method") for r in self.received]


@pytest.fixture
async def gw() -> AsyncIterator[Harness]:
    mock = create_mock_app()
    downstream_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock))
    app = create_app("http://downstream/mcp", TokenStore(TOKENS), client=downstream_client)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as c:
            yield Harness(c, mock.state.received)
    await downstream_client.aclose()


def call(name: Any, rid: Any = 1, **extra: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": {}}, **extra}


# --- authentication ---------------------------------------------------------


@pytest.mark.parametrize("token", [None, "wrong-token", ""])
async def test_missing_or_invalid_token_is_401(gw: Harness, token: str | None) -> None:
    r = await gw.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=token)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.json()["error"]["code"] == -32001
    assert gw.received == []


async def test_non_bearer_scheme_rejected(gw: Harness) -> None:
    r = await gw.client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"authorization": f"Basic {ADMIN}"}
    )
    assert r.status_code == 401
    assert gw.received == []


# --- tools/list: transparent forwarding --------------------------------------


@pytest.mark.parametrize("token", [ADMIN, VIEWER])
async def test_tools_list_forwarded_transparently(gw: Harness, token: str) -> None:
    r = await gw.post({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, token=token)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 7
    assert {t["name"] for t in body["result"]["tools"]} == {"get_weather", "admin_reset_key"}
    assert r.headers["mcp-session-id"] == "mock-session"
    assert gw.downstream_methods() == ["tools/list"]


async def test_client_token_is_not_forwarded_downstream(gw: Harness) -> None:
    await gw.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=ADMIN)
    assert gw.received[0]["authorization"] is None


# --- tools/call authorization --------------------------------------------------


async def test_viewer_can_call_regular_tool(gw: Harness) -> None:
    r = await gw.post(call("get_weather"), token=VIEWER)
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is False
    assert gw.downstream_methods() == ["tools/call"]


async def test_admin_can_call_admin_tool(gw: Harness) -> None:
    r = await gw.post(call("admin_reset_key"), token=ADMIN)
    assert r.status_code == 200
    assert "result" in r.json()
    assert gw.downstream_methods() == ["tools/call"]


async def test_viewer_calling_admin_tool_is_intercepted(gw: Harness) -> None:
    r = await gw.post(call("admin_reset_key", rid="req-42"), token=VIEWER)
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "jsonrpc": "2.0",
        "id": "req-42",
        "error": {"code": -32001, "message": "Unauthorized Tool Call",
                  "data": {"tool": "admin_reset_key", "required_role": "admin"}},
    }
    assert gw.received == [], "downstream must not be contacted for a denied call"


@pytest.mark.parametrize(
    "name",
    ["ADMIN_reset_key", "Admin_reset_key", " admin_reset_key", "admin_reset_key\t", "ａｄｍｉｎ_reset_key"],
)
async def test_admin_prefix_evasion_variants_are_denied(gw: Harness, name: str) -> None:
    r = await gw.post(call(name), token=VIEWER)
    assert r.json()["error"]["code"] == -32001
    assert gw.received == []


async def test_duplicate_name_key_parser_differential_is_rejected(gw: Harness) -> None:
    raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_weather","name":"admin_reset_key"}}'
    r = await gw.post(None, token=VIEWER, raw=raw)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700
    assert gw.received == []


async def test_batch_smuggling_is_rejected(gw: Harness) -> None:
    r = await gw.post([call("get_weather", 1), call("admin_reset_key", 2)], token=VIEWER)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32600
    assert gw.received == []


async def test_denied_tool_call_notification_is_dropped_silently(gw: Harness) -> None:
    note = call("admin_reset_key")
    del note["id"]
    r = await gw.post(note, token=VIEWER)
    assert r.status_code == 202
    assert r.content == b""
    assert gw.received == []


@pytest.mark.parametrize("params", [{}, {"name": 123}, {"name": ""}, ["admin_reset_key"]])
async def test_tools_call_with_bad_name_is_invalid_params(gw: Harness, params: Any) -> None:
    r = await gw.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": params}, token=ADMIN)
    assert r.status_code == 400
    assert r.json() == {"jsonrpc": "2.0", "id": 3,
                        "error": {"code": -32602, "message": 'Invalid params: "params.name" must be a non-empty string'}}
    assert gw.received == []


# --- wire-format validation ------------------------------------------------------


@pytest.mark.parametrize(
    "raw, code, rid",
    [
        (b"{not json", -32700, None),
        (b"\xff\xfe", -32700, None),
        (b'"just a string"', -32600, None),
        (b'{"id":5,"method":"tools/list"}', -32600, 5),
        (b'{"jsonrpc":"1.0","id":5,"method":"tools/list"}', -32600, 5),
        (b'{"jsonrpc":"2.0","id":true,"method":"tools/list"}', -32600, None),
        (b'{"jsonrpc":"2.0","id":5,"method":42}', -32600, 5),
        (b'{"jsonrpc":"2.0","id":5,"method":"tools/call","params":"x"}', -32600, 5),
        (b'{"jsonrpc":"2.0","id":5}', -32600, 5),
    ],
)
async def test_malformed_jsonrpc_rejected(gw: Harness, raw: bytes, code: int, rid: Any) -> None:
    r = await gw.post(None, token=ADMIN, raw=raw)
    assert r.status_code == 400
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == rid and body["error"]["code"] == code
    assert gw.received == []


async def test_oversized_body_rejected(gw: Harness) -> None:
    r = await gw.post(None, token=ADMIN, raw=b'{"x":"' + b"a" * (1024 * 1024 + 10) + b'"}')
    assert r.status_code == 413
    assert gw.received == []


async def test_other_methods_and_notifications_forwarded(gw: Harness) -> None:
    r = await gw.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, token=VIEWER)
    assert r.json()["result"]["serverInfo"]["name"] == "mock-downstream"
    r = await gw.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token=VIEWER)
    assert r.status_code == 202
    assert gw.downstream_methods() == ["initialize", "notifications/initialized"]


async def test_get_sse_stream_forwarded_with_session_and_without_token(gw: Harness) -> None:
    r = await gw.client.get("/mcp", headers={"authorization": f"Bearer {VIEWER}", "accept": "text/event-stream",
                                             "mcp-session-id": "sess-123"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert "notifications/tools/list_changed" in r.text
    assert gw.received == [{"http_method": "GET", "message": {}, "authorization": None,
                            "mcp-session-id": "sess-123"}]


async def test_delete_session_forwarded(gw: Harness) -> None:
    r = await gw.client.delete("/mcp", headers={"authorization": f"Bearer {ADMIN}", "mcp-session-id": "sess-9"})
    assert r.status_code == 204
    assert gw.received[0]["http_method"] == "DELETE" and gw.received[0]["mcp-session-id"] == "sess-9"
    assert gw.received[0]["authorization"] is None


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_get_and_delete_require_auth(gw: Harness, method: str) -> None:
    r = await gw.client.request(method, "/mcp", headers={"mcp-session-id": "sess-1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == -32001
    assert gw.received == []


async def test_unsupported_http_method_rejected(gw: Harness) -> None:
    r = await gw.client.put("/mcp", headers={"authorization": f"Bearer {ADMIN}"}, content=b"{}")
    assert r.status_code == 405
    assert gw.received == []


async def test_downstream_jsonrpc_errors_pass_through(gw: Harness) -> None:
    r = await gw.post(call("get_nonexistent"), token=VIEWER)
    assert r.json()["error"]["code"] == -32602


async def test_downstream_unreachable_is_sanitized_502() -> None:
    app = create_app("http://127.0.0.1:1/mcp", TokenStore(TOKENS))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
                             headers={"authorization": f"Bearer {VIEWER}"})
    assert r.status_code == 502
    assert r.json() == {"jsonrpc": "2.0", "id": 9,
                        "error": {"code": -32002, "message": "Downstream MCP server unavailable"}}


def test_policy_unit() -> None:
    assert is_admin_tool("admin_x") and is_admin_tool("ADMIN_x")
    assert not is_admin_tool("get_admin_status") and not is_admin_tool("administrator")


def test_empty_token_store_refuses_to_start() -> None:
    with pytest.raises(ValueError):
        TokenStore({})


# --- real sockets, real processes ----------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"port {port} never opened")


def test_end_to_end_over_real_http() -> None:
    down_port, gw_port = _free_port(), _free_port()
    env = {
        **os.environ,
        "GATEWAY_DOWNSTREAM_URL": f"http://127.0.0.1:{down_port}/mcp",
        "GATEWAY_TOKENS": json.dumps(TOKENS),
        "GATEWAY_PORT": str(gw_port),
    }
    mock_cmd = [sys.executable, "-c",
                f"import uvicorn; from mcp_gateway.mock_downstream import create_mock_app; "
                f"uvicorn.run(create_mock_app(), host='127.0.0.1', port={down_port}, log_level='warning')"]
    procs = [
        subprocess.Popen(mock_cmd, stderr=subprocess.DEVNULL),
        subprocess.Popen([sys.executable, "-m", "mcp_gateway"], env=env, stderr=subprocess.DEVNULL),
    ]
    try:
        _wait_for_port(down_port)
        _wait_for_port(gw_port)
        url = f"http://127.0.0.1:{gw_port}/mcp"
        with httpx.Client() as c:
            r = c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"authorization": f"Bearer {VIEWER}"})
            assert len(r.json()["result"]["tools"]) == 2
            r = c.post(url, json=call("admin_reset_key"), headers={"authorization": f"Bearer {VIEWER}"})
            assert r.json()["error"]["code"] == -32001
            r = c.post(url, json=call("admin_reset_key"), headers={"authorization": f"Bearer {ADMIN}"})
            assert "result" in r.json()
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=5)
