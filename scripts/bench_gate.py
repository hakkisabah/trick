"""Gate step: the latency budgets, measured on this host, not promised.

Three numbers decide whether Trick is usable on the cheapest machine in the
fleet, and all three are the kind that rot silently:

* cold start of the score matrix - a user pressing the shortcut must not wait;
* one top-k scan at the size the design targets (200k records here; the unit
  budget test covers 1M with a much looser bound);
* one conversation search over a long chat - the actual feature.

The thresholds are deliberately loose multipliers of the measured values (see
``scripts/bench.py`` for the raw numbers), because a gate that fails on a busy
CI box teaches people to ignore it. What it does catch is an accidental O(n) in
the open path, a lost mmap, or the model being called when the gate should have
skipped it.

Usage::

    python scripts/bench_gate.py [--records 200000] [--queries 40]
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trick.laya import MockScorer  # noqa: E402
from trick.matrix import MatrixIndex, MatrixWriter  # noqa: E402
from trick.quant import SIG_DIM, quantize  # noqa: E402
from trick.transcript import ConversationSearch, normalise_messages  # noqa: E402

# Budgets. p50/p95 in milliseconds unless stated otherwise.
COLD_OPEN_P50_MS = 5.0
COLD_OPEN_P95_MS = 15.0
SCAN_P50_MS = 400.0
SCAN_P95_MS = 900.0
CHAT_P50_MS = 600.0
CHAT_P95_MS = 1200.0
PYTHON_RECORD_US = 40.0  # per record, pure-Python fallback (numpy missing)

FILLER = [
    ("user", "Where do the intermediate results go after the fit finishes?"),
    ("assistant", "One file per contrast under results/, named after the contrast, so the report can glob the directory instead of reading a manifest."),
    ("user", "And if a contrast in the config has no file?"),
    ("assistant", "The report stops with an error naming the missing contrast; a silently shortened report is worse than a failed render."),
]


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def build(path: Path, records: int) -> float:
    rng = random.Random(99)
    started = time.perf_counter()
    with MatrixWriter(path) as writer:
        for i in range(records):
            writer.add(i + 1, bytes(rng.getrandbits(8) for _ in range(SIG_DIM)), rng.random(), 1)
    return time.perf_counter() - started


def transcript(turns: int) -> list[dict]:
    messages = []
    for i in range(turns):
        role, content = FILLER[i % len(FILLER)]
        messages.append({"role": role, "content": f"{content} (turn {i + 1})"})
    needle = "The tolerance for the dispersion fit is one part in ten thousand, and it is set in the config under fit.tolerance."
    messages[turns - 5] = {"role": "assistant", "content": needle}
    return messages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=200_000)
    parser.add_argument("--queries", type=int, default=40)
    args = parser.parse_args()

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scores.trk"
        build_seconds = build(path, args.records)
        size = path.stat().st_size

        cold: list[float] = []
        for _ in range(20):
            started = time.perf_counter()
            index = MatrixIndex(path)
            cold.append((time.perf_counter() - started) * 1000.0)
            index.close()

        signature = quantize([1.0] + [0.0] * (SIG_DIM - 1))
        scans: list[float] = []
        index = MatrixIndex(path)
        try:
            for _ in range(args.queries):
                started = time.perf_counter()
                hits = index.scan(signature, k=12)
                scans.append((time.perf_counter() - started) * 1000.0)
                assert len(hits) == 12
        finally:
            index.close()

        # The fallback path is what a host without numpy runs, and it is the one
        # that decides how large an index the cheapest machine can serve.
        probe = MatrixIndex(path)
        try:
            started = time.perf_counter()
            probe._scan_python(signature, 12)
            fallback_us = (time.perf_counter() - started) * 1e6 / max(1, probe.count)
        finally:
            probe.close()

        messages = normalise_messages(transcript(150))
        chat = ConversationSearch(MockScorer())
        chat_latency: list[float] = []
        for _ in range(10):
            started = time.perf_counter()
            chat.search("dispersion fit tolerance", messages, limit=10)
            chat_latency.append((time.perf_counter() - started) * 1000.0)

    report = {
        "records": args.records,
        "bytes": size,
        "build_s": round(build_seconds, 2),
        "cold_open_ms": [round(percentile(cold, 0.5), 2), round(percentile(cold, 0.95), 2)],
        "scan_ms": [round(percentile(scans, 0.5), 1), round(percentile(scans, 0.95), 1)],
        "python_us_per_record": round(fallback_us, 2),
        "chat_ms": [round(percentile(chat_latency, 0.5), 1), round(percentile(chat_latency, 0.95), 1)],
    }
    print(
        "budget: %d records, %.1f MB, cold open p50 %.2f ms p95 %.2f ms, "
        "scan p50 %.0f ms p95 %.0f ms, chat p50 %.0f ms p95 %.0f ms, "
        "pure-python %.1f us/record"
        % (
            report["records"],
            size / 1e6,
            report["cold_open_ms"][0],
            report["cold_open_ms"][1],
            report["scan_ms"][0],
            report["scan_ms"][1],
            report["chat_ms"][0],
            report["chat_ms"][1],
            report["python_us_per_record"],
        )
    )

    if report["cold_open_ms"][0] > COLD_OPEN_P50_MS:
        failures.append(f"cold open p50 {report['cold_open_ms'][0]} ms > {COLD_OPEN_P50_MS} ms")
    if report["cold_open_ms"][1] > COLD_OPEN_P95_MS:
        failures.append(f"cold open p95 {report['cold_open_ms'][1]} ms > {COLD_OPEN_P95_MS} ms")
    if report["scan_ms"][0] > SCAN_P50_MS:
        failures.append(f"scan p50 {report['scan_ms'][0]} ms > {SCAN_P50_MS} ms")
    if report["scan_ms"][1] > SCAN_P95_MS:
        failures.append(f"scan p95 {report['scan_ms'][1]} ms > {SCAN_P95_MS} ms")
    if report["chat_ms"][0] > CHAT_P50_MS:
        failures.append(f"chat p50 {report['chat_ms'][0]} ms > {CHAT_P50_MS} ms")
    if report["chat_ms"][1] > CHAT_P95_MS:
        failures.append(f"chat p95 {report['chat_ms'][1]} ms > {CHAT_P95_MS} ms")
    if fallback_us > PYTHON_RECORD_US:
        failures.append(f"pure-python {report['python_us_per_record']} us/record > {PYTHON_RECORD_US} us")

    for failure in failures:
        print(f"budget: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
