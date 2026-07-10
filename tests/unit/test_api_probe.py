"""LF-006: LeanInteract API-shape probe against the pinned installation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.lean.api_probe import (
    ADVERTISED_LEAN_MAX,
    ADVERTISED_LEAN_MIN,
    PINNED_VERSION,
    REQUIRED_SYMBOLS,
    probe_report_paths,
    run_api_probe,
)


@pytest.fixture(scope="module")
def report():  # type: ignore[no-untyped-def]
    return run_api_probe()


def test_probe_passes_against_pinned_install(report) -> None:  # type: ignore[no-untyped-def]
    assert report.installed_version == PINNED_VERSION
    assert report.version_match
    assert report.repl_fork_match
    assert report.ok, [c.name for c in report.caveats if not c.passed] + [
        s.symbol for s in report.symbols if not s.present
    ]


def test_probe_covers_every_appendix_a_symbol(report) -> None:  # type: ignore[no-untyped-def]
    probed = {(s.module, s.symbol) for s in report.symbols}
    assert probed == set(REQUIRED_SYMBOLS)
    assert all(s.present for s in report.symbols)


def test_interface_types_come_from_interface_module(report) -> None:  # type: ignore[no-untyped-def]
    interface_symbols = {s.symbol for s in report.symbols if s.module == "lean_interact.interface"}
    assert interface_symbols == {
        "CommandResponse",
        "DeclarationInfo",
        "InfoTreeOptions",
        "LeanError",
    }


def test_permissive_allow_sorry_default_recorded(report) -> None:  # type: ignore[no-untyped-def]
    caveat = next(c for c in report.caveats if c.name == "lean_code_is_valid_default_is_permissive")
    assert caveat.passed
    assert caveat.observed == "True"


def test_batch_exception_semantics_recorded(report) -> None:  # type: ignore[no-untyped-def]
    caveat = next(c for c in report.caveats if c.name == "run_batch_returns_per_item_exceptions")
    assert caveat.passed


def test_advertised_range_matches_plan(report) -> None:  # type: ignore[no-untyped-def]
    assert report.advertised_lean_min == ADVERTISED_LEAN_MIN == "v4.8.0-rc1"
    assert report.advertised_lean_max == ADVERTISED_LEAN_MAX == "v4.31.0-rc1"


def test_report_paths_are_declared_locations() -> None:
    paths = RepoPaths.discover(Path(__file__).parent)
    report_path, artifact_path = probe_report_paths(paths)
    assert report_path == paths.reports / "compatibility" / "leaninteract_api.json"
    assert artifact_path.parent == paths.artifacts / "compatibility"


def test_cli_probe_api_writes_artifacts(tmp_path: Path) -> None:
    # Redirect outputs to a scratch root that still has git metadata by
    # pointing --root at the real repo? No: use the real repo root so code
    # state resolves, but assert on the deliverable files it (re)writes.
    root = find_repo_root(Path(__file__).parent)
    runner = CliRunner()
    result = runner.invoke(app, ["probe-api", "--root", str(root)])
    assert result.exit_code == 0, result.output
    report_file = root / "reports" / "compatibility" / "leaninteract_api.json"
    assert report_file.is_file()
    payload = json.loads(report_file.read_text())
    assert payload["ok"] is True
    assert payload["installed_version"] == PINNED_VERSION
