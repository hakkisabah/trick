"""Per-source quota: membership bounds, not a re-ordering.

Two claims are tested here, and they are separate:

* the pure function: reservation is honoured, an unfilled reservation is handed
  back, a live source cannot exceed its cap, and the ranking order survives;
* the engine: with a live adapter that genuinely out-scores the local catalogue,
  a local entry still reaches the shortlist, and the scorer still sees it.

The second one is the regression that motivated the whole mechanism - a paper
abstract repeats the query words, a package description does not, so on a real
query the local catalogue could be evicted entirely.
"""

from __future__ import annotations

from trick.laya import Ranked
from trick.retrieval import SourceDoc
from trick.search import LOCAL_SOURCES, SearchEngine, apply_source_quota
from trick.textindex import DocumentStore

LOCAL = {"cran": 1, "bioconductor": 2}
LIVE = {"arxiv": 3, "openalex": 4, "crossref": 5}


def _sources(mapping: dict[int, str]) -> dict[int, str]:
    return dict(mapping)


# -- the pure function -----------------------------------------------------


def test_no_quota_is_a_plain_prefix():
    ranked = [10, 11, 12, 13]
    sources = {10: "arxiv", 11: "arxiv", 12: "arxiv", 13: "arxiv"}
    assert apply_source_quota(ranked, 2, sources) == [10, 11]


def test_floor_reserves_local_slots_ahead_of_the_walk():
    ranked = [1, 2, 3, 4, 5, 6]
    sources = {1: "arxiv", 2: "arxiv", 3: "arxiv", 4: "arxiv", 5: "cran", 6: "bioconductor"}
    kept = apply_source_quota(ranked, 4, sources, local_floor=2, live_cap=0)
    assert set(kept) == {1, 2, 5, 6}
    # membership changed, order did not
    assert kept == [1, 2, 5, 6]


def test_unfilled_reservation_is_handed_back():
    ranked = [1, 2, 3]
    sources = {1: "arxiv", 2: "arxiv", 3: "arxiv"}
    kept = apply_source_quota(ranked, 3, sources, local_floor=4, live_cap=0)
    assert kept == [1, 2, 3]


def test_floor_cannot_exceed_the_shortlist_size():
    ranked = list(range(1, 10))
    sources = {doc_id: ("cran" if doc_id < 6 else "arxiv") for doc_id in ranked}
    kept = apply_source_quota(ranked, 3, sources, local_floor=99, live_cap=0)
    assert len(kept) == 3


def test_live_cap_bounds_one_source_but_not_the_others():
    ranked = [1, 2, 3, 4, 5, 6]
    sources = {1: "arxiv", 2: "arxiv", 3: "arxiv", 4: "arxiv", 5: "openalex", 6: "crossref"}
    kept = apply_source_quota(ranked, 4, sources, local_floor=0, live_cap=2)
    assert kept == [1, 2, 5, 6]


def test_local_sources_are_not_capped_so_the_catalogue_can_fill_the_list():
    ranked = [1, 2, 3, 4]
    sources = {1: "cran", 2: "cran", 3: "cran", 4: "cran"}
    kept = apply_source_quota(ranked, 4, sources, local_floor=2, live_cap=1)
    assert kept == [1, 2, 3, 4]


def test_cap_relaxes_when_the_strict_walk_cannot_fill_the_list():
    ranked = [1, 2, 3, 4, 5]
    sources = {1: "arxiv", 2: "arxiv", 3: "arxiv", 4: "arxiv", 5: "arxiv"}
    kept = apply_source_quota(ranked, 5, sources, local_floor=0, live_cap=2)
    assert kept == [1, 2, 3, 4, 5]


def test_empty_and_degenerate_inputs_do_not_raise():
    assert apply_source_quota([], 4, {}) == []
    assert apply_source_quota([1], 0, {1: "cran"}) == []
    assert apply_source_quota([1, 2], 99, {1: "cran", 2: "arxiv"}, local_floor=1, live_cap=1) == [1, 2]


def test_unknown_source_is_treated_as_live():
    ranked = [1, 2, 3]
    sources = {1: "unknown", 2: "unknown", 3: "cran"}
    kept = apply_source_quota(ranked, 2, sources, local_floor=1, live_cap=1)
    assert kept == [1, 3]


