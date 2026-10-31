"""The Open WebUI action must survive the way Open WebUI actually calls it.

Open WebUI 0.12.4 dispatches an action as ``action(**params)``, where ``params``
is ``{"body": ...}`` plus only those reserved arguments whose exact names appear
in the signature (``open_webui/utils/actions.py``). A signature that also asks
for ``user_message``/``messages`` therefore raises before the first line of our
code runs - which is exactly the failure this file locks down.

The dispatch below reproduces that filter, so a signature change that needs more
than ``body`` fails here instead of in the user's chat window.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import types
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "integrations" / "openwebui" / "trick_action.py"


def _install_pydantic_stub() -> None:
    """The host venv has no pydantic; the file only uses BaseModel and Field."""
    if "pydantic" in sys.modules:
        return
    module = types.ModuleType("pydantic")

    class BaseModel:  # noqa: D401 - stub
        pass

    def Field(default=None, description="", **_kwargs):  # noqa: N802 - pydantic name
        return default

    module.BaseModel = BaseModel
    module.Field = Field
    sys.modules["pydantic"] = module


RESULT = {
    "ok": True,
    "result": {
        "rows": [
            {
                "rank": 1,
                "title": "DESeq2",
                "url": "https://bioconductor.org/packages/DESeq2",
                "source": "bioconductor",
                "key": "DESeq2",
                "final_score": 0.912,
                "score_label": "laya",
                "summary": "Differential expression for RNA-seq counts",
            }
        ],
        "chat": [
            {"turn": 3, "role": "assistant", "text": "we settled on 0.15", "scored_by_laya": False}
        ],
        "degraded": [],
        "report": {"weights": [0.2, 0.8], "blend_mode": "fused"},
    },
}


def load_module():
    _install_pydantic_stub()
    spec = importlib.util.spec_from_file_location("trick_action_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def dispatch_like_open_webui(module, body, **reserved):
    """Reproduce Open WebUI's parameter filter for an action."""
    sig = inspect.signature(module.Action.action)
    params = {"body": body}
    for name, value in reserved.items():
        if name in sig.parameters:
            params[name] = value
    return module.Action().action(**params)


class Recorder:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def __call__(self, payload):
        self.calls.append(payload)
        return self.answer


def html_of(response) -> str:
    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        return body.decode("utf-8")
    return str(body)


def test_body_alone_is_enough():
    """The regression that broke a real chat: three extra required arguments."""
    module = load_module()
    action = module.Action()
    sig = inspect.signature(action.action)
    required = [
        name
        for name, param in sig.parameters.items()
        if name != "self" and param.default is inspect.Parameter.empty
    ]
    assert required == ["body"], required


def test_dispatches_with_body_only(monkeypatch):
    module = load_module()
    posted: list[dict] = []
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: posted.append(payload) or RESULT)

    body = {
        "id": "m1",
        "model": "gpt",
        "messages": [
            {"role": "user", "content": "differential expression for RNA-seq counts"},
            {"role": "assistant", "content": "we settled on 0.15"},
        ],
    }
    response = asyncio.run(dispatch_like_open_webui(module, body))

    assert posted, "the action never reached the bridge"
    assert posted[0]["query"] == "differential expression for RNA-seq counts"
    assert posted[0]["scope"] == "both"
    assert posted[0]["messages"][0]["role"] == "user"
    card = html_of(response)
    # Default ('always'): the passage and the sources are both listed. Which of
    # the two matters is the reader's call, so the card does not hide one.
    assert "we settled on 0.15" in card
    assert "turn 3" in card
    assert "DESeq2" in card
    assert "0.20 x laya probability + 0.80 x retrieval" in card
    assert response.headers.get("Content-Disposition") == "inline"


def test_auto_quiet_mode_hides_sources_once_the_chat_answered(monkeypatch):
    """'auto' is the optional mode, not the default: it must stay reachable."""
    module = load_module()
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: RESULT)
    original = module.Action.__init__

    def init(self):
        original(self)
        self.valves.SOURCES = "auto"

    monkeypatch.setattr(module.Action, "__init__", init)
    body = {"id": "m1", "messages": [{"role": "user", "content": "differential expression"}]}

    card = html_of(asyncio.run(dispatch_like_open_webui(module, body)))

    assert "we settled on 0.15" in card
    assert "DESeq2" not in card
    assert "catalogue sources matched too" in card


def test_never_quiet_mode_hides_sources_without_nagging(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: RESULT)
    original = module.Action.__init__

    def init(self):
        original(self)
        self.valves.SOURCES = "never"

    monkeypatch.setattr(module.Action, "__init__", init)
    body = {"id": "m1", "messages": [{"role": "user", "content": "differential expression"}]}

    card = html_of(asyncio.run(dispatch_like_open_webui(module, body)))

    assert "DESeq2" not in card
    assert "catalogue sources matched too" not in card


