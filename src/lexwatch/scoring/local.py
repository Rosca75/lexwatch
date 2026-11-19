"""Optional local scorer: BAAI/bge-reranker-v2-m3 as ONNX on CPU.

Requires the ``local`` extra (onnxruntime, tokenizers, numpy), imported lazily so the default
image never needs them. Model files live in ``settings.local_model_dir``:

- ``<dir>/onnx/model_int8.onnx`` (``LOCAL_MODEL_FILE``, dynamic int8, single file, ~571 MB)
- ``<dir>/tokenizer.json``

They come from ``onnx-community/bge-reranker-v2-m3-ONNX`` at a pinned revision
(``LOCAL_MODEL_REVISION``) and are fetched by ``lexwatch download-model``.

Each (rule, bill) pair is encoded as ``<s> rule </s></s> bill </s>``, truncated to
``LOCAL_MAX_LENGTH`` tokens, and the single ``logits`` output is mapped to [0, 1] with a
sigmoid (FlagEmbedding's ``normalize=True``). These scores are not calibrated like Cohere
Rerank 5, so the local scorer has its own default threshold (``LOCAL_DEFAULT_THRESHOLD``).
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

from lexwatch.config import Settings
from lexwatch.scoring.base import ScoringError

log = logging.getLogger(__name__)

TOKENIZER_FILE = "tokenizer.json"
HF_BASE_URL = "https://huggingface.co"
# Default profile threshold for this scorer when DEFAULT_THRESHOLD is not set. First
# calibrated at 0.05 on the 250 most recent bills of dataset 121; an end-to-end run on all
# 4,013 bills with 3 French profiles then showed non-matching bills up to a profile mean of
# 0.013 to 0.028 (99th percentile), clearly relevant bills between 0.03 and 0.05 that 0.05
# missed, and about 60% relevant bills in the 0.02 to 0.05 band. bge sigmoid scores are very
# peaked, so the cut-off is far below Cohere Rerank 5's calibrated 0.5.
LOCAL_DEFAULT_THRESHOLD = 0.03
_XLMR_PAD_ID = 1


def model_label(settings: Settings) -> str:
    """Stable, human-readable model name stored in ``rule_scores.model``.

    ``onnx/model_int8.onnx`` from ``.../bge-reranker-v2-m3-ONNX`` -> ``bge-reranker-v2-m3-int8``.
    Another file or repo yields another name, so their scores count as stale.
    """
    repo = settings.local_model_repo.rsplit("/", 1)[-1]
    if repo.lower().endswith("-onnx"):
        repo = repo[: -len("-onnx")]
    stem = Path(settings.local_model_file).stem  # model_int8
    variant = stem.removeprefix("model").lstrip("_") or "fp32"
    return f"{repo}-{variant}"


def available_cpus() -> int:
    """CPUs this process may run on (affinity mask, e.g. ``docker --cpuset-cpus``)."""
    try:
        return len(os.sched_getaffinity(0)) or 1
    except (AttributeError, OSError):  # not on Linux
        return os.cpu_count() or 1


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


class LocalScorer:
    """Cross-encoder reranker on CPU. Lazily loads the model on first use."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: Any | None = None,
        tokenizer: Any | None = None,
    ) -> None:
        self.settings = settings
        self.model_name = model_label(settings)
        self.max_documents = max(1, settings.local_batch_size)
        self.max_length = settings.local_max_length
        self.billed_units = 0  # nothing is billed locally
        self.billing_unit: str | None = None
        self.default_threshold = (
            settings.default_threshold
            if settings.default_threshold_explicit
            else LOCAL_DEFAULT_THRESHOLD
        )
        self._session = session  # injectable for tests (onnxruntime.InferenceSession-like)
        self._tokenizer = tokenizer  # injectable for tests (tokenizers.Tokenizer-like)
        if tokenizer is not None:
            self._configure_tokenizer(tokenizer)

    @property
    def model_dir(self) -> Path:
        return Path(self.settings.local_model_dir)

    def _configure_tokenizer(self, tokenizer: Any) -> None:
        tokenizer.enable_truncation(max_length=self.max_length, strategy="longest_first")
        tokenizer.enable_padding(pad_id=_XLMR_PAD_ID, pad_token="<pad>")

    def _load(self) -> None:
        if self._session is not None and self._tokenizer is not None:
            return
        try:
            import onnxruntime
            import tokenizers
        except ImportError as exc:
            raise ScoringError(
                "SCORER=local requires the 'local' extra: pip install 'lexwatch[local]'"
            ) from exc
        model_path = self.model_dir / self.settings.local_model_file
        tok_path = self.model_dir / TOKENIZER_FILE
        missing = [str(p) for p in (model_path, tok_path) if not p.is_file()]
        if missing:
            raise ScoringError(
                f"local model files missing ({', '.join(missing)}); run `lexwatch download-model`"
            )
        # Errors only: onnxruntime otherwise prints warnings (and a telemetry notice on some
        # platforms) straight to stderr, outside the JSON logs.
        onnxruntime.set_default_logger_severity(3)
        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        threads = self.settings.local_threads or available_cpus()
        if threads:
            options.intra_op_num_threads = threads
        self._session = onnxruntime.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        tokenizer = tokenizers.Tokenizer.from_file(str(tok_path))
        self._configure_tokenizer(tokenizer)
        self._tokenizer = tokenizer
        log.info("scoring.local.loaded", extra={"model": self.model_name})

    def score_many(self, rule_text: str, bill_texts: list[str]) -> list[float]:
        if not bill_texts:
            return []
        self._load()
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - numpy ships with the extra
            raise ScoringError("SCORER=local requires numpy (the 'local' extra)") from exc

        # Each batch is padded to its longest pair, so batching texts of similar length (longest
        # first) avoids most of the padding work; scores are returned in input order.
        order = sorted(range(len(bill_texts)), key=lambda i: len(bill_texts[i]), reverse=True)
        scores: list[float] = [0.0] * len(bill_texts)
        size = self.max_documents
        for start in range(0, len(order), size):
            indices = order[start : start + size]
            chunk = [bill_texts[i] for i in indices]
            try:
                encodings = self._tokenizer.encode_batch([(rule_text, t) for t in chunk])
                input_ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
                attention = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)
                outputs = self._session.run(
                    ["logits"], {"input_ids": input_ids, "attention_mask": attention}
                )
                logits = np.asarray(outputs[0], dtype=np.float64).reshape(len(chunk), -1)[:, 0]
            except Exception as exc:
                raise ScoringError(f"local scorer failed: {type(exc).__name__}: {exc}") from exc
            for i, x in zip(indices, logits, strict=True):
                scores[i] = min(1.0, max(0.0, _sigmoid(float(x))))
        return scores


