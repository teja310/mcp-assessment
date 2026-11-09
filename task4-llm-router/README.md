# llm-router

A model-routing module for an LLM gateway:

- **Token-aware sliding-window rate limiting:** 50,000 tokens/minute per
  tenant API key, state in an **on-disk SQLite** file.
- **Automatic failover:** primary → secondary when the primary returns
  **429** or doesn't finish within **3000 ms** (also on 5xx, connection
  errors and invalid JSON).
- **Standardized, sanitized errors:** no upstream bodies, hostnames or stack
  traces ever reach the client.

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

## Tests

```bash
python -m pytest -q    # 38 passed, ~10s (two tests use the real 3000ms deadline)
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

## Limits / next steps

- **Streaming** (`stream: true`) is rejected with 400. Failover mid-stream
  needs a "no bytes sent yet" rule.
- **Tenants.** Any bearer key is a tenant; authentication belongs in front
  (e.g. the Task 2 gateway). Otherwise rotating keys would bypass limits.
- **Estimation** is chars/4. A real tokenizer (tiktoken) would tighten the
  reservation, but reconciliation already corrects the books after each call.
- **Scale.** SQLite serializes all writers on one host. That's fine for a
  single gateway host; a multi-host deployment would move the same algorithm
  to Redis (sorted set + Lua) or Postgres.
