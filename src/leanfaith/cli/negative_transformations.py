"""LF-018 negative-rule construction validation and manifest emission.

This is a mechanical implementation inventory, not dataset generation and
not a Gate-4G closure.  It constructs every code-owned v1 negative rule,
including N10 through its explicit pair-aware contract, and binds the exact
registry/rule/table bytes into a reproducible report.  It deliberately emits
no draft, semantic label, promotion decision, or theorem pair.
"""

from __future__ import annotations

import datetime
import re
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
    Polarity,
    RunManifest,
    collect_code_state,
    new_run_id,
    run_manifest_path,
    write_manifest,
)
from leanfaith.transforms.negative_factory import build_negative_rule_runtime
from leanfaith.transforms.negatives.n10_nearby_theorem import N10NearbyTheoremRule
from leanfaith.transforms.protocol import PairTransformationRule
from leanfaith.transforms.registry import (
    LoadedTransformationRegistry,
    RuleImplementationStatus,
    TransformationRuleConfig,
    load_transformation_registry,
)

_HEX64 = r"^[0-9a-f]{64}$"
_DEFAULT_REPORT = Path("reports/transformation_audits/lf018_negative_validation.json")
_REPLACEMENT_TABLE = Path("configs/transformations/replacement_table_v1.yaml")
_NEGATIVE_CONFIGS = (
    Path("configs/transformations/n01_operator.yaml"),
    Path("configs/transformations/n02_quantifier.yaml"),
    Path("configs/transformations/n03_drop_hypothesis.yaml"),
    Path("configs/transformations/n07_literal_bound.yaml"),
    Path("configs/transformations/n10_nearby_theorem.yaml"),
)
_EXPECTED_UNARY_RULES = (
    "n01_operator",
    "n02_quantifier",
    "n03_drop_hypothesis",
    "n07_literal_bound",
)
_EXPECTED_PAIR_RULES = ("n10_nearby_theorem",)


class NegativeRuleValidationReport(StrictModel):
    """Mechanical LF-018 implementation inventory; never semantic gold."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf018_negative_rule_validation"] = "lf018_negative_rule_validation"
    mechanical_pass: Literal[True] = True
    registry_hash: str = Field(pattern=_HEX64)
    authorization_sha256: str = Field(pattern=_HEX64)
    active_benchmark_manifest_sha256: str = Field(pattern=_HEX64)
    registered_unary_rule_ids: tuple[str, ...]
    pair_aware_rule_ids: tuple[str, ...]
    rule_config_sha256: dict[str, str]
    replacement_table_sha256: str = Field(pattern=_HEX64)
    n10_canonical_rule_config_hash: str = Field(pattern=_HEX64)
    n10_canonical_replacement_table_hash: str = Field(pattern=_HEX64)
    checks: tuple[str, ...]
    generated_drafts: Literal[0] = 0
    generated_pairs: Literal[0] = 0
    resolved_semantic_labels: Literal[0] = 0
    promoted_items: Literal[0] = 0
    output_tier: Literal["provisional"] = "provisional"
    gate_4g_closed: Literal[False] = False
    gate_4a_closed: Literal[False] = False
    gate_4b_closed: Literal[False] = False


class NegativeRuleValidationFailure(StrictModel):
    """Structured fail-closed LF-018 implementation-check failure."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf018_negative_rule_validation_failure"] = (
        "lf018_negative_rule_validation_failure"
    )
    mechanical_pass: Literal[False] = False
    failure_type: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    generated_drafts: Literal[0] = 0
    generated_pairs: Literal[0] = 0
    resolved_semantic_labels: Literal[0] = 0
    promoted_items: Literal[0] = 0
    gate_4g_closed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class NegativeRuleValidationArtifacts:
    """Paths and hashes emitted by a successful LF-018 validation."""

    report_path: Path
    report_sha256: str
    run_manifest_path: Path
    run_manifest_sha256: str


