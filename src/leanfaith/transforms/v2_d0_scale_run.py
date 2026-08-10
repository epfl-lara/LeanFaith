"""Resumable persisted scale runner for LF-034's experimental N11 profile."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.v2_d0_materializer import V2D0MaterializationResult
from leanfaith.transforms.v2_d0_runtime import V2D0Runtime
from leanfaith.transforms.v2_d0_scale import (
    V2D0MaterializationInput,
    materialize_v2_d0_batch,
)
from leanfaith.transforms.v2_e0_scale_run import (
    V2E0ScaleRunError,
    _canonical_line,
    _iter_jsonl,
    _load_inputs,
    _write_immutable,
)

_HEX64 = r"^[0-9a-f]{64}$"


class V2D0ScaleRunError(RuntimeError):
    """A persisted N11 run violated its exact replay contract."""


class V2D0ScaleRunSpec(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_v2_d0_scale_run_spec"] = (
        "deterministic_v2_d0_scale_run_spec"
    )
    profile_id: Literal["deterministic_v2_d0_n11_experimental"]
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


class V2D0ScaleRunManifest(StrictModel):
    schema_version: Literal[1] = 1
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


def _seed(base_seed: int, theorem_id: str) -> int:
    payload = canonical_json_bytes(
        {
            "schema": "deterministic_v2_d0_n11_scale_seed_v1",
            "base_seed": base_seed,
            "theorem_id": theorem_id,
            "rule_id": "n11_bound_variable_substitution",
        }
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=False)


def _ordered_attempts(
    aligned: Sequence[tuple[TheoremRecord, RepresentationRecord]],
    *,
    base_seed: int,
) -> tuple[V2D0MaterializationInput, ...]:
    return tuple(
        V2D0MaterializationInput(
            theorem=theorem,
            representation=representation,
            rule_id="n11_bound_variable_substitution",
            seed=_seed(base_seed, theorem.theorem_id),
        )
        for theorem, representation in aligned
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
    runtime: V2D0Runtime,
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


def run_v2_d0_scale(
    *,
    backend: LeanInteractBackend,
    runtime: V2D0Runtime,
    theorem_path: Path,
    representation_path: Path,
    project_dir: Path,
    import_header: str,
    output_dir: Path,
    batch_size: int = 128,
    base_seed: int = 0,
    max_sources: int | None = None,
) -> V2D0ScaleRunArtifacts:
    """Run or resume the exact N11 inventory through pooled LeanInteract."""

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
    try:
        aligned = _load_inputs(theorem_path, representation_path)
    except V2E0ScaleRunError as exc:
        raise V2D0ScaleRunError(str(exc)) from exc
    if max_sources is not None:
        aligned = aligned[:max_sources]
    if not aligned:
        raise V2D0ScaleRunError("selected N11 source inventory is empty")
    context_id = aligned[0][0].context_id
    attempts = _ordered_attempts(aligned, base_seed=base_seed)
    attempt_keys = [
        (item.theorem.theorem_id, item.representation.representation_id, item.rule_id, item.seed)
        for item in attempts
    ]
    spec = V2D0ScaleRunSpec(
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
        source_count=len(aligned),
        attempt_count=len(attempts),
        ordered_theorem_ids_sha256=hash_canonical([theorem.theorem_id for theorem, _ in aligned]),
        ordered_attempt_keys_sha256=hash_canonical(attempt_keys),
    )
    run_spec_path = output_dir / "run_spec.json"
    _write_d0_immutable(run_spec_path, _canonical_line(spec))

    all_results: list[V2D0MaterializationResult] = []
    journal_entries: list[tuple[str, str]] = []
    for batch_index, start in enumerate(range(0, len(attempts), batch_size)):
        batch_inputs = attempts[start : start + batch_size]
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
        all_results.extend(batch_results)
        journal_entries.append((batch_path.name, hash_file(batch_path)))

    results_path = output_dir / "results.jsonl"
    results_sha256 = _write_d0_immutable(results_path, _batch_payload(all_results))
    statuses = Counter(item.terminal_status for item in all_results)
    family_statuses = Counter(f"{item.rule_id}:{item.terminal_status}" for item in all_results)
    manifest = V2D0ScaleRunManifest(
        run_spec_sha256=hash_file(run_spec_path),
        batch_count=len(journal_entries),
        result_count=len(all_results),
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
        result_count=len(all_results),
    )


__all__ = [
    "V2D0ScaleRunArtifacts",
    "V2D0ScaleRunError",
    "V2D0ScaleRunManifest",
    "V2D0ScaleRunSpec",
    "run_v2_d0_scale",
]
