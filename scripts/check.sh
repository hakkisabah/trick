#!/bin/sh
# Repository control gate. Every failure class this project has hit so far is
# checked here, so a regression fails the gate instead of surfacing later.
#
#      1. tests         unit, corruption, contention, budget, adapters, pipeline
#      2. compile       every module imports and byte-compiles
#      3. layout        on-disk constants are still what the design promises
#      4. ranking       pipeline constants still match the measured defaults
#      5. offline       the test suite cannot reach the network
#      6. surfaces      the CLI, the MCP tool schema and the bridge are intact
#      7. transport     socket and token permissions are the secure ones
#      8. language      no Turkish-specific characters in tracked files
#      9. secrets       no obvious credentials in tracked files
#     10. stdio         the MCP server writes nothing but JSON-RPC to stdout
#     11. integrations  pipe metadata, MCP manifest, wrapper, commands, installer
#     12. integ. secrets no literal credentials under integrations/
set -eu

cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

if [ ! -x "$PY" ]; then
  echo "gate: $PY not found; create it with: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 2
fi

echo "== 1/12 tests"
"$PY" -m pytest -q

echo "== 2/12 compile"
"$PY" -m compileall -q src scripts >/dev/null

echo "== 3/12 layout"
"$PY" - <<'PYCODE'
import sys
sys.path.insert(0, "src")
from trick.matrix import HEADER_SIZE, HEADER_STRUCT, RECORD_SIZE, RECORD_STRUCT
assert HEADER_STRUCT.size == 28, HEADER_STRUCT.size
assert RECORD_STRUCT.size == RECORD_SIZE == 24, (RECORD_STRUCT.size, RECORD_SIZE)
assert HEADER_SIZE == 48, HEADER_SIZE
print("layout ok: 48 byte header, 24 byte records")
PYCODE

echo "== 4/12 ranking contracts"
"$PY" - <<'PYCODE'
import sys
sys.path.insert(0, "src")
from trick.laya import DEFAULT_INSTRUCTIONS, MAX_LABELS
from trick.search import (
    DEFAULT_BLEND_MODE,
    DEFAULT_FUSION,
    DEFAULT_GATE,
    DEFAULT_PRIOR,
    DEFAULT_WEIGHTS,
)
assert MAX_LABELS == 26
assert abs(sum(DEFAULT_WEIGHTS) - 1.0) < 1e-9, DEFAULT_WEIGHTS
assert DEFAULT_WEIGHTS[0] < DEFAULT_WEIGHTS[1], "laya must stay the smaller share"
assert 0.1 <= DEFAULT_WEIGHTS[0] <= 0.3, "the model must neither vanish nor dominate"
# 0.6 is the measured cliff: niche recall@12 falls 1.0 -> 0.6 there.
assert 0.3 <= DEFAULT_PRIOR < 0.6, DEFAULT_PRIOR
assert DEFAULT_FUSION == "linear", DEFAULT_FUSION
assert DEFAULT_BLEND_MODE == "fused", DEFAULT_BLEND_MODE
assert 0.0 <= DEFAULT_GATE < 1.0
assert len(DEFAULT_INSTRUCTIONS.split()) <= 8, "a long instruction measured worse"
print(f"ranking ok: weights={DEFAULT_WEIGHTS} prior={DEFAULT_PRIOR} "
      f"fusion={DEFAULT_FUSION} blend={DEFAULT_BLEND_MODE} gate={DEFAULT_GATE}")
PYCODE

echo "== 5/12 offline tests"
if grep -rnE '(live_adapters\(\)|bulk_adapters\(\)\.fetch|http_get\()' tests/ >/dev/null 2>&1; then
  echo "gate: the test suite must not use a real transport" >&2
  exit 1
fi
if ! grep -q 'no_live_sources' tests/conftest.py; then
  echo "gate: tests/conftest.py must patch live_adapters so no test reaches the network" >&2
  exit 1
fi
echo "offline ok"

echo "== 6/12 surfaces"
"$PY" - <<'PYCODE'
import sys
sys.path.insert(0, "src")
from trick.mcp import TOOLS
from trick.bridge import BridgeServer
from trick.errors import ConfigError

names = [tool["name"] for tool in TOOLS]
assert names == ["trick_search", "trick_health"], names
search = TOOLS[0]
assert search["inputSchema"]["required"] == ["query"]
assert search["inputSchema"]["additionalProperties"] is False
assert search["inputSchema"]["properties"]["limit"]["maximum"] == 26
assert search["inputSchema"]["properties"]["live"]["type"] == "boolean"

# A bridge bound to anything but loopback would expose the service to the LAN.
try:
    BridgeServer(object(), "x" * 40, host="0.0.0.0")
except ConfigError as exc:
    assert "loopback" in str(exc)
