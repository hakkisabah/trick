"""Shared fixtures. The package is used from ``src/`` without installation."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def rng() -> random.Random:
    return random.Random(20261029)


@pytest.fixture()
def matrix_path(tmp_path: Path) -> Path:
    return tmp_path / "scores.trk"


@pytest.fixture()
def sample_matrix(matrix_path: Path) -> tuple[Path, list[tuple[int, list[float], float, int]]]:
    """Three records with orthogonal-ish vectors and distinct scores."""
    from trick.matrix import MatrixWriter

    rows = [
        (10, [1.0, 0.0] + [0.0] * 14, 0.90, 0x01),
        (20, [0.0, 1.0] + [0.0] * 14, 0.40, 0x01),
        (30, [0.0, 0.0, 1.0] + [0.0] * 13, 0.00, 0x00),
    ]
    with MatrixWriter(matrix_path) as writer:
        for row in rows:
            writer.add(*row)
    return matrix_path, rows
