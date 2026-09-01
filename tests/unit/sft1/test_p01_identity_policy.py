"""Lean-free invariants for additive SFT1 P01 identity policy 0.3.5."""

from __future__ import annotations

import ast
import copy
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import leanfaith.sft1.p01_identity_policy as p01_policy
from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import DuplicateKeyError, load_yaml_mapping
from leanfaith.config.paths import find_repo_root

ZERO_SHA256 = "0" * 64
MUTATED_SHA256 = "f" * 64


@cache
def _loaded() -> p01_policy.LoadedP01IdentityPolicy:
    return p01_policy.load_p01_identity_policy()


def _payload() -> dict[str, Any]:
    return copy.deepcopy(_loaded().config.model_dump(mode="json"))


def _set_path(payload: dict[str, Any], path: str, value: object) -> None:
    parts = path.split(".")
    cursor: Any = payload
    for part in parts[:-1]:
        cursor = cursor[int(part)] if part.isdigit() else cursor[part]
    cursor[parts[-1]] = value


def _validate_payload(payload: dict[str, Any]) -> None:
    loaded = _loaded()
    config = p01_policy.P01IdentityPolicyOverlay.model_validate(payload)
    p01_policy.validate_p01_identity_policy(
        config,
        root=find_repo_root(),
        parent=loaded.parent,
    )


def _assert_mutation_rejected(path: str, value: object) -> None:
    payload = _payload()
    _set_path(payload, path, value)
    with pytest.raises((ValidationError, p01_policy.P01IdentityPolicyError)):
        _validate_payload(payload)


def test_checked_in_overlay_loads_with_exact_raw_and_semantic_hashes() -> None:
    loaded = _loaded()
    root = find_repo_root()

    assert p01_policy.EXPECTED_OVERLAY_FILE_SHA256 != ZERO_SHA256
    assert p01_policy.EXPECTED_OVERLAY_SEMANTIC_HASH != ZERO_SHA256
    assert loaded.file_sha256 == p01_policy.EXPECTED_OVERLAY_FILE_SHA256
    assert hash_file(root / p01_policy.DEFAULT_P01_IDENTITY_POLICY_PATH) == (
        p01_policy.EXPECTED_OVERLAY_FILE_SHA256
    )
    assert loaded.config_hash == p01_policy.EXPECTED_OVERLAY_SEMANTIC_HASH
    assert loaded.config_hash != p01_policy.EXPECTED_APPROVED_V0_3_5_SEMANTIC_HASH
    assert loaded.approved_runtime_policy_semantic_hash == (
        p01_policy.EXPECTED_APPROVED_V0_3_5_SEMANTIC_HASH
    )
    assert hash_canonical(p01_policy.approved_v0_3_5_policy_projection(loaded.config)) == (
        p01_policy.EXPECTED_APPROVED_V0_3_5_SEMANTIC_HASH
    )


def test_parent_commit_tree_registry_and_every_frozen_artifact_are_preserved() -> None:
    loaded = _loaded()
    parent = loaded.config.parent_freeze
    root = find_repo_root()

    assert parent.commit == p01_policy.EXPECTED_PARENT_COMMIT
    assert parent.tree == p01_policy.EXPECTED_PARENT_TREE
    assert parent.complete_46_operation_registry_remains_unmodified is True
    assert parent.every_revision_0_3_4_artifact_remains_unmodified is True
    observed = tuple(
        (item.artifact_id, item.path, item.file_sha256) for item in parent.frozen_artifacts
    )
    assert observed == p01_policy.EXPECTED_FROZEN_ARTIFACTS
    for _artifact_id, path, expected_hash in observed:
        assert hash_file(root / path) == expected_hash

    base_policy = loaded.parent.parent.loaded_admission.loaded_base_policy.config
    operations = (*base_policy.operations, *base_policy.synthetic_track.operations)
    assert len(operations) == 46
    assert len({operation.operation_id for operation in operations}) == 46


