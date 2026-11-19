"""CSRF protection (stream 4 owns; minimal working version from the skeleton).

- One random token per session (``request.session["csrf_token"]``), exposed to templates as
  ``csrf_token()``.
- ``verify_csrf`` is installed as a global app dependency: every POST/PUT/PATCH/DELETE must carry
  the token in form field ``csrf_token`` or header ``X-CSRF-Token`` (htmx: ``hx-headers`` on
  ``<body>``), else 403.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request

SESSION_KEY = "csrf_token"
FORM_FIELD = "csrf_token"
HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def get_csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get(SESSION_KEY)
    supplied = request.headers.get(HEADER)
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get(FORM_FIELD)
            supplied = value if isinstance(value, str) else None
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="csrf")
