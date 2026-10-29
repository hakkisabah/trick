"""MCP server and terminal UI: protocol conformance and session behaviour.

Both surfaces are driven through their real entry points - a scripted stdin and a
captured stdout - because the failures that matter here are framing failures, and
those only appear when the bytes actually travel.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from trick.errors import MatrixCorrupt, ScorerUnavailable
from trick.laya import Ranked
from trick.mcp import (
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    PROTOCOL_VERSION,
    EngineBackend,
    McpServer,
    TOOLS,
)
from trick.search import ResultRow, SearchEngine, SearchOutcome
from trick.textindex import DocumentStore
from trick.tui import TerminalUI

DOCS = [
    {
        "source": "cran",
        "key": "lme4",
        "title": "lme4: Linear Mixed-Effects Models using Eigen and S4",
        "summary": "Mixed effects models.",
        "url": "https://example.test/cran/lme4",
        "meta": {},
    },
    {
        "source": "bioconductor",
        "key": "DESeq2",
        "title": "DESeq2: Differential expression of count data",
        "summary": "RNA-seq counts.",
        "url": "https://example.test/bioc/DESeq2",
        "meta": {},
    },
]


class ScriptedScorer:
    name = "scripted"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def rank(self, query, candidates, instructions=""):
        if self.error is not None:
            raise self.error
        values = [0.1] * len(candidates)
        values[0] = 0.7
        ranked = [Ranked(i, p) for i, p in enumerate(values)]
        ranked.sort(key=lambda r: (-r.probability, r.index))
        return ranked


def engine(tmp_path: Path, *, scorer=None) -> SearchEngine:
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(DOCS)
    return SearchEngine(store, scorer or ScriptedScorer(), use_cache=False)


def make_outcome(query: str = "mixed effects") -> SearchOutcome:
    row = ResultRow(
        rank=1,
        doc_id=1,
        source="cran",
        key="lme4",
        title="lme4: Linear Mixed-Effects Models",
        summary="Mixed effects models.",
        url="https://example.test/cran/lme4",
        retrieval_score=0.8,
        laya_probability=0.6,
        final_score=0.66,
        scored_by_laya=True,
    )
    return SearchOutcome(query=query, rows=[row], degraded=[], report={"elapsed_ms": 5.0, "scorer": "scripted"})


class FakeBackend:
    def __init__(self, *, error: Exception | None = None, degraded: list[str] | None = None) -> None:
        self.error = error
        self.degraded = degraded or []
        self.calls: list[tuple[str, int, bool | None]] = []

    def search(self, query, *, limit=8, live=None):
        self.calls.append((query, limit, live))
        if self.error is not None:
            raise self.error
        outcome = make_outcome(query)
        outcome.degraded = list(self.degraded)
        return outcome

    def health(self):
        return {"store": {"docs": 2, "fts5": True}, "scorer": "scripted", "matrix": None}


def run(lines: list[str], backend=None, *, tui_kwargs: dict | None = None):
    """Feed lines into a TerminalUI, return (code, stdout, stderr)."""
    stdin = io.StringIO("".join(line + "\n" for line in lines))
    stdout, stderr = io.StringIO(), io.StringIO()
    ui = TerminalUI(
        backend or FakeBackend(),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        interactive=False,
        **(tui_kwargs or {}),
    )
    code = ui.run()
    return code, stdout.getvalue(), stderr.getvalue()


def serve(messages: list[dict | str], backend=None) -> tuple[list[dict], str]:
    """Run an MCP session over a scripted stdin, return parsed responses."""
    payload = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages)
    stdin = io.StringIO(payload)
    stdout, stderr = io.StringIO(), io.StringIO()
    server = McpServer(backend or FakeBackend(), stdin=stdin, stdout=stdout, stderr=stderr)
    server.serve()
    text = stdout.getvalue()
    parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    return parsed, text


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------
def test_initialize_reports_protocol_and_capabilities() -> None:
    responses, _ = serve(
        [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}}]
    )
    result = responses[0]["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == "trick"


def test_initialized_notification_gets_no_reply() -> None:
    responses, text = serve(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
    )
    assert len(responses) == 1
    assert text.count("\n") == 1


def test_tools_list_is_a_valid_schema() -> None:
    responses, _ = serve([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    tools = responses[0]["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {"trick_search", "trick_health"}
    search = next(tool for tool in tools if tool["name"] == "trick_search")
    assert search["inputSchema"]["required"] == ["query"]
    assert search["inputSchema"]["additionalProperties"] is False
    assert search["description"].strip()
    assert TOOLS[0]["name"] == "trick_search"


def test_tools_call_search_returns_content_and_structured_data() -> None:
    backend = FakeBackend()
    responses, _ = serve(
        [
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "trick_search", "arguments": {"query": "mixed effects", "limit": 3}},
            }
        ],
        backend,
    )
    result = responses[0]["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert "lme4" in result["content"][0]["text"]
    assert result["structuredContent"]["rows"][0]["key"] == "lme4"
    assert backend.calls == [("mixed effects", 3, True)]


def test_tools_call_passes_live_false() -> None:
    backend = FakeBackend()
    serve(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "trick_search", "arguments": {"query": "x y", "live": False}},
            }
        ],
        backend,
    )
    assert backend.calls == [("x y", 8, False)]


def test_tools_call_rejects_empty_query_and_bad_limit() -> None:
    responses, _ = serve(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "trick_search", "arguments": {"query": "  "}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "trick_search", "arguments": {"query": "ok", "limit": 99}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "trick_search", "arguments": {"query": "ok", "limit": "many"}},
            },
        ]
    )
    assert all(response["result"]["isError"] is True for response in responses)
    assert all(response["result"]["structuredContent"]["s_code"] == "S8" for response in responses)


def test_tools_call_health() -> None:
    responses, _ = serve([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "trick_health", "arguments": {}}}])
    result = responses[0]["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["store"]["docs"] == 2


def test_tool_failure_is_a_result_not_a_crash() -> None:
    backend = FakeBackend(error=ScorerUnavailable("auth", "HTTP 401"))
    responses, _ = serve(
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "trick_search", "arguments": {"query": "x y"}}}],
        backend,
    )
    result = responses[0]["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["s_code"] == "S5"
    assert "S5" in result["content"][0]["text"]


def test_unknown_tool_is_invalid_params() -> None:
    responses, _ = serve([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}])
    assert responses[0]["error"]["code"] == JSONRPC_INVALID_PARAMS


def test_unknown_method_is_method_not_found() -> None:
    responses, _ = serve([{"jsonrpc": "2.0", "id": 1, "method": "resources/list"}])
    assert responses[0]["error"]["code"] == JSONRPC_METHOD_NOT_FOUND


def test_broken_json_is_a_parse_error_and_session_continues() -> None:
    responses, _ = serve(["{not json", {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
    assert responses[0]["error"]["code"] == JSONRPC_PARSE_ERROR
    assert responses[1]["result"] == {}


def test_ping_answers_with_an_empty_result() -> None:
    responses, _ = serve([{"jsonrpc": "2.0", "id": 5, "method": "ping"}])
    assert responses[0] == {"jsonrpc": "2.0", "id": 5, "result": {}}


def test_empty_lines_are_ignored() -> None:
    responses, text = serve(["", "   ", {"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert len(responses) == 1
    assert text.strip().count("\n") == 0


def test_stdout_carries_only_jsonrpc() -> None:
    """A stray print corrupts the stream; the whole session must still parse."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "trick_search", "arguments": {"query": "mixed"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "trick_health", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "bogus"},
    ]
    responses, text = serve(messages)
    assert len(responses) == 5
    for line in text.splitlines():
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"
        assert "id" in message


