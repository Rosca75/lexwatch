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


def polite_get(client: httpx.Client, url: str, *, delay_seconds: float = 0.0) -> httpx.Response:
    """GET ``url`` politely: per-host delay, redirects followed, robots.txt gate for chd.lu.

    Raises ``SourceError`` when robots.txt forbids the URL or the response is not 2xx, and
    lets transport errors surface as ``SourceError`` too so adapters fail cleanly.
    """
    from lexwatch.sources.base import SourceError
    from lexwatch.sources.robots import is_allowed

    host = (urlsplit(url).hostname or "").lower()
    if host in ROBOTS_GATED_HOSTS:
        agent = client.headers.get("User-Agent", "lexwatch")
        if not is_allowed(client, url, agent):
            raise SourceError(f"robots.txt disallows {url}")
    _pace(host, delay_seconds)
    try:
        response = client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise SourceError(f"GET {url} failed: {exc!r}") from exc
    if response.status_code >= 400:
        raise SourceError(f"GET {url} returned HTTP {response.status_code}")
    return response
