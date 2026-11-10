"""Signed-JWT authentication and the optional tools/list filter."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from mcp_gateway.app import create_app
from mcp_gateway.auth import JwtVerifier, MultiAuthenticator, TokenStore, may_call_tool
from mcp_gateway.mock_downstream import create_mock_app

SECRET = "s" * 32 + "-test-hmac-secret"
ISS, AUD = "https://idp.example", "mcp-gateway"
STATIC = {"static-viewer-token": "viewer"}


def hs(claims: dict[str, Any], key: str = SECRET, alg: str = "HS256", **headers: Any) -> str:
    return jwt.encode(claims, key, algorithm=alg, headers=headers or None)


def claims(role: Any = "viewer", **kw: Any) -> dict[str, Any]:
    now = int(time.time())
    return {"sub": "user-42", "role": role, "iss": ISS, "aud": AUD, "iat": now, "exp": now + 300, **kw}


def bearer(token: str) -> str:
    return f"Bearer {token}"


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[Any, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return key, pub.decode()


# --- verifier --------------------------------------------------------------------


def test_hs256_roles() -> None:
    v = JwtVerifier(secret=SECRET, issuer=ISS, audience=AUD)
    p = v.authenticate(bearer(hs(claims("admin"))))
    assert p is not None and p.role == "admin" and p.subject == "user-42" and len(p.token_fingerprint) == 12
    assert v.authenticate(bearer(hs(claims("viewer")))).role == "viewer"
    assert v.authenticate(bearer(hs(claims(["viewer", "admin"])))).role == "admin"  # list claim
    assert v.authenticate(bearer(hs(claims(["viewer"])))).role == "viewer"


def test_rs256_and_es256_public_keys(rsa_keys: tuple[Any, str]) -> None:
    key, pub = rsa_keys
    v = JwtVerifier(public_key=pub, issuer=ISS, audience=AUD)
    assert v.authenticate(bearer(jwt.encode(claims("admin"), key, algorithm="RS256"))).role == "admin"
    ec_key = ec.generate_private_key(ec.SECP256R1())
    ec_pub = ec_key.public_key().public_bytes(serialization.Encoding.PEM,
                                              serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    v2 = JwtVerifier(public_key=ec_pub, algorithms=["ES256"], issuer=ISS, audience=AUD)
    assert v2.authenticate(bearer(jwt.encode(claims("admin"), ec_key, algorithm="ES256"))).role == "admin"


def _b64(obj: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


@pytest.mark.parametrize("label, token", [
    ("expired", hs(claims(exp=int(time.time()) - 3600))),
    ("wrong secret", hs(claims(), key="x" * 40)),
    ("wrong issuer", hs(claims(iss="https://evil.example"))),
    ("wrong audience", hs(claims(aud="other-service"))),
    ("missing exp", hs({k: v for k, v in claims().items() if k != "exp"})),
    ("missing iss", hs({k: v for k, v in claims().items() if k != "iss"})),
    ("no role claim", hs({k: v for k, v in claims().items() if k != "role"})),
    ("empty role", hs(claims(role=""))),
    ("non-string role", hs(claims(role=7))),
    ("not yet valid", hs(claims(nbf=int(time.time()) + 3600))),
    ("alg none", _b64({"alg": "none", "typ": "JWT"}) + "." + _b64(claims("admin")) + "."),
    ("tampered payload", (lambda t: t.split(".")[0] + "." + _b64(claims("admin")) + "." + t.split(".")[2])(
        hs(claims("viewer")))),
    ("garbage", "a.b.c"),
    ("HS384 not allowed", hs(claims("admin"), alg="HS384")),
])
def test_rejected_tokens(label: str, token: str) -> None:
    v = JwtVerifier(secret=SECRET, issuer=ISS, audience=AUD)
    assert v.authenticate(bearer(token)) is None, label


def test_audience_bound_token_rejected_when_gateway_has_no_audience() -> None:
    # A token minted for some service is not accepted by a gateway that
    # doesn't check audience: PyJWT rejects an unexpected `aud`.
    assert JwtVerifier(secret=SECRET).authenticate(bearer(hs(claims("admin")))) is None


def test_algorithm_confusion_public_key_as_hmac_secret(rsa_keys: tuple[Any, str]) -> None:
    """Classic attack: sign HS256 using the server's PUBLIC key as the HMAC
    secret. A verifier configured with a public key must not accept HS*."""
    _, pub = rsa_keys
    v = JwtVerifier(public_key=pub, issuer=ISS, audience=AUD)
    import hashlib
    import hmac as _hmac
    header, payload = _b64({"alg": "HS256", "typ": "JWT"}), _b64(claims("admin"))
    sig = base64.urlsafe_b64encode(_hmac.new(pub.encode(), f"{header}.{payload}".encode(),
                                              hashlib.sha256).digest()).rstrip(b"=").decode()
    assert v.authenticate(bearer(f"{header}.{payload}.{sig}")) is None


@pytest.mark.parametrize("kwargs, msg", [
    ({}, "exactly one"),
    ({"secret": SECRET, "public_key": "pem"}, "exactly one"),
    ({"secret": "short"}, "at least 32 bytes"),
    ({"secret": SECRET, "algorithms": ["RS256"]}, "not allowed"),
    ({"public_key": "pem", "algorithms": ["HS256"]}, "not allowed"),
    ({"secret": SECRET, "algorithms": ["none"]}, "not allowed"),
])
def test_unsafe_configuration_refused(kwargs: dict[str, Any], msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        JwtVerifier(**kwargs)


def test_multi_authenticator_static_and_jwt() -> None:
    a = MultiAuthenticator([TokenStore(STATIC), JwtVerifier(secret=SECRET, issuer=ISS, audience=AUD)])
    assert a.authenticate(bearer("static-viewer-token")).role == "viewer"
    admin = a.authenticate(bearer(hs(claims("admin"))))
    assert admin.role == "admin" and may_call_tool(admin, "admin_reset_key")
    assert a.authenticate(bearer("nope")) is None and a.authenticate(None) is None
    with pytest.raises(ValueError):
        MultiAuthenticator([])


# --- gateway with JWT + tools/list filter ------------------------------------------


class GW:
    def __init__(self, client: httpx.AsyncClient, mock: Any) -> None:
        self.client, self.mock = client, mock

    async def post(self, payload: dict[str, Any], token: str | None) -> httpx.Response:
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        if token:
            headers["authorization"] = bearer(token)
        return await self.client.post("/mcp", json=payload, headers=headers)


async def _gw(filter_tools_list: bool) -> AsyncIterator[GW]:
    mock = create_mock_app()
    down = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock))
    auth = MultiAuthenticator([TokenStore(STATIC), JwtVerifier(secret=SECRET, issuer=ISS, audience=AUD)])
    app = create_app("http://downstream/mcp", auth, client=down, filter_tools_list=filter_tools_list)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            yield GW(c, mock)
    await down.aclose()


@pytest.fixture
async def filtered() -> AsyncIterator[GW]:
    async for g in _gw(True):
        yield g


@pytest.fixture
async def unfiltered() -> AsyncIterator[GW]:
    async for g in _gw(False):
        yield g


LIST = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
CALL_ADMIN = {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "admin_reset_key", "arguments": {}}}


def names(r: httpx.Response) -> set[str]:
    if r.headers["content-type"].startswith("text/event-stream"):
        msgs = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        return {t["name"] for m in msgs if "result" in m for t in m["result"]["tools"]}
    return {t["name"] for t in r.json()["result"]["tools"]}


async def test_jwt_roles_enforced_end_to_end(filtered: GW) -> None:
    viewer, admin = hs(claims("viewer")), hs(claims("admin"))
    r = await filtered.post(CALL_ADMIN, viewer)
    assert r.json()["error"]["code"] == -32001 and r.json()["error"]["message"] == "Unauthorized Tool Call"
    assert filtered.mock.state.received == []
    r = await filtered.post(CALL_ADMIN, admin)
    assert "result" in r.json()
    r = await filtered.post(CALL_ADMIN, hs(claims("admin", exp=int(time.time()) - 3600)))
    assert r.status_code == 401 and r.json()["error"]["code"] == -32003


@pytest.mark.parametrize("sse", [False, True])
async def test_tools_list_filtered_for_non_admins(filtered: GW, sse: bool) -> None:
    filtered.mock.state.sse = sse
    for token in ["static-viewer-token", hs(claims("viewer"))]:
        r = await filtered.post(LIST, token)
        assert r.status_code == 200
        assert names(r) == {"get_weather"}, r.text
        assert r.headers["mcp-session-id"] == "mock-session"
    r = await filtered.post(LIST, hs(claims("admin")))
    assert names(r) == {"get_weather", "admin_reset_key"}
    if sse:
        # Other SSE events (progress notification, event names, ids) survive untouched.
        assert r.text.count("event: message") == 2 and "notifications/progress" in r.text and "id: 2" in r.text


async def test_tools_list_transparent_by_default(unfiltered: GW) -> None:
    r = await unfiltered.post(LIST, "static-viewer-token")
    assert names(r) == {"get_weather", "admin_reset_key"}


async def test_filtering_does_not_replace_call_policy(filtered: GW) -> None:
    """Hiding a tool is cosmetic; a viewer who knows the name is still denied."""
    r = await filtered.post(CALL_ADMIN, "static-viewer-token")
    assert r.json()["error"]["code"] == -32001 and filtered.mock.state.received == []


async def test_filter_uses_normalized_admin_check(filtered: GW) -> None:
    import mcp_gateway.mock_downstream as md
    original = list(md.TOOLS)
    md.TOOLS.extend([{"name": "ADMIN_wipe", "inputSchema": {"type": "object"}},
                     {"name": "ａｄｍｉｎ_drop", "inputSchema": {"type": "object"}},
                     {"name": "get_admin_status", "inputSchema": {"type": "object"}}])
    try:
        r = await filtered.post(LIST, "static-viewer-token")
        assert names(r) == {"get_weather", "get_admin_status"}  # prefix, not substring
    finally:
        md.TOOLS[:] = original


# --- configuration from environment ------------------------------------------------

from mcp_gateway.__main__ import build_authenticator  # noqa: E402


def test_env_config_builds_combined_authenticator(tmp_path: Any, rsa_keys: tuple[Any, str]) -> None:
    key, pub = rsa_keys
    pem = tmp_path / "pub.pem"
    pem.write_text(pub)
    a = build_authenticator({"GATEWAY_TOKENS": json.dumps(STATIC), "GATEWAY_JWT_PUBLIC_KEY_FILE": str(pem),
                             "GATEWAY_JWT_ISSUER": ISS, "GATEWAY_JWT_AUDIENCE": AUD, "GATEWAY_JWT_ROLE_CLAIM": "grp"})
    assert a.authenticate(bearer("static-viewer-token")).role == "viewer"
    tok = jwt.encode({**claims(), "grp": ["admin"]}, key, algorithm="RS256")
    assert a.authenticate(bearer(tok)).role == "admin"
    with pytest.raises(ValueError):
        build_authenticator({})  # nothing configured: refuse to start
    with pytest.raises(ValueError):
        build_authenticator({"GATEWAY_JWT_SECRET": "too-short"})