def test_notification_failure_does_not_produce_a_reply() -> None:
    backend = FakeBackend(error=ScorerUnavailable("timeout", "slow"))
    responses, text = serve(
        [{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "trick_search", "arguments": {"query": "x y"}}}],
        backend,
    )
    assert responses == []
    assert text == ""


def test_engine_backend_translates_live_flag(tmp_path: Path) -> None:
    backend = EngineBackend(engine(tmp_path))
    outcome = backend.search("mixed effects", limit=2, live=False)
    assert outcome.rows
    assert backend.health()["store"]["docs"] == 2
    backend.close()


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------
def test_tui_searches_on_enter_and_prints_rows() -> None:
    code, out, err = run(["mixed effects"])
    assert code == 0
    assert "lme4" in out
    assert "1*" in out  # scored by the model
    assert err == ""


def test_tui_ignores_blank_lines() -> None:
    code, out, _err = run(["", "   ", "mixed effects"])
    assert code == 0
    assert out.count("trick:") == 1


def test_tui_limit_command_changes_the_next_query() -> None:
    backend = FakeBackend()
    _code, _out, err = run(["/limit 2", "mixed effects"], backend)
    assert "limit set to 2" in err
    assert backend.calls[0][1] == 2


def test_tui_limit_rejects_out_of_range() -> None:
    _code, _out, err = run(["/limit 99"])
    assert "between 1 and 26" in err


