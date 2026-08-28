"""Fail-closed audit-only combination of deterministic-v2 provisional pairs.

The v2 scale runners intentionally persist transformation intentions, not
semantic labels.  This module validates completed E0/E2/D0 materialization
roots, reconstructs their unlabeled source-candidate pairs, and deduplicates
exact source/candidate observations without promoting any item.  Every output
is content addressed and an existing output directory is accepted only as an
exact immutable replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.representations import NORMALIZATION_VERSION
from leanfaith.schemas.enums import IntendedRelation, Polarity
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.materialize import build_deterministic_pair_record
from leanfaith.transforms.protocol import (
    build_deterministic_variant_record,
    verify_transformation_attempt_id,
    verify_transformation_audit_id,
    verify_variant_draft_id,
)
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_d0_materializer import V2D0MaterializationResult
from leanfaith.transforms.v2_d0_scale_run import (
    V2D0ScaleRunManifest,
    V2D0ScaleRunManifestLegacyV2,
    V2D0ScaleRunSpec,
    V2D0ScaleRunSpecLegacyV2,
)
from leanfaith.transforms.v2_e0_materializer import V2E0MaterializationResult
from leanfaith.transforms.v2_e0_scale_run import V2E0ScaleRunManifest, V2E0ScaleRunSpec
from leanfaith.transforms.v2_e2_materializer import V2E2MaterializationResult
from leanfaith.transforms.v2_e2_recovery_schema import (
    RecoveryPipelineAttempt,
    V2E2RecoveryReceipt,
    V2E2RecoverySpec,
)
from leanfaith.transforms.v2_e2_scale_run import (
    V2E2ScaleRunManifest,
    V2E2ScaleRunManifestLegacyV1,
    V2E2ScaleRunManifestLegacyV2,
    V2E2ScaleRunSpec,
    V2E2ScaleRunSpecLegacyV1,
    V2E2ScaleRunSpecLegacyV2,
)

_HEX64 = r"^[0-9a-f]{64}$"
_RUN_SPEC = "run_spec.json"
_MANIFEST = "manifest.json"
_RESULTS = "results.jsonl"
_RECOVERY_SPEC = "recovery_spec.json"
_RECOVERY_RECEIPT = "recovery_receipt.json"
_GROSS_OUTPUT = "gross_observations.jsonl"
_UNIQUE_OUTPUT = "unique_pairs.jsonl"
_COMBINED_MANIFEST = "manifest.json"
_EXPECTED_OUTPUTS = frozenset({_GROSS_OUTPUT, _UNIQUE_OUTPUT, _COMBINED_MANIFEST})
_ABORT_MARKERS = frozenset({"aborted", "abort", "failed", "failure"})
_ROOT_LOCATOR_FIELDS = frozenset(
    {
        "root_path",
        "theorem_partition_path",
        "representation_partition_path",
    }
)
_INFRASTRUCTURE_FAILURE_TOKENS = (
    "infrastructure",
    "lean_crash",
    "worker_crash",
    "server_crash",
    "server_unavailable",
    "timeout",
    "failed_to_create_thread",
    "thread_failure",
    "memory_limit",
    "out_of_memory",
)

type RunKind = Literal["e0", "e2", "d0"]
type RunSpec = (
    V2E0ScaleRunSpec
    | V2E2ScaleRunSpec
    | V2E2ScaleRunSpecLegacyV1
    | V2E2ScaleRunSpecLegacyV2
    | V2D0ScaleRunSpec
    | V2D0ScaleRunSpecLegacyV2
)
type RunManifest = (
    V2E0ScaleRunManifest
    | V2E2ScaleRunManifest
    | V2E2ScaleRunManifestLegacyV1
    | V2E2ScaleRunManifestLegacyV2
    | V2D0ScaleRunManifest
    | V2D0ScaleRunManifestLegacyV2
)
type MaterializationResult = (
    V2E0MaterializationResult | V2E2MaterializationResult | V2D0MaterializationResult
)


def _root_binding_identity_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Remove machine-local locators from one content-identity payload."""

    return {key: value for key, value in payload.items() if key not in _ROOT_LOCATOR_FIELDS}


