"""Scalable LF-021 postprocessing for collection-v2 research outputs.

This is a new artifact contract.  It does not modify or reinterpret the
immutable three-by-three postprocess-v1 or postprocess-v2 bundles.  Version 3
accepts the arbitrary positive problem count and per-family seed counts frozen
by :mod:`leanfaith.generation.research_collection_v2`.

The stage:

* replays the exact collection-v2 plan, manifest, terminals, sessions, and raw
  call lineage;
* binds the scalable pool, references, active registry, primary parsers,
  recovery parser, collector implementation, and this implementation;
* applies the already-frozen primary parser and the conservative Lean-backed
  recovery policy independently to every invocation;
* materializes, screens, and stores only unresolved REVIEW records;
* creates no semantic label, training supervision, or Gate-5 claim.

One invocation is the correctness and failure-isolation unit.  A parser or Lean
failure for one theorem cannot erase any sibling result.
"""

from __future__ import annotations

import datetime
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation import research_collection as collection_v1
from leanfaith.generation import research_collection_v2 as collection_v2
from leanfaith.generation import research_postprocess as postprocess_v1
from leanfaith.generation.candidate_screening import (
    CandidateScreeningIndex,
    PriorCandidateIdentity,
    screen_materialized_candidate,
)
from leanfaith.generation.invocation_failure import redact_exception_message
from leanfaith.generation.local_output_adapter import FinalFenceError, LeanExtractedCandidate
from leanfaith.generation.local_output_recovery import (
    RECOVERY_PARSER_ID,
    RecoveryError,
    extract_expected_declaration_with_lean,
    primary_failure_allows_recovery,
    recovery_parser_source_sha256,
)
from leanfaith.generation.real_outputs import (
    CandidateScreeningRecord,
    CandidateScreeningStatus,
    RealOutputMaterializationResult,
    RealOutputOutcomeCode,
    admit_screened_real_output_candidate,
    materialize_real_output_candidate,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.enums import ParseStatus
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

_HEX64 = r"^[0-9a-f]{64}$"
_TERMINAL_ID = r"^research_postprocess_v3_terminal:[0-9a-f]{64}$"
_FAMILY_ID = r"^research_postprocess_v3_family:[0-9a-f]{64}$"
_MANIFEST_ID = r"^research_postprocess_v3_manifest:[0-9a-f]{64}$"
_COLLECTOR_V2_ARTIFACT = "src/leanfaith/generation/research_collection_v2.py"


class ResearchPostprocessV3Error(RuntimeError):
    """A scalable postprocess input, output, or replay invariant failed."""


class RecoveryStatus(StrEnum):
    """Recovery decision for one independently processed invocation."""

    NOT_ATTEMPTED = "not_attempted"
    NOT_NEEDED = "not_needed"
    NOT_ELIGIBLE = "not_eligible"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ResearchPostprocessV3InputBinding(StrictModel):
    """Exact scalable denominator plus all executable and data dependencies."""

    schema_version: Literal[3] = 3
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
    implementation: postprocess_v1.PostprocessArtifactBinding
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
                "schema": "lf021_research_postprocess_input_binding_v3",
                **self.model_dump(mode="json"),
            }
        )

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.problem_record_ids != tuple(sorted(set(self.problem_record_ids))):
            raise ValueError("v3 problem IDs must be sorted and unique")
        if len(self.problem_record_ids) != self.problem_count:
            raise ValueError("v3 problem IDs do not reconcile problem_count")
        if self.invocation_ids != tuple(sorted(set(self.invocation_ids))):
            raise ValueError("v3 invocation IDs must be sorted and unique")
        if len(self.invocation_ids) != self.expected_invocations:
            raise ValueError("v3 invocation IDs do not reconcile expected_invocations")
        if self.family_ids != tuple(sorted(set(self.family_ids))):
            raise ValueError("v3 requires three sorted unique family IDs")
        if list(self.seed_count_by_family) != sorted(self.seed_count_by_family):
            raise ValueError("v3 seed_count_by_family must be sorted")
        if set(self.seed_count_by_family) != set(self.family_ids):
            raise ValueError("v3 seed-count families differ from family IDs")
        if any(count < 1 for count in self.seed_count_by_family.values()):
            raise ValueError("v3 requires at least one seed per family")
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if self.expected_invocations != expected:
            raise ValueError("v3 expected count differs from problem x family seeds")
        if (
            list(self.collection_terminal_artifacts) != sorted(self.collection_terminal_artifacts)
            or len(self.collection_terminal_artifacts) != expected
        ):
            raise ValueError("v3 terminal bindings do not reconcile the denominator")
        if list(self.collection_family_session_artifacts) != sorted(
            self.collection_family_session_artifacts
        ):
            raise ValueError("v3 family-session bindings must be sorted")
        if list(self.raw_collection_artifacts_by_invocation) != list(self.invocation_ids):
            raise ValueError("v3 raw-lineage bindings must cover every invocation in order")
        for invocation_id, artifacts in self.raw_collection_artifacts_by_invocation.items():
            if list(artifacts) != sorted(artifacts):
                raise ValueError(f"v3 raw artifacts are not sorted: {invocation_id}")
            if any(re.fullmatch(_HEX64, digest) is None for digest in artifacts.values()):
                raise ValueError(f"v3 raw artifacts contain a non-SHA: {invocation_id}")
        for field_name in (
            "collection_terminal_artifacts",
            "collection_family_session_artifacts",
        ):
            mapping = getattr(self, field_name)
            if any(re.fullmatch(_HEX64, digest) is None for digest in mapping.values()):
                raise ValueError(f"{field_name} values must be SHA-256")
        if list(self.primary_parser_implementations) != sorted(
            self.primary_parser_implementations
        ) or set(self.primary_parser_implementations) != set(self.family_ids):
            raise ValueError("v3 primary parser bindings must cover every family")
        if list(self.active_registry_artifacts) != sorted(self.active_registry_artifacts):
            raise ValueError("v3 active-registry bindings must be sorted")
        return self


