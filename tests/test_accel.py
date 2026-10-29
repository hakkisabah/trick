"""Accelerated and pure-Python scans must return identical rankings."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from trick import accel
from trick.matrix import MatrixIndex, MatrixWriter
from trick.quant import quantize

pytestmark = pytest.mark.skipif(not accel.available(), reason="numpy is not installed")


def build(path: Path, n: int = 500, seed: int = 7) -> Path:
    rng = random.Random(seed)
    with MatrixWriter(path) as writer:
        for i in range(n):
            vec = [rng.uniform(-1, 1) for _ in range(16)]
            writer.add(i + 1, vec, rng.random(), i % 3)
    return path


def test_numpy_and_python_paths_agree(tmp_path: Path) -> None:
    path = build(tmp_path / "parity.trk")
    rng = random.Random(99)
    with MatrixIndex(path) as index:
        for _ in range(20):
            query = [rng.uniform(-1, 1) for _ in range(16)]
            fast = index.scan(query, k=12)
            slow = index._scan_python(quantize(query), 12)
            assert [h.record_id for h in fast] == [h.record_id for h in slow]
            for a, b in zip(fast, slow):
                assert a.cosine == pytest.approx(b.cosine, abs=1e-5)
                assert a.score == pytest.approx(b.score, abs=1e-6)


def test_topk_matches_full_sort(tmp_path: Path) -> None:
    path = build(tmp_path / "topk.trk", n=300)
    with MatrixIndex(path) as index:
        hits = index.scan([1.0] * 16, k=300)
        assert [h.cosine for h in hits] == sorted((h.cosine for h in hits), reverse=True)
