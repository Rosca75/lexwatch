"""Jinja2 environment and the ``render`` helper used by all routes.

Template globals (per request): ``t(key, **fmt)`` bound to the request language,
``bill_label(number, type)`` ("Projet de loi n° 8627"), ``lang``,
``csrf_token()``, ``current_user`` (``User | None``), ``settings``, ``request``.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from lexwatch.i18n import LANGUAGES, bill_label, t
from lexwatch.web.csrf import get_csrf_token
from lexwatch.web.deps import get_lang

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _context(request: Request) -> dict[str, Any]:
    lang = get_lang(request)
    return {
        "lang": lang,
        "languages": LANGUAGES,
        "t": partial(t, lang=lang),
        "bill_label": partial(bill_label, lang=lang),
        "csrf_token": partial(get_csrf_token, request),
        "current_user": getattr(request.state, "user", None),
        "settings": request.app.state.settings,
    }


def make_templates() -> Jinja2Templates:
    return Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_context])


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request, name, context or {}, status_code=status_code, headers=headers
    )
