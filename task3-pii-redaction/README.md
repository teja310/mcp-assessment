# redact-gateway

An LLM gateway that forwards requests to a provider and streams the
response back, redacting **emails, SSNs, credit-card numbers and phone
numbers** to `[REDACTED]` in real time, including PII that the provider
splits across chunk boundaries.

| Endpoint | Format | Redacted |
|---|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions (also xAI, and other compatible APIs) | **every** string field of a delta/message except `role` (`content`, `reasoning_content`, `refusal`, …), `tool_calls[].function.arguments`, legacy `function_call.arguments` |
| `POST /v1/messages` | Anthropic Messages | `text_delta`, `thinking_delta`, `input_json_delta` / `tool_use.input`, and every non-opaque field of any other block or delta (citations, web search results, …) |

**Default-deny.** Fields are redacted unless they are known protocol
metadata (`role`, ids, types, signatures, encrypted blobs). This rule came
from a live xAI run: Grok streams its visible reasoning in
`delta.reasoning_content`, which repeated the user's email, phone, SSN and
card number. A gateway that only redacted `content` leaked all of them.

Streaming and non-streaming responses are both handled.

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

| Setting | Meaning |
|---|---|
| `LLM_UPSTREAM_BASE_URL` / `LLM_UPSTREAM_API_KEY` | OpenAI-compatible provider |
| `ANTHROPIC_UPSTREAM_BASE_URL` / `ANTHROPIC_UPSTREAM_API_KEY` | provider for `/v1/messages` (default: same base URL). Sent as `x-api-key`; the client's `anthropic-version` / `anthropic-beta` headers are passed through |
| `GATEWAY_API_KEYS` | comma-separated keys clients must send as `Authorization: Bearer` or `x-api-key`. Missing/wrong → 401 before the provider is contacted. Held as SHA-256 digests, compared in constant time, never forwarded. Unset = open (a warning is logged at startup) |

## How the streaming redaction works (`redactor.py`)

**The problem.** Deltas arrive like `"jo"`, `"hn.doe@exa"`, `"mple.com"`.
Redacting each delta separately leaks. Buffering the whole response destroys
time to first token (TTFT) and grows memory with response length.

**The approach: release up to the last hard delimiter.** Every pattern is
built only from *candidate* characters (letters, digits, `. _ % + - @ ( )`),
plus single spaces after a digit or `)` for numbers like
`4111 1111 1111 1111` or `(555) 123-4567`. So no match can span any other
character: spaces after a letter, `,;:!?"'`, newlines and so on. When a delta arrives:

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
| SSN without dashes | `123456789` or `123 45 6789`, **only when** `SSN`, `social security` or `soc. sec.` appears within the 40 characters before it, including text already sent in earlier chunks. Plain 9-digit numbers (order numbers, IDs) are left alone |
| Phone (North America) | `(555) 123-4567`, `555-123-4567`, `555.123.4567`, optional `+1`. A separator or parentheses is required, so a bare 10-digit ID is not matched |
| Phone (international) | `+CC` followed by 2–5 groups, 8–15 digits in total (E.164), e.g. `+44 20 7946 0958`, `+91 98765 43210` |
| Card | 13–19 contiguous digits, `4-4-4-4(-3)` or Amex `4-6-5` with one consistent space/dash separator, **and Luhn-valid** |

Luhn and the strict layouts keep order numbers from being redacted as cards. They also stop neighbouring numbers
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
- **Tool-call arguments** get their own redactor per (choice, tool-call
  index), in **JSON mode**: the redactor tracks whether each match is inside
  a JSON string. Inside a string the PII becomes `[REDACTED]`; a bare value
  (a card number sent as a JSON number) becomes the string `"[REDACTED]"`.
  A match that starts right after an escape (`\n`, `\u0040`) keeps the
  escape. So the arguments stay valid JSON: tested for every split point.
  Held argument text is flushed into the `finish_reason` chunk.
- **Anthropic stream:** `event:` names are kept; each content block has its
  own redactor (text, thinking, or JSON mode for tool input). Held text is
  emitted as one extra `content_block_delta` **before** that block's
  `content_block_stop`. `ping`, `message_delta`, `signature_delta` and
  `citations_delta` pass through. An abrupt end emits an Anthropic-style
  `event: error`.
