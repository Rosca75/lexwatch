"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lexwatch.config import Settings, get_settings
from lexwatch.db import get_db, get_engine, make_sessionmaker
from lexwatch.i18n import load_catalogs
from lexwatch.logging import configure_logging
from lexwatch.models import User
from lexwatch.web import auth
from lexwatch.web.csrf import verify_csrf
from lexwatch.web.deps import INT32_MAX, SESSION_USER_KEY, NotAuthenticated
from lexwatch.web.routes import admin, bills, health, i18n, rules
from lexwatch.web.templating import make_templates, render

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "lexwatch_session"
SESSION_MAX_AGE = 12 * 3600


log = logging.getLogger(__name__)
access_log = logging.getLogger("lexwatch.access")
QUIET_PREFIXES = ("/static/", "/health")


class AccessLogMiddleware:
    """One ``http.request`` log line per request: method, path, status, duration, user id.

    Replaces uvicorn's access log, which records the client IP address (personal data). The
    query string is left out (search terms); static files and ``/health`` are not logged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(QUIET_PREFIXES):
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            user = (scope.get("state") or {}).get("user")
            user_id = getattr(user, "id", None)
            if user_id is None:
                user_id = (scope.get("session") or {}).get("user_id")
            access_log.info(
                "http.request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                    "user_id": user_id,
                },
            )


def resolve_error_user(request: Request) -> None:
    """Set ``request.state.user`` for an error page, so base.html shows the full top bar.

    A 404 for an unknown path or a 500 never ran the ``current_user`` dependency. This only
    reads the user of the session cookie: no DEV_AUTH auto-login, no session changes. Any
    failure (e.g. the database is down) leaves an anonymous top bar.
    """
    if hasattr(request.state, "user"):
        return
    request.state.user = None
    user_id = request.session.get(SESSION_USER_KEY) if "session" in request.scope else None
    if type(user_id) is not int or not 0 < user_id <= INT32_MAX:
        return
    # Same session source as the routes (tests override get_db).
    provider = request.app.dependency_overrides.get(get_db)
    sessions = provider() if provider is not None else get_db(request)
    try:
        db = next(sessions)
        request.state.user = db.get(User, user_id)
    except Exception:
        log.warning("web.error_page_user_failed", exc_info=True)
    finally:
        sessions.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate()  # refuses DEV_AUTH in production, etc.
    configure_logging(settings.log_level)
    load_catalogs()

    app = FastAPI(
        title="lexwatch",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(verify_csrf)],
    )
    app.state.settings = settings
    app.state.engine = get_engine(settings.database_url)
    app.state.sessionmaker = make_sessionmaker(app.state.engine)
    app.state.templates = make_templates()

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=settings.session_https_only or settings.is_production,
    )
    # Outermost, so it sees the final status, the session and request.state.user.
    app.add_middleware(AccessLogMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for router in (
        health.router,
        auth.router,
        i18n.router,
        bills.router,
        rules.router,
        admin.router,
    ):
        app.include_router(router)

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if request.headers.get("HX-Request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse(getattr(exc, "login_url", None) or "/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith("/static") or "text/html" not in request.headers.get(
            "accept", ""
        ):
            from fastapi.exception_handlers import http_exception_handler

            return await http_exception_handler(request, exc)
        # ``detail`` is a short code for known errors (CSRF raises ``detail="csrf"``); the
        # template maps it to an i18n message (``error.csrf``) and never prints it raw.
        detail = exc.detail if isinstance(exc.detail, str) else None
        resolve_error_user(request)
        return render(
            request,
            "error.html",
            {"status_code": exc.status_code, "detail": detail},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _server_error(request: Request, exc: Exception) -> Response:
        # Starlette re-raises the exception after this response, so it is still logged.
        if "text/html" not in request.headers.get("accept", ""):
            return PlainTextResponse("Internal Server Error", status_code=500)
        try:
            resolve_error_user(request)
            return render(request, "error.html", {"status_code": 500, "detail": None}, 500)
        except Exception:
            log.exception("web.error_page_failed")
            return PlainTextResponse("Internal Server Error", status_code=500)

    return app
