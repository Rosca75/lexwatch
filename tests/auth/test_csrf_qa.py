"""QA (stream 4): CSRF on every data-changing route, enumerated from ``app.routes``.

Spec section 8: "CSRF protection on every form and htmx request that changes data."
Routes are discovered programmatically so a route added later by any stream is covered too.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.routing import Mount

from lexwatch.db import get_db
from lexwatch.web.app import create_app
from tests.auth.support import read_session
from tests.helpers import make_test_settings

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_PARAM = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def real_app(db: Session, **overrides) -> FastAPI:
    """The production app (no test-only routes) wired to the test transaction."""
    app = create_app(make_test_settings(**overrides))

    def _get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _get_db
    return app


def concrete(path: str) -> str:
    return _PARAM.sub("1", path)


def flat_routes(app: FastAPI) -> list[tuple[object, str, set[str]]]:
    """``(original_route, full_path, methods)`` for every route, included routers flattened."""
    try:  # FastAPI >= 0.16x keeps included routers as _IncludedRouter entries
        from fastapi.routing import iter_route_contexts
    except ImportError:  # pragma: no cover - older FastAPI flattens app.routes itself
        return [
            (r, getattr(r, "path", ""), set(getattr(r, "methods", None) or ())) for r in app.routes
        ]
    return [
        (ctx.original_route, ctx.path or "", set(getattr(ctx, "methods", None) or ()))
        for ctx in iter_route_contexts(app.routes)
    ]


def unsafe_endpoints(app: FastAPI) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for _route, path, methods in flat_routes(app):
        for method in sorted(methods & UNSAFE):
            found.append((method, concrete(path)))
    return found


def get_endpoints(app: FastAPI) -> list[str]:
    return [concrete(path) for _r, path, methods in flat_routes(app) if "GET" in methods]


@pytest.fixture
def admin_client(db) -> Iterator[TestClient]:
    """Logged in as the DEV_AUTH admin, so authorization never hides a missing CSRF check."""
    app = real_app(db)
    with TestClient(app) as client:
        assert client.get("/login?as=admin", follow_redirects=False).status_code == 303
        yield client


def _ids(params: list[tuple[str, str]]) -> list[str]:
    return [f"{m} {p}" for m, p in params]


# Enumerate once at import (routes do not depend on the DB).
_APP_FOR_ENUM = create_app(make_test_settings())
ENDPOINTS = unsafe_endpoints(_APP_FOR_ENUM)


def test_enumeration_finds_the_known_unsafe_routes():
    paths = {p for _, p in ENDPOINTS}
    assert "/logout" in paths
    assert "/lang" in paths


def test_every_unsafe_route_is_an_apiroute_with_app_level_csrf():
    """A plain Starlette Route/Mount accepting unsafe methods would bypass the dependency."""
    for route, path, methods in flat_routes(_APP_FOR_ENUM):
        if isinstance(route, Mount):
            continue
        if methods & UNSAFE:
            assert isinstance(route, APIRoute), path


@pytest.mark.parametrize(("method", "path"), ENDPOINTS, ids=_ids(ENDPOINTS))
def test_unsafe_route_without_token_is_403(admin_client, method, path):
    before = read_session(admin_client)
    response = admin_client.request(method, path, follow_redirects=False)
    assert response.status_code == 403, (method, path, response.status_code)
    # The session was not changed by the rejected request (e.g. /logout did not log out).
    assert read_session(admin_client).get("user_id") == before.get("user_id")


@pytest.mark.parametrize(("method", "path"), ENDPOINTS, ids=_ids(ENDPOINTS))
def test_unsafe_route_with_wrong_form_token_is_403(admin_client, method, path):
    response = admin_client.request(
        method,
        path,
        data={"csrf_token": "wrong", "lang": "en", "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 403


@pytest.mark.parametrize(("method", "path"), ENDPOINTS, ids=_ids(ENDPOINTS))
def test_unsafe_route_with_wrong_htmx_header_is_403(admin_client, method, path):
    response = admin_client.request(
        method,
        path,
        headers={"HX-Request": "true", "X-CSRF-Token": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert response.headers.get("HX-Refresh") == "true"


@pytest.mark.parametrize(("method", "path"), ENDPOINTS, ids=_ids(ENDPOINTS))
def test_unsafe_route_with_empty_token_is_403(admin_client, method, path):
    response = admin_client.request(
        method, path, headers={"X-CSRF-Token": ""}, data={"csrf_token": ""}
    )
    assert response.status_code == 403


@pytest.mark.parametrize(("method", "path"), ENDPOINTS, ids=_ids(ENDPOINTS))
def test_unsafe_route_anonymous_without_token_is_403(db, method, path):
    app = real_app(db, dev_auth=False)
    with TestClient(app) as client:
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 403


def test_token_from_another_session_is_rejected(db):
    app = real_app(db)
    with TestClient(app) as a, TestClient(app) as b:
        a.get("/login?as=user", follow_redirects=False)
        b.get("/login?as=user", follow_redirects=False)
        token_a = read_session(a)["csrf_token"]
        assert token_a != read_session(b)["csrf_token"]
        response = b.post("/lang", data={"csrf_token": token_a, "lang": "en"})
        assert response.status_code == 403


def test_token_in_query_string_or_json_body_is_not_accepted(admin_client):
    token = read_session(admin_client)["csrf_token"]
    assert admin_client.post(f"/lang?csrf_token={token}", data={"lang": "en"}).status_code == 403
    response = admin_client.post("/lang", json={"csrf_token": token, "lang": "en"})
    assert response.status_code == 403
    response = admin_client.post(
        "/lang",
        content=f"csrf_token={token}&lang=en",
        headers={"content-type": "text/plain"},
    )
    assert response.status_code == 403


def test_valid_token_form_and_header_are_accepted(admin_client):
    token = read_session(admin_client)["csrf_token"]
    response = admin_client.post(
        "/lang", data={"csrf_token": token, "lang": "en", "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 303
    response = admin_client.post(
        "/lang",
        data={"lang": "fr", "next": "/"},
        headers={"X-CSRF-Token": token, "HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 303


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "null", "http://testserver.evil.example", "http://evil/testserver"],
)
def test_cross_origin_with_valid_token_is_rejected(admin_client, origin):
    token = read_session(admin_client)["csrf_token"]
    response = admin_client.post(
        "/lang", data={"csrf_token": token, "lang": "en"}, headers={"Origin": origin}
    )
    assert response.status_code == 403


def test_logout_via_get_does_not_log_out(admin_client):
    assert admin_client.get("/logout", follow_redirects=False).status_code == 405
    assert read_session(admin_client).get("user_id") is not None


def test_logout_with_token_clears_session(db):
    app = real_app(db, dev_auth=True)
    with TestClient(app) as client:
        client.get("/login?as=user", follow_redirects=False)
        token = read_session(client)["csrf_token"]
        response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
        assert response.status_code == 303
        session = read_session(client)
        assert "user_id" not in session
        assert "csrf_token" not in session or session["csrf_token"] != token
