"""Matrix contract: layout, roundtrip, ranking, atomic publish."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trick.errors import ContractViolation, MatrixCorrupt, MatrixMissing, StorageIO
from trick.matrix import (
    HEADER_SIZE,
    RECORD_SIZE,
    RF_SCORED,
    RF_SYNTHETIC,
    MatrixIndex,
    MatrixWriter,
)


def test_file_layout_is_exactly_header_plus_fixed_records(sample_matrix) -> None:
    path, rows = sample_matrix
    assert path.stat().st_size == HEADER_SIZE + len(rows) * RECORD_SIZE


def test_struct_layout_is_pinned() -> None:
    """Guards against platform/version drift in struct format codes."""
    from trick.matrix import HEADER_STRUCT, RECORD_STRUCT

    assert HEADER_STRUCT.size == 28
    assert RECORD_STRUCT.size == 24
    assert HEADER_SIZE == 48
    assert isinstance(HEADER_STRUCT.unpack(b"\x00" * 28)[-1], float)


def test_roundtrip_preserves_ids_scores_and_flags(sample_matrix) -> None:
    path, rows = sample_matrix
    with MatrixIndex(path) as index:
        assert index.count == len(rows)
        got = {rid: (score, flags) for rid, _sig, score, flags in index.iter_records()}
    assert got[10][0] == pytest.approx(0.90, abs=1e-4)
    assert got[10][1] == RF_SCORED
    assert got[30][1] == 0x00
    assert [rid for rid in got] == [10, 20, 30]


def test_scan_orders_by_cosine_then_id(sample_matrix) -> None:
    path, _ = sample_matrix
    query = [1.0, 0.0] + [0.0] * 14
    with MatrixIndex(path) as index:
        hits = index.scan(query, k=3)
    assert [h.record_id for h in hits] == [10, 20, 30]
    assert hits[0].cosine == pytest.approx(1.0, abs=1e-3)
    assert hits[0].cosine > hits[1].cosine
    assert hits[1].cosine == pytest.approx(0.0, abs=1e-3)  # orthogonal to the query
    assert hits[2].cosine == pytest.approx(0.0, abs=1e-3)
    assert hits[0].tested is True
    assert hits[2].tested is False


def test_scan_respects_k_and_empty_cases(sample_matrix) -> None:
    path, _ = sample_matrix
    query = [1.0] + [0.0] * 15
    with MatrixIndex(path) as index:
        assert len(index.scan(query, k=1)) == 1
        assert len(index.scan(query, k=99)) == 3
        assert index.scan(query, k=0) == []


def test_duplicate_ids_are_refused(matrix_path: Path) -> None:
    with pytest.raises(ContractViolation):
        with MatrixWriter(matrix_path) as writer:
            writer.add(1, [1.0] * 16)
            writer.add(1, [0.0] * 16)


@pytest.mark.parametrize("bad_id", [-1, 2**32])
def test_out_of_range_ids_are_refused(matrix_path: Path, bad_id: int) -> None:
    with pytest.raises(ContractViolation):
        with MatrixWriter(matrix_path) as writer:
            writer.add(bad_id, [1.0] * 16)


def test_wrong_signature_length_is_refused(matrix_path: Path) -> None:
    with pytest.raises(ContractViolation):
        with MatrixWriter(matrix_path) as writer:
            writer.add(1, b"\x01\x02")


def test_abort_leaves_no_published_file(matrix_path: Path) -> None:
    writer = MatrixWriter(matrix_path)
    writer.open()
    writer.add(1, [1.0] * 16)
    writer.abort()
    assert not matrix_path.exists()
    assert not matrix_path.with_suffix(".trk.tmp").exists()


def test_finish_is_atomic_and_cleans_tmp(matrix_path: Path) -> None:
    with MatrixWriter(matrix_path) as writer:
        writer.add(7, [0.0] * 15 + [1.0])
    assert matrix_path.exists()
    assert not Path(str(matrix_path) + ".tmp").exists()


def test_publish_failure_keeps_previous_matrix(matrix_path: Path, monkeypatch) -> None:
    with MatrixWriter(matrix_path) as writer:
        writer.add(1, [1.0] * 16)
    before = matrix_path.read_bytes()

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    writer = MatrixWriter(matrix_path)
    writer.open()
    writer.add(2, [0.0] * 16)
    with pytest.raises(StorageIO):
        writer.finish()

    assert matrix_path.read_bytes() == before
    with MatrixIndex(matrix_path) as index:
        assert index.count == 1


def test_missing_file_raises_matrixmissing(tmp_path: Path) -> None:
    with pytest.raises(MatrixMissing):
        MatrixIndex(tmp_path / "nope.trk")


def test_empty_matrix_is_valid(matrix_path: Path) -> None:
    with MatrixWriter(matrix_path) as writer:
        pass
    with MatrixIndex(matrix_path) as index:
        assert index.count == 0
        assert index.scan([1.0] * 16) == []
        index.verify()


def test_verify_detects_body_tampering(matrix_path: Path) -> None:
    with MatrixWriter(matrix_path) as writer:
        for i in range(3):
            writer.add(i + 1, [float(i), 0.0] + [0.0] * 14, 0.5)
    raw = bytearray(matrix_path.read_bytes())
    raw[HEADER_SIZE + 5] ^= 0xFF
    matrix_path.write_bytes(bytes(raw))
    with MatrixIndex(matrix_path) as index:
        with pytest.raises(MatrixCorrupt) as info:
            index.verify()
    assert info.value.reason == "crc-mismatch"


def test_flags_roundtrip(matrix_path: Path) -> None:
    with MatrixWriter(matrix_path) as writer:
        writer.add(1, [1.0] * 16, 0.5, RF_SCORED | RF_SYNTHETIC)
    with MatrixIndex(matrix_path) as index:
        rid, _sig, _score, flags = next(index.iter_records())
    assert rid == 1
    assert flags & RF_SYNTHETIC


def test_synthetic_header_flag(matrix_path: Path) -> None:
    from trick.matrix import HF_SYNTHETIC

    with MatrixWriter(matrix_path, header_flags=HF_SYNTHETIC) as writer:
        writer.add(1, [1.0] * 16)
    with MatrixIndex(matrix_path) as index:
        assert index.synthetic is True
