"""Strict Lean-free loader for additive SFT1 P01 identity policy 0.3.5.

The overlay composes the frozen revision-0.3.4 readiness state without
rewriting it.  It clears only the policy-level P01 alpha-invariant identity
blocker.  It deliberately exposes no Lean, transformation, gate, row,
production, training, or publication surface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, get_args, get_origin

from pydantic import Field, StrictFloat, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.wave1_readiness import (
    LoadedWave1ImplementationReadiness,
    load_wave1_implementation_readiness,
    operation_bank_entry_hash,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitObject = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
IsoDate = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", strict=True)]
SymbolicId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]*$", strict=True)]

DEFAULT_P01_IDENTITY_POLICY_PATH = Path(
    "configs/transformations/sft1_value_first_v1/p01_identity_policy_v0_3_5.yaml"
)

EXPECTED_PARENT_COMMIT = "5ddda95d05fe4c0fcd755e042174ca50453ebd03"
EXPECTED_PARENT_TREE = "0ba6c5d2f5b3cf2a921e92e607eb89e2cbf8e0f0"
EXPECTED_USER_AUTHORIZATION_SHA256 = (
    "ee6440aa41d56b0a7dbc15d15f89ef75c629bd6361685c2b70a65d0e49e59514"
)
EXPECTED_OVERLAY_FILE_SHA256 = "ee43bbbe00dc7f1063cb9dec334bfb204bcedb3bae255841e3b70c85470c2bf3"
EXPECTED_OVERLAY_SEMANTIC_HASH = "a4aa3ddc383fdbc5fd1e161b5955f403ac17afa98f9d24defab4c2741846b4fd"

EXPECTED_BASE_POLICY_FILE_SHA256 = (
    "a052ecec4cc8f61db7438dd5acbc39373a624b155f8c0305bb75b7ae15d7195d"
)
EXPECTED_BASE_POLICY_SEMANTIC_HASH = (
    "08a6d1b2ea03f3674d06cdac44478377084af24ba5cd4af7cab57303f4e7a917"
)
EXPECTED_OPERATION_REGISTRY_HASH = (
    "d56fca674f7b58d92dca09f0b76a702c54d1df2e5b68dcbe94225cad7e5cd95f"
)
EXPECTED_PARENT_READINESS_SEMANTIC_HASH = (
    "cdf5ad5572c3887213017fe6d7c17987fedbe0eadecb47e87b41a8111911e25a"
)
EXPECTED_PARENT_OPERATION_BANK_SEMANTIC_HASH = (
    "99440883e2ae37b7ee95ca3332273107665277cb9597586c6de427b36e9ce8da"
)
EXPECTED_PARENT_FIXTURE_SEMANTIC_HASH = (
    "6d8dbc0d7da271f880223ec519afabbe030dd0d480ba7d0731707d5b66f46e51"
)
EXPECTED_PARENT_SOURCE_PREAMBLE_SHA256 = (
    "0b905f3d1d045694b3dcc1174fb069ca82f76d345b0f439936e7e61af3868009"
)

EXPECTED_FROZEN_ARTIFACTS: tuple[tuple[str, str, str], ...] = (
    (
        "wave1_lean_source_v0_3_4",
        "LeanFaith/Meta/SFT1/Wave1.lean",
        "7d4c27e1fd631cc1ba2f8de7cacec1eca618280c12c8ac351d9544a06e94ba4d",
    ),
    (
        "wave1_implementation_readiness_v0_3_4",
        "configs/transformations/sft1_value_first_v1/wave1_implementation_readiness_v0_3_4.yaml",
        "87197cef05d4e755a0d92745b2b3846787b5e1159edac29dfdd967ba81aed614",
    ),
    (
        "wave1_operation_banks_v0_3_4",
        "configs/transformations/sft1_value_first_v1/wave1_operation_banks_v0_3_4.yaml",
        "282836a539d055e227ccfba12dd612522654f036d6705aec07a551a217c82a34",
    ),
    (
        "wave1_fixture_matrix_v0_3_4",
        "tests/fixtures/sft1/wave1_v0_3_4.yaml",
        "0856c6cfa1536bd935d4606ec2d09a34c72ef5c2ddf92d2f15b67867ac6dd6ea",
    ),
    (
        "wave1_readiness_loader_v0_3_4",
        "src/leanfaith/sft1/wave1_readiness.py",
        "f59c9304f153532e96b8ef99626c77ad01bff56640d071316f9c78094c0dc56a",
    ),
    (
        "wave1_readiness_tests_v0_3_4",
        "tests/unit/sft1/test_wave1_readiness.py",
        "3a766c6a6e5e5e7d291e2796d0d8bcb0f74a0a41ed35b048f19bc0935aa07835",
    ),
)

EXPECTED_NEW_PATHS: tuple[str, ...] = (
    DEFAULT_P01_IDENTITY_POLICY_PATH.as_posix(),
    "src/leanfaith/sft1/p01_identity_policy.py",
    "tests/unit/sft1/test_p01_identity_policy.py",
)
EXPECTED_BRIEF_PATH = "plans/30_sft1_deterministic.md"
EXPECTED_BLOCKER_ID = "p01_alpha_closed_expr_hash_collision"
EXPECTED_REMAINING_IMPLEMENTATION_BLOCKERS: tuple[str, ...] = (
    "n31_guard_target_and_contradiction_bank_live_resolution",
    "n31_runtime_bank_identity_admission_requires_separate_user_authorization",
    "n31_in_process_exact_full_typed_bank_hash_verifier_binding",
    "lean_source_compile_and_symbol_resolution",
    "live_success_and_adversarial_fixture_replay",
    "certificate_checker_live_replay",
    "persistent_meta_request_and_same_request_repr_adapter",
    "central_persistent_cache_adapter_binding_and_replay",
)
EXPECTED_PRE_SMOKE_BLOCKERS: tuple[str, ...] = (
    "coordinator_shared_label_contract_update",
    "positive_smoke_root_specific_micro_census",
    "n31_rubric_smoke_root_specific_micro_census",
)
EXPECTED_CLOSED_EXPR_HASH_ALGORITHM = "sha256_canonical_closed_expr_alpha_tree_v1"
EXPECTED_REPR_FREEZE_COMMIT = "176a783842c5a73b84413dfa8347670608b615d9"
EXPECTED_TYPED_CERTIFICATE_FIELDS: tuple[str, ...] = (
    "binder_ordinal",
    "binder_site",
    "source_name",
    "candidate_name",
    "binder_info",
)
EXPECTED_SIDECAR_DELTA_FIELDS: tuple[str, ...] = (
    "selected_site_path",
    "old_binder_name",
    "new_binder_name",
    "binder_info",
    "binder_aware_source_fingerprint",
    "binder_aware_candidate_fingerprint",
)
EXPECTED_COMPOSITION_PRODUCTIONS: tuple[str, ...] = (
    "positive_row := P",
    "positive_row := P P",
    "positive_row := P P P",
    "negative_row := N",
    "negative_row := P N",
    "negative_row := P P N",
)


class P01IdentityPolicyError(ValueError):
    """Raised when the additive overlay or any frozen dependency drifts."""


class P01StrictModel(StrictModel):
    """Reject Python's bool/int equality aliases for exact scalar literals."""

    @model_validator(mode="before")
    @classmethod
    def _strict_bool_and_int_literals(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        for field_name, field in cls.model_fields.items():
            if field_name not in data:
                continue
            literal_values = (
                get_args(field.annotation) if get_origin(field.annotation) is Literal else ()
            )
            if len(literal_values) != 1:
                continue
            expected = literal_values[0]
            observed = data[field_name]
            if type(expected) in {bool, int} and type(observed) is not type(expected):
                raise ValueError(
                    f"{field_name} must use exact {type(expected).__name__} scalar syntax"
                )
        return data


class FrozenArtifactBinding(P01StrictModel):
    artifact_id: SymbolicId
    path: NonEmptyStr
    file_sha256: Sha256


class ParentFreezeBinding(P01StrictModel):
    commit: GitObject
    tree: GitObject
    parent_readiness_version: Literal["0.3.4"]
    parent_readiness_semantic_hash: Sha256
    parent_operation_bank_semantic_hash: Sha256
    parent_fixture_semantic_hash: Sha256
    parent_source_import_stripped_sha256: Sha256
    base_policy_file_sha256: Sha256
    base_policy_semantic_hash: Sha256
    complete_operation_registry_hash: Sha256
    frozen_artifacts: tuple[FrozenArtifactBinding, ...]
    every_revision_0_3_4_artifact_remains_unmodified: Literal[True]
    complete_46_operation_registry_remains_unmodified: Literal[True]


class UserAuthorization(P01StrictModel):
    exact_user_text: NonEmptyStr
    exact_user_text_sha256: Sha256
    recorded_date: IsoDate
    interpretation: Literal["additive_p01_identity_policy_only_no_execution"]


class AuthorizedScope(P01StrictModel):
    exact_new_paths: tuple[NonEmptyStr, ...]
    brief_update_path: Literal["plans/30_sft1_deterministic.md"]
    may_clear_blocker_ids: tuple[Literal["p01_alpha_closed_expr_hash_collision"], ...]
    may_clear_any_other_blocker: Literal[False]
    may_modify_parent_artifacts: Literal[False]
    lean_allowed: Literal[False]
    transformation_execution_allowed: Literal[False]
    gate_execution_allowed: Literal[False]
    model_facing_rows_allowed: Literal[False]
    production_admission_allowed: Literal[False]
    wave2_allowed: Literal[False]
    ten_k_allowed: Literal[False]
    scale_allowed: Literal[False]
    training_allowed: Literal[False]
    publication_allowed: Literal[False]


class P01OperationBinding(P01StrictModel):
    operation_id: Literal["P01_ALPHA_RENAME_SINGLE_V1"]
    family_id: Literal["P01"]
    mechanism_superclass: Literal["presentation_alpha"]
    evidence_class: Literal["P-DEF"]
    inverse_token: Literal["P01_ALPHA_RENAME"]
    anchor_ref: Literal["sft1.meta.p01_alpha_rename_single.v1"]
    anchor_hash: Sha256
    registry_entry_hash: Sha256
    operation_bank_entry_hash: Sha256
    operation_bank_entry_payload_hash: Sha256
    fixture_aggregate_hash: Sha256
    authored_bundle_hash: Sha256
    transparency: Literal["none"]
    allowed_axiom_profile: Literal["constructive_kernel"]
    maximum_uses_per_chain: Literal[1]
    maximum_retained_pairs_per_root: Literal[1]
    maximum_retained_share: StrictFloat


class FrozenParentContractHashes(P01StrictModel):
    p01_operation_spec_hash: Sha256
    p01_operation_bank_entry_hash: Sha256
    p01_operation_bank_entry_payload_hash: Sha256
    composition_grammar_hash: Sha256
    sampling_and_quality_hash: Sha256
    cap_contract_hash: Sha256
    deterministic_cap_order_hash: Sha256
    readiness_p01_blocker_hash: Sha256
    operation_bank_p01_blocker_hash: Sha256
    verification_state_hash: Sha256
    prohibitions_hash: Sha256
    authorization_scope_hash: Sha256


class ReprIdentityBinding(P01StrictModel):
    closed_expr_hash_algorithm: Literal["sha256_canonical_closed_expr_alpha_tree_v1"]
    repr_freeze_commit: Literal["176a783842c5a73b84413dfa8347670608b615d9"]
    repr_spec_hash: Sha256
    renderer_api_hash: Sha256
    universe_profile_id: Literal["goal_v1_first_occurrence_u_i_v1"]
    universe_profile_hash: Sha256
    render_context_id: Literal["goal_v1_render_context_v1"]
    render_context_hash: Sha256
    route: Literal["closed_expr_in_session"]
    emitter: Literal["LeanFaith.GoalV1.emitClosedProp"]
    parent_bindings_remain_unmodified: Literal[True]


class FrozenCollisionEvidence(P01StrictModel):
    case_id: Literal["mathlib_add_pow"]
    path: NonEmptyStr
    file_sha256: Sha256
    canonical_hash: Sha256
    reference_closed_expr_hash: Sha256
    candidate_closed_expr_hash: Sha256
    reference_render_hash: Sha256
    candidate_render_hash: Sha256
    render_hashes_distinct: Literal[True]
    representation_evidence_only: Literal[True]
    live_transform_or_certificate_evidence: Literal[False]
    production_admission: Literal[False]


class IdentityException(P01StrictModel):
    exception_id: Literal["p01_immediate_alpha_invariant_closed_expr_repeat_v0_3_5"]
    operation_id: Literal["P01_ALPHA_RENAME_SINGLE_V1"]
    repeatable_hash_kind: Literal["alpha_invariant_canonical_closed_expr_hash"]
    repeated_hash_must_equal_immediately_preceding_endpoint: Literal[True]
    may_match_any_non_immediately_preceding_endpoint: Literal[False]
    endpoint_trace_validation_required: Literal[True]
    permitted_repeated_hash_class_cardinality: Literal[2]
    permitted_repeated_hash_endpoints_must_be_adjacent: Literal[True]
    edge_between_permitted_repeated_endpoints_must_be_the_sole_p01_hop: Literal[True]
    third_or_nonadjacent_occurrence_disposition: Literal["cycle_rejected"]
    exception_applies_only_to_the_single_p01_hop: Literal[True]
    maximum_uses_per_chain: Literal[1]
    zero_p01_hops_remain_allowed: Literal[True]
    maximum_retained_pairs_per_root: Literal[1]
    maximum_retained_share: StrictFloat
    every_composed_pair_containing_p01_counts_toward_p01_caps_across_polarities: Literal[True]
    cap_denominator_unchanged: Literal[True]
    cap_is_maximum_not_quota: Literal[True]


class QualificationContract(P01StrictModel):
    reference_candidate_alpha_invariant_closed_expr_hash_equal: Literal[True]
    candidate_hash_equals_immediately_preceding_reference_hash: Literal[True]
    alpha_hash_equality_is_not_certificate_evidence: Literal[True]
    alpha_hash_algorithm_and_repr_context_remain_parent_pinned: Literal[True]
    reference_candidate_render_hashes_distinct: Literal[True]
    render_hash_kind: Literal["frozen_repr_rendered_goal_hash"]
    reference_candidate_model_facing_texts_distinct: Literal[True]
    model_facing_text_comparison: Literal["exact_sidecar_core_text_utf8_bytes"]
    reference_and_candidate_share_renderer_spec_universe_and_render_context: Literal[True]
    reference_and_candidate_remain_in_same_persistent_meta_request: Literal[True]
    distinctness_checks_apply_to_immediate_p01_step_not_only_final_pair: Literal[True]
    selected_binder_site_rediscovered_in_current_typed_expr: Literal[True]
    selected_binder_site_match_count: Literal[1]
    other_eligible_binder_sites_may_exist: Literal[True]
    stale_missing_or_ambiguous_site_disposition: Literal["typed_not_applicable"]
    typed_p01_certificate_field_order: tuple[NonEmptyStr, ...]
    binder_aware_sidecar_delta_field_order: tuple[NonEmptyStr, ...]
    certificate_binds_selected_site_exactly: Literal[True]
    certificate_binder_ordinal_path_and_chain_lineage_must_agree: Literal[True]
    certificate_binds_old_and_new_names_exactly: Literal[True]
    certificate_binds_binder_info_exactly: Literal[True]
    old_and_new_names_must_differ: Literal[True]
    selected_binder_must_remain_named_explicit_outer_forall: Literal[True]
    deterministic_candidate_name_must_use_name_mk_simple_without_macro_scopes: Literal[True]
    deterministic_candidate_name_must_be_capture_free_and_collision_free: Literal[True]
    certificate_replay_proves_every_non_name_expr_part_unchanged: Literal[True]
    unchanged_non_name_parts_include_domain_body_bvars_universes_metadata_other_binders_and_binder_info: (  # noqa: E501
        Literal[True]
    )
    expr_equal_replay_and_exact_certificate_equality_required: Literal[True]
    definitional_equality_or_hash_equality_alone_is_insufficient: Literal[True]
    candidate_must_equal_exact_deterministic_replay_expr: Literal[True]
    deterministic_replay_mismatch_disposition: Literal["certificate_replay_failed"]
    typed_certificate_replay_required_before_exception_applies: Literal[True]
    current_live_certificate_replay_complete: Literal[False]
    attempt_009_is_collision_and_render_evidence_not_live_transform_or_certificate_evidence: (
        Literal[True]
    )


class UnchangedRuleContract(P01StrictModel):
    maximum_total_operations_per_chain: Literal[3]
    composition_productions_unchanged: Literal[True]
    all_sites_pairwise_disjoint_after_typed_rediscovery: Literal[True]
    repeated_text_hashes_rejected: Literal[True]
    repeated_render_hashes_rejected: Literal[True]
    repeated_selected_site_lineage_rejected: Literal[True]
    repeated_operation_ids_rejected: Literal[True]
    one_operation_per_mechanism_superclass: Literal[True]
    repeated_inverse_tokens_rejected: Literal[True]
    all_nonexception_closed_expr_hash_repeats_rejected: Literal[True]
    all_non_immediately_preceding_closed_expr_hash_repeats_rejected: Literal[True]
    all_other_path_fingerprints_rejected: Literal[True]
    all_other_cycle_rules_unchanged: Literal[True]
    inherited_lemma_or_procedure_share_maximum: StrictFloat
    inherited_lemma_or_procedure_cap_remains_additionally_applicable: Literal[True]
    every_other_cap_and_deterministic_cap_order_unchanged: Literal[True]
    p01_operation_orientation_transparency_logic_axioms_projects_and_budgets_unchanged: Literal[
        True
    ]
    canonical_unordered_pair_hash_basis: Literal[
        "sha256_sorted_reference_candidate_render_hashes_v1"
    ]
    canonical_unordered_pair_deduplication_required: Literal[True]
    same_label_duplicate_survivor_rule: Literal["minimum_stable_row_hash"]
    conflicting_label_class_action: Literal["reject_entire_canonical_unordered_pair_class"]
    duplicate_conflict_screen_before_caps_checks_both_orientations: Literal[True]
    deterministic_training_orientation_swap_after_caps: Literal[True]
    post_orientation_global_duplicate_conflict_rejection_required: Literal[True]
    post_orientation_failure_action: Literal["fail_shard_without_commit_or_refill"]


class EffectiveStateTransition(P01StrictModel):
    base_blocker_id: Literal["p01_alpha_closed_expr_hash_collision"]
    base_blocker_status: Literal["open_fail_closed"]
    cleared_blocker_ids: tuple[Literal["p01_alpha_closed_expr_hash_collision"], ...]
    effective_p01_identity_policy_status: Literal["approved_exception_policy_only"]
    p01_identity_blocker_cleared: Literal[True]
    p01_operation_implementation_ready: Literal[False]
    overall_implementation_ready: Literal[False]
    gate_execution_may_start: Literal[False]
    remaining_implementation_blockers: tuple[NonEmptyStr, ...]
    remaining_pre_smoke_nonimplementation_blockers: tuple[NonEmptyStr, ...]
    parent_p01_blocker_object_remains_unmodified: Literal[True]
    parent_verification_state_remains_unmodified: Literal[True]
    parent_prohibitions_remain_unmodified: Literal[True]


class IncompletePrerequisites(P01StrictModel):
    lean_compilation_complete: Literal[False]
    live_fixtures_complete: Literal[False]
    certificate_replay_complete: Literal[False]
    persistent_meta_repr_adapter_complete: Literal[False]
    central_cache_integration_complete: Literal[False]
    n31_resolution_complete: Literal[False]
    shared_label_contract_complete: Literal[False]
    positive_smoke_root_micro_census_complete: Literal[False]
    n31_rubric_smoke_root_micro_census_complete: Literal[False]
    future_cache_policy_config_hash_must_bind_this_overlay_semantic_hash: Literal[True]


class Prohibitions(P01StrictModel):
    lean_project_compilation_or_meta_execution_started: Literal[False]
    transformation_execution_started: Literal[False]
    gate_execution_started: Literal[False]
    model_facing_rows_generated_or_emitted: Literal[False]
    production_admitted_operation_count: Literal[0]
    wave2_implementation_started: Literal[False]
    ten_k_authorized: Literal[False]
    scale_authorized: Literal[False]
    training_started: Literal[False]
    publication_authorized: Literal[False]
    shared_contract_modified: Literal[False]


class P01IdentityPolicyOverlay(P01StrictModel):
    schema_version: Literal[1]
    policy_id: Literal["sft1_p01_identity_policy_v0_3_5"]
    policy_version: Literal["0.3.5"]
    status: Literal["approved_additive_policy_only_no_execution"]
    parent_freeze: ParentFreezeBinding
    user_authorization: UserAuthorization
    authorized_scope: AuthorizedScope
    p01_operation_binding: P01OperationBinding
    frozen_parent_contract_hashes: FrozenParentContractHashes
    repr_identity_binding: ReprIdentityBinding
    frozen_collision_evidence: FrozenCollisionEvidence
    identity_exception: IdentityException
    qualification_contract: QualificationContract
    unchanged_rule_contract: UnchangedRuleContract
    effective_state_transition: EffectiveStateTransition
    incomplete_prerequisites: IncompletePrerequisites
    prohibitions: Prohibitions

    @model_validator(mode="after")
    def _exact_additive_scope(self) -> P01IdentityPolicyOverlay:
        authorization = self.user_authorization
        if (
            authorization.exact_user_text_sha256 != EXPECTED_USER_AUTHORIZATION_SHA256
            or sha256_hex(authorization.exact_user_text.encode("utf-8"))
            != EXPECTED_USER_AUTHORIZATION_SHA256
        ):
            raise ValueError("P01 authorization text/hash drift")
        if self.authorized_scope.exact_new_paths != EXPECTED_NEW_PATHS:
            raise ValueError("P01 overlay writable-path scope drift")
        if self.authorized_scope.brief_update_path != EXPECTED_BRIEF_PATH:
            raise ValueError("P01 overlay brief path drift")
        if self.authorized_scope.may_clear_blocker_ids != (EXPECTED_BLOCKER_ID,):
            raise ValueError("P01 overlay may clear exactly one named blocker")
        if self.qualification_contract.typed_p01_certificate_field_order != (
            EXPECTED_TYPED_CERTIFICATE_FIELDS
        ):
            raise ValueError("P01 typed certificate field inventory/order drift")
        if self.qualification_contract.binder_aware_sidecar_delta_field_order != (
            EXPECTED_SIDECAR_DELTA_FIELDS
        ):
            raise ValueError("P01 binder-aware sidecar field inventory/order drift")
        transition = self.effective_state_transition
        if transition.cleared_blocker_ids != (EXPECTED_BLOCKER_ID,):
            raise ValueError("effective transition clears more than the P01 identity blocker")
        if transition.remaining_implementation_blockers != (
            EXPECTED_REMAINING_IMPLEMENTATION_BLOCKERS
        ):
            raise ValueError("effective implementation-blocker projection drift")
        if transition.remaining_pre_smoke_nonimplementation_blockers != (
            EXPECTED_PRE_SMOKE_BLOCKERS
        ):
            raise ValueError("pre-smoke blockers changed under P01 overlay")
        if (
            self.p01_operation_binding.maximum_retained_share != 0.005
            or self.identity_exception.maximum_retained_share != 0.005
            or self.unchanged_rule_contract.inherited_lemma_or_procedure_share_maximum != 0.0025
        ):
            raise ValueError("P01 or inherited procedure cap drift")
        return self


def _repo_path(root: Path, relative: str, *, description: str) -> Path:
    resolved_root = root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise P01IdentityPolicyError(f"{description} path escapes repository")
    return resolved


def _validate_parent_freeze(
    config: P01IdentityPolicyOverlay,
    *,
    root: Path,
    parent: LoadedWave1ImplementationReadiness,
) -> None:
    binding = config.parent_freeze
    if binding.commit != EXPECTED_PARENT_COMMIT or binding.tree != EXPECTED_PARENT_TREE:
        raise P01IdentityPolicyError("revision-0.3.5 parent commit/tree drift")
    if (
        binding.parent_readiness_semantic_hash != EXPECTED_PARENT_READINESS_SEMANTIC_HASH
        or parent.config_hash != EXPECTED_PARENT_READINESS_SEMANTIC_HASH
        or binding.parent_operation_bank_semantic_hash
        != EXPECTED_PARENT_OPERATION_BANK_SEMANTIC_HASH
        or parent.banks.config_hash != EXPECTED_PARENT_OPERATION_BANK_SEMANTIC_HASH
        or binding.parent_fixture_semantic_hash != EXPECTED_PARENT_FIXTURE_SEMANTIC_HASH
        or parent.fixtures.config_hash != EXPECTED_PARENT_FIXTURE_SEMANTIC_HASH
        or binding.parent_source_import_stripped_sha256 != EXPECTED_PARENT_SOURCE_PREAMBLE_SHA256
        or parent.config.authored_artifacts.lean_source.import_stripped_preamble_sha256
        != EXPECTED_PARENT_SOURCE_PREAMBLE_SHA256
    ):
        raise P01IdentityPolicyError("revision-0.3.4 semantic dependency drift")
    base_policy = parent.parent.loaded_admission.loaded_base_policy
    if (
        binding.base_policy_file_sha256 != EXPECTED_BASE_POLICY_FILE_SHA256
        or hash_file(base_policy.path) != EXPECTED_BASE_POLICY_FILE_SHA256
        or binding.base_policy_semantic_hash != EXPECTED_BASE_POLICY_SEMANTIC_HASH
        or base_policy.config_hash != EXPECTED_BASE_POLICY_SEMANTIC_HASH
        or binding.complete_operation_registry_hash != EXPECTED_OPERATION_REGISTRY_HASH
    ):
        raise P01IdentityPolicyError("base policy or complete registry binding drift")
    observed_artifacts = tuple(
        (item.artifact_id, item.path, item.file_sha256) for item in binding.frozen_artifacts
    )
    if observed_artifacts != EXPECTED_FROZEN_ARTIFACTS:
        raise P01IdentityPolicyError("frozen revision-0.3.4 artifact inventory drift")
    for artifact_id, path, expected_hash in EXPECTED_FROZEN_ARTIFACTS:
        observed_hash = hash_file(_repo_path(root, path, description=artifact_id))
        if observed_hash != expected_hash:
            raise P01IdentityPolicyError(f"frozen artifact hash drift: {artifact_id}")


def _validate_p01_bindings(
    config: P01IdentityPolicyOverlay,
    parent: LoadedWave1ImplementationReadiness,
) -> None:
    policy = parent.parent.loaded_admission.loaded_base_policy.config
    operation = next(
        item
        for item in (*policy.operations, *policy.synthetic_track.operations)
        if item.operation_id == "P01_ALPHA_RENAME_SINGLE_V1"
    )
    bank_entry = next(
        item
        for item in parent.banks.config.operation_banks
        if item.operation_id == "P01_ALPHA_RENAME_SINGLE_V1"
    )
    bundle = next(
        item
        for item in parent.config.primary_bundles
        if item.operation_id == "P01_ALPHA_RENAME_SINGLE_V1"
    )
    operation_payload_hash = hash_canonical(operation.model_dump(mode="json"))
    bank_payload_hash = hash_canonical(bank_entry.model_dump(mode="json"))
    bank_binding_hash = operation_bank_entry_hash(parent.banks.config, operation.operation_id)
    binding = config.p01_operation_binding
    if (
        operation_payload_hash != binding.registry_entry_hash
        or operation_payload_hash != config.frozen_parent_contract_hashes.p01_operation_spec_hash
        or bank_payload_hash != binding.operation_bank_entry_payload_hash
        or bank_payload_hash
        != config.frozen_parent_contract_hashes.p01_operation_bank_entry_payload_hash
        or bank_binding_hash != binding.operation_bank_entry_hash
        or bank_binding_hash != config.frozen_parent_contract_hashes.p01_operation_bank_entry_hash
        or bundle.operation_bank_entry_hash != bank_binding_hash
        or bundle.fixture_aggregate_hash != binding.fixture_aggregate_hash
        or bundle.authored_bundle_hash != binding.authored_bundle_hash
    ):
        raise P01IdentityPolicyError("P01 operation/bank/bundle hash binding drift")
    if (
        operation.family_id != binding.family_id
        or operation.mechanism_superclass != binding.mechanism_superclass
        or operation.evidence_class != binding.evidence_class
        or operation.inverse_token != binding.inverse_token
        or operation.anchor.ref != binding.anchor_ref
        or operation.anchor.schema_lemma_procedure_hash != binding.anchor_hash
        or operation.transparency != binding.transparency
        or operation.allowed_axiom_profile != binding.allowed_axiom_profile
        or operation.cap.maximum_per_root != binding.maximum_retained_pairs_per_root
        or operation.cap.maximum_retained_share != binding.maximum_retained_share
        or operation.orientation != "rename_once"
        or operation.executable
        or operation.label_emission_authorized
        or operation.admission.production_admitted
    ):
        raise P01IdentityPolicyError("P01 frozen registry semantics drift")
    if (
        bank_entry.exact_delta_fields != EXPECTED_SIDECAR_DELTA_FIELDS
        or not bank_entry.static_source_complete
        or bank_entry.live_lean_verified
        or bank_entry.executable
    ):
        raise P01IdentityPolicyError("P01 frozen bank readiness semantics drift")


def _validate_parent_contract_hashes(
    config: P01IdentityPolicyOverlay,
    parent: LoadedWave1ImplementationReadiness,
) -> None:
    policy = parent.parent.loaded_admission.loaded_base_policy.config
    operation = next(
        item for item in policy.operations if item.operation_id == "P01_ALPHA_RENAME_SINGLE_V1"
    )
    bank_entry = next(
        item
        for item in parent.banks.config.operation_banks
        if item.operation_id == "P01_ALPHA_RENAME_SINGLE_V1"
    )
    contracts = config.frozen_parent_contract_hashes
    observed = {
        "p01_operation_spec_hash": hash_canonical(operation.model_dump(mode="json")),
        "p01_operation_bank_entry_hash": operation_bank_entry_hash(
            parent.banks.config, operation.operation_id
        ),
        "p01_operation_bank_entry_payload_hash": hash_canonical(bank_entry.model_dump(mode="json")),
        "composition_grammar_hash": hash_canonical(
            policy.composition_grammar.model_dump(mode="json")
        ),
        "sampling_and_quality_hash": hash_canonical(
            policy.sampling_and_quality.model_dump(mode="json")
        ),
        "cap_contract_hash": hash_canonical(policy.cap_contract.model_dump(mode="json")),
        "deterministic_cap_order_hash": hash_canonical(policy.deterministic_cap_order),
        "readiness_p01_blocker_hash": hash_canonical(
            parent.config.p01_identity_blocker.model_dump(mode="json")
        ),
        "operation_bank_p01_blocker_hash": hash_canonical(
            parent.banks.config.p01_identity_blocker.model_dump(mode="json")
        ),
        "verification_state_hash": hash_canonical(
            parent.config.verification_state.model_dump(mode="json")
        ),
        "prohibitions_hash": hash_canonical(parent.config.prohibitions.model_dump(mode="json")),
        "authorization_scope_hash": hash_canonical(
            parent.config.authorization_scope.model_dump(mode="json")
        ),
    }
    if observed != contracts.model_dump(mode="python"):
        raise P01IdentityPolicyError("frozen parent contract hash projection drift")


def _validate_repr_and_collision_evidence(
    config: P01IdentityPolicyOverlay,
    *,
    root: Path,
    parent: LoadedWave1ImplementationReadiness,
) -> None:
    base_policy = parent.parent.loaded_admission.loaded_base_policy.config
    renderer = base_policy.dependencies.expr_renderer_api
    repr_contract = parent.config.repr_integration_contract
    binding = config.repr_identity_binding
    if (
        binding.closed_expr_hash_algorithm != EXPECTED_CLOSED_EXPR_HASH_ALGORITHM
        or binding.repr_freeze_commit != EXPECTED_REPR_FREEZE_COMMIT
        or binding.repr_freeze_commit != renderer.replacement_commit
        or binding.repr_spec_hash != renderer.replacement_spec_hash
        or binding.repr_spec_hash != repr_contract.repr_spec_hash
        or binding.renderer_api_hash != renderer.renderer_api_hash
        or binding.renderer_api_hash != repr_contract.renderer_api_hash
        or binding.universe_profile_id != renderer.canonical_universe_profile_id
        or binding.universe_profile_id != repr_contract.universe_profile_id
        or binding.universe_profile_hash != renderer.canonical_universe_profile_hash
        or binding.universe_profile_hash != repr_contract.universe_profile_hash
        or binding.render_context_id != renderer.render_context_id
        or binding.render_context_id != repr_contract.render_context_id
        or binding.render_context_hash != renderer.render_context_hash
        or binding.render_context_hash != repr_contract.render_context_hash
        or binding.route != renderer.route_id
        or binding.route != repr_contract.route
        or binding.emitter != renderer.endpoint_emitter
        or binding.emitter != repr_contract.endpoint_emitter
    ):
        raise P01IdentityPolicyError("P01 overlay REPR identity binding drift")
    evidence = config.frozen_collision_evidence
    evidence_path = _repo_path(root, evidence.path, description="P01 collision evidence")
    if hash_file(evidence_path) != evidence.file_sha256:
        raise P01IdentityPolicyError("P01 collision evidence raw hash drift")
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise P01IdentityPolicyError("P01 collision evidence is not valid JSON") from exc
    if hash_canonical(payload) != evidence.canonical_hash:
        raise P01IdentityPolicyError("P01 collision evidence canonical hash drift")
    endpoints = payload.get("endpoint_bindings", {})
    expected_endpoints = {
        "reference_closed_expr_hash": evidence.reference_closed_expr_hash,
        "candidate_closed_expr_hash": evidence.candidate_closed_expr_hash,
        "reference_render_hash": evidence.reference_render_hash,
        "candidate_render_hash": evidence.candidate_render_hash,
    }
    if payload.get("case_id") != evidence.case_id or any(
        endpoints.get(key) != value for key, value in expected_endpoints.items()
    ):
        raise P01IdentityPolicyError("P01 collision endpoint evidence drift")
    if (
        evidence.reference_closed_expr_hash != evidence.candidate_closed_expr_hash
        or evidence.reference_render_hash == evidence.candidate_render_hash
    ):
        raise P01IdentityPolicyError("P01 collision evidence has wrong identity pattern")
    projection = payload.get("model_facing_projection")
    candidate_descriptor = payload.get("representation_only_candidate")
    if (
        not isinstance(projection, dict)
        or not isinstance(projection.get("reference"), str)
        or not isinstance(projection.get("candidate"), str)
        or projection["reference"] == projection["candidate"]
        or candidate_descriptor
        != {
            "family": "P01",
            "operation": "P01_ALPHA_RENAME_SINGLE_V1",
            "polarity": "positive",
            "production_admission": False,
        }
    ):
        raise P01IdentityPolicyError("P01 collision evidence projection/admission drift")
    sidecars = payload.get("complete_sidecars")
    if not isinstance(sidecars, list) or len(sidecars) != 2:
        raise P01IdentityPolicyError("P01 collision evidence must contain two complete sidecars")
    observed_roles: set[str] = set()
    for sidecar in sidecars:
        if not isinstance(sidecar, dict) or not isinstance(sidecar.get("record"), dict):
            raise P01IdentityPolicyError("P01 collision sidecar shape drift")
        record = sidecar["record"]
        role = record.get("endpoint_role")
        if role not in {"reference", "candidate"} or role in observed_roles:
            raise P01IdentityPolicyError("P01 collision endpoint roles drift")
        observed_roles.add(role)
        provenance = record.get("provenance")
        if not isinstance(provenance, dict):
            raise P01IdentityPolicyError("P01 collision provenance shape drift")
        expected_expr_hash = expected_endpoints[f"{role}_closed_expr_hash"]
        expected_render_hash = expected_endpoints[f"{role}_render_hash"]
        if (
            record.get("goal_v1") != projection[role]
            or record.get("rendered_goal_hash") != expected_render_hash
            or record.get("spec_hash") != binding.repr_spec_hash
            or provenance.get("expr_hash") != expected_expr_hash
            or provenance.get("expr_hash_algorithm") != binding.closed_expr_hash_algorithm
            or provenance.get("universe_profile_id") != binding.universe_profile_id
            or provenance.get("universe_profile_hash") != binding.universe_profile_hash
            or provenance.get("render_context_id") != binding.render_context_id
            or provenance.get("render_context_hash") != binding.render_context_hash
            or provenance.get("route_id") != binding.route
        ):
            raise P01IdentityPolicyError("P01 collision complete-sidecar binding drift")
    if observed_roles != {"reference", "candidate"}:
        raise P01IdentityPolicyError("P01 collision evidence endpoint inventory drift")


def _validate_unchanged_rules(
    config: P01IdentityPolicyOverlay,
    parent: LoadedWave1ImplementationReadiness,
) -> None:
    policy = parent.parent.loaded_admission.loaded_base_policy.config
    grammar = policy.composition_grammar
    quality = policy.sampling_and_quality
    cap = policy.cap_contract
    rules = config.unchanged_rule_contract
    frozen_exception = grammar.p01_alpha_fingerprint_repeat_exception
    if (
        grammar.maximum_total_operations != rules.maximum_total_operations_per_chain
        or grammar.productions != EXPECTED_COMPOSITION_PRODUCTIONS
        or not grammar.all_sites_pairwise_disjoint_after_typed_rediscovery
        or not grammar.repeated_text_hashes_rejected
        or not grammar.repeated_render_hashes_rejected
        or not grammar.repeated_selected_site_lineage_rejected
        or not grammar.one_operation_per_mechanism_superclass
        or not grammar.repeated_inverse_tokens_rejected
        or not grammar.repeated_closed_expr_hashes_rejected
        or not grammar.all_other_repeated_path_fingerprints_rejected
        or frozen_exception.maximum_uses_per_chain != 1
        or frozen_exception.may_repeat_expr_or_render_hash
    ):
        raise P01IdentityPolicyError("frozen composition grammar drift")
    if (
        quality.canonical_unordered_pair_hash_basis != rules.canonical_unordered_pair_hash_basis
        or quality.same_label_duplicate_survivor_rule != rules.same_label_duplicate_survivor_rule
        or quality.conflicting_label_class_action != rules.conflicting_label_class_action
        or not quality.duplicate_conflict_screen_before_caps_uses_canonical_unordered_pairs
        or not quality.duplicate_conflict_screen_before_caps_checks_both_orientations
        or not quality.orientation_swap_after_cap_selection
        or not quality.post_orientation_global_model_facing_duplicate_and_conflict_assertion
        or quality.post_orientation_assertion_failure_action
        != rules.post_orientation_failure_action
        or cap.lemma_or_procedure_share_maximum != rules.inherited_lemma_or_procedure_share_maximum
    ):
        raise P01IdentityPolicyError("frozen duplicate/cap/orientation rules drift")


def _validate_effective_transition(
    config: P01IdentityPolicyOverlay,
    parent: LoadedWave1ImplementationReadiness,
) -> None:
    readiness_blocker = parent.config.p01_identity_blocker
    bank_blocker = parent.banks.config.p01_identity_blocker
    if (
        readiness_blocker.blocker_id != EXPECTED_BLOCKER_ID
        or readiness_blocker.status != "open_fail_closed"
        or not readiness_blocker.blocks_operation_implementation_readiness
        or readiness_blocker.additive_fingerprint_overrides_frozen_rule
        or bank_blocker.blocker_id != EXPECTED_BLOCKER_ID
        or bank_blocker.status != "open_fail_closed"
        or not bank_blocker.blocks_p01_implementation_readiness
        or bank_blocker.binder_aware_delta_may_override_frozen_duplicate_rule
    ):
        raise P01IdentityPolicyError("frozen P01 blocker was mutated or pre-cleared")
    base_blockers = parent.config.verification_state.remaining_implementation_blockers
    if base_blockers.count(EXPECTED_BLOCKER_ID) != 1:
        raise P01IdentityPolicyError("frozen readiness must contain one exact P01 blocker")
    effective_blockers = tuple(item for item in base_blockers if item != EXPECTED_BLOCKER_ID)
    transition = config.effective_state_transition
    if (
        effective_blockers != EXPECTED_REMAINING_IMPLEMENTATION_BLOCKERS
        or transition.remaining_implementation_blockers != effective_blockers
        or parent.config.verification_state.remaining_pre_smoke_nonimplementation_blockers
        != EXPECTED_PRE_SMOKE_BLOCKERS
        or transition.remaining_pre_smoke_nonimplementation_blockers != EXPECTED_PRE_SMOKE_BLOCKERS
        or parent.config.verification_state.implementation_ready
        or parent.config.verification_state.gate_execution_may_start
        or transition.p01_operation_implementation_ready
        or transition.overall_implementation_ready
        or transition.gate_execution_may_start
    ):
        raise P01IdentityPolicyError("P01 effective blocker projection broadens readiness")
    checks = (
        parent.config.verification_state.lean_compile,
        parent.config.verification_state.live_success_fixtures,
        parent.config.verification_state.live_adversarial_rejections,
        parent.config.verification_state.certificate_replay,
    )
    if any(check.status != "not_run_unauthorized" or check.verified for check in checks):
        raise P01IdentityPolicyError("live verification state changed under policy-only overlay")
    if (
        parent.config.cache_readiness.central_cache_store_adapter_bound
        or parent.config.cache_readiness.central_cache_store_invoked
        or parent.config.cache_readiness.cache_replay_executed
        or parent.config.n31_target_bank_state.current_runtime_activation_authorized
        or parent.config.n31_target_bank_state.runtime_admitted_resolved_bank_identity_count != 0
    ):
        raise P01IdentityPolicyError("cache or N31 readiness changed under P01 overlay")
    base_prohibitions = parent.config.prohibitions
    if any(base_prohibitions.model_dump(mode="python").values()):
        raise P01IdentityPolicyError("frozen revision records unauthorized execution state")


def validate_p01_identity_policy(
    config: P01IdentityPolicyOverlay,
    *,
    root: Path,
    parent: LoadedWave1ImplementationReadiness,
) -> None:
    """Validate the additive policy and derive its one-blocker effective projection."""

    _validate_parent_freeze(config, root=root, parent=parent)
    _validate_p01_bindings(config, parent)
    _validate_parent_contract_hashes(config, parent)
    _validate_repr_and_collision_evidence(config, root=root, parent=parent)
    _validate_unchanged_rules(config, parent)
    _validate_effective_transition(config, parent)


@dataclass(frozen=True, slots=True)
class LoadedP01IdentityPolicy:
    loaded: LoadedConfig[P01IdentityPolicyOverlay]
    file_sha256: str
    parent: LoadedWave1ImplementationReadiness

    @property
    def config(self) -> P01IdentityPolicyOverlay:
        return self.loaded.config

    @property
    def config_hash(self) -> str:
        return self.loaded.config_hash

    @property
    def path(self) -> Path:
        return self.loaded.path

    @property
    def effective_remaining_implementation_blockers(self) -> tuple[str, ...]:
        return self.config.effective_state_transition.remaining_implementation_blockers


def load_p01_identity_policy(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedP01IdentityPolicy:
    """Load revision 0.3.5 without invoking Lean or any execution surface."""

    root = find_repo_root(repo_root)
    expected_path = (root / DEFAULT_P01_IDENTITY_POLICY_PATH).resolve()
    resolved = (path or expected_path).resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved != expected_path:
        raise P01IdentityPolicyError("P01 policy path differs from the claimed additive path")
    file_sha256 = hash_file(resolved)
    if file_sha256 != EXPECTED_OVERLAY_FILE_SHA256:
        raise P01IdentityPolicyError("P01 policy raw-file hash drift")
    loaded = load_config(resolved, P01IdentityPolicyOverlay)
    if loaded.config_hash != EXPECTED_OVERLAY_SEMANTIC_HASH:
        raise P01IdentityPolicyError("P01 policy semantic hash drift")
    parent = load_wave1_implementation_readiness(root)
    validate_p01_identity_policy(loaded.config, root=root, parent=parent)
    return LoadedP01IdentityPolicy(loaded=loaded, file_sha256=file_sha256, parent=parent)


__all__ = [
    "DEFAULT_P01_IDENTITY_POLICY_PATH",
    "EXPECTED_BLOCKER_ID",
    "EXPECTED_NEW_PATHS",
    "EXPECTED_OVERLAY_FILE_SHA256",
    "EXPECTED_OVERLAY_SEMANTIC_HASH",
    "EXPECTED_PARENT_COMMIT",
    "EXPECTED_PARENT_TREE",
    "EXPECTED_PRE_SMOKE_BLOCKERS",
    "EXPECTED_REMAINING_IMPLEMENTATION_BLOCKERS",
    "EXPECTED_SIDECAR_DELTA_FIELDS",
    "EXPECTED_TYPED_CERTIFICATE_FIELDS",
    "FrozenArtifactBinding",
    "LoadedP01IdentityPolicy",
    "P01IdentityPolicyError",
    "P01IdentityPolicyOverlay",
    "load_p01_identity_policy",
    "validate_p01_identity_policy",
]
