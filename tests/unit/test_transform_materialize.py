"""Phase-4 deterministic candidate and pair materialization invariants."""

from __future__ import annotations

import hashlib

import pytest

from leanfaith.schemas import (
    Applicability,
    IntendedRelation,
    QualityTier,
    ValidationStatus,
    make_id,
)
from leanfaith.transforms.materialize import (
    build_derived_theorem_record,
    build_deterministic_pair_record,
)
from leanfaith.transforms.protocol import (
    TransformationIdentityError,
    build_transformation_audit,
    build_variant_draft,
)
from tests.unit.record_factories import (
    ANC_A,
    ANC_B,
    CTX_ID,
    REPR_A,
    THM_A,
    THM_B,
    theorem_record,
)

_HASH = "4" * 64
_CODE = "theorem t (n : Nat) : n ≤ n := by sorry"


def _source_b():
    return theorem_record(
        theorem_id=THM_B,
        ancestry_id=ANC_B,
        root_ancestry_ids=(ANC_B,),
        declaration_name="donor",
        declaration_full_name="donor",
    )


def _draft(*, pair: bool = False):
    source_ids = (THM_A, THM_B) if pair else (THM_A,)
    representation_ids = (REPR_A, make_id("repr", {"materialize": "donor"})) if pair else (REPR_A,)
    trace = (
        {
            "operation": "replace",
            "primary_theorem_id": THM_A,
            "donor_theorem_id": THM_B,
        }
        if pair
        else {"operation": "replace"}
    )
    return build_variant_draft(
        source_theorem_ids=source_ids,
        source_representation_ids=representation_ids,
        context_id=CTX_ID,
        rule_id="n10_nearby_theorem" if pair else "n01_operator",
        rule_version="1.0.0",
        family_id="n10_nearby_theorem" if pair else "n01_operator",
        seed=17,
        candidate_code=_CODE,
        intended_relation=IntendedRelation.NEAR_MISS,
        intended_error_types=("E09", "E26") if pair else ("E11",),
        candidate_pool="deterministic_negative_provisional",
        transformation_trace=(trace,),
        generation_config_hash=_HASH,
    )


def _audit(draft, candidate):
    return build_transformation_audit(
        draft=draft,
        applicability=Applicability(applicable=True, reason_codes=()),
        audit_config_hash="5" * 64,
        recommended_validation_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        recommended_quality_tier=QualityTier.PROVISIONAL,
        candidate_theorem_id=candidate.theorem_id,
        candidate_representation_id=make_id("repr", {"candidate": candidate.theorem_id}),
    )


def test_unary_candidate_and_pair_are_deterministic_unlabeled_and_linked() -> None:
    source = theorem_record()
    draft = _draft()
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )
    replay = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )
    audit = _audit(draft, candidate)
    pair = build_deterministic_pair_record(
        source=source,
        candidate=candidate,
        draft=draft,
        audit=audit,
    )

    assert candidate == replay
    assert candidate.parent_theorem_ids == (source.theorem_id,)
    assert candidate.root_ancestry_ids == source.root_ancestry_ids
    assert candidate.statement_content_hash == hashlib.sha256(_CODE.encode()).hexdigest()
    assert pair.split_group_ids == source.root_ancestry_ids
    assert pair.resolved_label_id is None
    assert pair.metadata["resolved_semantic_label"] is False


def test_candidate_preserves_full_inline_elaboration_context_without_exposing_it_as_statement() -> (
    None
):
    source = theorem_record()
    draft = _draft()
    inline_source = (
        "import Mathlib\n"
        "namespace MaterializeFixture\n"
        "variable (unused : Nat)\n"
        f"{draft.candidate_code}\n"
        "end MaterializeFixture\n"
    )
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source=inline_source,
    )
    audit = _audit(draft, candidate)
    pair = build_deterministic_pair_record(
        source=source,
        candidate=candidate,
        draft=draft,
        audit=audit,
    )

    assert candidate.proof_stripped_declaration == draft.candidate_code
    assert candidate.inline_elaboration_source == inline_source
    assert candidate.inline_elaboration_source.count(draft.candidate_code) == 1
    assert pair.resolved_label_id is None


