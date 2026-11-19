"""QA (stream 4): daily email digest against spec section 6.5, sent to an in-process SMTP catcher.

"One email per profile per day, only when there is something new or changed. Plain HTML and
text versions, same minimal style, links back to the app."
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from lexwatch.models import Bill, Match, Profile, User
from lexwatch.notify.digest import DigestError, send_digests, send_digests_report
from tests.auth.smtp_catcher import Received, smtp_catcher
from tests.helpers import make_test_settings

NOW = datetime(2026, 11, 19, 6, 30, tzinfo=UTC)  # 07:30 in Luxembourg
YESTERDAY = NOW - timedelta(days=1)
OLD = NOW - timedelta(days=60)
BASE = "https://lex.example.lu/app"


# --- builders ------------------------------------------------------------------------------


def mk_user(db, sub: str, language: str = "fr") -> User:
    user = User(oidc_sub=sub, email=f"{sub}@x.lu", role="user", language=language)
    db.add(user)
    db.flush()
    return user


def mk_profile(db, owner: User, name: str, recipients: str, last=YESTERDAY) -> Profile:
    profile = Profile(name=name, owner_id=owner.id, recipients=recipients, last_digest_at=last)
    db.add(profile)
    db.flush()
    return profile


def mk_bill(db, number: str, *, first_seen=OLD, changed=OLD, title=None, status="Déposé") -> Bill:
    bill = Bill(
        number=number,
        title=title or f"Projet de loi {number}",
        status=status,
        source="test",
        first_seen_at=first_seen,
        last_changed_at=changed,
    )
    db.add(bill)
    db.flush()
    return bill


def mk_match(db, profile: Profile, bill: Bill, *, matched=True, computed=OLD) -> Match:
    match = Match(
        profile_id=profile.id,
        bill_number=bill.number,
        profile_score=0.8,
        matched=matched,
        computed_at=computed,
    )
    db.add(match)
    db.flush()
    return match


def new_bill_for(db, profile: Profile, number: str, at: datetime = NOW - timedelta(hours=2)):
    return mk_match(db, profile, mk_bill(db, number, first_seen=at, changed=at))


def smtp(port: int, **overrides):
    values = {
        "base_url": BASE + "/",
        "smtp_host": "127.0.0.1",
        "smtp_port": port,
        "smtp_starttls": False,
        "smtp_ssl": False,
        "smtp_from": "lexwatch@example.lu",
        "smtp_timeout_seconds": 3,
        **overrides,
    }
    return make_test_settings(**values)


def run(db, now=NOW, **overrides) -> tuple[list[Received], object]:
    with smtp_catcher() as (port, state):
        for addr in overrides.pop("refuse", ()):
            state.refuse.add(addr)
        report = send_digests_report(db, now=now, settings=smtp(port, **overrides))
    return list(state.messages), report


def parts(msg: EmailMessage) -> dict[str, str]:
    return {p.get_content_type(): p.get_content() for p in msg.walk() if not p.is_multipart()}


# --- one email per profile per day ---------------------------------------------------------


def test_one_email_per_profile_with_all_its_bills(db):
    owner = mk_user(db, "qa-d1")
    p1 = mk_profile(db, owner, "Fiscalité", "a@x.lu, b@x.lu")
    p2 = mk_profile(db, owner, "Santé", "c@x.lu")
    for n in ("9001", "9002", "9003"):
        new_bill_for(db, p1, n)
    new_bill_for(db, p2, "9004")
    messages, report = run(db)
    assert len(messages) == 2
    by_rcpt = {tuple(sorted(m.rcpt_to)): m for m in messages}
    assert set(by_rcpt) == {("a@x.lu", "b@x.lu"), ("c@x.lu",)}
    text1 = parts(by_rcpt[("a@x.lu", "b@x.lu")].message)["text/plain"]
    assert all(n in text1 for n in ("9001", "9002", "9003"))
    assert "9004" not in text1  # no leak of another profile's bills
    text2 = parts(by_rcpt[("c@x.lu",)].message)["text/plain"]
    assert "9004" in text2 and "9001" not in text2
    assert p1.last_digest_at == NOW and p2.last_digest_at == NOW


def test_unmatched_and_old_bills_are_not_in_the_digest(db):
    owner = mk_user(db, "qa-d2")
    profile = mk_profile(db, owner, "P", "a@x.lu")
    new_bill_for(db, profile, "9101")
    unmatched = mk_bill(db, "9102", first_seen=NOW - timedelta(hours=1))
    mk_match(db, profile, unmatched, matched=False, computed=NOW - timedelta(hours=1))
    mk_match(db, profile, mk_bill(db, "9103"))  # old, unchanged
    (message,), _ = run(db)
    text = parts(message.message)["text/plain"]
    assert "9101" in text and "9102" not in text and "9103" not in text


def test_nothing_new_sends_no_email(db):
    owner = mk_user(db, "qa-d3")
    profile = mk_profile(db, owner, "P", "a@x.lu")
    mk_match(db, profile, mk_bill(db, "9201"))
    messages, report = run(db)
    assert messages == []
    assert report.sent == 0
    assert profile.last_digest_at == YESTERDAY


def test_profile_without_matches_sends_no_email(db):
    owner = mk_user(db, "qa-d3b")
    profile = mk_profile(db, owner, "Empty", "a@x.lu", last=None)
    mk_bill(db, "9202", first_seen=NOW - timedelta(hours=1))
    messages, _ = run(db)
    assert messages == []
    assert profile.last_digest_at is None


def test_second_run_same_day_sends_no_duplicate(db):
    owner = mk_user(db, "qa-d4")
    profile = mk_profile(db, owner, "P", "a@x.lu")
    new_bill_for(db, profile, "9301")
    first, _ = run(db, now=NOW)
    assert len(first) == 1
    for later in (NOW, NOW + timedelta(minutes=1), NOW + timedelta(hours=6)):
        again, _ = run(db, now=later)
        assert again == [], later
    assert profile.last_digest_at == NOW


def test_second_run_same_day_with_new_changes_still_one_email_per_day(db):
    owner = mk_user(db, "qa-d5")
    profile = mk_profile(db, owner, "P", "a@x.lu")
    new_bill_for(db, profile, "9401")
    first, _ = run(db, now=NOW)
    assert len(first) == 1
    new_bill_for(db, profile, "9402", at=NOW + timedelta(hours=2))
    again, _ = run(db, now=NOW + timedelta(hours=4))  # 11:30 Luxembourg, same day
    assert again == []


def test_changes_after_a_digest_arrive_in_the_next_days_digest(db):
    owner = mk_user(db, "qa-d6")
    profile = mk_profile(db, owner, "P", "a@x.lu")
    new_bill_for(db, profile, "9501")
    run(db, now=NOW)
    new_bill_for(db, profile, "9502", at=NOW + timedelta(hours=2))
    (message,), _ = run(db, now=NOW + timedelta(days=1))
    text = parts(message.message)["text/plain"]
    assert "9502" in text and "9501" not in text


# --- content: links, both parts, attribution, language -------------------------------------


def test_html_and_text_parts_with_absolute_bill_links_and_attribution(db):
    owner = mk_user(db, "qa-d7")
    profile = mk_profile(db, owner, "Énergie", "a@x.lu")
    new_bill_for(db, profile, "9601")
    new_bill_for(db, profile, "9602")
    (received,), _ = run(db)
    msg = received.message
    body = parts(msg)
    assert set(body) == {"text/plain", "text/html"}
    html, text = body["text/html"], body["text/plain"]
    for number in ("9601", "9602"):
        url = f"{BASE}/bills/{number}"
        assert f'href="{url}"' in html
        assert url in text
    # every link in the HTML is absolute: back to BASE_URL or to the source
    hrefs = re.findall(r'href="([^"]*)"', html)
    assert hrefs
    for href in hrefs:
        assert href.startswith((BASE + "/", "https://www.chd.lu")), href
    assert "//bills" not in html and "//bills" not in text
    # attribution of the data source in both versions
    for version in (html, text):
        assert "Chambre des Députés" in version
        assert "chd.lu" in version


def test_html_is_minimal(db):
    owner = mk_user(db, "qa-d8")
    profile = mk_profile(db, owner, "P", "a@x.lu")
    new_bill_for(db, profile, "9701")
    (received,), _ = run(db)
    html = parts(received.message)["text/html"].lower()
    for forbidden in ("<img", "<script", "<style", "gradient", "box-shadow", "<link"):
        assert forbidden not in html


@pytest.mark.parametrize(
    ("language", "subject_word", "body_word"),
    [("fr", "projet", "Source :"), ("en", "bill", "Source:")],
)
def test_language_follows_profile_owner(db, language, subject_word, body_word):
    owner = mk_user(db, f"qa-lang-{language}", language=language)
    profile = mk_profile(db, owner, "P", "someone@x.lu")
    new_bill_for(db, profile, "9801")
    (received,), _ = run(db)
    msg = received.message
    assert subject_word in msg["Subject"]
    body = parts(msg)
    assert body_word in body["text/plain"]
    assert f'<html lang="{language}">' in body["text/html"]


def test_each_profile_uses_its_own_owners_language(db):
    fr_owner, en_owner = mk_user(db, "qa-fr", "fr"), mk_user(db, "qa-en", "en")
    p_fr = mk_profile(db, fr_owner, "PF", "f@x.lu")
    p_en = mk_profile(db, en_owner, "PE", "e@x.lu")
    new_bill_for(db, p_fr, "9811")
    new_bill_for(db, p_en, "9812")
    messages, _ = run(db)
    subjects = {m.rcpt_to[0]: m.message["Subject"] for m in messages}
    assert "projet" in subjects["f@x.lu"]
    assert "bill" in subjects["e@x.lu"] and "projet" not in subjects["e@x.lu"]


# --- recipients and header injection -------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "a@x.lu\r\nBcc: evil@x.lu",
        "a@x.lu\nBcc: evil@x.lu",
        "a@x.lu\rBcc: evil@x.lu",
        "a@x.lu, Bcc: evil@x.lu",
        "a@x.lu%0d%0aBcc:evil@x.lu",
        "a@x.lu\r\n\r\nevil body",
        "a@x.lu>\r\nBcc: <evil@x.lu",
        "Name\r\nBcc: evil@x.lu <a@x.lu>",
    ],
)
def test_header_injection_in_recipients(db, raw):
    owner = mk_user(db, "qa-inj")
    profile = mk_profile(db, owner, "P", raw)
    new_bill_for(db, profile, "9901")
    messages, _ = run(db)
    for received in messages:
        assert "evil@x.lu" not in received.rcpt_to
        msg = received.message
        assert msg["Bcc"] is None
        assert "evil" not in (msg["To"] or "")
        for name, value in msg.items():
            assert "\n" not in value and "\r" not in value, name


def _subject_injection_profiles(db, name: str) -> tuple[Profile, Profile]:
    owner = mk_user(db, "qa-subj")
    evil = mk_profile(db, owner, name, "a@x.lu")
    good = mk_profile(db, owner, "Good", "g@x.lu")
    new_bill_for(db, evil, "9911")
    new_bill_for(db, good, "9912")
    return evil, good


@pytest.mark.parametrize(
    "name",
    ["P\r\nBcc: evil@x.lu", "P\nBcc: evil@x.lu", "P\rX-Injected: 1", "P\r\n\r\n<b>body</b>"],
)
def test_header_injection_in_subject_is_neutralized(db, name):
    """No header can be injected through the profile name (which goes into the Subject).

    Refusing that profile (an exception) is acceptable for this test; sending it with an extra
    header or recipient is not.
    """
    _subject_injection_profiles(db, name)
    with smtp_catcher() as (port, state):
        with contextlib.suppress(ValueError):
            send_digests_report(db, now=NOW, settings=smtp(port))
        messages = list(state.messages)
    for received in messages:
        assert "evil@x.lu" not in received.rcpt_to
        msg = received.message
        assert msg["Bcc"] is None and msg["X-Injected"] is None
        assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"]
        assert "<b>body</b>" not in parts(msg).get("text/plain", "").split("\n")[0]


@pytest.mark.parametrize("name", ["P\r\nBcc: evil@x.lu", "P\nX-Injected: 1"])
def test_crlf_in_profile_name_does_not_stop_other_profiles(db, name):
    evil, good = _subject_injection_profiles(db, name)
    messages, report = run(db)
    assert "g@x.lu" in [r for m in messages for r in m.rcpt_to]
    assert good.last_digest_at == NOW
    assert all("evil@x.lu" not in m.rcpt_to for m in messages)


def test_invalid_recipients_are_ignored(db):
    owner = mk_user(db, "qa-inv")
    raw = "not-an-email; @x.lu; a@; ok@x.lu\nb@@x.lu, a b@x.lu, OK@X.LU, Second <s@x.lu>, c@-x.lu"
    profile = mk_profile(db, owner, "P", raw)
    new_bill_for(db, profile, "9921")
    (received,), report = run(db)
    assert sorted(received.rcpt_to) == ["ok@x.lu", "s@x.lu"]
    assert profile.last_digest_at == NOW


def test_only_invalid_recipients_sends_nothing_and_keeps_timestamp(db):
    owner = mk_user(db, "qa-inv2")
    profile = mk_profile(db, owner, "P", "nope; also nope; @@")
    new_bill_for(db, profile, "9931")
    messages, report = run(db)
    assert messages == []
    assert profile.last_digest_at == YESTERDAY
    assert not report.failed


def test_empty_recipients_sends_nothing(db):
    owner = mk_user(db, "qa-inv3")
    profile = mk_profile(db, owner, "P", "")
    new_bill_for(db, profile, "9941")
    messages, _ = run(db)
    assert messages == []


# --- SMTP failures -------------------------------------------------------------------------


def test_connection_failure_keeps_last_digest_at_and_retries_next_run(db):
    owner = mk_user(db, "qa-f1")
    profile = mk_profile(db, owner, "P", "a@x.lu")
    new_bill_for(db, profile, "9951")
    with smtp_catcher() as (port, _state):
        pass  # closed: connection refused
    with pytest.raises(DigestError):
        send_digests(db, now=NOW, settings=smtp(port))
    assert profile.last_digest_at == YESTERDAY
    # next run (later that day or tomorrow) still delivers the same bill
    (received,), _ = run(db, now=NOW + timedelta(hours=1))
    assert "9951" in parts(received.message)["text/plain"]
    assert profile.last_digest_at == NOW + timedelta(hours=1)


def test_all_recipients_refused_keeps_last_digest_at(db):
    owner = mk_user(db, "qa-f2")
    profile = mk_profile(db, owner, "P", "r1@x.lu, r2@x.lu")
    new_bill_for(db, profile, "9961")
    messages, report = run(db, refuse={"r1@x.lu", "r2@x.lu"})
    assert messages == []
    assert profile.id in report.failed
    assert profile.last_digest_at == YESTERDAY


def test_smtp_timeout_keeps_last_digest_at(db):
    """A relay that accepts the TCP connection but never answers: bounded by the timeout."""
    import socket

    owner = mk_user(db, "qa-f3")
    profile = mk_profile(db, owner, "P", "a@x.lu")
    new_bill_for(db, profile, "9971")
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        port = silent.getsockname()[1]
        report = send_digests_report(db, now=NOW, settings=smtp(port, smtp_timeout_seconds=0.5))
    assert profile.id in report.failed
    assert profile.last_digest_at == YESTERDAY


def test_failure_of_one_profile_does_not_lose_or_duplicate_others(db):
    owner = mk_user(db, "qa-f4")
    ok = mk_profile(db, owner, "OK", "ok@x.lu")
    bad = mk_profile(db, owner, "Bad", "bad@x.lu")
    new_bill_for(db, ok, "9981")
    new_bill_for(db, bad, "9982")
    messages, report = run(db, refuse={"bad@x.lu"})
    assert [m.rcpt_to for m in messages] == [["ok@x.lu"]]
    assert ok.last_digest_at == NOW and bad.last_digest_at == YESTERDAY
    # retry: only the failed profile is sent again
    messages, _ = run(db, now=NOW + timedelta(minutes=30))
    assert [m.rcpt_to for m in messages] == [["bad@x.lu"]]
