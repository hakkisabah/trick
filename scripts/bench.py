"""Measure the F1 budgets on the real host and print a JSON block.

Usage:
    python scripts/bench.py [--records 1000000] [--queries 50] [--json out.json]

The numbers are produced, not estimated: cold open, accelerator scan latency,
pure-Python fallback throughput (the minimal mode used when numpy is absent) and
resident memory growth.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trick import accel  # noqa: E402
from trick.matrix import HEADER_SIZE, RECORD_SIZE, MatrixIndex, MatrixWriter  # noqa: E402


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def build(path: Path, records: int) -> dict:
    rng = random.Random(4242)
    t0 = time.perf_counter()
    with MatrixWriter(path) as writer:
        for i in range(records):
            writer.add(i + 1, bytes(rng.getrandbits(8) for _ in range(16)), rng.random(), 1)
    elapsed = time.perf_counter() - t0
    size = path.stat().st_size
    assert size == HEADER_SIZE + records * RECORD_SIZE
    return {
        "records": records,
        "file_bytes": size,
        "bytes_per_record": RECORD_SIZE,
        "build_seconds": round(elapsed, 2),
        "records_per_second": int(records / elapsed),
    }


def cold_open(path: Path, runs: int = 10) -> dict:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        index = MatrixIndex(path)
        count = index.count
        times.append((time.perf_counter() - t0) * 1000)
        index.close()
    return {
        "count": count,
        "runs": runs,
        "median_ms": round(statistics.median(times), 3),
        "max_ms": round(max(times), 3),
    }


def scan_latency(path: Path, queries: int, k: int = 12) -> dict:
    rng = random.Random(9)
    qs = [bytes(rng.getrandbits(8) for _ in range(16)) for _ in range(queries)]
    with MatrixIndex(path) as index:
        index.scan(qs[0], k=k)  # warm
        times = []
        for q in qs:
            t0 = time.perf_counter()
            index.scan(q, k=k)
            times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    p95 = times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]
    return {
        "engine": "numpy" if accel.available() else "python",
        "k": k,
        "queries": queries,
        "p50_ms": round(statistics.median(times), 2),
        "p95_ms": round(p95, 2),
        "max_ms": round(max(times), 2),
    }


def python_path_throughput(path: Path, sample: int = 20000) -> dict:
    """Time the stdlib-only scan so the minimal mode (no numpy) is measurable."""
    from trick.matrix import RECORD_STRUCT
    from trick.quant import dot_int8

    rng = random.Random(3)
    q = bytes(rng.getrandbits(8) for _ in range(16))
    with MatrixIndex(path) as index:
        count = min(index.count, sample)
        body = index.body[: count * RECORD_SIZE]
        t0 = time.perf_counter()
        for rid, sig, raw, flags in RECORD_STRUCT.iter_unpack(body):
            dot_int8(q, sig)
        elapsed = time.perf_counter() - t0
    per_record_us = (elapsed / count) * 1_000_000
    return {
        "sampled_records": count,
        "seconds": round(elapsed, 3),
        "microseconds_per_record": round(per_record_us, 3),
        "projected_seconds_at_1m": round(per_record_us * 1_000_000 / 1_000_000, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, default=1_000_000)
    ap.add_argument("--queries", type=int, default=50)
    ap.add_argument("--json", type=str, default="")
    args = ap.parse_args()

    data_dir = Path(os.environ.get("TRICK_BENCH_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "bench.trk"

    result = {
        "host": {
            "python": sys.version.split()[0],
            "numpy": accel.numpy_module().__version__ if accel.available() else None,
        },
        "build": build(path, args.records),
        "cold_open": cold_open(path),
        "scan": scan_latency(path, args.queries),
        "pure_python": python_path_throughput(path),
        "max_rss_mb": round(rss_mb(), 1),
    }
    text = json.dumps(result, indent=2)
    print(text)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
