"""QA tests for LocalScorer with a mocked ONNX session and tokenizer. No model files, no network."""

from __future__ import annotations

import builtins
import math
import sys
from dataclasses import dataclass

import pytest

from lexwatch.scoring.base import ScoringError
from lexwatch.scoring.factory import get_scorer, scorer_configured
from lexwatch.scoring.local import LocalScorer
from tests.helpers import make_test_settings


def local_settings(tmp_path, **overrides):
    return make_test_settings(scorer="local", local_model_dir=str(tmp_path), **overrides)


@dataclass
class Enc:
    ids: list[int]
    attention_mask: list[int]


class Tokenizer:
    """Encodes each pair as one token whose id is the position of the document in ``docs``."""

    def __init__(self, docs: list[str]) -> None:
        self.docs = docs
        self.batches: list[list[tuple[str, str]]] = []
        self.truncation: dict = {}
        self.padding: dict = {}

    def enable_truncation(self, **kw):
        self.truncation = kw

    def enable_padding(self, **kw):
        self.padding = kw

    def encode_batch(self, pairs):
        pairs = list(pairs)
        self.batches.append(pairs)
        return [Enc(ids=[self.docs.index(d)], attention_mask=[1]) for _, d in pairs]


class Session:
    """Returns the logit configured for each document id, shape [batch, 1]."""

    def __init__(self, logits: list[float]) -> None:
        self.logits = logits
        self.batch_sizes: list[int] = []

    def run(self, outputs, feeds):
        import numpy as np

        assert outputs == ["logits"]
        assert set(feeds) == {"input_ids", "attention_mask"}
        ids = feeds["input_ids"][:, 0]
        self.batch_sizes.append(len(ids))
        return [np.asarray([[self.logits[i]] for i in ids], dtype=np.float32)]


def make(tmp_path, logits, docs, **overrides):
    pytest.importorskip("numpy")
    tok, sess = Tokenizer(docs), Session(logits)
    scorer = LocalScorer(local_settings(tmp_path, **overrides), session=sess, tokenizer=tok)
    return scorer, tok, sess


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def test_sigmoid_normalization_matches_reference_values(tmp_path):
    docs = ["relevant", "irrelevant", "zero"]
    scorer, _, _ = make(tmp_path, [5.26, -8.19, 0.0], docs)
    scores = scorer.score_many("q", docs)
    # BAAI's published examples: 5.26 -> 0.9948, -8.19 -> 0.00028
    assert scores[0] == pytest.approx(0.9948, abs=1e-4)
    assert scores[1] == pytest.approx(0.00028, abs=1e-5)
    assert scores[2] == pytest.approx(0.5)


def test_extreme_logits_stay_in_unit_interval_without_overflow(tmp_path):
    docs = ["a", "b", "c", "d"]
    scorer, _, _ = make(tmp_path, [1e4, -1e4, 80.0, -80.0], docs)
    scores = scorer.score_many("q", docs)
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert scores[0] == pytest.approx(1.0) and scores[1] == pytest.approx(0.0)
    assert all(math.isfinite(s) for s in scores)


def test_order_is_input_order_across_batches(tmp_path):
    docs = [f"doc{i}" for i in range(7)]
    logits = [3.0, -1.0, 0.5, -4.0, 2.0, 1.0, -2.0]  # deliberately not sorted
    scorer, tok, sess = make(tmp_path, logits, docs, local_batch_size=3)
    scores = scorer.score_many("rule", docs)
    assert scores == pytest.approx([sigmoid(x) for x in logits], rel=1e-6)
    assert sess.batch_sizes == [3, 3, 1]
    assert [d for b in tok.batches for _, d in b] == docs
    assert all(q == "rule" for b in tok.batches for q, _ in b)  # (query, document) pairs


@pytest.mark.parametrize(("n", "size", "batches"), [(4, 4, [4]), (5, 4, [4, 1]), (1, 8, [1])])
def test_batching_at_and_over_batch_size(tmp_path, n, size, batches):
    docs = [f"d{i}" for i in range(n)]
    scorer, _, sess = make(tmp_path, [0.0] * n, docs, local_batch_size=size)
    assert len(scorer.score_many("q", docs)) == n
    assert sess.batch_sizes == batches
    assert scorer.max_documents == size


def test_empty_input_does_not_load_model(tmp_path):
    scorer = LocalScorer(local_settings(tmp_path))  # no model files at all
    assert scorer.score_many("q", []) == []


def test_tokenizer_is_configured_with_max_length_and_xlmr_padding(tmp_path):
    _, tok, _ = make(tmp_path, [0.0], ["a"], local_max_length=384)
    assert tok.truncation.get("max_length") == 384
    assert tok.padding.get("pad_id") == 1


def test_nothing_billed_locally(tmp_path):
    docs = ["a", "b"]
    scorer, _, _ = make(tmp_path, [0.0, 1.0], docs)
    scorer.score_many("q", docs)
    assert scorer.billed_units == 0 and scorer.billing_unit is None


def _block_imports(monkeypatch, names):
    real = builtins.__import__

    def fake(name, *args, **kwargs):
        if name.split(".")[0] in names:
            raise ImportError(f"No module named {name!r}")
        return real(name, *args, **kwargs)

    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake)


@pytest.mark.parametrize(
    "missing", [{"onnxruntime"}, {"tokenizers"}, {"onnxruntime", "tokenizers"}]
)
def test_missing_extra_is_clear_error_and_construction_is_lazy(tmp_path, monkeypatch, missing):
    _block_imports(monkeypatch, missing)
    s = local_settings(tmp_path)
    scorer = get_scorer(s)  # building the scorer must not import the extra
    assert isinstance(scorer, LocalScorer)
    with pytest.raises(ScoringError) as info:
        scorer.score_many("q", ["doc"])
    message = str(info.value)
    assert "lexwatch[local]" in message


def test_scorer_configured_never_imports_the_extra(tmp_path, monkeypatch):
    _block_imports(monkeypatch, {"onnxruntime", "tokenizers"})
    assert scorer_configured(local_settings(tmp_path)) is False
    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model_int8.onnx").write_bytes(b"x")
    (tmp_path / "tokenizer.json").write_text("{}")
    assert scorer_configured(local_settings(tmp_path)) is True


def test_missing_model_files_name_download_command(tmp_path):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    with pytest.raises(ScoringError, match="download-model"):
        LocalScorer(local_settings(tmp_path)).score_many("q", ["doc"])
