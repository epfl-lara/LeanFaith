from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import load_config, load_yaml_mapping
from leanfaith.sft1.wave1_live_readiness import (
    EXPECTED_OPERATION_IDS,
    EXPECTED_PROJECT_IDS,
    EXPECTED_RUNTIME_FIXTURE_FILE_SHA256,
    EXPECTED_RUNTIME_FIXTURE_HASH,
    LoadedWave1LiveReadiness,
    N31ResolutionProposalBundle,
    PositiveResolvedAnchorInput,
    Wave1LiveReadinessError,
    Wave1RuntimeConfig,
    Wave1RuntimeFixtures,
    assemble_runtime_preamble,
    build_fixture_compile_context,
    compute_n31_proposal_bank_template_hash,
    compute_positive_resolved_anchor_hash,
    compute_runtime_fixture_bundle_hash,
    load_n31_resolution_proposal_bundle,
    load_wave1_live_readiness,
    n31_phase_receipt_id,
    validate_n31_resolution_proposal_bundle,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/sft1/wave1_v0_3_6.yaml"
RUNTIME_CONFIG = ROOT / "configs/transformations/sft1_value_first_v1/wave1_runtime_v0_3_6.yaml"
LOADER = ROOT / "src/leanfaith/sft1/wave1_live_readiness.py"


def _loaded_fixtures() -> Wave1RuntimeFixtures:
    return load_config(FIXTURES, Wave1RuntimeFixtures).config


def test_additive_fixture_is_exact_and_corrects_only_physlib_context() -> None:
    loaded = load_config(FIXTURES, Wave1RuntimeFixtures)
    assert hash_file(FIXTURES) == EXPECTED_RUNTIME_FIXTURE_FILE_SHA256
    assert loaded.config_hash == EXPECTED_RUNTIME_FIXTURE_HASH
    assert tuple(item.operation_id for item in loaded.config.templates[::2]) == (
        EXPECTED_OPERATION_IDS
    )
    physlib = loaded.config.project_contexts[-1]
    assert physlib.project_id == "physlib"
    assert physlib.import_header == "import Physlib"
    assert loaded.config.fixture_execution_is_wave1_gate_execution is False
    assert loaded.config.matrix_contract.exact_positive_fixture_count == 32
    assert loaded.config.matrix_contract.exact_n31_phase_request_count == 8
    assert loaded.config.matrix_contract.combined_40_case_live_checkpoint_allowed is False


def test_checked_in_runtime_loader_replays_its_canonical_hash_closure() -> None:
    loaded = load_wave1_live_readiness(ROOT)
    assert loaded.config_path == RUNTIME_CONFIG
    assert loaded.fixture_path == FIXTURES


def test_n31_fixture_is_proposal_only_and_nproof_is_absent() -> None:
    fixtures = _loaded_fixtures()
    n31 = fixtures.templates[-2:]
    assert tuple(item.expected_engine_terminal for item in n31) == (
        "proposed_not_admitted",
        "proposal_rejected",
    )
    assert fixtures.optional_n31_proof_fixture_count == 0
    assert all("_PROOF_" not in item.operation_id for item in fixtures.templates)


def test_fixture_unknown_field_and_physlean_typo_fail_closed() -> None:
    payload = load_yaml_mapping(FIXTURES)
    payload["unexpected"] = True
    with pytest.raises(ValueError):
        Wave1RuntimeFixtures.model_validate(payload)

    payload = load_yaml_mapping(FIXTURES)
    payload["project_contexts"][-1]["import_header"] = "import PhysLean"
    with pytest.raises(ValueError, match="Physlib fixture"):
        Wave1RuntimeFixtures.model_validate(payload)

    payload = load_yaml_mapping(FIXTURES)
    payload["matrix_contract"]["fixture_kind_order"] = [
        "adversarial_rejection",
        "success",
    ]
    with pytest.raises(ValueError, match="fixture kind order"):
        Wave1RuntimeFixtures.model_validate(payload)

    payload = load_yaml_mapping(FIXTURES)
    payload["templates"][0]["template_id"] = "forged"
    with pytest.raises(ValueError, match="template identity"):
        Wave1RuntimeFixtures.model_validate(payload)


def test_runtime_fixture_bundle_hash_is_operation_specific() -> None:
    fixtures = _loaded_fixtures()
    hashes = tuple(
        compute_runtime_fixture_bundle_hash(fixtures, operation_id)
        for operation_id in EXPECTED_OPERATION_IDS
    )
    assert len(set(hashes)) == len(EXPECTED_OPERATION_IDS)
    with pytest.raises(Wave1LiveReadinessError, match="unknown Wave 1 operation"):
        compute_runtime_fixture_bundle_hash(fixtures, "P99_FORGED_V1")


def test_positive_bankless_hash_convention_binds_live_resolution_receipt() -> None:
    base = PositiveResolvedAnchorInput(
        operation_id="P01_ALPHA_RENAME_SINGLE_V1",
        project_id="mathlib",
        toolchain_revision="v4.31.0-rc1",
        frozen_wave1_source_sha256=(
            "7d4c27e1fd631cc1ba2f8de7cacec1eca618280c12c8ac351d9544a06e94ba4d"
        ),
        runtime_helper_sha256=hash_canonical("runtime-helper"),
        operation_constructor=("LeanFaith.SFT1.Wave1.PrimaryOperation.p01AlphaRenameSingle"),
        dispatch_symbol="LeanFaith.SFT1.Wave1.dispatchAt",
        checker_symbol="LeanFaith.SFT1.Wave1.replayCertificate",
        anchor_hash="ca485f300ecc818057f10877f0eec5c6b4b963fec2e0a574a6895f9d83357095",
        symbol_resolution_receipt_hash=hash_canonical("symbol-receipt"),
    )
    observed = compute_positive_resolved_anchor_hash(base)
    assert observed == hash_canonical(base.model_dump(mode="json"))
    changed = base.model_copy(
        update={"symbol_resolution_receipt_hash": hash_canonical("different")}
    )
    assert compute_positive_resolved_anchor_hash(changed) != observed


def test_positive_bankless_hash_rejects_wrong_anchor() -> None:
    value = PositiveResolvedAnchorInput(
        operation_id="P01_ALPHA_RENAME_SINGLE_V1",
        project_id="mathlib",
        toolchain_revision="v4.31.0-rc1",
        frozen_wave1_source_sha256=(
            "7d4c27e1fd631cc1ba2f8de7cacec1eca618280c12c8ac351d9544a06e94ba4d"
        ),
        runtime_helper_sha256=hash_canonical("runtime-helper"),
        operation_constructor=("LeanFaith.SFT1.Wave1.PrimaryOperation.p01AlphaRenameSingle"),
        dispatch_symbol="LeanFaith.SFT1.Wave1.dispatchAt",
        checker_symbol="LeanFaith.SFT1.Wave1.replayCertificate",
        anchor_hash=hash_canonical("wrong"),
        symbol_resolution_receipt_hash=hash_canonical("symbol-receipt"),
    )
    with pytest.raises(Wave1LiveReadinessError, match="wrong anchor"):
        compute_positive_resolved_anchor_hash(value)


def _loaded_runtime() -> LoadedWave1LiveReadiness:
    config = load_config(RUNTIME_CONFIG, Wave1RuntimeConfig)
    fixtures = load_config(FIXTURES, Wave1RuntimeFixtures)
    return LoadedWave1LiveReadiness(
        config=config.config,
        config_path=config.path,
        config_hash=config.config_hash,
        config_file_sha256=hash_file(RUNTIME_CONFIG),
        fixtures=fixtures.config,
        fixture_path=fixtures.path,
        fixture_hash=fixtures.config_hash,
        fixture_file_sha256=hash_file(FIXTURES),
    )


def _resolved_head(name: str, argument_count: int, tags: list[str]) -> dict[str, object]:
    return {
        "name": name,
        "expected_argument_count": argument_count,
        "observed_argument_count": argument_count,
        "declaration_type_hash": str(10_000 + argument_count),
        "argument_binder_info_tags": tags,
        "declaration_found": True,
        "declaration_type_closed": True,
        "arity_matches": True,
    }


def _entry_resolution(
    entry_id: str,
    guard_shape_id: str,
    guard_name: str,
    guard_arity: int,
) -> dict[str, object]:
    return {
        "entry_id": entry_id,
        "guard_shape_id": guard_shape_id,
        "guard_head": _resolved_head(
            guard_name, guard_arity, ["implicit"] + ["default"] * (guard_arity - 1)
        ),
        "target_head": _resolved_head(
            "HDiv.hDiv",
            6,
            ["implicit", "implicit", "implicit", "instImplicit", "instImplicit", "default"],
        ),
        "guard_fixed_heads": [],
        "target_fixed_heads": [],
        "guard_nested_heads": [],
        "target_nested_heads": [],
        "guard_role_indices_in_range": True,
        "target_role_indices_in_range": True,
        "guard_instance_or_type_indices_in_range": True,
        "target_instance_or_type_indices_in_range": True,
        "guard_role_binder_infos_match": True,
        "target_role_binder_infos_match": True,
        "guard_instance_or_type_binder_infos_match": True,
        "target_instance_or_type_binder_infos_match": True,
        "guard_role_observed_binder_info_tags": ["default"],
        "target_role_observed_binder_info_tags": ["default"],
        "guard_instance_or_type_observed_binder_info_tags": ["implicit"],
        "target_instance_or_type_observed_binder_info_tags": ["implicit"],
        "structural_shape_resolved": True,
        "exact_constraint_terms_closed_and_typed": True,
        "passed": True,
    }


def _path_resolution(index: int, *, role: bool, base: int) -> dict[str, object]:
    binder = "default" if role else "implicit"
    return {
        "path": {"argument_indices": [index]},
        "expected_role_explicit": role,
        "steps": [
            {
                "head_name": "Eq",
                "observed_argument_count": 3,
                "selected_argument_index": index,
                "selected_argument_binder_info": binder,
                "selected_argument_binder_info_tag": binder,
                "declaration_type_hash": str(base + 1),
                "selected_expr_hash": str(base + 2),
                "selected_type_hash": str(base + 3),
                "passed": True,
            }
        ],
        "selected_expr_hash": str(base + 2),
        "selected_type_hash": str(base + 3),
        "selected_binder_info": binder,
        "selected_binder_info_tag": binder,
        "path_resolved": True,
        "binder_info_class_matches": True,
        "passed": True,
    }


def _retained_resolution(zero_hash: str, project_ordinal: int) -> dict[str, object]:
    return {
        "shape_id": "eq_zero_retained_v1",
        "head": _resolved_head("Eq", 3, ["implicit", "default", "default"]),
        "nested_heads": [],
        "witness_found_uniquely": True,
        "witness_expr_hash": str(20_000 + project_ordinal),
        "witness_type_hash": str(21_000 + project_ordinal),
        "witness_is_typed_prop": True,
        "root_head_matches": True,
        "root_arity_matches": True,
        "role_path_resolutions": [
            _path_resolution(1, role=True, base=30_000 + project_ordinal * 10)
        ],
        "instance_or_type_path_resolutions": [
            _path_resolution(0, role=False, base=40_000 + project_ordinal * 10)
        ],
        "nested_head_witness_resolutions": [],
        "literal_witness_resolutions": [],
        "exact_expr_witness_resolutions": [
            {
                "path": {"argument_indices": [2]},
                "expected_expr_hash": zero_hash,
                "selected_expr_hash": zero_hash,
                "path_resolved": True,
                "exact_expr_matches": True,
                "passed": True,
            }
        ],
        "paths_nonempty": True,
        "constraint_witness_replays_passed": True,
        "structural_shape_resolved": True,
        "exact_constraint_terms_closed_and_typed": True,
        "passed": True,
    }


def _bank_entry(
    entry_id: str,
    guard_shape_id: str,
    guard_name: str,
    guard_arity: int,
    guard_role_index: int,
    zero_hash: str,
) -> dict[str, object]:
    return {
        "entry_id": entry_id,
        "guard_shape_id": guard_shape_id,
        "guard_head_name": guard_name,
        "guard_argument_count": guard_arity,
        "guard_role_argument_indices": [guard_role_index],
        "guard_instance_or_type_argument_indices": [0],
        "guard_fixed_heads": [],
        "guard_nested_heads": [],
        "guard_literal_constraints": [],
        "guard_exact_expr_constraints": [
            {
                "path": {"argument_indices": [2]},
                "expected_expr_hash": zero_hash,
            }
        ],
        "target_head_name": "HDiv.hDiv",
        "target_argument_count": 6,
        "target_role_argument_indices": [5],
        "target_instance_or_type_argument_indices": [0],
        "target_fixed_heads": [],
        "target_nested_heads": [],
        "target_literal_constraints": [],
        "target_exact_expr_constraints": [],
    }


def _proposal_payload(
    project_id: str,
    resolved_lean_hash: str,
    resolution_receipt_hash: str,
    entries: list[dict[str, object]],
    retained: list[dict[str, object]],
    *,
    phase_two: bool,
) -> dict[str, object]:
    return {
        "operation_id": "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        "identity": {
            "project_id": project_id,
            "bank_id": "sft1_n31_nat_ne_zero_hdiv_proposal_v0_3_6",
            "resolved_lean_hash": resolved_lean_hash if phase_two else "",
            "resolution_receipt_hash": resolution_receipt_hash if phase_two else "",
        },
        "identity_project_and_bank_nonempty": True,
        "resolved_lean_hash_populated": phase_two,
        "resolution_receipt_hash_populated": phase_two,
        "entry_ids_unique": True,
        "retained_shape_ids_unique": True,
        "entries": entries,
        "retained_patterns": retained,
        "selectable_guard_definitions_coherent": True,
        "retained_shapes_disjoint_from_selectable": True,
        "implication_references_resolve": True,
        "contradiction_references_resolve": True,
        "all_entry_resolutions_passed": True,
        "all_retained_pattern_resolutions_passed": True,
        "all_names_resolved": True,
        "all_arities_resolved": True,
        "all_type_and_instance_constraints_resolved": True,
        "frozen_admission_is_empty": True,
        "proposed_identity_already_admitted": False,
        "private_semantic_checker_available": False,
        "semantic_success_conformance_performed": False,
        "semantic_adversarial_conformance_performed": False,
        "activation_exposed": False,
        "candidate_exposed": False,
        "proposal_resolution_passed": True,
    }


