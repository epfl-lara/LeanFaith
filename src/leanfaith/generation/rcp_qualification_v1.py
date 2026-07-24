"""Reference-blind, one-problem RCP qualification for LF-021.

This additive module does not modify or reinterpret any local-family
qualification or collection artifact.  It implements one OpenAI-compatible
remote-on-prem transport for EPFL RCP, with deterministic request, attempt,
response, and terminal lineage.  It has no bulk-collection entrypoint and
creates no semantic label or Gate claim.

Only ``RCP_BASE_URL`` and ``RCP_API_KEY`` are read from the environment.  The
credential is held only in a non-serializable runtime object and is redacted
from exception text before persistence.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation.prompts import (
    DirectOutputParseError,
    ParsedLeanDeclaration,
    parse_direct_autoformalization_output,
)
from leanfaith.generation.providers import (
    DecodingValue,
    PrivateContentTransmissionError,
    ProviderIdentity,
    ProviderRawResponse,
    ProviderRequest,
    create_provider_request_for_problem,
    persist_provider_raw_response,
    persist_provider_request,
)
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import TheoremRecord

_HEX64 = r"^[0-9a-f]{64}$"
_CONFIG_ID = "lf021_rcp_kimi_qualification_v1"
_EXPECTED_BASE_URL_ENV = "RCP_BASE_URL"
_EXPECTED_API_KEY_ENV = "RCP_API_KEY"
_PROMPT_HASH_TOKEN = "{{PROMPT_TEMPLATE_SHA256}}"
_PROBLEM_JSON_TOKEN = "{{PROBLEM_JSON}}"
_DECLARATION_TOKEN = "{{DECLARATION_NAME}}"
_TEMPLATE_TOKEN = re.compile(r"\{\{[A-Z0-9_]+\}\}")
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:RCP_API_KEY|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+"
)


class RCPQualificationError(RuntimeError):
    """Base class for fail-closed RCP qualification errors."""


class RCPConfigurationError(RCPQualificationError):
    """The frozen config or one of its repository artifacts drifted."""


class RCPCredentialError(RCPQualificationError):
    """The exact required environment credentials are absent or invalid."""


class RCPCatalogError(RCPQualificationError):
    """The live RCP model catalog did not validate the frozen model IDs."""


class RCPTransportError(RCPQualificationError):
    """One remote transport attempt failed before a usable model response."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool,
        http_status: int | None = None,
        response_body: bytes | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.response_body = response_body
        super().__init__(detail)


class RCPArtifactConflict(RCPQualificationError):
    """An immutable qualification artifact already contains different bytes."""


class RCPModelSpec(StrictModel):
    """One public service model and its non-authoritative research role."""

    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
    role: Literal["generator"] = "generator"
    use: Literal[
        "primary_qualification_and_candidate_generator",
        "explicit_fallback_and_ablation_only",
    ]
    family_id: Literal["moonshot_kimi_k2"] = "moonshot_kimi_k2"
    diversity_group: Literal["moonshot_kimi_k2"] = "moonshot_kimi_k2"
    counts_as_independent_family: Literal[False] = False
    judge_eligible: Literal[False] = False
    checkpoint_revision_status: Literal["unavailable_from_rcp_route_id"]
    training_cutoff_status: Literal["unknown"]
    contamination_status: Literal["unknown"]
    unseen_claim_eligible: Literal[False] = False
    heldout_claim_eligible: Literal[False] = False
    evaluation_claim_eligible: Literal[False] = False
    supplemental_generator_candidates_only: Literal[True] = True


class RCPModelRegistry(StrictModel):
    primary: RCPModelSpec
    fallback: RCPModelSpec

    @model_validator(mode="after")
    def _distinct_checkpoints_same_family(self) -> Self:
        if self.primary.model_id == self.fallback.model_id:
            raise ValueError("primary and fallback model IDs must differ")
        if (
            self.primary.family_id != self.fallback.family_id
            or self.primary.diversity_group != self.fallback.diversity_group
        ):
            raise ValueError("Moonshot checkpoints must share one family/diversity group")
        if self.primary.use != "primary_qualification_and_candidate_generator":
            raise ValueError("primary model use is inconsistent")
        if self.fallback.use != "explicit_fallback_and_ablation_only":
            raise ValueError("fallback model use is inconsistent")
        return self


class RCPTransportConfig(StrictModel):
    transport_id: Literal["epfl_rcp_openai_compatible_v1"]
    provenance: Literal["remote_on_prem_epfl_rcp"]
    base_url_env: Literal["RCP_BASE_URL"]
    api_key_env: Literal["RCP_API_KEY"]
    expected_base_url: Literal["https://inference.rcp.epfl.ch/v1"]
    catalog_path: Literal["/models"] = "/models"
    chat_completions_path: Literal["/chat/completions"] = "/chat/completions"


class RCPProblemBinding(StrictModel):
    records_artifact: str
    records_sha256: str = Field(pattern=_HEX64)
    reference_theorems_artifact: str
    reference_theorems_sha256: str = Field(pattern=_HEX64)
    expected_problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    required_private_source_content: Literal[False] = False
    required_external_provider_eligible: Literal[True] = True
    require_reference_hidden: Literal[True] = True


class RCPPromptConfig(StrictModel):
    artifact: str
    sha256: str = Field(pattern=_HEX64)
    declaration_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_']*$")
    system_message: str = Field(min_length=1)


class RCPDecodingConfig(StrictModel):
    temperature: float = Field(ge=0.0, le=2.0)
    max_tokens: int = Field(ge=1, le=65536)
    seed: int = Field(ge=0)
    stream: Literal[False] = False
    reasoning_effort: Literal["high"] = "high"
    chat_template_enable_thinking: Literal[True] = True

    def provider_decoding(self) -> dict[str, DecodingValue]:
        """Flatten provider extras into the canonical ProviderRequest hash."""

        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stream": self.stream,
            "reasoning_effort": self.reasoning_effort,
            "chat_template_enable_thinking": self.chat_template_enable_thinking,
        }

    def wire_fields(self) -> dict[str, object]:
        """Return OpenAI-compatible fields; ``extra_body`` merges at top level."""

        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stream": self.stream,
            "reasoning_effort": self.reasoning_effort,
            "chat_template_kwargs": {
                "enable_thinking": self.chat_template_enable_thinking,
            },
        }


