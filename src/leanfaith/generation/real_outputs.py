"""LF-021 Lean validation and record materialization for one generated candidate.

Provider execution and raw-response persistence happen before this boundary.
This module accepts one already parsed response, appends only the controlled
``:= by sorry`` placeholder needed to elaborate a statement, and creates
semantic-pool records only when Lean confirms exactly one named proposition.
No semantic relation or resolved label is inferred here.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import DenylistIndex
from leanfaith.generation.prompts import (
    ParsedLeanDeclaration,
    parse_direct_autoformalization_output,
)
from leanfaith.lean.extraction import (
    PLACEHOLDER,
    ExtractedDeclaration,
    SourceIdentity,
    extract_from_declarations,
    reconstruct_for_revalidation,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.pipeline import (
    RepresentationBatch,
    RepresentationFailure,
    TheoremForRepresentation,
    build_representation_batch,
)
from leanfaith.schemas.enums import (
    GeneratorKind,
    IntendedRelation,
    LLMCallStatus,
    LLMRole,
    ParseStatus,
    Polarity,
    QualityTier,
    ValidationStatus,
)
from leanfaith.schemas.ids import (
    HEX64_PATTERN,
    LLM_CALL_PREFIX,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    PROBLEM_PREFIX,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    VARIANT_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.nl_lean import (
    NLPLeanRecord,
    ProblemPoolRecord,
    ReferencePairLink,
    check_nl_lean_problem_link,
)
from leanfaith.schemas.pair import PairRecord, check_pair_groups
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import VariantRecord

_OUTCOME_ID_PATTERN = r"^real_output:[0-9a-f]{64}$"
_SCREENING_ID_PATTERN = r"^candidate_screen:[0-9a-f]{64}$"
_ELABORATING = {LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY}
_MINIMUM_ADMISSION_VIEWS = (
    "raw_proof_stripped",
    "headless",
    "signature_pp",
    "alpha_identity_fingerprint",
)


class RealOutputMaterializationError(ValueError):
    """Input records violate the LF-021 materialization contract."""


class RealOutputOutcomeCode(StrEnum):
    """Terminal status of one parsed candidate materialization attempt."""

    MATERIALIZED_PENDING_SCREENING = "materialized_pending_screening"
    MATERIALIZED = "materialized"
    NONCOMPILING = "noncompiling"
    QUARANTINED = "quarantined"
    UNSUPPORTED = "unsupported"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class RealOutputFailureCode(StrEnum):
    """Fail-closed reasons retained for failed or noncompiling candidates."""

    LEAN_INVALID = "lean_invalid"
    LEAN_TIMEOUT = "lean_timeout"
    LEAN_UNSUPPORTED = "lean_unsupported"
    LEAN_INFRASTRUCTURE = "lean_infrastructure"
    DECLARATION_COUNT = "declaration_count"
    DECLARATION_NAME = "declaration_name"
    DECLARATION_KIND = "declaration_kind"
    EXTRACTION_FAILED = "extraction_failed"
    REPRESENTATION_FAILED = "representation_failed"


class CandidateScreeningStatus(StrEnum):
    """Terminal candidate-side contamination/dedup screening state."""

    CLEAN = "clean"
    REJECTED = "rejected"


def _screening_id(
    *,
    problem_record_id: str,
    call_id: str,
    candidate_theorem_id: str,
    representation_id: str,
    theorem_statement_content_hash: str,
    representation_content_hash: str,
    raw_proof_stripped_sha256: str,
    headless_sha256: str,
    signature_pp_sha256: str,
    alpha_identity_fingerprint: str,
    frozen_registry_hash: str,
    benchmark_hits: tuple[str, ...],
    duplicate_candidate_theorem_ids: tuple[str, ...],
    canonical_candidate_theorem_id: str,
    status: CandidateScreeningStatus,
) -> str:
    return "candidate_screen:" + hash_canonical(
        {
            "schema": "candidate_screening_v1",
            "problem_record_id": problem_record_id,
            "call_id": call_id,
            "candidate_theorem_id": candidate_theorem_id,
            "representation_id": representation_id,
            "theorem_statement_content_hash": theorem_statement_content_hash,
            "representation_content_hash": representation_content_hash,
            "raw_proof_stripped_sha256": raw_proof_stripped_sha256,
            "headless_sha256": headless_sha256,
            "signature_pp_sha256": signature_pp_sha256,
            "alpha_identity_fingerprint": alpha_identity_fingerprint,
            "frozen_registry_hash": frozen_registry_hash,
            "benchmark_hits": benchmark_hits,
            "duplicate_candidate_theorem_ids": duplicate_candidate_theorem_ids,
            "canonical_candidate_theorem_id": canonical_candidate_theorem_id,
            "status": status.value,
        }
    )


def _admission_view_values(
    representation: RepresentationRecord,
) -> dict[str, str]:
    values = {
        "raw_proof_stripped": representation.raw_proof_stripped,
        "headless": representation.headless,
        "signature_pp": representation.signature_pp,
        "alpha_identity_fingerprint": representation.alpha_identity_fingerprint,
    }
    missing = tuple(name for name, value in values.items() if not value)
    if missing:
        raise RealOutputMaterializationError(
            "candidate screening/admission requires views: " + ", ".join(missing)
        )
    return {name: str(value) for name, value in values.items()}


def candidate_benchmark_hits(
    *,
    denylist_index: DenylistIndex,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
) -> tuple[str, ...]:
    """Screen one materialized candidate against the active frozen registry.

    Admission callers must pass the resulting hits into
    :meth:`CandidateScreeningRecord.create`; supplying an active-registry hash
    without performing this lookup is not a valid contamination screen.
    """

    hits: set[str] = set()
    text_views = {
        "theorem": theorem.proof_stripped_declaration,
        "raw_proof_stripped": representation.raw_proof_stripped,
        "headless": representation.headless,
        "signature_pp": representation.signature_pp,
        "signature_explicit": representation.signature_explicit,
    }
    for name, value in text_views.items():
        if value and denylist_index.contains_lean(value):
            hits.add(f"lean:{name}")

    representation_signatures: dict[str, str | None] = {
        "headless_hash": (
            sha256_hex(representation.headless.encode("utf-8")) if representation.headless else None
        ),
        "signature_pp_hash": (
            sha256_hex(representation.signature_pp.encode("utf-8"))
            if representation.signature_pp
            else None
        ),
        "signature_explicit_hash": (
            sha256_hex(representation.signature_explicit.encode("utf-8"))
            if representation.signature_explicit
            else None
        ),
        "alpha_identity_fingerprint": representation.alpha_identity_fingerprint,
    }
    for name, value in representation_signatures.items():
        if value and denylist_index.contains_representation(value):
            hits.add(f"representation:{name}")
    return tuple(sorted(hits))


def _outcome_id(
    *,
    problem_record_id: str,
    call_id: str,
    statement_sha256: str,
    generation_config_hash: str,
    outcome: RealOutputOutcomeCode,
    screening_id: str | None,
) -> str:
    return "real_output:" + hash_canonical(
        {
            "schema": "real_output_candidate_outcome_v2",
            "problem_record_id": problem_record_id,
            "call_id": call_id,
            "statement_sha256": statement_sha256,
            "generation_config_hash": generation_config_hash,
            "outcome": outcome.value,
            "screening_id": screening_id,
        }
    )


class CandidateScreeningRecord(StrictModel):
    """Candidate-side benchmark and dedup screen bound to immutable views.

    The screening implementation may use a richer frozen registry, but admission
    consumes only this typed, hash-bound result.  A clean result is therefore
    impossible to reuse for another theorem, representation, or regenerated
    view.
    """

    schema_version: Literal[1] = 1
    screening_id: str = Field(pattern=_SCREENING_ID_PATTERN)
    problem_record_id: str = Field(pattern=id_pattern(PROBLEM_PREFIX))
    call_id: str = Field(pattern=id_pattern(LLM_CALL_PREFIX))
    candidate_theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    representation_id: str = Field(pattern=id_pattern(REPRESENTATION_PREFIX))
    theorem_statement_content_hash: str = Field(pattern=HEX64_PATTERN)
    representation_content_hash: str = Field(pattern=HEX64_PATTERN)
    raw_proof_stripped_sha256: str = Field(pattern=HEX64_PATTERN)
    headless_sha256: str = Field(pattern=HEX64_PATTERN)
    signature_pp_sha256: str = Field(pattern=HEX64_PATTERN)
    alpha_identity_fingerprint: str = Field(pattern=HEX64_PATTERN)
    frozen_registry_hash: str = Field(pattern=HEX64_PATTERN)
    benchmark_hits: tuple[str, ...] = ()
    duplicate_candidate_theorem_ids: tuple[str, ...] = ()
    canonical_candidate_theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    is_canonical: bool
    status: CandidateScreeningStatus
    created_at: datetime.datetime

    @model_validator(mode="after")
    def _screening_shape(self) -> CandidateScreeningRecord:
        require_utc(self.created_at)
        for field_name in ("benchmark_hits", "duplicate_candidate_theorem_ids"):
            values = getattr(self, field_name)
            if list(values) != sorted(set(values)):
                raise ValueError(f"{field_name} must be sorted and unique")
        theorem_pattern = id_pattern(THEOREM_PREFIX)
        for theorem_id in self.duplicate_candidate_theorem_ids:
            if re.fullmatch(theorem_pattern, theorem_id) is None:
                raise ValueError(
                    "duplicate_candidate_theorem_ids must contain canonical theorem IDs"
                )
        if self.candidate_theorem_id in self.duplicate_candidate_theorem_ids:
            raise ValueError(
                "duplicate_candidate_theorem_ids must not contain candidate_theorem_id"
            )
        if self.is_canonical != (self.canonical_candidate_theorem_id == self.candidate_theorem_id):
            raise ValueError(
                "is_canonical must equal canonical_candidate_theorem_id == candidate_theorem_id"
            )
        if (
            not self.is_canonical
            and self.canonical_candidate_theorem_id not in self.duplicate_candidate_theorem_ids
        ):
            raise ValueError(
                "a noncanonical candidate must name its canonical theorem in "
                "duplicate_candidate_theorem_ids"
            )
        clean = (
            not self.benchmark_hits
            and not self.duplicate_candidate_theorem_ids
            and self.is_canonical
        )
        if (self.status is CandidateScreeningStatus.CLEAN) != clean:
            raise ValueError(
                "status=clean requires zero benchmark/dedup hits and canonical candidate"
            )
        expected = _screening_id(
            problem_record_id=self.problem_record_id,
            call_id=self.call_id,
            candidate_theorem_id=self.candidate_theorem_id,
            representation_id=self.representation_id,
            theorem_statement_content_hash=self.theorem_statement_content_hash,
            representation_content_hash=self.representation_content_hash,
            raw_proof_stripped_sha256=self.raw_proof_stripped_sha256,
            headless_sha256=self.headless_sha256,
            signature_pp_sha256=self.signature_pp_sha256,
            alpha_identity_fingerprint=self.alpha_identity_fingerprint,
            frozen_registry_hash=self.frozen_registry_hash,
            benchmark_hits=self.benchmark_hits,
            duplicate_candidate_theorem_ids=self.duplicate_candidate_theorem_ids,
            canonical_candidate_theorem_id=self.canonical_candidate_theorem_id,
            status=self.status,
        )
        if self.screening_id != expected:
            raise ValueError("screening_id does not match the immutable screening payload")
        return self

    @classmethod
    def create(
        cls,
        *,
        problem_record_id: str,
        call_id: str,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        frozen_registry_hash: str,
        benchmark_hits: tuple[str, ...] = (),
        duplicate_candidate_theorem_ids: tuple[str, ...] = (),
        canonical_candidate_theorem_id: str | None = None,
        created_at: datetime.datetime,
    ) -> CandidateScreeningRecord:
        """Create a screening record from the exact materialized views."""

        values = _admission_view_values(representation)
        benchmark_hits = tuple(sorted(set(benchmark_hits)))
        duplicate_candidate_theorem_ids = tuple(sorted(set(duplicate_candidate_theorem_ids)))
        canonical = canonical_candidate_theorem_id or theorem.theorem_id
        status = (
            CandidateScreeningStatus.CLEAN
            if not benchmark_hits
            and not duplicate_candidate_theorem_ids
            and canonical == theorem.theorem_id
            else CandidateScreeningStatus.REJECTED
        )
        raw_hash = sha256_hex(values["raw_proof_stripped"].encode("utf-8"))
        headless_hash = sha256_hex(values["headless"].encode("utf-8"))
        signature_hash = sha256_hex(values["signature_pp"].encode("utf-8"))
        screening_id = _screening_id(
            problem_record_id=problem_record_id,
            call_id=call_id,
            candidate_theorem_id=theorem.theorem_id,
            representation_id=representation.representation_id,
            theorem_statement_content_hash=theorem.statement_content_hash,
            representation_content_hash=representation.content_hash,
            raw_proof_stripped_sha256=raw_hash,
            headless_sha256=headless_hash,
            signature_pp_sha256=signature_hash,
            alpha_identity_fingerprint=values["alpha_identity_fingerprint"],
            frozen_registry_hash=frozen_registry_hash,
            benchmark_hits=benchmark_hits,
            duplicate_candidate_theorem_ids=duplicate_candidate_theorem_ids,
            canonical_candidate_theorem_id=canonical,
            status=status,
        )
        return cls(
            screening_id=screening_id,
            problem_record_id=problem_record_id,
            call_id=call_id,
            candidate_theorem_id=theorem.theorem_id,
            representation_id=representation.representation_id,
            theorem_statement_content_hash=theorem.statement_content_hash,
            representation_content_hash=representation.content_hash,
            raw_proof_stripped_sha256=raw_hash,
            headless_sha256=headless_hash,
            signature_pp_sha256=signature_hash,
            alpha_identity_fingerprint=values["alpha_identity_fingerprint"],
            frozen_registry_hash=frozen_registry_hash,
            benchmark_hits=benchmark_hits,
            duplicate_candidate_theorem_ids=duplicate_candidate_theorem_ids,
            canonical_candidate_theorem_id=canonical,
            is_canonical=canonical == theorem.theorem_id,
            status=status,
            created_at=created_at,
        )


class RealOutputCandidateOutcome(StrictModel):
    """Persistable terminal accounting for one candidate processing stage."""

    schema_version: Literal[2] = 2
    outcome_id: str = Field(pattern=_OUTCOME_ID_PATTERN)
    problem_record_id: str = Field(pattern=id_pattern(PROBLEM_PREFIX))
    call_id: str = Field(pattern=id_pattern(LLM_CALL_PREFIX))
    raw_output_artifact: str
    parsed_statement_sha256: str = Field(pattern=HEX64_PATTERN)
    declaration_name: str
    generation_config_hash: str = Field(pattern=HEX64_PATTERN)
    outcome: RealOutputOutcomeCode
    validation_status: ValidationStatus
    semantic_pool_eligible: bool
    variant_id: str = Field(pattern=id_pattern(VARIANT_PREFIX))
    candidate_theorem_id: str | None = Field(default=None, pattern=id_pattern(THEOREM_PREFIX))
    representation_id: str | None = Field(default=None, pattern=id_pattern(REPRESENTATION_PREFIX))
    pair_ids: tuple[str, ...] = ()
    nl_lean_id: str | None = Field(default=None, pattern=id_pattern(NL_LEAN_PREFIX))
    screening_id: str | None = Field(default=None, pattern=_SCREENING_ID_PATTERN)
    failure_code: RealOutputFailureCode | None = None
    failure_detail: str | None = None
    created_at: datetime.datetime

    @model_validator(mode="after")
    def _terminal_shape(self) -> RealOutputCandidateOutcome:
        require_utc(self.created_at)
        if self.semantic_pool_eligible:
            required = (
                self.candidate_theorem_id,
                self.representation_id,
                self.nl_lean_id,
            )
            if self.outcome != RealOutputOutcomeCode.MATERIALIZED or any(
                value is None for value in required
            ):
                raise ValueError(
                    "semantic-pool eligibility requires a complete materialized record"
                )
            if not self.pair_ids:
                raise ValueError("a materialized candidate requires reference pair IDs")
            if self.failure_code is not None or self.failure_detail is not None:
                raise ValueError("a materialized candidate cannot carry a failure")
            if self.screening_id is None:
                raise ValueError("semantic-pool eligibility requires a screening record")
        elif self.outcome is RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING:
            if self.candidate_theorem_id is None or self.representation_id is None:
                raise ValueError(
                    "pending-screening materialization requires theorem and representation"
                )
            if self.pair_ids or self.nl_lean_id is not None or self.screening_id is not None:
                raise ValueError(
                    "pending-screening materialization cannot enter semantic pair pools"
                )
            if self.failure_code is not None or self.failure_detail is not None:
                raise ValueError("pending-screening materialization is successful, not a failure")
        else:
            if self.pair_ids or self.nl_lean_id is not None:
                raise ValueError("failed candidates cannot enter semantic pair pools")
            if self.failure_code is None or self.failure_detail is None:
                raise ValueError("failed candidates require a failure code and detail")
        expected_id = _outcome_id(
            problem_record_id=self.problem_record_id,
            call_id=self.call_id,
            statement_sha256=self.parsed_statement_sha256,
            generation_config_hash=self.generation_config_hash,
            outcome=self.outcome,
            screening_id=self.screening_id,
        )
        if self.outcome_id != expected_id:
            raise ValueError("outcome_id does not match the immutable stage payload")
        return self


@dataclass(frozen=True, slots=True)
class RealOutputMaterializationResult:
    """All records produced for one candidate; absent pool records stay absent."""

    outcome: RealOutputCandidateOutcome
    variant: VariantRecord
    theorem: TheoremRecord | None = None
    representation: RepresentationRecord | None = None
    representation_failures: tuple[RepresentationFailure, ...] = ()
    pairs: tuple[PairRecord, ...] = ()
    nl_lean: NLPLeanRecord | None = None


def _artifact_path(value: str) -> None:
    path = PurePosixPath(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts:
        raise RealOutputMaterializationError(
            "raw_output_artifact must be a nonempty repository-relative path"
        )


def _validate_inputs(
    *,
    problem: ProblemPoolRecord,
    parsed: ParsedLeanDeclaration,
    call: LLMCallRecord,
    raw_output_artifact: str,
    context: ContextRecord,
    references: tuple[TheoremRecord, ...],
    imports: str,
    generation_config_hash: str,
    created_at: datetime.datetime,
) -> tuple[ParsedLeanDeclaration, tuple[TheoremRecord, ...]]:
    require_utc(created_at)
    if len(generation_config_hash) != 64 or any(
        char not in "0123456789abcdef" for char in generation_config_hash
    ):
        raise RealOutputMaterializationError("generation_config_hash must be lowercase SHA-256")
    if problem.eligibility != "eligible":
        raise RealOutputMaterializationError("only eligible ProblemPoolRecords may be materialized")
    if problem.context_id != context.context_id:
        raise RealOutputMaterializationError("problem and ContextRecord context IDs differ")
    imports_hash = sha256_hex(imports.encode("utf-8"))
    if imports_hash != problem.import_header_hash:
        raise RealOutputMaterializationError("imports do not match problem import_header_hash")
    if imports != context.header_text or imports_hash != context.header_hash:
        raise RealOutputMaterializationError(
            "imports do not match the registered ContextRecord header bytes/hash"
        )
    if problem.import_header_hash != context.header_hash:
        raise RealOutputMaterializationError(
            "problem import_header_hash does not match ContextRecord.header_hash"
        )

    reparsed = parse_direct_autoformalization_output(f"```lean4\n{parsed.statement}\n```")
    if reparsed != parsed:
        raise RealOutputMaterializationError(
            "ParsedLeanDeclaration does not match the canonical strict parser"
        )

    if call.schema_version != 2:
        raise RealOutputMaterializationError(
            "real-output materialization requires LLM call schema v2"
        )
    if call.role is not LLMRole.AUTOFORMALIZER:
        raise RealOutputMaterializationError("LLM call role must be autoformalizer")
    if call.terminal_status is not LLMCallStatus.COMPLETED:
        raise RealOutputMaterializationError("LLM call must be terminal_status=completed")
    if call.parse_status is not ParseStatus.PARSED or call.parsed_output is None:
        raise RealOutputMaterializationError("LLM call must bind a parsed output")
    if call.problem_record_id != problem.problem_record_id:
        raise RealOutputMaterializationError("LLM call problem_record_id mismatch")
    if call.problem_id != problem.problem_id or call.problem_group != problem.problem_group:
        raise RealOutputMaterializationError("LLM call problem identity mismatch")
    if problem.problem_record_id not in call.input_ids:
        raise RealOutputMaterializationError("LLM call input_ids omit problem_record_id")
    if call.raw_output_artifact != raw_output_artifact:
        raise RealOutputMaterializationError("raw artifact does not match pinned LLM call")
    _artifact_path(raw_output_artifact)
    if not call.denylist_checked or call.denylist_hits:
        raise RealOutputMaterializationError("LLM call denylist preflight is not clean")
    parsed_statement = call.parsed_output.get("lean_statement")
    if parsed_statement != parsed.statement:
        raise RealOutputMaterializationError(
            "LLM call parsed_output does not bind the parsed Lean statement"
        )

    reference_by_id = {reference.theorem_id: reference for reference in references}
    if len(reference_by_id) != len(references):
        raise RealOutputMaterializationError("reference theorem IDs must be unique")
    expected_ids = tuple(sorted(problem.reference_theorem_ids))
    if tuple(sorted(reference_by_id)) != expected_ids:
        raise RealOutputMaterializationError(
            "provided references must exactly match ProblemPoolRecord reference_theorem_ids"
        )
    if any(reference.context_id != context.context_id for reference in references):
        raise RealOutputMaterializationError(
            "all reference theorems must share the candidate context"
        )
    return reparsed, tuple(reference_by_id[theorem_id] for theorem_id in expected_ids)


def _validation_status(status: LeanStatus) -> ValidationStatus:
    if status in _ELABORATING:
        return ValidationStatus.ELABORATES_WITH_PLACEHOLDER
    if status == LeanStatus.TIMEOUT:
        return ValidationStatus.TIMEOUT
    if status == LeanStatus.UNSUPPORTED:
        return ValidationStatus.QUARANTINED
    if status in {
        LeanStatus.CRASH,
        LeanStatus.INTERNAL_ERROR,
        LeanStatus.SETUP_ERROR,
    }:
        return ValidationStatus.INFRASTRUCTURE_ERROR
    return ValidationStatus.INVALID


def _generator_id(call: LLMCallRecord) -> str:
    assert call.model_revision is not None
    return f"{call.provider}/{call.model}@{call.model_revision}"


def _variant_id(
    *,
    problem: ProblemPoolRecord,
    parsed: ParsedLeanDeclaration,
    call: LLMCallRecord,
    generation_config_hash: str,
) -> str:
    return make_id(
        VARIANT_PREFIX,
        {
            "schema": "real_output_variant_v1",
            "problem_record_id": problem.problem_record_id,
            "call_id": call.call_id,
            "statement_sha256": parsed.statement_sha256,
            "generation_config_hash": generation_config_hash,
        },
    )


def _variant(
    *,
    variant_id: str,
    problem: ProblemPoolRecord,
    parsed: ParsedLeanDeclaration,
    call: LLMCallRecord,
    raw_output_artifact: str,
    references: tuple[TheoremRecord, ...],
    context: ContextRecord,
    generation_config_hash: str,
    validation_status: ValidationStatus,
    theorem: TheoremRecord | None = None,
    representation: RepresentationRecord | None = None,
) -> VariantRecord:
    seed = call.decoding.get("seed")
    return VariantRecord(
        variant_id=variant_id,
        source_theorem_ids=tuple(reference.theorem_id for reference in references),
        context_id=context.context_id,
        generator_kind=GeneratorKind.AUTOFORMALIZER,
        generator_id=_generator_id(call),
        generation_config_hash=generation_config_hash,
        seed=seed if isinstance(seed, int) and not isinstance(seed, bool) else None,
        prompt_artifact=call.request_artifact,
        raw_output_artifact=raw_output_artifact,
        extracted_statement=parsed.statement,
        candidate_code_hash=parsed.statement_sha256,
        derived_representation_id=(
            representation.representation_id if representation is not None else None
        ),
        intended_relation=IntendedRelation.UNKNOWN,
        candidate_pool="real_outputs",
        validation_status=validation_status,
        derived_theorem_id=theorem.theorem_id if theorem is not None else None,
        quality_tier=QualityTier.PROVISIONAL,
        polarity_metadata=Polarity.UNKNOWN,
        metadata={
            "generation_intention_only": True,
            "llm_call_id": call.call_id,
            "problem_record_id": problem.problem_record_id,
            "prompt_render_hash": call.prompt_render_hash,
            "prompt_template_hash": call.prompt_template_hash,
            "raw_output_artifact": raw_output_artifact,
            "resolved_semantic_label": False,
        },
    )


def _outcome(
    *,
    problem: ProblemPoolRecord,
    parsed: ParsedLeanDeclaration,
    call: LLMCallRecord,
    raw_output_artifact: str,
    generation_config_hash: str,
    created_at: datetime.datetime,
    outcome: RealOutputOutcomeCode,
    validation_status: ValidationStatus,
    variant: VariantRecord,
    theorem: TheoremRecord | None = None,
    representation: RepresentationRecord | None = None,
    pairs: tuple[PairRecord, ...] = (),
    nl_lean: NLPLeanRecord | None = None,
    screening_id: str | None = None,
    failure_code: RealOutputFailureCode | None = None,
    failure_detail: str | None = None,
) -> RealOutputCandidateOutcome:
    outcome_id = _outcome_id(
        problem_record_id=problem.problem_record_id,
        call_id=call.call_id,
        statement_sha256=parsed.statement_sha256,
        generation_config_hash=generation_config_hash,
        outcome=outcome,
        screening_id=screening_id,
    )
    return RealOutputCandidateOutcome(
        outcome_id=outcome_id,
        problem_record_id=problem.problem_record_id,
        call_id=call.call_id,
        raw_output_artifact=raw_output_artifact,
        parsed_statement_sha256=parsed.statement_sha256,
        declaration_name=parsed.declaration_name,
        generation_config_hash=generation_config_hash,
        outcome=outcome,
        validation_status=validation_status,
        semantic_pool_eligible=outcome == RealOutputOutcomeCode.MATERIALIZED,
        variant_id=variant.variant_id,
        candidate_theorem_id=theorem.theorem_id if theorem is not None else None,
        representation_id=(
            representation.representation_id if representation is not None else None
        ),
        pair_ids=tuple(pair.pair_id for pair in pairs),
        nl_lean_id=nl_lean.nl_lean_id if nl_lean is not None else None,
        screening_id=screening_id,
        failure_code=failure_code,
        failure_detail=failure_detail,
        created_at=created_at,
    )


def _failure_result(
    *,
    problem: ProblemPoolRecord,
    parsed: ParsedLeanDeclaration,
    call: LLMCallRecord,
    raw_output_artifact: str,
    context: ContextRecord,
    references: tuple[TheoremRecord, ...],
    generation_config_hash: str,
    created_at: datetime.datetime,
    validation_status: ValidationStatus,
    outcome_code: RealOutputOutcomeCode,
    failure_code: RealOutputFailureCode,
    detail: str,
    theorem: TheoremRecord | None = None,
) -> RealOutputMaterializationResult:
    variant = _variant(
        variant_id=_variant_id(
            problem=problem,
            parsed=parsed,
            call=call,
            generation_config_hash=generation_config_hash,
        ),
        problem=problem,
        parsed=parsed,
        call=call,
        raw_output_artifact=raw_output_artifact,
        references=references,
        context=context,
        generation_config_hash=generation_config_hash,
        validation_status=validation_status,
        theorem=theorem,
    )
    return RealOutputMaterializationResult(
        outcome=_outcome(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            outcome=outcome_code,
            validation_status=validation_status,
            variant=variant,
            theorem=theorem,
            failure_code=failure_code,
            failure_detail=detail,
        ),
        variant=variant,
        theorem=theorem,
    )


def _diagnostics(result: LeanResult) -> str:
    values = [str(message.get("data", "")).strip() for message in result.messages]
    return "; ".join(value for value in values if value) or result.status.value


def materialize_real_output_candidate(
    *,
    problem: ProblemPoolRecord,
    parsed: ParsedLeanDeclaration,
    call: LLMCallRecord,
    raw_output_artifact: str,
    context: ContextRecord,
    references: tuple[TheoremRecord, ...],
    imports: str,
    backend: LeanInteractBackend,
    generation_config_hash: str,
    created_at: datetime.datetime,
) -> RealOutputMaterializationResult:
    """Validate and materialize one parsed direct-autoformalization candidate."""

    parsed, references = _validate_inputs(
        problem=problem,
        parsed=parsed,
        call=call,
        raw_output_artifact=raw_output_artifact,
        context=context,
        references=references,
        imports=imports,
        generation_config_hash=generation_config_hash,
        created_at=created_at,
    )
    variant_id = _variant_id(
        problem=problem,
        parsed=parsed,
        call=call,
        generation_config_hash=generation_config_hash,
    )
    joiner = "" if not imports or imports.endswith("\n") else "\n"
    source = imports + joiner + parsed.statement + PLACEHOLDER
    request = LeanRequest(
        request_id=f"lf021-validate-{call.call_id.removeprefix('call:')[:20]}",
        context_id=context.context_id,
        code=source,
        declarations=True,
        allow_sorry=True,
        timeout_seconds=300.0,
        metadata={
            "llm_call_id": call.call_id,
            "problem_record_id": problem.problem_record_id,
        },
    )
    try:
        lean_result = backend.run(request)
    except Exception as exc:
        return _failure_result(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            context=context,
            references=references,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            validation_status=ValidationStatus.INFRASTRUCTURE_ERROR,
            outcome_code=RealOutputOutcomeCode.INFRASTRUCTURE_ERROR,
            failure_code=RealOutputFailureCode.LEAN_INFRASTRUCTURE,
            detail=f"{type(exc).__name__}: {exc}",
        )

    validation_status = _validation_status(lean_result.status)
    if lean_result.status not in _ELABORATING:
        if validation_status == ValidationStatus.TIMEOUT:
            outcome_code = RealOutputOutcomeCode.INFRASTRUCTURE_ERROR
            failure_code = RealOutputFailureCode.LEAN_TIMEOUT
        elif validation_status == ValidationStatus.INFRASTRUCTURE_ERROR:
            outcome_code = RealOutputOutcomeCode.INFRASTRUCTURE_ERROR
            failure_code = RealOutputFailureCode.LEAN_INFRASTRUCTURE
        elif lean_result.status == LeanStatus.UNSUPPORTED:
            outcome_code = RealOutputOutcomeCode.UNSUPPORTED
            failure_code = RealOutputFailureCode.LEAN_UNSUPPORTED
        else:
            outcome_code = RealOutputOutcomeCode.NONCOMPILING
            failure_code = RealOutputFailureCode.LEAN_INVALID
        return _failure_result(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            context=context,
            references=references,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            validation_status=validation_status,
            outcome_code=outcome_code,
            failure_code=failure_code,
            detail=_diagnostics(lean_result),
        )

    declarations = tuple(lean_result.declarations)
    if len(declarations) != 1:
        return _failure_result(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            context=context,
            references=references,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            validation_status=ValidationStatus.QUARANTINED,
            outcome_code=RealOutputOutcomeCode.QUARANTINED,
            failure_code=RealOutputFailureCode.DECLARATION_COUNT,
            detail=f"expected exactly one declaration, observed {len(declarations)}",
        )
    declaration = declarations[0]
    syntactic_name = str(declaration.get("name") or "")
    observed_name = str(declaration.get("full_name") or syntactic_name)
    if syntactic_name != parsed.declaration_name:
        return _failure_result(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            context=context,
            references=references,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            validation_status=ValidationStatus.QUARANTINED,
            outcome_code=RealOutputOutcomeCode.QUARANTINED,
            failure_code=RealOutputFailureCode.DECLARATION_NAME,
            detail=(
                f"expected syntactic declaration {parsed.declaration_name!r}, "
                f"observed name={syntactic_name!r}, full_name={observed_name!r}"
            ),
        )
    if str(declaration.get("kind", "")) not in {"theorem", "lemma"}:
        return _failure_result(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            context=context,
            references=references,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            validation_status=ValidationStatus.QUARANTINED,
            outcome_code=RealOutputOutcomeCode.QUARANTINED,
            failure_code=RealOutputFailureCode.DECLARATION_KIND,
            detail=f"unexpected declaration kind {declaration.get('kind')!r}",
        )

    extraction = extract_from_declarations(
        SourceIdentity(
            source=f"autoformalizer:{call.model_family}",
            source_revision=call.model_revision or "",
            source_record=call.call_id,
            context_id=context.context_id,
            extraction_route="direct_autoformalization_v1",
            nl_pair_eligibility="unverified",
            source_split=problem.source_split,
            nl_source_link=problem.nl_source_link,
            nl_trust=problem.nl_trust,
        ),
        source,
        list(declarations),
        created_at=created_at,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        lean_result_id=lean_result.request_hash,
    )
    if extraction.failures or len(extraction.accepted) != 1:
        detail = (
            "; ".join(f"{failure.code.value}:{failure.detail}" for failure in extraction.failures)
            or f"accepted declaration count={len(extraction.accepted)}"
        )
        return _failure_result(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            context=context,
            references=references,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            validation_status=ValidationStatus.QUARANTINED,
            outcome_code=RealOutputOutcomeCode.QUARANTINED,
            failure_code=RealOutputFailureCode.EXTRACTION_FAILED,
            detail=detail,
        )

    extracted: ExtractedDeclaration = extraction.accepted[0]
    roots = tuple(
        sorted({root for reference in references for root in reference.root_ancestry_ids})
    )
    inline_source = reconstruct_for_revalidation(
        source,
        extracted.declaration,
        extracted.proof_stripped,
    )
    theorem = TheoremRecord.model_validate(
        {
            **extracted.theorem.model_dump(mode="python"),
            "root_ancestry_ids": roots,
            "parent_theorem_ids": tuple(reference.theorem_id for reference in references),
            "inline_elaboration_source": inline_source,
            "metadata": {
                **extracted.theorem.metadata,
                "generation_intention_only": True,
                "llm_call_id": call.call_id,
                "problem_record_id": problem.problem_record_id,
                "raw_output_artifact": raw_output_artifact,
                "resolved_semantic_label": False,
                "variant_id": variant_id,
            },
        }
    )

    theorem_input = TheoremForRepresentation(
        theorem_id=theorem.theorem_id,
        full_name=observed_name,
        proof_stripped=theorem.proof_stripped_declaration,
        context_id=context.context_id,
        source_signature=(str((declaration.get("signature") or {}).get("pp", "")).strip() or None),
        inline_declaration=True,
        inline_source=theorem.inline_elaboration_source,
    )
    try:
        representation_result = build_representation_batch(
            backend,
            RepresentationBatch(
                context_id=context.context_id,
                # ``inline_elaboration_source`` already contains this exact
                # registered header.  Passing it here too would execute
                # namespace/section/open context twice.
                import_header="",
                ordered_theorem_inputs=(theorem_input,),
            ),
            created_at=created_at,
        )
    except Exception as exc:
        return _failure_result(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            context=context,
            references=references,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            validation_status=ValidationStatus.INFRASTRUCTURE_ERROR,
            outcome_code=RealOutputOutcomeCode.INFRASTRUCTURE_ERROR,
            failure_code=RealOutputFailureCode.REPRESENTATION_FAILED,
            detail=f"{type(exc).__name__}: {exc}",
            theorem=theorem,
        )
    if len(representation_result.ordered_representation_records) != 1:
        return _failure_result(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            context=context,
            references=references,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            validation_status=ValidationStatus.INFRASTRUCTURE_ERROR,
            outcome_code=RealOutputOutcomeCode.INFRASTRUCTURE_ERROR,
            failure_code=RealOutputFailureCode.REPRESENTATION_FAILED,
            detail=(
                "representation builder returned "
                f"{len(representation_result.ordered_representation_records)} records"
            ),
            theorem=theorem,
        )
    representation = representation_result.ordered_representation_records[0]
    if representation.theorem_id != theorem.theorem_id:
        return _failure_result(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            context=context,
            references=references,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            validation_status=ValidationStatus.INFRASTRUCTURE_ERROR,
            outcome_code=RealOutputOutcomeCode.INFRASTRUCTURE_ERROR,
            failure_code=RealOutputFailureCode.REPRESENTATION_FAILED,
            detail=(
                "representation theorem_id mismatch: "
                f"{representation.theorem_id} != {theorem.theorem_id}"
            ),
            theorem=theorem,
        )
    if representation.context_id != theorem.context_id:
        return _failure_result(
            problem=problem,
            parsed=parsed,
            call=call,
            raw_output_artifact=raw_output_artifact,
            context=context,
            references=references,
            generation_config_hash=generation_config_hash,
            created_at=created_at,
            validation_status=ValidationStatus.INFRASTRUCTURE_ERROR,
            outcome_code=RealOutputOutcomeCode.INFRASTRUCTURE_ERROR,
            failure_code=RealOutputFailureCode.REPRESENTATION_FAILED,
            detail=(
                "representation context_id mismatch: "
                f"{representation.context_id} != {theorem.context_id}"
            ),
            theorem=theorem,
        )
    variant = _variant(
        variant_id=variant_id,
        problem=problem,
        parsed=parsed,
        call=call,
        raw_output_artifact=raw_output_artifact,
        references=references,
        context=context,
        generation_config_hash=generation_config_hash,
        validation_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        theorem=theorem,
        representation=representation,
    )

    outcome = _outcome(
        problem=problem,
        parsed=parsed,
        call=call,
        raw_output_artifact=raw_output_artifact,
        generation_config_hash=generation_config_hash,
        created_at=created_at,
        outcome=RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING,
        validation_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        variant=variant,
        theorem=theorem,
        representation=representation,
    )
    return RealOutputMaterializationResult(
        outcome=outcome,
        variant=variant,
        theorem=theorem,
        representation=representation,
        representation_failures=representation_result.per_theorem_failures,
    )


def _screening_binding_violations(
    *,
    materialized: RealOutputMaterializationResult,
    screening: CandidateScreeningRecord,
    problem: ProblemPoolRecord,
    expected_frozen_registry_hash: str,
) -> tuple[str, ...]:
    theorem = materialized.theorem
    representation = materialized.representation
    if theorem is None or representation is None:
        return ("materialized_records_missing",)
    violations: list[str] = []
    outcome = materialized.outcome
    expected = {
        "problem_record_id": problem.problem_record_id,
        "call_id": outcome.call_id,
        "candidate_theorem_id": theorem.theorem_id,
        "representation_id": representation.representation_id,
        "theorem_statement_content_hash": theorem.statement_content_hash,
        "representation_content_hash": representation.content_hash,
        "frozen_registry_hash": expected_frozen_registry_hash,
    }
    for field_name, value in expected.items():
        if getattr(screening, field_name) != value:
            violations.append(f"{field_name}_mismatch")
    try:
        values = _admission_view_values(representation)
    except RealOutputMaterializationError:
        violations.append("minimum_admission_views_missing")
        return tuple(violations)
    view_hashes = {
        "raw_proof_stripped_sha256": sha256_hex(values["raw_proof_stripped"].encode("utf-8")),
        "headless_sha256": sha256_hex(values["headless"].encode("utf-8")),
        "signature_pp_sha256": sha256_hex(values["signature_pp"].encode("utf-8")),
        "alpha_identity_fingerprint": values["alpha_identity_fingerprint"],
    }
    for field_name, value in view_hashes.items():
        if getattr(screening, field_name) != value:
            violations.append(f"{field_name}_mismatch")
    return tuple(violations)


def _build_semantic_pool_records(
    *,
    problem: ProblemPoolRecord,
    references: tuple[TheoremRecord, ...],
    theorem: TheoremRecord,
    variant: VariantRecord,
    call_id: str,
    raw_output_artifact: str,
    screening: CandidateScreeningRecord,
) -> tuple[tuple[PairRecord, ...], NLPLeanRecord]:
    reference_by_id = {reference.theorem_id: reference for reference in references}
    expected_reference_ids = tuple(sorted(problem.reference_theorem_ids))
    if tuple(sorted(reference_by_id)) != expected_reference_ids:
        raise RealOutputMaterializationError(
            "admission references must exactly match ProblemPoolRecord references"
        )
    if theorem.parent_theorem_ids != expected_reference_ids:
        raise RealOutputMaterializationError(
            "candidate parent_theorem_ids do not match admission references"
        )
    split_groups = tuple(
        sorted(
            {
                problem.problem_group,
                *(root for reference in references for root in reference.root_ancestry_ids),
                *theorem.root_ancestry_ids,
            }
        )
    )
    pairs: list[PairRecord] = []
    links: list[ReferencePairLink] = []
    for reference_id in expected_reference_ids:
        reference = reference_by_id[reference_id]
        pair_id = make_id(
            PAIR_PREFIX,
            {
                "schema": "real_output_pair_v1",
                "problem_record_id": problem.problem_record_id,
                "call_id": call_id,
                "reference_theorem_id": reference.theorem_id,
                "candidate_theorem_id": theorem.theorem_id,
                "screening_id": screening.screening_id,
            },
        )
        pair = PairRecord(
            pair_id=pair_id,
            theorem_a_id=reference.theorem_id,
            theorem_b_id=theorem.theorem_id,
            pair_source="real_autoformalizer_output",
            nl_problem_group=problem.problem_group,
            split_group_ids=split_groups,
            generator_id=variant.generator_id,
            intended_relation=IntendedRelation.UNKNOWN,
            resolved_label_id=None,
            evidence_ids=(),
            split_eligible=True,
            metadata={
                "generation_intention_only": True,
                "intended_relation": IntendedRelation.UNKNOWN.value,
                "llm_call_id": call_id,
                "problem_record_id": problem.problem_record_id,
                "raw_output_artifact": raw_output_artifact,
                "resolved_semantic_label": False,
                "screening_id": screening.screening_id,
                "frozen_registry_hash": screening.frozen_registry_hash,
                "variant_id": variant.variant_id,
            },
        )
        violations = check_pair_groups(pair, reference, theorem)
        if violations:
            raise RealOutputMaterializationError(
                "materialized pair violates split lineage: " + ", ".join(violations)
            )
        pairs.append(pair)
        links.append(
            ReferencePairLink(
                reference_theorem_id=reference.theorem_id,
                pair_id=pair.pair_id,
            )
        )

    nl_lean_id = make_id(
        NL_LEAN_PREFIX,
        {
            "schema": "nl_lean_real_output_v2",
            "problem_record_id": problem.problem_record_id,
            "call_id": call_id,
            "candidate_theorem_id": theorem.theorem_id,
            "screening_id": screening.screening_id,
        },
    )
    nl_lean = NLPLeanRecord(
        schema_version=2,
        nl_lean_id=nl_lean_id,
        problem_record_id=problem.problem_record_id,
        problem_id=problem.problem_id,
        problem_group=problem.problem_group,
        source=problem.source,
        source_revision=problem.source_revision,
        nl_statement=problem.nl_statement,
        nl_trust=problem.nl_trust,
        candidate_theorem_id=theorem.theorem_id,
        generator_id=variant.generator_id,
        reference_theorem_ids=expected_reference_ids,
        reference_pairs=tuple(links),
        resolved_label_id=None,
        evidence_ids=(),
        split_group_ids=split_groups,
        metadata={
            "generation_intention_only": True,
            "intended_relation": IntendedRelation.UNKNOWN.value,
            "llm_call_id": call_id,
            "raw_output_artifact": raw_output_artifact,
            "resolved_semantic_label": False,
            "screening_id": screening.screening_id,
            "frozen_registry_hash": screening.frozen_registry_hash,
            "variant_id": variant.variant_id,
        },
    )
    violations = check_nl_lean_problem_link(nl_lean, problem)
    if violations:
        raise RealOutputMaterializationError(
            "materialized NLPLeanRecord violates problem lineage: " + ", ".join(violations)
        )
    return tuple(pairs), nl_lean


def admit_screened_real_output_candidate(
    *,
    materialized: RealOutputMaterializationResult,
    screening: CandidateScreeningRecord,
    problem: ProblemPoolRecord,
    references: tuple[TheoremRecord, ...],
    expected_frozen_registry_hash: str,
    created_at: datetime.datetime,
) -> RealOutputMaterializationResult:
    """Admit one clean, hash-bound materialization into semantic pair pools."""

    require_utc(created_at)
    if len(expected_frozen_registry_hash) != 64 or any(
        char not in "0123456789abcdef" for char in expected_frozen_registry_hash
    ):
        raise RealOutputMaterializationError(
            "expected_frozen_registry_hash must be lowercase SHA-256"
        )
    if materialized.outcome.outcome is not RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING:
        raise RealOutputMaterializationError(
            "admission requires materialized_pending_screening outcome"
        )
    if (
        materialized.outcome.semantic_pool_eligible
        or materialized.pairs
        or materialized.nl_lean is not None
    ):
        raise RealOutputMaterializationError(
            "pre-screening materialization already contains semantic-pool records"
        )
    if screening.status is not CandidateScreeningStatus.CLEAN:
        raise RealOutputMaterializationError("candidate screening status is not clean")
    if screening.created_at < materialized.outcome.created_at:
        raise RealOutputMaterializationError("candidate screening cannot precede materialization")
    if created_at < screening.created_at:
        raise RealOutputMaterializationError("admission cannot precede candidate screening")
    if materialized.outcome.problem_record_id != problem.problem_record_id:
        raise RealOutputMaterializationError(
            "materialized outcome and admission problem_record_id differ"
        )
    theorem = materialized.theorem
    representation = materialized.representation
    if theorem is None or representation is None:
        raise RealOutputMaterializationError(
            "pending-screening materialization lacks theorem or representation"
        )
    violations = _screening_binding_violations(
        materialized=materialized,
        screening=screening,
        problem=problem,
        expected_frozen_registry_hash=expected_frozen_registry_hash,
    )
    if violations:
        raise RealOutputMaterializationError(
            "candidate screening binding failed: " + ", ".join(violations)
        )
    pairs, nl_lean = _build_semantic_pool_records(
        problem=problem,
        references=references,
        theorem=theorem,
        variant=materialized.variant,
        call_id=materialized.outcome.call_id,
        raw_output_artifact=materialized.outcome.raw_output_artifact,
        screening=screening,
    )
    outcome = RealOutputCandidateOutcome(
        outcome_id=_outcome_id(
            problem_record_id=problem.problem_record_id,
            call_id=materialized.outcome.call_id,
            statement_sha256=materialized.outcome.parsed_statement_sha256,
            generation_config_hash=materialized.outcome.generation_config_hash,
            outcome=RealOutputOutcomeCode.MATERIALIZED,
            screening_id=screening.screening_id,
        ),
        problem_record_id=problem.problem_record_id,
        call_id=materialized.outcome.call_id,
        raw_output_artifact=materialized.outcome.raw_output_artifact,
        parsed_statement_sha256=materialized.outcome.parsed_statement_sha256,
        declaration_name=materialized.outcome.declaration_name,
        generation_config_hash=materialized.outcome.generation_config_hash,
        outcome=RealOutputOutcomeCode.MATERIALIZED,
        validation_status=materialized.outcome.validation_status,
        semantic_pool_eligible=True,
        variant_id=materialized.variant.variant_id,
        candidate_theorem_id=theorem.theorem_id,
        representation_id=representation.representation_id,
        pair_ids=tuple(pair.pair_id for pair in pairs),
        nl_lean_id=nl_lean.nl_lean_id,
        screening_id=screening.screening_id,
        created_at=created_at,
    )
    return RealOutputMaterializationResult(
        outcome=outcome,
        variant=materialized.variant,
        theorem=theorem,
        representation=representation,
        representation_failures=materialized.representation_failures,
        pairs=pairs,
        nl_lean=nl_lean,
    )


__all__ = [
    "CandidateScreeningRecord",
    "CandidateScreeningStatus",
    "RealOutputCandidateOutcome",
    "RealOutputFailureCode",
    "RealOutputMaterializationError",
    "RealOutputMaterializationResult",
    "RealOutputOutcomeCode",
    "admit_screened_real_output_candidate",
    "candidate_benchmark_hits",
    "materialize_real_output_candidate",
]
