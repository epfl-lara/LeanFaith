"""Generic, resumable LF-021 collection over either closed public-pool dialect.

Collector v5 is the typed successor to the immutable, hash-bound collector-v4
snapshot.  Its orchestration is intentionally archived as a separate module:
changing v4 would invalidate active and historical manifests.  V5 preserves
the audited v4 runtime semantics while correcting its static type boundary and
binding both this module and its CLI in every newly derived config.  It does
not modify or reinterpret collector v1, v2, v3, or v4 artifacts.

The loader accepts exactly two closed pool dialects:

* the 40-record Algebra operational pool with its exact overlap-v2 records;
* the 20-record cross-domain operational pool with its exact overlap-v3
  records.

Pool validation is delegated to the already-audited closed-dialect validators.
Qualification and overlap evidence are replayed at their original versions.
Plans, preflight reports, manifests, output roots, and orchestration identity
are separately versioned v5.  Raw collection performs no parsing, semantic
labeling, supervision admission, or Gate closure.
"""

from __future__ import annotations

import datetime
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.generation import research_collection as _v1
from leanfaith.generation import research_collection_v2 as _v2
from leanfaith.generation import research_collection_v3 as _v3
from leanfaith.generation.local_qualification import (
    LocalQualificationConfig,
    load_local_qualification_config,
)
from leanfaith.generation.public_research_pool import HeldoutResearchFamily
from leanfaith.generation.research_overlap_v2 import ResearchFamilyOverlapRecordV2
from leanfaith.generation.research_overlap_v3 import ResearchFamilyOverlapRecordV3
from leanfaith.generation.tranche_expansion import (
    DecisionAction,
    ExpansionDecision,
    TrancheExpansionError,
    TrancheExpansionPolicy,
    evaluate_tranche_expansion,
    load_tranche_expansion_policy,
)
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

_HEX64 = r"^[0-9a-f]{64}$"
_PLAN_ID = r"^research_collection_plan_v5:[0-9a-f]{64}$"
_MANIFEST_ID = r"^research_collection_manifest_v5:[0-9a-f]{64}$"
_TRANCHE = r"^[a-z][a-z0-9_]*$"
_V5_ORCHESTRATION_ARTIFACT = "src/leanfaith/generation/research_collection_v5.py"
_V5_ORCHESTRATION_CLI = "scripts/27_collect_research_tranche_v5.py"

PoolDialect = Literal[
    "gate3_algebra_operational_v1",
    "cross_domain_operational_v1",
]
OverlapSchema = Literal[
    "lf021_research_family_overlap_v2",
    "lf021_research_family_overlap_v3",
]
LegacySourceMatrix = _v2.ScalableResearchSourceMatrixV2 | _v3.ScalableResearchSourceMatrixV3
OverlapRecord = ResearchFamilyOverlapRecordV2 | ResearchFamilyOverlapRecordV3


class ResearchCollectionV5Error(_v1.ResearchCollectionError):
    """The generic collection-v5 contract is invalid or incomplete."""


class ResearchCollectionV5ExecutionBlocked(
    ResearchCollectionV5Error,
    _v1.ResearchCollectionExecutionBlocked,
):
    """Execution was requested before every v5 prerequisite passed."""


class ResearchCollectionV5ArtifactConflict(
    ResearchCollectionV5Error,
    _v1.ResearchCollectionArtifactConflict,
):
    """A supposedly immutable v5 artifact contains different bytes."""


class ResearchCollectionV5Config(StrictModel):
    """Authorization for one preregistered closed-pool LF-021 tranche."""

    schema_version: Literal[5] = 5
    config_id: Literal["lf021_local_research_collection_v5"]
    tranche_id: str = Field(pattern=_TRANCHE)
    frozen_at: datetime.datetime
    artifact_class: Literal["research"] = "research"
    collection_scope: Literal["preregistered_closed_pool_three_family_tranche_v5"] = (
        "preregistered_closed_pool_three_family_tranche_v5"
    )
    shared_execution_record_schema: Literal["lf021_research_execution_records_v1"] = (
        "lf021_research_execution_records_v1"
    )
    status: Literal["blocked_pending_family_activation", "ready"]
    execution_enabled: bool
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    expansion_decision: _v1.ResearchArtifactBinding
    expansion_policy: _v1.ResearchArtifactBinding
    problem_pool_contract: _v3.ScalableProblemPoolContract
    problem_pool_records: _v1.ResearchArtifactBinding
    problem_pool_manifest: _v1.ResearchArtifactBinding
    context: _v1.ResearchArtifactBinding
    import_header: _v1.ResearchArtifactBinding
    source_matrix: _v1.ResearchArtifactBinding
    orchestration_cli: _v1.ResearchArtifactBinding
    runtime: _v1.ResearchRuntimeBinding
    families: tuple[_v1.ResearchCollectionFamily, ...]
    retry: _v1.ResearchRetryConfig
    outputs: _v1.ResearchCollectionOutputs
    rules: tuple[str, ...] = Field(min_length=1)

    @property
    def required_overlap_schema(self) -> OverlapSchema:
        if self.problem_pool_contract.pool_dialect == "gate3_algebra_operational_v1":
            return "lf021_research_family_overlap_v2"
        return "lf021_research_family_overlap_v3"

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        from leanfaith.schemas.manifest import require_utc

        require_utc(self.frozen_at)
        if len(self.families) != 3:
            raise ValueError("collection v5 requires exactly three model families")
        family_ids = [family.family_id for family in self.families]
        provider_slots = [family.provider_slot for family in self.families]
        if family_ids != sorted(set(family_ids)):
            raise ValueError("collection families must be sorted and unique")
        if len(provider_slots) != len(set(provider_slots)):
            raise ValueError("collection provider slots must be unique")
        if any(
            tuple(family.seeds) != tuple(sorted(set(family.seeds)))
            or any(seed < 0 for seed in family.seeds)
            or not family.seeds
            for family in self.families
        ):
            raise ValueError("each v5 family requires sorted unique nonnegative seeds")
        ready = all(family.activation.status == "ready" for family in self.families)
        if self.status == "ready":
            if not self.execution_enabled or not ready:
                raise ValueError("ready v5 collection requires all family activations")
        elif self.execution_enabled or ready:
            raise ValueError("blocked v5 collection cannot enable all execution")
        if self.runtime.orchestration_adapter.artifact != _V5_ORCHESTRATION_ARTIFACT:
            raise ValueError("collection v5 must bind the v5 orchestration module")
        if self.orchestration_cli.artifact != _V5_ORCHESTRATION_CLI:
            raise ValueError("collection v5 must bind the v5 orchestration CLI")
        pool_slug = {
            "gate3_algebra_operational_v1": "gate3_docstrings_operational_v1",
            "cross_domain_operational_v1": "cross_domain_docstrings_operational_v1",
        }[self.problem_pool_contract.pool_dialect]
        expected_root = f"data/raw/real_outputs/{pool_slug}/v5/{self.tranche_id}/local_collection"
        expected_preflight = (
            "reports/generation/"
            f"lf021_local_research_collection_preflight_{self.tranche_id}_v5.json"
        )
        if self.outputs.root != expected_root:
            raise ValueError("collection v5 output root differs from the exact tranche root")
        if self.outputs.preflight_report != expected_preflight:
            raise ValueError("collection v5 preflight report differs from the exact tranche path")
        return self


