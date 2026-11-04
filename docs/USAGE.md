# Using Trick

Trick is a **scored find** over two corpora:

1. the conversation you are in, and
2. the scientific catalogues (CRAN, Bioconductor, arXiv, OpenAlex, Crossref).

The two corpora are two products, and they never share a screen. **The Open WebUI
button searches the conversation only.** A chat question answered with eight R
packages read as noise, so that is no longer even a setting: the button has one
corpus. **The catalogues live in the terminal and in MCP**, where the question
"which package is this" is the question being asked.

It does not answer questions and it does not read meaning. It finds the places a
piece of text occurs, then asks laya to order the closest few. That distinction
is the whole of this page: a query shaped like a find works, a query shaped like
a question usually does not.

## Who this is for

Three people, three needs, all of them "I know the text, I cannot find the place":

- **Someone in a long chat.** Past forty turns, the browser's own find returns a
  scrollbar of hits and no order. You remember the number, the file name, the error
  string, the sentence - not which turn it was in. Trick returns that passage, ranked,
  with the turn number and a link that scrolls to it.
- **A researcher choosing a tool.** "Which R package does negative binomial
  differential expression for RNA-seq" is a catalogue question with hundreds of
  plausible neighbours; the catalogues answer it in one call, and the ranking is
  measured (mrr@10 0.895).
- **An agent with an MCP host.** The same two lookups, as tools, for a model that
  would otherwise guess.

Concretely: it replaces `Ctrl+F` in a chat and a page of CRAN search results. That is
the whole ambition, and it is why the tool is small.

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

## What an administrator does once

A user needs nothing installed: the button lives in the Open WebUI instance, and
the search runs on the machine that hosts Trick. Five minutes, once:

1. Install Trick on a host the Open WebUI container can reach, and start the bridge
   on the docker bridge address: `python -m trick serve --http-host 172.17.0.1
   --expose-network --http-port 8765`. `127.0.0.1` will not work from inside a
   container - that is the first thing that bites.
2. In Open WebUI: Settings > Account > API Keys > Create, and copy the key.
3. Point the installer at it: `TRICK_OPENWEBUI_API_KEY=<key> python
   integrations/openwebui/install.py`. It uploads the function, enables it globally,
   writes the bridge token into the function's valves and reads the stored copy back
   to prove it.
4. Verify without touching the browser: `python integrations/openwebui/install.py
   --check --db <copy of webui.db>`. It must say `up to date`.
5. Open a chat, hover an answer, press the magnifier.

Nothing else is required: no model to select, no Pipe in the model list, no prompt.
If the magnifier does not appear, the function is not global or not active - check
the row in Admin Settings > Functions. If it opens but the search fails, the bridge
is not running or its address is not the docker bridge address.

The full path, with the failure modes and the removal commands, is `docs/INSTALL.md`
section 8.

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

## Turkish, and what a fold does

Turkish adds two problems an English index never sees, and both were measured on
real chats: three of the user's own conversations, 36 gold passages.

| Query shape | Before | After |
| --- | --- | --- |
| the word exactly as the chat spells it | 35/36 | 35/36 |
| the same word typed in ASCII (i for the dotless i) | 0/36 | 35/36 |
| a five-letter stem, a different inflection in the chat | 9/36 | 26/36 |

What changed:

- **Both sides are folded.** The dotless i is a letter, not a diacritic, so
  SQLite's tokenizer never folded it: a reader typing `yasadigimiz` with an
  ordinary i matched nothing, while the chat holds the same word with a dotless
  i. Index and query are now folded to the same ASCII form before matching. The
  text you see is still the text that was written.
- **The coverage filter uses the same rule as the index.** It used to compare
  raw strings, so a passage the index had found was discarded one step later and
  the search returned nothing at all - silently. Both now agree.
- **A stem reaches its inflections.** `yapil` reaches `yapildi` and `yapilan`.
  Prefix expansion used to run only when the exact form found nothing, so a stem
  that also exists as a word abandoned every inflection of it: measured on a real
  chat, stem recall@3 was 0.50 against 1.00 for the exact surface form. Both
  channels now run, and prefix-only hits are damped to 0.8 so a passage that
  spells the term exactly still outranks one that merely shares the stem.
