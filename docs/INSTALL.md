# Installing and running Trick

This is the only installation path. Every runtime artifact the project creates is
listed here together with the command that creates it, the command that proves it
worked, and the command that removes it. Nothing starts on its own: only the
steps marked `[starts a service]` put a process on the machine.

## Paths

| Thing | Default path |
| --- | --- |
| repository | `$HOME/evrim_otonom/trick` (substitute your own) |
| interpreter | `<repo>/.venv/bin/python` |
| state directory | `$HOME/.local/share/trick` (override with `TRICK_HOME`) |
| document store | `$HOME/.local/share/trick/trick.db` |
| bridge token | `$HOME/.local/share/trick/bridge.token` (mode `0600`) |
| core socket | `$HOME/.local/share/trick/run/trick.sock` (mode `0600`) |
| terminal history | `$HOME/.local/share/trick/history` |
| score matrix (optional) | `$HOME/.local/share/trick/scores.trk` |
| laya credentials | `$HOME/.config/hermes-secrets/laya.credentials` (mode `0600`) |
| systemd user unit | `$HOME/.config/systemd/user/trick-bridge.service` |

The package declares no `[project.scripts]` entry point. The only supported
invocation is `<repo>/.venv/bin/python -m trick <command>`. Invoking the package
name as a program (`trick` with a subcommand, no `-m`) fails with
`command not found` on a clean machine; this document never uses that form, and
`scripts/check.sh` fails the build if a document starts using it again.

## 0. Prerequisites

Required: Python 3.10 or newer, SQLite with FTS5 (checked by step 2), and a laya
endpoint with an API key. Optional: `numpy` for the accelerator (`pip install -e
'.[accel]'`); without it the pure-Python scan gives the same ordering, only
slower.

Docker is needed only for the Open WebUI step, and Open WebUI is needed only if
you want that surface.

## 1. Create the environment

Not a service; safe to re-run.

```sh
cd "$HOME/evrim_otonom/trick"
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Verify:

```sh
.venv/bin/python -c "import trick, sqlite3; c=sqlite3.connect(':memory:'); c.execute('create virtual table t using fts5(x)'); print('trick ok, fts5 ok')"
```

Expected: `trick ok, fts5 ok`.

Remove: `rm -rf .venv`.

## 2. Check the environment

```sh
.venv/bin/python -m trick doctor
```

Prints one JSON object: store path and document count, FTS5 availability, active
scorer, shortlist size, blend weights, source quota, matrix state, and credential
origin. `"key_present": true` means a key was found; the key itself is never
printed.

Red flags in that output:

| Field | Meaning | Fix |
| --- | --- | --- |
| `"fts5": false` | this Python's SQLite has no FTS5 | use another interpreter, step 1 |
| `"scorer": "mock"` | no credentials resolved, results are not laya-scored | step 3 |
| `"credentials": null` | no credential file and no environment | step 3 |
| `"matrix": null` | optional score matrix absent | nothing; Trick runs without it |

## 3. Configure the laya endpoint

The client reads credentials from the first source that exists:

1. environment: `LAYA_BASE_URL`, `LAYA_API_KEY`, optional `LAYA_TIMEOUT_MS`,
   `LAYA_SCORE_PATH`, `LAYA_CHOICE_PATH`
2. file: `$HOME/.config/hermes-secrets/laya.credentials`, same `KEY=value` lines

The file is preferred on a workstation because it is not in shell history.

```sh
install -d -m 700 "$HOME/.config/hermes-secrets"
install -m 600 /dev/null "$HOME/.config/hermes-secrets/laya.credentials"
```

Then write these lines into it, real values, no placeholder left behind:

```
LAYA_BASE_URL=https://your-endpoint
LAYA_API_KEY=your-key
LAYA_TIMEOUT_MS=20000
```

Verify with one real call (costs one request, prints the score):

```sh
.venv/bin/python -m trick probe "mixed effects model does not converge"
```

Expected: a JSON object with `"ok": true` and a numeric score. `401` means the
key was rejected; `S1` in the output means the transport failed.

Remove: delete the file, or `unset LAYA_API_KEY LAYA_BASE_URL`.

## 4. Fill the local catalogue

First run only, or when you want fresher catalogue data. Downloads bulk metadata
(CRAN ~2 MB, Bioconductor ~6 MB) into the store. Takes about a minute.

```sh
.venv/bin/python -m trick ingest --source cran,bioc
```

Verify:

```sh
.venv/bin/python -m trick doctor
```

Expected: `"docs"` is above 25000.

Remove: `rm -f "$HOME/.local/share/trick/trick.db"` (also deletes the FTS index;
re-run this step to rebuild).

## 5. Terminal surface

No service, no socket, runs in the foreground:

```sh
.venv/bin/python -m trick tui
```

Type a question, press Enter. `/help` lists commands, `/quit` exits, Up/Down walk
the history file. It uses the core service when one is running and builds the
engine in-process otherwise, so both paths return the same order.

One-shot, scriptable:

```sh
.venv/bin/python -m trick query "single cell normalization" --limit 5 --json
```

## 6. MCP surface

`[starts a service]` once per client session; stdio only, no port, no token.

```sh
.venv/bin/python -m trick mcp
```

It speaks JSON-RPC 2.0 on stdout and writes nothing else there. Tools:
`trick_search` (required `query`; optional `limit` 1-26, `live` boolean) and
`trick_health`.

JSON-RPC probe without a client:

```sh
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"trick_health","arguments":{}}}' \
  | .venv/bin/python -m trick mcp
