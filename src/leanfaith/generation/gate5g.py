"""Fail-closed LF-021 Gate-5G frame finalization.

This module verifies a future CSPRNG-bound problem-aware frame and the entire
label-blind collection/postprocess/replay prefix before it can write the
canonical Gate-5G report.  Normal (dry-run) execution writes only a
content-addressed validation report below ``reports/generation``.  The
canonical ``reports/gates/gate_5g.json`` path is reachable only through the
explicit ``finalize=True`` branch.

Gate 5G is mechanical: it creates no semantic labels, admits no supervision,
and leaves Gate 5 open.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import stat
from collections import Counter, defaultdict
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.evaluation.prevalence import (
    PrevalenceInputError,
    load_prevalence_design_policy,
    verify_prevalence_design_policy_v2,
)
from leanfaith.generation.frame_freeze_v3 import (
    FrameFreezeDecisionV3,
    FrameFreezeV3Error,
    FrameItemV3,
    verify_frame_freeze_v3,
)
from leanfaith.schemas.gate5g import (
    Gate5GArtifactBinding,
    Gate5GInputBindings,
    Gate5GLineageManifestV1,
    Gate5GReFormApplicability,
    Gate5GReplayCertificateV1,
    Gate5GReportV1,
    Gate5GScopeLimitations,
    Gate5GValidationReportV1,
    ValidatedRealOutputsManifestV1,
)

_HEX64 = r"^[0-9a-f]{64}$"
_DEFAULT_POLICY = Path("configs/generation/lf021_gate5g_finalizer_v1.yaml")
_DEFAULT_PREVALENCE_DESIGN = Path("policies/lf021_prevalence_design_v2.yaml")
_GATE_PATH = Path("reports/gates/gate_5g.json")
_DRY_ROOT = Path("reports/generation/lf021_gate5g_finalization_v1")

GATE5G_CHECKS = (
    "preferred_frame_action",
    "preferred_frame_size_200_to_300",
    "zero_coverage_deficits",
    "problem_aware_unique_items",
    "csprng_seed_and_provenance_bound",
    "decision_policy_implementation_observations_hash_bound",
    "prevalence_design_v2_and_base_v1_bound",
    "frame_hash_and_count_bound",
    "all_selected_artifacts_hash_bound",
    "benchmark_clear_compiling_unresolved",
    "zero_semantic_labels",
    "zero_supervision",
    "family_pool_source_proxy_reconciled",
    "stratum_propensities_reconciled",
    "collection_denominators_reconciled",
    "postprocess_denominators_reconciled",
    "collection_replay_byte_identical",
    "postprocess_replay_byte_identical",
    "reform_applicability_explicit",
    "three_family_reduced_scope_explicit",
    "supplemental_families_excluded_from_gate_credit",
    "coverage_validated_phase_artifacts_finalized",
    "gate5_remains_open",
)


class Gate5GFinalizationError(ValueError):
    """Raised before any authoritative Gate-5G report can be emitted."""


class Gate5GFinalizerPolicyV1(StrictModel):
    """Frozen pre-label finalizer policy."""

    schema_version: Literal[1] = 1
    policy_id: Literal["lf021_gate5g_finalizer_v1"]
    status: Literal["frozen_prelabel"]
    required_decision_schema_version: Literal[3]
    required_v2_stop_action: Literal["freeze_preferred_frame"]
    required_prevalence_design_policy_id: Literal["lf021_prevalence_design_v2"]
    minimum_frame_size: int = Field(ge=200, le=300)
    maximum_frame_size: int = Field(ge=200, le=300)
    required_sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    required_sampling_rank_algorithm: Literal["hmac_sha256_keyed_rank_v1"]
    allowed_production_seed_sources: tuple[
        Literal["external_randomness_beacon_256"],
        Literal["os_csprng_secrets_token_bytes_256"],
    ]
    required_scalable_family_ids: tuple[str, str, str]
    required_scope_flags: dict[str, bool]
    required_reduced_data_reasons: tuple[str, str, str]
    dry_run_output_root: Literal["reports/generation/lf021_gate5g_finalization_v1"]
    canonical_gate_report: Literal["reports/gates/gate_5g.json"]
    semantic_labels_inspected_when_frozen: Literal[False]
    semantic_labels_created: Literal[False]
    supervision_eligible: Literal[False]
    gate_5_closed: Literal[False]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.minimum_frame_size > self.maximum_frame_size:
            raise ValueError("Gate-5G frame bounds are reversed")
        if self.allowed_production_seed_sources != (
            "external_randomness_beacon_256",
            "os_csprng_secrets_token_bytes_256",
        ):
            raise ValueError("Gate-5G has a noncanonical production seed-source inventory")
        if self.required_scalable_family_ids != tuple(
            sorted(set(self.required_scalable_family_ids))
        ):
            raise ValueError("Gate-5G requires three sorted unique families")
        expected_flags = {
            "three_family_collection_only": True,
            "reduced_data_ablation": True,
            "confirmatory_d4_d5_eligible": False,
            "heldout_generator_claim_eligible": False,
            "supplemental_qualifications_count_for_gate_credit": False,
        }
        if self.required_scope_flags != expected_flags:
            raise ValueError("Gate-5G has a noncanonical scope flag inventory")
        expected_reasons = (
            "confirmatory_d4_d5_unavailable",
            "heldout_generator_claim_unavailable",
            "three_family_collection_only",
        )
        if self.required_reduced_data_reasons != expected_reasons:
            raise ValueError("Gate-5G has noncanonical reduced-data reasons")
        return self


class _PostprocessTerminalProjection(BaseModel):
    model_config = ConfigDict(extra="allow")

    artifact_class: Literal["research"]
    invocation_id: str
    family_id: str
    problem_record_id: str
    status: Literal["admitted_unresolved"]
    parser_executed: Literal[True]
    lean_validation_executed: Literal[True]
    screening_executed: Literal[True]
    semantic_pool_admitted: Literal[True]
    output_artifact_hashes: dict[str, Annotated[str, Field(pattern=_HEX64)]]
    candidate_theorem_id: str
    same_claim: None
    relation: None
    resolution_outcome: Literal["unresolved"]
    quality_tier: Literal["unknown"]
    requires_adjudication: Literal[True]
    decision: Literal["REVIEW"]
    semantic_labels_created: Literal[False]
    supervision_eligible: Literal[False]
    gate_5g_credit_claimed: Literal[False]
    gate_5_closed: Literal[False]


class _ScreeningProjection(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidate_theorem_id: str
    problem_record_id: str
    alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    status: Literal["clean"]
    benchmark_hits: tuple[str, ...] | list[str] = ()

    @model_validator(mode="after")
    def _benchmark_clear(self) -> Self:
        if self.benchmark_hits:
            raise ValueError("selected candidate has a protected-benchmark hit")
        return self


class _RepresentationProjection(BaseModel):
    model_config = ConfigDict(extra="allow")

    theorem_id: str
    alpha_identity_fingerprint: str = Field(pattern=_HEX64)


class _CollectionManifestProjection(BaseModel):
    model_config = ConfigDict(extra="allow")

    manifest_id: str
    tranche_id: str
    expected_candidate_count: int = Field(ge=1)
    terminal_candidate_count: int = Field(ge=1)
    family_count: int = Field(ge=1)
    family_session_artifact_hashes: dict[str, Annotated[str, Field(pattern=_HEX64)]] = Field(
        min_length=2
    )
    terminal_artifact_hashes: dict[str, Annotated[str, Field(pattern=_HEX64)]]
    semantic_labels_created: Literal[False]
    gate_5g_credit_claimed: Literal[False]
    gate_5_closed: Literal[False]


class _PostprocessInputBindingProjection(BaseModel):
    model_config = ConfigDict(extra="allow")

    collection_manifest: Gate5GArtifactBinding
    collection_manifest_id: str


class _PostprocessManifestProjection(BaseModel):
    model_config = ConfigDict(extra="allow")

    manifest_id: str
    tranche_id: str
    expected_invocations: int = Field(ge=1)
    terminal_invocations: int = Field(ge=1)
    family_count: int = Field(ge=1)
    status_counts: dict[str, int]
    terminal_artifacts: dict[str, Annotated[str, Field(pattern=_HEX64)]]
    input_binding: _PostprocessInputBindingProjection
    semantic_labels_created: Literal[False]
    supervision_eligible: Literal[False]
    gate_5g_credit_claimed: Literal[False]
    gate_5_closed: Literal[False]


class _FamilySessionStartProjection(BaseModel):
    model_config = ConfigDict(extra="allow")

    family_id: str = Field(min_length=1)
    family_session_id: str = Field(min_length=1)
    model_repo_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)


class _FamilySessionEndProjection(BaseModel):
    model_config = ConfigDict(extra="allow")

    family_id: str = Field(min_length=1)
    family_session_id: str = Field(min_length=1)


class _OverlapManifestProjection(BaseModel):
    model_config = ConfigDict(extra="allow")

    family_artifacts: dict[str, Gate5GArtifactBinding] = Field(min_length=1)
    family_count: int = Field(ge=1)
    semantic_labels_created: Literal[False]
    gate_5g_credit_claimed: Literal[False]
    gate_5_closed: Literal[False]


class Gate5GFinalizationResult(StrictModel):
    """Paths emitted by one successful dry-run or explicit finalization."""

    validation_report_path: str
    validation_report_sha256: str = Field(pattern=_HEX64)
    validation_report: Gate5GValidationReportV1
    gate_report_path: str | None
    gate_report_sha256: str | None = Field(default=None, pattern=_HEX64)
    gate_report: Gate5GReportV1 | None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        has_gate = self.gate_report is not None
        if has_gate != (self.gate_report_path is not None) or has_gate != (
            self.gate_report_sha256 is not None
        ):
            raise ValueError("Gate-5G result has a partial gate report")
        return self


def load_gate5g_policy(
    path: Path,
) -> LoadedConfig[Gate5GFinalizerPolicyV1]:
    return load_config(path, Gate5GFinalizerPolicyV1)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Gate5GFinalizationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                Gate5GFinalizationError(f"non-finite JSON constant {value!r}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, Gate5GFinalizationError):
            raise
        raise Gate5GFinalizationError(f"cannot read strict JSON {path}: {exc}") from exc


def _repo_file(
    paths: RepoPaths,
    path: Path | str,
    *,
    label: str,
) -> tuple[Path, str]:
    root = paths.root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise Gate5GFinalizationError(f"{label} escapes the repository")
    if candidate.is_symlink() or not resolved.is_file():
        raise Gate5GFinalizationError(f"{label} is missing or symlinked: {candidate}")
    return resolved, str(resolved.relative_to(root))


def _binding(
    paths: RepoPaths,
    path: Path | str,
    *,
    label: str,
) -> tuple[Path, Gate5GArtifactBinding]:
    resolved, relative = _repo_file(paths, path, label=label)
    return resolved, Gate5GArtifactBinding(artifact=relative, sha256=hash_file(resolved))


def _verify_binding(
    paths: RepoPaths,
    binding: Gate5GArtifactBinding,
    *,
    label: str,
) -> Path:
    path, relative = _repo_file(paths, binding.artifact, label=label)
    if relative != binding.artifact:
        raise Gate5GFinalizationError(f"{label} path is not canonical repository-relative")
    observed = hash_file(path)
    if observed != binding.sha256:
        raise Gate5GFinalizationError(
            f"{label} hash mismatch: expected {binding.sha256}, observed {observed}"
        )
    return path


def _gate_binding(value: Any) -> Gate5GArtifactBinding:
    """Normalize another strict artifact-binding model to the Gate-5G schema."""

    if isinstance(value, Gate5GArtifactBinding):
        return value
    try:
        payload = value.model_dump(mode="json")
    except AttributeError as exc:
        raise Gate5GFinalizationError("artifact binding is not serializable") from exc
    return Gate5GArtifactBinding.model_validate(payload)


def _model(path: Path, model: type[BaseModel], *, label: str) -> BaseModel:
    try:
        return model.model_validate(_load_json(path))
    except ValueError as exc:
        raise Gate5GFinalizationError(f"invalid {label} {path}: {exc}") from exc


def _verify_embedded_path_hashes(paths: RepoPaths, value: Any, *, label: str) -> None:
    """Verify every recursively embedded repository path -> SHA-256 binding."""

    if isinstance(value, dict):
        for key, child in value.items():
            if (
                isinstance(key, str)
                and "/" in key
                and isinstance(child, str)
                and re.fullmatch(_HEX64, child)
            ):
                artifact, relative = _repo_file(paths, key, label=f"{label} embedded artifact")
                if relative != key or hash_file(artifact) != child:
                    raise Gate5GFinalizationError(
                        f"{label} embedded artifact binding differs: {key}"
                    )
            _verify_embedded_path_hashes(paths, child, label=label)
    elif isinstance(value, list):
        for child in value:
            _verify_embedded_path_hashes(paths, child, label=label)


def _require_label_blind(value: Any, *, label: str) -> None:
    """Reject semantic labels/supervision claims anywhere in bound JSON."""

    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"same_claim", "relation"} and child is not None:
                raise Gate5GFinalizationError(f"{label} contains semantic label {key}")
            if key in {"semantic_labels_created", "supervision_eligible"} and child is not False:
                raise Gate5GFinalizationError(f"{label} has non-false {key}")
            if key == "gate_5_closed" and child is not False:
                raise Gate5GFinalizationError(f"{label} claims Gate 5 closed")
            _require_label_blind(child, label=label)
    elif isinstance(value, list):
        for child in value:
            _require_label_blind(child, label=label)


def _load_jsonl(path: Path, *, label: str) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    Gate5GFinalizationError(f"non-finite JSON constant {token!r}")
                ),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise Gate5GFinalizationError(
                f"invalid {label} JSONL row {line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise Gate5GFinalizationError(f"{label} row {line_number} is not an object")
        rows.append(value)
    return tuple(rows)


def _scope_from_policy(policy: Gate5GFinalizerPolicyV1) -> Gate5GScopeLimitations:
    return Gate5GScopeLimitations(
        scalable_family_ids=policy.required_scalable_family_ids,
        three_family_collection_only=True,
        reduced_data_ablation=True,
        confirmatory_d4_d5_eligible=False,
        heldout_generator_claim_eligible=False,
        supplemental_qualifications_count_for_gate_credit=False,
        reduced_data_reasons=(
            "confirmatory_d4_d5_unavailable",
            "heldout_generator_claim_unavailable",
            "three_family_collection_only",
        ),
    )


def _verify_replay(
    paths: RepoPaths,
    *,
    binding: Gate5GArtifactBinding,
    manifest: Gate5GArtifactBinding,
    tranche_id: str,
    kind: Literal["collection", "postprocess"],
    expected_count: int,
) -> None:
    path = _verify_binding(paths, binding, label=f"{tranche_id} {kind} replay")
    try:
        certificate = Gate5GReplayCertificateV1.model_validate(_load_json(path))
    except ValueError as exc:
        raise Gate5GFinalizationError(
            f"{tranche_id} {kind} replay certificate is invalid: {exc}"
        ) from exc
    expected_kind = f"lf021_{kind}_replay_certificate_v1"
    if certificate.report_kind != expected_kind:
        raise Gate5GFinalizationError(f"{tranche_id} has the wrong {kind} replay kind")
    if (
        certificate.tranche_id != tranche_id
        or certificate.manifest.artifact != manifest.artifact
        or certificate.manifest.sha256 != manifest.sha256
        or certificate.expected_record_count != expected_count
    ):
        raise Gate5GFinalizationError(f"{tranche_id} {kind} replay binding differs")


def _verify_lineage(
    paths: RepoPaths,
    *,
    lineage: Gate5GLineageManifestV1,
    decision: FrameFreezeDecisionV3,
    required_families: tuple[str, str, str],
) -> None:
    if lineage.scalable_family_ids != required_families:
        raise Gate5GFinalizationError("lineage has the wrong scalable family inventory")
    observations = tuple(
        (
            item.tranche_id,
            item.manifest_id,
            item.postprocess_manifest.artifact,
            item.postprocess_manifest.sha256,
        )
        for item in decision.observations
    )
    expected_observations = tuple(
        (
            item.tranche_id,
            item.postprocess_manifest.manifest_id,
            item.postprocess_manifest.artifact,
            item.postprocess_manifest.sha256,
        )
        for item in lineage.tranches
    )
    if observations != expected_observations:
        raise Gate5GFinalizationError(
            "decision observations differ from the complete lineage prefix"
        )

    for tranche in lineage.tranches:
        collection_path = _verify_binding(
            paths,
            tranche.collection_manifest,
            label=f"{tranche.tranche_id} collection manifest",
        )
        collection_raw = _load_json(collection_path)
        collection = _CollectionManifestProjection.model_validate(collection_raw)
        if (
            collection.tranche_id != tranche.tranche_id
            or collection.expected_candidate_count != tranche.expected_invocations
            or collection.terminal_candidate_count != tranche.collection_terminal_count
            or collection.family_count != len(tranche.family_ids)
            or len(collection.terminal_artifact_hashes) != tranche.collection_terminal_count
        ):
            raise Gate5GFinalizationError(f"{tranche.tranche_id} collection denominator differs")
        expected_family_sessions = {
            binding.artifact: binding.sha256
            for family in tranche.family_revisions
            for binding in (family.session_start, family.session_end)
        }
        if collection.family_session_artifact_hashes != dict(
            sorted(expected_family_sessions.items())
        ):
            raise Gate5GFinalizationError(f"{tranche.tranche_id} family session lineage differs")
        _require_label_blind(collection_raw, label=f"{tranche.tranche_id} collection")
        _verify_embedded_path_hashes(
            paths,
            collection_raw,
            label=f"{tranche.tranche_id} collection",
        )
        for artifact, digest in collection.terminal_artifact_hashes.items():
            collection_terminal_path = _verify_binding(
                paths,
                Gate5GArtifactBinding(artifact=artifact, sha256=digest),
                label=f"{tranche.tranche_id} collection terminal",
            )
            collection_terminal_raw = _load_json(collection_terminal_path)
            _require_label_blind(
                collection_terminal_raw,
                label=f"{tranche.tranche_id} collection terminal",
            )
            _verify_embedded_path_hashes(
                paths,
                collection_terminal_raw,
                label=f"{tranche.tranche_id} collection terminal",
            )

        for family in tranche.family_revisions:
            session_start_path = _verify_binding(
                paths,
                family.session_start,
                label=f"{tranche.tranche_id} {family.family_id} session start",
            )
            session_end_path = _verify_binding(
                paths,
                family.session_end,
                label=f"{tranche.tranche_id} {family.family_id} session end",
            )
            start_raw = _load_json(session_start_path)
            end_raw = _load_json(session_end_path)
            start = _FamilySessionStartProjection.model_validate(start_raw)
            end = _FamilySessionEndProjection.model_validate(end_raw)
            if (
                start.family_id != family.family_id
                or start.model_repo_id != family.model_repo_id
                or start.model_revision != family.model_revision
                or end.family_id != family.family_id
                or end.family_session_id != start.family_session_id
            ):
                raise Gate5GFinalizationError(
                    f"{tranche.tranche_id} {family.family_id} revision/session differs"
                )
            _require_label_blind(
                start_raw,
                label=f"{tranche.tranche_id} {family.family_id} session start",
            )
            _require_label_blind(
                end_raw,
                label=f"{tranche.tranche_id} {family.family_id} session end",
            )

        overlap_path = _verify_binding(
            paths,
            tranche.overlap_manifest,
            label=f"{tranche.tranche_id} overlap manifest",
        )
        overlap_raw = _load_json(overlap_path)
        overlap = _OverlapManifestProjection.model_validate(overlap_raw)
        if (
            overlap.family_count != len(tranche.family_ids)
            or tuple(sorted(overlap.family_artifacts)) != tranche.family_ids
        ):
            raise Gate5GFinalizationError(f"{tranche.tranche_id} overlap family inventory differs")
        _require_label_blind(overlap_raw, label=f"{tranche.tranche_id} overlap")
        for family_id, overlap_binding in overlap.family_artifacts.items():
            overlap_family_path = _verify_binding(
                paths,
                overlap_binding,
                label=f"{tranche.tranche_id} {family_id} overlap",
            )
            overlap_family_raw = _load_json(overlap_family_path)
            _require_label_blind(
                overlap_family_raw,
                label=f"{tranche.tranche_id} {family_id} overlap",
            )
            _verify_embedded_path_hashes(
                paths,
                overlap_family_raw,
                label=f"{tranche.tranche_id} {family_id} overlap",
            )

        postprocess_path = _verify_binding(
            paths,
            tranche.postprocess_manifest,
            label=f"{tranche.tranche_id} postprocess manifest",
        )
        postprocess_raw = _load_json(postprocess_path)
        postprocess = _PostprocessManifestProjection.model_validate(postprocess_raw)
        if (
            postprocess.manifest_id != tranche.postprocess_manifest.manifest_id
            or postprocess.tranche_id != tranche.tranche_id
            or postprocess.expected_invocations != tranche.expected_invocations
            or postprocess.terminal_invocations != tranche.postprocess_terminal_count
            or postprocess.family_count != len(tranche.family_ids)
            or len(postprocess.terminal_artifacts) != tranche.postprocess_terminal_count
            or sum(postprocess.status_counts.values()) != tranche.postprocess_terminal_count
            or postprocess.status_counts.get("admitted_unresolved", 0)
            + postprocess.status_counts.get("screen_rejected", 0)
            != tranche.benchmark_clear_compiling_count
            or postprocess.input_binding.collection_manifest != tranche.collection_manifest
            or postprocess.input_binding.collection_manifest_id != collection.manifest_id
        ):
            raise Gate5GFinalizationError(
                f"{tranche.tranche_id} postprocess denominator/lineage differs"
            )
        _require_label_blind(postprocess_raw, label=f"{tranche.tranche_id} postprocess")
        _verify_embedded_path_hashes(
            paths,
            postprocess_raw,
            label=f"{tranche.tranche_id} postprocess",
        )
        for artifact, digest in postprocess.terminal_artifacts.items():
            terminal_path = _verify_binding(
                paths,
                Gate5GArtifactBinding(artifact=artifact, sha256=digest),
                label=f"{tranche.tranche_id} postprocess terminal",
            )
            terminal_raw = _load_json(terminal_path)
            _require_label_blind(
                terminal_raw,
                label=f"{tranche.tranche_id} postprocess terminal",
            )
            _verify_embedded_path_hashes(
                paths,
                terminal_raw,
                label=f"{tranche.tranche_id} postprocess terminal",
            )

        _verify_replay(
            paths,
            binding=tranche.collection_replay,
            manifest=tranche.collection_manifest,
            tranche_id=tranche.tranche_id,
            kind="collection",
            expected_count=tranche.collection_terminal_count,
        )
        _verify_replay(
            paths,
            binding=tranche.postprocess_replay,
            manifest=tranche.postprocess_manifest,
            tranche_id=tranche.tranche_id,
            kind="postprocess",
            expected_count=tranche.postprocess_terminal_count,
        )


def _verify_frame(
    paths: RepoPaths,
    *,
    frame_path: Path,
    decision: FrameFreezeDecisionV3,
    lineage: Gate5GLineageManifestV1,
    replayed_items: tuple[FrameItemV3, ...],
) -> tuple[
    tuple[FrameItemV3, ...],
    dict[str, int],
    dict[str, int],
    dict[str, int],
]:
    raw_rows = _load_jsonl(frame_path, label="Gate-5G frame")
    if len(raw_rows) != decision.frame.item_count:
        raise Gate5GFinalizationError("frame row count differs from decision")
    rows: list[FrameItemV3] = []
    seen_units: set[tuple[str, str]] = set()
    lineage_manifests = {item.postprocess_manifest.manifest_id for item in lineage.tranches}
    lineage_families = set(lineage.scalable_family_ids)
    lineage_pools = set(lineage.pool_ids)
    lineage_proxies = set(lineage.source_proxies)
    per_stratum: dict[str, list[FrameItemV3]] = defaultdict(list)
    family_counts: Counter[str] = Counter()
    pool_counts: Counter[str] = Counter()
    proxy_counts: Counter[str] = Counter()

    for raw in raw_rows:
        _require_label_blind(raw, label="Gate-5G frame item")
        row = FrameItemV3.model_validate(raw)
        if (
            row.population_manifest_id != decision.population_id
            or row.population_manifest != decision.population_manifest
            or row.sampling_method != decision.sampling_method
            or row.sampling_rank_algorithm != decision.sampling_rank_algorithm
            or row.sampling_seed_sha256 != decision.sampling_seed_sha256
            or row.sampling_seed_provenance != decision.sampling_seed_provenance
            or row.test_replay_only != decision.test_replay_only
        ):
            raise Gate5GFinalizationError("frame item differs from frame-freeze decision")
        unit = (row.problem_group, row.alpha_identity_fingerprint)
        if unit in seen_units:
            raise Gate5GFinalizationError("frame repeats a problem-aware claim unit")
        seen_units.add(unit)
        if not set(row.postprocess_manifest_ids).issubset(lineage_manifests):
            raise Gate5GFinalizationError("frame item cites an unbound postprocess manifest")
        if (
            not set(row.contributing_family_ids).issubset(lineage_families)
            or not set(row.contributing_pool_ids).issubset(lineage_pools)
            or not set(row.contributing_source_proxies).issubset(lineage_proxies)
        ):
            raise Gate5GFinalizationError("frame item cites lineage outside the frozen prefix")

        terminal_path = _verify_binding(
            paths,
            _gate_binding(row.terminal_artifact),
            label="selected postprocess terminal",
        )
        screening_path = _verify_binding(
            paths,
            _gate_binding(row.screening_artifact),
            label="selected benchmark screening",
        )
        representation_path = _verify_binding(
            paths,
            _gate_binding(row.representation_artifact),
            label="selected representation",
        )
        terminal_raw = _load_json(terminal_path)
        terminal = _PostprocessTerminalProjection.model_validate(terminal_raw)
        screening = _ScreeningProjection.model_validate(_load_json(screening_path))
        representation = _RepresentationProjection.model_validate(_load_json(representation_path))
        if (
            terminal.invocation_id != row.representative_invocation_id
            or terminal.family_id != row.representative_family_id
            or terminal.problem_record_id != row.representative_problem_record_id
            or screening.problem_record_id != row.representative_problem_record_id
            or screening.candidate_theorem_id != terminal.candidate_theorem_id
            or representation.theorem_id != terminal.candidate_theorem_id
            or screening.alpha_identity_fingerprint != row.alpha_identity_fingerprint
            or representation.alpha_identity_fingerprint != row.alpha_identity_fingerprint
            or terminal.output_artifact_hashes.get(row.screening_artifact.artifact)
            != row.screening_artifact.sha256
            or terminal.output_artifact_hashes.get(row.representation_artifact.artifact)
            != row.representation_artifact.sha256
        ):
            raise Gate5GFinalizationError("selected frame artifact projections disagree")
        _require_label_blind(terminal_raw, label="selected postprocess terminal")
        _verify_embedded_path_hashes(
            paths,
            terminal_raw,
            label="selected postprocess terminal",
        )
        per_stratum[row.sampling_stratum].append(row)
        family_counts[row.representative_family_id] += 1
        pool_counts[row.representative_pool_id] += 1
        proxy_counts[row.representative_source_proxy] += 1
        rows.append(row)

    total_population = 0
    for stratum, items in sorted(per_stratum.items()):
        n_h = items[0].stratum_sample_size
        population = items[0].stratum_population_size
        if len(items) != n_h:
            raise Gate5GFinalizationError(f"stratum {stratum} selected count differs from n_h")
        if any(
            item.stratum_sample_size != n_h
            or item.stratum_population_size != population
            or item.inclusion_probability_numerator != n_h
            or item.inclusion_probability_denominator != population
            for item in items
        ):
            raise Gate5GFinalizationError(f"stratum {stratum} propensities disagree")
        total_population += population
    if total_population != decision.counts.unique_compiling_count:
        raise Gate5GFinalizationError(
            "stratum population totals differ from problem-aware population"
        )
    if (
        dict(sorted((key, len(value)) for key, value in per_stratum.items()))
        != decision.stratum_sample_sizes
        or {key: items[0].stratum_population_size for key, items in sorted(per_stratum.items())}
        != decision.stratum_population_sizes
    ):
        raise Gate5GFinalizationError("frame stratum accounting differs from decision")
    parsed_rows = tuple(rows)
    if parsed_rows != replayed_items:
        raise Gate5GFinalizationError("Gate-5G frame differs from strict v3 replay")
    return (
        parsed_rows,
        dict(sorted(family_counts.items())),
        dict(sorted(pool_counts.items())),
        dict(sorted(proxy_counts.items())),
    )


def _require_text_bindings(
    path: Path,
    *,
    label: str,
    required_literals: tuple[str, ...],
) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise Gate5GFinalizationError(f"cannot read {label}: {exc}") from exc
    missing = tuple(value for value in required_literals if value not in text)
    if missing:
        raise Gate5GFinalizationError(f"{label} is stale; missing bindings {missing!r}")


def _publication_directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_publication_parent_no_follow(
    *,
    repo_root: Path,
    path: Path,
) -> tuple[Path, int, tuple[int, ...]]:
    """Open/create a repository-local parent through trusted descriptors."""

    try:
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise Gate5GFinalizationError(
            f"repository root is unavailable during publication: {exc}"
        ) from exc
    lexical = Path(os.path.abspath(os.fspath(path)))
    if not lexical.is_relative_to(root):
        raise Gate5GFinalizationError("publication target escapes the repository")
    opened: list[int] = []
    current_fd = os.open(root, _publication_directory_flags())
    opened.append(current_fd)
    try:
        for component in lexical.parent.relative_to(root).parts:
            try:
                next_fd = os.open(
                    component,
                    _publication_directory_flags(),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    os.fsync(current_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(
                        component,
                        _publication_directory_flags(),
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    raise Gate5GFinalizationError(
                        f"publication parent is not trusted: {component}"
                    ) from exc
            except OSError as exc:
                raise Gate5GFinalizationError(
                    f"publication parent is not trusted: {component}"
                ) from exc
            opened.append(next_fd)
            current_fd = next_fd
    except Exception:
        for descriptor in reversed(opened):
            os.close(descriptor)
        raise
    return lexical, current_fd, tuple(opened)


def _read_publication_fd(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _existing_publication_matches(
    *,
    parent_fd: int,
    filename: str,
    payload: bytes,
    label: str,
) -> os.stat_result | None:
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Gate5GFinalizationError(f"{label} is not a trusted regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise Gate5GFinalizationError(f"{label} is not a trusted regular file")
        if _read_publication_fd(descriptor) != payload:
            raise Gate5GFinalizationError(f"{label} already exists with different bytes")
        observed = os.fstat(descriptor)
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ) != (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
        ):
            raise Gate5GFinalizationError(f"{label} changed while read")
        return metadata
    finally:
        os.close(descriptor)


def _verify_publication_path(
    *,
    repo_root: Path,
    path: Path,
    expected: os.stat_result,
    label: str,
) -> None:
    """Require the published inode to remain reachable without symlinks."""

    root = repo_root.resolve(strict=True)
    lexical = Path(os.path.abspath(os.fspath(path)))
    current = root
    for component in lexical.relative_to(root).parts:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise Gate5GFinalizationError(
                f"{label} is no longer reachable at its trusted path"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise Gate5GFinalizationError(f"{label} publication path contains a symlink")
    observed = lexical.lstat()
    if not stat.S_ISREG(observed.st_mode) or (
        observed.st_dev,
        observed.st_ino,
    ) != (
        expected.st_dev,
        expected.st_ino,
    ):
        raise Gate5GFinalizationError(f"{label} path changed during publication")


def _write_immutable(
    path: Path,
    payload: bytes,
    *,
    repo_root: Path,
    label: str,
) -> None:
    """Atomically publish immutable bytes without following path symlinks."""

    lexical, parent_fd, opened = _open_publication_parent_no_follow(
        repo_root=repo_root,
        path=path,
    )
    filename = lexical.name
    temporary = f".{filename}.tmp.{os.getpid()}.{secrets.token_hex(16)}"
    published = False
    try:
        existing = _existing_publication_matches(
            parent_fd=parent_fd,
            filename=filename,
            payload=payload,
            label=label,
        )
        if existing is not None:
            _verify_publication_path(
                repo_root=repo_root,
                path=lexical,
                expected=existing,
                label=label,
            )
            return

        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count == 0:
                    raise Gate5GFinalizationError(f"{label} staging write made no progress")
                written += count
            os.fsync(descriptor)
            expected = os.fstat(descriptor)
        finally:
            os.close(descriptor)

        try:
            os.link(
                temporary,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            published = True
        except FileExistsError:
            existing = _existing_publication_matches(
                parent_fd=parent_fd,
                filename=filename,
                payload=payload,
                label=label,
            )
            if existing is None:
                raise Gate5GFinalizationError(f"{label} raced with a disappearing target") from None
            expected = existing
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
        try:
            _verify_publication_path(
                repo_root=repo_root,
                path=lexical,
                expected=expected,
                label=label,
            )
        except Exception:
            if published:
                try:
                    current = os.stat(
                        filename,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) == (
                        expected.st_dev,
                        expected.st_ino,
                    ):
                        os.unlink(filename, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                except OSError:
                    pass
            raise
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)
        for descriptor in reversed(opened):
            os.close(descriptor)


def validate_or_finalize_gate5g(
    *,
    paths: RepoPaths,
    frame_freeze_decision_path: Path,
    lineage_manifest_path: Path,
    validated_manifest_path: Path,
    coverage_report_path: Path,
    phase_milestone_path: Path,
    prevalence_design_policy_path: Path | None = None,
    policy_path: Path | None = None,
    finalize: bool = False,
    finalized_date: datetime.date | None = None,
) -> Gate5GFinalizationResult:
    """Validate the complete frame and optionally emit the canonical gate.

    ``finalize=False`` can only write a content-addressed validation report
    below ``reports/generation``.  ``finalize=True`` additionally requires an
    explicit date and writes exactly ``reports/gates/gate_5g.json``.
    """

    if finalize != (finalized_date is not None):
        raise Gate5GFinalizationError(
            "explicit finalize mode requires --finalized-date, and dry-run forbids it"
        )
    effective_policy_path = policy_path or paths.root / _DEFAULT_POLICY
    loaded_policy = load_gate5g_policy(effective_policy_path)
    policy = loaded_policy.config
    policy_file, policy_binding = _binding(
        paths,
        loaded_policy.path,
        label="Gate-5G finalizer policy",
    )
    implementation_source = Path(__file__).resolve()
    implementation_repo_path = paths.root / "src/leanfaith/generation/gate5g.py"
    implementation_file, implementation_binding = _binding(
        paths,
        implementation_repo_path,
        label="Gate-5G finalizer implementation",
    )
    if hash_file(implementation_file) != hash_file(implementation_source):
        raise Gate5GFinalizationError(
            "repository Gate-5G implementation differs from the executing module"
        )

    effective_prevalence_design_path = (
        prevalence_design_policy_path or paths.root / _DEFAULT_PREVALENCE_DESIGN
    )
    try:
        loaded_prevalence_design = load_prevalence_design_policy(effective_prevalence_design_path)
        verify_prevalence_design_policy_v2(
            repo_root=paths.root,
            loaded_policy=loaded_prevalence_design,
        )
    except (OSError, ValueError, PrevalenceInputError) as exc:
        raise Gate5GFinalizationError(f"invalid frozen prevalence design v2: {exc}") from exc
    prevalence_design = loaded_prevalence_design.config
    _, prevalence_design_binding = _binding(
        paths,
        loaded_prevalence_design.path,
        label="frozen prevalence design v2",
    )
    base_prevalence_design_binding = _gate_binding(prevalence_design.base_v1_design)
    _verify_binding(
        paths,
        base_prevalence_design_binding,
        label="bound base prevalence design v1",
    )
    if (
        prevalence_design.policy_id != policy.required_prevalence_design_policy_id
        or prevalence_design.target_population.frame_schema_version
        != policy.required_decision_schema_version
        or prevalence_design.target_population.sampling_method != policy.required_sampling_method
        or prevalence_design.target_population.sampling_rank_algorithm
        != policy.required_sampling_rank_algorithm
        or prevalence_design.scope.required_scalable_families != policy.required_scalable_family_ids
        or not prevalence_design.scope.three_family_collection_only
        or prevalence_design.scope.confirmatory_d4_d5_eligible
        or prevalence_design.scope.heldout_generator_claim_eligible
        or prevalence_design.scope.supplemental_qualifications_count_for_gate_credit
    ):
        raise Gate5GFinalizationError("prevalence design v2 differs from frozen Gate-5G scope")

    decision_file, decision_binding = _binding(
        paths,
        frame_freeze_decision_path,
        label="v3 frame-freeze decision",
    )
    try:
        decision = FrameFreezeDecisionV3.model_validate(_load_json(decision_file))
    except ValueError as exc:
        raise Gate5GFinalizationError(
            f"invalid v3 frame-freeze decision {decision_file}: {exc}"
        ) from exc
    if (
        decision.schema_version != policy.required_decision_schema_version
        or decision.action != policy.required_v2_stop_action
        or decision.v2_stop_action != policy.required_v2_stop_action
        or decision.coverage_deficits
        or decision.next_tranche is not None
        or decision.sampling_method != policy.required_sampling_method
        or decision.sampling_rank_algorithm != policy.required_sampling_rank_algorithm
        or not (policy.minimum_frame_size <= decision.frame.item_count <= policy.maximum_frame_size)
    ):
        raise Gate5GFinalizationError("decision differs from frozen Gate-5G policy")
    if decision.test_replay_only or decision.frame.test_replay_only:
        raise Gate5GFinalizationError("test/replay sampling seed cannot receive Gate-5G credit")
    frame_freeze_policy_path = _verify_binding(
        paths,
        _gate_binding(decision.policy_artifact),
        label="frame-freeze policy",
    )
    _verify_binding(
        paths,
        _gate_binding(decision.implementation_artifact),
        label="frame-freeze implementation",
    )
    _verify_binding(
        paths,
        _gate_binding(decision.v2_stop_decision),
        label="verified v2 stopping decision",
    )
    _verify_binding(
        paths,
        _gate_binding(decision.population_manifest),
        label="eligible-population manifest",
    )
    _verify_binding(
        paths,
        _gate_binding(decision.population_artifact),
        label="eligible-population artifact",
    )
    frame_path = _verify_binding(
        paths,
        Gate5GArtifactBinding(
            artifact=decision.frame.artifact,
            sha256=decision.frame.sha256,
        ),
        label="v3 prevalence frame",
    )
    if hash_file(frame_path) != decision.frame.sha256:
        raise Gate5GFinalizationError("frame hash differs from decision")

    try:
        verified_frame = verify_frame_freeze_v3(
            repo_root=paths.root,
            policy_path=frame_freeze_policy_path,
            decision_path=decision_file,
        )
    except (FrameFreezeV3Error, OSError, ValueError) as exc:
        raise Gate5GFinalizationError(f"v3 frame-freeze replay failed: {exc}") from exc
    if (
        verified_frame.decision != decision
        or verified_frame.decision_path.resolve() != decision_file.resolve()
        or _gate_binding(verified_frame.decision_binding) != decision_binding
        or verified_frame.frame_path.resolve() != frame_path.resolve()
    ):
        raise Gate5GFinalizationError("strict v3 frame-freeze verifier returned different bindings")

    seed_path = _verify_binding(
        paths,
        _gate_binding(decision.frame.sampling_seed_provenance),
        label="sampling-seed provenance",
    )
    seed = verified_frame.seed_provenance
    if verified_frame.seed_provenance_path.resolve() != seed_path.resolve():
        raise Gate5GFinalizationError("strict verifier returned different seed provenance")
    if (
        seed.source not in policy.allowed_production_seed_sources
        or seed.test_replay_only
        or seed.population_id != decision.population_id
        or seed.population_manifest != decision.population_manifest
        or seed.population_artifact != decision.population_artifact
        or seed.sampling_seed_sha256 != decision.frame.sampling_seed_sha256
    ):
        raise Gate5GFinalizationError("sampling seed/provenance does not reconcile")
    archived_seed_path = _verify_binding(
        paths,
        _gate_binding(seed.sampling_seed),
        label="archived 256-bit sampling seed",
    )
    archived_seed = archived_seed_path.read_bytes()
    if len(archived_seed) != 32 or sha256_hex(archived_seed) != seed.sampling_seed_sha256:
        raise Gate5GFinalizationError("sampling seed is not exact bound 256-bit content")
    if archived_seed != verified_frame.seed_bytes:
        raise Gate5GFinalizationError("strict verifier returned different sampling seed bytes")
    external_beacon_binding: Gate5GArtifactBinding | None = None
    if seed.external_beacon_provenance is not None:
        _verify_binding(
            paths,
            _gate_binding(seed.external_beacon_provenance),
            label="external-randomness-beacon provenance",
        )
        external_beacon_binding = _gate_binding(seed.external_beacon_provenance)
    _, seed_lock_binding = _binding(
        paths,
        verified_frame.seed_lock_path,
        label="population-bound sampling-seed lock",
    )

    lineage_file, lineage_binding = _binding(
        paths,
        lineage_manifest_path,
        label="Gate-5G lineage manifest",
    )
    lineage = Gate5GLineageManifestV1.model_validate(_load_json(lineage_file))
    _verify_lineage(
        paths,
        lineage=lineage,
        decision=decision,
        required_families=policy.required_scalable_family_ids,
    )
    rows, family_counts, pool_counts, proxy_counts = _verify_frame(
        paths,
        frame_path=frame_path,
        decision=decision,
        lineage=lineage,
        replayed_items=verified_frame.frame_items,
    )

    coverage_file, coverage_binding = _binding(
        paths,
        coverage_report_path,
        label="generation coverage report",
    )
    validated_file, validated_binding = _binding(
        paths,
        validated_manifest_path,
        label="validated real-output manifest",
    )
    validated = ValidatedRealOutputsManifestV1.model_validate(_load_json(validated_file))
    scope = _scope_from_policy(policy)
    expected_reform = Gate5GReFormApplicability(
        applicable=False,
        status="not_applicable",
        reason="none of the three scalable Gate-5G families is ReForm",
        overlap_report=None,
    )
    expected_frame_binding = Gate5GArtifactBinding(
        artifact=decision.frame.artifact,
        sha256=decision.frame.sha256,
    )
    expected_seed_provenance_binding = _gate_binding(decision.frame.sampling_seed_provenance)
    if (
        validated.frame_freeze_decision != decision_binding
        or validated.frame_freeze_decision_id != decision.decision_id
        or validated.frame != expected_frame_binding
        or validated.frame_id != decision.frame.frame_id
        or validated.frame_item_count != len(rows)
        or validated.lineage_manifest != lineage_binding
        or validated.lineage_manifest_id != lineage.manifest_id
        or validated.coverage_report != coverage_binding
        or validated.sampling_method != decision.frame.sampling_method
        or validated.sampling_seed_sha256 != decision.frame.sampling_seed_sha256
        or validated.sampling_seed_provenance != expected_seed_provenance_binding
        or validated.family_item_counts != family_counts
        or validated.pool_item_counts != pool_counts
        or validated.source_proxy_item_counts != proxy_counts
        or validated.scope_limitations != scope
        or validated.reform_applicability != expected_reform
    ):
        raise Gate5GFinalizationError("validated manifest differs from verified frame")
    observed_strata = {
        item.sampling_stratum: (
            item.stratum_population_size,
            item.stratum_sample_size,
        )
        for item in rows
    }
    validated_strata = {
        item.stratum: (item.population_size, item.sample_size) for item in validated.strata
    }
    if observed_strata != validated_strata:
        raise Gate5GFinalizationError("validated stratum accounting differs")

    coverage_literals = (
        prevalence_design.policy_id,
        prevalence_design_binding.sha256,
        base_prevalence_design_binding.sha256,
        decision.decision_id,
        decision_binding.sha256,
        decision.policy_id,
        decision.policy_artifact.sha256,
        decision.implementation_artifact.sha256,
        decision.v2_stop_decision_id,
        decision.v2_stop_decision.sha256,
        decision.population_id,
        decision.population_manifest.sha256,
        decision.population_artifact.sha256,
        decision.frame.frame_id,
        decision.frame.sha256,
        decision.sampling_seed_sha256,
        decision.sampling_seed_provenance.sha256,
        seed.sampling_seed.sha256,
        seed_lock_binding.sha256,
        lineage.manifest_id,
        lineage_binding.sha256,
        *policy.required_scalable_family_ids,
        "three_family_collection_only",
        "source proxy",
        "Gate 5 remains open",
    ) + ((external_beacon_binding.sha256,) if external_beacon_binding is not None else ())
    _require_text_bindings(
        coverage_file,
        label="generation coverage report",
        required_literals=coverage_literals,
    )
    phase_file, phase_binding = _binding(
        paths,
        phase_milestone_path,
        label="Phase-5 milestone",
    )
    _require_text_bindings(
        phase_file,
        label="Phase-5 milestone",
        required_literals=(
            *coverage_literals,
            coverage_binding.sha256,
            validated.manifest_id,
            validated_binding.sha256,
            "Gate 5G",
            "ready to finalize",
        ),
    )

    checks: dict[str, Literal[True]] = dict.fromkeys(GATE5G_CHECKS, True)
    input_bindings = Gate5GInputBindings(
        policy=policy_binding,
        implementation=implementation_binding,
        prevalence_design_policy=prevalence_design_binding,
        base_prevalence_design_policy=base_prevalence_design_binding,
        frame_freeze_decision=decision_binding,
        frame_freeze_policy=_gate_binding(decision.policy_artifact),
        frame_freeze_implementation=_gate_binding(decision.implementation_artifact),
        v2_stop_decision=_gate_binding(decision.v2_stop_decision),
        population_manifest=_gate_binding(decision.population_manifest),
        population_artifact=_gate_binding(decision.population_artifact),
        frame=expected_frame_binding,
        sampling_seed_provenance=expected_seed_provenance_binding,
        sampling_seed=_gate_binding(seed.sampling_seed),
        sampling_seed_lock=seed_lock_binding,
        external_beacon_provenance=external_beacon_binding,
        lineage_manifest=lineage_binding,
        validated_manifest=validated_binding,
        coverage_report=coverage_binding,
        phase_milestone=phase_binding,
    )
    validation_payload: dict[str, Any] = {
        "schema_version": 1,
        "validation_status": "ready_to_finalize",
        "input_bindings": input_bindings.model_dump(mode="json"),
        "prevalence_design_policy_id": prevalence_design.policy_id,
        "frame_freeze_decision_id": decision.decision_id,
        "frame_id": decision.frame.frame_id,
        "frame_item_count": len(rows),
        "sampling_method": decision.frame.sampling_method,
        "sampling_seed_sha256": decision.frame.sampling_seed_sha256,
        "observed_tranche_count": len(lineage.tranches),
        "scalable_family_ids": scope.scalable_family_ids,
        "pool_ids": lineage.pool_ids,
        "source_proxies": lineage.source_proxies,
        "scope_limitations": scope.model_dump(mode="json"),
        "reform_applicability": expected_reform.model_dump(mode="json"),
        "completed_checks": checks,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_closed": False,
        "gate_5_closed": False,
    }
    validation_id = "lf021_gate5g_validation:" + hash_canonical(
        {"schema": "lf021_gate5g_validation_v1", **validation_payload}
    )
    validation = Gate5GValidationReportV1.model_validate(
        {"validation_id": validation_id, **validation_payload}
    )
    output_root = paths.root / policy.dry_run_output_root
    if output_root.resolve() != (paths.root / _DRY_ROOT).resolve():
        raise Gate5GFinalizationError("policy dry-run output root is noncanonical")
    validation_path = output_root / f"{validation_id.rsplit(':', 1)[-1]}.json"
    validation_bytes = canonical_json_bytes(validation.model_dump(mode="json")) + b"\n"
    _write_immutable(
        validation_path,
        validation_bytes,
        repo_root=paths.root,
        label="Gate-5G validation report",
    )
    validation_binding = Gate5GArtifactBinding(
        artifact=str(validation_path.relative_to(paths.root)),
        sha256=sha256_hex(validation_bytes),
    )

    gate: Gate5GReportV1 | None = None
    gate_path: Path | None = None
    gate_hash: str | None = None
    if finalize:
        assert finalized_date is not None
        gate_payload: dict[str, Any] = {
            "schema_version": 1,
            "gate": "gate_5g",
            "decision": "pass",
            "finalized_date": finalized_date.isoformat(),
            "validation_report": validation_binding.model_dump(mode="json"),
            "validation_id": validation.validation_id,
            "prevalence_design_policy_id": prevalence_design.policy_id,
            "frame_freeze_decision_id": decision.decision_id,
            "frame_id": decision.frame.frame_id,
            "frame_item_count": len(rows),
            "scope_limitations": scope.model_dump(mode="json"),
            "completed_checks": checks,
            "blocking_checks": (),
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_closed": True,
            "gate_5_closed": False,
        }
        report_id = "lf021_gate5g_report:" + hash_canonical(
            {"schema": "lf021_gate5g_report_v1", **gate_payload}
        )
        gate = Gate5GReportV1.model_validate({"report_id": report_id, **gate_payload})
        gate_path = paths.root / policy.canonical_gate_report
        if gate_path.resolve() != (paths.root / _GATE_PATH).resolve():
            raise Gate5GFinalizationError("policy gate path is noncanonical")
        gate_bytes = canonical_json_bytes(gate.model_dump(mode="json")) + b"\n"
        _write_immutable(
            gate_path,
            gate_bytes,
            repo_root=paths.root,
            label="canonical Gate-5G report",
        )
        gate_hash = sha256_hex(gate_bytes)

    # Keep these live references explicit; they are part of the implementation
    # hash and prevent an accidental future cleanup from dropping policy/code
    # verification after report construction.
    assert (
        policy_file.is_file()
        and implementation_file.is_file()
        and loaded_prevalence_design.path.is_file()
    )
    return Gate5GFinalizationResult(
        validation_report_path=str(validation_path.relative_to(paths.root)),
        validation_report_sha256=validation_binding.sha256,
        validation_report=validation,
        gate_report_path=(str(gate_path.relative_to(paths.root)) if gate_path else None),
        gate_report_sha256=gate_hash,
        gate_report=gate,
    )


__all__ = [
    "GATE5G_CHECKS",
    "Gate5GFinalizationError",
    "Gate5GFinalizationResult",
    "Gate5GFinalizerPolicyV1",
    "load_gate5g_policy",
    "validate_or_finalize_gate5g",
]
