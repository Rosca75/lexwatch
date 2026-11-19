"""Request dependencies: current user, guards, language.

The authenticated user's id lives in the signed session cookie (``session["user_id"]``).
With ``DEV_AUTH`` on (never in production) an anonymous request is logged in as the fixed dev
user automatically. Guards raise ``NotAuthenticated`` (handled in ``app.py``: redirect to
/login, or ``HX-Redirect`` for htmx) or 403.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.db import get_db
from lexwatch.i18n import normalize_lang
from lexwatch.models import User

SESSION_USER_KEY = "user_id"
SESSION_LANG_KEY = "lang"
DEV_USERS = {
    "admin": ("dev-admin", "admin@lexwatch.local", "Dev Admin"),
    "user": ("dev-user", "user@lexwatch.local", "Dev User"),
}


class NotAuthenticated(Exception):
    pass


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_or_create_dev_user(db: Session, role: str) -> User:
    sub, email, name = DEV_USERS["admin" if role == "admin" else "user"]
    user = db.scalar(select(User).where(User.oidc_sub == sub))
    if user is None:
        user = User(oidc_sub=sub, email=email, name=name, role=role, language="fr")
        db.add(user)
        db.commit()
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """The logged-in user or ``None``. Also stored on ``request.state.user`` for templates."""
    if hasattr(request.state, "user"):
        return request.state.user
    settings: Settings = request.app.state.settings
    user: User | None = None
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is not None:
        user = db.get(User, user_id)
        if user is None:
            request.session.pop(SESSION_USER_KEY, None)
    if user is None and settings.dev_auth and not settings.is_production:
        user = get_or_create_dev_user(db, settings.dev_auth_role)
        request.session[SESSION_USER_KEY] = user.id
    request.state.user = user
    return user


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="forbidden")
    return user


def get_lang(request: Request) -> str:
    """User preference, else session choice, else default language (fr)."""
    user = getattr(request.state, "user", None)
    if user is not None and user.language:
        return normalize_lang(user.language)
    settings: Settings = request.app.state.settings
    return normalize_lang(request.session.get(SESSION_LANG_KEY) or settings.default_language)
