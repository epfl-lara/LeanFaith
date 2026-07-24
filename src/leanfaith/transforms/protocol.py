"""Typed LF-016 transformation protocol and deterministic record factories.

Rule implementations begin in LF-017/LF-018.  This module defines their
runtime-independent contract and creates schema records whose semantic IDs
exclude mutable metadata and timestamps.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from leanfaith.config.hashing import to_canonical
from leanfaith.schemas.enums import (
    GeneratorKind,
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
)
from leanfaith.schemas.ids import AUDIT_PREFIX, DRAFT_PREFIX, VARIANT_PREFIX, make_id
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import (
    Applicability,
    MetadataValue,
    SCIValidationStatus,
    TransformationAttempt,
    TransformationAttemptOutcome,
    TransformationAudit,
    VariantDraft,
    VariantRecord,
    check_deterministic_variant_lineage,
)


class TransformationIdentityError(ValueError):
    """A draft/audit ID or factory input violates deterministic identity rules."""


@runtime_checkable
class TransformationRule(Protocol):
    """Lean-aware transformation contract from PLAN.md §15.2.

    ``intended_relation`` values emitted by a rule are generation provenance,
    never resolved labels.  Registry dispatch validates all returned records
    before they can be persisted.
    """

    rule_id: str
    rule_version: str
    family_id: str
    polarity: Polarity
    implementation_key: str

    def assess(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> Applicability: ...

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> Sequence[VariantDraft]: ...

    def audit(
        self,
        source: TheoremRecord,
        source_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit: ...


@runtime_checkable
class PairTransformationRule(Protocol):
    """Explicit two-source transformation contract used by N10.

    This protocol is intentionally disjoint from :class:`TransformationRule`.
    The unary registry validates that every draft has exactly its one input
    theorem and representation; a two-parent rule must therefore be dispatched
    by a pair-aware orchestrator instead of weakening those unary checks.

    ``primary`` supplies the candidate declaration identity. ``donor`` supplies
    the compatible nearby signature component. Implementations must record both
    aligned source theorem/representation pairs in every draft.
    """

    rule_id: str
    rule_version: str
    family_id: str
    polarity: Polarity
    implementation_key: str
    generation_config_hash: str

    def assess_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
    ) -> Applicability: ...

    def generate_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
        seed: int,
    ) -> Sequence[VariantDraft]: ...

    def audit_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit: ...


def _json_mapping(value: Mapping[str, object], *, field_name: str) -> dict[str, JsonValue]:
    canonical = to_canonical(dict(value))
    if not isinstance(canonical, dict):
        raise TransformationIdentityError(f"{field_name} must be a JSON mapping")
    return canonical


def _json_trace(
    value: Sequence[Mapping[str, object]],
    *,
    field_name: str,
) -> tuple[dict[str, JsonValue], ...]:
    result = tuple(_json_mapping(item, field_name=field_name) for item in value)
    if field_name == "transformation_trace" and not result:
        raise TransformationIdentityError("transformation_trace must be nonempty")
    return result


def _source_pairs(
    theorem_ids: Sequence[str],
    representation_ids: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if len(theorem_ids) != len(representation_ids) or not theorem_ids:
        raise TransformationIdentityError(
            "source theorem and representation IDs must be nonempty and align one-to-one"
        )
    pairs = tuple(sorted(zip(theorem_ids, representation_ids, strict=True)))
    if len({theorem_id for theorem_id, _ in pairs}) != len(pairs):
        raise TransformationIdentityError("source theorem IDs must be unique")
    if len({representation_id for _, representation_id in pairs}) != len(pairs):
        raise TransformationIdentityError("source representation IDs must be unique")
    return (
        tuple(theorem_id for theorem_id, _ in pairs),
        tuple(representation_id for _, representation_id in pairs),
    )


def _draft_semantic_payload(data: Mapping[str, object]) -> dict[str, object]:
    """Return exactly the immutable fields covered by a ``draft:`` ID."""

    return {key: value for key, value in data.items() if key not in {"draft_id", "metadata"}}


def build_variant_draft(
    *,
    source_theorem_ids: Sequence[str],
    source_representation_ids: Sequence[str],
    context_id: str,
    rule_id: str,
    rule_version: str,
    family_id: str,
    seed: int,
    candidate_code: str,
    intended_relation: IntendedRelation,
    candidate_pool: str,
    transformation_trace: Sequence[Mapping[str, object]],
    generation_config_hash: str,
    intended_error_types: Sequence[str] = (),
    inverse_trace: Sequence[Mapping[str, object]] | None = None,
    expected_atom_mapping: Mapping[str, str] | None = None,
    expected_structural_diff: Mapping[str, object] | None = None,
    formalrx_sci_requested: str | None = None,
    formalrx_sci_validated: str | None = None,
    formalrx_sci_validation_status: SCIValidationStatus = "not_requested",
    formalrx_sci_proposer_family: str | None = None,
    formalrx_sci_validator_family: str | None = None,
    metadata: Mapping[str, MetadataValue] | None = None,
) -> VariantDraft:
    """Create a canonical draft and derive its deterministic semantic ID."""

    sorted_theorems, aligned_representations = _source_pairs(
        source_theorem_ids, source_representation_ids
    )
    trace = _json_trace(transformation_trace, field_name="transformation_trace")
    inverse = (
        None if inverse_trace is None else _json_trace(inverse_trace, field_name="inverse_trace")
    )
    data: dict[str, object] = {
        "schema_version": 1,
        "source_theorem_ids": sorted_theorems,
        "source_representation_ids": aligned_representations,
        "context_id": context_id,
        "rule_id": rule_id,
        "rule_version": rule_version,
        "family_id": family_id,
        "seed": seed,
        "candidate_code": candidate_code,
        "candidate_code_hash": hashlib.sha256(candidate_code.encode("utf-8")).hexdigest(),
        "intended_relation": intended_relation,
        "intended_error_types": tuple(sorted(set(intended_error_types))),
        "formalrx_sci_requested": formalrx_sci_requested,
        "formalrx_sci_validated": formalrx_sci_validated,
        "formalrx_sci_validation_status": formalrx_sci_validation_status,
        "formalrx_sci_proposer_family": formalrx_sci_proposer_family,
        "formalrx_sci_validator_family": formalrx_sci_validator_family,
        "candidate_pool": candidate_pool,
        "transformation_trace": trace,
        "inverse_trace": inverse,
        "expected_atom_mapping": dict(sorted((expected_atom_mapping or {}).items())),
        "expected_structural_diff": _json_mapping(
            expected_structural_diff or {},
            field_name="expected_structural_diff",
        ),
        "generation_config_hash": generation_config_hash,
        "metadata": dict(metadata or {}),
    }
    draft_id = make_id(DRAFT_PREFIX, _draft_semantic_payload(data))
    return VariantDraft.model_validate({"draft_id": draft_id, **data})


def expected_variant_draft_id(draft: VariantDraft) -> str:
    """Recompute the semantic ID of an already validated draft."""

    return make_id(
        DRAFT_PREFIX,
        _draft_semantic_payload(draft.model_dump(mode="json")),
    )


def verify_variant_draft_id(draft: VariantDraft) -> None:
    expected = expected_variant_draft_id(draft)
    if draft.draft_id != expected:
        raise TransformationIdentityError(
            f"draft_id mismatch: stored {draft.draft_id}, recomputed {expected}"
        )


def _audit_semantic_payload(data: Mapping[str, object]) -> dict[str, object]:
    """Return exactly the immutable fields covered by an ``audit:`` ID."""

    return {key: value for key, value in data.items() if key not in {"audit_id", "metadata"}}


def build_transformation_audit(
    *,
    draft: VariantDraft,
    applicability: Applicability,
    audit_config_hash: str,
    recommended_validation_status: ValidationStatus,
    recommended_quality_tier: QualityTier = QualityTier.PROVISIONAL,
    candidate_theorem_id: str | None = None,
    candidate_representation_id: str | None = None,
    elaboration_evidence_id: str | None = None,
    structural_diff_ok: bool | None = None,
    atom_mapping_ok: bool | None = None,
    inverse_or_roundtrip_ok: bool | None = None,
    certificate_evidence_ids: Sequence[str] = (),
    violation_codes: Sequence[str] = (),
    metadata: Mapping[str, MetadataValue] | None = None,
) -> TransformationAudit:
    """Create a mechanical audit linked to one draft; never promote a label."""

    data: dict[str, object] = {
        "schema_version": 1,
        "draft_id": draft.draft_id,
        "family_id": draft.family_id,
        "rule_id": draft.rule_id,
        "rule_version": draft.rule_version,
        "context_id": draft.context_id,
        "candidate_code_hash": draft.candidate_code_hash,
        "candidate_theorem_id": candidate_theorem_id,
        "candidate_representation_id": candidate_representation_id,
        "audit_config_hash": audit_config_hash,
        "applicability": applicability,
        "elaboration_evidence_id": elaboration_evidence_id,
        "structural_diff_ok": structural_diff_ok,
        "atom_mapping_ok": atom_mapping_ok,
        "inverse_or_roundtrip_ok": inverse_or_roundtrip_ok,
        "certificate_evidence_ids": tuple(sorted(set(certificate_evidence_ids))),
        "violation_codes": tuple(sorted(set(violation_codes))),
        "recommended_validation_status": recommended_validation_status,
        "recommended_quality_tier": recommended_quality_tier,
        "metadata": dict(metadata or {}),
    }
    audit_id = make_id(
        AUDIT_PREFIX,
        _audit_semantic_payload(
            TransformationAudit.model_validate(
                {"audit_id": f"audit:{'0' * 64}", **data}
            ).model_dump(mode="json")
        ),
    )
    return TransformationAudit.model_validate({"audit_id": audit_id, **data})


def expected_transformation_audit_id(audit: TransformationAudit) -> str:
    """Recompute the semantic ID of an already validated audit."""

    return make_id(
        AUDIT_PREFIX,
        _audit_semantic_payload(audit.model_dump(mode="json")),
    )


def verify_transformation_audit_id(audit: TransformationAudit) -> None:
    expected = expected_transformation_audit_id(audit)
    if audit.audit_id != expected:
        raise TransformationIdentityError(
            f"audit_id mismatch: stored {audit.audit_id}, recomputed {expected}"
        )


def _attempt_semantic_payload(data: Mapping[str, object]) -> dict[str, object]:
    """Return exactly the immutable fields covered by an ``attempt:`` ID."""

    return {key: value for key, value in data.items() if key not in {"attempt_id", "metadata"}}


def build_transformation_attempt(
    *,
    family_id: str,
    rule_id: str,
    rule_version: str,
    source_theorem_ids: Sequence[str],
    source_representation_ids: Sequence[str],
    context_id: str,
    registry_hash: str,
    generation_config_hash: str,
    seed: int,
    applicability: Applicability | None,
    terminal_outcome: TransformationAttemptOutcome,
    draft_ids: Sequence[str] = (),
    failure_codes: Sequence[str] = (),
    metadata: Mapping[str, MetadataValue] | None = None,
) -> TransformationAttempt:
    """Create one terminal rule-attempt record with a deterministic ID."""

    sorted_theorems, aligned_representations = _source_pairs(
        source_theorem_ids, source_representation_ids
    )
    data: dict[str, object] = {
        "schema_version": 1,
        "family_id": family_id,
        "rule_id": rule_id,
        "rule_version": rule_version,
        "source_theorem_ids": sorted_theorems,
        "source_representation_ids": aligned_representations,
        "context_id": context_id,
        "registry_hash": registry_hash,
        "generation_config_hash": generation_config_hash,
        "seed": seed,
        "applicability": applicability,
        "terminal_outcome": terminal_outcome,
        "draft_ids": tuple(sorted(set(draft_ids))),
        "failure_codes": tuple(sorted(set(failure_codes))),
        "metadata": dict(metadata or {}),
    }
    # Validate terminal-state coherence before hashing so invalid attempts never
    # receive a seemingly authoritative semantic ID.
    placeholder = TransformationAttempt.model_validate(
        {"attempt_id": f"attempt:{'0' * 64}", **data}
    )
    attempt_id = make_id(
        "attempt",
        _attempt_semantic_payload(placeholder.model_dump(mode="json")),
    )
    return TransformationAttempt.model_validate({"attempt_id": attempt_id, **data})


def expected_transformation_attempt_id(attempt: TransformationAttempt) -> str:
    """Recompute the semantic ID of an already validated attempt."""

    return make_id(
        "attempt",
        _attempt_semantic_payload(attempt.model_dump(mode="json")),
    )


def verify_transformation_attempt_id(attempt: TransformationAttempt) -> None:
    expected = expected_transformation_attempt_id(attempt)
    if attempt.attempt_id != expected:
        raise TransformationIdentityError(
            f"attempt_id mismatch: stored {attempt.attempt_id}, recomputed {expected}"
        )


def _variant_semantic_payload(data: Mapping[str, object]) -> dict[str, object]:
    """Return immutable deterministic-variant fields covered by ``variant:``."""

    return {key: value for key, value in data.items() if key not in {"variant_id", "metadata"}}


def build_deterministic_variant_record(
    *,
    attempt: TransformationAttempt,
    draft: VariantDraft,
    audit: TransformationAudit,
    candidate: TheoremRecord,
    candidate_representation: RepresentationRecord,
    polarity: Polarity,
    validation_evidence_id: str | None = None,
    metadata: Mapping[str, MetadataValue] | None = None,
) -> VariantRecord:
    """Materialize one fully linked deterministic candidate.

    The factory is deliberately stricter than the individual record schemas:
    it verifies all semantic IDs and cross-record links before assigning the
    final ``variant:`` ID.  Generation intention and mechanical audit status
    remain provenance; this function never creates a resolved semantic label.
    """

    verify_transformation_attempt_id(attempt)
    verify_variant_draft_id(draft)
    verify_transformation_audit_id(audit)
    if attempt.terminal_outcome != "generated":
        raise TransformationIdentityError(
            "a deterministic VariantRecord requires a generated attempt"
        )
    if draft.draft_id not in attempt.draft_ids:
        raise TransformationIdentityError("draft is not listed by its transformation attempt")
    if candidate.context_id != draft.context_id:
        raise TransformationIdentityError("candidate context does not match draft context")
    if candidate_representation.context_id != draft.context_id:
        raise TransformationIdentityError(
            "candidate representation context does not match draft context"
        )
    if candidate_representation.theorem_id != candidate.theorem_id:
        raise TransformationIdentityError(
            "candidate representation does not belong to candidate theorem"
        )
    if candidate.proof_stripped_declaration != draft.candidate_code:
        raise TransformationIdentityError("candidate theorem text does not match draft code")
    if candidate.statement_content_hash != draft.candidate_code_hash:
        raise TransformationIdentityError("candidate theorem hash does not match draft code hash")
    if tuple(sorted(candidate.parent_theorem_ids)) != draft.source_theorem_ids:
        raise TransformationIdentityError(
            "candidate parent theorem IDs must equal every draft source theorem ID"
        )
    if audit.candidate_theorem_id != candidate.theorem_id:
        raise TransformationIdentityError("audit candidate theorem link mismatch")
    if audit.candidate_representation_id != candidate_representation.representation_id:
        raise TransformationIdentityError("audit candidate representation link mismatch")

    data: dict[str, object] = {
        "schema_version": 1,
        "source_theorem_ids": draft.source_theorem_ids,
        "source_representation_ids": draft.source_representation_ids,
        "context_id": draft.context_id,
        "generator_kind": GeneratorKind.DETERMINISTIC_TRANSFORM,
        "generator_id": draft.rule_id,
        "generation_config_hash": draft.generation_config_hash,
        "seed": draft.seed,
        "extracted_statement": draft.candidate_code,
        "candidate_code_hash": draft.candidate_code_hash,
        "transformation_attempt_id": attempt.attempt_id,
        "draft_id": draft.draft_id,
        "audit_id": audit.audit_id,
        "family_id": draft.family_id,
        "rule_id": draft.rule_id,
        "rule_version": draft.rule_version,
        "derived_representation_id": candidate_representation.representation_id,
        "intended_relation": draft.intended_relation,
        "intended_error_types": draft.intended_error_types,
        "formalrx_sci_requested": draft.formalrx_sci_requested,
        "formalrx_sci_validated": draft.formalrx_sci_validated,
        "formalrx_sci_validation_status": draft.formalrx_sci_validation_status,
        "formalrx_sci_proposer_family": draft.formalrx_sci_proposer_family,
        "formalrx_sci_validator_family": draft.formalrx_sci_validator_family,
        "candidate_pool": draft.candidate_pool,
        "transformation_trace": draft.transformation_trace,
        "inverse_trace": draft.inverse_trace,
        "validation_status": audit.recommended_validation_status,
        "validation_evidence_id": validation_evidence_id,
        "derived_theorem_id": candidate.theorem_id,
        "quality_tier": audit.recommended_quality_tier,
        "polarity_metadata": polarity,
        "metadata": dict(metadata or {}),
    }
    placeholder = VariantRecord.model_validate(
        {"variant_id": f"{VARIANT_PREFIX}:{'0' * 64}", **data}
    )
    variant_id = make_id(
        VARIANT_PREFIX,
        _variant_semantic_payload(placeholder.model_dump(mode="json")),
    )
    variant = VariantRecord.model_validate({"variant_id": variant_id, **data})
    violations = check_deterministic_variant_lineage(variant, draft, audit, attempt)
    if violations:
        raise TransformationIdentityError(
            "deterministic variant lineage mismatch: " + ", ".join(violations)
        )
    return variant


def expected_deterministic_variant_id(variant: VariantRecord) -> str:
    """Recompute the semantic ID of a deterministic variant record."""

    if variant.generator_kind != GeneratorKind.DETERMINISTIC_TRANSFORM:
        raise TransformationIdentityError("variant is not a deterministic transform")
    return make_id(
        VARIANT_PREFIX,
        _variant_semantic_payload(variant.model_dump(mode="json")),
    )


def verify_deterministic_variant_id(variant: VariantRecord) -> None:
    expected = expected_deterministic_variant_id(variant)
    if variant.variant_id != expected:
        raise TransformationIdentityError(
            f"variant_id mismatch: stored {variant.variant_id}, recomputed {expected}"
        )
