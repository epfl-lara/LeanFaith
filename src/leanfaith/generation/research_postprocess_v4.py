"""Replayable LF-021 postprocessing for collector-v3 research tranches.

Postprocess v4 is a separately versioned envelope around the immutable,
already-audited postprocess-v3 processing primitives.  It consumes only a
completed :mod:`leanfaith.generation.research_collection_v3` bundle and binds
the tranche identity plus the truthful operational-pool dialect.

The v3 processing implementation remains the executable correctness primitive
for primary parsing, conservative Lean-backed recovery, LeanInteract
validation, benchmark screening, alpha-identity deduplication, and unresolved
REVIEW-only admission.  V4 records that reuse explicitly through
``shared_processing_record_schema`` and an exact v3 input-binding hash.  No
v1/v2/v3 implementation or recovery-parser bytes are modified or reinterpreted.

One collection invocation remains the failure-isolation unit.  The stage
creates no semantic label, training supervision, or Gate-5 claim.
"""

from __future__ import annotations

import datetime
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation import research_collection as collection_v1
from leanfaith.generation import research_collection_v3 as collection_v3
from leanfaith.generation import research_postprocess as postprocess_v1
from leanfaith.generation import research_postprocess_v3 as postprocess_v3
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

_HEX64 = r"^[0-9a-f]{64}$"
_TRANCHE = r"^[a-z][a-z0-9_]*$"
_TERMINAL_ID = r"^research_postprocess_v4_terminal:[0-9a-f]{64}$"
_FAMILY_ID = r"^research_postprocess_v4_family:[0-9a-f]{64}$"
_MANIFEST_ID = r"^research_postprocess_v4_manifest:[0-9a-f]{64}$"
_COLLECTOR_V3_ARTIFACT = "src/leanfaith/generation/research_collection_v3.py"
_SHARED_PROCESSOR_V3_ARTIFACT = "src/leanfaith/generation/research_postprocess_v3.py"

PoolDialect = Literal[
    "gate3_algebra_operational_v1",
    "cross_domain_operational_v1",
]
PoolSource = Literal[
    "mathlib_gate3_docstrings_operational_v1",
    "mathlib_cross_domain_docstrings_operational_v1",
]
PoolManifestKind = Literal[
    "lf021_gate3_docstrings_operational_problem_pool_v1",
    "lf021_cross_domain_docstrings_operational_problem_pool_v1",
]


class ResearchPostprocessV4Error(RuntimeError):
    """A collector-v3 input, v4 output, or replay invariant failed."""


class ResearchPostprocessV4InputBinding(StrictModel):
    """Exact collector-v3 denominator plus every executable dependency."""

    schema_version: Literal[4] = 4
    tranche_id: str = Field(pattern=_TRANCHE)
    pool_dialect: PoolDialect
    pool_source: PoolSource
    pool_manifest_artifact_kind: PoolManifestKind
    collection_config: postprocess_v1.PostprocessArtifactBinding
    collection_plan: postprocess_v1.PostprocessArtifactBinding
    collection_manifest: postprocess_v1.PostprocessArtifactBinding
    collection_plan_id: str
    collection_plan_hash: str = Field(pattern=_HEX64)
    collection_manifest_id: str
    collection_terminal_artifacts: dict[str, str]
    collection_family_session_artifacts: dict[str, str]
    raw_collection_artifacts_by_invocation: dict[str, dict[str, str]]
    problem_pool_manifest: postprocess_v1.PostprocessArtifactBinding
    problem_pool_records: postprocess_v1.PostprocessArtifactBinding
    context: postprocess_v1.PostprocessArtifactBinding
    import_header: postprocess_v1.PostprocessArtifactBinding
    source_matrix: postprocess_v1.PostprocessArtifactBinding
    reference_theorems: postprocess_v1.PostprocessArtifactBinding
    reference_representations: postprocess_v1.PostprocessArtifactBinding
    active_registry_artifacts: dict[str, postprocess_v1.PostprocessArtifactBinding]
    active_registry_content_hash: str = Field(pattern=_HEX64)
    collector_implementation: postprocess_v1.PostprocessArtifactBinding
    primary_parser_implementations: dict[str, postprocess_v1.PostprocessArtifactBinding]
    recovery_implementation: postprocess_v1.PostprocessArtifactBinding
    shared_processing_implementation: postprocess_v1.PostprocessArtifactBinding
    implementation: postprocess_v1.PostprocessArtifactBinding
    shared_execution_record_schema: Literal["lf021_research_execution_records_v1"] = (
        "lf021_research_execution_records_v1"
    )
    shared_processing_record_schema: Literal["lf021_research_postprocess_records_v3"] = (
        "lf021_research_postprocess_records_v3"
    )
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    seed_count_by_family: dict[str, int]
    expected_invocations: int = Field(ge=1)
    problem_record_ids: tuple[str, ...] = Field(min_length=1)
    invocation_ids: tuple[str, ...] = Field(min_length=1)
    family_ids: tuple[str, ...] = Field(min_length=3, max_length=3)

    @property
    def binding_hash(self) -> str:
        return hash_canonical(
            {
                "schema": "lf021_research_postprocess_input_binding_v4",
                **self.model_dump(mode="json"),
            }
        )

    def shared_v3_binding(self) -> postprocess_v3.ResearchPostprocessV3InputBinding:
        """Project the exact immutable v3 processing dependency envelope."""

        return postprocess_v3.ResearchPostprocessV3InputBinding(
            collection_config=self.collection_config,
            collection_plan=self.collection_plan,
            collection_manifest=self.collection_manifest,
            collection_plan_id=self.collection_plan_id,
            collection_plan_hash=self.collection_plan_hash,
            collection_manifest_id=self.collection_manifest_id,
            collection_terminal_artifacts=self.collection_terminal_artifacts,
            collection_family_session_artifacts=self.collection_family_session_artifacts,
            raw_collection_artifacts_by_invocation=(self.raw_collection_artifacts_by_invocation),
            problem_pool_manifest=self.problem_pool_manifest,
            problem_pool_records=self.problem_pool_records,
            context=self.context,
            import_header=self.import_header,
            source_matrix=self.source_matrix,
            reference_theorems=self.reference_theorems,
            reference_representations=self.reference_representations,
            active_registry_artifacts=self.active_registry_artifacts,
            active_registry_content_hash=self.active_registry_content_hash,
            collector_implementation=self.collector_implementation,
            primary_parser_implementations=self.primary_parser_implementations,
            recovery_implementation=self.recovery_implementation,
            implementation=self.shared_processing_implementation,
            problem_count=self.problem_count,
            seed_count_by_family=self.seed_count_by_family,
            expected_invocations=self.expected_invocations,
            problem_record_ids=self.problem_record_ids,
            invocation_ids=self.invocation_ids,
            family_ids=self.family_ids,
        )

    @property
    def shared_processing_input_binding_hash(self) -> str:
        return self.shared_v3_binding().binding_hash

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        dialect = {
            "gate3_algebra_operational_v1": (
                "mathlib_gate3_docstrings_operational_v1",
                "lf021_gate3_docstrings_operational_problem_pool_v1",
            ),
            "cross_domain_operational_v1": (
                "mathlib_cross_domain_docstrings_operational_v1",
                "lf021_cross_domain_docstrings_operational_problem_pool_v1",
            ),
        }
        if (self.pool_source, self.pool_manifest_artifact_kind) != dialect[self.pool_dialect]:
            raise ValueError("v4 pool dialect, source, and manifest kind disagree")
        if self.problem_record_ids != tuple(sorted(set(self.problem_record_ids))):
            raise ValueError("v4 problem IDs must be sorted and unique")
        if len(self.problem_record_ids) != self.problem_count:
            raise ValueError("v4 problem IDs do not reconcile problem_count")
        if self.invocation_ids != tuple(sorted(set(self.invocation_ids))):
            raise ValueError("v4 invocation IDs must be sorted and unique")
        if len(self.invocation_ids) != self.expected_invocations:
            raise ValueError("v4 invocation IDs do not reconcile expected_invocations")
        if self.family_ids != tuple(sorted(set(self.family_ids))):
            raise ValueError("v4 requires three sorted unique family IDs")
        if list(self.seed_count_by_family) != sorted(self.seed_count_by_family):
            raise ValueError("v4 seed_count_by_family must be sorted")
        if set(self.seed_count_by_family) != set(self.family_ids):
            raise ValueError("v4 seed-count families differ from family IDs")
        if any(count < 1 for count in self.seed_count_by_family.values()):
            raise ValueError("v4 requires at least one seed per family")
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if self.expected_invocations != expected:
            raise ValueError("v4 expected count differs from problem x family seeds")
        if (
            list(self.collection_terminal_artifacts) != sorted(self.collection_terminal_artifacts)
            or len(self.collection_terminal_artifacts) != expected
        ):
            raise ValueError("v4 terminal bindings do not reconcile denominator")
        if list(self.collection_family_session_artifacts) != sorted(
            self.collection_family_session_artifacts
        ):
            raise ValueError("v4 family-session bindings must be sorted")
        if list(self.raw_collection_artifacts_by_invocation) != list(self.invocation_ids):
            raise ValueError("v4 raw-lineage bindings must cover invocations in order")
        for invocation_id, artifacts in self.raw_collection_artifacts_by_invocation.items():
            if list(artifacts) != sorted(artifacts):
                raise ValueError(f"v4 raw artifacts are not sorted: {invocation_id}")
            if any(re.fullmatch(_HEX64, digest) is None for digest in artifacts.values()):
                raise ValueError(f"v4 raw artifacts contain a non-SHA: {invocation_id}")
        for mapping_name in (
            "collection_terminal_artifacts",
            "collection_family_session_artifacts",
        ):
            mapping = getattr(self, mapping_name)
            if any(re.fullmatch(_HEX64, digest) is None for digest in mapping.values()):
                raise ValueError(f"{mapping_name} values must be SHA-256")
        if list(self.primary_parser_implementations) != sorted(
            self.primary_parser_implementations
        ) or set(self.primary_parser_implementations) != set(self.family_ids):
            raise ValueError("v4 primary parser bindings must cover every family")
        if list(self.active_registry_artifacts) != sorted(self.active_registry_artifacts):
            raise ValueError("v4 active-registry bindings must be sorted")
        if self.collector_implementation.artifact != _COLLECTOR_V3_ARTIFACT:
            raise ValueError("v4 must bind collector-v3")
        if self.shared_processing_implementation.artifact != _SHARED_PROCESSOR_V3_ARTIFACT:
            raise ValueError("v4 must bind the immutable postprocess-v3 engine")
        self.shared_v3_binding()
        return self


