"""LF-007: project registry, toolchain enforcement, context identity, doctor."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.cli.doctor import run_doctor
from leanfaith.config.paths import RepoPaths
from leanfaith.lean.project_registry import (
    ContextPayload,
    ProjectKind,
    ProjectRole,
    ProjectSpec,
    ToolchainLock,
    ToolchainMode,
    ToolchainViolation,
    build_context_record,
    check_project_toolchain,
    check_toolchain_allowed,
    context_fingerprint,
    load_project_registry,
    read_project_toolchain,
)
from leanfaith.lean.versions import parse_lean_version

_IN_RANGE_LOCK = ToolchainLock(
    mode=ToolchainMode.ADVERTISED_RANGE,
    accepted_lean="v4.31.0-rc1",
)
_EXCEPTION_LOCK = ToolchainLock(
    mode=ToolchainMode.STABLE_V4_31_EXCEPTION,
    accepted_lean="v4.31.0",
    stable_v4_31_exception_adr="docs/adr/ADR-0001-environment-lock.md",
)


def _spec(**overrides: object) -> ProjectSpec:
    payload: dict[str, object] = {
        "registry_key": "fixtures",
        "kind": ProjectKind.LOCAL,
        "uri": "tests/lean_fixtures",
        "revision": "workspace",
        "expected_toolchain": "v4.31.0-rc1",
        "root_module": "LeanFaithFixtures",
        "role": ProjectRole.FIXTURE,
    }
    payload.update(overrides)
    return ProjectSpec.model_validate(payload)


def _project_dir(tmp_path: Path, toolchain: str) -> Path:
    directory = tmp_path / "proj"
    directory.mkdir()
    (directory / "lean-toolchain").write_text(f"leanprover/lean4:{toolchain}\n")
    return directory


# --- toolchain checks (§6.2) ---


def test_read_project_toolchain(tmp_path: Path) -> None:
    directory = _project_dir(tmp_path, "v4.31.0-rc1")
    assert str(read_project_toolchain(directory)) == "v4.31.0-rc1"


def test_read_project_toolchain_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ToolchainViolation, match="cannot read"):
        read_project_toolchain(tmp_path)


def test_in_range_toolchain_allowed() -> None:
    check_toolchain_allowed(parse_lean_version("v4.31.0-rc1"), _IN_RANGE_LOCK)


def test_stable_v4_31_rejected_without_adr() -> None:
    with pytest.raises(ToolchainViolation, match="ADR-0001"):
        check_toolchain_allowed(parse_lean_version("v4.31.0"), _IN_RANGE_LOCK)


def test_stable_v4_31_allowed_with_exception_adr() -> None:
    check_toolchain_allowed(parse_lean_version("v4.31.0"), _EXCEPTION_LOCK)


def test_v4_32_rc1_always_rejected() -> None:
    for lock in (_IN_RANGE_LOCK, _EXCEPTION_LOCK):
        with pytest.raises(ToolchainViolation):
            check_toolchain_allowed(parse_lean_version("v4.32.0-rc1"), lock)


def test_check_project_toolchain_matches_pin(tmp_path: Path) -> None:
    directory = _project_dir(tmp_path, "v4.31.0-rc1")
    version = check_project_toolchain(_spec(), directory, _IN_RANGE_LOCK)
    assert str(version) == "v4.31.0-rc1"


def test_check_project_toolchain_rejects_pin_mismatch(tmp_path: Path) -> None:
    directory = _project_dir(tmp_path, "v4.30.0")
    with pytest.raises(ToolchainViolation, match="expected_toolchain"):
        check_project_toolchain(_spec(), directory, _IN_RANGE_LOCK)


def test_check_project_toolchain_rejects_lock_mismatch(tmp_path: Path) -> None:
    directory = _project_dir(tmp_path, "v4.30.0")
    spec = _spec(expected_toolchain="v4.30.0")
    with pytest.raises(ToolchainViolation, match="accepted lock"):
        check_project_toolchain(spec, directory, _IN_RANGE_LOCK)


def test_ood_probe_projects_only_need_in_range_toolchain(tmp_path: Path) -> None:
    # CSLib/Physlib pins may lag the accepted lock; they must only be in range.
    directory = _project_dir(tmp_path, "v4.30.0")
    spec = _spec(
        registry_key="physlib",
        expected_toolchain="v4.30.0",
        role=ProjectRole.PROBE_NOW_ADAPTER_AT_OOD,
    )
    version = check_project_toolchain(spec, directory, _IN_RANGE_LOCK)
    assert str(version) == "v4.30.0"


# --- registry loading ---


def _registry_root(tmp_path: Path) -> RepoPaths:
    (tmp_path / "PLAN.md").write_text("plan\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "configs" / "projects").mkdir(parents=True)
    return RepoPaths(root=tmp_path)


def test_load_project_registry(tmp_path: Path) -> None:
    paths = _registry_root(tmp_path)
    (paths.configs / "projects" / "fixtures.yaml").write_text(
        "registry_key: fixtures\n"
        "kind: local\n"
        "uri: tests/lean_fixtures\n"
        "revision: workspace\n"
        "root_module: LeanFaithFixtures\n"
        "role: fixture\n"
    )
    registry = load_project_registry(paths)
    assert set(registry) == {"fixtures"}
    assert registry["fixtures"].kind is ProjectKind.LOCAL


def test_load_project_registry_rejects_stem_mismatch(tmp_path: Path) -> None:
    paths = _registry_root(tmp_path)
    (paths.configs / "projects" / "other.yaml").write_text(
        "registry_key: fixtures\nkind: local\nuri: u\nrevision: r\nrole: fixture\n"
    )
    with pytest.raises(ValueError, match="must match file stem"):
        load_project_registry(paths)


# --- context identity (§8.11, §12.5) ---


def _payload(**overrides: object) -> ContextPayload:
    base: dict[str, object] = {
        "environment_schema_version": 1,
        "lean_version": "v4.31.0-rc1",
        "lean_interact_version": "0.11.4",
        "repl_revision": "v1.3.17",
        "project_uri": "tests/lean_fixtures",
        "project_revision": "workspace",
        "imports": ("Mathlib",),
        "header_text": "import Mathlib\n",
    }
    base.update(overrides)
    return ContextPayload.model_validate(base)


def test_context_fingerprint_deterministic() -> None:
    assert context_fingerprint(_payload()) == context_fingerprint(_payload())


@pytest.mark.parametrize(
    "override",
    [
        {"environment_schema_version": 2},
        {"lean_version": "v4.30.0"},
        {"imports": ("Mathlib", "Aesop")},
        {"open_context": ("Nat",)},
        {"options": {"pp.explicit": True}},
        {"header_text": "import Mathlib\nimport Aesop\n"},
    ],
)
def test_context_fingerprint_covers_payload(override: dict[str, object]) -> None:
    assert context_fingerprint(_payload(**override)) != context_fingerprint(_payload())


def test_build_context_record_identity() -> None:
    record = build_context_record(_payload(), project_kind="local", project_registry_key="fixtures")
    assert record.context_id == f"ctx:{record.context_fingerprint}"
    assert record.context_fingerprint == context_fingerprint(_payload())


# --- doctor ---


def _doctor_root(tmp_path: Path, *, toolchain: str = "v4.31.0-rc1") -> RepoPaths:
    paths = _registry_root(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (paths.configs / "environment.lock.yaml").write_text(
        "environment_schema_version: 1\n"
        "python:\n  version: '3.12'\n"
        "lean_interact:\n"
        "  package: lean-interact\n"
        "  version: 0.11.4\n"
        "  advertised_lean_min: v4.8.0-rc1\n"
        "  advertised_lean_max: v4.31.0-rc1\n"
        "  repl_fork: https://github.com/augustepoiroux/repl\n"
        "toolchain_lock:\n"
        "  mode: advertised_range\n"
        f"  accepted_lean: {toolchain}\n"
        "lean_backend: {}\n"
    )
    (paths.configs / "projects" / "fixtures.yaml").write_text(
        "registry_key: fixtures\n"
        "kind: local\n"
        "uri: fixture_project\n"
        "revision: workspace\n"
        f"expected_toolchain: {toolchain}\n"
        "role: fixture\n"
    )
    project = tmp_path / "fixture_project"
    project.mkdir()
    (project / "lean-toolchain").write_text(f"leanprover/lean4:{toolchain}\n")
    # Reuse the real repo's probe report so the doctor sees ok=true.
    real_root = RepoPaths.discover(Path(__file__).parent)
    real_probe = real_root.reports / "compatibility" / "leaninteract_api.json"
    probe_dir = paths.reports / "compatibility"
    probe_dir.mkdir(parents=True)
    (probe_dir / "leaninteract_api.json").write_bytes(real_probe.read_bytes())
    return paths


def test_doctor_passes_on_consistent_root(tmp_path: Path) -> None:
    paths = _doctor_root(tmp_path)
    report = run_doctor(paths)
    assert report.ok, [c for c in report.checks if not c.passed]


def test_doctor_fails_without_lock(tmp_path: Path) -> None:
    paths = _doctor_root(tmp_path)
    (paths.configs / "environment.lock.yaml").unlink()
    report = run_doctor(paths)
    assert not report.ok
    failed = {c.name for c in report.checks if not c.passed}
    assert "environment_lock_resolved" in failed


def test_doctor_fails_on_out_of_range_toolchain(tmp_path: Path) -> None:
    paths = _doctor_root(tmp_path, toolchain="v4.32.0-rc1")
    report = run_doctor(paths)
    assert not report.ok
    failed = {c.name for c in report.checks if not c.passed}
    assert "project_toolchain:fixtures" in failed


def test_doctor_fails_on_missing_probe_report(tmp_path: Path) -> None:
    paths = _doctor_root(tmp_path)
    (paths.reports / "compatibility" / "leaninteract_api.json").unlink()
    report = run_doctor(paths)
    assert not report.ok


def test_doctor_cli_writes_report_and_exit_code(tmp_path: Path) -> None:
    paths = _doctor_root(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--root", str(paths.root)])
    assert result.exit_code == 0, result.output
    report_file = paths.reports / "gates" / "doctor_latest.json"
    assert json.loads(report_file.read_text())["ok"] is True

    (paths.configs / "environment.lock.yaml").unlink()
    result = runner.invoke(app, ["doctor", "--root", str(paths.root)])
    assert result.exit_code == 1
