"""Loopback HTTP bridge for hosts that cannot open a unix socket.

Open WebUI runs its tools inside a container, a browser extension has no file
system access, and neither can reach ``$TRICK_HOME/run/trick.sock``. This bridge
publishes the same method table over HTTP on 127.0.0.1 with a bearer token, and
forwards every call to the socket. It holds no ranking logic of its own.

Security decisions, each one deliberate:

* bound to 127.0.0.1 only - never 0.0.0.0, because the service has no user
  accounts and the machine may be on a shared network;
* a 32-byte token from a 0600 file, compared with ``hmac.compare_digest`` so a
  timing attack cannot guess it byte by byte;
* no token in any log line, and no token echoed in an error body;
* request bodies are capped before parsing (413), so a huge POST cannot exhaust
  the host;
* CORS is off unless ``TRICK_CORS_ORIGINS`` names explicit origins. A wildcard
  would let any page in the user's browser spend their laya credits.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from .errors import ConfigError, TrickError
from .render import explain
from .service import Backend, error_payload

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# 1 MiB: a request can carry a window of a long conversation (see
# service.MAX_LINE_BYTES). The cap is still enforced before parsing so a single
# POST cannot exhaust the host, and both transports agree on the same number.
MAX_BODY_BYTES = 1024 * 1024
# How much of a refused body is read and thrown away before the 413 is sent.
MAX_DRAIN_BYTES = 2 * 1024 * 1024
AUTH_HEADER = "Authorization"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
WILDCARD_HOSTS = ("0.0.0.0", "", "::", "*")


def validate_host(host: str, *, allow_network: bool = False) -> str:
    """Accept loopback by default, one explicit local address when asked.

    A wildcard bind is never allowed: the service has no user accounts, and a
    machine on a shared network would then be one token away from spending the
    owner's laya credits. A container cannot reach ``127.0.0.1`` on the host, so
    the docker bridge address (``172.17.0.1``) is a legitimate choice - it is
    reachable from containers and not from the LAN. Any other address has to be
    requested explicitly, which is what ``allow_network`` records in the log.
    """
    host = (host or "").strip()
    if host in WILDCARD_HOSTS:
        raise ConfigError(
            f"bridge refuses a wildcard bind, got {host!r}; the bridge is loopback-only "
            "unless --expose-network names one specific local address"
        )
    if host in LOOPBACK_HOSTS:
        return host
    if not allow_network:
        raise ConfigError(
            f"bridge host must be loopback, got {host!r}; pass --http-host with "
            "--expose-network only if this address is a local interface"
        )
    return host

STATUS_FOR_CODE = {
    "S1": 503,
    "S2": 503,
    "S3": 503,
    "S4": 502,
    "S5": 503,
    "S6": 507,
    "S7": 429,
    "S8": 400,
    "S9": 500,
    "S0": 500,
}


class BridgeServer:
    """A ThreadingHTTPServer whose handler closes over one backend and one token."""

    def __init__(
        self,
        backend: Backend,
        token: str,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        cors_origins: tuple[str, ...] = (),
        allow_network: bool = False,
    ) -> None:
        if not token or len(token) < 32:
            raise ConfigError("bridge token must be at least 32 characters")
        host = validate_host(host, allow_network=allow_network)
        self.backend = backend
        self.token = token
        self.cors_origins = tuple(o for o in cors_origins if o)
        self._httpd: ThreadingHTTPServer | None = None
        self._host = host
        self._port = int(port)
        self.requests = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> int:
        """Bind and serve in a background thread. Returns the actual port."""
        handler = _make_handler(self)
        httpd = ThreadingHTTPServer((self._host, self._port), handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        import threading

        threading.Thread(target=httpd.serve_forever, name="trick-bridge", daemon=True).start()
        return int(httpd.server_address[1])

    def serve_forever(self) -> None:
        handler = _make_handler(self)
        httpd = ThreadingHTTPServer((self._host, self._port), handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self._port

    # -- helpers used by the handler --------------------------------------
    def authorised(self, header: str | None) -> bool:
        if not header:
            return False
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix) :].strip(), self.token)

    def dispatch(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == "search":
            outcome = self.backend.search(
                str(params.get("query") or ""),
                limit=int(params.get("limit", 8) or 8),
                live=params.get("live"),
                messages=params.get("messages"),
                scope=str(params.get("scope") or "sources"),
            )
            payload = outcome.as_dict()
            payload["degraded_text"] = [explain(code) for code in outcome.degraded]
            return payload
        if method == "health":
            return self.backend.health()
        if method == "ping":
            return {"pong": True}
        raise ConfigError(f"unknown method {method!r}")


def _make_handler(bridge: BridgeServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "trick-bridge/0.3"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # -- logging: never the token, never the body --------------------
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            line = format % args
            if bridge.token:
                line = line.replace(bridge.token, "***")
            print(f"bridge: {line}", file=sys.stderr, flush=True)

        # -- responses ---------------------------------------------------
        def _send(self, status: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin")
            if origin and origin in bridge.cors_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, status: int, code: str, message: str) -> None:
            self._send(status, {"ok": False, "error": {"s_code": code, "message": message}})

        def _unauthorised(self) -> None:
            self._fail(401, "S9", "missing or invalid bearer token")

        def _drain(self, length: int) -> None:
            """Read and discard at most ``MAX_DRAIN_BYTES`` of an announced body.

            Bounded on purpose: the point is to let a well-behaved client read
            the 413 rather than a connection reset, not to accept a flood.
            """
            remaining = min(length, MAX_DRAIN_BYTES)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)

        # -- methods -----------------------------------------------------
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            origin = self.headers.get("Origin")
            if origin and origin in bridge.cors_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in ("/v1/ping", "/ping"):
                self._send(200, {"ok": True, "service": "trick-bridge"})
                return
            if path not in ("/v1/health", "/health"):
                self._fail(404, "S8", f"no route {path}")
                return
            if not bridge.authorised(self.headers.get(AUTH_HEADER)):
                self._unauthorised()
                return
            self._call("health", {})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path not in ("/v1/search", "/search"):
                self._fail(404, "S8", f"no route {path}")
                return
            if not bridge.authorised(self.headers.get(AUTH_HEADER)):
                self._unauthorised()
                return
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or 0)
            except ValueError:
                self._fail(400, "S8", "invalid Content-Length")
                return
            if length > MAX_BODY_BYTES:
                # Refuse before parsing, but drain what the client already
                # announced (bounded) so it receives the 413 instead of a
                # broken pipe it cannot interpret.
                self._drain(length)
                self._fail(413, "S8", f"body larger than {MAX_BODY_BYTES} bytes")
                return
            body = self.rfile.read(length) if length else b""
            try:
                params = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError) as exc:
                self._fail(400, "S8", f"body is not JSON: {exc}")
                return
            if not isinstance(params, dict):
                self._fail(400, "S8", "body must be a JSON object")
                return
            self._call("search", params)

        def _call(self, method: str, params: Mapping[str, Any]) -> None:
            bridge.requests += 1
            try:
                result = bridge.dispatch(method, params)
            except TrickError as exc:
                payload = error_payload(exc)
                status = STATUS_FOR_CODE.get(exc.s_code, 500)
                self._send(status, {"ok": False, "error": payload})
                return
            except (TypeError, ValueError) as exc:
                self._fail(400, "S8", f"{type(exc).__name__}: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - one request, one error
                self._fail(500, "S0", f"{type(exc).__name__}: {exc}")
                return
            self._send(200, {"ok": True, "result": result})

    return Handler


def cors_from_env(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Parse ``TRICK_CORS_ORIGINS`` (comma separated). Empty means no CORS."""
    env = os.environ if environ is None else environ
    raw = env.get("TRICK_CORS_ORIGINS", "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


__all__ = [
    "AUTH_HEADER",
    "BridgeServer",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MAX_BODY_BYTES",
    "STATUS_FOR_CODE",
    "cors_from_env",
    "validate_host",
]
