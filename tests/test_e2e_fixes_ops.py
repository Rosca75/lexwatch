"""Operational fixes from the end-to-end run: source error messages, CLI startup refusals,
alembic log noise, local scorer batching and logger settings."""

from __future__ import annotations

import json
import logging
import math

import httpx
import pytest

from lexwatch import cli
from lexwatch.config import Settings
from lexwatch.sources.base import SourceError
from lexwatch.sources.datapublic import Dataset112Adapter
from tests.helpers import TEST_DATABASE_URL, make_test_settings

API = "https://data.public.lu/api/1/"


def _adapter_with(handler) -> tuple[Dataset112Adapter, httpx.Client]:
    return Dataset112Adapter(API, request_delay_seconds=0), httpx.Client(
        transport=httpx.MockTransport(handler)
    )


# -- source errors -------------------------------------------------------------------------------


def test_http_502_is_reported_as_unreachable_not_missing():
    adapter, client = _adapter_with(lambda r: httpx.Response(502))
    with pytest.raises(SourceError) as info:
        adapter.fetch(client)
    message = str(info.value)
    assert "data.public.lu unreachable: 502 Bad Gateway" in message
    assert "not found" not in message
    assert info.value.status_code == 502


def test_connect_error_is_reported_as_unreachable():
    def handler(request):
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    adapter, client = _adapter_with(handler)
    with pytest.raises(SourceError) as info:
        adapter.fetch(client)
    message = str(info.value)
    assert "data.public.lu unreachable: ConnectError: [Errno 111] Connection refused" in message
    assert info.value.network is True


def test_real_404_is_dataset_not_found():
    adapter, client = _adapter_with(lambda r: httpx.Response(404))
    with pytest.raises(SourceError, match="dataset not found") as info:
        adapter.fetch(client)
    assert "unreachable" not in str(info.value)


# -- CLI startup refusals ------------------------------------------------------------------------


def _main_with(monkeypatch, settings: Settings, argv: list[str]) -> int:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return cli.main(argv)


def test_invalid_settings_exit_2_with_one_stderr_line(monkeypatch, capsys, caplog):
    bad = make_test_settings(env="production", dev_auth=True)
    with caplog.at_level(logging.ERROR, logger="lexwatch.cli"):
        code = _main_with(monkeypatch, bad, ["run-job"])
    assert code == 2
    out, err = capsys.readouterr()
    assert err.strip().splitlines() == [
        "lexwatch: configuration error: DEV_AUTH must not be enabled when ENV=production"
    ]
    assert "Traceback" not in err and "Traceback" not in out
    (record,) = [r for r in caplog.records if r.getMessage() == "config.invalid"]
    assert "DEV_AUTH" in record.error and record.exc_info is None
    # The JSON formatter renders it as one line.
    from lexwatch.logging import JsonFormatter

    line = JsonFormatter().format(record)
    assert "\n" not in line and json.loads(line)["error"].startswith("DEV_AUTH")


def test_unparsable_env_value_exits_2(monkeypatch, capsys):
    def broken():
        raise ValueError("invalid literal for int() with base 10: 'abc'")

    monkeypatch.setattr(cli, "get_settings", broken)
    assert cli.main(["run-job"]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_migrate_logs_one_line_and_silences_alembic_info(monkeypatch, caplog):
    """`lexwatch migrate` on an up-to-date DB: one "migrations: at head <rev>" INFO line."""
    settings = make_test_settings(database_url=TEST_DATABASE_URL)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    try:
        with caplog.at_level(logging.INFO):
            assert _main_with(monkeypatch, settings, ["migrate"]) == 0
        info = [r for r in caplog.records if r.levelno == logging.INFO]
        assert [r.name for r in info] == ["lexwatch.cli"]
        assert info[0].getMessage().startswith("migrations: at head ")
        assert info[0].revision
    finally:
        logging.getLogger("alembic").setLevel(logging.NOTSET)


# -- local scorer ----------------------------------------------------------------------------------


class _Enc:
    def __init__(self, n: int, width: int) -> None:
        self.ids = [0] * n + [1] * (width - n)
        self.attention_mask = [1] * n + [0] * (width - n)


class _Tok:
    def __init__(self) -> None:
        self.widths: list[int] = []

    def enable_truncation(self, **kw):
        pass

    def enable_padding(self, **kw):
        pass

    def encode_batch(self, pairs):
        lengths = [len(d.split()) for _, d in pairs]
        width = max(lengths)
        self.widths.append(width)
        return [_Enc(n, width) for n in lengths]


class _Sess:
    def run(self, outputs, feeds):
        lengths = feeds["attention_mask"].sum(axis=1).astype("float32")
        return [(lengths - 5.0).reshape(-1, 1)]


def test_local_scorer_batches_by_length_and_keeps_input_order(tmp_path):
    pytest.importorskip("numpy")
    from lexwatch.scoring.local import LocalScorer

    settings = make_test_settings(scorer="local", local_model_dir=str(tmp_path), local_batch_size=2)
    tok = _Tok()
    scorer = LocalScorer(settings, session=_Sess(), tokenizer=tok)
    docs = ["a " * 9, "a", "a " * 10, "a a"]
    scores = scorer.score_many("q", docs)
    assert scores == pytest.approx([1 / (1 + math.exp(-(len(d.split()) - 5))) for d in docs])
    assert tok.widths == [10, 2]  # long texts together, short texts together


def test_local_scorer_sets_threads_and_quiet_logger(tmp_path, monkeypatch):
    ort = pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    from lexwatch.scoring import local

    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model_int8.onnx").write_bytes(b"x")
    (tmp_path / "tokenizer.json").write_text("{}")
    seen: dict = {}

    class FakeSession:
        def __init__(self, path, sess_options, providers):
            seen["threads"] = sess_options.intra_op_num_threads
            seen["severity"] = sess_options.log_severity_level

    class FakeTokenizer:
        @staticmethod
        def from_file(path):
            return _Tok()

    import tokenizers

    monkeypatch.setattr(ort, "InferenceSession", FakeSession)
    monkeypatch.setattr(ort, "set_default_logger_severity", lambda s: seen.setdefault("default", s))
    monkeypatch.setattr(tokenizers, "Tokenizer", FakeTokenizer)
    settings = make_test_settings(scorer="local", local_model_dir=str(tmp_path), local_threads=3)
    local.LocalScorer(settings)._load()
    assert seen == {"threads": 3, "severity": 3, "default": 3}

    seen.clear()
    auto = make_test_settings(scorer="local", local_model_dir=str(tmp_path))
    local.LocalScorer(auto)._load()
    assert seen["threads"] == local.available_cpus() >= 1


def test_local_default_threshold_is_003():
    from lexwatch.scoring.factory import default_threshold_for
    from lexwatch.scoring.local import LOCAL_DEFAULT_THRESHOLD

    assert LOCAL_DEFAULT_THRESHOLD == 0.03
    assert default_threshold_for(make_test_settings(scorer="local")) == 0.03
    assert default_threshold_for(make_test_settings(scorer="foundry")) in (0.45, 0.5)
