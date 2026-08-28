"""LF-016 validation-only command and manifest behavior."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.cli.transformations import (
    TransformationFrameworkFailure,
    TransformationFrameworkReport,
    TransformationFrameworkValidationError,
    validate_transformation_framework,
)
from leanfaith.config.hashing import hash_file
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas import CodeState, RunManifest, read_manifest

_UTC = datetime.datetime(2026, 7, 23, 12, tzinfo=datetime.UTC)


class _Config(StrictModel):
    families: tuple[str, ...]
    rules: tuple[str, ...]

    @property
    def rule_count(self) -> int:
        return len(self.rules)


class _Profile(StrictModel):
    profile_id: str


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return hash_file(path)


def _fixture(tmp_path: Path) -> tuple[RepoPaths, Path]:
    for relative in (
        "configs/transformations/registry.yaml",
        "configs/transformations/v1.yaml",
        "policies/transformation_promotion_v1.yaml",
        "data/benchmarks/manifests/representation_signatures_v1.json",
    ):
        _write(tmp_path / relative, f"# {relative}\n")
    gate_2_hash = _write(
        tmp_path / "reports/gates/gate_2.json",
        '{"decision":"pass","gate":"gate_2"}\n',
    )
    gate_3_hash = _write(
        tmp_path / "reports/gates/gate_3.json",
        '{"decision":"pass","gate":"gate_3"}\n',
    )
    evidence_hash = _write(
        tmp_path / "reports/milestones/post_gate_benchmark_freeze.md",
        "# passed\n",
    )
    authorization = {
        "decision": "pass",
        "lf_016_authorized": True,
        "evidence": "reports/milestones/post_gate_benchmark_freeze.md",
        "evidence_sha256": evidence_hash,
        "prerequisites": {
            "gate_2": {
                "path": "reports/gates/gate_2.json",
                "sha256": gate_2_hash,
                "decision": "pass",
            },
            "gate_3": {
                "path": "reports/gates/gate_3.json",
                "sha256": gate_3_hash,
                "decision": "pass",
            },
        },
    }
    authorization_path = tmp_path / "reports/gates/lf_016_authorization.json"
    _write(authorization_path, json.dumps(authorization, sort_keys=True) + "\n")
    return RepoPaths(root=tmp_path), authorization_path


def _patch_dependencies(monkeypatch: pytest.MonkeyPatch, paths: RepoPaths) -> None:
    from leanfaith.cli import transformations

    config_path = paths.root / "configs/transformations/registry.yaml"
    profile_path = paths.root / "configs/transformations/v1.yaml"
    policy_path = paths.root / "policies/transformation_promotion_v1.yaml"
    loaded = SimpleNamespace(
        config=_Config(families=("p01", "n01"), rules=("p01", "n01")),
        profile=_Profile(profile_id="v1"),
        registry_path=config_path,
        profile_path=profile_path,
        promotion_policy_path=policy_path,
        registry_config_hash="1" * 64,
        profile_config_hash="2" * 64,
        promotion_policy_hash="7" * 64,
        registry_hash="3" * 64,
    )
    benchmark_path = paths.root / "data/benchmarks/manifests/representation_signatures_v1.json"
    monkeypatch.setattr(transformations, "load_transformation_registry", lambda *a, **kw: loaded)
    monkeypatch.setattr(
        transformations,
        "load_active_benchmark_registry",
        lambda **kw: SimpleNamespace(manifest_path=benchmark_path),
    )
    monkeypatch.setattr(
        transformations,
        "collect_code_state",
        lambda root: CodeState(
            git_revision="4" * 40,
            git_dirty=False,
            base_git_commit="4" * 40,
            code_tree_hash="5" * 64,
            tracked_diff_hash="6" * 64,
        ),
    )
    monkeypatch.setattr(
        transformations, "new_run_id", lambda created_at: "run_20260723T120000Z_deadbeef"
    )


def test_validation_only_writes_snapshot_report_and_run_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = _fixture(tmp_path)
    _patch_dependencies(monkeypatch, paths)

    result = validate_transformation_framework(paths=paths)

    report = read_manifest(result.report_path, TransformationFrameworkReport)
    manifest = read_manifest(result.run_manifest_path, RunManifest)
    assert report.generated_drafts == 0
    assert not report.gate_4g_closed
    assert report.configured_family_count == 2
    assert report.configured_rule_count == 2
    assert manifest.status_counts["generated_drafts"] == 0
    assert hash_file(result.snapshot_path) == result.snapshot_sha256
    assert hash_file(result.run_manifest_path) == result.run_manifest_sha256


def test_tampered_authorized_gate_fails_closed_with_structured_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = _fixture(tmp_path)
    _patch_dependencies(monkeypatch, paths)
    _write(paths.root / "reports/gates/gate_2.json", '{"decision":"tampered"}\n')

    with pytest.raises(TransformationFrameworkValidationError) as caught:
        validate_transformation_framework(paths=paths)

    failure = read_manifest(caught.value.report_path, TransformationFrameworkFailure)
    run = read_manifest(caught.value.run_manifest_path, RunManifest)
    assert not failure.mechanical_pass
    assert "hash mismatch" in failure.detail
    assert run.status_counts == {
        "checks_passed": 0,
        "checks_failed": 1,
        "generated_drafts": 0,
    }


def test_authorization_cannot_call_a_failed_gate_artifact_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, authorization_path = _fixture(tmp_path)
    _patch_dependencies(monkeypatch, paths)
    failed_gate_hash = _write(
        paths.root / "reports/gates/gate_2.json",
        '{"decision":"fail","gate":"gate_2"}\n',
    )
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    authorization["prerequisites"]["gate_2"]["sha256"] = failed_gate_hash
    _write(authorization_path, json.dumps(authorization, sort_keys=True) + "\n")

    with pytest.raises(TransformationFrameworkValidationError) as caught:
        validate_transformation_framework(paths=paths)

    failure = read_manifest(caught.value.report_path, TransformationFrameworkFailure)
    assert "not its own passed gate report" in failure.detail


def test_outside_report_path_fails_without_writing_outside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = _fixture(tmp_path)
    _patch_dependencies(monkeypatch, paths)
    outside = tmp_path.parent / f"{tmp_path.name}_outside.json"
    outside.unlink(missing_ok=True)

    with pytest.raises(TransformationFrameworkValidationError) as caught:
        validate_transformation_framework(paths=paths, report_path=outside)

    assert not outside.exists()
    assert caught.value.report_path == (
        paths.root / "reports/transformation_audits/lf016_registry_validation.json"
    )
    failure = read_manifest(caught.value.report_path, TransformationFrameworkFailure)
    assert "stay inside" in failure.detail


def test_cli_requires_one_explicit_deterministic_action() -> None:
    result = CliRunner().invoke(app, ["generate-deterministic"])

    assert result.exit_code == 2
    assert "choose one deterministic action" in result.output
    assert "--materialize-scale" in result.output


def test_cli_rejects_both_validation_modes() -> None:
    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--validate-only",
            "--validate-positives",
        ],
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_cli_invalid_registry_exits_nonzero_and_records_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = _fixture(tmp_path)
    _patch_dependencies(monkeypatch, paths)
    from leanfaith.cli import transformations

    monkeypatch.setattr(
        transformations,
        "load_transformation_registry",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid registry fixture")),
    )
    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--validate-only",
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "invalid registry fixture" in result.output
    failure = read_manifest(
        tmp_path / "reports/transformation_audits/lf016_registry_validation.json",
        TransformationFrameworkFailure,
    )
    assert failure.generated_drafts == 0