class RCPRetryConfig(StrictModel):
    max_attempts: int = Field(ge=1, le=5)
    request_timeout_seconds: int = Field(ge=1, le=3600)
    catalog_timeout_seconds: int = Field(ge=1, le=300)
    retry_delays_seconds: tuple[int, ...]
    retryable_http_statuses: tuple[int, ...]
    cold_start_markers: tuple[str, ...]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len(self.retry_delays_seconds) != self.max_attempts - 1:
            raise ValueError("retry delays must contain max_attempts - 1 entries")
        if any(delay < 0 for delay in self.retry_delays_seconds):
            raise ValueError("retry delays must be nonnegative")
        if tuple(sorted(set(self.retryable_http_statuses))) != self.retryable_http_statuses:
            raise ValueError("retryable HTTP statuses must be sorted and unique")
        markers = tuple(marker.casefold().strip() for marker in self.cold_start_markers)
        if any(not marker for marker in markers) or len(markers) != len(set(markers)):
            raise ValueError("cold-start markers must be nonempty and unique")
        return self


class RCPOutputConfig(StrictModel):
    root: str
    preflight_root: str


class RCPPolicyConfig(StrictModel):
    public_source_only: Literal[True] = True
    private_source_transmission_forbidden: Literal[True] = True
    reference_transmission_forbidden: Literal[True] = True
    bulk_execution_available: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False
    fallback_counts_as_independent_family: Literal[False] = False


class RCPQualificationConfig(StrictModel):
    schema_version: Literal[1] = 1
    config_id: Literal["lf021_rcp_kimi_qualification_v1"]
    frozen_at: datetime.datetime
    status: Literal["qualification_ready"]
    artifact_class: Literal["qualification"] = "qualification"
    transport: RCPTransportConfig
    models: RCPModelRegistry
    problem: RCPProblemBinding
    prompt: RCPPromptConfig
    decoding: RCPDecodingConfig
    retry: RCPRetryConfig
    outputs: RCPOutputConfig
    policy: RCPPolicyConfig

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.frozen_at.tzinfo is None or self.frozen_at.utcoffset() != datetime.timedelta(0):
            raise ValueError("frozen_at must be UTC")
        for label, value in (
            ("problem records", self.problem.records_artifact),
            ("reference theorems", self.problem.reference_theorems_artifact),
            ("prompt", self.prompt.artifact),
            ("output root", self.outputs.root),
            ("preflight root", self.outputs.preflight_root),
        ):
            path = PurePosixPath(value)
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise ValueError(f"{label} path must be repository-relative")
        return self


@dataclass(frozen=True, slots=True, repr=False)
class RCPCredentials:
    """Runtime-only credential bundle; never serialized or hashed."""

    base_url: str
    api_key: str

    def __repr__(self) -> str:
        return f"RCPCredentials(base_url={self.base_url!r}, api_key='<redacted>')"


class RCPHTTPResponse(StrictModel):
    """In-memory response from the small stdlib transport."""

    status_code: int = Field(ge=100, le=599)
    body: bytes


class RCPHTTPTransport(Protocol):
    """Injectable network boundary used by the live adapter and unit tests."""

    def get(
        self,
        *,
        url: str,
        api_key: str,
        timeout_seconds: int,
    ) -> RCPHTTPResponse: ...

    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> RCPHTTPResponse: ...


class UrllibRCPTransport:
    """OpenAI-compatible HTTPS transport with no third-party dependency."""

    @staticmethod
    def _execute(request: urllib.request.Request, timeout_seconds: int) -> RCPHTTPResponse:
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return RCPHTTPResponse(
                    status_code=int(response.status),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return RCPHTTPResponse(status_code=int(exc.code), body=exc.read())
        except TimeoutError as exc:
            raise RCPTransportError(
                "timeout",
                "RCP request timed out",
                retryable=True,
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            retryable = isinstance(reason, TimeoutError)
            raise RCPTransportError(
                "network_error",
                "RCP network request failed",
                retryable=retryable,
            ) from exc

    def get(
        self,
        *,
        url: str,
        api_key: str,
        timeout_seconds: int,
    ) -> RCPHTTPResponse:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "LeanFaith-LF021-RCP-Qualification/1",
            },
            method="GET",
        )
        return self._execute(request, timeout_seconds)

    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> RCPHTTPResponse:
        request = urllib.request.Request(
            url,
            data=canonical_json_bytes(payload),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "LeanFaith-LF021-RCP-Qualification/1",
            },
            method="POST",
        )
        return self._execute(request, timeout_seconds)


class RCPModelCatalogObservation(StrictModel):
    schema_version: Literal[1] = 1
    observation_id: str = Field(pattern=r"^rcp_catalog_observation:[0-9a-f]{64}$")
    transport: Literal["remote_on_prem_epfl_rcp"]
    endpoint: str
    endpoint_sha256: str = Field(pattern=_HEX64)
    observed_at: datetime.datetime
    http_status: Literal[200] = 200
    raw_response_sha256: str = Field(pattern=_HEX64)
    canonical_model_ids_sha256: str = Field(pattern=_HEX64)
    model_count: int = Field(ge=1)
    exact_model_ids: tuple[str, str]
    exact_models_present: tuple[Literal[True], Literal[True]]
    credential_serialized: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "observation_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != datetime.timedelta(0):
            raise ValueError("catalog observed_at must be UTC")
        if self.endpoint_sha256 != sha256_hex(self.endpoint.encode("utf-8")):
            raise ValueError("catalog endpoint hash differs")
        expected = "rcp_catalog_observation:" + hash_canonical(
            {"schema": "lf021_rcp_catalog_observation_v1", **self.id_payload()}
        )
        if self.observation_id != expected:
            raise ValueError("catalog observation ID differs")
        return self

    @classmethod
    def create(
        cls,
        *,
        endpoint: str,
        observed_at: datetime.datetime,
        raw_response_sha256: str,
        model_ids: tuple[str, ...],
        exact_model_ids: tuple[str, str],
    ) -> Self:
        payload = {
            "schema_version": 1,
            "transport": "remote_on_prem_epfl_rcp",
            "endpoint": endpoint,
            "endpoint_sha256": sha256_hex(endpoint.encode("utf-8")),
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "http_status": 200,
            "raw_response_sha256": raw_response_sha256,
            "canonical_model_ids_sha256": hash_canonical(model_ids),
            "model_count": len(model_ids),
            "exact_model_ids": exact_model_ids,
            "exact_models_present": (True, True),
            "credential_serialized": False,
        }
        observation_id = "rcp_catalog_observation:" + hash_canonical(
            {"schema": "lf021_rcp_catalog_observation_v1", **payload}
        )
        return cls.model_validate({"observation_id": observation_id, **payload})


