"""Fail-closed invariants for the design-only SFT1 revision 0.3.1 policy."""

from __future__ import annotations

import copy
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import leanfaith.sft1.composition_policy as policy_module
from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import DuplicateKeyError, load_config
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.composition_policy import (
    EXACT_CURRENT_USER_DECISION,
    EXPECTED_BANK_IDS,
    EXPECTED_BINDER_ELABORATION_CHECKS,
    EXPECTED_CACHE_KEY_FIELDS,
    EXPECTED_CANDIDATE_TERMINAL_DISPOSITION_COUNTERS,
    EXPECTED_CAP_ORDER,
    EXPECTED_CERTIFICATE_CLASS_BINDINGS,
    EXPECTED_CLAIM_ERASURE_GUARDS,
    EXPECTED_COMPOSITION_PRODUCTIONS,
    EXPECTED_CORRELATION_GROUPS,
    EXPECTED_CURRENT_WAVE_APPROXIMATE_ROOTS,
    EXPECTED_CURRENT_WAVE_FIXTURES,
    EXPECTED_CURRENT_WAVE_OPERATION_IDS,
    EXPECTED_CURRENT_WAVE_OPERATION_PROJECT_COMBINATIONS,
    EXPECTED_EFFECTIVE_DIVERSITY_GROUPS,
    EXPECTED_EMPTY_DOMAIN_PROFILES,
    EXPECTED_ENVIRONMENT_FINGERPRINT_FIELDS,
    EXPECTED_FAMILY_DISPOSITIONS,
    EXPECTED_FORBIDDEN_RENDER_FAILURE_MAPPINGS,
    EXPECTED_FULL_MATRIX_APPROXIMATE_ROOTS,
    EXPECTED_FULL_MATRIX_FIXTURES,
    EXPECTED_FULL_MATRIX_OPERATION_PROJECT_COMBINATIONS,
    EXPECTED_HUNDRED_ROOT_CONSERVATION_EQUATIONS,
    EXPECTED_HUNDRED_ROOT_COUNTER_DIMENSIONS,
    EXPECTED_HUNDRED_ROOT_COUNTER_FIELDS,
    EXPECTED_MUTUAL_EXCLUSION_OPERATION_IDS,
    EXPECTED_NEGATIVE_ADMISSIONS,
    EXPECTED_NEGATIVE_APPLICABILITY_BANKS,
    EXPECTED_NEGATIVE_PRODUCTION_DECISION_RECORD_FIELDS,
    EXPECTED_NEGATIVE_PROMOTION_MEASUREMENTS,
    EXPECTED_OPERATION_IDS,
    EXPECTED_OPERATION_REGISTRY_HASH,
    EXPECTED_P23_REGRESSIONS,
    EXPECTED_P23_SIDECAR_BINDINGS,
    EXPECTED_POLICY_CONFIG_HASH,
    EXPECTED_PRE_GATE_FAILURE_CLASSES,
    EXPECTED_PRE_GATE_FAILURE_DIMENSIONS,
    EXPECTED_PRE_GATE_FORBIDDEN_RENDER_SUBSTRINGS,
    EXPECTED_PRE_GATE_SIDECAR_BINDINGS,
    EXPECTED_PREVALIDATION_HASH_FIELDS,
    EXPECTED_REAL_GOAL_CASE_IDS,
    EXPECTED_RENDERER_API_HASH_PAYLOAD_FIELDS,
    EXPECTED_REPR_CONFIG_FILE_SHA256,
    EXPECTED_REPR_FREEZE_COMMIT,
    EXPECTED_REPR_IMPLEMENTATION_COMMIT,
    EXPECTED_REPR_IMPLEMENTATION_SET_HASH,
    EXPECTED_REPR_INJECTED_HELPER_SHA256,
    EXPECTED_REPR_LEAN_RENDERER_SHA256,
    EXPECTED_REPR_PYTHON_RENDERER_SHA256,
    EXPECTED_REPR_RENDER_CONTEXT_HASH,
    EXPECTED_REPR_RENDER_CONTEXT_ID,
    EXPECTED_REPR_RENDERER_API_HASH,
    EXPECTED_REPR_RENDERER_SEMANTIC_HASH,
    EXPECTED_REPR_SPEC_HASH,
    EXPECTED_REPR_UNIVERSE_PROFILE_HASH,
    EXPECTED_REPR_UNIVERSE_PROFILE_ID,
    EXPECTED_SIX_GOAL_EXECUTION_CONFIG_FILE_SHA256,
    EXPECTED_SIX_GOAL_EXECUTION_CONFIG_HASH,
    EXPECTED_SIX_GOAL_EXECUTION_CONFIG_PATH,
    EXPECTED_SIX_GOAL_GATE_CONFIG_FILE_SHA256,
    EXPECTED_SIX_GOAL_GATE_CONFIG_PATH,
    EXPECTED_SIX_GOAL_GATE_EFFECTIVE_CONFIG_HASH,
    EXPECTED_SIX_GOAL_HELPER_FILE_SHA256,
    EXPECTED_SIX_GOAL_HELPER_PREAMBLE_SHA256,
    EXPECTED_SIX_GOAL_HELPER_SOURCE_PATH,
    EXPECTED_SIX_GOAL_RECEIPT_FILE_SHA256,
    EXPECTED_SIX_GOAL_RECEIPT_HASH,
    EXPECTED_SIX_GOAL_RECEIPT_PATH,
    EXPECTED_STABLE_ROW_HASH_FIELDS,
    EXPECTED_STARTER_BANK_CONFIG_HASH,
    EXPECTED_STARTER_BANK_FILE_SHA256,
    EXPECTED_SYNTHETIC_OPERATION_IDS,
    CandidateTruth,
    ClosedPropValidation,
    EvidenceClass,
    F0Relation,
    F1ClaimRelation,
    LabelLane,
    OperationStatus,
    OperationTrack,
    RetainDisposition,
    RowEvidenceReceipt,
    SFT1CompositionPolicy,
    SFT1PolicyError,
    SFT1StarterBankSet,
    TerminalDispositionReason,
    load_sft1_composition_policy,
    validate_sft1_policy_bindings,
)


def _payload() -> dict[str, Any]:
    return copy.deepcopy(load_sft1_composition_policy().config.model_dump(mode="python"))


def _operation(payload: dict[str, Any], operation_id: str) -> dict[str, Any]:
    operations = payload["operations"]
    assert isinstance(operations, list | tuple)
    for operation in operations:
        assert isinstance(operation, dict)
        if operation["operation_id"] == operation_id:
            return operation
    raise AssertionError(f"operation not found: {operation_id}")


def test_checked_in_policy_and_banks_are_exact_and_unauthorized() -> None:
    first = load_sft1_composition_policy()
    replay = load_sft1_composition_policy()
    policy = first.config

    assert first.config_hash == replay.config_hash
    assert first.bank_config_hash == replay.bank_config_hash
    assert first.config_hash == EXPECTED_POLICY_CONFIG_HASH
    assert first.bank_config_hash == EXPECTED_STARTER_BANK_CONFIG_HASH
    assert hash_file(first.bank_path) == EXPECTED_STARTER_BANK_FILE_SHA256
    assert policy.policy_version == "0.3.1"
    assert policy.status == "awaiting_wave_1_gate_admission"
    assert policy.approval_recorded is False
    assert tuple(operation.operation_id for operation in policy.operations) == (
        EXPECTED_OPERATION_IDS
    )
    assert (
        tuple(operation.operation_id for operation in policy.synthetic_track.operations)
        == EXPECTED_SYNTHETIC_OPERATION_IDS
    )
    assert tuple(bank.bank_id for bank in first.banks.banks) == EXPECTED_BANK_IDS

    assert policy.authorization.policy_loader_and_invariant_tests is True
    assert policy.authorization.repr_dependency_integration is True
    assert policy.authorization.repr_six_real_goal_gate is True
    bounded = policy.authorization.bounded_implementation_scope
    assert bounded.user_authorized is True
    assert bounded.current_instruction_prohibits_lean_transform_execution_and_row_generation
    assert bounded.work_may_start_before_current_wave_gate_admission is False
    assert policy.authorization.implementation_readiness.ready is False
    assert (
        policy.authorization.implementation_readiness.unselected_operations_are_readiness_blockers
        is False
    )
    gate_admission = policy.authorization.gate_admission
    assert gate_admission.proposed_operation_ids == EXPECTED_CURRENT_WAVE_OPERATION_IDS
    assert gate_admission.proposed_negative_family_dimension_admission_ids == (
        "n31_required_domain_guard_natural_v1",
    )
    assert gate_admission.gate_admitted is False
    assert gate_admission.gate_admitted_operation_ids == ()
    assert gate_admission.exact_remaining_user_decision == EXACT_CURRENT_USER_DECISION
    assert policy.authorization.production_admission.admitted_operation_ids == ()
    assert policy.authorization.production_admission.current_freeze_production_negative_count == 0
    assert policy.authorization.row_emission_and_scale.row_emission is False
    assert policy.authorization.row_emission_and_scale.ten_k_pilot is False
    assert policy.authorization.row_emission_and_scale.bulk_scale is False
    assert policy.authorization.row_emission_and_scale.publication is False
    assert policy.authorization.row_emission_and_scale.row_count_commitment is False
    all_operations = (*policy.operations, *policy.synthetic_track.operations)
    assert (
        hash_canonical([operation.model_dump(mode="json") for operation in all_operations])
        == EXPECTED_OPERATION_REGISTRY_HASH
    )
    assert all(operation.admission.gate_admitted is False for operation in all_operations)
    assert all(operation.admission.production_admitted is False for operation in all_operations)
    assert all(operation.executable is False for operation in all_operations)
    assert all(operation.label_emission_authorized is False for operation in all_operations)


