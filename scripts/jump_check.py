"""Prove that a result row can jump to the turn it came from, without a browser.

Three mechanisms were tried against Open WebUI 0.12.4, and the first two were
dead ends measured on a real chat:

1. An HTML card in the action's embed iframe. That frame is sandboxed without
   ``allow-same-origin``, its origin is opaque, and the page accepts only three
   message types from it ("input:prompt", "input:prompt:submit",
   "action:submit"). It cannot reach the chat at all.
2. A Markdown link to ``#message-<id>``. The block did render, but Open WebUI's
   Markdown renderer hardcodes ``target="_blank"`` on every link it emits, so the
   click opened a second tab and the chat never moved.
3. A Markdown link to ``/c/<chat>?message=<id>``. Open WebUI's own click handler
   cancels the default navigation for ``/c/`` links and routes in place, and
   Chat.svelte watches the ``message`` query parameter reactively and calls
   ``scrollToMessage``. That is the mechanism its own search hits use.

The disclosure is an HTML ``<details>``/``<summary>`` pair for the same reason:
the shipped marked extension parses a block-level pair into a ``details`` token
and draws it with the Collapsible component, so it is the one tag that renders
instead of arriving as source.

This script asserts what makes that work, against the real Action class:

1. every row that carries both ids is a link to ``/c/<chat>?message=<id>``;
2. a row without a chat or message id is plain text, never a dead link;
3. both ids survive URL quoting and parse back unchanged;
4. the block is exactly one closed ``<details>`` with a ``<summary>`` first line,
   and carries no other HTML tag;
5. pressing the button twice replaces the block instead of stacking it, and the
   text the user wrote above the marker is never touched.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import types
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "integrations" / "openwebui"))

LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)", re.S)
CHAT = "3f2a-chat"
TAGS = ("<a ", "<li>", "<br>", "<div", "<p>", "<ol", "<ul", "<script", "<span")


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


def _check_block(block: str, ids: list, problems: list) -> int:
    """Shape of the block, and the hrefs it draws. Returns the anchor count."""
    if not block.startswith("<details>\n<summary>Trick · "):
        problems.append(f"block does not open with a details/summary pair: {block[:60]!r}")
    if not block.endswith("</details>"):
        problems.append("block is not closed")
    if block.count("<details>") != 1 or block.count("</details>") != 1:
        problems.append("block must hold exactly one details element")
    for tag in TAGS:
        if tag in block:
            problems.append(f"block carries HTML it cannot render: {tag!r}")

    links = LINK.findall(block)
    hrefs = [href for _, href in links]
    if len(hrefs) != len(ids):
        problems.append(f"expected {len(ids)} links, found {len(hrefs)}")
    for href, mid in zip(hrefs, ids):
        parsed = urllib.parse.urlsplit(href)
        if parsed.path != f"/c/{CHAT}":
            problems.append(f"link does not route into this chat: {href!r}")
            continue
        # A fragment would open a second tab (target="_blank" is hardcoded), so
        # the message must travel as a query parameter, not as an anchor.
        if parsed.fragment:
            problems.append(f"link uses a fragment, which opens a new tab: {href!r}")
        carried = urllib.parse.parse_qs(parsed.query).get("message", [""])[0]
        if carried != mid:
            problems.append(f"message id {mid!r} round-trips to {carried!r}")
    return len(hrefs)


def main() -> int:
    module = _module()
    action = module.Action()
    problems: list[str] = []

    ids = ["3f2a-0001", "3f2a-0002"]
    rows = [_row(ids[0], 1), _row(ids[1], 14, "assistant"), _row("", 26)]
    block = action._results_markdown("i5 memory problem", rows, chat_id=CHAT, transcript=True)
    anchors = _check_block(block, ids, problems)

    # A row without an id must stay text: a dead link promises a jump that
    # cannot happen, which is worse than no link at all.
    if "turn 26" not in block:
        problems.append("the row without an id disappeared instead of printing as text")
    if "(/c/" in block.split("turn 26", 1)[1]:
        problems.append("the row without an id was drawn as a link")

    # Without a chat id there is no route to scroll inside.
    chatless = action._results_markdown("i5", [_row(ids[0], 1)], transcript=True)
    if "](/c/" in chatless:
        problems.append("a block without a chat id drew a link into the chat route")
    if "no jump target" not in chatless:
        problems.append("a row with no reachable target did not say so")

    # The block lands on an assistant message, never on the user's own question:
    # a user message is drawn as plain text when Open WebUI's
    # renderMarkdownInUserMessages is off. A second press replaces it.
    branch = [
        {"id": "u-1", "role": "user", "content": "my own question"},
        {"id": ids[1], "role": "assistant", "content": "the answer"},
    ]
    first = module.Action._attach(branch, block, "u-1")
    if not first:
        problems.append("_attach returned nothing for a chat that has an answer")
    else:
        target = first["messages"][0]
        if target["id"] != ids[1]:
            problems.append("_attach wrote into the user's question instead of the answer")
        content = target["content"]
        if not content.startswith("the answer"):
            problems.append("_attach dropped the text of the answer")
        if content.count(module.MARKER) != 1:
            problems.append("the marker does not appear exactly once")
        second = module.Action._attach(
            [
                {"id": "u-1", "role": "user", "content": "my own question"},
                {"id": ids[1], "role": "assistant", "content": content},
            ],
            block,
            "u-1",
        )["messages"][0]["content"]
        if second.count(module.MARKER) != 1:
            problems.append("a second press stacked the block instead of replacing it")
        if second.count("<details") != 1:
            problems.append("a second press left two blocks in the message")
        if "the answer" not in html.unescape(second):
            problems.append("a second press lost the text of the answer")

    if module.Action._attach([{"id": "u-1", "role": "user", "content": "q"}], block, "u-1"):
        problems.append("_attach wrote a block into a chat that has no answer")

    notes = _frontend_notes(problems)

    if problems:
        print("jump check failed:", file=sys.stderr)
        for item in problems:
            print("  " + item, file=sys.stderr)
        return 1

    print(
        f"jump ok: {anchors} rows link to /c/<chat>?message=<id>, no dead links, "
        f"one closed disclosure, block replaced on re-press{notes}"
    )
    return 0


_CONTAINER_PROBE = r"""
import glob, json, sys
wanted = {
    "marked/extension.ts": "<summary>",
    "MarkdownInlineTokens.svelte": 'target="_blank"',
    "Chat.svelte": "searchParams.get('message')",
}
found = {name: None for name in wanted}
for path in glob.glob("/app/build/_app/immutable/chunks/*.map"):
    try:
        data = json.load(open(path))
    except Exception:
        continue
    for source, content in zip(data.get("sources", []), data.get("sourcesContent") or []):
        if not content:
            continue
        for name, needle in wanted.items():
            if found[name] is None and source.endswith(name) and needle in content:
                found[name] = path
    if all(value is not None for value in found.values()):
        break
print(json.dumps(found))
"""


def _frontend_notes(problems: list) -> str:
    """Confirm the frontend still holds the three facts this design rests on.

    Skipped with a note when the container is not reachable, so the gate stays
    usable on a machine that does not run Open WebUI.
    """
    try:
        probe = subprocess.run(
            ["docker", "exec", "-i", "open-webui", "python3", "-"],
            input=_CONTAINER_PROBE,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return " (frontend not checked: no container access here)"
    if probe.returncode != 0 or not probe.stdout.strip():
        return " (frontend not checked: open-webui container not available)"
    try:
        found = json.loads(probe.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return " (frontend not checked: unreadable probe output)"

    missing = [name for name, value in found.items() if value is None]
    if missing:
        # Each of these is a load-bearing assumption, not a detail: without the
        # details token the block shows as source, without the hardcoded target
        # the fragment link would have worked, without the reactive read the
        # query parameter would not scroll.
        problems.append("frontend no longer holds the jump assumptions: " + ", ".join(missing))
        return ""
    return " (frontend: details token, hardcoded target, reactive message parameter all present)"


if __name__ == "__main__":
    raise SystemExit(main())
