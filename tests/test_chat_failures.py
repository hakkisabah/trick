"""Two failures seen in a real Open WebUI chat, each locked down here.

1. ``split_message`` handed out the same ``chunk_id`` twice when one turn held
   both short and over-long sentences. ``chunk_id`` is the FTS5 ``rowid``, so the
   insert raised "constraint failed" and the whole conversation search died with
   S3 - on a normal 45-turn chat, every single query.
2. A failed chat stage was *cached*. After the bug above was fixed the answer did
   not change, because the stored payload kept replaying the old S3 error for
   that query in that conversation: a transient fault became permanent.
"""

from __future__ import annotations

import json
from pathlib import Path

from trick.errors import ResourceExhausted
from trick.search import SearchEngine
from trick.textindex import DocumentStore
from trick.transcript import TranscriptIndex, chunk_messages, normalise_messages


def _long_chat() -> list[dict]:
    """Short turns, plus one turn with a paragraph and one unbroken sentence."""
    turns = [
        {"role": "user", "content": "where did we take the wrong turn"},
        {"role": "assistant", "content": "the threshold we picked was too small. " * 12},
        {"role": "assistant", "content": " ".join(["word"] * 400)},
        {"role": "user", "content": "noted, moving on"},
    ]
    clean = []
    for position in range(6):
        clean.append({"role": "user", "content": f"filler turn {position} about plots"})
        clean.append({"role": "assistant", "content": f"answer {position} about plots"})
    return turns + clean


def test_chunk_ids_are_unique_when_one_turn_needs_splitting() -> None:
    messages = normalise_messages(_long_chat())
    chunks = chunk_messages(messages)
    ids = [chunk.chunk_id for chunk in chunks]

    assert len(ids) > len(messages), "the long turns must actually split"
    assert len(ids) == len(set(ids)), "duplicate chunk_id means a duplicate FTS rowid"


def test_the_index_builds_for_a_chat_with_a_long_turn() -> None:
    """The regression itself: this raised S3 'constraint failed'."""
    messages = normalise_messages(_long_chat())

    index = TranscriptIndex(messages)

    assert index.count == len(chunk_messages(messages))
    assert index.candidates("threshold", limit=5), "a long chat must still be searchable"


def test_a_failed_chat_stage_is_never_cached(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(
        [
            {
                "source": "cran",
                "key": "ggplot2",
                "title": "ggplot2: Create Elegant Data Visualisations",
                "summary": "threshold plots",
                "url": "https://cran.r-project.org/package=ggplot2",
                "text": "ggplot2 threshold plots",
            }
        ]
    )
    engine = SearchEngine(store, _FlatScorer(), use_cache=True)
    messages = _long_chat()
    calls = {"n": 0}
    real = engine.conversation.search

    def flaky(query, transcript, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ResourceExhausted("cannot build the transcript index: constraint failed")
        return real(query, transcript, **kwargs)

    engine.conversation.search = flaky  # type: ignore[method-assign]

    first = engine.search("threshold", messages=messages, scope="both")
    assert "S3" in first.degraded
    assert first.chat == []

    second = engine.search("threshold", messages=messages, scope="both")

    assert "S3" not in second.degraded, "the old failure was replayed from the cache"
    assert second.chat, "the second attempt must search the conversation for real"


def test_a_cached_payload_that_reports_a_chat_error_is_recomputed(tmp_path: Path) -> None:
    """Belt and braces: payloads written by the buggy release are still on disk."""
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(
        [
            {
                "source": "cran",
                "key": "lme4",
                "title": "lme4: Linear Mixed-Effects Models",
                "summary": "threshold models",
                "url": "https://cran.r-project.org/package=lme4",
                "text": "lme4 threshold models",
            }
        ]
    )
    engine = SearchEngine(store, _FlatScorer(), use_cache=True)
    messages = _long_chat()

    outcome = engine.search("threshold", messages=messages, scope="both")
    extra = f"{engine.pipeline_key}:{_fingerprint(messages)}"
    payload = store.cache_get("threshold", engine.scorer.name, extra=extra)
    assert payload is not None
    poisoned = dict(payload)
    poisoned["chat_report"] = {"reason": "S3: cannot build the transcript index: constraint failed"}
    poisoned["chat"] = []
    store.cache_put("threshold", engine.scorer.name, poisoned, extra=extra)

    replay = engine.search("threshold", messages=messages, scope="both")

    assert not (replay.chat_report or {}).get("reason"), "a poisoned cache entry was trusted"
    assert replay.chat


def _fingerprint(messages: list[dict]) -> str:
    from trick.transcript import fingerprint

    return fingerprint(normalise_messages(messages))


class _FlatScorer:
    name = "mock"
    probability = 0.5

    def rank(self, query, texts):  # noqa: ANN001 - test double
        from trick.laya import Ranked

        return [Ranked(index, 1.0 / max(1, len(texts))) for index in range(len(texts))]


def test_engine_keeps_cache_off_when_asked(tmp_path: Path) -> None:
    """Guard: the new cache rules must not resurrect a disabled cache."""
    store = DocumentStore(tmp_path / "trick.db")
    engine = SearchEngine(store, _FlatScorer(), use_cache=False)
    engine.search("models", messages=_long_chat(), scope="both")
    assert store.cache_get("threshold", engine.scorer.name, extra="x") is None


def _json_round_trip(value):
    return json.loads(json.dumps(value))
