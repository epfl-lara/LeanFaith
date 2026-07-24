"""LF-004: ResolvedLabel invariants, NL-Lean records, LLM calls, annotations, predictions."""

from __future__ import annotations

import pytest

from leanfaith.schemas import (
    AnnotationAnswer,
    Decision,
    FaithfulnessLevels,
    LLMRole,
    ParseStatus,
    PredictionRecord,
    QualityTier,
    ReferencePairLink,
    RelationLabel,
    ResolutionOutcome,
    SemanticLabelTargetKind,
    check_label_target_link,
    make_id,
)
from tests.unit.record_factories import (
    LABEL_ID,
    NL_ID,
    PAIR_ID,
    THM_B,
    annotation_record,
    llm_call_record,
    nl_lean_record,
    pair_record,
    resolved_label,
)

# --- ResolvedLabel invariants (§11.8) ---


def test_appendix_c1_alpha_restatement_label() -> None:
    label = resolved_label(
        faithfulness_levels=FaithfulnessLevels(
            F0_representation_equivalent=True,
            F1_same_claim=True,
            F2_truth_equivalent=True,
        ),
        truth_A_implies_B=True,
        truth_B_implies_A=True,
    )
    assert label.same_claim is True


def test_appendix_c2_claim_erasure_f2_true_f1_false() -> None:
    # E25 semantic erasure: truth-equivalent yet not the same claim (§3.2).
    label = resolved_label(
        same_claim=False,
        resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        relation=RelationLabel.INCOMPARABLE,
        faithfulness_levels=FaithfulnessLevels(
            F0_representation_equivalent=False,
            F1_same_claim=False,
            F2_truth_equivalent=True,
        ),
        truth_A_implies_B=True,
        truth_B_implies_A=True,
        error_types=("E25",),
        quality_tier=QualityTier.GOLD_HUMAN,
        resolution_method="expert_adjudication",
        eval_eligibility=True,
    )
    assert label.faithfulness_levels.F2_truth_equivalent is True
    assert label.same_claim is False


def test_appendix_c7_review_route() -> None:
    label = resolved_label(
        same_claim=None,
        resolution_outcome=ResolutionOutcome.UNRESOLVED,
        relation=None,
        faithfulness_levels=FaithfulnessLevels(),
        error_types=("E27",),
        quality_tier=QualityTier.UNKNOWN,
        resolution_method=None,
        requires_adjudication=True,
        train_eligibility=False,
        eval_eligibility=False,
    )
    assert label.requires_adjudication


def test_appendix_c8_terminal_ambiguity() -> None:
    label = resolved_label(
        same_claim=None,
        resolution_outcome=ResolutionOutcome.AMBIGUOUS,
        relation=RelationLabel.AMBIGUOUS,
        faithfulness_levels=FaithfulnessLevels(),
        error_types=("E30",),
        quality_tier=QualityTier.GOLD_HUMAN,
        resolution_method="expert_adjudication",
        requires_adjudication=False,
        train_eligibility=False,
        eval_eligibility=True,
    )
    assert label.resolution_outcome is ResolutionOutcome.AMBIGUOUS


def test_f1_must_equal_same_claim() -> None:
    with pytest.raises(ValueError, match="F1_same_claim"):
        resolved_label(
            faithfulness_levels=FaithfulnessLevels(F1_same_claim=False),
        )


def test_outcome_same_claim_requires_true() -> None:
    with pytest.raises(ValueError, match="requires same_claim=true"):
        resolved_label(
            same_claim=None,
            faithfulness_levels=FaithfulnessLevels(),
            relation=None,
        )


def test_null_never_means_false_ambiguous_requires_null() -> None:
    with pytest.raises(ValueError, match="requires same_claim=null"):
        resolved_label(
            same_claim=False,
            resolution_outcome=ResolutionOutcome.AMBIGUOUS,
            relation=RelationLabel.AMBIGUOUS,
            faithfulness_levels=FaithfulnessLevels(F1_same_claim=False),
            quality_tier=QualityTier.GOLD_HUMAN,
            requires_adjudication=False,
        )


def test_unresolved_requires_unknown_tier_and_adjudication() -> None:
    with pytest.raises(ValueError, match="unresolved review route"):
        resolved_label(
            same_claim=None,
            resolution_outcome=ResolutionOutcome.UNRESOLVED,
            relation=None,
            faithfulness_levels=FaithfulnessLevels(),
            quality_tier=QualityTier.GOLD_HUMAN,
            requires_adjudication=True,
            train_eligibility=False,
        )


