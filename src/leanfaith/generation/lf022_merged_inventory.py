"""Authoritative audit-only inventory of the current checked LF-022 partitions.

This module reconciles already-generated provisional variants across several
historical selection mechanisms.  It verifies the frozen selector, Lean-check,
source-line, and optional Codex-audit bindings before it emits a deduplicated
inventory.  It does *not* infer labels, promote examples, or claim that LF-022
generation is complete.

The inventory is intentionally bounded by a content-addressed specification.
Adding a new partition requires a new spec and a new inventory; an evolving
execution tree can never enter silently.
"""

from __future__ import annotations

import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import ConfigError, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_batch import LF022PublicBatchManifest
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditError,
    LF022CodexAuditManifest,
    LF022VerifiedCodexAuditJudgment,
    verify_completed_lf022_codex_audit,
)
from leanfaith.generation.lf022_inventory_snapshot import source_candidate_pair_hash
from leanfaith.generation.lf022_lean_check import (
    LF022LeanCheckManifest,
    LF022LeanCheckRecord,
)
from leanfaith.generation.lf022_postgen_reconcile import (
    LF022PostgenTerminalSelector,
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.variant import VariantRecord

LF022_MERGED_INVENTORY_VERSION: Literal["lf022_merged_checked_inventory_v1"] = (
    "lf022_merged_checked_inventory_v1"
)
_LEAN_VALID_OUTCOMES = frozenset({"elaborates", "elaborates_with_placeholder"})
SelectionKind = Literal[
    "legacy_check_input_scan",
    "frozen_batch_manifest",
    "verified_terminal_selector",
]


class LF022MergedInventoryError(RuntimeError):
    """A frozen LF-022 inventory binding or reconciliation invariant failed."""


class MergedArtifactBinding(StrictModel):
    """Exact path and byte hash for one immutable inventory input."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)


class LF022MergedPartitionExpected(StrictModel):
    """Frozen cardinalities for one checked source partition."""

    gross_observation_count: int = Field(ge=0, strict=True)
    lean_valid_observation_count: int = Field(ge=0, strict=True)
    audited_observation_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.audited_observation_count > self.lean_valid_observation_count:
            raise ValueError("audited observations exceed Lean-valid observations")
        if self.lean_valid_observation_count > self.gross_observation_count:
            raise ValueError("Lean-valid observations exceed gross observations")
        return self


class LF022MergedInventoryCounts(StrictModel):
    """Gross and exact-key counts for the merged checked inventory."""

    gross_observation_count: int = Field(ge=0, strict=True)
    unique_variant_id_count: int = Field(ge=0, strict=True)
    unique_candidate_content_count: int = Field(ge=0, strict=True)
    unique_pair_key_count: int = Field(ge=0, strict=True)
    lean_valid_gross_observation_count: int = Field(ge=0, strict=True)
    lean_valid_unique_variant_id_count: int = Field(ge=0, strict=True)
    lean_valid_unique_candidate_content_count: int = Field(ge=0, strict=True)
    lean_valid_unique_pair_key_count: int = Field(ge=0, strict=True)
    audited_gross_observation_count: int = Field(ge=0, strict=True)
    audited_unique_variant_id_count: int = Field(ge=0, strict=True)
    audited_unique_candidate_content_count: int = Field(ge=0, strict=True)
    audited_unique_pair_key_count: int = Field(ge=0, strict=True)
    lean_valid_unaudited_pair_key_count: int = Field(ge=0, strict=True)
    duplicate_pair_observation_count: int = Field(ge=0, strict=True)
    cross_partition_pair_key_count: int = Field(ge=0, strict=True)
    cross_model_pair_key_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _reconciled(self) -> Self:
        if self.duplicate_pair_observation_count != (
            self.gross_observation_count - self.unique_pair_key_count
        ):
            raise ValueError("duplicate pair observations do not reconcile")
        if self.lean_valid_gross_observation_count > self.gross_observation_count:
            raise ValueError("Lean-valid gross count exceeds gross count")
        if self.audited_gross_observation_count > self.lean_valid_gross_observation_count:
            raise ValueError("audited gross count exceeds Lean-valid gross count")
        if self.lean_valid_unaudited_pair_key_count != (
            self.lean_valid_unique_pair_key_count - self.audited_unique_pair_key_count
        ):
            raise ValueError("Lean-valid unaudited pair count does not reconcile")
        for prefix in ("unique", "lean_valid_unique", "audited_unique"):
            candidate = cast(int, getattr(self, f"{prefix}_candidate_content_count"))
            pair = cast(int, getattr(self, f"{prefix}_pair_key_count"))
            if candidate > pair:
                raise ValueError("candidate-content uniqueness cannot exceed pair uniqueness")
        return self


class LF022MergedConflictCounts(StrictModel):
    """Pair-key-level mechanical and audit-diagnostic disagreements."""

    lean_outcome_conflict_pair_key_count: int = Field(ge=0, strict=True)
    audit_same_claim_conflict_pair_key_count: int = Field(ge=0, strict=True)
    audit_relation_conflict_pair_key_count: int = Field(ge=0, strict=True)
    audit_directional_conflict_pair_key_count: int = Field(ge=0, strict=True)
    audit_any_core_tuple_conflict_pair_key_count: int = Field(ge=0, strict=True)


class LF022MergedExpected(StrictModel):
    """Frozen observed result required before this exact spec can materialize."""

    by_partition: dict[str, LF022MergedPartitionExpected]
    counts: LF022MergedInventoryCounts
    pairwise_partition_intersections: dict[str, int]
    conflicts: LF022MergedConflictCounts


class LF022MergedPartitionSpec(StrictModel):
    """One bounded historical partition and its selection adapter."""

    partition_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    source_root: str = Field(min_length=1)
    selection_kind: SelectionKind
    selection_artifact: MergedArtifactBinding | None = None
    lean_check_manifest: MergedArtifactBinding
    codex_audit_manifest: MergedArtifactBinding | None = None

    @model_validator(mode="after")
    def _selection_shape(self) -> Self:
        if self.selection_kind == "legacy_check_input_scan":
            if self.selection_artifact is not None:
                raise ValueError("legacy check-input selection cannot bind a selector artifact")
        elif self.selection_artifact is None:
            raise ValueError("batch and terminal-selector partitions require a selector artifact")
        return self


class LF022MergedInventorySpec(StrictModel):
    """Content-addressed specification of the bounded merged inventory."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_merged_checked_inventory_v1"] = LF022_MERGED_INVENTORY_VERSION
    inventory_spec_id: str = Field(pattern=id_pattern("lf022_merged_inventory_spec"))
    partitions: tuple[LF022MergedPartitionSpec, ...] = Field(min_length=1)
    expected: LF022MergedExpected
    audit_only: Literal[True] = True
    inventory_only: Literal[True] = True
    generation_complete: Literal[False] = False
    human_labels_created: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    gold_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        partition_ids = tuple(item.partition_id for item in self.partitions)
        if len(partition_ids) != len(set(partition_ids)):
            raise ValueError("partition IDs must be unique")
        if set(self.expected.by_partition) != set(partition_ids):
            raise ValueError("expected partition counts must cover exactly the frozen partitions")
        expected_pairs = {
            f"{left} | {right}"
            for index, left in enumerate(partition_ids)
            for right in partition_ids[index + 1 :]
        }
        if set(self.expected.pairwise_partition_intersections) != expected_pairs:
            raise ValueError("expected pairwise intersections do not cover every partition pair")
        expected_id = make_id(
            "lf022_merged_inventory_spec",
            _spec_id_values(self),
        )
        if self.inventory_spec_id != expected_id:
            raise ValueError("inventory_spec_id does not match the frozen specification")
        return self


