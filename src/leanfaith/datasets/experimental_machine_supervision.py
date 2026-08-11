"""Immutable, opt-in machine-supervision corpus for early learning experiments.

This module intentionally does *not* create semantic labels.  It projects
Lean-checked deterministic transformation intentions into a balanced corpus
that can be used only for explicitly enabled experimental smoke training.  It
cannot enter scientific training, model selection, calibration, or evaluation.

The builder consumes the already frozen deterministic provisional-pair audit,
replays its exact source/result lineage, re-applies the active benchmark
denylist, groups all siblings by root ancestry, and publishes an immutable
content-addressed dataset plus a readable public sample.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import (
    ActiveBenchmarkRegistry,
    load_active_benchmark_registry,
)
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.schemas.manifest import CodeState, collect_code_state
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_seed import (
    CompositionSeedManifest,
    CompositionSeedRecord,
)
from leanfaith.transforms.provisional_pair_combine import (
    MaterializationRootBinding,
    ProvisionalPairCombinationManifest,
    ProvisionalPairObservation,
    UniqueProvisionalPair,
    _canonical_line,
    _iter_jsonl_objects,
    _load_canonical_model,
    _parse_json_object,
    _result_model,
)

_HEX64 = r"^[0-9a-f]{64}$"
_DATASET_ID = r"^experimental-machine-supervision:[0-9a-f]{64}$"
_RECORD_ID = r"^experimental-machine-pair:[0-9a-f]{64}$"
_SPLIT_COMPONENT_ID = r"^split-component:[0-9a-f]{64}$"
_OUTPUT_FILES = frozenset(
    {
        "records.jsonl",
        "split_assignments.jsonl",
        "public_sample.jsonl",
        "public_sample.md",
        "summary.json",
        "manifest.json",
    }
)

PseudoTarget = Literal["same_claim", "not_same_claim"]
ExperimentalSplit = Literal["train", "validation", "test"]
ExperimentalPurpose = Literal["smoke_training", "learning_curve"]


class ExperimentalMachineSupervisionError(ValueError):
    """An input, policy guard, selection, or immutable replay failed closed."""


class ExperimentalInputBinding(StrictModel):
    """Exact hash/size binding for one input artifact."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=0)


class ExperimentalMachineSupervisionConfig(StrictModel):
    """Frozen selection policy for one experimental corpus."""

    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1)
    selection_seed: str = Field(min_length=1)
    source_category: Literal["mathlib"] = "mathlib"
    positive_count: int = Field(gt=0)
    negative_count: int = Field(gt=0)
    positive_family_quotas: dict[str, int]
    negative_family_quotas: dict[str, int]
    maximum_variants_per_component: int = Field(default=4, ge=1)
    train_percent: int = Field(default=80, ge=1, le=98)
    validation_percent: int = Field(default=10, ge=1, le=98)
    test_percent: int = Field(default=10, ge=1, le=98)
    public_sample_per_target: int = Field(default=10, ge=1)
    audit_manifest_sha256: str = Field(pattern=_HEX64)
    audit_gross_observations_sha256: str = Field(pattern=_HEX64)
    audit_unique_pairs_sha256: str = Field(pattern=_HEX64)
    positive_seed_manifest_sha256: str = Field(pattern=_HEX64)
    positive_seed_records_sha256: str = Field(pattern=_HEX64)
    benchmark_manifest_sha256: str = Field(pattern=_HEX64)
    benchmark_active_registry_sha256: str = Field(pattern=_HEX64)
    benchmark_authorization_sha256: str = Field(pattern=_HEX64)

    @field_validator("positive_family_quotas", "negative_family_quotas")
    @classmethod
    def _quotas_are_canonical(cls, value: dict[str, int]) -> dict[str, int]:
        if not value or any(not name or count <= 0 for name, count in value.items()):
            raise ValueError("family quotas must have nonempty names and positive counts")
        if list(value) != sorted(value):
            raise ValueError("family quota keys must be sorted")
        return value

    @model_validator(mode="after")
    def _counts_reconcile(self) -> ExperimentalMachineSupervisionConfig:
        if sum(self.positive_family_quotas.values()) != self.positive_count:
            raise ValueError("positive family quotas do not sum to positive_count")
        if sum(self.negative_family_quotas.values()) != self.negative_count:
            raise ValueError("negative family quotas do not sum to negative_count")
        if set(self.positive_family_quotas) & set(self.negative_family_quotas):
            raise ValueError("positive and negative family quota names overlap")
        if any(not name.startswith("p") for name in self.positive_family_quotas):
            raise ValueError("positive family quota names must begin with 'p'")
        if any(not name.startswith("n") for name in self.negative_family_quotas):
            raise ValueError("negative family quota names must begin with 'n'")
        if self.train_percent + self.validation_percent + self.test_percent != 100:
            raise ValueError("experimental split percentages must sum to 100")
        if self.public_sample_per_target > min(self.positive_count, self.negative_count):
            raise ValueError("public sample exceeds the smaller pseudo-target partition")
        return self


def load_experimental_machine_supervision_config(
    path: Path,
) -> LoadedConfig[ExperimentalMachineSupervisionConfig]:
    """Load one strict, content-hashed experimental-corpus policy."""

    return load_config(path, ExperimentalMachineSupervisionConfig)


class ExperimentalStatementView(StrictModel):
    """The two model-visible text views for one theorem side."""

    theorem_id: str = Field(min_length=1)
    representation_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    statement_content_hash: str = Field(pattern=_HEX64)
    representation_content_hash: str = Field(pattern=_HEX64)
    alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    headless: str = Field(min_length=1)
    signature_explicit: str = Field(min_length=1)


class ExperimentalMachineSupervisionRecord(StrictModel):
    """One explicitly provisional pair; never a resolved F1 label."""

    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=_RECORD_ID)
    dataset_profile_id: str = Field(min_length=1)
    unique_pair_id: str = Field(min_length=1)
    pair_id: str = Field(min_length=1)
    exact_pair_key: str = Field(pattern=_HEX64)
    observation_id: str = Field(min_length=1)
    root_binding_id: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    result_line_number: int = Field(ge=1)
    family_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    evidence_class: Literal["E2", "D0"]
    pseudo_target: PseudoTarget
    pseudo_target_basis: Literal["deterministic_transformation_intention"] = (
        "deterministic_transformation_intention"
    )
    intended_relation: Literal["equivalent", "near_miss"]
    source_category: Literal["mathlib"] = "mathlib"
    split_group_ids: tuple[str, ...] = Field(min_length=1)
    split_component_id: str = Field(pattern=_SPLIT_COMPONENT_ID)
    split: ExperimentalSplit
    source: ExperimentalStatementView
    candidate: ExperimentalStatementView
    candidate_code_hash: str = Field(pattern=_HEX64)
    candidate_code_key: str = Field(pattern=_HEX64)
    alpha_candidate_key: str = Field(pattern=_HEX64)
    certificate_kind: str | None = None
    certificate_sha256: str | None = Field(default=None, pattern=_HEX64)
    quality_tier: Literal["provisional"] = "provisional"
    semantic_label_id: None = None
    machine_supervision_only: Literal[True] = True
    experimental_smoke_training_eligible: Literal[True] = True
    scientific_training_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _record_is_coherent(self) -> ExperimentalMachineSupervisionRecord:
        payload = self.model_dump(mode="json", exclude={"record_id"})
        expected = f"experimental-machine-pair:{hash_canonical(payload)}"
        if self.record_id != expected:
            raise ValueError("record_id does not match canonical content")
        if self.split_group_ids != tuple(sorted(set(self.split_group_ids))):
            raise ValueError("split_group_ids must be sorted and unique")
        if self.source.context_id != self.candidate.context_id:
            raise ValueError("source and candidate contexts differ")
        if self.pseudo_target == "same_claim":
            if self.intended_relation != "equivalent" or self.evidence_class != "E2":
                raise ValueError("positive pseudo-target requires an E2 equivalent intention")
            if self.certificate_kind is None or self.certificate_sha256 is None:
                raise ValueError("positive pseudo-target requires a bound certificate")
        else:
            if self.intended_relation != "near_miss" or self.evidence_class != "D0":
                raise ValueError("negative pseudo-target requires a D0 near-miss intention")
            if self.certificate_kind is not None or self.certificate_sha256 is not None:
                raise ValueError("negative pseudo-target cannot claim a positive certificate")
        return self


