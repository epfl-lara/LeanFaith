"""Bounded sequential scale orchestration for the reviewed Codex proposer.

The v2 adapter deliberately delegates every selected public task to the exact
one-task v1 runner.  It does not widen any v1 schema literal, provider pin, or
execution permission.  Its only additional authority is to execute a bounded
ordered list sequentially, persist one isolated v1 tree per task, and rebuild a
replayable accounting manifest.

Outputs remain unvalidated provisional proposals.  A validator from a
different model family is required before any downstream supervision decision;
this module never creates labels or training/evaluation/gate-eligible records.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_batch import LF022PublicBatchManifest
from leanfaith.generation.lf022_codex_proposer import (
    CodexProposerExecutor,
    LF022CodexProposerError,
    LF022CodexProposerRunResult,
    LoadedLF022CodexProposerConfig,
    load_lf022_codex_proposer_config,
    run_lf022_codex_proposer,
    validate_lf022_codex_proposer_output_root,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id

LF022_CODEX_PROPOSER_SCALE_VERSION: Literal["lf022_codex_proposer_scale_v2"] = (
    "lf022_codex_proposer_scale_v2"
)
LF022_CODEX_PROPOSER_SCALE_HARD_MAXIMUM = 64


class LF022CodexProposerScaleConfig(StrictModel):
    """Reviewed authority for at most 64 sequential v1 proposer calls."""

    schema_version: Literal[2] = 2
    config_id: Literal["lf022_codex_proposer_scale_v2"]
    status: Literal["bounded_public_scale_only"]
    delegate_v1_config: LF022ArtifactBinding
    provider: Literal["openai_codex_exec"]
    provider_slot: Literal["lf022_codex_proposer_terra_v1"]
    model_family: Literal["openai_codex"]
    model: Literal["gpt-5.6-terra"]
    reasoning_effort: Literal["xhigh"]
    task_limit: int = Field(ge=1, le=LF022_CODEX_PROPOSER_SCALE_HARD_MAXIMUM, strict=True)
    hard_maximum_task_count: Literal[64] = 64
    maximum_concurrency: Literal[1] = 1
    execution_order: Literal["manifest_order_sequential"]
    one_v1_invocation_per_task: Literal[True] = True
    immutable_per_item_artifacts: Literal[True] = True
    terminal_replay_required: Literal[True] = True
    execute_requires_explicit_flag: Literal[True] = True
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    own_validator_allowed: Literal[False] = False
    separate_family_validation_required: Literal[True] = True
    validation_performed: Literal[False] = False
    outputs_provisional_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class LoadedLF022CodexProposerScaleConfig:
    config: LF022CodexProposerScaleConfig
    path: Path
    config_file_sha256: str
    effective_config_hash: str
    delegate: LoadedLF022CodexProposerConfig


class LF022CodexProposerScaleTaskResult(StrictModel):
    """One accounted v1 item in a v2 sequential tranche."""

    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    item_id: str = Field(pattern=id_pattern("lf022_codex_proposer_item"))
    terminal_id: str = Field(pattern=id_pattern("lf022_codex_proposer_terminal"))
    terminal_status: str = Field(min_length=1)
    provisional_variant_count: int = Field(ge=0, strict=True)
    delegate_run_manifest: str
    delegate_run_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    terminal_artifact: str
    terminal_sha256: str = Field(pattern=HEX64_PATTERN)
    invoked: bool
    reused: bool

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        _safe_relative(self.delegate_run_manifest, field="delegate_run_manifest")
        _safe_relative(self.terminal_artifact, field="terminal_artifact")
        if self.invoked == self.reused:
            raise ValueError("exactly one of invoked/reused must be true")
        return self


class LF022CodexProposerScaleTranche(StrictModel):
    """Immutable lifetime authority for one content-bound scale root."""

    schema_version: Literal[2] = 2
    tranche_id: str = Field(pattern=id_pattern("lf022_codex_proposer_tranche"))
    method_version: Literal["lf022_codex_proposer_scale_v2"] = LF022_CODEX_PROPOSER_SCALE_VERSION
    config_sha256: str = Field(pattern=HEX64_PATTERN)
    effective_config_hash: str = Field(pattern=HEX64_PATTERN)
    delegate_v1_config_sha256: str = Field(pattern=HEX64_PATTERN)
    source_batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    source_batch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    selection_mode: Literal["manifest_prefix", "explicit_ids"]
    ordered_execution_task_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    ordered_execution_task_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    hard_maximum_task_count: Literal[64] = 64

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if len(set(self.ordered_execution_task_ids)) != len(self.ordered_execution_task_ids):
            raise ValueError("tranche execution-task IDs must be unique")
        if self.ordered_execution_task_ids_sha256 != hash_canonical(
            self.ordered_execution_task_ids
        ):
            raise ValueError("tranche execution-task order hash differs")
        expected = make_id(
            "lf022_codex_proposer_tranche",
            self.model_dump(mode="json", exclude={"tranche_id"}),
        )
        if self.tranche_id != expected:
            raise ValueError("tranche_id does not match immutable tranche content")
        return self


class LF022CodexProposerScaleManifest(StrictModel):
    """Mutable summary rebuilt from immutable, independently replayed v1 trees."""

    schema_version: Literal[2] = 2
    method_version: Literal["lf022_codex_proposer_scale_v2"] = LF022_CODEX_PROPOSER_SCALE_VERSION
    config_artifact: str
    config_sha256: str = Field(pattern=HEX64_PATTERN)
    effective_config_hash: str = Field(pattern=HEX64_PATTERN)
    delegate_v1_config_artifact: str
    delegate_v1_config_sha256: str = Field(pattern=HEX64_PATTERN)
    tranche_id: str = Field(pattern=id_pattern("lf022_codex_proposer_tranche"))
    tranche_artifact: str
    tranche_sha256: str = Field(pattern=HEX64_PATTERN)
    source_batch_manifest: str
    source_batch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    selection_mode: Literal["manifest_prefix", "explicit_ids"]
    available_task_count: int = Field(ge=1, strict=True)
    effective_task_limit: int = Field(
        ge=1,
        le=LF022_CODEX_PROPOSER_SCALE_HARD_MAXIMUM,
        strict=True,
    )
    requested_task_count: int = Field(ge=1, strict=True)
    completed_count: int = Field(ge=0, strict=True)
    invoked_count: int = Field(ge=0, strict=True)
    reused_count: int = Field(ge=0, strict=True)
    provisional_variant_count: int = Field(ge=0, strict=True)
    status_counts: dict[str, int]
    ordered_execution_task_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    ordered_item_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    tasks: tuple[LF022CodexProposerScaleTaskResult, ...]
    model: Literal["gpt-5.6-terra"]
    reasoning_effort: Literal["xhigh"]
    maximum_concurrency: Literal[1] = 1
    sequential_execution: Literal[True] = True
    one_v1_invocation_per_task: Literal[True] = True
    outputs_provisional_only: Literal[True] = True
    separate_family_validation_required: Literal[True] = True
    validation_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _counts(self) -> Self:
        for field in (
            "config_artifact",
            "delegate_v1_config_artifact",
            "tranche_artifact",
            "source_batch_manifest",
        ):
            _safe_relative(getattr(self, field), field=field)
        if self.requested_task_count > self.effective_task_limit:
            raise ValueError("requested task count exceeds effective task limit")
        if self.completed_count != len(self.tasks):
            raise ValueError("completed count differs from task results")
        if self.completed_count != self.requested_task_count:
            raise ValueError("executed manifest must account for every requested task")
        if self.invoked_count + self.reused_count != self.completed_count:
            raise ValueError("invoked/reused counts do not reconcile")
        if sum(self.status_counts.values()) != self.completed_count:
            raise ValueError("terminal status counts do not reconcile")
        if self.provisional_variant_count != sum(
            item.provisional_variant_count for item in self.tasks
        ):
            raise ValueError("provisional variant count does not reconcile")
        task_ids = tuple(item.execution_task_id for item in self.tasks)
        item_ids = tuple(item.item_id for item in self.tasks)
        if len(set(task_ids)) != len(task_ids) or len(set(item_ids)) != len(item_ids):
            raise ValueError("scale manifest tasks/items must be unique")
        if self.ordered_execution_task_ids_sha256 != hash_canonical(task_ids):
            raise ValueError("execution-task order hash does not match task results")
        if self.ordered_item_ids_sha256 != hash_canonical(item_ids):
            raise ValueError("item order hash does not match task results")
        return self


@dataclass(frozen=True, slots=True)
class LF022CodexProposerScaleRunResult:
    selected_execution_task_ids: tuple[str, ...]
    delegate_results: tuple[LF022CodexProposerRunResult, ...]
    manifest: LF022CodexProposerScaleManifest | None
    manifest_path: Path | None
    invoked_count: int
    reused_count: int


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


def _lexical_absolute(path: Path) -> Path:
    """Make a path absolute without following any filesystem links."""

    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink_components(
    path: Path,
    *,
    label: str,
    allow_missing: bool,
) -> Path:
    """Reject direct and ancestor symlinks before any path resolution."""

    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:], start=1):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise LF022CodexProposerError(f"{label} is missing: {current}") from None
        except OSError as exc:
            raise LF022CodexProposerError(
                f"cannot inspect {label} path component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise LF022CodexProposerError(f"{label} contains a symlink component: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise LF022CodexProposerError(f"{label} parent component is not a directory: {current}")
    return absolute


def _relative(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise LF022CodexProposerError("scale artifact escapes repository root") from exc


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


def _write_immutable(path: Path, payload: bytes) -> str:
    path = _reject_symlink_components(
        path,
        label="immutable tranche artifact",
        allow_missing=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(
        path,
        label="immutable tranche artifact",
        allow_missing=True,
    )
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022CodexProposerError(f"immutable tranche conflict: {path}")
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
            if not path.is_file() or path.read_bytes() != payload:
                raise LF022CodexProposerError(
                    f"concurrent immutable tranche conflict: {path}"
                ) from None
        path.chmod(0o600)
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _bound_repo_file(repo_root: Path, binding: LF022ArtifactBinding, *, label: str) -> Path:
    try:
        relative = _safe_relative(binding.path, field=label)
    except ValueError as exc:
        raise LF022CodexProposerError(str(exc)) from exc
    root = _reject_symlink_components(
        repo_root,
        label="repository root",
        allow_missing=False,
    )
    path = _reject_symlink_components(
        root / relative,
        label=label,
        allow_missing=False,
    )
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LF022CodexProposerError(f"{label} escapes repository root") from exc
    if not path.is_file() or hash_file(path) != binding.sha256:
        raise LF022CodexProposerError(f"{label} is missing, unsafe, or differs from pin")
    return path


def load_lf022_codex_proposer_scale_config(
    path: Path,
    *,
    repo_root: Path,
) -> LoadedLF022CodexProposerScaleConfig:
    """Load v2 policy and replay its exact reviewed v1 delegate binding."""

    safe_path = _reject_symlink_components(
        path,
        label="scale config",
        allow_missing=False,
    )
    loaded: LoadedConfig[LF022CodexProposerScaleConfig] = load_config(
        safe_path, LF022CodexProposerScaleConfig
    )
    config = loaded.config
    delegate_path = _bound_repo_file(
        repo_root,
        config.delegate_v1_config,
        label="delegate v1 proposer config",
    )
    delegate = load_lf022_codex_proposer_config(delegate_path, repo_root=repo_root)
    if (
        delegate.config.provider != config.provider
        or delegate.config.provider_slot != config.provider_slot
        or delegate.config.model_family != config.model_family
        or delegate.config.model != config.model
        or delegate.config.reasoning_effort != config.reasoning_effort
        or delegate.config.maximum_task_count != 1
        or delegate.config.maximum_concurrency != 1
    ):
        raise LF022CodexProposerError("v2 provider/model pins differ from reviewed v1 delegate")
    return LoadedLF022CodexProposerScaleConfig(
        config=config,
        path=safe_path,
        config_file_sha256=hash_file(safe_path),
        effective_config_hash=loaded.config_hash,
        delegate=delegate,
    )


def _load_public_batch(path: Path, *, repo_root: Path) -> LF022PublicBatchManifest:
    root = _reject_symlink_components(
        repo_root,
        label="repository root",
        allow_missing=False,
    )
    resolved = _reject_symlink_components(
        path,
        label="public batch manifest",
        allow_missing=False,
    )
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise LF022CodexProposerError("batch manifest must be inside repository root") from exc
    if not resolved.is_file():
        raise LF022CodexProposerError("batch manifest is missing or unsafe")
    raw = resolved.read_bytes()
    try:
        manifest = LF022PublicBatchManifest.model_validate_json(raw)
    except ValueError as exc:
        raise LF022CodexProposerError(f"invalid public batch manifest: {exc}") from exc
    canonical = canonical_json_bytes(manifest.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise LF022CodexProposerError("public batch manifest is not canonical JSON")
    if not (
        manifest.public_sources_only
        and manifest.private_source_content_forbidden
        and manifest.optional_natural_language_forbidden
        and manifest.outputs_provisional_only
        and not manifest.semantic_labels_created
        and not manifest.training_eligible
        and not manifest.evaluation_eligible
    ):
        raise LF022CodexProposerError("batch manifest is not a public provisional source")
    return manifest


def _select_tasks(
    manifest: LF022PublicBatchManifest,
    *,
    requested_ids: Sequence[str],
    effective_limit: int,
) -> tuple[tuple[str, ...], Literal["manifest_prefix", "explicit_ids"]]:
    available = tuple(task.execution_task_id for route in manifest.routes for task in route.tasks)
    if len(set(available)) != len(available):
        raise LF022CodexProposerError("batch contains duplicate execution task IDs")
    requested = tuple(requested_ids)
    if requested:
        if len(set(requested)) != len(requested):
            raise LF022CodexProposerError("requested execution task IDs must be unique")
        if len(requested) > effective_limit:
            raise LF022CodexProposerError("requested task count exceeds effective task limit")
        unknown = tuple(task_id for task_id in requested if task_id not in set(available))
        if unknown:
            raise LF022CodexProposerError(
                f"requested execution tasks are absent from frozen batch: {unknown}"
            )
        return requested, "explicit_ids"
    selected = available[:effective_limit]
    if not selected:
        raise LF022CodexProposerError("frozen batch contains no selectable tasks")
    return selected, "manifest_prefix"


def _delegate_run_root(output_root: Path, execution_task_id: str) -> Path:
    return output_root / "v1_runs" / sha256_hex(execution_task_id.encode("utf-8"))


def _bind_tranche(
    *,
    output_root: Path,
    loaded: LoadedLF022CodexProposerScaleConfig,
    manifest: LF022PublicBatchManifest,
    batch_manifest_path: Path,
    selected_ids: tuple[str, ...],
    selection_mode: Literal["manifest_prefix", "explicit_ids"],
) -> LF022CodexProposerScaleTranche:
    values: dict[str, object] = {
        "schema_version": 2,
        "method_version": LF022_CODEX_PROPOSER_SCALE_VERSION,
        "config_sha256": loaded.config_file_sha256,
        "effective_config_hash": loaded.effective_config_hash,
        "delegate_v1_config_sha256": loaded.delegate.config_file_sha256,
        "source_batch_id": manifest.batch_id,
        "source_batch_manifest_sha256": hash_file(batch_manifest_path),
        "selection_mode": selection_mode,
        "ordered_execution_task_ids": selected_ids,
        "ordered_execution_task_ids_sha256": hash_canonical(selected_ids),
        "hard_maximum_task_count": LF022_CODEX_PROPOSER_SCALE_HARD_MAXIMUM,
    }
    tranche = LF022CodexProposerScaleTranche.model_validate(
        {
            **values,
            "tranche_id": make_id("lf022_codex_proposer_tranche", values),
        }
    )
    output_root.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(
        output_root,
        label="scale output root",
        allow_missing=False,
    )
    tranche_path = output_root / "tranche.json"
    tranche_payload = canonical_json_bytes(tranche.model_dump(mode="json")) + b"\n"
    tranche_existed = tranche_path.exists() or tranche_path.is_symlink()
    if tranche_existed:
        _write_immutable(tranche_path, tranche_payload)
    expected_run_names = {
        sha256_hex(task_id.encode("utf-8")) for task_id in tranche.ordered_execution_task_ids
    }
    runs_root = output_root / "v1_runs"
    if runs_root.exists():
        _reject_symlink_components(
            runs_root,
            label="scale v1 run directory",
            allow_missing=False,
        )
        if not runs_root.is_dir():
            raise LF022CodexProposerError("scale v1 run path is not a directory")
        entries = tuple(runs_root.iterdir())
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                raise LF022CodexProposerError(
                    f"scale root contains an unsafe v1 run entry: {entry}"
                )
        observed = {entry.name for entry in entries}
        foreign = observed - expected_run_names
        if foreign:
            raise LF022CodexProposerError(
                f"scale root contains runs outside its immutable tranche: {sorted(foreign)}"
            )
    if not tranche_existed:
        _write_immutable(tranche_path, tranche_payload)
    return tranche


def run_lf022_codex_proposer_scale(
    *,
    repo_root: Path,
    config_path: Path,
    batch_manifest_path: Path,
    output_root: Path,
    execution_task_ids: Sequence[str] = (),
    task_limit: int | None = None,
    execute_public_provisional: bool = False,
    executor: CodexProposerExecutor | None = None,
    verify_cli_pin: bool = True,
) -> LF022CodexProposerScaleRunResult:
    """Prepare or sequentially execute one bounded public proposer tranche."""

    repo_root = _reject_symlink_components(
        repo_root,
        label="repository root",
        allow_missing=False,
    )
    config_path = _reject_symlink_components(
        config_path,
        label="scale config",
        allow_missing=False,
    )
    batch_manifest_path = _reject_symlink_components(
        batch_manifest_path,
        label="public batch manifest",
        allow_missing=False,
    )
    output_root = _reject_symlink_components(
        output_root,
        label="scale output root",
        allow_missing=True,
    )
    try:
        output_root.relative_to(repo_root)
    except ValueError as exc:
        raise LF022CodexProposerError("output root must stay inside repository root") from exc
    loaded = load_lf022_codex_proposer_scale_config(config_path, repo_root=repo_root)
    effective_limit = loaded.config.task_limit if task_limit is None else task_limit
    if (
        isinstance(effective_limit, bool)
        or effective_limit < 1
        or effective_limit > loaded.config.task_limit
        or effective_limit > LF022_CODEX_PROPOSER_SCALE_HARD_MAXIMUM
    ):
        raise LF022CodexProposerError("effective task limit exceeds reviewed v2 bound")
    manifest = _load_public_batch(batch_manifest_path, repo_root=repo_root)
    selected_ids, selection_mode = _select_tasks(
        manifest,
        requested_ids=execution_task_ids,
        effective_limit=effective_limit,
    )
    tranche = _bind_tranche(
        output_root=output_root,
        loaded=loaded,
        manifest=manifest,
        batch_manifest_path=batch_manifest_path,
        selected_ids=selected_ids,
        selection_mode=selection_mode,
    )
    if not execute_public_provisional and executor is not None:
        raise LF022CodexProposerError("offline preparation rejects a process executor")
    delegate_results: list[LF022CodexProposerRunResult] = []
    for task_id in selected_ids:
        delegate_root = _delegate_run_root(output_root, task_id)
        validate_lf022_codex_proposer_output_root(delegate_root)
        delegate_results.append(
            run_lf022_codex_proposer(
                repo_root=repo_root,
                config_path=loaded.delegate.path,
                batch_manifest_path=batch_manifest_path,
                execution_task_ids=(task_id,),
                output_root=delegate_root,
                execute_public_provisional=execute_public_provisional,
                executor=executor,
                verify_cli_pin=verify_cli_pin,
            )
        )
    results = tuple(delegate_results)
    if not execute_public_provisional:
        return LF022CodexProposerScaleRunResult(
            selected_execution_task_ids=selected_ids,
            delegate_results=results,
            manifest=None,
            manifest_path=None,
            invoked_count=0,
            reused_count=0,
        )

    task_results: list[LF022CodexProposerScaleTaskResult] = []
    for task_id, result in zip(selected_ids, results, strict=True):
        if (
            len(result.prepared) != 1
            or len(result.terminals) != 1
            or result.manifest_path is None
            or result.invoked_count + result.reused_count != 1
        ):
            raise LF022CodexProposerError("v1 delegate did not return one fully accounted task")
        prepared = result.prepared[0]
        terminal = result.terminals[0]
        terminal_path = prepared.item_directory / "terminal.json"
        if not terminal_path.is_file() or terminal_path.is_symlink():
            raise LF022CodexProposerError("v1 delegate terminal artifact is missing or unsafe")
        task_results.append(
            LF022CodexProposerScaleTaskResult(
                execution_task_id=task_id,
                item_id=prepared.item.item_id,
                terminal_id=terminal.terminal_id,
                terminal_status=terminal.status,
                provisional_variant_count=terminal.provisional_variant_count,
                delegate_run_manifest=_relative(repo_root, result.manifest_path),
                delegate_run_manifest_sha256=hash_file(result.manifest_path),
                terminal_artifact=_relative(repo_root, terminal_path),
                terminal_sha256=hash_file(terminal_path),
                invoked=result.invoked_count == 1,
                reused=result.reused_count == 1,
            )
        )
    task_tuple = tuple(task_results)
    status_counts = dict(sorted(Counter(item.terminal_status for item in task_tuple).items()))
    scale_manifest = LF022CodexProposerScaleManifest(
        config_artifact=_relative(repo_root, config_path),
        config_sha256=loaded.config_file_sha256,
        effective_config_hash=loaded.effective_config_hash,
        delegate_v1_config_artifact=_relative(repo_root, loaded.delegate.path),
        delegate_v1_config_sha256=loaded.delegate.config_file_sha256,
        tranche_id=tranche.tranche_id,
        tranche_artifact=_relative(repo_root, output_root / "tranche.json"),
        tranche_sha256=hash_file(output_root / "tranche.json"),
        source_batch_manifest=_relative(repo_root, batch_manifest_path),
        source_batch_manifest_sha256=hash_file(batch_manifest_path),
        selection_mode=selection_mode,
        available_task_count=manifest.total_task_count,
        effective_task_limit=effective_limit,
        requested_task_count=len(selected_ids),
        completed_count=len(task_tuple),
        invoked_count=sum(item.invoked for item in task_tuple),
        reused_count=sum(item.reused for item in task_tuple),
        provisional_variant_count=sum(item.provisional_variant_count for item in task_tuple),
        status_counts=status_counts,
        ordered_execution_task_ids_sha256=hash_canonical(selected_ids),
        ordered_item_ids_sha256=hash_canonical(tuple(item.item_id for item in task_tuple)),
        tasks=task_tuple,
        model=loaded.config.model,
        reasoning_effort=loaded.config.reasoning_effort,
    )
    scale_manifest_path = output_root / "manifest.json"
    _write_atomic(
        scale_manifest_path,
        canonical_json_bytes(scale_manifest.model_dump(mode="json")) + b"\n",
    )
    return LF022CodexProposerScaleRunResult(
        selected_execution_task_ids=selected_ids,
        delegate_results=results,
        manifest=scale_manifest,
        manifest_path=scale_manifest_path,
        invoked_count=scale_manifest.invoked_count,
        reused_count=scale_manifest.reused_count,
    )


__all__ = [
    "LF022_CODEX_PROPOSER_SCALE_HARD_MAXIMUM",
    "LF022CodexProposerScaleConfig",
    "LF022CodexProposerScaleManifest",
    "LF022CodexProposerScaleRunResult",
    "LF022CodexProposerScaleTaskResult",
    "LF022CodexProposerScaleTranche",
    "LoadedLF022CodexProposerScaleConfig",
    "load_lf022_codex_proposer_scale_config",
    "run_lf022_codex_proposer_scale",
]
