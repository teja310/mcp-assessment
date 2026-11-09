"""Run the gateway: `python -m redact_gateway`.

Environment:
  LLM_UPSTREAM_BASE_URL   provider base URL (default http://127.0.0.1:9100/v1, the mock)
  LLM_UPSTREAM_API_KEY    provider API key (optional; sent as Bearer)
  GATEWAY_HOST / GATEWAY_PORT   listen address (default 127.0.0.1:8081)

Any of these may also be put in a .env file in the working directory
(see .env.example); real environment variables take precedence.
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn
from dotenv import load_dotenv

from redact_gateway.app import create_app


def main() -> int:
    # Values from ./.env fill in anything not already set in the real environment.
    load_dotenv(".env", override=False)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(
        os.environ.get("LLM_UPSTREAM_BASE_URL", "http://127.0.0.1:9100/v1"),
        os.environ.get("LLM_UPSTREAM_API_KEY"),
    )
    uvicorn.run(app, host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
                port=int(os.environ.get("GATEWAY_PORT", "8081")), log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