```

Expected: exactly two JSON lines, the second with a `result` object.

Client configuration (Claude Desktop, Codex, any MCP host) — absolute paths,
because hosts spawn servers with a sanitised `PATH`:

```json
{
  "mcpServers": {
    "trick": {
      "command": "/home/YOU/evrim_otonom/trick/.venv/bin/python",
      "args": ["-m", "trick", "mcp"],
      "env": { "PYTHONPATH": "/home/YOU/evrim_otonom/trick/src" }
    }
  }
}
```

## 7. HTTP bridge

Needed by Open WebUI only. The terminal, MCP and one-shot surfaces do not use it.

`[starts a service]` foreground:

```sh
.venv/bin/python -m trick serve
```

Defaults: unix socket `~/.local/share/trick/run/trick.sock` (0600) plus an HTTP
listener on `127.0.0.1:8765`, bearer token read from
`~/.local/share/trick/bridge.token` (created on first start, 0600, 32+ hex).

The bind policy is enforced in code, not by convention:

* loopback (`127.0.0.1`, `::1`) — allowed, default
* a single non-loopback address — only with `--expose-network`
* `0.0.0.0` or `::` — always refused, even with the flag, because the service has
  no accounts and a wildcard bind plus a leaked token lets anyone spend your laya
  credits

Verify:

```sh
TOKEN=$(cat "$HOME/.local/share/trick/bridge.token")
curl -s -X POST http://127.0.0.1:8765/v1/search \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"differential expression","limit":3}' | head -c 400
```

Expected: JSON with `"ok": true` and a `results` array. `401` means the token is
wrong; `000` means nothing is listening.

Stop: Ctrl-C, or `pkill -f 'trick serve'`.

### 7b. Run the bridge as a systemd user service (optional)

Use this when you want the bridge up after login without a terminal window. It is
an explicit opt-in; installing the unit does not start anything.

```sh
cd "$HOME/evrim_otonom/trick"
sh deploy/install-service.sh            # writes the unit, reloads systemd, does NOT start
sh deploy/install-service.sh --start    # writes it, enables it, starts it now
```

The script derives `ExecStart` from the repository location, picks the HTTP bind
address from `docker0` (falls back to loopback when Docker is absent), writes
`~/.config/systemd/user/trick-bridge.service`, and runs `systemctl --user
daemon-reload`.

Verify:

```sh
systemctl --user status trick-bridge.service --no-pager
TOKEN=$(cat "$HOME/.local/share/trick/bridge.token")
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/v1/search \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"x","limit":1}'
```

Expected: `active (running)` and `200`.

Logs: `journalctl --user -u trick-bridge.service -n 50 --no-pager`.

Boot without an interactive login (only if you want that):

```sh
loginctl enable-linger "$USER"
```

Remove:

```sh
systemctl --user disable --now trick-bridge.service
rm -f "$HOME/.config/systemd/user/trick-bridge.service"
systemctl --user daemon-reload
```

## 8. Open WebUI

Two things must be true first: the bridge is running and reachable from the
container, and you have an admin session in the Open WebUI web UI. Both routes
below require that session; there is no way to install a function anonymously,
and this repository never asks for your Open WebUI password.

The UI is at `http://localhost:3000` (the container listens on 8080 internally;
8080 is not published on the host, so `http://localhost:8080` fails by design).

### 8.1 Bridge address

A container cannot reach the host's `127.0.0.1`. Bind the bridge to the docker
bridge address instead — reachable from containers, not from the LAN:

```sh
ip -4 -o addr show docker0 | awk '{print $4}'   # e.g. 172.17.0.1/16
.venv/bin/python -m trick serve --http-host 172.17.0.1 --expose-network --http-port 8765
```

Inside a container started by the shipped compose file, `host.docker.internal`
resolves to that same address (`172.17.0.1 host.docker.internal` in `/etc/hosts`),
so the Pipe can use the hostname and survive an address change.

If Open WebUI runs natively, skip this step and keep the default loopback bind.

### 8.2 Install the function (canonical route)

1. Open `http://localhost:3000` and sign in **as an admin**. The functions API is
   admin-only; a key from a normal account gets `401` even when the key is valid.
2. Avatar, bottom left → `Settings` → `Account` → `API Keys` → `Create`.
   Copy the key once; the UI will not show it again.
