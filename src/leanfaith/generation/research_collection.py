"""Resumable non-smoke LF-021 collection over the frozen public research pool.

The module deliberately separates three concepts:

* fixture qualification is smoke evidence about one model/runtime path;
* a model-free preflight freezes the research invocation plan but performs no
  provider or model call;
* an actual research collection executes the frozen invocations and persists
  one immutable terminal record for every requested problem/family/seed.

Raw collection is not semantic evaluation.  This stage never creates a label,
never asserts faithfulness, and never closes Gate 5G or Gate 5.
"""

from __future__ import annotations

import datetime
import importlib.metadata
import json
import os
import re
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.generation.invocation_failure import redact_exception_message
from leanfaith.generation.local_hf import (
    LoadedLocalHFModel,
    LocalHFGeneratedText,
    LocalHFGenerationRequest,
    LocalHFGenerator,
    LocalHFLoader,
    LocalHFModelPin,
    LocalHFPromptFormatter,
    LocalHFProviderCompatibility,
    TransformersCausalGenerator,
    TransformersLocalLoader,
)
from leanfaith.generation.local_qualification import (
    LocalQualificationBundleManifest,
    LocalQualificationConfig,
    LocalQualificationTerminal,
    QualificationInputBinding,
    QualificationStatus,
    RuntimeEnvironmentBinding,
    build_local_qualification_formatter,
    load_local_qualification_config,
    render_local_qualification_prompt,
    verify_local_qualification_bundle,
)
from leanfaith.generation.providers import (
    DecodingValue,
    ProviderIdentity,
    ProviderRawResponse,
    bridge_provider_result_to_llm_lineage,
    create_provider_request_for_problem,
    persist_provider_raw_response,
    persist_provider_request,
)
from leanfaith.generation.public_research_pool import (
    LocalResearchSourceMatrix,
    PublicResearchPoolManifest,
)
from leanfaith.generation.research_overlap import ResearchFamilyOverlapRecord
from leanfaith.schemas.enums import ParseStatus
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_INVOCATION_ID = r"^research_collection_invocation:[0-9a-f]{64}$"
_PLAN_ID = r"^research_collection_plan:[0-9a-f]{64}$"
_TERMINAL_ID = r"^research_collection_terminal:[0-9a-f]{64}$"
_MANIFEST_ID = r"^research_collection_manifest:[0-9a-f]{64}$"
_BOUNDARY_ID = r"^research_collection_boundary:[0-9a-f]{64}$"
_FAMILY_SESSION_ID = r"^research_family_session:[0-9a-f]{64}$"
_DECLARATION = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
_RESEARCH_GPU_LIFECYCLE_LOCK = threading.Lock()


def _utc_json(value: datetime.datetime) -> str:
    require_utc(value)
    return value.isoformat().replace("+00:00", "Z")


class ResearchCollectionError(RuntimeError):
    """The frozen research collection contract is invalid or incomplete."""


class ResearchCollectionExecutionBlocked(ResearchCollectionError):
    """Scientific execution was requested before every activation prerequisite."""


class ResearchCollectionArtifactConflict(ResearchCollectionError):
    """A supposedly immutable collection artifact contains different bytes."""


class ResearchCollectionPostBoundaryError(ResearchCollectionError):
    """An error occurred after a provider/model-attempt boundary was persisted."""


def _relative_artifact(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"{field} must be a nonempty repository-relative path")
    return value


class ResearchArtifactBinding(StrictModel):
    """One immutable repository-owned input."""

    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _relative(self) -> Self:
        _relative_artifact(self.artifact, field="artifact")
        return self


class ResearchFamilyActivation(StrictModel):
    """Evidence required before a family can enter non-smoke collection."""

    status: Literal["blocked", "ready"]
    qualification_bundle_artifact: str | None = None
    qualification_bundle_sha256: str | None = Field(default=None, pattern=_HEX64)
    overlap_record_artifact: str | None = None
    overlap_record_sha256: str | None = Field(default=None, pattern=_HEX64)
    blocker: str | None = None

    @model_validator(mode="after")
    def _complete_or_blocked(self) -> Self:
        qualification_pair = (
            self.qualification_bundle_artifact,
            self.qualification_bundle_sha256,
        )
        overlap_pair = (
            self.overlap_record_artifact,
            self.overlap_record_sha256,
        )
        for label, pair in (
            ("qualification bundle", qualification_pair),
            ("overlap record", overlap_pair),
        ):
            if (pair[0] is None) != (pair[1] is None):
                raise ValueError(f"{label} artifact and hash must be bound together")
        if self.status == "ready":
            if any(value is None for value in (*qualification_pair, *overlap_pair)):
                raise ValueError("ready family activation requires qualification and overlap bytes")
            if self.blocker is not None:
                raise ValueError("ready family activation cannot retain a blocker")
            assert self.qualification_bundle_artifact is not None
            assert self.overlap_record_artifact is not None
            _relative_artifact(
                self.qualification_bundle_artifact,
                field="qualification_bundle_artifact",
            )
            _relative_artifact(self.overlap_record_artifact, field="overlap_record_artifact")
        else:
            if self.blocker is None or not self.blocker.strip():
                raise ValueError("blocked family activation requires a blocker")
            if self.qualification_bundle_artifact is not None:
                _relative_artifact(
                    self.qualification_bundle_artifact,
                    field="qualification_bundle_artifact",
                )
            if self.overlap_record_artifact is not None:
                _relative_artifact(
                    self.overlap_record_artifact,
                    field="overlap_record_artifact",
                )
        return self


class ResearchCollectionFamily(StrictModel):
    family_id: str = Field(min_length=1)
    provider_slot: str = Field(min_length=1)
    qualification_pin_source: ResearchArtifactBinding
    seeds: tuple[int, ...] = Field(min_length=1)
    activation: ResearchFamilyActivation

    @model_validator(mode="after")
    def _seeds(self) -> Self:
        if list(self.seeds) != sorted(set(self.seeds)) or any(seed < 0 for seed in self.seeds):
            raise ValueError("family seeds must be sorted, unique, and nonnegative")
        return self


class ResearchRetryConfig(StrictModel):
    max_attempts: Literal[1] = 1
    retryable_statuses: tuple[str, ...] = ()
    append_only_attempt_artifacts: Literal[True] = True

    @model_validator(mode="after")
    def _minimal_v1(self) -> Self:
        if self.retryable_statuses:
            raise ValueError("minimal research collection v1 is single-attempt")
        return self


class ResearchCollectionOutputs(StrictModel):
    root: str
    preflight_report: str

    @model_validator(mode="after")
    def _relative(self) -> Self:
        _relative_artifact(self.root, field="outputs.root")
        _relative_artifact(self.preflight_report, field="outputs.preflight_report")
        return self


class ResearchRuntimeBinding(StrictModel):
    """Exact single-device runtime authorized for the non-smoke collection."""

    device: Literal["cuda:0"] = "cuda:0"
    dtype: Literal["bfloat16"] = "bfloat16"
    loader_policy: Literal["local_files_only_single_device_v1"] = (
        "local_files_only_single_device_v1"
    )
    local_files_only: Literal[True] = True
    allow_remote_code: Literal[False] = False
    python_version: str = Field(min_length=1)
    torch_version: str = Field(min_length=1)
    transformers_version: str = Field(min_length=1)
    driver_version: str = Field(min_length=1)
    device_name: str = Field(min_length=1)
    runtime_adapter: ResearchArtifactBinding
    environment_lock: ResearchArtifactBinding
    orchestration_adapter: ResearchArtifactBinding

    @property
    def runtime_hash(self) -> str:
        return hash_canonical(
            {
                "schema": "lf021_research_runtime_binding_v1",
                **self.model_dump(mode="json"),
            }
        )

    def matches_qualification(self, binding: RuntimeEnvironmentBinding) -> bool:
        return (
            binding.environment_lock_artifact == self.environment_lock.artifact
            and binding.environment_lock_sha256 == self.environment_lock.sha256
            and binding.python_version == self.python_version
            and binding.torch_version == self.torch_version
            and binding.transformers_version == self.transformers_version
            and binding.driver_version == self.driver_version
            and binding.device_name == self.device_name
            and binding.dtype == self.dtype
            and binding.runtime_adapter_artifact == self.runtime_adapter.artifact
            and binding.runtime_adapter_sha256 == self.runtime_adapter.sha256
        )


class ResearchCollectionConfig(StrictModel):
    """The separate non-smoke research execution authorization."""

    schema_version: Literal[1] = 1
    config_id: Literal["lf021_local_research_collection_v1"]
    frozen_at: datetime.datetime
    artifact_class: Literal["research"] = "research"
    collection_scope: Literal["public_three_problem_three_family_minimal_v1"]
    status: Literal["blocked_pending_family_activation", "ready"]
    execution_enabled: bool
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    problem_pool_records: ResearchArtifactBinding
    problem_pool_manifest: ResearchArtifactBinding
    context: ResearchArtifactBinding
    import_header: ResearchArtifactBinding
    source_matrix: ResearchArtifactBinding
    runtime: ResearchRuntimeBinding
    families: tuple[ResearchCollectionFamily, ...]
    retry: ResearchRetryConfig
    outputs: ResearchCollectionOutputs
    rules: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.frozen_at)
        if len(self.families) != 3:
            raise ValueError("minimal public collection requires exactly three families")
        family_ids = [family.family_id for family in self.families]
        slots = [family.provider_slot for family in self.families]
        if family_ids != sorted(set(family_ids)):
            raise ValueError("collection families must be sorted and unique")
        if len(slots) != len(set(slots)):
            raise ValueError("collection provider slots must be unique")
        ready = all(family.activation.status == "ready" for family in self.families)
        if self.status == "ready":
            if not self.execution_enabled or not ready:
                raise ValueError("ready collection requires execution and all family activations")
        elif self.execution_enabled or ready:
            raise ValueError("blocked collection cannot enable execution or all families")
        return self


