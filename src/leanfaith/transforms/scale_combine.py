"""Fail-closed audit for combining unary and global-N10 scale passes."""

from __future__ import annotations

import datetime
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
from leanfaith.schemas.manifest import CodeState
from leanfaith.schemas.pair import PairRecord
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import (
    TransformationAttempt,
    TransformationAudit,
    VariantDraft,
    VariantRecord,
)
from leanfaith.transforms.scale_materializer import (
    DeterministicScaleConfig,
    DeterministicScaleError,
    DeterministicScaleRunSpec,
    ScaleQuarantineRecord,
    _canonical_model_bytes,
    _load_jsonl,
    _run_lock,
    _write_new_atomic,
)
from leanfaith.transforms.scale_merge import (
    DeterministicScaleMergedManifest,
    _load_canonical_model,
    merge_deterministic_scale_shards,
)

_HEX64_PATTERN = r"^[0-9a-f]{64}$"
_PARTITION_MODELS: Mapping[str, type[StrictModel]] = {
    "attempts": TransformationAttempt,
    "drafts": VariantDraft,
    "candidate_theorems": TheoremRecord,
    "candidate_representations": RepresentationRecord,
    "audits": TransformationAudit,
    "variants": VariantRecord,
    "pairs": PairRecord,
    "quarantine": ScaleQuarantineRecord,
}


class DeterministicScaleCombinedPassBinding(StrictModel):
    """Content binding for one independently merged deterministic pass."""

    role: Literal["unary", "global_n10"]
    merged_output_dir: str
    merged_manifest_path: str
    merged_manifest_hash: str = Field(pattern=_HEX64_PATTERN)
    merged_manifest_sha256: str = Field(pattern=_HEX64_PATTERN)
    source_universe_sha256: str = Field(pattern=_HEX64_PATTERN)
    active_rule_ids: tuple[str, ...]
    record_counts: dict[str, int]
    partition_sha256: dict[str, str]


