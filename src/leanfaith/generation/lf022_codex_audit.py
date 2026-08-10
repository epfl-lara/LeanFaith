"""Resumable, audit-only Codex judgments for Lean-valid LF-022 pairs.

This module deliberately does less than the weak-supervision pipeline.  It
projects only public, denylist-cleared LF-022 source/candidate pairs whose
candidate already passed the mechanical Lean checker, invokes ``codex exec``
once per pair, and preserves the raw process artifacts before parsing the
structured answer.  Its outputs are diagnostics only: they are not semantic
labels, silver data, training data, evaluation data, or gate evidence.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_execution import LF022GOpenExecutionTask
from leanfaith.generation.lf022_lean_check import LF022LeanCheckRecord
from leanfaith.generation.weak_supervision import (
    JudgePresentation,
    JudgeResponse,
    PublicLeanJudgePair,
    make_swapped_presentations,
    parse_blinded_judge_output,
    render_blinded_judge_prompt,
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.variant import VariantRecord

LF022_CODEX_AUDIT_VERSION: Literal["lf022_codex_audit_v1"] = "lf022_codex_audit_v1"
DEFAULT_CODEX_AUDIT_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_REASONING_EFFORT = "xhigh"
_PRIVATE_MARKERS = ("formalmathatepfl/sft_classic", "sft_classic")
_VALID_OUTCOMES = frozenset({"elaborates", "elaborates_with_placeholder"})


class LF022CodexAuditError(RuntimeError):
    """An audit input, process, or immutable artifact violated its contract."""


class LF022CodexAuditPrivacyError(LF022CodexAuditError):
    """A private or non-transmissible input reached the audit boundary."""


class LF022CodexAuditInput(StrictModel):
    """Content-bound input for one audit-only Codex invocation."""

    schema_version: Literal[1] = 1
    audit_item_id: str = Field(pattern=id_pattern("lf022_codex_audit_item"))
    audit_only: Literal[True] = True
    lean_check_id: str = Field(pattern=id_pattern("lf022_lean_check"))
    variant_id: str = Field(pattern=id_pattern("var"))
    pair: PublicLeanJudgePair
    presentation: JudgePresentation
    source_task_sha256: str = Field(pattern=HEX64_PATTERN)
    source_variant_artifact_sha256: str = Field(pattern=HEX64_PATTERN)
    source_variant_line_sha256: str = Field(pattern=HEX64_PATTERN)
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.presentation.orientation != "AB":
            raise ValueError("audit input requires the canonical AB orientation")
        if self.presentation.pair_id != self.pair.pair_id:
            raise ValueError("presentation/pair ID mismatch")
        if (
            self.presentation.lean_a != self.pair.canonical_lean_a
            or self.presentation.lean_b != self.pair.canonical_lean_b
        ):
            raise ValueError("presentation is not the canonical source/candidate order")
        expected = _audit_item_id(self)
        if self.audit_item_id != expected:
            raise ValueError("audit_item_id does not match canonical input")
        return self


def _audit_item_id(item: LF022CodexAuditInput) -> str:
    return _audit_item_id_values(
        lean_check_id=item.lean_check_id,
        variant_id=item.variant_id,
        pair=item.pair,
        presentation=item.presentation,
        source_task_sha256=item.source_task_sha256,
        source_variant_artifact_sha256=item.source_variant_artifact_sha256,
        source_variant_line_sha256=item.source_variant_line_sha256,
    )


def _audit_item_id_values(
    *,
    lean_check_id: str,
    variant_id: str,
    pair: PublicLeanJudgePair,
    presentation: JudgePresentation,
    source_task_sha256: str,
    source_variant_artifact_sha256: str,
    source_variant_line_sha256: str,
) -> str:
    return make_id(
        "lf022_codex_audit_item",
        {
            "schema": LF022_CODEX_AUDIT_VERSION,
            "lean_check_id": lean_check_id,
            "variant_id": variant_id,
            "pair_id": pair.pair_id,
            "pair_admission_sha256": pair.admission_sha256,
            "presentation_task_id": presentation.task_id,
            "source_task_sha256": source_task_sha256,
            "source_variant_artifact_sha256": source_variant_artifact_sha256,
            "source_variant_line_sha256": source_variant_line_sha256,
        },
    )


class LF022CodexAuditAttempt(StrictModel):
    """Request metadata written before the external process starts."""

    schema_version: Literal[1] = 1
    audit_item_id: str = Field(pattern=id_pattern("lf022_codex_audit_item"))
    attempt_index: int = Field(ge=0, strict=True)
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=2)
    prompt_sha256: str = Field(pattern=HEX64_PATTERN)
    output_schema_sha256: str = Field(pattern=HEX64_PATTERN)
    timeout_seconds: int = Field(ge=1, strict=True)
    audit_only: Literal[True] = True
    private_source_content: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False


AuditTerminalStatus = Literal[
    "completed",
    "process_failed",
    "timeout",
    "interrupted",
    "final_output_missing",
    "parse_failed",
]


class LF022CodexAuditTerminal(StrictModel):
    """One immutable attempt result; only ``completed`` is resumable success."""

    schema_version: Literal[1] = 1
    audit_item_id: str = Field(pattern=id_pattern("lf022_codex_audit_item"))
    attempt_index: int = Field(ge=0, strict=True)
    status: AuditTerminalStatus
    exit_code: int | None
    stdout_sha256: str = Field(pattern=HEX64_PATTERN)
    stderr_sha256: str = Field(pattern=HEX64_PATTERN)
    final_message_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    parsed_response_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    error: str | None = None
    audit_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _terminal_contract(self) -> Self:
        if self.status == "completed":
            if self.exit_code != 0 or self.final_message_sha256 is None:
                raise ValueError("completed audit requires exit 0 and final output")
            if self.parsed_response_sha256 is None or self.error is not None:
                raise ValueError("completed audit requires parsed response and no error")
        elif self.parsed_response_sha256 is not None:
            raise ValueError("non-completed audit cannot bind a parsed response")
        return self


class LF022CodexAuditManifest(StrictModel):
    """Mutable summary rebuilt from immutable item and attempt artifacts."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_codex_audit_v1"] = LF022_CODEX_AUDIT_VERSION
    checks_artifact: str
    checks_sha256: str = Field(pattern=HEX64_PATTERN)
    model: str
    reasoning_effort: str
    eligible_count: int = Field(ge=0, strict=True)
    completed_count: int = Field(ge=0, strict=True)
    reused_count: int = Field(ge=0, strict=True)
    invoked_count: int = Field(ge=0, strict=True)
    exhausted_count: int = Field(ge=0, strict=True)
    attempt_status_counts: dict[str, int]
    ordered_audit_item_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    audit_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class ProcessCapture:
    status: Literal["completed", "timeout", "interrupted"]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    final_message: bytes | None


