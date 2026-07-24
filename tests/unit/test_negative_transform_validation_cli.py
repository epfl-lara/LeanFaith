"""LF-018 negative implementation-inventory command and manifest behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.cli.negative_transformations import (
    NegativeRuleValidationError,
    NegativeRuleValidationFailure,
    NegativeRuleValidationReport,
    validate_negative_rule_implementations,
)
from leanfaith.config.hashing import hash_file
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas import CodeState, Polarity, RunManifest, read_manifest
from leanfaith.transforms.negatives.n10_nearby_theorem import N10NearbyTheoremRule
from leanfaith.transforms.registry import RuleImplementationStatus

_HEX = "a" * 64
_RULE_IDS = (
    "n01_operator",
    "n02_quantifier",
    "n03_drop_hypothesis",
    "n07_literal_bound",
)
_CONFIGS = (
    "n01_operator.yaml",
    "n02_quantifier.yaml",
    "n03_drop_hypothesis.yaml",
    "n07_literal_bound.yaml",
    "n10_nearby_theorem.yaml",
    "replacement_table_v1.yaml",
)


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _n10_rule() -> N10NearbyTheoremRule:
    rule = object.__new__(N10NearbyTheoremRule)
    rule.generation_config_hash = _HEX
    rule.rule_version = "1.0.0"
    rule.rule_config_hash = "b" * 64
    rule.table_hash = "c" * 64
    rule.audit_config_hash = "d" * 64
    return rule


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    n10_status: RuleImplementationStatus = RuleImplementationStatus.AVAILABLE,
) -> RepoPaths:
    from leanfaith.cli import negative_transformations

    for filename in _CONFIGS:
        _write(
            tmp_path / "configs/transformations" / filename,
            f"# fixture {filename}\n",
        )
    registry_path = tmp_path / "configs/transformations/registry.yaml"
    benchmark_path = tmp_path / "data/benchmarks/manifests/representation_signatures_v1.json"
    authorization_path = tmp_path / "reports/gates/lf_016_authorization.json"
    _write(registry_path, "# registry fixture\n")
    _write(benchmark_path, '{"fixture":true}\n')
    _write(authorization_path, '{"decision":"pass"}\n')

    configured_n10 = SimpleNamespace(
        rule_id="n10_nearby_theorem",
        rule_version="1.0.0",
        family_id="n10_nearby_theorem",
        polarity=Polarity.NEGATIVE,
        implementation_key="n10_nearby_theorem",
        implementation_status=n10_status,
    )
    loaded = SimpleNamespace(
        config=SimpleNamespace(
            families=(SimpleNamespace(rules=(configured_n10,)),),
        ),
        registry_hash=_HEX,
        registry_config_hash="e" * 64,
        registry_path=registry_path,
    )
    n10 = _n10_rule()
    registration = SimpleNamespace(
        registered_rule_ids=_RULE_IDS,
        pair_aware_rule_ids=("n10_nearby_theorem",),
        pair_rules=(n10,) if n10_status == RuleImplementationStatus.AVAILABLE else (),
    )

    monkeypatch.setattr(
        negative_transformations,
        "_validate_authorization",
        lambda root: (authorization_path, "1" * 64, "2" * 64, "3" * 64),
    )
    monkeypatch.setattr(
        negative_transformations,
        "load_transformation_registry",
        lambda root: loaded,
    )
    monkeypatch.setattr(
        negative_transformations,
        "build_negative_rule_runtime",
        lambda value: registration,
    )
    monkeypatch.setattr(
        negative_transformations,
        "load_active_benchmark_registry",
        lambda **kwargs: SimpleNamespace(manifest_path=benchmark_path),
    )
    monkeypatch.setattr(
        negative_transformations,
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
        negative_transformations,
        "new_run_id",
        lambda created_at: "run_20260723T120000Z_deadbeef",
    )
    return RepoPaths(root=tmp_path)


def test_negative_validation_writes_hash_bound_zero_output_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _fixture(tmp_path, monkeypatch)

    result = validate_negative_rule_implementations(paths=paths)

    report = read_manifest(result.report_path, NegativeRuleValidationReport)
    manifest = read_manifest(result.run_manifest_path, RunManifest)
    assert report.registered_unary_rule_ids == _RULE_IDS
    assert report.pair_aware_rule_ids == ("n10_nearby_theorem",)
    assert len(report.rule_config_sha256) == 5
    assert report.n10_canonical_rule_config_hash == "b" * 64
    assert report.n10_canonical_replacement_table_hash == "c" * 64
    assert report.generated_drafts == 0
    assert report.generated_pairs == 0
    assert report.resolved_semantic_labels == 0
    assert report.promoted_items == 0
    assert not report.gate_4g_closed
    assert manifest.status_counts["registered_unary_negative_rules"] == 4
    assert manifest.status_counts["registered_pair_aware_negative_rules"] == 1
    assert manifest.status_counts["generated_drafts"] == 0
    assert manifest.status_counts["generated_pairs"] == 0
    assert hash_file(result.report_path) == result.report_sha256
    assert hash_file(result.run_manifest_path) == result.run_manifest_sha256


def test_negative_validation_fails_closed_when_n10_is_not_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _fixture(
        tmp_path,
        monkeypatch,
        n10_status=RuleImplementationStatus.PENDING,
    )

    with pytest.raises(NegativeRuleValidationError) as caught:
        validate_negative_rule_implementations(paths=paths)

    failure = read_manifest(caught.value.report_path, NegativeRuleValidationFailure)
    manifest = read_manifest(caught.value.run_manifest_path, RunManifest)
    assert "n10_nearby_theorem is not available" in failure.detail
    assert failure.generated_drafts == 0
    assert failure.generated_pairs == 0
    assert failure.resolved_semantic_labels == 0
    assert failure.promoted_items == 0
    assert not failure.gate_4g_closed
    assert manifest.status_counts["checks_failed"] == 1


def test_negative_validation_rejects_report_path_outside_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    outside = tmp_path.parent / f"{tmp_path.name}_lf018_outside.json"
    outside.unlink(missing_ok=True)

    with pytest.raises(NegativeRuleValidationError) as caught:
        validate_negative_rule_implementations(paths=paths, report_path=outside)

    assert not outside.exists()
    assert caught.value.report_path == (
        paths.root / "reports/transformation_audits/lf018_negative_validation.json"
    )
    failure = read_manifest(caught.value.report_path, NegativeRuleValidationFailure)
    assert "stay inside" in failure.detail


def test_cli_negative_validation_reports_zero_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.cli import negative_transformations

    paths = _fixture(tmp_path, monkeypatch)
    artifacts = validate_negative_rule_implementations(paths=paths)
    monkeypatch.setattr(
        negative_transformations,
        "validate_negative_rule_implementations",
        lambda **kwargs: artifacts,
    )

    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--validate-negatives",
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "LF-018 negatives OK" in result.output
    assert "generated_drafts=0" in result.output
    assert "generated_pairs=0" in result.output
    assert "resolved_semantic_labels=0" in result.output
    assert "gate_4g_closed=false" in result.output


def test_cli_rejects_positive_and_negative_validation_together() -> None:
    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--validate-positives",
            "--validate-negatives",
        ],
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
