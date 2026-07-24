"""Fail-closed transformation promotion and audit recomputation.

This module implements the policy boundary in PLAN.md §15.4 and §15.7-15.9.
Generation intentions are deliberately absent from every promotion evidence
type: an ``IntendedRelation`` cannot be represented as a promotion route.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import QualityTier, RelationLabel, TransformationFamilyStatus
from leanfaith.schemas.ids import (
    AUDIT_PREFIX,
    EVIDENCE_PREFIX,
    HEX64_PATTERN,
    id_pattern,
    make_id,
)
from leanfaith.schemas.variant import (
    ECODE_PATTERN,
    FAMILY_ID_PATTERN,
    FamilyPromotionDecision,
)

NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
Sha256 = Annotated[str, Field(pattern=HEX64_PATTERN, strict=True)]
PositiveInt = Annotated[int, Field(ge=1, strict=True)]
AuditId = Annotated[str, Field(pattern=id_pattern(AUDIT_PREFIX), strict=True)]
EvidenceId = Annotated[str, Field(pattern=id_pattern(EVIDENCE_PREFIX), strict=True)]

POINT_PRECISION_MIN = 0.99
CLOPPER_PEARSON_LOWER_MIN = 0.95
POSITIVE_GOLD_AUDIT_N_MIN = 200
_TWO_SIDED_ALPHA = 0.05


class PromotionIntegrityError(ValueError):
    """A persisted result does not equal deterministic recomputation."""


class PositiveAuditOutcome(StrEnum):
    """Mutually exclusive blinded outcome for one selected audit item."""

    SAME_CLAIM = "same_claim"
    INCORRECT = "incorrect"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    POLICY_VIOLATION = "policy_violation"
    NOT_ELABORATED = "not_elaborated"


class AuditInvariantCheck(StrictModel):
    """One named, frozen family invariant evaluated on one audit item."""

    check_id: NonEmptyStr
    passed: StrictBool


class PositiveAuditItem(StrictModel):
    """One item selected by the frozen family audit design."""

    item_id: NonEmptyStr
    outcome: PositiveAuditOutcome
    invariant_checks: tuple[AuditInvariantCheck, ...]
    error_types: tuple[str, ...] = ()
    semantic_erasure_pattern: str | None = Field(default=None, min_length=1, strict=True)

    @model_validator(mode="after")
    def _validate_item(self) -> PositiveAuditItem:
        for error_type in self.error_types:
            if re.fullmatch(ECODE_PATTERN, error_type) is None:
                raise ValueError(f"unknown error code {error_type!r}; expected E01-E30")
        if len(set(self.error_types)) != len(self.error_types):
            raise ValueError("positive audit error_types must be unique")
        check_ids = tuple(check.check_id for check in self.invariant_checks)
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("positive audit invariant check_id values must be unique")
        has_erasure = "E25" in self.error_types
        if has_erasure != (self.semantic_erasure_pattern is not None):
            raise ValueError("semantic_erasure_pattern is required exactly when E25 is present")
        return self

    @property
    def invariants_hold(self) -> bool:
        """Whether every required invariant represented on the item passed."""

        return bool(self.invariant_checks) and all(check.passed for check in self.invariant_checks)

    @property
    def successfully_elaborated(self) -> bool:
        """Whether this item belongs to the policy precision denominator."""

        return self.outcome != PositiveAuditOutcome.NOT_ELABORATED

    @property
    def is_precision_success(self) -> bool:
        """Only a blinded ``same_claim`` judgment is a precision success."""

        return self.outcome == PositiveAuditOutcome.SAME_CLAIM


class HeldOutAuditCheck(StrictModel):
    """Frozen source/domain check contributing to the Gate 4A holdout condition."""

    check_id: NonEmptyStr
    source_or_domain: NonEmptyStr
    audited_count: PositiveInt
    manifest_hash: Sha256
    passed: StrictBool


class PositiveFamilyAuditInput(StrictModel):
    """Immutable evidence input from which Gate 4A is recomputed."""

    schema_version: Literal[1] = 1
    audit_id: AuditId
    family_id: str = Field(pattern=FAMILY_ID_PATTERN, strict=True)
    rule_version: NonEmptyStr
    polarity: Literal["positive"] = "positive"
    audit_manifest_hash: Sha256
    frozen_design_hash: Sha256
    blinded: StrictBool
    design_frozen_before_audit: StrictBool
    required_invariant_ids: tuple[NonEmptyStr, ...]
    items: tuple[PositiveAuditItem, ...]
    held_out_checks: tuple[HeldOutAuditCheck, ...]

    @model_validator(mode="after")
    def _unique_identifiers(self) -> PositiveFamilyAuditInput:
        item_ids = tuple(item.item_id for item in self.items)
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("positive audit item_id values must be unique")
        if not self.required_invariant_ids:
            raise ValueError("positive audit requires at least one family invariant")
        if len(set(self.required_invariant_ids)) != len(self.required_invariant_ids):
            raise ValueError("required_invariant_ids must be unique")
        required = set(self.required_invariant_ids)
        for item in self.items:
            observed = {check.check_id for check in item.invariant_checks}
            if observed != required:
                raise ValueError(
                    f"audit item {item.item_id!r} invariant checks do not exactly match "
                    "required_invariant_ids"
                )
        check_ids = tuple(check.check_id for check in self.held_out_checks)
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("held-out audit check_id values must be unique")
        return self


class PositivePromotionResult(StrictModel):
    """Deterministically recomputed Gate 4A family decision."""

    schema_version: Literal[1] = 1
    audit_id: AuditId
    family_id: str = Field(pattern=FAMILY_ID_PATTERN, strict=True)
    rule_version: NonEmptyStr
    audit_input_hash: Sha256
    selected_count: Annotated[int, Field(ge=0, strict=True)]
    denominator_n: Annotated[int, Field(ge=0, strict=True)]
    successes: Annotated[int, Field(ge=0, strict=True)]
    point_precision: Annotated[float, Field(ge=0.0, le=1.0, strict=True)]
    clopper_pearson_lower_95: Annotated[float, Field(ge=0.0, le=1.0, strict=True)]
    blinded: StrictBool
    design_frozen_before_audit: StrictBool
    all_invariants_hold: StrictBool
    held_out_source_domain_audit_passed: StrictBool
    recurrent_semantic_erasure_patterns: tuple[str, ...]
    gate_4a_passed: StrictBool
    promoted_family_status: TransformationFamilyStatus | None
    unlocked_item_tier: QualityTier | None
    failure_reasons: tuple[str, ...]

    @model_validator(mode="after")
    def _coherent(self) -> PositivePromotionResult:
        if self.successes > self.denominator_n:
            raise ValueError("positive promotion successes cannot exceed denominator_n")
        expected_status = TransformationFamilyStatus.GOLD_PROMOTED if self.gate_4a_passed else None
        expected_tier = QualityTier.GOLD_CONSERVATIVE_TRANSFORM if self.gate_4a_passed else None
        if self.promoted_family_status != expected_status:
            raise ValueError("promoted_family_status is inconsistent with gate_4a_passed")
        if self.unlocked_item_tier != expected_tier:
            raise ValueError("unlocked_item_tier is inconsistent with gate_4a_passed")
        if self.gate_4a_passed == bool(self.failure_reasons):
            raise ValueError("Gate 4A pass/failure reasons are inconsistent")
        return self


class PositiveItemPromotionInput(StrictModel):
    """The eight item-level conditions in PLAN.md §15.4."""

    schema_version: Literal[1] = 1
    item_id: NonEmptyStr
    family_id: str = Field(pattern=FAMILY_ID_PATTERN, strict=True)
    rule_version: NonEmptyStr
    family_status: TransformationFamilyStatus
    current_promotion_policy_hash: Sha256
    family_audit_input: PositiveFamilyAuditInput
    family_decision: FamilyPromotionDecision
    positive_allowlisted: StrictBool
    exact_local_structural_diff: StrictBool
    approved_semantic_atom_mapping: StrictBool
    dependencies_assumptions_literals_conclusion_heads_preserved: StrictBool
    inverse_roundtrip_or_rule_certificate: StrictBool
    no_proof_constants_or_admissions: StrictBool
    quarantine_violation: StrictBool


class PositiveItemPromotionResult(StrictModel):
    """Fail-closed item-tier admission result."""

    schema_version: Literal[1] = 1
    item_id: NonEmptyStr
    promoted: StrictBool
    resulting_tier: QualityTier
    failure_reasons: tuple[str, ...]

    @model_validator(mode="after")
    def _coherent(self) -> PositiveItemPromotionResult:
        expected = (
            QualityTier.GOLD_CONSERVATIVE_TRANSFORM if self.promoted else QualityTier.PROVISIONAL
        )
        if self.resulting_tier != expected:
            raise ValueError("positive item resulting_tier is inconsistent with promoted")
        if self.promoted == bool(self.failure_reasons):
            raise ValueError("positive item pass/failure reasons are inconsistent")
        return self


def _canonical_model_hash(model: StrictModel) -> str:
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _binomial_tail_at_least(successes: int, total: int, probability: float) -> float:
    """Return P[X >= successes] for X~Bin(total, probability), stably."""

    if successes <= 0:
        return 1.0
    if successes > total or probability <= 0.0:
        return 0.0
    if probability >= 1.0:
        return 1.0

    log_probability = math.log(probability)
    log_failure = math.log1p(-probability)
    log_terms = [
        math.lgamma(total + 1)
        - math.lgamma(count + 1)
        - math.lgamma(total - count + 1)
        + count * log_probability
        + (total - count) * log_failure
        for count in range(successes, total + 1)
    ]
    maximum = max(log_terms)
    result = math.exp(maximum) * math.fsum(math.exp(term - maximum) for term in log_terms)
    return min(1.0, result)


def clopper_pearson_lower_95(successes: int, total: int) -> float:
    """Exact-binomial lower endpoint of a two-sided 95% interval.

    The endpoint solves ``P_p[X >= successes] = 0.025``.  The interval is
    "exact" in the Clopper--Pearson sense; the root is evaluated
    deterministically with standard-library floating-point arithmetic.
    """

    if isinstance(successes, bool) or isinstance(total, bool):
        raise TypeError("successes and total must be integers, not bool")
    if not isinstance(successes, int) or not isinstance(total, int):
        raise TypeError("successes and total must be integers")
    if total < 0:
        raise ValueError("total must be nonnegative")
    if successes < 0 or successes > total:
        raise ValueError("successes must satisfy 0 <= successes <= total")
    if total == 0 or successes == 0:
        return 0.0

    target = _TWO_SIDED_ALPHA / 2.0
    low = 0.0
    high = 1.0
    # 64 iterations already exceed binary64 precision; the fixed count keeps
    # replay independent of platform-specific convergence tolerances.
    for _ in range(64):
        midpoint = (low + high) / 2.0
        if _binomial_tail_at_least(successes, total, midpoint) < target:
            low = midpoint
        else:
            high = midpoint
    # ``low`` is the conservative side of the bracket.  Returning it prevents
    # a one-ulp numerical overshoot from turning a boundary audit into a pass.
    return low


def recompute_positive_promotion(audit: PositiveFamilyAuditInput) -> PositivePromotionResult:
    """Recompute Gate 4A without trusting persisted summary statistics."""

    eligible = tuple(item for item in audit.items if item.successfully_elaborated)
    denominator = len(eligible)
    successes = sum(item.is_precision_success for item in eligible)
    point_precision = successes / denominator if denominator else 0.0
    lower = clopper_pearson_lower_95(successes, denominator)
    all_invariants = bool(audit.items) and all(item.invariants_hold for item in audit.items)
    held_out_passed = bool(audit.held_out_checks) and all(
        check.passed for check in audit.held_out_checks
    )
    erasure_patterns = tuple(
        item.semantic_erasure_pattern
        for item in audit.items
        if item.semantic_erasure_pattern is not None
    )
    recurrent_erasure = tuple(sorted(set(erasure_patterns))) if len(erasure_patterns) >= 2 else ()

    failures: list[str] = []
    if not audit.blinded:
        failures.append("audit_not_blinded")
    if not audit.design_frozen_before_audit:
        failures.append("design_not_frozen_before_audit")
    if denominator < POSITIVE_GOLD_AUDIT_N_MIN:
        failures.append("eligible_denominator_below_200")
    # Use integer arithmetic for the point threshold itself.
    if denominator == 0 or successes * 100 < 99 * denominator:
        failures.append("point_precision_below_0.99")
    if lower < CLOPPER_PEARSON_LOWER_MIN:
        failures.append("clopper_pearson_lower_below_0.95")
    if not all_invariants:
        failures.append("family_invariant_failure")
    if not held_out_passed:
        failures.append("held_out_source_domain_audit_failed")
    if recurrent_erasure:
        failures.append("recurrent_semantic_erasure_e25")

    passed = not failures
    return PositivePromotionResult(
        audit_id=audit.audit_id,
        family_id=audit.family_id,
        rule_version=audit.rule_version,
        audit_input_hash=_canonical_model_hash(audit),
        selected_count=len(audit.items),
        denominator_n=denominator,
        successes=successes,
        point_precision=point_precision,
        clopper_pearson_lower_95=lower,
        blinded=audit.blinded,
        design_frozen_before_audit=audit.design_frozen_before_audit,
        all_invariants_hold=all_invariants,
        held_out_source_domain_audit_passed=held_out_passed,
        recurrent_semantic_erasure_patterns=recurrent_erasure,
        gate_4a_passed=passed,
        promoted_family_status=(TransformationFamilyStatus.GOLD_PROMOTED if passed else None),
        unlocked_item_tier=(QualityTier.GOLD_CONSERVATIVE_TRANSFORM if passed else None),
        failure_reasons=tuple(failures),
    )


def verify_positive_promotion_result(
    audit: PositiveFamilyAuditInput, result: PositivePromotionResult
) -> None:
    """Reject any persisted Gate 4A result that differs from recomputation."""

    expected = recompute_positive_promotion(audit)
    if result != expected:
        raise PromotionIntegrityError("positive promotion result differs from recomputation")


def build_family_promotion_decision(
    audit: PositiveFamilyAuditInput,
    *,
    rule_id: str,
    policy_version: str,
    parent_registry_hash: str,
    promotion_policy_hash: str,
    non_gold_status: TransformationFamilyStatus = TransformationFamilyStatus.EXPERIMENTAL,
) -> FamilyPromotionDecision:
    """Build the canonical persistent decision from recomputed Gate 4A data.

    ``PositivePromotionResult`` is a transient numerical result.
    ``FamilyPromotionDecision`` remains the single persistent schema and binds
    the result to the registry, policy, manifest, and their hashes.
    """

    result = recompute_positive_promotion(audit)
    if not result.gate_4a_passed and non_gold_status == TransformationFamilyStatus.GOLD_PROMOTED:
        raise ValueError("a failed Gate 4A audit cannot receive gold_promoted status")
    decision = (
        TransformationFamilyStatus.GOLD_PROMOTED if result.gate_4a_passed else non_gold_status
    )
    result_hash = _canonical_model_hash(result)
    decision_id = make_id(
        "promotion",
        {
            "family_id": audit.family_id,
            "rule_id": rule_id,
            "rule_version": audit.rule_version,
            "policy_version": policy_version,
            "audit_id": audit.audit_id,
            "parent_registry_hash": parent_registry_hash,
            "promotion_policy_hash": promotion_policy_hash,
            "audit_manifest_hash": audit.audit_manifest_hash,
            "audit_input_hash": result.audit_input_hash,
            "audit_result_hash": result_hash,
            "decision": decision.value,
        },
    )
    return FamilyPromotionDecision(
        decision_id=decision_id,
        family_id=audit.family_id,
        rule_id=rule_id,
        rule_version=audit.rule_version,
        policy_version=policy_version,
        audit_id=audit.audit_id,
        parent_registry_hash=parent_registry_hash,
        promotion_policy_hash=promotion_policy_hash,
        audit_manifest_hash=audit.audit_manifest_hash,
        audit_input_hash=result.audit_input_hash,
        audit_result_hash=result_hash,
        selected_count=result.selected_count,
        denominator_n=result.denominator_n,
        successes=result.successes,
        point_precision=result.point_precision,
        clopper_pearson_lower_95=result.clopper_pearson_lower_95,
        blinded=result.blinded,
        design_frozen_before_audit=result.design_frozen_before_audit,
        all_invariants_hold=result.all_invariants_hold,
        held_out_source_domain_audit_passed=result.held_out_source_domain_audit_passed,
        recurrent_semantic_erasure_patterns=result.recurrent_semantic_erasure_patterns,
        decision=decision,
        unlocked_quality_tier=result.unlocked_item_tier,
        reason_codes=tuple(sorted(result.failure_reasons)),
    )


def verify_family_promotion_decision(
    audit: PositiveFamilyAuditInput,
    decision: FamilyPromotionDecision,
) -> None:
    """Verify every persistent decision binding by rebuilding it exactly."""

    non_gold_status = (
        decision.decision
        if decision.decision != TransformationFamilyStatus.GOLD_PROMOTED
        else TransformationFamilyStatus.EXPERIMENTAL
    )
    expected = build_family_promotion_decision(
        audit,
        rule_id=decision.rule_id,
        policy_version=decision.policy_version,
        parent_registry_hash=decision.parent_registry_hash,
        promotion_policy_hash=decision.promotion_policy_hash,
        non_gold_status=non_gold_status,
    )
    if decision != expected:
        raise PromotionIntegrityError(
            "family promotion decision differs from bound audit recomputation"
        )


def evaluate_positive_item_promotion(
    promotion: PositiveItemPromotionInput,
) -> PositiveItemPromotionResult:
    """Apply every §15.4 item condition after family-level promotion."""

    failures: list[str] = []
    family_audit = recompute_positive_promotion(promotion.family_audit_input)
    try:
        verify_family_promotion_decision(promotion.family_audit_input, promotion.family_decision)
    except (PromotionIntegrityError, ValueError):
        failures.append("family_promotion_decision_not_reproducible")
    if promotion.family_status != TransformationFamilyStatus.GOLD_PROMOTED:
        failures.append("family_not_gold_promoted")
    if promotion.family_decision.decision != TransformationFamilyStatus.GOLD_PROMOTED:
        failures.append("family_decision_not_gold_promoted")
    if not family_audit.gate_4a_passed:
        failures.append("gate_4a_audit_not_passed")
    if family_audit.family_id != promotion.family_id:
        failures.append("family_audit_family_mismatch")
    if family_audit.rule_version != promotion.rule_version:
        failures.append("family_audit_rule_version_mismatch")
    if promotion.family_decision.family_id != promotion.family_id:
        failures.append("family_decision_family_mismatch")
    if promotion.family_decision.rule_version != promotion.rule_version:
        failures.append("family_decision_rule_version_mismatch")
    if promotion.family_decision.promotion_policy_hash != promotion.current_promotion_policy_hash:
        failures.append("family_decision_policy_hash_is_stale")
    if not promotion.positive_allowlisted:
        failures.append("family_not_on_positive_allowlist")
    if not promotion.exact_local_structural_diff:
        failures.append("exact_local_structural_diff_missing")
    if not promotion.approved_semantic_atom_mapping:
        failures.append("semantic_atom_mapping_not_approved")
    if not promotion.dependencies_assumptions_literals_conclusion_heads_preserved:
        failures.append("semantic_structure_not_preserved")
    if not promotion.inverse_roundtrip_or_rule_certificate:
        failures.append("inverse_roundtrip_or_certificate_missing")
    if not promotion.no_proof_constants_or_admissions:
        failures.append("proof_constant_or_admission_detected")
    if promotion.quarantine_violation:
        failures.append("item_quarantined")
    promoted = not failures
    return PositiveItemPromotionResult(
        item_id=promotion.item_id,
        promoted=promoted,
        resulting_tier=(
            QualityTier.GOLD_CONSERVATIVE_TRANSFORM if promoted else QualityTier.PROVISIONAL
        ),
        failure_reasons=tuple(failures),
    )


CounterexampleOutcome = Literal["found", "not_found", "unsupported"]
ProofOutcome = Literal["proved", "not_proved"]


class CounterexampleRouteEvidence(StrictModel):
    """§15.7 route 1: checked separator in a supported fragment."""

    route: Literal["route_1"] = "route_1"
    evidence_id: EvidenceId
    checked: StrictBool
    supported_fragment: StrictBool
    counterexample_outcome: CounterexampleOutcome
    vacuity_or_ex_falso_detected: StrictBool = False


class DirectionalSeparatorRouteEvidence(StrictModel):
    """§15.7 route 2: one proof direction and a reverse separator."""

    route: Literal["route_2"] = "route_2"
    proof_evidence_id: EvidenceId
    separator_evidence_id: EvidenceId
    proved_direction: Literal["A_to_B", "B_to_A"]
    proof_outcome: ProofOutcome
    reverse_separator_outcome: CounterexampleOutcome
    supported_fragment: StrictBool
    recorded_relation: Literal["A_stronger", "B_stronger"]
    vacuity_or_ex_falso_detected: StrictBool = False

    @model_validator(mode="after")
    def _distinct_evidence(self) -> DirectionalSeparatorRouteEvidence:
        if self.proof_evidence_id == self.separator_evidence_id:
            raise ValueError("route_2 proof and separator evidence IDs must be distinct")
        return self


class ExpertAdjudicationRouteEvidence(StrictModel):
    """§15.7 route 3: an expert adjudication."""

    route: Literal["route_3"] = "route_3"
    evidence_id: EvidenceId
    adjudication_outcome: Literal["not_same_claim", "same_claim", "ambiguous", "unresolved"]


class ConsensusRouteEvidence(StrictModel):
    """§15.7 route 4: independent consensus plus an admitted audit."""

    route: Literal["route_4"] = "route_4"
    consensus_evidence_id: EvidenceId
    audit_id: AuditId
    consensus_outcome: Literal["not_same_claim", "same_claim", "ambiguous", "unresolved"]
    independent: StrictBool
    audited_precision_passed: StrictBool
    judge_families: tuple[NonEmptyStr, ...]


NegativeRouteEvidence = Annotated[
    CounterexampleRouteEvidence
    | DirectionalSeparatorRouteEvidence
    | ExpertAdjudicationRouteEvidence
    | ConsensusRouteEvidence,
    Field(discriminator="route"),
]


class NegativePromotionInput(StrictModel):
    """One provisional negative item requesting one supervised route.

    No field accepts an intended relation or intended error.  Because unknown
    fields are forbidden, generation intention cannot be smuggled into this
    promotion interface as evidence.
    """

    schema_version: Literal[1] = 1
    item_id: NonEmptyStr
    family_id: str = Field(pattern=FAMILY_ID_PATTERN, strict=True)
    rule_version: NonEmptyStr
    polarity: Literal["negative"] = "negative"
    requested_tier: QualityTier
    route_evidence: tuple[NegativeRouteEvidence, ...]


class NegativePromotionResult(StrictModel):
    """Deterministic Gate 4B item decision."""

    schema_version: Literal[1] = 1
    item_id: NonEmptyStr
    promotion_input_hash: Sha256
    route_count: Annotated[int, Field(ge=0, strict=True)]
    qualifying_route: Literal["route_1", "route_2", "route_3", "route_4"] | None
    requested_tier: QualityTier
    expected_tier: QualityTier | None
    promoted: StrictBool
    resulting_tier: QualityTier
    failure_reasons: tuple[str, ...]

    @model_validator(mode="after")
    def _coherent(self) -> NegativePromotionResult:
        if self.promoted:
            if self.expected_tier is None or self.resulting_tier != self.expected_tier:
                raise ValueError("promoted negative must receive the route's expected tier")
            if self.failure_reasons:
                raise ValueError("promoted negative cannot carry failure reasons")
        else:
            if self.resulting_tier != QualityTier.PROVISIONAL:
                raise ValueError("failed negative promotion must remain provisional")
            if not self.failure_reasons:
                raise ValueError("failed negative promotion requires failure reasons")
        return self


def _append_once(failures: list[str], reason: str) -> None:
    if reason not in failures:
        failures.append(reason)


def _expected_negative_tier(route: NegativeRouteEvidence, failures: list[str]) -> QualityTier:
    if isinstance(route, CounterexampleRouteEvidence):
        if route.counterexample_outcome == "not_found":
            _append_once(failures, "counterexample_not_found_is_not_separator")
        if not (
            route.checked and route.supported_fragment and route.counterexample_outcome == "found"
        ):
            _append_once(
                failures,
                "route_1_requires_checked_found_counterexample_in_supported_fragment",
            )
        if route.vacuity_or_ex_falso_detected:
            _append_once(failures, "vacuity_or_ex_falso_evidence_prohibited")
        return QualityTier.GOLD_COUNTEREXAMPLE

    if isinstance(route, DirectionalSeparatorRouteEvidence):
        if route.proof_outcome == "not_proved":
            _append_once(failures, "proof_not_proved_is_not_route_evidence")
        if route.reverse_separator_outcome == "not_found":
            _append_once(failures, "counterexample_not_found_is_not_separator")
        expected_relation = (
            RelationLabel.A_STRONGER
            if route.proved_direction == "A_to_B"
            else RelationLabel.B_STRONGER
        )
        if route.recorded_relation != expected_relation:
            _append_once(failures, "directional_relation_mismatches_proved_direction")
        if not (
            route.proof_outcome == "proved"
            and route.reverse_separator_outcome == "found"
            and route.supported_fragment
        ):
            _append_once(
                failures,
                "route_2_requires_proved_direction_and_found_reverse_separator",
            )
        if route.vacuity_or_ex_falso_detected:
            _append_once(failures, "vacuity_or_ex_falso_evidence_prohibited")
        return QualityTier.GOLD_COUNTEREXAMPLE

    if isinstance(route, ExpertAdjudicationRouteEvidence):
        if route.adjudication_outcome != "not_same_claim":
            _append_once(failures, "route_3_requires_not_same_claim_expert_adjudication")
        return QualityTier.GOLD_HUMAN

    if route.consensus_outcome != "not_same_claim":
        _append_once(failures, "route_4_requires_not_same_claim_consensus")
    if not route.independent:
        _append_once(failures, "route_4_consensus_not_independent")
    if not route.audited_precision_passed:
        _append_once(failures, "route_4_audited_precision_not_passed")
    if len(route.judge_families) < 2 or len(set(route.judge_families)) != len(route.judge_families):
        _append_once(failures, "route_4_requires_two_or_more_distinct_judge_families")
    return QualityTier.SILVER_CONSENSUS


def evaluate_negative_promotion(
    promotion: NegativePromotionInput,
) -> NegativePromotionResult:
    """Apply Gate 4B: exactly one qualifying route and its exact tier."""

    failures: list[str] = []
    expected_tier: QualityTier | None = None
    qualifying_route: Literal["route_1", "route_2", "route_3", "route_4"] | None = None

    if len(promotion.route_evidence) != 1:
        failures.append("exactly_one_negative_promotion_route_required")
        # Still expose prohibited search outcomes present in an invalid
        # multi-route submission so they cannot hide behind route-count failure.
        for evidence in promotion.route_evidence:
            if isinstance(evidence, CounterexampleRouteEvidence):
                if evidence.counterexample_outcome == "not_found":
                    _append_once(failures, "counterexample_not_found_is_not_separator")
            elif isinstance(evidence, DirectionalSeparatorRouteEvidence):
                if evidence.proof_outcome == "not_proved":
                    _append_once(failures, "proof_not_proved_is_not_route_evidence")
                if evidence.reverse_separator_outcome == "not_found":
                    _append_once(failures, "counterexample_not_found_is_not_separator")
    else:
        route = promotion.route_evidence[0]
        expected_tier = _expected_negative_tier(route, failures)
        qualifying_route = route.route
        if promotion.requested_tier != expected_tier:
            failures.append("requested_tier_does_not_match_promotion_route")

    promoted = not failures
    return NegativePromotionResult(
        item_id=promotion.item_id,
        promotion_input_hash=_canonical_model_hash(promotion),
        route_count=len(promotion.route_evidence),
        qualifying_route=qualifying_route,
        requested_tier=promotion.requested_tier,
        expected_tier=expected_tier,
        promoted=promoted,
        resulting_tier=expected_tier if promoted and expected_tier else QualityTier.PROVISIONAL,
        failure_reasons=tuple(failures),
    )


def verify_negative_promotion_result(
    promotion: NegativePromotionInput, result: NegativePromotionResult
) -> None:
    """Reject any persisted Gate 4B result that differs from recomputation."""

    expected = evaluate_negative_promotion(promotion)
    if result != expected:
        raise PromotionIntegrityError("negative promotion result differs from recomputation")
