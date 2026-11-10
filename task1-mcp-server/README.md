# customer-mcp

An MCP server, over stdio, exposing two tools backed by an in-memory mock
customer store:

- `get_customer_record` — look up a customer by `customer_id`.
- `trigger_refund` — trigger a refund for a customer, up to their
  refundable balance.

Built with the **low-level** `mcp.server.lowlevel.Server` API (not
`FastMCP`), strict Pydantic v2 validation, and a subprocess-level e2e test
suite that asserts the literal JSON-RPC wire format.

## Running it

```bash
# from the project root, with the project's venv active
uv pip install -e .            # or: uv pip install -e ".[dev]" for tests
python -m customer_mcp
```

It speaks newline-delimited JSON-RPC 2.0 over stdin/stdout. Example
session (one request per line on stdin, one response per line on stdout):

```jsonc
// -> stdin
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"demo","version":"0.0.1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-00001"}}}

// <- stdout
{"jsonrpc":"2.0","id":1,"result":{...}}
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{...json customer record...}"}],"isError":false}}
```

**Piped use is safe:** a client may write all its requests and close
stdin straight away; every request already received is still answered
before the server exits.

The SDK (mcp 2.3.0, `JSONRPCDispatcher.run`) cancels in-flight handlers
the moment stdin reaches EOF. That's right for HTTP, where EOF means the
caller is gone, but over stdio it's a half-close. In the first version,
`printf '<init>\n<initialized>\n<list>\n<call>\n' | python -m customer_mcp`
lost the last reply in 20 of 20 runs. `stdio_drain.py` sits between the
transport and the server: it tracks request ids and holds back EOF until
each has a response written, or was cancelled via `notifications/cancelled`.
A 30s cap ensures a hung handler can't keep the process alive forever.
Now 20 of 20 runs return every reply, and
`test_all_requests_answered_when_stdin_closes_immediately` sends 50 calls
and closes stdin at once.

### Registering with Claude Code

```bash
claude mcp add customer-server -- /absolute/path/to/mcp-task1/.venv/bin/python -m customer_mcp
```

### Registering with Claude Desktop (or any client using `mcpServers` JSON)

