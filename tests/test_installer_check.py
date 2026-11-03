"""The installer must be able to say "your install is stale".

Every round of this project that went wrong went wrong the same way: the file in
the repository moved on, the copy inside Open WebUI's database did not, and the
button behaved like an older release while looking fine. ``install.py --check``
compares bytes, type and visibility against the installed copy, so "is the thing
I am testing the thing I built" is one command instead of a suspicion.

The tests drive the real script against a stub of the three endpoints it reads.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "integrations" / "openwebui" / "install.py"
ACTION = ROOT / "integrations" / "openwebui" / "trick_action.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location("trick_owui_installer", INSTALL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Stub:
    """Serves the listing and the stored function, with whatever content it is given."""

    def __init__(self, content: str | None, *, is_global: bool = True, stored_type: str = "action"):
        self.content = content
        self.is_global = is_global
        self.stored_type = stored_type
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output clean
                return

            def _send(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path.rstrip("/") == "/api/v1/functions":
                    if outer.content is None:
                        return self._send([])
                    return self._send([{"id": "trick", "name": "Trick", "type": outer.stored_type}])
                if self.path == "/api/v1/functions/id/trick":
                    if outer.content is None:
                        return self._send({"detail": "not found"}, 404)
                    return self._send(
                        {
                            "id": "trick",
                            "type": outer.stored_type,
                            "content": outer.content,
                            "is_global": outer.is_global,
                            "is_active": True,
                        }
                    )
                return self._send({"detail": "unexpected path"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _run(monkeypatch, url: str, *extra: str) -> int:
    monkeypatch.setenv("TRICK_OPENWEBUI_API_KEY", "test-key")
    monkeypatch.setattr(sys, "argv", ["install.py", "--url", url, "--check", *extra])
    return _load_installer().main()


def test_a_matching_install_is_reported_as_uptodate(monkeypatch, capsys):
    content = ACTION.read_text(encoding="utf-8")
    with _Stub(content) as stub:
        assert _run(monkeypatch, stub.url) == 0
    assert "up to date" in capsys.readouterr().out


def test_an_older_install_fails_the_check(monkeypatch, capsys):
    stale = ACTION.read_text(encoding="utf-8").replace("version: 0.", "version: 0.0", 1)
    with _Stub(stale) as stub:
        assert _run(monkeypatch, stub.url) == 1
    captured = capsys.readouterr()
    assert "content differs" in captured.err
    assert "re-run this script" in captured.err


def test_a_missing_function_fails_the_check(monkeypatch, capsys):
    with _Stub(None) as stub:
        assert _run(monkeypatch, stub.url) == 1
    assert "not installed" in capsys.readouterr().err


def test_a_hidden_action_fails_the_check(monkeypatch, capsys):
    """A non-global action has no button, which reads as "the tool is broken"."""
    content = ACTION.read_text(encoding="utf-8")
    with _Stub(content, is_global=False) as stub:
        assert _run(monkeypatch, stub.url) == 1
    assert "not global" in capsys.readouterr().err


def test_a_pipe_install_fails_the_check(monkeypatch, capsys):
    """A stale install also kept the old type; that is what made it a model."""
    content = ACTION.read_text(encoding="utf-8")
    with _Stub(content, stored_type="pipe") as stub:
        assert _run(monkeypatch, stub.url) == 1
    assert "expected 'action'" in capsys.readouterr().err


def test_without_a_key_the_check_says_which_key_is_missing(monkeypatch, capsys):
    monkeypatch.delenv("TRICK_OPENWEBUI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["install.py", "--check"])
    assert _load_installer().main() == 2
    assert "TRICK_OPENWEBUI_API_KEY" in capsys.readouterr().err