class ResearchPostprocessV4Terminal(StrictModel):
    """One v4 tranche terminal with a complete immutable v3 processing record."""

    schema_version: Literal[4] = 4
    record_kind: Literal["lf021_research_postprocess_terminal_v4"] = (
        "lf021_research_postprocess_terminal_v4"
    )
    artifact_class: Literal["research"] = "research"
    terminal_id: str = Field(pattern=_TERMINAL_ID)
    input_binding_hash: str = Field(pattern=_HEX64)
    shared_processing_input_binding_hash: str = Field(pattern=_HEX64)
    shared_processing_terminal_id: str
    shared_processing_terminal_sha256: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    pool_dialect: PoolDialect
    pool_source: PoolSource
    invocation_id: str
    family_id: str
    problem_record_id: str
    seed: int = Field(ge=0)
    status: postprocess_v1.ResearchPostprocessStatus
    terminal_stage: postprocess_v1.ResearchPostprocessStage
    record_time_basis: datetime.datetime
    parser_executed: bool
    lean_validation_executed: bool
    screening_executed: bool
    semantic_pool_admitted: bool
    raw_lineage_hashes: dict[str, str]
    output_artifact_hashes: dict[str, str]
    candidate_theorem_id: str | None = None
    pair_ids: tuple[str, ...] = ()
    nl_lean_id: str | None = None
    same_claim: None = None
    relation: None = None
    resolution_outcome: Literal["unresolved"] | None = None
    quality_tier: Literal["unknown"] | None = None
    requires_adjudication: bool = False
    decision: Literal["REVIEW"] | None = None
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    shared_processing_terminal: postprocess_v3.ResearchPostprocessV3Terminal

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "terminal_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.record_time_basis)
        shared = self.shared_processing_terminal
        projection = {
            "invocation_id": self.invocation_id,
            "family_id": self.family_id,
            "problem_record_id": self.problem_record_id,
            "seed": self.seed,
            "status": self.status,
            "terminal_stage": self.terminal_stage,
            "record_time_basis": self.record_time_basis,
            "parser_executed": self.parser_executed,
            "lean_validation_executed": self.lean_validation_executed,
            "screening_executed": self.screening_executed,
            "semantic_pool_admitted": self.semantic_pool_admitted,
            "raw_lineage_hashes": self.raw_lineage_hashes,
            "output_artifact_hashes": self.output_artifact_hashes,
            "candidate_theorem_id": self.candidate_theorem_id,
            "pair_ids": self.pair_ids,
            "nl_lean_id": self.nl_lean_id,
            "same_claim": self.same_claim,
            "relation": self.relation,
            "resolution_outcome": self.resolution_outcome,
            "quality_tier": self.quality_tier,
            "requires_adjudication": self.requires_adjudication,
            "decision": self.decision,
            "semantic_labels_created": self.semantic_labels_created,
            "supervision_eligible": self.supervision_eligible,
            "gate_5g_credit_claimed": self.gate_5g_credit_claimed,
            "gate_5_closed": self.gate_5_closed,
        }
        shared_projection = {key: getattr(shared, key) for key in projection}
        if projection != shared_projection:
            raise ValueError("v4 terminal projection differs from shared v3 terminal")
        if (
            self.shared_processing_terminal_id != shared.terminal_id
            or self.shared_processing_input_binding_hash != shared.input_binding_hash
            or self.shared_processing_terminal_sha256
            != sha256_hex(postprocess_v1._canonical_record_bytes(shared))
        ):
            raise ValueError("v4 shared-processing terminal binding differs")
        for mapping_name in ("raw_lineage_hashes", "output_artifact_hashes"):
            mapping = getattr(self, mapping_name)
            if list(mapping) != sorted(mapping):
                raise ValueError(f"{mapping_name} must be sorted")
            if any(re.fullmatch(_HEX64, digest) is None for digest in mapping.values()):
                raise ValueError(f"{mapping_name} values must be SHA-256")
        identity = "research_postprocess_v4_terminal:" + hash_canonical(
            {"schema": "lf021_research_postprocess_terminal_v4", **self.id_payload()}
        )
        if self.terminal_id != identity:
            raise ValueError("v4 terminal ID does not match payload")
        return self