def _combination_identity_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Return a relocation-stable combination-identity payload."""

    identity = dict(payload)
    identity.pop("combination_hash", None)
    root_bindings = identity.get("root_bindings")
    if not isinstance(root_bindings, (list, tuple)):
        raise ValueError("combination identity lacks root bindings")
    normalized_bindings: list[dict[str, object]] = []
    for binding in root_bindings:
        if not isinstance(binding, dict):
            raise ValueError("combination identity contains an invalid root binding")
        normalized_bindings.append(_root_binding_identity_payload(binding))
    identity["root_bindings"] = normalized_bindings
    return identity


class ProvisionalPairCombineError(ValueError):
    """A materialization input or immutable output failed audit."""


class FileBinding(StrictModel):
    """Hash and size of one scientific input artifact."""

    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=0)


class MaterializationRootBinding(StrictModel):
    """Complete binding for one accepted deterministic-v2 root."""

    schema_version: Literal[2] = 2
    root_binding_id: str = Field(pattern=r"^detprov_root:[0-9a-f]{64}$")
    root_path: str = Field(min_length=1)
    run_kind: RunKind
    profile_id: str = Field(min_length=1)
    rule_ids: tuple[str, ...] = Field(min_length=1)
    context_id: str = Field(min_length=1)
    execution_settings_provenance: Literal["recorded", "legacy_unknown"]
    workers: int | None = Field(default=None, ge=1)
    memory_hard_limit_mb: int | None = Field(default=None, ge=1)
    run_spec: FileBinding
    manifest: FileBinding
    results: FileBinding
    journal_files: tuple[FileBinding, ...] = Field(min_length=1)
    root_file_count: int = Field(ge=4)
    root_tree_hash: str = Field(pattern=_HEX64)
    theorem_partition_path: str = Field(min_length=1)
    theorem_partition_sha256: str = Field(pattern=_HEX64)
    representation_partition_path: str = Field(min_length=1)
    representation_partition_sha256: str = Field(pattern=_HEX64)
    source_count: int = Field(ge=1)
    result_count: int = Field(ge=1)
    provisional_count: int = Field(ge=0)
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _identity_matches_payload(self) -> MaterializationRootBinding:
        payload = self.model_dump(mode="json")
        payload.pop("root_binding_id")
        expected = f"detprov_root:{hash_canonical(_root_binding_identity_payload(payload))}"
        if self.root_binding_id != expected:
            raise ValueError("root_binding_id does not match its bound payload")
        if self.rule_ids != tuple(sorted(set(self.rule_ids))):
            raise ValueError("rule_ids must be sorted and unique")
        if self.execution_settings_provenance == "recorded" and self.workers is None:
            raise ValueError("recorded execution settings require a worker count")
        if self.execution_settings_provenance == "legacy_unknown" and (
            self.workers is not None or self.memory_hard_limit_mb is not None
        ):
            raise ValueError("legacy execution settings must remain explicitly unknown")
        return self


class ProvisionalPairObservation(StrictModel):
    """One gross provisional pair occurrence with complete locator provenance."""

    schema_version: Literal[1] = 1
    observation_id: str = Field(pattern=r"^detprov_observation:[0-9a-f]{64}$")
    root_binding_id: str = Field(pattern=r"^detprov_root:[0-9a-f]{64}$")
    result_id: str = Field(min_length=1)
    result_line_number: int = Field(ge=1)
    profile_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    source_theorem_ids: tuple[str, ...] = Field(min_length=1)
    source_representation_ids: tuple[str, ...] = Field(min_length=1)
    source_categories: tuple[str, ...] = Field(min_length=1)
    source_root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    pair_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    audit_id: str = Field(min_length=1)
    variant_id: str = Field(min_length=1)
    candidate_theorem_id: str = Field(min_length=1)
    candidate_representation_id: str = Field(min_length=1)
    candidate_code_hash: str = Field(pattern=_HEX64)
    candidate_alpha_identity_fingerprint: str | None = Field(default=None, pattern=_HEX64)
    intended_relation: IntendedRelation
    polarity_metadata: Polarity
    exact_pair_key: str = Field(pattern=_HEX64)
    candidate_code_key: str = Field(pattern=_HEX64)
    ancestry_candidate_key: str = Field(pattern=_HEX64)
    alpha_candidate_key: str | None = Field(default=None, pattern=_HEX64)
    terminal_status: Literal["provisional_variant"] = "provisional_variant"
    quality_tier: Literal["provisional"] = "provisional"
    intention_only: Literal[True] = True
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _identity_matches_payload(self) -> ProvisionalPairObservation:
        payload = self.model_dump(mode="json")
        payload.pop("observation_id")
        expected = f"detprov_observation:{hash_canonical(payload)}"
        if self.observation_id != expected:
            raise ValueError("observation_id does not match its provenance payload")
        for field_name in (
            "source_theorem_ids",
            "source_representation_ids",
            "source_categories",
            "source_root_ancestry_ids",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        return self


class UniqueProvisionalPair(StrictModel):
    """One exact source/candidate group; no semantic representative is chosen."""

    schema_version: Literal[1] = 1
    unique_pair_id: str = Field(pattern=r"^detprov_pair:[0-9a-f]{64}$")
    exact_pair_key: str = Field(pattern=_HEX64)
    context_id: str = Field(min_length=1)
    source_theorem_ids: tuple[str, ...] = Field(min_length=1)
    candidate_code_hash: str = Field(pattern=_HEX64)
    observation_ids: tuple[str, ...] = Field(min_length=1)
    provenance_count: int = Field(ge=1)
    family_ids: tuple[str, ...] = Field(min_length=1)
    source_categories: tuple[str, ...] = Field(min_length=1)
    intended_relations: tuple[IntendedRelation, ...] = Field(min_length=1)
    polarity_metadata: tuple[Polarity, ...] = Field(min_length=1)
    conflicting_intentions: bool
    intention_only: Literal[True] = True
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> UniqueProvisionalPair:
        if self.unique_pair_id != f"detprov_pair:{self.exact_pair_key}":
            raise ValueError("unique_pair_id must be derived from exact_pair_key")
        if self.provenance_count != len(self.observation_ids):
            raise ValueError("provenance_count does not match observation_ids")
        for field_name in (
            "source_theorem_ids",
            "observation_ids",
            "family_ids",
            "source_categories",
            "intended_relations",
            "polarity_metadata",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values), key=str)):
                raise ValueError(f"{field_name} must be sorted and unique")
        if self.conflicting_intentions != (
            len(self.intended_relations) > 1 or len(self.polarity_metadata) > 1
        ):
            raise ValueError("conflicting_intentions does not reconcile")
        return self


class OverlapAudit(StrictModel):
    """Deduplication accounting for one explicit overlap key."""

    key_name: Literal[
        "exact_pair_key",
        "candidate_code_key",
        "ancestry_candidate_key",
        "alpha_candidate_key",
    ]
    observation_count: int = Field(ge=0)
    key_available_count: int = Field(ge=0)
    unique_key_count: int = Field(ge=0)
    duplicate_group_count: int = Field(ge=0)
    duplicate_excess_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _reconciles(self) -> OverlapAudit:
        if self.key_available_count > self.observation_count:
            raise ValueError("available overlap keys exceed observation count")
        if self.unique_key_count > self.key_available_count:
            raise ValueError("unique overlap keys exceed available keys")
        if self.duplicate_excess_count != self.key_available_count - self.unique_key_count:
            raise ValueError("duplicate excess does not reconcile")
        return self


class ProvisionalPairCombinationManifest(StrictModel):
    """Self-authenticating, audit-only result of an exact multi-root combine."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_provisional_pair_combination_audit"] = (
        "deterministic_provisional_pair_combination_audit"
    )
    combination_hash: str = Field(pattern=_HEX64)
    root_bindings: tuple[MaterializationRootBinding, ...] = Field(min_length=1)
    gross_observation_count: int = Field(ge=0)
    unique_pair_count: int = Field(ge=0)
    duplicate_group_count: int = Field(ge=0)
    duplicate_excess_count: int = Field(ge=0)
    cross_family_duplicate_group_count: int = Field(ge=0)
    cross_source_duplicate_group_count: int = Field(ge=0)
    gross_counts_by_family: dict[str, int]
    unique_counts_by_family: dict[str, int]
    gross_counts_by_source: dict[str, int]
    unique_counts_by_source: dict[str, int]
    overlap_audits: tuple[OverlapAudit, ...]
    gross_output: Literal["gross_observations.jsonl"] = "gross_observations.jsonl"
    gross_output_sha256: str = Field(pattern=_HEX64)
    unique_output: Literal["unique_pairs.jsonl"] = "unique_pairs.jsonl"
    unique_output_sha256: str = Field(pattern=_HEX64)
    audit_only: Literal[True] = True
    provisional_intentions_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _self_authenticating(self) -> ProvisionalPairCombinationManifest:
        payload = _combination_identity_payload(self.model_dump(mode="json"))
        if self.combination_hash != hash_canonical(payload):
            raise ValueError("combination_hash does not match manifest payload")
        root_ids = tuple(item.root_binding_id for item in self.root_bindings)
        if root_ids != tuple(sorted(set(root_ids))):
            raise ValueError("root bindings must be sorted and unique")
        if self.unique_pair_count > self.gross_observation_count:
            raise ValueError("unique pair count exceeds gross observations")
        if self.duplicate_excess_count != (self.gross_observation_count - self.unique_pair_count):
            raise ValueError("duplicate excess count does not reconcile")
        if sum(self.gross_counts_by_family.values()) != self.gross_observation_count:
            raise ValueError("gross family counts do not reconcile")
        if sum(self.gross_counts_by_source.values()) != self.gross_observation_count:
            raise ValueError("gross source counts do not reconcile")
        return self


@dataclass(frozen=True, slots=True)
class ProvisionalPairCombinationArtifacts:
    """Paths and summary returned by a new combine or exact replay."""

    output_dir: Path
    manifest_path: Path
    gross_path: Path
    unique_path: Path
    combination_hash: str
    gross_count: int
    unique_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _SourceInventory:
    ordered: tuple[tuple[TheoremRecord, RepresentationRecord], ...]
    by_theorem_id: Mapping[str, TheoremRecord]


