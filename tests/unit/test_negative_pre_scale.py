"""Focused contract tests for the LF-018 persisted negative pre-scale slice."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.cli.negative_pre_scale import (
    NegativePreScaleArtifacts,
    NegativePreScaleAuditError,
    NegativePreScaleCase,
    NegativePreScaleConfig,
    NegativePreScaleReport,
    run_negative_pre_scale_audit,
)
from leanfaith.config.loading import load_config
from leanfaith.config.paths import RepoPaths

_EXPECTED_RULES = (
    "n01_operator",
    "n02_quantifier",
    "n03_drop_hypothesis",
    "n07_literal_bound",
    "n10_nearby_theorem",
)


def _case(
    rule_id: str,
    *,
    donor: bool = False,
) -> dict[str, object]:
    value: dict[str, object] = {
        "case_id": f"case_{rule_id}",
        "rule_id": rule_id,
        "primary_name": f"primary_{rule_id}",
        "primary_code": f"theorem primary_{rule_id} : True := by sorry",
    }
    if donor:
        value.update(
            donor_name="donor_n10",
            donor_code="theorem donor_n10 : True := by sorry",
        )
    return value


def _config_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "audit_profile_id": "lf018_pre_scale_v1",
        "audit_profile_version": "1.0.0",
        "project_dir": "tests/lean_fixtures",
        "imports": "",
        "seed": 18018,
        "record_timestamp_utc": "2026-07-23T00:00:00+00:00",
        "cases": [
            _case("n01_operator"),
            _case("n02_quantifier"),
            _case("n03_drop_hypothesis"),
            _case("n07_literal_bound"),
            _case("n10_nearby_theorem", donor=True),
        ],
    }


def _artifacts(root: Path) -> NegativePreScaleArtifacts:
    return NegativePreScaleArtifacts(
        output_dir=root / "data/generated/deterministic/lf018_pre_scale_v1",
        output_manifest_path=root / "data/generated/deterministic/lf018_pre_scale_v1/manifest.json",
        output_manifest_sha256="a" * 64,
        report_path=root / "reports/transformation_audits/lf018_pre_scale_audit.json",
        report_sha256="b" * 64,
        run_manifest_path=root / "runs/run_fixture/manifest.json",
        run_manifest_sha256="c" * 64,
    )


def test_checked_in_pre_scale_config_has_exact_scoped_inventory() -> None:
    paths = RepoPaths.discover()
    loaded = load_config(
        paths.root / "configs/transformations/lf018_pre_scale_v1.yaml",
        NegativePreScaleConfig,
    )

    assert tuple(case.rule_id for case in loaded.config.cases) == _EXPECTED_RULES
    assert len(loaded.config.cases) == 5
    assert loaded.config.record_timestamp == datetime.datetime(
        2026,
        7,
        23,
        tzinfo=datetime.UTC,
    )
    n10 = loaded.config.cases[-1]
    assert n10.donor_name is not None
    assert n10.donor_code is not None


def test_pre_scale_config_rejects_missing_or_duplicate_family() -> None:
    payload = _config_payload()
    cases = payload["cases"]
    assert isinstance(cases, list)
    cases[-1] = cases[0]

    with pytest.raises(ValidationError, match="cases must contain exactly"):
        NegativePreScaleConfig.model_validate(payload)


@pytest.mark.parametrize(
    ("rule_id", "donor", "message"),
    [
        ("n01_operator", True, "only N10 requires"),
        ("n10_nearby_theorem", False, "only N10 requires"),
    ],
)
def test_pre_scale_case_enforces_dual_source_n10_only(
    rule_id: str,
    donor: bool,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        NegativePreScaleCase.model_validate(_case(rule_id, donor=donor))


def test_pre_scale_config_rejects_non_utc_record_timestamp() -> None:
    payload = _config_payload()
    payload["record_timestamp_utc"] = "2026-07-23T00:00:00+01:00"

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        NegativePreScaleConfig.model_validate(payload)


def test_pre_scale_report_requires_one_terminal_outcome_per_case() -> None:
    with pytest.raises(
        ValidationError,
        match="every configured case must have one terminal report outcome",
    ):
        NegativePreScaleReport(
            mechanical_pass=True,
            registry_hash="a" * 64,
            config_hash="b" * 64,
            transformation_input_hashes={
                str(path): "f" * 64
                for path in (
                    Path("configs/transformations/n01_operator.yaml"),
                    Path("configs/transformations/n02_quantifier.yaml"),
                    Path("configs/transformations/n03_drop_hypothesis.yaml"),
                    Path("configs/transformations/n07_literal_bound.yaml"),
                    Path("configs/transformations/n10_nearby_theorem.yaml"),
                    Path("configs/transformations/replacement_table_v1.yaml"),
                )
            },
            authorization_sha256="c" * 64,
            active_benchmark_manifest_sha256="d" * 64,
            environment_lock_sha256="1" * 64,
            context_record_sha256="2" * 64,
            context_id="ctx:" + "3" * 64,
            output_manifest_path="data/generated/deterministic/manifest.json",
            output_manifest_sha256="e" * 64,
            configured_case_count=5,
            successful_case_count=0,
            failure_count=0,
            case_results=(),
            failure_records=(),
            generated_drafts=0,
            generated_pairs=0,
            check_results={
                "all_scoped_negative_families_executed": False,
                "source_and_candidate_views_lean_backed": False,
                "candidate_statements_reelaborated": False,
                "attempt_draft_audit_variant_pair_lineage_complete": False,
                "n10_dual_source_ancestry_persisted": False,
                "all_outputs_provisional": False,
                "zero_resolved_semantic_labels": True,
                "zero_promotions": True,
            },
            checks=("zero_resolved_semantic_labels", "zero_promotions"),
        )


def test_pre_scale_rejects_paths_outside_repository(tmp_path: Path) -> None:
    paths = RepoPaths(root=tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}_outside"

    with pytest.raises(ValueError, match="must stay inside"):
        run_negative_pre_scale_audit(paths=paths, output_dir=outside)


def test_pre_scale_refuses_to_overwrite_nonempty_output(tmp_path: Path) -> None:
    paths = RepoPaths(root=tmp_path)
    output = tmp_path / "existing-output"
    output.mkdir()
    (output / "manifest.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="output directory is not empty"):
        run_negative_pre_scale_audit(paths=paths, output_dir=output)


def test_pre_scale_refuses_to_overwrite_existing_report(tmp_path: Path) -> None:
    paths = RepoPaths(root=tmp_path)
    report = tmp_path / "existing-report.json"
    report.write_text("{}\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="report already exists"):
        run_negative_pre_scale_audit(
            paths=paths,
            output_dir=tmp_path / "new-output",
            report_path=report,
        )


def test_cli_runs_negative_pre_scale_and_reports_unlabeled_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.cli import negative_pre_scale

    artifacts = _artifacts(tmp_path)
    seen: dict[str, object] = {}

    def fake_run(**kwargs: object) -> NegativePreScaleArtifacts:
        seen.update(kwargs)
        return artifacts

    monkeypatch.setattr(negative_pre_scale, "run_negative_pre_scale_audit", fake_run)
    output_dir = tmp_path / "custom-output"
    report_path = tmp_path / "custom-report.json"

    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--run-negative-pre-scale",
            "--root",
            str(tmp_path),
            "--output-dir",
            str(output_dir),
            "--report",
            str(report_path),
        ],
    )

    assert result.exit_code == 0
    assert "LF-018 pre-scale OK" in result.output
    assert "generated_drafts=5" in result.output
    assert "generated_pairs=5" in result.output
    assert "resolved_semantic_labels=0" in result.output
    assert "gate_4g_closed=false" in result.output
    assert seen["paths"] == RepoPaths(root=tmp_path)
    assert seen["output_dir"] == output_dir
    assert seen["report_path"] == report_path


def test_cli_reports_persisted_artifacts_when_pre_scale_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.cli import negative_pre_scale

    artifacts = _artifacts(tmp_path)

    def fake_run(**kwargs: object) -> NegativePreScaleArtifacts:
        del kwargs
        raise NegativePreScaleAuditError("one case failed", artifacts=artifacts)

    monkeypatch.setattr(negative_pre_scale, "run_negative_pre_scale_audit", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--run-negative-pre-scale",
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "LF-018 pre-scale FAILED: one case failed" in result.output
    assert str(artifacts.report_path) in result.output
    assert str(artifacts.output_manifest_path) in result.output
    assert str(artifacts.run_manifest_path) in result.output


def test_cli_rejects_pre_scale_with_validation_mode() -> None:
    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--run-negative-pre-scale",
            "--validate-negatives",
        ],
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
