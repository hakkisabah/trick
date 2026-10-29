"""The renderer: what a surface is allowed to claim about a score.

A wrong marker is a product bug, not a cosmetic one - it tells the user the model
ranked something it never saw. These tests pin every claim the text makes.
"""

from __future__ import annotations

from trick.render import DEGRADED_TEXT, explain, render_compact, render_json, render_outcome
from trick.search import ResultRow, SearchOutcome


def row(**overrides) -> ResultRow:
    base = dict(
        rank=1,
        doc_id=1,
        source="cran",
        key="lme4",
        title="lme4: Linear Mixed-Effects Models",
        summary="Mixed effects.",
        url="https://example.test/lme4",
        retrieval_score=0.8,
        laya_probability=0.6,
        final_score=0.64,
        scored_by_laya=True,
    )
    base.update(overrides)
    return ResultRow(**base)


def outcome(rows=None, degraded=None, **report) -> SearchOutcome:
    return SearchOutcome(
        query="mixed effects",
        rows=rows if rows is not None else [row()],
        degraded=degraded or [],
        report=report or {"elapsed_ms": 12.0, "scorer": "laya-remote"},
    )


def test_scored_row_is_marked_with_a_star() -> None:
    text = render_outcome(outcome())
    assert "1*" in text
    assert "1-" not in text


def test_unscored_row_is_marked_with_a_dash_and_says_so() -> None:
    text = render_outcome(outcome(rows=[row(scored_by_laya=False, laya_probability=None)]))
    assert "1-" in text


def test_heading_carries_timing_and_scorer() -> None:
    text = render_outcome(outcome())
    assert "12.0 ms" in text
    assert "scorer=laya-remote" in text


def test_degraded_codes_come_with_a_sentence() -> None:
    text = render_outcome(outcome(degraded=["S5"]))
    assert "degraded S5" in text
    assert "retrieval-only" in text


def test_unknown_degradation_code_stays_visible() -> None:
    assert "unknown degradation S99" in explain("S99")


def test_every_documented_code_has_a_sentence() -> None:
    for code in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"):
        assert DEGRADED_TEXT[code]


def test_empty_outcome_says_no_results() -> None:
    text = render_outcome(outcome(rows=[]))
    assert "no results" in text


def test_verbose_shows_both_components() -> None:
    text = render_outcome(outcome(), verbose=True)
    assert "components" in text
    assert "retrieval=0.8000" in text
    assert "laya=0.6000" in text


def test_verbose_says_n_a_when_the_model_did_not_score() -> None:
    text = render_outcome(outcome(rows=[row(scored_by_laya=False, laya_probability=None)]), verbose=True)
    assert "laya=n/a" in text


def test_long_title_is_truncated_with_an_ellipsis() -> None:
    text = render_outcome(outcome(rows=[row(title="x" * 300)]), width=40)
    assert "\u2026" in text
    assert len(max(text.splitlines(), key=len)) < 120


def test_compact_form_is_short_and_lists_degradations() -> None:
    text = render_compact(outcome(rows=[row(), row(rank=2)], degraded=["S5"]), limit=1)
    assert text.splitlines()[0].endswith(":")
    assert "degraded" in text
    assert text.count("https://example.test") == 1


def test_compact_form_handles_no_rows() -> None:
    text = render_compact(outcome(rows=[], degraded=["S4"]))
    assert "No result" in text
    assert "S4" in text


def test_json_form_is_parsable_and_complete() -> None:
    import json

    payload = json.loads(render_json(outcome(degraded=["S5"])))
    assert payload["query"] == "mixed effects"
    assert payload["degraded"] == ["S5"]
    assert payload["rows"][0]["score_label"] == "laya"
