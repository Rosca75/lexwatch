"""Cohere Rerank on Microsoft Foundry over REST (httpx, no Cohere SDK).

Endpoint (``FOUNDRY_ENDPOINT``): the full rerank URL
``https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank``. When the value is only
the resource root (no path), ``/providers/cohere/v2/rerank`` is appended. Any other path is
used as given (e.g. a gateway in front of Foundry).

Request: ``{"model": FOUNDRY_MODEL, "query": rule, "documents": [...]}`` (no ``top_n``, so
every document is scored), plus ``max_tokens_per_doc`` when ``FOUNDRY_MAX_TOKENS_PER_DOC`` > 0.
Documents are sent in batches of ``FOUNDRY_MAX_DOCUMENTS``. Results come sorted by score and are
mapped back to input order with ``results[].index``; scores are clamped to [0, 1]; a missing
index is a ``ScoringError``.

Auth: ``FOUNDRY_API_KEY`` when set, in header ``FOUNDRY_API_KEY_HEADER`` (``Authorization`` ->
``Bearer <key>``; ``api-key`` -> raw key). Otherwise Entra ID through
``azure.identity.DefaultAzureCredential`` with scope ``FOUNDRY_TOKEN_SCOPE``; the token is
cached until shortly before it expires.

Resilience: per-request timeout (``FOUNDRY_TIMEOUT_SECONDS``), up to ``FOUNDRY_MAX_RETRIES``
retries with exponential backoff and jitter on 429/500/502/503/504, timeouts and connection
errors, honouring ``Retry-After`` / ``retry-after-ms``. A per-instance circuit breaker opens after
``FOUNDRY_CIRCUIT_THRESHOLD`` consecutive failed requests; from then on every call raises
``ScorerUnavailable`` at once. Create one instance per run (``get_scorer``).

Billing: ``meta.billed_units.search_units`` (Rerank 4) or ``input_tokens`` (Rerank 5) is added to
``billed_units``; ``billing_unit`` names the unit seen. Document text and keys are never logged.
"""

from __future__ import annotations

import email.utils
import logging
import random
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from lexwatch.config import Settings
from lexwatch.scoring.base import ScorerUnavailable, ScoringError

log = logging.getLogger(__name__)

RERANK_PATH = "/providers/cohere/v2/rerank"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
RETRY_AFTER_MAX_SECONDS = 120.0
TOKEN_REFRESH_MARGIN_SECONDS = 300.0


def resolve_endpoint(value: str) -> str:
    """Full rerank URL from ``FOUNDRY_ENDPOINT`` (resource root -> append the rerank path)."""
    value = (value or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.path in ("", "/"):
        parts = parts._replace(path=RERANK_PATH)
    return urlunsplit(parts)


def foundry_default_threshold(model: str) -> float:
    """Default profile threshold for a Foundry rerank model (used when DEFAULT_THRESHOLD is unset).

    Rerank 5 is calibrated: Cohere suggests 0.50 for Pro and 0.45 for Fast. Rerank 4 is not
    calibrated; 0.5 is a placeholder until it is calibrated on real bills.
    """
    name = model.lower()
    if "v5" in name:
        return 0.45 if "fast" in name else 0.5
    return 0.5


def _billing_preference(model: str) -> tuple[str, str]:
    """Order in which to read ``meta.billed_units``: v4/v3 bill search units, v5 bills tokens."""
    name = model.lower()
    if "v4" in name or "v3" in name:
        return ("search_units", "input_tokens")
    return ("input_tokens", "search_units")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    ms = response.headers.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000.0)
        except ValueError:
            pass
    value = response.headers.get("retry-after")
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def error_detail(response: httpx.Response) -> str:
    """Short, log-safe description of an error response.

    Handles the Cohere envelope ``{"id", "message"}``, the Foundry gateway envelope
    ``{"statusCode", "message", "activityId"}`` and the Azure ``{"error": {"code", "message"}}``.
    """
    message = ""
    ref = ""
    try:
        data: Any = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("code") or "")
        elif isinstance(err, str):
            message = err
        message = str(data.get("message") or message or "")
        ref = str(data.get("activityId") or data.get("id") or "")
    if not message:
        message = (response.text or "").strip().replace("\n", " ")[:200]
    ref = ref or response.headers.get("apim-request-id") or response.headers.get("x-request-id", "")
    detail = f"HTTP {response.status_code}"
    if message:
        detail += f": {message[:300]}"
    if ref:
        detail += f" (ref {ref})"
    return detail


