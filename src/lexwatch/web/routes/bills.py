"""Bills list (home) and bill detail (stream 3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from lexwatch.db import get_db
from lexwatch.models import User
from lexwatch.web.deps import require_user
from lexwatch.web.templating import render

router = APIRouter(tags=["bills"])


@router.get("/")
def bills_index(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    return render(request, "bills/index.html", {})


@router.get("/bills/{number}")
def bill_detail(
    number: str, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    return render(request, "bills/detail.html", {"number": number})
