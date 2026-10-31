"""Conversation scope across the transports.

The conversation index lives in the engine, but the transcript arrives over a
socket, an HTTP body or an MCP tool call. Each of those hops can silently drop
the argument, and a dropped argument looks exactly like "your chat does not
mention it" - the failure mode this feature exists to remove. So the same
transcript is pushed through every real transport here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from trick.bridge import BridgeServer
from trick.laya import Ranked
from trick.mcp import EngineBackend, McpServer
from trick.search import SearchEngine
from trick.service import Service, ServiceClient, UnixServiceServer
from trick.textindex import DocumentStore

DOCS = [
    {
        "source": "cran",
        "key": "sf",
        "title": "sf: Simple Features for R",
        "summary": "Spatial data frames, joins and coordinate reference systems.",
        "url": "https://example.test/cran/sf",
        "meta": {},
    },
]

TARGET = (
    "For the spatial join you should reproject both layers to EPSG:3857 with st_transform "
    "before calling st_join, otherwise the raster stack disagrees about its projection."
)


def chat() -> list[dict]:
    messages: list[dict] = []
    for index in range(14):
        body = TARGET if index == 6 else f"Turn {index}. Notes about the pipeline and the matrix."
        messages.append({"role": "user" if index % 2 == 0 else "assistant", "content": body})
    return messages


class ScriptedScorer:
    name = "scripted"

    def rank(self, query, candidates, instructions=""):
        scores = [0.9 if "EPSG:3857" in text else 0.02 for text in candidates]
        total = sum(scores) or 1.0
        ranked = [Ranked(index, score / total) for index, score in enumerate(scores)]
        ranked.sort(key=lambda item: (-item.probability, item.index))
        return ranked


def build_engine(tmp_path: Path) -> SearchEngine:
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(DOCS)
    engine = SearchEngine(store, ScriptedScorer(), use_cache=False)
    engine.conversation.gate_margin = 0.0
    return engine


def build_service(tmp_path: Path) -> Service:
    return Service(lambda: build_engine(tmp_path))


# ---------------------------------------------------------------------------
# unix socket
# ---------------------------------------------------------------------------
@pytest.fixture()
def server(tmp_path: Path):
    service = build_service(tmp_path)
    instance = UnixServiceServer(tmp_path / "run" / "trick.sock", service)
    instance.start()
    yield instance, tmp_path / "run" / "trick.sock", service
    instance.stop()
    service.close()


def test_a_transcript_survives_the_socket(server) -> None:
    _instance, sock, _service = server
    client = ServiceClient(sock)
    try:
        outcome = client.search("spatial join reproject", messages=chat(), scope="both")
    finally:
        client.close()
    assert outcome.scope == "both"
    assert outcome.chat
    assert outcome.chat[0].turn == 7
    assert "EPSG:3857" in outcome.chat[0].text
    assert outcome.chat_report["turn_count"] == 14


def test_a_large_transcript_fits_the_frame(server) -> None:
    """200 turns of ordinary chat must not hit the line cap."""
    _instance, sock, _service = server
    big = [
        {"role": "user", "content": f"Turn {index}. " + ("context sentence. " * 40)}
        for index in range(200)
    ]
    client = ServiceClient(sock)
    try:
        outcome = client.search("context sentence", messages=big, scope="chat")
    finally:
        client.close()
    # The frame carried 200 turns without an S8 and the index built over them.
    assert outcome.chat
    assert outcome.chat_report["turn_count"] == 200
    assert outcome.chat_report["chunk_count"] > 200


def test_a_malformed_transcript_is_an_s8_not_an_empty_answer(server) -> None:
    _instance, sock, _service = server
    client = ServiceClient(sock)
    try:
        with pytest.raises(Exception) as caught:
            client.search("query", messages="not a list")
    finally:
        client.close()
    assert getattr(caught.value, "s_code", "") == "S8"


# ---------------------------------------------------------------------------
# HTTP bridge
# ---------------------------------------------------------------------------
def _post(port: int, payload: dict, token: str):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/search",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_a_transcript_survives_the_http_bridge(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    token = "b" * 48
    bridge = BridgeServer(service, token, port=0)
    port = bridge.start()
    try:
        status, payload = _post(port, {"query": "spatial join", "messages": chat(), "limit": 4}, token)
    finally:
        bridge.stop()
        service.close()
    assert status == 200, payload
    result = payload["result"]
    assert result["scope"] == "both"
    assert result["chat"][0]["turn"] == 7
    assert result["rows"]
    assert result.get("degraded_text") == []


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------
def _mcp(lines: list[dict], backend) -> list[dict]:
    import io

    text = "".join(json.dumps(line) + "\n" for line in lines)
    stdout = io.StringIO()
    McpServer(backend, stdin=io.StringIO(text), stdout=stdout, stderr=io.StringIO()).serve()
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def test_the_tool_schema_publishes_the_conversation_arguments() -> None:
    from trick.mcp import TOOLS

    properties = TOOLS[0]["inputSchema"]["properties"]
    assert properties["messages"]["type"] == "array"
    assert properties["messages"]["items"]["required"] == ["role", "content"]
    assert properties["scope"]["enum"] == ["sources", "chat", "both"]
    assert "conversation" in TOOLS[0]["description"]


def test_a_transcript_survives_the_mcp_tool_call(tmp_path: Path) -> None:
    engine = build_engine(tmp_path)
    backend = EngineBackend(engine)
    try:
        responses = _mcp(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "trick_search",
                        "arguments": {"query": "spatial join", "messages": chat(), "limit": 4},
                    },
                }
            ],
            backend,
        )
    finally:
        engine.store.close()
    result = responses[0]["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["chat"][0]["turn"] == 7
    assert structured["scope"] == "both"
    assert "conversation matches" in result["content"][0]["text"]


def test_mcp_rejects_a_malformed_transcript_as_a_tool_error(tmp_path: Path) -> None:
    engine = build_engine(tmp_path)
    backend = EngineBackend(engine)
    try:
        responses = _mcp(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "trick_search",
                        "arguments": {"query": "x", "messages": "nope"},
                    },
                }
            ],
            backend,
        )
    finally:
        engine.store.close()
    result = responses[0]["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["s_code"] == "S8"


def test_mcp_scope_chat_returns_no_catalogue_rows(tmp_path: Path) -> None:
    engine = build_engine(tmp_path)
    backend = EngineBackend(engine)
    try:
        responses = _mcp(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "trick_search",
                        "arguments": {"query": "spatial join", "messages": chat(), "scope": "chat"},
                    },
                }
            ],
            backend,
        )
    finally:
        engine.store.close()
    structured = responses[0]["result"]["structuredContent"]
    assert structured["rows"] == []
    assert structured["chat"]
