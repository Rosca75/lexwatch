"""Daily job orchestration: ingestion -> scoring -> digests, recorded in a ``runs`` row.

Stub wiring owned by the coordinator; streams 1, 2 and 4 provide the steps. Each step is
isolated: a failing step marks the run ``partial`` (or ``failed`` when nothing worked) and the
job continues where it makes sense (no ingestion failure prevents scoring pending bills).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
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


INGEST_KINDS = ("daily", "manual")


def carries_bootstrap(session: Session, run: Run) -> bool:
    """True when ``run`` must also count as a bootstrap run for the digest.

    That is the case when the most recent earlier daily/manual run was a bootstrap run that
    did not finish with ``success`` and did not complete its scoring step
    (``details["scoring_complete"]``): the run that finishes the initial scoring (or the next
    one, if that one is interrupted too) computes the first matches of every historical bill,
    and those must not be reported as news. A bootstrap run that scored everything but had a
    failing source or digest does not carry the flag (otherwise a source that keeps failing
    would suppress digests forever).
    """
    stmt = (
        select(Run)
        .where(
            Run.kind.in_(INGEST_KINDS),
            Run.id != run.id,
            Run.started_at.is_not(None),
            Run.status != "queued",
        )
        .order_by(Run.started_at.desc(), Run.id.desc())
        .limit(1)
    )
    if run.started_at is not None:
        stmt = stmt.where(Run.started_at <= run.started_at)
    previous = session.scalars(stmt).first()
    if previous is None:
        return False
    details = previous.details or {}
    return (
        bool(details.get("bootstrap"))
        and previous.status != "success"
        and not details.get("scoring_complete")
    )


def execute_run(session: Session, run: Run, settings: Settings) -> Run:
    """Execute an existing run row: ingestion, scoring, digests; finalize status and counters.

    Commits after each step so the admin page shows progress, and during scoring after each
    batch (``details["progress"]``). An ingestion into an empty ``bills`` table is marked
    ``details["bootstrap"] = true`` by ``run_ingestion``: the digest ignores what it added.
    A run that follows an interrupted bootstrap run carries the flag (``carries_bootstrap``).
    """
    from lexwatch.ingest.pipeline import run_ingestion
    from lexwatch.notify.digest import send_digests_report
    from lexwatch.scoring.factory import get_scorer
    from lexwatch.scoring.service import score_pending_bills

    run.status = "running"
    run.started_at = utcnow()
    if run.kind in INGEST_KINDS and carries_bootstrap(session, run):
        run.details = {**(run.details or {}), "bootstrap": True}
        log.info("job.bootstrap_carried", extra={"run_id": run.id})
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
        # Commits after each scoring batch: a crash keeps the scores already computed, and
        # run.details["progress"] shows how far it is on the admin page.
        scored = score_pending_bills(session, scorer, run, commit=session.commit)
        run.bills_scored, run.bills_unscored = scored.scored, scored.unscored
        if scored.error:
            errors["scoring"] = scored.error
        else:
            steps_ok += 1
            run.details = {**(run.details or {}), "scoring_complete": True}
        session.commit()
    except Exception as exc:
        session.rollback()
        log.exception("job.scoring_failed", extra={"run_id": run.id})
        errors["scoring"] = repr(exc)
    run.billed_units = int(getattr(scorer, "billed_units", 0) or 0)
    billing_unit = getattr(scorer, "billing_unit", None)
    if billing_unit:  # also when scoring raised after some billed requests
        run.details = {**(run.details or {}), "billing": {"unit": billing_unit}}
    session.commit()

    if settings.digest_enabled:
        try:
            report = send_digests_report(session, settings=settings)
            run.details = {
                **(run.details or {}),
                "digests_sent": report.sent,
                "digests_failed": len(report.failed),
                "digests_skipped": report.skipped,
            }
            if report.failed:
                errors["digest"] = f"{len(report.failed)} failed: " + "; ".join(
                    f"profile {pid}: {err}" for pid, err in sorted(report.failed.items())
                )
            else:
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
