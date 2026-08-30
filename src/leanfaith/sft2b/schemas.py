"""Strict versioned records for the SFT2B pipeline.

Core rows intentionally contain only ``reference``, ``candidate``, and
``label``. Everything needed to reproduce or audit those three values is
stored in stable-ID-keyed source, attempt, compilation, render, vote, unknown,
or manifest records.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, Field(min_length=1)]
StableId = Annotated[str, Field(pattern=r"^[a-z0-9_]+:[0-9a-f]{64}$")]
ContextId = Annotated[str, Field(pattern=r"^ctx:[0-9a-f]{64}$")]


def stable_id(prefix: str, payload: object) -> str:
    """Return the canonical content ID used by every SFT2B record."""

    if not prefix or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in prefix):
        raise ValueError("stable ID prefix must be lowercase ASCII alphanumeric/underscore")
    return f"{prefix}:{hash_canonical(payload)}"


class CandidateSlot(StrEnum):
    SLOT_0 = "slot_0"
    SLOT_1 = "slot_1"
    SLOT_2 = "slot_2"
    SLOT_3 = "slot_3"


class CandidateOrigin(StrEnum):
    EXISTING_301 = "existing_301"
    REFORM_8B = "reform_8b"
    REFORM_32B = "reform_32b"
    PILOT_ALTERNATIVE = "pilot_alternative"


class CompileStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"


class JudgeId(StrEnum):
    CODEX = "codex"
    LEMEX = "lemex"
    CLAUDE = "claude"


class JudgeDecision(StrEnum):
    EQUIVALENT = "equivalent"
    NON_EQUIVALENT = "non_equivalent"
    UNKNOWN = "unknown"


class MajorityDecision(StrEnum):
    EQUIVALENT = "equivalent"
    NON_EQUIVALENT = "non_equivalent"
    UNKNOWN = "unknown"


class CompileContextRecord(StrictModel):
    schema_version: Literal["sft2b_compile_context_v1"] = "sft2b_compile_context_v1"
    source_context_id: ContextId
    render_compile_context_id: ContextId
    project_id: NonEmpty
    project_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    project_path: NonEmpty
    lean_version: NonEmpty
    import_header: NonEmpty
    namespace_context: tuple[str, ...] = ()
    open_context: tuple[str, ...] = ()
    scoped_context: tuple[str, ...] = ()
    options: dict[str, str | int | float | bool] = Field(default_factory=dict)
    source_context_path: NonEmpty
    source_context_sha256: Sha256
    helper_path: NonEmpty
    helper_sha256: Sha256


class SourceProvenance(StrictModel):
    schema_version: Literal["sft2b_source_provenance_v1"] = "sft2b_source_provenance_v1"
    source_family: Literal["public_research", "algebra", "cross_domain", "new_audited"]
    source_url: NonEmpty
    source_revision: NonEmpty
    source_path: NonEmpty
    source_file_sha256: Sha256
    manifest_path: NonEmpty
    manifest_sha256: Sha256
    source_recipe_sha256: Sha256
    license_card_value: NonEmpty
    redistribution_note: NonEmpty
    nl_extraction_rule: NonEmpty
    trusted_reference_basis: NonEmpty
    benchmark_exact_hit: bool = False
    benchmark_near_hit: bool = False


class SourceRecord(StrictModel):
    schema_version: Literal["sft2b_source_v1"] = "sft2b_source_v1"
    source_id: StableId
    legacy_pair_id: NonEmpty | None = None
    nl_statement: NonEmpty
    reference_theorem_id: NonEmpty
    reference_declaration_name: NonEmpty | None = None
    reference_proposition: NonEmpty
    reference_proposition_sha256: Sha256
    compile_context: CompileContextRecord
    provenance: SourceProvenance
    standalone_nl: Literal[True]
    trusted_reference: Literal[True]
    training_eligible: bool

    @model_validator(mode="after")
    def validate_identity(self) -> SourceRecord:
        # Proposition hashes are byte hashes, not canonical-object hashes.
        from leanfaith.config.hashing import sha256_hex

        expected_hash = sha256_hex(self.reference_proposition.encode("utf-8"))
        if self.reference_proposition_sha256 != expected_hash:
            raise ValueError("reference proposition hash mismatch")
        expected_id = stable_id(
            "sft2b_source",
            {
                "reference_theorem_id": self.reference_theorem_id,
                "nl_statement": self.nl_statement,
                "source_revision": self.provenance.source_revision,
            },
        )
        if self.source_id != expected_id:
            raise ValueError("source_id does not replay from canonical source identity")
        return self


class FormalizerLineage(StrictModel):
    schema_version: Literal["sft2b_formalizer_lineage_v1"] = "sft2b_formalizer_lineage_v1"
    origin: CandidateOrigin
    provider: NonEmpty
    model_id: NonEmpty
    model_revision: NonEmpty
    prompt_sha256: Sha256
    decoding_sha256: Sha256
    seed: int
    upstream_call_id: NonEmpty | None = None
    upstream_generation_config_sha256: Sha256 | None = None


class CandidateRecord(StrictModel):
    schema_version: Literal["sft2b_candidate_v1"] = "sft2b_candidate_v1"
    candidate_id: StableId
    source_id: StableId
    slot: CandidateSlot
    raw_proof_free_signature: NonEmpty
    signature_sha256: Sha256
    source_context_id: ContextId
    lineage: FormalizerLineage
    legacy_candidate_theorem_id: NonEmpty | None = None
    legacy_pair_id: NonEmpty | None = None
    cheap_rejection: NonEmpty | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> CandidateRecord:
        from leanfaith.config.hashing import sha256_hex

        observed = sha256_hex(self.raw_proof_free_signature.encode("utf-8"))
        if observed != self.signature_sha256:
            raise ValueError("candidate signature hash mismatch")
        if ":= by" in self.raw_proof_free_signature or "sorry" in self.raw_proof_free_signature:
            raise ValueError("candidate signature must be proof-free")
        expected_id = stable_id(
            "sft2b_candidate",
            {
                "source_id": self.source_id,
                "slot": self.slot,
                "signature_sha256": self.signature_sha256,
                "source_context_id": self.source_context_id,
                "lineage": self.lineage.model_dump(mode="json"),
            },
        )
        if self.candidate_id != expected_id:
            raise ValueError("candidate_id does not replay")
        return self


class FourCandidatePlan(StrictModel):
    schema_version: Literal["sft2b_four_candidate_plan_v1"] = "sft2b_four_candidate_plan_v1"
    source_id: StableId
    candidates: tuple[CandidateRecord, CandidateRecord, CandidateRecord, CandidateRecord]

    @model_validator(mode="after")
    def validate_slots(self) -> FourCandidatePlan:
        if {item.slot for item in self.candidates} != set(CandidateSlot):
            raise ValueError("four-candidate plan must contain each slot exactly once")
        if any(item.source_id != self.source_id for item in self.candidates):
            raise ValueError("four-candidate plan mixes source IDs")
        return self


class FormalizerAttempt(StrictModel):
    schema_version: Literal["sft2b_formalizer_attempt_v1"] = "sft2b_formalizer_attempt_v1"
    attempt_id: StableId
    source_id: StableId
    slot: CandidateSlot
    lineage: FormalizerLineage
    prompt_input_sha256: Sha256
    raw_output_path: NonEmpty
    raw_output_sha256: Sha256
    extraction_status: Literal["candidate", "invalid"]
    candidate_id: StableId | None = None
    failure_class: NonEmpty | None = None
    failure_detail: NonEmpty | None = None
    elapsed_ms: Annotated[int, Field(ge=0)]
    prompt_tokens: Annotated[int, Field(ge=0)]
    completion_tokens: Annotated[int, Field(ge=0)]
    peak_cuda_allocated_bytes: Annotated[int, Field(ge=0)]
    peak_cuda_reserved_bytes: Annotated[int, Field(ge=0)]
    torch_version: NonEmpty
    transformers_version: NonEmpty

    @model_validator(mode="after")
    def validate_route(self) -> FormalizerAttempt:
        if self.extraction_status == "candidate":
            if self.candidate_id is None:
                raise ValueError("successful formalizer attempt requires candidate_id")
            if self.failure_class is not None or self.failure_detail is not None:
                raise ValueError("successful formalizer attempt cannot carry failure fields")
        else:
            if self.candidate_id is not None:
                raise ValueError("invalid formalizer attempt cannot carry candidate_id")
            if self.failure_class is None or self.failure_detail is None:
                raise ValueError("invalid formalizer attempt requires failure fields")
        return self


class FormalizerInvalidAttemptView(StrictModel):
    schema_version: Literal["sft2b_formalizer_invalid_v1"] = "sft2b_formalizer_invalid_v1"
    attempt_id: StableId
    source_id: StableId
    slot: CandidateSlot
    validity_label: Literal[False]
    failure_class: NonEmpty
    failure_detail: NonEmpty
    raw_output_sha256: Sha256


class EndpointCacheRecord(StrictModel):
    schema_version: Literal["sft2b_endpoint_cache_v1"] = "sft2b_endpoint_cache_v1"
    endpoint_cache_key: Sha256
    endpoint_id: NonEmpty
    endpoint_role: Literal["reference", "candidate"]
    source_id: StableId
    candidate_id: StableId | None = None
    proposition_sha256: Sha256
    source_context_id: ContextId
    render_compile_context_id: ContextId
    project_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    lean_version: NonEmpty
    helper_sha256: Sha256
    repr_spec_sha256: Sha256
    repr_implementation_set_sha256: Sha256
    status: CompileStatus
    goal_v1: NonEmpty | None = None
    goal_v1_sha256: Sha256 | None = None
    repr_sidecar: dict[str, object] | None = None
    error_class: NonEmpty | None = None
    error_detail: NonEmpty | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> EndpointCacheRecord:
        if self.endpoint_role == "reference" and self.candidate_id is not None:
            raise ValueError("reference endpoint cannot have candidate_id")
        if self.endpoint_role == "candidate" and self.candidate_id is None:
            raise ValueError("candidate endpoint requires candidate_id")
        if self.status == CompileStatus.VALID:
            if self.goal_v1 is None or self.goal_v1_sha256 is None or self.repr_sidecar is None:
                raise ValueError("valid endpoint requires goal text/hash/REPR sidecar")
            if self.error_class is not None or self.error_detail is not None:
                raise ValueError("valid endpoint cannot carry an error")
            if "[anonymous]" in self.goal_v1 or "⋯" in self.goal_v1:
                raise ValueError("model-facing render contains a forbidden placeholder")
        elif self.error_class is None or self.error_detail is None:
            raise ValueError("failed endpoint requires error class and detail")
        return self


class CompilationEvidence(StrictModel):
    schema_version: Literal["sft2b_compilation_evidence_v1"] = "sft2b_compilation_evidence_v1"
    evidence_id: StableId
    source_id: StableId
    candidate_id: StableId
    reference_cache_key: Sha256
    candidate_cache_key: Sha256
    request_hash: Sha256
    status: CompileStatus
    elapsed_ms: Annotated[int, Field(ge=0)]
    raw_response_path: str | None = None
    raw_response_sha256: Sha256 | None = None
    backend_method_version: NonEmpty
    cache_hit: bool
    reference: EndpointCacheRecord | None = None
    candidate: EndpointCacheRecord | None = None
    failure_class: NonEmpty | None = None
    failure_detail: NonEmpty | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> CompilationEvidence:
        if self.status == CompileStatus.VALID:
            if self.reference is None or self.candidate is None:
                raise ValueError("valid evidence requires reference and candidate endpoints")
            if (
                self.reference.status != CompileStatus.VALID
                or self.candidate.status != CompileStatus.VALID
            ):
                raise ValueError("valid evidence contains a failed endpoint")
            if self.failure_class is not None or self.failure_detail is not None:
                raise ValueError("valid evidence cannot carry failure fields")
        elif self.failure_class is None or self.failure_detail is None:
            raise ValueError("failed evidence requires failure fields")
        return self


class ReformRenderBatchTerminal(StrictModel):
    schema_version: Literal["sft2b_reform_render_batch_v1"] = "sft2b_reform_render_batch_v1"
    batch_key: Sha256
    source_id: StableId
    reference_cache_key: Sha256
    candidate_ids: tuple[StableId, ...]
    compilation_evidence: tuple[CompilationEvidence, ...]
    request_hash: Sha256
    elapsed_ms: Annotated[int, Field(ge=0)]
    raw_response_path: str | None = None
    raw_response_sha256: Sha256 | None = None
    peak_rss_bytes: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_batch(self) -> ReformRenderBatchTerminal:
        if not self.candidate_ids or len(self.candidate_ids) > 4:
            raise ValueError("ReForm batch requires one to four extracted candidates")
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("ReForm batch candidate IDs are not unique")
        if tuple(item.candidate_id for item in self.compilation_evidence) != self.candidate_ids:
            raise ValueError("ReForm batch evidence order/identity mismatch")
        if any(item.source_id != self.source_id for item in self.compilation_evidence):
            raise ValueError("ReForm batch mixes source IDs")
        return self


class JudgeVote(StrictModel):
    schema_version: Literal["sft2b_judge_vote_v1"] = "sft2b_judge_vote_v1"
    vote_id: StableId
    candidate_id: StableId
    judge: JudgeId
    provider: NonEmpty
    model_id: NonEmpty
    cli_version: NonEmpty
    prompt_sha256: Sha256
    judge_input_sha256: Sha256
    response_sha256: Sha256
    decision: JudgeDecision
    probability_equivalent: Annotated[float, Field(ge=0.0, le=1.0)]
    rationale: Annotated[str, Field(min_length=1, max_length=800)]
    relation_class: NonEmpty
    saw_expected_label: Literal[False]
    saw_other_votes: Literal[False]

    @model_validator(mode="after")
    def validate_vote_id(self) -> JudgeVote:
        expected = stable_id(
            "sft2b_vote",
            {
                "candidate_id": self.candidate_id,
                "judge": self.judge,
                "model_id": self.model_id,
                "prompt_sha256": self.prompt_sha256,
                "judge_input_sha256": self.judge_input_sha256,
            },
        )
        if self.vote_id != expected:
            raise ValueError("vote_id does not replay")
        return self


class MajorityOutcome(StrictModel):
    schema_version: Literal["sft2b_majority_v1"] = "sft2b_majority_v1"
    outcome_id: StableId
    candidate_id: StableId
    decision: MajorityDecision
    label: bool | None
    equivalent_votes: Annotated[int, Field(ge=0, le=3)]
    non_equivalent_votes: Annotated[int, Field(ge=0, le=3)]
    unknown_votes: Annotated[int, Field(ge=0, le=3)]
    vote_ids: tuple[StableId, StableId, StableId]

    @model_validator(mode="after")
    def validate_counts(self) -> MajorityOutcome:
        if self.equivalent_votes + self.non_equivalent_votes + self.unknown_votes != 3:
            raise ValueError("majority counts must sum to three")
        expected_label = {
            MajorityDecision.EQUIVALENT: True,
            MajorityDecision.NON_EQUIVALENT: False,
            MajorityDecision.UNKNOWN: None,
        }[self.decision]
        if self.label is not expected_label:
            raise ValueError("majority label disagrees with decision")
        return self


def majority_outcome(
    candidate_id: str, votes: tuple[JudgeVote, JudgeVote, JudgeVote]
) -> MajorityOutcome:
    """Apply the frozen two-of-three semantic vote rule."""

    if len({vote.judge for vote in votes}) != 3:
        raise ValueError("majority requires exactly one vote from each judge")
    if any(vote.candidate_id != candidate_id for vote in votes):
        raise ValueError("majority votes mix candidate IDs")
    counts = Counter(vote.decision for vote in votes)
    equivalent = counts[JudgeDecision.EQUIVALENT]
    non_equivalent = counts[JudgeDecision.NON_EQUIVALENT]
    unknown = counts[JudgeDecision.UNKNOWN]
    if equivalent >= 2:
        decision = MajorityDecision.EQUIVALENT
        label: bool | None = True
    elif non_equivalent >= 2:
        decision = MajorityDecision.NON_EQUIVALENT
        label = False
    else:
        decision = MajorityDecision.UNKNOWN
        label = None
    ordered_ids = tuple(vote.vote_id for vote in sorted(votes, key=lambda item: item.judge.value))
    payload = {
        "candidate_id": candidate_id,
        "vote_ids": ordered_ids,
        "decision": decision,
        "label": label,
    }
    return MajorityOutcome(
        outcome_id=stable_id("sft2b_outcome", payload),
        candidate_id=candidate_id,
        decision=decision,
        label=label,
        equivalent_votes=equivalent,
        non_equivalent_votes=non_equivalent,
        unknown_votes=unknown,
        vote_ids=ordered_ids,  # type: ignore[arg-type]
    )


class CoreRow(StrictModel):
    reference: NonEmpty
    candidate: NonEmpty
    label: bool


class InvalidAttempt(StrictModel):
    schema_version: Literal["sft2b_invalid_attempt_v1"] = "sft2b_invalid_attempt_v1"
    candidate_id: StableId
    source_id: StableId
    compilation_evidence_id: StableId
    validity_label: Literal[False]
    failure_class: NonEmpty
    failure_detail: NonEmpty


class UnknownCandidate(StrictModel):
    schema_version: Literal["sft2b_unknown_candidate_v1"] = "sft2b_unknown_candidate_v1"
    candidate_id: StableId
    source_id: StableId
    compilation_evidence_id: StableId
    majority_outcome_id: StableId
    vote_ids: tuple[StableId, StableId, StableId]
    reason: NonEmpty


class JournalEvent(StrictModel):
    schema_version: Literal["sft2b_journal_event_v1"] = "sft2b_journal_event_v1"
    event_id: StableId
    sequence: Annotated[int, Field(ge=0)]
    run_id: StableId
    source_id: StableId
    candidate_id: StableId | None = None
    stage: Literal[
        "source_recovered",
        "candidate_recovered",
        "formalizer_completed",
        "render_completed",
        "render_cache_hit",
        "vote_completed",
        "vote_cache_hit",
        "compacted",
    ]
    terminal_key: NonEmpty
    artifact_path: NonEmpty
    artifact_sha256: Sha256


class RunManifest(StrictModel):
    schema_version: Literal["sft2b_manifest_v1"] = "sft2b_manifest_v1"
    run_id: StableId
    run_kind: Literal["existing_301_smoke", "reform_8b_smoke", "matched_500_pilot"]
    source_ids: tuple[StableId, ...]
    candidate_ids: tuple[StableId, ...]
    repr_freeze_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    repr_spec_sha256: Sha256
    repr_implementation_set_sha256: Sha256
    repr_api_sha256: Sha256
    helper_sha256: Sha256
    prompt_hashes: dict[JudgeId, Sha256]
    input_receipt_sha256: Sha256
    journal_sha256: Sha256
    output_hashes: dict[str, Sha256]
    counts: dict[str, Annotated[int, Field(ge=0)]]
    lean_request_count: Annotated[int, Field(ge=0)]
    judge_call_count: Annotated[int, Field(ge=0)]
    restart_lean_request_count: Annotated[int, Field(ge=0)]
    restart_judge_call_count: Annotated[int, Field(ge=0)]
    publication_performed: Literal[False]
    training_performed: Literal[False]
