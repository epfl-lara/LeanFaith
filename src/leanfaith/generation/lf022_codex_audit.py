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

LF022_CODEX_AUDIT_VERSION: Literal["lf022_codex_audit_v2"] = "lf022_codex_audit_v2"
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
    method_version: Literal["lf022_codex_audit_v2"] = LF022_CODEX_AUDIT_VERSION
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


class LF022CodexAuditParentBinding(StrictModel):
    """Verified lineage for item trees copied from one earlier audit root."""

    schema_version: Literal[1] = 1
    audit_root: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    checks_sha256: str = Field(pattern=HEX64_PATTERN)
    response_artifact_set_sha256: str = Field(pattern=HEX64_PATTERN)
    reused_item_count: int = Field(gt=0, strict=True)
    ordered_reused_audit_item_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    copied_item_tree_set_sha256: str = Field(pattern=HEX64_PATTERN)


def _parent_binding_id_payloads(
    bindings: Sequence[LF022CodexAuditParentBinding],
) -> list[dict[str, object]]:
    """Remove machine-local locators while retaining all content bindings."""

    return [item.model_dump(mode="json", exclude={"audit_root"}) for item in bindings]


class LF022CodexAuditSummaryBucket(StrictModel):
    """Aggregate verdict diagnostics for one proposer or the whole audit."""

    total_count: int = Field(ge=0, strict=True)
    same_claim_counts: dict[str, int]
    relation_counts: dict[str, int]
    implication_counts: dict[str, int]
    error_type_counts: dict[str, int]
    needs_expert_review_count: int = Field(ge=0, strict=True)
    confidence_count: int = Field(ge=0, strict=True)
    confidence_mean: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_min: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_max: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _reconciled(self) -> Self:
        if sum(self.same_claim_counts.values()) != self.total_count:
            raise ValueError("same-claim counts do not reconcile")
        if sum(self.relation_counts.values()) != self.total_count:
            raise ValueError("relation counts do not reconcile")
        if sum(self.implication_counts.values()) != self.total_count:
            raise ValueError("implication counts do not reconcile")
        if self.needs_expert_review_count > self.total_count:
            raise ValueError("expert-review count exceeds total")
        if self.confidence_count > self.total_count:
            raise ValueError("confidence count exceeds total")
        values = (self.confidence_mean, self.confidence_min, self.confidence_max)
        if self.confidence_count == 0:
            if any(value is not None for value in values):
                raise ValueError("empty confidence aggregate must be null")
        elif any(value is None for value in values):
            raise ValueError("nonempty confidence aggregate must be complete")
        elif cast(float, self.confidence_min) > cast(float, self.confidence_max):
            raise ValueError("confidence minimum exceeds maximum")
        return self


