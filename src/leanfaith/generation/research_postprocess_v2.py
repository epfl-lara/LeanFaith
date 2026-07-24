"""LF-021 research postprocessing v2 with explicit parser recovery.

Version 1 is an immutable, replayable artifact contract.  This module imports
its verified collection loading, lineage checks, materialization helpers, and
screening policy without modifying those bytes.  It adds only:

* a separately hashed recovery parser;
* an explicit fallback after registered *operational* primary-parser errors;
* parser provenance on parsed records and every terminal;
* independently versioned v2 manifests under ``postprocess_v2``.

Recovery never runs after a primary ``lean_invalid`` result.  No outcome from
this module is a semantic label, supervision record, or Gate-5 claim.
"""

from __future__ import annotations

import datetime
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation import research_postprocess as v1
from leanfaith.generation.candidate_screening import (
    CandidateScreeningIndex,
    PriorCandidateIdentity,
    screen_materialized_candidate,
)
from leanfaith.generation.invocation_failure import redact_exception_message
from leanfaith.generation.local_output_adapter import (
    FinalFenceError,
    LeanExtractedCandidate,
)
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
from leanfaith.generation.research_collection import (
    ResearchCollectionInvocation,
    ResearchCollectionTerminal,
    ResearchTerminalStatus,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.enums import ParseStatus
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord

_HEX64 = r"^[0-9a-f]{64}$"
_TERMINAL_ID = r"^research_postprocess_v2_terminal:[0-9a-f]{64}$"
_FAMILY_ID = r"^research_postprocess_v2_family:[0-9a-f]{64}$"
_MANIFEST_ID = r"^research_postprocess_v2_manifest:[0-9a-f]{64}$"


class ResearchPostprocessV2Error(RuntimeError):
    """A v2 input, output, or replay invariant failed."""


class RecoveryStatus(StrEnum):
    """The recovery decision for one invocation."""

    NOT_ATTEMPTED = "not_attempted"
    NOT_NEEDED = "not_needed"
    NOT_ELIGIBLE = "not_eligible"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ResearchPostprocessV2InputBinding(StrictModel):
    """The immutable v1 denominator plus both v2 implementation modules."""

    schema_version: Literal[2] = 2
    primary_binding: v1.ResearchPostprocessInputBinding
    primary_implementation: v1.PostprocessArtifactBinding
    recovery_implementation: v1.PostprocessArtifactBinding
    implementation: v1.PostprocessArtifactBinding
    invocation_ids: tuple[str, ...]
    family_ids: tuple[str, ...]

    @property
    def binding_hash(self) -> str:
        return hash_canonical(
            {
                "schema": "lf021_research_postprocess_input_binding_v2",
                **self.model_dump(mode="json"),
            }
        )

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.primary_implementation != self.primary_binding.implementation:
            raise ValueError("v2 primary implementation must mirror the frozen v1 binding")
        if self.invocation_ids != self.primary_binding.invocation_ids:
            raise ValueError("v2 invocation denominator differs from v1")
        if self.family_ids != self.primary_binding.family_ids:
            raise ValueError("v2 family denominator differs from v1")
        if len(self.invocation_ids) != 9 or len(self.family_ids) != 3:
            raise ValueError("v2 requires the exact 3x3 denominator")
        return self


class ResearchPostprocessV2Terminal(StrictModel):
    """One versioned operational outcome with complete parser provenance."""

    schema_version: Literal[2] = 2
    record_kind: Literal["lf021_research_postprocess_terminal_v2"] = (
        "lf021_research_postprocess_terminal_v2"
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
    status: v1.ResearchPostprocessStatus
    terminal_stage: v1.ResearchPostprocessStage
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
                raise ValueError("not-needed recovery requires a successful primary parser")
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
        elif self.recovery_status is RecoveryStatus.NOT_ELIGIBLE and (
            self.primary_failure_code is None
            or self.actual_parser_id is not None
            or self.recovery_failure_code is not None
        ):
            raise ValueError("ineligible recovery cannot claim parser execution")
        elif self.recovery_status is RecoveryStatus.NOT_ATTEMPTED and (
            self.terminal_stage
            not in {
                v1.ResearchPostprocessStage.COLLECTION,
                v1.ResearchPostprocessStage.RAW_LINEAGE,
            }
            or self.primary_failure_code is not None
            or self.actual_parser_id is not None
            or self.recovery_failure_code is not None
            or self.parser_executed
        ):
            raise ValueError("not-attempted recovery is only valid before the parser stage")

        admitted = self.status is v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
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
                raise ValueError("admitted v2 terminal lacks semantic-pool IDs")
            if (
                self.resolution_outcome != "unresolved"
                or self.quality_tier != "unknown"
                or not self.requires_adjudication
                or self.decision != "REVIEW"
            ):
                raise ValueError("admitted v2 records must remain unresolved")
            if self.failure_code is not None or self.failure_detail is not None:
                raise ValueError("admitted v2 terminal cannot carry a failure")
            if not (
                self.parser_executed and self.lean_validation_executed and self.screening_executed
            ):
                raise ValueError("v2 admission requires parser, Lean, and screening")
        else:
            if (
                self.resolution_outcome is not None
                or self.quality_tier is not None
                or self.requires_adjudication
                or self.decision is not None
                or self.pair_ids
                or self.nl_lean_id is not None
            ):
                raise ValueError("non-admitted v2 outcomes cannot create semantic records")
            if self.failure_code is None or self.failure_detail is None:
                raise ValueError("non-admitted v2 terminal requires an operational reason")
        expected = "research_postprocess_v2_terminal:" + hash_canonical(
            {"schema": "lf021_research_postprocess_terminal_v2", **self.id_payload()}
        )
        if self.terminal_id != expected:
            raise ValueError("v2 terminal_id does not match payload")
        return self


class ResearchPostprocessV2FamilyReport(StrictModel):
    schema_version: Literal[2] = 2
    report_id: str = Field(pattern=_FAMILY_ID)
    input_binding_hash: str = Field(pattern=_HEX64)
    family_id: str
    expected_invocations: Literal[3] = 3
    terminal_invocations: Literal[3] = 3
    status_counts: dict[str, int]
    recovery_status_counts: dict[str, int]
    parser_success_count: int = Field(ge=0, le=3)
    admitted_unresolved_count: int = Field(ge=0, le=3)
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key != "report_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if sum(self.status_counts.values()) != 3:
            raise ValueError("v2 family status counts do not reconcile")
        if sum(self.recovery_status_counts.values()) != 3:
            raise ValueError("v2 family recovery counts do not reconcile")
        expected = "research_postprocess_v2_family:" + hash_canonical(
            {"schema": "lf021_research_postprocess_family_v2", **self.id_payload()}
        )
        if self.report_id != expected:
            raise ValueError("v2 family report ID does not match payload")
        return self


class ResearchPostprocessV2Manifest(StrictModel):
    schema_version: Literal[2] = 2
    manifest_id: str = Field(pattern=_MANIFEST_ID)
    input_binding: ResearchPostprocessV2InputBinding
    input_binding_hash: str = Field(pattern=_HEX64)
    expected_invocations: Literal[9] = 9
    terminal_invocations: Literal[9] = 9
    family_count: Literal[3] = 3
    status_counts: dict[str, int]
    recovery_status_counts: dict[str, int]
    terminal_artifacts: dict[str, str]
    family_report_artifacts: dict[str, str]
    admitted_pair_count: int = Field(ge=0)
    admitted_nl_lean_count: int = Field(ge=0, le=9)
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
            raise ValueError("v2 manifest input binding hash differs")
        if sum(self.status_counts.values()) != 9:
            raise ValueError("v2 manifest status counts do not reconcile")
        if sum(self.recovery_status_counts.values()) != 9:
            raise ValueError("v2 manifest recovery counts do not reconcile")
        if len(self.terminal_artifacts) != 9 or len(self.family_report_artifacts) != 3:
            raise ValueError("v2 manifest artifact denominator differs")
        for field_name in ("terminal_artifacts", "family_report_artifacts"):
            values = getattr(self, field_name)
            if list(values) != sorted(values):
                raise ValueError(f"{field_name} must be sorted")
            if any(re.fullmatch(_HEX64, value) is None for value in values.values()):
                raise ValueError(f"{field_name} values must be SHA-256")
        expected = "research_postprocess_v2_manifest:" + hash_canonical(
            {"schema": "lf021_research_postprocess_manifest_v2", **self.id_payload()}
        )
        if self.manifest_id != expected:
            raise ValueError("v2 manifest ID does not match payload")
        return self


@dataclass(frozen=True, slots=True)
class LoadedResearchPostprocessV2:
    base: v1.LoadedResearchPostprocess
    input_binding: ResearchPostprocessV2InputBinding


@dataclass(slots=True)
class _PreparedCandidate:
    invocation: ResearchCollectionInvocation
    collection_terminal: ResearchCollectionTerminal
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
class ResearchPostprocessV2Run:
    output_root: Path
    manifest_path: Path
    manifest: ResearchPostprocessV2Manifest
    terminals: tuple[ResearchPostprocessV2Terminal, ...]
    family_reports: tuple[ResearchPostprocessV2FamilyReport, ...]


class _ParsedCandidateRecordV2(StrictModel):
    schema_version: Literal[2] = 2
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


def _bound_repo_artifact(root: Path, path: Path) -> v1.PostprocessArtifactBinding:
    resolved = path.resolve()
    return v1.PostprocessArtifactBinding(
        artifact=str(resolved.relative_to(root.resolve())),
        sha256=hash_file(resolved),
    )


def load_research_postprocess_v2(
    *,
    repo_root: Path,
    collection_root: Path,
    problem_pool_records_path: Path,
    context_path: Path,
    import_header_path: Path,
    reference_theorems_path: Path,
    reference_representations_path: Path,
    output_root: Path | None = None,
) -> LoadedResearchPostprocessV2:
    """Load v1 inputs without reading or mutating postprocess_v1 outputs."""

    destination = (
        output_root.resolve()
        if output_root is not None
        else (collection_root.resolve() / "postprocess_v2")
    )
    base = v1.load_research_postprocess(
        repo_root=repo_root,
        collection_root=collection_root,
        problem_pool_records_path=problem_pool_records_path,
        context_path=context_path,
        import_header_path=import_header_path,
        reference_theorems_path=reference_theorems_path,
        reference_representations_path=reference_representations_path,
        output_root=destination,
    )
    binding = ResearchPostprocessV2InputBinding(
        primary_binding=base.input_binding,
        primary_implementation=base.input_binding.implementation,
        recovery_implementation=_bound_repo_artifact(
            base.repo_root,
            Path(__file__).with_name("local_output_recovery.py"),
        ),
        implementation=_bound_repo_artifact(base.repo_root, Path(__file__)),
        invocation_ids=base.input_binding.invocation_ids,
        family_ids=base.input_binding.family_ids,
    )
    return LoadedResearchPostprocessV2(base=base, input_binding=binding)


def _parser_failure_code(exc: BaseException) -> str:
    if isinstance(exc, (FinalFenceError, RecoveryError)):
        return exc.code.value
    return type(exc).__name__


def _terminal(
    *,
    loaded: LoadedResearchPostprocessV2,
    invocation: ResearchCollectionInvocation,
    collection_terminal: ResearchCollectionTerminal,
    status: v1.ResearchPostprocessStatus,
    stage: v1.ResearchPostprocessStage,
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
) -> ResearchPostprocessV2Terminal:
    final = admitted or materialized
    payload: dict[str, object] = {
        "schema_version": 2,
        "record_kind": "lf021_research_postprocess_terminal_v2",
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
        "record_time_basis": collection_terminal.completed_at.isoformat().replace("+00:00", "Z"),
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
        "semantic_pool_admitted": status is v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED,
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
    terminal_id = "research_postprocess_v2_terminal:" + hash_canonical(
        {"schema": "lf021_research_postprocess_terminal_v2", **payload}
    )
    return ResearchPostprocessV2Terminal.model_validate({"terminal_id": terminal_id, **payload})


def _failure_terminal(
    *,
    loaded: LoadedResearchPostprocessV2,
    invocation: ResearchCollectionInvocation,
    collection_terminal: ResearchCollectionTerminal,
    status: v1.ResearchPostprocessStatus,
    stage: v1.ResearchPostprocessStage,
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
) -> ResearchPostprocessV2Terminal:
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


def _parse_with_fallback(
    loaded: LoadedResearchPostprocessV2,
    *,
    invocation: ResearchCollectionInvocation,
    collection_terminal: ResearchCollectionTerminal,
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
    primary = v1._parser(
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
            raise ResearchPostprocessV2Error(
                "recovery parser source changed after the v2 input binding was loaded"
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


def _prepare_candidates(
    loaded: LoadedResearchPostprocessV2,
    *,
    backend: LeanInteractBackend,
) -> tuple[list[_PreparedCandidate], dict[str, ResearchPostprocessV2Terminal]]:
    prepared: list[_PreparedCandidate] = []
    terminals: dict[str, ResearchPostprocessV2Terminal] = {}
    base = loaded.base
    for invocation in sorted(base.invocations, key=lambda item: item.invocation_id):
        collection_terminal = base.collection_terminals[invocation.invocation_id]
        if collection_terminal.status is not ResearchTerminalStatus.RAW_COLLECTED:
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=v1.ResearchPostprocessStatus.COLLECTION_NOT_RAW,
                stage=v1.ResearchPostprocessStage.COLLECTION,
                code=f"collection_{collection_terminal.status.value}",
                detail=collection_terminal.error_detail or "raw output was not collected",
                primary_failure_code=None,
                recovery_status=RecoveryStatus.NOT_ATTEMPTED,
                actual_parser_id=None,
                actual_parser_source_sha256=None,
            )
            continue
        try:
            call, _, raw_output, raw_hashes = v1._verify_semantic_raw_lineage(
                base,
                invocation,
                collection_terminal,
            )
        except Exception as exc:
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=v1.ResearchPostprocessStatus.RAW_LINEAGE_FAILED,
                stage=v1.ResearchPostprocessStage.RAW_LINEAGE,
                code=type(exc).__name__,
                detail=str(exc),
                primary_failure_code=None,
                recovery_status=RecoveryStatus.NOT_ATTEMPTED,
                actual_parser_id=None,
                actual_parser_source_sha256=None,
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
            # The detail string carries the versioned recovery error prefix.
            # Extract only that prefix into the explicit code field.
            if recovery_status is RecoveryStatus.FAILED and parse_failure_detail:
                recovery_failure_code = parse_failure_detail.split(":", 1)[0]
            code = recovery_failure_code or primary_failure_code or "parser_failed"
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=v1.ResearchPostprocessStatus.PARSE_FAILED,
                stage=v1.ResearchPostprocessStage.PARSER,
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
                    "postprocess_primary_parser_id": invocation.parser_id,
                    "postprocess_primary_parser_source_sha256": (invocation.parser_source_sha256),
                    "postprocess_actual_parser_id": actual_parser_id,
                    "postprocess_actual_parser_source_sha256": actual_parser_hash,
                    "postprocess_primary_failure_code": primary_failure_code,
                    "postprocess_recovery_status": recovery_status.value,
                    "semantic_labels_created": False,
                },
            }
        )
        candidate_record = _ParsedCandidateRecordV2(
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
            path, digest = v1._persist_record(
                loaded=base,
                invocation_id=invocation.invocation_id,
                name=name,
                record=record,
            )
            output_hashes[path] = digest

        problem = base.problems[invocation.problem_record_id]
        references = tuple(
            base.references[theorem_id] for theorem_id in problem.reference_theorem_ids
        )
        generation_config_hash = hash_canonical(
            {
                "schema": "lf021_research_candidate_generation_v2",
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
                status=v1.ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                stage=v1.ResearchPostprocessStage.MATERIALIZATION,
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
            v1._persist_materialization(
                loaded=base,
                invocation_id=invocation.invocation_id,
                materialized=materialized,
                prefix="materialized",
            )
        )
        if materialized.outcome.outcome is not RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING:
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=v1.ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                stage=v1.ResearchPostprocessStage.MATERIALIZATION,
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
    loaded: LoadedResearchPostprocessV2,
    *,
    prepared: list[_PreparedCandidate],
    terminals: dict[str, ResearchPostprocessV2Terminal],
) -> None:
    base = loaded.base
    by_alpha: dict[str, list[_PreparedCandidate]] = defaultdict(list)
    for item in prepared:
        representation = item.materialized.representation
        if representation is None or representation.alpha_identity_fingerprint is None:
            terminals[item.invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=item.invocation,
                collection_terminal=item.collection_terminal,
                status=v1.ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                stage=v1.ResearchPostprocessStage.MATERIALIZATION,
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
    canonical_by_alpha = v1._canonical_candidate_keys_by_alpha(tuple(identity_rows))
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
            path, digest = v1._persist_record(
                loaded=base,
                invocation_id=item.invocation.invocation_id,
                name="screening.json",
                record=screening,
            )
            output_hashes[path] = digest
            if screening.status is not CandidateScreeningStatus.CLEAN:
                terminals[item.invocation.invocation_id] = _terminal(
                    loaded=loaded,
                    invocation=item.invocation,
                    collection_terminal=item.collection_terminal,
                    status=v1.ResearchPostprocessStatus.SCREEN_REJECTED,
                    stage=v1.ResearchPostprocessStage.SCREENING,
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
                        f"{list(screening.benchmark_hits)}; duplicate_candidate_theorem_ids="
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
                admitted = v1._unresolved_pairs(admitted)
            except Exception as exc:
                terminals[item.invocation.invocation_id] = _terminal(
                    loaded=loaded,
                    invocation=item.invocation,
                    collection_terminal=item.collection_terminal,
                    status=v1.ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                    stage=v1.ResearchPostprocessStage.ADMISSION,
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
                v1._persist_materialization(
                    loaded=base,
                    invocation_id=item.invocation.invocation_id,
                    materialized=admitted,
                    prefix="admitted",
                )
            )
            pair_path, pair_digest = v1._persist_jsonl(
                loaded=base,
                invocation_id=item.invocation.invocation_id,
                name="unresolved_pairs.jsonl",
                records=cast(tuple[StrictModel, ...], admitted.pairs),
            )
            output_hashes[pair_path] = pair_digest
            assert admitted.nl_lean is not None
            nl_path, nl_digest = v1._persist_record(
                loaded=base,
                invocation_id=item.invocation.invocation_id,
                name="unresolved_nl_lean.json",
                record=admitted.nl_lean,
            )
            output_hashes[nl_path] = nl_digest
            terminals[item.invocation.invocation_id] = _terminal(
                loaded=loaded,
                invocation=item.invocation,
                collection_terminal=item.collection_terminal,
                status=v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED,
                stage=v1.ResearchPostprocessStage.COMPLETE,
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
    loaded: LoadedResearchPostprocessV2,
    *,
    family_id: str,
    selected: tuple[ResearchPostprocessV2Terminal, ...],
) -> dict[str, object]:
    if len(selected) != 3 or any(item.family_id != family_id for item in selected):
        raise ResearchPostprocessV2Error(f"v2 family denominator differs: {family_id}")
    return {
        "schema_version": 2,
        "input_binding_hash": loaded.input_binding.binding_hash,
        "family_id": family_id,
        "expected_invocations": 3,
        "terminal_invocations": 3,
        "status_counts": dict(sorted(Counter(item.status.value for item in selected).items())),
        "recovery_status_counts": dict(
            sorted(Counter(item.recovery_status.value for item in selected).items())
        ),
        "parser_success_count": sum(
            item.status
            not in {
                v1.ResearchPostprocessStatus.PARSE_FAILED,
                v1.ResearchPostprocessStatus.RAW_LINEAGE_FAILED,
                v1.ResearchPostprocessStatus.COLLECTION_NOT_RAW,
            }
            for item in selected
        ),
        "admitted_unresolved_count": sum(
            item.status is v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED for item in selected
        ),
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }


def _write_terminals_and_reports(
    loaded: LoadedResearchPostprocessV2,
    terminals_by_id: dict[str, ResearchPostprocessV2Terminal],
) -> ResearchPostprocessV2Run:
    base = loaded.base
    if set(terminals_by_id) != set(loaded.input_binding.invocation_ids):
        missing = sorted(set(loaded.input_binding.invocation_ids) - set(terminals_by_id))
        raise ResearchPostprocessV2Error(
            "v2 postprocess denominator is incomplete: " + ", ".join(missing)
        )
    terminals = tuple(terminals_by_id[key] for key in sorted(terminals_by_id))
    terminal_artifacts: dict[str, str] = {}
    for terminal in terminals:
        path = v1._output_directory(base, terminal.invocation_id) / "processing_terminal.json"
        digest = v1._write_immutable(path, v1._canonical_record_bytes(terminal))
        terminal_artifacts[str(path.resolve().relative_to(base.repo_root))] = digest

    reports: list[ResearchPostprocessV2FamilyReport] = []
    report_artifacts: dict[str, str] = {}
    for family_id in loaded.input_binding.family_ids:
        selected = tuple(item for item in terminals if item.family_id == family_id)
        payload = _family_payload(loaded, family_id=family_id, selected=selected)
        report_id = "research_postprocess_v2_family:" + hash_canonical(
            {"schema": "lf021_research_postprocess_family_v2", **payload}
        )
        report = ResearchPostprocessV2FamilyReport.model_validate(
            {"report_id": report_id, **payload}
        )
        path = base.output_root / "families" / f"{family_id}.json"
        digest = v1._write_immutable(path, v1._canonical_record_bytes(report))
        report_artifacts[str(path.resolve().relative_to(base.repo_root))] = digest
        reports.append(report)

    admitted = tuple(
        item
        for item in terminals
        if item.status is v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    )
    payload = {
        "schema_version": 2,
        "input_binding": loaded.input_binding.model_dump(mode="json"),
        "input_binding_hash": loaded.input_binding.binding_hash,
        "expected_invocations": 9,
        "terminal_invocations": 9,
        "family_count": 3,
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
    manifest_id = "research_postprocess_v2_manifest:" + hash_canonical(
        {"schema": "lf021_research_postprocess_manifest_v2", **payload}
    )
    manifest = ResearchPostprocessV2Manifest.model_validate({"manifest_id": manifest_id, **payload})
    manifest_path = base.output_root / "manifest.json"
    v1._write_immutable(manifest_path, v1._canonical_record_bytes(manifest))
    return ResearchPostprocessV2Run(
        output_root=base.output_root,
        manifest_path=manifest_path,
        manifest=manifest,
        terminals=terminals,
        family_reports=tuple(reports),
    )


def run_research_postprocess_v2(
    loaded: LoadedResearchPostprocessV2,
    *,
    backend: LeanInteractBackend,
) -> ResearchPostprocessV2Run:
    """Process the exact nine raw calls with explicit recovery provenance."""

    prepared, terminals = _prepare_candidates(loaded, backend=backend)
    _screen_and_admit(loaded, prepared=prepared, terminals=terminals)
    return _write_terminals_and_reports(loaded, terminals)


def _verify_binding(
    repo_root: Path,
    binding: v1.PostprocessArtifactBinding,
) -> None:
    path = v1._resolve_bound_artifact(repo_root, binding)
    if hash_file(path) != binding.sha256:
        raise ResearchPostprocessV2Error(f"v2 bound artifact hash mismatch: {binding.artifact}")


def verify_research_postprocess_v2(
    loaded: LoadedResearchPostprocessV2,
) -> ResearchPostprocessV2Manifest:
    """Replay-verify v2 without Lean execution or writes."""

    base = loaded.base
    # Re-run all v1 input checks.  This intentionally does not read
    # postprocess_v1 output artifacts.
    primary = loaded.input_binding.primary_binding
    for binding in (
        primary.collection_plan,
        primary.collection_manifest,
        primary.problem_pool_records,
        primary.context,
        primary.import_header,
        primary.reference_theorems,
        primary.reference_representations,
        primary.implementation,
        *primary.active_registry_artifacts.values(),
        loaded.input_binding.recovery_implementation,
        loaded.input_binding.implementation,
    ):
        _verify_binding(base.repo_root, binding)
    for artifact_map in (
        primary.collection_terminal_artifacts,
        primary.collection_family_session_artifacts,
    ):
        for artifact, expected in artifact_map.items():
            if hash_file(v1._resolve_repo_artifact(base.repo_root, artifact)) != expected:
                raise ResearchPostprocessV2Error(f"v2 collection input hash mismatch: {artifact}")

    manifest = v1._load_canonical(
        base.output_root / "manifest.json",
        ResearchPostprocessV2Manifest,
    )
    if manifest.input_binding != loaded.input_binding:
        raise ResearchPostprocessV2Error("persisted v2 input binding has drifted")
    invocations = {item.invocation_id: item for item in base.invocations}
    terminals: list[ResearchPostprocessV2Terminal] = []
    for artifact, expected in manifest.terminal_artifacts.items():
        path = v1._resolve_repo_artifact(base.repo_root, artifact)
        if hash_file(path) != expected:
            raise ResearchPostprocessV2Error(f"v2 terminal hash mismatch: {artifact}")
        terminal = v1._load_canonical(path, ResearchPostprocessV2Terminal)
        expected_path = (
            v1._output_directory(base, terminal.invocation_id) / "processing_terminal.json"
        ).resolve()
        if path != expected_path:
            raise ResearchPostprocessV2Error(f"v2 terminal stored at unexpected path: {artifact}")
        invocation = invocations.get(terminal.invocation_id)
        collection = base.collection_terminals.get(terminal.invocation_id)
        collection_path = base.collection_terminal_paths.get(terminal.invocation_id)
        if invocation is None or collection is None or collection_path is None:
            raise ResearchPostprocessV2Error("v2 terminal lacks frozen invocation lineage")
        if (
            terminal.input_binding_hash != loaded.input_binding.binding_hash
            or terminal.invocation_payload_hash
            != hash_canonical(invocation.model_dump(mode="json"))
            or terminal.collection_terminal_id != collection.terminal_id
            or terminal.collection_terminal_sha256 != hash_file(collection_path)
            or terminal.primary_parser_id != invocation.parser_id
            or terminal.primary_parser_source_sha256 != invocation.parser_source_sha256
        ):
            raise ResearchPostprocessV2Error(
                f"v2 terminal lineage differs: {terminal.invocation_id}"
            )
        bound = {**terminal.raw_lineage_hashes, **terminal.output_artifact_hashes}
        if len(bound) != len(terminal.raw_lineage_hashes) + len(terminal.output_artifact_hashes):
            raise ResearchPostprocessV2Error(
                f"v2 raw/output path collision: {terminal.invocation_id}"
            )
        for bound_artifact, bound_hash in bound.items():
            if hash_file(v1._resolve_repo_artifact(base.repo_root, bound_artifact)) != bound_hash:
                raise ResearchPostprocessV2Error(f"v2 bound output hash mismatch: {bound_artifact}")
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
                raise ResearchPostprocessV2Error(
                    f"v2 parsed-candidate provenance missing: {terminal.invocation_id}"
                )
            parsed_record = v1._load_canonical(
                v1._resolve_repo_artifact(base.repo_root, parsed_candidates[0]),
                _ParsedCandidateRecordV2,
            )
            if (
                parsed_record.invocation_id != terminal.invocation_id
                or parsed_record.primary_parser_id != terminal.primary_parser_id
                or parsed_record.primary_parser_source_sha256
                != terminal.primary_parser_source_sha256
                or parsed_record.actual_parser_id != terminal.actual_parser_id
                or parsed_record.actual_parser_source_sha256 != terminal.actual_parser_source_sha256
                or parsed_record.primary_failure_code != terminal.primary_failure_code
                or parsed_record.recovery_status != terminal.recovery_status.value
            ):
                raise ResearchPostprocessV2Error(
                    f"v2 parsed-candidate parser provenance differs: {terminal.invocation_id}"
                )
        elif parsed_candidates:
            raise ResearchPostprocessV2Error(
                f"failed v2 parse unexpectedly persisted a candidate: {terminal.invocation_id}"
            )
        terminals.append(terminal)
    if tuple(sorted(item.invocation_id for item in terminals)) != (
        loaded.input_binding.invocation_ids
    ):
        raise ResearchPostprocessV2Error("persisted v2 terminal denominator differs")

    status_counts = dict(sorted(Counter(item.status.value for item in terminals).items()))
    recovery_counts = dict(
        sorted(Counter(item.recovery_status.value for item in terminals).items())
    )
    admitted = tuple(
        item
        for item in terminals
        if item.status is v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    )
    if (
        status_counts != manifest.status_counts
        or recovery_counts != manifest.recovery_status_counts
        or sum(len(item.pair_ids) for item in admitted) != manifest.admitted_pair_count
        or len(admitted) != manifest.admitted_nl_lean_count
    ):
        raise ResearchPostprocessV2Error("v2 manifest accounting differs")

    reports: dict[str, ResearchPostprocessV2FamilyReport] = {}
    for artifact, expected in manifest.family_report_artifacts.items():
        path = v1._resolve_repo_artifact(base.repo_root, artifact)
        if hash_file(path) != expected:
            raise ResearchPostprocessV2Error(f"v2 family hash mismatch: {artifact}")
        report = v1._load_canonical(path, ResearchPostprocessV2FamilyReport)
        if report.family_id in reports:
            raise ResearchPostprocessV2Error(f"duplicate v2 family report: {report.family_id}")
        expected_path = (base.output_root / "families" / f"{report.family_id}.json").resolve()
        if path != expected_path:
            raise ResearchPostprocessV2Error(
                f"v2 family report stored at unexpected path: {artifact}"
            )
        reports[report.family_id] = report
    if set(reports) != set(loaded.input_binding.family_ids):
        raise ResearchPostprocessV2Error("v2 family report denominator differs")
    for family_id, report in reports.items():
        expected_payload = _family_payload(
            loaded,
            family_id=family_id,
            selected=tuple(item for item in terminals if item.family_id == family_id),
        )
        if report.model_dump(mode="json", exclude={"report_id"}) != expected_payload:
            raise ResearchPostprocessV2Error(f"v2 family report accounting differs: {family_id}")
    return manifest


__all__ = [
    "LoadedResearchPostprocessV2",
    "RecoveryStatus",
    "ResearchPostprocessV2Error",
    "ResearchPostprocessV2FamilyReport",
    "ResearchPostprocessV2InputBinding",
    "ResearchPostprocessV2Manifest",
    "ResearchPostprocessV2Run",
    "ResearchPostprocessV2Terminal",
    "load_research_postprocess_v2",
    "run_research_postprocess_v2",
    "verify_research_postprocess_v2",
]
