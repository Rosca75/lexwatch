"""QA (stream 4): OIDC ID-token validation and role mapping, against a mocked provider.

Spec section 8: OIDC with issuer, client id/secret, redirect URI, scopes and role claim from the
environment; roles ``admin``/``user`` mapped from a group or role claim (section 6).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import select

from lexwatch.models import User
from lexwatch.web.auth import claim_values, role_from_values
from lexwatch.web.deps import SESSION_USER_KEY
from tests.auth.support import (
    CLIENT_ID,
    ISSUER,
    FakeProvider,
    build_app,
    oidc_settings,
    read_session,
    signing_key,
)


class MutatingProvider(FakeProvider):
    """FakeProvider whose token response can be rewritten (id_token tampering)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.mutate: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.drop_claims: set[str] = set()

    def _token(self, request: httpx.Request) -> httpx.Response:
        response = super()._token(request)
        if response.status_code != 200:
            return response
        body = json.loads(response.content)
        if self.drop_claims:
            claims = _payload(body["id_token"])
            for name in self.drop_claims:
                claims.pop(name, None)
            body["id_token"] = _sign(claims)
        if self.mutate is not None:
            body = self.mutate(body)
        return httpx.Response(200, json=body)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _payload(token: str) -> dict[str, Any]:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _sign(claims: dict[str, Any], key: RSAKey | None = None, kid: str = "test-key") -> str:
    return jwt.encode({"alg": "RS256", "kid": kid}, claims, key or signing_key())


def _user(db, sub: str) -> User | None:
    return db.scalar(select(User).where(User.oidc_sub == sub))


@pytest.fixture
def provider() -> MutatingProvider:
    return MutatingProvider()


def _rejected(db, client: TestClient, response, sub: str) -> None:
    assert response.status_code == 400, response.status_code
    assert "Traceback" not in response.text
    assert SESSION_USER_KEY not in read_session(client)
    assert _user(db, sub) is None
    assert client.get("/_t/user", follow_redirects=False).status_code == 303


# --- ID token validation -------------------------------------------------------------------


