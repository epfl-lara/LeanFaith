"""Resumable LF-021 raw collection over the frozen cross-domain public pool.

This module is a separately versioned successor to :mod:`research_collection_v2`.
It does not alter or reinterpret the immutable v1 or v2 collectors or their
bound artifacts. Version 3 accepts only the truthful 20-record cross-domain
operational manifest and exactly three locally qualified model families.

The actual provider boundary, model-attempt boundary, local generation result,
family session, and terminal record primitives remain the already-audited v1
execution records.  That reuse is explicit in the v3 config through
``shared_execution_record_schema``; a v1 collection config is never accepted
by this loader.  Plans, preflight reports, manifests, output roots, and config
identity are all v3.

Raw collection performs no parsing, Lean validation, semantic admission,
labeling, or gate closure.
"""

from __future__ import annotations

import datetime
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.generation import research_collection as _v1
from leanfaith.generation import research_collection_v2 as _v2
from leanfaith.generation.local_qualification import (
    LocalQualificationConfig,
    load_local_qualification_config,
)
from leanfaith.generation.public_research_pool import HeldoutResearchFamily
from leanfaith.generation.research_overlap_v3 import ResearchFamilyOverlapRecordV3
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

_HEX64 = r"^[0-9a-f]{64}$"
_PLAN_ID = r"^research_collection_plan_v3:[0-9a-f]{64}$"
_MANIFEST_ID = r"^research_collection_manifest_v3:[0-9a-f]{64}$"
_V3_ORCHESTRATION_ARTIFACT = "src/leanfaith/generation/research_collection_v3.py"


class ResearchCollectionV3Error(_v1.ResearchCollectionError):
    """The scalable collection contract is invalid or incomplete."""


class ResearchCollectionV3ExecutionBlocked(
    ResearchCollectionV3Error,
    _v1.ResearchCollectionExecutionBlocked,
):
    """Execution was requested before every v3 prerequisite passed."""


class ResearchCollectionV3ArtifactConflict(
    ResearchCollectionV3Error,
    _v1.ResearchCollectionArtifactConflict,
):
    """A supposedly immutable v3 artifact contains different bytes."""


class ScalableProblemPoolContract(StrictModel):
    """Closed, exact manifest dialect accepted by collection v3."""

    manifest_schema_version: Literal[1] = 1
    pool_dialect: Literal[
        "gate3_algebra_operational_v1",
        "cross_domain_operational_v1",
    ]
    manifest_artifact_kind: Literal[
        "lf021_gate3_docstrings_operational_problem_pool_v1",
        "lf021_cross_domain_docstrings_operational_problem_pool_v1",
    ]
    expected_problem_count: int = Field(ge=1)
    require_public_records: Literal[True] = True
    require_eligible_records: Literal[True] = True
    require_denylist_clear: Literal[True] = True
    require_local_model_authorization: Literal[True] = True
    require_reference_hidden: Literal[True] = True
    require_no_semantic_labels: Literal[True] = True
    require_no_gate_claims: Literal[True] = True

    @model_validator(mode="after")
    def _dialect_matches_manifest_kind(self) -> Self:
        expected = {
            "gate3_algebra_operational_v1": ("lf021_gate3_docstrings_operational_problem_pool_v1"),
            "cross_domain_operational_v1": (
                "lf021_cross_domain_docstrings_operational_problem_pool_v1"
            ),
        }[self.pool_dialect]
        if self.manifest_artifact_kind != expected:
            raise ValueError("pool dialect and manifest artifact kind disagree")
        return self


class ScalableResearchFamily(StrictModel):
    """One local model identity in the non-authoritative v3 source matrix."""

    family_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    role: Literal["supervision_candidate"] = "supervision_candidate"
    transport: Literal["local"] = "local"
    pool_compatible: Literal[True] = True


class ScalableResearchSourceMatrixV3(StrictModel):
    """Identity matrix only; activation remains in the collection config."""

    schema_version: Literal[3] = 3
    matrix_id: Literal["local_research_source_matrix_v3"]
    status: Literal["pool_compatible_activation_external_to_matrix"]
    pool_dialect: Literal[
        "gate3_algebra_operational_v1",
        "cross_domain_operational_v1",
    ]
    source: Literal[
        "mathlib_gate3_docstrings_operational_v1",
        "mathlib_cross_domain_docstrings_operational_v1",
    ]
    problem_count: int = Field(ge=1)
    problem_pool_manifest_sha256: str = Field(pattern=_HEX64)
    private_source_content: Literal[False] = False
    external_transmission_required: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_authorized: Literal[False] = False
    collection_authorization_source: Literal[
        "v3_config_and_replayed_qualification_overlap_evidence"
    ]
    families: tuple[ScalableResearchFamily, ...]
    heldout: HeldoutResearchFamily
    rules: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len(self.families) != 3:
            raise ValueError("scalable source matrix requires exactly three families")
        ids = tuple(family.family_id for family in self.families)
        models = tuple(family.model for family in self.families)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("source-matrix families must be sorted and unique")
        if len(models) != len(set(models)):
            raise ValueError("source-matrix model IDs must be unique")
        expected_source = {
            "gate3_algebra_operational_v1": "mathlib_gate3_docstrings_operational_v1",
            "cross_domain_operational_v1": ("mathlib_cross_domain_docstrings_operational_v1"),
        }[self.pool_dialect]
        if self.source != expected_source:
            raise ValueError("source matrix pool dialect and source disagree")
        return self


