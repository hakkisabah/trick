"""Measure conversation search on real chats, without a hand-made gold set.

The synthetic harness (``eval_chat.py``) knows where every needle is. Real chats
do not come with answers, so this script derives the gold set from the chat
itself. The trick: a token a person would actually type into find is also a
token whose passage is unambiguous ground truth.

Three question shapes are generated, in the order people use them:

``identifier``
    a file name, a ``file:line``, a pull-request number, a constant such as
    ``MemoryMax``, a ``key:value`` flag. Typing one of these and getting the
    passage that contains it is the whole point of the tool.
``rare``
    a word that occurs exactly once in the chat. This is the "I remember a word
    but not the sentence" case, and it must not be drowned by repetition.
``absent``
    a token that occurs nowhere. It must return nothing: an empty answer is
    correct here, and a confident wrong row is the failure the user reported
    ("I asked about the chat and got R packages").

Each query is scored only against passages of the same chat. Reported per shape:
recall@1, recall@3, MRR@10, and for absent queries the false-positive rate.

Usage::

    python scripts/eval_real.py chat.json [chat2.json ...]
    python scripts/eval_real.py --scorer remote chat.json
    python scripts/eval_real.py --limit 40 --json chat.json

The input may be an Open WebUI export (``{"history": {"messages": {...}}}``,
``{"messages": [...]}``) or a bare list of messages. Nothing is written to disk:
the output is aggregate metrics, so a private chat never becomes a fixture.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trick.laya import build_scorer  # noqa: E402
from trick.transcript import (  # noqa: E402
    ConversationSearch,
    chunk_messages,
    fold_tr,
    normalise_messages,
)

# Tokens a person types into find. Ordered by how much they narrow the chat: a
# file:line is almost unique, a constant usually is, a bare file name often is
# not. Each pattern is tried only while the token occurs in few enough passages.
PATTERNS = (
    ("file-line", re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:json|toml|ya?ml|tsx|vue|sql|py|sh|md|ts|js|rs):\d{1,4}")),
    ("identifier", re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:json|toml|ya?ml|tsx|vue|sql|py|sh|md|ts|js|rs|gz|db)")),
    ("pull-request", re.compile(r"#\d{1,4}\b")),
    ("constant", re.compile(r"\b(?:[A-Z][A-Z0-9_]{2,}|[a-z]+(?:[A-Z][a-z0-9]+){1,3})\b")),
    ("flag", re.compile(r"\b[a-z_]{3,}:(?:true|false|null|\d{1,4})\b")),
)

# Tokens that must not occur in any chat. They are ordinary words from other
# domains, so a retrieval layer cannot excuse a hit as a near-miss.
ABSENT = (
    "photosynthesis",
    "antikythera",
    "gondwanan",
    "supersymmetry",
    "bioreactor",
    "kuzeydogu",
)

WORD = re.compile(r"[0-9A-Za-z\u00c0-\u024f_]{6,}")
MAX_DF = 5  # a query that occurs in more passages than this is not a gold pair


def order_owui(messages: dict) -> list[dict]:
    """Flatten Open WebUI's id-keyed message tree, following the last branch."""
    if not isinstance(messages, dict):
        return list(messages)
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for mid, node in messages.items():
        parent = (node or {}).get("parentId")
        if parent and parent in messages:
            children.setdefault(parent, []).append(mid)
        else:
            roots.append(mid)
    if not roots:
        return [node for node in messages.values() if isinstance(node, dict)]

    def walk(mid: str) -> list[dict]:
        node = messages.get(mid) or {}
        item = dict(node)
        item.setdefault("id", mid)
        out = [item]
        kids = children.get(mid, [])
        if kids:
            out.extend(walk(kids[-1]))  # the active branch is the newest child
        return out

    flat: list[dict] = []
    for root in roots:
        flat.extend(walk(root))
    return flat


def load_messages(path: Path) -> list:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("history"), (dict, list)):
        history = raw["history"]
        payload = history.get("messages") if isinstance(history, dict) else history
        if isinstance(payload, dict):
            payload = order_owui(payload)
        return normalise_messages(payload or [])
    if isinstance(raw, dict) and isinstance(raw.get("messages"), (dict, list)):
        payload = raw["messages"]
        if isinstance(payload, dict):
            payload = order_owui(payload)
        return normalise_messages(payload or [])
    if isinstance(raw, list):
        return normalise_messages(raw)
    if isinstance(raw, dict):
        return normalise_messages(order_owui(raw))
    raise SystemExit(f"{path}: unrecognised chat shape")


def document_frequency(chunks) -> dict[str, int]:
    df: dict[str, int] = {}
    for chunk in chunks:
        for token in set(fold_tr(chunk.text).split()):
            token = token.strip("\"'()[]{}.,;:!?`*")
            if token:
                df[token] = df.get(token, 0) + 1
    return df


