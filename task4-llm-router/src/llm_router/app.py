"""HTTP surface: POST /v1/chat/completions, tenant identified by its API key.

If `api_keys` is given, only those keys are tenants: anything else gets 401
before the limiter is touched. Without it, any bearer key is a tenant, and a
client could mint new keys to get fresh budgets, so production deployments
should set it (or put an authenticating gateway in front)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Iterable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from llm_router.router import ModelRouter, RouterResponse, RouterStream, error_response

logger = logging.getLogger("llm_router.app")


def _json(r: RouterResponse) -> JSONResponse:
    return JSONResponse(r.body, status_code=r.status, headers=r.headers)


def create_app(router: ModelRouter, api_keys: Iterable[str] | None = None) -> Starlette:
    allowed = [hashlib.sha256(k.encode()).digest() for k in (api_keys or []) if k]

    def known(key: str) -> bool:
        if not allowed:
            return True
        digest = hashlib.sha256(key.encode()).digest()
        return sum(hmac.compare_digest(digest, d) for d in allowed) > 0  # constant time over all keys

    async def completions(request: Request) -> Response:
        rid = uuid.uuid4().hex
        try:
            scheme, _, key = request.headers.get("authorization", "").partition(" ")
            key = key.strip()
            if scheme.lower() != "bearer" or not key or not known(key):
                return _json(error_response("authentication_error", rid,
                                            {"www-authenticate": "Bearer"}))
            try:
                body = json.loads(await request.body())
            except ValueError:
                return _json(error_response("invalid_request", rid, message="Request body must be JSON."))
            result = await router.complete(key, body)
            if isinstance(result, RouterStream):
                return StreamingResponse(result.body, status_code=result.status, headers=result.headers,
                                         media_type="text/event-stream")
            return _json(result)
        except Exception:
            # Last line of defence: whatever went wrong, the client gets the
            # standard payload; the traceback goes to the log only.
            logger.exception("[%s] unhandled error", rid)
            return _json(error_response("internal_error", rid))

    return Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])