def test_exact_user_approval_and_task_owned_scope_are_hash_bound() -> None:
    config = _loaded().config
    authorization = config.user_authorization

    assert sha256_hex(authorization.exact_user_text.encode("utf-8")) == (
        p01_policy.EXPECTED_USER_AUTHORIZATION_SHA256
    )
    assert authorization.exact_user_text_sha256 == (p01_policy.EXPECTED_USER_AUTHORIZATION_SHA256)
    assert config.authorized_scope.exact_new_paths == p01_policy.EXPECTED_NEW_PATHS
    assert config.authorized_scope.brief_update_path == p01_policy.EXPECTED_BRIEF_PATH
    assert config.authorized_scope.may_clear_blocker_ids == (p01_policy.EXPECTED_BLOCKER_ID,)
    assert config.authorized_scope.may_clear_any_other_blocker is False
    assert config.authorized_scope.may_modify_parent_artifacts is False


def test_corrective_authorization_binds_preserved_505b747_revision() -> None:
    correction = _loaded().config.corrective_revision

    assert correction.parent_commit == p01_policy.EXPECTED_APPROVED_V0_3_5_COMMIT
    assert correction.parent_tree == p01_policy.EXPECTED_APPROVED_V0_3_5_TREE
    assert correction.approved_policy_file_sha256 == (
        p01_policy.EXPECTED_APPROVED_V0_3_5_FILE_SHA256
    )
    assert correction.approved_policy_semantic_hash == (
        p01_policy.EXPECTED_APPROVED_V0_3_5_SEMANTIC_HASH
    )
    assert sha256_hex(correction.exact_user_text.encode("utf-8")) == (
        correction.exact_user_text_sha256
    )
    assert correction.exact_user_text_sha256 == (
        p01_policy.EXPECTED_CORRECTIVE_AUTHORIZATION_SHA256
    )
    assert correction.parent_commit_preserved_in_history is True
    assert correction.lean_free_correction_only is True
    assert correction.push_authorized is False


def test_exception_is_one_optional_immediate_p01_hop_and_no_broader_cycle_escape() -> None:
    exception = _loaded().config.identity_exception

    assert exception.operation_id == "P01_ALPHA_RENAME_SINGLE_V1"
    assert exception.repeatable_hash_kind == "alpha_invariant_canonical_closed_expr_hash"
    assert exception.maximum_uses_per_chain == 1
    assert exception.zero_p01_hops_remain_allowed is True
    assert exception.repeated_hash_must_equal_immediately_preceding_endpoint is True
    assert exception.may_match_any_non_immediately_preceding_endpoint is False
    assert exception.permitted_repeated_hash_class_cardinality == 2
    assert exception.permitted_repeated_hash_endpoints_must_be_adjacent is True
    assert exception.edge_between_permitted_repeated_endpoints_must_be_the_sole_p01_hop is True
    assert exception.third_or_nonadjacent_occurrence_disposition == "cycle_rejected"
    assert exception.exception_applies_only_to_the_single_p01_hop is True


def test_p01_caps_remain_maxima_and_apply_to_every_chain_containing_p01() -> None:
    config = _loaded().config
    binding = config.p01_operation_binding
    exception = config.identity_exception
    rules = config.unchanged_rule_contract

    assert binding.maximum_retained_pairs_per_root == 1
    assert exception.maximum_retained_pairs_per_root == 1
    assert binding.maximum_retained_share == 0.005
    assert exception.maximum_retained_share == 0.005
    assert exception.cap_is_maximum_not_quota is True
    assert exception.cap_denominator_unchanged is True
    assert exception.every_composed_pair_containing_p01_counts_toward_p01_caps_across_polarities
    assert rules.inherited_lemma_or_procedure_share_maximum == 0.0025
    assert rules.inherited_lemma_or_procedure_cap_remains_additionally_applicable is True


def test_qualification_requires_distinct_render_and_text_plus_unique_site() -> None:
    qualification = _loaded().config.qualification_contract

    assert qualification.reference_candidate_alpha_invariant_closed_expr_hash_equal is True
    assert qualification.candidate_hash_equals_immediately_preceding_reference_hash is True
    assert qualification.alpha_hash_equality_is_not_certificate_evidence is True
    assert qualification.reference_candidate_render_hashes_distinct is True
    assert qualification.reference_candidate_model_facing_texts_distinct is True
    assert qualification.model_facing_text_comparison == "exact_sidecar_core_text_utf8_bytes"
    assert qualification.distinctness_checks_apply_to_immediate_p01_step_not_only_final_pair
    assert qualification.selected_binder_site_rediscovered_in_current_typed_expr is True
    assert qualification.selected_binder_site_match_count == 1
    assert qualification.other_eligible_binder_sites_may_exist is True
    assert qualification.stale_missing_or_ambiguous_site_disposition == "typed_not_applicable"


