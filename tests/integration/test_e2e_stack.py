"""End-to-end checks against a running lexwatch stack (docker compose).

Skipped unless ``LEXWATCH_E2E_URL`` is set (e.g. ``http://localhost:8000``), so the normal
suite stays offline. The stack must run with ``ENV=development`` and ``DEV_AUTH=true`` (the
tests log in through ``/login?as=admin``) and should have ingested data.

    LEXWATCH_E2E_URL=http://localhost:8000 uv run pytest tests/integration -v

The tests are read-only apart from switching the admin user's language (restored at the end).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import httpx
import pytest

E2E_URL = os.environ.get("LEXWATCH_E2E_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(not E2E_URL, reason="LEXWATCH_E2E_URL not set")

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
ROW_RE = re.compile(r'<a href="/bills/([^"]+)"')


def _csrf(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "no CSRF token in page"
    return match.group(1)


def _bill_numbers(html: str) -> list[str]:
    return list(dict.fromkeys(ROW_RE.findall(html)))


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=E2E_URL, follow_redirects=True, timeout=60) as c:
        resp = c.get("/login?as=admin")
        assert resp.status_code == 200, "DEV_AUTH login failed: is DEV_AUTH=true?"
        yield c
        c.post("/lang", data={"lang": "fr", "next": "/", "csrf_token": _csrf(c.get("/").text)})


def _set_lang(client: httpx.Client, lang: str) -> None:
    token = _csrf(client.get("/").text)
    resp = client.post("/lang", data={"lang": lang, "next": "/", "csrf_token": token})
    assert resp.status_code == 200
    assert f'<html lang="{lang}">' in resp.text


def test_health_and_ready() -> None:
    assert httpx.get(f"{E2E_URL}/health", timeout=10).json() == {"status": "ok"}
    ready = httpx.get(f"{E2E_URL}/ready", timeout=10)
    assert ready.status_code == 200, ready.text
    assert ready.json()["database"] is True


@pytest.mark.parametrize("lang", ["fr", "en"])
def test_every_screen_loads(client: httpx.Client, lang: str) -> None:
    _set_lang(client, lang)
    numbers = _bill_numbers(client.get("/?all=1").text)
    assert numbers, "no bills listed: has the daily job run?"
    rules = client.get("/rules").text
    profile_ids = re.findall(r'href="/rules/(\d+)"', rules)
    paths = [
        "/",
        "/?all=1",
        "/?all=1&q=blanchiment",
        f"/bills/{numbers[0]}",
        "/rules",
        "/rules/new",
        "/admin",
    ]
    if profile_ids:
        paths += [f"/rules/{profile_ids[0]}", f"/rules/{profile_ids[0]}/delete"]
    for path in paths:
        resp = client.get(path)
        assert resp.status_code == 200, f"{path}: {resp.status_code}"
        assert f'<html lang="{lang}">' in resp.text, path
        assert "https://www.chd.lu" in resp.text, f"{path}: source attribution missing"


def test_search_is_accent_and_case_insensitive(client: httpx.Client) -> None:
    base = _bill_numbers(client.get("/", params={"all": "1", "q": "energie"}).text)
    assert base, "no result for 'energie'"
    for variant in ("énergie", "ÉNERGIE", "Energie"):
        got = _bill_numbers(client.get("/", params={"all": "1", "q": variant}).text)
        assert got == base, variant


def test_search_by_bill_number(client: httpx.Client) -> None:
    numbers = _bill_numbers(client.get("/?all=1").text)
    got = _bill_numbers(client.get("/", params={"all": "1", "q": numbers[0]}).text)
    assert numbers[0] in got


def test_post_without_csrf_is_rejected(client: httpx.Client) -> None:
    for path in ("/admin/run", "/admin/rescore", "/rules/new", "/lang"):
        resp = client.post(path, data={"lang": "en"}, follow_redirects=False)
        assert resp.status_code == 403, f"{path}: {resp.status_code}"


@pytest.mark.parametrize("name", ["bills", "matches"])
def test_csv_exports(client: httpx.Client, name: str) -> None:
    resp = client.get(f"/admin/export/{name}.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers.get("content-disposition", "")
    lines = resp.text.lstrip("﻿").splitlines()
    assert lines and "," in lines[0]


def test_matched_bill_shows_rule_scores_on_hover(client: httpx.Client) -> None:
    numbers = _bill_numbers(client.get("/").text)  # matches of the selected profile
    if not numbers:
        pytest.skip("no matched bills for the default profile")
    html = client.get(f"/bills/{numbers[0]}").text
    hint = re.search(
        r'<span tabindex="0" class="hint" (?:title|data-tip)="([^"]+)" aria-describedby="([^"]+)"',
        html,
    )
    assert hint, "no per-rule hover on the score"
    assert f'id="{hint.group(2)}"' in html
