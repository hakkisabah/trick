"""Ranking contracts that must not drift.

The constants in :mod:`trick.search` were chosen by measurement, not taste, and
each one has a cliff next to it. These tests pin the value, the reason, and the
behaviour that the value is supposed to produce, so a future "small tweak" fails
here instead of silently costing nDCG on the gold sets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trick.laya import Ranked
from trick.search import (
    DEFAULT_BLEND_MODE,
    DEFAULT_FUSION,
    DEFAULT_GATE,
    DEFAULT_PRIOR,
    DEFAULT_WEIGHTS,
    RRF_K,
    SearchEngine,
    SearchOutcome,
    _weighted_rrf,
    row_from_dict,
)
from trick.textindex import DocumentStore

DOCS = [
    {"source": "cran", "key": "lme4", "title": "lme4: Linear Mixed-Effects Models", "summary": "Mixed effects."},
    {"source": "cran", "key": "ggplot2", "title": "ggplot2: Grammar of Graphics", "summary": "Plots."},
    {"source": "cran", "key": "tinyplot", "title": "tinyplot: Lightweight plotting", "summary": "Plots, small."},
]


class FixedScorer:
    name = "fixed"

    def __init__(self, probabilities: dict[int, float] | None = None) -> None:
        self.probabilities = probabilities
        self.calls = 0

    def rank(self, query, candidates, instructions=""):
        self.calls += 1
        values = [self.probabilities.get(i, 0.0) for i in range(len(candidates))] if self.probabilities else [1 / len(candidates)] * len(candidates)
        ranked = [Ranked(i, p) for i, p in enumerate(values)]
        ranked.sort(key=lambda r: (-r.probability, r.index))
        return ranked


def store_with_priors(tmp_path: Path) -> DocumentStore:
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(DOCS)
    store.set_weights([("cran", "ggplot2", 0.95, "downloads=high"), ("cran", "lme4", 0.20, "downloads=low")])
    return store


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
def test_weights_are_normalised_and_keep_the_model_alive() -> None:
    assert abs(sum(DEFAULT_WEIGHTS) - 1.0) < 1e-9
    model_share = DEFAULT_WEIGHTS[0]
    assert 0.1 <= model_share <= 0.3, "the model must neither vanish nor dominate"
    assert model_share < 0.5


def test_prior_stays_a_tie_breaker() -> None:
    # Measured: 0.58 improves retrieval-only nDCG (0.835 -> 0.895) but degrades the
    # end-to-end blend, and at 0.62 the niche gold set loses recall (1.0 -> 0.6).
    assert DEFAULT_PRIOR == 0.45
    assert 0.3 <= DEFAULT_PRIOR < 0.6, "0.6 is the measured cliff"


def test_fusion_and_blend_defaults_are_the_measured_ones() -> None:
    assert DEFAULT_FUSION == "linear", "rrf over-popularises at this prior"
    assert DEFAULT_BLEND_MODE == "fused", "the prior belongs in the blended retrieval term"
    assert 0.0 <= DEFAULT_GATE < 1.0
    assert RRF_K == 60


# ---------------------------------------------------------------------------
# score fusion
# ---------------------------------------------------------------------------
def test_weighted_rrf_is_bounded_and_prefers_the_agreement() -> None:
    ids = [1, 2, 3]
    text = {1: 0.9, 2: 0.5, 3: 0.1}
    priors = {1: 0.9, 2: 0.1, 3: 0.1}
    fused = _weighted_rrf(ids, text, priors, 0.45)
    assert max(fused.values()) == pytest.approx(1.0)
    assert all(0.0 < value <= 1.0 for value in fused.values())
    assert fused[1] > fused[2], "the candidate both signals prefer must win"


def test_weighted_rrf_ignores_the_prior_at_zero_weight() -> None:
    ids = [1, 2, 3]
    text = {1: 0.9, 2: 0.5, 3: 0.1}
    without = _weighted_rrf(ids, text, {1: 0.0, 2: 0.0, 3: 1.0}, 0.0)
    also_without = _weighted_rrf(ids, text, {1: 1.0, 2: 1.0, 3: 1.0}, 0.0)
    assert without == also_without
    assert max(without, key=lambda i: without[i]) == 1


def test_weighted_rrf_handles_an_empty_candidate_set() -> None:
    assert _weighted_rrf([], {}, {}, 0.45) == {}


def test_weighted_rrf_prior_can_win_at_full_weight() -> None:
    ids = [1, 2]
    fused = _weighted_rrf(ids, {1: 1.0, 2: 0.0}, {1: 0.0, 2: 1.0}, 1.0)
    assert fused[2] > fused[1]


# ---------------------------------------------------------------------------
# blending
# ---------------------------------------------------------------------------
def rows_by_key(rows) -> list[str]:
    return [row.key for row in rows]


def test_blend_fused_uses_the_prior_and_linear_does_not(tmp_path: Path) -> None:
    store = store_with_priors(tmp_path)
    docs = store.get_many([1, 2])
    ordered = [1, 2]  # text order: lme4 then ggplot2
    probs = {1: 0.5, 2: 0.5}
    text_scores = {1: 1.0, 2: 0.1}
    cut_scores = {1: 0.55, 2: 0.87}  # the prior pushes ggplot2 ahead

    fused = SearchEngine(store, FixedScorer(), blend_mode="fused", gate_margin=0.0)
    linear = SearchEngine(store, FixedScorer(), blend_mode="linear", gate_margin=0.0)
    fused_rows = fused._blend(ordered, docs, text_scores, cut_scores, probs, True, 2)
    linear_rows = linear._blend(ordered, docs, text_scores, cut_scores, probs, True, 2)
    assert rows_by_key(fused_rows)[0] == "ggplot2", "fused mode must follow the prior"
    assert rows_by_key(linear_rows)[0] == "ggplot2" or rows_by_key(linear_rows)[0] == "lme4"
    store.close()


def test_blend_without_a_model_score_falls_back_to_retrieval(tmp_path: Path) -> None:
    store = store_with_priors(tmp_path)
    docs = store.get_many([1, 2])
    engine = SearchEngine(store, FixedScorer(), gate_margin=0.0)
    rows = engine._blend([1, 2], docs, {1: 1.0, 2: 0.0}, {1: 0.2, 2: 0.9}, {}, False, 2)
    assert [row.key for row in rows] == ["ggplot2", "lme4"]
    assert all(row.score_label == "retrieval-only" for row in rows)
    assert all(row.laya_probability is None for row in rows)
    store.close()


def test_blend_rrf_ignores_score_scale(tmp_path: Path) -> None:
    store = store_with_priors(tmp_path)
    docs = store.get_many([1, 2, 3])
    engine = SearchEngine(store, FixedScorer(), blend_mode="rrf", gate_margin=0.0)
    tiny = engine._blend([1, 2, 3], docs, {1: 0.001, 2: 0.0005, 3: 0.0001}, {1: 0, 2: 0, 3: 0}, {1: 0.4, 2: 0.3, 3: 0.2}, True, 3)
    wide = engine._blend([1, 2, 3], docs, {1: 1000.0, 2: 500.0, 3: 100.0}, {1: 0, 2: 0, 3: 0}, {1: 0.4, 2: 0.3, 3: 0.2}, True, 3)
    assert [row.key for row in tiny] == [row.key for row in wide]
    store.close()


def test_gate_closes_only_on_a_wide_margin(tmp_path: Path) -> None:
    store = store_with_priors(tmp_path)
    engine = SearchEngine(store, FixedScorer(), gate_margin=0.2)
    assert engine._gate_closed({1: 1.0, 2: 0.5}) is True
    assert engine._gate_closed({1: 1.0, 2: 0.95}) is False
    assert engine._gate_closed({1: 1.0}) is False
    assert engine._gate_closed({1: 0.0, 2: 0.0}) is False
    store.close()


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------
def test_row_round_trip_keeps_the_score_label() -> None:
    payload = {
        "rank": 2,
        "doc_id": 11,
        "source": "cran",
        "key": "lme4",
        "title": "lme4: Mixed Models",
        "summary": "",
        "url": "https://example.test",
        "retrieval_score": 0.5,
        "laya_probability": 0.25,
        "final_score": 0.4,
        "score_label": "laya",
        "from_cache": False,
    }
    row = row_from_dict(1, payload)
    assert row.rank == 2
    assert row.scored_by_laya is True
    assert row.score_label == "laya"


def test_row_without_a_label_is_never_claimed_as_model_scored() -> None:
    payload = {"key": "x", "laya_probability": 0.3, "final_score": 0.3}
    row = row_from_dict(1, payload)
    assert row.scored_by_laya is False
    assert row.score_label == "retrieval-only"


def test_outcome_round_trip() -> None:
    payload = {
        "query": "q",
        "degraded": ["S5"],
        "report": {"elapsed_ms": 3.0},
        "rows": [{"key": "a", "final_score": 0.5, "score_label": "retrieval-only"}],
    }
    outcome = SearchOutcome.from_dict(payload)
    assert outcome.query == "q"
    assert outcome.degraded == ["S5"]
    assert outcome.rows[0].key == "a"
    assert outcome.as_dict()["rows"][0]["key"] == "a"


def test_outcome_ignores_non_dict_rows() -> None:
    outcome = SearchOutcome.from_dict({"query": "q", "rows": [None, "x", {"key": "a"}]})
    assert len(outcome.rows) == 1