class _LegacyLeanCheckManifest(StrictModel):
    """Reader for the pre-selector Lean-check manifest retained in legacy668."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_provisional_lean_check_v1"] = "lf022_provisional_lean_check_v1"
    input_root: str
    input_set_hash: str = Field(pattern=HEX64_PATTERN)
    record_count: int = Field(ge=0, strict=True)
    ordered_variant_ids_hash: str = Field(pattern=HEX64_PATTERN)
    checks_artifact: str
    checks_sha256: str = Field(pattern=HEX64_PATTERN)
    status_counts: dict[str, int]
    outcome_counts: dict[str, int]
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False


class LF022MergedAuditDiagnostic(StrictModel):
    """One replay-verified Codex answer retained as diagnostic evidence only."""

    audit_item_id: str = Field(pattern=id_pattern("lf022_codex_audit_item"))
    pair_id: str = Field(pattern=id_pattern("pair"))
    same_claim_answer: Literal["same_claim", "not_same_claim", "ambiguous", "uncertain"]
    relation: (
        Literal[
            "equivalent",
            "A_stronger",
            "B_stronger",
            "incomparable",
            "unrelated",
            "ambiguous",
        ]
        | None
    )
    a_implies_b: Literal["yes", "no", "unknown"]
    b_implies_a: Literal["yes", "no", "unknown"]
    final_message_sha256: str = Field(pattern=HEX64_PATTERN)
    parsed_response_sha256: str = Field(pattern=HEX64_PATTERN)
    audit_only: Literal[True] = True
    semantic_label: Literal[False] = False
    silver_record: Literal[False] = False
    gold_record: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False


class LF022MergedObservation(StrictModel):
    """One exact checked variant occurrence in one historical partition."""

    schema_version: Literal[1] = 1
    observation_id: str = Field(pattern=id_pattern("lf022_merged_observation"))
    partition_id: str
    check_id: str = Field(pattern=id_pattern("lf022_lean_check"))
    variant_id: str = Field(pattern=id_pattern("var"))
    pair_key: str = Field(pattern=HEX64_PATTERN)
    candidate_code_hash: str = Field(pattern=HEX64_PATTERN)
    source_theorem_ids: tuple[str, ...] = Field(min_length=1)
    proposer_model: str = Field(min_length=1)
    source_variant_artifact: str
    source_variant_artifact_sha256: str = Field(pattern=HEX64_PATTERN)
    source_variant_line_number: int = Field(ge=1, strict=True)
    source_variant_line_sha256: str = Field(pattern=HEX64_PATTERN)
    lean_outcome: str
    lean_valid: bool
    audit: LF022MergedAuditDiagnostic | None = None
    audit_only: Literal[True] = True
    inventory_only: Literal[True] = True
    generation_complete: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    gold_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if self.lean_valid != (self.lean_outcome in _LEAN_VALID_OUTCOMES):
            raise ValueError("lean_valid differs from the mechanical Lean outcome")
        if self.audit is not None and not self.lean_valid:
            raise ValueError("non-Lean-valid observation cannot carry an audit")
        expected = make_id(
            "lf022_merged_observation",
            _observation_id_values(self),
        )
        if self.observation_id != expected:
            raise ValueError("observation_id does not match observation content")
        return self


class LF022MergedPairRecord(StrictModel):
    """All checked observations grouped by the canonical source/candidate key."""

    schema_version: Literal[1] = 1
    merged_pair_id: str = Field(pattern=id_pattern("lf022_merged_pair"))
    pair_key: str = Field(pattern=HEX64_PATTERN)
    observation_ids: tuple[str, ...] = Field(min_length=1)
    partition_ids: tuple[str, ...] = Field(min_length=1)
    proposer_models: tuple[str, ...] = Field(min_length=1)
    variant_ids: tuple[str, ...] = Field(min_length=1)
    candidate_code_hashes: tuple[str, ...] = Field(min_length=1)
    lean_outcomes: tuple[str, ...] = Field(min_length=1)
    lean_outcome_conflict: bool
    audit_observation_count: int = Field(ge=0, strict=True)
    audit_same_claim_values: tuple[str, ...]
    audit_relation_values: tuple[str, ...]
    audit_directional_values: tuple[str, ...]
    audit_core_tuple_values: tuple[str, ...]
    audit_same_claim_conflict: bool
    audit_relation_conflict: bool
    audit_directional_conflict: bool
    audit_any_core_tuple_conflict: bool
    audit_only: Literal[True] = True
    inventory_only: Literal[True] = True
    generation_complete: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    gold_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        for name in (
            "observation_ids",
            "partition_ids",
            "proposer_models",
            "variant_ids",
            "candidate_code_hashes",
            "lean_outcomes",
            "audit_same_claim_values",
            "audit_relation_values",
            "audit_directional_values",
            "audit_core_tuple_values",
        ):
            values = cast(tuple[str, ...], getattr(self, name))
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be sorted and unique")
        if self.lean_outcome_conflict != (len(self.lean_outcomes) > 1):
            raise ValueError("Lean outcome conflict flag does not reconcile")
        for values_name, flag_name in (
            ("audit_same_claim_values", "audit_same_claim_conflict"),
            ("audit_relation_values", "audit_relation_conflict"),
            ("audit_directional_values", "audit_directional_conflict"),
            ("audit_core_tuple_values", "audit_any_core_tuple_conflict"),
        ):
            if cast(bool, getattr(self, flag_name)) != (
                len(cast(tuple[str, ...], getattr(self, values_name))) > 1
            ):
                raise ValueError(f"{flag_name} does not reconcile")
        expected = make_id(
            "lf022_merged_pair",
            self.model_dump(mode="json", exclude={"merged_pair_id"}),
        )
        if self.merged_pair_id != expected:
            raise ValueError("merged_pair_id does not match pair content")
        return self


class LF022MergedPartitionReport(StrictModel):
    """Verified frozen input bindings and counts for one partition."""

    partition_id: str
    selection_kind: SelectionKind
    source_root: str
    selection_artifact_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    lean_check_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    checks_artifact_sha256: str = Field(pattern=HEX64_PATTERN)
    codex_audit_manifest_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    codex_response_artifact_set_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    counts: LF022MergedPartitionExpected
    audit_only: Literal[True] = True
    inventory_only: Literal[True] = True
    generation_complete: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    gold_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False


class LF022MergedInventoryReport(StrictModel):
    """Content-addressed manifest for the immutable merged inventory files."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_merged_checked_inventory_v1"] = LF022_MERGED_INVENTORY_VERSION
    inventory_id: str = Field(pattern=id_pattern("lf022_merged_checked_inventory"))
    inventory_spec_id: str = Field(pattern=id_pattern("lf022_merged_inventory_spec"))
    inventory_spec_sha256: str = Field(pattern=HEX64_PATTERN)
    observations_artifact: Literal["observations.jsonl"] = "observations.jsonl"
    observations_sha256: str = Field(pattern=HEX64_PATTERN)
    pairs_artifact: Literal["pairs.jsonl"] = "pairs.jsonl"
    pairs_sha256: str = Field(pattern=HEX64_PATTERN)
    partitions: tuple[LF022MergedPartitionReport, ...] = Field(min_length=1)
    counts: LF022MergedInventoryCounts
    pairwise_partition_intersections: dict[str, int]
    conflicts: LF022MergedConflictCounts
    audit_only: Literal[True] = True
    inventory_only: Literal[True] = True
    readiness_claimed: Literal[False] = False
    generation_complete: Literal[False] = False
    human_labels_created: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    gold_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        expected = make_id(
            "lf022_merged_checked_inventory",
            _report_id_values(self),
        )
        if self.inventory_id != expected:
            raise ValueError("inventory_id does not match report content")
        return self


