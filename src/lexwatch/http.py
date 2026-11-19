"""Shared outbound HTTP client factory (User-Agent, timeouts, proxy from env) and polite GET."""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

import httpx

from lexwatch.config import Settings

# Hosts whose pages are only fetched after a robots.txt check (sources.robots).
ROBOTS_GATED_HOSTS = frozenset({"www.chd.lu", "chd.lu"})

_pace_lock = threading.Lock()
_last_request: dict[str, float] = {}


def make_client(settings: Settings, **kwargs: object) -> httpx.Client:
    """httpx client with the configured User-Agent and timeout.

    ``trust_env=True`` (httpx default) honours ``HTTPS_PROXY``/``NO_PROXY`` from the environment.
    """
    headers = {"User-Agent": settings.http_user_agent}
    return httpx.Client(
        headers=headers,
        timeout=settings.http_timeout_seconds,
        follow_redirects=True,
        **kwargs,  # type: ignore[arg-type]
    )


def _pace(host: str, delay_seconds: float) -> None:
    """Sleep so that two requests to ``host`` are at least ``delay_seconds`` apart."""
    with _pace_lock:
        last = _last_request.get(host)
        if last is not None and delay_seconds > 0:
            wait = delay_seconds - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        _last_request[host] = time.monotonic()


MAX_REDIRECTS = 10


def polite_get(client: httpx.Client, url: str, *, delay_seconds: float = 0.0) -> httpx.Response:
    """GET ``url`` politely: per-host delay, robots.txt gate for chd.lu, redirects followed.

    Redirects are followed manually, one hop at a time, so every hop is paced and a hop to a
    robots-gated host is checked against robots.txt before it is requested. The robots.txt
    download itself is paced like any other request to that host.

    Raises ``SourceError`` when robots.txt forbids a URL, the response is not 2xx, there are
    too many redirects, or the transport fails.
    """
    from lexwatch.sources.base import SourceError

    current = url
    for _ in range(MAX_REDIRECTS + 1):
        host = (urlsplit(current).hostname or "").lower()
        _check_robots(client, current, host, delay_seconds)
        _pace(host, delay_seconds)
        try:
            response = client.get(current, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise SourceError(f"GET {current} failed: {exc!r}") from exc
        if response.is_redirect and response.next_request is not None:
            current = str(response.next_request.url)
            continue
        if response.status_code >= 400:
            raise SourceError(f"GET {current} returned HTTP {response.status_code}")
        return response
    raise SourceError(f"GET {url}: more than {MAX_REDIRECTS} redirects")


def _check_robots(client: httpx.Client, url: str, host: str, delay_seconds: float) -> None:
    from lexwatch.sources import robots
    from lexwatch.sources.base import SourceError

    if host not in ROBOTS_GATED_HOSTS:
        return
    agent = client.headers.get("User-Agent", "lexwatch")
    if not robots.is_cached(url):
        _pace(host, delay_seconds)  # the robots.txt request counts as a request to the host
    if not robots.is_allowed(client, url, agent):
        raise SourceError(f"robots.txt disallows {url}")
