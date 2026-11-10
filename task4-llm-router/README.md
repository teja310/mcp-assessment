# llm-router

A model-routing module for an LLM gateway:

- **Token-aware sliding-window rate limiting:** 50,000 tokens/minute per
  tenant API key, state in an **on-disk SQLite** file.
- **Automatic failover:** primary → secondary when the primary returns
  **429** or doesn't finish within **3000 ms** (also on 5xx, connection
  errors and invalid JSON).
- **Standardized, sanitized errors:** no upstream bodies, hostnames or stack
  traces ever reach the client.
- **Streaming** (`"stream": true`) with failover until the first event, and
  real usage charged from the provider's final usage chunk.
- **Tenant allowlist** (`ROUTER_API_KEYS`) and a **real tokenizer**
  (`tiktoken`) for the pre-flight estimate.

## Running it

```bash
uv venv && uv pip install -e ".[dev]"
python -m llm_router.mock_provider 9201 primary hang   # primary that never answers in time
python -m llm_router.mock_provider 9202 secondary      # healthy backup
python -m llm_router                                   # router on :8082, state in ./router.db
curl -s localhost:8082/v1/chat/completions -H 'Authorization: Bearer tenant-1' \
  -H 'content-type: application/json' -d '{"model":"m","messages":[{"role":"user","content":"hi"}],"max_tokens":100}' -i
# ~3s later: HTTP 200, x-gateway-route: fallback, "hello from secondary"
```

Configuration comes from environment variables or a git-ignored `.env`
file (`cp .env.example .env`; real environment variables win). See
`.env.example` and `src/llm_router/__main__.py` for the full list
(`PRIMARY_BASE_URL`, `PRIMARY_API_KEY`, `PRIMARY_MODEL`, `PRIMARY_TIMEOUT_MS`,
`TOKENS_PER_MINUTE`, `ROUTER_DB_PATH`, …). Any OpenAI-compatible provider
(e.g. xAI at `https://api.x.ai/v1`) works as primary or secondary.

| Setting | Meaning |
|---|---|
| `ROUTER_API_KEYS` | comma-separated tenant keys. Any other key → **401 before the limiter is touched**, so a client can't mint new keys to get fresh budgets. Held as SHA-256 digests, compared in constant time. Unset = any bearer key is a tenant (warning logged at startup) |
| `TOKENIZER` | `auto` (default: `tiktoken` if installed and loadable, else chars/4 with a warning), `tiktoken` (fail at startup if unavailable), `chars` |
| `EXTRA_RESERVE_TOKENS` | headroom added to every estimate, for reasoning models (see below) |

## Rate limiter (`limiter.py`)

**Sliding log, not fixed buckets.** One row per admitted request:
`token_events(tenant, ts, tokens)`. Usage is `SUM(tokens) WHERE ts > now-60`,
which is exact. Fixed-minute buckets would let 2× the limit through across a
minute boundary.

**Reserve → reconcile → (release).**

1. Before calling any provider, reserve an estimate: prompt (~chars/4 plus
   per-message overhead) + `max_tokens`. This is what makes concurrent
   admission correct: 20 requests racing for the last 5k tokens can't all
   see "45k used" and all get in.
2. On success, the row is corrected to the provider's real
   `usage.total_tokens`, usually far below the estimate. The budget is
   charged once even when the request failed over.

   **Reasoning models are the exception.** Their hidden reasoning tokens
   are billed in `total_tokens` but are not capped by `max_tokens`. In a
   live xAI run, `grok-4.3` with `max_tokens: 50` reported **335** tokens.
   The books are still right after reconciliation, but the pre-flight check
   under-reserved, so one request can push a tenant over its budget.
   `EXTRA_RESERVE_TOKENS` (default 0) adds headroom to every estimate for
   such deployments.
3. On failure, a client-error rejection, or **cancellation** (client gone),
   the row is deleted: no tokens were consumed. The release runs in a
   `finally` under `asyncio.shield`, so a cancellation can't interrupt it.

**Atomicity.** Check-and-reserve runs in `BEGIN IMMEDIATE`, which takes
SQLite's write lock before summing. That serializes admission across threads
**and across processes** sharing the file (multiple gateway workers). WAL
mode plus `busy_timeout` make contenders wait instead of erroring.

**Eviction.** Rows with `ts <= now - window` are physically deleted in the
same transaction, for all tenants (indexed on `ts`). The table never holds
more than about one minute of traffic, and idle tenants leave nothing behind.

