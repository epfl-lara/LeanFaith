"""Fail-disabled prospective remote-provider portfolio for LF-021.

The v2 portfolio records reviewed route roles, family accounting, exact
decoding contracts, and already-persisted one-problem qualification evidence.
It deliberately exposes no provider client and grants no execution,
supervision, label, training, evaluation-independence, or Gate permission.

Any future remote use requires a separately versioned admission artifact and
runner.  Merely loading this module can never make a provider call.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import require_utc

_HEX64 = r"^[0-9a-f]{64}$"
_PORTFOLIO_ID = "lf021_remote_provider_portfolio_v2"
_POLICY_ID = "lf021_remote_generation_v2"
_READINESS_ID = r"^lf021_remote_provider_portfolio_v2_readiness:[0-9a-f]{64}$"

_PORTFOLIO_PATH = "configs/generation/rcp_provider_portfolio_v2.yaml"
_POLICY_PATH = "policies/rcp_remote_generation_v2.yaml"
_V1_PORTFOLIO = "configs/generation/rcp_provider_portfolio_v1.yaml"
_V1_POLICY = "policies/rcp_remote_generation_v1.yaml"
_COMBINED_AUDIT = (
    "reports/generation/lf021_remote_one_problem_qualifications_combined_audit_v1.json"
)

_ROUTE_ORDER = (
    "moonshotai/Kimi-K2.7-Code",
    "Qwen/Qwen3.6-35B-A3B",
    "gpt-5.6-terra",
    "moonshotai/Kimi-K2.6",
    "Qwen/Qwen3.5-397B-A17B",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
)
_ALL_ROUTES = (
    "moonshotai/Kimi-K2.7-Code",
    "moonshotai/Kimi-K2.6",
    "Qwen/Qwen3.6-35B-A3B",
    "Qwen/Qwen3.5-397B-A17B",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
    "Qwen/Qwen3-VL-235B-A22B-Thinking",
    "gpt-5.6-terra",
)
_RCP_ROUTES = frozenset(_ALL_ROUTES[:-1])
_FAMILY_ROUTES = {
    "moonshot_kimi_k2": (
        "moonshotai/Kimi-K2.7-Code",
        "moonshotai/Kimi-K2.6",
    ),
    "qwen3": (
        "Qwen/Qwen3.6-35B-A3B",
        "Qwen/Qwen3.5-397B-A17B",
        "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "Qwen/Qwen3-VL-235B-A22B-Thinking",
    ),
    "openai_codex": ("gpt-5.6-terra",),
}
_ROLE_BY_ROUTE = {
    "moonshotai/Kimi-K2.7-Code": "primary_remote_generator",
    "moonshotai/Kimi-K2.6": "same_family_fallback",
    "Qwen/Qwen3.6-35B-A3B": "distinct_family_backup",
    "Qwen/Qwen3.5-397B-A17B": "upper_capacity_ablation",
    "Qwen/Qwen3-30B-A3B-Instruct-2507": "cheap_non_thinking_fallback",
    "Qwen/Qwen3-VL-235B-A22B-Thinking": "excluded_multimodal_route",
    "gpt-5.6-terra": "selective_high_value_proposer",
}


class RemotePortfolioV2Error(RuntimeError):
    """The prospective portfolio or one of its evidence bindings failed closed."""


class ArtifactBindingV2(StrictModel):
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _safe_relative_path(self) -> Self:
        path = PurePosixPath(self.artifact)
        if path.is_absolute() or ".." in path.parts or "\\" in self.artifact:
            raise ValueError("artifact binding must be a safe repository-relative POSIX path")
        return self


class GlobalGuardsV2(StrictModel):
    route_execution_authorized: Literal[False] = False
    additional_qualification_calls_authorized: Literal[False] = False
    proposal_generation_authorized: Literal[False] = False
    bulk_generation_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    semantic_label_eligible: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    gate_credit_eligible: Literal[False] = False
    heldout_claim_eligible: Literal[False] = False
    unseen_claim_eligible: Literal[False] = False
    evaluation_independence_claim_eligible: Literal[False] = False
    public_sources_only: Literal[True] = True
    reference_hidden_required: Literal[True] = True
    trusted_lean_reference_transmission_forbidden: Literal[True] = True
    private_sft_classic_transmission_forbidden: Literal[True] = True
    alternate_precision_route_substitution_forbidden: Literal[True] = True
    separately_versioned_admission_required: Literal[True] = True


class FamilyGroupV2(StrictModel):
    family_id: Literal["moonshot_kimi_k2", "qwen3", "openai_codex"]
    route_ids: tuple[str, ...]
    independent_family_count: Literal[1] = 1
    current_gate_family_credit: Literal[0] = 0


class RCPDecodingContractV2(StrictModel):
    contract_id: Literal[
        "kimi_k2_7_forced_thinking_v2",
        "kimi_k2_6_thinking_v2",
        "qwen3_6_thinking_code_v2",
        "qwen3_5_thinking_code_v2",
        "qwen3_30b_non_thinking_v2",
    ]
    max_completion_tokens: Literal[4096] = 4096
    temperature: float = Field(ge=0.0, le=2.0)
    top_p: float = Field(gt=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1)
    min_p: float | None = Field(default=None, ge=0.0, le=1.0)
    presence_penalty: float | None = None
    repetition_penalty: float | None = Field(default=None, gt=0.0)
    thinking_mode: Literal["forced_thinking", "enabled", "non_thinking"]
    reasoning_effort: Literal["high"] | None
    chat_template_enable_thinking: bool | None
    chat_template_thinking: bool | None
    thinking_fields_forbidden: bool

    @model_validator(mode="after")
    def _thinking_fields_are_coherent(self) -> Self:
        if self.thinking_mode == "non_thinking":
            if (
                self.reasoning_effort is not None
                or self.chat_template_enable_thinking is not None
                or self.chat_template_thinking is not None
                or not self.thinking_fields_forbidden
            ):
                raise ValueError("non-thinking route must omit every thinking/reasoning field")
        elif (
            self.reasoning_effort != "high"
            or self.thinking_fields_forbidden
            or (
                self.chat_template_enable_thinking is not True
                and self.chat_template_thinking is not True
            )
        ):
            raise ValueError(
                "thinking route must bind high reasoning and one exact thinking switch"
            )
        return self


class CodexExecutionContractV2(StrictModel):
    contract_id: Literal["codex_exec_public_isolated_xhigh_v2"]
    reasoning_effort: Literal["xhigh"]
    prompt_transport: Literal["stdin"]
    working_directory: Literal["isolated_empty_directory"]
    sandbox: Literal["read-only"]
    web_search_enabled: Literal[False] = False
    inherit_environment: Literal[False] = False
    strict_output_schema: Literal[True] = True
    selective_high_value_only: Literal[True] = True


class QualificationEvidenceV2(StrictModel):
    status: Literal[
        "catalog_only",
        "excluded_catalog_only",
        "one_problem_operational_only",
        "one_problem_payload_accepted_application_unproven",
        "one_problem_codex_operational_only",
    ]
    request_contract_payload_matched: bool
    individual_field_application_proven: Literal[False] = False
    reference_hidden: Literal[True] = True
    public_source_only: Literal[True] = True
    private_source_transmission_performed: Literal[False] = False
    trusted_reference_transmission_performed: Literal[False] = False
    semantic_faithfulness_assessed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    evidence: tuple[ArtifactBindingV2, ...]

    @model_validator(mode="after")
    def _evidence_matches_status(self) -> Self:
        catalog_statuses = {"catalog_only", "excluded_catalog_only"}
        if self.status in catalog_statuses:
            if self.evidence or self.request_contract_payload_matched:
                raise ValueError("catalog-only route cannot claim request evidence")
        elif len(self.evidence) < 4:
            raise ValueError("operational qualification must bind audit, config, and run evidence")
        return self


class RemoteRouteV2(StrictModel):
    route_id: str = Field(min_length=1)
    family_id: Literal["moonshot_kimi_k2", "qwen3", "openai_codex"]
    transport: Literal["rcp_openai_compatible", "codex_exec"]
    role: Literal[
        "primary_remote_generator",
        "same_family_fallback",
        "distinct_family_backup",
        "upper_capacity_ablation",
        "cheap_non_thinking_fallback",
        "excluded_multimodal_route",
        "selective_high_value_proposer",
    ]
    execution_status: Literal[
        "disabled_pending_separately_reviewed_admission",
        "excluded_default_text_only",
    ]
    purpose_if_later_admitted: Literal[
        "routine_public_reference_hidden_proposals",
        "moonshot_fallback_and_within_family_ablation",
        "preferred_qwen_public_reference_hidden_proposals",
        "qwen_capacity_ablation_only",
        "lower_cost_qwen_non_thinking_fallback",
        "separately_scoped_multimodal_experiment_only",
        "high_value_public_reference_hidden_proposals_only",
    ]
    text_only_path_eligible: bool
    judge_eligible: Literal[False] = False
    public_source_only: Literal[True] = True
    reference_hidden_required: Literal[True] = True
    trusted_reference_transmission_forbidden: Literal[True] = True
    private_source_transmission_forbidden: Literal[True] = True
    route_substitution_forbidden: Literal[True] = True
    decoding_contract: RCPDecodingContractV2 | None
    codex_contract: CodexExecutionContractV2 | None
    qualification: QualificationEvidenceV2

    @model_validator(mode="after")
    def _transport_contract_is_exact(self) -> Self:
        if self.transport == "rcp_openai_compatible":
            if self.codex_contract is not None:
                raise ValueError("RCP route cannot carry a Codex contract")
            if self.execution_status == "excluded_default_text_only":
                if self.decoding_contract is not None or self.text_only_path_eligible:
                    raise ValueError("excluded multimodal route cannot enter text-only execution")
            elif self.decoding_contract is None or not self.text_only_path_eligible:
                raise ValueError("prospective RCP text route needs one decoding contract")
        elif (
            self.decoding_contract is not None
            or self.codex_contract is None
            or not self.text_only_path_eligible
        ):
            raise ValueError("Codex route must carry only its isolated execution contract")
        return self


class ContaminationPolicyV2(StrictModel):
    checkpoint_revision_status: Literal["unavailable_from_route_ids"]
    training_cutoff_status: Literal["unknown"]
    contamination_status: Literal["unknown"]
    unseen_claim_eligible: Literal[False] = False
    heldout_claim_eligible: Literal[False] = False
    evaluation_independence_claim_eligible: Literal[False] = False


class RemoteProviderPortfolioV2(StrictModel):
    schema_version: Literal[2] = 2
    portfolio_id: Literal["lf021_remote_provider_portfolio_v2"]
    frozen_at: datetime.datetime
    status: Literal["prospective_fail_disabled"]
    predecessor_portfolio: ArtifactBindingV2
    predecessor_policy: ArtifactBindingV2
    qualification_audit: ArtifactBindingV2
    catalog_evidence: ArtifactBindingV2
    global_guards: GlobalGuardsV2
    family_groups: tuple[FamilyGroupV2, FamilyGroupV2, FamilyGroupV2]
    prospective_route_order: tuple[str, str, str, str, str, str]
    routes: tuple[
        RemoteRouteV2,
        RemoteRouteV2,
        RemoteRouteV2,
        RemoteRouteV2,
        RemoteRouteV2,
        RemoteRouteV2,
        RemoteRouteV2,
    ]
    contamination_policy: ContaminationPolicyV2

    @model_validator(mode="after")
    def _portfolio_is_exact_and_disabled(self) -> Self:
        require_utc(self.frozen_at)
        if self.predecessor_portfolio.artifact != _V1_PORTFOLIO:
            raise ValueError("v2 portfolio must bind the frozen v1 portfolio")
        if self.predecessor_policy.artifact != _V1_POLICY:
            raise ValueError("v2 portfolio must bind the frozen v1 policy")
        if self.qualification_audit.artifact != _COMBINED_AUDIT:
            raise ValueError("v2 portfolio must bind the combined qualification audit")
        if self.prospective_route_order != _ROUTE_ORDER:
            raise ValueError("prospective route ordering differs from reviewed order")
        route_ids = tuple(route.route_id for route in self.routes)
        if route_ids != _ALL_ROUTES or len(set(route_ids)) != len(route_ids):
            raise ValueError("route inventory or order differs from the reviewed portfolio")

        by_route = {route.route_id: route for route in self.routes}
        for route_id, role in _ROLE_BY_ROUTE.items():
            if by_route[route_id].role != role:
                raise ValueError(f"role differs for {route_id}")
        for group, (family_id, expected_routes) in zip(
            self.family_groups,
            _FAMILY_ROUTES.items(),
            strict=True,
        ):
            if group.family_id != family_id or group.route_ids != expected_routes:
                raise ValueError(f"family accounting differs for {family_id}")
            if any(by_route[route_id].family_id != family_id for route_id in expected_routes):
                raise ValueError(f"route family differs for {family_id}")

        _require_exact_route_contracts(by_route)
        return self


class RemoteGenerationScopeV2(StrictModel):
    public_sources_only_if_later_admitted: Literal[True] = True
    reference_hidden_if_later_admitted: Literal[True] = True
    private_sft_classic_transmission_forbidden: Literal[True] = True
    trusted_lean_reference_transmission_forbidden: Literal[True] = True
    route_execution_authorized: Literal[False] = False
    additional_qualification_calls_authorized: Literal[False] = False
    proposal_generation_authorized: Literal[False] = False
    bulk_remote_collection_authorized: Literal[False] = False
    semantic_label_use_authorized: Literal[False] = False
    supervision_use_authorized: Literal[False] = False
    training_use_authorized: Literal[False] = False
    gate_use_authorized: Literal[False] = False
    heldout_or_unseen_claim_authorized: Literal[False] = False


class FutureRolePolicyV2(StrictModel):
    primary: Literal["moonshotai/Kimi-K2.7-Code"]
    same_family_fallback: Literal["moonshotai/Kimi-K2.6"]
    distinct_family_backup: Literal["Qwen/Qwen3.6-35B-A3B"]
    upper_capacity_ablation: Literal["Qwen/Qwen3.5-397B-A17B"]
    cheap_non_thinking_fallback: Literal["Qwen/Qwen3-30B-A3B-Instruct-2507"]
    excluded_default_text_only: Literal["Qwen/Qwen3-VL-235B-A22B-Thinking"]
    selective_high_value_public_proposer: Literal["gpt-5.6-terra"]
    clean_heldout_judge_family: Literal["intentionally_unassigned"]


class AdmissionBoundaryV2(StrictModel):
    authorized_route_ids: tuple[()] = ()
    required_future_artifact: Literal[
        "separately_versioned_route_admission_policy_and_execution_manifest"
    ]
    exact_route_and_decoding_contract_must_be_requalified: Literal[True] = True
    human_quality_review_required_before_model_ranking: Literal[True] = True
    no_v1_or_v2_qualification_counts_as_semantic_evidence: Literal[True] = True
    no_silent_route_or_precision_substitution: Literal[True] = True


class RemoteGenerationPolicyV2(StrictModel):
    schema_version: Literal[2] = 2
    policy_id: Literal["lf021_remote_generation_v2"]
    frozen_at: datetime.datetime
    status: Literal["prospective_fail_disabled_no_execution_authorization"]
    portfolio: ArtifactBindingV2
    predecessor_policy: ArtifactBindingV2
    scope: RemoteGenerationScopeV2
    future_role_policy: FutureRolePolicyV2
    family_accounting: tuple[FamilyGroupV2, FamilyGroupV2, FamilyGroupV2]
    admission_boundary: AdmissionBoundaryV2
    provider_calls_performed_by_policy_creation: Literal[0] = 0
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_eligible: Literal[False] = False
    gate_closed: Literal[False] = False

    @model_validator(mode="after")
    def _policy_is_exact_and_disabled(self) -> Self:
        require_utc(self.frozen_at)
        if self.portfolio.artifact != _PORTFOLIO_PATH:
            raise ValueError("v2 policy must bind the v2 portfolio")
        if self.predecessor_policy.artifact != _V1_POLICY:
            raise ValueError("v2 policy must bind the frozen v1 policy")
        for group, (family_id, expected_routes) in zip(
            self.family_accounting,
            _FAMILY_ROUTES.items(),
            strict=True,
        ):
            if group.family_id != family_id or group.route_ids != expected_routes:
                raise ValueError(f"policy family accounting differs for {family_id}")
        return self


class ReadinessChecksV2(StrictModel):
    frozen_v1_bytes_match: Literal[True] = True
    v2_portfolio_and_policy_validate: Literal[True] = True
    exact_route_inventory_matches: Literal[True] = True
    exact_family_accounting_matches: Literal[True] = True
    exact_route_contracts_match: Literal[True] = True
    qwen_30b_thinking_fields_absent: Literal[True] = True
    qwen_vl_default_text_path_excluded: Literal[True] = True
    qualification_evidence_hashes_match: Literal[True] = True
    rcp_catalog_contains_every_rcp_route: Literal[True] = True
    public_reference_hidden_guards_enforced: Literal[True] = True
    private_source_transmission_forbidden: Literal[True] = True
    all_execution_and_research_use_disabled: Literal[True] = True
    no_provider_client_or_runner_exposed: Literal[True] = True


class RemotePortfolioReadinessContentV2(StrictModel):
    schema_version: Literal[2] = 2
    report_kind: Literal["lf021_remote_provider_portfolio_v2_readiness"]
    audited_at: datetime.datetime
    verdict: Literal["PASS_PROSPECTIVE_FAIL_DISABLED"]
    scope: Literal["offline_schema_policy_and_evidence_integrity_only"]
    portfolio: ArtifactBindingV2
    policy: ArtifactBindingV2
    validator: ArtifactBindingV2
    test_module: ArtifactBindingV2
    predecessor_portfolio: ArtifactBindingV2
    predecessor_policy: ArtifactBindingV2
    qualification_audit: ArtifactBindingV2
    checks: ReadinessChecksV2
    provider_calls_performed: Literal[0] = 0
    network_requests_performed: Literal[0] = 0
    route_execution_authorized: Literal[False] = False
    proposal_generation_authorized: Literal[False] = False
    bulk_generation_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    gate_credit_eligible: Literal[False] = False
    scientifically_admitted_routes: tuple[()] = ()
    next_required_artifact: Literal[
        "separately_versioned_route_admission_policy_and_execution_manifest"
    ]

    @model_validator(mode="after")
    def _time_is_valid(self) -> Self:
        require_utc(self.audited_at)
        return self


class RemotePortfolioReadinessV2(RemotePortfolioReadinessContentV2):
    report_id: str = Field(pattern=_READINESS_ID)

    @model_validator(mode="after")
    def _content_id_is_valid(self) -> Self:
        expected = "lf021_remote_provider_portfolio_v2_readiness:" + hash_canonical(
            {
                "schema": "lf021_remote_provider_portfolio_v2_readiness",
                **self.model_dump(mode="json", exclude={"report_id"}),
            }
        )
        if self.report_id != expected:
            raise ValueError("readiness report ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedRemotePortfolioV2:
    portfolio: LoadedConfig[RemoteProviderPortfolioV2]
    policy: LoadedConfig[RemoteGenerationPolicyV2]
    verified_artifact_count: int
    advertised_rcp_routes: frozenset[str]


def _require_exact_route_contracts(by_route: dict[str, RemoteRouteV2]) -> None:
    expected_rcp: dict[str, dict[str, object]] = {
        "moonshotai/Kimi-K2.7-Code": {
            "contract_id": "kimi_k2_7_forced_thinking_v2",
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": None,
            "min_p": None,
            "thinking_mode": "forced_thinking",
            "reasoning_effort": "high",
            "chat_template_enable_thinking": True,
            "chat_template_thinking": None,
            "thinking_fields_forbidden": False,
        },
        "moonshotai/Kimi-K2.6": {
            "contract_id": "kimi_k2_6_thinking_v2",
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": None,
            "min_p": None,
            "thinking_mode": "enabled",
            "reasoning_effort": "high",
            "chat_template_enable_thinking": None,
            "chat_template_thinking": True,
            "thinking_fields_forbidden": False,
        },
        "Qwen/Qwen3.6-35B-A3B": {
            "contract_id": "qwen3_6_thinking_code_v2",
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "thinking_mode": "enabled",
            "reasoning_effort": "high",
            "chat_template_enable_thinking": True,
            "chat_template_thinking": None,
            "thinking_fields_forbidden": False,
        },
        "Qwen/Qwen3.5-397B-A17B": {
            "contract_id": "qwen3_5_thinking_code_v2",
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "thinking_mode": "enabled",
            "reasoning_effort": "high",
            "chat_template_enable_thinking": True,
            "chat_template_thinking": None,
            "thinking_fields_forbidden": False,
        },
        "Qwen/Qwen3-30B-A3B-Instruct-2507": {
            "contract_id": "qwen3_30b_non_thinking_v2",
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0.0,
            "thinking_mode": "non_thinking",
            "reasoning_effort": None,
            "chat_template_enable_thinking": None,
            "chat_template_thinking": None,
            "thinking_fields_forbidden": True,
        },
    }
    for route_id, expected in expected_rcp.items():
        contract = by_route[route_id].decoding_contract
        if contract is None:
            raise ValueError(f"missing decoding contract for {route_id}")
        observed = contract.model_dump(mode="json")
        for field_name, value in expected.items():
            if observed[field_name] != value:
                raise ValueError(f"{route_id} contract differs at {field_name}")

    vl = by_route["Qwen/Qwen3-VL-235B-A22B-Thinking"]
    if (
        vl.execution_status != "excluded_default_text_only"
        or vl.decoding_contract is not None
        or vl.text_only_path_eligible
    ):
        raise ValueError("Qwen VL route must stay excluded from the text-only path")

    codex = by_route["gpt-5.6-terra"]
    if (
        codex.transport != "codex_exec"
        or codex.codex_contract is None
        or not codex.codex_contract.selective_high_value_only
    ):
        raise ValueError("Codex route must stay selective and isolated")


def _resolve_and_verify(repo_root: Path, binding: ArtifactBindingV2) -> Path:
    root = repo_root.resolve()
    candidate = (root / binding.artifact).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RemotePortfolioV2Error(
            f"bound artifact escapes repository: {binding.artifact}"
        ) from exc
    if not candidate.is_file():
        raise RemotePortfolioV2Error(f"bound artifact is not a file: {binding.artifact}")
    observed = hash_file(candidate)
    if observed != binding.sha256:
        raise RemotePortfolioV2Error(
            f"bound artifact hash differs for {binding.artifact}: "
            f"expected {binding.sha256}, observed {observed}"
        )
    return candidate


def _iter_portfolio_bindings(
    portfolio: RemoteProviderPortfolioV2,
) -> tuple[ArtifactBindingV2, ...]:
    bindings = [
        portfolio.predecessor_portfolio,
        portfolio.predecessor_policy,
        portfolio.qualification_audit,
        portfolio.catalog_evidence,
    ]
    for route in portfolio.routes:
        bindings.extend(route.qualification.evidence)
    unique: dict[str, ArtifactBindingV2] = {}
    for binding in bindings:
        existing = unique.get(binding.artifact)
        if existing is not None and existing.sha256 != binding.sha256:
            raise RemotePortfolioV2Error(
                f"conflicting hashes for repeated artifact {binding.artifact}"
            )
        unique.setdefault(binding.artifact, binding)
    return tuple(unique[path] for path in sorted(unique))


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise RemotePortfolioV2Error(f"cannot parse bound JSON artifact {path}") from exc
    if not isinstance(value, dict):
        raise RemotePortfolioV2Error(f"bound JSON artifact is not an object: {path}")
    return cast(dict[str, Any], value)


def _verify_combined_audit(
    *,
    audit_path: Path,
    portfolio: RemoteProviderPortfolioV2,
) -> None:
    audit = _load_json_object(audit_path)
    if audit.get("verdict") != "PASS":
        raise RemotePortfolioV2Error("combined remote qualification audit is not PASS")
    qualifications = audit.get("qualifications")
    if not isinstance(qualifications, dict):
        raise RemotePortfolioV2Error("combined audit qualifications are absent")
    expected_models = {
        "kimi_k2_7": "moonshotai/Kimi-K2.7-Code",
        "qwen3_6": "Qwen/Qwen3.6-35B-A3B",
        "codex_gpt_5_6_terra": "gpt-5.6-terra",
    }
    for key, model_id in expected_models.items():
        value = qualifications.get(key)
        if not isinstance(value, dict) or value.get("model_id") != model_id:
            raise RemotePortfolioV2Error(f"combined audit route evidence differs for {key}")
        forbidden_true = (
            "semantic_labels_created",
            "semantic_faithfulness_assessed",
            "supervision_eligible",
            "gate_credit_claimed",
        )
        if any(value.get(name) is not False for name in forbidden_true):
            raise RemotePortfolioV2Error(f"combined audit overclaims research use for {key}")

    research = audit.get("research_use_constraints")
    if not isinstance(research, dict):
        raise RemotePortfolioV2Error("combined audit research constraints are absent")
    if (
        research.get("semantic_labels_created") is not False
        or research.get("supervision_eligible") is not False
        or research.get("gate_credit_claimed") is not False
    ):
        raise RemotePortfolioV2Error("combined audit permits forbidden research use")

    by_route = {route.route_id: route for route in portfolio.routes}
    if by_route["moonshotai/Kimi-K2.7-Code"].qualification.request_contract_payload_matched:
        raise RemotePortfolioV2Error("Kimi v1 qualification did not match the v2 sampling contract")
    if not by_route["Qwen/Qwen3.6-35B-A3B"].qualification.request_contract_payload_matched:
        raise RemotePortfolioV2Error("Qwen3.6 v2 contract lost its payload-match evidence")


def _verify_catalog(
    *,
    catalog_path: Path,
    portfolio: RemoteProviderPortfolioV2,
) -> frozenset[str]:
    catalog = _load_json_object(catalog_path)
    values = catalog.get("data")
    if not isinstance(values, list):
        raise RemotePortfolioV2Error("bound RCP catalog has no model list")
    route_ids = frozenset(
        str(item["id"])
        for item in values
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    expected = {
        route.route_id for route in portfolio.routes if route.transport == "rcp_openai_compatible"
    }
    missing = expected - route_ids
    if missing:
        raise RemotePortfolioV2Error(f"bound RCP catalog lacks routes: {sorted(missing)}")
    return route_ids


def load_and_verify_remote_portfolio_v2(
    *,
    repo_root: Path,
    portfolio_path: Path | None = None,
    policy_path: Path | None = None,
) -> VerifiedRemotePortfolioV2:
    """Strictly validate the prospective portfolio and every bound artifact."""

    root = repo_root.resolve()
    portfolio_file = root / _PORTFOLIO_PATH if portfolio_path is None else portfolio_path.resolve()
    policy_file = root / _POLICY_PATH if policy_path is None else policy_path.resolve()
    loaded_portfolio = load_config(portfolio_file, RemoteProviderPortfolioV2)
    loaded_policy = load_config(policy_file, RemoteGenerationPolicyV2)

    bindings = _iter_portfolio_bindings(loaded_portfolio.config)
    for binding in bindings:
        _resolve_and_verify(root, binding)
    _resolve_and_verify(root, loaded_policy.config.portfolio)
    _resolve_and_verify(root, loaded_policy.config.predecessor_policy)

    if loaded_policy.config.portfolio.sha256 != hash_file(portfolio_file):
        raise RemotePortfolioV2Error("v2 policy does not bind the loaded v2 portfolio bytes")
    if loaded_portfolio.config.predecessor_policy != loaded_policy.config.predecessor_policy:
        raise RemotePortfolioV2Error("v2 portfolio and policy bind different v1 policies")
    if loaded_policy.config.family_accounting != loaded_portfolio.config.family_groups:
        raise RemotePortfolioV2Error("portfolio and policy family accounting differ")

    audit_path = _resolve_and_verify(root, loaded_portfolio.config.qualification_audit)
    _verify_combined_audit(audit_path=audit_path, portfolio=loaded_portfolio.config)
    catalog_path = _resolve_and_verify(root, loaded_portfolio.config.catalog_evidence)
    advertised = _verify_catalog(
        catalog_path=catalog_path,
        portfolio=loaded_portfolio.config,
    )
    return VerifiedRemotePortfolioV2(
        portfolio=loaded_portfolio,
        policy=loaded_policy,
        verified_artifact_count=len(bindings) + 1,
        advertised_rcp_routes=advertised,
    )


def build_remote_portfolio_readiness_v2(
    *,
    repo_root: Path,
    audited_at: datetime.datetime,
    validator_path: Path,
    test_module_path: Path,
    portfolio_path: Path | None = None,
    policy_path: Path | None = None,
) -> RemotePortfolioReadinessV2:
    """Build an offline readiness record after strict validation.

    This function performs filesystem reads and hashing only.  It contains no
    network or provider adapter.
    """

    root = repo_root.resolve()
    verified = load_and_verify_remote_portfolio_v2(
        repo_root=root,
        portfolio_path=portfolio_path,
        policy_path=policy_path,
    )
    portfolio_file = verified.portfolio.path.resolve()
    policy_file = verified.policy.path.resolve()

    def binding(path: Path) -> ArtifactBindingV2:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root).as_posix()
        return ArtifactBindingV2(artifact=relative, sha256=hash_file(resolved))

    payload: dict[str, object] = {
        "schema_version": 2,
        "report_kind": "lf021_remote_provider_portfolio_v2_readiness",
        "audited_at": audited_at,
        "verdict": "PASS_PROSPECTIVE_FAIL_DISABLED",
        "scope": "offline_schema_policy_and_evidence_integrity_only",
        "portfolio": binding(portfolio_file),
        "policy": binding(policy_file),
        "validator": binding(validator_path),
        "test_module": binding(test_module_path),
        "predecessor_portfolio": verified.portfolio.config.predecessor_portfolio,
        "predecessor_policy": verified.portfolio.config.predecessor_policy,
        "qualification_audit": verified.portfolio.config.qualification_audit,
        "checks": ReadinessChecksV2(),
        "provider_calls_performed": 0,
        "network_requests_performed": 0,
        "route_execution_authorized": False,
        "proposal_generation_authorized": False,
        "bulk_generation_authorized": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "gate_credit_eligible": False,
        "scientifically_admitted_routes": (),
        "next_required_artifact": (
            "separately_versioned_route_admission_policy_and_execution_manifest"
        ),
    }
    content = RemotePortfolioReadinessContentV2.model_validate(payload)
    report_id = "lf021_remote_provider_portfolio_v2_readiness:" + hash_canonical(
        {
            "schema": "lf021_remote_provider_portfolio_v2_readiness",
            **content.model_dump(mode="json"),
        }
    )
    return RemotePortfolioReadinessV2.model_validate({"report_id": report_id, **payload})


def write_remote_portfolio_readiness_v2(
    *,
    repo_root: Path,
    output_path: Path,
    audited_at: datetime.datetime,
    validator_path: Path,
    test_module_path: Path,
    portfolio_path: Path | None = None,
    policy_path: Path | None = None,
) -> RemotePortfolioReadinessV2:
    report = build_remote_portfolio_readiness_v2(
        repo_root=repo_root,
        audited_at=audited_at,
        validator_path=validator_path,
        test_module_path=test_module_path,
        portfolio_path=portfolio_path,
        policy_path=policy_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(canonical_json_bytes(report.model_dump(mode="json")) + b"\n")
    return report


def verify_remote_portfolio_readiness_v2(
    *,
    repo_root: Path,
    report_path: Path,
) -> RemotePortfolioReadinessV2:
    try:
        report = RemotePortfolioReadinessV2.model_validate_json(report_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RemotePortfolioV2Error(f"cannot validate readiness report {report_path}") from exc
    for binding in (
        report.portfolio,
        report.policy,
        report.validator,
        report.test_module,
        report.predecessor_portfolio,
        report.predecessor_policy,
        report.qualification_audit,
    ):
        _resolve_and_verify(repo_root, binding)
    load_and_verify_remote_portfolio_v2(
        repo_root=repo_root,
        portfolio_path=(repo_root / report.portfolio.artifact),
        policy_path=(repo_root / report.policy.artifact),
    )
    return report


__all__ = [
    "ArtifactBindingV2",
    "CodexExecutionContractV2",
    "GlobalGuardsV2",
    "QualificationEvidenceV2",
    "RCPDecodingContractV2",
    "RemoteGenerationPolicyV2",
    "RemotePortfolioReadinessV2",
    "RemotePortfolioV2Error",
    "RemoteProviderPortfolioV2",
    "RemoteRouteV2",
    "VerifiedRemotePortfolioV2",
    "build_remote_portfolio_readiness_v2",
    "load_and_verify_remote_portfolio_v2",
    "verify_remote_portfolio_readiness_v2",
    "write_remote_portfolio_readiness_v2",
]
