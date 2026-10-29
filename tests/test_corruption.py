"""Corruption matrix: every malformed file must be classified, never guessed."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from trick.errors import MatrixCorrupt, StorageIO
from trick.matrix import HEADER_SIZE, RECORD_SIZE, MatrixIndex, MatrixWriter


def build(path: Path, n: int = 4) -> Path:
    with MatrixWriter(path) as writer:
        for i in range(n):
            vec = [0.0] * 16
            vec[i % 16] = 1.0
            writer.add(i + 1, vec, i / 10)
    return path


@pytest.mark.parametrize(
    "size,reason",
    [
        (0, "truncated-header"),
        (8, "truncated-header"),
        (HEADER_SIZE - 1, "truncated-header"),
    ],
)
def test_short_files_are_classified_as_truncated(tmp_path: Path, size: int, reason: str) -> None:
    path = tmp_path / "short.trk"
    path.write_bytes(b"\x00" * size)
    with pytest.raises(MatrixCorrupt) as info:
        MatrixIndex(path)
    assert info.value.reason == reason


def test_truncated_body_is_detected(tmp_path: Path) -> None:
    path = build(tmp_path / "body.trk")
    data = path.read_bytes()
    path.write_bytes(data[:-RECORD_SIZE])
    with pytest.raises(MatrixCorrupt) as info:
        MatrixIndex(path)
    assert info.value.reason == "size-mismatch"


def test_trailing_garbage_is_detected(tmp_path: Path) -> None:
    path = build(tmp_path / "garbage.trk")
    path.write_bytes(path.read_bytes() + b"\xff" * RECORD_SIZE)
    with pytest.raises(MatrixCorrupt) as info:
        MatrixIndex(path)
    assert info.value.reason == "size-mismatch"


def test_bad_magic_is_detected(tmp_path: Path) -> None:
    path = build(tmp_path / "magic.trk")
    raw = bytearray(path.read_bytes())
    raw[0:8] = b"NOTMAT\x00\x00"
    path.write_bytes(bytes(raw))
    with pytest.raises(MatrixCorrupt) as info:
        MatrixIndex(path)
    assert info.value.reason == "bad-magic"


def test_future_version_is_rejected(tmp_path: Path) -> None:
    path = build(tmp_path / "version.trk")
    raw = bytearray(path.read_bytes())
    struct.pack_into("<H", raw, 8, 99)
    path.write_bytes(bytes(raw))
    with pytest.raises(MatrixCorrupt) as info:
        MatrixIndex(path)
    assert info.value.reason == "unsupported-version"


def test_wrong_dimension_is_rejected(tmp_path: Path) -> None:
    path = build(tmp_path / "dim.trk")
    raw = bytearray(path.read_bytes())
    raw[10] = 32
    path.write_bytes(bytes(raw))
    with pytest.raises(MatrixCorrupt) as info:
        MatrixIndex(path)
    assert info.value.reason == "bad-dimension"


def test_wrong_record_size_is_rejected(tmp_path: Path) -> None:
    path = build(tmp_path / "reclen.trk")
    raw = bytearray(path.read_bytes())
    raw[11] = 32
    path.write_bytes(bytes(raw))
    with pytest.raises(MatrixCorrupt) as info:
        MatrixIndex(path)
    assert info.value.reason == "bad-record-size"


def test_absurd_count_does_not_allocate(tmp_path: Path) -> None:
    """A header claiming 4 billion records must fail on size, not on malloc."""
    path = build(tmp_path / "count.trk")
    raw = bytearray(path.read_bytes())
    struct.pack_into("<I", raw, 12, 0xFFFFFFFF)
    path.write_bytes(bytes(raw))
    with pytest.raises(MatrixCorrupt) as info:
        MatrixIndex(path)
    assert info.value.reason == "size-mismatch"


def test_zero_cost_when_file_is_removed_after_open(tmp_path: Path) -> None:
    """An already mapped index keeps working when the file is unlinked."""
    path = build(tmp_path / "unlinked.trk")
    with MatrixIndex(path) as index:
        path.unlink()
        assert index.count == 4
        assert len(index.scan([1.0] + [0.0] * 15, k=2)) == 2


def test_readonly_file_is_readable(tmp_path: Path) -> None:
    path = build(tmp_path / "ro.trk")
    path.chmod(0o444)
    try:
        with MatrixIndex(path) as index:
            assert index.count == 4
    finally:
        path.chmod(0o644)


def test_unreadable_path_reports_storage_io(tmp_path: Path) -> None:
    if hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() == 0:
        pytest.skip("running as root bypasses permission checks")
    path = build(tmp_path / "denied.trk")
    path.chmod(0o000)
    try:
        with pytest.raises(StorageIO):
            MatrixIndex(path)
    finally:
        path.chmod(0o644)
