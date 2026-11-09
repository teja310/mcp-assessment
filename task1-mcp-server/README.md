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

**Important for scripted/piped use:** a well-behaved client reads each
response before sending the next request, or at least before closing
stdin. This SDK (see "SDK version" below) cancels any request whose
response hasn't been written yet as soon as stdin reaches EOF — so a
shell pipeline that blasts several requests at once and closes stdin
immediately can lose the last response. This is a transport-teardown
characteristic of the installed SDK, not a bug in this server; the test
suite and any normal MCP client (which always waits for a reply before
proceeding) never hit it.

### Registering with Claude Code

```bash
claude mcp add customer-server -- /absolute/path/to/mcp-task1/.venv/bin/python -m customer_mcp
```

## Running the tests

```bash
uv pip install -e ".[dev]"
python -m pytest tests/test_e2e.py -v
```

All 33 tests spawn the server as a real subprocess and talk raw NDJSON
over its stdin/stdout — no SDK client involved — so what's asserted is
exactly what a client on the wire would see.

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
| Unknown tool name | JSON-RPC `error` | `-32601` `METHOD_NOT_FOUND` (see note below) |
| Unexpected/unhandled exception inside a tool handler | JSON-RPC `error` | `-32603` `INTERNAL_ERROR` (generic message; full traceback logged to stderr only) |
| Customer not found | Normal result, `isError: true` | — (business-level miss, not a protocol error) |
| Refund amount exceeds refundable balance | Normal result, `isError: true` | — (business-level miss) |

**Unknown-tool-name policy:** this server returns `METHOD_NOT_FOUND`
(-32601) rather than `INVALID_PARAMS` (-32602). Rationale: a tool name is
analogous to a JSON-RPC *method* — "I don't support this operation at
all" is squarely what -32601 means, whereas -32602 is reserved here for a
*recognized* tool called with bad arguments. (Both are defensible; this
is the documented pick, per the task's instruction to choose one.)

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
treated as a normal shutdown, not an error. `tests/test_e2e.py`'s
`test_stdout_is_pure_jsonrpc_and_stderr_has_logs` proves — by actually
running the server and parsing every stdout line as JSON — that nothing
but JSON-RPC ever reaches stdout, and that stderr does carry log output.

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
tests/
    test_e2e.py           # subprocess + raw NDJSON-over-stdio e2e tests
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
