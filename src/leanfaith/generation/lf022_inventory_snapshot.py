"""Deterministic, audit-only inventory snapshots for LF-022 generation artifacts.

The snapshot is deliberately not a dataset builder.  It verifies exact frozen
batch/check/audit bindings, counts the observed provisional variants, and
reports mechanical Lean and Codex-audit diagnostics.  It never resolves a
semantic label and every output is explicitly ineligible for training,
evaluation, promotion, or gate credit.

``partial_live`` mode is useful while an executor is still appending immutable
task terminals.  Such a report is always marked non-final.  ``final`` mode is
fail-closed: every frozen task must have a terminal, every generated variant
must have one Lean check, and every Lean-valid pair must have one completed
Codex audit.
"""

from __future__ import annotations

import os
import tempfile
from collections import Counter, defaultdict
from collections.abc import Sequence
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
from leanfaith.generation.lf022_batch import LF022PublicBatchManifest
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditError,
    LF022CodexAuditManifest,
    verify_completed_lf022_codex_audit,
)
from leanfaith.generation.lf022_execution import LF022GOpenExecutionTask
from leanfaith.generation.lf022_executor import LF022ExecutionTerminalRecord
from leanfaith.generation.lf022_lean_check import (
    LF022LeanCheckManifest,
    LF022LeanCheckRecord,
)
from leanfaith.generation.weak_supervision import JudgeResponse
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.variant import VariantRecord

LF022_INVENTORY_SNAPSHOT_VERSION: Literal["lf022_inventory_snapshot_v1"] = (
    "lf022_inventory_snapshot_v1"
)
_LEAN_VALID_OUTCOMES = frozenset({"elaborates", "elaborates_with_placeholder"})


class LF022InventorySnapshotError(RuntimeError):
    """A frozen binding or observed LF-022 artifact violated its contract."""


class SnapshotArtifactBinding(StrictModel):
    """Exact path and byte hash for one frozen input artifact."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)


class LF022InventoryCollectionSpec(StrictModel):
    """One single-proposer LF-022 collection included in the snapshot."""

    collection_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    artifact_root: str = Field(min_length=1)
    proposer_family_id: str = Field(min_length=1)
    proposer_model: str = Field(min_length=1)
    batch_manifest: SnapshotArtifactBinding
    lean_check_manifest: SnapshotArtifactBinding | None = None
    codex_audit_manifest: SnapshotArtifactBinding | None = None

    @model_validator(mode="after")
    def _stage_order(self) -> Self:
        if self.codex_audit_manifest is not None and self.lean_check_manifest is None:
            raise ValueError("Codex audit binding requires a Lean-check binding")
        return self


class LF022InventorySnapshotSpec(StrictModel):
    """Frozen request for either a final or point-in-time live snapshot."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_inventory_snapshot_v1"] = LF022_INVENTORY_SNAPSHOT_VERSION
    mode: Literal["final", "partial_live"]
    collections: tuple[LF022InventoryCollectionSpec, ...] = Field(min_length=1)
    audit_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _unique_and_complete(self) -> Self:
        identifiers = [item.collection_id for item in self.collections]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("collection IDs must be unique")
        if identifiers != sorted(identifiers):
            raise ValueError("collections must be sorted by collection_id")
        if self.mode == "final":
            incomplete = [
                item.collection_id
                for item in self.collections
                if item.lean_check_manifest is None or item.codex_audit_manifest is None
            ]
            if incomplete:
                raise ValueError(
                    "final snapshot requires check and audit bindings: " + ", ".join(incomplete)
                )
        return self


class LF022InventoryCountBucket(StrictModel):
    """Gross and deduplicated counts for one collection/model or all data."""

    planned_task_count: int = Field(ge=0, strict=True)
    observed_terminal_count: int = Field(ge=0, strict=True)
    missing_terminal_count: int = Field(ge=0, strict=True)
    terminal_status_counts: dict[str, int]
    gross_variant_count: int = Field(ge=0, strict=True)
    unique_variant_id_count: int = Field(ge=0, strict=True)
    unique_content_count: int = Field(ge=0, strict=True)
    unique_pair_count: int = Field(ge=0, strict=True)
    lean_checked_count: int = Field(ge=0, strict=True)
    lean_outcome_counts: dict[str, int]
    lean_valid_count: int = Field(ge=0, strict=True)
    lean_valid_unique_content_count: int = Field(ge=0, strict=True)
    lean_valid_unique_pair_count: int = Field(ge=0, strict=True)
    codex_audit_eligible_count: int = Field(ge=0, strict=True)
    codex_audit_completed_count: int = Field(ge=0, strict=True)
    codex_same_claim_counts: dict[str, int]
    codex_relation_counts: dict[str, int]
    codex_completed_unique_content_count: int = Field(ge=0, strict=True)
    codex_completed_unique_pair_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _reconcile(self) -> Self:
        if self.observed_terminal_count + self.missing_terminal_count != self.planned_task_count:
            raise ValueError("terminal counts do not reconcile with planned tasks")
        if sum(self.terminal_status_counts.values()) != self.observed_terminal_count:
            raise ValueError("terminal status counts do not reconcile")
        if sum(self.lean_outcome_counts.values()) != self.lean_checked_count:
            raise ValueError("Lean outcome counts do not reconcile")
        if sum(self.codex_same_claim_counts.values()) != self.codex_audit_completed_count:
            raise ValueError("Codex verdict counts do not reconcile")
        if sum(self.codex_relation_counts.values()) != self.codex_audit_completed_count:
            raise ValueError("Codex relation counts do not reconcile")
        if self.lean_checked_count > self.gross_variant_count:
            raise ValueError("Lean checks exceed generated variants")
        if self.codex_audit_completed_count > self.lean_valid_count:
            raise ValueError("Codex completions exceed Lean-valid variants")
        return self


