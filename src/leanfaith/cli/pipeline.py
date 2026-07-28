"""Stable Phase-2/3 pipeline commands required by Revision 4.1."""

from __future__ import annotations

import datetime
import json
import os
import shutil
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from huggingface_hub import hf_hub_download

from leanfaith.config.code_bundle import validate_code_bundle
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import load_config
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets.denylist import (
    FrozenRegistry,
    build_formalrx_test,
    build_proofnetverif,
    unresolved_benchmark,
    write_frozen_registry,
)
from leanfaith.lean.extract_run import (
    ExtractStats,
    extract_repository_files,
    extract_sft_classic_rows,
    merge_extraction_partitions,
    write_extraction_manifest,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.project_registry import (
    ContextPayload,
    ProjectSpec,
    build_context_record,
    check_project_revision,
    check_project_toolchain,
    load_environment_lock,
)
from leanfaith.representations import (
    NORMALIZATION_VERSION,
    ManualCollisionReview,
    RepresentationBatch,
    RepresentationBatchResult,
    TheoremForRepresentation,
    audit_representations,
    build_representation_batch,
    close_manual_collision_audit,
    declaration_environment_lookup_name,
)
from leanfaith.representations.views import normalize_pp_universe_placeholders
from leanfaith.schemas import (
    ArtifactClass,
    ContextRecord,
    DataStage,
    OutputManifest,
    RepresentationRecord,
    TheoremRecord,
    ValidationStatus,
    collect_code_state,
    make_id,
    new_run_id,
    write_manifest,
)
from leanfaith.sources.mathlib import build_inventory
from leanfaith.sources.mathlib_frame import (
    build_mathlib_file_frame,
    load_and_verify_mathlib_file_frame,
    mathlib_frame_additions,
    write_mathlib_file_frame,
)

FORMALRX_DATASET = "LARK-Lab/FormalRx-Test"
FORMALRX_REVISION = "4b7c6b883e0859e9bd38620a539bdcef408f91b4"
SFT_CLASSIC_REVISION = "0bf9f424309f668c2c2dd214aef6ec5d1d5c042f"
SCALE_ENVIRONMENT_SETUP_VERSION = "parent_prebuilt_v1"
DEFAULT_ENVIRONMENT_SETUP_VERSION = "backend_default_v1"


def _prepare_scale_lean_environment(
    *,
    project_dir: Path,
    context_fingerprint: str,
    raw_response_dir: Path,
    memory_hard_limit_mb: int | None,
) -> None:
    """Build the shared project and REPL once before chunk workers start."""

    LeanInteractBackend.prepare_environment(
        BackendSettings(
            project_dir=project_dir,
            context_fingerprint=context_fingerprint,
            environment_schema_version=1,
            raw_response_dir=raw_response_dir / "_environment_preflight",
            memory_hard_limit_mb=memory_hard_limit_mb,
        )
    )


def _extract_sft_chunk(
    *,
    project_dir: Path,
    context_fingerprint: str,
    context_id: str,
    raw_response_dir: Path,
    rows: list[dict[str, Any]],
    source_row_indices: list[int] | None,
    split: str,
    row_offset: int,
    out_dir: Path,
    memory_hard_limit_mb: int | None,
    job_hash: str,
    environment_is_prepared: bool = False,
) -> ExtractStats:
    """Process-safe extraction unit with one stable LeanInteract server."""

    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=project_dir,
            context_fingerprint=context_fingerprint,
            environment_schema_version=1,
            raw_response_dir=raw_response_dir,
            memory_hard_limit_mb=memory_hard_limit_mb,
            environment_is_prepared=environment_is_prepared,
        )
    )
    try:
        stats = extract_sft_classic_rows(
            backend,
            rows,
            source_revision=SFT_CLASSIC_REVISION,
            split=split,
            row_offset=row_offset,
            source_row_indices=source_row_indices,
            context_id=context_id,
            out_dir=out_dir,
        )
    finally:
        backend.close()
    _write_chunk_marker(out_dir, job_hash=job_hash, payload=stats.as_dict())
    return stats


def _write_chunk_marker(out_dir: Path, *, job_hash: str, payload: Mapping[str, object]) -> None:
    marker = out_dir / "chunk_complete.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    partial = marker.with_suffix(".json.partial")
    partial.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "job_hash": job_hash,
                "payload": payload,
                "artifacts": _chunk_artifacts(out_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(partial, marker)


def _read_chunk_marker(out_dir: Path, *, expected_job_hash: str) -> dict[str, object] | None:
    marker = out_dir / "chunk_complete.json"
    if not marker.is_file():
        return None
    value = json.loads(marker.read_text(encoding="utf-8"))
    if value.get("schema_version") != 2:
        raise ValueError(f"unsupported resume chunk marker schema: {marker}")
    if value.get("job_hash") != expected_job_hash:
        raise ValueError(f"resume chunk job hash mismatch: {marker}")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError(f"resume chunk artifacts are not an object: {marker}")
    observed_artifacts = _chunk_artifacts(out_dir)
    if artifacts != observed_artifacts:
        raise ValueError(f"resume chunk artifact integrity mismatch: {marker}")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError(f"resume chunk payload is not an object: {marker}")
    return payload


def _chunk_artifacts(out_dir: Path) -> dict[str, dict[str, int | str]]:
    """Bind a completion marker to the exact finalized JSONL partition bytes."""

    artifacts: dict[str, dict[str, int | str]] = {}
    for path in sorted(out_dir.rglob("*.jsonl")):
        relative = path.relative_to(out_dir).as_posix()
        with path.open("rb") as handle:
            rows = sum(1 for line in handle if line.strip())
        artifacts[relative] = {
            "bytes": path.stat().st_size,
            "rows": rows,
            "sha256": hash_file(path),
        }
    return artifacts


def _extract_sft_parallel(
    *,
    project_dir: Path,
    context_fingerprint: str,
    context_id: str,
    raw_response_dir: Path,
    rows: list[dict[str, Any]],
    source_row_indices: list[int] | None,
    split: str,
    row_offset: int,
    out_dir: Path,
    workers: int,
    chunk_size: int,
    run_id: str,
    memory_hard_limit_mb: int | None,
    resume_work_dir: Path | None,
    code_tree_hash: str | None,
    code_bundle_hash: str | None,
) -> ExtractStats:
    """Chunked scale path; outputs remain ordered by the frozen input."""

    if workers < 1:
        raise ValueError("chunked extraction requires at least one worker")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    persistent_work = resume_work_dir is not None
    work_root = resume_work_dir or out_dir / ".work" / f"sft_classic-{run_id}"
    work_root.mkdir(parents=True, exist_ok=persistent_work)
    jobs: list[dict[str, Any]] = []
    for chunk_index, start in enumerate(range(0, len(rows), chunk_size)):
        stop = min(start + chunk_size, len(rows))
        jobs.append(
            {
                "project_dir": project_dir,
                "context_fingerprint": context_fingerprint,
                "context_id": context_id,
                "raw_response_dir": raw_response_dir / f"chunk-{chunk_index:05d}",
                "rows": rows[start:stop],
                "source_row_indices": (
                    source_row_indices[start:stop] if source_row_indices is not None else None
                ),
                "split": split,
                "row_offset": row_offset + start,
                "out_dir": work_root / f"chunk-{chunk_index:05d}",
                "memory_hard_limit_mb": memory_hard_limit_mb,
                "environment_is_prepared": True,
                "job_hash": hash_canonical(
                    {
                        "source": "sft_classic",
                        "revision": SFT_CLASSIC_REVISION,
                        "split": split,
                        "row_offset": row_offset + start,
                        "source_row_indices": (
                            source_row_indices[start:stop]
                            if source_row_indices is not None
                            else None
                        ),
                        "rows": rows[start:stop],
                        "context_id": context_id,
                        "adapter": "extract_v2",
                        "code_tree_hash": code_tree_hash,
                        "code_bundle_hash": code_bundle_hash,
                        "memory_hard_limit_mb": memory_hard_limit_mb,
                        "leaninteract_environment_setup": SCALE_ENVIRONMENT_SETUP_VERSION,
                        "workers": workers,
                        "chunk_size": chunk_size,
                    }
                ),
            }
        )

    succeeded = False
    try:
        chunk_stats: list[ExtractStats | None] = [None] * len(jobs)
        pending: list[tuple[int, dict[str, Any]]] = []
        for index, job in enumerate(jobs):
            marker = _read_chunk_marker(
                job["out_dir"],
                expected_job_hash=job["job_hash"],
            )
            if marker is None:
                pending.append((index, job))
            else:
                chunk_stats[index] = ExtractStats.from_dict(marker)
        if pending:
            _prepare_scale_lean_environment(
                project_dir=project_dir,
                context_fingerprint=context_fingerprint,
                raw_response_dir=raw_response_dir,
                memory_hard_limit_mb=memory_hard_limit_mb,
            )
        if pending and workers == 1:
            for index, job in pending:
                chunk_stats[index] = _extract_sft_chunk(**job)
        elif pending:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                futures = [
                    (index, executor.submit(_extract_sft_chunk, **job)) for index, job in pending
                ]
                for index, future in futures:
                    chunk_stats[index] = future.result()
        if any(stats is None for stats in chunk_stats):
            raise RuntimeError("not every sft_classic extraction chunk reached a terminal marker")
        merge_extraction_partitions(
            [job["out_dir"] for job in jobs],
            out_dir=out_dir,
            source="sft_classic",
        )
        combined = ExtractStats()
        for stats in chunk_stats:
            assert stats is not None
            combined.merge(stats)
        combined.validate_accounting()
        succeeded = True
        return combined
    finally:
        if succeeded and not persistent_work:
            shutil.rmtree(work_root, ignore_errors=True)


def _extract_mathlib_chunk(
    *,
    project_dir: Path,
    context_fingerprint: str,
    context_id: str,
    raw_response_dir: Path,
    rel_paths: list[str],
    source_revision: str,
    out_dir: Path,
    memory_hard_limit_mb: int | None,
    job_hash: str,
    environment_is_prepared: bool = False,
) -> ExtractStats:
    """Process-safe mathlib file extraction unit."""

    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=project_dir,
            context_fingerprint=context_fingerprint,
            environment_schema_version=1,
            raw_response_dir=raw_response_dir,
            memory_hard_limit_mb=memory_hard_limit_mb,
            environment_is_prepared=environment_is_prepared,
        )
    )
    try:
        stats = extract_repository_files(
            backend,
            project_dir,
            rel_paths,
            source="mathlib",
            source_revision=source_revision,
            context_id=context_id,
            out_dir=out_dir,
        )
    finally:
        backend.close()
    _write_chunk_marker(out_dir, job_hash=job_hash, payload=stats.as_dict())
    return stats