class ResearchFamilyBinding(StrictModel):
    """Exact model/checkpoint/prompt/parser pin used by a plan."""

    family_id: str
    provider_slot: str
    qualification_config_id: str
    qualification_config_artifact: str
    qualification_config_file_sha256: str = Field(pattern=_HEX64)
    qualification_config_hash: str = Field(pattern=_HEX64)
    model_repo_id: str
    model_revision: str = Field(pattern=_HEX40)
    tokenizer_revision: str = Field(pattern=_HEX40)
    architecture: str
    checkpoint_bytes: int = Field(ge=1)
    checkpoint_manifest_hash: str = Field(pattern=_HEX64)
    prompt_template_artifact: str
    prompt_template_sha256: str = Field(pattern=_HEX64)
    common_suffix_artifact: str
    common_suffix_sha256: str = Field(pattern=_HEX64)
    prompt_formatter_id: str
    prompt_formatter_hash: str = Field(pattern=_HEX64)
    chat_template_sha256: str | None = Field(default=None, pattern=_HEX64)
    parser_id: str
    parser_source_artifact: str
    parser_source_sha256: str = Field(pattern=_HEX64)
    decoding: dict[str, object]
    decoding_hash: str = Field(pattern=_HEX64)
    runtime_hash: str = Field(pattern=_HEX64)
    runtime_device: Literal["cuda:0"]
    runtime_dtype: Literal["bfloat16"]
    activation_status: Literal["blocked", "ready"]
    qualification_bundle_sha256: str | None = Field(default=None, pattern=_HEX64)
    overlap_record_sha256: str | None = Field(default=None, pattern=_HEX64)

    @property
    def binding_hash(self) -> str:
        return hash_canonical(
            {
                "schema": "lf021_research_family_binding_v1",
                **self.model_dump(mode="json"),
            }
        )


class ResearchCollectionInvocation(StrictModel):
    """One frozen public problem x local family x seed request."""

    schema_version: Literal[1] = 1
    record_kind: Literal["lf021_research_collection_invocation"] = (
        "lf021_research_collection_invocation"
    )
    artifact_class: Literal["research"] = "research"
    execution_purpose: Literal["research_collection"] = "research_collection"
    invocation_id: str = Field(pattern=_INVOCATION_ID)
    collection_config_hash: str = Field(pattern=_HEX64)
    family_binding_hash: str = Field(pattern=_HEX64)
    family_id: str
    provider_slot: str
    model_repo_id: str
    model_revision: str = Field(pattern=_HEX40)
    tokenizer_revision: str = Field(pattern=_HEX40)
    problem_record_id: str
    problem_content_hash: str = Field(pattern=_HEX64)
    problem_id: str
    problem_group: str
    seed: int = Field(ge=0)
    expected_declaration_name: str
    context_id: str
    context_sha256: str = Field(pattern=_HEX64)
    import_header_artifact: str
    import_header_sha256: str = Field(pattern=_HEX64)
    rendered_user_prompt: str
    rendered_user_prompt_sha256: str = Field(pattern=_HEX64)
    prompt_template_bundle_hash: str = Field(pattern=_HEX64)
    prompt_formatter_id: str
    prompt_formatter_hash: str = Field(pattern=_HEX64)
    parser_id: str
    parser_source_sha256: str = Field(pattern=_HEX64)
    decoding: dict[str, object]
    decoding_hash: str = Field(pattern=_HEX64)
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "invocation_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if not _DECLARATION.fullmatch(self.expected_declaration_name):
            raise ValueError("expected_declaration_name is not a simple Lean identifier")
        if sha256_hex(self.rendered_user_prompt.encode("utf-8")) != (
            self.rendered_user_prompt_sha256
        ):
            raise ValueError("rendered prompt hash differs from prompt bytes")
        expected = "research_collection_invocation:" + hash_canonical(
            {"schema": "lf021_research_collection_invocation_v1", **self.id_payload()}
        )
        if self.invocation_id != expected:
            raise ValueError("invocation_id does not match frozen invocation payload")
        return self


class ResearchCollectionPlan(StrictModel):
    schema_version: Literal[1] = 1
    plan_id: str = Field(pattern=_PLAN_ID)
    collection_config_artifact: str
    collection_config_file_sha256: str = Field(pattern=_HEX64)
    collection_config_hash: str = Field(pattern=_HEX64)
    problem_pool_records_sha256: str = Field(pattern=_HEX64)
    problem_pool_manifest_sha256: str = Field(pattern=_HEX64)
    context_sha256: str = Field(pattern=_HEX64)
    import_header_sha256: str = Field(pattern=_HEX64)
    source_matrix_sha256: str = Field(pattern=_HEX64)
    runtime_adapter_sha256: str = Field(pattern=_HEX64)
    environment_lock_sha256: str = Field(pattern=_HEX64)
    orchestration_adapter_sha256: str = Field(pattern=_HEX64)
    runtime_hash: str = Field(pattern=_HEX64)
    family_bindings: tuple[ResearchFamilyBinding, ...]
    invocations: tuple[ResearchCollectionInvocation, ...]
    actual_collection_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key != "plan_id"
        }

    @property
    def plan_hash(self) -> str:
        return hash_canonical(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def _identity_and_counts(self) -> Self:
        if len(self.family_bindings) != 3 or len(self.invocations) != 9:
            raise ValueError("minimal research plan requires three families and nine invocations")
        ids = [item.invocation_id for item in self.invocations]
        if ids != sorted(set(ids)):
            raise ValueError("plan invocations must be sorted and unique")
        expected = "research_collection_plan:" + hash_canonical(
            {"schema": "lf021_research_collection_plan_v1", **self.id_payload()}
        )
        if self.plan_id != expected:
            raise ValueError("plan_id does not match frozen plan payload")
        return self


class ResearchCollectionPreflightReport(StrictModel):
    """Deterministic model-free report; no terminal candidate is created."""

    schema_version: Literal[1] = 1
    report_kind: Literal["lf021_local_research_collection_preflight"]
    passed: Literal[True] = True
    execution_ready: bool
    actual_collection_performed: Literal[False] = False
    gpu_model_execution_performed: Literal[False] = False
    provider_requests_created: Literal[0] = 0
    terminal_candidates_created: Literal[0] = 0
    semantic_labels_created: Literal[False] = False
    counts_as_smoke_qualification: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    plan_id: str = Field(pattern=_PLAN_ID)
    plan_hash: str = Field(pattern=_HEX64)
    problem_count: Literal[3] = 3
    family_count: Literal[3] = 3
    planned_candidate_count: Literal[9] = 9
    family_binding_hashes: dict[str, str]
    invocation_ids: tuple[str, ...]
    checks: dict[str, bool]
    blocking_prerequisites: tuple[str, ...]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if not self.checks or not all(self.checks.values()):
            raise ValueError("research collection preflight requires every model-free check")
        if self.execution_ready == bool(self.blocking_prerequisites):
            raise ValueError("execution readiness and blocking prerequisites disagree")
        if len(self.invocation_ids) != self.planned_candidate_count:
            raise ValueError("preflight invocation count does not reconcile")
        return self


class ResearchTerminalStatus(StrEnum):
    RAW_COLLECTED = "raw_collected"
    RUNTIME_FAILED = "runtime_failed"
    ORCHESTRATION_FAILED = "orchestration_failed"


class ResearchInvocationBoundary(StrictModel):
    """Immutable evidence that a provider request was durably created."""

    schema_version: Literal[1] = 1
    boundary_id: str = Field(pattern=_BOUNDARY_ID)
    invocation_id: str = Field(pattern=_INVOCATION_ID)
    provider_request_hash: str = Field(pattern=_HEX64)
    local_runtime_request_hash: str = Field(pattern=_HEX64)
    crossed_at: datetime.datetime

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "boundary_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.crossed_at)
        expected = "research_collection_boundary:" + hash_canonical(
            {"schema": "lf021_research_collection_boundary_v1", **self.id_payload()}
        )
        if self.boundary_id != expected:
            raise ValueError("boundary_id does not match immutable boundary payload")
        return self

    @classmethod
    def create(
        cls,
        *,
        invocation_id: str,
        provider_request_hash: str,
        local_runtime_request_hash: str,
        crossed_at: datetime.datetime,
    ) -> Self:
        payload = {
            "schema_version": 1,
            "invocation_id": invocation_id,
            "provider_request_hash": provider_request_hash,
            "local_runtime_request_hash": local_runtime_request_hash,
            "crossed_at": _utc_json(crossed_at),
        }
        boundary_id = "research_collection_boundary:" + hash_canonical(
            {"schema": "lf021_research_collection_boundary_v1", **payload}
        )
        return cls.model_validate({"boundary_id": boundary_id, **payload})


class ResearchModelAttemptBoundary(StrictModel):
    """Durable no-retry marker written immediately before model generation."""

    schema_version: Literal[1] = 1
    invocation_id: str = Field(pattern=_INVOCATION_ID)
    family_session_id: str = Field(pattern=_FAMILY_SESSION_ID)
    local_runtime_request_hash: str = Field(pattern=_HEX64)
    started_at: datetime.datetime

    @model_validator(mode="after")
    def _time(self) -> Self:
        require_utc(self.started_at)
        return self


class ResearchRuntimeFailure(StrictModel):
    schema_version: Literal[1] = 1
    stage: Literal[
        "provider_boundary",
        "model_generation",
        "incomplete_prior_model_attempt",
        "provider_lineage",
    ]
    model_invocation_attempted: bool
    exception_type: str
    exception_message: str
    failed_at: datetime.datetime

    @model_validator(mode="after")
    def _time(self) -> Self:
        require_utc(self.failed_at)
        return self


class ResearchLocalGenerationResult(StrictModel):
    """Per-candidate raw output produced inside a shared family model session."""

    schema_version: Literal[1] = 1
    family_session_id: str = Field(pattern=_FAMILY_SESSION_ID)
    request_hash: str = Field(pattern=_HEX64)
    formatted_prompt_hash: str = Field(pattern=_HEX64)
    raw_text: str
    output_hash: str = Field(pattern=_HEX64)
    prompt_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    generation_latency_ms: int = Field(ge=0)
    started_at: datetime.datetime
    completed_at: datetime.datetime
    decoding: dict[str, object]
    decoding_hash: str = Field(pattern=_HEX64)
    compatibility: LocalHFProviderCompatibility

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.started_at)
        require_utc(self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("research generation completion cannot precede start")
        if self.output_hash != sha256_hex(self.raw_text.encode("utf-8")):
            raise ValueError("research output hash differs from raw text")
        if self.total_tokens != self.prompt_tokens + self.output_tokens:
            raise ValueError("research total token count does not reconcile")
        if self.decoding_hash != hash_canonical(self.decoding):
            raise ValueError("research decoding hash differs from decoding")
        if (
            self.compatibility.output_hash != self.output_hash
            or self.compatibility.formatted_prompt_hash != self.formatted_prompt_hash
            or self.compatibility.decoding_hash != self.decoding_hash
        ):
            raise ValueError("research provider compatibility record differs from output")
        return self


class ResearchFamilySessionStart(StrictModel):
    """Proof that one family model was loaded once for its pending invocations."""

    schema_version: Literal[1] = 1
    family_session_id: str = Field(pattern=_FAMILY_SESSION_ID)
    family_id: str
    model_repo_id: str
    model_revision: str = Field(pattern=_HEX40)
    runtime_hash: str = Field(pattern=_HEX64)
    session_attempt_index: int = Field(ge=0)
    planned_invocation_ids: tuple[str, ...] = Field(min_length=1)
    load_count: Literal[1] = 1
    started_at: datetime.datetime
    loaded_at: datetime.datetime
    load_latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.started_at)
        require_utc(self.loaded_at)
        if self.loaded_at < self.started_at:
            raise ValueError("family load completion cannot precede start")
        if tuple(sorted(set(self.planned_invocation_ids))) != self.planned_invocation_ids:
            raise ValueError("family session invocation IDs must be sorted and unique")
        expected = "research_family_session:" + hash_canonical(
            {
                "schema": "lf021_research_family_session_v1",
                "family_id": self.family_id,
                "model_repo_id": self.model_repo_id,
                "model_revision": self.model_revision,
                "runtime_hash": self.runtime_hash,
                "session_attempt_index": self.session_attempt_index,
                "planned_invocation_ids": self.planned_invocation_ids,
                "started_at": _utc_json(self.started_at),
            }
        )
        if self.family_session_id != expected:
            raise ValueError("family_session_id differs from exact family session inputs")
        return self


