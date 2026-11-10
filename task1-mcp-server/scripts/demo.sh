#!/usr/bin/env bash
# Demo: drive the stdio MCP server with raw JSON-RPC, one message per line.
# Usage: scripts/demo.sh   (run from the repo root after installing)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}

send() {
  cat <<'MSGS'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"demo","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-00001"}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"trigger_refund","arguments":{"customer_id":"CUST-00001","amount":25.5,"reason":"Damaged item on delivery"}}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-99999"}}}
{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"trigger_refund","arguments":{"customer_id":"CUST-00001","amount":-5,"reason":"x"}}}
{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"not_a_tool","arguments":{}}}
MSGS
}

FORMAT='
import json, sys
labels = {1: "initialize", 2: "tools/list", 3: "get_customer_record", 4: "trigger_refund",
          5: "unknown customer", 6: "invalid refund args", 7: "unknown tool"}
for line in sys.stdin:
    m = json.loads(line)
    body = m.get("error") or m.get("result")
    if m["id"] == 1:
        body = body["serverInfo"]
    elif m["id"] == 2:
        body = [t["name"] for t in body["tools"]]
    elif "content" in body:
        body = body.get("structuredContent") or body["content"][0]["text"]
    print("[" + labels[m["id"]] + "]", json.dumps(body)[:240])
'
echo "== server stdout (pure JSON-RPC; logs go to stderr) =="
send | "$PY" -m customer_mcp 2>/dev/null | "$PY" -c "$FORMAT"
