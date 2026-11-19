"""Scorer selection by ``SCORER=foundry|local|fake``."""

from __future__ import annotations

from lexwatch.config import Settings
from lexwatch.scoring.base import Scorer


def get_scorer(settings: Settings) -> Scorer:
    """Return a fresh scorer (fresh circuit breaker and usage counter) for one run."""
    if settings.scorer == "fake":
        from lexwatch.scoring.fake import FakeScorer

        return FakeScorer()
    if settings.scorer == "foundry":
        from lexwatch.scoring.foundry import FoundryScorer

        return FoundryScorer(settings)
    if settings.scorer == "local":
        from lexwatch.scoring.local import LocalScorer

        return LocalScorer(settings)
    raise ValueError(f"unknown SCORER {settings.scorer!r}")


def scorer_configured(settings: Settings) -> bool:
    """Cheap readiness check for /ready: never calls the paid endpoint."""
    if settings.scorer == "fake":
        return True
    if settings.scorer == "foundry":
        return bool(settings.foundry_endpoint and settings.foundry_model)
    if settings.scorer == "local":
        from pathlib import Path

        return Path(settings.local_model_dir).is_dir()
    return False