class ExperimentalSplitAssignment(StrictModel):
    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=_RECORD_ID)
    split_component_id: str = Field(pattern=_SPLIT_COMPONENT_ID)
    split_group_ids: tuple[str, ...] = Field(min_length=1)
    split: ExperimentalSplit
    pseudo_target: PseudoTarget


class ExperimentalMachineSupervisionSummary(StrictModel):
    schema_version: Literal[1] = 1
    dataset_id: str = Field(pattern=_DATASET_ID)
    profile_id: str = Field(min_length=1)
    record_count: int = Field(gt=0)
    counts_by_pseudo_target: dict[str, int]
    counts_by_family: dict[str, int]
    counts_by_split: dict[str, int]
    counts_by_split_and_target: dict[str, int]
    component_count: int = Field(gt=0)
    maximum_observed_variants_per_component: int = Field(ge=1)
    source_theorem_count: int = Field(gt=0)
    benchmark_overlap_excluded_count: int = Field(ge=0)
    duplicate_excluded_counts: dict[str, int]
    public_sample_count: int = Field(gt=0)
    semantic_label_count: Literal[0] = 0
    production_training_ready_count: Literal[0] = 0
    use_note: Literal["experimental machine supervision only; not semantic ground truth"] = (
        "experimental machine supervision only; not semantic ground truth"
    )


