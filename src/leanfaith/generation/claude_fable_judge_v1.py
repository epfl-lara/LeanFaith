"""Fail-closed Claude Fable 5 bridge for LF-022 blinded judge cells.

This module is deliberately narrower than the generic weak-batch machinery:

* it executes only the Fable endpoint of an already prepared public-source
  weak batch;
* it preserves deterministic secret-redacted Claude CLI stdout/stderr before parsing;
* it writes the canonical :class:`ProviderRawResponse` consumed later by the
  offline-only weak-batch replay path; and
* it never creates a semantic, silver, human, training, or evaluation label.

One invocation is bounded to at most 64 pairs and executes both AB and BA
presentations for the selected Fable slot.  A one-pair invocation is therefore
the required two-call smoke, not a single orientation masquerading as a swap
audit.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
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

FABLE_JUDGE_METHOD_VERSION: Literal["lf022_claude_fable_judge_v1"] = "lf022_claude_fable_judge_v1"
FABLE_PROVIDER: Literal["anthropic_claude_code"] = "anthropic_claude_code"
FABLE_FAMILY: Literal["anthropic_fable"] = "anthropic_fable"
FABLE_MODEL: Literal["claude-fable-5"] = "claude-fable-5"
_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
_SYSTEM_PROMPT = (
    "You are a mathematical-semantics judge. Follow the supplied LeanFaith "
    "instructions exactly and return only the JSON object required by the "
    "supplied schema. Do not use tools."
)


class ClaudeFableJudgeError(RuntimeError):
    """A configuration, privacy, process, artifact, or replay invariant failed."""


class ClaudeFableAuthorizationError(ClaudeFableJudgeError):
    """The exact, time-bounded public live authorization is absent or invalid."""


class ClaudeFablePartialAttemptError(ClaudeFableJudgeError):
    """A possibly invoked attempt lacks enough durable evidence for safe recovery."""


class ClaudeFableJudgeConfig(StrictModel):
    """Exact local CLI and bounded execution contract."""

    schema_version: Literal[1] = 1
    config_id: Literal["lf022_claude_fable_judge_v1"]
    status: Literal["offline_implemented_live_smoke_not_yet_qualified"]
    provider: Literal["anthropic_claude_code"]
    model_family: Literal["anthropic_fable"]
    model: Literal["claude-fable-5"]
    registry_model_id: Literal["anthropic/claude-fable-5"]
    effort: Literal["max"]
    provider_catalog_sha256: str = Field(pattern=HEX64_PATTERN)
    claude_binary_path: str = Field(min_length=1)
    claude_cli_version: str = Field(min_length=1)
    claude_binary_sha256: str = Field(pattern=HEX64_PATTERN)
    server_model_revision_status: Literal["unavailable_floating_provider_alias"]
    system_prompt_sha256: str = Field(pattern=HEX64_PATTERN)
    output_schema_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_template_sha256: str = Field(pattern=HEX64_PATTERN)
    max_pairs_per_invocation: int = Field(ge=1, le=64, strict=True)
    maximum_concurrency: Literal[1] = 1
    max_attempts_per_cell: int = Field(ge=1, le=3, strict=True)
    timeout_seconds: int = Field(ge=1, le=7200, strict=True)
    termination_grace_seconds: int = Field(ge=1, le=60, strict=True)
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
    def _bound_prompt(self) -> Self:
        if self.system_prompt_sha256 != hash_canonical({"system_prompt": _SYSTEM_PROMPT}):
            raise ValueError("system_prompt_sha256 differs from the adapter system prompt")
        if self.output_schema_sha256 != hash_canonical(_judge_schema()):
            raise ValueError("output_schema_sha256 differs from the adapter schema")
        return self

    @property
    def endpoint_revision(self) -> str:
        return f"provider-deployment-snapshot:{self.provider_catalog_sha256}"


@dataclass(frozen=True, slots=True)
class LoadedClaudeFableJudgeConfig:
    config: ClaudeFableJudgeConfig
    path: Path
    sha256: str


class ClaudeFableLiveAuthorization(StrictModel):
    """Reviewed authorization bound to one exact Fable batch shard."""

    schema_version: Literal[1] = 1
    authorization_id: str = Field(pattern=id_pattern("lf022_fable_live_authorization"))
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    config_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_slot: Literal["judge_B"] = "judge_B"
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
            "lf022_fable_live_authorization",
            self.model_dump(mode="json", exclude={"authorization_id"}),
        )
        if self.authorization_id != expected:
            raise ValueError("authorization_id differs from authorization content")
        return self


class ClaudeCliCapture(StrictModel):
    """Exact result of one shell-free Claude CLI process."""

    status: Literal["completed", "timeout", "interrupted"]
    exit_code: int | None
    stdout: bytes
    stderr: bytes


class ClaudeFableProcessReceipt(StrictModel):
    """Durable proof that redacted process captures are complete."""

    schema_version: Literal[1] = 1
    receipt_id: str = Field(pattern=id_pattern("lf022_fable_process_receipt"))
    provider_attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    status: Literal["completed", "timeout", "interrupted"]
    exit_code: int | None
    stdout_sha256: str = Field(pattern=HEX64_PATTERN)
    stderr_sha256: str = Field(pattern=HEX64_PATTERN)
    redaction_report_sha256: str = Field(pattern=HEX64_PATTERN)
    redaction_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _identity(self) -> Self:
        expected = make_id(
            "lf022_fable_process_receipt",
            self.model_dump(mode="json", exclude={"receipt_id"}),
        )
        if self.receipt_id != expected:
            raise ValueError("receipt_id differs from receipt content")
        return self


class ClaudeCliExecutor(Protocol):
    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> ClaudeCliCapture: ...


class SubprocessClaudeCliExecutor:
    """Shell-free Claude execution with process-group timeout cleanup."""

    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> ClaudeCliCapture:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                tuple(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            status: Literal["completed", "timeout", "interrupted"] = "completed"
            try:
                process.communicate(input=prompt, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                status = "timeout"
                _terminate_process_group(process, termination_grace_seconds)
            except KeyboardInterrupt:
                status = "interrupted"
                _terminate_process_group(process, termination_grace_seconds)
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(_MAX_CAPTURE_BYTES + 1)
            stderr = stderr_file.read(_MAX_CAPTURE_BYTES + 1)
        return ClaudeCliCapture(
            status=status,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
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


class ClaudeFableAttemptTerminal(StrictModel):
    """Immutable terminal for one local CLI invocation."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_claude_fable_judge_v1"] = FABLE_JUDGE_METHOD_VERSION
    terminal_id: str = Field(pattern=id_pattern("lf022_fable_terminal"))
    dispatch_cell_id: str = Field(pattern=id_pattern("lf022_weak_cell"))
    provider_request_hash: str = Field(pattern=HEX64_PATTERN)
    provider_attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    attempt_index: int = Field(ge=0, strict=True)
    status: Literal[
        "completed",
        "timeout",
        "interrupted",
        "process_failed",
        "capture_too_large",
        "wrapper_parse_failed",
        "judge_parse_failed",
        "secret_redacted",
    ]
    exit_code: int | None
    argv_sha256: str = Field(pattern=HEX64_PATTERN)
    stdout_sha256: str = Field(pattern=HEX64_PATTERN)
    stderr_sha256: str = Field(pattern=HEX64_PATTERN)
    redaction_report_sha256: str = Field(pattern=HEX64_PATTERN)
    redaction_count: int = Field(ge=0, strict=True)
    parsed_response_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    attempt_raw_response_artifact: str = Field(min_length=1)
    attempt_raw_response_sha256: str = Field(pattern=HEX64_PATTERN)
    replay_bridge_raw_response_artifact: str | None = None
    replay_bridge_raw_response_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    error: str | None = None
    semantic_label_created: Literal[False] = False
    human_label_created: Literal[False] = False
    silver_promoted: Literal[False] = False
    train_eligible: Literal[False] = False
    eval_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _id_and_status(self) -> Self:
        if (self.status == "completed") != (self.parsed_response_sha256 is not None):
            raise ValueError("only completed terminals carry a parsed response")
        if (self.status == "completed") != (
            self.replay_bridge_raw_response_sha256 is not None
            and self.replay_bridge_raw_response_artifact is not None
        ):
            raise ValueError("only completed terminals carry a generic replay bridge")
        if self.status == "secret_redacted" and self.redaction_count < 1:
            raise ValueError("secret_redacted requires at least one replacement")
        if self.status != "secret_redacted" and self.redaction_count != 0:
            raise ValueError("redacted captures must fail as secret_redacted")
        expected = make_id(
            "lf022_fable_terminal",
            self.model_dump(mode="json", exclude={"terminal_id"}),
        )
        if self.terminal_id != expected:
            raise ValueError("terminal_id differs from terminal content")
        return self


