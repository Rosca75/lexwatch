"""LocalScorer with a mocked ONNX session and tokenizer, and download_model over MockTransport.

No real model and no network. Tests that need numpy are skipped when the ``local`` extra is not
installed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import httpx
import pytest

from lexwatch.scoring.base import Scorer, ScoringError
from lexwatch.scoring.factory import default_threshold_for, model_name_for, scorer_configured
from lexwatch.scoring.local import (
    LOCAL_DEFAULT_THRESHOLD,
    LocalScorer,
    download_model,
    model_label,
)
from tests.helpers import make_test_settings


def local_settings(tmp_path, **overrides):
    return make_test_settings(scorer="local", local_model_dir=str(tmp_path), **overrides)


@dataclass
class Enc:
    ids: list[int]
    attention_mask: list[int]


class FakeTokenizer:
    def __init__(self) -> None:
        self.truncation: dict = {}
        self.padding: dict = {}
        self.batches: list[list[tuple[str, str]]] = []

    def enable_truncation(self, **kw):
        self.truncation = kw

    def enable_padding(self, **kw):
        self.padding = kw

    def encode_batch(self, pairs):
        self.batches.append(list(pairs))
        width = max(len(q.split()) + len(d.split()) + 4 for q, d in pairs)
        out = []
        for q, d in pairs:
            n = len(q.split()) + len(d.split()) + 4
            out.append(
                Enc(ids=[0] * n + [1] * (width - n), attention_mask=[1] * n + [0] * (width - n))
            )
        return out


class FakeSession:
    """Logit = number of words in the document (from the attention mask) minus 6."""

    def __init__(self) -> None:
        self.feeds: list[dict] = []

    def run(self, outputs, feeds):
        import numpy as np

        assert outputs == ["logits"]
        assert feeds["input_ids"].dtype == np.int64
        assert feeds["attention_mask"].dtype == np.int64
        self.feeds.append(feeds)
        lengths = feeds["attention_mask"].sum(axis=1).astype(np.float32)
        return [(lengths - 6.0 - 4.0 - 1.0).reshape(-1, 1)]


def test_model_label_and_defaults(tmp_path):
    s = local_settings(tmp_path)
    assert model_label(s) == "bge-reranker-v2-m3-int8"
    assert model_name_for(s) == "bge-reranker-v2-m3-int8"
    assert model_label(local_settings(tmp_path, local_model_file="onnx/model.onnx")) == (
        "bge-reranker-v2-m3-fp32"
    )
    scorer = LocalScorer(s)
    assert isinstance(scorer, Scorer)
    assert scorer.default_threshold == LOCAL_DEFAULT_THRESHOLD == default_threshold_for(s)
    assert scorer.billed_units == 0 and scorer.billing_unit is None
    explicit = local_settings(tmp_path, default_threshold=0.4, default_threshold_explicit=True)
    assert LocalScorer(explicit).default_threshold == 0.4


def test_scores_with_mocked_session(tmp_path):
    pytest.importorskip("numpy")
    tok, sess = FakeTokenizer(), FakeSession()
    s = local_settings(tmp_path, local_batch_size=2, local_max_length=256)
    scorer = LocalScorer(s, session=sess, tokenizer=tok)
    docs = ["un", "un deux trois quatre", "a b c d e f g h i j"]
    scores = scorer.score_many("blanchiment", docs)
    # logits: words(doc) - 6 -> sigmoid
    expected = [1 / (1 + math.exp(-(len(d.split()) - 6))) for d in docs]
    assert scores == pytest.approx(expected, rel=1e-5)
    assert all(0.0 <= x <= 1.0 for x in scores)
    assert [len(b) for b in tok.batches] == [2, 1]  # batched by local_batch_size
    assert tok.batches[0][0] == ("blanchiment", "un")  # (query, document) pairs
    assert tok.truncation["max_length"] == 256
    assert tok.padding["pad_id"] == 1
    assert scorer.score_many("x", []) == []


def test_session_failure_is_scoring_error(tmp_path):
    pytest.importorskip("numpy")

    class Boom(FakeSession):
        def run(self, outputs, feeds):
            raise RuntimeError("onnx exploded")

    scorer = LocalScorer(local_settings(tmp_path), session=Boom(), tokenizer=FakeTokenizer())
    with pytest.raises(ScoringError, match="onnx exploded"):
        scorer.score_many("q", ["doc"])


def test_missing_model_files_give_clear_error(tmp_path):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    scorer = LocalScorer(local_settings(tmp_path))
    with pytest.raises(ScoringError, match="download-model"):
        scorer.score_many("q", ["doc"])
    assert scorer_configured(local_settings(tmp_path)) is False


def test_missing_extra_gives_clear_error(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in {"onnxruntime", "tokenizers"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ScoringError, match=r"lexwatch\[local\]"):
        LocalScorer(local_settings(tmp_path)).score_many("q", ["doc"])


# -- download ---------------------------------------------------------------------------------

MODEL_BYTES = b"onnx-model-bytes" * 100
TOKENIZER_BYTES = b'{"tokenizer": true}'


def hub_handler(model_bytes=MODEL_BYTES, sha=None, calls=None):
    sha = sha or hashlib.sha256(model_bytes).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, request.url.path))
        path = request.url.path
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=model_bytes)
        if path.endswith("onnx/model_int8.onnx"):
            headers = {
                "x-linked-size": str(len(model_bytes)),
                "x-linked-etag": f'"{sha}"',
                "location": "https://cdn.example/blob",
            }
            return httpx.Response(302, headers=headers)
        if path.endswith("tokenizer.json"):
            if request.method == "HEAD":
                return httpx.Response(
                    200, headers={"content-length": str(len(TOKENIZER_BYTES)), "etag": '"abc"'}
                )
            return httpx.Response(200, content=TOKENIZER_BYTES)
        return httpx.Response(404)

    return handler


def test_download_model_verifies_and_renames(tmp_path):
    s = local_settings(tmp_path)
    calls: list = []
    client = httpx.Client(transport=httpx.MockTransport(hub_handler(calls=calls)))
    paths = download_model(s, client=client)
    model = tmp_path / "onnx" / "model_int8.onnx"
    assert paths == [model, tmp_path / "tokenizer.json"]
    assert model.read_bytes() == MODEL_BYTES
    assert (tmp_path / "tokenizer.json").read_bytes() == TOKENIZER_BYTES
    assert oct(model.stat().st_mode & 0o777) == "0o644"
    assert not list(tmp_path.rglob("*.part"))
    rev = s.local_model_revision
    assert (
        "HEAD",
        f"/onnx-community/bge-reranker-v2-m3-ONNX/resolve/{rev}/onnx/model_int8.onnx",
    ) in calls
    assert scorer_configured(s) is True

    # second run: files present with the right size -> no GET
    calls.clear()
    download_model(s, client=client)
    assert [m for m, _ in calls] == ["HEAD", "HEAD"]


def test_download_model_rejects_bad_checksum(tmp_path):
    s = local_settings(tmp_path)
    handler = hub_handler(sha="0" * 64)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ScoringError, match="sha256 mismatch"):
        download_model(s, client=client)
    assert not (tmp_path / "onnx" / "model_int8.onnx").exists()
    assert not list(tmp_path.rglob("*.part"))


def test_download_model_rejects_truncated_file(tmp_path):
    s = local_settings(tmp_path)

    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=MODEL_BYTES[:10])
        return hub_handler()(request)

    with pytest.raises(ScoringError, match="size mismatch"):
        download_model(s, client=httpx.Client(transport=httpx.MockTransport(handler)))
