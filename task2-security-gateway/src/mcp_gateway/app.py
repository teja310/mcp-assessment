"""The gateway ASGI app.

Request flow for POST /mcp:

    authenticate bearer token  -> 401 + JSON-RPC -32001 if missing/invalid
    read body (size-capped)     -> 413
    strict JSON-RPC parse       -> -32700 / -32600
    policy:
      tools/list                -> forward transparently
      tools/call                -> params.name must be a string (-32602);
                                   admin_* requires role "admin", else
                                   -32001 "Unauthorized Tool Call" returned
                                   directly, downstream never contacted
      anything else             -> forward
    forward raw body bytes, stream the downstream response back

GET and DELETE on /mcp (Streamable HTTP's SSE stream / session teardown)
are authenticated and forwarded; they carry no JSON-RPC payload to inspect.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from mcp_gateway import jsonrpc
from mcp_gateway.auth import TokenStore, may_call_tool

logger = logging.getLogger("mcp_gateway")

MAX_BODY_BYTES = 1 * 1024 * 1024

# Only these headers cross the gateway. In particular the client's
# Authorization header is NOT forwarded: the token is terminated here and
# the downstream server never sees end-user credentials.
FORWARD_REQUEST_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
FORWARD_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "cache-control")


def _rpc_error(
    status: int,
    code: int,
    message: str,
    req_id: Any = None,
    data: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(jsonrpc.error_body(code, message, req_id, data), status_code=status, headers=headers)


async def _read_body_capped(request: Request) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    downstream_url: str,
    tokens: TokenStore,
    client: httpx.AsyncClient | None = None,
) -> Starlette:
    """Build the gateway. Pass `client` to inject a transport (tests)."""
    owns_client = client is None

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)
        )
        try:
            yield
        finally:
            if owns_client:
                await app.state.client.aclose()

    async def forward(request: Request, body: bytes | None, req_id: Any) -> Response:
        http: httpx.AsyncClient = request.app.state.client
        headers = {k: v for k in FORWARD_REQUEST_HEADERS if (v := request.headers.get(k)) is not None}
        upstream_req = http.build_request(request.method, downstream_url, headers=headers, content=body)
        try:
            upstream = await http.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            logger.error("downstream request failed: %s: %s", type(exc).__name__, exc)
            return _rpc_error(502, jsonrpc.DOWNSTREAM_UNAVAILABLE, "Downstream MCP server unavailable", req_id)

        resp_headers = {k: v for k in FORWARD_RESPONSE_HEADERS if (v := upstream.headers.get(k)) is not None}
        # aiter_bytes() decodes any Content-Encoding, so we deliberately do
        # not forward content-encoding / content-length. Streaming keeps SSE
        # responses flowing event-by-event instead of buffering them.
        return StreamingResponse(
            upstream.aiter_bytes(),
            status_code=upstream.status_code,
            headers=resp_headers,
            background=BackgroundTask(upstream.aclose),
        )

    async def mcp_endpoint(request: Request) -> Response:
        principal = tokens.authenticate(request.headers.get("authorization"))
        if principal is None:
            logger.info("auth denied: missing or invalid bearer token")
            return _rpc_error(
                401,
                jsonrpc.UNAUTHORIZED,
                "Unauthorized: missing or invalid bearer token",
                headers={"WWW-Authenticate": 'Bearer realm="mcp-gateway"'},
            )

        if request.method != "POST":
            return await forward(request, None, None)

        body = await _read_body_capped(request)
        if body is None:
            return _rpc_error(413, jsonrpc.INVALID_REQUEST, "Request body too large")

        try:
            msg = jsonrpc.parse_message(body)
        except jsonrpc.JsonRpcError as err:
            return _rpc_error(400, err.code, err.message, err.req_id, err.data)

        if msg.method == "tools/call":
            params = msg.raw.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if not isinstance(name, str) or not name:
                return _rpc_error(
                    400, jsonrpc.INVALID_PARAMS, 'Invalid params: "params.name" must be a non-empty string',
                    msg.response_id,
                )
            if not may_call_tool(principal, name):
                logger.warning(
                    "DENY tools/call name=%r role=%r token=%s", name, principal.role, principal.token_fingerprint
                )
                if msg.is_notification:
                    # JSON-RPC forbids replying to a notification. Drop it.
                    return Response(status_code=202)
                # HTTP 200: this is a JSON-RPC-level error, which MCP clients
                # surface to the agent; a 4xx would look like a transport failure.
                return _rpc_error(
                    200,
                    jsonrpc.UNAUTHORIZED,
                    "Unauthorized Tool Call",
                    msg.response_id,
                    data={"tool": name, "required_role": "admin"},
                )
            logger.info("ALLOW tools/call name=%r role=%r token=%s", name, principal.role, principal.token_fingerprint)

        return await forward(request, body, msg.response_id)

    return Starlette(
        routes=[Route("/mcp", mcp_endpoint, methods=["POST", "GET", "DELETE"])],
        lifespan=lifespan,
    )
