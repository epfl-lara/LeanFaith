"""The generic Qwen RCP proposal is versioned but cannot execute."""

from __future__ import annotations

from pathlib import Path

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import load_yaml_mapping

ROOT = Path(__file__).resolve().parents[2]
PROPOSAL = ROOT / "configs/generation/rcp_qwen_qualification_proposal_v1.yaml"
POLICY = ROOT / "policies/rcp_qwen_qualification_proposal_v1.yaml"


def test_qwen_roles_and_family_accounting_are_frozen() -> None:
    proposal = load_yaml_mapping(PROPOSAL)
    models = proposal["models"]

    assert models["primary"]["model_id"] == "Qwen/Qwen3.6-35B-A3B"
    assert models["upper_capacity_ablation"]["model_id"] == "Qwen/Qwen3.5-397B-A17B"
    assert models["excluded_default_text_route"]["model_id"] == "Qwen/Qwen3-VL-235B-A22B-Thinking"
    assert all(model["provider_family"] == "qwen3" for model in models.values())
    assert all(model["diversity_group"] == "qwen3" for model in models.values())
    assert all(model["judge_eligible"] is False for model in models.values())
    assert all(model["execution_authorized"] is False for model in models.values())
    assert proposal["family_policy"]["all_registered_qwen_models_one_family"] is True
    assert proposal["family_policy"]["current_family_credit"] == 0
    assert proposal["input_and_prompt"]["multimodal_route_enabled"] is False
    primary_decoding = proposal["model_specific_decoding"]["primary"]
    ablation_decoding = proposal["model_specific_decoding"]["upper_capacity_ablation"]
    expected_sampling = {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    }
    assert primary_decoding["proposed_sampling"] == expected_sampling
    assert ablation_decoding["proposed_sampling"] == expected_sampling
    assert (
        primary_decoding["official_source"]["repository_revision"]
        == "995ad96eacd98c81ed38be0c5b274b04031597b0"
    )
    assert (
        ablation_decoding["official_source"]["repository_revision"]
        == "8472618112abcbd45acbcdc58436aff4233c23f7"
    )
    capability = proposal["model_specific_decoding"]["rcp_capability_status"]
    assert capability["status"] == "unverified_for_exact_model_routes"
    assert capability["no_sampling_field_is_assumed_supported"] is True
    assert capability["silently_dropped_fields_forbidden"] is True


def test_qwen_proposal_has_zero_live_authority() -> None:
    proposal = load_yaml_mapping(PROPOSAL)
    policy = load_yaml_mapping(POLICY)

    assert proposal["status"] == "proposed_not_execution_authorized"
    assert proposal["execution_policy"]["provider_calls_permitted_by_this_proposal"] == 0
    assert proposal["execution_policy"]["live_catalog_requests_authorized"] is False
    assert proposal["execution_policy"]["live_generation_requests_authorized"] is False
    assert proposal["execution_policy"]["bulk_execution_available"] is False
    assert policy["authorization"]["maximum_provider_calls"] == 0
    assert policy["authorization"]["one_problem_generation_authorized"] is False
    assert policy["authorization"]["bulk_generation_authorized"] is False
    assert policy["admission"]["current_proposal_is_not_execution_authority"] is True
    assert policy["admission"]["rcp_sampling_field_support_currently_verified"] is False
    assert policy["admission"]["inherit_kimi_decoding_forbidden"] is True
    assert policy["scope"]["semantic_labels_created"] is False
    assert policy["scope"]["gate_credit_eligible"] is False
    assert hash_file(PROPOSAL)
    assert hash_file(POLICY)