def test_exact_name_only_certificate_and_deterministic_replay_are_mandatory() -> None:
    qualification = _loaded().config.qualification_contract

    assert qualification.typed_p01_certificate_field_order == (
        p01_policy.EXPECTED_TYPED_CERTIFICATE_FIELDS
    )
    assert qualification.binder_aware_sidecar_delta_field_order == (
        p01_policy.EXPECTED_SIDECAR_DELTA_FIELDS
    )
    assert qualification.certificate_binds_selected_site_exactly is True
    assert qualification.certificate_binder_ordinal_path_and_chain_lineage_must_agree
    assert qualification.certificate_binds_old_and_new_names_exactly is True
    assert qualification.certificate_binds_binder_info_exactly is True
    assert qualification.old_and_new_names_must_differ is True
    assert qualification.certificate_replay_proves_every_non_name_expr_part_unchanged
    assert qualification.unchanged_non_name_parts_include_domain_body_bvars_universes_metadata_other_binders_and_binder_info
    assert qualification.expr_equal_replay_and_exact_certificate_equality_required is True
    assert qualification.definitional_equality_or_hash_equality_alone_is_insufficient is True
    assert qualification.candidate_must_equal_exact_deterministic_replay_expr is True
    assert qualification.typed_certificate_replay_required_before_exception_applies is True
    assert qualification.current_live_certificate_replay_complete is False


def test_every_non_closed_expr_repeat_and_duplicate_conflict_rule_remains_frozen() -> None:
    loaded = _loaded()
    rules = loaded.config.unchanged_rule_contract
    grammar = loaded.parent.parent.loaded_admission.loaded_base_policy.config.composition_grammar

    assert grammar.p01_alpha_fingerprint_repeat_exception.may_repeat_expr_or_render_hash is False
    assert rules.repeated_text_hashes_rejected is True
    assert rules.repeated_render_hashes_rejected is True
    assert rules.repeated_selected_site_lineage_rejected is True
    assert rules.repeated_operation_ids_rejected is True
    assert rules.one_operation_per_mechanism_superclass is True
    assert rules.repeated_inverse_tokens_rejected is True
    assert rules.all_nonexception_closed_expr_hash_repeats_rejected is True
    assert rules.all_non_immediately_preceding_closed_expr_hash_repeats_rejected is True
    assert rules.all_other_cycle_rules_unchanged is True
    assert rules.canonical_unordered_pair_deduplication_required is True
    assert rules.duplicate_conflict_screen_before_caps_checks_both_orientations is True
    assert rules.post_orientation_global_duplicate_conflict_rejection_required is True
    assert rules.post_orientation_failure_action == "fail_shard_without_commit_or_refill"


def test_collision_receipt_is_only_representation_evidence_and_not_live_replay() -> None:
    evidence = _loaded().config.frozen_collision_evidence

    assert evidence.reference_closed_expr_hash == evidence.candidate_closed_expr_hash
    assert evidence.reference_render_hash != evidence.candidate_render_hash
    assert evidence.render_hashes_distinct is True
    assert evidence.representation_evidence_only is True
    assert evidence.live_transform_or_certificate_evidence is False
    assert evidence.production_admission is False