def test_score_note_omits_numbers_when_the_answer_has_no_weights():
    """An older bridge must not make the card print an invented formula."""
    module = load_module()
    note = module.Action._score_note({})
    assert "0.20" not in note
    assert "not a confidence" in note


def test_rows_are_listed_when_the_valve_asks_for_them(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: RESULT)
    original = module.Action.__init__

    def init(self):
        original(self)
        self.valves.SOURCES = "always"

    monkeypatch.setattr(module.Action, "__init__", init)
    body = {"id": "m1", "messages": [{"role": "user", "content": "differential expression"}]}

    card = html_of(asyncio.run(dispatch_like_open_webui(module, body)))

    assert "DESeq2" in card
    assert "we settled on 0.15" in card


def test_sources_are_shown_when_the_conversation_has_no_match(monkeypatch):
    module = load_module()
    empty_chat = {"ok": True, "result": {**RESULT["result"], "chat": []}}
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: empty_chat)
    body = {"id": "m1", "messages": [{"role": "user", "content": "differential expression"}]}

    card = html_of(asyncio.run(dispatch_like_open_webui(module, body)))

    assert "DESeq2" in card
    assert "no passage in this conversation contains" in card.lower()


def test_input_dialog_is_used_when_available(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: RESULT)
    event_call = Recorder("the threshold we settled on")
    body = {"id": "m1", "messages": [{"role": "user", "content": "something else entirely"}]}

    asyncio.run(dispatch_like_open_webui(module, body, __event_call__=event_call))

    assert event_call.calls, "no input dialog was requested"
    request = event_call.calls[0]
    assert request["type"] == "input"
    assert "find" in request["data"]["title"].lower()
    assert "placeholder" in request["data"]


def test_input_dialog_answer_is_the_query(monkeypatch):
    module = load_module()
    posted: list[dict] = []
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: posted.append(payload) or RESULT)
    body = {"id": "m1", "messages": [{"role": "user", "content": "unrelated"}]}

    asyncio.run(dispatch_like_open_webui(module, body, __event_call__=Recorder("  the   taper  ")))

    assert posted[0]["query"] == "the taper"


def test_dialog_that_cannot_open_falls_back(monkeypatch):
    """A closed tab returns {'error': ...}; that must not raise or lose the search."""
    module = load_module()
    posted: list[dict] = []
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: posted.append(payload) or RESULT)
    body = {"id": "m1", "messages": [{"role": "user", "content": "shrinkage estimator"}]}

    asyncio.run(dispatch_like_open_webui(module, body, __event_call__=Recorder({"error": "timeout"})))

    assert posted[0]["query"] == "shrinkage estimator"


def test_dialog_cancelled_reports_nothing_found(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: pytest.fail("must not search"))
    body = {"id": "m1", "messages": [{"role": "user", "content": "shrinkage estimator"}]}

    card = html_of(asyncio.run(dispatch_like_open_webui(module, body, __event_call__=Recorder(None))))

    assert "No question was entered" in card


def test_dialog_error_without_any_user_message_does_not_search(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: pytest.fail("must not search"))
    body = {"id": "m1", "messages": [{"role": "assistant", "content": "hello"}]}

    card = html_of(asyncio.run(dispatch_like_open_webui(module, body, __event_call__=Recorder({"error": "x"}))))

    assert "No question was entered" in card


def test_chat_text_is_escaped(monkeypatch):
    module = load_module()
    payload = json.loads(json.dumps(RESULT))
    payload["result"]["chat"][0]["text"] = "<script>alert(1)</script>"
    monkeypatch.setattr(module.Action, "_post", lambda self, p: payload)
    body = {"id": "m1", "messages": [{"role": "user", "content": "x"}]}

    card = html_of(asyncio.run(dispatch_like_open_webui(module, body)))

    # The only script in the card is the fixed one in SCRIPT; reader text and
    # result text must arrive escaped, never as markup.
    assert card.count("<script>") == module.SCRIPT.count("<script>") == 1
    assert "<script>alert(1)" not in card
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in card


def test_bridge_failure_explains_the_fix(monkeypatch):
    module = load_module()
    monkeypatch.setattr(
        module.Action,
        "_post",
        lambda self, p: {"ok": False, "status": 401, "error": {"s_code": "S9", "message": "unauthorized"}},
    )
    body = {"id": "m1", "messages": [{"role": "user", "content": "x"}]}

    card = html_of(asyncio.run(dispatch_like_open_webui(module, body)))

    assert "TRICK_TOKEN" in card


