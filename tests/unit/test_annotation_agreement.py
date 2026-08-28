from __future__ import annotations

import datetime

import pytest

from leanfaith.annotation_support.agreement import (
    AnnotationAgreementError,
    compute_annotation_agreement,
)
from leanfaith.schemas import (
    ANNOTATION_PREFIX,
    PAIR_PREFIX,
    AnnotationAnswer,
    AnnotationRecord,
    ReferenceIssue,
    RelationLabel,
    SemanticLabelTargetKind,
    make_id,
)

UTC_NOW = datetime.datetime(2026, 7, 28, tzinfo=datetime.UTC)


def _annotation(
    *,
    target_index: int,
    annotator: str,
    answer: AnnotationAnswer,
    relation: RelationLabel | None,
    confidence: int = 4,
    rationale: str = "",
) -> AnnotationRecord:
    target_id = make_id(PAIR_PREFIX, {"fixture_target": target_index})
    annotation_id = make_id(
        ANNOTATION_PREFIX,
        {
            "fixture_target": target_index,
            "annotator": annotator,
            "answer": answer.value,
            "relation": None if relation is None else relation.value,
        },
    )
    return AnnotationRecord(
        annotation_id=annotation_id,
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=target_id,
        annotator_id=annotator,
        round_id="pilot_round_1",
        same_claim=answer,
        relation=relation,
        confidence=confidence,
        rationale=rationale,
        reference_issue=ReferenceIssue.NONE,
        created_at=UTC_NOW,
        metadata={
            "campaign_id": "lf021_prevalence_v1",
            "annotator_slot": (
                "independent_annotator_1" if annotator == "expert_1" else "independent_annotator_2"
            ),
            "annotator_principal_hash": ("1" if annotator == "expert_1" else "2") * 64,
            "guideline_sha256": "a" * 64,
            "assignment_mode": "test_fixture",
            "origin_assurance": "test_fixture",
            "operator_attestation_verified": True,
            "backend_origin_verified": False,
            "human_gold_eligible": False,
            "fixture_only": True,
            "import_role": "raw_annotation_test_fixture",
            "raw_vote_only": True,
            "resolved_label_created": False,
            "gold_label_created": False,
            "training_eligible": False,
        },
    )


def test_agreement_uses_all_raw_categories_without_adjudication() -> None:
    first = (
        _annotation(
            target_index=1,
            annotator="expert_1",
            answer=AnnotationAnswer.SAME_CLAIM,
            relation=RelationLabel.EQUIVALENT,
        ),
        _annotation(
            target_index=2,
            annotator="expert_1",
            answer=AnnotationAnswer.NOT_SAME_CLAIM,
            relation=RelationLabel.A_STRONGER,
            rationale="A is stronger.",
        ),
        _annotation(
            target_index=3,
            annotator="expert_1",
            answer=AnnotationAnswer.AMBIGUOUS,
            relation=RelationLabel.AMBIGUOUS,
            rationale="The intended domain is underdetermined.",
        ),
        _annotation(
            target_index=4,
            annotator="expert_1",
            answer=AnnotationAnswer.CANNOT_ASSESS_YET,
            relation=None,
            rationale="A definition lookup is required.",
        ),
    )
    second = (
        _annotation(
            target_index=1,
            annotator="expert_2",
            answer=AnnotationAnswer.SAME_CLAIM,
            relation=RelationLabel.EQUIVALENT,
        ),
        _annotation(
            target_index=2,
            annotator="expert_2",
            answer=AnnotationAnswer.NOT_SAME_CLAIM,
            relation=RelationLabel.B_STRONGER,
            rationale="B is stronger.",
        ),
        _annotation(
            target_index=3,
            annotator="expert_2",
            answer=AnnotationAnswer.AMBIGUOUS,
            relation=RelationLabel.AMBIGUOUS,
            rationale="The intended domain is underdetermined.",
        ),
        _annotation(
            target_index=4,
            annotator="expert_2",
            answer=AnnotationAnswer.NOT_SAME_CLAIM,
            relation=RelationLabel.UNRELATED,
            rationale="The candidate states a different claim.",
        ),
    )

    report = compute_annotation_agreement(first, second, allow_test_fixture=True)

    assert report.target_count == 4
    assert report.same_claim_raw_agreement == 0.75
    assert report.same_claim_kappa.status == "defined"
    assert report.same_claim_kappa.value == pytest.approx(2 / 3)
    assert report.relation_raw_agreement == 0.5
    assert report.adjudicated_labels_substituted is False
    assert report.gate_closed_by_report is False
    cannot_assess = next(
        item for item in report.same_claim_categories if item.category == "cannot_assess_yet"
    )
    assert cannot_assess.first_count == 1
    assert cannot_assess.second_count == 0
    assert cannot_assess.both_over_either == 0.0


