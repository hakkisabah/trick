"""Credential loading for the laya scorer.

Precedence, highest first:

1. environment (``LAYA_BASE_URL``, ``LAYA_API_KEY``, ``LAYA_TIMEOUT_MS``),
2. ``$TRICK_CREDENTIALS`` or ``~/.config/hermes-secrets/laya.credentials``.

The file format is one ``KEY=value`` per line; ``#`` starts a comment. A
``LAYA_EXAMPLE_REQUEST_BEGIN``/``_END`` block is ignored: it is documentation
the user pastes next to the real values.

Security rules enforced here:

* the key is never included in ``repr()``, exception text or ``redact()`` output;
* a key file readable by group or other is refused, because the whole point of
  the 600 mode is that no other local user can spend the account's credits;
  set ``TRICK_ALLOW_INSECURE_CREDENTIALS=1`` to override on dev boxes;
* the key is stripped of surrounding whitespace, so a trailing newline in the
  file cannot produce a header that fails authentication.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "hermes-secrets" / "laya.credentials"
DEFAULT_TIMEOUT_MS = 20_000
IGNORED_BLOCK_START = "LAYA_EXAMPLE_REQUEST_BEGIN"
IGNORED_BLOCK_END = "LAYA_EXAMPLE_REQUEST_END"


@dataclass(frozen=True)
class LayaCredentials:
    """Resolved connection settings. ``api_key`` never appears in ``repr``."""

    base_url: str
    api_key: str = field(repr=False)
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    model: str = "laya"
    origin: str = "unset"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def timeout_s(self) -> float:
        return max(self.timeout_ms, 1) / 1000.0

    def redact(self, text: str) -> str:
        """Remove the key from ``text`` so it can be logged or raised."""
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, "***")
        return text


def parse_credentials_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines, skipping comments and the example block."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    in_example = False
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read credentials file {path}: {exc}") from exc
    for raw in raw_text.splitlines():
        line = raw.strip()
        if IGNORED_BLOCK_START in line:
            in_example = True
            continue
        if IGNORED_BLOCK_END in line:
            in_example = False
            continue
        if in_example or not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def check_permissions(path: Path) -> None:
    """Refuse a key file that other local users can read."""
    if os.environ.get("TRICK_ALLOW_INSECURE_CREDENTIALS") == "1":
        return
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ConfigError(f"cannot stat credentials file {path}: {exc}") from exc
    if mode & 0o077:
        raise ConfigError(
            f"credentials file {path} is readable by group or other (mode {oct(mode & 0o777)}); "
            f"run: chmod 600 {path}"
        )


def load_laya_credentials(
    path: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> LayaCredentials:
    """Resolve credentials from the environment, then from the key file."""
    environ = os.environ if env is None else env
    cred_path = Path(path or environ.get("TRICK_CREDENTIALS") or DEFAULT_CREDENTIALS_PATH)

    file_values: dict[str, str] = {}
    if cred_path.exists():
        check_permissions(cred_path)
        file_values = parse_credentials_file(cred_path)

    base = (environ.get("LAYA_BASE_URL") or file_values.get("LAYA_BASE_URL", "")).strip()
    key = (environ.get("LAYA_API_KEY") or file_values.get("LAYA_API_KEY", "")).strip()
    timeout_raw = environ.get("LAYA_TIMEOUT_MS") or file_values.get("LAYA_TIMEOUT_MS") or ""
    model = (environ.get("LAYA_MODEL") or file_values.get("LAYA_MODEL") or "laya").strip()

    try:
        timeout_ms = int(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_MS
    except ValueError:
        timeout_ms = DEFAULT_TIMEOUT_MS
    if timeout_ms < 1000:
        timeout_ms = DEFAULT_TIMEOUT_MS

    origin = "env" if environ.get("LAYA_API_KEY") else ("file" if key else "unset")
    if not base or not key:
        raise ConfigError(
            f"laya is not configured: set LAYA_BASE_URL and LAYA_API_KEY, or fill {cred_path}"
        )
    if not base.startswith(("https://", "http://")):
        raise ConfigError(f"LAYA_BASE_URL must be an absolute http(s) URL, got {base!r}")
    return LayaCredentials(base_url=base, api_key=key, timeout_ms=timeout_ms, model=model, origin=origin)


__all__ = [
    "DEFAULT_CREDENTIALS_PATH",
    "LayaCredentials",
    "check_permissions",
    "load_laya_credentials",
    "parse_credentials_file",
]