class ClaudeFableRunManifest(StrictModel):
    """Atomic summary of one bounded invocation."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_claude_fable_judge_v1"] = FABLE_JUDGE_METHOD_VERSION
    run_id: str = Field(pattern=id_pattern("lf022_fable_run"))
    created_at: datetime.datetime
    run_nonce_sha256: str = Field(pattern=HEX64_PATTERN)
    execution_mode: Literal["external", "offline_replay"]
    authorization_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    batch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    config_sha256: str = Field(pattern=HEX64_PATTERN)
    provider: Literal["anthropic_claude_code"] = FABLE_PROVIDER
    model_family: Literal["anthropic_fable"] = FABLE_FAMILY
    model: Literal["claude-fable-5"] = FABLE_MODEL
    effort: Literal["max"] = "max"
    judge_slot: JudgeSlot
    shard_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    offset_pairs: int = Field(ge=0, strict=True)
    selected_pair_count: int = Field(ge=0, le=64, strict=True)
    selected_cell_count: int = Field(ge=0, le=128, strict=True)
    invoked_cell_count: int = Field(ge=0, le=128, strict=True)
    reused_cell_count: int = Field(ge=0, le=128, strict=True)
    process_attempt_count: int = Field(ge=0, le=384, strict=True)
    completed_cell_count: int = Field(ge=0, le=128, strict=True)
    successful_attempt_ids_by_dispatch: dict[str, str]
    terminal_status_counts: dict[str, int]
    ordered_dispatch_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    all_selected_cells_terminal: bool
    both_orientations_selected: Literal[True] = True
    private_source_content_transmitted: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    human_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _counts_and_id(self) -> Self:
        require_utc(self.created_at)
        if self.selected_cell_count != 2 * self.selected_pair_count:
            raise ValueError("each selected pair must contribute AB and BA cells")
        if self.invoked_cell_count + self.reused_cell_count > self.selected_cell_count:
            raise ValueError("invoked plus reused cells cannot exceed selected cells")
        if sum(self.terminal_status_counts.values()) != self.selected_cell_count:
            raise ValueError("terminal status counts do not reconcile")
        if len(self.successful_attempt_ids_by_dispatch) != self.completed_cell_count:
            raise ValueError("successful attempt mapping must equal completed cell count")
        if any(
            not attempt_id.startswith("provider-attempt:")
            for attempt_id in self.successful_attempt_ids_by_dispatch.values()
        ):
            raise ValueError("successful attempt mapping contains an invalid attempt ID")
        if (self.execution_mode == "external") != (self.authorization_sha256 is not None):
            raise ValueError("only external runs carry a live authorization")
        expected = make_id(
            "lf022_fable_run",
            self.model_dump(mode="json", exclude={"run_id"}),
        )
        if self.run_id != expected:
            raise ValueError("run_id differs from run content")
        return self


@dataclass(frozen=True, slots=True)
class ClaudeFableRunResult:
    manifest: ClaudeFableRunManifest
    manifest_path: Path
    terminals: tuple[ClaudeFableAttemptTerminal, ...]


def load_claude_fable_judge_config(path: Path) -> LoadedClaudeFableJudgeConfig:
    loaded: LoadedConfig[ClaudeFableJudgeConfig] = load_config(path, ClaudeFableJudgeConfig)
    return LoadedClaudeFableJudgeConfig(
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
            raise ClaudeFableJudgeError(f"{label} is missing: {current}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ClaudeFableJudgeError(f"{label} contains a symlink component: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ClaudeFableJudgeError(f"{label} parent is not a directory: {current}")
    return absolute


def _write_immutable(path: Path, payload: bytes, *, label: str) -> str:
    path = _safe_absolute(path, label=label, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_absolute(path, label=label, allow_missing=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ClaudeFableJudgeError(f"immutable {label} conflicts at {path}")
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
                raise ClaudeFableJudgeError(f"concurrent immutable conflict: {path}") from None
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


def _load_canonical_authorization(
    path: Path,
) -> tuple[ClaudeFableLiveAuthorization, str]:
    path = _safe_absolute(path, label="Fable live authorization", allow_missing=False)
    if path.is_symlink() or not path.is_file():
        raise ClaudeFableAuthorizationError("live authorization is not a regular file")
    raw = path.read_bytes()
    try:
        payload = _strict_json(raw, label="Fable live authorization")
        authorization = ClaudeFableLiveAuthorization.model_validate(payload)
    except (ClaudeFableJudgeError, ValueError) as exc:
        raise ClaudeFableAuthorizationError(f"invalid live authorization: {exc}") from exc
    if raw != canonical_json_bytes(authorization.model_dump(mode="json")) + b"\n":
        raise ClaudeFableAuthorizationError("live authorization is not canonical JSON")
    return authorization, sha256_hex(raw)


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
        raise ClaudeFableAuthorizationError("authorization nonce must contain at least 32 bytes")
    authorization, digest = _load_canonical_authorization(authorization_path)
    expected = (
        batch_id,
        config_sha256,
        "judge_B",
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
        raise ClaudeFableAuthorizationError("live authorization differs from exact batch shard")
    require_utc(now)
    if now < authorization.approved_at or now > authorization.expires_at:
        raise ClaudeFableAuthorizationError("live authorization is not currently valid")
    return digest


def _utcnow() -> datetime.datetime:
    """Live wall clock seam; only offline tests may monkeypatch it."""

    return datetime.datetime.now(tz=datetime.UTC)


def _strict_json(raw: bytes, *, label: str) -> object:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    def nonfinite(value: str) -> float:
        raise ValueError(f"non-finite number {value!r}")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ClaudeFableJudgeError(f"invalid {label}: {exc}") from exc


def _judge_schema() -> dict[str, object]:
    return JudgeResponse.model_json_schema(by_alias=True)


def _argv(*, binary: Path, config: ClaudeFableJudgeConfig) -> tuple[str, ...]:
    schema = canonical_json_bytes(_judge_schema()).decode("utf-8")
    return (
        str(binary),
        "--print",
        "--safe-mode",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--no-chrome",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--model",
        config.model,
        "--effort",
        config.effort,
        "--system-prompt",
        _SYSTEM_PROMPT,
        "--json-schema",
        schema,
        "--output-format",
        "json",
    )


def _check_binary(config: ClaudeFableJudgeConfig) -> Path:
    observed = shutil.which("claude")
    if observed is None:
        raise ClaudeFableJudgeError("claude executable is not on PATH")
    binary = Path(observed).resolve(strict=True)
    configured = Path(config.claude_binary_path).resolve(strict=True)
    if binary != configured:
        raise ClaudeFableJudgeError("claude executable path differs from the frozen pin")
    if hash_file(binary) != config.claude_binary_sha256:
        raise ClaudeFableJudgeError("claude executable hash differs from the frozen pin")
    completed = subprocess.run(
        (str(binary), "--version"),
        check=False,
        capture_output=True,
        timeout=30,
    )
    version = (completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip()
    if completed.returncode != 0 or version != config.claude_cli_version:
        raise ClaudeFableJudgeError("claude CLI version differs from the frozen pin")
    return binary


def _parse_wrapper(stdout: bytes) -> JudgeResponse:
    wrapper = _strict_json(stdout, label="Claude JSON wrapper")
    if not isinstance(wrapper, dict):
        raise ClaudeFableJudgeError("Claude JSON wrapper must be an object")
    if (
        wrapper.get("type") != "result"
        or wrapper.get("subtype") != "success"
        or wrapper.get("is_error") is not False
    ):
        raise ClaudeFableJudgeError(
            "Claude JSON wrapper must be type=result, subtype=success, is_error=false"
        )
    structured = wrapper.get("structured_output")
    if not isinstance(structured, dict):
        raise ClaudeFableJudgeError("Claude JSON wrapper lacks object structured_output")
    try:
        return parse_blinded_judge_output(canonical_json_bytes(structured).decode("utf-8"))
    except JudgeOutputParseError as exc:
        raise ClaudeFableJudgeError(f"invalid structured judge response: {exc}") from exc


def _child_env(source: Mapping[str, str]) -> dict[str, str]:
    """Build the explicit environment allowed to reach the Claude child."""

    allowed = {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    }
    return {key: value for key, value in source.items() if key in allowed}


@contextlib.contextmanager
def _process_lock(output_root: Path) -> Iterator[None]:
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / ".fable-run.lock"
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ClaudeFableJudgeError(
                f"another Fable process owns the output lock: {lock_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _request_for_attempt(base: ProviderRequest, attempt_index: int) -> ProviderRequest:
    return ProviderRequest.create(
        identity=ProviderIdentity(
            provider=base.provider,
            model=base.model,
            revision=base.revision,
            transport="local",
        ),
        prompt_template_hash=base.prompt_template_hash,
        rendered_prompt=base.rendered_prompt,
        decoding=base.decoding,
        input_ids=base.input_ids,
        private_source_content=base.private_source_content,
        attempt_index=attempt_index,
    )


def _attempt_dir(output_root: Path, request_hash: str, attempt_index: int) -> Path:
    return output_root / "items" / request_hash[:2] / request_hash / f"attempt-{attempt_index:02d}"


def _terminal_bytes(terminal: ClaudeFableAttemptTerminal) -> bytes:
    return canonical_json_bytes(terminal.model_dump(mode="json")) + b"\n"


def _load_terminal(path: Path) -> ClaudeFableAttemptTerminal:
    if path.is_symlink() or not path.is_file():
        raise ClaudeFableJudgeError(f"terminal is missing or unsafe: {path}")
    raw = path.read_bytes()
    try:
        terminal = ClaudeFableAttemptTerminal.model_validate_json(raw)
    except ValueError as exc:
        raise ClaudeFableJudgeError(f"invalid terminal {path}: {exc}") from exc
    if raw != _terminal_bytes(terminal):
        raise ClaudeFableJudgeError(f"terminal is not canonical JSON: {path}")
    return terminal


def _load_receipt(path: Path) -> ClaudeFableProcessReceipt:
    if path.is_symlink() or not path.is_file():
        raise ClaudeFableJudgeError(f"process receipt is missing or unsafe: {path}")
    raw = path.read_bytes()
    try:
        receipt = ClaudeFableProcessReceipt.model_validate_json(raw)
    except ValueError as exc:
        raise ClaudeFableJudgeError(f"invalid process receipt {path}: {exc}") from exc
    canonical = canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n"
    if raw != canonical:
        raise ClaudeFableJudgeError(f"process receipt is not canonical JSON: {path}")
    return receipt


def _verify_attempt(
    *,
    config: ClaudeFableJudgeConfig,
    terminal: ClaudeFableAttemptTerminal,
    attempt_dir: Path,
    dispatch: LF022WeakDispatchRecord,
    base_request: ProviderRequest,
    request: ProviderRequest,
    raw_response_root: Path,
) -> None:
    if (
        terminal.dispatch_cell_id != dispatch.dispatch_cell_id
        or terminal.provider_request_hash != request.request_hash
        or terminal.provider_attempt_id != request.attempt_id
        or terminal.attempt_index != request.attempt_index
    ):
        raise ClaudeFableJudgeError("attempt terminal differs from dispatch/request")
    expected_argv = _argv(binary=Path(config.claude_binary_path), config=config)
    argv_path = attempt_dir / "argv.json"
    if (
        argv_path.is_symlink()
        or not argv_path.is_file()
        or argv_path.read_bytes() != canonical_json_bytes(list(expected_argv)) + b"\n"
        or hash_canonical(list(expected_argv)) != terminal.argv_sha256
    ):
        raise ClaudeFableJudgeError("attempt argv differs from the frozen CLI contract")
    request_path = attempt_dir / "provider_request.json"
    if load_provider_request(request_path) != request:
        raise ClaudeFableJudgeError("attempt provider request differs from retry lineage")
    required: list[tuple[str, str | None]] = [
        ("stdout.json", terminal.stdout_sha256),
        ("stderr.txt", terminal.stderr_sha256),
        ("redaction_report.json", terminal.redaction_report_sha256),
        ("process_receipt.json", None),
    ]
    if terminal.status == "completed":
        required.append(("parsed_response.json", terminal.parsed_response_sha256))
    for name, expected in required:
        path = attempt_dir / name
        if (
            path.is_symlink()
            or not path.is_file()
            or (expected is not None and hash_file(path) != expected)
        ):
            raise ClaudeFableJudgeError(f"attempt artifact differs: {path}")
    receipt = _load_receipt(attempt_dir / "process_receipt.json")
    if (
        receipt.provider_attempt_id != request.attempt_id
        or receipt.stdout_sha256 != terminal.stdout_sha256
        or receipt.stderr_sha256 != terminal.stderr_sha256
        or receipt.redaction_report_sha256 != terminal.redaction_report_sha256
        or receipt.redaction_count != terminal.redaction_count
    ):
        raise ClaudeFableJudgeError("attempt process receipt differs from terminal")
    attempt_root = attempt_dir.parents[3] / "provider_raw_attempts"
    raw_path = attempt_root / terminal.attempt_raw_response_artifact
    if raw_path != provider_raw_response_path(attempt_root, request):
        raise ClaudeFableJudgeError("attempt terminal points outside attempt raw root")
    raw = load_provider_raw_response(raw_path, request=request)
    if hash_file(raw_path) != terminal.attempt_raw_response_sha256:
        raise ClaudeFableJudgeError("attempt raw-response hash differs")
    if terminal.status == "completed":
        response = _parse_wrapper((attempt_dir / "stdout.json").read_bytes())
        parsed = canonical_json_bytes(response.model_dump(mode="json", by_alias=True))
        if (attempt_dir / "parsed_response.json").read_bytes() != parsed + b"\n":
            raise ClaudeFableJudgeError("completed parsed response does not replay")
        if raw.status != "success" or raw.output_text != parsed.decode("utf-8"):
            raise ClaudeFableJudgeError("completed raw response differs from parsed output")
        bridge_path = raw_response_root / str(terminal.replay_bridge_raw_response_artifact)
        if bridge_path != provider_raw_response_path(raw_response_root, base_request):
            raise ClaudeFableJudgeError("completed replay bridge points outside canonical root")
        load_provider_raw_response(bridge_path, request=base_request)
        if hash_file(bridge_path) != terminal.replay_bridge_raw_response_sha256:
            raise ClaudeFableJudgeError("completed replay bridge hash differs")
    elif raw.status != "error":
        raise ClaudeFableJudgeError("failed attempt must have an error raw response")


def _terminal(
    *,
    dispatch: LF022WeakDispatchRecord,
    request: ProviderRequest,
    attempt_index: int,
    status: str,
    capture: ClaudeCliCapture,
    argv_hash: str,
    stdout_hash: str,
    stderr_hash: str,
    redaction_report_sha256: str,
    redaction_count: int,
    parsed_hash: str | None,
    attempt_raw_response_artifact: str,
    attempt_raw_response_sha256: str,
    replay_bridge_raw_response_artifact: str | None,
    replay_bridge_raw_response_sha256: str | None,
    error: str | None,
) -> ClaudeFableAttemptTerminal:
    values: dict[str, object] = {
        "schema_version": 1,
        "method_version": FABLE_JUDGE_METHOD_VERSION,
        "dispatch_cell_id": dispatch.dispatch_cell_id,
        "provider_request_hash": request.request_hash,
        "provider_attempt_id": request.attempt_id,
        "attempt_index": attempt_index,
        "status": status,
        "exit_code": capture.exit_code,
        "argv_sha256": argv_hash,
        "stdout_sha256": stdout_hash,
        "stderr_sha256": stderr_hash,
        "redaction_report_sha256": redaction_report_sha256,
        "redaction_count": redaction_count,
        "parsed_response_sha256": parsed_hash,
        "attempt_raw_response_artifact": attempt_raw_response_artifact,
        "attempt_raw_response_sha256": attempt_raw_response_sha256,
        "replay_bridge_raw_response_artifact": replay_bridge_raw_response_artifact,
        "replay_bridge_raw_response_sha256": replay_bridge_raw_response_sha256,
        "error": error,
        "semantic_label_created": False,
        "human_label_created": False,
        "silver_promoted": False,
        "train_eligible": False,
        "eval_eligible": False,
        "gate_credit_claimed": False,
    }
    return ClaudeFableAttemptTerminal.model_validate(
        {**values, "terminal_id": make_id("lf022_fable_terminal", values)}
    )


def _run_attempt(
    *,
    config: ClaudeFableJudgeConfig,
    dispatch: LF022WeakDispatchRecord,
    base_request: ProviderRequest,
    request: ProviderRequest,
    output_root: Path,
    raw_response_root: Path,
    executor: ClaudeCliExecutor,
) -> ClaudeFableAttemptTerminal:
    # Re-check the binary immediately before every external call so a bounded
    # run cannot continue after an in-place CLI upgrade.
    binary = _check_binary(config)
    argv = _argv(binary=binary, config=config)
    argv_bytes = canonical_json_bytes(list(argv)) + b"\n"
    directory = _attempt_dir(output_root, request.request_hash, request.attempt_index)
    _write_immutable(directory / "argv.json", argv_bytes, label="Claude argv")
    _write_immutable(
        directory / "provider_request.json",
        canonical_json_bytes(request.model_dump(mode="json")) + b"\n",
        label="Claude provider request",
    )
    with tempfile.TemporaryDirectory(prefix="leanfaith-fable-empty-") as temporary:
        capture = executor.execute(
            argv=argv,
            prompt=request.rendered_prompt.encode("utf-8"),
            cwd=Path(temporary),
            env=_child_env(os.environ),
            timeout_seconds=config.timeout_seconds,
            termination_grace_seconds=config.termination_grace_seconds,
        )
    redacted = redact_captured_streams(
        {"stdout.json": capture.stdout, "stderr.txt": capture.stderr},
        environment=os.environ,
    )
    stdout_hash = _write_immutable(
        directory / "stdout.json", redacted.streams["stdout.json"], label="redacted Claude stdout"
    )
    stderr_hash = _write_immutable(
        directory / "stderr.txt", redacted.streams["stderr.txt"], label="redacted Claude stderr"
    )
    redaction_hash = _write_immutable(
        directory / "redaction_report.json",
        redacted.report_bytes,
        label="Claude capture redaction report",
    )
    receipt_values: dict[str, object] = {
        "schema_version": 1,
        "provider_attempt_id": request.attempt_id,
        "status": capture.status,
        "exit_code": capture.exit_code,
        "stdout_sha256": stdout_hash,
        "stderr_sha256": stderr_hash,
        "redaction_report_sha256": redaction_hash,
        "redaction_count": redacted.replacement_count,
    }
    receipt = ClaudeFableProcessReceipt.model_validate(
        {
            **receipt_values,
            "receipt_id": make_id("lf022_fable_process_receipt", receipt_values),
        }
    )
    _write_immutable(
        directory / "process_receipt.json",
        canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n",
        label="Claude process receipt",
    )
    return _materialize_terminal_from_receipt(
        config=config,
        dispatch=dispatch,
        base_request=base_request,
        request=request,
        output_root=output_root,
        raw_response_root=raw_response_root,
        receipt=receipt,
        propagate_interrupt=True,
    )


def _materialize_terminal_from_receipt(
    *,
    config: ClaudeFableJudgeConfig,
    dispatch: LF022WeakDispatchRecord,
    base_request: ProviderRequest,
    request: ProviderRequest,
    output_root: Path,
    raw_response_root: Path,
    receipt: ClaudeFableProcessReceipt,
    propagate_interrupt: bool,
) -> ClaudeFableAttemptTerminal:
    directory = _attempt_dir(output_root, request.request_hash, request.attempt_index)
    if receipt.provider_attempt_id != request.attempt_id:
        raise ClaudeFableJudgeError("process receipt differs from attempt request")
    stdout_path = directory / "stdout.json"
    stderr_path = directory / "stderr.txt"
    report_path = directory / "redaction_report.json"
    if (
        hash_file(stdout_path) != receipt.stdout_sha256
        or hash_file(stderr_path) != receipt.stderr_sha256
        or hash_file(report_path) != receipt.redaction_report_sha256
    ):
        raise ClaudeFableJudgeError("process receipt differs from redacted captures")
    stdout = stdout_path.read_bytes()
    stderr = stderr_path.read_bytes()
    status: str
    error: str | None = None
    parsed_hash: str | None = None
    response: JudgeResponse | None = None
    if receipt.redaction_count:
        status = "secret_redacted"
        error = "provider process output contained secret-like material and was redacted"
    elif len(stdout) > _MAX_CAPTURE_BYTES or len(stderr) > _MAX_CAPTURE_BYTES:
        status = "capture_too_large"
        error = "Claude stdout or stderr exceeded the bounded capture size"
    elif receipt.status == "timeout":
        status = "timeout"
        error = "Claude CLI timed out"
    elif receipt.status == "interrupted":
        status = "interrupted"
        error = "Claude CLI was interrupted"
    elif receipt.exit_code != 0:
        status = "process_failed"
        error = f"Claude CLI exited with code {receipt.exit_code}"
    else:
        try:
            response = _parse_wrapper(stdout)
        except ClaudeFableJudgeError as exc:
            message = str(exc)
            status = (
                "judge_parse_failed"
                if "structured judge response" in message
                else "wrapper_parse_failed"
            )
            error = message
        else:
            parsed = canonical_json_bytes(response.model_dump(mode="json", by_alias=True)) + b"\n"
            parsed_hash = _write_immutable(
                directory / "parsed_response.json", parsed, label="parsed Claude judgment"
            )
            status = "completed"
    if response is not None:
        attempt_raw = ProviderRawResponse.success(
            request,
            canonical_json_bytes(response.model_dump(mode="json", by_alias=True)).decode("utf-8"),
        )
    else:
        attempt_raw = ProviderRawResponse.error(
            request,
            error_type=status,
            error_detail=error,
        )
    attempt_root = output_root / "provider_raw_attempts"
    attempt_result = persist_provider_raw_response(attempt_root, attempt_raw)
    bridge_result = None
    if response is not None:
        bridge_result = persist_provider_raw_response(
            raw_response_root,
            ProviderRawResponse.success(
                base_request,
                canonical_json_bytes(response.model_dump(mode="json", by_alias=True)).decode(
                    "utf-8"
                ),
            ),
        )
    terminal = _terminal(
        dispatch=dispatch,
        request=request,
        attempt_index=request.attempt_index,
        status=status,
        capture=ClaudeCliCapture(
            status=receipt.status,
            exit_code=receipt.exit_code,
            stdout=stdout,
            stderr=stderr,
        ),
        argv_hash=hash_canonical(
            list(_argv(binary=Path(config.claude_binary_path), config=config))
        ),
        stdout_hash=receipt.stdout_sha256,
        stderr_hash=receipt.stderr_sha256,
        redaction_report_sha256=receipt.redaction_report_sha256,
        redaction_count=receipt.redaction_count,
        parsed_hash=parsed_hash,
        attempt_raw_response_artifact=str(
            attempt_result.raw_response_path.relative_to(attempt_root)
        ),
        attempt_raw_response_sha256=attempt_result.raw_response_sha256,
        replay_bridge_raw_response_artifact=(
            str(bridge_result.raw_response_path.relative_to(raw_response_root))
            if bridge_result is not None
            else None
        ),
        replay_bridge_raw_response_sha256=(
            bridge_result.raw_response_sha256 if bridge_result is not None else None
        ),
        error=error,
    )
    _write_immutable(
        directory / "terminal.json",
        _terminal_bytes(terminal),
        label="Claude terminal",
    )
    if status == "interrupted" and propagate_interrupt:
        raise KeyboardInterrupt
    return terminal


def _selected_cells(
    dispatches: Sequence[LF022WeakDispatchRecord],
    *,
    slot: JudgeSlot,
    offset_pairs: int,
    limit_pairs: int,
) -> tuple[LF022WeakDispatchRecord, ...]:
    if offset_pairs < 0:
        raise ClaudeFableJudgeError("offset_pairs must be nonnegative")
    by_pair: dict[str, list[LF022WeakDispatchRecord]] = {}
    ordered_pair_ids: list[str] = []
    for dispatch in dispatches:
        if dispatch.judge_slot == slot:
            if dispatch.pair_id not in by_pair:
                ordered_pair_ids.append(dispatch.pair_id)
            by_pair.setdefault(dispatch.pair_id, []).append(dispatch)
    selected: list[LF022WeakDispatchRecord] = []
    selected_pair_ids = ordered_pair_ids[offset_pairs : offset_pairs + limit_pairs]
    if len(selected_pair_ids) != limit_pairs:
        raise ClaudeFableJudgeError("requested pair shard exceeds prepared batch size")
    for pair_id in selected_pair_ids:
        cells = by_pair[pair_id]
        if {cell.orientation for cell in cells} != {"AB", "BA"} or len(cells) != 2:
            raise ClaudeFableJudgeError(f"pair lacks exact AB/BA Fable cells: {pair_id}")
        selected.extend(cells)
    return tuple(selected)


def _run_claude_fable_weak_cells_locked(
    *,
    batch_root: Path,
    raw_response_root: Path,
    output_root: Path,
    config_path: Path,
    judge_slot: JudgeSlot,
    shard_id: str,
    offset_pairs: int,
    limit_pairs: int,
    run_nonce: bytes,
    authorization_path: Path | None,
    authorization_nonce: bytes | None,
    execute_external: bool,
    executor: ClaudeCliExecutor | None = None,
) -> ClaudeFableRunResult:
    """Execute or resume the Fable cells of one prepared public weak batch."""

    if len(run_nonce) < 32:
        raise ClaudeFableJudgeError("run nonce must contain at least 32 bytes")
    if (
        not shard_id
        or len(shard_id) > 100
        or any(not (character.isalnum() or character in "_.-") for character in shard_id)
    ):
        raise ClaudeFableJudgeError("shard_id must be a short safe identifier")
    loaded = load_claude_fable_judge_config(config_path)
    config = loaded.config
    if limit_pairs < 1 or limit_pairs > config.max_pairs_per_invocation:
        raise ClaudeFableJudgeError(
            f"limit_pairs must be within 1..{config.max_pairs_per_invocation}"
        )
    batch_root = _safe_absolute(batch_root, label="weak batch root", allow_missing=False)
    raw_response_root = _safe_absolute(
        raw_response_root, label="provider raw root", allow_missing=True
    )
    output_root = _safe_absolute(output_root, label="Claude output root", allow_missing=True)
    spec, batch_manifest, dispatches, _ = _load_prepared_batch(batch_root)
    expected_raw_root = _safe_absolute(
        batch_root / "raw" / judge_slot,
        label="canonical Fable weak-batch raw root",
        allow_missing=True,
    )
    if raw_response_root != expected_raw_root:
        raise ClaudeFableJudgeError("provider raw root must be the canonical weak-batch slot root")
    created_at = _utcnow()
    require_utc(created_at)
    authorization_sha256: str | None = None
    if execute_external:
        if (
            not spec.live_provider_calls_authorized
            or spec.execution_authorization != "live_provider_calls_explicitly_authorized"
        ):
            raise ClaudeFableAuthorizationError(
                "prepared batch does not explicitly authorize live provider calls"
            )
        if authorization_path is None or authorization_nonce is None:
            raise ClaudeFableAuthorizationError(
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
    elif authorization_path is not None or authorization_nonce is not None:
        raise ClaudeFableAuthorizationError("offline replay rejects live-execution arguments")
    endpoint = _endpoint(spec, judge_slot)
    if (
        endpoint.provider != config.provider
        or endpoint.model != config.registry_model_id
        or endpoint.family_id != config.model_family
        or endpoint.revision != config.endpoint_revision
        or endpoint.decoding.get("effort") != config.effort
    ):
        raise ClaudeFableJudgeError("prepared Fable endpoint differs from the frozen CLI contract")
    if endpoint.decoding.get("system_prompt_sha256") != config.system_prompt_sha256:
        raise ClaudeFableJudgeError("prepared endpoint lacks the exact Fable system prompt pin")
    expected_decoding = {
        "effort": config.effort,
        "system_prompt_sha256": config.system_prompt_sha256,
        "output_schema_sha256": config.output_schema_sha256,
        "claude_cli_version": config.claude_cli_version,
        "claude_binary_sha256": config.claude_binary_sha256,
        "structured_output": True,
        "safe_mode": True,
        "tools_disabled": True,
        "session_persistence": False,
    }
    if endpoint.decoding != expected_decoding:
        raise ClaudeFableJudgeError("prepared Fable decoding differs from the frozen CLI contract")
    cells = _selected_cells(
        dispatches,
        slot=judge_slot,
        offset_pairs=offset_pairs,
        limit_pairs=limit_pairs,
    )
    if not cells:
        raise ClaudeFableJudgeError("prepared batch contains no Fable cells")
    if execute_external:
        persist_lf022_weak_execution_started_marker(
            batch_root=batch_root,
            dispatch_manifest=batch_manifest,
        )
    runner = executor or SubprocessClaudeCliExecutor()
    terminals: list[ClaudeFableAttemptTerminal] = []
    invoked_cells: set[str] = set()
    process_attempt_count = 0
    for dispatch in cells:
        base_request = _verify_dispatch_request(batch_root, dispatch, spec)
        if base_request.private_source_content or not dispatch.task.external_transmission_allowed:
            raise ClaudeFableJudgeError("private or transmission-forbidden task rejected")
        if base_request.prompt_template_hash != config.judge_template_sha256:
            raise ClaudeFableJudgeError("judge prompt template differs from Fable config")
        terminal: ClaudeFableAttemptTerminal | None = None
        for attempt_index in range(config.max_attempts_per_cell):
            request = _request_for_attempt(base_request, attempt_index)
            directory = _attempt_dir(output_root, request.request_hash, attempt_index)
            existing_path = directory / "terminal.json"
            if existing_path.exists():
                existing = _load_terminal(existing_path)
                _verify_attempt(
                    config=config,
                    terminal=existing,
                    attempt_dir=directory,
                    dispatch=dispatch,
                    base_request=base_request,
                    request=request,
                    raw_response_root=raw_response_root,
                )
                terminal = existing
                if existing.status == "completed":
                    terminal = existing
                    break
                continue
            if directory.exists() and any(directory.iterdir()):
                receipt_path = directory / "process_receipt.json"
                if not receipt_path.is_file() or receipt_path.is_symlink():
                    raise ClaudeFablePartialAttemptError(
                        "non-terminal attempt lacks a complete process receipt; refusing "
                        "recall or automatic advance pending explicit reviewed recovery"
                    )
                terminal = _materialize_terminal_from_receipt(
                    config=config,
                    dispatch=dispatch,
                    base_request=base_request,
                    request=request,
                    output_root=output_root,
                    raw_response_root=raw_response_root,
                    receipt=_load_receipt(receipt_path),
                    propagate_interrupt=False,
                )
                if terminal.status == "completed":
                    break
                continue
            if not execute_external:
                raise ClaudeFableJudgeError(
                    "offline replay is incomplete; a selected cell lacks a terminal attempt"
                )
            terminal = _run_attempt(
                config=config,
                dispatch=dispatch,
                base_request=base_request,
                request=request,
                output_root=output_root,
                raw_response_root=raw_response_root,
                executor=runner,
            )
            invoked_cells.add(dispatch.dispatch_cell_id)
            process_attempt_count += 1
            if terminal.status == "interrupted":
                raise KeyboardInterrupt
            if terminal.status == "completed":
                break
        if terminal is None:
            raise ClaudeFableJudgeError("attempt loop produced no terminal")
        terminals.append(terminal)
    status_counts = dict(sorted(Counter(item.status for item in terminals).items()))
    values: dict[str, object] = {
        "schema_version": 1,
        "method_version": FABLE_JUDGE_METHOD_VERSION,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "run_nonce_sha256": sha256_hex(run_nonce),
        "execution_mode": "external" if execute_external else "offline_replay",
        "authorization_sha256": authorization_sha256,
        "batch_id": batch_manifest.batch_id,
        "batch_manifest_sha256": hash_file(batch_root / "dispatch_manifest.json"),
        "config_sha256": loaded.sha256,
        "provider": config.provider,
        "model_family": config.model_family,
        "model": config.model,
        "effort": config.effort,
        "judge_slot": judge_slot,
        "shard_id": shard_id,
        "offset_pairs": offset_pairs,
        "selected_pair_count": len(cells) // 2,
        "selected_cell_count": len(cells),
        "invoked_cell_count": len(invoked_cells),
        "reused_cell_count": len(cells) - len(invoked_cells),
        "process_attempt_count": process_attempt_count,
        "completed_cell_count": sum(item.status == "completed" for item in terminals),
        "successful_attempt_ids_by_dispatch": {
            item.dispatch_cell_id: item.provider_attempt_id
            for item in terminals
            if item.status == "completed"
        },
        "terminal_status_counts": status_counts,
        "ordered_dispatch_ids_sha256": hash_canonical([item.dispatch_cell_id for item in cells]),
        "all_selected_cells_terminal": len(terminals) == len(cells),
        "both_orientations_selected": True,
        "private_source_content_transmitted": False,
        "semantic_labels_created": False,
        "human_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    manifest = ClaudeFableRunManifest.model_validate(
        {
            **values,
            "run_id": make_id("lf022_fable_run", values),
        }
    )
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    manifest_name = f"{manifest.run_id.removeprefix('lf022_fable_run:')}.json"
    manifest_path = output_root / "runs" / manifest_name
    _write_immutable(manifest_path, manifest_bytes, label="Claude run manifest")
    _write_atomic(
        output_root / "run_manifest.json",
        manifest_bytes,
    )
    return ClaudeFableRunResult(
        manifest=manifest,
        manifest_path=manifest_path,
        terminals=tuple(terminals),
    )


def run_claude_fable_weak_cells(
    *,
    batch_root: Path,
    raw_response_root: Path,
    output_root: Path,
    config_path: Path,
    judge_slot: JudgeSlot,
    shard_id: str,
    offset_pairs: int,
    limit_pairs: int,
    run_nonce: bytes,
    execute_external: bool,
    authorization_path: Path | None = None,
    authorization_nonce: bytes | None = None,
    executor: ClaudeCliExecutor | None = None,
) -> ClaudeFableRunResult:
    """Execute or strictly replay one bounded Fable shard under a process lock."""

    safe_output = _safe_absolute(
        output_root,
        label="Claude output root",
        allow_missing=True,
    )
    with _process_lock(safe_output):
        return _run_claude_fable_weak_cells_locked(
            batch_root=batch_root,
            raw_response_root=raw_response_root,
            output_root=safe_output,
            config_path=config_path,
            judge_slot=judge_slot,
            shard_id=shard_id,
            offset_pairs=offset_pairs,
            limit_pairs=limit_pairs,
            run_nonce=run_nonce,
            authorization_path=authorization_path,
            authorization_nonce=authorization_nonce,
            execute_external=execute_external,
            executor=executor,
        )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--raw-response-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--judge-slot", choices=("judge_A", "judge_B"), required=True)
    parser.add_argument("--shard-id", required=True)
    parser.add_argument("--offset-pairs", type=int, required=True)
    parser.add_argument("--limit-pairs", type=int, required=True)
    parser.add_argument("--run-nonce-file", type=Path, required=True)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--authorization-nonce-file", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--execute-public-external", action="store_true")
    mode.add_argument("--offline-replay", action="store_true")
    arguments = parser.parse_args()
    run_nonce = _safe_absolute(
        arguments.run_nonce_file,
        label="Fable run nonce",
        allow_missing=False,
    ).read_bytes()
    authorization_nonce = None
    if arguments.authorization_nonce_file is not None:
        authorization_nonce = _safe_absolute(
            arguments.authorization_nonce_file,
            label="Fable authorization nonce",
            allow_missing=False,
        ).read_bytes()
    result = run_claude_fable_weak_cells(
        batch_root=arguments.batch_root,
        raw_response_root=arguments.raw_response_root,
        output_root=arguments.output_root,
        config_path=arguments.config,
        judge_slot=arguments.judge_slot,
        shard_id=arguments.shard_id,
        offset_pairs=arguments.offset_pairs,
        limit_pairs=arguments.limit_pairs,
        run_nonce=run_nonce,
        execute_external=arguments.execute_public_external,
        authorization_path=arguments.authorization,
        authorization_nonce=authorization_nonce,
    )
    print(result.manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "ClaudeCliCapture",
    "ClaudeCliExecutor",
    "ClaudeFableAttemptTerminal",
    "ClaudeFableAuthorizationError",
    "ClaudeFableJudgeConfig",
    "ClaudeFableJudgeError",
    "ClaudeFableLiveAuthorization",
    "ClaudeFablePartialAttemptError",
    "ClaudeFableProcessReceipt",
    "ClaudeFableRunManifest",
    "ClaudeFableRunResult",
    "SubprocessClaudeCliExecutor",
    "load_claude_fable_judge_config",
    "run_claude_fable_weak_cells",
]