class ResearchPostprocessV4FamilyReport(StrictModel):
    """Dynamic per-family tranche accounting."""

    schema_version: Literal[4] = 4
    report_id: str = Field(pattern=_FAMILY_ID)
    input_binding_hash: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    pool_dialect: PoolDialect
    pool_source: PoolSource
    family_id: str
    problem_count: int = Field(ge=1)
    seed_count: int = Field(ge=1)
    expected_invocations: int = Field(ge=1)
    terminal_invocations: int = Field(ge=1)
    status_counts: dict[str, int]
    recovery_status_counts: dict[str, int]
    collection_raw_count: int = Field(ge=0)
    parser_success_count: int = Field(ge=0)
    admitted_unresolved_count: int = Field(ge=0)
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key != "report_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected = self.problem_count * self.seed_count
        if self.expected_invocations != expected or self.terminal_invocations != expected:
            raise ValueError("v4 family denominator differs from problems x seeds")
        if sum(self.status_counts.values()) != expected:
            raise ValueError("v4 family status counts do not reconcile")
        if sum(self.recovery_status_counts.values()) != expected:
            raise ValueError("v4 family recovery counts do not reconcile")
        if any(
            count > expected
            for count in (
                self.collection_raw_count,
                self.parser_success_count,
                self.admitted_unresolved_count,
            )
        ):
            raise ValueError("v4 family stage count exceeds denominator")
        identity = "research_postprocess_v4_family:" + hash_canonical(
            {"schema": "lf021_research_postprocess_family_v4", **self.id_payload()}
        )
        if self.report_id != identity:
            raise ValueError("v4 family report ID does not match payload")
        return self


class ResearchPostprocessV4Manifest(StrictModel):
    """Complete postprocess-v4 accounting and immutable artifact index."""

    schema_version: Literal[4] = 4
    manifest_id: str = Field(pattern=_MANIFEST_ID)
    input_binding: ResearchPostprocessV4InputBinding
    input_binding_hash: str = Field(pattern=_HEX64)
    shared_processing_input_binding_hash: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    pool_dialect: PoolDialect
    pool_source: PoolSource
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    seed_count_by_family: dict[str, int]
    expected_invocations: int = Field(ge=1)
    terminal_invocations: int = Field(ge=1)
    status_counts: dict[str, int]
    recovery_status_counts: dict[str, int]
    terminal_artifacts: dict[str, str]
    family_report_artifacts: dict[str, str]
    admitted_pair_count: int = Field(ge=0)
    admitted_nl_lean_count: int = Field(ge=0)
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "manifest_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        binding = self.input_binding
        if (
            self.input_binding_hash != binding.binding_hash
            or self.shared_processing_input_binding_hash
            != binding.shared_processing_input_binding_hash
        ):
            raise ValueError("v4 manifest input binding hash differs")
        if (
            self.tranche_id != binding.tranche_id
            or self.pool_dialect != binding.pool_dialect
            or self.pool_source != binding.pool_source
            or self.problem_count != binding.problem_count
            or self.seed_count_by_family != binding.seed_count_by_family
            or self.expected_invocations != binding.expected_invocations
            or self.terminal_invocations != self.expected_invocations
        ):
            raise ValueError("v4 manifest denominator or pool identity differs")
        if sum(self.status_counts.values()) != self.expected_invocations:
            raise ValueError("v4 manifest status counts do not reconcile")
        if sum(self.recovery_status_counts.values()) != self.expected_invocations:
            raise ValueError("v4 manifest recovery counts do not reconcile")
        if (
            len(self.terminal_artifacts) != self.expected_invocations
            or len(self.family_report_artifacts) != self.family_count
        ):
            raise ValueError("v4 manifest artifact denominator differs")
        for mapping_name in ("terminal_artifacts", "family_report_artifacts"):
            mapping = getattr(self, mapping_name)
            if list(mapping) != sorted(mapping):
                raise ValueError(f"{mapping_name} must be sorted")
            if any(re.fullmatch(_HEX64, digest) is None for digest in mapping.values()):
                raise ValueError(f"{mapping_name} values must be SHA-256")
        if self.admitted_nl_lean_count > self.expected_invocations:
            raise ValueError("v4 admitted NL-Lean count exceeds denominator")
        identity = "research_postprocess_v4_manifest:" + hash_canonical(
            {"schema": "lf021_research_postprocess_manifest_v4", **self.id_payload()}
        )
        if self.manifest_id != identity:
            raise ValueError("v4 manifest ID does not match payload")
        return self


@dataclass(frozen=True, slots=True)
class LoadedResearchPostprocessV4:
    """Bound v4 envelope plus the exact shared v3 execution projection."""

    base: postprocess_v3._PostprocessBase
    input_binding: ResearchPostprocessV4InputBinding
    shared_v3: postprocess_v3.LoadedResearchPostprocessV3


@dataclass(frozen=True, slots=True)
class ResearchPostprocessV4Run:
    output_root: Path
    manifest_path: Path
    manifest: ResearchPostprocessV4Manifest
    terminals: tuple[ResearchPostprocessV4Terminal, ...]
    family_reports: tuple[ResearchPostprocessV4FamilyReport, ...]


