"""Every i18n key used in templates exists in both catalogs; pages render in FR and EN."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lexwatch.i18n import catalog
from tests.helpers import csrf_token

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "lexwatch" / "web" / "templates"
KEY_RE = re.compile(r"""\bt\(\s*['"]([a-z0-9_.]+)['"]""")
# Keys built dynamically in templates: prefix -> values that must be translated.
DYNAMIC = {
    "bill.type": ("projet", "proposition"),
    "bill.status": (
        "deposited",
        "in_committee",
        "council_opinion",
        "second_vote_pending",
        "adopted",
        "rejected",
        "published",
        "withdrawn",
        "merged",
        "transformed",
        "other",
    ),
    "bill.field": (
        "type",
        "title",
        "status",
        "filing_date",
        "closing_date",
        "author",
        "dossier_url",
        "summary",
    ),
    "admin.runs.kind": ("daily", "manual", "rescore", "preview"),
    "admin.runs.status": ("queued", "running", "success", "partial", "failed"),
    "admin.sources.status": ("ok", "failed", "skipped"),
    "lang": ("fr", "en"),
}
PY_KEYS = (
    "rules.error.name",
    "rules.error.name_long",
    "rules.error.threshold",
    "rules.error.recipients",
    "rules.error.rule_long",
    "rules.error.chars",
    "rules.error.too_many",
    "rules.error.no_rules",
    "rules.preview.no_rules",
    "rules.preview.unavailable",
    "rules.preview.failed",
    "bill.scores.rule",
    "bill.link.full_pdf",
    "admin.unit.input_tokens",
    "admin.unit.search_units",
)


def _template_keys() -> set[str]:
    keys: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        if path.relative_to(TEMPLATES).parts[0] in ("email", "auth"):
            continue
        keys.update(KEY_RE.findall(path.read_text(encoding="utf-8")))
    return keys


def test_template_keys_exist_in_both_catalogs():
    found = _template_keys()
    prefixes = {k.rstrip(".") for k in found if k.endswith(".")}
    assert prefixes <= set(DYNAMIC), f"dynamic keys without a value list: {prefixes - set(DYNAMIC)}"
    keys = {k for k in found if not k.endswith(".")} | set(PY_KEYS)
    keys |= {f"{prefix}.{v}" for prefix, values in DYNAMIC.items() for v in values}
    assert len(keys) > 50
    for lang in ("fr", "en"):
        missing = sorted(k for k in keys if k not in catalog(lang))
        assert not missing, f"missing in {lang}: {missing}"


def test_catalogs_have_same_keys():
    assert set(catalog("fr")) == set(catalog("en"))


@pytest.mark.parametrize("lang", ["fr", "en"])
@pytest.mark.parametrize("path", ["/", "/rules", "/rules/new", "/admin"])
def test_pages_render_in_both_languages(client, lang, path):
    client.post("/lang", data={"lang": lang, "csrf_token": csrf_token(client)})
    response = client.get(path)
    assert response.status_code == 200
    assert f'<html lang="{lang}">' in response.text
    # No raw key leaked into the page (a missing key renders as itself).
    leaked = re.findall(r">\s*((?:nav|bills|bill|rules|admin|error)\.[a-z_.]+)\s*<", response.text)
    assert not leaked


def test_language_choice_is_stored_on_user(client, db):
    from tests.web.factories import dev_user

    client.post("/lang", data={"lang": "en", "csrf_token": csrf_token(client)})
    assert dev_user(db, "admin").language == "en"


def test_lang_next_keeps_query_string(client):
    response = client.post(
        "/lang",
        data={"lang": "en", "next": "/?q=route&all=1", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/?q=route&all=1"
    assert 'name="next" value="/?q=route&amp;all=1"' in client.get("/?q=route&all=1").text


def test_forbidden_page_is_minimal(user_client):
    response = user_client.get("/admin", headers={"Accept": "text/html"})
    assert response.status_code == 403
    assert "Accès refusé." in response.text
