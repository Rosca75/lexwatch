"""QA tests for FoundryScorer, written against build-prompt section 5 and the Foundry research.

Independent of the developer tests. Every request goes through httpx.MockTransport: no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from lexwatch.scoring.base import ScorerUnavailable, ScoringError
from lexwatch.scoring.factory import default_threshold_for, get_scorer
from lexwatch.scoring.foundry import FoundryScorer
from tests.helpers import make_test_settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "foundry"
ENDPOINT = "https://qa-res.services.ai.azure.com/providers/cohere/v2/rerank"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def qa_settings(**overrides):
    base = {
        "scorer": "foundry",
        "foundry_endpoint": ENDPOINT,
        "foundry_model": "Cohere-rerank-v5-pro",
        "foundry_api_key": "qa-key",
        "foundry_max_retries": 2,
        "foundry_circuit_threshold": 3,
        "foundry_max_documents": 100,
    }
    base.update(overrides)
    return make_test_settings(**base)


@dataclass
class Upstream:
    """Fake Foundry: queued responses first, then ``default``; records every request."""

    queue: list = field(default_factory=list)
    default: object = None
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if self.queue else self.default
        if callable(item) and not isinstance(item, httpx.Response):
            item = item(request)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, httpx.Response), "unexpected request"
        return item

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def ranked(scores: list[float], usage: dict | None = None, reverse: bool = True) -> dict:
    """A rerank body for ``scores`` (input order), results sorted by score like the API."""
    results = [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]
    results.sort(key=lambda r: r["relevance_score"], reverse=reverse)
    body: dict = {"id": "qa", "results": results}
    if usage is not None:
        body["meta"] = {"api_version": {"version": "2"}, "billed_units": usage}
    return body


def by_doc(score_of, usage_of=None):
    """Handler scoring each document with ``score_of(doc)``; optional billing per request."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        scores = [score_of(d) for d in body["documents"]]
        usage = usage_of(body) if usage_of else None
        return httpx.Response(200, json=ranked(scores, usage))

    return handler


def scorer_for(up: Upstream, sleeps: list | None = None, **overrides) -> FoundryScorer:
    return FoundryScorer(
        qa_settings(**overrides),
        client=httpx.Client(transport=httpx.MockTransport(up)),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        rand=lambda: 0.5,
    )


# -- response mapping -------------------------------------------------------------------------


def test_recorded_fixture_out_of_order_is_mapped_by_index():
    up = Upstream([httpx.Response(200, json=fixture("rerank_response.json"))])
    scores = scorer_for(up).score_many("q", ["d0", "d1", "d2", "d3", "d4"])
    # fixture lists index 2, 0, 4, 3, 1
    assert scores == pytest.approx([0.6382451, 0.0062190853, 0.91734606, 0.034817535, 0.21907842])


def test_ascending_and_shuffled_results_are_mapped_by_index():
    payload = {
        "results": [
            {"index": 1, "relevance_score": 0.2},
            {"index": 3, "relevance_score": 0.4},
            {"index": 0, "relevance_score": 0.1},
            {"index": 2, "relevance_score": 0.3},
        ]
    }
    up = Upstream([httpx.Response(200, json=payload)])
    assert scorer_for(up).score_many("q", list("abcd")) == [0.1, 0.2, 0.3, 0.4]


def test_index_is_relative_to_each_batch():
    docs = [f"doc-{i}" for i in range(5)]
    up = Upstream(default=by_doc(lambda d: int(d.split("-")[1]) / 10))
    scores = scorer_for(up, foundry_max_documents=2).score_many("q", docs)
    assert scores == pytest.approx([0.0, 0.1, 0.2, 0.3, 0.4])


def test_missing_index_is_error():
    payload = {
        "results": [{"index": 0, "relevance_score": 0.5}, {"index": 2, "relevance_score": 1}]
    }
    up = Upstream([httpx.Response(200, json=payload)])
    with pytest.raises(ScoringError):
        scorer_for(up).score_many("q", ["a", "b", "c"])


