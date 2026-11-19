"""QA tests for the scoring service (build-prompt section 5), against the test DB.

The Foundry scorer runs over httpx.MockTransport (no network); the fake scorer is used where
the HTTP layer does not matter.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from lexwatch.models import Bill, Match, Profile, Rule, RuleScore, Run, User
from lexwatch.scoring.fake import FakeScorer
from lexwatch.scoring.foundry import FoundryScorer
from lexwatch.scoring.service import (
    clear_preview_cache,
    compute_matches,
    invalidate_rule_scores,
    is_match,
    preview_matches,
    profile_score,
    recompute_profile_matches,
    rescore_profile,
    score_pending_bills,
    stale_score_count,
)
from tests.helpers import make_test_settings

ENDPOINT = "https://qa-res.services.ai.azure.com/providers/cohere/v2/rerank"
OLD = datetime(2020, 1, 1, tzinfo=UTC)
NUM = re.compile(r"n°(\d+)")

R_AML = "Lutte contre le blanchiment de capitaux"
R_TER = "Financement du terrorisme"
R_CULT = "Politique culturelle"


@pytest.fixture(autouse=True)
def _clean_preview_cache():
    clear_preview_cache()
    yield
    clear_preview_cache()


# -- fake Foundry -----------------------------------------------------------------------------


class Foundry:
    """MockTransport handler: score of (query, bill number) from ``table``, else ``default``.

    ``down`` makes every request answer 503. ``tokens`` is billed per document.
    """

    def __init__(self, table: dict[tuple[str, str], float] | None = None, default=0.0, tokens=10):
        self.table = table or {}
        self.default = default
        self.tokens = tokens
        self.down = False
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            return httpx.Response(503, json={"message": "Service unavailable"})
        body = json.loads(request.content)
        results = []
        for i, doc in enumerate(body["documents"]):
            m = NUM.search(doc)
            score = self.table.get((body["query"], m.group(1) if m else ""), self.default)
            results.append({"index": i, "relevance_score": score})
        results.sort(key=lambda r: -r["relevance_score"])
        meta = {"billed_units": {"input_tokens": self.tokens * len(body["documents"])}}
        return httpx.Response(200, json={"id": "qa", "results": results, "meta": meta})

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    def queries(self) -> list[str]:
        return [b["query"] for b in self.bodies()]


def foundry(up: Foundry, model="Cohere-rerank-v5-pro", **overrides) -> FoundryScorer:
    options = {
        "scorer": "foundry",
        "foundry_endpoint": ENDPOINT,
        "foundry_model": model,
        "foundry_api_key": "qa-key",
        "foundry_max_retries": 1,
        "foundry_circuit_threshold": 3,
    }
    s = make_test_settings(**{**options, **overrides})
    return FoundryScorer(
        s, client=httpx.Client(transport=httpx.MockTransport(up)), sleep=lambda x: None
    )


# -- data helpers -----------------------------------------------------------------------------


@pytest.fixture
def owner(db) -> User:
    user = User(
        oidc_sub="qa-owner-sub",
        email="marie.secret@client-bank.example",
        name="Marie Confidentielle",
        role="user",
    )
    db.add(user)
    db.flush()
    return user


def bill(db, number: str, title: str = "", filed: date | None = None, state="pending", **kw):
    b = Bill(
        number=number,
        title=f"Projet de loi n°{number} {title}".strip(),
        source="test",
        score_state=state,
        filing_date=filed or date(2024, 1, int(number) % 28 + 1),
        **kw,
    )
    db.add(b)
    db.flush()
    return b


def profile(db, owner, rules: list[str], threshold=None, name="QA profile", recipients=""):
    p = Profile(
        name=name,
        owner_id=owner.id,
        recipients=recipients,
        threshold=None if threshold is None else Decimal(str(threshold)),
    )
    p.rules = [Rule(text=t, position=i) for i, t in enumerate(rules)]
    db.add(p)
    db.flush()
    return p


def put_scores(db, rule: Rule, scores: dict[str, float], model="qa-model"):
    for number, s in scores.items():
        db.add(RuleScore(rule_id=rule.id, bill_number=number, score=s, model=model))
    db.flush()


def matches(db, p: Profile) -> dict[str, Match]:
    db.expire_all()
    return {m.bill_number: m for m in db.scalars(select(Match).where(Match.profile_id == p.id))}


def rule_scores(db, p: Profile) -> dict[tuple[int, str], RuleScore]:
    db.expire_all()
    ids = [r.id for r in p.rules]
    return {
        (r.rule_id, r.bill_number): r
        for r in db.scalars(select(RuleScore).where(RuleScore.rule_id.in_(ids)))
    }


def count(db, model) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


# -- profile score: plain mean ----------------------------------------------------------------


def test_profile_score_plain_mean_pure():
    assert profile_score([0.42]) == pytest.approx(0.42)
    assert profile_score([0.1, 0.9]) == pytest.approx(0.5)
    assert profile_score([0.0, 0.0, 0.0, 1.0]) == pytest.approx(0.25)  # not max, not weighted
    assert profile_score([0.2, 0.4, 0.6, 0.8, 1.0, 0.0, 0.3]) == pytest.approx(3.3 / 7)
    assert profile_score([0.7, 0.7, 0.7]) == pytest.approx(0.7)
    assert profile_score([]) is None


@pytest.mark.parametrize(
    ("scores", "threshold", "expected"),
    [
        ([0.5], 0.5, True),
        ([0.4999], 0.5, False),
        ([0.6, 0.4], 0.5, True),  # mean exactly at threshold
        ([0.6, 0.3998], 0.5, False),  # just below
        ([0.0], 0.0, True),
        ([1.0], 1.0, True),
    ],
)
def test_is_match_inclusive(scores, threshold, expected):
    assert is_match(profile_score(scores), threshold) is expected


def test_is_match_none_never_matches():
    assert is_match(None, 0.0) is False


def test_identical_scores_at_threshold_match():
    assert is_match(profile_score([0.173, 0.173, 0.173]), 0.173)


def test_compute_matches_mean_one_rule_many_rules_identical(db, owner):
    for n in ("101", "102", "103"):
        bill(db, n, state="scored")
    one = profile(db, owner, [R_AML], name="one")
    put_scores(db, one.rules[0], {"101": 0.37, "102": 0.0, "103": 1.0})
    many = profile(db, owner, [f"rule {i}" for i in range(5)], name="many")
    for rule, vals in zip(
        many.rules,
        [(0.1, 0.5, 0.5), (0.2, 0.5, 0.5), (0.3, 0.5, 0.5), (0.4, 0.5, 0.5), (1.0, 0.5, 0.5)],
        strict=True,
    ):
        put_scores(db, rule, dict(zip(("101", "102", "103"), vals, strict=True)))

    compute_matches(db, one, 0.5)
    compute_matches(db, many, 0.5)
    m1, m5 = matches(db, one), matches(db, many)
    assert m1["101"].profile_score == pytest.approx(0.37) and not m1["101"].matched
    assert m1["103"].matched and not m1["102"].matched
    assert m5["101"].profile_score == pytest.approx(0.4)  # (0.1+0.2+0.3+0.4+1.0)/5
    assert not m5["101"].matched
    assert m5["102"].profile_score == pytest.approx(0.5) and m5["102"].matched  # identical 0.5


@pytest.mark.parametrize(("second", "matched"), [(0.4, True), (0.398, False)])
def test_compute_matches_exactly_at_and_just_below_threshold(db, owner, second, matched):
    bill(db, "201", state="scored")
    p = profile(db, owner, [R_AML, R_TER], threshold=0.5)
    put_scores(db, p.rules[0], {"201": 0.6})
    put_scores(db, p.rules[1], {"201": second})
    compute_matches(db, p, 0.9)  # default is ignored: the profile has its own threshold
    assert matches(db, p)["201"].matched is matched


def test_threshold_override_vs_default_none(db, owner):
    bill(db, "301", state="scored")
    with_default = profile(db, owner, [R_AML], threshold=None, name="default")
    zero = profile(db, owner, [R_AML], threshold=0, name="zero")
    high = profile(db, owner, [R_AML], threshold=0.9, name="high")
    for p in (with_default, zero, high):
        put_scores(db, p.rules[0], {"301": 0.46})
    compute_matches(db, with_default, 0.45)
    compute_matches(db, zero, 0.45)
    compute_matches(db, high, 0.45)
    assert matches(db, with_default)["301"].matched is True  # 0.46 >= default 0.45
    assert matches(db, zero)["301"].matched is True  # explicit 0 is not "no threshold"
    assert matches(db, high)["301"].matched is False
    compute_matches(db, with_default, 0.5)
    assert matches(db, with_default)["301"].matched is False


@pytest.mark.parametrize(
    ("model", "matched"), [("Cohere-rerank-v5-fast", True), ("Cohere-rerank-v5-pro", False)]
)
def test_per_scorer_default_threshold_used_by_daily_scoring(db, owner, model, matched):
    bill(db, "401")
    p = profile(db, owner, [R_AML])  # no own threshold
    up = Foundry({(R_AML, "401"): 0.47})
    score_pending_bills(db, foundry(up, model=model), None)
    m = matches(db, p)["401"]
    assert m.profile_score == pytest.approx(0.47)
    assert m.matched is matched  # fast default 0.45, pro default 0.5


# -- daily scoring with Foundry ---------------------------------------------------------------


def test_one_request_per_rule_with_bills_as_documents_and_model_stored(db, owner):
    for n in ("501", "502", "503"):
        bill(db, n)
    p = profile(db, owner, [R_AML, R_TER])
    up = Foundry({(R_AML, "501"): 0.9, (R_TER, "501"): 0.7})
    scorer = foundry(up)
    result = score_pending_bills(db, scorer, None)
    assert result.scored == 3 and result.unscored == 0 and result.error is None
    assert sorted(up.queries()) == sorted([R_AML, R_TER])
    for body in up.bodies():
        assert body["model"] == "Cohere-rerank-v5-pro"
        assert len(body["documents"]) == 3
    scores = rule_scores(db, p)
    assert len(scores) == 6
    assert {s.model for s in scores.values()} == {"Cohere-rerank-v5-pro"}
    assert matches(db, p)["501"].profile_score == pytest.approx(0.8)


def test_request_bodies_contain_no_user_data(db, owner):
    bill(db, "601")
    profile(
        db,
        owner,
        [R_AML],
        name="Profil confidentiel Marie",
        recipients="alice.private@client-bank.example, bob@client-bank.example",
    )
    up = Foundry()
    score_pending_bills(db, foundry(up), None)
    assert up.requests
    for req in up.requests:
        raw = req.content.decode("utf-8") + str(req.url) + json.dumps(dict(req.headers))
        for secret in (
            owner.email,
            owner.name,
            "qa-owner-sub",
            "alice.private",
            "client-bank",
            "Profil confidentiel",
        ):
            assert secret not in raw
        assert "@" not in raw
        assert set(json.loads(req.content)) <= {"model", "query", "documents", "max_tokens_per_doc"}


def test_batches_bills_by_max_documents_in_service(db, owner):
    for i in range(5):
        bill(db, str(700 + i))
    profile(db, owner, [R_AML])
    up = Foundry(tokens=3)
    scorer = foundry(up, foundry_max_documents=2)
    result = score_pending_bills(db, scorer, None)
    assert sorted(len(b["documents"]) for b in up.bodies()) == [1, 2, 2]
    assert result.billed_units == 15  # 5 documents x 3 tokens, across 3 batches


def test_billing_recorded_on_run_details(db, owner):
    bill(db, "801")
    profile(db, owner, [R_AML, R_TER])
    run = Run(kind="daily", status="running", details={})
    db.add(run)
    db.flush()
    result = score_pending_bills(db, foundry(Foundry(tokens=11)), run)
    assert result.billed_units == 22 and result.billing_unit == "input_tokens"
    assert run.details["billing"] == {"unit": "input_tokens"}


def test_endpoint_down_marks_unscored_keeps_data_then_recovers(db, owner):
    bills = [bill(db, n, "relatif aux banques", summary="Résumé conservé") for n in ("901", "902")]
    p = profile(db, owner, [R_AML, R_TER])
    up = Foundry({(R_AML, "901"): 0.8, (R_TER, "901"): 0.6})
    up.down = True
    result = score_pending_bills(db, foundry(up), None)
    assert (result.scored, result.unscored) == (0, 2)
    assert result.error
    db.expire_all()
    for b in bills:
        assert b.score_state == "unscored"
        assert b.title.startswith("Projet de loi") and b.summary == "Résumé conservé"
    assert count(db, Bill) == 2
    assert rule_scores(db, p) == {}

    up.down = False
    up.requests.clear()
    result = score_pending_bills(db, foundry(up), None)  # next run, fresh scorer
    assert (result.scored, result.unscored, result.error) == (2, 0, None)
    db.expire_all()
    assert {b.score_state for b in bills} == {"scored"}
    assert matches(db, p)["901"].profile_score == pytest.approx(0.7)
    assert matches(db, p)["901"].matched


def test_circuit_open_mid_run_stops_requests_and_keeps_existing_matches(db, owner):
    b = bill(db, "1001", state="scored")
    p = profile(db, owner, [R_AML, R_TER, R_CULT])
    up = Foundry({(R_AML, "1001"): 0.9, (R_TER, "1001"): 0.9, (R_CULT, "1001"): 0.9})
    rescore_profile(db, foundry(up), p.id)
    assert matches(db, p)["1001"].matched

    b.score_state = "pending"  # the bill changed upstream (ingestion also bumps the time)
    b.last_changed_at = datetime.now(UTC)
    db.flush()
    up.down = True
    up.requests.clear()
    scorer = foundry(up, foundry_circuit_threshold=1, foundry_max_retries=0)
    result = score_pending_bills(db, scorer, None)
    assert len(up.requests) == 1  # breaker open after the first failed request
    assert result.unscored == 1 and "unavailable" in (result.error or "")
    db.expire_all()
    assert b.score_state == "unscored"
    assert matches(db, p)["1001"].matched  # nothing dropped


def test_model_change_rescores_everything_and_stores_model(db, owner):
    for n in ("1101", "1102"):
        bill(db, n)
    p = profile(db, owner, [R_AML, R_TER])
    old = Foundry({(R_AML, "1101"): 0.2, (R_TER, "1101"): 0.2})
    score_pending_bills(db, foundry(old, model="Cohere-rerank-v4.0-pro"), None)
    assert matches(db, p)["1101"].matched is False
    assert stale_score_count(db, "Cohere-rerank-v5-pro") == 4

    new = Foundry({(R_AML, "1101"): 0.9, (R_TER, "1101"): 0.8})
    result = score_pending_bills(db, foundry(new, model="Cohere-rerank-v5-pro"), None)
    assert len(new.requests) == 2  # one per rule, both bills in each
    assert all(len(b["documents"]) == 2 for b in new.bodies())
    assert all(b["model"] == "Cohere-rerank-v5-pro" for b in new.bodies())
    assert result.scored == 2
    assert stale_score_count(db, "Cohere-rerank-v5-pro") == 0
    assert {s.model for s in rule_scores(db, p).values()} == {"Cohere-rerank-v5-pro"}
    m = matches(db, p)["1101"]
    assert m.profile_score == pytest.approx(0.85) and m.matched


def test_model_change_via_rescore_profile(db, owner):
    bill(db, "1151")
    p = profile(db, owner, [R_AML])
    score_pending_bills(db, foundry(Foundry(default=0.1), model="old-deploy"), None)
    up = Foundry(default=0.6)
    rescore_profile(db, foundry(up, model="new-deploy"), p.id)
    assert len(up.requests) == 1
    assert {s.model for s in rule_scores(db, p).values()} == {"new-deploy"}
    assert matches(db, p)["1151"].profile_score == pytest.approx(0.6)


# -- rescore_profile --------------------------------------------------------------------------


def test_rescore_profile_only_touches_that_profile(db, owner):
    for n in ("1201", "1202"):
        bill(db, n)
    p1 = profile(db, owner, [R_AML, R_CULT], name="p1")
    p2 = profile(db, owner, [R_AML, R_TER], name="p2")  # shares a rule text with p1
    up = Foundry(default=0.3)
    score_pending_bills(db, foundry(up), None)
    db.execute(RuleScore.__table__.update().values(scored_at=OLD))
    db.execute(Match.__table__.update().values(computed_at=OLD))
    p2_scores_before = {k: v.score for k, v in rule_scores(db, p2).items()}
    p2_matches_before = {k: (v.profile_score, v.matched) for k, v in matches(db, p2).items()}

    up2 = Foundry(default=0.9)
    rescore_profile(db, foundry(up2), p1.id, force=True)
    assert sorted(up2.queries()) == sorted([R_AML, R_CULT])
    assert all(s.scored_at > OLD for s in rule_scores(db, p1).values())
    assert all(s.score == pytest.approx(0.9) for s in rule_scores(db, p1).values())
    after = rule_scores(db, p2)
    assert {k: v.score for k, v in after.items()} == p2_scores_before
    assert all(s.scored_at == OLD for s in after.values())
    assert {
        k: (v.profile_score, v.matched) for k, v in matches(db, p2).items()
    } == p2_matches_before


def test_rescore_profile_scores_a_new_rule_for_all_bills(db, owner):
    for n in ("1301", "1302", "1303"):
        bill(db, n)
    p = profile(db, owner, [R_AML])
    score_pending_bills(db, foundry(Foundry(default=0.4)), None)
    p.rules.append(Rule(text=R_TER, position=1))
    db.flush()
    up = Foundry(default=0.8)
    result = rescore_profile(db, foundry(up), p.id)
    assert up.queries() == [R_TER]
    assert len(up.bodies()[0]["documents"]) == 3
    assert result.scored == 3
    assert all(m.profile_score == pytest.approx(0.6) for m in matches(db, p).values())


def test_rule_text_change_in_place_is_rescored(db, owner):
    bill(db, "1401")
    p = profile(db, owner, [R_AML])
    score_pending_bills(db, foundry(Foundry({(R_AML, "1401"): 0.1})), None)
    assert matches(db, p)["1401"].matched is False

    p.rules[0].text = R_CULT
    p.rescore_status = "pending"
    db.flush()
    assert invalidate_rule_scores(db, p.rules[0].id) == 1  # what the web stream does on edit
    up = Foundry({(R_CULT, "1401"): 0.95})
    rescore_profile(db, foundry(up), p.id)  # what jobs.worker.tick does
    assert up.queries() == [R_CULT]
    assert matches(db, p)["1401"].matched is True


def test_rule_replaced_by_new_row_is_rescored(db, owner):
    bill(db, "1451")
    p = profile(db, owner, [R_AML])
    score_pending_bills(db, foundry(Foundry({(R_AML, "1451"): 0.1})), None)
    p.rules.clear()
    db.flush()
    p.rules.append(Rule(text=R_CULT, position=0))
    db.flush()
    up = Foundry({(R_CULT, "1451"): 0.95})
    rescore_profile(db, foundry(up), p.id)
    assert up.queries() == [R_CULT]
    m = matches(db, p)["1451"]
    assert m.profile_score == pytest.approx(0.95) and m.matched


def test_threshold_only_change_makes_no_request(db, owner):
    for n in ("1501", "1502"):
        bill(db, n)
    p = profile(db, owner, [R_AML], threshold=0.9)
    score_pending_bills(db, foundry(Foundry({(R_AML, "1501"): 0.7})), None)
    assert not matches(db, p)["1501"].matched

    p.threshold = Decimal("0.7")
    db.flush()
    up = Foundry()
    result = rescore_profile(db, foundry(up), p.id)  # worker path
    assert up.requests == []
    assert result.billed_units == 0
    assert matches(db, p)["1501"].matched is True

    p.threshold = Decimal("0.75")
    db.flush()
    settings = make_test_settings(scorer="foundry", foundry_model="Cohere-rerank-v5-pro")
    assert recompute_profile_matches(db, p.id, settings) == 0
    assert matches(db, p)["1501"].matched is False


# -- matches.computed_at ----------------------------------------------------------------------


def test_computed_at_changes_only_when_matched_flips(db, owner):
    b1, b2 = bill(db, "1601"), bill(db, "1602")
    p = profile(db, owner, [R_AML], threshold=0.5)
    table = {(R_AML, "1601"): 0.8, (R_AML, "1602"): 0.2}
    score_pending_bills(db, foundry(Foundry(table)), None)
    first = matches(db, p)
    assert first["1601"].matched and not first["1602"].matched
    assert first["1601"].computed_at is not None  # new row gets a timestamp
    db.execute(Match.__table__.update().values(computed_at=OLD))

    # both bills change; 1601 stays matched with another score, 1602 stays unmatched
    b1.score_state = b2.score_state = "pending"  # changed upstream
    b1.last_changed_at = b2.last_changed_at = datetime.now(UTC)
    db.flush()
    table.update({(R_AML, "1601"): 0.9, (R_AML, "1602"): 0.3})
    score_pending_bills(db, foundry(Foundry(table)), None)
    m = matches(db, p)
    assert m["1601"].profile_score == pytest.approx(0.9)
    assert m["1601"].computed_at == OLD and m["1602"].computed_at == OLD

    # 1601 flips to unmatched, 1602 flips to matched
    b1.score_state = b2.score_state = "pending"  # changed upstream
    b1.last_changed_at = b2.last_changed_at = datetime.now(UTC)
    db.flush()
    table.update({(R_AML, "1601"): 0.1, (R_AML, "1602"): 0.5})
    score_pending_bills(db, foundry(Foundry(table)), None)
    m = matches(db, p)
    assert not m["1601"].matched and m["1601"].computed_at > OLD
    assert m["1602"].matched and m["1602"].computed_at > OLD


def test_threshold_change_without_flip_keeps_computed_at(db, owner):
    bill(db, "1701")
    p = profile(db, owner, [R_AML], threshold=0.5)
    score_pending_bills(db, foundry(Foundry({(R_AML, "1701"): 0.8})), None)
    db.execute(Match.__table__.update().values(computed_at=OLD))
    p.threshold = Decimal("0.6")
    db.flush()
    compute_matches(db, p, 0.5)
    assert matches(db, p)["1701"].computed_at == OLD


# -- preview ----------------------------------------------------------------------------------


def _snapshot(db):
    db.expire_all()
    return (
        sorted((r.rule_id, r.bill_number, r.score, r.model) for r in db.scalars(select(RuleScore))),
        sorted(
            (m.profile_id, m.bill_number, m.profile_score, m.matched)
            for m in db.scalars(select(Match))
        ),
        sorted((b.number, b.score_state, b.scored_text) for b in db.scalars(select(Bill))),
    )


def test_preview_writes_nothing(db, owner):
    for n in ("1801", "1802", "1803"):
        bill(db, n)
    p = profile(db, owner, [R_AML])
    score_pending_bills(db, foundry(Foundry(default=0.2)), None)
    bill(db, "1804")  # a pending bill too
    before = _snapshot(db)
    up = Foundry(default=0.6)
    rows = preview_matches(db, foundry(up), [R_CULT, R_TER], threshold=0.5)
    assert len(rows) == 4 and all(r.matched for r in rows)
    assert not db.new and not db.dirty and not db.deleted
    assert _snapshot(db) == before
    assert len(matches(db, p)) == 3


def test_second_identical_preview_makes_no_request(db, owner):
    for n in ("1901", "1902"):
        bill(db, n)
    up = Foundry({(R_CULT, "1901"): 0.7})
    rows1 = preview_matches(db, foundry(up), [R_CULT], threshold=0.5)
    n = len(up.requests)
    assert n == 1
    rows2 = preview_matches(db, foundry(up), [R_CULT], threshold=0.5)  # new scorer instance
    assert len(up.requests) == n
    assert [(r.bill_number, r.profile_score) for r in rows2] == [
        (r.bill_number, r.profile_score) for r in rows1
    ]
    assert rows2[0].bill_number == "1901" and rows2[0].matched


def test_preview_cache_is_per_model(db, owner):
    bill(db, "1951")
    up = Foundry(default=0.4)
    preview_matches(db, foundry(up, model="deploy-a"), [R_CULT], threshold=0.5)
    preview_matches(db, foundry(up, model="deploy-b"), [R_CULT], threshold=0.5)
    assert len(up.requests) == 2


def test_preview_mean_and_default_threshold_from_scorer(db, owner):
    bill(db, "1961")
    bill(db, "1962")
    table = {
        (R_AML, "1961"): 0.7,
        (R_TER, "1961"): 0.3,
        (R_AML, "1962"): 0.5,
        (R_TER, "1962"): 0.38,
    }
    up = Foundry(table)
    rows = preview_matches(db, foundry(up, model="Cohere-rerank-v5-fast"), [R_AML, R_TER])
    by_number = {r.bill_number: r for r in rows}
    assert by_number["1961"].profile_score == pytest.approx(0.5)
    assert by_number["1961"].rule_scores == pytest.approx([0.7, 0.3])
    assert by_number["1961"].matched is True  # v5-fast default 0.45
    assert by_number["1962"].profile_score == pytest.approx(0.44)
    assert by_number["1962"].matched is False
    assert [r.bill_number for r in rows] == ["1961", "1962"]  # highest profile score first


FLOAT_BUG = (
    "BUG: the plain mean is compared with the threshold in raw floating point, so a mean that "
    "is exactly the threshold can miss it: (0.6 + 0.3) / 2 = 0.44999999999999996 < 0.45, the "
    "Rerank v5-fast default threshold"
)


def test_preview_mean_exactly_at_default_threshold_matches(db, owner):
    bill(db, "1963")
    up = Foundry({(R_AML, "1963"): 0.6, (R_TER, "1963"): 0.3})
    rows = preview_matches(db, foundry(up, model="Cohere-rerank-v5-fast"), [R_AML, R_TER])
    assert rows[0].matched is True


def test_stored_mean_exactly_at_threshold_matches(db, owner):
    bill(db, "1964")
    p = profile(db, owner, [R_AML, R_TER], threshold=0.45)
    up = Foundry({(R_AML, "1964"): 0.6, (R_TER, "1964"): 0.3})
    score_pending_bills(db, foundry(up), None)
    assert matches(db, p)["1964"].matched is True


def test_preview_request_bodies_have_no_user_data(db, owner):
    bill(db, "1971")
    profile(db, owner, [R_AML], name="Profil Marie", recipients="x@client-bank.example")
    up = Foundry()
    preview_matches(db, foundry(up), [R_CULT], threshold=0.5)
    raw = up.requests[0].content.decode()
    assert "@" not in raw and owner.name not in raw and "Profil Marie" not in raw


# -- fake scorer used through the same service --------------------------------------------


def test_fake_scorer_default_threshold_applies_when_profile_has_none(db, owner):
    bill(db, "2001", "blanchiment")
    p = profile(db, owner, ["blanchiment terrorisme culture"])  # 1 of 3 words -> 0.3333
    score_pending_bills(db, FakeScorer(), None)  # fake default 0.3
    m = matches(db, p)["2001"]
    assert m.profile_score == pytest.approx(1 / 3, abs=1e-3) and m.matched