def test_terminal_ambiguity_rejects_pending_adjudication() -> None:
    with pytest.raises(ValueError, match="terminal ambiguity"):
        resolved_label(
            same_claim=None,
            resolution_outcome=ResolutionOutcome.AMBIGUOUS,
            relation=RelationLabel.AMBIGUOUS,
            faithfulness_levels=FaithfulnessLevels(),
            quality_tier=QualityTier.GOLD_HUMAN,
            requires_adjudication=True,
            train_eligibility=False,
        )


def test_same_claim_true_requires_equivalent_relation() -> None:
    with pytest.raises(ValueError, match="relation=equivalent"):
        resolved_label(relation=RelationLabel.A_STRONGER)


def test_same_claim_false_rejects_equivalent_relation() -> None:
    with pytest.raises(ValueError, match="directional/incomparable/unrelated"):
        resolved_label(
            same_claim=False,
            resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
            relation=RelationLabel.EQUIVALENT,
            faithfulness_levels=FaithfulnessLevels(F1_same_claim=False),
            quality_tier=QualityTier.GOLD_HUMAN,
        )


def test_f2_true_requires_both_truth_directions() -> None:
    with pytest.raises(ValueError, match="F2_truth_equivalent"):
        resolved_label(
            faithfulness_levels=FaithfulnessLevels(F1_same_claim=True, F2_truth_equivalent=True),
            truth_A_implies_B=True,
            truth_B_implies_A=None,
        )


def test_f2_false_requires_refuted_direction() -> None:
    with pytest.raises(ValueError, match="F2_truth_equivalent"):
        resolved_label(
            faithfulness_levels=FaithfulnessLevels(F1_same_claim=True, F2_truth_equivalent=False),
            truth_A_implies_B=True,
            truth_B_implies_A=True,
        )


def test_failed_search_stays_null_and_is_valid() -> None:
    label = resolved_label(
        truth_A_implies_B=None,
        truth_B_implies_A=None,
        faithfulness_levels=FaithfulnessLevels(F1_same_claim=True, F2_truth_equivalent=None),
    )
    assert label.faithfulness_levels.F2_truth_equivalent is None


def test_unknown_tier_cannot_train() -> None:
    with pytest.raises(ValueError, match="no binary training target"):
        resolved_label(
            same_claim=None,
            resolution_outcome=ResolutionOutcome.UNRESOLVED,
            relation=None,
            faithfulness_levels=FaithfulnessLevels(),
            quality_tier=QualityTier.UNKNOWN,
            requires_adjudication=True,
            train_eligibility=True,
        )


def test_provisional_never_eval_eligible() -> None:
    with pytest.raises(ValueError, match="never evaluation-eligible"):
        resolved_label(quality_tier=QualityTier.PROVISIONAL, eval_eligibility=True)


def test_equivalent_admits_only_cosmetic_codes() -> None:
    with pytest.raises(ValueError, match="only E29"):
        resolved_label(error_types=("E01",))
    assert resolved_label(error_types=("E29",)).error_types == ("E29",)


def test_label_target_prefix_matches_kind() -> None:
    with pytest.raises(ValueError, match="does not match target_kind"):
        resolved_label(target_kind=SemanticLabelTargetKind.NL_LEAN, target_id=PAIR_ID)


# --- Cross-record reverse link (§11.8) ---


def test_label_link_round_trip() -> None:
    pair = pair_record(resolved_label_id=LABEL_ID)
    label = resolved_label()
    assert check_label_target_link(label, pair) == []


def test_label_link_detects_missing_backlink() -> None:
    pair = pair_record()
    label = resolved_label()
    violations = check_label_target_link(label, pair)
    assert any("point back" in violation for violation in violations)


def test_label_link_detects_kind_mismatch() -> None:
    nl = nl_lean_record(resolved_label_id=LABEL_ID)
    label = resolved_label()  # lean_pair label
    violations = check_label_target_link(label, nl)
    assert any("target_kind" in violation for violation in violations)


# --- NLPLeanRecord (§11.9) ---


