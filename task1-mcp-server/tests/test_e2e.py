"""Subprocess-level end-to-end tests.

These spawn `python -m customer_mcp` as a real subprocess and speak raw
newline-delimited JSON-RPC over its stdin/stdout -- deliberately NOT using
the `mcp` SDK's own client, so what we assert on is the literal wire
format a client would see, not whatever the SDK client might normalize or
hide.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator

import pytest

PYTHON = sys.executable


class ServerProcess:
    """Thin wrapper over a `python -m customer_mcp` subprocess: send a JSON
    object as one NDJSON line, read one NDJSON line back as a JSON object."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "customer_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        self._next_id = 0
        self.stdout_lines: list[str] = []

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def send_raw(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def recv(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if line == "":
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"server closed stdout unexpectedly. stderr:\n{stderr}")
        self.stdout_lines.append(line)
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        req_id = self._id()
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        resp = self.recv()
        assert resp.get("id") == req_id, f"id mismatch: sent {req_id}, got {resp}"
        return resp

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def initialize(self) -> dict:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e-test", "version": "0.0.1"},
            },
        )
        self.notify("notifications/initialized")
        return resp

    def close(self) -> tuple[str, str]:
        """Terminate and return (remaining_stdout, stderr)."""
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        stdout_rest = self.proc.stdout.read() if self.proc.stdout else ""
        stderr_all = self.proc.stderr.read() if self.proc.stderr else ""
        return stdout_rest, stderr_all


@pytest.fixture
def server() -> Iterator[ServerProcess]:
    s = ServerProcess()
    s.initialize()
    yield s
    s.close()


VALID_CUSTOMER_ID = "CUST-00001"
SUSPENDED_ZERO_BALANCE_ID = "CUST-00003"  # balance 0.00


# ---------------------------------------------------------------------------
# 1. initialize / tools/list
# ---------------------------------------------------------------------------


def test_initialize_and_tools_list(server: ServerProcess) -> None:
    resp = server.request("tools/list")
    assert "result" in resp, resp
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert set(tools) == {"get_customer_record", "trigger_refund"}
    for tool in tools.values():
        assert "inputSchema" in tool
        assert tool["inputSchema"].get("type") == "object"
        assert tool["inputSchema"].get("properties")


# ---------------------------------------------------------------------------
# 2. happy paths
# ---------------------------------------------------------------------------


def test_get_customer_record_happy_path(server: ServerProcess) -> None:
    resp = server.call_tool("get_customer_record", {"customer_id": VALID_CUSTOMER_ID})
    assert "result" in resp, resp
    result = resp["result"]
    assert result.get("isError") is not True
    record = json.loads(result["content"][0]["text"])
    assert record["customer_id"] == VALID_CUSTOMER_ID


def test_trigger_refund_happy_path(server: ServerProcess) -> None:
    resp = server.call_tool(
        "trigger_refund",
        {"customer_id": VALID_CUSTOMER_ID, "amount": 10.00, "reason": "Duplicate charge on invoice"},
    )
    assert "result" in resp, resp
    result = resp["result"]
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["customer_id"] == VALID_CUSTOMER_ID
    assert "refund_id" in payload and payload["refund_id"]


def test_trigger_refund_twice_creates_distinct_refunds(server: ServerProcess) -> None:
    args = {"customer_id": VALID_CUSTOMER_ID, "amount": 5.00, "reason": "Goodwill credit issued"}
    resp1 = server.call_tool("trigger_refund", args)
    resp2 = server.call_tool("trigger_refund", args)
    id1 = json.loads(resp1["result"]["content"][0]["text"])["refund_id"]
    id2 = json.loads(resp2["result"]["content"][0]["text"])["refund_id"]
    assert id1 != id2


# ---------------------------------------------------------------------------
# 3. edge cases -> JSON-RPC error, code -32602
# ---------------------------------------------------------------------------

BAD_CUSTOMER_IDS = [
    "cust-12345",
    "CUST-1234",
    "CUST-123456",
    "CUST-12A45",
    " CUST-12345",
    "CUST-12345\n",
    "",
    None,
    12345,
]


@pytest.mark.parametrize("bad_id", BAD_CUSTOMER_IDS)
def test_get_customer_record_bad_customer_id(server: ServerProcess, bad_id) -> None:
    resp = server.call_tool("get_customer_record", {"customer_id": bad_id})
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32602


