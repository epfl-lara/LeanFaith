"""Prospective remote portfolio v2 is exact, evidence-bound, and fail-disabled."""

from __future__ import annotations

import copy
import datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.generation import remote_provider_portfolio_v2 as portfolio

ROOT = Path(__file__).resolve().parents[2]
PORTFOLIO = ROOT / "configs/generation/rcp_provider_portfolio_v2.yaml"
POLICY = ROOT / "policies/rcp_remote_generation_v2.yaml"
REPORT = ROOT / "reports/generation/lf021_remote_provider_portfolio_v2_readiness.json"
VALIDATOR = ROOT / "src/leanfaith/generation/remote_provider_portfolio_v2.py"
TEST_MODULE = Path(__file__).resolve()

V1_PORTFOLIO_SHA256 = "6f6a79159ff68a3cb4bec59fd52a21e84a7f84b721622521046c40d60557c298"
V1_POLICY_SHA256 = "c924efacdf2bd28373eed255c9b04b8e45cb56d430b2f4906b253ab63d661470"
COMBINED_AUDIT_SHA256 = "e815141eab7493a90d966a1d617df6739ea0f5f6ec6b52ab24317a5e057496f0"


def _route_map() -> dict[str, portfolio.RemoteRouteV2]:
    verified = portfolio.load_and_verify_remote_portfolio_v2(repo_root=ROOT)
    return {route.route_id: route for route in verified.portfolio.config.routes}


def test_frozen_v1_bytes_remain_unchanged() -> None:
    assert (
        hash_file(ROOT / "configs/generation/rcp_provider_portfolio_v1.yaml") == V1_PORTFOLIO_SHA256
    )
    assert hash_file(ROOT / "policies/rcp_remote_generation_v1.yaml") == V1_POLICY_SHA256
    assert (
        hash_file(
            ROOT / "reports/generation/"
            "lf021_remote_one_problem_qualifications_combined_audit_v1.json"
        )
        == COMBINED_AUDIT_SHA256
    )


def test_portfolio_and_policy_replay_offline_and_fail_disabled() -> None:
    verified = portfolio.load_and_verify_remote_portfolio_v2(repo_root=ROOT)
    config = verified.portfolio.config
    policy = verified.policy.config

    assert config.portfolio_id == "lf021_remote_provider_portfolio_v2"
    assert policy.policy_id == "lf021_remote_generation_v2"
    assert config.status == "prospective_fail_disabled"
    assert policy.status == "prospective_fail_disabled_no_execution_authorization"
    assert verified.verified_artifact_count >= 14
    assert {
        route.route_id for route in config.routes if route.transport == "rcp_openai_compatible"
    } <= verified.advertised_rcp_routes

    assert not config.global_guards.route_execution_authorized
    assert not config.global_guards.additional_qualification_calls_authorized
    assert not config.global_guards.proposal_generation_authorized
    assert not config.global_guards.bulk_generation_authorized
    assert not config.global_guards.semantic_label_eligible
    assert not config.global_guards.supervision_eligible
    assert not config.global_guards.training_eligible
    assert not config.global_guards.gate_credit_eligible
    assert config.global_guards.public_sources_only
    assert config.global_guards.reference_hidden_required
    assert config.global_guards.private_sft_classic_transmission_forbidden

    assert policy.admission_boundary.authorized_route_ids == ()
    assert not policy.scope.route_execution_authorized
    assert not policy.scope.additional_qualification_calls_authorized
    assert not policy.scope.proposal_generation_authorized
    assert not policy.scope.bulk_remote_collection_authorized
    assert not policy.scope.semantic_label_use_authorized
    assert not policy.scope.supervision_use_authorized
    assert not policy.scope.training_use_authorized
    assert not policy.scope.gate_use_authorized
    assert policy.provider_calls_performed_by_policy_creation == 0


def test_exact_roles_order_and_family_accounting() -> None:
    verified = portfolio.load_and_verify_remote_portfolio_v2(repo_root=ROOT)
    config = verified.portfolio.config
    routes = {route.route_id: route for route in config.routes}

    assert config.prospective_route_order == (
        "moonshotai/Kimi-K2.7-Code",
        "Qwen/Qwen3.6-35B-A3B",
        "gpt-5.6-terra",
        "moonshotai/Kimi-K2.6",
        "Qwen/Qwen3.5-397B-A17B",
        "Qwen/Qwen3-30B-A3B-Instruct-2507",
    )
    assert routes["moonshotai/Kimi-K2.7-Code"].role == "primary_remote_generator"
    assert routes["moonshotai/Kimi-K2.6"].role == "same_family_fallback"
    assert routes["Qwen/Qwen3.6-35B-A3B"].role == "distinct_family_backup"
    assert routes["Qwen/Qwen3.5-397B-A17B"].role == "upper_capacity_ablation"
    assert routes["Qwen/Qwen3-30B-A3B-Instruct-2507"].role == "cheap_non_thinking_fallback"
    assert routes["gpt-5.6-terra"].role == "selective_high_value_proposer"

    families = {group.family_id: group for group in config.family_groups}
    assert families["moonshot_kimi_k2"].route_ids == (
        "moonshotai/Kimi-K2.7-Code",
        "moonshotai/Kimi-K2.6",
    )
    assert families["qwen3"].route_ids == (
        "Qwen/Qwen3.6-35B-A3B",
        "Qwen/Qwen3.5-397B-A17B",
        "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "Qwen/Qwen3-VL-235B-A22B-Thinking",
    )
    assert all(group.independent_family_count == 1 for group in families.values())
    assert all(group.current_gate_family_credit == 0 for group in families.values())