class CodexAuditExecutor(Protocol):
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


class SubprocessCodexAuditExecutor:
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
    ) -> ProcessCapture:
        if final_message_path.exists():
            raise LF022CodexAuditError("final-message path must be fresh")
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
        final_message = final_message_path.read_bytes() if final_message_path.is_file() else None
        return ProcessCapture(
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


def _write_immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise LF022CodexAuditError(f"immutable artifact conflict: {path}")
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
                raise LF022CodexAuditError(f"concurrent immutable conflict: {path}") from None
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
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _judge_response_output_schema() -> dict[str, object]:
    """Return the Codex structured-output form of the strict judge schema.

    OpenAI structured outputs require every declared property to be listed in
    ``required``.  Pydantic omits fields with defaults from that list even
    though our parser supplies deterministic defaults, so make the wire schema
    fully explicit while keeping the same parser model.
    """

    schema = cast(dict[str, object], JudgeResponse.model_json_schema(by_alias=True))
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not all(isinstance(key, str) for key in properties):
        raise LF022CodexAuditError("JudgeResponse JSON schema lacks string properties")
    schema["required"] = sorted(cast(dict[str, object], properties))
    schema["additionalProperties"] = False
    return schema


def _resolve_artifact(path_text: str, *, repo_root: Path) -> Path:
    path = Path(path_text)
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _load_variant_for_check(
    check: LF022LeanCheckRecord, *, repo_root: Path
) -> tuple[VariantRecord, Path, bytes]:
    artifact = _resolve_artifact(check.source_variant_artifact, repo_root=repo_root)
    if artifact.is_symlink() or not artifact.is_file():
        raise LF022CodexAuditError(f"source variant artifact is missing: {artifact}")
    if hash_file(artifact) != check.source_variant_artifact_sha256:
        raise LF022CodexAuditError(f"source variant artifact hash mismatch: {artifact}")
    lines = artifact.read_bytes().splitlines(keepends=True)
    try:
        line = lines[check.source_variant_line_number - 1]
    except IndexError as exc:
        raise LF022CodexAuditError(f"source variant line is missing: {artifact}") from exc
    if sha256_hex(line) != check.source_variant_line_sha256:
        raise LF022CodexAuditError(f"source variant line hash mismatch: {artifact}")
    try:
        variant = VariantRecord.model_validate_json(line)
    except ValueError as exc:
        raise LF022CodexAuditError(f"invalid source variant line: {artifact}: {exc}") from exc
    if (
        variant.variant_id != check.variant_id
        or variant.candidate_code_hash != check.candidate_code_hash
        or variant.extracted_statement is None
    ):
        raise LF022CodexAuditError(f"Lean check does not bind the source variant: {artifact}")
    return variant, artifact, line


def _make_audit_input(
    check: LF022LeanCheckRecord,
    *,
    repo_root: Path,
) -> LF022CodexAuditInput:
    if check.outcome not in _VALID_OUTCOMES or not check.declaration_verified:
        raise LF022CodexAuditError(f"Lean check {check.check_id} is not Lean-valid")
    variant, artifact, line = _load_variant_for_check(check, repo_root=repo_root)
    task_path = artifact.with_name("task.json")
    if task_path.is_symlink() or not task_path.is_file():
        raise LF022CodexAuditError(f"LF-022 task is missing beside variant: {task_path}")
    try:
        task = LF022GOpenExecutionTask.model_validate_json(task_path.read_bytes())
    except ValueError as exc:
        raise LF022CodexAuditPrivacyError(
            f"task is not an admitted public LF-022 input: {exc}"
        ) from exc
    source = task.source
    if (
        source.source_id != check.source_id
        or source.source_revision != check.source_revision
        or source.context_id != check.context_id
    ):
        raise LF022CodexAuditError("task, variant, and Lean-check source bindings differ")
    serialized = canonical_json_bytes(
        {
            "source": source.model_dump(mode="json"),
            "candidate": variant.extracted_statement,
        }
    ).decode("utf-8")
    if any(marker in serialized.casefold() for marker in _PRIVATE_MARKERS):
        raise LF022CodexAuditPrivacyError("private sft_classic content is forbidden")
    if (
        not source.source_is_public
        or not source.external_transmission_allowed
        or not source.denylist_checked
        or source.denylist_hits
    ):
        raise LF022CodexAuditPrivacyError("source is not public, transmissible, and denylist-clear")
    pair_id = make_id(
        "pair",
        {
            "schema": "lf022_codex_audit_pair_v1",
            "source_theorem_id": source.source_theorem_id,
            "variant_id": variant.variant_id,
            "source_statement_sha256": sha256_hex(source.source_statement.encode("utf-8")),
            "candidate_statement_sha256": sha256_hex(
                cast(str, variant.extracted_statement).encode("utf-8")
            ),
        },
    )
    pair = PublicLeanJudgePair(
        pair_id=pair_id,
        canonical_lean_a=source.source_statement,
        canonical_lean_b=cast(str, variant.extracted_statement),
        optional_natural_language=source.optional_natural_language,
        source_record_ids=tuple(sorted((source.source_theorem_id, variant.variant_id))),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )
    randomization_key = bytes.fromhex(
        hash_canonical(
            {
                "schema": "lf022_codex_audit_orientation_v1",
                "check_id": check.check_id,
                "pair_id": pair_id,
            }
        )
    )
    presentations = make_swapped_presentations(
        source=pair,
        judge_slot="judge_A",
        randomization_key=randomization_key,
    )
    presentation = next(item for item in presentations if item.orientation == "AB")
    values: dict[str, object] = {
        "schema_version": 1,
        "audit_only": True,
        "lean_check_id": check.check_id,
        "variant_id": variant.variant_id,
        "pair": pair,
        "presentation": presentation,
        "source_task_sha256": hash_file(task_path),
        "source_variant_artifact_sha256": hash_file(artifact),
        "source_variant_line_sha256": sha256_hex(line),
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    audit_item_id = _audit_item_id_values(
        lean_check_id=check.check_id,
        variant_id=variant.variant_id,
        pair=pair,
        presentation=presentation,
        source_task_sha256=cast(str, values["source_task_sha256"]),
        source_variant_artifact_sha256=cast(str, values["source_variant_artifact_sha256"]),
        source_variant_line_sha256=cast(str, values["source_variant_line_sha256"]),
    )
    return LF022CodexAuditInput.model_validate({**values, "audit_item_id": audit_item_id})


def load_lean_valid_audit_inputs(
    *,
    checks_path: Path,
    repo_root: Path,
    limit: int | None = None,
) -> tuple[LF022CodexAuditInput, ...]:
    """Project only mechanically valid LF-022 checks into audit inputs."""

    if limit is not None and limit < 1:
        raise LF022CodexAuditError("limit must be positive")
    if checks_path.is_symlink() or not checks_path.is_file():
        raise LF022CodexAuditError(f"checks artifact is missing: {checks_path}")
    inputs: list[LF022CodexAuditInput] = []
    seen_checks: set[str] = set()
    seen_items: set[str] = set()
    for line_number, raw in enumerate(checks_path.read_bytes().splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n"):
            raise LF022CodexAuditError(f"checks line lacks final newline: {line_number}")
        try:
            check = LF022LeanCheckRecord.model_validate_json(raw)
        except ValueError as exc:
            raise LF022CodexAuditError(f"invalid check line {line_number}: {exc}") from exc
        if check.check_id in seen_checks:
            raise LF022CodexAuditError(f"duplicate check ID {check.check_id}")
        seen_checks.add(check.check_id)
        if check.outcome not in _VALID_OUTCOMES or not check.declaration_verified:
            continue
        item = _make_audit_input(check, repo_root=repo_root)
        if item.audit_item_id in seen_items:
            raise LF022CodexAuditError(f"duplicate audit item {item.audit_item_id}")
        seen_items.add(item.audit_item_id)
        inputs.append(item)
        if limit is not None and len(inputs) >= limit:
            break
    return tuple(inputs)


def _item_dir(output_root: Path, audit_item_id: str) -> Path:
    digest = audit_item_id.removeprefix("lf022_codex_audit_item:")
    return output_root / "items" / digest[:2] / digest


def _build_argv(
    *,
    model: str,
    reasoning_effort: str,
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
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--json",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
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


def _attempt_count(item_dir: Path) -> int:
    attempts = item_dir / "attempts"
    if not attempts.exists():
        return 0
    names = sorted(path.name for path in attempts.iterdir() if path.is_dir())
    expected = [f"{index:04d}" for index in range(len(names))]
    if names != expected:
        raise LF022CodexAuditError(f"non-contiguous attempt directories: {attempts}")
    return len(names)


def _load_completed(path: Path, item: LF022CodexAuditInput) -> LF022CodexAuditTerminal:
    try:
        terminal = LF022CodexAuditTerminal.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise LF022CodexAuditError(f"invalid completed audit {path}: {exc}") from exc
    if terminal.status != "completed" or terminal.audit_item_id != item.audit_item_id:
        raise LF022CodexAuditError(f"completed audit does not bind input: {path}")
    return terminal


def _run_one(
    *,
    item: LF022CodexAuditInput,
    output_root: Path,
    output_schema_path: Path,
    model: str,
    reasoning_effort: str,
    timeout_seconds: int,
    termination_grace_seconds: int,
    executor: CodexAuditExecutor,
) -> LF022CodexAuditTerminal:
    item_dir = _item_dir(output_root, item.audit_item_id)
    _write_immutable(item_dir / "input.json", _canonical_line(item))
    rendered = render_blinded_judge_prompt(item.presentation)
    prompt = rendered.text.encode("utf-8")
    _write_immutable(item_dir / "prompt.txt", prompt)
    attempt_index = _attempt_count(item_dir)
    attempt_dir = item_dir / "attempts" / f"{attempt_index:04d}"
    workspace = attempt_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    final_path = attempt_dir / "final_message.json"
    argv = _build_argv(
        model=model,
        reasoning_effort=reasoning_effort,
        output_schema_path=output_schema_path,
        final_message_path=final_path,
    )
    attempt = LF022CodexAuditAttempt(
        audit_item_id=item.audit_item_id,
        attempt_index=attempt_index,
        model=model,
        reasoning_effort=reasoning_effort,
        argv=argv,
        prompt_sha256=sha256_hex(prompt),
        output_schema_sha256=hash_file(output_schema_path),
        timeout_seconds=timeout_seconds,
    )
    _write_immutable(attempt_dir / "request.json", _canonical_line(attempt))
    capture = executor.execute(
        argv=argv,
        prompt=prompt,
        cwd=workspace,
        final_message_path=final_path,
        timeout_seconds=timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )

    # Raw process artifacts are immutable and land before any response parsing.
    stdout_hash = _write_immutable(attempt_dir / "stdout.jsonl", capture.stdout)
    stderr_hash = _write_immutable(attempt_dir / "stderr.txt", capture.stderr)
    final_hash: str | None = None
    if capture.final_message is not None:
        final_hash = _write_immutable(attempt_dir / "final_message.json", capture.final_message)

    status: AuditTerminalStatus
    error: str | None = None
    parsed_hash: str | None = None
    if capture.status == "timeout":
        status = "timeout"
        error = "codex exec timed out"
    elif capture.status == "interrupted":
        status = "interrupted"
        error = "codex exec was interrupted"
    elif capture.exit_code != 0:
        status = "process_failed"
        error = f"codex exec exited with code {capture.exit_code}"
    elif capture.final_message is None:
        status = "final_output_missing"
        error = "codex exec did not create the final message"
    else:
        try:
            response = parse_blinded_judge_output(capture.final_message.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            status = "parse_failed"
            error = str(exc)
        else:
            parsed_hash = _write_immutable(
                attempt_dir / "parsed_response.json", _canonical_line(response)
            )
            status = "completed"
    terminal = LF022CodexAuditTerminal(
        audit_item_id=item.audit_item_id,
        attempt_index=attempt_index,
        status=status,
        exit_code=capture.exit_code,
        stdout_sha256=stdout_hash,
        stderr_sha256=stderr_hash,
        final_message_sha256=final_hash,
        parsed_response_sha256=parsed_hash,
        error=error,
    )
    _write_immutable(attempt_dir / "terminal.json", _canonical_line(terminal))
    if terminal.status == "completed":
        _write_immutable(item_dir / "completed.json", _canonical_line(terminal))
    return terminal


@dataclass(frozen=True, slots=True)
class LF022CodexAuditRunResult:
    manifest: LF022CodexAuditManifest
    manifest_path: Path
    terminals: tuple[LF022CodexAuditTerminal, ...]


def audit_lean_valid_lf022_pairs(
    *,
    repo_root: Path,
    checks_path: Path,
    output_root: Path,
    model: str = DEFAULT_CODEX_AUDIT_MODEL,
    reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT,
    timeout_seconds: int = 1800,
    termination_grace_seconds: int = 10,
    max_attempts: int = 2,
    limit: int | None = None,
    executor: CodexAuditExecutor | None = None,
) -> LF022CodexAuditRunResult:
    """Audit Lean-valid LF-022 pairs sequentially, one Codex process per pair."""

    if timeout_seconds < 1 or termination_grace_seconds < 1 or max_attempts < 1:
        raise LF022CodexAuditError("timeouts, grace, and max_attempts must be positive")
    if not model.strip() or not reasoning_effort.strip():
        raise LF022CodexAuditError("model and reasoning effort must be nonempty")
    repo_root = repo_root.resolve()
    checks_path = checks_path.resolve()
    output_root = output_root.resolve()
    if output_root == checks_path.parent or output_root.is_relative_to(checks_path.parent):
        raise LF022CodexAuditError("audit output must not be inside the immutable check root")
    items = load_lean_valid_audit_inputs(
        checks_path=checks_path,
        repo_root=repo_root,
        limit=limit,
    )
    output_root.mkdir(parents=True, exist_ok=True)
    schema_bytes = canonical_json_bytes(_judge_response_output_schema()) + b"\n"
    schema_hash = sha256_hex(schema_bytes)
    schema_path = output_root / "schemas" / f"judge_response.{schema_hash}.schema.json"
    _write_immutable(schema_path, schema_bytes)
    runner = executor or SubprocessCodexAuditExecutor()
    terminals: list[LF022CodexAuditTerminal] = []
    reused = 0
    invoked = 0
    exhausted = 0
    for item in items:
        completed_path = _item_dir(output_root, item.audit_item_id) / "completed.json"
        if completed_path.is_file():
            terminals.append(_load_completed(completed_path, item))
            reused += 1
            continue
        if _attempt_count(_item_dir(output_root, item.audit_item_id)) >= max_attempts:
            exhausted += 1
            continue
        terminal = _run_one(
            item=item,
            output_root=output_root,
            output_schema_path=schema_path,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            executor=runner,
        )
        terminals.append(terminal)
        invoked += 1
    status_counts: dict[str, int] = dict(
        sorted(Counter(str(item.status) for item in terminals).items())
    )
    manifest = LF022CodexAuditManifest(
        checks_artifact=str(checks_path),
        checks_sha256=hash_file(checks_path),
        model=model,
        reasoning_effort=reasoning_effort,
        eligible_count=len(items),
        completed_count=sum(item.status == "completed" for item in terminals),
        reused_count=reused,
        invoked_count=invoked,
        exhausted_count=exhausted,
        attempt_status_counts=status_counts,
        ordered_audit_item_ids_sha256=hash_canonical([item.audit_item_id for item in items]),
    )
    manifest_path = output_root / "manifest.json"
    _write_atomic(manifest_path, _canonical_line(manifest))
    return LF022CodexAuditRunResult(
        manifest=manifest,
        manifest_path=manifest_path,
        terminals=tuple(terminals),
    )


__all__ = [
    "DEFAULT_CODEX_AUDIT_MODEL",
    "DEFAULT_CODEX_REASONING_EFFORT",
    "LF022CodexAuditError",
    "LF022CodexAuditInput",
    "LF022CodexAuditManifest",
    "LF022CodexAuditPrivacyError",
    "LF022CodexAuditRunResult",
    "LF022CodexAuditTerminal",
    "ProcessCapture",
    "SubprocessCodexAuditExecutor",
    "audit_lean_valid_lf022_pairs",
    "load_lean_valid_audit_inputs",
]