class ResearchPostprocessV3Terminal(StrictModel):
    """One scalable operational outcome with explicit parser provenance."""

    schema_version: Literal[3] = 3
    record_kind: Literal["lf021_research_postprocess_terminal_v3"] = (
        "lf021_research_postprocess_terminal_v3"
    )
    artifact_class: Literal["research"] = "research"
    terminal_id: str = Field(pattern=_TERMINAL_ID)
    input_binding_hash: str = Field(pattern=_HEX64)
    invocation_id: str
    invocation_payload_hash: str = Field(pattern=_HEX64)
    collection_terminal_id: str
    collection_terminal_sha256: str = Field(pattern=_HEX64)
    family_id: str
    problem_record_id: str
    seed: int = Field(ge=0)
    status: postprocess_v1.ResearchPostprocessStatus
    terminal_stage: postprocess_v1.ResearchPostprocessStage
    record_time_basis: datetime.datetime
    primary_parser_id: str
    primary_parser_source_sha256: str = Field(pattern=_HEX64)
    actual_parser_id: str | None
    actual_parser_source_sha256: str | None = Field(default=None, pattern=_HEX64)
    primary_failure_code: str | None
    recovery_status: RecoveryStatus
    recovery_failure_code: str | None = None
    parser_executed: bool
    lean_validation_executed: bool
    screening_executed: bool
    semantic_pool_admitted: bool
    raw_lineage_hashes: dict[str, str]
    output_artifact_hashes: dict[str, str]
    materialization_outcome: str | None = None
    screening_status: str | None = None
    variant_id: str | None = None
    candidate_theorem_id: str | None = None
    representation_id: str | None = None
    screening_id: str | None = None
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
    failure_code: str | None = None
    failure_detail: str | None = None

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "terminal_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.record_time_basis)
        for field_name in ("raw_lineage_hashes", "output_artifact_hashes"):
            values = getattr(self, field_name)
            if list(values) != sorted(values):
                raise ValueError(f"{field_name} must be sorted")
            if any(re.fullmatch(_HEX64, value) is None for value in values.values()):
                raise ValueError(f"{field_name} values must be SHA-256")
        if (self.actual_parser_id is None) != (self.actual_parser_source_sha256 is None):
            raise ValueError("actual parser ID/hash must be present together")
        if self.recovery_status is RecoveryStatus.NOT_NEEDED:
            if (
                self.actual_parser_id != self.primary_parser_id
                or self.actual_parser_source_sha256 != self.primary_parser_source_sha256
                or self.primary_failure_code is not None
                or self.recovery_failure_code is not None
            ):
                raise ValueError("not-needed recovery requires primary-parser provenance")
        elif self.recovery_status is RecoveryStatus.SUCCEEDED:
            if (
                self.actual_parser_id != RECOVERY_PARSER_ID
                or self.primary_failure_code is None
                or self.recovery_failure_code is not None
            ):
                raise ValueError("successful recovery provenance is incomplete")
        elif self.recovery_status is RecoveryStatus.FAILED:
            if (
                self.actual_parser_id != RECOVERY_PARSER_ID
                or self.primary_failure_code is None
                or self.recovery_failure_code is None
            ):
                raise ValueError("failed recovery provenance is incomplete")
        elif self.recovery_status is RecoveryStatus.NOT_ELIGIBLE:
            if (
                self.primary_failure_code is None
                or self.actual_parser_id is not None
                or self.recovery_failure_code is not None
            ):
                raise ValueError("ineligible recovery cannot claim recovery execution")
        elif self.recovery_status is RecoveryStatus.NOT_ATTEMPTED and (
            self.terminal_stage
            not in {
                postprocess_v1.ResearchPostprocessStage.COLLECTION,
                postprocess_v1.ResearchPostprocessStage.RAW_LINEAGE,
            }
            or self.primary_failure_code is not None
            or self.actual_parser_id is not None
            or self.recovery_failure_code is not None
            or self.parser_executed
        ):
            raise ValueError("not-attempted recovery is only valid before parsing")

        admitted = self.status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
        if self.semantic_pool_admitted != admitted:
            raise ValueError("semantic_pool_admitted differs from terminal status")
        if admitted:
            required = (
                self.variant_id,
                self.candidate_theorem_id,
                self.representation_id,
                self.screening_id,
                self.nl_lean_id,
            )
            if any(value is None for value in required) or not self.pair_ids:
                raise ValueError("admitted v3 terminal lacks semantic-pool IDs")
            if (
                self.resolution_outcome != "unresolved"
                or self.quality_tier != "unknown"
                or not self.requires_adjudication
                or self.decision != "REVIEW"
            ):
                raise ValueError("admitted v3 records must remain unresolved REVIEW")
            if self.failure_code is not None or self.failure_detail is not None:
                raise ValueError("admitted v3 terminal cannot carry a failure")
            if not (
                self.parser_executed and self.lean_validation_executed and self.screening_executed
            ):
                raise ValueError("v3 admission requires parser, Lean, and screening")
        else:
            if (
                self.resolution_outcome is not None
                or self.quality_tier is not None
                or self.requires_adjudication
                or self.decision is not None
                or self.pair_ids
                or self.nl_lean_id is not None
            ):
                raise ValueError("non-admitted v3 outcomes cannot create semantic records")
            if self.failure_code is None or self.failure_detail is None:
                raise ValueError("non-admitted v3 terminal requires an operational reason")
        expected = "research_postprocess_v3_terminal:" + hash_canonical(
            {"schema": "lf021_research_postprocess_terminal_v3", **self.id_payload()}
        )
        if self.terminal_id != expected:
            raise ValueError("v3 terminal ID does not match payload")
        return self


class ResearchPostprocessV3FamilyReport(StrictModel):
    """Dynamic per-family accounting."""

    schema_version: Literal[3] = 3
    report_id: str = Field(pattern=_FAMILY_ID)
    input_binding_hash: str = Field(pattern=_HEX64)
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
            raise ValueError("v3 family denominator differs from problems x seeds")
        if sum(self.status_counts.values()) != expected:
            raise ValueError("v3 family status counts do not reconcile")
        if sum(self.recovery_status_counts.values()) != expected:
            raise ValueError("v3 family recovery counts do not reconcile")
        for count in (
            self.collection_raw_count,
            self.parser_success_count,
            self.admitted_unresolved_count,
        ):
            if count > expected:
                raise ValueError("v3 family stage count exceeds denominator")
        identity = "research_postprocess_v3_family:" + hash_canonical(
            {"schema": "lf021_research_postprocess_family_v3", **self.id_payload()}
        )
        if self.report_id != identity:
            raise ValueError("v3 family report ID does not match payload")
        return self


class ResearchPostprocessV3Manifest(StrictModel):
    """Complete scalable accounting and immutable artifact index."""

    schema_version: Literal[3] = 3
    manifest_id: str = Field(pattern=_MANIFEST_ID)
    input_binding: ResearchPostprocessV3InputBinding
    input_binding_hash: str = Field(pattern=_HEX64)
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
        if self.input_binding_hash != self.input_binding.binding_hash:
            raise ValueError("v3 manifest input binding hash differs")
        if (
            self.problem_count != self.input_binding.problem_count
            or self.seed_count_by_family != self.input_binding.seed_count_by_family
            or self.expected_invocations != self.input_binding.expected_invocations
            or self.terminal_invocations != self.expected_invocations
        ):
            raise ValueError("v3 manifest denominator differs from input binding")
        if sum(self.status_counts.values()) != self.expected_invocations:
            raise ValueError("v3 manifest status counts do not reconcile")
        if sum(self.recovery_status_counts.values()) != self.expected_invocations:
            raise ValueError("v3 manifest recovery counts do not reconcile")
        if (
            len(self.terminal_artifacts) != self.expected_invocations
            or len(self.family_report_artifacts) != self.family_count
        ):
            raise ValueError("v3 manifest artifact denominator differs")
        for field_name in ("terminal_artifacts", "family_report_artifacts"):
            values = getattr(self, field_name)
            if list(values) != sorted(values):
                raise ValueError(f"{field_name} must be sorted")
            if any(re.fullmatch(_HEX64, value) is None for value in values.values()):
                raise ValueError(f"{field_name} values must be SHA-256")
        if self.admitted_nl_lean_count > self.expected_invocations:
            raise ValueError("v3 admitted NL-Lean count exceeds denominator")
        identity = "research_postprocess_v3_manifest:" + hash_canonical(
            {"schema": "lf021_research_postprocess_manifest_v3", **self.id_payload()}
        )
        if self.manifest_id != identity:
            raise ValueError("v3 manifest ID does not match payload")
        return self


@dataclass(frozen=True, slots=True)
class _PostprocessBase:
    """Duck-compatible subset used by audited v1 helper functions."""

    repo_root: Path
    collection_root: Path
    output_root: Path
    plan: collection_v2.ResearchCollectionPlanV2
    manifest: collection_v2.ResearchCollectionManifestV2
    invocations: tuple[collection_v1.ResearchCollectionInvocation, ...]
    collection_terminals: dict[str, collection_v1.ResearchCollectionTerminal]
    collection_terminal_paths: dict[str, Path]
    problems: dict[str, ProblemPoolRecord]
    context: ContextRecord
    import_header: str
    references: dict[str, TheoremRecord]
    reference_representations: dict[str, RepresentationRecord]
    denylist: Any


@dataclass(frozen=True, slots=True)
class LoadedResearchPostprocessV3:
    base: _PostprocessBase
    input_binding: ResearchPostprocessV3InputBinding


@dataclass(slots=True)
class _PreparedCandidate:
    invocation: collection_v1.ResearchCollectionInvocation
    collection_terminal: collection_v1.ResearchCollectionTerminal
    problem: ProblemPoolRecord
    references: tuple[TheoremRecord, ...]
    parsed_call: LLMCallRecord
    parsed: LeanExtractedCandidate
    materialized: RealOutputMaterializationResult
    raw_lineage_hashes: dict[str, str]
    output_artifact_hashes: dict[str, str]
    primary_failure_code: str | None
    recovery_status: RecoveryStatus
    actual_parser_id: str
    actual_parser_source_sha256: str


@dataclass(frozen=True, slots=True)
class ResearchPostprocessV3Run:
    output_root: Path
    manifest_path: Path
    manifest: ResearchPostprocessV3Manifest
    terminals: tuple[ResearchPostprocessV3Terminal, ...]
    family_reports: tuple[ResearchPostprocessV3FamilyReport, ...]