def test_loader_is_a_zero_lean_policy_boundary() -> None:
    source = Path(policy_module.__file__).read_text(encoding="utf-8")
    assert "from leanfaith.lean" not in source
    assert "import leanfaith.lean" not in source
    assert "_verify_repr_binding" not in source
    assert "repr_third_freeze" not in source
    assert not hasattr(load_sft1_composition_policy().config, "generate")
    assert not hasattr(load_sft1_composition_policy().config, "emit_rows")


def test_policy_loader_replays_hardened_six_goal_durable_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path | None] = []
    real_loader = policy_module.load_six_goal_gate

    def _spy(path: Path | None = None) -> Any:
        calls.append(path)
        return real_loader(path)

    monkeypatch.setattr(policy_module, "load_six_goal_gate", _spy)
    loaded = load_sft1_composition_policy()
    assert loaded.config.representation_pre_gate_acceptance.six_goal_gate_status == "passed"
    assert calls == [Path(EXPECTED_SIX_GOAL_GATE_CONFIG_PATH)]


def test_policy_loader_fails_when_typed_durable_evidence_replay_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(_path: Path | None = None) -> Any:
        raise ValueError("durable sidecar missing or tampered")

    monkeypatch.setattr(policy_module, "load_six_goal_gate", _fail)
    with pytest.raises(SFT1PolicyError, match="durable evidence replay failed"):
        load_sft1_composition_policy()


def test_predecessor_is_superseded_and_approved_repr_freeze_is_exact() -> None:
    policy = load_sft1_composition_policy().config
    predecessor = policy.dependencies.repr_reviewed_predecessor
    api = policy.dependencies.expr_renderer_api
    representation = policy.representation_contract
    repo_root = find_repo_root(Path(__file__))

    assert predecessor.status == "reviewed_but_superseded"
    assert predecessor.consumable_by_sft1 is False
    assert predecessor.hashes_are_execution_dependencies is False
    assert predecessor.freeze_ordinal == 3
    assert api.status == "approved_freeze_sft1_six_goal_passed"
    assert api.required_signature == "renderClosedProp (e : Expr) : MetaM String"
    assert api.replacement_must_be_new_coherent_freeze is True
    assert api.implementation_commit == EXPECTED_REPR_IMPLEMENTATION_COMMIT
    assert api.replacement_commit == EXPECTED_REPR_FREEZE_COMMIT
    assert api.replacement_spec_hash == EXPECTED_REPR_SPEC_HASH
    assert api.replacement_config_path == "configs/representations/goal_v1_v1.yaml"
    assert api.replacement_config_file_sha256 == EXPECTED_REPR_CONFIG_FILE_SHA256
    assert api.replacement_lean_renderer_path == "LeanFaith/Meta/GoalV1.lean"
    assert api.replacement_lean_renderer_sha256 == EXPECTED_REPR_LEAN_RENDERER_SHA256
    assert api.replacement_injected_helper_sha256 == EXPECTED_REPR_INJECTED_HELPER_SHA256
    assert api.replacement_python_renderer_path == "src/leanfaith/representations/goal_v1.py"
    assert api.replacement_python_renderer_sha256 == EXPECTED_REPR_PYTHON_RENDERER_SHA256
    assert hash_file(repo_root / api.replacement_config_path) == api.replacement_config_file_sha256
    assert (
        hash_file(repo_root / api.replacement_lean_renderer_path)
        == api.replacement_lean_renderer_sha256
    )
    assert (
        hash_file(repo_root / api.replacement_python_renderer_path)
        == api.replacement_python_renderer_sha256
    )
    assert api.implementation_set_hash == EXPECTED_REPR_IMPLEMENTATION_SET_HASH
    assert api.renderer_semantic_hash == EXPECTED_REPR_RENDERER_SEMANTIC_HASH
    assert api.renderer_api_hash == EXPECTED_REPR_RENDERER_API_HASH
    assert api.renderer_api_hash_basis == "sha256_canonical_renderer_api_binding_v1"
    assert api.renderer_api_hash_payload_fields == EXPECTED_RENDERER_API_HASH_PAYLOAD_FIELDS
    assert api.populated_renderer_api_hash_must_replay_from_payload is True
    assert api.renderer_api_hash == hash_canonical(
        {field: getattr(api, field) for field in api.renderer_api_hash_payload_fields}
    )
    assert api.route_id == "closed_expr_in_session"
    assert api.python_entrypoint == "render_closed_expr_in_session"
    assert api.endpoint_emitter == "LeanFaith.GoalV1.emitClosedProp"
    assert api.emitter_calls_per_unrolled_endpoint == 1
    assert api.persist_complete_sidecars is True
    assert api.model_facing_projection == "sidecar.core_text()"
    assert api.canonical_universe_profile_id == EXPECTED_REPR_UNIVERSE_PROFILE_ID
    assert api.canonical_universe_profile_hash == EXPECTED_REPR_UNIVERSE_PROFILE_HASH
    assert api.render_context_id == EXPECTED_REPR_RENDER_CONTEXT_ID
    assert api.render_context_hash == EXPECTED_REPR_RENDER_CONTEXT_HASH
    assert api.real_goal_coverage_regression_id == "sft1_repr_six_real_goal_direct_expr_v0_3_1"
    assert api.real_goal_coverage_regression_hash == EXPECTED_SIX_GOAL_RECEIPT_HASH
    assert api.real_goal_coverage_regression_passed is True
    assert api.canonical_universe_profile_must_define_level_instantiation_and_naming
    assert api.real_goal_coverage_uses_closed_expr_api is True
    assert api.all_required_real_goals_must_render_successfully is True
    assert api.required_real_goal_case_ids == EXPECTED_REAL_GOAL_CASE_IDS
    assert api.anonymous_telescope_binder_names_allowed is False
    assert api.anonymous_binder_rejection_scope == "unsupported_anonymous_outer_pi_locals"
    assert api.preserves_nondependent_explicit_structural_arrows is True
    assert api.type_inference_must_succeed_before_render is True
    assert api.api_rejects_unsupported_anonymous_telescope_binder is True
    assert api.api_rejects_ill_typed_expr is True
    assert representation.reference_input == representation.candidate_input
    assert representation.reference_input == "canonical_closed_expr"
    assert representation.same_renderer_for_both_sides is True
    assert representation.sft1_calls_endpoint_emitter_directly_for_both_sides is True
    assert representation.sft1_calls_render_closed_prop_directly is False
    assert representation.endpoint_emitter_uses_frozen_renderer is True
    assert representation.same_persistent_meta_request is True
    assert representation.meta_request_command == "run_meta do"
    assert representation.endpoints_explicitly_unrolled is True
    assert representation.reference_and_candidate_exprs_alive_in_request is True
    assert representation.renderer_signature == api.required_signature
    assert representation.candidate_theorem_declaration_allowed is False
    assert representation.candidate_axiom_declaration_allowed is False
    assert representation.sorry_for_rendering_allowed is False
    assert representation.synthesize_candidate_proof_for_rendering is False
    assert representation.copy_renderer_or_options_into_sft1 is False
    assert representation.surface_render_candidate is False
    assert representation.pretty_print_then_reelaborate_candidate is False
    assert representation.compile_goal_v1_text is False
    assert representation.reelaborate_goal_v1_text is False
    assert representation.require_goal_v1_text_to_compile is False
    assert representation.reference_and_candidate_share_universe_profile is True
    assert representation.universe_profile_source == "repr_replacement_freeze"
    assert representation.local_u_i_canonicalization_without_repr_profile_allowed is False


@pytest.mark.parametrize(
    "field",
    [
        "implementation_commit",
        "replacement_commit",
        "replacement_spec_hash",
        "replacement_config_file_sha256",
        "replacement_lean_renderer_sha256",
        "replacement_injected_helper_sha256",
        "replacement_python_renderer_sha256",
        "implementation_set_hash",
        "renderer_semantic_hash",
        "renderer_api_hash",
        "canonical_universe_profile_hash",
        "render_context_hash",
    ],
)
def test_approved_repr_freeze_rejects_each_exact_pin_drift(field: str) -> None:
    payload = _payload()
    current = payload["dependencies"]["expr_renderer_api"][field]
    assert isinstance(current, str)
    payload["dependencies"]["expr_renderer_api"][field] = "0" * len(current)

    with pytest.raises(ValidationError, match=f"REPR approved-freeze binding differs at {field}"):
        SFT1CompositionPolicy.model_validate(payload)


