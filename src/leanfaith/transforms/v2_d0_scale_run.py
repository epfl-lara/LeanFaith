"""Resumable persisted scale runner for LF-034's experimental N11 profile."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.v2_d0_materializer import (
    D0ProfileId,
    D0RuleId,
    V2D0MaterializationResult,
)
from leanfaith.transforms.v2_d0_n12_runtime import V2D0N12Runtime
from leanfaith.transforms.v2_d0_runtime import V2D0Runtime
from leanfaith.transforms.v2_d0_scale import (
    V2D0MaterializationInput,
    materialize_v2_d0_batch,
)
from leanfaith.transforms.v2_e0_scale_run import (
    V2E0ScaleRunError,
    _canonical_line,
    _iter_jsonl,
    _write_immutable,
)

_HEX64 = r"^[0-9a-f]{64}$"


class V2D0ScaleRunError(RuntimeError):
    """A persisted N11 run violated its exact replay contract."""


class V2D0ScaleRunSpec(StrictModel):
    schema_version: Literal[2] = 2
    artifact_kind: Literal["deterministic_v2_d0_scale_run_spec"] = (
        "deterministic_v2_d0_scale_run_spec"
    )
    profile_id: D0ProfileId
    profile_config_hash: str = Field(pattern=_HEX64)
    rule_id: D0RuleId
    theorem_partition: str
    theorem_partition_sha256: str = Field(pattern=_HEX64)
    representation_partition: str
    representation_partition_sha256: str = Field(pattern=_HEX64)
    project_dir: str
    context_id: str
    import_header_sha256: str = Field(pattern=_HEX64)
    base_seed: int
    batch_size: int = Field(ge=1)
    max_sources: int | None = Field(default=None, ge=1)
    source_count: int = Field(ge=1)
    attempt_count: int = Field(ge=1)
    ordered_theorem_ids_sha256: str = Field(pattern=_HEX64)
    ordered_attempt_keys_sha256: str = Field(pattern=_HEX64)
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False


class V2D0ScaleRunManifest(StrictModel):
    schema_version: Literal[2] = 2
    artifact_kind: Literal["deterministic_v2_d0_scale_manifest"] = (
        "deterministic_v2_d0_scale_manifest"
    )
    run_spec_sha256: str = Field(pattern=_HEX64)
    batch_count: int = Field(ge=1)
    result_count: int = Field(ge=1)
    terminal_status_counts: dict[str, int]
    family_status_counts: dict[str, int]
    journal_tree_hash: str = Field(pattern=_HEX64)
    results_sha256: str = Field(pattern=_HEX64)
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _reconciles(self) -> V2D0ScaleRunManifest:
        if sum(self.terminal_status_counts.values()) != self.result_count:
            raise ValueError("terminal status counts do not reconcile")
        if sum(self.family_status_counts.values()) != self.result_count:
            raise ValueError("family status counts do not reconcile")
        return self


@dataclass(frozen=True, slots=True)
class V2D0ScaleRunArtifacts:
    output_dir: Path
    run_spec_path: Path
    manifest_path: Path
    results_path: Path
    result_count: int


def _seed(base_seed: int, theorem_id: str, rule_id: D0RuleId) -> int:
    payload = canonical_json_bytes(
        {
            "schema": "deterministic_v2_d0_scale_seed_v1",
            "base_seed": base_seed,
            "theorem_id": theorem_id,
            "rule_id": rule_id,
        }
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=False)


def _ordered_attempts(
    aligned: Sequence[tuple[TheoremRecord, RepresentationRecord]],
    runtime: V2D0Runtime | V2D0N12Runtime,
    *,
    base_seed: int,
) -> tuple[V2D0MaterializationInput, ...]:
    if len(runtime.rule_ids) != 1:
        raise V2D0ScaleRunError("persisted D0 profiles must contain exactly one rule")
    rule_id = runtime.rule_ids[0]
    return tuple(
        V2D0MaterializationInput(
            theorem=theorem,
            representation=representation,
            rule_id=rule_id,
            seed=_seed(base_seed, theorem.theorem_id, rule_id),
        )
        for theorem, representation in aligned
    )


def _iter_aligned_inputs(
    theorem_path: Path,
    representation_path: Path,
    *,
    max_sources: int | None,
) -> Iterator[tuple[TheoremRecord, RepresentationRecord]]:
    """Stream canonical aligned partitions with bounded live memory."""

    try:
        theorem_iter = _iter_jsonl(theorem_path, TheoremRecord, wrapper_key="theorem")
        representation_iter = _iter_jsonl(
            representation_path,
            RepresentationRecord,
        )
        seen: set[str] = set()
        count = 0
        context_id: str | None = None
        while max_sources is None or count < max_sources:
            try:
                theorem = next(theorem_iter)
            except StopIteration:
                try:
                    next(representation_iter)
                except StopIteration:
                    return
                raise V2D0ScaleRunError("representation partition contains extra records") from None
            try:
                representation = next(representation_iter)
            except StopIteration:
                raise V2D0ScaleRunError(
                    "theorem partition contains records without representations"
                ) from None
            if theorem.theorem_id != representation.theorem_id:
                raise V2D0ScaleRunError(
                    "streaming theorem/representation order mismatch: "
                    f"{theorem.theorem_id} != {representation.theorem_id}"
                )
            if theorem.theorem_id in seen:
                raise V2D0ScaleRunError(
                    f"duplicate theorem ID in selected inventory: {theorem.theorem_id}"
                )
            seen.add(theorem.theorem_id)
            if theorem.context_id != representation.context_id:
                raise V2D0ScaleRunError(f"source context mismatch for {theorem.theorem_id}")
            if context_id is None:
                context_id = theorem.context_id
            elif theorem.context_id != context_id:
                raise V2D0ScaleRunError("v2 D0 scale input must have exactly one Lean context")
            count += 1
            yield theorem, representation
    except V2E0ScaleRunError as exc:
        raise V2D0ScaleRunError(str(exc)) from exc


def _inventory(
    theorem_path: Path,
    representation_path: Path,
    *,
    runtime: V2D0Runtime | V2D0N12Runtime,
    base_seed: int,
    max_sources: int | None,
) -> tuple[int, str, str, str]:
    theorem_ids: list[str] = []
    attempt_keys: list[tuple[str, str, D0RuleId, int]] = []
    context_id: str | None = None
    for theorem, representation in _iter_aligned_inputs(
        theorem_path,
        representation_path,
        max_sources=max_sources,
    ):
        if context_id is None:
            context_id = theorem.context_id
        theorem_ids.append(theorem.theorem_id)
        rule_id = runtime.rule_ids[0]
        attempt_keys.append(
            (
                theorem.theorem_id,
                representation.representation_id,
                rule_id,
                _seed(base_seed, theorem.theorem_id, rule_id),
            )
        )
    if context_id is None:
        raise V2D0ScaleRunError("selected D0 source inventory is empty")
    return (
        len(theorem_ids),
        context_id,
        hash_canonical(theorem_ids),
        hash_canonical(attempt_keys),
    )


def _write_d0_immutable(path: Path, payload: bytes) -> str:
    try:
        return _write_immutable(path, payload)
    except V2E0ScaleRunError as exc:
        raise V2D0ScaleRunError(str(exc)) from exc


def _batch_payload(results: Sequence[V2D0MaterializationResult]) -> bytes:
    return b"".join(_canonical_line(item) for item in results)


def _load_batch(
    path: Path,
    expected: Sequence[V2D0MaterializationInput],
    runtime: V2D0Runtime | V2D0N12Runtime,
) -> tuple[V2D0MaterializationResult, ...]:
    try:
        results = tuple(_iter_jsonl(path, V2D0MaterializationResult))
    except V2E0ScaleRunError as exc:
        raise V2D0ScaleRunError(str(exc)) from exc
    if len(results) != len(expected):
        raise V2D0ScaleRunError(f"resume batch cardinality mismatch: {path}")
    for result, item in zip(results, expected, strict=True):
        if (
            result.profile_id != runtime.loaded.config.profile_id
            or result.profile_config_hash != runtime.generation_config_hash
            or result.rule_id != item.rule_id
            or result.attempt.source_theorem_ids != (item.theorem.theorem_id,)
            or result.attempt.source_representation_ids != (item.representation.representation_id,)
            or result.attempt.seed != item.seed
        ):
            raise V2D0ScaleRunError(f"resume batch does not bind expected attempt: {path}")
    return results


def _combined_hash_and_size(paths: Sequence[Path]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for source in paths:
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    return digest.hexdigest(), size


def _assemble_results(path: Path, journal_paths: Sequence[Path]) -> str:
    """Atomically concatenate journals without materializing all result bytes."""

    expected_hash, expected_size = _combined_hash_and_size(journal_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != expected_size
            or hash_file(path) != expected_hash
        ):
            raise V2D0ScaleRunError(f"immutable artifact conflict: {path}")
        return expected_hash
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            for source in journal_paths:
                with source.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if temporary.stat().st_size != expected_size or hash_file(temporary) != expected_hash:
            raise V2D0ScaleRunError("streamed result assembly hash mismatch")
        try:
            os.link(temporary, path)
        except FileExistsError:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != expected_size
                or hash_file(path) != expected_hash
            ):
                raise V2D0ScaleRunError(f"concurrent immutable conflict: {path}") from None
        return expected_hash
    finally:
        temporary.unlink(missing_ok=True)


def run_v2_d0_scale(
    *,
    backend: LeanInteractBackend,
    runtime: V2D0Runtime | V2D0N12Runtime,
    theorem_path: Path,
    representation_path: Path,
    project_dir: Path,
    import_header: str,
    output_dir: Path,
    batch_size: int = 128,
    base_seed: int = 0,
    max_sources: int | None = None,
) -> V2D0ScaleRunArtifacts:
    """Stream, run, or resume one exact D0 inventory through pooled LeanInteract."""

    if batch_size < 1:
        raise V2D0ScaleRunError("batch_size must be positive")
    if max_sources is not None and max_sources < 1:
        raise V2D0ScaleRunError("max_sources must be positive")
    theorem_path = theorem_path.resolve(strict=True)
    representation_path = representation_path.resolve(strict=True)
    project_dir = project_dir.resolve(strict=True)
    output_dir = output_dir.resolve()
    if output_dir in {theorem_path.parent, representation_path.parent}:
        raise V2D0ScaleRunError("output directory cannot overwrite an input directory")
    source_count, context_id, theorem_ids_hash, attempt_keys_hash = _inventory(
        theorem_path,
        representation_path,
        runtime=runtime,
        base_seed=base_seed,
        max_sources=max_sources,
    )
    spec = V2D0ScaleRunSpec(
        profile_id=runtime.loaded.config.profile_id,
        profile_config_hash=runtime.generation_config_hash,
        rule_id=runtime.rule_ids[0],
        theorem_partition=str(theorem_path),
        theorem_partition_sha256=hash_file(theorem_path),
        representation_partition=str(representation_path),
        representation_partition_sha256=hash_file(representation_path),
        project_dir=str(project_dir),
        context_id=context_id,
        import_header_sha256=hashlib.sha256(import_header.encode("utf-8")).hexdigest(),
        base_seed=base_seed,
        batch_size=batch_size,
        max_sources=max_sources,
        source_count=source_count,
        attempt_count=source_count,
        ordered_theorem_ids_sha256=theorem_ids_hash,
        ordered_attempt_keys_sha256=attempt_keys_hash,
    )
    run_spec_path = output_dir / "run_spec.json"
    _write_d0_immutable(run_spec_path, _canonical_line(spec))

    journal_entries: list[tuple[str, str]] = []
    journal_paths: list[Path] = []
    statuses: Counter[str] = Counter()
    family_statuses: Counter[str] = Counter()
    result_count = 0

    def process_batch(
        batch_index: int,
        batch_pairs: Sequence[tuple[TheoremRecord, RepresentationRecord]],
    ) -> None:
        nonlocal result_count
        batch_inputs = _ordered_attempts(batch_pairs, runtime, base_seed=base_seed)
        batch_path = output_dir / "journal" / f"batch_{batch_index:06d}.jsonl"
        if batch_path.exists():
            batch_results = _load_batch(batch_path, batch_inputs, runtime)
        else:
            batch_results = materialize_v2_d0_batch(
                backend=backend,
                runtime=runtime,
                inputs=batch_inputs,
                context_id=context_id,
                project_dir=project_dir,
                import_header=import_header,
            )
            _write_d0_immutable(batch_path, _batch_payload(batch_results))
        result_count += len(batch_results)
        statuses.update(item.terminal_status for item in batch_results)
        family_statuses.update(f"{item.rule_id}:{item.terminal_status}" for item in batch_results)
        journal_entries.append((batch_path.name, hash_file(batch_path)))
        journal_paths.append(batch_path)

    batch_pairs: list[tuple[TheoremRecord, RepresentationRecord]] = []
    batch_index = 0
    for pair in _iter_aligned_inputs(
        theorem_path,
        representation_path,
        max_sources=max_sources,
    ):
        batch_pairs.append(pair)
        if len(batch_pairs) == batch_size:
            process_batch(batch_index, batch_pairs)
            batch_pairs = []
            batch_index += 1
    if batch_pairs:
        process_batch(batch_index, batch_pairs)

    if result_count != source_count:
        raise V2D0ScaleRunError(f"streamed result count mismatch: {result_count} != {source_count}")

    results_path = output_dir / "results.jsonl"
    results_sha256 = _assemble_results(results_path, journal_paths)
    manifest = V2D0ScaleRunManifest(
        run_spec_sha256=hash_file(run_spec_path),
        batch_count=len(journal_entries),
        result_count=result_count,
        terminal_status_counts=dict(sorted(statuses.items())),
        family_status_counts=dict(sorted(family_statuses.items())),
        journal_tree_hash=hash_canonical(journal_entries),
        results_sha256=results_sha256,
    )
    manifest_path = output_dir / "manifest.json"
    _write_d0_immutable(manifest_path, _canonical_line(manifest))
    return V2D0ScaleRunArtifacts(
        output_dir=output_dir,
        run_spec_path=run_spec_path,
        manifest_path=manifest_path,
        results_path=results_path,
        result_count=result_count,
    )


__all__ = [
    "V2D0ScaleRunArtifacts",
    "V2D0ScaleRunError",
    "V2D0ScaleRunManifest",
    "V2D0ScaleRunSpec",
    "run_v2_d0_scale",
]
