"""Lean-free invariants for SFT1 Wave 1 implementation readiness 0.3.4."""

from __future__ import annotations

import copy
import re
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

import leanfaith.sft1.wave1_readiness as readiness
from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import DuplicateKeyError, load_yaml_mapping
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.composition_policy import EXPECTED_CACHE_KEY_FIELDS

ZERO_SHA256 = "0" * 64
MUTATED_SHA256 = "f" * 64
WAVE2_OPERATION_ID = "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1"


@cache
def _loaded() -> readiness.LoadedWave1ImplementationReadiness:
    return readiness.load_wave1_implementation_readiness()


def _readiness_payload() -> dict[str, Any]:
    return copy.deepcopy(_loaded().config.model_dump(mode="json"))


def _bank_payload() -> dict[str, Any]:
    return copy.deepcopy(_loaded().banks.config.model_dump(mode="json"))


def _fixture_payload() -> dict[str, Any]:
    return copy.deepcopy(_loaded().fixtures.config.model_dump(mode="json"))


def _validate_readiness_payload(payload: dict[str, Any]) -> None:
    loaded = _loaded()
    config = readiness.Wave1ImplementationReadiness.model_validate(payload)
    readiness.validate_wave1_implementation_readiness(
        config,
        root=find_repo_root(),
        parent=loaded.parent,
        banks=loaded.banks,
        fixtures=loaded.fixtures,
    )


def _validate_bank_payload(payload: dict[str, Any]) -> None:
    loaded = _loaded()
    config = readiness.Wave1OperationBanks.model_validate(payload)
    readiness.validate_operation_banks(config, loaded.parent)


def _validate_fixture_payload(payload: dict[str, Any]) -> None:
    loaded = _loaded()
    config = readiness.Wave1FixtureSet.model_validate(payload)
    readiness.validate_fixture_set(config, loaded.parent)


def _set_path(payload: dict[str, Any], path: str, value: object) -> None:
    parts = path.split(".")
    cursor: Any = payload
    for part in parts[:-1]:
        cursor = cursor[int(part)] if part.isdigit() else cursor[part]
    cursor[parts[-1]] = value


def _registry_operations(
    loaded: readiness.LoadedWave1ImplementationReadiness,
) -> dict[str, Any]:
    policy = loaded.parent.loaded_admission.loaded_base_policy.config
    return {
        operation.operation_id: operation
        for operation in (*policy.operations, *policy.synthetic_track.operations)
    }


def test_checked_in_readiness_loads_and_every_artifact_hash_is_frozen() -> None:
    loaded = _loaded()
    root = find_repo_root()

    expected = (
        (
            readiness.DEFAULT_OPERATION_BANK_PATH,
            readiness.EXPECTED_OPERATION_BANK_FILE_SHA256,
            readiness.EXPECTED_OPERATION_BANK_SEMANTIC_HASH,
            loaded.banks.config_hash,
        ),
        (
            readiness.DEFAULT_FIXTURE_PATH,
            readiness.EXPECTED_FIXTURE_FILE_SHA256,
            readiness.EXPECTED_FIXTURE_SEMANTIC_HASH,
            loaded.fixtures.config_hash,
        ),
        (
            readiness.DEFAULT_IMPLEMENTATION_READINESS_PATH,
            readiness.EXPECTED_IMPLEMENTATION_READINESS_FILE_SHA256,
            readiness.EXPECTED_IMPLEMENTATION_READINESS_SEMANTIC_HASH,
            loaded.config_hash,
        ),
    )
    for path, raw_hash, semantic_hash, observed_semantic_hash in expected:
        assert raw_hash != ZERO_SHA256
        assert semantic_hash != ZERO_SHA256
        assert hash_file(root / path) == raw_hash
        assert observed_semantic_hash == semantic_hash


def test_parent_freeze_and_complete_46_operation_registry_are_preserved() -> None:
    loaded = _loaded()
    parent = loaded.config.parent_freeze
    operations = _registry_operations(loaded)

    assert parent.commit == readiness.EXPECTED_PARENT_COMMIT
    assert parent.tree == readiness.EXPECTED_PARENT_TREE
    assert parent.preserves_complete_46_operation_registry is True
    assert len(operations) == 46
    assert len(set(operations)) == 46
    assert set(readiness.EXPECTED_PRIMARY_OPERATION_IDS) < set(operations)
    assert readiness.EXPECTED_OPTIONAL_PROOF_OPERATION_ID in operations


