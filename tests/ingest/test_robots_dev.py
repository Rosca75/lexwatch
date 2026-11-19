"""robots.txt gate against the real chd.lu robots.txt fixture."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from lexwatch.http import polite_get
from lexwatch.sources import robots
from lexwatch.sources.base import SourceError

ROBOTS = (Path(__file__).resolve().parents[1] / "fixtures" / "chd" / "robots.txt").read_text()
UA = "lexwatch/0.1 (+internal bill tracker)"


@pytest.fixture(autouse=True)
def _clear_cache():
    robots.clear_cache()
    yield
    robots.clear_cache()


def _client(status: int = 200, body: str = ROBOTS, calls: list | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(status, text=body)
        return httpx.Response(200, text="<html></html>")

    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": UA})


@pytest.mark.parametrize(
    "url",
    [
        "https://www.chd.lu/fr/dossier/8410",
        "https://www.chd.lu/de/dossier/7389",
        "https://www.chd.lu/fr/search?q=loi",
        "https://www.chd.lu/fr/searchactivities?filterActivity=x",
        "https://www.chd.lu/fr/ajax/feed",
        "https://www.chd.lu/fr/deputies?page=2",
        "https://www.chd.lu/fr/question/123",
    ],
)
def test_disallowed(url):
    assert robots.is_allowed(_client(), url, UA) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://www.chd.lu/fr/deputy/49",
        "https://www.chd.lu/fr/commission/681",
        "https://www.chd.lu/fr/open-data",
        "https://www.chd.lu/sitemap.xml",
        "https://www.chd.lu/robots.txt",
    ],
)
def test_allowed(url):
    assert robots.is_allowed(_client(), url, UA) is True


def test_robots_is_cached_per_host():
    calls: list[str] = []
    client = _client(calls=calls)
    robots.is_allowed(client, "https://www.chd.lu/fr/dossier/1", UA)
    robots.is_allowed(client, "https://www.chd.lu/fr/deputy/1", UA)
    assert calls == ["https://www.chd.lu/robots.txt"]


def test_missing_robots_allows_and_server_error_denies():
    assert robots.is_allowed(_client(404, ""), "https://www.chd.lu/fr/dossier/1", UA) is True
    robots.clear_cache()
    assert robots.is_allowed(_client(503, ""), "https://www.chd.lu/fr/deputy/1", UA) is False


def test_specific_agent_group_and_wildcards():
    rules = robots.RobotsRules.parse(
        "User-agent: lexwatch\nDisallow: /private\nAllow: /private/ok$\n\n"
        "User-agent: *\nDisallow: /\n"
    )
    assert rules.allowed("https://x/public", UA) is True
    assert rules.allowed("https://x/private/a", UA) is False
    assert rules.allowed("https://x/private/ok", UA) is False  # conservative default
    assert rules.allowed("https://x/private/ok", UA, conservative=False) is True
    assert rules.allowed("https://x/private/ok/more", UA, conservative=False) is False
    assert rules.allowed("https://x/public", "otherbot/1.0") is False


def test_polite_get_refuses_dossier_pages():
    calls: list[str] = []
    client = _client(calls=calls)
    with pytest.raises(SourceError, match="robots.txt disallows"):
        polite_get(client, "https://www.chd.lu/fr/dossier/8410")
    assert calls == ["https://www.chd.lu/robots.txt"]  # the page itself was never requested
    assert polite_get(client, "https://www.chd.lu/fr/deputy/49").status_code == 200
