"""Fail-closed GPT-5.6 Sol xhigh bridge for LF-022 blinded judge cells.

The bridge is intentionally narrow.  It consumes exactly one slot of an
already prepared, public-source generic LF-022 weak batch, executes exact AB
and BA cells in bounded shards, and publishes canonical ``ProviderRawResponse``
artifacts for the existing offline weak-batch replay.  It does not create a
semantic, silver, human, training, evaluation, or gate label.

Offline replay and live execution are separate entry points.  Live execution
requires a canonical, time-bounded authorization artifact and an explicit
boolean at the call site.  Merely importing this module or invoking the replay
entry point can never call Codex.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import resource
import shutil
import signal
import stat
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation.capture_redaction import redact_captured_streams
from leanfaith.generation.lf022_weak_batch import (
    LF022WeakDispatchRecord,
    _endpoint,
    _load_prepared_batch,
    _verify_dispatch_request,
    persist_lf022_weak_execution_started_marker,
)
from leanfaith.generation.providers import (
    DecodingValue,
    ProviderIdentity,
    ProviderRawResponse,
    ProviderRequest,
    load_provider_raw_response,
    load_provider_request,
    persist_provider_raw_response,
    provider_raw_response_path,
)
from leanfaith.generation.weak_supervision import (
    JudgeOutputParseError,
    JudgeResponse,
    JudgeSlot,
    parse_blinded_judge_output,
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.manifest import require_utc

SOL_JUDGE_METHOD_VERSION: Literal["lf022_codex_sol_judge_v1"] = "lf022_codex_sol_judge_v1"
SOL_PROVIDER: Literal["openai_codex_exec"] = "openai_codex_exec"
SOL_FAMILY: Literal["openai_codex_sol"] = "openai_codex_sol"
SOL_MODEL: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
SOL_REGISTRY_MODEL: Literal["openai/gpt-5.6-sol"] = "openai/gpt-5.6-sol"
SOL_REASONING_EFFORT: Literal["xhigh"] = "xhigh"
_SYSTEM_PROMPT = (
    "You are a mathematical-semantics judge. Follow the supplied LeanFaith "
    "instructions exactly and return only the JSON object required by the "
    "supplied schema. Do not use tools."
)
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
_EXPECTED_CHILD_ENV_ALLOWLIST = (
    "ALL_PROXY",
    "CODEX_HOME",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
)
_MAX_CODEX_AUTH_BYTES = 1024 * 1024
_CODEX_AUTH_SECRET_KEY = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|"
    r"PRIVATE[_-]?KEY|CREDENTIAL|AUTH)",
    flags=re.IGNORECASE,
)


class CodexSolJudgeError(RuntimeError):
    """A configuration, privacy, process, artifact, or replay invariant failed."""


class CodexSolAuthorizationError(CodexSolJudgeError):
    """The explicit public live-execution authorization is absent or invalid."""


class CodexSolPartialAttemptError(CodexSolJudgeError):
    """An interrupted attempt cannot be safely recalled or advanced automatically."""


class CodexSolJudgeConfig(StrictModel):
    """Exact local executable and bounded external execution contract."""

    schema_version: Literal[1] = 1
    config_id: Literal["lf022_codex_sol_judge_v1"]
    status: Literal["live_smoke_passed_scale_not_yet_qualified"]
    provider: Literal["openai_codex_exec"]
    model_family: Literal["openai_codex_sol"]
    model: Literal["gpt-5.6-sol"]
    registry_model_id: Literal["openai/gpt-5.6-sol"]
    reasoning_effort: Literal["xhigh"]
    provider_catalog_sha256: str = Field(pattern=HEX64_PATTERN)
    codex_cli_version: str = Field(min_length=1)
    codex_binary_sha256: str = Field(pattern=HEX64_PATTERN)
    server_model_revision_status: Literal["unavailable_floating_provider_alias"]
    system_prompt_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_template_sha256: str = Field(pattern=HEX64_PATTERN)
    output_schema_sha256: str = Field(pattern=HEX64_PATTERN)
    max_pairs_per_invocation: int = Field(ge=1, le=64, strict=True)
    maximum_concurrency: Literal[1] = 1
    max_attempts_per_cell: int = Field(ge=1, le=3, strict=True)
    timeout_seconds: int = Field(ge=1, le=7200, strict=True)
    termination_grace_seconds: int = Field(ge=1, le=60, strict=True)
    max_stdout_bytes: int = Field(ge=1024, le=64 * 1024 * 1024, strict=True)
    max_stderr_bytes: int = Field(ge=1024, le=64 * 1024 * 1024, strict=True)
    max_final_message_bytes: int = Field(ge=1024, le=16 * 1024 * 1024, strict=True)
    shell_tool_disabled: Literal[True] = True
    sandbox: Literal["read-only"] = "read-only"
    web_search: Literal["disabled"] = "disabled"
    child_environment_allowlist: tuple[str, ...]
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    both_orientations_required: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    human_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _frozen_wire_contract(self) -> Self:
        if self.system_prompt_sha256 != hash_canonical({"system_prompt": _SYSTEM_PROMPT}):
            raise ValueError("system_prompt_sha256 differs from the adapter system prompt")
        if self.output_schema_sha256 != sha256_hex(_output_schema_bytes()):
            raise ValueError("output_schema_sha256 differs from the adapter schema")
        if self.child_environment_allowlist != _EXPECTED_CHILD_ENV_ALLOWLIST:
            raise ValueError("child environment allowlist differs from the reviewed contract")
        return self

    @property
    def endpoint_revision(self) -> str:
        return f"provider-deployment-snapshot:{self.provider_catalog_sha256}"

    @property
    def endpoint_decoding(self) -> dict[str, DecodingValue]:
        return {
            "reasoning_effort": self.reasoning_effort,
            "structured_output": True,
            "system_prompt_sha256": self.system_prompt_sha256,
            "output_schema_sha256": self.output_schema_sha256,
            "codex_cli_version": self.codex_cli_version,
            "codex_binary_sha256": self.codex_binary_sha256,
            "shell_tool_disabled": True,
            "sandbox": self.sandbox,
            "web_search": self.web_search,
        }


@dataclass(frozen=True, slots=True)
class LoadedCodexSolJudgeConfig:
    config: CodexSolJudgeConfig
    path: Path
    sha256: str


class CodexSolLiveAuthorization(StrictModel):
    """Reviewed, time-bounded authorization for one exact public shard."""

    schema_version: Literal[1] = 1
    authorization_id: str = Field(pattern=id_pattern("lf022_sol_live_authorization"))
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    config_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_slot: Literal["judge_A"] = "judge_A"
    shard_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    offset_pairs: int = Field(ge=0, strict=True)
    limit_pairs: int = Field(ge=1, le=64, strict=True)
    authorization_nonce_sha256: str = Field(pattern=HEX64_PATTERN)
    approved_at: datetime.datetime
    expires_at: datetime.datetime
    approved_by: str = Field(min_length=1, max_length=200)
    public_external_execution_authorized: Literal[True]
    private_source_content_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.approved_at)
        require_utc(self.expires_at)
        if self.expires_at <= self.approved_at:
            raise ValueError("live authorization must expire after approval")
        expected = make_id(
            "lf022_sol_live_authorization",
            self.model_dump(mode="json", exclude={"authorization_id"}),
        )
        if self.authorization_id != expected:
            raise ValueError("authorization_id differs from authorization content")
        return self


CodexSolAttemptStatus = Literal[
    "completed",
    "timeout",
    "interrupted",
    "process_failed",
    "capture_too_large",
    "final_output_missing",
    "protocol_failed",
    "judge_parse_failed",
    "secret_redacted",
]


class CodexSolProcessCapture(StrictModel):
    """Exact bounded result of one shell-free Codex process."""

    status: Literal["completed", "timeout", "interrupted"]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    final_message: bytes | None


class CodexSolCliExecutor(Protocol):
    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        max_final_message_bytes: int,
        child_env: Mapping[str, str],
    ) -> CodexSolProcessCapture: ...


def _set_child_file_limit(max_bytes: int) -> None:
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))


class SubprocessCodexSolCliExecutor:
    """Shell-free Codex execution with bounded files and group cleanup."""

    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        max_final_message_bytes: int,
        child_env: Mapping[str, str],
    ) -> CodexSolProcessCapture:
        if final_message_path.exists():
            raise CodexSolJudgeError("final-message path must be fresh")
        hard_file_limit = max(max_stdout_bytes, max_stderr_bytes, max_final_message_bytes) + 1
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                tuple(argv),
                cwd=cwd,
                env=dict(child_env),
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
                preexec_fn=lambda: _set_child_file_limit(hard_file_limit),
            )
            status: Literal["completed", "timeout", "interrupted"] = "completed"
            try:
                process.communicate(input=prompt, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                status = "timeout"
                _terminate_process_group(process, termination_grace_seconds)
                process.wait()
            except KeyboardInterrupt:
                status = "interrupted"
                _terminate_process_group(process, termination_grace_seconds)
                process.wait()
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(max_stdout_bytes + 1)
            stderr = stderr_file.read(max_stderr_bytes + 1)
        final_message: bytes | None = None
        if final_message_path.is_file() and not final_message_path.is_symlink():
            with final_message_path.open("rb") as handle:
                final_message = handle.read(max_final_message_bytes + 1)
        return CodexSolProcessCapture(
            status=status,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            final_message=final_message,
        )


def _terminate_process_group(process: subprocess.Popen[bytes], grace_seconds: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace_seconds)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)


class CodexSolAttemptTerminal(StrictModel):
    """Immutable terminal for one attempt-specific provider request."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_codex_sol_judge_v1"] = SOL_JUDGE_METHOD_VERSION
    terminal_id: str = Field(pattern=id_pattern("lf022_sol_terminal"))
    dispatch_cell_id: str = Field(pattern=id_pattern("lf022_weak_cell"))
    provider_request_hash: str = Field(pattern=HEX64_PATTERN)
    provider_attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    attempt_index: int = Field(ge=0, strict=True)
    status: CodexSolAttemptStatus
    exit_code: int | None
    argv_sha256: str = Field(pattern=HEX64_PATTERN)
    provider_request_sha256: str = Field(pattern=HEX64_PATTERN)
    prompt_sha256: str = Field(pattern=HEX64_PATTERN)
    output_schema_sha256: str = Field(pattern=HEX64_PATTERN)
    stdout_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    stderr_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    final_message_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    redaction_report_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    redaction_count: int = Field(ge=0, strict=True)
    parsed_response_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    attempt_raw_response_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    replay_bridge_raw_response_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    error: str | None = None
    semantic_label_created: Literal[False] = False
    human_label_created: Literal[False] = False
    silver_promoted: Literal[False] = False
    train_eligible: Literal[False] = False
    eval_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _terminal_contract(self) -> Self:
        completed_fields = (
            self.stdout_sha256,
            self.stderr_sha256,
            self.final_message_sha256,
            self.redaction_report_sha256,
            self.parsed_response_sha256,
            self.attempt_raw_response_sha256,
            self.replay_bridge_raw_response_sha256,
        )
        if self.status == "completed":
            if self.exit_code != 0 or any(value is None for value in completed_fields):
                raise ValueError("completed terminal requires exit 0 and all artifacts")
            if self.error is not None:
                raise ValueError("completed terminal cannot carry an error")
        elif any(
            value is not None
            for value in (
                self.parsed_response_sha256,
                self.attempt_raw_response_sha256,
                self.replay_bridge_raw_response_sha256,
            )
        ):
            raise ValueError("non-completed terminal cannot carry parsed/provider responses")
        if self.status == "secret_redacted" and self.redaction_count < 1:
            raise ValueError("secret_redacted requires at least one replacement")
        if self.status != "secret_redacted" and self.redaction_count != 0:
            raise ValueError("redacted captures must fail as secret_redacted")
        expected = make_id(
            "lf022_sol_terminal",
            self.model_dump(mode="json", exclude={"terminal_id"}),
        )
        if self.terminal_id != expected:
            raise ValueError("terminal_id differs from terminal content")
        return self


