# Trick integrations

Two hosts, one engine. Both are thin clients for the core service: no ranking code
is duplicated, so the terminal, Open WebUI and any MCP host cannot disagree about
the same query.

```
Open WebUI Pipe   --HTTP bridge-->  core service  -->  retrieval + laya + matrix
MCP host (stdio)  --stdio MCP----->  core service  -->  same engine, same path
MCP host (stdio)  --stdio MCP----->  in-process engine (when the service is down)
```

Installation, verification and removal for every artifact live in one place:
`docs/INSTALL.md`, step 6 for MCP and step 8 for Open WebUI. That document is the
contract; this one only explains why the integration looks the way it does.

## Open WebUI

A Pipe is a model in Open WebUI, which is the right shape here: pick `Trick`, type
a question, press Enter, get the matching passages of the conversation plus ranked
sources instead of prose.

Open WebUI hands `pipe()` the whole chat as a `messages` list. The Pipe windows it
(the last `CHAT_TURNS` turns, at most `CHAT_MAX_CHARS` characters), sends it as
`messages` with `scope: "both"`, and prints the conversation block above the source
list. Dropped turns are counted and shown - "oldest 7 turns not searched" - because
a silent window looks like "it is not in the chat".

Three facts make this integration what it is, all measured:

- A container cannot reach the host's `127.0.0.1`. The bridge refuses a wildcard
  bind on purpose, so the documented bind is the docker bridge address instead:
  reachable from containers, not from the LAN.
- The functions API is admin-only. A key from a non-admin account returns `401`
  even when the key itself is valid, which is why the installer checks the listing
  endpoint before it posts anything.
- The function runs inside the container's Python, which may be a different version
  than the host's. `tests/test_openwebui_pipe.py` loads the Pipe with only a
  two-line `pydantic` stub, so anything the file needs beyond the standard library
  fails in the test before it fails in the container.

The Pipe never returns a blank answer. A wrong tag mentions the valve to fix, a
dead service names `TRICK_URL`, and an empty result shows the `S4`/`S5` reasons so
"no results" cannot be mistaken for "no matches".

## MCP

`python -m trick mcp` is a stdio JSON-RPC 2.0 server. Any MCP host can use it with
the absolute interpreter path and `PYTHONPATH=<repo>/src`; hosts spawn servers with
a sanitised environment, so a `PATH`-dependent command fails with `ENOENT`.

| Tool | Use it when |
| --- | --- |
| `trick_search` | the answer may be in the chat (`messages`) or needs a package or paper (`query`) |
| `trick_health` | a result looks wrong and the state should be checked before blaming the data |

The tool description tells the model to pass `messages` whenever the answer may
already be in the conversation; without that argument the call searches catalogues
only, which is the silent-wrong-answer mode this project exists to avoid.

## Notes that cost a debugging session

- The bridge refuses a non-loopback host unless `--expose-network` is given, and
  refuses `0.0.0.0` even with the flag. The flag is the only way to say "I meant a
  network bind".
- The service has no accounts: a wildcard bind plus a leaked token means someone
  else spends your laya credits. That is the whole reason for the policy.
- The bridge token file is `0600`, and the bridge compares it with
  `hmac.compare_digest`; a wrong token gets `401` before the body is parsed.
- Request bodies are capped at 1 MiB. A 200-turn transcript fits; if a chat is
  bigger, window it at the caller and show how much was dropped.
- Open WebUI listens on 8080 inside its container; on this host only 3000 is
  published, so a browser never uses 8080.
