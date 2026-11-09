"""Unit tests for stdio_drain: EOF is deferred until in-flight requests are
answered, but never forever."""

from __future__ import annotations

import time

import anyio
import mcp_types as types
from mcp.shared.message import SessionMessage

from customer_mcp.stdio_drain import serve_draining


def _request(req_id: int) -> SessionMessage:
    return SessionMessage(types.JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/call", params={"name": "x"}))


def _cancel(req_id: int) -> SessionMessage:
    return SessionMessage(types.JSONRPCNotification(
        jsonrpc="2.0", method="notifications/cancelled", params={"requestId": req_id}))


async def _run(incoming: list[SessionMessage], answer: set[int], answer_delay: float, drain_timeout: float):
    """Fake server: answers the request ids in `answer` after `answer_delay`,
    never answers the others. Returns (replies written to the transport,
    seconds until the server saw EOF)."""
    t_send, t_recv = anyio.create_memory_object_stream[SessionMessage | Exception](100)
    w_send, w_recv = anyio.create_memory_object_stream[SessionMessage](100)
    for m in incoming:
        await t_send.send(m)
    await t_send.aclose()  # stdin EOF right after the burst
    started = time.monotonic()
    eof_after: list[float] = []

    async def fake_server(read, write) -> None:
        async with write:
            async with anyio.create_task_group() as tg:
                async with read:
                    async for item in read:
                        msg = item.message
                        if isinstance(msg, types.JSONRPCRequest) and msg.id in answer:
                            async def reply(rid=msg.id) -> None:
                                await anyio.sleep(answer_delay)
                                await write.send(SessionMessage(
                                    types.JSONRPCResponse(jsonrpc="2.0", id=rid, result={})))
                            tg.start_soon(reply)
                eof_after.append(time.monotonic() - started)
                tg.cancel_scope.cancel()  # what the SDK does at EOF

    await serve_draining(fake_server, t_recv, w_send, drain_timeout=drain_timeout)
    replies = [m.message.id async for m in w_recv]
    return replies, eof_after[0]


async def test_eof_waits_for_slow_replies() -> None:
    replies, eof_after = await _run([_request(1), _request(2)], {1, 2}, answer_delay=0.3, drain_timeout=5)
    assert sorted(replies) == [1, 2]
    assert eof_after >= 0.3  # EOF was held until the replies were written


async def test_hung_request_cannot_block_shutdown_forever() -> None:
    replies, eof_after = await _run([_request(1), _request(2)], {1}, answer_delay=0.05, drain_timeout=0.5)
    assert replies == [1]
    assert 0.5 <= eof_after < 2.0


async def test_cancelled_request_does_not_hold_shutdown() -> None:
    # Request 2 is never answered, but the client cancelled it, so EOF must
    # not wait out the (long) drain timeout for it.
    replies, eof_after = await _run([_request(1), _request(2), _cancel(2)], {1}, answer_delay=0.05,
                                    drain_timeout=10)
    assert replies == [1]
    assert eof_after < 2.0