**Responses.** Over budget → **429** with `Retry-After` computed as the
moment enough of the oldest rows expire, so retrying then succeeds. A single
request bigger than the whole budget → **413**, because retrying could never
help.

API keys are stored as SHA-256 digests; the raw key never touches disk
(tested by scanning the DB and WAL files).

## Failover (`router.py`)

```
reserve ─▶ primary (deadline 3.0s) ──200──▶ commit usage ─▶ 200 (x-gateway-route: primary)
              │ 429 / timeout / 5xx / conn error / bad JSON
              ▼
           secondary (deadline 10s) ──200──▶ commit usage ─▶ 200 (x-gateway-route: fallback)
              │ fails
              ▼
           release ─▶ 503 upstream_unavailable

primary 400/404/422 ─▶ release ─▶ 400 invalid_request (no failover: the request itself is bad)
primary 401/403     ─▶ release ─▶ 502 upstream_error  (our provider credentials, not the client's fault)
```

**Timeout race handling.** `asyncio.timeout()` wraps the *whole* provider
call: connect, send, headers **and body**. httpx's own timeouts are
per-phase, so a provider that sends headers and then trickles a byte every
0.5s would never trip them; the whole-call deadline does. When it fires, the
in-flight request is cancelled and the connection dropped, so a late primary
answer can never be used or double-charged. The test suite checks this
(`completed == 0` long after the deadline).

## Streaming

```
reserve ─▶ primary: connect + headers + FIRST EVENT within the deadline
              │ 429 / 5xx / timeout / conn error / garbage first event ─▶ secondary (same rule)
              ▼ first event arrived: committed to this provider
           relay events as they arrive ─▶ [DONE] ─▶ commit real usage
              │ provider breaks mid-stream
              ▼
           error event (standard payload), no failover, charge the estimate
```

- **Failover only before the first byte.** Once the client has received
  part of an answer, switching provider would splice two different answers
  together, so a mid-stream failure ends the stream with a standardized
  `data: {"error": {...}}` event instead.
- **The deadline covers the first event, not just the headers.** A provider
  that sends `200` and then stalls is failed over. This is tested over real
  sockets, because in-process transports buffer whole responses and can't
  show the difference.
- **Usage.** The router always sends `stream_options.include_usage` and
  charges the `total_tokens` from the final chunk. If the client didn't ask
  for usage, that usage-only chunk (and any `usage` field) is removed, so
  the client sees what it asked for.
- **Disconnects.** If the client goes away or the stream breaks, the
  estimate is charged (the provider has most likely billed already) and the
  upstream connection is closed. The commit is shielded from cancellation.
- Mid-stream stalls are bounded by the HTTP client's read timeout (30s).

## Token estimate (`tokens.py`)

Before each call the router reserves prompt tokens (message contents plus
tool definitions, which providers bill as prompt) + 4 per message +
`max_tokens`. With `tiktoken` (`o200k_base`) installed, the prompt is
tokenized. Otherwise it falls back to chars/4, which is low exactly where it
hurts:

| Text | tiktoken | chars/4 |
|---|---|---|
| `你好世界` × 50 | 100 | 50 (CJK: 2× under) |
| `{"a": [1,2,3]}` × 20 | 200 | 70 (JSON: ~3× under) |
| `x` × 400 | 50 | 100 (over) |

`o200k_base` is OpenAI's tokenizer. For other providers it's a closer
approximation, not an exact count. The reservation is corrected to the
provider's reported usage after every call either way.

**Standard error payload.** Every error is one of the fixed entries in
`ERRORS`:

```json
{"error": {"type": "rate_limit_exceeded", "message": "Token rate limit exceeded for this API key.", "request_id": "…"}}
```

Types: `invalid_request` 400, `authentication_error` 401, `request_too_large`
413, `rate_limit_exceeded` 429, `upstream_error` 502, `upstream_unavailable`
503, `internal_error` 500. Upstream details are logged to stderr with the
`request_id` for correlation. The HTTP layer has a catch-all, so even an
unexpected exception returns the standard 500 payload.

## Demo and CI

`scripts/demo.sh` starts everything it needs on localhost, runs a short
scripted demonstration, and cleans up after itself. It always uses the bundled mock
providers, even when `.env` points at real ones.
`.github/workflows/tests.yml` runs the test suite and the demo on Ubuntu
and macOS with Python 3.12.

