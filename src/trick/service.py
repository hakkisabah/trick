"""The Trick core service: one process, two transports.

A search engine that opens a SQLite database and a memory-mapped matrix cannot be
copied into every plugin. So the core runs once and everything else talks to it:

    unix socket  $TRICK_HOME/run/trick.sock   (owner-only, 0600)
    http bridge  127.0.0.1:$TRICK_HTTP_PORT   (bearer token, loopback only)

The unix socket is the transport of record: no port to collide with, no token to
leak, and the kernel enforces the file permission. The HTTP bridge exists because
some hosts (Open WebUI, a Gemini extension, anything running in a container) can
only speak HTTP. It is not a second implementation - it forwards to the same
socket - so there is exactly one ranking path in the product.

Wire format on the socket is newline-delimited JSON::

    {"id": 1, "method": "search", "params": {"query": "...", "limit": 8}}
    {"id": 1, "ok": true, "result": {...}}
    {"id": 1, "ok": false, "error": {"s_code": "S5", "message": "..."}}

Failure rules:

* a malformed line costs one request, never the connection;
* an oversized line is refused before it is buffered (S8);
* concurrent clients queue behind one worker, because the engine owns one SQLite
  connection and one mmap, and pretending otherwise only moves the corruption;
* a stale socket file is detected by trying to connect, then removed. A file that
  is a live socket is never touched.
"""

from __future__ import annotations

import errno
import json
import os
import queue
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .errors import ConfigError, Contention, ContractViolation, StorageIO, TrickError
from .search import SearchEngine, SearchOutcome