def test_runtime_blocker_binds_reviewed_policy_hash_and_stays_open() -> None:
    loaded = _loaded()
    contract = loaded.config.runtime_binding_contract
    observed = contract.observed_state

    assert contract.blocker_id == p01_policy.EXPECTED_RUNTIME_BLOCKER_ID
    assert contract.status == "open_fail_closed"
    assert contract.approved_parent_commit == p01_policy.EXPECTED_APPROVED_V0_3_5_COMMIT
    assert contract.approved_parent_tree == p01_policy.EXPECTED_APPROVED_V0_3_5_TREE
    assert contract.approved_policy_file_sha256 == (p01_policy.EXPECTED_APPROVED_V0_3_5_FILE_SHA256)
    assert contract.required_policy_semantic_hash == (
        p01_policy.EXPECTED_APPROVED_V0_3_5_SEMANTIC_HASH
    )
    assert contract.corrected_overlay_semantic_hash_must_also_be_bound_by_runtime_receipt
    assert contract.blocks_p01_operation_implementation_readiness is True
    assert contract.blocks_overall_implementation_readiness is True
    assert contract.blocks_gate_execution is True
    assert contract.blocker_resolution_requires_every_contract_axis is True
    assert observed.runtime_implementation_path is None
    assert observed.runtime_implementation_symbol is None
    assert observed.runtime_code_sha256 is None
    assert observed.observed_policy_semantic_hash is None
    assert observed.binding_receipt_sha256 is None
    assert observed.replay_receipt_sha256 is None
    assert observed.policy_semantic_hash_loaded_and_bound is False
    assert observed.acceptance_replay_complete is False
    assert observed.rejection_matrix_replay_complete is False
    assert observed.cap_accounting_replay_complete is False
    assert observed.dedup_conflict_replay_complete is False
    assert observed.blocker_resolved is False


def test_runtime_acceptance_contract_is_exact_conjunction() -> None:
    acceptance = _loaded().config.runtime_binding_contract.acceptance_contract

    assert acceptance.required_operation_id == "P01_ALPHA_RENAME_SINGLE_V1"
    assert acceptance.repeatable_hash_kind == "alpha_invariant_canonical_closed_expr_hash"
    assert acceptance.required_policy_semantic_hash_loaded_and_bound_before_evaluation is True
    assert acceptance.exact_certificate_replay_must_pass_before_exception is True
    assert acceptance.permitted_repeated_hash_class_cardinality == 2
    assert acceptance.repeated_hash_endpoints_must_be_adjacent is True
    assert acceptance.connecting_edge_must_be_required_operation is True
    assert acceptance.connecting_edge_must_be_chain_sole_p01_hop is True
    assert acceptance.maximum_p01_hops_per_chain == 1
    assert acceptance.reference_candidate_render_hashes_must_differ is True
    assert acceptance.reference_candidate_model_facing_core_text_bytes_must_differ is True
    assert acceptance.all_other_closed_expr_hash_repetitions_rejected is True


def test_runtime_rejection_inventory_is_exact_and_ordered() -> None:
    rejection_conditions = _loaded().config.runtime_binding_contract.rejection_conditions

    assert (
        tuple((condition.case_id, condition.disposition) for condition in rejection_conditions)
        == p01_policy.EXPECTED_RUNTIME_REJECTION_CONDITIONS
    )


def test_runtime_cap_scope_covers_both_polarities_and_all_p01_compositions() -> None:
    caps = _loaded().config.runtime_binding_contract.cap_accounting_contract

    assert caps.scope_basis == "every_retained_pair_whose_operation_chain_contains_p01"
    assert caps.count_positive_polarity is True
    assert caps.count_negative_polarity is True
    assert caps.count_direct_p01_chains is True
    assert caps.count_composed_p01_chains is True
    assert caps.count_p01_in_every_permitted_chain_position is True
    assert caps.maximum_p01_hops_per_chain == 1
    assert caps.maximum_retained_pairs_per_root == 1
    assert caps.maximum_retained_share == 0.005
    assert caps.inherited_lemma_or_procedure_share_maximum == 0.0025
    assert caps.inherited_lemma_or_procedure_cap_remains_additionally_applicable is True
    assert caps.cap_denominator_unchanged is True
    assert caps.cap_is_maximum_not_quota is True