def test_tui_live_command_toggles() -> None:
    backend = FakeBackend()
    _code, _out, err = run(["/live off", "x y"], backend)
    assert "live turned off" in err
    assert backend.calls[0][2] is False


def test_tui_help_lists_every_command() -> None:
    _code, out, _err = run(["/help"])
    for command in ("/limit", "/live", "/verbose", "/json", "/health", "/quit"):
        assert command in out


def test_tui_quit_stops_reading() -> None:
    backend = FakeBackend()
    code, out, _err = run(["/quit", "mixed effects"], backend)
    assert code == 0
    assert backend.calls == []
    assert "lme4" not in out


def test_tui_eof_ends_the_session() -> None:
    code, _out, _err = run([])
    assert code == 0


def test_tui_unknown_command_warns_and_continues() -> None:
    backend = FakeBackend()
    code, _out, err = run(["/nope", "mixed effects"], backend)
    assert code == 0
    assert "unknown command" in err
    assert backend.calls


def test_tui_search_failure_keeps_the_session() -> None:
    backend = FakeBackend(error=MatrixCorrupt("magic", "bad header"))
    code, out, err = run(["mixed effects", "/health"], backend)
    assert code == 0
    assert "S2" in err
    assert "health:" in out  # the session survived


def test_tui_json_mode_emits_parsable_output() -> None:
    _code, out, _err = run(["/json", "mixed effects"])
    payload = json.loads(out)
    assert payload["query"] == "mixed effects"
    assert payload["rows"][0]["key"] == "lme4"


def test_tui_verbose_shows_components() -> None:
    _code, out, _err = run(["/verbose", "mixed effects"])
    assert "components" in out
    assert "laya=" in out


def test_tui_health_prints_store_and_scoring() -> None:
    _code, out, _err = run(["/health"])
    assert "documents" in out
    assert "scoring" in out


def test_tui_degraded_codes_are_explained() -> None:
    backend = FakeBackend(degraded=["S5"])
    _code, out, _err = run(["x y"], backend)
    assert "degraded S5" in out
    assert "laya did not answer" in out


def test_tui_with_a_real_engine(tmp_path: Path) -> None:
    stdin, stdout, stderr = io.StringIO("mixed effects\n/quit\n"), io.StringIO(), io.StringIO()
    ui = TerminalUI(
        EngineBackend(engine(tmp_path)),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        interactive=False,
    )
    assert ui.run() == 0
    assert "lme4" in stdout.getvalue()


def test_tui_non_interactive_writes_no_history(tmp_path: Path) -> None:
    history = tmp_path / "history"
    run(["mixed effects"], tui_kwargs={"history_path": history})
    assert not history.exists()


def test_tui_uses_engine_backend_protocol(tmp_path: Path) -> None:
    """The TUI must accept whatever the CLI hands it: socket client or engine."""
    backend = EngineBackend(engine(tmp_path))
    ui = TerminalUI(backend, stdin=io.StringIO(""), stdout=io.StringIO(), stderr=io.StringIO(), interactive=False)
    outcome = ui.search("mixed effects")
    assert outcome is not None and outcome.rows
    ui.close()  # closes the store through the adapter
