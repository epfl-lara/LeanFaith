"""Pooled, resumable Lean validation for provisional LF-022 variants.

This stage checks only whether an LLM-proposed theorem statement elaborates in
its registered Lean context.  It never changes the source ``VariantRecord``
and never creates semantic labels, silver data, or training eligibility.

The execution unit is an independent LeanInteract request.  Requests are
grouped by exact project, context, and import header, then submitted in bounded
chunks through ``LeanServerPool``.  INVALID results are confirmed by the
backend's fresh-process oracle; only infrastructure outcomes are retried.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_batch import LF022PublicBatchManifest
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.project_registry import read_git_revision
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import ServerMode
from leanfaith.schemas.enums import ValidationStatus
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.variant import VariantRecord

LF022_LEAN_CHECK_METHOD_VERSION = "lf022_provisional_lean_check_v1"
_DECLARATION_NAME = re.compile(r"^(?:theorem|lemma)\s+([^\s:({\[]+)", re.UNICODE)
_RETRYABLE_INFRASTRUCTURE = frozenset(
    {LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT}
)


class LF022LeanCheckError(RuntimeError):
    """An LF-022 input, execution, or persisted-resume invariant failed."""


class LF022LeanCheckAttempt(StrictModel):
    """One LeanInteract attempt; semantic meaning is deliberately absent."""

    attempt_index: int = Field(ge=0, strict=True)
    request_hash: str = Field(pattern=HEX64_PATTERN)
    lean_status: LeanStatus
    elapsed_ms: int = Field(ge=0, strict=True)
    messages: tuple[dict[str, object], ...] = ()
    sorries: tuple[dict[str, object], ...] = ()
    declarations: tuple[dict[str, object], ...] = ()
    raw_response_path: str | None = None
    raw_response_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    infrastructure_error: str | None = None

    @model_validator(mode="after")
    def _raw_binding_is_complete(self) -> Self:
        if (self.raw_response_path is None) != (self.raw_response_sha256 is None):
            raise ValueError("raw response path/hash must be present together")
        return self


LeanCheckOutcome = Literal[
    "elaborates",
    "elaborates_with_placeholder",
    "invalid",
    "timeout",
    "infrastructure_error",
    "unsupported",
    "declaration_mismatch",
]


class LF022LeanCheckRecord(StrictModel):
    """Immutable mechanical check attached to, but not replacing, a variant."""

    schema_version: Literal[1] = 1
    check_id: str = Field(pattern=id_pattern("lf022_lean_check"))
    method_version: Literal["lf022_provisional_lean_check_v1"] = "lf022_provisional_lean_check_v1"
    variant_id: str = Field(pattern=id_pattern("var"))
    source_variant_artifact: str
    source_variant_artifact_sha256: str = Field(pattern=HEX64_PATTERN)
    source_variant_line_number: int = Field(ge=1, strict=True)
    source_variant_line_sha256: str = Field(pattern=HEX64_PATTERN)
    candidate_code_hash: str = Field(pattern=HEX64_PATTERN)
    context_id: str = Field(pattern=id_pattern("ctx"))
    source_id: str
    source_revision: str
    project_dir: str
    project_revision: str
    import_header: str
    import_header_sha256: str = Field(pattern=HEX64_PATTERN)
    request_id: str
    request_code_sha256: str = Field(pattern=HEX64_PATTERN)
    lean_status: LeanStatus
    validation_status: ValidationStatus
    outcome: LeanCheckOutcome
    declaration_verified: bool
    fresh_invalid_confirmation_enabled: Literal[True] = True
    attempts: tuple[LF022LeanCheckAttempt, ...] = Field(min_length=1)
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        expected = _check_record_id(self)
        if self.check_id != expected:
            raise ValueError("check_id does not match canonical record")
        if self.lean_status != self.attempts[-1].lean_status:
            raise ValueError("terminal lean_status differs from final attempt")
        if self.outcome.startswith("elaborates") and not self.declaration_verified:
            raise ValueError("an elaborating outcome requires declaration verification")
        return self


def _check_record_id(record: LF022LeanCheckRecord) -> str:
    """Stable record ID excluding machine-local artifact path strings."""

    identity = {
        "schema_version": record.schema_version,
        "method_version": record.method_version,
        "variant_id": record.variant_id,
        "source_variant_artifact_sha256": record.source_variant_artifact_sha256,
        "source_variant_line_number": record.source_variant_line_number,
        "source_variant_line_sha256": record.source_variant_line_sha256,
        "candidate_code_hash": record.candidate_code_hash,
        "context_id": record.context_id,
        "source_id": record.source_id,
        "source_revision": record.source_revision,
        "project_revision": record.project_revision,
        "import_header_sha256": record.import_header_sha256,
        "request_id": record.request_id,
        "request_code_sha256": record.request_code_sha256,
        "lean_status": record.lean_status.value,
        "validation_status": record.validation_status.value,
        "outcome": record.outcome,
        "declaration_verified": record.declaration_verified,
        "fresh_invalid_confirmation_enabled": record.fresh_invalid_confirmation_enabled,
        "attempts": [
            attempt.model_dump(mode="json", exclude={"raw_response_path"})
            for attempt in record.attempts
        ],
        "semantic_labels_created": record.semantic_labels_created,
        "silver_records_created": record.silver_records_created,
        "training_eligible": record.training_eligible,
        "evaluation_eligible": record.evaluation_eligible,
    }
    return make_id("lf022_lean_check", identity)


class LF022LeanCheckManifest(StrictModel):
    """Deterministic summary of one current input snapshot."""

    schema_version: Literal[2, 3] = 2
    method_version: Literal["lf022_provisional_lean_check_v2"] = "lf022_provisional_lean_check_v2"
    input_root: str
    selection_batch_id: str | None = None
    selection_batch_manifest: str | None = None
    selection_batch_manifest_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    selection_postgen_selector_id: str | None = Field(
        default=None,
        pattern=id_pattern("lf022_postgen_terminal_selector"),
        exclude_if=lambda value: value is None,
    )
    selection_postgen_selector: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    selection_postgen_selector_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
        exclude_if=lambda value: value is None,
    )
    selected_execution_task_count: int | None = Field(default=None, ge=1, strict=True)
    input_set_hash: str = Field(pattern=HEX64_PATTERN)
    record_count: int = Field(ge=0, strict=True)
    ordered_variant_ids_hash: str = Field(pattern=HEX64_PATTERN)
    checks_artifact: str
    checks_sha256: str = Field(pattern=HEX64_PATTERN)
    status_counts: dict[str, int]
    outcome_counts: dict[str, int]
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _selection_binding_is_complete(self) -> Self:
        batch_values = (
            self.selection_batch_manifest,
            self.selection_batch_manifest_sha256,
        )
        postgen_values = (
            self.selection_postgen_selector_id,
            self.selection_postgen_selector,
            self.selection_postgen_selector_sha256,
        )
        if any(value is not None for value in batch_values) and not all(
            value is not None for value in batch_values
        ):
            raise ValueError("batch selection binding must be present or absent as one unit")
        if any(value is not None for value in postgen_values) and not all(
            value is not None for value in postgen_values
        ):
            raise ValueError("postgen selector binding must be present or absent as one unit")
        has_batch = all(value is not None for value in batch_values)
        has_postgen = all(value is not None for value in postgen_values)
        if has_batch and has_postgen:
            raise ValueError("batch manifest and postgen selector are mutually exclusive")
        if has_batch or has_postgen:
            if self.selection_batch_id is None or self.selected_execution_task_count is None:
                raise ValueError("selection lacks batch identity or selected task count")
        elif self.selection_batch_id is not None or self.selected_execution_task_count is not None:
            raise ValueError("partial selection identity is forbidden")
        if self.schema_version == 2 and has_postgen:
            raise ValueError("postgen selector requires Lean-check manifest schema v3")
        return self


@dataclass(frozen=True, slots=True)
class LF022LeanCheckRunResult:
    records: tuple[LF022LeanCheckRecord, ...]
    manifest: LF022LeanCheckManifest
    manifest_path: Path
    reused_count: int
    executed_count: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    position: int
    task_path: Path
    terminal_path: Path
    variant_path: Path
    variant_line_number: int
    variant_line: bytes
    variant: VariantRecord
    source_id: str
    source_revision: str
    context_id: str
    imports: tuple[str, ...]
    project_dir: Path

    @property
    def import_header(self) -> str:
        return "\n".join(f"import {module}" for module in self.imports) + "\n"

    @property
    def group_key(self) -> tuple[str, str, str]:
        return (str(self.project_dir.resolve()), self.context_id, self.import_header)


class _Backend(Protocol):
    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]: ...

    def reset_session(self) -> None: ...

    def close(self) -> None: ...


BackendFactory = Callable[[BackendSettings], _Backend]
PrepareEnvironment = Callable[[BackendSettings], None]


def _load_json_mapping(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LF022LeanCheckError(f"invalid {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LF022LeanCheckError(f"{label} {path} must be one JSON object")
    return cast(dict[str, object], value)


def _required_text(mapping: Mapping[str, object], key: str, *, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LF022LeanCheckError(f"{label} requires nonempty string {key!r}")
    return value


def _artifact_label(path: Path, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _safe_existing_directory(path: Path, *, label: str) -> Path:
    candidate = path.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LF022LeanCheckError(f"{label} traverses a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LF022LeanCheckError(f"{label} is missing") from exc
    if resolved != candidate or not resolved.is_dir():
        raise LF022LeanCheckError(f"{label} is missing or unsafe")
    return resolved


def _safe_existing_file(path: Path, *, label: str) -> Path:
    candidate = path.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LF022LeanCheckError(f"{label} traverses a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LF022LeanCheckError(f"{label} is missing") from exc
    if resolved != candidate or not resolved.is_file():
        raise LF022LeanCheckError(f"{label} is missing or unsafe")
    return resolved


def _safe_output_directory(path: Path, *, label: str) -> Path:
    """Create or reuse an output directory without following any symlink."""

    candidate = path.absolute()
    if ".." in candidate.parts:
        raise LF022LeanCheckError(f"{label} must be a normalized absolute path")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LF022LeanCheckError(f"{label} traverses a symlink")
        if current.exists() and not current.is_dir():
            raise LF022LeanCheckError(f"{label} component is not a directory")
        current.mkdir(exist_ok=True)
        if current.is_symlink() or not current.is_dir():
            raise LF022LeanCheckError(f"{label} became unsafe during creation")
    if candidate.resolve(strict=True) != candidate:
        raise LF022LeanCheckError(f"{label} is not canonical")
    return candidate


def _discover_candidates(
    *,
    repo_root: Path,
    input_root: Path,
    project_dirs: Mapping[str, Path],
    limit: int | None,
    selected_execution_task_ids: tuple[str, ...] | None,
    selected_task_content_hashes: Mapping[str, str] | None,
    selected_terminal_artifacts: Mapping[str, tuple[Path, str]] | None,
) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    seen_variant_ids: set[str] = set()
    if selected_execution_task_ids is None:
        terminal_paths = tuple(sorted(input_root.glob("tasks/*/*/terminal.json")))
    else:
        terminal_paths_list: list[Path] = []
        for execution_task_id in selected_execution_task_ids:
            if selected_terminal_artifacts is None:
                digest = execution_task_id.removeprefix("lf022_execution_task:")
                terminal_path = input_root / "tasks" / digest[:2] / digest / "terminal.json"
                expected_terminal_hash = None
            else:
                terminal_path, expected_terminal_hash = selected_terminal_artifacts[
                    execution_task_id
                ]
            if terminal_path.is_symlink() or not terminal_path.is_file():
                raise LF022LeanCheckError(
                    f"selected execution task lacks terminal artifact: {execution_task_id}"
                )
            if (
                expected_terminal_hash is not None
                and hash_file(terminal_path) != expected_terminal_hash
            ):
                raise LF022LeanCheckError(
                    f"selected execution terminal hash differs: {execution_task_id}"
                )
            terminal_paths_list.append(terminal_path)
        terminal_paths = tuple(terminal_paths_list)
    for terminal_path in terminal_paths:
        terminal = _load_json_mapping(terminal_path, label="terminal")
        path_execution_task_id = "lf022_execution_task:" + terminal_path.parent.name
        task_path = terminal_path.with_name("task.json")
        task = _load_json_mapping(task_path, label="task")
        task_execution_task_id = _required_text(task, "execution_task_id", label=str(task_path))
        if task_execution_task_id != path_execution_task_id:
            raise LF022LeanCheckError(
                f"task/path execution ID mismatch for {task_execution_task_id}"
            )
        if selected_task_content_hashes is not None:
            expected_content_hash = selected_task_content_hashes[path_execution_task_id]
            if hash_canonical(task) != expected_content_hash:
                raise LF022LeanCheckError(
                    f"executor task differs from selected batch task: {path_execution_task_id}"
                )
        if terminal.get("status") != "provisional_variants_created":
            continue
        source = task.get("source")
        if not isinstance(source, dict):
            raise LF022LeanCheckError(f"task {task_path} lacks source object")
        source_mapping = cast(dict[str, object], source)
        source_id = _required_text(source_mapping, "source_id", label=str(task_path))
        source_revision = _required_text(source_mapping, "source_revision", label=str(task_path))
        context_id = _required_text(source_mapping, "context_id", label=str(task_path))
        imports_value = source_mapping.get("imports")
        if (
            not isinstance(imports_value, list)
            or not imports_value
            or not all(isinstance(item, str) and item.strip() for item in imports_value)
        ):
            raise LF022LeanCheckError(f"task {task_path} requires nonempty string imports")
        imports = tuple(cast(list[str], imports_value))
        if source_id not in project_dirs:
            raise LF022LeanCheckError(
                f"task {task_path} source_id={source_id!r} has no --project mapping"
            )
        project_dir = project_dirs[source_id].resolve()
        if not project_dir.is_dir():
            raise LF022LeanCheckError(f"project directory is missing: {project_dir}")

        variants_path = terminal_path.with_name("provisional_variants.jsonl")
        expected_artifact = terminal.get("variants_artifact")
        expected_hash = terminal.get("variants_sha256")
        expected_count = terminal.get("provisional_variant_count")
        if not isinstance(expected_artifact, str) or not isinstance(expected_hash, str):
            raise LF022LeanCheckError(f"terminal {terminal_path} lacks variant binding")
        if not isinstance(expected_count, int) or isinstance(expected_count, bool):
            raise LF022LeanCheckError(f"terminal {terminal_path} lacks variant count")
        if hash_file(variants_path) != expected_hash:
            raise LF022LeanCheckError(f"variant artifact hash mismatch: {variants_path}")
        lines = variants_path.read_bytes().splitlines(keepends=True)
        if len(lines) != expected_count:
            raise LF022LeanCheckError(f"variant artifact count mismatch: {variants_path}")
        expected_relative = PurePosixPath(expected_artifact)
        if (
            expected_relative.is_absolute()
            or ".." in expected_relative.parts
            or "." in expected_relative.parts
            or tuple(variants_path.resolve().parts[-len(expected_relative.parts) :])
            != expected_relative.parts
        ):
            raise LF022LeanCheckError(
                f"terminal variant path differs from colocated artifact: {terminal_path}"
            )

        for line_number, line in enumerate(lines, start=1):
            if not line.endswith(b"\n"):
                raise LF022LeanCheckError(f"{variants_path}:{line_number} lacks final newline")
            try:
                variant = VariantRecord.model_validate_json(line)
            except ValueError as exc:
                raise LF022LeanCheckError(
                    f"invalid variant {variants_path}:{line_number}: {exc}"
                ) from exc
            if line != canonical_json_bytes(variant.model_dump(mode="json")) + b"\n":
                raise LF022LeanCheckError(
                    f"variant is not canonical JSONL: {variants_path}:{line_number}"
                )
            if variant.variant_id in seen_variant_ids:
                raise LF022LeanCheckError(f"duplicate variant_id {variant.variant_id}")
            if variant.context_id != context_id:
                raise LF022LeanCheckError(f"variant/task context mismatch for {variant.variant_id}")
            if variant.extracted_statement is None or variant.candidate_code_hash is None:
                raise LF022LeanCheckError(
                    f"variant {variant.variant_id} lacks a candidate statement/hash"
                )
            seen_variant_ids.add(variant.variant_id)
            candidates.append(
                _Candidate(
                    position=len(candidates),
                    task_path=task_path,
                    terminal_path=terminal_path,
                    variant_path=variants_path,
                    variant_line_number=line_number,
                    variant_line=line,
                    variant=variant,
                    source_id=source_id,
                    source_revision=source_revision,
                    context_id=context_id,
                    imports=imports,
                    project_dir=project_dir,
                )
            )
            if limit is not None and len(candidates) >= limit:
                return tuple(candidates)
    return tuple(candidates)


def _load_batch_selection(
    *, repo_root: Path, batch_manifest_path: Path
) -> tuple[LF022PublicBatchManifest, tuple[str, ...], dict[str, str], str]:
    """Load one canonical content-addressed batch as a check input selector."""

    raw = batch_manifest_path.read_bytes()
    try:
        manifest = LF022PublicBatchManifest.model_validate_json(raw)
    except ValueError as exc:
        raise LF022LeanCheckError(
            f"invalid LF-022 batch selector {batch_manifest_path}: {exc}"
        ) from exc
    if raw != canonical_json_bytes(manifest.model_dump(mode="json")):
        raise LF022LeanCheckError(
            f"LF-022 batch selector is not canonical JSON: {batch_manifest_path}"
        )
    task_ids: list[str] = []
    task_content_hashes: dict[str, str] = {}
    for route in manifest.routes:
        for task in route.tasks:
            task_ids.append(task.execution_task_id)
            frozen_task_path = repo_root / task.task.path
            if not frozen_task_path.is_file() or hash_file(frozen_task_path) != task.task.sha256:
                raise LF022LeanCheckError(
                    f"selected batch task binding failed: {task.execution_task_id}"
                )
            frozen_task = _load_json_mapping(frozen_task_path, label="selected batch task")
            frozen_execution_task_id = _required_text(
                frozen_task, "execution_task_id", label=str(frozen_task_path)
            )
            if frozen_execution_task_id != task.execution_task_id:
                raise LF022LeanCheckError(
                    f"selected batch task ID differs from its binding: {task.execution_task_id}"
                )
            task_content_hashes[task.execution_task_id] = hash_canonical(frozen_task)
    ordered_task_ids = tuple(task_ids)
    if len(ordered_task_ids) != manifest.total_task_count or len(set(ordered_task_ids)) != len(
        ordered_task_ids
    ):
        raise LF022LeanCheckError("LF-022 batch selector contains inconsistent task IDs")
    return (
        manifest,
        ordered_task_ids,
        task_content_hashes,
        _artifact_label(batch_manifest_path, repo_root),
    )


def _validation_source(candidate: _Candidate) -> tuple[str, str]:
    statement = candidate.variant.extracted_statement
    assert statement is not None
    match = _DECLARATION_NAME.match(statement.strip())
    if match is None:
        raise LF022LeanCheckError(f"variant {candidate.variant.variant_id} lacks declaration name")
    unique_name = "V_" + candidate.variant.variant_id.removeprefix("var:")[:24]
    renamed = statement.strip()[: match.start(1)] + unique_name + statement.strip()[match.end(1) :]
    full_name = f"LeanFaithLF022Check.{unique_name}"
    code = (
        candidate.import_header
        + "namespace LeanFaithLF022Check\n"
        + renamed
        + " := by sorry\n"
        + "end LeanFaithLF022Check\n"
    )
    return code, full_name


def _request(candidate: _Candidate, *, timeout_seconds: float) -> tuple[LeanRequest, str]:
    code, expected_name = _validation_source(candidate)
    request = LeanRequest(
        request_id="lf022-lean-check-" + candidate.variant.variant_id.removeprefix("var:"),
        context_id=candidate.context_id,
        code=code,
        declarations=True,
        allow_sorry=True,
        timeout_seconds=timeout_seconds,
        metadata={
            "artifact_kind": "lf022_provisional_lean_check",
            "variant_id": candidate.variant.variant_id,
        },
    )
    return request, expected_name


def _attempt_record(result: LeanResult, *, attempt_index: int) -> LF022LeanCheckAttempt:
    raw_hash: str | None = None
    if result.raw_response_path is not None:
        raw_path = Path(result.raw_response_path)
        if not raw_path.is_file():
            raise LF022LeanCheckError(f"Lean raw response is missing: {raw_path}")
        raw_hash = hash_file(raw_path)
    return LF022LeanCheckAttempt(
        attempt_index=attempt_index,
        request_hash=result.request_hash,
        lean_status=result.status,
        elapsed_ms=result.elapsed_ms,
        messages=tuple(cast(dict[str, object], dict(item)) for item in result.messages),
        sorries=tuple(cast(dict[str, object], dict(item)) for item in result.sorries),
        declarations=tuple(cast(dict[str, object], dict(item)) for item in result.declarations),
        raw_response_path=result.raw_response_path,
        raw_response_sha256=raw_hash,
        infrastructure_error=result.infrastructure_error,
    )


def _outcome(
    result: LeanResult, *, expected_name: str
) -> tuple[LeanCheckOutcome, ValidationStatus, bool]:
    declared_names = tuple(
        str(item.get("full_name") or item.get("name") or "")
        for item in result.declarations
        if str(item.get("kind") or "") in {"theorem", "lemma"}
    )
    declaration_verified = declared_names == (expected_name,)
    if result.status is LeanStatus.VALID:
        if not declaration_verified:
            return "declaration_mismatch", ValidationStatus.QUARANTINED, False
        return "elaborates", ValidationStatus.ELABORATES, True
    if result.status is LeanStatus.VALID_WITH_SORRY:
        if not declaration_verified:
            return "declaration_mismatch", ValidationStatus.QUARANTINED, False
        return (
            "elaborates_with_placeholder",
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            True,
        )
    if result.status is LeanStatus.INVALID:
        return "invalid", ValidationStatus.INVALID, False
    if result.status is LeanStatus.TIMEOUT:
        return "timeout", ValidationStatus.TIMEOUT, False
    if result.status is LeanStatus.UNSUPPORTED:
        return "unsupported", ValidationStatus.QUARANTINED, False
    return "infrastructure_error", ValidationStatus.INFRASTRUCTURE_ERROR, False


def _make_record(
    *,
    candidate: _Candidate,
    project_revision: str,
    request: LeanRequest,
    expected_name: str,
    results: Sequence[LeanResult],
    repo_root: Path,
) -> LF022LeanCheckRecord:
    final = results[-1]
    outcome, validation_status, declaration_verified = _outcome(final, expected_name=expected_name)
    assert request.code is not None
    payload: dict[str, object] = {
        "schema_version": 1,
        "method_version": LF022_LEAN_CHECK_METHOD_VERSION,
        "variant_id": candidate.variant.variant_id,
        "source_variant_artifact": _artifact_label(candidate.variant_path, repo_root),
        "source_variant_artifact_sha256": hash_file(candidate.variant_path),
        "source_variant_line_number": candidate.variant_line_number,
        "source_variant_line_sha256": sha256_hex(candidate.variant_line),
        "candidate_code_hash": candidate.variant.candidate_code_hash,
        "context_id": candidate.context_id,
        "source_id": candidate.source_id,
        "source_revision": candidate.source_revision,
        "project_dir": str(candidate.project_dir.resolve()),
        "project_revision": project_revision,
        "import_header": candidate.import_header,
        "import_header_sha256": sha256_hex(candidate.import_header.encode("utf-8")),
        "request_id": request.request_id,
        "request_code_sha256": sha256_hex(request.code.encode("utf-8")),
        "lean_status": final.status,
        "validation_status": validation_status,
        "outcome": outcome,
        "declaration_verified": declaration_verified,
        "fresh_invalid_confirmation_enabled": True,
        "attempts": tuple(
            _attempt_record(result, attempt_index=index) for index, result in enumerate(results)
        ),
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
    }
    draft = LF022LeanCheckRecord.model_construct(
        check_id="lf022_lean_check:" + "0" * 64,
        **cast(dict[str, Any], payload),
    )
    return LF022LeanCheckRecord.model_validate({**payload, "check_id": _check_record_id(draft)})


def _record_path(output_root: Path, variant_id: str) -> Path:
    digest = variant_id.removeprefix("var:")
    return output_root / "records" / digest[:2] / f"{digest}.json"


def _load_resume_record(
    path: Path,
    *,
    candidate: _Candidate,
    project_revision: str,
) -> LF022LeanCheckRecord:
    try:
        raw = path.read_bytes()
        record = LF022LeanCheckRecord.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise LF022LeanCheckError(f"invalid resume record {path}: {exc}") from exc
    if raw != canonical_json_bytes(record.model_dump(mode="json")) + b"\n":
        raise LF022LeanCheckError(f"resume record is not canonical JSON: {path}")
    if (
        record.variant_id != candidate.variant.variant_id
        or record.source_variant_artifact_sha256 != hash_file(candidate.variant_path)
        or record.source_variant_line_number != candidate.variant_line_number
        or record.source_variant_line_sha256 != sha256_hex(candidate.variant_line)
        or record.candidate_code_hash != candidate.variant.candidate_code_hash
        or record.context_id != candidate.context_id
        or record.source_id != candidate.source_id
        or record.source_revision != candidate.source_revision
        or record.project_dir != str(candidate.project_dir.resolve())
        or record.project_revision != project_revision
        or record.import_header != candidate.import_header
    ):
        raise LF022LeanCheckError(
            f"resume record no longer binds the immutable source variant: {path}"
        )
    return record


def _write_atomic(path: Path, payload: bytes) -> None:
    _safe_output_directory(path.parent, label="Lean-check artifact parent")
    if path.is_symlink():
        raise LF022LeanCheckError(f"Lean-check artifact cannot be a symlink: {path}")
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise LF022LeanCheckError(f"Lean-check temporary artifact cannot be a symlink: {temporary}")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _write_immutable(path: Path, payload: bytes) -> None:
    _safe_output_directory(path.parent, label="Lean-check artifact parent")
    if path.is_symlink():
        raise LF022LeanCheckError(f"Lean-check artifact cannot be a symlink: {path}")
    if path.exists():
        if path.read_bytes() != payload:
            raise LF022LeanCheckError(f"immutable artifact differs: {path}")
        return
    path.write_bytes(payload)


def _run_requests_with_infrastructure_retries(
    backend: _Backend,
    requests: Sequence[LeanRequest],
    *,
    max_attempts: int,
) -> list[tuple[LeanResult, ...]]:
    attempts: list[list[LeanResult]] = [[] for _ in requests]
    pending = list(range(len(requests)))
    for attempt_index in range(max_attempts):
        if not pending:
            break
        if attempt_index:
            backend.reset_session()
        attempt_requests = [
            replace(
                requests[index],
                metadata={**dict(requests[index].metadata), "attempt": str(attempt_index)},
            )
            for index in pending
        ]
        results = backend.run_batch(attempt_requests)
        if len(results) != len(attempt_requests):
            raise LF022LeanCheckError("Lean backend returned the wrong batch length")
        next_pending: list[int] = []
        for original_index, result in zip(pending, results, strict=True):
            if result.request_id != requests[original_index].request_id:
                raise LF022LeanCheckError("Lean backend did not preserve request order")
            attempts[original_index].append(result)
            if result.status in _RETRYABLE_INFRASTRUCTURE and attempt_index + 1 < max_attempts:
                next_pending.append(original_index)
        pending = next_pending
    if any(not lineage for lineage in attempts):
        raise LF022LeanCheckError("Lean execution omitted one or more candidates")
    return [tuple(lineage) for lineage in attempts]


def check_lf022_provisional_candidates(
    *,
    repo_root: Path,
    input_root: Path,
    output_root: Path,
    project_dirs: Mapping[str, Path],
    workers: int,
    chunk_size: int,
    timeout_seconds: float,
    max_attempts: int = 2,
    memory_hard_limit_mb: int | None = None,
    environment_schema_version: int = 1,
    limit: int | None = None,
    batch_manifest_path: Path | None = None,
    postgen_selector_path: Path | None = None,
    expected_postgen_selector_id: str | None = None,
    backend_factory: BackendFactory = LeanInteractBackend,
    prepare_environment: PrepareEnvironment = LeanInteractBackend.prepare_environment,
) -> LF022LeanCheckRunResult:
    """Check all current successful LF-022 provisional variants.

    Existing content-addressed per-variant records are reused only when they
    still bind the exact source JSONL bytes.  New source tasks can therefore be
    added and checked on a later invocation without repeating prior work.
    """

    if workers < 1 or chunk_size < 1 or max_attempts < 1:
        raise LF022LeanCheckError("workers, chunk_size, and max_attempts must be positive")
    if timeout_seconds <= 0:
        raise LF022LeanCheckError("timeout_seconds must be positive")
    if limit is not None and limit < 1:
        raise LF022LeanCheckError("limit must be positive when supplied")
    selection_inputs = sum(
        value is not None for value in (batch_manifest_path, postgen_selector_path)
    )
    if selection_inputs > 1:
        raise LF022LeanCheckError("batch manifest and postgen selector are mutually exclusive")
    if expected_postgen_selector_id is not None and postgen_selector_path is None:
        raise LF022LeanCheckError(
            "expected postgen selector ID requires a postgen selector artifact"
        )
    if (
        expected_postgen_selector_id is not None
        and re.fullmatch(
            id_pattern("lf022_postgen_terminal_selector"), expected_postgen_selector_id
        )
        is None
    ):
        raise LF022LeanCheckError("expected postgen selector ID is malformed")
    if limit is not None and selection_inputs:
        raise LF022LeanCheckError("--limit cannot truncate an exact task selection")
    repo_root = repo_root.resolve()
    supplied_input_root = input_root.absolute()
    input_root = input_root.resolve()
    supplied_output_root = output_root.absolute()
    if supplied_output_root == input_root or supplied_output_root.is_relative_to(input_root):
        raise LF022LeanCheckError("output_root must not be inside the immutable input root")
    output_root = _safe_output_directory(output_root, label="Lean-check output root")
    if output_root == input_root or output_root.is_relative_to(input_root):
        raise LF022LeanCheckError("output_root must not be inside the immutable input root")

    selection_manifest: LF022PublicBatchManifest | None = None
    selection_manifest_label: str | None = None
    selection_manifest_sha256: str | None = None
    selection_postgen_selector_id: str | None = None
    selection_postgen_selector_label: str | None = None
    selection_postgen_selector_sha256: str | None = None
    selected_execution_task_ids: tuple[str, ...] | None = None
    selected_task_content_hashes: dict[str, str] | None = None
    selected_terminal_artifacts: dict[str, tuple[Path, str]] | None = None
    if batch_manifest_path is not None:
        batch_manifest_path = batch_manifest_path.resolve()
        (
            selection_manifest,
            selected_execution_task_ids,
            selected_task_content_hashes,
            selection_manifest_label,
        ) = _load_batch_selection(
            repo_root=repo_root,
            batch_manifest_path=batch_manifest_path,
        )
        selection_manifest_sha256 = hash_file(batch_manifest_path)
    elif postgen_selector_path is not None:
        from leanfaith.generation.lf022_postgen_reconcile import (
            LF022PostgenReconciliationError,
            verify_lf022_postgen_terminal_selector,
        )

        postgen_selector_path = _safe_existing_file(
            postgen_selector_path,
            label="postgen terminal selector",
        )
        try:
            verified_selector = verify_lf022_postgen_terminal_selector(
                repo_root=repo_root,
                selector_path=postgen_selector_path,
            )
        except LF022PostgenReconciliationError as exc:
            raise LF022LeanCheckError(f"postgen terminal selector rejected: {exc}") from exc
        selector = verified_selector.selector
        if (
            expected_postgen_selector_id is not None
            and selector.selector_id != expected_postgen_selector_id
        ):
            raise LF022LeanCheckError(
                "verified postgen selector differs from the explicitly expected selector ID"
            )
        selected_execution_task_ids = verified_selector.execution_task_ids
        selected_task_content_hashes = verified_selector.task_content_hashes
        selected_terminal_artifacts = {
            task_id: (
                verified_selector.terminal_paths[task_id],
                verified_selector.terminal_bindings[task_id].sha256,
            )
            for task_id in selected_execution_task_ids
        }
        expected_input_root = _safe_existing_directory(
            repo_root / verified_selector.manifest.executor_output_root,
            label="selector executor output root",
        )
        canonical_supplied_input_root = _safe_existing_directory(
            supplied_input_root,
            label="Lean-check input root",
        )
        if canonical_supplied_input_root != expected_input_root:
            raise LF022LeanCheckError(
                "postgen selector input_root differs from its frozen executor output root"
            )
        selection_postgen_selector_id = selector.selector_id
        selection_postgen_selector_label = _artifact_label(postgen_selector_path, repo_root)
        selection_postgen_selector_sha256 = hash_file(postgen_selector_path)
        selection_manifest = verified_selector.manifest

    candidates = _discover_candidates(
        repo_root=repo_root,
        input_root=input_root,
        project_dirs=project_dirs,
        limit=limit,
        selected_execution_task_ids=selected_execution_task_ids,
        selected_task_content_hashes=selected_task_content_hashes,
        selected_terminal_artifacts=selected_terminal_artifacts,
    )
    input_hashes = {
        candidate.variant_path: hash_file(candidate.variant_path) for candidate in candidates
    }
    records: list[LF022LeanCheckRecord | None] = [None] * len(candidates)
    pending_by_group: dict[tuple[str, str, str], list[_Candidate]] = defaultdict(list)
    project_revisions: dict[Path, str] = {}
    for candidate in candidates:
        project_dir = candidate.project_dir.resolve()
        if project_dir not in project_revisions:
            project_revisions[project_dir] = read_git_revision(project_dir)
        if project_revisions[project_dir] != candidate.source_revision:
            raise LF022LeanCheckError(
                f"project revision {project_revisions[project_dir]} differs from task source "
                f"revision {candidate.source_revision}"
            )
    reused_count = 0
    for candidate in candidates:
        path = _record_path(output_root, candidate.variant.variant_id)
        _safe_output_directory(path.parent, label="Lean-check record directory")
        if path.is_file():
            records[candidate.position] = _load_resume_record(
                path,
                candidate=candidate,
                project_revision=project_revisions[candidate.project_dir.resolve()],
            )
            reused_count += 1
        else:
            pending_by_group[candidate.group_key].append(candidate)

    prepared_projects: set[Path] = set()
    executed_count = 0
    for group_key in sorted(pending_by_group):
        group = pending_by_group[group_key]
        project_dir = group[0].project_dir.resolve()
        source_revisions = {item.source_revision for item in group}
        if len(source_revisions) != 1:
            raise LF022LeanCheckError("one execution group contains multiple source revisions")
        project_revision = project_revisions[project_dir]
        if project_revision not in source_revisions:
            raise LF022LeanCheckError(
                f"project revision {project_revision} differs from task source revision "
                f"{next(iter(source_revisions))}"
            )
        context_fingerprint = group[0].context_id.removeprefix("ctx:")
        base_settings = BackendSettings(
            project_dir=project_dir,
            context_fingerprint=context_fingerprint,
            environment_schema_version=environment_schema_version,
            raw_response_dir=output_root / "raw_responses" / context_fingerprint,
            server_mode=ServerMode.POOL,
            workers=workers,
            memory_hard_limit_mb=memory_hard_limit_mb,
            enable_incremental_optimization=True,
            enable_parallel_elaboration=True,
            isolate_incremental_commands=True,
            confirm_invalid_on_fresh_process=True,
            environment_is_prepared=False,
        )
        _safe_output_directory(
            base_settings.raw_response_dir,
            label="Lean-check raw-response directory",
        )
        if project_dir not in prepared_projects:
            prepare_environment(base_settings)
            prepared_projects.add(project_dir)
        backend = backend_factory(replace(base_settings, environment_is_prepared=True))
        try:
            for start in range(0, len(group), chunk_size):
                chunk = group[start : start + chunk_size]
                request_pairs = [
                    _request(candidate, timeout_seconds=timeout_seconds) for candidate in chunk
                ]
                requests = [item[0] for item in request_pairs]
                lineages = _run_requests_with_infrastructure_retries(
                    backend,
                    requests,
                    max_attempts=max_attempts,
                )
                for candidate, (request, expected_name), lineage in zip(
                    chunk, request_pairs, lineages, strict=True
                ):
                    record = _make_record(
                        candidate=candidate,
                        project_revision=project_revision,
                        request=request,
                        expected_name=expected_name,
                        results=lineage,
                        repo_root=repo_root,
                    )
                    _write_immutable(
                        _record_path(output_root, candidate.variant.variant_id),
                        canonical_json_bytes(record.model_dump(mode="json")) + b"\n",
                    )
                    records[candidate.position] = record
                    executed_count += 1
        finally:
            backend.close()

    for path, before in input_hashes.items():
        if hash_file(path) != before:
            raise LF022LeanCheckError(f"source variant artifact changed during checking: {path}")
    if selected_terminal_artifacts is not None:
        for task_id, (terminal_path, expected_hash) in selected_terminal_artifacts.items():
            if terminal_path.is_symlink() or hash_file(terminal_path) != expected_hash:
                raise LF022LeanCheckError(
                    f"selected execution terminal changed during checking: {task_id}"
                )
    ordered = tuple(record for record in records if record is not None)
    if len(ordered) != len(candidates):
        raise LF022LeanCheckError("one or more candidates lack a terminal check record")
    checks_bytes = b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in ordered
    )
    checks_path = output_root / "checks.jsonl"
    _write_atomic(checks_path, checks_bytes)
    status_counts = dict(sorted(Counter(record.lean_status.value for record in ordered).items()))
    outcome_counts: dict[str, int] = dict(
        sorted(Counter(str(record.outcome) for record in ordered).items())
    )
    input_projection = [
        {
            "variant_id": candidate.variant.variant_id,
            "line_sha256": sha256_hex(candidate.variant_line),
            "context_id": candidate.context_id,
            "import_header_sha256": sha256_hex(candidate.import_header.encode("utf-8")),
            "project_dir": str(candidate.project_dir.resolve()),
        }
        for candidate in candidates
    ]
    if postgen_selector_path is not None:
        replayed_selector_path = _safe_existing_file(
            postgen_selector_path,
            label="postgen terminal selector before manifest write",
        )
        if (
            replayed_selector_path != postgen_selector_path
            or selection_postgen_selector_sha256 is None
            or hash_file(replayed_selector_path) != selection_postgen_selector_sha256
        ):
            raise LF022LeanCheckError("postgen terminal selector changed after verification")
    manifest = LF022LeanCheckManifest(
        schema_version=(3 if selection_postgen_selector_id is not None else 2),
        input_root=_artifact_label(input_root, repo_root),
        selection_batch_id=(
            selection_manifest.batch_id if selection_manifest is not None else None
        ),
        selection_batch_manifest=selection_manifest_label,
        selection_batch_manifest_sha256=selection_manifest_sha256,
        selection_postgen_selector_id=selection_postgen_selector_id,
        selection_postgen_selector=selection_postgen_selector_label,
        selection_postgen_selector_sha256=selection_postgen_selector_sha256,
        selected_execution_task_count=(
            len(selected_execution_task_ids) if selected_execution_task_ids is not None else None
        ),
        input_set_hash=hash_canonical(input_projection),
        record_count=len(ordered),
        ordered_variant_ids_hash=hash_canonical([record.variant_id for record in ordered]),
        checks_artifact=_artifact_label(checks_path, repo_root),
        checks_sha256=sha256_hex(checks_bytes),
        status_counts=status_counts,
        outcome_counts=outcome_counts,
    )
    manifest_path = output_root / "manifest.json"
    _write_atomic(
        manifest_path,
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    )
    return LF022LeanCheckRunResult(
        records=ordered,
        manifest=manifest,
        manifest_path=manifest_path,
        reused_count=reused_count,
        executed_count=executed_count,
    )


def parse_project_mappings(values: Sequence[str], *, repo_root: Path) -> dict[str, Path]:
    """Parse repeated ``SOURCE_ID=PROJECT_DIR`` CLI values."""

    mappings: dict[str, Path] = {}
    for value in values:
        source_id, separator, directory = value.partition("=")
        if not separator or not source_id.strip() or not directory.strip():
            raise LF022LeanCheckError(
                f"invalid --project {value!r}; expected SOURCE_ID=PROJECT_DIR"
            )
        source_id = source_id.strip()
        path = Path(directory.strip())
        if not path.is_absolute():
            path = repo_root / path
        if source_id in mappings and mappings[source_id].resolve() != path.resolve():
            raise LF022LeanCheckError(f"duplicate conflicting project mapping for {source_id}")
        mappings[source_id] = path.resolve()
    if not mappings:
        raise LF022LeanCheckError("at least one --project SOURCE_ID=PROJECT_DIR is required")
    return mappings


__all__ = [
    "LF022LeanCheckAttempt",
    "LF022LeanCheckError",
    "LF022LeanCheckManifest",
    "LF022LeanCheckRecord",
    "LF022LeanCheckRunResult",
    "check_lf022_provisional_candidates",
    "parse_project_mappings",
]
