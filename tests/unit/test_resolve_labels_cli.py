"""Stable diagnostic-only LF-024 CLI registration tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.cli.resolve_labels import (
    LabelResolutionBatchArtifacts,
    LabelResolutionBatchInputError,
)
from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.config.paths import RepoPaths, RepoRootNotFoundError
from leanfaith.schemas.enums import (
    ArtifactClass,
    Decision,
    QualityTier,
    ResolutionOutcome,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.label import ResolvedLabel
from leanfaith.schemas.manifest import CodeState, RunManifest, read_manifest
from leanfaith.schemas.pair import PairRecord

REPO_ROOT = Path(__file__).resolve().parents[2]


def _artifacts(tmp_path: Path) -> LabelResolutionBatchArtifacts:
    output = tmp_path / "diagnostic-output"
    return LabelResolutionBatchArtifacts(
        run_id="run_20260811T130000Z_lf024cli",
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        output_dir=output,
        linked_targets_path=output / "pairs.jsonl",
        labels_path=output / "labels.jsonl",
        audits_path=output / "resolution_audits.jsonl",
        derivations_path=output / "evidence_derivations.jsonl",
        conflicts_path=output / "conflicts.jsonl",
        overrides_path=output / "overrides.jsonl",
        run_manifest_path=tmp_path / "runs" / "lf024" / "manifest.json",
        run_manifest_sha256="a" * 64,
        target_count=7,
        resolved_count=3,
        unresolved_count=4,
        derivation_count=7,
        conflict_count=2,
        override_count=1,
    )


def _required_args(tmp_path: Path) -> list[str]:
    return [
        "resolve-labels",
        "--targets",
        str(tmp_path / "targets.jsonl"),
        "--evidence",
        str(tmp_path / "evidence.jsonl"),
        "--admissions",
        str(tmp_path / "admissions.jsonl"),
        "--candidates",
        str(tmp_path / "candidates.jsonl"),
        "--root",
        str(tmp_path),
    ]


def _write_record(path: Path, record: PairRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(record.model_dump(mode="json")) + b"\n")


def test_resolve_labels_help_exposes_only_diagnostic_inputs() -> None:
    result = CliRunner().invoke(app, ["resolve-labels", "--help"])

    assert result.exit_code == 0, result.output
    for option in (
        "--targets",
        "--evidence",
        "--admissions",
        "--candidates",
        "--prior-labels",
        "--output-dir",
        "--root",
    ):
        assert option in result.output
    assert "--artifact-class" not in result.output
    assert "--production" not in result.output


def test_resolve_labels_dispatches_diagnostic_mode_and_reports_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.cli import resolve_labels as module

    observed: dict[str, object] = {}

    def fake_resolve_label_batch(**kwargs: object) -> LabelResolutionBatchArtifacts:
        observed.update(kwargs)
        return _artifacts(tmp_path)

    monkeypatch.setattr(module, "resolve_label_batch", fake_resolve_label_batch)
    prior_labels = tmp_path / "prior-labels.jsonl"
    output_dir = tmp_path / "requested-output"
    result = CliRunner().invoke(
        app,
        [
            *_required_args(tmp_path),
            "--prior-labels",
            str(prior_labels),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert observed["paths"] == RepoPaths(root=tmp_path)
    assert observed["target_path"] == tmp_path / "targets.jsonl"
    assert observed["evidence_path"] == tmp_path / "evidence.jsonl"
    assert observed["admission_path"] == tmp_path / "admissions.jsonl"
    assert observed["candidate_path"] == tmp_path / "candidates.jsonl"
    assert observed["prior_label_path"] == prior_labels
    assert observed["output_dir"] == output_dir
    assert observed["artifact_class"] is ArtifactClass.DIAGNOSTIC
    assert "targets=7" in result.output
    assert "resolved=3" in result.output
    assert "unresolved=4" in result.output
    assert "derivations=7" in result.output
    assert "conflicts=2" in result.output
    assert "overrides=1" in result.output
    assert f"manifest={_artifacts(tmp_path).run_manifest_path}" in result.output
    assert f"manifest_sha256={'a' * 64}" in result.output


def test_resolve_labels_optional_paths_default_to_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.cli import resolve_labels as module

    observed: dict[str, object] = {}

    def fake_resolve_label_batch(**kwargs: object) -> LabelResolutionBatchArtifacts:
        observed.update(kwargs)
        return _artifacts(tmp_path)

    monkeypatch.setattr(module, "resolve_label_batch", fake_resolve_label_batch)
    result = CliRunner().invoke(app, _required_args(tmp_path))

    assert result.exit_code == 0, result.output
    assert observed["prior_label_path"] is None
    assert observed["output_dir"] is None
    assert observed["artifact_class"] is ArtifactClass.DIAGNOSTIC


def test_resolve_labels_cli_runs_real_diagnostic_batch_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise Typer registration, operation, persistence, and manifest together."""

    from leanfaith.cli import resolve_labels as module

    for relative_path in (
        Path("policies/label_resolution_v1.yaml"),
        Path("reports/gates/gate_0.json"),
    ):
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative_path, destination)

    monkeypatch.setattr(
        module,
        "collect_code_state",
        lambda root: CodeState(
            git_revision="1" * 40,
            git_dirty=False,
            base_git_commit="1" * 40,
            code_tree_hash="2" * 64,
            tracked_diff_hash="3" * 64,
        ),
    )

    pair = PairRecord(
        pair_id=make_id("pair", {"fixture": "lf024-cli-end-to-end"}),
        theorem_a_id=make_id("thm", {"fixture": "lf024-cli-end-to-end-a"}),
        theorem_b_id=make_id("thm", {"fixture": "lf024-cli-end-to-end-b"}),
        pair_source="lf024_cli_end_to_end_fixture",
        split_group_ids=("group:lf024-cli-end-to-end",),
    )
    inputs = tmp_path / "inputs"
    target_path = inputs / "targets.jsonl"
    evidence_path = inputs / "evidence.jsonl"
    admission_path = inputs / "admissions.jsonl"
    candidate_path = inputs / "candidates.jsonl"
    _write_record(target_path, pair)
    for path in (evidence_path, admission_path, candidate_path):
        path.write_bytes(b"")
    output_dir = tmp_path / "diagnostic-output"

    result = CliRunner().invoke(
        app,
        [
            "resolve-labels",
            "--targets",
            str(target_path),
            "--evidence",
            str(evidence_path),
            "--admissions",
            str(admission_path),
            "--candidates",
            str(candidate_path),
            "--output-dir",
            str(output_dir),
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "targets=1" in result.output
    assert "resolved=0" in result.output
    assert "unresolved=1" in result.output
    label_payload = json.loads((output_dir / "labels.jsonl").read_text(encoding="utf-8"))
    label = ResolvedLabel.model_validate(label_payload)
    assert label.same_claim is None
    assert label.relation is None
    assert label.resolution_outcome is ResolutionOutcome.UNRESOLVED
    assert label.quality_tier is QualityTier.UNKNOWN
    assert label.requires_adjudication is True
    assert label.decision is Decision.REVIEW

    (manifest_path,) = tuple((tmp_path / "runs").glob("run_*/manifest.json"))
    manifest = read_manifest(manifest_path, RunManifest)
    assert manifest.artifact_class is ArtifactClass.DIAGNOSTIC
    assert manifest.execution["production_admission"] is False
    assert manifest.execution["candidate_inference"] is False
    assert manifest.execution["candidate_promotion"] is False


def test_resolve_labels_reports_missing_repository_root_as_exit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_root(cls: type[RepoPaths]) -> RepoPaths:
        del cls
        raise RepoRootNotFoundError("no LeanFaith repository root in fixture")

    monkeypatch.setattr(RepoPaths, "discover", classmethod(missing_root))
    result = CliRunner().invoke(
        app,
        [
            "resolve-labels",
            "--targets",
            "targets.jsonl",
            "--evidence",
            "evidence.jsonl",
            "--admissions",
            "admissions.jsonl",
            "--candidates",
            "candidates.jsonl",
        ],
    )

    assert result.exit_code == 1
    assert "LF-024 diagnostic label resolution rejected" in result.output
    assert "no LeanFaith repository root" in result.output


@pytest.mark.parametrize(
    "error",
    [
        LabelResolutionBatchInputError("closed graph is malformed"),
        OSError("input vanished"),
        ValueError("policy binding is stale"),
    ],
)
def test_resolve_labels_converts_operational_errors_to_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    from leanfaith.cli import resolve_labels as module

    def fail(**kwargs: object) -> LabelResolutionBatchArtifacts:
        del kwargs
        raise error

    monkeypatch.setattr(module, "resolve_label_batch", fail)
    result = CliRunner().invoke(app, _required_args(tmp_path))

    assert result.exit_code == 1
    assert "LF-024 diagnostic label resolution rejected" in result.output
    assert str(error) in result.output
