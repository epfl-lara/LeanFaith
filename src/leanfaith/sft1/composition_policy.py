"""Strict, design-only loader for the SFT1 revision 0.3.1 composition policy.

This module is intentionally a policy boundary, not a transformation runtime. It
instantiates no backend, starts no Lean process, registers no operation, executes
no candidate, and emits no row. It imports the task-owned six-goal typed loader
only to replay already-frozen durable evidence. It validates the exact proposal
inventory, binds the approved REPR closed-Expr route and passed SFT1 six-goal
receipt, and binds the separately frozen starter-bank file.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.repr_six_goal_gate import load_six_goal_gate

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

EXPECTED_POLICY_CONFIG_HASH = "08a6d1b2ea03f3674d06cdac44478377084af24ba5cd4af7cab57303f4e7a917"
EXPECTED_OPERATION_REGISTRY_HASH = (
    "d56fca674f7b58d92dca09f0b76a702c54d1df2e5b68dcbe94225cad7e5cd95f"
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

EXPECTED_REPR_IMPLEMENTATION_COMMIT = "93cd9cf9d4848827f2bacad57a35c3d7f01500f7"
EXPECTED_REPR_FREEZE_COMMIT = "176a783842c5a73b84413dfa8347670608b615d9"
EXPECTED_REPR_SPEC_HASH = "68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"
EXPECTED_REPR_CONFIG_FILE_SHA256 = (
    "a65d5b29760bbc5eb89405927f946f205eb99856c0538fdf5b57d3f9eceb0db7"
)
EXPECTED_REPR_LEAN_RENDERER_SHA256 = (
    "4471262f812746046570c51dde5958ee33db31a450a6974071efce584ba56bc3"
)
EXPECTED_REPR_INJECTED_HELPER_SHA256 = (
    "a6650452eebe683db295df1dfe925d3db8b03fc24e55cbc6793e838b5fe2f272"
)
EXPECTED_REPR_PYTHON_RENDERER_SHA256 = (
    "496237e190c394e9bd3c3036e2bc01c635905116c5084787a42e6cb569f45517"
)
EXPECTED_REPR_IMPLEMENTATION_SET_HASH = (
    "9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff"
)
EXPECTED_REPR_RENDERER_SEMANTIC_HASH = (
    "0bec5429cc0e539841208be53cd52189a7b80cbdb4649ee2d45b84bd8a5ef1fd"
)
EXPECTED_REPR_RENDERER_API_HASH = "c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"
EXPECTED_REPR_UNIVERSE_PROFILE_ID = "goal_v1_first_occurrence_u_i_v1"
EXPECTED_REPR_UNIVERSE_PROFILE_HASH = (
    "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
)
EXPECTED_REPR_RENDER_CONTEXT_ID = "goal_v1_render_context_v1"
EXPECTED_REPR_RENDER_CONTEXT_HASH = (
    "5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"
)
EXPECTED_SIX_GOAL_HELPER_SOURCE_PATH = "LeanFaith/Meta/SFT1/RepresentationGate.lean"
EXPECTED_SIX_GOAL_HELPER_FILE_SHA256 = (
    "c87b9c5065a41f51e7cbdcdcc98f14fedc6a015054c40a0cfe4367ad63330129"
)
EXPECTED_SIX_GOAL_HELPER_PREAMBLE_SHA256 = (
    "bd0e3ef6b5e5c50bf07b31771e2a2ca0da131323d10d8571994bdd24a922981a"
)
EXPECTED_SIX_GOAL_GATE_CONFIG_PATH = (
    "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_v0_3_1.yaml"
)
EXPECTED_SIX_GOAL_GATE_CONFIG_FILE_SHA256 = (
    "5126eb8fb314218017fc930a79ab82cb810ff929e1794ce4617551f6c70ced91"
)
EXPECTED_SIX_GOAL_GATE_EFFECTIVE_CONFIG_HASH = (
    "7404e31935ab35b9c3270bf46654936121944a7e8f55fb91da4f1e047f59c0ad"
)
EXPECTED_SIX_GOAL_EXECUTION_CONFIG_PATH = (
    "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_execution_v0_3_1.yaml"
)
EXPECTED_SIX_GOAL_EXECUTION_CONFIG_FILE_SHA256 = (
    "82f22c08082e26424e1a55627b707d341e7fa84f72348cfed0b007b0526505ff"
)
EXPECTED_SIX_GOAL_EXECUTION_CONFIG_HASH = (
    "dfc7037ee8d5a340b82b237fa14ef1f3d9c2752bf64e91d34846d9570fac5747"
)
EXPECTED_SIX_GOAL_RECEIPT_PATH = (
    "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_receipt_v0_3_1.json"
)
EXPECTED_SIX_GOAL_RECEIPT_FILE_SHA256 = (
    "ebd400b4a7b05daa933b1abaaacc378d1a7b9ae68f9159ac03453cd6081406a8"
)
EXPECTED_SIX_GOAL_RECEIPT_HASH = "f62b68ebc946469952bdd34674c127e2bd1146b0a8febbe5d199fea54a081e78"

# Revision 0.3.1 preserves the 0.3.0 inventory in code as well as YAML. Keep this one
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
    "environment_fingerprint_hash",
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

EXPECTED_PRE_GATE_FORBIDDEN_RENDER_SUBSTRINGS: tuple[str, ...] = (
    "[anonymous]",
    "⋯",
)

EXPECTED_PRE_GATE_FAILURE_CLASSES: tuple[str, ...] = (
    "reference_render_failure",
    "candidate_render_failure",
    "expr_mvar",
    "universe_mvar",
    "free_variable",
    "loose_bound_variable",
    "anonymous_binder_name",
    "forbidden_rendered_placeholder",
    "ill_typed",
    "non_prop",
    "wrong_turnstile_count",
    "required_distinct_render_collapsed",
    "universe_profile_mismatch",
    "renderer_context_mismatch",
    "repr_real_goal_coverage_not_passed",
)

EXPECTED_FORBIDDEN_RENDER_FAILURE_MAPPINGS: tuple[tuple[str, str], ...] = (
    ("[anonymous]", "anonymous_binder_name"),
    ("⋯", "forbidden_rendered_placeholder"),
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

EXPECTED_ALL_OPERATION_IDS: tuple[str, ...] = (
    *EXPECTED_OPERATION_IDS,
    *EXPECTED_SYNTHETIC_OPERATION_IDS,
)

EXPECTED_CURRENT_WAVE_ID = "sft1_wave_1_proposal_v1"
EXPECTED_CURRENT_WAVE_OPERATION_IDS: tuple[str, ...] = (
    "P01_ALPHA_RENAME_SINGLE_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P21_BETA_REDUCE_V1",
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
)
EXPECTED_CURRENT_WAVE_UNSELECTED_OPERATION_IDS: tuple[str, ...] = tuple(
    operation_id
    for operation_id in EXPECTED_ALL_OPERATION_IDS
    if operation_id not in EXPECTED_CURRENT_WAVE_OPERATION_IDS
)
EXPECTED_CURRENT_WAVE_OPERATION_PROJECT_COMBINATIONS = 24
EXPECTED_CURRENT_WAVE_FIXTURES = 48
EXPECTED_CURRENT_WAVE_APPROXIMATE_ROOTS = 600
EXPECTED_FULL_MATRIX_OPERATION_PROJECT_COMBINATIONS = 156
EXPECTED_FULL_MATRIX_FIXTURES = 312
EXPECTED_FULL_MATRIX_APPROXIMATE_ROOTS = 4600
EXACT_CURRENT_USER_DECISION = (
    "Approve SFT1 Wave 1 gate admission for P01_ALPHA_RENAME_SINGLE_V1, "
    "P15_SWAP_IFF_SIDES_V1, P18_SYMMETRIZE_EQUALITY_V1, P21_BETA_REDUCE_V1, "
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1, and N31_DROP_REQUIRED_GUARD_PROOF_V1 "
    "across their registered eligible projects, including gate admission of the N31 "
    "required_domain_guard family/dimension for those two negative operations, solely for "
    "bounded implementation, the "
    "one-positive/one-negative end-to-end smoke, the selected-wave conformance matrix, "
    "and the approximately-100-roots-per-operation gate; do not grant production "
    "admission, row emission, a 10K pilot, scale, publication, or any row-count commitment."
)
EXPECTED_NEGATIVE_PROMOTION_MEASUREMENTS: tuple[str, ...] = (
    "typed_applicability_yield_and_exact_failure_classes",
    "certificate_replay_pass_rate",
    "anti_degeneracy_rejection_rate",
    "candidate_truth_proved_refuted_unknown_distribution",
    "duplicate_conflict_and_blocklist_drop_rates",
    "lean_seconds_and_sidecar_bytes_per_retained_pair",
    "source_project_family_mechanism_template_and_polarity_strata",
    "surface_residue_and_held_out_balanced_accuracy_with_confidence_bounds",
)
EXPECTED_NEGATIVE_PRODUCTION_DECISION_RECORD_FIELDS: tuple[str, ...] = (
    "operation_id",
    "operation_version",
    "eligible_project_ids",
    "family_id",
    "rubric_dimension",
    "label_lane",
    "track",
    "operation_registry_entry_hash",
    "resolved_implementation_file_sha256",
    "dispatch_symbol",
    "certificate_checker_id",
    "certificate_checker_file_sha256",
    "resolved_anchor_hash",
    "bank_or_template_resolved_hash",
    "fixture_bundle_hash_set",
    "logic_regime",
    "allowed_axiom_profile",
    "exact_cap",
    "hundred_root_gate_receipt_hash",
)

EXPECTED_CERTIFICATE_CLASS_BINDINGS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "P-DEF",
        "p_def_defeq_certificate_v1",
        (
            "P01_ALPHA_RENAME_SINGLE_V1",
            "P02_REGROUP_BINDERS_V1",
            "P20_FOLD_SET_NONEMPTY_V1",
            "P20_UNFOLD_SET_NONEMPTY_V1",
            "P21_BETA_INTRO_V1",
            "P21_BETA_REDUCE_V1",
            "P21_ZETA_INTRO_V1",
            "P21_ZETA_REDUCE_V1",
            "P22_ETA_REDUCE_EXPLICIT_FUN_V1",
        ),
    ),
    (
        "P-SCHEMA",
        "p_schema_typed_equivalence_certificate_v1",
        (
            "P11_BOUNDED_FORALL_EXPAND_V1",
            "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
            "P15_SWAP_IFF_SIDES_V1",
            "P16_REASSOC_AND_LEFT_V1",
            "P18_SYMMETRIZE_EQUALITY_V1",
            "P23_CURRY_PROP_PAIR_V1",
            "P24_SWAP_INDEPENDENT_PROP_BINDERS_V1",
            "P28_DECOMPOSE_IFF_V1",
            "P35_SET_INTER_MEMBERSHIP_V1",
            "P38_EXISTS_SUBTYPE_NONEMPTY_V1",
            "P40_EXISTS_UNIQUE_EXPAND_V1",
        ),
    ),
    (
        "P-LEMMA",
        "p_lemma_bidirectional_certificate_v1",
        (
            "P32_ADD_ASSOC_LOCAL_V1",
            "P32_ADD_COMM_LOCAL_V1",
            "P33_EQ_HYP_SUBSTITUTE_NONDEPENDENT_V1",
            "P34_NAT_SUCC_ADD_ONE_LOCAL_V1",
            "P36_SET_EXTENTIONALITY_V1",
            "P39_HYP_SET_INTER_REWRITE_V1",
            "P41_SUBTYPE_FORALL_GUARD_V1",
        ),
    ),
    (
        "P-REFLECT",
        "p_reflect_replay_certificate_v1",
        ("P42_RING_POLYNOMIAL_LOCAL_V1",),
    ),
    (
        "N-RUBRIC",
        "n_rubric_exact_protected_delta_certificate_v1",
        (
            "N19_NEGATE_CLOSED_CLAIM_RUBRIC_V1",
            "N25_TOGGLE_EQ_NE_RUBRIC_V1",
            "N26_INCREMENT_BOUND_RUBRIC_V1",
            "N29_SWAP_WITNESS_DEPENDENCY_RUBRIC_V1",
            "N30_ADD_UNJUSTIFIED_UNIQUENESS_RUBRIC_V1",
            "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
            "N32_SWAP_ROLE_ORDER_RUBRIC_V1",
            "N28_FINITE_ARITHMETIC_RUBRIC_V1",
            "N28_FINITE_SET_RUBRIC_V1",
        ),
    ),
    (
        "N-PROOF",
        "n_proof_source_proof_candidate_refutation_certificate_v1",
        (
            "N19_NEGATE_CLOSED_CLAIM_PROOF_V1",
            "N25_TOGGLE_EQ_NE_PROOF_V1",
            "N26_INCREMENT_BOUND_PROOF_V1",
            "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
            "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
            "N31_DROP_REQUIRED_GUARD_PROOF_V1",
            "N32_SWAP_ROLE_ORDER_PROOF_V1",
            "N28_FINITE_ARITHMETIC_PROOF_V1",
            "N28_FINITE_SET_PROOF_V1",
        ),
    ),
)

EXPECTED_ROW_EVIDENCE_AXES: tuple[str, ...] = (
    "reference_closed_prop_validation",
    "candidate_closed_prop_validation",
    "f0_definitional_relation",
    "f1_claim_relation_certificate",
    "candidate_truth_evidence",
    "optional_f2_direction_evidence",
    "final_retain_or_drop_disposition",
)

EXPECTED_BINDER_ELABORATION_CHECKS: tuple[str, ...] = (
    "complete_telescope_dependency_graph",
    "free_and_bound_variable_identity",
    "capture_and_shadowing",
    "binder_info_preservation",
    "dependent_binder_types_and_continuation",
    "de_bruijn_reindexing",
    "generated_name_hygiene",
    "implicit_arguments",
    "coercions_and_casts",
    "typeclass_instances",
    "universe_levels_and_constraints",
    "constructive_or_classical_logic_regime",
    "transparency_and_normalization_environment",
)

EXPECTED_ENVIRONMENT_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "lean_version",
    "project_id",
    "project_revision",
    "toolchain_revision",
    "imports_hash",
    "options_hash",
    "synthesized_instance_hashes",
    "transparency",
    "allowed_axiom_profile",
    "operation_registry_entry_hash",
    "resolved_anchor_hash",
    "certificate_checker_hash",
    "repr_replacement_commit",
    "renderer_api_hash",
    "repr_spec_hash",
    "canonical_universe_profile_hash",
    "render_context_hash",
)

EXPECTED_EMPTY_DOMAIN_PROFILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "requires_checked_nonempty_domain",
        (
            "N26_INCREMENT_BOUND_RUBRIC_V1",
            "N26_INCREMENT_BOUND_PROOF_V1",
            "N29_SWAP_WITNESS_DEPENDENCY_RUBRIC_V1",
            "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
            "N28_FINITE_ARITHMETIC_RUBRIC_V1",
            "N28_FINITE_ARITHMETIC_PROOF_V1",
            "N28_FINITE_SET_RUBRIC_V1",
            "N28_FINITE_SET_PROOF_V1",
        ),
    ),
    (
        "reject_empty_or_unreachable_domain",
        (
            "N30_ADD_UNJUSTIFIED_UNIQUENESS_RUBRIC_V1",
            "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
            "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
            "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        ),
    ),
    (
        "equivalence_valid_without_nonempty_assumption",
        (
            "P11_BOUNDED_FORALL_EXPAND_V1",
            "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
            "P23_CURRY_PROP_PAIR_V1",
            "P24_SWAP_INDEPENDENT_PROP_BINDERS_V1",
            "P38_EXISTS_SUBTYPE_NONEMPTY_V1",
            "P40_EXISTS_UNIQUE_EXPAND_V1",
            "P41_SUBTYPE_FORALL_GUARD_V1",
        ),
    ),
)

EXPECTED_NEGATIVE_APPLICABILITY_BANKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "n26_bounded_domain_strengthening_v1",
        ("N26_INCREMENT_BOUND_RUBRIC_V1", "N26_INCREMENT_BOUND_PROOF_V1"),
    ),
    (
        "n31_required_guard_heads_v1",
        ("N31_DROP_REQUIRED_GUARD_RUBRIC_V1", "N31_DROP_REQUIRED_GUARD_PROOF_V1"),
    ),
    (
        "n32_ordered_relation_heads_v1",
        ("N32_SWAP_ROLE_ORDER_RUBRIC_V1", "N32_SWAP_ROLE_ORDER_PROOF_V1"),
    ),
)

EXPECTED_CORRELATION_KEY_FIELDS: tuple[str, ...] = (
    "source_id",
    "project_id",
    "correlation_group_id",
)

EXPECTED_EFFECTIVE_DIVERSITY_KEY_FIELDS: tuple[str, ...] = (
    "source_id",
    "project_id",
    "effective_diversity_group_id",
)

EXPECTED_CORRELATION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("corr_p01_alpha", ("P01_ALPHA_RENAME_SINGLE_V1",)),
    ("corr_p02_binder_regroup", ("P02_REGROUP_BINDERS_V1",)),
    ("corr_p11_bounded_forall", ("P11_BOUNDED_FORALL_EXPAND_V1",)),
    ("corr_p14_data_binder_swap", ("P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",)),
    ("corr_p15_iff_symmetry", ("P15_SWAP_IFF_SIDES_V1",)),
    ("corr_p16_and_association", ("P16_REASSOC_AND_LEFT_V1",)),
    ("corr_p18_equality_symmetry", ("P18_SYMMETRIZE_EQUALITY_V1",)),
    (
        "corr_p20_set_nonempty_definition",
        ("P20_FOLD_SET_NONEMPTY_V1", "P20_UNFOLD_SET_NONEMPTY_V1"),
    ),
    ("corr_p21_beta", ("P21_BETA_INTRO_V1", "P21_BETA_REDUCE_V1")),
    ("corr_p21_zeta", ("P21_ZETA_INTRO_V1", "P21_ZETA_REDUCE_V1")),
    ("corr_p22_eta", ("P22_ETA_REDUCE_EXPLICIT_FUN_V1",)),
    ("corr_p23_prop_pair", ("P23_CURRY_PROP_PAIR_V1",)),
    ("corr_p24_prop_binder_swap", ("P24_SWAP_INDEPENDENT_PROP_BINDERS_V1",)),
    ("corr_p28_iff_decomposition", ("P28_DECOMPOSE_IFF_V1",)),
    ("corr_p32_add_ac", ("P32_ADD_ASSOC_LOCAL_V1", "P32_ADD_COMM_LOCAL_V1")),
    ("corr_p33_eq_hyp_transport", ("P33_EQ_HYP_SUBSTITUTE_NONDEPENDENT_V1",)),
    ("corr_p34_nat_succ", ("P34_NAT_SUCC_ADD_ONE_LOCAL_V1",)),
    ("corr_p35_set_inter_membership", ("P35_SET_INTER_MEMBERSHIP_V1",)),
    ("corr_p36_set_extensionality", ("P36_SET_EXTENTIONALITY_V1",)),
    ("corr_p38_subtype_nonempty", ("P38_EXISTS_SUBTYPE_NONEMPTY_V1",)),
    ("corr_p39_hyp_set_inter", ("P39_HYP_SET_INTER_REWRITE_V1",)),
    ("corr_p40_exists_unique", ("P40_EXISTS_UNIQUE_EXPAND_V1",)),
    ("corr_p41_subtype_guard", ("P41_SUBTYPE_FORALL_GUARD_V1",)),
    ("corr_p42_ring_reflection", ("P42_RING_POLYNOMIAL_LOCAL_V1",)),
    (
        "corr_n19_negation",
        ("N19_NEGATE_CLOSED_CLAIM_RUBRIC_V1", "N19_NEGATE_CLOSED_CLAIM_PROOF_V1"),
    ),
    (
        "corr_n25_relation_polarity",
        ("N25_TOGGLE_EQ_NE_RUBRIC_V1", "N25_TOGGLE_EQ_NE_PROOF_V1"),
    ),
    (
        "corr_n26_bound_strengthening",
        ("N26_INCREMENT_BOUND_RUBRIC_V1", "N26_INCREMENT_BOUND_PROOF_V1"),
    ),
    (
        "corr_n29_witness_dependency",
        (
            "N29_SWAP_WITNESS_DEPENDENCY_RUBRIC_V1",
            "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
        ),
    ),
    (
        "corr_n30_uniqueness",
        (
            "N30_ADD_UNJUSTIFIED_UNIQUENESS_RUBRIC_V1",
            "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
        ),
    ),
    (
        "corr_n31_guard_drop",
        ("N31_DROP_REQUIRED_GUARD_RUBRIC_V1", "N31_DROP_REQUIRED_GUARD_PROOF_V1"),
    ),
    (
        "corr_n32_role_order",
        ("N32_SWAP_ROLE_ORDER_RUBRIC_V1", "N32_SWAP_ROLE_ORDER_PROOF_V1"),
    ),
    (
        "corr_n28_finite_arithmetic",
        ("N28_FINITE_ARITHMETIC_RUBRIC_V1", "N28_FINITE_ARITHMETIC_PROOF_V1"),
    ),
    (
        "corr_n28_finite_set",
        ("N28_FINITE_SET_RUBRIC_V1", "N28_FINITE_SET_PROOF_V1"),
    ),
)

EXPECTED_EFFECTIVE_DIVERSITY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    *EXPECTED_CORRELATION_GROUPS[:8],
    (
        "effective_p21_beta_zeta",
        (
            "P21_BETA_INTRO_V1",
            "P21_BETA_REDUCE_V1",
            "P21_ZETA_INTRO_V1",
            "P21_ZETA_REDUCE_V1",
        ),
    ),
    *EXPECTED_CORRELATION_GROUPS[10:],
)

EXPECTED_HUNDRED_ROOT_CONSERVATION_EQUATIONS: tuple[str, ...] = (
    "roots_attempted = roots_ineligible + roots_eligible",
    "roots_eligible = roots_inapplicable + roots_applicable",
    "roots_applicable = source_closed_prop_invalid + source_closed_prop_valid",
    "candidates_generated = candidate_closed_prop_invalid + candidate_closed_prop_valid",
    (
        "candidate_closed_prop_valid = f1_relation_certified + "
        "f1_relation_uncertified + f1_not_run_due_prior_terminal_drop"
    ),
    (
        "f1_relation_certified = candidate_truth_proved + candidate_truth_refuted + "
        "candidate_truth_unknown"
    ),
    "retained = positive_retained + n_rubric_retained + n_proof_retained",
    (
        "candidates_generated = candidate_closed_prop_invalid + no_op_dropped + "
        "cancellation_dropped + blocklist_dropped + f1_relation_uncertified + "
        "vacuity_rejected + empty_domain_rejected + within_root_duplicate_dropped + "
        "cross_root_duplicate_dropped + split_cluster_collision_dropped + retained"
    ),
)

EXPECTED_CANDIDATE_TERMINAL_DISPOSITION_COUNTERS: tuple[str, ...] = (
    "candidate_closed_prop_invalid",
    "no_op_dropped",
    "cancellation_dropped",
    "blocklist_dropped",
    "f1_relation_uncertified",
    "vacuity_rejected",
    "empty_domain_rejected",
    "within_root_duplicate_dropped",
    "cross_root_duplicate_dropped",
    "split_cluster_collision_dropped",
    "retained",
)

EXPECTED_HUNDRED_ROOT_COUNTER_FIELDS: tuple[str, ...] = (
    "roots_attempted",
    "roots_ineligible",
    "roots_eligible",
    "roots_inapplicable",
    "roots_applicable",
    "candidates_generated",
    "source_closed_prop_invalid",
    "source_closed_prop_valid",
    "candidate_closed_prop_invalid",
    "candidate_closed_prop_valid",
    "f0_relation_checked",
    "f1_relation_certified",
    "f1_relation_uncertified",
    "f1_not_run_due_prior_terminal_drop",
    "candidate_truth_proved",
    "candidate_truth_refuted",
    "candidate_truth_unknown",
    "negative_witness_passed",
    "vacuity_rejected",
    "empty_domain_rejected",
    "no_op_dropped",
    "cancellation_dropped",
    "within_root_duplicate_dropped",
    "cross_root_duplicate_dropped",
    "split_cluster_collision_dropped",
    "blocklist_dropped",
    "positive_retained",
    "n_rubric_retained",
    "n_proof_retained",
    "retained",
)

EXPECTED_HUNDRED_ROOT_COUNTER_DIMENSIONS: tuple[str, ...] = (
    "source",
    "project",
    "family",
    "operation",
    "mechanism_superclass",
    "template_or_bank_entry",
    "polarity",
    "exact_failure_class",
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


class ClosedPropValidation(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class F0Relation(StrEnum):
    DEFINITIONALLY_EQUAL = "definitionally_equal"
    NOT_DEFINITIONALLY_EQUAL = "not_definitionally_equal"
    UNKNOWN = "unknown"


class F1ClaimRelation(StrEnum):
    PRESERVING = "preserving"
    BREAKING = "breaking"
    UNCERTIFIED = "uncertified"


class F2DirectionEvidence(StrEnum):
    EQUIVALENT = "equivalent"
    SOURCE_IMPLIES_CANDIDATE = "source_implies_candidate"
    CANDIDATE_IMPLIES_SOURCE = "candidate_implies_source"
    LOGICALLY_INCOMPARABLE = "logically_incomparable"


class RetainDisposition(StrEnum):
    RETAIN = "retain"
    DROP = "drop"


class TerminalDispositionReason(StrEnum):
    CANDIDATE_CLOSED_PROP_INVALID = "candidate_closed_prop_invalid"
    NO_OP_DROPPED = "no_op_dropped"
    CANCELLATION_DROPPED = "cancellation_dropped"
    BLOCKLIST_DROPPED = "blocklist_dropped"
    F1_RELATION_UNCERTIFIED = "f1_relation_uncertified"
    VACUITY_REJECTED = "vacuity_rejected"
    EMPTY_DOMAIN_REJECTED = "empty_domain_rejected"
    WITHIN_ROOT_DUPLICATE_DROPPED = "within_root_duplicate_dropped"
    CROSS_ROOT_DUPLICATE_DROPPED = "cross_root_duplicate_dropped"
    SPLIT_CLUSTER_COLLISION_DROPPED = "split_cluster_collision_dropped"
    RETAINED = "retained"


class EmptyDomainDisposition(StrEnum):
    REQUIRES_CHECKED_NONEMPTY = "requires_checked_nonempty_domain"
    REJECT_EMPTY = "reject_empty_or_unreachable_domain"
    VALID_WITHOUT_NONEMPTY = "equivalence_valid_without_nonempty_assumption"
    NOT_APPLICABLE = "empty_domain_not_applicable"


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


class BoundedImplementationScopeAuthorization(StrictModel):
    user_authorized: Literal[True]
    scope_id: Literal["sft1_selected_wave_bounded_gates_v1"]
    covers_transform_implementation_after_gate_admission_and_readiness: Literal[True]
    covers_one_positive_one_negative_end_to_end_smoke: Literal[True]
    covers_selected_wave_conformance_matrix: Literal[True]
    covers_approximately_hundred_roots_per_selected_operation: Literal[True]
    current_instruction_prohibits_lean_transform_execution_and_row_generation: Literal[True]
    work_may_start_before_current_wave_gate_admission: Literal[False]


class ImplementationReadiness(StrictModel):
    status: Literal["not_ready_fail_closed"]
    ready: Literal[False]
    unresolved_prerequisite_ids: tuple[NonEmptyStr, ...]
    unselected_operations_are_readiness_blockers: Literal[False]

    @model_validator(mode="after")
    def _exact_blockers(self) -> ImplementationReadiness:
        if self.unresolved_prerequisite_ids != (
            "current_wave_gate_admission",
            "shared_contract_update",
            "zero_lean_root_census_and_source_eligibility",
            "selected_wave_execution_bindings",
        ):
            raise ValueError("implementation-readiness blockers differ from revision 0.3.1")
        return self


class CurrentWaveGateAdmission(StrictModel):
    wave_id: Literal["sft1_wave_1_proposal_v1"]
    status: Literal["awaiting_exact_user_gate_admission"]
    proposed_operation_ids: tuple[OperationId, ...]
    proposed_negative_family_dimension_admission_ids: tuple[SymbolicId, ...]
    gate_admitted: Literal[False]
    gate_admitted_operation_ids: tuple[OperationId, ...]
    user_decision_record: None
    exact_remaining_user_decision: NonEmptyStr
    proposed_operation_project_combinations: PositiveInt
    proposed_fixture_count: PositiveInt
    proposed_approximate_root_count: PositiveInt
    grants_production_admission: Literal[False]
    grants_row_emission_or_scale: Literal[False]

    @model_validator(mode="after")
    def _exact_wave_proposal(self) -> CurrentWaveGateAdmission:
        if self.proposed_operation_ids != EXPECTED_CURRENT_WAVE_OPERATION_IDS:
            raise ValueError("current Wave 1 operation proposal differs from revision 0.3.1")
        if self.proposed_negative_family_dimension_admission_ids != (
            "n31_required_domain_guard_natural_v1",
        ):
            raise ValueError("current Wave 1 negative family/dimension proposal is not exact")
        if self.gate_admitted_operation_ids:
            raise ValueError("current freeze has no gate-admitted operation")
        if self.exact_remaining_user_decision != EXACT_CURRENT_USER_DECISION:
            raise ValueError("exact remaining Wave 1 user decision differs from revision 0.3.1")
        if (
            self.proposed_operation_project_combinations,
            self.proposed_fixture_count,
            self.proposed_approximate_root_count,
        ) != (
            EXPECTED_CURRENT_WAVE_OPERATION_PROJECT_COMBINATIONS,
            EXPECTED_CURRENT_WAVE_FIXTURES,
            EXPECTED_CURRENT_WAVE_APPROXIMATE_ROOTS,
        ):
            raise ValueError("current Wave 1 measured-work projection differs from revision 0.3.1")
        return self


class ProductionAdmissionAuthorization(StrictModel):
    status: Literal["none_admitted"]
    admitted_operation_ids: tuple[OperationId, ...]
    admitted_negative_operation_ids: tuple[OperationId, ...]
    current_freeze_production_negative_count: Literal[0]
    bounded_gate_pass_auto_grants_production_admission: Literal[False]
    exact_post_gate_user_decision_required: Literal[True]

    @model_validator(mode="after")
    def _none_admitted(self) -> ProductionAdmissionAuthorization:
        if self.admitted_operation_ids or self.admitted_negative_operation_ids:
            raise ValueError("revision 0.3.1 permits zero production-admitted operations")
        return self


class RowEmissionAndScaleAuthorization(StrictModel):
    row_emission: Literal[False]
    ten_k_pilot: Literal[False]
    bulk_scale: Literal[False]
    publication: Literal[False]
    row_count_commitment: Literal[False]


class Authorization(StrictModel):
    policy_loader_and_invariant_tests: Literal[True]
    repr_dependency_integration: Literal[True]
    repr_six_real_goal_gate: Literal[True]
    bounded_implementation_scope: BoundedImplementationScopeAuthorization
    implementation_readiness: ImplementationReadiness
    gate_admission: CurrentWaveGateAdmission
    production_admission: ProductionAdmissionAuthorization
    row_emission_and_scale: RowEmissionAndScaleAuthorization


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
    status: Literal["approved_freeze_sft1_six_goal_passed"]
    required_signature: Literal["renderClosedProp (e : Expr) : MetaM String"]
    required_namespace: Literal["LeanFaith.GoalV1"]
    replacement_must_be_new_coherent_freeze: Literal[True]
    implementation_commit: GitCommit
    replacement_commit: GitCommit
    replacement_spec_hash: Sha256
    replacement_config_path: Literal["configs/representations/goal_v1_v1.yaml"]
    replacement_config_file_sha256: Sha256
    replacement_lean_renderer_path: Literal["LeanFaith/Meta/GoalV1.lean"]
    replacement_lean_renderer_sha256: Sha256
    replacement_injected_helper_sha256: Sha256
    replacement_python_renderer_path: Literal["src/leanfaith/representations/goal_v1.py"]
    replacement_python_renderer_sha256: Sha256
    implementation_set_hash: Sha256
    renderer_semantic_hash: Sha256
    renderer_api_hash: Sha256
    renderer_api_hash_basis: Literal["sha256_canonical_renderer_api_binding_v1"]
    renderer_api_hash_payload_fields: tuple[NonEmptyStr, ...]
    populated_renderer_api_hash_must_replay_from_payload: Literal[True]
    canonical_universe_profile_id: Literal["goal_v1_first_occurrence_u_i_v1"]
    canonical_universe_profile_hash: Sha256
    canonical_universe_profile_must_define_level_instantiation_and_naming: Literal[True]
    render_context_id: Literal["goal_v1_render_context_v1"]
    render_context_hash: Sha256
    route_id: Literal["closed_expr_in_session"]
    python_entrypoint: Literal["render_closed_expr_in_session"]
    endpoint_emitter: Literal["LeanFaith.GoalV1.emitClosedProp"]
    emitter_calls_per_unrolled_endpoint: Literal[1]
    persist_complete_sidecars: Literal[True]
    model_facing_projection: Literal["sidecar.core_text()"]
    real_goal_coverage_regression_id: Literal["sft1_repr_six_real_goal_direct_expr_v0_3_1"]
    real_goal_coverage_regression_hash: Literal[
        "f62b68ebc946469952bdd34674c127e2bd1146b0a8febbe5d199fea54a081e78"
    ]
    real_goal_coverage_regression_passed: Literal[True]
    real_goal_coverage_uses_closed_expr_api: Literal[True]
    all_required_real_goals_must_render_successfully: Literal[True]
    required_real_goal_case_ids: tuple[NonEmptyStr, ...]
    unresolved_expr_mvars_allowed: Literal[False]
    unresolved_universe_mvars_allowed: Literal[False]
    free_variables_allowed: Literal[False]
    loose_bound_variables_allowed: Literal[False]
    anonymous_telescope_binder_names_allowed: Literal[False]
    anonymous_binder_rejection_scope: Literal["unsupported_anonymous_outer_pi_locals"]
    preserves_nondependent_explicit_structural_arrows: Literal[True]
    type_inference_must_succeed_before_render: Literal[True]
    api_rejects_unsupported_anonymous_telescope_binder: Literal[True]
    api_rejects_ill_typed_expr: Literal[True]
    non_prop_allowed: Literal[False]

    @model_validator(mode="after")
    def _exact_real_goal_cases(self) -> ExprRendererApiDependency:
        if self.required_real_goal_case_ids != EXPECTED_REAL_GOAL_CASE_IDS:
            raise ValueError("REPR replacement must cover the exact six real-goal cases")
        if self.renderer_api_hash_payload_fields != EXPECTED_RENDERER_API_HASH_PAYLOAD_FIELDS:
            raise ValueError("renderer API hash payload fields differ from revision 0.3.1")
        exact_binding = {
            "implementation_commit": EXPECTED_REPR_IMPLEMENTATION_COMMIT,
            "replacement_commit": EXPECTED_REPR_FREEZE_COMMIT,
            "replacement_spec_hash": EXPECTED_REPR_SPEC_HASH,
            "replacement_config_file_sha256": EXPECTED_REPR_CONFIG_FILE_SHA256,
            "replacement_lean_renderer_sha256": EXPECTED_REPR_LEAN_RENDERER_SHA256,
            "replacement_injected_helper_sha256": EXPECTED_REPR_INJECTED_HELPER_SHA256,
            "replacement_python_renderer_sha256": EXPECTED_REPR_PYTHON_RENDERER_SHA256,
            "implementation_set_hash": EXPECTED_REPR_IMPLEMENTATION_SET_HASH,
            "renderer_semantic_hash": EXPECTED_REPR_RENDERER_SEMANTIC_HASH,
            "renderer_api_hash": EXPECTED_REPR_RENDERER_API_HASH,
            "canonical_universe_profile_hash": EXPECTED_REPR_UNIVERSE_PROFILE_HASH,
            "render_context_hash": EXPECTED_REPR_RENDER_CONTEXT_HASH,
        }
        for field, expected in exact_binding.items():
            if getattr(self, field) != expected:
                raise ValueError(f"REPR approved-freeze binding differs at {field}")
        api_hash_payload = {
            field: getattr(self, field) for field in self.renderer_api_hash_payload_fields
        }
        if self.renderer_api_hash != hash_canonical(api_hash_payload):
            raise ValueError("renderer API hash does not replay from its declared payload")
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
    sft1_calls_endpoint_emitter_directly_for_both_sides: Literal[True]
    sft1_calls_render_closed_prop_directly: Literal[False]
    endpoint_emitter_uses_frozen_renderer: Literal[True]
    same_persistent_meta_request: Literal[True]
    meta_request_command: Literal["run_meta do"]
    endpoints_explicitly_unrolled: Literal[True]
    reference_and_candidate_exprs_alive_in_request: Literal[True]
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


class NegativeOperationPromotionContract(StrictModel):
    applies_to_evidence_classes: tuple[EvidenceClass, ...]
    initial_operation_status: Literal["proof_of_concept"]
    bounded_gate_admission_requires_current_wave_user_decision: Literal[True]
    required_gate_sequence: tuple[NonEmptyStr, ...]
    production_supporting_measured_gate: Literal[
        "approximately_100_roots_per_selected_operation_after_smoke_and_conformance"
    ]
    required_measured_report_fields: tuple[NonEmptyStr, ...]
    measured_gate_pass_auto_promotes_operation: Literal[False]
    production_transition: Literal[
        "proof_of_concept_to_implementation_candidate_plus_exact_production_admission"
    ]
    exact_production_user_decision_record_fields: tuple[NonEmptyStr, ...]
    n_proof_requires_parent_n_rubric_production_admission: Literal[True]
    n_proof_cap_may_not_exceed_parent_cap: Literal[True]
    current_production_eligible_negative_operation_ids: tuple[OperationId, ...]
    current_freeze_permits_production_negative_count: Literal[0]

    @model_validator(mode="after")
    def _exact_promotion_contract(self) -> NegativeOperationPromotionContract:
        if self.applies_to_evidence_classes != (
            EvidenceClass.N_RUBRIC,
            EvidenceClass.N_PROOF,
        ):
            raise ValueError("negative promotion must cover exactly N-RUBRIC and N-PROOF")
        if self.required_gate_sequence != (
            "one_positive_one_negative_end_to_end_smoke",
            "selected_wave_operation_conformance_matrix",
            "approximately_100_roots_per_selected_operation",
            "exact_post_report_production_user_decision",
        ):
            raise ValueError("negative promotion gate sequence differs from revision 0.3.1")
        if self.required_measured_report_fields != EXPECTED_NEGATIVE_PROMOTION_MEASUREMENTS:
            raise ValueError("negative promotion measurements differ from revision 0.3.1")
        if (
            self.exact_production_user_decision_record_fields
            != EXPECTED_NEGATIVE_PRODUCTION_DECISION_RECORD_FIELDS
        ):
            raise ValueError("negative production decision record differs from revision 0.3.1")
        if self.current_production_eligible_negative_operation_ids:
            raise ValueError("current freeze permits zero production negatives")
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
    negative_operation_promotion: NegativeOperationPromotionContract
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


class RowEvidenceContract(StrictModel):
    ordered_axes: tuple[NonEmptyStr, ...]
    label_derivation_axis: Literal["f1_claim_relation_certificate"]
    retained_requires_reference_and_candidate_closed_prop_validation: Literal[True]
    retained_requires_f1_certificate: Literal[True]
    f0_may_create_or_override_f1_label: Literal[False]
    candidate_truth_may_create_or_override_f1_label: Literal[False]
    optional_f2_may_create_or_override_f1_label: Literal[False]
    n_rubric_candidate_truth_values: tuple[CandidateTruth, ...]
    n_rubric_requires_f2_direction_evidence: Literal[False]
    n_rubric_logical_nonequivalence_or_refutation_required: Literal[False]
    n_proof_requires_source_proof_and_candidate_refutation: Literal[True]
    unknown_or_uncertified_rows_enter_labeled_training_view: Literal[False]
    complete_receipt_persisted_in_sidecar: Literal[True]

    @model_validator(mode="after")
    def _exact_axes_and_truth(self) -> RowEvidenceContract:
        if self.ordered_axes != EXPECTED_ROW_EVIDENCE_AXES:
            raise ValueError("row evidence axes differ from revision 0.3.1")
        if self.n_rubric_candidate_truth_values != (
            CandidateTruth.PROVED,
            CandidateTruth.REFUTED,
            CandidateTruth.UNKNOWN,
        ):
            raise ValueError("N-RUBRIC candidate truth domain must remain proved/refuted/unknown")
        return self


class RowEvidenceReceipt(StrictModel):
    """Typed retained/drop receipt; every terminal path preserves axis coherence."""

    operation_id: OperationId
    evidence_class: EvidenceClass
    certificate_class_id: SymbolicId
    reference_closed_prop_validation: ClosedPropValidation
    candidate_closed_prop_validation: ClosedPropValidation
    f0_definitional_relation: F0Relation
    f1_claim_relation_certificate: F1ClaimRelation
    f1_certificate_payload_hash: Sha256 | None
    candidate_truth_evidence: CandidateTruth
    candidate_truth_evidence_payload_hash: Sha256
    optional_f2_direction_evidence: F2DirectionEvidence | None
    optional_f2_direction_evidence_payload_hash: Sha256 | None
    source_proof_hash: Sha256 | None
    candidate_refutation_hash: Sha256 | None
    final_retain_or_drop_disposition: RetainDisposition
    terminal_disposition_reason: TerminalDispositionReason

    @model_validator(mode="after")
    def _receipt_is_label_safe(self) -> RowEvidenceReceipt:
        matching = [
            (evidence_class, certificate_class_id)
            for evidence_class, certificate_class_id, operation_ids in (
                EXPECTED_CERTIFICATE_CLASS_BINDINGS
            )
            if self.operation_id in operation_ids
        ]
        if matching != [(self.evidence_class.value, self.certificate_class_id)]:
            raise ValueError("operation, evidence class, and certificate class do not reconcile")
        if (self.optional_f2_direction_evidence is None) != (
            self.optional_f2_direction_evidence_payload_hash is None
        ):
            raise ValueError("optional F2 direction and its payload hash must appear together")
        if self.reference_closed_prop_validation != ClosedPropValidation.PASSED:
            raise ValueError("row evidence receipt requires a valid reference closed Prop")
        if (self.final_retain_or_drop_disposition == RetainDisposition.RETAIN) != (
            self.terminal_disposition_reason == TerminalDispositionReason.RETAINED
        ):
            raise ValueError("retain/drop disposition and terminal reason do not reconcile")

        candidate_invalid = (
            self.terminal_disposition_reason
            == TerminalDispositionReason.CANDIDATE_CLOSED_PROP_INVALID
        )
        if candidate_invalid != (
            self.candidate_closed_prop_validation == ClosedPropValidation.FAILED
        ):
            raise ValueError(
                "candidate validation axis must match candidate-closed-Prop terminal reason"
            )

        f1_certified = self.f1_claim_relation_certificate != F1ClaimRelation.UNCERTIFIED
        if f1_certified != (self.f1_certificate_payload_hash is not None):
            raise ValueError("F1 certificate direction and payload hash must appear together")
        positive = self.evidence_class in {
            EvidenceClass.P_DEF,
            EvidenceClass.P_SCHEMA,
            EvidenceClass.P_LEMMA,
            EvidenceClass.P_REFLECT,
        }
        if (
            f1_certified
            and positive
            and (self.f1_claim_relation_certificate != F1ClaimRelation.PRESERVING)
        ):
            raise ValueError("positive evidence class requires a preserving F1 certificate")
        if (
            f1_certified
            and not positive
            and (self.f1_claim_relation_certificate != F1ClaimRelation.BREAKING)
        ):
            raise ValueError("negative evidence class requires a breaking F1 certificate")
        if (
            self.evidence_class == EvidenceClass.P_DEF
            and f1_certified
            and self.f0_definitional_relation != F0Relation.DEFINITIONALLY_EQUAL
        ):
            raise ValueError("certified P-DEF receipt requires definitional equality")
        if (
            f1_certified
            and not positive
            and self.f0_definitional_relation == F0Relation.DEFINITIONALLY_EQUAL
        ):
            raise ValueError(
                "certified negative mutation cannot have definitionally equal endpoints"
            )

        proof_fields_present = (
            self.source_proof_hash is not None,
            self.candidate_refutation_hash is not None,
        )
        if self.evidence_class == EvidenceClass.N_PROOF:
            if proof_fields_present[0] != proof_fields_present[1]:
                raise ValueError("N-PROOF proof and refutation fields must appear together")
            if f1_certified and (
                self.candidate_truth_evidence != CandidateTruth.REFUTED
                or not all(proof_fields_present)
            ):
                raise ValueError(
                    "certified N-PROOF receipt requires source proof and candidate refutation"
                )
            if not f1_certified and any(proof_fields_present):
                raise ValueError("uncertified N-PROOF receipt cannot carry proof/refutation fields")
        elif any(proof_fields_present):
            raise ValueError("proof/refutation receipt fields belong only to N-PROOF")

        uncertified_terminal_reasons = {
            TerminalDispositionReason.CANDIDATE_CLOSED_PROP_INVALID,
            TerminalDispositionReason.NO_OP_DROPPED,
            TerminalDispositionReason.CANCELLATION_DROPPED,
            TerminalDispositionReason.BLOCKLIST_DROPPED,
            TerminalDispositionReason.F1_RELATION_UNCERTIFIED,
            TerminalDispositionReason.VACUITY_REJECTED,
            TerminalDispositionReason.EMPTY_DOMAIN_REJECTED,
        }
        certified_terminal_reasons = {
            TerminalDispositionReason.WITHIN_ROOT_DUPLICATE_DROPPED,
            TerminalDispositionReason.CROSS_ROOT_DUPLICATE_DROPPED,
            TerminalDispositionReason.SPLIT_CLUSTER_COLLISION_DROPPED,
            TerminalDispositionReason.RETAINED,
        }
        if self.terminal_disposition_reason in uncertified_terminal_reasons and f1_certified:
            raise ValueError("terminal reason requires the F1 axis to remain uncertified")
        if self.terminal_disposition_reason in certified_terminal_reasons and not f1_certified:
            raise ValueError("terminal reason requires a completed F1 certificate")
        if (
            self.terminal_disposition_reason
            in {
                TerminalDispositionReason.NO_OP_DROPPED,
                TerminalDispositionReason.CANCELLATION_DROPPED,
            }
            and self.f0_definitional_relation != F0Relation.DEFINITIONALLY_EQUAL
        ):
            raise ValueError("no-op/cancellation terminal reason requires definitional equality")
        if candidate_invalid and (
            self.f0_definitional_relation != F0Relation.UNKNOWN
            or self.candidate_truth_evidence != CandidateTruth.UNKNOWN
            or self.optional_f2_direction_evidence is not None
        ):
            raise ValueError("invalid candidate terminal reason forbids downstream axis evidence")
        if self.terminal_disposition_reason == TerminalDispositionReason.BLOCKLIST_DROPPED and (
            self.candidate_truth_evidence != CandidateTruth.UNKNOWN
            or self.optional_f2_direction_evidence is not None
        ):
            raise ValueError(
                "pre-F1 blocklist terminal reason forbids downstream truth/F2 evidence"
            )
        if self.final_retain_or_drop_disposition == RetainDisposition.RETAIN and (
            self.candidate_closed_prop_validation != ClosedPropValidation.PASSED or not f1_certified
        ):
            raise ValueError("retained row requires a valid candidate and an F1 certificate")
        return self


class CertificateClassBinding(StrictModel):
    evidence_class: EvidenceClass
    certificate_class_id: SymbolicId
    operation_ids: tuple[OperationId, ...]
    checker_binding_required_for_gate_admitted_operation_before_one_example_gate: Literal[True]
    certificate_payload_hash_required: Literal[True]
    persistent_meta_replay_required_for_retained_row: Literal[True]
    establishes_f1_relation: Literal[True]
    candidate_truth_axis_is_separate_from_f1_label: Literal[True]
    requires_source_proof_and_candidate_refutation: bool = Field(strict=True)


class CertificateClassContract(StrictModel):
    bindings: tuple[CertificateClassBinding, ...]
    exact_partition_of_registered_operations: Literal[True]
    operation_may_have_multiple_certificate_classes: Literal[False]
    unresolved_checker_binding_blocks_execution: Literal[True]

    @model_validator(mode="after")
    def _exact_partition(self) -> CertificateClassContract:
        observed = tuple(
            (
                binding.evidence_class.value,
                binding.certificate_class_id,
                binding.operation_ids,
            )
            for binding in self.bindings
        )
        if observed != EXPECTED_CERTIFICATE_CLASS_BINDINGS:
            raise ValueError("certificate-class bindings differ from the exact 46-operation map")
        flattened = tuple(
            operation_id for binding in self.bindings for operation_id in binding.operation_ids
        )
        if set(flattened) != set(EXPECTED_ALL_OPERATION_IDS) or len(flattened) != len(
            EXPECTED_ALL_OPERATION_IDS
        ):
            raise ValueError("certificate-class bindings must partition all operations once")
        if any(
            binding.requires_source_proof_and_candidate_refutation
            != (binding.evidence_class == EvidenceClass.N_PROOF)
            for binding in self.bindings
        ):
            raise ValueError("only N-PROOF certificate classes require proof plus refutation")
        return self


class NegativeFamilyDimensionAdmission(StrictModel):
    admission_id: SymbolicId
    family_id: FamilyId
    rubric_dimension: SymbolicId
    track: OperationTrack
    operation_ids: tuple[OperationId, ...]
    gate_admitted: Literal[False]
    production_admitted: Literal[False]

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
    resolved_hashes_required_only_for_gate_admitted_current_wave_operations: Literal[True]
    resolved_manifest_hash: None


class AdversarialFixtureFreeze(StrictModel):
    status: Literal["pending"]
    operation_fixture_ids_are_design_ids_only: Literal[True]
    per_gate_admitted_operation_and_eligible_project_specs_required: Literal[True]
    success_and_expected_rejection_code_required: Literal[True]
    selected_wave_fixture_bundle_hash_required_before_conformance_gate: Literal[True]
    fixture_bundle_hash: None


class ProjectFixtureBinding(StrictModel):
    project_id: Literal["compiler_data", "cslib", "mathlib", "physlib"]
    success_fixture_id: SymbolicId
    adversarial_rejection_fixture_id: SymbolicId
    fixture_bundle_sha256: Sha256


class ResolvedOperationExecutionBinding(StrictModel):
    operation_id: OperationId
    dispatch_symbol: NonEmptyStr
    implementation_path: NonEmptyStr
    implementation_file_sha256: Sha256
    resolved_anchor_hash: Sha256
    certificate_checker_id: SymbolicId
    certificate_checker_path: NonEmptyStr
    certificate_checker_file_sha256: Sha256
    bank_or_template_id: SymbolicId
    bank_or_template_resolved_hash: Sha256
    eligible_project_fixtures: tuple[ProjectFixtureBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _project_fixtures_unique(self) -> ResolvedOperationExecutionBinding:
        project_ids = tuple(item.project_id for item in self.eligible_project_fixtures)
        if project_ids != tuple(sorted(set(project_ids))):
            raise ValueError("operation project fixtures must be sorted and unique")
        return self


class OperationExecutionBindingRegistry(StrictModel):
    status: Literal[
        "current_wave_not_admitted_fail_closed",
        "current_wave_admitted_bindings_incomplete_fail_closed",
        "current_wave_bindings_resolved",
    ]
    current_wave_id: Literal["sft1_wave_1_proposal_v1"]
    binding_requirement_scope: Literal["gate_admitted_current_wave_operations_only"]
    proposed_current_wave_operation_ids: tuple[OperationId, ...]
    gate_admitted_operation_ids: tuple[OperationId, ...]
    unresolved_gate_admitted_operation_ids: tuple[OperationId, ...]
    unselected_or_unadmitted_operation_ids: tuple[OperationId, ...]
    resolved_bindings: tuple[ResolvedOperationExecutionBinding, ...]
    admitted_operation_requires_exact_dispatch_and_file_hash: Literal[True]
    admitted_operation_requires_resolved_anchor_and_checker_hash: Literal[True]
    admitted_operation_requires_bank_or_template_hash: Literal[True]
    admitted_operation_every_eligible_project_requires_fixture_hashes: Literal[True]
    unselected_operations_remain_fail_closed: Literal[True]
    unselected_operations_block_current_wave_readiness: Literal[False]
    execution_prerequisites_satisfied: bool = Field(strict=True)

    @model_validator(mode="after")
    def _wave_scoped_bindings(self) -> OperationExecutionBindingRegistry:
        if self.proposed_current_wave_operation_ids != EXPECTED_CURRENT_WAVE_OPERATION_IDS:
            raise ValueError("execution registry current-wave proposal differs from revision 0.3.1")
        admitted = self.gate_admitted_operation_ids
        if len(admitted) != len(set(admitted)) or not set(admitted) <= set(
            self.proposed_current_wave_operation_ids
        ):
            raise ValueError("gate-admitted operations must be unique members of the current wave")
        resolved_ids = tuple(binding.operation_id for binding in self.resolved_bindings)
        if len(resolved_ids) != len(set(resolved_ids)) or not set(resolved_ids) <= set(admitted):
            raise ValueError("resolved execution bindings must be unique and gate-admitted")
        expected_unresolved = tuple(
            operation_id for operation_id in admitted if operation_id not in resolved_ids
        )
        if self.unresolved_gate_admitted_operation_ids != expected_unresolved:
            raise ValueError("unresolved binding inventory must equal admitted minus resolved")
        expected_unselected = tuple(
            operation_id
            for operation_id in EXPECTED_ALL_OPERATION_IDS
            if operation_id not in admitted
        )
        if self.unselected_or_unadmitted_operation_ids != expected_unselected:
            raise ValueError(
                "unselected/unadmitted inventory must be the exact fail-closed complement"
            )
        expected_status = (
            "current_wave_not_admitted_fail_closed"
            if not admitted
            else (
                "current_wave_admitted_bindings_incomplete_fail_closed"
                if expected_unresolved
                else "current_wave_bindings_resolved"
            )
        )
        if self.status != expected_status:
            raise ValueError("execution-binding status disagrees with current-wave binding state")
        if self.execution_prerequisites_satisfied != (
            expected_status == "current_wave_bindings_resolved"
        ):
            raise ValueError("execution readiness requires every admitted current-wave binding")
        return self


class BinderElaborationProfile(StrictModel):
    profile_id: Literal["all_operations_full_binder_elaboration_v1"]
    operation_ids: tuple[OperationId, ...]
    required_checks: tuple[NonEmptyStr, ...]
    per_operation_check_result_domain: tuple[Literal["passed", "typed_not_applicable"], ...]
    typed_not_applicable_requires_reason_code: Literal[True]
    any_failed_or_unrecorded_check_blocks_candidate: Literal[True]
    complete_profile_receipt_required_before_retention: Literal[True]

    @model_validator(mode="after")
    def _exact_profile(self) -> BinderElaborationProfile:
        if self.operation_ids != EXPECTED_ALL_OPERATION_IDS:
            raise ValueError("binder profile must cover all operations in canonical order")
        if self.required_checks != EXPECTED_BINDER_ELABORATION_CHECKS:
            raise ValueError("binder elaboration checks differ from revision 0.3.1")
        if self.per_operation_check_result_domain != ("passed", "typed_not_applicable"):
            raise ValueError("binder check results must be passed or typed_not_applicable")
        return self


class EmptyDomainProfile(StrictModel):
    disposition: EmptyDomainDisposition
    operation_ids: tuple[OperationId, ...]
    exact_domain_evidence_required_when_applicable: Literal[True]


class EmptyDomainContract(StrictModel):
    profiles: tuple[EmptyDomainProfile, ...]
    exact_partition_of_registered_operations: Literal[True]
    empty_domain_status_recorded_per_candidate: Literal[True]
    unclassified_operation_blocks_execution: Literal[True]

    @model_validator(mode="after")
    def _exact_profiles(self) -> EmptyDomainContract:
        if len(self.profiles) != 4:
            raise ValueError("empty-domain contract requires four exact dispositions")
        observed = tuple(
            (profile.disposition.value, profile.operation_ids) for profile in self.profiles[:3]
        )
        if observed != EXPECTED_EMPTY_DOMAIN_PROFILES:
            raise ValueError("empty-domain special dispositions differ from revision 0.3.1")
        final = self.profiles[3]
        if final.disposition != EmptyDomainDisposition.NOT_APPLICABLE:
            raise ValueError("fourth empty-domain disposition must be not-applicable")
        flattened = tuple(
            operation_id for profile in self.profiles for operation_id in profile.operation_ids
        )
        if set(flattened) != set(EXPECTED_ALL_OPERATION_IDS) or len(flattened) != len(
            EXPECTED_ALL_OPERATION_IDS
        ):
            raise ValueError("empty-domain profiles must partition all operations once")
        specially_classified = {
            item for _, operation_ids in EXPECTED_EMPTY_DOMAIN_PROFILES for item in operation_ids
        }
        expected_not_applicable = tuple(
            operation_id
            for operation_id in EXPECTED_ALL_OPERATION_IDS
            if operation_id not in specially_classified
        )
        if final.operation_ids != expected_not_applicable:
            raise ValueError("empty-domain not-applicable inventory differs from revision 0.3.1")
        return self


class ClosedApplicabilityBank(StrictModel):
    bank_id: SymbolicId
    operation_ids: tuple[OperationId, ...]
    status: Literal["closed_design_implementation_unresolved"]
    admitted_typed_shapes: tuple[SymbolicId, ...] = Field(min_length=1)
    required_guards: tuple[SymbolicId, ...] = Field(min_length=1)
    excluded_shapes: tuple[SymbolicId, ...] = Field(min_length=1)
    implementation_resolved: Literal[False]
    bank_hash_basis: Literal["sha256_canonical_closed_applicability_bank_v1"]
    bank_hash: Sha256

    @model_validator(mode="after")
    def _closed_bank_hash(self) -> ClosedApplicabilityBank:
        for values in (self.admitted_typed_shapes, self.required_guards, self.excluded_shapes):
            if len(values) != len(set(values)):
                raise ValueError("closed applicability bank entries must be unique")
        payload = self.model_dump(mode="json")
        payload.pop("bank_hash")
        if self.bank_hash != hash_canonical(payload):
            raise ValueError(f"closed applicability bank hash mismatch for {self.bank_id}")
        return self


class NegativeApplicabilityBankContract(StrictModel):
    banks: tuple[ClosedApplicabilityBank, ...]
    exact_operation_bank_binding_required: Literal[True]
    generic_pattern_fallback_allowed: Literal[False]
    bank_resolution_required_for_selected_negative_before_one_example_gate: Literal[True]

    @model_validator(mode="after")
    def _exact_banks(self) -> NegativeApplicabilityBankContract:
        observed = tuple((bank.bank_id, bank.operation_ids) for bank in self.banks)
        if observed != EXPECTED_NEGATIVE_APPLICABILITY_BANKS:
            raise ValueError("N26/N31/N32 closed applicability banks differ from revision 0.3.1")
        return self


class OperationAccountingGroup(StrictModel):
    group_id: SymbolicId
    operation_ids: tuple[OperationId, ...] = Field(min_length=1)


class OperationAccountingContract(StrictModel):
    correlation_group_key_fields: tuple[NonEmptyStr, ...]
    effective_diversity_key_fields: tuple[NonEmptyStr, ...]
    keys_derived_only_from_frozen_registry_and_receipt: Literal[True]
    exact_operation_id_always_retained_as_accounting_dimension: Literal[True]
    correlation_groups: tuple[OperationAccountingGroup, ...]
    effective_diversity_groups: tuple[OperationAccountingGroup, ...]
    gate_eligible_statuses_after_prerequisites: tuple[OperationStatus, ...]
    production_eligible_statuses_after_admission: tuple[OperationStatus, ...]
    diagnostic_counted_in_production_volume_or_diversity: Literal[False]
    proof_of_concept_counted_in_production_volume_or_diversity: Literal[False]
    synthetic_track_counted_with_natural_production: Literal[False]
    unresolved_operations_gate_eligible_now: Literal[False]
    unresolved_operations_production_eligible_now: Literal[False]

    @model_validator(mode="after")
    def _exact_accounting(self) -> OperationAccountingContract:
        if self.correlation_group_key_fields != EXPECTED_CORRELATION_KEY_FIELDS:
            raise ValueError("correlation accounting keys differ from revision 0.3.1")
        if self.effective_diversity_key_fields != EXPECTED_EFFECTIVE_DIVERSITY_KEY_FIELDS:
            raise ValueError("effective-diversity keys differ from revision 0.3.1")
        if self.gate_eligible_statuses_after_prerequisites != (
            OperationStatus.IMPLEMENTATION_CANDIDATE,
            OperationStatus.DIAGNOSTIC,
            OperationStatus.PROOF_OF_CONCEPT,
        ):
            raise ValueError("gate eligibility status accounting differs from revision 0.3.1")
        if self.production_eligible_statuses_after_admission != (
            OperationStatus.IMPLEMENTATION_CANDIDATE,
        ):
            raise ValueError("production eligibility must exclude diagnostic and POC operations")
        observed_correlation = tuple(
            (group.group_id, group.operation_ids) for group in self.correlation_groups
        )
        observed_effective = tuple(
            (group.group_id, group.operation_ids) for group in self.effective_diversity_groups
        )
        if observed_correlation != EXPECTED_CORRELATION_GROUPS:
            raise ValueError("operation correlation groups differ from revision 0.3.1")
        if observed_effective != EXPECTED_EFFECTIVE_DIVERSITY_GROUPS:
            raise ValueError("effective-diversity groups differ from revision 0.3.1")
        for name, groups in (
            ("correlation", self.correlation_groups),
            ("effective diversity", self.effective_diversity_groups),
        ):
            flattened = tuple(item for group in groups for item in group.operation_ids)
            if set(flattened) != set(EXPECTED_ALL_OPERATION_IDS) or len(flattened) != len(
                EXPECTED_ALL_OPERATION_IDS
            ):
                raise ValueError(f"{name} groups must partition all operations exactly once")
        return self


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


class ForbiddenRenderedSubstringFailureMapping(StrictModel):
    substring: NonEmptyStr
    exact_failure_class: NonEmptyStr


class RepresentationPreGateAcceptance(StrictModel):
    status: Literal["passed"]
    required_before_one_example_gate: Literal[True]
    fixed_engine_reference_elaboration_source_path: Literal[
        "LeanFaith/Meta/SFT1/RepresentationGate.lean"
    ]
    fixed_engine_reference_elaboration_source_file_sha256: Sha256
    fixed_engine_reference_elaboration_preamble_hash: Sha256
    fixed_engine_reference_elaboration_import_strip_policy: Literal[
        "remove_lines_whose_first_token_is_import_v1"
    ]
    fixed_engine_reference_elaboration_preamble_reviewed: Literal[True]
    fixed_engine_reference_elaboration_preamble_review_status: Literal[
        "reviewed_for_bounded_six_goal_gate"
    ]
    fixed_engine_reference_elaboration_preamble_required_before_six_goal_gate: Literal[True]
    six_goal_gate_id: Literal["sft1_repr_six_real_goal_direct_expr_v0_3_1"]
    six_goal_gate_status: Literal["passed"]
    six_goal_gate_config_path: Literal[
        "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_v0_3_1.yaml"
    ]
    six_goal_gate_config_file_sha256: Sha256
    six_goal_gate_effective_config_hash: Sha256
    six_goal_execution_config_path: Literal[
        "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_execution_v0_3_1.yaml"
    ]
    six_goal_execution_config_file_sha256: Sha256
    six_goal_execution_config_hash: Sha256
    six_goal_receipt_path: Literal[
        "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_receipt_v0_3_1.json"
    ]
    six_goal_receipt_file_sha256: Sha256
    six_goal_receipt_hash: Sha256
    reference_render_succeeds_through_shared_api: Literal[True]
    candidate_render_succeeds_through_shared_api: Literal[True]
    required_distinct_render_is_distinct: Literal[True]
    exact_turnstile_count: Literal[1]
    expr_mvars_allowed: Literal[False]
    universe_mvars_allowed: Literal[False]
    free_variables_allowed: Literal[False]
    loose_bound_variables_allowed: Literal[False]
    anonymous_binder_names_allowed: Literal[False]
    anonymous_binder_rejection_scope: Literal["unsupported_anonymous_outer_pi_locals"]
    preserves_nondependent_explicit_structural_arrows: Literal[True]
    forbidden_render_substrings: tuple[NonEmptyStr, ...]
    forbidden_render_failure_mappings: tuple[ForbiddenRenderedSubstringFailureMapping, ...]
    type_inference_must_succeed_before_render: Literal[True]
    repr_real_goal_coverage_regression_must_pass: Literal[True]
    failure_reporting_dimensions: tuple[NonEmptyStr, ...]
    exact_failure_classes: tuple[NonEmptyStr, ...]
    stable_id_and_sidecar_bindings: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def _exact_reporting_contract(self) -> RepresentationPreGateAcceptance:
        exact_hashes = {
            "fixed_engine_reference_elaboration_source_file_sha256": (
                EXPECTED_SIX_GOAL_HELPER_FILE_SHA256
            ),
            "fixed_engine_reference_elaboration_preamble_hash": (
                EXPECTED_SIX_GOAL_HELPER_PREAMBLE_SHA256
            ),
            "six_goal_gate_config_file_sha256": EXPECTED_SIX_GOAL_GATE_CONFIG_FILE_SHA256,
            "six_goal_gate_effective_config_hash": EXPECTED_SIX_GOAL_GATE_EFFECTIVE_CONFIG_HASH,
            "six_goal_execution_config_file_sha256": (
                EXPECTED_SIX_GOAL_EXECUTION_CONFIG_FILE_SHA256
            ),
            "six_goal_execution_config_hash": EXPECTED_SIX_GOAL_EXECUTION_CONFIG_HASH,
            "six_goal_receipt_file_sha256": EXPECTED_SIX_GOAL_RECEIPT_FILE_SHA256,
            "six_goal_receipt_hash": EXPECTED_SIX_GOAL_RECEIPT_HASH,
        }
        for field, expected in exact_hashes.items():
            if getattr(self, field) != expected:
                raise ValueError(f"SFT1 six-goal pre-gate binding differs at {field}")
        if self.forbidden_render_substrings != EXPECTED_PRE_GATE_FORBIDDEN_RENDER_SUBSTRINGS:
            raise ValueError("pre-gate forbidden render substrings differ from revision 0.3.1")
        observed_mappings = tuple(
            (mapping.substring, mapping.exact_failure_class)
            for mapping in self.forbidden_render_failure_mappings
        )
        if observed_mappings != EXPECTED_FORBIDDEN_RENDER_FAILURE_MAPPINGS:
            raise ValueError("forbidden render failure mappings differ from revision 0.3.1")
        if self.failure_reporting_dimensions != EXPECTED_PRE_GATE_FAILURE_DIMENSIONS:
            raise ValueError("pre-gate failure dimensions differ from revision 0.3.1")
        if self.exact_failure_classes != EXPECTED_PRE_GATE_FAILURE_CLASSES:
            raise ValueError("pre-gate failure classes differ from revision 0.3.1")
        if self.stable_id_and_sidecar_bindings != EXPECTED_PRE_GATE_SIDECAR_BINDINGS:
            raise ValueError("pre-gate sidecar bindings differ from revision 0.3.1")
        return self

    def classify_forbidden_rendered_residue(self, rendered: str) -> tuple[str, ...]:
        """Return exact failure classes for every forbidden rendered residue present."""

        return tuple(
            mapping.exact_failure_class
            for mapping in self.forbidden_render_failure_mappings
            if mapping.substring in rendered
        )


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
    gate_admitted: Literal[False]
    production_admitted: Literal[False]


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
            raise ValueError("definitional mutual-exclusion members differ from revision 0.3.1")
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
            raise ValueError("revision 0.3.1 has no direct-only operation IDs")
        if len(self.mutual_exclusion_groups) != 1:
            raise ValueError("revision 0.3.1 requires the exact definitional exclusion group")
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
    required_environment_fingerprint_fields: tuple[NonEmptyStr, ...]
    environment_fingerprint_hash_basis: Literal["sha256_canonical_persistent_meta_environment_v1"]
    environment_fingerprint_required_per_persistent_request: Literal[True]
    environment_fingerprint_persisted_in_every_candidate_sidecar: Literal[True]
    environment_fingerprint_in_cache_key: Literal[True]
    retry_only_infrastructure_failures: Literal[True]
    machine_wide_worker_limit: Literal[2]
    machine_wide_lean_rss_gib_limit: Literal[40]

    @model_validator(mode="after")
    def _complete_replay(self) -> ExecutionContract:
        if self.retained_certificate_replay_fraction != 1.0:
            raise ValueError("every retained certificate must be replayed")
        if self.prevalidation_candidate_sampling_hash_fields != EXPECTED_PREVALIDATION_HASH_FIELDS:
            raise ValueError("prevalidation sampling hash fields differ from revision 0.3.1")
        if self.required_environment_fingerprint_fields != EXPECTED_ENVIRONMENT_FINGERPRINT_FIELDS:
            raise ValueError("environment fingerprint fields differ from revision 0.3.1")
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
            raise ValueError("balanced-accuracy thresholds differ from revision 0.3.1")
        if self.confidence_level != 0.95:
            raise ValueError("balanced-accuracy confidence level must be exactly 0.95")
        if self.stable_row_hash_fields != EXPECTED_STABLE_ROW_HASH_FIELDS:
            raise ValueError("stable-row hash fields differ from revision 0.3.1")
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
            raise ValueError("cap maxima differ from the exact revision 0.3.1 contract")
        return self


class OneExampleGate(StrictModel):
    user_scope_authorized: Literal[True]
    requires_current_wave_gate_admission: Literal[True]
    current_wave_gate_admitted: Literal[False]
    execution_ready: Literal[False]
    execution_started: Literal[False]
    requires_repr_expr_renderer_dependency: Literal[True]
    requires_shared_contract_update: Literal[True]
    requires_representation_pre_gate_acceptance: Literal[True]
    requires_zero_lean_census_and_source_eligibility: Literal[True]
    requires_selected_operation_execution_binding: Literal[True]
    requires_selected_operation_specific_regressions: Literal[True]
    actual_serialized_positive_example_count: Literal[1]
    actual_serialized_negative_example_count: Literal[1]
    positive_operation_id: Literal["P01_ALPHA_RENAME_SINGLE_V1"]
    negative_operation_id: Literal["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"]
    roots_chosen_from_completed_census: Literal[True]
    deterministic_root_selection_rule: Literal[
        "minimum_stable_eligible_root_hash_per_bound_smoke_operation"
    ]
    complete_sidecar_required: Literal[True]
    manifest_link_required: Literal[True]
    cache_replay_required: Literal[True]
    duplicate_suppression_replay_required: Literal[True]
    end_to_end_model_facing_projection_required: Literal[True]
    gate_artifacts_are_production_rows: Literal[False]
    retained_certificate_replay_fraction: Fraction

    @model_validator(mode="after")
    def _complete_replay(self) -> OneExampleGate:
        if self.retained_certificate_replay_fraction != 1.0:
            raise ValueError("one-example gate requires complete certificate replay")
        return self


class SelectedWaveOperationConformanceGate(StrictModel):
    user_scope_authorized: Literal[True]
    requires_current_wave_gate_admission: Literal[True]
    current_wave_gate_admitted: Literal[False]
    execution_ready: Literal[False]
    execution_started: Literal[False]
    requires_one_example_gate_pass: Literal[True]
    scope: Literal["gate_admitted_current_wave_operations_and_registered_eligible_projects"]
    success_per_operation_and_eligible_project: Literal[1]
    adversarial_rejection_per_operation_and_eligible_project: Literal[1]
    zero_yield_waiver_allowed: Literal[False]
    census_backed_inapplicable_project_requires_policy_revision: Literal[True]
    full_46_operation_matrix_required: Literal[False]


class HundredRootGate(StrictModel):
    user_scope_authorized: Literal[True]
    requires_current_wave_gate_admission: Literal[True]
    current_wave_gate_admitted: Literal[False]
    execution_ready: Literal[False]
    execution_started: Literal[False]
    requires_selected_wave_conformance_gate_pass: Literal[True]
    scope: Literal["gate_admitted_current_wave_operations"]
    eligible_roots_per_operation_approximately: Literal[100]
    retained_certificate_replay_fraction: Fraction
    persistent_meta_required: Literal[True]
    every_retained_row_typed_meta_validated_and_replayed: Literal[True]
    exact_counter_fields: tuple[NonEmptyStr, ...]
    exact_counter_dimensions: tuple[NonEmptyStr, ...]
    counters_derived_from_durable_receipts: Literal[True]
    per_dimension_totals_must_equal_global_totals: Literal[True]
    candidate_terminal_disposition_counter_fields: tuple[NonEmptyStr, ...]
    exactly_one_terminal_disposition_per_generated_candidate: Literal[True]
    terminal_disposition_counters_mutually_exclusive: Literal[True]
    conservation_equations: tuple[NonEmptyStr, ...]
    conservation_equations_must_hold_before_receipt_freeze: Literal[True]

    @model_validator(mode="after")
    def _complete_replay(self) -> HundredRootGate:
        if self.retained_certificate_replay_fraction != 1.0:
            raise ValueError("hundred-root gate requires complete certificate replay")
        if self.exact_counter_fields != EXPECTED_HUNDRED_ROOT_COUNTER_FIELDS:
            raise ValueError("hundred-root counters differ from revision 0.3.1")
        if self.exact_counter_dimensions != EXPECTED_HUNDRED_ROOT_COUNTER_DIMENSIONS:
            raise ValueError("hundred-root dimensions differ from revision 0.3.1")
        if (
            self.candidate_terminal_disposition_counter_fields
            != EXPECTED_CANDIDATE_TERMINAL_DISPOSITION_COUNTERS
        ):
            raise ValueError("candidate terminal-disposition counters differ from revision 0.3.1")
        if self.conservation_equations != EXPECTED_HUNDRED_ROOT_CONSERVATION_EQUATIONS:
            raise ValueError("hundred-root conservation equations differ from revision 0.3.1")
        return self


class SurfaceDiagnosticContract(StrictModel):
    scope: Literal["model_facing_reference_and_candidate_core_text_only"]
    sidecar_or_certificate_fields_visible_to_diagnostic: Literal[False]
    forbidden_feature_names: tuple[NonEmptyStr, ...]
    explicit_transform_marker_rate_maximum: Fraction
    explicit_transform_marker_violation_blocks_gate: Literal[True]
    shallow_operation_recoverability_reported: Literal[True]
    shallow_family_recoverability_reported: Literal[True]
    recoverability_is_label_evidence: Literal[False]
    recoverability_threshold_is_blocking: Literal[False]

    @model_validator(mode="after")
    def _exact_forbidden_features(self) -> SurfaceDiagnosticContract:
        if self.explicit_transform_marker_rate_maximum != 0.0:
            raise ValueError("explicit transform-marker rate maximum must be zero")
        if self.forbidden_feature_names != (
            "operation_id",
            "family_id",
            "mechanism_superclass",
            "template_or_bank_entry",
            "certificate_payload",
            "provenance_sidecar",
        ):
            raise ValueError("surface diagnostic forbidden features differ from revision 0.3.1")
        return self


class TenKGate(StrictModel):
    authorized: Literal[False]
    requires_separate_user_approval_after_hundred_root_report: Literal[True]
    requested_root_count: Literal[10000]


class ClosedGate(StrictModel):
    authorized: Literal[False]


class GateCostReference(StrictModel):
    registered_operation_count: Literal[46]
    operation_project_combinations: Literal[156]
    success_and_adversarial_fixture_count: Literal[312]
    approximate_root_count_at_hundred_per_operation: Literal[4600]
    required_before_selected_wave_progression: Literal[False]


class CurrentWaveCostReference(StrictModel):
    proposed_operation_count: Literal[6]
    operation_project_combinations: Literal[24]
    success_and_adversarial_fixture_count: Literal[48]
    approximate_root_count_at_hundred_per_operation: Literal[600]


class Gates(StrictModel):
    exact_progression: tuple[NonEmptyStr, ...]
    one_example: OneExampleGate
    selected_wave_operation_conformance: SelectedWaveOperationConformanceGate
    hundred_root: HundredRootGate
    ten_k_pilot: TenKGate
    bulk_scale: ClosedGate
    publication: ClosedGate
    full_matrix_cost_reference: GateCostReference
    current_wave_cost_reference: CurrentWaveCostReference

    @model_validator(mode="after")
    def _exact_progression(self) -> Gates:
        if self.exact_progression != (
            "one_positive_one_negative_end_to_end_smoke",
            "selected_wave_operation_conformance_matrix",
            "approximately_100_roots_per_selected_operation",
            "request_separate_10k_authorization",
        ):
            raise ValueError("gate progression differs from revision 0.3.1")
        return self


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
    policy_version: Literal["0.3.1"]
    status: Literal["awaiting_wave_1_gate_admission"]
    approval_recorded: Literal[False]
    authorization: Authorization
    dependencies: Dependencies
    representation_contract: RepresentationContract
    label_contract: LabelContract
    row_evidence_contract: RowEvidenceContract
    certificate_class_contract: CertificateClassContract
    negative_family_dimension_admissions: tuple[NegativeFamilyDimensionAdmission, ...]
    claim_erasure_guard_contract: ClaimErasureGuardContract
    axiom_profiles: tuple[AxiomProfile, ...]
    starter_banks: StarterBankBinding
    inline_anchor_resolution: InlineAnchorResolution
    adversarial_fixture_freeze: AdversarialFixtureFreeze
    operation_execution_bindings: OperationExecutionBindingRegistry
    binder_elaboration_profile: BinderElaborationProfile
    empty_domain_contract: EmptyDomainContract
    negative_applicability_banks: NegativeApplicabilityBankContract
    operation_accounting_contract: OperationAccountingContract
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
    hundred_root_surface_diagnostics: SurfaceDiagnosticContract
    scale_contract: ScaleContract

    @model_validator(mode="after")
    def _closed_policy(self) -> SFT1CompositionPolicy:
        renderer = self.dependencies.expr_renderer_api
        pre_gate = self.representation_pre_gate_acceptance
        if (
            renderer.real_goal_coverage_regression_id != pre_gate.six_goal_gate_id
            or renderer.real_goal_coverage_regression_hash != pre_gate.six_goal_receipt_hash
            or not renderer.real_goal_coverage_regression_passed
        ):
            raise ValueError("REPR dependency coverage must equal the frozen SFT1 six-goal receipt")
        operation_ids = tuple(operation.operation_id for operation in self.operations)
        if operation_ids != EXPECTED_OPERATION_IDS:
            raise ValueError("operations must equal the exact canonical revision 0.3.1 inventory")

        dispositions = tuple(
            (item.family_id, item.disposition) for item in self.family_dispositions
        )
        if dispositions != EXPECTED_FAMILY_DISPOSITIONS:
            raise ValueError("family dispositions differ from the exact revision 0.3.1 decisions")

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
        operations_by_id = {operation.operation_id: operation for operation in all_operations}
        current_wave = self.authorization.gate_admission
        execution_bindings = self.operation_execution_bindings
        if (
            current_wave.proposed_operation_ids
            != execution_bindings.proposed_current_wave_operation_ids
            or current_wave.gate_admitted_operation_ids
            != execution_bindings.gate_admitted_operation_ids
        ):
            raise ValueError("authorization and execution registry disagree on current Wave 1")
        if sum(len(operation.eligible_projects) for operation in all_operations) != (
            EXPECTED_FULL_MATRIX_OPERATION_PROJECT_COMBINATIONS
        ):
            raise ValueError("full-matrix operation-project cost differs from 156")
        proposed_wave_combinations = sum(
            len(operations_by_id[operation_id].eligible_projects)
            for operation_id in EXPECTED_CURRENT_WAVE_OPERATION_IDS
        )
        if proposed_wave_combinations != EXPECTED_CURRENT_WAVE_OPERATION_PROJECT_COMBINATIONS:
            raise ValueError("current-wave operation-project cost differs from 24")
        negative_promotion = self.label_contract.negative_operation_promotion
        promoted_negative_ids = (
            negative_promotion.current_production_eligible_negative_operation_ids
        )
        if (
            any(operation.admission.production_admitted for operation in all_operations)
            or promoted_negative_ids
        ):
            raise ValueError("current freeze permits zero production-admitted operation")
        for binding in self.certificate_class_contract.bindings:
            for operation_id in binding.operation_ids:
                if operations_by_id[operation_id].evidence_class != binding.evidence_class:
                    raise ValueError(
                        f"certificate class disagrees with operation evidence at {operation_id}"
                    )
        if any(
            operation.executable or operation.label_emission_authorized
            for operation in all_operations
        ):
            raise ValueError("unresolved policy cannot execute operations or emit labels")
        if (
            self.authorization.implementation_readiness.ready
            or self.authorization.gate_admission.gate_admitted
            or self.gates.one_example.execution_ready
            or self.gates.one_example.execution_started
            or self.gates.selected_wave_operation_conformance.execution_ready
            or self.gates.selected_wave_operation_conformance.execution_started
            or self.gates.hundred_root.execution_ready
            or self.gates.hundred_root.execution_started
        ):
            raise ValueError("bounded gate user scope does not satisfy execution prerequisites")
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
            raise ValueError("negative family/dimension admissions differ from revision 0.3.1")
        wave_negative_admissions = tuple(
            admission
            for admission in self.negative_family_dimension_admissions
            if admission.admission_id
            in self.authorization.gate_admission.proposed_negative_family_dimension_admission_ids
        )
        if len(wave_negative_admissions) != 1 or wave_negative_admissions[0].operation_ids != (
            "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
            "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        ):
            raise ValueError("Wave 1 negative operations and family/dimension admission disagree")
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

        p23 = operations_by_id["P23_CURRY_PROP_PAIR_V1"]
        if p23.orientation != "pack" or not p23.typed_applicability.startswith("pack exactly"):
            raise ValueError("P23 revision 0.3.1 is pack-only")
        n32_rubric = operations_by_id["N32_SWAP_ROLE_ORDER_RUBRIC_V1"]
        n32_proof = operations_by_id["N32_SWAP_ROLE_ORDER_PROOF_V1"]
        if (
            "same binary relation" not in n32_rubric.typed_applicability
            or "reject function-composition reordering" not in n32_rubric.context_restrictions
            or "reject_function_composition_case" not in n32_rubric.anti_degeneracy_checks
            or "relation-converse-only" not in n32_proof.typed_applicability
        ):
            raise ValueError("N32 revision 0.3.1 is restricted to relation converse mutations")
        n26_rubric = operations_by_id["N26_INCREMENT_BOUND_RUBRIC_V1"]
        if (
            "closed N26 applicability bank" not in n26_rubric.typed_applicability
            or "generic exponent" not in n26_rubric.context_restrictions
        ):
            raise ValueError("N26 must remain narrowed to its closed bounded-domain bank")
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
            raise ValueError("exact operation-registry fields differ from revision 0.3.1")
        if hash_canonical(self.root_census.model_dump(mode="json")) != EXPECTED_ROOT_CENSUS_HASH:
            raise ValueError("zero-Lean root census differs from revision 0.3.1")
        source_payload = [
            source.model_dump(mode="json") for source in self.source_eligibility_matrix
        ]
        if hash_canonical(source_payload) != EXPECTED_SOURCE_ELIGIBILITY_HASH:
            raise ValueError("source-eligibility matrix differs from revision 0.3.1")
        if hash_canonical(self.model_dump(mode="json")) != EXPECTED_POLICY_CONFIG_HASH:
            raise ValueError("SFT1 policy differs from the exact revision 0.3.1 freeze")

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
            raise ValueError("starter banks differ from the exact frozen revision 0.3.0 bank set")
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
            raise ValueError("starter-bank set differs from the exact revision 0.3.0 freeze")
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


def _verify_approved_repr_dependency(root: Path, api: ExprRendererApiDependency) -> None:
    """Hash-check the three consumable REPR source files without executing Lean."""

    bindings = (
        (
            api.replacement_config_path,
            api.replacement_config_file_sha256,
            "approved REPR config",
        ),
        (
            api.replacement_lean_renderer_path,
            api.replacement_lean_renderer_sha256,
            "approved REPR Lean renderer",
        ),
        (
            api.replacement_python_renderer_path,
            api.replacement_python_renderer_sha256,
            "approved REPR Python renderer",
        ),
    )
    for relative, expected_sha256, description in bindings:
        source_path = _repo_path(root, relative, description=description)
        observed_sha256 = hash_file(source_path)
        if observed_sha256 != expected_sha256:
            raise SFT1PolicyError(
                f"{description} file hash drift: {observed_sha256} != {expected_sha256}"
            )


def _import_stripped_six_goal_preamble(source: str) -> str:
    """Replay the reviewed gate's exact import-line stripping policy."""

    lines = [line for line in source.splitlines() if not line.lstrip().startswith("import ")]
    return "\n".join(lines).strip()


