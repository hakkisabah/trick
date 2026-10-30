"""The systemd unit shipped in deploy/ must be correct before anyone starts it.

The unit is generated, so the failure mode is silent: a wrong ExecStart only
shows up as a service that dies on boot. These tests render the unit the same way
the installer does and assert on the text, without touching systemd.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "install-service.sh"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )


def test_script_is_posix_syntax_clean():
    result = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_dry_run_renders_a_unit_and_starts_nothing():
    result = run("--dry-run")
    assert result.returncode == 0, result.stderr
    unit = result.stdout
    assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit
    assert "ExecStart=" in unit
    assert "-m trick serve" in unit, "the unit must use the only supported invocation"
    assert "0.0.0.0" not in unit, "the bridge must never be bound to a wildcard address"
    assert "Restart=on-failure" in unit
    assert str(ROOT / "src") in unit, "PYTHONPATH must point at this repository"


def test_dry_run_does_not_start_or_enable():
    result = run("--dry-run")
    assert "systemctl" not in result.stdout
    assert "not started" not in result.stdout


def test_non_numeric_port_is_refused():
    result = run("--dry-run", "--port=abc")
    assert result.returncode == 2
    assert "must be a number" in result.stderr


def test_unknown_argument_is_refused():
    result = run("--dry-run", "--teleport")
    assert result.returncode == 2


def test_help_exits_zero():
    result = run("--help")
    assert result.returncode == 0
    assert "systemd --user service" in result.stdout


def test_expose_network_only_appears_with_a_non_loopback_bind():
    unit = run("--dry-run").stdout
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    host = exec_line.split("--http-host ")[1].split()[0]
    if not host.startswith("127."):
        assert "--expose-network" in exec_line
    else:
        assert "--expose-network" not in exec_line
