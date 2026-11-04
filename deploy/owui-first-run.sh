#!/bin/sh
# First run: from a bare checkout to a working Trick button in Open WebUI.
#
# Every step is idempotent and checked. Nothing is started unless you pass
# --start, and the only thing that ever leaves this machine is the laya call
# you configured in step 3 of INSTALL.md.
#
#   sh deploy/owui-first-run.sh                 # prepare, then print what is left
#   sh deploy/owui-first-run.sh --start         # also start the bridge in the background
#   sh deploy/owui-first-run.sh --skip-ingest   # keep the store that already exists
#
# The installer for the Open WebUI function needs an admin API key and cannot be
# automated any further: run it with TRICK_OPENWEBUI_API_KEY set (see 8.3).
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HOME_DIR=${TRICK_HOME:-$HOME/.local/share/trick}
PY=$REPO/.venv/bin/python
START=0
INGEST=1

for arg in "$@"; do
  case $arg in
    --start) START=1 ;;
    --skip-ingest) INGEST=0 ;;
    --help|-h) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'FAILED: %s\n' "$*" >&2; exit 1; }

say "== 1/6 repository =="
[ -f "$REPO/pyproject.toml" ] || die "$REPO does not look like the Trick checkout"
say "   $REPO"

say "== 2/6 python environment =="
if [ ! -x "$PY" ]; then
  base=$(command -v python3 || true)
  [ -n "$base" ] || die "python3 is not installed"
  say "   creating .venv with $base"
  "$base" -m venv "$REPO/.venv"
fi
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "$($PY -V) is too old; Trick needs 3.11+"
"$PY" -m pip install -q --disable-pip-version-check -e "$REPO[dev]" \
  || die "pip install failed"
"$PY" -c 'import trick; print("   trick", trick.__version__)' || die "the package does not import"

say "== 3/6 catalogues =="
if [ "$INGEST" = 1 ]; then
  # A store with fewer than a thousand documents is not a catalogue; ingest is
  # resumable and skips documents it already holds.
  docs=$("$PY" -m trick doctor --json 2>/dev/null | "$PY" -c 'import json,sys
try:
    checks = json.load(sys.stdin)["checks"]
except Exception:
    print(0)
else:
    print(next((int(e.get("docs", 0)) for e in checks if e["name"] == "documents"), 0))' || echo 0)
  if [ "${docs:-0}" -ge 1000 ]; then
    say "   store already holds $docs documents; use --skip-ingest to silence this"
  else
    say "   downloading CRAN and Bioconductor metadata (a few minutes, ~60 MB)"
    "$PY" -m trick ingest --source cran --source bioconductor \
      || die "ingest failed; run the same command alone to see the error"
  fi
else
  say "   skipped by --skip-ingest"
fi

say "== 4/6 score matrix =="
"$PY" -m trick matrix build || die "the score matrix could not be written"
"$PY" -m trick matrix info || true

say "== 5/6 environment =="
"$PY" -m trick doctor || true
[ -f "${LAYA_CREDENTIALS:-$HOME/.config/hermes-secrets/laya.credentials}" ] \
  || say "   note: no laya credentials file; set one up per INSTALL.md step 3"

say "== 6/6 bridge =="
bridge_ip=$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)
port=8765

# One bridge per TRICK_HOME: the unix socket is exclusive, so a second
# `trick serve` exits with S7. Reporting that as "the bridge exited" would send
# the reader hunting a bug that is not there, so an existing listener is
# detected first and left alone.
listener=""
if command -v ss >/dev/null 2>&1; then
  listener=$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print}' | head -1)
fi

if [ -n "$listener" ]; then
  say "   already listening on port $port - left running, nothing to do"
  say "   $listener"
elif [ "$START" = 1 ]; then
  mkdir -p "$HOME_DIR"
  if [ -n "${bridge_ip:-}" ]; then
    nohup "$PY" -m trick serve --http-host "$bridge_ip" --expose-network --http-port "$port" \
      >>"$HOME_DIR/bridge.log" 2>&1 &
  else
    nohup "$PY" -m trick serve --http-port "$port" >>"$HOME_DIR/bridge.log" 2>&1 &
  fi
  echo $! >"$HOME_DIR/bridge.pid"
  sleep 2
  if ! kill -0 "$(cat "$HOME_DIR/bridge.pid")" 2>/dev/null; then
    tail -3 "$HOME_DIR/bridge.log" >&2 || true
    die "the bridge exited; the last lines of $HOME_DIR/bridge.log are above"
  fi
  say "   started (pid $(cat "$HOME_DIR/bridge.pid")), log: $HOME_DIR/bridge.log"
  say "   stop with: kill \$(cat $HOME_DIR/bridge.pid)"
else
  if [ -n "${bridge_ip:-}" ]; then
    say "   docker bridge address: $bridge_ip"
    say "   start with: $PY -m trick serve --http-host $bridge_ip --expose-network --http-port $port"
  else
    say "   no docker0 interface found (native Open WebUI?):"
    say "   start with: $PY -m trick serve --http-port $port"
  fi
  say "   or re-run this script with --start"
fi

say ""
say "Left to do by hand (admin rights, one time):"
say "  1. open http://localhost:3000, sign in as an admin"
say "  2. Settings -> Account -> API Keys -> Create, copy the key"
say "  3. TRICK_OPENWEBUI_API_KEY=paste-the-key $PY integrations/openwebui/install.py"
say "  4. open a chat, press the magnifier under any answer, type what you look for"
say ""
say "The button searches the conversation only. The catalogues (CRAN,"
say "Bioconductor, arXiv, OpenAlex) are reachable from '$PY -m trick query' and"
say "from the MCP server, never from this button."
