"""Quantisation contract: unit vectors in, stable 16 byte signatures out."""

from __future__ import annotations

import math
import os
import subprocess
import sys

import pytest

from trick.errors import ContractViolation
from trick.quant import (
    QSCALE,
    SIG_DIM,
    cosine_from_dot,
    decode_score,
    dot_int8,
    encode_score,
    feature_hash_signature,
    quantize,
)


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def test_signature_is_fixed_size() -> None:
    assert len(quantize([1.0] * SIG_DIM)) == SIG_DIM


def test_zero_vector_maps_to_zero_signature() -> None:
    assert quantize([0.0] * SIG_DIM) == bytes(SIG_DIM)


def test_wrong_dimension_is_rejected() -> None:
    with pytest.raises(ContractViolation):
        quantize([1.0, 2.0])


def test_quantisation_preserves_cosine_within_tolerance(rng) -> None:
    worst = 0.0
    for _ in range(200):
        a = [rng.uniform(-1, 1) for _ in range(SIG_DIM)]
        b = [rng.uniform(-1, 1) for _ in range(SIG_DIM)]
        approx = cosine_from_dot(dot_int8(quantize(a), quantize(b)))
        worst = max(worst, abs(approx - cosine(a, b)))
    assert worst < 0.02, f"worst cosine error {worst:.4f}"


def test_identical_vectors_hit_ceiling() -> None:
    v = [rng_value for rng_value in range(SIG_DIM)]
    sig = quantize([float(x) for x in v])
    assert abs(cosine_from_dot(dot_int8(sig, sig)) - 1.0) < 0.01


def test_opposite_vectors_hit_floor() -> None:
    v = [float(x + 1) for x in range(SIG_DIM)]
    sig = quantize(v)
    neg = quantize([-x for x in v])
    assert cosine_from_dot(dot_int8(sig, neg)) < -0.99


def test_score_codec_roundtrip() -> None:
    for value in (0.0, 0.5, 1.0, 0.1234):
        assert abs(decode_score(encode_score(value)) - value) < 1e-4


@pytest.mark.parametrize("bad", [-3.0, 2.5, float("nan")])
def test_score_codec_clamps(bad) -> None:
    raw = encode_score(bad)
    assert 0 <= raw <= 0xFFFF
    assert 0.0 <= decode_score(raw) <= 1.0


def test_feature_hash_is_stable_across_processes() -> None:
    """A PYTHONHASHSEED change must not alter the emergency signatures."""
    code = (
        "import sys;sys.path.insert(0,sys.argv[2]);"
        "from trick.quant import feature_hash_signature;"
        "print(feature_hash_signature('cran|MASS|linear models').hex())"
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(root, "src")
    outs = set()
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run(
            [sys.executable, "-c", code, "x", src],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout.strip()
        outs.add(out)
    assert len(outs) == 1, outs


def test_scale_constant_matches_storage() -> None:
    assert QSCALE == 127.0
