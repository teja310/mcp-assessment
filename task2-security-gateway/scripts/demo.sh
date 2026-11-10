#!/usr/bin/env bash
# Demo: mock downstream MCP server + gateway, then four requests.
# Usage: scripts/demo.sh   (run from the repo root after installing)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
DOWN_PORT=${DOWN_PORT:-9000}; GW_PORT=${GW_PORT:-8080}
pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
trap cleanup EXIT
wait_port() { for _ in $(seq 100); do curl -s -o /dev/null "http://127.0.0.1:$1/" && return; sleep 0.1; done; echo "port $1 not up" >&2; exit 1; }

"$PY" -c "import uvicorn; from mcp_gateway.mock_downstream import create_mock_app; uvicorn.run(create_mock_app(), host='127.0.0.1', port=$DOWN_PORT, log_level='warning')" &
pids+=($!)
GATEWAY_DOWNSTREAM_URL="http://127.0.0.1:$DOWN_PORT/mcp" GATEWAY_PORT=$GW_PORT \
GATEWAY_TOKENS='{"admin-token-123":"admin","viewer-token-456":"viewer"}' "$PY" -m mcp_gateway >/dev/null 2>&1 &
pids+=($!)
wait_port "$DOWN_PORT"; wait_port "$GW_PORT"

rpc() { # token, json
  curl -s "http://127.0.0.1:$GW_PORT/mcp" -H 'content-type: application/json' ${1:+-H "Authorization: Bearer $1"} -d "$2" -w '  [HTTP %{http_code}]\n'
}
echo "== viewer: tools/list (forwarded transparently)"; rpc viewer-token-456 '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | cut -c1-160
echo "== viewer: call admin_reset_key (blocked at the gateway)"; rpc viewer-token-456 '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"k1"}}}'
echo "== admin: call admin_reset_key (allowed)"; rpc admin-token-123 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"k1"}}}'
echo "== no token"; rpc "" '{"jsonrpc":"2.0","id":4,"method":"tools/list"}'
