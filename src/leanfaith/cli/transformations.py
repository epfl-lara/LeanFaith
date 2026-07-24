"""LF-016 transformation-framework validation command.

This module deliberately generates no theorem variants. It validates and
hash-binds the generic protocol/registry/promotion boundary so LF-017 and
LF-018 can add scoped rule implementations without weakening the gate
preconditions.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from leanfaith.config.hashing import canonical_json_bytes, hash_file
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
from leanfaith.transforms.registry import load_transformation_registry

_HEX64 = r"^[0-9a-f]{64}$"
_DEFAULT_REPORT = Path("reports/transformation_audits/lf016_registry_validation.json")
_AUTHORIZATION = Path("reports/gates/lf_016_authorization.json")
_REGISTRY = Path("configs/transformations/registry.yaml")
_PROFILE = Path("configs/transformations/v1.yaml")
_PROMOTION_POLICY = Path("policies/transformation_promotion_v1.yaml")


class TransformationFrameworkSnapshot(StrictModel):
    """Deterministic, hash-addressed LF-016 registry snapshot."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["transformation_registry_snapshot"] = "transformation_registry_snapshot"
    registry_hash: str = Field(pattern=_HEX64)
    registry_config_hash: str = Field(pattern=_HEX64)
    profile_config_hash: str = Field(pattern=_HEX64)
    promotion_policy_hash: str = Field(pattern=_HEX64)
    promotion_policy_file_sha256: str = Field(pattern=_HEX64)
    authorization_sha256: str = Field(pattern=_HEX64)
    gate_2_sha256: str = Field(pattern=_HEX64)
    gate_3_sha256: str = Field(pattern=_HEX64)
    active_benchmark_manifest_sha256: str = Field(pattern=_HEX64)
    registry: dict[str, object]
    profile: dict[str, object]
    generated_drafts: Literal[0] = 0


class TransformationFrameworkReport(StrictModel):
    """Mechanical LF-016 acceptance report; never a promotion decision."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf016_framework_validation"] = "lf016_framework_validation"
    mechanical_pass: Literal[True] = True
    registry_hash: str = Field(pattern=_HEX64)
    registry_config_hash: str = Field(pattern=_HEX64)
    profile_config_hash: str = Field(pattern=_HEX64)
    promotion_policy_hash: str = Field(pattern=_HEX64)
    promotion_policy_file_sha256: str = Field(pattern=_HEX64)
    authorization_sha256: str = Field(pattern=_HEX64)
    active_benchmark_manifest_sha256: str = Field(pattern=_HEX64)
    registry_snapshot_sha256: str = Field(pattern=_HEX64)
    configured_family_count: int = Field(ge=1)
    configured_rule_count: int = Field(ge=1)
    checks: tuple[str, ...]
    generated_drafts: Literal[0] = 0
    gate_4g_closed: Literal[False] = False
    gate_4a_closed: Literal[False] = False
    gate_4b_closed: Literal[False] = False


class TransformationFrameworkFailure(StrictModel):
    """Structured fail-closed artifact for an LF-016 validation attempt."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf016_framework_validation_failure"] = (
        "lf016_framework_validation_failure"
    )
    mechanical_pass: Literal[False] = False
    failure_type: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    generated_drafts: Literal[0] = 0


@dataclass(frozen=True, slots=True)
class TransformationValidationArtifacts:
    """Paths and hashes emitted by a successful validation-only run."""

    snapshot_path: Path
    snapshot_sha256: str
    report_path: Path
    report_sha256: str
    run_manifest_path: Path
    run_manifest_sha256: str


class TransformationFrameworkValidationError(RuntimeError):
    """Validation failed after a structured artifact was persisted."""

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


def _load_json_object(path: Path) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> object:
        raise ValueError(f"non-finite JSON constant {value!r}")

    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_nonfinite,
    )
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _bound_prerequisite(
    root: Path,
    raw: dict[str, object],
    key: str,
) -> tuple[Path, str]:
    prerequisites = raw.get("prerequisites")
    if not isinstance(prerequisites, dict):
        raise ValueError("LF-016 authorization has no prerequisites object")
    entry = prerequisites.get(key)
    if not isinstance(entry, dict):
        raise ValueError(f"LF-016 authorization has no {key!r} prerequisite")
    path_value = entry.get("path")
    digest = entry.get("sha256")
    decision = entry.get("decision")
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"LF-016 prerequisite {key!r} has no path")
    if not isinstance(digest, str) or re.fullmatch(_HEX64, digest) is None:
        raise ValueError(f"LF-016 prerequisite {key!r} has no SHA-256")
    if decision != "pass":
        raise ValueError(f"LF-016 prerequisite {key!r} is not passed")
    expected_relative = Path("reports/gates") / f"{key}.json"
    if Path(path_value) != expected_relative:
        raise ValueError(
            f"LF-016 prerequisite {key!r} must use {expected_relative}, got {path_value}"
        )
    path = (root / path_value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"LF-016 prerequisite {key!r} escapes the repository")
    if hash_file(path) != digest:
        raise ValueError(f"LF-016 prerequisite {key!r} hash mismatch")
    gate_payload = _load_json_object(path)
    if gate_payload.get("gate") != key or gate_payload.get("decision") != "pass":
        raise ValueError(f"LF-016 prerequisite {key!r} artifact is not its own passed gate report")
    return path, digest


