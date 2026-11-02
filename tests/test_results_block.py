"""Two defects measured on a real chat, locked down here.

1. The results block became *evidence*. It names the query and the turns it
   found, so the next search matched its own previous output and ranked a turn
   because that turn carried the block - "turn 45 · assistant" scored 7.25 on
   the query "mistake, made" purely because the block said "matched: mistake,
   made" on top of an unrelated answer.

2. The block landed on a **user** message. Open WebUI renders an assistant
   message through its Markdown renderer, but a user message is drawn as plain
   text when ``renderMarkdownInUserMessages`` is off - which is why the block
   arrived on screen as literal ``**bold**`` and why its links were not links.

Both are properties of the pipeline, not of one call site, so both are tested
where they live: the index for the first, the action for the second.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "integrations" / "openwebui" / "trick_action.py"
)

BLOCK = (
    "**Trick** · mistake, made · 2 passages match\n"
    "\n"
    "1. [turn 45 · assistant](#message-m-45) · matched: mistake, made · 7.25\n"
    "   > the answer that mentions the mistake\n"
    "\n"
    "_8 catalogue sources matched outside this conversation and are hidden "
    "(SOURCES=never)._"
)


def _install_pydantic_stub() -> None:
    if "pydantic" in sys.modules:
        return
    module = types.ModuleType("pydantic")

    class BaseModel:
        pass

    def Field(default=None, description="", **_kwargs):
        return default

    module.BaseModel = BaseModel
    module.Field = Field
    sys.modules["pydantic"] = module


def load_module():
    _install_pydantic_stub()
    name = f"trick_action_attach_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_block_is_not_indexed_as_a_passage():
    from trick.transcript import chunk_messages, normalise_messages

    messages = normalise_messages(
        [
            {
                "id": "m-45",
                "role": "assistant",
                "content": "We settled the gate at 0.15 after the second run.\n\n" + BLOCK,
            }
        ]
    )
    assert "matched:" not in messages[0].content
    assert messages[0].content.startswith("We settled the gate")
    chunks = chunk_messages(messages)
    text = " ".join(chunk.text for chunk in chunks)
    assert "matched:" not in text
    assert "catalogue sources" not in text
    # The real content is untouched.
    assert "gate at 0.15" in text


def test_a_message_that_only_mentions_the_marker_is_kept():
    """A user may type the marker; only a block-shaped tail is cut."""
    from trick.transcript import normalise_messages

    typed = "**Trick** · is this tool any good at finding things?"
    messages = normalise_messages([{"id": "u-1", "role": "user", "content": typed}])
    assert messages[0].content == typed


def test_a_legacy_html_block_is_also_cut():
    from trick.transcript import normalise_messages

    legacy = (
        "Real answer.\n\n"
        "<!-- trick:results --> <details><summary>3 passages match</summary>"
        "<ol><li>turn 7 · matched: mistake</li></ol></details>"
    )
    messages = normalise_messages([{"id": "m-1", "role": "assistant", "content": legacy}])
    assert messages[0].content == "Real answer."


def test_the_block_never_lands_on_a_user_message():
    """Pressing the button on your own question must not write there."""
    module = load_module()
    messages = [
        {"id": "u-1", "role": "user", "content": "where did we lose the most time?"},
        {"id": "a-2", "role": "assistant", "content": "The i5 build ran out of memory."},
        {"id": "u-3", "role": "user", "content": "and then?"},
    ]
    attached = module.Action._attach(messages, BLOCK, "u-3")
    assert attached["messages"][0]["id"] == "a-2"
    content = attached["messages"][0]["content"]
    assert content.startswith("The i5 build ran out of memory.")
    assert content.endswith(BLOCK)
    for item in messages:
        if item["role"] == "user":
            assert BLOCK not in item["content"]


def test_pressing_the_button_on_an_answer_writes_into_that_answer():
    module = load_module()
    messages = [
        {"id": "u-1", "role": "user", "content": "question"},
        {"id": "a-2", "role": "assistant", "content": "the answer"},
    ]
    attached = module.Action._attach(messages, BLOCK, "a-2")
    assert attached["messages"][0]["id"] == "a-2"


def test_a_second_search_replaces_the_block_instead_of_stacking_it():
    module = load_module()
    messages = [
        {"id": "u-1", "role": "user", "content": "question"},
        {"id": "a-2", "role": "assistant", "content": "the answer\n\n" + BLOCK},
    ]
    attached = module.Action._attach(messages, BLOCK, "u-1")
    content = attached["messages"][0]["content"]
    assert content.count("**Trick** · ") == 1


def test_a_chat_without_an_answer_gets_no_block_at_all():
    module = load_module()
    messages = [{"id": "u-1", "role": "user", "content": "only a question so far"}]
    assert module.Action._attach(messages, BLOCK, "u-1") == {}


def test_rows_point_at_the_anchor_the_chat_page_renders():
    """The id on a message element is ``message-<id>``; a row must use it."""
    module = load_module()
    row = {
        "turn": 7,
        "role": "assistant",
        "message_id": "m-7",
        "matched": ["gate"],
        "final_score": 1.0,
        "coverage": 1.0,
        "snippet": "the gate was 0.15",
    }
    action = module.Action()
    action.valves = action.Valves()
    block = action._row_markdown(1, row)
    joined = "\n".join(block)
    assert "(#message-m-7)" in joined
    assert "?message=" not in joined


def test_stored_messages_gain_their_id_from_the_key():
    """A stored message is keyed by id and may not repeat it inside the object."""
    module = load_module()
    history = {
        "messages": {
            "a": {"parentId": None, "role": "user", "content": "q", "childrenIds": ["b"]},
            "b": {"parentId": "a", "role": "assistant", "content": "a", "childrenIds": []},
        },
        "currentId": "b",
    }
    ordered = module.Action._order({"history": history}, "b")
    assert [item["id"] for item in ordered] == ["a", "b"]
