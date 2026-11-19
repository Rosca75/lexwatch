"""Admin page (stream 3): last runs, Run now, Rescore all, CSV export, monthly billed units.

"Run now" inserts a ``runs`` row with ``kind='manual', status='queued'`` (see
``jobs.daily.create_run``); "Rescore all" sets every profile's ``rescore_status='pending'``.
The worker picks both up.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from lexwatch.db import get_db
from lexwatch.models import User
from lexwatch.web.deps import require_admin
from lexwatch.web.templating import render

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("")
def admin_index(
    request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    return render(request, "admin/index.html", {})
