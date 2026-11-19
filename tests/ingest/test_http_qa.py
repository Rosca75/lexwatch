"""QA (independent) tests for the robots.txt gate, polite GET pacing and the User-Agent.

Requirements: build-prompt section 3 (one request at a time, a delay between requests, a clear
User-Agent) and DECISIONS.md (every chd.lu dossier/search/ajax/?page URL refused; robots.txt
failure must fail closed). Bugs are marked ``xfail(strict=True, reason="BUG: ...")``.
"""

from __future__ import annotations

import httpx
import pytest

import lexwatch.http as lhttp
from lexwatch.http import make_client, polite_get
from lexwatch.ingest.pipeline import run_ingestion
from lexwatch.sources import default_adapters, robots
from lexwatch.sources.base import SourceError
from lexwatch.sources.chd import ChdAdapter
from lexwatch.sources.datapublic import Dataset112Adapter, Dataset121Adapter
from tests.helpers import make_test_settings
from tests.ingest.mockdp import API, FIXTURES, DataPublicMock

ROBOTS = (FIXTURES / "chd" / "robots.txt").read_text(encoding="utf-8")
UA = "lexwatch-qa/1.0 (+https://example.org/contact)"


class FakeClock:
    """Replaces the ``time`` module used by lexwatch.http: no real sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    robots.clear_cache()
    monkeypatch.setattr(lhttp, "_last_request", {})
    yield
    robots.clear_cache()


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(lhttp, "time", fake)
    return fake


class ChdSite:
    """Fake www.chd.lu: serves robots.txt and records every request (URL, UA)."""

    def __init__(self, robots_status: int = 200, robots_body: str = ROBOTS, clock=None):
        self.robots_status = robots_status
        self.robots_body = robots_body
        self.calls: list[str] = []
        self.agents: list[str | None] = []
        self.times: list[float] = []
        self.redirects: dict[str, str] = {}
        self.robots_exc: Exception | None = None
        self.clock = clock

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        self.agents.append(request.headers.get("User-Agent"))
        if self.clock is not None:
            self.times.append(self.clock.now)
        if request.url.path == "/robots.txt":
            if self.robots_exc is not None:
                raise self.robots_exc
            return httpx.Response(self.robots_status, text=self.robots_body)
        if request.url.path in self.redirects:
            return httpx.Response(302, headers={"Location": self.redirects[request.url.path]})
        return httpx.Response(200, text="<html>page</html>")

    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self.handler),
            headers={"User-Agent": UA},
            follow_redirects=True,
        )

    def pages(self) -> list[str]:
        return [c for c in self.calls if not c.endswith("/robots.txt")]


# --- robots gate: the real chd.lu robots.txt ---------------------------------------------------

DISALLOWED = [
    "https://www.chd.lu/fr/dossier/8410",
    "https://www.chd.lu/en/dossier/8410",
    "https://www.chd.lu/de/dossier/7258A",
    "https://www.chd.lu/fr/dossier/8410?tab=documents",
    "https://www.chd.lu/fr/dossier/8410#documents",
    "https://WWW.CHD.LU/fr/dossier/8410",
    "https://www.chd.lu:443/fr/dossier/8410",
    "http://chd.lu/fr/dossier/8410",
    "https://www.chd.lu/fr/techdossier/123",
    "https://www.chd.lu/fr/eurodossier/123",
    "https://www.chd.lu/fr/search",
    "https://www.chd.lu/fr/search?q=logement",
    "https://www.chd.lu/fr/searchfolders?type=PL",
    "https://www.chd.lu/fr/searchactivities",
    "https://www.chd.lu/en/searchagenda?d=2026/01/01",
    "https://www.chd.lu/fr/ajax/feed",
    "https://www.chd.lu/lb/ajax/dossier/8410",
    "https://www.chd.lu/fr/deputies?page=2",
    "https://www.chd.lu/fr/commissions?page=1",
    "https://www.chd.lu/fr/deputy/123?page=3",
    "https://www.chd.lu/fr/glossaire?sort=asc&page=2",
    "https://www.chd.lu/fr/agenda",
    "https://www.chd.lu/fr/seance/42",
]
ALLOWED = [
    "https://www.chd.lu/fr/deputy/123",
    "https://www.chd.lu/fr/deputies",
    "https://www.chd.lu/fr/commission/12",
    "https://www.chd.lu/fr/notice-legale",
    "https://www.chd.lu/fr/open-data",
    "https://www.chd.lu/sitemap.xml",
    "https://www.chd.lu/fr/podcast",
]


@pytest.mark.parametrize("url", DISALLOWED)
def test_polite_get_refuses_disallowed_chd_urls(url, clock):
    site = ChdSite()
    with pytest.raises(SourceError, match="robots"):
        polite_get(site.client(), url)
    assert site.pages() == [], "a disallowed page was requested"


@pytest.mark.parametrize("url", ALLOWED)
def test_polite_get_allows_allowed_chd_pages(url, clock):
    site = ChdSite()
    response = polite_get(site.client(), url)
    assert response.status_code == 200
    assert site.pages() == [url]


def test_robots_txt_is_fetched_once_with_the_user_agent(clock):
    site = ChdSite()
    client = site.client()
    polite_get(client, "https://www.chd.lu/fr/deputies")
    polite_get(client, "https://www.chd.lu/fr/deputy/1")
    with pytest.raises(SourceError):
        polite_get(client, "https://www.chd.lu/fr/dossier/1")
    robots_calls = [c for c in site.calls if c.endswith("/robots.txt")]
    assert robots_calls == ["https://www.chd.lu/robots.txt"]
    assert set(site.agents) == {UA}


@pytest.mark.xfail(
    strict=True,
    reason="BUG: polite_get follows redirects without re-checking robots.txt; an allowed URL "
    "that redirects to /fr/dossier/N fetches the disallowed page",
)
def test_redirect_to_a_disallowed_page_is_not_followed(clock):
    site = ChdSite()
    site.redirects["/fr/deputies"] = "https://www.chd.lu/fr/dossier/8000"
    with pytest.raises(SourceError):
        polite_get(site.client(), "https://www.chd.lu/fr/deputies")
    assert "https://www.chd.lu/fr/dossier/8000" not in site.calls


@pytest.mark.xfail(
    strict=True,
    reason="BUG: percent-encoded unreserved characters are not decoded before matching "
    "(RFC 9309 2.2.2): /fr/%64ossier/8000 (= /fr/dossier/8000) is allowed",
)
def test_percent_encoded_dossier_path_is_refused(clock):
    site = ChdSite()
    with pytest.raises(SourceError):
        polite_get(site.client(), "https://www.chd.lu/fr/%64ossier/8000")


# --- robots.txt fetch failures ------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 500, 502, 503])
def test_robots_http_failure_fails_closed(status, clock):
    site = ChdSite(robots_status=status)
    with pytest.raises(SourceError):
        polite_get(site.client(), "https://www.chd.lu/fr/deputies")
    assert site.pages() == []


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("x")],
    ids=["connect", "timeout", "protocol"],
)
def test_robots_network_failure_fails_closed(exc, clock):
    site = ChdSite()
    site.robots_exc = exc
    with pytest.raises(SourceError):
        polite_get(site.client(), "https://www.chd.lu/fr/deputies")
    assert site.pages() == []


@pytest.mark.xfail(
    strict=True,
    reason="BUG: robots.txt answered with 429 Too Many Requests (rate limit / bot protection) "
    "is treated as 'no robots.txt, everything allowed' instead of failing closed",
)
def test_robots_rate_limited_fails_closed(clock):
    site = ChdSite(robots_status=429)
    with pytest.raises(SourceError):
        polite_get(site.client(), "https://www.chd.lu/fr/dossier/8000")
    assert site.pages() == []


def test_robots_404_means_no_rules_per_rfc(clock):
    """RFC 9309: a 4xx robots.txt (no file) allows everything; documented in robots.py."""
    site = ChdSite(robots_status=404)
    assert polite_get(site.client(), "https://www.chd.lu/fr/deputies").status_code == 200


def test_robots_failure_is_cached_so_retries_stay_closed(clock):
    site = ChdSite(robots_status=503)
    client = site.client()
    for _ in range(2):
        with pytest.raises(SourceError):
            polite_get(client, "https://www.chd.lu/fr/deputies")
    site.robots_status = 200
    with pytest.raises(SourceError):
        polite_get(client, "https://www.chd.lu/fr/deputies")
    assert site.pages() == []


def test_non_chd_hosts_are_not_gated(clock):
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    polite_get(client, "https://data.public.lu/api/1/datasets/x/")
    assert calls == ["https://data.public.lu/api/1/datasets/x/"]


def test_polite_get_http_error_is_a_source_error(clock):
    def handler(request):
        return httpx.Response(429, text="slow down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError, match="429"):
        polite_get(client, "https://data.public.lu/api/1/datasets/x/")


# --- pacing -------------------------------------------------------------------------------------


def test_requests_to_the_same_host_are_spaced(clock):
    site = ChdSite(clock=clock)
    client = site.client()
    for n in range(3):
        polite_get(client, f"https://www.chd.lu/fr/deputy/{n}", delay_seconds=2.0)
    page_times = [t for t, c in zip(site.times, site.calls, strict=True) if "/deputy/" in c]
    assert len(page_times) == 3
    gaps = [b - a for a, b in zip(page_times, page_times[1:], strict=False)]
    assert all(gap >= 2.0 for gap in gaps), gaps


def test_elapsed_time_counts_towards_the_delay(clock):
    def handler(request):
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    polite_get(client, "https://data.public.lu/a", delay_seconds=2.0)
    clock.now += 0.5
    polite_get(client, "https://data.public.lu/b", delay_seconds=2.0)
    clock.now += 5
    polite_get(client, "https://data.public.lu/c", delay_seconds=2.0)
    assert clock.sleeps == [1.5]


def test_different_hosts_are_paced_independently(clock):
    def handler(request):
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    polite_get(client, "https://data.public.lu/a", delay_seconds=3.0)
    polite_get(client, "https://example.org/b", delay_seconds=3.0)
    assert clock.sleeps == []


@pytest.mark.xfail(
    strict=True,
    reason="BUG: the robots.txt request is not paced; the first chd.lu page is requested "
    "immediately after robots.txt on the same host (no delay between the two requests)",
)
def test_robots_fetch_and_first_page_are_spaced(clock):
    site = ChdSite(clock=clock)
    polite_get(site.client(), "https://www.chd.lu/fr/deputies", delay_seconds=2.0)
    assert site.calls[0].endswith("/robots.txt")
    assert site.times[1] - site.times[0] >= 2.0


def test_full_ingestion_is_sequential_and_paced(db, clock):
    dp = DataPublicMock()
    times: list[tuple[float, str]] = []
    in_flight = {"n": 0, "max": 0}
    inner = dp.handler

    def handler(request):
        in_flight["n"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["n"])
        try:
            if request.url.host == "data.public.lu":
                times.append((clock.now, request.url.path))
            return inner(request)
        finally:
            in_flight["n"] -= 1

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    adapters = [Dataset112Adapter(API, 1.0), Dataset121Adapter(API, 1.0), ChdAdapter(API, 2.0)]
    result = run_ingestion(db, None, adapters, client)
    assert result.failed_sources == []
    assert in_flight["max"] == 1
    assert len(times) == 6  # 3 API calls + 3 stable-alias downloads
    gaps = [b[0] - a[0] for a, b in zip(times, times[1:], strict=False)]
    assert all(gap >= 1.0 for gap in gaps), gaps
    # chd's own delay (2 s) applies to its requests
    assert gaps[-1] >= 2.0 and gaps[-2] >= 2.0


# --- User-Agent and settings wiring -------------------------------------------------------------


def test_make_client_sets_user_agent_on_every_ingestion_request(db, clock):
    settings = make_test_settings(http_user_agent=UA, http_timeout_seconds=7.0)
    dp = DataPublicMock()
    agents: list[str | None] = []
    inner = dp.handler

    def handler(request):
        agents.append(request.headers.get("User-Agent"))
        return inner(request)

    client = make_client(settings, transport=httpx.MockTransport(handler))
    assert client.timeout.read == 7.0
    adapters = [Dataset112Adapter(API, 0), Dataset121Adapter(API, 0), ChdAdapter(API, 0)]
    result = run_ingestion(db, None, adapters, client)
    assert result.failed_sources == []
    assert agents and set(agents) == {UA}


def test_default_adapters_use_configured_delays_and_api():
    settings = make_test_settings(
        datapublic_api_url="https://dp.example/api/1",
        datapublic_request_delay_seconds=1.25,
        chd_request_delay_seconds=4.5,
        chd_enabled=True,
    )
    by_name = {a.name: a for a in default_adapters(settings)}
    assert list(by_name) == ["datapublic-112", "datapublic-121", "chd"]
    assert by_name["datapublic-112"].request_delay_seconds == 1.25
    assert by_name["datapublic-121"].request_delay_seconds == 1.25
    assert by_name["chd"].request_delay_seconds == 4.5
    assert all(a.api_url == "https://dp.example/api/1/" for a in by_name.values())


def test_default_politeness_settings_are_not_zero():
    from lexwatch.config import Settings

    defaults = Settings()
    assert defaults.datapublic_request_delay_seconds >= 1
    assert defaults.chd_request_delay_seconds >= 1
    assert defaults.http_user_agent and "lexwatch" in defaults.http_user_agent
