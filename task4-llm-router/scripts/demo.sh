#!/usr/bin/env bash
# Demo: hanging primary + healthy secondary + router; failover, streaming,
# rate limiting. Always uses the bundled mocks, even if .env points elsewhere.
# Usage: scripts/demo.sh   (run from the repo root after installing)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
P1=${P1:-9201}; P2=${P2:-9202}; GW=${GW:-8082}
DB=$(mktemp -d)/demo.db
pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; rm -rf "$(dirname "$DB")"; }
trap cleanup EXIT
wait_port() { for _ in $(seq 100); do curl -s -o /dev/null "http://127.0.0.1:$1/" && return; sleep 0.1; done; echo "port $1 not up" >&2; exit 1; }

"$PY" -m llm_router.mock_provider "$P1" primary hang & pids+=($!)
"$PY" -m llm_router.mock_provider "$P2" secondary & pids+=($!)
PRIMARY_BASE_URL="http://127.0.0.1:$P1/v1" PRIMARY_API_KEY="" PRIMARY_MODEL="" PRIMARY_TIMEOUT_MS=3000 \
SECONDARY_BASE_URL="http://127.0.0.1:$P2/v1" SECONDARY_API_KEY="" SECONDARY_MODEL="" \
TOKENS_PER_MINUTE=2500 ROUTER_DB_PATH="$DB" ROUTER_API_KEYS="tenant-1" TOKENIZER=chars EXTRA_RESERVE_TOKENS=0 \
GATEWAY_PORT=$GW "$PY" -m llm_router >/dev/null 2>&1 & pids+=($!)
wait_port "$P1"; wait_port "$P2"; wait_port "$GW"

req() { # body [key]: print status, route header and a short body
  curl -s "http://127.0.0.1:$GW/v1/chat/completions" -H "authorization: Bearer ${2:-tenant-1}" \
    -H 'content-type: application/json' -d "$1" -D "$HDR" -o "$OUT" -w '%{http_code} %{time_total}' > "$STAT"
  read -r code secs < "$STAT" || true  # no trailing newline from -w
  route=$(grep -i '^x-gateway-route' "$HDR" | tr -d '\r' | cut -d' ' -f2 || true)
  printf '  HTTP %s in %.1fs  route=%s  %s\n' "$code" "$secs" "${route:--}" "$(cut -c1-110 "$OUT")"
}
HDR=$(mktemp); OUT=$(mktemp); STAT=$(mktemp)
BIG='{"model":"m","max_tokens":1500,"messages":[{"role":"user","content":"hi"}]}'

echo "== primary hangs: failover after the 3000ms deadline"
req '{"model":"m","max_tokens":100,"messages":[{"role":"user","content":"hi"}]}'

echo "== streaming: also fails over, before any byte reaches the client"
curl -sN "http://127.0.0.1:$GW/v1/chat/completions" -H 'authorization: Bearer tenant-1' -H 'content-type: application/json' \
  -d '{"model":"m","stream":true,"max_tokens":100,"messages":[{"role":"user","content":"hi"}]}' |
  "$PY" -c 'import json,sys; print("  streamed text:", "".join(json.loads(l[6:])["choices"][0]["delta"].get("content") or "" for l in sys.stdin if l.startswith("data: {") and json.loads(l[6:]).get("choices")))'

echo "== budget 2500 tokens/min: two 1500-token requests at once; the second is refused while the first holds its reservation"
req "$BIG" & first=$!
sleep 0.5
req "$BIG"
wait $first

echo "== unknown tenant key (ROUTER_API_KEYS is set)"
req '{"model":"m","messages":[{"role":"user","content":"hi"}]}' minted-key
rm -f "$HDR" "$OUT" "$STAT"
