"""Derive one executable-diagnostic scaffold from an immutable public pool.

The reviewed ``repr_v3`` public pool is already content-addressed and exact
replayed.  Re-running its old extraction/representation lineage after later
extraction-only code changes would either fail or weaken the reviewed reuse
attestation.  This module instead selects one already-admitted source from that
immutable pool, replays every parent binding, and writes a new one-source,
family-specific diagnostic scaffold.

This operation is offline.  It does not resolve credentials, contact a model
provider, create a semantic label, or make any output train/evaluation eligible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import DenylistIndex, FrozenRegistry
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022BenchmarkRegistryManifest,
    LF022DenylistClearanceRecord,
    LF022JSONLArtifactBinding,
    LF022ProductionAdmission,
    LF022ProductionArtifactSet,
    LF022ProductionFamilyMatrix,
    LF022ProductionPlanManifest,
    LF022ProductionSourceRecord,
    LF022PublicSourceAuthorizationRegistry,
    _make_task,
    _role_assignment_ids,
    make_lf022_production_admission,
    write_lf022_production_plan,
)
from leanfaith.generation.lf022_public_pool import (
    LF022PublicPoolAudit,
    LF022PublicPoolOutputArtifacts,
    MaterializedLF022PublicPool,
    _output_directory,
    _write_json,
    _write_jsonl,
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.manifest import collect_code_state
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

LF022DiagnosticProposerFamily = Literal["qwen3", "glm5", "deepseek_v4"]


class LF022DiagnosticSubpoolError(ValueError):
    """An immutable parent pool cannot produce the requested diagnostic pool."""


class LF022DiagnosticSubpoolDerivation(StrictModel):
    """Content-addressed lineage from one parent pool to one diagnostic source."""

    schema_version: Literal[1, 2] = 1
    derivation_id: str = Field(pattern=id_pattern("lf022_diagnostic_subpool_derivation"))
    derivation_kind: Literal["immutable_parent_public_pool_subset"]
    selection_policy: Literal["parent_selection_order_first_v1"]
    parent_pool_audit: LF022ArtifactBinding
    parent_pool_audit_id: str = Field(pattern=id_pattern("lf022_public_pool_audit"))
    parent_outputs: LF022PublicPoolOutputArtifacts
    derived_family_matrix: LF022ArtifactBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    proposer_family_id: LF022DiagnosticProposerFamily
    selected_source_admission_record_id: str = Field(pattern=id_pattern("lf022_source_admission"))
    selected_source_locator_id: str = Field(pattern=HEX64_PATTERN)
    selected_theorem_id: str = Field(pattern=id_pattern("thm"))
    selected_representation_id: str = Field(pattern=id_pattern("repr"))
    selected_context_id: str = Field(pattern=id_pattern("ctx"))
    selected_clearance_id: str = Field(pattern=id_pattern("lf022_denylist_clearance"))
    selected_source_count: Literal[1]
    allocation_task_count: Literal[2]
    attesting_git_revision: str = Field(min_length=1)
    attesting_code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    public_sources_only: Literal[True]
    private_sft_classic_forbidden: Literal[True]
    network_execution_authorized: Literal[False]
    outputs_provisional_only: Literal[True]
    resolution_outcome: Literal["unresolved"]
    semantic_labels_created: Literal[False]
    training_eligible: Literal[False]
    evaluation_eligible: Literal[False]
    gate_credit_claimed: Literal[False]

    @model_validator(mode="after")
    def _content_addressed(self) -> LF022DiagnosticSubpoolDerivation:
        if self.schema_version == 1 and self.derived_family_matrix is not None:
            raise ValueError("schema_version 1 cannot replace the parent family matrix")
        if self.proposer_family_id == "deepseek_v4":
            if self.schema_version != 2 or self.derived_family_matrix is None:
                raise ValueError(
                    "DeepSeek diagnostic derivations require a bound replacement family matrix"
                )
        elif self.schema_version != 1 or self.derived_family_matrix is not None:
            raise ValueError(
                "legacy Qwen/GLM diagnostic derivations must preserve schema_version 1"
            )
        expected = make_id(
            "lf022_diagnostic_subpool_derivation",
            self.model_dump(mode="json", exclude={"derivation_id"}),
        )
        if self.derivation_id != expected:
            raise ValueError("derivation_id does not match canonical derivation content")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedLF022DiagnosticSubpool:
    """Replay result used by admission freezing and focused audits."""

    audit: LF022PublicPoolAudit
    derivation: LF022DiagnosticSubpoolDerivation
    parent_audit: LF022PublicPoolAudit
    source: LF022ProductionSourceRecord
    theorem: TheoremRecord
    representation: RepresentationRecord
    context: ContextRecord
    clearance: LF022DenylistClearanceRecord
    plan: LF022ProductionPlanManifest
    admission: LF022ProductionAdmission


@dataclass(frozen=True, slots=True)
class DerivedLF022DiagnosticSubpool:
    """Persisted one-source diagnostic scaffold plus replay result."""

    materialized: MaterializedLF022PublicPool
    derivation: LF022DiagnosticSubpoolDerivation
    derivation_binding: LF022ArtifactBinding


def _bound_path(
    repo_root: Path,
    binding: LF022ArtifactBinding,
    *,
    label: str,
) -> Path:
    root = repo_root.resolve(strict=True)
    candidate = root / binding.path
    current = root
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LF022DiagnosticSubpoolError(f"{label} escapes the repository") from exc
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise LF022DiagnosticSubpoolError(f"{label} traverses a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LF022DiagnosticSubpoolError(f"{label} is missing") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise LF022DiagnosticSubpoolError(f"{label} must be a repository-local regular file")
    if hash_file(resolved) != binding.sha256:
        raise LF022DiagnosticSubpoolError(f"{label} hash differs from its binding")
    return resolved


def _binding(repo_root: Path, path: Path, *, label: str) -> LF022ArtifactBinding:
    root = repo_root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise LF022DiagnosticSubpoolError(
            f"{label} must be a repository-local regular file"
        ) from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise LF022DiagnosticSubpoolError(f"{label} must be a repository-local regular file")
    return LF022ArtifactBinding(path=relative.as_posix(), sha256=hash_file(resolved))


def _load_json[RecordT: StrictModel](
    repo_root: Path,
    binding: LF022ArtifactBinding,
    model: type[RecordT],
    *,
    label: str,
) -> RecordT:
    path = _bound_path(repo_root, binding, label=label)
    raw = path.read_bytes()
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022DiagnosticSubpoolError(f"invalid {label}: {exc}") from exc
    canonical = canonical_json_bytes(record.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise LF022DiagnosticSubpoolError(f"{label} is not canonical JSON")
    return record


def _load_selected_jsonl[RecordT: StrictModel](
    repo_root: Path,
    binding: LF022JSONLArtifactBinding,
    model: type[RecordT],
    *,
    attribute: str,
    value: str,
    label: str,
) -> RecordT:
    path = _bound_path(repo_root, binding, label=label)
    encoded_value = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    matches: list[RecordT] = []
    count = 0
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            count += 1
            line = raw_line[:-1] if raw_line.endswith(b"\n") else raw_line
            if not line:
                raise LF022DiagnosticSubpoolError(f"{label} contains a blank row")
            if encoded_value not in line:
                continue
            try:
                record = model.model_validate_json(line)
            except ValueError as exc:
                raise LF022DiagnosticSubpoolError(
                    f"invalid selected {label} row {line_number}: {exc}"
                ) from exc
            if canonical_json_bytes(record.model_dump(mode="json")) != line:
                raise LF022DiagnosticSubpoolError(
                    f"selected {label} row {line_number} is not canonical JSON"
                )
            if cast(str, getattr(record, attribute)) == value:
                matches.append(record)
    if count != binding.record_count:
        raise LF022DiagnosticSubpoolError(f"{label} record count differs from its binding")
    if len(matches) != 1:
        raise LF022DiagnosticSubpoolError(
            f"{label} must contain exactly one record with {attribute}={value}"
        )
    return matches[0]


def _load_audit(
    repo_root: Path,
    binding: LF022ArtifactBinding,
    *,
    label: str,
) -> LF022PublicPoolAudit:
    return _load_json(repo_root, binding, LF022PublicPoolAudit, label=label)


def _parent_records(
    *,
    repo_root: Path,
    parent: LF022PublicPoolAudit,
    theorem_id: str,
) -> tuple[
    LF022ProductionSourceRecord,
    TheoremRecord,
    RepresentationRecord,
    ContextRecord,
    LF022DenylistClearanceRecord,
]:
    source = _load_selected_jsonl(
        repo_root,
        parent.outputs.source_pool,
        LF022ProductionSourceRecord,
        attribute="theorem_id",
        value=theorem_id,
        label="parent source pool",
    )
    theorem = _load_selected_jsonl(
        repo_root,
        parent.outputs.theorem_records,
        TheoremRecord,
        attribute="theorem_id",
        value=source.theorem_id,
        label="parent theorem records",
    )
    representation = _load_selected_jsonl(
        repo_root,
        parent.outputs.representation_records,
        RepresentationRecord,
        attribute="representation_id",
        value=source.representation_id,
        label="parent representation records",
    )
    context = _load_selected_jsonl(
        repo_root,
        parent.outputs.context_records,
        ContextRecord,
        attribute="context_id",
        value=source.context_id,
        label="parent context records",
    )
    clearance = _load_selected_jsonl(
        repo_root,
        parent.outputs.denylist_clearance_records,
        LF022DenylistClearanceRecord,
        attribute="clearance_id",
        value=source.denylist_clearance_id,
        label="parent denylist clearances",
    )
    return source, theorem, representation, context, clearance


def _verify_source_lineage(
    *,
    repo_root: Path,
    parent: LF022PublicPoolAudit,
    source: LF022ProductionSourceRecord,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
    context: ContextRecord,
    clearance: LF022DenylistClearanceRecord,
) -> None:
    if (
        source.theorem_id != theorem.theorem_id
        or source.representation_id != representation.representation_id
        or source.context_id != context.context_id
        or source.source != theorem.source
        or source.source_revision != theorem.source_revision
        or source.theorem_statement_content_hash != theorem.statement_content_hash
        or source.representation_content_hash != representation.content_hash
        or source.context_fingerprint != context.context_fingerprint
        or source.context_header_hash != context.header_hash
        or representation.theorem_id != theorem.theorem_id
        or representation.context_id != context.context_id
        or source.normalization_version != "repr_v3"
        or representation.normalization_version != "repr_v3"
    ):
        raise LF022DiagnosticSubpoolError(
            "selected parent source/theorem/representation/context linkage differs"
        )
    registry = _load_json(
        repo_root,
        parent.active_benchmark_registry,
        FrozenRegistry,
        label="parent active benchmark registry",
    )
    registry_content_hash = DenylistIndex(registry).registry_content_hash
    benchmark_manifest = _load_json(
        repo_root,
        parent.outputs.benchmark_registry_manifest,
        LF022BenchmarkRegistryManifest,
        label="parent benchmark registry manifest",
    )
    if (
        benchmark_manifest.active_registry != parent.active_benchmark_registry
        or registry_content_hash != parent.active_benchmark_registry_content_hash
        or clearance.benchmark_manifest_id != benchmark_manifest.manifest_id
        or clearance.active_registry_file_sha256 != parent.active_benchmark_registry.sha256
        or clearance.active_registry_content_hash != registry_content_hash
        or clearance.source_locator_id != source.source_locator_id
        or clearance.theorem_id != source.theorem_id
        or clearance.theorem_statement_content_hash != source.theorem_statement_content_hash
        or clearance.representation_id != source.representation_id
        or clearance.representation_content_hash != source.representation_content_hash
        or not clearance.all_identifier_and_content_screens_executed
        or not clearance.clear
    ):
        raise LF022DiagnosticSubpoolError(
            "selected parent source lacks its exact clear denylist result"
        )
    source_registry = _load_json(
        repo_root,
        parent.outputs.public_source_authorization_registry,
        LF022PublicSourceAuthorizationRegistry,
        label="parent public source authorization registry",
    )
    authorizations = tuple(
        item
        for item in source_registry.authorizations
        if item.authorization_id == source.public_source_authorization_id
    )
    if len(authorizations) != 1:
        raise LF022DiagnosticSubpoolError(
            "selected parent source lacks one exact public authorization"
        )
    authorization = authorizations[0]
    if (
        authorization.source != source.source
        or authorization.source_revision != source.source_revision
        or not authorization.source_is_public
        or not authorization.redistribution_allowed
        or not authorization.external_transmission_allowed
    ):
        raise LF022DiagnosticSubpoolError(
            "selected parent source authorization is not public/transmittable"
        )


def _parent_plan(
    *,
    repo_root: Path,
    parent: LF022PublicPoolAudit,
) -> LF022ProductionPlanManifest:
    plan = _load_json(
        repo_root,
        parent.outputs.production_plan,
        LF022ProductionPlanManifest,
        label="parent allocation plan",
    )
    if (
        plan.unique_source_count != parent.selected_count
        or len(plan.tasks) != 2 * parent.selected_count
        or plan.artifacts.family_matrix != parent.outputs.family_matrix
        or plan.artifacts.source_pool != parent.outputs.source_pool
        or plan.artifacts.theorem_records != parent.outputs.theorem_records
        or plan.artifacts.representation_records != parent.outputs.representation_records
        or plan.artifacts.context_records != parent.outputs.context_records
        or plan.artifacts.denylist_clearance_records != parent.outputs.denylist_clearance_records
        or plan.network_execution_authorized
        or plan.semantic_labels_created
        or plan.execution_bindings_present
    ):
        raise LF022DiagnosticSubpoolError(
            "parent public pool audit and allocation plan do not reconcile"
        )
    return plan


def _make_plan(
    *,
    admission: LF022ProductionAdmission,
    family_matrix: LF022ProductionFamilyMatrix,
    source: LF022ProductionSourceRecord,
    proposer_family_id: LF022DiagnosticProposerFamily,
) -> LF022ProductionPlanManifest:
    assignment = _role_assignment_ids(
        proposer_id=proposer_family_id,
        judge_family_ids=family_matrix.judge_family_ids,
        sci_validator_family_ids=family_matrix.sci_validator_family_ids,
        rotation_index=0,
    )
    if assignment is None:
        raise LF022DiagnosticSubpoolError(
            "selected proposer lacks two judges and one distinct SCI validator"
        )
    judge_a, judge_b, validator = assignment
    pins = family_matrix.pins_by_id
    tasks = tuple(
        _make_task(
            source=source,
            distribution=distribution,
            proposer=pins[proposer_family_id],
            judges=(pins[judge_a], pins[judge_b]),
            sci_validator=pins[validator],
            heldout_eval=pins[family_matrix.heldout_eval_family_id],
        )
        for distribution in ("G_sci", "G_open")
    )
    payload: dict[str, object] = {
        "schema_version": 2,
        "profile": "diagnostic_scaffold",
        "scientific_status": "diagnostic_only",
        "artifact_class": "allocation_scaffold",
        "status": "non_executable_allocation_complete",
        "admission_id": admission.admission_id,
        "family_matrix_id": family_matrix.matrix_id,
        "family_matrix_sha256": hash_canonical(family_matrix.model_dump(mode="json")),
        "artifacts": admission.artifacts.model_dump(mode="json"),
        "unique_source_count": 1,
        "source_admission_record_ids": [source.admission_record_id],
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "execution_binding_status": "absent",
        "execution_bindings_present": False,
        "network_execution_authorized": False,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
    }
    return LF022ProductionPlanManifest.model_validate(
        {**payload, "manifest_id": make_id("lf022_production_plan", payload)}
    )


def verify_lf022_diagnostic_subpool(
    *,
    repo_root: Path,
    audit: LF022PublicPoolAudit,
    expected_proposer_family_id: LF022DiagnosticProposerFamily | None = None,
    expected_code_tree_hash: str | None = None,
) -> VerifiedLF022DiagnosticSubpool:
    """Replay a derived diagnostic audit without reopening upstream extraction."""

    if (
        audit.schema_version != 2
        or audit.profile != "diagnostic_scaffold"
        or audit.requested_count != 1
        or audit.selected_count != 1
        or audit.selected_unique_ancestry_count != 1
        or audit.outputs.parent_pool_derivation is None
        or not audit.public_sources_only
        or not audit.private_sft_classic_forbidden
        or audit.network_execution_authorized
        or audit.semantic_labels_created
    ):
        raise LF022DiagnosticSubpoolError(
            "derived diagnostic audit does not have the required one-source safety shape"
        )
    derivation = _load_json(
        repo_root,
        audit.outputs.parent_pool_derivation,
        LF022DiagnosticSubpoolDerivation,
        label="diagnostic parent-pool derivation",
    )
    if expected_proposer_family_id is not None and (
        derivation.proposer_family_id != expected_proposer_family_id
    ):
        raise LF022DiagnosticSubpoolError(
            "diagnostic derivation belongs to a different proposer family"
        )
    if expected_code_tree_hash is not None and (
        derivation.attesting_code_tree_hash != expected_code_tree_hash
    ):
        raise LF022DiagnosticSubpoolError("diagnostic derivation belongs to a different code tree")
    parent = _load_audit(
        repo_root,
        derivation.parent_pool_audit,
        label="parent public-pool audit",
    )
    if (
        parent.schema_version != 1
        or parent.audit_id != derivation.parent_pool_audit_id
        or parent.outputs != derivation.parent_outputs
        or parent.selected_count < 1
        or not parent.public_sources_only
        or not parent.private_sft_classic_forbidden
        or parent.network_execution_authorized
        or parent.semantic_labels_created
    ):
        raise LF022DiagnosticSubpoolError(
            "parent public pool differs from the exact safe derivation binding"
        )
    parent_plan = _parent_plan(repo_root=repo_root, parent=parent)
    if derivation.selected_theorem_id != parent.selection_order_theorem_ids[0]:
        raise LF022DiagnosticSubpoolError(
            "diagnostic source is not the deterministic first parent selection"
        )
    source, theorem, representation, context, clearance = _parent_records(
        repo_root=repo_root,
        parent=parent,
        theorem_id=derivation.selected_theorem_id,
    )
    _verify_source_lineage(
        repo_root=repo_root,
        parent=parent,
        source=source,
        theorem=theorem,
        representation=representation,
        context=context,
        clearance=clearance,
    )
    if (
        derivation.selected_source_admission_record_id != source.admission_record_id
        or derivation.selected_source_locator_id != source.source_locator_id
        or derivation.selected_representation_id != representation.representation_id
        or derivation.selected_context_id != context.context_id
        or derivation.selected_clearance_id != clearance.clearance_id
        or source.admission_record_id not in parent_plan.source_admission_record_ids
    ):
        raise LF022DiagnosticSubpoolError(
            "diagnostic derivation selected lineage differs from parent pool"
        )

    derived_source = _load_selected_jsonl(
        repo_root,
        audit.outputs.source_pool,
        LF022ProductionSourceRecord,
        attribute="admission_record_id",
        value=source.admission_record_id,
        label="derived source pool",
    )
    derived_theorem = _load_selected_jsonl(
        repo_root,
        audit.outputs.theorem_records,
        TheoremRecord,
        attribute="theorem_id",
        value=theorem.theorem_id,
        label="derived theorem records",
    )
    derived_representation = _load_selected_jsonl(
        repo_root,
        audit.outputs.representation_records,
        RepresentationRecord,
        attribute="representation_id",
        value=representation.representation_id,
        label="derived representation records",
    )
    derived_context = _load_selected_jsonl(
        repo_root,
        audit.outputs.context_records,
        ContextRecord,
        attribute="context_id",
        value=context.context_id,
        label="derived context records",
    )
    derived_clearance = _load_selected_jsonl(
        repo_root,
        audit.outputs.denylist_clearance_records,
        LF022DenylistClearanceRecord,
        attribute="clearance_id",
        value=clearance.clearance_id,
        label="derived denylist clearances",
    )
    if (
        derived_source != source
        or derived_theorem != theorem
        or derived_representation != representation
        or derived_context != context
        or derived_clearance != clearance
    ):
        raise LF022DiagnosticSubpoolError(
            "derived singleton records differ from exact parent records"
        )
    if any(
        binding.record_count != 1
        for binding in (
            audit.outputs.source_pool,
            audit.outputs.theorem_records,
            audit.outputs.representation_records,
            audit.outputs.context_records,
            audit.outputs.denylist_clearance_records,
        )
    ):
        raise LF022DiagnosticSubpoolError(
            "derived diagnostic JSONL artifacts must each contain one record"
        )
    if (
        audit.input_theorems != parent.input_theorems
        or audit.input_representations != parent.input_representations
        or audit.input_contexts != parent.input_contexts
        or audit.input_extraction_output_manifest != parent.input_extraction_output_manifest
        or audit.input_representation_output_manifest != parent.input_representation_output_manifest
        or audit.input_mathlib_source_frame != parent.input_mathlib_source_frame
        or audit.active_benchmark_registry != parent.active_benchmark_registry
        or audit.active_benchmark_registry_content_hash
        != parent.active_benchmark_registry_content_hash
        or audit.eligible_count != parent.eligible_count
        or audit.eligible_unique_ancestry_count != parent.eligible_unique_ancestry_count
        or audit.eligible_not_selected_count != parent.eligible_count - 1
        or audit.rejection_counts != parent.rejection_counts
        or audit.selection_order_theorem_ids != (theorem.theorem_id,)
        or audit.selected_source_counts != {theorem.source: 1}
    ):
        raise LF022DiagnosticSubpoolError(
            "derived audit does not preserve exact parent eligibility/input lineage"
        )
    family_matrix = _load_json(
        repo_root,
        audit.outputs.family_matrix,
        LF022ProductionFamilyMatrix,
        label="derived family matrix",
    )
    expected_family_matrix = (
        derivation.derived_family_matrix
        if derivation.derived_family_matrix is not None
        else parent.outputs.family_matrix
    )
    if (
        audit.outputs.family_matrix != expected_family_matrix
        or derivation.proposer_family_id not in family_matrix.proposer_family_ids
    ):
        raise LF022DiagnosticSubpoolError(
            "derived family matrix is not the exact proposer-authorized derivation binding"
        )
    admission = _load_json(
        repo_root,
        audit.outputs.admission,
        LF022ProductionAdmission,
        label="derived allocation admission",
    )
    plan = _load_json(
        repo_root,
        audit.outputs.production_plan,
        LF022ProductionPlanManifest,
        label="derived allocation plan",
    )
    if (
        admission.profile != "diagnostic_scaffold"
        or plan.profile != "diagnostic_scaffold"
        or plan.admission_id != admission.admission_id
        or plan.artifacts != admission.artifacts
        or plan.unique_source_count != 1
        or len(plan.tasks) != 2
        or {task.distribution for task in plan.tasks} != {"G_sci", "G_open"}
        or {task.proposer_family_id for task in plan.tasks} != {derivation.proposer_family_id}
        or any(
            (
                task.admission_record_id,
                task.source_locator_id,
                task.theorem_id,
                task.representation_id,
                task.context_id,
            )
            != (
                source.admission_record_id,
                source.source_locator_id,
                source.theorem_id,
                source.representation_id,
                source.context_id,
            )
            for task in plan.tasks
        )
        or any(task.executable or task.network_execution_authorized for task in plan.tasks)
        or plan.semantic_labels_created
        or plan.silver_promotion_enabled
        or plan.gold_promotion_enabled
        or admission.network_execution_authorized
        or admission.semantic_labels_created
        or plan.artifacts.family_matrix != audit.outputs.family_matrix
        or plan.artifacts.public_source_authorization_registry
        != audit.outputs.public_source_authorization_registry
        or plan.artifacts.benchmark_registry_manifest != audit.outputs.benchmark_registry_manifest
        or plan.artifacts.active_benchmark_registry != audit.active_benchmark_registry
        or plan.artifacts.source_pool != audit.outputs.source_pool
        or plan.artifacts.theorem_records != audit.outputs.theorem_records
        or plan.artifacts.representation_records != audit.outputs.representation_records
        or plan.artifacts.context_records != audit.outputs.context_records
        or plan.artifacts.denylist_clearance_records != audit.outputs.denylist_clearance_records
        or family_matrix.matrix_id != plan.family_matrix_id
    ):
        raise LF022DiagnosticSubpoolError("derived diagnostic admission/plan does not reconcile")
    return VerifiedLF022DiagnosticSubpool(
        audit=audit,
        derivation=derivation,
        parent_audit=parent,
        source=source,
        theorem=theorem,
        representation=representation,
        context=context,
        clearance=clearance,
        plan=plan,
        admission=admission,
    )


def derive_lf022_diagnostic_subpool(
    *,
    repo_root: Path,
    parent_pool_audit_path: Path,
    proposer_family_id: LF022DiagnosticProposerFamily,
    output_directory: Path,
    replacement_family_matrix_path: Path | None = None,
) -> DerivedLF022DiagnosticSubpool:
    """Create and exact-replay one family-specific diagnostic subpool offline."""

    root = repo_root.resolve(strict=True)
    state = collect_code_state(root)
    if state.git_dirty or state.code_tree_hash is None:
        raise LF022DiagnosticSubpoolError(
            "diagnostic subpool derivation requires a clean, hashable code tree"
        )
    parent_binding = _binding(
        root,
        parent_pool_audit_path,
        label="parent public-pool audit",
    )
    parent = _load_audit(root, parent_binding, label="parent public-pool audit")
    if (
        parent.schema_version != 1
        or parent.selected_count < 1
        or not parent.public_sources_only
        or not parent.private_sft_classic_forbidden
        or parent.network_execution_authorized
        or parent.semantic_labels_created
        or parent.outputs.parent_pool_derivation is not None
    ):
        raise LF022DiagnosticSubpoolError(
            "parent must be one non-derived, public-only, non-executable pool"
        )
    _parent_plan(repo_root=root, parent=parent)
    if proposer_family_id == "deepseek_v4":
        if replacement_family_matrix_path is None:
            raise LF022DiagnosticSubpoolError(
                "DeepSeek diagnostic derivation requires --family-matrix"
            )
        family_matrix_binding = _binding(
            root,
            replacement_family_matrix_path,
            label="replacement family matrix",
        )
        if family_matrix_binding == parent.outputs.family_matrix:
            raise LF022DiagnosticSubpoolError(
                "DeepSeek replacement family matrix must differ from the parent matrix"
            )
        derivation_schema_version = 2
    else:
        if replacement_family_matrix_path is not None:
            raise LF022DiagnosticSubpoolError(
                "replacement family matrices are reserved for DeepSeek qualification"
            )
        family_matrix_binding = parent.outputs.family_matrix
        derivation_schema_version = 1
    selected_theorem_id = parent.selection_order_theorem_ids[0]
    source, theorem, representation, context, clearance = _parent_records(
        repo_root=root,
        parent=parent,
        theorem_id=selected_theorem_id,
    )
    _verify_source_lineage(
        repo_root=root,
        parent=parent,
        source=source,
        theorem=theorem,
        representation=representation,
        context=context,
        clearance=clearance,
    )
    output = _output_directory(root, output_directory)
    derivation_payload: dict[str, object] = {
        "schema_version": derivation_schema_version,
        "derivation_kind": "immutable_parent_public_pool_subset",
        "selection_policy": "parent_selection_order_first_v1",
        "parent_pool_audit": parent_binding.model_dump(mode="json"),
        "parent_pool_audit_id": parent.audit_id,
        "parent_outputs": parent.outputs.model_dump(mode="json"),
        **(
            {"derived_family_matrix": family_matrix_binding.model_dump(mode="json")}
            if derivation_schema_version == 2
            else {}
        ),
        "proposer_family_id": proposer_family_id,
        "selected_source_admission_record_id": source.admission_record_id,
        "selected_source_locator_id": source.source_locator_id,
        "selected_theorem_id": theorem.theorem_id,
        "selected_representation_id": representation.representation_id,
        "selected_context_id": context.context_id,
        "selected_clearance_id": clearance.clearance_id,
        "selected_source_count": 1,
        "allocation_task_count": 2,
        "attesting_git_revision": state.git_revision,
        "attesting_code_tree_hash": state.code_tree_hash,
        "public_sources_only": True,
        "private_sft_classic_forbidden": True,
        "network_execution_authorized": False,
        "outputs_provisional_only": True,
        "resolution_outcome": "unresolved",
        "semantic_labels_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    derivation = LF022DiagnosticSubpoolDerivation.model_validate(
        {
            **derivation_payload,
            "derivation_id": make_id(
                "lf022_diagnostic_subpool_derivation",
                derivation_payload,
            ),
        }
    )
    derivation_binding = _write_json(
        repo_root=root,
        path=output / "parent_pool_derivation.json",
        record=derivation,
    )
    source_binding = _write_jsonl(
        repo_root=root,
        path=output / "source_pool.jsonl",
        records=(source,),
    )
    theorem_binding = _write_jsonl(
        repo_root=root,
        path=output / "theorems.jsonl",
        records=(theorem,),
    )
    representation_binding = _write_jsonl(
        repo_root=root,
        path=output / "representations.jsonl",
        records=(representation,),
    )
    context_binding = _write_jsonl(
        repo_root=root,
        path=output / "contexts.jsonl",
        records=(context,),
    )
    clearance_binding = _write_jsonl(
        repo_root=root,
        path=output / "denylist_clearances.jsonl",
        records=(clearance,),
    )
    family_matrix = _load_json(
        root,
        family_matrix_binding,
        LF022ProductionFamilyMatrix,
        label="derived family matrix",
    )
    if proposer_family_id not in family_matrix.proposer_family_ids:
        raise LF022DiagnosticSubpoolError(
            "selected proposer is not authorized by the derived family matrix"
        )
    artifacts = LF022ProductionArtifactSet(
        family_matrix=family_matrix_binding,
        public_source_authorization_registry=(parent.outputs.public_source_authorization_registry),
        benchmark_registry_manifest=parent.outputs.benchmark_registry_manifest,
        active_benchmark_registry=parent.active_benchmark_registry,
        denylist_clearance_records=clearance_binding,
        source_pool=source_binding,
        theorem_records=theorem_binding,
        representation_records=representation_binding,
        context_records=context_binding,
    )
    admission = make_lf022_production_admission(
        family_matrix=family_matrix,
        artifacts=artifacts,
        profile="diagnostic_scaffold",
    )
    admission_binding = _write_json(
        repo_root=root,
        path=output / "admission.json",
        record=admission,
    )
    plan = _make_plan(
        admission=admission,
        family_matrix=family_matrix,
        source=source,
        proposer_family_id=proposer_family_id,
    )
    plan_binding = write_lf022_production_plan(
        repo_root=root,
        relative_path=(output / "production_plan.json").relative_to(root).as_posix(),
        plan=plan,
    )
    outputs = LF022PublicPoolOutputArtifacts(
        family_matrix=family_matrix_binding,
        upstream_extraction_output_manifest=(parent.outputs.upstream_extraction_output_manifest),
        upstream_representation_output_manifest=(
            parent.outputs.upstream_representation_output_manifest
        ),
        mathlib_source_frame=parent.outputs.mathlib_source_frame,
        extraction_manifests=parent.outputs.extraction_manifests,
        source_authorizations=parent.outputs.source_authorizations,
        public_source_authorization_registry=(parent.outputs.public_source_authorization_registry),
        benchmark_registry_manifest=parent.outputs.benchmark_registry_manifest,
        denylist_clearance_records=clearance_binding,
        source_pool=source_binding,
        theorem_records=theorem_binding,
        representation_records=representation_binding,
        context_records=context_binding,
        admission=admission_binding,
        production_plan=plan_binding,
        parent_pool_derivation=derivation_binding,
    )
    audit_payload: dict[str, object] = {
        "schema_version": 2,
        "selection_version": parent.selection_version,
        "profile": "diagnostic_scaffold",
        "requested_count": 1,
        "input_theorems": parent.input_theorems.model_dump(mode="json"),
        "input_representations": parent.input_representations.model_dump(mode="json"),
        "input_contexts": parent.input_contexts.model_dump(mode="json"),
        "input_extraction_output_manifest": (
            parent.input_extraction_output_manifest.model_dump(mode="json")
        ),
        "input_representation_output_manifest": (
            parent.input_representation_output_manifest.model_dump(mode="json")
        ),
        "input_mathlib_source_frame": parent.input_mathlib_source_frame.model_dump(mode="json"),
        "extraction_run_id": parent.extraction_run_id,
        "representation_run_id": parent.representation_run_id,
        "mathlib_source_frame_id": parent.mathlib_source_frame_id,
        "active_benchmark_registry": parent.active_benchmark_registry.model_dump(mode="json"),
        "active_benchmark_registry_content_hash": (parent.active_benchmark_registry_content_hash),
        "input_theorem_count": parent.input_theorem_count,
        "input_representation_count": parent.input_representation_count,
        "input_context_count": parent.input_context_count,
        "orphan_representation_count": parent.orphan_representation_count,
        "unused_context_count": max(parent.input_context_count - 1, 0),
        "eligible_count": parent.eligible_count,
        "eligible_unique_ancestry_count": parent.eligible_unique_ancestry_count,
        "eligible_not_selected_count": parent.eligible_count - 1,
        "selected_count": 1,
        "selected_unique_ancestry_count": 1,
        "rejection_counts": parent.rejection_counts,
        "selected_source_counts": {theorem.source: 1},
        "selection_order_theorem_ids": [theorem.theorem_id],
        "outputs": outputs.model_dump(mode="json"),
        "public_sources_only": True,
        "private_sft_classic_forbidden": True,
        "network_execution_authorized": False,
        "semantic_labels_created": False,
    }
    audit = LF022PublicPoolAudit.model_validate(
        {
            **audit_payload,
            "audit_id": make_id("lf022_public_pool_audit", audit_payload),
        }
    )
    audit_binding = _write_json(
        repo_root=root,
        path=output / "audit.json",
        record=audit,
    )
    verified = verify_lf022_diagnostic_subpool(
        repo_root=root,
        audit=audit,
        expected_proposer_family_id=proposer_family_id,
        expected_code_tree_hash=state.code_tree_hash,
    )
    if (
        verified.derivation != derivation
        or verified.plan != plan
        or verified.admission != admission
    ):
        raise LF022DiagnosticSubpoolError("persisted diagnostic subpool differs from exact replay")
    return DerivedLF022DiagnosticSubpool(
        materialized=MaterializedLF022PublicPool(
            audit=audit,
            audit_binding=audit_binding,
            admission=admission,
            plan=plan,
        ),
        derivation=derivation,
        derivation_binding=derivation_binding,
    )


__all__ = [
    "DerivedLF022DiagnosticSubpool",
    "LF022DiagnosticProposerFamily",
    "LF022DiagnosticSubpoolDerivation",
    "LF022DiagnosticSubpoolError",
    "VerifiedLF022DiagnosticSubpool",
    "derive_lf022_diagnostic_subpool",
    "verify_lf022_diagnostic_subpool",
]