def test_duplicate_index_replacing_a_missing_one_is_error():
    payload = {
        "results": [
            {"index": 0, "relevance_score": 0.5},
            {"index": 0, "relevance_score": 0.4},
        ]
    }
    up = Upstream([httpx.Response(200, json=payload)])
    with pytest.raises(ScoringError):
        scorer_for(up).score_many("q", ["a", "b"])


def test_duplicate_index_with_all_indices_present_is_error():
    payload = {
        "results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.5},
            {"index": 0, "relevance_score": 0.1},
        ]
    }
    up = Upstream([httpx.Response(200, json=payload)])
    with pytest.raises(ScoringError):
        scorer_for(up).score_many("q", ["a", "b"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1.7, 1.0), (-0.3, 0.0), (1, 1.0), (0, 0.0), (1.0000001, 1.0), (0.42, 0.42)],
)
def test_scores_outside_unit_interval_are_clamped(raw, expected):
    up = Upstream([httpx.Response(200, json={"results": [{"index": 0, "relevance_score": raw}]})])
    assert scorer_for(up).score_many("q", ["a"]) == [expected]


@pytest.mark.parametrize("bad", [None, "0.5", True, [0.5]])
def test_non_numeric_score_is_error(bad):
    up = Upstream([httpx.Response(200, json={"results": [{"index": 0, "relevance_score": bad}]})])
    with pytest.raises(ScoringError):
        scorer_for(up).score_many("q", ["a"])


def test_invalid_json_and_non_object_body_are_errors():
    up = Upstream([httpx.Response(200, text="not json"), httpx.Response(200, json=[1, 2])])
    scorer = scorer_for(up)
    with pytest.raises(ScoringError):
        scorer.score_many("q", ["a"])
    with pytest.raises(ScoringError):
        scorer.score_many("q", ["a"])


# -- batching ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n_docs", "max_docs", "sizes"),
    [
        (3, 3, [3]),
        (4, 3, [3, 1]),
        (1, 1, [1]),
        (2, 1, [1, 1]),
        (100, 100, [100]),
        (101, 100, [100, 1]),
    ],
)
def test_batching_at_and_just_over_max_documents(n_docs, max_docs, sizes):
    docs = [f"doc-{i}" for i in range(n_docs)]
    up = Upstream(default=by_doc(lambda d: int(d.split("-")[1]) / 1000))
    scores = scorer_for(up, foundry_max_documents=max_docs).score_many("q", docs)
    assert [len(b["documents"]) for b in up.bodies()] == sizes
    assert [d for b in up.bodies() for d in b["documents"]] == docs
    assert scores == pytest.approx([i / 1000 for i in range(n_docs)])


def test_empty_bill_list_makes_no_request_even_without_endpoint():
    up = Upstream()
    assert scorer_for(up).score_many("q", []) == []
    assert up.requests == []
    assert FoundryScorer(qa_settings(foundry_endpoint="")).score_many("q", []) == []


# -- request body -----------------------------------------------------------------------------


def test_request_body_carries_configured_model_query_and_documents_only():
    up = Upstream(default=by_doc(lambda d: 0.5))
    scorer = scorer_for(up, foundry_model="my-rerank-deployment-eu")
    scorer.score_many("Protection des données", ["Projet A", "Projet B"])
    req = up.requests[0]
    assert req.method == "POST"
    assert str(req.url) == ENDPOINT
    assert req.headers["content-type"].startswith("application/json")
    body = up.bodies()[0]
    assert body == {
        "model": "my-rerank-deployment-eu",
        "query": "Protection des données",
        "documents": ["Projet A", "Projet B"],
    }


def test_resource_root_endpoint_gets_rerank_path():
    up = Upstream(default=by_doc(lambda d: 0.5))
    scorer_for(up, foundry_endpoint="https://qa-res.services.ai.azure.com").score_many("q", ["a"])
    assert str(up.requests[0].url) == ENDPOINT