def test_p23_pack_only_hygiene_and_representation_pre_gate_are_exact() -> None:
    policy = load_sft1_composition_policy().config
    p23 = policy.p23_binder_hygiene_contract
    pre_gate = policy.representation_pre_gate_acceptance
    repo_root = find_repo_root(Path(__file__))
    operation = next(
        item for item in policy.operations if item.operation_id == "P23_CURRY_PROP_PAIR_V1"
    )

    assert operation.orientation == "pack"
    assert p23.status == "design_frozen_implementation_pending"
    assert p23.existing_shared_engine_is_consumable is False
    assert p23.binder_naming_policy_id == "p23_neutral_proof_binder_names_v1"
    assert p23.binder_naming_policy_hash == (
        "248896c182ca20655068eeff77063f68c7288f349f3e44c07f968d02a396dd94"
    )
    assert p23.introduced_binder_count == 1
    assert p23.anonymous_name_constructor_allowed is False
    assert p23.introduced_name_is_nonanonymous is True
    assert p23.introduced_name_renders_exactly_as_allocated is True
    assert p23.required_regressions == EXPECTED_P23_REGRESSIONS
    assert p23.sidecar_bindings == EXPECTED_P23_SIDECAR_BINDINGS
    assert p23.regressions_passed is False

    assert pre_gate.status == "passed"
    assert pre_gate.required_before_one_example_gate is True
    assert (
        pre_gate.fixed_engine_reference_elaboration_source_path
        == EXPECTED_SIX_GOAL_HELPER_SOURCE_PATH
    )
    assert (
        pre_gate.fixed_engine_reference_elaboration_source_file_sha256
        == EXPECTED_SIX_GOAL_HELPER_FILE_SHA256
    )
    assert (
        pre_gate.fixed_engine_reference_elaboration_preamble_hash
        == EXPECTED_SIX_GOAL_HELPER_PREAMBLE_SHA256
    )
    assert (
        pre_gate.fixed_engine_reference_elaboration_import_strip_policy
        == "remove_lines_whose_first_token_is_import_v1"
    )
    assert pre_gate.fixed_engine_reference_elaboration_preamble_reviewed is True
    assert (
        pre_gate.fixed_engine_reference_elaboration_preamble_review_status
        == "reviewed_for_bounded_six_goal_gate"
    )
    assert (
        pre_gate.fixed_engine_reference_elaboration_preamble_required_before_six_goal_gate is True
    )
    assert pre_gate.six_goal_gate_id == "sft1_repr_six_real_goal_direct_expr_v0_3_1"
    assert pre_gate.six_goal_gate_status == "passed"
    assert pre_gate.six_goal_gate_config_path == EXPECTED_SIX_GOAL_GATE_CONFIG_PATH
    assert pre_gate.six_goal_gate_config_file_sha256 == EXPECTED_SIX_GOAL_GATE_CONFIG_FILE_SHA256
    assert (
        pre_gate.six_goal_gate_effective_config_hash == EXPECTED_SIX_GOAL_GATE_EFFECTIVE_CONFIG_HASH
    )
    assert (
        pre_gate.six_goal_execution_config_file_sha256
        == EXPECTED_SIX_GOAL_EXECUTION_CONFIG_FILE_SHA256
    )
    assert pre_gate.six_goal_execution_config_path == EXPECTED_SIX_GOAL_EXECUTION_CONFIG_PATH
    assert pre_gate.six_goal_execution_config_hash == EXPECTED_SIX_GOAL_EXECUTION_CONFIG_HASH
    assert pre_gate.six_goal_receipt_path == EXPECTED_SIX_GOAL_RECEIPT_PATH
    assert pre_gate.six_goal_receipt_file_sha256 == EXPECTED_SIX_GOAL_RECEIPT_FILE_SHA256
    assert pre_gate.six_goal_receipt_hash == EXPECTED_SIX_GOAL_RECEIPT_HASH
    helper_path = repo_root / pre_gate.fixed_engine_reference_elaboration_source_path
    helper_source = helper_path.read_text(encoding="utf-8")
    helper_preamble = policy_module._import_stripped_six_goal_preamble(helper_source)
    assert hash_file(helper_path) == EXPECTED_SIX_GOAL_HELPER_FILE_SHA256
    assert sha256_hex(helper_preamble.encode("utf-8")) == EXPECTED_SIX_GOAL_HELPER_PREAMBLE_SHA256
    gate_config_path = repo_root / pre_gate.six_goal_gate_config_path
    assert hash_file(gate_config_path) == EXPECTED_SIX_GOAL_GATE_CONFIG_FILE_SHA256
    assert (
        policy_module._six_goal_gate_effective_config_hash(gate_config_path)
        == EXPECTED_SIX_GOAL_GATE_EFFECTIVE_CONFIG_HASH
    )
    receipt_path = repo_root / pre_gate.six_goal_receipt_path
    assert hash_file(receipt_path) == EXPECTED_SIX_GOAL_RECEIPT_FILE_SHA256
    assert pre_gate.reference_render_succeeds_through_shared_api is True
    assert pre_gate.candidate_render_succeeds_through_shared_api is True
    assert pre_gate.required_distinct_render_is_distinct is True
    assert pre_gate.exact_turnstile_count == 1
    assert pre_gate.expr_mvars_allowed is False
    assert pre_gate.universe_mvars_allowed is False
    assert pre_gate.anonymous_binder_names_allowed is False
    assert pre_gate.anonymous_binder_rejection_scope == "unsupported_anonymous_outer_pi_locals"
    assert pre_gate.preserves_nondependent_explicit_structural_arrows is True
    assert pre_gate.forbidden_render_substrings == EXPECTED_PRE_GATE_FORBIDDEN_RENDER_SUBSTRINGS
    assert (
        tuple(
            (mapping.substring, mapping.exact_failure_class)
            for mapping in pre_gate.forbidden_render_failure_mappings
        )
        == EXPECTED_FORBIDDEN_RENDER_FAILURE_MAPPINGS
    )
    assert pre_gate.type_inference_must_succeed_before_render is True
    assert pre_gate.failure_reporting_dimensions == EXPECTED_PRE_GATE_FAILURE_DIMENSIONS
    assert pre_gate.exact_failure_classes == EXPECTED_PRE_GATE_FAILURE_CLASSES
    assert pre_gate.stable_id_and_sidecar_bindings == EXPECTED_PRE_GATE_SIDECAR_BINDINGS


def test_forbidden_render_residues_map_to_exact_failure_classes_behaviorally() -> None:
    pre_gate = load_sft1_composition_policy().config.representation_pre_gate_acceptance

    assert pre_gate.classify_forbidden_rendered_residue("[anonymous] : True✝\n⊢ True") == (
        "anonymous_binder_name",
    )
    assert pre_gate.classify_forbidden_rendered_residue("x : Nat\n⊢ f ⋯") == (
        "forbidden_rendered_placeholder",
    )
    assert pre_gate.classify_forbidden_rendered_residue("[anonymous]\n⊢ f ⋯") == (
        "anonymous_binder_name",
        "forbidden_rendered_placeholder",
    )
    assert pre_gate.classify_forbidden_rendered_residue("x : Nat\n⊢ x = x") == ()


@pytest.mark.parametrize(
    "field",
    [
        "fixed_engine_reference_elaboration_source_file_sha256",
        "fixed_engine_reference_elaboration_preamble_hash",
        "six_goal_gate_config_file_sha256",
        "six_goal_gate_effective_config_hash",
    ],
)
def test_six_goal_pre_gate_rejects_each_exact_hash_drift(field: str) -> None:
    payload = _payload()
    pre_gate = payload["representation_pre_gate_acceptance"]
    current = pre_gate[field]
    assert isinstance(current, str)
    pre_gate[field] = "0" * len(current)

    with pytest.raises(
        ValidationError,
        match=f"SFT1 six-goal pre-gate binding differs at {field}",
    ):
        SFT1CompositionPolicy.model_validate(payload)


def test_bank_anchor_hashes_references_and_axiom_profiles_replay() -> None:
    loaded = load_sft1_composition_policy()
    validate_sft1_policy_bindings(loaded.config, loaded.banks)
    profiles = {profile.profile_id for profile in loaded.config.axiom_profiles}
    entry_index = {
        (bank.bank_id, entry.entry_id): (bank, entry)
        for bank in loaded.banks.banks
        for entry in bank.entries
    }
    for bank in loaded.banks.banks:
        for entry in bank.entries:
            assert entry.anchor_spec_hash
            assert set(entry.allowed_axiom_profiles) <= profiles
            assert entry.resolved_lean_hash is None
    for operation in (*loaded.config.operations, *loaded.config.synthetic_track.operations):
        if operation.anchor.bank_id is None:
            continue
        assert operation.anchor.bank_entry_id is not None
        key = (operation.anchor.bank_id, operation.anchor.bank_entry_id)
        bank, entry = entry_index[key]
        assert operation.anchor.kind == entry.anchor_kind
        assert operation.anchor.ref == entry.anchor_ref
        assert operation.anchor.schema_lemma_procedure_hash == entry.anchor_spec_hash
        assert operation.allowed_axiom_profile in entry.allowed_axiom_profiles
        if bank.family_id.startswith("P"):
            assert bank.family_id == operation.family_id


def test_dispositions_negative_lanes_and_claim_erasure_guards_are_exact() -> None:
    policy = load_sft1_composition_policy().config
    all_operations = (*policy.operations, *policy.synthetic_track.operations)
    dispositions = tuple((item.family_id, item.disposition) for item in policy.family_dispositions)
    assert dispositions == EXPECTED_FAMILY_DISPOSITIONS

    by_id = {operation.operation_id: operation for operation in all_operations}
    negative = [operation for operation in all_operations if operation.family_id.startswith("N")]
    assert {operation.label_lane for operation in negative} == {
        LabelLane.N_RUBRIC,
        LabelLane.N_PROOF,
    }
    for operation in negative:
        assert operation.exact_delta_evidence_required is True
        assert operation.rubric_dimension is not None
        assert operation.anti_degeneracy_checks
        if operation.evidence_class == EvidenceClass.N_RUBRIC:
            assert operation.candidate_truth_default == CandidateTruth.UNKNOWN
            assert operation.n_proof_subtype_of is None
        else:
            assert operation.candidate_truth_default == CandidateTruth.REFUTED
            assert operation.n_proof_subtype_of is not None
            parent = by_id[operation.n_proof_subtype_of]
            assert parent.evidence_class == EvidenceClass.N_RUBRIC
            assert parent.family_id == operation.family_id
            assert operation.cap.maximum_retained_share <= parent.cap.maximum_retained_share

    assert policy.label_contract.generic_d0_is_label_evidence is False
    assert policy.label_contract.n_rubric.claims_f2_truth is False
    assert policy.label_contract.n_proof.inherits_parent_typed_applicability is True
    assert policy.label_contract.n_proof.inherits_parent_exact_delta_evidence is True
    assert policy.label_contract.n_proof.inherits_parent_anti_degeneracy_checks is True
    promotion = policy.label_contract.negative_operation_promotion
    assert promotion.applies_to_evidence_classes == (
        EvidenceClass.N_RUBRIC,
        EvidenceClass.N_PROOF,
    )
    assert promotion.initial_operation_status == "proof_of_concept"
    assert promotion.production_supporting_measured_gate == (
        "approximately_100_roots_per_selected_operation_after_smoke_and_conformance"
    )
    assert promotion.required_measured_report_fields == EXPECTED_NEGATIVE_PROMOTION_MEASUREMENTS
    assert (
        promotion.exact_production_user_decision_record_fields
        == EXPECTED_NEGATIVE_PRODUCTION_DECISION_RECORD_FIELDS
    )
    assert promotion.measured_gate_pass_auto_promotes_operation is False
    assert promotion.n_proof_requires_parent_n_rubric_production_admission is True
    assert promotion.current_production_eligible_negative_operation_ids == ()
    assert promotion.current_freeze_permits_production_negative_count == 0
    assert policy.label_contract.n_rubric_family_dimension_user_admission_required is True
    assert policy.label_contract.operation_candidate_truth_default_may_fill_missing_sidecar is False
    assert policy.label_contract.candidate_truth_evidence_determines_label is False
    assert policy.label_contract.lane_selected_before_proof_validation is True
    assert policy.label_contract.proof_success_or_failure_may_select_label_lane is False
    assert policy.label_contract.candidate_provability_may_create_label is False
    assert policy.label_contract.candidate_truth_evidence_values == (
        CandidateTruth.PROVED,
        CandidateTruth.REFUTED,
        CandidateTruth.UNKNOWN,
    )
    for operation in all_operations:
        if operation.evidence_class in {EvidenceClass.P_LEMMA, EvidenceClass.P_REFLECT}:
            assert operation.claim_erasure_guards == EXPECTED_CLAIM_ERASURE_GUARDS
        else:
            assert operation.claim_erasure_guards is None

    admission_records = tuple(
        (
            admission.admission_id,
            admission.family_id,
            admission.rubric_dimension,
            admission.track.value,
            admission.operation_ids,
        )
        for admission in policy.negative_family_dimension_admissions
    )
    assert admission_records == EXPECTED_NEGATIVE_ADMISSIONS
    assert all(
        admission.gate_admitted is False and admission.production_admitted is False
        for admission in policy.negative_family_dimension_admissions
    )
    admitted_ids = {
        operation_id
        for admission in policy.negative_family_dimension_admissions
        for operation_id in admission.operation_ids
    }
    assert admitted_ids == {operation.operation_id for operation in negative}


