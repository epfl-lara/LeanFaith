"""Resumable one-task executor for public, provisional LF-022 ``G_open`` data.

The executor is offline by default.  Live transport requires an explicit flag,
an admitted public task, and runtime-only credentials.  Every wire request is
persisted before transport.  Every response body is persisted before HTTP,
OpenAI-response, or proposer parsing.
"""

from __future__ import annotations

import datetime
import fcntl
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_execution import (
    LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
    LF022ExecutionError,
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    VerifiedLF022ExecutionAdmission,
    VerifiedLF022ExecutionTaskInputs,
    lf022_qualification_claim_path,
    lf022_reviewed_proposer_prompt,
    make_lf022_qualification_claim,
    verify_lf022_execution_admission,
    verify_lf022_execution_task,
)
from leanfaith.generation.llm_variants import (
    RenderedVariantPrompt,
    VariantOutputErrorCode,
    VariantOutputParseError,
    VariantProposalBatch,
    materialize_verified_provisional_variants,
    parse_variant_proposer_output,
    render_variant_proposer_prompt,
    variant_provider_input_ids,
)
from leanfaith.generation.providers import (
    ProviderIdentity,
    ProviderRawResponse,
    ProviderRequest,
    ProviderResult,
    load_provider_request,
    persist_provider_raw_response,
    persist_provider_request,
)
from leanfaith.generation.rcp_provider import (
    RCPCompletion,
    RCPHTTPTransport,
    RCPResponseError,
    RCPTransportUnknownError,
    RCPWireResponse,
    classify_http_response,
    make_chat_completion_payload,
    parse_chat_completion,
    retry_delay_seconds,
)
from leanfaith.schemas.enums import (
    LLMAttemptStatus,
    LLMCallStatus,
    LLMRole,
    ParseStatus,
    QualityTier,
    ValidationStatus,
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.llm import (
    LLMAttemptRecord,
    LLMCallRecord,
    check_llm_call_attempt_lineage,
    make_llm_attempt_id,
    make_llm_call_id,
)
from leanfaith.schemas.manifest import ManifestError, collect_code_state, require_utc
from leanfaith.schemas.variant import VariantRecord


class LF022ExecutorError(RuntimeError):
    """A preflight, execution, persistence, or replay invariant failed."""


class LF022LiveExecutionRequired(LF022ExecutorError):
    """Reserved for callers that require a terminal result from an offline run."""


class LF022TaskLockedError(LF022ExecutorError):
    """Another process currently owns the same content-addressed task."""


@dataclass(frozen=True, slots=True, repr=False)
class RCPRuntimeCredentials:
    """Runtime-only RCP credentials; never serialized."""

    base_url: str
    api_key: str

    def __repr__(self) -> str:
        return f"RCPRuntimeCredentials(base_url={self.base_url!r}, api_key='<redacted>')"


class LF022ExecutionPreflight(StrictModel):
    """Network-free proof that one exact task is ready for explicit execution."""

    schema_version: Literal[1] = 1
    preflight_id: str = Field(pattern=id_pattern("lf022_execution_preflight"))
    execution_admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    allocation_plan_id: str = Field(pattern=id_pattern("lf022_production_plan"))
    public_pool_audit_id: str = Field(pattern=id_pattern("lf022_public_pool_audit"))
    route_snapshot_revision: str = Field(pattern=r"^rcp-catalog-sha256:[0-9a-f]{64}$")
    model_id: str
    proposer_family_id: str
    prompt_template_hash: str = Field(pattern=HEX64_PATTERN)
    prompt_render_hash: str = Field(pattern=HEX64_PATTERN)
    retry_policy_hash: str = Field(pattern=HEX64_PATTERN)
    code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    distribution: Literal["G_open"]
    source_is_public: Literal[True]
    private_source_content: Literal[False]
    denylist_checked: Literal[True]
    denylist_hits: tuple[()] = ()
    network_calls_performed: Literal[0] = 0
    live_execution_requires_explicit_flag: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_promotion_enabled: Literal[False] = False
    gold_promotion_enabled: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        expected = make_id(
            "lf022_execution_preflight",
            self.model_dump(mode="json", exclude={"preflight_id"}),
        )
        if self.preflight_id != expected:
            raise ValueError("preflight_id does not match canonical preflight")
        return self


class LF022WireResponseMetadata(StrictModel):
    """Persisted HTTP metadata bound to the exact raw body bytes."""

    schema_version: Literal[1] = 1
    status_code: int = Field(ge=100, le=599, strict=True)
    headers: dict[str, str]
    body_sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _headers_sorted(self) -> Self:
        if list(self.headers) != sorted(self.headers):
            raise ValueError("wire response headers must be sorted")
        return self


AttemptStatus = Literal[
    "response_parsed",
    "proposer_parse_failed",
    "invalid_response",
    "retryable_http_error",
    "terminal_http_error",
    "transport_unknown",
]


class LF022ExecutionAttemptRecord(StrictModel):
    """One append-only provider attempt and all exact artifact bindings."""

    schema_version: Literal[1] = 1
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    provider_request_hash: str = Field(pattern=HEX64_PATTERN)
    provider_attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    attempt_index: int = Field(ge=0, strict=True)
    request_artifact: str
    request_sha256: str = Field(pattern=HEX64_PATTERN)
    wire_request_artifact: str
    wire_request_sha256: str = Field(pattern=HEX64_PATTERN)
    wire_response_body_artifact: str | None = None
    wire_response_body_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    wire_response_metadata_artifact: str | None = None
    wire_response_metadata_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    provider_raw_artifact: str
    provider_raw_sha256: str = Field(pattern=HEX64_PATTERN)
    status: AttemptStatus
    retryable: bool
    http_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: float | None = Field(default=None, ge=0.0)
    error_code: str | None = None
    provider_request_id: str | None = None
    returned_model: str | None = None
    tokens: dict[str, int] = Field(default_factory=dict)
    started_at: datetime.datetime
    completed_at: datetime.datetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.started_at)
        require_utc(self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("attempt completion precedes start")
        response_fields = (
            self.wire_response_body_artifact,
            self.wire_response_body_sha256,
            self.wire_response_metadata_artifact,
            self.wire_response_metadata_sha256,
        )
        if any(value is not None for value in response_fields) and any(
            value is None for value in response_fields
        ):
            raise ValueError("wire response body/metadata bindings must be complete")
        response_present = response_fields[0] is not None
        if self.status == "transport_unknown":
            if response_present or self.http_status is not None:
                raise ValueError("transport_unknown cannot carry a wire response")
        elif not response_present:
            raise ValueError(f"{self.status} requires a persisted wire response")
        if self.status == "response_parsed":
            if (
                self.retryable
                or self.http_status != 200
                or self.error_code is not None
                or self.returned_model is None
            ):
                raise ValueError("response_parsed has inconsistent terminal fields")
        else:
            if self.error_code is None:
                raise ValueError(f"{self.status} requires error_code")
        if self.status == "retryable_http_error" and not self.retryable:
            raise ValueError("retryable_http_error must be retryable")
        if self.status != "retryable_http_error" and self.retryable:
            raise ValueError("only retryable_http_error may be retried")
        for field in (
            "request_artifact",
            "wire_request_artifact",
            "provider_raw_artifact",
            "wire_response_body_artifact",
            "wire_response_metadata_artifact",
        ):
            value = getattr(self, field)
            if value is not None:
                _safe_relative(value, field=field)
        return self


TerminalStatus = Literal[
    "provisional_variants_created",
    "proposer_parse_failed",
    "provider_exhausted",
    "transport_unknown",
]


class LF022ExecutionTerminalRecord(StrictModel):
    """Terminal, replayable task result; never a semantic label."""

    schema_version: Literal[1] = 1
    terminal_id: str = Field(pattern=id_pattern("lf022_execution_terminal"))
    execution_admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    status: TerminalStatus
    attempt_artifacts: tuple[str, ...] = Field(min_length=1)
    attempt_sha256s: tuple[str, ...] = Field(min_length=1)
    llm_attempt_artifacts: tuple[str, ...] = Field(min_length=1)
    llm_attempt_sha256s: tuple[str, ...] = Field(min_length=1)
    llm_call_id: str = Field(pattern=id_pattern("call"))
    llm_call_artifact: str
    llm_call_sha256: str = Field(pattern=HEX64_PATTERN)
    variants_artifact: str | None = None
    variants_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    provisional_variant_count: int = Field(ge=0, strict=True)
    terminal_error_code: str | None = None
    raw_before_parse_verified: Literal[True]
    exact_replay_supported: Literal[True]
    output_quality_tier: Literal["provisional"]
    semantic_labels_created: Literal[False]
    silver_promotion_enabled: Literal[False]
    gold_promotion_enabled: Literal[False]
    training_eligible: Literal[False]
    evaluation_eligible: Literal[False]
    gate_credit_claimed: Literal[False]

    @model_validator(mode="after")
    def _coherent_and_content_addressed(self) -> Self:
        if len(self.attempt_artifacts) != len(self.attempt_sha256s):
            raise ValueError("attempt artifact/hash counts differ")
        if len(self.llm_attempt_artifacts) != len(self.llm_attempt_sha256s):
            raise ValueError("LLM attempt artifact/hash counts differ")
        if len(self.attempt_artifacts) != len(self.llm_attempt_artifacts):
            raise ValueError("execution and LLM attempt counts differ")
        for path in (
            *self.attempt_artifacts,
            *self.llm_attempt_artifacts,
            self.llm_call_artifact,
        ):
            _safe_relative(path, field="terminal artifact")
        if self.variants_artifact is not None:
            _safe_relative(self.variants_artifact, field="variants_artifact")
        variants_present = self.variants_artifact is not None
        if variants_present != (self.variants_sha256 is not None):
            raise ValueError("variant artifact/hash must be present together")
        if self.status == "provisional_variants_created":
            if not variants_present or self.provisional_variant_count < 1:
                raise ValueError("successful terminal requires provisional variants")
            if self.terminal_error_code is not None:
                raise ValueError("successful terminal cannot carry an error")
        elif variants_present or self.provisional_variant_count != 0:
            raise ValueError("failed terminal cannot carry variants")
        expected = make_id(
            "lf022_execution_terminal",
            self.model_dump(mode="json", exclude={"terminal_id"}),
        )
        if self.terminal_id != expected:
            raise ValueError("terminal_id does not match canonical terminal record")
        return self


@dataclass(frozen=True, slots=True)
class PreparedLF022Execution:
    admission: LF022GOpenExecutionAdmission
    task: LF022GOpenExecutionTask
    prompt: RenderedVariantPrompt
    preflight: LF022ExecutionPreflight
    task_directory: Path


@dataclass(frozen=True, slots=True)
class LF022ExecutionResult:
    preflight: LF022ExecutionPreflight
    terminal: LF022ExecutionTerminalRecord | None
    terminal_path: Path | None
    replayed: bool
    network_calls_this_run: int


@dataclass(frozen=True, slots=True)
class _AttemptOutcome:
    record: LF022ExecutionAttemptRecord
    batch: VariantProposalBatch | None
    completion: RCPCompletion | None


def _safe_relative(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    if (
        not value.strip()
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in value
    ):
        raise ValueError(f"{field} must be a normalized relative path")
    return value


def _relative(path: Path, root: Path) -> str:
    try:
        return str(PurePosixPath(path.resolve().relative_to(root.resolve()).as_posix()))
    except ValueError as exc:
        raise LF022ExecutorError("execution artifact escapes artifact_root") from exc


def _artifact_path(root: Path, artifact: str, *, label: str) -> Path:
    """Resolve one persisted repository-relative artifact without following symlinks."""

    _safe_relative(artifact, field=label)
    canonical_root = root.resolve(strict=True)
    current = canonical_root
    for part in PurePosixPath(artifact).parts:
        current = current / part
        if current.is_symlink():
            raise LF022ExecutorError(f"{label} contains a symlinked component")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise LF022ExecutorError(f"{label} is missing: {artifact}") from exc
    if not resolved.is_relative_to(canonical_root) or not resolved.is_file():
        raise LF022ExecutorError(f"{label} must be a regular artifact file")
    return resolved


def _immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LF022ExecutorError(f"immutable artifact cannot be a symlink: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022ExecutorError(f"immutable artifact conflict: {path}")
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
                raise LF022ExecutorError(f"concurrent immutable conflict: {path}") from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_record(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _load_record[RecordT: StrictModel](
    path: Path,
    model: type[RecordT],
    *,
    label: str,
) -> RecordT:
    if path.is_symlink() or not path.is_file():
        raise LF022ExecutorError(f"{label} is missing or unsafe: {path}")
    raw = path.read_bytes()
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022ExecutorError(f"invalid {label}: {exc}") from exc
    if raw != _canonical_record(record):
        raise LF022ExecutorError(f"{label} is not canonical JSON")
    return record


def _task_directory(output_root: Path, execution_task_id: str) -> Path:
    digest = execution_task_id.removeprefix("lf022_execution_task:")
    return output_root / "tasks" / digest[:2] / digest


def _repository_output_root(repo_root: Path, output_root: Path) -> Path:
    root = repo_root.resolve(strict=True)
    candidate = output_root if output_root.is_absolute() else root / output_root
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LF022ExecutorError("output_root must stay inside repo_root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise LF022ExecutorError("output_root must be a normalized directory below repo_root")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LF022ExecutorError("output_root contains a symlinked component")
        if current.exists() and not current.is_dir():
            raise LF022ExecutorError("output_root path component is not a directory")
        current.mkdir(exist_ok=True)
    return current


def prepare_lf022_g_open_execution(
    *,
    repo_root: Path,
    output_root: Path,
    admission: LF022GOpenExecutionAdmission,
    task: LF022GOpenExecutionTask,
    verified_admission: VerifiedLF022ExecutionAdmission | None = None,
    verified_task_inputs: VerifiedLF022ExecutionTaskInputs | None = None,
    observed_code_tree_hash: str | None = None,
) -> PreparedLF022Execution:
    """Validate exact bindings and persist a network-free preflight."""

    current_code_tree_hash = observed_code_tree_hash
    if current_code_tree_hash is None:
        try:
            current_code_tree_hash = collect_code_state(repo_root).code_tree_hash
        except ManifestError as exc:
            raise LF022ExecutorError(f"cannot verify current code tree: {exc}") from exc
    if current_code_tree_hash != admission.code_tree_hash:
        raise LF022ExecutorError("current repository code tree differs from execution admission")
    canonical_output_root = _repository_output_root(repo_root, output_root)
    if verified_admission is not None:
        if verified_admission.admission_id != admission.admission_id:
            raise LF022ExecutorError("cached verification belongs to a different admission")
        verified = verified_admission
    else:
        if verified_task_inputs is not None:
            raise LF022ExecutorError("cached task inputs require the matching verified admission")
        verified = verify_lf022_execution_admission(
            repo_root=repo_root,
            admission=admission,
        )
    verify_lf022_execution_task(
        repo_root=repo_root,
        admission=admission,
        verified=verified,
        task=task,
        inputs=verified_task_inputs,
    )
    reviewed_prompt_path, reviewed_prompt_sha256 = lf022_reviewed_proposer_prompt(
        admission.prompt_template_version
    )
    if (
        admission.artifacts.prompt_template.path != reviewed_prompt_path
        or admission.artifacts.prompt_template.sha256 != reviewed_prompt_sha256
    ):
        raise LF022ExecutorError("execution prompt differs from the exact reviewed proposer prompt")
    if admission.route.execution_scope == "one_item_proposer_qualification_only":
        expected_output_root = repo_root.resolve(strict=True) / LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT
        if canonical_output_root != expected_output_root:
            raise LF022ExecutorError(
                "qualification execution requires the canonical global LF-022 executor root"
            )
        claim = make_lf022_qualification_claim(
            admission=admission,
            task=task,
        )
        _immutable(
            lf022_qualification_claim_path(
                output_root=canonical_output_root,
                admission=admission,
                claim=claim,
            ),
            canonical_json_bytes(claim.model_dump(mode="json")),
        )
    prompt_path = repo_root / admission.artifacts.prompt_template.path
    prompt = render_variant_proposer_prompt(
        task.prompt_request(),
        template_path=prompt_path,
    )
    if (
        prompt.template_version != admission.prompt_template_version
        or prompt.template_sha256 != admission.artifacts.prompt_template.sha256
    ):
        raise LF022ExecutorError("rendered prompt template differs from admission")
    payload: dict[str, object] = {
        "schema_version": 1,
        "execution_admission_id": admission.admission_id,
        "execution_task_id": task.execution_task_id,
        "allocation_plan_id": admission.allocation_plan_id,
        "public_pool_audit_id": admission.public_pool_audit_id,
        "route_snapshot_revision": admission.route.route_snapshot_revision,
        "model_id": admission.route.model_id,
        "proposer_family_id": admission.route.proposer_family_id,
        "prompt_template_hash": prompt.template_sha256,
        "prompt_render_hash": prompt.render_sha256,
        "retry_policy_hash": admission.retry_policy_hash,
        "code_tree_hash": admission.code_tree_hash,
        "distribution": "G_open",
        "source_is_public": True,
        "private_source_content": False,
        "denylist_checked": True,
        "denylist_hits": [],
        "network_calls_performed": 0,
        "live_execution_requires_explicit_flag": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    preflight = LF022ExecutionPreflight.model_validate(
        {
            **payload,
            "preflight_id": make_id("lf022_execution_preflight", payload),
        }
    )
    task_dir = _task_directory(canonical_output_root, task.execution_task_id)
    _immutable(task_dir / "admission.json", _canonical_record(admission))
    _immutable(task_dir / "task.json", _canonical_record(task))
    _immutable(task_dir / "preflight.json", _canonical_record(preflight))
    return PreparedLF022Execution(
        admission=admission,
        task=task,
        prompt=prompt,
        preflight=preflight,
        task_directory=task_dir,
    )


def _provider_request(
    prepared: PreparedLF022Execution,
    *,
    attempt_index: int,
) -> ProviderRequest:
    admission = prepared.admission
    identity = ProviderIdentity(
        provider=admission.route.provider_id,
        model=admission.route.model_id,
        revision=admission.route.route_snapshot_revision,
        transport="external_disabled",
    )
    return ProviderRequest.create(
        identity=identity,
        prompt_template_hash=prepared.prompt.template_sha256,
        rendered_prompt=prepared.prompt.text,
        decoding=admission.route.decoding.provider_decoding(),
        input_ids=variant_provider_input_ids(prepared.task.prompt_request()),
        private_source_content=False,
        attempt_index=attempt_index,
    )


def _wire_response(
    *,
    attempt_dir: Path,
    response: RCPWireResponse,
) -> tuple[Path, str, Path, str]:
    body_path = attempt_dir / "wire_response.body"
    body_sha = _immutable(body_path, response.body)
    metadata = LF022WireResponseMetadata(
        status_code=response.status_code,
        headers=dict(sorted(response.headers.items())),
        body_sha256=body_sha,
    )
    metadata_path = attempt_dir / "wire_response.json"
    metadata_sha = _immutable(metadata_path, _canonical_record(metadata))
    return body_path, body_sha, metadata_path, metadata_sha


def _load_wire_response(attempt_dir: Path) -> RCPWireResponse | None:
    body_path = attempt_dir / "wire_response.body"
    metadata_path = attempt_dir / "wire_response.json"
    if not body_path.exists() and not metadata_path.exists():
        return None
    if not body_path.is_file() or body_path.is_symlink():
        raise LF022ExecutorError("partial or unsafe persisted wire-response body")
    metadata = _load_record(
        metadata_path,
        LF022WireResponseMetadata,
        label="wire response metadata",
    )
    body = body_path.read_bytes()
    if hash_file(body_path) != metadata.body_sha256:
        raise LF022ExecutorError("wire response body hash differs from metadata")
    return RCPWireResponse(
        status_code=metadata.status_code,
        headers=metadata.headers,
        body=body,
    )


def _provider_result_from_error(
    *,
    task_dir: Path,
    request: ProviderRequest,
    error_code: str,
) -> ProviderResult:
    return persist_provider_raw_response(
        task_dir / "provider_raw",
        ProviderRawResponse.error(request, error_type=error_code),
    )


def _attempt_record(
    *,
    prepared: PreparedLF022Execution,
    artifact_root: Path,
    request: ProviderRequest,
    request_path: Path,
    wire_request_path: Path,
    response_paths: tuple[Path, str, Path, str] | None,
    provider_result: ProviderResult,
    status: AttemptStatus,
    retryable: bool,
    http_status: int | None,
    retry_after: float | None,
    error_code: str | None,
    completion: RCPCompletion | None,
    started_at: datetime.datetime,
    completed_at: datetime.datetime,
) -> LF022ExecutionAttemptRecord:
    if response_paths is None:
        body_path = None
        body_sha = None
        metadata_path = None
        metadata_sha = None
    else:
        body_path, body_sha, metadata_path, metadata_sha = response_paths
    return LF022ExecutionAttemptRecord(
        execution_task_id=prepared.task.execution_task_id,
        provider_request_hash=request.request_hash,
        provider_attempt_id=request.attempt_id,
        attempt_index=request.attempt_index,
        request_artifact=_relative(request_path, artifact_root),
        request_sha256=hash_file(request_path),
        wire_request_artifact=_relative(wire_request_path, artifact_root),
        wire_request_sha256=hash_file(wire_request_path),
        wire_response_body_artifact=(
            _relative(body_path, artifact_root) if body_path is not None else None
        ),
        wire_response_body_sha256=body_sha,
        wire_response_metadata_artifact=(
            _relative(metadata_path, artifact_root) if metadata_path is not None else None
        ),
        wire_response_metadata_sha256=metadata_sha,
        provider_raw_artifact=_relative(
            provider_result.raw_response_path,
            artifact_root,
        ),
        provider_raw_sha256=provider_result.raw_response_sha256,
        status=status,
        retryable=retryable,
        http_status=http_status,
        retry_after_seconds=retry_after,
        error_code=error_code,
        provider_request_id=(completion.provider_request_id if completion is not None else None),
        returned_model=(completion.returned_model if completion is not None else None),
        tokens=completion.usage if completion is not None else {},
        started_at=started_at,
        completed_at=completed_at,
    )


def _execute_or_recover_attempt(
    *,
    prepared: PreparedLF022Execution,
    artifact_root: Path,
    credentials: RCPRuntimeCredentials,
    transport: RCPHTTPTransport,
    attempt_index: int,
    clock: Callable[[], datetime.datetime],
    after_wire_response_persisted: Callable[[], None] | None,
) -> tuple[_AttemptOutcome, int]:
    task_dir = prepared.task_directory
    attempt_dir = task_dir / "attempts" / f"{attempt_index:04d}"
    attempt_record_path = attempt_dir / "attempt.json"
    if attempt_record_path.exists():
        record = _load_record(
            attempt_record_path,
            LF022ExecutionAttemptRecord,
            label="execution attempt",
        )
        if (
            record.execution_task_id != prepared.task.execution_task_id
            or record.attempt_index != attempt_index
        ):
            raise LF022ExecutorError("persisted attempt task identity or attempt index differs")
        expected_request = _provider_request(prepared, attempt_index=attempt_index)
        persisted_request = _verify_attempt_artifacts(
            prepared=prepared,
            artifact_root=artifact_root,
            record=record,
        )
        if persisted_request != expected_request:
            raise LF022ExecutorError("persisted attempt request differs from exact prepared task")
        expected_request_path = attempt_dir / "provider_request.json"
        expected_wire_path = attempt_dir / "wire_request.json"
        expected_attempt_path = attempt_dir / "attempt.json"
        if (
            record.request_artifact != _relative(expected_request_path, artifact_root)
            or record.wire_request_artifact != _relative(expected_wire_path, artifact_root)
            or attempt_record_path != expected_attempt_path
        ):
            raise LF022ExecutorError("persisted attempt artifact paths are noncanonical")
        expected_wire = (
            canonical_json_bytes(
                make_chat_completion_payload(
                    model_id=prepared.admission.route.model_id,
                    rendered_prompt=prepared.prompt.text,
                    decoding=prepared.admission.route.decoding,
                )
            )
            + b"\n"
        )
        if expected_wire_path.read_bytes() != expected_wire:
            raise LF022ExecutorError(
                "persisted wire request differs from exact reviewed route contract"
            )
        batch: VariantProposalBatch | None = None
        if record.status == "response_parsed":
            response = _provider_raw(artifact_root=artifact_root, record=record)
            if response.status != "success" or response.output_text is None:
                raise LF022ExecutorError("parsed attempt lacks a successful provider raw response")
            batch = parse_variant_proposer_output(response.output_text)
            request_contract = prepared.task.prompt_request()
            if len(batch.variants) != request_contract.proposal_count or any(
                proposal.intended_relation not in request_contract.requested_relations
                for proposal in batch.variants
            ):
                raise LF022ExecutorError(
                    "recovered proposer output differs from frozen task request"
                )
        return (
            _AttemptOutcome(
                record=record,
                batch=batch,
                completion=None,
            ),
            0,
        )

    request = _provider_request(prepared, attempt_index=attempt_index)
    request_path = attempt_dir / "provider_request.json"
    persist_provider_request(request, request_path)
    wire_payload = make_chat_completion_payload(
        model_id=prepared.admission.route.model_id,
        rendered_prompt=prepared.prompt.text,
        decoding=prepared.admission.route.decoding,
    )
    wire_request_path = attempt_dir / "wire_request.json"
    _immutable(wire_request_path, canonical_json_bytes(wire_payload) + b"\n")

    existing_response = _load_wire_response(attempt_dir)
    if existing_response is None and (attempt_dir / ".transport_completed").exists():
        raise LF022ExecutorError(
            "transport-completed marker exists without persisted wire response"
        )
    network_calls = 0
    started_at = clock()
    if existing_response is None and wire_request_path.exists():
        # A request artifact written by this invocation is not ambiguous until
        # transport begins.  Existing request bytes from an earlier invocation
        # are ambiguous because a crash may have happened after send.
        prior_attempt = any(
            path.exists()
            for path in (
                attempt_dir / ".transport_started",
                attempt_dir / ".transport_completed",
            )
        )
        if prior_attempt and not (attempt_dir / ".transport_completed").exists():
            provider_result = _provider_result_from_error(
                task_dir=task_dir,
                request=request,
                error_code="transport_unknown",
            )
            completed_at = clock()
            record = _attempt_record(
                prepared=prepared,
                artifact_root=artifact_root,
                request=request,
                request_path=request_path,
                wire_request_path=wire_request_path,
                response_paths=None,
                provider_result=provider_result,
                status="transport_unknown",
                retryable=False,
                http_status=None,
                retry_after=None,
                error_code="transport_unknown",
                completion=None,
                started_at=started_at,
                completed_at=completed_at,
            )
            _immutable(attempt_record_path, _canonical_record(record))
            return _AttemptOutcome(record=record, batch=None, completion=None), 0

        _immutable(attempt_dir / ".transport_started", b"started\n")
        try:
            existing_response = transport.post_json(
                url=credentials.base_url.rstrip("/") + "/chat/completions",
                api_key=credentials.api_key,
                payload=wire_payload,
                timeout_seconds=prepared.admission.retry_policy.request_timeout_seconds,
            )
            network_calls = 1
        except RCPTransportUnknownError:
            provider_result = _provider_result_from_error(
                task_dir=task_dir,
                request=request,
                error_code="transport_unknown",
            )
            completed_at = clock()
            record = _attempt_record(
                prepared=prepared,
                artifact_root=artifact_root,
                request=request,
                request_path=request_path,
                wire_request_path=wire_request_path,
                response_paths=None,
                provider_result=provider_result,
                status="transport_unknown",
                retryable=False,
                http_status=None,
                retry_after=None,
                error_code="transport_unknown",
                completion=None,
                started_at=started_at,
                completed_at=completed_at,
            )
            _immutable(attempt_record_path, _canonical_record(record))
            return _AttemptOutcome(record=record, batch=None, completion=None), 1
        response_paths = _wire_response(
            attempt_dir=attempt_dir,
            response=existing_response,
        )
        _immutable(attempt_dir / ".transport_completed", b"completed\n")
        if after_wire_response_persisted is not None:
            after_wire_response_persisted()
    else:
        assert existing_response is not None
        response_paths = (
            attempt_dir / "wire_response.body",
            hash_file(attempt_dir / "wire_response.body"),
            attempt_dir / "wire_response.json",
            hash_file(attempt_dir / "wire_response.json"),
        )

    completed_at = clock()
    http_error = classify_http_response(
        existing_response,
        policy=prepared.admission.retry_policy,
        now=completed_at,
    )
    if http_error is not None:
        provider_result = _provider_result_from_error(
            task_dir=task_dir,
            request=request,
            error_code=http_error.code,
        )
        status: AttemptStatus = (
            "retryable_http_error" if http_error.retryable else "terminal_http_error"
        )
        record = _attempt_record(
            prepared=prepared,
            artifact_root=artifact_root,
            request=request,
            request_path=request_path,
            wire_request_path=wire_request_path,
            response_paths=response_paths,
            provider_result=provider_result,
            status=status,
            retryable=http_error.retryable,
            http_status=http_error.http_status,
            retry_after=http_error.retry_after_seconds,
            error_code=http_error.code,
            completion=None,
            started_at=started_at,
            completed_at=completed_at,
        )
        _immutable(attempt_record_path, _canonical_record(record))
        return _AttemptOutcome(record=record, batch=None, completion=None), network_calls

    try:
        completion = parse_chat_completion(
            existing_response.body,
            expected_model=prepared.admission.route.model_id,
        )
    except RCPResponseError as exc:
        provider_result = _provider_result_from_error(
            task_dir=task_dir,
            request=request,
            error_code=exc.code,
        )
        record = _attempt_record(
            prepared=prepared,
            artifact_root=artifact_root,
            request=request,
            request_path=request_path,
            wire_request_path=wire_request_path,
            response_paths=response_paths,
            provider_result=provider_result,
            status="invalid_response",
            retryable=False,
            http_status=200,
            retry_after=None,
            error_code=exc.code,
            completion=None,
            started_at=started_at,
            completed_at=completed_at,
        )
        _immutable(attempt_record_path, _canonical_record(record))
        return _AttemptOutcome(record=record, batch=None, completion=None), network_calls

    provider_result = persist_provider_raw_response(
        task_dir / "provider_raw",
        ProviderRawResponse.success(request, completion.content),
    )
    try:
        batch = parse_variant_proposer_output(completion.content)
        # Enforce count/relation agreement at the same boundary as materialization.
        request_contract = prepared.task.prompt_request()
        if len(batch.variants) != request_contract.proposal_count or any(
            proposal.intended_relation not in request_contract.requested_relations
            for proposal in batch.variants
        ):
            raise VariantOutputParseError(
                code=VariantOutputErrorCode.REQUEST_MISMATCH,
                detail="parsed variants differ from the frozen task request",
            )
    except (VariantOutputParseError, ValueError) as exc:
        record = _attempt_record(
            prepared=prepared,
            artifact_root=artifact_root,
            request=request,
            request_path=request_path,
            wire_request_path=wire_request_path,
            response_paths=response_paths,
            provider_result=provider_result,
            status="proposer_parse_failed",
            retryable=False,
            http_status=200,
            retry_after=None,
            error_code=(
                exc.code.value if isinstance(exc, VariantOutputParseError) else "request_mismatch"
            ),
            completion=completion,
            started_at=started_at,
            completed_at=completed_at,
        )
        _immutable(attempt_record_path, _canonical_record(record))
        return _AttemptOutcome(record=record, batch=None, completion=completion), network_calls

    record = _attempt_record(
        prepared=prepared,
        artifact_root=artifact_root,
        request=request,
        request_path=request_path,
        wire_request_path=wire_request_path,
        response_paths=response_paths,
        provider_result=provider_result,
        status="response_parsed",
        retryable=False,
        http_status=200,
        retry_after=None,
        error_code=None,
        completion=completion,
        started_at=started_at,
        completed_at=completed_at,
    )
    _immutable(attempt_record_path, _canonical_record(record))
    return _AttemptOutcome(record=record, batch=batch, completion=completion), network_calls


def _provider_raw(
    *,
    artifact_root: Path,
    record: LF022ExecutionAttemptRecord,
) -> ProviderRawResponse:
    path = _artifact_path(
        artifact_root,
        record.provider_raw_artifact,
        label="provider_raw_artifact",
    )
    raw = path.read_bytes()
    try:
        response = ProviderRawResponse.model_validate_json(raw)
    except ValueError as exc:
        raise LF022ExecutorError(f"invalid provider raw response: {exc}") from exc
    if raw != _canonical_record(response) or hash_file(path) != record.provider_raw_sha256:
        raise LF022ExecutorError("provider raw response bytes differ from attempt record")
    return response


def _historical_response_error_matches(*, recorded: str, observed: str) -> bool:
    """Accept one versioned parser refinement without rewriting old artifacts."""

    return recorded == observed or (
        recorded == "empty_response" and observed == "output_budget_exhausted"
    )


def _verify_attempt_artifacts(
    *,
    prepared: PreparedLF022Execution,
    artifact_root: Path,
    record: LF022ExecutionAttemptRecord,
) -> ProviderRequest:
    """Verify every byte binding carried by one terminal attempt record."""

    request_path = _artifact_path(
        artifact_root,
        record.request_artifact,
        label="request_artifact",
    )
    if hash_file(request_path) != record.request_sha256:
        raise LF022ExecutorError("provider request hash differs from attempt record")
    request = load_provider_request(request_path)
    expected_request = _provider_request(prepared, attempt_index=record.attempt_index)
    if (
        request.request_hash != record.provider_request_hash
        or request.attempt_id != record.provider_attempt_id
        or request.attempt_index != record.attempt_index
        or request != expected_request
    ):
        raise LF022ExecutorError("provider request identity differs from attempt record")

    wire_request_path = _artifact_path(
        artifact_root,
        record.wire_request_artifact,
        label="wire_request_artifact",
    )
    if hash_file(wire_request_path) != record.wire_request_sha256:
        raise LF022ExecutorError("wire request hash differs from attempt record")
    expected_wire = (
        canonical_json_bytes(
            make_chat_completion_payload(
                model_id=prepared.admission.route.model_id,
                rendered_prompt=prepared.prompt.text,
                decoding=prepared.admission.route.decoding,
            )
        )
        + b"\n"
    )
    if wire_request_path.read_bytes() != expected_wire:
        raise LF022ExecutorError("wire request differs from exact reviewed route contract")

    response_paths = (
        record.wire_response_body_artifact,
        record.wire_response_body_sha256,
        record.wire_response_metadata_artifact,
        record.wire_response_metadata_sha256,
    )
    if response_paths[0] is not None:
        body_artifact, body_sha, metadata_artifact, metadata_sha = response_paths
        assert body_artifact is not None
        assert body_sha is not None
        assert metadata_artifact is not None
        assert metadata_sha is not None
        body_path = _artifact_path(
            artifact_root,
            body_artifact,
            label="wire_response_body_artifact",
        )
        metadata_path = _artifact_path(
            artifact_root,
            metadata_artifact,
            label="wire_response_metadata_artifact",
        )
        if hash_file(body_path) != body_sha:
            raise LF022ExecutorError("wire response body hash differs from attempt record")
        if hash_file(metadata_path) != metadata_sha:
            raise LF022ExecutorError("wire response metadata hash differs from attempt record")
        metadata = _load_record(
            metadata_path,
            LF022WireResponseMetadata,
            label="replayed wire response metadata",
        )
        if metadata.body_sha256 != body_sha or metadata.status_code != record.http_status:
            raise LF022ExecutorError("wire response metadata differs from attempt record")

    response = _provider_raw(artifact_root=artifact_root, record=record)
    if (
        response.request_hash != request.request_hash
        or response.attempt_id != request.attempt_id
        or response.attempt_index != request.attempt_index
        or response.provider != request.provider
        or response.model != request.model
        or response.revision != request.revision
        or response.prompt_template_hash != request.prompt_template_hash
        or response.prompt_render_hash != request.prompt_render_hash
        or response.decoding_hash != request.decoding_hash
    ):
        raise LF022ExecutorError("provider raw response differs from persisted request")
    if record.status in {"response_parsed", "proposer_parse_failed"}:
        assert response_paths[0] is not None
        body_path = _artifact_path(
            artifact_root,
            response_paths[0],
            label="wire response body artifact",
        )
        try:
            completion = parse_chat_completion(
                body_path.read_bytes(),
                expected_model=prepared.admission.route.model_id,
            )
        except RCPResponseError as exc:
            raise LF022ExecutorError(
                "persisted successful attempt has an invalid wire response"
            ) from exc
        if (
            response.status != "success"
            or response.output_text != completion.content
            or record.http_status != 200
            or record.provider_request_id != completion.provider_request_id
            or record.returned_model != completion.returned_model
            or record.tokens != completion.usage
        ):
            raise LF022ExecutorError("provider raw success differs from persisted wire response")
    else:
        if response.status != "error" or response.error_type != record.error_code:
            raise LF022ExecutorError("provider raw error differs from persisted attempt status")
        if record.status == "invalid_response":
            assert record.error_code is not None
            assert response_paths[0] is not None
            body_path = _artifact_path(
                artifact_root,
                response_paths[0],
                label="wire response body artifact",
            )
            try:
                parse_chat_completion(
                    body_path.read_bytes(),
                    expected_model=prepared.admission.route.model_id,
                )
            except RCPResponseError as exc:
                if not _historical_response_error_matches(
                    recorded=record.error_code,
                    observed=exc.code,
                ):
                    raise LF022ExecutorError(
                        "invalid-response code differs from persisted wire response"
                    ) from exc
            else:
                raise LF022ExecutorError(
                    "invalid-response attempt contains a valid wire completion"
                )
    return request


def _llm_status_for_attempt(
    record: LF022ExecutionAttemptRecord,
) -> LLMAttemptStatus:
    if record.status in {
        "response_parsed",
        "proposer_parse_failed",
        "invalid_response",
    }:
        return LLMAttemptStatus.RESPONSE_RECEIVED
    if record.status == "transport_unknown":
        return LLMAttemptStatus.INFRASTRUCTURE_ERROR
    return LLMAttemptStatus.PROVIDER_ERROR


def _build_lineage(
    *,
    prepared: PreparedLF022Execution,
    artifact_root: Path,
    attempt_records: tuple[LF022ExecutionAttemptRecord, ...],
    final_batch: VariantProposalBatch | None,
    persist: bool = True,
) -> tuple[
    LLMCallRecord,
    tuple[LLMAttemptRecord, ...],
    tuple[str, ...],
    tuple[str, ...],
    Path,
    str,
]:
    admission = prepared.admission
    task = prepared.task
    final_record = attempt_records[-1]
    final_request_path = _artifact_path(
        artifact_root,
        final_record.request_artifact,
        label="final request artifact",
    )
    final_request = load_provider_request(final_request_path)
    call_id = make_llm_call_id(
        provider=admission.route.provider_id,
        provider_slot=admission.route.proposer_family_id,
        model=admission.route.model_id,
        model_family=admission.route.canonical_family,
        model_revision=admission.route.route_snapshot_revision,
        role=LLMRole.PROPOSER,
        problem_record_id=None,
        prompt_template_hash=prepared.prompt.template_sha256,
        prompt_render_hash=prepared.prompt.render_sha256,
        input_ids=final_request.input_ids,
        decoding=final_request.decoding,
    )
    llm_attempts: list[LLMAttemptRecord] = []
    for index, record in enumerate(attempt_records):
        response = _provider_raw(artifact_root=artifact_root, record=record)
        llm_status = _llm_status_for_attempt(record)
        llm_attempts.append(
            LLMAttemptRecord(
                attempt_id=make_llm_attempt_id(call_id, index),
                call_id=call_id,
                attempt_index=index,
                execution_mode="external",
                started_at=record.started_at,
                completed_at=record.completed_at,
                request_artifact=record.request_artifact,
                raw_response_artifact=record.provider_raw_artifact,
                status=llm_status,
                provider_request_id=record.provider_request_id,
                error_code=(
                    None if llm_status is LLMAttemptStatus.RESPONSE_RECEIVED else record.error_code
                ),
                error_detail=None,
                retryable=record.retryable,
                latency_ms=max(
                    0,
                    int((record.completed_at - record.started_at).total_seconds() * 1000),
                ),
                tokens=record.tokens,
                provider_request_hash=record.provider_request_hash,
                provider_attempt_id=record.provider_attempt_id,
                request_artifact_sha256=record.request_sha256,
                raw_response_sha256=record.provider_raw_sha256,
                metadata={
                    "lf022_execution_task_id": task.execution_task_id,
                    "wire_request_sha256": record.wire_request_sha256,
                    "wire_response_body_sha256": record.wire_response_body_sha256,
                    "task_parse_error_code": (
                        record.error_code
                        if llm_status is LLMAttemptStatus.RESPONSE_RECEIVED
                        else None
                    ),
                },
            )
        )
        # Keep a strong read binding even though the typed record above already
        # revalidates the canonical provider response.
        if response.request_hash != record.provider_request_hash:
            raise LF022ExecutorError("provider raw response request binding differs")

    final_success = final_record.status in {
        "response_parsed",
        "proposer_parse_failed",
        "invalid_response",
    }
    parse_status = (
        ParseStatus.PARSED if final_record.status == "response_parsed" else ParseStatus.EMPTY
    )
    parsed_output = final_batch.model_dump(mode="json") if final_batch is not None else None
    generation_config_hash = hash_canonical(admission.model_dump(mode="json"))
    call = LLMCallRecord(
        schema_version=2,
        call_id=call_id,
        provider=admission.route.provider_id,
        provider_slot=admission.route.proposer_family_id,
        model=admission.route.model_id,
        model_family=admission.route.canonical_family,
        role=LLMRole.PROPOSER,
        model_revision=admission.route.route_snapshot_revision,
        request_date=attempt_records[0].started_at,
        started_at=attempt_records[0].started_at,
        completed_at=attempt_records[-1].completed_at,
        execution_mode="external",
        prompt_template_id=prepared.prompt.template_id,
        prompt_template_version=prepared.prompt.template_version,
        prompt_template_hash=prepared.prompt.template_sha256,
        prompt_render_hash=prepared.prompt.render_sha256,
        request_artifact=final_record.request_artifact,
        input_ids=final_request.input_ids,
        decoding=final_request.decoding,
        raw_output_artifact=final_record.provider_raw_artifact,
        parsed_output=parsed_output,
        parse_status=parse_status,
        retry_count=len(attempt_records) - 1,
        tokens=final_record.tokens,
        supervision_eligible=False,
        private_source_content=False,
        denylist_checked=True,
        denylist_hits=(),
        terminal_status=(LLMCallStatus.COMPLETED if final_success else LLMCallStatus.EXHAUSTED),
        attempt_ids=tuple(attempt.attempt_id for attempt in llm_attempts),
        latency_ms=max(
            0,
            int(
                (attempt_records[-1].completed_at - attempt_records[0].started_at).total_seconds()
                * 1000
            ),
        ),
        provider_request_hash=final_record.provider_request_hash,
        request_artifact_sha256=final_record.request_sha256,
        raw_response_sha256=final_record.provider_raw_sha256,
        metadata={
            "provider_protocol": "provider_v1",
            "provider_request_hash": final_record.provider_request_hash,
            "provider_attempt_id": final_record.provider_attempt_id,
            "request_artifact_sha256": final_record.request_sha256,
            "raw_response_sha256": final_record.provider_raw_sha256,
            "generation_distribution": "G_open",
            "generation_config_hash": generation_config_hash,
            "lf022_execution_admission_id": admission.admission_id,
            "lf022_execution_task_id": task.execution_task_id,
            "semantic_labels_created": False,
            "silver_promotion_enabled": False,
            "training_eligible": False,
            "evaluation_eligible": False,
        },
    )
    violations = check_llm_call_attempt_lineage(call, tuple(llm_attempts))
    if violations:
        raise LF022ExecutorError(
            "LLM call/attempt lineage is inconsistent: " + ", ".join(violations)
        )
    attempts_dir = prepared.task_directory / "llm_attempts"
    llm_attempt_paths: list[str] = []
    llm_attempt_hashes: list[str] = []
    for attempt in llm_attempts:
        path = attempts_dir / f"{attempt.attempt_index:04d}.json"
        payload = _canonical_record(attempt)
        digest = _immutable(path, payload) if persist else sha256_hex(payload)
        llm_attempt_paths.append(_relative(path, artifact_root))
        llm_attempt_hashes.append(digest)
    call_path = prepared.task_directory / "llm_call.json"
    call_payload = _canonical_record(call)
    call_sha = _immutable(call_path, call_payload) if persist else sha256_hex(call_payload)
    return (
        call,
        tuple(llm_attempts),
        tuple(llm_attempt_paths),
        tuple(llm_attempt_hashes),
        call_path,
        call_sha,
    )


def _terminal_record(
    *,
    prepared: PreparedLF022Execution,
    artifact_root: Path,
    attempt_records: tuple[LF022ExecutionAttemptRecord, ...],
    llm_attempt_paths: tuple[str, ...],
    llm_attempt_hashes: tuple[str, ...],
    call: LLMCallRecord,
    call_path: Path,
    call_sha: str,
    variants: tuple[VariantRecord, ...],
    variants_path: Path | None,
    variants_sha: str | None,
) -> LF022ExecutionTerminalRecord:
    final = attempt_records[-1]
    if variants:
        status: TerminalStatus = "provisional_variants_created"
        error_code = None
    elif final.status == "proposer_parse_failed":
        status = "proposer_parse_failed"
        error_code = final.error_code
    elif final.status == "transport_unknown":
        status = "transport_unknown"
        error_code = final.error_code
    else:
        status = "provider_exhausted"
        error_code = final.error_code
    attempt_paths = tuple(
        _relative(
            prepared.task_directory / "attempts" / f"{record.attempt_index:04d}" / "attempt.json",
            artifact_root,
        )
        for record in attempt_records
    )
    attempt_hashes = tuple(hash_file(artifact_root / path) for path in attempt_paths)
    payload: dict[str, object] = {
        "schema_version": 1,
        "execution_admission_id": prepared.admission.admission_id,
        "execution_task_id": prepared.task.execution_task_id,
        "status": status,
        "attempt_artifacts": list(attempt_paths),
        "attempt_sha256s": list(attempt_hashes),
        "llm_attempt_artifacts": list(llm_attempt_paths),
        "llm_attempt_sha256s": list(llm_attempt_hashes),
        "llm_call_id": call.call_id,
        "llm_call_artifact": _relative(call_path, artifact_root),
        "llm_call_sha256": call_sha,
        "variants_artifact": (
            _relative(variants_path, artifact_root) if variants_path is not None else None
        ),
        "variants_sha256": variants_sha,
        "provisional_variant_count": len(variants),
        "terminal_error_code": error_code,
        "raw_before_parse_verified": True,
        "exact_replay_supported": True,
        "output_quality_tier": "provisional",
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022ExecutionTerminalRecord.model_validate(
        {
            **payload,
            "terminal_id": make_id("lf022_execution_terminal", payload),
        }
    )


def _finalize(
    *,
    prepared: PreparedLF022Execution,
    artifact_root: Path,
    attempt_records: tuple[LF022ExecutionAttemptRecord, ...],
    batch: VariantProposalBatch | None,
) -> tuple[LF022ExecutionTerminalRecord, Path]:
    (
        call,
        _,
        llm_attempt_paths,
        llm_attempt_hashes,
        call_path,
        call_sha,
    ) = _build_lineage(
        prepared=prepared,
        artifact_root=artifact_root,
        attempt_records=attempt_records,
        final_batch=batch,
    )
    variants: tuple[VariantRecord, ...] = ()
    variants_path: Path | None = None
    variants_sha: str | None = None
    if batch is not None:
        generation_config_hash = hash_canonical(prepared.admission.model_dump(mode="json"))
        variants = materialize_verified_provisional_variants(
            request=prepared.task.prompt_request(),
            call=call,
            artifact_root=artifact_root,
            generation_config_hash=generation_config_hash,
            template_path=artifact_root / prepared.admission.artifacts.prompt_template.path,
        )
        if any(
            variant.quality_tier is not QualityTier.PROVISIONAL
            or variant.validation_status is not ValidationStatus.UNVALIDATED
            for variant in variants
        ):
            raise LF022ExecutorError("executor may emit only unvalidated provisional variants")
        variants_path = prepared.task_directory / "provisional_variants.jsonl"
        variants_sha = _immutable(
            variants_path,
            b"".join(_canonical_record(variant) for variant in variants),
        )
    terminal = _terminal_record(
        prepared=prepared,
        artifact_root=artifact_root,
        attempt_records=attempt_records,
        llm_attempt_paths=llm_attempt_paths,
        llm_attempt_hashes=llm_attempt_hashes,
        call=call,
        call_path=call_path,
        call_sha=call_sha,
        variants=variants,
        variants_path=variants_path,
        variants_sha=variants_sha,
    )
    terminal_path = prepared.task_directory / "terminal.json"
    _immutable(terminal_path, _canonical_record(terminal))
    return terminal, terminal_path


def replay_lf022_g_open_terminal(
    *,
    prepared: PreparedLF022Execution,
    artifact_root: Path,
) -> tuple[LF022ExecutionTerminalRecord, Path]:
    """Verify the terminal record and all provisional output bindings offline."""

    terminal_path = prepared.task_directory / "terminal.json"
    terminal = _load_record(
        terminal_path,
        LF022ExecutionTerminalRecord,
        label="execution terminal",
    )
    if (
        terminal.execution_admission_id != prepared.admission.admission_id
        or terminal.execution_task_id != prepared.task.execution_task_id
    ):
        raise LF022ExecutorError("terminal record differs from prepared task")
    attempts_root = prepared.task_directory / "attempts"
    discovered_attempt_names = sorted(
        path.parent.name
        for path in attempts_root.glob("*/attempt.json")
        if path.is_file() and not path.is_symlink()
    )
    expected_attempt_names = [f"{index:04d}" for index in range(len(terminal.attempt_artifacts))]
    if discovered_attempt_names != expected_attempt_names:
        raise LF022ExecutorError(
            "terminal attempt list does not cover the canonical persisted attempts"
        )
    attempt_records: list[LF022ExecutionAttemptRecord] = []
    for expected_index, (path, expected_hash) in enumerate(
        zip(
            terminal.attempt_artifacts,
            terminal.attempt_sha256s,
            strict=True,
        )
    ):
        attempt_path = _artifact_path(
            artifact_root,
            path,
            label="terminal attempt artifact",
        )
        expected_path = (
            prepared.task_directory / "attempts" / f"{expected_index:04d}" / "attempt.json"
        )
        if (
            path != _relative(expected_path, artifact_root)
            or hash_file(attempt_path) != expected_hash
        ):
            raise LF022ExecutorError("attempt artifact hash drifted")
        record = _load_record(attempt_path, LF022ExecutionAttemptRecord, label="replayed attempt")
        if (
            record.execution_task_id != prepared.task.execution_task_id
            or record.attempt_index != expected_index
        ):
            raise LF022ExecutorError("replayed attempt task identity or index differs")
        _verify_attempt_artifacts(
            prepared=prepared,
            artifact_root=artifact_root,
            record=record,
        )
        attempt_records.append(record)

    verified_final_batch: VariantProposalBatch | None = None
    if attempt_records[-1].status == "response_parsed":
        response = _provider_raw(
            artifact_root=artifact_root,
            record=attempt_records[-1],
        )
        if response.status != "success" or response.output_text is None:
            raise LF022ExecutorError("parsed attempt lacks a successful provider raw response")
        verified_final_batch = parse_variant_proposer_output(response.output_text)
        request_contract = prepared.task.prompt_request()
        if len(verified_final_batch.variants) != request_contract.proposal_count or any(
            proposal.intended_relation not in request_contract.requested_relations
            for proposal in verified_final_batch.variants
        ):
            raise LF022ExecutorError("replayed proposer output differs from frozen task request")

    # Rebuild the complete generic LLM lineage from the independently verified
    # execution attempts without trusting the persisted generic records.  The
    # expected canonical bytes and hashes must match exactly, including policy-
    # bearing fields that are deliberately outside the logical call ID.
    (
        expected_call,
        expected_llm_attempts,
        expected_llm_paths,
        expected_llm_hashes,
        expected_call_path,
        expected_call_sha,
    ) = _build_lineage(
        prepared=prepared,
        artifact_root=artifact_root,
        attempt_records=tuple(attempt_records),
        final_batch=verified_final_batch,
        persist=False,
    )

    call_path = _artifact_path(
        artifact_root,
        terminal.llm_call_artifact,
        label="terminal LLM call artifact",
    )
    if (
        terminal.llm_call_artifact != _relative(expected_call_path, artifact_root)
        or terminal.llm_call_sha256 != expected_call_sha
        or hash_file(call_path) != expected_call_sha
    ):
        raise LF022ExecutorError("LLM call artifact hash drifted")
    call = _load_record(call_path, LLMCallRecord, label="replayed LLM call")
    if call != expected_call or call.call_id != terminal.llm_call_id:
        raise LF022ExecutorError("terminal LLM call differs from exact reconstruction")
    llm_attempts: list[LLMAttemptRecord] = []
    llm_attempts_root = prepared.task_directory / "llm_attempts"
    discovered_llm_names = sorted(
        path.name
        for path in llm_attempts_root.glob("*.json")
        if path.is_file() and not path.is_symlink()
    )
    expected_llm_names = [f"{record.attempt_index:04d}.json" for record in attempt_records]
    if discovered_llm_names != expected_llm_names:
        raise LF022ExecutorError(
            "terminal LLM-attempt list does not cover canonical persisted attempts"
        )
    for (
        record,
        expected_llm_attempt,
        expected_llm_path,
        expected_llm_sha,
        llm_artifact,
        llm_sha,
    ) in zip(
        attempt_records,
        expected_llm_attempts,
        expected_llm_paths,
        expected_llm_hashes,
        terminal.llm_attempt_artifacts,
        terminal.llm_attempt_sha256s,
        strict=True,
    ):
        llm_attempt_path = _artifact_path(
            artifact_root,
            llm_artifact,
            label="replayed LLM attempt artifact",
        )
        if (
            llm_artifact != expected_llm_path
            or llm_sha != expected_llm_sha
            or hash_file(llm_attempt_path) != expected_llm_sha
        ):
            raise LF022ExecutorError("LLM attempt artifact path or hash drifted")
        llm_attempt = _load_record(
            llm_attempt_path,
            LLMAttemptRecord,
            label="replayed LLM attempt",
        )
        if llm_attempt != expected_llm_attempt:
            raise LF022ExecutorError("replayed LLM attempt differs from exact reconstruction")
        expected_error = (
            None
            if _llm_status_for_attempt(record) is LLMAttemptStatus.RESPONSE_RECEIVED
            else record.error_code
        )
        if (
            llm_attempt.attempt_index != record.attempt_index
            or llm_attempt.status is not _llm_status_for_attempt(record)
            or llm_attempt.request_artifact != record.request_artifact
            or llm_attempt.raw_response_artifact != record.provider_raw_artifact
            or llm_attempt.provider_request_hash != record.provider_request_hash
            or llm_attempt.provider_attempt_id != record.provider_attempt_id
            or llm_attempt.request_artifact_sha256 != record.request_sha256
            or llm_attempt.raw_response_sha256 != record.provider_raw_sha256
            or llm_attempt.error_code != expected_error
            or llm_attempt.retryable != record.retryable
        ):
            raise LF022ExecutorError("replayed LLM attempt differs from execution attempt")
        llm_attempts.append(llm_attempt)
    violations = check_llm_call_attempt_lineage(call, tuple(llm_attempts))
    if violations:
        raise LF022ExecutorError(
            "replayed LLM call/attempt lineage is inconsistent: " + ", ".join(violations)
        )

    final_attempt = attempt_records[-1]
    expected_variants: tuple[VariantRecord, ...] = ()
    if final_attempt.status == "response_parsed":
        expected_status: TerminalStatus = "provisional_variants_created"
        expected_terminal_error = None
        assert verified_final_batch is not None
        expected_variants = materialize_verified_provisional_variants(
            request=prepared.task.prompt_request(),
            call=expected_call,
            artifact_root=artifact_root,
            generation_config_hash=hash_canonical(prepared.admission.model_dump(mode="json")),
            template_path=artifact_root / prepared.admission.artifacts.prompt_template.path,
        )
    elif final_attempt.status == "proposer_parse_failed":
        expected_status = "proposer_parse_failed"
        expected_terminal_error = final_attempt.error_code
    elif final_attempt.status == "transport_unknown":
        expected_status = "transport_unknown"
        expected_terminal_error = final_attempt.error_code
    else:
        expected_status = "provider_exhausted"
        expected_terminal_error = final_attempt.error_code
    variants_expected = bool(expected_variants)
    if (
        terminal.status != expected_status
        or terminal.terminal_error_code != expected_terminal_error
        or terminal.provisional_variant_count != len(expected_variants)
        or (terminal.variants_artifact is not None) != variants_expected
        or (terminal.variants_sha256 is not None) != variants_expected
    ):
        raise LF022ExecutorError(
            "terminal semantics differ from verified attempts and proposer call"
        )

    reconstructed_variants_path: Path | None
    reconstructed_variants_sha: str | None
    if terminal.variants_artifact is not None:
        canonical_variants_path = prepared.task_directory / "provisional_variants.jsonl"
        if terminal.variants_artifact != _relative(canonical_variants_path, artifact_root):
            raise LF022ExecutorError("terminal variants artifact path is noncanonical")
        variants_path = _artifact_path(
            artifact_root,
            terminal.variants_artifact,
            label="terminal variants artifact",
        )
        assert terminal.variants_sha256 is not None
        if hash_file(variants_path) != terminal.variants_sha256:
            raise LF022ExecutorError("provisional variant artifact hash drifted")
        lines = variants_path.read_bytes().splitlines(keepends=True)
        if len(lines) != terminal.provisional_variant_count:
            raise LF022ExecutorError("provisional variant count drifted")
        variants = tuple(VariantRecord.model_validate_json(line) for line in lines)
        if b"".join(_canonical_record(variant) for variant in variants) != b"".join(lines):
            raise LF022ExecutorError("provisional variant JSONL is not canonical")
        if variants != expected_variants:
            raise LF022ExecutorError("replayed variants differ from verified raw response")
        if any(variant.quality_tier is not QualityTier.PROVISIONAL for variant in variants):
            raise LF022ExecutorError("replayed output contains a promoted variant")
        reconstructed_variants_path = variants_path
        reconstructed_variants_sha = hash_file(variants_path)
    else:
        reconstructed_variants_path = None
        reconstructed_variants_sha = None

    reconstructed_terminal = _terminal_record(
        prepared=prepared,
        artifact_root=artifact_root,
        attempt_records=tuple(attempt_records),
        llm_attempt_paths=expected_llm_paths,
        llm_attempt_hashes=expected_llm_hashes,
        call=expected_call,
        call_path=expected_call_path,
        call_sha=expected_call_sha,
        variants=expected_variants,
        variants_path=reconstructed_variants_path,
        variants_sha=reconstructed_variants_sha,
    )
    if terminal != reconstructed_terminal:
        raise LF022ExecutorError(
            "terminal record is not the exact reconstruction of persisted lineage"
        )
    return terminal, terminal_path


def execute_lf022_g_open_task(
    *,
    repo_root: Path,
    output_root: Path,
    admission: LF022GOpenExecutionAdmission,
    task: LF022GOpenExecutionTask,
    execute_public_provisional: bool = False,
    credentials: RCPRuntimeCredentials | None = None,
    transport: RCPHTTPTransport | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], datetime.datetime] | None = None,
    after_wire_response_persisted: Callable[[], None] | None = None,
    verified_admission: VerifiedLF022ExecutionAdmission | None = None,
    verified_task_inputs: VerifiedLF022ExecutionTaskInputs | None = None,
    observed_code_tree_hash: str | None = None,
) -> LF022ExecutionResult:
    """Preflight by default; execute only with the explicit live flag."""

    prepared = prepare_lf022_g_open_execution(
        repo_root=repo_root,
        output_root=output_root,
        admission=admission,
        task=task,
        verified_admission=verified_admission,
        verified_task_inputs=verified_task_inputs,
        observed_code_tree_hash=observed_code_tree_hash,
    )
    terminal_path = prepared.task_directory / "terminal.json"
    if terminal_path.exists():
        terminal, path = replay_lf022_g_open_terminal(
            prepared=prepared,
            artifact_root=repo_root,
        )
        return LF022ExecutionResult(
            preflight=prepared.preflight,
            terminal=terminal,
            terminal_path=path,
            replayed=True,
            network_calls_this_run=0,
        )
    if not execute_public_provisional:
        return LF022ExecutionResult(
            preflight=prepared.preflight,
            terminal=None,
            terminal_path=None,
            replayed=False,
            network_calls_this_run=0,
        )
    if (
        admission.route.proposer_family_id == "moonshot_kimi_k2"
        and admission.route.decoding.contract_id == "kimi_k2_7_public_smoke_v3"
    ):
        verified = verified_admission
        if verified is None:
            try:
                verified = verify_lf022_execution_admission(
                    repo_root=repo_root,
                    admission=admission,
                )
            except LF022ExecutionError as exc:
                raise LF022ExecutorError(
                    f"cannot determine Kimi-v3 admission profile: {exc}"
                ) from exc
        if verified.audit.profile == "scientific_production_scaffold":
            raise LF022ExecutorError(
                "live Kimi-v3 scientific execution is archived after the failed "
                "prefix-256 audit; use offline replay while Kimi-v4 remains unqualified"
            )
    if credentials is None or transport is None:
        raise LF022ExecutorError(
            "explicit live execution requires runtime credentials and a transport"
        )
    if credentials.base_url.rstrip("/") != "https://inference.rcp.epfl.ch/v1":
        raise LF022ExecutorError("RCP base URL differs from the admitted EPFL endpoint")
    if not credentials.api_key:
        raise LF022ExecutorError("RCP API key is empty")
    sleep = sleeper or time.sleep
    now = clock or (lambda: datetime.datetime.now(tz=datetime.UTC))

    prepared.task_directory.mkdir(parents=True, exist_ok=True)
    lock_path = prepared.task_directory / ".lock"
    if lock_path.is_symlink():
        raise LF022ExecutorError("execution lock path cannot be a symlink")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise LF022TaskLockedError("execution task is already locked") from exc
    except BaseException:
        os.close(descriptor)
        raise
    network_calls = 0
    try:
        if terminal_path.exists():
            terminal, path = replay_lf022_g_open_terminal(
                prepared=prepared,
                artifact_root=repo_root,
            )
            return LF022ExecutionResult(
                preflight=prepared.preflight,
                terminal=terminal,
                terminal_path=path,
                replayed=True,
                network_calls_this_run=0,
            )
        attempts: list[LF022ExecutionAttemptRecord] = []
        final_batch: VariantProposalBatch | None = None
        for attempt_index in range(admission.retry_policy.max_attempts):
            outcome, calls = _execute_or_recover_attempt(
                prepared=prepared,
                artifact_root=repo_root,
                credentials=credentials,
                transport=transport,
                attempt_index=attempt_index,
                clock=now,
                after_wire_response_persisted=after_wire_response_persisted,
            )
            network_calls += calls
            attempts.append(outcome.record)
            final_batch = outcome.batch
            if outcome.record.status == "retryable_http_error" and (
                attempt_index + 1 < admission.retry_policy.max_attempts
            ):
                sleep(
                    retry_delay_seconds(
                        policy=admission.retry_policy,
                        attempt_index=attempt_index,
                        retry_after=outcome.record.retry_after_seconds,
                    )
                )
                continue
            break
        if not attempts:
            raise LF022ExecutorError("executor produced no terminal attempt")
        terminal, path = _finalize(
            prepared=prepared,
            artifact_root=repo_root,
            attempt_records=tuple(attempts),
            batch=final_batch,
        )
        return LF022ExecutionResult(
            preflight=prepared.preflight,
            terminal=terminal,
            terminal_path=path,
            replayed=False,
            network_calls_this_run=network_calls,
        )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


__all__ = [
    "LF022ExecutionAttemptRecord",
    "LF022ExecutionPreflight",
    "LF022ExecutionResult",
    "LF022ExecutionTerminalRecord",
    "LF022ExecutorError",
    "LF022LiveExecutionRequired",
    "LF022TaskLockedError",
    "RCPRuntimeCredentials",
    "execute_lf022_g_open_task",
    "prepare_lf022_g_open_execution",
    "replay_lf022_g_open_terminal",
]
