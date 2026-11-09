"""Smoke test with the official MCP Python SDK *client* (not hand-written
JSON): proves a real MCP client can spawn the server over stdio, complete the
handshake, list tools, call them, and receive JSON-RPC errors as MCPError."""

from __future__ import annotations

import json
import sys

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError


async def test_official_sdk_client_round_trip() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "customer_mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "customer-mcp"

            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {"get_customer_record", "trigger_refund"}

            record = await session.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
            assert record.is_error is not True
            assert json.loads(record.content[0].text)["customer_id"] == "CUST-00001"

            refund = await session.call_tool(
                "trigger_refund", {"customer_id": "CUST-00001", "amount": 1.5, "reason": "SDK client smoke test"}
            )
            assert refund.is_error is not True and json.loads(refund.content[0].text)["refund_id"]

            missing = await session.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
            assert missing.is_error is True  # business miss: a result, not an error

            with pytest.raises(MCPError) as bad_args:
                await session.call_tool("trigger_refund", {"customer_id": "bad", "amount": 1, "reason": "x"})
            assert bad_args.value.code == -32602

            with pytest.raises(MCPError) as unknown:
                await session.call_tool("nope", {})
            assert unknown.value.code == -32602 and "Unknown tool: nope" in str(unknown.value)