def _extract_mathlib_parallel(
    *,
    project_dir: Path,
    context_fingerprint: str,
    context_id: str,
    raw_response_dir: Path,
    rel_paths: list[str],
    source_revision: str,
    out_dir: Path,
    workers: int,
    chunk_size: int,
    run_id: str,
    memory_hard_limit_mb: int | None,
    resume_work_dir: Path | None,
    code_tree_hash: str | None,
    code_bundle_hash: str | None,
) -> ExtractStats:
    """Chunked mathlib file extraction with deterministic partition order."""

    if workers < 1:
        raise ValueError("chunked extraction requires at least one worker")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")

    persistent_work = resume_work_dir is not None
    work_root = resume_work_dir or out_dir / ".work" / f"mathlib-{run_id}"
    work_root.mkdir(parents=True, exist_ok=persistent_work)
    jobs: list[dict[str, Any]] = []
    for chunk_index, start in enumerate(range(0, len(rel_paths), chunk_size)):
        jobs.append(
            {
                "project_dir": project_dir,
                "context_fingerprint": context_fingerprint,
                "context_id": context_id,
                "raw_response_dir": raw_response_dir / f"chunk-{chunk_index:05d}",
                "rel_paths": rel_paths[start : start + chunk_size],
                "source_revision": source_revision,
                "out_dir": work_root / f"chunk-{chunk_index:05d}",
                "memory_hard_limit_mb": memory_hard_limit_mb,
                "environment_is_prepared": True,
                "job_hash": hash_canonical(
                    {
                        "source": "mathlib",
                        "revision": source_revision,
                        "paths": rel_paths[start : start + chunk_size],
                        "context_id": context_id,
                        "adapter": "extract_v2",
                        "code_tree_hash": code_tree_hash,
                        "code_bundle_hash": code_bundle_hash,
                        "memory_hard_limit_mb": memory_hard_limit_mb,
                        "leaninteract_environment_setup": SCALE_ENVIRONMENT_SETUP_VERSION,
                        "workers": workers,
                        "chunk_size": chunk_size,
                    }
                ),
            }
        )
    succeeded = False
    try:
        chunk_stats: list[ExtractStats | None] = [None] * len(jobs)
        pending: list[tuple[int, dict[str, Any]]] = []
        for index, job in enumerate(jobs):
            marker = _read_chunk_marker(
                job["out_dir"],
                expected_job_hash=job["job_hash"],
            )
            if marker is None:
                pending.append((index, job))
            else:
                chunk_stats[index] = ExtractStats.from_dict(marker)
        if pending:
            _prepare_scale_lean_environment(
                project_dir=project_dir,
                context_fingerprint=context_fingerprint,
                raw_response_dir=raw_response_dir,
                memory_hard_limit_mb=memory_hard_limit_mb,
            )
        if pending and workers == 1:
            for index, job in pending:
                chunk_stats[index] = _extract_mathlib_chunk(**job)
        elif pending:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                futures = [
                    (index, executor.submit(_extract_mathlib_chunk, **job))
                    for index, job in pending
                ]
                for index, future in futures:
                    chunk_stats[index] = future.result()
        if any(stats is None for stats in chunk_stats):
            raise RuntimeError("not every mathlib extraction chunk reached a terminal marker")
        merge_extraction_partitions(
            [job["out_dir"] for job in jobs],
            out_dir=out_dir,
            source="mathlib",
        )
        combined = ExtractStats()
        for stats in chunk_stats:
            assert stats is not None
            combined.merge(stats)
        combined.validate_accounting()
        succeeded = True
        return combined
    finally:
        if succeeded and not persistent_work:
            shutil.rmtree(work_root, ignore_errors=True)


def _write_representation_partition(
    result: RepresentationBatchResult,
    *,
    out_dir: Path,
    source: str,
) -> dict[str, int]:
    """Write one ordered representation chunk and return deterministic counts."""

    record_path = out_dir / "records" / f"{source}.jsonl"
    failure_path = out_dir / "failures" / f"{source}.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        "".join(
            json.dumps(record.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) + "\n"
            for record in result.ordered_representation_records
        ),
        encoding="utf-8",
    )
    failure_path.write_text(
        "".join(
            json.dumps(asdict(failure), sort_keys=True, ensure_ascii=False) + "\n"
            for failure in result.per_theorem_failures
        ),
        encoding="utf-8",
    )
    counts: dict[str, int] = {
        "theorems": len(result.ordered_representation_records),
        "view_failures": len(result.per_theorem_failures),
    }
    for record in result.ordered_representation_records:
        for view, status in record.view_status.items():
            key = f"{view}:{status.value}"
            counts[key] = counts.get(key, 0) + 1
    return counts


def _represent_chunk(
    *,
    project_dir: Path,
    context_fingerprint: str,
    raw_response_dir: Path,
    batch: RepresentationBatch,
    created_at: datetime.datetime,
    out_dir: Path,
    source: str,
    memory_hard_limit_mb: int | None,
    job_hash: str,
    environment_is_prepared: bool = False,
) -> dict[str, int]:
    """Process-safe representation unit with one bounded Lean server lifetime."""

    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=project_dir,
            context_fingerprint=context_fingerprint,
            environment_schema_version=1,
            raw_response_dir=raw_response_dir,
            memory_hard_limit_mb=memory_hard_limit_mb,
            environment_is_prepared=environment_is_prepared,
            # Theorems remain independent LeanInteract requests. Incremental
            # mode only reuses the common import/helper prefix, which keeps
            # memory and latency bounded at Gate-3 scale. The backend detects
            # the known impossible core-environment corruption, drops the
            # poisoned REPL, and retries once on a fresh process.
            enable_incremental_optimization=True,
        )
    )
    try:
        result = build_representation_batch(backend, batch, created_at=created_at)
    finally:
        backend.close()
    counts = _write_representation_partition(result, out_dir=out_dir, source=source)
    _write_chunk_marker(out_dir, job_hash=job_hash, payload=counts)
    return counts


def _merge_representation_partitions(
    partition_dirs: list[Path],
    *,
    out_dir: Path,
    source: str,
) -> tuple[Path, Path]:
    """Concatenate chunk outputs in frozen input order without re-serialization."""

    record_path = out_dir / "records" / f"{source}.jsonl"
    failure_path = out_dir / "failures" / f"{source}.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    record_partial = record_path.with_suffix(record_path.suffix + ".partial")
    failure_partial = failure_path.with_suffix(failure_path.suffix + ".partial")
    with record_partial.open("wb") as records, failure_partial.open("wb") as failures:
        for partition in partition_dirs:
            records.write((partition / "records" / f"{source}.jsonl").read_bytes())
            failures.write((partition / "failures" / f"{source}.jsonl").read_bytes())
    os.replace(record_partial, record_path)
    os.replace(failure_partial, failure_path)
    return record_path, failure_path


