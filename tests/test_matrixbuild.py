"""The score matrix: built from the store, mapped lazily, used only on request.

Three contracts are checked here:

* a build covers every document and is honest about being placeholder-derived;
* a rebuild is idempotent, because ingest runs on a schedule;
* the matrix never reorders a healthy text result - it may only rescue an
  otherwise empty one, and only when the caller asked for that.
"""

from __future__ import annotations

import pytest

from trick.matrix import HEADER_SIZE, RECORD_SIZE, HF_SYNTHETIC, MatrixIndex
from trick.matrixbuild import build_from_store
from trick.quant import SCORE_MAX, decode_score
from trick.search import SearchEngine
from trick.textindex import DocumentStore

DOCS = [
    {
        "source": "cran",
        "key": "alpha",
        "title": "alpha: Fast aggregation of large tables",
        "summary": "Fast ordered joins and grouped aggregation without copies.",
    },
    {
        "source": "cran",
        "key": "beta",
        "title": "beta: Mixed effects models",
        "summary": "Fit linear mixed effects models with random slopes.",
    },
    {
        "source": "bioconductor",
        "key": "gamma",
        "title": "gamma: Differential expression",
        "summary": "Negative binomial generalised linear models for counts.",
    },
]


@pytest.fixture()
def store(tmp_path):
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(DOCS)
    try:
        yield store
    finally:
        store.close()


def test_build_covers_every_document(store, matrix_path):
    info = build_from_store(store, matrix_path)
    assert info["records"] == store.count() == 3
    assert info["bytes"] == HEADER_SIZE + RECORD_SIZE * 3
    assert info["synthetic"] is True
    with MatrixIndex(matrix_path) as index:
        assert index.count == 3
        # A reader must be able to tell the file came from the placeholder
        # encoder; presenting hashed signatures as embedding output would be a
        # quiet lie about ranking quality.
        assert index.synthetic is True


def test_build_stores_the_ingested_prior_as_the_record_score(store, matrix_path):
    store.set_weights([("cran", "alpha", 0.75, "downloads=1000")])
    build_from_store(store, matrix_path)
    alpha = store.doc_id_for("cran", "alpha")
    with MatrixIndex(matrix_path) as index:
        hits = {hit.record_id: hit for hit in index.scan(index_signature(index, alpha), k=8)}
        assert hits[alpha].score == pytest.approx(0.75, abs=0.01)


def index_signature(index: MatrixIndex, record_id: int) -> bytes:
    """Read one stored signature back, so a scan can be aimed at it."""
    body = index.body
    for offset in range(0, len(body), RECORD_SIZE):
        if int.from_bytes(body[offset : offset + 4], "little") == record_id:
            return bytes(body[offset + 4 : offset + 4 + 16])
    raise AssertionError(f"record {record_id} not in matrix")


def test_rebuild_is_idempotent(store, matrix_path):
    build_from_store(store, matrix_path)
    second = build_from_store(store, matrix_path)
    assert second["records"] == 3
    with MatrixIndex(matrix_path) as index:
        assert index.count == 3


def test_records_carry_the_prior_as_a_16_bit_score(store, matrix_path):
    store.set_weights([("cran", "beta", 1.0, "revdeps=99")])
    build_from_store(store, matrix_path)
    beta = store.doc_id_for("cran", "beta")
    with MatrixIndex(matrix_path) as index:
        for hit in index.scan(index_signature(index, beta), k=8):
            if hit.record_id == beta:
                assert hit.score == pytest.approx(1.0, abs=1.0 / SCORE_MAX)
                break
        else:  # pragma: no cover - a scan always returns what was put in
            raise AssertionError("beta missing from its own scan")
    assert decode_score(SCORE_MAX) == pytest.approx(1.0)


def test_matrix_does_not_reorder_a_healthy_text_result(store, matrix_path):
    build_from_store(store, matrix_path)
    without = SearchEngine(store, use_cache=False).search(
        "differential expression counts", limit=3, use_live=False, scope="sources"
    )
    with_matrix = SearchEngine(store, use_cache=False, matrix_path=matrix_path).search(
        "differential expression counts", limit=3, use_live=False, scope="sources"
    )
    assert [row.key for row in without.rows] == [row.key for row in with_matrix.rows]
    assert with_matrix.report["matrix_rescued"] == 0


def test_rescue_is_off_by_default_and_labelled_when_enabled(store, matrix_path):
    build_from_store(store, matrix_path)
    # "zulu" shares no token with any document, so the text index finds nothing.
    off = SearchEngine(store, use_cache=False, matrix_path=matrix_path).search(
        "zulu", limit=3, use_live=False, scope="sources"
    )
    assert off.rows == []

    on = SearchEngine(store, use_cache=False, matrix_path=matrix_path, matrix_rescue=True).search(
        "zulu", limit=3, use_live=False, scope="sources"
    )
    assert on.rows, "the rescue path exists to return something instead of nothing"
    assert on.report["matrix_rescued"] > 0
    # A rescued row is retrieval-only: it was never scored by the model.
    assert all(row.score_label == "retrieval-only" for row in on.rows)


def test_rescue_needs_a_matrix(store):
    engine = SearchEngine(store, use_cache=False, matrix_rescue=True)
    outcome = engine.search("zulu", limit=3, use_live=False, scope="sources")
    assert outcome.rows == []
    assert outcome.report.get("matrix_rescued", 0) == 0