def _validate_authorization(root: Path) -> tuple[Path, str, str, str]:
    path = (root / _AUTHORIZATION).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("LF-016 authorization path escapes the repository")
    raw = _load_json_object(path)
    if raw.get("decision") != "pass" or raw.get("lf_016_authorized") is not True:
        raise ValueError("LF-016 authorization is not a pass")
    _, gate_2_hash = _bound_prerequisite(root, raw, "gate_2")
    _, gate_3_hash = _bound_prerequisite(root, raw, "gate_3")
    evidence = raw.get("evidence")
    evidence_hash = raw.get("evidence_sha256")
    if not isinstance(evidence, str) or not isinstance(evidence_hash, str):
        raise ValueError("LF-016 authorization has no bound evidence")
    evidence_path = (root / evidence).resolve()
    if (
        not evidence_path.is_relative_to(root.resolve())
        or hash_file(evidence_path) != evidence_hash
    ):
        raise ValueError("LF-016 authorization evidence hash mismatch")
    return path, hash_file(path), gate_2_hash, gate_3_hash


def _write_canonical_payload(payload: StrictModel, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(payload.model_dump(mode="json")) + b"\n")
    return hash_file(path)


def _write_failure_run(
    *,
    paths: RepoPaths,
    report_path: Path,
    exc: Exception,
    created_at: datetime.datetime,
) -> tuple[Path, Path]:
    failure = TransformationFrameworkFailure(
        failure_type=type(exc).__name__,
        detail=str(exc) or "LF-016 transformation validation failed",
    )
    failure_hash = _write_canonical_payload(failure, report_path)
    run_id = new_run_id(created_at)
    manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.DIAGNOSTIC,
        command="leanfaith generate-deterministic --validate-only",
        argv=("leanfaith", "generate-deterministic", "--validate-only"),
        code=collect_code_state(paths.root),
        output_hashes={str(report_path.relative_to(paths.root)): failure_hash},
        status_counts={"checks_passed": 0, "checks_failed": 1, "generated_drafts": 0},
        created_at=created_at,
        notes="LF-016 validation failed closed; no theorem variants were generated.",
    )
    manifest_path = run_manifest_path(paths, run_id)
    write_manifest(manifest, manifest_path)
    return report_path, manifest_path