else:
    raise AssertionError("BridgeServer accepted a non-loopback host")
print("surfaces ok: mcp tools + loopback-only bridge")
PYCODE

echo "== 7/12 transport permissions"
"$PY" - <<'PYCODE'
import inspect
import sys
sys.path.insert(0, "src")
from trick import service

source = inspect.getsource(service.UnixServiceServer)
assert "0o600" in source, "the socket file must be owner-only"
assert "0o700" in source, "the socket directory must be owner-only"
token_source = inspect.getsource(service.load_or_create_token)
assert "0o600" in token_source, "the bridge token file must be owner-only"
assert "O_EXCL" in token_source, "token creation must not race"
print("transport ok: 0600 socket and token, 0700 directory")
PYCODE

echo "== 8/12 language (english only in the repository)"
"$PY" scripts/language_gate.py

echo "== 9/12 secrets"
if git grep --untracked -n -I -E '(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|aws_secret_access_key[[:space:]]*=)' -- . >/dev/null 2>&1; then
  echo "gate: credential-shaped string found in tracked files" >&2
  exit 1
fi
if git grep --untracked -n -I -E '^LAYA_API_KEY=[A-Za-z0-9_-]{12,}$' -- src tests scripts README.md integrations >/dev/null 2>&1; then
  echo "gate: a real LAYA_API_KEY value is tracked" >&2
  exit 1
fi
echo "secrets ok"

echo "== 10/12 stdio purity"
"$PY" - <<'PYCODE'
import io
import json
import sys
sys.path.insert(0, "src")
from trick.mcp import McpServer


class Backend:
    def search(self, query, *, limit=8, live=None):
        raise RuntimeError("never called")

    def health(self):
        return {"store": {"docs": 0}}


messages = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "trick_health", "arguments": {}}},
    "{broken",
]
stdin_text = "\n".join(json.dumps(m) if isinstance(m, dict) else m for m in messages) + "\n"
stdout = io.StringIO()
McpServer(Backend(), stdin=io.StringIO(stdin_text), stdout=stdout, stderr=io.StringIO()).serve()
lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
assert lines, "the server produced no output"
for line in lines:
    message = json.loads(line)
    assert message["jsonrpc"] == "2.0", message
    assert "id" in message, message
print(f"stdio ok: {len(lines)} well-formed responses, nothing else")
PYCODE

echo "== 11/12 integration surfaces"
"$PY" - <<'PYCODE'
import json
from pathlib import Path

root = Path(".")
pipe = (root / "integrations/openwebui/trick_pipe.py").read_text(encoding="utf-8")
# Open WebUI reads these from the docstring block; a missing one is a silent
# install failure (the function loads with no title and cannot be found).
for field in ("title: Trick", "version:", "required_open_webui_version:"):
    assert field in pipe, f"pipe metadata missing {field!r}"
assert "class Pipe" in pipe and "def pipes(" in pipe and "def pipe(" in pipe
assert "0.0.0.0" not in pipe, "the pipe must not point at a wildcard address"

manifest = json.loads((root / "integrations/gemini-extension/gemini-extension.json").read_text(encoding="utf-8"))
assert manifest["name"] == "trick"
server = manifest["mcpServers"]["trick"]
# Measured on Gemini CLI 0.66.0: it spawns MCP servers with a sanitised PATH, so
# "command": "trick" fails with ENOENT. The wrapper is the fix; keep it.
assert server["command"] != "trick", "MCP command must not depend on PATH"
assert server["command"].startswith("/"), server["command"]
assert any("trick-mcp.sh" in arg for arg in server["args"]), server["args"]
wrapper = root / "integrations/gemini-extension/trick-mcp.sh"
assert wrapper.exists(), "wrapper script missing"
assert wrapper.stat().st_mode & 0o111, "wrapper script is not executable"

for command in ("search", "health"):
    path = root / "integrations/gemini-extension/commands/trick" / f"{command}.toml"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("description ="), f"{path} needs a description"
    assert "prompt = " in text, f"{path} needs a prompt"

# The installer must never accept the key as an argument (shell history, ps).
installer = (root / "integrations/openwebui/install.py").read_text(encoding="utf-8")
assert "TRICK_OPENWEBUI_API_KEY" in installer
assert "--api-key" not in installer, "the API key belongs in the environment, not in argv"
print("integrations ok: pipe metadata, MCP manifest, wrapper, commands, installer")
PYCODE

echo "== 12/12 no secrets in the integration files"
if git grep --untracked -n -I -E '(api[_-]?key|token)[^=]*[:=][[:space:]]*["'"'"'][A-Za-z0-9_-]{24,}["'"'"']' -- integrations >/dev/null 2>&1; then
  echo "gate: a literal credential is tracked under integrations/" >&2
  exit 1
fi
echo "integration secrets ok"

echo "gate: all checks passed"
