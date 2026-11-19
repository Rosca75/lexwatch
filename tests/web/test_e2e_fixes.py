"""Web fixes from the end-to-end run: newest-first order, title fallback and clamp, bill detail
lists and documents, admin progress and source statuses, preview indicator and limit."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from lexwatch.models import Run
from lexwatch.web.routes import rules as rules_routes
from lexwatch.web.routes.bills import bill_extra, committee_text
from tests.web.factories import make_bill


def _numbers_in_table(html: str) -> list[str]:
    return re.findall(r'<a href="/bills/([^"]+)"', html)


def test_bills_list_newest_first_by_number(client, db):
    make_bill(db, "7999", filing_date=date(2026, 1, 1))  # filed late, older number
    make_bill(db, "8600", filing_date=None)  # vote-only record: no filing date
    make_bill(db, "8000A", filing_date=date(2020, 1, 1))
    make_bill(db, "8000B", filing_date=date(2020, 1, 1))
    make_bill(db, "8000", filing_date=date(2020, 1, 1))
    make_bill(db, "10000", filing_date=None)
    html = client.get("/?all=1").text
    numbers = [
        n
        for n in _numbers_in_table(html)
        if n in {"7999", "8600", "8000A", "8000B", "8000", "10000"}
    ]
    assert numbers == ["10000", "8600", "8000B", "8000A", "8000", "7999"]


def test_csv_export_newest_first(client, db):
    make_bill(db, "7999", filing_date=date(2026, 1, 1))
    make_bill(db, "8600", filing_date=None)
    response = client.get("/admin/export/bills.csv")
    rows = list(csv.reader(io.StringIO(response.text.lstrip("﻿"))))
    numbers = [r[0] for r in rows[1:] if r[0] in {"7999", "8600"}]
    assert numbers == ["8600", "7999"]


def test_empty_title_fallback_in_list_and_detail(client, db):
    make_bill(db, "8627", title="", type="projet")
    make_bill(db, "8628", title="  ", type="proposition")
    html = client.get("/?all=1").text
    assert "Projet de loi n° 8627" in html
    assert "Proposition de loi n° 8628" in html
    detail = client.get("/bills/8627").text
    assert "<h1>Projet de loi n° 8627</h1>" in detail
    assert 'class="lead"' not in detail
    client.post("/lang", data={"lang": "en", "next": "/"}, headers={"X-CSRF-Token": _token(client)})
    assert "Government bill 8627" in client.get("/?all=1").text


def _token(client) -> str:
    from tests.helpers import csrf_token

    return csrf_token(client)


def test_long_title_clamped_with_full_title_on_hover(client, db):
    title = "Projet de loi " + "portant modification de la loi modifiée " * 10
    make_bill(db, "8650", title=title)
    html = client.get("/?all=1").text
    assert re.search(
        r'<a href="/bills/8650"><span class="clamp" title="Projet de loi portant', html
    )
    css = client.get("/static/style.css").text
    assert "-webkit-line-clamp: 2" in css and "line-clamp: 2" in css


def test_bill_detail_committee_rapporteur_joined_and_documents_sorted(client, db):
    extra = {
        "committee": ["FI", "FIN"],
        "committee_name": "C. Finances & Budget|C. Finances",
        "rapporteur": ["A.Bauler", "F.Closener"],
        "documents": [
            {"label": "Rapport", "date": "2019-06-06", "url": "https://wdocs-pub.chd.lu/r.pdf"},
            {"label": "Sans date", "date": None, "url": "https://wdocs-pub.chd.lu/x.pdf"},
            {"label": "Dépôt", "date": "2018-12-04", "url": "https://wdocs-pub.chd.lu/d.pdf"},
            {"label": "Réunion", "date": "2019-05-06", "url": "https://wdocs-pub.chd.lu/m.pdf"},
        ],
    }
    make_bill(db, "8700", extra=extra)
    html = client.get("/bills/8700").text
    assert "C. Finances &amp; Budget, C. Finances" in html
    assert "A.Bauler, F.Closener" in html
    assert "[&#39;" not in html and "['" not in html  # never Python list syntax
    labels = re.findall(r'rel="noopener">(Dépôt|Réunion|Rapport|Sans date)</a>', html)
    assert labels == ["Dépôt", "Réunion", "Rapport", "Sans date"]


def test_committee_text_fallbacks():
    assert committee_text({"committee": ["J", "JUST"]}) == "J, JUST"
    assert committee_text({"committee": "J"}) == "J"
    assert committee_text({"committee_name": "C. Justice|C. Justice"}) == "C. Justice"
    assert committee_text({}) is None

    class B:
        extra = {"rapporteur": "G.Baum"}

    assert bill_extra(B())["rapporteur"] == "G.Baum"


def test_admin_shows_progress_of_running_run_and_unchanged_status(client, db):
    now = datetime.now(UTC)
    db.add(
        Run(
            kind="daily",
            status="running",
            started_at=now - timedelta(minutes=5),
            details={
                "progress": {"done": 1200, "total": 12039},
                "sources": {
                    "datapublic-112": {"status": "unchanged", "records": 0},
                    "chd": {"status": "skipped", "records": 0},
                },
            },
        )
    )
    db.flush()
    html = client.get("/admin").text
    assert "1 200 / 12 039" in html
    assert "Inchangée" in html and ">unchanged<" not in html
    client.post(
        "/lang", data={"lang": "en", "next": "/admin"}, headers={"X-CSRF-Token": _token(client)}
    )
    html = client.get("/admin").text
    assert "1,200 / 12,039" in html
    assert "Unchanged" in html


def test_admin_hides_progress_of_finished_run(client, db):
    now = datetime.now(UTC)
    db.add(
        Run(
            kind="daily",
            status="success",
            started_at=now - timedelta(minutes=5),
            finished_at=now,
            details={"progress": {"done": 10, "total": 10}},
        )
    )
    db.flush()
    assert "10 / 10" not in client.get("/admin").text


def test_preview_button_has_loading_indicator(client):
    html = client.get("/rules/new").text
    assert 'hx-indicator="#preview, #preview-busy"' in html
    assert re.search(r'<span id="preview-busy" class="htmx-indicator"[^>]*>Calcul…</span>', html)
    css = client.get("/static/style.css").text
    assert ".htmx-indicator { display: none; }" in css


def test_preview_limit_depends_on_scorer(settings):
    assert rules_routes.preview_limit(settings) == 50
    assert rules_routes.preview_limit(replace(settings, scorer="local")) == 30
    assert rules_routes.preview_limit(replace(settings, scorer="foundry")) == 50