class DeterministicScaleCombinedManifest(StrictModel):
    """Authorization to treat two independently generated passes together."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_scale_two_pass_manifest"] = (
        "deterministic_scale_two_pass_manifest"
    )
    combined_manifest_hash: str = Field(pattern=_HEX64_PATTERN)
    pass_bindings: tuple[DeterministicScaleCombinedPassBinding, ...]
    common_input_identity_hash: str = Field(pattern=_HEX64_PATTERN)
    source_universe_count: int = Field(ge=1)
    source_universe_sha256: str = Field(pattern=_HEX64_PATTERN)
    context_id: str
    context_record_sha256: str = Field(pattern=_HEX64_PATTERN)
    project_revision: str
    project_tree_hash: str
    code: CodeState
    family_ownership: dict[str, str]
    combined_record_counts: dict[str, int]
    combined_family_accepted_counts: dict[str, int]
    max_accepted_variants_per_root_ancestry: int = Field(ge=1)
    max_accepted_variants_per_family_per_root_ancestry: int = Field(ge=1)
    max_accepted_variants_per_family: int | None = Field(default=None, ge=1)
    cross_pass_ids_disjoint: Literal[True] = True
    cross_pass_candidates_disjoint: Literal[True] = True
    scientific_pairing_eligible: Literal[True] = True
    output_quality_tier: Literal["provisional"] = "provisional"
    training_eligible: Literal[False] = False
    created_at: datetime.datetime

    @model_validator(mode="after")
    def _self_consistent(self) -> DeterministicScaleCombinedManifest:
        payload = self.model_dump(mode="json")
        payload.pop("combined_manifest_hash")
        if self.combined_manifest_hash != hash_canonical(payload):
            raise ValueError("combined manifest hash does not match canonical payload")
        if tuple(binding.role for binding in self.pass_bindings) != (
            "unary",
            "global_n10",
        ):
            raise ValueError("combined pass bindings must be unary then global_n10")
        if set(self.family_ownership.values()) != {"unary", "global_n10"}:
            raise ValueError("combined family ownership must include both pass roles")
        return self


@dataclass(frozen=True, slots=True)
class DeterministicScaleCombinedArtifacts:
    output_dir: Path
    manifest_path: Path
    manifest_sha256: str
    combined_manifest_hash: str


@dataclass(frozen=True, slots=True)
class _LoadedPass:
    role: Literal["unary", "global_n10"]
    output_dir: Path
    manifest_path: Path
    manifest: DeterministicScaleMergedManifest
    spec: DeterministicScaleRunSpec
    config: DeterministicScaleConfig
    projected: Mapping[str, tuple[StrictModel, ...]]


def _merged_manifest_path(output_dir: Path) -> Path:
    paths = tuple(sorted(output_dir.glob("merged_manifest.*.json")))
    if len(paths) != 1:
        raise DeterministicScaleError(
            f"merged pass must contain exactly one content-addressed manifest: {output_dir}"
        )
    return paths[0]


def _common_input_identity(spec: DeterministicScaleRunSpec) -> dict[str, object]:
    return {
        "theorem_input_path": spec.theorem_input_path,
        "theorem_input_sha256": spec.theorem_input_sha256,
        "representation_input_path": spec.representation_input_path,
        "representation_input_sha256": spec.representation_input_sha256,
        "source_inventory_manifest_path": spec.source_inventory_manifest_path,
        "source_inventory_manifest_sha256": spec.source_inventory_manifest_sha256,
        "theorem_upstream_manifest_path": spec.theorem_upstream_manifest_path,
        "theorem_upstream_manifest_sha256": spec.theorem_upstream_manifest_sha256,
        "representation_upstream_manifest_path": spec.representation_upstream_manifest_path,
        "representation_upstream_manifest_sha256": (spec.representation_upstream_manifest_sha256),
        "registry_hash": spec.registry_hash,
        "benchmark_manifest_path": spec.benchmark_manifest_path,
        "benchmark_manifest_sha256": spec.benchmark_manifest_sha256,
        "context_id": spec.context_id,
        "context_record_sha256": spec.context_record_sha256,
        "project_dir": spec.project_dir,
        "project_revision": spec.project_revision,
        "project_tree_hash": spec.project_tree_hash,
        "code": spec.code.model_dump(mode="json"),
        "source_universe_theorem_ids": spec.source_universe_theorem_ids,
        "max_sources": spec.max_sources,
    }


def _revalidate_merged_pass_with_lean(
    *,
    paths: RepoPaths,
    output_dir: Path,
    manifest: DeterministicScaleMergedManifest,
) -> None:
    """Re-run the ordinary merge, including exact Lean replay, before combine."""

    shard_dirs = tuple(Path(binding.output_dir) for binding in manifest.shard_bindings)
    replayed = merge_deterministic_scale_shards(
        paths=paths,
        shard_output_dirs=shard_dirs,
        output_dir=output_dir,
    )
    if (
        replayed.merged_manifest_hash != manifest.merged_manifest_hash
        or replayed.manifest_sha256 != hash_file(_merged_manifest_path(output_dir))
    ):
        raise DeterministicScaleError(
            "Lean-replayed merged pass differs from its bound content-addressed manifest"
        )


def _load_pass(
    *,
    paths: RepoPaths,
    role: Literal["unary", "global_n10"],
    output_dir: Path,
) -> _LoadedPass:
    resolved = output_dir.resolve()
    manifest_path = _merged_manifest_path(resolved)
    manifest = _load_canonical_model(
        manifest_path,
        DeterministicScaleMergedManifest,
    )
    if manifest_path.name != f"merged_manifest.{manifest.merged_manifest_hash}.json":
        raise DeterministicScaleError(
            f"{role} merged manifest filename does not match its content hash"
        )
    if not manifest.merge_replayed_with_lean:
        raise DeterministicScaleError("combined input was not produced by an exact Lean merge")
    _revalidate_merged_pass_with_lean(
        paths=paths,
        output_dir=resolved,
        manifest=manifest,
    )
    producer_specs: list[DeterministicScaleRunSpec] = []
    for binding in manifest.shard_bindings:
        producer_dir = Path(binding.output_dir)
        spec_path = producer_dir / "run_spec.json"
        spec = _load_canonical_model(spec_path, DeterministicScaleRunSpec)
        if (
            spec.run_spec_hash != binding.run_spec_hash
            or hash_file(spec_path) != binding.run_spec_sha256
        ):
            raise DeterministicScaleError(
                f"{role} merged binding does not match its producer run spec"
            )
        producer_specs.append(spec)
    spec = producer_specs[0]
    common_identity = _common_input_identity(spec)
    if any(_common_input_identity(other) != common_identity for other in producer_specs[1:]):
        raise DeterministicScaleError(f"{role} producer shards have mixed common provenance")
    if manifest.source_universe_sha256 != hash_canonical(spec.source_universe_theorem_ids):
        raise DeterministicScaleError(
            f"{role} merged source universe does not match its producer run specs"
        )
    loaded_config = load_config(Path(spec.config_path), DeterministicScaleConfig)
    if loaded_config.config_hash != spec.config_hash:
        raise DeterministicScaleError(f"{role} scale configuration changed after merge")

    partition_dir = resolved / "partitions"
    actual = {path.stem for path in partition_dir.glob("*.jsonl")}
    if actual != set(manifest.partition_sha256):
        raise DeterministicScaleError(f"{role} merged partitions are incomplete or foreign")
    projected: dict[str, tuple[StrictModel, ...]] = {}
    for name, expected_hash in manifest.partition_sha256.items():
        path = partition_dir / f"{name}.jsonl"
        if hash_file(path) != expected_hash:
            raise DeterministicScaleError(f"{role} merged partition hash changed: {path}")
        count = sum(1 for line in path.read_bytes().splitlines() if line.strip())
        if count != manifest.record_counts.get(name):
            raise DeterministicScaleError(f"{role} merged partition count changed: {path}")
        model = _PARTITION_MODELS.get(name)
        if model is not None:
            projected[name] = cast(
                tuple[StrictModel, ...],
                _load_jsonl(path, model),
            )
    missing = set(_PARTITION_MODELS) - set(projected)
    if missing:
        raise DeterministicScaleError(
            f"{role} merged output lacks semantic partitions: {sorted(missing)}"
        )
    return _LoadedPass(
        role=role,
        output_dir=resolved,
        manifest_path=manifest_path,
        manifest=manifest,
        spec=spec,
        config=loaded_config.config,
        projected=projected,
    )


def _validate_family_ownership(
    unary: _LoadedPass,
    n10: _LoadedPass,
) -> dict[str, str]:
    unary_rules = set(unary.config.active_rule_ids)
    n10_rules = set(n10.config.active_rule_ids)
    if not unary_rules or "n10_nearby_theorem" in unary_rules:
        raise DeterministicScaleError("unary pass must own only non-N10 deterministic families")
    if n10_rules != {"n10_nearby_theorem"}:
        raise DeterministicScaleError("global N10 pass must own exactly n10_nearby_theorem")
    if unary_rules & n10_rules:
        raise DeterministicScaleError("two deterministic passes have overlapping family ownership")
    ownership = dict.fromkeys(sorted(unary_rules), "unary")
    ownership["n10_nearby_theorem"] = "global_n10"
    for loaded in (unary, n10):
        variants = cast(Sequence[VariantRecord], loaded.projected["variants"])
        if any(variant.family_id not in set(loaded.config.active_rule_ids) for variant in variants):
            raise DeterministicScaleError(
                f"{loaded.role} output contains a variant owned by the other pass"
            )
    return ownership


def _validate_cross_pass_disjointness(
    unary: _LoadedPass,
    n10: _LoadedPass,
) -> None:
    id_fields = {
        "attempts": "attempt_id",
        "drafts": "draft_id",
        "candidate_theorems": "theorem_id",
        "candidate_representations": "representation_id",
        "audits": "audit_id",
        "variants": "variant_id",
        "pairs": "pair_id",
        "quarantine": "draft_id",
    }
    for partition, field in id_fields.items():
        unary_ids = {getattr(record, field) for record in unary.projected[partition]}
        n10_ids = {getattr(record, field) for record in n10.projected[partition]}
        overlap = unary_ids & n10_ids
        if overlap:
            raise DeterministicScaleError(
                f"two-pass {field} inventories overlap: {sorted(overlap)[:3]}"
            )

    candidate_keys: list[tuple[tuple[str, ...], str]] = []
    for loaded in (unary, n10):
        candidate_by_id = {
            theorem.theorem_id: theorem
            for theorem in cast(
                Sequence[TheoremRecord],
                loaded.projected["candidate_theorems"],
            )
        }
        for variant in cast(Sequence[VariantRecord], loaded.projected["variants"]):
            if variant.derived_theorem_id is None or variant.candidate_code_hash is None:
                raise DeterministicScaleError(
                    f"{loaded.role} deterministic variant lacks its candidate binding"
                )
            candidate = candidate_by_id.get(variant.derived_theorem_id)
            if candidate is None:
                raise DeterministicScaleError(
                    f"{loaded.role} candidate theorem is missing for {variant.variant_id}"
                )
            candidate_keys.append((candidate.root_ancestry_ids, variant.candidate_code_hash))
    if len(candidate_keys) != len(set(candidate_keys)):
        raise DeterministicScaleError(
            "two deterministic passes contain a duplicate ancestry/candidate payload"
        )


def _cap_policy(config: DeterministicScaleConfig) -> tuple[int, int, int | None]:
    return (
        config.max_accepted_variants_per_root_ancestry,
        config.max_accepted_variants_per_family_per_root_ancestry,
        config.max_accepted_variants_per_family,
    )


def _common_config_policy(config: DeterministicScaleConfig) -> dict[str, object]:
    payload = config.model_dump(mode="json")
    payload.pop("active_rule_ids")
    return payload


def _validate_combined_caps(
    unary: _LoadedPass,
    n10: _LoadedPass,
) -> Counter[str]:
    if _cap_policy(unary.config) != _cap_policy(n10.config):
        raise DeterministicScaleError(
            "unary and global-N10 passes use different combined admission caps"
        )
    root_counts: Counter[str] = Counter()
    family_root_counts: Counter[tuple[str, str]] = Counter()
    family_counts: Counter[str] = Counter()
    for loaded in (unary, n10):
        candidate_by_id = {
            theorem.theorem_id: theorem
            for theorem in cast(
                Sequence[TheoremRecord],
                loaded.projected["candidate_theorems"],
            )
        }
        for variant in cast(Sequence[VariantRecord], loaded.projected["variants"]):
            if variant.family_id is None or variant.derived_theorem_id is None:
                raise DeterministicScaleError(
                    f"{loaded.role} deterministic variant lacks family/candidate lineage"
                )
            candidate = candidate_by_id.get(variant.derived_theorem_id)
            if candidate is None:
                raise DeterministicScaleError(
                    f"{loaded.role} candidate theorem is missing for {variant.variant_id}"
                )
            family_counts[variant.family_id] += 1
            for root_id in candidate.root_ancestry_ids:
                root_counts[root_id] += 1
                family_root_counts[(variant.family_id, root_id)] += 1
    root_cap, family_root_cap, family_cap = _cap_policy(unary.config)
    if any(count > root_cap for count in root_counts.values()):
        raise DeterministicScaleError("two-pass output violates the combined per-root cap")
    if any(count > family_root_cap for count in family_root_counts.values()):
        raise DeterministicScaleError("two-pass output violates the combined per-family/root cap")
    if family_cap is not None and any(count > family_cap for count in family_counts.values()):
        raise DeterministicScaleError("two-pass output violates the combined global family cap")
    return family_counts


def combine_deterministic_scale_passes(
    *,
    paths: RepoPaths,
    unary_merged_output_dir: Path,
    n10_merged_output_dir: Path,
    output_dir: Path,
) -> DeterministicScaleCombinedArtifacts:
    """Authorize the exact unary and global-N10 outputs as one data source."""

    unary_dir = unary_merged_output_dir.resolve()
    n10_dir = n10_merged_output_dir.resolve()
    output = output_dir.resolve()
    if unary_dir == n10_dir or output in {unary_dir, n10_dir}:
        raise DeterministicScaleError(
            "unary, global-N10, and combined manifest directories must be distinct"
        )
    unary = _load_pass(paths=paths, role="unary", output_dir=unary_dir)
    n10 = _load_pass(paths=paths, role="global_n10", output_dir=n10_dir)
    unary_identity = _common_input_identity(unary.spec)
    n10_identity = _common_input_identity(n10.spec)
    if unary_identity != n10_identity:
        raise DeterministicScaleError(
            "unary and global-N10 passes do not share exact inventory/code/context provenance"
        )
    if _common_config_policy(unary.config) != _common_config_policy(n10.config):
        raise DeterministicScaleError(
            "unary and global-N10 passes do not share one common execution/admission policy"
        )
    ownership = _validate_family_ownership(unary, n10)
    _validate_cross_pass_disjointness(unary, n10)
    family_counts = _validate_combined_caps(unary, n10)

    bindings = tuple(
        DeterministicScaleCombinedPassBinding(
            role=loaded.role,
            merged_output_dir=str(loaded.output_dir),
            merged_manifest_path=str(loaded.manifest_path),
            merged_manifest_hash=loaded.manifest.merged_manifest_hash,
            merged_manifest_sha256=hash_file(loaded.manifest_path),
            source_universe_sha256=loaded.manifest.source_universe_sha256,
            active_rule_ids=loaded.config.active_rule_ids,
            record_counts=loaded.manifest.record_counts,
            partition_sha256=loaded.manifest.partition_sha256,
        )
        for loaded in (unary, n10)
    )
    combined_counts = {
        name: unary.manifest.record_counts.get(name, 0) + n10.manifest.record_counts.get(name, 0)
        for name in sorted(set(unary.manifest.record_counts) | set(n10.manifest.record_counts))
    }
    root_cap, family_root_cap, family_cap = _cap_policy(unary.config)
    data: dict[str, object] = {
        "schema_version": 1,
        "artifact_kind": "deterministic_scale_two_pass_manifest",
        "pass_bindings": bindings,
        "common_input_identity_hash": hash_canonical(unary_identity),
        "source_universe_count": len(unary.spec.source_universe_theorem_ids),
        "source_universe_sha256": hash_canonical(unary.spec.source_universe_theorem_ids),
        "context_id": unary.spec.context_id,
        "context_record_sha256": unary.spec.context_record_sha256,
        "project_revision": unary.spec.project_revision,
        "project_tree_hash": unary.spec.project_tree_hash,
        "code": unary.spec.code,
        "family_ownership": dict(sorted(ownership.items())),
        "combined_record_counts": combined_counts,
        "combined_family_accepted_counts": dict(sorted(family_counts.items())),
        "max_accepted_variants_per_root_ancestry": root_cap,
        "max_accepted_variants_per_family_per_root_ancestry": family_root_cap,
        "max_accepted_variants_per_family": family_cap,
        "cross_pass_ids_disjoint": True,
        "cross_pass_candidates_disjoint": True,
        "scientific_pairing_eligible": True,
        "output_quality_tier": "provisional",
        "training_eligible": False,
        "created_at": unary.config.record_timestamp_utc,
    }
    hash_payload = {
        **data,
        "pass_bindings": tuple(binding.model_dump(mode="json") for binding in bindings),
        "code": unary.spec.code.model_dump(mode="json"),
        "created_at": TypeAdapter(datetime.datetime).dump_python(
            unary.config.record_timestamp_utc,
            mode="json",
        ),
    }
    combined_hash = hash_canonical(hash_payload)
    manifest = DeterministicScaleCombinedManifest.model_validate(
        {"combined_manifest_hash": combined_hash, **data}
    )
    with _run_lock(output):
        foreign = tuple(
            path
            for path in output.iterdir()
            if path.name != "run.lock" and not path.name.startswith("combined_manifest.")
        )
        if foreign:
            raise DeterministicScaleError(
                f"combined manifest directory contains foreign files: {foreign[:3]}"
            )
        manifest_path = output / f"combined_manifest.{combined_hash}.json"
        manifest_sha256 = _write_new_atomic(
            manifest_path,
            _canonical_model_bytes(manifest),
        )
    return DeterministicScaleCombinedArtifacts(
        output_dir=output,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        combined_manifest_hash=combined_hash,
    )


__all__ = [
    "DeterministicScaleCombinedArtifacts",
    "DeterministicScaleCombinedManifest",
    "DeterministicScaleCombinedPassBinding",
    "combine_deterministic_scale_passes",
]
