"""Shortlist quota harness: does reserving slots cost anything?

The quota exists because of a specific failure, so it is measured on that failure
and not on a proxy. Each query is retrieved from the live sources exactly once,
then every configuration is ranked from that same candidate set - otherwise the
comparison would mix a configuration change with whatever the network returned
that minute.

Reported per configuration:

    recall@12 / ndcg@10   the gold answer's position (all gold answers are local,
                          so this metric is structurally biased *towards* a high
                          floor - read it as a regression guard, not as evidence)
    zero_local            queries whose shortlist contains no local catalogue
                          entry at all. This is the failure the quota fixes.
    distinct              mean number of distinct sources in the shortlist
    local_share           mean share of local entries in the shortlist

    .venv/bin/python scripts/eval_quota.py --scorer mock
    .venv/bin/python scripts/eval_quota.py --scorer live --configs 0/0,4/4
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trick.laya import build_scorer  # noqa: E402
from trick.retrieval import live_adapters  # noqa: E402
from trick.search import LOCAL_SOURCES, SearchEngine  # noqa: E402
from trick.textindex import DocumentStore  # noqa: E402

DEFAULT_DB = Path.home() / ".local" / "share" / "trick" / "trick.db"

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


class MemoAdapter:
    """Fetch each (adapter, query) pair once, then replay it."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.name = inner.name
        self.cache: dict[str, list] = {}
        self.calls = 0

    def search(self, query, limit=5, timeout=1.5):
        if query not in self.cache:
            self.calls += 1
            self.cache[query] = list(self.inner.search(query, limit=limit, timeout=timeout))
        return list(self.cache[query])


def ndcg(rank: int | None, k: int = 10) -> float:
    if rank is None or rank > k:
        return 0.0
    return 1.0 / math.log2(rank + 1)


def parse_config(text: str) -> tuple[int, int]:
    floor, cap = text.split("/")
    return int(floor), int(cap)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--scorer", default="mock", choices=["mock", "live", "remote", "auto"])
    parser.add_argument("--set", dest="gold_set", default="both", choices=["canonical", "niche", "both"])
    parser.add_argument("--configs", default="0/0,3/4,4/4,4/6,6/4")
    parser.add_argument("--shortlist", type=int, default=12)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    configs = [parse_config(item) for item in args.configs.split(",") if item.strip()]
    names = ["canonical", "niche"] if args.gold_set == "both" else [args.gold_set]
    queries = [(name,) + item for name in names for item in GOLD_SETS[name]]

    store = DocumentStore(args.db)
    adapters = [MemoAdapter(adapter) for adapter in live_adapters()]
    results: list[dict] = []

    for floor, cap in configs:
        scorer = build_scorer(args.scorer)
        engine = SearchEngine(
            store,
            scorer,
            adapters=adapters,
            shortlist=args.shortlist,
            use_cache=False,
            local_floor=floor,
            live_cap=cap,
        )
        rows = []
        started = time.monotonic()
        for set_name, query, gold_source, gold_key in queries:
            outcome = engine.search(query, limit=args.shortlist)
            rank = None
            for row in outcome.rows:
                if row.source == gold_source and row.key == gold_key:
                    rank = row.rank
                    break
            sources = [row.source for row in outcome.rows]
            local = sum(1 for source in sources if source in LOCAL_SOURCES)
            rows.append(
                {
                    "set": set_name,
                    "query": query,
                    "gold": f"{gold_source}/{gold_key}",
                    "rank": rank,
                    "sources": sources,
                    "local": local,
                    "distinct": len(set(sources)),
                }
            )
        elapsed = time.monotonic() - started
        summary = {
            "config": f"{floor}/{cap}",
            "recall": round(sum(1 for row in rows if row["rank"]) / len(rows), 3),
            "ndcg": round(sum(ndcg(row["rank"]) for row in rows) / len(rows), 3),
            "zero_local": sum(1 for row in rows if row["local"] == 0),
            "distinct": round(sum(row["distinct"] for row in rows) / len(rows), 2),
            "local_share": round(sum(row["local"] for row in rows) / (len(rows) * args.shortlist), 3),
            "elapsed_s": round(elapsed, 1),
            "rows": rows,
        }
        results.append(summary)
        print(
            f"floor/cap {summary['config']:>5}  recall@12 {summary['recall']:.3f}  "
            f"ndcg@10 {summary['ndcg']:.3f}  zero_local {summary['zero_local']:2d}/{len(rows)}  "
            f"distinct {summary['distinct']:.2f}  local_share {summary['local_share']:.3f}  "
            f"({summary['elapsed_s']}s)"
        )

    live_calls = sum(adapter.calls for adapter in adapters)
    print(f"\nlive adapter calls: {live_calls} (memoised across {len(configs)} configs)")
    for summary in results:
        per_set: dict[str, list[dict]] = {}
        for row in summary["rows"]:
            per_set.setdefault(row["set"], []).append(row)
        detail = "  ".join(
            f"{name}: recall {sum(1 for r in items if r['rank']) / len(items):.3f}"
            for name, items in per_set.items()
        )
        print(f"  {summary['config']}: {detail}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