def validate_collection_v3_denominator(
    plan: collection_v3.ResearchCollectionPlanV3,
    manifest: collection_v3.ResearchCollectionManifestV3,
) -> None:
    """Fail closed on every collector-v3 tranche/cardinality relationship."""

    if (
        manifest.plan_id != plan.plan_id
        or manifest.plan_hash != plan.plan_hash
        or manifest.tranche_id != plan.tranche_id
        or manifest.shared_execution_record_schema != plan.shared_execution_record_schema
        or manifest.problem_count != plan.problem_count
        or manifest.family_count != plan.family_count
        or manifest.seed_count_by_family != plan.seed_count_by_family
        or manifest.expected_candidate_count != plan.expected_candidate_count
        or manifest.terminal_candidate_count != plan.expected_candidate_count
        or len(plan.problem_record_ids) != plan.problem_count
        or len(plan.family_bindings) != plan.family_count
        or len(plan.invocations) != plan.expected_candidate_count
        or len(manifest.terminal_artifact_hashes) != plan.expected_candidate_count
    ):
        raise ResearchPostprocessV4Error("collector-v3 plan/manifest denominator differs")
    if (
        plan.actual_collection_performed
        or plan.semantic_labels_created
        or plan.gate_5g_credit_claimed
        or plan.gate_5_closed
        or not manifest.actual_collection_performed
        or manifest.semantic_labels_created
        or manifest.gate_5g_credit_claimed
        or manifest.gate_5_closed
    ):
        raise ResearchPostprocessV4Error("collector-v3 plan/manifest policy differs")
    problem_ids = {item.problem_record_id for item in plan.invocations}
    if problem_ids != set(plan.problem_record_ids):
        raise ResearchPostprocessV4Error("collector-v3 invocation problems differ from pool")
    family_ids = tuple(binding.family_id for binding in plan.family_bindings)
    if family_ids != tuple(sorted(set(family_ids))):
        raise ResearchPostprocessV4Error("collector-v3 family bindings are not canonical")
    counts = Counter(item.family_id for item in plan.invocations)
    for family_id, seed_count in plan.seed_count_by_family.items():
        if counts[family_id] != plan.problem_count * seed_count:
            raise ResearchPostprocessV4Error(
                f"collector-v3 invocation count differs for {family_id}"
            )


def _repo_binding(
    repo_root: Path,
    path: Path,
) -> postprocess_v1.PostprocessArtifactBinding:
    return postprocess_v1._content_addressed_artifact(repo_root, path)


def _reference_artifact(
    *,
    repo_root: Path,
    pool_document: dict[str, object],
    pool_dialect: PoolDialect,
    field: Literal["reference_theorems", "reference_representations"],
) -> Path:
    if pool_dialect == "cross_domain_operational_v1":
        binding = collection_v3._nested_manifest_binding(
            pool_document,
            "output_artifacts",
            field,
        )
        return collection_v3._resolve_pool_binding(repo_root, binding)
    legacy_field = f"{field}_artifact"
    binding = collection_v3._manifest_binding(pool_document, legacy_field)
    return collection_v3._resolve_pool_binding(repo_root, binding)


def _load_collection_terminals(
    *,
    repo_root: Path,
    collection_root: Path,
    plan: collection_v3.ResearchCollectionPlanV3,
    manifest: collection_v3.ResearchCollectionManifestV3,
) -> tuple[
    dict[str, collection_v1.ResearchCollectionTerminal],
    dict[str, Path],
    dict[str, str],
]:
    try:
        return postprocess_v3._load_collection_terminals(
            repo_root=repo_root,
            collection_root=collection_root,
            plan=cast(Any, plan),
            manifest=cast(Any, manifest),
        )
    except postprocess_v3.ResearchPostprocessV3Error as exc:
        raise ResearchPostprocessV4Error(f"collector-v3 terminal replay failed: {exc}") from exc


def _resolve_collection_artifacts(
    *,
    repo_root: Path,
    collection_root: Path,
    artifacts: dict[str, str],
    label: str,
) -> dict[str, str]:
    try:
        return postprocess_v3._resolve_collection_artifact_map(
            repo_root=repo_root,
            collection_root=collection_root,
            artifacts=artifacts,
            label=label,
        )
    except postprocess_v3.ResearchPostprocessV3Error as exc:
        raise ResearchPostprocessV4Error(str(exc)) from exc


