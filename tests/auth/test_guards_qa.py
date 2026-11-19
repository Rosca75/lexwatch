"""QA (stream 4): guards, DEV_AUTH in production, cookie flags, open redirect via ``next``,
session fixation and deleted users. Tested against spec section 8, independently of the
developer's tests."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from urllib.parse import urlencode, urljoin, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lexwatch.config import Settings
from lexwatch.models import User
from lexwatch.web.app import SESSION_COOKIE, create_app
from lexwatch.web.deps import SESSION_USER_KEY, dev_auth_enabled
from tests.auth.support import FakeProvider, build_app, oidc_settings, read_session, write_session
from tests.auth.test_csrf_qa import concrete, flat_routes, real_app
from tests.helpers import make_test_settings

PUBLIC_GET = {"/health", "/ready", "/login", "/auth/callback", "/logged-out"}
_APP = create_app(make_test_settings())
PROTECTED_GET = sorted(
    {
        concrete(path)
        for _r, path, methods in flat_routes(_APP)
        if "GET" in methods and path not in PUBLIC_GET and not path.startswith("/static")
    }
)
ADMIN_ENDPOINTS = sorted(
    {
        (method, concrete(path))
        for _r, path, methods in flat_routes(_APP)
        if path.startswith("/admin")
        for method in methods
        if method != "HEAD"
    }
)


# --- anonymous access ----------------------------------------------------------------------


def test_route_enumeration_is_not_empty():
    assert "/" in PROTECTED_GET and "/admin" in PROTECTED_GET and "/rules" in PROTECTED_GET
    assert ("GET", "/admin") in ADMIN_ENDPOINTS


@pytest.fixture
def anon_client(db) -> Iterator[TestClient]:
    app = real_app(db, dev_auth=False)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_anonymous_page_redirects_to_login(anon_client, path):
    response = anon_client.get(path, headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code in (302, 303, 307), (path, response.status_code)
    assert urlsplit(response.headers["location"]).path == "/login"


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_anonymous_htmx_request_gets_hx_redirect(anon_client, path):
    response = anon_client.get(path, headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code in (200, 204, 401)
    assert urlsplit(response.headers["HX-Redirect"]).path == "/login"


@pytest.mark.parametrize("path", sorted(PUBLIC_GET - {"/auth/callback", "/login"}))
def test_public_pages_do_not_require_login(anon_client, path):
    response = anon_client.get(path, follow_redirects=False)
    assert response.status_code in (200, 503), path


def test_anonymous_with_forged_unsigned_cookie_is_not_logged_in(db):
    app = real_app(db, dev_auth=False)
    admin = User(oidc_sub="qa-admin-forge", role="admin")
    db.add(admin)
    db.flush()
    with TestClient(app) as client:
        client.get("/logged-out")
        write_session(client, {SESSION_USER_KEY: admin.id}, secret="not-the-secret")
        response = client.get("/admin", follow_redirects=False)
        assert response.status_code == 303


# --- admin routes --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), ADMIN_ENDPOINTS)
def test_non_admin_gets_403_on_every_admin_route(db, method, path):
    app = real_app(db, dev_auth=True)
    with TestClient(app) as client:
        client.get("/login?as=user", follow_redirects=False)
        token = read_session(client)["csrf_token"]
        response = client.request(
            method,
            path,
            headers={"X-CSRF-Token": token, "accept": "text/html"},
            follow_redirects=False,
        )
        assert response.status_code == 403, (method, path, response.status_code)
        assert "Traceback" not in response.text


def test_demoted_admin_loses_admin_access_on_next_request(db):
    app = real_app(db, dev_auth=False)
    user = User(oidc_sub="qa-demote", role="admin")
    db.add(user)
    db.flush()
    with TestClient(app) as client:
        client.get("/logged-out")
        write_session(client, {SESSION_USER_KEY: user.id, "csrf_token": "x"})
        assert client.get("/admin").status_code == 200
        user.role = "user"
        db.flush()
        assert client.get("/admin").status_code == 403


# --- DEV_AUTH ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_dev_auth_refused_by_settings_validation_in_production(value):
    settings = Settings.from_env({"ENV": "production", "DEV_AUTH": value, "SECRET_KEY": "k" * 40})
    with pytest.raises(ValueError):
        settings.validate()
    with pytest.raises(ValueError):
        create_app(settings)


def test_dev_auth_off_by_default():
    assert Settings.from_env({}).dev_auth is False
    assert Settings().dev_auth is False


@pytest.mark.xfail(
    strict=True,
    reason="BUG: ENV is compared case-sensitively; ENV=Production/PRODUCTION is not treated as "
    "production, so DEV_AUTH is accepted and the Secure cookie flag is not forced",
)
@pytest.mark.parametrize("env", ["Production", "PRODUCTION"])
def test_dev_auth_refused_for_production_in_any_case(env):
    settings = Settings.from_env({"ENV": env, "DEV_AUTH": "true", "SECRET_KEY": "k" * 40})
    with pytest.raises(ValueError):
        settings.validate()


def test_dev_auth_refused_at_runtime_when_production(db):
    """Even if validation were bypassed (settings swapped after startup), DEV_AUTH is inert."""
    app = real_app(db, dev_auth=True)
    prod = replace(app.state.settings, env="production", secret_key="k" * 40)
    assert dev_auth_enabled(prod) is False
    app.state.settings = prod
    with TestClient(app) as client:
        response = client.get("/login?as=admin", follow_redirects=False)
        assert response.status_code != 303 or response.headers["location"].startswith("http")
        assert SESSION_USER_KEY not in read_session(client)
        response = client.get("/admin", follow_redirects=False)
        assert response.status_code == 303
        assert urlsplit(response.headers["location"]).path == "/login"
    assert db.scalar(select(User).where(User.oidc_sub.in_(["dev-admin", "dev-user"]))) is None


def test_dev_login_as_role_ignored_without_dev_auth(db):
    app = real_app(db, dev_auth=False)
    with TestClient(app) as client:
        client.get("/login?as=admin", follow_redirects=False)
        assert SESSION_USER_KEY not in read_session(client)


# --- cookie flags --------------------------------------------------------------------------


def _session_set_cookie(response) -> str:
    cookies = [c for c in response.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE)]
    assert cookies, response.headers
    return cookies[-1]


def _attrs(cookie: str) -> set[str]:
    return {part.strip().split("=")[0].lower() for part in cookie.split(";")[1:]}


def test_cookie_flags_in_development(db):
    app = real_app(db, dev_auth=True)
    with TestClient(app) as client:
        cookie = _session_set_cookie(client.get("/login?as=user", follow_redirects=False))
        assert "httponly" in _attrs(cookie)
        assert re.search(r"samesite=lax", cookie, re.I)


def test_cookie_flags_in_production(db):
    settings = oidc_settings(env="production", secret_key="k" * 40, dev_auth=False)
    provider = FakeProvider()
    app = build_app(settings, db, provider)
    with TestClient(app, base_url="https://testserver") as client:
        cookie = _session_set_cookie(client.get("/login", follow_redirects=False))
        attrs = _attrs(cookie)
        assert {"httponly", "secure", "samesite"} <= attrs
        assert re.search(r"samesite=lax", cookie, re.I)
        # the session issued at the end of the OIDC login is Secure too
        response = provider.login(client, {"sub": "qa-prod-1", "email": "p@x.lu"})
        assert response.status_code == 303
        cookie = _session_set_cookie(response)
        assert {"httponly", "secure"} <= _attrs(cookie)
        assert re.search(r"samesite=lax", cookie, re.I)


def test_session_cookie_is_signed_not_plain_json(db):
    app = real_app(db, dev_auth=True)
    with TestClient(app) as client:
        client.get("/login?as=user", follow_redirects=False)
        raw = client.cookies.get(SESSION_COOKIE)
        assert raw and raw.count(".") >= 2  # itsdangerous payload.timestamp.signature


# --- open redirect -------------------------------------------------------------------------

EVIL_NEXT = [
    "https://evil.example/",
    "//evil.example/",
    "///evil.example/",
    "/\\evil.example/",
    "\\\\evil.example/",
    "\\/evil.example/",
    "/\t/evil.example/",
    "/\n/evil.example/",
    "/%2F/evil.example/",
    "http:evil.example",
    "javascript:alert(1)",
    "evil.example",
    " //evil.example",
    "/\r\nSet-Cookie: x=1",
]


def _browser_target(location: str) -> str:
    """Where a browser would go: strips tab/CR/LF, treats backslash as slash (WHATWG URL)."""
    cleaned = re.sub(r"[\t\r\n]", "", location).replace("\\", "/").strip()
    return urljoin("http://testserver/somewhere", cleaned)


def _assert_local(response) -> None:
    assert response.status_code in (302, 303, 307)
    location = response.headers["location"]
    assert "\r" not in location and "\n" not in location
    target = urlsplit(_browser_target(location))
    assert target.scheme in ("http", "https")
    assert target.netloc == "testserver", location


@pytest.mark.parametrize("evil", EVIL_NEXT)
def test_dev_login_next_cannot_redirect_off_site(db, evil):
    app = real_app(db, dev_auth=True)
    with TestClient(app) as client:
        response = client.get("/login", params={"as": "user", "next": evil}, follow_redirects=False)
        _assert_local(response)


@pytest.mark.parametrize("evil", EVIL_NEXT)
def test_oidc_login_next_cannot_redirect_off_site(db, evil):
    provider = FakeProvider()
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        path = "/login?" + urlencode({"next": evil})
        response = provider.login(client, {"sub": "qa-next"}, path=path)
        _assert_local(response)


@pytest.mark.parametrize("evil", EVIL_NEXT)
def test_language_switch_next_cannot_redirect_off_site(db, evil):
    app = real_app(db, dev_auth=True)
    with TestClient(app) as client:
        client.get("/login?as=user", follow_redirects=False)
        token = read_session(client)["csrf_token"]
        response = client.post(
            "/lang",
            data={"csrf_token": token, "lang": "en", "next": evil},
            follow_redirects=False,
        )
        _assert_local(response)


def test_local_next_is_honoured(db):
    provider = FakeProvider()
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        response = provider.login(client, {"sub": "qa-next-ok"}, path="/login?next=/rules%3Fa%3D1")
        assert response.headers["location"] == "/rules?a=1"


# --- session fixation ----------------------------------------------------------------------


def test_dev_login_rotates_session(db):
    app = real_app(db, dev_auth=True)
    with TestClient(app) as client:
        # attacker-known pre-login session: some anonymous state plus a CSRF token
        client.get("/logged-out")
        before_cookie = client.cookies.get(SESSION_COOKIE)
        before = read_session(client)
        client.get("/login?as=user", follow_redirects=False)
        after = read_session(client)
        assert client.cookies.get(SESSION_COOKIE) != before_cookie
        assert after.get("csrf_token") and after.get("csrf_token") != before.get("csrf_token")


def test_oidc_login_drops_planted_session_values(db):
    provider = FakeProvider()
    app = build_app(oidc_settings(), db, provider)
    with TestClient(app) as client:
        client.get("/logged-out")
        write_session(client, {"csrf_token": "attacker-known", "planted": "x", "lang": "en"})
        cookie_before = client.cookies.get(SESSION_COOKIE)
        response = provider.login(client, {"sub": "qa-fix-1", "email": "f@x.lu"})
        assert response.status_code == 303
        session = read_session(client)
        assert client.cookies.get(SESSION_COOKIE) != cookie_before
        assert session[SESSION_USER_KEY]
        assert session["csrf_token"] != "attacker-known"
        assert "planted" not in session
        assert not any(k.startswith("_state_") for k in session)
        # the attacker-known token is useless after login
        assert (
            client.post("/_t/echo", headers={"X-CSRF-Token": "attacker-known"}).status_code == 403
        )


def test_login_as_another_user_replaces_identity(db):
    app = real_app(db, dev_auth=True)
    with TestClient(app) as client:
        client.get("/login?as=admin", follow_redirects=False)
        admin_session = read_session(client)
        client.get("/login?as=user", follow_redirects=False)
        user_session = read_session(client)
        assert user_session[SESSION_USER_KEY] != admin_session[SESSION_USER_KEY]
        assert client.get("/admin", follow_redirects=False).status_code == 403


# --- deleted user / bad session values -----------------------------------------------------


def test_deleted_user_session_redirects_and_is_cleared(db):
    app = real_app(db, dev_auth=False)
    user = User(oidc_sub="qa-deleted", role="user")
    db.add(user)
    db.flush()
    with TestClient(app) as client:
        client.get("/logged-out")
        write_session(client, {SESSION_USER_KEY: user.id, "csrf_token": "t"})
        assert client.get("/rules").status_code == 200
        db.delete(user)
        db.flush()
        response = client.get("/rules", follow_redirects=False)
        assert response.status_code == 303
        assert urlsplit(response.headers["location"]).path == "/login"
        assert SESSION_USER_KEY not in read_session(client)
        # a recreated user who happens to get another id is not inherited by the old cookie
        assert client.get("/admin", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("value", ["1", 1.0, None, [1], {"id": 1}, -1])
def test_odd_user_id_values_are_not_authenticated(db, value):
    app = real_app(db, dev_auth=False)
    with TestClient(app) as client:
        client.get("/logged-out")
        write_session(client, {SESSION_USER_KEY: value})
        response = client.get("/rules", follow_redirects=False)
        assert response.status_code == 303


def test_deleted_user_with_htmx_gets_hx_redirect(db):
    app = real_app(db, dev_auth=False)
    user = User(oidc_sub="qa-deleted-hx", role="user")
    db.add(user)
    db.flush()
    uid = user.id
    db.delete(user)
    db.flush()
    with TestClient(app) as client:
        client.get("/logged-out")
        write_session(client, {SESSION_USER_KEY: uid})
        response = client.get("/rules", headers={"HX-Request": "true"}, follow_redirects=False)
        assert urlsplit(response.headers["HX-Redirect"]).path == "/login"
