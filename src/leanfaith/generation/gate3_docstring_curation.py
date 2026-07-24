"""Conservative operational curation of Gate-3 mathlib docstring candidates.

This stage consumes the immutable, model-free candidate-source artifact built
by :mod:`leanfaith.generation.gate3_docstring_pool`.  It never edits that
artifact.  The output records an LLM-assisted operational judgement about
whether each docstring is sufficiently standalone to be sent to a local
formalizer.  It is not human review, a semantic-equivalence label, or gate
evidence.

Every admitted record retains the complete pinned Lean context, the Gate-3
``signature_pp``, and an exact Lean alias whose type is inferred from the
source declaration already present in the pinned mathlib checkout. The
reference is retained for later scoring; it is explicitly not part of the
generator prompt.
"""

from __future__ import annotations

import datetime
import json
import os
import stat
import subprocess
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.generation.gate3_docstring_pool import (
    Gate3DocstringPoolManifest,
    Gate3MathlibDocstringCandidate,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord

CONFIG_PATH = Path("configs/generation/problem_pool_gate3_mathlib_docstrings_curation_v1.yaml")
REPORT_PATH = Path("reports/generation/lf021_gate3_mathlib_docstrings_curation_v1.json")
DEFAULT_OUTPUT_ROOT = Path(
    "/storage/milikic/leanfaith/lf021/problem_pool_gate3_mathlib_docstrings_curation_v1"
)

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"


class Gate3DocstringCurationError(RuntimeError):
    """The operational curation cannot be reproduced safely."""


class CurationDecision(StrEnum):
    STANDALONE_SUFFICIENT = "standalone_sufficient"
    REFERENTIAL_ONLY = "referential_only"
    EXTERNAL_CONTEXT_REQUIRED = "external_context_required"
    INTERNAL_DOCUMENTATION_ONLY = "internal_documentation_only"
    UNDER_SPECIFIED = "under_specified"
    AMBIGUOUS_OPERATIONAL = "ambiguous_operational"


_EXCLUSION_DECISIONS = frozenset(
    {
        CurationDecision.REFERENTIAL_ONLY,
        CurationDecision.EXTERNAL_CONTEXT_REQUIRED,
        CurationDecision.INTERNAL_DOCUMENTATION_ONLY,
        CurationDecision.UNDER_SPECIFIED,
        CurationDecision.AMBIGUOUS_OPERATIONAL,
    }
)


class ArtifactBinding(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class CurationInputsConfig(StrictModel):
    candidate_manifest: ArtifactBinding
    eligible_candidates: ArtifactBinding
    upstream_report: ArtifactBinding
    theorem_records: ArtifactBinding
    representation_records: ArtifactBinding
    expected_candidate_count: Literal[57]


class CurationExecutionContextConfig(StrictModel):
    project_registry_key: Literal["mathlib"]
    project_revision: str = Field(pattern=_HEX40)
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    environment_schema_version: Literal[1]
    import_header: ArtifactBinding


class ExclusionSpec(StrictModel):
    candidate_id: str = Field(pattern=r"^gate3_docstring_candidate:[0-9a-f]{64}$")
    declaration_full_name: str = Field(min_length=1)
    decision: CurationDecision
    reason_code: str = Field(pattern=r"^[a-z0-9_]+$")
    rationale: str = Field(min_length=20)

    @model_validator(mode="after")
    def _is_exclusion(self) -> ExclusionSpec:
        if self.decision not in _EXCLUSION_DECISIONS:
            raise ValueError("exclusion spec must use a terminal exclusion decision")
        return self


class CurationReviewConfig(StrictModel):
    reviewer_type: Literal["codex_agent"]
    review_method: Literal["llm_assisted_operational_curation_v1"]
    human_reviewed: Literal[False]
    semantic_gold_created: Literal[False]
    default_decision: Literal[CurationDecision.STANDALONE_SUFFICIENT]
    default_reason_code: str = Field(pattern=r"^[a-z0-9_]+$")
    default_rationale: str = Field(min_length=40)
    exclusions: tuple[ExclusionSpec, ...]

    @model_validator(mode="after")
    def _unique_exclusions(self) -> CurationReviewConfig:
        candidate_ids = [item.candidate_id for item in self.exclusions]
        declarations = [item.declaration_full_name for item in self.exclusions]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("exclusion candidate IDs must be unique")
        if len(declarations) != len(set(declarations)):
            raise ValueError("exclusion declaration names must be unique")
        return self


class ExpectedCounts(StrictModel):
    reviewed: Literal[57]
    admitted: Literal[40]
    excluded: Literal[17]
    ambiguous_exclusions: Literal[2]


class CurationOutputsConfig(StrictModel):
    root: str = Field(min_length=1)
    report: str = Field(min_length=1)

    @model_validator(mode="after")
    def _safe_report(self) -> CurationOutputsConfig:
        path = PurePosixPath(self.report)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("report path must be repository-relative")
        return self


class CurationPolicyConfig(StrictModel):
    candidate_source_records_only: Literal[False]
    problem_pool_admitted_for_operational_collection: Literal[True]
    human_review_claimed: Literal[False]
    semantic_labels_created: Literal[False]
    gate_claimed: Literal[False]
    model_execution_performed: Literal[False]
    graph_required: Literal[False]


class Gate3DocstringCurationConfig(StrictModel):
    schema_version: Literal[1]
    config_id: Literal["gate3_mathlib_docstring_operational_curation_v1"]
    frozen_at: datetime.datetime
    inputs: CurationInputsConfig
    execution_context: CurationExecutionContextConfig
    review: CurationReviewConfig
    expected_counts: ExpectedCounts
    outputs: CurationOutputsConfig
    policy: CurationPolicyConfig

    @model_validator(mode="after")
    def _consistent(self) -> Gate3DocstringCurationConfig:
        require_utc(self.frozen_at)
        if len(self.review.exclusions) != self.expected_counts.excluded:
            raise ValueError("configured exclusion count does not match expected_counts")
        ambiguous = sum(
            item.decision is CurationDecision.AMBIGUOUS_OPERATIONAL
            for item in self.review.exclusions
        )
        if ambiguous != self.expected_counts.ambiguous_exclusions:
            raise ValueError("ambiguous exclusion count does not match expected_counts")
        if self.expected_counts.admitted + self.expected_counts.excluded != (
            self.expected_counts.reviewed
        ):
            raise ValueError("admitted and excluded counts do not reconcile reviewed")
        return self


class OperationalReview(StrictModel):
    reviewer_type: Literal["codex_agent"]
    review_method: Literal["llm_assisted_operational_curation_v1"]
    human_reviewed: Literal[False] = False
    semantic_gold_created: Literal[False] = False
    decision: CurationDecision
    reason_code: str = Field(pattern=r"^[a-z0-9_]+$")
    rationale: str = Field(min_length=20)
    ambiguous_exclusion: bool
    model_collection_authorized: bool
    authorization_scope: Literal["local_models_only", "none"]
    reference_visible_to_generator: Literal[False] = False

    @model_validator(mode="after")
    def _decision_semantics(self) -> OperationalReview:
        admitted = self.decision is CurationDecision.STANDALONE_SUFFICIENT
        if self.model_collection_authorized != admitted:
            raise ValueError("collection authorization must match standalone decision")
        expected_scope = "local_models_only" if admitted else "none"
        if self.authorization_scope != expected_scope:
            raise ValueError("authorization_scope does not match decision")
        expected_ambiguous = self.decision is CurationDecision.AMBIGUOUS_OPERATIONAL
        if self.ambiguous_exclusion != expected_ambiguous:
            raise ValueError("ambiguous_exclusion does not match decision")
        return self


class ReferenceContext(StrictModel):
    project_registry_key: Literal["mathlib"]
    project_revision: str = Field(pattern=_HEX40)
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    environment_schema_version: Literal[1]
    import_header_artifact: str = Field(min_length=1)
    import_header_sha256: str = Field(pattern=_HEX64)
    import_header_text: Literal["import Mathlib\n"]


class ReferenceLeanStatement(StrictModel):
    theorem_id: str = Field(pattern=r"^thm:[0-9a-f]{64}$")
    representation_id: str = Field(pattern=r"^repr:[0-9a-f]{64}$")
    theorem_statement_content_hash: str = Field(pattern=_HEX64)
    representation_content_hash: str = Field(pattern=_HEX64)
    source_declaration_full_name: str = Field(min_length=1)
    reference_declaration_name: str = Field(pattern=r"^LeanFaithCurationReference_[0-9a-f]{16}$")
    reference_type_pp: str = Field(min_length=1)
    reference_lean_statement: str = Field(min_length=1)
    reference_statement_sha256: str = Field(pattern=_HEX64)
    binding_method: Literal["gate3_signature_pp_with_inferred_exact_pinned_declaration_alias_v1"]
    elaboration_status: Literal["valid"]
    lean_request_hash: str = Field(pattern=_HEX64)
    raw_response_artifact: ArtifactBinding

    @model_validator(mode="after")
    def _statement_shape(self) -> ReferenceLeanStatement:
        expected_hash = hash_canonical(
            {
                "schema": "gate3_docstring_reference_statement_text_v1",
                "text": self.reference_lean_statement,
            }
        )
        if self.reference_statement_sha256 != expected_hash:
            raise ValueError("reference statement hash does not match text")
        if f"def {self.reference_declaration_name} :=" not in self.reference_lean_statement:
            raise ValueError("reference declaration name is absent from statement")
        if f":= @{self.source_declaration_full_name}" not in self.reference_lean_statement:
            raise ValueError("reference statement is not bound to the pinned declaration")
        return self


class OperationalCurationRecord(StrictModel):
    schema_version: Literal[1] = 1
    decision_id: str = Field(pattern=r"^gate3_docstring_curation:[0-9a-f]{64}$")
    source_candidate_artifact_sha256: str = Field(pattern=_HEX64)
    source_candidate: Gate3MathlibDocstringCandidate
    review: OperationalReview
    reference_context: ReferenceContext | None
    reference: ReferenceLeanStatement | None
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    human_review_claimed: Literal[False] = False
    gate_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _identity_and_shape(self) -> OperationalCurationRecord:
        admitted = self.review.model_collection_authorized
        if admitted != (self.reference_context is not None and self.reference is not None):
            raise ValueError("only admitted records may retain collection reference material")
        if self.reference is not None:
            if self.reference.theorem_id != self.source_candidate.theorem_id:
                raise ValueError("reference theorem does not match source candidate")
            if self.reference.representation_id != self.source_candidate.representation_id:
                raise ValueError("reference representation does not match source candidate")
        expected = "gate3_docstring_curation:" + hash_canonical(
            {
                "schema": "gate3_docstring_operational_curation_record_v1",
                "source_candidate_artifact_sha256": self.source_candidate_artifact_sha256,
                "candidate_id": self.source_candidate.candidate_id,
                "decision": self.review.decision.value,
                "reason_code": self.review.reason_code,
                "rationale": self.review.rationale,
                "reference_statement_sha256": (
                    self.reference.reference_statement_sha256
                    if self.reference is not None
                    else None
                ),
            }
        )
        if self.decision_id != expected:
            raise ValueError("decision_id does not match immutable curation payload")
        return self


class CurationArtifactManifest(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["gate3_docstring_operational_curation_manifest_v1"]
    frozen_at: datetime.datetime
    config_hash: str = Field(pattern=_HEX64)
    input_artifacts: dict[str, ArtifactBinding]
    output_artifacts: dict[str, ArtifactBinding]
    reviewed_count: Literal[57]
    admitted_count: Literal[40]
    excluded_count: Literal[17]
    ambiguous_exclusion_count: Literal[2]
    decision_counts: dict[str, int]
    admitted_candidate_ids: tuple[str, ...]
    reviewer_type: Literal["codex_agent"]
    review_method: Literal["llm_assisted_operational_curation_v1"]
    human_reviewed: Literal[False] = False
    semantic_gold_created: Literal[False] = False
    model_execution_performed: Literal[False] = False
    gate_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _accounting(self) -> CurationArtifactManifest:
        require_utc(self.frozen_at)
        if sum(self.decision_counts.values()) != self.reviewed_count:
            raise ValueError("decision counts do not reconcile reviewed_count")
        if len(self.admitted_candidate_ids) != self.admitted_count:
            raise ValueError("admitted IDs do not reconcile admitted_count")
        if len(set(self.admitted_candidate_ids)) != len(self.admitted_candidate_ids):
            raise ValueError("admitted candidate IDs must be unique")
        return self


class CurationReport(StrictModel):
    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_gate3_docstring_operational_curation_v1"]
    passed: Literal[True]
    manifest: ArtifactBinding
    decisions: ArtifactBinding
    admitted: ArtifactBinding
    excluded: ArtifactBinding
    reference_checks: ArtifactBinding
    reviewed_count: Literal[57]
    admitted_count: Literal[40]
    excluded_count: Literal[17]
    ambiguous_exclusion_count: Literal[2]
    reviewer_type: Literal["codex_agent"]
    human_reviewed: Literal[False] = False
    semantic_gold_created: Literal[False] = False
    model_execution_performed: Literal[False] = False
    gate_claimed: Literal[False] = False
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Gate3DocstringCurationRun:
    report_path: Path
    manifest_path: Path
    report: CurationReport
    manifest: CurationArtifactManifest


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Gate3DocstringCurationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise Gate3DocstringCurationError(f"cannot load JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise Gate3DocstringCurationError(f"{path}: expected JSON object")
    return value


def _iter_jsonl(path: Path) -> Iterator[dict[str, object]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise Gate3DocstringCurationError(
                        f"{path}:{line_number}: invalid JSON: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise Gate3DocstringCurationError(f"{path}:{line_number}: expected JSON object")
                yield value
    except OSError as exc:
        raise Gate3DocstringCurationError(f"cannot read {path}: {exc}") from exc


def _resolve_artifact(root: Path, binding: ArtifactBinding) -> Path:
    path = Path(binding.path)
    return path if path.is_absolute() else root / path


def _verify_artifact(root: Path, binding: ArtifactBinding) -> Path:
    path = _resolve_artifact(root, binding)
    if not path.is_file():
        raise Gate3DocstringCurationError(f"required artifact is absent: {path}")
    observed = hash_file(path)
    if observed != binding.sha256:
        raise Gate3DocstringCurationError(
            f"artifact hash mismatch for {path}: expected {binding.sha256}, got {observed}"
        )
    return path


def _git_head(project_dir: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Gate3DocstringCurationError(
            f"cannot verify pinned mathlib checkout {project_dir}: {exc}"
        ) from exc


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise Gate3DocstringCurationError(f"refusing to overwrite curation artifact {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _jsonl_bytes(records: tuple[dict[str, object], ...]) -> bytes:
    return b"".join(canonical_json_bytes(record) + b"\n" for record in records)


def _artifact(path: Path, *, root: Path | None = None) -> ArtifactBinding:
    rendered = str(path.relative_to(root)) if root is not None else str(path)
    return ArtifactBinding(path=rendered, sha256=hash_file(path))


def _load_candidates(
    *,
    root: Path,
    config: Gate3DocstringCurationConfig,
) -> tuple[Gate3MathlibDocstringCandidate, ...]:
    manifest_path = _verify_artifact(root, config.inputs.candidate_manifest)
    eligible_path = _verify_artifact(root, config.inputs.eligible_candidates)
    _verify_artifact(root, config.inputs.upstream_report)
    manifest = Gate3DocstringPoolManifest.model_validate(_load_json(manifest_path))
    if manifest.profile != "full":
        raise Gate3DocstringCurationError("upstream candidate manifest is not the full profile")
    if manifest.eligible_distinct_ancestry_groups != config.inputs.expected_candidate_count:
        raise Gate3DocstringCurationError("upstream candidate count changed")
    if not (
        manifest.candidate_source_records_only
        and manifest.self_containedness_status == "unreviewed"
        and not manifest.problem_pool_admitted
        and not manifest.model_collection_authorized
        and not manifest.model_execution_performed
        and not manifest.semantic_labels_created
        and not manifest.gate_claimed
    ):
        raise Gate3DocstringCurationError("upstream candidate policy boundary changed")
    candidates = tuple(
        Gate3MathlibDocstringCandidate.model_validate(record)
        for record in _iter_jsonl(eligible_path)
    )
    if len(candidates) != config.inputs.expected_candidate_count:
        raise Gate3DocstringCurationError("eligible candidate partition count changed")
    ids = [item.candidate_id for item in candidates]
    if len(ids) != len(set(ids)):
        raise Gate3DocstringCurationError("eligible candidate IDs are not unique")
    if any(
        not item.registry_screens.all_three_screens_clear
        or not item.shared_three_family_temporal_eligible
        or not item.temporal_introduction.strictly_postdates_latest_checkpoint
        for item in candidates
    ):
        raise Gate3DocstringCurationError(
            "registry-screen or temporal eligibility changed upstream"
        )
    return candidates


def _load_references(
    *,
    root: Path,
    config: Gate3DocstringCurationConfig,
    candidates: tuple[Gate3MathlibDocstringCandidate, ...],
) -> tuple[dict[str, TheoremRecord], dict[str, RepresentationRecord]]:
    theorem_path = _verify_artifact(root, config.inputs.theorem_records)
    representation_path = _verify_artifact(root, config.inputs.representation_records)
    theorem_ids = {item.theorem_id for item in candidates}
    representation_ids = {item.representation_id for item in candidates}

    theorems: dict[str, TheoremRecord] = {}
    for wrapper in _iter_jsonl(theorem_path):
        raw = wrapper.get("theorem")
        if not isinstance(raw, dict):
            raise Gate3DocstringCurationError("Gate-3 theorem wrapper is malformed")
        theorem_id = raw.get("theorem_id")
        if theorem_id in theorem_ids:
            theorem = TheoremRecord.model_validate(raw)
            if theorem.theorem_id in theorems:
                raise Gate3DocstringCurationError(f"duplicate theorem record {theorem.theorem_id}")
            theorems[theorem.theorem_id] = theorem

    representations: dict[str, RepresentationRecord] = {}
    for raw in _iter_jsonl(representation_path):
        representation_id = raw.get("representation_id")
        if representation_id in representation_ids:
            representation = RepresentationRecord.model_validate(raw)
            if representation.representation_id in representations:
                raise Gate3DocstringCurationError(
                    f"duplicate representation record {representation.representation_id}"
                )
            representations[representation.representation_id] = representation

    if set(theorems) != theorem_ids:
        missing = sorted(theorem_ids - set(theorems))
        raise Gate3DocstringCurationError(f"missing theorem records: {missing[:3]}")
    if set(representations) != representation_ids:
        missing = sorted(representation_ids - set(representations))
        raise Gate3DocstringCurationError(f"missing representation records: {missing[:3]}")

    for candidate in candidates:
        theorem = theorems[candidate.theorem_id]
        representation = representations[candidate.representation_id]
        if theorem.context_id != config.execution_context.context_id:
            raise Gate3DocstringCurationError("candidate theorem context drifted")
        if theorem.statement_content_hash != candidate.theorem_statement_content_hash:
            raise Gate3DocstringCurationError("candidate theorem content hash drifted")
        if representation.theorem_id != theorem.theorem_id:
            raise Gate3DocstringCurationError("candidate representation theorem drifted")
        if representation.content_hash != candidate.representation_content_hash:
            raise Gate3DocstringCurationError("candidate representation content hash drifted")
        if not representation.signature_pp:
            raise Gate3DocstringCurationError("candidate lacks signature_pp reference")
        if "⋯" in representation.signature_pp:
            raise Gate3DocstringCurationError(
                "candidate signature_pp contains an unresolved pretty-print placeholder"
            )
    return theorems, representations


def _review_for(
    *,
    config: Gate3DocstringCurationConfig,
    candidate: Gate3MathlibDocstringCandidate,
) -> OperationalReview:
    exclusions = {item.candidate_id: item for item in config.review.exclusions}
    exclusion = exclusions.get(candidate.candidate_id)
    if exclusion is None:
        decision = CurationDecision.STANDALONE_SUFFICIENT
        reason_code = config.review.default_reason_code
        rationale = config.review.default_rationale
    else:
        if exclusion.declaration_full_name != candidate.declaration_full_name:
            raise Gate3DocstringCurationError(
                f"{candidate.candidate_id}: configured declaration binding drifted"
            )
        decision = exclusion.decision
        reason_code = exclusion.reason_code
        rationale = exclusion.rationale
    admitted = decision is CurationDecision.STANDALONE_SUFFICIENT
    return OperationalReview(
        reviewer_type=config.review.reviewer_type,
        review_method=config.review.review_method,
        decision=decision,
        reason_code=reason_code,
        rationale=rationale,
        ambiguous_exclusion=decision is CurationDecision.AMBIGUOUS_OPERATIONAL,
        model_collection_authorized=admitted,
        authorization_scope="local_models_only" if admitted else "none",
    )


def _reference_code(
    *,
    candidate: Gate3MathlibDocstringCandidate,
    representation: RepresentationRecord,
) -> tuple[str, str]:
    if representation.signature_pp is None:
        raise Gate3DocstringCurationError("reference lacks signature_pp")
    reference_name = "LeanFaithCurationReference_" + candidate.candidate_id[-16:]
    # Let Lean infer the exact universe-polymorphic type from the pinned
    # declaration. Re-elaborating ``signature_pp`` is intentionally avoided:
    # pretty printing can hide local instance terms (for example a ``letI``
    # induced through an equivalence) even though it remains the canonical
    # human-readable reference type retained below.
    statement = f"def {reference_name} := @{candidate.declaration_full_name}\n"
    return reference_name, statement


def _validate_reference(
    *,
    backend: LeanInteractBackend,
    output_root: Path,
    header_text: str,
    config: Gate3DocstringCurationConfig,
    candidate: Gate3MathlibDocstringCandidate,
    representation: RepresentationRecord,
) -> ReferenceLeanStatement:
    reference_name, statement = _reference_code(
        candidate=candidate,
        representation=representation,
    )
    result = backend.run(
        LeanRequest(
            request_id=f"lf021-curation-reference-{candidate.candidate_id[-16:]}",
            context_id=config.execution_context.context_id,
            code=header_text + statement,
            declarations=True,
            allow_sorry=False,
            timeout_seconds=300.0,
            metadata={"candidate_id": candidate.candidate_id},
        )
    )
    if result.status is not LeanStatus.VALID:
        errors = "; ".join(
            str(message.get("data", ""))
            for message in result.messages
            if message.get("severity") == "error"
        )
        raise Gate3DocstringCurationError(
            f"{candidate.candidate_id}: reference binding failed ({result.status.value}): {errors}"
        )
    declarations = tuple(result.declarations)
    if len(declarations) != 1 or declarations[0].get("name") != reference_name:
        raise Gate3DocstringCurationError(
            f"{candidate.candidate_id}: reference declaration extraction drifted"
        )
    if result.raw_response_path is None:
        raise Gate3DocstringCurationError("reference check did not persist a raw response")
    raw_path = Path(result.raw_response_path)
    if not raw_path.is_file() or output_root not in raw_path.parents:
        raise Gate3DocstringCurationError("reference raw response escaped output root")
    reference_type_pp = representation.signature_pp
    if reference_type_pp is None:
        raise Gate3DocstringCurationError("reference signature_pp disappeared during validation")
    return ReferenceLeanStatement(
        theorem_id=candidate.theorem_id,
        representation_id=candidate.representation_id,
        theorem_statement_content_hash=candidate.theorem_statement_content_hash,
        representation_content_hash=candidate.representation_content_hash,
        source_declaration_full_name=candidate.declaration_full_name,
        reference_declaration_name=reference_name,
        reference_type_pp=reference_type_pp,
        reference_lean_statement=statement,
        reference_statement_sha256=hash_canonical(
            {
                "schema": "gate3_docstring_reference_statement_text_v1",
                "text": statement,
            }
        ),
        binding_method=("gate3_signature_pp_with_inferred_exact_pinned_declaration_alias_v1"),
        elaboration_status="valid",
        lean_request_hash=result.request_hash,
        raw_response_artifact=_artifact(raw_path),
    )


def _decision_record(
    *,
    candidate_artifact_sha256: str,
    candidate: Gate3MathlibDocstringCandidate,
    review: OperationalReview,
    reference_context: ReferenceContext | None,
    reference: ReferenceLeanStatement | None,
) -> OperationalCurationRecord:
    decision_id = "gate3_docstring_curation:" + hash_canonical(
        {
            "schema": "gate3_docstring_operational_curation_record_v1",
            "source_candidate_artifact_sha256": candidate_artifact_sha256,
            "candidate_id": candidate.candidate_id,
            "decision": review.decision.value,
            "reason_code": review.reason_code,
            "rationale": review.rationale,
            "reference_statement_sha256": (
                reference.reference_statement_sha256 if reference is not None else None
            ),
        }
    )
    return OperationalCurationRecord(
        decision_id=decision_id,
        source_candidate_artifact_sha256=candidate_artifact_sha256,
        source_candidate=candidate,
        review=review,
        reference_context=reference_context,
        reference=reference,
    )


def run_gate3_docstring_curation(
    *,
    paths: RepoPaths,
    mathlib_checkout: Path,
    output_root: Path | None = None,
) -> Gate3DocstringCurationRun:
    """Run the frozen 57-record operational curation and reference checks."""

    root = paths.root
    loaded = load_config(root / CONFIG_PATH, Gate3DocstringCurationConfig)
    config = loaded.config
    output_root = output_root or Path(config.outputs.root)
    report_path = root / config.outputs.report
    if output_root.exists():
        raise Gate3DocstringCurationError(
            f"refusing to overwrite existing curation root {output_root}"
        )
    if report_path.exists():
        raise Gate3DocstringCurationError(
            f"refusing to overwrite existing curation report {report_path}"
        )
    if _git_head(mathlib_checkout) != config.execution_context.project_revision:
        raise Gate3DocstringCurationError(
            "mathlib checkout revision does not match curation config"
        )

    import_header_path = _verify_artifact(root, config.execution_context.import_header)
    header_text = import_header_path.read_text(encoding="utf-8")
    if header_text != "import Mathlib\n":
        raise Gate3DocstringCurationError("curation import header changed")

    candidates = _load_candidates(root=root, config=config)
    _, representations = _load_references(
        root=root,
        config=config,
        candidates=candidates,
    )
    configured_exclusions = {item.candidate_id for item in config.review.exclusions}
    observed_ids = {item.candidate_id for item in candidates}
    if not configured_exclusions <= observed_ids:
        raise Gate3DocstringCurationError(
            "curation exclusions contain candidates absent from the frozen input"
        )

    reference_context = ReferenceContext(
        project_registry_key=config.execution_context.project_registry_key,
        project_revision=config.execution_context.project_revision,
        context_id=config.execution_context.context_id,
        environment_schema_version=config.execution_context.environment_schema_version,
        import_header_artifact=config.execution_context.import_header.path,
        import_header_sha256=config.execution_context.import_header.sha256,
        import_header_text=cast(Literal["import Mathlib\n"], header_text),
    )

    raw_response_dir = output_root / "raw_reference_checks"
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=mathlib_checkout,
            context_fingerprint=config.execution_context.context_id.removeprefix("ctx:"),
            environment_schema_version=config.execution_context.environment_schema_version,
            raw_response_dir=raw_response_dir,
        )
    )
    records: list[OperationalCurationRecord] = []
    try:
        for candidate in candidates:
            review = _review_for(config=config, candidate=candidate)
            reference = (
                _validate_reference(
                    backend=backend,
                    output_root=output_root,
                    header_text=header_text,
                    config=config,
                    candidate=candidate,
                    representation=representations[candidate.representation_id],
                )
                if review.model_collection_authorized
                else None
            )
            records.append(
                _decision_record(
                    candidate_artifact_sha256=config.inputs.eligible_candidates.sha256,
                    candidate=candidate,
                    review=review,
                    reference_context=reference_context if reference is not None else None,
                    reference=reference,
                )
            )
    finally:
        backend.close()

    admitted = tuple(item for item in records if item.review.model_collection_authorized)
    excluded = tuple(item for item in records if not item.review.model_collection_authorized)
    ambiguous = tuple(item for item in excluded if item.review.ambiguous_exclusion)
    if (
        len(records) != config.expected_counts.reviewed
        or len(admitted) != config.expected_counts.admitted
        or len(excluded) != config.expected_counts.excluded
        or len(ambiguous) != config.expected_counts.ambiguous_exclusions
    ):
        raise Gate3DocstringCurationError("curation counts do not match frozen expectations")

    decision_path = output_root / "decisions.jsonl"
    admitted_path = output_root / "admitted.jsonl"
    excluded_path = output_root / "excluded.jsonl"
    reference_checks_path = output_root / "reference_checks.json"
    manifest_path = output_root / "manifest.json"

    _write_new(
        decision_path,
        _jsonl_bytes(tuple(item.model_dump(mode="json") for item in records)),
    )
    _write_new(
        admitted_path,
        _jsonl_bytes(tuple(item.model_dump(mode="json") for item in admitted)),
    )
    _write_new(
        excluded_path,
        _jsonl_bytes(tuple(item.model_dump(mode="json") for item in excluded)),
    )
    raw_checks = tuple(
        {
            "candidate_id": item.source_candidate.candidate_id,
            "decision_id": item.decision_id,
            "lean_request_hash": item.reference.lean_request_hash,
            "raw_response_artifact": item.reference.raw_response_artifact.model_dump(mode="json"),
        }
        for item in admitted
        if item.reference is not None
    )
    _write_new(
        reference_checks_path,
        canonical_json_bytes(
            {
                "schema_version": 1,
                "artifact_kind": "gate3_docstring_reference_checks_v1",
                "count": len(raw_checks),
                "checks": raw_checks,
                "all_valid": True,
                "allow_sorry": False,
            }
        ),
    )

    output_artifacts = {
        "decisions": _artifact(decision_path, root=output_root),
        "admitted": _artifact(admitted_path, root=output_root),
        "excluded": _artifact(excluded_path, root=output_root),
        "reference_checks": _artifact(reference_checks_path, root=output_root),
    }
    input_artifacts = {
        "candidate_manifest": config.inputs.candidate_manifest,
        "eligible_candidates": config.inputs.eligible_candidates,
        "upstream_report": config.inputs.upstream_report,
        "theorem_records": config.inputs.theorem_records,
        "representation_records": config.inputs.representation_records,
        "import_header": config.execution_context.import_header,
        "config": ArtifactBinding(path=str(CONFIG_PATH), sha256=hash_file(root / CONFIG_PATH)),
    }
    decision_counts = Counter(item.review.decision.value for item in records)
    manifest = CurationArtifactManifest(
        artifact_kind="gate3_docstring_operational_curation_manifest_v1",
        frozen_at=config.frozen_at,
        config_hash=loaded.config_hash,
        input_artifacts=input_artifacts,
        output_artifacts=output_artifacts,
        reviewed_count=config.expected_counts.reviewed,
        admitted_count=config.expected_counts.admitted,
        excluded_count=config.expected_counts.excluded,
        ambiguous_exclusion_count=config.expected_counts.ambiguous_exclusions,
        decision_counts=dict(sorted(decision_counts.items())),
        admitted_candidate_ids=tuple(item.source_candidate.candidate_id for item in admitted),
        reviewer_type=config.review.reviewer_type,
        review_method=config.review.review_method,
    )
    _write_new(manifest_path, canonical_json_bytes(manifest.model_dump(mode="json")))

    report = CurationReport(
        report_kind="lf021_gate3_docstring_operational_curation_v1",
        passed=True,
        manifest=_artifact(manifest_path),
        decisions=_artifact(decision_path),
        admitted=_artifact(admitted_path),
        excluded=_artifact(excluded_path),
        reference_checks=_artifact(reference_checks_path),
        reviewed_count=config.expected_counts.reviewed,
        admitted_count=config.expected_counts.admitted,
        excluded_count=config.expected_counts.excluded,
        ambiguous_exclusion_count=config.expected_counts.ambiguous_exclusions,
        reviewer_type=config.review.reviewer_type,
        limitations=(
            "This is Codex-agent/LLM-assisted operational curation, not human review.",
            "Standalone admission is not a same-claim or semantic-gold label.",
            "Reference statements are retained for later comparison and are not generator inputs.",
            "Authorization is restricted to the pinned local generator families.",
            "No model output, semantic label, or Gate-5 credit is created by this stage.",
        ),
    )
    _write_new(report_path, canonical_json_bytes(report.model_dump(mode="json")))
    manifest_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    report_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return Gate3DocstringCurationRun(
        report_path=report_path,
        manifest_path=manifest_path,
        report=report,
        manifest=manifest,
    )
