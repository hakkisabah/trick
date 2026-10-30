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
      +-- final = 0.2 * laya + 0.8 * retrieval -> rows + provenance

Why the blend: laya answers "which candidate answers this state", which is the
decision we want, but a 12-way choice is noisy at the tail; the retrieval score
keeps factual lexicographic overlap in play. The 0.2/0.8 split is measured, not
assumed (see DEFAULT_WEIGHTS), and every row carries both components so a wrong
ranking can be diagnosed instead of argued about.
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
# Model vs retrieval weight. Was 0.3/0.7 in phase 2; a sweep with the live model
# over both gold sets moved it to 0.2/0.8 (canonical nDCG@10 0.797 -> 0.803,
# niche 0.810 -> 0.846). Below 0.2 the curve is flat, so the model keeps a real
# share instead of the ranking becoming a pure keyword sort.
DEFAULT_WEIGHTS = (0.2, 0.8)
# How much the ingested canonicality prior (CRAN downloads, Bioconductor reverse
# dependencies) counts against raw BM25. Tuned on scripts/eval_ranking.py.
#
# Raising this was tried and rejected with numbers: at 0.58 the *retrieval-only*
# ranking improves (mock nDCG@10 0.835 -> 0.895) but the end-to-end result gets
# worse (0.831 -> 0.74..0.78) because extra popular candidates enter the
# shortlist and the model then ranks them low, while genuinely specific niche
# matches get evicted (niche recall@12 1.0 -> 0.6 at 0.62). The prior is a
# tie-breaker for text relevance, not a ranking signal of its own.
DEFAULT_PRIOR = 0.45
# Blend the prior into the retrieval term shown to the ranker? Only useful when
# there is no model score at all - see ``SearchEngine.blend_uses_prior``.
DEFAULT_BLEND_PRIOR = False
# How the two retrieval signals (text relevance, canonicality prior) are combined.
# "linear" mixes the scores, which works while the prior stays a tie-breaker.
# "rrf" fuses ranks instead; it preserves both signals' sharpness but measurably
# over-popularises at a high prior (niche recall@12 1.0 -> 0.6), so it is kept as
# an option, not as the default.
DEFAULT_FUSION = "linear"
RRF_K = 60
# How the model probability meets the retrieval score. See SearchEngine._blend.
# "fused" (prior included in the retrieval term) measured 0.803/0.846 against
# "linear" (text only) 0.807/0.612 - the prior belongs in the blend.
DEFAULT_BLEND_MODE = "fused"
# Skip the scorer when the retrieval top-1 leads top-2 by this fraction of its
# score: measured 7/22 queries gated with no change in nDCG@10.
DEFAULT_GATE = 0.15
# Sources that live in the local store as bulk catalogue snapshots. They are the
# reason Trick exists, and they lose to live APIs on raw BM25 very easily: a paper
# abstract repeats the query words, a package description rarely does.
LOCAL_SOURCES = frozenset({"cran", "bioconductor"})
# How many of the shortlist slots are reserved for local catalogue entries when
# such candidates exist, and how many one live source may take. Measured in
# phase 4: see scripts/eval_quota.py. Both are bounds, not targets - an unused
# reservation is handed back to the global ranking order.
DEFAULT_LOCAL_FLOOR = 4
DEFAULT_LIVE_CAP = 4


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

    @classmethod
    def from_dict(cls, payload: dict) -> "SearchOutcome":
        """Rebuild an outcome that crossed a process boundary.

        The socket client and the MCP server both hand back plain JSON, so this is
        the single place that decides how a row survives serialisation. A row that
        lost its ``score_label`` is treated as retrieval-only, because claiming a
        model score that was not there is the one mistake worth preventing.
        """
        rows = [
            row_from_dict(position, item)
            for position, item in enumerate(payload.get("rows") or [], start=1)
            if isinstance(item, dict)
        ]
        return cls(
            query=str(payload.get("query") or ""),
            rows=rows,
            degraded=[str(code) for code in payload.get("degraded") or []],
            report=dict(payload.get("report") or {}),
        )


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
        blend_prior: bool = DEFAULT_BLEND_PRIOR,
        fusion: str = DEFAULT_FUSION,
        blend_mode: str = DEFAULT_BLEND_MODE,
        gate_margin: float = DEFAULT_GATE,
        budget_s: float = 1.5,
        use_cache: bool = True,
        local_floor: int = DEFAULT_LOCAL_FLOOR,
        live_cap: int = DEFAULT_LIVE_CAP,
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
        self.blend_prior = bool(blend_prior)
        self.fusion = "rrf" if str(fusion).lower() == "rrf" else "linear"
        self.blend_mode = str(blend_mode).lower() if str(blend_mode).lower() in ("linear", "fused", "rrf") else "linear"
        # When the retrieval top-1 is this much better than top-2, asking the
        # scorer cannot change the answer, so we do not spend a call.
        self.gate_margin = max(0.0, float(gate_margin))
        self.local_floor = max(0, int(local_floor))
        self.live_cap = max(0, int(live_cap))
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
        shortlist_ids, cut_scores, text_scores = self._shortlist(query, candidates, priors)
        timings["shortlist_ms"] = (time.monotonic() - stage) * 1000.0

        # Stage 5: ask laya.
        stage = time.monotonic()
        docs = self.store.get_many(shortlist_ids)
        ordered_ids = [doc_id for doc_id in shortlist_ids if doc_id in docs]
        probabilities: dict[int, float] = {}
        scored = False
        gated = self._gate_closed(text_scores)
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
        rows = self._blend(ordered_ids, docs, text_scores, cut_scores, probabilities, scored, limit)
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
    ) -> tuple[list[int], dict[int, float], dict[int, float]]:
        """Cut the candidate union to the laya shortlist.

        Returns ``(keep, cut_scores, text_scores)``. The cut mixes in the ingested
        canonicality prior - raw BM25 favours short keyword-dense packages over
        canonical tools, and whatever falls out of the shortlist can never be
        rescued by the scorer. ``text_scores`` stays pure BM25, because that is
        the discriminative signal the blend wants once the model has spoken.
        """
        ids = list(candidates)
        beta = self.prior_weight
        text: dict[int, float] = {doc_id: max(candidates.get(doc_id, 0.0), 0.0) for doc_id in ids}
        if self.fusion == "rrf":
            cut = _weighted_rrf(ids, text, priors, beta)
        else:
            cut = {
                doc_id: (1.0 - beta) * text[doc_id] + beta * priors.get(doc_id, 0.0) for doc_id in ids
            }
        if len(ids) <= self.shortlist:
            return ids, cut, text

        stage_scores = self.store.rescore(query, ids)
        for doc_id in ids:
            text[doc_id] = max(text[doc_id], stage_scores.get(doc_id, 0.0))
        if self.fusion == "rrf":
            cut = _weighted_rrf(ids, text, priors, beta)
        else:
            for doc_id in ids:
                cut[doc_id] = max(
                    cut[doc_id],
                    (1.0 - beta) * text[doc_id] + beta * priors.get(doc_id, 0.0),
                )
        merged = sorted(((cut[doc_id], doc_id) for doc_id in ids), key=lambda item: (-item[0], item[1]))
        ranked = [doc_id for _, doc_id in merged]
        keep = apply_source_quota(
            ranked,
            self.shortlist,
            self.store.sources_for(ids),
            local_floor=self.local_floor,
            live_cap=self.live_cap,
        )
        return keep, {doc_id: cut[doc_id] for doc_id in keep}, {doc_id: text[doc_id] for doc_id in keep}

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
        text_scores: dict[int, float],
        cut_scores: dict[int, float],
        probabilities: dict[int, float],
        scored: bool,
        limit: int,
    ) -> list[ResultRow]:
        """Order the shortlist and attach both component scores to every row.

        ``blend_mode`` decides how the two signals meet:

        ``linear``  w*laya + (1-w)*bm25, the first thing that worked;
        ``fused``   w*laya + (1-w)*rrf(text, prior) - keeps the prior's opinion;
        ``rrf``     rank fusion of the model rank and the retrieval rank, which is
                    the only variant that cannot be swamped by a different score
                    scale on either side.

        Every mode falls back to retrieval order when the scorer did not answer,
        so a dead endpoint degrades the ordering instead of the answer.
        """
        w_laya, w_retrieval = self.weights
        mode = self.blend_mode if (scored and probabilities) else "linear"
        if not scored:
            retrieval_scores = cut_scores
        elif mode == "fused" or self.blend_prior:
            retrieval_scores = cut_scores
        else:
            retrieval_scores = text_scores

        raw: list[tuple[float, int]] = []
        if mode == "rrf":
            retr_rank = {
                doc_id: position
                for position, doc_id in enumerate(
                    sorted(ordered_ids, key=lambda i: (-retrieval_scores.get(i, 0.0), i)), start=1
                )
            }
            laya_rank = {
                doc_id: position
                for position, doc_id in enumerate(
                    sorted(ordered_ids, key=lambda i: (-(probabilities.get(i) or 0.0), i)), start=1
                )
            }
            for doc_id in ordered_ids:
                value = w_retrieval / (RRF_K + retr_rank[doc_id])
                if doc_id in probabilities:
                    value += w_laya / (RRF_K + laya_rank[doc_id])
                raw.append((value, doc_id))
            best = max((value for value, _ in raw), default=0.0) or 1.0
            raw = [(value / best, doc_id) for value, doc_id in raw]
        else:
            for doc_id in ordered_ids:
                retrieval = retrieval_scores.get(doc_id, 0.0)
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
                    retrieval_score=retrieval_scores.get(doc_id, 0.0),
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
            "prior_weight": self.prior_weight,
            "fusion": self.fusion,
            "blend_mode": self.blend_mode,
            "source_quota": {"local_floor": self.local_floor, "live_cap": self.live_cap},
        }
        matrix = self._open_matrix()
        if matrix is not None:
            info["matrix"] = {"count": matrix.count, "synthetic": matrix.synthetic}
        elif self._matrix_error:
            info["matrix"] = {"error": self._matrix_error}
        else:
            info["matrix"] = None
        return info