class LF022InventoryCollectionSnapshot(StrictModel):
    """Verified inventory for one frozen proposer collection."""

    collection_id: str
    proposer_family_id: str
    proposer_model: str
    batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    batch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    terminal_artifact_set_sha256: str = Field(pattern=HEX64_PATTERN)
    lean_check_manifest_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    codex_audit_manifest_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    codex_response_artifact_set_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
        exclude_if=lambda value: value is None,
    )
    generation_complete: bool
    lean_check_complete: bool
    codex_audit_complete: bool
    counts: LF022InventoryCountBucket


class LF022InventoryOverlap(StrictModel):
    """Exact candidate-content or source/candidate-pair overlap diagnostics."""

    key_kind: Literal["candidate_content_hash", "source_candidate_pair_hash"]
    gross_observation_count: int = Field(ge=0, strict=True)
    unique_key_count: int = Field(ge=0, strict=True)
    duplicate_observation_count: int = Field(ge=0, strict=True)
    cross_model_key_count: int = Field(ge=0, strict=True)
    pairwise_model_intersections: dict[str, int]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.duplicate_observation_count != (
            self.gross_observation_count - self.unique_key_count
        ):
            raise ValueError("overlap duplicate count does not reconcile")
        if self.cross_model_key_count > self.unique_key_count:
            raise ValueError("cross-model key count exceeds unique keys")
        return self


