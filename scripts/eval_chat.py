"""Measure conversation search: does it find the passage the user means?

There is no public gold set of engineering chats, so this script builds one with
known answers. Two properties make it adversarial in the way real chats are:

* the filler repeats itself, so a ranking that likes repeated text is punished;
* every needle has two *decoys* in the same conversation that share its rare
  vocabulary but answer a different question. A keyword sort cannot separate
  them; that is the case where the model has something to add.

Each needle is one sentence planted at a known turn. The query is what a user
types three days later: the two or three words they remember, not the sentence.

Reported per configuration:

* recall@3   - the needle passage is in the top 3
* MRR@10     - 1/rank of the needle, averaged over queries
* decoy@1    - the top hit is a decoy, i.e. confident and wrong
* laya calls - how often the gate let the model run
* p50/p95    - elapsed milliseconds

Usage::

    python scripts/eval_chat.py                 # mock scorer (deterministic)
    python scripts/eval_chat.py --scorer remote # the live laya endpoint
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trick.laya import build_scorer  # noqa: E402
from trick.transcript import ConversationSearch, Message, normalise_messages  # noqa: E402

FILLER = [
    ("user", "Remind me how we decided to store the intermediate results last week."),
    ("assistant", "One parquet file per contrast under the results directory, with the contrast name in the filename, so the report can glob them instead of reading a manifest."),
    ("user", "Does the report notebook pick those up automatically?"),
    ("assistant", "It globs the results directory and fails loudly when a contrast named in the config has no file, which is deliberate: a missing contrast should stop the report rather than silently shorten it."),
    ("user", "Keep the session info in the report header as well."),
    ("assistant", "Session info is captured once per render and written into the header block, which is enough provenance for a methods section."),
    ("user", "The batch variable is confounded with the sequencing run, is that a problem?"),
    ("assistant", "Partial confounding is workable as long as every condition appears in more than one run; complete confounding is not, and no model can fix it."),
    ("user", "How long does the fit take on forty samples?"),
    ("assistant", "A few minutes per contrast on one core; the refit loop is the expensive part, not the size factors."),
    ("user", "Can we cache the dispersions between runs?"),
    ("assistant", "Only when the sample set and the design are identical, which is rare during model selection, so caching them usually costs more than it saves."),
    ("user", "Where do the raw fastq files live now?"),
    ("assistant", "They stay on the archive tier and are only pulled back when a lane has to be re-quantified, which has happened twice."),
]

# (needle, decoy 1, decoy 2, query)
# The decoys reuse the needle's rare vocabulary and answer a *different* question,
# so a purely lexical ranking has to guess.
NEEDLES = [
    (
        "The size factors are computed with the median of ratios method, which assumes most genes are not differentially expressed, so a global shift is absorbed.",
        "Size factors are stored per sample in the column data, and they are recomputed from scratch whenever a sample is dropped.",
        "The size factor plot is written next to the dispersion plot so the two can be compared side by side in the report.",
        "assumption behind the size factors",
    ),
    (
        "The vignette warns that the independent filtering step changes the multiple testing correction, because it filters on a statistic that is independent of the p-value.",
        "Multiple testing correction is applied per contrast, and the number of tests is taken from the genes that passed the filter.",
        "Independent filtering is skipped entirely in the exploratory notebook, which is why the two sets of numbers never agree.",
        "did the filtering change the correction",
    ),
    (
        "We chose the quasi-likelihood pipeline in the second tool because it gave a comparable answer with a faster refit loop on the same matrix.",
        "The quasi-likelihood fit is slower than the Wald test on the same matrix, so we only use it for the final table.",
        "The refit loop had to be parallelised across contrasts, which is why the run time dropped from hours to minutes.",
        "why we picked the second tool for the fit",
    ),
    (
        "The heatmap clusters samples with Spearman correlation and complete linkage, not Euclidean distance, because the counts are compositional.",
        "The heatmap is drawn from the same parquet file as the table, so the two cannot disagree about a contrast.",
        "Complete linkage was replaced by Ward linkage in the second figure after the reviewer complained about the dendrogram.",
        "how samples are clustered in the heatmap",
    ),
    (
        "Library preparation batch had to be added as a second covariate after the sequencing centre changed its protocol in March.",
        "The sequencing centre changed its protocol again in June, and the new runs are marked in the sample sheet.",
        "A second covariate was considered for the RNA integrity number but dropped because it correlated with the batch.",
        "protocol change and the extra covariate",
    ),
    (
        "The surrogate variable analysis found two hidden factors, and one of them correlated with the RNA integrity number.",
        "Surrogate variable analysis is run as a sensitivity check only, never as the design of record.",
        "The RNA integrity number is recorded in the sample sheet but is not used in any model.",
        "hidden factors correlated with a quality metric",
    ),
    (
        "Allele-specific expression was dropped from scope because the phased genotypes were not available for most samples.",
        "Phased genotypes are available for the twenty samples that went through the long read pipeline.",
        "Allele-specific expression would need a different quantification, so it was never scheduled.",
        "why allele specific expression was dropped",
    ),
    (
        "The count matrix keeps the mitochondrial genes in, and they are regressed out with the percentage of mitochondrial reads.",
        "The count matrix is filtered to remove genes with fewer than ten reads in total before the model is fitted.",
        "Mitochondrial reads are counted separately in the quality report and flagged when a sample passes ten percent.",
        "what happens to mitochondrial genes",
    ),
    (
        "Kallisto quantifies against the transcriptome, so the counts are summed to gene level with the annotation from the same release.",
        "Kallisto is also used for the sensitivity run, with the same transcriptome but a different seed.",
        "Gene level counts are what the model consumes, so the annotation release has to match the one used for quantification.",
        "how quantification becomes gene level",
    ),
    (
        "We agreed the volcano plot thresholds at a fold change of one and an adjusted p-value of five percent, and the plot is generated from the parquet file.",
        "The volcano plot is the only figure that is regenerated on every render, because it is cheap.",
        "A fold change of two was used in the first draft and changed after the reviewer asked for one.",
        "thresholds used in the volcano plot",
    ),
]


def build_chat() -> tuple[list[Message], list[int], set[int]]:
    """A long chat with each needle planted at a turn we can name exactly."""
    turns: list[dict] = []
    planted: list[int] = []
    decoys: set[int] = set()
    filler_index = 0
    for needle, decoy_one, decoy_two, _ in NEEDLES:
        for _ in range(3):
            for role, text in (FILLER[filler_index % len(FILLER)], FILLER[(filler_index + 1) % len(FILLER)]):
                turns.append({"role": role, "content": text})
            filler_index += 2
        # Decoys sit before the needle, spaced out, the way a long chat wanders.
        turns.append({"role": "assistant", "content": decoy_one})
        decoys.add(len(turns) - 1)
        for _ in range(2):
            for role, text in (FILLER[filler_index % len(FILLER)], FILLER[(filler_index + 1) % len(FILLER)]):
                turns.append({"role": role, "content": text})
            filler_index += 2
        turns.append({"role": "user", "content": decoy_two})
        decoys.add(len(turns) - 1)
        for role, text in (FILLER[filler_index % len(FILLER)], FILLER[(filler_index + 1) % len(FILLER)]):
            turns.append({"role": role, "content": text})
        filler_index += 2
        turns.append({"role": "assistant", "content": needle})
        planted.append(len(turns) - 1)
    return normalise_messages(turns), planted, decoys


def run(scorer, *, weights, gate: float, label: str) -> dict:
    messages, planted, decoys = build_chat()
    search = ConversationSearch(scorer, weights=weights, gate_margin=gate)
    ranks: list[int | None] = []
    elapsed: list[float] = []
    laya_calls = 0
    decoy_top = 0
    for slot, (_, _, _, query) in enumerate(NEEDLES):
        started = time.monotonic()
        rows, report = search.search(query, messages, limit=10)
        elapsed.append((time.monotonic() - started) * 1000.0)
        if report.get("scored_by_laya"):
            laya_calls += 1
        ranks.append(next((row.rank for row in rows if row.turn == planted[slot]), None))
        if rows and rows[0].turn in decoys:
            decoy_top += 1
    hits = sum(1 for rank in ranks if rank is not None and rank <= 3)
    reciprocal = [0.0 if rank is None else 1.0 / rank for rank in ranks]
    ordered = sorted(elapsed)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return {
        "configuration": label,
        "queries": len(NEEDLES),
        "turns": len(messages),
        "recall@3": round(hits / len(NEEDLES), 3),
        "mrr@10": round(sum(reciprocal) / len(NEEDLES), 3),
        "decoy@1": round(decoy_top / len(NEEDLES), 3),
        "laya_calls": laya_calls,
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "ranks": ranks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure conversation search quality.")
    parser.add_argument("--scorer", default="mock", choices=["mock", "remote", "auto"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    scorer = build_scorer(args.scorer)
    results = [
        run(scorer, weights=(1.0, 0.0), gate=0.0, label="model only (retrieval weight 0)"),
        run(scorer, weights=(0.0, 1.0), gate=0.0, label="retrieval only (model weight 0)"),
        run(scorer, weights=(0.2, 0.8), gate=0.0, label="blend 0.2/0.8, gate open"),
        run(scorer, weights=(0.2, 0.8), gate=0.15, label="blend 0.2/0.8, gate 0.15 (shipping)"),
        run(scorer, weights=(0.5, 0.5), gate=0.15, label="blend 0.5/0.5, gate 0.15"),
    ]
    if args.json:
        print(json.dumps({"scorer": scorer.name, "results": results}, indent=1))
        return 0
    print(f"scorer={scorer.name}  queries={len(NEEDLES)}  turns={results[0]['turns']}")
    header = f"{'configuration':<36} {'recall@3':>8} {'MRR@10':>7} {'decoy@1':>8} {'laya':>5} {'p50 ms':>7} {'p95 ms':>7}"
    print(header)
    for item in results:
        print(
            f"{item['configuration']:<36} {item['recall@3']:>8.3f} {item['mrr@10']:>7.3f} {item['decoy@1']:>8.3f} "
            f"{item['laya_calls']:>5} {item['p50_ms']:>7.1f} {item['p95_ms']:>7.1f}"
        )
    for item in results:
        print(f"  {item['configuration']}: ranks {item['ranks']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
