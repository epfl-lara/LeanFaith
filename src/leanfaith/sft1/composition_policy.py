"""Strict, design-only loader for the SFT1 revision 0.3 composition policy.

This module is intentionally a policy boundary, not a transformation runtime.  It
imports no Lean backend, registers no operation, executes no candidate, and emits
no row.  It validates the exact proposal inventory, rejects consumption of the
superseded REPR predecessor, and binds the separately frozen starter-bank file.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
OperationId = Annotated[str, Field(pattern=r"^[PN][0-9]{2}_[A-Z0-9_]+_V[0-9]+$", strict=True)]
FamilyId = Annotated[
    str,
    Field(pattern=r"^(P[0-9]{2}|N[0-9]{2}|N-RUBRIC|N-PROOF|OTHER)$", strict=True),
]
SymbolicId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]*$", strict=True)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0, strict=True)]
PositiveFraction = Annotated[float, Field(gt=0.0, le=1.0, strict=True)]
PositiveSeconds = Annotated[float, Field(gt=0.0, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]

_DEFAULT_POLICY_PATH = Path(
    "configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml"
)

EXPECTED_POLICY_CONFIG_HASH = "59600c31d6502aac8381272944e1692362ab481d64217ff2b5e70fb2ad971242"
EXPECTED_OPERATION_REGISTRY_HASH = (
    "7740d128469b422529ca6ef429d286842ac251186c509cd26a5e42d5b1b572dd"
)
EXPECTED_STARTER_BANK_FILE_SHA256 = (
    "70b605dcdcd8a1d061cb1c4706c1fdd4867e327db00ccd738419727764736189"
)
EXPECTED_STARTER_BANK_CONFIG_HASH = (
    "ea533da7cf2b475a71dfa30735205f8d99b9a309ac68de4e696c54d67cfd8732"
)
EXPECTED_ROOT_CENSUS_HASH = "91da550b5590b2770b854fa01b10437b2d5a6d640b8dfc25a6f5bef6a1cb966d"
EXPECTED_SOURCE_ELIGIBILITY_HASH = (
    "cf0a8c4bb62446004c1225077fa6e8c77d1d2850f20b32a14570f2b5cd8d9aa2"
)

# Revision 0.3 freezes the inventory in code as well as YAML.  Keep this one
# tuple as the single edit point when the proposal adds an exact operation.
EXPECTED_OPERATION_IDS: tuple[str, ...] = (
    "P01_ALPHA_RENAME_SINGLE_V1",
    "P02_REGROUP_BINDERS_V1",
    "P11_BOUNDED_FORALL_EXPAND_V1",
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P16_REASSOC_AND_LEFT_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P20_FOLD_SET_NONEMPTY_V1",
    "P20_UNFOLD_SET_NONEMPTY_V1",
    "P21_BETA_INTRO_V1",
    "P21_BETA_REDUCE_V1",
    "P21_ZETA_INTRO_V1",
    "P21_ZETA_REDUCE_V1",
    "P22_ETA_REDUCE_EXPLICIT_FUN_V1",
    "P23_CURRY_PROP_PAIR_V1",
    "P24_SWAP_INDEPENDENT_PROP_BINDERS_V1",
    "P28_DECOMPOSE_IFF_V1",
    "P32_ADD_ASSOC_LOCAL_V1",
    "P32_ADD_COMM_LOCAL_V1",
    "P33_EQ_HYP_SUBSTITUTE_NONDEPENDENT_V1",
    "P34_NAT_SUCC_ADD_ONE_LOCAL_V1",
    "P35_SET_INTER_MEMBERSHIP_V1",
    "P36_SET_EXTENTIONALITY_V1",
    "P38_EXISTS_SUBTYPE_NONEMPTY_V1",
    "P39_HYP_SET_INTER_REWRITE_V1",
    "P40_EXISTS_UNIQUE_EXPAND_V1",
    "P41_SUBTYPE_FORALL_GUARD_V1",
    "P42_RING_POLYNOMIAL_LOCAL_V1",
    "N19_NEGATE_CLOSED_CLAIM_RUBRIC_V1",
    "N19_NEGATE_CLOSED_CLAIM_PROOF_V1",
    "N25_TOGGLE_EQ_NE_RUBRIC_V1",
    "N25_TOGGLE_EQ_NE_PROOF_V1",
    "N26_INCREMENT_BOUND_RUBRIC_V1",
    "N26_INCREMENT_BOUND_PROOF_V1",
    "N29_SWAP_WITNESS_DEPENDENCY_RUBRIC_V1",
    "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
    "N30_ADD_UNJUSTIFIED_UNIQUENESS_RUBRIC_V1",
    "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    "N32_SWAP_ROLE_ORDER_RUBRIC_V1",
    "N32_SWAP_ROLE_ORDER_PROOF_V1",
)

EXPECTED_SYNTHETIC_OPERATION_IDS: tuple[str, ...] = (
    "N28_FINITE_ARITHMETIC_RUBRIC_V1",
    "N28_FINITE_ARITHMETIC_PROOF_V1",
    "N28_FINITE_SET_RUBRIC_V1",
    "N28_FINITE_SET_PROOF_V1",
)

EXPECTED_CLAIM_ERASURE_GUARDS: tuple[str, ...] = (
    "reject_complete_claim_lemma_reuse",
    "reject_reflexive_or_true_false_collapse",
    "reject_hypothesis_to_true_deletion",
    "reject_root_relation_reflective_normalization",
)

EXPECTED_FAMILY_DISPOSITIONS: tuple[tuple[str, str], ...] = (
    ("P01", "operation_registry_only"),
    ("P02", "diagnostic_only"),
    ("P11", "diagnostic_only"),
    ("P20", "starter_bank_bound"),
    ("P21", "split_reductions_and_diagnostic_introductions"),
    ("P32", "starter_bank_bound"),
    ("P34", "starter_bank_bound"),
    ("P35", "starter_bank_bound"),
    ("P39", "proof_of_concept"),
    ("P41", "proof_of_concept"),
    ("P42", "proof_of_concept"),
    ("N21", "redesign_only"),
    ("N22", "redesign_only"),
    ("N28", "synthetic_separate_track"),
    ("N29", "proof_of_concept"),
    ("N30", "proof_of_concept"),
    ("N31", "prioritized_proof_of_concept"),
    ("N32", "proof_of_concept"),
    ("OTHER", "not_authorized_without_exact_operation"),
)

EXPECTED_BANK_IDS: tuple[str, ...] = (
    "p20_definitions_v1",
    "p32_ac_lemmas_v1",
    "p34_semantic_rewrites_v1",
    "p35_membership_v1",
    "p39_hypothesis_rewrites_v1",
    "p41_subtype_quantifiers_v1",
    "p42_reflective_procedures_v1",
    "negative_rubric_models_v1",
    "negative_proof_templates_v1",
    "n28_synthetic_templates_v1",
)

EXPECTED_COMPOSITION_PRODUCTIONS: tuple[str, ...] = (
    "positive_row := P",
    "positive_row := P P",
    "positive_row := P P P",
    "negative_row := N",
    "negative_row := P N",
    "negative_row := P P N",
)

EXPECTED_CACHE_KEY_FIELDS: tuple[str, ...] = (
    "source_closed_expr_hash",
    "candidate_closed_expr_hash",
    "canonical_universe_profile_id",
    "canonical_universe_profile_hash",
    "source_expr_builder_version",
    "candidate_expr_builder_version",
    "lean_version",
    "project_id",
    "project_revision",
    "toolchain_revision",
    "imports_hash",
    "options_hash",
    "synthesized_instance_hashes",
    "operation_id",
    "operation_registry_entry_hash",
    "schema_lemma_procedure_hash",
    "evidence_certificate_payload_hash",
    "bank_resolved_lean_hash",
    "transparency",
    "allowed_axiom_profile",
    "typed_meta_validator_version",
    "evidence_replay_version",
    "evaluation_blocklist_sha256",
    "repr_replacement_commit",
    "render_context_id",
    "render_context_hash",
    "renderer_api_hash",
    "repr_spec_hash",
    "policy_config_hash",
)

EXPECTED_CAP_ORDER: tuple[str, ...] = (
    "source_eligibility",
    "operation_applicability",
    "root_level_blocklist",
    "prevalidation_candidate_sampling",
    "typed_meta_validation_and_evidence_replay",
    "post_transform_blocklist",
    "stable_row_hash_total_order",
    "canonical_unordered_pair_duplicate_and_conflict_classification",
    "same_label_duplicate_keep_minimum_stable_row_hash",
    "conflicting_label_canonical_pair_class_rejection",
    "per_root_cap",
    "operation_cap",
    "bank_entry_or_template_cap",
    "lemma_or_procedure_cap",
    "family_cap",
    "mechanism_superclass_cap",
    "source_cap",
    "source_polarity_joint_balance",
    "deterministic_training_orientation_swap",
    "post_orientation_global_model_facing_duplicate_conflict_assertion",
)

EXPECTED_SOURCE_IDS: tuple[str, ...] = (
    "compiler_data",
    "cslib",
    "mathlib",
    "physlib",
)

EXPECTED_REAL_GOAL_CASE_IDS: tuple[str, ...] = (
    "mathlib_add_pow",
    "physlib_kinetic_energy_conserved",
    "cslib_ret_merge",
    "lean_compiler_int_lt",
    "canonical_gold_aime_1983_p1",
    "consistency_check_amc12a_2019_p21",
)

EXPECTED_RENDERER_API_HASH_PAYLOAD_FIELDS: tuple[str, ...] = (
    "replacement_commit",
    "replacement_lean_renderer_path",
    "replacement_lean_renderer_sha256",
    "required_namespace",
    "required_signature",
)

EXPECTED_PREVALIDATION_HASH_FIELDS: tuple[str, ...] = (
    "source_closed_expr_hash",
    "operation_id",
    "selected_site_lineage_hash",
    "candidate_closed_expr_hash",
)

EXPECTED_STABLE_ROW_HASH_FIELDS: tuple[str, ...] = (
    "root_ancestry_id",
    "source_identity_hash",
    "reference_closed_expr_hash",
    "candidate_closed_expr_hash",
    "operation_chain_hash",
    "selected_site_lineage_hash",
    "label",
    "evidence_certificate_payload_hash",
    "renderer_api_hash",
    "repr_spec_hash",
    "canonical_universe_profile_hash",
    "render_context_hash",
)

EXPECTED_MUTUAL_EXCLUSION_OPERATION_IDS: tuple[str, ...] = (
    "P20_FOLD_SET_NONEMPTY_V1",
    "P20_UNFOLD_SET_NONEMPTY_V1",
    "P21_BETA_INTRO_V1",
    "P21_BETA_REDUCE_V1",
    "P21_ZETA_INTRO_V1",
    "P21_ZETA_REDUCE_V1",
    "P22_ETA_REDUCE_EXPLICIT_FUN_V1",
)

EXPECTED_PRE_GATE_FAILURE_DIMENSIONS: tuple[str, ...] = (
    "source",
    "family",
    "operation",
    "polarity",
    "exact_failure_class",
)

EXPECTED_PRE_GATE_FAILURE_CLASSES: tuple[str, ...] = (
    "reference_render_failure",
    "candidate_render_failure",
    "expr_mvar",
    "universe_mvar",
    "free_variable",
    "loose_bound_variable",
    "anonymous_binder_name",
    "ill_typed",
    "non_prop",
    "wrong_turnstile_count",
    "required_distinct_render_collapsed",
    "universe_profile_mismatch",
    "renderer_context_mismatch",
    "repr_real_goal_coverage_not_passed",
)

EXPECTED_PRE_GATE_SIDECAR_BINDINGS: tuple[str, ...] = (
    "reference_closed_expr_hash",
    "candidate_closed_expr_hash",
    "reference_render_hash",
    "candidate_render_hash",
    "repr_replacement_commit",
    "renderer_api_hash",
    "repr_spec_hash",
    "canonical_universe_profile_id",
    "canonical_universe_profile_hash",
    "render_context_id",
    "render_context_hash",
)

EXPECTED_NEGATIVE_ADMISSIONS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    (
        "n19_negation_mistakes_natural_v1",
        "N19",
        "negation_mistakes",
        "natural",
        ("N19_NEGATE_CLOSED_CLAIM_RUBRIC_V1", "N19_NEGATE_CLOSED_CLAIM_PROOF_V1"),
    ),
    (
        "n25_negation_mistakes_natural_v1",
        "N25",
        "negation_mistakes",
        "natural",
        ("N25_TOGGLE_EQ_NE_RUBRIC_V1", "N25_TOGGLE_EQ_NE_PROOF_V1"),
    ),
    (
        "n26_edge_cases_natural_v1",
        "N26",
        "edge_cases",
        "natural",
        ("N26_INCREMENT_BOUND_RUBRIC_V1", "N26_INCREMENT_BOUND_PROOF_V1"),
    ),
    (
        "n29_witness_dependency_natural_v1",
        "N29",
        "witness_dependency",
        "natural",
        (
            "N29_SWAP_WITNESS_DEPENDENCY_RUBRIC_V1",
            "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
        ),
    ),
    (
        "n30_existence_uniqueness_natural_v1",
        "N30",
        "existence_uniqueness",
        "natural",
        (
            "N30_ADD_UNJUSTIFIED_UNIQUENESS_RUBRIC_V1",
            "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
        ),
    ),
    (
        "n31_required_domain_guard_natural_v1",
        "N31",
        "required_domain_guard",
        "natural",
        ("N31_DROP_REQUIRED_GUARD_RUBRIC_V1", "N31_DROP_REQUIRED_GUARD_PROOF_V1"),
    ),
    (
        "n32_converse_mistakes_natural_v1",
        "N32",
        "converse_mistakes",
        "natural",
        ("N32_SWAP_ROLE_ORDER_RUBRIC_V1", "N32_SWAP_ROLE_ORDER_PROOF_V1"),
    ),
    (
        "n28_edge_cases_synthetic_v1",
        "N28",
        "edge_cases",
        "synthetic_separate",
        ("N28_FINITE_ARITHMETIC_RUBRIC_V1", "N28_FINITE_ARITHMETIC_PROOF_V1"),
    ),
    (
        "n28_negation_mistakes_synthetic_v1",
        "N28",
        "negation_mistakes",
        "synthetic_separate",
        ("N28_FINITE_SET_RUBRIC_V1", "N28_FINITE_SET_PROOF_V1"),
    ),
)

EXPECTED_P23_REGRESSIONS: tuple[str, ...] = (
    "p23_pack_no_collision_uses_h",
    "p23_pack_existing_h_h_1_uses_h_2",
    "p23_pack_nested_shadowed_survivors_unchanged",
    "p23_pack_no_anonymous_or_inaccessible_display",
    "p23_pack_cross_worker_replay_same_names_and_render_hash",
    "p23_proof_dependent_continuation_reject",
)

EXPECTED_P23_SIDECAR_BINDINGS: tuple[str, ...] = (
    "binder_naming_policy_id",
    "binder_naming_policy_hash",
    "introduced_binder_names",
    "reserved_user_names_hash",
    "reserved_rendered_names_hash",
    "surviving_binder_lineage_before_hash",
    "surviving_binder_lineage_after_hash",
    "candidate_render_hash",
    "replay_render_hash",
)


class SFT1PolicyError(ValueError):
    """The SFT1 proposal, dependency binding, or starter bank is inconsistent."""


class EvidenceClass(StrEnum):
    P_DEF = "P-DEF"
    P_SCHEMA = "P-SCHEMA"
    P_LEMMA = "P-LEMMA"
    P_REFLECT = "P-REFLECT"
    N_RUBRIC = "N-RUBRIC"
    N_PROOF = "N-PROOF"


class LabelLane(StrEnum):
    POSITIVE = "positive"
    N_RUBRIC = "N-RUBRIC"
    N_PROOF = "N-PROOF"


class CandidateTruth(StrEnum):
    PROVED = "proved"
    REFUTED = "refuted"
    UNKNOWN = "unknown"


class OperationTrack(StrEnum):
    NATURAL = "natural"
    DIAGNOSTIC = "diagnostic"
    SYNTHETIC_SEPARATE = "synthetic_separate"


class OperationStatus(StrEnum):
    IMPLEMENTATION_CANDIDATE = "implementation_candidate"
    DIAGNOSTIC = "diagnostic"
    PROOF_OF_CONCEPT = "proof_of_concept"
    REDESIGN_ONLY = "redesign_only"


class AnchorKind(StrEnum):
    DEFINITION = "definition"
    LEMMA = "lemma"
    PROCEDURE = "procedure"
    RUBRIC = "rubric"
    SCHEMA = "schema"


class AnchorHashBasis(StrEnum):
    UTF8_REF = "sha256_utf8_anchor_ref_v1"
    BANK_ANCHOR = "bank_anchor_spec_sha256"


class Authorization(StrictModel):
    policy_loader_and_invariant_tests: Literal[True]
    transform_implementation: Literal[False]
    lean_execution: Literal[False]
    one_example_gate: Literal[False]
    hundred_root_gate: Literal[False]
    ten_k_pilot: Literal[False]
    row_generation: Literal[False]
    bulk_scale: Literal[False]
    publication: Literal[False]
    row_count_commitment: Literal[False]


class ReviewedReprPredecessor(StrictModel):
    status: Literal["reviewed_but_superseded"]
    consumable_by_sft1: Literal[False]
    hashes_are_execution_dependencies: Literal[False]
    freeze_ordinal: Literal[3]
    coherent_commit: GitCommit
    spec_hash: Sha256
    config_path: NonEmptyStr
    config_file_sha256: Sha256
    lean_renderer_path: NonEmptyStr
    lean_renderer_sha256: Sha256
    python_renderer_path: NonEmptyStr
    python_renderer_sha256: Sha256


class ExprRendererApiDependency(StrictModel):
    status: Literal["coordinator_request_open"]
    required_signature: Literal["renderClosedProp (e : Expr) : MetaM String"]
    required_namespace: Literal["LeanFaith.GoalV1"]
    replacement_must_be_new_coherent_freeze: Literal[True]
    replacement_commit: None
    replacement_spec_hash: None
    replacement_config_path: None
    replacement_config_file_sha256: None
    replacement_lean_renderer_path: None
    replacement_lean_renderer_sha256: None
    replacement_python_renderer_path: None
    replacement_python_renderer_sha256: None
    renderer_api_hash: None
    renderer_api_hash_basis: Literal["sha256_canonical_renderer_api_binding_v1"]
    renderer_api_hash_payload_fields: tuple[NonEmptyStr, ...]
    populated_renderer_api_hash_must_replay_from_payload: Literal[True]
    canonical_universe_profile_id: None
    canonical_universe_profile_hash: None
    canonical_universe_profile_must_define_level_instantiation_and_naming: Literal[True]
    render_context_id: None
    render_context_hash: None
    real_goal_coverage_regression_id: None
    real_goal_coverage_regression_hash: None
    real_goal_coverage_regression_passed: Literal[False]
    real_goal_coverage_uses_closed_expr_api: Literal[True]
    all_required_real_goals_must_render_successfully: Literal[True]
    required_real_goal_case_ids: tuple[NonEmptyStr, ...]
    unresolved_expr_mvars_allowed: Literal[False]
    unresolved_universe_mvars_allowed: Literal[False]
    free_variables_allowed: Literal[False]
    loose_bound_variables_allowed: Literal[False]
    anonymous_telescope_binder_names_allowed: Literal[False]
    anonymous_binder_rejection_scope: Literal["rendered_outer_pi_telescope"]
    type_inference_must_succeed_before_render: Literal[True]
    api_rejects_anonymous_telescope_binder: Literal[True]
    api_rejects_ill_typed_expr: Literal[True]
    non_prop_allowed: Literal[False]

    @model_validator(mode="after")
    def _exact_real_goal_cases(self) -> ExprRendererApiDependency:
        if self.required_real_goal_case_ids != EXPECTED_REAL_GOAL_CASE_IDS:
            raise ValueError("REPR replacement must cover the exact six real-goal cases")
        if self.renderer_api_hash_payload_fields != EXPECTED_RENDERER_API_HASH_PAYLOAD_FIELDS:
            raise ValueError("renderer API hash payload fields differ from revision 0.3")
        return self


class SharedContractDependency(StrictModel):
    status: Literal["coordinator_request_open"]
    required_rule: Literal[
        "exact_evidence_plus_operation_admission_creates_sft1_labels_not_polarity_multiplication"
    ]
    merged_commit: None


class Dependencies(StrictModel):
    repr_reviewed_predecessor: ReviewedReprPredecessor
    expr_renderer_api: ExprRendererApiDependency
    shared_contract_update: SharedContractDependency


class RepresentationContract(StrictModel):
    reference_input: Literal["canonical_closed_expr"]
    candidate_input: Literal["canonical_closed_expr"]
    same_renderer_for_both_sides: Literal[True]
    direct_renderer_call_for_both_sides: Literal[True]
    same_persistent_meta_request: Literal[True]
    renderer_signature: Literal["renderClosedProp (e : Expr) : MetaM String"]
    candidate_theorem_declaration_allowed: Literal[False]
    candidate_axiom_declaration_allowed: Literal[False]
    sorry_for_rendering_allowed: Literal[False]
    synthesize_candidate_proof_for_rendering: Literal[False]
    copy_renderer_or_options_into_sft1: Literal[False]
    surface_render_candidate: Literal[False]
    pretty_print_then_reelaborate_candidate: Literal[False]
    compile_goal_v1_text: Literal[False]
    reelaborate_goal_v1_text: Literal[False]
    require_goal_v1_text_to_compile: Literal[False]
    render_complete_pi_telescope: Literal[True]
    canonical_universe_profile_required: Literal[True]
    reference_and_candidate_share_universe_profile: Literal[True]
    universe_profile_source: Literal["repr_replacement_freeze"]
    local_u_i_canonicalization_without_repr_profile_allowed: Literal[False]


class NRubricContract(StrictModel):
    exact_typed_mutation_required: Literal[True]
    protected_rubric_dimension_required: Literal[True]
    operation_specific_applicability_required: Literal[True]
    anti_degeneracy_checks_required: Literal[True]
    exact_delta_evidence_required: Literal[True]
    claims_f2_truth: Literal[False]


class NProofContract(StrictModel):
    subtype_of_admitted_n_rubric_operation: Literal[True]
    exact_source_proof_required: Literal[True]
    exact_candidate_refutation_required: Literal[True]
    inherits_parent_typed_applicability: Literal[True]
    inherits_parent_exact_delta_evidence: Literal[True]
    inherits_parent_anti_degeneracy_checks: Literal[True]
    aggregate_retained_share_maximum: PositiveFraction

    @model_validator(mode="after")
    def _exact_aggregate_cap(self) -> NProofContract:
        if self.aggregate_retained_share_maximum != 0.10:
            raise ValueError("N-PROOF aggregate retained share must be capped at 0.10")
        return self


class LabelContract(StrictModel):
    positive_evidence_classes: tuple[EvidenceClass, ...]
    negative_label_lanes: tuple[EvidenceClass, ...]
    generic_d0_is_label_evidence: Literal[False]
    family_polarity_multiplication_allowed: Literal[False]
    exact_evidence_required: Literal[True]
    n_rubric_family_dimension_user_admission_required: Literal[True]
    operation_level_user_admission_required: Literal[True]
    n_rubric: NRubricContract
    n_proof: NProofContract
    candidate_truth_evidence_values: tuple[CandidateTruth, ...]
    candidate_truth_evidence_required_in_sidecar: Literal[True]
    operation_candidate_truth_default_may_fill_missing_sidecar: Literal[False]
    candidate_truth_evidence_determines_label: Literal[False]
    lane_selected_before_proof_validation: Literal[True]
    proof_success_or_failure_may_select_label_lane: Literal[False]
    candidate_provability_may_create_label: Literal[False]

    @model_validator(mode="after")
    def _exact_lanes(self) -> LabelContract:
        if self.positive_evidence_classes != (
            EvidenceClass.P_DEF,
            EvidenceClass.P_SCHEMA,
            EvidenceClass.P_LEMMA,
            EvidenceClass.P_REFLECT,
        ):
            raise ValueError("positive_evidence_classes must be the exact admitted positive lanes")
        if self.negative_label_lanes != (EvidenceClass.N_RUBRIC, EvidenceClass.N_PROOF):
            raise ValueError("negative_label_lanes must be exactly N-RUBRIC and N-PROOF")
        if self.candidate_truth_evidence_values != (
            CandidateTruth.PROVED,
            CandidateTruth.REFUTED,
            CandidateTruth.UNKNOWN,
        ):
            raise ValueError("candidate truth evidence must be proved/refuted/unknown")
        return self


class NegativeFamilyDimensionAdmission(StrictModel):
    admission_id: SymbolicId
    family_id: FamilyId
    rubric_dimension: SymbolicId
    track: OperationTrack
    operation_ids: tuple[OperationId, ...]
    status: Literal["pending_user_decision"]
    approved: Literal[False]

    @model_validator(mode="after")
    def _exact_pair_shape(self) -> NegativeFamilyDimensionAdmission:
        if len(self.operation_ids) != 2 or len(set(self.operation_ids)) != 2:
            raise ValueError("each negative admission must bind one exact rubric/proof pair")
        if any(
            not operation_id.startswith(f"{self.family_id}_") for operation_id in self.operation_ids
        ):
            raise ValueError("negative admission operations must belong to their exact family")
        if not self.operation_ids[0].endswith("_RUBRIC_V1"):
            raise ValueError("negative admission must list its N-RUBRIC operation first")
        if not self.operation_ids[1].endswith("_PROOF_V1"):
            raise ValueError("negative admission must list its N-PROOF operation second")
        return self


class ClaimErasureGuards(StrictModel):
    reject_complete_claim_lemma_reuse: Literal[True]
    reject_reflexive_or_true_false_collapse: Literal[True]
    reject_hypothesis_to_true_deletion: Literal[True]
    reject_root_relation_reflective_normalization: Literal[True]


class ClaimErasureGuardContract(ClaimErasureGuards):
    required_for_evidence_classes: tuple[EvidenceClass, ...]

    @model_validator(mode="after")
    def _exact_evidence_classes(self) -> ClaimErasureGuardContract:
        if self.required_for_evidence_classes != (
            EvidenceClass.P_LEMMA,
            EvidenceClass.P_REFLECT,
        ):
            raise ValueError("claim-erasure guards must cover exactly P-LEMMA and P-REFLECT")
        return self


class AxiomProfile(StrictModel):
    profile_id: SymbolicId
    allowed_axioms: tuple[NonEmptyStr, ...]
    unlisted_axioms_allowed: Literal[False]

    @model_validator(mode="after")
    def _unique_axioms(self) -> AxiomProfile:
        if len(set(self.allowed_axioms)) != len(self.allowed_axioms):
            raise ValueError("allowed_axioms must be unique")
        return self


class StarterBankBinding(StrictModel):
    path: Literal["configs/transformations/sft1_value_first_v1/starter_banks_v0_3_0.yaml"]
    file_sha256: Literal["70b605dcdcd8a1d061cb1c4706c1fdd4867e327db00ccd738419727764736189"]
    bank_set_id: Literal["sft1_starter_banks_v0_3_0"]
    bank_set_version: Literal["0.3.0"]
    frozen_before_transform_implementation: Literal[True]
    resolved_lean_hashes_required_before_operation_execution: Literal[True]


class InlineAnchorResolution(StrictModel):
    status: Literal["pending"]
    design_reference_hashes_are_executable_hashes: Literal[False]
    resolved_schema_lemma_procedure_hashes_required_before_one_example_gate: Literal[True]
    resolved_manifest_hash: None


class AdversarialFixtureFreeze(StrictModel):
    status: Literal["pending"]
    operation_fixture_ids_are_design_ids_only: Literal[True]
    per_operation_and_eligible_project_specs_required: Literal[True]
    success_and_expected_rejection_code_required: Literal[True]
    fixture_bundle_hash_required_before_one_example_gate: Literal[True]
    fixture_bundle_hash: None


class P23BinderHygieneContract(StrictModel):
    status: Literal["design_frozen_implementation_pending"]
    existing_shared_engine_path: Literal["LeanFaith/Meta/TransformEngine.lean"]
    existing_shared_engine_is_consumable: Literal[False]
    user_reported_failure_render: Literal["[anonymous] : True✝"]
    future_task_owned_path: Literal["LeanFaith/Meta/SFT1/TransformEngine.lean"]
    binder_naming_policy_id: Literal["p23_neutral_proof_binder_names_v1"]
    binder_naming_policy_hash_basis: Literal["sha256_canonical_p23_binder_hygiene_contract_v1"]
    binder_naming_policy_hash: Sha256
    introduced_binder_count: Literal[1]
    generated_name_constructor: Literal["Name.mkSimple"]
    anonymous_name_constructor_allowed: Literal[False]
    name_sequence_grammar: Literal[
        "h_then_h_underscore_smallest_positive_ascii_decimal_without_leading_zero"
    ]
    collision_domains: tuple[NonEmptyStr, ...]
    reserved_names_scope: Literal["complete_original_ordered_telescope"]
    naming_inputs: tuple[NonEmptyStr, ...]
    forbidden_naming_inputs: tuple[NonEmptyStr, ...]
    stable_under_resume_for_same_closed_expr_and_site: Literal[True]
    introduced_name_is_nonanonymous: Literal[True]
    introduced_name_renders_exactly_as_allocated: Literal[True]
    surviving_binder_names_and_lineage_unchanged: Literal[True]
    forbidden_render_substring: Literal["[anonymous]"]
    inspect_expr_binder_names_before_render: Literal[True]
    required_regressions: tuple[NonEmptyStr, ...]
    sidecar_bindings: tuple[NonEmptyStr, ...]
    regressions_passed: Literal[False]

    @model_validator(mode="after")
    def _exact_policy(self) -> P23BinderHygieneContract:
        if self.collision_domains != (
            "lean_name_equality",
            "repr_displayed_or_sanitized_local_name_equality",
        ):
            raise ValueError("P23 collision domains differ from the frozen pack-only policy")
        if self.naming_inputs != (
            "canonical_closed_expr_hash",
            "selected_site_lineage_hash",
        ):
            raise ValueError("P23 naming inputs differ from the frozen pack-only policy")
        if self.forbidden_naming_inputs != (
            "family",
            "operation_id",
            "label",
            "root_id",
            "row_id",
            "seed",
            "randomness",
        ):
            raise ValueError("P23 forbidden naming inputs differ from the frozen policy")
        if self.required_regressions != EXPECTED_P23_REGRESSIONS:
            raise ValueError("P23 requires the exact six pack-only regressions")
        if self.sidecar_bindings != EXPECTED_P23_SIDECAR_BINDINGS:
            raise ValueError("P23 sidecar bindings differ from the frozen policy")
        payload = self.model_dump(mode="json")
        payload.pop("binder_naming_policy_hash")
        payload.pop("regressions_passed")
        if self.binder_naming_policy_hash != hash_canonical(payload):
            raise ValueError("P23 binder-naming policy hash does not replay")
        return self


class RepresentationPreGateAcceptance(StrictModel):
    status: Literal["blocked_on_repr_replacement"]
    required_before_one_example_gate: Literal[True]
    reference_render_succeeds_through_shared_api: Literal[True]
    candidate_render_succeeds_through_shared_api: Literal[True]
    required_distinct_render_is_distinct: Literal[True]
    exact_turnstile_count: Literal[1]
    expr_mvars_allowed: Literal[False]
    universe_mvars_allowed: Literal[False]
    free_variables_allowed: Literal[False]
    loose_bound_variables_allowed: Literal[False]
    anonymous_binder_names_allowed: Literal[False]
    anonymous_binder_rejection_scope: Literal["rendered_outer_pi_telescope"]
    type_inference_must_succeed_before_render: Literal[True]
    repr_real_goal_coverage_regression_must_pass: Literal[True]
    failure_reporting_dimensions: tuple[NonEmptyStr, ...]
    exact_failure_classes: tuple[NonEmptyStr, ...]
    stable_id_and_sidecar_bindings: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def _exact_reporting_contract(self) -> RepresentationPreGateAcceptance:
        if self.failure_reporting_dimensions != EXPECTED_PRE_GATE_FAILURE_DIMENSIONS:
            raise ValueError("pre-gate failure dimensions differ from revision 0.3")
        if self.exact_failure_classes != EXPECTED_PRE_GATE_FAILURE_CLASSES:
            raise ValueError("pre-gate failure classes differ from revision 0.3")
        if self.stable_id_and_sidecar_bindings != EXPECTED_PRE_GATE_SIDECAR_BINDINGS:
            raise ValueError("pre-gate sidecar bindings differ from revision 0.3")
        return self


class FamilyDisposition(StrictModel):
    family_id: FamilyId
    disposition: NonEmptyStr


class OperationAnchor(StrictModel):
    kind: AnchorKind
    ref: NonEmptyStr
    hash_basis: AnchorHashBasis
    schema_lemma_procedure_hash: Sha256
    bank_id: SymbolicId | None
    bank_entry_id: SymbolicId | None

    @model_validator(mode="after")
    def _coherent_anchor(self) -> OperationAnchor:
        if self.hash_basis == AnchorHashBasis.UTF8_REF:
            if self.bank_id is not None or self.bank_entry_id is not None:
                raise ValueError("inline operation anchors cannot name a starter bank")
            expected = hashlib.sha256(self.ref.encode("utf-8")).hexdigest()
            if self.schema_lemma_procedure_hash != expected:
                raise ValueError("inline operation anchor hash does not match its UTF-8 reference")
        else:
            if self.bank_id is None or self.bank_entry_id is None:
                raise ValueError("bank-bound operation anchors require bank_id and bank_entry_id")
        return self


class OperationCap(StrictModel):
    maximum_retained_share: PositiveFraction
    maximum_per_root: PositiveInt


class OperationBudget(StrictModel):
    heartbeat_seconds: PositiveSeconds
    soft_seconds: PositiveSeconds
    hard_seconds: PositiveSeconds

    @model_validator(mode="after")
    def _ordered_budget(self) -> OperationBudget:
        if not self.heartbeat_seconds <= self.soft_seconds <= self.hard_seconds:
            raise ValueError("operation budgets must satisfy heartbeat <= soft <= hard")
        return self


class AdversarialFixtures(StrictModel):
    success: SymbolicId
    reject: SymbolicId

    @model_validator(mode="after")
    def _distinct_fixtures(self) -> AdversarialFixtures:
        if self.success == self.reject:
            raise ValueError("success and rejection fixtures must be distinct")
        return self


class OperationAdmission(StrictModel):
    status: Literal["pending_user_decision"]
    approved: Literal[False]


class OperationSpec(StrictModel):
    operation_id: OperationId
    family_id: FamilyId
    track: OperationTrack
    status: OperationStatus
    priority: NonNegativeInt
    label_lane: LabelLane
    evidence_class: EvidenceClass
    mechanism_superclass: SymbolicId
    anchor: OperationAnchor
    orientation: NonEmptyStr
    typed_applicability: NonEmptyStr
    context_restrictions: NonEmptyStr
    transparency: NonEmptyStr
    logic_regime: NonEmptyStr
    allowed_axiom_profile: SymbolicId
    inverse_token: NonEmptyStr
    cap: OperationCap
    budget: OperationBudget
    eligible_projects: tuple[Literal["compiler_data", "cslib", "mathlib", "physlib"], ...]
    adversarial_fixtures: AdversarialFixtures
    claim_erasure_guards: (
        tuple[
            Literal[
                "reject_complete_claim_lemma_reuse",
                "reject_reflexive_or_true_false_collapse",
                "reject_hypothesis_to_true_deletion",
                "reject_root_relation_reflective_normalization",
            ],
            ...,
        ]
        | None
    )
    rubric_dimension: NonEmptyStr | None
    anti_degeneracy_checks: tuple[NonEmptyStr, ...]
    n_proof_subtype_of: OperationId | None
    candidate_truth_default: CandidateTruth
    exact_delta_evidence_required: bool = Field(strict=True)
    admission: OperationAdmission
    executable: Literal[False]
    label_emission_authorized: Literal[False]

    @model_validator(mode="after")
    def _operation_contract(self) -> OperationSpec:
        if not self.operation_id.startswith(f"{self.family_id}_"):
            raise ValueError("operation_id must begin with its exact family_id")
        if self.eligible_projects != tuple(sorted(set(self.eligible_projects))):
            raise ValueError("eligible_projects must be sorted and unique")
        if len(set(self.anti_degeneracy_checks)) != len(self.anti_degeneracy_checks):
            raise ValueError("anti_degeneracy_checks must be unique")

        positive = self.evidence_class in {
            EvidenceClass.P_DEF,
            EvidenceClass.P_SCHEMA,
            EvidenceClass.P_LEMMA,
            EvidenceClass.P_REFLECT,
        }
        if positive:
            if self.label_lane != LabelLane.POSITIVE:
                raise ValueError("positive evidence must use the positive label lane")
            if self.rubric_dimension is not None or self.n_proof_subtype_of is not None:
                raise ValueError("positive operations cannot name negative evidence fields")
            if self.exact_delta_evidence_required:
                raise ValueError("positive operations cannot claim negative exact-delta evidence")
            if self.candidate_truth_default != CandidateTruth.UNKNOWN:
                raise ValueError("positive operations must default candidate truth to unknown")
        elif self.evidence_class == EvidenceClass.N_RUBRIC:
            if self.label_lane != LabelLane.N_RUBRIC:
                raise ValueError("N-RUBRIC evidence must use the n_rubric lane")
            if self.rubric_dimension is None or not self.anti_degeneracy_checks:
                raise ValueError("N-RUBRIC requires a rubric dimension and anti-degeneracy checks")
            if self.n_proof_subtype_of is not None:
                raise ValueError("N-RUBRIC cannot be a subtype of another operation")
            if not self.exact_delta_evidence_required:
                raise ValueError("N-RUBRIC requires exact-delta evidence")
            if self.candidate_truth_default != CandidateTruth.UNKNOWN:
                raise ValueError("N-RUBRIC must not infer candidate truth from its label lane")
        else:
            if self.label_lane != LabelLane.N_PROOF:
                raise ValueError("N-PROOF evidence must use the n_proof lane")
            if self.rubric_dimension is None or not self.anti_degeneracy_checks:
                raise ValueError("N-PROOF retains rubric dimension and anti-degeneracy evidence")
            if self.n_proof_subtype_of is None:
                raise ValueError("N-PROOF must name its admitted N-RUBRIC super-operation")
            if not self.exact_delta_evidence_required:
                raise ValueError("N-PROOF requires exact-delta evidence")
            if self.candidate_truth_default != CandidateTruth.REFUTED:
                raise ValueError("N-PROOF must record candidate truth as refuted")

        if self.evidence_class in {EvidenceClass.P_LEMMA, EvidenceClass.P_REFLECT}:
            if self.claim_erasure_guards != EXPECTED_CLAIM_ERASURE_GUARDS:
                raise ValueError(
                    "P-LEMMA/P-REFLECT operations require all exact claim-erasure guards"
                )
        elif self.claim_erasure_guards is not None:
            raise ValueError("claim-erasure guards are operation fields only for P-LEMMA/P-REFLECT")

        if self.track == OperationTrack.DIAGNOSTIC and self.status != OperationStatus.DIAGNOSTIC:
            raise ValueError("diagnostic-track operations must have diagnostic status")
        return self


class SyntheticTrack(StrictModel):
    track_id: Literal["sft1_n28_synthetic_v0_3_0"]
    status: Literal["separate_unapproved"]
    mixed_with_natural_roots: Literal[False]
    ten_k_pilot_authorized: Literal[False]
    row_generation_authorized: Literal[False]
    operations: tuple[OperationSpec, ...]

    @model_validator(mode="after")
    def _separate_inventory(self) -> SyntheticTrack:
        operation_ids = tuple(operation.operation_id for operation in self.operations)
        if operation_ids != EXPECTED_SYNTHETIC_OPERATION_IDS:
            raise ValueError("synthetic track differs from the exact separate N28 inventory")
        if any(
            operation.family_id != "N28" or operation.track != OperationTrack.SYNTHETIC_SEPARATE
            for operation in self.operations
        ):
            raise ValueError("synthetic operations must be N28 synthetic-separate operations")
        return self


class P01CycleException(StrictModel):
    operation_id: Literal["P01_ALPHA_RENAME_SINGLE_V1"]
    maximum_uses_per_chain: Literal[1]
    may_repeat_alpha_fingerprint_once: Literal[True]
    exception_applies_only_to_the_single_p01_hop: Literal[True]
    may_repeat_expr_or_render_hash: Literal[False]


class MutualExclusionGroup(StrictModel):
    group_id: Literal["one_definitional_mechanism_per_chain"]
    maximum_members_per_chain: Literal[1]
    operation_ids: tuple[OperationId, ...]

    @model_validator(mode="after")
    def _exact_members(self) -> MutualExclusionGroup:
        if self.operation_ids != EXPECTED_MUTUAL_EXCLUSION_OPERATION_IDS:
            raise ValueError("definitional mutual-exclusion members differ from revision 0.3")
        return self


class CompositionGrammar(StrictModel):
    maximum_total_operations: Literal[3]
    admitted_statuses_after_operation_approval: tuple[
        Literal["implementation_candidate", "proof_of_concept"], ...
    ]
    diagnostic_operations_may_emit_rows: Literal[False]
    positive_terminal: Literal["admitted_positive_operation"]
    negative_terminal: Literal["admitted_n_rubric_or_n_proof_operation"]
    productions: tuple[NonEmptyStr, ...]
    post_negative_operations_allowed: Literal[False]
    positive_row_negative_operation_count: Literal[0]
    negative_row_negative_operation_count: Literal[1]
    all_sites_pairwise_disjoint_after_typed_rediscovery: Literal[True]
    one_operation_per_mechanism_superclass: Literal[True]
    repeated_inverse_tokens_rejected: Literal[True]
    repeated_text_hashes_rejected: Literal[True]
    repeated_closed_expr_hashes_rejected: Literal[True]
    repeated_render_hashes_rejected: Literal[True]
    repeated_selected_site_lineage_rejected: Literal[True]
    direct_only_operation_ids: tuple[OperationId, ...]
    mutual_exclusion_groups: tuple[MutualExclusionGroup, ...]
    p01_alpha_fingerprint_repeat_exception: P01CycleException
    all_other_repeated_path_fingerprints_rejected: Literal[True]
    current_typed_site_rediscovery_each_hop: Literal[True]

    @model_validator(mode="after")
    def _exact_grammar(self) -> CompositionGrammar:
        if self.productions != EXPECTED_COMPOSITION_PRODUCTIONS:
            raise ValueError("composition productions differ from the exact negative-last grammar")
        if self.admitted_statuses_after_operation_approval != (
            "implementation_candidate",
            "proof_of_concept",
        ):
            raise ValueError("composition admits only implementation-candidate and POC operations")
        if self.direct_only_operation_ids:
            raise ValueError("revision 0.3 has no direct-only operation IDs")
        if len(self.mutual_exclusion_groups) != 1:
            raise ValueError("revision 0.3 requires the exact definitional exclusion group")
        return self


class RootCensus(StrictModel):
    census_id: Literal["sft1_zero_lean_root_census_v1"]
    status: Literal["pending"]
    executes_lean: Literal[False]
    required_before_any_row_commitment: Literal[True]
    required_before_one_example_gate: Literal[True]
    row_commitment_authorized: Literal[False]
    required_metrics: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def _unique_metrics(self) -> RootCensus:
        if len(self.required_metrics) != len(set(self.required_metrics)):
            raise ValueError("root census metrics must be unique")
        return self


class SourceEligibility(StrictModel):
    source_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    source_kind: Literal["extracted_signature", "imported_constant"]
    census_route: NonEmptyStr
    closed_expr_route: NonEmptyStr
    status: Literal["pending_census"]
    declaration_insertion_allowed: Literal[False]

    @model_validator(mode="after")
    def _closed_expr_route_matches_source(self) -> SourceEligibility:
        if self.source_id == "compiler_data":
            if self.source_kind != "extracted_signature":
                raise ValueError("compiler_data must use the extracted-signature route")
            if "term_elab_of_signature" not in self.closed_expr_route:
                raise ValueError("compiler_data must elaborate a signature directly to closed Expr")
        else:
            if self.source_kind != "imported_constant":
                raise ValueError("library sources must use imported constants")
            if "constant_info_type" not in self.closed_expr_route:
                raise ValueError("imported constants must close ConstantInfo types")
        return self


class ExecutionContract(StrictModel):
    no_per_row_process_spawn: Literal[True]
    retained_row_typed_meta_validation: Literal[True]
    retained_row_evidence_replay: Literal[True]
    retained_certificate_replay_fraction: Fraction
    persistent_meta_workers_required: Literal[True]
    in_process_hashing_required: Literal[True]
    sample_candidates_before_typed_validation: Literal[True]
    prevalidation_candidate_sampling_uses_randomness: Literal[False]
    prevalidation_candidate_sampling_rule: Literal[
        "stable_hash_total_order_then_operation_budget_prefix"
    ]
    prevalidation_candidate_sampling_hash_fields: tuple[NonEmptyStr, ...]
    per_operation_heartbeat_and_time_budget_required: Literal[True]
    per_root_durable_journal_required: Literal[True]
    measure_lean_seconds_per_retained_pair: Literal[True]
    measure_sidecar_bytes_per_retained_pair: Literal[True]
    retry_only_infrastructure_failures: Literal[True]
    machine_wide_worker_limit: Literal[2]
    machine_wide_lean_rss_gib_limit: Literal[40]

    @model_validator(mode="after")
    def _complete_replay(self) -> ExecutionContract:
        if self.retained_certificate_replay_fraction != 1.0:
            raise ValueError("every retained certificate must be replayed")
        if self.prevalidation_candidate_sampling_hash_fields != EXPECTED_PREVALIDATION_HASH_FIELDS:
            raise ValueError("prevalidation sampling hash fields differ from revision 0.3")
        return self


class CacheContract(StrictModel):
    exact_ordered_key_fields: tuple[NonEmptyStr, ...]
    cache_successes: Literal[True]
    cache_deterministic_terminal_failures: Literal[True]
    hash_inside_persistent_process: Literal[True]
    operation_registry_entry_hash_basis: Literal["sha256_canonical_operation_spec_v1"]

    @model_validator(mode="after")
    def _exact_cache_key(self) -> CacheContract:
        if self.exact_ordered_key_fields != EXPECTED_CACHE_KEY_FIELDS:
            raise ValueError("cache key fields differ from the exact ordered SFT1 cache key")
        return self


class SamplingAndQuality(StrictModel):
    deterministic_training_orientation_swap_fraction: Fraction
    orientation_swap_scope: Literal["training_only"]
    orientation_swap_rule: Literal["stable_hash_sort_then_swap_exactly_half_of_even_training_shard"]
    even_training_shards_required: Literal[True]
    stable_row_hash_basis: Literal["sha256_canonical_sft1_row_selection_identity_v1"]
    stable_row_hash_fields: tuple[NonEmptyStr, ...]
    canonical_unordered_pair_hash_basis: Literal[
        "sha256_sorted_reference_candidate_render_hashes_v1"
    ]
    same_label_duplicate_survivor_rule: Literal["minimum_stable_row_hash"]
    conflicting_label_class_action: Literal["reject_entire_canonical_unordered_pair_class"]
    preserve_root_ancestry_clusters: Literal[True]
    preserve_near_duplicate_clusters: Literal[True]
    evaluation_blocklist_path: Literal["data/benchmarks/golden_blocklist_v1.json"]
    evaluation_blocklist_sha256: Literal[
        "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
    ]
    both_blocklist_screens_use_exact_binding: Literal[True]
    root_level_evaluation_blocklist_screen: Literal[True]
    post_transform_evaluation_blocklist_screen: Literal[True]
    global_model_facing_duplicate_rejection: Literal[True]
    global_conflicting_label_rejection: Literal[True]
    duplicate_conflict_screen_before_caps_uses_canonical_unordered_pairs: Literal[True]
    duplicate_conflict_screen_before_caps_checks_both_orientations: Literal[True]
    orientation_swap_after_cap_selection: Literal[True]
    post_orientation_global_model_facing_duplicate_and_conflict_assertion: Literal[True]
    post_orientation_assertion_failure_action: Literal["fail_shard_without_commit_or_refill"]
    source_polarity_joint_stratification: Literal[True]
    negative_share_range_when_measured_yield_allows: tuple[Fraction, Fraction]
    force_rows_to_fill_balance: Literal[False]
    candidate_only_balanced_accuracy_strictly_below: Fraction
    reference_only_balanced_accuracy_strictly_below: Fraction
    paired_family_heldout_balanced_accuracy_strictly_below: Fraction
    paired_mechanism_heldout_balanced_accuracy_strictly_below: Fraction
    paired_template_heldout_balanced_accuracy_strictly_below: Fraction
    confidence_bounds_required: Literal[True]
    confidence_level: Fraction
    confidence_interval_method: Literal["stratified_cluster_bootstrap"]
    confidence_interval_upper_bound_must_be_strictly_below_threshold: Literal[True]

    @model_validator(mode="after")
    def _measured_balance_band(self) -> SamplingAndQuality:
        if self.deterministic_training_orientation_swap_fraction != 0.5:
            raise ValueError("training orientation swap fraction must be exactly 0.50")
        if self.negative_share_range_when_measured_yield_allows != (0.4, 0.6):
            raise ValueError("negative measured-yield balance band must be exactly [0.40, 0.60]")
        canary_thresholds = (
            self.candidate_only_balanced_accuracy_strictly_below,
            self.reference_only_balanced_accuracy_strictly_below,
            self.paired_family_heldout_balanced_accuracy_strictly_below,
            self.paired_mechanism_heldout_balanced_accuracy_strictly_below,
            self.paired_template_heldout_balanced_accuracy_strictly_below,
        )
        if canary_thresholds != (0.6, 0.6, 0.65, 0.65, 0.65):
            raise ValueError("balanced-accuracy thresholds differ from revision 0.3")
        if self.confidence_level != 0.95:
            raise ValueError("balanced-accuracy confidence level must be exactly 0.95")
        if self.stable_row_hash_fields != EXPECTED_STABLE_ROW_HASH_FIELDS:
            raise ValueError("stable-row hash fields differ from revision 0.3")
        return self


class CapContract(StrictModel):
    natural_and_synthetic_denominators_separate: Literal[True]
    synthetic_rows_count_toward_natural_caps: Literal[False]
    compiler_data_root_share_maximum: Fraction
    any_single_source_share_maximum: Fraction
    family_share_maximum: Fraction
    mechanism_superclass_share_maximum: Fraction
    presentation_and_definitional_combined_share_maximum: Fraction
    exact_operation_share_maximum: Fraction
    bank_entry_or_template_share_maximum: Fraction
    lemma_or_procedure_share_maximum: Fraction
    exact_ordered_composition_template_share_maximum: Fraction
    per_root_retained_pair_maximum: Literal[8]
    caps_are_maxima_not_quotas: Literal[True]
    force_rows_to_fill_cap_or_balance: Literal[False]

    @model_validator(mode="after")
    def _exact_caps(self) -> CapContract:
        observed = (
            self.compiler_data_root_share_maximum,
            self.any_single_source_share_maximum,
            self.family_share_maximum,
            self.mechanism_superclass_share_maximum,
            self.presentation_and_definitional_combined_share_maximum,
            self.exact_operation_share_maximum,
            self.bank_entry_or_template_share_maximum,
            self.lemma_or_procedure_share_maximum,
            self.exact_ordered_composition_template_share_maximum,
        )
        expected = (0.20, 0.40, 0.08, 0.12, 0.10, 0.02, 0.005, 0.0025, 0.005)
        if observed != expected:
            raise ValueError("cap maxima differ from the exact revision 0.3 contract")
        return self


class OneExampleGate(StrictModel):
    authorized: Literal[False]
    requires_user_approval: Literal[True]
    requires_repr_expr_renderer_dependency: Literal[True]
    requires_shared_contract_update: Literal[True]
    requires_representation_pre_gate_acceptance: Literal[True]
    requires_p23_binder_hygiene_regression: Literal[True]
    requires_inline_anchor_and_fixture_freezes: Literal[True]
    success_per_operation_and_eligible_project: Literal[1]
    adversarial_rejection_per_operation_and_eligible_project: Literal[1]
    zero_yield_waiver_allowed: Literal[False]
    census_backed_inapplicable_project_requires_policy_revision: Literal[True]
    retained_certificate_replay_fraction: Fraction

    @model_validator(mode="after")
    def _complete_replay(self) -> OneExampleGate:
        if self.retained_certificate_replay_fraction != 1.0:
            raise ValueError("one-example gate requires complete certificate replay")
        return self


class HundredRootGate(StrictModel):
    authorized: Literal[False]
    requires_one_example_gate_pass: Literal[True]
    eligible_roots_per_operation_approximately: Literal[100]
    retained_certificate_replay_fraction: Fraction
    persistent_meta_required: Literal[True]
    every_retained_row_typed_meta_validated_and_replayed: Literal[True]

    @model_validator(mode="after")
    def _complete_replay(self) -> HundredRootGate:
        if self.retained_certificate_replay_fraction != 1.0:
            raise ValueError("hundred-root gate requires complete certificate replay")
        return self


class TenKGate(StrictModel):
    authorized: Literal[False]
    requires_separate_user_approval_after_hundred_root_report: Literal[True]
    requested_root_count: Literal[10000]


class ClosedGate(StrictModel):
    authorized: Literal[False]


class Gates(StrictModel):
    one_example: OneExampleGate
    hundred_root: HundredRootGate
    ten_k_pilot: TenKGate
    bulk_scale: ClosedGate
    publication: ClosedGate


class ScaleContract(StrictModel):
    illustrative_root_count_for_cap_arithmetic: PositiveInt
    retained_pairs_per_root_cap: PositiveInt
    illustrative_arithmetical_maximum_rows: PositiveInt
    post_census_planning_band_rows: tuple[PositiveInt, PositiveInt]
    measured_target_is_minimum: Literal[False]
    measured_target_is_commitment: Literal[False]
    five_million_stretch_rows: Literal[5000000]
    minimum_roots_for_five_million_at_current_cap: PositiveInt
    five_million_feasible_at_illustrative_root_count: Literal[False]
    all_row_count_commitments_authorized: Literal[False]

    @model_validator(mode="after")
    def _arithmetic(self) -> ScaleContract:
        maximum = self.illustrative_root_count_for_cap_arithmetic * (
            self.retained_pairs_per_root_cap
        )
        if self.illustrative_arithmetical_maximum_rows != maximum:
            raise ValueError("illustrative maximum must equal root count times per-root cap")
        low, high = self.post_census_planning_band_rows
        if low > high or high > maximum:
            raise ValueError("post-census planning band must be ordered and feasible")
        minimum_roots = (
            self.five_million_stretch_rows + self.retained_pairs_per_root_cap - 1
        ) // self.retained_pairs_per_root_cap
        if self.minimum_roots_for_five_million_at_current_cap != minimum_roots:
            raise ValueError("five-million root requirement does not match the per-root cap")
        if self.illustrative_root_count_for_cap_arithmetic >= minimum_roots:
            raise ValueError("five-million feasibility contradicts the illustrative root count")
        return self


class SFT1CompositionPolicy(StrictModel):
    schema_version: Literal[1]
    policy_id: Literal["sft1_value_first_composition_proposal"]
    policy_version: Literal["0.3.0"]
    status: Literal["awaiting_user_approval"]
    approval_recorded: Literal[False]
    authorization: Authorization
    dependencies: Dependencies
    representation_contract: RepresentationContract
    label_contract: LabelContract
    negative_family_dimension_admissions: tuple[NegativeFamilyDimensionAdmission, ...]
    claim_erasure_guard_contract: ClaimErasureGuardContract
    axiom_profiles: tuple[AxiomProfile, ...]
    starter_banks: StarterBankBinding
    inline_anchor_resolution: InlineAnchorResolution
    adversarial_fixture_freeze: AdversarialFixtureFreeze
    p23_binder_hygiene_contract: P23BinderHygieneContract
    representation_pre_gate_acceptance: RepresentationPreGateAcceptance
    family_dispositions: tuple[FamilyDisposition, ...]
    operations: tuple[OperationSpec, ...]
    synthetic_track: SyntheticTrack
    composition_grammar: CompositionGrammar
    root_census: RootCensus
    source_eligibility_matrix: tuple[SourceEligibility, ...]
    execution_contract: ExecutionContract
    cache_contract: CacheContract
    sampling_and_quality: SamplingAndQuality
    cap_contract: CapContract
    deterministic_cap_order: tuple[NonEmptyStr, ...]
    gates: Gates
    scale_contract: ScaleContract

    @model_validator(mode="after")
    def _closed_policy(self) -> SFT1CompositionPolicy:
        operation_ids = tuple(operation.operation_id for operation in self.operations)
        if operation_ids != EXPECTED_OPERATION_IDS:
            raise ValueError("operations must equal the exact canonical revision 0.3 inventory")

        dispositions = tuple(
            (item.family_id, item.disposition) for item in self.family_dispositions
        )
        if dispositions != EXPECTED_FAMILY_DISPOSITIONS:
            raise ValueError("family dispositions differ from the exact revision 0.3 decisions")

        expected_axioms = {
            "classical_recorded": ("Classical.choice", "Quot.sound", "propext"),
            "constructive_kernel": (),
            "propext_recorded": ("propext",),
        }
        actual_axioms = {
            profile.profile_id: profile.allowed_axioms for profile in self.axiom_profiles
        }
        if actual_axioms != expected_axioms:
            raise ValueError("axiom profiles differ from the exact allowed profiles")
        all_operations = (*self.operations, *self.synthetic_track.operations)
        admission_records = tuple(
            (
                admission.admission_id,
                admission.family_id,
                admission.rubric_dimension,
                admission.track.value,
                admission.operation_ids,
            )
            for admission in self.negative_family_dimension_admissions
        )
        if admission_records != EXPECTED_NEGATIVE_ADMISSIONS:
            raise ValueError("negative family/dimension admissions differ from revision 0.3")
        admitted_negative_ids = tuple(
            operation_id
            for admission in self.negative_family_dimension_admissions
            for operation_id in admission.operation_ids
        )
        registered_negative_ids = tuple(
            operation.operation_id
            for operation in all_operations
            if operation.evidence_class in {EvidenceClass.N_RUBRIC, EvidenceClass.N_PROOF}
        )
        if set(admitted_negative_ids) != set(registered_negative_ids) or len(
            admitted_negative_ids
        ) != len(registered_negative_ids):
            raise ValueError("negative admissions must cover every negative operation exactly once")
        operations_for_admission = {
            operation.operation_id: operation for operation in all_operations
        }
        for admission in self.negative_family_dimension_admissions:
            for operation_id in admission.operation_ids:
                operation = operations_for_admission[operation_id]
                if (
                    operation.family_id != admission.family_id
                    or operation.rubric_dimension != admission.rubric_dimension
                    or operation.track != admission.track
                ):
                    raise ValueError("negative admission metadata disagrees with its operation")
        for operation in all_operations:
            if operation.allowed_axiom_profile not in actual_axioms:
                raise ValueError(
                    f"operation {operation.operation_id} names an unknown axiom profile"
                )

        if tuple(item.source_id for item in self.source_eligibility_matrix) != EXPECTED_SOURCE_IDS:
            raise ValueError("source eligibility matrix must contain the exact canonical sources")
        if self.deterministic_cap_order != EXPECTED_CAP_ORDER:
            raise ValueError("deterministic cap order differs from the frozen selection order")

        if self.scale_contract.retained_pairs_per_root_cap != (
            self.cap_contract.per_root_retained_pair_maximum
        ):
            raise ValueError("scale and cap contracts disagree on the per-root maximum")
        if any(
            operation.cap.maximum_retained_share > self.cap_contract.exact_operation_share_maximum
            or operation.cap.maximum_per_root > self.cap_contract.per_root_retained_pair_maximum
            for operation in all_operations
        ):
            raise ValueError("an operation cap exceeds the global exact-operation/per-root cap")

        by_family: dict[str, list[OperationSpec]] = {}
        for operation in self.operations:
            by_family.setdefault(operation.family_id, []).append(operation)
        for family_id in ("N21", "N22", "N28"):
            if family_id in by_family:
                raise ValueError(f"{family_id} cannot enter the natural operation registry")
        for family_id in ("P02", "P11"):
            if any(
                operation.status != OperationStatus.DIAGNOSTIC
                or operation.track != OperationTrack.DIAGNOSTIC
                for operation in by_family.get(family_id, ())
            ):
                raise ValueError(f"{family_id} operations must remain diagnostic")
        if tuple(operation.operation_id for operation in by_family.get("P01", ())) != (
            "P01_ALPHA_RENAME_SINGLE_V1",
        ):
            raise ValueError("P01 admits only the narrow single-rename alpha-cycle exception")
        for family_id in ("P39", "P41", "P42", "N29", "N30", "N31", "N32"):
            if any(
                operation.status != OperationStatus.PROOF_OF_CONCEPT
                for operation in by_family.get(family_id, ())
            ):
                raise ValueError(f"{family_id} operations must remain proof-of-concept")
        for operation in by_family.get("P21", ()):
            if "INTRO" in operation.operation_id and (
                operation.status != OperationStatus.DIAGNOSTIC
                or operation.cap.maximum_retained_share > 0.005
            ):
                raise ValueError("P21 introduction operations must be diagnostic and low-cap")

        operations_by_id = {operation.operation_id: operation for operation in all_operations}
        p23 = operations_by_id["P23_CURRY_PROP_PAIR_V1"]
        if p23.orientation != "pack" or not p23.typed_applicability.startswith("pack exactly"):
            raise ValueError("P23 revision 0.3 is pack-only")
        n32_rubric = operations_by_id["N32_SWAP_ROLE_ORDER_RUBRIC_V1"]
        n32_proof = operations_by_id["N32_SWAP_ROLE_ORDER_PROOF_V1"]
        if (
            "same binary relation" not in n32_rubric.typed_applicability
            or "reject function-composition reordering" not in n32_rubric.context_restrictions
            or "reject_function_composition_case" not in n32_rubric.anti_degeneracy_checks
            or "relation-converse-only" not in n32_proof.typed_applicability
        ):
            raise ValueError("N32 revision 0.3 is restricted to relation converse mutations")
        for operation in all_operations:
            if operation.evidence_class == EvidenceClass.N_PROOF:
                parent_id = operation.n_proof_subtype_of
                parent = operations_by_id.get(parent_id) if parent_id is not None else None
                if parent is None or parent.evidence_class != EvidenceClass.N_RUBRIC:
                    raise ValueError(
                        "N-PROOF subtype must reference an admitted N-RUBRIC operation"
                    )
                if parent.family_id != operation.family_id:
                    raise ValueError("N-PROOF and its N-RUBRIC super-operation must share a family")
                if operation.cap.maximum_retained_share > parent.cap.maximum_retained_share:
                    raise ValueError("N-PROOF cannot have a looser cap than its N-RUBRIC parent")

        registry_payload = [operation.model_dump(mode="json") for operation in all_operations]
        if hash_canonical(registry_payload) != EXPECTED_OPERATION_REGISTRY_HASH:
            raise ValueError("exact operation-registry fields differ from revision 0.3")
        if hash_canonical(self.root_census.model_dump(mode="json")) != EXPECTED_ROOT_CENSUS_HASH:
            raise ValueError("zero-Lean root census differs from revision 0.3")
        source_payload = [
            source.model_dump(mode="json") for source in self.source_eligibility_matrix
        ]
        if hash_canonical(source_payload) != EXPECTED_SOURCE_ELIGIBILITY_HASH:
            raise ValueError("source-eligibility matrix differs from revision 0.3")
        if hash_canonical(self.model_dump(mode="json")) != EXPECTED_POLICY_CONFIG_HASH:
            raise ValueError("SFT1 policy differs from the exact revision 0.3 freeze")

        return self


class StarterBankEntry(StrictModel):
    entry_id: SymbolicId
    anchor_kind: AnchorKind
    anchor_ref: NonEmptyStr
    expected_type_contract: NonEmptyStr
    orientations: tuple[NonEmptyStr, ...] = Field(min_length=1)
    allowed_axiom_profiles: tuple[SymbolicId, ...] = Field(min_length=1)
    anchor_spec_hash: Sha256
    resolved_lean_hash: Sha256 | None

    @model_validator(mode="after")
    def _anchor_hash(self) -> StarterBankEntry:
        if len(set(self.orientations)) != len(self.orientations):
            raise ValueError("bank-entry orientations must be unique")
        if len(set(self.allowed_axiom_profiles)) != len(self.allowed_axiom_profiles):
            raise ValueError("bank-entry axiom profiles must be unique")
        payload = {
            "entry_id": self.entry_id,
            "anchor_kind": self.anchor_kind.value,
            "anchor_ref": self.anchor_ref,
            "expected_type_contract": self.expected_type_contract,
            "orientations": list(self.orientations),
            "allowed_axiom_profiles": list(self.allowed_axiom_profiles),
        }
        if self.anchor_spec_hash != hash_canonical(payload):
            raise ValueError(f"starter-bank anchor hash mismatch for {self.entry_id}")
        return self


class StarterBank(StrictModel):
    bank_id: SymbolicId
    bank_kind: Literal[
        "positive_definition",
        "positive_lemma",
        "positive_procedure",
        "positive_schema",
        "negative_rubric_model",
        "negative_proof_template",
        "synthetic_negative_template",
    ]
    family_id: FamilyId
    track: OperationTrack
    entries: tuple[StarterBankEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_entries(self) -> StarterBank:
        entry_ids = tuple(entry.entry_id for entry in self.entries)
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError(f"starter bank {self.bank_id} contains duplicate entry IDs")
        return self


class StarterBankRegistryBinding(StrictModel):
    bank_id: SymbolicId
    entry_id: SymbolicId
    disposition: Literal["admitted_design", "reserved_unadmitted"]
    operation_ids: tuple[OperationId, ...]

    @model_validator(mode="after")
    def _admission_matches_operations(self) -> StarterBankRegistryBinding:
        if len(self.operation_ids) != len(set(self.operation_ids)):
            raise ValueError("starter-bank registry operation_ids must be unique")
        if self.disposition == "admitted_design" and not self.operation_ids:
            raise ValueError("admitted starter-bank entries require exact operation bindings")
        if self.disposition == "reserved_unadmitted" and self.operation_ids:
            raise ValueError("reserved starter-bank entries cannot bind operations")
        return self


class SFT1StarterBankSet(StrictModel):
    schema_version: Literal[1]
    bank_set_id: Literal["sft1_starter_banks_v0_3_0"]
    bank_set_version: Literal["0.3.0"]
    status: Literal["frozen_design"]
    hash_basis: Literal["sha256_canonical_anchor_spec_v1"]
    implementation_authorized: Literal[False]
    lean_resolution_complete: Literal[False]
    banks: tuple[StarterBank, ...]
    registry_bindings: tuple[StarterBankRegistryBinding, ...]

    @model_validator(mode="after")
    def _closed_bank_set(self) -> SFT1StarterBankSet:
        bank_ids = tuple(bank.bank_id for bank in self.banks)
        if bank_ids != EXPECTED_BANK_IDS:
            raise ValueError("starter banks differ from the exact frozen revision 0.3 bank set")
        entry_ids = [entry.entry_id for bank in self.banks for entry in bank.entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("starter-bank entry IDs must be globally unique")
        expected_bindings = tuple(
            (bank.bank_id, entry.entry_id) for bank in self.banks for entry in bank.entries
        )
        actual_bindings = tuple(
            (binding.bank_id, binding.entry_id) for binding in self.registry_bindings
        )
        if actual_bindings != expected_bindings:
            raise ValueError(
                "registry_bindings must cover every bank entry exactly once in canonical order"
            )
        if any(
            entry.resolved_lean_hash is not None for bank in self.banks for entry in bank.entries
        ):
            raise ValueError(
                "design freeze cannot claim unresolved Lean anchor hashes are resolved"
            )
        if hash_canonical(self.model_dump(mode="json")) != EXPECTED_STARTER_BANK_CONFIG_HASH:
            raise ValueError("starter-bank set differs from the exact revision 0.3 freeze")
        return self


def validate_sft1_policy_bindings(
    policy: SFT1CompositionPolicy,
    banks: SFT1StarterBankSet,
) -> None:
    """Validate all cross-file operation, bank, and axiom references."""

    profiles = {profile.profile_id for profile in policy.axiom_profiles}
    bank_index = {bank.bank_id: bank for bank in banks.banks}
    entry_index = {
        (bank.bank_id, entry.entry_id): (bank, entry)
        for bank in banks.banks
        for entry in bank.entries
    }
    binding_index = {
        (binding.bank_id, binding.entry_id): binding for binding in banks.registry_bindings
    }
    all_operations = (*policy.operations, *policy.synthetic_track.operations)
    operations_by_id = {operation.operation_id: operation for operation in all_operations}
    for bank in banks.banks:
        for entry in bank.entries:
            unknown_profiles = set(entry.allowed_axiom_profiles) - profiles
            if unknown_profiles:
                raise SFT1PolicyError(
                    f"starter-bank entry {entry.entry_id} names unknown axiom profiles "
                    f"{sorted(unknown_profiles)}"
                )

    bound_operation_ids: set[str] = set()
    for operation in all_operations:
        anchor = operation.anchor
        if anchor.hash_basis == AnchorHashBasis.UTF8_REF:
            continue
        assert anchor.bank_id is not None
        assert anchor.bank_entry_id is not None
        bound = entry_index.get((anchor.bank_id, anchor.bank_entry_id))
        if bound is None:
            raise SFT1PolicyError(
                f"operation {operation.operation_id} names an unknown starter-bank anchor"
            )
        bank, entry = bound
        binding = binding_index[(anchor.bank_id, anchor.bank_entry_id)]
        if operation.operation_id not in binding.operation_ids:
            raise SFT1PolicyError(
                f"operation {operation.operation_id} is absent from its bank registry binding"
            )
        bound_operation_ids.add(operation.operation_id)
        if anchor.kind != entry.anchor_kind or anchor.ref != entry.anchor_ref:
            raise SFT1PolicyError(
                f"operation {operation.operation_id} bank anchor kind/reference mismatch"
            )
        if anchor.schema_lemma_procedure_hash != entry.anchor_spec_hash:
            raise SFT1PolicyError(f"operation {operation.operation_id} bank anchor hash mismatch")
        if operation.allowed_axiom_profile not in entry.allowed_axiom_profiles:
            raise SFT1PolicyError(
                f"operation {operation.operation_id} uses a bank-disallowed axiom profile"
            )
        if bank.family_id.startswith("P") and bank.family_id != operation.family_id:
            raise SFT1PolicyError(
                f"operation {operation.operation_id} is bound to another positive family bank"
            )
        if bank.bank_id not in bank_index:
            raise AssertionError("unreachable bank index inconsistency")

    declared_bound_ids: set[str] = set()
    for binding in banks.registry_bindings:
        for operation_id in binding.operation_ids:
            registered_operation = operations_by_id.get(operation_id)
            if registered_operation is None:
                raise SFT1PolicyError(
                    f"starter-bank registry names unknown operation {operation_id}"
                )
            if operation_id in declared_bound_ids:
                raise SFT1PolicyError(
                    f"operation {operation_id} is bound to more than one starter-bank entry"
                )
            declared_bound_ids.add(operation_id)
            if (
                registered_operation.anchor.bank_id != binding.bank_id
                or registered_operation.anchor.bank_entry_id != binding.entry_id
            ):
                raise SFT1PolicyError(
                    f"starter-bank registry binding disagrees with operation {operation_id}"
                )
    if declared_bound_ids != bound_operation_ids:
        raise SFT1PolicyError("starter-bank registry and operation anchors do not reconcile")


@dataclass(frozen=True, slots=True)
class LoadedSFT1CompositionPolicy:
    """Validated policy and starter banks with deterministic provenance hashes."""

    loaded_policy: LoadedConfig[SFT1CompositionPolicy]
    loaded_banks: LoadedConfig[SFT1StarterBankSet]

    @property
    def config(self) -> SFT1CompositionPolicy:
        return self.loaded_policy.config

    @property
    def config_hash(self) -> str:
        return self.loaded_policy.config_hash

    @property
    def path(self) -> Path:
        return self.loaded_policy.path

    @property
    def banks(self) -> SFT1StarterBankSet:
        return self.loaded_banks.config

    @property
    def bank_config_hash(self) -> str:
        return self.loaded_banks.config_hash

    @property
    def bank_path(self) -> Path:
        return self.loaded_banks.path


def _repo_path(root: Path, relative: str, *, description: str) -> Path:
    resolved_root = root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise SFT1PolicyError(f"{description} path escapes the repository")
    return resolved


def load_sft1_composition_policy(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedSFT1CompositionPolicy:
    """Load revision 0.3 without importing Lean or authorizing execution."""

    root = find_repo_root(repo_root)
    resolved_root = root.resolve()
    resolved_policy = (path or root / _DEFAULT_POLICY_PATH).resolve()
    if not resolved_policy.is_relative_to(resolved_root):
        raise SFT1PolicyError("SFT1 policy path escapes the repository")
    loaded_policy = load_config(resolved_policy, SFT1CompositionPolicy)
    if loaded_policy.config_hash != EXPECTED_POLICY_CONFIG_HASH:
        raise SFT1PolicyError("SFT1 policy canonical hash differs from revision 0.3")
    policy = loaded_policy.config

    blocklist_path = _repo_path(
        root,
        policy.sampling_and_quality.evaluation_blocklist_path,
        description="evaluation blocklist",
    )
    observed_blocklist_sha = hash_file(blocklist_path)
    if observed_blocklist_sha != policy.sampling_and_quality.evaluation_blocklist_sha256:
        raise SFT1PolicyError(
            "evaluation-blocklist file hash drift: "
            f"{observed_blocklist_sha} != "
            f"{policy.sampling_and_quality.evaluation_blocklist_sha256}"
        )

    bank_path = _repo_path(root, policy.starter_banks.path, description="starter bank")
    observed_bank_sha = hash_file(bank_path)
    if observed_bank_sha != policy.starter_banks.file_sha256:
        raise SFT1PolicyError(
            "starter-bank file hash drift: "
            f"{observed_bank_sha} != {policy.starter_banks.file_sha256}"
        )
    loaded_banks = load_config(bank_path, SFT1StarterBankSet)
    if loaded_banks.config_hash != EXPECTED_STARTER_BANK_CONFIG_HASH:
        raise SFT1PolicyError("starter-bank canonical hash differs from revision 0.3")
    if (
        loaded_banks.config.bank_set_id != policy.starter_banks.bank_set_id
        or loaded_banks.config.bank_set_version != policy.starter_banks.bank_set_version
    ):
        raise SFT1PolicyError("starter-bank identity/version differs from policy binding")
    validate_sft1_policy_bindings(policy, loaded_banks.config)
    return LoadedSFT1CompositionPolicy(
        loaded_policy=loaded_policy,
        loaded_banks=loaded_banks,
    )


__all__ = [
    "EXPECTED_BANK_IDS",
    "EXPECTED_CACHE_KEY_FIELDS",
    "EXPECTED_CAP_ORDER",
    "EXPECTED_CLAIM_ERASURE_GUARDS",
    "EXPECTED_COMPOSITION_PRODUCTIONS",
    "EXPECTED_FAMILY_DISPOSITIONS",
    "EXPECTED_MUTUAL_EXCLUSION_OPERATION_IDS",
    "EXPECTED_NEGATIVE_ADMISSIONS",
    "EXPECTED_OPERATION_IDS",
    "EXPECTED_OPERATION_REGISTRY_HASH",
    "EXPECTED_P23_REGRESSIONS",
    "EXPECTED_P23_SIDECAR_BINDINGS",
    "EXPECTED_POLICY_CONFIG_HASH",
    "EXPECTED_PREVALIDATION_HASH_FIELDS",
    "EXPECTED_PRE_GATE_FAILURE_CLASSES",
    "EXPECTED_PRE_GATE_FAILURE_DIMENSIONS",
    "EXPECTED_PRE_GATE_SIDECAR_BINDINGS",
    "EXPECTED_REAL_GOAL_CASE_IDS",
    "EXPECTED_RENDERER_API_HASH_PAYLOAD_FIELDS",
    "EXPECTED_ROOT_CENSUS_HASH",
    "EXPECTED_SOURCE_ELIGIBILITY_HASH",
    "EXPECTED_STABLE_ROW_HASH_FIELDS",
    "EXPECTED_STARTER_BANK_CONFIG_HASH",
    "EXPECTED_STARTER_BANK_FILE_SHA256",
    "EXPECTED_SYNTHETIC_OPERATION_IDS",
    "CandidateTruth",
    "EvidenceClass",
    "LabelLane",
    "LoadedSFT1CompositionPolicy",
    "OperationSpec",
    "SFT1CompositionPolicy",
    "SFT1PolicyError",
    "SFT1StarterBankSet",
    "load_sft1_composition_policy",
    "validate_sft1_policy_bindings",
]
