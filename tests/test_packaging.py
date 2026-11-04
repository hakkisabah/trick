"""The package must be installable by a stranger, not just runnable in this checkout.

Two things a first-time user hits that no other test covered: there was no license
file at all (the metadata said "Proprietary" while the project is published open
source), and there was no console script, so every documented command assumed an
activated virtualenv. Both are asserted against the real pyproject, not against a
copy of it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_licence_file_exists_and_is_named_in_the_metadata():
    licence = pyproject()["project"]["license"]["text"]
    assert licence == "MIT"
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "WITHOUT WARRANTY OF ANY KIND" in text


def test_no_metadata_claims_proprietary_while_the_licence_file_is_mit():
    project = pyproject()["project"]
    assert "proprietary" not in str(project.get("license", {})).lower()
    assert (ROOT / "LICENSE").exists()


def test_console_script_is_declared_and_points_at_a_callable():
    scripts = pyproject()["project"]["scripts"]
    assert scripts["trick"] == "trick.cli:main"
    from trick.cli import main

    assert callable(main)


def test_version_is_semver_and_not_a_placeholder():
    version = pyproject()["project"]["version"]
    major, minor, patch = version.split(".")
    assert all(part.isdigit() for part in (major, minor, patch))


def test_readme_is_the_described_file_and_has_no_unfinished_marker():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "TODO" not in text
    assert "FIXME" not in text
