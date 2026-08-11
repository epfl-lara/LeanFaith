"""Fail-closed LF-022 batch reconciliation before downstream postprocessing.

The public batch executor intentionally keeps orchestration exceptions outside
the terminal vocabulary.  Consequently an ``executor_rejected`` journal event
is evidence that a task is *not terminal*; it must never be projected into a
provider failure terminal merely to make cardinalities add up.  This module
replays the frozen public batch and its append-only journal, classifies every
task exactly once, and emits content-addressed retry and terminal-selection
artifacts.

No function in this module performs provider I/O.  A retry plan is an explicit
operator input to a later live resume, not authorization to execute it.
"""

from __future__ import annotations

import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_batch import (
    LF022BatchJournalEvent,
    LF022PublicBatchManifest,
    VerifiedLF022BatchTask,
    load_lf022_public_batch,
)
from leanfaith.generation.lf022_executor import (
    LF022ExecutionTerminalRecord,
    LF022ExecutorError,
    prepare_lf022_g_open_execution,
    replay_lf022_g_open_terminal,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.schemas.ids import id_pattern, make_id


class LF022PostgenReconciliationError(RuntimeError):
    """Raised when frozen batch or journal state cannot be reconciled exactly."""


LF022ExecutionTaskID = Annotated[str, Field(pattern=id_pattern("lf022_execution_task"))]


class LF022PostgenSelectedTask(StrictModel):
    """One exact frozen task with one separately verified execution terminal."""

    execution_task_id: LF022ExecutionTaskID
    frozen_task: LF022ArtifactBinding
    terminal_id: str = Field(pattern=id_pattern("lf022_execution_terminal"))
    terminal_status: Literal[
        "provisional_variants_created",
        "proposer_parse_failed",
        "provider_exhausted",
        "transport_unknown",
    ]
    terminal: LF022ArtifactBinding
    terminal_event_id: str = Field(pattern=id_pattern("lf022_batch_event"))
    terminal_event: LF022ArtifactBinding


class LF022PostgenSelectorRoute(StrictModel):
    """Family-preserving route in a terminal-only downstream selector."""

    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3", "glm5", "deepseek_v4"]
    model_id: str = Field(min_length=1)
    tasks: tuple[LF022PostgenSelectedTask, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        ids = tuple(item.execution_task_id for item in self.tasks)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("selector route task IDs must be sorted and unique")
        return self


class LF022PostgenTerminalSelector(StrictModel):
    """Content-addressed snapshot of only verified terminal tasks.

    The selector may be consumed while a larger frozen batch is still running.
    It never changes the batch contract and never includes a preflight-only or
    executor-error task.
    """

    schema_version: Literal[2] = 2
    selector_id: str = Field(pattern=id_pattern("lf022_postgen_terminal_selector"))
    batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    batch_manifest: LF022ArtifactBinding
    journal_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    journal_snapshot: tuple[LF022ArtifactBinding, ...] = Field(min_length=1)
    selection_kind: Literal["verified_terminal_snapshot"] = "verified_terminal_snapshot"
    task_count: int = Field(ge=1, strict=True)
    routes: tuple[LF022PostgenSelectorRoute, ...] = Field(min_length=1)
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    optional_natural_language_forbidden: Literal[True] = True
    outputs_provisional_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if (
            hash_canonical([binding.model_dump(mode="json") for binding in self.journal_snapshot])
            != self.journal_snapshot_hash
        ):
            raise ValueError("selector journal snapshot hash differs from its bindings")
        journal_paths = tuple(binding.path for binding in self.journal_snapshot)
        if journal_paths != tuple(sorted(set(journal_paths))):
            raise ValueError("selector journal snapshot paths must be sorted and unique")
        families = tuple(route.proposer_family_id for route in self.routes)
        if families != tuple(sorted(set(families))):
            raise ValueError("selector routes must be sorted and family-unique")
        ids = tuple(task.execution_task_id for route in self.routes for task in route.tasks)
        if len(ids) != self.task_count or len(set(ids)) != len(ids):
            raise ValueError("selector task cardinality does not reconcile")
        expected = make_id(
            "lf022_postgen_terminal_selector",
            self.model_dump(mode="json", exclude={"selector_id"}),
        )
        if self.selector_id != expected:
            raise ValueError("selector_id does not match selector content")
        return self


class LF022PostgenRetryRoute(StrictModel):
    """Exact nonterminal task IDs retained under their original proposer route."""

    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3", "glm5", "deepseek_v4"]
    model_id: str = Field(min_length=1)
    execution_scope: str = Field(min_length=1)
    error_task_ids: tuple[LF022ExecutionTaskID, ...]
    missing_task_ids: tuple[LF022ExecutionTaskID, ...]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        for value in (self.error_task_ids, self.missing_task_ids):
            if value != tuple(sorted(set(value))):
                raise ValueError("retry route task IDs must be sorted and unique")
        if set(self.error_task_ids) & set(self.missing_task_ids):
            raise ValueError("retry task cannot be both error and missing")
        if not self.error_task_ids and not self.missing_task_ids:
            raise ValueError("retry route cannot be empty")
        return self


class LF022PostgenRetryPlan(StrictModel):
    """Offline-derived plan requiring a separate, explicit live resume."""

    schema_version: Literal[1] = 1
    retry_plan_id: str = Field(pattern=id_pattern("lf022_postgen_retry_plan"))
    batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    batch_manifest: LF022ArtifactBinding
    journal_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonterminal_task_count: int = Field(ge=1, strict=True)
    routes: tuple[LF022PostgenRetryRoute, ...] = Field(min_length=1)
    explicit_live_retry_required: Literal[True] = True
    offline_replay_forbidden_until_complete: Literal[True] = True
    network_calls_this_run: Literal[0] = 0
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    optional_natural_language_forbidden: Literal[True] = True
    outputs_provisional_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        families = tuple(route.proposer_family_id for route in self.routes)
        if families != tuple(sorted(set(families))):
            raise ValueError("retry routes must be sorted and family-unique")
        ids = tuple(
            task_id
            for route in self.routes
            for task_id in (*route.error_task_ids, *route.missing_task_ids)
        )
        if len(ids) != self.nonterminal_task_count or len(set(ids)) != len(ids):
            raise ValueError("retry plan task cardinality does not reconcile")
        expected = make_id(
            "lf022_postgen_retry_plan",
            self.model_dump(mode="json", exclude={"retry_plan_id"}),
        )
        if self.retry_plan_id != expected:
            raise ValueError("retry_plan_id does not match retry plan content")
        return self


class LF022PostgenReconciliation(StrictModel):
    """Exact task partition and post-generation readiness decision."""

    schema_version: Literal[1] = 1
    reconciliation_id: str = Field(pattern=id_pattern("lf022_postgen_reconciliation"))
    batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    batch_manifest: LF022ArtifactBinding
    journal_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    journal_event_count: int = Field(ge=0, strict=True)
    task_count: int = Field(ge=1, strict=True)
    terminal_task_ids: tuple[LF022ExecutionTaskID, ...]
    error_task_ids: tuple[LF022ExecutionTaskID, ...]
    missing_task_ids: tuple[LF022ExecutionTaskID, ...]
    historic_error_task_ids: tuple[LF022ExecutionTaskID, ...]
    terminal_status_counts: dict[str, Annotated[int, Field(ge=0, strict=True)]]
    state: Literal["offline_ready", "live_retry_required"]
    terminal_selector_id: str | None = Field(
        default=None,
        pattern=id_pattern("lf022_postgen_terminal_selector"),
    )
    retry_plan_id: str | None = Field(
        default=None,
        pattern=id_pattern("lf022_postgen_retry_plan"),
    )
    network_calls_this_run: Literal[0] = 0
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    optional_natural_language_forbidden: Literal[True] = True
    outputs_provisional_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        categories = (self.terminal_task_ids, self.error_task_ids, self.missing_task_ids)
        for values in (*categories, self.historic_error_task_ids):
            if values != tuple(sorted(set(values))):
                raise ValueError("reconciliation task IDs must be sorted and unique")
        union = set().union(*(set(values) for values in categories))
        if sum(len(values) for values in categories) != len(union) or len(union) != self.task_count:
            raise ValueError("terminal/error/missing task partition does not reconcile")
        if not set(self.historic_error_task_ids).issubset(set(self.terminal_task_ids)):
            raise ValueError("historic errors must identify tasks with later verified terminals")
        if sum(self.terminal_status_counts.values()) != len(self.terminal_task_ids):
            raise ValueError("terminal status counts do not reconcile")
        if list(self.terminal_status_counts) != sorted(self.terminal_status_counts):
            raise ValueError("terminal status counts must be sorted")
        nonterminal = bool(self.error_task_ids or self.missing_task_ids)
        if nonterminal != (self.state == "live_retry_required"):
            raise ValueError("reconciliation state differs from task partition")
        if nonterminal != (self.retry_plan_id is not None):
            raise ValueError("retry plan presence differs from task partition")
        if bool(self.terminal_task_ids) != (self.terminal_selector_id is not None):
            raise ValueError("terminal selector presence differs from terminal partition")
        expected = make_id(
            "lf022_postgen_reconciliation",
            self.model_dump(mode="json", exclude={"reconciliation_id"}),
        )
        if self.reconciliation_id != expected:
            raise ValueError("reconciliation_id does not match reconciliation content")
        return self


@dataclass(frozen=True, slots=True)
class LF022PostgenReconciliationResult:
    reconciliation: LF022PostgenReconciliation
    reconciliation_path: Path
    retry_plan: LF022PostgenRetryPlan | None
    retry_plan_path: Path | None
    terminal_selector: LF022PostgenTerminalSelector | None
    terminal_selector_path: Path | None


@dataclass(frozen=True, slots=True)
class VerifiedLF022PostgenTerminalSelector:
    """Fully replayed selector plus exact downstream terminal bindings."""

    selector: LF022PostgenTerminalSelector
    manifest: LF022PublicBatchManifest
    execution_task_ids: tuple[str, ...]
    task_content_hashes: dict[str, str]
    terminal_bindings: dict[str, LF022ArtifactBinding]
    terminal_paths: dict[str, Path]


def _repo_file(repo_root: Path, relative: str, *, label: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "." in path.parts or "\\" in relative:
        raise LF022PostgenReconciliationError(f"{label} is not a normalized relative path")
    current = repo_root.resolve(strict=True)
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise LF022PostgenReconciliationError(f"{label} traverses a symlink")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise LF022PostgenReconciliationError(f"{label} is missing: {relative}") from exc
    if not resolved.is_file() or not resolved.is_relative_to(repo_root.resolve(strict=True)):
        raise LF022PostgenReconciliationError(f"{label} is not a repository file")
    return resolved


def _canonical_record[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
    *,
    label: str,
    newline_allowed: bool,
) -> ModelT:
    if path.is_symlink() or not path.is_file():
        raise LF022PostgenReconciliationError(f"{label} is missing or unsafe")
    raw = path.read_bytes()
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022PostgenReconciliationError(f"invalid {label}: {exc}") from exc
    expected = canonical_json_bytes(record.model_dump(mode="json"))
    allowed = {expected, expected + b"\n"} if newline_allowed else {expected}
    if raw not in allowed:
        raise LF022PostgenReconciliationError(f"{label} is not canonical JSON")
    return record


def _safe_existing_file(path: Path, *, label: str) -> Path:
    """Resolve one existing file without allowing a symlink in its full path."""

    candidate = path.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LF022PostgenReconciliationError(f"{label} traverses a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LF022PostgenReconciliationError(f"{label} is missing") from exc
    if resolved != candidate or not resolved.is_file():
        raise LF022PostgenReconciliationError(f"{label} is missing or unsafe")
    return resolved


def _safe_existing_directory(path: Path, *, label: str) -> Path:
    """Resolve one existing directory without allowing symlink traversal."""

    candidate = path.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LF022PostgenReconciliationError(f"{label} traverses a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LF022PostgenReconciliationError(f"{label} is missing") from exc
    if resolved != candidate or not resolved.is_dir():
        raise LF022PostgenReconciliationError(f"{label} is missing or unsafe")
    return resolved


def _safe_directory(path: Path, *, label: str) -> Path:
    """Create/replay a directory while rejecting every symlinked component."""

    candidate = path.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LF022PostgenReconciliationError(f"{label} traverses a symlink")
        if current.exists() and not current.is_dir():
            raise LF022PostgenReconciliationError(f"{label} component is not a directory")
        current.mkdir(exist_ok=True)
        if current.is_symlink() or not current.is_dir():
            raise LF022PostgenReconciliationError(f"{label} became unsafe during creation")
    if candidate.resolve(strict=True) != candidate:
        raise LF022PostgenReconciliationError(f"{label} is not canonical")
    return candidate


def _write_immutable(path: Path, payload: bytes) -> None:
    _safe_directory(path.parent, label="immutable output parent")
    if path.is_symlink():
        raise LF022PostgenReconciliationError("immutable output cannot be a symlink")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022PostgenReconciliationError(f"immutable output conflict: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
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
                raise LF022PostgenReconciliationError(
                    f"concurrent immutable output conflict: {path}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _binding(repo_root: Path, path: Path) -> LF022ArtifactBinding:
    return LF022ArtifactBinding(
        path=path.resolve(strict=True).relative_to(repo_root.resolve(strict=True)).as_posix(),
        sha256=hash_file(path),
    )


def _task_routes(
    tasks: tuple[VerifiedLF022BatchTask, ...],
) -> dict[str, tuple[str, str, str]]:
    return {
        item.task.execution_task_id: (
            item.family,
            item.admission.route.model_id,
            item.admission.route.execution_scope,
        )
        for item in tasks
    }


def reconcile_lf022_postgen(
    *,
    repo_root: Path,
    manifest_path: Path,
    output_root: Path,
) -> LF022PostgenReconciliationResult:
    """Reconcile a frozen batch and emit immutable offline handoff artifacts."""

    repo_root = repo_root.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    try:
        manifest_relative = manifest_path.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise LF022PostgenReconciliationError(
            "batch manifest must remain inside the execution repository"
        ) from exc
    manifest_binding = LF022ArtifactBinding(
        path=manifest_relative,
        sha256=hash_file(manifest_path),
    )
    try:
        manifest, tasks = load_lf022_public_batch(
            repo_root=repo_root,
            manifest_binding=manifest_binding,
        )
    except (OSError, ValueError) as exc:
        raise LF022PostgenReconciliationError(f"frozen batch replay failed: {exc}") from exc
    if not (
        manifest.public_sources_only
        and manifest.private_source_content_forbidden
        and manifest.optional_natural_language_forbidden
        and manifest.outputs_provisional_only
        and not manifest.semantic_labels_created
        and not manifest.training_eligible
        and not manifest.evaluation_eligible
        and not manifest.gate_credit_claimed
    ):
        raise LF022PostgenReconciliationError("batch is not the public provisional contract")

    task_by_id = {item.task.execution_task_id: item for item in tasks}
    route_by_id = _task_routes(tasks)
    journal_dir = repo_root / manifest.journal_directory
    if journal_dir.is_symlink() or not journal_dir.is_dir():
        raise LF022PostgenReconciliationError("batch journal directory is missing or unsafe")
    canonical_journal_dir = _safe_existing_directory(
        journal_dir,
        label="batch journal directory",
    )

    events_by_task: dict[str, list[tuple[LF022BatchJournalEvent, Path]]] = defaultdict(list)
    journal_projection: list[dict[str, str]] = []
    for discovered_path in sorted(canonical_journal_dir.rglob("*.json")):
        event_path = _repo_file(
            repo_root,
            discovered_path.relative_to(repo_root).as_posix(),
            label="batch journal event",
        )
        event = _canonical_record(
            event_path,
            LF022BatchJournalEvent,
            label="batch journal event",
            newline_allowed=False,
        )
        loaded = task_by_id.get(event.execution_task_id)
        if loaded is None:
            raise LF022PostgenReconciliationError("journal contains a task outside the batch")
        if event.batch_id != manifest.batch_id or event.proposer_family_id != loaded.family:
            raise LF022PostgenReconciliationError("journal event route or batch differs")
        expected_dir = event.execution_task_id.split(":", 1)[1]
        expected_event_path = (
            canonical_journal_dir
            / expected_dir
            / f"{event.phase}-{event.event_id.split(':', 1)[1]}.json"
        )
        if event_path != expected_event_path:
            raise LF022PostgenReconciliationError(
                "journal event filename or task path is noncanonical"
            )
        events_by_task[event.execution_task_id].append((event, event_path))
        journal_projection.append(
            {
                "path": event_path.relative_to(repo_root).as_posix(),
                "sha256": hash_file(event_path),
            }
        )
    journal_snapshot_hash = hash_canonical(journal_projection)

    terminal_by_task: dict[
        str,
        tuple[
            LF022ExecutionTerminalRecord,
            LF022ArtifactBinding,
            LF022BatchJournalEvent,
            LF022ArtifactBinding,
        ],
    ] = {}
    error_ids: set[str] = set()
    for task_id, rows in events_by_task.items():
        terminal_rows = [(event, path) for event, path in rows if event.phase == "terminal"]
        if any(event.phase == "error" for event, _ in rows):
            error_ids.add(task_id)
        if not terminal_rows:
            continue
        observed_terminal_record_ids = {event.terminal_id for event, _ in terminal_rows}
        terminal_bindings = {
            (event.terminal_artifact.path, event.terminal_artifact.sha256)
            for event, _ in terminal_rows
            if event.terminal_artifact is not None
        }
        if len(observed_terminal_record_ids) != 1 or len(terminal_bindings) != 1:
            raise LF022PostgenReconciliationError("task has conflicting terminal journal events")
        event, event_path = terminal_rows[0]
        assert event.terminal_artifact is not None
        terminal_path = _repo_file(
            repo_root,
            event.terminal_artifact.path,
            label="execution terminal",
        )
        if hash_file(terminal_path) != event.terminal_artifact.sha256:
            raise LF022PostgenReconciliationError("execution terminal hash differs")
        expected_terminal = (
            repo_root
            / manifest.executor_output_root
            / "tasks"
            / task_id.split(":", 1)[1][:2]
            / task_id.split(":", 1)[1]
            / "terminal.json"
        ).resolve(strict=True)
        if terminal_path != expected_terminal:
            raise LF022PostgenReconciliationError("execution terminal path is noncanonical")
        terminal = _canonical_record(
            terminal_path,
            LF022ExecutionTerminalRecord,
            label="execution terminal",
            newline_allowed=True,
        )
        loaded = task_by_id[task_id]
        if (
            terminal.execution_task_id != task_id
            or terminal.terminal_id != event.terminal_id
            or terminal.status != event.status
            or terminal.execution_admission_id != loaded.admission.admission_id
        ):
            raise LF022PostgenReconciliationError("execution terminal identity differs")
        terminal_by_task[task_id] = (
            terminal,
            event.terminal_artifact,
            event,
            _binding(repo_root, event_path),
        )

    all_ids = set(task_by_id)
    terminal_ids = set(terminal_by_task)
    active_error_ids = error_ids - terminal_ids
    missing_ids = all_ids - terminal_ids - active_error_ids
    historic_error_ids = error_ids & terminal_ids
    if terminal_ids | active_error_ids | missing_ids != all_ids:
        raise LF022PostgenReconciliationError("task categories do not cover the frozen batch")

    selector: LF022PostgenTerminalSelector | None = None
    if terminal_ids:
        selected_by_route: dict[tuple[str, str], list[LF022PostgenSelectedTask]] = defaultdict(list)
        for task_id in sorted(terminal_ids):
            loaded = task_by_id[task_id]
            terminal, terminal_binding, terminal_event, terminal_event_binding = terminal_by_task[
                task_id
            ]
            frozen_task_binding = next(
                task.task
                for route in manifest.routes
                for task in route.tasks
                if task.execution_task_id == task_id
            )
            selected_by_route[(loaded.family, loaded.admission.route.model_id)].append(
                LF022PostgenSelectedTask(
                    execution_task_id=task_id,
                    frozen_task=frozen_task_binding,
                    terminal_id=terminal.terminal_id,
                    terminal_status=terminal.status,
                    terminal=terminal_binding,
                    terminal_event_id=terminal_event.event_id,
                    terminal_event=terminal_event_binding,
                )
            )
        selector_payload: dict[str, object] = {
            "schema_version": 2,
            "batch_id": manifest.batch_id,
            "batch_manifest": manifest_binding.model_dump(mode="json"),
            "journal_snapshot_hash": journal_snapshot_hash,
            "journal_snapshot": journal_projection,
            "selection_kind": "verified_terminal_snapshot",
            "task_count": len(terminal_ids),
            "routes": [
                {
                    "proposer_family_id": family,
                    "model_id": model,
                    "tasks": [item.model_dump(mode="json") for item in selected_by_route[key]],
                }
                for key in sorted(selected_by_route)
                for family, model in (key,)
            ],
            "public_sources_only": True,
            "private_source_content_forbidden": True,
            "optional_natural_language_forbidden": True,
            "outputs_provisional_only": True,
            "semantic_labels_created": False,
            "training_eligible": False,
            "evaluation_eligible": False,
            "gate_credit_claimed": False,
        }
        selector = LF022PostgenTerminalSelector.model_validate(
            {
                **selector_payload,
                "selector_id": make_id("lf022_postgen_terminal_selector", selector_payload),
            }
        )

    retry_plan: LF022PostgenRetryPlan | None = None
    nonterminal_ids = active_error_ids | missing_ids
    if nonterminal_ids:
        retry_by_route: dict[tuple[str, str, str], dict[str, list[str]]] = defaultdict(
            lambda: {"error": [], "missing": []}
        )
        for task_id in sorted(nonterminal_ids):
            key = route_by_id[task_id]
            retry_by_route[key]["error" if task_id in active_error_ids else "missing"].append(
                task_id
            )
        retry_payload: dict[str, object] = {
            "schema_version": 1,
            "batch_id": manifest.batch_id,
            "batch_manifest": manifest_binding.model_dump(mode="json"),
            "journal_snapshot_hash": journal_snapshot_hash,
            "nonterminal_task_count": len(nonterminal_ids),
            "routes": [
                {
                    "proposer_family_id": family,
                    "model_id": model,
                    "execution_scope": scope,
                    "error_task_ids": retry_by_route[key]["error"],
                    "missing_task_ids": retry_by_route[key]["missing"],
                }
                for key in sorted(retry_by_route)
                for family, model, scope in (key,)
            ],
            "explicit_live_retry_required": True,
            "offline_replay_forbidden_until_complete": True,
            "network_calls_this_run": 0,
            "public_sources_only": True,
            "private_source_content_forbidden": True,
            "optional_natural_language_forbidden": True,
            "outputs_provisional_only": True,
            "semantic_labels_created": False,
            "training_eligible": False,
            "evaluation_eligible": False,
            "gate_credit_claimed": False,
        }
        retry_plan = LF022PostgenRetryPlan.model_validate(
            {
                **retry_payload,
                "retry_plan_id": make_id("lf022_postgen_retry_plan", retry_payload),
            }
        )

    status_counts = Counter(row[0].status for row in terminal_by_task.values())
    report_payload: dict[str, object] = {
        "schema_version": 1,
        "batch_id": manifest.batch_id,
        "batch_manifest": manifest_binding.model_dump(mode="json"),
        "journal_snapshot_hash": journal_snapshot_hash,
        "journal_event_count": len(journal_projection),
        "task_count": len(tasks),
        "terminal_task_ids": sorted(terminal_ids),
        "error_task_ids": sorted(active_error_ids),
        "missing_task_ids": sorted(missing_ids),
        "historic_error_task_ids": sorted(historic_error_ids),
        "terminal_status_counts": dict(sorted(status_counts.items())),
        "state": "live_retry_required" if nonterminal_ids else "offline_ready",
        "terminal_selector_id": selector.selector_id if selector is not None else None,
        "retry_plan_id": retry_plan.retry_plan_id if retry_plan is not None else None,
        "network_calls_this_run": 0,
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "optional_natural_language_forbidden": True,
        "outputs_provisional_only": True,
        "semantic_labels_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    report = LF022PostgenReconciliation.model_validate(
        {
            **report_payload,
            "reconciliation_id": make_id("lf022_postgen_reconciliation", report_payload),
        }
    )

    output_root = output_root.absolute()
    if output_root == repo_root or output_root.is_relative_to(repo_root):
        raise LF022PostgenReconciliationError(
            "postgen evidence output must stay outside the execution repository so it cannot "
            "change the frozen executor code-state hash"
        )
    safe_output_root = _safe_directory(output_root, label="postgen evidence output root")
    destination = _safe_directory(
        safe_output_root / report.reconciliation_id.split(":", 1)[1],
        label="content-addressed reconciliation destination",
    )
    report_path = destination / "reconciliation.json"
    retry_path = destination / "retry_plan.json" if retry_plan is not None else None
    selector_path = destination / "terminal_selector.json" if selector is not None else None
    _write_immutable(
        report_path,
        canonical_json_bytes(report.model_dump(mode="json")) + b"\n",
    )
    if retry_plan is not None and retry_path is not None:
        _write_immutable(
            retry_path,
            canonical_json_bytes(retry_plan.model_dump(mode="json")) + b"\n",
        )
    if selector is not None and selector_path is not None:
        _write_immutable(
            selector_path,
            canonical_json_bytes(selector.model_dump(mode="json")) + b"\n",
        )
    return LF022PostgenReconciliationResult(
        reconciliation=report,
        reconciliation_path=report_path,
        retry_plan=retry_plan,
        retry_plan_path=retry_path,
        terminal_selector=selector,
        terminal_selector_path=selector_path,
    )


def verify_lf022_postgen_terminal_selector(
    *,
    repo_root: Path,
    selector_path: Path,
) -> VerifiedLF022PostgenTerminalSelector:
    """Replay every selector binding and the full persisted executor lineage.

    This is the downstream boundary used for safe incremental Lean checking.
    The selector may live outside the execution repository, but every selected
    task, canonical terminal journal event, and execution terminal remains
    bound to the original frozen public batch.
    """

    repo_root = repo_root.resolve(strict=True)
    safe_selector_path = _safe_existing_file(
        selector_path,
        label="postgen terminal selector",
    )
    selector = _canonical_record(
        safe_selector_path,
        LF022PostgenTerminalSelector,
        label="postgen terminal selector",
        newline_allowed=True,
    )
    manifest_path = _repo_file(
        repo_root,
        selector.batch_manifest.path,
        label="selector batch manifest",
    )
    if hash_file(manifest_path) != selector.batch_manifest.sha256:
        raise LF022PostgenReconciliationError("selector batch manifest hash differs")
    try:
        manifest, tasks = load_lf022_public_batch(
            repo_root=repo_root,
            manifest_binding=selector.batch_manifest,
        )
    except (OSError, ValueError) as exc:
        raise LF022PostgenReconciliationError(f"selector batch replay failed: {exc}") from exc
    if manifest.batch_id != selector.batch_id:
        raise LF022PostgenReconciliationError("selector belongs to another batch")
    frozen_by_id = {
        row.execution_task_id: row.task for route in manifest.routes for row in route.tasks
    }
    loaded_by_id = {item.task.execution_task_id: item for item in tasks}
    ordered_ids: list[str] = []
    task_hashes: dict[str, str] = {}
    terminal_bindings: dict[str, LF022ArtifactBinding] = {}
    terminal_paths: dict[str, Path] = {}
    executor_output_root = _safe_existing_directory(
        repo_root / manifest.executor_output_root,
        label="selector executor output root",
    )
    journal_directory = _safe_existing_directory(
        repo_root / manifest.journal_directory,
        label="selector journal directory",
    )
    snapshot_events: dict[str, tuple[LF022BatchJournalEvent, LF022ArtifactBinding]] = {}
    for event_binding in selector.journal_snapshot:
        event_path = _repo_file(
            repo_root,
            event_binding.path,
            label="selector journal snapshot event",
        )
        if hash_file(event_path) != event_binding.sha256:
            raise LF022PostgenReconciliationError("selector journal snapshot hash differs")
        event = _canonical_record(
            event_path,
            LF022BatchJournalEvent,
            label="selector journal snapshot event",
            newline_allowed=False,
        )
        loaded = loaded_by_id.get(event.execution_task_id)
        if loaded is None:
            raise LF022PostgenReconciliationError(
                "selector journal snapshot contains a task outside its batch"
            )
        task_digest = event.execution_task_id.split(":", 1)[1]
        expected_path = (
            journal_directory
            / task_digest
            / f"{event.phase}-{event.event_id.split(':', 1)[1]}.json"
        )
        if (
            event_path != expected_path
            or event.batch_id != selector.batch_id
            or event.proposer_family_id != loaded.family
        ):
            raise LF022PostgenReconciliationError(
                "selector journal snapshot event path or route is noncanonical"
            )
        if event.event_id in snapshot_events:
            raise LF022PostgenReconciliationError(
                "selector journal snapshot repeats an event identity"
            )
        snapshot_events[event.event_id] = (event, event_binding)
    for route in selector.routes:
        for selected in route.tasks:
            loaded = loaded_by_id.get(selected.execution_task_id)
            if loaded is None:
                raise LF022PostgenReconciliationError("selector task is outside its batch")
            if (
                loaded.family != route.proposer_family_id
                or loaded.admission.route.model_id != route.model_id
                or frozen_by_id[selected.execution_task_id] != selected.frozen_task
            ):
                raise LF022PostgenReconciliationError("selector task route or binding differs")
            frozen_path = _repo_file(
                repo_root,
                selected.frozen_task.path,
                label="selector frozen task",
            )
            if hash_file(frozen_path) != selected.frozen_task.sha256:
                raise LF022PostgenReconciliationError("selector frozen task hash differs")

            task_digest = selected.execution_task_id.split(":", 1)[1]
            expected_terminal_relative = (
                Path(manifest.executor_output_root)
                / "tasks"
                / task_digest[:2]
                / task_digest
                / "terminal.json"
            ).as_posix()
            if selected.terminal.path != expected_terminal_relative:
                raise LF022PostgenReconciliationError("selector terminal path is noncanonical")
            expected_event_relative = (
                Path(manifest.journal_directory)
                / task_digest
                / f"terminal-{selected.terminal_event_id.split(':', 1)[1]}.json"
            ).as_posix()
            if selected.terminal_event.path != expected_event_relative:
                raise LF022PostgenReconciliationError(
                    "selector terminal event path is noncanonical"
                )
            snapshot_event = snapshot_events.get(selected.terminal_event_id)
            if snapshot_event is None or snapshot_event[1] != selected.terminal_event:
                raise LF022PostgenReconciliationError(
                    "selector terminal event is absent from its journal snapshot"
                )
            event_path = _repo_file(
                repo_root,
                selected.terminal_event.path,
                label="selector terminal journal event",
            )
            expected_event_path = (
                journal_directory
                / task_digest
                / f"terminal-{selected.terminal_event_id.split(':', 1)[1]}.json"
            )
            if event_path != expected_event_path:
                raise LF022PostgenReconciliationError(
                    "selector terminal journal event is stored at a noncanonical path"
                )
            if hash_file(event_path) != selected.terminal_event.sha256:
                raise LF022PostgenReconciliationError("selector terminal event hash differs")
            event = _canonical_record(
                event_path,
                LF022BatchJournalEvent,
                label="selector terminal journal event",
                newline_allowed=False,
            )
            if (
                event.event_id != selected.terminal_event_id
                or event.batch_id != selector.batch_id
                or event.execution_task_id != selected.execution_task_id
                or event.proposer_family_id != route.proposer_family_id
                or event.phase != "terminal"
                or event.status != selected.terminal_status
                or event.terminal_id != selected.terminal_id
                or event.terminal_artifact != selected.terminal
            ):
                raise LF022PostgenReconciliationError(
                    "selector terminal event binding differs from selected task"
                )

            terminal_path = _repo_file(
                repo_root,
                selected.terminal.path,
                label="selector execution terminal",
            )
            expected_terminal_path = (
                executor_output_root / "tasks" / task_digest[:2] / task_digest / "terminal.json"
            )
            if terminal_path != expected_terminal_path:
                raise LF022PostgenReconciliationError(
                    "selector execution terminal is stored at a noncanonical path"
                )
            if hash_file(terminal_path) != selected.terminal.sha256:
                raise LF022PostgenReconciliationError("selector terminal hash differs")
            terminal = _canonical_record(
                terminal_path,
                LF022ExecutionTerminalRecord,
                label="selector execution terminal",
                newline_allowed=True,
            )
            if (
                terminal.execution_task_id != selected.execution_task_id
                or terminal.execution_admission_id != loaded.admission.admission_id
                or terminal.terminal_id != selected.terminal_id
                or terminal.status != selected.terminal_status
            ):
                raise LF022PostgenReconciliationError(
                    "selector terminal admission, task, status, or identity differs"
                )
            try:
                prepared = prepare_lf022_g_open_execution(
                    repo_root=repo_root,
                    output_root=executor_output_root,
                    admission=loaded.admission,
                    task=loaded.task,
                    verified_admission=loaded.verified,
                    verified_task_inputs=loaded.task_inputs,
                    observed_code_tree_hash=loaded.admission.code_tree_hash,
                )
                replayed_terminal, replayed_path = replay_lf022_g_open_terminal(
                    prepared=prepared,
                    artifact_root=repo_root,
                )
            except (LF022ExecutorError, OSError, ValueError) as exc:
                raise LF022PostgenReconciliationError(
                    f"selector executor terminal replay failed: {exc}"
                ) from exc
            if replayed_path != terminal_path or replayed_terminal != terminal:
                raise LF022PostgenReconciliationError(
                    "selector executor replay differs from selected terminal"
                )
            ordered_ids.append(selected.execution_task_id)
            task_hashes[selected.execution_task_id] = hash_canonical(
                loaded.task.model_dump(mode="json")
            )
            terminal_bindings[selected.execution_task_id] = selected.terminal
            terminal_paths[selected.execution_task_id] = terminal_path
    if len(ordered_ids) != selector.task_count or len(set(ordered_ids)) != len(ordered_ids):
        raise LF022PostgenReconciliationError("selector replay cardinality differs")
    return VerifiedLF022PostgenTerminalSelector(
        selector=selector,
        manifest=manifest,
        execution_task_ids=tuple(ordered_ids),
        task_content_hashes=task_hashes,
        terminal_bindings=terminal_bindings,
        terminal_paths=terminal_paths,
    )


__all__ = [
    "LF022PostgenReconciliation",
    "LF022PostgenReconciliationError",
    "LF022PostgenReconciliationResult",
    "LF022PostgenRetryPlan",
    "LF022PostgenTerminalSelector",
    "VerifiedLF022PostgenTerminalSelector",
    "reconcile_lf022_postgen",
    "verify_lf022_postgen_terminal_selector",
]
