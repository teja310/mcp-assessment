"""Answer every request before shutting down on stdin EOF.

The problem: when stdin reaches EOF, the SDK's `JSONRPCDispatcher.run`
(mcp 2.3.0, `shared/jsonrpc_dispatcher.py`) deliberately cancels all
in-flight handlers ("handlers whose callers are already gone"). That's right
for HTTP, but over stdio a client that writes its requests and then closes
stdin is half-closing: it's finished *sending*, not *listening*. Without this
module, e.g. `printf '<init>\\n<initialized>\\n<list>\\n<call>\\n' | server`
reliably lost the last response (20/20 runs).

The fix, without patching the SDK: sit between the transport and the server.

    stdio read  --pump_in-->   server read     (records request ids)
    server write --pump_out--> stdio write     (clears ids as replies pass)

When stdin hits EOF, `pump_in` does NOT close the server's read side
straight away. It first waits until every request it forwarded has had its
response or error written, or was cancelled by the client via
`notifications/cancelled`. Then it signals EOF. A hung handler can't hold
the process open forever: after `drain_timeout` seconds the remaining
requests are logged and EOF is delivered anyway (the SDK cancels them, as
before).
"""

from __future__ import annotations

import logging
from typing import Any

import anyio
import mcp_types as types
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from mcp.shared.message import SessionMessage

logger = logging.getLogger("customer_mcp.stdio_drain")

DEFAULT_DRAIN_TIMEOUT = 30.0


class _Pending:
    def __init__(self) -> None:
        self.ids: set[Any] = set()
        self.empty = anyio.Event()
        self.empty.set()

    def add(self, req_id: Any) -> None:
        self.ids.add(req_id)
        if self.empty.is_set():
            self.empty = anyio.Event()

    def discard(self, req_id: Any) -> None:
        self.ids.discard(req_id)
        if not self.ids:
            self.empty.set()


async def serve_draining(
    run_server: Any,
    read_stream: ObjectReceiveStream[SessionMessage | Exception],
    write_stream: ObjectSendStream[SessionMessage],
    drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
) -> None:
    """Run `run_server(inner_read, inner_write)` with EOF deferred until all
    forwarded requests have been answered."""
    pending = _Pending()
    in_send, in_recv = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    out_send, out_recv = anyio.create_memory_object_stream[SessionMessage](0)

    async def pump_in() -> None:
        async with in_send:
            async with read_stream:
                async for item in read_stream:
                    if isinstance(item, SessionMessage):
                        msg = item.message
                        if isinstance(msg, types.JSONRPCRequest):
                            pending.add(msg.id)
                        elif isinstance(msg, types.JSONRPCNotification) and msg.method == "notifications/cancelled":
                            params = msg.params or {}
                            pending.discard(params.get("requestId"))
                    await in_send.send(item)
            # stdin EOF. Let in-flight requests finish before the server sees it.
            if pending.ids:
                logger.info("stdin closed; waiting for %d in-flight request(s) to finish", len(pending.ids))
                with anyio.move_on_after(drain_timeout) as scope:
                    await pending.empty.wait()
                if scope.cancelled_caught:
                    logger.warning(
                        "drain timeout (%.0fs): shutting down with %d unanswered request(s): %s",
                        drain_timeout, len(pending.ids), sorted(map(str, pending.ids)),
                    )

    async def pump_out() -> None:
        async with write_stream:
            async with out_recv:
                async for item in out_recv:
                    await write_stream.send(item)
                    # Clear only AFTER the reply is handed to the stdout
                    # writer, so EOF can't overtake the last response.
                    msg = item.message
                    if isinstance(msg, (types.JSONRPCResponse, types.JSONRPCError)):
                        pending.discard(msg.id)

    async with anyio.create_task_group() as tg:
        tg.start_soon(pump_in)
        tg.start_soon(pump_out)
        await run_server(in_recv, out_send)
        # Server finished (EOF delivered, or it stopped on its own). If it
        # stopped before stdin closed, don't keep pump_in waiting on stdin.
        tg.cancel_scope.cancel()