class _ParsedCandidateRecordV3(StrictModel):
    schema_version: Literal[3] = 3
    invocation_id: str
    primary_parser_id: str
    primary_parser_source_sha256: str = Field(pattern=_HEX64)
    actual_parser_id: str
    actual_parser_source_sha256: str = Field(pattern=_HEX64)
    primary_failure_code: str | None
    recovery_status: Literal["not_needed", "succeeded"]
    raw_output_sha256: str = Field(pattern=_HEX64)
    declaration_kind: str
    declaration_name: str
    statement: str
    statement_sha256: str = Field(pattern=_HEX64)
    lean_status: str
    semantic_label: None = None


def validate_collection_v2_denominator(
    plan: collection_v2.ResearchCollectionPlanV2,
    manifest: collection_v2.ResearchCollectionManifestV2,
) -> None:
    """Fail closed on every dynamic collection-v2 cardinality relationship."""

    if (
        manifest.plan_id != plan.plan_id
        or manifest.plan_hash != plan.plan_hash
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
        raise ResearchPostprocessV3Error("collection-v2 plan/manifest denominator differs")
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
        raise ResearchPostprocessV3Error("collection-v2 plan/manifest policy differs")
    problem_ids = {item.problem_record_id for item in plan.invocations}
    if problem_ids != set(plan.problem_record_ids):
        raise ResearchPostprocessV3Error("collection-v2 invocation problems differ from pool")
    family_ids = tuple(binding.family_id for binding in plan.family_bindings)
    if family_ids != tuple(sorted(set(family_ids))):
        raise ResearchPostprocessV3Error("collection-v2 family bindings are not canonical")
    counts = Counter(item.family_id for item in plan.invocations)
    for family_id, seed_count in plan.seed_count_by_family.items():
        if counts[family_id] != plan.problem_count * seed_count:
            raise ResearchPostprocessV3Error(
                f"collection-v2 invocation count differs for {family_id}"
            )


def _resolve_collection_artifact_map(
    *,
    repo_root: Path,
    collection_root: Path,
    artifacts: dict[str, str],
    label: str,
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for artifact, expected in artifacts.items():
        path = postprocess_v1._resolve_repo_artifact(repo_root, artifact)
        try:
            path.relative_to(collection_root)
        except ValueError as exc:
            raise ResearchPostprocessV3Error(
                f"{label} escapes collection root: {artifact}"
            ) from exc
        if hash_file(path) != expected:
            raise ResearchPostprocessV3Error(f"{label} hash mismatch: {artifact}")
        resolved[artifact] = expected
    return dict(sorted(resolved.items()))


def _load_collection_terminals(
    *,
    repo_root: Path,
    collection_root: Path,
    plan: collection_v2.ResearchCollectionPlanV2,
    manifest: collection_v2.ResearchCollectionManifestV2,
) -> tuple[
    dict[str, collection_v1.ResearchCollectionTerminal],
    dict[str, Path],
    dict[str, str],
]:
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    terminals: dict[str, collection_v1.ResearchCollectionTerminal] = {}
    invocation_by_id = {item.invocation_id: item for item in plan.invocations}
    for invocation_id, invocation in sorted(invocation_by_id.items()):
        path = collection_root / "terminals" / f"{invocation_id.rsplit(':', 1)[-1]}.json"
        artifact = str(path.resolve().relative_to(repo_root))
        expected = manifest.terminal_artifact_hashes.get(artifact)
        if expected is None or hash_file(path) != expected:
            raise ResearchPostprocessV3Error(
                f"collection-v2 terminal missing or changed: {invocation_id}"
            )
        terminal = postprocess_v1._load_canonical(
            path,
            collection_v1.ResearchCollectionTerminal,
        )
        if (
            terminal.invocation_id != invocation_id
            or terminal.invocation_payload_hash
            != hash_canonical(invocation.model_dump(mode="json"))
            or terminal.family_id != invocation.family_id
            or terminal.problem_record_id != invocation.problem_record_id
            or terminal.seed != invocation.seed
        ):
            raise ResearchPostprocessV3Error(
                f"collection-v2 terminal differs from invocation: {invocation_id}"
            )
        paths[invocation_id] = path
        hashes[artifact] = expected
        terminals[invocation_id] = terminal
    if set(hashes) != set(manifest.terminal_artifact_hashes):
        raise ResearchPostprocessV3Error("collection-v2 manifest has unexpected terminals")
    observed = dict(sorted(Counter(item.status.value for item in terminals.values()).items()))
    if observed != manifest.status_counts:
        raise ResearchPostprocessV3Error("collection-v2 terminal counts differ")
    return terminals, paths, dict(sorted(hashes.items()))


def _pool_artifact(
    *,
    repo_root: Path,
    document: dict[str, object],
    field: str,
) -> Path:
    binding = collection_v2._manifest_binding(document, field)
    return collection_v2._resolve_pool_binding(repo_root, binding)


def _repo_binding(
    repo_root: Path,
    path: Path,
) -> postprocess_v1.PostprocessArtifactBinding:
    return postprocess_v1._content_addressed_artifact(repo_root, path)


def load_research_postprocess_v3(
    *,
    repo_root: Path,
    collection_root: Path,
    collection_config_path: Path,
    output_root: Path | None = None,
) -> LoadedResearchPostprocessV3:
    """Load and bind an arbitrary completed collection-v2 run."""

    root = repo_root.resolve()
    collection = collection_root.resolve()
    try:
        collection.relative_to(root)
    except ValueError as exc:
        raise ResearchPostprocessV3Error(
            "collection-v2 root must remain in the repository"
        ) from exc
    config_path = collection_config_path.resolve()
    try:
        config_path.relative_to(root)
    except ValueError as exc:
        raise ResearchPostprocessV3Error(
            "collection-v2 config must remain in the repository"
        ) from exc

    plan_path = collection / "plan.json"
    manifest_path = collection / "manifest.json"
    plan = postprocess_v1._load_canonical(
        plan_path,
        collection_v2.ResearchCollectionPlanV2,
    )
    manifest = postprocess_v1._load_canonical(
        manifest_path,
        collection_v2.ResearchCollectionManifestV2,
    )
    validate_collection_v2_denominator(plan, manifest)

    loaded_collection = collection_v2.load_research_collection_v2(
        config_path,
        repo_root=root,
    )
    if loaded_collection.plan != plan:
        raise ResearchPostprocessV3Error(
            "persisted collection-v2 plan differs from current frozen config replay"
        )
    if (
        plan.collection_config_artifact != str(config_path.relative_to(root))
        or plan.collection_config_file_sha256 != hash_file(config_path)
        or plan.collection_config_hash != loaded_collection.config.config_hash
    ):
        raise ResearchPostprocessV3Error("collection-v2 config binding differs")

    terminals, terminal_paths, terminal_hashes = _load_collection_terminals(
        repo_root=root,
        collection_root=collection,
        plan=plan,
        manifest=manifest,
    )
    session_hashes = _resolve_collection_artifact_map(
        repo_root=root,
        collection_root=collection,
        artifacts=manifest.family_session_artifact_hashes,
        label="collection-v2 family session",
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
            raise ResearchPostprocessV3Error(
                f"collection-v2 family session differs: {terminal.invocation_id}"
            )

    config = loaded_collection.config.config
    problem_path = postprocess_v1._resolve_repo_artifact(
        root,
        config.problem_pool_records.artifact,
    )
    context_path = postprocess_v1._resolve_repo_artifact(root, config.context.artifact)
    header_path = postprocess_v1._resolve_repo_artifact(root, config.import_header.artifact)
    source_matrix_path = postprocess_v1._resolve_repo_artifact(
        root,
        config.source_matrix.artifact,
    )
    pool_manifest_path = postprocess_v1._resolve_repo_artifact(
        root,
        config.problem_pool_manifest.artifact,
    )
    pool_document = dict(collection_v2._load_canonical_mapping(pool_manifest_path))
    reference_theorems_path = _pool_artifact(
        repo_root=root,
        document=pool_document,
        field="reference_theorems_artifact",
    )
    reference_representations_path = _pool_artifact(
        repo_root=root,
        document=pool_document,
        field="reference_representations_artifact",
    )

    problems = {item.problem_record_id: item for item in loaded_collection.problems}
    if tuple(sorted(problems)) != plan.problem_record_ids or len(problems) != plan.problem_count:
        raise ResearchPostprocessV3Error("collection-v2 problems differ from plan")
    context = postprocess_v1._load_canonical(context_path, ContextRecord)
    header = header_path.read_text(encoding="utf-8")
    if (
        context != loaded_collection.context
        or context.header_text != header
        or context.header_hash != hash_file(header_path)
    ):
        raise ResearchPostprocessV3Error("collection-v2 context/header differs")

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
        raise ResearchPostprocessV3Error("scalable pool reference artifacts differ")

    denylist, registry_bindings = postprocess_v1._registry_bindings(root)
    if any(
        problem.denylist_registry_content_hash != denylist.registry_content_hash
        or problem.denylist_manifest_sha256 != denylist.manifest_sha256
        or problem.denylist_active_registry_sha256 != denylist.active_registry_sha256
        for problem in problems.values()
    ):
        raise ResearchPostprocessV3Error("problem-level registry binding differs")

    destination = (
        output_root.resolve()
        if output_root is not None
        else (collection / "postprocess_v3").resolve()
    )
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ResearchPostprocessV3Error(
            "postprocess-v3 output must remain in the repository"
        ) from exc
    base = _PostprocessBase(
        repo_root=root,
        collection_root=collection,
        output_root=destination,
        plan=plan,
        manifest=manifest,
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
        parser_binding = _repo_binding(
            root,
            postprocess_v1._resolve_repo_artifact(
                root,
                family_binding.parser_source_artifact,
            ),
        )
        if parser_binding.sha256 != family_binding.parser_source_sha256:
            raise ResearchPostprocessV3Error(
                f"primary parser hash differs: {family_binding.family_id}"
            )
        parser_bindings[family_binding.family_id] = parser_binding
    collector_path = postprocess_v1._resolve_repo_artifact(
        root,
        _COLLECTOR_V2_ARTIFACT,
    )
    if hash_file(collector_path) != plan.orchestration_adapter_sha256:
        raise ResearchPostprocessV3Error("collection-v2 implementation hash differs")
    binding = ResearchPostprocessV3InputBinding(
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
        reference_representations=_repo_binding(root, reference_representations_path),
        active_registry_artifacts=registry_bindings,
        active_registry_content_hash=denylist.registry_content_hash,
        collector_implementation=_repo_binding(root, collector_path),
        primary_parser_implementations=dict(sorted(parser_bindings.items())),
        recovery_implementation=_repo_binding(
            root,
            Path(__file__).with_name("local_output_recovery.py"),
        ),
        implementation=_repo_binding(root, Path(__file__)),
        problem_count=plan.problem_count,
        seed_count_by_family=dict(sorted(plan.seed_count_by_family.items())),
        expected_invocations=plan.expected_candidate_count,
        problem_record_ids=plan.problem_record_ids,
        invocation_ids=tuple(item.invocation_id for item in plan.invocations),
        family_ids=tuple(binding.family_id for binding in plan.family_bindings),
    )
    return LoadedResearchPostprocessV3(base=base, input_binding=binding)


def _parser_failure_code(exc: BaseException) -> str:
    if isinstance(exc, (FinalFenceError, RecoveryError)):
        return exc.code.value
    return type(exc).__name__


def _parse_with_fallback(
    loaded: LoadedResearchPostprocessV3,
    *,
    invocation: collection_v1.ResearchCollectionInvocation,
    collection_terminal: collection_v1.ResearchCollectionTerminal,
    raw_output: str,
    backend: LeanInteractBackend,
) -> tuple[
    LeanExtractedCandidate | None,
    str | None,
    RecoveryStatus,
    str | None,
    str | None,
    str | None,
]:
    binding = next(
        item for item in loaded.base.plan.family_bindings if item.family_id == invocation.family_id
    )
    primary = postprocess_v1._parser(
        invocation=invocation,
        family_parser_artifact=binding.parser_source_artifact,
        family_parser_sha256=binding.parser_source_sha256,
        repo_root=loaded.base.repo_root,
    )
    try:
        parsed = primary(
            raw_output=raw_output,
            expected_declaration_name=invocation.expected_declaration_name,
            registered_header=loaded.base.import_header,
            problem=loaded.base.problems[invocation.problem_record_id],
            context=loaded.base.context,
            backend=backend,
            created_at=collection_terminal.completed_at,
        )
    except Exception as primary_exc:
        primary_code = _parser_failure_code(primary_exc)
        if not primary_failure_allows_recovery(primary_exc):
            return (
                None,
                primary_code,
                RecoveryStatus.NOT_ELIGIBLE,
                None,
                None,
                str(primary_exc),
            )
        recovery_hash = recovery_parser_source_sha256()
        if recovery_hash != loaded.input_binding.recovery_implementation.sha256:
            raise ResearchPostprocessV3Error(
                "recovery parser changed after the v3 binding was loaded"
            ) from None
        try:
            recovered = extract_expected_declaration_with_lean(
                raw_output=raw_output,
                expected_declaration_name=invocation.expected_declaration_name,
                registered_header=loaded.base.import_header,
                problem=loaded.base.problems[invocation.problem_record_id],
                context=loaded.base.context,
                backend=backend,
                created_at=collection_terminal.completed_at,
            )
        except Exception as recovery_exc:
            return (
                None,
                primary_code,
                RecoveryStatus.FAILED,
                RECOVERY_PARSER_ID,
                recovery_hash,
                str(recovery_exc),
            )
        return (
            recovered,
            primary_code,
            RecoveryStatus.SUCCEEDED,
            RECOVERY_PARSER_ID,
            recovery_hash,
            None,
        )
    return (
        parsed,
        None,
        RecoveryStatus.NOT_NEEDED,
        invocation.parser_id,
        invocation.parser_source_sha256,
        None,
    )


def _terminal(
    *,
    loaded: LoadedResearchPostprocessV3,
    invocation: collection_v1.ResearchCollectionInvocation,
    collection_terminal: collection_v1.ResearchCollectionTerminal,
    status: postprocess_v1.ResearchPostprocessStatus,
    stage: postprocess_v1.ResearchPostprocessStage,
    primary_failure_code: str | None,
    recovery_status: RecoveryStatus,
    actual_parser_id: str | None,
    actual_parser_source_sha256: str | None,
    parser_executed: bool,
    lean_validation_executed: bool,
    screening_executed: bool,
    raw_lineage_hashes: dict[str, str],
    output_artifact_hashes: dict[str, str],
    recovery_failure_code: str | None = None,
    materialized: RealOutputMaterializationResult | None = None,
    screening: CandidateScreeningRecord | None = None,
    admitted: RealOutputMaterializationResult | None = None,
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> ResearchPostprocessV3Terminal:
    final = admitted or materialized
    is_admitted = status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    payload: dict[str, object] = {
        "schema_version": 3,
        "record_kind": "lf021_research_postprocess_terminal_v3",
        "artifact_class": "research",
        "input_binding_hash": loaded.input_binding.binding_hash,
        "invocation_id": invocation.invocation_id,
        "invocation_payload_hash": hash_canonical(invocation.model_dump(mode="json")),
        "collection_terminal_id": collection_terminal.terminal_id,
        "collection_terminal_sha256": hash_file(
            loaded.base.collection_terminal_paths[invocation.invocation_id]
        ),
        "family_id": invocation.family_id,
        "problem_record_id": invocation.problem_record_id,
        "seed": invocation.seed,
        "status": status.value,
        "terminal_stage": stage.value,
        "record_time_basis": collection_terminal.completed_at.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "primary_parser_id": invocation.parser_id,
        "primary_parser_source_sha256": invocation.parser_source_sha256,
        "actual_parser_id": actual_parser_id,
        "actual_parser_source_sha256": actual_parser_source_sha256,
        "primary_failure_code": primary_failure_code,
        "recovery_status": recovery_status.value,
        "recovery_failure_code": recovery_failure_code,
        "parser_executed": parser_executed,
        "lean_validation_executed": lean_validation_executed,
        "screening_executed": screening_executed,
        "semantic_pool_admitted": is_admitted,
        "raw_lineage_hashes": dict(sorted(raw_lineage_hashes.items())),
        "output_artifact_hashes": dict(sorted(output_artifact_hashes.items())),
        "materialization_outcome": final.outcome.outcome.value if final is not None else None,
        "screening_status": screening.status.value if screening is not None else None,
        "variant_id": final.variant.variant_id if final is not None else None,
        "candidate_theorem_id": (
            final.theorem.theorem_id if final is not None and final.theorem is not None else None
        ),
        "representation_id": (
            final.representation.representation_id
            if final is not None and final.representation is not None
            else None
        ),
        "screening_id": screening.screening_id if screening is not None else None,
        "pair_ids": tuple(pair.pair_id for pair in admitted.pairs) if admitted else (),
        "nl_lean_id": (
            admitted.nl_lean.nl_lean_id
            if admitted is not None and admitted.nl_lean is not None
            else None
        ),
        "same_claim": None,
        "relation": None,
        "resolution_outcome": "unresolved" if admitted is not None else None,
        "quality_tier": "unknown" if admitted is not None else None,
        "requires_adjudication": admitted is not None,
        "decision": "REVIEW" if admitted is not None else None,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
        "failure_code": failure_code,
        "failure_detail": failure_detail,
    }
    terminal_id = "research_postprocess_v3_terminal:" + hash_canonical(
        {"schema": "lf021_research_postprocess_terminal_v3", **payload}
    )
    return ResearchPostprocessV3Terminal.model_validate({"terminal_id": terminal_id, **payload})


def _failure_terminal(
    *,
    loaded: LoadedResearchPostprocessV3,
    invocation: collection_v1.ResearchCollectionInvocation,
    collection_terminal: collection_v1.ResearchCollectionTerminal,
    status: postprocess_v1.ResearchPostprocessStatus,
    stage: postprocess_v1.ResearchPostprocessStage,
    code: str,
    detail: str,
    primary_failure_code: str | None,
    recovery_status: RecoveryStatus,
    actual_parser_id: str | None,
    actual_parser_source_sha256: str | None,
    recovery_failure_code: str | None = None,
    raw_lineage_hashes: dict[str, str] | None = None,
    output_artifact_hashes: dict[str, str] | None = None,
    parser_executed: bool = False,
    lean_validation_executed: bool = False,
    materialized: RealOutputMaterializationResult | None = None,
) -> ResearchPostprocessV3Terminal:
    return _terminal(
        loaded=loaded,
        invocation=invocation,
        collection_terminal=collection_terminal,
        status=status,
        stage=stage,
        primary_failure_code=primary_failure_code,
        recovery_status=recovery_status,
        actual_parser_id=actual_parser_id,
        actual_parser_source_sha256=actual_parser_source_sha256,
        recovery_failure_code=recovery_failure_code,
        parser_executed=parser_executed,
        lean_validation_executed=lean_validation_executed,
        screening_executed=False,
        raw_lineage_hashes=raw_lineage_hashes or {},
        output_artifact_hashes=output_artifact_hashes or {},
        materialized=materialized,
        failure_code=code,
        failure_detail=redact_exception_message(detail) or "(no detail)",
    )


def _persist_record(
    loaded: LoadedResearchPostprocessV3,
    invocation_id: str,
    name: str,
    record: StrictModel,
) -> tuple[str, str]:
    return postprocess_v1._persist_record(
        loaded=cast(Any, loaded.base),
        invocation_id=invocation_id,
        name=name,
        record=record,
    )


def _persist_jsonl(
    loaded: LoadedResearchPostprocessV3,
    invocation_id: str,
    name: str,
    records: tuple[StrictModel, ...],
) -> tuple[str, str]:
    return postprocess_v1._persist_jsonl(
        loaded=cast(Any, loaded.base),
        invocation_id=invocation_id,
        name=name,
        records=records,
    )


def _persist_materialization(
    loaded: LoadedResearchPostprocessV3,
    invocation_id: str,
    materialized: RealOutputMaterializationResult,
    prefix: str,
) -> dict[str, str]:
    return postprocess_v1._persist_materialization(
        loaded=cast(Any, loaded.base),
        invocation_id=invocation_id,
        materialized=materialized,
        prefix=prefix,
    )


def _prepare_candidates(
    loaded: LoadedResearchPostprocessV3,
    *,
    backend: LeanInteractBackend,
) -> tuple[list[_PreparedCandidate], dict[str, ResearchPostprocessV3Terminal]]:
    prepared: list[_PreparedCandidate] = []
    terminals: dict[str, ResearchPostprocessV3Terminal] = {}
    base = loaded.base
    for invocation in sorted(base.invocations, key=lambda item: item.invocation_id):
        collection_terminal = base.collection_terminals[invocation.invocation_id]
        if collection_terminal.status is not collection_v1.ResearchTerminalStatus.RAW_COLLECTED:
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=postprocess_v1.ResearchPostprocessStatus.COLLECTION_NOT_RAW,
                stage=postprocess_v1.ResearchPostprocessStage.COLLECTION,
                code=f"collection_{collection_terminal.status.value}",
                detail=collection_terminal.error_detail or "raw output was not collected",
                primary_failure_code=None,
                recovery_status=RecoveryStatus.NOT_ATTEMPTED,
                actual_parser_id=None,
                actual_parser_source_sha256=None,
            )
            continue
        try:
            call, _, raw_output, raw_hashes = postprocess_v1._verify_semantic_raw_lineage(
                cast(Any, base),
                invocation,
                collection_terminal,
            )
            if (
                raw_hashes
                != loaded.input_binding.raw_collection_artifacts_by_invocation[
                    invocation.invocation_id
                ]
            ):
                raise ResearchPostprocessV3Error("raw lineage differs from v3 binding")
        except Exception as exc:
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=postprocess_v1.ResearchPostprocessStatus.RAW_LINEAGE_FAILED,
                stage=postprocess_v1.ResearchPostprocessStage.RAW_LINEAGE,
                code=type(exc).__name__,
                detail=str(exc),
                primary_failure_code=None,
                recovery_status=RecoveryStatus.NOT_ATTEMPTED,
                actual_parser_id=None,
                actual_parser_source_sha256=None,
                raw_lineage_hashes=(
                    loaded.input_binding.raw_collection_artifacts_by_invocation[
                        invocation.invocation_id
                    ]
                ),
            )
            continue

        (
            parsed,
            primary_failure_code,
            recovery_status,
            actual_parser_id,
            actual_parser_hash,
            parse_failure_detail,
        ) = _parse_with_fallback(
            loaded,
            invocation=invocation,
            collection_terminal=collection_terminal,
            raw_output=raw_output,
            backend=backend,
        )
        if parsed is None:
            recovery_failure_code = None
            if recovery_status is RecoveryStatus.FAILED and parse_failure_detail:
                recovery_failure_code = parse_failure_detail.split(":", 1)[0]
            code = recovery_failure_code or primary_failure_code or "parser_failed"
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=postprocess_v1.ResearchPostprocessStatus.PARSE_FAILED,
                stage=postprocess_v1.ResearchPostprocessStage.PARSER,
                code=code,
                detail=parse_failure_detail or code,
                primary_failure_code=primary_failure_code,
                recovery_status=recovery_status,
                actual_parser_id=actual_parser_id,
                actual_parser_source_sha256=actual_parser_hash,
                recovery_failure_code=recovery_failure_code,
                raw_lineage_hashes=raw_hashes,
                parser_executed=True,
            )
            continue
        assert actual_parser_id is not None and actual_parser_hash is not None

        parsed_call = LLMCallRecord.model_validate(
            {
                **call.model_dump(mode="json"),
                "parse_status": ParseStatus.PARSED.value,
                "parsed_output": {"lean_statement": parsed.parsed.statement},
                "supervision_eligible": False,
                "metadata": {
                    **call.metadata,
                    "postprocess_version": 3,
                    "postprocess_primary_parser_id": invocation.parser_id,
                    "postprocess_primary_parser_source_sha256": (invocation.parser_source_sha256),
                    "postprocess_actual_parser_id": actual_parser_id,
                    "postprocess_actual_parser_source_sha256": actual_parser_hash,
                    "postprocess_primary_failure_code": primary_failure_code,
                    "postprocess_recovery_status": recovery_status.value,
                    "semantic_labels_created": False,
                    "supervision_eligible": False,
                    "gate_5g_credit_claimed": False,
                    "gate_5_closed": False,
                },
            }
        )
        candidate_record = _ParsedCandidateRecordV3(
            invocation_id=invocation.invocation_id,
            primary_parser_id=invocation.parser_id,
            primary_parser_source_sha256=invocation.parser_source_sha256,
            actual_parser_id=actual_parser_id,
            actual_parser_source_sha256=actual_parser_hash,
            primary_failure_code=primary_failure_code,
            recovery_status=cast(
                Literal["not_needed", "succeeded"],
                recovery_status.value,
            ),
            raw_output_sha256=cast(str, collection_terminal.raw_output_sha256),
            declaration_kind=parsed.parsed.declaration_kind,
            declaration_name=parsed.parsed.declaration_name,
            statement=parsed.parsed.statement,
            statement_sha256=parsed.parsed.statement_sha256,
            lean_status=parsed.lean_status.value,
        )
        output_hashes: dict[str, str] = {}
        for name, record in (
            ("parsed_call.json", parsed_call),
            ("parsed_candidate.json", candidate_record),
        ):
            path, digest = _persist_record(
                loaded,
                invocation.invocation_id,
                name,
                record,
            )
            output_hashes[path] = digest

        problem = base.problems[invocation.problem_record_id]
        references = tuple(
            base.references[theorem_id] for theorem_id in problem.reference_theorem_ids
        )
        generation_config_hash = hash_canonical(
            {
                "schema": "lf021_research_candidate_generation_v3",
                "plan_id": base.plan.plan_id,
                "invocation": invocation.model_dump(mode="json"),
                "input_binding_hash": loaded.input_binding.binding_hash,
                "primary_parser_id": invocation.parser_id,
                "actual_parser_id": actual_parser_id,
            }
        )
        try:
            materialized = materialize_real_output_candidate(
                problem=problem,
                parsed=parsed.parsed,
                call=parsed_call,
                raw_output_artifact=cast(str, parsed_call.raw_output_artifact),
                context=base.context,
                references=references,
                imports=base.import_header,
                backend=backend,
                generation_config_hash=generation_config_hash,
                created_at=collection_terminal.completed_at,
            )
        except Exception as exc:
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=postprocess_v1.ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                stage=postprocess_v1.ResearchPostprocessStage.MATERIALIZATION,
                code=type(exc).__name__,
                detail=str(exc),
                primary_failure_code=primary_failure_code,
                recovery_status=recovery_status,
                actual_parser_id=actual_parser_id,
                actual_parser_source_sha256=actual_parser_hash,
                raw_lineage_hashes=raw_hashes,
                output_artifact_hashes=output_hashes,
                parser_executed=True,
                lean_validation_executed=True,
            )
            continue
        output_hashes.update(
            _persist_materialization(
                loaded,
                invocation.invocation_id,
                materialized,
                "materialized",
            )
        )
        if materialized.outcome.outcome is not RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING:
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=postprocess_v1.ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                stage=postprocess_v1.ResearchPostprocessStage.MATERIALIZATION,
                code=(
                    materialized.outcome.failure_code.value
                    if materialized.outcome.failure_code is not None
                    else materialized.outcome.outcome.value
                ),
                detail=materialized.outcome.failure_detail or "candidate did not materialize",
                primary_failure_code=primary_failure_code,
                recovery_status=recovery_status,
                actual_parser_id=actual_parser_id,
                actual_parser_source_sha256=actual_parser_hash,
                raw_lineage_hashes=raw_hashes,
                output_artifact_hashes=output_hashes,
                parser_executed=True,
                lean_validation_executed=True,
                materialized=materialized,
            )
            continue
        prepared.append(
            _PreparedCandidate(
                invocation=invocation,
                collection_terminal=collection_terminal,
                problem=problem,
                references=references,
                parsed_call=parsed_call,
                parsed=parsed,
                materialized=materialized,
                raw_lineage_hashes=raw_hashes,
                output_artifact_hashes=output_hashes,
                primary_failure_code=primary_failure_code,
                recovery_status=recovery_status,
                actual_parser_id=actual_parser_id,
                actual_parser_source_sha256=actual_parser_hash,
            )
        )
    return prepared, terminals


