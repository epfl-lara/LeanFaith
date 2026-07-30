"""Fail-closed merge and audit for deterministic materialization shards."""

from __future__ import annotations

import datetime
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, TypeAdapter, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas.enums import QualityTier
from leanfaith.schemas.pair import PairRecord
from leanfaith.schemas.theorem import TheoremRecord
from leanfaith.schemas.variant import VariantRecord
from leanfaith.transforms.scale_materializer import (
    DeterministicScaleConfig,
    DeterministicScaleError,
    DeterministicScaleManifest,
    DeterministicScaleRunSpec,
    ScaleSourceShard,
    _AdmissionState,
    _admit_source_shard,
    _canonical_model_bytes,
    _journal_receipt_path,
    _load_journal_receipt,
    _load_jsonl,
    _load_source_shard,
    _project_records,
    _root_component_shard_assignments,
    _run_lock,
    _run_spec_payload,
    _selection_key,
    _shard_set_spec_payload,
    _source_shard_path,
    _tree_hash,
    _write_new_atomic,
    _write_partitions,
)

_HEX64_PATTERN = r"^[0-9a-f]{64}$"


class DeterministicScaleMergedShardBinding(StrictModel):
    """Content binding for one audited producer shard."""

    shard_index: int = Field(ge=0)
    output_dir: str
    run_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    run_spec_sha256: str = Field(pattern=_HEX64_PATTERN)
    manifest_sha256: str = Field(pattern=_HEX64_PATTERN)
    selected_source_count: int = Field(ge=1)
    selected_source_ids_sha256: str = Field(pattern=_HEX64_PATTERN)
    journal_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    journal_receipt_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    journal_chain_tip: str = Field(pattern=_HEX64_PATTERN)


