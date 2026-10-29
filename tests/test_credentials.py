"""Credential hygiene: parsing, permissions, precedence, and no key leakage."""

from __future__ import annotations

import os

import pytest

from trick.credentials import (
    check_permissions,
    load_laya_credentials,
    parse_credentials_file,
)
from trick.errors import ConfigError

KEY = "sk-live-abcdef123456"


def write_creds(tmp_path, *, mode=0o600, extra: str = "") -> "os.PathLike[str]":
    path = tmp_path / "laya.credentials"
    path.write_text(
        "# comment line\n"
        f"LAYA_BASE_URL=https://laya.test/v1/systemone/batch\n"
        f"LAYA_API_KEY={KEY}\n"
        "LAYA_TIMEOUT_MS=5000\n"
        "LAYA_EXAMPLE_REQUEST_BEGIN\n"
        "LAYA_API_KEY=should-be-ignored\n"
        "import requests\n"
        "LAYA_EXAMPLE_REQUEST_END\n" + extra,
        encoding="utf-8",
    )
    os.chmod(path, mode)
    return path


def test_parse_skips_comments_and_the_example_block(tmp_path):
    values = parse_credentials_file(write_creds(tmp_path))
    assert values["LAYA_API_KEY"] == KEY
    assert values["LAYA_TIMEOUT_MS"] == "5000"


def test_load_reads_the_file_and_strips_whitespace(tmp_path):
    path = tmp_path / "laya.credentials"
    path.write_text(f"LAYA_BASE_URL=https://laya.test/x\nLAYA_API_KEY={KEY}\n\n")
    os.chmod(path, 0o600)
    creds = load_laya_credentials(path, env={})
    assert creds.base_url == "https://laya.test/x"
    assert creds.api_key == KEY
    assert creds.origin == "file"
    assert creds.configured


def test_environment_wins_over_the_file(tmp_path, monkeypatch):
    path = write_creds(tmp_path)
    creds = load_laya_credentials(path, env={"LAYA_API_KEY": "from-env", "LAYA_BASE_URL": "https://env.test/x"})
    assert creds.api_key == "from-env"
    assert creds.origin == "env"


def test_group_readable_key_file_is_refused(tmp_path):
    path = write_creds(tmp_path, mode=0o644)
    with pytest.raises(ConfigError) as info:
        load_laya_credentials(path, env={})
    assert "chmod 600" in str(info.value)


def test_override_escape_hatch_allows_a_loose_file(tmp_path, monkeypatch):
    path = write_creds(tmp_path, mode=0o644)
    monkeypatch.setenv("TRICK_ALLOW_INSECURE_CREDENTIALS", "1")
    creds = load_laya_credentials(path, env={"TRICK_ALLOW_INSECURE_CREDENTIALS": "1"})
    assert creds.configured


def test_credentials_never_leak_the_key():
    from trick.credentials import LayaCredentials

    creds = LayaCredentials(base_url="https://x.test/y", api_key=KEY)
    assert KEY not in repr(creds)
    assert creds.redact(f"header Bearer {KEY} sent") == "header Bearer *** sent"
    assert creds.redact("clean text") == "clean text"


def test_missing_values_raise_config_error(tmp_path):
    with pytest.raises(ConfigError) as info:
        load_laya_credentials(tmp_path / "absent", env={})
    assert "not configured" in str(info.value)
    assert getattr(info.value, "s_code") == "S9"


def test_relative_url_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_laya_credentials(
            tmp_path / "absent", env={"LAYA_API_KEY": "k", "LAYA_BASE_URL": "laya.test/x"}
        )


def test_bad_timeout_falls_back_to_default(tmp_path):
    creds = load_laya_credentials(
        tmp_path / "absent",
        env={"LAYA_API_KEY": "k", "LAYA_BASE_URL": "https://laya.test/x", "LAYA_TIMEOUT_MS": "nonsense"},
    )
    assert creds.timeout_ms == 20_000
    assert creds.timeout_s == 20.0


def test_permission_check_is_a_noop_for_tight_modes(tmp_path):
    path = write_creds(tmp_path, mode=0o600)
    check_permissions(path)  # must not raise