Add this to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`,
Windows: `%APPDATA%\Claude\claude_desktop_config.json`), then restart
Claude Desktop:

```json
{
  "mcpServers": {
    "customer-server": {
      "command": "/absolute/path/to/mcp-task1/.venv/bin/python",
      "args": ["-m", "customer_mcp"]
    }
  }
}
```

Use the venv's Python by absolute path. Desktop apps don't inherit your
shell's `PATH` or activated venv, so a bare `python` usually resolves to a
system Python without the dependencies. The two tools then appear under
the tools menu. Server logs go to stderr, which Claude Desktop writes to
`~/Library/Logs/Claude/mcp-server-customer-server.log` on macOS.

## Demo and CI

`scripts/demo.sh` starts everything it needs on localhost, runs a short
scripted demonstration, and cleans up after itself.
The repository-level `.github/workflows/tests.yml` runs this suite and the demo on Ubuntu
and macOS with Python 3.12.

## Running the tests

```bash
uv pip install -e ".[dev]"
python -m pytest -v    # 41 passed
```

- `tests/test_e2e.py` (37 tests) spawns a real subprocess and talks raw
  NDJSON over its stdin/stdout, with no SDK client involved, so what's
  asserted is exactly what a client on the wire would see.
- `tests/test_sdk_client.py` drives the server with the **official MCP SDK
  client** (`stdio_client` + `ClientSession`): handshake, `tools/list`, both
  tools, a business miss, and both error cases arriving as `MCPError(-32602)`.
- `tests/test_stdio_drain.py` unit-tests EOF draining: slow replies are
  awaited, a hung request is cut off at the drain timeout, and a cancelled
  request doesn't hold shutdown.

The shared reader
(`parse_wire_line`) rejects any stdout line that isn't a JSON object with
`"jsonrpc": "2.0"`, including blank lines; nothing is skipped or filtered.

## Tool contracts

### `get_customer_record`

| field | type | rule |
|---|---|---|
| `customer_id` | string | must fully match `CUST-\d{5}` (exactly `CUST-` + 5 digits; no leading/trailing whitespace, no trailing newline, case-sensitive) |

No other fields are accepted (`extra="forbid"`).

Returns the customer record (id, name, email, status, balance, refund
history) as JSON text content.

### `trigger_refund`

| field | type | rule |
|---|---|---|
| `customer_id` | string | same as above |
| `amount` | number | strictly a `float`/`int` (not a numeric string, not a `bool`); finite; `0 < amount <= 1,000,000`; **at most 2 decimal places** |
| `reason` | string | after `strip()`, length is `>= 10` and `<= 500` |

No other fields are accepted.

**`amount` decimal-places policy:** more than 2 decimal places is
**rejected**, not silently rounded. A caller sending `19.999` almost
certainly meant `19.99` or `20.00` and got a typo; silently rounding would
hide a mistake that could matter for money. If you want rounding instead
of rejection, that's a one-line change in `schemas.py`'s
`_amount_must_be_finite_and_at_most_2dp` validator.

On success: checks the customer exists and the amount doesn't exceed
their refundable balance, generates a `uuid4()` refund id, appends it to
the customer's refund history, and returns a confirmation (refund id,
customer id, amount, reason, remaining balance) as JSON text content. Two
identical calls produce two distinct refunds (no idempotency/dedup
logic — each call is a new refund attempt).

## Error-code mapping

| Situation | Channel | Code |
|---|---|---|
| Malformed/invalid tool arguments (wrong type, bad pattern, missing field, extra field, out of range, etc.) | JSON-RPC `error` | `-32602` `INVALID_PARAMS` |
| Unknown tool name | JSON-RPC `error` | `-32602` `INVALID_PARAMS`, message `Unknown tool: <name>` (see note below) |
| Unexpected/unhandled exception inside a tool handler | JSON-RPC `error` | `-32603` `INTERNAL_ERROR` (generic message; full traceback logged to stderr only) |
| Customer not found | Normal result, `isError: true` | — (business-level miss, not a protocol error) |
| Refund amount exceeds refundable balance | Normal result, `isError: true` | — (business-level miss) |

**Unknown-tool-name policy:** this server returns `INVALID_PARAMS`
(-32602) with message `Unknown tool: <name>`, matching the MCP spec. The
[tools page](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
lists unknown tools under protocol errors, and its example error is exactly
`{"code": -32602, "message": "Unknown tool: invalid_tool_name"}`. The JSON-RPC
method (`tools/call`) exists; the `name` *parameter* is what's invalid, so
`METHOD_NOT_FOUND` (-32601) would misdescribe the error. (An earlier
version used -32601; it was changed to follow the spec.)

## Design decisions

### The `call_tool` decorator "pitfall" — and why this server doesn't have it

The brief this server was built against describes a known failure mode in
some `mcp` SDK versions: `@server.call_tool()` is a decorator that wraps
the registered function in a blanket `try/except`, turning *every*
exception — including a deliberately-raised protocol error — into a
successful `CallToolResult(isError=True)`. That would silently downgrade
a real JSON-RPC error into a fake 200-OK-shaped result.

Before writing any code, I checked what's actually installed:

```
$ python -c "from importlib.metadata import version; print(version('mcp'))"
2.3.0
```

I then read
`.venv/lib/python3.12/site-packages/mcp/server/lowlevel/server.py` and
`mcp/server/runner.py` / `mcp/shared/jsonrpc_dispatcher.py` directly.
**This version has no `@server.call_tool()` decorator at all** —
`grep -n "def call_tool" mcp/server/lowlevel/server.py` returns nothing.
The low-level `Server` is built by passing handler callables straight
into its constructor (`on_call_tool=`, `on_list_tools=`, ...); there's no
decorator layer to go through, and so none to bypass.

What I verified by reading the dispatch code instead of assuming it:

- The SDK validates only the generic envelope of a `tools/call` request
  (`{name: str, arguments: dict}`) before calling `on_call_tool`. The
  tool-specific argument shape is never touched by the SDK — that's
  entirely `schemas.py` + `server.py`'s job.
- Raising `mcp.shared.exceptions.MCPError(code, message, data)` from
  inside `on_call_tool` reaches the wire as a genuine JSON-RPC `error`
  object with that code (`jsonrpc_dispatcher.py`'s
  `handler_exception_to_error_data` unwraps `.error` directly) — it is
  **not** downgraded into an `isError` result.
- A bare `pydantic.ValidationError` raised from the handler is mapped by
  the same function straight to `ErrorData(code=INVALID_PARAMS, ...)`.
- Returning (not raising) `CallToolResult(isError=True, ...)` is the
  unmodified, correct channel for business-level misses.

So `server.py` never catches `MCPError` on its way out of
`handle_call_tool`, and validation failures are always *raised*
(`MCPError(INVALID_PARAMS, ...)`), never *returned* as a result. This is
the same outcome the task's described workaround was aiming for
("register the handler manually, don't let the SDK coerce a protocol
error into a fake success"), arrived at by using the constructor-based
API this SDK version actually offers rather than fighting a decorator
that doesn't exist here.

**Proof, not just inspection:** `tests/test_e2e.py` sends raw JSON-RPC
over the subprocess's stdin/stdout for every invalid-input case and
asserts the reply has a top-level `"error"` key with `error.code ==
-32602` — never a `"result"` with `isError`. See the manual demo above
(id `3`) for a live example: a too-short `reason` comes back as a real
`error` object, not a result.

### Other SDK-version adaptations

- Types live in the top-level `mcp_types` package, not `mcp.types` (no
  compatibility shim exists for the old import path in this version).
- `Tool.input_schema` (alias `inputSchema`), `CallToolResult.is_error`
  (alias `isError`), `ErrorData(code, message, data=None)`.
- `Server.run(read_stream, write_stream, init_options)` and
  `mcp.server.stdio.stdio_server()` are unchanged from the classic API.

### `amount` strict-typing

Pydantic v2 `strict=True` on a `float` field already rejects `bool`
(bool is an `int` subclass, but strict mode refuses the implicit
bool→float coercion) and numeric strings (`"10"`) — verified empirically,
not assumed. `int` is still accepted and promoted to `float`, which is
correct (`5` is a perfectly good amount). `inf`/`nan` are rejected by the
explicit `math.isfinite()` check in a field validator (they'd also fail
the `le=1_000_000` range constraint as a side effect, but the explicit
check documents the intent independently of that bound).

### `customer_id` regex anchoring

`Field(pattern=r"\ACUST-\d{5}\Z")` with
`ConfigDict(regex_engine="python-re")`. Two notes:
- Pydantic-core's default (Rust) regex engine doesn't support `\A`/`\Z`
  anchors at all — it raises `SchemaError` at class-definition time.
  Python's `re` module does, hence the explicit `regex_engine` override.
- `\A`/`\Z` instead of `^`/`$`: Python's `$` matches just before a
  trailing newline, so `^CUST-\d{5}$` would **accept**
  `"CUST-12345\n"`. `\Z` matches only the true end of string.

### STDIO isolation

`logging_setup.configure()` runs first in `__main__.py`, before any other
project module is imported: `logging.basicConfig(stream=sys.stderr, ...)`,
a `print()` → stderr shim, a `sys.excepthook`, and an asyncio loop
exception handler. `SIGINT`/`SIGTERM` trigger a clean shutdown via
`asyncio.Event` rather than an abrupt `KeyboardInterrupt`. EOF on stdin is
treated as a normal shutdown, not an error.

There is also a second, stronger layer that comes from the SDK, not from
this project: `mcp==2.3.0`'s `stdio_server()`
(`mcp/server/stdio.py`, `_claim_fd`) moves the real stdout pipe to a
private fd ≥ 3 and `dup2`s **fd 1 onto stderr** for as long as it is
serving. So `sys.stdout.write(...)` and even `os.write(1, ...)` inside a
handler land on stderr, not the wire. The window does **not** cover code
that runs before `stdio_server()` is entered (module import, `build_server`).

`test_stdout_is_pure_jsonrpc` runs a full session (initialize,
`notifications/initialized`, `tools/list`, a successful call to each tool,
a `-32602` error and an unknown tool), closes stdin, reads stdout to EOF
and requires exactly 6 valid JSON-RPC lines. `test_stderr_has_logs_and_no_jsonrpc_responses`
checks the other direction. `test_harness_detects_stdout_pollution` is a
control proving the reader really fails on junk, and
`test_sdk_stdio_server_diverts_fd1_to_stderr` pins the SDK behaviour
above so an upgrade that drops it is noticed.

### Stdout safety verification

Mutation checks (apply to `src/customer_mcp/server.py`, run
`python -m pytest tests/test_e2e.py -q`, then
`git checkout -- src/customer_mcp/server.py`):

| # | Mutation | Result | Why |
|---|----------|--------|-----|
| M1 | `print("debug")` first line of `handle_call_tool` | passes | `print` shim defaults to stderr (and fd 1 is diverted anyway) |
| M2 | `sys.stdout.write("debug\n"); sys.stdout.flush()` top of `handle_list_tools` | passes | SDK has fd 1 → stderr while serving; `debug` appears on stderr |
| M3 | same, top of `handle_call_tool` | passes | same as M2 |
| M4 | `os.write(1, b"debug\n")` top of `handle_call_tool` | passes | deliberate fd-level guard in the SDK (`dup2`) |
| M5 | `sys.stdout.write("debug\n")` at module import in `server.py` | **fails** (`WireViolation`) | runs before `stdio_server()` diverts fd 1, so it hits the wire |

M1–M4 passing is correct: the bytes never reach stdout (verified by
capturing the server's stdout and stderr separately). M5 and
`test_harness_detects_stdout_pollution` show the suite does fail when
pollution actually reaches the wire.

### Store concurrency

`CustomerStore` guards all reads and writes with one `asyncio.Lock`,
since `get_customer_record` and `trigger_refund` touch the same records
and stdio request handling is concurrent at the asyncio-task level.

## File layout

```
pyproject.toml
README.md
src/customer_mcp/
    __init__.py
    __main__.py          # entry point: python -m customer_mcp
    logging_setup.py      # stderr-only logging, print() shim, excepthook, asyncio handler
    schemas.py            # Pydantic v2 input models (shared validation + JSON Schema source)
    store.py               # in-memory mock store + asyncio.Lock
    server.py             # Server construction, on_list_tools/on_call_tool handlers
    stdio_drain.py        # answer in-flight requests before shutting down on stdin EOF
tests/
    test_e2e.py           # subprocess + raw NDJSON-over-stdio e2e tests
    test_sdk_client.py    # official MCP SDK client smoke test
    test_stdio_drain.py   # EOF-drain unit tests
```

## Assumptions / trade-offs

- The installed `mcp` SDK (2.3.0) has a materially different low-level
  API shape than the one the original brief assumed (see "Design
  decisions" above). I adapted to what's actually installed rather than
  pinning to a different version, since fighting the installed toolchain
  would make the "actually runnable in this environment" requirement
  harder to satisfy, not easier.
- `trigger_refund` has no idempotency key; two identical calls always
  create two distinct refunds, by design (per the task spec).
- The store is purely in-process and non-persistent — restarting the
  server resets all balances and refund history.
- `amount` over 2 decimal places is rejected, not rounded (see above).
