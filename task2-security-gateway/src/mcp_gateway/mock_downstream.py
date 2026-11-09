"""Mock downstream MCP server (HTTP JSON-RPC) for demos and tests.

Exposes one ordinary tool and one admin tool, and records every message it
receives (plus whether an Authorization header reached it) so tests can
assert the gateway did or did not forward something.

Run standalone:  python -m mcp_gateway.mock_downstream  (port 9000)
"""

from __future__ import annotations

import json
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the weather for a city.",
        "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
    {
        "name": "admin_reset_key",
        "description": "Rotate an API key. Admin only.",
        "inputSchema": {"type": "object", "properties": {"key_id": {"type": "string"}}, "required": ["key_id"]},
    },
]


def create_mock_app() -> Starlette:
    received: list[dict[str, Any]] = []

    async def mcp(request: Request) -> Response:
        if request.method in ("GET", "DELETE"):
            received.append({
                "http_method": request.method,
                "message": {},
                "authorization": request.headers.get("authorization"),
                "mcp-session-id": request.headers.get("mcp-session-id"),
            })
            if request.method == "DELETE":
                return Response(status_code=204)
            # GET opens the server->client SSE stream (Streamable HTTP).
            sse = 'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n\n'
            return Response(sse, media_type="text/event-stream", headers={"cache-control": "no-cache"})
        msg = json.loads(await request.body())
        received.append({"http_method": "POST", "message": msg, "authorization": request.headers.get("authorization")})
        if "id" not in msg:
            return Response(status_code=202)
        rid, method = msg["id"], msg.get("method")
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-downstream", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = msg["params"]["name"]
            if name not in {t["name"] for t in TOOLS}:
                return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
            text = json.dumps({"tool": name, "arguments": msg["params"].get("arguments", {}), "ok": True})
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers={"mcp-session-id": "mock-session"})

    app = Starlette(routes=[Route("/mcp", mcp, methods=["POST", "GET", "DELETE"])])
    app.state.received = received
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_mock_app(), host="127.0.0.1", port=9000, log_level="warning")