def test_request_contains_no_user_data():
    up = Upstream(default=by_doc(lambda d: 0.5))
    scorer = scorer_for(up)
    scorer.score_many("blanchiment de capitaux", ["Projet de loi 8407"])
    raw = up.requests[0].content.decode() + " ".join(
        f"{k}:{v}" for k, v in up.requests[0].headers.items()
    )
    assert "@" not in raw
    assert "owner" not in raw.lower()


# -- retries ----------------------------------------------------------------------------------


def test_429_retry_after_seconds_is_honoured_with_fixture_body():
    up = Upstream(
        [
            httpx.Response(
                429, json=fixture("rerank_error_429.json"), headers={"Retry-After": "3"}
            ),
            by_doc(lambda d: 0.7),
        ]
    )
    sleeps: list[float] = []
    assert scorer_for(up, sleeps).score_many("q", ["a"]) == [0.7]
    assert sleeps == [3.0]
    assert len(up.requests) == 2


def test_429_retry_after_http_date_is_honoured():
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
    up = Upstream([httpx.Response(429, headers={"Retry-After": when}), by_doc(lambda d: 0.7)])
    sleeps: list[float] = []
    scorer_for(up, sleeps).score_many("q", ["a"])
    assert len(sleeps) == 1 and 15.0 <= sleeps[0] <= 21.0


def test_429_without_retry_after_uses_backoff_and_eventually_fails():
    up = Upstream(default=lambda r: httpx.Response(429, json=fixture("rerank_error_429.json")))
    sleeps: list[float] = []
    with pytest.raises(ScoringError, match="Too many requests"):
        scorer_for(up, sleeps, foundry_max_retries=3).score_many("q", ["a"])
    assert len(up.requests) == 4
    assert len(sleeps) == 3
    assert sleeps == sorted(sleeps) and sleeps[0] > 0  # growing backoff


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_retried_then_succeeds(status):
    up = Upstream([httpx.Response(status), httpx.Response(status), by_doc(lambda d: 0.25)])
    sleeps: list[float] = []
    scorer = scorer_for(up, sleeps, foundry_max_retries=2)
    assert scorer.score_many("q", ["a"]) == [0.25]
    assert len(up.requests) == 3 and len(sleeps) == 2
    assert sleeps[1] > sleeps[0]
    assert not scorer.circuit_open


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_4xx_is_not_retried_with_api_key(status):
    up = Upstream(default=lambda r: httpx.Response(status, json={"message": "nope"}))
    sleeps: list[float] = []
    with pytest.raises(ScoringError) as info:
        scorer_for(up, sleeps).score_many("q", ["a"])
    assert not isinstance(info.value, ScorerUnavailable)
    assert len(up.requests) == 1 and sleeps == []


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout],
)
def test_timeout_is_retried(exc):
    req = httpx.Request("POST", ENDPOINT)
    up = Upstream([exc("slow", request=req), by_doc(lambda d: 0.6)])
    sleeps: list[float] = []
    assert scorer_for(up, sleeps).score_many("q", ["a"]) == [0.6]
    assert len(up.requests) == 2 and len(sleeps) == 1


def test_timeout_exhausted_is_scoring_error():
    req = httpx.Request("POST", ENDPOINT)
    up = Upstream(default=lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req)))
    with pytest.raises(ScoringError, match="timeout"):
        scorer_for(up, foundry_max_retries=1).score_many("q", ["a"])
    assert len(up.requests) == 2


def test_configured_timeout_applies_to_own_client():
    scorer = FoundryScorer(qa_settings(foundry_timeout_seconds=7.5))
    client = scorer._http()
    try:
        assert client.timeout.read == 7.5 and client.timeout.connect == 7.5
    finally:
        scorer.close()


# -- circuit breaker --------------------------------------------------------------------------


