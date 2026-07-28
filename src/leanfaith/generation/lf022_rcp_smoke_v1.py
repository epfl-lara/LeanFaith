"""Public-only, one-item LF-022 RCP smoke admission and runner.

This module is intentionally separate from the LF-021 provider qualification
and from the fail-disabled LF-022 foundation.  Loading or preflighting it can
perform at most one ``GET /models`` request and never performs inference.
Live execution requires an explicit caller flag and is capped at one proposer
call plus two blinded orientations for each of two distinct judge families.

The smoke is operational evidence only.  It creates no semantic label, silver
record, training/evaluation eligibility, or Gate credit.  Every exact wire
request is persisted before transport and every exact wire response is
persisted before JSON or task-specific parsing.  Only
``choices[0].message.content`` is passed to LF-022 parsers; hidden reasoning is
never interpreted as task output.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation.llm_variants import (
    PROPOSER_TEMPLATE_ID,
    PROPOSER_TEMPLATE_VERSION,
    PublicLeanVariantSource,
    RenderedVariantPrompt,
    VariantPromptRequest,
    materialize_verified_provisional_variants,
    parse_variant_proposer_output,
    render_variant_proposer_prompt,
    variant_provider_input_ids,
)
from leanfaith.generation.providers import (
    DecodingValue,
    ProviderError,
    ProviderIdentity,
    ProviderRawResponse,
    ProviderRequest,
    bridge_provider_result_to_generic_llm_lineage,
    load_provider_request,
    persist_provider_raw_response,
    persist_provider_request,
    verify_generic_llm_call_artifacts,
)
from leanfaith.generation.rcp_qualification_v1 import (
    RCPCredentials,
    RCPHTTPTransport,
    RCPTransportError,
)
from leanfaith.generation.weak_supervision import (
    JUDGE_TEMPLATE_ID,
    JUDGE_TEMPLATE_VERSION,
    FamilySeparationMatrix,
    JudgeOrientation,
    JudgePresentation,
    JudgeSlot,
    PublicLeanJudgePair,
    RenderedJudgePrompt,
    build_weak_consensus_candidate,
    judge_provider_input_ids,
    make_swapped_presentations,
    materialize_verified_judgment_evidence,
    parse_blinded_judge_output,
    render_blinded_judge_prompt,
    validate_family_separation,
)
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanStatus
from leanfaith.schemas.enums import (
    IntendedRelation,
    LLMCallStatus,
    LLMRole,
    ParseStatus,
    ValidationStatus,
)
from leanfaith.schemas.evidence import EvidenceRecord
from leanfaith.schemas.ids import PAIR_PREFIX, make_id
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import VariantRecord
from leanfaith.schemas.weak_supervision import (
    WeakConsensusCandidateRecord,
    WeakConsensusStatus,
)

_HEX64 = r"^[0-9a-f]{64}$"
_DECLARATION_NAME = re.compile(r"^(?:theorem|lemma)\s+([^\s:({\[]+)")
_PRIVATE_SOURCE_MARKERS = ("formalmathatepfl/sft_classic", "sft_classic")


class LF022RCPSmokeError(RuntimeError):
    """The smoke admission or execution failed closed."""


class LF022RCPSmokeConfigError(LF022RCPSmokeError):
    """A frozen config/source/prompt binding drifted."""


class LF022RCPSmokeCredentialError(LF022RCPSmokeError):
    """The exact RCP credential environment is absent or unsafe."""


class LF022RCPSmokeCatalogError(LF022RCPSmokeError):
    """The live catalog lacks one exact admitted RCP route."""


class LF022RCPSmokeArtifactConflict(LF022RCPSmokeError):
    """An immutable smoke artifact already contains different bytes."""


class BoundArtifact(StrictModel):
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _relative(self) -> Self:
        path = PurePosixPath(self.artifact)
        if path.is_absolute() or ".." in path.parts or "\\" in self.artifact:
            raise ValueError("artifact must be a safe repository-relative POSIX path")
        return self


class SmokeTransportConfig(StrictModel):
    transport_id: Literal["epfl_rcp_openai_compatible_v1"]
    base_url_env: Literal["RCP_BASE_URL"]
    api_key_env: Literal["RCP_API_KEY"]
    expected_base_url: Literal["https://inference.rcp.epfl.ch/v1"]
    catalog_path: Literal["/models"]
    chat_completions_path: Literal["/chat/completions"]
    catalog_timeout_seconds: int = Field(ge=1, le=300)
    request_timeout_seconds: int = Field(ge=1, le=3600)


class SmokeSourceConfig(StrictModel):
    problem_records: BoundArtifact
    reference_theorems: BoundArtifact
    reference_representations: BoundArtifact
    import_header: BoundArtifact
    expected_problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    expected_theorem_id: str = Field(pattern=r"^thm:[0-9a-f]{64}$")
    expected_representation_id: str = Field(pattern=r"^repr:[0-9a-f]{64}$")
    expected_source: Literal["mathlib_post_formalrx_docstrings_v1"]
    expected_source_license: Literal["Apache-2.0"]


class SmokePromptBinding(StrictModel):
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    template_id: str = Field(min_length=1)
    template_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _relative(self) -> Self:
        path = PurePosixPath(self.artifact)
        if path.is_absolute() or ".." in path.parts or "\\" in self.artifact:
            raise ValueError("prompt artifact must be repository-relative")
        return self


class SmokePromptConfig(StrictModel):
    proposer: SmokePromptBinding
    judge: SmokePromptBinding


class SmokeDecodingConfig(StrictModel):
    temperature: float = Field(ge=0.0, le=2.0)
    top_p: float = Field(gt=0.0, le=1.0)
    max_tokens: int = Field(ge=1, le=65536)
    seed: int = Field(ge=0)
    stream: Literal[False]
    reasoning_effort: Literal["high"]
    chat_template_enable_thinking: Literal[True]

    def provider_decoding(self) -> dict[str, DecodingValue]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stream": self.stream,
            "reasoning_effort": self.reasoning_effort,
            "chat_template_enable_thinking": self.chat_template_enable_thinking,
        }

    def wire_fields(self) -> dict[str, object]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stream": self.stream,
            "reasoning_effort": self.reasoning_effort,
            "chat_template_kwargs": {
                "enable_thinking": self.chat_template_enable_thinking,
            },
        }


class SmokeProviderConfig(StrictModel):
    provider_slot: str = Field(min_length=1)
    provider: Literal["epfl_rcp", "openai_codex"]
    model_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    role: Literal["proposer", "judge", "primary_eval_judge"]
    transport: Literal["rcp_openai_compatible", "codex_exec"]
    enabled_for_this_smoke: bool
    decoding: SmokeDecodingConfig | None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.enabled_for_this_smoke:
            if self.transport != "rcp_openai_compatible" or self.decoding is None:
                raise ValueError("enabled smoke providers require RCP decoding")
        elif self.role != "primary_eval_judge" or self.decoding is not None:
            raise ValueError("only the held-out primary evaluator may be disabled")
        return self


class SmokeProviderRegistry(StrictModel):
    proposer: SmokeProviderConfig
    judge_A: SmokeProviderConfig
    judge_B: SmokeProviderConfig
    primary_eval_judge: SmokeProviderConfig

    @model_validator(mode="after")
    def _roles_and_families(self) -> Self:
        expected = {
            "proposer": (self.proposer, "proposer", True),
            "judge_A": (self.judge_A, "judge", True),
            "judge_B": (self.judge_B, "judge", True),
            "primary_eval_judge": (
                self.primary_eval_judge,
                "primary_eval_judge",
                False,
            ),
        }
        for name, (provider, role, enabled) in expected.items():
            if provider.role != role or provider.enabled_for_this_smoke is not enabled:
                raise ValueError(f"{name} has an inconsistent role/enabled state")
        validate_family_separation(self.family_matrix())
        return self

    def family_matrix(self) -> FamilySeparationMatrix:
        return FamilySeparationMatrix(
            proposer_family=self.proposer.family_id,
            judge_a_family=self.judge_A.family_id,
            judge_b_family=self.judge_B.family_id,
            primary_eval_judge_family=self.primary_eval_judge.family_id,
        )

    def enabled_rcp(self) -> tuple[SmokeProviderConfig, ...]:
        return (self.proposer, self.judge_A, self.judge_B)


class SmokeGenerationConfig(StrictModel):
    distribution: Literal["G_open"]
    proposal_count: Literal[1]
    requested_relations: tuple[IntendedRelation, ...]
    requested_error_types: tuple[()] = ()
    requested_sci_categories: tuple[()] = ()

    @model_validator(mode="after")
    def _exact_open_request(self) -> Self:
        if self.requested_relations != (IntendedRelation.NEAR_MISS,):
            raise ValueError("public smoke requests exactly the near_miss intention")
        return self


class SmokeBudgetConfig(StrictModel):
    catalog_get_max: Literal[1]
    chat_completion_max: Literal[5]
    proposer_calls: Literal[1]
    judge_calls: Literal[4]
    attempts_per_call: Literal[1]


class SmokeOutputConfig(StrictModel):
    raw_root: str
    preflight_root: str

    @model_validator(mode="after")
    def _relative(self) -> Self:
        for value in (self.raw_root, self.preflight_root):
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError("output roots must be repository-relative")
        return self


class SmokePolicyConfig(StrictModel):
    public_source_only: Literal[True]
    private_source_transmission_forbidden: Literal[True]
    sft_classic_transmission_forbidden: Literal[True]
    denylist_clearance_required: Literal[True]
    g_open_only: Literal[True]
    sci_conditioning_performed: Literal[False]
    semantic_labels_created: Literal[False]
    silver_records_created: Literal[False]
    training_eligible: Literal[False]
    evaluation_eligible: Literal[False]
    supervision_eligible: Literal[False]
    gate_credit_claimed: Literal[False]
    primary_eval_judge_called: Literal[False]
    execute_requires_explicit_flag: Literal[True]


class LF022RCPSmokeConfig(StrictModel):
    schema_version: Literal[1]
    config_id: Literal[
        "lf022_rcp_public_smoke_v1",
        "lf022_rcp_public_smoke_v2",
        "lf022_rcp_public_smoke_v3",
    ]
    frozen_at: datetime.datetime
    status: Literal["executable_one_public_item_only"]
    artifact_class: Literal["smoke"]
    transport: SmokeTransportConfig
    source: SmokeSourceConfig
    prompts: SmokePromptConfig
    providers: SmokeProviderRegistry
    generation: SmokeGenerationConfig
    budget: SmokeBudgetConfig
    outputs: SmokeOutputConfig
    policy: SmokePolicyConfig

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.frozen_at.tzinfo is None or self.frozen_at.utcoffset() != datetime.timedelta(0):
            raise ValueError("frozen_at must be UTC")
        if (
            self.prompts.proposer.template_id != PROPOSER_TEMPLATE_ID
            or self.prompts.proposer.template_version != PROPOSER_TEMPLATE_VERSION
            or self.prompts.judge.template_id != JUDGE_TEMPLATE_ID
            or self.prompts.judge.template_version != JUDGE_TEMPLATE_VERSION
        ):
            raise ValueError("prompt IDs/versions differ from executable contracts")
        return self


class SmokeCatalogObservation(StrictModel):
    schema_version: Literal[1] = 1
    endpoint_sha256: str = Field(pattern=_HEX64)
    raw_response_sha256: str = Field(pattern=_HEX64)
    canonical_model_ids_sha256: str = Field(pattern=_HEX64)
    model_count: int = Field(ge=1)
    required_model_ids: tuple[str, str, str]
    required_models_present: tuple[Literal[True], Literal[True], Literal[True]]
    checkpoint_revision_status: Literal["unavailable_from_rcp_route_ids"]
    route_snapshot_revision: str = Field(pattern=r"^rcp-catalog-sha256:[0-9a-f]{64}$")
    immutable_weight_revision_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _bound_snapshot(self) -> Self:
        if self.route_snapshot_revision != f"rcp-catalog-sha256:{self.raw_response_sha256}":
            raise ValueError("catalog route snapshot must bind the exact raw response")
        if len(set(self.required_model_ids)) != 3:
            raise ValueError("catalog smoke routes must be distinct")
        return self


class LF022RCPSmokePreflight(StrictModel):
    schema_version: Literal[1] = 1
    preflight_id: str = Field(pattern=r"^lf022_rcp_preflight:[0-9a-f]{64}$")
    config_hash: str = Field(pattern=_HEX64)
    catalog: SmokeCatalogObservation
    catalog_artifact: str
    problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    theorem_id: str = Field(pattern=r"^thm:[0-9a-f]{64}$")
    representation_id: str = Field(pattern=r"^repr:[0-9a-f]{64}$")
    source_record_id: str = Field(pattern=_HEX64)
    proposer_prompt_sha256: str = Field(pattern=_HEX64)
    family_separation_valid: Literal[True] = True
    source_is_public: Literal[True] = True
    external_transmission_allowed: Literal[True] = True
    denylist_checked: Literal[True] = True
    denylist_hits: tuple[()] = ()
    private_source_content: Literal[False] = False
    sft_classic_content_present: Literal[False] = False
    generation_distribution: Literal["G_open"] = "G_open"
    sci_conditioning_performed: Literal[False] = False
    catalog_requests_performed: Literal[1] = 1
    chat_completion_requests_performed: Literal[0] = 0
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            "schema": "lf022_rcp_public_smoke_preflight_v1",
            "config_hash": self.config_hash,
            "catalog": self.catalog.model_dump(mode="json"),
            "catalog_artifact": self.catalog_artifact,
            "problem_record_id": self.problem_record_id,
            "theorem_id": self.theorem_id,
            "representation_id": self.representation_id,
            "source_record_id": self.source_record_id,
            "proposer_prompt_sha256": self.proposer_prompt_sha256,
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        expected = "lf022_rcp_preflight:" + hash_canonical(self.id_payload())
        if self.preflight_id != expected:
            raise ValueError("preflight_id does not bind the complete preflight")
        return self


class SmokeWireMetadata(StrictModel):
    provider_request_id: str | None = None
    returned_model: str
    usage: dict[str, int]
    reasoning_content_present: bool
    reasoning_content_chars: int = Field(ge=0)
    reasoning_content_sha256: str | None = Field(default=None, pattern=_HEX64)
    reasoning_present: bool
    reasoning_chars: int = Field(ge=0)
    reasoning_sha256: str | None = Field(default=None, pattern=_HEX64)


class SmokeCallArtifact(StrictModel):
    schema_version: Literal[1] = 1
    call_label: str = Field(min_length=1)
    llm_call_id: str = Field(pattern=r"^call:[0-9a-f]{64}$")
    role: Literal["proposer", "judge"]
    provider_slot: str
    model_id: str
    model_family: str
    model_revision: str
    provider_request_hash: str = Field(pattern=_HEX64)
    provider_request_artifact: str
    provider_request_sha256: str = Field(pattern=_HEX64)
    wire_request_artifact: str
    wire_request_sha256: str = Field(pattern=_HEX64)
    wire_response_artifact: str
    wire_response_sha256: str = Field(pattern=_HEX64)
    provider_raw_artifact: str
    provider_raw_sha256: str = Field(pattern=_HEX64)
    llm_call_artifact: str
    llm_call_sha256: str = Field(pattern=_HEX64)
    content_sha256: str = Field(pattern=_HEX64)
    wire: SmokeWireMetadata
    parse_status: Literal["parsed"]
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _safe_paths(self) -> Self:
        for field in (
            "provider_request_artifact",
            "wire_request_artifact",
            "wire_response_artifact",
            "provider_raw_artifact",
            "llm_call_artifact",
        ):
            value = str(getattr(self, field))
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError(f"{field} must be a safe repository-relative path")
        return self


class SmokeLeanValidationRecord(StrictModel):
    schema_version: Literal[1] = 1
    artifact_class: Literal["smoke"] = "smoke"
    request_id: str
    request_hash: str = Field(pattern=_HEX64)
    context_id: str
    code_sha256: str = Field(pattern=_HEX64)
    allow_sorry: Literal[True] = True
    status: Literal["valid", "valid_with_sorry"]
    declarations: tuple[dict[str, object], ...]
    messages: tuple[dict[str, object], ...]
    sorries: tuple[dict[str, object], ...]
    raw_response_artifact: str | None = None
    raw_response_sha256: str | None = Field(default=None, pattern=_HEX64)
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _raw_binding(self) -> Self:
        if (self.raw_response_artifact is None) != (self.raw_response_sha256 is None):
            raise ValueError("Lean raw response path/hash must be present together")
        if self.raw_response_artifact is not None:
            path = PurePosixPath(self.raw_response_artifact)
            if path.is_absolute() or ".." in path.parts or "\\" in self.raw_response_artifact:
                raise ValueError("Lean raw response artifact must be repository-relative")
        return self


class LF022RCPSmokeManifest(StrictModel):
    schema_version: Literal[1] = 1
    artifact_class: Literal["smoke"] = "smoke"
    manifest_id: str = Field(pattern=r"^lf022_rcp_smoke_manifest:[0-9a-f]{64}$")
    config_hash: str = Field(pattern=_HEX64)
    catalog_raw_response_sha256: str = Field(pattern=_HEX64)
    problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    source_theorem_id: str = Field(pattern=r"^thm:[0-9a-f]{64}$")
    source_representation_id: str = Field(pattern=r"^repr:[0-9a-f]{64}$")
    variant_id: str = Field(pattern=r"^var:[0-9a-f]{64}$")
    pair_id: str = Field(pattern=r"^pair:[0-9a-f]{64}$")
    preflight_artifact: BoundArtifact
    catalog_artifact: BoundArtifact
    call_artifacts: tuple[SmokeCallArtifact, ...] = Field(min_length=5, max_length=5)
    variant_artifact: BoundArtifact
    lean_validation_artifact: BoundArtifact
    judgment_evidence_artifacts: tuple[
        BoundArtifact,
        BoundArtifact,
        BoundArtifact,
        BoundArtifact,
    ]
    weak_consensus_artifact: BoundArtifact
    proposer_call_count: Literal[1] = 1
    judge_call_count: Literal[4] = 4
    primary_eval_judge_call_count: Literal[0] = 0
    candidate_validation_status: Literal["elaborates", "elaborates_with_placeholder"]
    weak_consensus_candidate_id: str = Field(pattern=r"^weak_consensus:[0-9a-f]{64}$")
    weak_consensus_status: WeakConsensusStatus
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False


class SmokeFailureArtifact(StrictModel):
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _safe(self) -> Self:
        path = PurePosixPath(self.artifact)
        if path.is_absolute() or ".." in path.parts or "\\" in self.artifact:
            raise ValueError("failure inventory path must be repository-relative")
        return self


class LF022RCPSmokeFailureManifest(StrictModel):
    """Terminal, zero-label record for one non-resumable partial smoke."""

    schema_version: Literal[1] = 1
    artifact_class: Literal["smoke"] = "smoke"
    failure_id: str = Field(pattern=r"^lf022_rcp_smoke_failure:[0-9a-f]{64}$")
    config_hash: str = Field(pattern=_HEX64)
    run_key: str = Field(pattern=_HEX64)
    error_type: str = Field(min_length=1)
    error_message_sha256: str = Field(pattern=_HEX64)
    chat_completion_attempts: int = Field(ge=0, le=5)
    completed_call_count: int = Field(ge=0, le=5)
    artifacts: tuple[SmokeFailureArtifact, ...]
    terminal: Literal[True] = True
    retry_permitted: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _identity(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"failure_id"})
        expected = "lf022_rcp_smoke_failure:" + hash_canonical(payload)
        if self.failure_id != expected:
            raise ValueError("failure_id does not bind the terminal failure")
        return self


@dataclass(frozen=True, slots=True)
class LoadedLF022RCPSmoke:
    loaded_config: LoadedConfig[LF022RCPSmokeConfig]
    problem: ProblemPoolRecord
    theorem: TheoremRecord
    representation: RepresentationRecord
    import_header: str
    proposer_request: VariantPromptRequest
    proposer_prompt: RenderedVariantPrompt


@dataclass(frozen=True, slots=True)
class SmokePreflightRun:
    preflight: LF022RCPSmokePreflight
    preflight_path: Path
    catalog_path: Path


@dataclass(frozen=True, slots=True)
class _WireCallResult:
    artifact: SmokeCallArtifact
    call: LLMCallRecord
    content: str


@dataclass(frozen=True, slots=True)
class SmokeExecutionRun:
    manifest: LF022RCPSmokeManifest
    manifest_path: Path
    variant: VariantRecord


@dataclass(frozen=True, slots=True)
class _FailureCallReplay:
    call_label: str
    call: LLMCallRecord
    request: ProviderRequest
    content: str | None
    parsed: StrictModel | None
    terminal_error_type: str | None


def _safe_repo_path(repo_root: Path, relative: str, *, label: str) -> Path:
    root = repo_root.resolve()
    path = root / relative
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise LF022RCPSmokeConfigError(f"{label} escapes repository root") from exc
    if path.is_symlink() or not path.is_file():
        raise LF022RCPSmokeConfigError(f"{label} is missing or unsafe: {relative}")
    return path


def _verify_bound(repo_root: Path, bound: BoundArtifact, *, label: str) -> Path:
    path = _safe_repo_path(repo_root, bound.artifact, label=label)
    observed = hash_file(path)
    if observed != bound.sha256:
        raise LF022RCPSmokeConfigError(f"{label} SHA-256 drift: {observed} != {bound.sha256}")
    return path


def _verify_prompt(
    repo_root: Path,
    binding: SmokePromptBinding,
    *,
    label: str,
) -> Path:
    path = _safe_repo_path(repo_root, binding.artifact, label=label)
    observed = hash_file(path)
    if observed != binding.sha256:
        raise LF022RCPSmokeConfigError(f"{label} SHA-256 drift: {observed} != {binding.sha256}")
    return path


def _load_one_jsonl(path: Path, model_type: type[Any], *, label: str) -> Any:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise LF022RCPSmokeConfigError(f"{label} must contain exactly one JSONL record")
    try:
        payload = json.loads(lines[0])
        return model_type.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LF022RCPSmokeConfigError(f"{label} is invalid: {exc}") from exc


def _imports_from_header(header: str) -> tuple[str, ...]:
    imports = tuple(
        line.strip().removeprefix("import").strip()
        for line in header.splitlines()
        if line.strip().startswith("import ")
    )
    if not imports or any(not item for item in imports):
        raise LF022RCPSmokeConfigError("import header contains no valid imports")
    return tuple(dict.fromkeys(imports))


def load_lf022_rcp_smoke(
    config_path: Path,
    *,
    repo_root: Path,
) -> LoadedLF022RCPSmoke:
    """Load and hash-verify the exact public one-item smoke admission."""

    loaded = load_config(config_path, LF022RCPSmokeConfig)
    config = loaded.config
    problem_path = _verify_bound(repo_root, config.source.problem_records, label="problem records")
    theorem_path = _verify_bound(
        repo_root, config.source.reference_theorems, label="reference theorems"
    )
    representation_path = _verify_bound(
        repo_root,
        config.source.reference_representations,
        label="reference representations",
    )
    header_path = _verify_bound(repo_root, config.source.import_header, label="import header")
    proposer_path = _verify_prompt(repo_root, config.prompts.proposer, label="proposer prompt")
    _verify_prompt(repo_root, config.prompts.judge, label="judge prompt")

    problem = _load_one_jsonl(problem_path, ProblemPoolRecord, label="problem records")
    theorem = _load_one_jsonl(theorem_path, TheoremRecord, label="reference theorems")
    representation = _load_one_jsonl(
        representation_path,
        RepresentationRecord,
        label="reference representations",
    )
    assert isinstance(problem, ProblemPoolRecord)
    assert isinstance(theorem, TheoremRecord)
    assert isinstance(representation, RepresentationRecord)
    expected = (
        config.source.expected_problem_record_id,
        config.source.expected_theorem_id,
        config.source.expected_representation_id,
    )
    observed = (
        problem.problem_record_id,
        theorem.theorem_id,
        representation.representation_id,
    )
    if observed != expected:
        raise LF022RCPSmokeConfigError(
            f"source IDs differ from exact admission: {observed!r} != {expected!r}"
        )
    if (
        problem.reference_theorem_ids != (theorem.theorem_id,)
        or representation.theorem_id != theorem.theorem_id
        or theorem.context_id != problem.context_id
        or representation.context_id != problem.context_id
    ):
        raise LF022RCPSmokeConfigError("problem/theorem/representation linkage differs")
    if (
        problem.source != config.source.expected_source
        or problem.source_license != config.source.expected_source_license
        or problem.private_source_content
        or not problem.external_provider_eligible
        or not problem.release_eligible
        or not problem.denylist_checked
        or problem.denylist_hits
    ):
        raise LF022RCPSmokeConfigError(
            "source is not public, release-eligible, denylist-clear external material"
        )
    serialized_source = (
        canonical_json_bytes(
            {
                "problem": problem.model_dump(mode="json"),
                "theorem": theorem.model_dump(mode="json"),
                "representation": representation.model_dump(mode="json"),
            }
        )
        .decode("utf-8")
        .casefold()
    )
    if any(marker in serialized_source for marker in _PRIVATE_SOURCE_MARKERS):
        raise LF022RCPSmokeConfigError("private sft_classic marker appears in smoke source")
    if theorem.declaration_name is None or representation.headless is None:
        raise LF022RCPSmokeConfigError("source lacks declaration name or headless view")
    source_statement = f"theorem {theorem.declaration_name} {representation.headless}"
    import_header = header_path.read_text(encoding="utf-8")
    source = PublicLeanVariantSource(
        source_theorem_id=theorem.theorem_id,
        source_representation_id=representation.representation_id,
        context_id=problem.context_id,
        imports=_imports_from_header(import_header),
        source_statement=source_statement,
        optional_natural_language=problem.nl_statement,
        source_id=problem.source,
        source_revision=problem.source_revision,
        source_license=problem.source_license,
        source_is_public=True,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )
    request_id = make_id(
        "variant_prompt",
        {
            "schema": "lf022_rcp_public_smoke_prompt_v1",
            "config_hash": loaded.config_hash,
            "source_theorem_id": theorem.theorem_id,
            "generation_distribution": "G_open",
            "proposal_count": config.generation.proposal_count,
        },
    )
    request = VariantPromptRequest(
        request_id=request_id,
        source=source,
        proposal_count=config.generation.proposal_count,
        requested_relations=config.generation.requested_relations,
        requested_error_types=(),
        requested_sci_categories=(),
        generation_distribution="G_open",
    )
    rendered = render_variant_proposer_prompt(request, template_path=proposer_path)
    if rendered.template_sha256 != config.prompts.proposer.sha256:
        raise LF022RCPSmokeConfigError("rendered proposer template hash differs")
    validate_family_separation(config.providers.family_matrix())
    return LoadedLF022RCPSmoke(
        loaded_config=loaded,
        problem=problem,
        theorem=theorem,
        representation=representation,
        import_header=import_header,
        proposer_request=request,
        proposer_prompt=rendered,
    )


def resolve_smoke_credentials(
    config: LF022RCPSmokeConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> RCPCredentials:
    source = os.environ if environ is None else environ
    base_url = source.get(config.transport.base_url_env, "").rstrip("/")
    api_key = source.get(config.transport.api_key_env, "")
    if not base_url or not api_key:
        raise LF022RCPSmokeCredentialError("RCP_BASE_URL and RCP_API_KEY must both be set")
    if base_url != config.transport.expected_base_url or not base_url.startswith("https://"):
        raise LF022RCPSmokeCredentialError("RCP_BASE_URL differs from frozen HTTPS endpoint")
    return RCPCredentials(base_url=base_url, api_key=api_key)


def _strict_json_object(body: bytes, *, label: str) -> dict[str, object]:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant {value!r}")

    def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            body.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LF022RCPSmokeError(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LF022RCPSmokeError(f"{label} JSON root must be an object")
    return value


def _assert_no_credential_echo(
    payload: bytes,
    *,
    api_key: str,
    label: str,
) -> None:
    """Reject exact RCP credentials before any bytes reach persistent storage."""

    key = api_key.encode("utf-8")
    bearer_markers = (
        b"Bearer " + key,
        b"bearer " + key,
    )
    if key in payload or any(marker in payload for marker in bearer_markers):
        raise LF022RCPSmokeCredentialError(f"{label} contains forbidden RCP credential material")


def _assert_safe_output_directory(
    repo_root: Path,
    directory: Path,
    *,
    label: str,
) -> None:
    """Reject output paths with symlinked or non-directory components."""

    root = repo_root.absolute()
    target = directory.absolute()
    if root.is_symlink() or not root.is_dir():
        raise LF022RCPSmokeError("repository root is missing or symlinked")
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise LF022RCPSmokeError(f"{label} escapes repository root") from exc
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise LF022RCPSmokeError(f"{label} contains a symlinked path component")
        if current.exists() and not current.is_dir():
            raise LF022RCPSmokeError(f"{label} contains a non-directory path component")


def _assert_artifact_tree_has_no_credentials(
    root: Path,
    *,
    api_key: str,
    label: str,
) -> None:
    """Scan a partial or complete smoke tree before terminal finalization."""

    if root.is_symlink() or not root.is_dir():
        raise LF022RCPSmokeError(f"{label} is missing or symlinked")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise LF022RCPSmokeError(f"{label} contains a symlinked artifact")
        if path.is_file():
            _assert_no_credential_echo(
                path.read_bytes(),
                api_key=api_key,
                label=f"{label} artifact",
            )


def _persist_immutable(
    path: Path,
    payload: bytes,
    *,
    private: bool = False,
    forbidden_api_key: str | None = None,
) -> str:
    if forbidden_api_key is not None:
        _assert_no_credential_echo(
            payload,
            api_key=forbidden_api_key,
            label="smoke artifact payload",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise LF022RCPSmokeArtifactConflict(f"immutable smoke artifact conflict at {path}")
        return hash_file(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022RCPSmokeArtifactConflict(
                    f"concurrent immutable smoke conflict at {path}"
                ) from None
        if private:
            os.chmod(path, 0o600)
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError as exc:
        raise LF022RCPSmokeError("artifact escapes repository root") from exc


def _safe_existing_artifact(repo_root: Path, relative: str, *, label: str) -> Path:
    path = _safe_repo_path(repo_root, relative, label=label)
    if _repo_relative(path, repo_root) != relative:
        raise LF022RCPSmokeError(f"{label} path is not canonical")
    return path


def _load_bound_model(
    repo_root: Path,
    bound: BoundArtifact,
    model_type: type[Any],
    *,
    label: str,
) -> Any:
    path = _safe_existing_artifact(repo_root, bound.artifact, label=label)
    raw = path.read_bytes()
    if hash_file(path) != bound.sha256:
        raise LF022RCPSmokeError(f"{label} hash differs")
    try:
        model = model_type.model_validate_json(raw)
    except ValueError as exc:
        raise LF022RCPSmokeError(f"{label} is invalid: {exc}") from exc
    if raw != canonical_json_bytes(model.model_dump(mode="json")) + b"\n":
        raise LF022RCPSmokeError(f"{label} is not canonical JSON")
    return model


def _verify_preflight_run(
    loaded: LoadedLF022RCPSmoke,
    *,
    preflight_run: SmokePreflightRun,
    repo_root: Path,
    credentials: RCPCredentials,
) -> LF022RCPSmokePreflight:
    """Reload and verify the complete catalog/preflight admission."""

    config = loaded.loaded_config.config
    if credentials.base_url != config.transport.expected_base_url:
        raise LF022RCPSmokeCredentialError(
            "execution credentials differ from the frozen RCP endpoint"
        )
    preflight_path = preflight_run.preflight_path
    if (
        preflight_path.is_symlink()
        or not preflight_path.is_file()
        or _repo_relative(preflight_path, repo_root)
        != _repo_relative(
            repo_root
            / config.outputs.preflight_root
            / "preflight"
            / f"{preflight_run.preflight.preflight_id.removeprefix('lf022_rcp_preflight:')}.json",
            repo_root,
        )
    ):
        raise LF022RCPSmokeError("preflight artifact path is missing or noncanonical")
    expected_preflight_bytes = (
        canonical_json_bytes(preflight_run.preflight.model_dump(mode="json")) + b"\n"
    )
    if preflight_path.read_bytes() != expected_preflight_bytes:
        raise LF022RCPSmokeError("persisted preflight differs from the supplied preflight")

    preflight = LF022RCPSmokePreflight.model_validate_json(
        preflight_path.read_text(encoding="utf-8")
    )
    expected_sources = (
        loaded.loaded_config.config_hash,
        loaded.problem.problem_record_id,
        loaded.theorem.theorem_id,
        loaded.representation.representation_id,
        loaded.problem.source_record_id,
        loaded.proposer_prompt.render_sha256,
    )
    observed_sources = (
        preflight.config_hash,
        preflight.problem_record_id,
        preflight.theorem_id,
        preflight.representation_id,
        preflight.source_record_id,
        preflight.proposer_prompt_sha256,
    )
    if observed_sources != expected_sources:
        raise LF022RCPSmokeError("preflight source/prompt/config bindings differ")

    catalog_path = _safe_existing_artifact(
        repo_root,
        preflight.catalog_artifact,
        label="preflight catalog",
    )
    if catalog_path != preflight_run.catalog_path.resolve():
        raise LF022RCPSmokeError("preflight catalog path differs from its run binding")
    catalog_bytes = catalog_path.read_bytes()
    if sha256_hex(catalog_bytes) != preflight.catalog.raw_response_sha256:
        raise LF022RCPSmokeError("preflight catalog raw hash differs")
    document = _strict_json_object(catalog_bytes, label="persisted RCP /models response")
    data = document.get("data")
    if not isinstance(data, list):
        raise LF022RCPSmokeError("persisted RCP catalog lacks a data array")
    model_ids = tuple(
        sorted(
            {
                item["id"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )
    )
    required = tuple(provider.model_id for provider in config.providers.enabled_rcp())
    if (
        preflight.catalog.endpoint_sha256
        != sha256_hex((credentials.base_url + config.transport.catalog_path).encode("utf-8"))
        or preflight.catalog.canonical_model_ids_sha256 != hash_canonical(model_ids)
        or preflight.catalog.model_count != len(model_ids)
        or preflight.catalog.required_model_ids != required
        or any(model not in model_ids for model in required)
    ):
        raise LF022RCPSmokeError("persisted preflight catalog bindings differ")
    return preflight


def probe_and_write_smoke_preflight(
    loaded: LoadedLF022RCPSmoke,
    *,
    repo_root: Path,
    credentials: RCPCredentials,
    transport: RCPHTTPTransport,
) -> SmokePreflightRun:
    """Perform exactly one catalog GET and zero chat-completion requests."""

    config = loaded.loaded_config.config
    if credentials.base_url != config.transport.expected_base_url:
        raise LF022RCPSmokeCredentialError(
            "preflight credentials differ from the frozen RCP endpoint"
        )
    preflight_root = repo_root / config.outputs.preflight_root
    _assert_safe_output_directory(
        repo_root,
        preflight_root,
        label="smoke preflight output root",
    )
    endpoint = credentials.base_url + config.transport.catalog_path
    response = transport.get(
        url=endpoint,
        api_key=credentials.api_key,
        timeout_seconds=config.transport.catalog_timeout_seconds,
    )
    _assert_no_credential_echo(
        response.body,
        api_key=credentials.api_key,
        label="RCP catalog response",
    )
    catalog_sha = sha256_hex(response.body)
    catalog_path = preflight_root / "catalog" / f"{catalog_sha}.json"
    _persist_immutable(
        catalog_path,
        response.body,
        forbidden_api_key=credentials.api_key,
    )
    if response.status_code != 200:
        raise LF022RCPSmokeCatalogError(
            f"RCP /models returned HTTP {response.status_code}; body persisted"
        )
    document = _strict_json_object(response.body, label="RCP /models response")
    data = document.get("data")
    if not isinstance(data, list):
        raise LF022RCPSmokeCatalogError("RCP /models response lacks data array")
    model_ids = tuple(
        sorted(
            {
                item["id"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )
    )
    required = tuple(provider.model_id for provider in config.providers.enabled_rcp())
    if len(required) != 3:
        raise LF022RCPSmokeCatalogError("smoke requires exactly three RCP routes")
    missing = tuple(model for model in required if model not in model_ids)
    if missing:
        raise LF022RCPSmokeCatalogError(
            "RCP catalog lacks exact admitted route IDs: " + ", ".join(missing)
        )
    canonical_ids_hash = hash_canonical(model_ids)
    typed_required = (required[0], required[1], required[2])
    catalog = SmokeCatalogObservation(
        endpoint_sha256=sha256_hex(endpoint.encode("utf-8")),
        raw_response_sha256=catalog_sha,
        canonical_model_ids_sha256=canonical_ids_hash,
        model_count=len(model_ids),
        required_model_ids=typed_required,
        required_models_present=(True, True, True),
        checkpoint_revision_status="unavailable_from_rcp_route_ids",
        route_snapshot_revision=f"rcp-catalog-sha256:{catalog_sha}",
    )
    catalog_artifact = _repo_relative(catalog_path, repo_root)
    preflight_id = "lf022_rcp_preflight:" + hash_canonical(
        {
            "schema": "lf022_rcp_public_smoke_preflight_v1",
            "config_hash": loaded.loaded_config.config_hash,
            "catalog": catalog.model_dump(mode="json"),
            "catalog_artifact": catalog_artifact,
            "problem_record_id": loaded.problem.problem_record_id,
            "theorem_id": loaded.theorem.theorem_id,
            "representation_id": loaded.representation.representation_id,
            "source_record_id": loaded.problem.source_record_id,
            "proposer_prompt_sha256": loaded.proposer_prompt.render_sha256,
        }
    )
    preflight = LF022RCPSmokePreflight(
        preflight_id=preflight_id,
        config_hash=loaded.loaded_config.config_hash,
        catalog=catalog,
        catalog_artifact=catalog_artifact,
        problem_record_id=loaded.problem.problem_record_id,
        theorem_id=loaded.theorem.theorem_id,
        representation_id=loaded.representation.representation_id,
        source_record_id=loaded.problem.source_record_id,
        proposer_prompt_sha256=loaded.proposer_prompt.render_sha256,
    )
    preflight_path = preflight_root / "preflight" / f"{preflight_id.split(':')[1]}.json"
    _persist_immutable(
        preflight_path,
        canonical_json_bytes(preflight.model_dump(mode="json")) + b"\n",
        forbidden_api_key=credentials.api_key,
    )
    _assert_artifact_tree_has_no_credentials(
        preflight_root,
        api_key=credentials.api_key,
        label="smoke preflight output",
    )
    return SmokePreflightRun(
        preflight=preflight,
        preflight_path=preflight_path,
        catalog_path=catalog_path,
    )


def _prompt_messages(text: str) -> list[dict[str, str]]:
    prefix = "SYSTEM\n"
    marker = "\n\nPROMPT_TEMPLATE_SHA256\n"
    if not text.startswith(prefix) or marker not in text:
        raise LF022RCPSmokeError("rendered prompt lacks frozen SYSTEM/user boundary")
    system, user = text[len(prefix) :].split(marker, 1)
    return [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": "PROMPT_TEMPLATE_SHA256\n" + user},
    ]


def _reasoning_metadata(
    message: Mapping[str, object],
    *,
    field: str,
) -> tuple[bool, int, str | None]:
    if field not in message or message[field] is None:
        return False, 0, None
    value = message[field]
    if isinstance(value, str):
        return True, len(value), sha256_hex(value.encode("utf-8"))
    encoded = canonical_json_bytes(value)
    return True, len(encoded), sha256_hex(encoded)


def extract_content_only(body: bytes, *, expected_model: str) -> tuple[str, SmokeWireMetadata]:
    """Extract only ``message.content`` while recording reasoning presence."""

    document = _strict_json_object(body, label="RCP chat-completion response")
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RCPTransportError(
            "invalid_response_shape",
            "RCP response lacks choices[0]",
            retryable=False,
            response_body=body,
        )
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RCPTransportError(
            "invalid_response_shape",
            "RCP response lacks choices[0].message",
            retryable=False,
            response_body=body,
        )
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RCPTransportError(
            "empty_response",
            "RCP response message.content is absent or empty",
            retryable=False,
            response_body=body,
        )
    returned_model = document.get("model")
    if not isinstance(returned_model, str) or returned_model != expected_model:
        raise RCPTransportError(
            "returned_model_mismatch",
            "RCP returned model differs from exact requested route",
            retryable=False,
            response_body=body,
        )
    usage: dict[str, int] = {}
    usage_raw = document.get("usage")
    if isinstance(usage_raw, dict):
        usage = {
            key: value
            for key, value in usage_raw.items()
            if isinstance(key, str) and isinstance(value, int) and value >= 0
        }
    content_reasoning = _reasoning_metadata(message, field="reasoning_content")
    reasoning = _reasoning_metadata(message, field="reasoning")
    request_id = document.get("id")
    return content, SmokeWireMetadata(
        provider_request_id=request_id if isinstance(request_id, str) else None,
        returned_model=returned_model,
        usage=usage,
        reasoning_content_present=content_reasoning[0],
        reasoning_content_chars=content_reasoning[1],
        reasoning_content_sha256=content_reasoning[2],
        reasoning_present=reasoning[0],
        reasoning_chars=reasoning[1],
        reasoning_sha256=reasoning[2],
    )


def _execute_parsed_call(
    *,
    loaded: LoadedLF022RCPSmoke,
    preflight: LF022RCPSmokePreflight,
    repo_root: Path,
    run_root: Path,
    credentials: RCPCredentials,
    transport: RCPHTTPTransport,
    provider: SmokeProviderConfig,
    call_label: str,
    role: LLMRole,
    rendered_prompt: str,
    prompt_template_hash: str,
    prompt_template_id: str,
    prompt_template_version: str,
    input_ids: tuple[str, ...],
    parse: Callable[[str], StrictModel],
    parsed_dump_by_alias: bool,
    metadata: Mapping[str, DecodingValue],
    clock: Callable[[], datetime.datetime],
) -> _WireCallResult:
    if provider.decoding is None or provider.transport != "rcp_openai_compatible":
        raise LF022RCPSmokeError("only enabled RCP providers can execute smoke calls")
    revision = preflight.catalog.route_snapshot_revision
    identity = ProviderIdentity(
        provider=provider.provider,
        model=provider.model_id,
        revision=revision,
        transport="external_disabled",
    )
    request = ProviderRequest.create(
        identity=identity,
        prompt_template_hash=prompt_template_hash,
        rendered_prompt=rendered_prompt,
        decoding=provider.decoding.provider_decoding(),
        input_ids=input_ids,
        private_source_content=False,
        attempt_index=0,
    )
    call_dir = run_root / "calls" / call_label
    request_path = call_dir / "provider_request.json"
    _assert_no_credential_echo(
        canonical_json_bytes(request.model_dump(mode="json")) + b"\n",
        api_key=credentials.api_key,
        label=f"{call_label} provider request",
    )
    request_sha = persist_provider_request(request, request_path)
    wire_payload = {
        "model": provider.model_id,
        "messages": _prompt_messages(rendered_prompt),
        **provider.decoding.wire_fields(),
    }
    wire_request_path = call_dir / "wire_request.json"
    wire_request_bytes = canonical_json_bytes(wire_payload) + b"\n"
    wire_request_sha = _persist_immutable(
        wire_request_path,
        wire_request_bytes,
        private=True,
        forbidden_api_key=credentials.api_key,
    )
    started = clock()
    lineage_path = call_dir / "llm_call.json"

    def persist_raw_response(response: ProviderRawResponse) -> Any:
        raw_bytes = canonical_json_bytes(response.model_dump(mode="json")) + b"\n"
        _assert_no_credential_echo(
            raw_bytes,
            api_key=credentials.api_key,
            label=f"{call_label} provider raw response",
        )
        result = persist_provider_raw_response(run_root / "provider_raw", response)
        _assert_no_credential_echo(
            result.raw_response_path.read_bytes(),
            api_key=credentials.api_key,
            label=f"{call_label} persisted provider raw response",
        )
        return result

    def persist_lineage(
        *,
        provider_result: Any,
        parse_status: ParseStatus,
        parsed_output: Mapping[str, object] | None,
        completed: datetime.datetime,
        extra_metadata: Mapping[str, DecodingValue],
    ) -> LLMCallRecord:
        lineage = bridge_provider_result_to_generic_llm_lineage(
            request=request,
            result=provider_result,
            request_artifact_path=request_path,
            artifact_root=repo_root,
            role=role,
            provider_slot=provider.provider_slot,
            model_family=provider.family_id,
            prompt_template_id=prompt_template_id,
            prompt_template_version=prompt_template_version,
            execution_mode="external",
            parse_status=parse_status,
            parsed_output=parsed_output,
            private_source_content=False,
            denylist_checked=True,
            denylist_hits=(),
            started_at=started,
            completed_at=completed,
            supervision_eligible=False,
            metadata={**metadata, "wire_request_sha256": wire_request_sha, **extra_metadata},
        )
        _persist_immutable(
            lineage_path,
            canonical_json_bytes(lineage.call.model_dump(mode="json")) + b"\n",
            private=True,
            forbidden_api_key=credentials.api_key,
        )
        return lineage.call

    try:
        response = transport.post_json(
            url=credentials.base_url + loaded.loaded_config.config.transport.chat_completions_path,
            api_key=credentials.api_key,
            payload=wire_payload,
            timeout_seconds=loaded.loaded_config.config.transport.request_timeout_seconds,
        )
        _assert_no_credential_echo(
            response.body,
            api_key=credentials.api_key,
            label=f"{call_label} RCP response",
        )
    except Exception as exc:
        completed = clock()
        provider_result = persist_raw_response(
            ProviderRawResponse.error(
                request,
                error_type=(exc.code if isinstance(exc, RCPTransportError) else type(exc).__name__),
            ),
        )
        persist_lineage(
            provider_result=provider_result,
            parse_status=ParseStatus.EMPTY,
            parsed_output=None,
            completed=completed,
            extra_metadata={"transport_failure": True},
        )
        raise
    completed = clock()
    wire_response_path = call_dir / "wire_response.json"
    wire_response_sha = _persist_immutable(
        wire_response_path,
        response.body,
        private=True,
        forbidden_api_key=credentials.api_key,
    )
    if response.status_code != 200:
        provider_result = persist_raw_response(
            ProviderRawResponse.error(
                request,
                error_type=f"http_{response.status_code}",
            ),
        )
        persist_lineage(
            provider_result=provider_result,
            parse_status=ParseStatus.EMPTY,
            parsed_output=None,
            completed=completed,
            extra_metadata={
                "http_status": response.status_code,
                "wire_response_sha256": wire_response_sha,
            },
        )
        raise LF022RCPSmokeError(
            f"{call_label} returned HTTP {response.status_code}; exact response persisted"
        )
    try:
        content, wire = extract_content_only(response.body, expected_model=provider.model_id)
    except Exception as exc:
        provider_result = persist_raw_response(
            ProviderRawResponse.error(
                request,
                error_type=(exc.code if isinstance(exc, RCPTransportError) else type(exc).__name__),
            ),
        )
        persist_lineage(
            provider_result=provider_result,
            parse_status=ParseStatus.EMPTY,
            parsed_output=None,
            completed=completed,
            extra_metadata={"wire_response_sha256": wire_response_sha},
        )
        raise
    provider_raw = ProviderRawResponse.success(request, content)
    provider_result = persist_raw_response(provider_raw)
    try:
        parsed = parse(content)
    except Exception:
        persist_lineage(
            provider_result=provider_result,
            parse_status=ParseStatus.PARSE_FAILED,
            parsed_output=None,
            completed=completed,
            extra_metadata={
                "wire_response_sha256": wire_response_sha,
                "parse_failed": True,
            },
        )
        raise
    parsed_output = parsed.model_dump(mode="json", by_alias=parsed_dump_by_alias)
    call = persist_lineage(
        provider_result=provider_result,
        parse_status=ParseStatus.PARSED,
        parsed_output=parsed_output,
        completed=completed,
        extra_metadata={
            "returned_model": wire.returned_model,
            "provider_request_id": wire.provider_request_id,
            "reasoning_content_present": wire.reasoning_content_present,
            "reasoning_present": wire.reasoning_present,
            "wire_response_sha256": wire_response_sha,
            **{f"usage_{key}": value for key, value in sorted(wire.usage.items())},
        },
    )
    lineage_sha = hash_file(lineage_path)
    artifact = SmokeCallArtifact(
        call_label=call_label,
        llm_call_id=call.call_id,
        role="proposer" if role is LLMRole.PROPOSER else "judge",
        provider_slot=provider.provider_slot,
        model_id=provider.model_id,
        model_family=provider.family_id,
        model_revision=revision,
        provider_request_hash=request.request_hash,
        provider_request_artifact=_repo_relative(request_path, repo_root),
        provider_request_sha256=request_sha,
        wire_request_artifact=_repo_relative(wire_request_path, repo_root),
        wire_request_sha256=wire_request_sha,
        wire_response_artifact=_repo_relative(wire_response_path, repo_root),
        wire_response_sha256=wire_response_sha,
        provider_raw_artifact=_repo_relative(provider_result.raw_response_path, repo_root),
        provider_raw_sha256=provider_result.raw_response_sha256,
        llm_call_artifact=_repo_relative(lineage_path, repo_root),
        llm_call_sha256=lineage_sha,
        content_sha256=sha256_hex(content.encode("utf-8")),
        wire=wire,
        parse_status="parsed",
    )
    artifact_path = call_dir / "call_artifact.json"
    _persist_immutable(
        artifact_path,
        canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n",
        private=True,
        forbidden_api_key=credentials.api_key,
    )
    return _WireCallResult(artifact=artifact, call=call, content=content)


def _candidate_name(statement: str) -> str:
    match = _DECLARATION_NAME.match(statement.strip())
    if match is None:
        raise LF022RCPSmokeError("parsed candidate lacks one theorem/lemma name")
    return match.group(1)


def _validate_candidate(
    loaded: LoadedLF022RCPSmoke,
    *,
    variant: VariantRecord,
    lean_backend: LeanBackend,
    repo_root: Path,
    run_root: Path,
    credentials: RCPCredentials,
) -> tuple[VariantRecord, BoundArtifact]:
    if variant.extracted_statement is None:
        raise LF022RCPSmokeError("provisional variant lacks extracted statement")
    declaration_name = _candidate_name(variant.extracted_statement)
    namespace_name = "LeanFaithLF022Smoke.Run" + variant.variant_id.removeprefix("var:")[:16]
    request = LeanRequest(
        request_id=f"lf022-rcp-smoke-{variant.variant_id}",
        context_id=loaded.problem.context_id,
        code=(
            f"{loaded.import_header.rstrip()}\n"
            f"namespace {namespace_name}\n"
            f"{variant.extracted_statement} := by sorry\n"
            "end "
            f"{namespace_name}\n"
        ),
        declarations=True,
        allow_sorry=True,
        timeout_seconds=120,
    )
    result = lean_backend.run(request)
    if result.status not in {LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY}:
        raise LF022RCPSmokeError(
            f"proposer candidate failed Lean validation: {result.status.value}"
        )
    full_names = tuple(
        str(item.get("full_name"))
        for item in result.declarations
        if isinstance(item, dict) and item.get("kind") in {"theorem", "lemma"}
    )
    expected_full_name = f"{namespace_name}.{declaration_name}"
    if full_names != (expected_full_name,):
        raise LF022RCPSmokeError(
            f"candidate declaration set differs: {full_names!r} != {(expected_full_name,)!r}"
        )
    validation_status = (
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER
        if result.status is LeanStatus.VALID_WITH_SORRY
        else ValidationStatus.ELABORATES
    )
    raw_response_artifact: str | None = None
    raw_response_sha256: str | None = None
    if result.raw_response_path is not None:
        raw_path = Path(result.raw_response_path)
        if not raw_path.is_absolute():
            raw_path = repo_root / raw_path
        if raw_path.is_symlink() or not raw_path.is_file():
            raise LF022RCPSmokeError("Lean raw response artifact is missing or unsafe")
        raw_response_artifact = _repo_relative(raw_path, repo_root)
        raw_response_sha256 = hash_file(raw_path)
    assert request.code is not None
    lean_status: Literal["valid", "valid_with_sorry"] = (
        "valid_with_sorry" if result.status is LeanStatus.VALID_WITH_SORRY else "valid"
    )
    lean_record = SmokeLeanValidationRecord(
        request_id=request.request_id,
        request_hash=result.request_hash,
        context_id=result.context_id,
        code_sha256=sha256_hex(request.code.encode("utf-8")),
        status=lean_status,
        declarations=tuple(dict(item) for item in result.declarations),
        messages=tuple(dict(item) for item in result.messages),
        sorries=tuple(dict(item) for item in result.sorries),
        raw_response_artifact=raw_response_artifact,
        raw_response_sha256=raw_response_sha256,
    )
    lean_path = run_root / "lean_validation.json"
    _persist_immutable(
        lean_path,
        canonical_json_bytes(lean_record.model_dump(mode="json")) + b"\n",
        private=True,
        forbidden_api_key=credentials.api_key,
    )
    lean_artifact = BoundArtifact(
        artifact=_repo_relative(lean_path, repo_root),
        sha256=hash_file(lean_path),
    )
    payload = variant.model_dump(mode="json")
    payload["validation_status"] = validation_status.value
    payload["metadata"] = {
        **variant.metadata,
        "artifact_class": "smoke",
        "lean_request_hash": result.request_hash,
        "lean_status": result.status.value,
        "lean_validation_artifact": lean_artifact.artifact,
        "lean_validation_sha256": lean_artifact.sha256,
        "semantic_label_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return VariantRecord.model_validate(payload), lean_artifact


def _execute_public_smoke_inner(
    loaded: LoadedLF022RCPSmoke,
    *,
    preflight: LF022RCPSmokePreflight,
    preflight_artifact: BoundArtifact,
    catalog_artifact: BoundArtifact,
    repo_root: Path,
    credentials: RCPCredentials,
    transport: RCPHTTPTransport,
    lean_backend: LeanBackend,
    clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC),
) -> SmokeExecutionRun:
    """Execute the five calls after the public wrapper owns the run lock."""

    config = loaded.loaded_config.config
    run_key = hash_canonical(
        {
            "schema": "lf022_rcp_public_smoke_run_v1",
            "config_hash": loaded.loaded_config.config_hash,
            "catalog_raw_response_sha256": preflight.catalog.raw_response_sha256,
            "problem_record_id": loaded.problem.problem_record_id,
        }
    )
    run_root = repo_root / config.outputs.raw_root / run_key
    proposer = _execute_parsed_call(
        loaded=loaded,
        preflight=preflight,
        repo_root=repo_root,
        run_root=run_root,
        credentials=credentials,
        transport=transport,
        provider=config.providers.proposer,
        call_label="proposer",
        role=LLMRole.PROPOSER,
        rendered_prompt=loaded.proposer_prompt.text,
        prompt_template_hash=loaded.proposer_prompt.template_sha256,
        prompt_template_id=loaded.proposer_prompt.template_id,
        prompt_template_version=loaded.proposer_prompt.template_version,
        input_ids=variant_provider_input_ids(loaded.proposer_request),
        parse=parse_variant_proposer_output,
        parsed_dump_by_alias=False,
        metadata={
            "generation_distribution": "G_open",
            "generation_config_hash": loaded.loaded_config.config_hash,
            "semantic_labels_created": False,
        },
        clock=clock,
    )
    variants = materialize_verified_provisional_variants(
        request=loaded.proposer_request,
        call=proposer.call,
        artifact_root=repo_root,
        generation_config_hash=loaded.loaded_config.config_hash,
        template_path=repo_root / config.prompts.proposer.artifact,
    )
    if len(variants) != 1:
        raise LF022RCPSmokeError("one-item smoke requires exactly one parsed variant")
    variant, lean_validation_artifact = _validate_candidate(
        loaded,
        variant=variants[0],
        lean_backend=lean_backend,
        repo_root=repo_root,
        run_root=run_root,
        credentials=credentials,
    )
    variant_path = run_root / "variant.json"
    _persist_immutable(
        variant_path,
        canonical_json_bytes(variant.model_dump(mode="json")) + b"\n",
        private=True,
        forbidden_api_key=credentials.api_key,
    )
    variant_artifact = BoundArtifact(
        artifact=_repo_relative(variant_path, repo_root),
        sha256=hash_file(variant_path),
    )
    assert variant.extracted_statement is not None
    source_statement = loaded.proposer_request.source.source_statement
    pair_id = make_id(
        PAIR_PREFIX,
        {
            "schema": "lf022_rcp_public_smoke_pair_v1",
            "source_theorem_id": loaded.theorem.theorem_id,
            "variant_id": variant.variant_id,
            "candidate_code_hash": variant.candidate_code_hash,
        },
    )
    judge_source = PublicLeanJudgePair(
        pair_id=pair_id,
        canonical_lean_a=source_statement,
        canonical_lean_b=variant.extracted_statement,
        optional_natural_language=loaded.problem.nl_statement,
        source_record_ids=(loaded.problem.source_record_id,),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )
    randomization_key = bytes.fromhex(
        hash_canonical(
            {
                "schema": "lf022_rcp_smoke_judge_randomization_v1",
                "config_hash": loaded.loaded_config.config_hash,
                "pair_id": pair_id,
            }
        )
    )
    judge_prompt_path = repo_root / config.prompts.judge.artifact
    judge_calls: list[_WireCallResult] = []
    tasks_by_id: dict[str, Any] = {}
    judge_specs: tuple[tuple[JudgeSlot, SmokeProviderConfig], ...] = (
        ("judge_A", config.providers.judge_A),
        ("judge_B", config.providers.judge_B),
    )
    for slot, provider in judge_specs:
        tasks = make_swapped_presentations(
            source=judge_source,
            judge_slot=slot,
            randomization_key=randomization_key,
        )
        for task in tasks:
            rendered = render_blinded_judge_prompt(task, template_path=judge_prompt_path)
            result = _execute_parsed_call(
                loaded=loaded,
                preflight=preflight,
                repo_root=repo_root,
                run_root=run_root,
                credentials=credentials,
                transport=transport,
                provider=provider,
                call_label=f"{slot}_{task.orientation}",
                role=LLMRole.JUDGE,
                rendered_prompt=rendered.text,
                prompt_template_hash=rendered.template_sha256,
                prompt_template_id=rendered.template_id,
                prompt_template_version=rendered.template_version,
                input_ids=judge_provider_input_ids(task),
                parse=parse_blinded_judge_output,
                parsed_dump_by_alias=True,
                metadata={
                    "weak_supervision_config_hash": loaded.loaded_config.config_hash,
                    "proposer_family": config.providers.proposer.family_id,
                    "judge_slot": slot,
                    "orientation": task.orientation,
                    "source_admission_sha256": judge_source.admission_sha256,
                },
                clock=clock,
            )
            judge_calls.append(result)
            tasks_by_id[result.call.call_id] = task
    if len(judge_calls) != 4:
        raise LF022RCPSmokeError("smoke must produce exactly four judge calls")
    evidence = tuple(
        materialize_verified_judgment_evidence(
            call=result.call,
            task=tasks_by_id[result.call.call_id],
            source=judge_source,
            family_matrix=config.providers.family_matrix(),
            proposer_family=config.providers.proposer.family_id,
            method_version="lf022_rcp_public_smoke_judgment_v1",
            config_hash=loaded.loaded_config.config_hash,
            artifact_root=repo_root,
            created_at=clock(),
            template_path=judge_prompt_path,
        ).model_copy(
            update={
                "metadata": {
                    "artifact_class": "smoke",
                    "llm_call_id": result.call.call_id,
                    "judge_family": result.call.model_family,
                    "judge_slot": tasks_by_id[result.call.call_id].judge_slot,
                    "proposer_family": config.providers.proposer.family_id,
                    "orientation": tasks_by_id[result.call.call_id].orientation,
                    "semantic_label_created": False,
                    "supervision_eligible": False,
                    "training_eligible": False,
                    "evaluation_eligible": False,
                    "gate_credit_claimed": False,
                }
            }
        )
        for result in judge_calls
    )
    evidence_artifacts: list[BoundArtifact] = []
    for record in evidence:
        evidence_path = run_root / "judgment_evidence" / f"{record.evidence_id.split(':')[1]}.json"
        _persist_immutable(
            evidence_path,
            canonical_json_bytes(record.model_dump(mode="json")) + b"\n",
            private=True,
            forbidden_api_key=credentials.api_key,
        )
        evidence_artifacts.append(
            BoundArtifact(
                artifact=_repo_relative(evidence_path, repo_root),
                sha256=hash_file(evidence_path),
            )
        )
    consensus = build_weak_consensus_candidate(
        pair_id=pair_id,
        proposer_family=config.providers.proposer.family_id,
        judgments=evidence,
        created_at=clock(),
        family_matrix=config.providers.family_matrix(),
    )
    consensus = consensus.model_copy(
        update={
            "metadata": {
                **consensus.metadata,
                "artifact_class": "smoke",
                "semantic_label_created": False,
                "supervision_eligible": False,
                "training_eligible": False,
                "evaluation_eligible": False,
                "gate_credit_claimed": False,
            }
        }
    )
    consensus_path = run_root / "weak_consensus_candidate.json"
    _persist_immutable(
        consensus_path,
        canonical_json_bytes(consensus.model_dump(mode="json")) + b"\n",
        private=True,
        forbidden_api_key=credentials.api_key,
    )
    consensus_artifact = BoundArtifact(
        artifact=_repo_relative(consensus_path, repo_root),
        sha256=hash_file(consensus_path),
    )
    if len(evidence_artifacts) != 4:
        raise LF022RCPSmokeError("smoke must persist exactly four judgment evidence records")
    typed_evidence_artifacts = (
        evidence_artifacts[0],
        evidence_artifacts[1],
        evidence_artifacts[2],
        evidence_artifacts[3],
    )
    call_artifacts = (proposer.artifact, *(item.artifact for item in judge_calls))
    payload = {
        "schema": "lf022_rcp_public_smoke_manifest_v1",
        "config_hash": loaded.loaded_config.config_hash,
        "catalog_raw_response_sha256": preflight.catalog.raw_response_sha256,
        "problem_record_id": loaded.problem.problem_record_id,
        "source_theorem_id": loaded.theorem.theorem_id,
        "source_representation_id": loaded.representation.representation_id,
        "variant_id": variant.variant_id,
        "pair_id": pair_id,
        "preflight_artifact": preflight_artifact.model_dump(mode="json"),
        "catalog_artifact": catalog_artifact.model_dump(mode="json"),
        "call_artifacts": tuple(item.model_dump(mode="json") for item in call_artifacts),
        "variant_artifact": variant_artifact.model_dump(mode="json"),
        "lean_validation_artifact": lean_validation_artifact.model_dump(mode="json"),
        "judgment_evidence_artifacts": tuple(
            item.model_dump(mode="json") for item in typed_evidence_artifacts
        ),
        "weak_consensus_artifact": consensus_artifact.model_dump(mode="json"),
        "weak_consensus_candidate_id": consensus.candidate_id,
    }
    manifest_id = "lf022_rcp_smoke_manifest:" + hash_canonical(payload)
    candidate_validation_status: Literal["elaborates", "elaborates_with_placeholder"] = (
        "elaborates_with_placeholder"
        if variant.validation_status is ValidationStatus.ELABORATES_WITH_PLACEHOLDER
        else "elaborates"
    )
    manifest = LF022RCPSmokeManifest(
        manifest_id=manifest_id,
        config_hash=loaded.loaded_config.config_hash,
        catalog_raw_response_sha256=preflight.catalog.raw_response_sha256,
        problem_record_id=loaded.problem.problem_record_id,
        source_theorem_id=loaded.theorem.theorem_id,
        source_representation_id=loaded.representation.representation_id,
        variant_id=variant.variant_id,
        pair_id=pair_id,
        preflight_artifact=preflight_artifact,
        catalog_artifact=catalog_artifact,
        call_artifacts=call_artifacts,
        variant_artifact=variant_artifact,
        lean_validation_artifact=lean_validation_artifact,
        judgment_evidence_artifacts=typed_evidence_artifacts,
        weak_consensus_artifact=consensus_artifact,
        candidate_validation_status=candidate_validation_status,
        weak_consensus_candidate_id=consensus.candidate_id,
        weak_consensus_status=consensus.status,
    )
    manifest_path = run_root / "manifest.json"
    _assert_artifact_tree_has_no_credentials(
        run_root,
        api_key=credentials.api_key,
        label="smoke success run",
    )
    _persist_immutable(
        manifest_path,
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
        private=True,
        forbidden_api_key=credentials.api_key,
    )
    _assert_artifact_tree_has_no_credentials(
        run_root,
        api_key=credentials.api_key,
        label="final smoke success run",
    )
    return SmokeExecutionRun(
        manifest=manifest,
        manifest_path=manifest_path,
        variant=variant,
    )


def _smoke_run_key(
    loaded: LoadedLF022RCPSmoke,
    preflight: LF022RCPSmokePreflight,
) -> str:
    return hash_canonical(
        {
            "schema": "lf022_rcp_public_smoke_run_v1",
            "config_hash": loaded.loaded_config.config_hash,
            "catalog_raw_response_sha256": preflight.catalog.raw_response_sha256,
            "problem_record_id": loaded.problem.problem_record_id,
        }
    )


def _persist_terminal_failure(
    *,
    run_root: Path,
    repo_root: Path,
    config_hash: str,
    run_key: str,
    error: BaseException,
    api_key: str,
) -> Path:
    """Inventory the immutable partial run without persisting exception text."""

    failure_path = run_root / "failure_manifest.json"
    if failure_path.is_file():
        return failure_path
    _assert_artifact_tree_has_no_credentials(
        run_root,
        api_key=api_key,
        label="smoke failure run",
    )
    artifacts = tuple(
        SmokeFailureArtifact(
            artifact=_repo_relative(path, repo_root),
            sha256=hash_file(path),
        )
        for path in sorted(run_root.rglob("*"))
        if path.is_file() and not path.is_symlink() and path != failure_path
    )
    payload = {
        "schema_version": 1,
        "artifact_class": "smoke",
        "config_hash": config_hash,
        "run_key": run_key,
        "error_type": type(error).__name__,
        "error_message_sha256": sha256_hex(str(error).encode("utf-8")),
        "chat_completion_attempts": sum(
            item.artifact.endswith("/wire_request.json") for item in artifacts
        ),
        "completed_call_count": sum(
            item.artifact.endswith("/call_artifact.json") for item in artifacts
        ),
        "artifacts": tuple(item.model_dump(mode="json") for item in artifacts),
        "terminal": True,
        "retry_permitted": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    failure = LF022RCPSmokeFailureManifest.model_validate(
        {
            "failure_id": "lf022_rcp_smoke_failure:" + hash_canonical(payload),
            **payload,
        }
    )
    _persist_immutable(
        failure_path,
        canonical_json_bytes(failure.model_dump(mode="json")) + b"\n",
        private=True,
        forbidden_api_key=api_key,
    )
    _assert_artifact_tree_has_no_credentials(
        run_root,
        api_key=api_key,
        label="final smoke failure run",
    )
    return failure_path


def execute_public_smoke(
    loaded: LoadedLF022RCPSmoke,
    *,
    preflight_run: SmokePreflightRun,
    repo_root: Path,
    credentials: RCPCredentials,
    transport: RCPHTTPTransport,
    lean_backend: LeanBackend,
    execute_public_smoke: bool,
    clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC),
) -> SmokeExecutionRun:
    """Execute one atomic, non-resumable, exactly-five-call public smoke."""

    if not execute_public_smoke:
        raise LF022RCPSmokeError(
            "live inference requires execute_public_smoke=True / --execute-public-smoke"
        )
    preflight = _verify_preflight_run(
        loaded,
        preflight_run=preflight_run,
        repo_root=repo_root,
        credentials=credentials,
    )
    run_key = _smoke_run_key(loaded, preflight)
    raw_root = repo_root / loaded.loaded_config.config.outputs.raw_root
    _assert_safe_output_directory(
        repo_root,
        raw_root,
        label="smoke raw output root",
    )
    run_root = raw_root / run_key
    _assert_safe_output_directory(
        repo_root,
        run_root,
        label="smoke run directory",
    )
    run_root.mkdir(parents=True, exist_ok=True)
    _assert_safe_output_directory(
        repo_root,
        run_root,
        label="smoke run directory",
    )
    lock_path = run_root / "run.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise LF022RCPSmokeError(
            "smoke run is already claimed or terminal; no call may be repeated"
        ) from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(
            canonical_json_bytes(
                {
                    "schema": "lf022_rcp_public_smoke_lock_v1",
                    "config_hash": loaded.loaded_config.config_hash,
                    "run_key": run_key,
                    "preflight_id": preflight.preflight_id,
                }
            )
            + b"\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    try:
        return _execute_public_smoke_inner(
            loaded,
            preflight=preflight,
            preflight_artifact=BoundArtifact(
                artifact=_repo_relative(preflight_run.preflight_path, repo_root),
                sha256=hash_file(preflight_run.preflight_path),
            ),
            catalog_artifact=BoundArtifact(
                artifact=_repo_relative(preflight_run.catalog_path, repo_root),
                sha256=hash_file(preflight_run.catalog_path),
            ),
            repo_root=repo_root,
            credentials=credentials,
            transport=transport,
            lean_backend=lean_backend,
            clock=clock,
        )
    except Exception as exc:
        _persist_terminal_failure(
            run_root=run_root,
            repo_root=repo_root,
            config_hash=loaded.loaded_config.config_hash,
            run_key=run_key,
            error=exc,
            api_key=credentials.api_key,
        )
        raise


def replay_public_smoke(
    loaded: LoadedLF022RCPSmoke,
    *,
    manifest_path: Path,
    repo_root: Path,
) -> LF022RCPSmokeManifest:
    """Verify one completed smoke entirely from immutable local artifacts."""

    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or _repo_relative(manifest_path, repo_root).startswith("../")
    ):
        raise LF022RCPSmokeError("smoke manifest is missing or unsafe")
    try:
        manifest_raw = manifest_path.read_bytes()
        manifest = LF022RCPSmokeManifest.model_validate_json(manifest_raw)
    except ValueError as exc:
        raise LF022RCPSmokeError(f"smoke manifest is invalid: {exc}") from exc
    if manifest_raw != canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n":
        raise LF022RCPSmokeError("smoke manifest is not canonical JSON")
    if manifest.config_hash != loaded.loaded_config.config_hash:
        raise LF022RCPSmokeError("smoke manifest config hash differs")
    expected_id = "lf022_rcp_smoke_manifest:" + hash_canonical(
        {
            "schema": "lf022_rcp_public_smoke_manifest_v1",
            "config_hash": manifest.config_hash,
            "catalog_raw_response_sha256": manifest.catalog_raw_response_sha256,
            "problem_record_id": manifest.problem_record_id,
            "source_theorem_id": manifest.source_theorem_id,
            "source_representation_id": manifest.source_representation_id,
            "variant_id": manifest.variant_id,
            "pair_id": manifest.pair_id,
            "preflight_artifact": manifest.preflight_artifact.model_dump(mode="json"),
            "catalog_artifact": manifest.catalog_artifact.model_dump(mode="json"),
            "call_artifacts": tuple(
                item.model_dump(mode="json") for item in manifest.call_artifacts
            ),
            "variant_artifact": manifest.variant_artifact.model_dump(mode="json"),
            "lean_validation_artifact": manifest.lean_validation_artifact.model_dump(mode="json"),
            "judgment_evidence_artifacts": tuple(
                item.model_dump(mode="json") for item in manifest.judgment_evidence_artifacts
            ),
            "weak_consensus_artifact": manifest.weak_consensus_artifact.model_dump(mode="json"),
            "weak_consensus_candidate_id": manifest.weak_consensus_candidate_id,
        }
    )
    if manifest.manifest_id != expected_id:
        raise LF022RCPSmokeError("smoke manifest semantic ID differs")
    if (
        manifest.problem_record_id != loaded.problem.problem_record_id
        or manifest.source_theorem_id != loaded.theorem.theorem_id
        or manifest.source_representation_id != loaded.representation.representation_id
    ):
        raise LF022RCPSmokeError("smoke manifest source bindings differ")
    stored_preflight = _load_bound_model(
        repo_root,
        manifest.preflight_artifact,
        LF022RCPSmokePreflight,
        label="smoke preflight",
    )
    assert isinstance(stored_preflight, LF022RCPSmokePreflight)
    catalog_path = _safe_existing_artifact(
        repo_root,
        manifest.catalog_artifact.artifact,
        label="smoke catalog",
    )
    catalog_bytes = catalog_path.read_bytes()
    if (
        hash_file(catalog_path) != manifest.catalog_artifact.sha256
        or sha256_hex(catalog_bytes) != manifest.catalog_raw_response_sha256
        or stored_preflight.catalog.raw_response_sha256 != manifest.catalog_raw_response_sha256
        or stored_preflight.config_hash != manifest.config_hash
        or stored_preflight.problem_record_id != manifest.problem_record_id
    ):
        raise LF022RCPSmokeError("smoke preflight/catalog replay bindings differ")
    stored_variant = _load_bound_model(
        repo_root,
        manifest.variant_artifact,
        VariantRecord,
        label="smoke variant",
    )
    assert isinstance(stored_variant, VariantRecord)
    lean_validation = _load_bound_model(
        repo_root,
        manifest.lean_validation_artifact,
        SmokeLeanValidationRecord,
        label="smoke Lean validation",
    )
    assert isinstance(lean_validation, SmokeLeanValidationRecord)
    stored_evidence = tuple(
        _load_bound_model(
            repo_root,
            bound,
            EvidenceRecord,
            label=f"smoke judgment evidence {index}",
        )
        for index, bound in enumerate(manifest.judgment_evidence_artifacts)
    )
    stored_consensus = _load_bound_model(
        repo_root,
        manifest.weak_consensus_artifact,
        WeakConsensusCandidateRecord,
        label="smoke weak consensus",
    )
    assert isinstance(stored_consensus, WeakConsensusCandidateRecord)
    if (
        stored_variant.variant_id != manifest.variant_id
        or stored_consensus.candidate_id != manifest.weak_consensus_candidate_id
        or stored_consensus.status != manifest.weak_consensus_status
        or any(not isinstance(record, EvidenceRecord) for record in stored_evidence)
    ):
        raise LF022RCPSmokeError("smoke downstream artifact bindings differ")
    if (
        stored_variant.metadata.get("lean_request_hash") != lean_validation.request_hash
        or stored_variant.metadata.get("lean_status") != lean_validation.status
        or stored_variant.metadata.get("lean_validation_artifact")
        != manifest.lean_validation_artifact.artifact
        or stored_variant.metadata.get("lean_validation_sha256")
        != manifest.lean_validation_artifact.sha256
    ):
        raise LF022RCPSmokeError("stored variant/Lean validation bindings differ")
    if lean_validation.raw_response_artifact is not None:
        assert lean_validation.raw_response_sha256 is not None
        raw_lean_path = _safe_existing_artifact(
            repo_root,
            lean_validation.raw_response_artifact,
            label="Lean raw response",
        )
        if hash_file(raw_lean_path) != lean_validation.raw_response_sha256:
            raise LF022RCPSmokeError("Lean raw response hash differs")
    for record in (stored_variant, *stored_evidence, stored_consensus):
        metadata = record.metadata
        if (
            metadata.get("artifact_class") != "smoke"
            or metadata.get("supervision_eligible") is not False
            or metadata.get("training_eligible") is not False
            or metadata.get("evaluation_eligible") is not False
            or metadata.get("gate_credit_claimed") is not False
        ):
            raise LF022RCPSmokeError("smoke downstream artifact quarantine differs")

    assert stored_variant.extracted_statement is not None
    declaration_name = _candidate_name(stored_variant.extracted_statement)
    namespace_name = "LeanFaithLF022Smoke.Run" + stored_variant.variant_id.removeprefix("var:")[:16]
    expected_lean_code = (
        f"{loaded.import_header.rstrip()}\n"
        f"namespace {namespace_name}\n"
        f"{stored_variant.extracted_statement} := by sorry\n"
        f"end {namespace_name}\n"
    )
    expected_status = (
        "elaborates_with_placeholder"
        if lean_validation.status == "valid_with_sorry"
        else "elaborates"
    )
    full_names = tuple(
        str(item.get("full_name"))
        for item in lean_validation.declarations
        if item.get("kind") in {"theorem", "lemma"}
    )
    if (
        lean_validation.code_sha256 != sha256_hex(expected_lean_code.encode("utf-8"))
        or full_names != (f"{namespace_name}.{declaration_name}",)
        or manifest.candidate_validation_status != expected_status
    ):
        raise LF022RCPSmokeError("Lean validation replay bindings differ")
    expected_pair_id = make_id(
        PAIR_PREFIX,
        {
            "schema": "lf022_rcp_public_smoke_pair_v1",
            "source_theorem_id": loaded.theorem.theorem_id,
            "variant_id": stored_variant.variant_id,
            "candidate_code_hash": stored_variant.candidate_code_hash,
        },
    )
    if expected_pair_id != manifest.pair_id:
        raise LF022RCPSmokeError("smoke pair ID differs from the bound variant")
    judge_source = PublicLeanJudgePair(
        pair_id=manifest.pair_id,
        canonical_lean_a=loaded.proposer_request.source.source_statement,
        canonical_lean_b=stored_variant.extracted_statement,
        optional_natural_language=loaded.problem.nl_statement,
        source_record_ids=(loaded.problem.source_record_id,),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )
    randomization_key = bytes.fromhex(
        hash_canonical(
            {
                "schema": "lf022_rcp_smoke_judge_randomization_v1",
                "config_hash": loaded.loaded_config.config_hash,
                "pair_id": manifest.pair_id,
            }
        )
    )
    judge_slots: tuple[JudgeSlot, JudgeSlot] = ("judge_A", "judge_B")
    expected_tasks: dict[tuple[JudgeSlot, JudgeOrientation], JudgePresentation] = {
        (slot, task.orientation): task
        for slot in judge_slots
        for task in make_swapped_presentations(
            source=judge_source,
            judge_slot=slot,
            randomization_key=randomization_key,
        )
    }
    evidence_by_call_id = {
        str(record.metadata.get("llm_call_id")): record for record in stored_evidence
    }
    if len(evidence_by_call_id) != 4:
        raise LF022RCPSmokeError("smoke evidence call lineage is incomplete")
    replayed_variant: VariantRecord | None = None
    replayed_evidence: list[EvidenceRecord] = []
    config = loaded.loaded_config.config
    for artifact in manifest.call_artifacts:
        bindings = (
            (artifact.provider_request_artifact, artifact.provider_request_sha256),
            (artifact.wire_request_artifact, artifact.wire_request_sha256),
            (artifact.wire_response_artifact, artifact.wire_response_sha256),
            (artifact.provider_raw_artifact, artifact.provider_raw_sha256),
            (artifact.llm_call_artifact, artifact.llm_call_sha256),
        )
        for relative, expected_sha in bindings:
            path = _safe_existing_artifact(
                repo_root,
                relative,
                label=f"{artifact.call_label} replay artifact",
            )
            if hash_file(path) != expected_sha:
                raise LF022RCPSmokeError(f"smoke replay artifact hash differs: {relative}")
        request_path = repo_root / artifact.provider_request_artifact
        request = load_provider_request(request_path)
        call_path = repo_root / artifact.llm_call_artifact
        call_raw = call_path.read_bytes()
        try:
            call = LLMCallRecord.model_validate_json(call_raw)
        except ValueError as exc:
            raise LF022RCPSmokeError(
                f"invalid persisted LLM call for {artifact.call_label}: {exc}"
            ) from exc
        if call_raw != canonical_json_bytes(call.model_dump(mode="json")) + b"\n":
            raise LF022RCPSmokeError(f"noncanonical persisted LLM call for {artifact.call_label}")

        task: JudgePresentation | None = None
        if artifact.call_label == "proposer":
            provider = config.providers.proposer
            expected_role = LLMRole.PROPOSER
            expected_input_ids = variant_provider_input_ids(loaded.proposer_request)
        elif artifact.call_label.startswith("judge_A_"):
            provider = config.providers.judge_A
            expected_role = LLMRole.JUDGE
            raw_orientation = artifact.call_label.removeprefix("judge_A_")
            if raw_orientation not in {"AB", "BA"}:
                raise LF022RCPSmokeError("judge_A call label has unknown orientation")
            orientation: JudgeOrientation = "AB" if raw_orientation == "AB" else "BA"
            task = expected_tasks.get(("judge_A", orientation))
            if task is None:
                raise LF022RCPSmokeError("judge_A call label has unknown orientation")
            expected_input_ids = judge_provider_input_ids(task)
        elif artifact.call_label.startswith("judge_B_"):
            provider = config.providers.judge_B
            expected_role = LLMRole.JUDGE
            raw_orientation = artifact.call_label.removeprefix("judge_B_")
            if raw_orientation not in {"AB", "BA"}:
                raise LF022RCPSmokeError("judge_B call label has unknown orientation")
            orientation = "AB" if raw_orientation == "AB" else "BA"
            task = expected_tasks.get(("judge_B", orientation))
            if task is None:
                raise LF022RCPSmokeError("judge_B call label has unknown orientation")
            expected_input_ids = judge_provider_input_ids(task)
        else:
            raise LF022RCPSmokeError(f"unknown smoke call label: {artifact.call_label}")
        if provider.decoding is None:
            raise LF022RCPSmokeError("replay provider lacks frozen decoding")
        expected_call_values = (
            artifact.llm_call_id,
            provider.provider_slot,
            provider.model_id,
            provider.family_id,
            artifact.model_revision,
            expected_role,
            expected_input_ids,
        )
        observed_call_values = (
            call.call_id,
            call.provider_slot,
            call.model,
            call.model_family,
            call.model_revision,
            call.role,
            call.input_ids,
        )
        if observed_call_values != expected_call_values:
            raise LF022RCPSmokeError(
                f"persisted LLM call bindings differ for {artifact.call_label}"
            )
        if expected_role is LLMRole.JUDGE and (
            len(call.input_ids) != 2
            or call.input_ids[0] != manifest.pair_id
            or not call.input_ids[1].startswith("judge_task:")
        ):
            raise LF022RCPSmokeError(f"judge task IDs differ for {artifact.call_label}")
        response = verify_generic_llm_call_artifacts(
            call=call,
            expected_role=expected_role,
            expected_input_ids=expected_input_ids,
            private_source_content=False,
            denylist_checked=True,
            denylist_hits=(),
            artifact_root=repo_root,
        )
        expected_wire = {
            "model": provider.model_id,
            "messages": _prompt_messages(request.rendered_prompt),
            **provider.decoding.wire_fields(),
        }
        wire_request_path = repo_root / artifact.wire_request_artifact
        if wire_request_path.read_bytes() != canonical_json_bytes(expected_wire) + b"\n":
            raise LF022RCPSmokeError(
                f"wire request differs from provider request for {artifact.call_label}"
            )
        wire_path = repo_root / artifact.wire_response_artifact
        content, metadata = extract_content_only(
            wire_path.read_bytes(), expected_model=artifact.model_id
        )
        if (
            sha256_hex(content.encode("utf-8")) != artifact.content_sha256
            or metadata != artifact.wire
            or response.output_text != content
            or call.metadata.get("wire_request_sha256") != artifact.wire_request_sha256
            or call.metadata.get("wire_response_sha256") != artifact.wire_response_sha256
        ):
            raise LF022RCPSmokeError(f"smoke content-only replay differs for {artifact.call_label}")
        parsed = (
            parse_variant_proposer_output(content)
            if expected_role is LLMRole.PROPOSER
            else parse_blinded_judge_output(content)
        )
        parsed_output = parsed.model_dump(
            mode="json",
            by_alias=expected_role is LLMRole.JUDGE,
        )
        if call.parsed_output != parsed_output:
            raise LF022RCPSmokeError(f"parsed payload replay differs for {artifact.call_label}")
        if expected_role is LLMRole.PROPOSER:
            variants = materialize_verified_provisional_variants(
                request=loaded.proposer_request,
                call=call,
                artifact_root=repo_root,
                generation_config_hash=loaded.loaded_config.config_hash,
                template_path=repo_root / config.prompts.proposer.artifact,
            )
            if len(variants) != 1 or variants[0].variant_id != manifest.variant_id:
                raise LF022RCPSmokeError("verified proposer materialization replay differs")
            replayed_variant = variants[0]
        else:
            assert task is not None
            persisted_evidence = evidence_by_call_id.get(call.call_id)
            if persisted_evidence is None:
                raise LF022RCPSmokeError(f"smoke evidence is missing for {artifact.call_label}")
            expected_evidence = materialize_verified_judgment_evidence(
                call=call,
                task=task,
                source=judge_source,
                family_matrix=config.providers.family_matrix(),
                proposer_family=config.providers.proposer.family_id,
                method_version="lf022_rcp_public_smoke_judgment_v1",
                config_hash=loaded.loaded_config.config_hash,
                artifact_root=repo_root,
                created_at=persisted_evidence.created_at,
                template_path=repo_root / config.prompts.judge.artifact,
            )
            expected_evidence = expected_evidence.model_copy(
                update={
                    "metadata": {
                        **expected_evidence.metadata,
                        "artifact_class": "smoke",
                        "supervision_eligible": False,
                        "training_eligible": False,
                        "evaluation_eligible": False,
                        "gate_credit_claimed": False,
                    }
                }
            )
            if expected_evidence != persisted_evidence:
                raise LF022RCPSmokeError(
                    f"verified judgment materialization differs for {artifact.call_label}"
                )
            replayed_evidence.append(expected_evidence)
    if replayed_variant is None:
        raise LF022RCPSmokeError("smoke replay lacks a verified proposer variant")
    stored_base = stored_variant.model_dump(mode="json")
    stored_base["validation_status"] = ValidationStatus.UNVALIDATED.value
    stored_metadata = dict(stored_base["metadata"])
    for key in (
        "artifact_class",
        "lean_request_hash",
        "lean_status",
        "lean_validation_artifact",
        "lean_validation_sha256",
        "semantic_label_created",
        "supervision_eligible",
        "training_eligible",
        "evaluation_eligible",
        "gate_credit_claimed",
    ):
        stored_metadata.pop(key, None)
    stored_base["metadata"] = stored_metadata
    if replayed_variant.model_dump(mode="json") != stored_base:
        raise LF022RCPSmokeError("stored validated variant differs from proposer replay")
    if len(replayed_evidence) != 4:
        raise LF022RCPSmokeError("smoke replay lacks four verified judgments")
    expected_consensus = build_weak_consensus_candidate(
        pair_id=manifest.pair_id,
        proposer_family=config.providers.proposer.family_id,
        judgments=tuple(replayed_evidence),
        created_at=stored_consensus.created_at,
        family_matrix=config.providers.family_matrix(),
    )
    expected_consensus = expected_consensus.model_copy(
        update={
            "metadata": {
                **expected_consensus.metadata,
                "artifact_class": "smoke",
                "semantic_label_created": False,
                "supervision_eligible": False,
                "training_eligible": False,
                "evaluation_eligible": False,
                "gate_credit_claimed": False,
            }
        }
    )
    if expected_consensus != stored_consensus:
        raise LF022RCPSmokeError("verified weak consensus replay differs")
    return manifest


def _load_failure_preflight(
    loaded: LoadedLF022RCPSmoke,
    *,
    failure: LF022RCPSmokeFailureManifest,
    run_root: Path,
    repo_root: Path,
) -> LF022RCPSmokePreflight:
    """Recover and fully verify the preflight bound by a terminal run lock."""

    lock_path = run_root / "run.lock"
    lock_raw = lock_path.read_bytes()
    lock = _strict_json_object(lock_raw, label="smoke failure run lock")
    expected_lock_keys = {"schema", "config_hash", "run_key", "preflight_id"}
    if set(lock) != expected_lock_keys:
        raise LF022RCPSmokeError("smoke failure run lock has unexpected fields")
    if lock_raw != canonical_json_bytes(lock) + b"\n":
        raise LF022RCPSmokeError("smoke failure run lock is not canonical JSON")
    preflight_id = lock.get("preflight_id")
    if (
        not isinstance(preflight_id, str)
        or re.fullmatch(r"lf022_rcp_preflight:[0-9a-f]{64}", preflight_id) is None
    ):
        raise LF022RCPSmokeError("smoke failure run lock has an invalid preflight ID")
    expected_lock = {
        "schema": "lf022_rcp_public_smoke_lock_v1",
        "config_hash": loaded.loaded_config.config_hash,
        "run_key": failure.run_key,
        "preflight_id": preflight_id,
    }
    if lock != expected_lock:
        raise LF022RCPSmokeError("smoke failure run lock bindings differ")

    config = loaded.loaded_config.config
    preflight_path = (
        repo_root
        / config.outputs.preflight_root
        / "preflight"
        / f"{preflight_id.removeprefix('lf022_rcp_preflight:')}.json"
    )
    if preflight_path.is_symlink() or not preflight_path.is_file():
        raise LF022RCPSmokeError("smoke failure preflight artifact is missing or unsafe")
    preflight_raw = preflight_path.read_bytes()
    try:
        preflight = LF022RCPSmokePreflight.model_validate_json(preflight_raw)
    except ValueError as exc:
        raise LF022RCPSmokeError(f"smoke failure preflight is invalid: {exc}") from exc
    if preflight_raw != canonical_json_bytes(preflight.model_dump(mode="json")) + b"\n":
        raise LF022RCPSmokeError("smoke failure preflight is not canonical JSON")
    if preflight.preflight_id != preflight_id:
        raise LF022RCPSmokeError("smoke failure preflight ID differs from run lock")
    expected_sources = (
        loaded.loaded_config.config_hash,
        loaded.problem.problem_record_id,
        loaded.theorem.theorem_id,
        loaded.representation.representation_id,
        loaded.problem.source_record_id,
        loaded.proposer_prompt.render_sha256,
    )
    observed_sources = (
        preflight.config_hash,
        preflight.problem_record_id,
        preflight.theorem_id,
        preflight.representation_id,
        preflight.source_record_id,
        preflight.proposer_prompt_sha256,
    )
    if observed_sources != expected_sources:
        raise LF022RCPSmokeError("smoke failure preflight source bindings differ")

    catalog_path = _safe_existing_artifact(
        repo_root,
        preflight.catalog_artifact,
        label="smoke failure preflight catalog",
    )
    catalog_raw = catalog_path.read_bytes()
    if sha256_hex(catalog_raw) != preflight.catalog.raw_response_sha256:
        raise LF022RCPSmokeError("smoke failure preflight catalog hash differs")
    document = _strict_json_object(catalog_raw, label="smoke failure RCP catalog")
    data = document.get("data")
    if not isinstance(data, list):
        raise LF022RCPSmokeError("smoke failure RCP catalog lacks a data array")
    model_ids = tuple(
        sorted(
            {
                item["id"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )
    )
    required = tuple(provider.model_id for provider in config.providers.enabled_rcp())
    if (
        preflight.catalog.endpoint_sha256
        != sha256_hex(
            (config.transport.expected_base_url + config.transport.catalog_path).encode("utf-8")
        )
        or preflight.catalog.canonical_model_ids_sha256 != hash_canonical(model_ids)
        or preflight.catalog.model_count != len(model_ids)
        or preflight.catalog.required_model_ids != required
        or any(model not in model_ids for model in required)
    ):
        raise LF022RCPSmokeError("smoke failure preflight catalog bindings differ")
    if _smoke_run_key(loaded, preflight) != failure.run_key:
        raise LF022RCPSmokeError("smoke failure run key differs from preflight")
    return preflight


def _load_failure_call(
    loaded: LoadedLF022RCPSmoke,
    *,
    preflight: LF022RCPSmokePreflight,
    run_root: Path,
    repo_root: Path,
    call_label: str,
    provider: SmokeProviderConfig,
    role: LLMRole,
    rendered_prompt: str,
    prompt_template_hash: str,
    prompt_template_id: str,
    prompt_template_version: str,
    input_ids: tuple[str, ...],
    parse: Callable[[str], StrictModel],
    parsed_dump_by_alias: bool,
) -> _FailureCallReplay:
    """Replay one attempted call from typed request, wire, and lineage artifacts."""

    if provider.decoding is None:
        raise LF022RCPSmokeError("failure replay provider lacks frozen decoding")
    call_dir = run_root / "calls" / call_label
    request_path = call_dir / "provider_request.json"
    wire_request_path = call_dir / "wire_request.json"
    lineage_path = call_dir / "llm_call.json"
    for path, label in (
        (request_path, "provider request"),
        (wire_request_path, "wire request"),
        (lineage_path, "LLM call"),
    ):
        if path.is_symlink() or not path.is_file():
            raise LF022RCPSmokeError(f"{call_label} failure replay lacks {label}")

    try:
        request = load_provider_request(request_path)
    except (OSError, ProviderError, ValueError) as exc:
        raise LF022RCPSmokeError(
            f"{call_label} failure replay provider request is invalid: {exc}"
        ) from exc
    expected_request = ProviderRequest.create(
        identity=ProviderIdentity(
            provider=provider.provider,
            model=provider.model_id,
            revision=preflight.catalog.route_snapshot_revision,
            transport="external_disabled",
        ),
        prompt_template_hash=prompt_template_hash,
        rendered_prompt=rendered_prompt,
        decoding=provider.decoding.provider_decoding(),
        input_ids=input_ids,
        private_source_content=False,
        attempt_index=0,
    )
    if request != expected_request:
        raise LF022RCPSmokeError(f"{call_label} failure replay provider request differs")
    expected_wire = {
        "model": provider.model_id,
        "messages": _prompt_messages(rendered_prompt),
        **provider.decoding.wire_fields(),
    }
    if wire_request_path.read_bytes() != canonical_json_bytes(expected_wire) + b"\n":
        raise LF022RCPSmokeError(f"{call_label} failure replay wire request differs")

    call_raw = lineage_path.read_bytes()
    try:
        call = LLMCallRecord.model_validate_json(call_raw)
    except ValueError as exc:
        raise LF022RCPSmokeError(f"{call_label} failure replay LLM call is invalid: {exc}") from exc
    if call_raw != canonical_json_bytes(call.model_dump(mode="json")) + b"\n":
        raise LF022RCPSmokeError(f"{call_label} failure replay LLM call is not canonical")
    expected_call_values = (
        provider.provider_slot,
        provider.model_id,
        provider.family_id,
        preflight.catalog.route_snapshot_revision,
        role,
        prompt_template_id,
        prompt_template_version,
        prompt_template_hash,
        request.prompt_render_hash,
        _repo_relative(request_path, repo_root),
        input_ids,
        request.decoding,
        request.request_hash,
        hash_file(request_path),
        "external",
        False,
        True,
        (),
        False,
    )
    observed_call_values = (
        call.provider_slot,
        call.model,
        call.model_family,
        call.model_revision,
        call.role,
        call.prompt_template_id,
        call.prompt_template_version,
        call.prompt_template_hash,
        call.prompt_render_hash,
        call.request_artifact,
        call.input_ids,
        call.decoding,
        call.provider_request_hash,
        call.request_artifact_sha256,
        call.execution_mode,
        call.private_source_content,
        call.denylist_checked,
        call.denylist_hits,
        call.supervision_eligible,
    )
    if observed_call_values != expected_call_values:
        raise LF022RCPSmokeError(f"{call_label} failure replay LLM call bindings differ")
    if (
        call.metadata.get("provider_protocol") != "provider_v1"
        or call.metadata.get("provider_request_hash") != request.request_hash
        or call.metadata.get("provider_attempt_id") != request.attempt_id
        or call.metadata.get("request_artifact_sha256") != hash_file(request_path)
        or call.metadata.get("wire_request_sha256") != hash_file(wire_request_path)
    ):
        raise LF022RCPSmokeError(f"{call_label} failure replay call metadata differs")
    if role is LLMRole.PROPOSER:
        if (
            call.metadata.get("generation_distribution") != "G_open"
            or call.metadata.get("generation_config_hash") != loaded.loaded_config.config_hash
            or call.metadata.get("semantic_labels_created") is not False
        ):
            raise LF022RCPSmokeError(f"{call_label} failure replay proposer metadata differs")
    else:
        judge_slot, separator, orientation = call_label.rpartition("_")
        if (
            not separator
            or judge_slot not in {"judge_A", "judge_B"}
            or orientation not in {"AB", "BA"}
            or call.metadata.get("weak_supervision_config_hash") != loaded.loaded_config.config_hash
            or call.metadata.get("proposer_family")
            != loaded.loaded_config.config.providers.proposer.family_id
            or call.metadata.get("judge_slot") != judge_slot
            or call.metadata.get("orientation") != orientation
        ):
            raise LF022RCPSmokeError(f"{call_label} failure replay judge metadata differs")

    if call.raw_output_artifact is None or call.raw_response_sha256 is None:
        raise LF022RCPSmokeError(f"{call_label} failure replay lacks raw-response bindings")
    raw_path = _safe_existing_artifact(
        repo_root,
        call.raw_output_artifact,
        label=f"{call_label} failure replay provider raw response",
    )
    provider_raw_bytes = raw_path.read_bytes()
    try:
        provider_raw = ProviderRawResponse.model_validate_json(provider_raw_bytes)
    except ValueError as exc:
        raise LF022RCPSmokeError(
            f"{call_label} failure replay provider raw response is invalid: {exc}"
        ) from exc
    if (
        provider_raw_bytes != canonical_json_bytes(provider_raw.model_dump(mode="json")) + b"\n"
        or hash_file(raw_path) != call.raw_response_sha256
        or call.metadata.get("raw_response_sha256") != call.raw_response_sha256
    ):
        raise LF022RCPSmokeError(f"{call_label} failure replay raw-response hash differs")
    expected_raw_bindings = (
        request.request_hash,
        request.attempt_id,
        request.attempt_index,
        request.is_retry,
        request.provider,
        request.model,
        request.revision,
        request.prompt_template_hash,
        request.prompt_render_hash,
        request.decoding_hash,
    )
    observed_raw_bindings = (
        provider_raw.request_hash,
        provider_raw.attempt_id,
        provider_raw.attempt_index,
        provider_raw.is_retry,
        provider_raw.provider,
        provider_raw.model,
        provider_raw.revision,
        provider_raw.prompt_template_hash,
        provider_raw.prompt_render_hash,
        provider_raw.decoding_hash,
    )
    if observed_raw_bindings != expected_raw_bindings:
        raise LF022RCPSmokeError(f"{call_label} failure replay raw response differs")

    wire_response_path = call_dir / "wire_response.json"
    content: str | None = None
    parsed: StrictModel | None = None
    terminal_error_type: str | None = None
    wire: SmokeWireMetadata | None = None
    if wire_response_path.is_file() and not wire_response_path.is_symlink():
        wire_response_sha = hash_file(wire_response_path)
        if call.metadata.get("wire_response_sha256") != wire_response_sha:
            raise LF022RCPSmokeError(f"{call_label} failure replay wire-response hash differs")
        try:
            content, wire = extract_content_only(
                wire_response_path.read_bytes(),
                expected_model=provider.model_id,
            )
        except Exception as exc:
            terminal_error_type = type(exc).__name__
            if (
                call.parse_status is not ParseStatus.EMPTY
                or call.terminal_status is not LLMCallStatus.EXHAUSTED
                or provider_raw.status != "error"
            ):
                raise LF022RCPSmokeError(
                    f"{call_label} failure replay extraction state differs"
                ) from exc
        else:
            if (
                provider_raw.status != "success"
                or provider_raw.output_text != content
                or call.terminal_status is not LLMCallStatus.COMPLETED
            ):
                raise LF022RCPSmokeError(
                    f"{call_label} failure replay successful response binding differs"
                )
            if call.parse_status is ParseStatus.PARSED:
                expected_wire_metadata: dict[str, object] = {
                    "returned_model": wire.returned_model,
                    "provider_request_id": wire.provider_request_id,
                    "reasoning_content_present": wire.reasoning_content_present,
                    "reasoning_present": wire.reasoning_present,
                    **{f"usage_{key}": value for key, value in sorted(wire.usage.items())},
                }
                if any(
                    call.metadata.get(key) != value for key, value in expected_wire_metadata.items()
                ):
                    raise LF022RCPSmokeError(f"{call_label} failure replay wire metadata differs")
                try:
                    parsed = parse(content)
                except Exception as exc:
                    raise LF022RCPSmokeError(
                        f"{call_label} persisted parsed output no longer parses"
                    ) from exc
                expected_parsed = parsed.model_dump(
                    mode="json",
                    by_alias=parsed_dump_by_alias,
                )
                if call.parsed_output != expected_parsed:
                    raise LF022RCPSmokeError(f"{call_label} failure replay parsed payload differs")
            elif call.parse_status is ParseStatus.PARSE_FAILED:
                if call.metadata.get("parse_failed") is not True:
                    raise LF022RCPSmokeError(
                        f"{call_label} failure replay parse-failure metadata differs"
                    )
                try:
                    parse(content)
                except Exception as exc:
                    terminal_error_type = type(exc).__name__
                else:
                    raise LF022RCPSmokeError(
                        f"{call_label} persisted parse failure now parses successfully"
                    )
            else:
                raise LF022RCPSmokeError(
                    f"{call_label} nonempty successful response has invalid parse state"
                )
    else:
        if (
            call.metadata.get("transport_failure") is not True
            or call.parse_status is not ParseStatus.EMPTY
            or call.terminal_status is not LLMCallStatus.EXHAUSTED
            or provider_raw.status != "error"
        ):
            raise LF022RCPSmokeError(f"{call_label} failure replay transport state differs")

    call_artifact_path = call_dir / "call_artifact.json"
    if call.parse_status is ParseStatus.PARSED:
        if (
            content is None
            or wire is None
            or call_artifact_path.is_symlink()
            or not call_artifact_path.is_file()
        ):
            raise LF022RCPSmokeError(f"{call_label} parsed call lacks its typed artifact")
        artifact_raw = call_artifact_path.read_bytes()
        try:
            artifact = SmokeCallArtifact.model_validate_json(artifact_raw)
        except ValueError as exc:
            raise LF022RCPSmokeError(
                f"{call_label} failure replay call artifact is invalid: {exc}"
            ) from exc
        if artifact_raw != canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n":
            raise LF022RCPSmokeError(f"{call_label} failure replay call artifact is not canonical")
        expected_artifact = SmokeCallArtifact(
            call_label=call_label,
            llm_call_id=call.call_id,
            role="proposer" if role is LLMRole.PROPOSER else "judge",
            provider_slot=provider.provider_slot,
            model_id=provider.model_id,
            model_family=provider.family_id,
            model_revision=preflight.catalog.route_snapshot_revision,
            provider_request_hash=request.request_hash,
            provider_request_artifact=_repo_relative(request_path, repo_root),
            provider_request_sha256=hash_file(request_path),
            wire_request_artifact=_repo_relative(wire_request_path, repo_root),
            wire_request_sha256=hash_file(wire_request_path),
            wire_response_artifact=_repo_relative(wire_response_path, repo_root),
            wire_response_sha256=hash_file(wire_response_path),
            provider_raw_artifact=_repo_relative(raw_path, repo_root),
            provider_raw_sha256=hash_file(raw_path),
            llm_call_artifact=_repo_relative(lineage_path, repo_root),
            llm_call_sha256=hash_file(lineage_path),
            content_sha256=sha256_hex(content.encode("utf-8")),
            wire=wire,
            parse_status="parsed",
        )
        if artifact != expected_artifact:
            raise LF022RCPSmokeError(f"{call_label} failure replay call artifact differs")
    elif call_artifact_path.exists():
        raise LF022RCPSmokeError(
            f"{call_label} non-parsed call unexpectedly has a typed call artifact"
        )
    return _FailureCallReplay(
        call_label=call_label,
        call=call,
        request=request,
        content=content,
        parsed=parsed,
        terminal_error_type=terminal_error_type,
    )


def _load_partial_failure_variant(
    loaded: LoadedLF022RCPSmoke,
    *,
    proposer: _FailureCallReplay,
    run_root: Path,
    repo_root: Path,
) -> tuple[VariantRecord | None, str | None]:
    """Verify an optional proposer materialization and Lean-validation prefix."""

    variant_path = run_root / "variant.json"
    lean_path = run_root / "lean_validation.json"
    if variant_path.exists() != lean_path.exists():
        raise LF022RCPSmokeError(
            "smoke failure partial variant and Lean validation must be present together"
        )
    if not variant_path.exists():
        return None, None
    if proposer.call.parse_status is not ParseStatus.PARSED:
        raise LF022RCPSmokeError("smoke failure has a variant from a non-parsed proposer")

    variant_bound = BoundArtifact(
        artifact=_repo_relative(variant_path, repo_root),
        sha256=hash_file(variant_path),
    )
    stored_variant = _load_bound_model(
        repo_root,
        variant_bound,
        VariantRecord,
        label="smoke failure partial variant",
    )
    assert isinstance(stored_variant, VariantRecord)
    lean_bound = BoundArtifact(
        artifact=_repo_relative(lean_path, repo_root),
        sha256=hash_file(lean_path),
    )
    lean_validation = _load_bound_model(
        repo_root,
        lean_bound,
        SmokeLeanValidationRecord,
        label="smoke failure partial Lean validation",
    )
    assert isinstance(lean_validation, SmokeLeanValidationRecord)
    if (
        stored_variant.metadata.get("artifact_class") != "smoke"
        or stored_variant.metadata.get("semantic_label_created") is not False
        or stored_variant.metadata.get("supervision_eligible") is not False
        or stored_variant.metadata.get("training_eligible") is not False
        or stored_variant.metadata.get("evaluation_eligible") is not False
        or stored_variant.metadata.get("gate_credit_claimed") is not False
    ):
        raise LF022RCPSmokeError("smoke failure partial variant quarantine differs")
    if (
        stored_variant.metadata.get("lean_request_hash") != lean_validation.request_hash
        or stored_variant.metadata.get("lean_status") != lean_validation.status
        or stored_variant.metadata.get("lean_validation_artifact") != lean_bound.artifact
        or stored_variant.metadata.get("lean_validation_sha256") != lean_bound.sha256
    ):
        raise LF022RCPSmokeError("smoke failure variant/Lean bindings differ")
    if lean_validation.raw_response_artifact is not None:
        assert lean_validation.raw_response_sha256 is not None
        raw_lean_path = _safe_existing_artifact(
            repo_root,
            lean_validation.raw_response_artifact,
            label="smoke failure Lean raw response",
        )
        if hash_file(raw_lean_path) != lean_validation.raw_response_sha256:
            raise LF022RCPSmokeError("smoke failure Lean raw response hash differs")

    variants = materialize_verified_provisional_variants(
        request=loaded.proposer_request,
        call=proposer.call,
        artifact_root=repo_root,
        generation_config_hash=loaded.loaded_config.config_hash,
        template_path=repo_root / loaded.loaded_config.config.prompts.proposer.artifact,
    )
    if len(variants) != 1:
        raise LF022RCPSmokeError("smoke failure proposer materialization count differs")
    replayed_variant = variants[0]
    stored_base = stored_variant.model_dump(mode="json")
    stored_base["validation_status"] = ValidationStatus.UNVALIDATED.value
    stored_metadata = dict(stored_base["metadata"])
    for key in (
        "artifact_class",
        "lean_request_hash",
        "lean_status",
        "lean_validation_artifact",
        "lean_validation_sha256",
        "semantic_label_created",
        "supervision_eligible",
        "training_eligible",
        "evaluation_eligible",
        "gate_credit_claimed",
    ):
        stored_metadata.pop(key, None)
    stored_base["metadata"] = stored_metadata
    if replayed_variant.model_dump(mode="json") != stored_base:
        raise LF022RCPSmokeError("smoke failure stored variant differs from proposer replay")

    assert stored_variant.extracted_statement is not None
    declaration_name = _candidate_name(stored_variant.extracted_statement)
    namespace_name = "LeanFaithLF022Smoke.Run" + stored_variant.variant_id.removeprefix("var:")[:16]
    expected_lean_code = (
        f"{loaded.import_header.rstrip()}\n"
        f"namespace {namespace_name}\n"
        f"{stored_variant.extracted_statement} := by sorry\n"
        f"end {namespace_name}\n"
    )
    full_names = tuple(
        str(item.get("full_name"))
        for item in lean_validation.declarations
        if item.get("kind") in {"theorem", "lemma"}
    )
    if lean_validation.code_sha256 != sha256_hex(
        expected_lean_code.encode("utf-8")
    ) or full_names != (f"{namespace_name}.{declaration_name}",):
        raise LF022RCPSmokeError("smoke failure Lean validation replay differs")
    pair_id = make_id(
        PAIR_PREFIX,
        {
            "schema": "lf022_rcp_public_smoke_pair_v1",
            "source_theorem_id": loaded.theorem.theorem_id,
            "variant_id": stored_variant.variant_id,
            "candidate_code_hash": stored_variant.candidate_code_hash,
        },
    )
    return stored_variant, pair_id


def replay_public_smoke_failure(
    loaded: LoadedLF022RCPSmoke,
    *,
    failure_manifest_path: Path,
    repo_root: Path,
) -> LF022RCPSmokeFailureManifest:
    """Strictly replay a terminal partial run without performing network I/O."""

    if failure_manifest_path.is_symlink() or not failure_manifest_path.is_file():
        raise LF022RCPSmokeError("smoke failure manifest is missing or unsafe")
    try:
        raw = failure_manifest_path.read_bytes()
        failure = LF022RCPSmokeFailureManifest.model_validate_json(raw)
    except ValueError as exc:
        raise LF022RCPSmokeError(f"smoke failure manifest is invalid: {exc}") from exc
    if raw != canonical_json_bytes(failure.model_dump(mode="json")) + b"\n":
        raise LF022RCPSmokeError("smoke failure manifest is not canonical JSON")
    if failure.config_hash != loaded.loaded_config.config_hash:
        raise LF022RCPSmokeError("smoke failure config hash differs")
    config = loaded.loaded_config.config
    run_root = repo_root / config.outputs.raw_root / failure.run_key
    expected_failure_path = run_root / "failure_manifest.json"
    if failure_manifest_path.resolve() != expected_failure_path.resolve():
        raise LF022RCPSmokeError("smoke failure manifest path differs from its run key")
    actual_artifacts = tuple(
        SmokeFailureArtifact(
            artifact=_repo_relative(path, repo_root),
            sha256=hash_file(path),
        )
        for path in sorted(run_root.rglob("*"))
        if path.is_file() and not path.is_symlink() and path != expected_failure_path
    )
    if actual_artifacts != failure.artifacts:
        raise LF022RCPSmokeError("smoke failure artifact inventory differs from the run tree")
    attempts = sum(
        artifact.artifact.endswith("/wire_request.json") for artifact in failure.artifacts
    )
    completed = sum(
        artifact.artifact.endswith("/call_artifact.json") for artifact in failure.artifacts
    )
    if attempts != failure.chat_completion_attempts or completed != failure.completed_call_count:
        raise LF022RCPSmokeError("smoke failure call accounting differs")
    preflight = _load_failure_preflight(
        loaded,
        failure=failure,
        run_root=run_root,
        repo_root=repo_root,
    )
    actual_call_labels = {
        PurePosixPath(artifact.artifact).parent.name
        for artifact in failure.artifacts
        if artifact.artifact.endswith("/wire_request.json")
    }
    if len(actual_call_labels) != attempts:
        raise LF022RCPSmokeError("smoke failure contains duplicate or ambiguous call attempts")
    if attempts == 0:
        if (run_root / "variant.json").exists() or (run_root / "lean_validation.json").exists():
            raise LF022RCPSmokeError("zero-call smoke failure contains downstream artifacts")
        if {artifact.artifact for artifact in failure.artifacts} != {
            _repo_relative(run_root / "run.lock", repo_root)
        }:
            raise LF022RCPSmokeError("zero-call smoke failure contains untyped lineage artifacts")
        return failure

    proposer = _load_failure_call(
        loaded,
        preflight=preflight,
        run_root=run_root,
        repo_root=repo_root,
        call_label="proposer",
        provider=config.providers.proposer,
        role=LLMRole.PROPOSER,
        rendered_prompt=loaded.proposer_prompt.text,
        prompt_template_hash=loaded.proposer_prompt.template_sha256,
        prompt_template_id=loaded.proposer_prompt.template_id,
        prompt_template_version=loaded.proposer_prompt.template_version,
        input_ids=variant_provider_input_ids(loaded.proposer_request),
        parse=parse_variant_proposer_output,
        parsed_dump_by_alias=False,
    )
    stored_variant, pair_id = _load_partial_failure_variant(
        loaded,
        proposer=proposer,
        run_root=run_root,
        repo_root=repo_root,
    )
    expected_call_order = ["proposer"]
    judge_replays: list[_FailureCallReplay] = []
    if attempts > 1:
        if stored_variant is None or pair_id is None:
            raise LF022RCPSmokeError("judge attempt lacks a verified proposer variant")
        assert stored_variant.extracted_statement is not None
        judge_source = PublicLeanJudgePair(
            pair_id=pair_id,
            canonical_lean_a=loaded.proposer_request.source.source_statement,
            canonical_lean_b=stored_variant.extracted_statement,
            optional_natural_language=loaded.problem.nl_statement,
            source_record_ids=(loaded.problem.source_record_id,),
            source_is_public=True,
            private_source_content=False,
            external_transmission_allowed=True,
            denylist_checked=True,
            denylist_hits=(),
        )
        randomization_key = bytes.fromhex(
            hash_canonical(
                {
                    "schema": "lf022_rcp_smoke_judge_randomization_v1",
                    "config_hash": loaded.loaded_config.config_hash,
                    "pair_id": pair_id,
                }
            )
        )
        judge_prompt_path = repo_root / config.prompts.judge.artifact
        judge_specs: tuple[tuple[JudgeSlot, SmokeProviderConfig], ...] = (
            ("judge_A", config.providers.judge_A),
            ("judge_B", config.providers.judge_B),
        )
        expected_judges: list[
            tuple[str, SmokeProviderConfig, JudgePresentation, RenderedJudgePrompt]
        ] = []
        for slot, provider in judge_specs:
            for task in make_swapped_presentations(
                source=judge_source,
                judge_slot=slot,
                randomization_key=randomization_key,
            ):
                rendered = render_blinded_judge_prompt(task, template_path=judge_prompt_path)
                label = f"{slot}_{task.orientation}"
                expected_call_order.append(label)
                expected_judges.append((label, provider, task, rendered))
        if attempts > len(expected_call_order):
            raise LF022RCPSmokeError("smoke failure exceeds the five-call budget")
        for label, provider, task, rendered in expected_judges[: attempts - 1]:
            judge_replays.append(
                _load_failure_call(
                    loaded,
                    preflight=preflight,
                    run_root=run_root,
                    repo_root=repo_root,
                    call_label=label,
                    provider=provider,
                    role=LLMRole.JUDGE,
                    rendered_prompt=rendered.text,
                    prompt_template_hash=rendered.template_sha256,
                    prompt_template_id=rendered.template_id,
                    prompt_template_version=rendered.template_version,
                    input_ids=judge_provider_input_ids(task),
                    parse=parse_blinded_judge_output,
                    parsed_dump_by_alias=True,
                )
            )
    expected_attempted_labels = set(expected_call_order[:attempts])
    if actual_call_labels != expected_attempted_labels:
        raise LF022RCPSmokeError("smoke failure call attempts are not an execution prefix")
    replayed_calls = [proposer, *judge_replays]
    if sum(item.call.parse_status is ParseStatus.PARSED for item in replayed_calls) != completed:
        raise LF022RCPSmokeError("smoke failure completed-call accounting differs from lineage")
    terminal_type = replayed_calls[-1].terminal_error_type
    if terminal_type is not None and terminal_type != failure.error_type:
        raise LF022RCPSmokeError(
            "smoke failure terminal error type differs from deterministic replay"
        )
    expected_inventory = {_repo_relative(run_root / "run.lock", repo_root)}
    for replayed in replayed_calls:
        call_dir = run_root / "calls" / replayed.call_label
        expected_inventory.update(
            {
                _repo_relative(call_dir / "provider_request.json", repo_root),
                _repo_relative(call_dir / "wire_request.json", repo_root),
                _repo_relative(call_dir / "llm_call.json", repo_root),
            }
        )
        assert replayed.call.raw_output_artifact is not None
        expected_inventory.add(replayed.call.raw_output_artifact)
        wire_response_path = call_dir / "wire_response.json"
        if wire_response_path.is_file():
            expected_inventory.add(_repo_relative(wire_response_path, repo_root))
        if replayed.call.parse_status is ParseStatus.PARSED:
            expected_inventory.add(_repo_relative(call_dir / "call_artifact.json", repo_root))
    if stored_variant is not None:
        expected_inventory.update(
            {
                _repo_relative(run_root / "variant.json", repo_root),
                _repo_relative(run_root / "lean_validation.json", repo_root),
            }
        )
    observed_inventory = {artifact.artifact for artifact in failure.artifacts}
    if observed_inventory != expected_inventory:
        raise LF022RCPSmokeError(
            "smoke failure inventory contains untyped or missing lineage artifacts"
        )
    return failure


__all__ = [
    "LF022RCPSmokeArtifactConflict",
    "LF022RCPSmokeCatalogError",
    "LF022RCPSmokeConfig",
    "LF022RCPSmokeConfigError",
    "LF022RCPSmokeCredentialError",
    "LF022RCPSmokeError",
    "LF022RCPSmokeFailureManifest",
    "LF022RCPSmokeManifest",
    "LF022RCPSmokePreflight",
    "LoadedLF022RCPSmoke",
    "SmokeExecutionRun",
    "SmokePreflightRun",
    "execute_public_smoke",
    "extract_content_only",
    "load_lf022_rcp_smoke",
    "probe_and_write_smoke_preflight",
    "replay_public_smoke",
    "replay_public_smoke_failure",
    "resolve_smoke_credentials",
]