class CodexSolRunManifest(StrictModel):
    """Atomic, time/nonced summary for one bounded execute or replay run."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_codex_sol_judge_v1"] = SOL_JUDGE_METHOD_VERSION
    run_id: str = Field(pattern=id_pattern("lf022_sol_run"))
    created_at: datetime.datetime
    run_nonce_sha256: str = Field(pattern=HEX64_PATTERN)
    execution_mode: Literal["external", "offline_replay"]
    authorization_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    batch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    config_sha256: str = Field(pattern=HEX64_PATTERN)
    provider: Literal["openai_codex_exec"] = SOL_PROVIDER
    model_family: Literal["openai_codex_sol"] = SOL_FAMILY
    model: Literal["gpt-5.6-sol"] = SOL_MODEL
    reasoning_effort: Literal["xhigh"] = SOL_REASONING_EFFORT
    judge_slot: Literal["judge_A"] = "judge_A"
    shard_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    offset_pairs: int = Field(ge=0, strict=True)
    selected_pair_count: int = Field(ge=1, le=64, strict=True)
    selected_cell_count: int = Field(ge=2, le=128, strict=True)
    invoked_cell_count: int = Field(ge=0, le=128, strict=True)
    reused_cell_count: int = Field(ge=0, le=128, strict=True)
    process_attempt_count: int = Field(ge=0, le=384, strict=True)
    completed_cell_count: int = Field(ge=0, le=128, strict=True)
    exhausted_cell_count: int = Field(ge=0, le=128, strict=True)
    terminal_status_counts: dict[str, int]
    ordered_dispatch_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    all_selected_cells_terminal: Literal[True] = True
    both_orientations_selected: Literal[True] = True
    private_source_content_transmitted: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    human_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _counts_and_identity(self) -> Self:
        require_utc(self.created_at)
        if self.selected_cell_count != 2 * self.selected_pair_count:
            raise ValueError("each selected pair must contribute AB and BA cells")
        if self.invoked_cell_count + self.reused_cell_count != self.selected_cell_count:
            raise ValueError("invoked plus reused cells must equal selected cells")
        if self.completed_cell_count + self.exhausted_cell_count != self.selected_cell_count:
            raise ValueError("completed plus exhausted cells must equal selected cells")
        if sum(self.terminal_status_counts.values()) != self.selected_cell_count:
            raise ValueError("terminal status counts do not reconcile")
        if (self.execution_mode == "external") != (self.authorization_sha256 is not None):
            raise ValueError("only external runs carry a live authorization")
        expected = make_id(
            "lf022_sol_run",
            self.model_dump(mode="json", exclude={"run_id"}),
        )
        if self.run_id != expected:
            raise ValueError("run_id differs from time/nonced run content")
        return self


@dataclass(frozen=True, slots=True)
class CodexSolRunResult:
    manifest: CodexSolRunManifest
    manifest_path: Path
    terminals: tuple[CodexSolAttemptTerminal, ...]


def _judge_output_schema() -> dict[str, object]:
    schema = cast(dict[str, object], JudgeResponse.model_json_schema(by_alias=True))
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not all(isinstance(key, str) for key in properties):
        raise CodexSolJudgeError("JudgeResponse JSON schema lacks string properties")
    schema["required"] = sorted(cast(dict[str, object], properties))
    schema["additionalProperties"] = False
    return schema


def _output_schema_bytes() -> bytes:
    return canonical_json_bytes(_judge_output_schema()) + b"\n"


def load_codex_sol_judge_config(path: Path) -> LoadedCodexSolJudgeConfig:
    loaded: LoadedConfig[CodexSolJudgeConfig] = load_config(path, CodexSolJudgeConfig)
    return LoadedCodexSolJudgeConfig(
        config=loaded.config,
        path=path.resolve(),
        sha256=hash_file(path),
    )


def _safe_absolute(path: Path, *, label: str, allow_missing: bool) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:], start=1):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise CodexSolJudgeError(f"{label} is missing: {current}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise CodexSolJudgeError(f"{label} contains a symlink component: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise CodexSolJudgeError(f"{label} parent is not a directory: {current}")
    return absolute


def _write_immutable(path: Path, payload: bytes, *, label: str) -> str:
    path = _safe_absolute(path, label=label, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_absolute(path, label=label, allow_missing=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise CodexSolJudgeError(f"immutable {label} conflicts at {path}")
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
                raise CodexSolJudgeError(f"concurrent immutable conflict: {path}") from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_atomic(path: Path, payload: bytes) -> None:
    path = _safe_absolute(path, label="run manifest", allow_missing=True)
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
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_canonical_authorization(path: Path) -> tuple[CodexSolLiveAuthorization, str]:
    path = _safe_absolute(path, label="Sol live authorization", allow_missing=False)
    if path.is_symlink() or not path.is_file():
        raise CodexSolAuthorizationError("live authorization is not a regular file")
    raw = path.read_bytes()

    def duplicate_free(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> float:
        raise ValueError(f"non-finite number {value!r}")

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=duplicate_free,
            parse_constant=reject_nonfinite,
        )
        authorization = CodexSolLiveAuthorization.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CodexSolAuthorizationError(f"invalid live authorization: {exc}") from exc
    canonical = canonical_json_bytes(authorization.model_dump(mode="json")) + b"\n"
    if raw != canonical:
        raise CodexSolAuthorizationError("live authorization is not canonical JSON")
    return authorization, sha256_hex(raw)


@contextmanager
def _operation_lock(output_root: Path) -> Iterator[None]:
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = _safe_absolute(
        output_root / ".codex-sol-operation.lock",
        label="Sol operation lock",
        allow_missing=True,
    )
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    with os.fdopen(descriptor, "a+b", buffering=0) as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CodexSolJudgeError(f"another Sol operation holds {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _check_binary(config: CodexSolJudgeConfig) -> Path:
    observed = shutil.which("codex")
    if observed is None:
        raise CodexSolJudgeError("codex executable is not on PATH")
    binary = Path(observed).resolve(strict=True)
    if hash_file(binary) != config.codex_binary_sha256:
        raise CodexSolJudgeError("codex executable hash differs from the frozen pin")
    completed = subprocess.run(
        (str(binary), "--version"),
        check=False,
        capture_output=True,
        timeout=30,
        shell=False,
        env=_child_environment(config),
    )
    version = (completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip()
    if completed.returncode != 0 or version != config.codex_cli_version:
        raise CodexSolJudgeError("codex CLI version differs from the frozen pin")
    return binary


def _child_environment(config: CodexSolJudgeConfig) -> dict[str, str]:
    child = {
        key: os.environ[key] for key in config.child_environment_allowlist if key in os.environ
    }
    child.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb"})
    return child


def _read_codex_auth(child_env: Mapping[str, str]) -> bytes:
    if "CODEX_HOME" in child_env:
        configured_home = child_env["CODEX_HOME"]
        if not configured_home:
            raise CodexSolJudgeError("inherited CODEX_HOME is empty")
        source_home = Path(configured_home)
    else:
        fallback_home = child_env.get("HOME")
        if not fallback_home:
            raise CodexSolJudgeError("Codex authentication requires CODEX_HOME or HOME")
        source_home = Path(fallback_home) / ".codex"
    auth_path = _safe_absolute(
        source_home / "auth.json",
        label="Codex authentication",
        allow_missing=False,
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(auth_path, flags)
    except OSError as exc:
        raise CodexSolJudgeError("Codex authentication is not safely readable") from exc
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise CodexSolJudgeError("Codex authentication is not a regular file")
        if metadata.st_uid != os.geteuid():
            raise CodexSolJudgeError("Codex authentication is not owned by the current user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CodexSolJudgeError("Codex authentication permissions are too broad")
        payload = handle.read(_MAX_CODEX_AUTH_BYTES + 1)
    if not payload:
        raise CodexSolJudgeError("Codex authentication is empty")
    if len(payload) > _MAX_CODEX_AUTH_BYTES:
        raise CodexSolJudgeError("Codex authentication exceeds its transient-copy bound")
    return payload


def _codex_auth_secret_values(payload: bytes) -> tuple[str, ...]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexSolJudgeError("Codex authentication is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise CodexSolJudgeError("Codex authentication must be a JSON object")
    secrets: set[str] = set()

    def collect(value: object, *, secret_context: bool) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise CodexSolJudgeError("Codex authentication has a non-string key")
                collect(
                    child,
                    secret_context=secret_context or _CODEX_AUTH_SECRET_KEY.search(key) is not None,
                )
        elif isinstance(value, list):
            for child in value:
                collect(child, secret_context=secret_context)
        elif isinstance(value, str) and secret_context and len(value.encode("utf-8")) >= 8:
            secrets.add(value)

    collect(parsed, secret_context=False)
    if not secrets:
        raise CodexSolJudgeError("Codex authentication has no redactable credential values")
    return tuple(sorted(secrets, key=lambda item: (-len(item.encode("utf-8")), item)))


def _codex_redaction_environment(*auth_payloads: bytes) -> dict[str, str]:
    environment = dict(os.environ)
    secrets = {secret for payload in auth_payloads for secret in _codex_auth_secret_values(payload)}
    for index, secret in enumerate(
        sorted(secrets, key=lambda item: (-len(item.encode("utf-8")), item))
    ):
        environment[f"LEANFAITH_CODEX_AUTH_SECRET_{index}"] = secret
    return environment


@contextmanager
def _isolated_codex_environment(
    config: CodexSolJudgeConfig,
) -> Iterator[dict[str, str]]:
    """Yield one run-scoped Codex environment containing only transient auth."""

    child_env = _child_environment(config)
    auth_payload = _read_codex_auth(child_env)
    _codex_auth_secret_values(auth_payload)
    with tempfile.TemporaryDirectory(prefix="leanfaith-sol-codex-home-") as temporary:
        isolated_root = _safe_absolute(
            Path(temporary),
            label="isolated Codex root",
            allow_missing=False,
        )
        os.chmod(isolated_root, 0o700)
        isolated_home = isolated_root / "codex-home"
        isolated_user_home = isolated_root / "home"
        isolated_home.mkdir(mode=0o700)
        isolated_user_home.mkdir(mode=0o700)
        os.chmod(isolated_home, 0o700)
        os.chmod(isolated_user_home, 0o700)
        auth_path = isolated_home / "auth.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(auth_path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(auth_payload)
            handle.flush()
            os.fsync(handle.fileno())
        if (
            stat.S_IMODE(isolated_root.stat().st_mode) != 0o700
            or stat.S_IMODE(isolated_home.stat().st_mode) != 0o700
            or stat.S_IMODE(isolated_user_home.stat().st_mode) != 0o700
            or stat.S_IMODE(auth_path.stat().st_mode) != 0o600
            or auth_path.read_bytes() != auth_payload
        ):
            raise CodexSolJudgeError("isolated Codex authentication copy differs")
        isolated_env = dict(child_env)
        isolated_env["CODEX_HOME"] = str(isolated_home)
        isolated_env["HOME"] = str(isolated_user_home)
        yield isolated_env


def _wire_prompt(request: ProviderRequest) -> bytes:
    return (_SYSTEM_PROMPT + "\n\n" + request.rendered_prompt).encode("utf-8")


def _argv(
    *,
    binary: Path,
    config: CodexSolJudgeConfig,
    output_schema_path: Path,
    final_message_path: Path,
) -> tuple[str, ...]:
    return (
        str(binary),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--disable",
        "shell_tool",
        "--sandbox",
        config.sandbox,
        "--skip-git-repo-check",
        "--color",
        "never",
        "--json",
        "--model",
        config.model,
        "-c",
        f'model_reasoning_effort="{config.reasoning_effort}"',
        "-c",
        'cli_auth_credentials_store="file"',
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


def _validate_codex_stdout(stdout: bytes, final_message: bytes) -> str | None:
    if not stdout or not stdout.endswith(b"\n"):
        return "stdout is empty or lacks a final newline"
    events: list[dict[str, object]] = []
    for index, line in enumerate(stdout.splitlines()):
        duplicate: str | None = None

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            nonlocal duplicate
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    duplicate = key
                result[key] = value
            return result

        def nonfinite(value: str) -> float:
            raise ValueError(f"non-finite JSON value {value!r}")

        try:
            parsed = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=pairs,
                parse_constant=nonfinite,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            return f"stdout event {index} is invalid JSON: {exc}"
        if duplicate is not None:
            return f"stdout event {index} contains duplicate key {duplicate!r}"
        if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
            return f"stdout event {index} is not a typed object"
        events.append(parsed)
    event_types = [cast(str, event["type"]) for event in events]
    unknown = [item for item in event_types if item not in _ALLOWED_CODEX_EVENT_TYPES]
    if unknown:
        return f"stdout contains unknown or failure event types: {unknown}"
    if event_types.count("thread.started") != 1 or event_types.count("turn.started") != 1:
        return "stdout requires exactly one thread.started and turn.started event"
    if event_types.count("turn.completed") != 1 or event_types[-1] != "turn.completed":
        return "stdout requires exactly one final turn.completed event"
    messages: list[str] = []
    for event in events:
        event_type = cast(str, event["type"])
        if not event_type.startswith("item."):
            continue
        item = event.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            return "stdout item event lacks a typed item"
        item_type = cast(str, item["type"])
        if item_type not in _ALLOWED_CODEX_ITEM_TYPES:
            return f"stdout rejected tool or item type {item_type!r}"
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


def _parse_final(final_message: bytes) -> JudgeResponse:
    try:
        text = final_message.decode("utf-8")
        return parse_blinded_judge_output(text)
    except (UnicodeDecodeError, JudgeOutputParseError) as exc:
        raise CodexSolJudgeError(f"invalid structured judge response: {exc}") from exc


def _selected_cells(
    dispatches: Sequence[LF022WeakDispatchRecord],
    *,
    slot: JudgeSlot,
    offset_pairs: int,
    limit_pairs: int,
) -> tuple[LF022WeakDispatchRecord, ...]:
    if offset_pairs < 0:
        raise CodexSolJudgeError("offset_pairs must be nonnegative")
    by_pair: dict[str, list[LF022WeakDispatchRecord]] = {}
    pair_ids: list[str] = []
    for dispatch in dispatches:
        if dispatch.judge_slot == slot:
            if dispatch.pair_id not in by_pair:
                pair_ids.append(dispatch.pair_id)
            by_pair.setdefault(dispatch.pair_id, []).append(dispatch)
    selected_pair_ids = pair_ids[offset_pairs : offset_pairs + limit_pairs]
    if len(selected_pair_ids) != limit_pairs:
        raise CodexSolJudgeError("requested shard exceeds prepared slot pair count")
    selected: list[LF022WeakDispatchRecord] = []
    for pair_id in selected_pair_ids:
        cells = by_pair[pair_id]
        if {cell.orientation for cell in cells} != {"AB", "BA"} or len(cells) != 2:
            raise CodexSolJudgeError(f"pair lacks exact AB/BA Sol cells: {pair_id}")
        selected.extend(cells)
    return tuple(selected)


def _attempt_dir(output_root: Path, request_hash: str, attempt_index: int) -> Path:
    return output_root / "items" / request_hash[:2] / request_hash / f"attempt-{attempt_index:02d}"


def _attempt_request(base: ProviderRequest, attempt_index: int) -> ProviderRequest:
    request = ProviderRequest.create(
        identity=ProviderIdentity(
            provider=base.provider,
            model=base.model,
            revision=base.revision,
            transport="external_disabled",
        ),
        prompt_template_hash=base.prompt_template_hash,
        rendered_prompt=base.rendered_prompt,
        decoding=base.decoding,
        input_ids=base.input_ids,
        private_source_content=base.private_source_content,
        attempt_index=attempt_index,
    )
    if request.request_hash != base.request_hash:
        raise CodexSolJudgeError("attempt-specific request changed semantic request hash")
    return request


class CodexSolProcessReceipt(StrictModel):
    """Written after raw process files and before any semantic parsing."""

    schema_version: Literal[1] = 1
    receipt_id: str = Field(pattern=id_pattern("lf022_sol_process_receipt"))
    provider_attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    status: Literal["completed", "timeout", "interrupted"]
    exit_code: int | None
    stdout_sha256: str = Field(pattern=HEX64_PATTERN)
    stderr_sha256: str = Field(pattern=HEX64_PATTERN)
    final_message_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    redaction_report_sha256: str = Field(pattern=HEX64_PATTERN)
    redaction_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _identity(self) -> Self:
        expected = make_id(
            "lf022_sol_process_receipt",
            self.model_dump(mode="json", exclude={"receipt_id"}),
        )
        if self.receipt_id != expected:
            raise ValueError("receipt_id differs from receipt content")
        return self


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _terminal_values(
    *,
    dispatch: LF022WeakDispatchRecord,
    request: ProviderRequest,
    status: CodexSolAttemptStatus,
    exit_code: int | None,
    argv_sha256: str,
    provider_request_sha256: str,
    prompt_sha256: str,
    output_schema_sha256: str,
    stdout_sha256: str | None,
    stderr_sha256: str | None,
    final_message_sha256: str | None,
    redaction_report_sha256: str | None,
    redaction_count: int,
    parsed_response_sha256: str | None,
    attempt_raw_response_sha256: str | None,
    replay_bridge_raw_response_sha256: str | None,
    error: str | None,
) -> CodexSolAttemptTerminal:
    values: dict[str, object] = {
        "schema_version": 1,
        "method_version": SOL_JUDGE_METHOD_VERSION,
        "dispatch_cell_id": dispatch.dispatch_cell_id,
        "provider_request_hash": request.request_hash,
        "provider_attempt_id": request.attempt_id,
        "attempt_index": request.attempt_index,
        "status": status,
        "exit_code": exit_code,
        "argv_sha256": argv_sha256,
        "provider_request_sha256": provider_request_sha256,
        "prompt_sha256": prompt_sha256,
        "output_schema_sha256": output_schema_sha256,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "final_message_sha256": final_message_sha256,
        "redaction_report_sha256": redaction_report_sha256,
        "redaction_count": redaction_count,
        "parsed_response_sha256": parsed_response_sha256,
        "attempt_raw_response_sha256": attempt_raw_response_sha256,
        "replay_bridge_raw_response_sha256": replay_bridge_raw_response_sha256,
        "error": error,
        "semantic_label_created": False,
        "human_label_created": False,
        "silver_promoted": False,
        "train_eligible": False,
        "eval_eligible": False,
        "gate_credit_claimed": False,
    }
    return CodexSolAttemptTerminal.model_validate(
        {**values, "terminal_id": make_id("lf022_sol_terminal", values)}
    )


def _load_terminal(path: Path) -> CodexSolAttemptTerminal:
    if path.is_symlink() or not path.is_file():
        raise CodexSolJudgeError(f"terminal is missing or unsafe: {path}")
    raw = path.read_bytes()
    try:
        terminal = CodexSolAttemptTerminal.model_validate_json(raw)
    except ValueError as exc:
        raise CodexSolJudgeError(f"invalid terminal {path}: {exc}") from exc
    if raw != _canonical_line(terminal):
        raise CodexSolJudgeError(f"terminal is not canonical JSON: {path}")
    return terminal


def _load_receipt(path: Path) -> CodexSolProcessReceipt:
    if path.is_symlink() or not path.is_file():
        raise CodexSolJudgeError(f"process receipt is missing or unsafe: {path}")
    raw = path.read_bytes()
    try:
        receipt = CodexSolProcessReceipt.model_validate_json(raw)
    except ValueError as exc:
        raise CodexSolJudgeError(f"invalid process receipt {path}: {exc}") from exc
    if raw != _canonical_line(receipt):
        raise CodexSolJudgeError(f"process receipt is not canonical JSON: {path}")
    return receipt


def _artifact_hash(path: Path, expected: str | None, *, label: str) -> None:
    if expected is None:
        if path.exists():
            raise CodexSolJudgeError(f"unexpected {label} exists: {path}")
        return
    if path.is_symlink() or not path.is_file() or hash_file(path) != expected:
        raise CodexSolJudgeError(f"{label} differs from terminal: {path}")


def _publish_provider_responses(
    *,
    output_root: Path,
    raw_response_root: Path,
    attempt_request: ProviderRequest,
    base_request: ProviderRequest,
    output_text: str,
) -> tuple[str, str]:
    # Preserve the exact provider final message in the generic raw-response
    # envelope.  The separately persisted ``parsed_response.json`` is the
    # canonicalized semantic view; conflating the two would weaken provenance.
    attempt_result = persist_provider_raw_response(
        output_root / "provider_raw_attempts",
        ProviderRawResponse.success(attempt_request, output_text),
    )
    bridge_result = persist_provider_raw_response(
        raw_response_root,
        ProviderRawResponse.success(base_request, output_text),
    )
    load_provider_raw_response(attempt_result.raw_response_path, request=attempt_request)
    load_provider_raw_response(bridge_result.raw_response_path, request=base_request)
    return attempt_result.raw_response_sha256, bridge_result.raw_response_sha256


def _materialize_terminal_from_receipt(
    *,
    config: CodexSolJudgeConfig,
    dispatch: LF022WeakDispatchRecord,
    base_request: ProviderRequest,
    attempt_request: ProviderRequest,
    directory: Path,
    output_root: Path,
    raw_response_root: Path,
    receipt: CodexSolProcessReceipt,
    propagate_interrupt: bool,
) -> CodexSolAttemptTerminal:
    stdout_path = directory / "stdout.jsonl"
    stderr_path = directory / "stderr.txt"
    final_path = directory / "final_message.json"
    if (
        hash_file(stdout_path) != receipt.stdout_sha256
        or hash_file(stderr_path) != receipt.stderr_sha256
    ):
        raise CodexSolJudgeError("process receipt differs from raw stdout/stderr")
    _artifact_hash(final_path, receipt.final_message_sha256, label="final message")
    _artifact_hash(
        directory / "redaction_report.json",
        receipt.redaction_report_sha256,
        label="redaction report",
    )
    stdout = stdout_path.read_bytes()
    stderr = stderr_path.read_bytes()
    final = final_path.read_bytes() if receipt.final_message_sha256 is not None else None

    status: CodexSolAttemptStatus
    error: str | None = None
    parsed_hash: str | None = None
    attempt_raw_hash: str | None = None
    bridge_raw_hash: str | None = None
    if receipt.redaction_count:
        status = "secret_redacted"
        error = "provider process output contained secret-like material and was redacted"
    elif (
        len(stdout) > config.max_stdout_bytes
        or len(stderr) > config.max_stderr_bytes
        or (final is not None and len(final) > config.max_final_message_bytes)
    ):
        status = "capture_too_large"
        error = "Codex stdout, stderr, or final message exceeded its frozen bound"
    elif receipt.status == "timeout":
        status = "timeout"
        error = "codex exec timed out"
    elif receipt.status == "interrupted":
        status = "interrupted"
        error = "codex exec was interrupted"
    elif receipt.exit_code != 0:
        status = "process_failed"
        error = f"codex exec exited with code {receipt.exit_code}"
    elif final is None:
        status = "final_output_missing"
        error = "codex exec did not create the final message"
    else:
        protocol_error = _validate_codex_stdout(stdout, final)
        if protocol_error is not None:
            status = "protocol_failed"
            error = protocol_error
        else:
            try:
                response = _parse_final(final)
            except CodexSolJudgeError as exc:
                status = "judge_parse_failed"
                error = str(exc)
            else:
                parsed = (
                    canonical_json_bytes(response.model_dump(mode="json", by_alias=True)) + b"\n"
                )
                parsed_hash = _write_immutable(
                    directory / "parsed_response.json",
                    parsed,
                    label="parsed Sol judgment",
                )
                attempt_raw_hash, bridge_raw_hash = _publish_provider_responses(
                    output_root=output_root,
                    raw_response_root=raw_response_root,
                    attempt_request=attempt_request,
                    base_request=base_request,
                    output_text=final.decode("utf-8"),
                )
                status = "completed"
    terminal = _terminal_values(
        dispatch=dispatch,
        request=attempt_request,
        status=status,
        exit_code=receipt.exit_code,
        argv_sha256=hash_file(directory / "argv.json"),
        provider_request_sha256=hash_file(directory / "provider_request.json"),
        prompt_sha256=hash_file(directory / "prompt.txt"),
        output_schema_sha256=hash_file(directory / "output_schema.json"),
        stdout_sha256=receipt.stdout_sha256,
        stderr_sha256=receipt.stderr_sha256,
        final_message_sha256=receipt.final_message_sha256,
        redaction_report_sha256=receipt.redaction_report_sha256,
        redaction_count=receipt.redaction_count,
        parsed_response_sha256=parsed_hash,
        attempt_raw_response_sha256=attempt_raw_hash,
        replay_bridge_raw_response_sha256=bridge_raw_hash,
        error=error,
    )
    _write_immutable(directory / "terminal.json", _canonical_line(terminal), label="Sol terminal")
    if status == "completed":
        _write_immutable(
            directory.parent / "completed.json",
            _canonical_line(terminal),
            label="Sol completion",
        )
    if status == "interrupted" and propagate_interrupt:
        raise KeyboardInterrupt
    return terminal


def _verify_attempt(
    *,
    config: CodexSolJudgeConfig,
    dispatch: LF022WeakDispatchRecord,
    base_request: ProviderRequest,
    terminal: CodexSolAttemptTerminal,
    directory: Path,
    output_root: Path,
    raw_response_root: Path,
    binary: Path | None,
) -> None:
    request = _attempt_request(base_request, terminal.attempt_index)
    if (
        terminal.dispatch_cell_id != dispatch.dispatch_cell_id
        or terminal.provider_request_hash != request.request_hash
        or terminal.provider_attempt_id != request.attempt_id
    ):
        raise CodexSolJudgeError("terminal differs from dispatch or attempt request")
    request_path = directory / "provider_request.json"
    if load_provider_request(request_path) != request:
        raise CodexSolJudgeError("attempt provider request does not replay")
    _artifact_hash(request_path, terminal.provider_request_sha256, label="provider request")
    _artifact_hash(directory / "prompt.txt", terminal.prompt_sha256, label="wire prompt")
    if (directory / "prompt.txt").read_bytes() != _wire_prompt(request):
        raise CodexSolJudgeError("attempt wire prompt differs from request and system pin")
    _artifact_hash(
        directory / "output_schema.json",
        terminal.output_schema_sha256,
        label="output schema",
    )
    if (directory / "output_schema.json").read_bytes() != _output_schema_bytes():
        raise CodexSolJudgeError("attempt output schema differs from frozen schema")
    argv_path = directory / "argv.json"
    _artifact_hash(argv_path, terminal.argv_sha256, label="argv")
    argv_payload = json.loads(argv_path.read_text(encoding="utf-8"))
    if not isinstance(argv_payload, list) or not all(
        isinstance(item, str) for item in argv_payload
    ):
        raise CodexSolJudgeError("attempt argv must be a string list")
    if binary is not None:
        argv_binary = binary
    else:
        argv_binary = Path(cast(str, argv_payload[0]))
        if not argv_binary.is_absolute():
            raise CodexSolJudgeError("recorded Codex executable path is not absolute")
    try:
        recorded_final_path = Path(cast(str, argv_payload[argv_payload.index("-o") + 1]))
    except (ValueError, IndexError) as exc:
        raise CodexSolJudgeError("attempt argv lacks a unique final-output sink") from exc
    if (
        not recorded_final_path.is_absolute()
        or recorded_final_path.name != "final_message.json"
        or not recorded_final_path.parent.name.startswith("leanfaith-sol-empty-")
    ):
        raise CodexSolJudgeError("attempt argv final-output sink is not an ephemeral capture path")
    expected_argv = _argv(
        binary=argv_binary,
        config=config,
        output_schema_path=directory / "output_schema.json",
        final_message_path=recorded_final_path,
    )
    if tuple(argv_payload) != expected_argv:
        raise CodexSolJudgeError("attempt argv differs from frozen CLI contract")
    for name, expected in (
        ("stdout.jsonl", terminal.stdout_sha256),
        ("stderr.txt", terminal.stderr_sha256),
        ("final_message.json", terminal.final_message_sha256),
        ("redaction_report.json", terminal.redaction_report_sha256),
        ("parsed_response.json", terminal.parsed_response_sha256),
    ):
        _artifact_hash(directory / name, expected, label=name)
    receipt = _load_receipt(directory / "process_receipt.json")
    if (
        receipt.redaction_report_sha256 != terminal.redaction_report_sha256
        or receipt.redaction_count != terminal.redaction_count
    ):
        raise CodexSolJudgeError("redaction receipt differs from terminal")
    if terminal.status != "completed":
        return
    assert terminal.final_message_sha256 is not None
    final = (directory / "final_message.json").read_bytes()
    stdout = (directory / "stdout.jsonl").read_bytes()
    if _validate_codex_stdout(stdout, final) is not None:
        raise CodexSolJudgeError("completed stdout/final protocol no longer replays")
    response = _parse_final(final)
    parsed = canonical_json_bytes(response.model_dump(mode="json", by_alias=True)) + b"\n"
    if (directory / "parsed_response.json").read_bytes() != parsed:
        raise CodexSolJudgeError("completed parsed response does not replay")
    attempt_path = provider_raw_response_path(
        output_root / "provider_raw_attempts",
        request,
    )
    bridge_path = provider_raw_response_path(raw_response_root, base_request)
    if (
        hash_file(attempt_path) != terminal.attempt_raw_response_sha256
        or hash_file(bridge_path) != terminal.replay_bridge_raw_response_sha256
    ):
        raise CodexSolJudgeError("completed provider response hash differs from terminal")
    load_provider_raw_response(attempt_path, request=request)
    load_provider_raw_response(bridge_path, request=base_request)


def _prepare_attempt_artifacts(
    *,
    config: CodexSolJudgeConfig,
    request: ProviderRequest,
    directory: Path,
    binary: Path,
    final_message_path: Path | None = None,
) -> tuple[str, str, str, str, tuple[str, ...]]:
    schema_path = directory / "output_schema.json"
    final_path = final_message_path or Path("/tmp/leanfaith-sol-empty-recovery/final_message.json")
    argv = _argv(
        binary=binary,
        config=config,
        output_schema_path=schema_path,
        final_message_path=final_path,
    )
    argv_hash = _write_immutable(
        directory / "argv.json",
        canonical_json_bytes(list(argv)) + b"\n",
        label="Sol argv",
    )
    request_hash = _write_immutable(
        directory / "provider_request.json",
        canonical_json_bytes(request.model_dump(mode="json")) + b"\n",
        label="attempt-specific provider request",
    )
    prompt_hash = _write_immutable(
        directory / "prompt.txt",
        _wire_prompt(request),
        label="Sol wire prompt",
    )
    schema_hash = _write_immutable(
        schema_path,
        _output_schema_bytes(),
        label="Sol output schema",
    )
    return argv_hash, request_hash, prompt_hash, schema_hash, argv


def _process_receipt(
    *,
    request: ProviderRequest,
    capture: CodexSolProcessCapture,
    stdout_sha256: str,
    stderr_sha256: str,
    final_message_sha256: str | None,
    redaction_report_sha256: str,
    redaction_count: int,
) -> CodexSolProcessReceipt:
    values: dict[str, object] = {
        "schema_version": 1,
        "provider_attempt_id": request.attempt_id,
        "status": capture.status,
        "exit_code": capture.exit_code,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "final_message_sha256": final_message_sha256,
        "redaction_report_sha256": redaction_report_sha256,
        "redaction_count": redaction_count,
    }
    return CodexSolProcessReceipt.model_validate(
        {
            **values,
            "receipt_id": make_id("lf022_sol_process_receipt", values),
        }
    )


def _run_external_attempt(
    *,
    config: CodexSolJudgeConfig,
    dispatch: LF022WeakDispatchRecord,
    base_request: ProviderRequest,
    request: ProviderRequest,
    directory: Path,
    output_root: Path,
    raw_response_root: Path,
    executor: CodexSolCliExecutor,
    child_env: Mapping[str, str],
) -> CodexSolAttemptTerminal:
    # Keep the non-provider version check outside the temporary CODEX_HOME.
    # The pinned standalone emits a benign PATH-alias warning for homes under
    # the system temporary directory, which would otherwise defeat the exact
    # version-output check.  The provider-bearing `codex exec` below is still
    # fully isolated.
    binary = _check_binary(config)
    auth_before = _read_codex_auth(child_env)
    with tempfile.TemporaryDirectory(prefix="leanfaith-sol-empty-") as temporary:
        transient_final_path = Path(temporary) / "final_message.json"
        _, _, _, _, argv = _prepare_attempt_artifacts(
            config=config,
            request=request,
            directory=directory,
            binary=binary,
            final_message_path=transient_final_path,
        )
        capture = executor.execute(
            argv=argv,
            prompt=_wire_prompt(request),
            cwd=Path(temporary),
            final_message_path=transient_final_path,
            timeout_seconds=config.timeout_seconds,
            termination_grace_seconds=config.termination_grace_seconds,
            max_stdout_bytes=config.max_stdout_bytes,
            max_stderr_bytes=config.max_stderr_bytes,
            max_final_message_bytes=config.max_final_message_bytes,
            child_env=child_env,
        )
    auth_after = _read_codex_auth(child_env)
    redacted = redact_captured_streams(
        {
            "stdout.jsonl": capture.stdout,
            "stderr.txt": capture.stderr,
            "final_message.json": capture.final_message or b"",
        },
        environment=_codex_redaction_environment(auth_before, auth_after),
    )
    # Only redacted process output lands durably before the receipt or parser.
    stdout_hash = _write_immutable(
        directory / "stdout.jsonl", redacted.streams["stdout.jsonl"], label="redacted Sol stdout"
    )
    stderr_hash = _write_immutable(
        directory / "stderr.txt", redacted.streams["stderr.txt"], label="redacted Sol stderr"
    )
    redaction_hash = _write_immutable(
        directory / "redaction_report.json",
        redacted.report_bytes,
        label="Sol capture redaction report",
    )
    final_hash: str | None = None
    if capture.final_message is not None:
        final_hash = _write_immutable(
            directory / "final_message.json",
            redacted.streams["final_message.json"],
            label="redacted Sol final message",
        )
    receipt = _process_receipt(
        request=request,
        capture=capture,
        stdout_sha256=stdout_hash,
        stderr_sha256=stderr_hash,
        final_message_sha256=final_hash,
        redaction_report_sha256=redaction_hash,
        redaction_count=redacted.replacement_count,
    )
    _write_immutable(
        directory / "process_receipt.json",
        _canonical_line(receipt),
        label="Sol process receipt",
    )
    return _materialize_terminal_from_receipt(
        config=config,
        dispatch=dispatch,
        base_request=base_request,
        attempt_request=request,
        directory=directory,
        output_root=output_root,
        raw_response_root=raw_response_root,
        receipt=receipt,
        propagate_interrupt=True,
    )


def _recover_or_verify_attempt(
    *,
    config: CodexSolJudgeConfig,
    dispatch: LF022WeakDispatchRecord,
    base_request: ProviderRequest,
    request: ProviderRequest,
    directory: Path,
    output_root: Path,
    raw_response_root: Path,
) -> CodexSolAttemptTerminal:
    terminal_path = directory / "terminal.json"
    if terminal_path.exists():
        terminal = _load_terminal(terminal_path)
        _verify_attempt(
            config=config,
            dispatch=dispatch,
            base_request=base_request,
            terminal=terminal,
            directory=directory,
            output_root=output_root,
            raw_response_root=raw_response_root,
            binary=None,
        )
        return terminal

    # The pre-call artifacts must all exist before an attempted process can be
    # recovered.  Their exact content is verified through a provisional
    # terminal below or through receipt materialization.
    pre_call = (
        directory / "argv.json",
        directory / "provider_request.json",
        directory / "prompt.txt",
        directory / "output_schema.json",
    )
    if any(path.is_symlink() or not path.is_file() for path in pre_call):
        raise CodexSolPartialAttemptError(
            f"partial attempt lacks complete pre-call artifacts; refusing recall: {directory}"
        )
    if load_provider_request(directory / "provider_request.json") != request:
        raise CodexSolPartialAttemptError("partial attempt request differs from expected retry")
    if (directory / "prompt.txt").read_bytes() != _wire_prompt(request):
        raise CodexSolPartialAttemptError("partial attempt prompt differs")
    if (directory / "output_schema.json").read_bytes() != _output_schema_bytes():
        raise CodexSolPartialAttemptError("partial attempt schema differs")

    receipt_path = directory / "process_receipt.json"
    if receipt_path.exists():
        receipt = _load_receipt(receipt_path)
        if receipt.provider_attempt_id != request.attempt_id:
            raise CodexSolPartialAttemptError("partial receipt differs from attempt request")
        terminal = _materialize_terminal_from_receipt(
            config=config,
            dispatch=dispatch,
            base_request=base_request,
            attempt_request=request,
            directory=directory,
            output_root=output_root,
            raw_response_root=raw_response_root,
            receipt=receipt,
            propagate_interrupt=False,
        )
        _verify_attempt(
            config=config,
            dispatch=dispatch,
            base_request=base_request,
            terminal=terminal,
            directory=directory,
            output_root=output_root,
            raw_response_root=raw_response_root,
            binary=None,
        )
        return terminal

    raise CodexSolPartialAttemptError(
        "non-terminal attempt lacks a complete process receipt; refusing recall or "
        "automatic advance pending explicit reviewed recovery"
    )


def _verify_authorization(
    *,
    authorization_path: Path,
    authorization_nonce: bytes,
    batch_id: str,
    config_sha256: str,
    shard_id: str,
    offset_pairs: int,
    limit_pairs: int,
    now: datetime.datetime,
) -> str:
    if len(authorization_nonce) < 32:
        raise CodexSolAuthorizationError("authorization nonce must contain at least 32 bytes")
    authorization, digest = _load_canonical_authorization(authorization_path)
    expected = (
        batch_id,
        config_sha256,
        "judge_A",
        shard_id,
        offset_pairs,
        limit_pairs,
        sha256_hex(authorization_nonce),
    )
    observed = (
        authorization.batch_id,
        authorization.config_sha256,
        authorization.judge_slot,
        authorization.shard_id,
        authorization.offset_pairs,
        authorization.limit_pairs,
        authorization.authorization_nonce_sha256,
    )
    if observed != expected:
        raise CodexSolAuthorizationError("live authorization differs from exact batch shard")
    require_utc(now)
    if now < authorization.approved_at or now > authorization.expires_at:
        raise CodexSolAuthorizationError("live authorization is not currently valid")
    return digest


def _utc_json(value: datetime.datetime) -> str:
    require_utc(value)
    return value.isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime.datetime:
    """Return the live wall clock used for authorization and run identity.

    This private seam is monkeypatched by offline tests.  Public execution APIs
    intentionally do not accept a caller-supplied time, because that would let
    a stale authorization be replayed by choosing an earlier timestamp.
    """

    return datetime.datetime.now(tz=datetime.UTC)


def _run_codex_sol_cells(
    *,
    execution_mode: Literal["external", "offline_replay"],
    batch_root: Path,
    output_root: Path,
    config_path: Path,
    judge_slot: JudgeSlot,
    shard_id: str,
    offset_pairs: int,
    limit_pairs: int,
    run_nonce: bytes,
    executor: CodexSolCliExecutor | None,
    authorization_path: Path | None,
    authorization_nonce: bytes | None,
    execute_external: bool,
) -> CodexSolRunResult:
    if len(run_nonce) < 32:
        raise CodexSolJudgeError("run nonce must contain at least 32 bytes")
    if not shard_id or len(shard_id) > 100:
        raise CodexSolJudgeError("shard_id must be a short nonempty identifier")
    if any(not (character.isalnum() or character in "_.-") for character in shard_id):
        raise CodexSolJudgeError("shard_id contains unsupported characters")
    loaded = load_codex_sol_judge_config(config_path)
    config = loaded.config
    if limit_pairs < 1 or limit_pairs > config.max_pairs_per_invocation:
        raise CodexSolJudgeError(f"limit_pairs must be within 1..{config.max_pairs_per_invocation}")
    if judge_slot != "judge_A":
        raise CodexSolJudgeError("GPT-5.6 Sol is frozen exclusively in judge_A")
    batch_root = _safe_absolute(batch_root, label="weak batch root", allow_missing=False)
    output_root = _safe_absolute(output_root, label="Sol output root", allow_missing=True)
    raw_response_root = _safe_absolute(
        batch_root / "raw" / judge_slot,
        label="canonical weak-batch raw root",
        allow_missing=True,
    )
    spec, batch_manifest, dispatches, _ = _load_prepared_batch(batch_root)
    endpoint = _endpoint(spec, judge_slot)
    if (
        endpoint.provider != config.provider
        or endpoint.model != config.registry_model_id
        or endpoint.family_id != config.model_family
        or endpoint.revision != config.endpoint_revision
        or endpoint.decoding != config.endpoint_decoding
    ):
        raise CodexSolJudgeError("prepared Sol endpoint differs from the frozen CLI contract")
    cells = _selected_cells(
        dispatches,
        slot=judge_slot,
        offset_pairs=offset_pairs,
        limit_pairs=limit_pairs,
    )
    if not cells:
        raise CodexSolJudgeError("prepared batch contains no selected Sol cells")
    created_at = _utcnow()
    require_utc(created_at)

    authorization_sha256: str | None = None
    if execution_mode == "external":
        if (
            not spec.live_provider_calls_authorized
            or spec.execution_authorization != "live_provider_calls_explicitly_authorized"
        ):
            raise CodexSolAuthorizationError(
                "prepared batch does not explicitly authorize live provider calls"
            )
        if not execute_external:
            raise CodexSolAuthorizationError(
                "external execution requires execute_external=True in addition to authorization"
            )
        if authorization_path is None or authorization_nonce is None:
            raise CodexSolAuthorizationError(
                "external execution requires an authorization artifact and nonce"
            )
        authorization_sha256 = _verify_authorization(
            authorization_path=authorization_path,
            authorization_nonce=authorization_nonce,
            batch_id=batch_manifest.batch_id,
            config_sha256=loaded.sha256,
            shard_id=shard_id,
            offset_pairs=offset_pairs,
            limit_pairs=limit_pairs,
            now=created_at,
        )
    elif execute_external or authorization_path is not None or authorization_nonce is not None:
        raise CodexSolAuthorizationError("offline replay rejects all live-execution arguments")

    if execution_mode == "external":
        persist_lf022_weak_execution_started_marker(
            batch_root=batch_root,
            dispatch_manifest=batch_manifest,
        )
    runner = executor or SubprocessCodexSolCliExecutor()
    final_terminals: list[CodexSolAttemptTerminal] = []
    invoked_cells: set[str] = set()
    process_attempt_count = 0
    with _operation_lock(output_root):
        for dispatch in cells:
            base_request = _verify_dispatch_request(batch_root, dispatch, spec)
            if (
                base_request.private_source_content
                or not dispatch.task.external_transmission_allowed
            ):
                raise CodexSolJudgeError("private or transmission-forbidden task rejected")
            if base_request.prompt_template_hash != config.judge_template_sha256:
                raise CodexSolJudgeError("judge prompt template differs from Sol config")
            terminal: CodexSolAttemptTerminal | None = None
            for attempt_index in range(config.max_attempts_per_cell):
                request = _attempt_request(base_request, attempt_index)
                directory = _attempt_dir(output_root, base_request.request_hash, attempt_index)
                if directory.exists():
                    if directory.is_symlink() or not directory.is_dir():
                        raise CodexSolJudgeError(f"unsafe Sol attempt directory: {directory}")
                    terminal = _recover_or_verify_attempt(
                        config=config,
                        dispatch=dispatch,
                        base_request=base_request,
                        request=request,
                        directory=directory,
                        output_root=output_root,
                        raw_response_root=raw_response_root,
                    )
                elif execution_mode == "external":
                    # Codex creates mutable logs under CODEX_HOME.  Give every
                    # process attempt a fresh private home so those logs cannot
                    # accumulate across a shard and hit the bounded file-size
                    # limit that protects durable captures.
                    with _isolated_codex_environment(config) as isolated_child_env:
                        terminal = _run_external_attempt(
                            config=config,
                            dispatch=dispatch,
                            base_request=base_request,
                            request=request,
                            directory=directory,
                            output_root=output_root,
                            raw_response_root=raw_response_root,
                            executor=runner,
                            child_env=isolated_child_env,
                        )
                    invoked_cells.add(dispatch.dispatch_cell_id)
                    process_attempt_count += 1
                else:
                    raise CodexSolJudgeError(
                        "offline replay lacks a terminal attempt for "
                        f"{dispatch.dispatch_cell_id} at index {attempt_index}"
                    )
                if terminal.status == "completed":
                    break
            if terminal is None:
                raise CodexSolJudgeError("attempt loop produced no terminal")
            final_terminals.append(terminal)

        status_counts = dict(sorted(Counter(item.status for item in final_terminals).items()))
        completed_count = sum(item.status == "completed" for item in final_terminals)
        values: dict[str, object] = {
            "schema_version": 1,
            "method_version": SOL_JUDGE_METHOD_VERSION,
            "created_at": _utc_json(created_at),
            "run_nonce_sha256": sha256_hex(run_nonce),
            "execution_mode": execution_mode,
            "authorization_sha256": authorization_sha256,
            "batch_id": batch_manifest.batch_id,
            "batch_manifest_sha256": hash_file(batch_root / "dispatch_manifest.json"),
            "config_sha256": loaded.sha256,
            "provider": config.provider,
            "model_family": config.model_family,
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
            "judge_slot": judge_slot,
            "shard_id": shard_id,
            "offset_pairs": offset_pairs,
            "selected_pair_count": len(cells) // 2,
            "selected_cell_count": len(cells),
            "invoked_cell_count": len(invoked_cells),
            "reused_cell_count": len(cells) - len(invoked_cells),
            "process_attempt_count": process_attempt_count,
            "completed_cell_count": completed_count,
            "exhausted_cell_count": len(cells) - completed_count,
            "terminal_status_counts": status_counts,
            "ordered_dispatch_ids_sha256": hash_canonical(
                [item.dispatch_cell_id for item in cells]
            ),
            "all_selected_cells_terminal": True,
            "both_orientations_selected": True,
            "private_source_content_transmitted": False,
            "semantic_labels_created": False,
            "human_labels_created": False,
            "silver_records_created": False,
            "training_eligible": False,
            "evaluation_eligible": False,
            "gate_credit_claimed": False,
        }
        manifest = CodexSolRunManifest.model_validate(
            {**values, "run_id": make_id("lf022_sol_run", values)}
        )
        manifest_bytes = _canonical_line(manifest)
        manifest_name = f"{manifest.run_id.removeprefix('lf022_sol_run:')}.json"
        manifest_path = output_root / "runs" / manifest_name
        _write_immutable(manifest_path, manifest_bytes, label="Sol run manifest")
        _write_atomic(output_root / "run_manifest.json", manifest_bytes)
    return CodexSolRunResult(
        manifest=manifest,
        manifest_path=manifest_path,
        terminals=tuple(final_terminals),
    )


def execute_codex_sol_weak_cells(
    *,
    batch_root: Path,
    output_root: Path,
    config_path: Path,
    judge_slot: JudgeSlot,
    shard_id: str,
    offset_pairs: int,
    limit_pairs: int,
    run_nonce: bytes,
    authorization_path: Path,
    authorization_nonce: bytes,
    execute_external: bool,
    executor: CodexSolCliExecutor | None = None,
) -> CodexSolRunResult:
    """Execute/resume one explicitly authorized public Sol shard."""

    return _run_codex_sol_cells(
        execution_mode="external",
        batch_root=batch_root,
        output_root=output_root,
        config_path=config_path,
        judge_slot=judge_slot,
        shard_id=shard_id,
        offset_pairs=offset_pairs,
        limit_pairs=limit_pairs,
        run_nonce=run_nonce,
        executor=executor,
        authorization_path=authorization_path,
        authorization_nonce=authorization_nonce,
        execute_external=execute_external,
    )


def replay_codex_sol_weak_cells(
    *,
    batch_root: Path,
    output_root: Path,
    config_path: Path,
    judge_slot: JudgeSlot,
    shard_id: str,
    offset_pairs: int,
    limit_pairs: int,
    run_nonce: bytes,
) -> CodexSolRunResult:
    """Verify an already terminal Sol shard without binary checks or provider calls."""

    return _run_codex_sol_cells(
        execution_mode="offline_replay",
        batch_root=batch_root,
        output_root=output_root,
        config_path=config_path,
        judge_slot=judge_slot,
        shard_id=shard_id,
        offset_pairs=offset_pairs,
        limit_pairs=limit_pairs,
        run_nonce=run_nonce,
        executor=None,
        authorization_path=None,
        authorization_nonce=None,
        execute_external=False,
    )


def _read_nonce(path: Path, *, label: str) -> bytes:
    safe = _safe_absolute(path, label=label, allow_missing=False)
    if safe.is_symlink() or not safe.is_file():
        raise CodexSolJudgeError(f"{label} is not a regular file")
    value = safe.read_bytes()
    if len(value) < 32:
        raise CodexSolJudgeError(f"{label} must contain at least 32 bytes")
    return value


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/generation/lf022_codex_sol_judge_v1.yaml"),
    )
    parser.add_argument("--judge-slot", choices=("judge_A",), default="judge_A")
    parser.add_argument("--shard-id", required=True)
    parser.add_argument("--offset-pairs", type=int, required=True)
    parser.add_argument("--limit-pairs", type=int, required=True)
    parser.add_argument("--run-nonce-file", type=Path, required=True)


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    replay_parser = commands.add_parser("replay", help="offline-only artifact replay")
    _common_arguments(replay_parser)
    live_parser = commands.add_parser("live", help="explicitly authorized public execution")
    _common_arguments(live_parser)
    live_parser.add_argument("--authorization", type=Path, required=True)
    live_parser.add_argument("--authorization-nonce-file", type=Path, required=True)
    live_parser.add_argument(
        "--execute-public-external",
        action="store_true",
        help="second explicit consent required in addition to authorization artifact",
    )
    arguments = parser.parse_args()
    common: dict[str, object] = {
        "batch_root": arguments.batch_root,
        "output_root": arguments.output_root,
        "config_path": arguments.config,
        "judge_slot": arguments.judge_slot,
        "shard_id": arguments.shard_id,
        "offset_pairs": arguments.offset_pairs,
        "limit_pairs": arguments.limit_pairs,
        "run_nonce": _read_nonce(arguments.run_nonce_file, label="run nonce"),
    }
    if arguments.mode == "replay":
        result = replay_codex_sol_weak_cells(**common)  # type: ignore[arg-type]
    else:
        result = execute_codex_sol_weak_cells(
            **common,  # type: ignore[arg-type]
            authorization_path=arguments.authorization,
            authorization_nonce=_read_nonce(
                arguments.authorization_nonce_file,
                label="authorization nonce",
            ),
            execute_external=arguments.execute_public_external,
        )
    print(result.manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "SOL_FAMILY",
    "SOL_JUDGE_METHOD_VERSION",
    "SOL_MODEL",
    "SOL_PROVIDER",
    "SOL_REASONING_EFFORT",
    "SOL_REGISTRY_MODEL",
    "CodexSolAttemptTerminal",
    "CodexSolAuthorizationError",
    "CodexSolCliExecutor",
    "CodexSolJudgeConfig",
    "CodexSolJudgeError",
    "CodexSolLiveAuthorization",
    "CodexSolPartialAttemptError",
    "CodexSolProcessCapture",
    "CodexSolRunManifest",
    "CodexSolRunResult",
    "LoadedCodexSolJudgeConfig",
    "SubprocessCodexSolCliExecutor",
    "execute_codex_sol_weak_cells",
    "load_codex_sol_judge_config",
    "replay_codex_sol_weak_cells",
]
