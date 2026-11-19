"""Profile rescoring recorded as a ``runs`` row (``kind='rescore'``).

Used by the worker (pending profiles, queued "Rescore all" runs) and by ``lexwatch rescore``.

"Rescore all" (admin button, ``lexwatch rescore --all``) queues one run
``runs(kind='rescore', status='queued', details={"force": true})`` with ``queue_rescore_all``
and sets every idle profile to ``pending`` so the UI shows it in progress. The worker claims
that run like any queued run and executes it with ``execute_rescore_all``: every profile is
rescored with ``force=True`` (every bill is sent again, whatever the stored scores), inside
that single run, using the same ``running -> idle`` compare-and-set per profile as a normal
rescore. No schema change is needed: the run row carries the "force" flag. Each profile rescore gets
its own run so its billed units count in the admin page's monthly total. The run's status:
``success`` when every request succeeded, ``partial`` when some bills were scored and some
failed, ``failed`` when nothing was scored or the rescore raised. Bills or pairs left unscored
are retried by the next daily run (``score_pending_bills``); the rescore itself is not retried.

A worker stop request (``jobs.stopping``) ends a rescore after the current batch: the run is
``failed`` with "interrupted (worker stopping)", ``WorkerStopping`` propagates to the worker,
and the profile stays ``running`` until ``recover_interrupted`` sets it back to ``pending``.
"""

from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.jobs.daily import mark_stopped
from lexwatch.jobs.stopping import WorkerStopping, commit_or_stop
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
        result = rescore_profile(
            session, scorer, profile_id, run=run, commit=commit_or_stop(session)
        )
        run.bills_scored, run.bills_unscored = result.scored, result.unscored
        if result.error is None:
            run.status = "success"
        else:
            run.status = "partial" if result.scored else "failed"
            run.error_message = result.error
        session.commit()
    except WorkerStopping:
        session.rollback()
        run.billed_units = int(getattr(scorer, "billed_units", 0) or 0) - before
        mark_stopped(session, run)
        raise
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


def queue_rescore_all(session: Session, requested_by: int | None = None) -> Run | None:
    """Queue a forced rescore of every profile; commits. ``None`` when one is already queued.

    Also sets every idle profile to ``pending`` (shown as in progress on the Rules page).
    """
    from sqlalchemy import select

    from lexwatch.jobs.daily import create_run

    already = session.scalar(
        select(Run.id).where(Run.kind == "rescore", Run.status == "queued").limit(1)
    )
    if already is not None:
        return None
    run = create_run(session, "rescore", requested_by=requested_by)
    run.details = {"force": True}
    session.execute(
        update(Profile)
        .where(Profile.rescore_status == "idle")
        .values(rescore_status="pending", updated_at=Profile.updated_at)
        .execution_options(synchronize_session="fetch")
    )
    session.commit()
    return run


def execute_rescore_all(
    session: Session, settings: Settings, run: Run, *, scorer: Scorer | None = None
) -> Run:
    """Execute a queued "Rescore all" run: force-rescore every profile; commits.

    Billed units, scored and unscored counts add up over the profiles. Status: ``success``
    when every profile rescored cleanly, ``failed`` when none scored anything and some
    failed, ``partial`` otherwise. Never raises for scoring errors.
    """
    from sqlalchemy import select

    from lexwatch.jobs.daily import utcnow
    from lexwatch.scoring.factory import get_scorer
    from lexwatch.scoring.service import rescore_profile

    run.status = "running"
    run.started_at = utcnow()
    session.commit()

    scorer = scorer or get_scorer(settings)
    before = int(getattr(scorer, "billed_units", 0) or 0)
    profile_ids = list(session.scalars(select(Profile.id).order_by(Profile.id)))
    errors: list[str] = []
    scored = unscored = 0
    for profile_id in profile_ids:
        session.execute(
            update(Profile)
            .where(Profile.id == profile_id)
            .values(rescore_status="running", updated_at=Profile.updated_at)
            .execution_options(synchronize_session="fetch")
        )
        session.commit()
        try:
            result = rescore_profile(
                session, scorer, profile_id, force=True, run=run, commit=commit_or_stop(session)
            )
            scored += result.scored
            unscored += result.unscored
            if result.error:
                errors.append(f"profile {profile_id}: {result.error}")
            session.commit()
        except WorkerStopping:
            # The profile stays ``running``: recover_interrupted sets it back to ``pending``
            # at the next start, like the profiles this run did not reach yet.
            session.rollback()
            run.bills_scored, run.bills_unscored = scored, unscored
            run.billed_units = int(getattr(scorer, "billed_units", 0) or 0) - before
            mark_stopped(session, run)
            raise
        except Exception as exc:
            session.rollback()
            log.exception("rescore.failed", extra={"run_id": run.id, "profile_id": profile_id})
            errors.append(f"profile {profile_id}: {exc!r}")
        finish_rescore_status(session, profile_id)

    run.bills_scored, run.bills_unscored = scored, unscored
    if not errors:
        run.status = "success"
    else:
        run.status = "partial" if scored else "failed"
        run.error_message = "; ".join(errors)
    run.billed_units = int(getattr(scorer, "billed_units", 0) or 0) - before
    unit = getattr(scorer, "billing_unit", None)
    run.details = {
        **(run.details or {}),
        "profiles": len(profile_ids),
        **({"billing": {"unit": unit}} if unit else {}),
    }
    run.finished_at = utcnow()
    session.commit()
    log.info(
        "rescore.all_finished",
        extra={"run_id": run.id, "profiles": len(profile_ids), "status": run.status},
    )
    return run


def recompute_default_threshold_matches(session: Session, settings: Settings) -> int:
    """Recompute matches of profiles without their own threshold; commits. No scoring requests.

    Run at worker startup so a change of ``DEFAULT_THRESHOLD`` (or of the scorer's default)
    takes effect on existing matches without a rescore. Returns the number of profiles done.
    """
    from sqlalchemy import select

    from lexwatch.scoring.service import recompute_profile_matches

    ids = list(
        session.scalars(select(Profile.id).where(Profile.threshold.is_(None)).order_by(Profile.id))
    )
    for profile_id in ids:
        recompute_profile_matches(session, profile_id, settings)
    session.commit()
    if ids:
        log.info("rescore.default_threshold_recomputed", extra={"profiles": len(ids)})
    return len(ids)


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