def test_exact_five_primary_mechanisms_and_optional_proof_scope() -> None:
    loaded = _loaded()
    config = loaded.config
    bundles = config.primary_bundles
    proof = config.optional_n31_proof_adapter

    assert tuple(config.authorization_scope.primary_operation_ids) == (
        readiness.EXPECTED_PRIMARY_OPERATION_IDS
    )
    assert tuple(bundle.operation_id for bundle in bundles) == (
        readiness.EXPECTED_PRIMARY_OPERATION_IDS
    )
    assert len({bundle.mechanism_id for bundle in bundles}) == 5
    assert config.authorization_scope.semantic_mechanism_count == 5
    assert proof.operation_id == readiness.EXPECTED_OPTIONAL_PROOF_OPERATION_ID
    assert proof.operation_id not in {bundle.operation_id for bundle in bundles}
    assert proof.status == "optional_not_implemented"
    assert proof.implementation_source_path is None
    assert proof.checker_symbol is None
    assert proof.required_for_primary_implementation_readiness is False
    assert proof.blocks_parent is False
    assert proof.blocks_wave is False
    assert proof.blocks_smoke is False
    assert proof.blocks_conformance is False
    assert proof.blocks_approximately_100_root_gate is False
    assert proof.optional_per_project is True
    assert proof.optional_per_root is True
    assert proof.unavailable_disposition == "not_in_scope_for_n_proof"
    assert proof.candidate_truth_when_unavailable == "unknown"
    assert proof.parent_candidate_and_root_pool_reuse_required is True
    assert proof.sidecar_upgrade_only is True
    assert proof.proof_upgrade_may_emit_additional_pair is False
    assert proof.counts_as_sixth_semantic_mechanism is False
    assert proof.counts_against_own_and_parent_caps is True

    bank_proof = loaded.banks.config.optional_n31_proof_adapter
    assert bank_proof.status == "optional_not_implemented"
    assert bank_proof.independent_mutation_implementation_forbidden is True
    assert bank_proof.parent_candidate_reuse_required is True
    assert bank_proof.parent_root_pool_only is True
    assert bank_proof.blocks_parent_or_wave_readiness is False
    assert bank_proof.exact_source_proof_required_if_attempted is True
    assert bank_proof.exact_candidate_refutation_required_if_attempted is True
    assert bank_proof.failed_search_is_evidence is False
    assert bank_proof.candidate_truth_when_absent == "unknown"
    assert bank_proof.candidate_truth_when_complete_proof_receipt_passes == "refuted"
    assert bank_proof.counts_against_own_and_parent_caps is True
    assert bank_proof.may_emit_additional_model_facing_pair is False


def test_fixture_matrix_is_exact_unexecuted_5_by_4_by_2_product() -> None:
    fixtures = _loaded().fixtures.config
    matrix = fixtures.fixture_matrix

    assert len(matrix) == 40
    assert len({item.fixture_id for item in matrix}) == 40
    assert Counter(
        (item.operation_id, item.project_id, item.fixture_kind) for item in matrix
    ) == Counter(
        (operation_id, project_id, fixture_kind)
        for operation_id in readiness.EXPECTED_PRIMARY_OPERATION_IDS
        for project_id in readiness.EXPECTED_PROJECT_IDS
        for fixture_kind in ("success", "adversarial_rejection")
    )
    assert all(item.live_verified is False for item in matrix)
    assert all(item.receipt_sha256 is None for item in matrix)
    assert fixtures.optional_n31_proof_fixture_count == 0
    assert all(
        item.operation_id != readiness.EXPECTED_OPTIONAL_PROOF_OPERATION_ID
        for item in (*fixtures.templates, *fixtures.fixture_matrix)
    )
    assert tuple(context.project_id for context in fixtures.project_contexts) == (
        readiness.EXPECTED_PROJECT_IDS
    )
    assert all(
        not context.namespace_context and not context.open_context and not context.scoped_context
        for context in fixtures.project_contexts
    )


def test_n31_fixtures_bind_named_reachability_assignments_and_exact_mode() -> None:
    fixtures = _loaded().fixtures.config
    n31_templates = [
        template
        for template in fixtures.templates
        if template.operation_id == "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
    ]
    assert len(n31_templates) == 2
    expected_assignments = {
        "success": (
            (0, "x", "(1 : Nat)"),
            (1, "hx", "Nat.one_ne_zero"),
        ),
        "adversarial_rejection": (
            (0, "x", "(1 : Nat)"),
            (1, "hx", "Nat.one_ne_zero"),
            (2, "hpos", "Nat.zero_lt_succ 0"),
        ),
    }
    for template in n31_templates:
        selector = template.selector
        assert isinstance(selector, readiness.GuardAndTargetSelector)
        assert selector.reachability_mode_id == (
            "explicit_telescope_witness_and_retained_hypothesis_proofs"
        )
        assert (
            tuple(
                (
                    assignment.binder_index,
                    assignment.binder_name,
                    assignment.candidate_term,
                )
                for assignment in selector.future_telescope_assignments
            )
            == expected_assignments[template.fixture_kind]
        )
        assert all(
            assignment.live_elaboration_verified is False
            for assignment in selector.future_telescope_assignments
        )
        assert "(x : Nat)" in template.reference_term
        assert "(hx : x ≠ 0)" in template.reference_term
        if template.fixture_kind == "adversarial_rejection":
            assert "(hpos : 0 < x)" in template.reference_term
    assert fixtures.matrix_contract.n31_reachability_candidate_terms_bound_by_fixture_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("binder_name", "changed_name"),
        ("candidate_term", "changed_term"),
        ("reachability_mode_id", "changed_mode"),
    ],
)
def test_n31_reachability_payload_mutations_reject_typed_fixture(field: str, value: str) -> None:
    payload = _fixture_payload()
    operation_id = "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
    templates = payload["templates"]
    template_index = next(
        index
        for index, template in enumerate(templates)
        if template["operation_id"] == operation_id and template["fixture_kind"] == "success"
    )
    selector = templates[template_index]["selector"]
    if field == "reachability_mode_id":
        selector[field] = value
    else:
        selector["future_telescope_assignments"][0][field] = value
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_fixture_payload(payload)


def test_per_operation_fixture_hash_is_sensitive_to_referenced_template_text() -> None:
    fixtures = _loaded().fixtures.config
    operation_id = "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
    original = readiness.fixture_operation_bundle_hash(fixtures, operation_id)
    templates = list(fixtures.templates)
    template_index = next(
        index
        for index, template in enumerate(templates)
        if template.operation_id == operation_id and template.fixture_kind == "success"
    )
    template = templates[template_index]
    templates[template_index] = template.model_copy(
        update={"reference_term": template.reference_term + " "}
    )
    changed_fixtures = fixtures.model_copy(update={"templates": tuple(templates)})
    assert readiness.fixture_operation_bundle_hash(changed_fixtures, operation_id) != original