def apply_source_quota(
    ranked: Sequence[int],
    size: int,
    sources: dict[int, str],
    *,
    local_floor: int = 0,
    live_cap: int = 0,
) -> list[int]:
    """Keep the ranking order while bounding what one source family may take.

    Two failure modes motivate this, and they are not symmetric:

    * a live API fills the whole shortlist, so the local catalogue - the thing
      the user actually asked for - never reaches the scorer. A short package
      description cannot out-BM25 a paper abstract that repeats the query, so
      this happens on realistic queries, not on pathological ones. The floor
      reserves slots for local entries *before* the global walk;
    * one live source returns twelve near-duplicates of the same paper and
      crowds out the other sources. ``live_cap`` bounds each live source.

    Both parameters are bounds, never targets. A reservation nobody fills is
    handed back to the global order in the third pass, so a query with no local
    candidates still gets a full live shortlist. The result keeps the original
    ranking order - the quota decides *membership*, not order, because the
    scorer and the blend own the order.
    """
    if size <= 0 or not ranked:
        return []
    limit = min(int(size), len(ranked))
    if local_floor <= 0 and live_cap <= 0:
        return list(ranked[:limit])

    reserved_budget = min(max(0, int(local_floor)), limit)
    chosen: list[int] = []
    seen: set[int] = set()
    taken: dict[str, int] = {}

    def take(doc_id: int) -> None:
        chosen.append(doc_id)
        seen.add(doc_id)
        source = sources.get(doc_id, "")
        taken[source] = taken.get(source, 0) + 1

    if reserved_budget:
        for doc_id in ranked:
            if len(chosen) >= reserved_budget:
                break
            if sources.get(doc_id) in LOCAL_SOURCES:
                take(doc_id)

    for doc_id in ranked:
        if len(chosen) >= limit:
            break
        if doc_id in seen:
            continue
        source = sources.get(doc_id, "")
        if source not in LOCAL_SOURCES and live_cap > 0 and taken.get(source, 0) >= live_cap:
            continue
        take(doc_id)

    if len(chosen) < limit:
        # Nothing could fill the list under the cap. An empty slot is worse than
        # an over-cap entry, so the rest is filled round-robin across the
        # remaining sources: a duplicate-heavy query degrades into a *balanced*
        # list instead of one source taking every leftover slot.
        queues: dict[str, list[int]] = {}
        order: list[str] = []
        for doc_id in ranked:
            if doc_id in seen:
                continue
            source = sources.get(doc_id, "")
            if source not in queues:
                queues[source] = []
                order.append(source)
            queues[source].append(doc_id)
        while len(chosen) < limit and any(queues[source] for source in order):
            for source in order:
                if len(chosen) >= limit:
                    break
                if queues[source]:
                    take(queues[source].pop(0))

    position = {doc_id: index for index, doc_id in enumerate(ranked)}
    return sorted(chosen, key=lambda doc_id: position[doc_id])


