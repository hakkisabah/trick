"""Remote memory store: the part that makes a $5/month box enough.

The split this module encodes is the whole commercial argument. A store holds the
*sealed* archive, byte for byte, and nothing else. It cannot read a decision, a
setting or a passphrase, because it never receives a key; if the box is seized or
its disk is published, the contents are an opaque blob. That is also why the box
can be the cheapest one on the market: it stores 400 KB, serves 50 requests a
day, and needs no model, no index and no CPU.

Two halves:

* ``VaultStore`` / ``VaultServer`` - what runs on the remote host. Content
  addressed (a file is named by the SHA-256 of its own bytes), atomic (tmp +
  fsync + rename), quota-checked before a byte is written, bearer-token
  authenticated, and TLS-only unless the caller is loopback and says so.
* ``RemoteVault`` - what runs next to the user. It uploads, then reads the object
  back and compares the hash before it reports success. "I sent it" is not a
  backup; "I read it back and it is the same 380906 bytes" is.

Quota units, stated precisely because they are easy to fake: ``max_bytes`` and
``max_objects`` are enforced by the server. ``max_rows`` is enforced by the
client, because the server cannot count rows inside a payload it cannot decrypt.
The server records the client-declared row count for display only, and the docs
say so instead of implying an enforcement that does not exist.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError, ContractViolation, StorageIO

DEFAULT_PORT = 8790
MAX_OBJECT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_OBJECTS = 200
DEFAULT_MAX_ROWS = 50_000
TOKEN_BYTES = 32
TOKEN_MIN_CHARS = 32
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
AUTH_HEADER = "Authorization"
INDEX_NAME = "index.json"
OBJECTS_DIR = "objects"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Account:
    """One token, one directory, one quota."""

    name: str
    token_sha256: str
    max_bytes: int = DEFAULT_MAX_BYTES
    max_objects: int = DEFAULT_MAX_OBJECTS
    max_rows: int = DEFAULT_MAX_ROWS

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "token_sha256": self.token_sha256,
            "max_bytes": self.max_bytes,
            "max_objects": self.max_objects,
            "max_rows": self.max_rows,
        }


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """32 random bytes, url-safe: never a passphrase, never guessable."""
    import base64

    return base64.urlsafe_b64encode(os.urandom(TOKEN_BYTES)).decode("ascii").rstrip("=")


def load_accounts(path: Path) -> list[Account]:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"no accounts file at {path}")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ConfigError(
            f"{path} is readable by group or others (mode {mode:o}); chmod 600 it, "
            "it holds token digests"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError(f"{path} is not JSON: {exc}") from exc
    entries = raw.get("accounts") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path} holds no accounts")
    accounts = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("token_sha256"):
            raise ConfigError(f"{path} has an account without a name or a token digest")
        digest = str(entry["token_sha256"])
        if not SHA_RE.match(digest):
            raise ConfigError(f"account {entry['name']!r}: token_sha256 must be 64 hex characters")
        accounts.append(
            Account(
                name=str(entry["name"]),
                token_sha256=digest,
                max_bytes=int(entry.get("max_bytes", DEFAULT_MAX_BYTES)),
                max_objects=int(entry.get("max_objects", DEFAULT_MAX_OBJECTS)),
                max_rows=int(entry.get("max_rows", DEFAULT_MAX_ROWS)),
            )
        )
    return accounts


def write_accounts(path: Path, accounts: list[Account]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"accounts": [a.as_dict() for a in accounts]}, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def init_store(root: Path, name: str = "owner") -> dict[str, Any]:
    """Create a store root, one account, and print the token exactly once."""
    root = Path(root)
    (root / OBJECTS_DIR).mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    accounts_path = root / "accounts.json"
    if accounts_path.exists():
        raise ConfigError(f"{accounts_path} already exists; refusing to overwrite an account list")
    token = new_token()
    accounts = [Account(name=name, token_sha256=token_digest(token))]
    write_accounts(accounts_path, accounts)
    return {
        "root": str(root),
        "accounts": str(accounts_path),
        "account": name,
        "token": token,
        "note": "this token is shown once and is not stored in clear text anywhere",
    }


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
class VaultStore:
    """A directory of opaque objects, one subdirectory per account."""

    def __init__(self, root: Path, accounts: list[Account]) -> None:
        self.root = Path(root)
        self.accounts = {a.name: a for a in accounts}
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / OBJECTS_DIR).mkdir(parents=True, exist_ok=True)
        for account in accounts:
            self._dir(account).mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------
    def _dir(self, account: Account) -> Path:
        # The account name is validated on load; still, never let it escape root.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", account.name)
        if safe != account.name:
            raise ConfigError(f"account name {account.name!r} must be [A-Za-z0-9._-]")
        return self.root / OBJECTS_DIR / safe

    def _index_path(self, account: Account) -> Path:
        return self._dir(account) / INDEX_NAME

    def _read_index(self, account: Account) -> dict[str, Any]:
        path = self._index_path(account)
        if not path.exists():
            return {"objects": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise StorageIO(f"the index for {account.name} is not JSON: {exc}") from exc
        if not isinstance(raw.get("objects"), dict):
            raise StorageIO(f"the index for {account.name} has no objects map")
        return raw

    def _write_index(self, account: Account, index: dict[str, Any]) -> None:
        path = self._index_path(account)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)

    # -- auth --------------------------------------------------------------
    def authenticate(self, token: str) -> Account | None:
        """Constant-time over every account, so a wrong token reveals nothing."""
        if not token:
            return None
        digest = token_digest(token)
        found = None
        for account in self.accounts.values():
            if hmac.compare_digest(digest, account.token_sha256):
                found = account
        return found

    # -- operations --------------------------------------------------------
    def usage(self, account: Account) -> dict[str, Any]:
        with self._lock:
            index = self._read_index(account)
            objects = index["objects"]
            used = sum(int(v.get("bytes", 0)) for v in objects.values())
            rows = sum(int(v.get("rows", 0)) for v in objects.values())
        return {
            "account": account.name,
            "objects": len(objects),
            "bytes": used,
            "declared_rows": rows,
            "max_bytes": account.max_bytes,
            "max_objects": account.max_objects,
            "max_rows": account.max_rows,
            "rows_unit": "client-declared; the store cannot count rows inside ciphertext",
        }

    def put(self, account: Account, sha: str, blob: bytes, rows: int, created_at: int) -> dict[str, Any]:
        if not SHA_RE.match(sha):
            raise ContractViolation(f"{sha!r} is not a sha256 hex digest")
        if not blob:
            raise ContractViolation("empty object")
        if len(blob) > MAX_OBJECT_BYTES:
            raise ContractViolation(f"object larger than {MAX_OBJECT_BYTES} bytes")
        actual = hashlib.sha256(blob).hexdigest()
        if actual != sha:
            raise ContractViolation(f"hash mismatch: the body hashes to {actual}, not {sha}")
        with self._lock:
            index = self._read_index(account)
            objects = index["objects"]
            existing = objects.get(sha)
            if existing and int(existing.get("bytes", -1)) == len(blob):
                # Content addressed: the same bytes are already there.
                return {**self._describe(sha, existing), "already_stored": True}
            used = sum(int(v.get("bytes", 0)) for v in objects.values())
            if used + len(blob) > account.max_bytes:
                raise StorageIO(
                    f"quota: {used} + {len(blob)} bytes exceeds {account.max_bytes}; "
                    "delete an old snapshot first"
                )
            if len(objects) + 1 > account.max_objects:
                raise StorageIO(
                    f"quota: object count would exceed {account.max_objects}; "
                    "delete an old snapshot first"
                )
            target = self._dir(account) / f"{sha}.trk"
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(blob)
            os.chmod(tmp, 0o600)
            with open(tmp, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp, target)
            _fsync_dir(target.parent)
            entry = {
                "bytes": len(blob),
                "rows": int(max(rows, 0)),
                "created_at": int(created_at or time.time()),
                "stored_at": int(time.time()),
            }
            objects[sha] = entry
            self._write_index(account, index)
            return {**self._describe(sha, entry), "already_stored": False}

    def get(self, account: Account, sha: str) -> bytes:
        if not SHA_RE.match(sha):
            raise ContractViolation(f"{sha!r} is not a sha256 hex digest")
        with self._lock:
            index = self._read_index(account)
            if sha not in index["objects"]:
                raise ConfigError(f"no such snapshot: {sha}")
            path = self._dir(account) / f"{sha}.trk"
        if not path.exists():
            raise StorageIO(f"index lists {sha} but the file is gone")
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != sha:
            raise StorageIO(f"the stored object {sha} no longer hashes to its name")
        return blob

    def delete(self, account: Account, sha: str) -> dict[str, Any]:
        if not SHA_RE.match(sha):
            raise ContractViolation(f"{sha!r} is not a sha256 hex digest")
        with self._lock:
            index = self._read_index(account)
            entry = index["objects"].pop(sha, None)
            if entry is None:
                raise ConfigError(f"no such snapshot: {sha}")
            path = self._dir(account) / f"{sha}.trk"
            if path.exists():
                path.unlink()
            self._write_index(account, index)
        return {"deleted": sha, "bytes": int(entry.get("bytes", 0))}

    def listing(self, account: Account) -> list[dict[str, Any]]:
        with self._lock:
            index = self._read_index(account)
            rows = [self._describe(sha, entry) for sha, entry in index["objects"].items()]
        return sorted(rows, key=lambda row: int(row.get("created_at") or 0), reverse=True)

    @staticmethod
    def _describe(sha: str, entry: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "sha256": sha,
            "bytes": int(entry.get("bytes", 0)),
            "rows": int(entry.get("rows", 0)),
            "created_at": int(entry.get("created_at", 0)),
            "stored_at": int(entry.get("stored_at", 0)),
        }


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------
def server_ssl_context(cert: Path, key: Path) -> ssl.SSLContext:
    cert, key = Path(cert), Path(key)
    if not cert.exists() or not key.exists():
        raise ConfigError(f"TLS needs both a certificate and a key ({cert}, {key})")
    mode = key.stat().st_mode & 0o777
    if mode & 0o077:
        raise ConfigError(f"{key} is readable by group or others (mode {mode:o}); chmod 600 it")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_COMPRESSION
    try:
        context.load_cert_chain(str(cert), str(key))
    except ssl.SSLError as exc:
        raise ConfigError(f"cannot load the certificate pair: {exc}") from exc
    return context


STATUS_FOR_CODE = {
    "S6": 507,
    "S8": 400,
    "S9": 401,
    "S1": 404,
}


class VaultServer:
    """A ThreadingHTTPServer that speaks four routes and nothing else."""

    def __init__(
        self,
        store: VaultStore,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        tls: ssl.SSLContext | None = None,
        allow_plaintext: bool = False,
        expose_public: bool = False,
    ) -> None:
        host = (host or "").strip()
        wildcard = host in ("0.0.0.0", "", "::", "*")
        if wildcard and not expose_public:
            # A store with no user accounts is one leaked token away from being
            # someone else's disk; a wildcard bind is refused, not warned about.
            raise ConfigError(
                "refusing a wildcard bind; name the interface, or pass --expose-public "
                "for the one deployment that needs it (a remote store behind a firewall)"
            )
        if wildcard and tls is None:
            raise ConfigError(
                "a wildcard bind requires TLS: publishing an unauthenticated store to a "
                "network in clear text is never what someone meant"
            )
        if tls is None and not allow_plaintext:
            raise ConfigError(
                "the store only speaks TLS; pass --cert and --key, or --allow-plaintext "
                "for a loopback-only test"
            )
        if allow_plaintext and host not in LOOPBACK_HOSTS:
            raise ConfigError(
                f"plaintext is only allowed on loopback, got {host!r}; ciphertext in transit "
                "is the whole point of a remote backup"
            )
        self.store = store
        self.host = host
        self.port = int(port)
        self.tls = tls
        self._httpd: ThreadingHTTPServer | None = None
        self.requests = 0
        self.rejected = 0

    def start(self) -> int:
        handler = _make_handler(self)
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        httpd.daemon_threads = True
        if self.tls is not None:
            httpd.socket = self.tls.wrap_socket(httpd.socket, server_side=True)
        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, name="trick-vault", daemon=True).start()
        return int(httpd.server_address[1])

    def serve_forever(self) -> None:
        handler = _make_handler(self)
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        httpd.daemon_threads = True
        if self.tls is not None:
            httpd.socket = self.tls.wrap_socket(httpd.socket, server_side=True)
        self._httpd = httpd
        httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def authenticate(self, header: str | None) -> Account | None:
        if not header or not header.startswith("Bearer "):
            return None
        return self.store.authenticate(header[len("Bearer ") :].strip())


def _make_handler(server: VaultServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "trick-vault/1.0"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            line = format % args
            print(f"vault: {line}", file=sys.stderr, flush=True)

        def handle_one_request(self) -> None:
            """A client that hangs up is normal traffic, not a server error.

            A store on a public address sees half-open connections and port
            scanners all day. Without this, every one of them prints a traceback
            and costs a thread; the connection is closed quietly instead.
            """
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, ssl.SSLError):
                self.close_connection = True

        # -- helpers -----------------------------------------------------
        def _send(self, status: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if server.tls is not None:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, blob: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(blob)

        def _fail(self, status: int, code: str, message: str) -> None:
            self._send(status, {"ok": False, "error": {"s_code": code, "message": message}})

        def _account(self) -> Account | None:
            account = server.authenticate(self.headers.get(AUTH_HEADER))
            if account is None:
                server.rejected += 1
                self._fail(401, "S9", "missing or invalid bearer token")
            return account

        # -- routes ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/v1/ping":
                self._send(200, {"ok": True, "service": "trick-vault"})
                return
            account = self._account()
            if account is None:
                return
            server.requests += 1
            try:
                if path == "/v1/usage":
                    self._send(200, {"ok": True, "result": server.store.usage(account)})
                    return
                if path == "/v1/objects":
                    self._send(200, {"ok": True, "result": server.store.listing(account)})
                    return
                prefix = "/v1/objects/"
                if path.startswith(prefix):
                    blob = server.store.get(account, path[len(prefix) :])
                    self._send_bytes(blob)
                    return
            except ContractViolation as exc:
                self._fail(400, "S8", str(exc))
                return
            except StorageIO as exc:
                self._fail(507, "S6", str(exc))
                return
            except ConfigError as exc:
                self._fail(404, "S1", str(exc))
                return
            self._fail(404, "S8", f"no route {path}")

        def do_PUT(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
            account = self._account()
            if account is None:
                return
            prefix = "/v1/objects/"
            if not path.startswith(prefix):
                self._fail(404, "S8", f"no route {path}")
                return
            sha = path[len(prefix) :]
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._fail(400, "S8", "invalid Content-Length")
                return
            if length <= 0:
                self._fail(400, "S8", "empty body")
                return
            if length > MAX_OBJECT_BYTES:
                self._fail(413, "S8", f"object larger than {MAX_OBJECT_BYTES} bytes")
                return
            blob = self.rfile.read(length)
            try:
                rows = int(self.headers.get("X-Trick-Rows") or 0)
                created = int(self.headers.get("X-Trick-Created") or 0)
            except (TypeError, ValueError):
                self._fail(400, "S8", "X-Trick-Rows and X-Trick-Created must be integers")
                return
            server.requests += 1
            try:
                result = server.store.put(account, sha, blob, rows, created)
            except ContractViolation as exc:
                self._fail(400, "S8", str(exc))
                return
            except StorageIO as exc:
                self._fail(507, "S6", str(exc))
                return
            self._send(200, {"ok": True, "result": result})

        def do_DELETE(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
            account = self._account()
            if account is None:
                return
            prefix = "/v1/objects/"
            if not path.startswith(prefix):
                self._fail(404, "S8", f"no route {path}")
                return
            server.requests += 1
            try:
                result = server.store.delete(account, path[len(prefix) :])
            except ContractViolation as exc:
                self._fail(400, "S8", str(exc))
                return
            except ConfigError as exc:
                self._fail(404, "S1", str(exc))
                return
            self._send(200, {"ok": True, "result": result})

    return Handler


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------
class RemoteVault:
    """Upload and verify. Every write is read back before it is called a backup."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        fingerprint: str | None = None,
        ca_file: str | Path | None = None,
        timeout: float = 30.0,
        allow_plaintext: bool = False,
    ) -> None:
        parts = urllib.parse.urlsplit(base_url.strip())
        if parts.scheme not in ("https", "http"):
            raise ConfigError(f"remote url must start with https:// (got {base_url!r})")
        if not parts.hostname:
            raise ConfigError(f"remote url has no host: {base_url!r}")
        if parts.scheme == "http":
            if parts.hostname not in LOOPBACK_HOSTS or not allow_plaintext:
                raise ConfigError(
                    "refusing plain http: the archive is encrypted, but a plain channel still "
                    "leaks how often you back up and lets anyone rewrite the bytes; use https, "
                    "or --allow-plaintext on loopback for a test"
                )
        if not token or len(token) < TOKEN_MIN_CHARS:
            raise ConfigError(f"the store token must be at least {TOKEN_MIN_CHARS} characters")
        self.url = base_url.rstrip("/")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.token = token
        self.fingerprint = (fingerprint or "").replace(":", "").lower() or None
        if self.fingerprint and not SHA_RE.match(self.fingerprint):
            raise ConfigError("the certificate fingerprint must be 64 hex characters (sha256 of the DER)")
        self.ca_file = str(ca_file) if ca_file else None
        self.timeout = float(timeout)
        self.allow_plaintext = allow_plaintext

    # -- transport ---------------------------------------------------------
    def _connect(self):
        if self.scheme == "http":
            return HTTPConnection(self.host, self.port, timeout=self.timeout)
        context = ssl.create_default_context(cafile=self.ca_file)
        if self.fingerprint and not self.ca_file:
            # Pinned by fingerprint: a self-signed certificate is fine, because
            # the identity is the hash, not a name in a public CA's book.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        conn = HTTPSConnection(self.host, self.port, timeout=self.timeout, context=context)
        return conn

    def _verify_peer(self, conn) -> None:
        if not self.fingerprint or self.scheme != "https":
            return
        sock = getattr(conn, "sock", None)
        der = sock.getpeercert(binary_form=True) if sock is not None else None
        if not der:
            raise ConfigError("the server presented no certificate; refusing to trust it")
        actual = hashlib.sha256(der).hexdigest()
        if not hmac.compare_digest(actual, self.fingerprint):
            raise ConfigError(
                f"certificate fingerprint mismatch: expected {self.fingerprint}, got {actual}; "
                "a store that cannot prove who it is is not a backup target"
            )

    def _request(self, method: str, path: str, body: bytes | None = None, headers: Mapping[str, str] | None = None):
        conn = self._connect()
        try:
            conn.connect()
            self._verify_peer(conn)
            payload = body if body is not None else b""
            merged = {
                "Authorization": f"Bearer {self.token}",
                "Content-Length": str(len(payload)),
                "User-Agent": "trick-vault/1.0",
            }
            if headers:
                merged.update(headers)
            conn.request(method, f"{self.prefix}{path}", body=payload, headers=merged)
            response = conn.getresponse()
            data = response.read()
            status = response.status
        finally:
            conn.close()
        if status >= 400:
            try:
                error = json.loads(data.decode("utf-8")).get("error", {})
            except (ValueError, UnicodeDecodeError):
                error = {}
            raise ConfigError(
                f"{method} {path} -> HTTP {status}: {error.get('message') or data[:200]!r}"
            )
        return data

    # -- operations --------------------------------------------------------
    def ping(self) -> dict[str, Any]:
        raw = self._request("GET", "/v1/ping")
        return json.loads(raw.decode("utf-8"))

    def usage(self) -> dict[str, Any]:
        return json.loads(self._request("GET", "/v1/usage").decode("utf-8"))["result"]

    def listing(self) -> list[dict[str, Any]]:
        return json.loads(self._request("GET", "/v1/objects").decode("utf-8"))["result"]

    def get(self, sha: str) -> bytes:
        blob = self._request("GET", f"/v1/objects/{sha}")
        if hashlib.sha256(blob).hexdigest() != sha:
            raise StorageIO(f"the bytes returned for {sha} do not hash to it")
        return blob

    def delete(self, sha: str) -> dict[str, Any]:
        return json.loads(self._request("DELETE", f"/v1/objects/{sha}").decode("utf-8"))["result"]

    def put(self, blob: bytes, *, rows: int = 0, created_at: int = 0) -> dict[str, Any]:
        from .vault import MAGIC

        if not blob.startswith(MAGIC):
            raise ConfigError(
                "this is not a Trick archive; export one with: trick backup export memory.trk"
            )
        sha = hashlib.sha256(blob).hexdigest()
        raw = self._request(
            "PUT",
            f"/v1/objects/{sha}",
            blob,
            {"Content-Type": "application/octet-stream", "X-Trick-Rows": str(int(rows)), "X-Trick-Created": str(int(created_at))},
        )
        stored = json.loads(raw.decode("utf-8"))["result"]
        # Read it back. An upload that is never verified is a hope.
        back = self.get(sha)
        if back != blob:
            raise StorageIO(
                f"the store returned {len(back)} bytes that differ from the {len(blob)} uploaded"
            )
        return {
            "sha256": sha,
            "bytes": len(blob),
            "rows": int(rows),
            "verified": True,
            "already_stored": bool(stored.get("already_stored")),
            "stored_at": stored.get("stored_at"),
        }

    def pull(self, sha: str, out: Path, passphrase: str | None = None) -> dict[str, Any]:
        """Download, verify the hash, and optionally prove it opens."""
        blob = self.get(sha)
        out = Path(out)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
        result: dict[str, Any] = {"path": str(out), "sha256": sha, "bytes": len(blob)}
        if passphrase is not None:
            from .vault import describe, read_archive

            result["contents"] = describe(read_archive(out, passphrase))
        return result


def token_for_home(home: Path) -> Path:
    return Path(home) / "vault.token"


def read_token(home: Path) -> str:
    """Token from the environment, else from a 0600 file next to the state."""
    env = os.environ.get("TRICK_VAULT_TOKEN")
    if env:
        return env.strip()
    path = token_for_home(home)
    if not path.exists():
        raise ConfigError(
            f"no store token: set TRICK_VAULT_TOKEN, or write it to {path} with mode 600"
        )
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ConfigError(f"{path} is readable by group or others (mode {mode:o}); chmod 600 it")
    return path.read_text(encoding="utf-8").strip()


__all__ = [
    "Account",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_OBJECTS",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_PORT",
    "MAX_OBJECT_BYTES",
    "RemoteVault",
    "VaultServer",
    "VaultStore",
    "init_store",
    "load_accounts",
    "new_token",
    "read_token",
    "server_ssl_context",
    "token_digest",
    "token_for_home",
    "write_accounts",
]
