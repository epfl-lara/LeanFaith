"""Fail-closed Codex scale-v2 to pooled Lean-check bridge tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import leanfaith.generation.lf022_lean_check as checker
from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation.lf022_codex_proposer_scale import run_lf022_codex_proposer_scale
from leanfaith.generation.lf022_codex_scale_lean_check import (
    LF022CodexScaleLeanCheckError,
    verify_lf022_codex_scale_for_lean_check,
)
from leanfaith.generation.lf022_lean_check import (
    LF022LeanCheckError,
    check_lf022_provisional_candidates,
)
from tests.unit.test_lf022_codex_proposer import (
    _batch_fixture,
    _SuccessfulExecutor,
)
from tests.unit.test_lf022_codex_proposer_scale import _scale_config_fixture
from tests.unit.test_lf022_lean_check import FakeBackend


def test_pooled_lean_check_cli_exposes_codex_scale_selector() -> None:
    result = CliRunner().invoke(app, ["check-lf022-provisional-lean", "--help"])
    assert result.exit_code == 0
    assert "--codex-scale-manife" in result.stdout
    assert "Codex proposer" in result.stdout
    assert "scale-v2 manifest" in result.stdout


def _completed_scale(root: Path) -> Path:
    batch_path, task_id = _batch_fixture(root)
    config_path = _scale_config_fixture(root, task_limit=1)
    result = run_lf022_codex_proposer_scale(
        repo_root=root,
        config_path=config_path,
        batch_manifest_path=batch_path,
        output_root=root / "data/codex_scale_output",
        execution_task_ids=(task_id,),
        execute_public_provisional=True,
        executor=_SuccessfulExecutor(),
        verify_cli_pin=False,
    )
    assert result.manifest_path is not None
    return result.manifest_path


def test_completed_scale_replays_into_existing_worker_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeBackend.created = []
    FakeBackend.status_scripts = {}
    manifest_path = _completed_scale(tmp_path)
    selected = verify_lf022_codex_scale_for_lean_check(
        repo_root=tmp_path,
        manifest_path=manifest_path,
    )
    assert len(selected.tasks) == 1
    project_dir = tmp_path / "mathlib-project"
    project_dir.mkdir()
    source_revision = selected.source_batch.routes[0].tasks[0]
    source_task = json.loads((tmp_path / source_revision.task.path).read_bytes())
    revision = source_task["source"]["source_revision"]
    monkeypatch.setattr(checker, "read_git_revision", lambda _path: revision)

    result = check_lf022_provisional_candidates(
        repo_root=tmp_path,
        input_root=manifest_path.parent,
        output_root=tmp_path / "data/scale_lean_checks",
        project_dirs={"mathlib": project_dir},
        workers=3,
        chunk_size=8,
        timeout_seconds=20,
        codex_scale_manifest_path=manifest_path,
        backend_factory=FakeBackend,
        prepare_environment=lambda _settings: None,
    )

    assert result.executed_count == 1
    assert result.manifest.schema_version == 4
    assert result.manifest.selection_batch_id == selected.source_batch.batch_id
    assert result.manifest.selection_codex_scale_tranche_id == selected.tranche.tranche_id
    assert result.manifest.selection_codex_scale_manifest_sha256 == hash_file(manifest_path)
    assert result.manifest.record_count == 1
    assert result.records[0].outcome == "elaborates_with_placeholder"
    assert result.manifest.semantic_labels_created is False
    assert result.manifest.silver_records_created is False
    assert result.manifest.training_eligible is False
    assert result.manifest.evaluation_eligible is False
    assert FakeBackend.created[0].settings.workers == 3


def test_scale_selector_rejects_partial_and_foreign_roots(tmp_path: Path) -> None:
    manifest_path = _completed_scale(tmp_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest_path.unlink()
    with pytest.raises(LF022CodexScaleLeanCheckError, match="missing or unreadable"):
        verify_lf022_codex_scale_for_lean_check(
            repo_root=tmp_path,
            manifest_path=manifest_path,
        )
    manifest_path.write_bytes(manifest_bytes)
    (manifest_path.parent / "foreign.txt").write_text("foreign", encoding="utf-8")
    with pytest.raises(LF022CodexScaleLeanCheckError, match="foreign entries"):
        verify_lf022_codex_scale_for_lean_check(
            repo_root=tmp_path,
            manifest_path=manifest_path,
        )

    missing_root = tmp_path / "missing-terminal"
    missing_root.mkdir()
    missing_manifest = _completed_scale(missing_root)
    terminal_path = next(missing_manifest.parent.glob("v1_runs/*/items/*/*/terminal.json"))
    terminal_path.unlink()
    with pytest.raises(LF022CodexScaleLeanCheckError, match="missing or unreadable"):
        verify_lf022_codex_scale_for_lean_check(
            repo_root=missing_root,
            manifest_path=missing_manifest,
        )


def test_scale_selector_rejects_foreign_run_and_nested_symlink(tmp_path: Path) -> None:
    manifest_path = _completed_scale(tmp_path)
    runs_root = manifest_path.parent / "v1_runs"
    foreign_run = runs_root / ("f" * 64)
    foreign_run.mkdir()
    with pytest.raises(LF022CodexScaleLeanCheckError, match="foreign entries"):
        verify_lf022_codex_scale_for_lean_check(
            repo_root=tmp_path,
            manifest_path=manifest_path,
        )
    foreign_run.rmdir()

    variants_path = next(runs_root.glob("*/items/*/*/provisional_variants.jsonl"))
    escaped = tmp_path / "escaped-variants.jsonl"
    escaped.write_bytes(variants_path.read_bytes())
    variants_path.unlink()
    variants_path.symlink_to(escaped)
    with pytest.raises(LF022CodexScaleLeanCheckError, match="symlink"):
        verify_lf022_codex_scale_for_lean_check(
            repo_root=tmp_path,
            manifest_path=manifest_path,
        )


def test_scale_selector_rejects_terminal_and_source_hash_drift(tmp_path: Path) -> None:
    manifest_path = _completed_scale(tmp_path)
    terminal_path = next(manifest_path.parent.glob("v1_runs/*/items/*/*/terminal.json"))
    terminal = json.loads(terminal_path.read_bytes())
    terminal["exit_code"] = 9
    terminal_path.write_bytes(canonical_json_bytes(terminal) + b"\n")
    with pytest.raises(LF022CodexScaleLeanCheckError, match="terminal binding differs"):
        verify_lf022_codex_scale_for_lean_check(
            repo_root=tmp_path,
            manifest_path=manifest_path,
        )

    other_root = tmp_path / "source-drift"
    other_root.mkdir()
    other_manifest = _completed_scale(other_root)
    scale = json.loads(other_manifest.read_bytes())
    batch_path = other_root / scale["source_batch_manifest"]
    batch = json.loads(batch_path.read_bytes())
    source_task_path = other_root / batch["routes"][0]["tasks"][0]["task"]["path"]
    source_task_path.write_bytes(source_task_path.read_bytes() + b"\n")
    with pytest.raises(LF022CodexScaleLeanCheckError, match="hash differs"):
        verify_lf022_codex_scale_for_lean_check(
            repo_root=other_root,
            manifest_path=other_manifest,
        )


def test_scale_selector_is_exclusive_and_input_root_is_bound(
    tmp_path: Path,
) -> None:
    manifest_path = _completed_scale(tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with pytest.raises(LF022LeanCheckError, match="mutually exclusive"):
        check_lf022_provisional_candidates(
            repo_root=tmp_path,
            input_root=manifest_path.parent,
            output_root=tmp_path / "checks-exclusive",
            project_dirs={"mathlib": project_dir},
            workers=1,
            chunk_size=1,
            timeout_seconds=1,
            batch_manifest_path=tmp_path / "unused.json",
            codex_scale_manifest_path=manifest_path,
            backend_factory=FakeBackend,
            prepare_environment=lambda _settings: None,
        )
    wrong_root = tmp_path / "wrong-root"
    wrong_root.mkdir()
    with pytest.raises(LF022LeanCheckError, match="input_root differs"):
        check_lf022_provisional_candidates(
            repo_root=tmp_path,
            input_root=wrong_root,
            output_root=tmp_path / "checks-wrong-root",
            project_dirs={"mathlib": project_dir},
            workers=1,
            chunk_size=1,
            timeout_seconds=1,
            codex_scale_manifest_path=manifest_path,
            backend_factory=FakeBackend,
            prepare_environment=lambda _settings: None,
        )


def test_scale_lineage_addition_during_check_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeBackend.created = []
    FakeBackend.status_scripts = {}
    manifest_path = _completed_scale(tmp_path)
    selected = verify_lf022_codex_scale_for_lean_check(
        repo_root=tmp_path,
        manifest_path=manifest_path,
    )
    source_binding = selected.source_batch.routes[0].tasks[0].task
    source_task = json.loads((tmp_path / source_binding.path).read_bytes())
    revision = source_task["source"]["source_revision"]
    monkeypatch.setattr(checker, "read_git_revision", lambda _path: revision)
    project_dir = tmp_path / "project-mutation"
    project_dir.mkdir()

    def mutate_scale(_settings: object) -> None:
        (manifest_path.parent / "foreign-during-check.txt").write_text(
            "foreign",
            encoding="utf-8",
        )

    with pytest.raises(LF022LeanCheckError, match="changed during checking"):
        check_lf022_provisional_candidates(
            repo_root=tmp_path,
            input_root=manifest_path.parent,
            output_root=tmp_path / "checks-mutation",
            project_dirs={"mathlib": project_dir},
            workers=1,
            chunk_size=1,
            timeout_seconds=1,
            codex_scale_manifest_path=manifest_path,
            backend_factory=FakeBackend,
            prepare_environment=mutate_scale,
        )