def test_transcript_window_reports_what_was_dropped(monkeypatch):
    module = load_module()
    posted: list[dict] = []
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: posted.append(payload) or RESULT)
    body = {
        "id": "m1",
        "messages": [{"role": "user", "content": f"turn {index}"} for index in range(10)],
    }
    action = module.Action()
    action.valves.CHAT_TURNS = 4
    asyncio.run(action.action(body, __event_call__=Recorder("q")))

    assert len(posted[0]["messages"]) == 4
    assert posted[0]["messages"][-1]["content"] == "turn 9"


def test_is_an_action_not_a_model():
    """The tool is a find, so it must not be selectable as a chat model."""
    module = load_module()
    assert not hasattr(module, "Pipe"), "an Action file must not define Pipe"
    assert hasattr(module.Action, "action")
    assert not hasattr(module.Action, "pipes")


# -- the conversation really has to come from the chat --------------------
#
# Open WebUI 0.12.4 sends the message object as ``body``: ``id``, ``chat_id``,
# ``model`` - and no ``messages`` array. A tool that reads only ``body["messages"]``
# answers with catalogue hits and nothing from the chat, which is the bug a
# reader hit: "en buyuk hata nerede yapildi" returned Crossref rows.


def test_chat_id_without_messages_reads_the_stored_chat(monkeypatch):
    module = load_module()
    posted: list[dict] = []
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: posted.append(payload) or RESULT)

    async def fake_storage(self, chat_id, message_id):
        assert chat_id == "c-1"
        assert message_id == "m-9"
        return [
            {"role": "user", "content": "the taper looked wrong"},
            {"role": "assistant", "content": "the biggest mistake was the threshold"},
        ]

    monkeypatch.setattr(module.Action, "_from_storage", fake_storage)
    body = {"id": "m-9", "chat_id": "c-1", "session_id": "s", "model": "llama"}

    card = html_of(asyncio.run(dispatch_like_open_webui(module, body)))

    assert posted[0]["scope"] == "both"
    assert [m["content"] for m in posted[0]["messages"]][-1].startswith("the biggest mistake")
    assert "Conversation (1 match" in card


def test_unread_chat_is_stated_not_silently_skipped(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.Action, "_post", lambda self, payload: RESULT)

    async def empty_storage(self, chat_id, message_id):
        return []

    monkeypatch.setattr(module.Action, "_from_storage", empty_storage)
    body = {"id": "m-9", "chat_id": "c-1", "model": "llama"}

    card = html_of(
        asyncio.run(dispatch_like_open_webui(module, body, __event_call__=Recorder("hata")))
    )

    assert "could not be read" in card


def test_branch_walk_follows_parent_ids():
    """An edited chat is a tree; search must stay on the branch on screen."""
    module = load_module()
    chat = {
        "history": {
            "currentId": "c",
            "messages": {
                "a": {"id": "a", "parentId": None, "role": "user", "content": "root"},
                "b": {"id": "b", "parentId": "a", "role": "assistant", "content": "old edit"},
                "c": {"id": "c", "parentId": "a", "role": "assistant", "content": "on screen"},
            },
        }
    }

    ordered = module.Action._order(chat, "")

    assert [m["content"] for m in ordered] == ["root", "on screen"]


def test_no_passage_note_when_the_chat_has_no_hit(monkeypatch):
    module = load_module()
    payload = json.loads(json.dumps(RESULT))
    payload["result"]["chat"] = []
    monkeypatch.setattr(module.Action, "_post", lambda self, p: payload)
    body = {"id": "m1", "messages": [{"role": "user", "content": "unrelated text"}]}

    card = html_of(asyncio.run(dispatch_like_open_webui(module, body)))

    assert "No passage in this conversation contains" in card
    assert "Sources (1)" in card


def test_card_offers_a_way_to_close_itself(monkeypatch):
    """The embed iframe cannot be removed by the document it holds, so the card
    collapses to one line: natively, through <details>, and on Escape."""
    module = load_module()
    monkeypatch.setattr(module.Action, "_post", lambda self, p: RESULT)

    card = html_of(asyncio.run(dispatch_like_open_webui(module, {"id": "m1"})))

    # A <details>/<summary> pair closes without a single line of script, which
    # is what a sandboxed embed can be relied on to run.
    assert "<details id='trick-card' open>" in card
    assert "<summary>" in card
    assert "id='trick-body'" in card
    assert "<summary>" in card and "aria-label='Close'" in card
    assert "event.key === 'Escape'" in card
    assert card.index("<summary>") < card.index("id='trick-body'")