def test_circuit_opens_after_n_failed_requests_and_stays_open():
    up = Upstream([httpx.Response(503), httpx.Response(503), httpx.Response(503)])
    up.default = by_doc(lambda d: 0.9)  # the endpoint recovers, but the run must not call it
    scorer = scorer_for(up, foundry_max_retries=0, foundry_circuit_threshold=3)
    for _ in range(2):
        with pytest.raises(ScoringError) as info:
            scorer.score_many("q", ["a"])
        assert not isinstance(info.value, ScorerUnavailable)
    with pytest.raises(ScorerUnavailable):
        scorer.score_many("q", ["a"])
    assert scorer.circuit_open
    n = len(up.requests)
    assert n == 3
    for _ in range(3):
        with pytest.raises(ScorerUnavailable):
            scorer.score_many("another rule", ["a", "b"])
    assert len(up.requests) == n


def test_circuit_counts_requests_not_attempts():
    # 2 retries per request: one failed request = 3 attempts, still one failure for the breaker
    up = Upstream(default=lambda r: httpx.Response(500))
    scorer = scorer_for(up, foundry_max_retries=2, foundry_circuit_threshold=2)
    with pytest.raises(ScoringError) as info:
        scorer.score_many("q", ["a"])
    assert not isinstance(info.value, ScorerUnavailable)
    assert len(up.requests) == 3
    with pytest.raises(ScorerUnavailable):
        scorer.score_many("q", ["a"])
    assert len(up.requests) == 6


def test_circuit_open_mid_batch_stops_remaining_batches():
    up = Upstream([by_doc(lambda d: 0.1)], default=lambda r: httpx.Response(503))
    scorer = scorer_for(
        up, foundry_max_retries=0, foundry_circuit_threshold=1, foundry_max_documents=1
    )
    with pytest.raises(ScorerUnavailable):
        scorer.score_many("q", ["a", "b", "c"])
    assert len(up.requests) == 2  # batch 1 ok, batch 2 fails and opens, batch 3 never sent


def test_get_scorer_gives_fresh_breaker_per_run():
    s = qa_settings()
    a, b = get_scorer(s), get_scorer(s)
    assert a is not b
    a._circuit_open = True
    assert not b.circuit_open


# -- billing ----------------------------------------------------------------------------------


def test_input_tokens_accumulate_across_batches_and_calls():
    up = Upstream(
        default=by_doc(lambda d: 0.5, lambda body: {"input_tokens": 100 * len(body["documents"])})
    )
    scorer = scorer_for(up, foundry_max_documents=2)
    scorer.score_many("q", ["a", "b", "c", "d", "e"])  # 3 requests: 200 + 200 + 100
    assert scorer.billed_units == 500
    scorer.score_many("q2", ["a"])
    assert scorer.billed_units == 600
    assert scorer.billing_unit == "input_tokens"


def test_search_units_accumulate_across_batches():
    up = Upstream(default=by_doc(lambda d: 0.5, lambda body: {"search_units": 1}))
    scorer = scorer_for(up, foundry_model="Cohere-rerank-v4.0-pro", foundry_max_documents=3)
    scorer.score_many("q", [str(i) for i in range(7)])  # 3 requests
    assert scorer.billed_units == 3
    assert scorer.billing_unit == "search_units"


def test_failed_batch_bills_nothing_but_earlier_batches_count():
    up = Upstream(
        [by_doc(lambda d: 0.5, lambda body: {"input_tokens": 40})],
        default=lambda r: httpx.Response(400, json={"message": "bad"}),
    )
    scorer = scorer_for(up, foundry_max_documents=1)
    with pytest.raises(ScoringError):
        scorer.score_many("q", ["a", "b"])
    assert scorer.billed_units == 40


# -- authentication ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "name", "value"),
    [
        ("Authorization", "authorization", "Bearer qa-key"),
        ("authorization", "authorization", "Bearer qa-key"),
        ("api-key", "api-key", "qa-key"),
        ("Api-Key", "api-key", "qa-key"),
    ],
)
def test_api_key_header_variants(header, name, value):
    up = Upstream(default=by_doc(lambda d: 0.5))
    scorer_for(up, foundry_api_key_header=header).score_many("q", ["a"])
    headers = up.requests[0].headers
    assert headers[name] == value
    other = "api-key" if name == "authorization" else "authorization"
    assert other not in headers


