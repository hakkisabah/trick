"""Interactive terminal UI: type a problem, press Enter, get scored sources.

Design constraints that shaped this file:

* **it has to work in a pipe.** ``echo "query" | trick tui`` must behave like a
  session, because that is how it is tested and how it is used in a shell script.
  readline is only imported when stdin is a TTY.
* **Enter is the trigger.** Nothing is sent while the user is still typing; a
  half-written word must never become a laya call.
* **a failed search keeps the session.** Any ``S<n>`` code prints one honest line
  and returns to the prompt; the UI never exits because a source was down.
* **Ctrl-C cancels the line, Ctrl-D exits.** Interrupting a thought is not the
  same as quitting the tool.

Every keystroke path is covered by tests/test_tui.py, including the piped form.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, TextIO

from .errors import TrickError
from .render import explain, render_json, render_outcome
from .search import SearchOutcome

HELP = """commands:
  <text>            search for it (Enter submits)
  /limit <n>        how many rows to show (1-26)
  /live on|off      include the live paper APIs
  /verbose          toggle score components
  /json             toggle raw JSON output
  /health           show the engine state
  /help             this text
  /quit             leave (Ctrl-D also works)"""


class TerminalUI:
    """A read-eval-print loop over any ``Backend``-shaped object."""

    def __init__(
        self,
        backend,
        *,
        limit: int = 8,
        live: bool = True,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        history_path: str | os.PathLike[str] | None = None,
        interactive: bool | None = None,
    ) -> None:
        self.backend = backend
        self.limit = max(1, min(int(limit), 26))
        self.live = bool(live)
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.history_path = Path(history_path) if history_path else None
        self.verbose = False
        self.as_json = False
        self.queries = 0
        self.interactive = self._is_tty() if interactive is None else bool(interactive)
        self._readline = None

    # -- io ----------------------------------------------------------------
    def _is_tty(self) -> bool:
        try:
            return bool(self.stdin.isatty())
        except Exception:  # noqa: BLE001 - a replaced stream may not implement it
            return False

    def _write(self, text: str = "") -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def _error(self, text: str) -> None:
        self.stderr.write(text + "\n")
        self.stderr.flush()

    def _prompt(self) -> str:
        return "trick> "

    def _setup_readline(self) -> None:
        if not self.interactive:
            return
        try:
            import readline
        except ImportError:  # pragma: no cover - Windows without pyreadline
            return
        self._readline = readline
        if self.history_path is not None:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                readline.read_history_file(str(self.history_path))
            except OSError:
                pass
            readline.set_history_length(500)
        readline.parse_and_bind("tab: complete")

    def _save_history(self) -> None:
        if self._readline is None or self.history_path is None:
            return
        try:
            self._readline.write_history_file(str(self.history_path))
        except OSError:
            pass

    # -- loop --------------------------------------------------------------
    def run(self) -> int:
        """Read lines until EOF or /quit. Returns a shell exit code."""
        self._setup_readline()
        if self.interactive:
            self._write("trick - scored retrieval. /help for commands, Ctrl-D to leave.")
        try:
            while True:
                try:
                    if self.interactive:
                        line = input(self._prompt())
                    else:
                        line = self.stdin.readline()
                        if not line:
                            break
                except EOFError:
                    break
                except KeyboardInterrupt:
                    # Cancel the current line, keep the session.
                    self._write("")
                    continue
                if not self._handle_line(line):
                    break
        finally:
            self._save_history()
            self.close()
        return 0

    def _handle_line(self, line: str) -> bool:
        """Process one input line. Returns False when the session should end."""
        text = line.strip()
        if not text:
            return True
        if text.startswith("/"):
            return self._command(text)
        self.search(text)
        return True

    def _notice(self, text: str) -> None:
        """Command feedback: on screen when interactive, on stderr when piped.

        A pipe carries data. ``trick tui | jq`` must see JSON and nothing else, so
        acknowledgements like "limit set to 2" go to stderr there while staying
        visible to a human typing at a prompt.
        """
        if self.interactive:
            self._write(text)
        else:
            self._error(text)

    def _command(self, text: str) -> bool:
        parts = text.split()
        name = parts[0].lower()
        args = parts[1:]
        if name in ("/quit", "/exit", "/q"):
            return False
        if name in ("/help", "/?", "/h"):
            self._write(HELP)
            return True
        if name == "/limit":
            if not args:
                self._notice(f"limit is {self.limit}")
                return True
            try:
                value = int(args[0])
            except ValueError:
                self._error("usage: /limit <1-26>")
                return True
            if not 1 <= value <= 26:
                self._error("limit must be between 1 and 26")
                return True
            self.limit = value
            self._notice(f"limit set to {self.limit}")
            return True
        if name == "/live":
            if not args:
                self._notice(f"live is {'on' if self.live else 'off'}")
                return True
            self.live = args[0].lower() in ("on", "1", "true", "yes")
            self._notice(f"live turned {'on' if self.live else 'off'}")
            return True
        if name == "/verbose":
            self.verbose = not self.verbose
            self._notice(f"verbose {'on' if self.verbose else 'off'}")
            return True
        if name == "/json":
            self.as_json = not self.as_json
            self._notice(f"json {'on' if self.as_json else 'off'}")
            return True
        if name == "/health":
            self.health()
            return True
        self._error(f"unknown command {name}; /help lists them")
        return True

    # -- actions -----------------------------------------------------------
    def search(self, query: str) -> SearchOutcome | None:
        """Run one query and print it. A failure prints and returns ``None``."""
        try:
            outcome = self.backend.search(query, limit=self.limit, live=self.live)
        except TrickError as exc:
            self._error(f"{exc.s_code}: {exc}")
            if exc.degraded_action and exc.degraded_action != "none":
                self._error(f"  {explain(exc.s_code)} ({exc.degraded_action})")
            return None
        except KeyboardInterrupt:
            self._error("cancelled")
            return None
        except Exception as exc:  # noqa: BLE001 - one bad query must not end the session
            self._error(f"unexpected error: {type(exc).__name__}: {exc}")
            return None
        self.queries += 1
        if self.as_json:
            self._write(render_json(outcome))
        else:
            self._write(render_outcome(outcome, verbose=self.verbose))
        return outcome

    def health(self) -> None:
        try:
            info = self.backend.health()
        except TrickError as exc:
            self._error(f"{exc.s_code}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            self._error(f"unexpected error: {type(exc).__name__}: {exc}")
            return
        self._write(_format_health(info))

    def close(self) -> None:
        closer: Callable[[], None] | None = getattr(self.backend, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass


def _format_health(info: dict) -> str:
    lines = ["health:"]
    store = info.get("store") or {}
    lines.append(f"  documents : {store.get('docs', 0)}  fts5={store.get('fts5')}")
    matrix = info.get("matrix")
    if isinstance(matrix, dict) and "count" in matrix:
        lines.append(f"  matrix    : {matrix.get('count')} records")
    elif isinstance(matrix, dict):
        lines.append(f"  matrix    : error {matrix.get('error')}")
    else:
        lines.append("  matrix    : not built")
    lines.append(
        f"  scoring   : {info.get('scorer')}  weights={info.get('weights')}"
        f"  prior={info.get('prior_weight')}  mode={info.get('blend_mode')}"
    )
    service = info.get("service") or {}
    if service:
        lines.append(f"  service   : pid={service.get('pid')} requests={service.get('requests')}")
    return "\n".join(lines)


def run(backend, **kwargs) -> int:
    """Convenience entry point used by the CLI."""
    return TerminalUI(backend, **kwargs).run()


__all__ = ["TerminalUI", "run"]
