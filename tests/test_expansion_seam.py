"""The query-expansion seam: optional, and never allowed to break a search.

Expansion itself was measured and rejected (see scripts/eval_expansion.py and the
numbers in README); what remains is the seam it was measured through. A rewriter
is optional, its failures fall back to the plain expression, and a different
rewriter never answers from another one's cached payload.
"""

from __future__ import annotations

import pytest

from trick.search import SearchEngine
from trick.textindex import DocumentStore

DOCS = [
    {"source": "cran", "key": "alpha", "title": "alpha: Fast aggregation", "summary": "ordered joins"},
    {"source": "cran", "key": "beta", "title": "beta: Mixed effects models", "summary": "random slopes"},
]


@pytest.fixture()
def store(tmp_path):
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(DOCS)
    try:
        yield store
    finally:
        store.close()


def test_rewriter_can_widen_the_expression(store):
    engine = SearchEngine(store, use_cache=False, expand=lambda q: '"beta"')
    outcome = engine.search("zulu", limit=2, use_live=False, scope="sources")
    assert [row.key for row in outcome.rows] == ["beta"]


def test_rewriter_that_raises_falls_back_to_the_plain_expression(store):
    def broken(_query: str) -> str:
        raise RuntimeError("a rewriter must not be able to kill a search")

    engine = SearchEngine(store, use_cache=False, expand=broken)
    outcome = engine.search("mixed effects", limit=2, use_live=False, scope="sources")
    assert [row.key for row in outcome.rows] == ["beta"]


def test_rewriter_that_returns_nothing_falls_back(store):
    engine = SearchEngine(store, use_cache=False, expand=lambda _q: "")
    outcome = engine.search("aggregation", limit=2, use_live=False, scope="sources")
    assert [row.key for row in outcome.rows] == ["alpha"]


def test_expansion_is_part_of_the_cache_identity(store):
    plain = SearchEngine(store, use_cache=False)
    again = SearchEngine(store, use_cache=False)
    widened = SearchEngine(store, use_cache=False, expand=lambda _q: '"beta"')
    assert plain.pipeline_key == again.pipeline_key, "same settings must share a cache"
    assert plain.pipeline_key != widened.pipeline_key, "a rewriter must not inherit the plain cache"
