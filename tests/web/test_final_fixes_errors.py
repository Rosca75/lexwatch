"""Error pages (404/403/500) show the full top bar for a logged-in user, a plain one otherwise."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.helpers import login_as

HTML = {"accept": "text/html"}


def _boom() -> None:
    raise RuntimeError("boom")


@pytest.fixture
def boom_app(app):
    app.add_api_route("/boom-test", _boom, methods=["GET"])
    return app


def _has_nav(html: str) -> bool:
    return 'href="/rules"' in html and 'action="/logout"' in html


def test_404_for_logged_in_user_has_nav_and_user_menu(user_client):
    response = user_client.get("/no-such-page", headers=HTML)
    assert response.status_code == 404
    assert "Page introuvable." in response.text
    assert _has_nav(response.text)
    assert "Dev User" in response.text
    assert 'href="/admin"' not in response.text


def test_404_for_admin_shows_admin_link(admin_client):
    response = admin_client.get("/no-such-page", headers=HTML)
    assert response.status_code == 404
    assert _has_nav(response.text) and 'href="/admin"' in response.text


def test_403_for_logged_in_user_has_nav(user_client):
    response = user_client.get("/admin", headers=HTML)
    assert response.status_code == 403
    assert "Accès refusé." in response.text
    assert _has_nav(response.text)


def test_404_for_anonymous_has_plain_top_bar(app):
    with TestClient(app) as anonymous:
        response = anonymous.get("/no-such-page", headers=HTML)
    assert response.status_code == 404
    assert "Page introuvable." in response.text
    assert not _has_nav(response.text)
    assert 'action="/lang"' in response.text  # the language switch is still there


def test_500_for_logged_in_user_has_nav(boom_app):
    with TestClient(boom_app, raise_server_exceptions=False) as client:
        login_as(client, "user")
        response = client.get("/boom-test", headers=HTML)
    assert response.status_code == 500
    assert "Une erreur est survenue." in response.text
    assert _has_nav(response.text)
    assert "RuntimeError" not in response.text


def test_500_for_anonymous_has_plain_top_bar(boom_app):
    with TestClient(boom_app, raise_server_exceptions=False) as client:
        response = client.get("/boom-test", headers=HTML)
    assert response.status_code == 500
    assert "Une erreur est survenue." in response.text
    assert not _has_nav(response.text)


def test_500_without_html_accept_stays_plain_text(boom_app):
    with TestClient(boom_app, raise_server_exceptions=False) as client:
        response = client.get("/boom-test", headers={"accept": "application/json"})
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
