"""Application settings.

All configuration comes from environment variables (see ``.env.example``). Settings are a
frozen dataclass so they can be passed around and overridden in tests with
``dataclasses.replace``. No pydantic-settings: parsing is explicit and small.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_DATABASE_URL = "postgresql+psycopg://lexwatch:lexwatch@localhost:5432/lexwatch"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


def _float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    return float(value)


def _str(value: str | None, default: str = "") -> str:
    if value is None:
        return default
    return value.strip()


@dataclass(frozen=True)
class Settings:
    # --- general -------------------------------------------------------------------------
    env: str = "development"  # development | test | production
    log_level: str = "INFO"
    base_url: str = "http://localhost:8000"  # public URL, used in email links and OIDC
    secret_key: str = "change-me"  # signs the session cookie
    session_https_only: bool = False  # Secure cookie flag; must be true in production
    default_language: str = "fr"

    # --- database ------------------------------------------------------------------------
    database_url: str = DEFAULT_DATABASE_URL

    # --- auth ----------------------------------------------------------------------------
    dev_auth: bool = False
    dev_auth_role: str = "admin"  # role of the fixed DEV_AUTH user
    oidc_issuer: str = ""  # e.g. https://login.microsoftonline.com/<tenant>/v2.0
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""  # default: {base_url}/auth/callback
    oidc_scopes: str = "openid email profile"
    oidc_role_claim: str = "roles"  # claim holding groups/roles (list or string)
    oidc_admin_value: str = "lexwatch-admin"  # value of role_claim that grants admin

    # --- sources / http ------------------------------------------------------------------
    http_user_agent: str = "lexwatch/0.1 (+internal bill tracker)"
    http_timeout_seconds: float = 30.0
    chd_enabled: bool = True
    chd_request_delay_seconds: float = 2.0
    datapublic_api_url: str = "https://data.public.lu/api/1/"
    https_proxy: str = ""  # httpx also honours HTTPS_PROXY from the environment

    # --- scoring -------------------------------------------------------------------------
    scorer: str = "foundry"  # foundry | local | fake
    # Profile threshold used when a profile has none. When DEFAULT_THRESHOLD is not set, the
    # scorer's own default applies (see lexwatch.scoring.factory.default_threshold_for);
    # ``default_threshold_explicit`` records whether the operator set it.
    default_threshold: float = 0.5
    default_threshold_explicit: bool = False
    scoring_max_chars: int = 4000
    foundry_endpoint: str = (
        ""  # https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank
    )
    foundry_model: str = "Cohere-rerank-v5-fast"
    foundry_api_key: str = ""  # if empty, Entra ID (azure-identity) is used
    foundry_token_scope: str = "https://cognitiveservices.azure.com/.default"
    foundry_timeout_seconds: float = 30.0
    foundry_max_retries: int = 4
    foundry_max_documents: int = 100
    foundry_api_key_header: str = "Authorization"  # Authorization (Bearer) | api-key
    foundry_max_tokens_per_doc: int = 0  # 0 = omit (server default 4096)
    foundry_circuit_threshold: int = 3  # consecutive failed requests that open the breaker
    local_model_dir: str = "/models/bge-reranker-v2-m3"
    local_model_repo: str = "onnx-community/bge-reranker-v2-m3-ONNX"
    local_model_revision: str = "6f5ff65298512715a1e669753bc754d2bc8f367b"
    local_model_file: str = "onnx/model_int8.onnx"
    local_max_length: int = 512
    local_batch_size: int = 8

    # --- email ---------------------------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_from: str = "lexwatch@localhost"
    digest_enabled: bool = True

    # --- worker --------------------------------------------------------------------------
    daily_job_time: str = "06:30"  # HH:MM, local time in `timezone`
    timezone: str = "Europe/Luxembourg"
    worker_poll_seconds: int = 15

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def effective_oidc_redirect_uri(self) -> str:
        return self.oidc_redirect_uri or f"{self.base_url.rstrip('/')}/auth/callback"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if environ is None else environ
        d = cls()
        return cls(
            env=_str(e.get("ENV"), d.env),
            log_level=_str(e.get("LOG_LEVEL"), d.log_level),
            base_url=_str(e.get("BASE_URL"), d.base_url),
            secret_key=_str(e.get("SECRET_KEY"), d.secret_key),
            session_https_only=_bool(e.get("SESSION_HTTPS_ONLY"), d.session_https_only),
            default_language=_str(e.get("DEFAULT_LANGUAGE"), d.default_language),
            database_url=_str(e.get("DATABASE_URL"), d.database_url),
            dev_auth=_bool(e.get("DEV_AUTH"), d.dev_auth),
            dev_auth_role=_str(e.get("DEV_AUTH_ROLE"), d.dev_auth_role),
            oidc_issuer=_str(e.get("OIDC_ISSUER")),
            oidc_client_id=_str(e.get("OIDC_CLIENT_ID")),
            oidc_client_secret=_str(e.get("OIDC_CLIENT_SECRET")),
            oidc_redirect_uri=_str(e.get("OIDC_REDIRECT_URI")),
            oidc_scopes=_str(e.get("OIDC_SCOPES"), d.oidc_scopes),
            oidc_role_claim=_str(e.get("OIDC_ROLE_CLAIM"), d.oidc_role_claim),
            oidc_admin_value=_str(e.get("OIDC_ADMIN_VALUE"), d.oidc_admin_value),
            http_user_agent=_str(e.get("HTTP_USER_AGENT"), d.http_user_agent),
            http_timeout_seconds=_float(e.get("HTTP_TIMEOUT_SECONDS"), d.http_timeout_seconds),
            chd_enabled=_bool(e.get("CHD_ENABLED"), d.chd_enabled),
            chd_request_delay_seconds=_float(
                e.get("CHD_REQUEST_DELAY_SECONDS"), d.chd_request_delay_seconds
            ),
            datapublic_api_url=_str(e.get("DATAPUBLIC_API_URL"), d.datapublic_api_url),
            https_proxy=_str(e.get("HTTPS_PROXY")),
            scorer=_str(e.get("SCORER"), d.scorer).lower(),
            default_threshold=_float(e.get("DEFAULT_THRESHOLD"), d.default_threshold),
            default_threshold_explicit=bool(_str(e.get("DEFAULT_THRESHOLD"))),
            scoring_max_chars=_int(e.get("SCORING_MAX_CHARS"), d.scoring_max_chars),
            foundry_endpoint=_str(e.get("FOUNDRY_ENDPOINT")),
            foundry_model=_str(e.get("FOUNDRY_MODEL"), d.foundry_model),
            foundry_api_key=_str(e.get("FOUNDRY_API_KEY")),
            foundry_token_scope=_str(e.get("FOUNDRY_TOKEN_SCOPE"), d.foundry_token_scope),
            foundry_timeout_seconds=_float(
                e.get("FOUNDRY_TIMEOUT_SECONDS"), d.foundry_timeout_seconds
            ),
            foundry_max_retries=_int(e.get("FOUNDRY_MAX_RETRIES"), d.foundry_max_retries),
            foundry_max_documents=_int(e.get("FOUNDRY_MAX_DOCUMENTS"), d.foundry_max_documents),
            foundry_api_key_header=_str(e.get("FOUNDRY_API_KEY_HEADER"), d.foundry_api_key_header),
            foundry_max_tokens_per_doc=_int(
                e.get("FOUNDRY_MAX_TOKENS_PER_DOC"), d.foundry_max_tokens_per_doc
            ),
            foundry_circuit_threshold=_int(
                e.get("FOUNDRY_CIRCUIT_THRESHOLD"), d.foundry_circuit_threshold
            ),
            local_model_dir=_str(e.get("LOCAL_MODEL_DIR"), d.local_model_dir),
            local_model_repo=_str(e.get("LOCAL_MODEL_REPO"), d.local_model_repo),
            local_model_revision=_str(e.get("LOCAL_MODEL_REVISION"), d.local_model_revision),
            local_model_file=_str(e.get("LOCAL_MODEL_FILE"), d.local_model_file),
            local_max_length=_int(e.get("LOCAL_MAX_LENGTH"), d.local_max_length),
            local_batch_size=_int(e.get("LOCAL_BATCH_SIZE"), d.local_batch_size),
            smtp_host=_str(e.get("SMTP_HOST")),
            smtp_port=_int(e.get("SMTP_PORT"), d.smtp_port),
            smtp_username=_str(e.get("SMTP_USERNAME")),
            smtp_password=_str(e.get("SMTP_PASSWORD")),
            smtp_starttls=_bool(e.get("SMTP_STARTTLS"), d.smtp_starttls),
            smtp_ssl=_bool(e.get("SMTP_SSL"), d.smtp_ssl),
            smtp_from=_str(e.get("SMTP_FROM"), d.smtp_from),
            digest_enabled=_bool(e.get("DIGEST_ENABLED"), d.digest_enabled),
            daily_job_time=_str(e.get("DAILY_JOB_TIME"), d.daily_job_time),
            timezone=_str(e.get("APP_TIMEZONE"), d.timezone),
            worker_poll_seconds=_int(e.get("WORKER_POLL_SECONDS"), d.worker_poll_seconds),
        )

    def validate(self) -> None:
        """Raise ``ValueError`` for configurations that must never run."""
        if self.is_production and self.dev_auth:
            raise ValueError("DEV_AUTH must not be enabled when ENV=production")
        if self.is_production and self.secret_key in {"", "change-me"}:
            raise ValueError("SECRET_KEY must be set when ENV=production")
        if self.scorer not in {"foundry", "local", "fake"}:
            raise ValueError(f"SCORER must be foundry, local or fake (got {self.scorer!r})")
        if self.default_language not in {"fr", "en"}:
            raise ValueError("DEFAULT_LANGUAGE must be fr or en")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings read from ``os.environ`` (cached)."""
    return Settings.from_env()
