# Installing and running Trick

This is the only installation path. Every artifact the project creates is listed
here with the command that creates it, the command that proves it works, and the
command that removes it. Nothing starts by itself: only steps marked
`[starts a process]` put something on the machine, and none of them is required
for the terminal tool to work.

## What Trick answers

Two questions, one pipeline:

* **Where in this long conversation did we say that?** Send the chat (Open WebUI
  does it automatically; the terminal takes `--transcript FILE`; MCP hosts pass
  `messages`). You get back the matching passages with their turn number, role and
  character offsets.
* **Which package or paper solves this?** You get ranked sources from CRAN,
  Bioconductor, arXiv, OpenAlex and Crossref.

Both are scored the same way: lexical retrieval narrows the candidates, laya's
typed decision scoring breaks the ties, and every row says which of the two
produced its score (`*` = scored by laya, `-` = retrieval only).

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

The package declares no `[project.scripts]` entry point, so the only supported
invocation is `<repo>/.venv/bin/python -m trick <command>`. Writing `trick
<command>` fails with `command not found` on a clean machine; this document never
uses that form, and `scripts/check.sh` fails the build if a document starts using
it again.

## 0. Prerequisites

Required: Python 3.10 or newer, and SQLite with FTS5 (step 2 proves it). Required
for model scoring: a laya endpoint with an API key (step 3). Without one Trick
runs the offline scorer and labels those results synthetic.

Optional: `numpy` for the matrix accelerator (`pip install -e '.[accel]'`).
Without it the pure-Python scan returns the same order, only slower.

Docker is needed only for the Open WebUI step. Nothing else needs a container.

## 1. Create the environment

Not a process; safe to re-run.

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

One JSON object: store path and document count, FTS5 availability, active scorer,
shortlist, blend weights, source quota, matrix state, and where credentials were
found. The key itself is never printed, only `"key_present": true`.

| Field | Meaning | Fix |
| --- | --- | --- |
| `"fts5": false` | this Python's SQLite has no FTS5 | use another interpreter, step 1 |
| `"scorer": "mock"` | no credentials resolved, results are not laya-scored | step 3 |
| `"credentials": {"error": ...}` | no credential file and no environment | step 3 |
| `"matrix": null` | optional score matrix absent | nothing; Trick runs without it |

## 3. Configure the laya endpoint

Credentials are read from the first source that exists:

1. environment: `LAYA_BASE_URL`, `LAYA_API_KEY`, optional `LAYA_TIMEOUT_MS`,
   `LAYA_SCORE_PATH`, `LAYA_CHOICE_PATH`
2. file: `$HOME/.config/hermes-secrets/laya.credentials`, same `KEY=value` lines

The file is preferred on a workstation because it stays out of shell history. It
must be mode `600`: a group-readable key file is refused instead of warned about,
because that file grants spending on a paid endpoint.

```sh
install -d -m 700 "$HOME/.config/hermes-secrets"
install -m 600 /dev/null "$HOME/.config/hermes-secrets/laya.credentials"
```

Write these lines into it, real values, no placeholder left behind:

```
LAYA_BASE_URL=https://your-endpoint
LAYA_API_KEY=your-key
LAYA_TIMEOUT_MS=20000
```

Add `LAYA_SCORE_PATH` and `LAYA_CHOICE_PATH` if your endpoint answers on
non-default paths; both default to `/v1/systemone`.

Verify with one real call (costs one request):

```sh
.venv/bin/python -m trick probe "mixed effects model does not converge"
```

Expected: `probe: ok in ... ms, usage={...}` followed by three candidate lines
with probabilities. `401` means the key was rejected; `S5` means the endpoint was
unreachable.

Remove: delete that file, or `unset LAYA_API_KEY LAYA_BASE_URL`.

## 4. Fill the local catalogue

First run only, or when you want fresher catalogue data. Downloads bulk metadata
(CRAN ~2 MB, Bioconductor ~6 MB) plus the ranking priors. Takes about three
minutes, mostly CRAN download counts fetched 120 package names per request.

```sh
.venv/bin/python -m trick ingest --source cran,bioc
```

Expected last lines: `cran: 25595 documents indexed`, `bioc: 2341 documents
indexed`, `store now holds ... upserted rows`.

Verify:

```sh
.venv/bin/python -m trick doctor
```

Expected: `"docs"` above 25000.

Remove: `rm -f "$HOME/.local/share/trick/trick.db"` (also deletes the FTS index and
the decision cache; re-run this step to rebuild).

## 5. Terminal surface

No process to start, no socket, runs in the foreground.

```sh
.venv/bin/python -m trick tui
```

Type a question, press Enter. `/help` lists the commands, `/quit` exits, Up/Down
walk the history file. It uses the core service when one is running and builds the
engine in-process otherwise, so both paths return the same order.