def validate_transformation_framework(
    *,
    paths: RepoPaths,
    report_path: Path | None = None,
) -> TransformationValidationArtifacts:
    """Validate/freeze LF-016 infrastructure while generating zero drafts."""

    created_at = datetime.datetime.now(tz=datetime.UTC)
    effective_report = report_path or paths.root / _DEFAULT_REPORT
    if not effective_report.is_absolute():
        effective_report = paths.root / effective_report
    effective_report = effective_report.resolve()
    if not effective_report.is_relative_to(paths.root.resolve()):
        path_error = ValueError("LF-016 report path must stay inside the repository")
        safe_report = (paths.root / _DEFAULT_REPORT).resolve()
        failure_report, failure_manifest = _write_failure_run(
            paths=paths,
            report_path=safe_report,
            exc=path_error,
            created_at=created_at,
        )
        raise TransformationFrameworkValidationError(
            str(path_error),
            report_path=failure_report,
            run_manifest_path=failure_manifest,
        ) from path_error
    try:
        auth_path, authorization_hash, gate_2_hash, gate_3_hash = _validate_authorization(
            paths.root
        )
        loaded = load_transformation_registry(
            paths.root,
            registry_path=paths.root / _REGISTRY,
            profile_path=paths.root / _PROFILE,
            promotion_policy_path=paths.root / _PROMOTION_POLICY,
        )
        benchmark = load_active_benchmark_registry(
            repo_root=paths.root,
            authorization_path=auth_path,
        )
        registry_payload = loaded.config.model_dump(mode="json")
        profile_payload = loaded.profile.model_dump(mode="json")
        family_count = len(loaded.config.families)
        rule_count = loaded.config.rule_count
        policy_hash = loaded.promotion_policy_hash
        policy_file_hash = hash_file(loaded.promotion_policy_path)
        benchmark_manifest_hash = hash_file(benchmark.manifest_path)
        snapshot = TransformationFrameworkSnapshot(
            registry_hash=loaded.registry_hash,
            registry_config_hash=loaded.registry_config_hash,
            profile_config_hash=loaded.profile_config_hash,
            promotion_policy_hash=policy_hash,
            promotion_policy_file_sha256=policy_file_hash,
            authorization_sha256=authorization_hash,
            gate_2_sha256=gate_2_hash,
            gate_3_sha256=gate_3_hash,
            active_benchmark_manifest_sha256=benchmark_manifest_hash,
            registry=registry_payload,
            profile=profile_payload,
        )
        snapshot_path = (
            paths.reports
            / "transformation_audits"
            / "registry_snapshots"
            / f"{loaded.registry_hash}.json"
        )
        snapshot_hash = _write_canonical_payload(snapshot, snapshot_path)
        report = TransformationFrameworkReport(
            registry_hash=loaded.registry_hash,
            registry_config_hash=loaded.registry_config_hash,
            profile_config_hash=loaded.profile_config_hash,
            promotion_policy_hash=policy_hash,
            promotion_policy_file_sha256=policy_file_hash,
            authorization_sha256=authorization_hash,
            active_benchmark_manifest_sha256=benchmark_manifest_hash,
            registry_snapshot_sha256=snapshot_hash,
            configured_family_count=family_count,
            configured_rule_count=rule_count,
            checks=(
                "gate_2_and_gate_3_authorization_hashes_match",
                "promotion_policy_and_registry_validate",
                "profile_and_code_owned_implementation_keys_validate",
                "disabled_and_pending_rules_fail_closed",
                "active_benchmark_registry_preflight_passes",
                "registry_snapshot_is_hash_bound",
                "generation_intentions_are_not_semantic_labels",
                "zero_drafts_generated",
            ),
        )
        report_hash = _write_canonical_payload(report, effective_report)
        run_id = new_run_id(created_at)
        run_manifest = RunManifest(
            run_id=run_id,
            artifact_class=ArtifactClass.DIAGNOSTIC,
            command="leanfaith generate-deterministic --validate-only",
            argv=("leanfaith", "generate-deterministic", "--validate-only"),
            code=collect_code_state(paths.root),
            config_hashes={
                str(loaded.registry_path.relative_to(paths.root)): loaded.registry_config_hash,
                str(loaded.profile_path.relative_to(paths.root)): loaded.profile_config_hash,
                str(loaded.promotion_policy_path.relative_to(paths.root)): policy_hash,
            },
            input_hashes={
                str(auth_path.relative_to(paths.root)): authorization_hash,
                "reports/gates/gate_2.json": gate_2_hash,
                "reports/gates/gate_3.json": gate_3_hash,
                str(benchmark.manifest_path.relative_to(paths.root)): benchmark_manifest_hash,
                str(loaded.promotion_policy_path.relative_to(paths.root)): policy_file_hash,
            },
            output_hashes={
                str(snapshot_path.relative_to(paths.root)): snapshot_hash,
                str(effective_report.relative_to(paths.root)): report_hash,
            },
            status_counts={
                "configured_families": family_count,
                "configured_rules": rule_count,
                "checks_passed": len(report.checks),
                "checks_failed": 0,
                "generated_drafts": 0,
            },
            created_at=created_at,
            notes=(
                "LF-016 validation-only run. LF-017/LF-018 rule semantics and "
                "Gate 4G/4A/4B remain open."
            ),
        )
        manifest_path = run_manifest_path(paths, run_id)
        manifest_hash = write_manifest(run_manifest, manifest_path)
        return TransformationValidationArtifacts(
            snapshot_path=snapshot_path,
            snapshot_sha256=snapshot_hash,
            report_path=effective_report,
            report_sha256=report_hash,
            run_manifest_path=manifest_path,
            run_manifest_sha256=manifest_hash,
        )
    except Exception as exc:
        failure_report, failure_manifest = _write_failure_run(
            paths=paths,
            report_path=effective_report,
            exc=exc,
            created_at=created_at,
        )
        raise TransformationFrameworkValidationError(
            str(exc) or type(exc).__name__,
            report_path=failure_report,
            run_manifest_path=failure_manifest,
        ) from exc
