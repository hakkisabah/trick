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

It is a find, not an answer: it matches the words you type and does not infer the
ones you did not. `docs/USAGE.md` shows the query shapes that work and what each
label on a row means.

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
(CRAN descriptions ~45 MB, Bioconductor ~6 MB) plus the ranking priors, then
writes the score matrix. Takes one to three minutes, mostly CRAN download counts
fetched 120 package names per request.

```sh
.venv/bin/python -m trick ingest --source cran,bioc
```

Expected last lines: `cran: 25611 documents indexed`, `bioconductor: 2341
documents indexed`, `store now holds ... upserted rows`, then `matrix: 28495
records, 683928 bytes, ... at .../scores.trk`.

CRAN comes from `crandb` `/-/latest`, not from the HTML index: 25,595 documents
with titles and no descriptions could not answer
`fast aggregation and joins for large tables`, because `data.table` is titled
"Extension of data.frame" and only its description contains those words. The 45 MB
payload is parsed entry by entry (73 MB peak RSS), not with `json.loads`.

Verify:

```sh
.venv/bin/python -m trick doctor
```

Expected: `"docs"` above 25000 and `"matrix": {"count": ...}` not null.

Rebuild just the matrix (safe at any time; it is written to a temporary file and
published with one rename):

```sh
.venv/bin/python -m trick matrix build
.venv/bin/python -m trick matrix info
```

Remove: `rm -f "$HOME/.local/share/trick/trick.db" "$HOME/.local/share/trick/scores.trk"`
(also deletes the FTS index, the decision cache and the matrix; re-run this step to
rebuild both).

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
opens, type what you are looking for, press Enter, and the results block is
appended to the **answer** in that chat - every hit is a link to the turn it came
from. It is not a model, so it never appears in the model selector and never
answers a chat turn.

**What it searches: this conversation, and nothing else.** The catalogues (CRAN,
Bioconductor, arXiv, OpenAlex) are a different corpus answering a different
question - "which package solves this" - and they are reachable from
`python -m trick query` and from the MCP server. There is no valve that adds them
to this button: an R package listed under a chat question is the noise this
surface is meant to avoid.

### 8.0 The whole path, in order

For a machine that has never run Trick. Each command is copy-paste; each step
prints something you can check before moving on.

```sh
cd "$HOME/evrim_otonom/trick"

# 1. environment, catalogues, score matrix, doctor - all in one idempotent script
sh deploy/owui-first-run.sh

# 2. bridge: reachable from the Open WebUI container, not from the LAN
sh deploy/owui-first-run.sh --start          # or run the command step 6 printed

# 3. the function itself (needs an admin key, see 8.2)
TRICK_OPENWEBUI_API_KEY=paste-the-key-here \
  .venv/bin/python integrations/openwebui/install.py

# 4. and confirm the installed copy is the one in this checkout
TRICK_OPENWEBUI_API_KEY=paste-the-key-here \
  .venv/bin/python integrations/openwebui/install.py --check
```

If you do not have an admin key to hand and Open WebUI runs in Docker on this
host, the same comparison works against the database, read-only, and changes
nothing:

```sh
docker cp open-webui:/app/backend/data/webui.db /tmp/webui-copy.db
.venv/bin/python integrations/openwebui/install.py --check --db /tmp/webui-copy.db
```

`--check --db` prints `up to date: N bytes, type=action, global, active (read
from ..., read-only)` or names exactly what is stale. It never installs: writing
needs the admin key, and that asymmetry is deliberate.

Step 1 creates `.venv`, installs the package, downloads the catalogues if the
store is empty, builds the score matrix and prints `python -m trick doctor`. It
starts nothing unless you pass `--start`, and it skips work that is already done,
so running it twice is safe.

Then open a chat, press the magnifier under any answer, type
`differential expression`, press Enter.

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

Only one bridge can run per `TRICK_HOME`: the unix socket is exclusive, so a
second `python -m trick serve` exits with `S7 another trick service is listening`.
That is not a defect - it means the bridge you wanted is already up. `--start`
detects a listener on the port and leaves it alone.

### 8.2 Get an admin API key

Installing a function is admin-only; a key from a normal account gets `401` even
when the key is valid. This repository never asks for your Open WebUI password.

1. Open `http://localhost:3000` and sign in as an admin. The UI listens on 3000;
   the container's own port 8080 is not published, so `http://localhost:8080`
   fails by design.