- **A verbatim hit outranks the noise it tokenises into.** `#11` becomes the token
  `11`, which occurs in every count and timestamp, so the passage holding the pull
  request ranked fourth. The passage that spells out what you typed is now placed
  above every non-verbatim hit, and it is a candidate even when the prefilter would
  have truncated it. Measured on a real chat: identifier recall@1 0.43 -> 1.00.

The limit, stated plainly: prefix matching finds inflections that extend the stem
(`yapil` -> `yapilan`), not ones that change it inside (`yazim` vs `yazilmasi`).
That needs a real Turkish stemmer, which Trick does not have. If a stem search
returns nothing, shorten it by hand or use a word you are sure of.

`scripts/eval_turkish.py` re-runs this measurement on any chat export, so the
numbers above can be checked instead of trusted: it takes an Open WebUI export
and prints the three recalls with a floor per row. On the same three chats with
60 gold passages it reports exact 0.967, ascii 0.967, stem 0.750.

`scripts/eval_real.py` measures the harder question, the one a real chat asks: it
has no needles at all. It derives the queries from the chat itself - file names,
`file:line`, pull-request numbers, constants, words that occur once - and adds
tokens that occur nowhere, which must return nothing. That last row is the one
that catches a confident wrong answer. On two real chats with 30 queries per
shape: identifier recall@3 1.00 and 1.00, rare 1.00 and 1.00, absent-query false
positives zero.

What laya adds in Turkish, measured rather than assumed: on a single rare word
it agreed with retrieval's top pick 14 of 15 times. On ambiguous two-word queries
it is not the thing carrying the search - its own top-1 was 1 of 16 against
retrieval's 14 of 16, and the confidence gate kept it out of 14 of those 16
queries, so the shipped blend returned exactly what retrieval alone returned
(14 of 16 top-1, 15 of 16 top-3). The conversation scope is lexical; laya is the
tie-breaker, and the gate is what keeps a tie-breaker from becoming a tie-maker.

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
| `CHAT_RESULTS` | `3` | how many passage rows to show |
| `SHOW_SCORES` | `true` | show the blended score per row |
| `CHAT_TURNS` | `200` | how many recent turns to index; older ones are dropped and counted |
| `CHAT_MAX_CHARS` | `150000` | transcript budget sent to the service |
| `TRICK_URL` | `http://host.docker.internal:8765/v1/search` | bridge address as seen from inside the container |
| `TRICK_TOKEN` | empty | contents of `$TRICK_HOME/bridge.token` |
| `TIMEOUT_S` | `25` | bridge timeout |

There is no corpus valve, no row limit and no live toggle: the button searches
the conversation, so those settings had nothing left to steer.

## Memory, and what a backup contains

Three things live in `$TRICK_HOME` and only one of them is worth carrying:

| File | Size here | Re-derivable | In the backup |
| --- | --- | --- | --- |
| `trick.db` catalogue (28 495 rows) | 44 MB | yes, one ingest | no |
| `trick.db` decision cache (77 rows) | - | no, it cost model calls | yes |
| `scores.trk` int8 matrix | 668 KB | yes, one build | header only |

That split is what makes a $5 cloud box enough for a few hundred users: the part
you back up is hundreds of kilobytes, and the part that is large is public data
you can rebuild anywhere. `python -m trick backup export` writes an encrypted
archive of the middle row only; `import` merges it, newer rows winning, and never
deletes anything you already have.

`backup push` and `backup pull` move that same archive to a store you run, which
holds the sealed blob and has no key. A real archive here is 380,781 bytes for 77
decisions, so 100 accounts fit in the cheapest instance. The store is optional and
the local build never contacts it; see `docs/REMOTE.md`.

## What Trick does not do

- It does not answer questions, summarise, or invent. There is no language model
  in the answer path; laya orders candidates and nothing else.
- It does not search the web. In a chat it searches that conversation; in the
  terminal and over MCP it searches the scientific catalogues.
- It does not search other chats. The button reads the chat it sits in, through
  the message branch the button was pressed on.
- It does not run a cloud fallback in the local build. Every surface above works
  offline, with no account, and nothing is sent anywhere except the sources and
  the scorer you configured.

If a result looks unrelated, read the `matched:` line first. It is the honest
answer to "why is this here", and it is usually the shortest route to a better
query.