def test_every_primary_bundle_replays_registry_anchor_bank_fixture_and_derived_hashes() -> None:
    loaded = _loaded()
    config = loaded.config
    operations = _registry_operations(loaded)
    source = config.authored_artifacts.lean_source
    source_text = (find_repo_root() / source.path).read_text(encoding="utf-8")

    assert hash_file(find_repo_root() / source.path) == source.file_sha256
    assert (
        sha256_hex(readiness.import_stripped_preamble(source_text).encode("utf-8"))
        == source.import_stripped_preamble_sha256
    )
    for bundle in config.primary_bundles:
        operation = operations[bundle.operation_id]
        assert hash_canonical(operation.model_dump(mode="json")) == (bundle.registry_entry_hash)
        assert operation.anchor.ref == bundle.anchor_ref
        assert operation.anchor.schema_lemma_procedure_hash == bundle.anchor_hash
        assert (
            readiness.operation_bank_entry_hash(loaded.banks.config, bundle.operation_id)
            == bundle.operation_bank_entry_hash
        )
        assert (
            readiness.fixture_operation_bundle_hash(loaded.fixtures.config, bundle.operation_id)
            == bundle.fixture_aggregate_hash
        )
        dispatch_hash = readiness.compute_dispatch_binding_hash(
            bundle,
            lean_source_file_sha256=source.file_sha256,
            bank_semantic_hash=loaded.banks.config_hash,
        )
        checker_hash = readiness.compute_checker_binding_hash(
            bundle,
            lean_source_file_sha256=source.file_sha256,
        )
        anchor_hash = readiness.compute_anchor_binding_hash(bundle)
        assert dispatch_hash == bundle.dispatch_binding_hash
        assert checker_hash == bundle.checker_binding_hash
        assert anchor_hash == bundle.anchor_binding_hash
        assert (
            readiness.compute_authored_bundle_hash(
                bundle,
                lean_source_file_sha256=source.file_sha256,
                dispatch_binding_hash=dispatch_hash,
                checker_binding_hash=checker_hash,
                anchor_binding_hash=anchor_hash,
            )
            == bundle.authored_bundle_hash
        )
        assert bundle.operation_constructor.rsplit(".", maxsplit=1)[-1] in source_text


def test_cache_key_has_exact_30_field_order_and_hash_replay() -> None:
    fields = _loaded().config.cache_readiness.exact_ordered_key_fields
    assert fields == EXPECTED_CACHE_KEY_FIELDS
    assert len(fields) == 30
    assert tuple(readiness.Wave1CacheKey.model_fields) == EXPECTED_CACHE_KEY_FIELDS

    payload: dict[str, Any] = {
        "source_closed_expr_hash": "1" * 64,
        "candidate_closed_expr_hash": "2" * 64,
        "canonical_universe_profile_id": "goal_v1_first_occurrence_u_i_v1",
        "canonical_universe_profile_hash": (
            "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
        ),
        "source_expr_builder_version": "source-builder-v1",
        "candidate_expr_builder_version": "candidate-builder-v1",
        "lean_version": "v4.test",
        "project_id": "mathlib",
        "project_revision": "revision",
        "toolchain_revision": "toolchain",
        "imports_hash": "3" * 64,
        "options_hash": "4" * 64,
        "synthesized_instance_hashes": ["5" * 64],
        "operation_id": "P15_SWAP_IFF_SIDES_V1",
        "operation_registry_entry_hash": "6" * 64,
        "schema_lemma_procedure_hash": "7" * 64,
        "evidence_certificate_payload_hash": "8" * 64,
        "bank_resolved_lean_hash": "9" * 64,
        "transparency": "reducible",
        "allowed_axiom_profile": "classical_v1",
        "typed_meta_validator_version": "validator-v1",
        "evidence_replay_version": "replay-v1",
        "evaluation_blocklist_sha256": (
            "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
        ),
        "repr_replacement_commit": "176a783842c5a73b84413dfa8347670608b615d9",
        "render_context_id": "goal_v1_render_context_v1",
        "render_context_hash": ("5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"),
        "renderer_api_hash": ("c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"),
        "repr_spec_hash": ("68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"),
        "environment_fingerprint_hash": "a" * 64,
        "policy_config_hash": "b" * 64,
    }
    key = readiness.Wave1CacheKey.model_validate(payload)
    expected = hash_canonical(key.model_dump(mode="json"))
    assert readiness.compute_wave1_cache_key(key) == expected
    assert readiness.compute_wave1_cache_key_hash(key) == expected


def test_p01_identity_collision_remains_an_open_fail_closed_blocker() -> None:
    loaded = _loaded()
    bank = loaded.banks.config.p01_identity_blocker
    state = loaded.config.p01_identity_blocker

    assert bank.status == state.status == "open_fail_closed"
    assert bank.observed_reference_closed_expr_hash == (bank.observed_candidate_closed_expr_hash)
    assert bank.observed_render_hashes_are_distinct is True
    assert bank.frozen_composition_rejects_repeated_closed_expr_hashes is True
    assert bank.binder_aware_delta_may_override_frozen_duplicate_rule is False
    assert bank.blocks_p01_implementation_readiness is True
    assert state.reference_and_candidate_closed_expr_hash_equal is True
    assert state.frozen_repeated_closed_expr_hash_rejection_still_applies is True
    assert state.additive_fingerprint_overrides_frozen_rule is False
    assert state.blocks_operation_implementation_readiness is True


