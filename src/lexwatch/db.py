"""Database engine, sessions and the FastAPI dependency."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from lexwatch.config import get_settings


@lru_cache(maxsize=8)
def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    return create_engine(url, pool_pre_ping=True, future=True)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Transactional scope for jobs/CLI: commit on success, rollback on error."""
    factory = factory or make_sessionmaker(get_engine())
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency. Uses the sessionmaker stored on ``app.state`` by ``create_app``.

    Routes must ``commit()`` explicitly after writes; the session is closed (and any
    uncommitted work rolled back) when the request ends. Tests override this dependency.
    """
    factory: sessionmaker[Session] = request.app.state.sessionmaker
    session = factory()
    try:
        yield session
    finally:
        session.close()
