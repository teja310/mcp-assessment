"""HTTP surface: POST /v1/chat/completions, tenant identified by its API key."""

from __future__ import annotations

import json
import logging
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from llm_router.router import ModelRouter, RouterResponse, error_response

logger = logging.getLogger("llm_router.app")


def _json(r: RouterResponse) -> JSONResponse:
    return JSONResponse(r.body, status_code=r.status, headers=r.headers)


def create_app(router: ModelRouter) -> Starlette:
    async def completions(request: Request) -> JSONResponse:
        rid = uuid.uuid4().hex
        try:
            scheme, _, key = request.headers.get("authorization", "").partition(" ")
            key = key.strip()
            if scheme.lower() != "bearer" or not key:
                return _json(error_response("authentication_error", rid,
                                            {"www-authenticate": "Bearer"}))
            try:
                body = json.loads(await request.body())
            except ValueError:
                return _json(error_response("invalid_request", rid, message="Request body must be JSON."))
            return _json(await router.complete(key, body))
        except Exception:
            # Last line of defence: whatever went wrong, the client gets the
            # standard payload; the traceback goes to the log only.
            logger.exception("[%s] unhandled error", rid)
            return _json(error_response("internal_error", rid))

    return Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])