def test_n31_target_bank_is_disjoint_authored_unverified_and_fail_closed() -> None:
    loaded = _loaded()
    bank = loaded.banks.config.n31_guard_target_bank
    state = loaded.config.n31_target_bank_state

    assert bank.status == "authored_candidates_require_live_resolution"
    assert bank.all_selectable_entries_live_resolved is False
    assert bank.all_retained_contradiction_patterns_live_resolved is False
    assert bank.usable_for_gate_execution is False
    assert all(entry.live_resolved is False for entry in bank.shape_entries)
    assert all(entry.live_resolved is False for entry in bank.retained_contradiction_patterns)
    assert len(bank.shape_entries) == 5
    assert len(bank.retained_contradiction_patterns) == 5
    selectable_ids = {entry.shape_id for entry in bank.shape_entries}
    retained_ids = {entry.shape_id for entry in bank.retained_contradiction_patterns}
    assert selectable_ids.isdisjoint(retained_ids)
    assert bank.retained_contradiction_patterns_are_not_selectable_or_dispatchable
    assert bank.retained_prop_match_cardinality == (
        "exactly_one_across_selectable_and_contradiction_patterns_v1"
    )
    assert bank.unknown_or_ambiguous_retained_prop_disposition == (
        "n31RetainedContextUnknownOrAmbiguous"
    )
    assert bank.unknown_or_ambiguous_retained_prop_is_typed_not_applicable
    for selectable in bank.shape_entries:
        assert set(selectable.contradiction_shape_ids) <= retained_ids
    for retained in bank.retained_contradiction_patterns:
        assert retained.contradicts_guard_shape_id in selectable_ids

    assert state.selectable_guard_target_candidate_entries_authored is True
    assert state.retained_contradiction_candidate_entries_authored is True
    assert state.bank_semantic_hash_binds_both_inventories is True
    assert (
        state.live_selectable_and_contradiction_name_arity_role_instance_resolution_complete
        is False
    )
    assert state.unknown_or_ambiguous_disposition == "typed_not_applicable"
    assert state.unknown_or_ambiguous_retained_prop_reason == (
        "n31RetainedContextUnknownOrAmbiguous"
    )
    assert state.blocks_n31_implementation_readiness is True


def test_n31_runtime_identity_inventory_is_exactly_empty_and_blocks_activation() -> None:
    loaded = _loaded()
    contract = loaded.banks.config.n31_runtime_admission_contract
    state = loaded.config.n31_target_bank_state
    source_binding = loaded.config.authored_artifacts.lean_source
    expected_symbol = "LeanFaith.SFT1.Wave1.admittedN31BankIdentitiesV0_3_4"
    expected_pin_fields = (
        "projectId",
        "bankId",
        "resolvedLeanHash",
        "resolutionReceiptHash",
    )

    assert contract.source_admission_symbol == expected_symbol
    assert source_binding.n31_bank_admission_symbol == expected_symbol
    assert contract.admitted_resolved_bank_identity_count == 0
    assert contract.admitted_resolved_bank_identities == ()
    assert contract.current_runtime_activation_authorized is False
    assert contract.empty_admission_blocks_dispatch_and_discovery is True
    assert contract.absent_bank_failure_reason == "n31BankMissing"
    assert contract.unadmitted_bank_failure_reason == "n31BankInvalid"
    assert contract.future_activation_requires_separate_user_authorization is True
    assert contract.future_identity_pin_field_order == expected_pin_fields
    assert contract.exact_project_bank_resolved_lean_hash_resolution_receipt_pin_required is True
    assert contract.in_process_exact_full_typed_bank_hash_verification_required is True
    assert contract.in_process_exact_full_typed_bank_hash_verifier_bound is False
    assert contract.identity_membership_is_not_a_substitute_for_bank_hash_verification
    assert contract.verification_must_precede_dispatch_discovery_and_replay

    assert state.source_admission_symbol == expected_symbol
    assert state.runtime_admitted_resolved_bank_identity_count == 0
    assert state.runtime_admitted_resolved_bank_identities == ()
    assert state.empty_admission_blocks_dispatch_and_discovery is True
    assert state.current_runtime_activation_authorized is False
    assert state.future_activation_requires_separate_user_authorization is True
    assert state.future_identity_pin_field_order == expected_pin_fields
    assert state.in_process_exact_full_typed_bank_hash_verification_required is True
    assert state.in_process_exact_full_typed_bank_hash_verifier_bound is False
    assert state.identity_membership_is_not_a_substitute_for_bank_hash_verification
    assert state.operation_bank_semantic_hash_binds_n31_runtime_contracts is True
    assert state.operation_bank_artifact_semantic_hash == loaded.banks.config_hash


def test_n31_certificate_binds_full_typed_bank_reachability_and_replay_context() -> None:
    loaded = _loaded()
    contract = loaded.banks.config.n31_certificate_binding_contract
    state = loaded.config.n31_target_bank_state

    assert contract.source_certificate_symbol == "LeanFaith.SFT1.Wave1.N31RubricCertificate"
    assert contract.source_bank_symbol == "LeanFaith.SFT1.Wave1.N31TargetBank"
    assert contract.source_reachability_symbol == ("LeanFaith.SFT1.Wave1.N31ReachabilityEvidence")
    assert contract.full_typed_bank_embedded_in_certificate is True
    assert contract.full_typed_bank_scope == (
        "identity_entries_retained_patterns_implications_contradictions_v1"
    )
    assert contract.full_reachability_evidence_embedded_in_certificate is True
    assert contract.full_reachability_scope == ("mode_guard_ordinal_ordered_assignment_exprs_v1")
    assert contract.reachability_assignment_equality == "ordered_Expr.equal_v1"
    assert contract.replay_requires_supplied_bank_and_reachability_exact_equal is True
    assert contract.mismatch_failure_reason == "replayContextMismatch"
    assert state.certificate_binds_full_typed_bank is True
    assert state.certificate_binds_full_reachability_assignment is True
    assert state.replay_context_mismatch_reason == "replayContextMismatch"