def _receipt_preimage(
    bank_payload: dict[str, object], proposal: dict[str, object]
) -> dict[str, object]:
    return {
        "basis_id": "sft1_n31_resolution_receipt_hash_preimage_v0_3_6",
        "sha256_input_contract": "python_canonical_json_utf8_v1",
        "bank_fingerprint_payload": bank_payload,
        "operation_id": proposal["operation_id"],
        "identity_project_and_bank_nonempty": proposal["identity_project_and_bank_nonempty"],
        "entry_ids_unique": proposal["entry_ids_unique"],
        "retained_shape_ids_unique": proposal["retained_shape_ids_unique"],
        "selectable_guard_definitions_coherent": proposal["selectable_guard_definitions_coherent"],
        "retained_shapes_disjoint_from_selectable": proposal[
            "retained_shapes_disjoint_from_selectable"
        ],
        "implication_references_resolve": proposal["implication_references_resolve"],
        "contradiction_references_resolve": proposal["contradiction_references_resolve"],
        "all_entry_resolutions_passed": proposal["all_entry_resolutions_passed"],
        "all_retained_pattern_resolutions_passed": proposal[
            "all_retained_pattern_resolutions_passed"
        ],
        "all_names_resolved": proposal["all_names_resolved"],
        "all_arities_resolved": proposal["all_arities_resolved"],
        "all_type_and_instance_constraints_resolved": proposal[
            "all_type_and_instance_constraints_resolved"
        ],
        "frozen_admission_is_empty": proposal["frozen_admission_is_empty"],
        "proposed_identity_already_admitted": proposal["proposed_identity_already_admitted"],
        "private_semantic_checker_available": proposal["private_semantic_checker_available"],
        "semantic_success_conformance_performed": proposal[
            "semantic_success_conformance_performed"
        ],
        "semantic_adversarial_conformance_performed": proposal[
            "semantic_adversarial_conformance_performed"
        ],
        "activation_exposed": proposal["activation_exposed"],
        "proposal_resolution_passed": proposal["proposal_resolution_passed"],
    }