def test_local_sources_constant_matches_the_adapters():
    from trick.retrieval import SOURCE_PRIORITY

    for source in ("cran", "bioconductor"):
        assert source in LOCAL_SOURCES
        assert source in SOURCE_PRIORITY


# -- the engine ------------------------------------------------------------


class QuietScorer:
    """Records what it was asked to rank, answers uniformly."""

    name = "scripted"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def rank(self, query, candidates, instructions=""):
        self.calls.append(list(candidates))
        return [Ranked(i, 0.5) for i in range(len(candidates))]


class LoudAdapter:
    """A live adapter whose documents repeat the query verbatim."""

    def __init__(self, name: str, count: int, query: str) -> None:
        self.name = name
        self.count = count
        self.query = query

    def search(self, query, limit=5, timeout=1.5):
        return [
            SourceDoc(
                source=self.name,
                key=f"{self.name}-{index}",
                title=f"{self.name} paper {index} on {self.query}",
                summary=f"{self.query} {self.query} abstract text {index}",
                url=f"https://example.test/{self.name}/{index}",
            )
            for index in range(self.count)
        ]


def _store_with(db_path) -> DocumentStore:
    store = DocumentStore(db_path)
    store.upsert_many(
        [
            {
                "source": "cran",
                "key": "lme4",
                "title": "lme4: Linear Mixed-Effects Models",
                "summary": "Fit linear and generalized linear mixed-effects models.",
                "url": "https://example.test/cran/lme4",
                "meta": {"kind": "r-package"},
            }
        ]
    )
    return store


def _engine(store: DocumentStore, adapters, **kwargs) -> SearchEngine:
    return SearchEngine(
        store,
        QuietScorer(),
        adapters=adapters,
        use_cache=False,
        gate_margin=0.0,
        **kwargs,
    )


QUERY = "mixed effects model convergence warnings"


def test_engine_keeps_a_local_entry_when_live_sources_flood_the_query(tmp_path):
    store = _store_with(tmp_path / "q.db")
    adapters = [LoudAdapter("arxiv", 12, QUERY), LoudAdapter("crossref", 12, QUERY)]
    try:
        flooded = _engine(store, adapters, local_floor=0, live_cap=0).search(QUERY, limit=12)
        assert flooded.rows, "the query should match something"
        assert all(row.source != "cran" for row in flooded.rows), (
            "precondition: without a quota the live adapters win every slot"
        )

        guarded = _engine(store, adapters, local_floor=4, live_cap=4).search(QUERY, limit=12)
        sources = [row.source for row in guarded.rows]
        assert "cran" in sources, f"the local catalogue must survive the flood, got {sources}"
        assert len(set(sources)) >= 3, f"the shortlist must stay diverse, got {sources}"
        assert sources.count("arxiv") <= 8, f"one live source may not own the list, got {sources}"
        assert sources.count("crossref") <= 8, f"one live source may not own the list, got {sources}"
    finally:
        store.close()


def test_a_single_live_source_may_fill_the_list_when_nothing_else_answers(tmp_path):
    """The cap is a bound on competition, not a limit on the answer."""
    store = _store_with(tmp_path / "one.db")
    try:
        outcome = _engine(store, [LoudAdapter("arxiv", 12, QUERY)], local_floor=4, live_cap=4).search(
            QUERY, limit=12
        )
        assert len(outcome.rows) == 12
        assert "cran" in [row.source for row in outcome.rows]
    finally:
        store.close()


def test_engine_passes_the_reserved_local_entry_to_the_scorer(tmp_path):
    store = _store_with(tmp_path / "s.db")
    adapters = [LoudAdapter("arxiv", 12, QUERY)]
    scorer = QuietScorer()
    engine = SearchEngine(
        store, scorer, adapters=adapters, use_cache=False, gate_margin=0.0, local_floor=4, live_cap=4
    )
    try:
        engine.search(QUERY, limit=12)
        assert scorer.calls, "the scorer should have been called"
        assert any("mixed-effects" in text.lower() or "lme4" in text.lower() for text in scorer.calls[0])
    finally:
        store.close()


def test_health_reports_the_configured_quota(tmp_path):
    store = _store_with(tmp_path / "h.db")
    try:
        engine = _engine(store, [], local_floor=3, live_cap=5)
        assert engine.health()["source_quota"] == {"local_floor": 3, "live_cap": 5}
    finally:
        store.close()
