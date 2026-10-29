#!/bin/sh
# Repository control gate. Every failure class this project has hit so far is
# checked here, so a regression fails the gate instead of surfacing later.
#
#   1. tests            unit, corruption, contention, budget
#   2. compile          every module imports and byte-compiles
#   3. layout           on-disk constants are still what the design promises
#   4. language         no Turkish-specific characters in tracked files
#   5. secrets          no obvious credentials in tracked files
set -eu

cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

if [ ! -x "$PY" ]; then
  echo "gate: $PY not found; create it with: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 2
fi

echo "== 1/5 tests"
"$PY" -m pytest -q

echo "== 2/5 compile"
"$PY" -m compileall -q src scripts >/dev/null

echo "== 3/5 layout"
"$PY" - <<'PYCODE'
import sys
sys.path.insert(0, "src")
from trick.matrix import HEADER_SIZE, HEADER_STRUCT, RECORD_SIZE, RECORD_STRUCT
assert HEADER_STRUCT.size == 28, HEADER_STRUCT.size
assert RECORD_STRUCT.size == RECORD_SIZE == 24, (RECORD_STRUCT.size, RECORD_SIZE)
assert HEADER_SIZE == 48, HEADER_SIZE
print("layout ok: 48 byte header, 24 byte records")
PYCODE

echo "== 4/5 language (english only in the repository)"
"$PY" scripts/language_gate.py

echo "== 5/5 secrets"
if git grep -n -I -E '(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|aws_secret_access_key[[:space:]]*=)' -- . >/dev/null 2>&1; then
  echo "gate: credential-shaped string found in tracked files" >&2
  exit 1
fi
echo "secrets ok"

echo "gate: all checks passed"
