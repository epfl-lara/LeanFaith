"""Fail-closed Codex CLI provider boundary for high-value public proposers.

This is deliberately separate from the bound LF-021 local collectors.  Codex
is an external, non-revisioned proposer family: it is not a validator, held-out
judge, semantic labeler, or routine high-volume real-output generator.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import require_utc

_HEX64 = r"^[0-9a-f]{64}$"
_REQUEST_ID = r"^codex_exec_request_v1:[0-9a-f]{64}$"
_ATTEMPT_ID = r"^codex_exec_attempt_v1:[0-9a-f]{64}$"
_TERMINAL_ID = r"^codex_exec_terminal_v1:[0-9a-f]{64}$"
_SECRET_NAME = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|"
    r"PRIVATE[_-]?KEY|CREDENTIAL|AUTH)",
    re.IGNORECASE,
)
_GENERIC_SECRETS = (
    re.compile(r"\bhf_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
)
_ALLOWED_EVENT_TYPES = {
    "thread.started",
    "turn.started",
    "item.started",
    "item.updated",
    "item.completed",
    "turn.completed",
}
_ALLOWED_ITEM_TYPES = {"reasoning", "agent_message"}
_MAX_STREAM_BYTES = 16 * 1024 * 1024
_MAX_EVENT_BYTES = 4 * 1024 * 1024
_MAX_EVENTS = 20_000
_OVERHEAD_WARNING = (
    "codex_exec_has_large_fixed_context_overhead_use_for_high_value_proposals_not_bulk"
)


class CodexExecProviderError(RuntimeError):
    """Base error for this provider boundary."""


class CodexExecConfigError(CodexExecProviderError):
    """A bound provider artifact or executable pin is invalid."""


class CodexExecPrivacyError(CodexExecProviderError):
    """Non-public content was presented to the external Codex provider."""


class CodexExecArtifactConflict(CodexExecProviderError):
    """A supposedly immutable artifact differs from existing bytes."""


class CodexExecPartialAttempt(CodexExecProviderError):
    """An allocated attempt lacks a validated terminal and must not be reused."""


class PriorCodexProbeObservation(StrictModel):
    """Non-authoritative economics/diagnostic observation from a live probe."""

    observed_at: datetime.datetime
    input_tokens: int = Field(ge=1, strict=True)
    stale_model_cache_diagnostic_observed: bool
    diagnostic_was_nonfatal: bool
    interpretation: Literal[
        "large_fixed_codex_context_overhead_not_suitable_for_routine_high_volume_generation"
    ]

    @model_validator(mode="after")
    def _utc(self) -> Self:
        require_utc(self.observed_at)
        return self


class CodexExecProviderConfigV1(StrictModel):
    """Exact immutable provider and artifact bindings."""

    schema_version: Literal[1] = 1
    config_id: Literal[
        "leanfaith_codex_exec_provider_probe_v1",
        "leanfaith_codex_exec_public_qualification_v1",
    ]
    status: Literal["probe_and_single_public_qualification_only"]
    provider: Literal["openai_codex_exec"]
    model_family: Literal["openai_codex"]
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    codex_cli_version: str = Field(min_length=1)
    codex_binary_sha256: str = Field(pattern=_HEX64)
    adapter_artifact: str = Field(min_length=1)
    adapter_sha256: str = Field(pattern=_HEX64)
    launcher_artifact: str = Field(min_length=1)
    launcher_sha256: str = Field(pattern=_HEX64)
    prompt_artifact: str = Field(min_length=1)
    prompt_sha256: str = Field(pattern=_HEX64)
    output_schema_artifact: str = Field(min_length=1)
    output_schema_sha256: str = Field(pattern=_HEX64)
    timeout_seconds: int = Field(ge=1, le=7200, strict=True)
    termination_grace_seconds: int = Field(ge=1, le=60, strict=True)
    intended_role: Literal["high_value_llm_mutation_or_statement_proposer"]
    own_validator_allowed: Literal[False] = False
    routine_high_volume_real_output_generator: Literal[False] = False
    private_source_content_allowed: Literal[False] = False
    external_provider_eligible_required: Literal[True] = True
    immutable_model_revision_available: Literal[False] = False
    contamination_status: Literal["unknown_no_public_training_cutoff_or_immutable_revision"]
    heldout_or_unseen_claim_allowed: Literal[False] = False
    evaluation_judge_eligible_if_used_for_supervision: Literal[False] = False
    prior_probe_observation: PriorCodexProbeObservation
    rules: tuple[str, ...] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class LoadedCodexExecConfigV1:
    config: CodexExecProviderConfigV1
    path: Path
    config_file_sha256: str
    effective_config_hash: str
    prompt: bytes
    output_schema: bytes
    schema_document: dict[str, object]


def _inside_repo(root: Path, artifact: str) -> Path:
    pure = PurePosixPath(artifact)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise CodexExecConfigError(f"artifact must be a safe repository-relative path: {artifact}")
    path = (root / Path(*pure.parts)).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise CodexExecConfigError(f"artifact escapes repository root: {artifact}") from exc
    if path.is_symlink() or not path.is_file():
        raise CodexExecConfigError(f"artifact is missing or not a regular file: {artifact}")
    return path


def _json_load_strict(raw: bytes, *, what: str) -> object:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def nonfinite(value: str) -> float:
        raise ValueError(f"non-finite JSON value {value!r}")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CodexExecConfigError(f"invalid {what}: {exc}") from exc


def load_codex_exec_config_v1(path: Path, *, repo_root: Path) -> LoadedCodexExecConfigV1:
    """Load and hash-check a frozen provider configuration."""

    loaded: LoadedConfig[CodexExecProviderConfigV1] = load_config(path, CodexExecProviderConfigV1)
    config = loaded.config
    bindings = (
        (config.adapter_artifact, config.adapter_sha256),
        (config.launcher_artifact, config.launcher_sha256),
        (config.prompt_artifact, config.prompt_sha256),
        (config.output_schema_artifact, config.output_schema_sha256),
    )
    resolved: dict[str, Path] = {}
    for artifact, expected in bindings:
        resolved[artifact] = _inside_repo(repo_root, artifact)
        observed = hash_file(resolved[artifact])
        if observed != expected:
            raise CodexExecConfigError(
                f"artifact hash mismatch for {artifact}: {observed} != {expected}"
            )
    prompt = resolved[config.prompt_artifact].read_bytes()
    if not prompt or b"\x00" in prompt:
        raise CodexExecConfigError("prompt must be nonempty UTF-8 text without NUL")
    try:
        prompt.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodexExecConfigError("prompt must be UTF-8") from exc
    output_schema = resolved[config.output_schema_artifact].read_bytes()
    schema = _json_load_strict(output_schema, what="output JSON schema")
    if not isinstance(schema, dict):
        raise CodexExecConfigError("output JSON schema root must be an object")
    return LoadedCodexExecConfigV1(
        config=config,
        path=path,
        config_file_sha256=hash_file(path),
        effective_config_hash=loaded.config_hash,
        prompt=prompt,
        output_schema=output_schema,
        schema_document=schema,
    )


def _request_id_payload(record: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in record.items() if key != "request_id"}


class CodexExecRequestV1(StrictModel):
    """Deterministic semantic request; no secret or credential values are fields."""

    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=_REQUEST_ID)
    execution_mode: Literal["mock", "external"]
    provider: Literal["openai_codex_exec"]
    model_family: Literal["openai_codex"]
    model: str
    reasoning_effort: str
    codex_cli_version: str
    codex_binary_sha256: str = Field(pattern=_HEX64)
    config_artifact: str
    config_file_sha256: str = Field(pattern=_HEX64)
    effective_config_hash: str = Field(pattern=_HEX64)
    prompt_artifact: str
    prompt_sha256: str = Field(pattern=_HEX64)
    output_schema_artifact: str
    output_schema_sha256: str = Field(pattern=_HEX64)
    input_ids: tuple[str, ...] = Field(min_length=1)
    private_source_content: Literal[False] = False
    external_provider_eligible: Literal[True] = True
    reference_hidden: bool
    timeout_seconds: int = Field(ge=1, strict=True)
    termination_grace_seconds: int = Field(ge=1, strict=True)
    argv: tuple[str, ...] = Field(min_length=1)
    immutable_model_revision_available: Literal[False] = False
    contamination_status: Literal["unknown_no_public_training_cutoff_or_immutable_revision"]
    semantic_labels_created: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    supervision_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _id(self) -> Self:
        expected = "codex_exec_request_v1:" + hash_canonical(
            {"schema": "codex_exec_request_v1", **_request_id_payload(self.model_dump(mode="json"))}
        )
        if self.request_id != expected:
            raise ValueError("request_id does not match request payload")
        return self


class CodexExecAttemptV1(StrictModel):
    """Deterministic append-only attempt allocation."""

    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=_ATTEMPT_ID)
    request_id: str = Field(pattern=_REQUEST_ID)
    attempt_index: int = Field(ge=0, strict=True)
    argv: tuple[str, ...] = Field(min_length=1)
    prompt_via_stdin: Literal[True] = True
    shell_used: Literal[False] = False
    isolated_empty_working_directory: Literal[True] = True
    output_last_message_must_be_fresh: Literal[True] = True
    timeout_seconds: int = Field(ge=1, strict=True)
    termination_grace_seconds: int = Field(ge=1, strict=True)
    semantic_labels_created: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _id(self) -> Self:
        expected = "codex_exec_attempt_v1:" + hash_canonical(
            {
                "schema": "codex_exec_attempt_v1",
                "request_id": self.request_id,
                "attempt_index": self.attempt_index,
            }
        )
        if self.attempt_id != expected:
            raise ValueError("attempt_id does not match request and attempt_index")
        return self


class CodexUsageV1(StrictModel):
    input_tokens: int = Field(ge=0, strict=True)
    cached_input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)


TerminalStatus = Literal[
    "success",
    "version_mismatch",
    "binary_mismatch",
    "process_error",
    "timeout",
    "interrupted",
    "secret_redacted",
    "stdout_invalid",
    "protocol_error",
    "tool_item_rejected",
    "missing_final_answer",
    "multiple_final_answers",
    "final_output_missing",
    "final_output_mismatch",
    "final_answer_unparseable",
    "schema_violation",
    "usage_missing_or_invalid",
]


class CodexExecTerminalV1(StrictModel):
    """Terminal record written only after raw artifacts are durably persisted."""

    schema_version: Literal[1] = 1
    terminal_id: str = Field(pattern=_TERMINAL_ID)
    request_id: str = Field(pattern=_REQUEST_ID)
    attempt_id: str = Field(pattern=_ATTEMPT_ID)
    attempt_index: int = Field(ge=0, strict=True)
    status: TerminalStatus
    exit_code: int | None
    started_at: datetime.datetime
    completed_at: datetime.datetime
    latency_ms: int = Field(ge=0, strict=True)
    stdout_artifact: str
    stdout_sha256: str = Field(pattern=_HEX64)
    stderr_artifact: str
    stderr_sha256: str = Field(pattern=_HEX64)
    final_message_artifact: str | None
    final_message_sha256: str | None = Field(default=None, pattern=_HEX64)
    parsed_output: dict[str, object] | None
    parsed_output_hash: str | None = Field(default=None, pattern=_HEX64)
    usage: CodexUsageV1 | None
    thread_id: str | None
    event_count: int = Field(ge=0, strict=True)
    redaction_count: int = Field(ge=0, strict=True)
    stderr_nonempty: bool
    replayed: Literal[False] = False
    prior_probe_input_tokens: int = Field(ge=1, strict=True)
    fixed_context_overhead_warning: Literal[
        "codex_exec_has_large_fixed_context_overhead_use_for_high_value_proposals_not_bulk"
    ]
    semantic_labels_created: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    supervision_eligible: Literal[False] = False
    heldout_or_unseen_claimed: Literal[False] = False
    operational_validity_only: Literal[True] = True
    error_detail: str | None = None

    def id_payload(self) -> dict[str, object]:
        excluded = {"terminal_id", "started_at", "completed_at", "latency_ms", "replayed"}
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key not in excluded
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.started_at)
        require_utc(self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("terminal completion precedes start")
        expected = "codex_exec_terminal_v1:" + hash_canonical(
            {"schema": "codex_exec_terminal_v1", **self.id_payload()}
        )
        if self.terminal_id != expected:
            raise ValueError("terminal_id does not match terminal payload")
        if self.status == "success":
            if (
                self.exit_code != 0
                or self.parsed_output is None
                or self.parsed_output_hash is None
                or self.usage is None
                or self.final_message_artifact is None
                or self.final_message_sha256 is None
                or self.error_detail is not None
            ):
                raise ValueError("successful terminal lacks a complete valid result")
        elif self.parsed_output is not None or self.parsed_output_hash is not None:
            raise ValueError("failed terminal cannot expose parsed output")
        if (self.final_message_artifact is None) != (self.final_message_sha256 is None):
            raise ValueError("final message path and hash must be present together")
        return self


@dataclass(frozen=True, slots=True)
class ProcessCapture:
    status: Literal["completed", "timeout", "interrupted"]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    final_message: bytes | None
    started_at: datetime.datetime
    completed_at: datetime.datetime


class CodexProcessExecutor(Protocol):
    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> ProcessCapture: ...


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _terminate_group(process: subprocess.Popen[bytes], *, grace: int) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


class SubprocessCodexExecutor:
    """Real subprocess implementation: argv only, prompt on stdin, process group."""

    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> ProcessCapture:
        if final_message_path.exists() or final_message_path.is_symlink():
            raise CodexExecArtifactConflict("final-message path must not exist before launch")
        started = _utcnow()
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        status: Literal["completed", "timeout", "interrupted"] = "completed"
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            status = "timeout"
            _terminate_group(process, grace=termination_grace_seconds)
            stdout, stderr = process.communicate()
        except KeyboardInterrupt:
            status = "interrupted"
            _terminate_group(process, grace=termination_grace_seconds)
            stdout, stderr = process.communicate()
        completed = _utcnow()
        final: bytes | None = None
        if final_message_path.exists():
            if final_message_path.is_symlink() or not final_message_path.is_file():
                raise CodexExecArtifactConflict("final-message output is not a regular file")
            final = final_message_path.read_bytes()
        return ProcessCapture(
            status=status,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            final_message=final,
            started_at=started,
            completed_at=completed,
        )


class MockCodexExecutor:
    """Offline executor used by unit tests and the explicit mock probe."""

    def __init__(self, capture: ProcessCapture) -> None:
        self.capture = capture
        self.calls = 0

    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> ProcessCapture:
        del timeout_seconds, termination_grace_seconds
        self.calls += 1
        if not argv or argv[-1] != "-" or not prompt or any(cwd.iterdir()):
            raise CodexExecProviderError("mock observed invalid argv/stdin/isolated-cwd contract")
        if final_message_path.exists():
            raise CodexExecArtifactConflict("mock final-message path was stale")
        if self.capture.final_message is not None:
            final_message_path.write_bytes(self.capture.final_message)
        return self.capture


def _redact_stream(raw: bytes) -> tuple[bytes, int]:
    text = raw.decode("utf-8", errors="replace")
    count = 0
    values = sorted(
        {
            value
            for name, value in os.environ.items()
            if value and len(value) >= 8 and _SECRET_NAME.search(name)
        },
        key=len,
        reverse=True,
    )
    for value in values:
        occurrences = text.count(value)
        if occurrences:
            text = text.replace(value, "[REDACTED]")
            count += occurrences
    for pattern in _GENERIC_SECRETS:
        text, observed = pattern.subn("[REDACTED]", text)
        count += observed
    return text.encode("utf-8"), count


def _immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise CodexExecArtifactConflict(f"immutable artifact conflict: {path}")
        path.chmod(0o600)
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
                raise CodexExecArtifactConflict(f"concurrent immutable conflict: {path}") from None
        path.chmod(0o600)
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_record(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _build_argv(config: CodexExecProviderConfigV1) -> tuple[str, ...]:
    return (
        "codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--json",
        "--model",
        config.model,
        "--config",
        f'model_reasoning_effort="{config.reasoning_effort}"',
        "--config",
        'web_search="disabled"',
        "--config",
        'shell_environment_policy.inherit="none"',
        "--output-schema",
        "../../../inputs/output_schema.json",
        "--output-last-message",
        "../final_message.json",
        "-",
    )


def make_codex_exec_request_v1(
    loaded: LoadedCodexExecConfigV1,
    *,
    execution_mode: Literal["mock", "external"],
    input_ids: tuple[str, ...],
    reference_hidden: bool,
    private_source_content: bool,
    external_provider_eligible: bool,
) -> CodexExecRequestV1:
    """Create the request only after applying the external-provider privacy guard."""

    if private_source_content or not external_provider_eligible:
        raise CodexExecPrivacyError(
            "Codex exec accepts only public, external-provider-eligible inputs"
        )
    values: dict[str, object] = {
        "schema_version": 1,
        "execution_mode": execution_mode,
        "provider": loaded.config.provider,
        "model_family": loaded.config.model_family,
        "model": loaded.config.model,
        "reasoning_effort": loaded.config.reasoning_effort,
        "codex_cli_version": loaded.config.codex_cli_version,
        "codex_binary_sha256": loaded.config.codex_binary_sha256,
        "config_artifact": str(loaded.path),
        "config_file_sha256": loaded.config_file_sha256,
        "effective_config_hash": loaded.effective_config_hash,
        "prompt_artifact": loaded.config.prompt_artifact,
        "prompt_sha256": loaded.config.prompt_sha256,
        "output_schema_artifact": loaded.config.output_schema_artifact,
        "output_schema_sha256": loaded.config.output_schema_sha256,
        "input_ids": input_ids,
        "private_source_content": False,
        "external_provider_eligible": True,
        "reference_hidden": reference_hidden,
        "timeout_seconds": loaded.config.timeout_seconds,
        "termination_grace_seconds": loaded.config.termination_grace_seconds,
        "argv": _build_argv(loaded.config),
        "immutable_model_revision_available": False,
        "contamination_status": loaded.config.contamination_status,
        "semantic_labels_created": False,
        "gate_credit_claimed": False,
        "supervision_eligible": False,
    }
    request_id = "codex_exec_request_v1:" + hash_canonical(
        {"schema": "codex_exec_request_v1", **values}
    )
    return CodexExecRequestV1.model_validate({"request_id": request_id, **values})


def _attempt(request: CodexExecRequestV1, attempt_index: int) -> CodexExecAttemptV1:
    attempt_id = "codex_exec_attempt_v1:" + hash_canonical(
        {
            "schema": "codex_exec_attempt_v1",
            "request_id": request.request_id,
            "attempt_index": attempt_index,
        }
    )
    return CodexExecAttemptV1(
        attempt_id=attempt_id,
        request_id=request.request_id,
        attempt_index=attempt_index,
        argv=request.argv,
        timeout_seconds=request.timeout_seconds,
        termination_grace_seconds=request.termination_grace_seconds,
    )


@dataclass(frozen=True, slots=True)
class ParsedEvents:
    status: TerminalStatus | None
    error_detail: str | None
    final_text: bytes | None
    usage: CodexUsageV1 | None
    thread_id: str | None
    event_count: int


def _parse_events(stdout: bytes) -> ParsedEvents:
    if len(stdout) > _MAX_STREAM_BYTES:
        return ParsedEvents("stdout_invalid", "stdout exceeds size limit", None, None, None, 0)
    if not stdout or not stdout.endswith(b"\n"):
        return ParsedEvents(
            "stdout_invalid", "stdout is empty or has a partial final line", None, None, None, 0
        )
    lines = stdout.splitlines()
    if len(lines) > _MAX_EVENTS:
        return ParsedEvents(
            "stdout_invalid", "event count exceeds limit", None, None, None, len(lines)
        )
    events: list[dict[str, object]] = []
    for index, line in enumerate(lines):
        if not line or len(line) > _MAX_EVENT_BYTES:
            return ParsedEvents(
                "stdout_invalid", f"invalid event line {index}", None, None, None, len(lines)
            )
        try:
            event = _json_load_strict(line, what=f"JSONL event {index}")
        except CodexExecConfigError as exc:
            return ParsedEvents("stdout_invalid", str(exc), None, None, None, len(lines))
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return ParsedEvents(
                "stdout_invalid",
                f"event {index} is not a typed object",
                None,
                None,
                None,
                len(lines),
            )
        events.append(event)

    types = [str(event["type"]) for event in events]
    unknown = [value for value in types if value not in _ALLOWED_EVENT_TYPES]
    if unknown:
        return ParsedEvents(
            "protocol_error", f"unknown/failure events: {unknown}", None, None, None, len(events)
        )
    if types.count("thread.started") != 1 or types.count("turn.started") != 1:
        return ParsedEvents(
            "protocol_error",
            "requires exactly one thread.started and turn.started",
            None,
            None,
            None,
            len(events),
        )
    if types.count("turn.completed") != 1 or types[-1] != "turn.completed":
        return ParsedEvents(
            "protocol_error",
            "requires exactly one final turn.completed",
            None,
            None,
            None,
            len(events),
        )

    messages: list[bytes] = []
    thread_id: str | None = None
    usage: CodexUsageV1 | None = None
    for event in events:
        event_type = str(event["type"])
        if event_type == "thread.started":
            candidate = event.get("thread_id")
            if not isinstance(candidate, str) or not candidate:
                return ParsedEvents(
                    "protocol_error",
                    "thread.started lacks thread_id",
                    None,
                    None,
                    None,
                    len(events),
                )
            thread_id = candidate
        if event_type.startswith("item."):
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                return ParsedEvents(
                    "protocol_error",
                    "item event lacks typed item",
                    None,
                    None,
                    thread_id,
                    len(events),
                )
            item_type = str(item["type"])
            if item_type not in _ALLOWED_ITEM_TYPES:
                return ParsedEvents(
                    "tool_item_rejected",
                    f"rejected item type {item_type!r}",
                    None,
                    None,
                    thread_id,
                    len(events),
                )
            if event_type == "item.completed" and item_type == "agent_message":
                text = item.get("text")
                if not isinstance(text, str):
                    return ParsedEvents(
                        "protocol_error",
                        "agent_message lacks text",
                        None,
                        None,
                        thread_id,
                        len(events),
                    )
                messages.append(text.encode("utf-8"))
        if event_type == "turn.completed":
            value = event.get("usage")
            try:
                usage = CodexUsageV1.model_validate(value)
            except Exception:
                return ParsedEvents(
                    "usage_missing_or_invalid",
                    "turn.completed usage invalid",
                    None,
                    None,
                    thread_id,
                    len(events),
                )
    if not messages:
        return ParsedEvents(
            "missing_final_answer",
            "no completed agent_message",
            None,
            usage,
            thread_id,
            len(events),
        )
    if len(messages) != 1:
        return ParsedEvents(
            "multiple_final_answers",
            f"observed {len(messages)} completed agent messages",
            None,
            usage,
            thread_id,
            len(events),
        )
    return ParsedEvents(None, None, messages[0], usage, thread_id, len(events))


def _validate_schema(value: object, schema: object, *, path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"{path} schema must be an object")
    allowed = {
        "$schema",
        "title",
        "description",
        "type",
        "const",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
    }
    unsupported = sorted(set(schema) - allowed)
    if unsupported:
        raise ValueError(f"{path} unsupported schema keywords: {unsupported}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} differs from const")
    if "enum" in schema:
        enum_values = schema["enum"]
        if not isinstance(enum_values, list):
            raise ValueError(f"{path} enum must be a list")
        if value not in enum_values:
            raise ValueError(f"{path} not in enum")
    kind = schema.get("type")
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, int | float) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if not isinstance(kind, str) or kind not in checks or not checks[kind](value):
        raise ValueError(f"{path} does not have required type {kind!r}")
    if kind == "object":
        assert isinstance(value, dict)
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError(f"{path} invalid object schema")
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"{path} missing required properties {missing}")
        extras = set(value) - set(properties)
        if schema.get("additionalProperties") is False and extras:
            raise ValueError(f"{path} has additional properties {sorted(extras)}")
        for key, item in value.items():
            if key in properties:
                _validate_schema(item, properties[key], path=f"{path}.{key}")
    elif kind == "array":
        assert isinstance(value, list)
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} shorter than minItems")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} longer than maxItems")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate_schema(item, schema["items"], path=f"{path}[{index}]")
    elif kind == "string":
        assert isinstance(value, str)
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        pattern = schema.get("pattern")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} shorter than minLength")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} longer than maxLength")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise ValueError(f"{path} does not match pattern")


def _terminal(
    *,
    attempt: CodexExecAttemptV1,
    status: TerminalStatus,
    capture: ProcessCapture,
    stdout_artifact: str,
    stdout_sha256: str,
    stderr_artifact: str,
    stderr_sha256: str,
    final_message_artifact: str | None,
    final_message_sha256: str | None,
    parsed_output: dict[str, object] | None,
    usage: CodexUsageV1 | None,
    thread_id: str | None,
    event_count: int,
    redaction_count: int,
    prior_probe_input_tokens: int,
    error_detail: str | None,
) -> CodexExecTerminalV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "request_id": attempt.request_id,
        "attempt_id": attempt.attempt_id,
        "attempt_index": attempt.attempt_index,
        "status": status,
        "exit_code": capture.exit_code,
        "started_at": capture.started_at,
        "completed_at": capture.completed_at,
        "latency_ms": max(
            0, int((capture.completed_at - capture.started_at).total_seconds() * 1000)
        ),
        "stdout_artifact": stdout_artifact,
        "stdout_sha256": stdout_sha256,
        "stderr_artifact": stderr_artifact,
        "stderr_sha256": stderr_sha256,
        "final_message_artifact": final_message_artifact,
        "final_message_sha256": final_message_sha256,
        "parsed_output": parsed_output,
        "parsed_output_hash": None if parsed_output is None else hash_canonical(parsed_output),
        "usage": None if usage is None else usage.model_dump(mode="json"),
        "thread_id": thread_id,
        "event_count": event_count,
        "redaction_count": redaction_count,
        "stderr_nonempty": bool(capture.stderr),
        "replayed": False,
        "prior_probe_input_tokens": prior_probe_input_tokens,
        "fixed_context_overhead_warning": _OVERHEAD_WARNING,
        "semantic_labels_created": False,
        "gate_credit_claimed": False,
        "supervision_eligible": False,
        "heldout_or_unseen_claimed": False,
        "operational_validity_only": True,
        "error_detail": error_detail,
    }
    excluded = {"started_at", "completed_at", "latency_ms", "replayed"}
    id_payload = {key: value for key, value in values.items() if key not in excluded}
    terminal_id = "codex_exec_terminal_v1:" + hash_canonical(
        {"schema": "codex_exec_terminal_v1", **id_payload}
    )
    return CodexExecTerminalV1.model_validate({"terminal_id": terminal_id, **values})


@dataclass(frozen=True, slots=True)
class CodexExecRunV1:
    run_directory: Path
    request: CodexExecRequestV1
    attempt: CodexExecAttemptV1
    terminal: CodexExecTerminalV1
    replayed: bool


def _load_terminal(path: Path) -> CodexExecTerminalV1:
    try:
        record = CodexExecTerminalV1.model_validate(
            _json_load_strict(path.read_bytes(), what="terminal record")
        )
    except OSError as exc:
        raise CodexExecArtifactConflict(f"cannot read terminal: {exc}") from exc
    if path.read_bytes() != _canonical_record(record):
        raise CodexExecArtifactConflict("terminal record is not canonical")
    return record


def execute_codex_exec_v1(
    loaded: LoadedCodexExecConfigV1,
    request: CodexExecRequestV1,
    *,
    output_root: Path,
    attempt_index: int,
    executor: CodexProcessExecutor,
) -> CodexExecRunV1:
    """Execute or immutably replay exactly one Codex attempt."""

    attempt = _attempt(request, attempt_index)
    run_dir = output_root / request.request_id
    attempt_dir = (
        run_dir
        / "attempts"
        / (f"{attempt_index:04d}-{attempt.attempt_id.removeprefix('codex_exec_attempt_v1:')}")
    )
    terminal_path = attempt_dir / "terminal.json"
    if terminal_path.exists():
        terminal = _load_terminal(terminal_path)
        if terminal.request_id != request.request_id or terminal.attempt_id != attempt.attempt_id:
            raise CodexExecArtifactConflict("terminal lineage differs from requested attempt")
        for artifact, digest in (
            (terminal.stdout_artifact, terminal.stdout_sha256),
            (terminal.stderr_artifact, terminal.stderr_sha256),
        ):
            path = attempt_dir / artifact
            if hash_file(path) != digest:
                raise CodexExecArtifactConflict(f"replay artifact hash mismatch: {artifact}")
        if terminal.final_message_artifact is not None:
            assert terminal.final_message_sha256 is not None
            if (
                hash_file(attempt_dir / terminal.final_message_artifact)
                != terminal.final_message_sha256
            ):
                raise CodexExecArtifactConflict("replay final-message hash mismatch")
        return CodexExecRunV1(run_dir, request, attempt, terminal, True)

    claim = attempt_dir / "CLAIM"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CodexExecPartialAttempt(
            "attempt already allocated without terminal; use a new attempt_index"
        ) from exc
    os.close(descriptor)

    _immutable(run_dir / "inputs" / "config.json", loaded.path.read_bytes())
    _immutable(run_dir / "inputs" / "prompt.txt", loaded.prompt)
    _immutable(run_dir / "inputs" / "output_schema.json", loaded.output_schema)
    _immutable(run_dir / "request.json", _canonical_record(request))
    _immutable(attempt_dir / "attempt.json", _canonical_record(attempt))
    workspace = attempt_dir / "workspace"
    workspace.mkdir()
    final_path = attempt_dir / "final_message.json"

    capture = executor.execute(
        argv=attempt.argv,
        prompt=loaded.prompt,
        cwd=workspace,
        final_message_path=final_path,
        timeout_seconds=attempt.timeout_seconds,
        termination_grace_seconds=attempt.termination_grace_seconds,
    )
    stdout, stdout_redactions = _redact_stream(capture.stdout)
    stderr, stderr_redactions = _redact_stream(capture.stderr)
    final: bytes | None = None
    final_redactions = 0
    if capture.final_message is not None:
        final, final_redactions = _redact_stream(capture.final_message)
    redactions = stdout_redactions + stderr_redactions + final_redactions

    stdout_hash = _immutable(attempt_dir / "stdout.jsonl", stdout)
    stderr_hash = _immutable(attempt_dir / "stderr.txt", stderr)
    final_hash = None
    if final is not None:
        final_hash = _immutable(final_path, final)

    parsed = _parse_events(stdout)
    status: TerminalStatus
    error: str | None
    structured: dict[str, object] | None = None
    if capture.status == "timeout":
        status, error = "timeout", "process exceeded timeout and process group was terminated"
    elif capture.status == "interrupted":
        status, error = "interrupted", "process was interrupted and process group was terminated"
    elif redactions:
        status, error = "secret_redacted", "captured streams contained redacted secret material"
    elif capture.exit_code != 0:
        status, error = "process_error", f"codex exited with {capture.exit_code}"
    elif parsed.status is not None:
        status, error = parsed.status, parsed.error_detail
    elif final is None:
        status, error = "final_output_missing", "--output-last-message did not create a fresh file"
    elif parsed.final_text != final:
        status, error = "final_output_mismatch", "JSONL final answer differs from fresh output file"
    else:
        try:
            value = _json_load_strict(final, what="final answer")
        except CodexExecConfigError as exc:
            status, error = "final_answer_unparseable", str(exc)
        else:
            try:
                _validate_schema(value, loaded.schema_document)
            except ValueError as exc:
                status, error = "schema_violation", str(exc)
            else:
                if not isinstance(value, dict):
                    status, error = "schema_violation", "final answer must be a JSON object"
                else:
                    status, error, structured = "success", None, value

    terminal = _terminal(
        attempt=attempt,
        status=status,
        capture=capture,
        stdout_artifact="stdout.jsonl",
        stdout_sha256=stdout_hash,
        stderr_artifact="stderr.txt",
        stderr_sha256=stderr_hash,
        final_message_artifact=None if final is None else "final_message.json",
        final_message_sha256=final_hash,
        parsed_output=structured,
        usage=parsed.usage,
        thread_id=parsed.thread_id,
        event_count=parsed.event_count,
        redaction_count=redactions,
        prior_probe_input_tokens=loaded.config.prior_probe_observation.input_tokens,
        error_detail=error,
    )
    _immutable(terminal_path, _canonical_record(terminal))
    return CodexExecRunV1(run_dir, request, attempt, terminal, False)


def verify_codex_cli_pin(config: CodexExecProviderConfigV1) -> None:
    """Fail before provider execution if CLI version or binary bytes drift."""

    result = subprocess.run(
        ["codex", "--version"],
        check=False,
        capture_output=True,
        timeout=30,
        shell=False,
    )
    observed = result.stdout.decode("utf-8", errors="strict").strip()
    if result.returncode != 0 or observed != config.codex_cli_version:
        raise CodexExecConfigError(
            f"codex version mismatch: observed={observed!r}, expected={config.codex_cli_version!r}"
        )
    binary_text = shutil.which("codex")
    if binary_text is None:
        raise CodexExecConfigError("codex executable is not on PATH")
    binary = Path(binary_text)
    if hash_file(binary.resolve()) != config.codex_binary_sha256:
        raise CodexExecConfigError("codex executable SHA-256 differs from frozen pin")
