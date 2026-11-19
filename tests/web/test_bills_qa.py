"""QA (stream 3): bills list (search, show-all toggle, pagination, new-or-changed weight) and
bill detail. Spec: docs/build-prompt.md section 6.1 and 6.2, section 7 (no badge).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import lexwatch.web
from lexwatch.models import User
from tests.web.factories import dev_user, make_bill, make_event, make_match, make_profile

ROW_RE = re.compile(r'<a href="/bills/([^"]+)">')


def numbers(html: str) -> list[str]:
    return ROW_RE.findall(html)


@pytest.fixture
def me(db: Session, user_client: TestClient) -> User:
    return dev_user(db, "user")


# --- search -------------------------------------------------------------------------------


@pytest.fixture
def corpus(db: Session) -> dict[str, str]:
    bills = {
        "aml": make_bill(
            db,
            "8101",
            title="Projet de loi relatif à la lutte contre les blanchiments de capitaux",
            filing_date=date(2025, 3, 1),
        ),
        "reg": make_bill(
            db,
            "8102",
            title="Règlement grand-ducal sur la sécurité des installations",
            filing_date=date(2025, 2, 1),
        ),
        "fish": make_bill(
            db,
            "8410",
            title="Loi sur la pêche",
            summary="Modifie la réglementation des cotisations sociales",
            filing_date=date(2025, 1, 1),
        ),
        "other": make_bill(db, "84100", title="Autre texte", filing_date=date(2024, 1, 1)),
    }
    return {k: b.number for k, b in bills.items()}


def search(client: TestClient, q: str) -> list[str]:
    response = client.get("/", params={"q": q, "all": "1"})
    assert response.status_code == 200, q
    return numbers(response.text)


def test_search_uses_french_stemming(user_client, me, corpus):
    assert search(user_client, "blanchiment") == [corpus["aml"]]
    assert search(user_client, "Blanchiments") == [corpus["aml"]]
    assert search(user_client, "BLANCHIMENT capitaux") == [corpus["aml"]]


def test_search_with_accents(user_client, me, corpus):
    assert search(user_client, "règlement") == [corpus["reg"]]
    assert search(user_client, "sécurité") == [corpus["reg"]]
    assert search(user_client, "pêche") == [corpus["fish"]]


def test_search_without_accents_finds_accented_titles(user_client, me, corpus):
    assert search(user_client, "securite") == [corpus["reg"]]
    assert search(user_client, "peche") == [corpus["fish"]]


def test_search_covers_summary(user_client, me, corpus):
    assert search(user_client, "cotisations") == [corpus["fish"]]


def test_search_bill_number_is_exact(user_client, me, corpus):
    assert search(user_client, "8410") == [corpus["fish"]]
    assert search(user_client, "84100") == [corpus["other"]]
    assert search(user_client, "08410") == [corpus["fish"]]
    assert search(user_client, "841") == []


@pytest.mark.parametrize(
    "q",
    [
        "'; DROP TABLE bills; --",
        "1' OR '1'='1",
        "a & b | !c",
        "blanchiment:*",
        "(((",
        '"unterminated',
        "\\",
        "%",
        "_",
        "-",
        "or",
        "!!!",
        "<->",
        "😀",
        "x" * 5000,
    ],
)
def test_search_with_hostile_input_never_errors(user_client, me, corpus, q):
    response = user_client.get("/", params={"q": q, "all": "1"})
    assert response.status_code == 200
    # Nothing in the corpus matches these; certainly not "everything".
    assert len(numbers(response.text)) < len(corpus)


def test_search_with_nul_byte_never_errors(user_client, me, corpus):
    assert user_client.get("/", params={"q": "abc\x00", "all": "1"}).status_code == 200


@pytest.mark.parametrize("q", ["", "   ", "\t"])
def test_empty_search_lists_everything(user_client, me, corpus, q):
    assert sorted(search(user_client, q)) == sorted(corpus.values())


def test_search_query_is_escaped_when_reflected(user_client, me, corpus):
    q = '"><script>alert(1)</script>'
    page = user_client.get("/", params={"q": q}).text
    assert "<script>alert(1)" not in page
    assert 'value="&#34;&gt;&lt;script' in page or 'value="&quot;&gt;&lt;script' in page


# --- show all toggle and profile ------------------------------------------------------------


@pytest.fixture
def profiled(db: Session, me: User):
    profile = make_profile(db, me, name="AML")
    matched = make_bill(db, "9001", filing_date=date(2025, 5, 1))
    below = make_bill(db, "9002", filing_date=date(2025, 4, 1))
    unscored = make_bill(db, "9003", filing_date=date(2025, 3, 1))
    make_match(db, profile, matched, 0.81)
    make_match(db, profile, below, 0.12)
    return profile, matched, below, unscored


def test_default_shows_only_matched_bills_with_score(user_client, profiled):
    profile, matched, below, unscored = profiled
    page = user_client.get("/").text
    assert numbers(page) == [matched.number]
    assert "0,81" in page


@pytest.mark.parametrize("value", ["1", "on", "true"])
def test_show_all_lists_every_bill(user_client, profiled, value):
    profile, matched, below, unscored = profiled
    page = user_client.get("/", params={"all": value}).text
    assert numbers(page) == [matched.number, below.number, unscored.number]
    assert "0,12" in page


@pytest.mark.parametrize("value", ["0", "", "false", "no"])
def test_show_all_off_values(user_client, profiled, value):
    profile, matched, *_ = profiled
    assert numbers(user_client.get("/", params={"all": value}).text) == [matched.number]


def test_show_all_checkbox_reflects_state(user_client, profiled):
    assert re.search(r'id="all"[^>]*checked', user_client.get("/?all=1").text)
    assert not re.search(r'id="all"[^>]*checked', user_client.get("/").text)


def test_search_respects_profile_filter(user_client, db, profiled):
    profile, matched, below, unscored = profiled
    below.title = "Texte unique zebre"
    db.flush()
    assert numbers(user_client.get("/", params={"q": "zebre"}).text) == []
    assert numbers(user_client.get("/", params={"q": "zebre", "all": "1"}).text) == [below.number]


@pytest.mark.parametrize("value", ["abc", "-1", "0", "1.5", "99999999999999999999999", ""])
def test_bad_profile_parameter_is_ignored(user_client, profiled, value):
    profile, matched, *_ = profiled
    response = user_client.get("/", params={"profile": value})
    assert response.status_code == 200
    assert numbers(response.text) == [matched.number]


def test_newest_first_with_missing_dates_last(user_client, db, me):
    make_bill(db, "500", filing_date=date(2024, 1, 1))
    make_bill(db, "501", filing_date=None)
    make_bill(db, "502", filing_date=date(2025, 1, 1))
    make_bill(db, "99", filing_date=date(2025, 1, 1))
    assert numbers(user_client.get("/").text) == ["502", "99", "500", "501"]


# --- pagination edges ---------------------------------------------------------------------


@pytest.fixture
def many(db: Session, me: User) -> list[str]:
    start = date(2020, 1, 1)
    return [
        make_bill(db, str(10000 + i), filing_date=start + timedelta(days=i)).number
        for i in range(51)
    ]


def test_first_page_has_next_only(user_client, many):
    page = user_client.get("/").text
    assert len(numbers(page)) == 50
    assert "page=2" in page
    assert "page=0" not in page


def test_second_page_has_last_row_and_prev(user_client, many):
    page = user_client.get("/?page=2").text
    assert numbers(page) == [many[0]]
    assert 'href="/"' in page or "page=1" in page
    assert "page=3" not in page


@pytest.mark.parametrize("value", ["0", "-1", "-999999", "abc", "1.5", ""])
def test_invalid_page_falls_back_to_first(user_client, many, value):
    response = user_client.get("/", params={"page": value})
    assert response.status_code == 200
    assert len(numbers(response.text)) == 50


def test_page_past_the_end_is_empty_not_error(user_client, many):
    response = user_client.get("/?page=1000")
    assert response.status_code == 200
    assert numbers(response.text) == []


def test_huge_page_number_is_not_500(user_client, many):
    assert user_client.get("/?page=999999999999999999999").status_code == 200


def test_pager_keeps_query_and_profile(user_client, db, me):
    profile = make_profile(db, me, name="P")
    for i in range(51):
        make_match(db, profile, make_bill(db, str(20000 + i), title="loi fiscale"), 0.9)
    page = user_client.get("/", params={"q": "fiscale"}).text
    next_link = re.search(r'href="(/\?[^"]*page=2[^"]*)"', page).group(1)
    assert "q=fiscale" in next_link
    assert f"profile={profile.id}" in next_link


# --- new or changed since the last visit: text weight only ----------------------------------


@pytest.fixture
def visit(db: Session, me: User):
    since = datetime(2025, 6, 1, tzinfo=UTC)
    me.previous_visit_at = since
    db.flush()
    fresh = make_bill(db, "601", last_changed_at=since + timedelta(minutes=1))
    old = make_bill(db, "602", last_changed_at=since - timedelta(days=1))
    same = make_bill(db, "603", last_changed_at=since)
    return fresh, old, same


def _row(html: str, number: str) -> str:
    match = re.search(r"<tr[^>]*>(?:(?!</tr>).)*?/bills/" + number + r'".*?</tr>', html, re.S)
    assert match, number
    return match.group(0)


def test_changed_rows_are_marked_by_class_only(user_client, visit):
    fresh, old, same = visit
    page = user_client.get("/?all=1").text
    fresh_row, old_row, same_row = (_row(page, b.number) for b in (fresh, old, same))
    assert re.match(r'<tr class="new">', fresh_row)
    assert old_row.startswith("<tr>")
    assert same_row.startswith("<tr>")
    # No badge: apart from the class and the bill data, the rows are identical in structure.
    strip = re.compile(r">[^<]*<")

    def shape(row: str, number: str) -> str:
        return strip.sub("><", row.replace(number, "N"))

    assert shape(fresh_row.replace(' class="new"', ""), fresh.number) == shape(old_row, old.number)
    for word in ("badge", "chip", "pill", "tag", "nouveau", "Nouveau", "New", "NEW"):
        assert word not in fresh_row


def test_new_class_means_bold_text_and_nothing_else():
    css = (Path(lexwatch.web.__file__).parent / "static" / "style.css").read_text("utf-8")
    rules = re.findall(r"([^{}]*\.new[^{}]*)\{([^}]*)\}", css)
    assert rules, "no CSS rule for .new"
    for _selector, body in rules:
        props = {p.split(":")[0].strip() for p in body.split(";") if p.strip()}
        assert props == {"font-weight"}, body
        assert re.search(r"font-weight:\s*(600|700|bold)", body)


def test_first_visit_marks_nothing(user_client, db, me):
    me.previous_visit_at = None
    db.flush()
    make_bill(db, "604", last_changed_at=datetime.now(UTC))
    assert 'class="new"' not in user_client.get("/?all=1").text


# --- bill detail --------------------------------------------------------------------------


def test_detail_shows_metadata_history_and_dossier(user_client, db, me):
    bill = make_bill(
        db,
        "8410",
        title="Loi sur la pêche",
        status="adopted",
        dossier_url="https://www.chd.lu/fr/dossier/8410",
    )
    make_event(db, bill, "status", "deposited", "adopted")
    page = user_client.get("/bills/8410").text
    assert "Loi sur la pêche" in page
    assert 'href="https://www.chd.lu/fr/dossier/8410"' in page
    assert "https://www.chd.lu" in page  # attribution


def test_detail_per_rule_scores_on_hover(user_client, db, me):
    profile = make_profile(db, me, name="AML", rules=("regle un", "regle deux"))
    bill = make_bill(db)
    make_match(db, profile, bill, 0.7)
    page = user_client.get(f"/bills/{bill.number}").text
    hint = re.search(r'<span[^>]*data-tip="([^"]*)"[^>]*aria-describedby="([^"]+)"', page)
    assert hint, "per-rule scores are not on a hover element"
    assert "regle un" in hint.group(1) and "regle deux" in hint.group(1)
    assert f'id="{hint.group(2)}"' in page


@pytest.mark.parametrize("number", ["0", "does-not-exist", "%3Cscript%3E"])
def test_detail_unknown_numbers_404(user_client, me, number):
    response = user_client.get(f"/bills/{number}", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "<script>" not in response.text


def test_detail_number_with_nul_byte_is_404(user_client, me):
    response = user_client.get("/bills/8410%00", headers={"Accept": "text/html"})
    assert response.status_code == 404


def test_bill_title_and_fields_are_escaped(user_client, db, me):
    xss = "<img src=x onerror=alert(1)>"
    bill = make_bill(db, title=xss, author=xss, summary=xss, status=xss, type=xss)
    make_event(db, bill, "title", xss, xss)
    for path in ("/?all=1", f"/bills/{bill.number}"):
        page = user_client.get(path).text
        assert "<img" not in page, path


def test_detail_dossier_url_rejects_javascript_scheme(user_client, db, me):
    bill = make_bill(db, dossier_url="javascript:alert(document.cookie)")
    page = user_client.get(f"/bills/{bill.number}").text
    assert 'href="javascript:' not in page
