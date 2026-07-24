"""One-problem, hash-bound EPFL RCP Qwen qualification for LF-021.

This module is intentionally separate from the frozen Kimi qualification.  It
permits exactly one live catalog request and exactly one chat-completion
request for ``Qwen/Qwen3.6-35B-A3B``.  The chat request carries the complete
Qwen decoding envelope; a rejected request is persisted and never retried
with fields removed.

An HTTP-200 response proves only that RCP accepted the combined request.  It
does not prove that every sampling or chat-template field was applied.  That
distinction is persisted explicitly in the capability evidence.

The sole input is a public, reference-hidden problem.  Outputs are operational
artifacts only: no semantic label, supervision eligibility, Gate credit,
held-out claim, unseen claim, or evaluation claim is created.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation import rcp_qualification_v1 as shared
from leanfaith.generation.prompts import (
    DirectOutputParseError,
    ParsedLeanDeclaration,
    parse_direct_autoformalization_output,
)
from leanfaith.generation.providers import (
    DecodingValue,
    ProviderIdentity,
    ProviderRawResponse,
    ProviderRequest,
    create_provider_request_for_problem,
    persist_provider_raw_response,
    persist_provider_request,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import TheoremRecord

_HEX64 = r"^[0-9a-f]{64}$"
_PRIMARY_MODEL = "Qwen/Qwen3.6-35B-A3B"
_ABLATION_MODEL = "Qwen/Qwen3.5-397B-A17B"
_FAMILY = "qwen3"
_PROMPT_HASH_TOKEN = "{{PROMPT_TEMPLATE_SHA256}}"
_PROBLEM_JSON_TOKEN = "{{PROBLEM_JSON}}"
_DECLARATION_TOKEN = "{{DECLARATION_NAME}}"
_TEMPLATE_TOKEN = re.compile(r"\{\{[A-Z0-9_]+\}\}")
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_AUTHORIZATION_PATTERN = re.compile(r'(?i)"authorization"\s*:')
_ENV_NAME = "RCP_API_KEY"


class RCPQwenQualificationError(RuntimeError):
    """A Qwen qualification invariant failed."""


class RCPQwenArtifactConflict(RCPQwenQualificationError):
    """An immutable output path already contains different bytes."""


class RCPQwenTerminalStatus(StrEnum):
    RAW_COLLECTED = "raw_collected"
    PARSE_FAILED = "parse_failed"
    REQUEST_FAILED = "request_failed"


class BoundArtifact(StrictModel):
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _relative(self) -> Self:
        path = PurePosixPath(self.artifact)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("bound artifact paths must be repository-relative")
        return self


class QwenTransportConfig(StrictModel):
    transport_id: Literal["epfl_rcp_openai_compatible_qwen_v1"]
    provenance: Literal["remote_on_prem_epfl_rcp"]
    base_url_env: Literal["RCP_BASE_URL"]
    api_key_env: Literal["RCP_API_KEY"]
    expected_base_url: Literal["https://inference.rcp.epfl.ch/v1"]
    catalog_path: Literal["/models"]
    chat_completions_path: Literal["/chat/completions"]


class QwenProblemBinding(StrictModel):
    records_artifact: str
    records_sha256: str = Field(pattern=_HEX64)
    reference_theorems_artifact: str
    reference_theorems_sha256: str = Field(pattern=_HEX64)
    expected_problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    required_private_source_content: Literal[False] = False
    required_external_provider_eligible: Literal[True] = True
    require_reference_hidden: Literal[True] = True


class QwenPromptBinding(StrictModel):
    artifact: str
    sha256: str = Field(pattern=_HEX64)
    declaration_name: Literal["leanfaith_rcp_qwen_qualification_v1"]
    system_message: str = Field(min_length=1)


class QwenDecodingConfig(StrictModel):
    temperature: float = Field(default=0.6, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, ge=0.0, le=1.0)
    top_k: Literal[20] = 20
    min_p: float = Field(default=0.0, ge=0.0, le=1.0)
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    repetition_penalty: float = Field(default=1.0, ge=0.0)
    max_tokens: Literal[4096] = 4096
    stream: Literal[False] = False
    chat_template_enable_thinking: Literal[True] = True

    @model_validator(mode="after")
    def _exact_qwen_values(self) -> Self:
        observed = (
            self.temperature,
            self.top_p,
            self.top_k,
            self.min_p,
            self.presence_penalty,
            self.repetition_penalty,
        )
        if observed != (0.6, 0.95, 20, 0.0, 0.0, 1.0):
            raise ValueError("Qwen decoding differs from the frozen exact envelope")
        return self

    def provider_decoding(self) -> dict[str, DecodingValue]:
        """Flatten all wire controls into the canonical request identity."""

        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "presence_penalty": self.presence_penalty,
            "repetition_penalty": self.repetition_penalty,
            "max_tokens": self.max_tokens,
            "stream": self.stream,
            "chat_template_enable_thinking": self.chat_template_enable_thinking,
        }

    def wire_fields(self) -> dict[str, object]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "presence_penalty": self.presence_penalty,
            "repetition_penalty": self.repetition_penalty,
            "max_tokens": self.max_tokens,
            "stream": self.stream,
            "chat_template_kwargs": {
                "enable_thinking": self.chat_template_enable_thinking,
            },
        }


class QwenRequestBudget(StrictModel):
    maximum_catalog_requests: Literal[1] = 1
    maximum_chat_completion_requests: Literal[1] = 1
    maximum_dedicated_capability_requests: Literal[0] = 0
    retries_with_removed_fields: Literal[0] = 0
    retry_attempts: Literal[1] = 1
    request_timeout_seconds: int = Field(ge=1, le=3600)
    catalog_timeout_seconds: int = Field(ge=1, le=300)


class QwenCapabilityPolicy(StrictModel):
    combined_generation_request_is_capability_observation: Literal[True] = True
    exact_application_proof_available: Literal[False] = False
    accepted_request_claim_only: Literal[True] = True
    unsupported_fields_fail_closed: Literal[True] = True
    silently_dropped_fields_forbidden: Literal[True] = True
    field_removal_retry_forbidden: Literal[True] = True
    required_wire_fields: tuple[
        Literal[
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
            "max_tokens",
            "stream",
            "chat_template_kwargs",
        ],
        ...,
    ]

    @model_validator(mode="after")
    def _exact_fields(self) -> Self:
        expected = (
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
            "max_tokens",
            "stream",
            "chat_template_kwargs",
        )
        if self.required_wire_fields != expected:
            raise ValueError("Qwen required wire field order/content differs")
        return self


class QwenResearchPolicy(StrictModel):
    public_source_only: Literal[True] = True
    private_source_transmission_forbidden: Literal[True] = True
    reference_transmission_forbidden: Literal[True] = True
    primary_generation_authorized: Literal[True] = True
    ablation_generation_authorized: Literal[False] = False
    bulk_generation_authorized: Literal[False] = False
    all_qwen_variants_one_family: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    semantic_faithfulness_assessed: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False
    checkpoint_revision_status: Literal["unavailable_from_rcp_route_ids"]
    training_cutoff_status: Literal["unknown"]
    contamination_status: Literal["unknown"]
    unseen_claim_eligible: Literal[False] = False
    heldout_claim_eligible: Literal[False] = False
    evaluation_claim_eligible: Literal[False] = False
    allowed_use: Literal["supplemental_generator_candidates_only"]


class QwenOutputConfig(StrictModel):
    root: str
    lean_raw_root: str
    audit_root: str

    @model_validator(mode="after")
    def _relative(self) -> Self:
        for value in (self.root, self.lean_raw_root, self.audit_root):
            path = PurePosixPath(value)
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise ValueError("Qwen output roots must be repository-relative")
        return self


class QwenQualificationConfig(StrictModel):
    schema_version: Literal[1] = 1
    config_id: Literal["lf021_rcp_qwen_qualification_v1"]
    frozen_at: datetime.datetime
    status: Literal["one_problem_execution_authorized"]
    artifact_class: Literal["qualification"]
    transport: QwenTransportConfig
    primary_model_id: Literal["Qwen/Qwen3.6-35B-A3B"]
    no_call_ablation_model_id: Literal["Qwen/Qwen3.5-397B-A17B"]
    provider_family: Literal["qwen3"]
    diversity_group: Literal["qwen3"]
    all_qwen_checkpoints_one_family: Literal[True] = True
    problem: QwenProblemBinding
    prompt: QwenPromptBinding
    decoding: QwenDecodingConfig
    request_budget: QwenRequestBudget
    capability_policy: QwenCapabilityPolicy
    policy: QwenResearchPolicy
    outputs: QwenOutputConfig
    bound_artifacts: dict[str, BoundArtifact]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.frozen_at.tzinfo is None or self.frozen_at.utcoffset() != datetime.timedelta(0):
            raise ValueError("Qwen frozen_at must be UTC")
        required = {
            "shared_transport_module",
            "engine_module",
            "cli_script",
            "prompt_template",
            "response_contract",
            "proposal",
            "execution_policy",
            "provider_portfolio",
            "remote_generation_policy",
        }
        if set(self.bound_artifacts) != required:
            raise ValueError("Qwen bound artifact inventory differs")
        return self


class QwenReferenceBlindAudit(StrictModel):
    schema_version: Literal[1] = 1
    problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    rendered_prompt_sha256: str = Field(pattern=_HEX64)
    reference_theorem_ids_absent: Literal[True] = True
    reference_declaration_names_absent: Literal[True] = True
    reference_signatures_absent: Literal[True] = True
    source_links_absent: Literal[True] = True
    private_source_content_absent: Literal[True] = True
    provider_payload_keys: tuple[str, ...]
    reference_transmission_performed: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False


class QwenCatalogObservation(StrictModel):
    schema_version: Literal[1] = 1
    observation_id: str = Field(pattern=r"^rcp_qwen_catalog_observation:[0-9a-f]{64}$")
    transport: Literal["remote_on_prem_epfl_rcp"]
    endpoint_sha256: str = Field(pattern=_HEX64)
    observed_at: datetime.datetime
    http_status: Literal[200] = 200
    raw_response_artifact: str
    raw_response_sha256: str = Field(pattern=_HEX64)
    canonical_model_ids_sha256: str = Field(pattern=_HEX64)
    model_count: int = Field(ge=1)
    primary_model_id: Literal["Qwen/Qwen3.6-35B-A3B"]
    primary_present: Literal[True] = True
    no_call_ablation_model_id: Literal["Qwen/Qwen3.5-397B-A17B"]
    no_call_ablation_present: bool
    catalog_requests_performed: Literal[1] = 1
    credential_serialized: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "observation_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.observed_at.tzinfo is None:
            raise ValueError("catalog timestamp must be timezone-aware")
        expected = "rcp_qwen_catalog_observation:" + hash_canonical(
            {"schema": "lf021_rcp_qwen_catalog_observation_v1", **self.id_payload()}
        )
        if self.observation_id != expected:
            raise ValueError("Qwen catalog observation ID differs")
        return self


class QwenInvocation(StrictModel):
    schema_version: Literal[1] = 1
    invocation_id: str = Field(pattern=r"^rcp_qwen_invocation:[0-9a-f]{64}$")
    config_file_sha256: str = Field(pattern=_HEX64)
    config_hash: str = Field(pattern=_HEX64)
    bound_artifact_hashes: dict[str, str]
    catalog_observation_id: str = Field(pattern=r"^rcp_qwen_catalog_observation:[0-9a-f]{64}$")
    model_id: Literal["Qwen/Qwen3.6-35B-A3B"]
    no_call_ablation_model_id: Literal["Qwen/Qwen3.5-397B-A17B"]
    no_call_ablation_requests_performed: Literal[0] = 0
    provider_family: Literal["qwen3"]
    diversity_group: Literal["qwen3"]
    all_qwen_checkpoints_one_family: Literal[True] = True
    problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    prompt_template_sha256: str = Field(pattern=_HEX64)
    rendered_prompt_sha256: str = Field(pattern=_HEX64)
    decoding: dict[str, DecodingValue]
    decoding_sha256: str = Field(pattern=_HEX64)
    wire_field_names: tuple[str, ...]
    capability_claim: Literal["combined_request_acceptance_only_application_unproven"]
    reference_hidden: Literal[True] = True
    private_source_content: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    semantic_faithfulness_assessed: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "invocation_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.decoding_sha256 != hash_canonical(self.decoding):
            raise ValueError("Qwen invocation decoding hash differs")
        expected = "rcp_qwen_invocation:" + hash_canonical(
            {"schema": "lf021_rcp_qwen_invocation_v1", **self.id_payload()}
        )
        if self.invocation_id != expected:
            raise ValueError("Qwen invocation ID differs")
        return self


class QwenCapabilityEvidence(StrictModel):
    schema_version: Literal[1] = 1
    invocation_id: str = Field(pattern=r"^rcp_qwen_invocation:[0-9a-f]{64}$")
    observation_method: Literal["single_combined_theorem_generation_request"]
    dedicated_capability_requests_performed: Literal[0] = 0
    chat_completion_requests_performed: Literal[1] = 1
    complete_frozen_field_set_sent: Literal[True] = True
    fields_removed_or_retried: Literal[False] = False
    http_status: int | None = Field(default=None, ge=100, le=599)
    combined_request_accepted: bool
    exact_field_application_proven: Literal[False] = False
    per_field_status: dict[
        str,
        Literal[
            "accepted_in_combined_request_application_unproven",
            "request_failed_application_unproven",
        ],
    ]
    reasoning_content_observed: bool
    reasoning_content_characters: int = Field(ge=0)
    claim: Literal[
        "route_accepted_complete_payload_application_unproven",
        "request_failed_no_field_support_claim",
    ]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected = {
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
            "max_tokens",
            "stream",
            "chat_template_kwargs",
        }
        if set(self.per_field_status) != expected:
            raise ValueError("capability evidence field inventory differs")
        accepted_status = "accepted_in_combined_request_application_unproven"
        if self.combined_request_accepted:
            if self.http_status != 200 or set(self.per_field_status.values()) != {accepted_status}:
                raise ValueError("accepted capability evidence is incoherent")
            if self.claim != "route_accepted_complete_payload_application_unproven":
                raise ValueError("accepted capability claim differs")
        elif self.claim != "request_failed_no_field_support_claim":
            raise ValueError("failed capability claim differs")
        return self


class QwenAttemptRecord(StrictModel):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^rcp_qwen_attempt:[0-9a-f]{64}$")
    invocation_id: str = Field(pattern=r"^rcp_qwen_invocation:[0-9a-f]{64}$")
    provider_request_hash: str = Field(pattern=_HEX64)
    provider_attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    request_artifact: str
    request_sha256: str = Field(pattern=_HEX64)
    wire_request_artifact: str
    wire_request_sha256: str = Field(pattern=_HEX64)
    wire_response_artifact: str
    wire_response_sha256: str = Field(pattern=_HEX64)
    provider_response_artifact: str
    provider_response_sha256: str = Field(pattern=_HEX64)
    capability_evidence_artifact: str
    capability_evidence_sha256: str = Field(pattern=_HEX64)
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_request_id: str | None = None
    returned_model: str | None = None
    status: Literal["response_received", "request_failed"]
    error_code: str | None = None
    error_detail: str | None = None
    tokens: dict[str, int] = Field(default_factory=dict)
    started_at: datetime.datetime
    completed_at: datetime.datetime
    latency_ms: int = Field(ge=0)
    chat_completion_requests_performed: Literal[1] = 1
    retries_performed: Literal[0] = 0
    api_key_serialized: Literal[False] = False

    def stable_payload(self) -> dict[str, object]:
        return {
            "invocation_id": self.invocation_id,
            "provider_request_hash": self.provider_request_hash,
            "provider_attempt_id": self.provider_attempt_id,
            "request_sha256": self.request_sha256,
            "wire_request_sha256": self.wire_request_sha256,
            "wire_response_sha256": self.wire_response_sha256,
            "provider_response_sha256": self.provider_response_sha256,
            "capability_evidence_sha256": self.capability_evidence_sha256,
            "http_status": self.http_status,
            "provider_request_id": self.provider_request_id,
            "returned_model": self.returned_model,
            "status": self.status,
            "error_code": self.error_code,
            "tokens": self.tokens,
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("attempt timestamps must be timezone-aware")
        expected_latency = max(
            0,
            int((self.completed_at - self.started_at).total_seconds() * 1000),
        )
        if self.latency_ms != expected_latency:
            raise ValueError("Qwen attempt latency differs")
        expected = "rcp_qwen_attempt:" + hash_canonical(
            {"schema": "lf021_rcp_qwen_attempt_v1", **self.stable_payload()}
        )
        if self.attempt_id != expected:
            raise ValueError("Qwen attempt ID differs")
        if self.status == "response_received" and self.http_status != 200:
            raise ValueError("successful Qwen attempt requires HTTP 200")
        return self


class QwenTerminal(StrictModel):
    schema_version: Literal[1] = 1
    terminal_id: str = Field(pattern=r"^rcp_qwen_terminal:[0-9a-f]{64}$")
    invocation_id: str = Field(pattern=r"^rcp_qwen_invocation:[0-9a-f]{64}$")
    attempt_id: str = Field(pattern=r"^rcp_qwen_attempt:[0-9a-f]{64}$")
    model_id: Literal["Qwen/Qwen3.6-35B-A3B"]
    status: RCPQwenTerminalStatus
    output_sha256: str | None = Field(default=None, pattern=_HEX64)
    parsed_statement_sha256: str | None = Field(default=None, pattern=_HEX64)
    parse_error_code: str | None = None
    chat_completion_requests_performed: Literal[1] = 1
    retries_performed: Literal[0] = 0
    no_call_ablation_requests_performed: Literal[0] = 0
    reference_transmission_performed: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    semantic_faithfulness_assessed: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "terminal_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        expected = "rcp_qwen_terminal:" + hash_canonical(
            {"schema": "lf021_rcp_qwen_terminal_v1", **self.id_payload()}
        )
        if self.terminal_id != expected:
            raise ValueError("Qwen terminal ID differs")
        if self.status is RCPQwenTerminalStatus.RAW_COLLECTED and (
            self.output_sha256 is None or self.parsed_statement_sha256 is None
        ):
            raise ValueError("raw-collected terminal lacks parsed output")
        return self


class QwenOperationalValidation(StrictModel):
    schema_version: Literal[1] = 1
    validation_id: str = Field(pattern=r"^rcp_qwen_operational_validation:[0-9a-f]{64}$")
    invocation_id: str = Field(pattern=r"^rcp_qwen_invocation:[0-9a-f]{64}$")
    parsed_statement_sha256: str = Field(pattern=_HEX64)
    lean_request_hash: str = Field(pattern=_HEX64)
    lean_raw_artifact: str
    lean_raw_sha256: str = Field(pattern=_HEX64)
    status: Literal["valid_with_sorry"]
    declaration_name: Literal["leanfaith_rcp_qwen_qualification_v1"]
    declaration_count: Literal[1] = 1
    sorry_count: int = Field(ge=1)
    semantic_faithfulness_assessed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "validation_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        expected = "rcp_qwen_operational_validation:" + hash_canonical(
            {"schema": "lf021_rcp_qwen_operational_validation_v1", **self.id_payload()}
        )
        if self.validation_id != expected:
            raise ValueError("Qwen operational validation ID differs")
        return self


class QwenManifest(StrictModel):
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=r"^rcp_qwen_manifest:[0-9a-f]{64}$")
    config_file_sha256: str = Field(pattern=_HEX64)
    config_hash: str = Field(pattern=_HEX64)
    bound_artifact_hashes: dict[str, str]
    run_key: str = Field(pattern=_HEX64)
    output_directory: str
    catalog_observation_id: str = Field(pattern=r"^rcp_qwen_catalog_observation:[0-9a-f]{64}$")
    invocation_id: str = Field(pattern=r"^rcp_qwen_invocation:[0-9a-f]{64}$")
    attempt_id: str = Field(pattern=r"^rcp_qwen_attempt:[0-9a-f]{64}$")
    terminal_id: str = Field(pattern=r"^rcp_qwen_terminal:[0-9a-f]{64}$")
    model_id: Literal["Qwen/Qwen3.6-35B-A3B"]
    terminal_status: RCPQwenTerminalStatus
    catalog_requests_performed: Literal[1] = 1
    chat_completion_requests_performed: Literal[1] = 1
    dedicated_capability_requests_performed: Literal[0] = 0
    no_call_ablation_requests_performed: Literal[0] = 0
    reference_transmission_performed: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    semantic_faithfulness_assessed: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False
    checkpoint_revision_status: Literal["unavailable_from_rcp_route_ids"]
    training_cutoff_status: Literal["unknown"]
    contamination_status: Literal["unknown"]
    unseen_claim_eligible: Literal[False] = False
    heldout_claim_eligible: Literal[False] = False
    evaluation_claim_eligible: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "manifest_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        expected = "rcp_qwen_manifest:" + hash_canonical(
            {"schema": "lf021_rcp_qwen_manifest_v1", **self.id_payload()}
        )
        if self.manifest_id != expected:
            raise ValueError("Qwen manifest ID differs")
        return self


class QwenVerificationReport(StrictModel):
    schema_version: Literal[1] = 1
    verification_id: str = Field(pattern=r"^rcp_qwen_verification:[0-9a-f]{64}$")
    run_key: str = Field(pattern=_HEX64)
    manifest_id: str = Field(pattern=r"^rcp_qwen_manifest:[0-9a-f]{64}$")
    operational_validation_id: str = Field(
        pattern=r"^rcp_qwen_operational_validation:[0-9a-f]{64}$"
    )
    artifact_inventory: dict[str, str]
    artifact_inventory_sha256: str = Field(pattern=_HEX64)
    secret_files_scanned: int = Field(ge=1)
    exact_credential_occurrences: Literal[0] = 0
    bearer_header_occurrences: Literal[0] = 0
    authorization_field_occurrences: Literal[0] = 0
    provider_calls_performed: Literal[0] = 0
    network_requests_performed: Literal[0] = 0
    immutable_replay_passed: Literal[True] = True
    capability_claim: Literal["route_accepted_complete_payload_application_unproven"]
    reference_transmission_performed: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    semantic_faithfulness_assessed: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "verification_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.artifact_inventory_sha256 != hash_canonical(self.artifact_inventory):
            raise ValueError("Qwen verification inventory hash differs")
        expected = "rcp_qwen_verification:" + hash_canonical(
            {"schema": "lf021_rcp_qwen_verification_v1", **self.id_payload()}
        )
        if self.verification_id != expected:
            raise ValueError("Qwen verification ID differs")
        return self


@dataclass(frozen=True, slots=True)
class LoadedQwenQualification:
    loaded_config: LoadedConfig[QwenQualificationConfig]
    config_file_sha256: str
    bound_artifact_hashes: dict[str, str]
    problem: ProblemPoolRecord
    references: tuple[TheoremRecord, ...]
    prompt_template_sha256: str
    rendered_prompt: str
    rendered_prompt_sha256: str
    reference_blind_audit: QwenReferenceBlindAudit
    run_key: str
    output_directory: Path


@dataclass(frozen=True, slots=True)
class QwenExecutionRun:
    catalog: QwenCatalogObservation
    invocation: QwenInvocation
    attempt: QwenAttemptRecord
    terminal: QwenTerminal
    manifest: QwenManifest
    output_directory: Path
    resumed: bool


@dataclass(frozen=True, slots=True)
class QwenVerificationRun:
    operational_validation: QwenOperationalValidation
    report: QwenVerificationReport
    report_path: Path
    report_sha256: str


def _record_bytes(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _persist_immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise RCPQwenArtifactConflict(f"immutable artifact conflict: {path}")
        return hash_file(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise RCPQwenArtifactConflict(f"concurrent artifact conflict: {path}") from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _repo_relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise RCPQwenQualificationError(f"artifact escapes repository: {path}") from exc


def _safe_repo_file(root: Path, artifact: str, expected_hash: str) -> Path:
    path = root / artifact
    if path.is_symlink() or not path.is_file():
        raise RCPQwenQualificationError(f"bound artifact is missing or unsafe: {artifact}")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RCPQwenQualificationError(f"bound artifact escapes repository: {artifact}") from exc
    observed = hash_file(path)
    if observed != expected_hash:
        raise RCPQwenQualificationError(
            f"bound artifact hash drift for {artifact}: {observed} != {expected_hash}"
        )
    return path


def _single_jsonl(path: Path, model: type[StrictModel]) -> StrictModel:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise RCPQwenQualificationError(f"expected exactly one JSONL record: {path}")
    try:
        return model.model_validate_json(lines[0])
    except ValueError as exc:
        raise RCPQwenQualificationError(f"invalid bound record {path}: {exc}") from exc


def _render_prompt(
    template_path: Path,
    *,
    nl_statement: str,
    declaration_name: str,
) -> tuple[str, str, str]:
    raw = template_path.read_bytes()
    template = raw.decode("utf-8")
    expected = {_PROMPT_HASH_TOKEN, _PROBLEM_JSON_TOKEN, _DECLARATION_TOKEN}
    if set(_TEMPLATE_TOKEN.findall(template)) != expected or any(
        template.count(token) != 1 for token in expected
    ):
        raise RCPQwenQualificationError("prompt template placeholders differ")
    template_hash = sha256_hex(raw)
    problem_json = canonical_json_bytes(
        {
            "schema": "reference_blind_public_problem_v1",
            "nl_statement": nl_statement,
        }
    ).decode("utf-8")
    rendered = (
        template.replace(_PROMPT_HASH_TOKEN, template_hash)
        .replace(_PROBLEM_JSON_TOKEN, problem_json)
        .replace(_DECLARATION_TOKEN, declaration_name)
    )
    if _TEMPLATE_TOKEN.search(rendered):
        raise RCPQwenQualificationError("prompt contains unresolved placeholders")
    return template_hash, rendered, sha256_hex(rendered.encode("utf-8"))


def _audit_reference_blind(
    *,
    problem: ProblemPoolRecord,
    references: tuple[TheoremRecord, ...],
    rendered_prompt: str,
    rendered_prompt_sha256: str,
    wire_fields: tuple[str, ...],
) -> QwenReferenceBlindAudit:
    if problem.private_source_content or not problem.external_provider_eligible:
        raise RCPQwenQualificationError("qualification input is not external-provider eligible")
    lowered = rendered_prompt.casefold()
    if any(item in rendered_prompt for item in problem.reference_theorem_ids):
        raise RCPQwenQualificationError("prompt leaks reference theorem ID")
    names = tuple(
        name.casefold()
        for theorem in references
        for name in (theorem.declaration_full_name, theorem.declaration_name)
        if name
    )
    if any(name in lowered for name in names):
        raise RCPQwenQualificationError("prompt leaks reference declaration name")
    signatures = tuple(
        signature
        for theorem in references
        for signature in (
            theorem.proof_stripped_declaration,
            theorem.inline_elaboration_source,
        )
        if signature and len(signature.strip()) >= 16
    )
    if any(signature in rendered_prompt for signature in signatures):
        raise RCPQwenQualificationError("prompt leaks trusted Lean signature")
    if problem.nl_source_link in rendered_prompt:
        raise RCPQwenQualificationError("prompt leaks source link")
    return QwenReferenceBlindAudit(
        problem_record_id=problem.problem_record_id,
        rendered_prompt_sha256=rendered_prompt_sha256,
        provider_payload_keys=("model", "messages", *wire_fields),
    )


def load_qwen_qualification(
    config_path: Path,
    *,
    repo_root: Path,
) -> LoadedQwenQualification:
    root = repo_root.resolve()
    loaded = load_config(config_path, QwenQualificationConfig)
    config = loaded.config
    bound_hashes: dict[str, str] = {}
    for label, binding in config.bound_artifacts.items():
        _safe_repo_file(root, binding.artifact, binding.sha256)
        bound_hashes[label] = binding.sha256
    records_path = _safe_repo_file(
        root,
        config.problem.records_artifact,
        config.problem.records_sha256,
    )
    references_path = _safe_repo_file(
        root,
        config.problem.reference_theorems_artifact,
        config.problem.reference_theorems_sha256,
    )
    prompt_path = _safe_repo_file(root, config.prompt.artifact, config.prompt.sha256)
    problem = _single_jsonl(records_path, ProblemPoolRecord)
    reference = _single_jsonl(references_path, TheoremRecord)
    assert isinstance(problem, ProblemPoolRecord)
    assert isinstance(reference, TheoremRecord)
    references = (reference,)
    if problem.problem_record_id != config.problem.expected_problem_record_id:
        raise RCPQwenQualificationError("problem ID differs from frozen binding")
    if (
        problem.private_source_content
        or not problem.external_provider_eligible
        or problem.eligibility != "eligible"
        or not problem.denylist_checked
        or problem.denylist_hits
        or tuple(problem.reference_theorem_ids)
        != tuple(theorem.theorem_id for theorem in references)
    ):
        raise RCPQwenQualificationError("problem eligibility/reference binding differs")
    template_hash, rendered, rendered_hash = _render_prompt(
        prompt_path,
        nl_statement=problem.nl_statement,
        declaration_name=config.prompt.declaration_name,
    )
    wire_fields = tuple(config.decoding.wire_fields())
    if wire_fields != config.capability_policy.required_wire_fields:
        raise RCPQwenQualificationError("wire fields differ from capability policy")
    audit = _audit_reference_blind(
        problem=problem,
        references=references,
        rendered_prompt=rendered,
        rendered_prompt_sha256=rendered_hash,
        wire_fields=wire_fields,
    )
    config_file_sha256 = hash_file(config_path)
    run_key = hash_canonical(
        {
            "schema": "lf021_rcp_qwen_run_key_v1",
            "config_file_sha256": config_file_sha256,
            "config_hash": loaded.config_hash,
            "bound_artifact_hashes": bound_hashes,
            "problem_record_id": problem.problem_record_id,
            "model_id": config.primary_model_id,
            "rendered_prompt_sha256": rendered_hash,
            "decoding": config.decoding.provider_decoding(),
        }
    )
    return LoadedQwenQualification(
        loaded_config=loaded,
        config_file_sha256=config_file_sha256,
        bound_artifact_hashes=bound_hashes,
        problem=problem,
        references=references,
        prompt_template_sha256=template_hash,
        rendered_prompt=rendered,
        rendered_prompt_sha256=rendered_hash,
        reference_blind_audit=audit,
        run_key=run_key,
        output_directory=root / config.outputs.root / run_key,
    )


def resolve_credentials(config: QwenQualificationConfig) -> shared.RCPCredentials:
    base_url = os.environ.get(config.transport.base_url_env, "").strip().rstrip("/")
    api_key = os.environ.get(config.transport.api_key_env, "").strip()
    if base_url != config.transport.expected_base_url or not base_url.startswith("https://"):
        raise RCPQwenQualificationError("RCP_BASE_URL differs from frozen HTTPS endpoint")
    if not api_key:
        raise RCPQwenQualificationError("RCP_API_KEY is unset or empty")
    return shared.RCPCredentials(base_url=base_url, api_key=api_key)


def _assert_frozen(loaded: LoadedQwenQualification, *, repo_root: Path) -> None:
    """Recheck every bound byte immediately at each remote boundary."""

    config = loaded.loaded_config.config
    checks = {
        str(loaded.loaded_config.path): loaded.config_file_sha256,
        config.problem.records_artifact: config.problem.records_sha256,
        config.problem.reference_theorems_artifact: (config.problem.reference_theorems_sha256),
        config.prompt.artifact: config.prompt.sha256,
        **{binding.artifact: binding.sha256 for binding in config.bound_artifacts.values()},
    }
    for artifact, expected in checks.items():
        path = Path(artifact)
        if not path.is_absolute():
            path = repo_root / path
        if path.is_symlink() or not path.is_file() or hash_file(path) != expected:
            raise RCPQwenQualificationError(
                f"frozen artifact drift at provider boundary: {artifact}"
            )


def _redact(text: str, *, api_key: str) -> str:
    value = text.replace(api_key, "<redacted>") if api_key else text
    value = _BEARER_PATTERN.sub("Bearer <redacted>", value)
    return _AUTHORIZATION_PATTERN.sub('"redacted_authorization":', value)


def _claim_execution(loaded: LoadedQwenQualification) -> None:
    root = loaded.output_directory
    root.mkdir(parents=True, exist_ok=True)
    terminal = root / "terminal.json"
    if terminal.exists():
        raise RCPQwenQualificationError(
            "completed run exists; use verify-only (no provider replay is allowed)"
        )
    claim_path = root / "execution_claim.json"
    payload = (
        canonical_json_bytes(
            {
                "schema_version": 1,
                "artifact_kind": "lf021_rcp_qwen_execution_claim_v1",
                "run_key": loaded.run_key,
                "config_file_sha256": loaded.config_file_sha256,
                "config_hash": loaded.loaded_config.config_hash,
                "maximum_catalog_requests": 1,
                "maximum_chat_completion_requests": 1,
                "field_removal_retry_forbidden": True,
            }
        )
        + b"\n"
    )
    try:
        descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RCPQwenQualificationError(
            "partial or concurrent run exists; refusing another provider request"
        ) from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _catalog(
    loaded: LoadedQwenQualification,
    *,
    credentials: shared.RCPCredentials,
    transport: shared.RCPHTTPTransport,
    clock: Callable[[], datetime.datetime],
    repo_root: Path,
) -> QwenCatalogObservation:
    config = loaded.loaded_config.config
    response = transport.get(
        url=credentials.base_url + config.transport.catalog_path,
        api_key=credentials.api_key,
        timeout_seconds=config.request_budget.catalog_timeout_seconds,
    )
    if response.status_code != 200:
        raise RCPQwenQualificationError(f"RCP catalog returned HTTP {response.status_code}")
    try:
        document = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RCPQwenQualificationError("RCP catalog is not valid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise RCPQwenQualificationError("RCP catalog shape differs")
    model_ids = tuple(
        sorted(
            {
                item["id"]
                for item in document["data"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )
    )
    if config.primary_model_id not in model_ids:
        raise RCPQwenQualificationError("primary Qwen model is absent from live catalog")
    raw_path = loaded.output_directory / "catalog_wire_response.json"
    _persist_immutable(raw_path, response.body)
    payload = {
        "schema_version": 1,
        "transport": "remote_on_prem_epfl_rcp",
        "endpoint_sha256": sha256_hex(
            (credentials.base_url + config.transport.catalog_path).encode("utf-8")
        ),
        "observed_at": clock().isoformat().replace("+00:00", "Z"),
        "http_status": 200,
        "raw_response_artifact": _repo_relative(raw_path, repo_root),
        "raw_response_sha256": sha256_hex(response.body),
        "canonical_model_ids_sha256": hash_canonical(model_ids),
        "model_count": len(model_ids),
        "primary_model_id": config.primary_model_id,
        "primary_present": True,
        "no_call_ablation_model_id": config.no_call_ablation_model_id,
        "no_call_ablation_present": config.no_call_ablation_model_id in model_ids,
        "catalog_requests_performed": 1,
        "credential_serialized": False,
    }
    observation_id = "rcp_qwen_catalog_observation:" + hash_canonical(
        {"schema": "lf021_rcp_qwen_catalog_observation_v1", **payload}
    )
    observation = QwenCatalogObservation.model_validate(
        {"observation_id": observation_id, **payload}
    )
    _persist_immutable(
        loaded.output_directory / "catalog_observation.json",
        _record_bytes(observation),
    )
    return observation


def _wire_payload(loaded: LoadedQwenQualification) -> dict[str, object]:
    config = loaded.loaded_config.config
    return {
        "model": config.primary_model_id,
        "messages": [
            {"role": "system", "content": config.prompt.system_message},
            {"role": "user", "content": loaded.rendered_prompt},
        ],
        **config.decoding.wire_fields(),
    }


def _invocation(
    loaded: LoadedQwenQualification,
    catalog: QwenCatalogObservation,
) -> QwenInvocation:
    config = loaded.loaded_config.config
    decoding = config.decoding.provider_decoding()
    payload = {
        "schema_version": 1,
        "config_file_sha256": loaded.config_file_sha256,
        "config_hash": loaded.loaded_config.config_hash,
        "bound_artifact_hashes": loaded.bound_artifact_hashes,
        "catalog_observation_id": catalog.observation_id,
        "model_id": config.primary_model_id,
        "no_call_ablation_model_id": config.no_call_ablation_model_id,
        "no_call_ablation_requests_performed": 0,
        "provider_family": _FAMILY,
        "diversity_group": _FAMILY,
        "all_qwen_checkpoints_one_family": True,
        "problem_record_id": loaded.problem.problem_record_id,
        "prompt_template_sha256": loaded.prompt_template_sha256,
        "rendered_prompt_sha256": loaded.rendered_prompt_sha256,
        "decoding": decoding,
        "decoding_sha256": hash_canonical(decoding),
        "wire_field_names": tuple(config.decoding.wire_fields()),
        "capability_claim": "combined_request_acceptance_only_application_unproven",
        "reference_hidden": True,
        "private_source_content": False,
        "semantic_labels_created": False,
        "semantic_faithfulness_assessed": False,
        "supervision_eligible": False,
        "gate_credit_claimed": False,
    }
    invocation_id = "rcp_qwen_invocation:" + hash_canonical(
        {"schema": "lf021_rcp_qwen_invocation_v1", **payload}
    )
    return QwenInvocation.model_validate({"invocation_id": invocation_id, **payload})


def _capability_evidence(
    invocation: QwenInvocation,
    *,
    http_status: int | None,
    reasoning_content: str,
) -> QwenCapabilityEvidence:
    accepted = http_status == 200
    value: Literal[
        "accepted_in_combined_request_application_unproven",
        "request_failed_application_unproven",
    ] = (
        "accepted_in_combined_request_application_unproven"
        if accepted
        else "request_failed_application_unproven"
    )
    return QwenCapabilityEvidence(
        invocation_id=invocation.invocation_id,
        observation_method="single_combined_theorem_generation_request",
        complete_frozen_field_set_sent=True,
        fields_removed_or_retried=False,
        http_status=http_status,
        combined_request_accepted=accepted,
        per_field_status=dict.fromkeys(invocation.wire_field_names, value),
        reasoning_content_observed=bool(reasoning_content),
        reasoning_content_characters=len(reasoning_content),
        claim=(
            "route_accepted_complete_payload_application_unproven"
            if accepted
            else "request_failed_no_field_support_claim"
        ),
    )


def _attempt(
    *,
    invocation: QwenInvocation,
    request: ProviderRequest,
    request_path: Path,
    wire_request_path: Path,
    wire_response_path: Path,
    provider_response_path: Path,
    capability_path: Path,
    http_status: int | None,
    provider_request_id: str | None,
    returned_model: str | None,
    status: Literal["response_received", "request_failed"],
    error_code: str | None,
    error_detail: str | None,
    tokens: dict[str, int],
    started_at: datetime.datetime,
    completed_at: datetime.datetime,
    repo_root: Path,
) -> QwenAttemptRecord:
    payload = {
        "schema_version": 1,
        "invocation_id": invocation.invocation_id,
        "provider_request_hash": request.request_hash,
        "provider_attempt_id": request.attempt_id,
        "request_artifact": _repo_relative(request_path, repo_root),
        "request_sha256": hash_file(request_path),
        "wire_request_artifact": _repo_relative(wire_request_path, repo_root),
        "wire_request_sha256": hash_file(wire_request_path),
        "wire_response_artifact": _repo_relative(wire_response_path, repo_root),
        "wire_response_sha256": hash_file(wire_response_path),
        "provider_response_artifact": _repo_relative(provider_response_path, repo_root),
        "provider_response_sha256": hash_file(provider_response_path),
        "capability_evidence_artifact": _repo_relative(capability_path, repo_root),
        "capability_evidence_sha256": hash_file(capability_path),
        "http_status": http_status,
        "provider_request_id": provider_request_id,
        "returned_model": returned_model,
        "status": status,
        "error_code": error_code,
        "error_detail": error_detail,
        "tokens": tokens,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "latency_ms": max(0, int((completed_at - started_at).total_seconds() * 1000)),
        "chat_completion_requests_performed": 1,
        "retries_performed": 0,
        "api_key_serialized": False,
    }
    stable = {
        "invocation_id": payload["invocation_id"],
        "provider_request_hash": payload["provider_request_hash"],
        "provider_attempt_id": payload["provider_attempt_id"],
        "request_sha256": payload["request_sha256"],
        "wire_request_sha256": payload["wire_request_sha256"],
        "wire_response_sha256": payload["wire_response_sha256"],
        "provider_response_sha256": payload["provider_response_sha256"],
        "capability_evidence_sha256": payload["capability_evidence_sha256"],
        "http_status": payload["http_status"],
        "provider_request_id": payload["provider_request_id"],
        "returned_model": payload["returned_model"],
        "status": payload["status"],
        "error_code": payload["error_code"],
        "tokens": payload["tokens"],
    }
    attempt_id = "rcp_qwen_attempt:" + hash_canonical(
        {"schema": "lf021_rcp_qwen_attempt_v1", **stable}
    )
    return QwenAttemptRecord.model_validate({"attempt_id": attempt_id, **payload})


def _terminal(
    invocation: QwenInvocation,
    attempt: QwenAttemptRecord,
    *,
    status: RCPQwenTerminalStatus,
    output_sha256: str | None,
    parsed_statement_sha256: str | None,
    parse_error_code: str | None,
) -> QwenTerminal:
    payload = {
        "schema_version": 1,
        "invocation_id": invocation.invocation_id,
        "attempt_id": attempt.attempt_id,
        "model_id": invocation.model_id,
        "status": status.value,
        "output_sha256": output_sha256,
        "parsed_statement_sha256": parsed_statement_sha256,
        "parse_error_code": parse_error_code,
        "chat_completion_requests_performed": 1,
        "retries_performed": 0,
        "no_call_ablation_requests_performed": 0,
        "reference_transmission_performed": False,
        "private_source_transmission_performed": False,
        "semantic_labels_created": False,
        "semantic_faithfulness_assessed": False,
        "supervision_eligible": False,
        "gate_credit_claimed": False,
        "gate_closed": False,
    }
    terminal_id = "rcp_qwen_terminal:" + hash_canonical(
        {"schema": "lf021_rcp_qwen_terminal_v1", **payload}
    )
    return QwenTerminal.model_validate({"terminal_id": terminal_id, **payload})


def _manifest(
    loaded: LoadedQwenQualification,
    *,
    catalog: QwenCatalogObservation,
    invocation: QwenInvocation,
    attempt: QwenAttemptRecord,
    terminal: QwenTerminal,
    repo_root: Path,
) -> QwenManifest:
    policy = loaded.loaded_config.config.policy
    payload = {
        "schema_version": 1,
        "config_file_sha256": loaded.config_file_sha256,
        "config_hash": loaded.loaded_config.config_hash,
        "bound_artifact_hashes": loaded.bound_artifact_hashes,
        "run_key": loaded.run_key,
        "output_directory": _repo_relative(loaded.output_directory, repo_root),
        "catalog_observation_id": catalog.observation_id,
        "invocation_id": invocation.invocation_id,
        "attempt_id": attempt.attempt_id,
        "terminal_id": terminal.terminal_id,
        "model_id": invocation.model_id,
        "terminal_status": terminal.status.value,
        "catalog_requests_performed": 1,
        "chat_completion_requests_performed": 1,
        "dedicated_capability_requests_performed": 0,
        "no_call_ablation_requests_performed": 0,
        "reference_transmission_performed": False,
        "private_source_transmission_performed": False,
        "semantic_labels_created": False,
        "semantic_faithfulness_assessed": False,
        "supervision_eligible": False,
        "gate_credit_claimed": False,
        "gate_closed": False,
        "checkpoint_revision_status": policy.checkpoint_revision_status,
        "training_cutoff_status": policy.training_cutoff_status,
        "contamination_status": policy.contamination_status,
        "unseen_claim_eligible": False,
        "heldout_claim_eligible": False,
        "evaluation_claim_eligible": False,
    }
    manifest_id = "rcp_qwen_manifest:" + hash_canonical(
        {"schema": "lf021_rcp_qwen_manifest_v1", **payload}
    )
    return QwenManifest.model_validate({"manifest_id": manifest_id, **payload})


def execute_one_qwen_qualification(
    loaded: LoadedQwenQualification,
    *,
    credentials: shared.RCPCredentials,
    repo_root: Path,
    transport: shared.RCPHTTPTransport | None = None,
    clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC),
) -> QwenExecutionRun:
    """Execute the only authorized Qwen call without retries or field removal."""

    _assert_frozen(loaded, repo_root=repo_root)
    _claim_execution(loaded)
    config = loaded.loaded_config.config
    transport = transport or shared.UrllibRCPTransport()
    root = loaded.output_directory
    catalog = _catalog(
        loaded,
        credentials=credentials,
        transport=transport,
        clock=clock,
        repo_root=repo_root,
    )
    invocation = _invocation(loaded, catalog)
    _persist_immutable(
        root / "reference_blind_audit.json",
        _record_bytes(loaded.reference_blind_audit),
    )
    _persist_immutable(root / "invocation.json", _record_bytes(invocation))

    request = create_provider_request_for_problem(
        identity=ProviderIdentity(
            provider="epfl_rcp",
            model=config.primary_model_id,
            revision=f"catalog-sha256:{catalog.raw_response_sha256}",
            transport="external_disabled",
        ),
        problem=loaded.problem,
        prompt_template_hash=loaded.prompt_template_sha256,
        rendered_prompt=loaded.rendered_prompt,
        decoding=config.decoding.provider_decoding(),
        attempt_index=0,
    )
    attempt_dir = root / "attempts/0000"
    request_path = attempt_dir / "provider_request.json"
    persist_provider_request(request, request_path)
    wire_request = _wire_payload(loaded)
    if tuple(key for key in wire_request if key not in {"model", "messages"}) != (
        config.capability_policy.required_wire_fields
    ):
        raise RCPQwenQualificationError("wire request field set/order differs before provider call")
    wire_request_path = attempt_dir / "wire_request.json"
    _persist_immutable(wire_request_path, canonical_json_bytes(wire_request) + b"\n")

    _assert_frozen(loaded, repo_root=repo_root)
    started_at = clock()
    response: shared.RCPHTTPResponse | None = None
    provider_request_id: str | None = None
    returned_model: str | None = None
    tokens: dict[str, int] = {}
    content: str | None = None
    reasoning_content = ""
    error_code: str | None = None
    error_detail: str | None = None
    try:
        response = transport.post_json(
            url=credentials.base_url + config.transport.chat_completions_path,
            api_key=credentials.api_key,
            payload=wire_request,
            timeout_seconds=config.request_budget.request_timeout_seconds,
        )
        if response.status_code != 200:
            error_code = f"http_{response.status_code}"
            error_detail = _redact(
                response.body.decode("utf-8", errors="replace")[:1000],
                api_key=credentials.api_key,
            )
        else:
            content, provider_request_id, returned_model, tokens = shared._extract_completion(
                response.body
            )
            document = json.loads(response.body)
            message = document["choices"][0]["message"]
            observed = message.get("reasoning_content", "")
            reasoning_content = observed if isinstance(observed, str) else ""
            if returned_model not in {None, config.primary_model_id}:
                error_code = "returned_model_mismatch"
                error_detail = f"returned model {returned_model!r} differs"
    except shared.RCPTransportError as exc:
        error_code = exc.code
        error_detail = _redact(str(exc), api_key=credentials.api_key)
    except (ValueError, KeyError, TypeError) as exc:
        error_code = "invalid_response"
        error_detail = _redact(str(exc), api_key=credentials.api_key)
    completed_at = clock()

    http_status = response.status_code if response is not None else None
    if response is not None:
        wire_response_path = attempt_dir / "wire_response.json"
        _persist_immutable(wire_response_path, response.body)
    else:
        wire_response_path = attempt_dir / "wire_response_error.json"
        _persist_immutable(
            wire_response_path,
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "http_status": None,
                    "error_code": error_code,
                    "error_detail": error_detail,
                    "credential_serialized": False,
                }
            )
            + b"\n",
        )
    route_accepted = http_status == 200
    accepted = route_accepted and error_code is None and content is not None
    capability = _capability_evidence(
        invocation,
        http_status=http_status,
        reasoning_content=reasoning_content,
    )
    capability_path = attempt_dir / "capability_evidence.json"
    _persist_immutable(capability_path, _record_bytes(capability))

    if accepted:
        assert content is not None
        provider_raw = ProviderRawResponse.success(request, content)
    else:
        provider_raw = ProviderRawResponse.error(
            request,
            error_type=error_code or "request_failed",
            error_detail=error_detail,
        )
    provider_result = persist_provider_raw_response(attempt_dir / "provider_raw", provider_raw)
    attempt = _attempt(
        invocation=invocation,
        request=request,
        request_path=request_path,
        wire_request_path=wire_request_path,
        wire_response_path=wire_response_path,
        provider_response_path=provider_result.raw_response_path,
        capability_path=capability_path,
        http_status=200 if accepted else http_status,
        provider_request_id=provider_request_id,
        returned_model=returned_model,
        status="response_received" if accepted else "request_failed",
        error_code=None if accepted else error_code or "request_failed",
        error_detail=None if accepted else error_detail,
        tokens=tokens,
        started_at=started_at,
        completed_at=completed_at,
        repo_root=repo_root,
    )
    _persist_immutable(attempt_dir / "attempt_record.json", _record_bytes(attempt))

    parsed: ParsedLeanDeclaration | None = None
    parse_error_code: str | None = None
    if accepted and content is not None:
        try:
            parsed = parse_direct_autoformalization_output(content)
            if parsed.declaration_name != config.prompt.declaration_name:
                parse_error_code = "wrong_declaration_name"
                parsed = None
        except DirectOutputParseError as exc:
            parse_error_code = exc.code.value
    if not accepted:
        terminal_status = RCPQwenTerminalStatus.REQUEST_FAILED
    elif parsed is None:
        terminal_status = RCPQwenTerminalStatus.PARSE_FAILED
    else:
        terminal_status = RCPQwenTerminalStatus.RAW_COLLECTED
    terminal = _terminal(
        invocation,
        attempt,
        status=terminal_status,
        output_sha256=provider_raw.output_hash,
        parsed_statement_sha256=(parsed.statement_sha256 if parsed is not None else None),
        parse_error_code=parse_error_code,
    )
    _persist_immutable(root / "terminal.json", _record_bytes(terminal))
    manifest = _manifest(
        loaded,
        catalog=catalog,
        invocation=invocation,
        attempt=attempt,
        terminal=terminal,
        repo_root=repo_root,
    )
    _persist_immutable(root / "qualification_manifest.json", _record_bytes(manifest))
    _scan_secret_material(root, credentials.api_key)
    return QwenExecutionRun(
        catalog=catalog,
        invocation=invocation,
        attempt=attempt,
        terminal=terminal,
        manifest=manifest,
        output_directory=root,
        resumed=False,
    )


def _load_model(path: Path, model: type[StrictModel]) -> StrictModel:
    try:
        return model.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise RCPQwenQualificationError(f"{path} fails {model.__name__}: {exc}") from exc


def load_completed_qwen_run(
    loaded: LoadedQwenQualification,
    *,
    repo_root: Path | None = None,
) -> QwenExecutionRun:
    """Load an existing terminal bundle without network access."""

    root_repo = repo_root.resolve() if repo_root is not None else loaded.output_directory.parents[5]
    root = loaded.output_directory
    catalog = _load_model(root / "catalog_observation.json", QwenCatalogObservation)
    invocation = _load_model(root / "invocation.json", QwenInvocation)
    attempt = _load_model(root / "attempts/0000/attempt_record.json", QwenAttemptRecord)
    terminal = _load_model(root / "terminal.json", QwenTerminal)
    manifest = _load_model(root / "qualification_manifest.json", QwenManifest)
    assert isinstance(catalog, QwenCatalogObservation)
    assert isinstance(invocation, QwenInvocation)
    assert isinstance(attempt, QwenAttemptRecord)
    assert isinstance(terminal, QwenTerminal)
    assert isinstance(manifest, QwenManifest)
    reference_audit = _load_model(
        root / "reference_blind_audit.json",
        QwenReferenceBlindAudit,
    )
    capability = _load_model(
        root / "attempts/0000/capability_evidence.json",
        QwenCapabilityEvidence,
    )
    request = _load_model(
        root / "attempts/0000/provider_request.json",
        ProviderRequest,
    )
    provider_raw = _provider_raw_from_attempt(attempt, root_repo)
    assert isinstance(reference_audit, QwenReferenceBlindAudit)
    assert isinstance(capability, QwenCapabilityEvidence)
    assert isinstance(request, ProviderRequest)
    if (
        manifest.config_file_sha256 != loaded.config_file_sha256
        or manifest.config_hash != loaded.loaded_config.config_hash
        or manifest.bound_artifact_hashes != loaded.bound_artifact_hashes
        or manifest.run_key != loaded.run_key
        or invocation.catalog_observation_id != catalog.observation_id
        or attempt.invocation_id != invocation.invocation_id
        or terminal.invocation_id != invocation.invocation_id
        or manifest.terminal_id != terminal.terminal_id
        or reference_audit.model_dump(mode="json")
        != loaded.reference_blind_audit.model_dump(mode="json")
        or capability.invocation_id != invocation.invocation_id
        or request.request_hash != attempt.provider_request_hash
        or provider_raw.request_hash != request.request_hash
        or provider_raw.attempt_id != request.attempt_id
    ):
        raise RCPQwenQualificationError("completed Qwen lineage does not replay")
    artifact_hashes = {
        attempt.request_artifact: attempt.request_sha256,
        attempt.wire_request_artifact: attempt.wire_request_sha256,
        attempt.wire_response_artifact: attempt.wire_response_sha256,
        attempt.provider_response_artifact: attempt.provider_response_sha256,
        attempt.capability_evidence_artifact: attempt.capability_evidence_sha256,
        catalog.raw_response_artifact: catalog.raw_response_sha256,
    }
    for artifact, expected_hash in artifact_hashes.items():
        path = root_repo / artifact
        if not path.is_file() or path.is_symlink() or hash_file(path) != expected_hash:
            raise RCPQwenQualificationError(f"completed artifact hash differs: {artifact}")
    replay_invocation = _invocation(loaded, catalog)
    if replay_invocation.model_dump(mode="json") != invocation.model_dump(mode="json"):
        raise RCPQwenQualificationError("Qwen invocation does not replay")
    replay_request = ProviderRequest.create(
        identity=ProviderIdentity(
            provider="epfl_rcp",
            model=loaded.loaded_config.config.primary_model_id,
            revision=f"catalog-sha256:{catalog.raw_response_sha256}",
            transport="fixture",
        ),
        prompt_template_hash=loaded.prompt_template_sha256,
        rendered_prompt=loaded.rendered_prompt,
        decoding=loaded.loaded_config.config.decoding.provider_decoding(),
        input_ids=(loaded.problem.problem_record_id,),
        private_source_content=False,
        attempt_index=0,
    )
    if replay_request.model_dump(mode="json") != request.model_dump(mode="json"):
        raise RCPQwenQualificationError("provider request does not replay")
    wire_request_path = root_repo / attempt.wire_request_artifact
    if json.loads(wire_request_path.read_bytes()) != _wire_payload(loaded):
        raise RCPQwenQualificationError("wire request does not replay")
    if terminal.status is RCPQwenTerminalStatus.RAW_COLLECTED:
        if provider_raw.status != "success" or provider_raw.output_text is None:
            raise RCPQwenQualificationError("raw-collected provider response differs")
        parsed = parse_direct_autoformalization_output(provider_raw.output_text)
        replay_terminal = _terminal(
            invocation,
            attempt,
            status=terminal.status,
            output_sha256=provider_raw.output_hash,
            parsed_statement_sha256=parsed.statement_sha256,
            parse_error_code=None,
        )
        if replay_terminal.model_dump(mode="json") != terminal.model_dump(mode="json"):
            raise RCPQwenQualificationError("Qwen terminal does not replay")
    replay_manifest = _manifest(
        loaded,
        catalog=catalog,
        invocation=invocation,
        attempt=attempt,
        terminal=terminal,
        repo_root=root_repo,
    )
    if replay_manifest.model_dump(mode="json") != manifest.model_dump(mode="json"):
        raise RCPQwenQualificationError("Qwen manifest does not replay")
    return QwenExecutionRun(
        catalog=catalog,
        invocation=invocation,
        attempt=attempt,
        terminal=terminal,
        manifest=manifest,
        output_directory=root,
        resumed=True,
    )


def _scan_secret_material(
    root: Path,
    credential: str,
    *,
    extra_paths: tuple[Path, ...] = (),
) -> int:
    if not credential:
        raise RCPQwenQualificationError("exact credential is required for secret scan")
    exact = credential.encode("utf-8")
    paths = tuple(
        sorted(
            {
                *(path for path in root.rglob("*") if path.is_file() and not path.is_symlink()),
                *(path for path in extra_paths if path.is_file() and not path.is_symlink()),
            },
            key=str,
        )
    )
    for path in paths:
        data = path.read_bytes()
        lowered = data.lower()
        if (
            exact in data
            or b"bearer " in lowered
            or b'"authorization"' in lowered
            or _ENV_NAME.encode("utf-8") in data
        ):
            raise RCPQwenQualificationError(f"secret-like material persisted in {path}")
    return len(paths)


def _provider_raw_from_attempt(attempt: QwenAttemptRecord, root: Path) -> ProviderRawResponse:
    record = _load_model(root / attempt.provider_response_artifact, ProviderRawResponse)
    assert isinstance(record, ProviderRawResponse)
    return record


def _validate_lean(
    loaded: LoadedQwenQualification,
    run: QwenExecutionRun,
    *,
    repo_root: Path,
    mathlib_project_dir: Path,
) -> QwenOperationalValidation:
    if run.terminal.status is not RCPQwenTerminalStatus.RAW_COLLECTED:
        raise RCPQwenQualificationError("Lean validation requires raw_collected terminal")
    provider_raw = _provider_raw_from_attempt(run.attempt, repo_root)
    if provider_raw.output_text is None:
        raise RCPQwenQualificationError("provider output text is missing")
    parsed = parse_direct_autoformalization_output(provider_raw.output_text)
    if parsed.statement_sha256 != run.terminal.parsed_statement_sha256:
        raise RCPQwenQualificationError("parsed statement hash differs from terminal")
    context_fingerprint = loaded.problem.context_id.removeprefix("ctx:")
    raw_dir = repo_root / loaded.loaded_config.config.outputs.lean_raw_root / loaded.run_key
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=mathlib_project_dir,
            context_fingerprint=context_fingerprint,
            environment_schema_version=1,
            raw_response_dir=raw_dir,
        )
    )
    try:
        result = backend.run(
            LeanRequest(
                request_id=f"rcp-qwen-{run.invocation.invocation_id}",
                context_id=loaded.problem.context_id,
                code=f"import Mathlib\n{parsed.statement} := by sorry",
                declarations=True,
                allow_sorry=True,
                timeout_seconds=120,
            )
        )
    finally:
        backend.close()
    if result.status is not LeanStatus.VALID_WITH_SORRY:
        raise RCPQwenQualificationError(
            f"Qwen output is not operationally valid with sorry: {result.status.value}"
        )
    if len(result.declarations) != 1:
        raise RCPQwenQualificationError("Qwen Lean validation declaration count differs")
    if (
        result.declarations[0].get("full_name")
        != loaded.loaded_config.config.prompt.declaration_name
    ):
        raise RCPQwenQualificationError("Qwen Lean validation declaration name differs")
    if result.raw_response_path is None:
        raise RCPQwenQualificationError("LeanInteract raw response path is absent")
    raw_path = Path(result.raw_response_path)
    payload = {
        "schema_version": 1,
        "invocation_id": run.invocation.invocation_id,
        "parsed_statement_sha256": parsed.statement_sha256,
        "lean_request_hash": result.request_hash,
        "lean_raw_artifact": _repo_relative(raw_path, repo_root),
        "lean_raw_sha256": hash_file(raw_path),
        "status": "valid_with_sorry",
        "declaration_name": loaded.loaded_config.config.prompt.declaration_name,
        "declaration_count": 1,
        "sorry_count": len(result.sorries),
        "semantic_faithfulness_assessed": False,
        "semantic_labels_created": False,
        "gate_credit_claimed": False,
    }
    validation_id = "rcp_qwen_operational_validation:" + hash_canonical(
        {"schema": "lf021_rcp_qwen_operational_validation_v1", **payload}
    )
    validation = QwenOperationalValidation.model_validate(
        {"validation_id": validation_id, **payload}
    )
    _persist_immutable(
        run.output_directory / "operational_validation.json",
        _record_bytes(validation),
    )
    return validation


def _load_operational_validation(
    loaded: LoadedQwenQualification,
    run: QwenExecutionRun,
    *,
    repo_root: Path,
) -> QwenOperationalValidation:
    record = _load_model(
        run.output_directory / "operational_validation.json",
        QwenOperationalValidation,
    )
    assert isinstance(record, QwenOperationalValidation)
    raw_path = repo_root / record.lean_raw_artifact
    if (
        record.invocation_id != run.invocation.invocation_id
        or not raw_path.is_file()
        or hash_file(raw_path) != record.lean_raw_sha256
        or record.parsed_statement_sha256 != run.terminal.parsed_statement_sha256
    ):
        raise RCPQwenQualificationError("Qwen Lean operational validation does not replay")
    return record


def verify_qwen_qualification(
    loaded: LoadedQwenQualification,
    *,
    repo_root: Path,
    credential: str,
    mathlib_project_dir: Path | None = None,
) -> QwenVerificationRun:
    """Offline replay; optionally create the one missing LeanInteract audit."""

    run = load_completed_qwen_run(loaded, repo_root=repo_root)
    if run.terminal.status is not RCPQwenTerminalStatus.RAW_COLLECTED:
        raise RCPQwenQualificationError("verification requires a parsed raw output")
    validation_path = run.output_directory / "operational_validation.json"
    if validation_path.exists():
        validation = _load_operational_validation(loaded, run, repo_root=repo_root)
    else:
        if mathlib_project_dir is None:
            raise RCPQwenQualificationError(
                "operational validation absent; supply --mathlib-project-dir"
            )
        validation = _validate_lean(
            loaded,
            run,
            repo_root=repo_root,
            mathlib_project_dir=mathlib_project_dir,
        )
    lean_raw_path = repo_root / validation.lean_raw_artifact
    scanned = _scan_secret_material(
        run.output_directory,
        credential,
        extra_paths=(lean_raw_path,),
    )
    paths = tuple(
        sorted(
            (
                path
                for path in run.output_directory.rglob("*")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: str(path.relative_to(repo_root)),
        )
    )
    inventory = {
        _repo_relative(path, repo_root): hash_file(path) for path in (*paths, lean_raw_path)
    }
    payload = {
        "schema_version": 1,
        "run_key": loaded.run_key,
        "manifest_id": run.manifest.manifest_id,
        "operational_validation_id": validation.validation_id,
        "artifact_inventory": inventory,
        "artifact_inventory_sha256": hash_canonical(inventory),
        "secret_files_scanned": scanned,
        "exact_credential_occurrences": 0,
        "bearer_header_occurrences": 0,
        "authorization_field_occurrences": 0,
        "provider_calls_performed": 0,
        "network_requests_performed": 0,
        "immutable_replay_passed": True,
        "capability_claim": "route_accepted_complete_payload_application_unproven",
        "reference_transmission_performed": False,
        "private_source_transmission_performed": False,
        "semantic_labels_created": False,
        "semantic_faithfulness_assessed": False,
        "supervision_eligible": False,
        "gate_credit_claimed": False,
        "gate_closed": False,
    }
    verification_id = "rcp_qwen_verification:" + hash_canonical(
        {"schema": "lf021_rcp_qwen_verification_v1", **payload}
    )
    report = QwenVerificationReport.model_validate({"verification_id": verification_id, **payload})
    report_path = (
        repo_root / loaded.loaded_config.config.outputs.audit_root / f"{loaded.run_key}.json"
    )
    report_sha256 = _persist_immutable(report_path, _record_bytes(report))
    return QwenVerificationRun(
        operational_validation=validation,
        report=report,
        report_path=report_path,
        report_sha256=report_sha256,
    )
