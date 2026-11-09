"""Low-level MCP server: builds the `Server`, wires its two tools, runs it
over stdio.

## The "call_tool decorator" pitfall, and why this file does something
## different

The task this server was built against describes an older `mcp` SDK shape
where `@server.call_tool()` is a decorator that wraps the registered
function in a blanket `try/except`, converting *every* exception --
including a deliberately-raised `McpError` -- into a successful
`CallToolResult(isError=True)`. That would silently turn a real
"invalid params" JSON-RPC error into a fake 200-OK-shaped result, which
breaks the protocol-level contract this server is required to keep.

The installed SDK here is `mcp==2.3.0`. I read
`.venv/lib/python3.12/site-packages/mcp/server/lowlevel/server.py` before
writing a line of this file: **there is no `@server.call_tool()`
decorator in this version at all** (`grep -n "def call_tool"` on that
file returns nothing). The low-level `Server` is instead constructed with
handler callables passed directly to `__init__` -- `on_call_tool=`,
`on_list_tools=`, etc. That already *is* "registering the handler
manually" rather than going through decorator magic, so there is no
decorator to bypass here.

What I verified instead, by reading `mcp/server/runner.py` and
`mcp/shared/jsonrpc_dispatcher.py`:
- The SDK validates only the generic envelope of a `tools/call` request
  (`{name: str, arguments: dict}`) before calling `on_call_tool`. It never
  validates tool-specific argument shape -- that's entirely this module's
  job, done below via the Pydantic models in `schemas.py`.
- Raising `mcp.shared.exceptions.MCPError(code, message, data)` from inside
  `on_call_tool` reaches the wire as a genuine JSON-RPC `error` object with
  that code -- it is NOT downgraded into an `isError` result. Likewise a
  bare `pydantic.ValidationError` is mapped straight to
  `ErrorData(code=INVALID_PARAMS, ...)` by the SDK's own exception mapper.
- Returning (not raising) `CallToolResult(isError=True, ...)` is the
  correct, unmodified channel for business-level misses.

So the equivalent of "bypassing the pitfall" here is simply: never catch
`MCPError` on its way out of the handler, and never translate a validation
failure into a result instead of a raised error. `tests/test_e2e.py`
proves this on the wire (not just by code inspection) by asserting the raw
JSON-RPC reply for invalid input has an `"error"` key with code -32602,
never a `"result"` with `isError`.
"""

from __future__ import annotations

import json
import logging

import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from customer_mcp.logging_setup import log_traceback
from customer_mcp.schemas import GetCustomerRecordInput, TriggerRefundInput
from customer_mcp.stdio_drain import serve_draining
from customer_mcp.store import CustomerNotFoundError, CustomerStore, RefundExceedsBalanceError

logger = logging.getLogger("customer_mcp.server")

GET_CUSTOMER_RECORD = "get_customer_record"
TRIGGER_REFUND = "trigger_refund"

# Unknown-tool-name policy: INVALID_PARAMS (-32602), matching the MCP spec.
# The spec's tools page (2025-06-18, "Error Handling") lists unknown tools
# under protocol errors, and its example is exactly
#   {"code": -32602, "message": "Unknown tool: invalid_tool_name"}.
# The method here is `tools/call`, which does exist; what's wrong is the
# `name` parameter, so -32601 METHOD_NOT_FOUND would misdescribe it.
UNKNOWN_TOOL_ERROR_CODE = types.INVALID_PARAMS


def _text_result(payload: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=payload)])


def _error_result(message: str) -> types.CallToolResult:
    """A *business-level* miss: a valid call that didn't succeed. This is a
    normal successful RPC carrying `isError=True`, not a JSON-RPC error."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)


def build_server(store: CustomerStore) -> Server[None]:
    """Construct the `Server`, injecting `store` as a closure over the two
    handlers below (no global state)."""

    async def handle_list_tools(ctx: object, params: object) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=GET_CUSTOMER_RECORD,
                    description="Look up a customer's record by customer_id.",
                    inputSchema=GetCustomerRecordInput.model_json_schema(),
                ),
                types.Tool(
                    name=TRIGGER_REFUND,
                    description="Trigger a refund for a customer, up to their refundable balance.",
                    inputSchema=TriggerRefundInput.model_json_schema(),
                ),
            ]
        )

    async def handle_call_tool(ctx: object, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        logger.info("tools/call: %s", name)

        try:
            if name == GET_CUSTOMER_RECORD:
                return await _call_get_customer_record(store, arguments)
            if name == TRIGGER_REFUND:
                return await _call_trigger_refund(store, arguments)
            # Unknown tool name: a real JSON-RPC error, not a result.
            raise MCPError(code=UNKNOWN_TOOL_ERROR_CODE, message=f"Unknown tool: {name}")
        except MCPError:
            # Already the right wire shape (INVALID_PARAMS) -- let it
            # propagate untouched. Do NOT catch this into a
            # CallToolResult; that is exactly the pitfall this server avoids.
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all boundary
            # Anything unexpected: log the real traceback to stderr only, and
            # hand the client a generic message with zero internals.
            log_traceback(f"unhandled exception in tool {name!r}", exc)
            raise MCPError(code=types.INTERNAL_ERROR, message="internal error") from exc

    return Server[None](
        "customer-mcp",
        version="0.1.0",
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


async def _call_get_customer_record(store: CustomerStore, arguments: dict[str, object]) -> types.CallToolResult:
    try:
        parsed = GetCustomerRecordInput.model_validate(arguments)
    except ValidationError as exc:
        raise MCPError(code=types.INVALID_PARAMS, message=_validation_message(exc)) from exc

    try:
        record = await store.get(parsed.customer_id)
    except CustomerNotFoundError:
        return _error_result(f"No customer found with customer_id {parsed.customer_id!r}.")

    return _text_result(json.dumps(record))


async def _call_trigger_refund(store: CustomerStore, arguments: dict[str, object]) -> types.CallToolResult:
    try:
        parsed = TriggerRefundInput.model_validate(arguments)
    except ValidationError as exc:
        raise MCPError(code=types.INVALID_PARAMS, message=_validation_message(exc)) from exc

    try:
        result = await store.refund(parsed.customer_id, parsed.amount, parsed.reason)
    except CustomerNotFoundError:
        return _error_result(f"No customer found with customer_id {parsed.customer_id!r}.")
    except RefundExceedsBalanceError as exc:
        return _error_result(str(exc))

    return _text_result(json.dumps(result))


def _validation_message(exc: ValidationError) -> str:
    """Build a concise 'field: reason' message for INVALID_PARAMS -- names
    the failing field(s) without leaking pydantic's internal error shape."""
    parts = []
    for err in exc.errors():
        field = ".".join(str(loc) for loc in err["loc"]) or "<root>"
        parts.append(f"{field}: {err['msg']}")
    return "Invalid parameters -- " + "; ".join(parts)


async def run_stdio(server: Server[None]) -> None:
    """Serve `server` over stdio until stdin closes (EOF) or the process is
    asked to stop. EOF is a normal shutdown, not an error. Requests already
    received when stdin closes are still answered first (see stdio_drain)."""

    async def run_server(read_stream, write_stream) -> None:
        await server.run(read_stream, write_stream, server.create_initialization_options())

    async with stdio_server() as (read_stream, write_stream):
        await serve_draining(run_server, read_stream, write_stream)
