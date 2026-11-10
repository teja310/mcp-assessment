# mcp-gateway

An HTTP JSON-RPC reverse proxy that sits between an AI agent and a
downstream MCP server. It authenticates the caller by bearer token (an
opaque token from a table, and/or a **signed JWT** whose claim carries the
role) and blocks `admin_*` tool calls for non-admin roles **before** they
reach the downstream server.

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

Settings come from environment variables or a git-ignored `.env` file
(`cp .env.example .env`; real environment variables win).

| Env var | Default | Meaning |
|---|---|---|
| `GATEWAY_DOWNSTREAM_URL` | `http://127.0.0.1:9000/mcp` | downstream MCP endpoint |
| `GATEWAY_TOKENS` / `GATEWAY_TOKENS_FILE` | — | opaque tokens: `{"<token>": "<role>"}` |
| `GATEWAY_JWT_SECRET` | — | HMAC secret (≥ 32 bytes) → accept HS256 JWTs |
| `GATEWAY_JWT_PUBLIC_KEY_FILE` | — | PEM public key → accept RS256 (or `GATEWAY_JWT_ALGORITHMS`, e.g. `ES256`) JWTs |
| `GATEWAY_JWT_ISSUER` / `GATEWAY_JWT_AUDIENCE` | — | required `iss` / `aud` (a warning is logged if no audience is set) |
| `GATEWAY_JWT_ROLE_CLAIM` | `role` | claim holding the role: a string, or a list where `"admin"` grants admin |
| `GATEWAY_FILTER_TOOLS_LIST` | `0` | `1` = hide `admin_*` tools from non-admins' `tools/list` |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8080` | listen address |

Opaque tokens and JWTs can be enabled together; the first one that
authenticates wins. The gateway refuses to start with neither configured.

## Request flow (`src/mcp_gateway/app.py`)

1. **Authenticate:** `Authorization: Bearer <token>` → role. Missing, wrong
   scheme, unknown token or an invalid JWT → HTTP 401, `WWW-Authenticate:
   Bearer`, JSON-RPC error **`-32003 Unauthenticated`**. This applies to every
   method, including `tools/list`. (`-32001` is reserved for "Unauthorized
   Tool Call", so a client can tell "who are you?" from "you may not".)
2. **Read body**, capped at 1 MiB → 413.
3. **Strict JSON-RPC parse** (`jsonrpc.py`) → `-32700` / `-32600` with HTTP 400.
4. **Policy**
   - `tools/list` (and every non-`tools/call` method) → forwarded transparently.
     With `GATEWAY_FILTER_TOOLS_LIST=1`, non-admins' `tools/list` results have
     `admin_*` tools removed (JSON or SSE response; see below).
   - `tools/call` → `params.name` must be a non-empty string (`-32602`).
     If it starts with `admin_` and role ≠ `admin` → **`-32001 Unauthorized
     Tool Call` returned by the gateway itself; downstream never contacted.**
5. **Forward** the exact raw bytes that were validated, with an allowlist of
   headers, and stream the downstream response back.

| Situation | HTTP | JSON-RPC code |
|---|---|---|
| no / bad token, invalid or expired JWT | 401 | -32003 `Unauthenticated` |
| non-admin calls `admin_*` | 200 | -32001 `Unauthorized Tool Call` |
| same, but sent as a notification (no `id`) | 202, empty body | — (JSON-RPC forbids replying) |
| invalid JSON, duplicate keys, bad UTF-8 | 400 | -32700 |
| batch, wrong `jsonrpc`, bad `id`/`method`/`params` | 400 | -32600 |
| `tools/call` without a string `params.name` | 400 | -32602 |
| downstream unreachable | 502 | -32002 (no internal details) |

Why 200 for the policy denial: it is a JSON-RPC-level error. MCP clients
surface those to the agent, whereas a 4xx is treated as a transport failure.

## Signed JWTs (`auth.JwtVerifier`)

- **Algorithms come from configuration, never from the token.** A shared
  secret allows only `HS*`, a public key only asymmetric algorithms. That
  rules out `alg: none` and the classic key-confusion attack (an HS256 token
  signed with the server's *public* key as the HMAC secret). Both are tested.
- **`exp` is required**, `iss`/`aud` are required when configured, with 30s
  clock leeway. A token with an `aud` is rejected when the gateway has no
  audience configured, so a token minted for another service isn't accepted
  by accident.
- HMAC secrets under 32 bytes are refused at startup (RFC 7518 §3.2).
- The rejection reason (expired, bad signature, wrong audience…) is logged.
  The client only gets the generic 401.
- Audit logs carry the JWT `sub` and a 12-character token fingerprint.

## Optional `tools/list` filtering

The task asks for `tools/list` to be forwarded transparently, so filtering
is **off by default**. With it on, a non-admin's `tools/list` response is
buffered (capped at 4 MiB; over that it fails closed with an error rather
than passing an unfiltered list through), and every `admin_*` tool is
removed. The **same normalized check** as the call policy is used, so
`ADMIN_x` and `ａｄｍｉｎ_x` are hidden too, while `get_admin_status` stays.
Both `application/json` and SSE responses are handled. Other SSE events
(progress notifications, `event:`/`id:` lines) pass through untouched.
Hiding is cosmetic: the call policy still denies a viewer who knows the name.

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

## Demo and CI

`scripts/demo.sh` starts everything it needs on localhost, runs a short
scripted demonstration, and cleans up after itself.
The repository-level `.github/workflows/tests.yml` runs this suite and the demo on Ubuntu
and macOS with Python 3.12.

## Tests

```bash
python -m pytest -q    # 79 passed
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
  doesn't need it. A second real-SDK test uses **signed JWTs with filtering
  on**: the viewer's `list_tools()` shows only `get_weather` and calling the
  admin tool by name is still `-32001`; the admin sees and runs everything;
  a static token works alongside JWTs.
- **JWT and filter** (`test_jwt_and_filter.py`): HS256/RS256/ES256, role as a
  string or list, and 14 rejected token shapes (expired, wrong
  secret/issuer/audience, missing `exp`/`iss`/role, `nbf` in the future,
  `alg: none`, a tampered payload, a disallowed algorithm…). Also key
  confusion, unsafe configurations refused, and filtering of JSON and SSE
  responses (normalized prefix, admins unfiltered, off by default).

Mutation check: the suite was run against three deliberately broken
policies, and each was caught:

| Mutation | Result |
|---|---|
| `may_call_tool` always returns `True` | 8 failed |
| prefix check without normalization | 5 failed |
| duplicate-key rejection removed | 1 failed |
| JWT `exp` not required | 1 failed |
| JWT issuer not checked | 1 failed |
| JWT algorithms widened to HS+RS for any key | 2 failed |
| role list: any role grants admin | 1 failed |
| short HMAC secrets allowed | 1 failed |
| `tools/list` filter does nothing | 4 failed |
| filter uses the raw prefix, not the normalized check | 1 failed |
| 401 uses `-32001` again | 7 failed |
| filter applied to admins too | 3 failed |

## Not in scope / next steps

- **Key rotation via JWKS.** JWTs are verified with one configured secret or
  public key. Fetching keys from an IdP's JWKS URL (with caching and `kid`
  selection) would allow rotation without a restart.
- **Token revocation.** A JWT is valid until `exp`; keep lifetimes short.
- **Server-initiated requests.** Responses on the GET SSE stream are passed
  through and not inspected; only client→server `tools/call` is policed.
