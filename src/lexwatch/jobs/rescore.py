"""Profile rescoring recorded as a ``runs`` row (``kind='rescore'``).

Used by the worker (pending profiles) and by ``lexwatch rescore``. Each profile rescore gets
its own run so its billed units count in the admin page's monthly total. The run's status:
``success`` when every request succeeded, ``partial`` when some bills were scored and some
failed, ``failed`` when nothing was scored or the rescore raised. Bills or pairs left unscored
are retried by the next daily run (``score_pending_bills``); the rescore itself is not retried.
"""

from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.models import Profile, Run
from lexwatch.scoring.base import Scorer

log = logging.getLogger(__name__)


def execute_rescore(
    session: Session,
    settings: Settings,
    profile_id: int,
    *,
    scorer: Scorer | None = None,
    requested_by: int | None = None,
) -> Run:
    """Rescore one profile inside a new ``runs`` row; commits. Never raises for scoring errors.

    ``scorer`` may be shared across several calls (CLI): billed units are counted as the delta
    on the scorer during this call.
    """
    from lexwatch.jobs.daily import utcnow
    from lexwatch.scoring.factory import get_scorer
    from lexwatch.scoring.service import rescore_profile

    run = Run(
        kind="rescore",
        status="running",
        requested_by=requested_by,
        started_at=utcnow(),
        details={"profile_id": profile_id},
    )
    session.add(run)
    session.commit()

    scorer = scorer or get_scorer(settings)
    before = int(getattr(scorer, "billed_units", 0) or 0)
    try:
        result = rescore_profile(session, scorer, profile_id, run=run)
        run.bills_scored, run.bills_unscored = result.scored, result.unscored
        if result.error is None:
            run.status = "success"
        else:
            run.status = "partial" if result.scored else "failed"
            run.error_message = result.error
        session.commit()
    except Exception as exc:
        session.rollback()
        log.exception("rescore.failed", extra={"run_id": run.id, "profile_id": profile_id})
        run.status = "failed"
        run.error_message = repr(exc)

    run.billed_units = int(getattr(scorer, "billed_units", 0) or 0) - before
    unit = getattr(scorer, "billing_unit", None)
    if unit:
        run.details = {**(run.details or {}), "billing": {"unit": unit}}
    run.finished_at = utcnow()
    session.commit()
    log.info(
        "rescore.finished",
        extra={"run_id": run.id, "profile_id": profile_id, "status": run.status},
    )
    return run


def finish_rescore_status(session: Session, profile_id: int) -> bool:
    """Compare-and-set ``running -> idle``; commits. Returns True when the row was updated.

    A profile saved again while it was being rescored is back to ``pending`` and stays so,
    so the worker rescores it once more with the new rules.
    """
    result = session.execute(
        update(Profile)
        .where(Profile.id == profile_id, Profile.rescore_status == "running")
        # A status flip is not a user edit: keep updated_at (the queue order) unchanged.
        .values(rescore_status="idle", updated_at=Profile.updated_at)
        .execution_options(synchronize_session="fetch")
    )
    session.commit()
    return bool(result.rowcount)


def mark_all_pending(session: Session) -> int:
    """Queue every profile for the worker (``rescore_status='pending'``); commits."""
    result = session.execute(
        update(Profile)
        .values(rescore_status="pending")
        .execution_options(synchronize_session="fetch")
    )
    session.commit()
    return int(result.rowcount or 0)
