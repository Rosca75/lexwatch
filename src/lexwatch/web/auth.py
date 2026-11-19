"""OIDC login (Authlib), logout and DEV_AUTH login (stream 4).

- ``GET /login``: with DEV_AUTH, ``?as=admin|user`` logs in the fixed dev user of that role.
  Otherwise redirects to the OIDC provider (authorization code flow with PKCE S256, ``state``
  and ``nonce``; Authlib keeps them in the session). ``?next=/path`` is honoured for local paths.
- ``GET /auth/callback``: exchanges the code, validates the ID token (signature via JWKS,
  ``iss``, ``aud``, ``exp``, ``nonce``), upserts ``users`` by ``sub`` (email, name and role on
  every login), then starts a fresh session (see ``deps.start_session``). Any failure renders
  a friendly error page; details go to the log only.
- ``POST /logout`` (CSRF-protected): clears the session; with ``OIDC_END_SESSION`` and a provider
  ``end_session_endpoint``, also signs out at the provider (RP-initiated logout) and comes back
  to ``/logged-out``. Register ``{BASE_URL}/logged-out`` as post-logout redirect URI.

Provider discovery: ``{OIDC_ISSUER}/.well-known/openid-configuration``. Works with Entra ID
(tenant-specific issuer ``https://login.microsoftonline.com/<tid>/v2.0``; ``roles`` app-role
claim or ``groups`` object ids) and Keycloak (``realm_access.roles``, ``resource_access.<c>.roles``
or ``groups``). The role claim may be a list or a string and may be a dotted path into nested
claims. It is read from the ID token, then the userinfo endpoint, then the access token payload
(Keycloak puts ``realm_access`` only there by default; the token comes straight from the token
endpoint over TLS).

Outbound calls use httpx with ``trust_env`` so ``HTTPS_PROXY``/``NO_PROXY`` apply. Tests can set
``app.state.oidc_transport`` to an ``httpx.MockTransport`` before the first request.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from lexwatch.config import Settings
from lexwatch.db import get_db
from lexwatch.i18n import LANGUAGES
from lexwatch.models import User
from lexwatch.web.deps import (
    SESSION_LANG_KEY,
    SESSION_USER_KEY,
    dev_auth_enabled,
    get_or_create_dev_user,
    get_settings_dep,
    start_session,
)
from lexwatch.web.templating import render

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

SESSION_NEXT_KEY = "login_next"
LOGGED_OUT_PATH = "/logged-out"


# --- helpers -------------------------------------------------------------------------------


def oidc_configured(settings: Settings) -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id)


def discovery_url(settings: Settings) -> str:
    return settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"


def build_oidc_client(settings: Settings, transport: Any = None) -> Any:
    """An Authlib Starlette OAuth2 app registered from the provider's discovery document."""
    client_kwargs: dict[str, Any] = {
        "scope": settings.oidc_scopes,
        "code_challenge_method": "S256",
        "timeout": settings.http_timeout_seconds,
        "headers": {"User-Agent": settings.http_user_agent},
        "trust_env": True,  # HTTPS_PROXY / NO_PROXY / SSL_CERT_FILE
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    oauth = OAuth()
    oauth.register(
        "oidc",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret or None,
        server_metadata_url=discovery_url(settings),
        client_kwargs=client_kwargs,
    )
    return oauth.create_client("oidc")


def get_oidc_client(request: Request) -> Any:
    """The app's OIDC client, built once (it caches discovery metadata and JWKS)."""
    state = request.app.state
    client = getattr(state, "oidc_client", None)
    if client is None:
        client = build_oidc_client(state.settings, getattr(state, "oidc_transport", None))
        state.oidc_client = client
    return client


def safe_next(value: str | None) -> str | None:
    """Accept only local absolute paths (no scheme, no ``//host``)."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return None
    if any(c in value for c in "\r\n"):
        return None
    return value


def claim_values(claims: Mapping[str, Any], path: str) -> list[str] | None:
    """Values of the claim at ``path`` (dotted for nested claims); ``None`` when absent.

    A literal key containing dots (e.g. a URI-named claim) wins over the nested lookup.
    A string value is split on commas and whitespace.
    """
    if not path:
        return None
    if path in claims:
        node: Any = claims[path]
    else:
        node = claims
        for part in path.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            else:
                return None
    if node is None:
        return None
    if isinstance(node, str):
        return [v for v in node.replace(",", " ").split() if v]
    if isinstance(node, list | tuple | set):
        return [str(v) for v in node if v is not None and not isinstance(v, Mapping | list)]
    if isinstance(node, Mapping):
        return [str(k) for k in node]
    return [str(node)]


def admin_values(settings: Settings) -> set[str]:
    return {v.strip().casefold() for v in settings.oidc_admin_value.split(",") if v.strip()}


def role_from_values(values: list[str] | None, settings: Settings) -> str:
    wanted = admin_values(settings)
    if values and wanted and any(v.casefold() in wanted for v in values):
        return "admin"
    return "user"


def _jwt_payload(token: str) -> dict[str, Any] | None:
    """Payload of a JWT access token, unverified (only used for role claims)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


async def resolve_role_values(
    client: Any, token: Mapping[str, Any], claims: Mapping[str, Any], settings: Settings
) -> list[str] | None:
    path = settings.oidc_role_claim
    values = claim_values(claims, path)
    if values is not None:
        return values
    metadata = getattr(client, "server_metadata", {}) or {}
    if metadata.get("userinfo_endpoint"):
        try:
            userinfo = await client.userinfo(token=dict(token))
            values = claim_values(userinfo, path)
        except Exception as exc:  # optional source, the login still works
            log.warning("auth.userinfo_failed", extra={"error": type(exc).__name__})
        if values is not None:
            return values
    access_token = token.get("access_token")
    if isinstance(access_token, str):
        payload = _jwt_payload(access_token)
        if payload is not None:
            return claim_values(payload, path)
    return None


def _email_from_claims(claims: Mapping[str, Any]) -> str | None:
    for key in ("email", "preferred_username", "upn", "unique_name"):
        value = claims.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip()[:320]
    return None


def upsert_user(db: Session, claims: Mapping[str, Any], role: str, lang: str | None) -> User:
    sub = str(claims["sub"])
    email = _email_from_claims(claims)
    name = claims.get("name") or claims.get("preferred_username") or email
    user = db.scalar(select(User).where(User.oidc_sub == sub))
    if user is None:
        user = User(oidc_sub=sub, language=lang if lang in LANGUAGES else "fr")
        db.add(user)
    user.email = email
    user.name = str(name)[:255] if name else None
    user.role = role
    db.flush()
    return user


def auth_error(request: Request, key: str, status_code: int = 400) -> Response:
    return render(
        request,
        "auth/error.html",
        {"message_key": key, "status_code": status_code},
        status_code=status_code,
    )


# --- routes --------------------------------------------------------------------------------


@router.get("/login")
async def login(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    next_path = safe_next(request.query_params.get("next"))
    if dev_auth_enabled(settings):
        role = request.query_params.get("as") or settings.dev_auth_role
        user = get_or_create_dev_user(db, role)
        start_session(request, user)
        db.commit()
        log.info("auth.login", extra={"user_id": user.id, "method": "dev"})
        return RedirectResponse(next_path or "/", status_code=303)

    if not oidc_configured(settings):
        return auth_error(request, "auth.error.not_configured", 503)
    if next_path:
        request.session[SESSION_NEXT_KEY] = next_path
    try:
        client = get_oidc_client(request)
        return await client.authorize_redirect(request, settings.effective_oidc_redirect_uri)
    except Exception as exc:  # discovery/network errors: friendly page
        log.error("auth.login_redirect_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        return auth_error(request, "auth.error.provider", 502)


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    if not oidc_configured(settings):
        return auth_error(request, "auth.error.not_configured", 503)
    client = get_oidc_client(request)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        log.warning(
            "auth.callback_rejected",
            extra={"error": exc.error, "description": exc.description},
        )
        key = "auth.error.denied" if exc.error == "access_denied" else "auth.error.failed"
        return auth_error(request, key, 400)
    except Exception as exc:  # token/JWKS/ID-token validation failures
        log.error("auth.callback_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        return auth_error(request, "auth.error.failed", 400)

    claims = dict(token.get("userinfo") or {})
    if not claims.get("sub"):
        log.warning("auth.callback_no_id_token")
        return auth_error(request, "auth.error.failed", 400)

    values = await resolve_role_values(client, token, claims, settings)
    role = role_from_values(values, settings)
    next_path = safe_next(request.session.get(SESSION_NEXT_KEY))
    user = upsert_user(db, claims, role, request.session.get(SESSION_LANG_KEY))
    start_session(request, user)
    db.commit()
    log.info(
        "auth.login",
        extra={"user_id": user.id, "email": user.email, "role": role, "method": "oidc"},
    )
    return RedirectResponse(next_path or "/", status_code=303)


@router.post("/logout")
async def logout(request: Request, settings: Settings = Depends(get_settings_dep)):
    user_id = request.session.get(SESSION_USER_KEY)
    lang = request.session.get(SESSION_LANG_KEY)
    request.session.clear()
    if lang in LANGUAGES:
        request.session[SESSION_LANG_KEY] = lang
    log.info("auth.logout", extra={"user_id": user_id})
    target = LOGGED_OUT_PATH
    if settings.oidc_end_session and oidc_configured(settings) and not dev_auth_enabled(settings):
        try:
            metadata = await get_oidc_client(request).load_server_metadata()
            end_session = metadata.get("end_session_endpoint")
        except Exception as exc:  # local logout still succeeded
            log.warning("auth.end_session_unavailable", extra={"error": type(exc).__name__})
            end_session = None
        if end_session:
            params = {
                "client_id": settings.oidc_client_id,
                "post_logout_redirect_uri": settings.base_url.rstrip("/") + LOGGED_OUT_PATH,
            }
            sep = "&" if "?" in end_session else "?"
            target = f"{end_session}{sep}{urlencode(params)}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


@router.get(LOGGED_OUT_PATH)
def logged_out(request: Request):
    return render(request, "auth/logged_out.html", {})
