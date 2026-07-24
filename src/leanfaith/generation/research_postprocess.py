"""Deterministic LF-021 postprocessing for completed local research outputs.

This module begins *after* raw model collection.  It verifies the frozen
collection denominator and raw lineage, dispatches the exact family parser,
uses LeanInteract-backed materialization, performs benchmark and within-run
deduplication screening, and admits only clean candidates as unresolved
``PairRecord``/``NLPLeanRecord`` items.

Operational failures are terminal processing outcomes.  They are never
semantic negatives, never resolved labels, and never Gate-5 evidence.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import load_active_benchmark_registry
from leanfaith.generation.candidate_screening import (
    CandidateScreeningIndex,
    PriorCandidateIdentity,
    screen_materialized_candidate,
)
from leanfaith.generation.invocation_failure import redact_exception_message
from leanfaith.generation.local_output_adapter import (
    FINAL_FENCE_PARSER_ID,
    RAW_OR_FINAL_PARSER_ID,
    TERMINAL_FENCE_OR_RAW_PARSER_ID,
    FinalFenceError,
    LeanExtractedCandidate,
    extract_candidate_signature_with_lean,
    extract_candidate_signature_with_lean_v2,
    extract_candidate_signature_with_lean_v3,
    parser_source_sha256,
)
from leanfaith.generation.local_output_adapter_stepfun import (
    STEPFUN_TERMINAL_PARSER_ID,
    extract_stepfun_candidate_signature_with_lean,
    stepfun_parser_source_sha256,
)
from leanfaith.generation.problem_pool import ProblemPoolDenylistBinding
from leanfaith.generation.providers import verify_llm_call_artifacts
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
    ResearchCollectionManifest,
    ResearchCollectionPlan,
    ResearchCollectionTerminal,
    ResearchFamilySessionStart,
    ResearchInvocationBoundary,
    ResearchLocalGenerationResult,
    ResearchModelAttemptBoundary,
    ResearchTerminalStatus,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.enums import LLMCallStatus, ParseStatus
from leanfaith.schemas.llm import (
    LLMAttemptRecord,
    LLMCallRecord,
    check_llm_call_attempt_lineage,
)
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

_HEX64 = r"^[0-9a-f]{64}$"
_POSTPROCESS_TERMINAL = r"^research_postprocess_terminal:[0-9a-f]{64}$"
_POSTPROCESS_MANIFEST = r"^research_postprocess_manifest:[0-9a-f]{64}$"
_POSTPROCESS_REPORT = r"^research_postprocess_family:[0-9a-f]{64}$"
_RAW_COLLECTION_ARTIFACT_KEYS = frozenset(
    {
        "family_session_start",
        "llm_attempt",
        "llm_call",
        "local_generation_result",
        "model_attempt_boundary",
        "provider_boundary",
        "provider_raw_response",
        "provider_request",
    }
)
type CandidateParser = Callable[..., LeanExtractedCandidate]


class ResearchPostprocessError(RuntimeError):
    """A frozen collection or postprocessing artifact violates the contract."""


class ResearchPostprocessArtifactConflict(ResearchPostprocessError):
    """An immutable postprocessing path already contains different bytes."""


class ResearchPostprocessStatus(StrEnum):
    """One terminal state for every frozen collection invocation."""

    COLLECTION_NOT_RAW = "collection_not_raw"
    RAW_LINEAGE_FAILED = "raw_lineage_failed"
    PARSE_FAILED = "parse_failed"
    MATERIALIZATION_FAILED = "materialization_failed"
    SCREEN_REJECTED = "screen_rejected"
    ADMITTED_UNRESOLVED = "admitted_unresolved"


class ResearchPostprocessStage(StrEnum):
    COLLECTION = "collection"
    RAW_LINEAGE = "raw_lineage"
    PARSER = "parser"
    MATERIALIZATION = "materialization"
    SCREENING = "screening"
    ADMISSION = "admission"
    COMPLETE = "complete"


def _relative_artifact(value: str, *, field: str) -> str:
    parsed = PurePosixPath(value)
    if not value.strip() or parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError(f"{field} must be a nonempty repository-relative path")
    return value


class PostprocessArtifactBinding(StrictModel):
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    location_kind: Literal["repo_relative", "absolute_content_addressed"] = "repo_relative"

    @model_validator(mode="after")
    def _valid_location(self) -> Self:
        if self.location_kind == "repo_relative":
            _relative_artifact(self.artifact, field="artifact")
        elif not Path(self.artifact).is_absolute():
            raise ValueError("absolute_content_addressed artifacts require an absolute path")
        return self


class ResearchPostprocessInputBinding(StrictModel):
    """All immutable bytes needed to replay the nine-candidate stage."""

    schema_version: Literal[1] = 1
    collection_plan: PostprocessArtifactBinding
    collection_manifest: PostprocessArtifactBinding
    collection_plan_id: str
    collection_plan_hash: str = Field(pattern=_HEX64)
    collection_manifest_id: str
    collection_terminal_artifacts: dict[str, str]
    collection_family_session_artifacts: dict[str, str]
    problem_pool_records: PostprocessArtifactBinding
    context: PostprocessArtifactBinding
    import_header: PostprocessArtifactBinding
    reference_theorems: PostprocessArtifactBinding
    reference_representations: PostprocessArtifactBinding
    active_registry_artifacts: dict[str, PostprocessArtifactBinding]
    active_registry_content_hash: str = Field(pattern=_HEX64)
    implementation: PostprocessArtifactBinding
    invocation_ids: tuple[str, ...]
    family_ids: tuple[str, ...]

    @property
    def binding_hash(self) -> str:
        return hash_canonical(
            {
                "schema": "lf021_research_postprocess_input_binding_v1",
                **self.model_dump(mode="json"),
            }
        )

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len(self.invocation_ids) != 9:
            raise ValueError("postprocessing input requires exactly nine invocations")
        if self.invocation_ids != tuple(sorted(set(self.invocation_ids))):
            raise ValueError("postprocessing invocation IDs must be sorted and unique")
        if len(self.family_ids) != 3 or self.family_ids != tuple(sorted(set(self.family_ids))):
            raise ValueError("postprocessing input requires three sorted families")
        if list(self.collection_terminal_artifacts) != sorted(self.collection_terminal_artifacts):
            raise ValueError("collection terminal artifacts must be sorted")
        if len(self.collection_terminal_artifacts) != 9:
            raise ValueError("postprocessing input requires nine collection terminals")
        if any(
            re.fullmatch(_HEX64, digest) is None
            for digest in self.collection_terminal_artifacts.values()
        ):
            raise ValueError("collection terminal artifact hashes must be SHA-256")
        if list(self.collection_family_session_artifacts) != sorted(
            self.collection_family_session_artifacts
        ) or any(
            re.fullmatch(_HEX64, digest) is None
            for digest in self.collection_family_session_artifacts.values()
        ):
            raise ValueError("collection family-session hashes must be sorted SHA-256")
        if list(self.active_registry_artifacts) != sorted(self.active_registry_artifacts):
            raise ValueError("active registry artifact map must be sorted")
        required_registry = {
            "active_registry",
            "base_registry",
            "code_bundle",
            "detailed_index",
            "input_manifest",
            "pointer_manifest",
        }
        if set(self.active_registry_artifacts) != required_registry:
            raise ValueError("active registry artifact map is incomplete")
        return self


class ResearchPostprocessTerminal(StrictModel):
    """One immutable processing outcome for one collection invocation."""

    schema_version: Literal[1] = 1
    record_kind: Literal["lf021_research_postprocess_terminal"] = (
        "lf021_research_postprocess_terminal"
    )
    artifact_class: Literal["research"] = "research"
    terminal_id: str = Field(pattern=_POSTPROCESS_TERMINAL)
    input_binding_hash: str = Field(pattern=_HEX64)
    invocation_id: str
    invocation_payload_hash: str = Field(pattern=_HEX64)
    collection_terminal_id: str
    collection_terminal_sha256: str = Field(pattern=_HEX64)
    family_id: str
    problem_record_id: str
    seed: int = Field(ge=0)
    status: ResearchPostprocessStatus
    terminal_stage: ResearchPostprocessStage
    record_time_basis: datetime.datetime
    parser_id: str
    parser_source_sha256: str = Field(pattern=_HEX64)
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
            if any(re.fullmatch(_HEX64, digest) is None for digest in values.values()):
                raise ValueError(f"{field_name} values must be SHA-256")
        admitted = self.status is ResearchPostprocessStatus.ADMITTED_UNRESOLVED
        if self.semantic_pool_admitted != admitted:
            raise ValueError("semantic_pool_admitted must match admitted_unresolved status")
        if admitted:
            required = (
                self.variant_id,
                self.candidate_theorem_id,
                self.representation_id,
                self.screening_id,
                self.nl_lean_id,
            )
            if any(value is None for value in required) or not self.pair_ids:
                raise ValueError("admitted unresolved terminal lacks semantic-pool IDs")
            if (
                self.resolution_outcome != "unresolved"
                or self.quality_tier != "unknown"
                or not self.requires_adjudication
                or self.decision != "REVIEW"
            ):
                raise ValueError("admitted records must remain unresolved and require review")
            if self.failure_code is not None or self.failure_detail is not None:
                raise ValueError("admitted terminal cannot carry an operational failure")
            if not (
                self.parser_executed and self.lean_validation_executed and self.screening_executed
            ):
                raise ValueError("admission requires parser, Lean, and screening execution")
        else:
            if (
                self.resolution_outcome is not None
                or self.quality_tier is not None
                or self.requires_adjudication
                or self.decision is not None
                or self.pair_ids
                or self.nl_lean_id is not None
            ):
                raise ValueError("non-admitted outcomes cannot create semantic-pool records")
            if self.failure_code is None or self.failure_detail is None:
                raise ValueError("non-admitted terminal requires an operational reason")
        expected = "research_postprocess_terminal:" + hash_canonical(
            {"schema": "lf021_research_postprocess_terminal_v1", **self.id_payload()}
        )
        if self.terminal_id != expected:
            raise ValueError("postprocess terminal_id does not match payload")
        return self


class ResearchPostprocessFamilyReport(StrictModel):
    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=_POSTPROCESS_REPORT)
    input_binding_hash: str = Field(pattern=_HEX64)
    family_id: str
    expected_invocations: Literal[3] = 3
    terminal_invocations: Literal[3] = 3
    status_counts: dict[str, int]
    collection_raw_count: int = Field(ge=0, le=3)
    parser_success_count: int = Field(ge=0, le=3)
    materialized_pending_screen_count: int = Field(ge=0, le=3)
    screening_clean_count: int = Field(ge=0, le=3)
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
            raise ValueError("family status counts do not reconcile")
        expected = "research_postprocess_family:" + hash_canonical(
            {"schema": "lf021_research_postprocess_family_v1", **self.id_payload()}
        )
        if self.report_id != expected:
            raise ValueError("family report_id does not match payload")
        return self


class ResearchPostprocessManifest(StrictModel):
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=_POSTPROCESS_MANIFEST)
    input_binding: ResearchPostprocessInputBinding
    input_binding_hash: str = Field(pattern=_HEX64)
    expected_invocations: Literal[9] = 9
    terminal_invocations: Literal[9] = 9
    family_count: Literal[3] = 3
    status_counts: dict[str, int]
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
            raise ValueError("postprocess input binding hash mismatch")
        if sum(self.status_counts.values()) != 9:
            raise ValueError("postprocess status counts do not reconcile")
        if len(self.terminal_artifacts) != 9:
            raise ValueError("postprocess manifest requires nine terminal artifacts")
        if len(self.family_report_artifacts) != 3:
            raise ValueError("postprocess manifest requires three family reports")
        for field_name in ("terminal_artifacts", "family_report_artifacts"):
            values = getattr(self, field_name)
            if list(values) != sorted(values):
                raise ValueError(f"{field_name} must be sorted")
            if any(re.fullmatch(_HEX64, digest) is None for digest in values.values()):
                raise ValueError(f"{field_name} values must be SHA-256")
        expected = "research_postprocess_manifest:" + hash_canonical(
            {"schema": "lf021_research_postprocess_manifest_v1", **self.id_payload()}
        )
        if self.manifest_id != expected:
            raise ValueError("postprocess manifest_id does not match payload")
        return self


@dataclass(frozen=True, slots=True)
class LoadedResearchPostprocess:
    repo_root: Path
    collection_root: Path
    output_root: Path
    plan: ResearchCollectionPlan
    manifest: ResearchCollectionManifest
    invocations: tuple[ResearchCollectionInvocation, ...]
    collection_terminals: dict[str, ResearchCollectionTerminal]
    collection_terminal_paths: dict[str, Path]
    problems: dict[str, ProblemPoolRecord]
    context: ContextRecord
    import_header: str
    references: dict[str, TheoremRecord]
    reference_representations: dict[str, RepresentationRecord]
    denylist: ProblemPoolDenylistBinding
    input_binding: ResearchPostprocessInputBinding


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


@dataclass(frozen=True, slots=True)
class ResearchPostprocessRun:
    output_root: Path
    manifest_path: Path
    manifest: ResearchPostprocessManifest
    terminals: tuple[ResearchPostprocessTerminal, ...]
    family_reports: tuple[ResearchPostprocessFamilyReport, ...]


def _canonical_record_bytes(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _canonical_jsonl_bytes(records: tuple[StrictModel, ...]) -> bytes:
    return b"".join(_canonical_record_bytes(record) for record in records)


def _write_immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ResearchPostprocessArtifactConflict(f"immutable artifact conflict: {path}")
        return hash_file(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise ResearchPostprocessArtifactConflict(
                    f"concurrent immutable artifact conflict: {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_canonical[RecordT: StrictModel](path: Path, model: type[RecordT]) -> RecordT:
    if path.is_symlink() or not path.is_file():
        raise ResearchPostprocessError(f"required canonical artifact is missing: {path}")
    try:
        record = model.model_validate_json(path.read_bytes())
    except Exception as exc:
        raise ResearchPostprocessError(f"invalid {model.__name__}: {path}: {exc}") from exc
    if path.read_bytes() != _canonical_record_bytes(record):
        raise ResearchPostprocessError(f"artifact is not canonical JSON: {path}")
    return record


def _load_jsonl[RecordT: StrictModel](
    path: Path,
    model: type[RecordT],
) -> tuple[RecordT, ...]:
    if path.is_symlink() or not path.is_file():
        raise ResearchPostprocessError(f"required JSONL artifact is missing: {path}")
    records: list[RecordT] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), start=1):
        if not line:
            raise ResearchPostprocessError(f"blank JSONL line {line_number}: {path}")
        try:
            record = model.model_validate_json(line)
        except Exception as exc:
            raise ResearchPostprocessError(
                f"invalid {model.__name__} line {line_number}: {path}: {exc}"
            ) from exc
        if line != canonical_json_bytes(record.model_dump(mode="json")):
            raise ResearchPostprocessError(
                f"noncanonical {model.__name__} line {line_number}: {path}"
            )
        records.append(record)
    return tuple(records)


def _repo_artifact(repo_root: Path, path: Path) -> PostprocessArtifactBinding:
    resolved = path.resolve()
    try:
        relative = str(resolved.relative_to(repo_root.resolve()))
    except ValueError as exc:
        raise ResearchPostprocessError(f"artifact escapes repository root: {path}") from exc
    return PostprocessArtifactBinding(artifact=relative, sha256=hash_file(resolved))


def _content_addressed_artifact(
    repo_root: Path,
    path: Path,
) -> PostprocessArtifactBinding:
    """Bind an immutable artifact either inside the repo or by exact absolute path.

    Gate artifacts intentionally live on bulk storage.  Their absolute locator is
    host-specific, but their content remains replay-safe because the binding
    carries the exact SHA-256 and the active registry separately binds that
    locator.
    """

    resolved = path.resolve()
    try:
        relative = str(resolved.relative_to(repo_root.resolve()))
    except ValueError:
        return PostprocessArtifactBinding(
            artifact=str(resolved),
            sha256=hash_file(resolved),
            location_kind="absolute_content_addressed",
        )
    return PostprocessArtifactBinding(
        artifact=relative,
        sha256=hash_file(resolved),
        location_kind="repo_relative",
    )


def _resolve_repo_artifact(repo_root: Path, artifact: str) -> Path:
    _relative_artifact(artifact, field="artifact")
    resolved = (repo_root / artifact).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ResearchPostprocessError(f"artifact escapes repository root: {artifact}") from exc
    return resolved


def _resolve_bound_artifact(
    repo_root: Path,
    binding: PostprocessArtifactBinding,
) -> Path:
    if binding.location_kind == "repo_relative":
        return _resolve_repo_artifact(repo_root, binding.artifact)
    resolved = Path(binding.artifact).resolve()
    if not resolved.is_absolute() or resolved.is_symlink() or not resolved.is_file():
        raise ResearchPostprocessError(
            f"absolute content-addressed artifact is unavailable: {binding.artifact}"
        )
    return resolved


def _registry_bindings(
    repo_root: Path,
) -> tuple[
    ProblemPoolDenylistBinding,
    dict[str, PostprocessArtifactBinding],
]:
    active = load_active_benchmark_registry(repo_root=repo_root)
    denylist = ProblemPoolDenylistBinding.from_active_registry(active, repo_root=repo_root)
    bindings = {
        "active_registry": _content_addressed_artifact(
            repo_root,
            active.active_registry_path,
        ),
        "base_registry": _content_addressed_artifact(
            repo_root,
            active.base_registry_path,
        ),
        "code_bundle": _content_addressed_artifact(
            repo_root,
            active.code_bundle_path,
        ),
        "detailed_index": _content_addressed_artifact(
            repo_root,
            active.detailed_index_path,
        ),
        "input_manifest": _content_addressed_artifact(
            repo_root,
            active.input_manifest_path,
        ),
        "pointer_manifest": _content_addressed_artifact(
            repo_root,
            active.manifest_path,
        ),
    }
    return denylist, dict(sorted(bindings.items()))


def _manifest_terminal_paths(
    *,
    repo_root: Path,
    collection_root: Path,
    plan: ResearchCollectionPlan,
    manifest: ResearchCollectionManifest,
) -> tuple[dict[str, Path], dict[str, str]]:
    by_path = manifest.terminal_artifact_hashes
    if list(by_path) != sorted(by_path):
        raise ResearchPostprocessError("collection terminal artifact map is not sorted")
    expected: dict[str, Path] = {}
    expected_hashes: dict[str, str] = {}
    for invocation in plan.invocations:
        suffix = invocation.invocation_id.rsplit(":", 1)[-1]
        path = collection_root / "terminals" / f"{suffix}.json"
        relative = str(path.resolve().relative_to(repo_root.resolve()))
        digest = by_path.get(relative)
        if digest is None:
            raise ResearchPostprocessError(
                f"collection manifest omits terminal for {invocation.invocation_id}"
            )
        if hash_file(path) != digest:
            raise ResearchPostprocessError(f"collection terminal hash mismatch: {path}")
        expected[invocation.invocation_id] = path
        expected_hashes[relative] = digest
    if set(expected_hashes) != set(by_path):
        raise ResearchPostprocessError("collection manifest contains unexpected terminals")
    return expected, dict(sorted(expected_hashes.items()))


def load_research_postprocess(
    *,
    repo_root: Path,
    collection_root: Path,
    problem_pool_records_path: Path,
    context_path: Path,
    import_header_path: Path,
    reference_theorems_path: Path,
    reference_representations_path: Path,
    output_root: Path | None = None,
) -> LoadedResearchPostprocess:
    """Load and bind the exact completed 3x3 research collection."""

    root = repo_root.resolve()
    collection = collection_root.resolve()
    try:
        collection.relative_to(root)
    except ValueError as exc:
        raise ResearchPostprocessError("collection root must remain in repository") from exc
    plan_path = collection / "plan.json"
    manifest_path = collection / "manifest.json"
    plan = _load_canonical(plan_path, ResearchCollectionPlan)
    manifest = _load_canonical(manifest_path, ResearchCollectionManifest)
    if (
        manifest.plan_id != plan.plan_id
        or manifest.plan_hash != plan.plan_hash
        or manifest.expected_candidate_count != 9
        or manifest.terminal_candidate_count != 9
        or len(plan.invocations) != 9
        or len(plan.family_bindings) != 3
    ):
        raise ResearchPostprocessError("collection plan/manifest is not the exact 3x3 denominator")

    terminal_paths, terminal_hashes = _manifest_terminal_paths(
        repo_root=root,
        collection_root=collection,
        plan=plan,
        manifest=manifest,
    )
    session_hashes: dict[str, str] = {}
    for artifact, expected in manifest.family_session_artifact_hashes.items():
        session_path = _resolve_repo_artifact(root, artifact)
        try:
            session_path.relative_to(collection)
        except ValueError as exc:
            raise ResearchPostprocessError(
                f"family-session artifact escapes collection root: {artifact}"
            ) from exc
        if hash_file(session_path) != expected:
            raise ResearchPostprocessError(f"collection family-session hash mismatch: {artifact}")
        session_hashes[artifact] = expected
    terminals: dict[str, ResearchCollectionTerminal] = {}
    invocations = tuple(plan.invocations)
    invocation_by_id = {item.invocation_id: item for item in invocations}
    for invocation_id, path in terminal_paths.items():
        terminal = _load_canonical(path, ResearchCollectionTerminal)
        invocation = invocation_by_id[invocation_id]
        if (
            terminal.invocation_id != invocation_id
            or terminal.invocation_payload_hash
            != hash_canonical(invocation.model_dump(mode="json"))
            or terminal.family_id != invocation.family_id
            or terminal.problem_record_id != invocation.problem_record_id
            or terminal.seed != invocation.seed
        ):
            raise ResearchPostprocessError(
                f"collection terminal differs from invocation: {invocation_id}"
            )
        if terminal.family_session_id is not None:
            session_start = (
                collection
                / "families"
                / terminal.family_id
                / "sessions"
                / terminal.family_session_id.rsplit(":", 1)[-1]
                / "family_session_start.json"
            )
            relative_session = str(session_start.resolve().relative_to(root))
            expected_session_hash = terminal.artifact_hashes.get("family_session_start")
            if (
                expected_session_hash is None
                or session_hashes.get(relative_session) != expected_session_hash
                or hash_file(session_start) != expected_session_hash
            ):
                raise ResearchPostprocessError(
                    f"collection terminal family session differs: {invocation_id}"
                )
        terminals[invocation_id] = terminal
    observed_counts = Counter(terminal.status.value for terminal in terminals.values())
    if dict(sorted(observed_counts.items())) != manifest.status_counts:
        raise ResearchPostprocessError("collection terminal counts differ from manifest")

    problems_tuple = _load_jsonl(problem_pool_records_path, ProblemPoolRecord)
    problems = {record.problem_record_id: record for record in problems_tuple}
    if len(problems) != 3 or set(problems) != {
        invocation.problem_record_id for invocation in invocations
    }:
        raise ResearchPostprocessError("problem pool does not match collection invocations")
    context = _load_canonical(context_path, ContextRecord)
    header = import_header_path.read_text(encoding="utf-8")
    if (
        hash_file(import_header_path) != plan.import_header_sha256
        or context.header_text != header
        or context.header_hash != plan.import_header_sha256
        or hash_file(context_path) != plan.context_sha256
        or any(problem.context_id != context.context_id for problem in problems.values())
    ):
        raise ResearchPostprocessError("problem/context/import-header bindings disagree")

    reference_tuple = _load_jsonl(reference_theorems_path, TheoremRecord)
    references = {record.theorem_id: record for record in reference_tuple}
    representation_tuple = _load_jsonl(
        reference_representations_path,
        RepresentationRecord,
    )
    reference_representations = {record.theorem_id: record for record in representation_tuple}
    required_references = {
        theorem_id for problem in problems.values() for theorem_id in problem.reference_theorem_ids
    }
    if (
        set(references) != required_references
        or set(reference_representations) != required_references
        or any(
            reference_representations[theorem_id].context_id != references[theorem_id].context_id
            or reference_representations[theorem_id].theorem_id != theorem_id
            for theorem_id in required_references
        )
    ):
        raise ResearchPostprocessError(
            "reference theorem/representation bytes do not match the problem pool"
        )

    denylist, registry_artifacts = _registry_bindings(root)
    if any(
        problem.denylist_registry_content_hash != denylist.registry_content_hash
        or problem.denylist_manifest_sha256 != denylist.manifest_sha256
        or problem.denylist_active_registry_sha256 != denylist.active_registry_sha256
        for problem in problems.values()
    ):
        raise ResearchPostprocessError("problem pool and active benchmark registry differ")

    implementation_path = Path(__file__)
    input_binding = ResearchPostprocessInputBinding(
        collection_plan=_repo_artifact(root, plan_path),
        collection_manifest=_repo_artifact(root, manifest_path),
        collection_plan_id=plan.plan_id,
        collection_plan_hash=plan.plan_hash,
        collection_manifest_id=manifest.manifest_id,
        collection_terminal_artifacts=terminal_hashes,
        collection_family_session_artifacts=dict(sorted(session_hashes.items())),
        problem_pool_records=_repo_artifact(root, problem_pool_records_path),
        context=_repo_artifact(root, context_path),
        import_header=_repo_artifact(root, import_header_path),
        reference_theorems=_repo_artifact(root, reference_theorems_path),
        reference_representations=_repo_artifact(root, reference_representations_path),
        active_registry_artifacts=registry_artifacts,
        active_registry_content_hash=denylist.registry_content_hash,
        implementation=_repo_artifact(root, implementation_path),
        invocation_ids=tuple(sorted(invocation_by_id)),
        family_ids=tuple(sorted(binding.family_id for binding in plan.family_bindings)),
    )
    destination = (
        output_root.resolve()
        if output_root is not None
        else (collection / "postprocess_v1").resolve()
    )
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ResearchPostprocessError("postprocess output root must remain in repository") from exc
    return LoadedResearchPostprocess(
        repo_root=root,
        collection_root=collection,
        output_root=destination,
        plan=plan,
        manifest=manifest,
        invocations=invocations,
        collection_terminals=terminals,
        collection_terminal_paths=terminal_paths,
        problems=problems,
        context=context,
        import_header=header,
        references=references,
        reference_representations=reference_representations,
        denylist=denylist,
        input_binding=input_binding,
    )


def _parser(
    *,
    invocation: ResearchCollectionInvocation,
    family_parser_artifact: str,
    family_parser_sha256: str,
    repo_root: Path,
) -> CandidateParser:
    if (
        invocation.parser_source_sha256 != family_parser_sha256
        or hash_file(_resolve_repo_artifact(repo_root, family_parser_artifact))
        != family_parser_sha256
    ):
        raise ResearchPostprocessError("frozen parser source binding differs")
    if invocation.parser_id == FINAL_FENCE_PARSER_ID:
        observed = parser_source_sha256()
        parser = extract_candidate_signature_with_lean
    elif invocation.parser_id == RAW_OR_FINAL_PARSER_ID:
        observed = parser_source_sha256()
        parser = extract_candidate_signature_with_lean_v2
    elif invocation.parser_id == TERMINAL_FENCE_OR_RAW_PARSER_ID:
        observed = parser_source_sha256()
        parser = extract_candidate_signature_with_lean_v3
    elif invocation.parser_id == STEPFUN_TERMINAL_PARSER_ID:
        observed = stepfun_parser_source_sha256()
        parser = extract_stepfun_candidate_signature_with_lean
    else:
        raise ResearchPostprocessError(f"unsupported frozen parser ID: {invocation.parser_id}")
    if observed != invocation.parser_source_sha256:
        raise ResearchPostprocessError("executable parser source hash differs from invocation")
    return parser


def _invocation_directory(loaded: LoadedResearchPostprocess, invocation_id: str) -> Path:
    return loaded.collection_root / "invocations" / invocation_id.rsplit(":", 1)[-1]


def _output_directory(loaded: LoadedResearchPostprocess, invocation_id: str) -> Path:
    return loaded.output_root / "invocations" / invocation_id.rsplit(":", 1)[-1]


def _require_exact_raw_collection_artifacts(
    terminal: ResearchCollectionTerminal,
) -> None:
    observed = set(terminal.artifact_hashes)
    if observed != _RAW_COLLECTION_ARTIFACT_KEYS:
        missing = sorted(_RAW_COLLECTION_ARTIFACT_KEYS - observed)
        unexpected = sorted(observed - _RAW_COLLECTION_ARTIFACT_KEYS)
        raise ResearchPostprocessError(
            "raw collection terminal artifact denominator differs: "
            f"missing={missing}; unexpected={unexpected}"
        )


def _verify_semantic_raw_lineage(
    loaded: LoadedResearchPostprocess,
    invocation: ResearchCollectionInvocation,
    terminal: ResearchCollectionTerminal,
) -> tuple[LLMCallRecord, LLMAttemptRecord, str, dict[str, str]]:
    _require_exact_raw_collection_artifacts(terminal)
    directory = _invocation_directory(loaded, invocation.invocation_id)
    paths = {
        "llm_attempt": directory / "llm_attempt.json",
        "llm_call": directory / "llm_call.json",
        "local_generation_result": directory / "local_generation_result.json",
        "model_attempt_boundary": directory / "model_attempt_boundary.json",
        "provider_boundary": directory / "provider_boundary.json",
        "provider_request": directory / "provider_request.json",
    }
    if terminal.family_session_id is None:
        raise ResearchPostprocessError("raw terminal lacks family_session_id")
    paths["family_session_start"] = (
        loaded.collection_root
        / "families"
        / terminal.family_id
        / "sessions"
        / terminal.family_session_id.rsplit(":", 1)[-1]
        / "family_session_start.json"
    )
    for key, path in paths.items():
        expected = terminal.artifact_hashes.get(key)
        if expected is None or hash_file(path) != expected:
            raise ResearchPostprocessError(f"raw lineage hash mismatch for {key}")
    call = _load_canonical(paths["llm_call"], LLMCallRecord)
    attempt = _load_canonical(paths["llm_attempt"], LLMAttemptRecord)
    local_result = _load_canonical(
        paths["local_generation_result"],
        ResearchLocalGenerationResult,
    )
    model_attempt = _load_canonical(
        paths["model_attempt_boundary"],
        ResearchModelAttemptBoundary,
    )
    provider_boundary = _load_canonical(
        paths["provider_boundary"],
        ResearchInvocationBoundary,
    )
    family_session = _load_canonical(
        paths["family_session_start"],
        ResearchFamilySessionStart,
    )
    family_binding = next(
        binding
        for binding in loaded.plan.family_bindings
        if binding.family_id == invocation.family_id
    )
    problem = loaded.problems[invocation.problem_record_id]
    response = verify_llm_call_artifacts(
        call=call,
        problem=problem,
        artifact_root=loaded.repo_root,
    )
    violations = check_llm_call_attempt_lineage(call, (attempt,))
    if violations:
        raise ResearchPostprocessError("invalid LLM call lineage: " + ", ".join(violations))
    if (
        call.call_id != terminal.llm_call_id
        or attempt.attempt_id != terminal.llm_attempt_id
        or call.provider_request_hash != terminal.provider_request_hash
        or call.terminal_status is not LLMCallStatus.COMPLETED
        or call.parse_status is not ParseStatus.EMPTY
        or call.parsed_output is not None
        or call.model_family != invocation.family_id
        or call.model != invocation.model_repo_id
        or call.model_revision != invocation.model_revision
        or call.problem_record_id != invocation.problem_record_id
        or call.decoding.get("seed") != invocation.seed
        or provider_boundary.invocation_id != invocation.invocation_id
        or provider_boundary.provider_request_hash != terminal.provider_request_hash
        or provider_boundary.local_runtime_request_hash != terminal.local_runtime_request_hash
        or provider_boundary.crossed_at != terminal.started_at
        or model_attempt.invocation_id != invocation.invocation_id
        or model_attempt.family_session_id != terminal.family_session_id
        or model_attempt.local_runtime_request_hash != terminal.local_runtime_request_hash
        or model_attempt.started_at < provider_boundary.crossed_at
        or model_attempt.started_at > terminal.completed_at
        or family_session.family_session_id != terminal.family_session_id
        or family_session.family_id != invocation.family_id
        or family_session.model_repo_id != invocation.model_repo_id
        or family_session.model_revision != invocation.model_revision
        or family_session.runtime_hash != family_binding.runtime_hash
        or invocation.invocation_id not in family_session.planned_invocation_ids
        or family_session.loaded_at > model_attempt.started_at
        or local_result.family_session_id != terminal.family_session_id
        or local_result.request_hash != terminal.local_runtime_request_hash
        or local_result.output_hash != terminal.raw_output_sha256
        or local_result.raw_text != response.output_text
        or sha256_hex(response.output_text.encode("utf-8")) != terminal.raw_output_sha256
    ):
        raise ResearchPostprocessError("raw call/runtime/terminal lineage differs")
    raw_path = _resolve_repo_artifact(loaded.repo_root, cast(str, call.raw_output_artifact))
    semantic_hashes = {
        **{key: hash_file(path) for key, path in paths.items()},
        "provider_raw_response": hash_file(raw_path),
    }
    if semantic_hashes["provider_raw_response"] != terminal.artifact_hashes.get(
        "provider_raw_response"
    ):
        raise ResearchPostprocessError("provider raw-response hash differs from terminal")
    lineage_paths = {
        str(path.resolve().relative_to(loaded.repo_root)): hash_file(path)
        for path in (*paths.values(), raw_path)
    }
    return call, attempt, response.output_text, dict(sorted(lineage_paths.items()))


def _persist_record(
    *,
    loaded: LoadedResearchPostprocess,
    invocation_id: str,
    name: str,
    record: StrictModel,
) -> tuple[str, str]:
    path = _output_directory(loaded, invocation_id) / name
    digest = _write_immutable(path, _canonical_record_bytes(record))
    return str(path.resolve().relative_to(loaded.repo_root)), digest


def _persist_jsonl(
    *,
    loaded: LoadedResearchPostprocess,
    invocation_id: str,
    name: str,
    records: tuple[StrictModel, ...],
) -> tuple[str, str]:
    path = _output_directory(loaded, invocation_id) / name
    digest = _write_immutable(path, _canonical_jsonl_bytes(records))
    return str(path.resolve().relative_to(loaded.repo_root)), digest


def _terminal(
    *,
    loaded: LoadedResearchPostprocess,
    invocation: ResearchCollectionInvocation,
    collection_terminal: ResearchCollectionTerminal,
    status: ResearchPostprocessStatus,
    terminal_stage: ResearchPostprocessStage,
    parser_executed: bool,
    lean_validation_executed: bool,
    screening_executed: bool,
    raw_lineage_hashes: dict[str, str],
    output_artifact_hashes: dict[str, str],
    materialized: RealOutputMaterializationResult | None = None,
    screening: CandidateScreeningRecord | None = None,
    admitted: RealOutputMaterializationResult | None = None,
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> ResearchPostprocessTerminal:
    final = admitted or materialized
    payload: dict[str, object] = {
        "schema_version": 1,
        "record_kind": "lf021_research_postprocess_terminal",
        "artifact_class": "research",
        "input_binding_hash": loaded.input_binding.binding_hash,
        "invocation_id": invocation.invocation_id,
        "invocation_payload_hash": hash_canonical(invocation.model_dump(mode="json")),
        "collection_terminal_id": collection_terminal.terminal_id,
        "collection_terminal_sha256": hash_file(
            loaded.collection_terminal_paths[invocation.invocation_id]
        ),
        "family_id": invocation.family_id,
        "problem_record_id": invocation.problem_record_id,
        "seed": invocation.seed,
        "status": status.value,
        "terminal_stage": terminal_stage.value,
        "record_time_basis": collection_terminal.completed_at.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "parser_id": invocation.parser_id,
        "parser_source_sha256": invocation.parser_source_sha256,
        "parser_executed": parser_executed,
        "lean_validation_executed": lean_validation_executed,
        "screening_executed": screening_executed,
        "semantic_pool_admitted": status is ResearchPostprocessStatus.ADMITTED_UNRESOLVED,
        "raw_lineage_hashes": dict(sorted(raw_lineage_hashes.items())),
        "output_artifact_hashes": dict(sorted(output_artifact_hashes.items())),
        "materialization_outcome": (final.outcome.outcome.value if final is not None else None),
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
        "pair_ids": (
            tuple(pair.pair_id for pair in admitted.pairs) if admitted is not None else ()
        ),
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
    terminal_id = "research_postprocess_terminal:" + hash_canonical(
        {"schema": "lf021_research_postprocess_terminal_v1", **payload}
    )
    return ResearchPostprocessTerminal.model_validate({"terminal_id": terminal_id, **payload})


def _failure_terminal(
    *,
    loaded: LoadedResearchPostprocess,
    invocation: ResearchCollectionInvocation,
    collection_terminal: ResearchCollectionTerminal,
    status: ResearchPostprocessStatus,
    stage: ResearchPostprocessStage,
    code: str,
    detail: str,
    raw_lineage_hashes: dict[str, str] | None = None,
    output_artifact_hashes: dict[str, str] | None = None,
    parser_executed: bool = False,
    lean_validation_executed: bool = False,
    materialized: RealOutputMaterializationResult | None = None,
) -> ResearchPostprocessTerminal:
    return _terminal(
        loaded=loaded,
        invocation=invocation,
        collection_terminal=collection_terminal,
        status=status,
        terminal_stage=stage,
        parser_executed=parser_executed,
        lean_validation_executed=lean_validation_executed,
        screening_executed=False,
        raw_lineage_hashes=raw_lineage_hashes or {},
        output_artifact_hashes=output_artifact_hashes or {},
        materialized=materialized,
        failure_code=code,
        failure_detail=redact_exception_message(detail) or "(no detail)",
    )


def _unresolved_pairs(
    admitted: RealOutputMaterializationResult,
) -> RealOutputMaterializationResult:
    metadata = {
        "same_claim": None,
        "relation": None,
        "resolution_outcome": "unresolved",
        "quality_tier": "unknown",
        "requires_adjudication": True,
        "decision": "REVIEW",
        "semantic_labels_created": False,
    }
    pairs = tuple(
        pair.model_copy(update={"metadata": {**pair.metadata, **metadata}})
        for pair in admitted.pairs
    )
    nl_lean = (
        admitted.nl_lean.model_copy(update={"metadata": {**admitted.nl_lean.metadata, **metadata}})
        if admitted.nl_lean is not None
        else None
    )
    return RealOutputMaterializationResult(
        outcome=admitted.outcome,
        variant=admitted.variant,
        theorem=admitted.theorem,
        representation=admitted.representation,
        representation_failures=admitted.representation_failures,
        pairs=pairs,
        nl_lean=nl_lean,
    )


def _canonical_candidate_keys_by_alpha(
    identities: tuple[tuple[str, str, str], ...],
) -> dict[str, tuple[str, str]]:
    """Choose one canonical candidate per alpha group independent of input order.

    Each item is ``(alpha_fingerprint, theorem_id, invocation_id)``.  The
    invocation ID is required as a deterministic tie-breaker because byte-
    identical generated statements intentionally share a theorem ID.
    """

    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for alpha, theorem_id, invocation_id in identities:
        grouped[alpha].append((theorem_id, invocation_id))
    return {alpha: min(values) for alpha, values in sorted(grouped.items())}


def _persist_materialization(
    *,
    loaded: LoadedResearchPostprocess,
    invocation_id: str,
    materialized: RealOutputMaterializationResult,
    prefix: str,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    records: tuple[tuple[str, StrictModel | None], ...] = (
        (f"{prefix}_outcome.json", materialized.outcome),
        (f"{prefix}_variant.json", materialized.variant),
        (f"{prefix}_theorem.json", materialized.theorem),
        (f"{prefix}_representation.json", materialized.representation),
    )
    for name, record in records:
        if record is None:
            continue
        path, digest = _persist_record(
            loaded=loaded,
            invocation_id=invocation_id,
            name=name,
            record=record,
        )
        artifacts[path] = digest
    if materialized.representation_failures:
        failure_path = _output_directory(loaded, invocation_id) / (
            f"{prefix}_representation_failures.jsonl"
        )
        payload = b"".join(
            canonical_json_bytes(dataclasses.asdict(failure)) + b"\n"
            for failure in sorted(
                materialized.representation_failures,
                key=lambda item: (item.theorem_id, item.view, item.status, item.detail),
            )
        )
        artifacts[str(failure_path.resolve().relative_to(loaded.repo_root))] = _write_immutable(
            failure_path,
            payload,
        )
    return artifacts


def _prepare_candidates(
    loaded: LoadedResearchPostprocess,
    *,
    backend: LeanInteractBackend,
) -> tuple[list[_PreparedCandidate], dict[str, ResearchPostprocessTerminal]]:
    family_binding = {binding.family_id: binding for binding in loaded.plan.family_bindings}
    prepared: list[_PreparedCandidate] = []
    terminals: dict[str, ResearchPostprocessTerminal] = {}
    for invocation in sorted(loaded.invocations, key=lambda item: item.invocation_id):
        collection_terminal = loaded.collection_terminals[invocation.invocation_id]
        if collection_terminal.status is not ResearchTerminalStatus.RAW_COLLECTED:
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=ResearchPostprocessStatus.COLLECTION_NOT_RAW,
                stage=ResearchPostprocessStage.COLLECTION,
                code=f"collection_{collection_terminal.status.value}",
                detail=(collection_terminal.error_detail or "raw model output was not collected"),
            )
            continue
        try:
            call, _, raw_output, raw_hashes = _verify_semantic_raw_lineage(
                loaded,
                invocation,
                collection_terminal,
            )
        except Exception as exc:
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=ResearchPostprocessStatus.RAW_LINEAGE_FAILED,
                stage=ResearchPostprocessStage.RAW_LINEAGE,
                code=type(exc).__name__,
                detail=str(exc),
            )
            continue

        binding = family_binding[invocation.family_id]
        try:
            parser = _parser(
                invocation=invocation,
                family_parser_artifact=binding.parser_source_artifact,
                family_parser_sha256=binding.parser_source_sha256,
                repo_root=loaded.repo_root,
            )
            parsed = parser(
                raw_output=raw_output,
                expected_declaration_name=invocation.expected_declaration_name,
                registered_header=loaded.import_header,
                problem=loaded.problems[invocation.problem_record_id],
                context=loaded.context,
                backend=backend,
                created_at=collection_terminal.completed_at,
            )
        except Exception as exc:
            code = exc.code.value if isinstance(exc, FinalFenceError) else type(exc).__name__
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=ResearchPostprocessStatus.PARSE_FAILED,
                stage=ResearchPostprocessStage.PARSER,
                code=code,
                detail=str(exc),
                raw_lineage_hashes=raw_hashes,
                parser_executed=True,
            )
            continue

        parsed_call = LLMCallRecord.model_validate(
            {
                **call.model_dump(mode="json"),
                "parse_status": ParseStatus.PARSED.value,
                "parsed_output": {"lean_statement": parsed.parsed.statement},
                "supervision_eligible": False,
                "metadata": {
                    **call.metadata,
                    "postprocess_parser_id": invocation.parser_id,
                    "postprocess_parser_source_sha256": invocation.parser_source_sha256,
                    "semantic_labels_created": False,
                },
            }
        )
        invocation_artifacts: dict[str, str] = {}
        for name, record in (
            ("parsed_call.json", parsed_call),
            (
                "parsed_candidate.json",
                _ParsedCandidateRecord.from_candidate(
                    invocation=invocation,
                    candidate=parsed,
                    raw_output_sha256=cast(str, collection_terminal.raw_output_sha256),
                ),
            ),
        ):
            path, digest = _persist_record(
                loaded=loaded,
                invocation_id=invocation.invocation_id,
                name=name,
                record=record,
            )
            invocation_artifacts[path] = digest

        problem = loaded.problems[invocation.problem_record_id]
        references = tuple(
            loaded.references[theorem_id] for theorem_id in problem.reference_theorem_ids
        )
        generation_config_hash = hash_canonical(
            {
                "schema": "lf021_research_candidate_generation_v1",
                "plan_id": loaded.plan.plan_id,
                "invocation": invocation.model_dump(mode="json"),
                "input_binding_hash": loaded.input_binding.binding_hash,
            }
        )
        try:
            materialized = materialize_real_output_candidate(
                problem=problem,
                parsed=parsed.parsed,
                call=parsed_call,
                raw_output_artifact=cast(str, parsed_call.raw_output_artifact),
                context=loaded.context,
                references=references,
                imports=loaded.import_header,
                backend=backend,
                generation_config_hash=generation_config_hash,
                created_at=collection_terminal.completed_at,
            )
        except Exception as exc:
            terminals[invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=invocation,
                collection_terminal=collection_terminal,
                status=ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                stage=ResearchPostprocessStage.MATERIALIZATION,
                code=type(exc).__name__,
                detail=str(exc),
                raw_lineage_hashes=raw_hashes,
                output_artifact_hashes=invocation_artifacts,
                parser_executed=True,
                lean_validation_executed=True,
            )
            continue
        invocation_artifacts.update(
            _persist_materialization(
                loaded=loaded,
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
                status=ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                stage=ResearchPostprocessStage.MATERIALIZATION,
                code=(
                    materialized.outcome.failure_code.value
                    if materialized.outcome.failure_code is not None
                    else materialized.outcome.outcome.value
                ),
                detail=materialized.outcome.failure_detail or "candidate did not materialize",
                raw_lineage_hashes=raw_hashes,
                output_artifact_hashes=invocation_artifacts,
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
                output_artifact_hashes=invocation_artifacts,
            )
        )
    return prepared, terminals


class _ParsedCandidateRecord(StrictModel):
    schema_version: Literal[1] = 1
    invocation_id: str
    parser_id: str
    parser_source_sha256: str = Field(pattern=_HEX64)
    raw_output_sha256: str = Field(pattern=_HEX64)
    declaration_kind: str
    declaration_name: str
    statement: str
    statement_sha256: str = Field(pattern=_HEX64)
    parser_source_sha256_observed: str = Field(pattern=_HEX64)
    lean_status: str
    semantic_label: None = None

    @classmethod
    def from_candidate(
        cls,
        *,
        invocation: ResearchCollectionInvocation,
        candidate: LeanExtractedCandidate,
        raw_output_sha256: str,
    ) -> Self:
        return cls(
            invocation_id=invocation.invocation_id,
            parser_id=invocation.parser_id,
            parser_source_sha256=invocation.parser_source_sha256,
            raw_output_sha256=raw_output_sha256,
            declaration_kind=candidate.parsed.declaration_kind,
            declaration_name=candidate.parsed.declaration_name,
            statement=candidate.parsed.statement,
            statement_sha256=candidate.parsed.statement_sha256,
            parser_source_sha256_observed=invocation.parser_source_sha256,
            lean_status=candidate.lean_status.value,
        )


def _screen_and_admit(
    loaded: LoadedResearchPostprocess,
    *,
    prepared: list[_PreparedCandidate],
    terminals: dict[str, ResearchPostprocessTerminal],
) -> None:
    by_alpha: dict[str, list[_PreparedCandidate]] = defaultdict(list)
    for item in prepared:
        representation = item.materialized.representation
        if representation is None or representation.alpha_identity_fingerprint is None:
            terminals[item.invocation.invocation_id] = _failure_terminal(
                loaded=loaded,
                invocation=item.invocation,
                collection_terminal=item.collection_terminal,
                status=ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                stage=ResearchPostprocessStage.MATERIALIZATION,
                code="missing_alpha_identity_fingerprint",
                detail="materialized candidate lacks the required deduplication identity",
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
    canonical_by_alpha = _canonical_candidate_keys_by_alpha(tuple(identity_rows))
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
            priors: tuple[PriorCandidateIdentity, ...]
            if (theorem.theorem_id, item.invocation.invocation_id) == canonical_key:
                # The canonical member is screened without its same-alpha
                # siblings, so exactly one deterministic representative can
                # be admitted.  Noncanonical siblings bind the canonical ID.
                priors = ()
            else:
                priors = (
                    PriorCandidateIdentity(
                        theorem_id=canonical_theorem.theorem_id,
                        alpha_identity_fingerprint=alpha,
                    ),
                )
            screening = screen_materialized_candidate(
                index=CandidateScreeningIndex(
                    denylist=loaded.denylist,
                    prior_candidates=priors,
                ),
                problem_record_id=item.problem.problem_record_id,
                call_id=item.parsed_call.call_id,
                theorem=theorem,
                representation=representation,
                created_at=item.collection_terminal.completed_at,
            )
            output_hashes = dict(item.output_artifact_hashes)
            raw_hashes = dict(item.raw_lineage_hashes)
            path, digest = _persist_record(
                loaded=loaded,
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
                    status=ResearchPostprocessStatus.SCREEN_REJECTED,
                    terminal_stage=ResearchPostprocessStage.SCREENING,
                    parser_executed=True,
                    lean_validation_executed=True,
                    screening_executed=True,
                    raw_lineage_hashes=raw_hashes,
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
                    expected_frozen_registry_hash=loaded.denylist.registry_content_hash,
                    created_at=item.collection_terminal.completed_at,
                )
                admitted = _unresolved_pairs(admitted)
            except Exception as exc:
                terminals[item.invocation.invocation_id] = _terminal(
                    loaded=loaded,
                    invocation=item.invocation,
                    collection_terminal=item.collection_terminal,
                    status=ResearchPostprocessStatus.MATERIALIZATION_FAILED,
                    terminal_stage=ResearchPostprocessStage.ADMISSION,
                    parser_executed=True,
                    lean_validation_executed=True,
                    screening_executed=True,
                    raw_lineage_hashes=raw_hashes,
                    output_artifact_hashes=output_hashes,
                    materialized=item.materialized,
                    screening=screening,
                    failure_code=type(exc).__name__,
                    failure_detail=redact_exception_message(str(exc)) or "(no detail)",
                )
                continue
            output_hashes.update(
                _persist_materialization(
                    loaded=loaded,
                    invocation_id=item.invocation.invocation_id,
                    materialized=admitted,
                    prefix="admitted",
                )
            )
            pair_path, pair_digest = _persist_jsonl(
                loaded=loaded,
                invocation_id=item.invocation.invocation_id,
                name="unresolved_pairs.jsonl",
                records=cast(tuple[StrictModel, ...], admitted.pairs),
            )
            output_hashes[pair_path] = pair_digest
            assert admitted.nl_lean is not None
            nl_path, nl_digest = _persist_record(
                loaded=loaded,
                invocation_id=item.invocation.invocation_id,
                name="unresolved_nl_lean.json",
                record=admitted.nl_lean,
            )
            output_hashes[nl_path] = nl_digest
            terminals[item.invocation.invocation_id] = _terminal(
                loaded=loaded,
                invocation=item.invocation,
                collection_terminal=item.collection_terminal,
                status=ResearchPostprocessStatus.ADMITTED_UNRESOLVED,
                terminal_stage=ResearchPostprocessStage.COMPLETE,
                parser_executed=True,
                lean_validation_executed=True,
                screening_executed=True,
                raw_lineage_hashes=raw_hashes,
                output_artifact_hashes=output_hashes,
                materialized=item.materialized,
                screening=screening,
                admitted=admitted,
            )


def _family_report_payload(
    loaded: LoadedResearchPostprocess,
    *,
    family_id: str,
    selected: tuple[ResearchPostprocessTerminal, ...],
) -> dict[str, object]:
    if len(selected) != 3:
        raise ResearchPostprocessError(f"family denominator is not three: {family_id}")
    if any(item.family_id != family_id for item in selected):
        raise ResearchPostprocessError(f"family report contains a foreign terminal: {family_id}")
    counts = Counter(terminal.status.value for terminal in selected)
    return {
        "schema_version": 1,
        "input_binding_hash": loaded.input_binding.binding_hash,
        "family_id": family_id,
        "expected_invocations": 3,
        "terminal_invocations": 3,
        "status_counts": dict(sorted(counts.items())),
        "collection_raw_count": sum(
            loaded.collection_terminals[item.invocation_id].status
            is ResearchTerminalStatus.RAW_COLLECTED
            for item in selected
        ),
        "parser_success_count": sum(
            item.parser_executed
            and item.status
            not in {
                ResearchPostprocessStatus.PARSE_FAILED,
                ResearchPostprocessStatus.RAW_LINEAGE_FAILED,
            }
            for item in selected
        ),
        "materialized_pending_screen_count": sum(
            item.materialization_outcome
            in {
                RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING.value,
                RealOutputOutcomeCode.MATERIALIZED.value,
            }
            for item in selected
        ),
        "screening_clean_count": sum(
            item.screening_status == CandidateScreeningStatus.CLEAN.value for item in selected
        ),
        "admitted_unresolved_count": sum(
            item.status is ResearchPostprocessStatus.ADMITTED_UNRESOLVED for item in selected
        ),
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }


def _write_terminals_and_reports(
    loaded: LoadedResearchPostprocess,
    terminals_by_id: dict[str, ResearchPostprocessTerminal],
) -> ResearchPostprocessRun:
    if set(terminals_by_id) != set(loaded.input_binding.invocation_ids):
        missing = sorted(set(loaded.input_binding.invocation_ids) - set(terminals_by_id))
        raise ResearchPostprocessError(
            "postprocess denominator is incomplete: " + ", ".join(missing)
        )
    terminals = tuple(terminals_by_id[key] for key in sorted(terminals_by_id))
    terminal_artifacts: dict[str, str] = {}
    for terminal in terminals:
        path = _output_directory(loaded, terminal.invocation_id) / "processing_terminal.json"
        digest = _write_immutable(path, _canonical_record_bytes(terminal))
        terminal_artifacts[str(path.resolve().relative_to(loaded.repo_root))] = digest

    family_reports: list[ResearchPostprocessFamilyReport] = []
    family_report_artifacts: dict[str, str] = {}
    for family_id in loaded.input_binding.family_ids:
        selected = tuple(terminal for terminal in terminals if terminal.family_id == family_id)
        payload = _family_report_payload(
            loaded,
            family_id=family_id,
            selected=selected,
        )
        report_id = "research_postprocess_family:" + hash_canonical(
            {"schema": "lf021_research_postprocess_family_v1", **payload}
        )
        report = ResearchPostprocessFamilyReport.model_validate({"report_id": report_id, **payload})
        path = loaded.output_root / "families" / f"{family_id}.json"
        digest = _write_immutable(path, _canonical_record_bytes(report))
        family_report_artifacts[str(path.resolve().relative_to(loaded.repo_root))] = digest
        family_reports.append(report)

    counts = Counter(terminal.status.value for terminal in terminals)
    admitted = tuple(
        terminal
        for terminal in terminals
        if terminal.status is ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    )
    payload = {
        "schema_version": 1,
        "input_binding": loaded.input_binding.model_dump(mode="json"),
        "input_binding_hash": loaded.input_binding.binding_hash,
        "expected_invocations": 9,
        "terminal_invocations": 9,
        "family_count": 3,
        "status_counts": dict(sorted(counts.items())),
        "terminal_artifacts": dict(sorted(terminal_artifacts.items())),
        "family_report_artifacts": dict(sorted(family_report_artifacts.items())),
        "admitted_pair_count": sum(len(terminal.pair_ids) for terminal in admitted),
        "admitted_nl_lean_count": len(admitted),
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    manifest_id = "research_postprocess_manifest:" + hash_canonical(
        {"schema": "lf021_research_postprocess_manifest_v1", **payload}
    )
    manifest = ResearchPostprocessManifest.model_validate({"manifest_id": manifest_id, **payload})
    manifest_path = loaded.output_root / "manifest.json"
    _write_immutable(manifest_path, _canonical_record_bytes(manifest))
    return ResearchPostprocessRun(
        output_root=loaded.output_root,
        manifest_path=manifest_path,
        manifest=manifest,
        terminals=terminals,
        family_reports=tuple(family_reports),
    )


def run_research_postprocess(
    loaded: LoadedResearchPostprocess,
    *,
    backend: LeanInteractBackend,
) -> ResearchPostprocessRun:
    """Process and replay-verify the exact frozen nine-invocation denominator."""

    prepared, terminals = _prepare_candidates(loaded, backend=backend)
    _screen_and_admit(loaded, prepared=prepared, terminals=terminals)
    return _write_terminals_and_reports(loaded, terminals)


def verify_research_postprocess(
    loaded: LoadedResearchPostprocess,
) -> ResearchPostprocessManifest:
    """Reload all terminal/report bytes and verify their input/output bindings."""

    bindings = (
        loaded.input_binding.collection_plan,
        loaded.input_binding.collection_manifest,
        loaded.input_binding.problem_pool_records,
        loaded.input_binding.context,
        loaded.input_binding.import_header,
        loaded.input_binding.reference_theorems,
        loaded.input_binding.reference_representations,
        loaded.input_binding.implementation,
        *loaded.input_binding.active_registry_artifacts.values(),
    )
    for binding in bindings:
        path = _resolve_bound_artifact(loaded.repo_root, binding)
        if hash_file(path) != binding.sha256:
            raise ResearchPostprocessError(
                f"postprocess input artifact hash mismatch: {binding.artifact}"
            )
    for artifact, expected in loaded.input_binding.collection_terminal_artifacts.items():
        path = _resolve_repo_artifact(loaded.repo_root, artifact)
        if hash_file(path) != expected:
            raise ResearchPostprocessError(f"collection terminal input hash mismatch: {artifact}")
    for artifact, expected in loaded.input_binding.collection_family_session_artifacts.items():
        path = _resolve_repo_artifact(loaded.repo_root, artifact)
        if hash_file(path) != expected:
            raise ResearchPostprocessError(
                f"collection family-session input hash mismatch: {artifact}"
            )
    manifest = _load_canonical(
        loaded.output_root / "manifest.json",
        ResearchPostprocessManifest,
    )
    if manifest.input_binding != loaded.input_binding:
        raise ResearchPostprocessError("persisted postprocess input binding has drifted")
    invocation_by_id = {item.invocation_id: item for item in loaded.invocations}
    if set(invocation_by_id) != set(loaded.input_binding.invocation_ids):
        raise ResearchPostprocessError("loaded invocation denominator differs from input binding")
    terminals: list[ResearchPostprocessTerminal] = []
    for artifact, expected in manifest.terminal_artifacts.items():
        path = _resolve_repo_artifact(loaded.repo_root, artifact)
        if hash_file(path) != expected:
            raise ResearchPostprocessError(f"postprocess terminal hash mismatch: {artifact}")
        terminal = _load_canonical(path, ResearchPostprocessTerminal)
        expected_terminal_path = (
            _output_directory(loaded, terminal.invocation_id) / "processing_terminal.json"
        )
        if path != expected_terminal_path.resolve():
            raise ResearchPostprocessError(
                f"postprocess terminal is stored at an unexpected path: {artifact}"
            )
        if terminal.input_binding_hash != loaded.input_binding.binding_hash:
            raise ResearchPostprocessError(
                f"terminal input binding differs: {terminal.invocation_id}"
            )
        invocation = invocation_by_id.get(terminal.invocation_id)
        collection_terminal = loaded.collection_terminals.get(terminal.invocation_id)
        collection_path = loaded.collection_terminal_paths.get(terminal.invocation_id)
        if invocation is None or collection_terminal is None or collection_path is None:
            raise ResearchPostprocessError(
                f"terminal has no frozen collection input: {terminal.invocation_id}"
            )
        if (
            terminal.invocation_payload_hash != hash_canonical(invocation.model_dump(mode="json"))
            or terminal.collection_terminal_id != collection_terminal.terminal_id
            or terminal.collection_terminal_sha256 != hash_file(collection_path)
            or terminal.family_id != invocation.family_id
            or terminal.problem_record_id != invocation.problem_record_id
            or terminal.seed != invocation.seed
        ):
            raise ResearchPostprocessError(
                f"terminal collection lineage differs: {terminal.invocation_id}"
            )
        terminals.append(terminal)
        bound_artifacts = {
            **terminal.raw_lineage_hashes,
            **terminal.output_artifact_hashes,
        }
        if len(bound_artifacts) != (
            len(terminal.raw_lineage_hashes) + len(terminal.output_artifact_hashes)
        ):
            raise ResearchPostprocessError(
                f"raw/output artifact path collision: {terminal.invocation_id}"
            )
        for bound_artifact, bound_hash in bound_artifacts.items():
            output_path = _resolve_repo_artifact(loaded.repo_root, bound_artifact)
            if hash_file(output_path) != bound_hash:
                raise ResearchPostprocessError(
                    f"postprocess bound artifact hash mismatch: {bound_artifact}"
                )
    if tuple(sorted(item.invocation_id for item in terminals)) != (
        loaded.input_binding.invocation_ids
    ):
        raise ResearchPostprocessError("persisted postprocess terminal denominator differs")
    terminal_counts = dict(sorted(Counter(item.status.value for item in terminals).items()))
    admitted = tuple(
        item for item in terminals if item.status is ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    )
    if (
        terminal_counts != manifest.status_counts
        or sum(len(item.pair_ids) for item in admitted) != manifest.admitted_pair_count
        or len(admitted) != manifest.admitted_nl_lean_count
    ):
        raise ResearchPostprocessError("postprocess manifest accounting differs from terminals")
    reports_by_family: dict[str, ResearchPostprocessFamilyReport] = {}
    for artifact, expected in manifest.family_report_artifacts.items():
        path = _resolve_repo_artifact(loaded.repo_root, artifact)
        if hash_file(path) != expected:
            raise ResearchPostprocessError(f"family report hash mismatch: {artifact}")
        report = _load_canonical(path, ResearchPostprocessFamilyReport)
        expected_report_path = loaded.output_root / "families" / f"{report.family_id}.json"
        if path != expected_report_path.resolve():
            raise ResearchPostprocessError(
                f"family report is stored at an unexpected path: {artifact}"
            )
        if report.family_id in reports_by_family:
            raise ResearchPostprocessError(f"duplicate family report: {report.family_id}")
        reports_by_family[report.family_id] = report
    if set(reports_by_family) != set(loaded.input_binding.family_ids):
        raise ResearchPostprocessError("postprocess family-report denominator differs")
    for family_id, report in reports_by_family.items():
        selected = tuple(item for item in terminals if item.family_id == family_id)
        expected_payload = _family_report_payload(
            loaded,
            family_id=family_id,
            selected=selected,
        )
        observed_payload = report.model_dump(mode="json", exclude={"report_id"})
        if observed_payload != expected_payload:
            raise ResearchPostprocessError(
                f"postprocess family report accounting differs: {family_id}"
            )
    return manifest


__all__ = [
    "LoadedResearchPostprocess",
    "ResearchPostprocessArtifactConflict",
    "ResearchPostprocessError",
    "ResearchPostprocessFamilyReport",
    "ResearchPostprocessInputBinding",
    "ResearchPostprocessManifest",
    "ResearchPostprocessRun",
    "ResearchPostprocessStage",
    "ResearchPostprocessStatus",
    "ResearchPostprocessTerminal",
    "load_research_postprocess",
    "run_research_postprocess",
    "verify_research_postprocess",
]
