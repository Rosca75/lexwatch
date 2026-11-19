"""QA (stream 4): every i18n key used by the auth and email templates and by the auth/notify
Python code exists in both FR and EN (spec section 10: "i18n coverage (every key exists in FR
and EN)"). Keys are found by scanning the sources, not from a hand-written list."""

from __future__ import annotations

import ast
import re
import string
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lexwatch.i18n import LANGUAGES, catalog
from tests.auth.support import FakeProvider, build_app, oidc_settings

SRC = Path(__file__).resolve().parents[2] / "src" / "lexwatch"
TEMPLATES = SRC / "web" / "templates"
TEMPLATE_FILES = sorted([*(TEMPLATES / "auth").glob("*.html"), *(TEMPLATES / "email").glob("*.*")])
PY_FILES = sorted(
    [
        SRC / "web" / "auth.py",
        SRC / "web" / "deps.py",
        SRC / "web" / "csrf.py",
        *(SRC / "notify").rglob("*.py"),
    ]
)
_T_CALL = re.compile(r"""\bt\(\s*(['"])([^'"]+)\1""")
_KEY_LIKE = re.compile(r"^(auth|email|digest|error|app|nav|lang|bill|bills)\.[a-z0-9_.]+$")
_DYNAMIC_T = re.compile(r"""\bt\(\s*[a-z_]""")


def template_keys(path: Path) -> set[str]:
    return {m.group(2) for m in _T_CALL.finditer(path.read_text(encoding="utf-8"))}


_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}


def python_keys(path: Path) -> set[str]:
    """Key-like string literals in the module, except structured-log event names.

    Catches direct ``t("...")``/``tr("...")`` calls as well as keys passed around in variables
    (``auth_error(request, key)`` with ``key = "auth.error.denied" if ... else ...``).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    log_events: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            log_events.add(id(node.args[0]))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in log_events
            and _KEY_LIKE.match(node.value)
            and not node.value.endswith(".")  # f-string prefix (bill.status.{x}), has fallback
        ):
            keys.add(node.value)
    return keys


def all_used_keys() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in TEMPLATE_FILES:
        for key in template_keys(path):
            used.setdefault(key, set()).add(path.name)
    for path in PY_FILES:
        for key in python_keys(path):
            used.setdefault(key, set()).add(path.name)
    return used


def test_scan_finds_keys_in_every_template_and_module():
    assert TEMPLATE_FILES and all(template_keys(p) for p in TEMPLATE_FILES)
    used = all_used_keys()
    # sanity: known keys from each area are discovered
    assert "auth.error.failed" in used and "email.digest.subject" in used
    assert "auth.logged_out.title" in used and "email.source" in used


@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_used_key_exists_in_catalog(lang):
    cat = catalog(lang)
    missing = {k: sorted(files) for k, files in all_used_keys().items() if k not in cat}
    assert not missing, f"missing in {lang}: {missing}"


def test_dynamic_template_keys_are_only_the_known_ones():
    """``t(variable)`` in a template must only receive keys produced by auth.py."""
    for path in TEMPLATE_FILES:
        for match in _DYNAMIC_T.finditer(path.read_text(encoding="utf-8")):
            line = path.read_text(encoding="utf-8")[match.start() : match.start() + 40]
            assert line.startswith("t(message_key"), (path.name, line)


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_placeholders_match_between_languages():
    fr, en = catalog("fr"), catalog("en")
    for key in all_used_keys():
        if key in fr and key in en:
            assert _fields(fr[key]) == _fields(en[key]), key


def test_auth_and_mail_part_files_have_the_same_keys_in_fr_and_en():
    fr, en = catalog("fr"), catalog("en")
    prefixes = ("auth.", "email.", "digest.")
    assert {k for k in fr if k.startswith(prefixes)} == {k for k in en if k.startswith(prefixes)}


def test_values_are_translated_not_empty():
    fr, en = catalog("fr"), catalog("en")
    for key in all_used_keys():
        assert fr.get(key, "").strip(), key
        assert en.get(key, "").strip(), key


# --- rendered pages show no raw keys -------------------------------------------------------

_RAW_KEY = re.compile(r"\b(?:auth|email|error|nav|lang|app)\.[a-z_]+(?:\.[a-z_]+)*\b")


def _no_raw_keys(html: str) -> None:
    text = re.sub(r"<[^>]+>", " ", html)
    assert not _RAW_KEY.findall(text), _RAW_KEY.findall(text)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_auth_pages_render_without_raw_keys(db, lang):
    provider = FakeProvider()
    app = build_app(oidc_settings(default_language=lang), db, provider)
    with TestClient(app) as client:
        _no_raw_keys(client.get("/logged-out").text)
        client.get("/login", follow_redirects=False)
        _no_raw_keys(client.get("/auth/callback?code=x&state=forged").text)
        _no_raw_keys(client.get("/auth/callback?error=access_denied&state=x").text)
    app = build_app(oidc_settings(default_language=lang, oidc_issuer=""), db)
    with TestClient(app) as client:
        response = client.get("/login")
        assert response.status_code == 503
        _no_raw_keys(response.text)