class ResearchFamilySessionEnd(StrictModel):
    schema_version: Literal[1] = 1
    family_session_id: str = Field(pattern=_FAMILY_SESSION_ID)
    family_id: str
    unload_count: Literal[1] = 1
    completed_invocation_ids: tuple[str, ...]
    started_at: datetime.datetime
    completed_at: datetime.datetime
    unload_latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.started_at)
        require_utc(self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("family unload completion cannot precede start")
        if tuple(sorted(set(self.completed_invocation_ids))) != self.completed_invocation_ids:
            raise ValueError("completed family invocation IDs must be sorted and unique")
        return self


class ResearchCollectionTerminal(StrictModel):
    """One immutable terminal outcome for an actual requested candidate."""

    schema_version: Literal[1] = 1
    record_kind: Literal["lf021_research_collection_terminal"] = (
        "lf021_research_collection_terminal"
    )
    artifact_class: Literal["research"] = "research"
    terminal_id: str = Field(pattern=_TERMINAL_ID)
    invocation_id: str = Field(pattern=_INVOCATION_ID)
    invocation_payload_hash: str = Field(pattern=_HEX64)
    status: ResearchTerminalStatus
    family_id: str
    problem_record_id: str
    seed: int = Field(ge=0)
    started_at: datetime.datetime
    completed_at: datetime.datetime
    model_invocation_attempted: bool
    resumed_from_persisted_runtime_result: bool
    family_session_id: str | None = Field(default=None, pattern=_FAMILY_SESSION_ID)
    provider_request_hash: str | None = Field(default=None, pattern=_HEX64)
    llm_call_id: str | None = None
    llm_attempt_id: str | None = None
    local_runtime_request_hash: str | None = Field(default=None, pattern=_HEX64)
    raw_output_sha256: str | None = Field(default=None, pattern=_HEX64)
    artifact_hashes: dict[str, str]
    parser_executed: Literal[False] = False
    lean_validation_executed: Literal[False] = False
    semantic_pool_admitted: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    counts_as_smoke_qualification: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    error_code: str | None = None
    error_detail: str | None = None

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "terminal_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.started_at)
        require_utc(self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("terminal completion cannot precede start")
        if list(self.artifact_hashes) != sorted(self.artifact_hashes):
            raise ValueError("terminal artifact_hashes must be sorted")
        if any(re.fullmatch(_HEX64, digest) is None for digest in self.artifact_hashes.values()):
            raise ValueError("terminal artifact hashes must be SHA-256")
        complete_values = (
            self.provider_request_hash,
            self.llm_call_id,
            self.llm_attempt_id,
            self.local_runtime_request_hash,
            self.raw_output_sha256,
        )
        if self.status is ResearchTerminalStatus.RAW_COLLECTED:
            if any(value is None for value in complete_values):
                raise ValueError("raw-collected terminal requires complete raw lineage")
            if self.error_code is not None or self.error_detail is not None:
                raise ValueError("raw-collected terminal cannot carry an error")
            required = {
                "provider_request",
                "provider_raw_response",
                "llm_call",
                "llm_attempt",
                "local_generation_result",
            }
            if not required.issubset(self.artifact_hashes):
                raise ValueError("raw-collected terminal lacks required artifacts")
            if self.family_session_id is None or "family_session_start" not in self.artifact_hashes:
                raise ValueError("raw-collected terminal lacks its family session binding")
        else:
            if self.error_code is None or self.error_detail is None:
                raise ValueError("failed collection terminal requires an error")
            if (
                self.status is ResearchTerminalStatus.RUNTIME_FAILED
                and self.model_invocation_attempted
                and (
                    self.family_session_id is None
                    or "family_session_start" not in self.artifact_hashes
                )
            ):
                raise ValueError("runtime failure lacks its family session binding")
            if self.status is ResearchTerminalStatus.ORCHESTRATION_FAILED and (
                self.model_invocation_attempted
                or self.family_session_id is not None
                or any(value is not None for value in complete_values)
                or self.artifact_hashes
            ):
                raise ValueError("orchestration failure cannot claim provider/runtime artifacts")
        expected = "research_collection_terminal:" + hash_canonical(
            {"schema": "lf021_research_collection_terminal_v1", **self.id_payload()}
        )
        if self.terminal_id != expected:
            raise ValueError("terminal_id does not match terminal payload")
        return self


class ResearchCollectionManifest(StrictModel):
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=_MANIFEST_ID)
    plan_id: str = Field(pattern=_PLAN_ID)
    plan_hash: str = Field(pattern=_HEX64)
    actual_collection_performed: Literal[True] = True
    expected_candidate_count: int = Field(ge=1)
    terminal_candidate_count: int = Field(ge=1)
    status_counts: dict[str, int]
    successful_family_count: int = Field(ge=0)
    terminal_artifact_hashes: dict[str, str]
    family_session_artifact_hashes: dict[str, str]
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "manifest_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.expected_candidate_count != self.terminal_candidate_count:
            raise ValueError("complete manifest requires one terminal per requested candidate")
        if sum(self.status_counts.values()) != self.terminal_candidate_count:
            raise ValueError("manifest status counts do not reconcile")
        if len(self.terminal_artifact_hashes) != self.terminal_candidate_count:
            raise ValueError("manifest terminal artifact hashes do not reconcile")
        if list(self.family_session_artifact_hashes) != sorted(
            self.family_session_artifact_hashes
        ) or any(
            re.fullmatch(_HEX64, digest) is None
            for digest in self.family_session_artifact_hashes.values()
        ):
            raise ValueError("manifest family session hashes must be sorted SHA-256 values")
        expected = "research_collection_manifest:" + hash_canonical(
            {"schema": "lf021_research_collection_manifest_v1", **self.id_payload()}
        )
        if self.manifest_id != expected:
            raise ValueError("manifest_id does not match manifest payload")
        return self


class ResearchFamilySessionFailure(StrictModel):
    """Append-only evidence that a family model could not be loaded."""

    schema_version: Literal[1] = 1
    family_id: str
    model_repo_id: str
    model_revision: str = Field(pattern=_HEX40)
    runtime_hash: str = Field(pattern=_HEX64)
    planned_invocation_ids: tuple[str, ...] = Field(min_length=1)
    exception_type: str
    exception_message: str
    failed_at: datetime.datetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.failed_at)
        if tuple(sorted(set(self.planned_invocation_ids))) != self.planned_invocation_ids:
            raise ValueError("failed family invocation IDs must be sorted and unique")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedResearchFamilyActivation:
    """Fully replayed evidence authorizing one family, or its partial blocked state."""

    qualification_manifest: LocalQualificationBundleManifest | None
    qualification_terminal: LocalQualificationTerminal | None
    qualification_inputs: QualificationInputBinding | None
    overlap_record: ResearchFamilyOverlapRecord | None


@dataclass(frozen=True, slots=True)
class LoadedResearchCollection:
    config: LoadedConfig[ResearchCollectionConfig]
    problems: tuple[ProblemPoolRecord, ...]
    context: ContextRecord
    source_matrix: LocalResearchSourceMatrix
    qualifications: Mapping[str, LoadedConfig[LocalQualificationConfig]]
    activation_evidence: Mapping[str, VerifiedResearchFamilyActivation]
    plan: ResearchCollectionPlan
    preflight: ResearchCollectionPreflightReport


def _resolve_binding(repo_root: Path, binding: ResearchArtifactBinding) -> Path:
    root = repo_root.resolve()
    path = (root / binding.artifact).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ResearchCollectionError(f"artifact escapes repository: {binding.artifact}") from exc
    if path.is_symlink() or not path.is_file():
        raise ResearchCollectionError(f"bound artifact is missing: {binding.artifact}")
    observed = hash_file(path)
    if observed != binding.sha256:
        raise ResearchCollectionError(
            f"bound artifact hash mismatch for {binding.artifact}: {binding.sha256} != {observed}"
        )
    return path


