"""A search result must be able to point back at the turn it came from.

Open WebUI scrolls to ``#message-<chat>-<message>``, so the message id has to
survive every hop between the chat and the card: the action's transcript, the
service's untrusted-input validation (which rebuilds each message dict), the
chunker, the ranked row, and the JSON that crosses the socket and the HTTP
bridge. It was lost in that validation step, and the card then looked correct
while every row was unclickable - a silent failure, so it is locked here.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

from trick.search import SearchOutcome
from trick.service import validate_messages_param
from trick.transcript import ChatRow, chunk_messages, normalise_messages

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "integrations" / "openwebui" / "trick_action.py"


def test_transport_validation_keeps_the_message_id():
    out = validate_messages_param([{"id": "a-2", "role": "assistant", "content": "yes"}])

    assert out == [{"role": "assistant", "content": "yes", "id": "a-2"}]


def test_transport_validation_drops_an_unusable_id_instead_of_rejecting_it():
    """A chat without ids is still searchable; the row is simply not clickable."""
    out = validate_messages_param(
        [
            {"id": 17, "role": "user", "content": "one"},
            {"id": "x" * 500, "role": "user", "content": "two"},
            {"id": "", "role": "user", "content": "three"},
        ]
    )

    assert out == [
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
        {"role": "user", "content": "three"},
    ]


def test_the_id_reaches_every_chunk_of_its_message():
    messages = normalise_messages(
        [
            {"id": "u-1", "role": "user", "content": "short question"},
            {"id": "a-2", "role": "assistant", "content": "Long answer. " * 120},
        ]
    )

    chunks = chunk_messages(messages)

    assert {chunk.mid for chunk in chunks} == {"u-1", "a-2"}
    assert max(chunk.mid for chunk in chunks if chunk.turn == 2) == "a-2"


def test_the_row_keeps_the_id_across_json():
    row = ChatRow(
        rank=1,
        turn=3,
        role="assistant",
        text="we settled on 0.15",
        char_start=0,
        char_end=18,
        retrieval_score=0.5,
        laya_probability=None,
        final_score=0.5,
        scored_by_laya=False,
        mid="a-2",
    )

    payload = row.as_dict()
    assert payload["message_id"] == "a-2"
    assert ChatRow.from_dict(1, payload).mid == "a-2"


def test_an_outcome_survives_the_process_boundary_with_its_ids():
    row = ChatRow(
        rank=1,
        turn=1,
        role="user",
        text="where did we put the taper",
        char_start=0,
        char_end=26,
        retrieval_score=0.4,
        laya_probability=0.8,
        final_score=0.48,
        scored_by_laya=True,
        mid="u-1",
    )
    outcome = SearchOutcome(
        query="taper",
        scope="both",
        degraded=[],
        report={},
        chat_report={},
        chat=[row],
        rows=[],
    )

    rebuilt = SearchOutcome.from_dict(outcome.as_dict())

    assert [item.mid for item in rebuilt.chat] == ["u-1"]
    assert rebuilt.as_dict()["chat"][0]["message_id"] == "u-1"


def _load_action_module():
    # The host venv has no pydantic; the file only uses BaseModel and Field, so a
    # two-name stub is enough to import it (same approach as the action tests).
    if "pydantic" not in sys.modules:
        stub = types.ModuleType("pydantic")

        class BaseModel:
            pass

        def Field(default=None, description="", **_kwargs):
            return default

        stub.BaseModel = BaseModel
        stub.Field = Field
        sys.modules["pydantic"] = stub
    spec = importlib.util.spec_from_file_location("trick_action_ids", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_action_sends_the_ids_and_renders_them_as_jump_targets():
    module = _load_action_module()
    action = module.Action()
    transcript, dropped = action._transcript(
        [
            {"id": "u-1", "role": "user", "content": "where did we put the taper"},
            {"id": "a-2", "role": "assistant", "content": "at 0.15"},
            {"role": "user", "content": "no id on this one"},
        ]
    )

    assert dropped == 0
    assert transcript[0]["id"] == "u-1"
    assert "id" not in transcript[2]

    block = action._chat_block(
        [{"turn": 1, "role": "user", "text": "where did we put the taper", "message_id": "u-1"}],
        0,
        "chat-9",
    )

    assert "data-mid='u-1'" in block
    assert "data-cid='chat-9'" in block
    assert "class='hit'" in block
