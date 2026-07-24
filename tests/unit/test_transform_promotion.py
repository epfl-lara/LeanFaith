"""Exact, fail-closed tests for PLAN.md §15 transformation promotion."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from leanfaith.schemas.enums import QualityTier, TransformationFamilyStatus
from leanfaith.transforms.promotion import (
    AuditInvariantCheck,
    ConsensusRouteEvidence,
    CounterexampleRouteEvidence,
    DirectionalSeparatorRouteEvidence,
    ExpertAdjudicationRouteEvidence,
    HeldOutAuditCheck,
    NegativePromotionInput,
    NegativeRouteEvidence,
    PositiveAuditItem,
    PositiveAuditOutcome,
    PositiveFamilyAuditInput,
    PositiveItemPromotionInput,
    PromotionIntegrityError,
    build_family_promotion_decision,
    clopper_pearson_lower_95,
    evaluate_negative_promotion,
    evaluate_positive_item_promotion,
    recompute_positive_promotion,
    verify_family_promotion_decision,
    verify_negative_promotion_result,
    verify_positive_promotion_result,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
AUDIT_ID = f"audit:{'1' * 64}"


def _evidence_id(digit: str) -> str:
    return f"ev:{digit * 64}"


def _item(
    index: int,
    outcome: PositiveAuditOutcome = PositiveAuditOutcome.SAME_CLAIM,
    *,
    invariants_hold: bool = True,
    error_types: tuple[str, ...] = (),
    erasure_pattern: str | None = None,
) -> PositiveAuditItem:
    return PositiveAuditItem(
        item_id=f"item-{index:04d}",
        outcome=outcome,
        invariant_checks=(
            AuditInvariantCheck(check_id="structural-invariant", passed=invariants_hold),
        ),
        error_types=error_types,
        semantic_erasure_pattern=erasure_pattern,
    )


def _positive_audit(
    *,
    successes: int = 198,
    incorrect: int = 2,
    not_elaborated: int = 0,
    blinded: bool = True,
    frozen: bool = True,
    held_out_passed: bool = True,
    invariant_failure_at: int | None = None,
    erasure_patterns: tuple[str, ...] = (),
) -> PositiveFamilyAuditInput:
    items: list[PositiveAuditItem] = []
    for index in range(successes):
        items.append(
            _item(
                index,
                invariants_hold=index != invariant_failure_at,
            )
        )
    for offset in range(incorrect):
        index = successes + offset
        pattern = erasure_patterns[offset] if offset < len(erasure_patterns) else None
        items.append(
            _item(
                index,
                PositiveAuditOutcome.INCORRECT,
                invariants_hold=index != invariant_failure_at,
                error_types=("E25",) if pattern else (),
                erasure_pattern=pattern,
            )
        )
    for offset in range(not_elaborated):
        index = successes + incorrect + offset
        items.append(
            _item(
                index,
                PositiveAuditOutcome.NOT_ELABORATED,
                invariants_hold=index != invariant_failure_at,
            )
        )
    return PositiveFamilyAuditInput(
        audit_id=AUDIT_ID,
        family_id="p01_alpha",
        rule_version="1.0.0",
        audit_manifest_hash=HASH_A,
        frozen_design_hash=HASH_B,
        blinded=blinded,
        design_frozen_before_audit=frozen,
        required_invariant_ids=("structural-invariant",),
        items=tuple(items),
        held_out_checks=(
            HeldOutAuditCheck(
                check_id="heldout-domain",
                source_or_domain="heldout-algebra",
                audited_count=50,
                manifest_hash=HASH_A,
                passed=held_out_passed,
            ),
        ),
    )


def test_clopper_pearson_special_cases_match_closed_forms() -> None:
    assert clopper_pearson_lower_95(0, 200) == 0.0
    assert clopper_pearson_lower_95(200, 200) == pytest.approx(0.025 ** (1.0 / 200.0), abs=2e-14)
    assert clopper_pearson_lower_95(1, 200) == pytest.approx(
        1.0 - 0.975 ** (1.0 / 200.0), abs=2e-14
    )


@pytest.mark.parametrize(
    ("successes", "total", "error"),
    [
        (-1, 2, ValueError),
        (3, 2, ValueError),
        (0, -1, ValueError),
        (True, 2, TypeError),
        (1, False, TypeError),
        (1.0, 2, TypeError),
    ],
)
def test_clopper_pearson_rejects_invalid_counts(
    successes: object, total: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        clopper_pearson_lower_95(successes, total)  # type: ignore[arg-type]


def test_gate_4a_passes_at_inclusive_point_threshold() -> None:
    result = recompute_positive_promotion(_positive_audit())

    assert result.selected_count == 200
    assert result.denominator_n == 200
    assert result.successes == 198
    assert result.point_precision == 0.99
    assert result.clopper_pearson_lower_95 > 0.95
    assert result.gate_4a_passed
    assert result.promoted_family_status == TransformationFamilyStatus.GOLD_PROMOTED
    assert result.unlocked_item_tier == QualityTier.GOLD_CONSERVATIVE_TRANSFORM
    assert result.failure_reasons == ()


def test_non_elaborating_items_are_recorded_but_not_in_precision_denominator() -> None:
    result = recompute_positive_promotion(_positive_audit(not_elaborated=1))

    assert result.selected_count == 201
    assert result.denominator_n == 200
    assert result.successes == 198
    assert result.gate_4a_passed


def test_all_terminal_non_success_outcomes_stay_in_precision_denominator() -> None:
    audit = _positive_audit(successes=200, incorrect=0)
    replacements = (
        PositiveAuditOutcome.INCORRECT,
        PositiveAuditOutcome.AMBIGUOUS,
        PositiveAuditOutcome.UNRESOLVED,
        PositiveAuditOutcome.POLICY_VIOLATION,
    )
    items = list(audit.items)
    for index, outcome in enumerate(replacements):
        items[index] = _item(index, outcome)
    result = recompute_positive_promotion(audit.model_copy(update={"items": tuple(items)}))

    assert result.denominator_n == 200
    assert result.successes == 196
    assert result.point_precision == 0.98
    assert "point_precision_below_0.99" in result.failure_reasons


@pytest.mark.parametrize(
    ("audit", "reason"),
    [
        (
            _positive_audit(successes=197, incorrect=2),
            "eligible_denominator_below_200",
        ),
        (
            _positive_audit(successes=197, incorrect=3),
            "point_precision_below_0.99",
        ),
        (_positive_audit(blinded=False), "audit_not_blinded"),
        (
            _positive_audit(frozen=False),
            "design_not_frozen_before_audit",
        ),
        (
            _positive_audit(invariant_failure_at=0),
            "family_invariant_failure",
        ),
        (
            _positive_audit(held_out_passed=False),
            "held_out_source_domain_audit_failed",
        ),
        (
            _positive_audit(erasure_patterns=("same-collapse", "same-collapse")),
            "recurrent_semantic_erasure_e25",
        ),
    ],
)
def test_gate_4a_fails_each_required_condition(
    audit: PositiveFamilyAuditInput, reason: str
) -> None:
    result = recompute_positive_promotion(audit)

    assert not result.gate_4a_passed
    assert result.promoted_family_status is None
    assert result.unlocked_item_tier is None
    assert reason in result.failure_reasons


def test_recurrent_e25_cannot_be_hidden_by_renaming_patterns() -> None:
    audit = _positive_audit(erasure_patterns=("collapse-a", "collapse-b"))
    result = recompute_positive_promotion(audit)

    assert result.recurrent_semantic_erasure_patterns == ("collapse-a", "collapse-b")
    assert not result.gate_4a_passed
    assert "recurrent_semantic_erasure_e25" in result.failure_reasons
    decision = build_family_promotion_decision(
        audit,
        rule_id="p01_alpha_rule",
        policy_version="transformation_promotion_v1",
        parent_registry_hash=HASH_A,
        promotion_policy_hash=HASH_B,
    )
    assert decision.decision == TransformationFamilyStatus.EXPERIMENTAL
    assert decision.recurrent_semantic_erasure_patterns == ("collapse-a", "collapse-b")


def test_positive_audit_models_are_strict_frozen_and_recomputable() -> None:
    audit = _positive_audit()
    result = recompute_positive_promotion(audit)

    verify_positive_promotion_result(audit, result)
    assert len(result.audit_input_hash) == 64
    with pytest.raises(ValidationError):
        audit.blinded = False
    with pytest.raises(PromotionIntegrityError):
        verify_positive_promotion_result(
            audit,
            result.model_copy(update={"successes": result.successes - 1}),
        )
    with pytest.raises(ValidationError):
        PositiveFamilyAuditInput.model_validate(
            {
                **audit.model_dump(mode="json"),
                "blinded": "true",
            }
        )


def test_persistent_family_decision_is_bound_to_registry_policy_and_recomputation() -> None:
    audit = _positive_audit()
    decision = build_family_promotion_decision(
        audit,
        rule_id="p01_alpha_rule",
        policy_version="transformation_promotion_v1",
        parent_registry_hash=HASH_A,
        promotion_policy_hash=HASH_B,
    )

    assert decision.decision == TransformationFamilyStatus.GOLD_PROMOTED
    assert decision.unlocked_quality_tier == QualityTier.GOLD_CONSERVATIVE_TRANSFORM
    assert decision.audit_input_hash == recompute_positive_promotion(audit).audit_input_hash
    verify_family_promotion_decision(audit, decision)
    with pytest.raises(PromotionIntegrityError):
        verify_family_promotion_decision(
            audit,
            decision.model_copy(update={"parent_registry_hash": "c" * 64}),
        )


def test_failed_audit_persists_non_gold_decision_with_sorted_reasons() -> None:
    audit = _positive_audit(blinded=False, held_out_passed=False)
    decision = build_family_promotion_decision(
        audit,
        rule_id="p01_alpha_rule",
        policy_version="transformation_promotion_v1",
        parent_registry_hash=HASH_A,
        promotion_policy_hash=HASH_B,
        non_gold_status=TransformationFamilyStatus.SILVER,
    )

    assert decision.decision == TransformationFamilyStatus.SILVER
    assert decision.unlocked_quality_tier is None
    assert decision.reason_codes == tuple(sorted(decision.reason_codes))
    assert set(decision.reason_codes) == {
        "audit_not_blinded",
        "held_out_source_domain_audit_failed",
    }
    experimental = build_family_promotion_decision(
        audit,
        rule_id="p01_alpha_rule",
        policy_version="transformation_promotion_v1",
        parent_registry_hash=HASH_A,
        promotion_policy_hash=HASH_B,
        non_gold_status=TransformationFamilyStatus.EXPERIMENTAL,
    )
    assert experimental.decision_id != decision.decision_id
    verify_family_promotion_decision(audit, decision)
    with pytest.raises(ValueError, match="cannot receive gold_promoted"):
        build_family_promotion_decision(
            audit,
            rule_id="p01_alpha_rule",
            policy_version="transformation_promotion_v1",
            parent_registry_hash=HASH_A,
            promotion_policy_hash=HASH_B,
            non_gold_status=TransformationFamilyStatus.GOLD_PROMOTED,
        )


def test_positive_audit_rejects_duplicate_items_and_malformed_erasure_metadata() -> None:
    audit = _positive_audit()
    with pytest.raises(ValidationError, match="item_id values must be unique"):
        PositiveFamilyAuditInput(
            **{
                **audit.model_dump(),
                "items": (audit.items[0], audit.items[0]),
            }
        )
    with pytest.raises(ValidationError, match="required exactly when E25"):
        PositiveAuditItem(
            item_id="bad-erasure",
            outcome=PositiveAuditOutcome.INCORRECT,
            invariant_checks=(AuditInvariantCheck(check_id="structural-invariant", passed=True),),
            error_types=("E25",),
        )
    with pytest.raises(ValidationError, match="do not exactly match"):
        PositiveFamilyAuditInput(
            **{
                **audit.model_dump(),
                "items": (
                    audit.items[0].model_copy(update={"invariant_checks": ()}),
                    *audit.items[1:],
                ),
            }
        )
    with pytest.raises(ValidationError):
        PositiveFamilyAuditInput.model_validate(
            {
                **audit.model_dump(mode="json"),
                "intended_relation": "equivalent",
            }
        )


def test_positive_item_requires_all_section_15_4_conditions() -> None:
    audit = _positive_audit()
    decision = build_family_promotion_decision(
        audit,
        rule_id="p01_alpha_rule",
        policy_version="transformation_promotion_v1",
        parent_registry_hash=HASH_A,
        promotion_policy_hash=HASH_B,
    )
    promotion = PositiveItemPromotionInput(
        item_id="draft-p01-1",
        family_id="p01_alpha",
        rule_version="1.0.0",
        family_status=TransformationFamilyStatus.GOLD_PROMOTED,
        current_promotion_policy_hash=HASH_B,
        family_audit_input=audit,
        family_decision=decision,
        positive_allowlisted=True,
        exact_local_structural_diff=True,
        approved_semantic_atom_mapping=True,
        dependencies_assumptions_literals_conclusion_heads_preserved=True,
        inverse_roundtrip_or_rule_certificate=True,
        no_proof_constants_or_admissions=True,
        quarantine_violation=False,
    )

    passed = evaluate_positive_item_promotion(promotion)
    assert passed.promoted
    assert passed.resulting_tier == QualityTier.GOLD_CONSERVATIVE_TRANSFORM

    failed = evaluate_positive_item_promotion(
        promotion.model_copy(update={"approved_semantic_atom_mapping": False})
    )
    assert not failed.promoted
    assert failed.resulting_tier == QualityTier.PROVISIONAL
    assert failed.failure_reasons == ("semantic_atom_mapping_not_approved",)

    failed_audit = evaluate_positive_item_promotion(
        promotion.model_copy(
            update={
                "family_audit_input": promotion.family_audit_input.model_copy(
                    update={"blinded": False}
                )
            }
        )
    )
    assert not failed_audit.promoted
    assert "gate_4a_audit_not_passed" in failed_audit.failure_reasons
    assert "family_promotion_decision_not_reproducible" in failed_audit.failure_reasons

    stale = evaluate_positive_item_promotion(
        promotion.model_copy(update={"current_promotion_policy_hash": "c" * 64})
    )
    assert not stale.promoted
    assert "family_decision_policy_hash_is_stale" in stale.failure_reasons


def _negative_input(
    route: NegativeRouteEvidence,
    tier: QualityTier,
) -> NegativePromotionInput:
    return NegativePromotionInput(
        item_id="negative-item-1",
        family_id="n01_operator",
        rule_version="1.0.0",
        requested_tier=tier,
        route_evidence=(route,),
    )


@pytest.mark.parametrize(
    ("promotion", "expected_route", "expected_tier"),
    [
        (
            _negative_input(
                CounterexampleRouteEvidence(
                    evidence_id=_evidence_id("1"),
                    checked=True,
                    supported_fragment=True,
                    counterexample_outcome="found",
                ),
                QualityTier.GOLD_COUNTEREXAMPLE,
            ),
            "route_1",
            QualityTier.GOLD_COUNTEREXAMPLE,
        ),
        (
            _negative_input(
                DirectionalSeparatorRouteEvidence(
                    proof_evidence_id=_evidence_id("2"),
                    separator_evidence_id=_evidence_id("3"),
                    proved_direction="A_to_B",
                    proof_outcome="proved",
                    reverse_separator_outcome="found",
                    supported_fragment=True,
                    recorded_relation="A_stronger",
                ),
                QualityTier.GOLD_COUNTEREXAMPLE,
            ),
            "route_2",
            QualityTier.GOLD_COUNTEREXAMPLE,
        ),
        (
            _negative_input(
                ExpertAdjudicationRouteEvidence(
                    evidence_id=_evidence_id("4"),
                    adjudication_outcome="not_same_claim",
                ),
                QualityTier.GOLD_HUMAN,
            ),
            "route_3",
            QualityTier.GOLD_HUMAN,
        ),
        (
            _negative_input(
                ConsensusRouteEvidence(
                    consensus_evidence_id=_evidence_id("5"),
                    audit_id=AUDIT_ID,
                    consensus_outcome="not_same_claim",
                    independent=True,
                    audited_precision_passed=True,
                    judge_families=("judge-a", "judge-b"),
                ),
                QualityTier.SILVER_CONSENSUS,
            ),
            "route_4",
            QualityTier.SILVER_CONSENSUS,
        ),
    ],
)
def test_each_negative_route_promotes_only_to_its_policy_tier(
    promotion: NegativePromotionInput,
    expected_route: str,
    expected_tier: QualityTier,
) -> None:
    result = evaluate_negative_promotion(promotion)

    assert result.promoted
    assert result.qualifying_route == expected_route
    assert result.expected_tier == expected_tier
    assert result.resulting_tier == expected_tier
    assert result.failure_reasons == ()


def test_negative_requires_exactly_one_route() -> None:
    counterexample = CounterexampleRouteEvidence(
        evidence_id=_evidence_id("1"),
        checked=True,
        supported_fragment=True,
        counterexample_outcome="found",
    )
    expert = ExpertAdjudicationRouteEvidence(
        evidence_id=_evidence_id("4"),
        adjudication_outcome="not_same_claim",
    )
    zero = NegativePromotionInput(
        item_id="negative-zero",
        family_id="n01_operator",
        rule_version="1.0.0",
        requested_tier=QualityTier.GOLD_COUNTEREXAMPLE,
        route_evidence=(),
    )
    multiple = NegativePromotionInput(
        item_id="negative-multiple",
        family_id="n01_operator",
        rule_version="1.0.0",
        requested_tier=QualityTier.GOLD_COUNTEREXAMPLE,
        route_evidence=(counterexample, expert),
    )

    for promotion in (zero, multiple):
        result = evaluate_negative_promotion(promotion)
        assert not result.promoted
        assert result.resulting_tier == QualityTier.PROVISIONAL
        assert result.failure_reasons[0] == "exactly_one_negative_promotion_route_required"


@pytest.mark.parametrize(
    ("route", "reason"),
    [
        (
            CounterexampleRouteEvidence(
                evidence_id=_evidence_id("6"),
                checked=True,
                supported_fragment=True,
                counterexample_outcome="not_found",
            ),
            "counterexample_not_found_is_not_separator",
        ),
        (
            DirectionalSeparatorRouteEvidence(
                proof_evidence_id=_evidence_id("7"),
                separator_evidence_id=_evidence_id("3"),
                proved_direction="A_to_B",
                proof_outcome="not_proved",
                reverse_separator_outcome="found",
                supported_fragment=True,
                recorded_relation="A_stronger",
            ),
            "proof_not_proved_is_not_route_evidence",
        ),
        (
            DirectionalSeparatorRouteEvidence(
                proof_evidence_id=_evidence_id("2"),
                separator_evidence_id=_evidence_id("6"),
                proved_direction="A_to_B",
                proof_outcome="proved",
                reverse_separator_outcome="not_found",
                supported_fragment=True,
                recorded_relation="A_stronger",
            ),
            "counterexample_not_found_is_not_separator",
        ),
    ],
)
def test_search_failure_never_promotes(route: NegativeRouteEvidence, reason: str) -> None:
    result = evaluate_negative_promotion(_negative_input(route, QualityTier.GOLD_COUNTEREXAMPLE))

    assert not result.promoted
    assert result.resulting_tier == QualityTier.PROVISIONAL
    assert reason in result.failure_reasons


def test_negative_route_tier_and_direction_must_match() -> None:
    route = DirectionalSeparatorRouteEvidence(
        proof_evidence_id=_evidence_id("2"),
        separator_evidence_id=_evidence_id("3"),
        proved_direction="A_to_B",
        proof_outcome="proved",
        reverse_separator_outcome="found",
        supported_fragment=True,
        recorded_relation="B_stronger",
    )
    result = evaluate_negative_promotion(_negative_input(route, QualityTier.GOLD_HUMAN))

    assert not result.promoted
    assert "directional_relation_mismatches_proved_direction" in result.failure_reasons
    assert "requested_tier_does_not_match_promotion_route" in result.failure_reasons


def test_directional_route_requires_distinct_proof_and_separator_records() -> None:
    with pytest.raises(ValidationError, match="must be distinct"):
        DirectionalSeparatorRouteEvidence(
            proof_evidence_id=_evidence_id("2"),
            separator_evidence_id=_evidence_id("2"),
            proved_direction="A_to_B",
            proof_outcome="proved",
            reverse_separator_outcome="found",
            supported_fragment=True,
            recorded_relation="A_stronger",
        )


def test_consensus_requires_independence_audit_and_distinct_families() -> None:
    route = ConsensusRouteEvidence(
        consensus_evidence_id=_evidence_id("5"),
        audit_id=AUDIT_ID,
        consensus_outcome="not_same_claim",
        independent=False,
        audited_precision_passed=False,
        judge_families=("same-judge", "same-judge"),
    )
    result = evaluate_negative_promotion(_negative_input(route, QualityTier.SILVER_CONSENSUS))

    assert not result.promoted
    assert set(result.failure_reasons) == {
        "route_4_consensus_not_independent",
        "route_4_audited_precision_not_passed",
        "route_4_requires_two_or_more_distinct_judge_families",
    }


@pytest.mark.parametrize(
    "route",
    [
        ExpertAdjudicationRouteEvidence(
            evidence_id=_evidence_id("4"),
            adjudication_outcome="same_claim",
        ),
        ConsensusRouteEvidence(
            consensus_evidence_id=_evidence_id("5"),
            audit_id=AUDIT_ID,
            consensus_outcome="same_claim",
            independent=True,
            audited_precision_passed=True,
            judge_families=("judge-a", "judge-b"),
        ),
    ],
)
def test_judgment_routes_must_explicitly_resolve_not_same_claim(
    route: NegativeRouteEvidence,
) -> None:
    tier = (
        QualityTier.GOLD_HUMAN
        if isinstance(route, ExpertAdjudicationRouteEvidence)
        else QualityTier.SILVER_CONSENSUS
    )
    result = evaluate_negative_promotion(_negative_input(route, tier))

    assert not result.promoted
    assert any("requires_not_same_claim" in reason for reason in result.failure_reasons)


def test_vacuity_or_ex_falso_never_certifies_negative_route() -> None:
    result = evaluate_negative_promotion(
        _negative_input(
            DirectionalSeparatorRouteEvidence(
                proof_evidence_id=_evidence_id("2"),
                separator_evidence_id=_evidence_id("3"),
                proved_direction="A_to_B",
                proof_outcome="proved",
                reverse_separator_outcome="found",
                supported_fragment=True,
                recorded_relation="A_stronger",
                vacuity_or_ex_falso_detected=True,
            ),
            QualityTier.GOLD_COUNTEREXAMPLE,
        )
    )

    assert not result.promoted
    assert "vacuity_or_ex_falso_evidence_prohibited" in result.failure_reasons


def test_intention_cannot_be_encoded_as_negative_promotion_evidence() -> None:
    with pytest.raises(ValidationError):
        NegativePromotionInput.model_validate(
            {
                "item_id": "negative-intention",
                "family_id": "n01_operator",
                "rule_version": "1.0.0",
                "requested_tier": "gold_counterexample",
                "route_evidence": (
                    {
                        "route": "intended_relation",
                        "intended_relation": "near_miss",
                    },
                ),
            }
        )

    valid = _negative_input(
        ExpertAdjudicationRouteEvidence(
            evidence_id=_evidence_id("4"),
            adjudication_outcome="not_same_claim",
        ),
        QualityTier.GOLD_HUMAN,
    )
    with pytest.raises(ValidationError):
        NegativePromotionInput.model_validate(
            {
                **valid.model_dump(mode="json"),
                "intended_relation": "near_miss",
            }
        )


def test_negative_result_is_deterministic_frozen_and_verified() -> None:
    promotion = _negative_input(
        ExpertAdjudicationRouteEvidence(
            evidence_id=_evidence_id("4"),
            adjudication_outcome="not_same_claim",
        ),
        QualityTier.GOLD_HUMAN,
    )
    result = evaluate_negative_promotion(promotion)

    assert result == evaluate_negative_promotion(promotion)
    assert len(result.promotion_input_hash) == 64
    verify_negative_promotion_result(promotion, result)
    with pytest.raises(PromotionIntegrityError):
        verify_negative_promotion_result(
            promotion,
            result.model_copy(update={"promotion_input_hash": HASH_B}),
        )
    with pytest.raises(ValidationError):
        result.promoted = False


def test_clopper_pearson_is_monotone_in_successes() -> None:
    endpoints = [clopper_pearson_lower_95(successes, 200) for successes in range(190, 201)]

    assert all(math.isfinite(endpoint) for endpoint in endpoints)
    assert endpoints == sorted(endpoints)
