"""End-to-end pipeline: retrieval, shortlist cut, laya blend, degradation, cache."""

from __future__ import annotations

import pytest

from trick.errors import ScorerUnavailable
from trick.laya import Ranked
from trick.matrix import MatrixWriter
from trick.quant import feature_hash_signature
from trick.retrieval import SourceDoc
from trick.search import SearchEngine
from trick.textindex import DocumentStore


class ScriptedScorer:
    """A scorer whose answer is decided by the test, not by the model."""

    name = "scripted"

    def __init__(self, winner: int = 0, error: Exception | None = None, probabilities: dict[int, float] | None = None):
        self.winner = winner
        self.error = error
        self.probabilities = probabilities
        self.calls: list[list[str]] = []

    def rank(self, query, candidates, instructions=""):
        self.calls.append(list(candidates))
        if self.error is not None:
            raise self.error
        count = len(candidates)
        if self.probabilities:
            values = [self.probabilities.get(i, 0.0) for i in range(count)]
        else:
            values = [0.05] * count
            values[self.winner % count] = 0.6
        ranked = [Ranked(i, p) for i, p in enumerate(values)]
        ranked.sort(key=lambda r: (-r.probability, r.index))
        return ranked


class FakeAdapter:
    """A live adapter that answers from memory."""

    def __init__(self, name: str, docs: list[SourceDoc], error: Exception | None = None):
        self.name = name
        self.docs = docs
        self.error = error
        self.calls = 0

    def search(self, query, limit=5, timeout=1.5):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.docs[:limit])


def doc(source, key, title, summary="", kind="work") -> dict:
    return {
        "source": source,
        "key": key,
        "title": title,
        "summary": summary,
        "url": f"https://example.test/{source}/{key}",
        "meta": {"kind": kind},
    }


DOCS = [
    doc("cran", "lme4", "lme4: Linear Mixed-Effects Models using Eigen and S4", "Mixed effects models.", "r-package"),
    doc("bioconductor", "DESeq2", "DESeq2: Differential expression of count data", "RNA-seq counts.", "bioc-package"),
    doc("arxiv", "2101.00001", "Mixed effects modelling in genomics", "Random effects review.", "preprint"),
]


def engine(tmp_path, *, scorer=None, adapters=None, shortlist=12, matrix_path=None) -> SearchEngine:
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(DOCS)
    return SearchEngine(
        store,
        scorer if scorer is not None else ScriptedScorer(winner=2),
        adapters=adapters if adapters is not None else [],
        shortlist=shortlist,
        matrix_path=matrix_path,
        # Scorer-behaviour tests must always reach the scorer; the gate is
        # exercised separately.
        gate_margin=0.0,
    )


def test_end_to_end_ranks_by_laya_and_marks_provenance(tmp_path):
    scorer = ScriptedScorer(winner=2)
    eng = engine(tmp_path, scorer=scorer, adapters=[FakeAdapter("arxiv", [])])
    outcome = eng.search("mixed effects models", limit=3)

    assert outcome.ok
    assert outcome.rows[0].scored_by_laya is True
    assert outcome.rows[0].score_label == "laya"
    assert outcome.rows[0].laya_probability == pytest.approx(0.6)
    assert outcome.report["scored_by_laya"] is True
    assert len(scorer.calls) == 1
    assert len(scorer.calls[0]) <= 12
    assert set(outcome.report["timings"]) >= {"live_ms", "bm25_ms", "matrix_ms", "shortlist_ms", "laya_ms"}


def test_final_score_is_the_documented_blend(tmp_path):
    eng = engine(tmp_path, scorer=ScriptedScorer(probabilities={0: 1.0, 1: 0.0, 2: 0.0}))
    outcome = eng.search("mixed effects models", limit=3)
    top = outcome.rows[0]
    w_laya, w_retrieval = eng.weights
    assert top.final_score == pytest.approx(w_laya * top.laya_probability + w_retrieval * top.retrieval_score, abs=1e-9)


def test_scorer_failure_degrades_to_retrieval_order(tmp_path):
    eng = engine(tmp_path, scorer=ScriptedScorer(error=ScorerUnavailable("unreachable", "boom")))
    outcome = eng.search("mixed effects models", limit=3)
    assert outcome.ok
    assert "S5" in outcome.degraded
    assert all(row.scored_by_laya is False for row in outcome.rows)
    assert all(row.score_label == "retrieval-only" for row in outcome.rows)
    assert outcome.report["scored_by_laya"] is False


def test_failing_source_is_recorded_not_fatal(tmp_path):
    adapters = [FakeAdapter("arxiv", [], error=ScorerUnavailable("timeout"))]
    eng = engine(tmp_path, adapters=adapters)
    outcome = eng.search("mixed effects models", limit=3)
    assert outcome.ok
    assert "S4" in outcome.degraded
    assert outcome.report["sources"]["failed"]


def test_live_documents_are_ingested_and_searchable(tmp_path):
    fresh = SourceDoc("crossref", "10.1/new", "Spatial mixed models for ecology", "New row.", "", {})
    eng = engine(tmp_path, adapters=[FakeAdapter("crossref", [fresh])])
    outcome = eng.search("spatial mixed models ecology", limit=5)
    sources = {row.source for row in outcome.rows}
    assert "crossref" in sources
    assert eng.store.doc_id_for("crossref", "10.1/new") is not None


