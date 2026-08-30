"""Strict, Lean-free policy for the Wave 1 N31 required-guard checker.

This module validates a closed checker *contract*.  It does not inspect Lean
expressions, instantiate a backend, execute a transform, or create a label.
The finite fact evaluator is deliberately fail-closed: every fact must have
already been established by the future hash-bound typed checker.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import ConfigError, LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]

DEFAULT_N31_GUARD_BANK_PATH = Path(
    "configs/transformations/sft1_value_first_v1/wave1_n31_guard_bank_v0_3_2.yaml"
)

# Filled from the checked-in bytes and validated effective model.  Changing any
# contract field requires a reviewed version bump and intentional hash update.
EXPECTED_N31_GUARD_BANK_FILE_SHA256 = (
    "c2a5aa63158ffbc561bc61f2e3acaa2598aff54a926fd774014e62e6c1cd8cd8"
)
EXPECTED_N31_GUARD_BANK_CONFIG_HASH = (
    "82bca9b16861412ebaf296591944338932e51f6aaaf8372baa4fd4c1f097f9e1"
)

EXPECTED_OPERATION_IDS = (
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
)


class GuardShape(StrEnum):
    NE_ZERO = "ne_zero_guard_v1"
    POSITIVE = "positive_guard_v1"
    NONNEGATIVE = "nonnegative_guard_v1"
    MEMBERSHIP = "membership_guard_v1"
    INDEX_LT = "index_lt_guard_v1"


class RetainedContradictionShape(StrEnum):
    EQ_ZERO = "eq_zero_retained_v1"
    NONPOSITIVE = "nonpositive_retained_v1"
    NEGATIVE = "negative_retained_v1"
    NOT_MEMBERSHIP = "not_membership_retained_v1"
    BOUND_LE_INDEX = "bound_le_index_retained_v1"


class CheckerOutcome(StrEnum):
    APPLICABLE = "applicable"
    TYPED_NOT_APPLICABLE = "typed_not_applicable"


class ReachabilityStatus(StrEnum):
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


class N31FailureReason(StrEnum):
    ARBITRARY_PROP = "arbitrary_prop_hypothesis"
    UNUSED_GUARD = "unused_guard"
    GUARD_TRUE = "guard_definitionally_true"
    TARGET_MISSING_OR_AMBIGUOUS = "target_site_missing_or_ambiguous"
    ROLE_MISMATCH = "protected_role_mismatch"
    TARGET_HEAD_UNBOUND = "target_head_not_in_frozen_bank"
    UNRELATED_TARGET = "target_occurrence_unrelated"
    COMPETING_GUARD = "competing_retained_guard"
    CONTRADICTORY_CONTEXT = "retained_context_contradiction"
    REACHABILITY_UNKNOWN = "domain_reachability_unknown"
    EMPTY_OR_UNREACHABLE = "empty_or_unreachable_domain"
    REINDEX_MISMATCH = "de_bruijn_reindex_mismatch"
    EXTRA_DELTA = "nonselected_delta_detected"
    ENDPOINTS_DEFEQ = "endpoints_definitionally_equal"
    NONREDUNDANCY_UNKNOWN = "nonredundancy_unknown"


EXPECTED_SHAPES = tuple(shape.value for shape in GuardShape)
EXPECTED_CONTRADICTION_SHAPES = tuple(shape.value for shape in RetainedContradictionShape)
EXPECTED_REQUIRED_STEPS = (
    "exact_guard_shape_recognition",
    "exact_guard_local_identity",
    "guard_not_definitionally_true",
    "selected_target_site_rediscovery",
    "protected_role_expr_hash_match",
    "selected_target_head_bank_membership",
    "guard_body_dependency",
    "frozen_implication_closure_scan",
    "frozen_contradiction_scan",
    "closed_domain_reachability_certificate",
    "exact_single_local_deletion",
    "exact_de_bruijn_reindex_reconstruction",
    "unchanged_nonselected_telescope_and_body",
    "non_definitionally_equal_closed_endpoints",
    "exact_delta_receipt_replay",
)
EXPECTED_FAILURE_REASONS = tuple(reason.value for reason in N31FailureReason)
EXPECTED_FIXTURE_REQUIREMENTS = (
    "live_conformance_one_success_and_one_rejection_per_operation_project",
    "regression_bank_covers_each_guard_shape_at_least_once_over_project_union",
    "same_guard_redundant_via_retained_duplicate_reject",
    "nonzero_redundant_via_retained_positive_reject",
    "nonnegative_redundant_via_retained_positive_reject",
    "retained_contradiction_per_shape_reject",
    "same_data_in_unrelated_target_location_reject",
    "target_head_not_in_bank_reject",
    "role_expr_mismatch_reject",
    "unknown_nonredundancy_reject",
    "missing_reachability_certificate_reject",
    "de_bruijn_reindex_off_by_one_reject",
    "second_delta_reject",
)
EXPECTED_READINESS_REQUIREMENTS = (
    "exact_target_head_bank_hash_bound",
    "checker_implementation_and_symbol_hash_bound",
    "checker_procedure_hash_bound",
    "success_and_rejection_fixture_bundles_hash_bound",
    "operation_specific_regressions_hash_bound",
    "source_eligibility_and_source_proof_routes_resolved",
    "shared_label_contract_merged_and_pinned",
    "clean_checkout_receipt_passed",
    "selected_wave_admission_record_valid",
)


class DecisionContract(StrictModel):
    admitted_outcome: Literal[CheckerOutcome.APPLICABLE]
    rejected_outcome: Literal[CheckerOutcome.TYPED_NOT_APPLICABLE]
    unknown_outcome: Literal[CheckerOutcome.TYPED_NOT_APPLICABLE]
    unknown_may_create_negative_label: Literal[False]
    unrestricted_theorem_search_allowed: Literal[False]
    failed_search_is_evidence: Literal[False]
    generic_d0_is_label_evidence: Literal[False]
    rubric_lane_makes_f2_claim: Literal[False]
    candidate_truth_axis: tuple[Literal["proved", "refuted", "unknown"], ...]
    exact_single_guard_delta_required: Literal[True]
    exact_de_bruijn_reindex_receipt_required: Literal[True]

    @model_validator(mode="after")
    def _exact_truth_axis(self) -> DecisionContract:
        if self.candidate_truth_axis != ("proved", "refuted", "unknown"):
            raise ValueError("candidate truth axis must remain proved/refuted/unknown")
        return self


class GuardShapeContract(StrictModel):
    shape_id: GuardShape
    protected_relation: NonEmptyStr
    exact_orientation: NonEmptyStr
    protected_data_roles: tuple[NonEmptyStr, ...]
    target_role: NonEmptyStr
    competing_guard_shapes: tuple[GuardShape, ...]
    contradictory_retained_shapes: tuple[RetainedContradictionShape, ...]


class ImplicationEdge(StrictModel):
    premise_shape: GuardShape
    conclusion_shape: GuardShape
    required_same_roles: tuple[NonEmptyStr, ...]
    required_same_type: Literal[True]
    required_same_instance: Literal[True]


class ContradictionContract(StrictModel):
    shape_id: RetainedContradictionShape
    contradicts_guard_shape: GuardShape
    required_same_roles: tuple[NonEmptyStr, ...]
    required_same_type: Literal[True]
    required_same_instance: Literal[True]


class ReachabilityContract(StrictModel):
    allowed_certificate_modes: tuple[
        Literal[
            "explicit_telescope_witness_and_retained_hypothesis_proofs",
            "frozen_shape_specific_decidable_certificate",
        ],
        ...,
    ]
    certificate_replay_required: Literal[True]
    absent_or_unknown_is_typed_not_applicable: Literal[True]
    source_proof_not_required_for_rubric_lane: Literal[True]

    @model_validator(mode="after")
    def _exact_modes(self) -> ReachabilityContract:
        expected = (
            "explicit_telescope_witness_and_retained_hypothesis_proofs",
            "frozen_shape_specific_decidable_certificate",
        )
        if self.allowed_certificate_modes != expected:
            raise ValueError("reachability certificate modes differ from the closed contract")
        return self


class TargetHeadBankBinding(StrictModel):
    status: Literal["unresolved"]
    required_per_shape: Literal[True]
    bank_hash: None
    checker_symbol: None
    checker_file_sha256: None
    resolution_rule: NonEmptyStr


class RubricLaneContract(StrictModel):
    operation_id: Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"]
    required_evidence: tuple[NonEmptyStr, ...]
    source_proof_required: Literal[False]
    candidate_refutation_required: Literal[False]
    makes_f2_claim: Literal[False]


class ProofLaneContract(StrictModel):
    operation_id: Literal["N31_DROP_REQUIRED_GUARD_PROOF_V1"]
    parent_operation_id: Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"]
    required_evidence: tuple[NonEmptyStr, ...]
    candidate_truth_required: Literal["refuted"]
    separate_cap_required: Literal[True]
    separately_reported_stratum: Literal[True]


class LaneContracts(StrictModel):
    n_rubric: RubricLaneContract
    n_proof: ProofLaneContract


class N31GuardBank(StrictModel):
    policy_version: Literal["0.3.2"]
    contract_id: Literal["sft1_n31_required_domain_guard_closed_checker_v1"]
    status: Literal["design_frozen_implementation_unresolved"]
    purpose: NonEmptyStr
    operation_ids: tuple[NonEmptyStr, ...]
    family_id: Literal["N31"]
    rubric_dimension: Literal["required_domain_guard"]
    implementation_resolved: Literal[False]
    execution_ready: Literal[False]
    production_eligible: Literal[False]
    row_emission_authorized: Literal[False]
    decision_contract: DecisionContract
    guard_shapes: tuple[GuardShapeContract, ...]
    frozen_implication_closure: tuple[ImplicationEdge, ...]
    retained_contradiction_shapes: tuple[ContradictionContract, ...]
    required_checker_steps: tuple[NonEmptyStr, ...]
    fail_closed_reasons: tuple[N31FailureReason, ...]
    reachability_contract: ReachabilityContract
    target_head_bank_binding: TargetHeadBankBinding
    lane_contracts: LaneContracts
    adversarial_fixture_requirements: tuple[NonEmptyStr, ...]
    readiness_requirements: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def _validate_exact_closed_contract(self) -> N31GuardBank:
        if self.operation_ids != EXPECTED_OPERATION_IDS:
            raise ValueError("N31 operation inventory or order changed")
        if tuple(shape.shape_id.value for shape in self.guard_shapes) != EXPECTED_SHAPES:
            raise ValueError("N31 guard shapes differ from the five-shape closed bank")
        if tuple(item.shape_id.value for item in self.retained_contradiction_shapes) != (
            EXPECTED_CONTRADICTION_SHAPES
        ):
            raise ValueError("N31 contradiction shapes differ from the closed bank")
        if self.required_checker_steps != EXPECTED_REQUIRED_STEPS:
            raise ValueError("N31 checker steps changed")
        if tuple(reason.value for reason in self.fail_closed_reasons) != EXPECTED_FAILURE_REASONS:
            raise ValueError("N31 failure taxonomy changed")
        if self.adversarial_fixture_requirements != EXPECTED_FIXTURE_REQUIREMENTS:
            raise ValueError("N31 fixture requirements changed")
        if self.readiness_requirements != EXPECTED_READINESS_REQUIREMENTS:
            raise ValueError("N31 readiness requirements changed")
        expected_edges = {
            (GuardShape.POSITIVE, GuardShape.NE_ZERO, ("data",)),
            (GuardShape.POSITIVE, GuardShape.NONNEGATIVE, ("data",)),
        }
        observed_edges = {
            (edge.premise_shape, edge.conclusion_shape, edge.required_same_roles)
            for edge in self.frozen_implication_closure
        }
        if observed_edges != expected_edges or len(self.frozen_implication_closure) != 2:
            raise ValueError("N31 implication closure changed")
        shape_by_id = {shape.shape_id: shape for shape in self.guard_shapes}
        if len(shape_by_id) != len(self.guard_shapes):
            raise ValueError("duplicate N31 guard shape")
        contradiction_by_id = {item.shape_id: item for item in self.retained_contradiction_shapes}
        if len(contradiction_by_id) != len(self.retained_contradiction_shapes):
            raise ValueError("duplicate N31 contradiction shape")
        if any(
            not item.required_same_type or not item.required_same_instance
            for item in self.retained_contradiction_shapes
        ):
            raise ValueError("N31 contradictions require exact type and instance identity")
        for shape in self.guard_shapes:
            if len(set(shape.protected_data_roles)) != len(shape.protected_data_roles):
                raise ValueError(f"duplicate protected role for {shape.shape_id}")
            if shape.shape_id not in shape.competing_guard_shapes:
                raise ValueError(f"reflexive competing-guard check missing for {shape.shape_id}")
            if not shape.contradictory_retained_shapes:
                raise ValueError(f"contradiction check missing for {shape.shape_id}")
        return self


class N31CheckerFacts(StrictModel):
    """Facts produced by the future typed checker, never inferred here."""

    recognized_guard_shape: GuardShape | None
    exact_guard_local_identity: StrictBool
    guard_not_definitionally_true: StrictBool
    target_site_unique: StrictBool
    protected_roles_match: StrictBool
    target_head_in_frozen_bank: StrictBool
    guard_body_dependency_present: StrictBool
    protected_target_relation_established: StrictBool
    competing_retained_guard_absent: StrictBool
    retained_contradiction_absent: StrictBool
    reachability_status: ReachabilityStatus
    nonredundancy_established: StrictBool
    exact_single_local_deletion: StrictBool
    exact_de_bruijn_reindex: StrictBool
    nonselected_structure_unchanged: StrictBool
    endpoints_nondefeq: StrictBool
    exact_delta_replay_passed: StrictBool


class N31CheckerDecision(StrictModel):
    outcome: CheckerOutcome
    failure_reason: N31FailureReason | None

    @model_validator(mode="after")
    def _coherent(self) -> N31CheckerDecision:
        if (self.outcome == CheckerOutcome.APPLICABLE) != (self.failure_reason is None):
            raise ValueError("applicable iff no N31 failure reason")
        return self


def decide_n31_checker_facts(facts: N31CheckerFacts) -> N31CheckerDecision:
    """Apply a deterministic fail-closed order to already-established typed facts."""

    checks: tuple[tuple[bool, N31FailureReason], ...] = (
        (facts.recognized_guard_shape is not None, N31FailureReason.ARBITRARY_PROP),
        (facts.exact_guard_local_identity, N31FailureReason.TARGET_MISSING_OR_AMBIGUOUS),
        (facts.guard_not_definitionally_true, N31FailureReason.GUARD_TRUE),
        (facts.target_site_unique, N31FailureReason.TARGET_MISSING_OR_AMBIGUOUS),
        (facts.protected_roles_match, N31FailureReason.ROLE_MISMATCH),
        (facts.target_head_in_frozen_bank, N31FailureReason.TARGET_HEAD_UNBOUND),
        (facts.guard_body_dependency_present, N31FailureReason.UNUSED_GUARD),
        (
            facts.protected_target_relation_established,
            N31FailureReason.UNRELATED_TARGET,
        ),
        (facts.competing_retained_guard_absent, N31FailureReason.COMPETING_GUARD),
        (facts.retained_contradiction_absent, N31FailureReason.CONTRADICTORY_CONTEXT),
        (
            facts.reachability_status is not ReachabilityStatus.UNKNOWN,
            N31FailureReason.REACHABILITY_UNKNOWN,
        ),
        (
            facts.reachability_status is ReachabilityStatus.REACHABLE,
            N31FailureReason.EMPTY_OR_UNREACHABLE,
        ),
        (facts.nonredundancy_established, N31FailureReason.NONREDUNDANCY_UNKNOWN),
        (facts.exact_single_local_deletion, N31FailureReason.EXTRA_DELTA),
        (facts.exact_de_bruijn_reindex, N31FailureReason.REINDEX_MISMATCH),
        (facts.nonselected_structure_unchanged, N31FailureReason.EXTRA_DELTA),
        (facts.endpoints_nondefeq, N31FailureReason.ENDPOINTS_DEFEQ),
        (facts.exact_delta_replay_passed, N31FailureReason.EXTRA_DELTA),
    )
    for passed, reason in checks:
        if not passed:
            return N31CheckerDecision(
                outcome=CheckerOutcome.TYPED_NOT_APPLICABLE,
                failure_reason=reason,
            )
    return N31CheckerDecision(outcome=CheckerOutcome.APPLICABLE, failure_reason=None)


def load_n31_guard_bank(
    repo_root: Path | None = None,
    path: Path | None = None,
) -> LoadedConfig[N31GuardBank]:
    """Load the exact checked-in N31 design contract and verify both hashes."""

    root = find_repo_root(repo_root)
    expected_path = (root / DEFAULT_N31_GUARD_BANK_PATH).resolve()
    config_path = (path or expected_path).resolve()
    if not config_path.is_relative_to(root.resolve()) or config_path != expected_path:
        raise ConfigError("N31 guard-bank path differs from the frozen repository path")
    observed_file_hash = hash_file(config_path)
    if observed_file_hash != EXPECTED_N31_GUARD_BANK_FILE_SHA256:
        raise ConfigError("N31 guard-bank file hash differs from the frozen revision")
    loaded = load_config(config_path, N31GuardBank)
    if loaded.config_hash != EXPECTED_N31_GUARD_BANK_CONFIG_HASH:
        raise ConfigError("N31 guard-bank effective hash differs from the frozen revision")
    return loaded


__all__ = [
    "DEFAULT_N31_GUARD_BANK_PATH",
    "EXPECTED_N31_GUARD_BANK_CONFIG_HASH",
    "EXPECTED_N31_GUARD_BANK_FILE_SHA256",
    "CheckerOutcome",
    "GuardShape",
    "N31CheckerDecision",
    "N31CheckerFacts",
    "N31FailureReason",
    "N31GuardBank",
    "ReachabilityStatus",
    "decide_n31_checker_facts",
    "load_n31_guard_bank",
]
