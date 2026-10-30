# Trick integrations

Two hosts, one engine. Both are thin clients for the core service: no ranking
code is duplicated, so the terminal, Open WebUI and Gemini CLI cannot disagree
about the same query.

```
Open WebUI Pipe  --HTTP bridge-->  core service  -->  retrieval + laya + matrix
Gemini CLI (MCP) --stdio MCP----->  core service  -->  same engine, same path
Gemini CLI (MCP) --stdio MCP----->  in-process engine (when the service is down)
```

Installation, verification and removal for every artifact live in one place:
`docs/INSTALL.md`, step 8 for Open WebUI and step 9 for Gemini CLI. That document
is the contract; this one only explains why the integration looks the way it
does.

## Open WebUI

A Pipe is a model in Open WebUI, which is the right shape here: pick `Trick`,
type a question, press Enter, get ranked sources instead of prose.

Two facts make this integration what it is, both measured:

- A container cannot reach the host's `127.0.0.1`. The bridge refuses a wildcard
  bind on purpose, so the documented bind is the docker bridge address instead:
  reachable from containers, not from the LAN.
- The functions API is admin-only. A key from a non-admin account returns `401`
  even when the key itself is valid, which is why the installer checks the
  listing endpoint before it posts anything.

The Pipe never returns a blank answer. A wrong token names the valve to fix, a
dead service names `TRICK_URL`, and an empty result shows the `S4`/`S5` reasons
so "no results" cannot be mistaken for "no matches".

## Gemini CLI

The extension registers the same MCP server (`python -m trick mcp`) and adds two
commands, `/trick:search` and `/trick:health`. `GEMINI.md` tells the model when to
call `trick_search` instead of answering from memory.

### Why a wrapper script

Gemini CLI spawns MCP servers with a sanitised environment; `PATH` is not your
shell's `PATH`, so a manifest with `"command": "trick"` fails with
`spawn trick ENOENT` (measured on 0.66.0). `trick-mcp.sh` resolves the interpreter
itself: `$TRICK_PYTHON`, then `<repo>/.venv/bin/python`, then `python3`, then
`/usr/bin/python3`, and imports the package from `<repo>/src`. No install step, no
`PATH` assumption.

If your python lives somewhere unusual, set the extension setting
`Trick Python` to its absolute path instead of editing the manifest.

## Notes that cost a debugging session

- The bridge refuses a non-loopback host unless `--expose-network` is given, and
  refuses `0.0.0.0` even with the flag. The flag is the only way to say "I meant
  a network bind".
- The service has no accounts: a wildcard bind plus a leaked token means someone
  else spends your laya credits. That is the whole reason for the policy.
- The bridge token file is `0600`, and the bridge compares it with
  `hmac.compare_digest`; a wrong token gets `401` before the body is parsed.
- Open WebUI listens on 8080 inside its container; on this host only 3000 is
  published, so a browser never uses 8080.
