"""Language switch: POST /lang (form: lang, next). Remembered per user and in the session."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from lexwatch.db import get_db
from lexwatch.i18n import normalize_lang
from lexwatch.models import User
from lexwatch.web.deps import SESSION_LANG_KEY, current_user

router = APIRouter(tags=["i18n"])


def _safe_next(value: str | None) -> str:
    if not value:
        return "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@router.post("/lang")
def set_language(
    request: Request,
    lang: str = Form(...),
    next: str | None = Form(None),
    user: User | None = Depends(current_user),
    db: Session = Depends(get_db),
):
    lang = normalize_lang(lang)
    request.session[SESSION_LANG_KEY] = lang
    if user is not None:
        user.language = lang
        db.commit()
    return RedirectResponse(_safe_next(next), status_code=303)
