"""Email digest: selection window, rendering, recipients, sending, last_digest_at."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lexwatch.i18n import catalog
from lexwatch.models import Bill, Match, Profile, User
from lexwatch.notify import digest as digest_mod
from lexwatch.notify.digest import (
    DigestError,
    build_digest,
    parse_recipients,
    send_digests,
    send_digests_report,
)
from tests.auth.smtp_catcher import smtp_catcher
from tests.helpers import make_test_settings

NOW = datetime(2026, 11, 19, 6, 30, tzinfo=UTC)
LAST = NOW - timedelta(days=1)
OLD = NOW - timedelta(days=30)


@pytest.fixture
def owner(db) -> User:
    user = User(oidc_sub="digest-owner", email="owner@x.lu", role="user", language="fr")
    db.add(user)
    db.flush()
    return user


def add_bill(db, number, *, first_seen=OLD, changed=OLD, status="Déposé", title=None) -> Bill:
    bill = Bill(
        number=number,
        title=title or f"Projet de loi {number} portant modification",
        status=status,
        source="test",
        first_seen_at=first_seen,
        last_changed_at=changed,
    )
    db.add(bill)
    db.flush()
    return bill


def add_profile(db, owner, name="Fiscalité", recipients="a@x.lu", last=LAST) -> Profile:
    profile = Profile(
        name=name, owner_id=owner.id, recipients=recipients, last_digest_at=last, created_at=OLD
    )
    db.add(profile)
    db.flush()
    return profile


def add_match(db, profile, bill, *, matched=True, score=0.81, computed=OLD) -> Match:
    match = Match(
        profile_id=profile.id,
        bill_number=bill.number,
        profile_score=score,
        matched=matched,
        computed_at=computed,
    )
    db.add(match)
    db.flush()
    return match


@pytest.fixture
def scenario(db, owner):
    """Profile with: a new bill, a changed bill, an old one, an unmatched new one, a
    newly matched old one."""
    profile = add_profile(db, owner)
    new = add_bill(
        db, "8501", first_seen=NOW - timedelta(hours=2), changed=NOW - timedelta(hours=2)
    )
    changed = add_bill(
        db, "8410", changed=NOW - timedelta(hours=3), status="Avis du Conseil d'État"
    )
    old = add_bill(db, "8000")
    unmatched = add_bill(
        db, "8502", first_seen=NOW - timedelta(hours=1), changed=NOW - timedelta(hours=1)
    )
    rematched = add_bill(db, "7999")
    add_match(db, profile, new, score=0.9)
    add_match(db, profile, changed, score=0.75)
    add_match(db, profile, old)
    add_match(db, profile, unmatched, matched=False, score=0.2, computed=NOW)
    add_match(db, profile, rematched, score=0.55, computed=NOW - timedelta(hours=5))
    return profile


SETTINGS = make_test_settings(base_url="https://lexwatch.example.lu")


# --- recipients -----------------------------------------------------------------------------


def test_parse_recipients():
    valid, invalid = parse_recipients(
        "a@x.lu, b@x.lu;c@x.lu\nA@X.lu\r\n  Jean Dupont <jd@etat.lu> ; not-an-email; x@y ;; "
    )
    assert valid == ["a@x.lu", "b@x.lu", "c@x.lu", "jd@etat.lu"]
    assert invalid == ["not-an-email", "x@y"]
    assert parse_recipients("") == ([], [])
    assert parse_recipients(None) == ([], [])
    assert parse_recipients("a b@x.lu") == ([], ["a b@x.lu"])


# --- selection --------------------------------------------------------------------------------


def test_build_digest_selects_new_changed_and_rematched(db, scenario):
    digest = build_digest(db, scenario, LAST, "fr", settings=SETTINGS, now=NOW)
    assert digest is not None
    assert digest.bill_numbers == ["8501", "8410", "7999"]
    assert digest.recipients == ["a@x.lu"]
    assert digest.profile_id == scenario.id


def test_nothing_new_returns_none(db, owner):
    profile = add_profile(db, owner)
    add_match(db, profile, add_bill(db, "8000"))
    add_match(db, profile, add_bill(db, "8001", changed=NOW - timedelta(hours=1)), matched=False)
    assert build_digest(db, profile, LAST, settings=SETTINGS, now=NOW) is None


def test_window_excludes_lower_bound_and_future(db, owner):
    profile = add_profile(db, owner)
    add_match(db, profile, add_bill(db, "1", changed=LAST))  # reported last time
    add_match(db, profile, add_bill(db, "2", changed=NOW + timedelta(minutes=1)))  # next time
    assert build_digest(db, profile, LAST, settings=SETTINGS, now=NOW) is None


def test_first_digest_uses_last_24h_and_ignores_match_computations(db, owner):
    profile = add_profile(db, owner, last=None)
    add_match(db, profile, add_bill(db, "10", changed=NOW - timedelta(hours=5)))
    add_match(db, profile, add_bill(db, "11", changed=NOW - timedelta(hours=30)))
    add_match(db, profile, add_bill(db, "12"), computed=NOW - timedelta(hours=1))
    digest = build_digest(db, profile, None, settings=SETTINGS, now=NOW)
    assert digest.bill_numbers == ["10"]


# --- rendering --------------------------------------------------------------------------------


def test_render_html_and_text_fr(db, scenario):
    digest = build_digest(db, scenario, LAST, "fr", settings=SETTINGS, now=NOW)
    assert digest.subject == "lexwatch : Fiscalité – 3 projet(s) nouveau(x) ou modifié(s)"
    html, text = digest.html, digest.text
    assert '<html lang="fr">' in html
    assert 'href="https://lexwatch.example.lu/bills/8501"' in html
    assert 'href="https://lexwatch.example.lu/?profile=' in html
    assert "Chambre des Députés du Grand-Duché de Luxembourg" in html
    assert 'href="https://www.chd.lu"' in html
    assert "Avis du Conseil d&#39;État" in html  # escaped
    assert "0.90" in html and "0.75" in html
    for forbidden in ("<img", "<style", "<script", "<link"):
        assert forbidden not in html
    assert "https://lexwatch.example.lu/bills/8410" in text
    assert "Statut : Avis du Conseil d'État | Score : 0.75" in text
    assert "Source : Chambre des Députés" in text
    assert "<td" not in text


def test_render_in_owner_language_en(db, owner, scenario):
    owner.language = "en"
    db.flush()
    report = send_digests_report(db, now=NOW, dry_run=True, settings=SETTINGS, echo=lambda s: None)
    (digest,) = report.digests
    assert digest.subject == "lexwatch: Fiscalité – 3 new or changed bill(s)"
    assert '<html lang="en">' in digest.html
    assert "Source: Chambre des Députés du Grand-Duché de Luxembourg" in digest.text


def test_status_label_uses_catalog_when_present(monkeypatch, db, scenario):
    fr = dict(catalog("fr"))
    fr["bill.status.Déposé"] = "Déposé (libellé)"
    monkeypatch.setattr(
        digest_mod,
        "t",
        lambda key, lang=None, **kw: fr.get(key, key).format(**kw) if kw else fr.get(key, key),
    )
    assert digest_mod.status_label("Déposé", "fr") == "Déposé (libellé)"
    assert digest_mod.status_label("Inconnu", "fr") == "Inconnu"
    assert digest_mod.status_label(None, "fr") == ""


def test_html_escapes_titles(db, owner):
    profile = add_profile(db, owner, name="<b>P</b>")
    add_match(
        db,
        profile,
        add_bill(db, "20", title="<script>x</script>", changed=NOW - timedelta(hours=1)),
    )
    digest = build_digest(db, profile, LAST, settings=SETTINGS, now=NOW)
    assert "<script>x" not in digest.html and "&lt;script&gt;" in digest.html
    assert "<b>P</b>" not in digest.html
    assert "<script>x</script>" in digest.text  # plain text is not escaped


def test_mail_catalog_keys_exist_in_both_languages():
    fr, en = catalog("fr"), catalog("en")
    mine = {k for k in fr if k.startswith(("email.", "auth."))}
    assert mine == {k for k in en if k.startswith(("email.", "auth."))}


# --- sending ----------------------------------------------------------------------------------


def smtp_settings(port: int, **overrides):
    values = {
        "base_url": "https://lexwatch.example.lu",
        "smtp_host": "127.0.0.1",
        "smtp_port": port,
        "smtp_starttls": False,
        "smtp_ssl": False,
        "smtp_from": "lexwatch <lexwatch@example.lu>",
        "smtp_timeout_seconds": 5,
        **overrides,
    }
    return make_test_settings(**values)


def test_send_digests_sends_and_updates_last_digest_at(db, owner, scenario):
    other = add_profile(db, owner, name="Vide", recipients="z@x.lu")  # nothing new
    no_rcpt = add_profile(db, owner, name="Sans", recipients="invalid")
    add_match(db, no_rcpt, add_bill(db, "30", changed=NOW - timedelta(hours=1)))
    with smtp_catcher() as (port, state):
        settings = smtp_settings(port, smtp_username="u", smtp_password="p")
        sent = send_digests(db, now=NOW, settings=settings)
    assert sent == 1
    assert len(state.messages) == 1
    received = state.messages[0]
    assert received.rcpt_to == ["a@x.lu"]
    assert received.mail_from == "lexwatch@example.lu"
    assert received.auth  # logged in
    msg = received.message
    assert msg["Subject"].startswith("lexwatch : Fiscalité")
    assert msg["Message-ID"].endswith("@example.lu>")
    assert msg["Date"]
    assert msg.get_content_type() == "multipart/alternative"
    parts = {p.get_content_type(): p.get_content() for p in msg.iter_parts()}
    assert "https://lexwatch.example.lu/bills/8501" in parts["text/plain"]
    assert "<table" in parts["text/html"]
    assert scenario.last_digest_at == NOW
    assert other.last_digest_at == LAST
    assert no_rcpt.last_digest_at == LAST
    # Same day, nothing new any more.
    with smtp_catcher() as (port, state):
        assert send_digests(db, now=NOW + timedelta(minutes=5), settings=smtp_settings(port)) == 0
        assert state.messages == []


def test_send_failure_is_reported_and_does_not_stop_others(db, owner, scenario, caplog):
    second = add_profile(db, owner, name="Deux", recipients="refused@x.lu")
    add_match(db, second, add_bill(db, "40", changed=NOW - timedelta(hours=1)))
    third = add_profile(db, owner, name="Trois", recipients="b@x.lu")
    add_match(db, third, add_bill(db, "41", changed=NOW - timedelta(hours=1)))
    with smtp_catcher() as (port, state):
        state.refuse.add("refused@x.lu")
        with pytest.raises(DigestError) as excinfo:
            send_digests(db, now=NOW, settings=smtp_settings(port))
    report = excinfo.value.report
    assert report.sent == 2
    assert list(report.failed) == [second.id]
    assert report.failed[second.id].startswith("SMTPRecipientsRefused: 5")
    # Type and SMTP code only: no recipient address in runs.error_message or the logs.
    assert "refused@x.lu" not in report.failed[second.id]
    assert "refused@x.lu" not in caplog.text
    assert second.last_digest_at == LAST
    assert scenario.last_digest_at == NOW and third.last_digest_at == NOW
    assert [m.rcpt_to for m in state.messages] == [["a@x.lu"], ["b@x.lu"]]


def test_partial_refusal_counts_as_sent(db, owner):
    profile = add_profile(db, owner, recipients="ok@x.lu, refused@x.lu")
    add_match(db, profile, add_bill(db, "50", changed=NOW - timedelta(hours=1)))
    with smtp_catcher() as (port, state):
        state.refuse.add("refused@x.lu")
        assert send_digests(db, now=NOW, settings=smtp_settings(port)) == 1
    assert profile.last_digest_at == NOW


def test_connection_failure_is_reported(db, scenario):
    with smtp_catcher() as (port, _state):
        pass  # server closed: nothing listens on port any more
    with pytest.raises(DigestError, match="1 digest"):
        send_digests(db, now=NOW, settings=smtp_settings(port, smtp_timeout_seconds=2))
    assert scenario.last_digest_at == LAST


def test_dry_run_changes_nothing(db, scenario):
    printed: list[str] = []
    report = send_digests_report(db, now=NOW, dry_run=True, settings=SETTINGS, echo=printed.append)
    assert report.sent == 1 and not report.failed
    assert "Subject: lexwatch : Fiscalité" in printed[0]
    assert "To: a@x.lu" in printed[0]
    assert scenario.last_digest_at == LAST


def test_smtp_not_configured_sends_nothing(db, scenario):
    assert send_digests(db, now=NOW, settings=make_test_settings(smtp_host="")) == 0
    assert scenario.last_digest_at == LAST