def _six_goal_gate_effective_config_hash(path: Path) -> str:
    """Replay the typed gate model's explicit nullable case defaults."""

    payload = load_yaml_mapping(path)
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise SFT1PolicyError("SFT1 six-goal gate config cases must be a list")
    for case in cases:
        if not isinstance(case, dict):
            raise SFT1PolicyError("SFT1 six-goal gate config case must be a mapping")
        typed_case: dict[str, Any] = case
        typed_case.setdefault("source_record", None)
        typed_case.setdefault("source_file_sha256", None)
        typed_case.setdefault("source_formal_statement_sha256", None)
        typed_case.setdefault("source_syntax_normalization_id", None)
        typed_case.setdefault("source_syntax_normalization_rule", None)
        typed_case.setdefault("normalized_proposition_sha256", None)
    return hash_canonical(payload)


def _verify_six_goal_pre_gate_binding(
    root: Path,
    pre_gate: RepresentationPreGateAcceptance,
) -> None:
    """Verify the reviewed helper, passed gate config, and frozen receipt."""

    helper_path = _repo_path(
        root,
        pre_gate.fixed_engine_reference_elaboration_source_path,
        description="SFT1 six-goal helper",
    )
    observed_helper_sha256 = hash_file(helper_path)
    if observed_helper_sha256 != pre_gate.fixed_engine_reference_elaboration_source_file_sha256:
        raise SFT1PolicyError(
            "SFT1 six-goal helper file hash drift: "
            f"{observed_helper_sha256} != "
            f"{pre_gate.fixed_engine_reference_elaboration_source_file_sha256}"
        )
    helper_source = helper_path.read_text(encoding="utf-8")
    preamble = _import_stripped_six_goal_preamble(helper_source)
    observed_preamble_sha256 = hashlib.sha256(preamble.encode("utf-8")).hexdigest()
    if observed_preamble_sha256 != pre_gate.fixed_engine_reference_elaboration_preamble_hash:
        raise SFT1PolicyError(
            "SFT1 six-goal import-stripped preamble hash drift: "
            f"{observed_preamble_sha256} != "
            f"{pre_gate.fixed_engine_reference_elaboration_preamble_hash}"
        )

    gate_config_path = _repo_path(
        root,
        pre_gate.six_goal_gate_config_path,
        description="SFT1 six-goal gate config",
    )
    observed_config_sha256 = hash_file(gate_config_path)
    if observed_config_sha256 != pre_gate.six_goal_gate_config_file_sha256:
        raise SFT1PolicyError(
            "SFT1 six-goal gate config file hash drift: "
            f"{observed_config_sha256} != {pre_gate.six_goal_gate_config_file_sha256}"
        )
    observed_effective_hash = _six_goal_gate_effective_config_hash(gate_config_path)
    if observed_effective_hash != pre_gate.six_goal_gate_effective_config_hash:
        raise SFT1PolicyError(
            "SFT1 six-goal gate effective config hash drift: "
            f"{observed_effective_hash} != {pre_gate.six_goal_gate_effective_config_hash}"
        )

    execution_config_path = _repo_path(
        root,
        pre_gate.six_goal_execution_config_path,
        description="SFT1 six-goal execution config",
    )
    observed_execution_sha256 = hash_file(execution_config_path)
    if observed_execution_sha256 != pre_gate.six_goal_execution_config_file_sha256:
        raise SFT1PolicyError(
            "SFT1 six-goal execution config file hash drift: "
            f"{observed_execution_sha256} != "
            f"{pre_gate.six_goal_execution_config_file_sha256}"
        )
    observed_execution_hash = _six_goal_gate_effective_config_hash(execution_config_path)
    if observed_execution_hash != pre_gate.six_goal_execution_config_hash:
        raise SFT1PolicyError(
            "SFT1 six-goal execution config effective hash drift: "
            f"{observed_execution_hash} != {pre_gate.six_goal_execution_config_hash}"
        )

    receipt_path = _repo_path(
        root,
        pre_gate.six_goal_receipt_path,
        description="SFT1 six-goal receipt",
    )
    observed_receipt_file_sha256 = hash_file(receipt_path)
    if observed_receipt_file_sha256 != pre_gate.six_goal_receipt_file_sha256:
        raise SFT1PolicyError(
            "SFT1 six-goal receipt file hash drift: "
            f"{observed_receipt_file_sha256} != {pre_gate.six_goal_receipt_file_sha256}"
        )
    receipt_text = receipt_path.read_text(encoding="utf-8")
    if "094550" in receipt_text:
        raise SFT1PolicyError("REPR ConsistencyCheck receipt cannot satisfy the SFT1 gate")
    receipt = json.loads(receipt_text)
    if not isinstance(receipt, dict) or set(receipt) != {
        "cases",
        "gate_config_file_sha256",
        "gate_config_hash",
        "helper_file_sha256",
        "helper_preamble_sha256",
        "passed",
        "receipt_hash",
        "regression_id",
        "schema_version",
    }:
        raise SFT1PolicyError("SFT1 six-goal receipt has an unexpected schema")
    observed_receipt_hash = receipt.pop("receipt_hash", None)
    if (
        observed_receipt_hash != hash_canonical(receipt)
        or observed_receipt_hash != pre_gate.six_goal_receipt_hash
    ):
        raise SFT1PolicyError("SFT1 six-goal receipt semantic hash drift")
    cases = receipt.get("cases")
    if (
        not isinstance(cases, list)
        or any(not isinstance(case, dict) for case in cases)
        or tuple(case.get("case_id") for case in cases) != EXPECTED_REAL_GOAL_CASE_IDS
    ):
        raise SFT1PolicyError("SFT1 six-goal receipt case order differs from policy")
    expected_case_fields = {
        "case_id",
        "elapsed_ms",
        "evidence_path",
        "evidence_sha256",
        "request_hash",
    }
    if any(
        set(case) != expected_case_fields
        or not isinstance(case["elapsed_ms"], int)
        or case["elapsed_ms"] <= 0
        or not isinstance(case["evidence_path"], str)
        or not case["evidence_path"]
        or not isinstance(case["evidence_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", case["evidence_sha256"]) is None
        or not isinstance(case["request_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", case["request_hash"]) is None
        for case in cases
    ):
        raise SFT1PolicyError("SFT1 six-goal receipt case evidence is malformed")
    expected_receipt_bindings = {
        "schema_version": 1,
        "regression_id": "sft1_repr_six_real_goal_direct_expr_v0_3_1",
        "passed": True,
        "gate_config_file_sha256": pre_gate.six_goal_execution_config_file_sha256,
        "gate_config_hash": pre_gate.six_goal_execution_config_hash,
        "helper_file_sha256": pre_gate.fixed_engine_reference_elaboration_source_file_sha256,
        "helper_preamble_sha256": pre_gate.fixed_engine_reference_elaboration_preamble_hash,
    }
    if any(receipt.get(field) != value for field, value in expected_receipt_bindings.items()):
        raise SFT1PolicyError("SFT1 six-goal receipt differs from executed bindings")
    try:
        replayed_gate = load_six_goal_gate(Path(pre_gate.six_goal_gate_config_path))
    except (OSError, ValueError) as exc:
        raise SFT1PolicyError(
            "SFT1 six-goal durable evidence replay failed through the typed gate loader"
        ) from exc
    if (
        replayed_gate.config_hash != pre_gate.six_goal_gate_effective_config_hash
        or replayed_gate.config_file_sha256 != pre_gate.six_goal_gate_config_file_sha256
    ):
        raise SFT1PolicyError("typed six-goal replay disagrees with the policy pre-gate binding")


def load_sft1_composition_policy(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedSFT1CompositionPolicy:
    """Load revision 0.3.1 without instantiating a backend or authorizing execution."""

    root = find_repo_root(repo_root)
    resolved_root = root.resolve()
    resolved_policy = (path or root / _DEFAULT_POLICY_PATH).resolve()
    if not resolved_policy.is_relative_to(resolved_root):
        raise SFT1PolicyError("SFT1 policy path escapes the repository")
    loaded_policy = load_config(resolved_policy, SFT1CompositionPolicy)
    if loaded_policy.config_hash != EXPECTED_POLICY_CONFIG_HASH:
        raise SFT1PolicyError("SFT1 policy canonical hash differs from revision 0.3.1")
    policy = loaded_policy.config
    _verify_approved_repr_dependency(root, policy.dependencies.expr_renderer_api)
    _verify_six_goal_pre_gate_binding(root, policy.representation_pre_gate_acceptance)

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
        raise SFT1PolicyError("starter-bank canonical hash differs from frozen revision 0.3.0")
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
    "EXACT_CURRENT_USER_DECISION",
    "EXPECTED_BANK_IDS",
    "EXPECTED_BINDER_ELABORATION_CHECKS",
    "EXPECTED_CACHE_KEY_FIELDS",
    "EXPECTED_CANDIDATE_TERMINAL_DISPOSITION_COUNTERS",
    "EXPECTED_CAP_ORDER",
    "EXPECTED_CERTIFICATE_CLASS_BINDINGS",
    "EXPECTED_CLAIM_ERASURE_GUARDS",
    "EXPECTED_COMPOSITION_PRODUCTIONS",
    "EXPECTED_CORRELATION_GROUPS",
    "EXPECTED_CURRENT_WAVE_APPROXIMATE_ROOTS",
    "EXPECTED_CURRENT_WAVE_FIXTURES",
    "EXPECTED_CURRENT_WAVE_ID",
    "EXPECTED_CURRENT_WAVE_OPERATION_IDS",
    "EXPECTED_CURRENT_WAVE_OPERATION_PROJECT_COMBINATIONS",
    "EXPECTED_CURRENT_WAVE_UNSELECTED_OPERATION_IDS",
    "EXPECTED_EFFECTIVE_DIVERSITY_GROUPS",
    "EXPECTED_EMPTY_DOMAIN_PROFILES",
    "EXPECTED_ENVIRONMENT_FINGERPRINT_FIELDS",
    "EXPECTED_FAMILY_DISPOSITIONS",
    "EXPECTED_FORBIDDEN_RENDER_FAILURE_MAPPINGS",
    "EXPECTED_FULL_MATRIX_APPROXIMATE_ROOTS",
    "EXPECTED_FULL_MATRIX_FIXTURES",
    "EXPECTED_FULL_MATRIX_OPERATION_PROJECT_COMBINATIONS",
    "EXPECTED_HUNDRED_ROOT_CONSERVATION_EQUATIONS",
    "EXPECTED_HUNDRED_ROOT_COUNTER_DIMENSIONS",
    "EXPECTED_HUNDRED_ROOT_COUNTER_FIELDS",
    "EXPECTED_MUTUAL_EXCLUSION_OPERATION_IDS",
    "EXPECTED_NEGATIVE_ADMISSIONS",
    "EXPECTED_NEGATIVE_APPLICABILITY_BANKS",
    "EXPECTED_NEGATIVE_PRODUCTION_DECISION_RECORD_FIELDS",
    "EXPECTED_NEGATIVE_PROMOTION_MEASUREMENTS",
    "EXPECTED_OPERATION_IDS",
    "EXPECTED_OPERATION_REGISTRY_HASH",
    "EXPECTED_P23_REGRESSIONS",
    "EXPECTED_P23_SIDECAR_BINDINGS",
    "EXPECTED_POLICY_CONFIG_HASH",
    "EXPECTED_PREVALIDATION_HASH_FIELDS",
    "EXPECTED_PRE_GATE_FAILURE_CLASSES",
    "EXPECTED_PRE_GATE_FAILURE_DIMENSIONS",
    "EXPECTED_PRE_GATE_FORBIDDEN_RENDER_SUBSTRINGS",
    "EXPECTED_PRE_GATE_SIDECAR_BINDINGS",
    "EXPECTED_REAL_GOAL_CASE_IDS",
    "EXPECTED_RENDERER_API_HASH_PAYLOAD_FIELDS",
    "EXPECTED_REPR_CONFIG_FILE_SHA256",
    "EXPECTED_REPR_FREEZE_COMMIT",
    "EXPECTED_REPR_IMPLEMENTATION_COMMIT",
    "EXPECTED_REPR_IMPLEMENTATION_SET_HASH",
    "EXPECTED_REPR_INJECTED_HELPER_SHA256",
    "EXPECTED_REPR_LEAN_RENDERER_SHA256",
    "EXPECTED_REPR_PYTHON_RENDERER_SHA256",
    "EXPECTED_REPR_RENDERER_API_HASH",
    "EXPECTED_REPR_RENDERER_SEMANTIC_HASH",
    "EXPECTED_REPR_RENDER_CONTEXT_HASH",
    "EXPECTED_REPR_RENDER_CONTEXT_ID",
    "EXPECTED_REPR_SPEC_HASH",
    "EXPECTED_REPR_UNIVERSE_PROFILE_HASH",
    "EXPECTED_REPR_UNIVERSE_PROFILE_ID",
    "EXPECTED_ROOT_CENSUS_HASH",
    "EXPECTED_SIX_GOAL_EXECUTION_CONFIG_FILE_SHA256",
    "EXPECTED_SIX_GOAL_EXECUTION_CONFIG_HASH",
    "EXPECTED_SIX_GOAL_EXECUTION_CONFIG_PATH",
    "EXPECTED_SIX_GOAL_GATE_CONFIG_FILE_SHA256",
    "EXPECTED_SIX_GOAL_GATE_CONFIG_PATH",
    "EXPECTED_SIX_GOAL_GATE_EFFECTIVE_CONFIG_HASH",
    "EXPECTED_SIX_GOAL_HELPER_FILE_SHA256",
    "EXPECTED_SIX_GOAL_HELPER_PREAMBLE_SHA256",
    "EXPECTED_SIX_GOAL_HELPER_SOURCE_PATH",
    "EXPECTED_SIX_GOAL_RECEIPT_FILE_SHA256",
    "EXPECTED_SIX_GOAL_RECEIPT_HASH",
    "EXPECTED_SIX_GOAL_RECEIPT_PATH",
    "EXPECTED_SOURCE_ELIGIBILITY_HASH",
    "EXPECTED_STABLE_ROW_HASH_FIELDS",
    "EXPECTED_STARTER_BANK_CONFIG_HASH",
    "EXPECTED_STARTER_BANK_FILE_SHA256",
    "EXPECTED_SYNTHETIC_OPERATION_IDS",
    "CandidateTruth",
    "ClosedPropValidation",
    "EvidenceClass",
    "F0Relation",
    "F1ClaimRelation",
    "LabelLane",
    "LoadedSFT1CompositionPolicy",
    "OperationSpec",
    "RetainDisposition",
    "RowEvidenceReceipt",
    "SFT1CompositionPolicy",
    "SFT1PolicyError",
    "SFT1StarterBankSet",
    "TerminalDispositionReason",
    "load_sft1_composition_policy",
    "validate_sft1_policy_bindings",
]