class _RetryableError(Exception):
    def __init__(self, detail: str, retry_after: float | None = None) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


class FoundryScorer:
    """``Scorer`` for Cohere Rerank deployed on Microsoft Foundry."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        *,
        credential: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.endpoint = resolve_endpoint(settings.foundry_endpoint)
        self.model_name = settings.foundry_model
        self.max_documents = max(1, settings.foundry_max_documents)
        self.max_retries = max(0, settings.foundry_max_retries)
        self.circuit_threshold = max(1, settings.foundry_circuit_threshold)
        self.billed_units = 0
        self.billing_unit: str | None = None
        self.requests = 0  # successful upstream requests
        self.default_threshold = (
            settings.default_threshold
            if settings.default_threshold_explicit
            else foundry_default_threshold(settings.foundry_model)
        )
        self._client = client
        self._own_client = client is None
        self._credential = credential
        self._token: str | None = None
        self._token_expires_on = 0.0
        self._sleep = sleep
        self._rand = rand
        self._clock = clock
        self._consecutive_failures = 0
        self._circuit_open = False
        self._last_error: str | None = None

    # -- lifecycle -----------------------------------------------------------------------

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open

    def close(self) -> None:
        if self._client is not None and self._own_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> FoundryScorer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.settings.foundry_timeout_seconds),
                headers={"User-Agent": self.settings.http_user_agent},
            )
        return self._client

    # -- auth ----------------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        key = self.settings.foundry_api_key
        if key:
            header = self.settings.foundry_api_key_header or "Authorization"
            value = f"Bearer {key}" if header.lower() == "authorization" else key
            return {header: value}
        return {"Authorization": f"Bearer {self._entra_token()}"}

    def _entra_token(self) -> str:
        now = self._clock()
        if self._token and now < self._token_expires_on - TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token
        if self._credential is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:  # pragma: no cover - azure-identity is a core dep
                raise ScoringError("Entra ID auth requires azure-identity") from exc
            self._credential = DefaultAzureCredential()
        try:
            access = self._credential.get_token(self.settings.foundry_token_scope)
        except Exception as exc:
            raise ScoringError(f"Entra ID token request failed: {type(exc).__name__}") from exc
        self._token = access.token
        self._token_expires_on = float(access.expires_on)
        return self._token

    # -- scoring -------------------------------------------------------------------------

    def score_many(self, rule_text: str, bill_texts: list[str]) -> list[float]:
        if not bill_texts:
            return []
        self._check_circuit()
        if not self.endpoint:
            raise ScoringError("FOUNDRY_ENDPOINT is not set")
        if not self.model_name:
            raise ScoringError("FOUNDRY_MODEL is not set")
        scores: list[float] = []
        for start in range(0, len(bill_texts), self.max_documents):
            scores.extend(self._rerank(rule_text, bill_texts[start : start + self.max_documents]))
        return scores

    def _check_circuit(self) -> None:
        if self._circuit_open:
            raise ScorerUnavailable(
                f"Foundry circuit open after {self._consecutive_failures} consecutive failed "
                f"requests; last error: {self._last_error}"
            )

    def _rerank(self, query: str, documents: list[str]) -> list[float]:
        self._check_circuit()
        body: dict[str, Any] = {"model": self.model_name, "query": query, "documents": documents}
        if self.settings.foundry_max_tokens_per_doc > 0:
            body["max_tokens_per_doc"] = self.settings.foundry_max_tokens_per_doc
        try:
            data = self._post_with_retries(body, len(documents))
            scores = self._parse(data, len(documents))
        except ScoringError as exc:
            self._record_failure(str(exc))
            if self._circuit_open:
                raise ScorerUnavailable(
                    f"Foundry circuit opened after {self._consecutive_failures} consecutive "
                    f"failed requests; last error: {exc}"
                ) from exc
            raise
        self._consecutive_failures = 0
        self.requests += 1
        self._add_billing(data)
        return scores

    def _record_failure(self, detail: str) -> None:
        self._consecutive_failures += 1
        self._last_error = detail
        if self._consecutive_failures >= self.circuit_threshold and not self._circuit_open:
            self._circuit_open = True
            log.error(
                "scoring.foundry.circuit_open",
                extra={"failures": self._consecutive_failures, "error": detail},
            )

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, RETRY_AFTER_MAX_SECONDS)
        base = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2**attempt))
        return base * (0.5 + 0.5 * self._rand())  # jitter in [base/2, base]

    def _post_with_retries(self, body: dict[str, Any], n_docs: int) -> dict[str, Any]:
        attempts = self.max_retries + 1
        last: str = ""
        for attempt in range(attempts):
            try:
                return self._post_once(body)
            except _RetryableError as exc:
                last = str(exc)
                if attempt + 1 >= attempts:
                    break
                delay = self._backoff(attempt, exc.retry_after)
                log.warning(
                    "scoring.foundry.retry",
                    extra={
                        "attempt": attempt + 1,
                        "delay_seconds": round(delay, 2),
                        "error": last,
                        "documents": n_docs,
                    },
                )
                self._sleep(delay)
        raise ScoringError(f"Foundry rerank failed after {attempts} attempts: {last}")

    def _post_once(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", **self._auth_headers()}
        try:
            response = self._http().post(self.endpoint, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise _RetryableError(f"timeout ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise _RetryableError(f"connection error ({type(exc).__name__})") from exc
        if response.status_code in RETRYABLE_STATUS:
            raise _RetryableError(error_detail(response), _retry_after_seconds(response))
        if response.status_code == 401:
            self._token = None  # force a fresh Entra token on the next request
        if response.status_code >= 400:
            raise ScoringError(f"Foundry rerank rejected the request: {error_detail(response)}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ScoringError("Foundry rerank returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ScoringError("Foundry rerank returned an unexpected body")
        return data

    @staticmethod
    def _parse(data: dict[str, Any], n_docs: int) -> list[float]:
        results = data.get("results")
        if not isinstance(results, list):
            raise ScoringError("Foundry rerank response has no 'results' list")
        scores: list[float | None] = [None] * n_docs
        for item in results:
            if not isinstance(item, dict):
                raise ScoringError("Foundry rerank result is not an object")
            index, score = item.get("index"), item.get("relevance_score")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < n_docs:
                raise ScoringError(f"Foundry rerank returned an invalid index: {index!r}")
            if isinstance(score, bool) or not isinstance(score, int | float):
                raise ScoringError(f"Foundry rerank returned an invalid score for {index}")
            scores[index] = min(1.0, max(0.0, float(score)))
        missing = [i for i, s in enumerate(scores) if s is None]
        if missing:
            raise ScoringError(
                f"Foundry rerank returned no score for {len(missing)} of {n_docs} documents "
                f"(first missing index {missing[0]})"
            )
        return [float(s) for s in scores if s is not None]

    def _add_billing(self, data: dict[str, Any]) -> None:
        meta = data.get("meta")
        billed = meta.get("billed_units") if isinstance(meta, dict) else None
        if not isinstance(billed, dict):
            return
        for unit in _billing_preference(self.model_name):
            value = billed.get(unit)
            if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
                self.billed_units += round(value)
                self.billing_unit = unit
                return