@dataclass(frozen=True, slots=True)
class LF022MergedInventoryBundle:
    """In-memory canonical output bundle."""

    observations: tuple[LF022MergedObservation, ...]
    pairs: tuple[LF022MergedPairRecord, ...]
    report: LF022MergedInventoryReport
    observations_bytes: bytes
    pairs_bytes: bytes


@dataclass(frozen=True, slots=True)
class LF022MergedInventoryWriteResult:
    """Paths and hashes from one exact materialization or replay."""

    output_dir: Path
    observations_path: Path
    pairs_path: Path
    report_path: Path
    report: LF022MergedInventoryReport
    replayed: bool


@dataclass(frozen=True, slots=True)
class _CheckManifestView:
    input_root: str
    input_set_hash: str
    record_count: int
    ordered_variant_ids_hash: str
    checks_artifact: str
    checks_sha256: str
    status_counts: dict[str, int]
    outcome_counts: dict[str, int]
    selection_batch_id: str | None
    selection_batch_manifest: str | None
    selection_batch_manifest_sha256: str | None
    selection_postgen_selector_id: str | None
    selection_postgen_selector: str | None
    selection_postgen_selector_sha256: str | None
    selected_execution_task_count: int | None


@dataclass(frozen=True, slots=True)
class _LoadedPartition:
    report: LF022MergedPartitionReport
    observations: tuple[LF022MergedObservation, ...]


def _spec_id_values(spec: LF022MergedInventorySpec) -> dict[str, object]:
    """Machine-independent logical identity for a path-bound frozen spec."""

    return {
        "schema_version": spec.schema_version,
        "method_version": spec.method_version,
        "partitions": [
            {
                "partition_id": item.partition_id,
                "selection_kind": item.selection_kind,
                "selection_sha256": (
                    item.selection_artifact.sha256 if item.selection_artifact else None
                ),
                "lean_check_manifest_sha256": item.lean_check_manifest.sha256,
                "codex_audit_manifest_sha256": (
                    item.codex_audit_manifest.sha256 if item.codex_audit_manifest else None
                ),
            }
            for item in spec.partitions
        ],
        "expected": spec.expected.model_dump(mode="json"),
        "audit_only": spec.audit_only,
        "inventory_only": spec.inventory_only,
        "generation_complete": spec.generation_complete,
        "human_labels_created": spec.human_labels_created,
        "semantic_labels_created": spec.semantic_labels_created,
        "silver_records_created": spec.silver_records_created,
        "gold_records_created": spec.gold_records_created,
        "training_eligible": spec.training_eligible,
        "evaluation_eligible": spec.evaluation_eligible,
        "gate_credit_claimed": spec.gate_credit_claimed,
    }