def test_nl_lean_reference_pairs_must_be_declared() -> None:
    link = ReferencePairLink(reference_theorem_id=THM_B, pair_id=PAIR_ID)
    with pytest.raises(ValueError, match="undeclared reference"):
        nl_lean_record(reference_pairs=(link,))


def test_nl_lean_declared_reference_accepted() -> None:
    link = ReferencePairLink(reference_theorem_id=THM_B, pair_id=PAIR_ID)
    record = nl_lean_record(reference_theorem_ids=(THM_B,), reference_pairs=(link,))
    assert record.reference_pairs[0].pair_id == PAIR_ID


def test_nl_lean_requires_problem_group_in_split_groups() -> None:
    with pytest.raises(ValueError, match="NL problem group"):
        nl_lean_record(split_group_ids=("anc:" + "9" * 64,))


def test_nl_lean_requires_nonempty_statement() -> None:
    with pytest.raises(ValueError, match="at least 1 character"):
        nl_lean_record(nl_statement="")


# --- LLMCallRecord (§11.11) ---


def test_llm_call_parse_status_consistency() -> None:
    with pytest.raises(ValueError, match="requires parsed_output"):
        llm_call_record(parsed_output=None)
    with pytest.raises(ValueError, match="cannot carry parsed_output"):
        llm_call_record(parse_status=ParseStatus.PARSE_FAILED)


def test_llm_call_private_source_needs_approval() -> None:
    with pytest.raises(ValueError, match=r"9\.2"):
        llm_call_record(private_source_content=True)
    record = llm_call_record(
        private_source_content=True,
        external_api_approval="ADR-0005-sft-classic-external-api",
    )
    assert record.external_api_approval is not None


def test_primary_eval_judge_never_supervises() -> None:
    with pytest.raises(ValueError, match="excluded from all training supervision"):
        llm_call_record(role=LLMRole.PRIMARY_EVAL_JUDGE, supervision_eligible=True)


# --- AnnotationRecord (§18.5) ---


def test_annotation_rationale_required_for_not_same() -> None:
    with pytest.raises(ValueError, match="rationale"):
        annotation_record(
            same_claim=AnnotationAnswer.NOT_SAME_CLAIM,
            relation=RelationLabel.A_STRONGER,
        )


def test_annotation_confidence_bounds() -> None:
    with pytest.raises(ValueError, match="less than or equal"):
        annotation_record(confidence=6)


def test_annotation_cannot_assess_yet_is_valid_route() -> None:
    record = annotation_record(
        same_claim=AnnotationAnswer.CANNOT_ASSESS_YET,
        relation=None,
        rationale="A required definition is unavailable in the displayed context.",
    )
    assert record.same_claim is AnnotationAnswer.CANNOT_ASSESS_YET


def test_annotation_cannot_assess_yet_requires_rationale() -> None:
    with pytest.raises(ValueError, match="rationale"):
        annotation_record(
            same_claim=AnnotationAnswer.CANNOT_ASSESS_YET,
            relation=None,
        )


# --- PredictionRecord (§20.6) ---


def _prediction(**overrides: object) -> PredictionRecord:
    payload: dict[str, object] = {
        "record_id": PAIR_ID,
        "method": "lexical_edit",
        "method_version": "v1",
        "score_same_claim": 0.9,
        "score_ambiguous": 0.05,
        "decision": Decision.ACCEPT,
        "elapsed_ms": 12,
        "config_hash": "7" * 64,
    }
    payload.update(overrides)
    return PredictionRecord.model_validate(payload)


def test_prediction_accepts_pair_and_nllean_ids() -> None:
    assert _prediction().record_id == PAIR_ID
    assert _prediction(record_id=NL_ID).record_id == NL_ID


def test_prediction_rejects_other_record_ids() -> None:
    with pytest.raises(ValueError, match="pattern"):
        _prediction(record_id=make_id("thm", {"x": 1}))


def test_prediction_rejects_unknown_relation() -> None:
    with pytest.raises(ValueError, match="unknown relation"):
        _prediction(relation_scores={"sorta_equal": 0.5})


def test_prediction_legacy_error_scores_migrate_to_optional_auxiliary_scores() -> None:
    prediction = _prediction(error_type_scores={"E99": 0.5})
    assert prediction.optional_auxiliary_scores == {"E99": 0.5}


def test_prediction_score_bounds() -> None:
    with pytest.raises(ValueError, match="less than or equal"):
        _prediction(score_same_claim=1.5)
