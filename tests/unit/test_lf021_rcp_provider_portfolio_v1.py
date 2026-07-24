"""Prospective RCP provider roles remain supplemental and fail closed."""

from __future__ import annotations

from pathlib import Path

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import load_yaml_mapping

ROOT = Path(__file__).resolve().parents[2]
PORTFOLIO = ROOT / "configs/generation/rcp_provider_portfolio_v1.yaml"
POLICY = ROOT / "policies/rcp_remote_generation_v1.yaml"


def test_portfolio_freezes_roles_without_creating_fake_family_diversity() -> None:
    document = load_yaml_mapping(PORTFOLIO)
    models = {
        str(item["model_id"]): item
        for item in document["models"]  # type: ignore[index]
    }

    assert models["moonshotai/Kimi-K2.7-Code"]["role"] == "primary_remote_generator"
    assert models["moonshotai/Kimi-K2.6"]["role"] == "moonshot_fallback_and_ablation"
    assert models["Qwen/Qwen3.6-35B-A3B"]["role"] == "preferred_distinct_family_backup_generator"
    assert models["Qwen/Qwen3.5-397B-A17B"]["role"] == "upper_capacity_generator_ablation"
    assert models["Qwen/Qwen3-30B-A3B-Instruct-2507"]["role"] == "cheap_qwen_generator_fallback"
    assert models["Qwen/Qwen3-VL-235B-A22B-Thinking"]["execution_status"] == "excluded"
    assert all(item["judge_eligible"] is False for item in models.values())
    assert all(item["current_gate_family_credit"] is False for item in models.values())
    assert (
        models["moonshotai/Kimi-K2.7-Code"]["diversity_group"]
        == models["moonshotai/Kimi-K2.6"]["diversity_group"]
    )
    assert document["reasoning"] == {  # type: ignore[index]
        "chat_template_kwargs": {"enable_thinking": True},
        "reasoning_effort": "high",
    }
    assert (
        models["Qwen/Qwen3-VL-235B-A22B-Thinking"]["diversity_group"]
        == models["Qwen/Qwen3.6-35B-A3B"]["diversity_group"]
    )
    assert document["contamination_policy"]["contamination_status"] == "unknown"  # type: ignore[index]
    assert document["contamination_policy"]["unseen_claim_eligible"] is False  # type: ignore[index]


def test_policy_keeps_remote_results_out_of_labels_gates_and_v1_stopping() -> None:
    policy = load_yaml_mapping(POLICY)

    assert policy["scope"]["bulk_remote_collection_authorized"] is False
    assert policy["scope"]["qwen_execution_authorized"] is False
    assert policy["research_status"]["semantic_labels_created"] is False
    assert policy["research_status"]["supervision_eligible"] is False
    assert policy["research_status"]["gate_credit_eligible"] is False
    assert policy["research_status"]["remote_outputs_modify_v1_expansion_stopping"] is False
    assert policy["diversity_policy"]["current_remote_gate_family_credit"] == 0
    assert policy["research_status"]["contamination_status"] == "unknown"
    assert policy["research_status"]["heldout_claim_eligible"] is False
    assert hash_file(PORTFOLIO)
    assert hash_file(POLICY)