def _observation_id_values(observation: LF022MergedObservation) -> dict[str, object]:
    values = observation.model_dump(
        mode="json",
        exclude={"observation_id", "source_variant_artifact"},
    )
    return cast(dict[str, object], values)


def _report_id_values(report: LF022MergedInventoryReport) -> dict[str, object]:
    values = report.model_dump(mode="json", exclude={"inventory_id", "partitions"})
    values["partitions"] = [
        item.model_dump(mode="json", exclude={"source_root"}) for item in report.partitions
    ]
    return cast(dict[str, object], values)


def _safe_file(path: Path, *, label: str) -> Path:
    candidate = path.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LF022MergedInventoryError(f"{label} traverses a symlink: {path}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LF022MergedInventoryError(f"{label} is missing: {path}") from exc
    if resolved != candidate or not resolved.is_file():
        raise LF022MergedInventoryError(f"{label} is not a canonical regular file: {path}")
    return resolved


def _safe_root(path_text: str) -> Path:
    return _safe_directory(Path(path_text), label="partition source root")


def _safe_directory(path: Path, *, label: str) -> Path:
    candidate = path.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LF022MergedInventoryError(f"{label} traverses a symlink: {path}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LF022MergedInventoryError(f"{label} is missing: {path}") from exc
    if resolved != candidate or not resolved.is_dir():
        raise LF022MergedInventoryError(f"{label} is not a canonical directory: {path}")
    return resolved


def _safe_output_directory(path: Path) -> Path:
    """Create an output directory without following any symlink component."""

    candidate = path.absolute()
    if ".." in candidate.parts:
        raise LF022MergedInventoryError("output directory must be normalized")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LF022MergedInventoryError("output directory traverses a symlink")
        if current.exists() and not current.is_dir():
            raise LF022MergedInventoryError("output directory component is not a directory")
        current.mkdir(exist_ok=True)
        if current.is_symlink() or not current.is_dir():
            raise LF022MergedInventoryError("output directory became unsafe during creation")
    if candidate.resolve(strict=True) != candidate:
        raise LF022MergedInventoryError("output directory is not canonical")
    return candidate


def _resolve_artifact(path_text: str, *, source_root: Path, label: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        resolved = _safe_file(path, label=label)
    else:
        pure = PurePosixPath(path_text)
        if not path_text.strip() or ".." in pure.parts or "." in pure.parts or "\\" in path_text:
            raise LF022MergedInventoryError(f"unsafe {label} path: {path_text}")
        resolved = _safe_file(source_root / pure, label=label)
    return resolved


def _load_spec(
    spec_path: Path,
    *,
    expected_spec_sha256: str,
) -> tuple[LF022MergedInventorySpec, str]:
    path = _safe_file(spec_path, label="merged inventory specification")
    observed = hash_file(path)
    if observed != expected_spec_sha256:
        raise LF022MergedInventoryError(
            f"merged inventory spec hash mismatch: expected {expected_spec_sha256}, "
            f"observed {observed}"
        )
    try:
        loaded = load_config(path, LF022MergedInventorySpec)
    except ConfigError as exc:
        raise LF022MergedInventoryError(f"invalid merged inventory spec: {exc}") from exc
    return loaded.config, observed


def _manifest_view(
    partition: LF022MergedPartitionSpec,
    *,
    source_root: Path,
) -> tuple[_CheckManifestView, Path]:
    manifest_path = _resolve_artifact(
        partition.lean_check_manifest.path,
        source_root=source_root,
        label=f"{partition.partition_id} Lean-check manifest",
    )
    observed = hash_file(manifest_path)
    if observed != partition.lean_check_manifest.sha256:
        raise LF022MergedInventoryError(
            f"{partition.partition_id} Lean-check manifest hash mismatch"
        )
    raw = manifest_path.read_bytes()
    try:
        if partition.selection_kind == "legacy_check_input_scan":
            manifest: _LegacyLeanCheckManifest | LF022LeanCheckManifest = (
                _LegacyLeanCheckManifest.model_validate_json(raw)
            )
        else:
            manifest = LF022LeanCheckManifest.model_validate_json(raw)
    except ValueError as exc:
        raise LF022MergedInventoryError(
            f"invalid {partition.partition_id} Lean-check manifest: {exc}"
        ) from exc
    if isinstance(manifest, _LegacyLeanCheckManifest):
        view = _CheckManifestView(
            input_root=manifest.input_root,
            input_set_hash=manifest.input_set_hash,
            record_count=manifest.record_count,
            ordered_variant_ids_hash=manifest.ordered_variant_ids_hash,
            checks_artifact=manifest.checks_artifact,
            checks_sha256=manifest.checks_sha256,
            status_counts=manifest.status_counts,
            outcome_counts=manifest.outcome_counts,
            selection_batch_id=None,
            selection_batch_manifest=None,
            selection_batch_manifest_sha256=None,
            selection_postgen_selector_id=None,
            selection_postgen_selector=None,
            selection_postgen_selector_sha256=None,
            selected_execution_task_count=None,
        )
    else:
        view = _CheckManifestView(
            input_root=manifest.input_root,
            input_set_hash=manifest.input_set_hash,
            record_count=manifest.record_count,
            ordered_variant_ids_hash=manifest.ordered_variant_ids_hash,
            checks_artifact=manifest.checks_artifact,
            checks_sha256=manifest.checks_sha256,
            status_counts=manifest.status_counts,
            outcome_counts=manifest.outcome_counts,
            selection_batch_id=manifest.selection_batch_id,
            selection_batch_manifest=manifest.selection_batch_manifest,
            selection_batch_manifest_sha256=manifest.selection_batch_manifest_sha256,
            selection_postgen_selector_id=manifest.selection_postgen_selector_id,
            selection_postgen_selector=manifest.selection_postgen_selector,
            selection_postgen_selector_sha256=manifest.selection_postgen_selector_sha256,
            selected_execution_task_count=manifest.selected_execution_task_count,
        )
    return view, manifest_path


def _verify_selection(
    partition: LF022MergedPartitionSpec,
    *,
    source_root: Path,
    manifest: _CheckManifestView,
) -> None:
    binding = partition.selection_artifact
    if partition.selection_kind == "legacy_check_input_scan":
        if any(
            value is not None
            for value in (
                manifest.selection_batch_id,
                manifest.selection_batch_manifest,
                manifest.selection_postgen_selector_id,
                manifest.selection_postgen_selector,
                manifest.selected_execution_task_count,
            )
        ):
            raise LF022MergedInventoryError("legacy partition unexpectedly carries a selector")
        return
    assert binding is not None
    selection_path = _resolve_artifact(
        binding.path,
        source_root=source_root,
        label=f"{partition.partition_id} selection artifact",
    )
    if hash_file(selection_path) != binding.sha256:
        raise LF022MergedInventoryError(f"{partition.partition_id} selector hash mismatch")
    if partition.selection_kind == "frozen_batch_manifest":
        if Path(binding.path).is_absolute():
            raise LF022MergedInventoryError(
                "frozen batch selection path must be repository-relative"
            )
        if (
            manifest.selection_batch_manifest != binding.path
            or manifest.selection_batch_manifest_sha256 != binding.sha256
            or manifest.selection_batch_id is None
            or manifest.selected_execution_task_count is None
            or manifest.selection_postgen_selector is not None
        ):
            raise LF022MergedInventoryError("Lean-check manifest does not bind the frozen batch")
        raw = selection_path.read_bytes()
        try:
            batch = LF022PublicBatchManifest.model_validate_json(raw)
        except ValueError as exc:
            raise LF022MergedInventoryError(f"invalid frozen batch selector: {exc}") from exc
        canonical = canonical_json_bytes(batch.model_dump(mode="json"))
        if raw not in {canonical, canonical + b"\n"}:
            raise LF022MergedInventoryError("frozen batch selector is not canonical JSON")
        if (
            batch.batch_id != manifest.selection_batch_id
            or batch.total_task_count != manifest.selected_execution_task_count
        ):
            raise LF022MergedInventoryError("frozen batch cardinality or identity differs")
        return
    if (
        manifest.selection_postgen_selector != binding.path
        or manifest.selection_postgen_selector_sha256 != binding.sha256
        or manifest.selection_postgen_selector_id is None
        or manifest.selected_execution_task_count is None
        or manifest.selection_batch_manifest is not None
    ):
        raise LF022MergedInventoryError("Lean-check manifest does not bind the terminal selector")
    raw = selection_path.read_bytes()
    try:
        selector = LF022PostgenTerminalSelector.model_validate_json(raw)
    except ValueError as exc:
        raise LF022MergedInventoryError(f"invalid terminal selector: {exc}") from exc
    canonical = canonical_json_bytes(selector.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise LF022MergedInventoryError("terminal selector is not canonical JSON")
    if (
        selector.selector_id != manifest.selection_postgen_selector_id
        or selector.task_count != manifest.selected_execution_task_count
    ):
        raise LF022MergedInventoryError("terminal selector cardinality or identity differs")


def _load_checks(
    partition: LF022MergedPartitionSpec,
    *,
    source_root: Path,
    manifest: _CheckManifestView,
) -> tuple[list[LF022LeanCheckRecord], Path]:
    checks_path = _resolve_artifact(
        manifest.checks_artifact,
        source_root=source_root,
        label=f"{partition.partition_id} checks artifact",
    )
    if hash_file(checks_path) != manifest.checks_sha256:
        raise LF022MergedInventoryError(f"{partition.partition_id} checks hash mismatch")
    checks: list[LF022LeanCheckRecord] = []
    seen_checks: set[str] = set()
    seen_variants: set[str] = set()
    for line_number, raw in enumerate(checks_path.read_bytes().splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n"):
            raise LF022MergedInventoryError(
                f"{partition.partition_id} checks line lacks final newline: {line_number}"
            )
        try:
            check = LF022LeanCheckRecord.model_validate_json(raw)
        except ValueError as exc:
            raise LF022MergedInventoryError(
                f"invalid {partition.partition_id} check line {line_number}: {exc}"
            ) from exc
        if raw != canonical_json_bytes(check.model_dump(mode="json")) + b"\n":
            raise LF022MergedInventoryError(
                f"noncanonical {partition.partition_id} check line {line_number}"
            )
        if check.check_id in seen_checks or check.variant_id in seen_variants:
            raise LF022MergedInventoryError(
                f"duplicate check or variant in {partition.partition_id}: {check.variant_id}"
            )
        seen_checks.add(check.check_id)
        seen_variants.add(check.variant_id)
        checks.append(check)
    if len(checks) != manifest.record_count:
        raise LF022MergedInventoryError(f"{partition.partition_id} check count differs")
    if hash_canonical([item.variant_id for item in checks]) != manifest.ordered_variant_ids_hash:
        raise LF022MergedInventoryError(f"{partition.partition_id} ordered variant hash differs")
    if dict(sorted(Counter(item.lean_status.value for item in checks).items())) != dict(
        sorted(manifest.status_counts.items())
    ):
        raise LF022MergedInventoryError(f"{partition.partition_id} Lean status counts differ")
    if dict(sorted(Counter(item.outcome for item in checks).items())) != dict(
        sorted(manifest.outcome_counts.items())
    ):
        raise LF022MergedInventoryError(f"{partition.partition_id} outcome counts differ")
    input_projection = [
        {
            "variant_id": item.variant_id,
            "line_sha256": item.source_variant_line_sha256,
            "context_id": item.context_id,
            "import_header_sha256": item.import_header_sha256,
            "project_dir": item.project_dir,
        }
        for item in checks
    ]
    if hash_canonical(input_projection) != manifest.input_set_hash:
        raise LF022MergedInventoryError(f"{partition.partition_id} input-set hash differs")
    return checks, checks_path


def _load_variant(
    check: LF022LeanCheckRecord,
    *,
    source_root: Path,
    artifact_cache: dict[Path, tuple[bytes, str]],
) -> VariantRecord:
    artifact = _resolve_artifact(
        check.source_variant_artifact,
        source_root=source_root,
        label="source variant artifact",
    )
    try:
        artifact.relative_to(source_root)
    except ValueError as exc:
        raise LF022MergedInventoryError(
            f"source variant escapes partition source root: {artifact}"
        ) from exc
    if artifact not in artifact_cache:
        artifact_cache[artifact] = (artifact.read_bytes(), hash_file(artifact))
    raw_artifact, artifact_hash = artifact_cache[artifact]
    if artifact_hash != check.source_variant_artifact_sha256:
        raise LF022MergedInventoryError(f"source variant artifact hash differs: {artifact}")
    lines = raw_artifact.splitlines(keepends=True)
    try:
        raw = lines[check.source_variant_line_number - 1]
    except IndexError as exc:
        raise LF022MergedInventoryError(f"source variant line is missing: {artifact}") from exc
    if not raw.endswith(b"\n") or sha256_hex(raw) != check.source_variant_line_sha256:
        raise LF022MergedInventoryError(f"source variant line binding differs: {artifact}")
    try:
        variant = VariantRecord.model_validate_json(raw)
    except ValueError as exc:
        raise LF022MergedInventoryError(f"invalid source variant: {artifact}: {exc}") from exc
    if raw != canonical_json_bytes(variant.model_dump(mode="json")) + b"\n":
        raise LF022MergedInventoryError(f"source variant is not canonical JSONL: {artifact}")
    if (
        variant.variant_id != check.variant_id
        or variant.candidate_code_hash != check.candidate_code_hash
        or variant.extracted_statement is None
        or variant.context_id != check.context_id
    ):
        raise LF022MergedInventoryError(f"Lean check does not bind source variant: {artifact}")
    return variant


def _load_audit(
    partition: LF022MergedPartitionSpec,
    *,
    source_root: Path,
    checks_path: Path,
    checks: list[LF022LeanCheckRecord],
) -> tuple[dict[str, LF022VerifiedCodexAuditJudgment], str | None]:
    binding = partition.codex_audit_manifest
    if binding is None:
        return {}, None
    manifest_path = _resolve_artifact(
        binding.path,
        source_root=source_root,
        label=f"{partition.partition_id} Codex-audit manifest",
    )
    if hash_file(manifest_path) != binding.sha256:
        raise LF022MergedInventoryError(f"{partition.partition_id} audit manifest hash differs")
    try:
        frozen_manifest = LF022CodexAuditManifest.model_validate_json(manifest_path.read_bytes())
        verified = verify_completed_lf022_codex_audit(
            repo_root=source_root,
            checks_path=checks_path,
            audit_root=manifest_path.parent,
            require_complete_clean=True,
        )
    except (OSError, ValueError, LF022CodexAuditError) as exc:
        raise LF022MergedInventoryError(
            f"{partition.partition_id} audit replay failed: {exc}"
        ) from exc
    if verified.manifest != frozen_manifest or verified.checks != tuple(checks):
        raise LF022MergedInventoryError(f"{partition.partition_id} audit/check replay differs")
    judgments = {item.variant_id: item for item in verified.judgments}
    if len(judgments) != len(verified.judgments):
        raise LF022MergedInventoryError(f"{partition.partition_id} audit repeats a variant")
    valid_ids = {
        item.variant_id
        for item in checks
        if item.outcome in _LEAN_VALID_OUTCOMES and item.declaration_verified
    }
    if set(judgments) != valid_ids:
        raise LF022MergedInventoryError(
            f"{partition.partition_id} completed audit does not cover exact Lean-valid set"
        )
    return judgments, verified.response_artifact_set_sha256


def _diagnostic(judgment: LF022VerifiedCodexAuditJudgment) -> LF022MergedAuditDiagnostic:
    response = judgment.response
    return LF022MergedAuditDiagnostic(
        audit_item_id=judgment.audit_item_id,
        pair_id=judgment.pair_id,
        same_claim_answer=response.same_claim_answer,
        relation=response.relation.value if response.relation is not None else None,
        a_implies_b=response.a_implies_b,
        b_implies_a=response.b_implies_a,
        final_message_sha256=judgment.final_message_sha256,
        parsed_response_sha256=judgment.parsed_response_sha256,
    )


def _observation(
    *,
    partition_id: str,
    check: LF022LeanCheckRecord,
    variant: VariantRecord,
    judgment: LF022VerifiedCodexAuditJudgment | None,
) -> LF022MergedObservation:
    values: dict[str, object] = {
        "schema_version": 1,
        "partition_id": partition_id,
        "check_id": check.check_id,
        "variant_id": variant.variant_id,
        "pair_key": source_candidate_pair_hash(variant),
        "candidate_code_hash": cast(str, variant.candidate_code_hash),
        "source_theorem_ids": variant.source_theorem_ids,
        "proposer_model": variant.generator_id,
        "source_variant_artifact": check.source_variant_artifact,
        "source_variant_artifact_sha256": check.source_variant_artifact_sha256,
        "source_variant_line_number": check.source_variant_line_number,
        "source_variant_line_sha256": check.source_variant_line_sha256,
        "lean_outcome": check.outcome,
        "lean_valid": check.outcome in _LEAN_VALID_OUTCOMES,
        "audit": _diagnostic(judgment) if judgment is not None else None,
        "audit_only": True,
        "inventory_only": True,
        "generation_complete": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "gold_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    draft = LF022MergedObservation.model_construct(
        **cast(
            dict[str, Any],
            {
                **values,
                "observation_id": "lf022_merged_observation:" + "0" * 64,
            },
        )
    )
    return LF022MergedObservation.model_validate(
        {
            **values,
            "observation_id": make_id("lf022_merged_observation", _observation_id_values(draft)),
        }
    )


def _load_partition(partition: LF022MergedPartitionSpec) -> _LoadedPartition:
    source_root = _safe_root(partition.source_root)
    manifest, _manifest_path = _manifest_view(partition, source_root=source_root)
    _verify_selection(partition, source_root=source_root, manifest=manifest)
    checks, checks_path = _load_checks(partition, source_root=source_root, manifest=manifest)
    judgments, response_set_hash = _load_audit(
        partition,
        source_root=source_root,
        checks_path=checks_path,
        checks=checks,
    )
    artifact_cache: dict[Path, tuple[bytes, str]] = {}
    observations: list[LF022MergedObservation] = []
    for check in checks:
        variant = _load_variant(
            check,
            source_root=source_root,
            artifact_cache=artifact_cache,
        )
        observations.append(
            _observation(
                partition_id=partition.partition_id,
                check=check,
                variant=variant,
                judgment=judgments.get(variant.variant_id),
            )
        )
    counts = LF022MergedPartitionExpected(
        gross_observation_count=len(observations),
        lean_valid_observation_count=sum(item.lean_valid for item in observations),
        audited_observation_count=sum(item.audit is not None for item in observations),
    )
    report = LF022MergedPartitionReport(
        partition_id=partition.partition_id,
        selection_kind=partition.selection_kind,
        source_root=str(source_root),
        selection_artifact_sha256=(
            partition.selection_artifact.sha256 if partition.selection_artifact else None
        ),
        lean_check_manifest_sha256=partition.lean_check_manifest.sha256,
        checks_artifact_sha256=manifest.checks_sha256,
        codex_audit_manifest_sha256=(
            partition.codex_audit_manifest.sha256 if partition.codex_audit_manifest else None
        ),
        codex_response_artifact_set_sha256=response_set_hash,
        counts=counts,
    )
    return _LoadedPartition(report=report, observations=tuple(observations))


def _audit_value(value: str | None) -> str:
    return "null" if value is None else value


def _pair_record(
    pair_key: str,
    observations: list[LF022MergedObservation],
) -> LF022MergedPairRecord:
    audits = [item.audit for item in observations if item.audit is not None]
    same_claim = tuple(sorted({item.same_claim_answer for item in audits}))
    relation = tuple(sorted({_audit_value(item.relation) for item in audits}))
    directional = tuple(sorted({f"A={item.a_implies_b},B={item.b_implies_a}" for item in audits}))
    core = tuple(
        sorted(
            {
                f"same={item.same_claim_answer};relation={_audit_value(item.relation)};"
                f"A={item.a_implies_b};B={item.b_implies_a}"
                for item in audits
            }
        )
    )
    lean_outcomes = tuple(sorted({item.lean_outcome for item in observations}))
    values: dict[str, object] = {
        "schema_version": 1,
        "pair_key": pair_key,
        "observation_ids": tuple(sorted(item.observation_id for item in observations)),
        "partition_ids": tuple(sorted({item.partition_id for item in observations})),
        "proposer_models": tuple(sorted({item.proposer_model for item in observations})),
        "variant_ids": tuple(sorted({item.variant_id for item in observations})),
        "candidate_code_hashes": tuple(sorted({item.candidate_code_hash for item in observations})),
        "lean_outcomes": lean_outcomes,
        "lean_outcome_conflict": len(lean_outcomes) > 1,
        "audit_observation_count": len(audits),
        "audit_same_claim_values": same_claim,
        "audit_relation_values": relation,
        "audit_directional_values": directional,
        "audit_core_tuple_values": core,
        "audit_same_claim_conflict": len(same_claim) > 1,
        "audit_relation_conflict": len(relation) > 1,
        "audit_directional_conflict": len(directional) > 1,
        "audit_any_core_tuple_conflict": len(core) > 1,
        "audit_only": True,
        "inventory_only": True,
        "generation_complete": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "gold_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022MergedPairRecord.model_validate(
        {**values, "merged_pair_id": make_id("lf022_merged_pair", values)}
    )


def _counts(observations: tuple[LF022MergedObservation, ...]) -> LF022MergedInventoryCounts:
    valid = tuple(item for item in observations if item.lean_valid)
    audited = tuple(item for item in observations if item.audit is not None)
    valid_pairs = {item.pair_key for item in valid}
    audited_pairs = {item.pair_key for item in audited}
    by_pair: dict[str, list[LF022MergedObservation]] = defaultdict(list)
    for item in observations:
        by_pair[item.pair_key].append(item)
    return LF022MergedInventoryCounts(
        gross_observation_count=len(observations),
        unique_variant_id_count=len({item.variant_id for item in observations}),
        unique_candidate_content_count=len({item.candidate_code_hash for item in observations}),
        unique_pair_key_count=len(by_pair),
        lean_valid_gross_observation_count=len(valid),
        lean_valid_unique_variant_id_count=len({item.variant_id for item in valid}),
        lean_valid_unique_candidate_content_count=len({item.candidate_code_hash for item in valid}),
        lean_valid_unique_pair_key_count=len(valid_pairs),
        audited_gross_observation_count=len(audited),
        audited_unique_variant_id_count=len({item.variant_id for item in audited}),
        audited_unique_candidate_content_count=len({item.candidate_code_hash for item in audited}),
        audited_unique_pair_key_count=len(audited_pairs),
        lean_valid_unaudited_pair_key_count=len(valid_pairs - audited_pairs),
        duplicate_pair_observation_count=len(observations) - len(by_pair),
        cross_partition_pair_key_count=sum(
            len({item.partition_id for item in values}) > 1 for values in by_pair.values()
        ),
        cross_model_pair_key_count=sum(
            len({item.proposer_model for item in values}) > 1 for values in by_pair.values()
        ),
    )


def _conflicts(pairs: tuple[LF022MergedPairRecord, ...]) -> LF022MergedConflictCounts:
    return LF022MergedConflictCounts(
        lean_outcome_conflict_pair_key_count=sum(item.lean_outcome_conflict for item in pairs),
        audit_same_claim_conflict_pair_key_count=sum(
            item.audit_same_claim_conflict for item in pairs
        ),
        audit_relation_conflict_pair_key_count=sum(item.audit_relation_conflict for item in pairs),
        audit_directional_conflict_pair_key_count=sum(
            item.audit_directional_conflict for item in pairs
        ),
        audit_any_core_tuple_conflict_pair_key_count=sum(
            item.audit_any_core_tuple_conflict for item in pairs
        ),
    )


def build_lf022_merged_checked_inventory(
    *,
    spec_path: Path,
    expected_spec_sha256: str,
) -> LF022MergedInventoryBundle:
    """Verify the frozen sources and construct the bounded audit-only inventory."""

    spec, spec_sha256 = _load_spec(spec_path.resolve(), expected_spec_sha256=expected_spec_sha256)
    loaded = tuple(_load_partition(partition) for partition in spec.partitions)
    observed_partition_counts = {item.report.partition_id: item.report.counts for item in loaded}
    if observed_partition_counts != spec.expected.by_partition:
        raise LF022MergedInventoryError("observed partition counts differ from frozen expectation")
    observations = tuple(
        sorted(
            (item for partition in loaded for item in partition.observations),
            key=lambda item: (
                item.pair_key,
                item.partition_id,
                item.variant_id,
                item.check_id,
            ),
        )
    )
    grouped: dict[str, list[LF022MergedObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.pair_key].append(observation)
    pairs = tuple(_pair_record(key, grouped[key]) for key in sorted(grouped))
    counts = _counts(observations)
    conflicts = _conflicts(pairs)
    partition_ids = tuple(item.partition_id for item in spec.partitions)
    keys_by_partition = {
        partition_id: {
            observation.pair_key
            for observation in observations
            if observation.partition_id == partition_id
        }
        for partition_id in partition_ids
    }
    intersections = {
        f"{left} | {right}": len(keys_by_partition[left] & keys_by_partition[right])
        for index, left in enumerate(partition_ids)
        for right in partition_ids[index + 1 :]
    }
    if counts != spec.expected.counts:
        raise LF022MergedInventoryError("merged counts differ from frozen expectation")
    if intersections != spec.expected.pairwise_partition_intersections:
        raise LF022MergedInventoryError("partition intersections differ from frozen expectation")
    if conflicts != spec.expected.conflicts:
        raise LF022MergedInventoryError("conflict counts differ from frozen expectation")
    observation_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in observations
    )
    pair_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in pairs
    )
    report_values: dict[str, object] = {
        "schema_version": 1,
        "method_version": LF022_MERGED_INVENTORY_VERSION,
        "inventory_spec_id": spec.inventory_spec_id,
        "inventory_spec_sha256": spec_sha256,
        "observations_artifact": "observations.jsonl",
        "observations_sha256": sha256_hex(observation_bytes),
        "pairs_artifact": "pairs.jsonl",
        "pairs_sha256": sha256_hex(pair_bytes),
        "partitions": tuple(item.report for item in loaded),
        "counts": counts,
        "pairwise_partition_intersections": intersections,
        "conflicts": conflicts,
        "audit_only": True,
        "inventory_only": True,
        "readiness_claimed": False,
        "generation_complete": False,
        "human_labels_created": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "gold_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    draft_report = LF022MergedInventoryReport.model_construct(
        **cast(
            dict[str, Any],
            {
                **report_values,
                "inventory_id": "lf022_merged_checked_inventory:" + "0" * 64,
            },
        )
    )
    report = LF022MergedInventoryReport.model_validate(
        {
            **report_values,
            "inventory_id": make_id(
                "lf022_merged_checked_inventory", _report_id_values(draft_report)
            ),
        }
    )
    return LF022MergedInventoryBundle(
        observations=observations,
        pairs=pairs,
        report=report,
        observations_bytes=observation_bytes,
        pairs_bytes=pair_bytes,
    )


def _write_immutable(path: Path, payload: bytes) -> bool:
    if path.is_symlink():
        raise LF022MergedInventoryError(f"immutable output cannot be a symlink: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022MergedInventoryError(f"immutable output differs: {path}")
        return True
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022MergedInventoryError(
                    f"concurrent immutable output differs: {path}"
                ) from None
            return True
        return False
    finally:
        temporary.unlink(missing_ok=True)


def write_lf022_merged_checked_inventory(
    bundle: LF022MergedInventoryBundle,
    *,
    output_dir: Path,
) -> LF022MergedInventoryWriteResult:
    """Write the three canonical artifacts, allowing byte-identical replay only."""

    supplied = output_dir.absolute()
    output_dir = _safe_output_directory(supplied)
    expected_names = {"observations.jsonl", "pairs.jsonl", "manifest.json"}
    extras = sorted(path.name for path in output_dir.iterdir() if path.name not in expected_names)
    if extras:
        raise LF022MergedInventoryError(
            "merged inventory output directory contains unexpected artifacts: " + ", ".join(extras)
        )
    report_bytes = canonical_json_bytes(bundle.report.model_dump(mode="json")) + b"\n"
    observations_path = output_dir / bundle.report.observations_artifact
    pairs_path = output_dir / bundle.report.pairs_artifact
    report_path = output_dir / "manifest.json"
    replayed = _write_immutable(observations_path, bundle.observations_bytes)
    replayed = _write_immutable(pairs_path, bundle.pairs_bytes) and replayed
    replayed = _write_immutable(report_path, report_bytes) and replayed
    if (
        hash_file(observations_path) != bundle.report.observations_sha256
        or hash_file(pairs_path) != bundle.report.pairs_sha256
    ):
        raise LF022MergedInventoryError("written inventory artifact hash differs from report")
    return LF022MergedInventoryWriteResult(
        output_dir=output_dir,
        observations_path=observations_path,
        pairs_path=pairs_path,
        report_path=report_path,
        report=bundle.report,
        replayed=replayed,
    )


__all__ = [
    "LF022MergedConflictCounts",
    "LF022MergedInventoryBundle",
    "LF022MergedInventoryCounts",
    "LF022MergedInventoryError",
    "LF022MergedInventoryReport",
    "LF022MergedInventorySpec",
    "LF022MergedInventoryWriteResult",
    "LF022MergedObservation",
    "LF022MergedPairRecord",
    "build_lf022_merged_checked_inventory",
    "write_lf022_merged_checked_inventory",
]