class ScalableResearchSourceMatrixV5(StrictModel):
    """Tranche-specific identity matrix binding an immutable v2/v3 matrix."""

    schema_version: Literal[5] = 5
    matrix_id: str = Field(pattern=r"^local_research_source_matrix_v5:[0-9a-f]{64}$")
    tranche_id: str = Field(pattern=_TRANCHE)
    status: Literal["pool_compatible_activation_external_to_matrix"]
    pool_dialect: PoolDialect
    source: Literal[
        "mathlib_gate3_docstrings_operational_v1",
        "mathlib_cross_domain_docstrings_operational_v1",
    ]
    problem_count: int = Field(ge=1)
    problem_pool_manifest_sha256: str = Field(pattern=_HEX64)
    base_source_matrix: _v1.ResearchArtifactBinding
    private_source_content: Literal[False] = False
    external_transmission_required: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_authorized: Literal[False] = False
    collection_authorization_source: Literal[
        "v5_config_and_original_pool_specific_overlap_evidence"
    ]
    families: tuple[_v3.ScalableResearchFamily, ...]
    heldout: HeldoutResearchFamily
    rules: tuple[str, ...] = Field(min_length=1)

    def id_payload(self) -> dict[str, object]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key != "matrix_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected_source = {
            "gate3_algebra_operational_v1": "mathlib_gate3_docstrings_operational_v1",
            "cross_domain_operational_v1": ("mathlib_cross_domain_docstrings_operational_v1"),
        }[self.pool_dialect]
        if self.source != expected_source:
            raise ValueError("v5 source matrix dialect and source disagree")
        ids = tuple(item.family_id for item in self.families)
        models = tuple(item.model for item in self.families)
        if len(ids) != 3 or ids != tuple(sorted(set(ids))):
            raise ValueError("v5 source matrix requires three sorted families")
        if len(models) != len(set(models)):
            raise ValueError("v5 source-matrix model IDs must be unique")
        expected = "local_research_source_matrix_v5:" + hash_canonical(
            {"schema": "lf021_research_source_matrix_v5", **self.id_payload()}
        )
        if self.matrix_id != expected:
            raise ValueError("v5 source matrix ID does not match payload")
        return self


class ResearchCollectionPlanV5(StrictModel):
    """Deterministic plan for one arbitrary preregistered v5 tranche."""

    schema_version: Literal[5] = 5
    plan_id: str = Field(pattern=_PLAN_ID)
    tranche_id: str = Field(pattern=_TRANCHE)
    pool_dialect: PoolDialect
    overlap_schema: OverlapSchema
    expansion_decision_id: str
    expansion_decision_sha256: str = Field(pattern=_HEX64)
    expansion_policy_id: str
    expansion_policy_sha256: str = Field(pattern=_HEX64)
    collection_config_artifact: str
    collection_config_file_sha256: str = Field(pattern=_HEX64)
    collection_config_hash: str = Field(pattern=_HEX64)
    problem_pool_records_sha256: str = Field(pattern=_HEX64)
    problem_pool_manifest_sha256: str = Field(pattern=_HEX64)
    context_sha256: str = Field(pattern=_HEX64)
    import_header_sha256: str = Field(pattern=_HEX64)
    source_matrix_sha256: str = Field(pattern=_HEX64)
    runtime_adapter_sha256: str = Field(pattern=_HEX64)
    environment_lock_sha256: str = Field(pattern=_HEX64)
    orchestration_adapter_sha256: str = Field(pattern=_HEX64)
    orchestration_cli_sha256: str = Field(pattern=_HEX64)
    runtime_hash: str = Field(pattern=_HEX64)
    shared_execution_record_schema: Literal["lf021_research_execution_records_v1"] = (
        "lf021_research_execution_records_v1"
    )
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    seed_count_by_family: dict[str, int]
    expected_candidate_count: int = Field(ge=1)
    problem_record_ids: tuple[str, ...] = Field(min_length=1)
    family_bindings: tuple[_v1.ResearchFamilyBinding, ...]
    invocations: tuple[_v1.ResearchCollectionInvocation, ...]
    actual_collection_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @property
    def plan_hash(self) -> str:
        return hash_canonical(self.model_dump(mode="json"))

    def id_payload(self) -> dict[str, object]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key != "plan_id"
        }

    @model_validator(mode="after")
    def _identity_and_counts(self) -> Self:
        required = {
            "gate3_algebra_operational_v1": "lf021_research_family_overlap_v2",
            "cross_domain_operational_v1": "lf021_research_family_overlap_v3",
        }[self.pool_dialect]
        if self.overlap_schema != required:
            raise ValueError("v5 overlap schema differs from pool dialect")
        if len(self.problem_record_ids) != self.problem_count or self.problem_record_ids != tuple(
            sorted(set(self.problem_record_ids))
        ):
            raise ValueError("v5 plan problem IDs do not reconcile")
        family_ids = tuple(binding.family_id for binding in self.family_bindings)
        if len(family_ids) != self.family_count or family_ids != tuple(sorted(set(family_ids))):
            raise ValueError("v5 plan requires three sorted unique family bindings")
        if list(self.seed_count_by_family) != sorted(self.seed_count_by_family):
            raise ValueError("v5 seed counts must be sorted")
        if set(self.seed_count_by_family) != set(family_ids):
            raise ValueError("v5 seed-count families differ from family bindings")
        if any(count < 1 for count in self.seed_count_by_family.values()):
            raise ValueError("every v5 family must have at least one seed")
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if self.expected_candidate_count != expected or len(self.invocations) != expected:
            raise ValueError("v5 invocation denominator does not reconcile")
        invocation_ids = tuple(item.invocation_id for item in self.invocations)
        if invocation_ids != tuple(sorted(set(invocation_ids))):
            raise ValueError("v5 plan invocations must be sorted and unique")
        actual = Counter(item.family_id for item in self.invocations)
        for family_id, seed_count in self.seed_count_by_family.items():
            if actual[family_id] != self.problem_count * seed_count:
                raise ValueError("v5 family invocation count does not reconcile")
        if {item.problem_record_id for item in self.invocations} != set(self.problem_record_ids):
            raise ValueError("v5 invocation problems differ from frozen pool")
        expected_id = "research_collection_plan_v5:" + hash_canonical(
            {"schema": "lf021_research_collection_plan_v5", **self.id_payload()}
        )
        if self.plan_id != expected_id:
            raise ValueError("plan_id does not match frozen v5 plan payload")
        return self