class RCPReferenceBlindAudit(StrictModel):
    schema_version: Literal[1] = 1
    problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    rendered_prompt_sha256: str = Field(pattern=_HEX64)
    reference_theorem_ids_absent: Literal[True] = True
    reference_declaration_names_absent: Literal[True] = True
    reference_signatures_absent: Literal[True] = True
    source_links_absent: Literal[True] = True
    provider_payload_keys: tuple[str, ...]
    reference_transmission_performed: Literal[False] = False


class RCPAttemptStatus(StrEnum):
    RESPONSE_RECEIVED = "response_received"
    TIMEOUT = "timeout"
    RETRYABLE_HTTP_ERROR = "retryable_http_error"
    TERMINAL_HTTP_ERROR = "terminal_http_error"
    TRANSPORT_ERROR = "transport_error"
    INVALID_RESPONSE = "invalid_response"


class RCPAttemptRecord(StrictModel):
    schema_version: Literal[1] = 1
    attempt_record_id: str = Field(pattern=r"^rcp_attempt_record:[0-9a-f]{64}$")
    invocation_id: str = Field(pattern=r"^rcp_qualification_invocation:[0-9a-f]{64}$")
    request_hash: str = Field(pattern=_HEX64)
    provider_attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    attempt_index: int = Field(ge=0)
    request_artifact: str
    request_artifact_sha256: str = Field(pattern=_HEX64)
    wire_response_artifact: str
    wire_response_sha256: str = Field(pattern=_HEX64)
    provider_response_artifact: str
    provider_response_sha256: str = Field(pattern=_HEX64)
    status: RCPAttemptStatus
    retryable: bool
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_request_id: str | None = None
    returned_model: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    tokens: dict[str, int] = Field(default_factory=dict)
    started_at: datetime.datetime
    completed_at: datetime.datetime
    latency_ms: int = Field(ge=0)
    api_key_serialized: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            "invocation_id": self.invocation_id,
            "request_hash": self.request_hash,
            "provider_attempt_id": self.provider_attempt_id,
            "attempt_index": self.attempt_index,
            "request_artifact_sha256": self.request_artifact_sha256,
            "wire_response_sha256": self.wire_response_sha256,
            "provider_response_sha256": self.provider_response_sha256,
            "status": self.status.value,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "provider_request_id": self.provider_request_id,
            "returned_model": self.returned_model,
            "error_code": self.error_code,
            "tokens": self.tokens,
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("attempt timestamps must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("attempt completion precedes start")
        expected_latency = max(
            0,
            int((self.completed_at - self.started_at).total_seconds() * 1000),
        )
        if self.latency_ms != expected_latency:
            raise ValueError("attempt latency differs from timestamps")
        expected = "rcp_attempt_record:" + hash_canonical(
            {"schema": "lf021_rcp_attempt_record_v1", **self.id_payload()}
        )
        if self.attempt_record_id != expected:
            raise ValueError("attempt record ID differs")
        if self.status is RCPAttemptStatus.RESPONSE_RECEIVED:
            if self.retryable or self.error_code is not None:
                raise ValueError("successful attempt cannot be retryable or carry error")
        elif self.error_code is None:
            raise ValueError("failed attempt requires an error code")
        return self


class RCPQualificationInvocation(StrictModel):
    schema_version: Literal[1] = 1
    invocation_id: str = Field(pattern=r"^rcp_qualification_invocation:[0-9a-f]{64}$")
    config_hash: str = Field(pattern=_HEX64)
    catalog_observation_id: str = Field(pattern=r"^rcp_catalog_observation:[0-9a-f]{64}$")
    catalog_raw_response_sha256: str = Field(pattern=_HEX64)
    transport: Literal["remote_on_prem_epfl_rcp"]
    provider: Literal["epfl_rcp"]
    provider_slot: Literal["rcp_moonshot_kimi_qualification_v1"]
    model_id: str
    model_selection: Literal["primary", "fallback"]
    model_family: Literal["moonshot_kimi_k2"]
    counts_as_independent_family: Literal[False] = False
    role: Literal["generator"] = "generator"
    judge_eligible: Literal[False] = False
    checkpoint_revision_status: Literal["unavailable_from_rcp_route_id"]
    training_cutoff_status: Literal["unknown"]
    contamination_status: Literal["unknown"]
    unseen_claim_eligible: Literal[False] = False
    heldout_claim_eligible: Literal[False] = False
    evaluation_claim_eligible: Literal[False] = False
    supplemental_generator_candidates_only: Literal[True] = True
    problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    prompt_template_sha256: str = Field(pattern=_HEX64)
    rendered_prompt_sha256: str = Field(pattern=_HEX64)
    decoding: dict[str, DecodingValue]
    decoding_sha256: str = Field(pattern=_HEX64)
    reference_hidden: Literal[True] = True
    private_source_content: Literal[False] = False
    external_provider_eligible: Literal[True] = True
    semantic_labels_created: Literal[False] = False
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
            raise ValueError("invocation decoding hash differs")
        expected = "rcp_qualification_invocation:" + hash_canonical(
            {"schema": "lf021_rcp_qualification_invocation_v1", **self.id_payload()}
        )
        if self.invocation_id != expected:
            raise ValueError("qualification invocation ID differs")
        return self

    @classmethod
    def create(
        cls,
        *,
        config_hash: str,
        catalog: RCPModelCatalogObservation,
        model: RCPModelSpec,
        model_selection: Literal["primary", "fallback"],
        problem: ProblemPoolRecord,
        prompt_template_sha256: str,
        rendered_prompt_sha256: str,
        decoding: dict[str, DecodingValue],
    ) -> Self:
        payload = {
            "schema_version": 1,
            "config_hash": config_hash,
            "catalog_observation_id": catalog.observation_id,
            "catalog_raw_response_sha256": catalog.raw_response_sha256,
            "transport": "remote_on_prem_epfl_rcp",
            "provider": "epfl_rcp",
            "provider_slot": "rcp_moonshot_kimi_qualification_v1",
            "model_id": model.model_id,
            "model_selection": model_selection,
            "model_family": model.family_id,
            "counts_as_independent_family": False,
            "role": "generator",
            "judge_eligible": False,
            "checkpoint_revision_status": model.checkpoint_revision_status,
            "training_cutoff_status": model.training_cutoff_status,
            "contamination_status": model.contamination_status,
            "unseen_claim_eligible": False,
            "heldout_claim_eligible": False,
            "evaluation_claim_eligible": False,
            "supplemental_generator_candidates_only": True,
            "problem_record_id": problem.problem_record_id,
            "prompt_template_sha256": prompt_template_sha256,
            "rendered_prompt_sha256": rendered_prompt_sha256,
            "decoding": decoding,
            "decoding_sha256": hash_canonical(decoding),
            "reference_hidden": True,
            "private_source_content": False,
            "external_provider_eligible": True,
            "semantic_labels_created": False,
            "gate_credit_claimed": False,
        }
        invocation_id = "rcp_qualification_invocation:" + hash_canonical(
            {"schema": "lf021_rcp_qualification_invocation_v1", **payload}
        )
        return cls.model_validate({"invocation_id": invocation_id, **payload})


class RCPTerminalStatus(StrEnum):
    RAW_COLLECTED = "raw_collected"
    PARSE_FAILED = "parse_failed"
    EXHAUSTED = "exhausted"


class RCPQualificationTerminal(StrictModel):
    schema_version: Literal[1] = 1
    terminal_id: str = Field(pattern=r"^rcp_qualification_terminal:[0-9a-f]{64}$")
    invocation_id: str = Field(pattern=r"^rcp_qualification_invocation:[0-9a-f]{64}$")
    model_id: str
    model_selection: Literal["primary", "fallback"]
    attempt_record_ids: tuple[str, ...] = Field(min_length=1)
    status: RCPTerminalStatus
    output_sha256: str | None = Field(default=None, pattern=_HEX64)
    parsed_statement_sha256: str | None = Field(default=None, pattern=_HEX64)
    parse_error_code: str | None = None
    final_attempt_artifact_sha256: str = Field(pattern=_HEX64)
    started_at: datetime.datetime
    completed_at: datetime.datetime
    reference_hidden: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            "invocation_id": self.invocation_id,
            "model_id": self.model_id,
            "model_selection": self.model_selection,
            "attempt_record_ids": self.attempt_record_ids,
            "status": self.status.value,
            "output_sha256": self.output_sha256,
            "parsed_statement_sha256": self.parsed_statement_sha256,
            "parse_error_code": self.parse_error_code,
            "final_attempt_artifact_sha256": self.final_attempt_artifact_sha256,
            "reference_hidden": self.reference_hidden,
            "semantic_labels_created": self.semantic_labels_created,
            "supervision_eligible": self.supervision_eligible,
            "gate_credit_claimed": self.gate_credit_claimed,
            "gate_closed": self.gate_closed,
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("terminal completion precedes start")
        expected = "rcp_qualification_terminal:" + hash_canonical(
            {"schema": "lf021_rcp_qualification_terminal_v1", **self.id_payload()}
        )
        if self.terminal_id != expected:
            raise ValueError("qualification terminal ID differs")
        if self.status is RCPTerminalStatus.RAW_COLLECTED:
            if self.output_sha256 is None or self.parsed_statement_sha256 is None:
                raise ValueError("raw_collected terminal requires parsed output hashes")
            if self.parse_error_code is not None:
                raise ValueError("raw_collected terminal cannot carry parse error")
        elif self.status is RCPTerminalStatus.PARSE_FAILED:
            if self.output_sha256 is None or self.parse_error_code is None:
                raise ValueError("parse_failed terminal requires output and parse error")
            if self.parsed_statement_sha256 is not None:
                raise ValueError("parse_failed terminal cannot carry a parsed statement")
        elif (
            self.output_sha256 is not None
            or self.parsed_statement_sha256 is not None
            or self.parse_error_code is not None
        ):
            raise ValueError("exhausted terminal cannot carry output or parse fields")
        return self


class RCPQualificationPreflight(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf021_rcp_kimi_qualification_preflight_v1"]
    config_id: Literal["lf021_rcp_kimi_qualification_v1"]
    config_file_sha256: str = Field(pattern=_HEX64)
    config_hash: str = Field(pattern=_HEX64)
    catalog: RCPModelCatalogObservation
    problem_record_id: str = Field(pattern=r"^problem:[0-9a-f]{64}$")
    problem_records_sha256: str = Field(pattern=_HEX64)
    reference_theorems_sha256: str = Field(pattern=_HEX64)
    prompt_template_sha256: str = Field(pattern=_HEX64)
    rendered_prompt_sha256: str = Field(pattern=_HEX64)
    reference_blind_audit: RCPReferenceBlindAudit
    selected_model_id: str
    selected_model_role: Literal["generator"] = "generator"
    selected_model_judge_eligible: Literal[False] = False
    same_family_as_fallback: Literal[True] = True
    bulk_execution_available: Literal[False] = False
    provider_requests_created: Literal[0] = 0
    private_source_transmission_performed: Literal[False] = False
    reference_transmission_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class LoadedRCPQualification:
    loaded_config: LoadedConfig[RCPQualificationConfig]
    problem: ProblemPoolRecord
    reference_theorems: tuple[TheoremRecord, ...]
    prompt_template_sha256: str
    rendered_prompt: str
    rendered_prompt_sha256: str
    reference_blind_audit: RCPReferenceBlindAudit


@dataclass(frozen=True, slots=True)
class RCPQualificationRun:
    output_directory: Path
    terminal_path: Path
    terminal: RCPQualificationTerminal
    attempt_paths: tuple[Path, ...]
    parsed_declaration: ParsedLeanDeclaration | None


def _canonical_record_bytes(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _persist_immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise RCPArtifactConflict(f"immutable artifact conflict at {path}")
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
                raise RCPArtifactConflict(
                    f"concurrent immutable artifact conflict at {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_repo_file(repo_root: Path, artifact: str, expected_hash: str) -> Path:
    relative = PurePosixPath(artifact)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RCPConfigurationError(f"unsafe repository artifact path: {artifact}")
    root = repo_root.resolve()
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise RCPConfigurationError(f"artifact is missing or not a regular file: {artifact}")
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise RCPConfigurationError(f"artifact escapes repository root: {artifact}") from exc
    observed = hash_file(path)
    if observed != expected_hash:
        raise RCPConfigurationError(
            f"artifact hash drift for {artifact}: {observed} != {expected_hash}"
        )
    return path


def _load_single_jsonl(path: Path, model: type[StrictModel]) -> StrictModel:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise RCPConfigurationError(f"qualification input must contain exactly one record: {path}")
    try:
        return model.model_validate_json(lines[0])
    except ValueError as exc:
        raise RCPConfigurationError(f"invalid qualification record {path}: {exc}") from exc


def _render_reference_blind_prompt(
    *,
    template_path: Path,
    nl_statement: str,
    declaration_name: str,
) -> tuple[str, str, str]:
    template_bytes = template_path.read_bytes()
    try:
        template = template_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RCPConfigurationError("RCP prompt template is not UTF-8") from exc
    expected = {_PROMPT_HASH_TOKEN, _PROBLEM_JSON_TOKEN, _DECLARATION_TOKEN}
    if set(_TEMPLATE_TOKEN.findall(template)) != expected or any(
        template.count(token) != 1 for token in expected
    ):
        raise RCPConfigurationError("RCP prompt template placeholders are invalid")
    template_hash = sha256_hex(template_bytes)
    payload = {
        "schema": "reference_blind_public_problem_v1",
        "nl_statement": nl_statement,
    }
    rendered = (
        template.replace(_PROMPT_HASH_TOKEN, template_hash)
        .replace(_DECLARATION_TOKEN, declaration_name)
        .replace(_PROBLEM_JSON_TOKEN, canonical_json_bytes(payload).decode("utf-8"))
    )
    if _TEMPLATE_TOKEN.search(rendered):
        raise RCPConfigurationError("RCP prompt contains unresolved placeholders")
    return template_hash, rendered, sha256_hex(rendered.encode("utf-8"))


def _reference_blind_audit(
    *,
    problem: ProblemPoolRecord,
    reference_theorems: tuple[TheoremRecord, ...],
    rendered_prompt: str,
    rendered_prompt_sha256: str,
) -> RCPReferenceBlindAudit:
    if problem.private_source_content or not problem.external_provider_eligible:
        raise PrivateContentTransmissionError(
            "RCP qualification requires public external-provider-eligible content"
        )
    lowered = rendered_prompt.casefold()
    if any(theorem_id in rendered_prompt for theorem_id in problem.reference_theorem_ids):
        raise RCPConfigurationError("rendered RCP prompt leaks a reference theorem ID")
    reference_names = tuple(
        name.casefold()
        for theorem in reference_theorems
        for name in (theorem.declaration_full_name, theorem.declaration_name)
        if name
    )
    if any(name in lowered for name in reference_names):
        raise RCPConfigurationError("rendered RCP prompt leaks a reference declaration name")
    signatures = tuple(
        signature
        for theorem in reference_theorems
        for signature in (
            theorem.proof_stripped_declaration,
            theorem.inline_elaboration_source,
        )
        if signature and len(signature.strip()) >= 16
    )
    if any(signature in rendered_prompt for signature in signatures):
        raise RCPConfigurationError("rendered RCP prompt leaks a trusted Lean signature")
    if problem.nl_source_link in rendered_prompt:
        raise RCPConfigurationError("rendered RCP prompt leaks the source link")
    return RCPReferenceBlindAudit(
        problem_record_id=problem.problem_record_id,
        rendered_prompt_sha256=rendered_prompt_sha256,
        provider_payload_keys=(
            "model",
            "messages",
            "temperature",
            "max_tokens",
            "seed",
            "stream",
            "reasoning_effort",
            "chat_template_kwargs",
        ),
    )


def load_rcp_qualification(
    config_path: Path,
    *,
    repo_root: Path,
) -> LoadedRCPQualification:
    loaded = load_config(config_path, RCPQualificationConfig)
    config = loaded.config
    records_path = _safe_repo_file(
        repo_root,
        config.problem.records_artifact,
        config.problem.records_sha256,
    )
    references_path = _safe_repo_file(
        repo_root,
        config.problem.reference_theorems_artifact,
        config.problem.reference_theorems_sha256,
    )
    prompt_path = _safe_repo_file(
        repo_root,
        config.prompt.artifact,
        config.prompt.sha256,
    )
    problem = _load_single_jsonl(records_path, ProblemPoolRecord)
    assert isinstance(problem, ProblemPoolRecord)
    reference = _load_single_jsonl(references_path, TheoremRecord)
    assert isinstance(reference, TheoremRecord)
    references = (reference,)
    if problem.problem_record_id != config.problem.expected_problem_record_id:
        raise RCPConfigurationError("qualification problem ID differs from config")
    if (
        problem.private_source_content != config.problem.required_private_source_content
        or problem.external_provider_eligible != config.problem.required_external_provider_eligible
        or problem.eligibility != "eligible"
        or not problem.denylist_checked
        or problem.denylist_hits
    ):
        raise RCPConfigurationError("qualification problem is not provider-eligible")
    if tuple(problem.reference_theorem_ids) != tuple(theorem.theorem_id for theorem in references):
        raise RCPConfigurationError("qualification reference theorem binding differs")
    template_hash, rendered_prompt, render_hash = _render_reference_blind_prompt(
        template_path=prompt_path,
        nl_statement=problem.nl_statement,
        declaration_name=config.prompt.declaration_name,
    )
    audit = _reference_blind_audit(
        problem=problem,
        reference_theorems=references,
        rendered_prompt=rendered_prompt,
        rendered_prompt_sha256=render_hash,
    )
    return LoadedRCPQualification(
        loaded_config=loaded,
        problem=problem,
        reference_theorems=references,
        prompt_template_sha256=template_hash,
        rendered_prompt=rendered_prompt,
        rendered_prompt_sha256=render_hash,
        reference_blind_audit=audit,
    )


def resolve_rcp_credentials(config: RCPQualificationConfig) -> RCPCredentials:
    """Read only the two frozen RCP environment variables."""

    base_url = os.environ.get(_EXPECTED_BASE_URL_ENV, "").strip().rstrip("/")
    api_key = os.environ.get(_EXPECTED_API_KEY_ENV, "").strip()
    if not base_url:
        raise RCPCredentialError("RCP_BASE_URL is unset or empty")
    if not api_key:
        raise RCPCredentialError("RCP_API_KEY is unset or empty")
    if base_url != config.transport.expected_base_url:
        raise RCPCredentialError("RCP_BASE_URL does not match the frozen HTTPS endpoint")
    if not base_url.startswith("https://"):
        raise RCPCredentialError("RCP_BASE_URL must use HTTPS")
    return RCPCredentials(base_url=base_url, api_key=api_key)


def redact_rcp_text(text: str, *, api_key: str) -> str:
    """Redact exact and patterned credential material before persistence."""

    result = text.replace(api_key, "<redacted>") if api_key else text
    result = _BEARER_PATTERN.sub("Bearer <redacted>", result)
    return _KEY_ASSIGNMENT_PATTERN.sub("credential=<redacted>", result)


def _json_document(body: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RCPTransportError(
            "invalid_json",
            f"{label} returned invalid JSON",
            retryable=False,
            response_body=body,
        ) from exc
    if not isinstance(document, dict):
        raise RCPTransportError(
            "invalid_json_shape",
            f"{label} JSON root is not an object",
            retryable=False,
            response_body=body,
        )
    return document


def probe_rcp_catalog(
    loaded: LoadedRCPQualification,
    *,
    credentials: RCPCredentials,
    transport: RCPHTTPTransport,
    clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC),
) -> RCPModelCatalogObservation:
    config = loaded.loaded_config.config
    endpoint = credentials.base_url + config.transport.catalog_path
    response = transport.get(
        url=endpoint,
        api_key=credentials.api_key,
        timeout_seconds=config.retry.catalog_timeout_seconds,
    )
    if response.status_code != 200:
        detail = redact_rcp_text(
            response.body.decode("utf-8", errors="replace")[:1000],
            api_key=credentials.api_key,
        )
        raise RCPCatalogError(f"RCP /models returned HTTP {response.status_code}: {detail}")
    document = _json_document(response.body, label="RCP /models")
    data = document.get("data")
    if not isinstance(data, list):
        raise RCPCatalogError("RCP /models response lacks data array")
    model_ids = tuple(
        sorted(
            {
                str(item["id"])
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )
    )
    exact = (
        config.models.primary.model_id,
        config.models.fallback.model_id,
    )
    missing = tuple(model for model in exact if model not in model_ids)
    if missing:
        raise RCPCatalogError("RCP catalog lacks exact frozen model IDs: " + ", ".join(missing))
    return RCPModelCatalogObservation.create(
        endpoint=endpoint,
        observed_at=clock(),
        raw_response_sha256=sha256_hex(response.body),
        model_ids=model_ids,
        exact_model_ids=exact,
    )


def build_rcp_preflight(
    loaded: LoadedRCPQualification,
    *,
    catalog: RCPModelCatalogObservation,
    config_file_sha256: str,
) -> RCPQualificationPreflight:
    config = loaded.loaded_config.config
    return RCPQualificationPreflight(
        artifact_kind="lf021_rcp_kimi_qualification_preflight_v1",
        config_id=config.config_id,
        config_file_sha256=config_file_sha256,
        config_hash=loaded.loaded_config.config_hash,
        catalog=catalog,
        problem_record_id=loaded.problem.problem_record_id,
        problem_records_sha256=config.problem.records_sha256,
        reference_theorems_sha256=config.problem.reference_theorems_sha256,
        prompt_template_sha256=loaded.prompt_template_sha256,
        rendered_prompt_sha256=loaded.rendered_prompt_sha256,
        reference_blind_audit=loaded.reference_blind_audit,
        selected_model_id=config.models.primary.model_id,
    )


def write_rcp_preflight(
    loaded: LoadedRCPQualification,
    *,
    catalog: RCPModelCatalogObservation,
    repo_root: Path,
) -> tuple[Path, str]:
    config = loaded.loaded_config.config
    preflight = build_rcp_preflight(
        loaded,
        catalog=catalog,
        config_file_sha256=hash_file(loaded.loaded_config.path),
    )
    observation_suffix = catalog.observation_id.rsplit(":", 1)[-1]
    path = repo_root / config.outputs.preflight_root / f"{observation_suffix}.json"
    digest = _persist_immutable(path, _canonical_record_bytes(preflight))
    return path, digest


def _wire_payload(
    *,
    config: RCPQualificationConfig,
    model_id: str,
    rendered_prompt: str,
) -> dict[str, object]:
    return {
        "model": model_id,
        "messages": [
            {"role": "system", "content": config.prompt.system_message},
            {"role": "user", "content": rendered_prompt},
        ],
        **config.decoding.wire_fields(),
    }


def _extract_completion(
    body: bytes,
) -> tuple[str, str | None, str | None, dict[str, int]]:
    document = _json_document(body, label="RCP chat completion")
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RCPTransportError(
            "invalid_response_shape",
            "RCP response lacks choices[0]",
            retryable=False,
            response_body=body,
        )
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise RCPTransportError(
            "invalid_response_shape",
            "RCP response lacks choices[0].message.content",
            retryable=False,
            response_body=body,
        )
    content = str(message["content"])
    if not content.strip():
        raise RCPTransportError(
            "empty_response",
            "RCP response content is empty",
            retryable=False,
            response_body=body,
        )
    request_id = document.get("id") if isinstance(document.get("id"), str) else None
    returned_model = document.get("model") if isinstance(document.get("model"), str) else None
    usage_raw = document.get("usage")
    usage: dict[str, int] = {}
    if isinstance(usage_raw, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage_raw.get(key)
            if isinstance(value, int) and value >= 0:
                usage[key] = value
    return content, request_id, returned_model, usage


def _classify_http_failure(
    *,
    response: RCPHTTPResponse,
    config: RCPRetryConfig,
    api_key: str,
) -> RCPTransportError:
    body_text = response.body.decode("utf-8", errors="replace")
    lowered = body_text.casefold()
    cold_start = any(marker.casefold() in lowered for marker in config.cold_start_markers)
    retryable = response.status_code in config.retryable_http_statuses or cold_start
    code = "cold_start" if cold_start else f"http_{response.status_code}"
    detail = redact_rcp_text(body_text[:1000], api_key=api_key)
    return RCPTransportError(
        code,
        detail or f"RCP returned HTTP {response.status_code}",
        retryable=retryable,
        http_status=response.status_code,
        response_body=response.body,
    )


def _relative_artifact(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise RCPQualificationError("qualification artifact escapes repository") from exc


def _attempt_record(
    *,
    invocation: RCPQualificationInvocation,
    request: ProviderRequest,
    request_path: Path,
    wire_path: Path,
    provider_response_path: Path,
    status: RCPAttemptStatus,
    retryable: bool,
    http_status: int | None,
    provider_request_id: str | None,
    returned_model: str | None,
    error_code: str | None,
    error_detail: str | None,
    tokens: dict[str, int],
    started_at: datetime.datetime,
    completed_at: datetime.datetime,
    repo_root: Path,
) -> RCPAttemptRecord:
    payload = {
        "schema_version": 1,
        "invocation_id": invocation.invocation_id,
        "request_hash": request.request_hash,
        "provider_attempt_id": request.attempt_id,
        "attempt_index": request.attempt_index,
        "request_artifact": _relative_artifact(request_path, repo_root),
        "request_artifact_sha256": hash_file(request_path),
        "wire_response_artifact": _relative_artifact(wire_path, repo_root),
        "wire_response_sha256": hash_file(wire_path),
        "provider_response_artifact": _relative_artifact(provider_response_path, repo_root),
        "provider_response_sha256": hash_file(provider_response_path),
        "status": status.value,
        "retryable": retryable,
        "http_status": http_status,
        "provider_request_id": provider_request_id,
        "returned_model": returned_model,
        "error_code": error_code,
        "error_detail": error_detail,
        "tokens": tokens,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "latency_ms": max(0, int((completed_at - started_at).total_seconds() * 1000)),
        "api_key_serialized": False,
    }
    stable = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "schema_version",
            "request_artifact",
            "wire_response_artifact",
            "provider_response_artifact",
            "error_detail",
            "started_at",
            "completed_at",
            "latency_ms",
            "api_key_serialized",
        }
    }
    attempt_record_id = "rcp_attempt_record:" + hash_canonical(
        {"schema": "lf021_rcp_attempt_record_v1", **stable}
    )
    return RCPAttemptRecord.model_validate({"attempt_record_id": attempt_record_id, **payload})


def _terminal(
    *,
    invocation: RCPQualificationInvocation,
    attempt_records: tuple[RCPAttemptRecord, ...],
    status: RCPTerminalStatus,
    output_sha256: str | None,
    parsed_statement_sha256: str | None,
    parse_error_code: str | None,
) -> RCPQualificationTerminal:
    first = attempt_records[0]
    final = attempt_records[-1]
    payload = {
        "schema_version": 1,
        "invocation_id": invocation.invocation_id,
        "model_id": invocation.model_id,
        "model_selection": invocation.model_selection,
        "attempt_record_ids": tuple(item.attempt_record_id for item in attempt_records),
        "status": status.value,
        "output_sha256": output_sha256,
        "parsed_statement_sha256": parsed_statement_sha256,
        "parse_error_code": parse_error_code,
        "final_attempt_artifact_sha256": hash_canonical(final.model_dump(mode="json")),
        "started_at": first.started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": final.completed_at.isoformat().replace("+00:00", "Z"),
        "reference_hidden": True,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_credit_claimed": False,
        "gate_closed": False,
    }
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"schema_version", "started_at", "completed_at"}
    }
    terminal_id = "rcp_qualification_terminal:" + hash_canonical(
        {"schema": "lf021_rcp_qualification_terminal_v1", **stable}
    )
    return RCPQualificationTerminal.model_validate({"terminal_id": terminal_id, **payload})


def _secret_absent(root: Path, api_key: str) -> None:
    needle = api_key.encode("utf-8")
    if not needle:
        return
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and needle in path.read_bytes():
            raise RCPQualificationError(f"credential material found in artifact {path}")


def execute_one_rcp_qualification(
    loaded: LoadedRCPQualification,
    *,
    catalog: RCPModelCatalogObservation,
    credentials: RCPCredentials,
    repo_root: Path,
    model_selection: Literal["primary", "fallback"] = "primary",
    transport: RCPHTTPTransport | None = None,
    clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC),
    sleeper: Callable[[float], None] = time.sleep,
) -> RCPQualificationRun:
    """Execute exactly one public, reference-blind qualification call."""

    config = loaded.loaded_config.config
    transport = transport or UrllibRCPTransport()
    model = config.models.primary if model_selection == "primary" else config.models.fallback
    decoding = config.decoding.provider_decoding()
    invocation = RCPQualificationInvocation.create(
        config_hash=loaded.loaded_config.config_hash,
        catalog=catalog,
        model=model,
        model_selection=model_selection,
        problem=loaded.problem,
        prompt_template_sha256=loaded.prompt_template_sha256,
        rendered_prompt_sha256=loaded.rendered_prompt_sha256,
        decoding=decoding,
    )
    output_root = repo_root / config.outputs.root / invocation.invocation_id.rsplit(":", 1)[-1]
    _persist_immutable(
        output_root / "catalog_observation.json",
        _canonical_record_bytes(catalog),
    )
    _persist_immutable(
        output_root / "reference_blind_audit.json",
        _canonical_record_bytes(loaded.reference_blind_audit),
    )
    _persist_immutable(
        output_root / "invocation.json",
        _canonical_record_bytes(invocation),
    )
    wire_payload = _wire_payload(
        config=config,
        model_id=model.model_id,
        rendered_prompt=loaded.rendered_prompt,
    )
    attempt_records: list[RCPAttemptRecord] = []
    parsed: ParsedLeanDeclaration | None = None
    output_text: str | None = None
    parse_error_code: str | None = None
    for attempt_index in range(config.retry.max_attempts):
        request = create_provider_request_for_problem(
            identity=ProviderIdentity(
                provider="epfl_rcp",
                model=model.model_id,
                revision=f"catalog-sha256:{catalog.raw_response_sha256}",
                # The existing provider-v1 identity has no enabled external
                # transport variant.  ``external_disabled`` is used only to
                # invoke its fail-closed external privacy check; the actual
                # transport is recorded truthfully in all RCP-v1 records.
                transport="external_disabled",
            ),
            problem=loaded.problem,
            prompt_template_hash=loaded.prompt_template_sha256,
            rendered_prompt=loaded.rendered_prompt,
            decoding=decoding,
            attempt_index=attempt_index,
        )
        attempt_dir = output_root / "attempts" / f"{attempt_index:04d}"
        request_path = attempt_dir / "provider_request.json"
        persist_provider_request(request, request_path)
        started_at = clock()
        try:
            response = transport.post_json(
                url=credentials.base_url + config.transport.chat_completions_path,
                api_key=credentials.api_key,
                payload=wire_payload,
                timeout_seconds=config.retry.request_timeout_seconds,
            )
            if response.status_code != 200:
                raise _classify_http_failure(
                    response=response,
                    config=config.retry,
                    api_key=credentials.api_key,
                )
            content, provider_request_id, returned_model, tokens = _extract_completion(
                response.body
            )
            completed_at = clock()
            wire_document = _json_document(response.body, label="RCP chat completion")
            wire_path = attempt_dir / "wire_response.json"
            _persist_immutable(
                wire_path,
                canonical_json_bytes(wire_document) + b"\n",
            )
            provider_result = persist_provider_raw_response(
                attempt_dir / "provider_raw",
                ProviderRawResponse.success(request, content),
            )
            attempt = _attempt_record(
                invocation=invocation,
                request=request,
                request_path=request_path,
                wire_path=wire_path,
                provider_response_path=provider_result.raw_response_path,
                status=RCPAttemptStatus.RESPONSE_RECEIVED,
                retryable=False,
                http_status=200,
                provider_request_id=provider_request_id,
                returned_model=returned_model,
                error_code=None,
                error_detail=None,
                tokens=tokens,
                started_at=started_at,
                completed_at=completed_at,
                repo_root=repo_root,
            )
            attempt_path = attempt_dir / "attempt_record.json"
            _persist_immutable(attempt_path, _canonical_record_bytes(attempt))
            attempt_records.append(attempt)
            output_text = content
            try:
                parsed = parse_direct_autoformalization_output(content)
                if parsed.declaration_name != config.prompt.declaration_name:
                    parsed = None
                    parse_error_code = "wrong_declaration_name"
            except DirectOutputParseError as exc:
                parse_error_code = exc.code.value
            break
        except RCPTransportError as exc:
            completed_at = clock()
            detail = redact_rcp_text(str(exc), api_key=credentials.api_key)
            wire_payload_error = {
                "schema_version": 1,
                "http_status": exc.http_status,
                "error_code": exc.code,
                "error_detail": detail,
                "response_body_sha256": (
                    sha256_hex(exc.response_body) if exc.response_body is not None else None
                ),
                "response_body_preview": (
                    redact_rcp_text(
                        exc.response_body.decode("utf-8", errors="replace")[:1000],
                        api_key=credentials.api_key,
                    )
                    if exc.response_body is not None
                    else None
                ),
                "api_key_serialized": False,
            }
            wire_path = attempt_dir / "wire_response_error.json"
            _persist_immutable(
                wire_path,
                canonical_json_bytes(wire_payload_error) + b"\n",
            )
            provider_result = persist_provider_raw_response(
                attempt_dir / "provider_raw",
                ProviderRawResponse.error(
                    request,
                    error_type=exc.code,
                    error_detail=detail,
                ),
            )
            if exc.code == "timeout":
                status = RCPAttemptStatus.TIMEOUT
            elif exc.http_status is not None and exc.retryable:
                status = RCPAttemptStatus.RETRYABLE_HTTP_ERROR
            elif exc.http_status is not None:
                status = RCPAttemptStatus.TERMINAL_HTTP_ERROR
            elif exc.code in {
                "invalid_json",
                "invalid_json_shape",
                "invalid_response_shape",
                "empty_response",
            }:
                status = RCPAttemptStatus.INVALID_RESPONSE
            else:
                status = RCPAttemptStatus.TRANSPORT_ERROR
            attempt = _attempt_record(
                invocation=invocation,
                request=request,
                request_path=request_path,
                wire_path=wire_path,
                provider_response_path=provider_result.raw_response_path,
                status=status,
                retryable=exc.retryable,
                http_status=exc.http_status,
                provider_request_id=None,
                returned_model=None,
                error_code=exc.code,
                error_detail=detail,
                tokens={},
                started_at=started_at,
                completed_at=completed_at,
                repo_root=repo_root,
            )
            attempt_path = attempt_dir / "attempt_record.json"
            _persist_immutable(attempt_path, _canonical_record_bytes(attempt))
            attempt_records.append(attempt)
            if not exc.retryable or attempt_index + 1 >= config.retry.max_attempts:
                break
            sleeper(float(config.retry.retry_delays_seconds[attempt_index]))

    if not attempt_records:
        raise RCPQualificationError("qualification produced no attempt record")
    if output_text is None:
        terminal_status = RCPTerminalStatus.EXHAUSTED
        output_hash = None
        parsed_hash = None
    elif parsed is None:
        terminal_status = RCPTerminalStatus.PARSE_FAILED
        output_hash = sha256_hex(output_text.encode("utf-8"))
        parsed_hash = None
        parse_error_code = parse_error_code or "unknown_parse_failure"
    else:
        terminal_status = RCPTerminalStatus.RAW_COLLECTED
        output_hash = sha256_hex(output_text.encode("utf-8"))
        parsed_hash = parsed.statement_sha256
    terminal = _terminal(
        invocation=invocation,
        attempt_records=tuple(attempt_records),
        status=terminal_status,
        output_sha256=output_hash,
        parsed_statement_sha256=parsed_hash,
        parse_error_code=parse_error_code,
    )
    terminal_path = output_root / "terminal.json"
    _persist_immutable(terminal_path, _canonical_record_bytes(terminal))
    _secret_absent(output_root, credentials.api_key)
    return RCPQualificationRun(
        output_directory=output_root,
        terminal_path=terminal_path,
        terminal=terminal,
        attempt_paths=tuple(
            output_root / "attempts" / f"{index:04d}" / "attempt_record.json"
            for index in range(len(attempt_records))
        ),
        parsed_declaration=parsed,
    )
