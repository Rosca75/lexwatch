"""Worker loop (no APScheduler/Celery/Redis).

Every ``WORKER_POLL_SECONDS`` it checks, in order:
(a) the daily job: local time (``APP_TIMEZONE``) >= ``DAILY_JOB_TIME`` and no ``daily`` run
    started today (local date) -> run it;
(b) runs with ``status='queued'`` -> claim with ``SELECT ... FOR UPDATE SKIP LOCKED`` and
    execute: ``manual`` (admin "Run now") runs the daily job, ``rescore`` (admin "Rescore
    all", ``details={"force": true}``) force-rescores every profile
    (``jobs.rescore.execute_rescore_all``);
(c) profiles with ``rescore_status='pending'`` -> set ``running``, rescore inside a
    ``runs(kind='rescore')`` row (``jobs.rescore.execute_rescore``), then set ``idle`` only if
    still ``running`` (compare-and-set: a profile saved during the rescore stays ``pending``).
    A failed rescore is recorded in its run and not retried here; its unscored bills/pairs are
    picked up by the next daily run.
Only one worker replica is expected; the row locks make a second one harmless.

At startup the worker also recomputes the matches of profiles without their own threshold
(no scoring requests), so a changed ``DEFAULT_THRESHOLD`` or scorer default takes effect.
"""

from __future__ import annotations

import logging
import signal
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
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
    from lexwatch.jobs.rescore import execute_rescore, execute_rescore_all, finish_rescore_status
    from lexwatch.jobs.stopping import WorkerStopping, raise_if_stopping

    if daily_due(session, settings):
        run = create_run(session, "daily")
        session.commit()
        execute_run(session, run, settings)

    raise_if_stopping()
    run = claim_queued_run(session)
    if run is not None:
        if run.kind == "rescore":
            execute_rescore_all(session, settings, run)
        else:
            execute_run(session, run, settings)

    raise_if_stopping()
    profile = claim_pending_rescore(session)
    if profile is not None:
        profile_id = profile.id
        profile.rescore_status = "running"
        session.commit()
        try:
            execute_rescore(session, settings, profile_id)
        except WorkerStopping:
            raise  # the profile stays running -> pending again at the next start
        except Exception:
            session.rollback()
            log.exception("worker.rescore_failed", extra={"profile_id": profile_id})
        finish_rescore_status(session, profile_id)


INTERRUPTED = "interrupted (worker restarted)"


def recover_interrupted(session: Session) -> tuple[int, int]:
    """Clean up after a worker that died mid-work; call once at startup. Commits.

    Profiles left ``running`` go back to ``pending`` (rescored again), and runs left
    ``running`` become ``failed`` with ``INTERRUPTED``. Only one worker replica is expected,
    so nothing else can be running at this point. Returns ``(profiles, runs)`` reset.
    """
    from lexwatch.jobs.daily import utcnow

    profiles = session.execute(
        update(Profile)
        .where(Profile.rescore_status == "running")
        .values(rescore_status="pending", updated_at=Profile.updated_at)
        .execution_options(synchronize_session="fetch")
    ).rowcount
    runs = session.execute(
        update(Run)
        .where(Run.status == "running")
        .values(status="failed", error_message=INTERRUPTED, finished_at=utcnow())
        .execution_options(synchronize_session="fetch")
    ).rowcount
    session.commit()
    if profiles or runs:
        log.warning("worker.recovered", extra={"profiles": profiles, "runs": runs})
    return int(profiles or 0), int(runs or 0)


def run_worker(settings: Settings) -> None:
    from lexwatch.db import get_engine, make_sessionmaker
    from lexwatch.jobs.stopping import WorkerStopping, clear_stop, request_stop, stop_event

    factory = make_sessionmaker(get_engine(settings.database_url))
    # The handler only sets a flag: a long scoring run checks it after each committed batch
    # (jobs.stopping.commit_or_stop) and stops, so `docker compose stop` needs no SIGKILL.
    clear_stop()
    stop = stop_event()

    def _on_signal(signum: int, _frame: object) -> None:
        if not stop.is_set():
            log.info("worker.stop_requested", extra={"signal": signal.Signals(signum).name})
        request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)
    log.info("worker.started", extra={"poll_seconds": settings.worker_poll_seconds})
    try:
        with factory() as session:
            recover_interrupted(session)
    except Exception:
        log.exception("worker.recover_failed")
    try:
        from lexwatch.jobs.rescore import recompute_default_threshold_matches

        with factory() as session:
            recompute_default_threshold_matches(session, settings)
    except Exception:
        log.exception("worker.recompute_matches_failed")
    while not stop.is_set():
        try:
            with factory() as session:
                tick(session, settings)
        except WorkerStopping:
            break
        except Exception:
            log.exception("worker.tick_failed")
        stop.wait(settings.worker_poll_seconds)
    log.info("worker.stopped")