class ResearchCollectionPreflightReportV5(StrictModel):
    """Model-free replay report for one generic v5 tranche."""

    schema_version: Literal[5] = 5
    report_kind: Literal["lf021_local_research_collection_preflight_v5"]
    passed: Literal[True] = True
    execution_ready: bool
    actual_collection_performed: Literal[False] = False
    gpu_model_execution_performed: Literal[False] = False
    provider_requests_created: Literal[0] = 0
    terminal_candidates_created: Literal[0] = 0
    semantic_labels_created: Literal[False] = False
    counts_as_smoke_qualification: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    plan_id: str = Field(pattern=_PLAN_ID)
    plan_hash: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    pool_dialect: PoolDialect
    overlap_schema: OverlapSchema
    expansion_decision_id: str
    expansion_decision_sha256: str = Field(pattern=_HEX64)
    expansion_policy_id: str
    expansion_policy_sha256: str = Field(pattern=_HEX64)
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    seed_count_by_family: dict[str, int]
    planned_candidate_count: int = Field(ge=1)
    family_binding_hashes: dict[str, str]
    invocation_ids: tuple[str, ...]
    checks: dict[str, bool]
    blocking_prerequisites: tuple[str, ...]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if not self.checks or not all(self.checks.values()):
            raise ValueError("collection v5 preflight requires every model-free check")
        if self.execution_ready == bool(self.blocking_prerequisites):
            raise ValueError("execution readiness and blockers disagree")
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if (
            self.planned_candidate_count != expected
            or len(self.invocation_ids) != expected
            or len(self.family_binding_hashes) != self.family_count
        ):
            raise ValueError("v5 preflight denominator does not reconcile")
        return self


class ResearchCollectionManifestV5(StrictModel):
    """Complete terminal accounting for one collector-v5 plan."""

    schema_version: Literal[5] = 5
    manifest_id: str = Field(pattern=_MANIFEST_ID)
    plan_id: str = Field(pattern=_PLAN_ID)
    plan_hash: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    pool_dialect: PoolDialect
    overlap_schema: OverlapSchema
    expansion_decision_id: str
    expansion_decision_sha256: str = Field(pattern=_HEX64)
    expansion_policy_id: str
    expansion_policy_sha256: str = Field(pattern=_HEX64)
    shared_execution_record_schema: Literal["lf021_research_execution_records_v1"] = (
        "lf021_research_execution_records_v1"
    )
    actual_collection_performed: Literal[True] = True
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    seed_count_by_family: dict[str, int]
    expected_candidate_count: int = Field(ge=1)
    terminal_candidate_count: int = Field(ge=1)
    status_counts: dict[str, int]
    successful_family_count: int = Field(ge=0, le=3)
    terminal_artifact_hashes: dict[str, str]
    family_session_artifact_hashes: dict[str, str]
    semantic_labels_created: Literal[False] = False
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
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if (
            self.expected_candidate_count != expected
            or self.terminal_candidate_count != expected
            or sum(self.status_counts.values()) != expected
            or len(self.terminal_artifact_hashes) != expected
        ):
            raise ValueError("v5 manifest accounting does not reconcile")
        for field in ("terminal_artifact_hashes", "family_session_artifact_hashes"):
            mapping = getattr(self, field)
            if list(mapping) != sorted(mapping):
                raise ValueError(f"{field} must be sorted")
        expected_id = "research_collection_manifest_v5:" + hash_canonical(
            {"schema": "lf021_research_collection_manifest_v5", **self.id_payload()}
        )
        if self.manifest_id != expected_id:
            raise ValueError("manifest_id does not match v5 manifest payload")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedResearchFamilyActivationV5:
    """Original-version qualification and exact pool-overlap evidence."""

    qualification: _v1.VerifiedResearchFamilyActivation
    overlap_record: OverlapRecord
    overlap_schema: OverlapSchema


@dataclass(frozen=True, slots=True)
class LoadedResearchCollectionV5:
    config: LoadedConfig[ResearchCollectionV5Config]
    problems: tuple[ProblemPoolRecord, ...]
    context: ContextRecord
    source_matrix: ScalableResearchSourceMatrixV5
    pool_evidence: _v3.ScalableProblemPoolEvidence
    qualifications: Mapping[str, LoadedConfig[LocalQualificationConfig]]
    activation_evidence: Mapping[str, VerifiedResearchFamilyActivationV5]
    plan: ResearchCollectionPlanV5
    preflight: ResearchCollectionPreflightReportV5


@dataclass(frozen=True, slots=True)
class ResearchCollectionRunV5:
    output_directory: Path
    plan_path: Path
    manifest_path: Path
    manifest: ResearchCollectionManifestV5
    terminals: tuple[_v1.ResearchCollectionTerminal, ...]


class ResearchInvocationExecutorV5(Protocol):
    """Structural executor contract shared with the audited v1 local executor."""

    def begin_family(
        self,
        *,
        family: _v1.ResearchFamilyBinding,
        qualification: LoadedConfig[LocalQualificationConfig],
        runtime: _v1.ResearchRuntimeBinding,
        invocations: tuple[_v1.ResearchCollectionInvocation, ...],
        family_directory: Path,
    ) -> None: ...

    def execute(
        self,
        *,
        invocation: _v1.ResearchCollectionInvocation,
        problem: ProblemPoolRecord,
        qualification: LoadedConfig[LocalQualificationConfig],
        invocation_directory: Path,
        artifact_root: Path,
    ) -> _v1.ResearchCollectionTerminal: ...

    def end_family(
        self,
        *,
        family: _v1.ResearchFamilyBinding,
        completed_invocation_ids: tuple[str, ...],
        family_directory: Path,
    ) -> None: ...


def _load_source_matrix(
    path: Path,
) -> ScalableResearchSourceMatrixV5:
    raw = load_yaml_mapping(path)
    return ScalableResearchSourceMatrixV5.model_validate(raw)


