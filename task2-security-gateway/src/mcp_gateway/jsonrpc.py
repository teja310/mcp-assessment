"""JSON-RPC 2.0 wire-format parsing for the gateway.

The gateway makes an authorization decision based on what it parsed, then
forwards the *raw bytes* downstream. That is only safe if the downstream
server is guaranteed to read the same message the gateway read, so parsing
here is deliberately strict:

* Duplicate object keys are rejected. Otherwise
  `{"params": {"name": "safe_tool", "name": "admin_reset_key"}}` would be
  authorized on one reading and executed on another (parser differential).
* Batches (top-level arrays) are rejected. MCP 2025-06-18 dropped JSON-RPC
  batching, and a batch would otherwise need every element authorized.
* The envelope must be a proper JSON-RPC 2.0 request, notification or
  response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Standard JSON-RPC 2.0 codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Implementation-defined server errors (-32000..-32099).
UNAUTHORIZED = -32001  # required by the task spec: "Unauthorized Tool Call"
DOWNSTREAM_UNAVAILABLE = -32002
UNAUTHENTICATED = -32003  # missing/invalid credentials (HTTP 401); distinct from -32001

_MISSING = object()


class JsonRpcError(Exception):
    """Raised while parsing; carries the JSON-RPC error to send back."""

    def __init__(self, code: int, message: str, req_id: Any = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.req_id = req_id
        self.data = data


@dataclass(frozen=True)
class Message:
    """A validated JSON-RPC message. `id` is `_MISSING` for notifications."""

    raw: dict[str, Any]
    method: str | None
    id: Any

    @property
    def is_notification(self) -> bool:
        return self.method is not None and self.id is _MISSING

    @property
    def response_id(self) -> Any:
        return None if self.id is _MISSING else self.id


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _valid_id(value: Any) -> bool:
    # JSON-RPC: String, Number or Null. bool is an int subclass in Python;
    # exclude it explicitly. Floats are allowed by the spec but SHOULD NOT
    # be used; rejecting them avoids precision-mismatch games with ids.
    return value is None or (isinstance(value, (str, int)) and not isinstance(value, bool))


def parse_message(body: bytes) -> Message:
    try:
        text = body.decode("utf-8")
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        # json.JSONDecodeError is a ValueError, as is our duplicate-key error.
        raise JsonRpcError(PARSE_ERROR, "Parse error", data={"detail": str(exc)}) from None

    if isinstance(data, list):
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: batch requests are not supported")
    if not isinstance(data, dict):
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: expected a JSON object")

    req_id = data.get("id", _MISSING)
    echo_id = req_id if req_id is not _MISSING and _valid_id(req_id) else None

    if data.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, 'Invalid Request: "jsonrpc" must be "2.0"', echo_id)
    if req_id is not _MISSING and not _valid_id(req_id):
        raise JsonRpcError(INVALID_REQUEST, 'Invalid Request: "id" must be a string, integer or null')

    if "method" in data:
        method = data["method"]
        if not isinstance(method, str) or not method:
            raise JsonRpcError(INVALID_REQUEST, 'Invalid Request: "method" must be a non-empty string', echo_id)
        params = data.get("params", _MISSING)
        if params is not _MISSING and not isinstance(params, (dict, list)):
            raise JsonRpcError(INVALID_REQUEST, 'Invalid Request: "params" must be an object or array', echo_id)
        return Message(raw=data, method=method, id=req_id)

    # No "method": must be a response (client answering a server request,
    # e.g. sampling). Forwarded as-is; there is nothing to authorize.
    if req_id is _MISSING or (("result" in data) == ("error" in data)):
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: not a request, notification or response", echo_id)
    return Message(raw=data, method=None, id=req_id)


def error_body(code: int, message: str, req_id: Any = None, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}
