"""Final fixes in the jobs: bootstrap flag carried over an interrupted bootstrap run."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from lexwatch.ingest import pipeline
from lexwatch.jobs.daily import carries_bootstrap, create_run, execute_run
from lexwatch.models import Match, Profile, Rule, Run, User
from lexwatch.scoring import factory
from lexwatch.scoring.fake import FakeScorer
from lexwatch.sources.base import BillRecord

RULE = "blanchiment capitaux"
real_run_ingestion = pipeline.run_ingestion


def record(number: str, title: str) -> BillRecord:
    return BillRecord(
        number=number,
        type="projet",
        title=title,
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


class Source:
    name = "test-source"

    def __init__(self) -> None:
        self.records = [
            record("100", "Projet de loi blanchiment de capitaux"),
            record("101", "Projet de loi capitaux et blanchiment, registre"),
            record("102", "Projet de loi agriculture"),
        ]

    def fetch(self, client):
        return list(self.records)


class CrashingScorer(FakeScorer):
    """Stands in for a worker killed during the initial scoring (not a ScoringError)."""

    def score_many(self, query, bill_texts):
        raise RuntimeError("worker killed")


@pytest.fixture
def source(monkeypatch) -> Source:
    src = Source()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))

    def ingest(session, run, adapters=None, client_=None):
        return real_run_ingestion(session, run, adapters=[src], client=client)

    monkeypatch.setattr(pipeline, "run_ingestion", ingest)
    return src


@pytest.fixture
def sent(monkeypatch) -> list:
    from lexwatch.notify import mailer

    messages: list = []
    monkeypatch.setattr(mailer, "send_message", lambda settings, message: messages.append(message))
    return messages


@pytest.fixture
def profile(db) -> Profile:
    owner = User(oidc_sub="bootstrap-owner", email="owner@example.test", role="user")
    db.add(owner)
    db.flush()
    long_ago = datetime.now(UTC) - timedelta(days=10)
    prof = Profile(
        name="AML",
        owner_id=owner.id,
        recipients="a@example.test",
        created_at=long_ago,
        last_digest_at=datetime.now(UTC) - timedelta(days=3),  # older than every run
    )
    prof.rules = [Rule(text=RULE, position=0)]
    db.add(prof)
    db.flush()
    return prof


def run_with(db, monkeypatch, settings, scorer) -> Run:
    monkeypatch.setattr(factory, "get_scorer", lambda s: scorer)
    run = create_run(db, "daily")
    db.commit()
    return execute_run(db, run, replace(settings, smtp_host="smtp.test"))


def test_interrupted_bootstrap_carries_over_to_the_run_that_finishes_scoring(
    db, settings, profile, source, sent, monkeypatch
):
    # 1. Bootstrap: ingestion into the empty table, then the scoring is interrupted.
    first = run_with(db, monkeypatch, settings, CrashingScorer())
    assert first.details["bootstrap"] is True
    assert first.status == "partial" and "worker killed" in first.error_message
    assert "scoring_complete" not in first.details
    assert db.scalar(select(Match.id).limit(1)) is None

    # 2. The next run completes the initial scoring: it counts as bootstrap, no digest.
    second = run_with(db, monkeypatch, settings, FakeScorer())
    assert second.details["bootstrap"] is True
    assert second.details["scoring_complete"] is True
    assert second.status == "success"
    matched = set(db.scalars(select(Match.bill_number).where(Match.matched.is_(True))))
    assert matched == {"100", "101"}
    assert second.details["digests_sent"] == 0 and sent == []

    # 3. A real later change: a normal run, a digest with just that bill.
    source.records[1] = record("101", "Projet de loi capitaux et blanchiment, registre modifié")
    third = run_with(db, monkeypatch, settings, FakeScorer())
    assert "bootstrap" not in third.details
    assert third.bills_changed == 1
    assert third.details["digests_sent"] == 1
    assert len(sent) == 1
    body = sent[0].get_body(("plain",)).get_content()
    assert "101" in body and "100" not in body.replace("101", "")


def _run(db, status, details, minutes_ago, kind="daily") -> Run:
    start = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    run = Run(kind=kind, status=status, details=details, started_at=start, finished_at=start)
    db.add(run)
    db.flush()
    return run


def _new_run(db) -> Run:
    run = Run(kind="daily", status="running", details={}, started_at=datetime.now(UTC))
    db.add(run)
    db.flush()
    return run


@pytest.mark.parametrize(
    ("status", "details", "carried"),
    [
        ("failed", {"bootstrap": True}, True),  # crashed / stopped (recover_interrupted)
        ("partial", {"bootstrap": True}, True),  # scoring failed
        ("partial", {"bootstrap": True, "scoring_complete": True}, False),  # a source failed
        ("success", {"bootstrap": True, "scoring_complete": True}, False),
        ("failed", {}, False),
    ],
)
def test_carries_bootstrap_rules(db, status, details, carried):
    _run(db, status, details, minutes_ago=30)
    assert carries_bootstrap(db, _new_run(db)) is carried


def test_only_the_most_recent_earlier_ingest_run_counts(db):
    _run(db, "failed", {"bootstrap": True}, minutes_ago=60)
    _run(db, "success", {"scoring_complete": True}, minutes_ago=30)
    _run(db, "success", {}, minutes_ago=10, kind="rescore")  # ignored kind
    probe = _new_run(db)
    assert carries_bootstrap(db, probe) is False
    db.delete(probe)

    _run(db, "failed", {"bootstrap": True}, minutes_ago=5)
    _run(db, "failed", {}, minutes_ago=3, kind="rescore")
    assert carries_bootstrap(db, _new_run(db)) is True
