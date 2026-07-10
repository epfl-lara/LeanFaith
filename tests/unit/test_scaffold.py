"""LF-001 scaffold acceptance tests."""

from __future__ import annotations

from typer.testing import CliRunner

import leanfaith
from leanfaith.cli.app import app

runner = CliRunner()


def test_package_exposes_version() -> None:
    assert leanfaith.__version__ == "0.1.0"


def test_cli_help_succeeds() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "LeanFaith" in result.output


def test_cli_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert leanfaith.__version__ in result.output


def test_cli_unknown_command_fails() -> None:
    result = runner.invoke(app, ["does-not-exist"])
    assert result.exit_code != 0
