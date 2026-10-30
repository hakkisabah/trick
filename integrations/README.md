# Trick integrations

Two hosts, one engine. Both are thin clients for `trick serve`: no ranking code
is duplicated, so the terminal, Open WebUI and Gemini CLI cannot disagree about
the same query.

```
Open WebUI Pipe  --HTTP bridge-->  trick serve  -->  retrieval + laya + matrix
Gemini CLI (MCP) --stdio MCP----->  trick serve  -->  same engine, same path
Gemini CLI (MCP) --stdio MCP----->  in-process engine (when serve is not running)
```

## Open WebUI

A Pipe is a model in Open WebUI, which is the right shape here: pick `Trick`,
type a question, press Enter, get ranked sources instead of prose.

### 1. Start the service so a container can reach it

In Docker, `127.0.0.1` inside a Pipe means "the Open WebUI container", not your
host. The bridge refuses a wildcard bind on purpose, so bind it to the docker
bridge address instead - reachable from containers, not from the LAN:

```sh
cd /home/hakki/evrim_otonom/trick
.venv/bin/python -m trick serve --http-host 172.17.0.1 --expose-network --http-port 8765
```

`172.17.0.1` is `docker0`. Read yours with:

```sh
ip -4 -o addr show docker0 | awk '{print $4}'
```

Without Docker, the default `trick serve` (127.0.0.1:8765) is enough.

### 2. Install the Pipe

Create an API key in Open WebUI: avatar -> Settings -> Account -> API Keys ->
Create. Then:

```sh
cd /home/hakki/evrim_otonom/trick
TRICK_OPENWEBUI_API_KEY=paste-the-key-here python3 integrations/openwebui/install.py
```

The installer creates or updates the function, reads it back, and compares the
stored byte count with the file, so "it said OK" is not the only evidence. Use
`--dry-run` to see what it would do. Key can also be given for a remote instance:

```sh
TRICK_OPENWEBUI_URL=http://localhost:3000 TRICK_OPENWEBUI_API_KEY=... \
  python3 integrations/openwebui/install.py
```

### 3. Paste the bridge token

Admin panel -> Functions -> Trick -> Valves:

| Valve          | Value                                                    |
| -------------- | -------------------------------------------------------- |
| `TRICK_URL`    | `http://host.docker.internal:8765/v1/search` (Docker) or `http://127.0.0.1:8765/v1/search` (native) |
| `TRICK_TOKEN`  | contents of `~/.local/share/trick/bridge.token`            |
| `LIMIT`        | 8 (1-26)                                                  |
| `LIVE`         | true                                                      |

Set `TRICK_URL` explicitly: the default points at `host.docker.internal`, which
only exists when the compose file ships `extra_hosts` (the shipped one does).

### 4. Use it

New chat -> model `Trick - scored scientific sources` -> type a question ->
Enter. Each row shows the title as a link, its summary, `source`/`key`, and the
blended score. `*` means laya scored it, `-` means retrieval only.

### Failure text is part of the product

The Pipe never returns a blank answer. A wrong token names the valve to fix, a
dead service names `TRICK_URL`, and an empty result shows the `S4`/`S5` reasons
so "no results" cannot be mistaken for "no matches".

## Gemini CLI

The extension registers the same MCP server as `trick mcp` and adds two commands.

```sh
cd /home/hakki/evrim_otonom/trick/integrations/gemini-extension
gemini extensions link . --consent
```

Restart Gemini CLI, then `/trick:search negative binomial differential expression`
or just ask a question - `GEMINI.md` tells the model when to call `trick_search`.

### Why a wrapper script

Gemini CLI spawns MCP servers with a sanitised environment; `PATH` is not your
shell's `PATH`, so a manifest with `"command": "trick"` fails with
`spawn trick ENOENT` (measured on 0.66.0). `trick-mcp.sh` resolves the
interpreter itself: `$TRICK_PYTHON`, then `<repo>/.venv/bin/python`, then
`python3`, then `/usr/bin/python3`, and imports the package from `<repo>/src`.
No install step, no PATH assumption.

If your python lives somewhere unusual, set the extension setting
`Trick Python` to its absolute path instead of editing the manifest.

## Notes that cost a debugging session

- `trick serve --http-host 172.17.0.1` without `--expose-network` is refused.
  That is deliberate: the flag is the only way to say "I meant a network bind".
- `0.0.0.0` is rejected even with the flag. The service has no accounts; a
  wildcard bind plus a leaked token means someone else spends your laya credits.
- The bridge token file is `0600`, and the bridge compares it with
  `hmac.compare_digest`; a wrong token gets `401` before the body is parsed.