MAX_LINE_BYTES = 64 * 1024
DEFAULT_MAX_CLIENTS = 8
DEFAULT_CONCURRENCY = 4
SOCKET_NAME = "trick.sock"
BACKOFF_S = 0.05


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------
def error_payload(exc: BaseException) -> dict[str, Any]:
    """Turn any exception into a protocol error with a stable code."""
    if isinstance(exc, TrickError):
        payload = {"s_code": exc.s_code, "message": str(exc)}
        reason = getattr(exc, "reason", None)
        if reason:
            payload["reason"] = str(reason)
        return payload
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout)):
        return {"s_code": "S7", "message": f"transport: {type(exc).__name__}: {exc}"}
    return {"s_code": "S0", "message": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# the method table
# ---------------------------------------------------------------------------
class Backend(Protocol):
    """What the transports need; the socket client implements this too."""

    def search(self, query: str, *, limit: int = 8, live: bool | None = None) -> SearchOutcome: ...

    def health(self) -> dict[str, Any]: ...


class Service:
    """Owns one engine and serialises access to it.

    The engine is built and used on exactly one thread. That is not decoration:
    a ``sqlite3.Connection`` refuses cross-thread use, and an mmap'd file has no
    synchronisation of its own. A lock alone would not be enough - the first
    request would create the connection on one handler thread and every later
    request would arrive on another. So calls are queued to a single worker and
    the caller blocks on a per-job event.
    """

    def __init__(self, engine_factory: Callable[[], SearchEngine]) -> None:
        self._factory = engine_factory
        self._engine: SearchEngine | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._started_at = time.time()
        self.requests = 0
        self.failures = 0

    # -- lifecycle ---------------------------------------------------------
    def _ensure_worker(self) -> None:
        """Start the single engine thread exactly once.

        Called from several handler threads at the same instant, so the creation
        has to be guarded: two workers would each build an engine, and the second
        one would touch the first one's SQLite connection - the exact cross-thread
        error this design exists to avoid.
        """
        if self._worker is not None:
            return
        with self._worker_lock:
            if self._worker is not None:
                return
            worker = threading.Thread(target=self._run_jobs, name="trick-engine", daemon=True)
            worker.start()
            self._worker = worker

    def _run_jobs(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            function, box, done = job
            try:
                box["result"] = function()
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                box["error"] = exc
            finally:
                done.set()

    def _submit(self, function: Callable[[], Any], *, timeout: float = 300.0) -> Any:
        self._ensure_worker()
        box: dict[str, Any] = {}
        done = threading.Event()
        self._jobs.put((function, box, done))
        if not done.wait(timeout):
            raise Contention("the engine did not answer in time")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def engine(self) -> SearchEngine:
        if self._engine is None:
            self._engine = self._factory()
        return self._engine

    def close(self) -> None:
        self._jobs.put(None)
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(timeout=5.0)
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.store.close()
            except Exception:  # noqa: BLE001 - closing must not raise
                pass

    # -- methods -----------------------------------------------------------
    def search(self, query: str, *, limit: int = 8, live: bool | None = None) -> SearchOutcome:
        text = " ".join(str(query or "").split())
        if not text:
            raise ContractViolation("query is empty")
        if len(text) > 2000:
            raise ContractViolation("query is longer than 2000 characters")
        size = int(limit)
        if not 1 <= size <= 26:
            raise ContractViolation(f"limit must be between 1 and 26, got {size}")

        def job() -> SearchOutcome:
            return self.engine().search(text, limit=size, use_live=live)

        return self._submit(job)

    def health(self) -> dict[str, Any]:
        def job() -> dict[str, Any]:
            return dict(self.engine().health())

        info = self._submit(job)
        info["service"] = {
            "uptime_s": round(time.time() - self._started_at, 1),
            "requests": self.requests,
            "failures": self.failures,
            "pid": os.getpid(),
        }
        return info

    def dispatch(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Route one protocol call. Unknown methods are a contract violation."""
        if method == "search":
            outcome = self.search(
                params.get("query", ""),
                limit=int(params.get("limit", 8)),
                live=_tri_state(params.get("live")),
            )
            return outcome.as_dict()
        if method == "health":
            return self.health()
        if method == "ping":
            return {"pong": True, "pid": os.getpid()}
        raise ContractViolation(f"unknown method {method!r}")


def _tri_state(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def handle_request(service: Service, raw: bytes) -> bytes | None:
    """One NDJSON request in, one response line out (``None`` for a notification)."""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    started = time.monotonic()
    try:
        request = json.loads(text)
    except ValueError as exc:
        return _encode(None, error={"s_code": "S8", "message": f"invalid JSON: {exc}"})
    if not isinstance(request, dict):
        return _encode(None, error={"s_code": "S8", "message": "request must be an object"})
    request_id = request.get("id")
    method = str(request.get("method") or "")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return _encode(request_id, error={"s_code": "S8", "message": "params must be an object"})
    if not method:
        return _encode(request_id, error={"s_code": "S8", "message": "method is required"})

    service.requests += 1
    try:
        result = service.dispatch(method, params)
    except TrickError as exc:
        service.failures += 1
        return _encode(request_id, error=error_payload(exc))
    except Exception as exc:  # noqa: BLE001 - a crash must become one failed request
        service.failures += 1
        return _encode(request_id, error=error_payload(exc))
    result = dict(result)
    result.setdefault("elapsed_ms", round((time.monotonic() - started) * 1000.0, 1))
    return _encode(request_id, result=result)


def _encode(request_id: Any, *, result: dict | None = None, error: dict | None = None) -> bytes:
    message: dict[str, Any] = {"id": request_id}
    if error is None:
        message["ok"] = True
        message["result"] = result or {}
    else:
        message["ok"] = False
        message["error"] = error
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# unix socket server
# ---------------------------------------------------------------------------
class UnixServiceServer:
    """Threaded line server on a unix socket, with an owner-only socket file."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        service: Service,
        *,
        max_clients: int = DEFAULT_MAX_CLIENTS,
        max_line: int = MAX_LINE_BYTES,
    ) -> None:
        self.path = Path(socket_path)
        self.service = service
        self.max_clients = max(1, int(max_clients))
        self.max_line = max(1024, int(max_line))
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(self.max_clients)
        self.connections = 0

    # -- lifecycle ---------------------------------------------------------
    def bind(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._clear_stale()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.path))
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE:
                raise Contention(f"another trick service is listening on {self.path}") from exc
            raise StorageIO(f"cannot bind {self.path}: {exc}") from exc
        os.chmod(self.path, 0o600)
        sock.listen(16)
        sock.settimeout(0.2)
        self._sock = sock

    def _clear_stale(self) -> None:
        """Remove a leftover socket file only when nothing is listening on it."""
        if not self.path.exists():
            return
        mode = self.path.lstat().st_mode
        if not stat.S_ISSOCK(mode):
            raise ConfigError(f"{self.path} exists and is not a socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(str(self.path))
        except OSError:
            try:
                self.path.unlink()
            except OSError as exc:
                raise StorageIO(f"cannot remove stale socket {self.path}: {exc}") from exc
        else:
            raise Contention(f"another trick service is listening on {self.path}")
        finally:
            probe.close()

    def start(self) -> None:
        if self._sock is None:
            self.bind()
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, name="trick-accept", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            if self.path.exists() and stat.S_ISSOCK(self.path.lstat().st_mode):
                self.path.unlink()
        except OSError:
            pass

    def serve_forever(self) -> None:
        """Blocking form used by ``trick serve``."""
        if self._sock is None:
            self.bind()
        self._accept_loop()

    # -- internals ---------------------------------------------------------
    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(BACKOFF_S)
                continue
            if not self._slots.acquire(blocking=False):
                _reject(conn, "S7", "server is at its connection limit")
                continue
            self.connections += 1
            threading.Thread(target=self._serve_conn, args=(conn,), name="trick-conn", daemon=True).start()

    def _serve_conn(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(120.0)
            stream = conn.makefile("rwb")
            while not self._stop.is_set():
                line = _read_line(stream, self.max_line)
                if line is None:
                    break
                if line is TOO_LONG:
                    stream.write(_encode(None, error={"s_code": "S8", "message": "request line too long"}))
                    stream.flush()
                    break
                if not line.strip():
                    continue
                response = handle_request(self.service, line)
                if response is not None:
                    stream.write(response)
                    stream.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            self._slots.release()
            try:
                conn.close()
            except OSError:
                pass


TOO_LONG = b"\x00__too_long__"


def _read_line(stream, limit: int) -> bytes | None:
    """Read one newline-terminated line without buffering more than ``limit``."""
    buffer = bytearray()
    while True:
        chunk = stream.read(1)
        if not chunk:
            return bytes(buffer) if buffer else None
        buffer += chunk
        if chunk == b"\n":
            return bytes(buffer)
        if len(buffer) > limit:
            # Drain the rest of the line so the stream stays usable, then report.
            while chunk and chunk != b"\n":
                chunk = stream.read(1)
            return TOO_LONG


def _reject(conn: socket.socket, code: str, message: str) -> None:
    try:
        conn.sendall(_encode(None, error={"s_code": code, "message": message}))
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# unix socket client
# ---------------------------------------------------------------------------
class ServiceClient:
    """Blocking client for the core service, safe to share between threads."""

    def __init__(self, socket_path: str | os.PathLike[str], *, timeout: float = 120.0) -> None:
        self.path = Path(socket_path)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._stream = None
        self._lock = threading.Lock()
        self._next_id = 0

    def connect(self) -> None:
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.path))
        except FileNotFoundError as exc:
            sock.close()
            raise ConfigError(
                f"trick service is not running ({self.path} missing); start it with: trick serve"
            ) from exc
        except ConnectionRefusedError as exc:
            sock.close()
            raise Contention(f"trick service is not accepting connections on {self.path}") from exc
        except OSError as exc:
            sock.close()
            raise StorageIO(f"cannot connect to {self.path}: {exc}") from exc
        self._sock = sock
        self._stream = sock.makefile("rwb")

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            for attempt in (0, 1):
                try:
                    return self._call_once(method, params or {})
                except (BrokenPipeError, ConnectionResetError):
                    self.close()
                    if attempt:
                        raise Contention("trick service dropped the connection")
                    self.connect()
            raise Contention("trick service dropped the connection")

    def _call_once(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.connect()
        assert self._stream is not None
        self._next_id += 1
        request_id = self._next_id
        line = json.dumps({"id": request_id, "method": method, "params": dict(params)}, ensure_ascii=False)
        try:
            self._stream.write(line.encode("utf-8") + b"\n")
            self._stream.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close()
            raise
        raw = self._stream.readline()
        if not raw:
            self.close()
            raise Contention("trick service closed the connection")
        try:
            message = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise StorageIO(f"service sent invalid JSON: {exc}") from exc
        if not message.get("ok"):
            error = message.get("error") or {}
            raise _error_from(error)
        return dict(message.get("result") or {})

    # -- Backend-ish convenience ------------------------------------------
    def search(self, query: str, *, limit: int = 8, live: bool | None = None) -> SearchOutcome:
        payload = self.call("search", {"query": query, "limit": limit, "live": live})
        return SearchOutcome.from_dict(payload)

    def health(self) -> dict[str, Any]:
        return self.call("health")


def _error_from(error: Mapping[str, Any]) -> TrickError:
    code = str(error.get("s_code") or "S0")
    message = str(error.get("message") or "service error")
    mapping = {
        "S9": ConfigError,
        "S7": Contention,
        "S6": StorageIO,
        "S8": ContractViolation,
    }
    cls = mapping.get(code)
    if cls is not None:
        return cls(message)
    exc = TrickError(message)
    exc.s_code = code  # type: ignore[misc]
    return exc


# ---------------------------------------------------------------------------
# token handling for the HTTP bridge
# ---------------------------------------------------------------------------
def load_or_create_token(path: str | os.PathLike[str]) -> str:
    """A 32-byte hex token in a 0600 file, created once and reused."""
    token_path = Path(path)
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            mode = token_path.stat().st_mode
            if mode & 0o077:
                raise ConfigError(f"bridge token {token_path} is readable by others; run: chmod 600 {token_path}")
            return token
        raise ConfigError(f"bridge token file {token_path} is malformed; delete it and restart")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(token_path.parent, 0o700)
    token = os.urandom(32).hex()
    handle = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
    return token


__all__ = [
    "Backend",
    "Service",
    "ServiceClient",
    "UnixServiceServer",
    "error_payload",
    "handle_request",
    "load_or_create_token",
]
