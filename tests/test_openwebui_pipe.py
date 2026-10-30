"""The Open WebUI Pipe, tested against a real HTTP server.

The Pipe runs inside Open WebUI's container, so this test has to fake two things
and must not fake the third:

* ``pydantic`` - the host venv does not have it, and the Pipe only uses it for
  ``BaseModel``/``Field``. A two-line stub is honest; a real pydantic would not
  test anything the container does not already guarantee.
* Open WebUI itself - ``pipe()`` is a plain method with a fixed signature, so
  calling it directly is the same call the host makes.

What is *not* faked is the transport: a real ``http.server`` on a real port
answers, and refuses a wrong token like the bridge does. A test with a mocked
``urlopen`` would pass while the Pipe sent the token in the wrong header.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PIPE_PATH = Path(__file__).resolve().parents[1] / "integrations" / "openwebui" / "trick_pipe.py"
TOKEN = "x" * 40


def _install_pydantic_stub() -> None:
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


@pytest.fixture()
def pipe_module():
    _install_pydantic_stub()
    spec = importlib.util.spec_from_file_location("trick_pipe_under_test", PIPE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _search_payload(rows: int = 2, degraded: list[str] | None = None) -> dict:
    items = [
        {
            "rank": index + 1,
            "doc_id": 100 + index,
            "source": "cran" if index % 2 == 0 else "bioconductor",
            "key": f"pkg{index}",
            "title": f"Package {index}",
            "summary": "A description that is long enough to be truncated " * 8,
            "url": f"https://example.test/pkg{index}",
            "retrieval_score": 0.9,
            "laya_probability": 0.5,
            "final_score": 0.75,
            "score_label": "laya",
        }
        for index in range(rows)
    ]
    return {
        "ok": True,
        "result": {
            "query": "q",
            "degraded": degraded or [],
            "degraded_text": [f"text for {code}" for code in (degraded or [])],
            "report": {"elapsed_ms": 12.0},
            "rows": items,
        },
    }


class _Server:
    def __init__(self, response: dict, status: int = 200) -> None:
        self.response = response
        self.status = status
        self.seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # noqa: D401 - silence
                return

            def do_POST(self):  # noqa: N802 - http.server name
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.seen.append(
                    {
                        "auth": self.headers.get("Authorization"),
                        "path": self.path,
                        "body": json.loads(body.decode("utf-8")),
                    }
                )
                if outer.status != 200 or self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    payload = {"ok": False, "error": {"s_code": "S9", "message": "no"}}
                    status = 401
                else:
                    payload = outer.response
                    status = 200
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        import threading

        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/search"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _pipe(module, url: str, token: str = TOKEN, limit: int = 2):
    pipe = module.Pipe()
    pipe.valves.TRICK_URL = url
    pipe.valves.TRICK_TOKEN = token
    pipe.valves.LIMIT = limit
    return pipe


def test_manifest_fields_open_webui_reads(pipe_module):
    pipe = pipe_module.Pipe()
    assert pipe.type == "manifold"
    assert pipe.id == "trick"
    assert pipe.name.endswith(": ")
    assert pipe.pipes()[0]["id"] == "trick-search"


def test_successful_search_renders_links_ranks_and_labels(pipe_module):
    server = _Server(_search_payload())
    try:
        text = _pipe(pipe_module, server.url).pipe("negative binomial", "trick-search", [], {})
    finally:
        server.close()
    assert "Package 0" in text
    assert "(https://example.test/pkg0)" in text
    assert "1. " in text
    assert "CRAN" in text and "Bioconductor" in text
    assert "0.750" in text
    assert server.seen[0]["auth"] == f"Bearer {TOKEN}"
    assert server.seen[0]["body"] == {"query": "negative binomial", "limit": 2, "live": True}


def test_long_summary_is_truncated_not_dumped(pipe_module):
    server = _Server(_search_payload(rows=1))
    try:
        text = _pipe(pipe_module, server.url).pipe("q", "trick-search", [], {})
    finally:
        server.close()
    assert "..." in text
    assert len(text) < 2000


def test_wrong_token_tells_the_user_which_valve_to_paste(pipe_module):
    server = _Server(_search_payload())
    try:
        text = _pipe(pipe_module, server.url, token="y" * 40).pipe("q", "trick-search", [], {})
    finally:
        server.close()
    assert "TRICK_TOKEN" in text
    assert "bridge.token" in text


def test_unreachable_service_explains_the_url_valve(pipe_module):
    text = _pipe(pipe_module, "http://127.0.0.1:9/v1/search").pipe("q", "trick-search", [], {})
    assert "Trick could not search" in text
    assert "TRICK_URL" in text


def test_empty_result_is_an_answer_not_an_error(pipe_module):
    payload = _search_payload(rows=0, degraded=["S4"])
    server = _Server(payload)
    try:
        text = _pipe(pipe_module, server.url).pipe("nothing matches", "trick-search", [], {})
    finally:
        server.close()
    assert "Nothing matched" in text
    assert "text for S4" in text


def test_empty_message_does_not_call_the_service(pipe_module):
    pipe = _pipe(pipe_module, "http://127.0.0.1:9/v1/search")
    assert "needs a question" in pipe.pipe("   ", "trick-search", [], {})


def test_limit_is_clamped_to_the_service_contract(pipe_module):
    server = _Server(_search_payload())
    try:
        _pipe(pipe_module, server.url, limit=999).pipe("q", "trick-search", [], {})
    finally:
        server.close()
    assert server.seen[0]["body"]["limit"] == 26


# ---------------------------------------------------------------------------
# conversation scope
# ---------------------------------------------------------------------------
def _chat_payload(chat: int = 2, rows: int = 1) -> dict:
    payload = _search_payload(rows=rows)
    payload["result"]["chat"] = [
        {
            "rank": index + 1,
            "turn": 7 + index,
            "role": "assistant" if index % 2 == 0 else "user",
            "text": f"passage {index} about the spatial join",
            "char_start": 10 * index,
            "char_end": 10 * index + 40,
            "retrieval_score": 5.0,
            "laya_probability": 0.6,
            "final_score": 4.1,
            "score_label": "laya",
            "scored_by_laya": True,
        }
        for index in range(chat)
    ]
    return payload


def test_the_chat_is_sent_with_the_messages_it_was_given(pipe_module):
    server = _Server(_chat_payload())
    messages = [
        {"role": "user", "content": "where did we discuss the spatial join?"},
        {"role": "assistant", "content": [{"type": "text", "text": "turn about the polygon layer"}]},
    ]
    try:
        text = _pipe(pipe_module, server.url).pipe("spatial join", "trick-search", messages, {})
    finally:
        server.close()
    body = server.seen[0]["body"]
    assert body["scope"] == "both"
    assert body["messages"][0] == {
        "role": "user",
        "content": "where did we discuss the spatial join?",
    }
    # List-shaped content is flattened, not dropped.
    assert body["messages"][1]["content"] == "turn about the polygon layer"
    assert "conversation match" in text
    assert "turn 7" in text


def test_the_oldest_turns_are_dropped_and_the_reader_is_told(pipe_module):
    server = _Server(_chat_payload(chat=1))
    pipe = _pipe(pipe_module, server.url)
    pipe.valves.CHAT_TURNS = 3
    messages = [{"role": "user", "content": f"turn {index}"} for index in range(10)]
    try:
        text = pipe.pipe("q", "trick-search", messages, {})
    finally:
        server.close()
    kept, dropped = pipe._transcript(messages)
    assert [message["content"] for message in kept] == ["turn 7", "turn 8", "turn 9"]
    assert dropped == 7
    assert server.seen[0]["body"]["messages"] == kept
    assert "1 conversation match" in text
    assert "oldest 7 turns not searched" in text


def test_the_character_budget_stops_at_the_newest_turns(pipe_module):
    pipe = pipe_module.Pipe()
    pipe.valves.CHAT_MAX_CHARS = 1000
    messages = [{"role": "user", "content": "x" * 400} for _ in range(10)]
    kept, dropped = pipe._transcript(messages)
    assert kept and sum(len(item["content"]) for item in kept) <= 1000
    assert dropped == 10 - len(kept)


def test_search_chat_off_sends_no_transcript(pipe_module):
    server = _Server(_chat_payload(chat=0, rows=1))
    pipe = _pipe(pipe_module, server.url)
    pipe.valves.SEARCH_CHAT = False
    try:
        pipe.pipe("q", "trick-search", [{"role": "user", "content": "hello"}], {})
    finally:
        server.close()
    assert "messages" not in server.seen[0]["body"]
    assert "scope" not in server.seen[0]["body"]


def test_a_chat_only_answer_does_not_claim_a_catalogue_miss(pipe_module):
    server = _Server(_chat_payload(chat=1, rows=0))
    try:
        text = _pipe(pipe_module, server.url).pipe("spatial join", "trick-search", [{"role": "user", "content": "x"}], {})
    finally:
        server.close()
    assert "conversation match" in text
    assert "No catalogue source matched" in text


def test_an_empty_conversation_list_sends_nothing(pipe_module):
    server = _Server(_search_payload(rows=1))
    try:
        _pipe(pipe_module, server.url).pipe("q", "trick-search", [{"role": "user", "content": "  "}], {})
    finally:
        server.close()
    assert "messages" not in server.seen[0]["body"]