def test_api_key_never_asks_entra():
    class Exploding:
        def get_token(self, *scopes):
            raise AssertionError("credential must not be used with an API key")

    up = Upstream(default=by_doc(lambda d: 0.5))
    scorer = FoundryScorer(
        qa_settings(),
        client=httpx.Client(transport=httpx.MockTransport(up)),
        credential=Exploding(),
    )
    assert scorer.score_many("q", ["a"]) == [0.5]


@dataclass
class Token:
    token: str
    expires_on: int


class FakeCredential:
    def __init__(self, lifetime: int = 3600, clock: list[float] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.lifetime = lifetime
        self.clock = clock or [0.0]

    def get_token(self, *scopes: str) -> Token:
        self.calls.append(scopes)
        return Token(f"entra-{len(self.calls)}", int(self.clock[0]) + self.lifetime)


def entra_scorer(up: Upstream, cred: FakeCredential, clock: list[float], **overrides):
    return FoundryScorer(
        qa_settings(foundry_api_key="", **overrides),
        client=httpx.Client(transport=httpx.MockTransport(up)),
        credential=cred,
        sleep=lambda s: None,
        clock=lambda: clock[0],
    )


def test_entra_token_cached_across_requests_and_refreshed_after_expiry():
    clock = [1_000_000.0]
    cred = FakeCredential(lifetime=3600, clock=clock)
    up = Upstream(default=by_doc(lambda d: 0.5))
    scorer = entra_scorer(up, cred, clock, foundry_max_documents=1)
    scorer.score_many("q", ["a", "b", "c"])  # 3 requests, one token
    assert len(cred.calls) == 1
    assert cred.calls[0] == ("https://cognitiveservices.azure.com/.default",)
    clock[0] += 1800
    scorer.score_many("q", ["a"])
    assert len(cred.calls) == 1
    clock[0] += 3600  # well past expiry
    scorer.score_many("q", ["a"])
    assert len(cred.calls) == 2
    auth = [r.headers["authorization"] for r in up.requests]
    assert auth == ["Bearer entra-1"] * 4 + ["Bearer entra-2"]


def test_entra_scope_is_configurable():
    clock = [0.0]
    cred = FakeCredential(clock=clock)
    up = Upstream(default=by_doc(lambda d: 0.5))
    entra_scorer(up, cred, clock, foundry_token_scope="https://ai.azure.com/.default").score_many(
        "q", ["a"]
    )
    assert cred.calls == [("https://ai.azure.com/.default",)]


def test_entra_401_refreshes_token_and_retries_once():
    clock = [0.0]
    cred = FakeCredential(clock=clock)
    up = Upstream([httpx.Response(401, json={"message": "token expired"}), by_doc(lambda d: 0.8)])
    scorer = entra_scorer(up, cred, clock)
    assert scorer.score_many("q", ["a"]) == [0.8]
    assert [r.headers["authorization"] for r in up.requests] == [
        "Bearer entra-1",
        "Bearer entra-2",
    ]


def test_entra_persistent_401_does_not_loop():
    clock = [0.0]
    cred = FakeCredential(clock=clock)
    up = Upstream(default=lambda r: httpx.Response(401, json={"message": "no role"}))
    scorer = entra_scorer(up, cred, clock)
    with pytest.raises(ScoringError):
        scorer.score_many("q", ["a"])
    assert len(up.requests) <= 2


# -- defaults ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("Cohere-rerank-v5-pro", 0.5),
        ("Cohere-rerank-v5-fast", 0.45),
        ("Cohere-rerank-v4.0-pro", 0.5),
    ],
)
def test_per_model_default_threshold_matches_factory(model, expected):
    s = qa_settings(foundry_model=model)
    assert FoundryScorer(s).default_threshold == expected == default_threshold_for(s)


def test_explicit_default_threshold_wins_over_model_default():
    s = qa_settings(
        foundry_model="Cohere-rerank-v5-fast",
        default_threshold=0.62,
        default_threshold_explicit=True,
    )
    assert FoundryScorer(s).default_threshold == 0.62 == default_threshold_for(s)
