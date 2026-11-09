"""Run the gateway: `python -m mcp_gateway`.

Environment:
  GATEWAY_DOWNSTREAM_URL  downstream MCP endpoint (default http://127.0.0.1:9000/mcp)
  GATEWAY_TOKENS          JSON object {"<token>": "<role>"}   (or)
  GATEWAY_TOKENS_FILE     path to a file containing that JSON
  GATEWAY_HOST / GATEWAY_PORT   listen address (default 127.0.0.1:8080)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

from mcp_gateway.app import create_app
from mcp_gateway.auth import TokenStore


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if path := os.environ.get("GATEWAY_TOKENS_FILE"):
        raw = Path(path).read_text()
    elif raw_env := os.environ.get("GATEWAY_TOKENS"):
        raw = raw_env
    else:
        print("error: set GATEWAY_TOKENS or GATEWAY_TOKENS_FILE", file=sys.stderr)
        return 2
    app = create_app(
        os.environ.get("GATEWAY_DOWNSTREAM_URL", "http://127.0.0.1:9000/mcp"),
        TokenStore.from_json(raw),
    )
    uvicorn.run(
        app,
        host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_PORT", "8080")),
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