def test_route_specific_reasoning_contracts_are_not_blanket_defaults() -> None:
    routes = _route_map()

    kimi = routes["moonshotai/Kimi-K2.7-Code"].decoding_contract
    assert kimi is not None
    assert kimi.thinking_mode == "forced_thinking"
    assert kimi.temperature == 1.0
    assert kimi.top_p == 0.95
    assert kimi.reasoning_effort == "high"
    assert kimi.chat_template_enable_thinking is True
    assert not routes["moonshotai/Kimi-K2.7-Code"].qualification.request_contract_payload_matched

    kimi_fallback = routes["moonshotai/Kimi-K2.6"].decoding_contract
    assert kimi_fallback is not None
    assert kimi_fallback.chat_template_thinking is True
    assert kimi_fallback.chat_template_enable_thinking is None
    assert routes["moonshotai/Kimi-K2.6"].qualification.status == "catalog_only"

    qwen = routes["Qwen/Qwen3.6-35B-A3B"].decoding_contract
    assert qwen is not None
    assert qwen.temperature == 0.6
    assert qwen.top_p == 0.95
    assert qwen.top_k == 20
    assert qwen.min_p == 0.0
    assert qwen.reasoning_effort == "high"
    assert qwen.chat_template_enable_thinking is True
    assert routes["Qwen/Qwen3.6-35B-A3B"].qualification.request_contract_payload_matched
    assert not routes["Qwen/Qwen3.6-35B-A3B"].qualification.individual_field_application_proven

    non_thinking = routes["Qwen/Qwen3-30B-A3B-Instruct-2507"].decoding_contract
    assert non_thinking is not None
    assert non_thinking.thinking_mode == "non_thinking"
    assert non_thinking.temperature == 0.7
    assert non_thinking.top_p == 0.8
    assert non_thinking.top_k == 20
    assert non_thinking.min_p == 0.0
    assert non_thinking.reasoning_effort is None
    assert non_thinking.chat_template_enable_thinking is None
    assert non_thinking.chat_template_thinking is None
    assert non_thinking.thinking_fields_forbidden

    vl = routes["Qwen/Qwen3-VL-235B-A22B-Thinking"]
    assert vl.execution_status == "excluded_default_text_only"
    assert not vl.text_only_path_eligible
    assert vl.decoding_contract is None

    codex = routes["gpt-5.6-terra"]
    assert codex.transport == "codex_exec"
    assert codex.codex_contract is not None
    assert codex.codex_contract.reasoning_effort == "xhigh"
    assert codex.codex_contract.prompt_transport == "stdin"
    assert codex.codex_contract.working_directory == "isolated_empty_directory"
    assert codex.codex_contract.sandbox == "read-only"
    assert not codex.codex_contract.web_search_enabled
    assert not codex.codex_contract.inherit_environment
    assert codex.codex_contract.selective_high_value_only


def test_every_route_remains_public_reference_hidden_and_non_research() -> None:
    for route in _route_map().values():
        assert route.execution_status in {
            "disabled_pending_separately_reviewed_admission",
            "excluded_default_text_only",
        }
        assert route.public_source_only
        assert route.reference_hidden_required
        assert route.trusted_reference_transmission_forbidden
        assert route.private_source_transmission_forbidden
        assert route.route_substitution_forbidden
        assert not route.judge_eligible
        assert route.qualification.reference_hidden
        assert route.qualification.public_source_only
        assert not route.qualification.private_source_transmission_performed
        assert not route.qualification.trusted_reference_transmission_performed
        assert not route.qualification.semantic_faithfulness_assessed
        assert not route.qualification.semantic_labels_created
        assert not route.qualification.supervision_eligible
        assert not route.qualification.gate_credit_claimed


def test_schema_rejects_route_activation_and_fake_family_diversity() -> None:
    raw = load_yaml_mapping(PORTFOLIO)

    activated = copy.deepcopy(raw)
    activated["routes"][0]["execution_status"] = "enabled"
    with pytest.raises(ValidationError):
        portfolio.RemoteProviderPortfolioV2.model_validate(activated)

    fake_family = copy.deepcopy(raw)
    fake_family["family_groups"][0]["independent_family_count"] = 2
    with pytest.raises(ValidationError):
        portfolio.RemoteProviderPortfolioV2.model_validate(fake_family)

    split_qwen = copy.deepcopy(raw)
    split_qwen["routes"][3]["family_id"] = "openai_codex"
    with pytest.raises(ValidationError):
        portfolio.RemoteProviderPortfolioV2.model_validate(split_qwen)


