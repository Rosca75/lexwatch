"""Digest fixes from the end-to-end run: bootstrap suppression, first digest after profile
creation, order, decimal comma, title fallback and truncation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from lexwatch.ingest.pipeline import run_ingestion
from lexwatch.models import Bill, Match, Profile, Run, User
from lexwatch.notify.digest import build_digest, send_digests_report
from lexwatch.sources.base import BillRecord
from tests.helpers import make_test_settings

NOW = datetime(2026, 11, 19, 6, 30, tzinfo=UTC)
OLD = NOW - timedelta(days=30)
SETTINGS = make_test_settings(base_url="https://lexwatch.example.lu")


@pytest.fixture
def owner(db) -> User:
    user = User(oidc_sub="digest-e2e", email="owner@x.lu", role="user", language="fr")
    db.add(user)
    db.flush()
    return user


def add_bill(db, number, *, first_seen=OLD, changed=OLD, title=None, type_="projet") -> Bill:
    bill = Bill(
        number=number,
        type=type_,
        title=f"Projet de loi {number}" if title is None else title,
        source="test",
        first_seen_at=first_seen,
        last_changed_at=changed,
    )
    db.add(bill)
    db.flush()
    return bill


def add_profile(db, owner, *, last=None, created=OLD) -> Profile:
    profile = Profile(
        name="Fiscalité",
        owner_id=owner.id,
        recipients="a@x.lu",
        last_digest_at=last,
        created_at=created,
    )
    db.add(profile)
    db.flush()
    return profile


def add_match(db, profile, bill, *, score=0.81, computed=OLD) -> Match:
    match = Match(
        profile_id=profile.id,
        bill_number=bill.number,
        profile_score=score,
        matched=True,
        computed_at=computed,
    )
    db.add(match)
    db.flush()
    return match


def add_bootstrap_run(db, start, end) -> Run:
    run = Run(
        kind="daily",
        status="success" if end else "running",
        started_at=start,
        finished_at=end,
        details={"bootstrap": True},
    )
    db.add(run)
    db.flush()
    return run


# -- bootstrap ---------------------------------------------------------------------------------


class _Adapter:
    name = "test-source"

    def __init__(self, records):
        self.records = records

    def fetch(self, client):
        return self.records


def _record(number: str) -> BillRecord:
    return BillRecord(
        number=number,
        type="projet",
        title=f"Projet de loi {number}",
        status=None,
        filing_date=None,
        closing_date=None,
        author=None,
        dossier_url=None,
        summary=None,
        source="test-source",
        source_updated_at=None,
        extra={},
    )


def test_ingestion_into_empty_table_marks_run_bootstrap(db):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    first = Run(kind="daily", status="running", details={})
    db.add(first)
    db.flush()
    result = run_ingestion(
        db, first, adapters=[_Adapter([_record("1"), _record("2")])], client=client
    )
    assert result.bootstrap is True
    assert first.details["bootstrap"] is True

    second = Run(kind="daily", status="running", details={})
    db.add(second)
    db.flush()
    result = run_ingestion(db, second, adapters=[_Adapter([_record("3")])], client=client)
    assert result.bootstrap is False and result.added == 1
    assert "bootstrap" not in second.details


def test_empty_ingestion_is_not_bootstrap(db):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    run = Run(kind="daily", status="running", details={})
    db.add(run)
    db.flush()
    result = run_ingestion(db, run, adapters=[_Adapter([])], client=client)
    assert result.bootstrap is False
    assert "bootstrap" not in run.details


def test_bills_from_a_bootstrap_run_are_not_news(db, owner):
    start, end = NOW - timedelta(hours=6), NOW - timedelta(hours=4)
    add_bootstrap_run(db, start, end)
    profile = add_profile(db, owner, last=NOW - timedelta(days=1))
    during = NOW - timedelta(hours=5)
    historical = add_bill(db, "8000", first_seen=during, changed=during)
    add_match(db, profile, historical, computed=during)
    later = add_bill(db, "8001", first_seen=during, changed=NOW - timedelta(hours=1))
    add_match(db, profile, later, computed=during)
    fresh = add_bill(
        db, "8002", first_seen=NOW - timedelta(hours=2), changed=NOW - timedelta(hours=2)
    )
    add_match(db, profile, fresh, computed=NOW - timedelta(hours=2))

    digest = build_digest(db, profile, profile.last_digest_at, "fr", settings=SETTINGS, now=NOW)
    assert digest is not None
    assert digest.bill_numbers == ["8002", "8001"]  # newest number first
    # 8001 was first seen during the bootstrap: changed, not new (normal weight in HTML).
    assert 'font-weight:600;">Projet de loi 8001' not in digest.html
    assert 'font-weight:600;">Projet de loi 8002' in digest.html


def test_running_bootstrap_run_suppresses_its_own_digest(db, owner):
    """The digest step of the bootstrap run itself (finished_at still NULL) sends nothing."""
    add_bootstrap_run(db, NOW - timedelta(minutes=30), None)
    profile = add_profile(db, owner, created=NOW - timedelta(days=2))
    during = NOW - timedelta(minutes=20)
    for number in ("1", "2", "3"):
        add_match(
            db, profile, add_bill(db, number, first_seen=during, changed=during), computed=during
        )
    assert build_digest(db, profile, None, settings=SETTINGS, now=NOW) is None
    report = send_digests_report(db, now=NOW, dry_run=True, settings=SETTINGS, echo=lambda s: None)
    assert report.sent == 0 and report.skipped == 1


# -- first digest after profile creation -------------------------------------------------------


def test_first_digest_only_lists_changes_after_profile_creation(db, owner):
    profile = add_profile(db, owner, last=None, created=NOW - timedelta(hours=3))
    before = add_bill(
        db, "10", first_seen=NOW - timedelta(hours=5), changed=NOW - timedelta(hours=5)
    )
    after = add_bill(db, "11", changed=NOW - timedelta(hours=2))
    add_match(db, profile, before)
    add_match(db, profile, after)
    digest = build_digest(db, profile, None, settings=SETTINGS, now=NOW)
    assert digest is not None and digest.bill_numbers == ["11"]


# -- rendering -----------------------------------------------------------------------------------


def test_fr_digest_uses_decimal_comma_en_uses_point(db, owner):
    profile = add_profile(db, owner, last=NOW - timedelta(days=1))
    add_match(db, profile, add_bill(db, "20", changed=NOW - timedelta(hours=1)), score=0.4567)
    fr = build_digest(db, profile, profile.last_digest_at, "fr", settings=SETTINGS, now=NOW)
    en = build_digest(db, profile, profile.last_digest_at, "en", settings=SETTINGS, now=NOW)
    assert "0,46" in fr.html and "0,46" in fr.text and "0.46" not in fr.text
    assert "0.46" in en.html and "0.46" in en.text


def test_digest_title_fallback_and_truncation(db, owner):
    profile = add_profile(db, owner, last=NOW - timedelta(days=1))
    changed = NOW - timedelta(hours=1)
    add_match(db, profile, add_bill(db, "8627", changed=changed, title=""))
    add_match(db, profile, add_bill(db, "8628", changed=changed, title="", type_="proposition"))
    long_title = "Projet de loi " + "portant modification " * 30
    add_match(db, profile, add_bill(db, "8629", changed=changed, title=long_title))
    fr = build_digest(db, profile, profile.last_digest_at, "fr", settings=SETTINGS, now=NOW)
    en = build_digest(db, profile, profile.last_digest_at, "en", settings=SETTINGS, now=NOW)
    assert "Projet de loi n° 8627" in fr.text
    assert "Proposition de loi n° 8628" in fr.text
    assert "Government bill 8627" in en.text
    line = next(li for li in fr.text.splitlines() if li.startswith("8629"))
    title = line.split("  ", 1)[1]
    assert len(title) == 200 and title.endswith("…")
    assert long_title.strip() not in fr.html
