"""laya client contract: label opacity, probability mapping, retries, S5 causes."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from trick.credentials import LayaCredentials
from trick.errors import ContractViolation, ScorerUnavailable
from trick.laya import (
    LABEL_ALPHABET,
    LayaClient,
    MockScorer,
    build_scorer,
    label_order,
)

BASE = "https://laya.test/v1/systemone/batch"


def creds() -> LayaCredentials:
    return LayaCredentials(base_url=BASE, api_key="test-key-not-real", timeout_ms=1000)


def choice_body(probabilities: dict[str, float]) -> bytes:
    return json.dumps(
        {
            "model": "laya",
            "results": [
                {
                    "model": "laya",
                    "answers": {
                        "best": {
                            "type": "choice",
                            "choice": max(probabilities, key=probabilities.get),
                            "probabilities": probabilities,
                            "confidence": 0.5,
                            "answer_confidence": max(probabilities.values()),
                            "action": {"act_probability": 1.0},
                        }
                    },
                    "usage": {"input_tokens": 42, "output_tokens": 0},
                }
            ],
            "total_usage": {"input_tokens": 42, "output_tokens": 0},
        }
    ).encode()


def uniform(n: int) -> dict[str, float]:
    return {LABEL_ALPHABET[i]: 1.0 / n for i in range(n)}


def http_error(code: int, body: str = "{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(BASE, code, "err", {}, io.BytesIO(body.encode()))


def make_client(handler, **kwargs) -> LayaClient:
    """Build a client whose transport is ``handler`` and whose clock never sleeps."""
    return LayaClient(
        creds(),
        opener=handler,
        sleep=lambda _s: None,
        clock=lambda: 0.0,
        jitter=lambda: 0.0,
        **kwargs,
    )


def test_label_order_is_deterministic_and_a_permutation():
    first = label_order("mixed effects convergence", 12)
    second = label_order("mixed effects convergence", 12)
    assert first == second
    assert sorted(first) == list(range(12))
    assert first != list(range(12)), "shuffle must actually reorder"


def test_rank_maps_probabilities_back_to_caller_indices():
    query = "spatial data in R"
    candidates = ["a", "b", "c", "d"]
    order = label_order(query, 4, "trick")
    winner_source = order[2]  # the third displayed label belongs to this candidate
    probs = uniform(4)
    probs[LABEL_ALPHABET[2]] = 0.7
    probs[LABEL_ALPHABET[0]] = 0.1
    probs[LABEL_ALPHABET[1]] = 0.1
    probs[LABEL_ALPHABET[3]] = 0.1

    seen: dict = {}

    def handler(request, timeout):
        seen["payload"] = json.loads(request.data.decode())
        seen["headers"] = request.headers
        return choice_body(probs)

    client = make_client(handler)
    ranked = client.rank(query, candidates)

    assert ranked[0].index == winner_source
    assert ranked[0].probability == pytest.approx(0.7)
    assert [r.probability for r in ranked] == sorted((r.probability for r in ranked), reverse=True)
    question = seen["payload"]["questions"]["best"]
    assert question["type"] == "choice"
    assert question["instructions"], "the endpoint rejects a choice question without instructions"
    assert set(question["criteria"]) == set(LABEL_ALPHABET[:4])
    assert seen["headers"]["Authorization"] == "Bearer test-key-not-real"
    assert client.stats.calls == 1
    assert client.stats.input_tokens == 42


def test_rank_single_candidate_short_circuits_without_a_call():
    def handler(request, timeout):  # pragma: no cover - must not run
        raise AssertionError("single candidate must not hit the network")

    client = make_client(handler)
    ranked = client.rank("q", ["only one"])
    assert len(ranked) == 1 and ranked[0].index == 0 and ranked[0].probability == 1.0
    assert client.stats.calls == 0


def test_rank_rejects_more_than_the_configured_shortlist():
    client = make_client(lambda request, timeout: choice_body(uniform(13)))
    with pytest.raises(ContractViolation):
        client.rank("q", [f"c{i}" for i in range(13)])


def test_auth_failure_is_s5_auth_and_never_leaks_the_key():
    def handler(request, timeout):
        raise http_error(401, '{"error":{"message":"API anahtari eksik"}}')

    client = make_client(handler)
    with pytest.raises(ScorerUnavailable) as info:
        client.rank("q", ["a", "b"])
    assert info.value.reason == "auth"
    assert "test-key-not-real" not in str(info.value)
    assert client.stats.failures == 1


def test_rate_limit_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise http_error(429, '{"error":{"message":"slow down"}}')
        return choice_body(uniform(2))

    client = make_client(handler, retries=2)
    ranked = client.rank("q", ["a", "b"])
    assert calls["n"] == 2
    assert len(ranked) == 2
    assert client.stats.retries == 1
    assert client.stats.failures == 0


def test_server_error_exhausts_retries_and_reports_reason():
    def handler(request, timeout):
        raise http_error(503, "upstream down")

    client = make_client(handler, retries=1)
    with pytest.raises(ScorerUnavailable) as info:
        client.rank("q", ["a", "b"])
    assert info.value.reason == "http-503"
    assert client.stats.failures == 1


def test_bad_request_is_not_retried():
    calls = {"n": 0}

    def handler(request, timeout):
        calls["n"] += 1
        raise http_error(400, '{"error":{"message":"bad criteria"}}')

    client = make_client(handler, retries=3)
    with pytest.raises(ScorerUnavailable) as info:
        client.rank("q", ["a", "b"])
    assert info.value.reason == "bad-request"
    assert calls["n"] == 1


def test_circuit_breaker_opens_after_threshold():
    state = {"now": 0.0}

    def handler(request, timeout):
        raise http_error(500, "boom")

    client = LayaClient(
        creds(),
        opener=handler,
        sleep=lambda _s: None,
        clock=lambda: state["now"],
        jitter=lambda: 0.0,
        retries=0,
        breaker_threshold=2,
        breaker_cooldown_s=30.0,
    )
    for _ in range(2):
        with pytest.raises(ScorerUnavailable):
            client.rank("q", ["a", "b"])
    with pytest.raises(ScorerUnavailable) as info:
        client.rank("q", ["a", "b"])
    assert info.value.reason == "circuit-open"
    state["now"] = 31.0
    # after the cooldown the breaker closes and the next call is attempted
    with pytest.raises(ScorerUnavailable) as info:
        client.rank("q", ["a", "b"])
    assert info.value.reason == "http-500"


def test_truncated_probability_table_is_invalid_response():
    def handler(request, timeout):
        return choice_body({LABEL_ALPHABET[0]: 1.0})

    client = make_client(handler)
    with pytest.raises(ScorerUnavailable) as info:
        client.rank("q", ["a", "b", "c"])
    assert info.value.reason == "invalid-response"


def test_non_json_body_is_invalid_response():
    client = make_client(lambda request, timeout: b"<html>gateway</html>")
    with pytest.raises(ScorerUnavailable) as info:
        client.rank("q", ["a", "b"])
    assert info.value.reason == "invalid-response"


def test_score_levels_normalises_probability_mass_to_unit_interval():
    def handler(request, timeout):
        payload = json.loads(request.data.decode())
        assert payload["questions"]["fit"]["type"] == "score"
        assert payload["questions"]["fit"]["criteria"] == ["unrelated", "relevant", "exact"]
        body = {
            "results": [
                {
                    "answers": {
                        "fit": {
                            "type": "score",
                            "score": 1.4,
                            "probabilities": {"0": 0.2, "1": 0.5, "2": 0.3},
                            "confidence": 0.0,
                            "answer_confidence": 0.5,
                            "action": {"act_probability": 1.0},
                        }
                    }
                }
            ]
        }
        return json.dumps(body).encode()

    client = make_client(handler)
    value = client.score_levels("q", ["unrelated", "relevant", "exact"])
    assert 0.5 < value < 0.6  # 0.2*0 + 0.5*0.5 + 0.3*1.0 = 0.55


def test_health_reports_failure_without_raising():
    client = make_client(lambda request, timeout: (_ for _ in ()).throw(http_error(401)))
    health = client.health()
    assert health["ok"] is False
    assert health["reason"] == "auth"


def test_usage_counters_accumulate():
    client = make_client(lambda request, timeout: choice_body(uniform(2)))
    client.rank("q1", ["a", "b"])
    client.rank("q2", ["a", "b"])
    assert client.stats.calls == 2
    assert client.stats.input_tokens == 84


def test_mock_scorer_prefers_the_overlapping_candidate_and_is_deterministic():
    scorer = MockScorer()
    texts = [
        "[arxiv/preprint] Graph neural networks for molecules",
        "[cran/r-package] mixed effects models with lme4",
    ]
    first = scorer.rank("mixed effects models lme4", texts)
    second = MockScorer().rank("mixed effects models lme4", texts)
    assert first[0].index == 1
    assert [r.index for r in first] == [r.index for r in second]
    assert sum(r.probability for r in first) == pytest.approx(1.0)


def test_build_scorer_auto_without_credentials_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("TRICK_CREDENTIALS", "/nonexistent/credentials")
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    monkeypatch.delenv("LAYA_BASE_URL", raising=False)
    assert build_scorer("auto").name == "mock"
    assert build_scorer("mock").name == "mock"
    with pytest.raises(Exception):
        build_scorer("remote")