def _screen_and_admit(
    loaded: LoadedResearchPostprocessV3,
    *,
    prepared: list[_PreparedCandidate],
    terminals: dict[str, ResearchPostprocessV3Terminal],
) -> None:
    """Screen independently materialized candidates and deduplicate globally."""

    base = loaded.base
    by_alpha: dict[str, list[_PreparedCandidate]] = defaultdict(list)
    for item in prepared:
        representation = item.materialized.representation
        if representation is None or representation.alpha_identity_fingerprint is None:
            terminals[item.invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=item.invocation,
                collection_terminal=item.collection_terminal,
                status=postprocess_v1.ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                stage=postprocess_v1.ResearchPostprocessStage.MATERIALIZATION,
                code="missing_alpha_identity_fingerprint",
                detail="materialized candidate lacks the required deduplication identity",
                primary_failure_code=item.primary_failure_code,
                recovery_status=item.recovery_status,
                actual_parser_id=item.actual_parser_id,
                actual_parser_source_sha256=item.actual_parser_source_sha256,
                raw_lineage_hashes=item.raw_lineage_hashes,
                output_artifact_hashes=item.output_artifact_hashes,
                parser_executed=True,
                lean_validation_executed=True,
                materialized=item.materialized,
            )
            continue
        by_alpha[representation.alpha_identity_fingerprint].append(item)

    identity_rows: list[tuple[str, str, str]] = []
    for item in prepared:
        representation = item.materialized.representation
        theorem = item.materialized.theorem
        if (
            item.invocation.invocation_id in terminals
            or representation is None
            or representation.alpha_identity_fingerprint is None
            or theorem is None
        ):
            continue
        identity_rows.append(
            (
                representation.alpha_identity_fingerprint,
                theorem.theorem_id,
                item.invocation.invocation_id,
            )
        )
    canonical_by_alpha = postprocess_v1._canonical_candidate_keys_by_alpha(tuple(identity_rows))
    for alpha, group in sorted(by_alpha.items()):
        ordered = sorted(
            group,
            key=lambda item: (
                cast(TheoremRecord, item.materialized.theorem).theorem_id,
                item.invocation.invocation_id,
            ),
        )
        canonical_key = canonical_by_alpha[alpha]
        canonical_item = next(
            item
            for item in ordered
            if (
                cast(TheoremRecord, item.materialized.theorem).theorem_id,
                item.invocation.invocation_id,
            )
            == canonical_key
        )
        canonical_theorem = cast(TheoremRecord, canonical_item.materialized.theorem)
        for item in ordered:
            if item.invocation.invocation_id in terminals:
                continue
            theorem = cast(TheoremRecord, item.materialized.theorem)
            representation = cast(RepresentationRecord, item.materialized.representation)
            if (theorem.theorem_id, item.invocation.invocation_id) == canonical_key:
                priors: tuple[PriorCandidateIdentity, ...] = ()
            else:
                priors = (
                    PriorCandidateIdentity(
                        theorem_id=canonical_theorem.theorem_id,
                        alpha_identity_fingerprint=alpha,
                    ),
                )
            screening = screen_materialized_candidate(
                index=CandidateScreeningIndex(
                    denylist=base.denylist,
                    prior_candidates=priors,
                ),
                problem_record_id=item.problem.problem_record_id,
                call_id=item.parsed_call.call_id,
                theorem=theorem,
                representation=representation,
                created_at=item.collection_terminal.completed_at,
            )
            output_hashes = dict(item.output_artifact_hashes)
            path, digest = _persist_record(
                loaded,
                item.invocation.invocation_id,
                "screening.json",
                screening,
            )
            output_hashes[path] = digest
            if screening.status is not CandidateScreeningStatus.CLEAN:
                terminals[item.invocation.invocation_id] = _terminal(
                    loaded=loaded,
                    invocation=item.invocation,
                    collection_terminal=item.collection_terminal,
                    status=postprocess_v1.ResearchPostprocessStatus.SCREEN_REJECTED,
                    stage=postprocess_v1.ResearchPostprocessStage.SCREENING,
                    primary_failure_code=item.primary_failure_code,
                    recovery_status=item.recovery_status,
                    actual_parser_id=item.actual_parser_id,
                    actual_parser_source_sha256=item.actual_parser_source_sha256,
                    parser_executed=True,
                    lean_validation_executed=True,
                    screening_executed=True,
                    raw_lineage_hashes=item.raw_lineage_hashes,
                    output_artifact_hashes=output_hashes,
                    materialized=item.materialized,
                    screening=screening,
                    failure_code="candidate_screen_rejected",
                    failure_detail=(
                        "benchmark_hits="
                        f"{list(screening.benchmark_hits)}; "
                        "duplicate_candidate_theorem_ids="
                        f"{list(screening.duplicate_candidate_theorem_ids)}"
                    ),
                )
                continue
            try:
                admitted = admit_screened_real_output_candidate(
                    materialized=item.materialized,
                    screening=screening,
                    problem=item.problem,
                    references=item.references,
                    expected_frozen_registry_hash=base.denylist.registry_content_hash,
                    created_at=item.collection_terminal.completed_at,
                )
                admitted = postprocess_v1._unresolved_pairs(admitted)
            except Exception as exc:
                terminals[item.invocation.invocation_id] = _terminal(
                    loaded=loaded,
                    invocation=item.invocation,
                    collection_terminal=item.collection_terminal,
                    status=postprocess_v1.ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                    stage=postprocess_v1.ResearchPostprocessStage.ADMISSION,
                    primary_failure_code=item.primary_failure_code,
                    recovery_status=item.recovery_status,
                    actual_parser_id=item.actual_parser_id,
                    actual_parser_source_sha256=item.actual_parser_source_sha256,
                    parser_executed=True,
                    lean_validation_executed=True,
                    screening_executed=True,
                    raw_lineage_hashes=item.raw_lineage_hashes,
                    output_artifact_hashes=output_hashes,
                    materialized=item.materialized,
                    screening=screening,
                    failure_code=type(exc).__name__,
                    failure_detail=redact_exception_message(str(exc)) or "(no detail)",
                )
                continue
            output_hashes.update(
                _persist_materialization(
                    loaded,
                    item.invocation.invocation_id,
                    admitted,
                    "admitted",
                )
            )
            pair_path, pair_digest = _persist_jsonl(
                loaded,
                item.invocation.invocation_id,
                "unresolved_pairs.jsonl",
                cast(tuple[StrictModel, ...], admitted.pairs),
            )
            output_hashes[pair_path] = pair_digest
            assert admitted.nl_lean is not None
            nl_path, nl_digest = _persist_record(
                loaded,
                item.invocation.invocation_id,
                "unresolved_nl_lean.json",
                admitted.nl_lean,
            )
            output_hashes[nl_path] = nl_digest
            terminals[item.invocation.invocation_id] = _terminal(
                loaded=loaded,
                invocation=item.invocation,
                collection_terminal=item.collection_terminal,
                status=postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED,
                stage=postprocess_v1.ResearchPostprocessStage.COMPLETE,
                primary_failure_code=item.primary_failure_code,
                recovery_status=item.recovery_status,
                actual_parser_id=item.actual_parser_id,
                actual_parser_source_sha256=item.actual_parser_source_sha256,
                parser_executed=True,
                lean_validation_executed=True,
                screening_executed=True,
                raw_lineage_hashes=item.raw_lineage_hashes,
                output_artifact_hashes=output_hashes,
                materialized=item.materialized,
                screening=screening,
                admitted=admitted,
            )