def test_n31_runtime_structural_constraints_are_all_fail_closed() -> None:
    loaded = _loaded()
    constraints = loaded.banks.config.n31_runtime_structural_constraints
    state = loaded.config.n31_target_bank_state

    assert constraints.selectable_guard_role_and_instance_indices_disjoint is True
    assert constraints.selectable_target_role_and_instance_indices_disjoint is True
    assert constraints.selectable_guard_instance_or_type_indices_nonempty is True
    assert constraints.selectable_target_instance_or_type_indices_nonempty is True
    assert constraints.retained_contradiction_role_and_instance_paths_disjoint is True
    assert constraints.retained_contradiction_instance_or_type_paths_nonempty is True
    assert constraints.exact_expr_constraints_must_be_clean_and_closed is True
    assert constraints.unresolved_symbolic_candidate_bank_is_runtime_inadmissible is True
    assert constraints.resolved_runtime_bank_must_satisfy_all_constraints is True
    assert state.selectable_guard_and_target_role_instance_indices_disjoint is True
    assert state.selectable_guard_and_target_instance_or_type_constraints_nonempty is True
    assert state.retained_contradiction_role_instance_paths_disjoint is True
    assert state.retained_contradiction_instance_or_type_constraints_nonempty is True


def test_new_n31_runtime_blockers_remain_explicit() -> None:
    blockers = set(_loaded().config.verification_state.remaining_implementation_blockers)

    assert "n31_runtime_bank_identity_admission_requires_separate_user_authorization" in blockers
    assert "n31_in_process_exact_full_typed_bank_hash_verifier_binding" in blockers


def test_all_execution_gate_row_and_release_states_remain_false() -> None:
    loaded = _loaded()
    config = loaded.config
    scope = config.authorization_scope

    assert scope.lean_compilation_or_execution_allowed is False
    assert scope.transform_or_gate_execution_allowed is False
    assert scope.row_generation_or_emission_allowed is False
    assert scope.wave2_implementation_allowed is False
    assert scope.production_admission_allowed is False
    assert scope.ten_k_allowed is False
    assert scope.scale_allowed is False
    assert scope.publication_allowed is False
    assert all(
        bundle.lean_compile_verified is False
        and bundle.live_success_verified is False
        and bundle.live_adversarial_rejection_verified is False
        and bundle.implementation_ready is False
        and bundle.gate_execution_may_start is False
        and bundle.production_admitted is False
        and bundle.row_emission_authorized is False
        for bundle in config.primary_bundles
    )
    checks = (
        config.verification_state.lean_compile,
        config.verification_state.live_success_fixtures,
        config.verification_state.live_adversarial_rejections,
        config.verification_state.certificate_replay,
    )
    assert all(
        check.status == "not_run_unauthorized"
        and check.verified is False
        and check.receipt_sha256 is None
        for check in checks
    )
    assert config.verification_state.implementation_ready is False
    assert config.verification_state.gate_execution_may_start is False
    assert config.cache_readiness.central_cache_store_adapter_bound is False
    assert config.cache_readiness.central_cache_store_invoked is False
    assert config.cache_readiness.cache_replay_executed is False
    assert config.prohibitions.model_dump(mode="python") == {
        "lean_project_compilation_or_meta_execution_started": False,
        "transform_execution_started": False,
        "gate_execution_started": False,
        "census_processing_started": False,
        "rows_generated_or_emitted": False,
        "wave2_implementation_started": False,
        "production_admitted_operation_count": 0,
        "production_admitted_negative_count": 0,
        "ten_k_authorized": False,
        "scale_authorized": False,
        "publication_authorized": False,
        "shared_contract_modified": False,
    }


def test_exact_one_boundary_incident_is_preserved_without_scope_conflation() -> None:
    incidents = _loaded().config.boundary_incidents

    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.incident_id == "unauthorized_read_only_lean_print_prefix_v1"
    assert incident.command == "lean --print-prefix"
    assert incident.authorization_status == ("outside_authorized_scope_recorded_for_audit")
    assert incident.lean_executable_invoked is True
    assert incident.project_loaded is False
    assert incident.project_imported is False
    assert incident.project_compiled is False
    assert incident.meta_execution_started is False
    assert incident.transform_execution_started is False
    assert incident.gate_execution_started is False
    assert incident.row_generation_or_emission_started is False
    assert incident.files_or_artifacts_produced is False


@pytest.mark.parametrize(
    "case",
    ["removed", "duplicated", "renamed", "project_loaded", "meta_started"],
)
def test_boundary_incident_removal_duplication_or_scope_drift_rejects(case: str) -> None:
    payload = _readiness_payload()
    incidents = payload["boundary_incidents"]
    if case == "removed":
        incidents.clear()
    elif case == "duplicated":
        incidents.append(copy.deepcopy(incidents[0]))
    elif case == "renamed":
        incidents[0]["incident_id"] = "unknown_incident"
    elif case == "project_loaded":
        incidents[0]["project_loaded"] = True
    else:
        incidents[0]["meta_execution_started"] = True
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_readiness_payload(payload)


