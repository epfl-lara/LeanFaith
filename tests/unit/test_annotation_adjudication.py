from __future__ import annotations

import datetime

import pytest

from leanfaith.annotation_support.adjudication import (
    AdjudicationRoutingError,
    AdjudicationTrigger,
    adjudication_triggers,
    build_adjudication_queue,
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
    reference_issue: ReferenceIssue = ReferenceIssue.NONE,
    rationale: str = "",
) -> AnnotationRecord:
    target_id = make_id(PAIR_PREFIX, {"adjudication_target": target_index})
    return AnnotationRecord(
        annotation_id=make_id(
            ANNOTATION_PREFIX,
            {
                "adjudication_target": target_index,
                "annotator": annotator,
                "answer": answer.value,
                "relation": None if relation is None else relation.value,
                "confidence": confidence,
                "reference_issue": reference_issue.value,
            },
        ),
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=target_id,
        annotator_id=annotator,
        round_id="pilot_round_1",
        same_claim=answer,
        relation=relation,
        confidence=confidence,
        rationale=rationale,
        reference_issue=reference_issue,
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


def test_trigger_router_is_deterministic_and_never_resolves() -> None:
    first = _annotation(
        target_index=1,
        annotator="expert_1",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )
    second = _annotation(
        target_index=1,
        annotator="expert_2",
        answer=AnnotationAnswer.NOT_SAME_CLAIM,
        relation=RelationLabel.UNRELATED,
        confidence=2,
        reference_issue=ReferenceIssue.DEFINITE,
        rationale="The candidate states a different proposition.",
    )

    triggers = adjudication_triggers(first, second)

    assert set(triggers) == {
        AdjudicationTrigger.SAME_CLAIM_DISAGREEMENT,
        AdjudicationTrigger.TERMINAL_RELATION_DISAGREEMENT,
        AdjudicationTrigger.EITHER_REFERENCE_ISSUE_DEFINITE,
        AdjudicationTrigger.EITHER_CONFIDENCE_AT_MOST_2,
    }
    queue = build_adjudication_queue((first,), (second,), allow_test_fixture=True)
    assert queue.routed_target_count == 1
    assert queue.semantic_labels_created is False
    assert queue.adjudications_created is False
    assert queue.automatic_resolutions_created is False
    assert queue.items[0].semantic_resolution is None
    assert queue.items[0].auto_resolved is False
    assert queue.items[0].requires_human_adjudication is True


def test_agreement_without_trigger_is_not_auto_adjudicated() -> None:
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

    queue = build_adjudication_queue((first,), (second,), allow_test_fixture=True)

    assert queue.routed_target_count == 0
    assert queue.items == ()
    assert queue.human_action_required is False
    assert queue.automatic_resolutions_created is False


def test_cannot_assess_and_versioned_policy_route_to_humans() -> None:
    first = _annotation(
        target_index=2,
        annotator="expert_1",
        answer=AnnotationAnswer.CANNOT_ASSESS_YET,
        relation=None,
        rationale="A definition lookup is required.",
    )
    second = _annotation(
        target_index=2,
        annotator="expert_2",
        answer=AnnotationAnswer.CANNOT_ASSESS_YET,
        relation=None,
        rationale="A definition lookup is required.",
    )
    key = (first.target_kind.value, first.target_id)

    queue = build_adjudication_queue(
        (first,),
        (second,),
        policy_trigger_targets=(key,),
        allow_test_fixture=True,
    )

    assert set(queue.items[0].triggers) == {
        AdjudicationTrigger.EITHER_CANNOT_ASSESS_YET,
        AdjudicationTrigger.VERSIONED_POLICY_TRIGGER,
    }


def test_router_rejects_nonindependent_annotators() -> None:
    first = _annotation(
        target_index=1,
        annotator="expert",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )
    second = _annotation(
        target_index=1,
        annotator="expert",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )
    with pytest.raises(AdjudicationRoutingError, match="distinct annotators"):
        build_adjudication_queue((first,), (second,), allow_test_fixture=True)


def test_router_rejects_mixed_rounds_same_slot_and_campaign_drift() -> None:
    first = _annotation(
        target_index=1,
        annotator="expert_1",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )
    first_other_round = _annotation(
        target_index=2,
        annotator="expert_1",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    ).model_copy(update={"round_id": "pilot_round_2"})
    second = _annotation(
        target_index=1,
        annotator="expert_2",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )
    second_target_2 = _annotation(
        target_index=2,
        annotator="expert_2",
        answer=AnnotationAnswer.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )
    with pytest.raises(AdjudicationRoutingError, match="one annotator, principal, round"):
        build_adjudication_queue(
            (first, first_other_round),
            (second, second_target_2),
            allow_test_fixture=True,
        )

    same_slot = second.model_copy(
        update={"metadata": {**second.metadata, "annotator_slot": "independent_annotator_1"}}
    )
    with pytest.raises(AdjudicationRoutingError, match="independent annotator slots"):
        build_adjudication_queue((first,), (same_slot,), allow_test_fixture=True)

    other_campaign = second.model_copy(
        update={"metadata": {**second.metadata, "campaign_id": "different_campaign"}}
    )
    with pytest.raises(AdjudicationRoutingError, match="different campaigns"):
        build_adjudication_queue((first,), (other_campaign,), allow_test_fixture=True)


def test_router_rejects_fixture_by_default_cross_round_and_same_principal() -> None:
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
    with pytest.raises(AdjudicationRoutingError, match="test-fixture"):
        build_adjudication_queue((first,), (second,))

    other_round = second.model_copy(update={"round_id": "pilot_round_2"})
    with pytest.raises(AdjudicationRoutingError, match="different rounds"):
        build_adjudication_queue((first,), (other_round,), allow_test_fixture=True)

    same_principal = second.model_copy(
        update={
            "metadata": {
                **second.metadata,
                "annotator_principal_hash": first.metadata["annotator_principal_hash"],
            }
        }
    )
    with pytest.raises(AdjudicationRoutingError, match="distinct human principals"):
        build_adjudication_queue((first,), (same_principal,), allow_test_fixture=True)
