"""Cohere Rerank on Microsoft Foundry over REST (httpx), stream 2.

- POST ``settings.foundry_endpoint`` with ``{"model", "query", "documents", "top_n"}``;
  read ``results[].index`` / ``results[].relevance_score``; usage from
  ``meta.billed_units.search_units`` (verify against docs/research/).
- Auth: ``FOUNDRY_API_KEY`` if set, else Entra ID via ``azure.identity.DefaultAzureCredential``
  with scope ``settings.foundry_token_scope``.
- Timeouts, retries with exponential backoff on 429/5xx, and a per-instance circuit breaker
  (raise ``ScorerUnavailable`` once open). Create one instance per run.
"""

from __future__ import annotations

import httpx

from lexwatch.config import Settings


class FoundryScorer:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.model_name = settings.foundry_model
        self.max_documents = settings.foundry_max_documents
        self.billed_units = 0
        self._client = client

    def score_many(self, rule_text: str, bill_texts: list[str]) -> list[float]:
        raise NotImplementedError("stream 2: Foundry rerank scorer")