def _n31_project_payload(
    loaded: LoadedWave1LiveReadiness, project_id: str, project_ordinal: int
) -> dict[str, object]:
    preamble = assemble_runtime_preamble(ROOT, loaded.config.source_bindings)
    project = next(
        item for item in loaded.fixtures.project_contexts if item.project_id == project_id
    )
    context = build_fixture_compile_context(project, assembled_preamble=preamble.text)
    zero_hash = str(50_000 + project_ordinal)
    entries = [
        _entry_resolution("n31_ne_zero_hdiv_nat_v0_3_6", "ne_zero_guard_v1", "Ne", 3),
        _entry_resolution("n31_positive_hdiv_nat_v0_3_6", "positive_guard_v1", "LT.lt", 4),
    ]
    retained = [_retained_resolution(zero_hash, project_ordinal)]
    bank_payload: dict[str, object] = {
        "basis_id": "sft1_n31_structural_bank_fingerprint_payload_v0_3_6",
        "sha256_input_contract": "python_canonical_json_utf8_v1",
        "structural_expr_fingerprint_id": "lean_hashable_expr_uint64_decimal_v1",
        "identity": {
            "project_id": project_id,
            "bank_id": "sft1_n31_nat_ne_zero_hdiv_proposal_v0_3_6",
            "resolved_lean_hash": "",
            "resolution_receipt_hash": "",
        },
        "entries": [
            _bank_entry(
                "n31_ne_zero_hdiv_nat_v0_3_6",
                "ne_zero_guard_v1",
                "Ne",
                3,
                1,
                zero_hash,
            ),
            _bank_entry(
                "n31_positive_hdiv_nat_v0_3_6",
                "positive_guard_v1",
                "LT.lt",
                4,
                3,
                zero_hash,
            ),
        ],
        "retained_contradiction_patterns": [
            {
                "shape_id": "eq_zero_retained_v1",
                "head_name": "Eq",
                "argument_count": 3,
                "role_paths": [{"argument_indices": [1]}],
                "instance_or_type_paths": [{"argument_indices": [0]}],
                "nested_heads": [],
                "literal_constraints": [],
                "exact_expr_constraints": [
                    {
                        "path": {"argument_indices": [2]},
                        "expected_expr_hash": zero_hash,
                    }
                ],
            }
        ],
        "implications": [
            {
                "premise_shape_id": "positive_guard_v1",
                "conclusion_shape_id": "ne_zero_guard_v1",
            }
        ],
        "contradictions": [
            {
                "retained_shape_id": "eq_zero_retained_v1",
                "removed_shape_id": "ne_zero_guard_v1",
            }
        ],
        "resolved_entries": entries,
        "resolved_retained_patterns": retained,
    }
    resolved_lean_hash = hash_canonical(bank_payload)
    phase_one_proposal = _proposal_payload(project_id, "", "", entries, retained, phase_two=False)
    receipt_preimage = _receipt_preimage(bank_payload, phase_one_proposal)
    resolution_receipt_hash = hash_canonical(receipt_preimage)
    phase_two_proposal = _proposal_payload(
        project_id,
        resolved_lean_hash,
        resolution_receipt_hash,
        entries,
        retained,
        phase_two=True,
    )
    external_hash_contract = {
        "algorithm": "sha256",
        "canonicalization": "python_canonical_json_utf8_v1",
        "resolved_lean_hash_preimage_field": "bank_fingerprint_payload",
        "resolution_receipt_hash_preimage_field": ("resolution_receipt_hash_preimage_payload"),
        "identity_equality_rechecked_in_second_meta_request": True,
        "payload_digest_verification_owned_by_strict_runner": True,
    }
    phase_one: dict[str, object] = {
        "schema_version": 1,
        "receipt_kind": "n31_proposal_resolution",
        "receipt_id": n31_phase_receipt_id(project_id, "phase_one"),
        "source_version": "sft1_wave1_runtime_readiness_v0_3_6",
        "proposal": phase_one_proposal,
        "bank_fingerprint_payload": bank_payload,
        "resolution_receipt_hash_preimage_payload": receipt_preimage,
        "external_hash_installation_contract": external_hash_contract,
        "candidate_constructed": False,
        "candidate_exposed": False,
        "semantic_conformance_performed": False,
        "row_or_gate_emitted": False,
    }
    phase_two: dict[str, object] = {
        "schema_version": 1,
        "receipt_kind": "n31_frozen_nonactivation",
        "receipt_id": n31_phase_receipt_id(project_id, "phase_two"),
        "source_version": "sft1_wave1_runtime_readiness_v0_3_6",
        "operation_id": "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        "identity": {
            "project_id": project_id,
            "bank_id": "sft1_n31_nat_ne_zero_hdiv_proposal_v0_3_6",
            "resolved_lean_hash": resolved_lean_hash,
            "resolution_receipt_hash": resolution_receipt_hash,
        },
        "expected_resolved_lean_hash": resolved_lean_hash,
        "expected_resolution_receipt_hash": resolution_receipt_hash,
        "expected_hashes_nonempty": True,
        "expected_hashes_are_lower_hex_sha256": True,
        "identity_matches_expected_hashes": True,
        "external_hash_computation_performed_in_lean": False,
        "external_strict_runner_hash_verification_required": True,
        "source_expr_hash": str(60_000 + project_ordinal),
        "selector": {
            "kind": "requiredGuard",
            "guard_ordinal": 1,
            "target_position": "/",
            "target_position_nat": "1",
            "bank_entry_id": "n31_ne_zero_hdiv_nat_v0_3_6",
        },
        "reachability": {
            "mode_id": "explicit_telescope_witness_and_retained_hypothesis_proofs",
            "guard_ordinal": 1,
            "assignment_expr_hashes": [
                str(70_000 + project_ordinal),
                str(80_000 + project_ordinal),
            ],
        },
        "proposal": phase_two_proposal,
        "bank_fingerprint_payload": bank_payload,
        "resolution_receipt_hash_preimage_payload": receipt_preimage,
        "external_hash_installation_contract": external_hash_contract,
        "proposal_resolution_passed": True,
        "frozen_admission_is_empty": True,
        "identity_absent_from_frozen_admission": True,
        "frozen_dispatch_rejected_as_unadmitted_bank": True,
        "rejection_reason": "n31BankInvalid",
        "private_semantic_checker_available": False,
        "semantic_conformance_performed": False,
        "candidate_constructed": False,
        "candidate_exposed": False,
        "activation_exposed": False,
        "row_or_gate_emitted": False,
    }
    core: dict[str, object] = {
        "project_id": project_id,
        "compile_context_id": context.compile_context_id,
        "compile_context_fingerprint": context.fingerprint,
        "bank_id": "sft1_n31_nat_ne_zero_hdiv_proposal_v0_3_6",
        "bank_template_hash": compute_n31_proposal_bank_template_hash(
            loaded.config.n31_proposal_bank
        ),
        "resolved_lean_hash": resolved_lean_hash,
        "resolution_receipt_hash": resolution_receipt_hash,
        "phase_one_request_hash": hash_canonical({"project": project_id, "phase": 1}),
        "phase_two_request_hash": hash_canonical({"project": project_id, "phase": 2}),
        "phase_one_raw_response_sha256": hash_canonical({"project": project_id, "raw": 1}),
        "phase_two_raw_response_sha256": hash_canonical({"project": project_id, "raw": 2}),
        "phase_one_task_receipt": phase_one,
        "phase_two_task_receipt": phase_two,
        "phase_one_task_receipt_hash": hash_canonical(phase_one),
        "phase_two_task_receipt_hash": hash_canonical(phase_two),
        "exact_name_arity_type_instance_resolution_passed": True,
        "frozen_nonactivation_replayed": True,
        "runtime_activated": False,
        "semantic_success_conformance_performed": False,
        "semantic_adversarial_conformance_performed": False,
        "candidate_constructed": False,
        "row_or_gate_emitted": False,
        "elapsed_ms": project_ordinal + 1,
        "measured_peak_rss_bytes": project_ordinal + 1,
    }
    return {**core, "project_receipt_hash": hash_canonical(core)}


