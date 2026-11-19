"""Shared outbound HTTP client factory (User-Agent, timeouts, proxy from env)."""

from __future__ import annotations

import httpx

from lexwatch.config import Settings


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