def test_runtime_preserves_unordered_pair_dedup_and_conflict_pipeline() -> None:
    dedup = _loaded().config.runtime_binding_contract.dedup_conflict_contract

    assert dedup.canonical_unordered_pair_hash_basis == (
        "sha256_sorted_reference_candidate_render_hashes_v1"
    )
    assert dedup.canonical_unordered_pair_deduplication_required is True
    assert dedup.same_label_duplicate_survivor_rule == "minimum_stable_row_hash"
    assert dedup.conflicting_label_class_action == ("reject_entire_canonical_unordered_pair_class")
    assert dedup.duplicate_conflict_screen_before_caps_checks_both_orientations is True
    assert dedup.deterministic_training_orientation_swap_after_caps is True
    assert dedup.post_orientation_global_duplicate_conflict_rejection_required is True
    assert dedup.post_orientation_failure_action == "fail_shard_without_commit_or_refill"


def test_only_p01_policy_blocker_is_removed_and_all_readiness_remains_false() -> None:
    loaded = _loaded()
    transition = loaded.config.effective_state_transition
    base_blockers = loaded.parent.config.verification_state.remaining_implementation_blockers

    assert transition.cleared_blocker_ids == (p01_policy.EXPECTED_BLOCKER_ID,)
    assert loaded.effective_remaining_implementation_blockers == (
        p01_policy.EXPECTED_RUNTIME_BLOCKER_ID,
        *(blocker for blocker in base_blockers if blocker != p01_policy.EXPECTED_BLOCKER_ID),
    )
    assert transition.remaining_implementation_blockers == (
        p01_policy.EXPECTED_REMAINING_IMPLEMENTATION_BLOCKERS
    )
    assert transition.remaining_pre_smoke_nonimplementation_blockers == (
        p01_policy.EXPECTED_PRE_SMOKE_BLOCKERS
    )
    assert transition.p01_identity_blocker_cleared is True
    assert (
        transition.remaining_implementation_blockers.count(p01_policy.EXPECTED_RUNTIME_BLOCKER_ID)
        == 1
    )
    assert transition.p01_operation_implementation_ready is False
    assert transition.overall_implementation_ready is False
    assert transition.gate_execution_may_start is False


