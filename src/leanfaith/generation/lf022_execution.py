"""Typed admission for public, proposer-only LF-022 execution.

The allocation plan remains non-executable.  This module adds a separate,
content-addressed binding for one exact public ``G_open`` collection route.
Loading or validating an admission performs no network I/O.  Live execution
still requires an explicit caller flag at the executor boundary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.code_bundle import validate_code_bundle
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import DenylistIndex, FrozenRegistry
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022BenchmarkRegistryManifest,
    LF022DenylistClearanceRecord,
    LF022JSONLArtifactBinding,
    LF022ProductionFamilyMatrix,
    LF022ProductionPlanManifest,
    LF022ProductionSourceRecord,
    LF022ProductionTask,
    LF022ProviderCatalogSnapshot,
    LF022PublicSourceAuthorization,
    LF022PublicSourceAuthorizationRegistry,
)
from leanfaith.generation.lf022_public_pool import LF022PublicPoolAudit
from leanfaith.generation.llm_variants import (
    PROPOSER_TEMPLATE_ID,
    PROPOSER_TEMPLATE_VERSION,
    PROPOSER_TEMPLATE_VERSION_V2,
    PublicLeanVariantSource,
    VariantPromptRequest,
)
from leanfaith.schemas.enums import IntendedRelation, ViewStatus
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

_PRIVATE_SOURCE_MARKERS = ("formalmathatepfl/sft_classic", "sft_classic")
_CATALOG_REVISION_PREFIX = "rcp-catalog-sha256:"
LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT = "data/lf022_execution"
LF022_REVIEWED_PROPOSER_PROMPT_PATH = "prompts/proposers/lean_variant_v1.txt"
LF022_REVIEWED_PROPOSER_PROMPT_SHA256 = (
    "0f7b74aab06659e745980879cf9a13cdbcdd29927c1ddbb7ca47c6840e541f36"
)
LF022_REVIEWED_PROPOSER_PROMPT_V2_PATH = "prompts/proposers/lean_variant_v2.txt"
LF022_REVIEWED_PROPOSER_PROMPT_V2_SHA256 = (
    "f4b6792b9ed1dc4000c72e3aa552be00950f312b4418e2fa5c3d822618cf0944"
)


def lf022_reviewed_proposer_prompt(version: str) -> tuple[str, str]:
    """Return the exact reviewed prompt path/hash for an admitted version."""

    prompts = {
        PROPOSER_TEMPLATE_VERSION: (
            LF022_REVIEWED_PROPOSER_PROMPT_PATH,
            LF022_REVIEWED_PROPOSER_PROMPT_SHA256,
        ),
        PROPOSER_TEMPLATE_VERSION_V2: (
            LF022_REVIEWED_PROPOSER_PROMPT_V2_PATH,
            LF022_REVIEWED_PROPOSER_PROMPT_V2_SHA256,
        ),
    }
    try:
        return prompts[version]
    except KeyError as exc:
        raise LF022ExecutionError(f"unsupported proposer prompt version {version!r}") from exc


class LF022ExecutionError(RuntimeError):
    """An execution admission or task failed closed."""


@dataclass(frozen=True, slots=True)
class VerifiedLF022ExecutionAdmission:
    """Hash-verified allocation and public-pool inputs."""

    admission_id: str
    plan: LF022ProductionPlanManifest
    audit: LF022PublicPoolAudit
    family_matrix: LF022ProductionFamilyMatrix


@dataclass(frozen=True, slots=True)
class VerifiedLF022ExecutionTaskInputs:
    """One immutable in-memory view of the exact public task artifacts.

    Batch execution loads and hash-checks these artifacts once per admission.
    Individual execution retains the existing fail-closed behavior by loading
    the same view on demand.
    """

    source_records: tuple[LF022ProductionSourceRecord, ...]
    theorems: tuple[TheoremRecord, ...]
    representations: tuple[RepresentationRecord, ...]
    contexts: tuple[ContextRecord, ...]
    clearances: tuple[LF022DenylistClearanceRecord, ...]
    benchmark_manifest: LF022BenchmarkRegistryManifest
    active_registry: FrozenRegistry
    authorization_registry: LF022PublicSourceAuthorizationRegistry
    allocation_tasks_by_id: Mapping[str, LF022ProductionTask]
    source_records_by_admission_id: Mapping[str, LF022ProductionSourceRecord]
    theorems_by_id: Mapping[str, TheoremRecord]
    representations_by_id: Mapping[str, RepresentationRecord]
    contexts_by_id: Mapping[str, ContextRecord]
    clearances_by_id: Mapping[str, LF022DenylistClearanceRecord]
    authorizations_by_id: Mapping[str, LF022PublicSourceAuthorization]
    active_registry_content_hash: str


def _safe_relative_path(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    if (
        not value.strip()
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in value
    ):
        raise ValueError(f"{field} must be a normalized repository-relative path")
    return value


def _content_id(prefix: str, record: StrictModel, *, id_field: str) -> str:
    return make_id(prefix, record.model_dump(mode="json", exclude={id_field}))


class LF022RCPRetryPolicy(StrictModel):
    """Frozen, bounded retry policy for response-confirmed transient failures."""

    schema_version: Literal[1] = 1
    max_attempts: int = Field(ge=1, le=5, strict=True)
    request_timeout_seconds: int = Field(default=3600, ge=1, le=3600, strict=True)
    base_delay_seconds: float = Field(ge=0.0, le=300.0)
    maximum_delay_seconds: float = Field(ge=0.0, le=3600.0)
    retryable_http_statuses: tuple[int, ...]
    honor_retry_after: Literal[True] = True
    retry_transport_unknown: Literal[False] = False
    retry_parse_failures: Literal[False] = False
    retry_lean_failures: Literal[False] = False

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        if self.maximum_delay_seconds < self.base_delay_seconds:
            raise ValueError("maximum_delay_seconds cannot be smaller than base_delay_seconds")
        if tuple(sorted(set(self.retryable_http_statuses))) != self.retryable_http_statuses:
            raise ValueError("retryable_http_statuses must be sorted and unique")
        if any(status < 400 or status > 599 for status in self.retryable_http_statuses):
            raise ValueError("retryable_http_statuses must contain HTTP error statuses")
        return self

    @property
    def policy_hash(self) -> str:
        return hash_canonical(self.model_dump(mode="json"))


class LF022RCPDecodingContract(StrictModel):
    """Exact model-specific decoding and thinking behavior."""

    schema_version: Literal[1] = 1
    contract_id: Literal[
        "kimi_k2_7_public_smoke_v3",
        "kimi_k2_7_public_proposer_v4",
        "qwen3_5_proposer_qualification_v1",
        "qwen3_5_proposer_qualification_v2",
        "glm5_2_proposer_qualification_v1",
        "glm5_2_proposer_qualification_v2",
        "deepseek_v4_proposer_qualification_v1",
    ]
    temperature: float = Field(ge=0.0, le=2.0)
    top_p: float = Field(gt=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0, strict=True)
    min_p: float | None = Field(default=None, ge=0.0, le=1.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    repetition_penalty: float | None = Field(default=None, ge=0.0)
    max_tokens: int = Field(ge=1, le=65_536, strict=True)
    seed: int | None = Field(default=None, ge=0, strict=True)
    stream: Literal[False] = False
    thinking_mode: Literal["forced_thinking", "enabled"]
    reasoning_effort: Literal["high"]
    chat_template_enable_thinking: Literal[True]
    chat_template_thinking: bool | None = None
    thinking_fields_forbidden: Literal[False] = False

    @model_validator(mode="after")
    def _exact_reviewed_contract(self) -> Self:
        contracts: dict[str, dict[str, object]] = {
            "kimi_k2_7_public_smoke_v3": {
                "temperature": 1.0,
                "top_p": 0.95,
                "top_k": None,
                "min_p": None,
                "presence_penalty": None,
                "repetition_penalty": None,
                "max_tokens": 16384,
                "seed": 42,
                "stream": False,
                "thinking_mode": "forced_thinking",
                "reasoning_effort": "high",
                "chat_template_enable_thinking": True,
                "chat_template_thinking": None,
                "thinking_fields_forbidden": False,
            },
            "kimi_k2_7_public_proposer_v4": {
                "temperature": 1.0,
                "top_p": 0.95,
                "top_k": None,
                "min_p": None,
                "presence_penalty": None,
                "repetition_penalty": None,
                "max_tokens": 32768,
                "seed": 42,
                "stream": False,
                "thinking_mode": "forced_thinking",
                "reasoning_effort": "high",
                "chat_template_enable_thinking": True,
                "chat_template_thinking": None,
                "thinking_fields_forbidden": False,
            },
            "qwen3_5_proposer_qualification_v1": {
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repetition_penalty": 1.0,
                "max_tokens": 4096,
                "seed": 42,
                "stream": False,
                "thinking_mode": "enabled",
                "reasoning_effort": "high",
                "chat_template_enable_thinking": True,
                "chat_template_thinking": None,
                "thinking_fields_forbidden": False,
            },
            "qwen3_5_proposer_qualification_v2": {
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repetition_penalty": 1.0,
                "max_tokens": 16384,
                "seed": 42,
                "stream": False,
                "thinking_mode": "enabled",
                "reasoning_effort": "high",
                "chat_template_enable_thinking": True,
                "chat_template_thinking": None,
                "thinking_fields_forbidden": False,
            },
            "glm5_2_proposer_qualification_v1": {
                "temperature": 0.0,
                "top_p": 1.0,
                "top_k": None,
                "min_p": None,
                "presence_penalty": None,
                "repetition_penalty": None,
                "max_tokens": 8192,
                "seed": 42,
                "stream": False,
                "thinking_mode": "enabled",
                "reasoning_effort": "high",
                "chat_template_enable_thinking": True,
                "chat_template_thinking": None,
                "thinking_fields_forbidden": False,
            },
            "glm5_2_proposer_qualification_v2": {
                "temperature": 0.0,
                "top_p": 1.0,
                "top_k": None,
                "min_p": None,
                "presence_penalty": None,
                "repetition_penalty": None,
                "max_tokens": 8192,
                "seed": 42,
                "stream": False,
                "thinking_mode": "enabled",
                "reasoning_effort": "high",
                "chat_template_enable_thinking": True,
                "chat_template_thinking": None,
                "thinking_fields_forbidden": False,
            },
            "deepseek_v4_proposer_qualification_v1": {
                "temperature": 0.0,
                "top_p": 1.0,
                "top_k": None,
                "min_p": None,
                "presence_penalty": None,
                "repetition_penalty": None,
                "max_tokens": 8192,
                "seed": 42,
                "stream": False,
                "thinking_mode": "enabled",
                "reasoning_effort": "high",
                "chat_template_enable_thinking": True,
                "chat_template_thinking": None,
                "thinking_fields_forbidden": False,
            },
        }
        expected = contracts[self.contract_id]
        observed = self.model_dump(mode="json", exclude={"schema_version", "contract_id"})
        if observed != expected:
            raise ValueError(f"decoding differs from exact reviewed contract {self.contract_id!r}")
        return self

    def provider_decoding(self) -> dict[str, str | int | float | bool | None]:
        result: dict[str, str | int | float | bool | None] = {
            "contract_id": self.contract_id,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "presence_penalty": self.presence_penalty,
            "repetition_penalty": self.repetition_penalty,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stream": self.stream,
            "thinking_mode": self.thinking_mode,
            "reasoning_effort": self.reasoning_effort,
            "chat_template_enable_thinking": self.chat_template_enable_thinking,
            "chat_template_thinking": self.chat_template_thinking,
            "thinking_fields_forbidden": self.thinking_fields_forbidden,
        }
        return result

    def wire_fields(self) -> dict[str, object]:
        result: dict[str, object] = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "stream": self.stream,
            "reasoning_effort": self.reasoning_effort,
            "chat_template_kwargs": {
                "enable_thinking": self.chat_template_enable_thinking,
            },
        }
        for field in (
            "top_k",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
            "seed",
        ):
            value = getattr(self, field)
            if value is not None:
                result[field] = value
        return result


class LF022RCPRouteBinding(StrictModel):
    """One exact provider route admitted from one frozen catalog snapshot."""

    schema_version: Literal[1] = 1
    provider_id: Literal["epfl_rcp"]
    model_id: str = Field(min_length=3)
    deployment_id: str = Field(min_length=1)
    proposer_family_id: str = Field(min_length=1)
    canonical_family: str = Field(min_length=3)
    catalog_snapshot_id: str = Field(pattern=id_pattern("lf022_provider_catalog"))
    route_snapshot_revision: str = Field(pattern=r"^rcp-catalog-sha256:[0-9a-f]{64}$")
    underlying_checkpoint_revision_status: Literal["provider_not_disclosed"]
    execution_scope: Literal[
        "public_provisional_g_open",
        "one_item_proposer_qualification_only",
    ]
    decoding: LF022RCPDecodingContract

    @model_validator(mode="after")
    def _model_specific_contract(self) -> Self:
        expected = {
            "moonshotai/Kimi-K2.7-Code": (
                "moonshot_kimi_k2",
                "moonshotai/kimi-k2",
                (
                    "kimi_k2_7_public_smoke_v3",
                    "kimi_k2_7_public_proposer_v4",
                ),
                ("public_provisional_g_open",),
            ),
            "Qwen/Qwen3.5-397B-A17B": (
                "qwen3",
                "qwen/qwen3",
                (
                    "qwen3_5_proposer_qualification_v1",
                    "qwen3_5_proposer_qualification_v2",
                ),
                (
                    "one_item_proposer_qualification_only",
                    "public_provisional_g_open",
                ),
            ),
            "zai-org/GLM-5.2": (
                "glm5",
                "zai-org/glm-5.2",
                (
                    "glm5_2_proposer_qualification_v1",
                    "glm5_2_proposer_qualification_v2",
                ),
                (
                    "one_item_proposer_qualification_only",
                    "public_provisional_g_open",
                ),
            ),
            "deepseek-ai/DeepSeek-V4-Pro": (
                "deepseek_v4",
                "deepseek-ai/deepseek-v4-pro",
                ("deepseek_v4_proposer_qualification_v1",),
                (
                    "one_item_proposer_qualification_only",
                    "public_provisional_g_open",
                ),
            ),
        }.get(self.model_id)
        if expected is None:
            raise ValueError("route is outside the exact reviewed LF-022 proposer scope")
        expected_family, expected_canonical, expected_contracts, allowed_scopes = expected
        if isinstance(expected_contracts, str):
            expected_contracts = (expected_contracts,)
        if (
            self.proposer_family_id != expected_family
            or self.canonical_family != expected_canonical
            or self.decoding.contract_id not in expected_contracts
            or self.execution_scope not in allowed_scopes
        ):
            raise ValueError(
                "route family or decoding contract differs from the reviewed proposer route"
            )
        v1_qualification_contracts = {
            "qwen3_5_proposer_qualification_v1",
            "glm5_2_proposer_qualification_v1",
        }
        if (
            self.execution_scope == "public_provisional_g_open"
            and self.decoding.contract_id in v1_qualification_contracts
        ):
            raise ValueError(
                "Qwen/GLM v1 decoding is restricted to one-item proposer qualification"
            )
        return self


class LF022ExecutionArtifacts(StrictModel):
    """All exact inputs required before a public proposer call."""

    public_pool_audit: LF022ArtifactBinding
    allocation_plan: LF022ArtifactBinding
    provider_catalog_raw: LF022ArtifactBinding
    provider_catalog_normalized: LF022ArtifactBinding
    reviewed_route_portfolio: LF022ArtifactBinding
    reviewed_route_contract: LF022ArtifactBinding
    reviewed_route_evidence: LF022ArtifactBinding
    prompt_template: LF022ArtifactBinding
    code_bundle: LF022ArtifactBinding
    proposer_production_eligibility: LF022ArtifactBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    qualification_supersession: LF022ArtifactBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


FailedQualificationTerminalStatus = Literal[
    "provider_exhausted",
    "proposer_parse_failed",
]


class LF022QualificationSupersession(StrictModel):
    """Append-only authority for one fresh attempt after a verified failure."""

    schema_version: Literal[1] = 1
    supersession_id: str = Field(pattern=id_pattern("lf022_qualification_supersession"))
    proposer_family_id: Literal["qwen3", "glm5"]
    model_id: str
    previous_claim_id: str = Field(pattern=id_pattern("lf022_qualification_claim"))
    previous_claim: LF022ArtifactBinding
    previous_admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    previous_admission: LF022ArtifactBinding
    previous_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    previous_task: LF022ArtifactBinding
    previous_terminal_id: str = Field(pattern=id_pattern("lf022_execution_terminal"))
    previous_terminal: LF022ArtifactBinding
    previous_terminal_status: FailedQualificationTerminalStatus
    previous_terminal_error_code: str = Field(min_length=1)
    previous_decoding_contract_id: str = Field(min_length=1)
    next_decoding_contract_id: str = Field(min_length=1)
    reason: Literal["replay_verified_failed_qualification"]
    exact_failed_replay_verified: Literal[True] = True
    replay_network_calls: Literal[0] = 0
    semantic_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if self.previous_decoding_contract_id == self.next_decoding_contract_id:
            raise ValueError("qualification supersession requires a new decoding contract")
        expected = _content_id(
            "lf022_qualification_supersession",
            self,
            id_field="supersession_id",
        )
        if self.supersession_id != expected:
            raise ValueError("supersession_id does not match canonical qualification recovery")
        return self


class LF022QualificationClaim(StrictModel):
    """Repository-global exactly-once reservation for an unqualified proposer."""

    schema_version: Literal[1, 2] = 1
    claim_id: str = Field(pattern=id_pattern("lf022_qualification_claim"))
    proposer_family_id: Literal["qwen3", "glm5", "deepseek_v4"]
    model_id: str
    execution_scope: Literal["one_item_proposer_qualification_only"]
    admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    allocation_task_id: str = Field(pattern=id_pattern("lf022_production_task"))
    public_pool_audit_id: str = Field(pattern=id_pattern("lf022_public_pool_audit"))
    allocation_plan_id: str = Field(pattern=id_pattern("lf022_production_plan"))
    output_quality_tier: Literal["provisional"] = "provisional"
    semantic_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    qualification_supersession: LF022ArtifactBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        expected_schema = 2 if self.qualification_supersession is not None else 1
        if self.schema_version != expected_schema:
            raise ValueError("qualification claim schema differs from supersession state")
        expected = _content_id(
            "lf022_qualification_claim",
            self,
            id_field="claim_id",
        )
        if self.claim_id != expected:
            raise ValueError("claim_id does not match canonical qualification claim")
        return self


class LF022GOpenExecutionAdmission(StrictModel):
    """Reviewed authority for proposer-only, public, provisional collection."""

    schema_version: Literal[1, 2, 3]
    admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    status: Literal["public_provisional_g_open_admitted"]
    normalization_version: Literal["repr_v3"]
    public_pool_audit_id: str = Field(pattern=id_pattern("lf022_public_pool_audit"))
    allocation_plan_id: str = Field(pattern=id_pattern("lf022_production_plan"))
    artifacts: LF022ExecutionArtifacts
    route: LF022RCPRouteBinding
    retry_policy: LF022RCPRetryPolicy
    retry_policy_hash: str = Field(pattern=HEX64_PATTERN)
    code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    prompt_template_id: Literal["lean_variant"] = "lean_variant"
    prompt_template_version: Literal["v1", "v2"] = "v1"
    distribution: Literal["G_open"]
    public_sources_only: Literal[True]
    private_source_content_forbidden: Literal[True]
    execute_requires_explicit_flag: Literal[True]
    network_execution_authorized: Literal[True]
    outputs_provisional_only: Literal[True]
    semantic_labels_created: Literal[False]
    silver_promotion_enabled: Literal[False]
    gold_promotion_enabled: Literal[False]
    training_eligible: Literal[False]
    evaluation_eligible: Literal[False]
    gate_credit_claimed: Literal[False]

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        expected_prompt_version = (
            PROPOSER_TEMPLATE_VERSION_V2
            if self.route.decoding.contract_id == "kimi_k2_7_public_proposer_v4"
            else PROPOSER_TEMPLATE_VERSION
        )
        if self.prompt_template_version != expected_prompt_version:
            raise ValueError("prompt version differs from the reviewed decoding contract")
        if self.retry_policy_hash != self.retry_policy.policy_hash:
            raise ValueError("retry_policy_hash does not match retry_policy")
        expected_revision = _CATALOG_REVISION_PREFIX + self.artifacts.provider_catalog_raw.sha256
        if self.route.route_snapshot_revision != expected_revision:
            raise ValueError("route_snapshot_revision must bind the exact raw catalog artifact")
        requires_eligibility = (
            self.route.execution_scope == "public_provisional_g_open"
            and self.route.decoding.contract_id
            in {
                "kimi_k2_7_public_proposer_v4",
                "qwen3_5_proposer_qualification_v2",
                "glm5_2_proposer_qualification_v2",
                "deepseek_v4_proposer_qualification_v1",
            }
        )
        if requires_eligibility != (self.artifacts.proposer_production_eligibility is not None):
            raise ValueError(
                "qualified production scope requires exactly one bound proposer eligibility"
            )
        has_supersession = self.artifacts.qualification_supersession is not None
        recovery_contract = self.route.decoding.contract_id in {
            "qwen3_5_proposer_qualification_v2",
            "glm5_2_proposer_qualification_v2",
        }
        if has_supersession and (
            requires_eligibility
            or self.route.proposer_family_id not in {"qwen3", "glm5"}
            or self.route.execution_scope != "one_item_proposer_qualification_only"
        ):
            raise ValueError(
                "qualification supersession is restricted to an unqualified Qwen/GLM route"
            )
        if (
            self.route.execution_scope == "one_item_proposer_qualification_only"
            and recovery_contract != has_supersession
        ):
            raise ValueError("qualification v2 recovery contract requires exactly one supersession")
        expected_schema = 2 if requires_eligibility else 3 if has_supersession else 1
        if self.schema_version != expected_schema:
            raise ValueError(
                "execution admission schema version differs from route eligibility state"
            )
        expected = _content_id(
            "lf022_execution_admission",
            self,
            id_field="admission_id",
        )
        if self.admission_id != expected:
            raise ValueError("admission_id does not match canonical execution binding")
        return self


def make_lf022_g_open_execution_admission(
    *,
    public_pool_audit_id: str,
    allocation_plan_id: str,
    artifacts: LF022ExecutionArtifacts,
    route: LF022RCPRouteBinding,
    retry_policy: LF022RCPRetryPolicy,
    code_tree_hash: str,
) -> LF022GOpenExecutionAdmission:
    payload: dict[str, object] = {
        "schema_version": (
            2
            if artifacts.proposer_production_eligibility is not None
            else 3
            if artifacts.qualification_supersession is not None
            else 1
        ),
        "status": "public_provisional_g_open_admitted",
        "normalization_version": "repr_v3",
        "public_pool_audit_id": public_pool_audit_id,
        "allocation_plan_id": allocation_plan_id,
        "artifacts": artifacts.model_dump(mode="json"),
        "route": route.model_dump(mode="json"),
        "retry_policy": retry_policy.model_dump(mode="json"),
        "retry_policy_hash": retry_policy.policy_hash,
        "code_tree_hash": code_tree_hash,
        "prompt_template_id": PROPOSER_TEMPLATE_ID,
        "prompt_template_version": (
            PROPOSER_TEMPLATE_VERSION_V2
            if route.decoding.contract_id == "kimi_k2_7_public_proposer_v4"
            else PROPOSER_TEMPLATE_VERSION
        ),
        "distribution": "G_open",
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "execute_requires_explicit_flag": True,
        "network_execution_authorized": True,
        "outputs_provisional_only": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022GOpenExecutionAdmission.model_validate(
        {
            **payload,
            "admission_id": make_id("lf022_execution_admission", payload),
        }
    )


class LF022GOpenExecutionTask(StrictModel):
    """One allocation-bound, public proposer request."""

    schema_version: Literal[1, 2] = 1
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    execution_admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    allocation_plan_id: str = Field(pattern=id_pattern("lf022_production_plan"))
    allocation_task: LF022ProductionTask
    normalization_version: Literal["repr_v3"]
    source: PublicLeanVariantSource
    proposal_count: int = Field(ge=1, le=32, strict=True)
    requested_relations: tuple[IntendedRelation, ...] = Field(min_length=1)
    distribution: Literal["G_open"]
    semantic_labels_created: Literal[False]
    silver_promotion_enabled: Literal[False]
    gold_promotion_enabled: Literal[False]
    training_eligible: Literal[False]
    evaluation_eligible: Literal[False]
    source_statement_version: Literal["named_signature_v2"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _public_open_task(self) -> Self:
        if (self.schema_version == 2) != (self.source_statement_version == "named_signature_v2"):
            raise ValueError("task schema differs from proposer source-statement version")
        allocation = self.allocation_task
        if allocation.distribution != "G_open":
            raise ValueError("proposer-only execution accepts only G_open allocations")
        if (
            allocation.theorem_id != self.source.source_theorem_id
            or allocation.representation_id != self.source.source_representation_id
            or allocation.context_id != self.source.context_id
        ):
            raise ValueError("source IDs differ from the allocation task")
        serialized = canonical_json_bytes(self.source.model_dump(mode="json")).decode("utf-8")
        if any(marker in serialized.casefold() for marker in _PRIVATE_SOURCE_MARKERS):
            raise ValueError("private sft_classic content is forbidden")
        if (
            not self.source.source_is_public
            or not self.source.external_transmission_allowed
            or not self.source.denylist_checked
            or self.source.denylist_hits
        ):
            raise ValueError("execution task requires public, transmissible, denylist-clear source")
        if self.source.optional_natural_language is not None:
            raise ValueError(
                "Lean-only G_open execution forbids optional natural-language prompt content"
            )
        if len(set(self.requested_relations)) != len(self.requested_relations):
            raise ValueError("requested_relations must be unique")
        expected = _content_id(
            "lf022_execution_task",
            self,
            id_field="execution_task_id",
        )
        if self.execution_task_id != expected:
            raise ValueError("execution_task_id does not match canonical task content")
        return self

    def prompt_request(self) -> VariantPromptRequest:
        return VariantPromptRequest(
            request_id=self.execution_task_id,
            source=self.source,
            proposal_count=self.proposal_count,
            requested_relations=self.requested_relations,
            requested_error_types=(),
            requested_sci_categories=(),
            generation_distribution="G_open",
        )


def make_lf022_g_open_execution_task(
    *,
    admission: LF022GOpenExecutionAdmission,
    allocation_task: LF022ProductionTask,
    source: PublicLeanVariantSource,
    proposal_count: int = 1,
    requested_relations: tuple[IntendedRelation, ...] = (IntendedRelation.NEAR_MISS,),
) -> LF022GOpenExecutionTask:
    if allocation_task.proposer_family_id != admission.route.proposer_family_id:
        raise LF022ExecutionError("allocation proposer family differs from the admitted RCP route")
    if (
        admission.route.execution_scope == "one_item_proposer_qualification_only"
        and proposal_count != 1
    ):
        raise LF022ExecutionError(
            "proposer qualification routes require exactly one requested proposal"
        )
    payload: dict[str, object] = {
        "schema_version": 2,
        "execution_admission_id": admission.admission_id,
        "allocation_plan_id": admission.allocation_plan_id,
        "allocation_task": allocation_task.model_dump(mode="json"),
        "normalization_version": admission.normalization_version,
        "source": source.model_dump(mode="json"),
        "proposal_count": proposal_count,
        "requested_relations": [relation.value for relation in requested_relations],
        "distribution": "G_open",
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "source_statement_version": "named_signature_v2",
    }
    return LF022GOpenExecutionTask.model_validate(
        {
            **payload,
            "execution_task_id": make_id("lf022_execution_task", payload),
        }
    )


def make_lf022_qualification_claim(
    *,
    admission: LF022GOpenExecutionAdmission,
    task: LF022GOpenExecutionTask,
) -> LF022QualificationClaim:
    """Bind one repository-global qualification task for a reviewed proposer."""

    if admission.route.proposer_family_id not in {"qwen3", "glm5", "deepseek_v4"}:
        raise LF022ExecutionError("route may not reserve a proposer qualification claim")
    if admission.route.execution_scope != "one_item_proposer_qualification_only":
        raise LF022ExecutionError("qualification claim requires the one-item execution scope")
    if task.execution_admission_id != admission.admission_id:
        raise LF022ExecutionError("qualification claim task differs from its admission")
    supersession = admission.artifacts.qualification_supersession
    payload: dict[str, object] = {
        "schema_version": 2 if supersession is not None else 1,
        "proposer_family_id": admission.route.proposer_family_id,
        "model_id": admission.route.model_id,
        "execution_scope": admission.route.execution_scope,
        "admission_id": admission.admission_id,
        "execution_task_id": task.execution_task_id,
        "allocation_task_id": task.allocation_task.task_id,
        "public_pool_audit_id": admission.public_pool_audit_id,
        "allocation_plan_id": admission.allocation_plan_id,
        "output_quality_tier": "provisional",
        "semantic_labels_created": False,
        "training_eligible": False,
        "gate_credit_claimed": False,
        "qualification_supersession": (
            supersession.model_dump(mode="json") if supersession is not None else None
        ),
    }
    if supersession is None:
        payload.pop("qualification_supersession")
    return LF022QualificationClaim.model_validate(
        {
            **payload,
            "claim_id": make_id("lf022_qualification_claim", payload),
        }
    )


def lf022_qualification_claim_path(
    *,
    output_root: Path,
    admission: LF022GOpenExecutionAdmission,
    claim: LF022QualificationClaim,
) -> Path:
    """Return the legacy or append-only content-addressed claim location."""

    claims_root = output_root / "qualification_claims"
    if claim.qualification_supersession is None:
        return claims_root / f"{admission.route.proposer_family_id}.json"
    digest = claim.claim_id.split(":", 1)[1]
    return claims_root / admission.route.proposer_family_id / f"{digest}.json"


def make_lf022_named_signature(
    *,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
) -> str:
    """Build a proof-free named statement with every elaborated binder exposed."""

    def valid_name(name: str) -> bool:
        segments = name.split(".")
        return bool(segments) and all(
            bool(segment)
            and (segment[0].isalpha() or segment[0] == "_")
            and all(
                character.isalnum() or character in {"_", "'", "!", "?"}
                for character in segment[1:]
            )
            for segment in segments
        )

    if (
        theorem.declaration_name is None
        or not theorem.declaration_name.strip()
        or not valid_name(theorem.declaration_name)
        or representation.theorem_id != theorem.theorem_id
        or representation.context_id != theorem.context_id
        or representation.normalization_version != "repr_v3"
        or representation.signature_pp is None
        or representation.view_status.get("signature_pp") is not ViewStatus.OK
    ):
        raise LF022ExecutionError(
            "proposer source requires a named theorem and successful signature_pp view"
        )
    signature = representation.signature_pp.strip()
    if not signature:
        raise LF022ExecutionError("proposer signature is empty or malformed")
    # ``signature_pp`` is a hash-bound, type-only representation.  ``:=`` may
    # legitimately occur inside that type (for example in structure literals
    # or ``let`` expressions), so it is not evidence of an outer proof body.
    declaration_kind = (
        theorem.declaration_kind if theorem.declaration_kind in {"theorem", "lemma"} else "theorem"
    )
    return f"{declaration_kind} {theorem.declaration_name} : {signature}"


def _bound_path(
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    label: str,
) -> Path:
    root = repo_root.resolve(strict=True)
    relative = PurePosixPath(_safe_relative_path(binding.path, field=label))
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LF022ExecutionError(f"{label} contains a symlinked component")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise LF022ExecutionError(f"{label} is missing: {binding.path}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise LF022ExecutionError(f"{label} must be a regular repository file")
    observed = hash_file(resolved)
    if observed != binding.sha256:
        raise LF022ExecutionError(f"{label} SHA-256 mismatch: {observed} != {binding.sha256}")
    return resolved


def _load_strict_json(
    path: Path,
    model: type[StrictModel],
    *,
    label: str,
) -> StrictModel:
    try:
        raw = path.read_bytes()
        record = model.model_validate(cast(object, json.loads(raw)))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LF022ExecutionError(f"invalid {label}: {exc}") from exc
    canonical = canonical_json_bytes(record.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise LF022ExecutionError(f"{label} is not canonical JSON")
    return record


def _load_raw_rcp_catalog_ids(path: Path) -> frozenset[str]:
    """Parse the exact OpenAI-compatible ``/models`` response bound by admission."""

    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LF022ExecutionError(f"invalid raw RCP catalog: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise LF022ExecutionError("raw RCP catalog must contain a data list")
    model_ids: list[str] = []
    for index, item in enumerate(document["data"]):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise LF022ExecutionError(f"raw RCP catalog data[{index}] lacks a string model id")
        model_id = cast(str, item["id"]).strip()
        if not model_id:
            raise LF022ExecutionError(f"raw RCP catalog data[{index}] has an empty model id")
        model_ids.append(model_id)
    if not model_ids or len(model_ids) != len(set(model_ids)):
        raise LF022ExecutionError("raw RCP catalog model IDs must be nonempty and unique")
    return frozenset(model_ids)


def _load_bound_jsonl[RecordT: StrictModel](
    *,
    repo_root: Path,
    binding: LF022JSONLArtifactBinding,
    model: type[RecordT],
    label: str,
) -> tuple[RecordT, ...]:
    path = _bound_path(repo_root=repo_root, binding=binding, label=label)
    raw_lines = path.read_bytes().splitlines(keepends=True)
    if len(raw_lines) != binding.record_count or not raw_lines:
        raise LF022ExecutionError(f"{label} record count differs from its binding")
    records: list[RecordT] = []
    for line_number, line in enumerate(raw_lines, start=1):
        if not line.endswith(b"\n"):
            raise LF022ExecutionError(f"{label}:{line_number} lacks a final newline")
        try:
            record = model.model_validate_json(line)
        except ValueError as exc:
            raise LF022ExecutionError(f"invalid {label}:{line_number}: {exc}") from exc
        if line != canonical_json_bytes(record.model_dump(mode="json")) + b"\n":
            raise LF022ExecutionError(f"{label}:{line_number} is not canonical JSONL")
        records.append(record)
    return tuple(records)


def _unique_execution_index[RecordT: StrictModel](
    records: tuple[RecordT, ...],
    *,
    attribute: str,
    label: str,
) -> dict[str, RecordT]:
    indexed: dict[str, RecordT] = {}
    for record in records:
        key = cast(str, getattr(record, attribute))
        if key in indexed:
            raise LF022ExecutionError(f"{label} contains duplicate key {key}")
        indexed[key] = record
    return indexed


def _reviewed_route_payload(
    *,
    path: Path,
    route: LF022RCPRouteBinding,
) -> dict[str, object]:
    """Recover the exact reviewed decoding payload for one supported route."""

    relative = "configs/generation/lf022_rcp_public_smoke_v3.yaml"
    if path.as_posix().split("/")[-len(PurePosixPath(relative).parts) :] != list(
        PurePosixPath(relative).parts
    ):
        raise LF022ExecutionError(
            f"reviewed route contract must bind canonical artifact {relative}"
        )
    try:
        document = load_yaml_mapping(path)
    except ValueError as exc:
        raise LF022ExecutionError(f"invalid reviewed route contract: {exc}") from exc
    providers = document.get("providers")
    if not isinstance(providers, dict):
        raise LF022ExecutionError("reviewed public smoke contract lacks providers")
    matching = tuple(
        value
        for value in providers.values()
        if isinstance(value, dict) and value.get("model_id") == route.model_id
    )
    if len(matching) != 1:
        raise LF022ExecutionError("route is absent or duplicated in reviewed public smoke")
    provider = matching[0]
    if (
        provider.get("provider") != "epfl_rcp"
        or provider.get("transport") != "rcp_openai_compatible"
        or provider.get("enabled_for_this_smoke") is not True
    ):
        raise LF022ExecutionError("reviewed public smoke provider contract is not executable")
    decoding = provider.get("decoding")
    if not isinstance(decoding, dict):
        raise LF022ExecutionError("reviewed public smoke lacks decoding")
    return cast(dict[str, object], decoding)


def _verify_reviewed_route_contract(
    *,
    repo_root: Path,
    portfolio_path: Path,
    contract_path: Path,
    evidence_path: Path,
    route: LF022RCPRouteBinding,
) -> None:
    required_evidence = "reports/generation/lf022_rcp_public_smoke_qualification_v1.json"
    if evidence_path.as_posix().split("/")[-len(PurePosixPath(required_evidence).parts) :] != list(
        PurePosixPath(required_evidence).parts
    ):
        raise LF022ExecutionError(
            "reviewed route evidence must bind the successful public smoke report"
        )
    try:
        evidence = _LF022RouteEvidence.model_validate_json(evidence_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise LF022ExecutionError(f"invalid reviewed_route_evidence: {exc}") from exc
    if (
        evidence.verified_success.status != "success"
        or not evidence.verified_success.replay_verified
    ):
        raise LF022ExecutionError("reviewed public smoke evidence is not replay-verified")

    if route.proposer_family_id == "moonshot_kimi_k2":
        if route.execution_scope != "public_provisional_g_open":
            raise LF022ExecutionError("Kimi route must use the reviewed production scope")
        if route.decoding.contract_id == "kimi_k2_7_public_proposer_v4":
            required_contract = "configs/generation/lf022_kimi_k2_7_proposer_v4.yaml"
            if contract_path.as_posix().split("/")[
                -len(PurePosixPath(required_contract).parts) :
            ] != list(PurePosixPath(required_contract).parts):
                raise LF022ExecutionError(
                    "Kimi-v4 route does not bind its canonical challenge contract"
                )
            from leanfaith.generation.lf022_kimi_v4_selection import (
                LF022KimiV4ChallengeContract,
            )

            try:
                mapping = dict(load_yaml_mapping(contract_path))
                contract_decoding = dict(cast(dict[str, object], mapping["decoding"]))
                contract_decoding.update(
                    schema_version=1,
                    contract_id=mapping["contract_id"],
                )
                mapping["decoding"] = contract_decoding
                contract = LF022KimiV4ChallengeContract.model_validate(mapping)
            except (KeyError, TypeError, ValueError) as exc:
                raise LF022ExecutionError(f"invalid Kimi-v4 route contract: {exc}") from exc
            if (
                contract.model_id != route.model_id
                or contract.family_id != route.proposer_family_id
                or contract.canonical_family != route.canonical_family
                or contract.provider != route.provider_id
                or contract.execution_scope != route.execution_scope
                or contract.decoding != route.decoding
                or contract.prompt.artifact != LF022_REVIEWED_PROPOSER_PROMPT_V2_PATH
                or contract.prompt.sha256 != LF022_REVIEWED_PROPOSER_PROMPT_V2_SHA256
                or evidence.verified_success.proposer != route.model_id
                or evidence.verified_success.config_file
                != "configs/generation/lf022_rcp_public_smoke_v3.yaml"
                or evidence.verified_success.config_file_sha256
                != hash_file(repo_root / "configs/generation/lf022_rcp_public_smoke_v3.yaml")
            ):
                raise LF022ExecutionError(
                    "Kimi-v4 route differs from its reviewed challenge and prior transport evidence"
                )
            return
        reviewed = _reviewed_route_payload(path=contract_path, route=route)
        expected = route.decoding.model_dump(mode="json")
        for field in (
            "schema_version",
            "contract_id",
            "thinking_mode",
            "top_k",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
            "chat_template_thinking",
            "thinking_fields_forbidden",
        ):
            expected.pop(field)
        if reviewed != expected:
            raise LF022ExecutionError(
                "live route decoding differs from its exact reviewed contract artifact"
            )
        if (
            evidence.verified_success.proposer != route.model_id
            or evidence.verified_success.config_file
            != "configs/generation/lf022_rcp_public_smoke_v3.yaml"
            or evidence.verified_success.config_file_sha256 != hash_file(contract_path)
        ):
            raise LF022ExecutionError(
                "production proposer route lacks exact successful proposer evidence"
            )
        return

    required_contract = {
        "qwen3_5_proposer_qualification_v1": (
            "configs/generation/lf022_qwen3_5_proposer_qualification_v1.yaml"
        ),
        "qwen3_5_proposer_qualification_v2": (
            "configs/generation/lf022_qwen3_5_proposer_qualification_v2.yaml"
        ),
        "glm5_2_proposer_qualification_v1": (
            "configs/generation/lf022_glm5_2_proposer_qualification_v1.yaml"
        ),
        "glm5_2_proposer_qualification_v2": (
            "configs/generation/lf022_glm5_2_proposer_qualification_v2.yaml"
        ),
        "deepseek_v4_proposer_qualification_v1": (
            "configs/generation/lf022_deepseek_v4_proposer_qualification_v1.yaml"
        ),
    }[route.decoding.contract_id]
    if contract_path.as_posix().split("/")[-len(PurePosixPath(required_contract).parts) :] != list(
        PurePosixPath(required_contract).parts
    ):
        raise LF022ExecutionError(
            "qualification route does not bind its canonical proposer contract"
        )
    try:
        qualification = _LF022ProposerQualificationContract.model_validate(
            load_yaml_mapping(contract_path)
        )
    except ValueError as exc:
        raise LF022ExecutionError(f"invalid proposer qualification contract: {exc}") from exc
    if (
        qualification.contract_id != route.decoding.contract_id
        or qualification.model_id != route.model_id
        or qualification.family_id != route.proposer_family_id
        or qualification.canonical_family != route.canonical_family
        or qualification.provider != route.provider_id
        or qualification.execution_scope != "one_item_proposer_qualification_only"
        or LF022RCPDecodingContract.model_validate(
            qualification.decoding.route_payload(
                contract_id=qualification.contract_id,
            )
        )
        != route.decoding
    ):
        raise LF022ExecutionError(
            "qualification route differs from exact role-aware proposer contract"
        )
    transport_evidence = qualification.prior_transport_evidence
    common_transport_mismatch = (
        transport_evidence.artifact != required_evidence
        or transport_evidence.sha256 != hash_file(evidence_path)
        or evidence.verified_success.proposer == route.model_id
    )
    capability = qualification.wire_capability_policy
    if route.proposer_family_id == "deepseek_v4":
        accepted_failure = capability.accepted_failure_manifest
        terminal_evidence = tuple(
            item
            for item in evidence.terminal_attempts
            if accepted_failure is not None
            and item.get("config_file") == "configs/generation/lf022_rcp_public_smoke_v2.yaml"
            and item.get("status") == "terminal_failure"
            and item.get("failure_manifest") == accepted_failure.artifact
            and item.get("failure_manifest_sha256") == accepted_failure.sha256
        )
        transport_mismatch = len(terminal_evidence) != 1
    else:
        transport_mismatch = route.model_id not in {
            judge.model for judge in evidence.verified_success.judge_calls
        }
    if common_transport_mismatch or transport_mismatch:
        raise LF022ExecutionError("qualification route lacks exact judge-transport-only evidence")
    if (
        capability.evidence != transport_evidence
        or qualification.decoding.reasoning_effort not in capability.reasoning_effort_values
        or qualification.decoding.chat_template_enable_thinking
        not in capability.chat_template_enable_thinking_values
    ):
        raise LF022ExecutionError(
            "qualification route requests unsupported or unbound reasoning fields"
        )
    accepted_wire_path = _bound_path(
        repo_root=repo_root,
        binding=LF022ArtifactBinding(
            path=capability.accepted_wire_request.artifact,
            sha256=capability.accepted_wire_request.sha256,
        ),
        label="qualification accepted wire request",
    )
    try:
        accepted_wire_raw = accepted_wire_path.read_bytes()
        accepted_wire = json.loads(accepted_wire_raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LF022ExecutionError(f"invalid qualification accepted wire request: {exc}") from exc
    if (
        not isinstance(accepted_wire, dict)
        or accepted_wire_raw != canonical_json_bytes(accepted_wire) + b"\n"
        or accepted_wire.get("model") != route.model_id
        or accepted_wire.get("reasoning_effort") != qualification.decoding.reasoning_effort
        or not isinstance(accepted_wire.get("chat_template_kwargs"), dict)
        or accepted_wire["chat_template_kwargs"].get("enable_thinking")
        is not qualification.decoding.chat_template_enable_thinking
    ):
        raise LF022ExecutionError(
            "qualification reasoning capability differs from exact accepted wire bytes"
        )
    if capability.accepted_evidence_status == "parsed_success_manifest":
        accepted_manifest_path = _bound_path(
            repo_root=repo_root,
            binding=LF022ArtifactBinding(
                path=evidence.verified_success.manifest,
                sha256=evidence.verified_success.manifest_sha256,
            ),
            label="qualification replay manifest",
        )
        try:
            accepted_manifest_raw = accepted_manifest_path.read_bytes()
            accepted_manifest = json.loads(accepted_manifest_raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LF022ExecutionError(f"invalid qualification replay manifest: {exc}") from exc
        if (
            not isinstance(accepted_manifest, dict)
            or accepted_manifest_raw != canonical_json_bytes(accepted_manifest) + b"\n"
            or not isinstance(accepted_manifest.get("call_artifacts"), list)
        ):
            raise LF022ExecutionError(
                "qualification replay manifest is not canonical call evidence"
            )
        matching_calls = tuple(
            item
            for item in accepted_manifest["call_artifacts"]
            if isinstance(item, dict)
            and item.get("call_label") == capability.accepted_call_label
            and item.get("model_id") == route.model_id
            and item.get("role") == "judge"
            and item.get("parse_status") == "parsed"
            and item.get("wire_request_artifact") == capability.accepted_wire_request.artifact
            and item.get("wire_request_sha256") == capability.accepted_wire_request.sha256
        )
        if len(matching_calls) != 1:
            raise LF022ExecutionError(
                "qualification accepted wire request is not one exact replay-manifest call"
            )
        accepted_call = matching_calls[0]
        response_artifact = accepted_call.get("wire_response_artifact")
        response_sha256 = accepted_call.get("wire_response_sha256")
        if not isinstance(response_artifact, str) or not isinstance(response_sha256, str):
            raise LF022ExecutionError("qualification accepted call lacks bound response bytes")
    else:
        accepted_response = capability.accepted_wire_response
        accepted_failure = capability.accepted_failure_manifest
        assert accepted_response is not None
        assert accepted_failure is not None
        response_artifact = accepted_response.artifact
        response_sha256 = accepted_response.sha256
        failure_path = _bound_path(
            repo_root=repo_root,
            binding=LF022ArtifactBinding(
                path=accepted_failure.artifact,
                sha256=accepted_failure.sha256,
            ),
            label="qualification accepted failure manifest",
        )
        try:
            failure_raw = failure_path.read_bytes()
            failure = json.loads(failure_raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LF022ExecutionError(
                f"invalid qualification accepted failure manifest: {exc}"
            ) from exc
        if (
            not isinstance(failure, dict)
            or failure_raw != canonical_json_bytes(failure) + b"\n"
            or failure.get("error_type") != "JudgeOutputParseError"
            or failure.get("terminal") is not True
            or failure.get("semantic_labels_created") is not False
            or failure.get("training_eligible") is not False
            or not isinstance(failure.get("artifacts"), list)
        ):
            raise LF022ExecutionError(
                "qualification failure manifest is not a canonical parse-failure terminal"
            )
        artifact_pairs = {
            (item.get("artifact"), item.get("sha256"))
            for item in failure["artifacts"]
            if isinstance(item, dict)
        }
        if {
            (
                capability.accepted_wire_request.artifact,
                capability.accepted_wire_request.sha256,
            ),
            (response_artifact, response_sha256),
        } - artifact_pairs:
            raise LF022ExecutionError(
                "qualification failure manifest does not bind request and response bytes"
            )
    accepted_response_path = _bound_path(
        repo_root=repo_root,
        binding=LF022ArtifactBinding(
            path=response_artifact,
            sha256=response_sha256,
        ),
        label="qualification accepted wire response",
    )
    try:
        accepted_response_raw = accepted_response_path.read_bytes()
        accepted_response = json.loads(accepted_response_raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LF022ExecutionError(f"invalid qualification accepted wire response: {exc}") from exc
    choices = accepted_response.get("choices") if isinstance(accepted_response, dict) else None
    if (
        not isinstance(accepted_response, dict)
        or accepted_response.get("model") != route.model_id
        or not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
        or choices[0].get("finish_reason") != "stop"
    ):
        raise LF022ExecutionError(
            "qualification accepted response does not prove successful exact route handling"
        )
    if route.proposer_family_id == "qwen3":
        source = qualification.portfolio_source
        if (
            source is None
            or source.artifact != "configs/generation/rcp_provider_portfolio_v2.yaml"
            or source.sha256 != hash_file(portfolio_path)
        ):
            raise LF022ExecutionError(
                "Qwen proposer qualification does not bind portfolio-v2 decoding"
            )
    elif qualification.portfolio_source is not None:
        raise LF022ExecutionError(
            "non-Qwen proposer qualification must not invent a portfolio-v2 route"
        )


def _verify_reviewed_v2_portfolio(
    *,
    path: Path,
    route: LF022RCPRouteBinding,
) -> None:
    """Bind portfolio routes; separately reviewed late-added families stay absent."""

    required = "configs/generation/rcp_provider_portfolio_v2.yaml"
    if path.as_posix().split("/")[-len(PurePosixPath(required).parts) :] != list(
        PurePosixPath(required).parts
    ):
        raise LF022ExecutionError("reviewed route portfolio must bind portfolio v2")
    try:
        document = load_yaml_mapping(path)
    except ValueError as exc:
        raise LF022ExecutionError(f"invalid reviewed route portfolio: {exc}") from exc
    routes = document.get("routes")
    if not isinstance(routes, list):
        raise LF022ExecutionError("reviewed RCP portfolio lacks routes")
    matching = tuple(
        value
        for value in routes
        if isinstance(value, dict) and value.get("route_id") == route.model_id
    )
    if route.proposer_family_id in {"glm5", "deepseek_v4"}:
        if matching:
            raise LF022ExecutionError(
                "late-added proposer unexpectedly aliases a portfolio-v2 route"
            )
        return
    if len(matching) != 1:
        raise LF022ExecutionError("route is absent or duplicated in reviewed RCP portfolio")
    reviewed_route = matching[0]
    if (
        reviewed_route.get("family_id") != route.proposer_family_id
        or reviewed_route.get("transport") != "rcp_openai_compatible"
        or reviewed_route.get("text_only_path_eligible") is not True
        or reviewed_route.get("public_source_only") is not True
        or reviewed_route.get("reference_hidden_required") is not True
        or reviewed_route.get("trusted_reference_transmission_forbidden") is not True
        or reviewed_route.get("private_source_transmission_forbidden") is not True
        or reviewed_route.get("route_substitution_forbidden") is not True
    ):
        raise LF022ExecutionError("reviewed RCP route scope differs from execution route")


class _LF022RouteEvidenceJudge(StrictModel):
    model: str
    orientations: tuple[str, ...]


class _LF022RouteEvidenceSuccess(StrictModel):
    status: Literal["success"]
    call_count: int
    candidate_validation_status: str
    config_file: str
    config_file_sha256: str = Field(pattern=HEX64_PATTERN)
    config_hash: str = Field(pattern=HEX64_PATTERN)
    proposer: str
    proposer_calls: int
    judge_calls: tuple[_LF022RouteEvidenceJudge, ...]
    manifest: str
    manifest_id: str
    manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    preflight_id: str
    primary_evaluation_judge_calls: int
    replay_method: str
    replay_verified: Literal[True]
    weak_consensus_status: str


class _LF022RouteEvidence(StrictModel):
    """Minimal strict view of the reviewed smoke qualification artifact."""

    schema_version: Literal[1]
    artifact_class: str
    created_at: str
    gate_6_closed: bool
    gate_6g_closed: bool
    live_qualification_status: str
    scientific_outputs: dict[str, int]
    secret_hygiene: dict[str, object]
    source: dict[str, object]
    terminal_attempts: tuple[dict[str, object], ...]
    timestamp_note: dict[str, object]
    verified_success: _LF022RouteEvidenceSuccess


class _LF022QualificationArtifact(StrictModel):
    artifact: str
    sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _safe_path(self) -> Self:
        _safe_relative_path(self.artifact, field="qualification artifact")
        return self


class _LF022QualificationPortfolio(_LF022QualificationArtifact):
    decoding_contract_id: Literal["qwen3_5_thinking_code_v2"]


class _LF022TransportEvidence(_LF022QualificationArtifact):
    observed_role: Literal["judge"]
    proposer_evidence: Literal[False]


class _LF022WireCapabilityPolicy(StrictModel):
    """Exact optional thinking fields accepted by a bound prior RCP call."""

    status: Literal["exact_fields_accepted_by_bound_prior_rcp_call"]
    reasoning_effort_values: tuple[Literal["high"], ...] = Field(min_length=1)
    chat_template_enable_thinking_values: tuple[Literal[True], ...] = Field(min_length=1)
    unsupported_reasoning_fields: Literal["reject"]
    evidence: _LF022TransportEvidence
    accepted_call_label: str = Field(min_length=1)
    accepted_evidence_status: Literal[
        "parsed_success_manifest",
        "terminal_parse_failure_after_http_success",
    ] = "parsed_success_manifest"
    accepted_wire_request: _LF022QualificationArtifact
    accepted_wire_response: _LF022QualificationArtifact | None = None
    accepted_failure_manifest: _LF022QualificationArtifact | None = None

    @model_validator(mode="after")
    def _exact_values(self) -> Self:
        if self.reasoning_effort_values != ("high",):
            raise ValueError("only explicitly accepted reasoning_effort='high' is supported")
        if self.chat_template_enable_thinking_values != (True,):
            raise ValueError("only explicitly accepted enable_thinking=true is supported")
        failure_evidence = self.accepted_evidence_status == (
            "terminal_parse_failure_after_http_success"
        )
        has_response = self.accepted_wire_response is not None
        has_failure = self.accepted_failure_manifest is not None
        if (failure_evidence and not (has_response and has_failure)) or (
            not failure_evidence and (has_response or has_failure)
        ):
            raise ValueError(
                "terminal parse-failure capability requires exact response and failure manifest"
            )
        return self


class _LF022ProposerQualificationDecoding(StrictModel):
    temperature: float
    top_p: float
    top_k: int | None
    min_p: float | None
    presence_penalty: float | None
    repetition_penalty: float | None
    max_tokens: int
    seed: int
    stream: Literal[False]
    thinking_mode: Literal["enabled"]
    reasoning_effort: Literal["high"]
    chat_template_enable_thinking: Literal[True]
    chat_template_thinking: None = None
    thinking_fields_forbidden: Literal[False]

    def route_payload(self, *, contract_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract_id": contract_id,
            **self.model_dump(mode="json"),
        }


class _LF022ProposerQualificationContract(StrictModel):
    schema_version: Literal[1]
    artifact_class: Literal["proposer_qualification_contract"]
    contract_id: Literal[
        "qwen3_5_proposer_qualification_v1",
        "qwen3_5_proposer_qualification_v2",
        "glm5_2_proposer_qualification_v1",
        "glm5_2_proposer_qualification_v2",
        "deepseek_v4_proposer_qualification_v1",
    ]
    role: Literal["proposer"]
    qualification_status: Literal["pending_one_item_live_qualification"]
    model_id: str
    family_id: str
    canonical_family: str
    provider: Literal["epfl_rcp"]
    transport: Literal["rcp_openai_compatible"]
    execution_scope: Literal["one_item_proposer_qualification_only"]
    proposal_count: Literal[1]
    decoding: _LF022ProposerQualificationDecoding
    portfolio_source: _LF022QualificationPortfolio | None
    prior_transport_evidence: _LF022TransportEvidence
    wire_capability_policy: _LF022WireCapabilityPolicy


def verify_lf022_execution_admission(
    *,
    repo_root: Path,
    admission: LF022GOpenExecutionAdmission,
) -> VerifiedLF022ExecutionAdmission:
    """Hash-check all bindings and verify the selected route and allocation plan."""

    paths = {
        name: _bound_path(repo_root=repo_root, binding=binding, label=name)
        for name, binding in (
            ("public_pool_audit", admission.artifacts.public_pool_audit),
            ("allocation_plan", admission.artifacts.allocation_plan),
            ("provider_catalog_raw", admission.artifacts.provider_catalog_raw),
            (
                "provider_catalog_normalized",
                admission.artifacts.provider_catalog_normalized,
            ),
            ("reviewed_route_portfolio", admission.artifacts.reviewed_route_portfolio),
            ("reviewed_route_contract", admission.artifacts.reviewed_route_contract),
            ("reviewed_route_evidence", admission.artifacts.reviewed_route_evidence),
            ("prompt_template", admission.artifacts.prompt_template),
            ("code_bundle", admission.artifacts.code_bundle),
        )
    }
    if admission.artifacts.proposer_production_eligibility is not None:
        paths["proposer_production_eligibility"] = _bound_path(
            repo_root=repo_root,
            binding=admission.artifacts.proposer_production_eligibility,
            label="proposer_production_eligibility",
        )
    if admission.artifacts.qualification_supersession is not None:
        paths["qualification_supersession"] = _bound_path(
            repo_root=repo_root,
            binding=admission.artifacts.qualification_supersession,
            label="qualification_supersession",
        )
    plan = _load_strict_json(
        paths["allocation_plan"],
        LF022ProductionPlanManifest,
        label="allocation_plan",
    )
    assert isinstance(plan, LF022ProductionPlanManifest)
    if plan.manifest_id != admission.allocation_plan_id:
        raise LF022ExecutionError("allocation-plan ID differs from admission")
    audit = _load_strict_json(
        paths["public_pool_audit"],
        LF022PublicPoolAudit,
        label="public_pool_audit",
    )
    assert isinstance(audit, LF022PublicPoolAudit)
    if audit.audit_id != admission.public_pool_audit_id:
        raise LF022ExecutionError("public-pool audit ID differs from admission")
    if audit.outputs.production_plan != admission.artifacts.allocation_plan:
        raise LF022ExecutionError("public-pool audit does not bind the admitted allocation plan")
    if not audit.public_sources_only or not audit.private_sft_classic_forbidden:
        raise LF022ExecutionError("public-pool audit is not public-only")
    if (
        audit.profile != plan.profile
        or audit.selected_count != plan.unique_source_count
        or len(plan.tasks) != 2 * plan.unique_source_count
    ):
        raise LF022ExecutionError(
            "public-pool audit profile or selected-source count differs from allocation plan"
        )
    if admission.route.execution_scope == "one_item_proposer_qualification_only" and (
        plan.profile != "diagnostic_scaffold" or audit.selected_count != 1 or len(plan.tasks) != 2
    ):
        raise LF022ExecutionError(
            "unqualified proposer routes are restricted to one-item qualification"
        )
    if (
        audit.outputs.family_matrix != plan.artifacts.family_matrix
        or audit.outputs.production_plan != admission.artifacts.allocation_plan
        or audit.outputs.source_pool != plan.artifacts.source_pool
        or audit.outputs.theorem_records != plan.artifacts.theorem_records
        or audit.outputs.representation_records != plan.artifacts.representation_records
        or audit.outputs.context_records != plan.artifacts.context_records
        or audit.outputs.denylist_clearance_records != plan.artifacts.denylist_clearance_records
        or audit.outputs.benchmark_registry_manifest != plan.artifacts.benchmark_registry_manifest
        or audit.active_benchmark_registry != plan.artifacts.active_benchmark_registry
    ):
        raise LF022ExecutionError("public-pool audit and allocation-plan artifacts differ")
    reviewed_prompt_path, reviewed_prompt_sha256 = lf022_reviewed_proposer_prompt(
        admission.prompt_template_version
    )
    if (
        admission.artifacts.prompt_template.path != reviewed_prompt_path
        or admission.artifacts.prompt_template.sha256 != reviewed_prompt_sha256
    ):
        raise LF022ExecutionError("prompt template differs from the exact reviewed proposer prompt")
    if admission.artifacts.prompt_template.sha256 != hash_file(paths["prompt_template"]):
        raise LF022ExecutionError("prompt-template binding drifted")

    family_matrix_path = _bound_path(
        repo_root=repo_root,
        binding=plan.artifacts.family_matrix,
        label="production family matrix",
    )
    family_matrix = _load_strict_json(
        family_matrix_path,
        LF022ProductionFamilyMatrix,
        label="production family matrix",
    )
    assert isinstance(family_matrix, LF022ProductionFamilyMatrix)
    pins = tuple(
        pin
        for pin in family_matrix.family_registry
        if pin.family_id == admission.route.proposer_family_id
    )
    if len(pins) != 1:
        raise LF022ExecutionError("route proposer family lacks one exact family-matrix pin")
    pin = pins[0]
    route = admission.route
    expected_qualified_production_contract = {
        "qwen3": "qwen3_5_proposer_qualification_v2",
        "glm5": "glm5_2_proposer_qualification_v2",
        "deepseek_v4": "deepseek_v4_proposer_qualification_v1",
    }.get(route.proposer_family_id)
    if route.decoding.contract_id == "kimi_k2_7_public_proposer_v4":
        expected_qualified_production_contract = "kimi_k2_7_public_proposer_v4"
    if (
        expected_qualified_production_contract is not None
        and route.execution_scope == "public_provisional_g_open"
        and route.decoding.contract_id != expected_qualified_production_contract
    ):
        raise LF022ExecutionError("production requires its exact replay-qualified contract")
    if (
        pin.model_id != route.model_id
        or pin.canonical_family != route.canonical_family
        or pin.provider_id != route.provider_id
        or pin.provider_deployment_id != route.deployment_id
        or pin.underlying_checkpoint_revision_status != route.underlying_checkpoint_revision_status
        or pin.provider_catalog_artifact != admission.artifacts.provider_catalog_normalized
        or route.proposer_family_id not in family_matrix.proposer_family_ids
    ):
        raise LF022ExecutionError("route identity differs from exact family-matrix pin")

    catalog = _load_strict_json(
        paths["provider_catalog_normalized"],
        LF022ProviderCatalogSnapshot,
        label="provider_catalog_normalized",
    )
    assert isinstance(catalog, LF022ProviderCatalogSnapshot)
    if catalog.snapshot_id != route.catalog_snapshot_id:
        raise LF022ExecutionError("catalog snapshot ID differs from route binding")
    if catalog.provider_id != route.provider_id:
        raise LF022ExecutionError("catalog provider differs from route binding")
    raw_catalog_ids = _load_raw_rcp_catalog_ids(paths["provider_catalog_raw"])
    normalized_ids = {
        identifier
        for deployment in catalog.deployments
        for identifier in (deployment.model_id, deployment.deployment_id)
    }
    missing_raw_ids = sorted(normalized_ids - raw_catalog_ids)
    if missing_raw_ids:
        raise LF022ExecutionError(
            "normalized provider catalog contains IDs absent from raw /models response: "
            + ", ".join(missing_raw_ids)
        )
    deployments = {(item.model_id, item.deployment_id) for item in catalog.deployments}
    if (route.model_id, route.deployment_id) not in deployments:
        raise LF022ExecutionError("selected route is absent from normalized catalog")
    _verify_reviewed_v2_portfolio(
        path=paths["reviewed_route_portfolio"],
        route=route,
    )
    _verify_reviewed_route_contract(
        repo_root=repo_root,
        portfolio_path=paths["reviewed_route_portfolio"],
        contract_path=paths["reviewed_route_contract"],
        evidence_path=paths["reviewed_route_evidence"],
        route=route,
    )
    if admission.artifacts.qualification_supersession is not None:
        from leanfaith.generation.lf022_route_qualification import (
            LF022RouteQualificationError,
            verify_lf022_qualification_supersession,
        )

        try:
            supersession = verify_lf022_qualification_supersession(
                repo_root=repo_root,
                supersession_binding=admission.artifacts.qualification_supersession,
            )
        except LF022RouteQualificationError as exc:
            raise LF022ExecutionError(f"qualification supersession rejected: {exc}") from exc
        if (
            supersession.proposer_family_id != route.proposer_family_id
            or supersession.model_id != route.model_id
            or supersession.next_decoding_contract_id != route.decoding.contract_id
        ):
            raise LF022ExecutionError(
                "qualification supersession belongs to a different recovery route"
            )
    if route.proposer_family_id == "moonshot_kimi_k2" and (
        route.execution_scope == "public_provisional_g_open"
        and route.decoding.contract_id == "kimi_k2_7_public_proposer_v4"
    ):
        from leanfaith.generation.lf022_kimi_v4_eligibility import (
            LF022KimiV4EligibilityError,
            verify_lf022_kimi_v4_production_eligibility,
        )

        eligibility_binding = admission.artifacts.proposer_production_eligibility
        assert eligibility_binding is not None
        try:
            kimi_eligibility = verify_lf022_kimi_v4_production_eligibility(
                repo_root=repo_root,
                eligibility_binding=eligibility_binding,
            )
        except LF022KimiV4EligibilityError as exc:
            raise LF022ExecutionError(f"Kimi-v4 production eligibility rejected: {exc}") from exc
        if (
            kimi_eligibility.proposer_family_id != route.proposer_family_id
            or kimi_eligibility.model_id != route.model_id
            or kimi_eligibility.deployment_id != route.deployment_id
            or kimi_eligibility.canonical_family != route.canonical_family
            or kimi_eligibility.provider_id != route.provider_id
            or kimi_eligibility.catalog_snapshot_id != route.catalog_snapshot_id
            or kimi_eligibility.route_snapshot_revision != route.route_snapshot_revision
            or kimi_eligibility.decoding_contract_id != route.decoding.contract_id
            or kimi_eligibility.decoding_contract_hash
            != hash_canonical(route.decoding.model_dump(mode="json"))
            or kimi_eligibility.v4_contract != admission.artifacts.reviewed_route_contract
            or kimi_eligibility.v4_prompt != admission.artifacts.prompt_template
            or kimi_eligibility.family_matrix != plan.artifacts.family_matrix
            or kimi_eligibility.family_matrix_id != family_matrix.matrix_id
        ):
            raise LF022ExecutionError(
                "Kimi-v4 production eligibility belongs to a different route or matrix"
            )
    elif (
        route.proposer_family_id in {"qwen3", "glm5", "deepseek_v4"}
        and route.execution_scope == "public_provisional_g_open"
    ):
        from leanfaith.generation.lf022_route_qualification import (
            LF022RouteQualificationError,
            verify_lf022_proposer_production_eligibility,
        )

        eligibility_binding = admission.artifacts.proposer_production_eligibility
        assert eligibility_binding is not None
        try:
            eligibility = verify_lf022_proposer_production_eligibility(
                repo_root=repo_root,
                eligibility_binding=eligibility_binding,
            )
        except LF022RouteQualificationError as exc:
            raise LF022ExecutionError(f"proposer production eligibility rejected: {exc}") from exc
        if (
            eligibility.proposer_family_id != route.proposer_family_id
            or eligibility.model_id != route.model_id
            or eligibility.deployment_id != route.deployment_id
            or eligibility.canonical_family != route.canonical_family
            or eligibility.provider_id != route.provider_id
            or eligibility.catalog_snapshot_id != route.catalog_snapshot_id
            or eligibility.route_snapshot_revision != route.route_snapshot_revision
            or eligibility.decoding_contract_id != route.decoding.contract_id
            or eligibility.decoding_contract_hash
            != hash_canonical(route.decoding.model_dump(mode="json"))
            or eligibility.qualification_contract != admission.artifacts.reviewed_route_contract
            or eligibility.family_matrix != plan.artifacts.family_matrix
            or eligibility.family_matrix_id != family_matrix.matrix_id
        ):
            raise LF022ExecutionError(
                "proposer production eligibility belongs to a different route or matrix"
            )
    try:
        validate_code_bundle(paths["code_bundle"], admission.code_tree_hash)
    except (OSError, ValueError) as exc:
        raise LF022ExecutionError(f"code-bundle validation failed: {exc}") from exc
    return VerifiedLF022ExecutionAdmission(
        admission_id=admission.admission_id,
        plan=plan,
        audit=audit,
        family_matrix=family_matrix,
    )


def load_lf022_execution_task_inputs(
    *,
    repo_root: Path,
    verified: VerifiedLF022ExecutionAdmission,
) -> VerifiedLF022ExecutionTaskInputs:
    """Load every exact public task artifact once for bounded batch reuse."""

    audit = verified.audit
    source_records = _load_bound_jsonl(
        repo_root=repo_root,
        binding=audit.outputs.source_pool,
        model=LF022ProductionSourceRecord,
        label="public source pool",
    )
    theorems = _load_bound_jsonl(
        repo_root=repo_root,
        binding=audit.outputs.theorem_records,
        model=TheoremRecord,
        label="public theorem records",
    )
    representations = _load_bound_jsonl(
        repo_root=repo_root,
        binding=audit.outputs.representation_records,
        model=RepresentationRecord,
        label="public representation records",
    )
    contexts = _load_bound_jsonl(
        repo_root=repo_root,
        binding=audit.outputs.context_records,
        model=ContextRecord,
        label="public context records",
    )
    clearances = _load_bound_jsonl(
        repo_root=repo_root,
        binding=audit.outputs.denylist_clearance_records,
        model=LF022DenylistClearanceRecord,
        label="denylist clearance records",
    )
    benchmark_manifest_path = _bound_path(
        repo_root=repo_root,
        binding=audit.outputs.benchmark_registry_manifest,
        label="benchmark registry manifest",
    )
    benchmark_manifest = _load_strict_json(
        benchmark_manifest_path,
        LF022BenchmarkRegistryManifest,
        label="benchmark registry manifest",
    )
    assert isinstance(benchmark_manifest, LF022BenchmarkRegistryManifest)
    if benchmark_manifest.active_registry != audit.active_benchmark_registry:
        raise LF022ExecutionError("benchmark registry manifest differs from public-pool audit")
    active_registry_path = _bound_path(
        repo_root=repo_root,
        binding=audit.active_benchmark_registry,
        label="active benchmark registry",
    )
    active_registry = _load_strict_json(
        active_registry_path,
        FrozenRegistry,
        label="active benchmark registry",
    )
    assert isinstance(active_registry, FrozenRegistry)
    active_registry_content_hash = DenylistIndex(active_registry).registry_content_hash
    if active_registry_content_hash != audit.active_benchmark_registry_content_hash:
        raise LF022ExecutionError("active benchmark registry content hash differs")
    registry_path = _bound_path(
        repo_root=repo_root,
        binding=audit.outputs.public_source_authorization_registry,
        label="public source authorization registry",
    )
    registry = _load_strict_json(
        registry_path,
        LF022PublicSourceAuthorizationRegistry,
        label="public source authorization registry",
    )
    assert isinstance(registry, LF022PublicSourceAuthorizationRegistry)
    return VerifiedLF022ExecutionTaskInputs(
        source_records=source_records,
        theorems=theorems,
        representations=representations,
        contexts=contexts,
        clearances=clearances,
        benchmark_manifest=benchmark_manifest,
        active_registry=active_registry,
        authorization_registry=registry,
        allocation_tasks_by_id=_unique_execution_index(
            verified.plan.tasks,
            attribute="task_id",
            label="allocation plan",
        ),
        source_records_by_admission_id=_unique_execution_index(
            source_records,
            attribute="admission_record_id",
            label="public source pool",
        ),
        theorems_by_id=_unique_execution_index(
            theorems,
            attribute="theorem_id",
            label="public theorem records",
        ),
        representations_by_id=_unique_execution_index(
            representations,
            attribute="representation_id",
            label="public representation records",
        ),
        contexts_by_id=_unique_execution_index(
            contexts,
            attribute="context_id",
            label="public context records",
        ),
        clearances_by_id=_unique_execution_index(
            clearances,
            attribute="clearance_id",
            label="denylist clearance records",
        ),
        authorizations_by_id=_unique_execution_index(
            registry.authorizations,
            attribute="authorization_id",
            label="public source authorization registry",
        ),
        active_registry_content_hash=active_registry_content_hash,
    )


def verify_lf022_execution_task(
    *,
    repo_root: Path,
    admission: LF022GOpenExecutionAdmission,
    verified: VerifiedLF022ExecutionAdmission,
    task: LF022GOpenExecutionTask,
    inputs: VerifiedLF022ExecutionTaskInputs | None = None,
) -> None:
    """Require exact task membership and route/source agreement before prompting."""

    if (
        task.execution_admission_id != admission.admission_id
        or task.allocation_plan_id != admission.allocation_plan_id
        or task.normalization_version != admission.normalization_version
    ):
        raise LF022ExecutionError("execution task differs from its admission")
    if task.allocation_task.proposer_family_id != admission.route.proposer_family_id:
        raise LF022ExecutionError("task proposer family differs from admitted route")
    if (
        admission.route.execution_scope == "one_item_proposer_qualification_only"
        and task.proposal_count != 1
    ):
        raise LF022ExecutionError(
            "proposer qualification route task must request exactly one proposal"
        )
    audit = verified.audit
    task_inputs = inputs or load_lf022_execution_task_inputs(
        repo_root=repo_root,
        verified=verified,
    )
    allocation = task.allocation_task
    if task_inputs.allocation_tasks_by_id.get(allocation.task_id) != allocation:
        raise LF022ExecutionError("allocation task is absent or differs from the bound plan")
    source_record = task_inputs.source_records_by_admission_id.get(allocation.admission_record_id)
    theorem = task_inputs.theorems_by_id.get(allocation.theorem_id)
    representation = task_inputs.representations_by_id.get(allocation.representation_id)
    context = task_inputs.contexts_by_id.get(allocation.context_id)
    if source_record is None or theorem is None or representation is None or context is None:
        raise LF022ExecutionError(
            "allocation source, theorem, representation, or context is absent"
        )
    clearance = task_inputs.clearances_by_id.get(source_record.denylist_clearance_id)
    if clearance is None:
        raise LF022ExecutionError("allocation denylist clearance is absent")
    if (
        source_record.theorem_id != theorem.theorem_id
        or source_record.representation_id != representation.representation_id
        or source_record.context_id != context.context_id
        or source_record.source != theorem.source
        or source_record.source_revision != theorem.source_revision
        or source_record.theorem_statement_content_hash != theorem.statement_content_hash
        or source_record.representation_content_hash != representation.content_hash
        or source_record.context_fingerprint != context.context_fingerprint
        or source_record.context_header_hash != context.header_hash
        or representation.theorem_id != theorem.theorem_id
        or representation.context_id != context.context_id
    ):
        raise LF022ExecutionError("public source/theorem/representation/context linkage differs")
    if (
        source_record.normalization_version != admission.normalization_version
        or representation.normalization_version != admission.normalization_version
    ):
        raise LF022ExecutionError("execution source is not represented under repr_v3")
    benchmark_manifest = task_inputs.benchmark_manifest
    if benchmark_manifest.active_registry != audit.active_benchmark_registry:
        raise LF022ExecutionError("benchmark registry manifest differs from public-pool audit")
    if task_inputs.active_registry_content_hash != audit.active_benchmark_registry_content_hash:
        raise LF022ExecutionError("active benchmark registry content hash differs")
    expected_clearance = (
        source_record.denylist_clearance_id,
        benchmark_manifest.manifest_id,
        audit.active_benchmark_registry.sha256,
        audit.active_benchmark_registry_content_hash,
        source_record.source_locator_id,
        source_record.theorem_id,
        source_record.theorem_statement_content_hash,
        source_record.representation_id,
        source_record.representation_content_hash,
    )
    observed_clearance = (
        clearance.clearance_id,
        clearance.benchmark_manifest_id,
        clearance.active_registry_file_sha256,
        clearance.active_registry_content_hash,
        clearance.source_locator_id,
        clearance.theorem_id,
        clearance.theorem_statement_content_hash,
        clearance.representation_id,
        clearance.representation_content_hash,
    )
    if (
        observed_clearance != expected_clearance
        or not clearance.all_identifier_and_content_screens_executed
        or not clearance.clear
    ):
        raise LF022ExecutionError(
            "denylist clearance does not exactly and clearly bind the execution source"
        )
    source = task.source
    expected_source_statement = (
        theorem.proof_stripped_declaration
        if task.source_statement_version is None
        else make_lf022_named_signature(
            theorem=theorem,
            representation=representation,
        )
    )
    if (
        source.source_statement != expected_source_statement
        or source.imports != context.imports
        or source.source_id != theorem.source
        or source.source_revision != theorem.source_revision
    ):
        raise LF022ExecutionError("prompt source content differs from the bound public pool")

    authorization = task_inputs.authorizations_by_id.get(
        source_record.public_source_authorization_id
    )
    if authorization is None:
        raise LF022ExecutionError("public source lacks one exact authorization")
    if (
        authorization.source != theorem.source
        or authorization.source_revision != theorem.source_revision
        or source.source_license != authorization.license_id
        or not authorization.source_is_public
        or not authorization.external_transmission_allowed
    ):
        raise LF022ExecutionError("task source license/transmission policy differs")


__all__ = [
    "LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT",
    "LF022_REVIEWED_PROPOSER_PROMPT_PATH",
    "LF022_REVIEWED_PROPOSER_PROMPT_SHA256",
    "LF022_REVIEWED_PROPOSER_PROMPT_V2_PATH",
    "LF022_REVIEWED_PROPOSER_PROMPT_V2_SHA256",
    "LF022ExecutionArtifacts",
    "LF022ExecutionError",
    "LF022GOpenExecutionAdmission",
    "LF022GOpenExecutionTask",
    "LF022QualificationClaim",
    "LF022QualificationSupersession",
    "LF022RCPDecodingContract",
    "LF022RCPRetryPolicy",
    "LF022RCPRouteBinding",
    "VerifiedLF022ExecutionAdmission",
    "VerifiedLF022ExecutionTaskInputs",
    "lf022_qualification_claim_path",
    "lf022_reviewed_proposer_prompt",
    "load_lf022_execution_task_inputs",
    "make_lf022_g_open_execution_admission",
    "make_lf022_g_open_execution_task",
    "make_lf022_named_signature",
    "make_lf022_qualification_claim",
    "verify_lf022_execution_admission",
    "verify_lf022_execution_task",
]
