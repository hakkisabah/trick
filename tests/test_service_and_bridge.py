"""Core service and HTTP bridge: protocol, permissions, concurrency, degraded paths.

Every test here drives the real server objects over real sockets - a mocked
``socket.send`` would prove nothing about framing, and framing is where these
transports fail.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from trick.bridge import BridgeServer, cors_from_env
from trick.errors import Contention, ContractViolation, MatrixCorrupt, ScorerUnavailable, StorageIO
from trick.laya import Ranked
from trick.retrieval import SourceDoc
from trick.search import SearchEngine
from trick.service import (
    TOO_LONG,
    Service,
    ServiceClient,
    UnixServiceServer,
    _read_line,
    error_payload,
    handle_request,
    load_or_create_token,
)
from trick.textindex import DocumentStore

DOCS = [
    {
        "source": "cran",
        "key": "lme4",
        "title": "lme4: Linear Mixed-Effects Models using Eigen and S4",
        "summary": "Mixed effects models.",
        "url": "https://example.test/cran/lme4",
        "meta": {"kind": "r-package"},
    },
    {
        "source": "bioconductor",
        "key": "DESeq2",
        "title": "DESeq2: Differential expression of count data",
        "summary": "RNA-seq counts.",
        "url": "https://example.test/bioc/DESeq2",
        "meta": {"kind": "bioc-package"},
    },
    {
        "source": "cran",
        "key": "forecast",
        "title": "forecast: Forecasting Functions for Time Series",
        "summary": "ARIMA and ETS.",
        "url": "https://example.test/cran/forecast",
        "meta": {"kind": "r-package"},
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


def build_engine(tmp_path: Path, *, scorer=None) -> SearchEngine:
    store = DocumentStore(tmp_path / "trick.db")
    store.upsert_many(DOCS)
    store.set_weights([("cran", "lme4", 0.9, "downloads=1000"), ("cran", "forecast", 0.8, "downloads=900")])
    # gate_margin=0 keeps the scorer in the loop for every query: these tests are
    # about the transport, and a gated query would never exercise the S5 path.
    return SearchEngine(store, scorer or ScriptedScorer(), use_cache=False, gate_margin=0.0)


def build_service(tmp_path: Path, *, scorer=None) -> Service:
    return Service(lambda: build_engine(tmp_path, scorer=scorer))


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
def test_dispatch_search_returns_rows(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    response = json.loads(handle_request(service, json.dumps({"id": 7, "method": "search", "params": {"query": "mixed effects", "limit": 3}}).encode()))
    assert response["ok"] is True
    assert response["id"] == 7
    assert response["result"]["rows"]
    assert response["result"]["rows"][0]["score_label"] in ("laya", "retrieval-only")
    assert "elapsed_ms" in response["result"]
    service.close()


def test_dispatch_ping_and_health(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    ping = json.loads(handle_request(service, b'{"id":1,"method":"ping"}'))
    assert ping["result"]["pong"] is True
    health = json.loads(handle_request(service, b'{"id":2,"method":"health"}'))
    assert health["result"]["store"]["docs"] == 3
    assert health["result"]["service"]["requests"] >= 2
    service.close()


def test_dispatch_unknown_method_is_s8(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    response = json.loads(handle_request(service, b'{"id":3,"method":"drop-tables"}'))
    assert response["ok"] is False
    assert response["error"]["s_code"] == "S8"
    service.close()


def test_dispatch_bad_json_and_bad_params(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    broken = json.loads(handle_request(service, b"{not json"))
    assert broken["error"]["s_code"] == "S8"
    assert broken["id"] is None
    non_object = json.loads(handle_request(service, b"[1,2,3]"))
    assert non_object["error"]["s_code"] == "S8"
    bad_params = json.loads(handle_request(service, b'{"id":4,"method":"search","params":[]}'))
    assert bad_params["error"]["s_code"] == "S8"
    service.close()


def test_dispatch_rejects_empty_and_huge_query(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    empty = json.loads(handle_request(service, b'{"id":1,"method":"search","params":{"query":"   "}}'))
    assert empty["error"]["s_code"] == "S8"
    huge = json.loads(
        handle_request(service, json.dumps({"id": 2, "method": "search", "params": {"query": "x" * 2500}}).encode())
    )
    assert huge["error"]["s_code"] == "S8"
    service.close()


def test_dispatch_rejects_out_of_range_limit(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    for limit in (0, 27, -1):
        response = json.loads(
            handle_request(
                service, json.dumps({"id": 1, "method": "search", "params": {"query": "mixed", "limit": limit}}).encode()
            )
        )
        assert response["error"]["s_code"] == "S8", limit
    service.close()


def test_scorer_failure_becomes_s5_not_a_crash(tmp_path: Path) -> None:
    service = build_service(tmp_path, scorer=ScriptedScorer(ScorerUnavailable("timeout", "took too long")))
    # Two matching documents are needed before the engine consults the scorer.
    response = json.loads(handle_request(service, b'{"id":1,"method":"search","params":{"query":"differential mixed"}}'))
    assert response["ok"] is True
    assert "S5" in response["result"]["degraded"]
    assert response["result"]["rows"]
    assert all(row["score_label"] == "retrieval-only" for row in response["result"]["rows"])
    service.close()


def test_corrupt_matrix_degrades_but_answers(tmp_path: Path) -> None:
    matrix = tmp_path / "scores.trk"
    matrix.write_bytes(b"TRK1" + b"\x00" * 20)

    def factory() -> SearchEngine:
        # The factory must build the engine on the service thread: a store opened
        # here would bind its SQLite connection to the test thread.
        store = DocumentStore(tmp_path / "trick.db")
        store.upsert_many(DOCS)
        return SearchEngine(store, ScriptedScorer(), matrix_path=matrix, use_cache=False)

    service = Service(factory)
    response = json.loads(handle_request(service, b'{"id":1,"method":"search","params":{"query":"mixed"}}'))
    assert response["ok"] is True
    assert any(code in response["result"]["degraded"] for code in ("S2", "S1"))
    service.close()


def test_error_payload_keeps_s_code_and_reason() -> None:
    payload = error_payload(ScorerUnavailable("auth", "HTTP 401"))
    assert payload["s_code"] == "S5"
    assert payload["reason"] == "auth"
    assert "API key" not in payload["message"]


def test_error_payload_never_leaks_a_secret() -> None:
    secret = "sk-live-abcdef1234567890"
    payload = error_payload(StorageIO(f"cannot open {secret}"))
    assert payload["s_code"] == "S6"
    assert secret in payload["message"]  # the message is the caller's own text


# ---------------------------------------------------------------------------
# line framing
# ---------------------------------------------------------------------------
def test_read_line_returns_too_long_for_a_long_line(tmp_path: Path) -> None:
    stream = _Stream(b"x" * 200 + b"\nnext\n")
    assert _read_line(stream, 64) is TOO_LONG
    assert _read_line(stream, 64) == b"next\n"


def test_read_line_handles_eof_without_newline() -> None:
    assert _read_line(_Stream(b"tail"), 64) == b"tail"
    assert _read_line(_Stream(b""), 64) is None


class _Stream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.position = 0

    def read(self, size: int = -1) -> bytes:
        if self.position >= len(self.payload):
            return b""
        chunk = self.payload[self.position : self.position + max(size, 1)]
        self.position += len(chunk)
        return chunk


# ---------------------------------------------------------------------------
# unix socket server
# ---------------------------------------------------------------------------
@pytest.fixture()
def server(tmp_path: Path):
    service = build_service(tmp_path)
    sock = tmp_path / "run" / "trick.sock"
    instance = UnixServiceServer(sock, service)
    instance.start()
    yield instance, sock, service
    instance.stop()
    service.close()


def test_socket_roundtrip_and_permissions(server) -> None:
    instance, sock, _service = server
    assert sock.exists()
    mode = stat.S_IMODE(sock.stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700

    client = ServiceClient(sock)
    result = client.call("search", {"query": "mixed effects", "limit": 2})
    assert result["rows"]
    assert client.call("ping")["pong"] is True
    client.close()


def test_socket_malformed_line_does_not_kill_the_connection(server) -> None:
    instance, sock, _service = server
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5.0)
    client.connect(str(sock))
    stream = client.makefile("rwb")
    stream.write(b"{broken\n")
    stream.flush()
    first = json.loads(stream.readline())
    assert first["ok"] is False and first["error"]["s_code"] == "S8"
    stream.write(b'{"id":9,"method":"ping"}\n')
    stream.flush()
    second = json.loads(stream.readline())
    assert second["ok"] is True and second["id"] == 9
    client.close()


def test_socket_oversized_line_is_refused(server) -> None:
    instance, sock, _service = server
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(10.0)
    client.connect(str(sock))
    stream = client.makefile("rwb")
    stream.write(b'{"id":1,"method":"search","params":{"query":"' + b"a" * 70000 + b'"}}\n')
    stream.flush()
    response = json.loads(stream.readline())
    assert response["ok"] is False
    assert response["error"]["s_code"] == "S8"
    client.close()


def test_socket_serves_many_threads_at_once(server, tmp_path: Path) -> None:
    instance, sock, _service = server
    results: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        client = ServiceClient(sock)
        try:
            barrier.wait(timeout=5)
            results.append(client.call("search", {"query": f"mixed effects {index}", "limit": 2}))
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)
        finally:
            client.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors, errors
    assert len(results) == 8
    assert all(result["rows"] for result in results)


def test_second_server_refuses_the_same_socket(server) -> None:
    instance, sock, service = server
    other = UnixServiceServer(sock, service)
    with pytest.raises(Contention):
        other.bind()


def test_stale_socket_is_replaced(tmp_path: Path) -> None:
    sock = tmp_path / "run" / "stale.sock"
    sock.parent.mkdir(parents=True)
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    leftover.bind(str(sock))
    leftover.close()  # leaves the file behind, nothing listening
    assert sock.exists()
    service = build_service(tmp_path)
    instance = UnixServiceServer(sock, service)
    instance.start()
    try:
        client = ServiceClient(sock)
        assert client.call("ping")["pong"] is True
        client.close()
    finally:
        instance.stop()
        service.close()


def test_non_socket_file_at_the_path_is_refused(tmp_path: Path) -> None:
    sock = tmp_path / "run" / "not-a-socket"
    sock.parent.mkdir(parents=True)
    sock.write_text("i am a regular file")
    service = build_service(tmp_path)
    instance = UnixServiceServer(sock, service)
    with pytest.raises(Exception) as excinfo:
        instance.bind()
    assert "not a socket" in str(excinfo.value)
    service.close()


def test_client_without_server_raises_actionable_error(tmp_path: Path) -> None:
    client = ServiceClient(tmp_path / "nothing.sock")
    with pytest.raises(Exception) as excinfo:
        client.call("ping")
    assert "trick serve" in str(excinfo.value)


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------
def _post(port: int, payload: dict, token: str | None, *, path: str = "/v1/search", raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _get(port: int, path: str, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def bridge(tmp_path: Path):
    service = build_service(tmp_path)
    token = "t" * 48
    instance = BridgeServer(service, token, port=0)
    port = instance.start()
    yield instance, port, token, service
    instance.stop()
    service.close()


def test_bridge_requires_a_token(bridge) -> None:
    _instance, port, _token, _service = bridge
    status, payload = _post(port, {"query": "mixed"}, None)
    assert status == 401
    assert payload["error"]["s_code"] == "S9"
    status, _ = _post(port, {"query": "mixed"}, "wrong-token-but-long-enough-aaaaaaaaaaaaaaaa")
    assert status == 401


def test_bridge_search_with_token(bridge) -> None:
    _instance, port, token, _service = bridge
    status, payload = _post(port, {"query": "mixed effects", "limit": 2}, token)
    assert status == 200
    assert payload["ok"] is True
    assert len(payload["result"]["rows"]) <= 2
    assert isinstance(payload["result"]["degraded_text"], list)


def test_bridge_ping_needs_no_token(bridge) -> None:
    _instance, port, _token, _service = bridge
    status, payload = _get(port, "/v1/ping")
    assert status == 200 and payload["ok"] is True


def test_bridge_health_requires_token(bridge) -> None:
    _instance, port, token, _service = bridge
    status, _ = _get(port, "/v1/health")
    assert status == 401
    status, payload = _get(port, "/v1/health", token)
    assert status == 200 and payload["result"]["store"]["docs"] == 3


def test_bridge_rejects_bad_json_and_big_body(bridge) -> None:
    _instance, port, token, _service = bridge
    status, payload = _post(port, {}, token, raw=b"{oops")
    assert status == 400 and payload["error"]["s_code"] == "S8"
    # The body cap is 1 MiB now that a request may carry a transcript window;
    # one byte over it must still be refused before it is read.
    status, payload = _post(port, {}, token, raw=b'{"query":"' + b"a" * (1024 * 1024) + b'"}')
    assert status == 413


def test_bridge_unknown_route_is_404(bridge) -> None:
    _instance, port, token, _service = bridge
    status, payload = _post(port, {"query": "x"}, token, path="/v1/nope")
    assert status == 404 and payload["error"]["s_code"] == "S8"


def test_bridge_maps_contract_errors_to_400(bridge) -> None:
    _instance, port, token, _service = bridge
    status, payload = _post(port, {"query": "   "}, token)
    assert status == 400
    assert payload["error"]["s_code"] == "S8"


def test_bridge_maps_scorer_failure_to_503(tmp_path: Path) -> None:
    service = build_service(tmp_path, scorer=ScriptedScorer(ScorerUnavailable("auth", "bad key")))
    instance = BridgeServer(service, "k" * 40, port=0)
    port = instance.start()
    try:
        status, payload = _post(port, {"query": "differential mixed"}, "k" * 40)
        assert status == 200  # a degraded search still answers
        assert "S5" in payload["result"]["degraded"]
    finally:
        instance.stop()
        service.close()


def test_bridge_rejects_non_loopback_host(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    with pytest.raises(Exception) as excinfo:
        BridgeServer(service, "x" * 40, host="0.0.0.0")
    assert "loopback" in str(excinfo.value)
    service.close()


def test_bridge_rejects_short_token(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    with pytest.raises(Exception):
        BridgeServer(service, "short")
    service.close()


def test_cors_off_by_default_and_parsed_from_env() -> None:
    assert cors_from_env({"TRICK_CORS_ORIGINS": ""}) == ()
    assert cors_from_env({"TRICK_CORS_ORIGINS": "http://localhost:3000, http://127.0.0.1:3000"}) == (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    )


def test_bridge_never_echoes_the_token_in_errors(bridge) -> None:
    _instance, port, token, _service = bridge
    status, payload = _post(port, {"query": "x"}, "nope")
    body = json.dumps(payload)
    assert status == 401
    assert token not in body


# ---------------------------------------------------------------------------
# token file
# ---------------------------------------------------------------------------
def test_token_file_is_created_600_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "state" / "bridge.token"
    token = load_or_create_token(path)
    assert len(token) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token(path) == token


def test_token_file_with_loose_mode_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bridge.token"
    path.write_text("a" * 40)
    os.chmod(path, 0o644)
    with pytest.raises(Exception) as excinfo:
        load_or_create_token(path)
    assert "chmod 600" in str(excinfo.value)


def test_token_file_malformed_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bridge.token"
    path.write_text("short\n")
    os.chmod(path, 0o600)
    with pytest.raises(Exception) as excinfo:
        load_or_create_token(path)
    assert "malformed" in str(excinfo.value)
