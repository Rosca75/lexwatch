"""Guards, DEV_AUTH, session invalidation and CSRF."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from lexwatch.config import Settings
from lexwatch.models import Profile, User
from lexwatch.web.app import create_app
from lexwatch.web.deps import SESSION_USER_KEY, can_manage_profile, dev_auth_enabled
from tests.auth.support import build_app, oidc_settings, open_client, read_session, write_session
from tests.helpers import make_test_settings

# --- guards ---------------------------------------------------------------------------------


def test_anonymous_is_redirected_to_login(db):
    app = build_app(oidc_settings(), db)
    with open_client(app) as client:
        response = client.get("/_t/user", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        response = client.get("/_t/admin", follow_redirects=False)
        assert response.status_code == 303


def test_anonymous_htmx_gets_hx_redirect(db):
    app = build_app(oidc_settings(), db)
    with open_client(app) as client:
        response = client.get("/_t/user", headers={"HX-Request": "true"})
        assert response.status_code == 401
        assert response.headers["HX-Redirect"] == "/login"


def test_user_gets_403_on_admin(db):
    app = build_app(make_test_settings(), db)
    with open_client(app) as client:
        assert client.get("/login?as=user", follow_redirects=False).status_code == 303
        assert client.get("/_t/user").json()["role"] == "user"
        assert client.get("/_t/admin").status_code == 403
        page = client.get("/_t/admin", headers={"accept": "text/html"})
        assert page.status_code == 403
        assert "Traceback" not in page.text


def test_admin_guard_with_dev_admin(db):
    app = build_app(make_test_settings(), db)
    with open_client(app) as client:
        client.get("/login?as=admin", follow_redirects=False)
        assert client.get("/_t/admin").status_code == 200


def test_can_manage_profile(db):
    owner = User(oidc_sub="o", role="user")
    other = User(oidc_sub="p", role="user")
    admin = User(oidc_sub="a", role="admin")
    db.add_all([owner, other, admin])
    db.flush()
    profile = Profile(name="P", owner_id=owner.id)
    assert can_manage_profile(owner, profile)
    assert not can_manage_profile(other, profile)
    assert can_manage_profile(admin, profile)
    assert not can_manage_profile(None, profile)


# --- DEV_AUTH -------------------------------------------------------------------------------


def test_dev_auth_refused_in_production():
    settings = make_test_settings(env="production", secret_key="x" * 40, dev_auth=True)
    with pytest.raises(ValueError, match="DEV_AUTH"):
        settings.validate()
    with pytest.raises(ValueError, match="DEV_AUTH"):
        create_app(settings)


def test_dev_auth_from_env_is_off_by_default():
    assert Settings.from_env({}).dev_auth is False
    assert dev_auth_enabled(Settings(env="production", dev_auth=True)) is False
    assert dev_auth_enabled(Settings(env="development", dev_auth=True)) is True


def test_dev_login_as_is_ignored_without_dev_auth(db):
    app = build_app(oidc_settings(oidc_issuer=""), db)
    with open_client(app) as client:
        response = client.get("/login?as=admin", follow_redirects=False)
        assert response.status_code == 503
        assert SESSION_USER_KEY not in read_session(client)


def test_dev_login_rotates_session_and_records_visit(db):
    app = build_app(make_test_settings(), db)
    with open_client(app) as client:
        client.get("/login?as=user", follow_redirects=False)
        first = read_session(client)
        user = db.scalar(select(User).where(User.oidc_sub == "dev-user"))
        assert first[SESSION_USER_KEY] == user.id
        last = user.last_visit_at
        assert last is not None
        client.get("/login?as=user", follow_redirects=False)
        second = read_session(client)
        assert second["csrf_token"] != first["csrf_token"]
        db.refresh(user)
        assert user.previous_visit_at == last


def test_dev_auto_login_records_visit(db):
    app = build_app(make_test_settings(dev_auth_role="user"), db)
    with open_client(app) as client:
        assert client.get("/_t/user").status_code == 200
    user = db.scalar(select(User).where(User.oidc_sub == "dev-user"))
    assert user.last_visit_at is not None


# --- session invalidation -------------------------------------------------------------------


def test_deleted_user_session_is_invalidated(db):
    app = build_app(oidc_settings(), db)
    user = User(oidc_sub="gone", role="admin")
    db.add(user)
    db.flush()
    with open_client(app) as client:
        client.get("/logged-out")
        write_session(client, {SESSION_USER_KEY: user.id, "csrf_token": "t"})
        assert client.get("/_t/admin").status_code == 200
        db.delete(user)
        db.flush()
        assert client.get("/_t/admin", follow_redirects=False).status_code == 303
        assert read_session(client) == {} or SESSION_USER_KEY not in read_session(client)


def test_tampered_cookie_is_ignored(db):
    app = build_app(oidc_settings(), db)
    with open_client(app) as client:
        client.get("/logged-out")
        write_session(client, {SESSION_USER_KEY: 1}, secret="wrong-secret")
        assert client.get("/_t/user", follow_redirects=False).status_code == 303


def test_session_cookie_flags(db):
    app = build_app(make_test_settings(), db)
    with open_client(app) as client:
        response = client.get("/login?as=user", follow_redirects=False)
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=43200" in cookie
        assert "secure" not in cookie
    app = build_app(make_test_settings(session_https_only=True), db)
    with open_client(app) as client:
        cookie = client.get("/login?as=user", follow_redirects=False).headers["set-cookie"]
        assert "secure" in cookie.lower()


# --- CSRF -----------------------------------------------------------------------------------


@pytest.fixture
def logged_in(db):
    app = build_app(make_test_settings(), db)
    with open_client(app) as client:
        client.get("/login?as=user", follow_redirects=False)
        yield client


def test_csrf_missing_token_is_403(logged_in):
    assert logged_in.post("/_t/echo").status_code == 403
    assert logged_in.post("/_t/echo", data={"x": "1"}).status_code == 403


def test_csrf_wrong_token_is_403(logged_in):
    assert logged_in.post("/_t/echo", data={"csrf_token": "nope"}).status_code == 403
    assert logged_in.post("/_t/echo", headers={"X-CSRF-Token": "nope"}).status_code == 403


def test_csrf_form_field_and_header(logged_in):
    token = read_session(logged_in)["csrf_token"]
    assert logged_in.post("/_t/echo", data={"csrf_token": token}).status_code == 200
    assert logged_in.post("/_t/echo", headers={"X-CSRF-Token": token}).status_code == 200
    multipart = logged_in.post("/_t/echo", data={"csrf_token": token}, files={"f": b"x"})
    assert multipart.status_code == 200


def test_csrf_token_rendered_in_pages(logged_in):
    token = read_session(logged_in)["csrf_token"]
    page = logged_in.get("/logged-out")
    assert f'name="csrf_token" value="{token}"' in page.text
    assert f'"X-CSRF-Token": "{token}"' in page.text


def test_csrf_htmx_failure_asks_for_refresh(logged_in):
    response = logged_in.post("/_t/echo", headers={"HX-Request": "true", "X-CSRF-Token": "x"})
    assert response.status_code == 403
    assert response.headers["HX-Refresh"] == "true"


def test_csrf_origin_check(logged_in):
    token = read_session(logged_in)["csrf_token"]
    ok = {"X-CSRF-Token": token, "Origin": "http://testserver"}
    assert logged_in.post("/_t/echo", headers=ok).status_code == 200
    bad = {"X-CSRF-Token": token, "Origin": "https://evil.example"}
    assert logged_in.post("/_t/echo", headers=bad).status_code == 403
    null = {"X-CSRF-Token": token, "Origin": "null"}
    assert logged_in.post("/_t/echo", headers=null).status_code == 403


def test_csrf_not_required_for_safe_methods(logged_in):
    assert logged_in.get("/_t/user").status_code == 200


@pytest.mark.parametrize("bad", [2**31, 2**63, 10**30, -1, 0, "1", True, 1.5])
def test_out_of_range_or_bad_user_id_is_invalid_session(db, bad):
    app = build_app(oidc_settings(), db)
    with open_client(app) as client:
        client.get("/logged-out")
        write_session(client, {SESSION_USER_KEY: bad, "csrf_token": "t"})
        response = client.get("/_t/user", follow_redirects=False)
        assert response.status_code == 303
        assert SESSION_USER_KEY not in read_session(client)


def test_not_authenticated_carries_next(db):
    from lexwatch.web.deps import NotAuthenticated, require_user

    app = build_app(oidc_settings(), db)

    @app.exception_handler(NotAuthenticated)
    async def _handler(request, exc):  # what app.py should do with exc.login_url
        from fastapi.responses import RedirectResponse

        return RedirectResponse(exc.login_url, status_code=303)

    assert require_user  # imported for clarity
    with open_client(app) as client:
        response = client.get("/_t/user?x=1", follow_redirects=False)
        assert response.headers["location"] == "/login?next=%2F_t%2Fuser%3Fx%3D1"
        htmx = client.get("/_t/user", headers={"HX-Request": "true"}, follow_redirects=False)
        assert htmx.status_code in (303, 401)
