"""OIDC login (Authlib), logout and DEV_AUTH login (stream 4).

- ``GET /login``: with DEV_AUTH, ``?as=admin|user`` logs in the fixed dev user of that role;
  otherwise redirects to the OIDC provider (``authorize_redirect``).
- ``GET /auth/callback``: exchange code, upsert ``users`` by ``sub``, map role from
  ``settings.oidc_role_claim`` / ``oidc_admin_value``, store ``session["user_id"]``.
- ``POST /logout``: clear the session (CSRF-protected).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.db import get_db
from lexwatch.web.deps import SESSION_USER_KEY, get_or_create_dev_user, get_settings_dep

router = APIRouter(tags=["auth"])


@router.get("/login")
def login(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    if settings.dev_auth and not settings.is_production:
        role = request.query_params.get("as") or settings.dev_auth_role
        user = get_or_create_dev_user(db, "admin" if role == "admin" else "user")
        request.session.clear()
        request.session[SESSION_USER_KEY] = user.id
        return RedirectResponse("/", status_code=303)
    raise NotImplementedError("stream 4: OIDC authorize redirect")


@router.get("/auth/callback")
def auth_callback(request: Request, db: Session = Depends(get_db)):
    raise NotImplementedError("stream 4: OIDC callback")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
