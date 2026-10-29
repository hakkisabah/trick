"""Writer contention and fallback loading behaviour."""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from trick.emergency import SEED_CATALOGUE, build_emergency_matrix, catalogue_digest, catalogue_text, open_or_emergency
from trick.errors import Contention, MatrixCorrupt, MatrixMissing
from trick.matrix import RF_SCORED, RF_SYNTHETIC, MatrixIndex, MatrixWriter


def test_second_writer_is_refused(matrix_path: Path) -> None:
    first = MatrixWriter(matrix_path)
    first.open()
    try:
        with pytest.raises(Contention):
            MatrixWriter(matrix_path).open()
    finally:
        first.abort()


def test_lock_is_released_after_finish(matrix_path: Path) -> None:
    with MatrixWriter(matrix_path) as writer:
        writer.add(1, [1.0] * 16)
    with MatrixWriter(matrix_path) as writer:
        writer.add(1, [1.0] * 16)
    with MatrixIndex(matrix_path) as index:
        assert index.count == 1


def _hold_lock(path: str, ready) -> None:  # pragma: no cover - worker
    writer = MatrixWriter(path)
    writer.open()
    ready.set()
    time.sleep(1.5)
    writer.abort()


def test_lock_blocks_other_processes(matrix_path: Path) -> None:  # pragma: no cover - needs fork
    ctx = mp.get_context("fork")
    ready = ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(str(matrix_path), ready))
    proc.start()
    try:
        assert ready.wait(5), "worker never acquired the lock"
        with pytest.raises(Contention):
            MatrixWriter(matrix_path).open()
    finally:
        proc.join(10)
        if proc.is_alive():
            proc.terminate()


def test_emergency_catalogue_size_is_bounded() -> None:
    """The fallback must stay small: it is shipped inside the wheel."""
    assert 40 <= len(SEED_CATALOGUE) <= 128
    assert len(set(SEED_CATALOGUE)) == len(SEED_CATALOGUE)


def test_emergency_matrix_is_flagged_synthetic_and_unscored(tmp_path: Path) -> None:
    path = build_emergency_matrix(tmp_path / "emergency.trk")
    assert path.stat().st_size < 8192
    with MatrixIndex(path) as index:
        assert index.count == len(SEED_CATALOGUE)
        assert index.synthetic is True
        for _rid, _sig, score, flags in index.iter_records():
            assert flags & RF_SYNTHETIC
            assert not flags & RF_SCORED
            assert score == 0.0


def test_emergency_digest_is_stable() -> None:
    assert catalogue_digest() == catalogue_digest()
    assert len(catalogue_digest()) == 8


def test_catalogue_lookup(tmp_path: Path) -> None:
    assert catalogue_text(1) == SEED_CATALOGUE[0]
    assert catalogue_text(0) is None
    assert catalogue_text(len(SEED_CATALOGUE) + 1) is None


def test_missing_matrix_falls_back(tmp_path: Path) -> None:
    result = open_or_emergency(tmp_path / "absent.trk")
    try:
        assert result.source == "emergency"
        assert isinstance(result.error, MatrixMissing)
        assert result.degraded is True
        assert len(result.index) == len(SEED_CATALOGUE)
    finally:
        result.index.close()


def test_corrupt_matrix_falls_back(tmp_path: Path) -> None:
    bad = tmp_path / "scores.trk"
    bad.write_bytes(b"TRKMAT\x01\x00" + b"\x00" * 4)
    result = open_or_emergency(bad, emergency_path=tmp_path / "fallback.trk")
    try:
        assert result.source == "emergency"
        assert isinstance(result.error, MatrixCorrupt)
    finally:
        result.index.close()


def test_valid_matrix_is_preferred(tmp_path: Path) -> None:
    good = tmp_path / "scores.trk"
    with MatrixWriter(good) as writer:
        writer.add(1, [1.0] * 16, 0.8, RF_SCORED)
    result = open_or_emergency(good)
    try:
        assert result.source == "file"
        assert result.error is None
        assert result.degraded is False
        assert result.index.count == 1
    finally:
        result.index.close()
