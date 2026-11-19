"""Scorer selection by ``SCORER=foundry|local|fake``, plus per-scorer defaults.

Default threshold: when ``DEFAULT_THRESHOLD`` is set in the environment it applies to every
scorer. Otherwise each scorer has its own default, because the score scales differ:

- foundry, Rerank 5 (calibrated): 0.50 for ``*-v5-pro``, 0.45 for ``*-v5-fast`` (Cohere's
  recommended cut-offs); Rerank 4 (not calibrated): 0.5 placeholder.
- local (bge-reranker-v2-m3, sigmoid of a logit): ``LOCAL_DEFAULT_THRESHOLD`` = 0.03,
  calibrated on real bills (see ``lexwatch.scoring.local``).
- fake (keyword overlap): 0.3.

A profile's own threshold always wins (``Profile.effective_threshold``).
"""

from __future__ import annotations

from lexwatch.config import Settings
from lexwatch.scoring.base import Scorer


def get_scorer(settings: Settings) -> Scorer:
    """Return a fresh scorer (fresh circuit breaker and usage counter) for one run."""
    if settings.scorer == "fake":
        from lexwatch.scoring.fake import FakeScorer

        return FakeScorer(default_threshold=default_threshold_for(settings))
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
        # Auth is an API key or Entra ID resolved at call time (managed identity, env vars).
        return bool(settings.foundry_endpoint and settings.foundry_model)
    if settings.scorer == "local":
        from pathlib import Path

        from lexwatch.scoring.local import TOKENIZER_FILE

        model_dir = Path(settings.local_model_dir)
        return (model_dir / settings.local_model_file).is_file() and (
            model_dir / TOKENIZER_FILE
        ).is_file()
    return False


def model_name_for(settings: Settings) -> str:
    """The ``model_name`` that ``get_scorer(settings)`` would store, without building it."""
    if settings.scorer == "fake":
        return "fake"
    if settings.scorer == "foundry":
        return settings.foundry_model
    if settings.scorer == "local":
        from lexwatch.scoring.local import model_label

        return model_label(settings)
    raise ValueError(f"unknown SCORER {settings.scorer!r}")


def default_threshold_for(settings: Settings) -> float:
    """Threshold for profiles without their own (explicit DEFAULT_THRESHOLD, else per scorer)."""
    if settings.default_threshold_explicit:
        return settings.default_threshold
    if settings.scorer == "foundry":
        from lexwatch.scoring.foundry import foundry_default_threshold

        return foundry_default_threshold(settings.foundry_model)
    if settings.scorer == "local":
        from lexwatch.scoring.local import LOCAL_DEFAULT_THRESHOLD

        return LOCAL_DEFAULT_THRESHOLD
    if settings.scorer == "fake":
        from lexwatch.scoring.fake import FAKE_DEFAULT_THRESHOLD

        return FAKE_DEFAULT_THRESHOLD
    return settings.default_threshold