def test_schema_rejects_thinking_fields_on_qwen_30b() -> None:
    raw = load_yaml_mapping(PORTFOLIO)
    altered = copy.deepcopy(raw)
    route = next(
        item for item in altered["routes"] if item["route_id"] == "Qwen/Qwen3-30B-A3B-Instruct-2507"
    )
    route["decoding_contract"]["reasoning_effort"] = "high"
    route["decoding_contract"]["chat_template_enable_thinking"] = True
    with pytest.raises(ValidationError, match="non-thinking route"):
        portfolio.RemoteProviderPortfolioV2.model_validate(altered)


def test_schema_rejects_claims_beyond_qualification_evidence() -> None:
    raw = load_yaml_mapping(PORTFOLIO)
    altered = copy.deepcopy(raw)
    route = next(item for item in altered["routes"] if item["route_id"] == "Qwen/Qwen3.6-35B-A3B")
    route["qualification"]["semantic_faithfulness_assessed"] = True
    with pytest.raises(ValidationError):
        portfolio.RemoteProviderPortfolioV2.model_validate(altered)

    altered = copy.deepcopy(raw)
    route = next(item for item in altered["routes"] if item["route_id"] == "moonshotai/Kimi-K2.6")
    route["qualification"]["request_contract_payload_matched"] = True
    with pytest.raises(ValidationError, match="catalog-only"):
        portfolio.RemoteProviderPortfolioV2.model_validate(altered)


def test_hash_drift_fails_closed(tmp_path: Path) -> None:
    raw = load_yaml_mapping(PORTFOLIO)
    altered = copy.deepcopy(raw)
    altered["predecessor_portfolio"]["sha256"] = "0" * 64
    temporary = tmp_path / "portfolio.yaml"
    temporary.write_text(yaml.safe_dump(altered, sort_keys=False), encoding="utf-8")
    with pytest.raises(portfolio.RemotePortfolioV2Error, match="hash differs"):
        portfolio.load_and_verify_remote_portfolio_v2(
            repo_root=ROOT,
            portfolio_path=temporary,
            policy_path=POLICY,
        )


def test_conflicting_hash_for_repeated_artifact_fails_closed() -> None:
    raw = load_yaml_mapping(PORTFOLIO)
    altered = copy.deepcopy(raw)
    altered["routes"][0]["qualification"]["evidence"][0]["sha256"] = "0" * 64
    config = portfolio.RemoteProviderPortfolioV2.model_validate(altered)

    with pytest.raises(
        portfolio.RemotePortfolioV2Error,
        match="conflicting hashes for repeated artifact",
    ):
        portfolio._iter_portfolio_bindings(config)


def test_module_exposes_no_execution_or_provider_entrypoint() -> None:
    forbidden = {
        "execute_remote_generation",
        "execute_provider_call",
        "generate_remote_candidates",
        "run_bulk_generation",
    }
    assert forbidden.isdisjoint(set(dir(portfolio)))


def test_static_readiness_audit_is_replayable_and_non_authorizing() -> None:
    report = portfolio.verify_remote_portfolio_readiness_v2(
        repo_root=ROOT,
        report_path=REPORT,
    )
    assert report.verdict == "PASS_PROSPECTIVE_FAIL_DISABLED"
    assert report.scope == "offline_schema_policy_and_evidence_integrity_only"
    assert report.provider_calls_performed == 0
    assert report.network_requests_performed == 0
    assert not report.route_execution_authorized
    assert not report.proposal_generation_authorized
    assert not report.bulk_generation_authorized
    assert not report.semantic_labels_created
    assert not report.supervision_eligible
    assert not report.training_eligible
    assert not report.gate_credit_eligible
    assert report.scientifically_admitted_routes == ()


def test_readiness_builder_is_deterministic(tmp_path: Path) -> None:
    audited_at = datetime.datetime(2026, 7, 24, 5, 35, tzinfo=datetime.UTC)
    first = portfolio.write_remote_portfolio_readiness_v2(
        repo_root=ROOT,
        output_path=tmp_path / "first.json",
        audited_at=audited_at,
        validator_path=VALIDATOR,
        test_module_path=TEST_MODULE,
    )
    second = portfolio.write_remote_portfolio_readiness_v2(
        repo_root=ROOT,
        output_path=tmp_path / "second.json",
        audited_at=audited_at,
        validator_path=VALIDATOR,
        test_module_path=TEST_MODULE,
    )
    assert first == second
    assert (tmp_path / "first.json").read_bytes() == (tmp_path / "second.json").read_bytes()