class NegativeRuleValidationError(RuntimeError):
    """LF-018 validation failed after persisting a structured artifact."""

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
    failure = NegativeRuleValidationFailure(
        failure_type=type(exc).__name__,
        detail=str(exc) or "LF-018 negative-rule validation failed",
    )
    report_hash = _write_canonical_payload(failure, report_path)
    run_id = new_run_id(created_at)
    manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.DIAGNOSTIC,
        command="leanfaith generate-deterministic --validate-negatives",
        argv=("leanfaith", "generate-deterministic", "--validate-negatives"),
        code=collect_code_state(paths.root),
        output_hashes={str(report_path.relative_to(paths.root)): report_hash},
        status_counts={
            "checks_passed": 0,
            "checks_failed": 1,
            "generated_drafts": 0,
            "generated_pairs": 0,
            "resolved_semantic_labels": 0,
            "promoted_items": 0,
        },
        created_at=created_at,
        notes=(
            "LF-018 negative-rule validation failed closed; no drafts, pairs, "
            "labels, or promotion decisions were generated."
        ),
    )
    manifest_path = run_manifest_path(paths, run_id)
    write_manifest(manifest, manifest_path)
    return report_path, manifest_path


def _find_rule(
    loaded: LoadedTransformationRegistry,
    rule_id: str,
) -> TransformationRuleConfig:
    matches = tuple(
        rule
        for family in loaded.config.families
        for rule in family.rules
        if rule.rule_id == rule_id
    )
    if len(matches) != 1:
        raise ValueError(f"expected exactly one registry entry for {rule_id!r}")
    return matches[0]


def _validate_n10_pair_rule(
    loaded: LoadedTransformationRegistry,
    *,
    pair_rules: tuple[PairTransformationRule, ...],
) -> N10NearbyTheoremRule:
    configured = _find_rule(loaded, "n10_nearby_theorem")
    if configured.implementation_status != RuleImplementationStatus.AVAILABLE:
        raise ValueError("scoped pair-aware rule n10_nearby_theorem is not available")
    matches = tuple(rule for rule in pair_rules if rule.rule_id == configured.rule_id)
    if len(matches) != 1:
        raise ValueError(
            "static negative factory must construct exactly one n10_nearby_theorem "
            "pair implementation"
        )
    implementation = matches[0]
    if not isinstance(implementation, N10NearbyTheoremRule):
        raise TypeError("static negative factory returned a non-code-owned N10 implementation")
    if not isinstance(implementation, PairTransformationRule):
        raise TypeError("n10_nearby_theorem does not implement PairTransformationRule")
    mismatches = tuple(
        field_name
        for field_name, expected in (
            ("rule_id", configured.rule_id),
            ("rule_version", configured.rule_version),
            ("family_id", configured.family_id),
            ("polarity", Polarity.NEGATIVE),
            ("implementation_key", configured.implementation_key),
            ("generation_config_hash", loaded.registry_hash),
        )
        if getattr(implementation, field_name, None) != expected
    )
    if mismatches:
        raise ValueError("n10 pair-aware implementation metadata mismatch: " + ",".join(mismatches))
    for field_name in ("rule_config_hash", "table_hash", "audit_config_hash"):
        digest = getattr(implementation, field_name, None)
        if not isinstance(digest, str) or re.fullmatch(_HEX64, digest) is None:
            raise ValueError(f"n10 pair-aware implementation has invalid {field_name}")
    return implementation


