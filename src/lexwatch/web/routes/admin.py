"""Admin page (stream 3): last runs, Run now, Rescore all, CSV export, monthly billed units.

"Run now" inserts a ``runs`` row with ``kind='manual', status='queued'`` (see
``jobs.daily.create_run``); "Rescore all" sets every profile's ``rescore_status='pending'``.
The worker picks both up. CSV exports are UTF-8 with a BOM so Excel opens them correctly.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lexwatch.db import get_db
from lexwatch.jobs.daily import create_run
from lexwatch.models import Bill, Match, Profile, RuleScore, Run, User
from lexwatch.scoring.factory import get_scorer
from lexwatch.web.deps import require_admin
from lexwatch.web.routes.bills import bill_order, format_dt
from lexwatch.web.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

RUNS_SHOWN = 20


def _month_start(request: Request) -> datetime:
    tz = ZoneInfo(request.app.state.settings.timezone)
    now = datetime.now(tz)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _duration(run: Run) -> str:
    if run.started_at is None or run.finished_at is None:
        return ""
    seconds = int((run.finished_at - run.started_at).total_seconds())
    minutes, seconds = divmod(max(seconds, 0), 60)
    return f"{minutes} min {seconds:02d} s" if minutes else f"{seconds} s"


def _stale_scores(request: Request, db: Session) -> int | None:
    """Rule scores computed with another model than the configured one (None if unknown)."""
    try:
        model = get_scorer(request.app.state.settings).model_name
    except Exception:
        return None
    return db.scalar(select(func.count(RuleScore.id)).where(RuleScore.model != model)) or 0


@router.get("")
def admin_index(
    request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    runs = list(
        db.scalars(select(Run).order_by(Run.created_at.desc(), Run.id.desc()).limit(RUNS_SHOWN))
    )
    run_rows = [
        {
            "run": r,
            "time": format_dt(r.started_at or r.created_at, request),
            "duration": _duration(r),
        }
        for r in runs
    ]

    sources_run = next((r for r in runs if (r.details or {}).get("sources")), None)
    sources: list[dict[str, Any]] = []
    if sources_run is not None:
        for name, info in sorted(sources_run.details["sources"].items()):
            info = info if isinstance(info, dict) else {}
            sources.append(
                {
                    "name": name,
                    "status": str(info.get("status") or ""),
                    "records": info.get("records"),
                    "error": str(info.get("error") or ""),
                }
            )

    month_start = _month_start(request)
    month_units = (
        db.scalar(
            select(func.coalesce(func.sum(Run.billed_units), 0)).where(
                Run.started_at >= month_start
            )
        )
        or 0
    )
    unit = None
    for details in db.scalars(
        select(Run.details).where(Run.started_at >= month_start).order_by(Run.started_at.desc())
    ):
        billing = (details or {}).get("billing")
        if isinstance(billing, dict) and billing.get("unit"):
            unit = str(billing["unit"])
            break

    state_counts = dict(
        db.execute(select(Bill.score_state, func.count()).group_by(Bill.score_state)).all()
    )
    context = {
        "runs": run_rows,
        "sources": sources,
        "sources_time": format_dt(sources_run.started_at or sources_run.created_at, request)
        if sources_run
        else "",
        "bills_total": sum(state_counts.values()),
        "bills_pending": state_counts.get("pending", 0),
        "bills_unscored": state_counts.get("unscored", 0),
        "stale_scores": _stale_scores(request, db),
        "month_units": month_units,
        "unit": unit,
        "queued": any(r.status == "queued" for r in runs),
        "rescoring": db.scalar(
            select(func.count(Profile.id)).where(Profile.rescore_status != "idle")
        )
        or 0,
    }
    return render(request, "admin/index.html", context)


@router.post("/run")
def run_now(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    already = db.scalar(select(func.count(Run.id)).where(Run.status == "queued"))
    if not already:
        run = create_run(db, "manual", requested_by=user.id)
        db.commit()
        log.info("admin.run_queued", extra={"run_id": run.id, "user_id": user.id})
    return RedirectResponse("/admin", status_code=303)


@router.post("/rescore")
def rescore_all(
    request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    db.execute(
        update(Profile).where(Profile.rescore_status == "idle").values(rescore_status="pending")
    )
    db.commit()
    log.info("admin.rescore_all", extra={"user_id": user.id})
    return RedirectResponse("/admin", status_code=303)


def _csv_stream(header: list[str], rows: Iterable[Iterable[Any]]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    buffer.write("﻿")
    writer.writerow(header)
    for i, row in enumerate(rows, 1):
        writer.writerow(["" if v is None else _cell(v) for v in row])
        if i % 500 == 0:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()


def _cell(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.4f}"
    return value


def _csv_response(filename: str, header: list[str], rows: list[Any]) -> StreamingResponse:
    return StreamingResponse(
        _csv_stream(header, rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


BILL_COLUMNS = [
    "number",
    "type",
    "status",
    "title",
    "filing_date",
    "closing_date",
    "author",
    "dossier_url",
    "source",
    "score_state",
    "first_seen_at",
    "last_changed_at",
]


@router.get("/export/bills.csv")
def export_bills(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    # Rows are materialized before streaming: the DB session closes with the request scope.
    columns = [getattr(Bill, c) for c in BILL_COLUMNS]
    rows = db.execute(select(*columns).order_by(*bill_order())).all()
    return _csv_response("bills.csv", BILL_COLUMNS, rows)


MATCH_COLUMNS = [
    "profile_id",
    "profile_name",
    "bill_number",
    "bill_title",
    "profile_score",
    "matched",
    "computed_at",
]


@router.get("/export/matches.csv")
def export_matches(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            Profile.id,
            Profile.name,
            Match.bill_number,
            Bill.title,
            Match.profile_score,
            Match.matched,
            Match.computed_at,
        )
        .join(Profile, Profile.id == Match.profile_id)
        .join(Bill, Bill.number == Match.bill_number)
        .order_by(func.lower(Profile.name), Profile.id, Match.profile_score.desc())
    ).all()
    return _csv_response("matches.csv", MATCH_COLUMNS, rows)
