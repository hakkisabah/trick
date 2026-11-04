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


# ---------------------------------------------------------------------------
# the same comparison without an admin key
# ---------------------------------------------------------------------------
def _make_db(path: Path, content: str | None, *, is_global: bool = True, is_active: bool = True,
             kind: str = "action", with_table: bool = True) -> Path:
    """A minimal copy of the table Open WebUI keeps functions in."""
    import sqlite3

    db = sqlite3.connect(path)
    try:
        if with_table:
            db.execute(
                "CREATE TABLE function (id TEXT PRIMARY KEY, content TEXT, type TEXT, "
                "is_global INTEGER, is_active INTEGER)"
            )
            if content is not None:
                db.execute(
                    "INSERT INTO function VALUES (?, ?, ?, ?, ?)",
                    ("trick", content, kind, int(is_global), int(is_active)),
                )
        else:
            db.execute("CREATE TABLE chat (id TEXT)")
        db.commit()
    finally:
        db.close()
    return path


def _run_db(monkeypatch, db_path: Path) -> int:
    """--check --db, with no API key at all: the read-only path."""
    monkeypatch.delenv("TRICK_OPENWEBUI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["install.py", "--check", "--db", str(db_path)])
    return _load_installer().main()


def test_the_database_path_reports_a_matching_install(monkeypatch, tmp_path, capsys):
    content = ACTION.read_text(encoding="utf-8")
    db = _make_db(tmp_path / "webui.db", content)
    assert _run_db(monkeypatch, db) == 0
    out = capsys.readouterr().out
    assert "up to date" in out
    assert "read-only" in out


def test_the_database_path_reports_a_stale_install(monkeypatch, tmp_path, capsys):
    stale = ACTION.read_text(encoding="utf-8").replace("version: 0.", "version: 0.0", 1)
    db = _make_db(tmp_path / "webui.db", stale)
    assert _run_db(monkeypatch, db) == 1
    assert "content differs" in capsys.readouterr().err


def test_the_database_path_reports_a_missing_function(monkeypatch, tmp_path, capsys):
    db = _make_db(tmp_path / "webui.db", None)
    assert _run_db(monkeypatch, db) == 1
    assert "not installed" in capsys.readouterr().err


def test_the_database_path_reports_a_disabled_action(monkeypatch, tmp_path, capsys):
    content = ACTION.read_text(encoding="utf-8")
    db = _make_db(tmp_path / "webui.db", content, is_active=False)
    assert _run_db(monkeypatch, db) == 1
    assert "disabled" in capsys.readouterr().err


def test_a_missing_database_is_a_usage_error(monkeypatch, tmp_path, capsys):
    assert _run_db(monkeypatch, tmp_path / "absent.db") == 2
    assert "database not found" in capsys.readouterr().err


def test_a_database_without_the_function_table_says_so(monkeypatch, tmp_path, capsys):
    db = _make_db(tmp_path / "webui.db", None, with_table=False)
    assert _run_db(monkeypatch, db) == 2
    assert "function table" in capsys.readouterr().err


def test_the_check_does_not_write_to_the_database(monkeypatch, tmp_path):
    """mode=ro is the whole point: verification must not touch a live database."""
    content = ACTION.read_text(encoding="utf-8")
    db = _make_db(tmp_path / "webui.db", content)
    before = db.read_bytes()
    assert _run_db(monkeypatch, db) == 0
    assert db.read_bytes() == before
