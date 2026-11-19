"""Scorer contract (scoring stream).

A scorer returns, for one rule (the query) and N bill texts (the documents), N relevance
scores in [0, 1], in input order. Implementations batch internally to ``max_documents``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class ScoringError(Exception):
    """A scoring call failed after retries (bad response, auth error, exhausted retries)."""


class ScorerUnavailable(ScoringError):
    """The circuit breaker is open for this run: stop calling, mark remaining bills unscored."""


@runtime_checkable
class Scorer(Protocol):
    model_name: str  # stored with every rule score (rule_scores.model)
    max_documents: int  # max documents per upstream request
    billed_units: int  # accumulated usage (search_units or input_tokens) across calls
    billing_unit: str | None  # "search_units" | "input_tokens" | None (nothing billed/reported)
    default_threshold: float  # profile threshold when the profile has none (see factory)

    def score_many(self, rule_text: str, bill_texts: list[str]) -> list[float]:
        """Return one score in [0, 1] per bill text, in order.

        Raise ``ScoringError`` on failure, ``ScorerUnavailable`` when the circuit is open.
        """
        ...