@dataclass(frozen=True, slots=True)
class _LoadedRoot:
    binding: MaterializationRootBinding
    observations: tuple[ProvisionalPairObservation, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProvisionalPairCombineError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _parse_json_object(payload: bytes, *, path: Path) -> dict[str, object]:
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProvisionalPairCombineError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProvisionalPairCombineError(f"JSON input is not an object: {path}")
    return value


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _load_canonical_model[ModelT: StrictModel](path: Path, model: type[ModelT]) -> ModelT:
    if not path.is_file() or path.is_symlink():
        raise ProvisionalPairCombineError(f"input is not a regular file: {path}")
    payload = path.read_bytes()
    try:
        parsed = model.model_validate(_parse_json_object(payload, path=path))
    except ValueError as exc:
        raise ProvisionalPairCombineError(f"invalid {model.__name__} at {path}: {exc}") from exc
    if payload != _canonical_line(parsed):
        raise ProvisionalPairCombineError(f"non-canonical {model.__name__}: {path}")
    return parsed


def _regular_file_binding(path: Path, *, relative_to: Path) -> FileBinding:
    if not path.is_file() or path.is_symlink():
        raise ProvisionalPairCombineError(f"artifact is not a regular file: {path}")
    return FileBinding(
        relative_path=path.relative_to(relative_to).as_posix(),
        sha256=hash_file(path),
        byte_count=path.stat().st_size,
    )


def _root_tree(root: Path) -> tuple[int, str]:
    entries: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ProvisionalPairCombineError(f"materialization root contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ProvisionalPairCombineError(
                f"materialization root contains a non-regular entry: {path}"
            )
        lowered = path.name.lower()
        if any(marker in lowered for marker in _ABORT_MARKERS) or lowered.endswith(".partial"):
            raise ProvisionalPairCombineError(
                f"materialization root contains abort/incomplete marker: {path}"
            )
        entries.append((path.relative_to(root).as_posix(), hash_file(path), path.stat().st_size))
    if not entries:
        raise ProvisionalPairCombineError(f"materialization root is empty: {root}")
    return len(entries), hash_canonical(entries)


def _root_tree_without(root: Path, excluded: frozenset[str]) -> tuple[int, str]:
    entries: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ProvisionalPairCombineError(f"materialization root contains a symlink: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        if not path.is_file():
            raise ProvisionalPairCombineError(
                f"materialization root contains a non-regular entry: {path}"
            )
        entries.append((relative, hash_file(path), path.stat().st_size))
    if not entries:
        raise ProvisionalPairCombineError(f"materialization root is empty: {root}")
    return len(entries), hash_canonical(entries)


def _validate_e2_recovery_metadata(root: Path) -> None:
    """Verify the exact one-line parent/output reconciliation, when present."""

    spec_path = root / _RECOVERY_SPEC
    receipt_path = root / _RECOVERY_RECEIPT
    if not spec_path.exists() and not receipt_path.exists():
        return
    if not spec_path.is_file() or not receipt_path.is_file():
        raise ProvisionalPairCombineError("recovered root must contain both recovery artifacts")
    recovery_spec = _load_canonical_model(spec_path, V2E2RecoverySpec)
    receipt = _load_canonical_model(receipt_path, V2E2RecoveryReceipt)
    if (
        receipt.recovery_spec_id != recovery_spec.recovery_spec_id
        or receipt.recovery_spec_sha256 != hash_file(spec_path)
    ):
        raise ProvisionalPairCombineError("recovery receipt does not bind recovery spec")
    if receipt.output_run_spec_sha256 != hash_file(root / _RUN_SPEC):
        raise ProvisionalPairCombineError("recovery receipt does not bind output run spec")
    if receipt.output_results_sha256 != hash_file(root / _RESULTS):
        raise ProvisionalPairCombineError("recovery receipt does not bind output results")
    if receipt.output_manifest_sha256 != hash_file(root / _MANIFEST):
        raise ProvisionalPairCombineError("recovery receipt does not bind output manifest")
    output_manifest_payload = _parse_json_object(
        (root / _MANIFEST).read_bytes(), path=root / _MANIFEST
    )
    if output_manifest_payload.get("journal_tree_hash") != receipt.output_journal_tree_hash:
        raise ProvisionalPairCombineError("recovery receipt does not bind output journal tree")
    if _root_tree_without(root, frozenset({_RECOVERY_RECEIPT})) != (
        receipt.output_root_file_count_without_receipt,
        receipt.output_root_tree_hash_without_receipt,
    ):
        raise ProvisionalPairCombineError("recovery receipt does not bind output root tree")

    parent = Path(recovery_spec.parent_root_path).resolve(strict=True)
    if parent == root:
        raise ProvisionalPairCombineError("recovery parent cannot be the recovered root")
    if _root_tree(parent) != (
        recovery_spec.parent_root_file_count,
        recovery_spec.parent_root_tree_hash,
    ):
        raise ProvisionalPairCombineError("recovery parent root changed")
    if hash_file(parent / _RUN_SPEC) != recovery_spec.parent_run_spec_sha256:
        raise ProvisionalPairCombineError("recovery spec does not bind parent run spec")
    if hash_file(parent / _MANIFEST) != recovery_spec.parent_manifest_sha256:
        raise ProvisionalPairCombineError("recovery spec does not bind parent manifest")
    if hash_file(parent / _RESULTS) != recovery_spec.parent_results_sha256:
        raise ProvisionalPairCombineError("recovery spec does not bind parent results")
    parent_manifest_payload = _parse_json_object(
        (parent / _MANIFEST).read_bytes(), path=parent / _MANIFEST
    )
    if parent_manifest_payload.get("journal_tree_hash") != (recovery_spec.parent_journal_tree_hash):
        raise ProvisionalPairCombineError("recovery spec does not bind parent journal tree")
    if (root / _RUN_SPEC).read_bytes() != (parent / _RUN_SPEC).read_bytes():
        raise ProvisionalPairCombineError("recovery changed run_spec.json bytes")
    output_run_spec_payload = _parse_json_object(
        (root / _RUN_SPEC).read_bytes(), path=root / _RUN_SPEC
    )
    if (
        output_run_spec_payload.get("profile_id") != recovery_spec.profile_id
        or output_run_spec_payload.get("profile_config_hash") != recovery_spec.profile_config_hash
    ):
        raise ProvisionalPairCombineError("recovery spec does not bind run profile/config")

    parent_lines = (parent / _RESULTS).read_bytes().splitlines(keepends=True)
    output_lines = (root / _RESULTS).read_bytes().splitlines(keepends=True)
    if len(parent_lines) != len(output_lines):
        raise ProvisionalPairCombineError("recovery changed result cardinality")
    target_index = recovery_spec.target_result_line_number - 1
    if target_index >= len(parent_lines):
        raise ProvisionalPairCombineError("recovery target line is outside parent results")
    try:
        parent_target = V2E2MaterializationResult.model_validate(
            _parse_json_object(parent_lines[target_index], path=parent / _RESULTS)
        )
        output_target = V2E2MaterializationResult.model_validate(
            _parse_json_object(output_lines[target_index], path=root / _RESULTS)
        )
    except ValueError as exc:
        raise ProvisionalPairCombineError(f"invalid recovery target result: {exc}") from exc
    if (
        parent_target.result_id != recovery_spec.target_result_id
        or parent_target.attempt.attempt_id != recovery_spec.target_attempt_id
        or parent_target.draft is None
        or parent_target.draft.draft_id != recovery_spec.target_draft_id
        or parent_target.attempt.source_theorem_ids != (recovery_spec.target_source_theorem_id,)
        or parent_target.attempt.source_representation_ids
        != (recovery_spec.target_source_representation_id,)
        or not _has_infrastructure_error(parent_target)
    ):
        raise ProvisionalPairCombineError("recovery target does not bind parent failure")
    if (
        output_target.result_id != receipt.replacement_result_id
        or output_target.terminal_status != receipt.replacement_terminal_status
        or output_target.attempt != parent_target.attempt
        or output_target.draft != parent_target.draft
        or _has_infrastructure_error(output_target)
        or hashlib.sha256(output_lines[target_index]).hexdigest()
        != receipt.replacement_result_sha256
    ):
        raise ProvisionalPairCombineError("recovery receipt does not bind replacement result")
    if any(
        before != after
        for index, (before, after) in enumerate(
            zip(parent_lines, output_lines, strict=True), start=1
        )
        if index != recovery_spec.target_result_line_number
    ):
        raise ProvisionalPairCombineError("recovery changed a non-target result line")
    if receipt.unchanged_result_line_count != len(parent_lines) - 1:
        raise ProvisionalPairCombineError("recovery unchanged-line count is inconsistent")

    parent_journals = tuple(sorted((parent / "journal").glob("batch_*.jsonl")))
    output_journals = tuple(sorted((root / "journal").glob("batch_*.jsonl")))
    if len(parent_journals) != len(output_journals):
        raise ProvisionalPairCombineError("recovery changed journal cardinality")
    for index, (before, after) in enumerate(zip(parent_journals, output_journals, strict=True)):
        if index == recovery_spec.target_batch_index:
            before_lines = before.read_bytes().splitlines(keepends=True)
            after_lines = after.read_bytes().splitlines(keepends=True)
            if len(before_lines) != len(after_lines):
                raise ProvisionalPairCombineError("recovery changed target batch cardinality")
            local_target = recovery_spec.target_batch_line_number - 1
            if any(
                left != right
                for line_index, (left, right) in enumerate(
                    zip(before_lines, after_lines, strict=True)
                )
                if line_index != local_target
            ):
                raise ProvisionalPairCombineError("recovery changed a non-target batch line")
        elif before.read_bytes() != after.read_bytes():
            raise ProvisionalPairCombineError("recovery changed a non-target journal file")
    if receipt.unchanged_journal_file_count != len(parent_journals) - 1:
        raise ProvisionalPairCombineError("recovery unchanged-journal count is inconsistent")

    seen_raw_paths: set[str] = set()
    saw_representation_stage = False
    per_request_attempts: dict[str, list[RecoveryPipelineAttempt]] = defaultdict(list)
    for raw_attempt in receipt.pipeline_attempts:
        if raw_attempt.raw_response_relative_path in seen_raw_paths:
            raise ProvisionalPairCombineError("recovery reused a raw response path")
        seen_raw_paths.add(raw_attempt.raw_response_relative_path)
        raw_path = root / raw_attempt.raw_response_relative_path
        try:
            raw_path.resolve(strict=True).relative_to(root.resolve())
        except (OSError, ValueError) as exc:
            raise ProvisionalPairCombineError("recovery raw response escaped output root") from exc
        if not raw_path.is_file() or hash_file(raw_path) != raw_attempt.raw_response_sha256:
            raise ProvisionalPairCombineError("recovery raw response hash mismatch")
        raw_payload = _parse_json_object(raw_path.read_bytes(), path=raw_path)
        raw_request = raw_payload.get("request")
        if not isinstance(raw_request, dict):
            raise ProvisionalPairCombineError("recovery raw response lacks its request")
        if (
            raw_payload.get("request_hash") != raw_attempt.request_hash
            or raw_request.get("request_id") != raw_attempt.request_id
            or raw_request.get("context_id") != raw_attempt.context_id
            or raw_request.get("timeout_seconds") != raw_attempt.timeout_seconds
            or raw_request.get("allow_sorry") is not raw_attempt.allow_sorry
        ):
            raise ProvisionalPairCombineError("recovery receipt differs from its raw request")
        transport = raw_payload.get("transport_isolation")
        if transport is None:
            transport_attempt = None
        elif isinstance(transport, dict):
            transport_attempt = transport.get("attempt")
        else:
            raise ProvisionalPairCombineError("recovery raw transport isolation is malformed")
        if transport_attempt != raw_attempt.transport_isolation_attempt:
            raise ProvisionalPairCombineError("recovery transport-attempt binding differs")
        if raw_attempt.context_id != parent_target.attempt.context_id:
            raise ProvisionalPairCombineError("recovery request context differs from target")
        if raw_attempt.stage == "candidate_validation":
            if saw_representation_stage:
                raise ProvisionalPairCombineError(
                    "candidate validation followed representation work"
                )
        else:
            saw_representation_stage = True
        per_request_attempts[raw_attempt.request_id].append(raw_attempt)

    expected_candidate_request_id = (
        f"v2-e2-{recovery_spec.target_draft_id.removeprefix('draft:')[:24]}"
    )
    if any(
        attempt.request_id != expected_candidate_request_id
        or attempt.timeout_seconds != recovery_spec.candidate_timeout_seconds
        or not attempt.allow_sorry
        for attempt in receipt.lean_attempts
    ):
        raise ProvisionalPairCombineError("candidate recovery request differs from recovery spec")
    for request_id, attempts in per_request_attempts.items():
        if tuple(item.attempt_index for item in attempts) != tuple(range(len(attempts))):
            raise ProvisionalPairCombineError(
                f"recovery request {request_id} has non-contiguous retry lineage"
            )
        if any(item.status not in {"crash", "internal_error", "timeout"} for item in attempts[:-1]):
            raise ProvisionalPairCombineError("recovery retried a non-infrastructure result")
    if any(
        attempt.status not in {"crash", "internal_error", "timeout"}
        for attempt in receipt.lean_attempts[:-1]
    ):
        raise ProvisionalPairCombineError("recovery retried a non-infrastructure result")
    final_status = receipt.lean_attempts[-1].status
    if receipt.replacement_terminal_status == "candidate_invalid":
        if final_status != "invalid":
            raise ProvisionalPairCombineError("candidate-invalid recovery lacks INVALID lineage")
    elif final_status not in {"valid", "valid_with_sorry"}:
        raise ProvisionalPairCombineError("materialized recovery lacks elaborating final lineage")
    if receipt.replacement_terminal_status == "candidate_representation_failed" and any(
        attempts[-1].status in {"crash", "internal_error", "timeout"}
        for request_id, attempts in per_request_attempts.items()
        if request_id != expected_candidate_request_id
    ):
        raise ProvisionalPairCombineError(
            "recovered representation retains an infrastructure failure"
        )


def _load_run_models(root: Path) -> tuple[RunKind, RunSpec, RunManifest]:
    spec_path = root / _RUN_SPEC
    manifest_path = root / _MANIFEST
    if not spec_path.is_file() or not manifest_path.is_file():
        raise ProvisionalPairCombineError(f"materialization root is incomplete: {root}")
    spec_raw = _parse_json_object(spec_path.read_bytes(), path=spec_path)
    kind = spec_raw.get("artifact_kind")
    if kind == "deterministic_v2_e0_scale_run_spec":
        run_kind: RunKind = "e0"
        spec: RunSpec = _load_canonical_model(spec_path, V2E0ScaleRunSpec)
        manifest: RunManifest = _load_canonical_model(manifest_path, V2E0ScaleRunManifest)
    elif kind == "deterministic_v2_e2_scale_run_spec":
        run_kind = "e2"
        if spec_raw.get("schema_version") == 1:
            spec = _load_canonical_model(spec_path, V2E2ScaleRunSpecLegacyV1)
            manifest = _load_canonical_model(manifest_path, V2E2ScaleRunManifestLegacyV1)
        elif spec_raw.get("schema_version") == 2:
            spec = _load_canonical_model(spec_path, V2E2ScaleRunSpecLegacyV2)
            manifest = _load_canonical_model(manifest_path, V2E2ScaleRunManifestLegacyV2)
        else:
            spec = _load_canonical_model(spec_path, V2E2ScaleRunSpec)
            manifest = _load_canonical_model(manifest_path, V2E2ScaleRunManifest)
    elif kind == "deterministic_v2_d0_scale_run_spec":
        run_kind = "d0"
        if spec_raw.get("schema_version") == 2:
            spec = _load_canonical_model(spec_path, V2D0ScaleRunSpecLegacyV2)
            manifest = _load_canonical_model(manifest_path, V2D0ScaleRunManifestLegacyV2)
        else:
            spec = _load_canonical_model(spec_path, V2D0ScaleRunSpec)
            manifest = _load_canonical_model(manifest_path, V2D0ScaleRunManifest)
    else:
        raise ProvisionalPairCombineError(
            f"unsupported deterministic materialization run kind at {spec_path}: {kind!r}"
        )
    if manifest.run_spec_sha256 != hash_file(spec_path):
        raise ProvisionalPairCombineError("manifest does not bind run_spec.json")
    return run_kind, spec, manifest


def _iter_jsonl_objects(path: Path) -> Iterator[tuple[int, dict[str, object], bytes]]:
    if not path.is_file() or path.is_symlink():
        raise ProvisionalPairCombineError(f"JSONL input is not a regular file: {path}")
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.endswith(b"\n") or not raw_line.strip():
                raise ProvisionalPairCombineError(f"invalid JSONL framing at {path}:{line_number}")
            yield line_number, _parse_json_object(raw_line, path=path), raw_line


def _load_source_inventory(spec: RunSpec) -> _SourceInventory:
    theorem_path = Path(spec.theorem_partition)
    representation_path = Path(spec.representation_partition)
    for path in (theorem_path, representation_path):
        if not path.is_file() or path.is_symlink():
            raise ProvisionalPairCombineError(
                f"source inventory input is not a regular file: {path}"
            )
    if hash_file(theorem_path) != spec.theorem_partition_sha256:
        raise ProvisionalPairCombineError(f"theorem partition changed: {theorem_path}")
    if hash_file(representation_path) != spec.representation_partition_sha256:
        raise ProvisionalPairCombineError(
            f"representation partition changed: {representation_path}"
        )

    theorem_rows = list(_iter_jsonl_objects(theorem_path))
    representation_rows = list(_iter_jsonl_objects(representation_path))
    if spec.max_sources is None and len(theorem_rows) != len(representation_rows):
        raise ProvisionalPairCombineError(
            "complete theorem/representation partitions have different cardinality"
        )
    selected_count = spec.max_sources if spec.max_sources is not None else len(theorem_rows)
    if len(theorem_rows) < selected_count or len(representation_rows) < selected_count:
        raise ProvisionalPairCombineError(
            "source partitions are shorter than the run_spec selection"
        )
    selected_theorems = theorem_rows[:selected_count]
    selected_representations = representation_rows[:selected_count]
    if (
        len(selected_theorems) != spec.source_count
        or len(selected_representations) != spec.source_count
    ):
        raise ProvisionalPairCombineError("source partitions do not match run_spec source_count")

    ordered: list[tuple[TheoremRecord, RepresentationRecord]] = []
    seen_theorems: set[str] = set()
    seen_representations: set[str] = set()
    for theorem_row, representation_row in zip(
        selected_theorems,
        selected_representations,
        strict=True,
    ):
        theorem_line, theorem_raw, theorem_bytes = theorem_row
        representation_line, representation_raw, representation_bytes = representation_row
        theorem_payload = theorem_raw.get("theorem", theorem_raw)
        if not isinstance(theorem_payload, dict):
            raise ProvisionalPairCombineError(
                f"invalid wrapped theorem at {theorem_path}:{theorem_line}"
            )
        try:
            theorem = TheoremRecord.model_validate(theorem_payload)
            representation = RepresentationRecord.model_validate(representation_raw)
        except ValueError as exc:
            raise ProvisionalPairCombineError(
                "invalid source inventory record at lines "
                f"{theorem_line}/{representation_line}: {exc}"
            ) from exc
        # Historical frozen source partitions were written by more than one
        # JSON serializer.  Their exact bytes are already bound by the run
        # specification, and parsing above rejects duplicate keys and validates
        # the complete schema.  Requiring current compact serialization here
        # would reject semantically identical, immutable historical inputs.
        del theorem_bytes, representation_bytes
        if "theorem" in theorem_raw:
            embedded = theorem_raw.get("representation")
            if not isinstance(embedded, dict):
                raise ProvisionalPairCombineError(
                    "wrapped theorem lacks an embedded representation: "
                    f"{theorem_path}:{theorem_line}"
                )
            # Extraction wrappers carry an older, source-local view projection,
            # not necessarily a persistent RepresentationRecord.  The exact
            # wrapper bytes remain bound by theorem_partition_sha256; the
            # separate representation partition is the scale runner's
            # authoritative representation input and is validated below.
        if theorem.theorem_id != representation.theorem_id:
            raise ProvisionalPairCombineError(
                f"theorem/representation mismatch: {theorem.theorem_id} != "
                f"{representation.theorem_id}"
            )
        if theorem.context_id != spec.context_id or representation.context_id != spec.context_id:
            raise ProvisionalPairCombineError(
                f"source inventory context differs from run spec: {theorem.theorem_id}"
            )
        if theorem.theorem_id in seen_theorems:
            raise ProvisionalPairCombineError(f"duplicate source theorem: {theorem.theorem_id}")
        if representation.representation_id in seen_representations:
            raise ProvisionalPairCombineError(
                f"duplicate source representation: {representation.representation_id}"
            )
        seen_theorems.add(theorem.theorem_id)
        seen_representations.add(representation.representation_id)
        ordered.append((theorem, representation))

    theorem_ids = [theorem.theorem_id for theorem, _ in ordered]
    if hash_canonical(theorem_ids) != spec.ordered_theorem_ids_sha256:
        raise ProvisionalPairCombineError("ordered source theorem hash differs from run spec")
    return _SourceInventory(
        ordered=tuple(ordered),
        by_theorem_id={theorem.theorem_id: theorem for theorem, _ in ordered},
    )


def _journal_bindings(root: Path, manifest: RunManifest) -> tuple[FileBinding, ...]:
    journal_dir = root / "journal"
    if not journal_dir.is_dir() or journal_dir.is_symlink():
        raise ProvisionalPairCombineError(f"missing regular journal directory: {journal_dir}")
    expected_names = tuple(f"batch_{index:06d}.jsonl" for index in range(manifest.batch_count))
    actual_paths = tuple(sorted(journal_dir.iterdir(), key=lambda path: path.name))
    if tuple(path.name for path in actual_paths) != expected_names:
        raise ProvisionalPairCombineError("journal is incomplete or contains foreign files")
    bindings = tuple(_regular_file_binding(path, relative_to=root) for path in actual_paths)
    entries = [(Path(item.relative_path).name, item.sha256) for item in bindings]
    if hash_canonical(entries) != manifest.journal_tree_hash:
        raise ProvisionalPairCombineError("journal tree hash differs from manifest")
    return bindings


def _combined_file_hash(paths: Sequence[Path]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for path in paths:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    return digest.hexdigest(), size


def _result_model(run_kind: RunKind) -> type[MaterializationResult]:
    if run_kind == "e0":
        return cast(type[MaterializationResult], V2E0MaterializationResult)
    if run_kind == "e2":
        return cast(type[MaterializationResult], V2E2MaterializationResult)
    return cast(type[MaterializationResult], V2D0MaterializationResult)


def _has_infrastructure_error(result: MaterializationResult) -> bool:
    if "infrastructure" in result.terminal_status:
        return True
    if result.attempt.terminal_outcome == "infrastructure_error":
        return True
    lowered_codes = tuple(code.lower() for code in result.failure_codes)
    return any(token in code for code in lowered_codes for token in _INFRASTRUCTURE_FAILURE_TOKENS)


def _observation(
    *,
    root_binding_id: str,
    line_number: int,
    result: MaterializationResult,
    source_inventory: _SourceInventory,
) -> ProvisionalPairObservation:
    if result.terminal_status != "provisional_variant":
        raise ProvisionalPairCombineError("only provisional results can become observations")
    if any(
        value is None
        for value in (
            result.draft,
            result.candidate_theorem,
            result.candidate_representation,
            result.audit,
            result.variant,
        )
    ):
        raise ProvisionalPairCombineError("provisional result lacks complete lineage")
    assert result.draft is not None
    assert result.candidate_theorem is not None
    assert result.candidate_representation is not None
    assert result.audit is not None
    assert result.variant is not None
    verify_transformation_attempt_id(result.attempt)
    verify_variant_draft_id(result.draft)
    verify_transformation_audit_id(result.audit)
    sources: list[TheoremRecord] = []
    for theorem_id in result.draft.source_theorem_ids:
        source = source_inventory.by_theorem_id.get(theorem_id)
        if source is None:
            raise ProvisionalPairCombineError(
                f"provisional result references an unknown source theorem: {theorem_id}"
            )
        sources.append(source)
    if len(sources) != 1:
        raise ProvisionalPairCombineError(
            "deterministic-v2 combiner expects unary materializations"
        )
    source = sources[0]
    if result.candidate_representation.normalization_version != NORMALIZATION_VERSION:
        raise ProvisionalPairCombineError("candidate representation uses an unsupported version")
    if result.candidate_representation.content_hash != _representation_payload_hash(
        result.candidate_representation
    ):
        raise ProvisionalPairCombineError("candidate representation content hash is invalid")
    rebuilt_variant = build_deterministic_variant_record(
        attempt=result.attempt,
        draft=result.draft,
        audit=result.audit,
        candidate=result.candidate_theorem,
        candidate_representation=result.candidate_representation,
        polarity=result.variant.polarity_metadata,
        validation_evidence_id=result.variant.validation_evidence_id,
        metadata=result.variant.metadata,
    )
    if rebuilt_variant != result.variant:
        raise ProvisionalPairCombineError("stored provisional variant does not replay exactly")
    pair = build_deterministic_pair_record(
        source=source,
        candidate=result.candidate_theorem,
        draft=result.draft,
        audit=result.audit,
        all_sources=sources,
    )

    source_ids = tuple(sorted(result.draft.source_theorem_ids))
    source_representation_ids = tuple(sorted(result.draft.source_representation_ids))
    source_categories = tuple(sorted({item.source for item in sources}))
    source_roots = tuple(sorted({root for item in sources for root in item.root_ancestry_ids}))
    exact_pair_key = hash_canonical(
        {
            "schema": "deterministic_provisional_exact_pair_key_v1",
            "context_id": result.draft.context_id,
            "source_theorem_ids": source_ids,
            "candidate_code_hash": result.draft.candidate_code_hash,
        }
    )
    candidate_code_key = hash_canonical(
        {
            "schema": "deterministic_provisional_candidate_code_key_v1",
            "context_id": result.draft.context_id,
            "candidate_code_hash": result.draft.candidate_code_hash,
        }
    )
    ancestry_candidate_key = hash_canonical(
        {
            "schema": "deterministic_provisional_ancestry_candidate_key_v1",
            "context_id": result.draft.context_id,
            "root_ancestry_ids": source_roots,
            "candidate_code_hash": result.draft.candidate_code_hash,
        }
    )
    alpha = result.candidate_representation.alpha_identity_fingerprint
    alpha_candidate_key = (
        hash_canonical(
            {
                "schema": "deterministic_provisional_alpha_candidate_key_v1",
                "context_id": result.draft.context_id,
                "alpha_identity_fingerprint": alpha,
            }
        )
        if alpha is not None
        else None
    )
    data: dict[str, object] = {
        "root_binding_id": root_binding_id,
        "result_id": result.result_id,
        "result_line_number": line_number,
        "profile_id": result.profile_id,
        "family_id": result.draft.family_id,
        "rule_id": result.rule_id,
        "context_id": result.draft.context_id,
        "source_theorem_ids": source_ids,
        "source_representation_ids": source_representation_ids,
        "source_categories": source_categories,
        "source_root_ancestry_ids": source_roots,
        "pair_id": pair.pair_id,
        "attempt_id": result.attempt.attempt_id,
        "draft_id": result.draft.draft_id,
        "audit_id": result.audit.audit_id,
        "variant_id": result.variant.variant_id,
        "candidate_theorem_id": result.candidate_theorem.theorem_id,
        "candidate_representation_id": result.candidate_representation.representation_id,
        "candidate_code_hash": result.draft.candidate_code_hash,
        "candidate_alpha_identity_fingerprint": alpha,
        "intended_relation": result.draft.intended_relation,
        "polarity_metadata": result.variant.polarity_metadata,
        "exact_pair_key": exact_pair_key,
        "candidate_code_key": candidate_code_key,
        "ancestry_candidate_key": ancestry_candidate_key,
        "alpha_candidate_key": alpha_candidate_key,
    }
    placeholder = ProvisionalPairObservation.model_construct(
        _fields_set=None,
        observation_id=f"detprov_observation:{'0' * 64}",
        **data,
    )
    payload = placeholder.model_dump(mode="json")
    payload.pop("observation_id")
    return ProvisionalPairObservation.model_validate(
        {
            "observation_id": f"detprov_observation:{hash_canonical(payload)}",
            **data,
        }
    )


def _load_root(
    root: Path,
    *,
    allowed_infrastructure_result_ids: frozenset[str] = frozenset(),
) -> _LoadedRoot:
    root = root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ProvisionalPairCombineError(f"materialization root is not a directory: {root}")
    _validate_e2_recovery_metadata(root)
    initial_file_count, initial_tree_hash = _root_tree(root)
    run_kind, spec, manifest = _load_run_models(root)
    source_inventory = _load_source_inventory(spec)
    journal_bindings = _journal_bindings(root, manifest)
    journal_paths = tuple(root / item.relative_path for item in journal_bindings)
    combined_hash, combined_size = _combined_file_hash(journal_paths)
    results_path = root / _RESULTS
    if not results_path.is_file() or results_path.is_symlink():
        raise ProvisionalPairCombineError(f"materialization root lacks results.jsonl: {root}")
    if (
        combined_hash != manifest.results_sha256
        or hash_file(results_path) != manifest.results_sha256
        or results_path.stat().st_size != combined_size
    ):
        raise ProvisionalPairCombineError("results.jsonl does not exactly assemble the journal")

    model = _result_model(run_kind)
    results: list[tuple[int, MaterializationResult]] = []
    status_counts: Counter[str] = Counter()
    family_status_counts: Counter[str] = Counter()
    actual_attempt_keys: list[tuple[str, str, str, int]] = []
    rules: set[str] = set()
    seen_allowed_infrastructure_result_ids: set[str] = set()
    for line_number, raw, raw_line in _iter_jsonl_objects(results_path):
        try:
            result = model.model_validate(raw)
        except ValueError as exc:
            raise ProvisionalPairCombineError(
                f"invalid result at {results_path}:{line_number}: {exc}"
            ) from exc
        if raw_line != _canonical_line(result):
            raise ProvisionalPairCombineError(
                f"non-canonical result at {results_path}:{line_number}"
            )
        if result.profile_id != spec.profile_id:
            raise ProvisionalPairCombineError("result profile differs from run spec")
        if result.profile_config_hash != spec.profile_config_hash:
            raise ProvisionalPairCombineError("result config hash differs from run spec")
        if result.attempt.context_id != spec.context_id:
            raise ProvisionalPairCombineError("result context differs from run spec")
        if _has_infrastructure_error(result):
            if result.result_id not in allowed_infrastructure_result_ids:
                raise ProvisionalPairCombineError(
                    f"materialization root contains an infrastructure-error result at line "
                    f"{line_number}"
                )
            seen_allowed_infrastructure_result_ids.add(result.result_id)
        status_counts[result.terminal_status] += 1
        family_status_counts[f"{result.rule_id}:{result.terminal_status}"] += 1
        source_ids = result.attempt.source_theorem_ids
        representation_ids = result.attempt.source_representation_ids
        if len(source_ids) != 1 or len(representation_ids) != 1:
            raise ProvisionalPairCombineError("v2 scale result is not a unary attempt")
        actual_attempt_keys.append(
            (source_ids[0], representation_ids[0], result.rule_id, result.attempt.seed)
        )
        rules.add(result.rule_id)
        results.append((line_number, result))

    if seen_allowed_infrastructure_result_ids != set(allowed_infrastructure_result_ids):
        raise ProvisionalPairCombineError(
            "allowed infrastructure result IDs do not exactly match the validated root"
        )

    if len(results) != manifest.result_count or len(results) != spec.attempt_count:
        raise ProvisionalPairCombineError("result cardinality does not match spec/manifest")
    if dict(sorted(status_counts.items())) != manifest.terminal_status_counts:
        raise ProvisionalPairCombineError("terminal status counts differ from manifest")
    if dict(sorted(family_status_counts.items())) != manifest.family_status_counts:
        raise ProvisionalPairCombineError("family status counts differ from manifest")
    if hash_canonical(actual_attempt_keys) != spec.ordered_attempt_keys_sha256:
        raise ProvisionalPairCombineError("ordered result attempts differ from run spec")

    provisional_count = status_counts["provisional_variant"]
    if isinstance(
        spec,
        (V2D0ScaleRunSpec, V2E2ScaleRunSpec, V2E2ScaleRunSpecLegacyV2),
    ):
        execution_settings_provenance = "recorded"
        execution_workers = spec.workers
        execution_memory_hard_limit_mb = spec.memory_hard_limit_mb
    else:
        execution_settings_provenance = "legacy_unknown"
        execution_workers = None
        execution_memory_hard_limit_mb = None
    binding_data: dict[str, object] = {
        "root_path": str(root),
        "run_kind": run_kind,
        "profile_id": spec.profile_id,
        "rule_ids": tuple(sorted(rules)),
        "context_id": spec.context_id,
        "execution_settings_provenance": execution_settings_provenance,
        "workers": execution_workers,
        "memory_hard_limit_mb": execution_memory_hard_limit_mb,
        "run_spec": _regular_file_binding(root / _RUN_SPEC, relative_to=root),
        "manifest": _regular_file_binding(root / _MANIFEST, relative_to=root),
        "results": _regular_file_binding(results_path, relative_to=root),
        "journal_files": journal_bindings,
        "root_file_count": initial_file_count,
        "root_tree_hash": initial_tree_hash,
        "theorem_partition_path": str(Path(spec.theorem_partition).resolve(strict=True)),
        "theorem_partition_sha256": spec.theorem_partition_sha256,
        "representation_partition_path": str(
            Path(spec.representation_partition).resolve(strict=True)
        ),
        "representation_partition_sha256": spec.representation_partition_sha256,
        "source_count": spec.source_count,
        "result_count": manifest.result_count,
        "provisional_count": provisional_count,
    }
    placeholder = MaterializationRootBinding.model_construct(
        _fields_set=None,
        root_binding_id=f"detprov_root:{'0' * 64}",
        **binding_data,
    )
    binding_payload = placeholder.model_dump(mode="json")
    binding_payload.pop("root_binding_id")
    binding_identity_payload = _root_binding_identity_payload(binding_payload)
    binding = MaterializationRootBinding.model_validate(
        {
            "root_binding_id": f"detprov_root:{hash_canonical(binding_identity_payload)}",
            **binding_data,
        }
    )
    observations = tuple(
        _observation(
            root_binding_id=binding.root_binding_id,
            line_number=line_number,
            result=result,
            source_inventory=source_inventory,
        )
        for line_number, result in results
        if result.terminal_status == "provisional_variant"
    )
    if len(observations) != provisional_count:
        raise ProvisionalPairCombineError("provisional observation count does not reconcile")

    final_file_count, final_tree_hash = _root_tree(root)
    if (final_file_count, final_tree_hash) != (initial_file_count, initial_tree_hash):
        raise ProvisionalPairCombineError(f"materialization root changed during audit: {root}")
    if hash_file(Path(spec.theorem_partition)) != spec.theorem_partition_sha256:
        raise ProvisionalPairCombineError("theorem partition changed during audit")
    if hash_file(Path(spec.representation_partition)) != spec.representation_partition_sha256:
        raise ProvisionalPairCombineError("representation partition changed during audit")
    return _LoadedRoot(binding=binding, observations=observations)


def _unique_pairs(
    observations: Sequence[ProvisionalPairObservation],
) -> tuple[UniqueProvisionalPair, ...]:
    grouped: dict[str, list[ProvisionalPairObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.exact_pair_key].append(observation)
    unique: list[UniqueProvisionalPair] = []
    for key, group in sorted(grouped.items()):
        first = group[0]
        if any(
            item.context_id != first.context_id
            or item.source_theorem_ids != first.source_theorem_ids
            or item.candidate_code_hash != first.candidate_code_hash
            for item in group
        ):
            raise ProvisionalPairCombineError("exact pair key collision detected")
        relations = tuple(sorted({item.intended_relation for item in group}, key=str))
        polarities = tuple(sorted({item.polarity_metadata for item in group}, key=str))
        unique.append(
            UniqueProvisionalPair(
                unique_pair_id=f"detprov_pair:{key}",
                exact_pair_key=key,
                context_id=first.context_id,
                source_theorem_ids=first.source_theorem_ids,
                candidate_code_hash=first.candidate_code_hash,
                observation_ids=tuple(sorted(item.observation_id for item in group)),
                provenance_count=len(group),
                family_ids=tuple(sorted({item.family_id for item in group})),
                source_categories=tuple(
                    sorted({source for item in group for source in item.source_categories})
                ),
                intended_relations=relations,
                polarity_metadata=polarities,
                conflicting_intentions=len(relations) > 1 or len(polarities) > 1,
            )
        )
    return tuple(unique)


def _overlap_audit(
    observations: Sequence[ProvisionalPairObservation],
    field_name: Literal[
        "exact_pair_key",
        "candidate_code_key",
        "ancestry_candidate_key",
        "alpha_candidate_key",
    ],
) -> OverlapAudit:
    counts: Counter[str] = Counter()
    for item in observations:
        value = getattr(item, field_name)
        if value is not None:
            counts[value] += 1
    return OverlapAudit(
        key_name=field_name,
        observation_count=len(observations),
        key_available_count=sum(counts.values()),
        unique_key_count=len(counts),
        duplicate_group_count=sum(count > 1 for count in counts.values()),
        duplicate_excess_count=sum(counts.values()) - len(counts),
    )


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_line(record) for record in records)


def _write_new_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _verify_existing(output_dir: Path, payloads: Mapping[str, bytes]) -> None:
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise ProvisionalPairCombineError(f"existing output is not a directory: {output_dir}")
    actual = {path.name for path in output_dir.iterdir()}
    if actual != _EXPECTED_OUTPUTS:
        raise ProvisionalPairCombineError(
            "existing output is not an exact replay; "
            f"expected {sorted(_EXPECTED_OUTPUTS)}, found {sorted(actual)}"
        )
    for name, payload in payloads.items():
        path = output_dir / name
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise ProvisionalPairCombineError(f"existing output differs: {path}")


def combine_provisional_pair_roots(
    *,
    materialization_roots: Sequence[Path],
    output_dir: Path,
) -> ProvisionalPairCombinationArtifacts:
    """Audit and exactly deduplicate completed deterministic-v2 roots."""

    if not materialization_roots:
        raise ProvisionalPairCombineError("at least one materialization root is required")
    resolved_roots = tuple(sorted(path.resolve(strict=True) for path in materialization_roots))
    if len(resolved_roots) != len(set(resolved_roots)):
        raise ProvisionalPairCombineError("materialization roots must be unique")
    output_dir = output_dir.resolve(strict=False)
    for root in resolved_roots:
        if output_dir == root or output_dir.is_relative_to(root):
            raise ProvisionalPairCombineError(
                "output directory cannot be a materialization root or its descendant"
            )

    loaded = tuple(_load_root(root) for root in resolved_roots)
    root_bindings = tuple(
        sorted((item.binding for item in loaded), key=lambda item: item.root_binding_id)
    )
    if len({item.root_binding_id for item in root_bindings}) != len(root_bindings):
        raise ProvisionalPairCombineError("duplicate materialization root bindings detected")
    observations = tuple(
        sorted(
            (observation for item in loaded for observation in item.observations),
            key=lambda item: item.observation_id,
        )
    )
    if len({item.observation_id for item in observations}) != len(observations):
        raise ProvisionalPairCombineError(
            "duplicate gross observation identities detected across materialization roots"
        )
    unique = _unique_pairs(observations)

    gross_family = Counter(item.family_id for item in observations)
    gross_source = Counter(source for item in observations for source in item.source_categories)
    unique_family = Counter(family for item in unique for family in item.family_ids)
    unique_source = Counter(source for item in unique for source in item.source_categories)
    duplicate_groups = tuple(item for item in unique if item.provenance_count > 1)
    gross_payload = _canonical_jsonl(observations)
    unique_payload = _canonical_jsonl(unique)
    manifest_data: dict[str, object] = {
        "root_bindings": root_bindings,
        "gross_observation_count": len(observations),
        "unique_pair_count": len(unique),
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_excess_count": len(observations) - len(unique),
        "cross_family_duplicate_group_count": sum(
            len(item.family_ids) > 1 for item in duplicate_groups
        ),
        "cross_source_duplicate_group_count": sum(
            len(item.source_categories) > 1 for item in duplicate_groups
        ),
        "gross_counts_by_family": dict(sorted(gross_family.items())),
        "unique_counts_by_family": dict(sorted(unique_family.items())),
        "gross_counts_by_source": dict(sorted(gross_source.items())),
        "unique_counts_by_source": dict(sorted(unique_source.items())),
        "overlap_audits": tuple(
            _overlap_audit(observations, name)
            for name in (
                "exact_pair_key",
                "candidate_code_key",
                "ancestry_candidate_key",
                "alpha_candidate_key",
            )
        ),
        "gross_output_sha256": hashlib.sha256(gross_payload).hexdigest(),
        "unique_output_sha256": hashlib.sha256(unique_payload).hexdigest(),
    }
    placeholder = ProvisionalPairCombinationManifest.model_construct(
        _fields_set=None,
        combination_hash="0" * 64,
        **manifest_data,
    )
    manifest_payload = _combination_identity_payload(placeholder.model_dump(mode="json"))
    manifest = ProvisionalPairCombinationManifest.model_validate(
        {"combination_hash": hash_canonical(manifest_payload), **manifest_data}
    )
    payloads = {
        _GROSS_OUTPUT: gross_payload,
        _UNIQUE_OUTPUT: unique_payload,
        _COMBINED_MANIFEST: _canonical_line(manifest),
    }

    if output_dir.exists():
        _verify_existing(output_dir, payloads)
        replayed = True
    else:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
        try:
            for name, payload in payloads.items():
                _write_new_file(temporary / name, payload)
            try:
                os.rename(temporary, output_dir)
            except FileExistsError:
                _verify_existing(output_dir, payloads)
                replayed = True
            else:
                replayed = False
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    return ProvisionalPairCombinationArtifacts(
        output_dir=output_dir,
        manifest_path=output_dir / _COMBINED_MANIFEST,
        gross_path=output_dir / _GROSS_OUTPUT,
        unique_path=output_dir / _UNIQUE_OUTPUT,
        combination_hash=manifest.combination_hash,
        gross_count=len(observations),
        unique_count=len(unique),
        replayed=replayed,
    )


__all__ = [
    "MaterializationRootBinding",
    "OverlapAudit",
    "ProvisionalPairCombinationArtifacts",
    "ProvisionalPairCombinationManifest",
    "ProvisionalPairCombineError",
    "ProvisionalPairObservation",
    "UniqueProvisionalPair",
    "combine_provisional_pair_roots",
]
