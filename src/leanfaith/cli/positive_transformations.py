"""LF-017 positive-rule construction validation and manifest emission.

This command is a mechanical implementation checkpoint.  It constructs every
code-owned positive rule from the effective registry and binds the exact rule
configs into a reproducible report.  It deliberately emits no training pair,
semantic label, or promotion decision; live LeanInteract/property tests remain
the rule-specific semantic checks.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from leanfaith.cli.transformations import (
    _validate_authorization,
    _write_canonical_payload,
)
from leanfaith.config.hashing import hash_file
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets import load_active_benchmark_registry
from leanfaith.schemas import (
    ArtifactClass,
    RunManifest,
    collect_code_state,
    new_run_id,
    run_manifest_path,
    write_manifest,
)
from leanfaith.transforms.factory import build_positive_rule_runtime
from leanfaith.transforms.registry import load_transformation_registry

_HEX64 = r"^[0-9a-f]{64}$"
_DEFAULT_REPORT = Path("reports/transformation_audits/lf017_positive_validation.json")
_POSITIVE_CONFIGS = (
    Path("configs/transformations/p01_alpha.yaml"),
    Path("configs/transformations/p02_binders.yaml"),
    Path("configs/transformations/p04_notation_lite.yaml"),
)
_EXPECTED_RULES = ("p01_alpha", "p02_binders", "p04_notation_lite")


class PositiveRuleValidationReport(StrictModel):
    """Mechanical LF-017 implementation inventory; never semantic gold."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf017_positive_rule_validation"] = "lf017_positive_rule_validation"
    mechanical_pass: Literal[True] = True
    registry_hash: str = Field(pattern=_HEX64)
    authorization_sha256: str = Field(pattern=_HEX64)
    active_benchmark_manifest_sha256: str = Field(pattern=_HEX64)
    registered_rule_ids: tuple[str, ...]
    rule_config_hashes: dict[str, str]
    checks: tuple[str, ...]
    generated_drafts: Literal[0] = 0
    resolved_semantic_labels: Literal[0] = 0
    promoted_items: Literal[0] = 0
    output_tier: Literal["provisional"] = "provisional"
    gate_4g_closed: Literal[False] = False
    gate_4a_closed: Literal[False] = False