def _n31_bundle_payload() -> dict[str, object]:
    loaded = _loaded_runtime()
    preamble = assemble_runtime_preamble(ROOT, loaded.config.source_bindings)
    proposals = [
        _n31_project_payload(loaded, project_id, ordinal)
        for ordinal, project_id in enumerate(EXPECTED_PROJECT_IDS)
    ]
    implementation_identity_core: dict[str, object] = {
        "schema_version": 1,
        "worktree": str(ROOT),
        "implementation_commit": "1" * 40,
        "implementation_tree": "2" * 40,
        "status_porcelain_sha256": sha256_hex(b""),
        "worktree_clean": True,
        "verified_before_resource_claim": True,
    }
    implementation_identity = {
        **implementation_identity_core,
        "verification_hash": hash_canonical(implementation_identity_core),
    }
    resource_snapshot = {
        "task": "SFT1",
        "lean_workers": 1,
        "lean_rss_gib": 24.0,
        "gpu": False,
        "pid": 1,
        "owner_session": "unit-test",
        "hostname": "unit-test-host",
        "worktree": str(ROOT),
        "created_at": "2026-08-31T00:00:00+00:00",
    }
    resource_snapshot_hash = sha256_hex(canonical_json_bytes(resource_snapshot) + b"\n")
    run_spec_hash = hash_canonical("n31 run spec")
    positive_hash = hash_canonical("positive checkpoint")
    project_completions = [
        {
            "project_id": project_id,
            "path": str(ROOT / f".test-n31-{project_id}.completion.json"),
            "file_sha256": hash_canonical([project_id, "completion file"]),
            "completion_hash": hash_canonical([project_id, "completion"]),
        }
        for project_id in EXPECTED_PROJECT_IDS
    ]
    project_journals = [
        {
            "project_id": project_id,
            "path": str(ROOT / f".test-n31-{project_id}.journal.jsonl"),
            "file_sha256": hash_canonical([project_id, "journal file"]),
            "final_chain_hash": hash_canonical([project_id, "journal chain"]),
        }
        for project_id in EXPECTED_PROJECT_IDS
    ]
    terminal_core = {
        "schema_version": 1,
        "terminal_status": "stopped_for_exact_n31_user_admission",
        "run_spec_hash": run_spec_hash,
        "positive_checkpoint_receipt_hash": positive_hash,
        "project_journals": project_journals,
        "resource_released": True,
        "n31_activation_performed": False,
        "semantic_conformance_performed": False,
        "wave1_gate_executed": False,
        "model_facing_rows_emitted": False,
        "exact_user_decision_required": [],
    }
    core: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": "sft1_wave1_n31_resolution_proposal_v0_3_6",
        "run_spec_hash": run_spec_hash,
        "run_spec_path": str(ROOT / ".test-n31.run-spec.json"),
        "run_spec_file_sha256": hash_canonical("run spec file"),
        "positive_checkpoint_receipt_hash": positive_hash,
        "positive_checkpoint_receipt_path": str(ROOT / ".test-positive.json"),
        "positive_checkpoint_receipt_file_sha256": hash_canonical("positive file"),
        "runtime_config_file_sha256": loaded.config_file_sha256,
        "runtime_config_hash": loaded.config_hash,
        "runtime_fixture_file_sha256": loaded.fixture_file_sha256,
        "runtime_fixture_hash": loaded.fixture_hash,
        "runtime_loader_file_sha256": hash_file(LOADER),
        "live_runner_file_sha256": hash_canonical("live runner"),
        "implementation_commit": "1" * 40,
        "implementation_tree": "2" * 40,
        "implementation_identity_receipt": implementation_identity,
        "implementation_identity_receipt_hash": implementation_identity["verification_hash"],
        "assembled_preamble_sha256": preamble.sha256,
        "resource_claim_id": f"SFT1:{resource_snapshot_hash[:24]}",
        "resource_claim_snapshot": resource_snapshot,
        "resource_claim_snapshot_hash": resource_snapshot_hash,
        "resource_released": True,
        "persistent_worker_count": 1,
        "measured_combined_peak_rss_bytes": 4,
        "measured_total_lean_seconds": 0.01,
        "elab_async": False,
        "per_row_process_spawned": False,
        "corpus_compiled": False,
        "proposals": proposals,
        "project_completions": project_completions,
        "project_journals": project_journals,
        "journal_is_durable_log": True,
        "heartbeat_path": str(ROOT / ".test-n31.heartbeat.json"),
        "heartbeat_file_sha256": hash_canonical("heartbeat file"),
        "n31_activation_performed": False,
        "semantic_success_conformance_performed": False,
        "semantic_adversarial_conformance_performed": False,
        "wave1_gate_executed": False,
        "model_facing_rows_emitted": False,
        "terminal_status": "stopped_for_exact_n31_user_admission",
        "exact_user_admission_fields": [
            "project_id",
            "bank_id",
            "resolved_lean_hash",
            "resolution_receipt_hash",
        ],
        "terminal_marker_path": str(ROOT / ".test-n31.terminal.json"),
        "terminal_marker_preimage_hash": hash_canonical(terminal_core),
    }
    return {**core, "receipt_hash": hash_canonical(core)}


