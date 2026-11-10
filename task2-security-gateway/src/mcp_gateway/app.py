"""The gateway ASGI app.

Request flow for POST /mcp:

    authenticate bearer token  -> 401 + JSON-RPC -32003 Unauthenticated
                                   (opaque token table and/or signed JWT)
    read body (size-capped)     -> 413
    strict JSON-RPC parse       -> -32700 / -32600
    policy:
      tools/list                -> forward transparently (default), or, with
                                   filter_tools_list, remove admin_* tools from
                                   the result for non-admins
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

import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from mcp_gateway import jsonrpc
from mcp_gateway.auth import ADMIN_ROLE, Authenticator, is_admin_tool, may_call_tool

logger = logging.getLogger("mcp_gateway")

MAX_BODY_BYTES = 1 * 1024 * 1024
# A tools/list response is buffered to be filtered; past this, fail closed.
MAX_TOOLS_LIST_BYTES = 4 * 1024 * 1024

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


def _filter_tools_result(msg: Any) -> int:
    """Remove admin_* tools from a JSON-RPC tools/list result in place.
    Returns how many were removed. Uses the same normalized prefix check as
    the call policy, so the list and the policy can't disagree."""
    result = msg.get("result") if isinstance(msg, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return 0
    kept = [t for t in tools if not (isinstance(t, dict) and isinstance(t.get("name"), str) and is_admin_tool(t["name"]))]
    result["tools"] = kept
    return len(tools) - len(kept)


_SSE_EVENT_SPLIT = re.compile(r"\r\n\r\n|\n\n|\r\r")


def _filter_sse(text: str) -> tuple[str, int]:
    """Filter every JSON-RPC message carried in an SSE body. Non-data lines
    (event:, id:, retry:, comments) are kept as they are."""
    out, removed = [], 0
    for event in _SSE_EVENT_SPLIT.split(text):
        if not event.strip():
            continue
        lines = event.splitlines()
        data = "\n".join(line[5:].removeprefix(" ") for line in lines if line.startswith("data:"))
        try:
            msg = json.loads(data) if data else None
        except ValueError:
            msg = None
        n = _filter_tools_result(msg) if msg is not None else 0
        if n:
            removed += n
            lines = [line for line in lines if not line.startswith("data:")] + ["data: " + json.dumps(msg)]
        out.append("\n".join(lines))
    return "".join(e + "\n\n" for e in out), removed


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
    tokens: Authenticator,
    client: httpx.AsyncClient | None = None,
    *,
    filter_tools_list: bool = False,
) -> Starlette:
    """Build the gateway. `tokens` is any Authenticator (TokenStore,
    JwtVerifier, MultiAuthenticator). Pass `client` to inject a transport
    (tests)."""
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

    async def forward_filtered(request: Request, body: bytes, req_id: Any, principal: Any) -> Response:
        """Forward tools/list, then drop admin_* tools from the result."""
        http: httpx.AsyncClient = request.app.state.client
        headers = {k: v for k in FORWARD_REQUEST_HEADERS if (v := request.headers.get(k)) is not None}
        try:
            upstream = await http.send(http.build_request("POST", downstream_url, headers=headers, content=body),
                                       stream=True)
        except httpx.HTTPError as exc:
            logger.error("downstream request failed: %s: %s", type(exc).__name__, exc)
            return _rpc_error(502, jsonrpc.DOWNSTREAM_UNAVAILABLE, "Downstream MCP server unavailable", req_id)
        try:
            raw = b""
            async for chunk in upstream.aiter_bytes():
                raw += chunk
                if len(raw) > MAX_TOOLS_LIST_BYTES:
                    logger.error("tools/list response over %d bytes; refusing to pass it unfiltered",
                                 MAX_TOOLS_LIST_BYTES)
                    return _rpc_error(502, jsonrpc.INTERNAL_ERROR, "tools/list response too large", req_id)
        except httpx.HTTPError as exc:
            logger.error("downstream response broke: %s: %s", type(exc).__name__, exc)
            return _rpc_error(502, jsonrpc.DOWNSTREAM_UNAVAILABLE, "Downstream MCP server unavailable", req_id)
        finally:
            await upstream.aclose()
        resp_headers = {k: v for k in FORWARD_RESPONSE_HEADERS if (v := upstream.headers.get(k)) is not None}
        ctype = upstream.headers.get("content-type", "")
        removed = 0
        if upstream.status_code == 200 and ctype.startswith("application/json"):
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = None
            if msg is not None and (removed := _filter_tools_result(msg)):
                raw = json.dumps(msg).encode()
        elif upstream.status_code == 200 and ctype.startswith("text/event-stream"):
            text, removed = _filter_sse(raw.decode("utf-8", "replace"))
            if removed:
                raw = text.encode()
        if removed:
            logger.info("FILTER tools/list removed=%d role=%r token=%s", removed, principal.role,
                        principal.token_fingerprint)
        return Response(raw, status_code=upstream.status_code, headers=resp_headers)

    async def mcp_endpoint(request: Request) -> Response:
        principal = tokens.authenticate(request.headers.get("authorization"))
        if principal is None:
            logger.info("auth denied: missing or invalid bearer token")
            return _rpc_error(
                401,
                jsonrpc.UNAUTHENTICATED,
                "Unauthenticated: missing or invalid bearer token",
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

        if (filter_tools_list and msg.method == "tools/list" and not msg.is_notification
                and principal.role != ADMIN_ROLE):
            return await forward_filtered(request, body, msg.response_id, principal)

        return await forward(request, body, msg.response_id)

    return Starlette(
        routes=[Route("/mcp", mcp_endpoint, methods=["POST", "GET", "DELETE"])],
        lifespan=lifespan,
    )