def _weighted_rrf(
    ids: Sequence[int], text: dict[int, float], priors: dict[int, float], beta: float
) -> dict[int, float]:
    """Fuse a text ranking and a prior ranking by weighted reciprocal rank.

    A linear mix of scores assumes both signals live on comparable scales. They do
    not: the prior is a log-scaled popularity value that sits near 1.0 for every
    established package, so mixing it into a normalised BM25 compresses the
    differences until the small model term decides the order. Fusing ranks keeps
    both signals equally sharp whatever their scale, which is what lets a stronger
    prior actually help instead of quietly wrecking the blend.
    """
    if not ids:
        return {}
    by_text = sorted(ids, key=lambda i: (-text.get(i, 0.0), i))
    by_prior = sorted(ids, key=lambda i: (-priors.get(i, 0.0), i))
    text_rank = {doc_id: position for position, doc_id in enumerate(by_text, start=1)}
    prior_rank = {doc_id: position for position, doc_id in enumerate(by_prior, start=1)}
    weight = max(0.0, min(float(beta), 1.0))
    fused: dict[int, float] = {}
    for doc_id in ids:
        value = (1.0 - weight) / (RRF_K + text_rank[doc_id])
        if weight > 0.0:
            value += weight / (RRF_K + prior_rank[doc_id])
        fused[doc_id] = value
    best = max(fused.values()) or 1.0
    return {doc_id: value / best for doc_id, value in fused.items()}


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