class DeterministicScaleMergedManifest(StrictModel):
    """Content-addressed audit manifest for one complete shard set."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_scale_merged_manifest"] = (
        "deterministic_scale_merged_manifest"
    )
    merged_manifest_hash: str = Field(pattern=_HEX64_PATTERN)
    shard_set_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    shard_count: int = Field(ge=1)
    shard_bindings: tuple[DeterministicScaleMergedShardBinding, ...]
    source_universe_count: int = Field(ge=1)
    source_universe_sha256: str = Field(pattern=_HEX64_PATTERN)
    source_assignment_sha256: str = Field(pattern=_HEX64_PATTERN)
    eligible_source_count: int = Field(ge=0)
    ineligible_source_count: int = Field(ge=0)
    rule_status_counts: dict[str, int]
    family_accepted_counts: dict[str, int]
    record_counts: dict[str, int]
    partition_sha256: dict[str, str]
    aggregate_journal_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    aggregate_receipt_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    aggregate_raw_response_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    resolved_semantic_labels: Literal[0] = 0
    promoted_items: Literal[0] = 0
    output_quality_tier: Literal["provisional"] = "provisional"
    created_at: datetime.datetime

    @model_validator(mode="after")
    def _self_authenticating(self) -> DeterministicScaleMergedManifest:
        payload = self.model_dump(mode="json")
        payload.pop("merged_manifest_hash")
        if self.merged_manifest_hash != hash_canonical(payload):
            raise ValueError("merged manifest hash does not match canonical payload")
        if len(self.shard_bindings) != self.shard_count:
            raise ValueError("merged shard binding count differs from shard_count")
        if tuple(binding.shard_index for binding in self.shard_bindings) != tuple(
            range(self.shard_count)
        ):
            raise ValueError("merged shard bindings are not complete and ordered")
        return self


@dataclass(frozen=True, slots=True)
class DeterministicScaleMergeArtifacts:
    output_dir: Path
    manifest_path: Path
    manifest_sha256: str
    merged_manifest_hash: str
    partition_paths: Mapping[str, Path]


def _load_canonical_model[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
) -> ModelT:
    try:
        payload = path.read_bytes()
        raw = json.loads(payload)
        parsed = model.model_validate(raw)
    except Exception as exc:
        raise DeterministicScaleError(f"invalid {model.__name__} at {path}: {exc}") from exc
    if payload != _canonical_model_bytes(parsed):
        raise DeterministicScaleError(f"{model.__name__} is not canonical JSON: {path}")
    return parsed


def _validate_run_spec(spec: DeterministicScaleRunSpec) -> None:
    dumped = spec.model_dump(mode="json")
    if hash_canonical(_run_spec_payload(dumped)) != spec.run_spec_hash:
        raise DeterministicScaleError("shard run_spec_hash does not match its payload")
    if hash_canonical(_shard_set_spec_payload(dumped)) != spec.shard_set_spec_hash:
        raise DeterministicScaleError("shard_set_spec_hash does not match its common payload")


def _canonical_partition_payload(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_model_bytes(record) for record in records)


def _validate_current_input_bindings(spec: DeterministicScaleRunSpec) -> None:
    bindings = {
        "theorem input": (spec.theorem_input_path, spec.theorem_input_sha256),
        "representation input": (
            spec.representation_input_path,
            spec.representation_input_sha256,
        ),
        "source inventory": (
            spec.source_inventory_manifest_path,
            spec.source_inventory_manifest_sha256,
        ),
        "theorem upstream manifest": (
            spec.theorem_upstream_manifest_path,
            spec.theorem_upstream_manifest_sha256,
        ),
        "representation upstream manifest": (
            spec.representation_upstream_manifest_path,
            spec.representation_upstream_manifest_sha256,
        ),
        "benchmark manifest": (
            spec.benchmark_manifest_path,
            spec.benchmark_manifest_sha256,
        ),
    }
    for label, (raw_path, expected_hash) in bindings.items():
        path = Path(raw_path)
        if not path.is_file() or hash_file(path) != expected_hash:
            raise DeterministicScaleError(f"{label} changed or is unavailable: {path}")
    loaded_config = load_config(Path(spec.config_path), DeterministicScaleConfig)
    if loaded_config.config_hash != spec.config_hash:
        raise DeterministicScaleError("deterministic scale config changed after shard execution")


def _expected_local_entries(
    spec: DeterministicScaleRunSpec,
) -> tuple[tuple[int, str], ...]:
    return tuple(
        (global_index, theorem_id)
        for global_index, (theorem_id, assignment) in enumerate(
            zip(
                spec.source_universe_theorem_ids,
                spec.source_shard_assignments,
                strict=True,
            )
        )
        if assignment == spec.shard_index
    )


def _validate_shard_output(
    *,
    output_dir: Path,
    spec: DeterministicScaleRunSpec,
    manifest: DeterministicScaleManifest,
    config: DeterministicScaleConfig,
) -> tuple[tuple[ScaleSourceShard, ...], DeterministicScaleMergedShardBinding]:
    run_spec_path = output_dir / "run_spec.json"
    manifest_path = output_dir / "manifest.json"
    if (
        manifest.run_spec_hash != spec.run_spec_hash
        or manifest.run_spec_sha256 != hash_file(run_spec_path)
        or manifest.shard_set_spec_hash != spec.shard_set_spec_hash
        or manifest.shard_count != spec.shard_count
        or manifest.shard_index != spec.shard_index
        or manifest.source_universe_count != len(spec.source_universe_theorem_ids)
    ):
        raise DeterministicScaleError("shard manifest/run-spec identity does not reconcile")
    assignment_hash = hash_canonical(
        {
            "source_universe_theorem_ids": spec.source_universe_theorem_ids,
            "source_shard_assignments": spec.source_shard_assignments,
        }
    )
    if manifest.source_assignment_sha256 != assignment_hash:
        raise DeterministicScaleError("shard manifest source assignment hash mismatch")

    entries = _expected_local_entries(spec)
    journal_dir = output_dir / "journal"
    receipt_dir = output_dir / "journal_receipts"
    expected_paths = tuple(
        _source_shard_path(journal_dir, global_index, theorem_id)
        for global_index, theorem_id in entries
    )
    expected_receipts = tuple(_journal_receipt_path(receipt_dir, path) for path in expected_paths)
    if set(journal_dir.glob("*.json")) != set(expected_paths):
        raise DeterministicScaleError("shard journal is incomplete or contains foreign files")
    if set(receipt_dir.glob("*.json")) != set(expected_receipts):
        raise DeterministicScaleError(
            "shard journal receipt chain is incomplete or contains foreign files"
        )

    shards: list[ScaleSourceShard] = []
    previous_receipt_hash = "0" * 64
    for (global_index, theorem_id), shard_path, receipt_path in zip(
        entries,
        expected_paths,
        expected_receipts,
        strict=True,
    ):
        shard = _load_source_shard(shard_path)
        if (
            shard.run_spec_hash != spec.run_spec_hash
            or shard.source_index != global_index
            or shard.source_theorem_id != theorem_id
        ):
            raise DeterministicScaleError("journal shard source/run assignment mismatch")
        receipt = _load_journal_receipt(
            path=receipt_path,
            shard=shard,
            shard_path=shard_path,
            previous_receipt_hash=previous_receipt_hash,
        )
        previous_receipt_hash = receipt.receipt_hash
        shards.append(shard)

    projected = _project_records(shards)
    expected_partition_names = set(projected)
    partition_dir = output_dir / "partitions"
    actual_partition_names = {path.stem for path in partition_dir.glob("*.jsonl")}
    if actual_partition_names != expected_partition_names:
        raise DeterministicScaleError(
            "shard partitions are incomplete or contain an unexpected label/artifact partition"
        )
    partition_hashes: dict[str, str] = {}
    for name, records in projected.items():
        path = partition_dir / f"{name}.jsonl"
        expected_payload = _canonical_partition_payload(records)
        if path.read_bytes() != expected_payload:
            raise DeterministicScaleError(f"shard partition differs from journal: {path}")
        partition_hashes[name] = hash_file(path)

    state = _AdmissionState(
        root_counts=Counter(),
        family_root_counts=Counter(),
        family_counts=Counter(),
        candidate_keys=set(),
        variant_ids=set(),
        pair_ids=set(),
    )
    for shard in shards:
        _admit_source_shard(state, shard)
    status_counts = Counter(result.status for shard in shards for result in shard.rule_results)
    journal_count, journal_tree_hash = _tree_hash(journal_dir, "*.json")
    receipt_count, receipt_tree_hash = _tree_hash(receipt_dir, "*.json")
    raw_count, raw_tree_hash = _tree_hash(output_dir / "raw_lean_responses", "*")
    expected_manifest = DeterministicScaleManifest(
        run_spec_hash=spec.run_spec_hash,
        run_spec_sha256=hash_file(run_spec_path),
        shard_set_spec_hash=spec.shard_set_spec_hash,
        shard_count=spec.shard_count,
        shard_index=spec.shard_index,
        source_universe_count=len(spec.source_universe_theorem_ids),
        source_assignment_sha256=assignment_hash,
        source_count=len(shards),
        eligible_source_count=sum(shard.source_status == "eligible" for shard in shards),
        ineligible_source_count=sum(shard.source_status == "ineligible" for shard in shards),
        journal_shard_count=journal_count,
        rule_status_counts=dict(sorted(status_counts.items())),
        family_accepted_counts=dict(sorted(state.family_counts.items())),
        record_counts={name: len(records) for name, records in projected.items()},
        partition_sha256=dict(sorted(partition_hashes.items())),
        journal_tree_hash=journal_tree_hash,
        journal_receipt_count=receipt_count,
        journal_receipt_tree_hash=receipt_tree_hash,
        journal_chain_tip=previous_receipt_hash,
        raw_response_file_count=raw_count,
        raw_response_tree_hash=raw_tree_hash,
        created_at=config.record_timestamp_utc,
    )
    if manifest != expected_manifest:
        raise DeterministicScaleError("shard manifest does not reconcile from immutable outputs")

    return (
        tuple(shards),
        DeterministicScaleMergedShardBinding(
            shard_index=spec.shard_index,
            output_dir=str(output_dir),
            run_spec_hash=spec.run_spec_hash,
            run_spec_sha256=hash_file(run_spec_path),
            manifest_sha256=hash_file(manifest_path),
            selected_source_count=len(spec.selected_source_theorem_ids),
            selected_source_ids_sha256=hash_canonical(spec.selected_source_theorem_ids),
            journal_tree_hash=journal_tree_hash,
            journal_receipt_tree_hash=receipt_tree_hash,
            journal_chain_tip=previous_receipt_hash,
        ),
    )


def _reject_cross_shard_semantic_leakage(
    projected: Mapping[str, Sequence[StrictModel]],
) -> None:
    variants = cast(Sequence[VariantRecord], projected["variants"])
    pairs = cast(Sequence[PairRecord], projected["pairs"])
    if any(variant.quality_tier != QualityTier.PROVISIONAL for variant in variants):
        raise DeterministicScaleError("merged variants are not uniformly provisional")
    if any(pair.resolved_label_id is not None for pair in pairs):
        raise DeterministicScaleError("merged pairs contain resolved semantic labels")

    id_fields = {
        "drafts": "draft_id",
        "candidate_theorems": "theorem_id",
        "candidate_representations": "representation_id",
        "audits": "audit_id",
        "variants": "variant_id",
        "pairs": "pair_id",
    }
    for partition, field in id_fields.items():
        values = [getattr(record, field) for record in projected[partition]]
        if len(values) != len(set(values)):
            raise DeterministicScaleError(f"duplicate {field} values detected across merged shards")

    candidate_keys = [
        (
            theorem.root_ancestry_ids,
            variant.candidate_code_hash,
        )
        for theorem, variant in zip(
            cast(Sequence[TheoremRecord], projected["candidate_theorems"]),
            variants,
            strict=True,
        )
    ]
    if len(candidate_keys) != len(set(candidate_keys)):
        raise DeterministicScaleError(
            "duplicate ancestry/candidate payload detected across merged shards"
        )


def merge_deterministic_scale_shards(
    *,
    paths: RepoPaths,
    shard_output_dirs: Sequence[Path],
    output_dir: Path,
) -> DeterministicScaleMergeArtifacts:
    """Audit a complete shard set and write deterministic merged projections."""

    resolved_dirs = tuple(path.resolve() for path in shard_output_dirs)
    if not resolved_dirs or len(set(resolved_dirs)) != len(resolved_dirs):
        raise DeterministicScaleError("shard output directories must be nonempty and unique")
    output = output_dir.resolve()
    if output in resolved_dirs:
        raise DeterministicScaleError("merged output directory cannot be a producer shard")

    loaded: list[tuple[Path, DeterministicScaleRunSpec, DeterministicScaleManifest]] = []
    for shard_dir in resolved_dirs:
        spec = _load_canonical_model(
            shard_dir / "run_spec.json",
            DeterministicScaleRunSpec,
        )
        _validate_run_spec(spec)
        manifest = _load_canonical_model(
            shard_dir / "manifest.json",
            DeterministicScaleManifest,
        )
        loaded.append((shard_dir, spec, manifest))
    loaded.sort(key=lambda item: item[1].shard_index)

    first_spec = loaded[0][1]
    common_payload = _shard_set_spec_payload(first_spec.model_dump(mode="json"))
    if len(loaded) != first_spec.shard_count:
        raise DeterministicScaleError("merge requires every shard in the bound shard set")
    for expected_index, (_, spec, _) in enumerate(loaded):
        if spec.shard_index != expected_index:
            raise DeterministicScaleError("shard indices contain a gap or overlap")
        if (
            spec.shard_set_spec_hash != first_spec.shard_set_spec_hash
            or _shard_set_spec_payload(spec.model_dump(mode="json")) != common_payload
        ):
            raise DeterministicScaleError(
                "shards do not share identical input/config/code provenance"
            )

    _validate_current_input_bindings(first_spec)
    loaded_config = load_config(
        Path(first_spec.config_path),
        DeterministicScaleConfig,
    )
    theorems = _load_jsonl(
        Path(first_spec.theorem_input_path),
        TheoremRecord,
        wrapper_key="theorem",
    )
    ordered = tuple(
        sorted(
            theorems,
            key=lambda theorem: _selection_key(
                loaded_config.config.base_seed,
                theorem.theorem_id,
            ),
        )
    )
    universe = ordered if first_spec.max_sources is None else ordered[: first_spec.max_sources]
    if tuple(theorem.theorem_id for theorem in universe) != (
        first_spec.source_universe_theorem_ids
    ):
        raise DeterministicScaleError("source universe no longer matches immutable inputs")
    recomputed_assignments = _root_component_shard_assignments(
        universe,
        shard_count=first_spec.shard_count,
    )
    if recomputed_assignments != first_spec.source_shard_assignments:
        raise DeterministicScaleError("source shard assignment does not recompute")

    all_shards: list[ScaleSourceShard] = []
    bindings: list[DeterministicScaleMergedShardBinding] = []
    observed_sources: set[str] = set()
    for shard_dir, spec, manifest in loaded:
        overlap = observed_sources & set(spec.selected_source_theorem_ids)
        if overlap:
            raise DeterministicScaleError(
                f"source assignment overlaps across shards: {sorted(overlap)[:3]}"
            )
        observed_sources.update(spec.selected_source_theorem_ids)
        shards, binding = _validate_shard_output(
            output_dir=shard_dir,
            spec=spec,
            manifest=manifest,
            config=loaded_config.config,
        )
        all_shards.extend(shards)
        bindings.append(binding)
    if observed_sources != set(first_spec.source_universe_theorem_ids):
        missing = set(first_spec.source_universe_theorem_ids) - observed_sources
        raise DeterministicScaleError(
            f"source assignment is incomplete: missing {sorted(missing)[:3]}"
        )

    all_shards.sort(key=lambda shard: shard.source_index)
    if tuple(shard.source_theorem_id for shard in all_shards) != (
        first_spec.source_universe_theorem_ids
    ):
        raise DeterministicScaleError("merged source journal order is not the source universe")
    projected = _project_records(all_shards)
    _reject_cross_shard_semantic_leakage(projected)

    global_state = _AdmissionState(
        root_counts=Counter(),
        family_root_counts=Counter(),
        family_counts=Counter(),
        candidate_keys=set(),
        variant_ids=set(),
        pair_ids=set(),
    )
    for shard in all_shards:
        _admit_source_shard(global_state, shard)
    config = loaded_config.config
    if any(
        count > config.max_accepted_variants_per_root_ancestry
        for count in global_state.root_counts.values()
    ):
        raise DeterministicScaleError("merged output violates the per-root admission cap")
    if any(
        count > config.max_accepted_variants_per_family_per_root_ancestry
        for count in global_state.family_root_counts.values()
    ):
        raise DeterministicScaleError("merged output violates the per-family/root admission cap")
    if config.max_accepted_variants_per_family is not None and any(
        count > config.max_accepted_variants_per_family
        for count in global_state.family_counts.values()
    ):
        raise DeterministicScaleError("merged output violates the global family cap")

    with _run_lock(output):
        unexpected = tuple(
            path
            for path in output.iterdir()
            if path.name != "run.lock"
            and not path.name.startswith("merged_manifest.")
            and path.name != "partitions"
        )
        if unexpected:
            raise DeterministicScaleError(
                f"merged output directory contains foreign files: {unexpected[:3]}"
            )
        existing_partition_names = {path.stem for path in (output / "partitions").glob("*.jsonl")}
        foreign_partitions = existing_partition_names - set(projected)
        if foreign_partitions:
            raise DeterministicScaleError(
                f"merged output contains foreign partitions: {sorted(foreign_partitions)}"
            )
        partition_paths, partition_hashes = _write_partitions(output, projected)
        status_counts = Counter(
            result.status for shard in all_shards for result in shard.rule_results
        )
        source_assignment_hash = hash_canonical(
            {
                "source_universe_theorem_ids": first_spec.source_universe_theorem_ids,
                "source_shard_assignments": first_spec.source_shard_assignments,
            }
        )
        data: dict[str, object] = {
            "schema_version": 1,
            "artifact_kind": "deterministic_scale_merged_manifest",
            "shard_set_spec_hash": first_spec.shard_set_spec_hash,
            "shard_count": first_spec.shard_count,
            "shard_bindings": tuple(bindings),
            "source_universe_count": len(first_spec.source_universe_theorem_ids),
            "source_universe_sha256": hash_canonical(first_spec.source_universe_theorem_ids),
            "source_assignment_sha256": source_assignment_hash,
            "eligible_source_count": sum(shard.source_status == "eligible" for shard in all_shards),
            "ineligible_source_count": sum(
                shard.source_status == "ineligible" for shard in all_shards
            ),
            "rule_status_counts": dict(sorted(status_counts.items())),
            "family_accepted_counts": dict(sorted(global_state.family_counts.items())),
            "record_counts": {name: len(records) for name, records in projected.items()},
            "partition_sha256": dict(sorted(partition_hashes.items())),
            "aggregate_journal_tree_hash": hash_canonical(
                tuple((binding.shard_index, binding.journal_tree_hash) for binding in bindings)
            ),
            "aggregate_receipt_tree_hash": hash_canonical(
                tuple(
                    (binding.shard_index, binding.journal_receipt_tree_hash) for binding in bindings
                )
            ),
            "aggregate_raw_response_tree_hash": hash_canonical(
                tuple(
                    (
                        spec.shard_index,
                        manifest.raw_response_tree_hash,
                        manifest.raw_response_file_count,
                    )
                    for _, spec, manifest in loaded
                )
            ),
            "resolved_semantic_labels": 0,
            "promoted_items": 0,
            "output_quality_tier": "provisional",
            "created_at": config.record_timestamp_utc,
        }
        hash_payload = {
            **data,
            "shard_bindings": tuple(binding.model_dump(mode="json") for binding in bindings),
            "created_at": TypeAdapter(datetime.datetime).dump_python(
                config.record_timestamp_utc,
                mode="json",
            ),
        }
        merged_manifest_hash = hash_canonical(hash_payload)
        merged_manifest = DeterministicScaleMergedManifest.model_validate(
            {"merged_manifest_hash": merged_manifest_hash, **data}
        )
        manifest_path = output / f"merged_manifest.{merged_manifest_hash}.json"
        manifest_sha256 = _write_new_atomic(
            manifest_path,
            _canonical_model_bytes(merged_manifest),
        )
    return DeterministicScaleMergeArtifacts(
        output_dir=output,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        merged_manifest_hash=merged_manifest_hash,
        partition_paths=partition_paths,
    )


__all__ = [
    "DeterministicScaleMergeArtifacts",
    "DeterministicScaleMergedManifest",
    "DeterministicScaleMergedShardBinding",
    "merge_deterministic_scale_shards",
]
