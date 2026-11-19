"""CSRF protection (stream 4).

- One random token per session (``request.session["csrf_token"]``), exposed to templates as
  ``csrf_token()``. A new token is issued whenever a new login session starts.
- ``verify_csrf`` is installed as a global app dependency: every POST/PUT/PATCH/DELETE must carry
  the token in form field ``csrf_token`` or header ``X-CSRF-Token`` (htmx: ``hx-headers`` on
  ``<body>``), else 403. Tokens are compared in constant time.
- Defense in depth: when the browser sends an ``Origin`` header on an unsafe request, its host
  must be the ``BASE_URL`` host or the request's own ``Host``. A missing ``Origin`` is allowed
  (older browsers, non-browser clients); the token check still applies.
- htmx requests that fail get ``HX-Refresh: true`` so the page reloads with a fresh token
  (htmx does not swap 4xx responses).
"""

from __future__ import annotations

import hmac
import logging
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

SESSION_KEY = "csrf_token"
FORM_FIELD = "csrf_token"
HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def get_csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


def tokens_match(expected: object, supplied: object) -> bool:
    if not isinstance(expected, str) or not isinstance(supplied, str):
        return False
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected.encode(), supplied.encode())


def origin_allowed(request: Request) -> bool:
    """True when there is no ``Origin`` header or it names this site."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if origin == "null":
        return False
    host = urlsplit(origin).netloc.lower()
    allowed = {request.headers.get("host", "").lower()}
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.base_url:
        allowed.add(urlsplit(settings.base_url).netloc.lower())
    return bool(host) and host in allowed


def _reject(request: Request, reason: str) -> HTTPException:
    log.info("csrf.rejected", extra={"reason": reason, "path": request.url.path})
    headers = {"HX-Refresh": "true"} if request.headers.get("HX-Request") else None
    return HTTPException(status_code=403, detail="csrf", headers=headers)


async def verify_csrf(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    if not origin_allowed(request):
        raise _reject(request, "origin")
    expected = request.session.get(SESSION_KEY)
    supplied: object = request.headers.get(HEADER)
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(FORM_TYPES):
            form = await request.form()
            supplied = form.get(FORM_FIELD)
    if not tokens_match(expected, supplied):
        raise _reject(request, "token")
