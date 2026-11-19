"""OIDC login against a mocked provider (discovery, JWKS, token endpoint via MockTransport)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from lexwatch.models import User
from lexwatch.web.auth import claim_values, role_from_values, safe_next
from lexwatch.web.deps import SESSION_USER_KEY
from tests.auth.support import (
    CLIENT_ID,
    ISSUER,
    FakeProvider,
    build_app,
    oidc_settings,
    open_client,
    read_session,
    write_session,
)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


def _user(db, sub: str) -> User | None:
    return db.scalar(select(User).where(User.oidc_sub == sub))


def test_login_redirects_with_pkce_state_and_nonce(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 302
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["client_id"] == [CLIENT_ID]
        assert query["redirect_uri"] == ["http://testserver/auth/callback"]
        assert query["response_type"] == ["code"]
        assert query["scope"] == ["openid email profile"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["state"][0] and query["nonce"][0] and query["code_challenge"][0]
        # discovery went to {issuer}/.well-known/openid-configuration
        assert str(provider.requests[0].url) == f"{ISSUER}/.well-known/openid-configuration"


def test_callback_creates_user_and_logs_in(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        response = provider.login(
            client,
            {"sub": "abc-1", "email": "Ana@Example.lu", "name": "Ana", "roles": ["lexwatch-admin"]},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        user = _user(db, "abc-1")
        assert user is not None
        assert (user.email, user.name, user.role) == ("Ana@Example.lu", "Ana", "admin")
        session = read_session(client)
        assert session[SESSION_USER_KEY] == user.id
        assert not any(k.startswith("_state_") for k in session)
        assert client.get("/_t/admin").status_code == 200


def test_callback_updates_role_email_and_name_on_every_login(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        provider.login(client, {"sub": "abc-2", "email": "a@x.lu", "roles": ["lexwatch-admin"]})
    assert _user(db, "abc-2").role == "admin"
    with open_client(app) as client:
        provider.login(client, {"sub": "abc-2", "email": "b@x.lu", "name": "B", "roles": []})
    user = _user(db, "abc-2")
    db.refresh(user)
    assert (user.role, user.email, user.name) == ("user", "b@x.lu", "B")


def test_callback_rejects_bad_state(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        client.get("/login", follow_redirects=False)
        response = client.get("/auth/callback?code=x&state=forged", headers={"accept": "text/html"})
        assert response.status_code == 400
        assert "Traceback" not in response.text
        assert "pas abouti" in response.text
        assert SESSION_USER_KEY not in read_session(client)


def test_callback_rejects_wrong_nonce(db, provider):
    app = build_app(oidc_settings(), db, provider)
    provider.id_token_overrides = {"nonce": "not-the-nonce"}
    with open_client(app) as client:
        response = provider.login(client, {"sub": "n-1", "email": "n@x.lu"})
        assert response.status_code == 400
    assert _user(db, "n-1") is None


def test_callback_rejects_wrong_audience_and_issuer(db, provider):
    app = build_app(oidc_settings(), db, provider)
    provider.id_token_overrides = {"aud": "someone-else"}
    with open_client(app) as client:
        assert provider.login(client, {"sub": "aud-1"}).status_code == 400
    provider.id_token_overrides = {"iss": "https://evil.example"}
    with open_client(app) as client:
        assert provider.login(client, {"sub": "iss-1"}).status_code == 400
    assert _user(db, "aud-1") is None and _user(db, "iss-1") is None


def test_callback_provider_error_shows_friendly_page(db, provider):
    app = build_app(oidc_settings(oidc_role_claim="roles"), db, provider)
    with open_client(app) as client:
        client.get("/login", follow_redirects=False)
        response = client.get(
            "/auth/callback?error=access_denied&error_description=nope&state=x",
        )
        assert response.status_code == 400
        assert "La connexion a été refusée" in response.text


def test_token_endpoint_error_shows_friendly_page(db, provider):
    app = build_app(oidc_settings(), db, provider)
    provider.token_error = "invalid_grant"
    with open_client(app) as client:
        response = provider.login(client, {"sub": "t-1"})
        assert response.status_code == 400
        assert "Traceback" not in response.text


def test_discovery_failure_shows_friendly_page(db):
    provider = FakeProvider(issuer="https://other.example.test")
    app = build_app(oidc_settings(), db, provider)  # issuer does not match provider -> 404
    with open_client(app) as client:
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 502
        assert "injoignable" in response.text


def test_login_without_oidc_config_and_without_dev_auth(db):
    app = build_app(oidc_settings(oidc_issuer=""), db)
    with open_client(app) as client:
        response = client.get("/login")
        assert response.status_code == 503
        assert "pas configurée" in response.text


def test_next_parameter_is_honoured_for_local_paths_only(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        response = provider.login(client, {"sub": "nx-1"}, path="/login?next=/rules/3")
        assert response.headers["location"] == "/rules/3"
    with open_client(app) as client:
        response = provider.login(client, {"sub": "nx-1"}, path="/login?next=//evil.example")
        assert response.headers["location"] == "/"
    assert safe_next("https://evil.example") is None
    assert safe_next("/\\evil.example") is None
    assert safe_next("/bills?q=1") == "/bills?q=1"


def test_session_rotation_on_login(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        client.get("/logged-out")  # renders a page: issues an anonymous CSRF token
        before = read_session(client)
        assert before.get("csrf_token")
        provider.login(client, {"sub": "rot-1"})
        after = read_session(client)
        assert after["csrf_token"] != before["csrf_token"]
        assert set(after) <= {"user_id", "csrf_token", "login_at", "lang"}
        # The pre-login token no longer works.
        assert (
            client.post("/_t/echo", headers={"X-CSRF-Token": before["csrf_token"]}).status_code
            == 403
        )
        assert (
            client.post("/_t/echo", headers={"X-CSRF-Token": after["csrf_token"]}).status_code
            == 200
        )


def test_session_keeps_anonymous_language_choice(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        client.get("/logged-out")
        # Simulate the anonymous language switch stored in the session.
        write_session(client, {**read_session(client), "lang": "en"})
        provider.login(client, {"sub": "lang-1"})
        assert read_session(client)["lang"] == "en"
    assert _user(db, "lang-1").language == "en"


def test_visit_timestamps_shift_on_each_login(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        provider.login(client, {"sub": "v-1"})
    user = _user(db, "v-1")
    first = user.last_visit_at
    assert first is not None and user.previous_visit_at is None
    assert abs(first - datetime.now(UTC)) < timedelta(minutes=1)
    with open_client(app) as client:
        provider.login(client, {"sub": "v-1"})
    db.refresh(user)
    assert user.previous_visit_at == first
    assert user.last_visit_at >= first


# --- role mapping -------------------------------------------------------------------------


def test_entra_roles_claim(db, provider):
    app = build_app(
        oidc_settings(oidc_role_claim="roles", oidc_admin_value="Lexwatch.Admin"), db, provider
    )
    with open_client(app) as client:
        provider.login(
            client,
            {
                "sub": "entra-1",
                "preferred_username": "ana@contoso.lu",
                "name": "Ana",
                "tid": "11111111-2222-3333-4444-555555555555",
                "roles": ["Lexwatch.Reader", "Lexwatch.Admin"],
            },
        )
    user = _user(db, "entra-1")
    assert user.role == "admin"
    assert user.email == "ana@contoso.lu"  # no email claim: preferred_username


def test_entra_groups_guid_claim(db, provider):
    gid = "6A1C0B2E-1F44-4C5B-9F0D-ABCDEF012345"
    app = build_app(
        oidc_settings(oidc_role_claim="groups", oidc_admin_value=f"other-id, {gid.lower()}"),
        db,
        provider,
    )
    with open_client(app) as client:
        provider.login(client, {"sub": "entra-2", "groups": ["0000", gid]})
    assert _user(db, "entra-2").role == "admin"


def test_keycloak_nested_realm_roles_in_id_token(db, provider):
    app = build_app(oidc_settings(oidc_role_claim="realm_access.roles"), db, provider)
    with open_client(app) as client:
        provider.login(
            client,
            {"sub": "kc-1", "realm_access": {"roles": ["offline_access", "lexwatch-admin"]}},
        )
    assert _user(db, "kc-1").role == "admin"


def test_keycloak_roles_only_in_access_token(db, provider):
    app = build_app(oidc_settings(oidc_role_claim="realm_access.roles"), db, provider)
    provider.access_token_claims = {"realm_access": {"roles": ["lexwatch-admin"]}}
    with open_client(app) as client:
        provider.login(client, {"sub": "kc-2"})
    assert _user(db, "kc-2").role == "admin"


def test_role_from_userinfo_when_missing_in_id_token(db, provider):
    app = build_app(
        oidc_settings(oidc_role_claim="groups", oidc_admin_value="/admins"), db, provider
    )
    provider.userinfo = {"sub": "kc-3", "groups": ["/admins"]}
    with open_client(app) as client:
        provider.login(client, {"sub": "kc-3"})
    assert _user(db, "kc-3").role == "admin"


def test_string_role_claim(db, provider):
    app = build_app(oidc_settings(oidc_role_claim="role"), db, provider)
    with open_client(app) as client:
        provider.login(client, {"sub": "s-1", "role": "lexwatch-admin"})
        provider.login(client, {"sub": "s-2", "role": "reader"})
    assert _user(db, "s-1").role == "admin"
    assert _user(db, "s-2").role == "user"


def test_missing_role_claim_means_user(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        provider.login(client, {"sub": "m-1"})
    assert _user(db, "m-1").role == "user"


@pytest.mark.parametrize(
    ("claims", "path", "expected"),
    [
        ({"roles": ["a", "b"]}, "roles", ["a", "b"]),
        ({"roles": "a"}, "roles", ["a"]),
        ({"roles": "a, b c"}, "roles", ["a", "b", "c"]),
        ({"realm_access": {"roles": ["x"]}}, "realm_access.roles", ["x"]),
        (
            {"resource_access": {"lexwatch-app": {"roles": ["y"]}}},
            "resource_access.lexwatch-app.roles",
            ["y"],
        ),
        ({"https://ns.example/roles": ["z"]}, "https://ns.example/roles", ["z"]),
        ({"realm_access": {}}, "realm_access.roles", None),
        ({}, "roles", None),
        ({"roles": None}, "roles", None),
    ],
)
def test_claim_values(claims, path, expected):
    assert claim_values(claims, path) == expected


def test_role_from_values_is_case_insensitive_and_multi_valued():
    settings = oidc_settings(oidc_admin_value="Admins, lexwatch-admin")
    assert role_from_values(["admins"], settings) == "admin"
    assert role_from_values(["LEXWATCH-ADMIN"], settings) == "admin"
    assert role_from_values(["reader"], settings) == "user"
    assert role_from_values(None, settings) == "user"
    assert role_from_values(["x"], oidc_settings(oidc_admin_value="")) == "user"


# --- logout ---------------------------------------------------------------------------------


def test_logout_requires_csrf_and_signs_out_at_provider(db, provider):
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        provider.login(client, {"sub": "lo-1"})
        assert client.post("/logout").status_code == 403
        token = read_session(client)["csrf_token"]
        response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
        assert response.status_code == 303
        location = urlsplit(response.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/logout"
        query = parse_qs(location.query)
        assert query["post_logout_redirect_uri"] == ["http://testserver/logged-out"]
        assert query["client_id"] == [CLIENT_ID]
        assert SESSION_USER_KEY not in read_session(client)
        assert client.get("/_t/user", follow_redirects=False).status_code == 303


def test_logout_without_end_session_endpoint(db):
    provider = FakeProvider(end_session=False)
    app = build_app(oidc_settings(), db, provider)
    with open_client(app) as client:
        provider.login(client, {"sub": "lo-2"})
        token = read_session(client)["csrf_token"]
        response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
        assert response.headers["location"] == "/logged-out"
        page = client.get("/logged-out")
        assert page.status_code == 200
        assert "Vous êtes déconnecté" in page.text


def test_logout_end_session_disabled(db, provider):
    app = build_app(oidc_settings(oidc_end_session=False), db, provider)
    with open_client(app) as client:
        provider.login(client, {"sub": "lo-3"})
        token = read_session(client)["csrf_token"]
        response = client.post("/logout", headers={"X-CSRF-Token": token, "HX-Request": "true"})
        assert response.status_code == 204
        assert response.headers["HX-Redirect"] == "/logged-out"
