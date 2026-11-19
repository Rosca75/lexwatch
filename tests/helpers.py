"""Test helpers importable from any test module (``from tests.helpers import ...``)."""

from __future__ import annotations

import os
import re
from dataclasses import replace

from fastapi.testclient import TestClient

from lexwatch.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://lexwatch:lexwatch@localhost:55432/lexwatch_test",
)
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def make_test_settings(**overrides: object) -> Settings:
    base = Settings(
        env="test",
        log_level="WARNING",
        base_url="http://testserver",
        secret_key="test-secret",
        database_url=TEST_DATABASE_URL,
        dev_auth=True,
        dev_auth_role="admin",
        scorer="fake",
        default_threshold=0.5,
        smtp_host="",
        chd_request_delay_seconds=0.0,
    )
    return replace(base, **overrides) if overrides else base


def login_as(client: TestClient, role: str) -> TestClient:
    """Log the client in as the DEV_AUTH user of ``role`` ('admin' or 'user')."""
    response = client.get(f"/login?as={role}", follow_redirects=False)
    assert response.status_code == 303, response.text
    return client


def csrf_token(client: TestClient, path: str = "/") -> str:
    """Return the CSRF token of the client's session (parsed from a rendered page)."""
    response = client.get(path)
    match = _CSRF_RE.search(response.text)
    assert match, f"no csrf_token field on {path}"
    return match.group(1)
