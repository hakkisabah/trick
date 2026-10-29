#!/bin/sh
# Repository control gate. Every failure class this project has hit so far is
# checked here, so a regression fails the gate instead of surfacing later.
#
#   1. tests            unit, corruption, contention, budget, adapters, pipeline
#   2. compile          every module imports and byte-compiles
#   3. layout           on-disk constants are still what the design promises
#   4. ranking          pipeline constants still match the measured defaults
#   5. offline          the test suite never reaches the network
#   6. language         no Turkish-specific characters in tracked files
#   7. secrets          no obvious credentials in tracked files
set -eu

cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

if [ ! -x "$PY" ]; then
  echo "gate: $PY not found; create it with: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 2
fi

echo "== 1/7 tests"
"$PY" -m pytest -q

echo "== 2/7 compile"
"$PY" -m compileall -q src scripts >/dev/null

echo "== 3/7 layout"
"$PY" - <<'PYCODE'
import sys
sys.path.insert(0, "src")
from trick.matrix import HEADER_SIZE, HEADER_STRUCT, RECORD_SIZE, RECORD_STRUCT
assert HEADER_STRUCT.size == 28, HEADER_STRUCT.size
assert RECORD_STRUCT.size == RECORD_SIZE == 24, (RECORD_STRUCT.size, RECORD_SIZE)
assert HEADER_SIZE == 48, HEADER_SIZE
print("layout ok: 48 byte header, 24 byte records")
PYCODE

echo "== 4/7 ranking contracts"
"$PY" - <<'PYCODE'
import sys
sys.path.insert(0, "src")
from trick.laya import DEFAULT_INSTRUCTIONS, MAX_LABELS
from trick.search import DEFAULT_GATE, DEFAULT_PRIOR, DEFAULT_WEIGHTS
assert MAX_LABELS == 26
assert abs(sum(DEFAULT_WEIGHTS) - 1.0) < 1e-9, DEFAULT_WEIGHTS
assert DEFAULT_WEIGHTS[0] < DEFAULT_WEIGHTS[1], "laya must stay the tie-breaker"
assert 0.0 <= DEFAULT_GATE < 1.0
assert 0.0 <= DEFAULT_PRIOR < 1.0
assert len(DEFAULT_INSTRUCTIONS.split()) <= 8, "a long instruction measured worse"
print(f"ranking ok: weights={DEFAULT_WEIGHTS} prior={DEFAULT_PRIOR} gate={DEFAULT_GATE}")
PYCODE

echo "== 5/7 offline tests"
if grep -rnE '(live_adapters\(\)|bulk_adapters\(\)\.fetch|http_get\()' tests/ >/dev/null 2>&1; then
  echo "gate: the test suite must not use a real transport" >&2
  exit 1
fi
echo "offline ok"

echo "== 6/7 language (english only in the repository)"
"$PY" scripts/language_gate.py

echo "== 7/7 secrets"
if git grep -n -I -E '(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|aws_secret_access_key[[:space:]]*=)' -- . >/dev/null 2>&1; then
  echo "gate: credential-shaped string found in tracked files" >&2
  exit 1
fi
if git grep -n -I -E '^LAYA_API_KEY=[A-Za-z0-9_-]{12,}$' -- src tests scripts README.md >/dev/null 2>&1; then
  echo "gate: a real LAYA_API_KEY value is tracked" >&2
  exit 1
fi
echo "secrets ok"

echo "gate: all checks passed"
