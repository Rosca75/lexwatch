"""Contract tests for the job/worker wiring (steps are monkeypatched, no network)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from lexwatch.ingest import pipeline
from lexwatch.jobs import worker
from lexwatch.jobs.daily import create_run, execute_run
from lexwatch.notify import digest
from lexwatch.scoring import service


@pytest.fixture
def steps(monkeypatch):
    calls: list[str] = []

    def ingest(session, run, adapters=None, client=None):
        calls.append("ingest")
        return pipeline.IngestResult(added=2, changed=1, sources={"chd": {"status": "ok"}})

    def score(session, scorer, run):
        calls.append("score")
        scorer.billed_units += 3
        return service.ScoreResult(scored=3)

    def send(session, now=None, dry_run=False, *, settings=None, echo=None):
        calls.append("digest")
        return digest.DigestReport(sent=1)

    monkeypatch.setattr(pipeline, "run_ingestion", ingest)
    monkeypatch.setattr(service, "score_pending_bills", score)
    monkeypatch.setattr(digest, "send_digests_report", send)
    return calls


def test_execute_run_success(db, settings, steps):
    run = create_run(db, "manual")
    execute_run(db, run, settings)
    assert steps == ["ingest", "score", "digest"]
    assert run.status == "success"
    assert (run.bills_added, run.bills_changed, run.bills_scored) == (2, 1, 3)
    assert run.billed_units == 3
    assert run.details["sources"]["chd"]["status"] == "ok"
    assert run.started_at and run.finished_at


def test_execute_run_partial_when_ingestion_fails(db, settings, steps, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("chd.lu structure changed")

    monkeypatch.setattr(pipeline, "run_ingestion", boom)
    run = create_run(db, "manual")
    execute_run(db, run, settings)
    assert run.status == "partial"
    assert "chd.lu structure changed" in (run.error_message or "")


def test_daily_due_and_queued_claim(db, settings):
    s = replace(settings, daily_job_time="00:00")
    assert worker.daily_due(db, s) is True
    daily = create_run(db, "daily")
    daily.status = "success"
    assert worker.daily_due(db, s) is False

    queued = create_run(db, "manual")
    assert worker.claim_queued_run(db).id == queued.id