def test_get_customer_record_missing_customer_id(server: ServerProcess) -> None:
    resp = server.call_tool("get_customer_record", {})
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32602


BAD_AMOUNTS = [0, -5, "10", None, True, 1e309]  # 1e309 overflows to float('inf')


@pytest.mark.parametrize("bad_amount", BAD_AMOUNTS)
def test_trigger_refund_bad_amount(server: ServerProcess, bad_amount) -> None:
    resp = server.call_tool(
        "trigger_refund",
        {"customer_id": VALID_CUSTOMER_ID, "amount": bad_amount, "reason": "Valid reason text here"},
    )
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32602


def test_trigger_refund_missing_amount(server: ServerProcess) -> None:
    resp = server.call_tool(
        "trigger_refund", {"customer_id": VALID_CUSTOMER_ID, "reason": "Valid reason text here"}
    )
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32602


BAD_REASONS = ["short", "          ", "123456789", 12345]


@pytest.mark.parametrize("bad_reason", BAD_REASONS)
def test_trigger_refund_bad_reason(server: ServerProcess, bad_reason) -> None:
    resp = server.call_tool(
        "trigger_refund",
        {"customer_id": VALID_CUSTOMER_ID, "amount": 10.0, "reason": bad_reason},
    )
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32602


def test_trigger_refund_extra_unknown_field(server: ServerProcess) -> None:
    resp = server.call_tool(
        "trigger_refund",
        {
            "customer_id": VALID_CUSTOMER_ID,
            "amount": 10.0,
            "reason": "Valid reason text here",
            "unexpected_field": "nope",
        },
    )
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32602


def test_get_customer_record_extra_unknown_field(server: ServerProcess) -> None:
    resp = server.call_tool(
        "get_customer_record", {"customer_id": VALID_CUSTOMER_ID, "extra": "nope"}
    )
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# 4. unknown tool name
# ---------------------------------------------------------------------------


def test_unknown_tool_name(server: ServerProcess) -> None:
    resp = server.call_tool("not_a_real_tool", {})
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32601  # METHOD_NOT_FOUND, documented in README


# ---------------------------------------------------------------------------
# 5. business-level misses -> isError result, NOT a JSON-RPC error
# ---------------------------------------------------------------------------


def test_get_customer_record_nonexistent_customer(server: ServerProcess) -> None:
    resp = server.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
    assert "result" in resp, resp
    assert "error" not in resp
    assert resp["result"]["isError"] is True


def test_trigger_refund_over_balance(server: ServerProcess) -> None:
    resp = server.call_tool(
        "trigger_refund",
        {"customer_id": SUSPENDED_ZERO_BALANCE_ID, "amount": 50.0, "reason": "Attempted over-refund here"},
    )
    assert "result" in resp, resp
    assert "error" not in resp
    assert resp["result"]["isError"] is True


def test_trigger_refund_nonexistent_customer(server: ServerProcess) -> None:
    resp = server.call_tool(
        "trigger_refund",
        {"customer_id": "CUST-99999", "amount": 10.0, "reason": "Valid reason text here"},
    )
    assert "result" in resp, resp
    assert "error" not in resp
    assert resp["result"]["isError"] is True


# ---------------------------------------------------------------------------
# 6. stdout purity + stderr has log output
# ---------------------------------------------------------------------------


def test_stdout_is_pure_jsonrpc_and_stderr_has_logs() -> None:
    s = ServerProcess()
    s.initialize()
    s.request("tools/list")
    s.call_tool("get_customer_record", {"customer_id": VALID_CUSTOMER_ID})
    s.call_tool("get_customer_record", {"customer_id": "not-even-close"})  # triggers a log line
    stdout_rest, stderr_all = s.close()

    all_stdout = "".join(s.stdout_lines) + stdout_rest
    for line in all_stdout.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)  # fails the test if any line isn't valid JSON
        assert obj.get("jsonrpc") == "2.0"

    assert stderr_all.strip() != "", "expected log output on stderr"


# ---------------------------------------------------------------------------
# 7. malformed JSON on stdin doesn't crash the server
# ---------------------------------------------------------------------------


def test_malformed_json_line_does_not_crash_server(server: ServerProcess) -> None:
    server.send_raw("{not valid json at all")
    # The server should still be alive and answer a subsequent valid request.
    resp = server.call_tool("get_customer_record", {"customer_id": VALID_CUSTOMER_ID})
    assert "result" in resp, resp