def test_second_identical_query_comes_from_cache(tmp_path):
    scorer = ScriptedScorer(winner=1)
    eng = engine(tmp_path, scorer=scorer)
    first = eng.search("mixed effects models", limit=3)
    second = eng.search("mixed effects models", limit=3)
    assert first.report["cached"] is False
    assert second.report["cached"] is True
    assert all(row.from_cache for row in second.rows)
    assert [r.doc_id for r in first.rows] == [r.doc_id for r in second.rows]
    assert len(scorer.calls) == 1, "a cached query must not spend another laya call"


def test_cache_can_be_disabled(tmp_path):
    scorer = ScriptedScorer(winner=1)
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(DOCS)
    eng = SearchEngine(store, scorer, adapters=[], use_cache=False, gate_margin=0.0)
    eng.search("mixed effects models", limit=3)
    eng.search("mixed effects models", limit=3)
    assert len(scorer.calls) == 2


def test_empty_corpus_returns_no_rows_with_a_reason(tmp_path):
    store = DocumentStore(tmp_path / "empty.db")
    eng = SearchEngine(store, ScriptedScorer(), adapters=[])
    outcome = eng.search("nothing here", limit=5)
    assert outcome.rows == [] and outcome.ok is False
    assert outcome.report["reason"] == "no candidates"
    assert "S1" in outcome.degraded


def test_shortlist_cuts_before_calling_laya(tmp_path):
    store = DocumentStore(tmp_path / "many.db")
    store.upsert_many([doc("cran", f"p{i}", f"mixed effects package {i}", "model fitting") for i in range(40)])
    scorer = ScriptedScorer()
    eng = SearchEngine(store, scorer, adapters=[], shortlist=12, prefilter=40)
    eng.search("mixed effects package", limit=5)
    assert len(scorer.calls[0]) == 12


def test_matrix_prefilter_finds_a_document_bm25_misses(tmp_path):
    store = DocumentStore(tmp_path / "m.db")
    store.upsert_many([doc("cran", "nlme", "Nonlinear longitudinal modelling toolkit", "unrelated words", "r-package")])
    doc_id = store.doc_id_for("cran", "nlme")
    matrix_path = tmp_path / "scores.trk"
    with MatrixWriter(matrix_path) as writer:
        writer.add(doc_id, feature_hash_signature("Nonlinear longitudinal modelling toolkit"), 0.0, 0)

    eng = SearchEngine(store, ScriptedScorer(), adapters=[], matrix_path=matrix_path)
    outcome = eng.search("Nonlinear longitudinal modelling toolkit", limit=3)
    assert outcome.rows, "matrix prefilter should surface a row even when BM25 has no match"
    assert outcome.rows[0].key == "nlme"


def test_corrupt_matrix_degrades_without_breaking_search(tmp_path):
    store = DocumentStore(tmp_path / "c.db")
    store.upsert_many(DOCS)
    matrix_path = tmp_path / "broken.trk"
    matrix_path.write_bytes(b"TRKMAT\x01\x00" + b"\x00" * 10)
    eng = SearchEngine(store, ScriptedScorer(), adapters=[], matrix_path=matrix_path)
    outcome = eng.search("mixed effects models", limit=3)
    assert outcome.ok
    assert "S2" in outcome.degraded


def test_health_reports_store_and_matrix_state(tmp_path):
    eng = engine(tmp_path)
    info = eng.health()
    assert info["store"]["docs"] == 3
    assert info["scorer"] == "scripted"
    assert info["matrix"] is None


def test_empty_query_is_rejected(tmp_path):
    from trick.errors import TrickError

    eng = engine(tmp_path)
    with pytest.raises(TrickError):
        eng.search("   ")


def test_missing_matrix_is_not_a_degradation(tmp_path):
    """A fresh install has no matrix yet; that is normal, not a failure."""
    eng = engine(tmp_path)
    outcome = eng.search("mixed effects models", limit=3)
    assert outcome.ok
    assert outcome.degraded == [], outcome.degraded


def test_gate_skips_the_scorer_when_retrieval_is_decisive(tmp_path):
    store = DocumentStore(tmp_path / "gate.db")
    store.upsert_many(
        [
            doc("cran", "lme4", "lme4: Linear Mixed-Effects Models with Eigen", "mixed effects model fitting"),
            doc("cran", "zzz", "Linear models notes", "a short note about linear models"),
        ]
    )
    scorer = ScriptedScorer(winner=1)
    eng = SearchEngine(store, scorer, adapters=[], gate_margin=0.15)
    outcome = eng.search("linear mixed effects models fitting", limit=2)
    assert outcome.report["gated"] is True
    assert outcome.report["scored_by_laya"] is False
    assert scorer.calls == [], "a gated query must not spend a laya call"
    assert outcome.rows[0].key == "lme4"


def test_gate_opens_when_the_top_two_are_close(tmp_path):
    store = DocumentStore(tmp_path / "gate2.db")
    store.upsert_many(
        [
            doc("cran", "a1", "Mixed effects model toolkit alpha", "mixed effects model"),
            doc("cran", "a2", "Mixed effects model toolkit beta", "mixed effects model"),
        ]
    )
    scorer = ScriptedScorer(winner=0)
    eng = SearchEngine(store, scorer, adapters=[], gate_margin=0.15)
    outcome = eng.search("mixed effects model", limit=2)
    assert outcome.report["gated"] is False
    assert outcome.report["scored_by_laya"] is True
    assert scorer.calls
