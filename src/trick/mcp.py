"""MCP server: expose Trick as tools over stdio.

Model Context Protocol is the cheapest way to reach several hosts at once:
Open WebUI's tool servers, Claude, Codex and any other MCP host speak it, so one
server serves all of them instead of one adapter per product. The transport is
newline-delimited JSON-RPC 2.0 on stdin/stdout.

Two rules make the difference between a demo and something usable:

* **stdout is the protocol.** Every diagnostic goes to stderr. A stray ``print``
  corrupts the stream and the host reports a broken server with no clue why, so
  the tests assert that stdout only ever carries JSON-RPC.
* **a tool failure is a result, not a crash.** ``isError`` carries the ``S<n>``
  code and a sentence, so the model can tell the user "laya did not answer"
  instead of dying mid-turn.

The server never talks to laya or SQLite directly if the core service is running:
it forwards to the unix socket. Without the service it builds an engine in
process, so ``trick mcp`` works standalone for a quick test.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Mapping, Protocol, Sequence, TextIO

from .errors import TrickError
from .render import render_compact, render_outcome
from .search import SearchOutcome

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "trick"
SERVER_VERSION = "0.3.0"
MAX_TOOL_CANDIDATES = 26

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class ToolBackend(Protocol):
    """What the MCP layer needs; satisfied by the socket client or a local engine."""

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        live: bool | None = None,
        messages: Sequence[object] | None = None,
        scope: str = "sources",
    ) -> SearchOutcome: ...

    def health(self) -> dict[str, Any]: ...


TOOLS: list[dict[str, Any]] = [
    {
        "name": "trick_search",
        "description": (
            "Search two things at once and rank them with the laya decision model: "
            "(1) the conversation you pass in `messages`, so you can find the exact passage of a "
            "long chat where something was said, and (2) scientific and engineering sources "
            "(CRAN, Bioconductor, arXiv, OpenAlex, Crossref) for a technical need. "
            "Always pass `messages` when the answer may already be in the chat. Pass the user's "
            "problem statement as the query, not just keywords."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The problem or need in natural language, e.g. 'mixed effects model does not converge'.",
                    "minLength": 2,
                    "maxLength": 2000,
                },
                "messages": {
                    "type": "array",
                    "description": (
                        "Optional conversation to search inside, oldest first, as {role, content} "
                        "objects. Send the chat you are in; the result carries the matching passages "
                        "with their turn numbers."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "description": "user, assistant or system."},
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                    "maxItems": 4000,
                },
                "scope": {
                    "type": "string",
                    "description": "sources, chat, or both (default both when messages are sent).",
                    "enum": ["sources", "chat", "both"],
                    "default": "both",
                },
                "limit": {
                    "type": "integer",
                    "description": f"How many results to return (1-{MAX_TOOL_CANDIDATES}).",
                    "minimum": 1,
                    "maximum": MAX_TOOL_CANDIDATES,
                    "default": 8,
                },
                "live": {
                    "type": "boolean",
                    "description": "Query the live paper APIs as well as the local catalogue. Default true.",
                    "default": True,
                },
                "verbose": {
                    "type": "boolean",
                    "description": "Include the retrieval and model components of every score.",
                    "default": False,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "trick_health",
        "description": (
            "Report Trick's state: indexed document count, score matrix, scoring backend and "
            "whether laya is reachable. Call this before blaming a search result on the data."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class McpServer:
    """One session: read a line, answer a line, until stdin closes."""

    def __init__(
        self,
        backend: ToolBackend,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.backend = backend
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.initialised = False
        self.handled = 0

    # -- transport ---------------------------------------------------------
    def serve(self) -> int:
        """Consume stdin until EOF. Never raises for a bad request."""
        while True:
            line = self.stdin.readline()
            if not line:
                return 0
            text = line.strip()
            if not text:
                continue
            response = self.handle_line(text)
            if response is not None:
                self._write(response)

    def _write(self, message: Mapping[str, Any]) -> None:
        self.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.stdout.flush()

    def _log(self, message: str) -> None:
        try:
            self.stderr.write(f"trick-mcp: {message}\n")
            self.stderr.flush()
        except Exception:  # noqa: BLE001 - logging must never break the stream
            pass

    # -- dispatch ----------------------------------------------------------
    def handle_line(self, text: str) -> dict[str, Any] | None:
        try:
            message = json.loads(text)
        except ValueError as exc:
            return _error(None, JSONRPC_PARSE_ERROR, f"parse error: {exc}")
        if not isinstance(message, dict):
            return _error(None, JSONRPC_INVALID_REQUEST, "message must be an object")
        return self.handle(message)

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message
        if not isinstance(method, str) or not method:
            return None if is_notification else _error(request_id, JSONRPC_INVALID_REQUEST, "method is required")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return None if is_notification else _error(request_id, JSONRPC_INVALID_PARAMS, "params must be an object")

        handler = {
            "initialize": self._initialize,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "ping": lambda _params: ({}, None),
            "notifications/initialized": lambda _params: (None, None),
            "shutdown": lambda _params: ({}, None),
        }.get(method)
        if handler is None:
            if is_notification:
                self._log(f"ignoring unknown notification {method!r}")
                return None
            return _error(request_id, JSONRPC_METHOD_NOT_FOUND, f"unknown method {method!r}")

        try:
            result, error = handler(params)
        except TrickError as exc:
            return None if is_notification else _error(request_id, JSONRPC_INTERNAL_ERROR, str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the session
            self._log(f"{method} failed: {type(exc).__name__}: {exc}")
            return None if is_notification else _error(request_id, JSONRPC_INTERNAL_ERROR, f"{type(exc).__name__}")
        if error is not None:
            return None if is_notification else _error(request_id, error[0], error[1])
        if result is None or is_notification:
            return None
        self.handled += 1
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # -- methods -----------------------------------------------------------
    def _initialize(self, params: Mapping[str, Any]) -> tuple[dict | None, tuple[int, str] | None]:
        requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        if requested not in (PROTOCOL_VERSION, "2024-10-07", "2025-03-26", "2025-06-18"):
            self._log(f"client asked for protocol {requested}, answering {PROTOCOL_VERSION}")
        self.initialised = True
        return (
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Trick searches scientific and engineering sources and ranks them with laya. "
                    "Use trick_search with a problem statement."
                ),
            },
            None,
        )

    def _tools_list(self, _params: Mapping[str, Any]) -> tuple[dict | None, tuple[int, str] | None]:
        return {"tools": TOOLS}, None

    def _tools_call(self, params: Mapping[str, Any]) -> tuple[dict | None, tuple[int, str] | None]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            return None, (JSONRPC_INVALID_PARAMS, "tools/call needs a tool name")
        if not isinstance(arguments, dict):
            return None, (JSONRPC_INVALID_PARAMS, "arguments must be an object")
        if name == "trick_search":
            return self._call_search(arguments), None
        if name == "trick_health":
            return self._call_health(), None
        return None, (JSONRPC_INVALID_PARAMS, f"unknown tool {name!r}")

    def _call_search(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return _tool_error("S8", "query must be a non-empty string")
        limit_raw = arguments.get("limit", 8)
        if isinstance(limit_raw, bool) or not isinstance(limit_raw, (int, str)):
            return _tool_error("S8", "limit must be an integer")
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return _tool_error("S8", "limit must be an integer")
        if not 1 <= limit <= MAX_TOOL_CANDIDATES:
            return _tool_error("S8", f"limit must be between 1 and {MAX_TOOL_CANDIDATES}")
        live = arguments.get("live", True)
        if not isinstance(live, bool):
            live = str(live).strip().lower() not in ("0", "false", "no", "off")
        verbose = bool(arguments.get("verbose", False))
        messages = arguments.get("messages")
        if messages is not None:
            if not isinstance(messages, list):
                return _tool_error("S8", "messages must be an array of {role, content} objects")
            for position, item in enumerate(messages):
                if not isinstance(item, dict):
                    return _tool_error("S8", f"message {position} is not an object")
        scope = arguments.get("scope")
        if scope is None:
            scope = "both"
        scope = str(scope).strip().lower()
        if scope not in ("sources", "chat", "both"):
            return _tool_error("S8", f"unknown scope {scope!r}; use sources, chat or both")
        try:
            outcome = self.backend.search(
                query.strip(),
                limit=limit,
                live=live,
                messages=messages,
                scope=scope,
            )
        except TrickError as exc:
            return _tool_error(exc.s_code, str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a tool error
            return _tool_error("S0", f"{type(exc).__name__}: {exc}")
        text = render_compact(outcome, limit=limit)
        if verbose:
            text = render_outcome(outcome, verbose=True)
        payload = outcome.as_dict()
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": False,
        }

    def _call_health(self) -> dict[str, Any]:
        try:
            info = self.backend.health()
        except TrickError as exc:
            return _tool_error(exc.s_code, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _tool_error("S0", f"{type(exc).__name__}: {exc}")
        return {
            "content": [{"type": "text", "text": json.dumps(info, indent=2, ensure_ascii=False)}],
            "structuredContent": info,
            "isError": False,
        }


def _tool_error(code: str, message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"{code}: {message}"}],
        "structuredContent": {"s_code": code, "message": message},
        "isError": True,
    }


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class EngineBackend:
    """Adapts a local :class:`~trick.search.SearchEngine` to the tool protocol.

    The engine spells the switch ``use_live`` and does not own a ``close``; the
    transports speak ``live``. One adapter here keeps that translation in a single
    place instead of in every host integration.
    """

    def __init__(self, engine) -> None:
        self.engine = engine

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        live: bool | None = None,
        messages: Sequence[object] | None = None,
        scope: str = "sources",
    ) -> SearchOutcome:
        return self.engine.search(
            query,
            limit=limit,
            use_live=live,
            messages=messages,
            scope=scope,
        )

    def health(self) -> dict[str, Any]:
        return self.engine.health()

    def close(self) -> None:
        try:
            self.engine.store.close()
        except Exception:  # noqa: BLE001 - closing must not raise
            pass


def build_backend(socket_path: str | os.PathLike[str], factory: Callable[[], Any]) -> ToolBackend:
    """Prefer the running service; fall back to an in-process engine."""
    from .service import ServiceClient

    client = ServiceClient(socket_path, timeout=180.0)
    try:
        client.call("ping")
    except TrickError:
        client.close()
        return EngineBackend(factory())
    return client


__all__ = [
    "EngineBackend",
    "McpServer",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "TOOLS",
    "build_backend",
]
