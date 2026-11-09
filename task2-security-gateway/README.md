# mcp-gateway

An HTTP JSON-RPC reverse proxy that sits between an AI agent and a
downstream MCP server, authenticates the caller by bearer token, and
blocks `admin_*` tool calls for non-admin roles **before** they reach the
downstream server.

Starlette + httpx, streaming pass-through (works with Streamable HTTP's
SSE responses), no SDK client involved.

## Running it

```bash
uv venv && uv pip install -e ".[dev]"

# terminal 1: mock downstream MCP server on :9000
python -m mcp_gateway.mock_downstream

# terminal 2: the gateway on :8080
GATEWAY_TOKENS='{"admin-token-123":"admin","viewer-token-456":"viewer"}' python -m mcp_gateway

# terminal 3
curl -s localhost:8080/mcp -H 'Authorization: Bearer viewer-token-456' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"k1"}}}'
# {"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Unauthorized Tool Call","data":{"tool":"admin_reset_key","required_role":"admin"}}}
```

| Env var | Default | Meaning |
|---|---|---|
| `GATEWAY_DOWNSTREAM_URL` | `http://127.0.0.1:9000/mcp` | downstream MCP endpoint |
| `GATEWAY_TOKENS` / `GATEWAY_TOKENS_FILE` | — (required) | `{"<token>": "<role>"}` |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8080` | listen address |

The gateway refuses to start with no tokens configured.

## Request flow (`src/mcp_gateway/app.py`)

1. **Authenticate:** `Authorization: Bearer <token>` → role. Missing, wrong
   scheme or unknown token → HTTP 401, `WWW-Authenticate: Bearer`, JSON-RPC
   error `-32001`. This applies to every method, including `tools/list`.
2. **Read body**, capped at 1 MiB → 413.
3. **Strict JSON-RPC parse** (`jsonrpc.py`) → `-32700` / `-32600` with HTTP 400.
4. **Policy**
   - `tools/list` (and every non-`tools/call` method) → forwarded transparently.
   - `tools/call` → `params.name` must be a non-empty string (`-32602`).
     If it starts with `admin_` and role ≠ `admin` → **`-32001 Unauthorized
     Tool Call` returned by the gateway itself; downstream never contacted.**
5. **Forward** the exact raw bytes that were validated, with an allowlist of
   headers, and stream the downstream response back.

| Situation | HTTP | JSON-RPC code |
|---|---|---|
| no / bad token | 401 | -32001 |
| non-admin calls `admin_*` | 200 | -32001 `Unauthorized Tool Call` |
| same, but sent as a notification (no `id`) | 202, empty body | — (JSON-RPC forbids replying) |
| invalid JSON, duplicate keys, bad UTF-8 | 400 | -32700 |
| batch, wrong `jsonrpc`, bad `id`/`method`/`params` | 400 | -32600 |
| `tools/call` without a string `params.name` | 400 | -32602 |
| downstream unreachable | 502 | -32002 (no internal details) |

Why 200 for the policy denial: it is a JSON-RPC-level error. MCP clients
surface those to the agent, whereas a 4xx is treated as a transport failure.

## Security decisions

- **Authorize what is executed.** The gateway forwards the *same bytes* it
  parsed, and the parser rejects anything that two JSON parsers might read
  differently:
  - **Duplicate keys** (`{"name":"get_weather","name":"admin_reset_key"}`)
    are rejected, so the gateway and the downstream can't disagree on `name`.
  - **Batches** are rejected. MCP 2025-06-18 removed batching, and otherwise
    an `admin_` call could hide in element 2.
- **Prefix check is normalized.** NFKC + casefold + strip. `ADMIN_x`,
  `" admin_x"` and full-width `ａｄｍｉｎ_x` are all denied. The original name
  is what gets forwarded; normalization is only for the decision.
  `get_admin_status` is *not* an admin tool (prefix, not substring).
- **Only an exact `admin` role** passes. Unknown roles are treated as
  non-admin.
- **Token termination.** The client's `Authorization` header is not
  forwarded downstream. Tokens are held only as SHA-256 digests; audit logs
  (stderr) show a 12-character fingerprint, never the token.
- **Errors are sanitized.** A downstream connection failure returns a fixed
  message; details go to the log.

## Tests

```bash
python -m pytest -q    # 46 passed
```

- **In-process** (`test_gateway.py`): drive the gateway through
  `httpx.ASGITransport` against the mock downstream. The mock records every
  request it receives, so "downstream never contacted" is asserted directly
  (`gw.received == []`), not inferred. This covers POST and also the
  Streamable HTTP **GET** (SSE stream) and **DELETE** (session end) paths:
  they require auth, carry `Mcp-Session-Id` through, and never forward the
  client's token. Other HTTP methods → 405.
- **Real processes** (`test_end_to_end_over_real_http`): both servers as
  separate processes on real ports.
- **Real MCP client and server** (`test_real_mcp.py`): the **official MCP
  SDK client** talks through the gateway to an **official SDK server**
  (`Server.streamable_http_app()`), all on real sockets. This exercises the
  SDK's own handshake, SSE parsing, session IDs and DELETE-on-close. A
  viewer's `admin_reset_key` surfaces in the client as `MCPError(-32001)`,
  the downstream's tool log shows it never ran, and the session stays usable.
  A bad token surfaces as the gateway's 401 error before anything reaches
  the downstream. `mcp==2.3.0` is a *dev* dependency only; the gateway
  doesn't need it.

Mutation check: the suite was run against three deliberately broken
policies, and each was caught:

| Mutation | Result |
|---|---|
| `may_call_tool` always returns `True` | 8 failed |
| prefix check without normalization | 5 failed |
| duplicate-key rejection removed | 1 failed |

## Not in scope / next steps

- **Signed tokens.** Tokens are opaque and looked up in a static table.
  Swapping `TokenStore` for JWT/OIDC verification (role from a claim) would
  not change the rest of the gateway.
- **Filtering `tools/list`.** The spec asks for transparent forwarding, so
  viewers can *see* admin tools but not call them. Hiding them from the list
  would be a small response-rewrite step.
- **Server-initiated requests.** Responses on the GET SSE stream are passed
  through and not inspected; only client→server `tools/call` is policed.
