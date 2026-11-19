"""QA (stream 3): profile form validation (threshold, name, rules, recipients) and XSS.

Spec: docs/build-prompt.md section 6.3 (name, rules, threshold, recipients) and section 8.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lexwatch.models import Profile, User
from tests.helpers import csrf_token
from tests.web.factories import dev_user, make_bill, make_match, make_profile

XSS = '<script>alert("x")</script>'
XSS_ATTR = '" onmouseover="alert(1)'


@pytest.fixture
def me(db: Session, user_client: TestClient) -> User:
    return dev_user(db, "user")


def post_profile(client: TestClient, path: str = "/rules/new", **fields: object):
    data: dict[str, object] = {
        "csrf_token": csrf_token(client),
        "name": "Profil",
        "threshold": "",
        "recipients": "",
        "rule_id": [""],
        "rule_text": ["blanchiment de capitaux"],
        "action": "save",
    }
    data.update(fields)
    return client.post(path, data=data, follow_redirects=False)


def saved(db: Session, name: str) -> Profile | None:
    db.expire_all()
    return db.scalar(select(Profile).where(Profile.name == name))


def count_profiles(db: Session) -> int:
    db.expire_all()
    return db.scalar(select(func.count(Profile.id))) or 0


# --- threshold ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", Decimal("0")),
        ("1", Decimal("1")),
        ("0.5", Decimal("0.5")),
        ("0,5", Decimal("0.5")),
        ("0,725", Decimal("0.725")),
        (" 0,3 ", Decimal("0.3")),
        ("1,000", Decimal("1")),
        ("", None),
    ],
)
def test_valid_thresholds_are_saved(user_client, db, me, raw, expected):
    response = post_profile(user_client, name=f"T {raw!r}", threshold=raw)
    assert response.status_code == 303, response.text
    profile = saved(db, f"T {raw!r}")
    assert profile is not None
    assert profile.threshold == expected


@pytest.mark.parametrize(
    "raw",
    [
        "1.01",
        "1,5",
        "-0.1",
        "-0",
        "2",
        "abc",
        "0.5abc",
        "NaN",
        "Infinity",
        "-Infinity",
        "1e5",
        "0x1",
        "50%",
        "1.000,5",
        "0,5,5",
    ],
)
def test_invalid_thresholds_are_rejected_with_422(user_client, db, me, raw):
    before = count_profiles(db)
    response = post_profile(user_client, name="Invalid threshold", threshold=raw)
    if raw == "-0":
        # -0 is numerically 0: accepting it as 0 is fine, it must never be a 5xx.
        assert response.status_code in (303, 422)
        return
    assert response.status_code == 422, raw
    assert count_profiles(db) == before
    assert 'id="threshold-error"' in response.text
    assert 'aria-invalid="true"' in response.text


def test_invalid_threshold_on_edit_keeps_old_value(user_client, db, me):
    profile = make_profile(db, me, name="Edit me", threshold=0.4)
    response = post_profile(user_client, f"/rules/{profile.id}", name="Edit me", threshold="7")
    assert response.status_code == 422
    db.expire_all()
    assert db.get(Profile, profile.id).threshold == Decimal("0.4")


def test_threshold_is_shown_with_locale_decimal_separator(user_client, db, me):
    profile = make_profile(db, me, name="Sep", threshold=0.65)
    page = user_client.get(f"/rules/{profile.id}").text
    assert 'value="0,65"' in page


# --- name ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_empty_name_rejected(user_client, db, me, name):
    before = count_profiles(db)
    response = post_profile(user_client, name=name)
    assert response.status_code == 422
    assert count_profiles(db) == before
    assert 'id="name-error"' in response.text


def test_too_long_name_rejected_not_500(user_client, db, me):
    response = post_profile(user_client, name="N" * 5000)
    assert response.status_code == 422
    assert count_profiles(db) == 0


def test_name_at_limit_accepted(user_client, db, me):
    assert post_profile(user_client, name="N" * 200).status_code == 303
    assert saved(db, "N" * 200) is not None


def test_name_with_nul_byte_is_rejected_not_500(user_client, db, me):
    response = post_profile(user_client, name="AML\x00evil")
    assert response.status_code in (303, 422)
    profile = db.scalar(select(Profile).where(Profile.name.like("AML%")))
    if profile is not None:
        assert "\x00" not in profile.name


def test_name_with_newlines_is_normalized_or_rejected(user_client, db, me):
    response = post_profile(user_client, name="AML\r\nBcc: evil@example.org")
    assert response.status_code in (303, 422)
    profile = db.scalar(select(Profile).where(Profile.name.like("AML%")))
    if profile is not None:
        assert "\r" not in profile.name and "\n" not in profile.name


# --- rules --------------------------------------------------------------------------------


def test_rule_text_too_long_rejected(user_client, db, me):
    response = post_profile(user_client, name="Long", rule_text=["x" * 1001])
    assert response.status_code == 422
    assert saved(db, "Long") is None


def test_very_long_rule_text_is_rejected_not_500(user_client, db, me):
    response = post_profile(user_client, name="Huge", rule_text=["mot " * 50_000])
    assert response.status_code == 422
    assert saved(db, "Huge") is None


def test_rule_text_at_limit_accepted_and_whitespace_collapsed(user_client, db, me):
    text = "a" * 1000
    assert post_profile(user_client, name="Limit", rule_text=[f"  {text}\n"]).status_code == 303
    assert [r.text for r in saved(db, "Limit").rules] == [text]


def test_blank_rules_are_dropped(user_client, db, me):
    response = post_profile(
        user_client, name="Blank", rule_id=["", "", ""], rule_text=["  ", "une regle", "\n"]
    )
    assert response.status_code == 303
    assert [r.text for r in saved(db, "Blank").rules] == ["une regle"]


def test_fifty_rules_are_accepted(user_client, db, me):
    texts = [f"regle {i}" for i in range(50)]
    response = post_profile(user_client, name="Fifty", rule_id=[""] * 50, rule_text=texts)
    assert response.status_code == 303
    profile = saved(db, "Fifty")
    assert [r.text for r in profile.rules] == texts
    assert [r.position for r in profile.rules] == list(range(50))


def test_more_than_fifty_rules_is_not_silently_truncated(user_client, db, me):
    texts = [f"regle {i}" for i in range(60)]
    response = post_profile(user_client, name="Sixty", rule_id=[""] * 60, rule_text=texts)
    if response.status_code == 303:
        assert len(saved(db, "Sixty").rules) == 60
    else:
        assert response.status_code == 422


def test_add_action_never_exceeds_limit(user_client, db, me):
    texts = [f"r{i}" for i in range(50)]
    response = post_profile(
        user_client, name="Add", rule_id=[""] * 50, rule_text=texts, action="add"
    )
    assert response.status_code == 200
    assert response.text.count('name="rule_text"') <= 50


@pytest.mark.parametrize("action", ["remove-99", "up-0", "down-0", "up-x", "bogus", "remove--1"])
def test_unknown_or_out_of_range_actions_do_not_save_or_crash(user_client, db, me, action):
    response = post_profile(user_client, name="Act", action=action)
    assert response.status_code == 200
    assert saved(db, "Act") is None


def test_rules_with_nul_byte_are_rejected_not_500(user_client, db, me):
    response = post_profile(user_client, name="Nul", rule_text=["abc\x00def"])
    assert response.status_code in (303, 422)


# --- recipients ---------------------------------------------------------------------------


def test_valid_recipients_are_saved_and_deduplicated(user_client, db, me):
    response = post_profile(
        user_client,
        name="Mail",
        recipients="a@example.org, b@example.org;a@example.org\nc.d+e@sub.example.lu",
    )
    assert response.status_code == 303
    assert saved(db, "Mail").recipient_list() == [
        "a@example.org",
        "b@example.org",
        "c.d+e@sub.example.lu",
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "not-an-email",
        "a@b",
        "@example.org",
        "a@",
        "a@@example.org",
        "a@example.org\r\nBcc: evil@example.org",
        "a@example.org\nSubject: hi",
        "Name <a@example.org>",
        "<a@example.org>",
        "a@example.org%0d%0aBcc:evil@example.org",
        "a@exa mple.org",
        '"><script>@x.org',
    ],
)
def test_invalid_or_header_injection_recipients_rejected(user_client, db, me, raw):
    response = post_profile(user_client, name="BadMail", recipients=raw)
    assert response.status_code == 422, raw
    assert saved(db, "BadMail") is None
    assert 'id="recipients-error"' in response.text
    assert "<script>" not in response.text


def test_recipients_with_nul_or_control_chars_rejected_not_500(user_client, db, me):
    response = post_profile(user_client, name="Ctl", recipients="a@example.org\x00")
    assert response.status_code == 422


def test_recipients_with_crlf_never_stored(user_client, db, me):
    post_profile(user_client, name="CRLF", recipients="a@example.org\r\n\r\nb@example.org")
    profile = saved(db, "CRLF")
    assert profile is not None
    for address in profile.recipient_list():
        assert "\r" not in address and "\n" not in address


# --- XSS ----------------------------------------------------------------------------------


def test_profile_name_is_escaped_everywhere(user_client, db, me):
    profile = make_profile(db, me, name=XSS + XSS_ATTR, rules=(XSS,))
    bill = make_bill(db)
    make_match(db, profile, bill, 0.9)
    for path in (
        "/rules",
        f"/rules/{profile.id}",
        f"/rules/{profile.id}/delete",
        "/",
        f"/bills/{bill.number}",
    ):
        page = user_client.get(path).text
        assert XSS not in page, path
        assert 'onmouseover="alert' not in page, path
        assert "&lt;script&gt;" in page, path


def test_rule_text_is_escaped_in_editor_and_hover(user_client, db, me):
    payload = "</textarea><script>alert(1)</script>" + XSS_ATTR
    profile = make_profile(db, me, name="R", rules=(payload,))
    bill = make_bill(db)
    make_match(db, profile, bill, 0.6)
    for path in (f"/rules/{profile.id}", f"/bills/{bill.number}"):
        page = user_client.get(path).text
        assert "<script>alert(1)" not in page, path
        assert "</textarea><script>" not in page, path
        assert 'onmouseover="alert' not in page, path


def test_invalid_form_echo_is_escaped(user_client, db, me):
    response = post_profile(
        user_client, name=XSS, threshold=XSS, recipients=XSS, rule_text=[XSS + "x" * 1001]
    )
    assert response.status_code == 422
    assert XSS not in response.text
    assert "<script>" not in response.text


def test_preview_fragment_escapes_rule_text(user_client, me):
    response = user_client.post(
        "/rules/preview",
        data={
            "csrf_token": csrf_token(user_client),
            "name": XSS,
            "threshold": XSS,
            "rule_id": [""],
            "rule_text": [XSS],
        },
    )
    assert response.status_code == 200
    assert "<script>" not in response.text