def _load_json_record[RecordT: StrictModel](
    path: Path,
    model: type[RecordT],
) -> RecordT:
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
        record = model.model_validate(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ResearchCollectionError(f"invalid bound JSON record {path}") from exc
    if raw != canonical_json_bytes(record.model_dump(mode="json")) + b"\n":
        raise ResearchCollectionError(f"bound JSON record is not canonical: {path}")
    return record


def _resolve_relative_hash(
    repo_root: Path,
    artifact: str,
    expected_sha256: str,
    *,
    label: str,
) -> Path:
    try:
        binding = ResearchArtifactBinding(artifact=artifact, sha256=expected_sha256)
        return _resolve_binding(repo_root, binding)
    except (ValueError, ResearchCollectionError) as exc:
        raise ResearchCollectionError(f"invalid {label}: {artifact}") from exc


def _load_bundle_problem(
    manifest: LocalQualificationBundleManifest,
    *,
    repo_root: Path,
) -> ProblemPoolRecord:
    """Recover the exact smoke-fixture problem bound by a qualification bundle."""

    try:
        artifact = manifest.artifacts["input_execution_input"]
        digest = manifest.artifact_sha256["input_execution_input"]
    except KeyError as exc:
        raise ResearchCollectionError(
            "qualification bundle lacks its archived execution input"
        ) from exc
    execution_path = _resolve_relative_hash(
        repo_root,
        artifact,
        digest,
        label="qualification execution input",
    )
    try:
        raw = execution_path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchCollectionError("invalid archived qualification execution input") from exc
    if raw != canonical_json_bytes(document) + b"\n":
        raise ResearchCollectionError("qualification execution input is not canonical")
    if document.get("schema") != "lf021_qualification_execution_input_v1":
        raise ResearchCollectionError("qualification execution input has the wrong schema")
    try:
        return ProblemPoolRecord.model_validate(document["problem"])
    except (KeyError, ValueError) as exc:
        raise ResearchCollectionError("qualification execution input has no valid problem") from exc


def _load_problem_records(path: Path) -> tuple[ProblemPoolRecord, ...]:
    records: list[ProblemPoolRecord] = []
    try:
        lines = path.read_bytes().splitlines()
        for line in lines:
            if not line:
                raise ValueError("blank JSONL row")
            records.append(ProblemPoolRecord.model_validate_json(line))
    except (OSError, ValueError) as exc:
        raise ResearchCollectionError(f"invalid problem-pool JSONL: {path}") from exc
    ordered = tuple(sorted(records, key=lambda item: item.problem_record_id))
    if tuple(records) != ordered or len(records) != 3:
        raise ResearchCollectionError("public research collection requires three sorted problems")
    if any(
        record.eligibility != "eligible"
        or record.private_source_content
        or not record.denylist_checked
        or record.denylist_hits
        for record in records
    ):
        raise ResearchCollectionError(
            "research collection problems must be public, eligible, and denylist-clear"
        )
    return tuple(records)


def _expected_declaration_name(family_id: str, problem: ProblemPoolRecord, seed: int) -> str:
    family = re.sub(r"[^A-Za-z0-9_]", "_", family_id)
    suffix = problem.problem_record_id.rsplit(":", 1)[-1][:12]
    return f"lf021_research_{family}_{suffix}_s{seed}"


def _family_binding(
    *,
    family: ResearchCollectionFamily,
    loaded: LoadedConfig[LocalQualificationConfig],
    config_file_sha256: str,
    runtime: ResearchRuntimeBinding,
) -> ResearchFamilyBinding:
    config = loaded.config
    formatter = build_local_qualification_formatter(config)
    model = config.active_model
    checkpoint = model.checkpoint_artifacts
    if checkpoint is None:
        raise ResearchCollectionError(f"{family.family_id} lacks exact checkpoint artifacts")
    decoding = config.decoding.model_dump(mode="json")
    return ResearchFamilyBinding(
        family_id=family.family_id,
        provider_slot=family.provider_slot,
        qualification_config_id=config.config_id,
        qualification_config_artifact=family.qualification_pin_source.artifact,
        qualification_config_file_sha256=config_file_sha256,
        qualification_config_hash=loaded.config_hash,
        model_repo_id=model.repo_id,
        model_revision=model.revision,
        tokenizer_revision=model.tokenizer_revision,
        architecture=model.architecture,
        checkpoint_bytes=model.checkpoint_bytes,
        checkpoint_manifest_hash=hash_canonical(checkpoint.model_dump(mode="json")),
        prompt_template_artifact=config.prompt.template_artifact,
        prompt_template_sha256=config.prompt.template_sha256,
        common_suffix_artifact=config.prompt.common_suffix_artifact,
        common_suffix_sha256=config.prompt.common_suffix_sha256,
        prompt_formatter_id=config.prompt.formatter_id,
        prompt_formatter_hash=formatter.formatter_hash,
        chat_template_sha256=config.prompt.chat_template_sha256,
        parser_id=config.prompt.parser_id,
        parser_source_artifact=config.prompt.parser_source_artifact,
        parser_source_sha256=config.prompt.parser_source_sha256,
        decoding=decoding,
        decoding_hash=hash_canonical(decoding),
        runtime_hash=runtime.runtime_hash,
        runtime_device=runtime.device,
        runtime_dtype=runtime.dtype,
        activation_status=family.activation.status,
        qualification_bundle_sha256=family.activation.qualification_bundle_sha256,
        overlap_record_sha256=family.activation.overlap_record_sha256,
    )


def _make_invocation(
    *,
    collection_config_hash: str,
    family_config: ResearchCollectionFamily,
    family: ResearchFamilyBinding,
    qualification: LocalQualificationConfig,
    problem: ProblemPoolRecord,
    seed: int,
    repo_root: Path,
    context: ContextRecord,
    context_sha256: str,
    header_text: str,
) -> ResearchCollectionInvocation:
    expected_name = _expected_declaration_name(family.family_id, problem, seed)
    rendered = render_local_qualification_prompt(
        config=qualification,
        repo_root=repo_root,
        problem=problem,
        expected_declaration_name=expected_name,
        registered_header=header_text,
    )
    decoding = dict(family.decoding)
    decoding["seed"] = seed
    payload: dict[str, object] = {
        "schema_version": 1,
        "record_kind": "lf021_research_collection_invocation",
        "artifact_class": "research",
        "execution_purpose": "research_collection",
        "collection_config_hash": collection_config_hash,
        "family_binding_hash": family.binding_hash,
        "family_id": family.family_id,
        "provider_slot": family_config.provider_slot,
        "model_repo_id": family.model_repo_id,
        "model_revision": family.model_revision,
        "tokenizer_revision": family.tokenizer_revision,
        "problem_record_id": problem.problem_record_id,
        "problem_content_hash": hash_canonical(problem.model_dump(mode="json")),
        "problem_id": problem.problem_id,
        "problem_group": problem.problem_group,
        "seed": seed,
        "expected_declaration_name": expected_name,
        "context_id": context.context_id,
        "context_sha256": context_sha256,
        "import_header_artifact": problem.import_header_artifact,
        "import_header_sha256": problem.import_header_hash,
        "rendered_user_prompt": rendered.user_prompt,
        "rendered_user_prompt_sha256": rendered.render_hash,
        "prompt_template_bundle_hash": rendered.template_bundle_hash,
        "prompt_formatter_id": family.prompt_formatter_id,
        "prompt_formatter_hash": family.prompt_formatter_hash,
        "parser_id": family.parser_id,
        "parser_source_sha256": family.parser_source_sha256,
        "decoding": decoding,
        "decoding_hash": hash_canonical(decoding),
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
    }
    invocation_id = "research_collection_invocation:" + hash_canonical(
        {"schema": "lf021_research_collection_invocation_v1", **payload}
    )
    return ResearchCollectionInvocation.model_validate({"invocation_id": invocation_id, **payload})


def _verify_family_activation(
    *,
    family: ResearchCollectionFamily,
    qualification: LoadedConfig[LocalQualificationConfig],
    config: ResearchCollectionConfig,
    pool_manifest: PublicResearchPoolManifest,
    problems: tuple[ProblemPoolRecord, ...],
    repo_root: Path,
) -> VerifiedResearchFamilyActivation:
    activation = family.activation
    qualification_manifest: LocalQualificationBundleManifest | None = None
    qualification_terminal: LocalQualificationTerminal | None = None
    qualification_inputs: QualificationInputBinding | None = None
    overlap_record: ResearchFamilyOverlapRecord | None = None

    if activation.qualification_bundle_artifact is not None:
        assert activation.qualification_bundle_sha256 is not None
        manifest_path = _resolve_relative_hash(
            repo_root,
            activation.qualification_bundle_artifact,
            activation.qualification_bundle_sha256,
            label="qualification bundle manifest",
        )
        qualification_manifest = _load_json_record(
            manifest_path,
            LocalQualificationBundleManifest,
        )
        fixture_problem = _load_bundle_problem(
            qualification_manifest,
            repo_root=repo_root,
        )
        try:
            terminal, _, _ = verify_local_qualification_bundle(
                qualification_manifest,
                artifact_root=repo_root,
                repo_root=repo_root,
                problem=fixture_problem,
            )
        except Exception as exc:
            raise ResearchCollectionError(
                f"qualification bundle replay failed for {family.family_id}"
            ) from exc
        qualification_terminal = terminal
        qualification_inputs_path = _resolve_relative_hash(
            repo_root,
            qualification_manifest.artifacts["qualification_inputs"],
            qualification_manifest.artifact_sha256["qualification_inputs"],
            label="qualification input binding",
        )
        qualification_inputs = _load_json_record(
            qualification_inputs_path,
            QualificationInputBinding,
        )
        active_model = qualification.config.active_model
        checkpoint = qualification_inputs.checkpoint_verification
        if (
            terminal.status is not QualificationStatus.QUALIFIED_SMOKE
            or terminal.artifact_class != "smoke"
            or terminal.qualifies_for_gate5g
            or terminal.semantic_labels_created
            or terminal.supervision_eligible
            or qualification_manifest.artifact_class != "smoke"
            or qualification_manifest.qualifies_for_gate5g
            or qualification_manifest.semantic_labels_created
            or qualification_manifest.supervision_eligible
            or qualification_manifest.training_eligible
            or qualification_manifest.release_eligible
            or qualification_manifest.calibration_eligible
            or qualification_manifest.model_selection_eligible
            or qualification_manifest.scientific_evaluation_eligible
        ):
            raise ResearchCollectionError(
                f"qualification bundle is not hard-false qualified-smoke evidence: "
                f"{family.family_id}"
            )
        if (
            terminal.model_family != family.family_id
            or terminal.model != active_model.repo_id
            or terminal.model_revision != active_model.revision
            or qualification_inputs.model_repo_id != active_model.repo_id
            or qualification_inputs.model_revision != active_model.revision
            or qualification_inputs.tokenizer_revision != active_model.tokenizer_revision
            or checkpoint is None
            or not checkpoint.matches_model(active_model)
            or fixture_problem.private_source_content
            or fixture_problem.eligibility != "eligible"
        ):
            raise ResearchCollectionError(
                f"qualification family/model/checkpoint/context differs from collection: "
                f"{family.family_id}"
            )
        prompt = qualification.config.prompt
        formatter = build_local_qualification_formatter(qualification.config)
        formatter_binding_matches = (
            qualification_inputs.prompt_formatter_hash == formatter.formatter_hash
            or (
                qualification_inputs.prompt_formatter_hash is None
                and not formatter.requires_hash_binding
            )
        )
        if (
            qualification_inputs.prompt_template_artifact != prompt.template_artifact
            or qualification_inputs.prompt_template_sha256 != prompt.template_sha256
            or qualification_inputs.common_suffix_artifact != prompt.common_suffix_artifact
            or qualification_inputs.common_suffix_sha256 != prompt.common_suffix_sha256
            or not formatter_binding_matches
            or not config.runtime.matches_qualification(qualification_inputs.runtime)
        ):
            raise ResearchCollectionError(
                f"qualification prompt/runtime differs from collection: {family.family_id}"
            )

    if activation.overlap_record_artifact is not None:
        assert activation.overlap_record_sha256 is not None
        overlap_path = _resolve_relative_hash(
            repo_root,
            activation.overlap_record_artifact,
            activation.overlap_record_sha256,
            label="family overlap record",
        )
        overlap_record = _load_json_record(overlap_path, ResearchFamilyOverlapRecord)
        active_model = qualification.config.active_model
        manifest_inputs = pool_manifest.input_hashes
        expected_problem_ids = tuple(problem.problem_record_id for problem in problems)
        if (
            overlap_record.family_id != family.family_id
            or overlap_record.model_repo_id != active_model.repo_id
            or overlap_record.model_revision != active_model.revision
            or overlap_record.problem_pool_records_sha256 != config.problem_pool_records.sha256
            or overlap_record.problem_pool_manifest_sha256 != config.problem_pool_manifest.sha256
            or overlap_record.active_benchmark_manifest_sha256
            != manifest_inputs.get("active_benchmark_manifest")
            or overlap_record.active_benchmark_registry_sha256
            != manifest_inputs.get("active_benchmark_registry")
            or overlap_record.public_source_manifest_sha256
            != manifest_inputs.get("source_manifest")
            or tuple(
                introduction.problem_record_id
                for introduction in overlap_record.source_introductions
            )
            != expected_problem_ids
            or overlap_record.pinned_readme_sha256 != active_model.metadata_hashes.readme
        ):
            raise ResearchCollectionError(
                f"overlap evidence differs from the exact family/pool bindings: {family.family_id}"
            )

    if activation.status == "ready" and (
        qualification_manifest is None
        or qualification_terminal is None
        or qualification_inputs is None
        or overlap_record is None
    ):
        raise ResearchCollectionError(
            f"ready family lacks replayed activation evidence: {family.family_id}"
        )
    return VerifiedResearchFamilyActivation(
        qualification_manifest=qualification_manifest,
        qualification_terminal=qualification_terminal,
        qualification_inputs=qualification_inputs,
        overlap_record=overlap_record,
    )


def load_research_collection(
    config_path: Path,
    *,
    repo_root: Path,
) -> LoadedResearchCollection:
    """Load, cross-check, and plan without importing GPU dependencies."""

    root = repo_root.resolve()
    loaded = load_config(config_path, ResearchCollectionConfig)
    config = loaded.config
    config_file_sha256 = hash_file(config_path)
    bindings = (
        config.problem_pool_records,
        config.problem_pool_manifest,
        config.context,
        config.import_header,
        config.source_matrix,
        config.runtime.runtime_adapter,
        config.runtime.environment_lock,
        config.runtime.orchestration_adapter,
    )
    resolved = {binding.artifact: _resolve_binding(root, binding) for binding in bindings}

    problems = _load_problem_records(resolved[config.problem_pool_records.artifact])
    manifest_record = _load_json_record(
        resolved[config.problem_pool_manifest.artifact],
        PublicResearchPoolManifest,
    )
    assert isinstance(manifest_record, PublicResearchPoolManifest)
    if (
        manifest_record.profile != "three_record_slice_v1"
        or tuple(manifest_record.record_ids)
        != tuple(problem.problem_record_id for problem in problems)
        or manifest_record.output_hashes.get("problem_pool_records")
        != config.problem_pool_records.sha256
    ):
        raise ResearchCollectionError("problem records differ from their frozen pool manifest")

    context_record = _load_json_record(resolved[config.context.artifact], ContextRecord)
    assert isinstance(context_record, ContextRecord)
    header_path = resolved[config.import_header.artifact]
    header_text = header_path.read_text(encoding="utf-8")
    if (
        context_record.header_hash != config.import_header.sha256
        or context_record.header_text != header_text
        or any(
            problem.context_id != context_record.context_id
            or problem.import_header_artifact != config.import_header.artifact
            or problem.import_header_hash != config.import_header.sha256
            for problem in problems
        )
    ):
        raise ResearchCollectionError("problem, context, and import-header bindings disagree")

    matrix_document = load_yaml_mapping(resolved[config.source_matrix.artifact])
    matrix = LocalResearchSourceMatrix.model_validate(matrix_document)
    matrix_by_family = {family.family_id: family for family in matrix.families}

    qualifications: dict[str, LoadedConfig[LocalQualificationConfig]] = {}
    activation_evidence: dict[str, VerifiedResearchFamilyActivation] = {}
    family_bindings: list[ResearchFamilyBinding] = []
    for family_config in config.families:
        pin_path = _resolve_binding(root, family_config.qualification_pin_source)
        qualification = load_local_qualification_config(pin_path, repo_root=root)
        matrix_family = matrix_by_family.get(family_config.family_id)
        if matrix_family is None:
            raise ResearchCollectionError(
                f"family absent from source matrix: {family_config.family_id}"
            )
        model = qualification.config.active_model
        if (
            model.family_id != family_config.family_id
            or model.repo_id != matrix_family.model
            or model.revision != matrix_family.revision
        ):
            raise ResearchCollectionError(
                f"family pin differs between collection, qualification, and matrix: "
                f"{family_config.family_id}"
            )
        qualifications[family_config.family_id] = qualification
        family_bindings.append(
            _family_binding(
                family=family_config,
                loaded=qualification,
                config_file_sha256=hash_file(pin_path),
                runtime=config.runtime,
            )
        )

    for family in config.families:
        activation_evidence[family.family_id] = _verify_family_activation(
            family=family,
            qualification=qualifications[family.family_id],
            config=config,
            pool_manifest=manifest_record,
            problems=problems,
            repo_root=root,
        )

    family_bindings = sorted(family_bindings, key=lambda item: item.family_id)
    config_by_family = {family.family_id: family for family in config.families}
    binding_by_family = {family.family_id: family for family in family_bindings}
    invocations = sorted(
        (
            _make_invocation(
                collection_config_hash=loaded.config_hash,
                family_config=config_by_family[family_id],
                family=binding_by_family[family_id],
                qualification=qualifications[family_id].config,
                problem=problem,
                seed=seed,
                repo_root=root,
                context=context_record,
                context_sha256=config.context.sha256,
                header_text=header_text,
            )
            for family_id in sorted(config_by_family)
            for seed in config_by_family[family_id].seeds
            for problem in problems
        ),
        key=lambda item: item.invocation_id,
    )
    plan_payload: dict[str, object] = {
        "schema_version": 1,
        "collection_config_artifact": str(config_path.resolve().relative_to(root)),
        "collection_config_file_sha256": config_file_sha256,
        "collection_config_hash": loaded.config_hash,
        "problem_pool_records_sha256": config.problem_pool_records.sha256,
        "problem_pool_manifest_sha256": config.problem_pool_manifest.sha256,
        "context_sha256": config.context.sha256,
        "import_header_sha256": config.import_header.sha256,
        "source_matrix_sha256": config.source_matrix.sha256,
        "runtime_adapter_sha256": config.runtime.runtime_adapter.sha256,
        "environment_lock_sha256": config.runtime.environment_lock.sha256,
        "orchestration_adapter_sha256": config.runtime.orchestration_adapter.sha256,
        "runtime_hash": config.runtime.runtime_hash,
        "family_bindings": [item.model_dump(mode="json") for item in family_bindings],
        "invocations": [item.model_dump(mode="json") for item in invocations],
        "actual_collection_performed": False,
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    plan_id = "research_collection_plan:" + hash_canonical(
        {"schema": "lf021_research_collection_plan_v1", **plan_payload}
    )
    plan = ResearchCollectionPlan.model_validate({"plan_id": plan_id, **plan_payload})
    blockers = tuple(
        f"{family.family_id}: {family.activation.blocker}"
        for family in config.families
        if family.activation.status == "blocked"
    )
    execution_ready = config.status == "ready" and config.execution_enabled and not blockers
    preflight = ResearchCollectionPreflightReport(
        report_kind="lf021_local_research_collection_preflight",
        execution_ready=execution_ready,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        family_binding_hashes={family.family_id: family.binding_hash for family in family_bindings},
        invocation_ids=tuple(item.invocation_id for item in invocations),
        checks={
            "collection_is_non_smoke": True,
            "context_and_header_bound": True,
            "exact_checkpoint_manifests_bound": True,
            "exact_prompt_formatter_and_parser_bound": True,
            "runtime_device_dtype_and_versions_bound": True,
            "families_match_non_authoritative_source_matrix_identity": True,
            "nine_unique_invocations_planned": len(invocations) == 9,
            "problem_pool_is_public_and_denylist_clear": True,
            "qualification_configs_are_pin_sources_only": True,
            "bound_activation_artifacts_fully_validated": True,
        },
        blocking_prerequisites=blockers,
    )
    return LoadedResearchCollection(
        config=loaded,
        problems=problems,
        context=context_record,
        source_matrix=matrix,
        qualifications=qualifications,
        activation_evidence=activation_evidence,
        plan=plan,
        preflight=preflight,
    )


def _canonical_record_bytes(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _write_immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ResearchCollectionArtifactConflict(f"immutable artifact conflict: {path}")
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
                raise ResearchCollectionArtifactConflict(
                    f"concurrent immutable artifact conflict: {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_preflight_report(
    loaded: LoadedResearchCollection,
    *,
    repo_root: Path,
) -> tuple[Path, str]:
    path = repo_root / loaded.config.config.outputs.preflight_report
    digest = _write_immutable(path, _canonical_record_bytes(loaded.preflight))
    return path, digest


def _load_canonical[RecordT: StrictModel](
    path: Path,
    model: type[RecordT],
) -> RecordT:
    return _load_json_record(path, model)


def _terminal_payload(
    *,
    invocation: ResearchCollectionInvocation,
    status: ResearchTerminalStatus,
    started_at: datetime.datetime,
    completed_at: datetime.datetime,
    model_invocation_attempted: bool,
    resumed: bool,
    family_session_id: str | None,
    provider_request_hash: str | None,
    llm_call_id: str | None,
    llm_attempt_id: str | None,
    local_runtime_request_hash: str | None,
    raw_output_sha256: str | None,
    artifact_hashes: Mapping[str, str],
    error_code: str | None,
    error_detail: str | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_kind": "lf021_research_collection_terminal",
        "artifact_class": "research",
        "invocation_id": invocation.invocation_id,
        "invocation_payload_hash": hash_canonical(invocation.model_dump(mode="json")),
        "status": status.value,
        "family_id": invocation.family_id,
        "problem_record_id": invocation.problem_record_id,
        "seed": invocation.seed,
        "started_at": _utc_json(started_at),
        "completed_at": _utc_json(completed_at),
        "model_invocation_attempted": model_invocation_attempted,
        "resumed_from_persisted_runtime_result": resumed,
        "family_session_id": family_session_id,
        "provider_request_hash": provider_request_hash,
        "llm_call_id": llm_call_id,
        "llm_attempt_id": llm_attempt_id,
        "local_runtime_request_hash": local_runtime_request_hash,
        "raw_output_sha256": raw_output_sha256,
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
        "parser_executed": False,
        "lean_validation_executed": False,
        "semantic_pool_admitted": False,
        "semantic_labels_created": False,
        "counts_as_smoke_qualification": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
        "error_code": error_code,
        "error_detail": error_detail,
    }


def make_orchestration_failure_terminal(
    invocation: ResearchCollectionInvocation,
    *,
    exception: Exception,
    at: datetime.datetime,
) -> ResearchCollectionTerminal:
    payload = _terminal_payload(
        invocation=invocation,
        status=ResearchTerminalStatus.ORCHESTRATION_FAILED,
        started_at=at,
        completed_at=at,
        model_invocation_attempted=False,
        resumed=False,
        family_session_id=None,
        provider_request_hash=None,
        llm_call_id=None,
        llm_attempt_id=None,
        local_runtime_request_hash=None,
        raw_output_sha256=None,
        artifact_hashes={},
        error_code=type(exception).__name__,
        error_detail=redact_exception_message(str(exception)) or "(no message)",
    )
    terminal_id = "research_collection_terminal:" + hash_canonical(
        {"schema": "lf021_research_collection_terminal_v1", **payload}
    )
    return ResearchCollectionTerminal.model_validate({"terminal_id": terminal_id, **payload})


class ResearchInvocationExecutor(Protocol):
    def begin_family(
        self,
        *,
        family: ResearchFamilyBinding,
        qualification: LoadedConfig[LocalQualificationConfig],
        runtime: ResearchRuntimeBinding,
        invocations: tuple[ResearchCollectionInvocation, ...],
        family_directory: Path,
    ) -> None: ...

    def execute(
        self,
        *,
        invocation: ResearchCollectionInvocation,
        problem: ProblemPoolRecord,
        qualification: LoadedConfig[LocalQualificationConfig],
        invocation_directory: Path,
        artifact_root: Path,
    ) -> ResearchCollectionTerminal: ...

    def end_family(
        self,
        *,
        family: ResearchFamilyBinding,
        completed_invocation_ids: tuple[str, ...],
        family_directory: Path,
    ) -> None: ...


Clock = Callable[[], datetime.datetime]
MonotonicClock = Callable[[], float]


def _elapsed_ms(start: float, end: float) -> int:
    return max(0, round((end - start) * 1000))


@dataclass(slots=True)
class _LoadedResearchFamily:
    family: ResearchFamilyBinding
    runtime: ResearchRuntimeBinding
    pin: LocalHFModelPin
    formatter: LocalHFPromptFormatter
    loaded: LoadedLocalHFModel
    session_id: str
    start: ResearchFamilySessionStart
    start_path: Path
    start_sha256: str
    lock_held: bool = True


@dataclass(slots=True)
class LocalHFResearchExecutor:
    """Actual raw collector with one load/generate*/unload lifecycle per family."""

    clock: Clock = lambda: datetime.datetime.now(datetime.UTC)
    monotonic_clock: MonotonicClock = time.perf_counter
    loader: LocalHFLoader | None = None
    generator: LocalHFGenerator | None = None
    _session: _LoadedResearchFamily | None = None

    def begin_family(
        self,
        *,
        family: ResearchFamilyBinding,
        qualification: LoadedConfig[LocalQualificationConfig],
        runtime: ResearchRuntimeBinding,
        invocations: tuple[ResearchCollectionInvocation, ...],
        family_directory: Path,
    ) -> None:
        if self._session is not None:
            raise ResearchCollectionError("a research family session is already loaded")
        if not invocations or any(item.family_id != family.family_id for item in invocations):
            raise ResearchCollectionError("family session received invalid invocation membership")
        config = qualification.config
        model = config.active_model
        if (
            family.family_id != model.family_id
            or family.model_repo_id != model.repo_id
            or family.model_revision != model.revision
            or family.tokenizer_revision != model.tokenizer_revision
            or family.runtime_hash != runtime.runtime_hash
            or family.runtime_device != runtime.device
            or family.runtime_dtype != runtime.dtype
        ):
            raise ResearchCollectionError("family session differs from frozen executable pins")

        # This check intentionally imports metadata only. The actual torch/CUDA
        # boundary remains the loader invocation below.
        installed_torch = importlib.metadata.version("torch")
        normalized_bound_torch = runtime.torch_version.split("+", 1)[0]
        if (
            importlib.metadata.version("transformers") != runtime.transformers_version
            or installed_torch != normalized_bound_torch
        ):
            raise ResearchCollectionError("installed torch/transformers versions differ")

        pin = LocalHFModelPin(
            repo_id=family.model_repo_id,
            revision=family.model_revision,
            device=runtime.device,
            dtype=runtime.dtype,
            allow_remote_code=runtime.allow_remote_code,
        )
        formatter = build_local_qualification_formatter(config)
        loader = self.loader or TransformersLocalLoader()
        planned_ids = tuple(sorted(item.invocation_id for item in invocations))
        existing_session_starts = tuple(
            sorted((family_directory / "sessions").glob("*/family_session_start.json"))
        )
        session_attempt_index = len(existing_session_starts)
        started_at = self.clock()
        load_started = self.monotonic_clock()
        _RESEARCH_GPU_LIFECYCLE_LOCK.acquire()
        try:
            loaded_model = loader.load(pin)
        except Exception as exc:
            _RESEARCH_GPU_LIFECYCLE_LOCK.release()
            failure = ResearchFamilySessionFailure(
                family_id=family.family_id,
                model_repo_id=family.model_repo_id,
                model_revision=family.model_revision,
                runtime_hash=runtime.runtime_hash,
                planned_invocation_ids=planned_ids,
                exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
                exception_message=redact_exception_message(str(exc)) or "(no message)",
                failed_at=self.clock(),
            )
            failure_bytes = _canonical_record_bytes(failure)
            failure_hash = sha256_hex(failure_bytes)
            _write_immutable(
                family_directory / "sessions" / "failures" / f"{failure_hash}.json",
                failure_bytes,
            )
            raise
        except BaseException:
            _RESEARCH_GPU_LIFECYCLE_LOCK.release()
            raise
        loaded_at = self.clock()
        load_latency_ms = _elapsed_ms(load_started, self.monotonic_clock())
        session_id = "research_family_session:" + hash_canonical(
            {
                "schema": "lf021_research_family_session_v1",
                "family_id": family.family_id,
                "model_repo_id": family.model_repo_id,
                "model_revision": family.model_revision,
                "runtime_hash": runtime.runtime_hash,
                "session_attempt_index": session_attempt_index,
                "planned_invocation_ids": planned_ids,
                "started_at": _utc_json(started_at),
            }
        )
        start = ResearchFamilySessionStart(
            family_session_id=session_id,
            family_id=family.family_id,
            model_repo_id=family.model_repo_id,
            model_revision=family.model_revision,
            runtime_hash=runtime.runtime_hash,
            session_attempt_index=session_attempt_index,
            planned_invocation_ids=planned_ids,
            started_at=started_at,
            loaded_at=loaded_at,
            load_latency_ms=load_latency_ms,
        )
        try:
            start_path = (
                family_directory
                / "sessions"
                / session_id.rsplit(":", 1)[-1]
                / "family_session_start.json"
            )
            start_sha256 = _write_immutable(
                start_path,
                _canonical_record_bytes(start),
            )
        except BaseException:
            try:
                loader.unload(loaded_model)
            finally:
                _RESEARCH_GPU_LIFECYCLE_LOCK.release()
            raise
        self._session = _LoadedResearchFamily(
            family=family,
            runtime=runtime,
            pin=pin,
            formatter=formatter,
            loaded=loaded_model,
            session_id=session_id,
            start=start,
            start_path=start_path,
            start_sha256=start_sha256,
        )

    def end_family(
        self,
        *,
        family: ResearchFamilyBinding,
        completed_invocation_ids: tuple[str, ...],
        family_directory: Path,
    ) -> None:
        session = self._session
        if session is None or session.family.family_id != family.family_id:
            raise ResearchCollectionError("cannot end an absent or different family session")
        loader = self.loader or TransformersLocalLoader()
        unload_started_at = self.clock()
        unload_started = self.monotonic_clock()
        try:
            loader.unload(session.loaded)
            completed_at = self.clock()
            end = ResearchFamilySessionEnd(
                family_session_id=session.session_id,
                family_id=family.family_id,
                completed_invocation_ids=tuple(sorted(completed_invocation_ids)),
                started_at=unload_started_at,
                completed_at=completed_at,
                unload_latency_ms=_elapsed_ms(unload_started, self.monotonic_clock()),
            )
            _write_immutable(
                family_directory
                / "sessions"
                / session.session_id.rsplit(":", 1)[-1]
                / "family_session_end.json",
                _canonical_record_bytes(end),
            )
        finally:
            self._session = None
            if session.lock_held:
                _RESEARCH_GPU_LIFECYCLE_LOCK.release()

    def _generate(
        self,
        request: LocalHFGenerationRequest,
    ) -> ResearchLocalGenerationResult:
        session = self._session
        if session is None:
            raise ResearchCollectionError("model generation requested without a family session")
        if (
            request.pin != session.pin
            or request.prompt_formatter_id != session.formatter.formatter_id
            or (
                request.prompt_formatter_hash is not None
                and request.prompt_formatter_hash != session.formatter.formatter_hash
            )
        ):
            raise ResearchCollectionError("local request differs from loaded family session")
        started_at = self.clock()
        started = self.monotonic_clock()
        formatted_prompt = session.formatter.format_prompt(
            request.prompt,
            tokenizer=session.loaded.tokenizer,
            pin=request.pin,
        )
        generated: LocalHFGeneratedText = (
            self.generator or TransformersCausalGenerator()
        ).generate(
            loaded=session.loaded,
            formatted_prompt=formatted_prompt,
            decoding=request.decoding,
            device=request.pin.device,
        )
        completed_at = self.clock()
        output_hash = sha256_hex(generated.raw_text.encode("utf-8"))
        formatted_prompt_hash = sha256_hex(formatted_prompt.encode("utf-8"))
        decoding = request.decoding.model_dump(mode="json")
        decoding_hash = hash_canonical(decoding)
        compatibility = LocalHFProviderCompatibility(
            model=request.pin.repo_id,
            revision=request.pin.revision,
            remote_code_authorized=request.pin.allow_remote_code,
            private_source_content=request.private_source_content,
            execution_purpose=request.execution_purpose,
            output_hash=output_hash,
            formatted_prompt_hash=formatted_prompt_hash,
            prompt_formatter_id=request.prompt_formatter_id,
            prompt_formatter_hash=session.formatter.formatter_hash,
            decoding_hash=decoding_hash,
        )
        return ResearchLocalGenerationResult(
            family_session_id=session.session_id,
            request_hash=request.request_hash,
            formatted_prompt_hash=formatted_prompt_hash,
            raw_text=generated.raw_text,
            output_hash=output_hash,
            prompt_tokens=generated.prompt_tokens,
            output_tokens=generated.output_tokens,
            total_tokens=generated.prompt_tokens + generated.output_tokens,
            generation_latency_ms=_elapsed_ms(started, self.monotonic_clock()),
            started_at=started_at,
            completed_at=completed_at,
            decoding=decoding,
            decoding_hash=decoding_hash,
            compatibility=compatibility,
        )

    def execute(
        self,
        *,
        invocation: ResearchCollectionInvocation,
        problem: ProblemPoolRecord,
        qualification: LoadedConfig[LocalQualificationConfig],
        invocation_directory: Path,
        artifact_root: Path,
    ) -> ResearchCollectionTerminal:
        session = self._session
        config = qualification.config
        if (
            session is None
            or session.family.family_id != invocation.family_id
            or config.active_model.family_id != invocation.family_id
            or config.active_model.repo_id != invocation.model_repo_id
            or config.active_model.revision != invocation.model_revision
            or config.prompt.parser_id != invocation.parser_id
            or config.prompt.parser_source_sha256 != invocation.parser_source_sha256
        ):
            raise ResearchCollectionError("invocation differs from executable family pin")

        identity = ProviderIdentity(
            provider="local_hf",
            model=invocation.model_repo_id,
            revision=invocation.model_revision,
            transport="local",
        )
        request = create_provider_request_for_problem(
            identity=identity,
            problem=problem,
            prompt_template_hash=invocation.prompt_template_bundle_hash,
            rendered_prompt=invocation.rendered_user_prompt,
            decoding=cast(Mapping[str, DecodingValue], invocation.decoding),
        )
        local_request = LocalHFGenerationRequest(
            pin=LocalHFModelPin(
                repo_id=invocation.model_repo_id,
                revision=invocation.model_revision,
                device=session.runtime.device,
                dtype=session.runtime.dtype,
                allow_remote_code=session.runtime.allow_remote_code,
            ),
            prompt=invocation.rendered_user_prompt,
            prompt_formatter_id=invocation.prompt_formatter_id,
            prompt_formatter_hash=invocation.prompt_formatter_hash,
            decoding=config.decoding.model_copy(update={"seed": invocation.seed}),
            input_ids=(problem.problem_record_id,),
            private_source_content=False,
            execution_purpose="research_collection",
        )
        request_path = invocation_directory / "provider_request.json"
        persist_provider_request(request, request_path)
        boundary_path = invocation_directory / "provider_boundary.json"
        if boundary_path.exists():
            boundary = _load_canonical(boundary_path, ResearchInvocationBoundary)
            if (
                boundary.invocation_id != invocation.invocation_id
                or boundary.provider_request_hash != request.request_hash
                or boundary.local_runtime_request_hash != local_request.request_hash
            ):
                raise ResearchCollectionArtifactConflict(
                    "persisted provider boundary differs from frozen request"
                )
        else:
            boundary = ResearchInvocationBoundary.create(
                invocation_id=invocation.invocation_id,
                provider_request_hash=request.request_hash,
                local_runtime_request_hash=local_request.request_hash,
                crossed_at=self.clock(),
            )
            _write_immutable(boundary_path, _canonical_record_bytes(boundary))

        result_path = invocation_directory / "local_generation_result.json"
        failure_path = invocation_directory / "local_runtime_failure.json"
        attempt_path = invocation_directory / "model_attempt_boundary.json"
        resumed = False
        runtime_result: ResearchLocalGenerationResult | None = None
        runtime_failure: ResearchRuntimeFailure | None = None
        if result_path.exists() and failure_path.exists():
            raise ResearchCollectionArtifactConflict(
                "invocation has both a runtime result and runtime failure"
            )
        if result_path.exists():
            runtime_result = _load_canonical(result_path, ResearchLocalGenerationResult)
            resumed = True
        elif failure_path.exists():
            runtime_failure = _load_canonical(failure_path, ResearchRuntimeFailure)
            resumed = True
        elif attempt_path.exists():
            attempt = _load_canonical(attempt_path, ResearchModelAttemptBoundary)
            if (
                attempt.invocation_id != invocation.invocation_id
                or attempt.local_runtime_request_hash != local_request.request_hash
            ):
                raise ResearchCollectionArtifactConflict(
                    "persisted model-attempt boundary differs from frozen request"
                )
            runtime_failure = ResearchRuntimeFailure(
                stage="incomplete_prior_model_attempt",
                model_invocation_attempted=True,
                exception_type="IncompletePriorModelAttempt",
                exception_message=(
                    "a prior model attempt crossed its no-retry boundary without "
                    "persisting a result"
                ),
                failed_at=self.clock(),
            )
            _write_immutable(failure_path, _canonical_record_bytes(runtime_failure))
            resumed = True
        else:
            attempt = ResearchModelAttemptBoundary(
                invocation_id=invocation.invocation_id,
                family_session_id=session.session_id,
                local_runtime_request_hash=local_request.request_hash,
                started_at=self.clock(),
            )
            _write_immutable(attempt_path, _canonical_record_bytes(attempt))
            try:
                runtime_result = self._generate(local_request)
                _write_immutable(result_path, _canonical_record_bytes(runtime_result))
            except Exception as exc:
                runtime_failure = ResearchRuntimeFailure(
                    stage="model_generation",
                    model_invocation_attempted=True,
                    exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
                    exception_message=redact_exception_message(str(exc)) or "(no message)",
                    failed_at=self.clock(),
                )
                _write_immutable(failure_path, _canonical_record_bytes(runtime_failure))

        if not attempt_path.is_file():
            raise ResearchCollectionArtifactConflict(
                "persisted runtime outcome lacks its model-attempt boundary"
            )
        attempt = _load_canonical(attempt_path, ResearchModelAttemptBoundary)
        if (
            attempt.invocation_id != invocation.invocation_id
            or attempt.local_runtime_request_hash != local_request.request_hash
        ):
            raise ResearchCollectionArtifactConflict(
                "model-attempt boundary differs from frozen request"
            )
        if runtime_result is not None and (
            runtime_result.request_hash != local_request.request_hash
        ):
            raise ResearchCollectionArtifactConflict(
                "persisted local result differs from frozen local request"
            )
        if runtime_result is not None:
            response = ProviderRawResponse.success(request, runtime_result.raw_text)
            status = ResearchTerminalStatus.RAW_COLLECTED
            error_code = None
            error_detail = None
        else:
            assert runtime_failure is not None
            response = ProviderRawResponse.error(
                request,
                error_type=runtime_failure.exception_type,
                error_detail=runtime_failure.exception_message,
            )
            status = ResearchTerminalStatus.RUNTIME_FAILED
            error_code = runtime_failure.exception_type
            error_detail = runtime_failure.exception_message
        completed_at = (
            runtime_result.completed_at
            if runtime_result is not None
            else cast(ResearchRuntimeFailure, runtime_failure).failed_at
        )
        result_session_id = (
            runtime_result.family_session_id
            if runtime_result is not None
            else attempt.family_session_id
        )
        collection_root = invocation_directory.parent.parent
        session_start_path = (
            collection_root
            / "families"
            / invocation.family_id
            / "sessions"
            / result_session_id.rsplit(":", 1)[-1]
            / "family_session_start.json"
        )
        if not session_start_path.is_file():
            raise ResearchCollectionArtifactConflict(
                "runtime outcome lacks its family session start record"
            )
        artifacts = {
            "family_session_start": hash_file(session_start_path),
            "provider_boundary": hash_file(boundary_path),
            "provider_request": hash_file(request_path),
            "model_attempt_boundary": hash_file(attempt_path),
        }
        if runtime_result is not None:
            artifacts["local_generation_result"] = hash_file(result_path)
        else:
            artifacts["local_runtime_failure"] = hash_file(failure_path)
        try:
            provider_result = persist_provider_raw_response(
                invocation_directory / "raw",
                response,
            )
            lineage = bridge_provider_result_to_llm_lineage(
                request=request,
                result=provider_result,
                request_artifact_path=request_path,
                artifact_root=artifact_root,
                problem=problem,
                provider_slot=invocation.provider_slot,
                model_family=invocation.family_id,
                prompt_template_id=invocation.prompt_formatter_id,
                prompt_template_version="research_v1",
                execution_mode="local",
                parse_status=ParseStatus.EMPTY,
                parsed_statement=None,
                started_at=boundary.crossed_at,
                completed_at=completed_at,
                supervision_eligible=False,
                heldout_generator=False,
                metadata={
                    "artifact_class": "research",
                    "execution_purpose": "research_collection",
                    "invocation_id": invocation.invocation_id,
                    "context_id": invocation.context_id,
                    "parser_id": invocation.parser_id,
                    "parser_source_sha256": invocation.parser_source_sha256,
                    "semantic_labels_created": False,
                    "gate_5g_credit_claimed": False,
                },
            )
            call_path = invocation_directory / "llm_call.json"
            lineage_attempt_path = invocation_directory / "llm_attempt.json"
            call_hash = _write_immutable(call_path, _canonical_record_bytes(lineage.call))
            attempt_hash = _write_immutable(
                lineage_attempt_path,
                _canonical_record_bytes(lineage.attempt),
            )
            artifacts.update(
                {
                    "llm_attempt": attempt_hash,
                    "llm_call": call_hash,
                    "provider_raw_response": provider_result.raw_response_sha256,
                }
            )
            llm_call_id: str | None = lineage.call.call_id
            llm_attempt_id: str | None = lineage.attempt.attempt_id
        except Exception as exc:
            lineage_failure_path = invocation_directory / "provider_lineage_failure.json"
            lineage_failure = ResearchRuntimeFailure(
                stage="provider_lineage",
                model_invocation_attempted=True,
                exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
                exception_message=redact_exception_message(str(exc)) or "(no message)",
                failed_at=self.clock(),
            )
            try:
                artifacts["provider_lineage_failure"] = _write_immutable(
                    lineage_failure_path,
                    _canonical_record_bytes(lineage_failure),
                )
            except Exception as persist_exc:
                raise ResearchCollectionPostBoundaryError(
                    "cannot persist post-boundary provider-lineage failure"
                ) from persist_exc
            status = ResearchTerminalStatus.RUNTIME_FAILED
            error_code = lineage_failure.exception_type
            error_detail = lineage_failure.exception_message
            completed_at = lineage_failure.failed_at
            llm_call_id = None
            llm_attempt_id = None

        payload = _terminal_payload(
            invocation=invocation,
            status=status,
            started_at=boundary.crossed_at,
            completed_at=completed_at,
            model_invocation_attempted=True,
            resumed=resumed,
            family_session_id=(result_session_id),
            provider_request_hash=request.request_hash,
            llm_call_id=llm_call_id,
            llm_attempt_id=llm_attempt_id,
            local_runtime_request_hash=local_request.request_hash,
            raw_output_sha256=(runtime_result.output_hash if runtime_result is not None else None),
            artifact_hashes=artifacts,
            error_code=error_code,
            error_detail=error_detail,
        )
        terminal_id = "research_collection_terminal:" + hash_canonical(
            {"schema": "lf021_research_collection_terminal_v1", **payload}
        )
        return ResearchCollectionTerminal.model_validate({"terminal_id": terminal_id, **payload})


@dataclass(frozen=True, slots=True)
class ResearchCollectionRun:
    output_directory: Path
    plan_path: Path
    manifest_path: Path
    manifest: ResearchCollectionManifest
    terminals: tuple[ResearchCollectionTerminal, ...]


def _terminal_path(root: Path, invocation: ResearchCollectionInvocation) -> Path:
    suffix = invocation.invocation_id.rsplit(":", 1)[-1]
    return root / "terminals" / f"{suffix}.json"


def execute_research_collection(
    loaded: LoadedResearchCollection,
    *,
    repo_root: Path,
    executor: ResearchInvocationExecutor,
    clock: Clock = lambda: datetime.datetime.now(datetime.UTC),
) -> ResearchCollectionRun:
    """Execute or resume the exact plan, writing one terminal per invocation."""

    if not loaded.preflight.execution_ready:
        raise ResearchCollectionExecutionBlocked(
            "research collection is blocked: " + "; ".join(loaded.preflight.blocking_prerequisites)
        )
    root = repo_root / loaded.config.config.outputs.root / loaded.plan.plan_id.split(":")[-1]
    plan_path = root / "plan.json"
    _write_immutable(plan_path, _canonical_record_bytes(loaded.plan))
    problem_by_id = {problem.problem_record_id: problem for problem in loaded.problems}
    terminals: list[ResearchCollectionTerminal] = []
    binding_by_family = {binding.family_id: binding for binding in loaded.plan.family_bindings}
    invocations_by_family = {
        family_id: tuple(
            invocation
            for invocation in loaded.plan.invocations
            if invocation.family_id == family_id
        )
        for family_id in sorted(binding_by_family)
    }
    for family_id, family_invocations in invocations_by_family.items():
        existing: list[ResearchCollectionTerminal] = []
        pending: list[ResearchCollectionInvocation] = []
        for invocation in family_invocations:
            terminal_path = _terminal_path(root, invocation)
            if not terminal_path.exists():
                pending.append(invocation)
                continue
            record = _load_canonical(terminal_path, ResearchCollectionTerminal)
            if (
                record.invocation_id != invocation.invocation_id
                or record.invocation_payload_hash
                != hash_canonical(invocation.model_dump(mode="json"))
            ):
                raise ResearchCollectionArtifactConflict(
                    f"terminal differs from invocation: {terminal_path}"
                )
            existing.append(record)
        terminals.extend(existing)
        if not pending:
            continue

        binding = binding_by_family[family_id]
        family_directory = root / "families" / family_id
        try:
            executor.begin_family(
                family=binding,
                qualification=loaded.qualifications[family_id],
                runtime=loaded.config.config.runtime,
                invocations=tuple(pending),
                family_directory=family_directory,
            )
        except Exception as exc:
            for invocation in pending:
                terminal = make_orchestration_failure_terminal(
                    invocation,
                    exception=exc,
                    at=clock(),
                )
                _write_immutable(
                    _terminal_path(root, invocation),
                    _canonical_record_bytes(terminal),
                )
                terminals.append(terminal)
            continue

        completed_family_ids: list[str] = []
        try:
            for invocation in pending:
                terminal_path = _terminal_path(root, invocation)
                invocation_directory = (
                    root / "invocations" / invocation.invocation_id.split(":")[-1]
                )
                try:
                    terminal = executor.execute(
                        invocation=invocation,
                        problem=problem_by_id[invocation.problem_record_id],
                        qualification=loaded.qualifications[invocation.family_id],
                        invocation_directory=invocation_directory,
                        artifact_root=repo_root,
                    )
                except Exception as exc:
                    provider_artifacts_exist = (
                        invocation_directory / "provider_request.json"
                    ).exists() or (invocation_directory / "provider_boundary.json").exists()
                    if provider_artifacts_exist:
                        raise ResearchCollectionPostBoundaryError(
                            "executor raised after a provider/model-attempt boundary; "
                            "the run remains incomplete rather than inventing a terminal"
                        ) from exc
                    terminal = make_orchestration_failure_terminal(
                        invocation,
                        exception=exc,
                        at=clock(),
                    )
                _write_immutable(terminal_path, _canonical_record_bytes(terminal))
                terminals.append(terminal)
                completed_family_ids.append(invocation.invocation_id)
        finally:
            executor.end_family(
                family=binding,
                completed_invocation_ids=tuple(completed_family_ids),
                family_directory=family_directory,
            )

    terminals = sorted(terminals, key=lambda item: item.invocation_id)
    for terminal in terminals:
        if terminal.family_session_id is None:
            continue
        session_start_path = (
            root
            / "families"
            / terminal.family_id
            / "sessions"
            / terminal.family_session_id.rsplit(":", 1)[-1]
            / "family_session_start.json"
        )
        if not session_start_path.is_file() or hash_file(
            session_start_path
        ) != terminal.artifact_hashes.get("family_session_start"):
            raise ResearchCollectionArtifactConflict(
                f"terminal family-session binding is missing or changed: {terminal.invocation_id}"
            )
    counts = Counter(terminal.status.value for terminal in terminals)
    successful_families = {
        terminal.family_id
        for terminal in terminals
        if terminal.status is ResearchTerminalStatus.RAW_COLLECTED
    }
    terminal_hashes = {
        str(_terminal_path(root, invocation).relative_to(repo_root)): hash_file(
            _terminal_path(root, invocation)
        )
        for invocation in loaded.plan.invocations
    }
    family_session_hashes = {
        str(path.relative_to(repo_root)): hash_file(path)
        for path in sorted((root / "families").glob("**/*.json"))
        if path.is_file() and not path.is_symlink()
    }
    manifest_payload: dict[str, object] = {
        "schema_version": 1,
        "plan_id": loaded.plan.plan_id,
        "plan_hash": loaded.plan.plan_hash,
        "actual_collection_performed": True,
        "expected_candidate_count": len(loaded.plan.invocations),
        "terminal_candidate_count": len(terminals),
        "status_counts": dict(sorted(counts.items())),
        "successful_family_count": len(successful_families),
        "terminal_artifact_hashes": dict(sorted(terminal_hashes.items())),
        "family_session_artifact_hashes": dict(sorted(family_session_hashes.items())),
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    manifest_id = "research_collection_manifest:" + hash_canonical(
        {"schema": "lf021_research_collection_manifest_v1", **manifest_payload}
    )
    manifest = ResearchCollectionManifest.model_validate(
        {"manifest_id": manifest_id, **manifest_payload}
    )
    manifest_path = root / "manifest.json"
    _write_immutable(manifest_path, _canonical_record_bytes(manifest))
    return ResearchCollectionRun(
        output_directory=root,
        plan_path=plan_path,
        manifest_path=manifest_path,
        manifest=manifest,
        terminals=tuple(terminals),
    )


__all__ = [
    "LoadedResearchCollection",
    "LocalHFResearchExecutor",
    "ResearchCollectionArtifactConflict",
    "ResearchCollectionConfig",
    "ResearchCollectionError",
    "ResearchCollectionExecutionBlocked",
    "ResearchCollectionInvocation",
    "ResearchCollectionManifest",
    "ResearchCollectionPlan",
    "ResearchCollectionPreflightReport",
    "ResearchCollectionRun",
    "ResearchCollectionTerminal",
    "ResearchInvocationExecutor",
    "ResearchTerminalStatus",
    "execute_research_collection",
    "load_research_collection",
    "make_orchestration_failure_terminal",
    "write_preflight_report",
]
