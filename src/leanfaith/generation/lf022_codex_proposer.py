"""Opt-in, resumable Codex proposer for frozen public LF-022 tasks.

The RCP executor remains the scientific LF-022 collection path.  This module
adds a deliberately narrow Codex ``exec`` adapter for high-value public
proposals.  It consumes an already frozen LF-022 public batch task, renders the
reviewed proposer prompt, persists raw process/provider artifacts before
parsing, and materializes the existing unvalidated ``VariantRecord`` schema.

Nothing emitted here is a semantic label, silver record, training example,
evaluation example, or gate artifact.  A separate-family validation/admission
stage is required before any downstream scientific use.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import mmap
import os
import shutil
import signal
import subprocess
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import DenylistIndex, FrozenRegistry
from leanfaith.generation.lf022_batch import (
    LF022BatchFreezeRequest,
    LF022PublicBatchManifest,
)
from leanfaith.generation.lf022_execution import (
    LF022ExecutionError,
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    VerifiedLF022ExecutionTaskInputs,
    make_lf022_g_open_execution_admission,
    verify_lf022_execution_task,
)
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022BenchmarkRegistryManifest,
    LF022DenylistClearanceRecord,
    LF022JSONLArtifactBinding,
    LF022ProductionSourceRecord,
    LF022ProviderCatalogSnapshot,
    LF022PublicSourceAuthorizationRegistry,
)
from leanfaith.generation.lf022_public_pool import LF022PublicPoolAudit
from leanfaith.generation.llm_variants import (
    PROPOSER_TEMPLATE_ID,
    PROPOSER_TEMPLATE_VERSION_V2,
    VariantOutputErrorCode,
    VariantOutputParseError,
    VariantPromptRequest,
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
    bridge_provider_result_to_generic_llm_lineage,
    load_provider_raw_response,
    load_provider_request,
    persist_provider_raw_response,
    persist_provider_request,
)
from leanfaith.schemas.enums import LLMRole, ParseStatus, QualityTier, ValidationStatus
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.llm import LLMAttemptRecord, LLMCallRecord
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import VariantRecord

LF022_CODEX_PROPOSER_VERSION: Literal["lf022_codex_proposer_v1"] = "lf022_codex_proposer_v1"
_PRIVATE_MARKERS = ("formalmathatepfl/sft_classic", "sft_classic")
_ALLOWED_CODEX_EVENT_TYPES = frozenset(
    {
        "thread.started",
        "turn.started",
        "item.started",
        "item.updated",
        "item.completed",
        "turn.completed",
    }
)
_ALLOWED_CODEX_ITEM_TYPES = frozenset({"reasoning", "agent_message"})
_MAX_CODEX_STREAM_BYTES = 16 * 1024 * 1024
_MAX_CODEX_EVENT_BYTES = 4 * 1024 * 1024
_MAX_CODEX_EVENTS = 20_000


class LF022CodexProposerError(RuntimeError):
    """A frozen input, Codex process, or immutable artifact failed closed."""


class LF022CodexProposerLockedError(LF022CodexProposerError):
    """Another process owns the same content-addressed proposer item."""


class LF022CodexProposerConfig(StrictModel):
    """Frozen provider/runtime policy for the one-example public smoke."""

    schema_version: Literal[1] = 1
    config_id: Literal["lf022_codex_proposer_smoke_v1"]
    status: Literal["one_public_task_smoke_only"]
    provider: Literal["openai_codex_exec"]
    provider_slot: Literal["lf022_codex_proposer_terra_v1"]
    model_family: Literal["openai_codex"]
    model: Literal["gpt-5.6-terra"]
    reasoning_effort: Literal["xhigh"]
    codex_cli_version: str = Field(min_length=1)
    codex_binary_sha256: str = Field(pattern=HEX64_PATTERN)
    provider_catalog: LF022ArtifactBinding
    prompt_template: LF022ArtifactBinding
    output_schema: LF022ArtifactBinding
    timeout_seconds: int = Field(ge=1, le=7200, strict=True)
    termination_grace_seconds: int = Field(ge=1, le=60, strict=True)
    maximum_task_count: Literal[1] = 1
    maximum_concurrency: Literal[1] = 1
    execute_requires_explicit_flag: Literal[True] = True
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    own_validator_allowed: Literal[False] = False
    outputs_provisional_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class LoadedLF022CodexProposerConfig:
    config: LF022CodexProposerConfig
    path: Path
    config_file_sha256: str
    effective_config_hash: str
    catalog: LF022ProviderCatalogSnapshot
    prompt_path: Path
    output_schema_path: Path


class LF022CodexPublicTaskAuthorization(StrictModel):
    """Compact proof that one selected task replayed its full public lineage."""

    schema_version: Literal[1] = 1
    authorization_id: str = Field(pattern=id_pattern("lf022_codex_public_task"))
    source_batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    source_batch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    freeze_request_id: str = Field(pattern=id_pattern("lf022_batch_request"))
    freeze_request_sha256: str = Field(pattern=HEX64_PATTERN)
    admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    admission_sha256: str = Field(pattern=HEX64_PATTERN)
    public_pool_audit_id: str = Field(pattern=id_pattern("lf022_public_pool_audit"))
    public_pool_audit_sha256: str = Field(pattern=HEX64_PATTERN)
    allocation_plan_id: str = Field(pattern=id_pattern("lf022_production_plan"))
    allocation_plan_sha256: str = Field(pattern=HEX64_PATTERN)
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    execution_task_sha256: str = Field(pattern=HEX64_PATTERN)
    allocation_task_id: str = Field(pattern=id_pattern("lf022_production_task"))
    source_admission_record_id: str = Field(pattern=id_pattern("lf022_source_admission"))
    source_record_sha256: str = Field(pattern=HEX64_PATTERN)
    theorem_id: str = Field(pattern=id_pattern("thm"))
    theorem_record_sha256: str = Field(pattern=HEX64_PATTERN)
    representation_id: str = Field(pattern=id_pattern("repr"))
    representation_record_sha256: str = Field(pattern=HEX64_PATTERN)
    context_id: str = Field(pattern=id_pattern("ctx"))
    context_record_sha256: str = Field(pattern=HEX64_PATTERN)
    denylist_clearance_id: str = Field(pattern=id_pattern("lf022_denylist_clearance"))
    denylist_clearance_sha256: str = Field(pattern=HEX64_PATTERN)
    public_source_authorization_id: str = Field(pattern=id_pattern("lf022_public_source"))
    public_source_registry_id: str = Field(pattern=id_pattern("lf022_public_source_registry"))
    benchmark_manifest_id: str = Field(pattern=id_pattern("lf022_benchmark_registry"))
    active_registry_file_sha256: str = Field(pattern=HEX64_PATTERN)
    active_registry_content_hash: str = Field(pattern=HEX64_PATTERN)
    full_bound_artifact_hashes_verified: Literal[True] = True
    allocation_membership_verified: Literal[True] = True
    public_authorization_verified: Literal[True] = True
    denylist_clearance_verified: Literal[True] = True
    private_source_content: Literal[False] = False
    external_transmission_allowed: Literal[True] = True

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        expected = make_id(
            "lf022_codex_public_task",
            self.model_dump(mode="json", exclude={"authorization_id"}),
        )
        if self.authorization_id != expected:
            raise ValueError("authorization_id does not match public-task lineage")
        return self


class LF022CodexProposerItem(StrictModel):
    """One deterministic Codex request derived from a frozen public task."""

    schema_version: Literal[1] = 1
    item_id: str = Field(pattern=id_pattern("lf022_codex_proposer_item"))
    method_version: Literal["lf022_codex_proposer_v1"] = LF022_CODEX_PROPOSER_VERSION
    config_file_sha256: str = Field(pattern=HEX64_PATTERN)
    effective_config_hash: str = Field(pattern=HEX64_PATTERN)
    runner_sha256: str = Field(pattern=HEX64_PATTERN)
    source_batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    source_batch_manifest: str
    source_batch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    source_execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    source_task_artifact: str
    source_task_sha256: str = Field(pattern=HEX64_PATTERN)
    source_authorization_id: str = Field(pattern=id_pattern("lf022_codex_public_task"))
    source_authorization_sha256: str = Field(pattern=HEX64_PATTERN)
    prompt_request_id: str = Field(pattern=id_pattern("lf022_codex_proposer_request"))
    prompt_template_sha256: str = Field(pattern=HEX64_PATTERN)
    prompt_render_sha256: str = Field(pattern=HEX64_PATTERN)
    provider: Literal["openai_codex_exec"]
    provider_slot: Literal["lf022_codex_proposer_terra_v1"]
    model_family: Literal["openai_codex"]
    model: Literal["gpt-5.6-terra"]
    model_revision: str = Field(pattern=id_pattern("lf022_provider_catalog"))
    reasoning_effort: Literal["xhigh"]
    proposal_count: Literal[1]
    private_source_content: Literal[False] = False
    source_is_public: Literal[True] = True
    external_transmission_allowed: Literal[True] = True
    denylist_checked: Literal[True] = True
    denylist_hits: tuple[()] = ()
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        for field in ("source_batch_manifest", "source_task_artifact"):
            _safe_relative(getattr(self, field), field=field)
        expected = make_id(
            "lf022_codex_proposer_item",
            self.model_dump(mode="json", exclude={"item_id"}),
        )
        if self.item_id != expected:
            raise ValueError("item_id does not match proposer input")
        return self


CodexProposerTerminalStatus = Literal[
    "provisional_variants_created",
    "proposer_parse_failed",
    "process_failed",
    "timeout",
    "interrupted",
    "final_output_missing",
    "stdout_protocol_failed",
]


class LF022CodexProposerTerminal(StrictModel):
    """Replayable one-attempt result for an operational Codex proposal."""

    schema_version: Literal[1] = 1
    terminal_id: str = Field(pattern=id_pattern("lf022_codex_proposer_terminal"))
    item_id: str = Field(pattern=id_pattern("lf022_codex_proposer_item"))
    status: CodexProposerTerminalStatus
    exit_code: int | None
    stdout_artifact: str
    stdout_sha256: str = Field(pattern=HEX64_PATTERN)
    stderr_artifact: str
    stderr_sha256: str = Field(pattern=HEX64_PATTERN)
    final_message_artifact: str | None = None
    final_message_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    provider_request_artifact: str
    provider_request_sha256: str = Field(pattern=HEX64_PATTERN)
    provider_raw_artifact: str
    provider_raw_sha256: str = Field(pattern=HEX64_PATTERN)
    llm_attempt_artifact: str
    llm_attempt_sha256: str = Field(pattern=HEX64_PATTERN)
    llm_call_id: str = Field(pattern=id_pattern("call"))
    llm_call_artifact: str
    llm_call_sha256: str = Field(pattern=HEX64_PATTERN)
    variants_artifact: str | None = None
    variants_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    provisional_variant_count: int = Field(ge=0, strict=True)
    error_code: str | None = None
    raw_before_parse_verified: Literal[True] = True
    exact_replay_supported: Literal[True] = True
    output_quality_tier: Literal["provisional"] = "provisional"
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        for field in (
            "stdout_artifact",
            "stderr_artifact",
            "provider_request_artifact",
            "provider_raw_artifact",
            "llm_attempt_artifact",
            "llm_call_artifact",
        ):
            _safe_relative(getattr(self, field), field=field)
        for field in ("final_message_artifact", "variants_artifact"):
            value = getattr(self, field)
            if value is not None:
                _safe_relative(value, field=field)
        if (self.final_message_artifact is None) != (self.final_message_sha256 is None):
            raise ValueError("final-message artifact/hash must be present together")
        if (self.variants_artifact is None) != (self.variants_sha256 is None):
            raise ValueError("variant artifact/hash must be present together")
        successful = self.status == "provisional_variants_created"
        if successful != (self.provisional_variant_count > 0):
            raise ValueError("only successful terminals may contain variants")
        if successful != (self.variants_artifact is not None):
            raise ValueError("successful terminal requires a variant artifact")
        if successful and self.error_code is not None:
            raise ValueError("successful terminal cannot carry an error")
        if not successful and not self.error_code:
            raise ValueError("failed terminal requires an error code")
        expected = make_id(
            "lf022_codex_proposer_terminal",
            self.model_dump(mode="json", exclude={"terminal_id"}),
        )
        if self.terminal_id != expected:
            raise ValueError("terminal_id does not match terminal content")
        return self


class LF022CodexProposerManifest(StrictModel):
    """Mutable summary rebuilt from immutable proposer item trees."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_codex_proposer_v1"] = LF022_CODEX_PROPOSER_VERSION
    config_artifact: str
    config_sha256: str = Field(pattern=HEX64_PATTERN)
    effective_config_hash: str = Field(pattern=HEX64_PATTERN)
    source_batch_manifest: str
    source_batch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    requested_task_count: int = Field(ge=1, strict=True)
    completed_count: int = Field(ge=0, strict=True)
    invoked_count: int = Field(ge=0, strict=True)
    reused_count: int = Field(ge=0, strict=True)
    status_counts: dict[str, int]
    ordered_item_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    model: Literal["gpt-5.6-terra"]
    reasoning_effort: Literal["xhigh"]
    outputs_provisional_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _counts(self) -> Self:
        if sum(self.status_counts.values()) != self.completed_count:
            raise ValueError("terminal status counts do not reconcile")
        if self.invoked_count + self.reused_count != self.completed_count:
            raise ValueError("invoked/reused counts do not reconcile")
        return self