def _family_payload(
    loaded: LoadedResearchPostprocessV3,
    *,
    family_id: str,
    selected: tuple[ResearchPostprocessV3Terminal, ...],
) -> dict[str, object]:
    seed_count = loaded.input_binding.seed_count_by_family[family_id]
    expected = loaded.input_binding.problem_count * seed_count
    if len(selected) != expected or any(item.family_id != family_id for item in selected):
        raise ResearchPostprocessV3Error(f"v3 family denominator differs: {family_id}")
    return {
        "schema_version": 3,
        "input_binding_hash": loaded.input_binding.binding_hash,
        "family_id": family_id,
        "problem_count": loaded.input_binding.problem_count,
        "seed_count": seed_count,
        "expected_invocations": expected,
        "terminal_invocations": expected,
        "status_counts": dict(sorted(Counter(item.status.value for item in selected).items())),
        "recovery_status_counts": dict(
            sorted(Counter(item.recovery_status.value for item in selected).items())
        ),
        "collection_raw_count": sum(
            loaded.base.collection_terminals[item.invocation_id].status
            is collection_v1.ResearchTerminalStatus.RAW_COLLECTED
            for item in selected
        ),
        "parser_success_count": sum(
            item.recovery_status in {RecoveryStatus.NOT_NEEDED, RecoveryStatus.SUCCEEDED}
            for item in selected
        ),
        "admitted_unresolved_count": sum(
            item.status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
            for item in selected
        ),
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }


