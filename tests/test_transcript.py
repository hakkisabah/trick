"""Conversation scope: the product's other half.

Catalogue search answers "which package is this". These tests cover the question
the user actually asked first: *where in this long chat did we say that*. The
tests drive the real index and the real pipeline, with a scripted scorer in place
of laya, so nothing here depends on the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trick.errors import ContractViolation, ResourceExhausted, ScorerUnavailable
from trick.laya import Ranked
from trick.search import SearchEngine
from trick.textindex import DocumentStore
from trick.transcript import (
    MAX_CHARS,
    MAX_MESSAGES,
    MIN_CHARS,
    ChatRow,
    ConversationSearch,
    Message,
    TranscriptCache,
    TranscriptIndex,
    chunk_messages,
    fingerprint,
    normalise_messages,
    snippet,
    split_message,
    window_messages,
)

TARGET = (
    "For the spatial join you should reproject both layers to EPSG:3857 with st_transform "
    "before calling st_join, otherwise the raster stack and the polygon layer disagree "
    "about their coordinate reference system."
)

FILLER = [
    "We started by loading the count matrix and checking the library sizes.",
    "The normalization step looked fine, so we moved on to the design formula.",
    "Then we discussed whether to keep the batch term in the model at all.",
    "I think the outlier sample should be dropped before the dispersion fit.",
    "Let us look at the diagnostics for the mean-variance trend once more.",
]


def long_chat(turns: int = 24) -> list[Message]:
    """A conversation where exactly one passage mentions the spatial join."""
    messages: list[Message] = []
    for index in range(turns):
        role = "user" if index % 2 == 0 else "assistant"
        body = FILLER[index % len(FILLER)]
        if index == 5:
            body = TARGET
        messages.append(Message(role=role, content=f"Turn {index}. {body} Extra context here."))
    return messages


class ScriptedScorer:
    """Gives the candidate containing the marker word the highest probability."""

    name = "scripted"

    def __init__(self, *, marker: str = "EPSG:3857", error: Exception | None = None) -> None:
        self.marker = marker
        self.error = error
        self.calls: list[list[str]] = []

    def rank(self, query, candidates, instructions=""):
        if self.error is not None:
            raise self.error
        self.calls.append(list(candidates))
        scores = []
        for text in candidates:
            scores.append(0.9 if self.marker in text else 0.02)
        total = sum(scores) or 1.0
        ranked = [Ranked(index, score / total) for index, score in enumerate(scores)]
        ranked.sort(key=lambda item: (-item.probability, item.index))
        return ranked


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------
def test_chunks_respect_the_size_contract_and_index_the_original_text() -> None:
    message = Message(role="user", content=TARGET + " " + TARGET)
    chunks = split_message(message, turn=3, next_id=0)
    assert chunks
    for chunk in chunks:
        assert MIN_CHARS <= len(chunk.text) <= MAX_CHARS
        # Offsets index the original message, so a UI can highlight the span.
        assert message.content[chunk.char_start : chunk.char_end].strip()
        assert chunk.text.split()[0] in message.content[chunk.char_start : chunk.char_end]
        assert chunk.turn == 3


def test_a_word_is_never_split_across_two_chunks() -> None:
    words = " ".join(f"token{index}" for index in range(400))
    chunks = split_message(Message(role="assistant", content=words), turn=0, next_id=0)
    assert len(chunks) > 1
    joined = " ".join(chunk.text for chunk in chunks)
    assert joined.split() == words.split()
    for chunk in chunks:
        assert not chunk.text.startswith("oken")


def test_empty_and_whitespace_messages_produce_no_chunks() -> None:
    assert chunk_messages([Message(role="user", content=""), Message(role="user", content="  \n ")]) == []


def test_turn_numbers_survive_chunking() -> None:
    messages = long_chat(6)
    chunks = chunk_messages(messages)
    assert {chunk.turn for chunk in chunks} == {1, 2, 3, 4, 5, 6}


# ---------------------------------------------------------------------------
# input contract (S8)
# ---------------------------------------------------------------------------
def test_normalise_accepts_strings_parts_and_none_content() -> None:
    messages = normalise_messages(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "part one"}, "part two"]},
            {"role": "system"},
            "a bare string is a user turn",
        ]
    )
    assert [message.role for message in messages] == ["user", "assistant", "system", "user"]
    assert messages[1].content == "part one\npart two"
    assert messages[2].content == ""
    assert messages[3].content.startswith("a bare string")


def test_normalise_rejects_a_message_that_is_not_an_object() -> None:
    with pytest.raises(ContractViolation) as caught:
        normalise_messages([{"role": "user", "content": "ok"}, 42])
    assert caught.value.s_code == "S8"


def test_normalise_rejects_an_unsupported_content_type() -> None:
    with pytest.raises(ContractViolation):
        normalise_messages([{"role": "user", "content": {"unexpected": True}}])


def test_too_many_messages_is_resource_exhaustion_not_silence() -> None:
    with pytest.raises(ResourceExhausted):
        normalise_messages([{"role": "user", "content": "x" * 10} for _ in range(MAX_MESSAGES + 1)])


def test_window_keeps_the_tail_and_reports_nothing_when_it_fits() -> None:
    messages = long_chat(10)
    assert window_messages(messages, tail=20) == messages
    assert len(window_messages(messages, tail=4)) == 4
    assert window_messages(messages, tail=4)[-1] == messages[-1]


def test_fingerprint_is_stable_and_distinguishes_two_chats() -> None:
    first = long_chat(8)
    same = [Message(role=m.role, content=m.content) for m in first]
    other = long_chat(8)
    other[0] = Message(role="user", content="a completely different opening line")
    assert fingerprint(first) == fingerprint(same)
    assert fingerprint(first) != fingerprint(other)


# ---------------------------------------------------------------------------
# retrieval inside one transcript
# ---------------------------------------------------------------------------
def test_index_finds_the_one_passage_that_mentions_the_term() -> None:
    index = TranscriptIndex(long_chat())
    hits = index.bm25("EPSG 3857 projection")
    assert hits
    best = max(hits, key=lambda item: item[1])[0]
    chunk = next(chunk for chunk in index.chunks if chunk.chunk_id == best)
    assert chunk.turn == 6
    assert "EPSG:3857" in chunk.text


def test_no_lexical_match_is_an_empty_answer_not_an_error() -> None:
    index = TranscriptIndex(long_chat())
    assert index.bm25("quantumchromodynamics") == []


def test_an_empty_transcript_has_no_index_and_no_rows() -> None:
    search = ConversationSearch(ScriptedScorer())
    rows, report = search.search("anything", [])
    assert rows == []
    assert report["chunk_count"] == 0


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------
def test_the_buried_passage_reaches_rank_one_with_turn_and_offsets() -> None:
    scorer = ScriptedScorer()
    search = ConversationSearch(scorer, gate_margin=0.0)
    rows, report = search.search("spatial join reproject layers", long_chat(), limit=5)
    assert rows
    # Exactly one passage shares a query term with the query now that hash-only
    # candidates are gone, so there is nothing for the model to break a tie on:
    # it must not be called, and the row says so instead of faking a score.
    assert len(rows) == 1
    assert rows[0].turn == 6
    assert "EPSG:3857" in rows[0].text
    assert rows[0].scored_by_laya is False
    assert report["scored_by_laya"] is False
    assert scorer.calls == []
    assert rows[0].char_end > rows[0].char_start
    assert rows[0].retrieval_score > 0.0
    # The reported score is the documented blend, not an unexplained number.
    model_weight, retrieval_weight = search.weights
    if rows[0].laya_probability is not None:
        expected = retrieval_weight * rows[0].retrieval_score + model_weight * rows[0].laya_probability
        assert rows[0].final_score == pytest.approx(expected, rel=1e-9)


def tie_chat() -> list[Message]:
    """Two passages with identical terms, so retrieval alone cannot separate them.

    This is the case the model exists for: a 12-way choice over candidates the
    lexical stage scored the same. Without laya the order would come from the
    chunk id, which is an accident of message length.
    """
    messages = [
        Message(role="user", content=f"Turn {index}. We keep talking about the pipeline and the matrix.")
        for index in range(4)
    ]
    messages.append(Message(role="user", content="The spatial join decision is still open for the polygon layer."))
    messages.extend(
        Message(role="assistant", content=f"Turn {index}. Notes about the pipeline and the matrix.")
        for index in range(5, 9)
    )
    messages.append(
        Message(role="assistant", content="The spatial join decision is still open for the polygon layer!")
    )
    return messages


def test_the_model_breaks_a_retrieval_tie() -> None:
    query = "spatial join decision polygon layer"
    decided = ConversationSearch(ScriptedScorer(marker="polygon layer!"), gate_margin=0.0)
    rows, report = decided.search(query, tie_chat(), limit=4)
    assert report["scored_by_laya"] is True
    assert rows[0].turn == 10

    undecided = ConversationSearch(ScriptedScorer(error=ScorerUnavailable("down")), gate_margin=0.0)
    plain, _ = undecided.search(query, tie_chat(), limit=4)
    # Same retrieval evidence, different order: that difference is the model.
    assert sorted(round(row.retrieval_score, 6) for row in plain) == sorted(
        round(row.retrieval_score, 6) for row in rows
    )
    assert plain[0].turn == 5
    assert plain[0].final_score == pytest.approx(plain[1].final_score, rel=1e-9)


def test_the_shortlist_never_exceeds_the_choice_contract() -> None:
    scorer = ScriptedScorer(marker="Extra context")
    search = ConversationSearch(scorer, shortlist=12, gate_margin=0.0)
    search.search("context here", long_chat(60), limit=8)
    assert len(scorer.calls[0]) <= 12


def test_a_clear_retrieval_winner_skips_the_model_call() -> None:
    scorer = ScriptedScorer()
    search = ConversationSearch(scorer, gate_margin=0.15)
    # One term from another passage (dispersion) gives two candidates, so the
    # margin rule is what skips the model - not the single-candidate shortcut.
    _rows, report = search.search("EPSG:3857 reproject layers dispersion", long_chat(), limit=5)
    assert report["gated"] is True
    assert scorer.calls == []


def test_scorer_failure_degrades_to_retrieval_only_with_s5() -> None:
    scorer = ScriptedScorer(error=ScorerUnavailable("laya is down"))
    search = ConversationSearch(scorer, gate_margin=0.0)
    rows, report = search.search("dispersion normalization library", long_chat(), limit=5)
    assert rows
    assert rows[0].scored_by_laya is False
    assert report["degraded"] == ["S5"]
    assert report["scored_by_laya"] is False


def test_rows_are_labelled_by_how_they_were_scored() -> None:
    search = ConversationSearch(ScriptedScorer(error=ScorerUnavailable("down")), gate_margin=0.0)
    rows, _ = search.search("spatial join", long_chat(), limit=3)
    assert all(row.score_label == "retrieval-only" for row in rows)
    payload = rows[0].as_dict()
    assert payload["score_label"] == "retrieval-only"
    assert ChatRow.from_dict(1, payload).turn == rows[0].turn


def test_the_cache_reuses_an_index_but_never_mixes_two_chats() -> None:
    cache = TranscriptCache(capacity=2)
    first = long_chat(8)
    other = [Message(role="user", content="an unrelated conversation about compiler flags")]
    a = cache.get(first)
    again = cache.get([Message(role=m.role, content=m.content) for m in first])
    b = cache.get(other)
    assert a is again
    assert a.key != b.key
    assert len(cache) == 2
    cache.clear()
    assert len(cache) == 0


def test_snippet_windows_around_the_matching_term() -> None:
    text = "start " + "filler " * 60 + "the EPSG:3857 answer " + "tail " * 60
    shown = snippet(text, "EPSG:3857", width=80)
    assert "EPSG:3857" in shown
    assert len(shown) <= 82


# ---------------------------------------------------------------------------
# the engine: scopes, windowing and the decision cache
# ---------------------------------------------------------------------------
DOCS = [
    {
        "source": "cran",
        "key": "sf",
        "title": "sf: Simple Features for R",
        "summary": "Spatial data frames, joins and coordinate reference systems.",
        "url": "https://example.test/cran/sf",
        "meta": {},
    },
    {
        "source": "bioconductor",
        "key": "DESeq2",
        "title": "DESeq2: Differential expression of count data",
        "summary": "RNA-seq counts and dispersion estimation.",
        "url": "https://example.test/bioc/DESeq2",
        "meta": {},
    },
]


def make_engine(tmp_path: Path, *, use_cache: bool = False, scorer=None) -> SearchEngine:
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(DOCS)
    engine = SearchEngine(store, scorer or ScriptedScorer(), use_cache=use_cache)
    engine.conversation.gate_margin = 0.0
    return engine


def test_engine_returns_both_blocks_when_a_transcript_is_sent(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    try:
        outcome = engine.search("spatial join", messages=[m.as_dict() for m in long_chat()])
    finally:
        engine.store.close()
    assert outcome.scope == "both"
    assert outcome.chat and outcome.chat[0].turn == 6
    assert outcome.rows and outcome.rows[0].key == "sf"
    assert outcome.ok is True
    payload = outcome.as_dict()
    assert payload["chat"][0]["turn"] == 6
    assert payload["scope"] == "both"


def test_a_transcript_with_sources_scope_is_promoted_not_ignored(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    try:
        outcome = engine.search(
            "spatial join",
            messages=[m.as_dict() for m in long_chat()],
            scope="sources",
        )
    finally:
        engine.store.close()
    assert outcome.scope == "both"
    assert outcome.chat


def test_chat_scope_returns_no_catalogue_rows(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    try:
        outcome = engine.search(
            "spatial join",
            messages=[m.as_dict() for m in long_chat()],
            scope="chat",
        )
    finally:
        engine.store.close()
    assert outcome.scope == "chat"
    assert outcome.chat
    assert outcome.rows == []


def test_chat_scope_without_a_transcript_falls_back_to_sources(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    try:
        outcome = engine.search("spatial join", scope="chat")
    finally:
        engine.store.close()
    assert outcome.scope == "sources"
    assert outcome.chat == []


def test_unknown_scope_is_a_contract_violation(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    try:
        with pytest.raises(ContractViolation):
            engine.search("spatial join", messages=[{"role": "user", "content": "x"}], scope="everything")
    finally:
        engine.store.close()


def test_a_long_chat_is_windowed_and_says_so(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.chat_window = 6
    messages = [m.as_dict() for m in long_chat(30)]
    try:
        outcome = engine.search("spatial join", messages=messages)
    finally:
        engine.store.close()
    assert outcome.chat_report["dropped_turns"] == 24
    # The passage at turn 5 is outside the window, so it must not be found.
    assert all(row.turn >= 25 for row in outcome.chat)


def test_the_cache_never_crosses_two_conversations(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, use_cache=True)
    first = [m.as_dict() for m in long_chat()]
    second = [{"role": "user", "content": "completely unrelated talk about compiler flags"}]
    try:
        one = engine.search("spatial join", messages=first)
        two = engine.search("spatial join", messages=second)
        three = engine.search("spatial join", messages=first)
    finally:
        engine.store.close()
    assert one.chat and one.chat[0].turn == 6
    assert two.chat == []
    assert three.chat and three.chat[0].turn == 6
    assert three.report.get("cached") is True


def test_engine_json_round_trip_keeps_chat_rows(tmp_path: Path) -> None:
    from trick.search import SearchOutcome

    engine = make_engine(tmp_path)
    try:
        outcome = engine.search("spatial join", messages=[m.as_dict() for m in long_chat()])
    finally:
        engine.store.close()
    restored = SearchOutcome.from_dict(json.loads(json.dumps(outcome.as_dict())))
    assert restored.scope == "both"
    assert restored.chat[0].turn == outcome.chat[0].turn
    assert restored.chat[0].text == outcome.chat[0].text


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def test_renderer_shows_the_conversation_block_before_the_sources(tmp_path: Path) -> None:
    from trick.render import render_compact, render_outcome

    engine = make_engine(tmp_path)
    try:
        outcome = engine.search("spatial join", messages=[m.as_dict() for m in long_chat()])
    finally:
        engine.store.close()
    text = render_outcome(outcome, verbose=True)
    assert "conversation:" in text
    assert "turn 6" in text
    assert text.index("conversation:") < text.index("sf: Simple Features")
    compact = render_compact(outcome)
    assert "conversation matches" in compact
    assert "turn 6" in compact


def test_renderer_says_no_sources_when_only_the_chat_matched(tmp_path: Path) -> None:
    from trick.render import render_outcome

    engine = make_engine(tmp_path)
    try:
        outcome = engine.search(
            "spatial join reproject",
            messages=[m.as_dict() for m in long_chat()],
            scope="chat",
        )
    finally:
        engine.store.close()
    text = render_outcome(outcome)
    assert "no sources" in text


def test_identical_passages_collapse_to_the_best_occurrence() -> None:
    """A chat repeats itself; the same sentence must not fill the result list."""
    repeated = "The spatial join decision is still open for the polygon layer."
    messages = [Message(role="user", content=repeated) for _ in range(3)]
    messages.append(
        Message(role="assistant", content="A different note about spatial indices and the projection matrix.")
    )
    search = ConversationSearch(ScriptedScorer(marker="polygon"), gate_margin=0.0)
    rows, report = search.search("spatial join polygon layer", messages, limit=5)
    texts = [" ".join(row.text.lower().split()) for row in rows]
    assert len(texts) == len(set(texts))
    assert len(rows) == 2
    assert report["collapsed_duplicates"] >= 1
    assert [row.rank for row in rows] == [1, 2]


def test_a_cache_entry_from_an_older_pipeline_version_is_not_replayed(tmp_path: Path) -> None:
    """An upgrade must not serve yesterday's ranking under today's field names."""
    engine = make_engine(tmp_path, use_cache=True)
    try:
        engine.store.cache_put("spatial join", engine.scorer.name, {"rows": [], "degraded": []}, extra="")
        outcome = engine.search("spatial join", messages=[m.as_dict() for m in long_chat()])
    finally:
        engine.store.close()
    assert outcome.report.get("cached") is not True
    assert outcome.chat


def test_tuning_a_ranking_constant_invalidates_the_decision_cache(tmp_path: Path) -> None:
    """A cached answer computed under other weights must not be replayed."""
    fresh = make_engine(tmp_path, use_cache=True)
    try:
        first = fresh.search("spatial join")
        second = fresh.search("spatial join")
        tuned = SearchEngine(fresh.store, ScriptedScorer(), use_cache=True, weights=(0.3, 0.7))
        third = tuned.search("spatial join")
    finally:
        fresh.store.close()
    assert first.report.get("cached") is not True
    assert second.report.get("cached") is True
    assert tuned.pipeline_key != fresh.pipeline_key
    assert third.report.get("cached") is not True


def test_a_hash_only_candidate_is_not_a_match(monkeypatch) -> None:
    """The bug a reader reported: "find this phrase" returned a turn about
    something else. A 16-byte signature cosine can pass 0.25 on unrelated text,
    so a signature hit now needs a shared query term as well."""
    index = TranscriptIndex(long_chat())
    # Force the signature channel to claim a perfect match; without the lexical
    # gate every passage would become a candidate.
    monkeypatch.setattr("trick.transcript.sig_cosine", lambda *_args, **_kwargs: 0.99)
    assert index.candidates("xgboost forecasting quantile") == {}

    # The same forced signature, with one real term, still contributes.
    hits = index.candidates("dispersion")
    assert hits, "lexical hits must survive the gate"


def test_every_row_says_which_query_terms_it_contains() -> None:
    """A hit that shares one rare word with the question read as random, so a
    row now carries the terms it actually contains. The list is meant to explain
    a weak match, not to flatter it: one shared term is reported as one."""
    search = ConversationSearch(ScriptedScorer(), gate_margin=0.0)
    rows, _report = search.search("spatial join reproject layers", long_chat(), limit=5)
    assert rows
    whole_chat = " ".join(str(item.content).lower() for item in long_chat())
    for row in rows:
        assert row.matched, "a row without matched terms cannot explain itself"
        for term in row.matched:
            assert term in whole_chat, term
    # The reported terms are a subset of what the query asked for.
    assert set(rows[0].matched) <= {"spatial", "join", "reproject", "layers"}


def test_matched_terms_survive_the_wire_format() -> None:
    """The action renders what the bridge sent, so the field has to round-trip
    through as_dict/from_dict; a field added to the dataclass alone is invisible
    over HTTP and was the shape of an earlier bug."""
    row = ChatRow(
        rank=1,
        turn=3,
        role="assistant",
        text="the threshold was 0.15",
        char_start=0,
        char_end=25,
        retrieval_score=1.0,
        laya_probability=None,
        final_score=1.0,
        scored_by_laya=False,
        mid="a-2",
        matched=("threshold", "0.15"),
    )
    payload = row.as_dict()
    assert payload["matched"] == ["threshold", "0.15"]
    restored = ChatRow.from_dict(1, payload)
    assert restored.matched == ("threshold", "0.15")
    # An answer from an older bridge has no such field and must not break.
    payload.pop("matched")
    assert ChatRow.from_dict(1, payload).matched == ()
