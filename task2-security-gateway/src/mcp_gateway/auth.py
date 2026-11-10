"""Bearer-token -> role resolution and the tool-call authorization policy.

Two authenticators, usable together (MultiAuthenticator):

  TokenStore   opaque tokens looked up in a static {token: role} table
  JwtVerifier  signed JWTs; the role comes from a claim

Both answer `authenticate(authorization_header) -> Principal | None`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import jwt

logger = logging.getLogger("mcp_gateway")

ADMIN_ROLE = "admin"
ADMIN_TOOL_PREFIX = "admin_"


@dataclass(frozen=True)
class Principal:
    role: str
    token_fingerprint: str  # short hash for audit logs; never the token itself
    subject: str | None = None  # JWT "sub", for audit logs


class Authenticator(Protocol):
    def authenticate(self, authorization: str | None) -> Principal | None: ...


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


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
        token = bearer_token(authorization)
        if token is None:
            return None
        digest = _digest(token)
        role = self._roles.get(digest)
        if role is None:
            return None
        return Principal(role=role, token_fingerprint=digest[:12])


_HMAC_ALGS = frozenset({"HS256", "HS384", "HS512"})
_ASYMMETRIC_ALGS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512",
                              "EdDSA"})


class JwtVerifier:
    """Verifies signed JWTs and takes the role from a claim.

    Security choices:
      - The accepted algorithms are fixed by configuration, never taken from
        the token header. A shared secret allows only HS*, a public key only
        asymmetric algorithms, so the "RS256 key used as HS256 secret"
        confusion attack and `alg: none` are impossible.
      - `exp` is required; `iss`/`aud` are required and checked when
        configured. A token carrying `aud` is rejected if no audience is
        configured (PyJWT's default), so tokens minted for another service
        aren't accepted by accident.
      - HMAC secrets must be at least 32 bytes (RFC 7518 section 3.2).
      - The role claim may be a string or a list; a list containing "admin"
        yields the admin role.
    """

    def __init__(self, *, secret: str | None = None, public_key: str | None = None,
                 algorithms: Sequence[str] | None = None, issuer: str | None = None,
                 audience: str | None = None, role_claim: str = "role", leeway: float = 30.0) -> None:
        if bool(secret) == bool(public_key):
            raise ValueError("configure exactly one of a JWT secret or a JWT public key")
        if secret:
            if len(secret.encode()) < 32:
                raise ValueError("JWT HMAC secret must be at least 32 bytes")
            algs = list(algorithms or ["HS256"])
            allowed = _HMAC_ALGS
            self._key: object = secret
        else:
            algs = list(algorithms or ["RS256"])
            allowed = _ASYMMETRIC_ALGS
            self._key = public_key
        bad = [a for a in algs if a not in allowed]
        if bad:
            raise ValueError(f"algorithms {bad} not allowed with this key type (allowed: {sorted(allowed)})")
        self._algorithms = algs
        self._issuer = issuer
        self._audience = audience
        self._role_claim = role_claim
        self._leeway = leeway
        self._required = ["exp"] + (["iss"] if issuer else []) + (["aud"] if audience else [])

    def authenticate(self, authorization: str | None) -> Principal | None:
        token = bearer_token(authorization)
        if token is None or token.count(".") != 2:
            return None  # not a JWT (maybe an opaque token for another authenticator)
        try:
            claims = jwt.decode(token, self._key, algorithms=self._algorithms, issuer=self._issuer,
                                audience=self._audience, leeway=self._leeway,
                                options={"require": self._required})
        except jwt.PyJWTError as exc:
            # The reason (expired, bad signature, wrong audience...) is logged,
            # never returned to the client.
            logger.info("jwt rejected: %s", type(exc).__name__)
            return None
        role = claims.get(self._role_claim)
        if isinstance(role, list):
            role = ADMIN_ROLE if ADMIN_ROLE in role else next((r for r in role if isinstance(r, str) and r), None)
        if not isinstance(role, str) or not role:
            logger.info("jwt rejected: no usable %r claim", self._role_claim)
            return None
        sub = claims.get("sub")
        return Principal(role=role, token_fingerprint=_digest(token)[:12], subject=str(sub) if sub else None)


class MultiAuthenticator:
    """Tries each authenticator in order; the first principal wins."""

    def __init__(self, authenticators: Iterable[Authenticator]) -> None:
        self._all = list(authenticators)
        if not self._all:
            raise ValueError("no authenticators configured; refusing to start a gateway that accepts nobody")

    def authenticate(self, authorization: str | None) -> Principal | None:
        for a in self._all:
            if (principal := a.authenticate(authorization)) is not None:
                return principal
        return None


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
