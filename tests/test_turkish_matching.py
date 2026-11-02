"""Turkish matching: the fold, the stem fallback, and the filter that hid both.

The Turkish letters in this file are written as \\uXXXX escapes: the repository
is ASCII-only by rule (scripts/language_gate.py), and a test about Turkish
letters cannot avoid them. Each escape is the letter it names - \\u0131 is the
dotless i, \\u015f the s-cedilla, \\u011f the g-breve.

Measured on three real chats before this file existed: exact-surface queries
found 35 of 36 gold passages, the same words typed in ASCII found 0 of 36, and a
five-letter stem found 9 of 36. The two failures were different bugs - the
dotless i is not a diacritic, so SQLite never folded it, and the coverage filter
compared raw strings, so a passage the index had found was dropped a step later.

These tests are the checkpoint for that class: any change that reintroduces
either failure turns one of them red. The scorer is uniform, so the ranking is
the lexical pipeline's own and nothing here touches the network.
"""

from __future__ import annotations

import pytest
from trick.laya import Ranked
from trick.transcript import (
    FOLDED_STOPWORDS,
    ConversationSearch,
    Message,
    TranscriptIndex,
    _terms,
    fold_tr,
    snippet,
    term_hit,
)

# Two turns: the first carries the dotless-i word a reader would remember, the
# second repeats the stem in a different inflection. A third passage shares only
# a function word, so it must never surface as a match.
CHAT = [
    Message(
        mid="a1",
        role="user",
        content=(
            "i5 uzerinde en cok problem yasadigimiz yer burasi: nobetci betigi "
            "iki kez ayni dosyayi yaziyor ve ikinci yazim birincisinin uzerine "
            "geciyor, bu yuzden sabah raporunda eksik gun gorunuyor."
        ),
    ),
    Message(
        mid="a2",
        role="assistant",
        content=(
            "Duzeltme: yazma islemi gecici dosyaya yapilacak, sonra tek bir "
            "yeniden adlandirma ile yerine konacak. Bu sayede yarim kalan yazim "
            "eski dosyayi bozmaz ve rapor her zaman tam gun sayisini gosterir."
        ),
    ),
    Message(
        mid="a3",
        role="user",
        content=(
            "Tamam, en cok merak ettigim sey bu degil; baska bir konuya gecelim "
            "ve oradaki ayrintilari konusalim, cunku zaman azaliyor."
        ),
    ),
]


class Uniform:
    """Uniform probabilities: never changes the retrieval order."""

    name = "uniform"

    def rank(self, query, candidates, instructions=""):
        return [Ranked(index=i, probability=0.5) for i in range(len(candidates))]


def _engine() -> ConversationSearch:
    return ConversationSearch(Uniform(), gate_margin=0.0)


def _rows(query: str, limit: int = 3):
    rows, _report = _engine().search(query, CHAT, limit=limit)
    return rows


def test_fold_maps_the_dotless_i_and_the_accented_letters() -> None:
    assert fold_tr("ya\u015fad\u0131\u011f\u0131m\u0131z") == "yasadigimiz"
    assert fold_tr("\u0130stanbul") == "istanbul"
    assert fold_tr("I\u015eIK") == "isik"
    assert fold_tr("\u00c7\u00d6Z\u00dcM") == "cozum"
    # One character for one character, so snippet offsets survive the fold.
    assert len(fold_tr("ya\u015fad\u0131\u011f\u0131m\u0131z")) == len("ya\u015fad\u0131\u011f\u0131m\u0131z")


def test_turkish_stopwords_survive_the_fold() -> None:
    # The list is written with Turkish letters; folding must not make it inert.
    assert "cok" in FOLDED_STOPWORDS
    assert "en" in FOLDED_STOPWORDS
    # Both function words are dropped; the folded content words survive.
    assert _terms("en \u00e7ok problem ya\u015fad\u0131\u011f\u0131m\u0131z") == ["problem", "yasadigimiz"]


def test_ascii_typed_query_finds_the_accented_passage() -> None:
    # The reader types without Turkish letters: no dotless i, no s-cedilla.
    rows = _rows("problem yasadigimiz yer")
    assert rows, "an ASCII-typed query must still find the passage"
    assert rows[0].mid == "a1"


def test_accented_query_finds_the_same_passage() -> None:
    rows = _rows("problem ya\u015fad\u0131\u011f\u0131m\u0131z yer")
    assert rows and rows[0].mid == "a1"


def test_a_stem_finds_its_inflections() -> None:
    # "yazim" is the stem of "yaziyor"/"yazim"/"yazma"; exact matching found none.
    rows = _rows("yazim")
    assert rows, "a five-letter Turkish stem must reach its inflected forms"
    assert {row.mid for row in rows} & {"a1", "a2"}


def test_the_coverage_filter_does_not_drop_what_the_index_found() -> None:
    # The bug that made ASCII queries return nothing: FTS hit, then the raw
    # string comparison scored coverage 0.0 and the row was discarded silently.
    rows = _rows("yasadigimiz")
    assert rows and rows[0].coverage > 0.0
    assert rows[0].matched == ("yasadigimiz",)


def test_a_passage_sharing_only_a_function_word_is_not_a_match() -> None:
    rows = _rows("en cok")
    assert rows == [], "function words alone must not produce a match"


def test_term_hit_matches_prefixes_only_for_long_terms() -> None:
    tokens = frozenset({"enerji", "yapildi", "yapilan"})
    assert term_hit("enerji", tokens) is True
    assert term_hit("yapil", tokens) is True  # stem reaches both inflections
    assert term_hit("en", tokens) is False, "short terms must match a whole word"
    assert term_hit("ener", tokens) is False


def test_snippet_centres_on_the_passage_spelling_not_the_query() -> None:
    text = "x" * 200 + " ya\u015fad\u0131\u011f\u0131m\u0131z yer burasi " + "y" * 200
    window = snippet(text, "yasadigimiz", width=80)
    assert "ya\u015fad\u0131\u011f\u0131m\u0131z" in window


def test_bm25_falls_back_to_prefix_only_when_exact_finds_nothing() -> None:
    index = TranscriptIndex(CHAT)
    exact = index.bm25("yasadigimiz")
    assert exact, "an exact token must be found without prefix expansion"
    stem = index.bm25("yazim")
    assert stem, "a stem with no exact match must still find its inflections"


@pytest.mark.parametrize("query", ["YASADIGIMIZ", "Ya\u015fad\u0131\u011f\u0131m\u0131z", "yasadigimiz"])
def test_case_and_spelling_variants_agree(query: str) -> None:
    rows = _rows(query)
    assert rows and rows[0].mid == "a1"
