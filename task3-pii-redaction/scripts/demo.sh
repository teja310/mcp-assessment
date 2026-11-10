#!/usr/bin/env bash
# Demo: mock provider + redaction gateway; streams with PII split across chunks.
# Always uses the bundled mock, even if .env points at a real provider.
# Usage: scripts/demo.sh   (run from the repo root after installing)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
PROV_PORT=${PROV_PORT:-9100}; GW_PORT=${GW_PORT:-8081}
pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
trap cleanup EXIT
wait_port() { for _ in $(seq 100); do curl -s -o /dev/null "http://127.0.0.1:$1/" && return; sleep 0.1; done; echo "port $1 not up" >&2; exit 1; }

"$PY" -c "import uvicorn; from redact_gateway.mock_provider import create_mock_provider, DEFAULT_TOOL_PIECES as T; a = create_mock_provider(); a.state.tool_pieces = list(T); uvicorn.run(a, host='127.0.0.1', port=$PROV_PORT, log_level='warning')" &
pids+=($!)
# Explicit (even empty) values win over .env, so the demo never calls a real provider.
LLM_UPSTREAM_BASE_URL="http://127.0.0.1:$PROV_PORT/v1" LLM_UPSTREAM_API_KEY="" ANTHROPIC_UPSTREAM_BASE_URL="" \
ANTHROPIC_UPSTREAM_API_KEY="" GATEWAY_API_KEYS="demo-key" GATEWAY_PORT=$GW_PORT "$PY" -m redact_gateway >/dev/null 2>&1 &
pids+=($!)
wait_port "$PROV_PORT"; wait_port "$GW_PORT"

show() { "$PY" -c '
import json, sys
text, args = "", ""
for block in sys.stdin.read().split("\n\n"):
    for line in block.splitlines():
        if not line.startswith("data: {"): continue
        e = json.loads(line[6:])
        for c in e.get("choices", []):
            d = c.get("delta", {}); text += d.get("content") or ""
            args += "".join((t.get("function") or {}).get("arguments") or "" for t in d.get("tool_calls") or [])
        d = e.get("delta") if e.get("type") == "content_block_delta" else None
        if d: text += d.get("text", ""); args += d.get("partial_json", "")
print("  text:      ", text); print("  tool args: ", args)'; }
echo "== provider sends (split across chunks): 'Reach me at jo|hn.doe@exa|mple.com ... SSN 123-4|5-6789 ... card 4111 11|11 1111 1111'"
echo "== OpenAI format, streamed through the gateway:"
curl -sN "http://127.0.0.1:$GW_PORT/v1/chat/completions" -H 'authorization: Bearer demo-key' -H 'content-type: application/json' \
  -d '{"model":"m","stream":true,"messages":[{"role":"user","content":"hi"}]}' | show
echo "== Anthropic format, streamed through the gateway:"
curl -sN "http://127.0.0.1:$GW_PORT/v1/messages" -H 'x-api-key: demo-key' -H 'content-type: application/json' \
  -d '{"model":"m","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"hi"}]}' | show
echo "== without a gateway key:"
curl -s "http://127.0.0.1:$GW_PORT/v1/chat/completions" -H 'content-type: application/json' -d '{}' -w '  [HTTP %{http_code}]\n'
