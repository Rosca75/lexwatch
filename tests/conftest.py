"""Shared test fixtures.

- ``settings``: test Settings (DEV_AUTH on, fake scorer, test DB).
- ``engine``: session-scoped; the test DB schema is dropped and rebuilt with
  ``alembic upgrade head`` once per test session.
- ``db``: a SQLAlchemy Session inside an outer transaction rolled back after each test.
  ``session.commit()`` in code under test only releases a SAVEPOINT.
- ``app`` / ``client``: the FastAPI app wired to ``db``; ``client`` is logged in as the DEV_AUTH
  admin automatically. ``admin_client`` / ``user_client`` log in explicitly by role.
- Helpers (``tests/helpers.py``): ``make_test_settings``, ``login_as``, ``csrf_token``.
- ``fake_scorer``: deterministic ``FakeScorer``.

Set ``TEST_DATABASE_URL`` to point at another database. Never point it at real data.
"""

from __future__ import annotations

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
from tests.helpers import TEST_DATABASE_URL, login_as, make_test_settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


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


@pytest.fixture
def admin_client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield login_as(c, "admin")


@pytest.fixture
def user_client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield login_as(c, "user")


@pytest.fixture
def fake_scorer() -> FakeScorer:
    return FakeScorer()