# -- download ------------------------------------------------------------------------------


def _hf_url(settings: Settings, path: str) -> str:
    repo, rev = settings.local_model_repo, settings.local_model_revision
    return f"{HF_BASE_URL}/{repo}/resolve/{rev}/{path}"


def _remote_meta(client: httpx.Client, url: str) -> tuple[int | None, str | None]:
    """(size, sha256) from a HEAD on the Hub ``resolve`` URL (redirect not followed).

    LFS files answer 302 with ``x-linked-size`` and ``x-linked-etag`` (the LFS sha256).
    Small non-LFS files answer 200 with ``content-length`` and a git-sha1 etag (not usable).
    Returns ``(None, None)`` when the Hub cannot be reached (an existing file is then kept).
    """
    try:
        resp = client.head(url, follow_redirects=False)
    except httpx.HTTPError as exc:
        log.warning("scoring.download.head_failed", extra={"url": url, "error": repr(exc)})
        return None, None
    if resp.status_code >= 400:
        raise ScoringError(f"model file not found: {url} (HTTP {resp.status_code})")
    size_header = resp.headers.get("x-linked-size") or (
        resp.headers.get("content-length") if resp.status_code == 200 else None
    )
    size = int(size_header) if size_header and size_header.isdigit() else None
    etag = resp.headers.get("x-linked-etag") or ""
    etag = etag.strip().removeprefix("W/").strip('"').lower()
    is_sha256 = len(etag) == 64 and all(c in "0123456789abcdef" for c in etag)
    return size, (etag if is_sha256 else None)


def _download_file(
    client: httpx.Client, url: str, dest: Path, size: int | None, sha256: str | None
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
    tmp = Path(tmp_name)
    digest = hashlib.sha256()
    written = 0
    try:
        with os.fdopen(fd, "wb") as fh, client.stream("GET", url, follow_redirects=True) as resp:
            if resp.status_code >= 400:
                raise ScoringError(f"download failed: {url} (HTTP {resp.status_code})")
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
                digest.update(chunk)
                written += len(chunk)
        if size is not None and written != size:
            raise ScoringError(f"size mismatch for {dest.name}: got {written}, expected {size}")
        if sha256 is not None and digest.hexdigest() != sha256:
            raise ScoringError(f"sha256 mismatch for {dest.name}")
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; the model volume is shared
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log.info(
        "scoring.download.done",
        extra={"file": str(dest), "bytes": written, "sha256_verified": sha256 is not None},
    )


def download_model(settings: Settings, client: httpx.Client | None = None) -> list[Path]:
    """Download the ONNX model and tokenizer into ``settings.local_model_dir``.

    Each file is streamed to a temporary file in the target directory, verified (size, and
    sha256 when the Hub reports the LFS hash), then atomically renamed into place. A file that
    is already present with the expected size is skipped. Returns the local paths.
    """
    from lexwatch.http import make_client

    own = client is None
    client = client or make_client(settings)  # timeout applies per read, not per file
    target = Path(settings.local_model_dir)
    paths: list[Path] = []
    try:
        for rel in (settings.local_model_file, TOKENIZER_FILE):
            url = _hf_url(settings, rel)
            dest = target / rel
            size, sha256 = _remote_meta(client, url)
            if dest.is_file() and (size is None or dest.stat().st_size == size):
                log.info("scoring.download.skip", extra={"file": str(dest)})
                paths.append(dest)
                continue
            log.info("scoring.download.start", extra={"url": url, "bytes": size})
            _download_file(client, url, dest, size, sha256)
            paths.append(dest)
    finally:
        if own:
            client.close()
    return paths