def test_degenerate_perfect_agreement_reports_undefined_kappa() -> None:
    first = (
        _annotation(
            target_index=1,
            annotator="expert_1",
            answer=AnnotationAnswer.SAME_CLAIM,
            relation=RelationLabel.EQUIVALENT,
        ),
    )
    second = (
        _annotation(
            target_index=1,
            annotator="expert_2",
            answer=AnnotationAnswer.SAME_CLAIM,
            relation=RelationLabel.EQUIVALENT,
        ),
    )

    report = compute_annotation_agreement(first, second, allow_test_fixture=True)

    assert report.same_claim_raw_agreement == 1.0
    assert report.same_claim_kappa.status == "undefined_degenerate_marginals"
    assert report.same_claim_kappa.value is None


def test_agreement_rejects_unpaired_target_sets() -> None:
    first = (
        _annotation(
            target_index=1,
            annotator="expert_1",
            answer=AnnotationAnswer.SAME_CLAIM,
            relation=RelationLabel.EQUIVALENT,
        ),
    )
    second = (
        _annotation(
            target_index=2,
            annotator="expert_2",
            answer=AnnotationAnswer.SAME_CLAIM,
            relation=RelationLabel.EQUIVALENT,
        ),
    )
    with pytest.raises(AnnotationAgreementError, match="target sets differ"):
        compute_annotation_agreement(first, second, allow_test_fixture=True)


def test_agreement_rejects_same_slot_and_campaign_drift() -> None:
    first = _annotation(
        target_index=1,
        annotator="expert_1",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )
    second = _annotation(
        target_index=1,
        annotator="expert_2",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )
    same_slot = second.model_copy(
        update={"metadata": {**second.metadata, "annotator_slot": "independent_annotator_1"}}
    )
    with pytest.raises(AnnotationAgreementError, match="independent annotator slots"):
        compute_annotation_agreement((first,), (same_slot,), allow_test_fixture=True)

    other_campaign = second.model_copy(
        update={"metadata": {**second.metadata, "campaign_id": "different_campaign"}}
    )
    with pytest.raises(AnnotationAgreementError, match="different campaigns"):
        compute_annotation_agreement((first,), (other_campaign,), allow_test_fixture=True)


def test_agreement_rejects_fixture_by_default_round_drift_and_same_principal() -> None:
    first = _annotation(
        target_index=1,
        annotator="expert_1",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )
    second = _annotation(
        target_index=1,
        annotator="expert_2",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )
    with pytest.raises(AnnotationAgreementError, match="test-fixture"):
        compute_annotation_agreement((first,), (second,))

    other_round = second.model_copy(update={"round_id": "pilot_round_2"})
    with pytest.raises(AnnotationAgreementError, match="different rounds"):
        compute_annotation_agreement((first,), (other_round,), allow_test_fixture=True)

    same_principal = second.model_copy(
        update={
            "metadata": {
                **second.metadata,
                "annotator_principal_hash": first.metadata["annotator_principal_hash"],
            }
        }
    )
    with pytest.raises(AnnotationAgreementError, match="distinct human principals"):
        compute_annotation_agreement((first,), (same_principal,), allow_test_fixture=True)
