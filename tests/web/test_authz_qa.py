"""QA (stream 3): authorization matrix, anonymous access, CSRF on every mutating route, IDOR.

Written against docs/build-prompt.md sections 6 and 8 and docs/architecture.md, not against the
implementation. User A is the DEV_AUTH "user"; user B is another plain user; admin is the
DEV_AUTH "admin".
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lexwatch.db import get_db
from lexwatch.models import Profile, Rule, RuleScore, Run, User
from lexwatch.web.app import create_app
from tests.helpers import csrf_token, make_test_settings
from tests.web.factories import dev_user, make_bill, make_match, make_profile, other_user

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
SECRET_NAME = "Profil-secret-de-B"
SECRET_RULE = "texte de regle confidentiel de B"


@pytest.fixture
def user_a(db: Session, user_client: TestClient) -> User:
    return dev_user(db, "user")


@pytest.fixture
def profile_b(db: Session) -> Profile:
    owner = other_user(db, "user-b")
    return make_profile(db, owner, name=SECRET_NAME, rules=(SECRET_RULE, "deuxieme regle"))


@pytest.fixture
def anon_client(db: Session) -> Iterator[TestClient]:
    """A client of an app with DEV_AUTH off: requests are really anonymous."""
    application = create_app(make_test_settings(dev_auth=False))

    def _get_db() -> Iterator[Session]:
        yield db

    application.dependency_overrides[get_db] = _get_db
    with TestClient(application) as c:
        yield c


def _form(token: str, **extra: object) -> dict[str, object]:
    data: dict[str, object] = {
        "csrf_token": token,
        "name": "Hijacked",
        "threshold": "0,9",
        "recipients": "",
        "rule_id": [""],
        "rule_text": ["hijack"],
        "action": "save",
    }
    data.update(extra)
    return data


# --- authorization matrix: user A against user B's profile -------------------------------


@pytest.mark.parametrize(
    "path",
    ["/rules/{id}", "/rules/{id}/delete", "/rules/{id}/status"],
)
def test_user_cannot_read_foreign_profile_pages(user_client, user_a, profile_b, path):
    response = user_client.get(path.format(id=profile_b.id))
    assert response.status_code == 403
    assert SECRET_NAME not in response.text
    assert SECRET_RULE not in response.text


def test_user_cannot_poll_foreign_profile_status_via_htmx(user_client, user_a, profile_b):
    response = user_client.get(
        f"/rules/{profile_b.id}/status", headers={"HX-Request": "true", "HX-Target": "x"}
    )
    assert response.status_code == 403
    assert SECRET_NAME not in response.text


def test_user_cannot_edit_foreign_profile(user_client, db, user_a, profile_b):
    token = csrf_token(user_client)
    rules_before = [(r.id, r.text) for r in profile_b.rules]
    response = user_client.post(f"/rules/{profile_b.id}", data=_form(token))
    assert response.status_code == 403
    db.expire_all()
    profile = db.get(Profile, profile_b.id)
    assert profile.name == SECRET_NAME
    assert profile.threshold is None
    assert [(r.id, r.text) for r in profile.rules] == rules_before


@pytest.mark.parametrize("action", ["preview", "add", "remove-0", "up-1", "down-0"])
def test_user_cannot_use_editing_actions_on_foreign_profile(user_client, user_a, profile_b, action):
    token = csrf_token(user_client)
    response = user_client.post(f"/rules/{profile_b.id}", data=_form(token, action=action))
    assert response.status_code == 403
    assert SECRET_NAME not in response.text
    assert SECRET_RULE not in response.text


def test_user_cannot_delete_foreign_profile(user_client, db, user_a, profile_b):
    token = csrf_token(user_client)
    response = user_client.post(f"/rules/{profile_b.id}/delete", data={"csrf_token": token})
    assert response.status_code == 403
    db.expire_all()
    assert db.get(Profile, profile_b.id) is not None


def test_user_rules_list_hides_foreign_profiles(user_client, user_a, profile_b):
    response = user_client.get("/rules")
    assert response.status_code == 200
    assert SECRET_NAME not in response.text
    assert f"/rules/{profile_b.id}" not in response.text


def test_user_cannot_select_foreign_profile_on_home(user_client, db, user_a, profile_b):
    own = make_profile(db, user_a, name="Mon profil")
    bill = make_bill(db, title="Projet visible seulement par B")
    make_match(db, profile_b, bill, 0.9)
    response = user_client.get(f"/?profile={profile_b.id}")
    assert response.status_code == 200
    assert SECRET_NAME not in response.text
    assert f'value="{profile_b.id}"' not in response.text
    # The page falls back to the user's own profile; B's match must not show up.
    assert "Projet visible seulement par B" not in response.text
    assert f'value="{own.id}" selected' in response.text
    # And the foreign id is not remembered in the session either.
    again = user_client.get("/")
    assert SECRET_NAME not in again.text
    assert f'value="{own.id}" selected' in again.text


def test_user_without_profiles_cannot_select_foreign_profile(user_client, db, user_a, profile_b):
    bill = make_bill(db, title="Facture quelconque")
    make_match(db, profile_b, bill, 0.42)
    response = user_client.get(f"/?profile={profile_b.id}&all=1")
    assert response.status_code == 200
    assert SECRET_NAME not in response.text
    # No profile selected: no score column (the 0,42 of B's profile must not leak).
    assert "0,42" not in response.text and "0.42" not in response.text


def test_bill_detail_does_not_leak_foreign_profile_or_rules(user_client, db, user_a, profile_b):
    bill = make_bill(db)
    make_match(db, profile_b, bill, 0.77)
    response = user_client.get(f"/bills/{bill.number}")
    assert response.status_code == 200
    assert SECRET_NAME not in response.text
    assert SECRET_RULE not in response.text
    assert "0,77" not in response.text


def test_missing_profile_is_404_not_500(user_client, user_a):
    for path in ("/rules/999999", "/rules/999999/delete", "/rules/999999/status"):
        assert user_client.get(path).status_code == 404
    assert user_client.get("/rules/abc").status_code in (404, 422)


# --- admin can do everything ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/rules/{id}", "/rules/{id}/delete", "/rules/{id}/status"],
)
def test_admin_can_view_foreign_profile_pages(admin_client, profile_b, path):
    response = admin_client.get(path.format(id=profile_b.id))
    assert response.status_code == 200


def test_admin_sees_all_profiles(admin_client, profile_b):
    assert SECRET_NAME in admin_client.get("/rules").text
    assert SECRET_NAME in admin_client.get(f"/?profile={profile_b.id}").text


def test_admin_can_edit_and_delete_foreign_profile(admin_client, db, profile_b):
    token = csrf_token(admin_client)
    response = admin_client.post(
        f"/rules/{profile_b.id}",
        data=_form(token, name="Renamed by admin", rule_id=[""], rule_text=["x"]),
        follow_redirects=False,
    )
    assert response.status_code == 303
    db.expire_all()
    profile = db.get(Profile, profile_b.id)
    assert profile.name == "Renamed by admin"
    # The owner does not change when an admin edits.
    assert profile.owner.oidc_sub == "user-b"
    response = admin_client.post(
        f"/rules/{profile_b.id}/delete", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    db.expire_all()
    assert db.get(Profile, profile_b.id) is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/admin"),
        ("get", "/admin/export/bills.csv"),
        ("get", "/admin/export/matches.csv"),
        ("post", "/admin/run"),
        ("post", "/admin/rescore"),
    ],
)
def test_user_cannot_use_admin_routes(user_client, db, user_a, method, path):
    runs_before = db.scalar(select(func.count(Run.id)))
    if method == "post":
        response = user_client.post(path, data={"csrf_token": csrf_token(user_client)})
    else:
        response = user_client.get(path)
    assert response.status_code == 403
    assert db.scalar(select(func.count(Run.id))) == runs_before


def test_user_does_not_see_admin_nav(user_client, user_a):
    assert 'href="/admin"' not in user_client.get("/").text


def test_user_rescore_all_does_not_touch_profiles(user_client, db, user_a, profile_b):
    user_client.post("/admin/rescore", data={"csrf_token": csrf_token(user_client)})
    db.expire_all()
    assert db.get(Profile, profile_b.id).rescore_status == "idle"


# --- anonymous ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/?profile=1",
        "/bills/8410",
        "/rules",
        "/rules/new",
        "/rules/1",
        "/rules/1/status",
        "/rules/1/delete",
        "/admin",
        "/admin/export/bills.csv",
        "/admin/export/matches.csv",
    ],
)
def test_anonymous_is_redirected_to_login(anon_client, path):
    response = anon_client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/login")


def test_anonymous_htmx_gets_hx_redirect(anon_client):
    response = anon_client.get(
        "/", headers={"HX-Request": "true", "HX-Target": "results"}, follow_redirects=False
    )
    assert response.headers.get("HX-Redirect") == "/login"
    assert response.status_code in (200, 204, 401)


def test_anonymous_post_with_valid_csrf_is_still_rejected(anon_client, db, profile_b):
    page = anon_client.get("/does-not-exist", headers={"Accept": "text/html"})
    assert page.status_code == 404
    token = CSRF_RE.search(page.text).group(1)
    runs_before = db.scalar(select(func.count(Run.id)))
    for path in ("/admin/run", "/admin/rescore", "/rules/new", f"/rules/{profile_b.id}/delete"):
        response = anon_client.post(
            path, data=_form(token), headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert response.status_code == 303, path
        assert response.headers["location"].endswith("/login")
    db.expire_all()
    assert db.scalar(select(func.count(Run.id))) == runs_before
    assert db.get(Profile, profile_b.id) is not None
    assert db.scalar(select(func.count(Profile.id)).where(Profile.name == "Hijacked")) == 0


# --- CSRF on every mutating route ---------------------------------------------------------

MUTATING = [
    "/lang",
    "/logout",
    "/rules/new",
    "/rules/preview",
    "/rules/{id}",
    "/rules/{id}/delete",
    "/admin/run",
    "/admin/rescore",
]


@pytest.mark.parametrize("path", MUTATING)
@pytest.mark.parametrize("token", [None, "", "forged-token"])
def test_mutating_route_rejects_missing_or_wrong_csrf(admin_client, db, profile_b, path, token):
    csrf_token(admin_client)  # the session has a real token; the request must still fail
    data = _form("x", lang="en", next="/")
    if token is None:
        data.pop("csrf_token")
    else:
        data["csrf_token"] = token
    response = admin_client.post(path.format(id=profile_b.id), data=data, follow_redirects=False)
    assert response.status_code == 403


@pytest.mark.parametrize("path", MUTATING)
def test_csrf_header_with_wrong_value_is_rejected(admin_client, profile_b, path):
    real = csrf_token(admin_client)
    data = _form(real, lang="en", next="/")
    response = admin_client.post(
        path.format(id=profile_b.id),
        data=data,
        headers={"X-CSRF-Token": "wrong"},
        follow_redirects=False,
    )
    # A wrong header token is a wrong token, even if the body field is right.
    assert response.status_code == 403


@pytest.mark.parametrize("path", MUTATING)
def test_csrf_json_body_without_header_is_rejected(admin_client, profile_b, path):
    csrf_token(admin_client)
    response = admin_client.post(
        path.format(id=profile_b.id), json={"lang": "en"}, follow_redirects=False
    )
    assert response.status_code == 403


def test_csrf_token_of_another_session_is_rejected(app: FastAPI, db, profile_b):
    with TestClient(app) as attacker, TestClient(app) as victim:
        attacker.get("/login?as=user")
        victim.get("/login?as=admin")
        foreign = csrf_token(attacker)
        csrf_token(victim)
        before = db.scalar(select(func.count(Run.id)))
        response = victim.post("/admin/run", data={"csrf_token": foreign}, follow_redirects=False)
        assert response.status_code == 403
        assert db.scalar(select(func.count(Run.id))) == before
        response = victim.post(
            f"/rules/{profile_b.id}/delete", data={"csrf_token": foreign}, follow_redirects=False
        )
        assert response.status_code == 403
        db.expire_all()
        assert db.get(Profile, profile_b.id) is not None


def test_csrf_failures_have_no_side_effects(admin_client, db, profile_b):
    admin = dev_user(db, "admin")
    csrf_token(admin_client)
    runs = db.scalar(select(func.count(Run.id)))
    admin_client.post("/admin/run", data={"csrf_token": "bad"})
    admin_client.post("/admin/rescore", data={"csrf_token": "bad"})
    admin_client.post("/rules/new", data=_form("bad", name="Nope"))
    admin_client.post(f"/rules/{profile_b.id}/delete", data={"csrf_token": "bad"})
    admin_client.post("/lang", data={"csrf_token": "bad", "lang": "en"})
    admin_client.post("/logout", data={"csrf_token": "bad"})
    db.expire_all()
    assert db.scalar(select(func.count(Run.id))) == runs
    assert db.get(Profile, profile_b.id).rescore_status == "idle"
    assert db.get(Profile, profile_b.id) is not None
    assert db.scalar(select(func.count(Profile.id)).where(Profile.name == "Nope")) == 0
    assert db.get(User, admin.id).language == "fr"
    # Still logged in.
    assert admin_client.get("/admin", follow_redirects=False).status_code == 200


def test_get_routes_do_not_mutate(admin_client, db, profile_b):
    """Safe methods only: confirming a delete with GET must not delete."""
    admin_client.get(f"/rules/{profile_b.id}/delete")
    admin_client.get("/admin/run")
    admin_client.get("/admin/rescore")
    admin_client.get("/lang?lang=en")
    db.expire_all()
    assert db.get(Profile, profile_b.id) is not None
    assert db.scalar(select(func.count(Run.id))) == 0
    assert db.get(Profile, profile_b.id).rescore_status == "idle"


# --- IDOR on rule ids ---------------------------------------------------------------------


def _rule_snapshot(db: Session, profile_id: int) -> list[tuple[int, str, int]]:
    db.expire_all()
    return [
        (r.id, r.text, r.position)
        for r in db.scalars(
            select(Rule).where(Rule.profile_id == profile_id).order_by(Rule.position)
        )
    ]


def test_foreign_rule_id_in_own_profile_does_not_touch_foreign_rule(
    user_client, db, user_a, profile_b
):
    own = make_profile(db, user_a, name="A", rules=("regle de A",))
    bill = make_bill(db)
    make_match(db, profile_b, bill, 0.8)
    b_before = _rule_snapshot(db, profile_b.id)
    b_scores = db.scalar(
        select(func.count(RuleScore.id)).where(RuleScore.rule_id.in_([r[0] for r in b_before]))
    )
    foreign_id = b_before[0][0]
    token = csrf_token(user_client)
    response = user_client.post(
        f"/rules/{own.id}",
        data=_form(
            token,
            name="A",
            threshold="",
            rule_id=[str(own.rules[0].id), str(foreign_id)],
            rule_text=["regle de A", "texte injecte"],
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _rule_snapshot(db, profile_b.id) == b_before
    assert (
        db.scalar(
            select(func.count(RuleScore.id)).where(RuleScore.rule_id.in_([r[0] for r in b_before]))
        )
        == b_scores
    )
    a_rules = _rule_snapshot(db, own.id)
    assert [text for _, text, _ in a_rules] == ["regle de A", "texte injecte"]
    assert foreign_id not in [rid for rid, _, _ in a_rules]


def test_foreign_rule_id_when_creating_profile_is_ignored(user_client, db, user_a, profile_b):
    b_before = _rule_snapshot(db, profile_b.id)
    token = csrf_token(user_client)
    response = user_client.post(
        "/rules/new",
        data=_form(
            token,
            name="Nouveau",
            threshold="",
            rule_id=[str(b_before[0][0])],
            rule_text=["vol de regle"],
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _rule_snapshot(db, profile_b.id) == b_before
    created = db.scalar(select(Profile).where(Profile.name == "Nouveau"))
    assert created.owner_id == user_a.id
    assert [r.text for r in created.rules] == ["vol de regle"]
    assert b_before[0][0] not in [r.id for r in created.rules]


def test_admin_cross_profile_rule_id_does_not_move_rules(admin_client, db, user_a, profile_b):
    target = make_profile(db, user_a, name="Cible", rules=("une",))
    b_before = _rule_snapshot(db, profile_b.id)
    token = csrf_token(admin_client)
    admin_client.post(
        f"/rules/{target.id}",
        data=_form(
            token,
            name="Cible",
            threshold="",
            rule_id=[str(b_before[0][0]), str(b_before[1][0])],
            rule_text=["changee", "aussi"],
        ),
    )
    assert _rule_snapshot(db, profile_b.id) == b_before
    assert [t for _, t, _ in _rule_snapshot(db, target.id)] == ["changee", "aussi"]


def test_non_numeric_and_negative_rule_ids_are_harmless(user_client, db, user_a):
    own = make_profile(db, user_a, name="A", rules=("r1",))
    token = csrf_token(user_client)
    response = user_client.post(
        f"/rules/{own.id}",
        data=_form(
            token,
            name="A",
            threshold="",
            rule_id=["-1", "abc", "99999999999999999999999"],
            rule_text=["x", "y", "z"],
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert [t for _, t, _ in _rule_snapshot(db, own.id)] == ["x", "y", "z"]
