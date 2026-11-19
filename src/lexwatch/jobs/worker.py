"""Worker loop (no APScheduler/Celery/Redis).

Every ``WORKER_POLL_SECONDS`` it checks, in order:
(a) the daily job: local time (``APP_TIMEZONE``) >= ``DAILY_JOB_TIME`` and no ``daily`` run
    started today (local date) -> run it;
(b) runs with ``status='queued'`` (admin "Run now") -> claim with ``SELECT ... FOR UPDATE SKIP
    LOCKED`` and execute;
(c) profiles with ``rescore_status='pending'`` -> set ``running``, rescore inside a
    ``runs(kind='rescore')`` row (``jobs.rescore.execute_rescore``), then set ``idle`` only if
    still ``running`` (compare-and-set: a profile saved during the rescore stays ``pending``).
    A failed rescore is recorded in its run and not retried here; its unscored bills/pairs are
    picked up by the next daily run.
Only one worker replica is expected; the row locks make a second one harmless.
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.models import Profile, Run

log = logging.getLogger(__name__)


def parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def daily_due(session: Session, settings: Settings, now: datetime | None = None) -> bool:
    """True when the daily time has passed today (local tz) and no daily run exists today."""
    tz = ZoneInfo(settings.timezone)
    local_now = (now or datetime.now(tz)).astimezone(tz)
    if local_now.time() < parse_hhmm(settings.daily_job_time):
        return False
    start_of_day = datetime.combine(local_now.date(), time(0, 0), tzinfo=tz)
    existing = session.scalar(
        select(Run.id).where(Run.kind == "daily", Run.created_at >= start_of_day).limit(1)
    )
    return existing is None


def claim_queued_run(session: Session) -> Run | None:
    stmt = (
        select(Run)
        .where(Run.status == "queued")
        .order_by(Run.created_at, Run.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return session.scalars(stmt).first()


def claim_pending_rescore(session: Session) -> Profile | None:
    stmt = (
        select(Profile)
        .where(Profile.rescore_status == "pending")
        .order_by(Profile.updated_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return session.scalars(stmt).first()


def tick(session: Session, settings: Settings) -> None:
    """One polling iteration (testable)."""
    from lexwatch.jobs.daily import create_run, execute_run
    from lexwatch.jobs.rescore import execute_rescore, finish_rescore_status

    if daily_due(session, settings):
        run = create_run(session, "daily")
        session.commit()
        execute_run(session, run, settings)

    run = claim_queued_run(session)
    if run is not None:
        execute_run(session, run, settings)

    profile = claim_pending_rescore(session)
    if profile is not None:
        profile_id = profile.id
        profile.rescore_status = "running"
        session.commit()
        try:
            execute_rescore(session, settings, profile_id)
        except Exception:
            session.rollback()
            log.exception("worker.rescore_failed", extra={"profile_id": profile_id})
        finally:
            finish_rescore_status(session, profile_id)


def run_worker(settings: Settings) -> None:
    from lexwatch.db import get_engine, make_sessionmaker

    factory = make_sessionmaker(get_engine(settings.database_url))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker.started", extra={"poll_seconds": settings.worker_poll_seconds})
    while not stop.is_set():
        try:
            with factory() as session:
                tick(session, settings)
        except Exception:
            log.exception("worker.tick_failed")
        stop.wait(settings.worker_poll_seconds)
    log.info("worker.stopped")
