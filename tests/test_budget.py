"""Phase budget: cold open and a top-k scan must stay interactive at 1M records.

These are the numbers the architecture promised on the cheapest host:
  * 1M records = 24 MB on disk, nothing resident until scanned;
  * cold open (stat + mmap + header parse) is O(1);
  * one scan is a single fused pass and stays well under a second.
"""

from __future__ import annotations

import random
import resource
import time
from pathlib import Path

import pytest

from trick.matrix import HEADER_SIZE, RECORD_SIZE, MatrixIndex, MatrixWriter

RECORDS = 1_000_000


@pytest.fixture(scope="module")
def big_matrix(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("big") / "scores.trk"
    rng = random.Random(1234)
    t0 = time.perf_counter()
    with MatrixWriter(path) as writer:
        for i in range(RECORDS):
            writer.add(i + 1, bytes(rng.getrandbits(8) for _ in range(16)), rng.random(), 1)
    assert time.perf_counter() - t0 < 600
    return path


def test_size_is_24_bytes_per_record(big_matrix: Path) -> None:
    assert big_matrix.stat().st_size == HEADER_SIZE + RECORDS * RECORD_SIZE
    assert big_matrix.stat().st_size == 24 * 1000 * 1000 + HEADER_SIZE


def test_cold_open_is_constant_time(big_matrix: Path) -> None:
    t0 = time.perf_counter()
    index = MatrixIndex(big_matrix)
    elapsed = time.perf_counter() - t0
    try:
        assert index.count == RECORDS
    finally:
        index.close()
    assert elapsed < 0.10, f"cold open took {elapsed * 1000:.1f} ms"


def test_scan_stays_within_budget(big_matrix: Path) -> None:
    query = bytes(random.Random(5).getrandbits(8) for _ in range(16))
    with MatrixIndex(big_matrix) as index:
        index.scan(query, k=12)  # warm the pages
        t0 = time.perf_counter()
        hits = index.scan(query, k=12)
        elapsed = time.perf_counter() - t0
    assert len(hits) == 12
    assert elapsed < 0.75, f"scan took {elapsed * 1000:.1f} ms"


def test_resident_memory_stays_bounded(big_matrix: Path) -> None:
    """Scanning must not materialise the whole index in the heap."""
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB on Linux
    with MatrixIndex(big_matrix) as index:
        for _ in range(3):
            index.scan(bytes(range(16)), k=12)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    growth_mb = (after - before) / 1024
    assert growth_mb < 150, f"heap grew {growth_mb:.1f} MB"
