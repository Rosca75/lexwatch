"""Deterministic keyword-overlap scorer for tests and dev (``SCORER=fake``). No network."""

from __future__ import annotations

import re
import unicodedata

_WORD = re.compile(r"\w{3,}")


def _tokens(text: str) -> set[str]:
    norm = unicodedata.normalize("NFKD", text.lower())
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return set(_WORD.findall(norm))


class FakeScorer:
    """Score = |rule tokens ∩ bill tokens| / |rule tokens| (accent- and case-insensitive)."""

    model_name = "fake"

    def __init__(self, max_documents: int = 1000) -> None:
        self.max_documents = max_documents
        self.billed_units = 0
        self.calls: list[tuple[str, int]] = []  # (rule_text, n_documents), for assertions

    def score_many(self, rule_text: str, bill_texts: list[str]) -> list[float]:
        self.calls.append((rule_text, len(bill_texts)))
        rule = _tokens(rule_text)
        if not bill_texts:
            return []
        self.billed_units += 1 + (len(bill_texts) - 1) // self.max_documents
        if not rule:
            return [0.0 for _ in bill_texts]
        return [round(len(rule & _tokens(text)) / len(rule), 4) for text in bill_texts]