One shot, scriptable:

```sh
.venv/bin/python -m trick query "single cell normalization" --limit 5 --json
```

Search inside a saved conversation instead of the catalogues:

```sh
.venv/bin/python -m trick tui --transcript "$HOME/chat.json"
.venv/bin/python -m trick query "spatial join" --transcript "$HOME/chat.json"
```

`chat.json` is any JSON array of `{"role": ..., "content": ...}` objects, oldest
first; a `{"messages": [...]}` wrapper is accepted too. The answer is a
`conversation:` block above the source list, one line per passage with its turn
number, and the header states how many turns were searched and how many the window
dropped (default: the last 200).

Remove: `rm -f "$HOME/.local/share/trick/history"` (the history file only; the
binary and the store are untouched).

## 6. MCP surface

`[starts a process]` once per client session; stdio only, no port, no token.

```sh
.venv/bin/python -m trick mcp
```

It speaks JSON-RPC 2.0 on stdout and writes nothing else there; every diagnostic
goes to stderr.

| Tool | Arguments |
| --- | --- |
| `trick_search` | `query` (required), `messages` (the conversation), `scope` (`sources`, `chat`, `both`), `limit` 1-26, `live`, `verbose` |
| `trick_health` | none |

A tool failure is a result (`isError: true` plus the `S<n>` code), never a dead
session.

Probe without a client (two JSON lines back, nothing else):

```sh
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"trick_health","arguments":{}}}' \
  | .venv/bin/python -m trick mcp
```

Client configuration for any MCP host. Absolute paths, because hosts spawn servers
with a sanitised `PATH`, and a relative command fails with `ENOENT`:

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

Remove: delete that block from the host's configuration; nothing was written to
disk.

## 7. HTTP bridge

Needed by Open WebUI, which runs in a container and cannot open a unix socket. The
terminal, one-shot and MCP surfaces do not use it.

`[starts a process]` foreground:

```sh
.venv/bin/python -m trick serve
```

Defaults: unix socket `$HOME/.local/share/trick/run/trick.sock` (0600) plus HTTP on
`127.0.0.1:8765`, bearer token read from
`$HOME/.local/share/trick/bridge.token` (created on first start, 0600, 64 hex
characters).

Bind policy, enforced in code rather than by convention:

* loopback (`127.0.0.1`, `::1`) — allowed, default
* one specific non-loopback local address — only with `--expose-network`
* `0.0.0.0` or `::` — always refused, even with the flag: the service has no
  accounts, so a wildcard bind plus a leaked token lets anyone spend your laya
  credits

Request bodies are capped at 1 MiB and refused before parsing, so one POST cannot
exhaust the host while a window of a long conversation still fits.

The store is shared safely: the bridge, the terminal surface and a one-shot query
can point at the same `trick.db` at the same time. Reading never waits for the
write lock, a writer waits up to 5 s and then reports `S7` instead of failing with
a bare `database is locked`, and a decision-cache write that cannot happen costs
the memo, not the ranking.

Verify (this reads your own token file; nothing secret is printed back):

```sh
TOKEN=$(cat "$HOME/.local/share/trick/bridge.token")
curl -s -X POST http://127.0.0.1:8765/v1/search \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"differential expression","limit":3}'
```

Expected: `{"ok": true, "result": {"query": "differential expression", "rows": [...]}}`.
`401` means the token is wrong; an empty reply means nothing is listening.

Add `"messages": [{"role": "user", "content": "..."}]` and `"scope": "both"` to the
same body to search a conversation through the bridge.

Stop: Ctrl-C, or `pkill -f 'trick serve'`.

### 7b. Run the bridge as a systemd user service (optional)

Use this only if you want the bridge up after login without a terminal window.
Installing the unit starts nothing.

```sh
cd "$HOME/evrim_otonom/trick"
sh deploy/install-service.sh            # writes the unit, reloads systemd, does NOT start
sh deploy/install-service.sh --start    # writes it, enables it, starts it now
```

The script derives `ExecStart` from the repository location, picks the bind address
from `docker0` when Docker is present (loopback otherwise), writes
`$HOME/.config/systemd/user/trick-bridge.service` and runs `systemctl --user
daemon-reload`. It refuses to run without the venv from step 1.

Verify:

```sh
systemctl --user status trick-bridge.service --no-pager
```

Expected: `active (running)`.

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

Trick is an **Action**: a button in the message toolbar. Press it, an input dialog
opens, type what you are looking for, press Enter, the passage appears as a card
under the message. It is not a model, so it never appears in the model selector
and never answers a chat turn.

Three things must be true: the bridge is running and reachable from the container,
you have an **admin** Open WebUI session, and that session has produced an API key.
Installing a function is admin-only; a key from a normal account gets `401` even
when the key is valid. This repository never asks for your Open WebUI password.