def _output_directory(
    loaded: LoadedResearchPostprocessV3,
    invocation_id: str,
) -> Path:
    return loaded.base.output_root / "invocations" / invocation_id.rsplit(":", 1)[-1]


def _write_terminals_and_reports(
    loaded: LoadedResearchPostprocessV3,
    terminals_by_id: dict[str, ResearchPostprocessV3Terminal],
) -> ResearchPostprocessV3Run:
    expected_ids = set(loaded.input_binding.invocation_ids)
    if set(terminals_by_id) != expected_ids:
        missing = sorted(expected_ids - set(terminals_by_id))
        extra = sorted(set(terminals_by_id) - expected_ids)
        raise ResearchPostprocessV3Error(
            f"v3 postprocess denominator is incomplete: missing={missing}; extra={extra}"
        )
    terminals = tuple(terminals_by_id[key] for key in sorted(terminals_by_id))
    terminal_artifacts: dict[str, str] = {}
    for terminal in terminals:
        path = (
            _output_directory(
                loaded,
                terminal.invocation_id,
            )
            / "processing_terminal.json"
        )
        digest = postprocess_v1._write_immutable(
            path,
            postprocess_v1._canonical_record_bytes(terminal),
        )
        terminal_artifacts[str(path.resolve().relative_to(loaded.base.repo_root))] = digest

    reports: list[ResearchPostprocessV3FamilyReport] = []
    report_artifacts: dict[str, str] = {}
    for family_id in loaded.input_binding.family_ids:
        selected = tuple(item for item in terminals if item.family_id == family_id)
        family_payload = _family_payload(
            loaded,
            family_id=family_id,
            selected=selected,
        )
        report_id = "research_postprocess_v3_family:" + hash_canonical(
            {"schema": "lf021_research_postprocess_family_v3", **family_payload}
        )
        report = ResearchPostprocessV3FamilyReport.model_validate(
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
        "schema_version": 3,
        "input_binding": loaded.input_binding.model_dump(mode="json"),
        "input_binding_hash": loaded.input_binding.binding_hash,
        "problem_count": loaded.input_binding.problem_count,
        "family_count": loaded.input_binding.family_count,
        "seed_count_by_family": loaded.input_binding.seed_count_by_family,
        "expected_invocations": loaded.input_binding.expected_invocations,
        "terminal_invocations": len(terminals),
        "status_counts": dict(sorted(Counter(item.status.value for item in terminals).items())),
        "recovery_status_counts": dict(
            sorted(Counter(item.recovery_status.value for item in terminals).items())
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
    manifest_id = "research_postprocess_v3_manifest:" + hash_canonical(
        {"schema": "lf021_research_postprocess_manifest_v3", **payload}
    )
    manifest = ResearchPostprocessV3Manifest.model_validate({"manifest_id": manifest_id, **payload})
    manifest_path = loaded.base.output_root / "manifest.json"
    postprocess_v1._write_immutable(
        manifest_path,
        postprocess_v1._canonical_record_bytes(manifest),
    )
    return ResearchPostprocessV3Run(
        output_root=loaded.base.output_root,
        manifest_path=manifest_path,
        manifest=manifest,
        terminals=terminals,
        family_reports=tuple(reports),
    )


def run_research_postprocess_v3(
    loaded: LoadedResearchPostprocessV3,
    *,
    backend: LeanInteractBackend,
) -> ResearchPostprocessV3Run:
    """Process every frozen collection-v2 invocation independently."""

    for binding in (
        loaded.input_binding.implementation,
        loaded.input_binding.recovery_implementation,
        loaded.input_binding.collector_implementation,
        *loaded.input_binding.primary_parser_implementations.values(),
    ):
        path = postprocess_v1._resolve_bound_artifact(
            loaded.base.repo_root,
            binding,
        )
        if hash_file(path) != binding.sha256:
            raise ResearchPostprocessV3Error(f"v3 executable binding changed: {binding.artifact}")
    prepared, terminals = _prepare_candidates(loaded, backend=backend)
    _screen_and_admit(loaded, prepared=prepared, terminals=terminals)
    return _write_terminals_and_reports(loaded, terminals)


def _verify_binding(
    repo_root: Path,
    binding: postprocess_v1.PostprocessArtifactBinding,
) -> None:
    path = postprocess_v1._resolve_bound_artifact(repo_root, binding)
    if hash_file(path) != binding.sha256:
        raise ResearchPostprocessV3Error(f"v3 bound artifact hash mismatch: {binding.artifact}")


def verify_research_postprocess_v3(
    loaded: LoadedResearchPostprocessV3,
) -> ResearchPostprocessV3Manifest:
    """Replay-verify a v3 bundle without Lean execution or writes."""

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
            path = postprocess_v1._resolve_repo_artifact(base.repo_root, artifact)
            if hash_file(path) != expected:
                raise ResearchPostprocessV3Error(f"v3 collection input hash mismatch: {artifact}")
    for invocation_id, artifacts in bindings.raw_collection_artifacts_by_invocation.items():
        for artifact, expected in artifacts.items():
            path = postprocess_v1._resolve_repo_artifact(base.repo_root, artifact)
            if hash_file(path) != expected:
                raise ResearchPostprocessV3Error(
                    f"v3 raw input hash mismatch: {invocation_id}: {artifact}"
                )

    manifest = postprocess_v1._load_canonical(
        base.output_root / "manifest.json",
        ResearchPostprocessV3Manifest,
    )
    if manifest.input_binding != loaded.input_binding:
        raise ResearchPostprocessV3Error("persisted v3 input binding has drifted")
    invocation_by_id = {item.invocation_id: item for item in base.invocations}
    terminals: list[ResearchPostprocessV3Terminal] = []
    for artifact, expected in manifest.terminal_artifacts.items():
        path = postprocess_v1._resolve_repo_artifact(base.repo_root, artifact)
        if hash_file(path) != expected:
            raise ResearchPostprocessV3Error(f"v3 terminal hash mismatch: {artifact}")
        terminal = postprocess_v1._load_canonical(
            path,
            ResearchPostprocessV3Terminal,
        )
        expected_path = (
            _output_directory(loaded, terminal.invocation_id) / "processing_terminal.json"
        ).resolve()
        if path != expected_path:
            raise ResearchPostprocessV3Error(f"v3 terminal stored at unexpected path: {artifact}")
        invocation = invocation_by_id.get(terminal.invocation_id)
        collection = base.collection_terminals.get(terminal.invocation_id)
        collection_path = base.collection_terminal_paths.get(terminal.invocation_id)
        if invocation is None or collection is None or collection_path is None:
            raise ResearchPostprocessV3Error("v3 terminal lacks frozen collection lineage")
        if (
            terminal.input_binding_hash != bindings.binding_hash
            or terminal.invocation_payload_hash
            != hash_canonical(invocation.model_dump(mode="json"))
            or terminal.collection_terminal_id != collection.terminal_id
            or terminal.collection_terminal_sha256 != hash_file(collection_path)
            or terminal.primary_parser_id != invocation.parser_id
            or terminal.primary_parser_source_sha256 != invocation.parser_source_sha256
            or terminal.raw_lineage_hashes
            != bindings.raw_collection_artifacts_by_invocation[terminal.invocation_id]
        ):
            raise ResearchPostprocessV3Error(
                f"v3 terminal lineage differs: {terminal.invocation_id}"
            )
        bound = {
            **terminal.raw_lineage_hashes,
            **terminal.output_artifact_hashes,
        }
        if len(bound) != (len(terminal.raw_lineage_hashes) + len(terminal.output_artifact_hashes)):
            raise ResearchPostprocessV3Error(
                f"v3 raw/output path collision: {terminal.invocation_id}"
            )
        for bound_artifact, bound_hash in bound.items():
            resolved = postprocess_v1._resolve_repo_artifact(
                base.repo_root,
                bound_artifact,
            )
            if hash_file(resolved) != bound_hash:
                raise ResearchPostprocessV3Error(f"v3 bound output hash mismatch: {bound_artifact}")
        parsed_candidates = [
            candidate_path
            for candidate_path in terminal.output_artifact_hashes
            if candidate_path.endswith("/parsed_candidate.json")
        ]
        parser_succeeded = terminal.recovery_status in {
            RecoveryStatus.NOT_NEEDED,
            RecoveryStatus.SUCCEEDED,
        }
        if parser_succeeded:
            if len(parsed_candidates) != 1:
                raise ResearchPostprocessV3Error(
                    f"v3 parsed-candidate provenance missing: {terminal.invocation_id}"
                )
            parsed = postprocess_v1._load_canonical(
                postprocess_v1._resolve_repo_artifact(
                    base.repo_root,
                    parsed_candidates[0],
                ),
                _ParsedCandidateRecordV3,
            )
            if (
                parsed.invocation_id != terminal.invocation_id
                or parsed.primary_parser_id != terminal.primary_parser_id
                or parsed.primary_parser_source_sha256 != terminal.primary_parser_source_sha256
                or parsed.actual_parser_id != terminal.actual_parser_id
                or parsed.actual_parser_source_sha256 != terminal.actual_parser_source_sha256
                or parsed.primary_failure_code != terminal.primary_failure_code
                or parsed.recovery_status != terminal.recovery_status.value
            ):
                raise ResearchPostprocessV3Error(
                    f"v3 parser provenance differs: {terminal.invocation_id}"
                )
        elif parsed_candidates:
            raise ResearchPostprocessV3Error(
                f"failed v3 parse persisted a candidate: {terminal.invocation_id}"
            )
        terminals.append(terminal)
    if tuple(sorted(item.invocation_id for item in terminals)) != bindings.invocation_ids:
        raise ResearchPostprocessV3Error("persisted v3 terminal denominator differs")

    status_counts = dict(sorted(Counter(item.status.value for item in terminals).items()))
    recovery_counts = dict(
        sorted(Counter(item.recovery_status.value for item in terminals).items())
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
        raise ResearchPostprocessV3Error("v3 manifest accounting differs")

    reports: dict[str, ResearchPostprocessV3FamilyReport] = {}
    for artifact, expected in manifest.family_report_artifacts.items():
        path = postprocess_v1._resolve_repo_artifact(base.repo_root, artifact)
        if hash_file(path) != expected:
            raise ResearchPostprocessV3Error(f"v3 family report hash mismatch: {artifact}")
        report = postprocess_v1._load_canonical(
            path,
            ResearchPostprocessV3FamilyReport,
        )
        if report.family_id in reports:
            raise ResearchPostprocessV3Error(f"duplicate v3 family report: {report.family_id}")
        expected_path = (base.output_root / "families" / f"{report.family_id}.json").resolve()
        if path != expected_path:
            raise ResearchPostprocessV3Error(
                f"v3 family report stored at unexpected path: {artifact}"
            )
        reports[report.family_id] = report
    if set(reports) != set(bindings.family_ids):
        raise ResearchPostprocessV3Error("v3 family report denominator differs")
    for family_id, report in reports.items():
        expected_payload = _family_payload(
            loaded,
            family_id=family_id,
            selected=tuple(item for item in terminals if item.family_id == family_id),
        )
        if report.model_dump(mode="json", exclude={"report_id"}) != expected_payload:
            raise ResearchPostprocessV3Error(f"v3 family report accounting differs: {family_id}")
    return manifest


__all__ = [
    "LoadedResearchPostprocessV3",
    "RecoveryStatus",
    "ResearchPostprocessV3Error",
    "ResearchPostprocessV3FamilyReport",
    "ResearchPostprocessV3InputBinding",
    "ResearchPostprocessV3Manifest",
    "ResearchPostprocessV3Run",
    "ResearchPostprocessV3Terminal",
    "load_research_postprocess_v3",
    "run_research_postprocess_v3",
    "validate_collection_v2_denominator",
    "verify_research_postprocess_v3",
]
