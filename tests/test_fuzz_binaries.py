"""Fuzz the two binary formats: a bad file is classified, never guessed.

The matrix and the backup archive are the only two files Trick both writes and
re-reads across restarts, and both live on disks the project does not own. The
contract tested here is deliberately narrow, because it is the one that matters
in production:

* a mutated file either loads and behaves (finite scores, self-consistent
  counts) or raises the one declared error class;
* no ``struct.error``, ``IndexError``, ``AssertionError`` or
  ``cryptography.exceptions.InvalidTag`` ever escapes to the caller.

The sweep is deterministic (fixed seed, fixed case count) so a failure is always
reproducible from the test name alone.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import fuzzlib
from trick.matrix import MatrixIndex
from trick.vault import VaultError, read_archive, seal

CASES = 48


@pytest.fixture(scope="module")
def matrix_base(tmp_path_factory) -> bytes:
    path = fuzzlib.build_matrix(tmp_path_factory.mktemp("fuzz") / "base.trk")
    return path.read_bytes()


def test_matrix_mutations_are_classified(tmp_path: Path, matrix_base: bytes) -> None:
    rng = random.Random(20261104)
    tally = fuzzlib.sweep(
        lambda path, blob: path.write_bytes(blob),
        lambda path: fuzzlib.check_matrix_bytes(path, fuzzlib.quantise_sample()),
        fuzzlib.matrix_cases(matrix_base, rng, CASES),
        tmp_path / "cases",
    )
    assert tally.get("loaded", 0) + tally.get("rejected", 0) == CASES
    # Both outcomes must actually occur, otherwise the sweep is testing nothing:
    # every case rejected would mean the loader refuses valid files, every case
    # loaded would mean the header checks are gone.
    assert tally["rejected"] > 0
    assert tally["loaded"] > 0


def test_matrix_body_bit_flips_are_caught_by_verify(tmp_path: Path, matrix_base: bytes) -> None:
    """A body flip must not be silently trusted: ``verify`` reports it."""
    rng = random.Random(7)
    caught = 0
    for name, blob in fuzzlib.matrix_cases(matrix_base, rng, CASES):
        if not name.startswith("body-bit-"):
            continue
        path = tmp_path / name
        path.write_bytes(blob)
        try:
            index = MatrixIndex(path)
        except fuzzlib.MATRIX_ERRORS:
            continue
        try:
            with pytest.raises(Exception) as info:
                index.verify()
            from trick.errors import MatrixCorrupt

            assert isinstance(info.value, MatrixCorrupt)
            assert info.value.reason == "crc-mismatch"
            caught += 1
        finally:
            index.close()
    assert caught > 0, "no body mutation reached verify()"


def test_non_finite_qscale_is_rejected(tmp_path: Path, matrix_base: bytes) -> None:
    """``inf``/``nan``/absurd scales would load and rank nothing - refuse them."""
    import struct

    from trick.errors import MatrixCorrupt

    from trick.matrix import HEADER_STRUCT

    for value in (float("inf"), float("nan"), 0.0, -1.0, 1e9):
        data = bytearray(matrix_base)
        header = list(HEADER_STRUCT.unpack(bytes(data[: HEADER_STRUCT.size])))
        header[-1] = value
        data[: HEADER_STRUCT.size] = HEADER_STRUCT.pack(*header)
        path = tmp_path / f"qscale-{value!r}.trk".replace("/", "_")
        path.write_bytes(bytes(data))
        with pytest.raises(MatrixCorrupt) as info:
            MatrixIndex(path)
        assert info.value.reason == "bad-qscale"


def test_archive_mutations_are_classified(tmp_path: Path) -> None:
    passphrase = "correct horse battery staple"
    base = fuzzlib.sealed_sample(passphrase)
    rng = random.Random(20261104)
    opened = 0
    rejected = 0
    for name, blob in fuzzlib.archive_cases(base, rng, CASES):
        path = tmp_path / name
        path.write_bytes(blob)
        try:
            read_archive(path, passphrase)
            opened += 1
        except VaultError:
            rejected += 1
    assert rejected == CASES, f"{opened} mutated archives opened; {CASES - rejected} accepted"
    assert opened == 0


def test_archive_from_another_passphrase_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.trk"
    path.write_bytes(seal(b'{"format": "TRICKV1"}', "one"))
    with pytest.raises(VaultError):
        read_archive(path, "two")