def load_research_postprocess_v4(
    *,
    repo_root: Path,
    collection_root: Path,
    collection_config_path: Path,
    output_root: Path | None = None,
) -> LoadedResearchPostprocessV4:
    """Load and bind one completed collector-v3 tranche."""

    root = repo_root.resolve()
    collection = collection_root.resolve()
    try:
        collection.relative_to(root)
    except ValueError as exc:
        raise ResearchPostprocessV4Error("collector-v3 root must remain in the repository") from exc
    config_path = collection_config_path.resolve()
    try:
        config_path.relative_to(root)
    except ValueError as exc:
        raise ResearchPostprocessV4Error(
            "collector-v3 config must remain in the repository"
        ) from exc

    plan_path = collection / "plan.json"
    manifest_path = collection / "manifest.json"
    plan = postprocess_v1._load_canonical(
        plan_path,
        collection_v3.ResearchCollectionPlanV3,
    )
    manifest = postprocess_v1._load_canonical(
        manifest_path,
        collection_v3.ResearchCollectionManifestV3,
    )
    validate_collection_v3_denominator(plan, manifest)

    loaded_collection = collection_v3.load_research_collection_v3(
        config_path,
        repo_root=root,
    )
    if loaded_collection.plan != plan:
        raise ResearchPostprocessV4Error(
            "persisted collector-v3 plan differs from frozen config replay"
        )
    if (
        plan.collection_config_artifact != str(config_path.relative_to(root))
        or plan.collection_config_file_sha256 != hash_file(config_path)
        or plan.collection_config_hash != loaded_collection.config.config_hash
        or plan.tranche_id != loaded_collection.config.config.tranche_id
        or manifest.tranche_id != loaded_collection.config.config.tranche_id
    ):
        raise ResearchPostprocessV4Error("collector-v3 config or tranche binding differs")

    terminals, terminal_paths, terminal_hashes = _load_collection_terminals(
        repo_root=root,
        collection_root=collection,
        plan=plan,
        manifest=manifest,
    )
    session_hashes = _resolve_collection_artifacts(
        repo_root=root,
        collection_root=collection,
        artifacts=manifest.family_session_artifact_hashes,
        label="collector-v3 family session",
    )
    for terminal in terminals.values():
        if terminal.family_session_id is None:
            continue
        session = (
            collection
            / "families"
            / terminal.family_id
            / "sessions"
            / terminal.family_session_id.rsplit(":", 1)[-1]
            / "family_session_start.json"
        )
        artifact = str(session.resolve().relative_to(root))
        expected = terminal.artifact_hashes.get("family_session_start")
        if expected is None or session_hashes.get(artifact) != expected:
            raise ResearchPostprocessV4Error(
                f"collector-v3 family session differs: {terminal.invocation_id}"
            )

    config = loaded_collection.config.config
    problem_path = postprocess_v1._resolve_repo_artifact(
        root,
        config.problem_pool_records.artifact,
    )
    context_path = postprocess_v1._resolve_repo_artifact(
        root,
        config.context.artifact,
    )
    header_path = postprocess_v1._resolve_repo_artifact(
        root,
        config.import_header.artifact,
    )
    source_matrix_path = postprocess_v1._resolve_repo_artifact(
        root,
        config.source_matrix.artifact,
    )
    pool_manifest_path = postprocess_v1._resolve_repo_artifact(
        root,
        config.problem_pool_manifest.artifact,
    )
    pool_document = dict(collection_v3._load_canonical_mapping(pool_manifest_path))
    reference_theorems_path = _reference_artifact(
        repo_root=root,
        pool_document=pool_document,
        pool_dialect=config.problem_pool_contract.pool_dialect,
        field="reference_theorems",
    )
    reference_representations_path = _reference_artifact(
        repo_root=root,
        pool_document=pool_document,
        pool_dialect=config.problem_pool_contract.pool_dialect,
        field="reference_representations",
    )

    problems = {item.problem_record_id: item for item in loaded_collection.problems}
    if tuple(sorted(problems)) != plan.problem_record_ids or len(problems) != plan.problem_count:
        raise ResearchPostprocessV4Error("collector-v3 problems differ from plan")
    context = postprocess_v1._load_canonical(context_path, ContextRecord)
    header = header_path.read_text(encoding="utf-8")
    if (
        context != loaded_collection.context
        or context.header_text != header
        or context.header_hash != hash_file(header_path)
    ):
        raise ResearchPostprocessV4Error("collector-v3 context/header differs")

    reference_records = postprocess_v1._load_jsonl(
        reference_theorems_path,
        TheoremRecord,
    )
    representation_records = postprocess_v1._load_jsonl(
        reference_representations_path,
        RepresentationRecord,
    )
    references = {item.theorem_id: item for item in reference_records}
    representations = {item.theorem_id: item for item in representation_records}
    required_references = {
        theorem_id for problem in problems.values() for theorem_id in problem.reference_theorem_ids
    }
    if (
        set(references) != required_references
        or set(representations) != required_references
        or any(
            representations[theorem_id].theorem_id != theorem_id
            or representations[theorem_id].context_id != references[theorem_id].context_id
            for theorem_id in required_references
        )
    ):
        raise ResearchPostprocessV4Error("collector-v3 pool reference artifacts differ")

    denylist, registry_bindings = postprocess_v1._registry_bindings(root)
    if any(
        problem.denylist_registry_content_hash != denylist.registry_content_hash
        or problem.denylist_manifest_sha256 != denylist.manifest_sha256
        or problem.denylist_active_registry_sha256 != denylist.active_registry_sha256
        for problem in problems.values()
    ):
        raise ResearchPostprocessV4Error("collector-v3 problem registry binding differs")

    destination = (
        output_root.resolve()
        if output_root is not None
        else (collection / "postprocess_v4").resolve()
    )
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ResearchPostprocessV4Error(
            "postprocess-v4 output must remain in the repository"
        ) from exc
    if "postprocess_v4" not in destination.parts:
        raise ResearchPostprocessV4Error("postprocess-v4 output path must contain postprocess_v4")

    base = postprocess_v3._PostprocessBase(
        repo_root=root,
        collection_root=collection,
        output_root=destination,
        plan=cast(Any, plan),
        manifest=cast(Any, manifest),
        invocations=tuple(plan.invocations),
        collection_terminals=terminals,
        collection_terminal_paths=terminal_paths,
        problems=problems,
        context=context,
        import_header=header,
        references=references,
        reference_representations=representations,
        denylist=denylist,
    )

    raw_by_invocation: dict[str, dict[str, str]] = {}
    for invocation in base.invocations:
        terminal = terminals[invocation.invocation_id]
        if terminal.status is collection_v1.ResearchTerminalStatus.RAW_COLLECTED:
            _, _, _, hashes = postprocess_v1._verify_semantic_raw_lineage(
                cast(Any, base),
                invocation,
                terminal,
            )
            raw_by_invocation[invocation.invocation_id] = hashes
        else:
            raw_by_invocation[invocation.invocation_id] = {}

    parser_bindings: dict[str, postprocess_v1.PostprocessArtifactBinding] = {}
    for family_binding in plan.family_bindings:
        parser_path = postprocess_v1._resolve_repo_artifact(
            root,
            family_binding.parser_source_artifact,
        )
        parser_binding = _repo_binding(root, parser_path)
        if parser_binding.sha256 != family_binding.parser_source_sha256:
            raise ResearchPostprocessV4Error(
                f"primary parser hash differs: {family_binding.family_id}"
            )
        parser_bindings[family_binding.family_id] = parser_binding

    collector_path = postprocess_v1._resolve_repo_artifact(
        root,
        _COLLECTOR_V3_ARTIFACT,
    )
    if hash_file(collector_path) != plan.orchestration_adapter_sha256:
        raise ResearchPostprocessV4Error("collector-v3 implementation hash differs")
    shared_processor_path = postprocess_v1._resolve_repo_artifact(
        root,
        _SHARED_PROCESSOR_V3_ARTIFACT,
    )
    source_matrix = cast(
        collection_v3.ScalableResearchSourceMatrixV3,
        loaded_collection.source_matrix,
    )
    pool_source = source_matrix.source
    pool_kind = config.problem_pool_contract.manifest_artifact_kind
    binding = ResearchPostprocessV4InputBinding(
        tranche_id=plan.tranche_id,
        pool_dialect=config.problem_pool_contract.pool_dialect,
        pool_source=pool_source,
        pool_manifest_artifact_kind=pool_kind,
        collection_config=_repo_binding(root, config_path),
        collection_plan=_repo_binding(root, plan_path),
        collection_manifest=_repo_binding(root, manifest_path),
        collection_plan_id=plan.plan_id,
        collection_plan_hash=plan.plan_hash,
        collection_manifest_id=manifest.manifest_id,
        collection_terminal_artifacts=terminal_hashes,
        collection_family_session_artifacts=session_hashes,
        raw_collection_artifacts_by_invocation=dict(sorted(raw_by_invocation.items())),
        problem_pool_manifest=_repo_binding(root, pool_manifest_path),
        problem_pool_records=_repo_binding(root, problem_path),
        context=_repo_binding(root, context_path),
        import_header=_repo_binding(root, header_path),
        source_matrix=_repo_binding(root, source_matrix_path),
        reference_theorems=_repo_binding(root, reference_theorems_path),
        reference_representations=_repo_binding(
            root,
            reference_representations_path,
        ),
        active_registry_artifacts=registry_bindings,
        active_registry_content_hash=denylist.registry_content_hash,
        collector_implementation=_repo_binding(root, collector_path),
        primary_parser_implementations=dict(sorted(parser_bindings.items())),
        recovery_implementation=_repo_binding(
            root,
            Path(postprocess_v3.__file__).with_name("local_output_recovery.py"),
        ),
        shared_processing_implementation=_repo_binding(
            root,
            shared_processor_path,
        ),
        implementation=_repo_binding(root, Path(__file__)),
        problem_count=plan.problem_count,
        seed_count_by_family=dict(sorted(plan.seed_count_by_family.items())),
        expected_invocations=plan.expected_candidate_count,
        problem_record_ids=plan.problem_record_ids,
        invocation_ids=tuple(item.invocation_id for item in plan.invocations),
        family_ids=tuple(item.family_id for item in plan.family_bindings),
    )
    shared_loaded = postprocess_v3.LoadedResearchPostprocessV3(
        base=base,
        input_binding=binding.shared_v3_binding(),
    )
    return LoadedResearchPostprocessV4(
        base=base,
        input_binding=binding,
        shared_v3=shared_loaded,
    )