def test_all_execution_release_and_incomplete_prerequisite_states_fail_closed() -> None:
    config = _loaded().config

    assert config.incomplete_prerequisites.model_dump(mode="python") == {
        "p01_identity_exception_composition_dedup_runtime_binding_and_replay_complete": False,
        "lean_compilation_complete": False,
        "live_fixtures_complete": False,
        "certificate_replay_complete": False,
        "persistent_meta_repr_adapter_complete": False,
        "central_cache_integration_complete": False,
        "n31_resolution_complete": False,
        "shared_label_contract_complete": False,
        "positive_smoke_root_micro_census_complete": False,
        "n31_rubric_smoke_root_micro_census_complete": False,
        "future_cache_policy_config_hash_must_bind_this_overlay_semantic_hash": True,
    }
    assert config.prohibitions.model_dump(mode="python") == {
        "lean_project_compilation_or_meta_execution_started": False,
        "transformation_execution_started": False,
        "gate_execution_started": False,
        "model_facing_rows_generated_or_emitted": False,
        "production_admitted_operation_count": 0,
        "wave2_implementation_started": False,
        "ten_k_authorized": False,
        "scale_authorized": False,
        "training_started": False,
        "publication_authorized": False,
        "shared_contract_modified": False,
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("parent_freeze.commit", "f" * 40),
        ("parent_freeze.tree", "f" * 40),
        ("parent_freeze.frozen_artifacts.0.file_sha256", MUTATED_SHA256),
        ("corrective_revision.parent_commit", "f" * 40),
        ("corrective_revision.parent_tree", "f" * 40),
        ("corrective_revision.approved_policy_file_sha256", MUTATED_SHA256),
        ("corrective_revision.approved_policy_semantic_hash", MUTATED_SHA256),
        ("p01_operation_binding.registry_entry_hash", MUTATED_SHA256),
        ("p01_operation_binding.maximum_retained_pairs_per_root", 2),
        ("p01_operation_binding.maximum_retained_share", 0.006),
        ("identity_exception.maximum_uses_per_chain", 2),
        ("identity_exception.permitted_repeated_hash_class_cardinality", 3),
        ("identity_exception.may_match_any_non_immediately_preceding_endpoint", True),
        ("identity_exception.permitted_repeated_hash_endpoints_must_be_adjacent", False),
        (
            "identity_exception.edge_between_permitted_repeated_endpoints_must_be_the_sole_p01_hop",
            False,
        ),
        ("identity_exception.maximum_retained_pairs_per_root", 2),
        ("identity_exception.maximum_retained_share", 0.006),
        ("repr_identity_binding.repr_spec_hash", MUTATED_SHA256),
        ("frozen_collision_evidence.reference_closed_expr_hash", MUTATED_SHA256),
        (
            "frozen_collision_evidence.candidate_render_hash",
            "5ed5c331ab8fcd2b5d3c2a9ab185215e39e629d9d3819232614a7fa7b442bd1d",
        ),
        ("frozen_collision_evidence.production_admission", True),
        ("qualification_contract.reference_candidate_render_hashes_distinct", False),
        ("qualification_contract.reference_candidate_model_facing_texts_distinct", False),
        ("qualification_contract.selected_binder_site_match_count", 2),
        ("qualification_contract.certificate_binds_selected_site_exactly", False),
        ("qualification_contract.certificate_binds_old_and_new_names_exactly", False),
        ("qualification_contract.certificate_binds_binder_info_exactly", False),
        (
            "qualification_contract.certificate_replay_proves_every_non_name_expr_part_unchanged",
            False,
        ),
        ("qualification_contract.candidate_must_equal_exact_deterministic_replay_expr", False),
        ("qualification_contract.current_live_certificate_replay_complete", True),
        ("runtime_binding_contract.blocker_id", "wrong_runtime_blocker"),
        ("runtime_binding_contract.status", "resolved"),
        ("runtime_binding_contract.approved_parent_commit", "f" * 40),
        ("runtime_binding_contract.approved_parent_tree", "f" * 40),
        ("runtime_binding_contract.approved_policy_file_sha256", MUTATED_SHA256),
        ("runtime_binding_contract.required_policy_semantic_hash", MUTATED_SHA256),
        (
            "runtime_binding_contract.corrected_overlay_semantic_hash_must_also_be_bound_by_runtime_receipt",
            False,
        ),
        ("runtime_binding_contract.blocks_p01_operation_implementation_readiness", False),
        ("runtime_binding_contract.blocks_overall_implementation_readiness", False),
        ("runtime_binding_contract.blocks_gate_execution", False),
        (
            "runtime_binding_contract.acceptance_contract.required_policy_semantic_hash_loaded_and_bound_before_evaluation",
            False,
        ),
        (
            "runtime_binding_contract.acceptance_contract.exact_certificate_replay_must_pass_before_exception",
            False,
        ),
        (
            "runtime_binding_contract.acceptance_contract.permitted_repeated_hash_class_cardinality",
            3,
        ),
        (
            "runtime_binding_contract.acceptance_contract.repeated_hash_endpoints_must_be_adjacent",
            False,
        ),
        (
            "runtime_binding_contract.acceptance_contract.connecting_edge_must_be_required_operation",
            False,
        ),
        (
            "runtime_binding_contract.acceptance_contract.connecting_edge_must_be_chain_sole_p01_hop",
            False,
        ),
        ("runtime_binding_contract.acceptance_contract.maximum_p01_hops_per_chain", 2),
        (
            "runtime_binding_contract.acceptance_contract.reference_candidate_render_hashes_must_differ",
            False,
        ),
        (
            "runtime_binding_contract.acceptance_contract.reference_candidate_model_facing_core_text_bytes_must_differ",
            False,
        ),
        (
            "runtime_binding_contract.acceptance_contract.all_other_closed_expr_hash_repetitions_rejected",
            False,
        ),
        ("runtime_binding_contract.rejection_conditions.2.disposition", "accept"),
        ("runtime_binding_contract.cap_accounting_contract.count_positive_polarity", False),
        ("runtime_binding_contract.cap_accounting_contract.count_negative_polarity", False),
        ("runtime_binding_contract.cap_accounting_contract.count_direct_p01_chains", False),
        ("runtime_binding_contract.cap_accounting_contract.count_composed_p01_chains", False),
        (
            "runtime_binding_contract.cap_accounting_contract.count_p01_in_every_permitted_chain_position",
            False,
        ),
        ("runtime_binding_contract.cap_accounting_contract.maximum_p01_hops_per_chain", 2),
        ("runtime_binding_contract.cap_accounting_contract.maximum_retained_pairs_per_root", 2),
        ("runtime_binding_contract.cap_accounting_contract.maximum_retained_share", 0.006),
        (
            "runtime_binding_contract.cap_accounting_contract.inherited_lemma_or_procedure_share_maximum",
            0.005,
        ),
        (
            "runtime_binding_contract.dedup_conflict_contract.canonical_unordered_pair_deduplication_required",
            False,
        ),
        (
            "runtime_binding_contract.dedup_conflict_contract.duplicate_conflict_screen_before_caps_checks_both_orientations",
            False,
        ),
        (
            "runtime_binding_contract.dedup_conflict_contract.deterministic_training_orientation_swap_after_caps",
            False,
        ),
        (
            "runtime_binding_contract.dedup_conflict_contract.post_orientation_global_duplicate_conflict_rejection_required",
            False,
        ),
        (
            "runtime_binding_contract.dedup_conflict_contract.post_orientation_failure_action",
            "continue_with_refill",
        ),
        ("runtime_binding_contract.observed_state.runtime_implementation_path", "runtime.py"),
        (
            "runtime_binding_contract.observed_state.observed_policy_semantic_hash",
            p01_policy.EXPECTED_APPROVED_V0_3_5_SEMANTIC_HASH,
        ),
        (
            "runtime_binding_contract.observed_state.policy_semantic_hash_loaded_and_bound",
            True,
        ),
        ("runtime_binding_contract.observed_state.acceptance_replay_complete", True),
        ("runtime_binding_contract.observed_state.rejection_matrix_replay_complete", True),
        ("runtime_binding_contract.observed_state.cap_accounting_replay_complete", True),
        ("runtime_binding_contract.observed_state.dedup_conflict_replay_complete", True),
        ("runtime_binding_contract.observed_state.blocker_resolved", True),
        ("unchanged_rule_contract.repeated_text_hashes_rejected", False),
        ("unchanged_rule_contract.repeated_render_hashes_rejected", False),
        ("unchanged_rule_contract.repeated_selected_site_lineage_rejected", False),
        ("unchanged_rule_contract.repeated_operation_ids_rejected", False),
        ("unchanged_rule_contract.one_operation_per_mechanism_superclass", False),
        ("unchanged_rule_contract.repeated_inverse_tokens_rejected", False),
        ("unchanged_rule_contract.canonical_unordered_pair_deduplication_required", False),
        (
            "unchanged_rule_contract.post_orientation_global_duplicate_conflict_rejection_required",
            False,
        ),
        ("unchanged_rule_contract.inherited_lemma_or_procedure_share_maximum", 0.005),
        (
            "effective_state_transition.cleared_blocker_ids",
            [p01_policy.EXPECTED_BLOCKER_ID, "other"],
        ),
        ("effective_state_transition.overall_implementation_ready", True),
        ("effective_state_transition.gate_execution_may_start", True),
        (
            "incomplete_prerequisites.p01_identity_exception_composition_dedup_runtime_binding_and_replay_complete",
            True,
        ),
        ("incomplete_prerequisites.lean_compilation_complete", True),
        ("incomplete_prerequisites.certificate_replay_complete", True),
        ("authorized_scope.lean_allowed", True),
        ("authorized_scope.transformation_execution_allowed", True),
        ("authorized_scope.gate_execution_allowed", True),
        ("authorized_scope.model_facing_rows_allowed", True),
        ("prohibitions.lean_project_compilation_or_meta_execution_started", True),
        ("prohibitions.transformation_execution_started", True),
        ("prohibitions.gate_execution_started", True),
        ("prohibitions.production_admitted_operation_count", 1),
        ("prohibitions.ten_k_authorized", True),
    ],
)
def test_narrow_exception_and_fail_closed_boundaries_reject_mutations(
    path: str,
    value: object,
) -> None:
    _assert_mutation_rejected(path, value)


