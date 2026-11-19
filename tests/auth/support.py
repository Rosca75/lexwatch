"""Test support for stream 4: a fake OIDC provider (httpx.MockTransport), app builder, session
decoder."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.db import get_db
from lexwatch.models import User
from lexwatch.web.app import SESSION_COOKIE, create_app
from lexwatch.web.deps import require_admin, require_user
from tests.helpers import make_test_settings

ISSUER = "https://idp.example.test/realms/lexwatch"
CLIENT_ID = "lexwatch-app"
_KEY: RSAKey | None = None


def signing_key() -> RSAKey:
    global _KEY
    if _KEY is None:
        _KEY = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    return _KEY


class FakeProvider:
    """Discovery, JWKS, token and userinfo endpoints served through ``httpx.MockTransport``."""

    def __init__(self, issuer: str = ISSUER, end_session: bool = True) -> None:
        self.issuer = issuer
        self.end_session = end_session
        self.claims: dict[str, Any] = {}
        self.access_token_claims: dict[str, Any] | None = None
        self.userinfo: dict[str, Any] | None = None
        self.nonce: str | None = None
        self.code_challenge: str | None = None
        self.token_error: str | None = None
        self.id_token_overrides: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    # endpoints ------------------------------------------------------------------------
    def metadata(self) -> dict[str, Any]:
        data = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/auth",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/certs",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        if self.userinfo is not None:
            data["userinfo_endpoint"] = f"{self.issuer}/userinfo"
        if self.end_session:
            data["end_session_endpoint"] = f"{self.issuer}/logout"
        return data

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == f"{self.issuer}/.well-known/openid-configuration":
            return httpx.Response(200, json=self.metadata())
        if url == f"{self.issuer}/certs":
            return httpx.Response(200, json={"keys": [signing_key().as_dict(private=False)]})
        if url == f"{self.issuer}/token":
            return self._token(request)
        if url == f"{self.issuer}/userinfo":
            return httpx.Response(200, json=self.userinfo or {})
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_error:
            return httpx.Response(400, json={"error": self.token_error})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        verifier = form.get("code_verifier", "")
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if form.get("grant_type") != "authorization_code" or challenge != self.code_challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": self.nonce,
            **self.claims,
            **self.id_token_overrides,
        }
        id_token = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, signing_key())
        if self.access_token_claims is not None:
            access_token = jwt.encode(
                {"alg": "RS256", "kid": "test-key"}, self.access_token_claims, signing_key()
            )
        else:
            access_token = "opaque-access-token"
        return httpx.Response(
            200,
            json={
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": id_token,
            },
        )

    # browser side -----------------------------------------------------------------------
    def login(self, client: TestClient, claims: dict[str, Any], path: str = "/login"):
        """Run /login -> provider -> /auth/callback; return the callback response."""
        self.claims = claims
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302, response.text
        location = response.headers["location"]
        assert location.startswith(f"{self.issuer}/auth?")
        query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        self.nonce = query["nonce"]
        self.code_challenge = query["code_challenge"]
        assert query["code_challenge_method"] == "S256"
        return client.get(
            f"/auth/callback?code=the-code&state={query['state']}", follow_redirects=False
        )


def oidc_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "dev_auth": False,
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": "s3cret",
        "oidc_role_claim": "roles",
        "oidc_admin_value": "lexwatch-admin",
    }
    base.update(overrides)
    return make_test_settings(**base)


def build_app(settings: Settings, db: Session, provider: FakeProvider | None = None) -> FastAPI:
    app = create_app(settings)

    def _get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _get_db
    if provider is not None:
        app.state.oidc_transport = provider.transport

    @app.get("/_t/user")
    def _user_only(user: User = Depends(require_user)) -> dict[str, Any]:
        return {"id": user.id, "role": user.role}

    @app.get("/_t/admin")
    def _admin_only(user: User = Depends(require_admin)) -> dict[str, Any]:
        return {"id": user.id}

    @app.post("/_t/echo")
    def _echo() -> dict[str, bool]:
        return {"ok": True}

    return app


@contextmanager
def open_client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def read_session(client: TestClient, secret: str = "test-secret") -> dict[str, Any]:
    """Decode the signed session cookie of ``client`` (empty dict when there is none)."""
    raw = client.cookies.get(SESSION_COOKIE)
    if not raw:
        return {}
    data = TimestampSigner(secret).unsign(raw.encode())
    return json.loads(base64.b64decode(data))


def write_session(client: TestClient, data: dict[str, Any], secret: str = "test-secret") -> None:
    """Replace the client's session cookie with a validly signed one holding ``data``."""
    payload = base64.b64encode(json.dumps(data).encode())
    signed = TimestampSigner(secret).sign(payload).decode()
    domain = next((c.domain for c in client.cookies.jar if c.name == SESSION_COOKIE), "")
    client.cookies.delete(SESSION_COOKIE)
    client.cookies.set(SESSION_COOKIE, signed, domain=domain)
