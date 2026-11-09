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
SERVER_ARGV = [PYTHON, "-m", "customer_mcp"]


class WireViolation(AssertionError):
    """Something other than a JSON-RPC 2.0 message appeared on server stdout."""


def parse_wire_line(line: str) -> dict:
    """Strictly parse one line of server stdout.

    Every line on the wire must be a JSON object with jsonrpc == "2.0".
    Nothing is skipped or filtered: blank lines, whitespace-only lines and
    non-JSON text all raise WireViolation.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise WireViolation(f"non-JSON line on stdout: {line!r}") from exc
    if not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0":
        raise WireViolation(f"stdout line is not a JSON-RPC 2.0 message: {line!r}")
    return obj


def parse_wire(text: str) -> list[dict]:
    """Strictly parse a chunk of stdout (e.g. everything read after EOF)."""
    if text and not text.endswith("\n"):
        raise WireViolation(f"stdout does not end with a newline: {text[-80:]!r}")
    return [parse_wire_line(line) for line in text.splitlines()]


class ServerProcess:
    """Thin wrapper over a `python -m customer_mcp` subprocess: send a JSON
    object as one NDJSON line, read one NDJSON line back as a JSON object.

    Every stdout line read -- by recv() and by close() -- goes through
    parse_wire_line(), so any stray byte on stdout fails whichever test
    caused it."""

    def __init__(self, argv: list[str] | None = None) -> None:
        self.proc = subprocess.Popen(
            argv or SERVER_ARGV,
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
        return parse_wire_line(line)

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
        """Close stdin, wait for exit, read stdout and stderr to EOF, and
        strictly validate the remaining stdout. Returns (remaining_stdout,
        stderr)."""
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
        self.stderr = stderr_all
        parse_wire(stdout_rest)  # trailing pollution fails the test too
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
    # Per the MCP spec's tools/call example: -32602 "Unknown tool: <name>".
    assert resp["error"] == {"code": -32602, "message": "Unknown tool: not_a_real_tool"}


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


def _full_session() -> tuple[ServerProcess, str, str]:
    """Drive one complete session covering every stdout-producing path, then
    close stdin, wait for exit and read both pipes to EOF.

    Each request waits for its response before the next is sent (and before
    stdin is closed), so the expected response count is deterministic."""
    s = ServerProcess()
    s.initialize()  # 1 response; notifications/initialized gets none
    assert "result" in s.request("tools/list")  # 2
    ok = s.call_tool("get_customer_record", {"customer_id": VALID_CUSTOMER_ID})  # 3
    assert "result" in ok and ok["result"].get("isError") is not True, ok
    refund = s.call_tool(
        "trigger_refund",
        {"customer_id": VALID_CUSTOMER_ID, "amount": 1.00, "reason": "Purity test refund"},
    )  # 4
    assert "result" in refund and refund["result"].get("isError") is not True, refund
    bad = s.call_tool("get_customer_record", {"customer_id": "not-even-close"})  # 5
    assert bad["error"]["code"] == -32602, bad
    unknown = s.call_tool("not_a_real_tool", {})  # 6
    assert "error" in unknown, unknown
    stdout_rest, stderr_all = s.close()
    return s, stdout_rest, stderr_all


EXPECTED_SESSION_RESPONSES = 6


def test_stdout_is_pure_jsonrpc() -> None:
    s, stdout_rest, _ = _full_session()
    assert s.proc.returncode == 0, f"server exited with {s.proc.returncode}"

    all_stdout = "".join(s.stdout_lines) + stdout_rest
    messages = parse_wire(all_stdout)  # raises on any non-JSON-RPC byte
    assert len(messages) == EXPECTED_SESSION_RESPONSES, (
        f"expected exactly {EXPECTED_SESSION_RESPONSES} lines on stdout, got "
        f"{len(messages)}:\n{all_stdout}"
    )
    assert stdout_rest == "", f"unexpected extra stdout after last response: {stdout_rest!r}"
    assert [m.get("id") for m in messages] == list(range(1, EXPECTED_SESSION_RESPONSES + 1))


def test_stderr_has_logs_and_no_jsonrpc_responses() -> None:
    _, _, stderr_all = _full_session()
    assert stderr_all.strip() != "", "expected log output on stderr"
    for line in stderr_all.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # ordinary log text: exactly what stderr is for
        is_response = isinstance(obj, dict) and obj.get("jsonrpc") == "2.0" and (
            "result" in obj or "error" in obj
        )
        assert not is_response, f"JSON-RPC response leaked onto stderr: {line!r}"


def test_harness_detects_stdout_pollution() -> None:
    """Control: prove the reader is not blind. Launch the real server via a
    wrapper that writes one junk line to fd 1 *before* the server starts
    (outside the SDK's fd-1 diversion window), and require that the first
    read fails."""
    wrapper = (
        "import os, runpy; os.write(1, b'junk\\n'); "
        "runpy.run_module('customer_mcp', run_name='__main__', alter_sys=True)"
    )
    s = ServerProcess([PYTHON, "-c", wrapper])
    try:
        with pytest.raises(WireViolation, match="junk"):
            s.initialize()
    finally:
        s.proc.kill()
        s.proc.wait(timeout=5)


SDK_GUARD_PROBE = """
import os, sys, anyio
from mcp.server.stdio import stdio_server

async def main():
    async with stdio_server() as (_read, write):
        sys.stdout.write("SYS_STDOUT_MARKER\\n"); sys.stdout.flush()
        os.write(1, b"FD1_MARKER\\n")
        await write.aclose()
    os.write(1, b"AFTER_RESTORE\\n")

anyio.run(main)
"""


def test_sdk_stdio_server_diverts_fd1_to_stderr() -> None:
    """Pin the SDK behaviour this server relies on: while `stdio_server()` is
    serving, fd 1 is dup2'd onto stderr, so handler-level sys.stdout.write /
    os.write(1, ...) can't reach the wire. If an SDK upgrade drops this, this
    test fails and the README's mutation-check table needs revisiting."""
    proc = subprocess.run(
        [PYTHON, "-c", SDK_GUARD_PROBE],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "AFTER_RESTORE\n", proc.stdout
    assert "SYS_STDOUT_MARKER" in proc.stderr
    assert "FD1_MARKER" in proc.stderr


def test_all_requests_answered_when_stdin_closes_immediately() -> None:
    """Write a burst of requests and close stdin at once, without waiting for
    any replies (a half-close). Every request must still get its response.
    Before stdio_drain, the SDK cancelled in-flight handlers at EOF and the
    tail of the burst was lost (20/20 runs dropped a reply)."""
    n_calls = 50
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "burst", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    for i in range(n_calls):
        lines.append({"jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
                      "params": {"name": "get_customer_record", "arguments": {"customer_id": VALID_CUSTOMER_ID}}})
    proc = subprocess.run(
        SERVER_ARGV,
        input="".join(json.dumps(m) + "\n" for m in lines),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    messages = parse_wire(proc.stdout)
    assert sorted(m["id"] for m in messages) == [1, *range(100, 100 + n_calls)]
    assert all("result" in m for m in messages)


# ---------------------------------------------------------------------------
# 7. malformed JSON on stdin doesn't crash the server
# ---------------------------------------------------------------------------


def test_malformed_json_line_does_not_crash_server(server: ServerProcess) -> None:
    server.send_raw("{not valid json at all")
    # The server should still be alive and answer a subsequent valid request.
    resp = server.call_tool("get_customer_record", {"customer_id": VALID_CUSTOMER_ID})
    assert "result" in resp, resp
