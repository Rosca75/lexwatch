"""Scoring orchestration (stream 2).

- ``score_pending_bills``: for every profile rule, score bills with ``score_state='pending'``
  or 'unscored' (one ``score_many`` call per rule, batched to ``max_documents``), upsert
  ``rule_scores`` (with ``scorer.model_name``), set ``bills.scored_text`` and
  ``score_state='scored'``; on ``ScoringError``/``ScorerUnavailable`` mark the remaining bills
  ``unscored`` (kept and retried next run). Then recompute matches for affected profiles.
  Rule scores whose ``model`` differs from ``scorer.model_name`` are stale and get rescored.
- ``rescore_profile``: rescore every bill for one profile (rules/threshold changed).
- ``preview_matches``: score unsaved rule texts against recent bills for the Rules editor
  preview (htmx); writes nothing. May reuse stored rule scores for unchanged rule texts.
- ``compute_matches``: ``profile_score = mean(rule scores)``; ``matched = profile_score >=
  profile.effective_threshold(settings.default_threshold)``; upsert ``matches``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from lexwatch.models import Profile, Run
from lexwatch.scoring.base import Scorer


@dataclass(slots=True)
class ScoreResult:
    scored: int = 0
    unscored: int = 0
    billed_units: int = 0
    error: str | None = None


@dataclass(slots=True)
class PreviewRow:
    bill_number: str
    title: str
    profile_score: float
    matched: bool
    rule_scores: list[float] = field(default_factory=list)  # same order as the rule texts


def profile_score(rule_scores: list[float]) -> float | None:
    """Plain average of rule scores; ``None`` when there are no rules."""
    if not rule_scores:
        return None
    return sum(rule_scores) / len(rule_scores)


def is_match(score: float | None, threshold: float) -> bool:
    return score is not None and score >= threshold


def score_pending_bills(session: Session, scorer: Scorer, run: Run | None) -> ScoreResult:
    raise NotImplementedError("stream 2: score pending bills")


def rescore_profile(session: Session, scorer: Scorer, profile_id: int) -> ScoreResult:
    raise NotImplementedError("stream 2: rescore profile")


def compute_matches(
    session: Session, profile: Profile, default_threshold: float | None = None
) -> int:
    """Recompute ``matches`` rows for ``profile``; return the number of matched bills.

    ``default_threshold`` falls back to ``get_settings().default_threshold``.
    """
    raise NotImplementedError("stream 2: compute matches")


def preview_matches(
    session: Session,
    scorer: Scorer,
    rule_texts: list[str],
    threshold: float,
    limit: int = 50,
) -> list[PreviewRow]:
    """Score ``rule_texts`` against the ``limit`` most recent bills; nothing is persisted."""
    raise NotImplementedError("stream 2: preview")
