"""Optional live test against a real Foundry rerank deployment (costs a fraction of a cent).

Skipped unless ``FOUNDRY_ENDPOINT`` is set together with an auth method: ``FOUNDRY_API_KEY``,
or ``FOUNDRY_LIVE_ENTRA=1`` to use Entra ID (azure-identity ``DefaultAzureCredential``).
Run explicitly, e.g.::

    FOUNDRY_ENDPOINT=https://<resource>.services.ai.azure.com FOUNDRY_API_KEY=... \\
        uv run pytest tests/scoring/test_foundry_live.py -v
"""

from __future__ import annotations

import os

import pytest

from lexwatch.config import Settings
from lexwatch.scoring.foundry import FoundryScorer

ENDPOINT = os.environ.get("FOUNDRY_ENDPOINT", "")
HAS_AUTH = bool(os.environ.get("FOUNDRY_API_KEY")) or os.environ.get("FOUNDRY_LIVE_ENTRA") == "1"

pytestmark = pytest.mark.skipif(
    not (ENDPOINT and HAS_AUTH),
    reason="live Foundry test: set FOUNDRY_ENDPOINT and FOUNDRY_API_KEY (or FOUNDRY_LIVE_ENTRA=1)",
)


def test_live_rerank_orders_relevant_bill_first():
    settings = Settings.from_env()
    with FoundryScorer(settings) as scorer:
        scores = scorer.score_many(
            "Lutte contre le blanchiment de capitaux et le financement du terrorisme",
            [
                "Projet de loi portant création d'un Observatoire de la culture",
                "Projet de loi portant modification de la loi du 12 novembre 2004 relative à la "
                "lutte contre le blanchiment et contre le financement du terrorisme",
            ],
        )
    assert len(scores) == 2
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert scores[1] > scores[0]
    # Record what the deployment reports for billing (meta pass-through is unverified).
    print(f"scores={scores} billed={scorer.billed_units} unit={scorer.billing_unit}")
