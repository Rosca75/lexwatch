"""FoundryScorer against recorded responses (httpx.MockTransport). Never touches the network."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from lexwatch.scoring.base import Scorer, ScorerUnavailable, ScoringError
from lexwatch.scoring.foundry import (
    RERANK_PATH,
    FoundryScorer,
    error_detail,
    foundry_default_threshold,
    resolve_endpoint,
)
from tests.helpers import make_test_settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "foundry"
ENDPOINT = "https://res.services.ai.azure.com/providers/cohere/v2/rerank"
DOCS = [f"Projet de loi numéro {i}" for i in range(5)]


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def settings(**overrides):
    base = {
        "scorer": "foundry",
        "foundry_endpoint": ENDPOINT,
        "foundry_model": "Cohere-rerank-v5-pro",
        "foundry_api_key": "secret-key",
        "foundry_max_retries": 3,
        "foundry_circuit_threshold": 3,
    }
    base.update(overrides)
    return make_test_settings(**base)


@dataclass
class Recorder:
    """MockTransport handler returning queued responses and recording requests."""

    responses: list = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    default: object = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if self.responses else self.default
        if callable(item):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, httpx.Response), "no response queued"
        return item

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def echo_scores(request: httpx.Request, usage: dict | None = None) -> httpx.Response:
    """Score document i as (i+1)/100, results sorted by score desc, like the real API."""
    body = json.loads(request.content)
    results = [
        {"index": i, "relevance_score": (i + 1) / 100} for i in range(len(body["documents"]))
    ]
    results.sort(key=lambda r: -r["relevance_score"])
    meta = {"billed_units": usage or {"input_tokens": 10 * len(results)}}
    return httpx.Response(200, json={"id": "x", "results": results, "meta": meta})


def make_scorer(rec: Recorder, sleeps: list[float] | None = None, **overrides) -> FoundryScorer:
    client = httpx.Client(transport=httpx.MockTransport(rec))
    sleep_log = sleeps if sleeps is not None else []
    return FoundryScorer(
        settings(**overrides), client=client, sleep=sleep_log.append, rand=lambda: 1.0
    )


# -- endpoint / defaults --------------------------------------------------------------------


def test_resolve_endpoint_appends_path_to_resource_root():
    root = "https://res.services.ai.azure.com"
    assert resolve_endpoint(root) == root + RERANK_PATH
    assert resolve_endpoint(root + "/") == root + RERANK_PATH
    assert resolve_endpoint(ENDPOINT) == ENDPOINT
    assert (
        resolve_endpoint("https://gw.example/custom/rerank") == "https://gw.example/custom/rerank"
    )
    assert resolve_endpoint("") == ""


def test_default_threshold_per_model():
    assert foundry_default_threshold("Cohere-rerank-v5-pro") == 0.5
    assert foundry_default_threshold("Cohere-rerank-v5-fast") == 0.45
    assert foundry_default_threshold("Cohere-rerank-v4.0-pro") == 0.5
    assert FoundryScorer(settings()).default_threshold == 0.5
    explicit = settings(default_threshold=0.7, default_threshold_explicit=True)
    assert FoundryScorer(explicit).default_threshold == 0.7


def test_implements_protocol():
    assert isinstance(FoundryScorer(settings()), Scorer)


# -- request / response -----------------------------------------------------------------------


def test_recorded_response_maps_index_back_to_input_order():
    rec = Recorder([httpx.Response(200, json=recorded("rerank_response.json"))])
    scorer = make_scorer(rec)
    scores = scorer.score_many("Lutte contre le blanchiment", DOCS)
    assert scores == pytest.approx([0.6382451, 0.0062190853, 0.91734606, 0.034817535, 0.21907842])
    body = rec.bodies()[0]
    assert body == {
        "model": "Cohere-rerank-v5-pro",
        "query": "Lutte contre le blanchiment",
        "documents": DOCS,
    }
    assert "top_n" not in body
    assert str(rec.requests[0].url) == ENDPOINT
    assert scorer.billed_units == 412 and scorer.billing_unit == "input_tokens"


def test_max_tokens_per_doc_is_sent_when_configured():
    rec = Recorder(default=echo_scores)
    make_scorer(rec, foundry_max_tokens_per_doc=512).score_many("q", ["a"])
    assert rec.bodies()[0]["max_tokens_per_doc"] == 512


def test_batches_by_max_documents_and_keeps_order():
    rec = Recorder(default=echo_scores)
    scorer = make_scorer(rec, foundry_max_documents=2)
    scores = scorer.score_many("q", DOCS)
    assert [len(b["documents"]) for b in rec.bodies()] == [2, 2, 1]
    assert scores == pytest.approx([0.01, 0.02, 0.01, 0.02, 0.01])
    assert scorer.billed_units == 50 and scorer.requests == 3


def test_empty_input_makes_no_request():
    rec = Recorder()
    assert make_scorer(rec).score_many("q", []) == []
    assert rec.requests == []


def test_scores_are_clamped():
    payload = {
        "results": [{"index": 0, "relevance_score": 1.2}, {"index": 1, "relevance_score": -0.1}]
    }
    rec = Recorder([httpx.Response(200, json=payload)])
    assert make_scorer(rec).score_many("q", ["a", "b"]) == [1.0, 0.0]


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [{"index": 0, "relevance_score": 0.5}]},  # index 1 missing
        {"results": [{"index": 0, "relevance_score": 0.5}, {"index": 7, "relevance_score": 0.1}]},
        {"results": [{"index": 0, "relevance_score": "x"}, {"index": 1, "relevance_score": 0.1}]},
        {"no_results": []},
    ],
)
def test_bad_results_raise_scoring_error(payload):
    rec = Recorder([httpx.Response(200, json=payload)])
    with pytest.raises(ScoringError):
        make_scorer(rec).score_many("q", ["a", "b"])


def test_billing_search_units_for_v4():
    payload = {
        "results": [{"index": 0, "relevance_score": 0.3}],
        "meta": {"billed_units": {"search_units": 1}},
    }
    rec = Recorder([httpx.Response(200, json=payload), httpx.Response(200, json=payload)])
    scorer = make_scorer(rec, foundry_model="Cohere-rerank-v4.0-fast")
    scorer.score_many("q", ["a"])
    scorer.score_many("q2", ["a"])
    assert scorer.billed_units == 2 and scorer.billing_unit == "search_units"


def test_billing_absent_meta_is_ignored():
    rec = Recorder([httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.3}]})])
    scorer = make_scorer(rec)
    scorer.score_many("q", ["a"])
    assert scorer.billed_units == 0 and scorer.billing_unit is None


# -- retries / backoff ------------------------------------------------------------------------


def test_429_retry_after_is_honoured():
    rec = Recorder(
        [
            httpx.Response(
                429, json=recorded("rerank_error_429.json"), headers={"Retry-After": "7"}
            ),
            httpx.Response(429, headers={"retry-after-ms": "1500"}),
            echo_scores,
        ]
    )
    sleeps: list[float] = []
    scorer = make_scorer(rec, sleeps)
    assert scorer.score_many("q", ["a"]) == [0.01]
    assert sleeps == [7.0, 1.5]
    assert len(rec.requests) == 3


def test_5xx_and_transport_errors_use_exponential_backoff():
    req = httpx.Request("POST", ENDPOINT)
    rec = Recorder(
        [
            httpx.Response(503, text="busy"),
            httpx.ReadTimeout("slow", request=req),
            httpx.ConnectError("refused", request=req),
            echo_scores,
        ]
    )
    sleeps: list[float] = []
    assert make_scorer(rec, sleeps).score_many("q", ["a"]) == [0.01]
    assert sleeps == [1.0, 2.0, 4.0]  # rand() == 1.0 -> no jitter reduction


def test_jitter_reduces_delay():
    rec = Recorder([httpx.Response(500), echo_scores])
    sleeps: list[float] = []
    scorer = FoundryScorer(
        settings(),
        client=httpx.Client(transport=httpx.MockTransport(rec)),
        sleep=sleeps.append,
        rand=lambda: 0.0,
    )
    scorer.score_many("q", ["a"])
    assert sleeps == [0.5]


def test_retries_exhausted_raise_scoring_error_with_gateway_message():
    gateway = {"statusCode": 500, "message": "Internal server error", "activityId": "act-1"}
    rec = Recorder(default=lambda r: httpx.Response(500, json=gateway))
    sleeps: list[float] = []
    scorer = make_scorer(rec, sleeps, foundry_max_retries=2)
    with pytest.raises(ScoringError) as info:
        scorer.score_many("q", ["a"])
    assert not isinstance(info.value, ScorerUnavailable)
    assert "Internal server error" in str(info.value) and "act-1" in str(info.value)
    assert len(rec.requests) == 3 and len(sleeps) == 2


def test_4xx_is_not_retried():
    rec = Recorder([httpx.Response(404, json={"id": "c1", "message": "DeploymentNotFound"})])
    sleeps: list[float] = []
    with pytest.raises(ScoringError, match="DeploymentNotFound"):
        make_scorer(rec, sleeps).score_many("q", ["a"])
    assert len(rec.requests) == 1 and sleeps == []


def test_error_detail_shapes():
    def resp(**kw):
        return httpx.Response(request=httpx.Request("POST", ENDPOINT), **kw)

    assert error_detail(resp(status_code=429, json={"id": "i1", "message": "Too many"})) == (
        "HTTP 429: Too many (ref i1)"
    )
    azure = {"error": {"code": "Unauthorized", "message": "bad token"}}
    assert error_detail(resp(status_code=401, json=azure)) == "HTTP 401: bad token"
    plain = resp(status_code=502, text="Bad gateway", headers={"apim-request-id": "a9"})
    assert error_detail(plain) == "HTTP 502: Bad gateway (ref a9)"


# -- circuit breaker --------------------------------------------------------------------------


def test_circuit_breaker_opens_after_consecutive_failures():
    rec = Recorder(default=lambda r: httpx.Response(503))
    scorer = make_scorer(rec, foundry_max_retries=0, foundry_circuit_threshold=2)
    with pytest.raises(ScoringError) as first:
        scorer.score_many("q", ["a"])
    assert not isinstance(first.value, ScorerUnavailable)
    with pytest.raises(ScorerUnavailable):
        scorer.score_many("q", ["a"])
    assert scorer.circuit_open
    calls = len(rec.requests)
    with pytest.raises(ScorerUnavailable):
        scorer.score_many("q", ["a"])
    assert len(rec.requests) == calls  # open circuit: no further requests


def test_success_resets_failure_count():
    rec = Recorder(
        [httpx.Response(503), echo_scores, httpx.Response(503), echo_scores],
    )
    scorer = make_scorer(rec, foundry_max_retries=0, foundry_circuit_threshold=2)
    for _ in range(2):
        with pytest.raises(ScoringError):
            scorer.score_many("q", ["a"])
        scorer.score_many("q", ["a"])
    assert not scorer.circuit_open


def test_fresh_instance_has_closed_circuit():
    rec = Recorder(default=lambda r: httpx.Response(503))
    scorer = make_scorer(rec, foundry_max_retries=0, foundry_circuit_threshold=1)
    with pytest.raises(ScorerUnavailable):
        scorer.score_many("q", ["a"])
    rec.default = echo_scores
    assert make_scorer(rec).score_many("q", ["a"]) == [0.01]


# -- auth -------------------------------------------------------------------------------------


def test_api_key_bearer_header_by_default():
    rec = Recorder(default=echo_scores)
    make_scorer(rec).score_many("q", ["a"])
    assert rec.requests[0].headers["Authorization"] == "Bearer secret-key"
    assert "api-key" not in rec.requests[0].headers


def test_api_key_alternative_header():
    rec = Recorder(default=echo_scores)
    make_scorer(rec, foundry_api_key_header="api-key").score_many("q", ["a"])
    assert rec.requests[0].headers["api-key"] == "secret-key"
    assert "Authorization" not in rec.requests[0].headers


@dataclass
class FakeToken:
    token: str
    expires_on: int


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    def get_token(self, *scopes: str) -> FakeToken:
        self.scopes.extend(scopes)
        return FakeToken(f"tok-{len(self.scopes)}", expires_on=10_000)


def entra_scorer(rec: Recorder, credential, now: list[float]) -> FoundryScorer:
    return FoundryScorer(
        settings(foundry_api_key="", foundry_token_scope="https://ai.azure.com/.default"),
        client=httpx.Client(transport=httpx.MockTransport(rec)),
        credential=credential,
        sleep=lambda s: None,
        clock=lambda: now[0],
    )


def test_entra_token_is_cached_until_expiry():
    rec = Recorder(default=echo_scores)
    cred = FakeCredential()
    now = [1_000.0]
    scorer = entra_scorer(rec, cred, now)
    scorer.score_many("q", ["a"])
    scorer.score_many("q", ["a"])
    assert cred.scopes == ["https://ai.azure.com/.default"]
    assert [r.headers["Authorization"] for r in rec.requests] == ["Bearer tok-1"] * 2
    now[0] = 9_800.0  # within the refresh margin before expires_on=10_000
    scorer.score_many("q", ["a"])
    assert rec.requests[-1].headers["Authorization"] == "Bearer tok-2"


def test_entra_credential_failure_is_scoring_error():
    class Broken:
        def get_token(self, *scopes):
            raise RuntimeError("no identity")

    rec = Recorder(default=echo_scores)
    with pytest.raises(ScoringError, match="Entra ID token"):
        entra_scorer(rec, Broken(), [0.0]).score_many("q", ["a"])
    assert rec.requests == []


def test_entra_401_retries_once_with_fresh_token():
    rec = Recorder([httpx.Response(401, json={"message": "expired"}), echo_scores])
    scorer = entra_scorer(rec, FakeCredential(), [0.0])
    assert scorer.score_many("q", ["a"]) == [0.01]
    assert [r.headers["Authorization"] for r in rec.requests] == ["Bearer tok-1", "Bearer tok-2"]


def test_entra_repeated_401_fails_and_next_call_gets_fresh_token():
    unauthorized = httpx.Response(401, json={"message": "no role"})
    rec = Recorder([unauthorized, unauthorized, echo_scores])
    scorer = entra_scorer(rec, FakeCredential(), [0.0])
    with pytest.raises(ScoringError, match="no role"):
        scorer.score_many("q", ["a"])
    assert len(rec.requests) == 2
    scorer.score_many("q", ["a"])
    assert rec.requests[-1].headers["Authorization"] == "Bearer tok-3"


def test_api_key_401_is_not_retried():
    rec = Recorder([httpx.Response(401, json={"message": "bad key"})])
    with pytest.raises(ScoringError, match="bad key"):
        make_scorer(rec).score_many("q", ["a"])
    assert len(rec.requests) == 1


def test_missing_endpoint_is_scoring_error():
    with pytest.raises(ScoringError, match="FOUNDRY_ENDPOINT"):
        FoundryScorer(settings(foundry_endpoint="")).score_many("q", ["a"])


def test_secrets_and_documents_are_not_logged(caplog):
    rec = Recorder([httpx.Response(503), httpx.Response(400, json={"message": "bad"})])
    scorer = make_scorer(rec)
    with caplog.at_level("DEBUG"), pytest.raises(ScoringError):
        scorer.score_many("secret rule", ["confidential document text"])
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "secret-key" not in logged
    assert "confidential document text" not in logged
