"""The search engine: retrieve, prefilter, rescore, ask laya, rank.

Pipeline (each stage may degrade, none may crash the search)::

    query
      |
      +-- decision cache hit? -----------------> return cached ranking
      |
      +-- live adapters (arXiv, OpenAlex,      |
      |   Crossref), per-source budget         |  S4: skipped, recorded
      |
      +-- local store BM25 (bulk catalogues)   |
      |
      +-- int8 signature prefilter (matrix)    |  S1/S2/S3: skipped, recorded
      |
      +-- BM25 rescore of the union ----------> shortlist (<= 12)
      |
      +-- laya choice question over labels ---> probabilities
      |                                       |  S5: retrieval order only
      |
      +-- final = 0.7 * laya + 0.3 * retrieval -> rows + provenance

Why the blend: laya answers "which candidate answers this state", which is the
decision we want, but a 12-way choice is noisy at the tail; the retrieval score
keeps factual lexicographic overlap in play. The weights are configuration, and
every row carries both components so a wrong ranking can be diagnosed instead of
argued about.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .errors import MatrixCorrupt, MatrixMissing, ResourceExhausted, ScorerUnavailable, TrickError
from .laya import MockScorer, Scorer, ScorerStats, build_scorer
from .matrix import MatrixIndex
from .quant import feature_hash_signature
from .retrieval import QueryAdapter, RetrievalReport, live_adapters, search_live
from .textindex import DocumentStore

DEFAULT_SHORTLIST = 12
DEFAULT_PREFILTER = 200
DEFAULT_WEIGHTS = (0.3, 0.7)
# How much the ingested canonicality prior (CRAN downloads, Bioconductor reverse
# dependencies) counts against raw BM25. Tuned on scripts/eval_ranking.py; set to
# 0.0 to rank on text alone.
DEFAULT_PRIOR = 0.45
# Skip the scorer when the retrieval top-1 leads top-2 by this fraction of its
# score: measured 7/22 queries gated with no change in nDCG@10.
DEFAULT_GATE = 0.15


@dataclass(frozen=True)
class ResultRow:
    """One ranked source, with the provenance of its score."""

    rank: int
    doc_id: int
    source: str
    key: str
    title: str
    summary: str
    url: str
    retrieval_score: float
    laya_probability: float | None
    final_score: float
    scored_by_laya: bool
    from_cache: bool = False

    @property
    def score_label(self) -> str:
        """What the UI may claim about this row's score."""
        if self.scored_by_laya:
            return "laya"
        return "retrieval-only"

    def as_dict(self) -> dict:
        return {
            "rank": self.rank,
            "doc_id": self.doc_id,
            "source": self.source,
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "url": self.url,
            "retrieval_score": round(self.retrieval_score, 4),
            "laya_probability": None if self.laya_probability is None else round(self.laya_probability, 4),
            "final_score": round(self.final_score, 4),
            "score_label": self.score_label,
            "from_cache": self.from_cache,
        }


@dataclass
class SearchOutcome:
    """Rows plus everything a caller needs to explain them."""

    query: str
    rows: list[ResultRow]
    degraded: list[str] = field(default_factory=list)
    report: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.rows)

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "degraded": list(self.degraded),
            "report": dict(self.report),
            "rows": [row.as_dict() for row in self.rows],
        }