class ScalablePoolArtifactBinding(StrictModel):
    """One manifest-bound repository or absolute content-addressed artifact."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _safe_path(self) -> Self:
        path = PurePosixPath(self.path)
        if ".." in path.parts or not self.path.strip():
            raise ValueError("pool artifact path cannot be empty or contain '..'")
        return self


class ResearchCollectionV3Config(StrictModel):
    """Separate authorization for cross-domain LF-021 raw collection."""

    schema_version: Literal[3] = 3
    config_id: Literal["lf021_local_research_collection_v3"]
    tranche_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    frozen_at: datetime.datetime
    artifact_class: Literal["research"] = "research"
    collection_scope: Literal["cross_domain_s0_three_family_v3"]
    shared_execution_record_schema: Literal["lf021_research_execution_records_v1"] = (
        "lf021_research_execution_records_v1"
    )
    status: Literal["blocked_pending_family_activation", "ready"]
    execution_enabled: bool
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    problem_pool_contract: ScalableProblemPoolContract
    problem_pool_records: _v1.ResearchArtifactBinding
    problem_pool_manifest: _v1.ResearchArtifactBinding
    context: _v1.ResearchArtifactBinding
    import_header: _v1.ResearchArtifactBinding
    source_matrix: _v1.ResearchArtifactBinding
    runtime: _v1.ResearchRuntimeBinding
    families: tuple[_v1.ResearchCollectionFamily, ...]
    retry: _v1.ResearchRetryConfig
    outputs: _v1.ResearchCollectionOutputs
    rules: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        from leanfaith.schemas.manifest import require_utc

        require_utc(self.frozen_at)
        if len(self.families) != 3:
            raise ValueError("collection v3 requires exactly three model families")
        family_ids = [family.family_id for family in self.families]
        provider_slots = [family.provider_slot for family in self.families]
        if family_ids != sorted(set(family_ids)):
            raise ValueError("collection families must be sorted and unique")
        if len(provider_slots) != len(set(provider_slots)):
            raise ValueError("collection provider slots must be unique")
        ready = all(family.activation.status == "ready" for family in self.families)
        if self.status == "ready":
            if not self.execution_enabled or not ready:
                raise ValueError("ready v3 collection requires all family activations")
        elif self.execution_enabled or ready:
            raise ValueError("blocked v3 collection cannot enable all execution")
        if self.runtime.orchestration_adapter.artifact != _V3_ORCHESTRATION_ARTIFACT:
            raise ValueError("collection v3 must bind the v3 orchestration module")
        if "v3" not in PurePosixPath(self.outputs.root).parts:
            raise ValueError("collection v3 output root must contain a v3 path component")
        if self.tranche_id not in PurePosixPath(self.outputs.root).parts:
            raise ValueError("collection v3 output root must contain the exact tranche_id")
        if "v3" not in PurePosixPath(self.outputs.preflight_report).stem:
            raise ValueError("collection v3 preflight report must be explicitly versioned v3")
        return self


@dataclass(frozen=True, slots=True)
class ScalableProblemPoolEvidence:
    """Validated projection of the operational pool manifest."""

    schema_version: int
    artifact_kind: str
    problem_count: int
    problem_record_ids: tuple[str, ...]
    problem_groups: tuple[str, ...]
    declaration_full_names: tuple[str, ...]
    active_benchmark_manifest_sha256: str
    active_benchmark_registry_sha256: str
    public_source_evidence_sha256: str
    critical_artifact_bindings: tuple[ScalablePoolArtifactBinding, ...]
    raw_document: Mapping[str, object]


class ResearchCollectionPlanV3(StrictModel):
    """Deterministic arbitrary-size plan using the audited execution records."""

    schema_version: Literal[3] = 3
    plan_id: str = Field(pattern=_PLAN_ID)
    tranche_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
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

    def id_payload(self) -> dict[str, object]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key != "plan_id"
        }

    @property
    def plan_hash(self) -> str:
        return hash_canonical(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def _identity_and_counts(self) -> Self:
        if len(self.problem_record_ids) != self.problem_count:
            raise ValueError("plan problem IDs do not reconcile problem_count")
        if self.problem_record_ids != tuple(sorted(set(self.problem_record_ids))):
            raise ValueError("plan problem IDs must be sorted and unique")
        family_ids = tuple(binding.family_id for binding in self.family_bindings)
        if len(family_ids) != self.family_count or family_ids != tuple(sorted(set(family_ids))):
            raise ValueError("plan requires three sorted unique family bindings")
        if list(self.seed_count_by_family) != sorted(self.seed_count_by_family):
            raise ValueError("seed_count_by_family must be sorted")
        if set(self.seed_count_by_family) != set(family_ids):
            raise ValueError("seed-count families differ from family bindings")
        if any(count < 1 for count in self.seed_count_by_family.values()):
            raise ValueError("every family must have at least one seed")
        expected_count = self.problem_count * sum(self.seed_count_by_family.values())
        if self.expected_candidate_count != expected_count:
            raise ValueError("expected candidate count differs from pool x family seeds")
        if len(self.invocations) != expected_count:
            raise ValueError("plan invocation count does not reconcile")
        invocation_ids = tuple(item.invocation_id for item in self.invocations)
        if invocation_ids != tuple(sorted(set(invocation_ids))):
            raise ValueError("plan invocations must be sorted and unique")
        actual_counts = Counter(item.family_id for item in self.invocations)
        for family_id, seed_count in self.seed_count_by_family.items():
            if actual_counts[family_id] != self.problem_count * seed_count:
                raise ValueError("family invocation count does not reconcile")
        if {item.problem_record_id for item in self.invocations} != set(self.problem_record_ids):
            raise ValueError("invocation problems differ from the frozen pool")
        expected = "research_collection_plan_v3:" + hash_canonical(
            {"schema": "lf021_research_collection_plan_v3", **self.id_payload()}
        )
        if self.plan_id != expected:
            raise ValueError("plan_id does not match frozen v3 plan payload")
        return self


class ResearchCollectionPreflightReportV3(StrictModel):
    """Deterministic model-free report for an arbitrary-size plan."""

    schema_version: Literal[3] = 3
    report_kind: Literal["lf021_local_research_collection_preflight_v3"]
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
    tranche_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
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
            raise ValueError("collection v3 preflight requires every model-free check")
        if self.execution_ready == bool(self.blocking_prerequisites):
            raise ValueError("execution readiness and blocking prerequisites disagree")
        if len(self.invocation_ids) != self.planned_candidate_count:
            raise ValueError("preflight invocation count does not reconcile")
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if self.planned_candidate_count != expected:
            raise ValueError("preflight count differs from pool x family seeds")
        if len(self.family_binding_hashes) != self.family_count:
            raise ValueError("preflight family binding count does not reconcile")
        return self


class ResearchCollectionManifestV3(StrictModel):
    """Complete terminal accounting for one v3 plan."""

    schema_version: Literal[3] = 3
    manifest_id: str = Field(pattern=_MANIFEST_ID)
    plan_id: str = Field(pattern=_PLAN_ID)
    plan_hash: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
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
        if self.expected_candidate_count != expected:
            raise ValueError("manifest expected count differs from pool x family seeds")
        if self.expected_candidate_count != self.terminal_candidate_count:
            raise ValueError("complete manifest requires one terminal per invocation")
        if sum(self.status_counts.values()) != self.terminal_candidate_count:
            raise ValueError("manifest status counts do not reconcile")
        if len(self.terminal_artifact_hashes) != self.terminal_candidate_count:
            raise ValueError("manifest terminal hashes do not reconcile")
        for mapping_name in (
            "terminal_artifact_hashes",
            "family_session_artifact_hashes",
        ):
            mapping = getattr(self, mapping_name)
            if list(mapping) != sorted(mapping):
                raise ValueError(f"{mapping_name} must be sorted")
            if any(re.fullmatch(_HEX64, digest) is None for digest in mapping.values()):
                raise ValueError(f"{mapping_name} values must be SHA-256")
        expected_id = "research_collection_manifest_v3:" + hash_canonical(
            {"schema": "lf021_research_collection_manifest_v3", **self.id_payload()}
        )
        if self.manifest_id != expected_id:
            raise ValueError("manifest_id does not match v3 manifest payload")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedResearchFamilyActivationV3:
    """Replayed smoke qualification plus exact scalable-pool overlap evidence."""

    qualification: _v1.VerifiedResearchFamilyActivation
    overlap_record: ResearchFamilyOverlapRecordV3


@dataclass(frozen=True, slots=True)
class LoadedResearchCollectionV3:
    config: LoadedConfig[ResearchCollectionV3Config]
    problems: tuple[ProblemPoolRecord, ...]
    context: ContextRecord
    source_matrix: object
    pool_evidence: ScalableProblemPoolEvidence
    qualifications: Mapping[str, LoadedConfig[LocalQualificationConfig]]
    activation_evidence: Mapping[str, VerifiedResearchFamilyActivationV3]
    plan: ResearchCollectionPlanV3
    preflight: ResearchCollectionPreflightReportV3


@dataclass(frozen=True, slots=True)
class ResearchCollectionRunV3:
    output_directory: Path
    plan_path: Path
    manifest_path: Path
    manifest: ResearchCollectionManifestV3
    terminals: tuple[_v1.ResearchCollectionTerminal, ...]


class ResearchInvocationExecutorV3(Protocol):
    """Structural executor contract shared with the audited local HF executor."""

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


def _load_canonical_mapping(path: Path) -> Mapping[str, object]:
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchCollectionV3Error(f"invalid JSON manifest: {path}") from exc
    if not isinstance(document, dict):
        raise ResearchCollectionV3Error("problem-pool manifest must be a mapping")
    if raw != canonical_json_bytes(document) + b"\n":
        raise ResearchCollectionV3Error("problem-pool manifest is not canonical JSON")
    return cast(Mapping[str, object], document)


def _manifest_binding(
    document: Mapping[str, object],
    field: str,
) -> ScalablePoolArtifactBinding:
    value = document.get(field)
    if not isinstance(value, dict):
        raise ResearchCollectionV3Error(f"pool manifest lacks artifact binding {field}")
    try:
        path_value = value["path"]
        sha_value = value["sha256"]
    except KeyError as exc:
        raise ResearchCollectionV3Error(
            f"pool manifest artifact binding {field} is incomplete"
        ) from exc
    try:
        return ScalablePoolArtifactBinding(path=path_value, sha256=sha_value)
    except ValueError as exc:
        raise ResearchCollectionV3Error(
            f"pool manifest artifact binding {field} is invalid"
        ) from exc


def _nested_manifest_binding(
    document: Mapping[str, object],
    section: str,
    field: str,
) -> ScalablePoolArtifactBinding:
    value = document.get(section)
    if not isinstance(value, dict):
        raise ResearchCollectionV3Error(f"pool manifest lacks artifact section {section}")
    try:
        return _manifest_binding(cast(Mapping[str, object], value), field)
    except ResearchCollectionV3Error as exc:
        raise ResearchCollectionV3Error(
            f"pool manifest {section}.{field} binding is invalid"
        ) from exc


def _string_tuple(
    document: Mapping[str, object],
    field: str,
    *,
    expected_count: int,
    sorted_required: bool,
) -> tuple[str, ...]:
    value = document.get(field)
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != expected_count
    ):
        raise ResearchCollectionV3Error(
            f"pool manifest {field} must contain {expected_count} strings"
        )
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ResearchCollectionV3Error(f"pool manifest {field} must be unique")
    if sorted_required and result != tuple(sorted(result)):
        raise ResearchCollectionV3Error(f"pool manifest {field} must be sorted")
    return result


def _manifest_false(document: Mapping[str, object], field: str) -> None:
    if document.get(field) is not False:
        raise ResearchCollectionV3Error(f"pool manifest must set {field}=false")


def cross_domain_pool_source_evidence_sha256(
    document: Mapping[str, object],
) -> str:
    """Hash exact cross-domain source, curation, and screening bindings."""

    bindings: dict[str, object] = {
        f"input_artifacts.{field}": _nested_manifest_binding(
            document,
            "input_artifacts",
            field,
        ).model_dump(mode="json")
        for field in (
            "selected_candidates",
            "source_config",
            "pool_config",
            "public_replication_profile",
        )
    }
    bindings.update(
        {
            f"output_artifacts.{field}": _nested_manifest_binding(
                document,
                "output_artifacts",
                field,
            ).model_dump(mode="json")
            for field in (
                "curation_decisions",
                "no_sorry_reference_checks",
                "record_audits",
            )
        }
    )
    domain_proxy_counts = document.get("domain_proxy_counts")
    if not isinstance(domain_proxy_counts, dict):
        raise ResearchCollectionV3Error("pool manifest lacks domain-proxy counts")
    return hash_canonical(
        {
            "schema": "lf021_cross_domain_pool_source_evidence_v3",
            "bindings": dict(sorted(bindings.items())),
            "domain_proxy_counts": domain_proxy_counts,
            "domain_proxy_method": document.get("domain_proxy_method"),
            "source": document.get("source"),
            "source_revision": document.get("source_revision"),
        }
    )


def _resolve_pool_binding(
    repo_root: Path,
    binding: ScalablePoolArtifactBinding,
) -> Path:
    path = Path(binding.path)
    if not path.is_absolute():
        path = repo_root / path
    resolved = path.resolve()
    if not Path(binding.path).is_absolute():
        try:
            resolved.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise ResearchCollectionV3Error(
                f"pool artifact escapes repository: {binding.path}"
            ) from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise ResearchCollectionV3Error(f"pool artifact is missing: {binding.path}")
    observed = hash_file(resolved)
    if observed != binding.sha256:
        raise ResearchCollectionV3Error(f"pool artifact hash mismatch: {binding.path}")
    return resolved


def _validate_problem_pool_manifest(
    *,
    document: Mapping[str, object],
    contract: ScalableProblemPoolContract,
    config: ResearchCollectionV3Config,
    problems: tuple[ProblemPoolRecord, ...],
) -> ScalableProblemPoolEvidence:
    if document.get("schema_version") != contract.manifest_schema_version:
        raise ResearchCollectionV3Error("pool manifest schema differs from v3 contract")
    if document.get("artifact_kind") != contract.manifest_artifact_kind:
        raise ResearchCollectionV3Error("pool manifest kind differs from v3 contract")
    if document.get("problem_count") != contract.expected_problem_count:
        raise ResearchCollectionV3Error("pool manifest problem count differs from v3 contract")
    if len(problems) != contract.expected_problem_count:
        raise ResearchCollectionV3Error("problem records differ from configured problem count")

    if contract.pool_dialect == "gate3_algebra_operational_v1":
        try:
            evidence = _v2._validate_problem_pool_manifest(
                document=document,
                contract=_v2.ScalableProblemPoolContract(
                    expected_problem_count=contract.expected_problem_count
                ),
                config=cast(_v2.ResearchCollectionV2Config, config),
                problems=problems,
            )
        except _v2.ResearchCollectionV2Error as exc:
            raise ResearchCollectionV3Error(
                "gate3 Algebra pool failed its closed v2 dialect validation"
            ) from exc
        if any(problem.source != "mathlib_gate3_docstrings_operational_v1" for problem in problems):
            raise ResearchCollectionV3Error(
                "gate3 Algebra dialect contains a different problem source"
            )
        return ScalableProblemPoolEvidence(
            schema_version=evidence.schema_version,
            artifact_kind=evidence.artifact_kind,
            problem_count=evidence.problem_count,
            problem_record_ids=evidence.problem_record_ids,
            problem_groups=evidence.problem_groups,
            declaration_full_names=evidence.declaration_full_names,
            active_benchmark_manifest_sha256=evidence.active_benchmark_manifest_sha256,
            active_benchmark_registry_sha256=evidence.active_benchmark_registry_sha256,
            public_source_evidence_sha256=evidence.public_source_evidence_sha256,
            critical_artifact_bindings=tuple(
                ScalablePoolArtifactBinding.model_validate(binding.model_dump(mode="json"))
                for binding in evidence.critical_artifact_bindings
            ),
            raw_document=document,
        )

    problem_record_ids = _string_tuple(
        document,
        "problem_record_ids",
        expected_count=contract.expected_problem_count,
        sorted_required=True,
    )
    problem_groups = _string_tuple(
        document,
        "problem_groups",
        expected_count=contract.expected_problem_count,
        sorted_required=False,
    )
    declaration_names = _string_tuple(
        document,
        "declaration_full_names",
        expected_count=contract.expected_problem_count,
        sorted_required=False,
    )
    actual_record_ids = tuple(problem.problem_record_id for problem in problems)
    actual_groups = tuple(problem.problem_group for problem in problems)
    if problem_record_ids != actual_record_ids:
        raise ResearchCollectionV3Error("pool manifest IDs differ from problem records")
    if problem_groups != actual_groups:
        raise ResearchCollectionV3Error("pool manifest groups differ from problem records")
    record_declarations = tuple(
        problem.metadata.get("source_declaration_full_name") for problem in problems
    )
    if any(not isinstance(name, str) or not name for name in record_declarations):
        raise ResearchCollectionV3Error(
            "a scalable problem lacks source_declaration_full_name metadata"
        )
    if set(declaration_names) != set(record_declarations):
        raise ResearchCollectionV3Error("pool manifest declarations differ from problem metadata")

    if (
        document.get("source") != "mathlib_cross_domain_docstrings_operational_v1"
        or document.get("cross_domain_proxy_coverage_established") is not True
        or document.get("domain_proxy_method") != "mathlib_source_path_first_segment_v1"
        or document.get("domain_proxy_is_semantic_gold") is not False
        or document.get("semantic_domain_gold_created") is not False
    ):
        raise ResearchCollectionV3Error(
            "pool manifest is not the truthful cross-domain operational source"
        )
    domain_proxy_counts = document.get("domain_proxy_counts")
    if (
        not isinstance(domain_proxy_counts, dict)
        or set(domain_proxy_counts)
        != {
            "Analysis",
            "Combinatorics",
            "Geometry",
            "NumberTheory",
            "Probability",
            "Topology",
        }
        or any(not isinstance(count, int) or count < 1 for count in domain_proxy_counts.values())
        or sum(cast(dict[str, int], domain_proxy_counts).values())
        != contract.expected_problem_count
    ):
        raise ResearchCollectionV3Error("cross-domain proxy counts do not reconcile")

    records_binding = _nested_manifest_binding(
        document,
        "output_artifacts",
        "problem_pool_records",
    )
    context_binding = _nested_manifest_binding(document, "output_artifacts", "context")
    header_binding = _nested_manifest_binding(document, "input_artifacts", "import_header")
    if (
        records_binding.path != config.problem_pool_records.artifact
        or records_binding.sha256 != config.problem_pool_records.sha256
    ):
        raise ResearchCollectionV3Error("pool manifest records binding differs from config")
    if (
        context_binding.path != config.context.artifact
        or context_binding.sha256 != config.context.sha256
    ):
        raise ResearchCollectionV3Error("pool manifest context binding differs from config")
    if (
        header_binding.path != config.import_header.artifact
        or header_binding.sha256 != config.import_header.sha256
    ):
        raise ResearchCollectionV3Error("pool manifest header binding differs from config")

    benchmark_manifest = _nested_manifest_binding(
        document,
        "input_artifacts",
        "active_benchmark_manifest",
    )
    benchmark_registry = _nested_manifest_binding(
        document,
        "input_artifacts",
        "active_benchmark_registry",
    )
    registry_sha = document.get("active_benchmark_registry_sha256")
    registry_content_hash = document.get("active_benchmark_registry_content_hash")
    if (
        not isinstance(registry_sha, str)
        or re.fullmatch(_HEX64, registry_sha) is None
        or not isinstance(registry_content_hash, str)
        or re.fullmatch(_HEX64, registry_content_hash) is None
        or benchmark_registry.sha256 != registry_sha
    ):
        raise ResearchCollectionV3Error("pool manifest lacks exact active-registry evidence")
    source_evidence_records: dict[str, ScalablePoolArtifactBinding] = {
        f"input_artifacts.{field}": _nested_manifest_binding(
            document,
            "input_artifacts",
            field,
        )
        for field in (
            "selected_candidates",
            "source_config",
            "pool_config",
            "public_replication_profile",
        )
    }
    source_evidence_records.update(
        {
            f"output_artifacts.{field}": _nested_manifest_binding(
                document,
                "output_artifacts",
                field,
            )
            for field in (
                "curation_decisions",
                "no_sorry_reference_checks",
                "record_audits",
            )
        }
    )
    public_source_evidence_sha256 = cross_domain_pool_source_evidence_sha256(document)

    if (
        contract.require_local_model_authorization
        and document.get("model_collection_authorized_count") != contract.expected_problem_count
    ):
        raise ResearchCollectionV3Error(
            "pool manifest does not authorize local-model-only collection"
        )
    for problem in problems:
        metadata = problem.metadata
        if (
            metadata.get("model_collection_authorized") is not True
            or metadata.get("model_collection_scope") != "local_models_only"
            or metadata.get("external_provider_collection_authorized") is not False
            or metadata.get("reference_visible_to_generator") is not False
            or metadata.get("cross_domain_proxy_coverage_established") is not True
            or metadata.get("domain_proxy_is_semantic_gold") is not False
            or metadata.get("semantic_domain_gold_created") is not False
            or metadata.get("semantic_gold_created") is not False
            or metadata.get("gate_claimed") is not False
            or problem.source != "mathlib_cross_domain_docstrings_operational_v1"
        ):
            raise ResearchCollectionV3Error(
                "problem-level local-only/no-label/no-gate policy is inconsistent"
            )
    if contract.require_reference_hidden:
        _manifest_false(document, "reference_visible_to_generator")
    if contract.require_no_semantic_labels:
        _manifest_false(document, "semantic_labels_created")
        _manifest_false(document, "semantic_domain_gold_created")
    if contract.require_no_gate_claims:
        gate_fields = (
            "gate_claimed",
            "gate_5g_credit_claimed",
            "gate_5_closed",
        )
        present = tuple(field for field in gate_fields if field in document)
        if not present:
            raise ResearchCollectionV3Error("pool manifest lacks an explicit no-gate claim")
        for field in present:
            _manifest_false(document, field)
    if document.get("generator_collection_plan_created") is not False:
        raise ResearchCollectionV3Error("pool manifest must not claim a collection plan")
    _manifest_false(document, "model_execution_performed")

    return ScalableProblemPoolEvidence(
        schema_version=contract.manifest_schema_version,
        artifact_kind=contract.manifest_artifact_kind,
        problem_count=contract.expected_problem_count,
        problem_record_ids=problem_record_ids,
        problem_groups=problem_groups,
        declaration_full_names=declaration_names,
        active_benchmark_manifest_sha256=benchmark_manifest.sha256,
        active_benchmark_registry_sha256=registry_sha,
        public_source_evidence_sha256=public_source_evidence_sha256,
        critical_artifact_bindings=(
            records_binding,
            context_binding,
            header_binding,
            benchmark_manifest,
            benchmark_registry,
            *tuple(source_evidence_records.values()),
        ),
        raw_document=document,
    )


def _load_problem_records_v3(path: Path) -> tuple[ProblemPoolRecord, ...]:
    records: list[ProblemPoolRecord] = []
    try:
        for line in path.read_bytes().splitlines():
            if not line:
                raise ValueError("blank JSONL row")
            records.append(ProblemPoolRecord.model_validate_json(line))
    except (OSError, ValueError) as exc:
        raise ResearchCollectionV3Error(f"invalid cross-domain problem JSONL: {path}") from exc
    if not records:
        raise ResearchCollectionV3Error("cross-domain problem pool cannot be empty")
    ordered = tuple(sorted(records, key=lambda item: item.problem_record_id))
    if tuple(records) != ordered:
        raise ResearchCollectionV3Error("scalable problem records must be sorted")
    if len({item.problem_record_id for item in records}) != len(records):
        raise ResearchCollectionV3Error("scalable problem record IDs must be unique")
    if len({item.problem_id for item in records}) != len(records):
        raise ResearchCollectionV3Error("scalable problem IDs must be unique")
    if len({item.problem_group for item in records}) != len(records):
        raise ResearchCollectionV3Error("scalable problem groups must be unique")
    for record in records:
        if (
            record.schema_version != 2
            or record.eligibility != "eligible"
            or record.private_source_content
            or not record.denylist_checked
            or record.denylist_hits
            or record.source_config_sha256 is None
            or record.source_authorization_hash is None
            or record.source_license is None
        ):
            raise ResearchCollectionV3Error(
                "v3 collection requires public, eligible, source-bound, denylist-clear records"
            )
    return tuple(records)


def _verify_activation_v3(
    *,
    family: _v1.ResearchCollectionFamily,
    qualification: LoadedConfig[LocalQualificationConfig],
    config: ResearchCollectionV3Config,
    pool_evidence: ScalableProblemPoolEvidence,
    problems: tuple[ProblemPoolRecord, ...],
    repo_root: Path,
) -> VerifiedResearchFamilyActivationV3:
    activation = family.activation
    if (
        activation.status != "ready"
        or activation.qualification_bundle_artifact is None
        or activation.qualification_bundle_sha256 is None
        or activation.overlap_record_artifact is None
        or activation.overlap_record_sha256 is None
    ):
        raise ResearchCollectionV3Error(
            f"ready v3 family lacks complete activation bindings: {family.family_id}"
        )

    qualification_only_activation = _v1.ResearchFamilyActivation(
        status="blocked",
        qualification_bundle_artifact=activation.qualification_bundle_artifact,
        qualification_bundle_sha256=activation.qualification_bundle_sha256,
        blocker="v3 overlap is validated separately",
    )
    qualification_only_family = family.model_copy(
        update={"activation": qualification_only_activation}
    )
    qualification_evidence = _v1._verify_family_activation(
        family=qualification_only_family,
        qualification=qualification,
        config=cast(Any, config),
        pool_manifest=cast(Any, pool_evidence),
        problems=problems,
        repo_root=repo_root,
    )

    overlap_path = _v1._resolve_relative_hash(
        repo_root,
        activation.overlap_record_artifact,
        activation.overlap_record_sha256,
        label="v3 family overlap record",
    )
    overlap = _v1._load_json_record(
        overlap_path,
        ResearchFamilyOverlapRecordV3,
    )
    model = qualification.config.active_model
    expected_ids = tuple(problem.problem_record_id for problem in problems)
    expected_problem_ids = {problem.problem_record_id: problem.problem_id for problem in problems}
    introductions = overlap.source_introductions
    if (
        overlap.family_id != family.family_id
        or overlap.model_repo_id != model.repo_id
        or overlap.model_revision != model.revision
        or overlap.pinned_readme_sha256 != model.metadata_hashes.readme
        or overlap.problem_pool_records_sha256 != config.problem_pool_records.sha256
        or overlap.problem_pool_manifest_sha256 != config.problem_pool_manifest.sha256
        or overlap.active_benchmark_manifest_sha256
        != pool_evidence.active_benchmark_manifest_sha256
        or overlap.active_benchmark_registry_sha256
        != pool_evidence.active_benchmark_registry_sha256
        or overlap.public_source_evidence_sha256 != pool_evidence.public_source_evidence_sha256
        or overlap.problem_count != len(problems)
        or tuple(item.problem_record_id for item in introductions) != expected_ids
        or any(
            expected_problem_ids[item.problem_record_id] != item.problem_id
            for item in introductions
        )
    ):
        raise ResearchCollectionV3Error(
            f"v3 overlap differs from exact family/pool bindings: {family.family_id}"
        )
    return VerifiedResearchFamilyActivationV3(
        qualification=qualification_evidence,
        overlap_record=overlap,
    )


def _make_invocations(
    *,
    config_hash: str,
    config: ResearchCollectionV3Config,
    family_bindings: tuple[_v1.ResearchFamilyBinding, ...],
    qualifications: Mapping[str, LoadedConfig[LocalQualificationConfig]],
    problems: tuple[ProblemPoolRecord, ...],
    repo_root: Path,
    context: ContextRecord,
    header_text: str,
) -> tuple[_v1.ResearchCollectionInvocation, ...]:
    config_by_family = {family.family_id: family for family in config.families}
    binding_by_family = {binding.family_id: binding for binding in family_bindings}
    invocations = tuple(
        sorted(
            (
                _v1._make_invocation(
                    collection_config_hash=config_hash,
                    family_config=config_by_family[family_id],
                    family=binding_by_family[family_id],
                    qualification=qualifications[family_id].config,
                    problem=problem,
                    seed=seed,
                    repo_root=repo_root,
                    context=context,
                    context_sha256=config.context.sha256,
                    header_text=header_text,
                )
                for family_id in sorted(config_by_family)
                for seed in config_by_family[family_id].seeds
                for problem in problems
            ),
            key=lambda item: item.invocation_id,
        )
    )
    expected_names = tuple(item.expected_declaration_name for item in invocations)
    if len(expected_names) != len(set(expected_names)):
        raise ResearchCollectionV3Error("expected declaration names collide")
    return invocations


def _build_plan(
    *,
    loaded_config: LoadedConfig[ResearchCollectionV3Config],
    config_path: Path,
    config_file_sha256: str,
    repo_root: Path,
    problems: tuple[ProblemPoolRecord, ...],
    family_bindings: tuple[_v1.ResearchFamilyBinding, ...],
    invocations: tuple[_v1.ResearchCollectionInvocation, ...],
) -> ResearchCollectionPlanV3:
    config = loaded_config.config
    seed_counts = {family.family_id: len(family.seeds) for family in config.families}
    payload: dict[str, object] = {
        "schema_version": 3,
        "tranche_id": config.tranche_id,
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
        "runtime_hash": config.runtime.runtime_hash,
        "shared_execution_record_schema": config.shared_execution_record_schema,
        "problem_count": len(problems),
        "family_count": 3,
        "seed_count_by_family": dict(sorted(seed_counts.items())),
        "expected_candidate_count": len(invocations),
        "problem_record_ids": [problem.problem_record_id for problem in problems],
        "family_bindings": [item.model_dump(mode="json") for item in family_bindings],
        "invocations": [item.model_dump(mode="json") for item in invocations],
        "actual_collection_performed": False,
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    plan_id = "research_collection_plan_v3:" + hash_canonical(
        {"schema": "lf021_research_collection_plan_v3", **payload}
    )
    return ResearchCollectionPlanV3.model_validate({"plan_id": plan_id, **payload})


def load_research_collection_v3(
    config_path: Path,
    *,
    repo_root: Path,
) -> LoadedResearchCollectionV3:
    """Load and replay all v3 prerequisites without importing GPU libraries."""

    root = repo_root.resolve()
    loaded = load_config(config_path, ResearchCollectionV3Config)
    config = loaded.config
    bindings = (
        config.problem_pool_records,
        config.problem_pool_manifest,
        config.context,
        config.import_header,
        config.source_matrix,
        config.runtime.runtime_adapter,
        config.runtime.environment_lock,
        config.runtime.orchestration_adapter,
    )
    resolved = {binding.artifact: _v1._resolve_binding(root, binding) for binding in bindings}
    problems = _load_problem_records_v3(resolved[config.problem_pool_records.artifact])
    manifest_document = _load_canonical_mapping(resolved[config.problem_pool_manifest.artifact])
    pool_evidence = _validate_problem_pool_manifest(
        document=manifest_document,
        contract=config.problem_pool_contract,
        config=config,
        problems=problems,
    )
    for binding in pool_evidence.critical_artifact_bindings:
        _resolve_pool_binding(root, binding)

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
        raise ResearchCollectionV3Error("problem, context, and import-header bindings disagree")

    matrix = ScalableResearchSourceMatrixV3.model_validate(
        load_yaml_mapping(resolved[config.source_matrix.artifact])
    )
    if (
        matrix.problem_count != len(problems)
        or matrix.problem_pool_manifest_sha256 != config.problem_pool_manifest.sha256
        or matrix.pool_dialect != config.problem_pool_contract.pool_dialect
    ):
        raise ResearchCollectionV3Error(
            "source matrix differs from the exact scalable problem pool"
        )
    matrix_by_family = {family.family_id: family for family in matrix.families}
    qualifications: dict[str, LoadedConfig[LocalQualificationConfig]] = {}
    bindings_by_family: list[_v1.ResearchFamilyBinding] = []
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
            raise ResearchCollectionV3Error(
                f"family differs from its source-matrix pin: {family.family_id}"
            )
        qualifications[family.family_id] = qualification
        bindings_by_family.append(
            _v1._family_binding(
                family=family,
                loaded=qualification,
                config_file_sha256=hash_file(pin_path),
                runtime=config.runtime,
            )
        )
    family_bindings = tuple(sorted(bindings_by_family, key=lambda item: item.family_id))
    activation_evidence = {
        family.family_id: _verify_activation_v3(
            family=family,
            qualification=qualifications[family.family_id],
            config=config,
            pool_evidence=pool_evidence,
            problems=problems,
            repo_root=root,
        )
        for family in config.families
    }
    invocations = _make_invocations(
        config_hash=loaded.config_hash,
        config=config,
        family_bindings=family_bindings,
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
        family_bindings=family_bindings,
        invocations=invocations,
    )
    blockers = tuple(
        f"{family.family_id}: {family.activation.blocker}"
        for family in config.families
        if family.activation.status == "blocked"
    )
    ready = config.status == "ready" and config.execution_enabled and not blockers
    seed_counts = dict(sorted(plan.seed_count_by_family.items()))
    preflight = ResearchCollectionPreflightReportV3(
        report_kind="lf021_local_research_collection_preflight_v3",
        execution_ready=ready,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        tranche_id=plan.tranche_id,
        problem_count=plan.problem_count,
        seed_count_by_family=seed_counts,
        planned_candidate_count=plan.expected_candidate_count,
        family_binding_hashes={family.family_id: family.binding_hash for family in family_bindings},
        invocation_ids=tuple(item.invocation_id for item in invocations),
        checks={
            "activation_artifacts_fully_replayed": True,
            "cross_domain_20_problem_count_reconciled": True,
            "collection_is_non_smoke": True,
            "context_and_header_bound": True,
            "exact_checkpoint_prompt_parser_runtime_bound": True,
            "exactly_three_unique_families": True,
            "expected_declaration_names_unique": True,
            "problem_pool_is_public_eligible_and_denylist_clear": True,
            "cross_domain_overlap_records_match_exact_pool": True,
            "seeds_are_sorted_unique_and_nonnegative": True,
            "v3_config_plan_outputs_and_orchestrator_bound": True,
            "zero_labels_and_zero_gate_claims": True,
        },
        blocking_prerequisites=blockers,
    )
    return LoadedResearchCollectionV3(
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


def write_preflight_report_v3(
    loaded: LoadedResearchCollectionV3,
    *,
    repo_root: Path,
) -> tuple[Path, str]:
    """Persist or replay the exact model-free v3 preflight."""

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
    suffix = invocation.invocation_id.rsplit(":", 1)[-1]
    return root / "terminals" / f"{suffix}.json"


def execute_research_collection_v3(
    loaded: LoadedResearchCollectionV3,
    *,
    repo_root: Path,
    executor: ResearchInvocationExecutorV3,
    clock: _v1.Clock = lambda: datetime.datetime.now(datetime.UTC),
) -> ResearchCollectionRunV3:
    """Execute or resume exactly one terminal for every frozen v3 invocation."""

    if not loaded.preflight.execution_ready:
        raise ResearchCollectionV3ExecutionBlocked(
            "research collection v3 is blocked: "
            + "; ".join(loaded.preflight.blocking_prerequisites)
        )
    root = repo_root / loaded.config.config.outputs.root / loaded.plan.plan_id.rsplit(":", 1)[-1]
    plan_path = root / "plan.json"
    _v1._write_immutable(
        plan_path,
        _v1._canonical_record_bytes(loaded.plan),
    )
    problem_by_id = {problem.problem_record_id: problem for problem in loaded.problems}
    terminals: list[_v1.ResearchCollectionTerminal] = []
    binding_by_family = {binding.family_id: binding for binding in loaded.plan.family_bindings}
    invocations_by_family = {
        family_id: tuple(
            invocation
            for invocation in loaded.plan.invocations
            if invocation.family_id == family_id
        )
        for family_id in sorted(binding_by_family)
    }
    for family_id, family_invocations in invocations_by_family.items():
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
                raise ResearchCollectionV3ArtifactConflict(
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

        completed_family_ids: list[str] = []
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
                    provider_artifacts_exist = (
                        invocation_directory / "provider_request.json"
                    ).exists() or (invocation_directory / "provider_boundary.json").exists()
                    if provider_artifacts_exist:
                        raise _v1.ResearchCollectionPostBoundaryError(
                            "executor raised after a provider/model-attempt boundary; "
                            "the v3 run remains incomplete"
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
                completed_family_ids.append(invocation.invocation_id)
        finally:
            executor.end_family(
                family=binding,
                completed_invocation_ids=tuple(completed_family_ids),
                family_directory=family_directory,
            )

    terminals = sorted(terminals, key=lambda item: item.invocation_id)
    if len(terminals) != loaded.plan.expected_candidate_count:
        raise ResearchCollectionV3ArtifactConflict(
            "v3 run did not produce one terminal per invocation"
        )
    for terminal in terminals:
        if terminal.family_session_id is None:
            continue
        session_start_path = (
            root
            / "families"
            / terminal.family_id
            / "sessions"
            / terminal.family_session_id.rsplit(":", 1)[-1]
            / "family_session_start.json"
        )
        if not session_start_path.is_file() or hash_file(
            session_start_path
        ) != terminal.artifact_hashes.get("family_session_start"):
            raise ResearchCollectionV3ArtifactConflict(
                "terminal family-session binding is missing or changed"
            )
    status_counts = Counter(terminal.status.value for terminal in terminals)
    successful_families = {
        terminal.family_id
        for terminal in terminals
        if terminal.status is _v1.ResearchTerminalStatus.RAW_COLLECTED
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
    manifest_payload: dict[str, object] = {
        "schema_version": 3,
        "plan_id": loaded.plan.plan_id,
        "plan_hash": loaded.plan.plan_hash,
        "tranche_id": loaded.plan.tranche_id,
        "shared_execution_record_schema": (loaded.plan.shared_execution_record_schema),
        "actual_collection_performed": True,
        "problem_count": loaded.plan.problem_count,
        "family_count": loaded.plan.family_count,
        "seed_count_by_family": dict(sorted(loaded.plan.seed_count_by_family.items())),
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
    manifest_id = "research_collection_manifest_v3:" + hash_canonical(
        {"schema": "lf021_research_collection_manifest_v3", **manifest_payload}
    )
    manifest = ResearchCollectionManifestV3.model_validate(
        {"manifest_id": manifest_id, **manifest_payload}
    )
    manifest_path = root / "manifest.json"
    _v1._write_immutable(
        manifest_path,
        _v1._canonical_record_bytes(manifest),
    )
    return ResearchCollectionRunV3(
        output_directory=root,
        plan_path=plan_path,
        manifest_path=manifest_path,
        manifest=manifest,
        terminals=tuple(terminals),
    )


LocalHFResearchExecutor = _v1.LocalHFResearchExecutor
ResearchCollectionArtifactConflict = _v1.ResearchCollectionArtifactConflict
ResearchCollectionPostBoundaryError = _v1.ResearchCollectionPostBoundaryError
ResearchCollectionTerminal = _v1.ResearchCollectionTerminal
ResearchFamilyBinding = _v1.ResearchFamilyBinding
ResearchTerminalStatus = _v1.ResearchTerminalStatus
make_orchestration_failure_terminal = _v1.make_orchestration_failure_terminal


__all__ = [
    "LoadedResearchCollectionV3",
    "LocalHFResearchExecutor",
    "ResearchCollectionArtifactConflict",
    "ResearchCollectionManifestV3",
    "ResearchCollectionPlanV3",
    "ResearchCollectionPostBoundaryError",
    "ResearchCollectionPreflightReportV3",
    "ResearchCollectionRunV3",
    "ResearchCollectionTerminal",
    "ResearchCollectionV3ArtifactConflict",
    "ResearchCollectionV3Config",
    "ResearchCollectionV3Error",
    "ResearchCollectionV3ExecutionBlocked",
    "ResearchFamilyBinding",
    "ResearchInvocationExecutorV3",
    "ResearchTerminalStatus",
    "ScalableProblemPoolContract",
    "ScalableProblemPoolEvidence",
    "ScalableResearchFamily",
    "ScalableResearchSourceMatrixV3",
    "cross_domain_pool_source_evidence_sha256",
    "execute_research_collection_v3",
    "load_research_collection_v3",
    "make_orchestration_failure_terminal",
    "write_preflight_report_v3",
]