def test_candidate_rejects_ambiguous_inline_elaboration_context() -> None:
    draft = _draft()

    with pytest.raises(TransformationIdentityError, match="exactly once"):
        build_derived_theorem_record(
            draft=draft,
            sources=(theorem_record(),),
            primary_source_id=THM_A,
            elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            inline_elaboration_source=f"{draft.candidate_code}\n{draft.candidate_code}",
        )


def test_n10_candidate_unions_both_roots_and_pair_preserves_them() -> None:
    primary = theorem_record()
    donor = _source_b()
    draft = _draft(pair=True)
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(primary, donor),
        primary_source_id=primary.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )
    audit = _audit(draft, candidate)
    pair = build_deterministic_pair_record(
        source=primary,
        candidate=candidate,
        draft=draft,
        audit=audit,
        all_sources=(primary, donor),
    )

    assert candidate.parent_theorem_ids == tuple(sorted((THM_A, THM_B)))
    assert candidate.root_ancestry_ids == tuple(sorted((ANC_A, ANC_B)))
    assert pair.split_group_ids == tuple(sorted((ANC_A, ANC_B)))
    assert pair.metadata["near_miss"] is True


def test_source_lineage_mismatch_fails_before_id_creation() -> None:
    draft = _draft(pair=True)

    with pytest.raises(TransformationIdentityError, match="source theorem IDs"):
        build_derived_theorem_record(
            draft=draft,
            sources=(theorem_record(),),
            primary_source_id=THM_A,
            elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        )


def test_non_elaborating_candidate_cannot_be_materialized() -> None:
    with pytest.raises(TransformationIdentityError, match="elaborating status"):
        build_derived_theorem_record(
            draft=_draft(),
            sources=(theorem_record(),),
            primary_source_id=THM_A,
            elaboration_status=ValidationStatus.INVALID,
        )


def test_quarantined_audit_cannot_create_pair() -> None:
    source = theorem_record()
    draft = _draft()
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=THM_A,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )
    audit = build_transformation_audit(
        draft=draft,
        applicability=Applicability(applicable=True, reason_codes=()),
        audit_config_hash="5" * 64,
        recommended_validation_status=ValidationStatus.INVALID,
        recommended_quality_tier=QualityTier.UNKNOWN,
        candidate_theorem_id=candidate.theorem_id,
        candidate_representation_id=make_id("repr", {"candidate": candidate.theorem_id}),
        violation_codes=("candidate_invalid",),
    )

    with pytest.raises(TransformationIdentityError, match="elaborating candidate audit"):
        build_deterministic_pair_record(
            source=source,
            candidate=candidate,
            draft=draft,
            audit=audit,
        )


def test_metadata_cannot_claim_resolution() -> None:
    with pytest.raises(TransformationIdentityError, match="resolved-label"):
        build_derived_theorem_record(
            draft=_draft(),
            sources=(theorem_record(),),
            primary_source_id=THM_A,
            elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            metadata={"resolved_semantic_label": True},
        )


@pytest.mark.parametrize(
    "metadata",
    (
        {"same_claim": False},
        {"relation": "unrelated"},
        {"resolution_outcome": "not_same_claim"},
        {"quality_tier": "gold_human"},
        {"resolved_label_id": make_id("lbl", {"materialize": "forbidden"})},
        {"candidate_pool": "overridden"},
        {"draft_id": make_id("draft", {"materialize": "overridden"})},
    ),
)
def test_derived_metadata_rejects_label_and_lineage_fields(
    metadata: dict[str, str | bool],
) -> None:
    with pytest.raises(TransformationIdentityError, match="resolved-label"):
        build_derived_theorem_record(
            draft=_draft(),
            sources=(theorem_record(),),
            primary_source_id=THM_A,
            elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            metadata=metadata,
        )


def test_pair_metadata_rejects_label_and_lineage_fields() -> None:
    source = theorem_record()
    draft = _draft()
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=THM_A,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )
    audit = _audit(draft, candidate)

    with pytest.raises(TransformationIdentityError, match="resolved-label"):
        build_deterministic_pair_record(
            source=source,
            candidate=candidate,
            draft=draft,
            audit=audit,
            metadata={"same_claim": False},
        )