def _load_legacy_source_matrix(
    path: Path,
    *,
    dialect: PoolDialect,
) -> LegacySourceMatrix:
    raw = load_yaml_mapping(path)
    if dialect == "gate3_algebra_operational_v1":
        return _v2.ScalableResearchSourceMatrixV2.model_validate(raw)
    return _v3.ScalableResearchSourceMatrixV3.model_validate(raw)


def _verify_activation_v5(
    *,
    family: _v1.ResearchCollectionFamily,
    qualification: LoadedConfig[LocalQualificationConfig],
    config: ResearchCollectionV5Config,
    pool_evidence: _v3.ScalableProblemPoolEvidence,
    problems: tuple[ProblemPoolRecord, ...],
    repo_root: Path,
) -> VerifiedResearchFamilyActivationV5:
    dialect = config.problem_pool_contract.pool_dialect
    try:
        if dialect == "gate3_algebra_operational_v1":
            evidence = _v2._verify_activation_v2(
                family=family,
                qualification=qualification,
                config=cast(Any, config),
                pool_evidence=cast(Any, pool_evidence),
                problems=problems,
                repo_root=repo_root,
            )
            return VerifiedResearchFamilyActivationV5(
                qualification=evidence.qualification,
                overlap_record=evidence.overlap_record,
                overlap_schema="lf021_research_family_overlap_v2",
            )
        evidence_v3 = _v3._verify_activation_v3(
            family=family,
            qualification=qualification,
            config=cast(Any, config),
            pool_evidence=pool_evidence,
            problems=problems,
            repo_root=repo_root,
        )
        return VerifiedResearchFamilyActivationV5(
            qualification=evidence_v3.qualification,
            overlap_record=evidence_v3.overlap_record,
            overlap_schema="lf021_research_family_overlap_v3",
        )
    except (_v2.ResearchCollectionV2Error, _v3.ResearchCollectionV3Error) as exc:
        raise ResearchCollectionV5Error(
            f"v5 activation replay failed for {family.family_id}"
        ) from exc


