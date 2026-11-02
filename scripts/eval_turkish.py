"""Measure Turkish retrieval on a real conversation.

The unit tests pin the behaviour; this harness measures the quality, on your own
chats, so a change that looks harmless cannot quietly undo it.

Usage:

    python scripts/eval_turkish.py chat.json

The input is either an Open WebUI export (``{"history": {"messages": {...}}}``,
``{"messages": [...]}``) or a plain ``{"messages": [...]}`` list. Every gold
passage is a chunk holding a distinctive word; that word is then searched back in
three shapes:

  exact  the word as the chat spells it
  ascii  the same word typed without Turkish letters (i for the dotless i)
  stem   the first five letters, standing in for a different inflection

Reported as recall@3: the share of gold passages that come back in the top three.
Measured on three real chats (36 gold passages) before the fold existed:
exact 0.972, ascii 0.000, stem 0.250. With the fold and the stem fallback:
exact 0.972, ascii 0.972, stem 0.722.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trick.laya import Ranked  # noqa: E402
from trick.transcript import (  # noqa: E402
    ConversationSearch,
    chunk_messages,
    fold_tr,
    normalise_messages,
)

TR_CHARS = set("\u0131\u015f\u00e7\u011f\u00fc\u00f6\u0130\u015e\u00c7\u011e\u00dc\u00d6")
ASCII = str.maketrans(
    {
        "\u0131": "i",
        "\u015f": "s",
        "\u00e7": "c",
        "\u011f": "g",
        "\u00fc": "u",
        "\u00f6": "o",
        "\u0130": "I",
        "\u015e": "S",
        "\u00c7": "C",
        "\u011e": "G",
        "\u00dc": "U",
        "\u00d6": "O",
    }
)


class Uniform:
    """Uniform probabilities: the ranking stays the retrieval pipeline's own."""

    name = "uniform"

    def rank(self, query, candidates, instructions=""):
        return [Ranked(index=index, probability=0.5) for index in range(len(candidates))]


def messages_of(payload: object) -> list:
    """Accept the shapes an export actually has."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise SystemExit("unsupported input: expected an object or a list")
    history = payload.get("history")
    if isinstance(history, dict) and isinstance(history.get("messages"), dict):
        return [
            {"id": mid, "role": msg.get("role"), "content": msg.get("content"),
             "childrenIds": msg.get("childrenIds") or []}
            for mid, msg in history["messages"].items()
            if isinstance(msg, dict)
        ]
    if isinstance(payload.get("messages"), dict):
        return [
            {"id": mid, "role": msg.get("role"), "content": msg.get("content"),
             "childrenIds": msg.get("childrenIds") or []}
            for mid, msg in payload["messages"].items()
            if isinstance(msg, dict)
        ]
    if isinstance(payload.get("messages"), list):
        return payload["messages"]
    raise SystemExit("no messages in the input")


def gold_pairs(chunks, limit: int = 20):
    """Distinctive words that contain a Turkish letter, at most two passages.

    The words are taken from the text as written (that is where the Turkish
    letters are) and then folded, because the search side is folded too.
    """
    df: dict[str, int] = {}
    for chunk in chunks:
        for word in set(re.findall(r"[^\W\d_]+", fold_tr(chunk.text))):
            df[word] = df.get(word, 0) + 1
    pairs, seen = [], set()
    for chunk in chunks:
        for word in re.findall(r"[^\W\d_]+", chunk.text):
            folded = fold_tr(word)
            if len(folded) < 7 or folded in seen or df.get(folded, 99) > 2:
                continue
            if not any(char in TR_CHARS for char in word):
                continue
            seen.add(folded)
            pairs.append((folded, chunk))
            if len(pairs) >= limit:
                return pairs
    return pairs


def run(pairs, engine, messages, label: str, shape) -> tuple[int, int]:
    hits = total = 0
    for word, gold in pairs:
        query = shape(word)
        rows, _report = engine.search(query, messages, limit=8)
        total += 1
        if any(
            row.turn == gold.turn and abs(row.char_start - gold.char_start) < 1
            for row in rows[:3]
        ):
            hits += 1
    print(f"  {label:6} recall@3 = {hits}/{total} = {hits / max(1, total):.3f}")
    return hits, total


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    payloads = []
    for path in argv[1:]:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for item in raw if isinstance(raw, list) else [raw]:
            payloads.append(messages_of(item))

    engine = ConversationSearch(Uniform(), gate_margin=0.0)
    totals = {"exact": [0, 0], "ascii": [0, 0], "stem": [0, 0]}
    for index, items in enumerate(payloads, start=1):
        messages = normalise_messages(items)
        chunks = chunk_messages(messages)
        pairs = gold_pairs(chunks)
        if len(pairs) < 5:
            print(f"chat {index}: {len(items)} messages, too little text to measure")
            continue
        print(f"chat {index}: {len(items)} messages, {len(chunks)} passages, {len(pairs)} gold")
        for label, shape in (
            ("exact", lambda word: word),
            ("ascii", lambda word: word.translate(ASCII)),
            ("stem", lambda word: word[:5]),
        ):
            hits, total = run(pairs, engine, messages, label, shape)
            totals[label][0] += hits
            totals[label][1] += total

    print("totals")
    failed = False
    for label, (hits, total) in totals.items():
        share = hits / max(1, total)
        print(f"  {label:6} {hits}/{total} = {share:.3f}")
        # The floor the fold has to hold: an ASCII-typed query is as good as an
        # exact one, and a five-letter stem finds most of its inflections.
        floor = {"exact": 0.90, "ascii": 0.90, "stem": 0.65}[label]
        if total and share < floor:
            print(f"    below the measured floor {floor:.2f}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