- Upstream errors become `{"error":{"type","message","request_id"}}`:
  429 → 429 `rate_limited`, other 4xx → 400, 5xx → 502, connect failure → 502.
  Provider bodies, org IDs and tracebacks are logged, never returned.

## Demo and CI

`scripts/demo.sh` starts everything it needs on localhost, runs a short
scripted demonstration, and cleans up after itself. It always uses the bundled mock
provider, even when `.env` points at a real one.
`.github/workflows/tests.yml` runs the test suite and the demo on Ubuntu
and macOS with Python 3.12.

## Tests

```bash
python -m pytest -q    # 138 passed
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
- **New detections:** phones and keyword SSNs are in the every-split-point
  samples. False-positive cases are tested too (order numbers, bare
  10-digit IDs, dates, a keyword more than 40 characters back).
- **Tool calls (JSON mode):** redacted arguments must still `json.loads`,
  at every split point, including PII as a JSON number and after escapes.
- **Anthropic:** text and tool input redacted, event names kept, held text
  before `content_block_stop`, truncated stream, sanitized errors, provider
  key/header handling, and the real-socket TTFT test.
- **Client auth:** missing/wrong/`Basic` keys → 401 on both endpoints with
  the provider never contacted; valid keys accepted and not forwarded.
- **Default-deny:** `reasoning_content` (streamed and non-streamed), an
  unknown future delta field, `refusal`, legacy `function_call`; Anthropic
  `citations_delta`, web search results and unknown blocks are redacted,
  while `signature`, `encrypted_content` and ids are byte-for-byte intact
  at every nesting depth (a test caught nested `encrypted_content` being
  altered during development).

Mutation check, run against deliberately broken versions:

| Mutation | Result |
|---|---|
| redact each delta independently (`redact_text(delta)`) | 4 failed (PII leaked) |
| collect the whole response, then send | 1 failed (TTFT test) |
| original unbounded email regex (`[...]+@...`) | 2 failed: 200k unbroken chars took **30.2s** |
| OpenAI tool-call arguments passed through | caught |
| tool arguments redacted without JSON mode | caught (invalid JSON) |
| Anthropic: held text dropped at `content_block_stop` | caught |
| Anthropic: `input_json_delta` not redacted | caught |
| client auth always passes | caught |
| bare-SSN keyword check off | caught |
| SSN keyword context not carried across chunks | caught |
| escape before a match not preserved (JSON mode) | caught |
| space after `)` treated as hard (streaming phones) | caught |
| only `content` redacted (the original behaviour) | 2 failed (`reasoning_content` leak) |

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
ended with `[DONE]`, and nothing leaked.

**Second live run (tool calls and reasoning).** Grok was asked to "think
out loud, repeating every detail" and call a `send_record` tool with an
email, phone, keyword SSN and card number. The first attempt **leaked**:
the PII came through in `delta.reasoning_content`, a field the gateway did
not redact yet. After the default-deny fix, five runs (four streamed with
tools, one non-streamed) contained no raw PII. Each had 4–12 `[REDACTED]`
across reasoning, text and tool arguments, and the arguments were valid
JSON: `{"email":"[REDACTED]","phone":"[REDACTED]","ssn":"[REDACTED]","card":"[REDACTED]"}`. First text arrived at ~6.7s, while
calling xAI directly gave 4.9–5.5s (the reasoning model thinks before its
first token). The gateway's added latency is small next to the model's own.

## Limits / next steps

- Holdback is one partial word, and a space-separated run of numbers is
  held until it ends or hits `max_hold`.
- **Names and street addresses are not detected.** They have no reliable
  shape: "Jordan", "Paris" or "1 Main St" can't be told apart from ordinary
  words with a pattern. That needs a named-entity model (e.g. spaCy or
  Microsoft Presidio). It would slot in where `PII_PATTERN` is applied, but
  a model needs whole phrases, so it would add holdback (TTFT) and CPU per
  token. That's a product decision, not a regex change.
- JSON-escaped PII inside tool arguments (`john\u0040example.com`) is not
  decoded before matching.
- Legacy `function_call` (pre-`tool_calls`) deltas are not redacted.