def test_row_evidence_contract_is_f1_only_and_receipts_are_typed() -> None:
    policy = load_sft1_composition_policy().config
    contract = policy.row_evidence_contract
    assert contract.ordered_axes == policy_module.EXPECTED_ROW_EVIDENCE_AXES
    assert contract.label_derivation_axis == "f1_claim_relation_certificate"
    assert contract.f0_may_create_or_override_f1_label is False
    assert contract.candidate_truth_may_create_or_override_f1_label is False
    assert contract.optional_f2_may_create_or_override_f1_label is False
    assert contract.n_rubric_requires_f2_direction_evidence is False
    assert contract.n_rubric_logical_nonequivalence_or_refutation_required is False
    assert contract.n_proof_requires_source_proof_and_candidate_refutation is True

    common = {
        "operation_id": "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        "evidence_class": EvidenceClass.N_RUBRIC,
        "certificate_class_id": "n_rubric_exact_protected_delta_certificate_v1",
        "reference_closed_prop_validation": ClosedPropValidation.PASSED,
        "candidate_closed_prop_validation": ClosedPropValidation.PASSED,
        "f0_definitional_relation": F0Relation.NOT_DEFINITIONALLY_EQUAL,
        "f1_claim_relation_certificate": F1ClaimRelation.BREAKING,
        "f1_certificate_payload_hash": "1" * 64,
        "candidate_truth_evidence_payload_hash": "2" * 64,
        "optional_f2_direction_evidence": None,
        "optional_f2_direction_evidence_payload_hash": None,
        "source_proof_hash": None,
        "candidate_refutation_hash": None,
        "final_retain_or_drop_disposition": RetainDisposition.RETAIN,
        "terminal_disposition_reason": TerminalDispositionReason.RETAINED,
    }
    for truth in CandidateTruth:
        receipt = RowEvidenceReceipt.model_validate({**common, "candidate_truth_evidence": truth})
        assert receipt.candidate_truth_evidence == truth
        assert receipt.optional_f2_direction_evidence is None

    proof_receipt = RowEvidenceReceipt.model_validate(
        {
            **common,
            "operation_id": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
            "evidence_class": EvidenceClass.N_PROOF,
            "certificate_class_id": ("n_proof_source_proof_candidate_refutation_certificate_v1"),
            "candidate_truth_evidence": CandidateTruth.REFUTED,
            "source_proof_hash": "3" * 64,
            "candidate_refutation_hash": "4" * 64,
        }
    )
    assert proof_receipt.candidate_refutation_hash == "4" * 64


def test_row_evidence_receipts_reject_axis_collapse_and_wrong_terminal_reason() -> None:
    base: dict[str, Any] = {
        "operation_id": "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        "evidence_class": EvidenceClass.N_RUBRIC,
        "certificate_class_id": "n_rubric_exact_protected_delta_certificate_v1",
        "reference_closed_prop_validation": ClosedPropValidation.PASSED,
        "candidate_closed_prop_validation": ClosedPropValidation.PASSED,
        "f0_definitional_relation": F0Relation.NOT_DEFINITIONALLY_EQUAL,
        "f1_claim_relation_certificate": F1ClaimRelation.BREAKING,
        "f1_certificate_payload_hash": "1" * 64,
        "candidate_truth_evidence": CandidateTruth.UNKNOWN,
        "candidate_truth_evidence_payload_hash": "2" * 64,
        "optional_f2_direction_evidence": None,
        "optional_f2_direction_evidence_payload_hash": None,
        "source_proof_hash": None,
        "candidate_refutation_hash": None,
        "final_retain_or_drop_disposition": RetainDisposition.RETAIN,
        "terminal_disposition_reason": TerminalDispositionReason.RETAINED,
    }
    mutations = (
        {"f0_definitional_relation": F0Relation.DEFINITIONALLY_EQUAL},
        {
            "operation_id": "P01_ALPHA_RENAME_SINGLE_V1",
            "evidence_class": EvidenceClass.N_RUBRIC,
        },
        {"terminal_disposition_reason": TerminalDispositionReason.NO_OP_DROPPED},
    )
    for mutation in mutations:
        with pytest.raises(ValidationError):
            RowEvidenceReceipt.model_validate({**base, **mutation})

    p_def = {
        **base,
        "operation_id": "P01_ALPHA_RENAME_SINGLE_V1",
        "evidence_class": EvidenceClass.P_DEF,
        "certificate_class_id": "p_def_defeq_certificate_v1",
        "f1_claim_relation_certificate": F1ClaimRelation.PRESERVING,
        "f0_definitional_relation": F0Relation.NOT_DEFINITIONALLY_EQUAL,
    }
    with pytest.raises(ValidationError, match="P-DEF"):
        RowEvidenceReceipt.model_validate(p_def)

    n_proof_without_receipts = {
        **base,
        "operation_id": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        "evidence_class": EvidenceClass.N_PROOF,
        "certificate_class_id": "n_proof_source_proof_candidate_refutation_certificate_v1",
        "candidate_truth_evidence": CandidateTruth.REFUTED,
    }
    with pytest.raises(ValidationError, match="N-PROOF"):
        RowEvidenceReceipt.model_validate(n_proof_without_receipts)


def test_drop_receipts_enforce_terminal_axis_and_n_proof_discipline() -> None:
    certified_rubric_drop: dict[str, Any] = {
        "operation_id": "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        "evidence_class": EvidenceClass.N_RUBRIC,
        "certificate_class_id": "n_rubric_exact_protected_delta_certificate_v1",
        "reference_closed_prop_validation": ClosedPropValidation.PASSED,
        "candidate_closed_prop_validation": ClosedPropValidation.PASSED,
        "f0_definitional_relation": F0Relation.NOT_DEFINITIONALLY_EQUAL,
        "f1_claim_relation_certificate": F1ClaimRelation.BREAKING,
        "f1_certificate_payload_hash": "1" * 64,
        "candidate_truth_evidence": CandidateTruth.UNKNOWN,
        "candidate_truth_evidence_payload_hash": "2" * 64,
        "optional_f2_direction_evidence": None,
        "optional_f2_direction_evidence_payload_hash": None,
        "source_proof_hash": None,
        "candidate_refutation_hash": None,
        "final_retain_or_drop_disposition": RetainDisposition.DROP,
        "terminal_disposition_reason": TerminalDispositionReason.WITHIN_ROOT_DUPLICATE_DROPPED,
    }
    assert (
        RowEvidenceReceipt.model_validate(certified_rubric_drop).terminal_disposition_reason
        == TerminalDispositionReason.WITHIN_ROOT_DUPLICATE_DROPPED
    )

    pre_f1_blocklist = {
        **certified_rubric_drop,
        "f0_definitional_relation": F0Relation.UNKNOWN,
        "f1_claim_relation_certificate": F1ClaimRelation.UNCERTIFIED,
        "f1_certificate_payload_hash": None,
        "terminal_disposition_reason": TerminalDispositionReason.BLOCKLIST_DROPPED,
    }
    assert (
        RowEvidenceReceipt.model_validate(pre_f1_blocklist).terminal_disposition_reason
        == TerminalDispositionReason.BLOCKLIST_DROPPED
    )

    certified_n_proof_drop = {
        **certified_rubric_drop,
        "operation_id": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        "evidence_class": EvidenceClass.N_PROOF,
        "certificate_class_id": "n_proof_source_proof_candidate_refutation_certificate_v1",
        "candidate_truth_evidence": CandidateTruth.REFUTED,
        "source_proof_hash": "3" * 64,
        "candidate_refutation_hash": "4" * 64,
    }
    assert RowEvidenceReceipt.model_validate(certified_n_proof_drop).source_proof_hash == "3" * 64

    contradictory_drops = (
        {
            **certified_rubric_drop,
            "f1_claim_relation_certificate": F1ClaimRelation.UNCERTIFIED,
            "f1_certificate_payload_hash": None,
        },
        {
            **certified_rubric_drop,
            "terminal_disposition_reason": TerminalDispositionReason.F1_RELATION_UNCERTIFIED,
        },
        {
            **certified_rubric_drop,
            "operation_id": "P01_ALPHA_RENAME_SINGLE_V1",
            "evidence_class": EvidenceClass.P_DEF,
            "certificate_class_id": "p_def_defeq_certificate_v1",
            "f0_definitional_relation": F0Relation.DEFINITIONALLY_EQUAL,
        },
        {
            **certified_n_proof_drop,
            "source_proof_hash": None,
            "candidate_refutation_hash": None,
        },
        {
            **pre_f1_blocklist,
            "operation_id": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
            "evidence_class": EvidenceClass.N_PROOF,
            "certificate_class_id": "n_proof_source_proof_candidate_refutation_certificate_v1",
            "source_proof_hash": "3" * 64,
            "candidate_refutation_hash": "4" * 64,
        },
        {
            **pre_f1_blocklist,
            "candidate_closed_prop_validation": ClosedPropValidation.FAILED,
        },
        {
            **pre_f1_blocklist,
            "f1_claim_relation_certificate": F1ClaimRelation.BREAKING,
            "f1_certificate_payload_hash": "1" * 64,
        },
        {
            **pre_f1_blocklist,
            "candidate_truth_evidence": CandidateTruth.PROVED,
        },
        {
            **certified_rubric_drop,
            "source_proof_hash": "3" * 64,
            "candidate_refutation_hash": "4" * 64,
        },
    )
    for contradictory in contradictory_drops:
        with pytest.raises(ValidationError):
            RowEvidenceReceipt.model_validate(contradictory)


