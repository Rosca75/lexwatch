"""Final fixes in the jobs: bootstrap flag carried over an interrupted bootstrap run."""

from __future__ import annotations

import os
import signal
import time
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from lexwatch.ingest import pipeline
from lexwatch.jobs.daily import carries_bootstrap, create_run, execute_run
from lexwatch.jobs.stopping import STOPPING_MESSAGE, WorkerStopping, clear_stop, request_stop
from lexwatch.models import Bill, Match, Profile, Rule, RuleScore, Run, User
from lexwatch.notify import digest
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


# -- worker stop (SIGTERM) within a long scoring run ------------------------------------------


@pytest.fixture
def stop_flag():
    clear_stop()
    yield
    clear_stop()


class StopAfterScorer(FakeScorer):
    """Fake scorer that requests a worker stop (as the SIGTERM handler does) on call N."""

    def __init__(self, stop_on_call: int, *, action=None) -> None:
        super().__init__()
        self.max_documents = 2
        self.stop_on_call = stop_on_call
        self.action = action or request_stop

    def score_many(self, query, bill_texts):
        scores = super().score_many(query, bill_texts)
        if len(self.calls) == self.stop_on_call:
            self.action()
        return scores


def add_bills(db, count: int) -> None:
    for i in range(count):
        db.add(
            Bill(
                number=str(500 + i),
                title=f"Projet de loi blanchiment de capitaux {i}",
                source="test",
                score_state="pending",
            )
        )
    db.flush()


def scored_pairs(db) -> int:
    return len(list(db.scalars(select(RuleScore.id))))


def test_stop_request_ends_a_scoring_run_after_the_current_batch(
    db, settings, profile, stop_flag, monkeypatch
):
    monkeypatch.setattr(
        pipeline, "run_ingestion", lambda *a, **k: pipeline.IngestResult(0, 0, 0, sources={})
    )
    add_bills(db, 10)  # 5 batches of 2
    scorer = StopAfterScorer(stop_on_call=2)
    monkeypatch.setattr(factory, "get_scorer", lambda s: scorer)
    digests = []
    monkeypatch.setattr(digest, "send_digests_report", lambda *a, **k: digests.append(1))
    run = create_run(db, "daily")
    db.commit()

    with pytest.raises(WorkerStopping):
        execute_run(db, run, settings)

    assert len(scorer.calls) == 2  # no batch after the stop request
    assert scored_pairs(db) == 4  # the two completed batches were committed
    db.refresh(run)
    assert run.status == "failed"
    assert run.error_message == STOPPING_MESSAGE
    assert run.finished_at is not None
    assert run.details["progress"] == {"done": 4, "total": 10}
    assert run.billed_units == 2
    assert digests == []  # the digest step did not run
    assert "scoring_complete" not in run.details


def test_stop_request_during_a_profile_rescore_in_tick(
    db, settings, profile, stop_flag, monkeypatch
):
    from lexwatch.jobs import worker

    monkeypatch.setattr(worker, "daily_due", lambda session, settings, now=None: False)
    add_bills(db, 6)
    scorer = StopAfterScorer(stop_on_call=1)
    monkeypatch.setattr(factory, "get_scorer", lambda s: scorer)
    profile.rescore_status = "pending"
    db.commit()

    with pytest.raises(WorkerStopping):
        worker.tick(db, settings)

    assert len(scorer.calls) == 1 and scored_pairs(db) == 2
    run = db.scalar(select(Run).where(Run.kind == "rescore"))
    assert run.status == "failed" and run.error_message == STOPPING_MESSAGE
    db.refresh(profile)
    assert profile.rescore_status == "running"
    worker.recover_interrupted(db)  # next start: rescored again
    db.refresh(profile)
    assert profile.rescore_status == "pending"


def test_stop_request_during_rescore_all(db, settings, profile, stop_flag, monkeypatch):
    from lexwatch.jobs.rescore import execute_rescore_all, queue_rescore_all

    add_bills(db, 6)
    run = queue_rescore_all(db)
    scorer = StopAfterScorer(stop_on_call=1)
    with pytest.raises(WorkerStopping):
        execute_rescore_all(db, settings, run, scorer=scorer)
    db.refresh(run)
    assert run.status == "failed" and run.error_message == STOPPING_MESSAGE
    assert len(scorer.calls) == 1 and scored_pairs(db) == 2


def test_sigterm_stops_the_worker_loop_within_a_scoring_run(
    db, settings, profile, stop_flag, monkeypatch
):
    """The real signal handler: SIGTERM during scoring -> run failed, run_worker returns."""
    from lexwatch import db as db_module
    from lexwatch.jobs import worker

    monkeypatch.setattr(worker, "daily_due", lambda session, settings, now=None: False)
    monkeypatch.setattr(
        pipeline, "run_ingestion", lambda *a, **k: pipeline.IngestResult(0, 0, 0, sources={})
    )
    monkeypatch.setattr(digest, "send_digests_report", lambda *a, **k: digest.DigestReport())
    monkeypatch.setattr(db_module, "make_sessionmaker", lambda engine: lambda: nullcontext(db))
    add_bills(db, 10)
    scorer = StopAfterScorer(stop_on_call=1, action=lambda: os.kill(os.getpid(), signal.SIGTERM))
    monkeypatch.setattr(factory, "get_scorer", lambda s: scorer)
    run = create_run(db, "manual")
    db.commit()

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    started = time.monotonic()
    try:
        worker.run_worker(replace(settings, worker_poll_seconds=30))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    assert time.monotonic() - started < 10  # did not wait for the next poll
    assert len(scorer.calls) == 1
    db.refresh(run)
    assert run.status == "failed" and run.error_message == STOPPING_MESSAGE