class SearchEngine:
    """Wires a document store, an optional matrix and a scorer into one call."""

    def __init__(
        self,
        store: DocumentStore,
        scorer: Scorer | None = None,
        *,
        matrix_path: str | Path | None = None,
        adapters: Sequence[QueryAdapter] | None = None,
        shortlist: int = DEFAULT_SHORTLIST,
        prefilter: int = DEFAULT_PREFILTER,
        weights: tuple[float, float] = DEFAULT_WEIGHTS,
        prior_weight: float = DEFAULT_PRIOR,
        gate_margin: float = DEFAULT_GATE,
        budget_s: float = 1.5,
        use_cache: bool = True,
    ) -> None:
        self.store = store
        self.scorer: Scorer = scorer if scorer is not None else MockScorer()
        self.matrix_path = Path(matrix_path) if matrix_path else None
        self._adapters = adapters
        self.shortlist = max(2, min(int(shortlist), 26))
        self.prefilter = max(self.shortlist, int(prefilter))
        total = sum(weights) or 1.0
        self.weights = (weights[0] / total, weights[1] / total)
        self.prior_weight = max(0.0, min(float(prior_weight), 0.9))
        # When the retrieval top-1 is this much better than top-2, asking the
        # scorer cannot change the answer, so we do not spend a call.
        self.gate_margin = max(0.0, float(gate_margin))
        self.budget_s = budget_s
        self.use_cache = use_cache
        self._matrix: MatrixIndex | None = None
        self._matrix_error: str | None = None
        self._matrix_absent = False

    # -- matrix ------------------------------------------------------------
    def _open_matrix(self) -> MatrixIndex | None:
        if self._matrix is not None:
            return self._matrix
        if self.matrix_path is None or self._matrix_error is not None or self._matrix_absent:
            return None
        try:
            self._matrix = MatrixIndex(self.matrix_path)
        except MatrixMissing:
            # Not an error: a fresh install has no matrix until the first ingest
            # writes one. Only corruption and resource failures are degradations.
            self._matrix_absent = True
            return None
        except (MatrixCorrupt, ResourceExhausted, TrickError) as exc:
            self._matrix_error = f"{exc.s_code}: {exc}"
            return None
        return self._matrix

    # -- search ------------------------------------------------------------
    def search(self, query: str, *, limit: int = 10, use_live: bool | None = None) -> SearchOutcome:
        query = " ".join(str(query).split())
        if not query:
            raise TrickError("query is empty")

        degraded: list[str] = []
        timings: dict[str, float] = {}
        started = time.monotonic()

        if self.use_cache:
            cached = self.store.cache_get(query, self.scorer.name)
            if cached and isinstance(cached.get("rows"), list):
                rows = [_row_from_cache(i + 1, item) for i, item in enumerate(cached["rows"][:limit])]
                return SearchOutcome(
                    query=query,
                    rows=rows,
                    degraded=list(cached.get("degraded") or []),
                    report={"cached": True, "elapsed_ms": 0.0},
                )

        # Stage 1: live sources.
        stage = time.monotonic()
        report = RetrievalReport([], [], {}, 0.0)
        if use_live is not False:
            adapters = self._adapters if self._adapters is not None else live_adapters()
            report = search_live(query, adapters=adapters, budget_s=self.budget_s)
            if report.docs:
                self.store.upsert_many(doc.as_row() for doc in report.docs)
            if report.failed:
                degraded.append("S4")
        timings["live_ms"] = (time.monotonic() - stage) * 1000.0

        # Stage 2: local BM25 over the whole store (bulk catalogues live here).
        stage = time.monotonic()
        candidates: dict[int, float] = {}
        for doc_id, score in self.store.bm25(query, limit=self.prefilter):
            candidates[doc_id] = score
        timings["bm25_ms"] = (time.monotonic() - stage) * 1000.0

        # Stage 3: matrix prefilter (remembered signatures).
        stage = time.monotonic()
        matrix = self._open_matrix()
        if matrix is not None and matrix.count:
            try:
                hits = matrix.scan(feature_hash_signature(query), k=self.prefilter)
            except TrickError as exc:
                degraded.append(exc.s_code)
            else:
                for hit in hits:
                    if hit.cosine > 0:
                        candidates.setdefault(hit.record_id, max(hit.cosine, 0.0))
        elif self._matrix_error:
            degraded.append(self._matrix_error.split(":", 1)[0])
        timings["matrix_ms"] = (time.monotonic() - stage) * 1000.0

        if not candidates:
            return SearchOutcome(
                query=query,
                rows=[],
                degraded=sorted(set(degraded + ["S1"])),
                report={
                    "cached": False,
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                    "sources": report.as_dict(),
                    "reason": "no candidates",
                },
            )

        # Stage 4: rescore the union with BM25 restricted to those ids, then cut.
        stage = time.monotonic()
        priors = self.store.weights_for(list(candidates))
        shortlist_ids, shortlist_scores = self._shortlist(query, candidates, priors)
        timings["shortlist_ms"] = (time.monotonic() - stage) * 1000.0

        # Stage 5: ask laya.
        stage = time.monotonic()
        docs = self.store.get_many(shortlist_ids)
        ordered_ids = [doc_id for doc_id in shortlist_ids if doc_id in docs]
        probabilities: dict[int, float] = {}
        scored = False
        gated = self._gate_closed(shortlist_scores)
        if len(ordered_ids) >= 2 and not gated:
            texts = [_text_for(docs[doc_id]) for doc_id in ordered_ids]
            try:
                ranked = self.scorer.rank(query, texts)
            except ScorerUnavailable as exc:
                degraded.append(exc.s_code)
            except TrickError as exc:
                degraded.append(exc.s_code)
            else:
                scored = True
                for item in ranked:
                    if 0 <= item.index < len(ordered_ids):
                        probabilities[ordered_ids[item.index]] = item.probability
        timings["laya_ms"] = (time.monotonic() - stage) * 1000.0

        # Stage 6: blend and order.
        rows = self._blend(ordered_ids, docs, shortlist_scores, probabilities, scored, limit)
        if self.use_cache and rows:
            self.store.cache_put(
                query,
                self.scorer.name,
                {"rows": [row.as_dict() for row in rows], "degraded": sorted(set(degraded))},
            )

        return SearchOutcome(
            query=query,
            rows=rows,
            degraded=sorted(set(degraded)),
            report={
                "cached": False,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                "timings": {k: round(v, 1) for k, v in timings.items()},
                "sources": report.as_dict(),
                "candidates": len(candidates),
                "shortlist": len(ordered_ids),
                "scored_by_laya": scored,
                "gated": gated,
                "scorer": getattr(self.scorer, "name", "unknown"),
                "stats": _stats(self.scorer),
            },
        )

    def _shortlist(
        self, query: str, candidates: dict[int, float], priors: dict[int, float]
    ) -> tuple[list[int], dict[int, float]]:
        """Cut the candidate union to the laya shortlist, returning retrieval scores too.

        The cut matters more than it looks: whatever falls out of the shortlist can
        never be rescued by the scorer. Raw BM25 favours short keyword-dense
        packages over canonical tools, so the ingested prior is mixed in here and
        in the retrieval component that the final blend uses.
        """
        ids = list(candidates)
        beta = self.prior_weight
        retrieval = {
            doc_id: (1.0 - beta) * max(candidates.get(doc_id, 0.0), 0.0) + beta * priors.get(doc_id, 0.0)
            for doc_id in ids
        }
        if len(ids) <= self.shortlist:
            return ids, retrieval

        stage_scores = self.store.rescore(query, ids)
        merged: list[tuple[float, int]] = []
        for doc_id in ids:
            bm25 = stage_scores.get(doc_id, 0.0)
            prior = priors.get(doc_id, 0.0)
            combined = (1.0 - beta) * bm25 + beta * prior
            merged.append((combined, doc_id))
            retrieval[doc_id] = max(retrieval[doc_id], combined)
        merged.sort(key=lambda item: (-item[0], item[1]))
        keep = [doc_id for _, doc_id in merged[: self.shortlist]]
        return keep, {doc_id: retrieval[doc_id] for doc_id in keep}

    def _gate_closed(self, scores: dict[int, float]) -> bool:
        """True when the retrieval margin is wide enough to skip the scorer."""
        if self.gate_margin <= 0.0 or len(scores) < 2:
            return False
        ordered = sorted(scores.values(), reverse=True)
        best, second = ordered[0], ordered[1]
        if best <= 0.0:
            return False
        return (best - second) / best >= self.gate_margin

    def _blend(
        self,
        ordered_ids: list[int],
        docs: dict,
        candidates: dict[int, float],
        probabilities: dict[int, float],
        scored: bool,
        limit: int,
    ) -> list[ResultRow]:
        w_laya, w_retrieval = self.weights
        raw: list[tuple[float, int]] = []
        for doc_id in ordered_ids:
            retrieval = candidates.get(doc_id, 0.0)
            probability = probabilities.get(doc_id)
            if scored and probability is not None:
                final = w_laya * probability + w_retrieval * retrieval
            else:
                final = retrieval
            raw.append((final, doc_id))
        raw.sort(key=lambda item: (-item[0], item[1]))
        rows: list[ResultRow] = []
        for position, (final, doc_id) in enumerate(raw[:limit], start=1):
            doc = docs[doc_id]
            rows.append(
                ResultRow(
                    rank=position,
                    doc_id=doc_id,
                    source=doc.source,
                    key=doc.key,
                    title=doc.title,
                    summary=doc.summary,
                    url=doc.url,
                    retrieval_score=candidates.get(doc_id, 0.0),
                    laya_probability=probabilities.get(doc_id),
                    final_score=final,
                    scored_by_laya=scored and probabilities.get(doc_id) is not None,
                )
            )
        return rows

    # -- diagnostics -------------------------------------------------------
    def health(self) -> dict:
        info: dict = {
            "store": {"path": str(self.store.path), "docs": self.store.count(), "fts5": self.store.fts5},
            "scorer": getattr(self.scorer, "name", "unknown"),
            "shortlist": self.shortlist,
            "weights": list(self.weights),
        }
        matrix = self._open_matrix()
        if matrix is not None:
            info["matrix"] = {"count": matrix.count, "synthetic": matrix.synthetic}
        elif self._matrix_error:
            info["matrix"] = {"error": self._matrix_error}
        else:
            info["matrix"] = None
        return info