def test_exact_certificate_binder_domain_bank_and_accounting_partitions() -> None:
    policy = load_sft1_composition_policy().config
    certificate_observed = tuple(
        (
            binding.evidence_class.value,
            binding.certificate_class_id,
            binding.operation_ids,
        )
        for binding in policy.certificate_class_contract.bindings
    )
    assert certificate_observed == EXPECTED_CERTIFICATE_CLASS_BINDINGS
    bindings = policy.operation_execution_bindings
    assert bindings.binding_requirement_scope == "gate_admitted_current_wave_operations_only"
    assert bindings.proposed_current_wave_operation_ids == EXPECTED_CURRENT_WAVE_OPERATION_IDS
    assert bindings.gate_admitted_operation_ids == ()
    assert bindings.unresolved_gate_admitted_operation_ids == ()
    assert bindings.unselected_or_unadmitted_operation_ids == (
        *EXPECTED_OPERATION_IDS,
        *EXPECTED_SYNTHETIC_OPERATION_IDS,
    )
    assert bindings.resolved_bindings == ()
    assert bindings.unselected_operations_remain_fail_closed is True
    assert bindings.unselected_operations_block_current_wave_readiness is False
    assert bindings.execution_prerequisites_satisfied is False
    assert policy.binder_elaboration_profile.required_checks == (EXPECTED_BINDER_ELABORATION_CHECKS)
    empty_observed = tuple(
        (profile.disposition.value, profile.operation_ids)
        for profile in policy.empty_domain_contract.profiles[:3]
    )
    assert empty_observed == EXPECTED_EMPTY_DOMAIN_PROFILES
    bank_observed = tuple(
        (bank.bank_id, bank.operation_ids) for bank in policy.negative_applicability_banks.banks
    )
    assert bank_observed == EXPECTED_NEGATIVE_APPLICABILITY_BANKS
    assert all(
        bank.implementation_resolved is False for bank in policy.negative_applicability_banks.banks
    )
    accounting = policy.operation_accounting_contract
    assert (
        tuple((group.group_id, group.operation_ids) for group in accounting.correlation_groups)
        == EXPECTED_CORRELATION_GROUPS
    )
    assert (
        tuple(
            (group.group_id, group.operation_ids) for group in accounting.effective_diversity_groups
        )
        == EXPECTED_EFFECTIVE_DIVERSITY_GROUPS
    )
    assert accounting.proof_of_concept_counted_in_production_volume_or_diversity is False


def test_execution_binding_registry_scopes_barrier_to_gate_admitted_wave_only() -> None:
    policy = load_sft1_composition_policy().config
    payload = policy.operation_execution_bindings.model_dump(mode="python")
    admitted = "P01_ALPHA_RENAME_SINGLE_V1"
    payload["status"] = "current_wave_admitted_bindings_incomplete_fail_closed"
    payload["gate_admitted_operation_ids"] = [admitted]
    payload["unresolved_gate_admitted_operation_ids"] = [admitted]
    payload["unselected_or_unadmitted_operation_ids"] = [
        operation_id
        for operation_id in (*EXPECTED_OPERATION_IDS, *EXPECTED_SYNTHETIC_OPERATION_IDS)
        if operation_id != admitted
    ]

    registry = policy_module.OperationExecutionBindingRegistry.model_validate(payload)
    assert registry.unresolved_gate_admitted_operation_ids == (admitted,)
    assert len(registry.unselected_or_unadmitted_operation_ids) == 45
    assert registry.unselected_operations_block_current_wave_readiness is False


def test_special_family_dispositions_remain_non_executable() -> None:
    policy = load_sft1_composition_policy().config
    by_family: dict[str, list[Any]] = {}
    for operation in policy.operations:
        by_family.setdefault(operation.family_id, []).append(operation)

    assert all(
        operation.status == OperationStatus.DIAGNOSTIC
        and operation.track == OperationTrack.DIAGNOSTIC
        for family in ("P02", "P11")
        for operation in by_family[family]
    )
    assert tuple(operation.operation_id for operation in by_family["P01"]) == (
        "P01_ALPHA_RENAME_SINGLE_V1",
    )
    assert all(
        operation.status == OperationStatus.DIAGNOSTIC
        and operation.cap.maximum_retained_share <= 0.005
        for operation in by_family["P21"]
        if "INTRO" in operation.operation_id
    )
    for family in ("P39", "P41", "P42", "N29", "N30", "N31", "N32"):
        assert all(
            operation.status == OperationStatus.PROOF_OF_CONCEPT for operation in by_family[family]
        )
    assert "N21" not in by_family
    assert "N22" not in by_family
    assert "N28" not in by_family
    assert all(operation.family_id == "N28" for operation in policy.synthetic_track.operations)
    n32_rubric, n32_proof = by_family["N32"]
    assert "same binary relation" in n32_rubric.typed_applicability
    assert "reject function-composition reordering" in n32_rubric.context_restrictions
    assert "reject_function_composition_case" in n32_rubric.anti_degeneracy_checks
    assert "relation-converse-only" in n32_proof.typed_applicability
    negative_priorities = [
        operation.priority for operation in policy.operations if operation.family_id.startswith("N")
    ]
    assert {operation.priority for operation in by_family["N31"]} == {min(negative_priorities)}


