"""Fail-closed tests for the SFT1 Wave 1 admission/readiness receipt."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import leanfaith.sft1.admission_readiness as admission_module
from leanfaith.config.hashing import hash_file, sha256_hex
from leanfaith.config.loading import DuplicateKeyError, load_config
from leanfaith.sft1.admission_readiness import (
    EXPECTED_ADMISSION_RECEIPT_CONFIG_HASH,
    EXPECTED_ADMISSION_RECEIPT_FILE_SHA256,
    EXPECTED_APPROVED_COMMIT,
    EXPECTED_CURRENT_WAVE_OPERATION_IDS,
    EXPECTED_PROJECTS,
    EXPECTED_REVIEW_ATTACHMENT_RAW_SHA256,
    EXPECTED_SECTION_8_NORMALIZED_SHA256,
    EXPECTED_UNRESOLVED_BLOCKER_IDS,
    EXPECTED_USER_ADOPTION_TEXT,
    AdmissionReadinessError,
    BindingStatus,
    Wave1GateAdmissionReceipt,
    load_wave1_gate_admission,
    normalize_section8_markdown,
    validate_wave1_gate_admission,
)

_RECEIPT_PATH = Path("configs/transformations/sft1_value_first_v1/wave1_gate_admission_v0_3_2.yaml")
_SECTION_8_RAW_SHA256 = "300e109e997e9c07f84eaaebf51593c5da341b2fa0420840e73d50945a35cd48"


def _loaded() -> admission_module.LoadedWave1GateAdmission:
    return load_wave1_gate_admission()


def _payload() -> dict[str, Any]:
    return copy.deepcopy(_loaded().config.model_dump(mode="python"))


def _validate_payload(payload: dict[str, Any]) -> None:
    receipt = Wave1GateAdmissionReceipt.model_validate(payload)
    validate_wave1_gate_admission(receipt, _loaded().loaded_base_policy)


def _operation(payload: dict[str, Any], operation_id: str) -> dict[str, Any]:
    for operation in payload["approved_operations"]:
        if operation["operation_id"] == operation_id:
            return operation
    raise AssertionError(f"missing operation {operation_id}")


def _binding(payload: dict[str, Any], operation_id: str) -> dict[str, Any]:
    for binding in payload["readiness"]["operation_bindings"]["operations"]:
        if binding["operation_id"] == operation_id:
            return binding
    raise AssertionError(f"missing operation binding {operation_id}")


def test_checked_in_receipt_is_exact_gate_admitted_and_readiness_blocked() -> None:
    loaded = _loaded()
    receipt = loaded.config

    assert loaded.config_hash == EXPECTED_ADMISSION_RECEIPT_CONFIG_HASH
    assert hash_file(_RECEIPT_PATH) == EXPECTED_ADMISSION_RECEIPT_FILE_SHA256
    assert receipt.receipt_version == "0.3.2"
    assert receipt.status == "gate_admitted_readiness_blocked"
    assert receipt.authorization.gate_admission_recorded is True
    assert receipt.authorization.task_owned_implementation_authorized_now is True
    assert receipt.authorization.bounded_gate_execution_conditionally_authorized is True
    assert receipt.authorization.bounded_gate_execution_may_start_now is False
    assert receipt.authorization.implementation_readiness is False
    assert receipt.readiness.all_blockers_satisfied is False
    assert tuple(receipt.readiness.unresolved_blocker_ids) == EXPECTED_UNRESOLVED_BLOCKER_IDS


def test_review_attachment_section_and_exact_user_adoption_are_hash_bound() -> None:
    review = _loaded().config.review_source
    adoption = _loaded().config.user_adoption

    assert review.attachment_raw_sha256 == EXPECTED_REVIEW_ATTACHMENT_RAW_SHA256
    assert sha256_hex(review.section8_markdown.encode("utf-8")) == _SECTION_8_RAW_SHA256
    normalized = normalize_section8_markdown(review.section8_markdown)
    assert sha256_hex(normalized.encode("utf-8")) == EXPECTED_SECTION_8_NORMALIZED_SHA256
    assert review.section8_normalized_sha256 == EXPECTED_SECTION_8_NORMALIZED_SHA256
    assert adoption.exact_user_text == EXPECTED_USER_ADOPTION_TEXT
    assert adoption.approved_commit == EXPECTED_APPROVED_COMMIT
    assert (
        adoption.adopted_review_section8_normalized_sha256 == EXPECTED_SECTION_8_NORMALIZED_SHA256
    )
    assert "authorizes only task-owned implementation" in review.section8_markdown
    assert "N31 admissions are proof-of-concept gate admissions only" in review.section8_markdown
    assert "does not grant production admission" in review.section8_markdown


def test_exact_six_operations_projects_and_negative_dimension_are_admitted() -> None:
    receipt = _loaded().config

    assert tuple(item.operation_id for item in receipt.approved_operations) == (
        EXPECTED_CURRENT_WAVE_OPERATION_IDS
    )
    assert all(
        tuple(item.registered_eligible_projects) == EXPECTED_PROJECTS
        for item in receipt.approved_operations
    )
    assert all(item.gate_admitted for item in receipt.approved_operations)
    assert all(not item.production_admitted for item in receipt.approved_operations)
    dimension = receipt.negative_dimension_admission
    assert dimension.admission_id == "n31_required_domain_guard_natural_v1"
    assert dimension.family_id == "N31"
    assert dimension.rubric_dimension == "required_domain_guard"
    assert tuple(dimension.operation_ids) == EXPECTED_CURRENT_WAVE_OPERATION_IDS[-2:]
    assert dimension.gate_admitted is True
    assert dimension.proof_of_concept_gate_only is True
    assert dimension.production_admitted is False


def test_all_non_gate_authorities_remain_false() -> None:
    receipt = _loaded().config
    prohibited = receipt.prohibited_authorizations.model_dump(mode="python")

    assert prohibited
    assert all(value is False for value in prohibited.values())
    hold = receipt.authorization.current_session_hold
    assert hold.policy_loader_and_lean_free_tests_allowed is True
    assert hold.task_owned_implementation_allowed is True
    assert hold.lean_execution_prohibited is True
    assert hold.transform_execution_prohibited is True
    assert hold.row_generation_prohibited is True
    bounds = receipt.authorization.bounds
    assert tuple(bounds.authorized_gate_sequence) == (
        "one_positive_one_negative_end_to_end_smoke",
        "selected_wave_operation_project_conformance_matrix",
        "approximately_100_eligible_roots_per_selected_operation",
    )
    assert bounds.smoke_actual_serialized_artifact_count == 2
    assert bounds.operation_project_combination_count == 24
    assert bounds.success_and_rejection_fixture_count == 48
    assert bounds.approximate_total_eligible_roots == 600
    assert bounds.retained_certificate_replay_fraction == 1.0
    assert bounds.bounded_artifacts_are_model_facing_training_rows is False


def test_every_named_readiness_blocker_is_explicit_and_fail_closed() -> None:
    readiness = _loaded().config.readiness

    assert readiness.shared_contract.status == "pending_coordinator_merge"
    assert readiness.shared_contract.satisfied is False
    assert readiness.shared_contract.merged_commit is None
    assert readiness.clean_checkout.status == "passed_hash_bound"
    assert readiness.clean_checkout.satisfied is True
    assert readiness.clean_checkout.passed is True
    assert readiness.clean_checkout.receipt_file_sha256 == (
        "4133c2df44b81b388d3cc39e499feb65d1cd410909b6843591ec6b1295ea3331"
    )
    assert readiness.clean_checkout.receipt_semantic_hash == (
        "90ca160b90e294170a1d88918a6aaf5cf900b8a1c89e8c7f77fcd2c8ba5b89c5"
    )
    assert readiness.zero_lean_census.status == "pending_zero_lean_census"
    assert readiness.zero_lean_census.lean_invoked is False
    assert readiness.zero_lean_census.source_eligibility_matrix_passed is False
    assert readiness.zero_lean_census.census_config_file_sha256 == (
        "a8c6c3616a543ff9e1f5d4700a3b5a86da2442f70475737caf23bd264ebd2aaa"
    )
    assert readiness.zero_lean_census.census_config_semantic_hash == (
        "daf4b26b782d096f77b9677e0a7cef5670103771942c415dc3420b3031eda44e"
    )
    assert (
        tuple(
            item.project_id
            for item in readiness.zero_lean_census.source_proof_availability_by_project
        )
        == EXPECTED_PROJECTS
    )
    assert all(
        item.status.value == "unknown"
        for item in readiness.zero_lean_census.source_proof_availability_by_project
    )
    assert readiness.n31_checker.status == "unresolved_fail_closed"
    assert readiness.n31_checker.guard_bank_file_sha256 == (
        "c2a5aa63158ffbc561bc61f2e3acaa2598aff54a926fd774014e62e6c1cd8cd8"
    )
    assert readiness.n31_checker.guard_bank_semantic_hash == (
        "82bca9b16861412ebaf296591944338932e51f6aaaf8372baa4fd4c1f097f9e1"
    )
    assert readiness.n31_checker.target_head_bank_sha256 is None
    assert readiness.n31_checker.checker_source_sha256 is None
    assert readiness.n31_checker.unknown_nonredundancy_disposition == "typed_not_applicable"
    assert readiness.operation_bindings.status == "unresolved_fail_closed"
    assert (
        tuple(item.operation_id for item in readiness.operation_bindings.operations)
        == EXPECTED_CURRENT_WAVE_OPERATION_IDS
    )
    assert all(
        item.status is BindingStatus.UNRESOLVED_FAIL_CLOSED and not item.ready
        for item in readiness.operation_bindings.operations
    )


def test_loader_replays_exact_base_policy_without_lean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path | None] = []
    real_loader = admission_module.load_sft1_composition_policy

    def _spy(repo_root: Path | None = None, *, path: Path | None = None) -> Any:
        calls.append(path)
        return real_loader(repo_root, path=path)

    monkeypatch.setattr(admission_module, "load_sft1_composition_policy", _spy)
    loaded = load_wave1_gate_admission()
    assert loaded.config.approved_policy.approved_commit == EXPECTED_APPROVED_COMMIT
    assert calls == [Path.cwd() / admission_module.EXPECTED_BASE_POLICY_PATH]


def test_loader_module_is_a_zero_lean_non_execution_boundary() -> None:
    source = Path(admission_module.__file__).read_text(encoding="utf-8")

    assert "from leanfaith.lean" not in source
    assert "import leanfaith.lean" not in source
    assert "LeanInteract" not in source
    assert "subprocess" not in source
    assert "lake env lean" not in source
    config = _loaded().config
    assert not hasattr(config, "execute")
    assert not hasattr(config, "transform")
    assert not hasattr(config, "emit_rows")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["user_adoption"].__setitem__("approved_commit", "0" * 40),
        lambda payload: payload["approved_policy"].__setitem__("approved_commit", "0" * 40),
        lambda payload: payload["approved_policy"].__setitem__("approved_commit_tree", "0" * 40),
        lambda payload: payload["approved_policy"].__setitem__(
            "composition_policy_config_hash", "0" * 64
        ),
        lambda payload: payload["approved_policy"].__setitem__("operation_registry_hash", "0" * 64),
    ],
    ids=[
        "adoption-commit",
        "policy-commit",
        "commit-tree",
        "policy-hash",
        "registry-hash",
    ],
)
def test_commit_and_policy_binding_drift_fail_closed(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((AdmissionReadinessError, ValidationError)):
        _validate_payload(payload)


def test_self_consistent_forged_section8_wording_still_fails_closed() -> None:
    payload = _payload()
    text = payload["review_source"]["section8_markdown"].replace(
        "does not grant production admission",
        "grants production admission",
    )
    payload["review_source"]["section8_markdown"] = text
    payload["review_source"]["section8_raw_sha256"] = sha256_hex(text.encode("utf-8"))
    payload["review_source"]["section8_normalized_sha256"] = sha256_hex(
        normalize_section8_markdown(text).encode("utf-8")
    )
    payload["user_adoption"]["adopted_review_section8_normalized_sha256"] = payload[
        "review_source"
    ]["section8_normalized_sha256"]

    with pytest.raises(AdmissionReadinessError, match="Section 8"):
        _validate_payload(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("review_source", "attachment_raw_sha256"), "0" * 64),
        (("review_source", "review_url"), "https://example.invalid/review"),
        (("user_adoption", "exact_user_text"), "I approve something else."),
        (("user_adoption", "exact_user_text_sha256"), "0" * 64),
        (("review_source", "section8_normalized_sha256"), "0" * 64),
    ],
    ids=["attachment-hash", "review-url", "adoption-text", "adoption-hash", "section-hash"],
)
def test_wording_and_review_provenance_drift_fail_closed(
    path: tuple[str, str],
    value: str,
) -> None:
    payload = _payload()
    payload[path[0]][path[1]] = value
    with pytest.raises((AdmissionReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__(
            "approved_operations", payload["approved_operations"][:-1]
        ),
        lambda payload: payload.__setitem__(
            "approved_operations", tuple(reversed(payload["approved_operations"]))
        ),
        lambda payload: payload["approved_operations"][0].__setitem__(
            "operation_id", "P02_REGROUP_BINDERS_V1"
        ),
        lambda payload: payload["approved_operations"][0].__setitem__(
            "registered_eligible_projects",
            payload["approved_operations"][0]["registered_eligible_projects"][:-1],
        ),
        lambda payload: payload["approved_operations"][0].__setitem__("family_id", "P15"),
        lambda payload: payload["approved_operations"][0].__setitem__("gate_admitted", False),
        lambda payload: payload["approved_operations"][0].__setitem__("production_admitted", True),
    ],
    ids=[
        "missing-operation",
        "operation-order",
        "unapproved-operation",
        "project-scope",
        "family-metadata",
        "gate-revoked",
        "production-escalation",
    ],
)
def test_operation_scope_drift_fails_closed(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((AdmissionReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["negative_dimension_admission"].__setitem__(
            "rubric_dimension", "hypothesis_strength"
        ),
        lambda payload: payload["negative_dimension_admission"].__setitem__(
            "operation_ids", payload["negative_dimension_admission"]["operation_ids"][:-1]
        ),
        lambda payload: payload["negative_dimension_admission"].__setitem__("gate_admitted", False),
        lambda payload: payload["negative_dimension_admission"].__setitem__(
            "proof_of_concept_gate_only", False
        ),
        lambda payload: payload["negative_dimension_admission"].__setitem__(
            "production_admitted", True
        ),
    ],
    ids=["dimension", "negative-operation-set", "gate", "poc-only", "production"],
)
def test_negative_dimension_drift_fails_closed(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((AdmissionReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("authorization", "task_owned_implementation_authorized_now", False),
        ("authorization", "bounded_gate_execution_conditionally_authorized", False),
        ("authorization", "bounded_gate_execution_may_start_now", True),
        ("authorization", "implementation_readiness", True),
        ("prohibited_authorizations", "production_admission", True),
        ("prohibited_authorizations", "model_facing_row_emission", True),
        ("prohibited_authorizations", "ten_k_pilot", True),
        ("prohibited_authorizations", "bulk_generation", True),
        ("prohibited_authorizations", "training", True),
        ("prohibited_authorizations", "publication", True),
        ("prohibited_authorizations", "row_count_commitment", True),
    ],
)
def test_authorization_escalation_or_contraction_fails_closed(
    section: str,
    field: str,
    value: bool,
) -> None:
    payload = _payload()
    payload[section][field] = value
    with pytest.raises((AdmissionReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["authorization"]["bounds"].__setitem__(
            "authorized_gate_sequence",
            (*payload["authorization"]["bounds"]["authorized_gate_sequence"], "ten_k_pilot"),
        ),
        lambda payload: payload["authorization"]["bounds"].__setitem__(
            "operation_project_combination_count", 156
        ),
        lambda payload: payload["authorization"]["bounds"].__setitem__(
            "approximate_total_eligible_roots", 4600
        ),
        lambda payload: payload["authorization"]["bounds"].__setitem__(
            "bounded_artifacts_are_model_facing_training_rows", True
        ),
    ],
    ids=["add-10k", "all-registry-combinations", "all-registry-roots", "training-rows"],
)
def test_bounded_gate_scope_drift_fails_closed(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((AdmissionReadinessError, ValidationError)):
        _validate_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["readiness"].__setitem__(
            "unresolved_blocker_ids", payload["readiness"]["unresolved_blocker_ids"][:-1]
        ),
        lambda payload: payload["readiness"].__setitem__("all_blockers_satisfied", True),
        lambda payload: payload["readiness"]["shared_contract"].__setitem__("satisfied", True),
        lambda payload: payload["readiness"]["clean_checkout"].__setitem__("passed", False),
        lambda payload: payload["readiness"]["zero_lean_census"].__setitem__(
            "source_eligibility_matrix_passed", True
        ),
        lambda payload: payload["readiness"]["zero_lean_census"].__setitem__("lean_invoked", True),
        lambda payload: payload["readiness"]["n31_checker"].__setitem__(
            "required_capabilities",
            payload["readiness"]["n31_checker"]["required_capabilities"][:-1],
        ),
        lambda payload: payload["readiness"]["operation_bindings"].__setitem__(
            "required_binding_components",
            payload["readiness"]["operation_bindings"]["required_binding_components"][:-1],
        ),
        lambda payload: payload["readiness"]["operation_bindings"].__setitem__(
            "operations", payload["readiness"]["operation_bindings"]["operations"][:-1]
        ),
    ],
    ids=[
        "remove-blocker",
        "claim-ready",
        "shared-contract",
        "clean-checkout",
        "census-pass",
        "census-used-lean",
        "n31-capability",
        "binding-component",
        "operation-binding",
    ],
)
def test_readiness_blocker_drift_fails_closed(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises((AdmissionReadinessError, ValidationError)):
        _validate_payload(payload)


def test_partial_operation_binding_is_rejected_atomically() -> None:
    payload = _payload()
    binding = _binding(payload, "P01_ALPHA_RENAME_SINGLE_V1")
    binding["implementation_source_path"] = "LeanFaith/Meta/SFT1/TransformEngine.lean"

    with pytest.raises(ValidationError, match="unresolved operation binding"):
        Wave1GateAdmissionReceipt.model_validate(payload)


def test_resolved_status_without_every_binding_is_rejected_atomically() -> None:
    payload = _payload()
    binding = _binding(payload, "P01_ALPHA_RENAME_SINGLE_V1")
    binding["status"] = "resolved_hash_bound"
    binding["ready"] = True

    with pytest.raises(ValidationError, match="resolved operation binding"):
        Wave1GateAdmissionReceipt.model_validate(payload)


def test_unknown_source_proof_availability_cannot_carry_evidence() -> None:
    payload = _payload()
    project = payload["readiness"]["zero_lean_census"]["source_proof_availability_by_project"][0]
    project["evidence_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="unknown source-proof availability"):
        Wave1GateAdmissionReceipt.model_validate(payload)


def test_unknown_fields_and_duplicate_yaml_keys_fail_closed(tmp_path: Path) -> None:
    payload = _payload()
    payload["silent_production_override"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Wave1GateAdmissionReceipt.model_validate(payload)

    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(
        "schema_version: 1\nschema_version: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(DuplicateKeyError, match="duplicate key"):
        load_config(duplicate, Wave1GateAdmissionReceipt)


def test_public_loader_rejects_frozen_config_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admission_module, "EXPECTED_ADMISSION_RECEIPT_CONFIG_HASH", "0" * 64)
    with pytest.raises(AdmissionReadinessError, match="canonical hash drift"):
        load_wave1_gate_admission()


def test_public_loader_rejects_frozen_raw_file_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admission_module, "EXPECTED_ADMISSION_RECEIPT_FILE_SHA256", "0" * 64)
    with pytest.raises(AdmissionReadinessError, match="raw-file hash drift"):
        load_wave1_gate_admission()


def test_public_loader_rejects_base_policy_raw_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_hash_file = admission_module.hash_file

    def _drift(path: Path) -> str:
        if path.as_posix().endswith(admission_module.EXPECTED_BASE_POLICY_PATH):
            return "0" * 64
        return real_hash_file(path)

    monkeypatch.setattr(admission_module, "hash_file", _drift)
    with pytest.raises(AdmissionReadinessError, match="raw-file hash drift"):
        load_wave1_gate_admission()