@dataclass(frozen=True, slots=True)
class PreparedLF022CodexProposerItem:
    item: LF022CodexProposerItem
    task: LF022GOpenExecutionTask
    prompt_request: VariantPromptRequest
    source_authorization: LF022CodexPublicTaskAuthorization
    rendered_prompt: str
    item_directory: Path


@dataclass(frozen=True, slots=True)
class CodexProcessCapture:
    status: Literal["completed", "timeout", "interrupted"]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    final_message: bytes | None
    started_at: datetime.datetime
    completed_at: datetime.datetime


class CodexProposerExecutor(Protocol):
    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> CodexProcessCapture: ...


class SubprocessCodexProposerExecutor:
    """Shell-free Codex execution with process-group timeout cleanup."""

    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> CodexProcessCapture:
        if final_message_path.exists():
            raise LF022CodexProposerError("final-message path must be fresh")
        started = datetime.datetime.now(tz=datetime.UTC)
        process = subprocess.Popen(
            tuple(argv),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        status: Literal["completed", "timeout", "interrupted"] = "completed"
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            status = "timeout"
            _terminate_process_group(process, termination_grace_seconds)
            stdout, stderr = process.communicate()
        except KeyboardInterrupt:
            status = "interrupted"
            _terminate_process_group(process, termination_grace_seconds)
            stdout, stderr = process.communicate()
        completed = datetime.datetime.now(tz=datetime.UTC)
        final = final_message_path.read_bytes() if final_message_path.is_file() else None
        return CodexProcessCapture(
            status=status,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            final_message=final,
            started_at=started,
            completed_at=completed,
        )


@dataclass(frozen=True, slots=True)
class LF022CodexProposerRunResult:
    prepared: tuple[PreparedLF022CodexProposerItem, ...]
    terminals: tuple[LF022CodexProposerTerminal, ...]
    manifest: LF022CodexProposerManifest | None
    manifest_path: Path | None
    invoked_count: int
    reused_count: int


def _terminate_process_group(process: subprocess.Popen[bytes], grace_seconds: int) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def _safe_relative(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    if (
        not value.strip()
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
    ):
        raise ValueError(f"{field} must be a normalized repository-relative path")
    return value


def _repo_path(repo_root: Path, value: str, *, field: str) -> Path:
    _safe_relative(value, field=field)
    root = repo_root.resolve()
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LF022CodexProposerError(f"{field} escapes repository root") from exc
    if path.is_symlink() or not path.is_file():
        raise LF022CodexProposerError(f"{field} is missing or unsafe: {path}")
    return path


def _relative(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise LF022CodexProposerError("proposer artifact escapes repository root") from exc


def _canonical_line(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _load_canonical[RecordT: StrictModel](
    path: Path,
    *,
    model: type[RecordT],
    expected_sha256: str | None = None,
    label: str,
) -> RecordT:
    if path.is_symlink() or not path.is_file():
        raise LF022CodexProposerError(f"{label} is missing or unsafe: {path}")
    raw = path.read_bytes()
    if expected_sha256 is not None and hash_file(path) != expected_sha256:
        raise LF022CodexProposerError(f"{label} hash differs from its binding")
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022CodexProposerError(f"invalid {label}: {exc}") from exc
    canonical = canonical_json_bytes(record.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise LF022CodexProposerError(f"{label} is not canonical JSON")
    return record


def _write_immutable(path: Path, payload: bytes) -> str:
    if path.is_symlink():
        raise LF022CodexProposerError(f"immutable path is a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022CodexProposerError(f"immutable artifact conflict: {path}")
        return hash_file(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022CodexProposerError(
                    f"concurrent immutable artifact conflict: {path}"
                ) from None
        path.chmod(0o600)
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _load_bound_artifact(
    repo_root: Path,
    binding: LF022ArtifactBinding,
    *,
    label: str,
) -> Path:
    path = _repo_path(repo_root, binding.path, field=label)
    if hash_file(path) != binding.sha256:
        raise LF022CodexProposerError(f"{label} hash differs from config")
    return path


def load_lf022_codex_proposer_config(
    path: Path,
    *,
    repo_root: Path,
) -> LoadedLF022CodexProposerConfig:
    """Load and replay every immutable provider/config artifact binding."""

    loaded: LoadedConfig[LF022CodexProposerConfig] = load_config(path, LF022CodexProposerConfig)
    config = loaded.config
    catalog_path = _load_bound_artifact(
        repo_root, config.provider_catalog, label="provider catalog"
    )
    prompt_path = _load_bound_artifact(repo_root, config.prompt_template, label="proposer prompt")
    output_schema_path = _load_bound_artifact(
        repo_root, config.output_schema, label="proposer output schema"
    )
    catalog = _load_canonical(
        catalog_path,
        model=LF022ProviderCatalogSnapshot,
        expected_sha256=config.provider_catalog.sha256,
        label="provider catalog",
    )
    expected_model_id = f"openai/{config.model}"
    if catalog.provider_id != config.provider or not any(
        item.deployment_id == config.model and item.model_id == expected_model_id
        for item in catalog.deployments
    ):
        raise LF022CodexProposerError("provider catalog does not bind the configured model")
    rendered_probe = prompt_path.read_text(encoding="utf-8")
    if "{{PROMPT_TEMPLATE_SHA256}}" not in rendered_probe or "{{INPUT_JSON}}" not in rendered_probe:
        raise LF022CodexProposerError("configured prompt is not the reviewed LF-022 template")
    try:
        schema = json.loads(output_schema_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LF022CodexProposerError("invalid Codex proposer output schema") from exc
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise LF022CodexProposerError("Codex proposer output schema root must be an object")
    return LoadedLF022CodexProposerConfig(
        config=config,
        path=path.resolve(),
        config_file_sha256=hash_file(path),
        effective_config_hash=loaded.config_hash,
        catalog=catalog,
        prompt_path=prompt_path,
        output_schema_path=output_schema_path,
    )


def _verify_binding_hash(
    repo_root: Path,
    binding: LF022ArtifactBinding,
    *,
    label: str,
    digest_cache: dict[Path, str],
) -> Path:
    path = _repo_path(repo_root, binding.path, field=label)
    observed = digest_cache.get(path)
    if observed is None:
        observed = hash_file(path)
        digest_cache[path] = observed
    if observed != binding.sha256:
        raise LF022CodexProposerError(f"{label} hash differs from its binding")
    return path


def _select_bound_jsonl[RecordT: StrictModel](
    *,
    repo_root: Path,
    binding: LF022JSONLArtifactBinding,
    model: type[RecordT],
    id_field: str,
    expected_id: str,
    label: str,
    digest_cache: dict[Path, str],
) -> tuple[RecordT, str]:
    """Hash the complete artifact while materializing only one selected record."""

    path = _verify_binding_hash(repo_root, binding, label=label, digest_cache=digest_cache)
    needle = canonical_json_bytes({id_field: expected_id})[1:-1]
    selected: list[tuple[RecordT, bytes]] = []
    line_count = 0
    with path.open("rb") as handle:
        for line_count, line in enumerate(handle, start=1):
            if not line.endswith(b"\n"):
                raise LF022CodexProposerError(f"{label} line {line_count} lacks a final newline")
            if needle not in line:
                continue
            try:
                record = model.model_validate_json(line)
            except ValueError as exc:
                raise LF022CodexProposerError(f"selected {label} record is invalid: {exc}") from exc
            if getattr(record, id_field) == expected_id:
                if line != _canonical_line(record):
                    raise LF022CodexProposerError(f"selected {label} record is not canonical JSONL")
                selected.append((record, line))
    if line_count != binding.record_count:
        raise LF022CodexProposerError(f"{label} line count differs from its binding")
    if len(selected) != 1:
        raise LF022CodexProposerError(
            f"{label} must contain exactly one selected record {expected_id}"
        )
    record, raw = selected[0]
    return record, hash_canonical(json.loads(raw))


def _require_exact_bytes_once(path: Path, needle: bytes, *, label: str) -> None:
    """Check exact membership in a large canonical JSON artifact without loading it."""

    if not needle:
        raise LF022CodexProposerError(f"empty membership needle for {label}")
    with path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        first = data.find(needle)
        if first < 0 or data.find(needle, first + 1) >= 0:
            raise LF022CodexProposerError(
                f"{label} does not occur exactly once in its bound artifact"
            )


def _verify_selected_public_task(
    *,
    repo_root: Path,
    manifest_path: Path,
    manifest: LF022PublicBatchManifest,
    task: LF022GOpenExecutionTask,
    task_sha256: str,
) -> LF022CodexPublicTaskAuthorization:
    """Replay the selected task's complete public lineage with bounded memory."""

    digest_cache: dict[Path, str] = {manifest_path: hash_file(manifest_path)}
    request_path = _verify_binding_hash(
        repo_root,
        manifest.freeze_request,
        label="batch freeze request",
        digest_cache=digest_cache,
    )
    request = _load_canonical(
        request_path,
        model=LF022BatchFreezeRequest,
        expected_sha256=manifest.freeze_request.sha256,
        label="batch freeze request",
    )
    if (
        request.request_id != manifest.freeze_request_id
        or request.batch_directory != manifest.batch_directory
        or request.executor_output_root != manifest.executor_output_root
        or len(request.routes) != len(manifest.routes)
    ):
        raise LF022CodexProposerError("batch manifest differs from its freeze request")
    matching_routes = [
        (route, route_request)
        for route, route_request in zip(manifest.routes, request.routes, strict=True)
        if any(item.execution_task_id == task.execution_task_id for item in route.tasks)
    ]
    if len(matching_routes) != 1:
        raise LF022CodexProposerError("selected task lacks one exact frozen route")
    route, route_request = matching_routes[0]
    if route.proposer_family_id != route_request.proposer_family_id:
        raise LF022CodexProposerError("selected batch route differs from freeze request")
    admission_path = _verify_binding_hash(
        repo_root,
        route.admission,
        label="frozen execution admission",
        digest_cache=digest_cache,
    )
    admission = _load_canonical(
        admission_path,
        model=LF022GOpenExecutionAdmission,
        expected_sha256=route.admission.sha256,
        label="frozen execution admission",
    )
    expected_admission = make_lf022_g_open_execution_admission(
        public_pool_audit_id=route_request.public_pool_audit_id,
        allocation_plan_id=route_request.allocation_plan_id,
        artifacts=route_request.execution_artifacts,
        route=route_request.route,
        retry_policy=route_request.retry_policy,
        code_tree_hash=route_request.code_tree_hash,
    )
    if (
        admission != expected_admission
        or admission.admission_id != route.admission_id
        or admission.route.proposer_family_id != route.proposer_family_id
        or task.execution_admission_id != admission.admission_id
        or task.allocation_plan_id != admission.allocation_plan_id
        or task.allocation_task.task_id not in route_request.allocation_task_ids
        or task.proposal_count != route_request.proposal_count
        or task.requested_relations != route_request.requested_relations
    ):
        raise LF022CodexProposerError("selected task differs from frozen route/admission")

    audit_path = _verify_binding_hash(
        repo_root,
        admission.artifacts.public_pool_audit,
        label="public-pool audit",
        digest_cache=digest_cache,
    )
    audit = _load_canonical(
        audit_path,
        model=LF022PublicPoolAudit,
        expected_sha256=admission.artifacts.public_pool_audit.sha256,
        label="public-pool audit",
    )
    plan_path = _verify_binding_hash(
        repo_root,
        admission.artifacts.allocation_plan,
        label="allocation plan",
        digest_cache=digest_cache,
    )
    if (
        audit.audit_id != admission.public_pool_audit_id
        or audit.outputs.production_plan != admission.artifacts.allocation_plan
        or audit.outputs.source_pool.path == ""
        or not audit.public_sources_only
        or not audit.private_sft_classic_forbidden
    ):
        raise LF022CodexProposerError("public-pool audit differs from admission")
    _require_exact_bytes_once(
        plan_path,
        canonical_json_bytes(task.allocation_task.model_dump(mode="json")),
        label="selected allocation task",
    )
    _require_exact_bytes_once(
        plan_path,
        f'"manifest_id":"{admission.allocation_plan_id}"'.encode(),
        label="allocation plan ID",
    )

    allocation = task.allocation_task
    source_record, source_hash = _select_bound_jsonl(
        repo_root=repo_root,
        binding=audit.outputs.source_pool,
        model=LF022ProductionSourceRecord,
        id_field="admission_record_id",
        expected_id=allocation.admission_record_id,
        label="public source pool",
        digest_cache=digest_cache,
    )
    theorem, theorem_hash = _select_bound_jsonl(
        repo_root=repo_root,
        binding=audit.outputs.theorem_records,
        model=TheoremRecord,
        id_field="theorem_id",
        expected_id=allocation.theorem_id,
        label="public theorem records",
        digest_cache=digest_cache,
    )
    representation, representation_hash = _select_bound_jsonl(
        repo_root=repo_root,
        binding=audit.outputs.representation_records,
        model=RepresentationRecord,
        id_field="representation_id",
        expected_id=allocation.representation_id,
        label="public representation records",
        digest_cache=digest_cache,
    )
    context, context_hash = _select_bound_jsonl(
        repo_root=repo_root,
        binding=audit.outputs.context_records,
        model=ContextRecord,
        id_field="context_id",
        expected_id=allocation.context_id,
        label="public context records",
        digest_cache=digest_cache,
    )
    clearance, clearance_hash = _select_bound_jsonl(
        repo_root=repo_root,
        binding=audit.outputs.denylist_clearance_records,
        model=LF022DenylistClearanceRecord,
        id_field="clearance_id",
        expected_id=source_record.denylist_clearance_id,
        label="denylist clearance records",
        digest_cache=digest_cache,
    )

    benchmark_path = _verify_binding_hash(
        repo_root,
        audit.outputs.benchmark_registry_manifest,
        label="benchmark registry manifest",
        digest_cache=digest_cache,
    )
    benchmark = _load_canonical(
        benchmark_path,
        model=LF022BenchmarkRegistryManifest,
        expected_sha256=audit.outputs.benchmark_registry_manifest.sha256,
        label="benchmark registry manifest",
    )
    registry_path = _verify_binding_hash(
        repo_root,
        audit.active_benchmark_registry,
        label="active benchmark registry",
        digest_cache=digest_cache,
    )
    active_registry = _load_canonical(
        registry_path,
        model=FrozenRegistry,
        expected_sha256=audit.active_benchmark_registry.sha256,
        label="active benchmark registry",
    )
    active_content_hash = DenylistIndex(active_registry).registry_content_hash
    authorization_registry_path = _verify_binding_hash(
        repo_root,
        audit.outputs.public_source_authorization_registry,
        label="public source authorization registry",
        digest_cache=digest_cache,
    )
    authorization_registry = _load_canonical(
        authorization_registry_path,
        model=LF022PublicSourceAuthorizationRegistry,
        expected_sha256=audit.outputs.public_source_authorization_registry.sha256,
        label="public source authorization registry",
    )
    authorizations = {item.authorization_id: item for item in authorization_registry.authorizations}
    authorization = authorizations.get(source_record.public_source_authorization_id)
    if authorization is None:
        raise LF022CodexProposerError("selected source lacks public authorization")
    for name, binding in (
        ("authorized upstream theorem records", authorization.upstream_theorem_records),
        ("authorized upstream context records", authorization.upstream_context_records),
        (
            "authorized upstream extraction manifest",
            authorization.upstream_extraction_output_manifest,
        ),
        (
            "authorized upstream representation records",
            authorization.upstream_representation_records,
        ),
        (
            "authorized upstream representation manifest",
            authorization.upstream_representation_output_manifest,
        ),
        ("authorized mathlib source frame", authorization.mathlib_source_frame),
        ("authorized extraction manifest", authorization.extraction_manifest),
    ):
        _verify_binding_hash(repo_root, binding, label=name, digest_cache=digest_cache)
    if authorization.extraction_reuse_attestation is not None:
        _verify_binding_hash(
            repo_root,
            authorization.extraction_reuse_attestation,
            label="authorized extraction reuse attestation",
            digest_cache=digest_cache,
        )

    expected_source = task.source
    try:
        # Reuse the canonical task-link verifier on a compact exact-input view.
        # The allocation plan membership was independently checked above.
        compact_inputs = VerifiedLF022ExecutionTaskInputs(
            source_records=(source_record,),
            theorems=(theorem,),
            representations=(representation,),
            contexts=(context,),
            clearances=(clearance,),
            benchmark_manifest=benchmark,
            active_registry=active_registry,
            authorization_registry=authorization_registry,
            allocation_tasks_by_id={allocation.task_id: allocation},
            source_records_by_admission_id={source_record.admission_record_id: source_record},
            theorems_by_id={theorem.theorem_id: theorem},
            representations_by_id={representation.representation_id: representation},
            contexts_by_id={context.context_id: context},
            clearances_by_id={clearance.clearance_id: clearance},
            authorizations_by_id=authorizations,
            active_registry_content_hash=active_content_hash,
        )

        # Build only the fields consumed by verify_lf022_execution_task while
        # retaining the original audit and allocation task.
        verify_lf022_execution_task(
            repo_root=repo_root,
            admission=admission,
            verified=SimpleNamespace(
                plan=SimpleNamespace(tasks=(allocation,)),
                audit=audit,
            ),  # type: ignore[arg-type]
            task=task,
            inputs=compact_inputs,
        )
    except LF022ExecutionError as exc:
        raise LF022CodexProposerError(f"public task lineage rejected: {exc}") from exc
    if (
        benchmark.active_registry != audit.active_benchmark_registry
        or active_content_hash != audit.active_benchmark_registry_content_hash
        or source_record.source != expected_source.source_id
        or authorization.source != expected_source.source_id
        or authorization.source_revision != expected_source.source_revision
        or authorization.license_id != expected_source.source_license
        or not authorization.source_is_public
        or not authorization.external_transmission_allowed
        or not clearance.clear
    ):
        raise LF022CodexProposerError("selected task is not fully public and denylist-clear")

    values: dict[str, object] = {
        "schema_version": 1,
        "source_batch_id": manifest.batch_id,
        "source_batch_manifest_sha256": digest_cache[manifest_path],
        "freeze_request_id": request.request_id,
        "freeze_request_sha256": digest_cache[request_path],
        "admission_id": admission.admission_id,
        "admission_sha256": digest_cache[admission_path],
        "public_pool_audit_id": audit.audit_id,
        "public_pool_audit_sha256": digest_cache[audit_path],
        "allocation_plan_id": admission.allocation_plan_id,
        "allocation_plan_sha256": digest_cache[plan_path],
        "execution_task_id": task.execution_task_id,
        "execution_task_sha256": task_sha256,
        "allocation_task_id": allocation.task_id,
        "source_admission_record_id": source_record.admission_record_id,
        "source_record_sha256": source_hash,
        "theorem_id": theorem.theorem_id,
        "theorem_record_sha256": theorem_hash,
        "representation_id": representation.representation_id,
        "representation_record_sha256": representation_hash,
        "context_id": context.context_id,
        "context_record_sha256": context_hash,
        "denylist_clearance_id": clearance.clearance_id,
        "denylist_clearance_sha256": clearance_hash,
        "public_source_authorization_id": authorization.authorization_id,
        "public_source_registry_id": authorization_registry.registry_id,
        "benchmark_manifest_id": benchmark.manifest_id,
        "active_registry_file_sha256": audit.active_benchmark_registry.sha256,
        "active_registry_content_hash": active_content_hash,
        "full_bound_artifact_hashes_verified": True,
        "allocation_membership_verified": True,
        "public_authorization_verified": True,
        "denylist_clearance_verified": True,
        "private_source_content": False,
        "external_transmission_allowed": True,
    }
    return LF022CodexPublicTaskAuthorization.model_validate(
        {
            **values,
            "authorization_id": make_id("lf022_codex_public_task", values),
        }
    )


def _load_selected_task(
    *,
    repo_root: Path,
    batch_manifest_path: Path,
    execution_task_id: str,
) -> tuple[
    LF022PublicBatchManifest,
    str,
    LF022GOpenExecutionTask,
    str,
    Path,
    LF022CodexPublicTaskAuthorization,
]:
    manifest_path = batch_manifest_path.resolve()
    try:
        manifest_relative = manifest_path.relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise LF022CodexProposerError("batch manifest must be inside repository root") from exc
    manifest = _load_canonical(
        manifest_path,
        model=LF022PublicBatchManifest,
        label="LF-022 public batch manifest",
    )
    if not (
        manifest.public_sources_only
        and manifest.private_source_content_forbidden
        and manifest.outputs_provisional_only
        and not manifest.semantic_labels_created
        and not manifest.training_eligible
        and not manifest.evaluation_eligible
    ):
        raise LF022CodexProposerError("batch manifest is not a public provisional source")
    matches = [
        task
        for route in manifest.routes
        for task in route.tasks
        if task.execution_task_id == execution_task_id
    ]
    if len(matches) != 1:
        raise LF022CodexProposerError(
            f"execution task must occur exactly once in frozen batch: {execution_task_id}"
        )
    binding = matches[0].task
    task_path = _repo_path(repo_root, binding.path, field="source task artifact")
    task = _load_canonical(
        task_path,
        model=LF022GOpenExecutionTask,
        expected_sha256=binding.sha256,
        label="frozen LF-022 source task",
    )
    serialized = canonical_json_bytes(task.source.model_dump(mode="json")).decode("utf-8")
    if any(marker in serialized.casefold() for marker in _PRIVATE_MARKERS):
        raise LF022CodexProposerError("private source marker reached Codex proposer")
    if (
        not task.source.source_is_public
        or not task.source.external_transmission_allowed
        or not task.source.denylist_checked
        or task.source.denylist_hits
        or task.source.optional_natural_language is not None
        or task.proposal_count != 1
    ):
        raise LF022CodexProposerError(
            "Codex smoke requires one public, transmissible, Lean-only proposal task"
        )
    authorization = _verify_selected_public_task(
        repo_root=repo_root,
        manifest_path=manifest_path,
        manifest=manifest,
        task=task,
        task_sha256=binding.sha256,
    )
    return manifest, manifest_relative, task, binding.sha256, task_path, authorization


def _item_directory(output_root: Path, item_id: str) -> Path:
    digest = item_id.removeprefix("lf022_codex_proposer_item:")
    return output_root / "items" / digest[:2] / digest


def _prompt_request(
    *,
    task: LF022GOpenExecutionTask,
    loaded: LoadedLF022CodexProposerConfig,
    batch_id: str,
) -> VariantPromptRequest:
    request_id = make_id(
        "lf022_codex_proposer_request",
        {
            "schema": LF022_CODEX_PROPOSER_VERSION,
            "effective_config_hash": loaded.effective_config_hash,
            "source_batch_id": batch_id,
            "source_execution_task_id": task.execution_task_id,
            "source_theorem_id": task.source.source_theorem_id,
            "model": loaded.config.model,
        },
    )
    return VariantPromptRequest(
        request_id=request_id,
        source=task.source,
        proposal_count=task.proposal_count,
        requested_relations=task.requested_relations,
        requested_error_types=(),
        requested_sci_categories=(),
        generation_distribution="G_open",
    )


def _prepare_item(
    *,
    repo_root: Path,
    output_root: Path,
    loaded: LoadedLF022CodexProposerConfig,
    batch_manifest_path: Path,
    execution_task_id: str,
) -> PreparedLF022CodexProposerItem:
    manifest, manifest_relative, task, task_sha, task_path, source_authorization = (
        _load_selected_task(
            repo_root=repo_root,
            batch_manifest_path=batch_manifest_path,
            execution_task_id=execution_task_id,
        )
    )
    request = _prompt_request(task=task, loaded=loaded, batch_id=manifest.batch_id)
    rendered = render_variant_proposer_prompt(
        request,
        template_path=loaded.prompt_path,
    )
    if rendered.template_version != PROPOSER_TEMPLATE_VERSION_V2:
        raise LF022CodexProposerError("Codex proposer requires reviewed prompt v2")
    values: dict[str, object] = {
        "schema_version": 1,
        "method_version": LF022_CODEX_PROPOSER_VERSION,
        "config_file_sha256": loaded.config_file_sha256,
        "effective_config_hash": loaded.effective_config_hash,
        "runner_sha256": hash_file(Path(__file__)),
        "source_batch_id": manifest.batch_id,
        "source_batch_manifest": manifest_relative,
        "source_batch_manifest_sha256": hash_file(batch_manifest_path),
        "source_execution_task_id": task.execution_task_id,
        "source_task_artifact": _relative(repo_root, task_path),
        "source_task_sha256": task_sha,
        "source_authorization_id": source_authorization.authorization_id,
        "source_authorization_sha256": sha256_hex(_canonical_line(source_authorization)),
        "prompt_request_id": request.request_id,
        "prompt_template_sha256": rendered.template_sha256,
        "prompt_render_sha256": rendered.render_sha256,
        "provider": loaded.config.provider,
        "provider_slot": loaded.config.provider_slot,
        "model_family": loaded.config.model_family,
        "model": loaded.config.model,
        "model_revision": loaded.catalog.snapshot_id,
        "reasoning_effort": loaded.config.reasoning_effort,
        "proposal_count": task.proposal_count,
        "private_source_content": False,
        "source_is_public": True,
        "external_transmission_allowed": True,
        "denylist_checked": True,
        "denylist_hits": (),
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    item = LF022CodexProposerItem.model_validate(
        {
            **values,
            "item_id": make_id("lf022_codex_proposer_item", values),
        }
    )
    return PreparedLF022CodexProposerItem(
        item=item,
        task=task,
        prompt_request=request,
        source_authorization=source_authorization,
        rendered_prompt=rendered.text,
        item_directory=_item_directory(output_root, item.item_id),
    )


def _build_argv(
    *,
    loaded: LoadedLF022CodexProposerConfig,
    output_schema_path: Path,
    final_message_path: Path,
) -> tuple[str, ...]:
    return (
        "codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--disable",
        "shell_tool",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--json",
        "--model",
        loaded.config.model,
        "-c",
        f'model_reasoning_effort="{loaded.config.reasoning_effort}"',
        "-c",
        "web_search=disabled",
        "-c",
        "shell_environment_policy.inherit=none",
        "--output-schema",
        str(output_schema_path),
        "-o",
        str(final_message_path),
        "-",
    )


def verify_codex_proposer_cli_pin(config: LF022CodexProposerConfig) -> None:
    """Fail before external I/O if the Codex executable changed."""

    result = subprocess.run(
        ["codex", "--version"],
        check=False,
        capture_output=True,
        timeout=30,
        shell=False,
    )
    observed = result.stdout.decode("utf-8", errors="strict").strip()
    if result.returncode != 0 or observed != config.codex_cli_version:
        raise LF022CodexProposerError(
            f"Codex version mismatch: {observed!r} != {config.codex_cli_version!r}"
        )
    binary_text = shutil.which("codex")
    if binary_text is None or hash_file(Path(binary_text).resolve()) != config.codex_binary_sha256:
        raise LF022CodexProposerError("Codex executable is missing or differs from pin")


def _provider_request(
    prepared: PreparedLF022CodexProposerItem,
    loaded: LoadedLF022CodexProposerConfig,
) -> ProviderRequest:
    identity = ProviderIdentity(
        provider=loaded.config.provider,
        model=loaded.config.model,
        revision=loaded.catalog.snapshot_id,
        transport="external_disabled",
    )
    decoding: dict[str, str] = {
        "reasoning_effort": loaded.config.reasoning_effort,
        "codex_cli_version": loaded.config.codex_cli_version,
        "output_schema_sha256": loaded.config.output_schema.sha256,
    }
    return ProviderRequest.create(
        identity=identity,
        prompt_template_hash=prepared.item.prompt_template_sha256,
        rendered_prompt=prepared.rendered_prompt,
        decoding=decoding,
        input_ids=variant_provider_input_ids(prepared.prompt_request),
        private_source_content=False,
        attempt_index=0,
    )


def _validate_codex_stdout(stdout: bytes, final_message: bytes) -> str | None:
    """Require one completed agent message identical to the fresh final file."""

    if len(stdout) > _MAX_CODEX_STREAM_BYTES:
        return "stdout exceeds size limit"
    if not stdout or not stdout.endswith(b"\n"):
        return "stdout is empty or lacks a final newline"
    lines = stdout.splitlines()
    if len(lines) > _MAX_CODEX_EVENTS:
        return "stdout event count exceeds limit"
    events: list[Mapping[str, object]] = []
    for index, line in enumerate(lines):
        if not line or len(line) > _MAX_CODEX_EVENT_BYTES:
            return f"stdout event {index} is empty or too large"
        duplicate: str | None = None

        def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
            nonlocal duplicate
            result: dict[str, object] = {}
            for key, value in values:
                if key in result:
                    duplicate = key
                result[key] = value
            return result

        def nonfinite(value: str) -> float:
            raise ValueError(f"non-finite JSON value {value!r}")

        try:
            value = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=pairs,
                parse_constant=nonfinite,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            return f"stdout event {index} is invalid JSON: {exc}"
        if duplicate is not None:
            return f"stdout event {index} contains duplicate key {duplicate!r}"
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            return f"stdout event {index} is not a typed object"
        events.append(value)
    event_types = [str(event["type"]) for event in events]
    unknown = [item for item in event_types if item not in _ALLOWED_CODEX_EVENT_TYPES]
    if unknown:
        return f"stdout contains unknown/failure event types: {unknown}"
    if event_types.count("thread.started") != 1 or event_types.count("turn.started") != 1:
        return "stdout requires exactly one thread.started and turn.started event"
    if event_types.count("turn.completed") != 1 or event_types[-1] != "turn.completed":
        return "stdout requires exactly one final turn.completed event"
    messages: list[str] = []
    for event in events:
        event_type = str(event["type"])
        if event_type.startswith("item."):
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                return "stdout item event lacks a typed item"
            item_type = str(item["type"])
            if item_type not in _ALLOWED_CODEX_ITEM_TYPES:
                return f"stdout rejected tool/item type {item_type!r}"
        else:
            continue
        if event_type == "item.completed" and item_type == "agent_message":
            text = item.get("text")
            if not isinstance(text, str):
                return "completed agent message lacks text"
            messages.append(text)
    if len(messages) != 1:
        return f"stdout contains {len(messages)} completed agent messages"
    if messages[0].encode("utf-8") != final_message:
        return "stdout agent message differs from fresh final-message file"
    return None


def _terminal(
    *,
    item: LF022CodexProposerItem,
    status: CodexProposerTerminalStatus,
    exit_code: int | None,
    repo_root: Path,
    stdout_path: Path,
    stderr_path: Path,
    final_path: Path | None,
    request_path: Path,
    provider_result: ProviderResult,
    llm_attempt_path: Path,
    llm_call: LLMCallRecord,
    llm_call_path: Path,
    variants_path: Path | None,
    variant_count: int,
    error_code: str | None,
) -> LF022CodexProposerTerminal:
    values: dict[str, object] = {
        "schema_version": 1,
        "item_id": item.item_id,
        "status": status,
        "exit_code": exit_code,
        "stdout_artifact": _relative(repo_root, stdout_path),
        "stdout_sha256": hash_file(stdout_path),
        "stderr_artifact": _relative(repo_root, stderr_path),
        "stderr_sha256": hash_file(stderr_path),
        "final_message_artifact": (
            _relative(repo_root, final_path) if final_path is not None else None
        ),
        "final_message_sha256": hash_file(final_path) if final_path is not None else None,
        "provider_request_artifact": _relative(repo_root, request_path),
        "provider_request_sha256": hash_file(request_path),
        "provider_raw_artifact": _relative(repo_root, provider_result.raw_response_path),
        "provider_raw_sha256": provider_result.raw_response_sha256,
        "llm_attempt_artifact": _relative(repo_root, llm_attempt_path),
        "llm_attempt_sha256": hash_file(llm_attempt_path),
        "llm_call_id": llm_call.call_id,
        "llm_call_artifact": _relative(repo_root, llm_call_path),
        "llm_call_sha256": hash_file(llm_call_path),
        "variants_artifact": (
            _relative(repo_root, variants_path) if variants_path is not None else None
        ),
        "variants_sha256": hash_file(variants_path) if variants_path is not None else None,
        "provisional_variant_count": variant_count,
        "error_code": error_code,
        "raw_before_parse_verified": True,
        "exact_replay_supported": True,
        "output_quality_tier": "provisional",
        "semantic_labels_created": False,
        "silver_records_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022CodexProposerTerminal.model_validate(
        {
            **values,
            "terminal_id": make_id("lf022_codex_proposer_terminal", values),
        }
    )


def _persist_lineage(
    *,
    repo_root: Path,
    prepared: PreparedLF022CodexProposerItem,
    loaded: LoadedLF022CodexProposerConfig,
    provider_request: ProviderRequest,
    provider_result: ProviderResult,
    request_path: Path,
    parse_status: ParseStatus,
    parsed_batch: VariantProposalBatch | None,
    started_at: datetime.datetime,
    completed_at: datetime.datetime,
) -> tuple[LLMAttemptRecord, Path, LLMCallRecord, Path]:
    lineage = bridge_provider_result_to_generic_llm_lineage(
        request=provider_request,
        result=provider_result,
        request_artifact_path=request_path,
        artifact_root=repo_root,
        role=LLMRole.PROPOSER,
        provider_slot=loaded.config.provider_slot,
        model_family=loaded.config.model_family,
        prompt_template_id=PROPOSER_TEMPLATE_ID,
        prompt_template_version=PROPOSER_TEMPLATE_VERSION_V2,
        execution_mode="external",
        parse_status=parse_status,
        parsed_output=(parsed_batch.model_dump(mode="json") if parsed_batch is not None else None),
        private_source_content=False,
        denylist_checked=True,
        denylist_hits=(),
        started_at=started_at,
        completed_at=completed_at,
        supervision_eligible=False,
        metadata={
            "generation_config_hash": loaded.effective_config_hash,
            "source_batch_id": prepared.item.source_batch_id,
            "source_execution_task_id": prepared.item.source_execution_task_id,
            "semantic_labels_created": False,
        },
    )
    attempt_path = prepared.item_directory / "llm_attempt.json"
    call_path = prepared.item_directory / "llm_call.json"
    _write_immutable(attempt_path, _canonical_line(lineage.attempt))
    _write_immutable(call_path, _canonical_line(lineage.call))
    return lineage.attempt, attempt_path, lineage.call, call_path


def _execute_one(
    *,
    repo_root: Path,
    prepared: PreparedLF022CodexProposerItem,
    loaded: LoadedLF022CodexProposerConfig,
    executor: CodexProposerExecutor,
) -> LF022CodexProposerTerminal:
    item_dir = prepared.item_directory
    item_dir.mkdir(parents=True, exist_ok=True)
    lock_path = item_dir / ".lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LF022CodexProposerLockedError(
                f"another process owns {prepared.item.item_id}"
            ) from exc
        terminal_path = item_dir / "terminal.json"
        if terminal_path.is_file():
            return _replay_one(
                repo_root=repo_root,
                prepared=prepared,
                loaded=loaded,
            )

        _write_immutable(item_dir / "input.json", _canonical_line(prepared.item))
        _write_immutable(
            item_dir / "source_authorization.json",
            _canonical_line(prepared.source_authorization),
        )
        _write_immutable(item_dir / "prompt.txt", prepared.rendered_prompt.encode("utf-8"))
        schema_path = item_dir / "output_schema.json"
        _write_immutable(schema_path, loaded.output_schema_path.read_bytes())
        provider_request = _provider_request(prepared, loaded)
        request_path = item_dir / "provider_request.json"
        persist_provider_request(provider_request, request_path)

        workspace = item_dir / "workspace"
        workspace.mkdir(exist_ok=False)
        final_path = item_dir / "final_message.json"
        argv = _build_argv(
            loaded=loaded,
            output_schema_path=schema_path,
            final_message_path=final_path,
        )
        capture = executor.execute(
            argv=argv,
            prompt=prepared.rendered_prompt.encode("utf-8"),
            cwd=workspace,
            final_message_path=final_path,
            timeout_seconds=loaded.config.timeout_seconds,
            termination_grace_seconds=loaded.config.termination_grace_seconds,
        )
        stdout_path = item_dir / "stdout.jsonl"
        stderr_path = item_dir / "stderr.txt"
        _write_immutable(stdout_path, capture.stdout)
        _write_immutable(stderr_path, capture.stderr)
        persisted_final: Path | None = None
        if capture.final_message is not None:
            _write_immutable(final_path, capture.final_message)
            persisted_final = final_path

        status: CodexProposerTerminalStatus
        error_code: str | None
        parsed_batch: VariantProposalBatch | None = None
        if capture.status == "timeout":
            status, error_code = "timeout", "codex_timeout"
        elif capture.status == "interrupted":
            status, error_code = "interrupted", "codex_interrupted"
        elif capture.exit_code != 0:
            status, error_code = "process_failed", f"codex_exit_{capture.exit_code}"
        elif capture.final_message is None:
            status, error_code = "final_output_missing", "final_output_missing"
        else:
            protocol_error = _validate_codex_stdout(capture.stdout, capture.final_message)
            if protocol_error is not None:
                status, error_code = "stdout_protocol_failed", protocol_error
            else:
                try:
                    parsed_batch = parse_variant_proposer_output(
                        capture.final_message.decode("utf-8")
                    )
                    if len(parsed_batch.variants) != prepared.prompt_request.proposal_count or any(
                        proposal.intended_relation
                        not in prepared.prompt_request.requested_relations
                        for proposal in parsed_batch.variants
                    ):
                        raise VariantOutputParseError(
                            VariantOutputErrorCode.REQUEST_MISMATCH,
                            "response differs from requested count or relations",
                        )
                except (UnicodeDecodeError, ValueError) as exc:
                    status, error_code = "proposer_parse_failed", str(exc)
                    parsed_batch = None
                else:
                    status, error_code = "provisional_variants_created", None

        if status in {"provisional_variants_created", "proposer_parse_failed"}:
            assert capture.final_message is not None
            provider_raw = ProviderRawResponse.success(
                provider_request,
                capture.final_message.decode("utf-8"),
            )
            parse_status = (
                ParseStatus.PARSED
                if status == "provisional_variants_created"
                else ParseStatus.PARSE_FAILED
            )
        else:
            provider_raw = ProviderRawResponse.error(
                provider_request,
                error_type=error_code or "codex_process_failure",
                error_detail=error_code,
            )
            parse_status = ParseStatus.EMPTY
        provider_result = persist_provider_raw_response(
            item_dir / "provider_raw",
            provider_raw,
        )
        _, llm_attempt_path, llm_call, llm_call_path = _persist_lineage(
            repo_root=repo_root,
            prepared=prepared,
            loaded=loaded,
            provider_request=provider_request,
            provider_result=provider_result,
            request_path=request_path,
            parse_status=parse_status,
            parsed_batch=parsed_batch,
            started_at=capture.started_at,
            completed_at=capture.completed_at,
        )

        variants: tuple[VariantRecord, ...] = ()
        variants_path: Path | None = None
        if status == "provisional_variants_created":
            variants = materialize_verified_provisional_variants(
                request=prepared.prompt_request,
                call=llm_call,
                artifact_root=repo_root,
                generation_config_hash=loaded.effective_config_hash,
                template_path=loaded.prompt_path,
            )
            if any(
                item.quality_tier is not QualityTier.PROVISIONAL
                or item.validation_status is not ValidationStatus.UNVALIDATED
                for item in variants
            ):
                raise LF022CodexProposerError(
                    "Codex proposer may emit only unvalidated provisional variants"
                )
            variants_path = item_dir / "provisional_variants.jsonl"
            _write_immutable(
                variants_path,
                b"".join(_canonical_line(item) for item in variants),
            )
        terminal = _terminal(
            item=prepared.item,
            status=status,
            exit_code=capture.exit_code,
            repo_root=repo_root,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            final_path=persisted_final,
            request_path=request_path,
            provider_result=provider_result,
            llm_attempt_path=llm_attempt_path,
            llm_call=llm_call,
            llm_call_path=llm_call_path,
            variants_path=variants_path,
            variant_count=len(variants),
            error_code=error_code,
        )
        _write_immutable(terminal_path, _canonical_line(terminal))
        return terminal


def _bound_path(
    repo_root: Path,
    artifact: str,
    digest: str,
    *,
    label: str,
) -> Path:
    path = _repo_path(repo_root, artifact, field=label)
    if hash_file(path) != digest:
        raise LF022CodexProposerError(f"{label} hash drifted")
    return path


def _replay_one(
    *,
    repo_root: Path,
    prepared: PreparedLF022CodexProposerItem,
    loaded: LoadedLF022CodexProposerConfig,
) -> LF022CodexProposerTerminal:
    item_dir = prepared.item_directory
    input_record = _load_canonical(
        item_dir / "input.json",
        model=LF022CodexProposerItem,
        label="Codex proposer input",
    )
    if input_record != prepared.item:
        raise LF022CodexProposerError("persisted proposer input differs from frozen task")
    source_authorization = _load_canonical(
        item_dir / "source_authorization.json",
        model=LF022CodexPublicTaskAuthorization,
        expected_sha256=prepared.item.source_authorization_sha256,
        label="Codex public-task authorization",
    )
    if source_authorization != prepared.source_authorization:
        raise LF022CodexProposerError(
            "persisted public-task authorization differs from replayed lineage"
        )
    terminal = _load_canonical(
        item_dir / "terminal.json",
        model=LF022CodexProposerTerminal,
        label="Codex proposer terminal",
    )
    if terminal.item_id != prepared.item.item_id:
        raise LF022CodexProposerError("terminal belongs to a different proposer item")
    for artifact, digest, label in (
        (terminal.stdout_artifact, terminal.stdout_sha256, "stdout"),
        (terminal.stderr_artifact, terminal.stderr_sha256, "stderr"),
        (
            terminal.provider_request_artifact,
            terminal.provider_request_sha256,
            "provider request",
        ),
        (terminal.provider_raw_artifact, terminal.provider_raw_sha256, "provider raw response"),
        (terminal.llm_attempt_artifact, terminal.llm_attempt_sha256, "LLM attempt"),
        (terminal.llm_call_artifact, terminal.llm_call_sha256, "LLM call"),
    ):
        _bound_path(repo_root, artifact, digest, label=label)
    if terminal.final_message_artifact is not None:
        assert terminal.final_message_sha256 is not None
        _bound_path(
            repo_root,
            terminal.final_message_artifact,
            terminal.final_message_sha256,
            label="final message",
        )
    request_path = _bound_path(
        repo_root,
        terminal.provider_request_artifact,
        terminal.provider_request_sha256,
        label="provider request",
    )
    request = load_provider_request(request_path)
    raw_path = _bound_path(
        repo_root,
        terminal.provider_raw_artifact,
        terminal.provider_raw_sha256,
        label="provider raw response",
    )
    raw = load_provider_raw_response(raw_path, request=request)
    call_path = _bound_path(
        repo_root,
        terminal.llm_call_artifact,
        terminal.llm_call_sha256,
        label="LLM call",
    )
    call = _load_canonical(call_path, model=LLMCallRecord, label="LLM call")
    attempt_path = _bound_path(
        repo_root,
        terminal.llm_attempt_artifact,
        terminal.llm_attempt_sha256,
        label="LLM attempt",
    )
    attempt = _load_canonical(attempt_path, model=LLMAttemptRecord, label="LLM attempt")
    if (
        call.call_id != terminal.llm_call_id
        or attempt.call_id != call.call_id
        or call.provider_request_hash != request.request_hash
        or call.raw_response_sha256 != hash_file(raw_path)
        or call.supervision_eligible
        or call.private_source_content
    ):
        raise LF022CodexProposerError("replayed LLM lineage is inconsistent")
    if terminal.status == "provisional_variants_created":
        if raw.status != "success" or call.parse_status is not ParseStatus.PARSED:
            raise LF022CodexProposerError("successful terminal lacks a parsed provider response")
        variants = materialize_verified_provisional_variants(
            request=prepared.prompt_request,
            call=call,
            artifact_root=repo_root,
            generation_config_hash=loaded.effective_config_hash,
            template_path=loaded.prompt_path,
        )
        assert terminal.variants_artifact is not None
        assert terminal.variants_sha256 is not None
        variants_path = _bound_path(
            repo_root,
            terminal.variants_artifact,
            terminal.variants_sha256,
            label="provisional variants",
        )
        expected = b"".join(_canonical_line(item) for item in variants)
        if (
            variants_path.read_bytes() != expected
            or len(variants) != terminal.provisional_variant_count
        ):
            raise LF022CodexProposerError("replayed provisional variants differ")
    elif terminal.variants_artifact is not None or terminal.provisional_variant_count != 0:
        raise LF022CodexProposerError("failed terminal unexpectedly contains variants")
    return terminal


def run_lf022_codex_proposer(
    *,
    repo_root: Path,
    config_path: Path,
    batch_manifest_path: Path,
    execution_task_ids: Sequence[str],
    output_root: Path,
    execute_public_provisional: bool = False,
    executor: CodexProposerExecutor | None = None,
    verify_cli_pin: bool = True,
) -> LF022CodexProposerRunResult:
    """Prepare or explicitly execute a frozen public Codex proposer subset.

    V1 intentionally accepts exactly one task.  A later reviewed config may
    raise this bound; callers cannot turn the smoke into a scale job with a CLI
    flag alone.
    """

    repo_root = repo_root.resolve()
    config_path = config_path.resolve()
    batch_manifest_path = batch_manifest_path.resolve()
    output_root = output_root.resolve()
    try:
        output_root.relative_to(repo_root)
    except ValueError as exc:
        raise LF022CodexProposerError("output root must stay inside repository root") from exc
    if output_root.is_symlink():
        raise LF022CodexProposerError("output root cannot be a symlink")
    if len(execution_task_ids) != 1 or len(set(execution_task_ids)) != 1:
        raise LF022CodexProposerError("v1 requires exactly one unique execution task ID")
    loaded = load_lf022_codex_proposer_config(config_path, repo_root=repo_root)
    prepared = tuple(
        _prepare_item(
            repo_root=repo_root,
            output_root=output_root,
            loaded=loaded,
            batch_manifest_path=batch_manifest_path,
            execution_task_id=task_id,
        )
        for task_id in execution_task_ids
    )
    if not execute_public_provisional:
        if executor is not None:
            raise LF022CodexProposerError("offline preparation rejects a process executor")
        return LF022CodexProposerRunResult(prepared, (), None, None, 0, 0)
    if verify_cli_pin:
        verify_codex_proposer_cli_pin(loaded.config)
    runner = executor or SubprocessCodexProposerExecutor()
    output_root.mkdir(parents=True, exist_ok=True)
    terminals: list[LF022CodexProposerTerminal] = []
    invoked = 0
    reused = 0
    for item in prepared:
        existed = (item.item_directory / "terminal.json").is_file()
        terminals.append(
            _execute_one(
                repo_root=repo_root,
                prepared=item,
                loaded=loaded,
                executor=runner,
            )
        )
        if existed:
            reused += 1
        else:
            invoked += 1
    counts: dict[str, int] = {
        str(status): count
        for status, count in sorted(Counter(item.status for item in terminals).items())
    }
    manifest = LF022CodexProposerManifest(
        config_artifact=_relative(repo_root, config_path),
        config_sha256=loaded.config_file_sha256,
        effective_config_hash=loaded.effective_config_hash,
        source_batch_manifest=_relative(repo_root, batch_manifest_path),
        source_batch_manifest_sha256=hash_file(batch_manifest_path),
        requested_task_count=len(prepared),
        completed_count=len(terminals),
        invoked_count=invoked,
        reused_count=reused,
        status_counts=counts,
        ordered_item_ids_sha256=hash_canonical([item.item.item_id for item in prepared]),
        model=loaded.config.model,
        reasoning_effort=loaded.config.reasoning_effort,
    )
    manifest_path = output_root / "manifest.json"
    _write_atomic(manifest_path, _canonical_line(manifest))
    return LF022CodexProposerRunResult(
        prepared=prepared,
        terminals=tuple(terminals),
        manifest=manifest,
        manifest_path=manifest_path,
        invoked_count=invoked,
        reused_count=reused,
    )


__all__ = [
    "CodexProcessCapture",
    "CodexProposerExecutor",
    "LF022CodexProposerConfig",
    "LF022CodexProposerError",
    "LF022CodexProposerItem",
    "LF022CodexProposerLockedError",
    "LF022CodexProposerManifest",
    "LF022CodexProposerRunResult",
    "LF022CodexProposerTerminal",
    "LoadedLF022CodexProposerConfig",
    "SubprocessCodexProposerExecutor",
    "load_lf022_codex_proposer_config",
    "run_lf022_codex_proposer",
    "verify_codex_proposer_cli_pin",
]
