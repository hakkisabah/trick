"""An S3-shaped server that *verifies* SigV4 instead of trusting it.

A signer tested against a server that ignores the signature proves nothing: it
would pass with a wrong key, a wrong scope or a wrong canonical request. This
server rebuilds the canonical request from the bytes it actually received and
compares the signature, which is what AWS does. A wrong secret therefore returns
403 SignatureDoesNotMatch here exactly as it would in Frankfurt.

It implements only what the store uses: PUT, GET, HEAD, DELETE, path-style and
virtual-host addressing, and an optional first-attempt 503 so the retry path can
be exercised without a flaky network.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ACCESS_KEY = "AKIATRICKTESTACCESS"
SECRET_KEY = "trick-test-secret-key-do-not-use"
REGION = "eu-central-1"
BUCKET = "trick-test"


class FakeS3:
    def __init__(self, *, addressing: str = "path", fail_first: int = 0, reject: str = "") -> None:
        self.objects: dict[str, bytes] = {}
        self.addressing = addressing
        self.requests: list[tuple[str, str]] = []
        self.fail_first = fail_first
        self.reject = reject
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep pytest output readable
                return

            def _body(self) -> bytes:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                return self.rfile.read(length) if length else b""

            def _key(self) -> str:
                path = urllib.parse.urlsplit(self.path).path.lstrip("/")
                if fake.addressing == "virtual":
                    return path
                bucket, _, key = path.partition("/")
                if bucket != BUCKET:
                    return ""
                return key

            def _verify(self, body: bytes) -> str:
                """Return '' when the request is authentic, else the S3 error code."""
                auth = self.headers.get("Authorization") or ""
                if not auth.startswith("AWS4-HMAC-SHA256 "):
                    return "AccessDenied"
                fields = {}
                for piece in auth[len("AWS4-HMAC-SHA256 ") :].split(","):
                    name, _, value = piece.strip().partition("=")
                    fields[name] = value
                credential = fields.get("Credential", "")
                signed_headers = fields.get("SignedHeaders", "")
                signature = fields.get("Signature", "")
                if not credential or not signed_headers or not signature:
                    return "AuthorizationHeaderMalformed"
                access_key, _, scope = credential.partition("/")
                if access_key != ACCESS_KEY:
                    return "InvalidAccessKeyId"
                if scope != f"{self.headers.get('x-amz-date', '')[:8]}/{REGION}/s3/aws4_request":
                    return "SignatureDoesNotMatch"
                payload_sha = self.headers.get("x-amz-content-sha256") or ""
                if payload_sha != hashlib.sha256(body).hexdigest():
                    return "XAmzContentSHA256Mismatch"
                names = signed_headers.split(";")
                canonical_headers = ""
                for name in names:
                    value = (self.headers.get(name) or "").strip()
                    value = " ".join(value.split())
                    canonical_headers += f"{name}:{value}\n"
                canonical_request = "\n".join(
                    [
                        self.command,
                        urllib.parse.urlsplit(self.path).path,
                        "",
                        canonical_headers,
                        signed_headers,
                        payload_sha,
                    ]
                )
                string_to_sign = "\n".join(
                    [
                        "AWS4-HMAC-SHA256",
                        self.headers.get("x-amz-date", ""),
                        scope,
                        hashlib.sha256(canonical_request.encode()).hexdigest(),
                    ]
                )
                k_date = hmac.new(("AWS4" + SECRET_KEY).encode(), self.headers["x-amz-date"][:8].encode(), hashlib.sha256).digest()
                k_region = hmac.new(k_date, REGION.encode(), hashlib.sha256).digest()
                k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
                k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
                expected = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, signature):
                    return "SignatureDoesNotMatch"
                return ""

            def _send(self, status: int, body: bytes = b"", code: str = "", message: str = "") -> None:
                if code:
                    body = f"<?xml version=\"1.0\"?><Error><Code>{code}</Code><Message>{message or code}</Message></Error>".encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/xml" if code else "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _guarded(self, body: bytes) -> bool:
                fake.requests.append((self.command, self._key()))
                if fake.reject:
                    self._send(403, code=fake.reject, message=fake.reject)
                    return False
                error = self._verify(body)
                if error:
                    self._send(403, code=error, message=error)
                    return False
                if fake.fail_first > 0:
                    with fake._lock:
                        fake.fail_first -= 1
                    self._send(503, code="SlowDown", message="please retry")
                    return False
                return True

            def do_PUT(self):  # noqa: N802
                body = self._body()
                if not self._guarded(body):
                    return
                key = self._key()
                if not key:
                    self._send(404, code="NoSuchBucket", message="no such bucket")
                    return
                with fake._lock:
                    fake.objects[key] = body
                self._send(200)

            def do_GET(self):  # noqa: N802
                if not self._guarded(b""):
                    return
                with fake._lock:
                    blob = fake.objects.get(self._key())
                if blob is None:
                    self._send(404, code="NoSuchKey", message="no such key")
                    return
                self._send(200, blob)

            def do_HEAD(self):  # noqa: N802
                if not self._guarded(b""):
                    return
                with fake._lock:
                    blob = fake.objects.get(self._key())
                if blob is None:
                    self._send(404, code="NoSuchKey", message="no such key")
                    return
                self._send(200)

            def do_DELETE(self):  # noqa: N802
                if not self._guarded(b""):
                    return
                with fake._lock:
                    fake.objects.pop(self._key(), None)
                self._send(204)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeS3":
        self.url = self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
