"""Lean-free invariants for the additive SFT1 readiness revision 0.3.3."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

import leanfaith.sft1.effective_readiness as effective_module
from leanfaith.config.hashing import hash_file, sha256_hex
from leanfaith.config.loading import DuplicateKeyError, load_config, load_yaml_mapping
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.effective_readiness import (
    DEFAULT_EFFECTIVE_READINESS_PATH,
    EXPECTED_EFFECTIVE_CONFIG_FILE_SHA256,
    EXPECTED_EFFECTIVE_CONFIG_SEMANTIC_HASH,
    EffectiveReadinessError,
    EffectiveWaveReadinessOverlay,
    load_effective_wave_state,
    validate_effective_wave_state,
)
from leanfaith.sft1.source_census import load_wave1_source_census

WAVE1_OPERATION_IDS = (
    "P01_ALPHA_RENAME_SINGLE_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P21_BETA_REDUCE_V1",
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
)
WAVE1_EFFECTIVE_MECHANISM_IDS = (
    "p01_alpha_rename_single_v1",
    "p15_swap_iff_sides_v1",
    "p18_symmetrize_equality_v1",
    "p21_beta_reduce_v1",
    "n31_required_guard_mutation",
)
WAVE2_OPERATION_IDS = (
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
    "P16_REASSOC_AND_LEFT_V1",
    "P22_ETA_REDUCE_EXPLICIT_FUN_V1",
    "P23_CURRY_PROP_PAIR_V1",
    "P24_SWAP_INDEPENDENT_PROP_BINDERS_V1",
    "P28_DECOMPOSE_IFF_V1",
    "P32_ADD_COMM_LOCAL_V1",
    "P33_EQ_HYP_SUBSTITUTE_NONDEPENDENT_V1",
    "P35_SET_INTER_MEMBERSHIP_V1",
    "P40_EXISTS_UNIQUE_EXPAND_V1",
    "P42_RING_POLYNOMIAL_LOCAL_V1",
    "N25_TOGGLE_EQ_NE_RUBRIC_V1",
    "N25_TOGGLE_EQ_NE_PROOF_V1",
    "N26_INCREMENT_BOUND_RUBRIC_V1",
    "N26_INCREMENT_BOUND_PROOF_V1",
    "N30_ADD_UNJUSTIFIED_UNIQUENESS_RUBRIC_V1",
    "N32_SWAP_ROLE_ORDER_RUBRIC_V1",
)
FROZEN_V032_FILE_HASHES = {
    "configs/transformations/sft1_value_first_v1/wave1_gate_admission_v0_3_2.yaml": (
        "c1cf07713bfca91e6b5fbedf75a5b5f6e0f841886df7a71e7f4f6c9d82c862b3"
    ),
    "configs/transformations/sft1_value_first_v1/wave1_source_census_v0_3_2.yaml": (
        "a8c6c3616a543ff9e1f5d4700a3b5a86da2442f70475737caf23bd264ebd2aaa"
    ),
    "configs/transformations/sft1_value_first_v1/wave1_n31_guard_bank_v0_3_2.yaml": (
        "c2a5aa63158ffbc561bc61f2e3acaa2598aff54a926fd774014e62e6c1cd8cd8"
    ),
    "configs/transformations/sft1_value_first_v1/clean_checkout_receipt_v0_3_2.json": (
        "4133c2df44b81b388d3cc39e499feb65d1cd410909b6843591ec6b1295ea3331"
    ),
    "configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml": (
        "a052ecec4cc8f61db7438dd5acbc39373a624b155f8c0305bb75b7ae15d7195d"
    ),
    "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_v0_3_1.yaml": (
        "5126eb8fb314218017fc930a79ab82cb810ff929e1794ce4617551f6c70ced91"
    ),
    "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_receipt_v0_3_1.json": (
        "ebd400b4a7b05daa933b1abaaacc378d1a7b9ae68f9159ac03453cd6081406a8"
    ),
    "policies/source_use_v2.yaml": (
        "62a4daca09ca669aef0133cd4d0b0913e1d7795558560f3aac4b289efc75e95c"
    ),
}


def _loaded() -> effective_module.LoadedEffectiveWaveState:
    return load_effective_wave_state()


def _payload() -> dict[str, Any]:
    return copy.deepcopy(_loaded().config.model_dump(mode="json"))


def _validate_payload(payload: dict[str, Any]) -> None:
    config = EffectiveWaveReadinessOverlay.model_validate(payload)
    validate_effective_wave_state(config, _loaded().loaded_admission)


def _source(payload: dict[str, Any], project_id: str) -> dict[str, Any]:
    for source in payload["source_authorization"]["sources"]:
        if source["source_id"] == project_id:
            return cast(dict[str, Any], source)
    raise AssertionError(f"missing source {project_id}")


def test_checked_in_effective_overlay_loads_exact_fail_closed_state() -> None:
    loaded = _loaded()
    config = loaded.config

    assert loaded.config_hash == EXPECTED_EFFECTIVE_CONFIG_SEMANTIC_HASH
    assert loaded.config_file_sha256 == EXPECTED_EFFECTIVE_CONFIG_FILE_SHA256
    assert hash_file(find_repo_root() / DEFAULT_EFFECTIVE_READINESS_PATH) == (
        EXPECTED_EFFECTIVE_CONFIG_FILE_SHA256
    )
    assert config.overlay_version == "0.3.3"
    assert config.status == "policy_overlay_authorized_execution_prohibited"
    assert config.effective_wave1.gate_admission_recorded is True
    assert config.effective_wave1.implementation_ready is False
    assert config.effective_wave1.smoke_ready is False
    assert config.effective_wave1.conformance_ready is False
    assert config.effective_wave1.approximately_100_roots_ready is False


def test_review_and_exact_user_authorization_are_hash_bound_without_conflation() -> None:
    config = _loaded().config
    review = config.review_binding
    authorization = config.user_authorization

    assert review.reviewed_checkpoint_commit == ("dae99b3bd04d765a7a2011e10129589951dcb3c2")
    assert review.attachment_raw_sha256 == (
        "6eacfa333ab0f3507189584e497539b2b4053acc4e8fc4a91bb74b94d9597a75"
    )
    assert review.review_is_policy_input_not_authorization is True
    assert review.wave2_recommendation_is_not_admission is True
    assert sha256_hex(authorization.exact_user_text.encode("utf-8")) == (
        authorization.exact_user_text_sha256
    )
    assert authorization.exact_user_text_sha256 == (
        "fc0c951ebaf1c43c47c9582e0f6c8ca0769b40c1d6af0613d59556278d111e56"
    )
    assert authorization.interpretation == "additive_policy_and_loader_only_no_execution"


def test_frozen_v032_inputs_are_hash_bound_and_unchanged() -> None:
    root = find_repo_root()
    frozen = _loaded().config.frozen_dependencies.model_dump(mode="python")
    bound = {
        frozen["base_policy"]["path"]: frozen["base_policy"]["file_sha256"],
        frozen["admission_v0_3_2"]["path"]: frozen["admission_v0_3_2"]["file_sha256"],
        frozen["clean_checkout_receipt_v0_3_2"]["path"]: frozen["clean_checkout_receipt_v0_3_2"][
            "file_sha256"
        ],
        frozen["source_census_v0_3_2"]["path"]: frozen["source_census_v0_3_2"]["file_sha256"],
        frozen["n31_guard_bank_v0_3_2"]["path"]: frozen["n31_guard_bank_v0_3_2"]["file_sha256"],
        frozen["repr_gate_v0_3_1"]["path"]: frozen["repr_gate_v0_3_1"]["file_sha256"],
        frozen["repr_receipt_v0_3_1"]["path"]: frozen["repr_receipt_v0_3_1"]["file_sha256"],
        frozen["source_use_v2"]["path"]: frozen["source_use_v2"]["file_sha256"],
    }

    assert bound == FROZEN_V032_FILE_HASHES
    for path, expected_hash in FROZEN_V032_FILE_HASHES.items():
        assert hash_file(root / path) == expected_hash


def test_wave1_has_six_ids_but_only_five_semantic_mechanisms() -> None:
    wave = _loaded().config.effective_wave1

    assert tuple(wave.operation_ids) == WAVE1_OPERATION_IDS
    assert tuple(wave.effective_mechanism_ids) == WAVE1_EFFECTIVE_MECHANISM_IDS
    assert len(wave.operation_ids) == 6
    assert len(wave.effective_mechanism_ids) == 5
    mapping = {
        binding.operation_id: binding.mechanism_id for binding in wave.operation_to_mechanism
    }
    assert set(mapping) == set(WAVE1_OPERATION_IDS)
    assert mapping["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"] == ("n31_required_guard_mutation")
    assert mapping["N31_DROP_REQUIRED_GUARD_PROOF_V1"] == ("n31_required_guard_mutation")


def test_wave1_gate_accounting_treats_proof_as_an_optional_nested_pass() -> None:
    accounting = _loaded().config.effective_wave1.dynamic_gate_accounting

    assert tuple(accounting.primary_operation_ids) == WAVE1_OPERATION_IDS[:-1]
    assert accounting.optional_proof_operation_id == WAVE1_OPERATION_IDS[-1]
    assert tuple(accounting.proof_eligible_project_ids) == ()
    assert accounting.primary_operation_project_cell_count == 20
    assert accounting.current_optional_proof_operation_project_cell_count == 0
    assert accounting.current_total_operation_project_cell_count == 20
    assert accounting.minimum_total_operation_project_cell_count == 20
    assert accounting.maximum_total_operation_project_cell_count == 24
    assert accounting.current_fixture_count == 40
    assert accounting.minimum_fixture_count == 40
    assert accounting.maximum_fixture_count == 48
    assert accounting.approximate_roots_per_semantic_mechanism == 100
    assert accounting.approximate_independent_root_pool_target == 500
    assert accounting.proof_additional_independent_root_pool_count == 0
    assert accounting.counts_are_gate_accounting_not_row_commitments is True


def test_census_tiers_are_attached_to_the_correct_gates() -> None:
    tiers = _loaded().config.census_tiers

    assert tuple(tiers.smoke_micro_census.required_before) == (
        "one_positive_one_negative_end_to_end_smoke",
    )
    assert tuple(tiers.selected_wave_sampling_frame_census.required_before) == (
        "approximately_100_roots_per_semantic_mechanism",
    )
    assert tuple(tiers.complete_cross_source_census.required_before) == (
        "ten_k_pilot_decision",
        "production_row_count_decision",
        "multi_million_feasibility_claim",
        "scale_decision",
        "publication_decision",
    )
    assert "one_positive_one_negative_end_to_end_smoke" not in (
        tiers.complete_cross_source_census.required_before
    )
    assert "approximately_100_roots_per_semantic_mechanism" not in (
        tiers.complete_cross_source_census.required_before
    )
    assert tiers.smoke_micro_census.blocks_two_row_smoke is True
    assert tiers.smoke_micro_census.blocks_approximately_100_root_gate is False
    assert tiers.selected_wave_sampling_frame_census.blocks_two_row_smoke is False
    assert tiers.selected_wave_sampling_frame_census.blocks_approximately_100_root_gate is True
    assert tiers.complete_cross_source_census.blocks_two_row_smoke is False
    assert tiers.complete_cross_source_census.blocks_approximately_100_root_gate is False


def test_smoke_micro_census_is_hash_bound_and_deterministically_selects_its_root() -> None:
    micro = _loaded().config.census_tiers.smoke_micro_census

    assert micro.receipt_scope == "exact_selected_smoke_root"
    assert micro.receipt_hash_bound is True
    assert micro.selection_pool_hash_bound is True
    assert micro.selection_rule == "minimum_stable_eligible_root_hash_v1"
    assert "smoke_pool_construction_rule_and_candidate_set_hash" in micro.requirements
    assert "minimum_stable_eligible_root_hash_over_smoke_pool" in micro.requirements
    assert "selected_root_is_minimum_over_bound_eligible_pool" in micro.requirements


def test_complete_census_retains_rich_inventory_and_scale_claim_requirements() -> None:
    complete = _loaded().config.census_tiers.complete_cross_source_census

    assert "multi_million_feasibility_claim" in complete.required_before
    assert set(complete.requirements) == {
        "raw_theorem_or_lemma_counts",
        "pinned_source_revision_and_release_state",
        "complete_cross_source_duplicate_unions",
        "exact_and_near_duplicate_clusters",
        "complete_source_strata",
        "source_domain_signature_strata",
        "exact_import_context_availability",
        "closed_expr_route_availability",
        "license_and_release_state",
        "realistic_root_and_yield_estimates",
    }


def test_pending_full_census_and_proof_routes_do_not_block_rubric_smoke() -> None:
    config = _loaded().config
    tiers = config.census_tiers
    lane = config.n31_lane_contract

    assert tiers.complete_cross_source_census.status == "required_not_completed"
    assert tiers.complete_cross_source_census.complete is False
    assert tiers.complete_cross_source_census.blocks_two_row_smoke is False
    assert lane.proof_unavailability_outcome == "not_in_scope_for_n_proof"
    assert lane.proof_unavailability_blocks_rubric is False
    assert lane.proof_unavailability_blocks_smoke is False
    assert lane.proof_unavailability_is_operation_failure is False
    assert lane.proof_unavailability_blocks_wave is False
    assert lane.proof_unavailability_blocks_conformance is False
    assert lane.proof_unavailability_blocks_approximately_100_root_gate is False
    assert lane.proof_unavailability_blocks_other_operations is False
    assert tuple(lane.proof_eligibility_formula) == (
        "parent_n_rubric_applicable",
        "exact_source_proof_available",
        "exact_candidate_refutation_replays",
    )
    assert lane.proof_eligibility_is_project_and_root_scoped is True
    assert tuple(lane.rubric_projects) == ("compiler_data", "cslib", "mathlib", "physlib")
    assert tuple(lane.proof_eligible_projects) == ()
    assert tuple(item.project_id for item in lane.proof_availability_by_project) == (
        "compiler_data",
        "cslib",
        "mathlib",
        "physlib",
    )
    assert all(item.status == "unknown" for item in lane.proof_availability_by_project)
    assert all(
        item.scope == "not_in_scope_for_n_proof" for item in lane.proof_availability_by_project
    )
    assert all(not item.proof_eligible for item in lane.proof_availability_by_project)
    assert config.effective_wave1.smoke_ready is False
    assert tuple(config.effective_wave1.smoke_blocker_ids) == (
        "coordinator_shared_label_contract_update",
        "five_primary_wave1_implementation_dispatch_checker_anchor_bank_fixture_bindings",
        "n31_closed_rubric_checker_and_target_head_bank",
        "positive_smoke_root_specific_micro_census",
        "n31_rubric_smoke_root_specific_micro_census",
    )
    assert tuple(config.effective_wave1.smoke_non_blocker_ids) == (
        "n31_source_proof_availability",
        "n31_optional_proof_execution_binding",
        "selected_wave_sampling_frame_census",
        "complete_cross_source_census",
        "wave2_proposal",
    )


def test_internal_measurement_and_publication_source_states_are_separate() -> None:
    sources = _loaded().config.source_authorization.sources

    assert tuple(source.source_id for source in sources) == (
        "compiler_data",
        "cslib",
        "mathlib",
        "physlib",
    )
    assert all(source.internal_gate_eligible for source in sources)
    assert all(source.pilot_eligible for source in sources)
    assert all(not source.redistribution_review_complete for source in sources)
    assert all(not source.publication_eligible for source in sources)
    exact_source_projection = {
        source.source_id: (
            source.repository,
            source.revision,
            source.spdx_id,
            source.authorization_evidence_sha256,
        )
        for source in sources
    }
    assert exact_source_projection == {
        "compiler_data": (
            "formalmathatepfl/compiler_data",
            "ca37d4701b11022f183e72b7b96ff543a8a615d3",
            None,
            "62a4daca09ca669aef0133cd4d0b0913e1d7795558560f3aac4b289efc75e95c",
        ),
        "cslib": (
            "https://github.com/leanprover/cslib",
            "2f677bfc8ef76fa7a27feafc597c1e4a7eda3e42",
            "Apache-2.0",
            "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
        ),
        "mathlib": (
            "https://github.com/leanprover-community/mathlib4",
            "d568c8c09630de097a046763c17b9ea99f95f950",
            "Apache-2.0",
            "b40930bbcf80744c86c46a12bc9da056641d722716c378f5659b9e555ef833e1",
        ),
        "physlib": (
            "https://github.com/leanprover-community/physlib",
            "f5242c99d796b59a390d26cd7d1a8057e04c46b5",
            "Apache-2.0",
            "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
        ),
    }
    source_policy = _loaded().config.source_authorization
    frozen_source_policy = _loaded().config.frozen_dependencies.source_use_v2
    assert source_policy.policy_file_sha256 == frozen_source_policy.file_sha256
    assert source_policy.policy_semantic_hash == frozen_source_policy.semantic_hash
    assert source_policy.internal_eligibility_does_not_authorize_gate is True
    assert source_policy.pilot_eligibility_does_not_authorize_ten_k is True
    assert source_policy.publication_requires_review is True
    assert _loaded().config.prohibitions.gate_execution_authorized is False
    assert _loaded().config.prohibitions.ten_k_pilot_authorized is False
    assert _loaded().config.census_tiers.complete_cross_source_census.complete is False


def test_n31_proof_is_a_nested_optional_evidence_lane() -> None:
    lane = _loaded().config.n31_lane_contract

    assert lane.rubric_operation_id == "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
    assert lane.proof_operation_id == "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    assert lane.shared_semantic_mechanism_id == "n31_required_guard_mutation"
    assert lane.same_parent_root_pool is True
    assert lane.independent_root_sampling_forbidden is True
    assert lane.proof_counts_against_own_cap is True
    assert lane.proof_counts_against_parent_semantic_cap is True
    assert lane.proof_counts_as_distinct_semantic_mechanism is False
    assert lane.proof_maximum_retained_share == 0.005
    assert lane.parent_maximum_retained_share == 0.01
    assert lane.proof_maximum_retained_share <= lane.parent_maximum_retained_share
    assert lane.natural_cap_denominator == (
        "natural_model_facing_retained_semantic_pair_population_after_duplicate_conflict_"
        "screen_before_orientation"
    )
    assert lane.parent_semantic_union_key == (
        "unique_parent_mutation_pair_id_including_proof_upgraded_subset"
    )
    assert lane.duplicate_invariants.proof_may_emit_additional_core_pair is False
    assert lane.duplicate_invariants.model_facing_pair_multiplicity_maximum == 1
    assert lane.parent_production_admission_required is True
    assert lane.shared_family_id == "N31"
    assert lane.shared_mechanism_superclass == "required_guard_mutation"
    assert lane.shared_correlation_group_id == "corr_n31_guard_drop"
    assert lane.shared_effective_diversity_group_id == "corr_n31_guard_drop"
    assert lane.shared_heldout_group_id == "corr_n31_guard_drop"


def test_wave2_is_exactly_seventeen_ids_and_only_proposed() -> None:
    wave2 = _loaded().config.wave2_proposal

    assert tuple(wave2.operation_ids) == WAVE2_OPERATION_IDS
    assert len(wave2.operation_ids) == 17
    assert wave2.semantic_mechanism_count == 15
    assert wave2.status == "proposed_not_admitted"
    assert wave2.gate_admitted is False
    assert wave2.dimension_gate_admitted is False
    assert wave2.implementation_authorized is False
    assert wave2.implementation_started is False
    assert wave2.execution_authorized is False
    assert wave2.production_admitted is False
    assert wave2.row_emission_authorized is False
    assert wave2.ten_k_authorized is False
    assert wave2.scale_authorized is False
    assert wave2.publication_authorized is False
    assert tuple(wave2.n_proof_operation_ids) == (
        "N25_TOGGLE_EQ_NE_PROOF_V1",
        "N26_INCREMENT_BOUND_PROOF_V1",
    )
    assert wave2.n_proof_ids_count_as_distinct_mechanisms is False
    assert tuple(wave2.family_dimension_ids) == (
        "n25_negation_mistakes_natural_v1",
        "n26_edge_cases_natural_v1",
        "n30_existence_uniqueness_natural_v1",
        "n32_converse_mistakes_natural_v1",
    )


def test_current_authority_is_policy_only_and_all_execution_paths_are_closed() -> None:
    boundaries = _loaded().config.authorization_boundaries
    prohibitions = _loaded().config.prohibitions.model_dump(mode="python")

    assert boundaries.allowed_now.effective_state_loader_changes is True
    assert boundaries.allowed_now.lean_free_loader_tests is True
    assert boundaries.allowed_now.lean_free_invariant_tests is True
    assert boundaries.allowed_now.formatting_checks is True
    assert boundaries.allowed_now.plan_tests is True
    assert boundaries.prior_wave1_authority_preserved.gate_admission_recorded is True
    assert boundaries.prior_wave1_authority_preserved.implementation_readiness is False
    assert boundaries.prior_wave1_authority_preserved.gate_execution_may_start is False
    assert boundaries.current_revision_scope.transform_implementation_changes_authorized is False
    assert boundaries.current_revision_scope.wave2_implementation_changes_authorized is False
    assert boundaries.coordinator_request.contract_path == "plans/00_shared_contracts.md"
    assert boundaries.coordinator_request.status == "open_untouched"
    assert boundaries.coordinator_request.task_may_edit_contract is False
    assert prohibitions
    assert all(value is False for value in prohibitions.values())


def test_complete_frozen_registry_is_preserved_without_wave2_admission() -> None:
    loaded = _loaded()
    policy = loaded.loaded_admission.loaded_base_policy.config
    operations = (*policy.operations, *policy.synthetic_track.operations)

    assert loaded.config.frozen_dependencies.base_policy.operation_count == 46
    assert len(operations) == 46
    assert {operation.operation_id for operation in operations}.issuperset(WAVE2_OPERATION_IDS)
    assert loaded.config.wave2_proposal.gate_admitted is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["review_binding"].__setitem__(
            "reviewed_checkpoint_commit", "0" * 40
        ),
        lambda payload: payload["review_binding"].__setitem__("attachment_raw_sha256", "0" * 64),
        lambda payload: payload["review_binding"].__setitem__(
            "review_is_policy_input_not_authorization", False
        ),
        lambda payload: payload["user_authorization"].__setitem__(
            "exact_user_text", "I approve execution."
        ),
        lambda payload: payload["user_authorization"].__setitem__(
            "exact_user_text_sha256", "0" * 64
        ),
        lambda payload: payload["frozen_dependencies"].__setitem__("checkpoint_commit", "0" * 40),
        lambda payload: payload["frozen_dependencies"]["base_policy"].__setitem__(
            "operation_registry_hash", "0" * 64
        ),
    ],
    ids=[
        "checkpoint-review",
        "review-attachment",
        "review-as-authorization",
        "authorization-text",
        "authorization-hash",
        "frozen-checkpoint",
        "registry-hash",
    ],
)
def test_review_authorization_and_registry_binding_drift_fails_closed(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((EffectiveReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["effective_wave1"]["smoke_non_blocker_ids"].remove(
            "n31_optional_proof_execution_binding"
        ),
        lambda payload: payload["effective_wave1"]["smoke_blocker_ids"].__setitem__(
            1,
            "exact_six_wave1_implementation_dispatch_checker_anchor_bank_fixture_bindings",
        ),
        lambda payload: payload["source_authorization"].__setitem__("policy_file_sha256", "0" * 64),
        lambda payload: payload["source_authorization"].__setitem__(
            "policy_semantic_hash", "0" * 64
        ),
    ],
    ids=[
        "remove-optional-proof-binding-nonblocker",
        "restore-ambiguous-six-operation-blocker",
        "source-policy-raw-hash",
        "source-policy-semantic-hash",
    ],
)
def test_exact_smoke_dependency_and_source_policy_projection_drift_fails_closed(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((EffectiveReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["effective_wave1"]["operation_ids"].pop(),
        lambda payload: payload["effective_wave1"]["operation_ids"].append(
            "P01_ALPHA_RENAME_SINGLE_V1"
        ),
        lambda payload: payload["effective_wave1"]["effective_mechanism_ids"].append(
            "n31_proof_as_fake_second_mechanism"
        ),
        lambda payload: payload["effective_wave1"]["operation_to_mechanism"][-1].__setitem__(
            "mechanism_id", "n31_proof_as_fake_second_mechanism"
        ),
        lambda payload: payload["wave2_proposal"]["operation_ids"].pop(),
        lambda payload: payload["wave2_proposal"]["operation_ids"].append(
            "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1"
        ),
        lambda payload: payload["wave2_proposal"].__setitem__("status", "gate_admitted"),
        lambda payload: payload["wave2_proposal"].__setitem__("gate_admitted", True),
        lambda payload: payload["wave2_proposal"].__setitem__("dimension_gate_admitted", True),
        lambda payload: payload["wave2_proposal"].__setitem__("implementation_started", True),
        lambda payload: payload["wave2_proposal"].__setitem__("execution_authorized", True),
        lambda payload: payload["wave2_proposal"].__setitem__("ten_k_authorized", True),
    ],
    ids=[
        "wave1-missing-operation",
        "wave1-duplicate-operation",
        "fake-proof-mechanism",
        "proof-mechanism-split",
        "wave2-missing-operation",
        "wave2-duplicate-operation",
        "wave2-status-admitted",
        "wave2-gate-admitted",
        "wave2-dimension-admitted",
        "wave2-implementation-started",
        "wave2-execution-authorized",
        "wave2-10k-authorized",
    ],
)
def test_exact_wave_sets_and_statuses_fail_closed_on_drift(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((EffectiveReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["census_tiers"]["complete_cross_source_census"][
            "required_before"
        ].append("one_positive_one_negative_end_to_end_smoke"),
        lambda payload: payload["census_tiers"]["complete_cross_source_census"].__setitem__(
            "blocks_two_row_smoke", True
        ),
        lambda payload: payload["census_tiers"]["complete_cross_source_census"].__setitem__(
            "blocks_approximately_100_root_gate", True
        ),
        lambda payload: payload["census_tiers"]["selected_wave_sampling_frame_census"].__setitem__(
            "blocks_approximately_100_root_gate", False
        ),
        lambda payload: payload["census_tiers"]["smoke_micro_census"].__setitem__(
            "selection_pool_hash_bound", False
        ),
        lambda payload: payload["census_tiers"]["smoke_micro_census"].__setitem__(
            "selection_rule", "first_seen_root"
        ),
        lambda payload: payload["census_tiers"]["complete_cross_source_census"][
            "required_before"
        ].remove("multi_million_feasibility_claim"),
        lambda payload: payload["census_tiers"]["complete_cross_source_census"][
            "requirements"
        ].remove("raw_theorem_or_lemma_counts"),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_unavailability_blocks_rubric", True
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_unavailability_blocks_smoke", True
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_unavailability_outcome", "wave_not_ready"
        ),
        lambda payload: payload["n31_lane_contract"]["proof_eligibility_formula"].pop(),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_eligibility_is_project_and_root_scoped", False
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_unavailability_is_operation_failure", True
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_unavailability_blocks_wave", True
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_unavailability_blocks_conformance", True
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_unavailability_blocks_approximately_100_root_gate", True
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_unavailability_blocks_other_operations", True
        ),
    ],
    ids=[
        "full-census-blocks-smoke",
        "full-census-block-boolean",
        "full-census-blocks-100-root",
        "sampling-census-not-required",
        "micro-pool-not-hash-bound",
        "nondeterministic-smoke-selection",
        "full-census-omits-multi-million-claim",
        "full-census-omits-raw-counts",
        "proof-blocks-rubric",
        "proof-blocks-smoke",
        "proof-unavailable-wave-failure",
        "partial-proof-eligibility-formula",
        "proof-not-project-and-root-scoped",
        "proof-absence-is-operation-failure",
        "proof-absence-blocks-wave",
        "proof-absence-blocks-conformance",
        "proof-absence-blocks-100-root",
        "proof-absence-blocks-other-operation",
    ],
)
def test_census_and_optional_proof_coupling_contradictions_are_rejected(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((EffectiveReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["n31_lane_contract"].__setitem__("same_parent_root_pool", False),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "independent_root_sampling_forbidden", False
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_counts_against_parent_semantic_cap", False
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_counts_as_distinct_semantic_mechanism", True
        ),
        lambda payload: payload["n31_lane_contract"]["duplicate_invariants"].__setitem__(
            "proof_may_emit_additional_core_pair", True
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "proof_maximum_retained_share", 0.02
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "shared_correlation_group_id", "corr_n31_proof_only"
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "parent_production_admission_required", False
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "natural_cap_denominator", "proof_lane_population"
        ),
        lambda payload: payload["n31_lane_contract"].__setitem__(
            "parent_semantic_union_key", "operation_id"
        ),
        lambda payload: payload["n31_lane_contract"]["duplicate_invariants"].__setitem__(
            "model_facing_pair_multiplicity_maximum", 2
        ),
    ],
    ids=[
        "different-root-pool",
        "independent-root-sampling",
        "not-counted-under-parent-cap",
        "fake-distinct-mechanism",
        "additive-proof-volume",
        "proof-cap-exceeds-parent",
        "different-correlation-group",
        "no-parent-production-dependency",
        "wrong-cap-denominator",
        "additive-parent-sum-key",
        "duplicate-core-proof-row",
    ],
)
def test_nproof_nesting_contradictions_are_rejected(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((EffectiveReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["effective_wave1"]["dynamic_gate_accounting"].__setitem__(
            "current_total_operation_project_cell_count", 24
        ),
        lambda payload: payload["effective_wave1"]["dynamic_gate_accounting"].__setitem__(
            "current_fixture_count", 48
        ),
        lambda payload: payload["effective_wave1"]["dynamic_gate_accounting"].__setitem__(
            "approximate_independent_root_pool_target", 600
        ),
        lambda payload: payload["effective_wave1"]["dynamic_gate_accounting"].__setitem__(
            "proof_additional_independent_root_pool_count", 100
        ),
        lambda payload: payload["n31_lane_contract"]["proof_availability_by_project"][0].update(
            {"status": "unknown", "proof_eligible": True}
        ),
        lambda payload: payload["n31_lane_contract"]["proof_eligible_projects"].append(
            "compiler_data"
        ),
    ],
    ids=[
        "unknown-proof-counted-as-four-cells",
        "unknown-proof-counted-as-eight-fixtures",
        "proof-counted-as-sixth-root-pool",
        "proof-adds-independent-root-pool",
        "unknown-proof-marked-eligible",
        "project-opened-without-proof-evidence",
    ],
)
def test_optional_proof_scope_and_dynamic_accounting_contradictions_are_rejected(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((EffectiveReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: _source(payload, "compiler_data").__setitem__("publication_eligible", True),
        lambda payload: _source(payload, "mathlib").__setitem__("internal_gate_eligible", False),
        lambda payload: _source(payload, "cslib").__setitem__(
            "redistribution_review_complete", True
        ),
        lambda payload: _source(payload, "physlib").__setitem__("pilot_eligible", False),
        lambda payload: _source(payload, "compiler_data").__setitem__(
            "repository", "somebody_else/compiler_data"
        ),
        lambda payload: _source(payload, "compiler_data").__setitem__("revision", "0" * 40),
        lambda payload: _source(payload, "cslib").__setitem__("spdx_id", "MIT"),
        lambda payload: _source(payload, "mathlib").__setitem__(
            "authorization_evidence_sha256", "0" * 64
        ),
    ],
    ids=[
        "publication-without-review",
        "apache-source-unnecessarily-blocked",
        "unearned-redistribution-review",
        "authorized-source-census-blocked",
        "compiler-owner-namespace",
        "compiler-revision",
        "apache-spdx",
        "apache-license-evidence",
    ],
)
def test_source_authorization_contradictions_are_rejected(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((EffectiveReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    ("project_id", "identity_updates", "license_updates"),
    [
        ("compiler_data", {"repository": "somebody_else/compiler_data"}, {}),
        ("compiler_data", {"revision": "0" * 40}, {}),
        ("cslib", {"revision": "0" * 40}, {}),
        ("cslib", {}, {"spdx_id": "MIT"}),
        ("mathlib", {}, {"evidence_sha256": "0" * 64}),
    ],
    ids=[
        "compiler-owner-namespace",
        "compiler-revision",
        "apache-revision",
        "apache-spdx",
        "apache-license-evidence",
    ],
)
def test_source_authorization_projection_rejects_frozen_census_identity_drift(
    project_id: str,
    identity_updates: dict[str, Any],
    license_updates: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_census = load_wave1_source_census()
    sources = list(loaded_census.config.sources)
    index = next(i for i, source in enumerate(sources) if source.source_id == project_id)
    source = sources[index]
    sources[index] = source.model_copy(
        update={
            "identity": source.identity.model_copy(update=identity_updates),
            "license": source.license.model_copy(update=license_updates),
        }
    )
    drifted = replace(
        loaded_census,
        config=loaded_census.config.model_copy(update={"sources": tuple(sources)}),
    )
    monkeypatch.setattr(effective_module, "load_wave1_source_census", lambda _root: drifted)

    with pytest.raises(
        EffectiveReadinessError,
        match=r"source.*projection|source.*identity|Apache/revision/license binding drift",
    ):
        load_effective_wave_state()


def test_source_use_namespace_policy_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_load_yaml_mapping = load_yaml_mapping

    def _drift_source_use(path: Path) -> dict[str, Any]:
        payload = real_load_yaml_mapping(path)
        if path.as_posix().endswith("policies/source_use_v2.yaml"):
            payload["scope"]["namespace"] = "somebody_else/*"
        return payload

    monkeypatch.setattr(effective_module, "load_yaml_mapping", _drift_source_use)
    with pytest.raises(EffectiveReadinessError, match="source_use_v2 semantic hash drift"):
        load_effective_wave_state()


def test_unknown_fields_and_duplicate_yaml_keys_fail_closed(tmp_path: Path) -> None:
    payload = _payload()
    payload["silent_execution_override"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EffectiveWaveReadinessOverlay.model_validate(payload)

    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(
        "schema_version: 1\nschema_version: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(DuplicateKeyError, match="duplicate key"):
        load_config(duplicate, EffectiveWaveReadinessOverlay)


def test_loader_rejects_effective_overlay_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(effective_module, "EXPECTED_EFFECTIVE_CONFIG_SEMANTIC_HASH", "0" * 64)
    with pytest.raises(
        EffectiveReadinessError,
        match=r"canonical hash drift|semantic hash drift",
    ):
        load_effective_wave_state()


def test_loader_rejects_effective_overlay_raw_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(effective_module, "EXPECTED_EFFECTIVE_CONFIG_FILE_SHA256", "0" * 64)
    with pytest.raises(EffectiveReadinessError, match="raw-file hash drift"):
        load_effective_wave_state()


def test_loader_rejects_frozen_dependency_file_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_hash_file = hash_file
    drift_path = next(iter(FROZEN_V032_FILE_HASHES))

    def _drift(path: Path) -> str:
        if path.as_posix().endswith(drift_path):
            return "0" * 64
        return real_hash_file(path)

    monkeypatch.setattr(effective_module, "hash_file", _drift)
    with pytest.raises(EffectiveReadinessError, match=r"frozen dependency.*drift"):
        load_effective_wave_state()


def test_loader_rejects_an_alternate_unfrozen_overlay_path(tmp_path: Path) -> None:
    alternate = tmp_path / "effective.yaml"
    alternate.write_text(
        (find_repo_root() / DEFAULT_EFFECTIVE_READINESS_PATH).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(EffectiveReadinessError, match="path differs"):
        load_effective_wave_state(path=alternate)


def test_module_is_lean_free_and_has_no_execution_surface() -> None:
    source = Path(effective_module.__file__).read_text(encoding="utf-8")

    assert "from leanfaith.lean" not in source
    assert "import leanfaith.lean" not in source
    assert "LeanInteract" not in source
    assert "subprocess" not in source
    assert "lake env lean" not in source
    config = _loaded().config
    assert not hasattr(config, "execute")
    assert not hasattr(config, "transform")
    assert not hasattr(config, "emit_rows")
