"""Startup noise: no Authlib httpx deprecation warning, other warnings still shown."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import make_test_settings


def _stderr(code: str) -> str:
    return subprocess.run(
        [sys.executable, "-W", "default", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stderr


def test_app_import_does_not_print_authlib_httpx_deprecation():
    stderr = _stderr("import lexwatch.web.app")
    assert "httpx module is deprecated" not in stderr
    assert "AuthlibDeprecationWarning" not in stderr


def test_other_authlib_deprecations_are_still_shown():
    stderr = _stderr(
        "import lexwatch.web.app\n"
        "from authlib.deprecate import deprecate\n"
        "deprecate('something else')"
    )
    assert "something else" in stderr


# -- onnxruntime telemetry notice --------------------------------------------------------------

LOAD_LOCAL_MODEL = """
from lexwatch.config import Settings
from lexwatch.scoring.local import LocalScorer
print(LocalScorer(Settings.from_env()).score_many("impôts", ["loi fiscale"]))
"""


def test_local_scorer_disables_onnxruntime_telemetry_before_import(monkeypatch, tmp_path):
    """``ORT_DISABLE_TELEMETRY`` must be set before onnxruntime is imported (only then is
    "Failed to persist telemetry device ID" not printed)."""
    import types

    from lexwatch.scoring.base import ScoringError
    from lexwatch.scoring.local import ORT_TELEMETRY_ENV, LocalScorer

    seen: dict[str, str | None] = {}

    class _Finder:
        def find_spec(self, name, path=None, target=None):
            if name == "onnxruntime":
                seen["env"] = os.environ.get(ORT_TELEMETRY_ENV)
            return None

    monkeypatch.delenv(ORT_TELEMETRY_ENV, raising=False)
    monkeypatch.delitem(sys.modules, "onnxruntime", raising=False)
    monkeypatch.setitem(sys.modules, "tokenizers", types.ModuleType("tokenizers"))
    monkeypatch.setattr(sys, "meta_path", [_Finder(), *sys.meta_path])
    scorer = LocalScorer(make_test_settings(local_model_dir=str(tmp_path)))
    with pytest.raises(ScoringError):
        scorer._load()  # no model files in tmp_path (or no onnxruntime installed)
    assert seen["env"] == "1"


@pytest.mark.skipif(
    importlib.util.find_spec("onnxruntime") is None
    or not (Path(os.environ.get("LOCAL_MODEL_DIR", "/nonexistent")) / "tokenizer.json").is_file(),
    reason="needs the local extra and LOCAL_MODEL_DIR with the downloaded model",
)
def test_real_local_model_load_keeps_stderr_clean(tmp_path):
    # HOME is a plain file: no place to persist a telemetry device id, even as root.
    home = tmp_path / "home"
    home.write_text("")
    skip = ("ORT_DISABLE_TELEMETRY", "XDG_CACHE_HOME")
    env = {k: v for k, v in os.environ.items() if k not in skip}
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, "-c", LOAD_LOCAL_MODEL],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=True,
    )
    assert result.stdout.startswith("[")
    assert "telemetry" not in result.stderr
    assert "\x1b[" not in result.stderr