def _v4_terminal(
    loaded: LoadedResearchPostprocessV4,
    shared: postprocess_v3.ResearchPostprocessV3Terminal,
) -> ResearchPostprocessV4Terminal:
    payload: dict[str, object] = {
        "schema_version": 4,
        "record_kind": "lf021_research_postprocess_terminal_v4",
        "artifact_class": "research",
        "input_binding_hash": loaded.input_binding.binding_hash,
        "shared_processing_input_binding_hash": (
            loaded.input_binding.shared_processing_input_binding_hash
        ),
        "shared_processing_terminal_id": shared.terminal_id,
        "shared_processing_terminal_sha256": sha256_hex(
            postprocess_v1._canonical_record_bytes(shared)
        ),
        "tranche_id": loaded.input_binding.tranche_id,
        "pool_dialect": loaded.input_binding.pool_dialect,
        "pool_source": loaded.input_binding.pool_source,
        "invocation_id": shared.invocation_id,
        "family_id": shared.family_id,
        "problem_record_id": shared.problem_record_id,
        "seed": shared.seed,
        "status": shared.status.value,
        "terminal_stage": shared.terminal_stage.value,
        "record_time_basis": shared.record_time_basis.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "parser_executed": shared.parser_executed,
        "lean_validation_executed": shared.lean_validation_executed,
        "screening_executed": shared.screening_executed,
        "semantic_pool_admitted": shared.semantic_pool_admitted,
        "raw_lineage_hashes": shared.raw_lineage_hashes,
        "output_artifact_hashes": shared.output_artifact_hashes,
        "candidate_theorem_id": shared.candidate_theorem_id,
        "pair_ids": shared.pair_ids,
        "nl_lean_id": shared.nl_lean_id,
        "same_claim": None,
        "relation": None,
        "resolution_outcome": shared.resolution_outcome,
        "quality_tier": shared.quality_tier,
        "requires_adjudication": shared.requires_adjudication,
        "decision": shared.decision,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
        "shared_processing_terminal": shared.model_dump(mode="json"),
    }
    terminal_id = "research_postprocess_v4_terminal:" + hash_canonical(
        {"schema": "lf021_research_postprocess_terminal_v4", **payload}
    )
    return ResearchPostprocessV4Terminal.model_validate({"terminal_id": terminal_id, **payload})


def _family_payload(
    loaded: LoadedResearchPostprocessV4,
    *,
    family_id: str,
    selected: tuple[ResearchPostprocessV4Terminal, ...],
) -> dict[str, object]:
    seed_count = loaded.input_binding.seed_count_by_family[family_id]
    expected = loaded.input_binding.problem_count * seed_count
    if len(selected) != expected or any(item.family_id != family_id for item in selected):
        raise ResearchPostprocessV4Error(f"v4 family denominator differs: {family_id}")
    return {
        "schema_version": 4,
        "input_binding_hash": loaded.input_binding.binding_hash,
        "tranche_id": loaded.input_binding.tranche_id,
        "pool_dialect": loaded.input_binding.pool_dialect,
        "pool_source": loaded.input_binding.pool_source,
        "family_id": family_id,
        "problem_count": loaded.input_binding.problem_count,
        "seed_count": seed_count,
        "expected_invocations": expected,
        "terminal_invocations": expected,
        "status_counts": dict(sorted(Counter(item.status.value for item in selected).items())),
        "recovery_status_counts": dict(
            sorted(
                Counter(
                    item.shared_processing_terminal.recovery_status.value for item in selected
                ).items()
            )
        ),
        "collection_raw_count": sum(
            loaded.base.collection_terminals[item.invocation_id].status
            is collection_v1.ResearchTerminalStatus.RAW_COLLECTED
            for item in selected
        ),
        "parser_success_count": sum(
            item.shared_processing_terminal.recovery_status
            in {
                postprocess_v3.RecoveryStatus.NOT_NEEDED,
                postprocess_v3.RecoveryStatus.SUCCEEDED,
            }
            for item in selected
        ),
        "admitted_unresolved_count": sum(
            item.status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
            for item in selected
        ),
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }


def _output_directory(
    loaded: LoadedResearchPostprocessV4,
    invocation_id: str,
) -> Path:
    return loaded.base.output_root / "invocations" / invocation_id.rsplit(":", 1)[-1]


