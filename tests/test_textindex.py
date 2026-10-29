"""Document store: BM25 ordering, shortlist rescoring, cache expiry, fallback."""

from __future__ import annotations

import sqlite3

import pytest

from trick.errors import ContractViolation
from trick.textindex import DocumentStore, _fts_expression, query_hash


def make_store(tmp_path, name="trick.db") -> DocumentStore:
    return DocumentStore(tmp_path / name)


DOCS = [
    {
        "source": "cran",
        "key": "lme4",
        "title": "lme4: Linear Mixed-Effects Models using Eigen and S4",
        "summary": "Fit linear and generalized linear mixed-effects models.",
        "url": "https://CRAN.R-project.org/package=lme4",
        "meta": {"kind": "r-package"},
    },
    {
        "source": "bioconductor",
        "key": "DESeq2",
        "title": "DESeq2: Differential expression analysis of count data",
        "summary": "Negative binomial models for RNA-seq counts.",
        "url": "https://bioconductor.org/packages/DESeq2",
    },
    {
        "source": "arxiv",
        "key": "2101.00001",
        "title": "A survey of mixed-effects modelling in genomics",
        "summary": "We review random effects and shrinkage in genomic studies.",
        "url": "https://arxiv.org/abs/2101.00001",
    },
]


def test_upsert_counts_and_dedupes_by_source_key(tmp_path):
    with make_store(tmp_path) as store:
        assert store.upsert_many(DOCS) == 3
        assert store.upsert_many([DOCS[0]]) == 1
        assert store.count() == 3
        doc_id = store.doc_id_for("cran", "lme4")
        assert doc_id is not None
        assert store.get(doc_id).title.startswith("lme4")


def test_document_without_source_key_or_title_is_rejected(tmp_path):
    with make_store(tmp_path) as store:
        with pytest.raises(ContractViolation):
            store.upsert_many([{"source": "cran", "key": "x"}])


def test_bm25_ranks_the_on_topic_document_first(tmp_path):
    with make_store(tmp_path) as store:
        store.upsert_many(DOCS)
        ranked = store.bm25("mixed effects model", limit=10)
        assert ranked, "expected matches"
        top = store.get(ranked[0][0])
        assert "Mixed-Effects" in top.title or "mixed-effects" in top.title
        scores = [score for _, score in ranked]
        assert scores[0] == pytest.approx(1.0), "best hit must normalise to 1.0"
        assert scores == sorted(scores, reverse=True)


def test_rescore_restricts_to_the_shortlist(tmp_path):
    with make_store(tmp_path) as store:
        store.upsert_many(DOCS)
        only = store.doc_id_for("bioconductor", "DESeq2")
        hits = store.rescore("count data", [only])
        assert list(hits) == [only], "rows outside the shortlist must not appear"
        assert store.rescore("mixed effects model", [only]) == {}, "shortlist must not leak other rows"


def test_bm25_on_stopword_only_query_returns_nothing(tmp_path):
    with make_store(tmp_path) as store:
        store.upsert_many(DOCS)
        assert store.bm25("a", limit=5) == []


def test_fts_expression_is_quoted_and_deduplicated():
    expression = _fts_expression('mixed "effects" effects AND 1')
    assert expression == '"mixed" OR "effects" OR "and"'
    assert _fts_expression("   ") == ""


def test_query_hash_is_stable_and_normalised():
    assert query_hash("Mixed  Effects") == query_hash("mixed effects")
    assert query_hash("mixed effects", extra="12") != query_hash("mixed effects")


def test_cache_roundtrip_and_expiry(tmp_path):
    with make_store(tmp_path) as store:
        store.cache_put("mixed effects", "laya-remote", {"rows": [{"doc_id": 1}]}, now=1000)
        hit = store.cache_get("mixed effects", "laya-remote", now=1000)
        assert hit == {"rows": [{"doc_id": 1}]}
        assert store.cache_get("mixed effects", "other-scorer", now=1000) is None
        expired = store.cache_get("mixed effects", "laya-remote", now=1000 + store.cache_ttl_s + 1)
        assert expired is None


def test_prune_removes_old_decisions_and_documents(tmp_path):
    with make_store(tmp_path) as store:
        store.upsert_many(DOCS, now=1000)
        store.cache_put("old query", "laya-remote", {"rows": []}, now=1000)
        removed = store.prune(now=1000 + 40 * 86400, keep_days=30)
        assert removed == 4
        assert store.count() == 0


def test_missing_fts5_degrades_to_like_search(tmp_path, monkeypatch):
    """A SQLite build without FTS5 must still answer, only without BM25."""
    store = make_store(tmp_path)
    store.upsert_many(DOCS)
    ids = [store.doc_id_for("cran", "lme4")]
    store.fts5 = False
    hits = store.rescore("mixed effects", ids)
    assert list(hits) == ids, "LIKE fallback must still find the document"


def test_corrupt_database_file_is_storage_io(tmp_path):
    path = tmp_path / "broken.db"
    path.write_bytes(b"not a sqlite database at all")
    with pytest.raises(Exception) as info:
        DocumentStore(path)
    assert "S6" == getattr(info.value, "s_code", "S6")


def test_triggers_keep_the_fts_mirror_in_sync(tmp_path):
    with make_store(tmp_path) as store:
        store.upsert_many(DOCS)
        before = store.bm25("genomics", limit=5)
        assert before
        store.upsert_many(
            [{"source": "arxiv", "key": "2101.00001", "title": "Renamed work", "summary": "unrelated"}]
        )
        after = store.bm25("genomics", limit=5)
        assert after == [], "the old title must leave the index after an update"


def test_wal_mode_is_enabled(tmp_path):
    with make_store(tmp_path) as store:
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"


def test_schema_is_idempotent_across_reopen(tmp_path):
    with make_store(tmp_path) as store:
        store.upsert_many(DOCS)
    with make_store(tmp_path) as store:
        assert store.count() == 3
        assert store.conn.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0] == 3