def test_certificate_field_order_and_effective_blocker_inventory_reject_drift() -> None:
    _assert_mutation_rejected(
        "qualification_contract.typed_p01_certificate_field_order",
        [
            "binder_site",
            "binder_ordinal",
            "source_name",
            "candidate_name",
            "binder_info",
        ],
    )
    _assert_mutation_rejected(
        "qualification_contract.binder_aware_sidecar_delta_field_order",
        list(reversed(p01_policy.EXPECTED_SIDECAR_DELTA_FIELDS)),
    )
    _assert_mutation_rejected(
        "effective_state_transition.remaining_implementation_blockers",
        list(p01_policy.EXPECTED_REMAINING_IMPLEMENTATION_BLOCKERS[:-1]),
    )


def test_runtime_rejection_inventory_rejects_drop_reorder_duplicate_and_extra() -> None:
    payload = _payload()
    original = payload["runtime_binding_contract"]["rejection_conditions"]
    assert isinstance(original, list)

    mutations = (
        original[:-1],
        list(reversed(original)),
        [*original, copy.deepcopy(original[0])],
        [*original, {"case_id": "unexpected", "disposition": "reject_unexpected"}],
    )
    for rejection_conditions in mutations:
        mutated = _payload()
        mutated["runtime_binding_contract"]["rejection_conditions"] = rejection_conditions
        with pytest.raises((ValidationError, p01_policy.P01IdentityPolicyError)):
            _validate_payload(mutated)