class LF022InventorySnapshotReport(StrictModel):
    """Content-addressed, diagnostic-only snapshot report."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_inventory_snapshot_v1"] = LF022_INVENTORY_SNAPSHOT_VERSION
    snapshot_id: str = Field(pattern=id_pattern("lf022_inventory_snapshot"))
    snapshot_spec_sha256: str = Field(pattern=HEX64_PATTERN)
    snapshot_status: Literal["final", "non_final_point_in_time"]
    non_final: bool
    collections: tuple[LF022InventoryCollectionSnapshot, ...]
    by_proposer_model: dict[str, LF022InventoryCountBucket]
    overall: LF022InventoryCountBucket
    candidate_content_overlap: LF022InventoryOverlap
    source_candidate_pair_overlap: LF022InventoryOverlap
    audit_only: Literal[True] = True
    point_in_time_inventory_only: Literal[True] = True
    human_labels_created: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if self.non_final != (self.snapshot_status == "non_final_point_in_time"):
            raise ValueError("non_final flag and status disagree")
        expected = make_id(
            "lf022_inventory_snapshot",
            self.model_dump(mode="json", exclude={"snapshot_id"}),
        )
        if self.snapshot_id != expected:
            raise ValueError("snapshot_id does not match report content")
        return self


@dataclass(frozen=True, slots=True)
class _VariantObservation:
    collection_id: str
    proposer_model: str
    variant_id: str
    candidate_content_hash: str
    pair_hash: str


@dataclass(frozen=True, slots=True)
class _CollectionInventory:
    snapshot: LF022InventoryCollectionSnapshot
    variants: tuple[_VariantObservation, ...]
    lean_valid_variant_ids: frozenset[str]
    codex_completed_variant_ids: frozenset[str]


def _resolve_path(path_text: str, *, repo_root: Path) -> Path:
    path = Path(path_text)
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _regular_file(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise LF022InventorySnapshotError(f"{label} is missing or not a regular file: {path}")
    return path


def _load_bound_model[ModelT: StrictModel](
    binding: SnapshotArtifactBinding,
    model: type[ModelT],
    *,
    repo_root: Path,
    label: str,
) -> tuple[ModelT, Path]:
    path = _regular_file(_resolve_path(binding.path, repo_root=repo_root), label=label)
    observed = hash_file(path)
    if observed != binding.sha256:
        raise LF022InventorySnapshotError(
            f"{label} hash mismatch: expected {binding.sha256}, observed {observed}"
        )
    try:
        return model.model_validate_json(path.read_bytes()), path
    except ValueError as exc:
        raise LF022InventorySnapshotError(f"invalid {label}: {path}: {exc}") from exc


def _resolve_internal_artifact(path_text: str, *, artifact_root: Path, label: str) -> Path:
    pure = PurePosixPath(path_text)
    if (
        not path_text.strip()
        or pure.is_absolute()
        or ".." in pure.parts
        or "." in pure.parts
        or "\\" in path_text
    ):
        raise LF022InventorySnapshotError(f"unsafe {label} path: {path_text}")
    root = artifact_root.resolve(strict=True)
    current = root
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise LF022InventorySnapshotError(f"{label} contains a symlink: {path_text}")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise LF022InventorySnapshotError(f"missing {label}: {path_text}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise LF022InventorySnapshotError(f"{label} escapes artifact root: {path_text}")
    return resolved


def _task_directory(executor_root: Path, task_id: str) -> Path:
    digest = task_id.removeprefix("lf022_execution_task:")
    return executor_root / "tasks" / digest[:2] / digest


def _scan_terminal_artifact_set(
    *,
    executor_root: Path,
    task_ids: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    """Capture the exact immutable terminal set for a frozen ordered task inventory."""

    bindings: list[tuple[str, str]] = []
    for task_id in task_ids:
        terminal_path = _task_directory(executor_root, task_id) / "terminal.json"
        if terminal_path.is_symlink():
            raise LF022InventorySnapshotError(
                f"execution terminal cannot be a symlink: {terminal_path}"
            )
        if terminal_path.exists():
            _regular_file(terminal_path, label="execution terminal")
            bindings.append((task_id, hash_file(terminal_path)))
    return tuple(bindings)


def _scan_collection_terminal_artifact_set(
    spec: LF022InventoryCollectionSpec,
    *,
    repo_root: Path,
) -> tuple[tuple[str, str], ...]:
    batch, _ = _load_bound_model(
        spec.batch_manifest,
        LF022PublicBatchManifest,
        repo_root=repo_root,
        label=f"{spec.collection_id} batch manifest",
    )
    if len(batch.routes) != 1:
        raise LF022InventorySnapshotError("inventory collections must contain one proposer route")
    artifact_root = _resolve_path(spec.artifact_root, repo_root=repo_root)
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise LF022InventorySnapshotError(
            f"{spec.collection_id} artifact root is unavailable: {artifact_root}"
        )
    route = batch.routes[0]
    return _scan_terminal_artifact_set(
        executor_root=artifact_root / batch.executor_output_root,
        task_ids=tuple(binding.execution_task_id for binding in route.tasks),
    )


def _read_jsonl[ModelT: StrictModel](
    path: Path, model: type[ModelT], *, label: str
) -> list[ModelT]:
    raw_lines = path.read_bytes().splitlines(keepends=True)
    records: list[ModelT] = []
    for line_number, line in enumerate(raw_lines, start=1):
        if not line.endswith(b"\n"):
            raise LF022InventorySnapshotError(f"{label} line lacks final newline: {line_number}")
        try:
            records.append(model.model_validate_json(line))
        except ValueError as exc:
            raise LF022InventorySnapshotError(
                f"invalid {label} line {line_number}: {path}: {exc}"
            ) from exc
    return records


def _pair_hash(variant: VariantRecord) -> str:
    return hash_canonical(
        {
            "source_theorem_ids": sorted(variant.source_theorem_ids),
            "candidate_code_hash": variant.candidate_code_hash,
        }
    )


def _validate_terminal_artifacts(
    terminal: LF022ExecutionTerminalRecord,
    *,
    artifact_root: Path,
) -> None:
    bindings = [
        *zip(terminal.attempt_artifacts, terminal.attempt_sha256s, strict=True),
        *zip(terminal.llm_attempt_artifacts, terminal.llm_attempt_sha256s, strict=True),
        (terminal.llm_call_artifact, terminal.llm_call_sha256),
    ]
    if terminal.variants_artifact is not None and terminal.variants_sha256 is not None:
        bindings.append((terminal.variants_artifact, terminal.variants_sha256))
    for path_text, expected_hash in bindings:
        path = _resolve_internal_artifact(
            path_text,
            artifact_root=artifact_root,
            label="terminal-bound artifact",
        )
        if hash_file(path) != expected_hash:
            raise LF022InventorySnapshotError(f"terminal artifact hash mismatch: {path}")


def _load_generation(
    spec: LF022InventoryCollectionSpec,
    *,
    repo_root: Path,
    mode: Literal["final", "partial_live"],
    terminal_cut: tuple[tuple[str, str], ...] | None = None,
) -> tuple[
    LF022PublicBatchManifest,
    Path,
    list[VariantRecord],
    dict[str, tuple[Path, int, str]],
    Counter[str],
    list[dict[str, str]],
]:
    batch, batch_path = _load_bound_model(
        spec.batch_manifest,
        LF022PublicBatchManifest,
        repo_root=repo_root,
        label=f"{spec.collection_id} batch manifest",
    )
    if len(batch.routes) != 1:
        raise LF022InventorySnapshotError("inventory collections must contain one proposer route")
    route = batch.routes[0]
    if route.proposer_family_id != spec.proposer_family_id or route.model_id != spec.proposer_model:
        raise LF022InventorySnapshotError(
            f"{spec.collection_id} proposer identity differs from frozen batch"
        )
    artifact_root = _resolve_path(spec.artifact_root, repo_root=repo_root)
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise LF022InventorySnapshotError(
            f"{spec.collection_id} artifact root is unavailable: {artifact_root}"
        )
    expected_batch_path = artifact_root / batch.batch_directory / "batch_manifest.json"
    if batch_path != expected_batch_path.resolve():
        raise LF022InventorySnapshotError(
            f"{spec.collection_id} batch path does not match batch_directory"
        )
    executor_root = artifact_root / batch.executor_output_root
    terminal_cut_by_id = dict(terminal_cut or ())
    if mode == "partial_live" and terminal_cut is None:
        raise LF022InventorySnapshotError("partial-live generation requires a frozen terminal cut")
    variants: list[VariantRecord] = []
    variant_locations: dict[str, tuple[Path, int, str]] = {}
    terminal_statuses: Counter[str] = Counter()
    terminal_bindings: list[dict[str, str]] = []
    seen_tasks: set[str] = set()
    for binding in route.tasks:
        task_path = _resolve_internal_artifact(
            binding.task.path,
            artifact_root=artifact_root,
            label="frozen execution task",
        )
        if hash_file(task_path) != binding.task.sha256:
            raise LF022InventorySnapshotError(f"frozen task hash mismatch: {task_path}")
        task_bytes = task_path.read_bytes()
        try:
            frozen_task = LF022GOpenExecutionTask.model_validate_json(task_bytes)
        except ValueError as exc:
            raise LF022InventorySnapshotError(f"invalid frozen task: {task_path}: {exc}") from exc
        if frozen_task.execution_task_id != binding.execution_task_id:
            raise LF022InventorySnapshotError(f"frozen task ID mismatch: {task_path}")
        task_dir = _task_directory(executor_root, binding.execution_task_id)
        terminal_path = task_dir / "terminal.json"
        if mode == "partial_live" and binding.execution_task_id not in terminal_cut_by_id:
            continue
        if not terminal_path.exists():
            continue
        _regular_file(terminal_path, label="execution terminal")
        if (
            mode == "partial_live"
            and hash_file(terminal_path) != terminal_cut_by_id[binding.execution_task_id]
        ):
            raise LF022InventorySnapshotError(
                f"{spec.collection_id} frozen live terminal drifted: {terminal_path}"
            )
        copied_task = _regular_file(task_dir / "task.json", label="executor task copy")
        copied_task_bytes = copied_task.read_bytes()
        if copied_task_bytes not in {task_bytes, task_bytes + b"\n"}:
            raise LF022InventorySnapshotError(
                f"executor task differs from batch task: {copied_task}"
            )
        try:
            terminal = LF022ExecutionTerminalRecord.model_validate_json(terminal_path.read_bytes())
        except ValueError as exc:
            raise LF022InventorySnapshotError(f"invalid terminal: {terminal_path}: {exc}") from exc
        if terminal.execution_task_id != binding.execution_task_id:
            raise LF022InventorySnapshotError(f"terminal/task ID mismatch: {terminal_path}")
        if terminal.execution_task_id in seen_tasks:
            raise LF022InventorySnapshotError("duplicate observed execution task")
        seen_tasks.add(terminal.execution_task_id)
        _validate_terminal_artifacts(terminal, artifact_root=artifact_root)
        terminal_hash = hash_file(terminal_path)
        terminal_bindings.append(
            {
                "execution_task_id": terminal.execution_task_id,
                "terminal_sha256": terminal_hash,
            }
        )
        terminal_statuses[terminal.status] += 1
        if terminal.variants_artifact is None:
            continue
        variant_path = _resolve_internal_artifact(
            terminal.variants_artifact,
            artifact_root=artifact_root,
            label="provisional variants",
        )
        loaded = _read_jsonl(variant_path, VariantRecord, label="provisional variant")
        if len(loaded) != terminal.provisional_variant_count:
            raise LF022InventorySnapshotError(
                f"variant count differs from terminal: {terminal_path}"
            )
        for line_number, variant in enumerate(loaded, start=1):
            if variant.generator_id != spec.proposer_model:
                raise LF022InventorySnapshotError(
                    f"variant generator differs from collection model: {variant.variant_id}"
                )
            if variant.variant_id in variant_locations:
                raise LF022InventorySnapshotError(
                    f"duplicate variant ID within collection: {variant.variant_id}"
                )
            variant_locations[variant.variant_id] = (
                variant_path,
                line_number,
                hash_file(variant_path),
            )
            variants.append(variant)
    missing = batch.total_task_count - len(seen_tasks)
    if mode == "final" and missing:
        raise LF022InventorySnapshotError(
            f"{spec.collection_id} final snapshot is missing {missing} task terminals"
        )
    terminal_bindings.sort(key=lambda item: item["execution_task_id"])
    return (
        batch,
        artifact_root,
        variants,
        variant_locations,
        terminal_statuses,
        terminal_bindings,
    )


def _load_checks(
    spec: LF022InventoryCollectionSpec,
    *,
    repo_root: Path,
    batch: LF022PublicBatchManifest,
    variants: list[VariantRecord],
    variant_locations: dict[str, tuple[Path, int, str]],
    mode: Literal["final", "partial_live"],
) -> tuple[
    LF022LeanCheckManifest | None,
    Path | None,
    list[LF022LeanCheckRecord],
    frozenset[str],
]:
    if spec.lean_check_manifest is None:
        if mode == "final":
            raise LF022InventorySnapshotError("final snapshot requires Lean checks")
        return None, None, [], frozenset()
    manifest, _ = _load_bound_model(
        spec.lean_check_manifest,
        LF022LeanCheckManifest,
        repo_root=repo_root,
        label=f"{spec.collection_id} Lean-check manifest",
    )
    if (
        manifest.selection_batch_id != batch.batch_id
        or manifest.selection_batch_manifest_sha256 != spec.batch_manifest.sha256
        or manifest.selected_execution_task_count != batch.total_task_count
    ):
        raise LF022InventorySnapshotError(
            f"{spec.collection_id} Lean-check selection does not bind the batch"
        )
    checks_path = _regular_file(
        _resolve_path(manifest.checks_artifact, repo_root=repo_root),
        label="Lean checks artifact",
    )
    if hash_file(checks_path) != manifest.checks_sha256:
        raise LF022InventorySnapshotError("Lean checks artifact hash mismatch")
    checks = _read_jsonl(checks_path, LF022LeanCheckRecord, label="Lean check")
    if len(checks) != manifest.record_count:
        raise LF022InventorySnapshotError("Lean-check record count differs from manifest")
    outcome_counts = dict(sorted(Counter(check.outcome for check in checks).items()))
    if outcome_counts != manifest.outcome_counts:
        raise LF022InventorySnapshotError("Lean-check outcomes differ from manifest")
    variants_by_id = {variant.variant_id: variant for variant in variants}
    variant_ids = set(variants_by_id)
    observed: set[str] = set()
    valid: set[str] = set()
    for check in checks:
        if check.variant_id not in variant_ids or check.variant_id in observed:
            raise LF022InventorySnapshotError(
                f"Lean check is duplicate or outside collection: {check.variant_id}"
            )
        observed.add(check.variant_id)
        path, line_number, artifact_hash = variant_locations[check.variant_id]
        check_artifact = _resolve_path(check.source_variant_artifact, repo_root=repo_root)
        if (
            check_artifact != path.resolve()
            or check.source_variant_line_number != line_number
            or check.source_variant_artifact_sha256 != artifact_hash
            or check.candidate_code_hash != variants_by_id[check.variant_id].candidate_code_hash
        ):
            raise LF022InventorySnapshotError(
                f"Lean check does not bind exact variant artifact: {check.check_id}"
            )
        lines = path.read_bytes().splitlines(keepends=True)
        try:
            source_line = lines[line_number - 1]
        except IndexError as exc:
            raise LF022InventorySnapshotError(
                f"Lean check source line is missing: {check.check_id}"
            ) from exc
        if sha256_hex(source_line) != check.source_variant_line_sha256:
            raise LF022InventorySnapshotError(
                f"Lean check source line hash mismatch: {check.check_id}"
            )
        if check.outcome in _LEAN_VALID_OUTCOMES:
            valid.add(check.variant_id)
    if mode == "final" and observed != variant_ids:
        raise LF022InventorySnapshotError(
            f"{spec.collection_id} final Lean checks do not cover every variant"
        )
    return manifest, checks_path, checks, frozenset(valid)


def _load_codex_audit(
    spec: LF022InventoryCollectionSpec,
    *,
    repo_root: Path,
    checks_path: Path | None,
    lean_valid_variant_ids: frozenset[str],
    mode: Literal["final", "partial_live"],
) -> tuple[
    LF022CodexAuditManifest | None,
    list[tuple[str, JudgeResponse]],
    str | None,
]:
    if spec.codex_audit_manifest is None:
        if mode == "final":
            raise LF022InventorySnapshotError("final snapshot requires Codex audit")
        return None, [], None
    if checks_path is None:
        raise LF022InventorySnapshotError("Codex audit cannot exist without Lean checks")
    manifest, manifest_path = _load_bound_model(
        spec.codex_audit_manifest,
        LF022CodexAuditManifest,
        repo_root=repo_root,
        label=f"{spec.collection_id} Codex-audit manifest",
    )
    if manifest.checks_sha256 != hash_file(checks_path):
        raise LF022InventorySnapshotError("Codex audit does not bind Lean checks")
    audit_root = manifest_path.parent
    try:
        verified = verify_completed_lf022_codex_audit(
            repo_root=repo_root,
            checks_path=checks_path,
            audit_root=audit_root,
            require_complete_clean=mode == "final",
        )
    except LF022CodexAuditError as exc:
        raise LF022InventorySnapshotError(f"Codex audit replay failed: {exc}") from exc
    if verified.manifest != manifest:
        raise LF022InventorySnapshotError("verified Codex manifest differs from frozen binding")
    if {item.variant_id for item in verified.items} != set(lean_valid_variant_ids):
        raise LF022InventorySnapshotError("Codex eligible items differ from Lean-valid variants")
    judgments = [(item.variant_id, item.response) for item in verified.judgments]
    return manifest, judgments, verified.response_artifact_set_sha256


def _bucket_from_observations(
    *,
    planned_task_count: int,
    terminal_status_counts: Counter[str],
    observations: list[_VariantObservation],
    checks: list[LF022LeanCheckRecord],
    lean_valid_ids: frozenset[str],
    audit_eligible_count: int,
    judgments: list[tuple[str, JudgeResponse]],
) -> LF022InventoryCountBucket:
    by_id = {item.variant_id: item for item in observations}
    valid_observations = [by_id[item] for item in lean_valid_ids]
    judged_observations = [by_id[variant_id] for variant_id, _ in judgments]
    return LF022InventoryCountBucket(
        planned_task_count=planned_task_count,
        observed_terminal_count=sum(terminal_status_counts.values()),
        missing_terminal_count=planned_task_count - sum(terminal_status_counts.values()),
        terminal_status_counts=dict(sorted(terminal_status_counts.items())),
        gross_variant_count=len(observations),
        unique_variant_id_count=len(by_id),
        unique_content_count=len({item.candidate_content_hash for item in observations}),
        unique_pair_count=len({item.pair_hash for item in observations}),
        lean_checked_count=len(checks),
        lean_outcome_counts=dict(sorted(Counter(check.outcome for check in checks).items())),
        lean_valid_count=len(lean_valid_ids),
        lean_valid_unique_content_count=len(
            {item.candidate_content_hash for item in valid_observations}
        ),
        lean_valid_unique_pair_count=len({item.pair_hash for item in valid_observations}),
        codex_audit_eligible_count=audit_eligible_count,
        codex_audit_completed_count=len(judgments),
        codex_same_claim_counts=dict(
            sorted(Counter(response.same_claim_answer for _, response in judgments).items())
        ),
        codex_relation_counts=dict(
            sorted(
                Counter(
                    response.relation.value if response.relation is not None else "null"
                    for _, response in judgments
                ).items()
            )
        ),
        codex_completed_unique_content_count=len(
            {item.candidate_content_hash for item in judged_observations}
        ),
        codex_completed_unique_pair_count=len({item.pair_hash for item in judged_observations}),
    )


def _inventory_collection(
    spec: LF022InventoryCollectionSpec,
    *,
    repo_root: Path,
    mode: Literal["final", "partial_live"],
    terminal_cut: tuple[tuple[str, str], ...] | None = None,
) -> _CollectionInventory:
    (
        batch,
        _artifact_root,
        variants,
        locations,
        terminal_statuses,
        terminal_bindings,
    ) = _load_generation(
        spec,
        repo_root=repo_root,
        mode=mode,
        terminal_cut=terminal_cut,
    )
    check_manifest, checks_path, checks, lean_valid_ids = _load_checks(
        spec,
        repo_root=repo_root,
        batch=batch,
        variants=variants,
        variant_locations=locations,
        mode=mode,
    )
    audit_manifest, judgments, response_artifact_set_sha256 = _load_codex_audit(
        spec,
        repo_root=repo_root,
        checks_path=checks_path,
        lean_valid_variant_ids=lean_valid_ids,
        mode=mode,
    )
    observations: list[_VariantObservation] = []
    for variant in variants:
        if variant.candidate_code_hash is None:
            raise LF022InventorySnapshotError(
                f"variant lacks candidate content hash: {variant.variant_id}"
            )
        observations.append(
            _VariantObservation(
                collection_id=spec.collection_id,
                proposer_model=spec.proposer_model,
                variant_id=variant.variant_id,
                candidate_content_hash=variant.candidate_code_hash,
                pair_hash=_pair_hash(variant),
            )
        )
    bucket = _bucket_from_observations(
        planned_task_count=batch.total_task_count,
        terminal_status_counts=terminal_statuses,
        observations=observations,
        checks=checks,
        lean_valid_ids=lean_valid_ids,
        audit_eligible_count=audit_manifest.eligible_count if audit_manifest else 0,
        judgments=judgments,
    )
    generation_complete = bucket.missing_terminal_count == 0
    check_complete = check_manifest is not None and bucket.lean_checked_count == len(variants)
    audit_complete = (
        audit_manifest is not None
        and audit_manifest.completed_count == audit_manifest.eligible_count
        and audit_manifest.exhausted_count == 0
    )
    snapshot = LF022InventoryCollectionSnapshot(
        collection_id=spec.collection_id,
        proposer_family_id=spec.proposer_family_id,
        proposer_model=spec.proposer_model,
        batch_id=batch.batch_id,
        batch_manifest_sha256=spec.batch_manifest.sha256,
        terminal_artifact_set_sha256=hash_canonical(terminal_bindings),
        lean_check_manifest_sha256=(
            spec.lean_check_manifest.sha256 if spec.lean_check_manifest else None
        ),
        codex_audit_manifest_sha256=(
            spec.codex_audit_manifest.sha256 if spec.codex_audit_manifest else None
        ),
        codex_response_artifact_set_sha256=response_artifact_set_sha256,
        generation_complete=generation_complete,
        lean_check_complete=check_complete,
        codex_audit_complete=audit_complete,
        counts=bucket,
    )
    return _CollectionInventory(
        snapshot=snapshot,
        variants=tuple(observations),
        lean_valid_variant_ids=lean_valid_ids,
        codex_completed_variant_ids=frozenset(variant_id for variant_id, _ in judgments),
    )


def _merge_buckets(inventories: list[_CollectionInventory]) -> LF022InventoryCountBucket:
    observations = [item for inventory in inventories for item in inventory.variants]
    valid_ids = {
        (inventory.snapshot.collection_id, variant_id)
        for inventory in inventories
        for variant_id in inventory.lean_valid_variant_ids
    }
    judged_ids = {
        (inventory.snapshot.collection_id, variant_id)
        for inventory in inventories
        for variant_id in inventory.codex_completed_variant_ids
    }
    valid_observations = [
        item for item in observations if (item.collection_id, item.variant_id) in valid_ids
    ]
    judged_observations = [
        item for item in observations if (item.collection_id, item.variant_id) in judged_ids
    ]
    counts = [inventory.snapshot.counts for inventory in inventories]
    return LF022InventoryCountBucket(
        planned_task_count=sum(item.planned_task_count for item in counts),
        observed_terminal_count=sum(item.observed_terminal_count for item in counts),
        missing_terminal_count=sum(item.missing_terminal_count for item in counts),
        terminal_status_counts=dict(
            sorted(
                sum((Counter(item.terminal_status_counts) for item in counts), Counter()).items()
            )
        ),
        gross_variant_count=len(observations),
        unique_variant_id_count=len({item.variant_id for item in observations}),
        unique_content_count=len({item.candidate_content_hash for item in observations}),
        unique_pair_count=len({item.pair_hash for item in observations}),
        lean_checked_count=sum(item.lean_checked_count for item in counts),
        lean_outcome_counts=dict(
            sorted(sum((Counter(item.lean_outcome_counts) for item in counts), Counter()).items())
        ),
        lean_valid_count=sum(item.lean_valid_count for item in counts),
        lean_valid_unique_content_count=len(
            {item.candidate_content_hash for item in valid_observations}
        ),
        lean_valid_unique_pair_count=len({item.pair_hash for item in valid_observations}),
        codex_audit_eligible_count=sum(item.codex_audit_eligible_count for item in counts),
        codex_audit_completed_count=sum(item.codex_audit_completed_count for item in counts),
        codex_same_claim_counts=dict(
            sorted(
                sum((Counter(item.codex_same_claim_counts) for item in counts), Counter()).items()
            )
        ),
        codex_relation_counts=dict(
            sorted(sum((Counter(item.codex_relation_counts) for item in counts), Counter()).items())
        ),
        codex_completed_unique_content_count=len(
            {item.candidate_content_hash for item in judged_observations}
        ),
        codex_completed_unique_pair_count=len({item.pair_hash for item in judged_observations}),
    )


def _overlap(
    observations: list[_VariantObservation],
    *,
    key_kind: Literal["candidate_content_hash", "source_candidate_pair_hash"],
) -> LF022InventoryOverlap:
    models_by_key: dict[str, set[str]] = defaultdict(set)
    keys_by_model: dict[str, set[str]] = defaultdict(set)
    for item in observations:
        value = (
            item.candidate_content_hash if key_kind == "candidate_content_hash" else item.pair_hash
        )
        models_by_key[value].add(item.proposer_model)
        keys_by_model[item.proposer_model].add(value)
    models = sorted(keys_by_model)
    intersections = {
        f"{left} | {right}": len(keys_by_model[left] & keys_by_model[right])
        for index, left in enumerate(models)
        for right in models[index + 1 :]
    }
    return LF022InventoryOverlap(
        key_kind=key_kind,
        gross_observation_count=len(observations),
        unique_key_count=len(models_by_key),
        duplicate_observation_count=len(observations) - len(models_by_key),
        cross_model_key_count=sum(len(model_ids) > 1 for model_ids in models_by_key.values()),
        pairwise_model_intersections=intersections,
    )


def build_lf022_inventory_snapshot(
    *,
    repo_root: Path,
    spec_path: Path,
    expected_spec_sha256: str,
) -> LF022InventorySnapshotReport:
    """Verify the frozen request and build a deterministic diagnostic snapshot."""

    repo_root = repo_root.resolve()
    spec_path = _regular_file(spec_path.resolve(), label="snapshot spec")
    observed_spec_hash = hash_file(spec_path)
    if observed_spec_hash != expected_spec_sha256:
        raise LF022InventorySnapshotError(
            f"snapshot spec hash mismatch: expected {expected_spec_sha256}, "
            f"observed {observed_spec_hash}"
        )
    try:
        spec = LF022InventorySnapshotSpec.model_validate_json(spec_path.read_bytes())
    except ValueError as exc:
        raise LF022InventorySnapshotError(f"invalid snapshot spec: {exc}") from exc
    live_terminal_sets_before = (
        {
            item.collection_id: _scan_collection_terminal_artifact_set(
                item,
                repo_root=repo_root,
            )
            for item in spec.collections
        }
        if spec.mode == "partial_live"
        else None
    )
    inventories = [
        _inventory_collection(
            item,
            repo_root=repo_root,
            mode=spec.mode,
            terminal_cut=(
                live_terminal_sets_before[item.collection_id]
                if live_terminal_sets_before is not None
                else None
            ),
        )
        for item in spec.collections
    ]
    if live_terminal_sets_before is not None:
        live_terminal_sets_after = {
            item.collection_id: _scan_collection_terminal_artifact_set(
                item,
                repo_root=repo_root,
            )
            for item in spec.collections
        }
        for collection_id, frozen_cut in live_terminal_sets_before.items():
            observed_after = dict(live_terminal_sets_after[collection_id])
            if any(observed_after.get(task_id) != digest for task_id, digest in frozen_cut):
                raise LF022InventorySnapshotError(
                    "frozen live terminal tree drifted during snapshot; retry"
                )
    grouped: dict[str, list[_CollectionInventory]] = defaultdict(list)
    for inventory in inventories:
        grouped[inventory.snapshot.proposer_model].append(inventory)
    by_model = {model: _merge_buckets(grouped[model]) for model in sorted(grouped)}
    overall = _merge_buckets(inventories)
    observations = [item for inventory in inventories for item in inventory.variants]
    status: Literal["final", "non_final_point_in_time"] = (
        "final" if spec.mode == "final" else "non_final_point_in_time"
    )
    content_overlap = _overlap(observations, key_kind="candidate_content_hash")
    pair_overlap = _overlap(observations, key_kind="source_candidate_pair_hash")
    values = {
        "schema_version": 1,
        "method_version": LF022_INVENTORY_SNAPSHOT_VERSION,
        "snapshot_spec_sha256": observed_spec_hash,
        "snapshot_status": status,
        "non_final": spec.mode == "partial_live",
        "collections": [item.snapshot.model_dump(mode="json") for item in inventories],
        "by_proposer_model": {
            model: bucket.model_dump(mode="json") for model, bucket in by_model.items()
        },
        "overall": overall.model_dump(mode="json"),
        "candidate_content_overlap": content_overlap.model_dump(mode="json"),
        "source_candidate_pair_overlap": pair_overlap.model_dump(mode="json"),
        "audit_only": True,
        "point_in_time_inventory_only": True,
        "human_labels_created": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022InventorySnapshotReport(
        snapshot_id=make_id("lf022_inventory_snapshot", values),
        snapshot_spec_sha256=observed_spec_hash,
        snapshot_status=status,
        non_final=spec.mode == "partial_live",
        collections=tuple(item.snapshot for item in inventories),
        by_proposer_model=by_model,
        overall=overall,
        candidate_content_overlap=content_overlap,
        source_candidate_pair_overlap=pair_overlap,
    )


def write_lf022_inventory_snapshot(
    report: LF022InventorySnapshotReport,
    *,
    output_path: Path,
) -> str:
    """Write one immutable canonical report, allowing exact replay only."""

    payload = canonical_json_bytes(report.model_dump(mode="json")) + b"\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_symlink():
        raise LF022InventorySnapshotError(f"immutable snapshot conflict: {output_path}")
    if output_path.exists():
        if not output_path.is_file() or output_path.read_bytes() != payload:
            raise LF022InventorySnapshotError(f"immutable snapshot conflict: {output_path}")
        return hash_file(output_path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".partial",
        dir=output_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output_path)
        except FileExistsError:
            if (
                output_path.is_symlink()
                or not output_path.is_file()
                or output_path.read_bytes() != payload
            ):
                raise LF022InventorySnapshotError(
                    f"concurrent immutable snapshot conflict: {output_path}"
                ) from None
        return hash_file(output_path)
    finally:
        temporary.unlink(missing_ok=True)