def _rehash_project(project: dict[str, object]) -> None:
    core = deepcopy(project)
    core.pop("project_receipt_hash")
    project["project_receipt_hash"] = hash_canonical(core)


def _rehash_bundle(payload: dict[str, object]) -> None:
    core = deepcopy(payload)
    core.pop("receipt_hash")
    payload["receipt_hash"] = hash_canonical(core)


def test_n31_four_project_proposal_replays_both_canonical_hash_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import leanfaith.sft1.wave1_live_runner as live_runner

    monkeypatch.setattr(
        live_runner,
        "validate_n31_proposal_checkpoint",
        lambda loaded, payload: N31ResolutionProposalBundle.model_validate(payload),
    )
    payload = _n31_bundle_payload()
    receipt = N31ResolutionProposalBundle.model_validate(payload)
    assert tuple(item.project_id for item in receipt.proposals) == EXPECTED_PROJECT_IDS
    assert receipt.terminal_status == "stopped_for_exact_n31_user_admission"
    assert receipt.n31_activation_performed is False
    for proposal in receipt.proposals:
        assert proposal.resolved_lean_hash == hash_canonical(
            proposal.phase_one_task_receipt["bank_fingerprint_payload"]
        )
        assert proposal.resolution_receipt_hash == hash_canonical(
            proposal.phase_one_task_receipt["resolution_receipt_hash_preimage_payload"]
        )
        assert proposal.phase_one_task_receipt_hash == hash_canonical(
            proposal.phase_one_task_receipt
        )
        assert proposal.phase_two_task_receipt_hash == hash_canonical(
            proposal.phase_two_task_receipt
        )
    validate_n31_resolution_proposal_bundle(receipt, repo_root=ROOT)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("project_order", "project inventory/order"),
        ("terminal", "terminal_status"),
        ("activation", "n31_activation_performed"),
        ("candidate", "candidate_constructed"),
        ("phase_one_extra", "field inventory"),
        ("phase_id", "receipt identity"),
        ("resolved_hash", "external hash replay"),
        ("preimage", "receipt preimage replay"),
    ],
)
def test_n31_proposal_model_rejects_adversarial_receipt_drift(mutation: str, message: str) -> None:
    payload = _n31_bundle_payload()
    proposals = payload["proposals"]
    assert isinstance(proposals, list) and isinstance(proposals[0], dict)
    first = proposals[0]
    if mutation == "project_order":
        proposals[0], proposals[1] = proposals[1], proposals[0]
    elif mutation == "terminal":
        payload["terminal_status"] = "continue"
    elif mutation == "activation":
        payload["n31_activation_performed"] = True
    elif mutation == "candidate":
        first["candidate_constructed"] = True
        _rehash_project(first)
    elif mutation == "phase_one_extra":
        phase_one = first["phase_one_task_receipt"]
        assert isinstance(phase_one, dict)
        phase_one["unexpected"] = False
        first["phase_one_task_receipt_hash"] = hash_canonical(phase_one)
        _rehash_project(first)
    elif mutation == "phase_id":
        phase_one = first["phase_one_task_receipt"]
        assert isinstance(phase_one, dict)
        phase_one["receipt_id"] = "forged"
        first["phase_one_task_receipt_hash"] = hash_canonical(phase_one)
        _rehash_project(first)
    elif mutation == "resolved_hash":
        first["resolved_lean_hash"] = hash_canonical("forged")
        _rehash_project(first)
    else:
        phase_one = first["phase_one_task_receipt"]
        phase_two = first["phase_two_task_receipt"]
        assert isinstance(phase_one, dict) and isinstance(phase_two, dict)
        preimage = phase_one["resolution_receipt_hash_preimage_payload"]
        assert isinstance(preimage, dict)
        preimage["all_names_resolved"] = False
        phase_two["resolution_receipt_hash_preimage_payload"] = deepcopy(preimage)
        new_hash = hash_canonical(preimage)
        first["resolution_receipt_hash"] = new_hash
        for task in (phase_one, phase_two):
            identity = task.get("identity")
            if isinstance(identity, dict):
                identity["resolution_receipt_hash"] = new_hash
            proposal = task["proposal"]
            assert isinstance(proposal, dict)
            proposal_identity = proposal["identity"]
            assert isinstance(proposal_identity, dict)
            if proposal["resolution_receipt_hash_populated"] is True:
                proposal_identity["resolution_receipt_hash"] = new_hash
        phase_two["expected_resolution_receipt_hash"] = new_hash
        first["phase_one_task_receipt_hash"] = hash_canonical(phase_one)
        first["phase_two_task_receipt_hash"] = hash_canonical(phase_two)
        _rehash_project(first)
    _rehash_bundle(payload)
    with pytest.raises(ValueError, match=message):
        N31ResolutionProposalBundle.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("config", "config/fixture binding"),
        ("loader", "current strict loader"),
        ("preamble", "preamble binding"),
        ("context", "compile-context drift"),
        ("bank", "bank-template drift"),
        ("duplicate_request", "duplicate N31 request hash"),
    ],
)
def test_n31_proposal_loader_validation_rejects_repository_binding_drift(
    mutation: str, message: str
) -> None:
    payload = _n31_bundle_payload()
    proposals = payload["proposals"]
    assert isinstance(proposals, list)
    if mutation == "config":
        payload["runtime_config_hash"] = hash_canonical("forged config")
    elif mutation == "loader":
        payload["runtime_loader_file_sha256"] = hash_canonical("forged loader")
    elif mutation == "preamble":
        payload["assembled_preamble_sha256"] = hash_canonical("forged preamble")
    elif mutation == "context":
        first = proposals[0]
        assert isinstance(first, dict)
        forged = hash_canonical("forged context")
        first["compile_context_id"] = f"ctx:{forged}"
        first["compile_context_fingerprint"] = forged
        _rehash_project(first)
    elif mutation == "bank":
        first = proposals[0]
        assert isinstance(first, dict)
        first["bank_template_hash"] = hash_canonical("forged bank")
        _rehash_project(first)
    else:
        first, second = proposals[:2]
        assert isinstance(first, dict) and isinstance(second, dict)
        second["phase_one_request_hash"] = first["phase_one_request_hash"]
        _rehash_project(second)
    _rehash_bundle(payload)
    receipt = N31ResolutionProposalBundle.model_validate(payload)
    with pytest.raises(Wave1LiveReadinessError, match=message):
        validate_n31_resolution_proposal_bundle(receipt, repo_root=ROOT)


def test_n31_project_receipt_embeds_complete_git_independent_task_payloads() -> None:
    receipt = N31ResolutionProposalBundle.model_validate(_n31_bundle_payload())
    for proposal in receipt.proposals:
        assert proposal.phase_one_task_receipt["receipt_kind"] == "n31_proposal_resolution"
        assert proposal.phase_two_task_receipt["receipt_kind"] == "n31_frozen_nonactivation"
        assert (
            proposal.phase_one_task_receipt["bank_fingerprint_payload"]
            == (proposal.phase_two_task_receipt["bank_fingerprint_payload"])
        )
        assert proposal.phase_two_task_receipt["rejection_reason"] == "n31BankInvalid"
        assert proposal.phase_two_task_receipt["candidate_exposed"] is False


def test_n31_proposal_loader_rejects_any_noncontract_receipt_path(tmp_path: Path) -> None:
    with pytest.raises(Wave1LiveReadinessError, match="path differs"):
        load_n31_resolution_proposal_bundle(ROOT, receipt_path=tmp_path / "forged-proposal.json")