The UI listens on `http://localhost:3000`; the container's own port 8080 is not
published on the host, so `http://localhost:8080` fails by design.

### 8.1 Bridge address

A container cannot reach the host's `127.0.0.1`. Bind the docker bridge address
instead — reachable from containers, not from the LAN:

```sh
ip -4 -o addr show docker0 | awk '{print $4}'    # e.g. 172.17.0.1/16
.venv/bin/python -m trick serve --http-host 172.17.0.1 --expose-network --http-port 8765
```

Inside a container that maps `host.docker.internal` to the host, the Action can use
that hostname instead and survive an address change. If Open WebUI runs natively,
skip this and keep the loopback default.

### 8.2 Install the function (canonical route)

1. Open `http://localhost:3000` and sign in as an admin.
2. Avatar, bottom left → `Settings` → `Account` → `API Keys` → `Create`. Copy the
   key; the UI shows it once.
3. Run the installer in a shell **on the host**, not inside the container:

```sh
cd "$HOME/evrim_otonom/trick"
TRICK_OPENWEBUI_API_KEY=paste-the-key-here \
  .venv/bin/python integrations/openwebui/install.py
```

Add `--dry-run` to see what would be written without writing it.

The installer does five things and checks each one: creates or updates the
function, reads it back and compares the stored byte count with the file, asserts
Open WebUI derived `type=action` (a Pipe would show up as a model), switches the
global toggle on so the button appears on every model, and writes the bridge token
into the `TRICK_TOKEN` valve. The token is never printed. Expected last lines:

```
installed and verified: 16384 bytes, type=action, is_global=True, is_active=True
TRICK_TOKEN valve set from $HOME/.local/share/trick/bridge.token (43 characters, not printed)
done: open a chat, press the Trick button under any answer, type what you are looking for
```

`401` → the key is not from an admin account. `stored content differs` → the
instance truncated or rewrote the content; do not use the button until this
matches. `stored type='pipe'` → an old install is still in the database; delete it
under `Admin Settings` → `Functions` and re-run the installer.

### 8.3 Fallback route (only if the installer cannot be used)

`Admin Settings` → `Functions` → `+` → paste the whole content of
`integrations/openwebui/trick_action.py` → `Save`, then enable the global toggle
and paste the token into the `TRICK_TOKEN` valve by hand. This route has no
read-back, so a truncated paste is indistinguishable from a working install until
you press the button; that is why it comes second.

### 8.4 Valves

`Admin Settings` → `Functions` → `Trick` → gear icon:

| Valve | Value | Note |
| --- | --- | --- |
| `TRICK_URL` | `http://host.docker.internal:8765/v1/search` | Docker default; native Open WebUI uses `http://127.0.0.1:8765/v1/search` |
| `TRICK_TOKEN` | contents of `$HOME/.local/share/trick/bridge.token` | written by the installer; no quotes |
| `LIMIT` | `8` | 1-26 |
| `LIVE` | `true` | `false` = local catalogue only |
| `SOURCES` | `never` | `never` (default) = this button finds in the conversation only; `auto` = also list catalogue matches, but only when the conversation had none; `always` = list them beside every hit |
| `TIMEOUT_S` | `25` | raise only on a slow network |
| `SHOW_SCORES` | `true` | blended score per row |
| `SEARCH_CHAT` | `true` | also search inside the conversation |
| `CHAT_TURNS` | `200` | how many recent turns are searched |
| `CHAT_MAX_CHARS` | `150000` | transcript budget sent to the service |
| `CHAT_RESULTS` | `3` | how many passages are shown |
| `priority` | `-5` | button order in the toolbar; lower is further left |

Verify end to end: open a chat that has a few turns, press the Trick button (the
magnifier) under any answer, type `differential expression for RNA-seq counts`,
press Enter. Expected: a card in about 1-2 s with a `Conversation` block (turn
number, role, quoted passage, and `matched: ...` naming the query words that
passage actually contains - a hit that shares only one rare word with the
question says so instead of looking random). Clicking a passage scrolls the chat
to the message it came from and flashes it.

How the jump works, because it decides what you can expect: Open WebUI renders
this card in an iframe whose sandbox is `allow-scripts allow-downloads`. The
sandbox flag `iframeSandboxAllowSameOrigin` defaults to **false** in 0.12.4, so
the card cannot read the chat document - `window.parent.document` throws - and
code written against a shared origin does nothing when a row is clicked. The
card therefore asks the page: it posts `{source:'trick-card', mid, cid}` to its
parent, and a listener that the action puts into the page through
`__event_call__` type `execute` finds `message-<thread>-<id>` (then
`message-<id>`, then any `message-*` id ending with the message id), scrolls,
flashes, and replies. **No user or admin setting has to be changed for this.**
If the message is not rendered at all (a long chat renders only nearby turns) or
nothing answers within about a second, the click opens the full passage inside
the card instead, and the row says which of the two happened - so a click never
does nothing.