def test_runtime_blocker_rejects_removal_duplication_and_clearing() -> None:
    for blockers in (
        list(p01_policy.EXPECTED_REMAINING_IMPLEMENTATION_BLOCKERS[1:]),
        [
            p01_policy.EXPECTED_RUNTIME_BLOCKER_ID,
            *p01_policy.EXPECTED_REMAINING_IMPLEMENTATION_BLOCKERS,
        ],
        [
            p01_policy.EXPECTED_BLOCKER_ID,
            *p01_policy.EXPECTED_REMAINING_IMPLEMENTATION_BLOCKERS,
        ],
    ):
        mutated = _payload()
        mutated["effective_state_transition"]["remaining_implementation_blockers"] = blockers
        with pytest.raises((ValidationError, p01_policy.P01IdentityPolicyError)):
            _validate_payload(mutated)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("schema_version", True),
        ("identity_exception.maximum_uses_per_chain", True),
        ("qualification_contract.selected_binder_site_match_count", True),
        ("identity_exception.zero_p01_hops_remain_allowed", 1),
        ("runtime_binding_contract.acceptance_contract.maximum_p01_hops_per_chain", True),
        (
            "runtime_binding_contract.observed_state.policy_semantic_hash_loaded_and_bound",
            0,
        ),
        ("corrective_revision.push_authorized", 0),
        ("prohibitions.production_admitted_operation_count", False),
    ],
)
def test_bool_int_literal_aliases_are_rejected(path: str, value: object) -> None:
    _assert_mutation_rejected(path, value)


def test_unknown_fields_duplicate_yaml_and_alternate_policy_paths_fail_closed(
    tmp_path: Path,
) -> None:
    payload = _payload()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        p01_policy.P01IdentityPolicyOverlay.model_validate(payload)

    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text("schema_version: 1\nschema_version: 1\n", encoding="utf-8")
    with pytest.raises(DuplicateKeyError):
        load_yaml_mapping(duplicate)

    copied = tmp_path / "p01_identity_policy_v0_3_5.yaml"
    copied.write_bytes(
        (find_repo_root() / p01_policy.DEFAULT_P01_IDENTITY_POLICY_PATH).read_bytes()
    )
    with pytest.raises(p01_policy.P01IdentityPolicyError, match="claimed additive path"):
        p01_policy.load_p01_identity_policy(path=copied)


def test_loader_import_graph_has_no_lean_or_execution_adapter() -> None:
    source_path = find_repo_root() / "src/leanfaith/sft1/p01_identity_policy.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    assert "subprocess" not in imported_modules
    assert not any(module.startswith("leanfaith.lean") for module in imported_modules)
    assert "leanfaith.sft1.repr_six_goal_gate" not in imported_modules