3. Run the installer in a shell **on the host**, not inside the container:

```sh
cd "$HOME/evrim_otonom/trick"
TRICK_OPENWEBUI_API_KEY=paste-the-key-here \
  .venv/bin/python integrations/openwebui/install.py
```

Dry run first if you want to see the target without changing anything:

```sh
TRICK_OPENWEBUI_API_KEY=paste-the-key-here \
  .venv/bin/python integrations/openwebui/install.py --dry-run
```

The installer creates or updates the function, reads it back, and compares the
stored byte count with the file, so "it printed OK" is not the only evidence.
Expected last lines:

```
installed and verified: 12345 bytes, enabled=True
next: paste the contents of /home/YOU/.local/share/trick/bridge.token into the Pipe's TRICK_TOKEN valve
```

`401` → the key is not from an admin account, repeat step 2 signed in as admin.
`the stored content differs` → the Open WebUI instance truncated or rewrote the
content; do not use the Pipe until this matches, reinstall and re-check.

### 8.3 Fallback route (only if the installer cannot be used)

Admin panel → `Admin Settings` → `Functions` → `+` → paste the whole content of
`integrations/openwebui/trick_pipe.py` → `Save`. This route has no read-back, so
a truncated paste is indistinguishable from a working install until you chat with
it; that is why it is second choice.

### 8.4 Valves

`Admin Settings` → `Functions` → `Trick` → gear icon:

| Valve | Value | Note |
| --- | --- | --- |
| `TRICK_URL` | `http://host.docker.internal:8765/v1/search` | Docker default; native Open WebUI uses `http://127.0.0.1:8765/v1/search` |
| `TRICK_TOKEN` | contents of `~/.local/share/trick/bridge.token` | 32+ hex chars, no quotes |
| `LIMIT` | `8` | 1-26 |
| `LIVE` | `true` | `false` = local catalogue only |
| `TIMEOUT_S` | `25` | raise only for slow networks |
| `SHOW_SCORES` | `true` | per-row blended score |

Verify end to end: new chat → model `Trick - scored scientific sources` → ask
`differential expression for RNA-seq counts` → Enter. Expected: a numbered list
of R/Bioconductor packages, each row `source`/`key`, a blended score, and `*` for
laya-scored rows, `-` for retrieval-only rows. Answer arrives in about 1-2 s with
live sources enabled.

Failure text is intentional and names the fix:

| You see | Cause | Fix |
| --- | --- | --- |
| `set TRICK_TOKEN` | empty valve | paste the token |
| `cannot reach ...` / `transport error` | bridge down or wrong URL | start the bridge, check `TRICK_URL` |
| `401` from the bridge | valve token differs from the file | re-copy the file contents |
| empty list with `S4`/`S5` | sources answered nothing | check network, `LIVE` |

Remove: `Admin Settings` → `Functions` → `Trick` → trash icon. Nothing else on
the host changes.

## 9. Gemini CLI extension

Requires Gemini CLI 0.66 or newer. The extension registers the same MCP server as
`step 6` and adds `/trick:search` and `/trick:health`.

```sh
cd "$HOME/evrim_otonom/trick/integrations/gemini-extension"
gemini extensions link . --consent
```

Restart Gemini CLI, then:

```
/trick:search negative binomial differential expression
```

The manifest deliberately does not use `"command": "trick"`: Gemini CLI spawns MCP
servers with a sanitised environment, so a `PATH`-dependent command fails with
`spawn trick ENOENT` (measured on 0.66.0). `trick-mcp.sh` resolves the interpreter
itself in this order: `$TRICK_PYTHON`, `<repo>/.venv/bin/python`, `python3`,
`/usr/bin/python3`.

Verify:

```sh
gemini extensions list
```

Expected: `trick` listed with two MCP tools. If the server does not start, set the
extension setting `Trick Python` to an absolute interpreter path.

Remove: `gemini extensions uninstall trick`.

## 10. Everything this project creates, in one table

| Artifact | Created by | Removed by |
| --- | --- | --- |
| `<repo>/.venv` | step 1 | `rm -rf .venv` |
| `~/.local/share/trick/trick.db` (+`-wal`, `-shm`) | steps 4, usage | `rm -f trick.db*` |
| `~/.local/share/trick/bridge.token` | step 7 first start | `rm -f bridge.token` |
| `~/.local/share/trick/run/` socket | step 7 | stop the service, `rm -rf run/` |
| `~/.local/share/trick/history` | step 5 | `rm -f history` |
| `~/.local/share/trick/scores.trk` | optional, when a matrix is written | `rm -f scores.trk` |
| `~/.config/systemd/user/trick-bridge.service` | step 7b | step 7b remove block |
| Open WebUI function `trick` | step 8 | step 8 remove |
| Gemini extension `trick` | step 9 | step 9 remove |
| laya credentials file | step 3 | `rm -f` that file |

No process survives a reboot except the systemd unit from step 7b, and that one
only exists if you ran it.
