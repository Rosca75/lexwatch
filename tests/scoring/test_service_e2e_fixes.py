"""Scoring fixes from the end-to-end run: per-batch commits, progress, resume, bills without
text, recency order of the preview."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from lexwatch.models import Bill, Match, Profile, Rule, RuleScore, Run, User
from lexwatch.scoring.base import ScoringError
from lexwatch.scoring.fake import FakeScorer
from lexwatch.scoring.service import (
    clear_preview_cache,
    preview_matches,
    rescore_profile,
    score_pending_bills,
)

RULE_A = "blanchiment capitaux"
RULE_B = "financement terrorisme"


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_preview_cache()
    yield
    clear_preview_cache()


@pytest.fixture
def owner(db) -> User:
    user = User(oidc_sub="e2e-owner", email="e2e@example.test", role="user")
    db.add(user)
    db.flush()
    return user


def add_bill(db, number: str, title: str, **kw) -> Bill:
    bill = Bill(number=number, title=title, source="test", score_state="pending", **kw)
    db.add(bill)
    db.flush()
    return bill


def add_profile(db, owner, rules: list[str], name: str = "AML") -> Profile:
    profile = Profile(name=name, owner_id=owner.id)
    profile.rules = [Rule(text=t, position=i) for i, t in enumerate(rules)]
    db.add(profile)
    db.flush()
    return profile


class SmallBatchScorer(FakeScorer):
    """FakeScorer with a tiny batch size; can crash after a number of requests."""

    def __init__(self, *, max_documents: int = 2, crash_after: int | None = None, **kw):
        super().__init__(**kw)
        self.max_documents = max_documents
        self.crash_after = crash_after
        self.texts: list[str] = []

    def score_many(self, rule_text, bill_texts):
        if self.crash_after is not None and len(self.calls) >= self.crash_after:
            raise RuntimeError("worker killed")
        self.texts.extend(bill_texts)
        return super().score_many(rule_text, bill_texts)


def five_bills(db) -> list[Bill]:
    return [
        add_bill(db, str(8400 + i), f"Projet de loi {i} relatif au blanchiment de capitaux")
        for i in range(5)
    ]


def test_commit_is_called_per_batch_and_progress_written(db, owner, caplog):
    five_bills(db)
    add_profile(db, owner, [RULE_A, RULE_B])
    run = Run(kind="manual", status="running", details={"sources": {}})
    db.add(run)
    db.flush()
    snapshots: list[dict] = []

    def commit():
        snapshots.append(dict(run.details["progress"]))
        db.commit()

    scorer = SmallBatchScorer(max_documents=2)
    with caplog.at_level(logging.INFO, logger="lexwatch.scoring.service"):
        result = score_pending_bills(db, scorer, run, commit=commit)

    assert result.scored == 5
    # 2 rule texts x 5 bills = 10 pairs, batches of 2, 2, 1 per rule text.
    assert [s["done"] for s in snapshots] == [2, 4, 5, 7, 9, 10]
    assert {s["total"] for s in snapshots} == {10}
    assert run.details["progress"] == {"done": 10, "total": 10}
    assert run.details["sources"] == {}  # other details are kept
    progress_logs = [r for r in caplog.records if r.getMessage() == "scoring.progress"]
    assert progress_logs and progress_logs[-1].done == 10 and progress_logs[-1].total == 10


def test_crash_keeps_completed_batches_and_next_run_resumes(db, owner):
    five_bills(db)
    profile = add_profile(db, owner, [RULE_A, RULE_B])

    crashing = SmallBatchScorer(max_documents=2, crash_after=4)  # dies in rule B's 2nd batch
    with pytest.raises(RuntimeError):
        score_pending_bills(db, crashing, None, commit=db.commit)
    db.rollback()  # what the job does after an exception: committed batches survive

    kept = db.scalars(select(RuleScore)).all()
    assert len(kept) == 7  # rule A complete (5), rule B first batch (2)
    assert all(b.score_state == "pending" for b in db.scalars(select(Bill)))

    resumed = SmallBatchScorer(max_documents=2)
    result = score_pending_bills(db, resumed, None, commit=db.commit)
    assert result.scored == 5 and result.error is None
    # Only the 3 pairs of rule B that were not scored yet are sent again.
    assert [(q, n) for q, n in resumed.calls] == [(RULE_B, 2), (RULE_B, 1)]
    assert all(b.score_state == "scored" for b in db.scalars(select(Bill)))
    matches = db.scalars(select(Match).where(Match.profile_id == profile.id)).all()
    assert len(matches) == 5


def test_resume_recomputes_matches_when_nothing_left_to_send(db, owner):
    five_bills(db)
    profile = add_profile(db, owner, [RULE_A])
    scorer = SmallBatchScorer(max_documents=10)
    # Simulate a run that stored every score and died before marking the bills scored.
    score_pending_bills(db, scorer, None)
    db.execute(Match.__table__.delete())
    for bill in db.scalars(select(Bill)):
        bill.score_state = "pending"
    db.flush()

    again = SmallBatchScorer(max_documents=10)
    score_pending_bills(db, again, None)
    assert again.calls == []
    assert len(db.scalars(select(Match).where(Match.profile_id == profile.id)).all()) == 5
    assert all(b.score_state == "scored" for b in db.scalars(select(Bill)))


def test_changed_bill_is_rescored_even_with_older_scores(db, owner):
    bill = add_bill(db, "8500", "Projet de loi blanchiment de capitaux")
    add_profile(db, owner, [RULE_A])
    score_pending_bills(db, FakeScorer(), None)
    bill.title = "Projet de loi blanchiment de capitaux et terrorisme"
    bill.score_state = "pending"
    bill.last_changed_at = datetime.now(UTC) + timedelta(seconds=1)
    db.flush()
    scorer = FakeScorer()
    score_pending_bills(db, scorer, None)
    assert scorer.calls == [(RULE_A, 1)]


def test_rescore_profile_commits_per_batch_with_progress(db, owner):
    five_bills(db)
    profile = add_profile(db, owner, [RULE_A])
    run = Run(kind="rescore", status="running", details={"profile_id": profile.id})
    db.add(run)
    db.flush()
    commits: list[int] = []
    scorer = SmallBatchScorer(max_documents=2)
    rescore_profile(db, scorer, profile.id, force=True, run=run, commit=lambda: commits.append(1))
    assert len(commits) == 3
    assert run.details["progress"] == {"done": 5, "total": 5}
    assert run.details["profile_id"] == profile.id


# -- bills without text ------------------------------------------------------------------------


def test_bills_without_title_are_never_sent_nor_matched(db, owner):
    add_bill(db, "8627", "")
    add_bill(db, "8628", "   \n ", author="Ministre des Finances")
    add_bill(db, "8629", "Projet de loi blanchiment de capitaux")
    profile = add_profile(db, owner, [RULE_A])

    scorer = SmallBatchScorer(max_documents=10)
    result = score_pending_bills(db, scorer, None)
    assert scorer.calls == [(RULE_A, 1)]
    assert all(t.strip() for t in scorer.texts)
    assert result.scored == 1
    states = {b.number: b.score_state for b in db.scalars(select(Bill))}
    assert states == {"8627": "pending", "8628": "pending", "8629": "scored"}
    assert {m.bill_number for m in db.scalars(select(Match))} == {"8629"}

    # Not retried on the next run (no request at all), still pending.
    again = SmallBatchScorer(max_documents=10)
    score_pending_bills(db, again, None)
    assert again.calls == []
    assert db.get(Bill, "8627").score_state == "pending"

    # A forced rescore skips them too.
    forced = SmallBatchScorer(max_documents=10)
    rescore_profile(db, forced, profile.id, force=True)
    assert forced.calls == [(RULE_A, 1)]

    # When a title arrives (ingestion marks the bill changed), it is scored.
    bill = db.get(Bill, "8627")
    bill.title = "Projet de loi sur le blanchiment de capitaux"
    bill.last_changed_at = datetime.now(UTC) + timedelta(seconds=1)
    db.flush()
    later = SmallBatchScorer(max_documents=10)
    score_pending_bills(db, later, None)
    assert later.calls == [(RULE_A, 1)]
    assert db.get(Bill, "8627").score_state == "scored"
    assert {m.bill_number for m in db.scalars(select(Match))} == {"8627", "8629"}


def test_scorer_never_receives_empty_text(db, owner):
    """Even a pair that slipped through (e.g. a stale scored list) is filtered out."""
    add_bill(db, "8700", "")
    add_profile(db, owner, [RULE_A])

    class Strict(FakeScorer):
        def score_many(self, rule_text, bill_texts):
            if any(not t.strip() for t in bill_texts):
                raise ScoringError("empty document")
            return super().score_many(rule_text, bill_texts)

    result = score_pending_bills(db, Strict(), None)
    assert result.error is None


# -- preview: most recent by bill number -------------------------------------------------------


def test_preview_takes_highest_numbers_and_skips_bills_without_text(db):
    from datetime import date

    add_bill(db, "7000", "Projet de loi blanchiment ancien", filing_date=date(2025, 1, 1))
    add_bill(db, "8600", "Projet de loi blanchiment récent")  # vote-only: no filing date
    add_bill(db, "8601", "")  # no text: not in the preview
    add_bill(db, "8599A", "Projet de loi blanchiment A")
    add_bill(db, "8599B", "Projet de loi blanchiment B")
    rows = preview_matches(db, FakeScorer(), [RULE_A], 0.0, limit=3)
    assert {r.bill_number for r in rows} == {"8600", "8599B", "8599A"}
