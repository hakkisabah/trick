"""Measure query expansion on the catalogue gold sets.

The open question from the ranking phase: raw text misses queries whose words
never appear in a package description (``data.table`` is titled "Extension of
data.frame"). Expansion is the usual answer, but every expansion also adds
noise, so this harness compares four rewriters on one identical path.

All four variants produce an FTS5 expression, which then goes through exactly
the production stages two and four:

    expression -> BM25 candidates -> BM25 restricted rescore -> prior mix
               -> source quota -> top N

Rewriters:

    baseline  the query as typed (current behaviour)
    prefix    tokens of five letters or more are also matched as prefixes, so
              inflections and compounds are reached (generic, no vocabulary)
    dict      a hand-written abbreviation/synonym table for scientific English
              (R, statistics, bioinformatics). Declared bias: written knowing
              the gold queries, so read it as an upper bound
    prf       pseudo-relevance feedback: the first pass's top three documents
              contribute their most distinctive terms (tf * idf over the store).
              Corpus-driven, nothing hand-written

Retrieval only: expansion changes which candidates exist, and that is what this
measures. Ranking with laya on top is ``eval_ranking.py``.

    .venv/bin/python scripts/eval_expansion.py
    .venv/bin/python scripts/eval_expansion.py --variant prf --json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trick.search import DEFAULT_SHORTLIST, SearchEngine  # noqa: E402
from trick.textindex import DocumentStore, _fts_expression  # noqa: E402

DEFAULT_DB = Path.home() / ".local" / "share" / "trick" / "trick.db"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_ranking import GOLD_SETS  # noqa: E402

STOP = set(
    """a an and are as at be by for from has have how in into is it its of on or
    that the their them then there these this to using was were what when where
    which who why with without""".split()
)

# Abbreviations and spellings a scientific catalogue does not spell out.
# Written once from domain vocabulary, not tuned per query after a run.
DICT: dict[str, tuple[str, ...]] = {
    "rna-seq": ("rnaseq", "rna", "sequencing"),
    "rnaseq": ("rna", "sequencing"),
    "differential": ("differential", "expression"),
    "de": ("differential", "expression"),
    "mixed": ("mixed", "effects"),
    "glm": ("generalized", "linear", "model"),
    "gam": ("generalized", "additive", "model"),
    "pca": ("principal", "component", "analysis"),
    "svm": ("support", "vector", "machine"),
    "hmm": ("hidden", "markov", "model"),
    "mcmc": ("markov", "chain", "monte", "carlo"),
    "ts": ("time", "series"),
    "arima": ("arima", "time", "series"),
    "survival": ("survival", "hazard"),
    "cox": ("cox", "hazard", "survival"),
    "copula": ("copula", "dependence"),
    "lasso": ("lasso", "regularized", "penalized"),
    "elastic": ("elastic", "net", "regularized"),
    "forest": ("forest", "random"),
    "bayes": ("bayes", "bayesian"),
    "spatial": ("spatial", "geostatistics"),
}


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-zA-Z+#]+", text.lower()) if len(t) >= 2]


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def expression_of(terms: list[str], *, prefixes: tuple[str, ...] = ()) -> str:
    """OR-expression of plain terms plus prefix terms, deduplicated in order."""
    parts: list[str] = []
    for term in terms:
        quoted = _quote(term)
        if quoted not in parts:
            parts.append(quoted)
    for term in prefixes:
        part = _quote(term) + "*"
        if part not in parts:
            parts.append(part)
    return " OR ".join(parts)


def expr_baseline(query: str, store: DocumentStore) -> str:
    return _fts_expression(query)


def expr_prefix(query: str, store: DocumentStore) -> str:
    terms = tokens(query)
    prefixes = tuple(t for t in terms if len(t) >= 5 and t not in STOP)
    return expression_of(terms, prefixes=prefixes)


def expr_dict(query: str, store: DocumentStore) -> str:
    terms = tokens(query)
    extra: list[str] = []
    for tok in terms:
        for term in DICT.get(tok, ()):
            if term not in extra:
                extra.append(term)
    return expression_of(terms + extra)


def expr_prf(query: str, store: DocumentStore) -> str:
    """Append the three most distinctive terms of the first pass's top documents."""
    hits = store.bm25(query, limit=3)
    terms = tokens(query)
    if not hits:
        return expression_of(terms)
    seen = set(terms)
    counts: dict[str, int] = {}
    for doc_id, _score in hits:
        doc = store.get(doc_id)
        if doc is None:
            continue
        for tok in set(tokens(f"{doc.title} {doc.summary}")):
            if len(tok) < 5 or tok in STOP or tok in seen or tok.isdigit():
                continue
            counts[tok] = counts.get(tok, 0) + 1
    if not counts:
        return expression_of(terms)
    total = max(1, store.count())
    scored: list[tuple[float, str]] = []
    for tok, tf in counts.items():
        df = _doc_freq(store, tok)
        # A term present in a tenth of the corpus cannot narrow anything.
        if df <= 0 or df > total * 0.10:
            continue
        scored.append((tf * math.log(total / df), tok))
    scored.sort(reverse=True)
    return expression_of(terms + [tok for _score, tok in scored[:3]])


