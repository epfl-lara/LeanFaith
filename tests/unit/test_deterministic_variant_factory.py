"""Cross-record identity tests for persisted deterministic variants."""

from __future__ import annotations

import hashlib

import pytest

from leanfaith.schemas import (
    Applicability,
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
    make_id,
)
from leanfaith.transforms.protocol import (
    TransformationIdentityError,
    build_deterministic_variant_record,
    build_transformation_attempt,
    build_transformation_audit,
    build_variant_draft,
    expected_deterministic_variant_id,
    verify_deterministic_variant_id,
)
from tests.unit.record_factories import (
    ANC_A,
    CTX_ID,
    REPR_A,
    THM_A,
    representation_record,
    theorem_record,
)

_CONFIG_HASH = "4" * 64
_CODE = "theorem transformed : False ∨ True := by sorry"
_CODE_HASH = hashlib.sha256(_CODE.encode("utf-8")).hexdigest()
_CANDIDATE_ID = make_id("thm", {"deterministic-variant": _CODE})
_CANDIDATE_REPR_ID = make_id("repr", {"deterministic-variant": _CODE})


def _lineage():
    applicability = Applicability(applicable=True, reason_codes=())
    draft = build_variant_draft(
        source_theorem_ids=(THM_A,),
        source_representation_ids=(REPR_A,),
        context_id=CTX_ID,
        rule_id="n01_operator",
        rule_version="1.0.0",
        family_id="n01_operator",
        seed=17,
        candidate_code=_CODE,
        intended_relation=IntendedRelation.NEAR_MISS,
        intended_error_types=("E10",),
        candidate_pool="deterministic_negative_provisional",
        transformation_trace=({"operation": "replace_operator"},),
        generation_config_hash=_CONFIG_HASH,
    )
    attempt = build_transformation_attempt(
        family_id=draft.family_id,
        rule_id=draft.rule_id,
        rule_version=draft.rule_version,
        source_theorem_ids=draft.source_theorem_ids,
        source_representation_ids=draft.source_representation_ids,
        context_id=draft.context_id,
        registry_hash=_CONFIG_HASH,
        generation_config_hash=draft.generation_config_hash,
        seed=draft.seed,
        applicability=applicability,
        terminal_outcome="generated",
        draft_ids=(draft.draft_id,),
    )
    candidate = theorem_record(
        theorem_id=_CANDIDATE_ID,
        ancestry_id=make_id("anc", {"deterministic-variant": _CODE}),
        root_ancestry_ids=(ANC_A,),
        parent_theorem_ids=(THM_A,),
        declaration_name="transformed",
        declaration_full_name="transformed",
        proof_stripped_declaration=_CODE,
        inline_elaboration_source=_CODE,
        statement_content_hash=_CODE_HASH,
    )
    candidate_representation = representation_record(
        theorem_id=_CANDIDATE_ID,
        representation_id=_CANDIDATE_REPR_ID,
        raw_proof_stripped=_CODE,
        headless=": False ∨ True",
        signature_pp="False ∨ True",
        content_hash="5" * 64,
    )
    audit = build_transformation_audit(
        draft=draft,
        applicability=applicability,
        audit_config_hash="6" * 64,
        recommended_validation_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        recommended_quality_tier=QualityTier.PROVISIONAL,
        candidate_theorem_id=candidate.theorem_id,
        candidate_representation_id=candidate_representation.representation_id,
    )
    return attempt, draft, audit, candidate, candidate_representation


def test_factory_builds_replayable_fully_linked_variant() -> None:
    attempt, draft, audit, candidate, candidate_representation = _lineage()

    first = build_deterministic_variant_record(
        attempt=attempt,
        draft=draft,
        audit=audit,
        candidate=candidate,
        candidate_representation=candidate_representation,
        polarity=Polarity.NEGATIVE,
    )
    replay = build_deterministic_variant_record(
        attempt=attempt,
        draft=draft,
        audit=audit,
        candidate=candidate,
        candidate_representation=candidate_representation,
        polarity=Polarity.NEGATIVE,
    )

    assert first == replay
    assert first.variant_id == expected_deterministic_variant_id(first)
    assert first.quality_tier == QualityTier.PROVISIONAL
    assert first.validation_status == ValidationStatus.ELABORATES_WITH_PLACEHOLDER
    verify_deterministic_variant_id(first)


def test_attempt_and_selected_site_audit_applicability_may_differ() -> None:
    attempt, draft, _, candidate, candidate_representation = _lineage()
    selected_site_audit = build_transformation_audit(
        draft=draft,
        applicability=Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=("selected_site",),
            required_capabilities=("exact_selected_site_audit",),
        ),
        audit_config_hash="6" * 64,
        recommended_validation_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        recommended_quality_tier=QualityTier.PROVISIONAL,
        candidate_theorem_id=candidate.theorem_id,
        candidate_representation_id=candidate_representation.representation_id,
    )

    variant = build_deterministic_variant_record(
        attempt=attempt,
        draft=draft,
        audit=selected_site_audit,
        candidate=candidate,
        candidate_representation=candidate_representation,
        polarity=Polarity.NEGATIVE,
    )

    assert attempt.applicability != selected_site_audit.applicability
    assert variant.audit_id == selected_site_audit.audit_id


def test_mutable_metadata_does_not_change_variant_identity() -> None:
    attempt, draft, audit, candidate, candidate_representation = _lineage()

    first = build_deterministic_variant_record(
        attempt=attempt,
        draft=draft,
        audit=audit,
        candidate=candidate,
        candidate_representation=candidate_representation,
        polarity=Polarity.NEGATIVE,
        metadata={"worker": "a"},
    )
    second = build_deterministic_variant_record(
        attempt=attempt,
        draft=draft,
        audit=audit,
        candidate=candidate,
        candidate_representation=candidate_representation,
        polarity=Polarity.NEGATIVE,
        metadata={"worker": "b"},
    )

    assert first.variant_id == second.variant_id


def test_factory_rejects_candidate_without_all_source_parents() -> None:
    attempt, draft, audit, candidate, candidate_representation = _lineage()
    detached = candidate.model_copy(update={"parent_theorem_ids": ()})

    with pytest.raises(TransformationIdentityError, match="parent theorem"):
        build_deterministic_variant_record(
            attempt=attempt,
            draft=draft,
            audit=audit,
            candidate=detached,
            candidate_representation=candidate_representation,
            polarity=Polarity.NEGATIVE,
        )


def test_factory_rejects_tampered_audit_link() -> None:
    attempt, draft, audit, candidate, candidate_representation = _lineage()
    tampered = audit.model_copy(update={"candidate_theorem_id": THM_A})

    with pytest.raises(TransformationIdentityError, match="audit_id mismatch"):
        build_deterministic_variant_record(
            attempt=attempt,
            draft=draft,
            audit=tampered,
            candidate=candidate,
            candidate_representation=candidate_representation,
            polarity=Polarity.NEGATIVE,
        )