def test_valid_login_baseline(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        response = provider.login(client, {"sub": "qa-ok", "email": "ok@x.lu"})
        assert response.status_code == 303
        assert client.get("/_t/user").status_code == 200


def test_tampered_payload_bad_signature_rejected(db, provider):
    def tamper(body):
        header, payload, sig = body["id_token"].split(".")
        claims = _payload(body["id_token"])
        claims["sub"] = "qa-tampered"
        body["id_token"] = ".".join([header, _b64(json.dumps(claims).encode()), sig])
        return body

    provider.mutate = tamper
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        response = provider.login(client, {"sub": "qa-original"})
        _rejected(db, client, response, "qa-tampered")
        assert _user(db, "qa-original") is None


def test_token_signed_by_foreign_key_with_same_kid_rejected(db, provider):
    other = RSAKey.generate_key(2048, parameters={"kid": "test-key"})

    def resign(body):
        body["id_token"] = _sign(_payload(body["id_token"]), key=other)
        return body

    provider.mutate = resign
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-foreign"}), "qa-foreign")


def test_unsigned_alg_none_token_rejected(db, provider):
    def unsigned(body):
        claims = _payload(body["id_token"])
        header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        body["id_token"] = f"{header}.{_b64(json.dumps(claims).encode())}."
        return body

    provider.mutate = unsigned
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-none"}), "qa-none")


def test_hs256_token_signed_with_client_secret_rejected(db, provider):
    """Algorithm confusion: the provider only advertises RS256."""
    from joserfc.jwk import OctKey

    def hs(body):
        claims = _payload(body["id_token"])
        body["id_token"] = jwt.encode({"alg": "HS256"}, claims, OctKey.import_key("s3cret"))
        return body

    provider.mutate = hs
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-hs"}), "qa-hs")


@pytest.mark.parametrize(
    "aud", ["someone-else", ["someone-else"], "lexwatch-app-x", "LEXWATCH-APP"]
)
def test_wrong_audience_rejected(db, provider, aud):
    provider.id_token_overrides = {"aud": aud}
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-aud"}), "qa-aud")


@pytest.mark.xfail(
    strict=True,
    reason="BUG: an ID token whose aud is present but empty ('' or []) is accepted; Authlib's "
    "validate_aud skips falsy values and the app adds no own check that aud contains client_id",
)
@pytest.mark.parametrize("aud", ["", []])
def test_empty_audience_rejected(db, provider, aud):
    provider.id_token_overrides = {"aud": aud}
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-aud-empty"}), "qa-aud-empty")


@pytest.mark.parametrize(
    "iss", ["https://evil.example", ISSUER + "/", ISSUER.upper(), ISSUER + "x", ""]
)
def test_wrong_issuer_rejected(db, provider, iss):
    provider.id_token_overrides = {"iss": iss}
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-iss"}), "qa-iss")


def test_missing_issuer_and_audience_rejected(db, provider):
    provider.drop_claims = {"iss", "aud"}
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-noiss"}), "qa-noiss")


def test_expired_token_rejected(db, provider):
    now = int(time.time())
    provider.id_token_overrides = {"iat": now - 7200, "exp": now - 3600}
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-exp"}), "qa-exp")


def test_missing_exp_rejected(db, provider):
    provider.drop_claims = {"exp"}
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-noexp"}), "qa-noexp")


def test_missing_nonce_rejected(db, provider):
    provider.drop_claims = {"nonce"}
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-nononce"}), "qa-nononce")


def test_nonce_from_another_login_rejected(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as other:
        other_location = other.get("/login", follow_redirects=False).headers["location"]
    other_nonce = parse_qs(urlsplit(other_location).query)["nonce"][0]
    provider.id_token_overrides = {"nonce": other_nonce}
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-nonce-x"}), "qa-nonce-x")


def test_missing_sub_rejected(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        response = provider.login(client, {"email": "nosub@x.lu"})
        assert response.status_code == 400
        assert SESSION_USER_KEY not in read_session(client)


def test_no_id_token_in_token_response_rejected(db, provider):
    def drop(body):
        body.pop("id_token")
        return body

    provider.mutate = drop
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        response = provider.login(client, {"sub": "qa-noid"})
        assert response.status_code == 400
        assert SESSION_USER_KEY not in read_session(client)


# --- state -----------------------------------------------------------------------------------


def _start(client: TestClient) -> dict[str, str]:
    location = client.get("/login", follow_redirects=False).headers["location"]
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def test_state_mismatch_rejected(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        query = _start(client)
        provider.nonce, provider.code_challenge = query["nonce"], query["code_challenge"]
        provider.claims = {"sub": "qa-state"}
        response = client.get(f"/auth/callback?code=c&state={query['state']}x")
        _rejected(db, client, response, "qa-state")


def test_missing_state_rejected(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        query = _start(client)
        provider.nonce, provider.code_challenge = query["nonce"], query["code_challenge"]
        provider.claims = {"sub": "qa-nostate"}
        _rejected(db, client, client.get("/auth/callback?code=c"), "qa-nostate")


def test_callback_without_login_rejected(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        response = client.get("/auth/callback?code=c&state=anything")
        assert response.status_code == 400
        assert SESSION_USER_KEY not in read_session(client)


def test_state_from_another_browser_rejected(db, provider):
    """Login CSRF: the attacker's callback URL must not work in the victim's browser."""
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as attacker, TestClient(app) as victim:
        query = _start(attacker)
        _start(victim)  # victim has its own pending login
        provider.nonce, provider.code_challenge = query["nonce"], query["code_challenge"]
        provider.claims = {"sub": "qa-attacker"}
        response = victim.get(f"/auth/callback?code=c&state={query['state']}")
        _rejected(db, victim, response, "qa-attacker")


def test_callback_replay_rejected(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        query = _start(client)
        provider.nonce, provider.code_challenge = query["nonce"], query["code_challenge"]
        provider.claims = {"sub": "qa-replay"}
        url = f"/auth/callback?code=c&state={query['state']}"
        assert client.get(url, follow_redirects=False).status_code == 303
    with TestClient(app) as fresh:
        assert fresh.get(url).status_code == 400
        assert SESSION_USER_KEY not in read_session(fresh)


def test_pkce_s256_and_distinct_state_per_login(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as a, TestClient(app) as b:
        qa, qb = _start(a), _start(b)
    assert qa["code_challenge_method"] == "S256"
    assert qa["state"] != qb["state"] and qa["nonce"] != qb["nonce"]
    assert len(qa["state"]) >= 16 and len(qa["nonce"]) >= 16


# --- role mapping (unit) ---------------------------------------------------------------------


def _role(claims: dict[str, Any], path: str = "roles", admin: str = "lexwatch-admin") -> str:
    settings = oidc_settings(oidc_role_claim=path, oidc_admin_value=admin)
    return role_from_values(claim_values(claims, path), settings)


@pytest.mark.parametrize(
    ("claims", "path", "expected"),
    [
        ({}, "roles", "user"),  # missing claim
        ({"roles": None}, "roles", "user"),
        ({"roles": []}, "roles", "user"),
        ({"roles": ""}, "roles", "user"),
        ({"roles": "lexwatch-admin"}, "roles", "admin"),  # string
        ({"roles": ["lexwatch-admin"]}, "roles", "admin"),  # list
        ({"roles": ["reader", "lexwatch-admin"]}, "roles", "admin"),
        ({"roles": "reader lexwatch-admin"}, "roles", "admin"),
        ({"roles": "reader,lexwatch-admin"}, "roles", "admin"),
        ({"roles": ["lexwatch-admins"]}, "roles", "user"),  # no substring/prefix match
        ({"roles": ["not-lexwatch-admin"]}, "roles", "user"),
        ({"roles": "lexwatch-administrator"}, "roles", "user"),
        ({"roles": [["lexwatch-admin"]]}, "roles", "user"),  # nested list is not a value
        ({"roles": [{"lexwatch-admin": 1}]}, "roles", "user"),
        ({"roles": True}, "roles", "user"),
        ({"groups": ["lexwatch-admin"]}, "roles", "user"),  # other claim does not count
        ({"realm_access": {"roles": ["lexwatch-admin"]}}, "realm_access.roles", "admin"),
        ({"realm_access": {"roles": ["x"]}}, "realm_access.roles", "user"),
        ({"realm_access": {}}, "realm_access.roles", "user"),
        ({"realm_access": "lexwatch-admin"}, "realm_access.roles", "user"),
        (
            {"resource_access": {"lexwatch-app": {"roles": ["lexwatch-admin"]}}},
            "resource_access.lexwatch-app.roles",
            "admin",
        ),
        ({"https://x.test/roles": ["lexwatch-admin"]}, "https://x.test/roles", "admin"),
        ({"roles": ["LexWatch-Admin"]}, "roles", "admin"),  # case-insensitive (documented)
        ({"roles": [" lexwatch-admin "]}, "roles", "user"),
    ],
)
def test_role_mapping_edge_cases(claims, path, expected):
    assert _role(claims, path) == expected


def test_role_mapping_empty_admin_value_never_grants_admin():
    assert _role({"roles": ["lexwatch-admin"]}, admin="") == "user"
    assert _role({"roles": [""]}, admin="") == "user"
    assert _role({"roles": ""}, admin=" , ") == "user"


def test_role_mapping_multiple_admin_values():
    assert _role({"groups": ["g-2"]}, "groups", admin="g-1, g-2") == "admin"
    assert _role({"groups": ["g-3"]}, "groups", admin="g-1, g-2") == "user"


def test_role_mapping_entra_group_guid_case():
    guid = "1B2c3d4e-0000-1111-2222-333344445555"
    assert _role({"groups": [guid.lower()]}, "groups", admin=guid) == "admin"


# --- role mapping (end to end) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "path", "expected"),
    [
        ({"sub": "qa-r1"}, "roles", "user"),
        ({"sub": "qa-r2", "roles": "lexwatch-admin"}, "roles", "admin"),
        ({"sub": "qa-r3", "roles": ["lexwatch-admin"]}, "roles", "admin"),
        (
            {"sub": "qa-r4", "realm_access": {"roles": ["lexwatch-admin"]}},
            "realm_access.roles",
            "admin",
        ),
        ({"sub": "qa-r5", "roles": ["LEXWATCH-ADMIN"]}, "roles", "admin"),
        ({"sub": "qa-r6", "roles": ["lexwatch-admin-ro"]}, "roles", "user"),
    ],
)
def test_role_mapping_end_to_end(db, provider, claims, path, expected):
    app = build_app(oidc_settings(oidc_role_claim=path), db, provider)
    with TestClient(app) as client:
        assert provider.login(client, claims).status_code == 303
        assert _user(db, claims["sub"]).role == expected
        assert client.get("/_t/admin").status_code == (200 if expected == "admin" else 403)


def test_role_claim_in_access_token_only(db, provider):
    """Keycloak default: realm_access only in the access token."""
    provider.access_token_claims = {"realm_access": {"roles": ["lexwatch-admin"]}}
    app = build_app(oidc_settings(oidc_role_claim="realm_access.roles"), db, provider)
    with TestClient(app) as client:
        assert provider.login(client, {"sub": "qa-at"}).status_code == 303
    assert _user(db, "qa-at").role == "admin"


def test_role_in_id_token_wins_over_access_token(db, provider):
    provider.access_token_claims = {"roles": ["lexwatch-admin"]}
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        assert provider.login(client, {"sub": "qa-idwins", "roles": []}).status_code == 303
    assert _user(db, "qa-idwins").role == "user"


def test_user_role_is_not_taken_from_login_request(db, provider):
    """Nothing the browser sends (query params) can choose the role."""
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        response = provider.login(client, {"sub": "qa-as"}, path="/login?as=admin&role=admin")
        assert response.status_code == 303
    assert _user(db, "qa-as").role == "user"


def test_client_id_and_issuer_in_authorize_request(db, provider):
    app = build_app(oidc_settings(oidc_scopes="openid email profile groups"), db, provider)
    with TestClient(app) as client:
        query = _start(client)
    assert query["client_id"] == CLIENT_ID
    assert query["scope"] == "openid email profile groups"


@pytest.mark.parametrize("claim", ["aud", "iss", "iat", "sub"])
def test_missing_essential_claim_rejected(db, provider, claim):
    provider.drop_claims = {claim}
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        _rejected(db, client, provider.login(client, {"sub": "qa-ess"}), "qa-ess")
