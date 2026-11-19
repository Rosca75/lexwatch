"""Profiles and rules: CRUD, reorder, ownership, rescore flag, preview."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from lexwatch.models import Profile, Rule, RuleScore
from lexwatch.scoring import service as scoring_service
from lexwatch.scoring.base import ScoringError
from lexwatch.scoring.service import PreviewRow
from tests.helpers import csrf_token
from tests.web.factories import dev_user, make_bill, make_match, make_profile, other_user


def _post(client, url, **fields):
    data = {"csrf_token": csrf_token(client, "/rules"), "action": "save"}
    data.update(fields)
    return client.post(url, data=data, follow_redirects=False)


def _form(profile, **overrides):
    data = {
        "name": profile.name,
        "threshold": "",
        "recipients": profile.recipients,
        "rule_id": [str(r.id) for r in profile.rules],
        "rule_text": [r.text for r in profile.rules],
    }
    data.update(overrides)
    return data


def test_rules_index_fr_en(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("a", "b"), threshold=0.425)
    html = client.get("/rules").text
    assert "AML" in html and "0,425" in html
    assert f'href="/rules/{profile.id}"' in html
    client.post("/lang", data={"lang": "en", "csrf_token": csrf_token(client)})
    html = client.get("/rules").text
    assert "New profile" in html and "0.425" in html


def test_create_profile(user_client, db):
    response = _post(
        user_client,
        "/rules/new",
        name="Finance",
        threshold="0,6",
        recipients="a@example.org\nb@example.org",
        rule_id=["", ""],
        rule_text=["Blanchiment de capitaux", "  "],
    )
    assert response.status_code == 303
    profile = db.scalar(select(Profile).where(Profile.name == "Finance"))
    assert profile.owner_id == dev_user(db, "user").id
    assert profile.threshold == Decimal("0.600")
    assert profile.recipient_list() == ["a@example.org", "b@example.org"]
    assert [r.text for r in profile.rules] == ["Blanchiment de capitaux"]
    assert profile.rescore_status == "pending"


def test_create_validation_errors(user_client, db):
    response = _post(
        user_client,
        "/rules/new",
        name="",
        threshold="1.5",
        recipients="good@example.org, not-an-email",
        rule_text=["x"],
    )
    assert response.status_code == 422
    html = response.text
    assert "Indiquez un nom." in html
    assert "Le seuil doit être un nombre entre 0 et 1." in html
    assert "Adresses invalides : not-an-email" in html
    assert 'aria-invalid="true"' in html
    assert db.scalar(select(Profile)) is None


def test_edit_form_renders(client, db, settings):
    from lexwatch.scoring.factory import default_threshold_for
    from lexwatch.web.routes.rules import format_threshold

    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1", "r2"))
    html = client.get(f"/rules/{profile.id}").text
    default = format_threshold(default_threshold_for(settings), "fr")
    assert f'placeholder="{default}"' in html  # default threshold as placeholder
    assert 'value="up-1"' in html and 'value="down-0"' in html
    assert 'hx-post="/rules/preview"' in html


def test_reorder_add_remove_without_saving(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1", "r2", "r3"))
    data = _form(profile)
    response = client.post(
        f"/rules/{profile.id}",
        data={**data, "action": "up-2", "csrf_token": csrf_token(client)},
    )
    assert response.status_code == 200
    html = response.text
    assert html.index(">r3</textarea>") < html.index(">r2</textarea>")
    assert 'id="up-1"' in html and "autofocus" in html  # focus follows the moved rule
    response = client.post(
        f"/rules/{profile.id}",
        data={**data, "action": "remove-0", "csrf_token": csrf_token(client)},
    )
    assert ">r1</textarea>" not in response.text
    response = client.post(
        f"/rules/{profile.id}", data={**data, "action": "add", "csrf_token": csrf_token(client)}
    )
    assert response.text.count('<textarea id="rule-') == 4
    db.refresh(profile)
    assert [r.text for r in profile.rules] == ["r1", "r2", "r3"]  # nothing saved


def test_save_reorder_keeps_rule_ids_and_scores(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1", "r2"))
    make_match(db, profile, make_bill(db), 0.7)
    ids = [r.id for r in profile.rules]
    data = _form(profile, rule_id=[str(ids[1]), str(ids[0])], rule_text=["r2", "r1"])
    assert _post(client, f"/rules/{profile.id}", **data).status_code == 303
    db.expire_all()
    profile = db.get(Profile, profile.id)
    assert [(r.id, r.text) for r in profile.rules] == [(ids[1], "r2"), (ids[0], "r1")]
    assert profile.rescore_status == "idle"  # order does not change the average
    assert db.scalar(select(RuleScore).where(RuleScore.rule_id == ids[0])) is not None


def test_save_changed_rule_sets_pending_and_drops_its_scores(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1", "r2"))
    make_match(db, profile, make_bill(db), 0.7)
    ids = [r.id for r in profile.rules]
    data = _form(profile, rule_text=["r1 changed", "r2"])
    assert _post(client, f"/rules/{profile.id}", **data).status_code == 303
    db.expire_all()
    profile = db.get(Profile, profile.id)
    assert profile.rescore_status == "pending"
    assert [r.text for r in profile.rules] == ["r1 changed", "r2"]
    assert db.scalar(select(RuleScore).where(RuleScore.rule_id == ids[0])) is None
    assert db.scalar(select(RuleScore).where(RuleScore.rule_id == ids[1])) is not None


def test_save_removed_rule(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1", "r2"))
    ids = [r.id for r in profile.rules]
    data = _form(profile, rule_id=[str(ids[1])], rule_text=["r2"])
    assert _post(client, f"/rules/{profile.id}", **data).status_code == 303
    assert db.get(Rule, ids[0]) is None
    assert db.get(Profile, profile.id).rescore_status == "pending"


def test_name_only_change_does_not_rescore(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1",))
    assert _post(client, f"/rules/{profile.id}", **_form(profile, name="AML 2")).status_code == 303
    profile = db.get(Profile, profile.id)
    assert profile.name == "AML 2" and profile.rescore_status == "idle"


def _not_implemented(*args, **kwargs):
    raise NotImplementedError


def test_threshold_only_change_without_compute_matches_queues_rescore(client, db, monkeypatch):
    monkeypatch.setattr(scoring_service, "recompute_profile_matches", _not_implemented)
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1",))
    _post(client, f"/rules/{profile.id}", **_form(profile, threshold="0.7"))
    profile = db.get(Profile, profile.id)
    assert profile.threshold == Decimal("0.700")
    assert profile.rescore_status == "pending"


def test_threshold_only_change_recomputes_matches(client, db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        scoring_service,
        "recompute_profile_matches",
        lambda s, pid, settings=None: calls.append((pid, settings.default_threshold)) or 0,
    )
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1",))
    _post(client, f"/rules/{profile.id}", **_form(profile, threshold="0.7"))
    assert calls == [(profile.id, 0.5)]
    assert db.get(Profile, profile.id).rescore_status == "idle"


def test_rescore_state_cell_polls_while_pending(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1",))
    profile.rescore_status = "pending"
    db.flush()
    html = client.get("/rules").text
    assert f'hx-get="/rules/{profile.id}/status"' in html and "réévaluation en cours" in html
    # Shown after the profile name (no separate column); the only polled element.
    assert re.search(r'>AML</a>\s*<span id="status-\d+" class="status" hx-get', html)
    assert html.count("hx-trigger") == 1
    assert '<th scope="col">Évaluation</th>' not in html
    profile.rescore_status = "idle"
    db.flush()
    cell = client.get(f"/rules/{profile.id}/status").text
    assert "hx-get" not in cell and "en cours" not in cell


def test_delete_with_confirmation(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1",))
    confirm = client.get(f"/rules/{profile.id}/delete")
    assert confirm.status_code == 200 and "Supprimer « AML »" in confirm.text
    response = client.post(
        f"/rules/{profile.id}/delete",
        data={"csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get(Profile, profile.id) is None


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", ""), ("post", ""), ("get", "/delete"), ("post", "/delete"), ("get", "/status")],
)
def test_user_cannot_touch_another_users_profile(user_client, db, method, suffix):
    profile = make_profile(db, other_user(db), "Theirs", ("r1",))
    url = f"/rules/{profile.id}{suffix}"
    if method == "get":
        response = user_client.get(url)
    else:
        response = user_client.post(
            url, data={**_form(profile, name="Hacked"), "csrf_token": csrf_token(user_client)}
        )
    assert response.status_code == 403
    assert db.get(Profile, profile.id).name == "Theirs"


def test_user_sees_only_own_profiles(user_client, db):
    make_profile(db, other_user(db), "Theirs")
    make_profile(db, dev_user(db, "user"), "Mine")
    html = user_client.get("/rules").text
    assert "Mine" in html and "Theirs" not in html


def test_admin_manages_all_profiles(admin_client, db):
    profile = make_profile(db, other_user(db), "Theirs", ("r1",))
    assert admin_client.get(f"/rules/{profile.id}").status_code == 200
    assert "someone-else" in admin_client.get("/rules").text  # owner column


def test_missing_profile_404(client):
    assert client.get("/rules/999999").status_code == 404


def test_rules_post_requires_csrf(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("r1",))
    response = client.post(f"/rules/{profile.id}", data=_form(profile, name="X"))
    assert response.status_code == 403
    response = client.post(f"/rules/{profile.id}/delete", data={})
    assert response.status_code == 403
    assert db.get(Profile, profile.id).name == "AML"


def _fake_preview(session, scorer, rule_texts, threshold, limit=50):
    from sqlalchemy import select as sel

    from lexwatch.models import Bill
    from lexwatch.scoring.text import bill_text

    bills = list(session.scalars(sel(Bill).order_by(Bill.filing_date.desc()).limit(limit)))
    per_rule = [scorer.score_many(text, [bill_text(b) for b in bills]) for text in rule_texts]
    rows = []
    for i, bill in enumerate(bills):
        scores = [s[i] for s in per_rule]
        mean = scoring_service.profile_score(scores)
        rows.append(
            PreviewRow(
                bill.number, bill.title, mean, scoring_service.is_match(mean, threshold), scores
            )
        )
    return rows


def test_preview_with_fake_scorer(client, db, monkeypatch):
    monkeypatch.setattr(scoring_service, "preview_matches", _fake_preview)
    make_bill(db, "8601", title="Lutte contre le blanchiment de capitaux")
    make_bill(db, "8602", title="Code de la route")
    response = client.post(
        "/rules/preview",
        data={"rule_text": ["blanchiment capitaux"], "threshold": "", "name": "x"},
        headers={"HX-Request": "true", "X-CSRF-Token": csrf_token(client)},
    )
    assert response.status_code == 200
    html = response.text
    assert "<html" not in html
    assert "/bills/8601" in html and "/bills/8602" not in html
    assert "1 des 2 projets les plus récents seraient retenus." in html
    assert "blanchiment capitaux : 1,00" in html


def test_preview_without_js_uses_action(client, db, monkeypatch):
    monkeypatch.setattr(scoring_service, "preview_matches", _fake_preview)
    make_bill(db, "8601", title="Lutte contre le blanchiment de capitaux")
    response = client.post(
        "/rules/new",
        data={
            "csrf_token": csrf_token(client),
            "action": "preview",
            "name": "x",
            "rule_text": ["blanchiment"],
        },
    )
    assert response.status_code == 200
    assert "<html" in response.text and "/bills/8601" in response.text
    assert db.scalar(select(Profile)) is None


def test_preview_when_scoring_not_implemented(client, db, monkeypatch):
    monkeypatch.setattr(scoring_service, "preview_matches", _not_implemented)
    response = client.post(
        "/rules/preview",
        data={"rule_text": ["x"], "csrf_token": csrf_token(client)},
    )
    assert response.status_code == 200
    assert "service d’évaluation n’est pas configuré" in response.text


def test_preview_when_scorer_fails(client, db, monkeypatch):
    def boom(*args, **kwargs):
        raise ScoringError("down")

    monkeypatch.setattr(scoring_service, "preview_matches", boom)
    response = client.post(
        "/rules/preview", data={"rule_text": ["x"], "csrf_token": csrf_token(client)}
    )
    assert "ne répond pas" in response.text


def test_preview_needs_rules(client):
    response = client.post(
        "/rules/preview", data={"rule_text": [" "], "csrf_token": csrf_token(client)}
    )
    assert "Ajoutez une règle" in response.text


def test_profile_without_rules_is_rejected(user_client, db):
    response = _post(user_client, "/rules/new", name="Empty", rule_id=["", ""], rule_text=["", " "])
    assert response.status_code == 422
    assert "Ajoutez au moins une règle." in response.text
    assert db.scalar(select(Profile)) is None


def _preview_runs(db):
    from lexwatch.models import Run

    return list(db.scalars(select(Run).where(Run.kind == "preview").order_by(Run.id)))


def test_preview_records_billed_units_as_a_preview_run(client, db):
    scoring_service.clear_preview_cache()
    make_bill(db, "8601", title="Lutte contre le blanchiment de capitaux")
    data = {"rule_text": ["blanchiment capitaux"], "name": "x", "csrf_token": csrf_token(client)}
    assert "/bills/8601" in client.post("/rules/preview", data=data).text
    (run,) = _preview_runs(db)
    assert (run.status, run.requested_by) == ("success", dev_user(db, "admin").id)
    assert run.billed_units >= 1 and run.started_at == run.finished_at is not None
    # Served from the preview cache: nothing billed, no new run.
    client.post("/rules/preview", data=data)
    assert len(_preview_runs(db)) == 1
    scoring_service.clear_preview_cache()


def test_preview_with_nothing_billed_records_no_run(client, db, monkeypatch):
    monkeypatch.setattr(scoring_service, "preview_matches", lambda *a, **k: [])
    client.post("/rules/preview", data={"rule_text": ["x"], "csrf_token": csrf_token(client)})
    assert _preview_runs(db) == []


def test_preview_is_triggered_by_the_button_only(client):
    html = client.get("/rules/new").text
    assert "hx-trigger" not in html
    button = html.split('value="preview"', 1)[1].split(">", 1)[0]
    assert 'hx-post="/rules/preview"' in button
    section = html.split('id="preview"', 1)[1].split(">", 1)[0]
    assert "hx-post" not in section