2. Avatar, bottom left → `Settings` → `Account` → `API Keys` → `Create`. Copy the
   key; the UI shows it once.

### 8.3 Install the function (canonical route)

Run the installer in a shell **on the host**, not inside the container:

```sh
cd "$HOME/evrim_otonom/trick"
TRICK_OPENWEBUI_API_KEY=paste-the-key-here \
  .venv/bin/python integrations/openwebui/install.py
```

Add `--dry-run` to see what would be written without writing it.

The installer does six things and checks each one: creates or updates the
function, reads it back and compares the stored byte count with the file, asserts
Open WebUI derived `type=action` (a Pipe would show up as a model), switches the
global toggle on so the button appears on every model, writes the bridge token
into the `TRICK_TOKEN` valve, and refuses to finish if the installed file still
declares a catalogue valve (`SOURCES`, `SEARCH_CHAT`) - that release answered chat
questions with R packages. The token is never printed. Expected last lines:

```
installed and verified: 16384 bytes, type=action, is_global=True, is_active=True
TRICK_TOKEN valve set from $HOME/.local/share/trick/bridge.token (43 characters, not printed)
done: open a chat, press the Trick button under any answer, type what you are looking for
```

`401` → the key is not from an admin account. `stored content differs` → the
instance truncated or rewrote the content; do not use the button until this
matches. `stored type='pipe'` → an old install is still in the database; delete it
under `Admin Settings` → `Functions` and re-run the installer.

After pulling a new version of this repository, ask whether the installed copy is
still the one you built:

```sh
TRICK_OPENWEBUI_API_KEY=paste-the-key-here \
  .venv/bin/python integrations/openwebui/install.py --check
```

`up to date: <n> bytes, type=action, global, active` means the button runs the
code in this checkout. `stale: content differs (...)` means it does not - reinstall.
This check exists because a stale install is indistinguishable from a working one
from the outside: it looks right and behaves like an older release.

### 8.4 Fallback route (only if the installer cannot be used)

`Admin Settings` → `Functions` → `+` → paste the whole content of
`integrations/openwebui/trick_action.py` → `Save`, then enable the global toggle
and paste the token into the `TRICK_TOKEN` valve by hand. This route has no
read-back, so a truncated paste is indistinguishable from a working install until
you press the button; that is why it comes second.

### 8.5 Valves

`Admin Settings` → `Functions` → `Trick` → gear icon:

| Valve | Value | Note |
| --- | --- | --- |
| `TRICK_URL` | `http://host.docker.internal:8765/v1/search` | Docker default; native Open WebUI uses `http://127.0.0.1:8765/v1/search` |
| `TRICK_TOKEN` | contents of `$HOME/.local/share/trick/bridge.token` | written by the installer; no quotes |
| `TIMEOUT_S` | `25` | raise only on a slow network |
| `SHOW_SCORES` | `true` | blended score per row |
| `CHAT_TURNS` | `200` | how many recent turns are searched |
| `CHAT_MAX_CHARS` | `150000` | transcript budget sent to the service |
| `CHAT_RESULTS` | `3` | how many passages are shown |
| `priority` | `-5` | button order in the toolbar; lower is further left |

There is no valve for the catalogues, for a row limit or for a live query: the
button has one corpus and one scope, so a setting that changed either would only
reintroduce the wrong answers this release removed.

