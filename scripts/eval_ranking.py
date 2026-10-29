"""Ranking quality harness.

One laya call per query produces both score components, so a single run yields
three comparable orderings:

    retrieval   BM25/prefilter only      (what a plain keyword search gives)
    laya        the model's probability  (what the model thinks answers it)
    blend       0.7 * laya + 0.3 * bm25  (what Trick shows)

Metrics are computed on the gold key of each query: recall@shortlist, MRR@10 and
nDCG@10 with binary relevance. The point is not a pretty number, it is a
controlled comparison: change one component, re-run, see whether the order moved
for the better.

    .venv/bin/python scripts/eval_ranking.py                 # live laya + local store
    .venv/bin/python scripts/eval_ranking.py --no-live        # local store only
    .venv/bin/python scripts/eval_ranking.py --scorer mock    # offline sanity run
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trick.laya import build_scorer  # noqa: E402
from trick.search import SearchEngine  # noqa: E402
from trick.textindex import DocumentStore  # noqa: E402

DEFAULT_DB = Path.home() / ".local" / "share" / "trick" / "trick.db"

# Two gold sets, because they pull in opposite directions.
#
# ``canonical``: the tool a domain expert would name. These are popular packages,
# so this set rewards a popularity prior - by construction it cannot prove that
# the prior is harmless.
#
# ``niche``: the query describes a specific technique whose best match is a
# low-traffic package. Here raw text matching should win, so the set catches a
# prior that is too strong.
GOLD_SETS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "canonical": (
        ("mixed effects model does not converge in R", "cran", "lme4"),
        ("negative binomial differential expression for RNA-seq counts", "bioconductor", "DESeq2"),
        ("simple features for spatial vector data", "cran", "sf"),
        ("arima and ets time series forecasting", "cran", "forecast"),
        ("cox proportional hazards survival analysis", "cran", "survival"),
        ("network analysis and graph algorithms", "cran", "igraph"),
        ("elastic net regularised regression", "cran", "glmnet"),
        ("probabilistic programming with stan", "cran", "rstan"),
        ("grammar of graphics plotting system", "cran", "ggplot2"),
        ("fast aggregation and joins for large tables", "cran", "data.table"),
        ("linear mixed effects p-values and tests", "cran", "lmerTest"),
        ("nonlinear mixed effects models", "cran", "nlme"),
    ),
    "niche": (
        ("croston method intermittent demand forecasting", "cran", "tsintermittent"),
        ("wavelet based arima forecasting", "cran", "WaveletArima"),
        ("copula model dependent censoring cox regression", "cran", "SemiPar.depCens"),
        ("remedy for proportional hazards assumption violations", "cran", "cox.rvph"),
        ("negative binomial mixed models large sample approximation", "cran", "nebula"),
        ("fractional arima long memory time series", "cran", "arfima"),
        ("regularized estimation in mixed effects models", "cran", "REMixed"),
        ("prediction intervals for arima processes", "cran", "tsPI"),
        ("grammar of graphics side plots", "cran", "ggside"),
        ("unified differential expression variability skewness rna-seq", "cran", "SIEVEseq"),
    ),
}

@dataclass
class Score:
    retrieval: int | None
    laya: int | None
    blend: int | None


def rank_of(rows, key: str, field: str) -> int | None:
    ordered = sorted(rows, key=lambda r: -getattr(r, field))
    for position, row in enumerate(ordered, start=1):
        if row.key == key:
            return position
    return None


def mrr(positions: list[int | None], k: int = 10) -> float:
    hits = [1.0 / p for p in positions if p and p <= k]
    return sum(hits) / len(positions) if positions else 0.0


def ndcg(positions: list[int | None], k: int = 10) -> float:
    total = 0.0
    for p in positions:
        total += (1.0 / math.log2(p + 1)) if p and p <= k else 0.0
    return total / len(positions) if positions else 0.0


def recall(positions: list[int | None]) -> float:
    return sum(1 for p in positions if p) / len(positions) if positions else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--scorer", default="auto", choices=["auto", "remote", "mock"])
    parser.add_argument("--no-live", action="store_true", help="skip arXiv/OpenAlex/Crossref")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--blend", default="", help="override blend weights, e.g. 0.4,0.6")
    parser.add_argument("--gate", type=float, default=None, help="skip the scorer when the retrieval margin is this wide")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--prior", type=float, default=None, help="override the canonicality prior weight")
    parser.add_argument("--set", dest="gold_set", default="both", choices=["canonical", "niche", "both"])
    args = parser.parse_args()

    store = DocumentStore(args.db)
    if store.count() == 0:
        print(f"store {args.db} is empty; run: python -m trick ingest", file=sys.stderr)
        return 2

    engine = SearchEngine(
        store,
        build_scorer(args.scorer),
        use_cache=False,
        shortlist=args.limit,
        **({"prior_weight": args.prior} if args.prior is not None else {}),
        **({"weights": tuple(float(x) for x in args.blend.split(","))} if args.blend else {}),
        **({"gate_margin": args.gate} if args.gate is not None else {}),
    )
    positions: dict[str, list[int | None]] = {"retrieval": [], "laya": [], "blend": []}
    detail: list[dict] = []

    sets = ["canonical", "niche"] if args.gold_set == "both" else [args.gold_set]
    queries = [(name,) + item for name in sets for item in GOLD_SETS[name]]
    query_count = len(queries)
    for _set_name, query, source, key in queries:
        outcome = engine.search(query, limit=args.limit, use_live=not args.no_live)
        rows = outcome.rows
        found = rank_of(rows, key, "final_score")
        scored = any(row.scored_by_laya for row in rows)
        positions["retrieval"].append(rank_of(rows, key, "retrieval_score"))
        positions["laya"].append(rank_of(rows, key, "laya_probability") if scored else None)
        positions["blend"].append(found)
        detail.append(
            {
                "set": _set_name,
                "query": query,
                "gold": f"{source}/{key}",
                "retrieval_rank": positions["retrieval"][-1],
                "laya_rank": positions["laya"][-1],
                "blend_rank": found,
                "degraded": outcome.degraded,
                "shortlist": outcome.report.get("shortlist"),
                "ms": outcome.report.get("elapsed_ms"),
                "gated": outcome.report.get("gated"),
            }
        )

    summary = {
        name: {
            f"recall@{args.limit}": round(recall(values), 3),
            "mrr@10": round(mrr(values), 3),
            "ndcg@10": round(ndcg(values), 3),
        }
        for name, values in positions.items()
    }
    per_set: dict[str, dict[str, dict[str, float]]] = {}
    for set_name in sets:
        subset = [row for row in detail if row["set"] == set_name]
        per_set[set_name] = {
            field: {
                f"recall@{args.limit}": round(recall([row[f"{field}_rank"] for row in subset]), 3),
                "mrr@10": round(mrr([row[f"{field}_rank"] for row in subset]), 3),
                "ndcg@10": round(ndcg([row[f"{field}_rank"] for row in subset]), 3),
            }
            for field in ("retrieval", "laya", "blend")
        }
    store.close()

    if args.json:
        print(json.dumps({"summary": summary, "per_set": per_set, "detail": detail}, indent=2, ensure_ascii=False))
        return 0

    gated_count = sum(1 for row in detail if row.get("gated"))
    print(f"queries={query_count} scorer={engine.scorer.name} live={not args.no_live} "
          f"shortlist={args.limit} prior={engine.prior_weight} blend={engine.weights} "
          f"gate={engine.gate_margin} skipped={gated_count}")
    for set_name, block in per_set.items():
        print(f"  [{set_name}]")
        for name, metrics in block.items():
            print(f"    {name:9s} " + "  ".join(f"{k}={v}" for k, v in metrics.items()))
    print("  [all]")
    for name, metrics in summary.items():
        print(f"    {name:9s} " + "  ".join(f"{k}={v}" for k, v in metrics.items()))
    print("\nper query (rank of the gold answer, empty = outside shortlist):")
    for row in detail:
        flag = "*" if row["degraded"] else " "
        print(
            f" {flag} [{row['set'][:3]}] retr={_fmt(row['retrieval_rank'])} laya={_fmt(row['laya_rank'])} "
            f"blend={_fmt(row['blend_rank'])}  {row['query'][:58]}"
        )
    return 0


def _fmt(value: int | None) -> str:
    return "  - " if value is None else f"{value:3d} "


if __name__ == "__main__":
    raise SystemExit(main())
