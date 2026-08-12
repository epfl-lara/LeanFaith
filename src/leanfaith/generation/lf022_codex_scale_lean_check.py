"""Fail-closed selector from Codex scale-v2 outputs to LF-022 Lean checking.

The Codex scale adapter intentionally stores one complete proposer-v1 tree per
execution task.  The pooled Lean checker historically consumed the flatter
executor layout instead.  This module bridges those layouts without copying or
rewriting any proposal bytes: it replays the scale, tranche, frozen source task,
v1 input, terminal, and full proposer lineage, then returns exact filesystem
bindings for the existing LeanInteract worker pool.

Verification creates no semantic label and does not promote provisional data.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_batch import LF022PublicBatchManifest
from leanfaith.generation.lf022_codex_proposer import (
    LF022CodexProposerError,
    LF022CodexProposerItem,
    LF022CodexProposerManifest,
    _prepare_item,
    _replay_one,
    validate_lf022_codex_proposer_output_root,
)
from leanfaith.generation.lf022_codex_proposer_scale import (
    LF022CodexProposerScaleManifest,
    LF022CodexProposerScaleTranche,
    _delegate_run_root,
    load_lf022_codex_proposer_scale_config,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding


class LF022CodexScaleLeanCheckError(RuntimeError):
    """A Codex scale selector or one of its bound artifacts failed replay."""


@dataclass(frozen=True, slots=True)
class LF022CodexScaleLeanCheckTask:
    """Exact task and terminal locations selected for mechanical checking."""

    execution_task_id: str
    source_task_path: Path
    source_task_sha256: str
    terminal_path: Path
    terminal_sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedLF022CodexScaleLeanCheckSelector:
    """Fully replayed scale-v2 selection and its immutable lineage snapshot."""

    manifest: LF022CodexProposerScaleManifest
    manifest_path: Path
    manifest_sha256: str
    tranche: LF022CodexProposerScaleTranche
    tranche_path: Path
    tranche_sha256: str
    source_batch: LF022PublicBatchManifest
    source_batch_path: Path
    scale_root: Path
    tasks: tuple[LF022CodexScaleLeanCheckTask, ...]
    artifact_hashes: tuple[tuple[Path, str], ...]

    @property
    def execution_task_ids(self) -> tuple[str, ...]:
        return tuple(task.execution_task_id for task in self.tasks)


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _safe_existing_file(path: Path, *, label: str) -> Path:
    candidate = _lexical_absolute(path)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise LF022CodexScaleLeanCheckError(f"{label} is missing or unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise LF022CodexScaleLeanCheckError(f"{label} traverses a symlink: {current}")
    if not candidate.is_file():
        raise LF022CodexScaleLeanCheckError(f"{label} is not a regular file")
    return candidate


def _safe_existing_directory(path: Path, *, label: str) -> Path:
    candidate = _lexical_absolute(path)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise LF022CodexScaleLeanCheckError(f"{label} is missing or unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise LF022CodexScaleLeanCheckError(f"{label} traverses a symlink: {current}")
    if not candidate.is_dir():
        raise LF022CodexScaleLeanCheckError(f"{label} is not a directory")
    return candidate


def _load_canonical[ModelT: StrictModel](path: Path, *, model: type[ModelT], label: str) -> ModelT:
    safe = _safe_existing_file(path, label=label)
    raw = safe.read_bytes()
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022CodexScaleLeanCheckError(f"invalid {label}: {exc}") from exc
    expected = canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
    if raw != expected:
        raise LF022CodexScaleLeanCheckError(f"{label} is not canonical newline-terminated JSON")
    return record


def _repo_file(repo_root: Path, relative: str, digest: str, *, label: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or "." in path.parts or ".." in path.parts or "\\" in relative:
        raise LF022CodexScaleLeanCheckError(f"{label} is not repository-relative")
    candidate = _safe_existing_file(repo_root / path, label=label)
    try:
        candidate.relative_to(repo_root)
    except ValueError as exc:
        raise LF022CodexScaleLeanCheckError(f"{label} escapes repository root") from exc
    if hash_file(candidate) != digest:
        raise LF022CodexScaleLeanCheckError(f"{label} hash differs from its binding")
    return candidate


def _repo_relative(repo_root: Path, path: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise LF022CodexScaleLeanCheckError("scale artifact escapes repository root") from exc


def _safe_tree_hashes(root: Path) -> tuple[tuple[Path, str], ...]:
    """Hash every regular lineage file and reject links/special files anywhere."""

    hashes: list[tuple[Path, str]] = []
    for directory, child_directories, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in (*child_directories, *filenames):
            candidate = directory_path / name
            try:
                metadata = os.lstat(candidate)
            except OSError as exc:
                raise LF022CodexScaleLeanCheckError(
                    f"cannot inspect scale lineage entry: {candidate}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise LF022CodexScaleLeanCheckError(
                    f"scale lineage contains a symlink: {candidate}"
                )
            if stat.S_ISREG(metadata.st_mode):
                hashes.append((candidate, hash_file(candidate)))
            elif not stat.S_ISDIR(metadata.st_mode):
                raise LF022CodexScaleLeanCheckError(
                    f"scale lineage contains a special file: {candidate}"
                )
    return tuple(sorted(hashes, key=lambda item: item[0].as_posix()))


def _batch_task_bindings(
    manifest: LF022PublicBatchManifest,
) -> dict[str, LF022ArtifactBinding]:
    bindings: dict[str, LF022ArtifactBinding] = {}
    for route in manifest.routes:
        for task in route.tasks:
            if task.execution_task_id in bindings:
                raise LF022CodexScaleLeanCheckError(
                    "source batch contains duplicate execution-task IDs"
                )
            bindings[task.execution_task_id] = task.task
    if len(bindings) != manifest.total_task_count:
        raise LF022CodexScaleLeanCheckError("source batch task count does not reconcile")
    return bindings


def verify_lf022_codex_scale_for_lean_check(
    *,
    repo_root: Path,
    manifest_path: Path,
) -> VerifiedLF022CodexScaleLeanCheckSelector:
    """Replay one complete bounded Codex scale result for pooled Lean checking."""

    repo_root = _safe_existing_directory(repo_root, label="repository root")
    manifest_path = _safe_existing_file(manifest_path, label="Codex scale manifest")
    try:
        manifest_path.relative_to(repo_root)
    except ValueError as exc:
        raise LF022CodexScaleLeanCheckError(
            "Codex scale manifest must be inside the repository root"
        ) from exc
    if manifest_path.name != "manifest.json":
        raise LF022CodexScaleLeanCheckError("Codex scale selector must be named manifest.json")
    scale_root = _safe_existing_directory(manifest_path.parent, label="Codex scale root")
    observed_root_entries = {entry.name for entry in scale_root.iterdir()}
    if observed_root_entries != {"manifest.json", "tranche.json", "v1_runs"}:
        raise LF022CodexScaleLeanCheckError(
            "Codex scale root is partial or contains foreign entries"
        )

    manifest = _load_canonical(
        manifest_path,
        model=LF022CodexProposerScaleManifest,
        label="Codex scale manifest",
    )
    assert isinstance(manifest, LF022CodexProposerScaleManifest)
    tranche_path = scale_root / "tranche.json"
    tranche = _load_canonical(
        tranche_path,
        model=LF022CodexProposerScaleTranche,
        label="Codex scale tranche",
    )
    assert isinstance(tranche, LF022CodexProposerScaleTranche)

    if (
        manifest.tranche_id != tranche.tranche_id
        or manifest.tranche_artifact != _repo_relative(repo_root, tranche_path)
        or manifest.tranche_sha256 != hash_file(tranche_path)
        or manifest.selection_mode != tranche.selection_mode
        or manifest.ordered_execution_task_ids_sha256 != tranche.ordered_execution_task_ids_sha256
        or tuple(task.execution_task_id for task in manifest.tasks)
        != tranche.ordered_execution_task_ids
    ):
        raise LF022CodexScaleLeanCheckError("scale manifest and immutable tranche differ")

    config_path = _repo_file(
        repo_root,
        manifest.config_artifact,
        manifest.config_sha256,
        label="Codex scale config",
    )
    try:
        loaded = load_lf022_codex_proposer_scale_config(config_path, repo_root=repo_root)
    except (LF022CodexProposerError, OSError, ValueError) as exc:
        raise LF022CodexScaleLeanCheckError(f"Codex scale config replay failed: {exc}") from exc
    if (
        manifest.effective_config_hash != loaded.effective_config_hash
        or manifest.delegate_v1_config_artifact != _repo_relative(repo_root, loaded.delegate.path)
        or manifest.delegate_v1_config_sha256 != loaded.delegate.config_file_sha256
        or tranche.config_sha256 != loaded.config_file_sha256
        or tranche.effective_config_hash != loaded.effective_config_hash
        or tranche.delegate_v1_config_sha256 != loaded.delegate.config_file_sha256
        or manifest.effective_task_limit > loaded.config.task_limit
    ):
        raise LF022CodexScaleLeanCheckError("scale config pins differ from completed output")

    source_batch_path = _repo_file(
        repo_root,
        manifest.source_batch_manifest,
        manifest.source_batch_manifest_sha256,
        label="Codex scale source batch",
    )
    raw_batch = source_batch_path.read_bytes()
    try:
        source_batch = LF022PublicBatchManifest.model_validate_json(raw_batch)
    except ValueError as exc:
        raise LF022CodexScaleLeanCheckError(f"invalid scale source batch: {exc}") from exc
    canonical_batch = canonical_json_bytes(source_batch.model_dump(mode="json"))
    if raw_batch not in {canonical_batch, canonical_batch + b"\n"}:
        raise LF022CodexScaleLeanCheckError("scale source batch is not canonical JSON")
    if (
        source_batch.batch_id != tranche.source_batch_id
        or hash_file(source_batch_path) != tranche.source_batch_manifest_sha256
        or source_batch.total_task_count != manifest.available_task_count
        or not source_batch.public_sources_only
        or not source_batch.private_source_content_forbidden
        or not source_batch.outputs_provisional_only
        or source_batch.semantic_labels_created
        or source_batch.training_eligible
        or source_batch.evaluation_eligible
    ):
        raise LF022CodexScaleLeanCheckError("scale source batch policy or identity differs")
    source_bindings = _batch_task_bindings(source_batch)

    runs_root = _safe_existing_directory(scale_root / "v1_runs", label="Codex v1 runs root")
    expected_run_names = {
        sha256_hex(task_id.encode("utf-8")) for task_id in tranche.ordered_execution_task_ids
    }
    observed_run_names = {entry.name for entry in runs_root.iterdir()}
    if observed_run_names != expected_run_names:
        raise LF022CodexScaleLeanCheckError(
            "Codex scale v1 runs are partial or contain foreign entries"
        )

    verified_tasks: list[LF022CodexScaleLeanCheckTask] = []
    for scale_task in manifest.tasks:
        task_id = scale_task.execution_task_id
        source_binding = source_bindings.get(task_id)
        if source_binding is None:
            raise LF022CodexScaleLeanCheckError(
                f"scale task is absent from the source batch: {task_id}"
            )
        source_task_path = _repo_file(
            repo_root,
            source_binding.path,
            source_binding.sha256,
            label=f"frozen source task {task_id}",
        )
        run_root = _delegate_run_root(scale_root, task_id)
        try:
            prepared = _prepare_item(
                repo_root=repo_root,
                output_root=run_root,
                loaded=loaded.delegate,
                batch_manifest_path=source_batch_path,
                execution_task_id=task_id,
            )
            if prepared.item.item_id != scale_task.item_id:
                raise LF022CodexScaleLeanCheckError(
                    f"scale task item differs from replayed source: {task_id}"
                )
            validate_lf022_codex_proposer_output_root(
                run_root,
                expected_item_directory=prepared.item_directory,
            )
        except (LF022CodexProposerError, OSError, ValueError) as exc:
            if isinstance(exc, LF022CodexScaleLeanCheckError):
                raise
            raise LF022CodexScaleLeanCheckError(
                f"Codex v1 input replay failed for {task_id}: {exc}"
            ) from exc

        expected_delegate_manifest = run_root / "manifest.json"
        expected_delegate_manifest = _safe_existing_file(
            expected_delegate_manifest,
            label=f"Codex v1 manifest {task_id}",
        )
        if (
            scale_task.delegate_run_manifest
            != _repo_relative(repo_root, expected_delegate_manifest)
            or hash_file(expected_delegate_manifest) != scale_task.delegate_run_manifest_sha256
        ):
            raise LF022CodexScaleLeanCheckError(f"delegate manifest binding differs for {task_id}")
        delegate_manifest = _load_canonical(
            expected_delegate_manifest,
            model=LF022CodexProposerManifest,
            label=f"Codex v1 manifest {task_id}",
        )
        assert isinstance(delegate_manifest, LF022CodexProposerManifest)
        if (
            delegate_manifest.config_sha256 != loaded.delegate.config_file_sha256
            or delegate_manifest.effective_config_hash != loaded.delegate.effective_config_hash
            or delegate_manifest.source_batch_manifest
            != _repo_relative(repo_root, source_batch_path)
            or delegate_manifest.source_batch_manifest_sha256 != hash_file(source_batch_path)
            or delegate_manifest.requested_task_count != 1
            or delegate_manifest.completed_count != 1
            or delegate_manifest.ordered_item_ids_sha256 != hash_canonical((scale_task.item_id,))
            or delegate_manifest.status_counts != {scale_task.terminal_status: 1}
        ):
            raise LF022CodexScaleLeanCheckError(
                f"delegate manifest does not account for exactly one scale task: {task_id}"
            )

        input_record = _load_canonical(
            prepared.item_directory / "input.json",
            model=LF022CodexProposerItem,
            label=f"Codex v1 input {task_id}",
        )
        assert isinstance(input_record, LF022CodexProposerItem)
        if (
            input_record != prepared.item
            or input_record.source_execution_task_id != task_id
            or input_record.source_task_artifact != source_binding.path
            or input_record.source_task_sha256 != source_binding.sha256
        ):
            raise LF022CodexScaleLeanCheckError(
                f"delegate input differs from frozen source task: {task_id}"
            )

        expected_terminal_path = prepared.item_directory / "terminal.json"
        expected_terminal_path = _safe_existing_file(
            expected_terminal_path,
            label=f"Codex v1 terminal {task_id}",
        )
        if (
            scale_task.terminal_artifact != _repo_relative(repo_root, expected_terminal_path)
            or hash_file(expected_terminal_path) != scale_task.terminal_sha256
        ):
            raise LF022CodexScaleLeanCheckError(f"terminal binding differs for {task_id}")
        try:
            terminal = _replay_one(
                repo_root=repo_root,
                prepared=prepared,
                loaded=loaded.delegate,
            )
        except (LF022CodexProposerError, OSError, ValueError) as exc:
            raise LF022CodexScaleLeanCheckError(
                f"Codex v1 terminal replay failed for {task_id}: {exc}"
            ) from exc
        if (
            terminal.terminal_id != scale_task.terminal_id
            or terminal.status != scale_task.terminal_status
            or terminal.provisional_variant_count != scale_task.provisional_variant_count
        ):
            raise LF022CodexScaleLeanCheckError(
                f"scale terminal summary differs from replayed terminal: {task_id}"
            )
        if terminal.status == "provisional_variants_created":
            expected_variants_path = prepared.item_directory / "provisional_variants.jsonl"
            if terminal.variants_artifact != _repo_relative(repo_root, expected_variants_path):
                raise LF022CodexScaleLeanCheckError(
                    f"successful terminal redirects its variants artifact: {task_id}"
                )
        verified_tasks.append(
            LF022CodexScaleLeanCheckTask(
                execution_task_id=task_id,
                source_task_path=source_task_path,
                source_task_sha256=source_binding.sha256,
                terminal_path=expected_terminal_path,
                terminal_sha256=scale_task.terminal_sha256,
            )
        )

    artifact_hashes = _safe_tree_hashes(scale_root)
    return VerifiedLF022CodexScaleLeanCheckSelector(
        manifest=manifest,
        manifest_path=manifest_path,
        manifest_sha256=hash_file(manifest_path),
        tranche=tranche,
        tranche_path=tranche_path,
        tranche_sha256=hash_file(tranche_path),
        source_batch=source_batch,
        source_batch_path=source_batch_path,
        scale_root=scale_root,
        tasks=tuple(verified_tasks),
        artifact_hashes=artifact_hashes,
    )


__all__ = [
    "LF022CodexScaleLeanCheckError",
    "LF022CodexScaleLeanCheckTask",
    "VerifiedLF022CodexScaleLeanCheckSelector",
    "verify_lf022_codex_scale_for_lean_check",
]