That listener is installed once per browser tab and reused by later cards; it
only reacts to messages whose `source` is `trick-card`, and its only effect is a
scroll plus a highlight. If you run a hardened instance that refuses `execute`
calls, the jump degrades to "open the passage in the card", which is the
behaviour of every row by default. Pressing Enter or Space on a focused row does
the same as clicking. With the default `SOURCES=never` the
catalogues stay out of the way and the card only says in one line that `N
catalogue sources matched outside this conversation and are hidden`; set the
valve to `auto` to list them when the chat had no match, or `always` to list them
beside every hit. When the conversation has no match, the card says so. Listed
sources carry `source`/`key`, a blended score, `*` for laya-scored rows and `-`
for retrieval-only rows. The footnote states the blend the score came from (the
weights are shipped with the answer, so the text cannot drift), and that the
number orders this list only - it is not a confidence and not comparable between
queries. The card closes from the `x` in its header or on Escape, and reopens
from the same line - it needs no script to do that. Closing the input dialog
without typing searches nothing.

The catalogue itself is not gone: `python -m trick query` and the MCP
`trick_search` tool still search CRAN, Bioconductor, arXiv and the live sources,
which is where a package lookup belongs.

Failure text is intentional and names the fix:

| You see | Cause | Fix |
| --- | --- | --- |
| `TRICK_TOKEN` in the card | empty or stale valve | re-run the installer |
| `cannot reach ...` / `transport error` | bridge down or wrong URL | start the bridge, check `TRICK_URL` |
| `401` from the bridge | valve token differs from the file | re-run the installer |
| `Nothing matched` with `S4`/`S5` | sources or scorer did not answer | check the network, `LIVE` |
| no button in the toolbar | function not global, or not active | `Admin Settings` → `Functions` → global toggle |

Remove: `Admin Settings` → `Functions` → `Trick` → trash icon. Nothing on the host
changes.

## 9. Everything this project creates

| Artifact | Created by | Removed by |
| --- | --- | --- |
| `<repo>/.venv` | step 1 | `rm -rf .venv` |
| `$HOME/.local/share/trick/trick.db` (+`-wal`, `-shm`) | step 4, usage | `rm -f trick.db*` |
| `$HOME/.local/share/trick/bridge.token` | first `serve` (step 7) | `rm -f bridge.token` |
| `$HOME/.local/share/trick/run/trick.sock` | step 7 | stop the process, `rm -rf run/` |
| `$HOME/.local/share/trick/history` | step 5 | `rm -f history` |
| `$HOME/.local/share/trick/scores.trk` | optional, when a matrix is written | `rm -f scores.trk` |
| `$HOME/.config/systemd/user/trick-bridge.service` | step 7b | step 7b remove block |
| Open WebUI function `trick` | step 8 | step 8 remove |
| laya credentials file | step 3 | `rm -f` that file |

No process survives a reboot except the systemd unit from step 7b, and that one
exists only if you installed it.

## 10. When something is degraded

Trick never hides a failure behind a plausible answer. Every degradation has a
code, and it is the same code on every surface:

| Code | Meaning | What the result looks like |
| --- | --- | --- |
| `S1` | score matrix not built yet | signatures skipped, text ranking only |
| `S2` | score matrix corrupt | emergency catalogue in use, rows flagged synthetic |
| `S3` | not enough memory for the index | text-only mode |
| `S4` | one live source did not answer | remaining sources, noted in the output |
| `S5` | laya did not answer | rows are `-` (retrieval only), never faked |
| `S6` | storage problem | if the store cannot be read the request fails with the path and reason; if only the decision cache cannot be written the ranking is returned and `S6` is listed as degraded |
| `S7` | another writer holds the lock | the write waits up to 5 s, then reports `S7`; reads never wait |
| `S8` | input rejected (bad transcript, oversized body) | the request fails with a reason |
| `S9` | configuration problem | the request fails with the setting to fix |

Run the repository gate before trusting a checkout:

```sh
sh scripts/check.sh
```

Fourteen checks: tests, byte-compile, on-disk layout, ranking constants, no network
in tests, MCP and bridge surfaces, socket and token permissions, English-only
repository, no credentials in tracked files, stdout purity, integration surfaces,
integration secrets, documentation truth (every documented command and path must
exist), and conversation-search quality against planted needles.

The last one is a floor, not a target: it fails the gate if `recall@3` drops below
0.9, `MRR@10` below 0.8 or a decoy becomes the top hit more than 20% of the time,
so a ranking change cannot silently make the tool worse at its primary job.
