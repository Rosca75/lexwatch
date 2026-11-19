"""Liveness and readiness probes (no auth, no CSRF impact: GET only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from lexwatch.db import get_db
from lexwatch.scoring.factory import scorer_configured

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    checks: dict[str, bool] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False
    checks["scorer"] = scorer_configured(request.app.state.settings)
    ok = all(checks.values())
    return JSONResponse({"status": "ok" if ok else "unavailable", **checks}, 200 if ok else 503)
