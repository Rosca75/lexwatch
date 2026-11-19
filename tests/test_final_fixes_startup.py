"""Startup noise: no Authlib httpx deprecation warning, other warnings still shown."""

from __future__ import annotations

import subprocess
import sys


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