## Tests

```bash
python -m pytest -q    # 58 passed, ~14s (some tests use real sockets and the real 3000ms deadline)
```

Highlights:

- Exact admission under concurrency: 60 threads over 6 connections → exactly
  33 of 1,500 tokens. **4 OS processes** → exactly 50 of 1,000 tokens.
- A **deterministic race test**: SQLite's trace callback injects a competing
  reservation exactly between one connection's `SUM` and its `INSERT`.
- Sliding-window eviction and `Retry-After` accuracy with an injected clock;
  persistence across restart; no plaintext keys on disk.
- Failover on 429 / hang / 500 / garbage / connection refused; no failover
  on 400; the **spec's 3000ms default over real HTTP** for both a hanging
  provider and a headers-then-trickle provider (fails over at 2.95–3.6s).
- Budget charged once on failover, refunded on failure and on cancellation;
  20 concurrent requests → exactly 9 admitted.
- Every error checked for leaks (`Traceback`, internal paths, org IDs,
  provider hostnames, exception names).

**Mutation check:**

| Mutation | Result |
|---|---|
| httpx per-phase timeout instead of whole-call `asyncio.timeout` | 4 failed (incl. trickle-body test) |
| no transaction around check-and-reserve | caught 5/5 by the deterministic race test |
| reservation not released on failure/cancel | 3 failed |
| no eviction of expired rows | 3 failed |
| fail over on every non-200, including 400 | 1 failed |
| `EXTRA_RESERVE_TOKENS` ignored | 1 failed |
| stream deadline covers headers only, not the first event | 1 failed (real-socket test) |
| usage-only chunk forwarded to a client that didn't ask | 1 failed |
| streamed usage ignored (always charge the estimate) | 2 failed |
| relay delays each event (buffering) | 6 failed |
| client disconnect releases instead of charging | 4 failed |
| mid-stream break without an error event | 1 failed |
| tenant allowlist ignored | 1 failed |
| router ignores the configured tokenizer | 1 failed |

Two of these first **survived**. The first-event deadline mutant survived
because the in-process transport buffers the provider's whole response, so
a real-socket test was added. The usage-chunk mutant survived because the
test only checked for a `usage` key, so it now also asserts that no empty
chunk is relayed.
| deferred `BEGIN` instead of `BEGIN IMMEDIATE` | passes. **Equivalent here:** the first statement is the eviction `DELETE`, which takes the write lock immediately. `IMMEDIATE` is kept so correctness doesn't depend on statement order. |

### Live run against a real provider (xAI)

Run on 2026-11-09 with xAI (`https://api.x.ai/v1`, model `grok-4.3`) as
primary and the bundled mock as secondary, configured through `.env`:

| Scenario | Result |
|---|---|
| Normal call, `PRIMARY_TIMEOUT_MS=30000` | 200 from `grok-4.3` in 2.9s, route `primary` |
| Normal call, spec default 3000ms | 200 from `grok-4.3` in 2.7s: **inside the deadline, but only just** |
| Primary unreachable | warning logged, 200 from the secondary |
| `TOKENS_PER_MINUTE=200` | first request 200 (real usage 335 committed), the next ones 429 with `Retry-After: 58` |
| Restart on the same DB | still 429: the budget survives a restart |

**Deadline note:** 3000ms is the spec's default and stays the default. A
reasoning model such as `grok-4.3` needs 2.7–5.5s before its first token,
so with that model as primary, many longer prompts will fail over. For such
a deployment, raise `PRIMARY_TIMEOUT_MS` in `.env`.

**Live streaming run (xAI, 2026-11-09).** Three streamed requests to
`grok-4.3` through the router, with `ROUTER_API_KEYS` set and `TOKENIZER=auto`
(which resolved to `tiktoken:o200k_base`). Each was served by the primary,
with the first event after 1.0–1.25s, comfortably inside the 3000ms deadline.
Each committed Grok's real usage (319–370 tokens), and a made-up key got
401 `authentication_error`.

## Limits / next steps

- **Tenant keys are a static list.** Rotating them means a restart; a
  database or the Task 2 gateway in front would allow live changes.
- **Streaming retries.** A stream that breaks after the first byte is not
  retried. Resuming would need provider-side support.
- **Scale.** SQLite serializes all writers on one host. That's fine for a
  single gateway host; a multi-host deployment would move the same algorithm
  to Redis (sorted set + Lua) or Postgres.