@pytest.mark.parametrize(
    "case",
    ["duplicate", "missing", "unknown", "wave2", "reordered"],
)
def test_primary_inventory_mutations_reject(case: str) -> None:
    payload = _readiness_payload()
    bundles = cast(list[dict[str, Any]], payload["primary_bundles"])
    if case == "duplicate":
        bundles[-1] = copy.deepcopy(bundles[0])
    elif case == "missing":
        bundles.pop()
    elif case == "unknown":
        bundles[0]["operation_id"] = "P99_UNKNOWN_V1"
    elif case == "wave2":
        bundles[0]["operation_id"] = WAVE2_OPERATION_ID
    else:
        bundles[0], bundles[1] = bundles[1], bundles[0]
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_readiness_payload(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("parent_freeze.commit", "0" * 40),
        ("parent_freeze.preserves_complete_46_operation_registry", False),
        ("user_authorization.exact_user_text", "not the user authorization"),
        ("user_authorization.exact_user_text_sha256", MUTATED_SHA256),
        ("primary_bundles.0.registry_entry_hash", MUTATED_SHA256),
        ("primary_bundles.0.anchor_hash", MUTATED_SHA256),
        ("primary_bundles.0.operation_bank_entry_hash", MUTATED_SHA256),
        ("primary_bundles.0.fixture_aggregate_hash", MUTATED_SHA256),
        ("primary_bundles.0.dispatch_binding_hash", MUTATED_SHA256),
        ("primary_bundles.0.checker_binding_hash", MUTATED_SHA256),
        ("primary_bundles.0.anchor_binding_hash", MUTATED_SHA256),
        ("primary_bundles.0.authored_bundle_hash", MUTATED_SHA256),
        ("authored_artifacts.lean_source.file_sha256", MUTATED_SHA256),
        ("n31_target_bank_state.bank_semantic_hash", MUTATED_SHA256),
    ],
)
def test_parent_authorization_and_hash_binding_mutations_reject(path: str, value: object) -> None:
    payload = _readiness_payload()
    _set_path(payload, path, value)
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_readiness_payload(payload)


@pytest.mark.parametrize(
    "field",
    [
        "required_for_primary_implementation_readiness",
        "blocks_parent",
        "blocks_wave",
        "blocks_smoke",
        "blocks_conformance",
        "blocks_approximately_100_root_gate",
        "proof_upgrade_may_emit_additional_pair",
    ],
)
def test_optional_n31_proof_cannot_become_a_blocker_or_independent_pair(
    field: str,
) -> None:
    payload = _readiness_payload()
    payload["optional_n31_proof_adapter"][field] = True
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_readiness_payload(payload)


def test_optional_n31_proof_cannot_gain_source_or_checker_bindings() -> None:
    for field, value in (
        ("implementation_source_path", "LeanFaith/Meta/SFT1/Wave1.lean"),
        ("checker_symbol", "LeanFaith.SFT1.Wave1.replayCertificate"),
    ):
        payload = _readiness_payload()
        payload["optional_n31_proof_adapter"][field] = value
        with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
            _validate_readiness_payload(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("optional_per_project", False),
        ("optional_per_root", False),
        ("unavailable_disposition", "proved"),
        ("candidate_truth_when_unavailable", "refuted"),
        ("parent_candidate_and_root_pool_reuse_required", False),
        ("sidecar_upgrade_only", False),
        ("counts_as_sixth_semantic_mechanism", True),
        ("counts_against_own_and_parent_caps", False),
    ],
)
def test_optional_n31_proof_semantics_cannot_drift(field: str, value: object) -> None:
    payload = _readiness_payload()
    payload["optional_n31_proof_adapter"][field] = value
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_readiness_payload(payload)


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "missing",
        "unknown",
        "wave2",
        "reordered",
        "registry_hash",
        "anchor_hash",
        "live",
        "executable",
        "p01_unblocked",
        "n31_live",
        "n31_retained_live",
        "n31_retained_selectable",
        "n31_unknown_allowed",
        "proof_blocks",
        "proof_independent_mutation",
        "proof_failed_search_evidence",
        "proof_additional_pair",
    ],
)
def test_operation_bank_mutations_reject(case: str) -> None:
    payload = _bank_payload()
    banks = cast(list[dict[str, Any]], payload["operation_banks"])
    if case == "duplicate":
        banks[-1] = copy.deepcopy(banks[0])
    elif case == "missing":
        banks.pop()
    elif case == "unknown":
        banks[0]["operation_id"] = "P99_UNKNOWN_V1"
    elif case == "wave2":
        banks[0]["operation_id"] = WAVE2_OPERATION_ID
    elif case == "reordered":
        banks[0], banks[1] = banks[1], banks[0]
    elif case == "registry_hash":
        banks[0]["registry_entry_hash"] = MUTATED_SHA256
    elif case == "anchor_hash":
        banks[0]["anchor_hash"] = MUTATED_SHA256
    elif case == "live":
        banks[0]["live_lean_verified"] = True
    elif case == "executable":
        banks[0]["executable"] = True
    elif case == "p01_unblocked":
        payload["p01_identity_blocker"]["blocks_p01_implementation_readiness"] = False
    elif case == "n31_live":
        payload["n31_guard_target_bank"]["all_selectable_entries_live_resolved"] = True
    elif case == "n31_retained_live":
        payload["n31_guard_target_bank"]["all_retained_contradiction_patterns_live_resolved"] = True
    elif case == "n31_retained_selectable":
        payload["n31_guard_target_bank"][
            "retained_contradiction_patterns_are_not_selectable_or_dispatchable"
        ] = False
    elif case == "n31_unknown_allowed":
        payload["n31_guard_target_bank"][
            "unknown_or_ambiguous_retained_prop_is_typed_not_applicable"
        ] = False
    elif case == "proof_blocks":
        payload["optional_n31_proof_adapter"]["blocks_parent_or_wave_readiness"] = True
    elif case == "proof_independent_mutation":
        payload["optional_n31_proof_adapter"]["independent_mutation_implementation_forbidden"] = (
            False
        )
    elif case == "proof_failed_search_evidence":
        payload["optional_n31_proof_adapter"]["failed_search_is_evidence"] = True
    else:
        payload["optional_n31_proof_adapter"]["may_emit_additional_model_facing_pair"] = True
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_bank_payload(payload)


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "missing",
        "unknown",
        "wave2",
        "reordered",
        "live",
        "receipt",
        "optional_proof_fixture",
        "nonminimal_context",
    ],
)
def test_fixture_matrix_mutations_reject(case: str) -> None:
    payload = _fixture_payload()
    matrix = cast(list[dict[str, Any]], payload["fixture_matrix"])
    if case == "duplicate":
        matrix[-1] = copy.deepcopy(matrix[0])
    elif case == "missing":
        matrix.pop()
    elif case == "unknown":
        matrix[0]["operation_id"] = "P99_UNKNOWN_V1"
    elif case == "wave2":
        matrix[0]["operation_id"] = WAVE2_OPERATION_ID
    elif case == "reordered":
        matrix[0], matrix[1] = matrix[1], matrix[0]
    elif case == "live":
        matrix[0]["live_verified"] = True
    elif case == "receipt":
        matrix[0]["receipt_sha256"] = MUTATED_SHA256
    elif case == "optional_proof_fixture":
        payload["optional_n31_proof_fixture_count"] = 1
    else:
        payload["project_contexts"][0]["namespace_context"] = ["LeanFaith"]
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_fixture_payload(payload)