def _merge_count_maps(counts: list[dict[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for chunk in counts:
        for key, value in chunk.items():
            merged[key] = merged.get(key, 0) + value
    return merged


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            rows.append(value)
    return rows


def _mathlib_spec(paths: RepoPaths) -> ProjectSpec:
    return load_config(paths.configs / "projects" / "mathlib.yaml", ProjectSpec).config


def _validate_frozen_gate3_manifest(manifest_path: Path, theorem_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("frozen Gate-3 manifest must be a JSON object")
    if manifest.get("schema_version") != 2:
        raise ValueError("frozen Gate-3 manifest must use schema_version 2")
    if manifest.get("selection_version") != "gate3_equal_source_hash_order_v1":
        raise ValueError("unsupported frozen Gate-3 selection_version")
    observed_hash = hash_file(theorem_path)
    if manifest.get("theorem_partition_sha256") != observed_hash:
        raise ValueError("frozen Gate-3 theorem partition hash mismatch")
    theorems = [
        TheoremRecord.model_validate(row.get("theorem", row)) for row in read_jsonl(theorem_path)
    ]
    theorem_ids = [theorem.theorem_id for theorem in theorems]
    if len(theorem_ids) != len(set(theorem_ids)):
        raise ValueError("frozen Gate-3 theorem partition contains duplicate theorem IDs")
    frozen_records = manifest.get("records")
    if not isinstance(frozen_records, list):
        raise ValueError("frozen Gate-3 manifest records must be a list")
    expected_records = [
        {
            "source": theorem.source,
            "theorem_id": theorem.theorem_id,
            "context_id": theorem.context_id,
            "statement_content_hash": theorem.statement_content_hash,
        }
        for theorem in theorems
    ]
    if frozen_records != expected_records:
        raise ValueError("frozen Gate-3 theorem partition records/order do not match manifest")
    if manifest.get("record_count") != len(theorem_ids):
        raise ValueError("frozen Gate-3 manifest record_count does not match partition")
    per_source = manifest.get("per_source")
    if not isinstance(per_source, int) or isinstance(per_source, bool) or per_source < 1:
        raise ValueError("frozen Gate-3 manifest per_source must be a positive integer")
    expected_source_counts = {"mathlib": per_source, "sft_classic": per_source}
    observed_source_counts = {
        source: sum(theorem.source == source for theorem in theorems)
        for source in expected_source_counts
    }
    if observed_source_counts != expected_source_counts:
        raise ValueError("frozen Gate-3 theorem partition is not exactly balanced by source")
    if manifest.get("source_counts") != expected_source_counts:
        raise ValueError("frozen Gate-3 manifest source_counts do not reconcile")
    if len(theorems) != 2 * per_source:
        raise ValueError("frozen Gate-3 manifest record_count does not equal 2 * per_source")
    contexts = {theorem.context_id for theorem in theorems}
    if len(contexts) != 1:
        raise ValueError("frozen Gate-3 theorem partition contains mixed contexts")
    context_id = next(iter(contexts))
    if manifest.get("context_id") != context_id:
        raise ValueError("frozen Gate-3 manifest context_id does not match partition")
    invalid_eligibility = [
        theorem.theorem_id
        for theorem in theorems
        if theorem.metadata.get("transform_source_eligible") is not True
        or not theorem.is_proposition
        or theorem.elaboration_status
        not in {ValidationStatus.ELABORATES, ValidationStatus.ELABORATES_WITH_PLACEHOLDER}
    ]
    if invalid_eligibility:
        raise ValueError("frozen Gate-3 theorem partition contains ineligible theorem records")
    return manifest


def build_mathlib_context(paths: RepoPaths, project_dir: Path) -> tuple[ContextRecord, str]:
    spec = _mathlib_spec(paths)
    lock = load_environment_lock(paths)
    revision = check_project_revision(spec, project_dir)
    lean_version = check_project_toolchain(spec, project_dir, lock.toolchain_lock)
    payload = ContextPayload(
        environment_schema_version=lock.environment_schema_version,
        lean_version=str(lean_version),
        lean_interact_version="0.11.4",
        repl_revision="augustepoiroux/repl@lean-interact-0.11.4",
        project_uri=spec.uri,
        project_revision=revision,
        imports=("Mathlib",),
        header_text="import Mathlib\n",
    )
    context = build_context_record(
        payload,
        project_kind=spec.kind.value,
        project_registry_key="mathlib",
    )
    context_path = paths.data / "extracted" / "contexts" / f"{context.context_fingerprint}.json"
    context_hash = write_manifest(context, context_path)
    if not (project_dir / "lean-toolchain").is_file():
        raise ValueError(f"mathlib project directory has no lean-toolchain: {project_dir}")
    return context, context_hash


def run_extract(
    *,
    paths: RepoPaths,
    source: str,
    project_dir: Path,
    input_path: Path | None,
    out_dir: Path,
    limit: int | None,
    split: str,
    row_offset: int,
    workers: int = 1,
    chunk_size: int = 500,
    memory_hard_limit_mb: int | None = None,
    code_bundle_path: Path | None = None,
    resume_work_dir: Path | None = None,
    mathlib_file_frame_path: Path | None = None,
    mathlib_frame_selection_seed: str | None = None,
    mathlib_previous_file_frame_path: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    """Execute the stable `leanfaith extract` command."""

    if source not in {"mathlib", "sft_classic"}:
        raise ValueError("MVP extraction source must be mathlib or sft_classic")
    if workers < 1:
        raise ValueError("workers must be positive")
    if source != "mathlib" and (
        mathlib_file_frame_path is not None
        or mathlib_frame_selection_seed is not None
        or mathlib_previous_file_frame_path is not None
    ):
        raise ValueError("mathlib file-frame options require source=mathlib")
    if (mathlib_file_frame_path is None) != (mathlib_frame_selection_seed is None):
        raise ValueError(
            "--mathlib-file-frame and --mathlib-frame-selection-seed must be provided together"
        )
    if mathlib_file_frame_path is not None and limit is not None:
        raise ValueError("--mathlib-file-frame and --limit are mutually exclusive")
    if mathlib_previous_file_frame_path is not None and mathlib_file_frame_path is None:
        raise ValueError("--mathlib-previous-file-frame requires --mathlib-file-frame")
    context, context_hash = build_mathlib_context(paths, project_dir)
    code = collect_code_state(paths.root)
    code_bundle_hash: str | None = None
    if code_bundle_path is not None:
        code_tree_hash = code.code_tree_hash
        if code_tree_hash is None:
            raise ValueError("code bundle validation requires a nonempty code_tree_hash")
        code_bundle_hash = validate_code_bundle(code_bundle_path, code_tree_hash)
    created_at = datetime.datetime.now(tz=datetime.UTC)
    run_id = new_run_id(created_at)
    raw_response_dir = paths.data / "raw" / "lean_extract" / run_id
    mathlib_frame_id: str | None = None
    mathlib_frame_sha256: str | None = None
    mathlib_previous_frame_id: str | None = None
    mathlib_previous_frame_sha256: str | None = None
    if source == "mathlib":
        spec = _mathlib_spec(paths)
        if mathlib_file_frame_path is None:
            inventory = build_inventory(
                project_dir,
                source="mathlib",
                revision=spec.revision,
                root_module=spec.root_module or "Mathlib",
                globs=spec.globs,
                limit=limit,
            )
            rel_paths = [entry.relative_path for entry in inventory.files]
        else:
            inventory = build_inventory(
                project_dir,
                source="mathlib",
                revision=spec.revision,
                root_module=spec.root_module or "Mathlib",
                globs=spec.globs,
            )
            if mathlib_frame_selection_seed is None:  # guarded above; narrows for mypy
                raise AssertionError("mathlib frame selection seed is missing")
            frame = load_and_verify_mathlib_file_frame(
                mathlib_file_frame_path,
                inventory=inventory,
                expected_revision=spec.revision,
                selection_seed=mathlib_frame_selection_seed,
            )
            mathlib_frame_id = frame.frame_id
            mathlib_frame_sha256 = sha256_hex(
                canonical_json_bytes(frame.model_dump(mode="json")) + b"\n"
            )
            if mathlib_previous_file_frame_path is None:
                selected_members = frame.members
            else:
                previous = load_and_verify_mathlib_file_frame(
                    mathlib_previous_file_frame_path,
                    inventory=inventory,
                    expected_revision=spec.revision,
                    selection_seed=mathlib_frame_selection_seed,
                )
                selected_members = mathlib_frame_additions(previous, frame)
                mathlib_previous_frame_id = previous.frame_id
                mathlib_previous_frame_sha256 = sha256_hex(
                    canonical_json_bytes(previous.model_dump(mode="json")) + b"\n"
                )
            rel_paths = [member.relative_path for member in selected_members]
        if workers == 1 and resume_work_dir is None:
            backend = LeanInteractBackend(
                BackendSettings(
                    project_dir=project_dir,
                    context_fingerprint=context.context_fingerprint,
                    environment_schema_version=1,
                    raw_response_dir=raw_response_dir,
                    memory_hard_limit_mb=memory_hard_limit_mb,
                )
            )
            try:
                stats = extract_repository_files(
                    backend,
                    project_dir,
                    rel_paths,
                    source="mathlib",
                    source_revision=spec.revision,
                    context_id=context.context_id,
                    out_dir=out_dir,
                )
            finally:
                backend.close()
        else:
            stats = _extract_mathlib_parallel(
                project_dir=project_dir,
                context_fingerprint=context.context_fingerprint,
                context_id=context.context_id,
                raw_response_dir=raw_response_dir,
                rel_paths=rel_paths,
                source_revision=spec.revision,
                out_dir=out_dir,
                workers=workers,
                chunk_size=chunk_size,
                run_id=run_id,
                memory_hard_limit_mb=memory_hard_limit_mb,
                resume_work_dir=resume_work_dir,
                code_tree_hash=code.code_tree_hash,
                code_bundle_hash=code_bundle_hash,
            )
        if mathlib_file_frame_path is not None:
            # Recheck the exact pinned tree and immutable frame after Lean has
            # consumed every selected file. This prevents a long extraction
            # from being finalized if checkout or frame bytes drifted during
            # execution.
            post_inventory = build_inventory(
                project_dir,
                source="mathlib",
                revision=spec.revision,
                root_module=spec.root_module or "Mathlib",
                globs=spec.globs,
            )
            post_frame = load_and_verify_mathlib_file_frame(
                mathlib_file_frame_path,
                inventory=post_inventory,
                expected_revision=spec.revision,
                selection_seed=mathlib_frame_selection_seed or "",
            )
            if post_frame != frame:
                raise ValueError("mathlib file frame changed during extraction")
            if mathlib_previous_file_frame_path is not None:
                post_previous = load_and_verify_mathlib_file_frame(
                    mathlib_previous_file_frame_path,
                    inventory=post_inventory,
                    expected_revision=spec.revision,
                    selection_seed=mathlib_frame_selection_seed or "",
                )
                if post_previous != previous:
                    raise ValueError("previous mathlib file frame changed during extraction")
        input_paths = tuple(project_dir / relative for relative in rel_paths)
        if mathlib_file_frame_path is not None:
            input_paths = (*input_paths, mathlib_file_frame_path)
        if mathlib_previous_file_frame_path is not None:
            input_paths = (*input_paths, mathlib_previous_file_frame_path)
        revision = spec.revision
    else:
        if input_path is None:
            raise ValueError("sft_classic extraction requires --input raw JSONL")
        loaded_rows = read_jsonl(input_path)
        if limit is not None:
            loaded_rows = loaded_rows[:limit]
        sampled = bool(loaded_rows) and all(
            isinstance(item.get("row"), dict) and isinstance(item.get("source_row_index"), int)
            for item in loaded_rows
        )
        rows = [dict(item["row"]) for item in loaded_rows] if sampled else loaded_rows
        source_row_indices = (
            [int(item["source_row_index"]) for item in loaded_rows] if sampled else None
        )
        if workers == 1 and resume_work_dir is None:
            backend = LeanInteractBackend(
                BackendSettings(
                    project_dir=project_dir,
                    context_fingerprint=context.context_fingerprint,
                    environment_schema_version=1,
                    raw_response_dir=raw_response_dir,
                    memory_hard_limit_mb=memory_hard_limit_mb,
                )
            )
            try:
                stats = extract_sft_classic_rows(
                    backend,
                    rows,
                    source_revision=SFT_CLASSIC_REVISION,
                    split=split,
                    row_offset=row_offset,
                    source_row_indices=source_row_indices,
                    context_id=context.context_id,
                    out_dir=out_dir,
                )
            finally:
                backend.close()
        else:
            stats = _extract_sft_parallel(
                project_dir=project_dir,
                context_fingerprint=context.context_fingerprint,
                context_id=context.context_id,
                raw_response_dir=raw_response_dir,
                rows=rows,
                source_row_indices=source_row_indices,
                split=split,
                row_offset=row_offset,
                out_dir=out_dir,
                workers=workers,
                chunk_size=chunk_size,
                run_id=run_id,
                memory_hard_limit_mb=memory_hard_limit_mb,
                resume_work_dir=resume_work_dir,
                code_tree_hash=code.code_tree_hash,
                code_bundle_hash=code_bundle_hash,
            )
        input_paths = (input_path,)
        revision = SFT_CLASSIC_REVISION
    if code_bundle_path is not None:
        input_paths = (*input_paths, code_bundle_path)
    environment_path = paths.configs / "environment.lock.yaml"
    chunked_environment_setup = workers != 1 or resume_work_dir is not None
    manifest = write_extraction_manifest(
        stats,
        source=source,
        source_revision=revision,
        run_id=run_id,
        code=code,
        out_dir=out_dir,
        root=paths.root,
        input_paths=input_paths,
        environment_hash=hash_file(environment_path),
        context_hash=context_hash,
        config_payload={
            "project_dir": str(project_dir.resolve()),
            "input_path": str(input_path.resolve()) if input_path is not None else None,
            "limit": limit,
            "split": split,
            "row_offset": row_offset,
            "workers": workers,
            "chunk_size": chunk_size,
            "memory_hard_limit_mb": memory_hard_limit_mb,
            "leaninteract_environment_setup": (
                SCALE_ENVIRONMENT_SETUP_VERSION
                if chunked_environment_setup
                else DEFAULT_ENVIRONMENT_SETUP_VERSION
            ),
            "code_bundle_sha256": code_bundle_hash,
            "resumable_chunk_markers": resume_work_dir is not None,
            "mathlib_file_frame_id": mathlib_frame_id,
            "mathlib_file_frame_sha256": mathlib_frame_sha256,
            "mathlib_previous_file_frame_id": mathlib_previous_frame_id,
            "mathlib_previous_file_frame_sha256": mathlib_previous_frame_sha256,
            "mathlib_frame_selection_seed_sha256": (
                hash_canonical(
                    {
                        "schema": "mathlib_file_frame_selection_seed_v1",
                        "selection_seed": mathlib_frame_selection_seed,
                    }
                )
                if mathlib_frame_selection_seed is not None
                else None
            ),
        },
    )
    return manifest, stats.as_dict()


def run_freeze_mathlib_file_frame(
    *,
    paths: RepoPaths,
    project_dir: Path,
    target_file_count: int,
    selection_seed: str,
    excluded_domains: tuple[str, ...],
    output_path: Path,
) -> tuple[Path, str, dict[str, object]]:
    """Freeze one replayable public mathlib extraction frame."""

    spec = _mathlib_spec(paths)
    inventory = build_inventory(
        project_dir,
        source="mathlib",
        revision=spec.revision,
        root_module=spec.root_module or "Mathlib",
        globs=spec.globs,
    )
    frame = build_mathlib_file_frame(
        inventory,
        expected_revision=spec.revision,
        target_file_count=target_file_count,
        selection_seed=selection_seed,
        excluded_domains=excluded_domains,
    )
    digest = write_mathlib_file_frame(frame, output_path)
    return (
        output_path,
        digest,
        {
            "frame_id": frame.frame_id,
            "inventory_id": frame.inventory_id,
            "inventory_file_count": frame.inventory_file_count,
            "eligible_file_count": frame.eligible_file_count,
            "excluded_file_count": frame.excluded_file_count,
            "selected_file_count": frame.selected_file_count,
            "domain_count": len(frame.domain_allocations),
        },
    )


def _formalrx_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        downloaded = hf_hub_download(
            FORMALRX_DATASET,
            "FormalRx_Test.jsonl",
            repo_type="dataset",
            revision=FORMALRX_REVISION,
        )
        path = Path(downloaded)
    return read_jsonl(path)


def run_freeze_benchmarks(
    *,
    paths: RepoPaths,
    proofnet_dir: Path,
    formalrx_jsonl: Path | None,
    frozen_at: datetime.datetime,
) -> tuple[Path, str]:
    """Freeze exact benchmark identities before any transformation work."""

    proofnet_rows: dict[str, list[dict[str, object]]] = {}
    for split in ("valid", "test"):
        converted: list[dict[str, object]] = []
        for row in read_jsonl(proofnet_dir / f"{split}.jsonl"):
            converted.append(
                {
                    "id": row["problem_id"],
                    "nl_statement": row["nl_statement"],
                    "lean4_formalization": row["reference_lean"],
                    "lean4_prediction": row["candidate_lean"],
                }
            )
        proofnet_rows[split] = converted
    proofnet_manifest = json.loads((proofnet_dir / "manifest.json").read_text())
    proofnet = build_proofnetverif(
        proofnet_rows,
        source_id="PAug/ProofNetVerif",
        revision=str(proofnet_manifest["source_revision"]),
    )
    formalrx = build_formalrx_test(
        _formalrx_rows(formalrx_jsonl),
        source_id=FORMALRX_DATASET,
        revision=FORMALRX_REVISION,
    )
    unresolved_keys = (
        "proofnet_sharp",
        "rlm25",
        "con_nf",
        "epla",
        "criticleanbench",
        "consistency_check",
        "gaokao_formal",
        "driftbench",
        "minif2f_variants",
    )
    benchmarks = [proofnet, formalrx]
    benchmarks.extend(
        unresolved_benchmark(
            key,
            "identity/revision unavailable at Revision 4.1 freeze; preserve denylist by name",
        )
        for key in unresolved_keys
    )
    registry = FrozenRegistry(
        frozen_at=frozen_at,
        benchmarks=tuple(sorted(benchmarks, key=lambda value: value.registry_key)),
    )
    path = paths.data / "benchmarks" / "frozen_ids.json"
    return path, write_frozen_registry(registry, path)


def run_represent(
    *,
    paths: RepoPaths,
    source: str,
    theorem_jsonl: Path,
    project_dir: Path,
    out_dir: Path,
    limit: int | None,
    workers: int = 1,
    chunk_size: int = 20,
    memory_hard_limit_mb: int | None = None,
    code_bundle_path: Path | None = None,
    frozen_manifest_path: Path | None = None,
    resume_work_dir: Path | None = None,
) -> tuple[Path, dict[str, int]]:
    """Build isolated per-theorem views and explicit failures."""

    if frozen_manifest_path is not None:
        _validate_frozen_gate3_manifest(
            frozen_manifest_path,
            theorem_jsonl,
        )
    source_rows = read_jsonl(theorem_jsonl)
    if limit is not None:
        source_rows = source_rows[:limit]
    theorems: list[TheoremForRepresentation] = []
    for row in source_rows:
        payload = row.get("theorem", row)
        theorem = TheoremRecord.model_validate(payload)
        full_name = theorem.declaration_full_name or theorem.declaration_name
        if not full_name:
            raise ValueError(f"theorem {theorem.theorem_id} has no declaration name")
        theorems.append(
            TheoremForRepresentation(
                theorem_id=theorem.theorem_id,
                full_name=full_name,
                proof_stripped=theorem.proof_stripped_declaration,
                context_id=theorem.context_id,
                source_signature=(
                    str(row.get("representation", {}).get("headless"))
                    if row.get("representation", {}).get("headless")
                    else None
                ),
                inline_declaration=theorem.source != "mathlib",
                inline_source=theorem.inline_elaboration_source,
                environment_lookup_name=declaration_environment_lookup_name(
                    full_name,
                    theorem.source_file if theorem.source == "mathlib" else None,
                ),
            )
        )
    if not theorems:
        raise ValueError("representation input is empty")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    contexts = {theorem.context_id for theorem in theorems}
    if len(contexts) != 1:
        raise ValueError("representation input contains mixed contexts")
    context_id = next(iter(contexts))
    context_fingerprint = context_id.removeprefix("ctx:")
    code = collect_code_state(paths.root)
    code_bundle_hash: str | None = None
    if code_bundle_path is not None:
        if code.code_tree_hash is None:
            raise ValueError("code bundle validation requires a nonempty code_tree_hash")
        code_bundle_hash = validate_code_bundle(code_bundle_path, code.code_tree_hash)
    created_at = datetime.datetime.now(tz=datetime.UTC)
    run_id = new_run_id(created_at)
    raw_response_dir = paths.data / "raw" / "lean_represent" / run_id
    persistent_work = resume_work_dir is not None
    work_root = resume_work_dir or out_dir / ".work" / f"{source}-{run_id}"
    work_root.mkdir(parents=True, exist_ok=persistent_work)
    jobs: list[dict[str, Any]] = []
    for chunk_index, start in enumerate(range(0, len(theorems), chunk_size)):
        partition = work_root / f"chunk-{chunk_index:05d}"
        jobs.append(
            {
                "project_dir": project_dir,
                "context_fingerprint": context_fingerprint,
                "raw_response_dir": raw_response_dir / f"chunk-{chunk_index:05d}",
                "batch": RepresentationBatch(
                    context_id,
                    "import Mathlib",
                    tuple(theorems[start : start + chunk_size]),
                ),
                "created_at": created_at,
                "out_dir": partition,
                "source": source,
                "memory_hard_limit_mb": memory_hard_limit_mb,
                "environment_is_prepared": True,
                "job_hash": hash_canonical(
                    {
                        "source": source,
                        "normalization_version": NORMALIZATION_VERSION,
                        "context_id": context_id,
                        "code_tree_hash": code.code_tree_hash,
                        "code_bundle_hash": code_bundle_hash,
                        "memory_hard_limit_mb": memory_hard_limit_mb,
                        "incremental_optimization": True,
                        "leaninteract_environment_setup": SCALE_ENVIRONMENT_SETUP_VERSION,
                        "workers": workers,
                        "chunk_size": chunk_size,
                        "theorems": [
                            {
                                "theorem_id": theorem.theorem_id,
                                "full_name": theorem.full_name,
                                "proof_stripped": theorem.proof_stripped,
                                "inline_source": theorem.inline_source,
                                "source_signature": theorem.source_signature,
                                "inline_declaration": theorem.inline_declaration,
                                "environment_lookup_name": theorem.environment_lookup_name,
                            }
                            for theorem in theorems[start : start + chunk_size]
                        ],
                    }
                ),
            }
        )
    succeeded = False
    try:
        chunk_counts: list[dict[str, int] | None] = [None] * len(jobs)
        pending: list[tuple[int, dict[str, Any]]] = []
        for index, job in enumerate(jobs):
            marker = _read_chunk_marker(
                job["out_dir"],
                expected_job_hash=job["job_hash"],
            )
            if marker is None:
                pending.append((index, job))
            else:
                parsed_counts: dict[str, int] = {}
                for key, value in marker.items():
                    if not isinstance(value, int):
                        raise ValueError(f"representation chunk count {key} must be an integer")
                    parsed_counts[str(key)] = value
                chunk_counts[index] = parsed_counts
        if pending:
            _prepare_scale_lean_environment(
                project_dir=project_dir,
                context_fingerprint=context_fingerprint,
                raw_response_dir=raw_response_dir,
                memory_hard_limit_mb=memory_hard_limit_mb,
            )
        if pending and workers == 1:
            for index, job in pending:
                chunk_counts[index] = _represent_chunk(**job)
        elif pending:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                futures = [
                    (index, executor.submit(_represent_chunk, **job)) for index, job in pending
                ]
                for index, future in futures:
                    chunk_counts[index] = future.result()
        if any(counts is None for counts in chunk_counts):
            raise RuntimeError("not every representation chunk reached a terminal marker")
        record_path, failure_path = _merge_representation_partitions(
            [job["out_dir"] for job in jobs],
            out_dir=out_dir,
            source=source,
        )
        succeeded = True
    finally:
        if succeeded and not persistent_work:
            shutil.rmtree(work_root, ignore_errors=True)
    completed_counts = [counts for counts in chunk_counts if counts is not None]
    status_counts = _merge_count_maps(completed_counts)
    manifest = OutputManifest(
        stage=DataStage.REPRESENTED,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id=run_id,
        source=source,
        source_revision="from_theorem_partition",
        config_hash=hash_canonical(
            {
                "representation_version": NORMALIZATION_VERSION,
                "isolation": "one_theorem_per_request",
                "workers": workers,
                "chunk_size": chunk_size,
                "memory_hard_limit_mb": memory_hard_limit_mb,
                "incremental_optimization": True,
                "leaninteract_environment_setup": SCALE_ENVIRONMENT_SETUP_VERSION,
                "code_bundle_sha256": code_bundle_hash,
                "frozen_manifest_sha256": (
                    hash_file(frozen_manifest_path) if frozen_manifest_path is not None else None
                ),
                "resumable_chunk_markers": resume_work_dir is not None,
            }
        ),
        record_schema_version=1,
        row_count=status_counts.get("theorems", 0),
        attempted_row_count=len(theorems),
        terminal_outcome_counts={
            "represented": status_counts.get("theorems", 0),
            "view_failures": status_counts.get("view_failures", 0),
        },
        file_checksums={
            str(
                record_path.relative_to(paths.root)
                if record_path.is_relative_to(paths.root)
                else record_path
            ): hash_file(record_path),
            str(
                failure_path.relative_to(paths.root)
                if failure_path.is_relative_to(paths.root)
                else failure_path
            ): hash_file(failure_path),
        },
        input_partition_checksums={
            str(theorem_jsonl): hash_file(theorem_jsonl),
            **(
                {str(code_bundle_path): hash_file(code_bundle_path)}
                if code_bundle_path is not None
                else {}
            ),
            **(
                {str(frozen_manifest_path): hash_file(frozen_manifest_path)}
                if frozen_manifest_path is not None
                else {}
            ),
        },
        output_partition_checksums={str(record_path): hash_file(record_path)},
        failure_partition_checksums={str(failure_path): hash_file(failure_path)},
        environment_hash=hash_file(paths.configs / "environment.lock.yaml"),
        context_hash=hash_canonical({"context_id": context_id}),
        code_tree_hash=code.code_tree_hash,
        code=code,
        created_at=created_at,
        notes=json.dumps(status_counts, sort_keys=True),
    )
    manifest_path = out_dir / "manifests" / f"{source}.json"
    write_manifest(manifest, manifest_path)
    return manifest_path, status_counts


def run_freeze_gate3_inputs(
    *,
    mathlib_jsonl: Path,
    sft_classic_jsonl: Path,
    out_path: Path,
    per_source: int,
) -> tuple[Path, str]:
    """Freeze equal-sized source strata and their exact theorem partition."""

    selected: list[dict[str, object]] = []
    selected_rows: list[dict[str, object]] = []
    input_accounting: dict[str, dict[str, int]] = {}
    input_contexts: set[str] = set()
    input_theorem_ids: dict[str, Path] = {}
    for source, input_path in (
        ("mathlib", mathlib_jsonl),
        ("sft_classic", sft_classic_jsonl),
    ):
        eligible: list[tuple[TheoremRecord, str | None]] = []
        seen: set[str] = set()
        input_records = 0
        for row in read_jsonl(input_path):
            theorem = TheoremRecord.model_validate(row.get("theorem", row))
            input_records += 1
            if theorem.source != source:
                raise ValueError(
                    f"{input_path} contains source {theorem.source!r}; expected {source!r}"
                )
            if theorem.theorem_id in seen:
                raise ValueError(f"duplicate theorem ID in {input_path}: {theorem.theorem_id}")
            seen.add(theorem.theorem_id)
            previous_path = input_theorem_ids.get(theorem.theorem_id)
            if previous_path is not None:
                raise ValueError(
                    f"duplicate theorem ID across {previous_path} and {input_path}: "
                    f"{theorem.theorem_id}"
                )
            input_theorem_ids[theorem.theorem_id] = input_path
            input_contexts.add(theorem.context_id)
            if theorem.metadata.get("transform_source_eligible") is True:
                if not theorem.is_proposition or theorem.elaboration_status not in {
                    ValidationStatus.ELABORATES,
                    ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
                }:
                    raise ValueError(
                        f"{input_path} marks non-elaborating/non-Prop theorem "
                        f"{theorem.theorem_id} transform-eligible"
                    )
                representation = row.get("representation")
                headless = (
                    str(representation.get("headless"))
                    if isinstance(representation, dict)
                    and representation.get("headless") is not None
                    else None
                )
                eligible.append((theorem, headless))
        eligible.sort(key=lambda item: hash_canonical({"theorem_id": item[0].theorem_id}))
        if len(eligible) < per_source:
            raise ValueError(
                f"{source} has {len(eligible)} transform-eligible theorems; "
                f"Gate 2 requires {per_source}"
            )
        chosen = eligible[:per_source]
        input_accounting[source] = {
            "input_records": input_records,
            "eligible_records": len(eligible),
            "selected_records": len(chosen),
        }
        selected_rows.extend(
            {
                "theorem": theorem.model_dump(mode="json"),
                "representation": {"headless": headless} if headless is not None else {},
            }
            for theorem, headless in chosen
        )
        selected.extend(
            {
                "source": source,
                "theorem_id": theorem.theorem_id,
                "context_id": theorem.context_id,
                "statement_content_hash": theorem.statement_content_hash,
            }
            for theorem, _ in chosen
        )
    if len(input_contexts) != 1:
        raise ValueError("Gate-3 freeze inputs contain mixed contexts")
    context_id = next(iter(input_contexts))
    expected_record_count = 2 * per_source
    if len(selected) != expected_record_count or len(selected_rows) != expected_record_count:
        raise AssertionError("Gate-3 equal-source selection did not reconcile")
    source_counts = {
        source: sum(record["source"] == source for record in selected)
        for source in ("mathlib", "sft_classic")
    }
    if source_counts != {"mathlib": per_source, "sft_classic": per_source}:
        raise AssertionError("Gate-3 selected source counts did not reconcile")
    theorem_partition = out_path.with_suffix(".theorems.jsonl")
    theorem_partition.parent.mkdir(parents=True, exist_ok=True)
    theorem_partition.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in selected_rows
        ),
        encoding="utf-8",
    )
    payload = {
        "schema_version": 2,
        "selection_version": "gate3_equal_source_hash_order_v1",
        "per_source": per_source,
        "record_count": len(selected),
        "source_counts": source_counts,
        "context_id": context_id,
        "input_accounting": input_accounting,
        "theorem_partition": str(theorem_partition),
        "theorem_partition_sha256": hash_file(theorem_partition),
        "input_checksums": {
            str(mathlib_jsonl): hash_file(mathlib_jsonl),
            str(sft_classic_jsonl): hash_file(sft_classic_jsonl),
        },
        "records": selected,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return out_path, hash_file(out_path)


def _alpha_property_sources(index: int) -> tuple[str, str]:
    """Return one source pair differing only in local binder names.

    This is a Gate-3 serializer property probe. It is deliberately separate
    from the P01 training transformation family and is never released as a
    model example.
    """

    left_name = f"lf_alpha_property_left_{index:04d}"
    right_name = f"lf_alpha_property_right_{index:04d}"
    shape = index % 5
    if shape == 0:
        left = f"(x{index} : Nat) (hx{index} : x{index} = x{index}) : x{index} = x{index}"
        right = f"(y{index} : Nat) (hy{index} : y{index} = y{index}) : y{index} = y{index}"
    elif shape == 1:
        left = f"(f{index} : Nat → Nat) (x{index} : Nat) : f{index} x{index} = f{index} x{index}"
        right = f"(g{index} : Nat → Nat) (y{index} : Nat) : g{index} y{index} = g{index} y{index}"
    elif shape == 2:
        left = f"{{a{index} : Type}} (x{index} : a{index}) : x{index} = x{index}"
        right = f"{{b{index} : Type}} (y{index} : b{index}) : y{index} = y{index}"
    elif shape == 3:
        left = (
            f"{{a{index} : Type}} [Inhabited a{index}] (x{index} : a{index}) : x{index} = x{index}"
        )
        right = (
            f"{{b{index} : Type}} [Inhabited b{index}] (y{index} : b{index}) : y{index} = y{index}"
        )
    else:
        left = f"(p{index} q{index} : Prop) : p{index} ∧ q{index} → q{index} ∧ p{index}"
        right = f"(r{index} s{index} : Prop) : r{index} ∧ s{index} → s{index} ∧ r{index}"
    return (
        f"theorem {left_name} {left} := by sorry",
        f"theorem {right_name} {right} := by sorry",
    )


def run_alpha_invariance_audit(
    *,
    paths: RepoPaths,
    project_dir: Path,
    out_path: Path,
    cases: int = 1000,
    workers: int = 1,
    chunk_size: int = 20,
    memory_hard_limit_mb: int | None = None,
    code_bundle_path: Path | None = None,
    resume_work_dir: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    """Elaborate synthetic alpha pairs and require fingerprint invariance."""

    if cases < 1:
        raise ValueError("alpha-invariance audit requires at least one case")
    context, _ = build_mathlib_context(paths, project_dir)
    theorem_path = out_path.with_suffix(".theorems.jsonl")
    theorem_path.parent.mkdir(parents=True, exist_ok=True)
    theorem_records: list[TheoremRecord] = []
    pair_ids: list[tuple[str, str]] = []
    for index in range(cases):
        sources = _alpha_property_sources(index)
        ids: list[str] = []
        for side, source in zip(("left", "right"), sources, strict=True):
            theorem_id = make_id(
                "thm", {"audit": "alpha_property_v1", "index": index, "side": side}
            )
            ancestry_id = make_id(
                "anc", {"audit": "alpha_property_v1", "index": index, "side": side}
            )
            name = f"lf_alpha_property_{side}_{index:04d}"
            theorem_records.append(
                TheoremRecord(
                    theorem_id=theorem_id,
                    ancestry_id=ancestry_id,
                    root_ancestry_ids=(ancestry_id,),
                    source="alpha_property_audit",
                    source_revision="alpha_property_renamer_v1",
                    context_id=context.context_id,
                    declaration_kind="theorem",
                    declaration_name=name,
                    declaration_full_name=name,
                    proof_stripped_declaration=source,
                    is_proposition=True,
                    elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
                    statement_content_hash=hash_canonical({"source": source}),
                    metadata={
                        "transform_source_eligible": False,
                        "audit_only": True,
                        "alpha_case": index,
                    },
                )
            )
            ids.append(theorem_id)
        pair_ids.append((ids[0], ids[1]))
    theorem_path.write_text(
        "".join(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
            for record in theorem_records
        ),
        encoding="utf-8",
    )
    representation_dir = out_path.parent / f"{out_path.stem}_representations"
    representation_manifest, _ = run_represent(
        paths=paths,
        source="alpha_property_audit",
        theorem_jsonl=theorem_path,
        project_dir=project_dir,
        out_dir=representation_dir,
        limit=None,
        workers=workers,
        chunk_size=chunk_size,
        memory_hard_limit_mb=memory_hard_limit_mb,
        code_bundle_path=code_bundle_path,
        resume_work_dir=resume_work_dir,
    )
    representation_path = representation_dir / "records" / "alpha_property_audit.jsonl"
    records = {
        record.theorem_id: record
        for record in (
            RepresentationRecord.model_validate(row) for row in read_jsonl(representation_path)
        )
    }
    failures: list[dict[str, object]] = []
    for index, (left_id, right_id) in enumerate(pair_ids):
        left = records.get(left_id)
        right = records.get(right_id)
        if left is None or right is None:
            failures.append({"case": index, "reason": "missing_representation"})
        elif left.alpha_identity_fingerprint is None or right.alpha_identity_fingerprint is None:
            failures.append({"case": index, "reason": "missing_fingerprint"})
        elif left.alpha_identity_fingerprint != right.alpha_identity_fingerprint:
            failures.append(
                {
                    "case": index,
                    "reason": "fingerprint_mismatch",
                    "left": left.alpha_identity_fingerprint,
                    "right": right.alpha_identity_fingerprint,
                }
            )
    report: dict[str, object] = {
        "schema_version": 1,
        "audit_version": "alpha_property_renamer_v1",
        "artifact_class": "audit_only",
        "training_eligible": False,
        "cases": cases,
        "passed": cases - len(failures),
        "failures": failures,
        "mechanical_pass": not failures,
        "input_sha256": hash_file(theorem_path),
        "representation_sha256": hash_file(representation_path),
        "representation_manifest_sha256": hash_file(representation_manifest),
    }
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    report["report_sha256"] = hash_file(out_path)
    return out_path, report


_CROSS_PATH_AUDIT_VERSION = "cross_path_exact_type_alias_v3"
_CROSS_PATH_SELECTION_VERSION = "cross_path_parser_addressable_public_v1"


def _inline_alias_of(name: str, original_full_name: str) -> str:
    """Declare an audit-only alias whose type Lean infers from the original.

    The explicit pretty view is not a lossless Lean serialization. An inferred
    alias exercises inline declaration and inspection without round-tripping
    proof elisions or other pretty-printer-only syntax.
    """

    return f"noncomputable def {name} := @{original_full_name}"


def _cross_path_exclusion_reason(
    theorem: TheoremRecord,
    named: RepresentationRecord | None,
) -> str | None:
    """Return why a frozen mathlib theorem cannot enter the cross-path panel."""

    full_name = (theorem.declaration_full_name or "").strip()
    if not full_name:
        return "missing_declaration_full_name"
    if full_name.startswith("_private."):
        # Lean stores source-qualified private constants in the environment, but
        # their rendered ``_private.0.*`` names are not parser-addressable.
        return "environment_only_private_name"
    if named is None:
        return "missing_named_representation"
    if named.normalization_version != NORMALIZATION_VERSION:
        return "named_normalization_version_mismatch"
    if named.signature_explicit is None:
        return "missing_named_explicit_signature"
    if named.alpha_identity_fingerprint is None:
        return "missing_named_alpha_identity_fingerprint"
    return None


def _select_cross_path_inputs(
    frozen_mathlib: list[TheoremRecord],
    named_records: Mapping[str, RepresentationRecord],
    *,
    cases: int,
) -> tuple[list[TheoremRecord], list[dict[str, object]]]:
    """Select a deterministic public panel and account for every other input."""

    selected: list[TheoremRecord] = []
    exclusions: list[dict[str, object]] = []
    for ordinal, theorem in enumerate(frozen_mathlib):
        reason = _cross_path_exclusion_reason(
            theorem,
            named_records.get(theorem.theorem_id),
        )
        if reason is None and len(selected) < cases:
            selected.append(theorem)
            continue
        exclusions.append(
            {
                "mathlib_input_ordinal": ordinal,
                "theorem_id": theorem.theorem_id,
                "declaration_full_name": theorem.declaration_full_name,
                "reason": reason or "case_limit_reached",
            }
        )
    return selected, exclusions


def run_cross_path_audit(
    *,
    paths: RepoPaths,
    project_dir: Path,
    theorem_jsonl: Path,
    representation_jsonl: Path,
    out_path: Path,
    cases: int = 500,
    workers: int = 1,
    chunk_size: int = 20,
    memory_hard_limit_mb: int | None = None,
    code_bundle_path: Path | None = None,
    resume_work_dir: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    """Inspect audit-only exact-type inline aliases and compare identities."""

    if cases < 1:
        raise ValueError("cross-path audit requires at least one case")
    frozen_mathlib = [
        TheoremRecord.model_validate(row.get("theorem", row))
        for row in read_jsonl(theorem_jsonl)
        if str(row.get("source", row.get("theorem", {}).get("source", ""))) == "mathlib"
    ]
    named_records = {
        record.theorem_id: record
        for record in (
            RepresentationRecord.model_validate(row) for row in read_jsonl(representation_jsonl)
        )
    }
    selected, selection_exclusions = _select_cross_path_inputs(
        frozen_mathlib,
        named_records,
        cases=cases,
    )
    exclusion_reason_counts: dict[str, int] = {}
    for exclusion in selection_exclusions:
        reason = str(exclusion["reason"])
        exclusion_reason_counts[reason] = exclusion_reason_counts.get(reason, 0) + 1
    selection_path = out_path.with_suffix(".selection.json")
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    selection_report: dict[str, object] = {
        "schema_version": 1,
        "selection_version": _CROSS_PATH_SELECTION_VERSION,
        "requested_cases": cases,
        "input_mathlib_count": len(frozen_mathlib),
        "selected_count": len(selected),
        "selected_theorem_ids": [theorem.theorem_id for theorem in selected],
        "selected_declaration_full_names": [theorem.declaration_full_name for theorem in selected],
        "exclusion_count": len(selection_exclusions),
        "exclusion_reason_counts": dict(sorted(exclusion_reason_counts.items())),
        "exclusions": selection_exclusions,
        "theorem_input_sha256": hash_file(theorem_jsonl),
        "named_representation_sha256": hash_file(representation_jsonl),
    }
    selection_path.write_text(
        json.dumps(
            selection_report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    if len(selected) < cases:
        raise ValueError(
            "cross-path audit requires "
            f"{cases} parser-addressable public mathlib inputs; selected {len(selected)} "
            f"from {len(frozen_mathlib)}; selection={selection_path}"
        )
    context, _ = build_mathlib_context(paths, project_dir)
    inline_theorem_path = out_path.with_suffix(".theorems.jsonl")
    inline_records: list[TheoremRecord] = []
    pair_ids: list[tuple[str, str]] = []
    setup_failures: list[dict[str, object]] = []
    for index, original in enumerate(selected):
        named = named_records.get(original.theorem_id)
        if named is None or named.signature_explicit is None:
            setup_failures.append(
                {
                    "case": index,
                    "original_theorem_id": original.theorem_id,
                    "reason": "missing_named_explicit_signature",
                }
            )
            continue
        inline_name = f"lf_cross_path_{index:04d}"
        original_full_name = original.declaration_full_name
        if original_full_name is None:
            raise AssertionError("cross-path selection admitted a nameless theorem")
        source = _inline_alias_of(inline_name, original_full_name)
        theorem_id = make_id(
            "thm", {"audit": _CROSS_PATH_AUDIT_VERSION, "original": original.theorem_id}
        )
        ancestry_id = make_id(
            "anc", {"audit": _CROSS_PATH_AUDIT_VERSION, "original": original.theorem_id}
        )
        inline_records.append(
            TheoremRecord(
                theorem_id=theorem_id,
                ancestry_id=ancestry_id,
                root_ancestry_ids=(ancestry_id,),
                source="cross_path_audit",
                source_revision=_CROSS_PATH_AUDIT_VERSION,
                context_id=context.context_id,
                declaration_kind="def",
                declaration_name=inline_name,
                declaration_full_name=inline_name,
                proof_stripped_declaration=source,
                inline_elaboration_source=source,
                is_proposition=True,
                elaboration_status=ValidationStatus.ELABORATES,
                statement_content_hash=hash_canonical({"source": source}),
                metadata={
                    "transform_source_eligible": False,
                    "audit_only": True,
                    "artifact_class": "audit_only",
                    "cross_path_construction": "environment_inferred_alias_v1",
                    "original_theorem_id": original.theorem_id,
                },
            )
        )
        pair_ids.append((original.theorem_id, theorem_id))
    inline_theorem_path.parent.mkdir(parents=True, exist_ok=True)
    inline_theorem_path.write_text(
        "".join(
            json.dumps(
                {
                    "theorem": record.model_dump(mode="json"),
                    "representation": {
                        "headless": named_records[
                            str(record.metadata["original_theorem_id"])
                        ].headless
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
            for record in inline_records
        ),
        encoding="utf-8",
    )
    comparison_failures = list(setup_failures)
    representation_manifest: Path | None = None
    inline_representation_path: Path | None = None
    raw_explicit_equal = 0
    normalized_explicit_equal = 0
    if inline_records:
        representation_dir = out_path.parent / f"{out_path.stem}_representations"
        representation_manifest, _ = run_represent(
            paths=paths,
            source="cross_path_audit",
            theorem_jsonl=inline_theorem_path,
            project_dir=project_dir,
            out_dir=representation_dir,
            limit=None,
            workers=workers,
            chunk_size=chunk_size,
            memory_hard_limit_mb=memory_hard_limit_mb,
            code_bundle_path=code_bundle_path,
            resume_work_dir=resume_work_dir,
        )
        inline_representation_path = representation_dir / "records" / "cross_path_audit.jsonl"
        inline_by_id = {
            record.theorem_id: record
            for record in (
                RepresentationRecord.model_validate(row)
                for row in read_jsonl(inline_representation_path)
            )
        }
        for index, (original_id, inline_id) in enumerate(pair_ids):
            named = named_records[original_id]
            inline = inline_by_id.get(inline_id)
            if inline is None:
                comparison_failures.append(
                    {
                        "case": index,
                        "original_theorem_id": original_id,
                        "reason": "missing_inline_representation",
                    }
                )
            elif inline.alpha_identity_fingerprint is None:
                comparison_failures.append(
                    {
                        "case": index,
                        "original_theorem_id": original_id,
                        "reason": "missing_inline_alpha_identity_fingerprint",
                    }
                )
            elif inline.signature_explicit is None:
                comparison_failures.append(
                    {
                        "case": index,
                        "original_theorem_id": original_id,
                        "reason": "missing_inline_explicit_signature",
                    }
                )
            else:
                if named.signature_explicit == inline.signature_explicit:
                    raw_explicit_equal += 1
                assert named.signature_explicit is not None
                named_explicit = normalize_pp_universe_placeholders(named.signature_explicit)
                inline_explicit = normalize_pp_universe_placeholders(inline.signature_explicit)
                if named_explicit == inline_explicit:
                    normalized_explicit_equal += 1
                if named.alpha_identity_fingerprint != inline.alpha_identity_fingerprint:
                    comparison_failures.append(
                        {
                            "case": index,
                            "original_theorem_id": original_id,
                            "reason": "alpha_fingerprint_mismatch",
                        }
                    )
                elif named_explicit != inline_explicit:
                    comparison_failures.append(
                        {
                            "case": index,
                            "original_theorem_id": original_id,
                            "reason": "normalized_explicit_signature_mismatch",
                        }
                    )
    report: dict[str, object] = {
        "schema_version": 1,
        "audit_version": _CROSS_PATH_AUDIT_VERSION,
        "selection_version": _CROSS_PATH_SELECTION_VERSION,
        "artifact_class": "audit_only",
        "training_eligible": False,
        "construction": "environment_inferred_alias_v1",
        "normalization_version": NORMALIZATION_VERSION,
        "explicit_comparison": "first_occurrence_u_number_normalization_v1",
        "raw_explicit_equal": raw_explicit_equal,
        "normalized_explicit_equal": normalized_explicit_equal,
        "cases": cases,
        "passed": cases - len(comparison_failures),
        "failures": comparison_failures,
        "mechanical_pass": not comparison_failures and len(pair_ids) == cases,
        "selected_theorem_ids": [theorem.theorem_id for theorem in selected],
        "selection_exclusion_count": len(selection_exclusions),
        "selection_exclusion_reason_counts": dict(sorted(exclusion_reason_counts.items())),
        "selection_manifest": str(selection_path),
        "selection_manifest_sha256": hash_file(selection_path),
        "theorem_input_sha256": hash_file(theorem_jsonl),
        "named_representation_sha256": hash_file(representation_jsonl),
        "inline_theorem_sha256": hash_file(inline_theorem_path),
        "inline_representation_sha256": (
            hash_file(inline_representation_path)
            if inline_representation_path is not None
            else None
        ),
        "representation_manifest_sha256": (
            hash_file(representation_manifest) if representation_manifest is not None else None
        ),
    }
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    report["report_sha256"] = hash_file(out_path)
    return out_path, report


def run_audit_representations(
    *,
    representation_jsonl: Path,
    theorem_jsonl: Path,
    out_path: Path,
    failure_jsonl: Path | None = None,
    frozen_manifest_path: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    """Audit a frozen representation partition without denominator filtering."""

    frozen_manifest: dict[str, Any] | None = None
    if frozen_manifest_path is not None:
        frozen_manifest = _validate_frozen_gate3_manifest(
            frozen_manifest_path,
            theorem_jsonl,
        )
    records = tuple(
        RepresentationRecord.model_validate(row) for row in read_jsonl(representation_jsonl)
    )
    source_by_theorem: dict[str, str] = {}
    expected_context_by_theorem: dict[str, str] = {}
    expected_raw_by_theorem: dict[str, str] = {}
    expected_headless_by_theorem: dict[str, str] = {}
    expected_ids: set[str] = set()
    for row in read_jsonl(theorem_jsonl):
        theorem = TheoremRecord.model_validate(row.get("theorem", row))
        expected_ids.add(theorem.theorem_id)
        source_by_theorem[theorem.theorem_id] = theorem.source
        expected_context_by_theorem[theorem.theorem_id] = theorem.context_id
        expected_raw_by_theorem[theorem.theorem_id] = theorem.proof_stripped_declaration
        representation = row.get("representation")
        if isinstance(representation, dict) and isinstance(representation.get("headless"), str):
            expected_headless_by_theorem[theorem.theorem_id] = representation["headless"]
    actual_ids = {record.theorem_id for record in records}
    missing = sorted(expected_ids - actual_ids)
    unexpected = sorted(actual_ids - expected_ids)
    failure_keys: set[tuple[str, str]] | None = None
    duplicate_failure_keys: list[str] = []
    if failure_jsonl is not None:
        failure_keys = set()
        for row in read_jsonl(failure_jsonl):
            key = (str(row.get("theorem_id", "")), str(row.get("view", "")))
            if key in failure_keys:
                duplicate_failure_keys.append(f"{key[0]}:{key[1]}")
            failure_keys.add(key)
    report = audit_representations(
        records,
        source_by_theorem=source_by_theorem,
        failure_keys=failure_keys,
        expected_context_by_theorem=expected_context_by_theorem,
        expected_raw_by_theorem=expected_raw_by_theorem,
        expected_headless_by_theorem=expected_headless_by_theorem,
    )
    report["input_manifest_count"] = len(expected_ids)
    report["missing_theorem_ids"] = missing
    report["unexpected_theorem_ids"] = unexpected
    report["duplicate_failure_keys"] = sorted(duplicate_failure_keys)
    report["input_checksums"] = {
        str(representation_jsonl): hash_file(representation_jsonl),
        str(theorem_jsonl): hash_file(theorem_jsonl),
        **({str(failure_jsonl): hash_file(failure_jsonl)} if failure_jsonl is not None else {}),
        **(
            {str(frozen_manifest_path): hash_file(frozen_manifest_path)}
            if frozen_manifest_path is not None
            else {}
        ),
    }
    report["frozen_manifest_bound"] = frozen_manifest is not None
    report["mechanical_pass"] = (
        bool(report["mechanical_pass"])
        and not missing
        and not unexpected
        and not duplicate_failure_keys
    )
    report["gate_pass"] = (
        bool(report["mechanical_pass"]) and report.get("manual_audit_status") == "not_required"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return out_path, report


def run_close_manual_collision_audit(
    *,
    mechanical_report_path: Path,
    review_jsonl: Path,
    out_path: Path,
) -> tuple[Path, dict[str, object]]:
    """Validate terminal human reviews against the exact mechanical sample."""

    mechanical_report = json.loads(mechanical_report_path.read_text(encoding="utf-8"))
    if not isinstance(mechanical_report, dict):
        raise ValueError("mechanical representation audit must be a JSON object")
    reviews = tuple(ManualCollisionReview.model_validate(row) for row in read_jsonl(review_jsonl))
    report = close_manual_collision_audit(mechanical_report, reviews)
    report["input_checksums"] = {
        str(mechanical_report_path): hash_file(mechanical_report_path),
        str(review_jsonl): hash_file(review_jsonl),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    report["report_sha256"] = hash_file(out_path)
    return out_path, report


def default_mathlib_checkout() -> Path:
    return Path(os.environ.get("LEANFAITH_MATHLIB_DIR", "/storage/milikic/leanfaith/mathlib4"))
