# redact-gateway

An OpenAI-compatible LLM gateway endpoint (`POST /v1/chat/completions`)
that forwards requests to a provider and streams the response back,
redacting **emails, SSNs and credit-card numbers** to `[REDACTED]` in real
time, including PII that the provider splits across chunk boundaries.

## Running it

```bash
uv venv && uv pip install -e ".[dev]"
python -m redact_gateway.mock_provider          # mock provider on :9100
python -m redact_gateway                        # gateway on :8081
curl -N localhost:8081/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"m","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

For a real provider (any OpenAI-compatible API, e.g. OpenAI or xAI):
`cp .env.example .env`, fill in `LLM_UPSTREAM_BASE_URL` and
`LLM_UPSTREAM_API_KEY`, then `python -m redact_gateway`. `.env` is
git-ignored, and real environment variables override it. The gateway holds
the provider key; the client's own `Authorization` header is not forwarded.

## How the streaming redaction works (`redactor.py`)

**The problem.** Deltas arrive like `"jo"`, `"hn.doe@exa"`, `"mple.com"`.
Redacting each delta separately leaks. Buffering the whole response destroys
time to first token (TTFT) and grows memory with response length.

**The approach: release up to the last hard delimiter.** Every pattern is
built only from *candidate* characters (letters, digits, `. _ % + - @`), plus
single spaces between digits for cards like `4111 1111 1111 1111`. So no
match can span any other character: spaces after a non-digit, `,;:!?()"'`,
newlines and so on. When a delta arrives:

1. Append it to a small per-stream buffer.
2. Scan back to the last hard delimiter. Everything before it is final:
   regex-match it, redact it, emit it.
3. Hold only the trailing run, which in prose is the last partial word.

```
feed("Hello wor")      -> "Hello "              holds "wor"
feed("ld, mail john.d") -> "world, mail "        holds "john.d"
feed("oe@exa")          -> ""                    holds "john.doe@exa"
feed("mple.com now")    -> "[REDACTED] "         holds "now"
```

**Memory bound.** If the held run exceeds `max_hold` (2048 characters; only
an unbroken token or a long space-separated digit run gets there), it is
force-flushed. The last `tail` (512 characters, which is at least the
longest possible match of 409) are kept, and the cut never
lands inside a match. Forced cuts only remove *left* context, and the
patterns use negative lookbehinds, so a forced cut can over-redact but never
leak.

**Detection**

| Type | Rule |
|---|---|
| Email | `local@domain.tld`, TLD of 2–24 letters (`user@localhost` is not matched). Every part is length-bounded (local ≤ 64, label ≤ 63, ≤ 5 labels), so matching is **linear time** (see below) |
| SSN | `ddd-dd-dddd`, not glued to further digits or dashes |
| Card | 13–19 contiguous digits, `4-4-4-4(-3)` or Amex `4-6-5` with one consistent space/dash separator, **and Luhn-valid** |

Luhn and the strict layouts keep order numbers and phone numbers
(`555-123-4567`) from being redacted. They also stop neighbouring numbers
(`9 4111 1111 1111 1111`) from being merged into one Luhn-invalid run that
would hide a real card. That last one was a bug a test caught during
development.

**Measured** (1.2 MB of PII-dense text in 303k 4-character deltas, one core):
about 2 µs per delta, at most 20 characters held, 17 KiB peak allocation.

## The proxy (`app.py`)

- Upstream SSE is consumed line by line (`aiter_lines`). Each `data:` event
  is parsed, and each `choices[i].delta.content` goes through **that
  choice's own** redactor, so `n > 1` streams never mix. The event is
  re-serialized and yielded immediately. `id`/`model`/`role`/`finish_reason`/`usage`
  are preserved.
- On `finish_reason`, that choice's held text is flushed into the same chunk.
- On `[DONE]` without a `finish_reason`, held text is emitted in a synthetic
  chunk before `[DONE]`.
- On an abrupt upstream end, held text is flushed (redacted) and a
  sanitized `{"error": ...}` event is sent.
- A non-JSON `data:` payload is redacted as text rather than passed through
  blindly.
- Non-streaming responses are redacted whole.
- Upstream errors become `{"error":{"type","message","request_id"}}`:
  429 → 429 `rate_limited`, other 4xx → 400, 5xx → 502, connect failure → 502.
  Provider bodies, org IDs and tracebacks are logged, never returned.

## Tests

```bash
python -m pytest -q    # 67 passed
```

- **Correctness against a reference.** For 8 PII-dense samples, the stream
  output must equal `redact_text(full_text)` for **every** single split
  point, char-by-char feeding, and 300 random chunkings each. No emitted
  piece may contain raw PII.
- **TTFT, over real sockets.** `test_first_tokens_reach_client_before_provider_finishes`
  runs the provider and gateway in uvicorn. The provider *blocks* after its
  first pieces until the client has received them through the gateway, so a
  buffering gateway deadlocks and the test fails. (`httpx.ASGITransport`
  buffers whole bodies, so it can't test this.)
- Memory bound, forced-flush across the cut point, multi-choice,
  missing `finish_reason`, truncated stream, sanitized errors.

Mutation check, run against deliberately broken versions:

| Mutation | Result |
|---|---|
| redact each delta independently (`redact_text(delta)`) | 4 failed (PII leaked) |
| collect the whole response, then send | 1 failed (TTFT test) |
| original unbounded email regex (`[...]+@...`) | 2 failed: 200k unbroken chars took **30.2s** |

**ReDoS fix.** The first version's email pattern used unbounded `+`/`*`.
On an unbroken run of candidate characters, every start position scanned to
the end of the run before failing, which is quadratic: 20k chars took 0.30s,
40k took 1.18s, 200k took 30s. The streaming path was capped by `max_hold`,
but the non-streaming path wasn't. With RFC length bounds on every part, the
same inputs take 0.006s (40k), 0.059s (400k) and 0.59s (4M), which is linear.
`test_matching_is_linear_time_on_adversarial_input` covers six adversarial
shapes in both paths.

### Live run against a real provider (xAI)

Run on 2026-11-09 with `LLM_UPSTREAM_BASE_URL=https://api.x.ai/v1` and
model `grok-4.3`, configured through `.env`. The model was asked to repeat
`Contact jane.doe@example.com or ssn 123-45-6789, card 4111 1111 1111 1111.
Thanks.` What the client received through the gateway:

```
Contact [REDACTED] or ssn [REDACTED], card [REDACTED]. Thanks.
```

All three PII items were redacted across real token boundaries, the stream
ended with `[DONE]`, and nothing leaked. First text arrived at ~6.7s, while
calling xAI directly gave 4.9–5.5s (the reasoning model thinks before its
first token). The gateway's added latency is small next to the model's own.

## Limits / next steps

- Holdback is one partial word, and a space-separated run of numbers is
  held until it ends or hits `max_hold`.
- `tool_calls[].function.arguments` deltas are passed through unredacted.
  Arguments are JSON fragments and would need a second redactor per
  tool-call index.
- Regex detection: no names, addresses or 9-digit SSNs without dashes.
  The redactor takes one compiled pattern, so adding types is local to
  `PII_PATTERN`.
