"""Optional local scorer: BAAI/bge-reranker-v2-m3 as ONNX on CPU (stream 2).

Requires the ``local`` extra (onnxruntime, tokenizers, numpy), imported lazily so the default
image never needs them. Model files live in ``settings.local_model_dir`` and are fetched by
``lexwatch download-model``. Raw logits are sigmoid-normalized to [0, 1].
"""

from __future__ import annotations

from lexwatch.config import Settings
from lexwatch.scoring.base import ScoringError


class LocalScorer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_name = f"local:{settings.local_model_repo}"
        self.max_documents = 32
        self.billed_units = 0
        self._session = None

    def _load(self) -> None:
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise ScoringError(
                "SCORER=local requires the 'local' extra: pip install 'lexwatch[local]'"
            ) from exc
        raise NotImplementedError("stream 2: load ONNX model")

    def score_many(self, rule_text: str, bill_texts: list[str]) -> list[float]:
        raise NotImplementedError("stream 2: local ONNX scorer")


def download_model(settings: Settings) -> None:
    """Download the ONNX model + tokenizer into ``settings.local_model_dir`` (stream 2)."""
    raise NotImplementedError("stream 2: download-model")