class LF022CodexAuditSummary(StrictModel):
    """Hash-bound diagnostic summary; explicitly not semantic supervision."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_codex_audit_summary_v1"] = "lf022_codex_audit_summary_v1"
    summary_id: str = Field(pattern=id_pattern("lf022_codex_audit_summary"))
    audit_manifest: str
    audit_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    audit_method_version: Literal["lf022_codex_audit_v2"]
    checks_artifact: str
    checks_sha256: str = Field(pattern=HEX64_PATTERN)
    response_artifact_set_sha256: str = Field(pattern=HEX64_PATTERN)
    parent_audit_bindings: tuple[LF022CodexAuditParentBinding, ...] = ()
    findings_artifact: str
    findings_sha256: str = Field(pattern=HEX64_PATTERN)
    model: str
    reasoning_effort: str
    total_check_count: int = Field(ge=0, strict=True)
    lean_check_outcome_counts: dict[str, int]
    lean_valid_check_count: int = Field(ge=0, strict=True)
    lean_invalid_check_count: int = Field(ge=0, strict=True)
    completed_judgment_count: int = Field(ge=0, strict=True)
    overall: LF022CodexAuditSummaryBucket
    by_proposer_family: dict[str, LF022CodexAuditSummaryBucket]
    audit_only: Literal[True] = True
    human_labels_created: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent_summary(self) -> Self:
        if sum(self.lean_check_outcome_counts.values()) != self.total_check_count:
            raise ValueError("Lean-check outcome counts do not reconcile")
        expected_valid = sum(
            count
            for outcome, count in self.lean_check_outcome_counts.items()
            if outcome in _VALID_OUTCOMES
        )
        if self.lean_valid_check_count != expected_valid:
            raise ValueError("Lean-valid count differs from outcome counts")
        if self.lean_invalid_check_count != self.lean_check_outcome_counts.get("invalid", 0):
            raise ValueError("Lean-invalid count differs from outcome counts")
        if self.lean_valid_check_count + self.lean_invalid_check_count != self.total_check_count:
            raise ValueError("final summary requires every non-valid check to be invalid")
        if self.completed_judgment_count != self.lean_valid_check_count:
            raise ValueError("summary requires one completed judgment per Lean-valid check")
        if self.overall.total_count != self.completed_judgment_count:
            raise ValueError("overall judgment total does not reconcile")
        if sum(bucket.total_count for bucket in self.by_proposer_family.values()) != (
            self.completed_judgment_count
        ):
            raise ValueError("proposer-family totals do not reconcile")
        excluded = {
            "summary_id",
            "audit_manifest",
            "checks_artifact",
            "findings_artifact",
        }
        # Preserve validation of summaries created before composite lineage was
        # supported.  New composite summaries include the nonempty binding in
        # their content-addressed identity.
        if not self.parent_audit_bindings:
            excluded.add("parent_audit_bindings")
        values = self.model_dump(mode="json", exclude=excluded)
        if self.parent_audit_bindings:
            values["parent_audit_bindings"] = _parent_binding_id_payloads(
                self.parent_audit_bindings
            )
        expected = make_id("lf022_codex_audit_summary", values)
        if self.summary_id != expected:
            raise ValueError("summary_id does not match summary content")
        return self


@dataclass(frozen=True, slots=True)
class LF022CodexAuditSummaryResult:
    summary: LF022CodexAuditSummary
    json_path: Path
    markdown_path: Path
    findings_path: Path


class LF022CodexAuditFinding(StrictModel):
    """One compact audit-only verdict bound to its public pair and raw response."""

    schema_version: Literal[1] = 1
    finding_id: str = Field(pattern=id_pattern("lf022_codex_audit_finding"))
    audit_item_id: str = Field(pattern=id_pattern("lf022_codex_audit_item"))
    lean_check_id: str = Field(pattern=id_pattern("lf022_lean_check"))
    pair_id: str = Field(pattern=id_pattern("pair"))
    variant_id: str = Field(pattern=id_pattern("var"))
    source_record_ids: tuple[str, ...] = Field(min_length=2)
    proposer_family_id: str = Field(min_length=1)
    same_claim_answer: Literal[
        "same_claim",
        "not_same_claim",
        "ambiguous",
        "uncertain",
    ]
    relation: str | None
    a_implies_b: Literal["yes", "no", "unknown"]
    b_implies_a: Literal["yes", "no", "unknown"]
    error_types: tuple[str, ...]
    confidence: float = Field(ge=0.0, le=1.0)
    needs_expert_review: bool
    final_message_sha256: str = Field(pattern=HEX64_PATTERN)
    parsed_response_sha256: str = Field(pattern=HEX64_PATTERN)
    audit_only: Literal[True] = True
    human_label: Literal[False] = False
    semantic_label: Literal[False] = False
    silver_record: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        values = self.model_dump(mode="json", exclude={"finding_id"})
        expected = make_id("lf022_codex_audit_finding", values)
        if self.finding_id != expected:
            raise ValueError("finding_id does not match finding content")
        return self


@dataclass(frozen=True, slots=True)
class LF022VerifiedCodexAuditJudgment:
    audit_item_id: str
    lean_check_id: str
    pair_id: str
    variant_id: str
    source_record_ids: tuple[str, ...]
    proposer_family_id: str
    response: JudgeResponse
    final_message_sha256: str
    parsed_response_sha256: str


@dataclass(frozen=True, slots=True)
class LF022VerifiedCodexAudit:
    """Fully replay-verified, audit-only Codex evidence and its exact binding."""

    manifest: LF022CodexAuditManifest
    manifest_path: Path
    checks: tuple[LF022LeanCheckRecord, ...]
    lean_check_outcome_counts: dict[str, int]
    items: tuple[LF022CodexAuditInput, ...]
    judgments: tuple[LF022VerifiedCodexAuditJudgment, ...]
    response_artifact_set_sha256: str
    parent_audit_bindings: tuple[LF022CodexAuditParentBinding, ...] = ()


# Compatibility alias for existing internal tests and callers; new code uses the public name.
_VerifiedAuditJudgment = LF022VerifiedCodexAuditJudgment


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


def _regular_tree_inventory(root: Path) -> tuple[dict[str, object], ...]:
    """Describe one item tree without following links or accepting special files."""

    if root.is_symlink() or not root.is_dir():
        raise LF022CodexAuditError(f"audit item tree is not a real directory: {root}")
    entries: list[dict[str, object]] = []
    for current_text, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_text)
        for name in sorted(directory_names):
            path = current / name
            if path.is_symlink() or not path.is_dir():
                raise LF022CodexAuditError(f"audit item tree contains a linked directory: {path}")
            entries.append(
                {
                    "kind": "directory",
                    "path": path.relative_to(root).as_posix(),
                }
            )
        for name in sorted(file_names):
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise LF022CodexAuditError(f"audit item tree contains a non-file: {path}")
            entries.append(
                {
                    "kind": "file",
                    "path": path.relative_to(root).as_posix(),
                    "byte_count": path.stat().st_size,
                    "sha256": hash_file(path),
                }
            )
    return tuple(sorted(entries, key=lambda item: (cast(str, item["path"]), item["kind"])))


def _assert_byte_identical_item_tree(*, copied: Path, parent: Path) -> str:
    copied_inventory = _regular_tree_inventory(copied)
    parent_inventory = _regular_tree_inventory(parent)
    if copied_inventory != parent_inventory:
        raise LF022CodexAuditError(
            f"copied audit item tree differs from its declared parent: {copied}"
        )
    return hash_canonical(copied_inventory)


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


def _load_check_inventory(checks_path: Path) -> tuple[LF022LeanCheckRecord, ...]:
    if checks_path.is_symlink() or not checks_path.is_file():
        raise LF022CodexAuditError(f"checks artifact is missing: {checks_path}")
    records: list[LF022LeanCheckRecord] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(checks_path.read_bytes().splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n"):
            raise LF022CodexAuditError(f"checks line lacks final newline: {line_number}")
        try:
            record = LF022LeanCheckRecord.model_validate_json(raw)
        except ValueError as exc:
            raise LF022CodexAuditError(f"invalid check line {line_number}: {exc}") from exc
        if record.check_id in seen:
            raise LF022CodexAuditError(f"duplicate check ID {record.check_id}")
        seen.add(record.check_id)
        records.append(record)
    return tuple(records)


def _request_path_argument(argv: tuple[str, ...], option: str) -> Path:
    try:
        index = argv.index(option)
        value = argv[index + 1]
    except (ValueError, IndexError) as exc:
        raise LF022CodexAuditError(f"Codex request lacks {option}") from exc
    return Path(value).resolve()


def _proposer_family_for_check(
    check: LF022LeanCheckRecord,
    item: LF022CodexAuditInput,
    *,
    repo_root: Path,
) -> str:
    _, artifact, _ = _load_variant_for_check(check, repo_root=repo_root)
    task_path = artifact.with_name("task.json")
    if task_path.is_symlink() or not task_path.is_file():
        raise LF022CodexAuditError(f"LF-022 task is missing beside variant: {task_path}")
    if hash_file(task_path) != item.source_task_sha256:
        raise LF022CodexAuditError(f"audit input does not bind task: {task_path}")
    try:
        task = LF022GOpenExecutionTask.model_validate_json(task_path.read_bytes())
    except ValueError as exc:
        raise LF022CodexAuditError(f"invalid LF-022 task {task_path}: {exc}") from exc
    return task.allocation_task.proposer_family_id


def _make_summary_bucket(
    judgments: Sequence[LF022VerifiedCodexAuditJudgment],
) -> LF022CodexAuditSummaryBucket:
    same_claim = Counter(item.response.same_claim_answer for item in judgments)
    relations = Counter(
        item.response.relation.value if item.response.relation is not None else "null"
        for item in judgments
    )
    implications = Counter(
        f"A={item.response.a_implies_b},B={item.response.b_implies_a}" for item in judgments
    )
    errors = Counter(error_type for item in judgments for error_type in item.response.error_types)
    confidences = [item.response.confidence for item in judgments]
    return LF022CodexAuditSummaryBucket(
        total_count=len(judgments),
        same_claim_counts=dict(sorted(same_claim.items())),
        relation_counts=dict(sorted(relations.items())),
        implication_counts=dict(sorted(implications.items())),
        error_type_counts=dict(sorted(errors.items())),
        needs_expert_review_count=sum(item.response.needs_expert_review for item in judgments),
        confidence_count=len(confidences),
        confidence_mean=(sum(confidences) / len(confidences) if confidences else None),
        confidence_min=min(confidences, default=None),
        confidence_max=max(confidences, default=None),
    )


def _make_audit_finding(item: LF022VerifiedCodexAuditJudgment) -> LF022CodexAuditFinding:
    response = item.response
    values: dict[str, object] = {
        "schema_version": 1,
        "audit_item_id": item.audit_item_id,
        "lean_check_id": item.lean_check_id,
        "pair_id": item.pair_id,
        "variant_id": item.variant_id,
        "source_record_ids": item.source_record_ids,
        "proposer_family_id": item.proposer_family_id,
        "same_claim_answer": response.same_claim_answer,
        "relation": response.relation.value if response.relation is not None else None,
        "a_implies_b": response.a_implies_b,
        "b_implies_a": response.b_implies_a,
        "error_types": tuple(sorted(response.error_types)),
        "confidence": response.confidence,
        "needs_expert_review": response.needs_expert_review,
        "final_message_sha256": item.final_message_sha256,
        "parsed_response_sha256": item.parsed_response_sha256,
        "audit_only": True,
        "human_label": False,
        "semantic_label": False,
        "silver_record": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022CodexAuditFinding.model_validate(
        {
            **values,
            "finding_id": make_id("lf022_codex_audit_finding", values),
        }
    )


def _render_summary_markdown(summary: LF022CodexAuditSummary) -> bytes:
    def counts(values: dict[str, int]) -> str:
        return ", ".join(f"`{key}` {value}" for key, value in values.items()) or "none"

    lines = [
        "# LF-022 Codex audit summary",
        "",
        "This is a hash-verified diagnostic summary. It creates no human or semantic",
        "labels and contributes no training, evaluation, silver-promotion, or gate credit.",
        "",
        "## Bound artifacts",
        "",
        f"- Summary ID: `{summary.summary_id}`",
        f"- Audit manifest SHA-256: `{summary.audit_manifest_sha256}`",
        f"- Lean checks SHA-256: `{summary.checks_sha256}`",
        f"- Verified response set SHA-256: `{summary.response_artifact_set_sha256}`",
        f"- Compact findings SHA-256: `{summary.findings_sha256}`",
        f"- Judge: `{summary.model}` with reasoning `{summary.reasoning_effort}`",
        f"- Explicit parent-audit bindings: {len(summary.parent_audit_bindings)}",
        "",
        "## Mechanical filtering",
        "",
        f"- Total generated candidates checked by Lean: {summary.total_check_count}",
        f"- Lean-valid candidates audited: {summary.lean_valid_check_count}",
        f"- Lean-invalid candidates: {summary.lean_invalid_check_count}",
        f"- Completed Codex judgments: {summary.completed_judgment_count}",
        f"- Lean outcomes: {counts(summary.lean_check_outcome_counts)}",
        "",
        "## Audit verdicts",
        "",
        f"- Same-claim answers: {counts(summary.overall.same_claim_counts)}",
        f"- Relations: {counts(summary.overall.relation_counts)}",
        f"- Directional implications: {counts(summary.overall.implication_counts)}",
        f"- Error types: {counts(summary.overall.error_type_counts)}",
        f"- Needs expert review: {summary.overall.needs_expert_review_count}",
        f"- Mean confidence: {summary.overall.confidence_mean:.6f}",
        "",
        "## By proposer family",
        "",
        "| Proposer | Audited | Same claim | Not same | Ambiguous | Uncertain | Mean confidence |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for family, bucket in summary.by_proposer_family.items():
        lines.append(
            "| "
            f"`{family}` | {bucket.total_count} | "
            f"{bucket.same_claim_counts.get('same_claim', 0)} | "
            f"{bucket.same_claim_counts.get('not_same_claim', 0)} | "
            f"{bucket.same_claim_counts.get('ambiguous', 0)} | "
            f"{bucket.same_claim_counts.get('uncertain', 0)} | "
            f"{cast(float, bucket.confidence_mean):.6f} |"
        )
    lines.extend(
        [
            "",
            "## Scientific status",
            "",
            "These judgments are useful evidence about the quality of the generated pairs,",
            "but they come from one judge family and one AB presentation. They therefore do",
            "not satisfy LeanFaith's two-family, swapped-order weak-consensus contract and are",
            "not human gold.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def verify_completed_lf022_codex_audit(
    *,
    repo_root: Path,
    checks_path: Path,
    audit_root: Path,
    require_complete_clean: bool = True,
    parent_audit_roots: Sequence[Path] = (),
) -> LF022VerifiedCodexAudit:
    """Replay and verify every completed Codex audit artifact without writing output.

    A copied completed item is accepted only when its original absolute request
    paths point into an explicitly declared parent audit and its complete item
    tree is byte-identical to that already independently verified parent item.
    Merely having stale paths, matching response hashes, or a completed marker
    is insufficient.
    """

    repo_root = repo_root.resolve()
    checks_path = checks_path.resolve()
    audit_root = audit_root.resolve()
    manifest_path = audit_root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise LF022CodexAuditError(f"audit manifest is missing: {manifest_path}")
    try:
        manifest = LF022CodexAuditManifest.model_validate_json(manifest_path.read_bytes())
    except ValueError as exc:
        raise LF022CodexAuditError(f"invalid audit manifest: {exc}") from exc
    if _resolve_artifact(manifest.checks_artifact, repo_root=repo_root) != checks_path:
        raise LF022CodexAuditError("audit manifest references a different checks artifact")
    if hash_file(checks_path) != manifest.checks_sha256:
        raise LF022CodexAuditError("checks artifact hash differs from audit manifest")

    parent_roots = tuple(sorted({path.resolve() for path in parent_audit_roots}))
    if len(parent_roots) != len(parent_audit_roots):
        raise LF022CodexAuditError("parent audit roots must be unique")
    parent_audits: dict[Path, LF022VerifiedCodexAudit] = {}
    parent_items: dict[Path, dict[str, LF022CodexAuditInput]] = {}
    for parent_root in parent_roots:
        if (
            parent_root == audit_root
            or parent_root.is_relative_to(audit_root)
            or audit_root.is_relative_to(parent_root)
        ):
            raise LF022CodexAuditError("parent and child audit roots must be disjoint")
        parent_manifest_path = parent_root / "manifest.json"
        if parent_manifest_path.is_symlink() or not parent_manifest_path.is_file():
            raise LF022CodexAuditError(
                f"declared parent audit manifest is missing: {parent_manifest_path}"
            )
        try:
            parent_manifest = LF022CodexAuditManifest.model_validate_json(
                parent_manifest_path.read_bytes()
            )
        except ValueError as exc:
            raise LF022CodexAuditError(
                f"invalid declared parent audit manifest: {parent_manifest_path}: {exc}"
            ) from exc
        parent_checks = _resolve_artifact(parent_manifest.checks_artifact, repo_root=repo_root)
        parent_verified = verify_completed_lf022_codex_audit(
            repo_root=repo_root,
            checks_path=parent_checks,
            audit_root=parent_root,
            require_complete_clean=True,
        )
        if (
            parent_verified.manifest.model != manifest.model
            or parent_verified.manifest.reasoning_effort != manifest.reasoning_effort
        ):
            raise LF022CodexAuditError("parent and child audit judge configurations differ")
        parent_audits[parent_root] = parent_verified
        parent_items[parent_root] = {item.audit_item_id: item for item in parent_verified.items}

    checks = _load_check_inventory(checks_path)
    outcome_counts: dict[str, int] = {
        str(outcome): count
        for outcome, count in sorted(Counter(check.outcome for check in checks).items())
    }
    unsupported = sorted(set(outcome_counts) - {*_VALID_OUTCOMES, "invalid"})
    if unsupported:
        raise LF022CodexAuditError(
            "completed audit verification requires all non-valid Lean checks to be invalid; found "
            + ", ".join(unsupported)
        )
    checks_by_id = {check.check_id: check for check in checks}
    items = load_lean_valid_audit_inputs(checks_path=checks_path, repo_root=repo_root)
    if manifest.eligible_count != len(items):
        raise LF022CodexAuditError("audit manifest eligible count differs from checks")
    if manifest.ordered_audit_item_ids_sha256 != hash_canonical(
        [item.audit_item_id for item in items]
    ):
        raise LF022CodexAuditError("audit manifest ordered input hash differs from checks")
    if require_complete_clean and (
        manifest.completed_count != len(items)
        or manifest.exhausted_count != 0
        or manifest.attempt_status_counts != {"completed": len(items)}
    ):
        raise LF022CodexAuditError("audit is not complete and clean")

    expected_completed_paths: dict[Path, LF022CodexAuditInput] = {}
    for item in items:
        completed_path = _item_dir(audit_root, item.audit_item_id) / "completed.json"
        if completed_path.is_symlink():
            raise LF022CodexAuditError(f"completed audit cannot be a symlink: {completed_path}")
        if completed_path.is_file():
            expected_completed_paths[completed_path.resolve()] = item
    observed_completed_paths: dict[Path, Path] = {}
    for path in (audit_root / "items").glob("*/*/completed.json"):
        if path.is_symlink() or not path.is_file():
            raise LF022CodexAuditError(f"completed audit is not a regular file: {path}")
        observed_completed_paths[path.resolve()] = path
    if set(observed_completed_paths) != set(expected_completed_paths):
        raise LF022CodexAuditError(
            "completed audit artifacts are missing, extra, or stored at noncanonical paths"
        )
    if len(expected_completed_paths) != manifest.completed_count:
        raise LF022CodexAuditError("audit manifest completed count differs from artifacts")

    verified: list[LF022VerifiedCodexAuditJudgment] = []
    response_bindings: list[dict[str, object]] = []
    reused_ids_by_parent: dict[Path, list[str]] = {root: [] for root in parent_roots}
    copied_tree_bindings_by_parent: dict[Path, list[dict[str, str]]] = {
        root: [] for root in parent_roots
    }
    for completed_path in sorted(expected_completed_paths):
        item = expected_completed_paths[completed_path]
        check = checks_by_id.get(item.lean_check_id)
        if check is None:
            raise LF022CodexAuditError(f"audit item lacks Lean check {item.lean_check_id}")
        item_dir = _item_dir(audit_root, item.audit_item_id)
        input_path = item_dir / "input.json"
        if input_path.read_bytes() != _canonical_line(item):
            raise LF022CodexAuditError(f"audit input replay mismatch: {input_path}")
        terminal = _load_completed(completed_path, item)
        attempt_dir = item_dir / "attempts" / f"{terminal.attempt_index:04d}"
        terminal_path = attempt_dir / "terminal.json"
        if terminal_path.read_bytes() != _canonical_line(terminal):
            raise LF022CodexAuditError(f"terminal/completed mismatch: {attempt_dir}")
        try:
            request = LF022CodexAuditAttempt.model_validate_json(
                (attempt_dir / "request.json").read_bytes()
            )
        except (OSError, ValueError) as exc:
            raise LF022CodexAuditError(f"invalid audit request: {attempt_dir}: {exc}") from exc
        if (
            request.audit_item_id != item.audit_item_id
            or request.attempt_index != terminal.attempt_index
            or request.model != manifest.model
            or request.reasoning_effort != manifest.reasoning_effort
        ):
            raise LF022CodexAuditError(f"request does not bind manifest/input: {attempt_dir}")
        prompt = render_blinded_judge_prompt(item.presentation).text.encode("utf-8")
        if (item_dir / "prompt.txt").read_bytes() != prompt or request.prompt_sha256 != sha256_hex(
            prompt
        ):
            raise LF022CodexAuditError(f"prompt replay mismatch: {item_dir}")
        schema_path = _request_path_argument(request.argv, "--output-schema")
        requested_final_path = _request_path_argument(request.argv, "-o")
        expected_current_final = (attempt_dir / "final_message.json").resolve()
        expected_current_schema = (
            audit_root / "schemas" / f"judge_response.{request.output_schema_sha256}.schema.json"
        ).resolve()
        if requested_final_path == expected_current_final:
            if schema_path != expected_current_schema:
                raise LF022CodexAuditError(f"request response-schema path mismatch: {attempt_dir}")
        else:
            matching_parents: list[Path] = []
            for parent_root in parent_roots:
                if item.audit_item_id not in parent_items[parent_root]:
                    continue
                parent_item_dir = _item_dir(parent_root, item.audit_item_id)
                parent_attempt_dir = parent_item_dir / "attempts" / f"{terminal.attempt_index:04d}"
                expected_parent_final = (parent_attempt_dir / "final_message.json").resolve()
                expected_parent_schema = (
                    parent_root
                    / "schemas"
                    / f"judge_response.{request.output_schema_sha256}.schema.json"
                ).resolve()
                if (
                    requested_final_path == expected_parent_final
                    and schema_path == expected_parent_schema
                ):
                    matching_parents.append(parent_root)
            if len(matching_parents) != 1:
                raise LF022CodexAuditError(
                    f"request paths do not bind exactly one declared parent audit: {attempt_dir}"
                )
            parent_root = matching_parents[0]
            parent_item_dir = _item_dir(parent_root, item.audit_item_id)
            copied_tree_sha256 = _assert_byte_identical_item_tree(
                copied=item_dir,
                parent=parent_item_dir,
            )
            reused_ids_by_parent[parent_root].append(item.audit_item_id)
            copied_tree_bindings_by_parent[parent_root].append(
                {
                    "audit_item_id": item.audit_item_id,
                    "copied_item_tree_sha256": copied_tree_sha256,
                }
            )
        if hash_file(schema_path) != request.output_schema_sha256:
            raise LF022CodexAuditError(f"response schema hash mismatch: {attempt_dir}")
        stdout_path = attempt_dir / "stdout.jsonl"
        stderr_path = attempt_dir / "stderr.txt"
        final_path = attempt_dir / "final_message.json"
        parsed_path = attempt_dir / "parsed_response.json"
        if hash_file(stdout_path) != terminal.stdout_sha256:
            raise LF022CodexAuditError(f"stdout hash mismatch: {attempt_dir}")
        if hash_file(stderr_path) != terminal.stderr_sha256:
            raise LF022CodexAuditError(f"stderr hash mismatch: {attempt_dir}")
        if hash_file(final_path) != terminal.final_message_sha256:
            raise LF022CodexAuditError(f"final response hash mismatch: {attempt_dir}")
        if hash_file(parsed_path) != terminal.parsed_response_sha256:
            raise LF022CodexAuditError(f"parsed response hash mismatch: {attempt_dir}")
        try:
            response = parse_blinded_judge_output(final_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise LF022CodexAuditError(
                f"final response replay failed: {attempt_dir}: {exc}"
            ) from exc
        if parsed_path.read_bytes() != _canonical_line(response):
            raise LF022CodexAuditError(f"parsed response replay mismatch: {attempt_dir}")
        proposer_family_id = _proposer_family_for_check(check, item, repo_root=repo_root)
        verified_item = LF022VerifiedCodexAuditJudgment(
            audit_item_id=item.audit_item_id,
            lean_check_id=item.lean_check_id,
            pair_id=item.pair.pair_id,
            variant_id=item.variant_id,
            source_record_ids=item.pair.source_record_ids,
            proposer_family_id=proposer_family_id,
            response=response,
            final_message_sha256=terminal.final_message_sha256,
            parsed_response_sha256=terminal.parsed_response_sha256,
        )
        verified.append(verified_item)
        response_bindings.append(
            {
                "audit_item_id": verified_item.audit_item_id,
                "proposer_family_id": proposer_family_id,
                "final_message_sha256": verified_item.final_message_sha256,
                "parsed_response_sha256": verified_item.parsed_response_sha256,
            }
        )
    parent_bindings: list[LF022CodexAuditParentBinding] = []
    for parent_root in parent_roots:
        reused_ids = reused_ids_by_parent[parent_root]
        if not reused_ids:
            raise LF022CodexAuditError(f"declared parent audit was not used: {parent_root}")
        parent_verified = parent_audits[parent_root]
        parent_bindings.append(
            LF022CodexAuditParentBinding(
                audit_root=str(parent_root),
                manifest_sha256=hash_file(parent_verified.manifest_path),
                checks_sha256=parent_verified.manifest.checks_sha256,
                response_artifact_set_sha256=(parent_verified.response_artifact_set_sha256),
                reused_item_count=len(reused_ids),
                ordered_reused_audit_item_ids_sha256=hash_canonical(reused_ids),
                copied_item_tree_set_sha256=hash_canonical(
                    copied_tree_bindings_by_parent[parent_root]
                ),
            )
        )
    parent_bound_count = sum(item.reused_item_count for item in parent_bindings)
    if parent_bound_count > manifest.reused_count:
        raise LF022CodexAuditError("parent-bound copied item count exceeds manifest reused count")
    return LF022VerifiedCodexAudit(
        manifest=manifest,
        manifest_path=manifest_path,
        checks=checks,
        lean_check_outcome_counts=outcome_counts,
        items=items,
        judgments=tuple(verified),
        response_artifact_set_sha256=hash_canonical(response_bindings),
        parent_audit_bindings=tuple(parent_bindings),
    )


def summarize_completed_lf022_codex_audit(
    *,
    repo_root: Path,
    checks_path: Path,
    audit_root: Path,
    output_json_path: Path,
    output_markdown_path: Path,
    output_findings_path: Path,
    parent_audit_roots: Sequence[Path] = (),
) -> LF022CodexAuditSummaryResult:
    """Verify every completed audit artifact and write a diagnostic-only summary."""

    repo_root = repo_root.resolve()
    checks_path = checks_path.resolve()
    audit_root = audit_root.resolve()
    output_json_path = output_json_path.resolve()
    output_markdown_path = output_markdown_path.resolve()
    output_findings_path = output_findings_path.resolve()
    output_paths = {output_json_path, output_markdown_path, output_findings_path}
    if len(output_paths) != 3:
        raise LF022CodexAuditError("summary JSON, Markdown, and findings paths must differ")
    if any(path.is_relative_to(audit_root) for path in output_paths):
        raise LF022CodexAuditError("summary outputs must remain outside the immutable audit root")

    verified_audit = verify_completed_lf022_codex_audit(
        repo_root=repo_root,
        checks_path=checks_path,
        audit_root=audit_root,
        parent_audit_roots=parent_audit_roots,
    )
    manifest = verified_audit.manifest
    manifest_path = verified_audit.manifest_path
    checks = verified_audit.checks
    outcome_counts = verified_audit.lean_check_outcome_counts
    items = verified_audit.items
    verified = verified_audit.judgments

    grouped: dict[str, list[LF022VerifiedCodexAuditJudgment]] = {}
    for judgment in verified:
        grouped.setdefault(judgment.proposer_family_id, []).append(judgment)
    overall = _make_summary_bucket(verified)
    by_proposer_family = {
        family: _make_summary_bucket(grouped[family]) for family in sorted(grouped)
    }
    findings = tuple(_make_audit_finding(item) for item in verified)
    findings_bytes = b"".join(_canonical_line(finding) for finding in findings)
    findings_sha256 = sha256_hex(findings_bytes)
    values: dict[str, object] = {
        "schema_version": 1,
        "method_version": "lf022_codex_audit_summary_v1",
        "audit_manifest": str(manifest_path),
        "audit_manifest_sha256": hash_file(manifest_path),
        "audit_method_version": manifest.method_version,
        "checks_artifact": str(checks_path),
        "checks_sha256": hash_file(checks_path),
        "response_artifact_set_sha256": verified_audit.response_artifact_set_sha256,
        "findings_artifact": str(output_findings_path),
        "findings_sha256": findings_sha256,
        "model": manifest.model,
        "reasoning_effort": manifest.reasoning_effort,
        "total_check_count": len(checks),
        "lean_check_outcome_counts": outcome_counts,
        "lean_valid_check_count": len(items),
        "lean_invalid_check_count": outcome_counts.get("invalid", 0),
        "completed_judgment_count": len(verified),
        "overall": overall.model_dump(mode="json"),
        "by_proposer_family": {
            family: bucket.model_dump(mode="json") for family, bucket in by_proposer_family.items()
        },
        "audit_only": True,
        "human_labels_created": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    if verified_audit.parent_audit_bindings:
        values["parent_audit_bindings"] = [
            item.model_dump(mode="json") for item in verified_audit.parent_audit_bindings
        ]
    identity_values = {
        key: value
        for key, value in values.items()
        if key not in {"audit_manifest", "checks_artifact", "findings_artifact"}
    }
    if verified_audit.parent_audit_bindings:
        identity_values["parent_audit_bindings"] = _parent_binding_id_payloads(
            verified_audit.parent_audit_bindings
        )
    summary = LF022CodexAuditSummary.model_validate(
        {
            **values,
            "summary_id": make_id("lf022_codex_audit_summary", identity_values),
        }
    )
    _write_atomic(output_findings_path, findings_bytes)
    _write_atomic(output_json_path, _canonical_line(summary))
    _write_atomic(output_markdown_path, _render_summary_markdown(summary))
    return LF022CodexAuditSummaryResult(
        summary=summary,
        json_path=output_json_path,
        markdown_path=output_markdown_path,
        findings_path=output_findings_path,
    )


__all__ = [
    "DEFAULT_CODEX_AUDIT_MODEL",
    "DEFAULT_CODEX_REASONING_EFFORT",
    "LF022CodexAuditError",
    "LF022CodexAuditFinding",
    "LF022CodexAuditInput",
    "LF022CodexAuditManifest",
    "LF022CodexAuditParentBinding",
    "LF022CodexAuditPrivacyError",
    "LF022CodexAuditRunResult",
    "LF022CodexAuditSummary",
    "LF022CodexAuditSummaryBucket",
    "LF022CodexAuditSummaryResult",
    "LF022CodexAuditTerminal",
    "LF022VerifiedCodexAudit",
    "LF022VerifiedCodexAuditJudgment",
    "ProcessCapture",
    "SubprocessCodexAuditExecutor",
    "audit_lean_valid_lf022_pairs",
    "load_lean_valid_audit_inputs",
    "summarize_completed_lf022_codex_audit",
    "verify_completed_lf022_codex_audit",
]
