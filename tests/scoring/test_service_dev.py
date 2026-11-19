"""Scoring service against the test DB with FakeScorer (keyword overlap) and failing stubs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from lexwatch.models import Bill, Match, Profile, Rule, RuleScore, Run, User
from lexwatch.scoring.base import ScorerUnavailable, ScoringError
from lexwatch.scoring.fake import FakeScorer
from lexwatch.scoring.service import (
    clear_preview_cache,
    compute_matches,
    preview_matches,
    recompute_profile_matches,
    rescore_profile,
    score_pending_bills,
    stale_score_count,
)
from lexwatch.scoring.text import bill_text
from tests.helpers import make_test_settings

AML = "blanchiment capitaux"
TERROR = "financement terrorisme"


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_preview_cache()
    yield
    clear_preview_cache()


@pytest.fixture
def owner(db) -> User:
    user = User(oidc_sub="scoring-owner", email="owner@example.test", role="user")
    db.add(user)
    db.flush()
    return user


def add_bill(db, number: str, title: str, *, filed: date | None = None, state="pending", **kw):
    bill = Bill(
        number=number,
        title=title,
        source="test",
        score_state=state,
        filing_date=filed,
        **kw,
    )
    db.add(bill)
    db.flush()
    return bill


def add_profile(db, owner, rules: list[str], threshold: float | None = None) -> Profile:
    profile = Profile(
        name="AML",
        owner_id=owner.id,
        threshold=None if threshold is None else Decimal(str(threshold)),
    )
    profile.rules = [Rule(text=t, position=i) for i, t in enumerate(rules)]
    db.add(profile)
    db.flush()
    return profile


@pytest.fixture
def bills(db):
    return [
        add_bill(
            db,
            "8407",
            "Projet de loi relative à la lutte contre le blanchiment de capitaux "
            "et le financement du terrorisme",
            filed=date(2024, 6, 1),
            author="Ministre de la Justice",
        ),
        add_bill(
            db,
            "8408",
            "Projet de loi relatif au blanchiment de capitaux",
            filed=date(2024, 6, 2),
        ),
        add_bill(
            db,
            "8410",
            "Projet de loi portant création d'un Observatoire de la culture",
            filed=date(2024, 6, 3),
        ),
    ]


def scores_of(db, profile: Profile) -> dict[tuple[int, str], RuleScore]:
    rows = db.scalars(select(RuleScore).where(RuleScore.rule_id.in_([r.id for r in profile.rules])))
    return {(r.rule_id, r.bill_number): r for r in rows}


def matches_of(db, profile: Profile) -> dict[str, Match]:
    rows = db.scalars(select(Match).where(Match.profile_id == profile.id))
    return {m.bill_number: m for m in rows}


class StubScorer(FakeScorer):
    """FakeScorer that can fail for given rule texts or report billing."""

    def __init__(self, *, fail_on=(), unavailable_on=(), model="fake", unit=None, **kw):
        super().__init__(**kw)
        self.model_name = model
        self.fail_on = set(fail_on)
        self.unavailable_on = set(unavailable_on)
        self.unit = unit

    def score_many(self, rule_text, bill_texts):
        if rule_text in self.unavailable_on:
            self.calls.append((rule_text, len(bill_texts)))
            raise ScorerUnavailable("circuit open")
        if rule_text in self.fail_on:
            self.calls.append((rule_text, len(bill_texts)))
            raise ScoringError("HTTP 500: boom")
        out = super().score_many(rule_text, bill_texts)
        if self.unit:
            self.billing_unit = self.unit
        return out


# -- score_pending_bills ----------------------------------------------------------------------


def test_scores_pending_bills_average_and_threshold(db, owner, bills):
    profile = add_profile(db, owner, [AML, TERROR], threshold=0.6)
    scorer = FakeScorer()
    result = score_pending_bills(db, scorer, None)

    assert (result.scored, result.unscored, result.error) == (3, 0, None)
    assert scorer.calls == [(AML, 3), (TERROR, 3)]  # one request per rule
    scores = scores_of(db, profile)
    assert len(scores) == 6
    assert {s.model for s in scores.values()} == {"fake"}
    assert all(s.scored_at is not None for s in scores.values())

    matches = matches_of(db, profile)
    assert matches["8407"].profile_score == pytest.approx(1.0)
    assert matches["8408"].profile_score == pytest.approx(0.5)  # (1.0 + 0.0) / 2
    assert matches["8410"].profile_score == pytest.approx(0.0)
    assert [n for n, m in sorted(matches.items()) if m.matched] == ["8407"]

    for bill in bills:
        db.refresh(bill)
        assert bill.score_state == "scored"
        assert bill.scored_text == bill_text(bill)
    assert "Auteur : Ministre de la Justice" in bills[0].scored_text

    # nothing left to do on the next run
    again = FakeScorer()
    assert score_pending_bills(db, again, None).scored == 0
    assert again.calls == []


def test_threshold_is_inclusive_and_default_comes_from_scorer(db, owner, bills):
    profile = add_profile(db, owner, [AML, TERROR])
    score_pending_bills(db, FakeScorer(default_threshold=0.5), None)
    matches = matches_of(db, profile)
    assert matches["8408"].matched is True  # 0.5 >= 0.5
    assert matches["8410"].matched is False


def test_threshold_change_recomputes_without_scoring(db, owner, bills):
    profile = add_profile(db, owner, [AML, TERROR], threshold=0.6)
    score_pending_bills(db, FakeScorer(), None)
    assert matches_of(db, profile)["8408"].matched is False

    profile.threshold = Decimal("0.4")
    db.flush()
    n = recompute_profile_matches(db, profile.id, make_test_settings())
    assert n == 2
    assert matches_of(db, profile)["8408"].matched is True


def test_dedupes_identical_rule_texts_and_batches(db, owner, bills):
    p1 = add_profile(db, owner, [AML])
    p2 = add_profile(db, owner, [AML])
    scorer = FakeScorer(max_documents=2)
    score_pending_bills(db, scorer, None)
    assert scorer.calls == [(AML, 2), (AML, 1)]  # one rule text, batched by max_documents
    assert len(scores_of(db, p1)) == 3 and len(scores_of(db, p2)) == 3
    assert matches_of(db, p2)["8408"].profile_score == pytest.approx(1.0)


def test_failed_rule_marks_bills_unscored_and_retries_next_run(db, owner, bills):
    profile = add_profile(db, owner, [AML, TERROR])
    result = score_pending_bills(db, StubScorer(fail_on={TERROR}), None)
    assert (result.scored, result.unscored) == (0, 3)
    assert "boom" in (result.error or "")
    for bill in bills:
        db.refresh(bill)
        assert bill.score_state == "unscored"
        assert bill.title  # data kept
    assert matches_of(db, profile) == {}  # no complete set of rule scores yet

    retry = FakeScorer()
    result = score_pending_bills(db, retry, None)
    assert (result.scored, result.unscored, result.error) == (3, 0, None)
    # Only the failed rule is sent again: the AML pairs already have scores from after the
    # bills' last change.
    assert retry.calls == [(TERROR, 3)]
    assert len(matches_of(db, profile)) == 3


def test_scorer_unavailable_stops_calling(db, owner, bills):
    add_profile(db, owner, [AML, TERROR, "culture"])
    scorer = StubScorer(unavailable_on={AML})
    result = score_pending_bills(db, scorer, None)
    assert scorer.calls == [(AML, 3)]  # no further calls once the circuit is open
    assert (result.scored, result.unscored) == (0, 3)
    assert result.error.startswith("scorer unavailable")
    states = set(db.scalars(select(Bill.score_state)))
    assert states == {"unscored"}


def test_no_rules_marks_pending_scored_without_calls(db, bills):
    scorer = FakeScorer()
    result = score_pending_bills(db, scorer, None)
    assert result.scored == 3 and scorer.calls == []
    db.refresh(bills[0])
    assert bills[0].score_state == "scored" and bills[0].scored_text


def test_model_switch_rescores_stale_scores(db, owner, bills):
    profile = add_profile(db, owner, [AML])
    score_pending_bills(db, StubScorer(model="old-model"), None)
    assert stale_score_count(db, "fake") == 3
    assert stale_score_count(db, "old-model") == 0

    scorer = FakeScorer()  # model "fake"; all bills are already 'scored'
    result = score_pending_bills(db, scorer, None)
    assert scorer.calls == [(AML, 3)]
    assert result.scored == 3
    assert stale_score_count(db, "fake") == 0
    assert {s.model for s in scores_of(db, profile).values()} == {"fake"}


def test_billing_recorded_on_run(db, owner, bills):
    add_profile(db, owner, [AML])
    run = Run(kind="manual", status="running", details={"sources": {}})
    db.add(run)
    db.flush()
    scorer = StubScorer(unit="input_tokens")
    result = score_pending_bills(db, scorer, run)
    assert result.billed_units == 1 and result.billing_unit == "input_tokens"
    assert run.details == {
        "sources": {},
        "billing": {"unit": "input_tokens"},
        "progress": {"done": 3, "total": 3},
    }


# -- compute_matches --------------------------------------------------------------------------


def test_compute_matches_skips_bills_missing_a_rule_score(db, owner, bills):
    profile = add_profile(db, owner, [AML])
    score_pending_bills(db, FakeScorer(), None)
    old = matches_of(db, profile)["8407"].profile_score
    profile.rules.append(Rule(text=TERROR, position=1))
    db.flush()
    assert compute_matches(db, profile, 0.5) == 2  # previous rows kept, nothing recomputed
    assert matches_of(db, profile)["8407"].profile_score == old


def test_compute_matches_without_rules_removes_matches(db, owner, bills):
    profile = add_profile(db, owner, [AML])
    score_pending_bills(db, FakeScorer(), None)
    assert matches_of(db, profile)
    profile.rules.clear()
    db.flush()
    assert compute_matches(db, profile, 0.5) == 0
    assert matches_of(db, profile) == {}


def test_compute_matches_ignores_other_models_when_model_given(db, owner, bills):
    profile = add_profile(db, owner, [AML])
    score_pending_bills(db, StubScorer(model="old-model"), None)
    db.execute(Match.__table__.delete())
    assert compute_matches(db, profile, 0.5, model="fake") == 0
    assert matches_of(db, profile) == {}
    assert compute_matches(db, profile, 0.5, model="old-model") == 2


# -- rescore_profile --------------------------------------------------------------------------


def test_rescore_profile_scores_only_missing_pairs(db, owner, bills):
    profile = add_profile(db, owner, [AML])
    score_pending_bills(db, FakeScorer(), None)
    profile.rules.append(Rule(text=TERROR, position=1))
    db.flush()

    scorer = FakeScorer()
    result = rescore_profile(db, scorer, profile.id)
    assert scorer.calls == [(TERROR, 3)]
    assert (result.scored, result.unscored, result.error) == (3, 0, None)
    assert matches_of(db, profile)["8408"].profile_score == pytest.approx(0.5)

    forced = FakeScorer()
    rescore_profile(db, forced, profile.id, force=True)
    assert forced.calls == [(AML, 3), (TERROR, 3)]


def test_rescore_profile_failure_marks_unscored(db, owner, bills):
    profile = add_profile(db, owner, [AML])
    score_pending_bills(db, FakeScorer(), None)
    profile.rules.append(Rule(text=TERROR, position=1))
    db.flush()
    result = rescore_profile(db, StubScorer(fail_on={TERROR}), profile.id)
    assert result.unscored == 3 and result.error
    assert set(db.scalars(select(Bill.score_state))) == {"unscored"}


def test_rescore_unknown_profile(db):
    assert rescore_profile(db, FakeScorer(), 999_999).error == "profile 999999 not found"


# -- preview ----------------------------------------------------------------------------------


def test_preview_sorts_limits_and_writes_nothing(db, owner, bills):
    scorer = FakeScorer()
    before = db.scalar(select(func.count()).select_from(RuleScore))
    rows = preview_matches(db, scorer, [AML, "  ", TERROR], threshold=0.6, limit=2)
    # the 2 most recent bills by filing date: 8410 and 8408
    assert [r.number for r in rows] == ["8408", "8410"]
    assert rows[0].score == pytest.approx(0.5) and rows[0].rule_scores == [1.0, 0.0]
    assert [r.matched for r in rows] == [False, False]
    assert rows[0].title.startswith("Projet de loi relatif")
    assert scorer.calls == [(AML, 2), (TERROR, 2)]
    assert db.scalar(select(func.count()).select_from(RuleScore)) == before
    assert db.scalar(select(func.count()).select_from(Match)) == 0


def test_preview_uses_cache_and_stored_scores(db, owner, bills):
    scorer = FakeScorer()
    preview_matches(db, scorer, [AML], threshold=0.5)
    preview_matches(db, scorer, [AML], threshold=0.5)
    assert scorer.calls == [(AML, 3)]  # second call served from the LRU cache

    clear_preview_cache()
    add_profile(db, owner, [TERROR])
    score_pending_bills(db, FakeScorer(), None)
    fresh = FakeScorer()
    rows = preview_matches(db, fresh, [TERROR, AML], threshold=0.5)
    assert fresh.calls == [(AML, 3)]  # TERROR reused from stored rule scores
    assert rows[0].number == "8407" and rows[0].matched


def test_preview_without_rules_or_bills(db, bills):
    assert preview_matches(db, FakeScorer(), ["", " "], threshold=0.5) == []


def test_preview_propagates_scoring_errors(db, bills):
    with pytest.raises(ScoringError):
        preview_matches(db, StubScorer(fail_on={AML}), [AML], threshold=0.5)


def test_computed_at_only_moves_on_new_row_or_matched_flip(db, owner, bills):
    from datetime import UTC, datetime

    profile = add_profile(db, owner, [AML, TERROR], threshold=0.6)
    score_pending_bills(db, FakeScorer(), None)
    old = datetime(2020, 1, 1, tzinfo=UTC)
    db.execute(Match.__table__.update().values(computed_at=old))

    def stamps():
        db.expire_all()
        return {
            n: (m.computed_at, m.matched, m.profile_score)
            for n, m in matches_of(db, profile).items()
        }

    # rescore with identical scores, then a score-only change: no computed_at bump
    rescore_profile(db, FakeScorer(), profile.id, force=True)
    db.execute(
        RuleScore.__table__.update()
        .where(RuleScore.bill_number == "8407", RuleScore.rule_id == profile.rules[1].id)
        .values(score=0.9)
    )
    compute_matches(db, profile, 0.5)
    after = stamps()
    assert after["8407"][2] == pytest.approx(0.95)  # score updated
    assert all(ts == old for ts, _, _ in after.values())

    # threshold flip: 8408 (0.5) becomes matched -> only that row bumps
    profile.threshold = Decimal("0.4")
    db.flush()
    compute_matches(db, profile, 0.5)
    after = stamps()
    assert after["8408"][1] is True and after["8408"][0] > old
    assert after["8407"][0] == old and after["8410"][0] == old