def test_cache_field_order_mutation_rejects() -> None:
    payload = _readiness_payload()
    fields = payload["cache_readiness"]["exact_ordered_key_fields"]
    fields[0], fields[1] = fields[1], fields[0]
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_readiness_payload(payload)


@pytest.mark.parametrize(
    ("artifact", "status"),
    [
        ("readiness", "implementation_ready"),
        ("bank", "executable"),
        ("fixture", "live_verified"),
    ],
)
def test_top_level_status_mutations_reject(artifact: str, status: str) -> None:
    payload = {
        "readiness": _readiness_payload,
        "bank": _bank_payload,
        "fixture": _fixture_payload,
    }[artifact]()
    payload["status"] = status
    validator = {
        "readiness": _validate_readiness_payload,
        "bank": _validate_bank_payload,
        "fixture": _validate_fixture_payload,
    }[artifact]
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        validator(payload)


@pytest.mark.parametrize(
    "path",
    [
        "authorization_scope.lean_compilation_or_execution_allowed",
        "authorization_scope.transform_or_gate_execution_allowed",
        "authorization_scope.row_generation_or_emission_allowed",
        "authorization_scope.wave2_implementation_allowed",
        "authorization_scope.production_admission_allowed",
        "authorization_scope.ten_k_allowed",
        "authorization_scope.scale_allowed",
        "authorization_scope.publication_allowed",
        "primary_bundles.0.lean_compile_verified",
        "primary_bundles.0.live_success_verified",
        "primary_bundles.0.live_adversarial_rejection_verified",
        "primary_bundles.0.implementation_ready",
        "primary_bundles.0.gate_execution_may_start",
        "primary_bundles.0.production_admitted",
        "primary_bundles.0.row_emission_authorized",
        "verification_state.implementation_ready",
        "verification_state.gate_execution_may_start",
        "cache_readiness.central_cache_store_adapter_bound",
        "cache_readiness.central_cache_store_invoked",
        "cache_readiness.cache_replay_executed",
        "prohibitions.lean_project_compilation_or_meta_execution_started",
        "prohibitions.transform_execution_started",
        "prohibitions.gate_execution_started",
        "prohibitions.census_processing_started",
        "prohibitions.rows_generated_or_emitted",
        "prohibitions.wave2_implementation_started",
        "prohibitions.ten_k_authorized",
        "prohibitions.scale_authorized",
        "prohibitions.publication_authorized",
        "prohibitions.shared_contract_modified",
    ],
)
def test_unauthorized_state_mutations_reject(path: str) -> None:
    payload = _readiness_payload()
    _set_path(payload, path, True)
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_readiness_payload(payload)


@pytest.mark.parametrize(
    "path",
    [
        "prohibitions.production_admitted_operation_count",
        "prohibitions.production_admitted_negative_count",
    ],
)
def test_production_count_mutations_reject(path: str) -> None:
    payload = _readiness_payload()
    _set_path(payload, path, 1)
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        _validate_readiness_payload(payload)


@pytest.mark.parametrize("kind", ["readiness", "bank", "fixture"])
def test_unknown_fields_reject(kind: str) -> None:
    payload = {
        "readiness": _readiness_payload,
        "bank": _bank_payload,
        "fixture": _fixture_payload,
    }[kind]()
    payload["unexpected_field"] = "must fail closed"
    validator = {
        "readiness": _validate_readiness_payload,
        "bank": _validate_bank_payload,
        "fixture": _validate_fixture_payload,
    }[kind]
    with pytest.raises((ValidationError, readiness.Wave1ReadinessError)):
        validator(payload)