class PositiveRuleValidationFailure(StrictModel):
    """Structured fail-closed LF-017 implementation-check failure."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf017_positive_rule_validation_failure"] = (
        "lf017_positive_rule_validation_failure"
    )
    mechanical_pass: Literal[False] = False
    failure_type: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    generated_drafts: Literal[0] = 0
    resolved_semantic_labels: Literal[0] = 0


@dataclass(frozen=True, slots=True)
class PositiveRuleValidationArtifacts:
    """Paths and hashes emitted by a successful LF-017 validation."""

    report_path: Path
    report_sha256: str
    run_manifest_path: Path
    run_manifest_sha256: str


class PositiveRuleValidationError(RuntimeError):
    """LF-017 validation failed after persisting a structured artifact."""

    def __init__(
        self,
        detail: str,
        *,
        report_path: Path,
        run_manifest_path: Path,
    ) -> None:
        super().__init__(detail)
        self.report_path = report_path
        self.run_manifest_path = run_manifest_path


def _write_failure(
    *,
    paths: RepoPaths,
    report_path: Path,
    exc: Exception,
    created_at: datetime.datetime,
) -> tuple[Path, Path]:
    failure = PositiveRuleValidationFailure(
        failure_type=type(exc).__name__,
        detail=str(exc) or "LF-017 positive-rule validation failed",
    )
    report_hash = _write_canonical_payload(failure, report_path)
    run_id = new_run_id(created_at)
    manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.DIAGNOSTIC,
        command="leanfaith generate-deterministic --validate-positives",
        argv=("leanfaith", "generate-deterministic", "--validate-positives"),
        code=collect_code_state(paths.root),
        output_hashes={str(report_path.relative_to(paths.root)): report_hash},
        status_counts={
            "checks_passed": 0,
            "checks_failed": 1,
            "generated_drafts": 0,
            "resolved_semantic_labels": 0,
        },
        created_at=created_at,
        notes="LF-017 positive-rule validation failed closed; no drafts were generated.",
    )
    manifest_path = run_manifest_path(paths, run_id)
    write_manifest(manifest, manifest_path)
    return report_path, manifest_path


def validate_positive_rule_implementations(
    *,
    paths: RepoPaths,
    report_path: Path | None = None,
) -> PositiveRuleValidationArtifacts:
    """Construct every scoped positive and emit a hash-bound zero-label report."""

    created_at = datetime.datetime.now(tz=datetime.UTC)
    effective_report = report_path or paths.root / _DEFAULT_REPORT
    if not effective_report.is_absolute():
        effective_report = paths.root / effective_report
    effective_report = effective_report.resolve()
    if not effective_report.is_relative_to(paths.root.resolve()):
        path_error = ValueError("LF-017 report path must stay inside the repository")
        safe_report = (paths.root / _DEFAULT_REPORT).resolve()
        failure_report, failure_manifest = _write_failure(
            paths=paths,
            report_path=safe_report,
            exc=path_error,
            created_at=created_at,
        )
        raise PositiveRuleValidationError(
            str(path_error),
            report_path=failure_report,
            run_manifest_path=failure_manifest,
        ) from path_error

    try:
        authorization_path, authorization_hash, gate_2_hash, gate_3_hash = _validate_authorization(
            paths.root
        )
        loaded = load_transformation_registry(paths.root)
        registration = build_positive_rule_runtime(loaded)
        if registration.registered_rule_ids != _EXPECTED_RULES:
            raise ValueError(
                "scoped positive inventory mismatch: "
                f"expected={_EXPECTED_RULES}, got={registration.registered_rule_ids}"
            )
        benchmark = load_active_benchmark_registry(
            repo_root=paths.root,
            authorization_path=authorization_path,
        )
        benchmark_hash = hash_file(benchmark.manifest_path)
        config_hashes: dict[str, str] = {}
        for relative in _POSITIVE_CONFIGS:
            path = (paths.root / relative).resolve()
            if not path.is_relative_to(paths.root.resolve()):
                raise ValueError(f"positive config path escapes repository: {relative}")
            config_hashes[str(relative)] = hash_file(path)

        report = PositiveRuleValidationReport(
            registry_hash=loaded.registry_hash,
            authorization_sha256=authorization_hash,
            active_benchmark_manifest_sha256=benchmark_hash,
            registered_rule_ids=registration.registered_rule_ids,
            rule_config_hashes=config_hashes,
            checks=(
                "all_scoped_positive_rules_construct_from_static_code",
                "all_scoped_positive_rules_bind_effective_registry_hash",
                "pending_and_disabled_rules_remain_non_executable",
                "positive_outputs_remain_provisional",
                "no_generation_intention_is_a_resolved_label",
                "zero_drafts_and_zero_promotions_in_validation_command",
            ),
        )
        report_hash = _write_canonical_payload(report, effective_report)
        run_id = new_run_id(created_at)
        manifest = RunManifest(
            run_id=run_id,
            artifact_class=ArtifactClass.DIAGNOSTIC,
            command="leanfaith generate-deterministic --validate-positives",
            argv=("leanfaith", "generate-deterministic", "--validate-positives"),
            code=collect_code_state(paths.root),
            config_hashes={
                str(loaded.registry_path.relative_to(paths.root)): (loaded.registry_config_hash),
                **config_hashes,
            },
            input_hashes={
                str(authorization_path.relative_to(paths.root)): authorization_hash,
                "reports/gates/gate_2.json": gate_2_hash,
                "reports/gates/gate_3.json": gate_3_hash,
                str(benchmark.manifest_path.relative_to(paths.root)): benchmark_hash,
            },
            output_hashes={
                str(effective_report.relative_to(paths.root)): report_hash,
            },
            status_counts={
                "registered_positive_rules": len(registration.registered_rule_ids),
                "checks_passed": len(report.checks),
                "checks_failed": 0,
                "generated_drafts": 0,
                "resolved_semantic_labels": 0,
                "promoted_items": 0,
            },
            created_at=created_at,
            notes=(
                "LF-017 scoped positive implementations validated. Rule-specific "
                "LeanInteract/property tests provide semantic checks; this command "
                "creates no drafts, labels, or promotion decisions."
            ),
        )
        manifest_path = run_manifest_path(paths, run_id)
        manifest_hash = write_manifest(manifest, manifest_path)
        return PositiveRuleValidationArtifacts(
            report_path=effective_report,
            report_sha256=report_hash,
            run_manifest_path=manifest_path,
            run_manifest_sha256=manifest_hash,
        )
    except Exception as exc:
        failure_report, failure_manifest = _write_failure(
            paths=paths,
            report_path=effective_report,
            exc=exc,
            created_at=created_at,
        )
        raise PositiveRuleValidationError(
            str(exc) or type(exc).__name__,
            report_path=failure_report,
            run_manifest_path=failure_manifest,
        ) from exc


__all__ = [
    "PositiveRuleValidationArtifacts",
    "PositiveRuleValidationError",
    "PositiveRuleValidationFailure",
    "PositiveRuleValidationReport",
    "validate_positive_rule_implementations",
]