def test_grammar_cache_cap_order_quality_gates_and_scale_are_exact() -> None:
    policy = load_sft1_composition_policy().config

    assert policy.composition_grammar.productions == EXPECTED_COMPOSITION_PRODUCTIONS
    assert policy.composition_grammar.maximum_total_operations == 3
    assert policy.composition_grammar.admitted_statuses_after_operation_approval == (
        "implementation_candidate",
        "proof_of_concept",
    )
    assert policy.composition_grammar.diagnostic_operations_may_emit_rows is False
    assert policy.composition_grammar.post_negative_operations_allowed is False
    assert policy.composition_grammar.positive_row_negative_operation_count == 0
    assert policy.composition_grammar.negative_row_negative_operation_count == 1
    assert policy.composition_grammar.all_sites_pairwise_disjoint_after_typed_rediscovery is True
    assert policy.composition_grammar.one_operation_per_mechanism_superclass is True
    assert policy.composition_grammar.repeated_inverse_tokens_rejected is True
    assert policy.composition_grammar.repeated_text_hashes_rejected is True
    assert policy.composition_grammar.repeated_closed_expr_hashes_rejected is True
    assert policy.composition_grammar.repeated_render_hashes_rejected is True
    assert policy.composition_grammar.repeated_selected_site_lineage_rejected is True
    assert policy.composition_grammar.direct_only_operation_ids == ()
    assert len(policy.composition_grammar.mutual_exclusion_groups) == 1
    exclusion = policy.composition_grammar.mutual_exclusion_groups[0]
    assert exclusion.group_id == "one_definitional_mechanism_per_chain"
    assert exclusion.maximum_members_per_chain == 1
    assert exclusion.operation_ids == EXPECTED_MUTUAL_EXCLUSION_OPERATION_IDS
    exception = policy.composition_grammar.p01_alpha_fingerprint_repeat_exception
    assert exception.maximum_uses_per_chain == 1
    assert exception.may_repeat_alpha_fingerprint_once is True
    assert exception.exception_applies_only_to_the_single_p01_hop is True
    assert exception.may_repeat_expr_or_render_hash is False
    assert policy.cache_contract.exact_ordered_key_fields == EXPECTED_CACHE_KEY_FIELDS
    assert (
        policy.cache_contract.operation_registry_entry_hash_basis
        == "sha256_canonical_operation_spec_v1"
    )
    assert policy.deterministic_cap_order == EXPECTED_CAP_ORDER
    assert policy.deterministic_cap_order.index("stable_row_hash_total_order") < (
        policy.deterministic_cap_order.index(
            "canonical_unordered_pair_duplicate_and_conflict_classification"
        )
    )
    assert policy.deterministic_cap_order.index(
        "conflicting_label_canonical_pair_class_rejection"
    ) < policy.deterministic_cap_order.index("per_root_cap")
    assert policy.deterministic_cap_order.index("source_polarity_joint_balance") < (
        policy.deterministic_cap_order.index("deterministic_training_orientation_swap")
    )
    assert policy.deterministic_cap_order[-1] == (
        "post_orientation_global_model_facing_duplicate_conflict_assertion"
    )

    execution = policy.execution_contract
    assert execution.prevalidation_candidate_sampling_uses_randomness is False
    assert (
        execution.prevalidation_candidate_sampling_rule
        == "stable_hash_total_order_then_operation_budget_prefix"
    )
    assert execution.prevalidation_candidate_sampling_hash_fields == (
        EXPECTED_PREVALIDATION_HASH_FIELDS
    )
    assert execution.required_environment_fingerprint_fields == (
        EXPECTED_ENVIRONMENT_FINGERPRINT_FIELDS
    )
    assert execution.environment_fingerprint_required_per_persistent_request is True
    assert execution.environment_fingerprint_persisted_in_every_candidate_sidecar is True
    assert execution.environment_fingerprint_in_cache_key is True

    quality = policy.sampling_and_quality
    assert quality.deterministic_training_orientation_swap_fraction == 0.5
    assert quality.orientation_swap_scope == "training_only"
    assert quality.stable_row_hash_basis == "sha256_canonical_sft1_row_selection_identity_v1"
    assert quality.stable_row_hash_fields == EXPECTED_STABLE_ROW_HASH_FIELDS
    assert (
        quality.canonical_unordered_pair_hash_basis
        == "sha256_sorted_reference_candidate_render_hashes_v1"
    )
    assert quality.same_label_duplicate_survivor_rule == "minimum_stable_row_hash"
    assert quality.conflicting_label_class_action == (
        "reject_entire_canonical_unordered_pair_class"
    )
    assert quality.preserve_root_ancestry_clusters is True
    assert quality.preserve_near_duplicate_clusters is True
    assert quality.evaluation_blocklist_path == "data/benchmarks/golden_blocklist_v1.json"
    assert quality.evaluation_blocklist_sha256 == (
        "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
    )
    assert quality.both_blocklist_screens_use_exact_binding is True
    assert quality.root_level_evaluation_blocklist_screen is True
    assert quality.post_transform_evaluation_blocklist_screen is True
    assert quality.global_model_facing_duplicate_rejection is True
    assert quality.global_conflicting_label_rejection is True
    assert quality.duplicate_conflict_screen_before_caps_uses_canonical_unordered_pairs is True
    assert quality.duplicate_conflict_screen_before_caps_checks_both_orientations is True
    assert quality.orientation_swap_after_cap_selection is True
    assert quality.post_orientation_global_model_facing_duplicate_and_conflict_assertion is True
    assert quality.post_orientation_assertion_failure_action == (
        "fail_shard_without_commit_or_refill"
    )
    assert quality.candidate_only_balanced_accuracy_strictly_below == 0.6
    assert quality.reference_only_balanced_accuracy_strictly_below == 0.6
    assert quality.paired_family_heldout_balanced_accuracy_strictly_below == 0.65
    assert quality.paired_mechanism_heldout_balanced_accuracy_strictly_below == 0.65
    assert quality.paired_template_heldout_balanced_accuracy_strictly_below == 0.65
    assert quality.confidence_bounds_required is True
    assert quality.confidence_level == 0.95
    assert quality.confidence_interval_method == "stratified_cluster_bootstrap"
    assert quality.confidence_interval_upper_bound_must_be_strictly_below_threshold is True

    caps = policy.cap_contract
    assert caps.natural_and_synthetic_denominators_separate is True
    assert caps.synthetic_rows_count_toward_natural_caps is False
    assert caps.compiler_data_root_share_maximum == 0.20
    assert caps.any_single_source_share_maximum == 0.40
    assert caps.family_share_maximum == 0.08
    assert caps.mechanism_superclass_share_maximum == 0.12
    assert caps.presentation_and_definitional_combined_share_maximum == 0.10
    assert caps.exact_operation_share_maximum == 0.02
    assert caps.bank_entry_or_template_share_maximum == 0.005
    assert caps.lemma_or_procedure_share_maximum == 0.0025
    assert caps.exact_ordered_composition_template_share_maximum == 0.005
    assert caps.per_root_retained_pair_maximum == 8
    assert caps.caps_are_maxima_not_quotas is True
    assert caps.force_rows_to_fill_cap_or_balance is False

    assert policy.inline_anchor_resolution.status == "pending"
    assert policy.inline_anchor_resolution.design_reference_hashes_are_executable_hashes is False
    assert policy.inline_anchor_resolution.resolved_manifest_hash is None
    assert policy.adversarial_fixture_freeze.status == "pending"
    assert policy.adversarial_fixture_freeze.operation_fixture_ids_are_design_ids_only is True
    assert policy.adversarial_fixture_freeze.fixture_bundle_hash is None
    assert policy.root_census.executes_lean is False
    assert policy.root_census.required_before_any_row_commitment is True
    assert policy.root_census.row_commitment_authorized is False

    assert policy.gates.one_example.user_scope_authorized is True
    assert policy.gates.one_example.requires_current_wave_gate_admission is True
    assert policy.gates.one_example.current_wave_gate_admitted is False
    assert policy.gates.one_example.execution_ready is False
    assert policy.gates.one_example.execution_started is False
    assert policy.gates.one_example.requires_shared_contract_update is True
    assert policy.gates.one_example.requires_representation_pre_gate_acceptance is True
    assert policy.gates.one_example.requires_zero_lean_census_and_source_eligibility is True
    assert policy.gates.one_example.requires_selected_operation_execution_binding is True
    assert policy.gates.one_example.actual_serialized_positive_example_count == 1
    assert policy.gates.one_example.actual_serialized_negative_example_count == 1
    assert policy.gates.one_example.positive_operation_id == "P01_ALPHA_RENAME_SINGLE_V1"
    assert policy.gates.one_example.negative_operation_id == "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
    assert policy.gates.one_example.roots_chosen_from_completed_census is True
    assert policy.gates.one_example.complete_sidecar_required is True
    assert policy.gates.one_example.manifest_link_required is True
    assert policy.gates.one_example.cache_replay_required is True
    assert policy.gates.one_example.duplicate_suppression_replay_required is True
    assert policy.gates.one_example.gate_artifacts_are_production_rows is False
    conformance = policy.gates.selected_wave_operation_conformance
    assert conformance.requires_one_example_gate_pass is True
    assert conformance.current_wave_gate_admitted is False
    assert conformance.success_per_operation_and_eligible_project == 1
    assert conformance.adversarial_rejection_per_operation_and_eligible_project == 1
    assert conformance.zero_yield_waiver_allowed is False
    assert conformance.census_backed_inapplicable_project_requires_policy_revision
    assert conformance.full_46_operation_matrix_required is False
    assert policy.gates.hundred_root.user_scope_authorized is True
    assert policy.gates.hundred_root.requires_current_wave_gate_admission is True
    assert policy.gates.hundred_root.current_wave_gate_admitted is False
    assert policy.gates.hundred_root.execution_ready is False
    assert policy.gates.hundred_root.execution_started is False
    assert policy.gates.hundred_root.requires_selected_wave_conformance_gate_pass is True
    assert policy.gates.hundred_root.eligible_roots_per_operation_approximately == 100
    assert policy.gates.hundred_root.every_retained_row_typed_meta_validated_and_replayed
    assert policy.gates.hundred_root.exact_counter_fields == EXPECTED_HUNDRED_ROOT_COUNTER_FIELDS
    assert policy.gates.hundred_root.exact_counter_dimensions == (
        EXPECTED_HUNDRED_ROOT_COUNTER_DIMENSIONS
    )
    assert policy.gates.hundred_root.candidate_terminal_disposition_counter_fields == (
        EXPECTED_CANDIDATE_TERMINAL_DISPOSITION_COUNTERS
    )
    assert policy.gates.hundred_root.exactly_one_terminal_disposition_per_generated_candidate
    assert policy.gates.hundred_root.terminal_disposition_counters_mutually_exclusive
    assert policy.gates.hundred_root.conservation_equations == (
        EXPECTED_HUNDRED_ROOT_CONSERVATION_EQUATIONS
    )
    diagnostics = policy.hundred_root_surface_diagnostics
    assert diagnostics.scope == "model_facing_reference_and_candidate_core_text_only"
    assert diagnostics.sidecar_or_certificate_fields_visible_to_diagnostic is False
    assert diagnostics.explicit_transform_marker_rate_maximum == 0.0
    assert diagnostics.explicit_transform_marker_violation_blocks_gate is True
    assert diagnostics.recoverability_is_label_evidence is False
    assert diagnostics.recoverability_threshold_is_blocking is False
    assert policy.gates.ten_k_pilot.authorized is False
    assert policy.gates.ten_k_pilot.requires_separate_user_approval_after_hundred_root_report
    assert policy.gates.bulk_scale.authorized is False
    assert policy.gates.publication.authorized is False
    full_cost = policy.gates.full_matrix_cost_reference
    assert (
        full_cost.operation_project_combinations
        == EXPECTED_FULL_MATRIX_OPERATION_PROJECT_COMBINATIONS
    )
    assert full_cost.success_and_adversarial_fixture_count == EXPECTED_FULL_MATRIX_FIXTURES
    assert (
        full_cost.approximate_root_count_at_hundred_per_operation
        == EXPECTED_FULL_MATRIX_APPROXIMATE_ROOTS
    )
    assert full_cost.required_before_selected_wave_progression is False
    wave_cost = policy.gates.current_wave_cost_reference
    assert (
        wave_cost.operation_project_combinations
        == EXPECTED_CURRENT_WAVE_OPERATION_PROJECT_COMBINATIONS
    )
    assert wave_cost.success_and_adversarial_fixture_count == EXPECTED_CURRENT_WAVE_FIXTURES
    assert (
        wave_cost.approximate_root_count_at_hundred_per_operation
        == EXPECTED_CURRENT_WAVE_APPROXIMATE_ROOTS
    )
    assert policy.execution_contract.retained_certificate_replay_fraction == 1.0

    scale = policy.scale_contract
    assert scale.measured_target_is_minimum is False
    assert scale.measured_target_is_commitment is False
    assert scale.illustrative_arithmetical_maximum_rows == (
        scale.illustrative_root_count_for_cap_arithmetic * scale.retained_pairs_per_root_cap
    )
    assert scale.post_census_planning_band_rows == (2000000, 3000000)
    assert scale.minimum_roots_for_five_million_at_current_cap == 625000
    assert scale.five_million_feasible_at_illustrative_root_count is False
    assert scale.all_row_count_commitments_authorized is False


