"""Admin page: guard, runs, Run now, Rescore all, CSV exports."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from lexwatch.models import Profile, Run
from tests.helpers import csrf_token
from tests.web.factories import dev_user, make_bill, make_match, make_profile, other_user


def _run(db, **fields):
    now = datetime.now(UTC)
    values = {
        "kind": "daily",
        "status": "success",
        "started_at": now - timedelta(minutes=3),
        "finished_at": now - timedelta(minutes=1),
        "details": {},
    }
    values.update(fields)
    run = Run(**values)
    db.add(run)
    db.flush()
    return run


def test_admin_guard(user_client, client):
    assert user_client.get("/admin").status_code == 403
    assert user_client.get("/admin/export/bills.csv").status_code == 403
    assert user_client.get("/admin/export/matches.csv").status_code == 403
    token = csrf_token(user_client)
    assert user_client.post("/admin/run", data={"csrf_token": token}).status_code == 403
    assert user_client.post("/admin/rescore", data={"csrf_token": token}).status_code == 403


def test_admin_page_shows_runs_sources_and_units(client, db):
    _run(
        db,
        bills_added=3,
        bills_changed=2,
        bills_scored=5,
        billed_units=1200,
        details={
            "sources": {
                "chd": {"status": "failed", "records": 0, "error": "parse error"},
                "datapublic-112": {"status": "ok", "records": 812},
            },
            "billing": {"unit": "input_tokens"},
        },
    )
    _run(db, status="partial", billed_units=300, error_message="scoring: circuit open")
    make_bill(db, "8800", score_state="unscored")
    html = client.get("/admin").text
    assert "2 min 00 s" in html
    assert "parse error" in html and "812" in html
    assert "scoring: circuit open" in html and "<details>" in html
    assert "1500 jetons" in html
    assert "Partiel" in html
    client.post("/lang", data={"lang": "en", "csrf_token": csrf_token(client)})
    html = client.get("/admin").text
    assert "Last runs" in html and "Partial" in html
    assert "1500 tokens" in html


def test_run_now_inserts_queued_manual_run(client, db):
    response = client.post(
        "/admin/run", data={"csrf_token": csrf_token(client)}, follow_redirects=False
    )
    assert response.status_code == 303
    runs = list(db.scalars(select(Run)))
    assert len(runs) == 1
    assert (runs[0].kind, runs[0].status) == ("manual", "queued")
    assert runs[0].requested_by == dev_user(db, "admin").id
    # A second click while a run is queued does not queue another one.
    client.post("/admin/run", data={"csrf_token": csrf_token(client)})
    assert len(list(db.scalars(select(Run)))) == 1


def test_run_now_requires_csrf(client, db):
    assert client.post("/admin/run", data={}).status_code == 403
    assert client.post("/admin/rescore", data={}).status_code == 403
    assert db.scalar(select(Run)) is None


def test_rescore_all(client, db):
    p1 = make_profile(db, dev_user(db, "admin"), "A")
    p2 = make_profile(db, other_user(db), "B")
    p2.rescore_status = "running"
    db.flush()
    client.post("/admin/rescore", data={"csrf_token": csrf_token(client)})
    db.expire_all()
    assert db.get(Profile, p1.id).rescore_status == "pending"
    assert db.get(Profile, p2.id).rescore_status == "running"


def _csv(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_export_bills_csv(client, db):
    make_bill(db, "8900", title="Blanchiment, « capitaux »", status="adopted")
    rows = _csv(client.get("/admin/export/bills.csv"))
    assert rows[0][:4] == ["number", "type", "status", "title"]
    assert rows[1][:4] == ["8900", "projet", "adopted", "Blanchiment, « capitaux »"]


def test_export_matches_csv(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML")
    make_match(db, profile, make_bill(db, "8901", title="T"), 0.75)
    rows = _csv(client.get("/admin/export/matches.csv"))
    assert rows[0] == [
        "profile_id",
        "profile_name",
        "bill_number",
        "bill_title",
        "profile_score",
        "matched",
        "computed_at",
    ]
    assert rows[1][:6] == [str(profile.id), "AML", "8901", "T", "0.7500", "1"]


def test_preview_runs_count_in_month_total_but_are_not_listed(client, db):
    _run(db, billed_units=100, details={"billing": {"unit": "search_units"}})
    _run(db, kind="preview", billed_units=7, details={"billing": {"unit": "search_units"}})
    html = client.get("/admin").text
    assert "107 unités de recherche" in html
    assert "Aperçu" not in html  # the preview run is not in the runs table
    assert html.count("<td>Quotidien</td>") == 1


def test_state_lines_shown_only_when_nonzero(client, db):
    make_bill(db, "8801", score_state="scored")
    html = client.get("/admin").text
    assert "Facturé ce mois-ci" in html
    for label in ("Non évalués", "Scores d’un ancien modèle", "Profils en réévaluation"):
        assert label not in html
    assert "Projets de loi</dt>" not in html and "En attente d’évaluation" not in html
    make_bill(db, "8802", score_state="unscored")
    profile = make_profile(db, dev_user(db, "admin"), "A")
    profile.rescore_status = "pending"
    db.flush()
    html = client.get("/admin").text
    assert "Non évalués" in html and "Profils en réévaluation" in html


def test_rescore_all_is_secondary_and_warns_about_cost(client):
    html = client.get("/admin").text
    form = html.split('action="/admin/rescore"', 1)[1].split("</form>", 1)[0]
    assert 'class="link"' in form and 'class="primary"' not in form
    assert "peut entraîner un coût" in form