def validate_negative_rule_implementations(
    *,
    paths: RepoPaths,
    report_path: Path | None = None,
) -> NegativeRuleValidationArtifacts:
    """Construct all scoped negatives and emit a hash-bound zero-output report."""

    created_at = datetime.datetime.now(tz=datetime.UTC)
    effective_report = report_path or paths.root / _DEFAULT_REPORT
    if not effective_report.is_absolute():
        effective_report = paths.root / effective_report
    effective_report = effective_report.resolve()
    if not effective_report.is_relative_to(paths.root.resolve()):
        path_error = ValueError("LF-018 report path must stay inside the repository")
        safe_report = (paths.root / _DEFAULT_REPORT).resolve()
        failure_report, failure_manifest = _write_failure(
            paths=paths,
            report_path=safe_report,
            exc=path_error,
            created_at=created_at,
        )
        raise NegativeRuleValidationError(
            str(path_error),
            report_path=failure_report,
            run_manifest_path=failure_manifest,
        ) from path_error

    try:
        authorization_path, authorization_hash, gate_2_hash, gate_3_hash = _validate_authorization(
            paths.root
        )
        loaded = load_transformation_registry(paths.root)
        registration = build_negative_rule_runtime(loaded)
        if registration.registered_rule_ids != _EXPECTED_UNARY_RULES:
            raise ValueError(
                "scoped unary negative inventory mismatch: "
                f"expected={_EXPECTED_UNARY_RULES}, got={registration.registered_rule_ids}"
            )
        if registration.pair_aware_rule_ids != _EXPECTED_PAIR_RULES:
            raise ValueError(
                "scoped pair-aware negative inventory mismatch: "
                f"expected={_EXPECTED_PAIR_RULES}, got={registration.pair_aware_rule_ids}"
            )
        n10 = _validate_n10_pair_rule(
            loaded,
            pair_rules=registration.pair_rules,
        )

        benchmark = load_active_benchmark_registry(
            repo_root=paths.root,
            authorization_path=authorization_path,
        )
        benchmark_hash = hash_file(benchmark.manifest_path)

        config_hashes: dict[str, str] = {}
        for relative in _NEGATIVE_CONFIGS:
            path = (paths.root / relative).resolve()
            if not path.is_relative_to(paths.root.resolve()):
                raise ValueError(f"negative config path escapes repository: {relative}")
            config_hashes[str(relative)] = hash_file(path)
        table_path = (paths.root / _REPLACEMENT_TABLE).resolve()
        if not table_path.is_relative_to(paths.root.resolve()):
            raise ValueError("negative replacement-table path escapes repository")
        table_file_hash = hash_file(table_path)

        report = NegativeRuleValidationReport(
            registry_hash=loaded.registry_hash,
            authorization_sha256=authorization_hash,
            active_benchmark_manifest_sha256=benchmark_hash,
            registered_unary_rule_ids=registration.registered_rule_ids,
            pair_aware_rule_ids=registration.pair_aware_rule_ids,
            rule_config_sha256=config_hashes,
            replacement_table_sha256=table_file_hash,
            n10_canonical_rule_config_hash=n10.rule_config_hash,
            n10_canonical_replacement_table_hash=n10.table_hash,
            checks=(
                "all_scoped_unary_negative_rules_construct_from_static_code",
                "n10_constructs_through_explicit_pair_aware_contract",
                "all_scoped_negative_rules_bind_effective_registry_hash",
                "all_rule_configs_and_shared_replacement_table_are_hash_bound",
                "negative_outputs_remain_semantically_provisional",
                "failed_proof_search_is_never_negative_evidence",
                "generation_intention_is_never_a_resolved_label",
                "zero_drafts_pairs_labels_and_promotions_in_validation_command",
                "gate_4g_remains_open_pending_integrated_generation_audit",
            ),
        )
        report_hash = _write_canonical_payload(report, effective_report)
        run_id = new_run_id(created_at)
        manifest = RunManifest(
            run_id=run_id,
            artifact_class=ArtifactClass.DIAGNOSTIC,
            command="leanfaith generate-deterministic --validate-negatives",
            argv=("leanfaith", "generate-deterministic", "--validate-negatives"),
            code=collect_code_state(paths.root),
            config_hashes={
                str(loaded.registry_path.relative_to(paths.root)): loaded.registry_config_hash,
                **config_hashes,
                str(_REPLACEMENT_TABLE): table_file_hash,
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
                "registered_unary_negative_rules": len(registration.registered_rule_ids),
                "registered_pair_aware_negative_rules": len(registration.pair_aware_rule_ids),
                "checks_passed": len(report.checks),
                "checks_failed": 0,
                "generated_drafts": 0,
                "generated_pairs": 0,
                "resolved_semantic_labels": 0,
                "promoted_items": 0,
            },
            created_at=created_at,
            notes=(
                "LF-018 scoped negative implementations validated mechanically. "
                "Rule-specific LeanInteract/property tests provide semantic checks; "
                "this command creates no drafts, pairs, labels, or promotion decisions "
                "and does not close Gate 4G."
            ),
        )
        manifest_path = run_manifest_path(paths, run_id)
        manifest_hash = write_manifest(manifest, manifest_path)
        return NegativeRuleValidationArtifacts(
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
        raise NegativeRuleValidationError(
            str(exc) or type(exc).__name__,
            report_path=failure_report,
            run_manifest_path=failure_manifest,
        ) from exc


__all__ = [
    "NegativeRuleValidationArtifacts",
    "NegativeRuleValidationError",
    "NegativeRuleValidationFailure",
    "NegativeRuleValidationReport",
    "validate_negative_rule_implementations",
]
