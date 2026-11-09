"""Bearer-token -> role resolution and the tool-call authorization policy."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

ADMIN_ROLE = "admin"
ADMIN_TOOL_PREFIX = "admin_"


@dataclass(frozen=True)
class Principal:
    role: str
    token_fingerprint: str  # short hash for audit logs; never the token itself


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    """Maps bearer tokens to roles.

    Tokens are kept only as SHA-256 digests, so the lookup is a dict probe
    on a hash rather than a byte-by-byte comparison of the secret, and the
    raw tokens are not kept in memory after loading.
    """

    def __init__(self, tokens: Mapping[str, str]) -> None:
        if not tokens:
            raise ValueError("no tokens configured; refusing to start an auth gateway that accepts nobody")
        self._roles: dict[str, str] = {}
        for token, role in tokens.items():
            if not isinstance(token, str) or not token or not isinstance(role, str) or not role:
                raise ValueError("token and role must be non-empty strings")
            self._roles[_digest(token)] = role

    @classmethod
    def from_json(cls, text: str) -> TokenStore:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError('token config must be a JSON object: {"<token>": "<role>"}')
        return cls(data)

    def authenticate(self, authorization: str | None) -> Principal | None:
        """Return the principal for an `Authorization` header, or None."""
        if not authorization:
            return None
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        digest = _digest(token)
        role = self._roles.get(digest)
        if role is None:
            return None
        return Principal(role=role, token_fingerprint=digest[:12])


def normalize_tool_name(name: str) -> str:
    """Canonical form used ONLY for the policy check (the original name is
    what gets forwarded). NFKC folds full-width/compatibility characters
    (e.g. "ａｄｍｉｎ_"), casefold handles "ADMIN_", strip handles padding --
    so a downstream that is lenient about any of these can't be used to
    sneak an admin tool past the prefix check."""
    return unicodedata.normalize("NFKC", name).casefold().strip()


def is_admin_tool(name: str) -> bool:
    return normalize_tool_name(name).startswith(ADMIN_TOOL_PREFIX)


def may_call_tool(principal: Principal, tool_name: str) -> bool:
    return not is_admin_tool(tool_name) or principal.role == ADMIN_ROLE