def _doc_freq(store: DocumentStore, term: str) -> int:
    expression = _fts_expression(term)
    if not expression or store.conn is None:
        return 0
    try:
        row = store.conn.execute(
            "SELECT count(*) AS n FROM docs_fts WHERE docs_fts MATCH ?", (expression,)
        ).fetchone()
    except Exception:  # noqa: BLE001 - a probe must not break the run
        return 0
    return int(row["n"]) if row else 0


VARIANTS = {
    "baseline": expr_baseline,
    "prefix": expr_prefix,
    "dict": expr_dict,
    "prf": expr_prf,
}


def bind(rewrite, store: DocumentStore):
    """Give the engine a one-argument rewriter, keeping the variant's name."""

    def rewritten(query: str) -> str:
        return rewrite(query, store)

    rewritten.__name__ = rewrite.__name__.replace("expr_", "")
    return rewritten


def rank(engine: SearchEngine, query: str, limit: int) -> list[tuple[str, str]]:
    """Production path with the model's weight at zero: pure retrieval ordering."""
    outcome = engine.search(query, limit=limit, use_live=False, scope="sources")
    return [(row.source, row.key) for row in outcome.rows]


def metrics(ranked: list[tuple[str, str]], gold: tuple[str, str]) -> tuple[int, float, float]:
    hit_rank = None
    for position, item in enumerate(ranked, start=1):
        if item == gold:
            hit_rank = position
            break
    recall = 1.0 if hit_rank is not None else 0.0
    mrr = 1.0 / hit_rank if hit_rank else 0.0
    ndcg = 1.0 / math.log2(hit_rank + 1) if hit_rank and hit_rank <= 10 else 0.0
    return int(recall), mrr, ndcg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="measure query expansion")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=DEFAULT_SHORTLIST)
    parser.add_argument("--variant", default=",".join(VARIANTS), help="comma separated")
    parser.add_argument("--matrix", default="", help="score matrix path (empty: no matrix)")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    args = parser.parse_args(argv)

    wanted = [name.strip() for name in args.variant.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in VARIANTS]
    if unknown:
        print(f"unknown variant(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    store = DocumentStore(args.db)
    try:
        results: dict[str, dict[str, float]] = {}
        per_set: dict[str, dict[str, list[float]]] = {}
        detail: list[dict] = []
        for name in wanted:
            rewrite = VARIANTS[name]
            # Same pipeline, same constants, one rewriter swapped in.
            engine = SearchEngine(
                store,
                weights=(0.0, 1.0),
                use_cache=False,
                gate_margin=0.0,
                matrix_path=args.matrix or None,
                expand=bind(rewrite, store),
            )
            totals = {"recall": 0.0, "mrr": 0.0, "ndcg": 0.0}
            count = 0
            for set_name, queries in GOLD_SETS.items():
                bucket = per_set.setdefault(name, {}).setdefault(set_name, [0.0, 0.0, 0.0, 0.0])
                for query, source, key in queries:
                    try:
                        expression = engine._expression(query)
                        ranked = rank(engine, query, args.limit)
                    except Exception as exc:  # noqa: BLE001 - report, do not hide
                        print(f"{name}: {query!r} failed: {exc}", file=sys.stderr)
                        expression, ranked = "", []
                    recall, mrr, ndcg = metrics(ranked, (source, key))
                    totals["recall"] += recall
                    totals["mrr"] += mrr
                    totals["ndcg"] += ndcg
                    count += 1
                    bucket[0] += recall
                    bucket[1] += mrr
                    bucket[2] += ndcg
                    bucket[3] += 1
                    detail.append(
                        {
                            "variant": name,
                            "set": set_name,
                            "query": query,
                            "expression": expression,
                            "gold": f"{source}/{key}",
                            "found_at": next(
                                (i for i, item in enumerate(ranked, 1) if item == (source, key)), None
                            ),
                        }
                    )
            results[name] = {k: round(v / max(1, count), 4) for k, v in totals.items()}

        summary = {
            "limit": args.limit,
            "documents": store.count(),
            "variants": results,
            "by_set": {
                name: {
                    set_name: {
                        "recall": round(bucket[0] / max(1, bucket[3]), 4),
                        "mrr": round(bucket[1] / max(1, bucket[3]), 4),
                        "ndcg": round(bucket[2] / max(1, bucket[3]), 4),
                        "queries": int(bucket[3]),
                    }
                    for set_name, bucket in sets.items()
                }
                for name, sets in per_set.items()
            },
        }
        if args.json:
            summary["detail"] = detail
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(f"documents: {summary['documents']}  limit: {args.limit}")
            print(f"{'variant':10} {'recall':>7} {'mrr':>7} {'ndcg':>7}")
            for name, row in results.items():
                print(f"{name:10} {row['recall']:>7.3f} {row['mrr']:>7.3f} {row['ndcg']:>7.3f}")
            print()
            for name, sets in summary["by_set"].items():
                for set_name, row in sets.items():
                    print(
                        f"{name:10} {set_name:10} recall {row['recall']:.3f} "
                        f"mrr {row['mrr']:.3f} ndcg {row['ndcg']:.3f} ({row['queries']} queries)"
                    )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