def _load_expansion_authorization(
    *,
    repo_root: Path,
    decision_binding: _v1.ResearchArtifactBinding,
    policy_binding: _v1.ResearchArtifactBinding,
) -> tuple[ExpansionDecision, TrancheExpansionPolicy]:
    """Replay the exact label-blind decision, policy, and observed prefix."""

    try:
        decision_path = _v1._resolve_binding(repo_root, decision_binding)
        policy_path = _v1._resolve_binding(repo_root, policy_binding)
        decision = ExpansionDecision.model_validate_json(decision_path.read_text(encoding="utf-8"))
    except (
        _v1.ResearchCollectionError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise ResearchCollectionV5Error("invalid expansion decision artifact") from exc
    loaded_policy = load_tranche_expansion_policy(policy_path)
    policy = loaded_policy.config
    if (
        decision.action is not DecisionAction.COLLECT_NEXT_TRANCHE
        or decision.next_tranche is None
        or decision.policy_id != policy.policy_id
        or decision.policy_artifact.artifact != str(policy_path.relative_to(repo_root))
        or decision.policy_artifact.sha256 != policy_binding.sha256
    ):
        raise ResearchCollectionV5Error(
            "expansion decision does not authorize a tranche under bound policy"
        )
    if (
        decision.next_tranche.order >= len(policy.tranches)
        or decision.next_tranche != policy.tranches[decision.next_tranche.order]
    ):
        raise ResearchCollectionV5Error(
            "expansion decision next tranche differs from the bound policy"
        )
    expected_prefix = tuple(
        item.tranche_id for item in policy.tranches[: decision.next_tranche.order]
    )
    if tuple(item.tranche_id for item in decision.observations) != expected_prefix:
        raise ResearchCollectionV5Error("expansion decision observations differ from policy prefix")
    observed_paths: list[Path] = []
    for observation in decision.observations:
        manifest_path = _v1._resolve_relative_hash(
            repo_root,
            observation.postprocess_manifest.artifact,
            observation.postprocess_manifest.sha256,
            label="expansion observation manifest",
        )
        observed_paths.append(manifest_path)
    try:
        replayed, frame_bytes = evaluate_tranche_expansion(
            repo_root=repo_root,
            loaded_policy=loaded_policy,
            observed_manifests=tuple(observed_paths),
        )
    except TrancheExpansionError as exc:
        raise ResearchCollectionV5Error("expansion decision replay failed") from exc
    if frame_bytes is not None or replayed != decision:
        raise ResearchCollectionV5Error("expansion decision differs from exact policy replay")
    return decision, policy


def _build_plan(
    *,
    loaded_config: LoadedConfig[ResearchCollectionV5Config],
    config_path: Path,
    config_file_sha256: str,
    repo_root: Path,
    problems: tuple[ProblemPoolRecord, ...],
    family_bindings: tuple[_v1.ResearchFamilyBinding, ...],
    invocations: tuple[_v1.ResearchCollectionInvocation, ...],
    expansion_decision: ExpansionDecision,
    expansion_policy: TrancheExpansionPolicy,
) -> ResearchCollectionPlanV5:
    config = loaded_config.config
    seed_counts = dict(sorted((family.family_id, len(family.seeds)) for family in config.families))
    payload: dict[str, object] = {
        "schema_version": 5,
        "tranche_id": config.tranche_id,
        "pool_dialect": config.problem_pool_contract.pool_dialect,
        "overlap_schema": config.required_overlap_schema,
        "expansion_decision_id": expansion_decision.decision_id,
        "expansion_decision_sha256": config.expansion_decision.sha256,
        "expansion_policy_id": expansion_policy.policy_id,
        "expansion_policy_sha256": config.expansion_policy.sha256,
        "collection_config_artifact": str(config_path.resolve().relative_to(repo_root)),
        "collection_config_file_sha256": config_file_sha256,
        "collection_config_hash": loaded_config.config_hash,
        "problem_pool_records_sha256": config.problem_pool_records.sha256,
        "problem_pool_manifest_sha256": config.problem_pool_manifest.sha256,
        "context_sha256": config.context.sha256,
        "import_header_sha256": config.import_header.sha256,
        "source_matrix_sha256": config.source_matrix.sha256,
        "runtime_adapter_sha256": config.runtime.runtime_adapter.sha256,
        "environment_lock_sha256": config.runtime.environment_lock.sha256,
        "orchestration_adapter_sha256": config.runtime.orchestration_adapter.sha256,
        "orchestration_cli_sha256": config.orchestration_cli.sha256,
        "runtime_hash": config.runtime.runtime_hash,
        "shared_execution_record_schema": config.shared_execution_record_schema,
        "problem_count": len(problems),
        "family_count": 3,
        "seed_count_by_family": seed_counts,
        "expected_candidate_count": len(invocations),
        "problem_record_ids": [problem.problem_record_id for problem in problems],
        "family_bindings": [item.model_dump(mode="json") for item in family_bindings],
        "invocations": [item.model_dump(mode="json") for item in invocations],
        "actual_collection_performed": False,
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    plan_id = "research_collection_plan_v5:" + hash_canonical(
        {"schema": "lf021_research_collection_plan_v5", **payload}
    )
    return ResearchCollectionPlanV5.model_validate({"plan_id": plan_id, **payload})


def load_research_collection_v5(
    config_path: Path,
    *,
    repo_root: Path,
) -> LoadedResearchCollectionV5:
    """Replay a generic v5 tranche without importing GPU libraries."""

    root = repo_root.resolve()
    config_path = config_path.resolve()
    try:
        config_path.relative_to(root)
    except ValueError as exc:
        raise ResearchCollectionV5Error("v5 config must remain in the repository") from exc
    loaded = load_config(config_path, ResearchCollectionV5Config)
    config = loaded.config
    expansion_decision, expansion_policy = _load_expansion_authorization(
        repo_root=root,
        decision_binding=config.expansion_decision,
        policy_binding=config.expansion_policy,
    )
    next_tranche = expansion_decision.next_tranche
    assert next_tranche is not None
    policy_pool = {pool.pool_id: pool for pool in expansion_policy.pools}[next_tranche.pool_id]
    if (
        config.tranche_id != next_tranche.tranche_id
        or config.problem_pool_contract.expected_problem_count
        != next_tranche.expected_problem_count
        or {family.family_id: family.seeds for family in config.families}
        != {family_id: (seed,) for family_id, seed in next_tranche.seeds_by_family.items()}
        or config.problem_pool_records.sha256 != policy_pool.records.sha256
        or config.problem_pool_manifest.sha256 != policy_pool.manifest.sha256
    ):
        raise ResearchCollectionV5Error("v5 config differs from expansion-selected tranche")
    artifact_bindings = (
        config.expansion_decision,
        config.expansion_policy,
        config.problem_pool_records,
        config.problem_pool_manifest,
        config.context,
        config.import_header,
        config.source_matrix,
        config.orchestration_cli,
        config.runtime.runtime_adapter,
        config.runtime.environment_lock,
        config.runtime.orchestration_adapter,
    )
    resolved = {
        binding.artifact: _v1._resolve_binding(root, binding) for binding in artifact_bindings
    }
    problems = _v3._load_problem_records_v3(resolved[config.problem_pool_records.artifact])
    manifest_document = _v3._load_canonical_mapping(resolved[config.problem_pool_manifest.artifact])
    try:
        pool_evidence = _v3._validate_problem_pool_manifest(
            document=manifest_document,
            contract=config.problem_pool_contract,
            config=cast(Any, config),
            problems=problems,
        )
    except _v3.ResearchCollectionV3Error as exc:
        raise ResearchCollectionV5Error("v5 closed-pool validation failed") from exc
    for binding in pool_evidence.critical_artifact_bindings:
        _v3._resolve_pool_binding(root, binding)

    context = _v1._load_json_record(
        resolved[config.context.artifact],
        ContextRecord,
    )
    header_text = resolved[config.import_header.artifact].read_text(encoding="utf-8")
    if (
        context.header_hash != config.import_header.sha256
        or context.header_text != header_text
        or any(
            problem.context_id != context.context_id
            or problem.import_header_artifact != config.import_header.artifact
            or problem.import_header_hash != config.import_header.sha256
            for problem in problems
        )
    ):
        raise ResearchCollectionV5Error("v5 problem, context, and import-header bindings disagree")

    matrix = _load_source_matrix(resolved[config.source_matrix.artifact])
    if (
        matrix.problem_count != len(problems)
        or matrix.problem_pool_manifest_sha256 != config.problem_pool_manifest.sha256
        or matrix.pool_dialect != config.problem_pool_contract.pool_dialect
        or matrix.tranche_id != config.tranche_id
    ):
        raise ResearchCollectionV5Error("v5 source matrix differs from exact pool")
    legacy_matrix_path = _v1._resolve_binding(root, matrix.base_source_matrix)
    legacy_matrix = _load_legacy_source_matrix(
        legacy_matrix_path,
        dialect=matrix.pool_dialect,
    )
    legacy_families = tuple(
        (item.family_id, item.model, item.revision) for item in legacy_matrix.families
    )
    v5_families = tuple((item.family_id, item.model, item.revision) for item in matrix.families)
    if (
        legacy_matrix.problem_count != matrix.problem_count
        or legacy_matrix.problem_pool_manifest_sha256 != matrix.problem_pool_manifest_sha256
        or legacy_families != v5_families
        or legacy_matrix.heldout.model_dump(mode="json") != matrix.heldout.model_dump(mode="json")
    ):
        raise ResearchCollectionV5Error("v5 source matrix differs from immutable base matrix")
    matrix_by_family = {family.family_id: family for family in matrix.families}

    qualifications: dict[str, LoadedConfig[LocalQualificationConfig]] = {}
    family_bindings: list[_v1.ResearchFamilyBinding] = []
    for family in config.families:
        pin_path = _v1._resolve_binding(root, family.qualification_pin_source)
        qualification = load_local_qualification_config(pin_path, repo_root=root)
        matrix_family = matrix_by_family.get(family.family_id)
        model = qualification.config.active_model
        if (
            matrix_family is None
            or model.family_id != family.family_id
            or model.repo_id != matrix_family.model
            or model.revision != matrix_family.revision
        ):
            raise ResearchCollectionV5Error(
                f"v5 family differs from source-matrix pin: {family.family_id}"
            )
        qualifications[family.family_id] = qualification
        family_bindings.append(
            _v1._family_binding(
                family=family,
                loaded=qualification,
                config_file_sha256=hash_file(pin_path),
                runtime=config.runtime,
            )
        )
    ordered_bindings = tuple(sorted(family_bindings, key=lambda item: item.family_id))
    activation_evidence = {
        family.family_id: _verify_activation_v5(
            family=family,
            qualification=qualifications[family.family_id],
            config=config,
            pool_evidence=pool_evidence,
            problems=problems,
            repo_root=root,
        )
        for family in config.families
    }
    if {evidence.overlap_schema for evidence in activation_evidence.values()} != {
        config.required_overlap_schema
    }:
        raise ResearchCollectionV5Error("v5 overlap evidence version is inconsistent")

    invocations = _v3._make_invocations(
        config_hash=loaded.config_hash,
        config=cast(Any, config),
        family_bindings=ordered_bindings,
        qualifications=qualifications,
        problems=problems,
        repo_root=root,
        context=context,
        header_text=header_text,
    )
    plan = _build_plan(
        loaded_config=loaded,
        config_path=config_path,
        config_file_sha256=hash_file(config_path),
        repo_root=root,
        problems=problems,
        family_bindings=ordered_bindings,
        invocations=invocations,
        expansion_decision=expansion_decision,
        expansion_policy=expansion_policy,
    )
    blockers = tuple(
        f"{family.family_id}: {family.activation.blocker}"
        for family in config.families
        if family.activation.status == "blocked"
    )
    ready = config.status == "ready" and config.execution_enabled and not blockers
    preflight = ResearchCollectionPreflightReportV5(
        report_kind="lf021_local_research_collection_preflight_v5",
        execution_ready=ready,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        tranche_id=plan.tranche_id,
        pool_dialect=plan.pool_dialect,
        overlap_schema=plan.overlap_schema,
        expansion_decision_id=plan.expansion_decision_id,
        expansion_decision_sha256=plan.expansion_decision_sha256,
        expansion_policy_id=plan.expansion_policy_id,
        expansion_policy_sha256=plan.expansion_policy_sha256,
        problem_count=plan.problem_count,
        seed_count_by_family=plan.seed_count_by_family,
        planned_candidate_count=plan.expected_candidate_count,
        family_binding_hashes={
            family.family_id: family.binding_hash for family in ordered_bindings
        },
        invocation_ids=tuple(item.invocation_id for item in invocations),
        checks={
            "closed_pool_dialect_replayed": True,
            "collection_is_non_smoke": True,
            "context_and_header_bound": True,
            "exact_checkpoint_prompt_parser_runtime_bound": True,
            "exact_overlap_schema_matches_pool_dialect": True,
            "exact_v5_module_and_cli_bound": True,
            "expansion_decision_and_policy_replayed": True,
            "expansion_observed_prefix_replayed": True,
            "expansion_selected_pool_count_and_seeds_match": True,
            "exactly_three_unique_families": True,
            "expected_declaration_names_unique": True,
            "problem_pool_is_public_eligible_and_denylist_clear": True,
            "qualification_and_overlap_evidence_replayed": True,
            "seeds_are_sorted_unique_and_nonnegative": True,
            "tranche_scope_and_v5_outputs_are_truthful": True,
            "zero_labels_and_zero_gate_claims": True,
        },
        blocking_prerequisites=blockers,
    )
    return LoadedResearchCollectionV5(
        config=loaded,
        problems=problems,
        context=context,
        source_matrix=matrix,
        pool_evidence=pool_evidence,
        qualifications=qualifications,
        activation_evidence=activation_evidence,
        plan=plan,
        preflight=preflight,
    )


def write_preflight_report_v5(
    loaded: LoadedResearchCollectionV5,
    *,
    repo_root: Path,
) -> tuple[Path, str]:
    """Persist or replay the exact model-free v5 preflight."""

    path = repo_root / loaded.config.config.outputs.preflight_report
    digest = _v1._write_immutable(
        path,
        _v1._canonical_record_bytes(loaded.preflight),
    )
    return path, digest


def _terminal_path(
    root: Path,
    invocation: _v1.ResearchCollectionInvocation,
) -> Path:
    return root / "terminals" / f"{invocation.invocation_id.rsplit(':', 1)[-1]}.json"


def execute_research_collection_v5(
    loaded: LoadedResearchCollectionV5,
    *,
    repo_root: Path,
    executor: ResearchInvocationExecutorV5,
    clock: _v1.Clock = lambda: datetime.datetime.now(datetime.UTC),
) -> ResearchCollectionRunV5:
    """Execute or deterministically resume one terminal per frozen invocation."""

    if not loaded.preflight.execution_ready:
        raise ResearchCollectionV5ExecutionBlocked(
            "research collection v5 is blocked: "
            + "; ".join(loaded.preflight.blocking_prerequisites)
        )
    root = repo_root / loaded.config.config.outputs.root / loaded.plan.plan_id.rsplit(":", 1)[-1]
    plan_path = root / "plan.json"
    _v1._write_immutable(plan_path, _v1._canonical_record_bytes(loaded.plan))
    problem_by_id = {item.problem_record_id: item for item in loaded.problems}
    terminals: list[_v1.ResearchCollectionTerminal] = []
    binding_by_family = {binding.family_id: binding for binding in loaded.plan.family_bindings}
    for family_id in sorted(binding_by_family):
        family_invocations = tuple(
            invocation
            for invocation in loaded.plan.invocations
            if invocation.family_id == family_id
        )
        pending: list[_v1.ResearchCollectionInvocation] = []
        for invocation in family_invocations:
            terminal_path = _terminal_path(root, invocation)
            if not terminal_path.exists():
                pending.append(invocation)
                continue
            terminal = _v1._load_canonical(
                terminal_path,
                _v1.ResearchCollectionTerminal,
            )
            if (
                terminal.invocation_id != invocation.invocation_id
                or terminal.invocation_payload_hash
                != hash_canonical(invocation.model_dump(mode="json"))
            ):
                raise ResearchCollectionV5ArtifactConflict(
                    f"terminal differs from invocation: {terminal_path}"
                )
            terminals.append(terminal)
        if not pending:
            continue

        binding = binding_by_family[family_id]
        family_directory = root / "families" / family_id
        try:
            executor.begin_family(
                family=binding,
                qualification=loaded.qualifications[family_id],
                runtime=loaded.config.config.runtime,
                invocations=tuple(pending),
                family_directory=family_directory,
            )
        except Exception as exc:
            for invocation in pending:
                terminal = _v1.make_orchestration_failure_terminal(
                    invocation,
                    exception=exc,
                    at=clock(),
                )
                _v1._write_immutable(
                    _terminal_path(root, invocation),
                    _v1._canonical_record_bytes(terminal),
                )
                terminals.append(terminal)
            continue

        completed: list[str] = []
        try:
            for invocation in pending:
                terminal_path = _terminal_path(root, invocation)
                invocation_directory = (
                    root / "invocations" / invocation.invocation_id.rsplit(":", 1)[-1]
                )
                try:
                    terminal = executor.execute(
                        invocation=invocation,
                        problem=problem_by_id[invocation.problem_record_id],
                        qualification=loaded.qualifications[invocation.family_id],
                        invocation_directory=invocation_directory,
                        artifact_root=repo_root,
                    )
                except Exception as exc:
                    crossed = (invocation_directory / "provider_request.json").exists() or (
                        invocation_directory / "provider_boundary.json"
                    ).exists()
                    if crossed:
                        raise _v1.ResearchCollectionPostBoundaryError(
                            "executor raised after a provider/model-attempt boundary; "
                            "the v5 run remains incomplete"
                        ) from exc
                    terminal = _v1.make_orchestration_failure_terminal(
                        invocation,
                        exception=exc,
                        at=clock(),
                    )
                _v1._write_immutable(
                    terminal_path,
                    _v1._canonical_record_bytes(terminal),
                )
                terminals.append(terminal)
                completed.append(invocation.invocation_id)
        finally:
            executor.end_family(
                family=binding,
                completed_invocation_ids=tuple(completed),
                family_directory=family_directory,
            )

    terminals.sort(key=lambda item: item.invocation_id)
    if len(terminals) != loaded.plan.expected_candidate_count:
        raise ResearchCollectionV5ArtifactConflict(
            "v5 run did not produce one terminal per invocation"
        )
    for terminal in terminals:
        if terminal.family_session_id is None:
            continue
        session_start = (
            root
            / "families"
            / terminal.family_id
            / "sessions"
            / terminal.family_session_id.rsplit(":", 1)[-1]
            / "family_session_start.json"
        )
        if not session_start.is_file() or hash_file(session_start) != terminal.artifact_hashes.get(
            "family_session_start"
        ):
            raise ResearchCollectionV5ArtifactConflict(
                "terminal family-session binding is missing or changed"
            )
    status_counts = Counter(item.status.value for item in terminals)
    successful_families = {
        item.family_id
        for item in terminals
        if item.status is _v1.ResearchTerminalStatus.RAW_COLLECTED
    }
    terminal_hashes = {
        str(_terminal_path(root, invocation).relative_to(repo_root)): hash_file(
            _terminal_path(root, invocation)
        )
        for invocation in loaded.plan.invocations
    }
    family_session_hashes = {
        str(path.relative_to(repo_root)): hash_file(path)
        for path in sorted((root / "families").glob("**/*.json"))
        if path.is_file() and not path.is_symlink()
    }
    payload: dict[str, object] = {
        "schema_version": 5,
        "plan_id": loaded.plan.plan_id,
        "plan_hash": loaded.plan.plan_hash,
        "tranche_id": loaded.plan.tranche_id,
        "pool_dialect": loaded.plan.pool_dialect,
        "overlap_schema": loaded.plan.overlap_schema,
        "expansion_decision_id": loaded.plan.expansion_decision_id,
        "expansion_decision_sha256": loaded.plan.expansion_decision_sha256,
        "expansion_policy_id": loaded.plan.expansion_policy_id,
        "expansion_policy_sha256": loaded.plan.expansion_policy_sha256,
        "shared_execution_record_schema": loaded.plan.shared_execution_record_schema,
        "actual_collection_performed": True,
        "problem_count": loaded.plan.problem_count,
        "family_count": loaded.plan.family_count,
        "seed_count_by_family": loaded.plan.seed_count_by_family,
        "expected_candidate_count": loaded.plan.expected_candidate_count,
        "terminal_candidate_count": len(terminals),
        "status_counts": dict(sorted(status_counts.items())),
        "successful_family_count": len(successful_families),
        "terminal_artifact_hashes": dict(sorted(terminal_hashes.items())),
        "family_session_artifact_hashes": dict(sorted(family_session_hashes.items())),
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    manifest_id = "research_collection_manifest_v5:" + hash_canonical(
        {"schema": "lf021_research_collection_manifest_v5", **payload}
    )
    manifest = ResearchCollectionManifestV5.model_validate({"manifest_id": manifest_id, **payload})
    manifest_path = root / "manifest.json"
    _v1._write_immutable(
        manifest_path,
        _v1._canonical_record_bytes(manifest),
    )
    return ResearchCollectionRunV5(
        output_directory=root,
        plan_path=plan_path,
        manifest_path=manifest_path,
        manifest=manifest,
        terminals=tuple(terminals),
    )


def derive_research_collection_v5_config(
    *,
    base_config_path: Path,
    expansion_decision_path: Path,
    expansion_policy_path: Path,
    output_source_matrix_path: Path,
    output_config_path: Path,
    repo_root: Path,
    frozen_at: datetime.datetime,
) -> tuple[Path, str]:
    """Create an immutable v5 config from a bound v2 or v3 pool config.

    This is the decision-complete tranche contract.  It changes only tranche
    identity, frozen seeds, v5 orchestration/output bindings, and versioned
    policy text.  Every pool, model, qualification, and overlap artifact remains
    byte-for-byte bound to the selected base configuration.
    """

    root = repo_root.resolve()
    source = base_config_path.resolve()
    decision_path = expansion_decision_path.resolve()
    policy_path = expansion_policy_path.resolve()
    matrix_destination = output_source_matrix_path.resolve()
    destination = output_config_path.resolve()
    for path, label in (
        (source, "base"),
        (decision_path, "expansion decision"),
        (policy_path, "expansion policy"),
        (matrix_destination, "source-matrix output"),
        (destination, "config output"),
    ):
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ResearchCollectionV5Error(f"v5 {label} config must remain in repository") from exc
    decision_binding = _v1.ResearchArtifactBinding(
        artifact=str(decision_path.relative_to(root)),
        sha256=hash_file(decision_path),
    )
    policy_binding = _v1.ResearchArtifactBinding(
        artifact=str(policy_path.relative_to(root)),
        sha256=hash_file(policy_path),
    )
    decision, policy = _load_expansion_authorization(
        repo_root=root,
        decision_binding=decision_binding,
        policy_binding=policy_binding,
    )
    next_tranche = decision.next_tranche
    assert next_tranche is not None
    tranche_id = next_tranche.tranche_id
    seeds_by_family = {
        family_id: (seed,) for family_id, seed in next_tranche.seeds_by_family.items()
    }
    raw = load_yaml_mapping(source)
    version = raw.get("schema_version")
    base_matrix: LegacySourceMatrix
    if version == 2:
        base = _v2.ResearchCollectionV2Config.model_validate(raw)
        dialect: PoolDialect = "gate3_algebra_operational_v1"
        pool_slug = "gate3_docstrings_operational_v1"
        document = base.model_dump(mode="json")
        base_matrix_path = _v1._resolve_binding(root, base.source_matrix)
        base_matrix = _v2.ScalableResearchSourceMatrixV2.model_validate(
            load_yaml_mapping(base_matrix_path)
        )
        contract = dict(cast(dict[str, object], document["problem_pool_contract"]))
        contract["pool_dialect"] = dialect
        document["problem_pool_contract"] = contract
    elif version == 3:
        base_v3 = _v3.ResearchCollectionV3Config.model_validate(raw)
        dialect = base_v3.problem_pool_contract.pool_dialect
        pool_slug = {
            "gate3_algebra_operational_v1": "gate3_docstrings_operational_v1",
            "cross_domain_operational_v1": "cross_domain_docstrings_operational_v1",
        }[dialect]
        document = base_v3.model_dump(mode="json")
        base_matrix_path = _v1._resolve_binding(root, base_v3.source_matrix)
        base_matrix = _v3.ScalableResearchSourceMatrixV3.model_validate(
            load_yaml_mapping(base_matrix_path)
        )
    else:
        raise ResearchCollectionV5Error("base config must be collector v2 or v3")

    selected_pool = {item.pool_id: item for item in policy.pools}[next_tranche.pool_id]
    expected_dialect = {
        "algebra_gate3_docstrings_v1": "gate3_algebra_operational_v1",
        "cross_domain_docstrings_v1": "cross_domain_operational_v1",
    }.get(next_tranche.pool_id)
    if (
        expected_dialect != dialect
        or document["problem_pool_records"]["sha256"] != selected_pool.records.sha256
        or document["problem_pool_manifest"]["sha256"] != selected_pool.manifest.sha256
        or next_tranche.expected_problem_count != selected_pool.problem_count
    ):
        raise ResearchCollectionV5Error("base config does not bind the expansion-selected pool")
    expected_families = tuple(family["family_id"] for family in document["families"])
    if tuple(sorted(seeds_by_family)) != tuple(sorted(expected_families)):
        raise ResearchCollectionV5Error(
            "seed mapping must cover exactly the three base-config families"
        )
    for family in cast(list[dict[str, object]], document["families"]):
        seeds = tuple(seeds_by_family[cast(str, family["family_id"])])
        if not seeds or seeds != tuple(sorted(set(seeds))) or any(seed < 0 for seed in seeds):
            raise ResearchCollectionV5Error("each family requires sorted unique nonnegative seeds")
        family["seeds"] = list(seeds)

    module_path = root / _V5_ORCHESTRATION_ARTIFACT
    cli_path = root / _V5_ORCHESTRATION_CLI
    matrix_payload: dict[str, object] = {
        "schema_version": 5,
        "tranche_id": tranche_id,
        "status": "pool_compatible_activation_external_to_matrix",
        "pool_dialect": dialect,
        "source": base_matrix.source,
        "problem_count": base_matrix.problem_count,
        "problem_pool_manifest_sha256": (base_matrix.problem_pool_manifest_sha256),
        "base_source_matrix": {
            "artifact": str(base_matrix_path.relative_to(root)),
            "sha256": hash_file(base_matrix_path),
        },
        "private_source_content": False,
        "external_transmission_required": False,
        "semantic_labels_created": False,
        "gate_5g_credit_authorized": False,
        "collection_authorization_source": (
            "v5_config_and_original_pool_specific_overlap_evidence"
        ),
        "families": [family.model_dump(mode="json") for family in base_matrix.families],
        "heldout": base_matrix.heldout.model_dump(mode="json"),
        "rules": [
            "this tranche matrix preserves the exact immutable base-matrix identities",
            "activation remains in the v5 config and exact pool-specific overlap records",
            "model output creates no semantic label, supervision admission, or Gate claim",
        ],
    }
    matrix_id = "local_research_source_matrix_v5:" + hash_canonical(
        {"schema": "lf021_research_source_matrix_v5", **matrix_payload}
    )
    matrix = ScalableResearchSourceMatrixV5.model_validate(
        {"matrix_id": matrix_id, **matrix_payload}
    )
    matrix_digest = _v1._write_immutable(
        matrix_destination,
        _v1._canonical_record_bytes(matrix),
    )
    document["source_matrix"] = {
        "artifact": str(matrix_destination.relative_to(root)),
        "sha256": matrix_digest,
    }
    document["orchestration_cli"] = {
        "artifact": _V5_ORCHESTRATION_CLI,
        "sha256": hash_file(cli_path),
    }
    runtime = cast(dict[str, object], document["runtime"])
    runtime["orchestration_adapter"] = {
        "artifact": _V5_ORCHESTRATION_ARTIFACT,
        "sha256": hash_file(module_path),
    }
    document.update(
        {
            "schema_version": 5,
            "config_id": "lf021_local_research_collection_v5",
            "tranche_id": tranche_id,
            "frozen_at": frozen_at.isoformat().replace("+00:00", "Z"),
            "collection_scope": ("preregistered_closed_pool_three_family_tranche_v5"),
            "expansion_decision": decision_binding.model_dump(mode="json"),
            "expansion_policy": policy_binding.model_dump(mode="json"),
            "outputs": {
                "root": (f"data/raw/real_outputs/{pool_slug}/v5/{tranche_id}/local_collection"),
                "preflight_report": (
                    "reports/generation/"
                    f"lf021_local_research_collection_preflight_{tranche_id}_v5.json"
                ),
            },
            "rules": [
                "this is one preregistered closed-pool tranche over exactly three local families",
                "the original exact qualification and pool-specific overlap records remain bound",
                (
                    "raw collection creates no parsing result, semantic label, "
                    "supervision admission, or Gate claim"
                ),
                (
                    "every problem x family x seed has one deterministic "
                    "invocation and terminal outcome"
                ),
                "completed artifacts are immutable and resume verifies their exact hashes",
                (
                    "v1 execution primitives are reused; config, plan, preflight, "
                    "manifest, tranche, and roots are v5"
                ),
            ],
        }
    )
    config = ResearchCollectionV5Config.model_validate(document)
    digest = _v1._write_immutable(
        destination,
        _v1._canonical_record_bytes(config),
    )
    return destination, digest


LocalHFResearchExecutor = _v1.LocalHFResearchExecutor
ResearchCollectionTerminal = _v1.ResearchCollectionTerminal
ResearchTerminalStatus = _v1.ResearchTerminalStatus


__all__ = [
    "LoadedResearchCollectionV5",
    "LocalHFResearchExecutor",
    "ResearchCollectionManifestV5",
    "ResearchCollectionPlanV5",
    "ResearchCollectionPreflightReportV5",
    "ResearchCollectionRunV5",
    "ResearchCollectionTerminal",
    "ResearchCollectionV5ArtifactConflict",
    "ResearchCollectionV5Config",
    "ResearchCollectionV5Error",
    "ResearchCollectionV5ExecutionBlocked",
    "ResearchInvocationExecutorV5",
    "ResearchTerminalStatus",
    "ScalableResearchSourceMatrixV5",
    "VerifiedResearchFamilyActivationV5",
    "derive_research_collection_v5_config",
    "execute_research_collection_v5",
    "load_research_collection_v5",
    "write_preflight_report_v5",
]
