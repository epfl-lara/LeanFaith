"""Resumable persisted runner for experimental deterministic-v2 E0 families.

This is intentionally smaller than the accepted v1 scientific-scale pipeline:
v2 outputs remain provisional and create no pairs, labels, promotion, or
training eligibility.  The runner nevertheless binds every input/config byte,
persists contiguous immutable batches, and validates an existing batch against
the exact source/rule/seed sequence before accepting it for resume.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.v2_e0_materializer import V2E0MaterializationResult
from leanfaith.transforms.v2_e0_runtime import V2E0RuleId, V2E0Runtime
from leanfaith.transforms.v2_e0_scale import (
    V2E0MaterializationInput,
    materialize_v2_e0_batch,
)

_HEX64 = r"^[0-9a-f]{64}$"


class V2E0ScaleRunError(RuntimeError):
    """A persisted v2 run violated its input, replay, or output contract."""


class V2E0ScaleRunSpec(StrictModel):
    """Immutable identity of one ordered v2 scale execution."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_v2_e0_scale_run_spec"] = (
        "deterministic_v2_e0_scale_run_spec"
    )
    profile_id: str
    profile_config_hash: str = Field(pattern=_HEX64)
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


class V2E0ScaleRunManifest(StrictModel):
    """Final exact accounting for a completed persisted v2 run."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_v2_e0_scale_manifest"] = (
        "deterministic_v2_e0_scale_manifest"
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
    def _reconciles(self) -> V2E0ScaleRunManifest:
        if sum(self.terminal_status_counts.values()) != self.result_count:
            raise ValueError("terminal status counts do not reconcile")
        if sum(self.family_status_counts.values()) != self.result_count:
            raise ValueError("family status counts do not reconcile")
        return self


@dataclass(frozen=True, slots=True)
class V2E0ScaleRunArtifacts:
    output_dir: Path
    run_spec_path: Path
    manifest_path: Path
    results_path: Path
    result_count: int


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _iter_jsonl[RecordT: StrictModel](
    path: Path,
    model: type[RecordT],
    *,
    wrapper_key: str | None = None,
) -> Iterator[RecordT]:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise V2E0ScaleRunError(f"cannot read {path}: {exc}") from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.endswith("\n") or not line.strip():
                raise V2E0ScaleRunError(f"{path}:{line_number}: invalid JSONL framing")
            try:
                raw = json.loads(line, object_pairs_hook=_reject_duplicates)
                if wrapper_key is not None and isinstance(raw, dict) and wrapper_key in raw:
                    raw = raw[wrapper_key]
                yield model.model_validate(raw)
            except Exception as exc:
                raise V2E0ScaleRunError(
                    f"{path}:{line_number}: invalid {model.__name__}: {exc}"
                ) from exc


def _iter_aligned_inputs(
    theorem_path: Path,
    representation_path: Path,
    *,
    max_sources: int | None,
) -> Iterator[tuple[TheoremRecord, RepresentationRecord]]:
    """Stream two canonical, order-aligned partitions with bounded live memory."""

    theorem_iter = _iter_jsonl(theorem_path, TheoremRecord, wrapper_key="theorem")
    representation_iter = _iter_jsonl(representation_path, RepresentationRecord)
    seen: set[str] = set()
    context_id: str | None = None
    count = 0
    while max_sources is None or count < max_sources:
        try:
            theorem = next(theorem_iter)
        except StopIteration:
            try:
                next(representation_iter)
            except StopIteration:
                return
            raise V2E0ScaleRunError("representation partition contains extra records") from None
        try:
            representation = next(representation_iter)
        except StopIteration:
            raise V2E0ScaleRunError(
                "theorem partition contains records without representations"
            ) from None
        if theorem.theorem_id != representation.theorem_id:
            raise V2E0ScaleRunError(
                "streaming theorem/representation order mismatch: "
                f"{theorem.theorem_id} != {representation.theorem_id}"
            )
        if theorem.theorem_id in seen:
            raise V2E0ScaleRunError(
                f"duplicate theorem ID in selected inventory: {theorem.theorem_id}"
            )
        seen.add(theorem.theorem_id)
        if theorem.context_id != representation.context_id:
            raise V2E0ScaleRunError(f"source context mismatch for {theorem.theorem_id}")
        if context_id is None:
            context_id = theorem.context_id
        elif theorem.context_id != context_id:
            raise V2E0ScaleRunError("v2 scale input must have exactly one Lean context")
        count += 1
        yield theorem, representation


def _inventory(
    theorem_path: Path,
    representation_path: Path,
    *,
    runtime: V2E0Runtime,
    base_seed: int,
    max_sources: int | None,
) -> tuple[int, int, str, str, str]:
    theorem_ids: list[str] = []
    attempt_keys: list[tuple[str, str, V2E0RuleId, int]] = []
    context_id: str | None = None
    for theorem, representation in _iter_aligned_inputs(
        theorem_path,
        representation_path,
        max_sources=max_sources,
    ):
        if context_id is None:
            context_id = theorem.context_id
        theorem_ids.append(theorem.theorem_id)
        for rule_id in runtime.rule_ids:
            typed_rule_id = cast(V2E0RuleId, rule_id)
            attempt_keys.append(
                (
                    theorem.theorem_id,
                    representation.representation_id,
                    typed_rule_id,
                    _seed(base_seed, theorem.theorem_id, typed_rule_id),
                )
            )
    if context_id is None:
        raise V2E0ScaleRunError("selected v2 E0 source inventory is empty")
    return (
        len(theorem_ids),
        len(attempt_keys),
        context_id,
        hash_canonical(theorem_ids),
        hash_canonical(attempt_keys),
    )


def _seed(base_seed: int, theorem_id: str, rule_id: str) -> int:
    payload = canonical_json_bytes(
        {
            "schema": "deterministic_v2_e0_scale_seed_v1",
            "base_seed": base_seed,
            "theorem_id": theorem_id,
            "rule_id": rule_id,
        }
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=False)


def _ordered_attempts(
    aligned: Sequence[tuple[TheoremRecord, RepresentationRecord]],
    runtime: V2E0Runtime,
    *,
    base_seed: int,
) -> tuple[V2E0MaterializationInput, ...]:
    return tuple(
        V2E0MaterializationInput(
            theorem=theorem,
            representation=representation,
            rule_id=cast(V2E0RuleId, rule_id),
            seed=_seed(base_seed, theorem.theorem_id, rule_id),
        )
        for theorem, representation in aligned
        for rule_id in runtime.rule_ids
    )


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _write_immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise V2E0ScaleRunError(f"immutable artifact conflict: {path}")
        return hash_file(path)
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
                raise V2E0ScaleRunError(f"concurrent immutable conflict: {path}") from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _batch_payload(results: Sequence[V2E0MaterializationResult]) -> bytes:
    return b"".join(_canonical_line(item) for item in results)


def _load_batch(
    path: Path,
    expected: Sequence[V2E0MaterializationInput],
    runtime: V2E0Runtime,
) -> tuple[V2E0MaterializationResult, ...]:
    results = tuple(_iter_jsonl(path, V2E0MaterializationResult))
    if len(results) != len(expected):
        raise V2E0ScaleRunError(f"resume batch cardinality mismatch: {path}")
    for result, item in zip(results, expected, strict=True):
        if (
            result.profile_id != runtime.loaded.config.profile_id
            or result.profile_config_hash != runtime.generation_config_hash
            or result.rule_id != item.rule_id
            or result.attempt.source_theorem_ids != (item.theorem.theorem_id,)
            or result.attempt.source_representation_ids != (item.representation.representation_id,)
            or result.attempt.seed != item.seed
        ):
            raise V2E0ScaleRunError(f"resume batch does not bind expected attempt: {path}")
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
    """Atomically concatenate immutable journals without retaining every result."""

    expected_hash, expected_size = _combined_hash_and_size(journal_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != expected_size
            or hash_file(path) != expected_hash
        ):
            raise V2E0ScaleRunError(f"immutable artifact conflict: {path}")
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
            raise V2E0ScaleRunError("streamed result assembly hash mismatch")
        try:
            os.link(temporary, path)
        except FileExistsError:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != expected_size
                or hash_file(path) != expected_hash
            ):
                raise V2E0ScaleRunError(f"concurrent immutable conflict: {path}") from None
        return expected_hash
    finally:
        temporary.unlink(missing_ok=True)


def run_v2_e0_scale(
    *,
    backend: LeanInteractBackend,
    runtime: V2E0Runtime,
    theorem_path: Path,
    representation_path: Path,
    project_dir: Path,
    import_header: str,
    output_dir: Path,
    batch_size: int = 128,
    base_seed: int = 0,
    max_sources: int | None = None,
) -> V2E0ScaleRunArtifacts:
    """Run or resume an exact experimental E0 inventory through pooled Lean."""

    if batch_size < 1:
        raise V2E0ScaleRunError("batch_size must be positive")
    if max_sources is not None and max_sources < 1:
        raise V2E0ScaleRunError("max_sources must be positive")
    theorem_path = theorem_path.resolve(strict=True)
    representation_path = representation_path.resolve(strict=True)
    project_dir = project_dir.resolve(strict=True)
    output_dir = output_dir.resolve()
    if output_dir in {theorem_path.parent, representation_path.parent}:
        raise V2E0ScaleRunError("output directory cannot overwrite an input directory")
    source_count, attempt_count, context_id, theorem_ids_hash, attempt_keys_hash = _inventory(
        theorem_path,
        representation_path,
        runtime=runtime,
        base_seed=base_seed,
        max_sources=max_sources,
    )
    spec = V2E0ScaleRunSpec(
        profile_id=runtime.loaded.config.profile_id,
        profile_config_hash=runtime.generation_config_hash,
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
        attempt_count=attempt_count,
        ordered_theorem_ids_sha256=theorem_ids_hash,
        ordered_attempt_keys_sha256=attempt_keys_hash,
    )
    run_spec_path = output_dir / "run_spec.json"
    _write_immutable(run_spec_path, _canonical_line(spec))

    journal_entries: list[tuple[str, str]] = []
    journal_paths: list[Path] = []
    statuses: Counter[str] = Counter()
    family_statuses: Counter[str] = Counter()
    result_count = 0

    def process_batch(
        batch_index: int,
        batch_inputs: Sequence[V2E0MaterializationInput],
    ) -> None:
        nonlocal result_count
        batch_path = output_dir / "journal" / f"batch_{batch_index:06d}.jsonl"
        if batch_path.exists():
            batch_results = _load_batch(batch_path, batch_inputs, runtime)
        else:
            batch_results = materialize_v2_e0_batch(
                backend=backend,
                runtime=runtime,
                inputs=batch_inputs,
                context_id=context_id,
                project_dir=project_dir,
                import_header=import_header,
            )
            _write_immutable(batch_path, _batch_payload(batch_results))
        result_count += len(batch_results)
        statuses.update(item.terminal_status for item in batch_results)
        family_statuses.update(f"{item.rule_id}:{item.terminal_status}" for item in batch_results)
        journal_entries.append((batch_path.name, hash_file(batch_path)))
        journal_paths.append(batch_path)

    batch_inputs: list[V2E0MaterializationInput] = []
    batch_index = 0
    for pair in _iter_aligned_inputs(
        theorem_path,
        representation_path,
        max_sources=max_sources,
    ):
        for attempt in _ordered_attempts((pair,), runtime, base_seed=base_seed):
            batch_inputs.append(attempt)
            if len(batch_inputs) == batch_size:
                process_batch(batch_index, batch_inputs)
                batch_inputs = []
                batch_index += 1
    if batch_inputs:
        process_batch(batch_index, batch_inputs)

    if result_count != attempt_count:
        raise V2E0ScaleRunError(
            f"streamed result count mismatch: {result_count} != {attempt_count}"
        )

    results_path = output_dir / "results.jsonl"
    results_sha256 = _assemble_results(results_path, journal_paths)
    manifest = V2E0ScaleRunManifest(
        run_spec_sha256=hash_file(run_spec_path),
        batch_count=len(journal_entries),
        result_count=result_count,
        terminal_status_counts=dict(sorted(statuses.items())),
        family_status_counts=dict(sorted(family_statuses.items())),
        journal_tree_hash=hash_canonical(journal_entries),
        results_sha256=results_sha256,
    )
    manifest_path = output_dir / "manifest.json"
    _write_immutable(manifest_path, _canonical_line(manifest))
    return V2E0ScaleRunArtifacts(
        output_dir=output_dir,
        run_spec_path=run_spec_path,
        manifest_path=manifest_path,
        results_path=results_path,
        result_count=result_count,
    )


__all__ = [
    "V2E0ScaleRunArtifacts",
    "V2E0ScaleRunError",
    "V2E0ScaleRunManifest",
    "V2E0ScaleRunSpec",
    "run_v2_e0_scale",
]
