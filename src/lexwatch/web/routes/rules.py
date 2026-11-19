"""Profiles and rules management (stream 3).

Saving rules or the threshold must set ``profile.rescore_status='pending'`` (the worker
rescores). Users manage their own profiles; admins manage all.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from lexwatch.db import get_db
from lexwatch.models import User
from lexwatch.web.deps import require_user
from lexwatch.web.templating import render

router = APIRouter(prefix="/rules", tags=["rules"])


@router.get("")
def rules_index(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    return render(request, "rules/index.html", {})