class ExperimentalMachineSupervisionManifest(StrictModel):
    """Content-addressed manifest for the exact immutable corpus."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["experimental_machine_supervision_corpus"] = (
        "experimental_machine_supervision_corpus"
    )
    dataset_id: str = Field(pattern=_DATASET_ID)
    profile_id: str = Field(min_length=1)
    config_hash: str = Field(pattern=_HEX64)
    config: ExperimentalMachineSupervisionConfig
    code: CodeState
    inputs: dict[str, ExperimentalInputBinding]
    record_count: int = Field(gt=0)
    component_count: int = Field(gt=0)
    output_sha256: dict[str, str]
    required_opt_in_flag: Literal["--allow-experimental-machine-supervision"] = (
        "--allow-experimental-machine-supervision"
    )
    allowed_purposes: tuple[ExperimentalPurpose, ...] = (
        "learning_curve",
        "smoke_training",
    )
    semantic_labels_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    scientific_training_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    release_claim_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _manifest_is_coherent(self) -> ExperimentalMachineSupervisionManifest:
        if self.profile_id != self.config.profile_id:
            raise ValueError("manifest profile differs from embedded config")
        if self.config_hash != hash_canonical(self.config.model_dump(mode="json")):
            raise ValueError("manifest config_hash differs from embedded config")
        if self.code.git_dirty or self.code.code_tree_hash is None or self.code.untracked_files:
            raise ValueError("experimental corpus requires a clean, fully tracked code tree")
        if self.allowed_purposes != tuple(sorted(set(self.allowed_purposes))):
            raise ValueError("allowed_purposes must be sorted and unique")
        if set(self.output_sha256) != _OUTPUT_FILES - {"manifest.json"}:
            raise ValueError("output_sha256 does not bind the exact non-manifest outputs")
        if any(not name or not value for name, value in self.inputs.items()):
            raise ValueError("manifest inputs must be nonempty")
        return self


@dataclass(frozen=True, slots=True)
class ExperimentalMachineSupervisionArtifacts:
    output_dir: Path
    manifest_path: Path
    records_path: Path
    split_assignments_path: Path
    public_sample_path: Path
    public_sample_markdown_path: Path
    summary_path: Path
    dataset_id: str
    record_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _Candidate:
    unique: UniqueProvisionalPair
    observation: ProvisionalPairObservation
    root: MaterializationRootBinding
    source_theorem: TheoremRecord
    source_representation: RepresentationRecord
    candidate_theorem: TheoremRecord
    candidate_representation: RepresentationRecord
    pseudo_target: PseudoTarget
    evidence_class: Literal["E2", "D0"]
    seed: CompositionSeedRecord | None


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink_components(path: Path, *, allow_missing: bool) -> Path:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:], start=1):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise ExperimentalMachineSupervisionError(
                f"required path is absent: {current}"
            ) from None
        except OSError as exc:
            raise ExperimentalMachineSupervisionError(
                f"cannot inspect path component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ExperimentalMachineSupervisionError(f"path contains a symlink: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ExperimentalMachineSupervisionError(
                f"path parent component is not a directory: {current}"
            )
    return absolute


def _regular_file(path: Path) -> Path:
    safe = _reject_symlink_components(path, allow_missing=False)
    if not safe.is_file():
        raise ExperimentalMachineSupervisionError(f"input is not a regular file: {safe}")
    return safe


def _real_directory(path: Path) -> Path:
    safe = _reject_symlink_components(path, allow_missing=False)
    if not safe.is_dir():
        raise ExperimentalMachineSupervisionError(f"input is not a directory: {safe}")
    return safe


def _binding(path: Path) -> ExperimentalInputBinding:
    safe = _regular_file(path)
    return ExperimentalInputBinding(
        path=str(safe),
        sha256=hash_file(safe),
        byte_count=safe.stat().st_size,
    )


def _require_hash(path: Path, expected: str, *, field: str) -> Path:
    safe = _regular_file(path)
    observed = hash_file(safe)
    if observed != expected:
        raise ExperimentalMachineSupervisionError(
            f"{field} hash differs: expected {expected}, observed {observed}"
        )
    return safe


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_line(record) for record in records)


def _load_canonical_jsonl[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
) -> tuple[ModelT, ...]:
    records: list[ModelT] = []
    try:
        rows = _iter_jsonl_objects(path)
        for line_number, raw, raw_line in rows:
            try:
                record = model.model_validate(raw)
            except ValueError as exc:
                raise ExperimentalMachineSupervisionError(
                    f"invalid {model.__name__} at {path}:{line_number}: {exc}"
                ) from exc
            if raw_line != _canonical_line(record):
                raise ExperimentalMachineSupervisionError(
                    f"non-canonical {model.__name__} at {path}:{line_number}"
                )
            records.append(record)
    except ExperimentalMachineSupervisionError:
        raise
    except Exception as exc:
        raise ExperimentalMachineSupervisionError(f"cannot load JSONL {path}: {exc}") from exc
    return tuple(records)


def _load_audit_inputs(
    audit_dir: Path,
    *,
    config: ExperimentalMachineSupervisionConfig,
) -> tuple[
    ProvisionalPairCombinationManifest,
    tuple[ProvisionalPairObservation, ...],
    tuple[UniqueProvisionalPair, ...],
]:
    root = _real_directory(audit_dir)
    manifest_path = _require_hash(
        root / "manifest.json",
        config.audit_manifest_sha256,
        field="audit manifest",
    )
    gross_path = _require_hash(
        root / "gross_observations.jsonl",
        config.audit_gross_observations_sha256,
        field="audit gross observations",
    )
    unique_path = _require_hash(
        root / "unique_pairs.jsonl",
        config.audit_unique_pairs_sha256,
        field="audit unique pairs",
    )
    try:
        manifest = _load_canonical_model(manifest_path, ProvisionalPairCombinationManifest)
    except Exception as exc:
        raise ExperimentalMachineSupervisionError(f"invalid audit manifest: {exc}") from exc
    if (
        manifest.gross_output_sha256 != config.audit_gross_observations_sha256
        or manifest.unique_output_sha256 != config.audit_unique_pairs_sha256
    ):
        raise ExperimentalMachineSupervisionError("audit manifest does not bind its partitions")
    gross = _load_canonical_jsonl(gross_path, ProvisionalPairObservation)
    unique = _load_canonical_jsonl(unique_path, UniqueProvisionalPair)
    if len(gross) != manifest.gross_observation_count:
        raise ExperimentalMachineSupervisionError("gross audit count differs from manifest")
    if len(unique) != manifest.unique_pair_count:
        raise ExperimentalMachineSupervisionError("unique audit count differs from manifest")
    return manifest, gross, unique


def _load_positive_seeds(
    seed_dir: Path,
    *,
    config: ExperimentalMachineSupervisionConfig,
) -> tuple[CompositionSeedManifest, dict[str, CompositionSeedRecord]]:
    root = _real_directory(seed_dir)
    manifest_path = _require_hash(
        root / "manifest.json",
        config.positive_seed_manifest_sha256,
        field="positive-seed manifest",
    )
    seeds_path = _require_hash(
        root / "seeds.jsonl",
        config.positive_seed_records_sha256,
        field="positive-seed records",
    )
    try:
        manifest = _load_canonical_model(manifest_path, CompositionSeedManifest)
    except Exception as exc:
        raise ExperimentalMachineSupervisionError(f"invalid seed manifest: {exc}") from exc
    if manifest.seed_output_sha256 != config.positive_seed_records_sha256:
        raise ExperimentalMachineSupervisionError("seed manifest does not bind seeds.jsonl")
    seeds = _load_canonical_jsonl(seeds_path, CompositionSeedRecord)
    if len(seeds) != manifest.seed_count:
        raise ExperimentalMachineSupervisionError("seed count differs from seed manifest")
    by_pair: dict[str, CompositionSeedRecord] = {}
    for seed in seeds:
        if seed.unique_pair_id in by_pair:
            raise ExperimentalMachineSupervisionError(
                f"duplicate positive seed unique_pair_id: {seed.unique_pair_id}"
            )
        by_pair[seed.unique_pair_id] = seed
    return manifest, by_pair


def _load_benchmark_registry(
    repo_root: Path,
    *,
    config: ExperimentalMachineSupervisionConfig,
) -> ActiveBenchmarkRegistry:
    authorization = _require_hash(
        repo_root / "reports/gates/lf_016_authorization.json",
        config.benchmark_authorization_sha256,
        field="benchmark authorization",
    )
    manifest_path = repo_root / "data/benchmarks/manifests/representation_signatures_v1.json"
    registry = load_active_benchmark_registry(
        manifest_path,
        repo_root=repo_root,
        expected_manifest_sha256=config.benchmark_manifest_sha256,
        authorization_path=authorization,
    )
    if registry.manifest.active_registry.sha256 != config.benchmark_active_registry_sha256:
        raise ExperimentalMachineSupervisionError(
            "active benchmark registry hash differs from frozen config"
        )
    return registry


@dataclass(frozen=True, slots=True)
class _CandidateLocator:
    unique: UniqueProvisionalPair
    observation: ProvisionalPairObservation
    root: MaterializationRootBinding
    pseudo_target: PseudoTarget
    evidence_class: Literal["E2", "D0"]
    seed: CompositionSeedRecord | None


def _candidate_locators(
    manifest: ProvisionalPairCombinationManifest,
    gross: Sequence[ProvisionalPairObservation],
    unique: Sequence[UniqueProvisionalPair],
    seeds: Mapping[str, CompositionSeedRecord],
    *,
    config: ExperimentalMachineSupervisionConfig,
) -> tuple[_CandidateLocator, ...]:
    observation_by_id = {item.observation_id: item for item in gross}
    if len(observation_by_id) != len(gross):
        raise ExperimentalMachineSupervisionError("gross audit has duplicate observation IDs")
    roots = {item.root_binding_id: item for item in manifest.root_bindings}
    if len(roots) != len(manifest.root_bindings):
        raise ExperimentalMachineSupervisionError("audit manifest has duplicate root bindings")
    allowed_positive = set(config.positive_family_quotas)
    allowed_negative = set(config.negative_family_quotas)
    locators: list[_CandidateLocator] = []
    for item in unique:
        if item.conflicting_intentions or item.source_categories != (config.source_category,):
            continue
        if len(item.family_ids) != 1 or len(item.intended_relations) != 1:
            continue
        family = item.family_ids[0]
        seed = seeds.get(item.unique_pair_id)
        if family in allowed_positive:
            if item.intended_relations != ("equivalent",) or seed is None:
                continue
            if (
                seed.input_combination_hash != manifest.combination_hash
                or seed.exact_pair_key != item.exact_pair_key
                or seed.first_hop_family_id != family
            ):
                raise ExperimentalMachineSupervisionError(
                    f"positive seed lineage disagrees with {item.unique_pair_id}"
                )
            selected_id = seed.selected_observation_id
            pseudo_target: PseudoTarget = "same_claim"
            evidence_class: Literal["E2", "D0"] = "E2"
        elif family in allowed_negative:
            if item.intended_relations != ("near_miss",) or seed is not None:
                continue
            selected_id = min(item.observation_ids)
            pseudo_target = "not_same_claim"
            evidence_class = "D0"
        else:
            continue
        if selected_id not in item.observation_ids:
            raise ExperimentalMachineSupervisionError(
                f"selected observation is absent from {item.unique_pair_id}"
            )
        observation = observation_by_id.get(selected_id)
        if observation is None:
            raise ExperimentalMachineSupervisionError(f"missing selected observation {selected_id}")
        if (
            observation.family_id != family
            or observation.exact_pair_key != item.exact_pair_key
            or observation.source_categories != (config.source_category,)
            or len(observation.source_theorem_ids) != 1
            or len(observation.source_representation_ids) != 1
            or observation.candidate_alpha_identity_fingerprint is None
            or observation.alpha_candidate_key is None
        ):
            raise ExperimentalMachineSupervisionError(
                f"selected observation disagrees with {item.unique_pair_id}"
            )
        root = roots.get(observation.root_binding_id)
        if root is None:
            raise ExperimentalMachineSupervisionError(
                f"observation references an unknown root: {observation.root_binding_id}"
            )
        if evidence_class == "E2" and root.run_kind != "e2":
            raise ExperimentalMachineSupervisionError("positive seed does not bind an E2 root")
        if seed is not None and (
            seed.first_hop_root_binding_id != observation.root_binding_id
            or seed.first_hop_result_id != observation.result_id
            or seed.first_hop_result_line_number != observation.result_line_number
            or seed.first_hop_rule_id != observation.rule_id
            or seed.first_hop_attempt_id != observation.attempt_id
            or seed.first_hop_draft_id != observation.draft_id
            or seed.first_hop_audit_id != observation.audit_id
            or seed.first_hop_variant_id != observation.variant_id
            or seed.source_theorem_id != observation.source_theorem_ids[0]
            or seed.source_representation_id != observation.source_representation_ids[0]
            or seed.intermediate_theorem_id != observation.candidate_theorem_id
            or seed.intermediate_representation_id != observation.candidate_representation_id
            or seed.root_ancestry_ids != observation.source_root_ancestry_ids
            or seed.intermediate_candidate_code_hash != observation.candidate_code_hash
            or seed.intermediate_alpha_identity_fingerprint
            != observation.candidate_alpha_identity_fingerprint
        ):
            raise ExperimentalMachineSupervisionError(
                f"positive seed receipt disagrees with {item.unique_pair_id}"
            )
        if evidence_class == "D0" and root.run_kind != "d0":
            raise ExperimentalMachineSupervisionError("negative intention does not bind a D0 root")
        locators.append(
            _CandidateLocator(
                unique=item,
                observation=observation,
                root=root,
                pseudo_target=pseudo_target,
                evidence_class=evidence_class,
                seed=seed,
            )
        )
    return tuple(locators)


def _verified_result_payloads(
    locators: Sequence[_CandidateLocator],
) -> dict[str, dict[str, object]]:
    by_root: dict[str, dict[int, _CandidateLocator]] = defaultdict(dict)
    roots: dict[str, MaterializationRootBinding] = {}
    for locator in locators:
        line = locator.observation.result_line_number
        root_id = locator.root.root_binding_id
        prior = by_root[root_id].get(line)
        if prior is not None and prior.observation.result_id != locator.observation.result_id:
            raise ExperimentalMachineSupervisionError("two results claim one root line")
        by_root[root_id][line] = locator
        roots[root_id] = locator.root

    payloads: dict[str, dict[str, object]] = {}
    for root_id in sorted(by_root):
        binding = roots[root_id]
        root = _real_directory(Path(binding.root_path))
        for file_binding in (binding.run_spec, binding.manifest, binding.results):
            path = _require_hash(
                root / file_binding.relative_path,
                file_binding.sha256,
                field=f"{root_id}:{file_binding.relative_path}",
            )
            if path.stat().st_size != file_binding.byte_count:
                raise ExperimentalMachineSupervisionError(
                    f"bound file size differs: {root_id}:{file_binding.relative_path}"
                )
        required = by_root[root_id]
        result_path = root / binding.results.relative_path
        result_type = _result_model(binding.run_kind)
        found: set[int] = set()
        with result_path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                required_locator = required.get(line_number)
                if required_locator is None:
                    continue
                raw = _parse_json_object(raw_line, path=result_path)
                try:
                    result = result_type.model_validate(raw)
                except ValueError as exc:
                    raise ExperimentalMachineSupervisionError(
                        f"invalid bound result at {result_path}:{line_number}: {exc}"
                    ) from exc
                if raw_line != _canonical_line(result):
                    raise ExperimentalMachineSupervisionError(
                        f"non-canonical bound result at {result_path}:{line_number}"
                    )
                payload = cast(dict[str, object], result.model_dump(mode="json"))
                if payload.get("result_id") != required_locator.observation.result_id:
                    raise ExperimentalMachineSupervisionError(
                        f"result ID differs at {result_path}:{line_number}"
                    )
                payloads[required_locator.observation.observation_id] = payload
                found.add(line_number)
        if found != set(required):
            missing = sorted(set(required) - found)
            raise ExperimentalMachineSupervisionError(
                f"bound results are missing lines in {root}: {missing[:10]}"
            )
    if len(payloads) != len(locators):
        raise ExperimentalMachineSupervisionError("result payload count does not reconcile")
    return payloads


def _partition_targets(
    locators: Sequence[_CandidateLocator],
) -> tuple[
    dict[tuple[str, str], set[str]],
    dict[tuple[str, str], set[str]],
]:
    theorem_targets: dict[tuple[str, str], set[str]] = defaultdict(set)
    representation_targets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for locator in locators:
        observation = locator.observation
        if (
            len(observation.source_theorem_ids) != 1
            or len(observation.source_representation_ids) != 1
        ):
            raise ExperimentalMachineSupervisionError("experimental builder requires unary pairs")
        theorem_targets[
            (
                locator.root.theorem_partition_path,
                locator.root.theorem_partition_sha256,
            )
        ].add(observation.source_theorem_ids[0])
        representation_targets[
            (
                locator.root.representation_partition_path,
                locator.root.representation_partition_sha256,
            )
        ].add(observation.source_representation_ids[0])
    return theorem_targets, representation_targets


def _load_source_theorems(
    targets: Mapping[tuple[str, str], set[str]],
) -> dict[str, TheoremRecord]:
    output: dict[str, TheoremRecord] = {}
    for path_value, expected_hash in sorted(targets):
        path = _require_hash(Path(path_value), expected_hash, field="source theorem partition")
        wanted = targets[(path_value, expected_hash)]
        found: set[str] = set()
        for line_number, raw, _raw_line in _iter_jsonl_objects(path):
            payload = raw.get("theorem", raw)
            if not isinstance(payload, dict):
                raise ExperimentalMachineSupervisionError(
                    f"invalid wrapped theorem at {path}:{line_number}"
                )
            theorem_id = payload.get("theorem_id")
            if theorem_id not in wanted:
                continue
            try:
                theorem = TheoremRecord.model_validate(payload)
            except ValueError as exc:
                raise ExperimentalMachineSupervisionError(
                    f"invalid source theorem at {path}:{line_number}: {exc}"
                ) from exc
            prior = output.get(theorem.theorem_id)
            if prior is not None and prior != theorem:
                raise ExperimentalMachineSupervisionError(
                    f"source theorem differs across partitions: {theorem.theorem_id}"
                )
            output[theorem.theorem_id] = theorem
            found.add(theorem.theorem_id)
        if found != wanted:
            raise ExperimentalMachineSupervisionError(
                f"source theorem partition is missing {len(wanted - found)} selected records"
            )
    return output


def _load_source_representations(
    targets: Mapping[tuple[str, str], set[str]],
) -> dict[str, RepresentationRecord]:
    output: dict[str, RepresentationRecord] = {}
    for path_value, expected_hash in sorted(targets):
        path = _require_hash(
            Path(path_value), expected_hash, field="source representation partition"
        )
        wanted = targets[(path_value, expected_hash)]
        found: set[str] = set()
        for line_number, raw, _raw_line in _iter_jsonl_objects(path):
            representation_id = raw.get("representation_id")
            if representation_id not in wanted:
                continue
            try:
                representation = RepresentationRecord.model_validate(raw)
            except ValueError as exc:
                raise ExperimentalMachineSupervisionError(
                    f"invalid source representation at {path}:{line_number}: {exc}"
                ) from exc
            prior = output.get(representation.representation_id)
            if prior is not None and prior != representation:
                raise ExperimentalMachineSupervisionError(
                    "source representation differs across partitions: "
                    f"{representation.representation_id}"
                )
            output[representation.representation_id] = representation
            found.add(representation.representation_id)
        if found != wanted:
            raise ExperimentalMachineSupervisionError(
                f"source representation partition is missing {len(wanted - found)} selected records"
            )
    return output


def _join_candidates(
    locators: Sequence[_CandidateLocator],
    payloads: Mapping[str, dict[str, object]],
) -> tuple[_Candidate, ...]:
    theorem_targets, representation_targets = _partition_targets(locators)
    source_theorems = _load_source_theorems(theorem_targets)
    source_representations = _load_source_representations(representation_targets)
    joined: list[_Candidate] = []
    for locator in locators:
        observation = locator.observation
        payload = payloads[observation.observation_id]
        candidate_theorem_raw = payload.get("candidate_theorem")
        candidate_representation_raw = payload.get("candidate_representation")
        if not isinstance(candidate_theorem_raw, dict) or not isinstance(
            candidate_representation_raw, dict
        ):
            raise ExperimentalMachineSupervisionError("bound result lacks candidate records")
        try:
            candidate_theorem = TheoremRecord.model_validate(candidate_theorem_raw)
            candidate_representation = RepresentationRecord.model_validate(
                candidate_representation_raw
            )
        except ValueError as exc:
            raise ExperimentalMachineSupervisionError(
                f"invalid candidate record for {observation.observation_id}: {exc}"
            ) from exc
        source_theorem = source_theorems[observation.source_theorem_ids[0]]
        source_representation = source_representations[observation.source_representation_ids[0]]
        if (
            candidate_theorem.theorem_id != observation.candidate_theorem_id
            or candidate_representation.representation_id != observation.candidate_representation_id
            or candidate_theorem.statement_content_hash != observation.candidate_code_hash
            or candidate_representation.alpha_identity_fingerprint
            != observation.candidate_alpha_identity_fingerprint
            or source_theorem.theorem_id != source_representation.theorem_id
            or candidate_theorem.theorem_id != candidate_representation.theorem_id
            or source_theorem.root_ancestry_ids != observation.source_root_ancestry_ids
            or candidate_theorem.root_ancestry_ids != observation.source_root_ancestry_ids
        ):
            raise ExperimentalMachineSupervisionError(
                f"joined theorem lineage differs for {observation.observation_id}"
            )
        if source_theorem.source != "mathlib":
            raise ExperimentalMachineSupervisionError("public experimental source is not mathlib")
        joined.append(
            _Candidate(
                unique=locator.unique,
                observation=observation,
                root=locator.root,
                source_theorem=source_theorem,
                source_representation=source_representation,
                candidate_theorem=candidate_theorem,
                candidate_representation=candidate_representation,
                pseudo_target=locator.pseudo_target,
                evidence_class=locator.evidence_class,
                seed=locator.seed,
            )
        )
    return tuple(joined)


def _representation_is_protected(
    registry: ActiveBenchmarkRegistry,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
) -> bool:
    row_ids = (
        theorem.theorem_id,
        theorem.source_record,
        theorem.source_record_id,
        theorem.upstream_uuid,
    )
    if any(value is not None and registry.index.contains_row_id(value) for value in row_ids):
        return True
    if registry.index.contains_lean(theorem.proof_stripped_declaration):
        return True
    for view_name in ("headless", "signature_pp", "signature_explicit"):
        value = getattr(representation, view_name)
        if value is None:
            continue
        if registry.index.contains_lean(value):
            return True
        if registry.index.contains_representation(signature_near_dup_hash(value)):
            return True
    alpha = representation.alpha_identity_fingerprint
    return alpha is not None and registry.index.contains_representation(alpha)


def _screen_candidates(
    candidates: Sequence[_Candidate],
    registry: ActiveBenchmarkRegistry,
) -> tuple[tuple[_Candidate, ...], Counter[str]]:
    accepted: list[_Candidate] = []
    exclusions: Counter[str] = Counter()
    seen_exact: set[str] = set()
    seen_code: set[str] = set()
    seen_alpha: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: item.unique.unique_pair_id):
        source_representation = candidate.source_representation
        candidate_representation = candidate.candidate_representation
        if (
            source_representation.headless is None
            or source_representation.signature_explicit is None
            or source_representation.alpha_identity_fingerprint is None
            or candidate_representation.headless is None
            or candidate_representation.signature_explicit is None
            or candidate_representation.alpha_identity_fingerprint is None
        ):
            exclusions["missing_required_model_view"] += 1
            continue
        if _representation_is_protected(
            registry, candidate.source_theorem, source_representation
        ) or _representation_is_protected(
            registry, candidate.candidate_theorem, candidate_representation
        ):
            exclusions["benchmark_overlap"] += 1
            continue
        observation = candidate.observation
        assert observation.alpha_candidate_key is not None
        if observation.exact_pair_key in seen_exact:
            exclusions["exact_pair_duplicate"] += 1
            continue
        if observation.candidate_code_key in seen_code:
            exclusions["candidate_code_duplicate"] += 1
            continue
        if observation.alpha_candidate_key in seen_alpha:
            exclusions["alpha_candidate_duplicate"] += 1
            continue
        seen_exact.add(observation.exact_pair_key)
        seen_code.add(observation.candidate_code_key)
        seen_alpha.add(observation.alpha_candidate_key)
        accepted.append(candidate)
    return tuple(accepted), exclusions


def _component_id(groups: Sequence[str]) -> str:
    return "split-component:" + hash_canonical(
        {
            "schema": "leanfaith_split_component_v1",
            "split_group_ids": sorted(set(groups)),
        }
    )


def _union_component_ids(
    items: Sequence[tuple[str, Sequence[str]]],
) -> dict[str, str]:
    """Assign atomic connected components over overlapping ancestry groups."""

    parent: dict[str, str] = {}

    def find(value: str) -> str:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            next_value = parent[value]
            parent[value] = root
            value = next_value
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        low, high = sorted((left_root, right_root))
        parent[high] = low

    seen_ids: set[str] = set()
    for item_id, raw_groups in items:
        if item_id in seen_ids:
            raise ExperimentalMachineSupervisionError(
                f"duplicate item in ancestry component construction: {item_id}"
            )
        seen_ids.add(item_id)
        groups = tuple(sorted(set(raw_groups)))
        if not groups:
            raise ExperimentalMachineSupervisionError(f"item has no ancestry groups: {item_id}")
        for group in groups:
            parent.setdefault(group, group)
        for group in groups[1:]:
            union(groups[0], group)

    members_by_root: dict[str, set[str]] = defaultdict(set)
    for group in parent:
        members_by_root[find(group)].add(group)
    component_by_root = {
        root: _component_id(tuple(sorted(members))) for root, members in members_by_root.items()
    }
    output: dict[str, str] = {}
    for item_id, raw_groups in items:
        first = min(raw_groups)
        output[item_id] = component_by_root[find(first)]
    return output


def _candidate_component_ids(candidates: Sequence[_Candidate]) -> dict[str, str]:
    return _union_component_ids(
        tuple(
            (
                candidate.observation.observation_id,
                candidate.observation.source_root_ancestry_ids,
            )
            for candidate in candidates
        )
    )


def _candidate_order_key(candidate: _Candidate, *, seed: str) -> str:
    return hash_canonical(
        {
            "schema": "experimental_machine_supervision_selection_v1",
            "seed": seed,
            "unique_pair_id": candidate.unique.unique_pair_id,
            "observation_id": candidate.observation.observation_id,
        }
    )


def _select_quotas(
    candidates: Sequence[_Candidate],
    *,
    config: ExperimentalMachineSupervisionConfig,
    component_ids: Mapping[str, str] | None = None,
) -> tuple[_Candidate, ...]:
    effective_components = (
        _candidate_component_ids(candidates) if component_ids is None else component_ids
    )
    if set(effective_components) != {
        candidate.observation.observation_id for candidate in candidates
    }:
        raise ExperimentalMachineSupervisionError(
            "candidate component map does not cover exactly the screened candidates"
        )
    by_key: dict[tuple[PseudoTarget, str], list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_key[(candidate.pseudo_target, candidate.observation.family_id)].append(candidate)
    for items in by_key.values():
        items.sort(key=lambda item: _candidate_order_key(item, seed=config.selection_seed))

    quota_tasks: list[tuple[float, PseudoTarget, str, int]] = []
    quota_sources: tuple[tuple[PseudoTarget, Mapping[str, int]], ...] = (
        ("same_claim", config.positive_family_quotas),
        ("not_same_claim", config.negative_family_quotas),
    )
    for target, quotas in quota_sources:
        for family, quota in quotas.items():
            available = len(by_key[(target, family)])
            if available < quota:
                raise ExperimentalMachineSupervisionError(
                    f"family {family} has {available} clean candidates, requires {quota}"
                )
            quota_tasks.append((available / quota, target, family, quota))
    # Scarce families are allocated first so shared ancestry/candidate caps
    # cannot be consumed by abundant families.
    quota_tasks.sort(key=lambda item: (item[0], item[1], item[2]))

    selected: list[_Candidate] = []
    component_counts: Counter[str] = Counter()
    seen_code: set[str] = set()
    seen_alpha: set[str] = set()
    for _ratio, target, family, quota in quota_tasks:
        admitted = 0
        for candidate in by_key[(target, family)]:
            observation = candidate.observation
            assert observation.alpha_candidate_key is not None
            component = effective_components[observation.observation_id]
            if component_counts[component] >= config.maximum_variants_per_component:
                continue
            if observation.candidate_code_key in seen_code:
                continue
            if observation.alpha_candidate_key in seen_alpha:
                continue
            selected.append(candidate)
            component_counts[component] += 1
            seen_code.add(observation.candidate_code_key)
            seen_alpha.add(observation.alpha_candidate_key)
            admitted += 1
            if admitted == quota:
                break
        if admitted != quota:
            raise ExperimentalMachineSupervisionError(
                f"family {family} admits {admitted}/{quota} after component/dedup caps"
            )
    expected = config.positive_count + config.negative_count
    if len(selected) != expected:
        raise ExperimentalMachineSupervisionError(
            f"selected {len(selected)} records, expected {expected}"
        )
    return tuple(sorted(selected, key=lambda item: item.unique.unique_pair_id))


def _split_for_component(
    component_id: str,
    *,
    config: ExperimentalMachineSupervisionConfig,
) -> ExperimentalSplit:
    bucket = (
        int(
            hash_canonical(
                {
                    "schema": "experimental_machine_supervision_split_v1",
                    "seed": config.selection_seed,
                    "component_id": component_id,
                }
            )[:8],
            16,
        )
        % 100
    )
    if bucket < config.train_percent:
        return "train"
    if bucket < config.train_percent + config.validation_percent:
        return "validation"
    return "test"


def _statement_view(
    theorem: TheoremRecord,
    representation: RepresentationRecord,
) -> ExperimentalStatementView:
    if (
        representation.headless is None
        or representation.signature_explicit is None
        or representation.alpha_identity_fingerprint is None
    ):
        raise ExperimentalMachineSupervisionError(
            f"required model view is absent: {representation.representation_id}"
        )
    return ExperimentalStatementView(
        theorem_id=theorem.theorem_id,
        representation_id=representation.representation_id,
        context_id=theorem.context_id,
        statement_content_hash=theorem.statement_content_hash,
        representation_content_hash=representation.content_hash,
        alpha_identity_fingerprint=representation.alpha_identity_fingerprint,
        headless=representation.headless,
        signature_explicit=representation.signature_explicit,
    )


def _build_records(
    candidates: Sequence[_Candidate],
    *,
    config: ExperimentalMachineSupervisionConfig,
    component_ids: Mapping[str, str],
) -> tuple[ExperimentalMachineSupervisionRecord, ...]:
    records: list[ExperimentalMachineSupervisionRecord] = []
    for candidate in candidates:
        observation = candidate.observation
        try:
            component = component_ids[observation.observation_id]
        except KeyError as exc:
            raise ExperimentalMachineSupervisionError(
                f"missing ancestry component for {observation.observation_id}"
            ) from exc
        data: dict[str, object] = {
            "dataset_profile_id": config.profile_id,
            "unique_pair_id": candidate.unique.unique_pair_id,
            "pair_id": observation.pair_id,
            "exact_pair_key": observation.exact_pair_key,
            "observation_id": observation.observation_id,
            "root_binding_id": observation.root_binding_id,
            "result_id": observation.result_id,
            "result_line_number": observation.result_line_number,
            "family_id": observation.family_id,
            "rule_id": observation.rule_id,
            "evidence_class": candidate.evidence_class,
            "pseudo_target": candidate.pseudo_target,
            "intended_relation": observation.intended_relation.value,
            "split_group_ids": observation.source_root_ancestry_ids,
            "split_component_id": component,
            "split": _split_for_component(component, config=config),
            "source": _statement_view(
                candidate.source_theorem, candidate.source_representation
            ).model_dump(mode="json"),
            "candidate": _statement_view(
                candidate.candidate_theorem, candidate.candidate_representation
            ).model_dump(mode="json"),
            "candidate_code_hash": observation.candidate_code_hash,
            "candidate_code_key": observation.candidate_code_key,
            "alpha_candidate_key": observation.alpha_candidate_key,
            "certificate_kind": (
                None if candidate.seed is None else candidate.seed.certificate_kind
            ),
            "certificate_sha256": (
                None if candidate.seed is None else candidate.seed.certificate_sha256
            ),
        }
        placeholder = ExperimentalMachineSupervisionRecord.model_construct(
            _fields_set=None,
            record_id=f"experimental-machine-pair:{'0' * 64}",
            **data,
        )
        payload = placeholder.model_dump(mode="json", exclude={"record_id"})
        records.append(
            ExperimentalMachineSupervisionRecord.model_validate(
                {
                    "record_id": f"experimental-machine-pair:{hash_canonical(payload)}",
                    **data,
                }
            )
        )
    return tuple(sorted(records, key=lambda item: item.record_id))


def _dataset_id(
    *,
    config_hash: str,
    code_tree_hash: str,
    inputs: Mapping[str, ExperimentalInputBinding],
    records: Sequence[ExperimentalMachineSupervisionRecord],
) -> str:
    return "experimental-machine-supervision:" + hash_canonical(
        {
            "schema": "experimental_machine_supervision_dataset_v1",
            "config_hash": config_hash,
            "code_tree_hash": code_tree_hash,
            "inputs": {
                name: binding.model_dump(mode="json") for name, binding in sorted(inputs.items())
            },
            "record_ids": [record.record_id for record in records],
        }
    )


def _summary(
    records: Sequence[ExperimentalMachineSupervisionRecord],
    *,
    dataset_id: str,
    config: ExperimentalMachineSupervisionConfig,
    exclusions: Mapping[str, int],
) -> ExperimentalMachineSupervisionSummary:
    by_target = Counter(record.pseudo_target for record in records)
    by_family = Counter(record.family_id for record in records)
    by_split = Counter(record.split for record in records)
    by_split_target = Counter(f"{record.split}:{record.pseudo_target}" for record in records)
    by_component = Counter(record.split_component_id for record in records)
    return ExperimentalMachineSupervisionSummary(
        dataset_id=dataset_id,
        profile_id=config.profile_id,
        record_count=len(records),
        counts_by_pseudo_target=dict(sorted(by_target.items())),
        counts_by_family=dict(sorted(by_family.items())),
        counts_by_split=dict(sorted(by_split.items())),
        counts_by_split_and_target=dict(sorted(by_split_target.items())),
        component_count=len(by_component),
        maximum_observed_variants_per_component=max(by_component.values()),
        source_theorem_count=len({record.source.theorem_id for record in records}),
        benchmark_overlap_excluded_count=exclusions.get("benchmark_overlap", 0),
        duplicate_excluded_counts={
            name: count for name, count in sorted(exclusions.items()) if "duplicate" in name
        },
        public_sample_count=2 * config.public_sample_per_target,
    )


def _public_sample(
    records: Sequence[ExperimentalMachineSupervisionRecord],
    *,
    config: ExperimentalMachineSupervisionConfig,
) -> tuple[ExperimentalMachineSupervisionRecord, ...]:
    output: list[ExperimentalMachineSupervisionRecord] = []
    for target in ("same_claim", "not_same_claim"):
        partition = sorted(
            (record for record in records if record.pseudo_target == target),
            key=lambda record: hash_canonical(
                {
                    "schema": "experimental_machine_supervision_public_sample_v1",
                    "seed": config.selection_seed,
                    "record_id": record.record_id,
                }
            ),
        )
        output.extend(partition[: config.public_sample_per_target])
    return tuple(sorted(output, key=lambda record: (record.pseudo_target, record.record_id)))


def _markdown_statement(value: str, *, limit: int = 2400) -> str:
    normalized = value.replace("```", "`` `")
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "\n-- [display truncated; JSONL contains the full statement]"


def _public_sample_markdown(
    sample: Sequence[ExperimentalMachineSupervisionRecord],
    *,
    dataset_id: str,
) -> bytes:
    lines = [
        "# LeanFaith experimental machine-supervision sample",
        "",
        f"Dataset: `{dataset_id}`",
        "",
        "> These are deterministic transformation intentions, not resolved semantic labels.",
        "> They are suitable only for opt-in smoke training and learning-curve experiments.",
        "",
    ]
    for index, record in enumerate(sample, start=1):
        lines.extend(
            [
                f"## {index}. {record.pseudo_target} — {record.family_id}",
                "",
                f"Record: `{record.record_id}`  ",
                f"Split: `{record.split}`  ",
                f"Evidence class: `{record.evidence_class}`",
                "",
                "### Reference statement",
                "",
                "```lean",
                _markdown_statement(record.source.headless),
                "```",
                "",
                "### Candidate statement",
                "",
                "```lean",
                _markdown_statement(record.candidate.headless),
                "```",
                "",
            ]
        )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _verify_existing_output(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    safe = _real_directory(output_dir)
    if {path.name for path in safe.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalMachineSupervisionError("existing output file set is not exact")
    for name, expected in payloads.items():
        path = _regular_file(safe / name)
        if path.read_bytes() != expected:
            raise ExperimentalMachineSupervisionError(
                f"existing experimental output differs: {path}"
            )
    return True


def _write_or_replay(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    if set(payloads) != _OUTPUT_FILES:
        raise ExperimentalMachineSupervisionError("output payload set is not exact")
    safe_output = _reject_symlink_components(output_dir, allow_missing=True)
    if safe_output.exists():
        return _verify_existing_output(safe_output, payloads)
    safe_output.parent.mkdir(parents=True, exist_ok=True)
    _real_directory(safe_output.parent)
    safe_output = _reject_symlink_components(safe_output, allow_missing=True)
    if safe_output.exists():
        return _verify_existing_output(safe_output, payloads)
    temporary = Path(tempfile.mkdtemp(prefix=f".{safe_output.name}.", dir=safe_output.parent))
    try:
        for name, payload in sorted(payloads.items()):
            path = temporary / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        os.rename(temporary, safe_output)
    except FileExistsError:
        if temporary.exists():
            shutil.rmtree(temporary)
        return _verify_existing_output(safe_output, payloads)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return False


def _input_bindings(
    *,
    audit_dir: Path,
    seed_dir: Path,
    benchmark: ActiveBenchmarkRegistry,
    repo_root: Path,
) -> dict[str, ExperimentalInputBinding]:
    return {
        "audit_gross_observations": _binding(audit_dir / "gross_observations.jsonl"),
        "audit_manifest": _binding(audit_dir / "manifest.json"),
        "audit_unique_pairs": _binding(audit_dir / "unique_pairs.jsonl"),
        "benchmark_active_registry": _binding(benchmark.active_registry_path),
        "benchmark_authorization": _binding(repo_root / "reports/gates/lf_016_authorization.json"),
        "benchmark_manifest": _binding(benchmark.manifest_path),
        "positive_seed_manifest": _binding(seed_dir / "manifest.json"),
        "positive_seed_records": _binding(seed_dir / "seeds.jsonl"),
    }


def freeze_experimental_machine_supervision(
    *,
    repo_root: Path,
    audit_dir: Path,
    positive_seed_dir: Path,
    output_dir: Path,
    config: ExperimentalMachineSupervisionConfig,
    config_hash: str | None = None,
) -> ExperimentalMachineSupervisionArtifacts:
    """Build or exactly replay one immutable experimental corpus."""

    repo = _real_directory(repo_root)
    audit = _real_directory(audit_dir)
    seeds_root = _real_directory(positive_seed_dir)
    output = _reject_symlink_components(output_dir, allow_missing=True)
    for input_root in (repo, audit, seeds_root):
        if output == input_root or output in input_root.parents or input_root in output.parents:
            raise ExperimentalMachineSupervisionError(
                "output directory must be disjoint from every input root"
            )
    effective_config_hash = config_hash or hash_canonical(config.model_dump(mode="json"))
    if effective_config_hash != hash_canonical(config.model_dump(mode="json")):
        raise ExperimentalMachineSupervisionError("config_hash differs from effective config")
    code = collect_code_state(repo)
    code_tree_hash = code.code_tree_hash
    if code.git_dirty or code_tree_hash is None or code.untracked_files:
        raise ExperimentalMachineSupervisionError(
            "experimental corpus freeze requires a clean, fully tracked code tree"
        )

    audit_manifest, gross, unique = _load_audit_inputs(audit, config=config)
    seed_manifest, seeds = _load_positive_seeds(seeds_root, config=config)
    if (
        seed_manifest.input_combination_hash != audit_manifest.combination_hash
        or seed_manifest.input_combination_manifest_sha256 != config.audit_manifest_sha256
        or seed_manifest.input_gross_observations_sha256 != config.audit_gross_observations_sha256
        or seed_manifest.input_unique_pairs_sha256 != config.audit_unique_pairs_sha256
    ):
        raise ExperimentalMachineSupervisionError(
            "positive-seed set does not bind the selected deterministic audit"
        )
    benchmark = _load_benchmark_registry(repo, config=config)
    inputs = _input_bindings(
        audit_dir=audit,
        seed_dir=seeds_root,
        benchmark=benchmark,
        repo_root=repo,
    )
    locators = _candidate_locators(
        audit_manifest,
        gross,
        unique,
        seeds,
        config=config,
    )
    payloads = _verified_result_payloads(locators)
    joined = _join_candidates(locators, payloads)
    screened, exclusions = _screen_candidates(joined, benchmark)
    screened_component_ids = _candidate_component_ids(screened)
    selected = _select_quotas(
        screened,
        config=config,
        component_ids=screened_component_ids,
    )
    selected_component_ids = _candidate_component_ids(selected)
    records = _build_records(
        selected,
        config=config,
        component_ids=selected_component_ids,
    )
    dataset_id = _dataset_id(
        config_hash=effective_config_hash,
        code_tree_hash=code_tree_hash,
        inputs=inputs,
        records=records,
    )
    summary = _summary(
        records,
        dataset_id=dataset_id,
        config=config,
        exclusions=exclusions,
    )
    assignments = tuple(
        ExperimentalSplitAssignment(
            record_id=record.record_id,
            split_component_id=record.split_component_id,
            split_group_ids=record.split_group_ids,
            split=record.split,
            pseudo_target=record.pseudo_target,
        )
        for record in records
    )
    sample = _public_sample(records, config=config)
    non_manifest_payloads: dict[str, bytes] = {
        "records.jsonl": _canonical_jsonl(records),
        "split_assignments.jsonl": _canonical_jsonl(assignments),
        "public_sample.jsonl": _canonical_jsonl(sample),
        "public_sample.md": _public_sample_markdown(sample, dataset_id=dataset_id),
        "summary.json": canonical_json_bytes(summary.model_dump(mode="json")) + b"\n",
    }
    manifest = ExperimentalMachineSupervisionManifest(
        dataset_id=dataset_id,
        profile_id=config.profile_id,
        config_hash=effective_config_hash,
        config=config,
        code=code,
        inputs=inputs,
        record_count=len(records),
        component_count=summary.component_count,
        output_sha256={
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in non_manifest_payloads.items()
        },
    )
    final_payloads = {
        **non_manifest_payloads,
        "manifest.json": canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    }

    # Re-hash all external inputs immediately before publication.  A concurrent
    # mutation therefore fails rather than producing a mixed-lineage corpus.
    for name, binding in inputs.items():
        if hash_file(_regular_file(Path(binding.path))) != binding.sha256:
            raise ExperimentalMachineSupervisionError(f"input changed during build: {name}")
    replayed = _write_or_replay(output, final_payloads)
    verify_experimental_machine_supervision(output)
    return ExperimentalMachineSupervisionArtifacts(
        output_dir=output,
        manifest_path=output / "manifest.json",
        records_path=output / "records.jsonl",
        split_assignments_path=output / "split_assignments.jsonl",
        public_sample_path=output / "public_sample.jsonl",
        public_sample_markdown_path=output / "public_sample.md",
        summary_path=output / "summary.json",
        dataset_id=dataset_id,
        record_count=len(records),
        replayed=replayed,
    )


def verify_experimental_machine_supervision(
    output_dir: Path,
    *,
    verify_external_inputs: bool = True,
) -> ExperimentalMachineSupervisionManifest:
    """Verify one corpus from bytes; make no writes and execute no Lean/model calls."""

    root = _real_directory(output_dir)
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalMachineSupervisionError("experimental corpus file set is not exact")
    try:
        manifest = _load_canonical_model(
            root / "manifest.json", ExperimentalMachineSupervisionManifest
        )
        summary = _load_canonical_model(
            root / "summary.json", ExperimentalMachineSupervisionSummary
        )
    except Exception as exc:
        raise ExperimentalMachineSupervisionError(f"invalid corpus metadata: {exc}") from exc
    for name, expected in manifest.output_sha256.items():
        if hash_file(_regular_file(root / name)) != expected:
            raise ExperimentalMachineSupervisionError(f"output hash differs: {name}")
    records = _load_canonical_jsonl(root / "records.jsonl", ExperimentalMachineSupervisionRecord)
    assignments = _load_canonical_jsonl(
        root / "split_assignments.jsonl", ExperimentalSplitAssignment
    )
    sample = _load_canonical_jsonl(
        root / "public_sample.jsonl", ExperimentalMachineSupervisionRecord
    )
    if len(records) != manifest.record_count or len(records) != summary.record_count:
        raise ExperimentalMachineSupervisionError("record count differs from metadata")
    if len({record.record_id for record in records}) != len(records):
        raise ExperimentalMachineSupervisionError("duplicate experimental record ID")
    if tuple(sorted(records, key=lambda item: item.record_id)) != records:
        raise ExperimentalMachineSupervisionError("records are not in canonical ID order")
    if any(record.dataset_profile_id != manifest.profile_id for record in records):
        raise ExperimentalMachineSupervisionError("record profile differs from manifest")
    manifest_code_tree_hash = manifest.code.code_tree_hash
    if manifest_code_tree_hash is None:
        raise ExperimentalMachineSupervisionError("manifest lacks a code-tree hash")
    if (
        _dataset_id(
            config_hash=manifest.config_hash,
            code_tree_hash=manifest_code_tree_hash,
            inputs=manifest.inputs,
            records=records,
        )
        != manifest.dataset_id
    ):
        raise ExperimentalMachineSupervisionError("dataset ID differs from corpus content")
    if summary.dataset_id != manifest.dataset_id or summary.profile_id != manifest.profile_id:
        raise ExperimentalMachineSupervisionError("summary identity differs from manifest")

    expected_assignments = tuple(
        ExperimentalSplitAssignment(
            record_id=record.record_id,
            split_component_id=record.split_component_id,
            split_group_ids=record.split_group_ids,
            split=record.split,
            pseudo_target=record.pseudo_target,
        )
        for record in records
    )
    if assignments != expected_assignments:
        raise ExperimentalMachineSupervisionError("split assignments differ from records")
    component_splits: dict[str, set[str]] = defaultdict(set)
    component_groups: dict[str, set[str]] = defaultdict(set)
    component_counts: Counter[str] = Counter()
    expected_components = _union_component_ids(
        tuple((record.record_id, record.split_group_ids) for record in records)
    )
    for record in records:
        if record.split_component_id != expected_components[record.record_id]:
            raise ExperimentalMachineSupervisionError(
                f"record has a non-canonical ancestry component: {record.record_id}"
            )
        component_splits[record.split_component_id].add(record.split)
        component_groups[record.split_component_id].update(record.split_group_ids)
        component_counts[record.split_component_id] += 1
    if any(len(splits) != 1 for splits in component_splits.values()):
        raise ExperimentalMachineSupervisionError("one ancestry component crosses splits")
    group_owners: dict[str, str] = {}
    for component, groups in component_groups.items():
        for group in groups:
            prior = group_owners.setdefault(group, component)
            if prior != component:
                raise ExperimentalMachineSupervisionError(
                    f"split group crosses components: {group}"
                )
    if max(component_counts.values()) > manifest.config.maximum_variants_per_component:
        raise ExperimentalMachineSupervisionError("component variant cap is exceeded")
    if len(component_counts) != manifest.component_count:
        raise ExperimentalMachineSupervisionError("component count differs from manifest")

    by_target = Counter(record.pseudo_target for record in records)
    by_family = Counter(record.family_id for record in records)
    if by_target != Counter(
        {
            "same_claim": manifest.config.positive_count,
            "not_same_claim": manifest.config.negative_count,
        }
    ):
        raise ExperimentalMachineSupervisionError("pseudo-target counts differ from config")
    expected_families = Counter(
        {
            **manifest.config.positive_family_quotas,
            **manifest.config.negative_family_quotas,
        }
    )
    if by_family != expected_families:
        raise ExperimentalMachineSupervisionError("family counts differ from config")
    if dict(sorted(by_target.items())) != summary.counts_by_pseudo_target:
        raise ExperimentalMachineSupervisionError("summary pseudo-target counts differ")
    if dict(sorted(by_family.items())) != summary.counts_by_family:
        raise ExperimentalMachineSupervisionError("summary family counts differ")
    split_counts = Counter(record.split for record in records)
    if dict(sorted(split_counts.items())) != summary.counts_by_split:
        raise ExperimentalMachineSupervisionError("summary split counts differ")
    if summary.component_count != len(component_counts):
        raise ExperimentalMachineSupervisionError("summary component count differs")
    if summary.maximum_observed_variants_per_component != max(component_counts.values()):
        raise ExperimentalMachineSupervisionError("summary component maximum differs")
    if summary.source_theorem_count != len({record.source.theorem_id for record in records}):
        raise ExperimentalMachineSupervisionError("summary source theorem count differs")

    sample_ids = {record.record_id for record in sample}
    if len(sample_ids) != len(sample) or not sample_ids.issubset(
        {record.record_id for record in records}
    ):
        raise ExperimentalMachineSupervisionError("public sample is not a unique corpus subset")
    if Counter(record.pseudo_target for record in sample) != Counter(
        {
            "same_claim": manifest.config.public_sample_per_target,
            "not_same_claim": manifest.config.public_sample_per_target,
        }
    ):
        raise ExperimentalMachineSupervisionError("public sample is not target-balanced")
    if len(sample) != summary.public_sample_count:
        raise ExperimentalMachineSupervisionError("public sample count differs from summary")
    if (root / "public_sample.md").read_bytes() != _public_sample_markdown(
        sample, dataset_id=manifest.dataset_id
    ):
        raise ExperimentalMachineSupervisionError("public sample Markdown differs")

    if verify_external_inputs:
        for name, binding in manifest.inputs.items():
            path = _regular_file(Path(binding.path))
            if hash_file(path) != binding.sha256 or path.stat().st_size != binding.byte_count:
                raise ExperimentalMachineSupervisionError(f"external input differs: {name}")
    return manifest


def load_experimental_machine_supervision(
    output_dir: Path,
    *,
    allow_experimental_machine_supervision: bool,
    purpose: str,
) -> tuple[ExperimentalMachineSupervisionRecord, ...]:
    """Load only after an explicit opt-in and only for a non-scientific purpose."""

    if not allow_experimental_machine_supervision:
        raise ExperimentalMachineSupervisionError(
            "loading requires --allow-experimental-machine-supervision"
        )
    if purpose not in {"smoke_training", "learning_curve"}:
        raise ExperimentalMachineSupervisionError(
            "experimental corpus is forbidden for production, selection, calibration, or eval"
        )
    manifest = verify_experimental_machine_supervision(output_dir)
    if purpose not in manifest.allowed_purposes:
        raise ExperimentalMachineSupervisionError(f"purpose is not admitted: {purpose}")
    return _load_canonical_jsonl(output_dir / "records.jsonl", ExperimentalMachineSupervisionRecord)


__all__ = [
    "ExperimentalMachineSupervisionArtifacts",
    "ExperimentalMachineSupervisionConfig",
    "ExperimentalMachineSupervisionError",
    "ExperimentalMachineSupervisionManifest",
    "ExperimentalMachineSupervisionRecord",
    "ExperimentalMachineSupervisionSummary",
    "ExperimentalSplitAssignment",
    "freeze_experimental_machine_supervision",
    "load_experimental_machine_supervision",
    "load_experimental_machine_supervision_config",
    "verify_experimental_machine_supervision",
]
