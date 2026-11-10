"""Run the router: `python -m llm_router`.

Environment (defaults point at the bundled mock providers):
  ROUTER_DB_PATH          SQLite file (default ./router.db)
  TOKENS_PER_MINUTE       per-tenant budget (default 50000)
  PRIMARY_BASE_URL        default http://127.0.0.1:9201/v1
  PRIMARY_API_KEY / PRIMARY_MODEL
  PRIMARY_TIMEOUT_MS      default 3000
  SECONDARY_BASE_URL      default http://127.0.0.1:9202/v1
  SECONDARY_API_KEY / SECONDARY_MODEL
  SECONDARY_TIMEOUT_MS    default 10000
  ROUTER_API_KEYS         comma-separated tenant keys; unknown keys get 401.
                          Unset = any bearer key is a tenant (warning logged)
  TOKENIZER               auto (default: tiktoken if available, else chars/4)
                          | tiktoken | chars
  EXTRA_RESERVE_TOKENS    added to every pre-flight estimate (default 0);
                          set for reasoning models, whose hidden reasoning
                          tokens are billed but not capped by max_tokens
  GATEWAY_HOST / GATEWAY_PORT   default 127.0.0.1:8082

Any of these may also be put in a .env file in the working directory
(see .env.example); real environment variables take precedence.
"""

from __future__ import annotations

import logging
import os
import sys

import httpx
import uvicorn
from dotenv import load_dotenv

from llm_router.app import create_app
from llm_router.limiter import SqliteTokenLimiter
from llm_router.router import ModelRouter, Provider
from llm_router.tokens import make_counter


def _provider(prefix: str, name: str, default_url: str) -> Provider:
    return Provider(
        name=name,
        base_url=os.environ.get(f"{prefix}_BASE_URL", default_url),
        api_key=os.environ.get(f"{prefix}_API_KEY"),
        model=os.environ.get(f"{prefix}_MODEL"),
    )


def main() -> int:
    # Values from ./.env fill in anything not already set in the real environment.
    load_dotenv(".env", override=False)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = os.environ.get
    log = logging.getLogger("llm_router")
    tokenizer, count_tokens = make_counter(env("TOKENIZER", "auto"))
    log.info("token estimate: %s", tokenizer)
    api_keys = [k.strip() for k in env("ROUTER_API_KEYS", "").split(",") if k.strip()]
    if not api_keys:
        log.warning("ROUTER_API_KEYS is not set: any bearer key is accepted as a tenant")
    limiter = SqliteTokenLimiter(env("ROUTER_DB_PATH", "router.db"), limit=int(env("TOKENS_PER_MINUTE", "50000")))
    router = ModelRouter(
        limiter,
        _provider("PRIMARY", "primary", "http://127.0.0.1:9201/v1"),
        _provider("SECONDARY", "secondary", "http://127.0.0.1:9202/v1"),
        httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)),
        primary_timeout=int(env("PRIMARY_TIMEOUT_MS", "3000")) / 1000,
        secondary_timeout=int(env("SECONDARY_TIMEOUT_MS", "10000")) / 1000,
        extra_reserve_tokens=int(env("EXTRA_RESERVE_TOKENS", "0")),
        count_tokens=count_tokens,
    )
    uvicorn.run(create_app(router, api_keys), host=env("GATEWAY_HOST", "127.0.0.1"),
                port=int(env("GATEWAY_PORT", "8082")), log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