Verify end to end: open a chat that has a few turns, press the Trick button (the
magnifier) under any answer, type `differential expression for RNA-seq counts`,
press Enter. Expected in about 1-2 s: a collapsed `Trick` disclosure appended to
that answer, titled `Trick · <query> · <n> passages match, showing the closest
<k> · best: turn <n> · <role>`. Click the title to open it: one row per hit,
`turn <n> · <role>` as a **link**, the query words that passage actually contains
(`matched: ...`, plus `(partial)` when the passage carries less than half of the
question's distinctive weight), and the passage. Clicking a row scrolls the chat
to that turn. Clicking the title again closes it.

The disclosure is closed by default because Open WebUI draws every `details`
block through a Collapsible that starts closed and takes its initial state from
the `Always Expand Details` interface setting, not from the message content. If
you would rather have Trick's results open on their own, turn that setting on:
Settings → Interface → Always Expand Details.

How the jump works, because it decides what to expect. Open WebUI renders an
`HTMLResponse` embed in an iframe whose sandbox is `allow-scripts allow-popups
allow-downloads`, and `iframeSandboxAllowSameOrigin` defaults to **false** in
0.12.4. Such a frame has an opaque origin: it cannot read the chat document, it
cannot be granted top-level navigation, and the page accepts only three message
types from it (`input:prompt`, `input:prompt:submit`, `action:submit`). There is
no supported way for a card inside that frame to scroll the chat, so this action
does not use a frame at all: it returns an **assistant** message with the block
appended (Open WebUI applies `res.messages` and saves it), and the block is
rendered by the chat page's own Markdown renderer.

Which message holds it matters. Open WebUI draws an assistant message through
that renderer (`renderMarkdownInAssistantMessages`, default true), but a user
message is drawn as plain text whenever `renderMarkdownInUserMessages` is off -
and then `**bold**` arrives as literal asterisks and a link arrives as text.
Measured on a real chat: the block had been written into the user's own question,
which is exactly why it looked like source and nothing was clickable. So the
target is the pressed message when it is an answer, otherwise the closest answer
above it, otherwise the closest one below. A chat that holds no answer yet gets a
warning and no block, instead of plain text that looks broken.

That makes every row a link to `/c/<chat>?message=<id>`, and both halves of that
are load-bearing. Open WebUI's renderer hardcodes `target="_blank"` on every
Markdown link it emits, so a fragment link (`#message-<id>`) opens a second tab
and the chat never moves - measured on a real chat, where the tab opened at the
top of the chat. A `/c/` link is intercepted by Open WebUI's own click handler,
which cancels that default navigation and routes in place; `Chat.svelte` then
watches the `message` query parameter reactively and calls `scrollToMessage`.
No script, no sandbox flag, no admin setting.

The block is one `<details>`/`<summary>` pair, which the shipped marked extension
parses into a token drawn by Open WebUI's Collapsible component: closed by
default, opened and closed with a click, and no other HTML anywhere in the block
(anything else would arrive on screen as its own source). The results live in the
message, so they persist with the chat, and a second search replaces the block
instead of stacking another one. `<summary>Trick · ` is the only part the action
ever rewrites; the text above it is never touched. The block itself is never
indexed: it names the query and the turns it found, so searching its own output
would rank a previous answer above the passage that holds the answer. The index
is built from the messages as they were before the block was appended.

The block never mixes corpora: every row is a passage of this conversation. A
catalogue row cannot appear here even if the bridge still returns one, because
the card does not read that field. Closing the input dialog without typing
searches nothing.

The block states its own confidence. A row lists the query words it contains
(`matched: ...`); when no passage contains the distinctive words of the
question - normal for Turkish, where "en", "cok", "ne" carry no signal - the
block says `Weak match` and shows the closest turns instead of presenting them
as answers. Function words are dropped from the query before scoring, for both
Turkish and English.

The catalogue is a separate surface, not a hidden half of this one:
`python -m trick query` and the MCP `trick_search` tool search CRAN,
Bioconductor, arXiv and the live sources, which is where a package lookup
belongs.

Failure text is intentional and names the fix:

| You see | Cause | Fix |
| --- | --- | --- |
| `TRICK_TOKEN` in the card | empty or stale valve | re-run the installer |
| `cannot reach ...` / `transport error` | bridge down or wrong URL | start the bridge, check `TRICK_URL` |
| `401` from the bridge | valve token differs from the file | re-run the installer |
| `Nothing matched` with `S4`/`S5` | a live source or the scorer did not answer (CLI/MCP surfaces) | check the network and the laya credentials |
| no button in the toolbar | function not global, or not active | `Admin Settings` → `Functions` → global toggle |

Remove: `Admin Settings` → `Functions` → `Trick` → trash icon. Nothing on the host
changes.

## 9. The R package

`r/trick` is the same find in R: same matching contract, same on-disk score
matrix, no compiled code and no dependencies. It is a separate package on
purpose - the corpora this tool cares about are R corpora, and a reader who works
in R should not have to leave the language to search their own material.

```sh
R CMD INSTALL r/trick          # install it
sh r/check.sh                  # build and check the way CRAN does
```

`r/check.sh` runs `R CMD build` and `R CMD check --as-cran --no-manual`. On a
host where R cannot find `which` (some builds hardcode `/usr/bin/which`), the
script reruns the check inside an unprivileged user namespace with a shim, so no
root access and no change to the host is needed.

What is shared with the Python implementation is proven, not claimed. Two
levels: `r/trick/inst/extdata/sample.trk` is a matrix written by the Python
writer and the R test suite asserts that R reads it and reproduces it byte for
byte; and `scripts/parity_r.py` (gate 18) runs one fixture through **both**
engines, comparing passage boundaries, message ids, ranking order for three
queries and the matrix bytes in both directions. Measured on this tree: 6
passages identical, 3 rankings identical, byte-identical writes each way. The
step is skipped, not failed, on a host without `Rscript`.

```r
library(trick)
index <- trick_index(list(
  list(id = "u1", role = "user", content = "Where did we set the threshold?"),
  list(id = "a1", role = "assistant", content = "At 0.15, after the second run.")
))
trick_search(index, "threshold 0.15", limit = 2)
```

The R package makes no network call and reads no credential; the model score is
an optional argument. Submitting it to CRAN is the maintainer's step: the package
name `trick` is free on CRAN, `r/check.sh` produces the tarball, and the two
NOTEs it reports (`New submission`, `unable to verify current time`) are expected
on a first submission and in a sandbox.

## 10. Encrypted memory backup (optional)

The local install is complete without this section and stays free. What follows
is only for carrying the small part that cannot be re-derived onto another
machine.

What is backed up: the decision cache (which query was answered, with which
scorer, when) and a settings snapshot with every credential replaced by the name
of the environment variable that holds it. What is **not** backed up: the
catalogue. It is 44 MB of public data that one `python -m trick ingest` rebuilds,
and a backup that carries it is both wasteful and pointless. On this tree the
archive is 372 KB against a 44 MB store.

```sh
.venv/bin/pip install 'cryptography>=42'      # or: .venv/bin/pip install -e '.[backup]'

# write an archive; the passphrase is asked for twice and never stored
.venv/bin/python -m trick backup export ~/trick-memory.trk

# inspect one (needs only the passphrase, not the local state)
.venv/bin/python -m trick backup info ~/trick-memory.trk

# merge one into this machine: newer rows win, nothing is deleted
.venv/bin/python -m trick backup import ~/trick-memory.trk
```

The passphrase can come from `TRICK_BACKUP_PASSPHRASE` for scripted use; it is
never written to disk, never printed, and never passed as an argument, because a
passphrase in `ps` output is a leaked passphrase. The archive is
`TRICKV1 | salt | nonce | AES-256-GCM`, key derived with scrypt (n=2^15, r=8,
p=1). A wrong passphrase, a flipped byte and a truncated file all fail the same
way - `cannot open the archive` - because GCM authenticates the whole payload
before anything is written into the local cache.

## 10b. Remote store (optional, $5/month)

Only if you want the archive on another machine. The local install is unaffected:
nothing in the terminal, MCP, Open WebUI or R surfaces talks to a store, and no
local feature is paid.

The store holds the sealed blob and has no key, so it cannot read a decision, a
setting or the passphrase. One real archive from this tree is 380,781 bytes
against a 64 MB default quota, which is why the cheapest instance is enough.

Server side, on the instance (Ubuntu 24.04, the $5 plan, static IP, only the
store port open):

```sh
sudo sh deploy/vault-server.sh /opt/trick-src
sudo systemctl start trick-vault
```

It prints the account token once and the certificate fingerprint. Copy the token
to the client, at mode 600:

```sh
install -m 600 /dev/null ~/.local/share/trick/vault.token
printf '%s' 'THE-TOKEN-PRINTED-ON-THE-SERVER' > ~/.local/share/trick/vault.token
```

Client side:

```sh
.venv/bin/python -m trick backup export ~/trick-memory.trk
.venv/bin/python -m trick backup push ~/trick-memory.trk \
    --remote https://YOUR-STATIC-IP:8790 --fingerprint THE-FINGERPRINT
.venv/bin/python -m trick backup list --remote https://YOUR-STATIC-IP:8790 --fingerprint THE-FINGERPRINT
.venv/bin/python -m trick backup pull SNAPSHOT-ID --out ~/restored.trk --verify \
    --remote https://YOUR-STATIC-IP:8790 --fingerprint THE-FINGERPRINT
.venv/bin/python -m trick backup import ~/restored.trk
```

`push` reads the object back and compares the hash before it reports success. The
fingerprint is the server's identity: a different box on the same address is
refused, which is why a self-signed certificate is enough. Quotas, the leak the
store cannot avoid, and rotation are in `docs/REMOTE.md`.

A backup that only happens when you remember it is not a backup, so there is a
timer. It writes a systemd --user unit and an environment file, and it refuses to
enable anything until the passphrase is in that file:

```sh
sh deploy/backup-timer.sh --remote=https://YOUR-STATIC-IP:8790 \
    --fingerprint=THE-FINGERPRINT --start
```

`--start` also runs the service once, so a broken setup fails now instead of at
03:30. What it runs is one command, and you can run the same one by hand:

```sh
.venv/bin/python -m trick backup run        # export, push, verify, clean up
```

Removal: `systemctl --user disable --now trick-backup.timer`, then delete
`~/.config/systemd/user/trick-backup.service` and `trick-backup.timer`, then
`systemctl --user daemon-reload`.

### 10b-2. Where the blobs live: local disk, S3, or another Linux box

The store process is the same in every case: same routes, same bearer token, same
quota logic, same TLS. Only the place the sealed blobs are written changes, and it
is one config file (`<root>/storage.json`).

| Backend | What it is | When it is the right answer |
| --- | --- | --- |
| `fs` (default) | a directory on the store's own disk | one box, up to ~1000 snapshots in 20 GB, no third party involved |
| `s3` | any S3-compatible endpoint: AWS S3, MinIO, Cloudflare R2, Backblaze B2 | the disk is small, the provider must hold the bytes, or the box must be disposable |

The index stays on local disk in every case. It is a few kilobytes that the store
locks and rewrites while enforcing quota, and an object store has no atomic
read-modify-write, so putting it there would turn a quota check into a race.

Credentials never go into `storage.json`; it holds bucket names, not keys, because
config files end up in bug reports. On a box installed by `deploy/vault-server.sh`
there is already a 0600 file for them, wired into the unit as an
`EnvironmentFile`:

```sh
sudo editor /etc/trick-vault/storage.env      # TRICK_S3_ACCESS_KEY=... ; TRICK_S3_SECRET_KEY=...
sudo chmod 600 /etc/trick-vault/storage.env
sudo systemctl restart trick-vault
```

Anywhere else, the same two variables in the service environment, or a 0600 file
passed as `--credentials-file`:

```sh
install -m 600 /dev/null ~/.config/trick/s3.credentials
printf 'TRICK_S3_ACCESS_KEY=%s\nTRICK_S3_SECRET_KEY=%s\n' 'AKIA...' 'THE-SECRET' > ~/.config/trick/s3.credentials
```

Check a bucket before trusting it. `storage test` writes a probe object, reads it
back, compares and deletes it; a wrong bucket, region or secret fails here instead
of at 03:30:

```sh
.venv/bin/python -m trick vault storage test --root /var/lib/trick-vault \
    --backend s3 --endpoint https://s3.eu-central-1.amazonaws.com \
    --bucket YOUR-BUCKET --region eu-central-1 \
    --credentials-file ~/.config/trick/s3.credentials

.venv/bin/python -m trick vault storage show --root /var/lib/trick-vault
```

Switching backends. This is a copy plus a read-back check per object; the config
is rewritten **last**, so an interrupted migration leaves the store pointing at a
source that is still complete. The old blobs are kept unless you say otherwise.

```sh
# 1. stop the store so nothing writes while the blobs move
systemctl --user stop trick-vault

# 2. copy, verify every object, then switch the config to s3
.venv/bin/python -m trick vault storage migrate --root /var/lib/trick-vault \
    --backend s3 --endpoint https://s3.eu-central-1.amazonaws.com \
    --bucket YOUR-BUCKET --region eu-central-1 \
    --credentials-file ~/.config/trick/s3.credentials

# 3. start it again and confirm what it reads
systemctl --user start trick-vault
.venv/bin/python -m trick backup list --remote https://YOUR-HOST:8790 --fingerprint THE-FINGERPRINT

# going back is the same command with --backend fs
```

What the migration reports, and what it refuses to do:

* every object is read from the source, hashed, written to the target, read back
  from the target and hashed again; only then does it count as copied;
* one refused write stops the run and the config is left unchanged, because a
  half-migrated store that already points at the target is worse than one that
  never moved;
* `--dry-run` prints the byte and object totals and writes nothing;
* `--keep-source` is the default; add nothing and the old blobs stay, so you can
  migrate back by running the command in reverse. Only when the new backend has
  served a real restore should you delete the old copies.

Measured on this tree with a real archive (380,781 bytes, 77 decisions) through
three legs -- local disk, S3, local disk again -- the pulled file hashed to the
pushed sha256 every time, and the rows after import were identical. The script
that does it is `scripts/verify_storage.py`; run it once on your own stack before
pointing production at a new bucket.

### 10c. Plans and devices (optional)

A plan is a set of limits on the server, not a feature flag on the client: the
free build is complete, and a plan only buys room for the sealed archive. The
table and what is enforced where are in `docs/PLANS.md`.

```sh
# see every row, with the numbers the store actually enforces
.venv/bin/python -m trick vault plans

# what this account is on right now
.venv/bin/python -m trick vault plan --root ~/.local/share/trick/vault-store --account owner

# move it to a plan by hand (an operator, not a payment provider)
.venv/bin/python -m trick vault plan --root ~/.local/share/trick/vault-store \
    --account owner --set backup

# one more device: prints a token once, refuses beyond the plan's device count
.venv/bin/python -m trick vault device --root ~/.local/share/trick/vault-store --account owner
```

Both commands rewrite `accounts.json` in one atomic write and never create an
account: an unknown name is an error, so a mistyped webhook cannot conjure a disk.

To let a payment provider move plans automatically, give the store a secret. On
the server, in the file the unit already looks for (absent by default, and with
it absent the route answers 401):

```sh
sudo install -m 600 -o trickvault -g trickvault /dev/null /etc/trick-vault/admin.env
sudo sh -c 'printf "TRICK_ADMIN_SECRET=%s\n" "THE-SHARED-SECRET" > /etc/trick-vault/admin.env'
sudo systemctl restart trick-vault
```

The claim body, the signature header, the 300-second timestamp window and the
replayed-event ledger are documented in `docs/PLANS.md`. Without the secret no
plan can change, which is the intended default.

For a store you run by hand instead (no systemd), the same secret goes in the
environment:

```sh
TRICK_ADMIN_SECRET=the-shared-secret .venv/bin/python -m trick vault serve \
    --root ~/.local/share/trick/vault-store --host 127.0.0.1 --port 8790 \
    --cert /etc/trick-vault/tls/cert.pem --key /etc/trick-vault/tls/key.pem
```

Renting the box itself is scripted, and the script prints before it spends:

```sh
sh deploy/lightsail.sh plan     # prints the exact AWS commands, runs none
sh deploy/lightsail.sh status   # needs lightsail:GetInstance
TRICK_LIGHTSAIL_CONFIRM=yes sh deploy/lightsail.sh create
```

Its header lists the exact IAM actions required. A publish-only key gets
`AccessDeniedException`, which is the correct outcome rather than a workaround.

## 11. Everything this project creates

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
| `~/trick-memory.trk` | step 10, only if you asked for it | `rm -f` that file |
| `$HOME/.local/share/trick/vault.token` | step 10b, only if you push | `rm -f vault.token` |
| `$HOME/.local/share/trick/vault-store/` | only if you run a store here | `rm -rf vault-store/` |
| `$HOME/.local/share/trick/vault-store/entitlements.json` | first signed entitlement (step 10c) | `rm -f entitlements.json` |
| `$HOME/.config/trick/backup.env` | step 10b timer | `rm -f backup.env` |
| `$HOME/.config/systemd/user/trick-backup.timer` | step 10b timer | disable it, then delete the unit |
| laya credentials file | step 3 | `rm -f` that file |

No process survives a reboot except the systemd unit from step 7b, and that one
exists only if you installed it.

## 12. When something is degraded

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

Nineteen checks: tests, byte-compile, on-disk layout, ranking constants, no
network in tests, MCP and bridge surfaces, socket and token permissions,
English-only repository, no credentials in tracked files, stdout purity,
integration surfaces, integration secrets, documentation truth (every documented
command and path must exist), conversation-search quality against planted
needles, conversation search on a real chat shape (identifiers, rare words and
tokens that must return nothing), result rows that jump to their message, the
score matrix build and read-back, R parity with the Python engine, and a real TLS
memory store pushed, pulled and imported with byte, payload and row parity.

Two of them are floors, not targets. Conversation quality fails the gate if
`recall@3` drops below 0.9, `MRR@10` below 0.8 or a decoy becomes the top hit
more than 20% of the time. The real-chat gate fails it if identifier `recall@3`
drops below 0.9 or a query whose words occur nowhere returns a single row - that
second one is the "confident wrong answer" class, and it is why an unrelated
answer cannot come back unnoticed.
