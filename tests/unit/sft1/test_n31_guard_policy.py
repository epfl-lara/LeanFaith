"""Lean-free invariants for the closed N31 required-guard policy."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import leanfaith.sft1.n31_guard_policy as policy_module
from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import ConfigError, DuplicateKeyError, load_yaml_mapping
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.n31_guard_policy import (
    DEFAULT_N31_GUARD_BANK_PATH,
    EXPECTED_FAILURE_REASONS,
    EXPECTED_N31_GUARD_BANK_CONFIG_HASH,
    EXPECTED_N31_GUARD_BANK_FILE_SHA256,
    EXPECTED_OPERATION_IDS,
    EXPECTED_SHAPES,
    CheckerOutcome,
    GuardShape,
    N31CheckerFacts,
    N31FailureReason,
    N31GuardBank,
    ReachabilityStatus,
    decide_n31_checker_facts,
    load_n31_guard_bank,
)


def _payload() -> dict[str, Any]:
    path = find_repo_root() / DEFAULT_N31_GUARD_BANK_PATH
    return copy.deepcopy(load_yaml_mapping(path))


def _all_pass_facts() -> dict[str, object]:
    return {
        "recognized_guard_shape": GuardShape.NE_ZERO,
        "exact_guard_local_identity": True,
        "guard_not_definitionally_true": True,
        "target_site_unique": True,
        "protected_roles_match": True,
        "target_head_in_frozen_bank": True,
        "guard_body_dependency_present": True,
        "protected_target_relation_established": True,
        "competing_retained_guard_absent": True,
        "retained_contradiction_absent": True,
        "reachability_status": ReachabilityStatus.REACHABLE,
        "nonredundancy_established": True,
        "exact_single_local_deletion": True,
        "exact_de_bruijn_reindex": True,
        "nonselected_structure_unchanged": True,
        "endpoints_nondefeq": True,
        "exact_delta_replay_passed": True,
    }


def test_checked_in_n31_guard_bank_loads_with_frozen_hashes() -> None:
    root = find_repo_root()
    loaded = load_n31_guard_bank(root)
    assert loaded.config_hash == EXPECTED_N31_GUARD_BANK_CONFIG_HASH
    assert hash_file(root / DEFAULT_N31_GUARD_BANK_PATH) == (EXPECTED_N31_GUARD_BANK_FILE_SHA256)


def test_loader_rejects_any_alternate_path() -> None:
    root = find_repo_root()
    alternate = root / "configs/transformations/sft1_value_first_v1/starter_banks_v0_3_0.yaml"
    with pytest.raises(ConfigError, match="path differs"):
        load_n31_guard_bank(root, path=alternate)


def test_loader_rejects_frozen_file_hash_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy_module, "hash_file", lambda _path: "0" * 64)
    with pytest.raises(ConfigError, match="file hash differs"):
        load_n31_guard_bank()


def test_contract_is_policy_only_and_fail_closed() -> None:
    contract = load_n31_guard_bank().config
    assert contract.operation_ids == EXPECTED_OPERATION_IDS
    assert contract.implementation_resolved is False
    assert contract.execution_ready is False
    assert contract.production_eligible is False
    assert contract.row_emission_authorized is False
    assert contract.target_head_bank_binding.status == "unresolved"
    assert contract.target_head_bank_binding.bank_hash is None
    assert contract.target_head_bank_binding.checker_symbol is None


def test_exact_five_shape_bank_and_failure_taxonomy() -> None:
    contract = load_n31_guard_bank().config
    assert tuple(shape.shape_id.value for shape in contract.guard_shapes) == EXPECTED_SHAPES
    assert tuple(reason.value for reason in contract.fail_closed_reasons) == (
        EXPECTED_FAILURE_REASONS
    )


def test_implication_closure_rejects_positive_redundancy() -> None:
    contract = load_n31_guard_bank().config
    edges = {
        (edge.premise_shape, edge.conclusion_shape) for edge in contract.frozen_implication_closure
    }
    assert edges == {
        (GuardShape.POSITIVE, GuardShape.NE_ZERO),
        (GuardShape.POSITIVE, GuardShape.NONNEGATIVE),
    }
    assert all(edge.required_same_type for edge in contract.frozen_implication_closure)
    assert all(edge.required_same_instance for edge in contract.frozen_implication_closure)


def test_each_shape_has_reflexive_competitor_and_contradiction() -> None:
    contract = load_n31_guard_bank().config
    for shape in contract.guard_shapes:
        assert shape.shape_id in shape.competing_guard_shapes
        assert shape.contradictory_retained_shapes
    assert all(item.required_same_type for item in contract.retained_contradiction_shapes)
    assert all(item.required_same_instance for item in contract.retained_contradiction_shapes)


def test_live_conformance_and_shape_regressions_are_separate() -> None:
    fixtures = load_n31_guard_bank().config.adversarial_fixture_requirements
    assert fixtures[0] == "live_conformance_one_success_and_one_rejection_per_operation_project"
    assert fixtures[1] == "regression_bank_covers_each_guard_shape_at_least_once_over_project_union"
    assert "one_success_per_shape_and_project" not in fixtures


def test_rubric_and_proof_lanes_remain_distinct() -> None:
    lanes = load_n31_guard_bank().config.lane_contracts
    assert lanes.n_rubric.source_proof_required is False
    assert lanes.n_rubric.candidate_refutation_required is False
    assert lanes.n_rubric.makes_f2_claim is False
    assert lanes.n_proof.parent_operation_id == lanes.n_rubric.operation_id
    assert lanes.n_proof.candidate_truth_required == "refuted"
    assert lanes.n_proof.separate_cap_required is True
    assert lanes.n_proof.separately_reported_stratum is True


def test_unknown_is_typed_not_applicable_and_never_a_label() -> None:
    decision = load_n31_guard_bank().config.decision_contract
    assert decision.unknown_outcome == CheckerOutcome.TYPED_NOT_APPLICABLE
    assert decision.unknown_may_create_negative_label is False
    assert decision.generic_d0_is_label_evidence is False
    assert decision.failed_search_is_evidence is False
    assert decision.unrestricted_theorem_search_allowed is False


def test_complete_checker_facts_are_applicable() -> None:
    result = decide_n31_checker_facts(N31CheckerFacts.model_validate(_all_pass_facts()))
    assert result.outcome == CheckerOutcome.APPLICABLE
    assert result.failure_reason is None


def test_checker_fact_booleans_are_strict() -> None:
    facts = _all_pass_facts()
    facts["nonredundancy_established"] = "true"
    with pytest.raises(ValidationError):
        N31CheckerFacts.model_validate(facts)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("recognized_guard_shape", None, N31FailureReason.ARBITRARY_PROP),
        (
            "guard_not_definitionally_true",
            False,
            N31FailureReason.GUARD_TRUE,
        ),
        ("target_site_unique", False, N31FailureReason.TARGET_MISSING_OR_AMBIGUOUS),
        ("protected_roles_match", False, N31FailureReason.ROLE_MISMATCH),
        ("target_head_in_frozen_bank", False, N31FailureReason.TARGET_HEAD_UNBOUND),
        ("guard_body_dependency_present", False, N31FailureReason.UNUSED_GUARD),
        (
            "protected_target_relation_established",
            False,
            N31FailureReason.UNRELATED_TARGET,
        ),
        (
            "competing_retained_guard_absent",
            False,
            N31FailureReason.COMPETING_GUARD,
        ),
        (
            "retained_contradiction_absent",
            False,
            N31FailureReason.CONTRADICTORY_CONTEXT,
        ),
        (
            "reachability_status",
            ReachabilityStatus.UNKNOWN,
            N31FailureReason.REACHABILITY_UNKNOWN,
        ),
        (
            "reachability_status",
            ReachabilityStatus.UNREACHABLE,
            N31FailureReason.EMPTY_OR_UNREACHABLE,
        ),
        (
            "nonredundancy_established",
            False,
            N31FailureReason.NONREDUNDANCY_UNKNOWN,
        ),
        ("exact_de_bruijn_reindex", False, N31FailureReason.REINDEX_MISMATCH),
        ("endpoints_nondefeq", False, N31FailureReason.ENDPOINTS_DEFEQ),
    ],
)
def test_checker_facts_fail_closed(
    field: str,
    value: object,
    reason: N31FailureReason,
) -> None:
    facts = _all_pass_facts()
    facts[field] = value
    result = decide_n31_checker_facts(N31CheckerFacts.model_validate(facts))
    assert result.outcome == CheckerOutcome.TYPED_NOT_APPLICABLE
    assert result.failure_reason == reason


def test_target_role_values_are_not_execution_bindings() -> None:
    contract = load_n31_guard_bank().config
    assert "not execution bindings" in contract.target_head_bank_binding.resolution_rule
    assert "exact_target_head_bank_hash_bound" in contract.readiness_requirements


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload["operation_ids"].reverse(),
        lambda payload: payload["guard_shapes"].pop(),
        lambda payload: payload["frozen_implication_closure"].pop(),
        lambda payload: payload["retained_contradiction_shapes"][0].__setitem__(
            "required_same_instance", False
        ),
        lambda payload: payload["fail_closed_reasons"].remove("nonredundancy_unknown"),
        lambda payload: payload["target_head_bank_binding"].update(
            {"status": "resolved", "bank_hash": "0" * 64}
        ),
    ],
)
def test_contract_drift_fails_validation(mutate: Any) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises(ValidationError):
        N31GuardBank.model_validate(payload)


def test_duplicate_yaml_key_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yaml"
    path.write_text("policy_version: '0.3.2'\npolicy_version: '0.3.2'\n", encoding="utf-8")
    with pytest.raises(DuplicateKeyError):
        load_yaml_mapping(path)


def test_round_trip_payload_retains_exact_contract() -> None:
    payload = _payload()
    path_text = yaml.safe_dump(payload, sort_keys=False)
    reparsed = yaml.safe_load(path_text)
    assert N31GuardBank.model_validate(reparsed) == N31GuardBank.model_validate(payload)
