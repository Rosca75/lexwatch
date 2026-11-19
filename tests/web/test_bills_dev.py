"""Bills home and bill detail."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest

from tests.helpers import csrf_token
from tests.web.factories import (
    dev_user,
    make_bill,
    make_event,
    make_match,
    make_profile,
    other_user,
)


def _rows(html: str) -> list[str]:
    return re.findall(r'<a href="/bills/([^"]+)">', html)


@pytest.fixture
def data(db):
    admin = dev_user(db, "admin")
    aml = make_profile(db, admin, "AML", ("blanchiment de capitaux",))
    b1 = make_bill(
        db, "8100", title="Lutte contre le blanchiment de capitaux", filing_date=date(2024, 5, 1)
    )
    b2 = make_bill(db, "8200", title="Code de la route", filing_date=date(2025, 3, 1))
    b3 = make_bill(
        db,
        "8300",
        title="Blanchiment et financement du terrorisme",
        type="proposition",
        status="in_committee",
        filing_date=date(2025, 6, 1),
    )
    make_match(db, aml, b1, 0.81)
    make_match(db, aml, b2, 0.12)
    make_match(db, aml, b3, 0.66)
    return {"admin": admin, "aml": aml, "bills": (b1, b2, b3)}


def test_home_lists_matched_bills_newest_first(client, data):
    response = client.get(f"/?profile={data['aml'].id}")
    assert response.status_code == 200
    assert _rows(response.text) == ["8300", "8100"]
    assert "0,66" in response.text  # French decimal separator
    assert "Proposition de loi" in response.text  # translated type
    assert "En commission" in response.text  # translated status


def test_home_in_english(client, data):
    client.post("/lang", data={"lang": "en", "csrf_token": csrf_token(client)})
    response = client.get("/")
    assert '<html lang="en">' in response.text
    assert "Private member’s bill" in response.text
    assert "0.66" in response.text


def test_unknown_status_falls_back_to_raw_value(client, db, data):
    make_bill(db, "8400", status="weird")
    response = client.get("/?all=1")
    assert "weird" in response.text


def test_show_all_toggle(client, data):
    response = client.get(f"/?profile={data['aml'].id}&all=1")
    assert _rows(response.text) == ["8300", "8200", "8100"]
    assert "0,12" in response.text


def test_search_full_text_and_number(client, data):
    response = client.get("/?all=1&q=blanchiment")
    assert set(_rows(response.text)) == {"8100", "8300"}
    response = client.get("/?all=1&q=8200")
    assert _rows(response.text) == ["8200"]
    response = client.get("/?all=1&q=introuvable")
    assert _rows(response.text) == []


def test_htmx_request_returns_fragment(client, data):
    response = client.get("/?all=1&q=route", headers={"HX-Request": "true", "HX-Target": "results"})
    assert response.status_code == 200
    assert "<html" not in response.text
    assert _rows(response.text) == ["8200"]


def test_profile_is_remembered_in_session(client, db, data):
    other = make_profile(db, data["admin"], "Zoning", ("aménagement du territoire",))
    make_match(db, other, data["bills"][1], 0.9)
    assert _rows(client.get(f"/?profile={other.id}").text) == ["8200"]
    assert _rows(client.get("/").text) == ["8200"]


def test_user_cannot_select_foreign_profile(user_client, db, data):
    mine = make_profile(db, dev_user(db, "user"), "Mine", ("route",))
    make_match(db, mine, data["bills"][1], 0.7)
    response = user_client.get(f"/?profile={data['aml'].id}")
    assert "AML" not in response.text
    assert _rows(response.text) == ["8200"]


def test_new_or_changed_rows_are_bold(client, db, data):
    admin = data["admin"]
    # Login shifts last_visit_at into previous_visit_at; set both so either order works.
    admin.previous_visit_at = admin.last_visit_at = datetime(2025, 2, 1, tzinfo=UTC)
    data["bills"][2].last_changed_at = datetime(2025, 6, 2, tzinfo=UTC)
    db.flush()
    html = client.get(f"/?profile={data['aml'].id}").text
    assert re.search(r'<tr class="new">\s*<td class="nowrap">8300</td>', html)
    assert not re.search(r'<tr class="new">\s*<td class="nowrap">8100</td>', html)
    assert 'title="En gras' in html  # explained on hover, no badge


def test_pagination(client, db):
    for i in range(55):
        make_bill(db, str(9000 + i), filing_date=date(2025, 1, 1))
    first = client.get("/?all=1")
    assert len(_rows(first.text)) == 50
    assert "page=2" in first.text
    second = client.get("/?all=1&page=2")
    assert len(_rows(second.text)) == 5
    assert _rows(second.text)[-1] == "9000"


def test_home_without_profiles_shows_all(user_client, db):
    make_bill(db, "8500")
    response = user_client.get("/")
    assert _rows(response.text) == ["8500"]


def test_bill_detail(client, db, data):
    b1 = data["bills"][0]
    make_event(db, b1, "status", "deposited", "in_committee")
    response = client.get("/bills/8100")
    assert response.status_code == 200
    html = response.text
    assert "Projet de loi n° 8100" in html
    assert "https://www.chd.lu/fr/dossier/8100" in html
    assert "Déposé" in html and "En commission" in html  # history translated
    assert "0,81" in html
    assert "blanchiment de capitaux : 0,81" in html  # per-rule score on hover
    assert 'aria-describedby="rules-' in html
    assert html.count("Chambre des Députés du Grand-Duché de Luxembourg") >= 2


def test_bill_detail_hides_foreign_profiles(user_client, db, data):
    response = user_client.get("/bills/8100")
    assert response.status_code == 200
    assert "AML" not in response.text


def test_bill_detail_404(client):
    response = client.get("/bills/nope", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "Page introuvable" in response.text


def test_foreign_profile_owner_data_not_leaked_in_select(user_client, db):
    stranger = other_user(db)
    make_profile(db, stranger, "Secret profile")
    assert "Secret profile" not in user_client.get("/").text


def test_bill_detail_shows_extra_when_present(client, db):
    bill = make_bill(db, "8700")
    # Plain attribute until ingestion's bills.extra column is merged; same shape either way.
    bill.extra = {
        "committee": "Commission des Finances",
        "rapporteur": "Jane Doe",
        "dossier_pdf_url": "https://wdocs-pub.chd.lu/docs/compilation/docpa/pdf/8700_Dossier_Complet.pdf",
        "documents": [
            {"label": "Dépôt", "date": "2025-01-15", "url": "https://wdocs-pub.chd.lu/x.pdf"},
            {"label": "Bad", "date": "", "url": "javascript:alert(1)"},
        ],
    }
    html = client.get("/bills/8700").text
    assert "Commission des Finances" in html and "Jane Doe" in html
    assert "8700_Dossier_Complet.pdf" in html and "Dossier complet (PDF)" in html
    assert 'href="https://wdocs-pub.chd.lu/x.pdf"' in html and "2025-01-15" in html
    assert "javascript:" not in html


def test_digest_links_work(user_client, db):
    """The digest links to /?profile={id} and /bills/{number} (BASE_URL-relative)."""
    mine = make_profile(db, dev_user(db, "user"), "Mine", ("route",))
    make_match(db, mine, make_bill(db, "8801", title="Code de la route"), 0.9)
    page = user_client.get(f"/?profile={mine.id}")
    assert page.status_code == 200 and _rows(page.text) == ["8801"]
    assert user_client.get("/bills/8801").status_code == 200
