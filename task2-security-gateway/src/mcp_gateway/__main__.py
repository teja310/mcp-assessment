"""Run the gateway: `python -m mcp_gateway`.

Environment (or a .env file in the working directory; real environment
variables take precedence; see .env.example):
  GATEWAY_DOWNSTREAM_URL  downstream MCP endpoint (default http://127.0.0.1:9000/mcp)

  Opaque tokens (optional):
  GATEWAY_TOKENS          JSON object {"<token>": "<role>"}   (or)
  GATEWAY_TOKENS_FILE     path to a file containing that JSON

  Signed JWTs (optional; one of the two keys):
  GATEWAY_JWT_SECRET            HMAC secret (>= 32 bytes) for HS256
  GATEWAY_JWT_PUBLIC_KEY_FILE   PEM public key for RS256/ES256/...
  GATEWAY_JWT_ALGORITHMS        comma-separated (default HS256 / RS256)
  GATEWAY_JWT_ISSUER            required `iss` (recommended)
  GATEWAY_JWT_AUDIENCE          required `aud` (recommended)
  GATEWAY_JWT_ROLE_CLAIM        claim holding the role (default "role")

  GATEWAY_FILTER_TOOLS_LIST     1 = hide admin_* tools from non-admins'
                                tools/list (default 0: transparent)
  GATEWAY_HOST / GATEWAY_PORT   listen address (default 127.0.0.1:8080)

At least one of opaque tokens or JWT must be configured.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from mcp_gateway.app import create_app
from mcp_gateway.auth import Authenticator, JwtVerifier, MultiAuthenticator, TokenStore


def build_authenticator(env: dict[str, str]) -> Authenticator:
    auths: list[Authenticator] = []
    if path := env.get("GATEWAY_TOKENS_FILE"):
        auths.append(TokenStore.from_json(Path(path).read_text()))
    elif raw := env.get("GATEWAY_TOKENS"):
        auths.append(TokenStore.from_json(raw))
    secret = env.get("GATEWAY_JWT_SECRET") or None
    key_file = env.get("GATEWAY_JWT_PUBLIC_KEY_FILE") or None
    if secret or key_file:
        algs = [a.strip() for a in env.get("GATEWAY_JWT_ALGORITHMS", "").split(",") if a.strip()] or None
        auths.append(JwtVerifier(
            secret=secret,
            public_key=Path(key_file).read_text() if key_file else None,
            algorithms=algs,
            issuer=env.get("GATEWAY_JWT_ISSUER") or None,
            audience=env.get("GATEWAY_JWT_AUDIENCE") or None,
            role_claim=env.get("GATEWAY_JWT_ROLE_CLAIM") or "role",
        ))
    return MultiAuthenticator(auths)  # raises if nothing is configured


def main() -> int:
    load_dotenv(".env", override=False)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = dict(os.environ)
    try:
        auth = build_authenticator(env)
    except ValueError as exc:
        print(f"error: {exc}. Set GATEWAY_TOKENS / GATEWAY_TOKENS_FILE and/or GATEWAY_JWT_SECRET / "
              "GATEWAY_JWT_PUBLIC_KEY_FILE.", file=sys.stderr)
        return 2
    if (env.get("GATEWAY_JWT_SECRET") or env.get("GATEWAY_JWT_PUBLIC_KEY_FILE")) and not env.get("GATEWAY_JWT_AUDIENCE"):
        logging.getLogger("mcp_gateway").warning("GATEWAY_JWT_AUDIENCE is not set; set it so tokens minted for "
                                                 "other services are rejected")
    app = create_app(
        env.get("GATEWAY_DOWNSTREAM_URL", "http://127.0.0.1:9000/mcp"),
        auth,
        filter_tools_list=env.get("GATEWAY_FILTER_TOOLS_LIST", "0").lower() in ("1", "true", "yes"),
    )
    uvicorn.run(
        app,
        host=env.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(env.get("GATEWAY_PORT", "8080")),
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