def test_internally_valid_wrong_rule_audit_is_rejected() -> None:
    source = theorem_record()
    draft = _draft()
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=THM_A,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )
    wrong_draft = draft.model_copy(
        update={
            "family_id": "n07_literal_bound",
            "rule_id": "n07_literal_bound",
        }
    )
    wrong_audit = build_transformation_audit(
        draft=wrong_draft,
        applicability=Applicability(applicable=True, reason_codes=()),
        audit_config_hash="5" * 64,
        recommended_validation_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        recommended_quality_tier=QualityTier.PROVISIONAL,
        candidate_theorem_id=candidate.theorem_id,
        candidate_representation_id=make_id("repr", {"candidate": candidate.theorem_id}),
    )

    with pytest.raises(TransformationIdentityError, match="audit does not match draft"):
        build_deterministic_pair_record(
            source=source,
            candidate=candidate,
            draft=draft,
            audit=wrong_audit,
        )


def test_audit_and_candidate_validation_status_must_match() -> None:
    source = theorem_record()
    draft = _draft()
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=THM_A,
        elaboration_status=ValidationStatus.ELABORATES,
    )
    audit = _audit(draft, candidate)

    with pytest.raises(TransformationIdentityError, match="validation status"):
        build_deterministic_pair_record(
            source=source,
            candidate=candidate,
            draft=draft,
            audit=audit,
        )


def test_n10_requires_every_source_at_pair_materialization() -> None:
    primary = theorem_record()
    donor = _source_b()
    draft = _draft(pair=True)
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(primary, donor),
        primary_source_id=primary.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )
    audit = _audit(draft, candidate)

    with pytest.raises(TransformationIdentityError, match="requires every source"):
        build_deterministic_pair_record(
            source=primary,
            candidate=candidate,
            draft=draft,
            audit=audit,
        )


def test_n10_pair_rejects_missing_donor_root_and_wrong_primary() -> None:
    primary = theorem_record()
    donor = _source_b()
    draft = _draft(pair=True)
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(primary, donor),
        primary_source_id=primary.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )
    audit = _audit(draft, candidate)
    missing_root = candidate.model_copy(update={"root_ancestry_ids": (ANC_A,)})

    with pytest.raises(TransformationIdentityError, match="root ancestries"):
        build_deterministic_pair_record(
            source=primary,
            candidate=missing_root,
            draft=draft,
            audit=audit,
            all_sources=(primary, donor),
        )
    with pytest.raises(TransformationIdentityError, match="primary and donor"):
        build_deterministic_pair_record(
            source=donor,
            candidate=candidate,
            draft=draft,
            audit=audit,
            all_sources=(primary, donor),
        )


def test_pair_rejects_noncanonical_candidate_parent_order() -> None:
    primary = theorem_record()
    donor = _source_b()
    draft = _draft(pair=True)
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(primary, donor),
        primary_source_id=primary.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )
    audit = _audit(draft, candidate)
    reversed_parents = candidate.model_copy(
        update={"parent_theorem_ids": tuple(reversed(candidate.parent_theorem_ids))}
    )

    with pytest.raises(TransformationIdentityError, match="candidate parents"):
        build_deterministic_pair_record(
            source=primary,
            candidate=reversed_parents,
            draft=draft,
            audit=audit,
            all_sources=(primary, donor),
        )


def test_n10_candidate_cannot_reverse_primary_and_donor_roles() -> None:
    primary = theorem_record()
    donor = _source_b()
    draft = _draft(pair=True)

    with pytest.raises(TransformationIdentityError, match="primary and donor"):
        build_derived_theorem_record(
            draft=draft,
            sources=(primary, donor),
            primary_source_id=donor.theorem_id,
            elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("metadata", {"same_claim": False}),
        ("metadata", {"generation_intention_only": False}),
        ("declaration_name", "tampered"),
        ("inline_elaboration_source", "theorem injected : True := by trivial"),
    ),
)
def test_pair_rejects_tampered_candidate_provenance(field: str, value: object) -> None:
    source = theorem_record()
    draft = _draft()
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=THM_A,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )
    audit = _audit(draft, candidate)
    tampered = candidate.model_copy(update={field: value})

    with pytest.raises(TransformationIdentityError):
        build_deterministic_pair_record(
            source=source,
            candidate=tampered,
            draft=draft,
            audit=audit,
        )
