"""LF-016 persistent transformation-lineage schema invariants."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from leanfaith.schemas import (
    Applicability,
    FamilyPromotionDecision,
    GeneratorKind,
    IntendedRelation,
    QualityTier,
    TransformationAttempt,
    TransformationAudit,
    TransformationFamilyStatus,
    ValidationStatus,
    VariantDraft,
    VariantRecord,
    check_deterministic_variant_lineage,
    make_id,
)
from tests.unit.record_factories import (
    ATTEMPT_ID,
    AUDIT_ID,
    CTX_ID,
    DRAFT_ID,
    REPR_A,
    THM_A,
    VAR_CANDIDATE,
    VAR_CANDIDATE_HASH,
    variant_record,
)

CONFIG_HASH = "4" * 64
REGISTRY_HASH = "5" * 64
AUDIT_CONFIG_HASH = "6" * 64
RULE_ID = "p01_alpha"
RULE_VERSION = "1.0.0"
FAMILY_ID = "p01_alpha"


def applicability(**overrides: Any) -> Applicability:
    payload: dict[str, Any] = {
        "applicable": True,
        "reason_codes": (),
        "matched_nodes": ("binder:0",),
        "required_capabilities": ("alpha_identity",),
    }
    payload.update(overrides)
    return Applicability.model_validate(payload)


def draft(**overrides: Any) -> VariantDraft:
    payload: dict[str, Any] = {
        "draft_id": DRAFT_ID,
        "source_theorem_ids": (THM_A,),
        "source_representation_ids": (REPR_A,),
        "context_id": CTX_ID,
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "family_id": FAMILY_ID,
        "seed": 7,
        "candidate_code": VAR_CANDIDATE,
        "candidate_code_hash": VAR_CANDIDATE_HASH,
        "intended_relation": IntendedRelation.EQUIVALENT,
        "candidate_pool": "main",
        "transformation_trace": ({"operation": "rename"},),
        "generation_config_hash": CONFIG_HASH,
    }
    payload.update(overrides)
    return VariantDraft.model_validate(payload)


def attempt(**overrides: Any) -> TransformationAttempt:
    payload: dict[str, Any] = {
        "attempt_id": ATTEMPT_ID,
        "family_id": FAMILY_ID,
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "source_theorem_ids": (THM_A,),
        "source_representation_ids": (REPR_A,),
        "context_id": CTX_ID,
        "registry_hash": REGISTRY_HASH,
        "generation_config_hash": CONFIG_HASH,
        "seed": 7,
        "applicability": applicability(),
        "terminal_outcome": "generated",
        "draft_ids": (DRAFT_ID,),
    }
    payload.update(overrides)
    return TransformationAttempt.model_validate(payload)


def audit(**overrides: Any) -> TransformationAudit:
    payload: dict[str, Any] = {
        "audit_id": AUDIT_ID,
        "draft_id": DRAFT_ID,
        "family_id": FAMILY_ID,
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "context_id": CTX_ID,
        "candidate_code_hash": VAR_CANDIDATE_HASH,
        "audit_config_hash": AUDIT_CONFIG_HASH,
        "applicability": applicability(),
        "recommended_validation_status": ValidationStatus.UNVALIDATED,
        "recommended_quality_tier": QualityTier.PROVISIONAL,
    }
    payload.update(overrides)
    return TransformationAudit.model_validate(payload)


def promotion_decision(**overrides: Any) -> FamilyPromotionDecision:
    payload: dict[str, Any] = {
        "decision_id": make_id("promotion", {"family": FAMILY_ID, "version": RULE_VERSION}),
        "family_id": FAMILY_ID,
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "policy_version": "promotion_v1",
        "audit_id": AUDIT_ID,
        "parent_registry_hash": "1" * 64,
        "promotion_policy_hash": "2" * 64,
        "audit_manifest_hash": "3" * 64,
        "audit_input_hash": "4" * 64,
        "audit_result_hash": "5" * 64,
        "selected_count": 200,
        "denominator_n": 200,
        "successes": 200,
        "point_precision": 1.0,
        "clopper_pearson_lower_95": 0.9817246596448638,
        "blinded": True,
        "design_frozen_before_audit": True,
        "all_invariants_hold": True,
        "held_out_source_domain_audit_passed": True,
        "decision": TransformationFamilyStatus.GOLD_PROMOTED,
        "unlocked_quality_tier": QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
    }
    payload.update(overrides)
    return FamilyPromotionDecision.model_validate(payload)


def test_records_persist_explicit_schema_version() -> None:
    assert draft().schema_version == 1
    assert attempt().schema_version == 1
    assert audit().schema_version == 1
    assert promotion_decision().schema_version == 1


def test_draft_binds_aligned_sources_context_and_candidate_hash() -> None:
    record = draft()
    assert record.source_representation_ids == (REPR_A,)
    assert record.context_id == CTX_ID
    assert (
        record.candidate_code_hash
        == hashlib.sha256(record.candidate_code.encode("utf-8")).hexdigest()
    )


def test_draft_rejects_unaligned_source_representations() -> None:
    with pytest.raises(ValidationError, match="align one-to-one"):
        draft(source_representation_ids=(REPR_A, make_id("repr", {"other": 1})))


def test_draft_rejects_unsorted_or_duplicate_sources() -> None:
    other_theorem = make_id("thm", {"other": 1})
    other_representation = make_id("repr", {"other": 1})
    theorem_ids = tuple(sorted((THM_A, other_theorem), reverse=True))
    with pytest.raises(ValidationError, match="source_theorem_ids must be sorted and unique"):
        draft(
            source_theorem_ids=theorem_ids,
            source_representation_ids=(REPR_A, other_representation),
        )


def test_draft_rejects_candidate_hash_mismatch() -> None:
    with pytest.raises(ValidationError, match="candidate_code_hash does not match"):
        draft(candidate_code_hash="0" * 64)


def test_draft_rejects_noncanonical_json_trace() -> None:
    with pytest.raises(ValidationError, match="canonical JSON"):
        draft(transformation_trace=({"score": float("nan")},))


def test_draft_requires_nonempty_transformation_trace() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        draft(transformation_trace=())


def test_attempt_records_non_applicability_without_a_draft() -> None:
    record = attempt(
        applicability=applicability(
            applicable=False,
            reason_codes=("missing_binder",),
            matched_nodes=(),
        ),
        terminal_outcome="not_applicable",
        draft_ids=(),
    )
    assert record.terminal_outcome == "not_applicable"


def test_attempt_generated_outcome_requires_draft() -> None:
    with pytest.raises(ValidationError, match="at least one draft_id"):
        attempt(draft_ids=())


def test_attempt_failure_is_not_silently_unaccounted() -> None:
    with pytest.raises(ValidationError, match="requires a failure_code"):
        attempt(terminal_outcome="generation_error", draft_ids=(), failure_codes=())


def test_audit_cannot_mechanically_promote_semantic_quality() -> None:
    with pytest.raises(ValidationError, match="cannot self-promote"):
        audit(recommended_quality_tier=QualityTier.GOLD_CONSERVATIVE_TRANSFORM)


def test_audit_evidence_and_violations_are_sorted_unique() -> None:
    evidence_a = make_id("ev", {"n": 1})
    evidence_b = make_id("ev", {"n": 2})
    with pytest.raises(ValidationError, match="certificate_evidence_ids"):
        audit(certificate_evidence_ids=(evidence_b, evidence_a))
    with pytest.raises(ValidationError, match="violation_codes"):
        audit(violation_codes=("z", "a"))


def test_audit_elaboration_requires_candidate_links() -> None:
    with pytest.raises(ValidationError, match="candidate theorem and representation"):
        audit(recommended_validation_status=ValidationStatus.ELABORATES)


def test_deterministic_variant_fails_closed_without_lineage() -> None:
    record = variant_record()
    payload = record.model_dump(mode="python")
    del payload["audit_id"]
    with pytest.raises(ValidationError, match="complete lineage fields"):
        VariantRecord.model_validate(payload)


def test_elaborating_deterministic_variant_requires_derived_links() -> None:
    with pytest.raises(ValidationError, match="derived theorem and representation"):
        variant_record(validation_status=ValidationStatus.ELABORATES)


def test_non_deterministic_variant_remains_compatible_without_transform_lineage() -> None:
    record = VariantRecord(
        variant_id=make_id("var", {"auto": 1}),
        source_theorem_ids=(),
        generator_kind=GeneratorKind.AUTOFORMALIZER,
        generator_id="generator_a",
        generation_config_hash=CONFIG_HASH,
        extracted_statement=VAR_CANDIDATE,
        intended_relation=IntendedRelation.UNKNOWN,
        candidate_pool="real_output",
    )
    assert record.draft_id is None
    assert record.source_representation_ids == ()


def test_cross_record_lineage_accepts_one_consistent_chain() -> None:
    assert (
        check_deterministic_variant_lineage(
            variant_record(),
            draft(),
            audit(),
            attempt(),
        )
        == []
    )


def test_cross_record_lineage_reports_mismatched_audit_rule() -> None:
    violations = check_deterministic_variant_lineage(
        variant_record(),
        draft(),
        audit(rule_version="2.0.0"),
        attempt(),
    )
    assert "audit.rule_version_mismatch" in violations


def test_promotion_decision_binds_exact_stats_and_parent_hashes() -> None:
    record = promotion_decision()
    assert record.parent_registry_hash == "1" * 64
    assert record.point_precision == record.successes / record.denominator_n


def test_promotion_decision_rejects_inexact_or_unbound_decision() -> None:
    with pytest.raises(ValidationError, match="point_precision"):
        promotion_decision(successes=199, point_precision=1.0)
    with pytest.raises(ValidationError, match="semantic-erasure"):
        promotion_decision(recurrent_semantic_erasure_patterns=("drops_conclusion",))
    with pytest.raises(ValidationError, match="requires at least one reason_code"):
        promotion_decision(
            decision=TransformationFamilyStatus.EXPERIMENTAL,
            unlocked_quality_tier=None,
        )