def _write_terminals_and_reports(
    loaded: LoadedResearchPostprocessV4,
    shared_by_id: dict[str, postprocess_v3.ResearchPostprocessV3Terminal],
) -> ResearchPostprocessV4Run:
    expected_ids = set(loaded.input_binding.invocation_ids)
    if set(shared_by_id) != expected_ids:
        missing = sorted(expected_ids - set(shared_by_id))
        extra = sorted(set(shared_by_id) - expected_ids)
        raise ResearchPostprocessV4Error(
            f"v4 postprocess denominator is incomplete: missing={missing}; extra={extra}"
        )
    terminals = tuple(_v4_terminal(loaded, shared_by_id[key]) for key in sorted(shared_by_id))
    terminal_artifacts: dict[str, str] = {}
    for terminal in terminals:
        path = _output_directory(loaded, terminal.invocation_id) / "processing_terminal.json"
        digest = postprocess_v1._write_immutable(
            path,
            postprocess_v1._canonical_record_bytes(terminal),
        )
        terminal_artifacts[str(path.resolve().relative_to(loaded.base.repo_root))] = digest

    reports: list[ResearchPostprocessV4FamilyReport] = []
    report_artifacts: dict[str, str] = {}
    for family_id in loaded.input_binding.family_ids:
        selected = tuple(item for item in terminals if item.family_id == family_id)
        family_payload = _family_payload(
            loaded,
            family_id=family_id,
            selected=selected,
        )
        report_id = "research_postprocess_v4_family:" + hash_canonical(
            {"schema": "lf021_research_postprocess_family_v4", **family_payload}
        )
        report = ResearchPostprocessV4FamilyReport.model_validate(
            {"report_id": report_id, **family_payload}
        )
        path = loaded.base.output_root / "families" / f"{family_id}.json"
        digest = postprocess_v1._write_immutable(
            path,
            postprocess_v1._canonical_record_bytes(report),
        )
        report_artifacts[str(path.resolve().relative_to(loaded.base.repo_root))] = digest
        reports.append(report)

    admitted = tuple(
        item
        for item in terminals
        if item.status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    )
    payload: dict[str, object] = {
        "schema_version": 4,
        "input_binding": loaded.input_binding.model_dump(mode="json"),
        "input_binding_hash": loaded.input_binding.binding_hash,
        "shared_processing_input_binding_hash": (
            loaded.input_binding.shared_processing_input_binding_hash
        ),
        "tranche_id": loaded.input_binding.tranche_id,
        "pool_dialect": loaded.input_binding.pool_dialect,
        "pool_source": loaded.input_binding.pool_source,
        "problem_count": loaded.input_binding.problem_count,
        "family_count": loaded.input_binding.family_count,
        "seed_count_by_family": loaded.input_binding.seed_count_by_family,
        "expected_invocations": loaded.input_binding.expected_invocations,
        "terminal_invocations": len(terminals),
        "status_counts": dict(sorted(Counter(item.status.value for item in terminals).items())),
        "recovery_status_counts": dict(
            sorted(
                Counter(
                    item.shared_processing_terminal.recovery_status.value for item in terminals
                ).items()
            )
        ),
        "terminal_artifacts": dict(sorted(terminal_artifacts.items())),
        "family_report_artifacts": dict(sorted(report_artifacts.items())),
        "admitted_pair_count": sum(len(item.pair_ids) for item in admitted),
        "admitted_nl_lean_count": len(admitted),
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    manifest_id = "research_postprocess_v4_manifest:" + hash_canonical(
        {"schema": "lf021_research_postprocess_manifest_v4", **payload}
    )
    manifest = ResearchPostprocessV4Manifest.model_validate({"manifest_id": manifest_id, **payload})
    manifest_path = loaded.base.output_root / "manifest.json"
    postprocess_v1._write_immutable(
        manifest_path,
        postprocess_v1._canonical_record_bytes(manifest),
    )
    return ResearchPostprocessV4Run(
        output_root=loaded.base.output_root,
        manifest_path=manifest_path,
        manifest=manifest,
        terminals=terminals,
        family_reports=tuple(reports),
    )


def run_research_postprocess_v4(
    loaded: LoadedResearchPostprocessV4,
    *,
    backend: LeanInteractBackend,
) -> ResearchPostprocessV4Run:
    """Run the exact v3 processing engine over every collector-v3 invocation."""

    for binding in (
        loaded.input_binding.implementation,
        loaded.input_binding.shared_processing_implementation,
        loaded.input_binding.recovery_implementation,
        loaded.input_binding.collector_implementation,
        *loaded.input_binding.primary_parser_implementations.values(),
    ):
        path = postprocess_v1._resolve_bound_artifact(
            loaded.base.repo_root,
            binding,
        )
        if hash_file(path) != binding.sha256:
            raise ResearchPostprocessV4Error(f"v4 executable binding changed: {binding.artifact}")
    try:
        prepared, shared_terminals = postprocess_v3._prepare_candidates(
            loaded.shared_v3,
            backend=backend,
        )
        postprocess_v3._screen_and_admit(
            loaded.shared_v3,
            prepared=prepared,
            terminals=shared_terminals,
        )
    except postprocess_v3.ResearchPostprocessV3Error as exc:
        raise ResearchPostprocessV4Error(f"shared v3 processing failed: {exc}") from exc
    return _write_terminals_and_reports(loaded, shared_terminals)


def _verify_binding(
    repo_root: Path,
    binding: postprocess_v1.PostprocessArtifactBinding,
) -> None:
    path = postprocess_v1._resolve_bound_artifact(repo_root, binding)
    if hash_file(path) != binding.sha256:
        raise ResearchPostprocessV4Error(f"v4 bound artifact hash mismatch: {binding.artifact}")


def verify_research_postprocess_v4(
    loaded: LoadedResearchPostprocessV4,
) -> ResearchPostprocessV4Manifest:
    """Replay-verify a v4 bundle without Lean execution or writes."""

    base = loaded.base
    bindings = loaded.input_binding
    for binding in (
        bindings.collection_config,
        bindings.collection_plan,
        bindings.collection_manifest,
        bindings.problem_pool_manifest,
        bindings.problem_pool_records,
        bindings.context,
        bindings.import_header,
        bindings.source_matrix,
        bindings.reference_theorems,
        bindings.reference_representations,
        bindings.collector_implementation,
        bindings.recovery_implementation,
        bindings.shared_processing_implementation,
        bindings.implementation,
        *bindings.primary_parser_implementations.values(),
        *bindings.active_registry_artifacts.values(),
    ):
        _verify_binding(base.repo_root, binding)
    for artifact_map in (
        bindings.collection_terminal_artifacts,
        bindings.collection_family_session_artifacts,
    ):
        for artifact, expected in artifact_map.items():
            path = postprocess_v1._resolve_repo_artifact(
                base.repo_root,
                artifact,
            )
            if hash_file(path) != expected:
                raise ResearchPostprocessV4Error(f"v4 collector input hash mismatch: {artifact}")
    for invocation_id, artifacts in bindings.raw_collection_artifacts_by_invocation.items():
        for artifact, expected in artifacts.items():
            path = postprocess_v1._resolve_repo_artifact(
                base.repo_root,
                artifact,
            )
            if hash_file(path) != expected:
                raise ResearchPostprocessV4Error(
                    f"v4 raw input hash mismatch: {invocation_id}: {artifact}"
                )

    manifest = postprocess_v1._load_canonical(
        base.output_root / "manifest.json",
        ResearchPostprocessV4Manifest,
    )
    if manifest.input_binding != bindings:
        raise ResearchPostprocessV4Error("persisted v4 input binding has drifted")
    invocation_by_id = {item.invocation_id: item for item in base.invocations}
    terminals: list[ResearchPostprocessV4Terminal] = []
    for artifact, expected in manifest.terminal_artifacts.items():
        path = postprocess_v1._resolve_repo_artifact(
            base.repo_root,
            artifact,
        )
        if hash_file(path) != expected:
            raise ResearchPostprocessV4Error(f"v4 terminal hash mismatch: {artifact}")
        terminal = postprocess_v1._load_canonical(
            path,
            ResearchPostprocessV4Terminal,
        )
        expected_path = (
            _output_directory(loaded, terminal.invocation_id) / "processing_terminal.json"
        ).resolve()
        if path != expected_path:
            raise ResearchPostprocessV4Error(f"v4 terminal stored at unexpected path: {artifact}")
        invocation = invocation_by_id.get(terminal.invocation_id)
        collection = base.collection_terminals.get(terminal.invocation_id)
        collection_path = base.collection_terminal_paths.get(terminal.invocation_id)
        shared = terminal.shared_processing_terminal
        if invocation is None or collection is None or collection_path is None:
            raise ResearchPostprocessV4Error("v4 terminal lacks frozen collector lineage")
        if (
            terminal.input_binding_hash != bindings.binding_hash
            or terminal.tranche_id != bindings.tranche_id
            or terminal.pool_dialect != bindings.pool_dialect
            or terminal.pool_source != bindings.pool_source
            or shared.input_binding_hash != bindings.shared_processing_input_binding_hash
            or shared.invocation_payload_hash != hash_canonical(invocation.model_dump(mode="json"))
            or shared.collection_terminal_id != collection.terminal_id
            or shared.collection_terminal_sha256 != hash_file(collection_path)
            or shared.primary_parser_id != invocation.parser_id
            or shared.primary_parser_source_sha256 != invocation.parser_source_sha256
            or terminal.raw_lineage_hashes
            != bindings.raw_collection_artifacts_by_invocation[terminal.invocation_id]
        ):
            raise ResearchPostprocessV4Error(
                f"v4 terminal lineage differs: {terminal.invocation_id}"
            )
        bound = {
            **terminal.raw_lineage_hashes,
            **terminal.output_artifact_hashes,
        }
        if len(bound) != (len(terminal.raw_lineage_hashes) + len(terminal.output_artifact_hashes)):
            raise ResearchPostprocessV4Error(
                f"v4 raw/output path collision: {terminal.invocation_id}"
            )
        for bound_artifact, bound_hash in bound.items():
            resolved = postprocess_v1._resolve_repo_artifact(
                base.repo_root,
                bound_artifact,
            )
            if hash_file(resolved) != bound_hash:
                raise ResearchPostprocessV4Error(f"v4 bound output hash mismatch: {bound_artifact}")
        parsed_candidates = [
            candidate_path
            for candidate_path in terminal.output_artifact_hashes
            if candidate_path.endswith("/parsed_candidate.json")
        ]
        parser_succeeded = shared.recovery_status in {
            postprocess_v3.RecoveryStatus.NOT_NEEDED,
            postprocess_v3.RecoveryStatus.SUCCEEDED,
        }
        if parser_succeeded:
            if len(parsed_candidates) != 1:
                raise ResearchPostprocessV4Error(
                    f"v4 parsed-candidate provenance missing: {terminal.invocation_id}"
                )
            parsed = postprocess_v1._load_canonical(
                postprocess_v1._resolve_repo_artifact(
                    base.repo_root,
                    parsed_candidates[0],
                ),
                postprocess_v3._ParsedCandidateRecordV3,
            )
            if (
                parsed.invocation_id != terminal.invocation_id
                or parsed.primary_parser_id != shared.primary_parser_id
                or parsed.primary_parser_source_sha256 != shared.primary_parser_source_sha256
                or parsed.actual_parser_id != shared.actual_parser_id
                or parsed.actual_parser_source_sha256 != shared.actual_parser_source_sha256
                or parsed.primary_failure_code != shared.primary_failure_code
                or parsed.recovery_status != shared.recovery_status.value
            ):
                raise ResearchPostprocessV4Error(
                    f"v4 parser provenance differs: {terminal.invocation_id}"
                )
        elif parsed_candidates:
            raise ResearchPostprocessV4Error(
                f"failed v4 parse persisted a candidate: {terminal.invocation_id}"
            )
        terminals.append(terminal)
    if tuple(sorted(item.invocation_id for item in terminals)) != (bindings.invocation_ids):
        raise ResearchPostprocessV4Error("persisted v4 terminal denominator differs")

    status_counts = dict(sorted(Counter(item.status.value for item in terminals).items()))
    recovery_counts = dict(
        sorted(
            Counter(
                item.shared_processing_terminal.recovery_status.value for item in terminals
            ).items()
        )
    )
    admitted = tuple(
        item
        for item in terminals
        if item.status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    )
    if (
        status_counts != manifest.status_counts
        or recovery_counts != manifest.recovery_status_counts
        or sum(len(item.pair_ids) for item in admitted) != manifest.admitted_pair_count
        or len(admitted) != manifest.admitted_nl_lean_count
    ):
        raise ResearchPostprocessV4Error("v4 manifest accounting differs")

    reports: dict[str, ResearchPostprocessV4FamilyReport] = {}
    for artifact, expected in manifest.family_report_artifacts.items():
        path = postprocess_v1._resolve_repo_artifact(
            base.repo_root,
            artifact,
        )
        if hash_file(path) != expected:
            raise ResearchPostprocessV4Error(f"v4 family report hash mismatch: {artifact}")
        report = postprocess_v1._load_canonical(
            path,
            ResearchPostprocessV4FamilyReport,
        )
        if report.family_id in reports:
            raise ResearchPostprocessV4Error(f"duplicate v4 family report: {report.family_id}")
        expected_path = (base.output_root / "families" / f"{report.family_id}.json").resolve()
        if path != expected_path:
            raise ResearchPostprocessV4Error(
                f"v4 family report stored at unexpected path: {artifact}"
            )
        reports[report.family_id] = report
    if set(reports) != set(bindings.family_ids):
        raise ResearchPostprocessV4Error("v4 family report denominator differs")
    for family_id, report in reports.items():
        expected_payload = _family_payload(
            loaded,
            family_id=family_id,
            selected=tuple(item for item in terminals if item.family_id == family_id),
        )
        if (
            report.model_dump(
                mode="json",
                exclude={"report_id"},
            )
            != expected_payload
        ):
            raise ResearchPostprocessV4Error(f"v4 family report accounting differs: {family_id}")
    return manifest


__all__ = [
    "LoadedResearchPostprocessV4",
    "ResearchPostprocessV4Error",
    "ResearchPostprocessV4FamilyReport",
    "ResearchPostprocessV4InputBinding",
    "ResearchPostprocessV4Manifest",
    "ResearchPostprocessV4Run",
    "ResearchPostprocessV4Terminal",
    "load_research_postprocess_v4",
    "run_research_postprocess_v4",
    "validate_collection_v3_denominator",
    "verify_research_postprocess_v4",
]
