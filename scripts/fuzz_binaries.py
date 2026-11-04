"""Gate step: a large deterministic fuzz sweep of the matrix and the archive.

The pytest suite runs a small sweep on every commit; this runs a much larger one
as part of the repository gate, because the interesting failures here are rare by
construction (one bit in one field). Two things are asserted for every case:

* the reader either returns a usable value or raises its declared error class;
* an accepted matrix is self-consistent: declared count == bytes on disk, at most
  ``k`` rows per scan, every score finite.

Usage::

    python scripts/fuzz_binaries.py [--matrix-cases 600] [--archive-cases 300]
"""

from __future__ import annotations

import argparse
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import fuzzlib  # noqa: E402

SEED = 20261104


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-cases", type=int, default=600)
    parser.add_argument("--archive-cases", type=int, default=300)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        base_path = fuzzlib.build_matrix(root / "base.trk")
        base = base_path.read_bytes()
        signature = fuzzlib.quantise_sample()

        rng = random.Random(SEED)
        tally = fuzzlib.sweep(
            lambda path, blob: path.write_bytes(blob),
            lambda path: fuzzlib.check_matrix_bytes(path, signature),
            fuzzlib.matrix_cases(base, rng, args.matrix_cases),
            root / "matrix",
        )
        if tally.get("loaded", 0) + tally.get("rejected", 0) != args.matrix_cases:
            print(f"fuzz: matrix tally is incomplete: {tally}", file=sys.stderr)
            return 1
        if tally["rejected"] == 0 or tally["loaded"] == 0:
            print(f"fuzz: matrix sweep proves nothing: {tally}", file=sys.stderr)
            return 1

        passphrase = "correct horse battery staple"
        archive = fuzzlib.sealed_sample(passphrase)
        rng = random.Random(SEED)
        opened = 0
        rejected = 0
        for name, blob in fuzzlib.archive_cases(archive, rng, args.archive_cases):
            path = root / "archive" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            outcome = fuzzlib.check_archive_bytes(path.read_bytes(), passphrase)
            if outcome == "opened":
                opened += 1
            else:
                rejected += 1
        if opened:
            print(f"fuzz: {opened} mutated archives opened", file=sys.stderr)
            return 1

        print(
            "fuzz ok: matrix %d cases (%d loaded, %d rejected), archive %d cases (0 opened)"
            % (
                args.matrix_cases,
                tally["loaded"],
                tally["rejected"],
                args.archive_cases,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