def build_cases(chunks, limit: int) -> dict[str, list[tuple[str, set[int]]]]:
    """Return ``{shape: [(query, {gold chunk ids})]}``, deterministic order."""
    folded = [fold_tr(chunk.text) for chunk in chunks]
    # A row identifies its passage by turn and offset: ChatRow exposes those, and
    # they are stable between the index build and the ranked output.
    where_of = [(chunk.turn, chunk.char_start) for chunk in chunks]
    holders: dict[str, set[tuple[int, int]]] = {}
    spelling: dict[str, str] = {}
    for _name, pattern in PATTERNS:
        for idx, text in enumerate(folded):
            for match in pattern.findall(text):
                token = match.strip("\"'()[]{}.,;:`*")
                if len(token) < 3:
                    continue
                holders.setdefault(token, set()).add(where_of[idx])
                spelling.setdefault(token, match)

    df = document_frequency(chunks)
    identifiers: list[tuple[str, set[tuple[int, int]]]] = []
    for token, where in holders.items():
        if 1 <= len(where) <= MAX_DF:
            identifiers.append((spelling.get(token, token), where))

    rare: list[tuple[str, set[tuple[int, int]]]] = []
    for idx, text in enumerate(folded):
        seen: set[str] = set()
        for word in WORD.findall(text):
            word = word.strip("_")
            if len(word) < 6 or word in seen:
                continue
            seen.add(word)
            if df.get(word, 0) == 1:
                rare.append((word, {where_of[idx]}))

    absent: list[tuple[str, set[tuple[int, int]]]] = [
        (word, set()) for word in ABSENT if df.get(word, 0) == 0
    ]

    def keyed(items):
        # Shortest query first: a one-word find is the hard case.
        return sorted(items, key=lambda pair: (len(pair[0]), pair[0]))[:limit]

    return {
        "identifier": keyed(identifiers),
        "rare": keyed(rare),
        "absent": absent,
    }


def measure(engine: ConversationSearch, query: str, messages, limit: int = 10):
    started = time.monotonic()
    rows, report = engine.search(query, messages, limit=limit)
    return rows, report, (time.monotonic() - started) * 1000.0


def run(paths: list[Path], scorer_mode: str, limit: int, as_json: bool) -> int:
    scorer = build_scorer(scorer_mode)
    engine = ConversationSearch(scorer)
    totals: dict[str, dict] = {}
    timings: list[float] = []
    failures: list[str] = []

    for path in paths:
        messages = load_messages(path)
        chunks = chunk_messages(messages)
        cases = build_cases(chunks, limit)
        for shape, items in cases.items():
            bucket = totals.setdefault(
                shape,
                {"queries": 0, "top1": 0, "top3": 0, "mrr": 0.0, "false_positive": 0},
            )
            for query, gold in items:
                rows, _report, elapsed = measure(engine, query, messages)
                timings.append(elapsed)
                bucket["queries"] += 1
                if shape == "absent":
                    if rows:
                        bucket["false_positive"] += 1
                        failures.append(f"absent query returned {len(rows)} rows: {query!r} ({path.name})")
                    continue
                found = [row for row in rows if (row.turn, row.char_start) in gold]
                if not found:
                    failures.append(f"{shape} query missed: {query!r} ({path.name})")
                    continue
                rank = min(found, key=lambda row: row.rank).rank
                bucket["mrr"] += 1.0 / rank
                if rank == 1:
                    bucket["top1"] += 1
                if rank <= 3:
                    bucket["top3"] += 1
                else:
                    failures.append(f"{shape} query ranked {rank}: {query!r} ({path.name})")

    summary = {}
    for shape, bucket in totals.items():
        n = bucket["queries"] or 1
        summary[shape] = {
            "queries": bucket["queries"],
            "recall@1": round(bucket["top1"] / n, 3),
            "recall@3": round(bucket["top3"] / n, 3),
            "mrr@10": round(bucket["mrr"] / n, 3),
            "false_positive": round(bucket["false_positive"] / n, 3) if shape == "absent" else None,
        }
    latency = {
        "p50_ms": round(statistics.median(timings), 1) if timings else 0.0,
        "p95_ms": round(sorted(timings)[max(0, int(len(timings) * 0.95) - 1)], 1) if timings else 0.0,
    }

    if as_json:
        print(json.dumps({"summary": summary, "latency": latency, "failures": failures}, indent=2))
    else:
        print(f"scorer={scorer_mode} chats={len(paths)} queries={len(timings)}")
        print(f"{'shape':<12}{'n':>4}{'r@1':>8}{'r@3':>8}{'mrr':>8}{'fp':>8}")
        for shape in ("identifier", "rare", "absent"):
            if shape not in summary:
                continue
            row = summary[shape]
            fp = "-" if row["false_positive"] is None else f"{row['false_positive']:.2f}"
            print(
                f"{shape:<12}{row['queries']:>4}{row['recall@1']:>8.2f}"
                f"{row['recall@3']:>8.2f}{row['mrr@10']:>8.2f}{fp:>8}"
            )
        print(f"latency p50={latency['p50_ms']}ms p95={latency['p95_ms']}ms")
        for line in failures[:20]:
            print(f"  miss: {line}")
        if len(failures) > 20:
            print(f"  ... {len(failures) - 20} more")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "eval_real").splitlines()[0])
    parser.add_argument("chats", nargs="+", type=Path)
    parser.add_argument("--scorer", default="mock", choices=("mock", "remote", "auto"))
    parser.add_argument("--limit", type=int, default=40, help="max queries per shape, per chat")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    missing = [path for path in args.chats if not path.exists()]
    if missing:
        for path in missing:
            print(f"missing chat file: {path}", file=sys.stderr)
        return 2
    return run(args.chats, args.scorer, args.limit, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
