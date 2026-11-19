"""Shared test fixtures.

- ``settings``: test Settings (DEV_AUTH on, fake scorer, test DB).
- ``engine``: session-scoped; the test DB schema is dropped and rebuilt with
  ``alembic upgrade head`` once per test session.
- ``db``: a SQLAlchemy Session inside an outer transaction rolled back after each test.
  ``session.commit()`` in code under test only releases a SAVEPOINT.
- ``app`` / ``client``: the FastAPI app wired to ``db``; ``client`` is logged in as the DEV_AUTH
  admin automatically. ``admin_client`` / ``user_client`` log in explicitly by role.
- ``csrf_token(client)``: helper returning the session's CSRF token (parsed from a page).
- ``fake_scorer``: deterministic ``FakeScorer``.

Set ``TEST_DATABASE_URL`` to point at another database. Never point it at real data.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.db import get_db
from lexwatch.scoring.fake import FakeScorer
from lexwatch.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
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
    from dataclasses import replace

    return replace(base, **overrides) if overrides else base


@pytest.fixture(scope="session")
def settings() -> Settings:
    return make_test_settings()


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DATABASE_URL, future=True)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def app(settings: Settings, db: Session) -> FastAPI:
    application = create_app(settings)

    def _get_db() -> Iterator[Session]:
        yield db

    application.dependency_overrides[get_db] = _get_db
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def login_as(client: TestClient, role: str) -> TestClient:
    """Log the client in as the DEV_AUTH user of ``role`` ('admin' or 'user')."""
    response = client.get(f"/login?as={role}", follow_redirects=False)
    assert response.status_code == 303, response.text
    return client


@pytest.fixture
def admin_client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield login_as(c, "admin")


@pytest.fixture
def user_client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield login_as(c, "user")


def csrf_token(client: TestClient, path: str = "/") -> str:
    """Return the CSRF token of the client's session (parsed from a rendered page)."""
    response = client.get(path)
    match = _CSRF_RE.search(response.text)
    assert match, f"no csrf_token field on {path}"
    return match.group(1)


@pytest.fixture
def fake_scorer() -> FakeScorer:
    return FakeScorer()
