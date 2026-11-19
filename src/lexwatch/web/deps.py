"""Request dependencies: current user, guards, language, session start.

The authenticated user's id lives in the signed session cookie (``session["user_id"]``).
With ``DEV_AUTH`` on (never in production) an anonymous request is logged in as the fixed dev
user automatically. Guards raise ``NotAuthenticated`` (handled in ``app.py``: redirect to
/login, or ``HX-Redirect`` for htmx) or 403.

A session whose user no longer exists (deleted user) is cleared on the next request.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.db import get_db
from lexwatch.i18n import LANGUAGES, normalize_lang
from lexwatch.models import Profile, User

log = logging.getLogger(__name__)

SESSION_USER_KEY = "user_id"
SESSION_LANG_KEY = "lang"
SESSION_CSRF_KEY = "csrf_token"  # same key as lexwatch.web.csrf.SESSION_KEY
SESSION_LOGIN_AT_KEY = "login_at"
DEV_USERS = {
    "admin": ("dev-admin", "admin@lexwatch.local", "Dev Admin"),
    "user": ("dev-user", "user@lexwatch.local", "Dev User"),
}


INT32_MAX = 2**31 - 1  # users.id is a PostgreSQL integer


class NotAuthenticated(Exception):
    """Raised by ``require_user``. ``login_url`` is ``/login?next=<requested local path>``."""

    def __init__(self, login_url: str = "/login") -> None:
        super().__init__(login_url)
        self.login_url = login_url


def login_url_for(request: Request) -> str:
    """``/login`` with ``next`` set to the requested path (GET only; local paths only)."""
    if request.method != "GET" or request.headers.get("HX-Request"):
        return "/login"
    path = request.url.path
    if request.url.query:
        path += "?" + request.url.query
    if not path.startswith("/") or path.startswith(("//", "/\\")) or path == "/":
        return "/login"
    return "/login?" + urlencode({"next": path})


def utcnow() -> datetime:
    return datetime.now(UTC)


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def dev_auth_enabled(settings: Settings) -> bool:
    """DEV_AUTH is honoured only outside production (``Settings.validate`` also refuses it)."""
    return settings.dev_auth and not settings.is_production


def get_or_create_dev_user(db: Session, role: str) -> User:
    """The fixed DEV_AUTH user of ``role`` (flushed, not committed)."""
    role = "admin" if role == "admin" else "user"
    sub, email, name = DEV_USERS[role]
    user = db.scalar(select(User).where(User.oidc_sub == sub))
    if user is None:
        user = User(oidc_sub=sub, email=email, name=name, role=role, language="fr")
        db.add(user)
        db.flush()
    elif user.role != role:
        user.role = role
    return user


def record_visit(user: User, now: datetime | None = None) -> None:
    """A new session started: shift ``last_visit_at`` into ``previous_visit_at``."""
    user.previous_visit_at = user.last_visit_at
    user.last_visit_at = now or utcnow()


def start_session(request: Request, user: User, now: datetime | None = None) -> None:
    """Start a fresh session for ``user`` (session rotation) and record the visit.

    Everything in the old session is dropped (OIDC state, old CSRF token, ...) except the
    anonymous language choice. A new CSRF token is issued. The caller commits the user row.
    """
    lang = request.session.get(SESSION_LANG_KEY)
    request.session.clear()
    if lang in LANGUAGES:
        request.session[SESSION_LANG_KEY] = lang
    now = now or utcnow()
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_CSRF_KEY] = secrets.token_urlsafe(32)
    request.session[SESSION_LOGIN_AT_KEY] = int(now.timestamp())
    record_visit(user, now)
    request.state.user = user


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """The logged-in user or ``None``. Also stored on ``request.state.user`` for templates."""
    if hasattr(request.state, "user"):
        return request.state.user
    settings: Settings = request.app.state.settings
    user: User | None = None
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is not None:
        valid = type(user_id) is int and 0 < user_id <= INT32_MAX
        user = db.get(User, user_id) if valid else None
        if user is None:
            # Deleted user (or unexpected value): invalidate the whole session.
            log.info("auth.session_invalidated", extra={"user_id": user_id})
            request.session.clear()
    if user is None and dev_auth_enabled(settings):
        user = get_or_create_dev_user(db, settings.dev_auth_role)
        start_session(request, user)
        db.commit()
    request.state.user = user
    return user


def require_user(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise NotAuthenticated(login_url_for(request))
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="forbidden")
    return user


def can_manage_profile(user: User | None, profile: Profile) -> bool:
    """Users manage their own profiles; admins manage all of them."""
    if user is None:
        return False
    return user.is_admin or profile.owner_id == user.id


def get_lang(request: Request) -> str:
    """User preference, else session choice, else default language (fr)."""
    user = getattr(request.state, "user", None)
    if user is not None and user.language:
        return normalize_lang(user.language)
    settings: Settings = request.app.state.settings
    return normalize_lang(request.session.get(SESSION_LANG_KEY) or settings.default_language)
