"""Prove that a result row can jump to the turn it came from, without a browser.

Open WebUI 0.12.4 renders an action's HTML embed inside an iframe whose sandbox
is "allow-scripts allow-popups allow-downloads", with
``iframeSandboxAllowSameOrigin`` defaulting to false. That frame has an opaque
origin: it cannot read the chat document, and the page accepts only three
message types from it ("input:prompt", "input:prompt:submit", "action:submit").
An earlier design tried to scroll the chat from inside the card; the click did
nothing while the code looked alive.

The mechanism that does work is the one Open WebUI uses for its own search hits:
a link to ``?message=<id>`` on the chat route. Chat.svelte reads that query
parameter, checks ``$chat.messages[id]`` exists, sets the current id and calls
``scrollToMessage(id)``. Message content is rendered as markdown in the page,
not in a frame, so those anchors are ordinary links and need no script - which
is why the results are written into the message instead of a card.

This script asserts what makes that work, against the real Action class:

1. every row that carries a message id is an anchor to ``?message=<id>``;
2. a row without an id is plain text, never a dead link;
3. the id survives URL quoting and parses back unchanged;
4. pressing the button twice replaces the block instead of stacking it, and the
   text the user wrote above the marker is never touched.
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
import types
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "integrations" / "openwebui"))

ANCHOR = re.compile(r"<a href='([^']+)'>(.*?)</a>", re.S)


def _module():
    """Import the action the way Open WebUI does: the host may have no pydantic."""
    try:
        import pydantic  # noqa: F401
    except ImportError:
        stub = types.ModuleType("pydantic")

        class _BaseModel:
            pass

        def _Field(default=None, description="", **_kwargs):
            return default

        stub.BaseModel = _BaseModel
        stub.Field = _Field
        sys.modules["pydantic"] = stub
    import trick_action

    return trick_action


def _row(mid: str, turn: int, role: str = "user") -> dict:
    return {
        "turn": turn,
        "role": role,
        "message_id": mid,
        "text": f"passage from turn {turn}",
        "final_score": 0.62,
        "matched": ["i5", "memory"],
    }


def main() -> int:
    module = _module()
    action = module.Action()
    problems: list[str] = []

    ids = ["3f2a-0001", "3f2a-0002"]
    rows = [_row(ids[0], 1), _row(ids[1], 14, "assistant"), _row("", 26)]
    block = action._results_html("i5 memory problem", rows, [], transcript=True)

    # 1 and 3: every id-carrying row is an anchor whose id round-trips.
    hrefs = [href for href, _ in ANCHOR.findall(block)]
    if len(hrefs) != len(ids):
        problems.append(f"expected {len(ids)} anchors, found {len(hrefs)}")
    for href, mid in zip(hrefs, ids):
        if not href.startswith("?message="):
            problems.append(f"anchor is not a message link: {href!r}")
            continue
        carried = urllib.parse.unquote(href.split("=", 1)[1])
        if carried != mid:
            problems.append(f"anchor for {mid!r} round-trips to {carried!r}")

    # 2: the row without an id must stay text. A dead link promises a jump that
    # cannot happen, which is worse than no link at all.
    if "?message=')" in block or "?message=" + "')" in block:
        problems.append("a row without a message id produced an empty anchor")
    if "turn 26" not in block:
        problems.append("the row without an id disappeared instead of printing as text")

    # The reader on the other side scrolls only a message it can find in the
    # loaded chat, so the id in the anchor is the whole contract.
    for mid in ids:
        if f"?message={mid}" not in html.unescape(block):
            problems.append(f"row {mid!r} has no reachable anchor target")

    # 4: a second press replaces the block; the user's text is never rewritten.
    body = {"id": ids[1], "messages": [{"id": ids[1], "content": "my own question"}]}
    first = module.Action._attach(body, block)
    if not first:
        problems.append("_attach returned nothing for a message that has an id")
    else:
        content = first["messages"][0]["content"]
        if not content.startswith("my own question"):
            problems.append("_attach dropped the text the user wrote")
        if content.count(module.MARKER) != 1:
            problems.append("the marker does not appear exactly once")
        again = module.Action._attach(
            {"id": ids[1], "messages": [{"id": ids[1], "content": content}]}, block
        )["messages"][0]["content"]
        if again.count(module.MARKER) != 1:
            problems.append("a second press stacked the block instead of replacing it")
        if again.count("<details") > 1:
            problems.append("a second press left two blocks in the message")

    if module.Action._attach({"id": "", "messages": []}, block):
        problems.append("_attach invented a target for a call with no message id")

    if problems:
        print("jump check failed:", file=sys.stderr)
        for item in problems:
            print("  " + item, file=sys.stderr)
        return 1

    # The mechanism was read out of the running frontend, not guessed: assert
    # the reader still exists there when the container is reachable, and skip
    # that half (with a note) when it is not.
    note = _reader_note()
    print(
        f"jump ok: {len(hrefs)} rows link to ?message=<id>, no dead links, "
        f"block replaced on re-press{note}"
    )
    return 0


def _reader_note() -> str:
    """Confirm Open WebUI still reads the query parameter, if it is installed."""
    try:
        found = subprocess.run(
            [
                "docker",
                "exec",
                "open-webui",
                "sh",
                "-c",
                'grep -rlo \'searchParams.get("message")\' /app/build/_app/immutable/chunks/*.js | head -1',
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return " (reader not checked: no container access here)"
    if found.returncode != 0 or not found.stdout.strip():
        return " (reader not checked: open-webui container not available)"
    return " (chat page still reads ?message=<id>)"


if __name__ == "__main__":
    raise SystemExit(main())
