"""One renderer for every surface.

The terminal UI, the MCP tool result and the HTTP bridge all show the same search
outcome, so they share one formatter. Duplicated formatting is how a product ends
up telling two users two different things about the same query.

Rules that this module enforces:

* a row scored by the model is marked ``*``; a row ordered without it is marked
  ``-`` and its label says so, because a silent downgrade is worse than a visible
  one;
* degraded stages are printed as ``S4``-style codes plus a one-line explanation,
  never hidden;
* the score column is the final blend, and the two components are shown when
  asked for, so a surprising order can be explained instead of argued about.
"""

from __future__ import annotations

import json

from .search import SearchOutcome

SOURCE_LABELS = {
    "cran": "CRAN",
    "bioconductor": "Bioc",
    "arxiv": "arXiv",
    "openalex": "OpenAlex",
    "crossref": "Crossref",
    "local": "local",
}

DEGRADED_TEXT = {
    "S1": "score matrix not built yet",
    "S2": "score matrix corrupt, emergency matrix in use",
    "S3": "not enough memory for the index, text-only mode",
    "S4": "a live source did not answer",
    "S5": "laya did not answer, results are retrieval-only",
    "S6": "storage problem",
    "S7": "another writer holds the lock",
    "S8": "input rejected by the file contract",
    "S9": "configuration problem",
}


def explain(code: str) -> str:
    """Human sentence for an ``S<n>`` code; unknown codes stay visible."""
    return DEGRADED_TEXT.get(code, f"unknown degradation {code}")


def render_outcome(
    outcome: SearchOutcome,
    *,
    width: int = 100,
    verbose: bool = False,
    heading: bool = True,
) -> str:
    """Render one search outcome as plain text."""
    lines: list[str] = []
    if heading:
        report = outcome.report or {}
        elapsed = report.get("elapsed_ms")
        parts = [f"trick: {outcome.query}"]
        if elapsed is not None:
            parts.append(f"{elapsed} ms")
        scorer = report.get("scorer")
        if scorer:
            parts.append(f"scorer={scorer}")
        if report.get("cached"):
            parts.append("cached")
        if report.get("gated"):
            parts.append("gated")
        lines.append("  ".join(str(p) for p in parts))

    for code in outcome.degraded:
        lines.append(f"degraded {code}: {explain(code)}")

    chat = list(getattr(outcome, "chat", None) or [])
    if chat:
        lines.extend(_chat_block(outcome, chat, width=width, verbose=verbose))

    if not outcome.rows:
        lines.append("no sources" if chat else "no results")
        return "\n".join(lines)

    for row in outcome.rows:
        marker = "*" if row.scored_by_laya else "-"
        label = SOURCE_LABELS.get(row.source, row.source[:6])
        title = row.title if len(row.title) <= width else row.title[: width - 1].rstrip() + "\u2026"
        lines.append(f"{row.rank:2d}{marker} {row.final_score:5.3f}  {label:6s} {title}")
        if row.key and row.key not in title:
            lines.append(f"        key={row.key}")
        if row.url:
            lines.append(f"        {row.url}")
        if verbose:
            lines.append(
                "        components"
                f" retrieval={row.retrieval_score:.4f}"
                f" laya={'n/a' if row.laya_probability is None else format(row.laya_probability, '.4f')}"
                f" label={row.score_label}"
            )
    return "\n".join(lines)


def render_json(outcome: SearchOutcome) -> str:
    """Machine form, identical keys on every surface."""
    return json.dumps(outcome.as_dict(), indent=2, ensure_ascii=False)


def _chat_block(outcome: SearchOutcome, chat: list, *, width: int, verbose: bool) -> list[str]:
    """Conversation passages, printed above the catalogue rows.

    Kept a separate block on purpose: the two lists come from different corpora,
    so numbering them as one list would claim a comparison that was never made.
    """
    report = getattr(outcome, "chat_report", None) or {}
    parts = [f"conversation: {len(chat)} passage{'s' if len(chat) != 1 else ''}"]
    chunks = report.get("chunk_count")
    turns = report.get("turn_count")
    if chunks:
        parts.append(f"of {chunks} chunks")
    if turns:
        parts.append(f"{turns} turns")
    if report.get("dropped_turns"):
        parts.append(f"windowed: oldest {report['dropped_turns']} turns not searched")
    lines = ["  ".join(str(p) for p in parts)]
    for row in chat:
        marker = "*" if row.scored_by_laya else "-"
        role = (row.role or "user")[:9]
        text = row.text if len(row.text) <= width else row.text[: width - 1].rstrip() + "\u2026"
        lines.append(f"c{row.rank}{marker} {row.final_score:5.3f}  turn {row.turn:<4d}{role:10s} {text}")
        if verbose:
            lines.append(
                "        components"
                f" retrieval={row.retrieval_score:.4f}"
                f" laya={'n/a' if row.laya_probability is None else format(row.laya_probability, '.4f')}"
                f" chars={row.char_start}-{row.char_end}"
            )
    return lines


def render_compact(outcome: SearchOutcome, *, limit: int = 5) -> str:
    """Few-line form for a UI that has little space, such as a chat message."""
    chat = list(getattr(outcome, "chat", None) or [])
    lines: list[str] = []
    if chat:
        lines.append(f"{len(chat)} conversation matches for {outcome.query!r}:")
        for row in chat[:limit]:
            marker = "*" if row.scored_by_laya else "-"
            lines.append(f"{row.rank}{marker} turn {row.turn} {row.role}: {row.text}")
    if not outcome.rows:
        if chat:
            lines.append(f"no catalogue source matched {outcome.query!r}.")
        else:
            detail = ", ".join(outcome.degraded) if outcome.degraded else "no candidates"
            return f"No result for {outcome.query!r} ({detail})."
    else:
        if chat:
            lines.append("")
        lines.append(f"{len(outcome.rows)} sources for {outcome.query!r}:")
        for row in outcome.rows[:limit]:
            marker = "*" if row.scored_by_laya else "-"
            lines.append(f"{row.rank}{marker} [{row.source}] {row.title}")
            if row.url:
                lines.append(f"   {row.url}")
    if outcome.degraded:
        lines.append("degraded: " + ", ".join(f"{c} ({explain(c)})" for c in outcome.degraded))
    return "\n".join(lines)


__all__ = [
    "DEGRADED_TEXT",
    "SOURCE_LABELS",
    "explain",
    "render_compact",
    "render_json",
    "render_outcome",
]