def _drop_operation(payload: dict[str, Any]) -> None:
    payload["operations"] = payload["operations"][:-1]


def _approve_operation(payload: dict[str, Any]) -> None:
    _operation(payload, "P01_ALPHA_RENAME_SINGLE_V1")["admission"]["gate_admitted"] = True


def _enable_operation(payload: dict[str, Any]) -> None:
    _operation(payload, "P01_ALPHA_RENAME_SINGLE_V1")["executable"] = True


def _drift_operation_orientation(payload: dict[str, Any]) -> None:
    _operation(payload, "P01_ALPHA_RENAME_SINGLE_V1")["orientation"] = "renamed_drift"


def _weaken_same_renderer(payload: dict[str, Any]) -> None:
    payload["representation_contract"]["same_renderer_for_both_sides"] = False


def _make_predecessor_consumable(payload: dict[str, Any]) -> None:
    payload["dependencies"]["repr_reviewed_predecessor"]["consumable_by_sft1"] = True


def _drift_repr_freeze_commit(payload: dict[str, Any]) -> None:
    payload["dependencies"]["expr_renderer_api"]["replacement_commit"] = "0" * 40


def _drift_renderer_api_hash(payload: dict[str, Any]) -> None:
    payload["dependencies"]["expr_renderer_api"]["renderer_api_hash"] = "0" * 64


def _drift_renderer_api_hash_payload(payload: dict[str, Any]) -> None:
    payload["dependencies"]["expr_renderer_api"]["renderer_api_hash_payload_fields"] = [
        "required_signature"
    ]


def _clear_frozen_replacement_coverage(payload: dict[str, Any]) -> None:
    payload["dependencies"]["expr_renderer_api"]["real_goal_coverage_regression_passed"] = False


def _weaken_direct_endpoint_emitter_call(payload: dict[str, Any]) -> None:
    payload["representation_contract"]["sft1_calls_endpoint_emitter_directly_for_both_sides"] = (
        False
    )


def _call_render_closed_prop_directly(payload: dict[str, Any]) -> None:
    payload["representation_contract"]["sft1_calls_render_closed_prop_directly"] = True


def _drop_structural_arrow_support(payload: dict[str, Any]) -> None:
    payload["dependencies"]["expr_renderer_api"][
        "preserves_nondependent_explicit_structural_arrows"
    ] = False


def _allow_ellipsis_render(payload: dict[str, Any]) -> None:
    payload["representation_pre_gate_acceptance"]["forbidden_render_substrings"] = ["[anonymous]"]


def _clear_fixed_preamble_review(payload: dict[str, Any]) -> None:
    payload["representation_pre_gate_acceptance"][
        "fixed_engine_reference_elaboration_preamble_reviewed"
    ] = False


def _split_renderer_requests(payload: dict[str, Any]) -> None:
    payload["representation_contract"]["same_persistent_meta_request"] = False


def _allow_local_universe_canonicalization(payload: dict[str, Any]) -> None:
    payload["representation_contract"][
        "local_u_i_canonicalization_without_repr_profile_allowed"
    ] = True


def _allow_anonymous_outer_telescope(payload: dict[str, Any]) -> None:
    payload["dependencies"]["expr_renderer_api"]["anonymous_telescope_binder_names_allowed"] = True


def _allow_ill_typed_renderer_input(payload: dict[str, Any]) -> None:
    payload["dependencies"]["expr_renderer_api"]["api_rejects_ill_typed_expr"] = False


def _allow_anonymous_p23_binders(payload: dict[str, Any]) -> None:
    payload["p23_binder_hygiene_contract"]["anonymous_name_constructor_allowed"] = True


def _claim_p23_regressions_prematurely(payload: dict[str, Any]) -> None:
    payload["p23_binder_hygiene_contract"]["regressions_passed"] = True


def _bypass_representation_pre_gate(payload: dict[str, Any]) -> None:
    payload["gates"]["one_example"]["requires_representation_pre_gate_acceptance"] = False


def _bypass_shared_contract_update(payload: dict[str, Any]) -> None:
    payload["gates"]["one_example"]["requires_shared_contract_update"] = False


def _remove_mutual_exclusion(payload: dict[str, Any]) -> None:
    payload["composition_grammar"]["mutual_exclusion_groups"] = []


def _allow_negative_operation_in_positive_row(payload: dict[str, Any]) -> None:
    payload["composition_grammar"]["positive_row_negative_operation_count"] = 1


def _approve_negative_family_dimension(payload: dict[str, Any]) -> None:
    payload["negative_family_dimension_admissions"][0]["gate_admitted"] = True


def _drop_negative_family_dimension(payload: dict[str, Any]) -> None:
    payload["negative_family_dimension_admissions"] = payload[
        "negative_family_dimension_admissions"
    ][:-1]


def _undercover_negative_family_dimension(payload: dict[str, Any]) -> None:
    payload["negative_family_dimension_admissions"][0]["operation_ids"] = [
        "N19_NEGATE_CLOSED_CLAIM_RUBRIC_V1"
    ]


def _compile_goal_text(payload: dict[str, Any]) -> None:
    payload["representation_contract"]["compile_goal_v1_text"] = True


def _make_truth_a_label(payload: dict[str, Any]) -> None:
    payload["label_contract"]["candidate_truth_evidence_determines_label"] = True


def _let_candidate_provability_create_a_label(payload: dict[str, Any]) -> None:
    payload["label_contract"]["candidate_provability_may_create_label"] = True


def _make_rubric_claim_f2(payload: dict[str, Any]) -> None:
    payload["label_contract"]["n_rubric"]["claims_f2_truth"] = True


def _remove_claim_guard(payload: dict[str, Any]) -> None:
    guards = _operation(payload, "P32_ADD_COMM_LOCAL_V1")["claim_erasure_guards"]
    _operation(payload, "P32_ADD_COMM_LOCAL_V1")["claim_erasure_guards"] = guards[:-1]


def _permit_post_negative(payload: dict[str, Any]) -> None:
    payload["composition_grammar"]["post_negative_operations_allowed"] = True


def _allow_overlapping_sites(payload: dict[str, Any]) -> None:
    payload["composition_grammar"]["all_sites_pairwise_disjoint_after_typed_rediscovery"] = False


def _drift_cache_key(payload: dict[str, Any]) -> None:
    payload["cache_contract"]["exact_ordered_key_fields"] = payload["cache_contract"][
        "exact_ordered_key_fields"
    ][:-1]


def _remove_evidence_certificate_from_cache(payload: dict[str, Any]) -> None:
    fields = payload["cache_contract"]["exact_ordered_key_fields"]
    payload["cache_contract"]["exact_ordered_key_fields"] = tuple(
        field for field in fields if field != "evidence_certificate_payload_hash"
    )


def _enable_prevalidation_randomness(payload: dict[str, Any]) -> None:
    payload["execution_contract"]["prevalidation_candidate_sampling_uses_randomness"] = True


def _drop_environment_fingerprint_field(payload: dict[str, Any]) -> None:
    payload["execution_contract"]["required_environment_fingerprint_fields"] = payload[
        "execution_contract"
    ]["required_environment_fingerprint_fields"][:-1]


def _resolve_one_operation_without_complete_registry(payload: dict[str, Any]) -> None:
    payload["operation_execution_bindings"]["unselected_or_unadmitted_operation_ids"] = payload[
        "operation_execution_bindings"
    ]["unselected_or_unadmitted_operation_ids"][:-1]


def _drift_certificate_partition(payload: dict[str, Any]) -> None:
    payload["certificate_class_contract"]["bindings"][0]["operation_ids"] = payload[
        "certificate_class_contract"
    ]["bindings"][0]["operation_ids"][:-1]


def _drift_empty_domain_partition(payload: dict[str, Any]) -> None:
    payload["empty_domain_contract"]["profiles"][0]["operation_ids"] = payload[
        "empty_domain_contract"
    ]["profiles"][0]["operation_ids"][:-1]


def _drift_closed_negative_applicability_bank(payload: dict[str, Any]) -> None:
    payload["negative_applicability_banks"]["banks"][0]["admitted_typed_shapes"] = [
        "generic_exponent_increment"
    ]


def _drift_correlation_group(payload: dict[str, Any]) -> None:
    payload["operation_accounting_contract"]["correlation_groups"][0]["operation_ids"] = [
        "P02_REGROUP_BINDERS_V1"
    ]


def _weaken_hundred_root_terminal_conservation(payload: dict[str, Any]) -> None:
    payload["gates"]["hundred_root"]["conservation_equations"] = payload["gates"]["hundred_root"][
        "conservation_equations"
    ][:-1]


def _expose_sidecar_to_surface_diagnostic(payload: dict[str, Any]) -> None:
    payload["hundred_root_surface_diagnostics"][
        "sidecar_or_certificate_fields_visible_to_diagnostic"
    ] = True


def _drift_prevalidation_hash_fields(payload: dict[str, Any]) -> None:
    payload["execution_contract"]["prevalidation_candidate_sampling_hash_fields"] = [
        "source_closed_expr_hash"
    ]


def _drift_blocklist_binding(payload: dict[str, Any]) -> None:
    payload["sampling_and_quality"]["evaluation_blocklist_sha256"] = "0" * 64


def _drift_stable_row_hash_fields(payload: dict[str, Any]) -> None:
    payload["sampling_and_quality"]["stable_row_hash_fields"] = payload["sampling_and_quality"][
        "stable_row_hash_fields"
    ][:-1]


def _keep_nonminimum_same_label_duplicate(payload: dict[str, Any]) -> None:
    payload["sampling_and_quality"]["same_label_duplicate_survivor_rule"] = "keep_first"


def _retain_part_of_conflicting_label_class(payload: dict[str, Any]) -> None:
    payload["sampling_and_quality"]["conflicting_label_class_action"] = "keep_one"


