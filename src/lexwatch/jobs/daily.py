"""Daily job orchestration: ingestion -> scoring -> digests, recorded in a ``runs`` row.

Stub wiring owned by the coordinator; streams 1, 2 and 4 provide the steps. Each step is
isolated: a failing step marks the run ``partial`` (or ``failed`` when nothing worked) and the
job continues where it makes sense (no ingestion failure prevents scoring pending bills).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.models import Run

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def create_run(session: Session, kind: str, requested_by: int | None = None) -> Run:
    """Insert a run in ``queued`` state (admin "Run now" uses this, then commits)."""
    run = Run(kind=kind, status="queued", requested_by=requested_by, details={})
    session.add(run)
    session.flush()
    return run


def execute_run(session: Session, run: Run, settings: Settings) -> Run:
    """Execute an existing run row: ingestion, scoring, digests; finalize status and counters.

    Commits after each step so the admin page shows progress.
    """
    from lexwatch.ingest.pipeline import run_ingestion
    from lexwatch.notify.digest import send_digests
    from lexwatch.scoring.factory import get_scorer
    from lexwatch.scoring.service import score_pending_bills

    run.status = "running"
    run.started_at = utcnow()
    session.commit()

    errors: dict[str, str] = {}
    steps_ok = 0

    try:
        ingest = run_ingestion(session, run)
        run.bills_added, run.bills_changed = ingest.added, ingest.changed
        run.details = {**(run.details or {}), "sources": ingest.sources}
        if ingest.failed_sources:
            errors["ingestion"] = "failed sources: " + ", ".join(ingest.failed_sources)
        else:
            steps_ok += 1
        session.commit()
    except Exception as exc:
        session.rollback()
        log.exception("job.ingestion_failed", extra={"run_id": run.id})
        errors["ingestion"] = repr(exc)

    scorer = get_scorer(settings)
    try:
        scored = score_pending_bills(session, scorer, run)
        run.bills_scored, run.bills_unscored = scored.scored, scored.unscored
        if scored.error:
            errors["scoring"] = scored.error
        else:
            steps_ok += 1
        session.commit()
    except Exception as exc:
        session.rollback()
        log.exception("job.scoring_failed", extra={"run_id": run.id})
        errors["scoring"] = repr(exc)
    run.billed_units = int(getattr(scorer, "billed_units", 0) or 0)

    if settings.digest_enabled:
        try:
            sent = send_digests(session)
            run.details = {**(run.details or {}), "digests_sent": sent}
            steps_ok += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            log.exception("job.digest_failed", extra={"run_id": run.id})
            errors["digest"] = repr(exc)

    run.finished_at = utcnow()
    if not errors:
        run.status = "success"
    elif steps_ok == 0:
        run.status = "failed"
    else:
        run.status = "partial"
    run.error_message = "; ".join(f"{k}: {v}" for k, v in errors.items()) or None
    session.commit()
    log.info("job.finished", extra={"run_id": run.id, "status": run.status})
    return run


def run_daily_job(settings: Settings, kind: str = "daily", session: Session | None = None) -> int:
    """Create a run row and execute it. Returns the run id."""
    from lexwatch.db import get_engine, make_sessionmaker

    own = session is None
    session = session or make_sessionmaker(get_engine(settings.database_url))()
    try:
        run = create_run(session, kind)
        session.commit()
        execute_run(session, run, settings)
        return run.id
    finally:
        if own:
            session.close()