def _text_for(doc) -> str:
    """Candidate text shown to laya.

    No source tag: prefixing every candidate with "[cran/r-package]" measurably
    hurt the choice head (nDCG@10 0.650 with tags vs 0.760 without on the gold
    sets), because it adds a token that is identical across candidates.
    """
    summary = (doc.summary or "")[:280]
    return f"{doc.title}. {summary}".strip()


def _stats(scorer: Scorer) -> dict:
    stats = getattr(scorer, "stats", None)
    if isinstance(stats, ScorerStats):
        return stats.as_dict()
    return {}


def _row_from_cache(rank: int, item: dict) -> ResultRow:
    return ResultRow(
        rank=rank,
        doc_id=int(item.get("doc_id", 0)),
        source=str(item.get("source", "")),
        key=str(item.get("key", "")),
        title=str(item.get("title", "")),
        summary=str(item.get("summary", "")),
        url=str(item.get("url", "")),
        retrieval_score=float(item.get("retrieval_score", 0.0)),
        laya_probability=item.get("laya_probability"),
        final_score=float(item.get("final_score", 0.0)),
        scored_by_laya=bool(item.get("score_label") == "laya"),
        from_cache=True,
    )


def build_engine(
    db_path: str | Path,
    *,
    matrix_path: str | Path | None = None,
    scorer_mode: str = "auto",
    use_cache: bool = True,
    budget_s: float = 1.5,
) -> SearchEngine:
    """Convenience constructor used by the CLI and the MCP server."""
    store = DocumentStore(db_path)
    return SearchEngine(
        store,
        build_scorer(scorer_mode),
        matrix_path=matrix_path,
        use_cache=use_cache,
        budget_s=budget_s,
    )


__all__ = [
    "DEFAULT_PREFILTER",
    "DEFAULT_SHORTLIST",
    "DEFAULT_WEIGHTS",
    "ResultRow",
    "SearchEngine",
    "SearchOutcome",
    "build_engine",
]