def _swap_orientation_before_caps(payload: dict[str, Any]) -> None:
    payload["sampling_and_quality"]["orientation_swap_after_cap_selection"] = False


def _drop_post_orientation_assertion(payload: dict[str, Any]) -> None:
    payload["sampling_and_quality"][
        "post_orientation_global_model_facing_duplicate_and_conflict_assertion"
    ] = False


def _broaden_n32_to_function_composition(payload: dict[str, Any]) -> None:
    _operation(payload, "N32_SWAP_ROLE_ORDER_RUBRIC_V1")["typed_applicability"] = (
        "function composition reordering"
    )


def _drift_cap_order(payload: dict[str, Any]) -> None:
    order = list(payload["deterministic_cap_order"])
    order[0], order[1] = order[1], order[0]
    payload["deterministic_cap_order"] = order


def _split_ancestry(payload: dict[str, Any]) -> None:
    payload["sampling_and_quality"]["preserve_root_ancestry_clusters"] = False


def _weaken_canary(payload: dict[str, Any]) -> None:
    payload["sampling_and_quality"]["candidate_only_balanced_accuracy_strictly_below"] = 0.61


def _weaken_confidence(payload: dict[str, Any]) -> None:
    payload["sampling_and_quality"]["confidence_level"] = 0.90


def _loosen_operation_cap(payload: dict[str, Any]) -> None:
    payload["cap_contract"]["exact_operation_share_maximum"] = 0.03


def _misorder_operation_budget(payload: dict[str, Any]) -> None:
    _operation(payload, "P01_ALPHA_RENAME_SINGLE_V1")["budget"]["soft_seconds"] = 0.10


def _allow_zero_yield_waiver(payload: dict[str, Any]) -> None:
    payload["gates"]["selected_wave_operation_conformance"]["zero_yield_waiver_allowed"] = True


def _authorize_ten_k(payload: dict[str, Any]) -> None:
    payload["gates"]["ten_k_pilot"]["authorized"] = True


def _break_scale_arithmetic(payload: dict[str, Any]) -> None:
    payload["scale_contract"]["illustrative_arithmetical_maximum_rows"] = 5000000


@pytest.mark.parametrize(
    "mutation",
    [
        _drop_operation,
        _approve_operation,
        _enable_operation,
        _drift_operation_orientation,
        _weaken_same_renderer,
        _make_predecessor_consumable,
        _drift_repr_freeze_commit,
        _drift_renderer_api_hash,
        _drift_renderer_api_hash_payload,
        _clear_frozen_replacement_coverage,
        _weaken_direct_endpoint_emitter_call,
        _call_render_closed_prop_directly,
        _drop_structural_arrow_support,
        _allow_ellipsis_render,
        _clear_fixed_preamble_review,
        _split_renderer_requests,
        _allow_local_universe_canonicalization,
        _allow_anonymous_outer_telescope,
        _allow_ill_typed_renderer_input,
        _allow_anonymous_p23_binders,
        _claim_p23_regressions_prematurely,
        _bypass_representation_pre_gate,
        _bypass_shared_contract_update,
        _remove_mutual_exclusion,
        _allow_negative_operation_in_positive_row,
        _approve_negative_family_dimension,
        _drop_negative_family_dimension,
        _undercover_negative_family_dimension,
        _compile_goal_text,
        _make_truth_a_label,
        _let_candidate_provability_create_a_label,
        _make_rubric_claim_f2,
        _remove_claim_guard,
        _permit_post_negative,
        _allow_overlapping_sites,
        _drift_cache_key,
        _remove_evidence_certificate_from_cache,
        _enable_prevalidation_randomness,
        _drop_environment_fingerprint_field,
        _resolve_one_operation_without_complete_registry,
        _drift_certificate_partition,
        _drift_empty_domain_partition,
        _drift_closed_negative_applicability_bank,
        _drift_correlation_group,
        _weaken_hundred_root_terminal_conservation,
        _expose_sidecar_to_surface_diagnostic,
        _drift_prevalidation_hash_fields,
        _drift_blocklist_binding,
        _drift_stable_row_hash_fields,
        _keep_nonminimum_same_label_duplicate,
        _retain_part_of_conflicting_label_class,
        _swap_orientation_before_caps,
        _drop_post_orientation_assertion,
        _broaden_n32_to_function_composition,
        _drift_cap_order,
        _split_ancestry,
        _weaken_canary,
        _weaken_confidence,
        _loosen_operation_cap,
        _misorder_operation_budget,
        _allow_zero_yield_waiver,
        _authorize_ten_k,
        _break_scale_arithmetic,
    ],
)
def test_policy_rejects_required_invariant_drift(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutation(payload)
    with pytest.raises((ValidationError, ValueError)):
        SFT1CompositionPolicy.model_validate(payload)


def test_registry_freeze_rejects_operation_anchor_drift_before_cross_binding() -> None:
    loaded = load_sft1_composition_policy()
    payload = loaded.config.model_dump(mode="python")
    operation = _operation(payload, "P32_ADD_COMM_LOCAL_V1")
    operation["anchor"]["schema_lemma_procedure_hash"] = "0" * 64

    with pytest.raises(ValidationError, match="exact operation-registry fields differ"):
        SFT1CompositionPolicy.model_validate(payload)


def test_bank_model_rejects_anchor_spec_drift() -> None:
    loaded = load_sft1_composition_policy()
    payload = loaded.banks.model_dump(mode="python")
    payload["banks"][0]["entries"][0]["anchor_spec_hash"] = "0" * 64

    with pytest.raises(ValidationError, match="anchor hash mismatch"):
        SFT1StarterBankSet.model_validate(payload)


def _copy_contract_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "PLAN.md").parent.mkdir(parents=True, exist_ok=True)
    (root / "PLAN.md").write_text("# fixture\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    loaded = load_sft1_composition_policy()
    for source in (
        loaded.path,
        loaded.bank_path,
        Path("data/benchmarks/golden_blocklist_v1.json"),
        Path("configs/representations/goal_v1_v1.yaml"),
        Path("LeanFaith/Meta/GoalV1.lean"),
        Path("src/leanfaith/representations/goal_v1.py"),
        Path(EXPECTED_SIX_GOAL_HELPER_SOURCE_PATH),
        Path(EXPECTED_SIX_GOAL_GATE_CONFIG_PATH),
        Path(EXPECTED_SIX_GOAL_EXECUTION_CONFIG_PATH),
        Path(EXPECTED_SIX_GOAL_RECEIPT_PATH),
    ):
        relative = source.resolve().relative_to(find_repo_root(loaded.path))
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return root


def test_loader_skips_predecessor_and_rejects_approved_repr_dependency_byte_drift(
    tmp_path: Path,
) -> None:
    root = _copy_contract_fixture(tmp_path)
    assert load_sft1_composition_policy(root).config.policy_version == "0.3.1"

    root = _copy_contract_fixture(tmp_path / "repr")
    renderer = root / "LeanFaith/Meta/GoalV1.lean"
    renderer.write_bytes(renderer.read_bytes() + b"\n")
    with pytest.raises(SFT1PolicyError, match="approved REPR Lean renderer file hash drift"):
        load_sft1_composition_policy(root)

    root = _copy_contract_fixture(tmp_path / "other")
    banks = root / "configs/transformations/sft1_value_first_v1/starter_banks_v0_3_0.yaml"
    banks.write_bytes(banks.read_bytes() + b"\n")
    with pytest.raises(SFT1PolicyError, match="starter-bank file hash drift"):
        load_sft1_composition_policy(root)

    root = _copy_contract_fixture(tmp_path / "blocklist")
    blocklist = root / "data/benchmarks/golden_blocklist_v1.json"
    blocklist.write_bytes(blocklist.read_bytes() + b"\n")
    with pytest.raises(SFT1PolicyError, match="evaluation-blocklist file hash drift"):
        load_sft1_composition_policy(root)


def test_loader_rejects_six_goal_helper_and_gate_config_byte_drift(tmp_path: Path) -> None:
    root = _copy_contract_fixture(tmp_path / "helper")
    helper = root / EXPECTED_SIX_GOAL_HELPER_SOURCE_PATH
    helper.write_bytes(helper.read_bytes() + b"\n")
    with pytest.raises(SFT1PolicyError, match="SFT1 six-goal helper file hash drift"):
        load_sft1_composition_policy(root)

    root = _copy_contract_fixture(tmp_path / "config")
    gate_config = root / EXPECTED_SIX_GOAL_GATE_CONFIG_PATH
    gate_config.write_bytes(gate_config.read_bytes() + b"\n")
    with pytest.raises(SFT1PolicyError, match="SFT1 six-goal gate config file hash drift"):
        load_sft1_composition_policy(root)

    root = _copy_contract_fixture(tmp_path / "execution_config")
    execution_config = root / EXPECTED_SIX_GOAL_EXECUTION_CONFIG_PATH
    execution_config.write_bytes(execution_config.read_bytes() + b"\n")
    with pytest.raises(SFT1PolicyError, match="execution config file hash drift"):
        load_sft1_composition_policy(root)

    root = _copy_contract_fixture(tmp_path / "receipt")
    receipt = root / EXPECTED_SIX_GOAL_RECEIPT_PATH
    receipt.write_bytes(receipt.read_bytes() + b"\n")
    with pytest.raises(SFT1PolicyError, match="SFT1 six-goal receipt file hash drift"):
        load_sft1_composition_policy(root)


def test_loader_rejects_duplicate_policy_key_and_path_escape(tmp_path: Path) -> None:
    checked_in = load_sft1_composition_policy().path
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(
        checked_in.read_text(encoding="utf-8") + "\npolicy_version: 0.3.1\n",
        encoding="utf-8",
    )
    with pytest.raises(DuplicateKeyError, match="policy_version"):
        load_config(duplicate, SFT1CompositionPolicy)
    with pytest.raises(SFT1PolicyError, match="escapes the repository"):
        load_sft1_composition_policy(path=tmp_path / "outside.yaml")
