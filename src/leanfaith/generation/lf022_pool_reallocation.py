"""Reallocate one immutable LF-022 public pool onto a new family matrix.

The selected public sources, theorem statements, representations, contexts,
authorizations, and benchmark clearances are already content-addressed by the
parent audit.  A family-rotation change must not reopen extraction or mutate
those reviewed artifacts.  This module therefore derives only a new offline
admission and task allocation while preserving every source-facing parent
binding byte-for-byte.

The operation performs no provider call and creates no semantic label,
training record, evaluation record, promotion, or Gate credit.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_diagnostic_subpool import (
    LF022DiagnosticSubpoolError,
    _binding,
    _bound_path,
    _load_audit,
    _load_json,
    _parent_plan,
)
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022ProductionAdmission,
    LF022ProductionArtifactSet,
    LF022ProductionFamilyMatrix,
    LF022ProductionPlanManifest,
    LF022ProductionSourceRecord,
    LF022ProductionTask,
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
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.manifest import collect_code_state

_REALLOCATION_PROFILES = frozenset(
    {
        "pilot_scaffold",
        "scientific_production_scaffold",
    }
)


class LF022PoolReallocationError(ValueError):
    """An immutable public pool cannot be safely reallocated."""


class LF022PoolReallocationDerivation(StrictModel):
    """Content-addressed lineage for one family-matrix-only reallocation."""

    schema_version: Literal[1] = 1
    derivation_id: str = Field(pattern=id_pattern("lf022_pool_reallocation"))
    derivation_kind: Literal["immutable_parent_public_pool_family_reallocation"]
    parent_pool_audit: LF022ArtifactBinding
    parent_pool_audit_id: str = Field(pattern=id_pattern("lf022_public_pool_audit"))
    parent_outputs: LF022PublicPoolOutputArtifacts
    replacement_family_matrix: LF022ArtifactBinding
    replacement_family_matrix_id: str = Field(pattern=id_pattern("lf022_family_matrix"))
    profile: Literal["pilot_scaffold", "scientific_production_scaffold"]
    selected_source_count: int = Field(ge=1)
    allocation_task_count: int = Field(ge=2)
    attesting_git_revision: str = Field(min_length=1)
    attesting_code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    source_artifacts_reused_byte_for_byte: Literal[True]
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
    def _canonical(self) -> LF022PoolReallocationDerivation:
        if not (
            self.source_artifacts_reused_byte_for_byte
            and self.public_sources_only
            and self.private_sft_classic_forbidden
            and self.outputs_provisional_only
        ):
            raise ValueError("reallocation must preserve its public-only source bindings")
        if any(
            (
                self.network_execution_authorized,
                self.semantic_labels_created,
                self.training_eligible,
                self.evaluation_eligible,
                self.gate_credit_claimed,
            )
        ):
            raise ValueError("reallocation cannot authorize execution, labels, or dataset use")
        if self.allocation_task_count != 2 * self.selected_source_count:
            raise ValueError("reallocation must contain G_sci and G_open for every source")
        expected = make_id(
            "lf022_pool_reallocation",
            self.model_dump(mode="json", exclude={"derivation_id"}),
        )
        if self.derivation_id != expected:
            raise ValueError("derivation_id does not match canonical reallocation content")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedLF022PoolReallocation:
    """Exact replay result for admission freezing and operational audits."""

    audit: LF022PublicPoolAudit
    derivation: LF022PoolReallocationDerivation
    parent_audit: LF022PublicPoolAudit
    family_matrix: LF022ProductionFamilyMatrix
    admission: LF022ProductionAdmission
    plan: LF022ProductionPlanManifest


@dataclass(frozen=True, slots=True)
class DerivedLF022PoolReallocation:
    """Persisted derived pool plus its exact lineage record."""

    materialized: MaterializedLF022PublicPool
    derivation: LF022PoolReallocationDerivation
    derivation_binding: LF022ArtifactBinding


def _load_source_pool(
    *,
    repo_root: Path,
    audit: LF022PublicPoolAudit,
) -> tuple[LF022ProductionSourceRecord, ...]:
    binding = audit.outputs.source_pool
    try:
        path = _bound_path(repo_root, binding, label="parent source pool")
    except LF022DiagnosticSubpoolError as exc:
        raise LF022PoolReallocationError(str(exc)) from exc
    records: list[LF022ProductionSourceRecord] = []
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line[:-1] if raw_line.endswith(b"\n") else raw_line
            if not line:
                raise LF022PoolReallocationError(
                    f"parent source pool contains a blank row at {line_number}"
                )
            try:
                record = LF022ProductionSourceRecord.model_validate_json(line)
            except ValueError as exc:
                raise LF022PoolReallocationError(
                    f"invalid parent source-pool row {line_number}: {exc}"
                ) from exc
            if canonical_json_bytes(record.model_dump(mode="json")) != line:
                raise LF022PoolReallocationError(
                    f"parent source-pool row {line_number} is not canonical JSON"
                )
            records.append(record)
    if len(records) != binding.record_count:
        raise LF022PoolReallocationError(
            "parent source-pool count differs from its exact artifact binding"
        )
    ordered = tuple(sorted(records, key=lambda item: item.admission_record_id))
    ids = tuple(item.admission_record_id for item in ordered)
    if len(ids) != len(set(ids)):
        raise LF022PoolReallocationError("parent source pool contains duplicate admission IDs")
    return ordered


def _validate_parent_task_lineage(
    *,
    parent_plan: LF022ProductionPlanManifest,
    sources: tuple[LF022ProductionSourceRecord, ...],
) -> None:
    source_ids = tuple(item.admission_record_id for item in sources)
    if source_ids != parent_plan.source_admission_record_ids:
        raise LF022PoolReallocationError(
            "parent source-pool order differs from the exact parent plan"
        )
    tasks_by_source: dict[str, list[LF022ProductionTask]] = defaultdict(list)
    for task in parent_plan.tasks:
        tasks_by_source[task.admission_record_id].append(task)
    if set(tasks_by_source) != set(source_ids):
        raise LF022PoolReallocationError("parent tasks do not cover the exact source pool")
    for source in sources:
        tasks = tasks_by_source[source.admission_record_id]
        if len(tasks) != 2 or {task.distribution for task in tasks} != {"G_sci", "G_open"}:
            raise LF022PoolReallocationError(
                "parent plan does not contain exactly two distributions per source"
            )
        for task in tasks:
            if (
                task.source_locator_id != source.source_locator_id
                or task.theorem_id != source.theorem_id
                or task.representation_id != source.representation_id
                or task.context_id != source.context_id
            ):
                raise LF022PoolReallocationError(
                    "parent task lineage differs from the exact source-pool record"
                )


def _build_reallocated_plan(
    *,
    admission: LF022ProductionAdmission,
    family_matrix: LF022ProductionFamilyMatrix,
    parent_plan: LF022ProductionPlanManifest,
    sources: tuple[LF022ProductionSourceRecord, ...],
) -> LF022ProductionPlanManifest:
    _validate_parent_task_lineage(parent_plan=parent_plan, sources=sources)
    pins = family_matrix.pins_by_id
    proposer_ids = family_matrix.proposer_family_ids
    proposer_counts: Counter[str] = Counter()
    tasks: list[LF022ProductionTask] = []
    for source_index, source in enumerate(sources):
        proposer_id = proposer_ids[source_index % len(proposer_ids)]
        proposer_counts[proposer_id] += 1
        assignment = _role_assignment_ids(
            proposer_id=proposer_id,
            judge_family_ids=family_matrix.judge_family_ids,
            sci_validator_family_ids=family_matrix.sci_validator_family_ids,
            rotation_index=source_index,
        )
        if assignment is None:  # pragma: no cover - matrix validator proves this
            raise LF022PoolReallocationError(
                f"replacement matrix cannot assign roles for {proposer_id}"
            )
        judge_a, judge_b, validator = assignment
        for distribution in ("G_sci", "G_open"):
            tasks.append(
                _make_task(
                    source=source,
                    distribution=distribution,
                    proposer=pins[proposer_id],
                    judges=(pins[judge_a], pins[judge_b]),
                    sci_validator=pins[validator],
                    heldout_eval=pins[family_matrix.heldout_eval_family_id],
                )
            )
    if admission.profile == "scientific_production_scaffold":
        largest = max(proposer_counts.values())
        if largest * 100 > len(sources) * 40:
            raise LF022PoolReallocationError(
                "reallocated scientific plan exceeds the 40% proposer-family cap"
            )
    scientific_status = {
        "pilot_scaffold": "pilot_only",
        "scientific_production_scaffold": "scientific_allocation_scaffold",
    }[admission.profile]
    matrix_hash = hash_canonical(family_matrix.model_dump(mode="json"))
    payload: dict[str, object] = {
        "schema_version": 2,
        "profile": admission.profile,
        "scientific_status": scientific_status,
        "artifact_class": "allocation_scaffold",
        "status": "non_executable_allocation_complete",
        "admission_id": admission.admission_id,
        "family_matrix_id": family_matrix.matrix_id,
        "family_matrix_sha256": matrix_hash,
        "artifacts": admission.artifacts.model_dump(mode="json"),
        "unique_source_count": len(sources),
        "source_admission_record_ids": [item.admission_record_id for item in sources],
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


def _production_artifacts(
    *,
    parent: LF022PublicPoolAudit,
    family_matrix_binding: LF022ArtifactBinding,
) -> LF022ProductionArtifactSet:
    return LF022ProductionArtifactSet(
        family_matrix=family_matrix_binding,
        public_source_authorization_registry=parent.outputs.public_source_authorization_registry,
        benchmark_registry_manifest=parent.outputs.benchmark_registry_manifest,
        active_benchmark_registry=parent.active_benchmark_registry,
        denylist_clearance_records=parent.outputs.denylist_clearance_records,
        source_pool=parent.outputs.source_pool,
        theorem_records=parent.outputs.theorem_records,
        representation_records=parent.outputs.representation_records,
        context_records=parent.outputs.context_records,
    )


def _audit_base(audit: LF022PublicPoolAudit) -> dict[str, object]:
    return audit.model_dump(
        mode="json",
        exclude={"schema_version", "audit_id", "outputs"},
    )


def verify_lf022_pool_reallocation(
    *,
    repo_root: Path,
    audit: LF022PublicPoolAudit,
    expected_code_tree_hash: str | None = None,
) -> VerifiedLF022PoolReallocation:
    """Exact-replay a family-only derived public-pool allocation."""

    if (
        audit.schema_version != 2
        or audit.profile not in _REALLOCATION_PROFILES
        or audit.outputs.parent_pool_derivation is None
        or not audit.public_sources_only
        or not audit.private_sft_classic_forbidden
        or audit.network_execution_authorized
        or audit.semantic_labels_created
    ):
        raise LF022PoolReallocationError(
            "derived audit does not have the required public reallocation shape"
        )
    try:
        derivation = _load_json(
            repo_root,
            audit.outputs.parent_pool_derivation,
            LF022PoolReallocationDerivation,
            label="public-pool reallocation derivation",
        )
        parent = _load_audit(
            repo_root,
            derivation.parent_pool_audit,
            label="parent public-pool audit",
        )
        parent_plan = _parent_plan(repo_root=repo_root, parent=parent)
    except LF022DiagnosticSubpoolError as exc:
        raise LF022PoolReallocationError(str(exc)) from exc
    if (
        parent.schema_version != 1
        or parent.outputs.parent_pool_derivation is not None
        or parent.audit_id != derivation.parent_pool_audit_id
        or parent.outputs != derivation.parent_outputs
        or parent.profile != derivation.profile
        or parent.selected_count != derivation.selected_source_count
        or len(parent_plan.tasks) != derivation.allocation_task_count
        or parent.profile not in _REALLOCATION_PROFILES
        or not parent.public_sources_only
        or not parent.private_sft_classic_forbidden
        or parent.network_execution_authorized
        or parent.semantic_labels_created
    ):
        raise LF022PoolReallocationError(
            "parent pool differs from the exact safe reallocation binding"
        )
    if expected_code_tree_hash is not None and (
        derivation.attesting_code_tree_hash != expected_code_tree_hash
    ):
        raise LF022PoolReallocationError("reallocation belongs to a different code tree")
    if _audit_base(audit) != _audit_base(parent):
        raise LF022PoolReallocationError(
            "derived audit changes source selection, accounting, or benchmark inputs"
        )
    try:
        family_matrix = _load_json(
            repo_root,
            derivation.replacement_family_matrix,
            LF022ProductionFamilyMatrix,
            label="replacement family matrix",
        )
        admission = _load_json(
            repo_root,
            audit.outputs.admission,
            LF022ProductionAdmission,
            label="reallocated admission",
        )
        plan = _load_json(
            repo_root,
            audit.outputs.production_plan,
            LF022ProductionPlanManifest,
            label="reallocated production plan",
        )
    except LF022DiagnosticSubpoolError as exc:
        raise LF022PoolReallocationError(str(exc)) from exc
    if (
        family_matrix.matrix_id != derivation.replacement_family_matrix_id
        or audit.outputs.family_matrix != derivation.replacement_family_matrix
        or derivation.replacement_family_matrix == parent.outputs.family_matrix
    ):
        raise LF022PoolReallocationError(
            "replacement family matrix differs from the derivation binding"
        )
    expected_artifacts = _production_artifacts(
        parent=parent,
        family_matrix_binding=derivation.replacement_family_matrix,
    )
    expected_admission = make_lf022_production_admission(
        family_matrix=family_matrix,
        artifacts=expected_artifacts,
        profile=parent.profile,
    )
    sources = _load_source_pool(repo_root=repo_root, audit=parent)
    expected_plan = _build_reallocated_plan(
        admission=expected_admission,
        family_matrix=family_matrix,
        parent_plan=parent_plan,
        sources=sources,
    )
    expected_outputs = parent.outputs.model_copy(
        update={
            "family_matrix": derivation.replacement_family_matrix,
            "admission": audit.outputs.admission,
            "production_plan": audit.outputs.production_plan,
            "parent_pool_derivation": audit.outputs.parent_pool_derivation,
        }
    )
    if (
        audit.outputs != expected_outputs
        or admission != expected_admission
        or plan != expected_plan
        or plan.artifacts != expected_artifacts
        or plan.unique_source_count != parent.selected_count
        or len(plan.tasks) != 2 * parent.selected_count
    ):
        raise LF022PoolReallocationError(
            "reallocated outputs, admission, or plan differ from exact replay"
        )
    return VerifiedLF022PoolReallocation(
        audit=audit,
        derivation=derivation,
        parent_audit=parent,
        family_matrix=family_matrix,
        admission=admission,
        plan=plan,
    )


def derive_lf022_pool_reallocation(
    *,
    repo_root: Path,
    parent_pool_audit_path: Path,
    replacement_family_matrix_path: Path,
    output_directory: Path,
) -> DerivedLF022PoolReallocation:
    """Create and exact-replay one offline family-only public-pool reallocation."""

    root = repo_root.resolve(strict=True)
    state = collect_code_state(root)
    if state.git_dirty or state.code_tree_hash is None:
        raise LF022PoolReallocationError(
            "public-pool reallocation requires a clean, hashable code tree"
        )
    try:
        parent_binding = _binding(
            root,
            parent_pool_audit_path,
            label="parent public-pool audit",
        )
        parent = _load_audit(root, parent_binding, label="parent public-pool audit")
        parent_plan = _parent_plan(repo_root=root, parent=parent)
        matrix_binding = _binding(
            root,
            replacement_family_matrix_path,
            label="replacement family matrix",
        )
        family_matrix = _load_json(
            root,
            matrix_binding,
            LF022ProductionFamilyMatrix,
            label="replacement family matrix",
        )
    except LF022DiagnosticSubpoolError as exc:
        raise LF022PoolReallocationError(str(exc)) from exc
    if (
        parent.schema_version != 1
        or parent.profile not in _REALLOCATION_PROFILES
        or parent.outputs.parent_pool_derivation is not None
        or not parent.public_sources_only
        or not parent.private_sft_classic_forbidden
        or parent.network_execution_authorized
        or parent.semantic_labels_created
    ):
        raise LF022PoolReallocationError(
            "parent must be one non-derived pilot/scientific public-only pool"
        )
    if matrix_binding == parent.outputs.family_matrix:
        raise LF022PoolReallocationError("replacement matrix must differ from the parent matrix")
    if len(family_matrix.proposer_family_ids) < 3:
        raise LF022PoolReallocationError(
            "replacement scientific matrix must retain at least three proposer families"
        )
    sources = _load_source_pool(repo_root=root, audit=parent)
    _validate_parent_task_lineage(parent_plan=parent_plan, sources=sources)
    output = _output_directory(root, output_directory)
    derivation_payload: dict[str, object] = {
        "schema_version": 1,
        "derivation_kind": "immutable_parent_public_pool_family_reallocation",
        "parent_pool_audit": parent_binding.model_dump(mode="json"),
        "parent_pool_audit_id": parent.audit_id,
        "parent_outputs": parent.outputs.model_dump(mode="json"),
        "replacement_family_matrix": matrix_binding.model_dump(mode="json"),
        "replacement_family_matrix_id": family_matrix.matrix_id,
        "profile": parent.profile,
        "selected_source_count": parent.selected_count,
        "allocation_task_count": 2 * parent.selected_count,
        "attesting_git_revision": state.git_revision,
        "attesting_code_tree_hash": state.code_tree_hash,
        "source_artifacts_reused_byte_for_byte": True,
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
    derivation = LF022PoolReallocationDerivation.model_validate(
        {
            **derivation_payload,
            "derivation_id": make_id("lf022_pool_reallocation", derivation_payload),
        }
    )
    derivation_binding = _write_json(
        repo_root=root,
        path=output / "parent_pool_reallocation.json",
        record=derivation,
    )
    artifacts = _production_artifacts(
        parent=parent,
        family_matrix_binding=matrix_binding,
    )
    admission = make_lf022_production_admission(
        family_matrix=family_matrix,
        artifacts=artifacts,
        profile=parent.profile,
    )
    admission_binding = _write_json(
        repo_root=root,
        path=output / "admission.json",
        record=admission,
    )
    plan = _build_reallocated_plan(
        admission=admission,
        family_matrix=family_matrix,
        parent_plan=parent_plan,
        sources=sources,
    )
    plan_binding = write_lf022_production_plan(
        repo_root=root,
        relative_path=(output / "production_plan.json").relative_to(root).as_posix(),
        plan=plan,
    )
    outputs = parent.outputs.model_copy(
        update={
            "family_matrix": matrix_binding,
            "admission": admission_binding,
            "production_plan": plan_binding,
            "parent_pool_derivation": derivation_binding,
        }
    )
    audit_payload = {
        "schema_version": 2,
        **_audit_base(parent),
        "outputs": outputs.model_dump(mode="json"),
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
    verified = verify_lf022_pool_reallocation(
        repo_root=root,
        audit=audit,
        expected_code_tree_hash=state.code_tree_hash,
    )
    if verified.admission != admission or verified.plan != plan:
        raise LF022PoolReallocationError(
            "persisted public-pool reallocation differs from exact replay"
        )
    return DerivedLF022PoolReallocation(
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
    "DerivedLF022PoolReallocation",
    "LF022PoolReallocationDerivation",
    "LF022PoolReallocationError",
    "VerifiedLF022PoolReallocation",
    "derive_lf022_pool_reallocation",
    "verify_lf022_pool_reallocation",
]