def test_duplicate_yaml_keys_reject_before_typed_loading(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text("schema_version: 1\nschema_version: 1\n", encoding="utf-8")
    with pytest.raises(DuplicateKeyError):
        load_yaml_mapping(duplicate)


def test_static_source_contains_no_execution_or_representation_escape_hatches() -> None:
    config = _loaded().config
    source = (find_repo_root() / config.authored_artifacts.lean_source.path).read_text(
        encoding="utf-8"
    )

    forbidden_patterns = (
        r"(?m)^\s*(?:#eval|#check|run_meta|theorem|axiom|example)\b",
        r"\bsorry\b",
        r"\b(?:Lean\.)?addDecl\b",
        r"\bmk(?:Axiom|Theorem)\b",
        r"import\s+LeanFaith\.Meta\.TransformEngine\b",
        r"\b(?:emitClosedProp|renderClosedProp)\b",
    )
    assert all(re.search(pattern, source) is None for pattern in forbidden_patterns)
    assert config.authored_artifacts.lean_source.imports_shared_transform_engine is False
    assert config.authored_artifacts.lean_source.owns_renderer_or_universe_canonicalizer is False


def test_static_source_encodes_strict_unknown_retained_context_rejection() -> None:
    config = _loaded().config
    source = (find_repo_root() / config.authored_artifacts.lean_source.path).read_text(
        encoding="utf-8"
    )

    assert "retainedContradictionPatterns : Array N31RetainedContradictionPattern" in source
    assert "matchingGuardShapeIds bank localType" in source
    assert "matchingRetainedContradictionShapeIds bank localType" in source
    assert "selectableShapeIds.size + contradictionShapeIds.size != 1" in source
    assert "hasUnknownOrAmbiguous := true" in source
    assert "return .n31RetainedContextUnknownOrAmbiguous" in source
    assert source.index("if hasUnknownOrAmbiguous then") < source.index(
        "if hasRetainedContradiction then"
    )


@pytest.mark.parametrize(
    ("old", "new", "replacement_count"),
    [
        (
            "def admittedN31BankIdentitiesV0_3_4 : Array N31BankIdentity := #[]",
            "def admittedN31BankIdentitiesV0_3_4 : Array N31BankIdentity := #[bankIdentity]",
            1,
        ),
        (
            "\n  bank : N31TargetBank\n  reachability : N31ReachabilityEvidence",
            "\n  bankPayload : N31TargetBank\n  reachability : N31ReachabilityEvidence",
            1,
        ),
        (
            "\n  bank : N31TargetBank\n  reachability : N31ReachabilityEvidence",
            "\n  bank : N31TargetBank\n  reachabilityPayload : N31ReachabilityEvidence",
            1,
        ),
        (
            "bank == value.bank && reachability == value.reachability",
            "bank == value.bank",
            1,
        ),
        (
            "if !certificate.contextMatches context then",
            "if false then",
            1,
        ),
        (
            "admittedN31BankIdentitiesV0_3_4.contains bank.identity",
            "true",
            1,
        ),
    ],
)
def test_n31_static_source_contract_mutations_reject(
    old: str,
    new: str,
    replacement_count: int,
) -> None:
    config = _loaded().config
    source = (find_repo_root() / config.authored_artifacts.lean_source.path).read_text(
        encoding="utf-8"
    )
    assert source.count(old) >= replacement_count
    mutated = source.replace(old, new, replacement_count)
    with pytest.raises(readiness.Wave1ReadinessError):
        readiness.validate_authored_source_text(mutated)


@pytest.mark.parametrize(
    "forbidden",
    [
        "\n#eval 1\n",
        "\n#check True\n",
        "\nrun_meta do pure ()\n",
        "\ntheorem candidate : True := True.intro\n",
        "\naxiom candidate : True\n",
        "\nexample : True := True.intro\n",
        "\ndef candidate : True := by sorry\n",
        "\ndef candidate := Lean.mkSorry (mkConst ``True) true\n",
        "\nLean.addDecl declaration\n",
        "\nLean.addAndCompile declaration\n",
        "\nLean.mkAxiom candidate\n",
        "\nLean.mkTheorem candidate\n",
        "\nLean.Declaration.axiomDecl declaration\n",
        "\nLean.Declaration.thmDecl declaration\n",
        "\nLean.Declaration.opaqueDecl declaration\n",
        "\ndef proof := synthInstance type\n",
        "\ndef proof := synthesizeSyntheticMVarsNoPostponing\n",
        "\ndef proof := runTactic syntax\n",
        "\nimport LeanFaith.Meta.TransformEngine\n",
        "\ndef copiedRenderer := LeanFaith.GoalV1.emitClosedProp\n",
        "\ndef copiedRenderer := LeanFaith.GoalV1.renderClosedProp\n",
        "\ndef rendered := ppExpr expression\n",
        "\ndef rendered := delaborate expression\n",
        "\ndef rendered := prettyPrint expression\n",
        "\ndef parsed := elabTerm syntax none\n",
        "\ndef parsed := elabType syntax\n",
        "\ndef parsed := runParserCategory env `term text\n",
    ],
)
def test_static_source_prohibition_mutations_reject(forbidden: str) -> None:
    source = "def safeMarker : Nat := 1\n" + forbidden
    with pytest.raises(readiness.Wave1ReadinessError):
        readiness.validate_authored_source_text(source)
