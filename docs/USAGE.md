# Using Trick

Trick is a **scored find** over two corpora:

1. the conversation you are in, and
2. the scientific catalogues (CRAN, Bioconductor, arXiv, OpenAlex, Crossref).

It does not answer questions and it does not read meaning. It finds the places a
piece of text occurs, then asks laya to order the closest few. That distinction
is the whole of this page: a query shaped like a find works, a query shaped like
a question usually does not.

## The Open WebUI button

Press the magnifier under any answer. A dialog opens; type what to look for and
press Enter. The results are written into that answer as a collapsible block, and
each row is a link that scrolls the chat to the message it came from.

```
Trick · gonderildi:false · 3 passages match, showing the closest 3 · best: turn 39 · assistant

1. turn 39 · assistant · matched: gonderildi, false · 9.46
   > ...the panel code was turned fail-closed, but no test covers gonderildi:false...
2. turn 3 · assistant · matched: false · 3.46
   > ...the ACL-test false-green fix in kur.sh...
```

Every label on a row is a fact about that row, not decoration:

| Label | Meaning |
| --- | --- |
| `turn 39 · assistant` | which message, counting from the first message of this chat |
| `matched: a, b` | the query words that literally occur in that passage |
| `(partial)` | the passage carries less than half of the question's distinctive weight: closest turn, not an answer |
| `9.46` | blended score, `0.2 x laya + 0.8 x retrieval`; comparable inside a list, never across searches |
| `Weak match` | no passage carries the distinctive words of the question |
| `One word was searched` | the query was a single token; the block says how many passages contain it |
| `_degraded: ..._` | a live source or the scorer did not answer; the list is retrieval-only |

## Query shapes, measured on real chats

The examples below are from a 47-message chat, 338 indexed passages. Queries may
be in any language; they are written without diacritics here because the
repository gate rejects non-ASCII letters in tracked files.

| Query | What happens |
| --- | --- |
| `gonderildi:false` | 1 passage contains both words; it is row 1. **Best shape.** |
| `VeriView.vue:398` | a file-and-line fragment; it lands on the audit row that names it. |
| `False` | 3 of 338 passages contain it, and rows 1-3 are exactly those three. Correct, and useless as an answer: a token that appears in a handful of code snippets finds code snippets. |
| `where did we have the most problems` | no passage says that; only the rare token (`i5`) discriminates, so every row matches `i5` alone and the block says `Weak match`. |

Rules of thumb:

- **Good:** an identifier, an error string, a file path, a `key:value` pair, a
  phrase you remember verbatim, a rare word. `gonderildi:false`,
  `VeriView.vue:398`, `MemoryMax`, `taper in the dispersion plot`.
- **Bad:** one common word (`False`, `error`), a whole sentence in your own words,
  or a question. Trick searches for the words you typed; it does not infer the
  ones you did not.
- If a query returns nothing, drop words until it does, then add the rarest one
  back. The block tells you which words actually matched, so you can see where it
  went wrong.

Scope is the current conversation. Each turn is split into passages of 120-600
characters at sentence boundaries, the most recent 200 turns are searched, and
Trick's own result blocks are cut out of the text before indexing, so a search
never matches its own previous output.

## Terminal, one-shot and MCP

```sh
.venv/bin/python -m trick tui                                  # session; Enter submits
.venv/bin/python -m trick query "gonderildi:false" --json       # one shot
.venv/bin/python -m trick tui --transcript chat.json            # search an exported chat
.venv/bin/python -m trick mcp                                  # stdio MCP server
```

The terminal has the same two corpora and the same ranking path as the button: a
query returns the same rows in both. `python -m trick query` prints the catalogue
sources as well, because there is no conversation to search unless `--transcript`
is given.

Over MCP the tool is `trick_search`: `{"query": "...", "limit": 8}`, plus
`trick_health`. A host that can pass the conversation (Open WebUI, or any MCP
client with message context) may send `messages` as well, and then the
conversation is searched too.

## Valves of the Open WebUI action

| Valve | Default | Effect |
| --- | --- | --- |
| `SOURCES` | `never` | `never` keeps the button a find-in-conversation tool; `auto` lists catalogue sources only when the conversation had no match; `always` lists them beside every hit |
| `CHAT_RESULTS` | `3` | how many passage rows to show |
| `SHOW_SCORES` | `true` | show the blended score per row |
| `SEARCH_CHAT` | `true` | search the conversation as well as the catalogues |
| `CHAT_TURNS` | `200` | how many recent turns to index; older ones are dropped and counted |
| `LIMIT` | `8` | catalogue sources to show when `SOURCES` is not `never` |
| `LIVE` | `true` | query the live source APIs, not only the local catalogue |
| `TRICK_URL` | `http://host.docker.internal:8765/v1/search` | bridge address as seen from inside the container |
| `TRICK_TOKEN` | empty | contents of `$TRICK_HOME/bridge.token` |

## What Trick does not do

- It does not answer questions, summarise, or invent. There is no language model
  in the answer path; laya orders candidates and nothing else.
- It does not search the web. It searches the conversation and the catalogues.
- It does not search other chats. The button reads the chat it sits in, through
  the message branch the button was pressed on.
- It does not run a cloud fallback in the local build. Every surface above works
  offline, with no account, and nothing is sent anywhere except the sources and
  the scorer you configured.

If a result looks unrelated, read the `matched:` line first. It is the honest
answer to "why is this here", and it is usually the shortest route to a better
query.