def row_from_dict(rank: int, item: dict) -> ResultRow:
    """Rebuild one result row from its JSON form (see ``SearchOutcome.from_dict``)."""
    label = str(item.get("score_label") or "")
    probability = item.get("laya_probability")
    return ResultRow(
        rank=int(item.get("rank") or rank),
        doc_id=int(item.get("doc_id") or 0),
        source=str(item.get("source") or ""),
        key=str(item.get("key") or ""),
        title=str(item.get("title") or ""),
        summary=str(item.get("summary") or ""),
        url=str(item.get("url") or ""),
        retrieval_score=float(item.get("retrieval_score") or 0.0),
        laya_probability=None if probability is None else float(probability),
        final_score=float(item.get("final_score") or 0.0),
        scored_by_laya=(label == "laya") and probability is not None,
        from_cache=bool(item.get("from_cache")),
    )


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
    local_floor: int = DEFAULT_LOCAL_FLOOR,
    live_cap: int = DEFAULT_LIVE_CAP,
) -> SearchEngine:
    """Convenience constructor used by the CLI and the MCP server."""
    store = DocumentStore(db_path)
    return SearchEngine(
        store,
        build_scorer(scorer_mode),
        matrix_path=matrix_path,
        use_cache=use_cache,
        budget_s=budget_s,
        local_floor=local_floor,
        live_cap=live_cap,
    )


__all__ = [
    "DEFAULT_LIVE_CAP",
    "DEFAULT_LOCAL_FLOOR",
    "DEFAULT_PREFILTER",
    "DEFAULT_SHORTLIST",
    "DEFAULT_WEIGHTS",
    "LOCAL_SOURCES",
    "ResultRow",
    "SearchEngine",
    "SearchOutcome",
    "apply_source_quota",
    "build_engine",
    "row_from_dict",
]
