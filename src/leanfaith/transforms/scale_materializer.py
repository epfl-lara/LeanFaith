"""Resumable scientific-scale materialization for deterministic v1 transforms.

The LF-017/LF-018 rule implementations deliberately separate generation
intention from semantic truth.  This module scales those already-guarded
runtimes over an immutable theorem/representation inventory without weakening
that boundary:

* every source is processed in a deterministic order under a hash-bound spec;
* each source produces one immutable, atomic journal shard;
* resume accepts only a contiguous prefix of byte-valid shards;
* accepted candidates re-elaborate and receive ``repr_v3`` views;
* all variants remain provisional and all pairs remain unresolved;
* type correctness never becomes a negative semantic label.

Journal shards are the append-only source of truth.  Canonical JSONL
partitions are deterministic projections written only after all selected
sources reach terminal outcomes.
"""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import Field, TypeAdapter, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets import ActiveBenchmarkRegistry, load_active_benchmark_registry
from leanfaith.lean.extraction import EXTRACTION_SCHEMA_VERSION
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.project_registry import load_environment_lock
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.lean.session_policy import RetryPolicy, run_with_retries
from leanfaith.representations import (
    NORMALIZATION_VERSION,
    TheoremForRepresentation,
    build_representations,
    signature_near_dup_hash,
)
from leanfaith.representations.views import representation_content_hash
from leanfaith.schemas.enums import (
    ArtifactClass,
    DataStage,
    Polarity,
    QualityTier,
    ValidationStatus,
    ViewStatus,
)
from leanfaith.schemas.ids import REPRESENTATION_PREFIX, THEOREM_PREFIX, make_id
from leanfaith.schemas.manifest import CodeState, OutputManifest, collect_code_state
from leanfaith.schemas.pair import PairRecord, check_pair_groups
from leanfaith.schemas.source import make_source_ancestry_id
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import (
    TransformationAttempt,
    TransformationAudit,
    VariantDraft,
    VariantRecord,
    check_deterministic_variant_lineage,
)
from leanfaith.transforms.factory import build_positive_rule_runtime
from leanfaith.transforms.materialize import (
    build_derived_theorem_record,
    build_deterministic_pair_record,
)
from leanfaith.transforms.negative_factory import build_negative_rule_runtime
from leanfaith.transforms.negatives.n10_nearby_theorem import nearby_theorem_bucket_keys
from leanfaith.transforms.pair_runtime import (
    PairTransformationDispatchError,
    audit_pair_transformation,
    execute_pair_transformation,
)
from leanfaith.transforms.protocol import (
    build_deterministic_variant_record,
    verify_deterministic_variant_id,
    verify_transformation_attempt_id,
    verify_transformation_audit_id,
    verify_variant_draft_id,
)
from leanfaith.transforms.registry import (
    LoadedTransformationRegistry,
    TransformationExecution,
    TransformationExecutionFailed,
    load_transformation_registry,
)

_HEX64_PATTERN = r"^[0-9a-f]{64}$"
_VALID_SOURCE_STATUSES = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
_ACTIVE_V1_RULES = (
    "p01_alpha",
    "p02_binders",
    "p04_notation_lite",
    "n01_operator",
    "n02_quantifier",
    "n03_drop_hypothesis",
    "n07_literal_bound",
    "n10_nearby_theorem",
)
_RULE_POLARITY: Mapping[str, Polarity] = {
    "p01_alpha": Polarity.POSITIVE,
    "p02_binders": Polarity.POSITIVE,
    "p04_notation_lite": Polarity.POSITIVE,
    "n01_operator": Polarity.NEGATIVE,
    "n02_quantifier": Polarity.NEGATIVE,
    "n03_drop_hypothesis": Polarity.NEGATIVE,
    "n07_literal_bound": Polarity.NEGATIVE,
    "n10_nearby_theorem": Polarity.NEGATIVE,
}
_REQUIRED_CANDIDATE_VIEWS = (
    "raw_proof_stripped",
    "headless",
    "signature_pp",
    "signature_explicit",
    "semantic_atoms",
    "operator_tree",
)
_DEFAULT_CONFIG = Path("configs/transformations/deterministic_scale_v1.yaml")

ScaleSourceStatus = Literal["eligible", "ineligible"]
ScaleRuleStatus = Literal[
    "accepted",
    "not_applicable",
    "no_output",
    "dispatch_failed",
    "candidate_invalid",
    "candidate_infrastructure_error",
    "candidate_representation_failed",
    "audit_quarantined",
    "protected_benchmark_overlap",
    "duplicate_candidate",
    "cap_skipped",
    "no_donor",
]
ScaleDraftStatus = Literal[
    "accepted",
    "candidate_invalid",
    "candidate_infrastructure_error",
    "candidate_representation_failed",
    "audit_quarantined",
    "protected_benchmark_overlap",
    "duplicate_candidate",
    "cap_skipped",
]
ScaleQuarantineStatus = Literal[
    "candidate_invalid",
    "candidate_infrastructure_error",
    "candidate_representation_failed",
    "audit_quarantined",
    "protected_benchmark_overlap",
    "duplicate_candidate",
    "cap_skipped",
]


class DeterministicScaleError(RuntimeError):
    """A scale run cannot proceed or resume without violating provenance."""


class _CandidateValidationFailure(DeterministicScaleError):
    """One terminal Lean validation outcome with its exact request identity."""

    def __init__(
        self,
        *,
        status: LeanStatus,
        request_hash: str,
        diagnostics: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"candidate validation ended as {status.value}; request={request_hash}; "
            f"diagnostics={diagnostics[:3]}"
        )
        self.status = status
        self.request_hash = request_hash
        self.diagnostics = diagnostics


class DeterministicScaleConfig(StrictModel):
    """Versioned execution and admission policy for deterministic generation."""

    schema_version: Literal[1] = 1
    profile_id: Literal["deterministic_scale_v1"] = "deterministic_scale_v1"
    profile_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    base_seed: int = Field(strict=True)
    active_rule_ids: tuple[str, ...]
    normalization_version: Literal["repr_v3"] = "repr_v3"
    required_source_views: tuple[str, ...]
    max_accepted_variants_per_root_ancestry: int = Field(ge=1)
    max_accepted_variants_per_family_per_root_ancestry: int = Field(ge=1)
    max_accepted_variants_per_family: int | None = Field(default=None, ge=1)
    max_n10_donor_attempts_per_primary: int = Field(ge=1)
    candidate_timeout_seconds: float = Field(gt=0)
    record_timestamp_utc: datetime.datetime
    reject_protected_benchmark_overlap: Literal[True] = True
    negatives_remain_provisional: Literal[True] = True
    positives_require_clean_mechanical_audit: Literal[True] = True
    failed_proof_search_is_negative_evidence: Literal[False] = False

    @model_validator(mode="after")
    def _closed_policy(self) -> DeterministicScaleConfig:
        if self.record_timestamp_utc.tzinfo is None or (
            self.record_timestamp_utc.utcoffset() != datetime.timedelta(0)
        ):
            raise ValueError("record_timestamp_utc must be timezone-aware UTC")
        if self.active_rule_ids != tuple(sorted(set(self.active_rule_ids))):
            raise ValueError("active_rule_ids must be sorted and unique")
        unknown = sorted(set(self.active_rule_ids) - set(_ACTIVE_V1_RULES))
        if unknown:
            raise ValueError(f"deterministic scale profile names unknown v1 rules: {unknown}")
        if not self.active_rule_ids:
            raise ValueError("at least one deterministic v1 rule must be active")
        if self.required_source_views != tuple(sorted(set(self.required_source_views))):
            raise ValueError("required_source_views must be sorted and unique")
        required = {
            "raw_proof_stripped",
            "headless",
            "signature_explicit",
            "semantic_atoms",
            "operator_tree",
        }
        if not required.issubset(self.required_source_views):
            raise ValueError(
                "required_source_views must include raw/headless/explicit/atoms/operator_tree"
            )
        return self


class ScaleFailure(StrictModel):
    """One structured operational or policy failure; never a semantic label."""

    schema_version: Literal[1] = 1
    stage: str = Field(min_length=1)
    code: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    source_theorem_ids: tuple[str, ...]
    rule_id: str | None = None
    draft_id: str | None = None
    lean_request_hash: str | None = Field(default=None, pattern=_HEX64_PATTERN)


class ScaleDraftResult(StrictModel):
    """Terminal materialization state for one generated draft."""

    schema_version: Literal[1] = 1
    status: ScaleDraftStatus
    draft: VariantDraft | None = None
    redacted_draft_id: str | None = None
    redacted_candidate_code_hash: str | None = Field(
        default=None,
        pattern=_HEX64_PATTERN,
    )
    candidate_content_redacted: bool = False
    candidate_theorem: TheoremRecord | None = None
    candidate_representation: RepresentationRecord | None = None
    audit: TransformationAudit | None = None
    variant: VariantRecord | None = None
    pair: PairRecord | None = None
    failure: ScaleFailure | None = None

    @model_validator(mode="after")
    def _coherent(self) -> ScaleDraftResult:
        if self.status == "accepted":
            if self.draft is None:
                raise ValueError("accepted draft result requires the full draft")
            if any(
                item is None
                for item in (
                    self.candidate_theorem,
                    self.candidate_representation,
                    self.audit,
                    self.variant,
                    self.pair,
                )
            ):
                raise ValueError("accepted draft result requires complete materialized lineage")
            if self.failure is not None:
                raise ValueError("accepted draft result cannot carry a failure")
            assert self.variant is not None and self.pair is not None
            if self.variant.quality_tier != QualityTier.PROVISIONAL:
                raise ValueError("scale variants must remain provisional")
            if self.pair.resolved_label_id is not None:
                raise ValueError("scale pairs must remain semantically unresolved")
            if (
                self.redacted_draft_id is not None
                or self.redacted_candidate_code_hash is not None
                or self.candidate_content_redacted
            ):
                raise ValueError("accepted draft result cannot use redacted identity fields")
        else:
            if self.failure is None:
                raise ValueError("non-accepted draft result requires a structured failure")
            if self.status == "protected_benchmark_overlap":
                if (
                    self.draft is not None
                    or self.redacted_draft_id is None
                    or self.redacted_candidate_code_hash is None
                    or not self.candidate_content_redacted
                ):
                    raise ValueError("protected overlap must persist only redacted draft identity")
                if any(
                    item is not None
                    for item in (
                        self.candidate_theorem,
                        self.candidate_representation,
                        self.audit,
                        self.variant,
                        self.pair,
                    )
                ):
                    raise ValueError("protected overlap cannot persist candidate-bearing lineage")
            else:
                if self.draft is None:
                    raise ValueError("non-protected draft result requires its draft")
                if (
                    self.redacted_draft_id is not None
                    or self.redacted_candidate_code_hash is not None
                    or self.candidate_content_redacted
                ):
                    raise ValueError("only protected overlap may redact draft content")
                if any(
                    item is not None
                    for item in (
                        self.candidate_theorem,
                        self.candidate_representation,
                        self.audit,
                        self.variant,
                        self.pair,
                    )
                ):
                    raise ValueError("non-accepted result cannot persist candidate-bearing lineage")
        return self

    @property
    def persistent_draft_id(self) -> str:
        if self.draft is not None:
            return self.draft.draft_id
        assert self.redacted_draft_id is not None
        return self.redacted_draft_id

    @property
    def persistent_candidate_code_hash(self) -> str:
        if self.draft is not None:
            return self.draft.candidate_code_hash
        assert self.redacted_candidate_code_hash is not None
        return self.redacted_candidate_code_hash


class ScaleQuarantineRecord(StrictModel):
    """Non-training projection of one rejected draft, with no candidate text."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_scale_quarantine"] = "deterministic_scale_quarantine"
    status: ScaleQuarantineStatus
    source_theorem_ids: tuple[str, ...]
    rule_id: str
    family_id: str
    polarity: Polarity
    draft_id: str
    candidate_code_hash: str = Field(pattern=_HEX64_PATTERN)
    failure: ScaleFailure
    candidate_content_redacted: bool
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    semantic_label_created: Literal[False] = False


class ScaleRuleResult(StrictModel):
    """One unary execution or one primary/donor N10 execution."""

    schema_version: Literal[1] = 1
    status: ScaleRuleStatus
    rule_id: str
    family_id: str
    polarity: Polarity
    seed: int
    source_theorem_ids: tuple[str, ...]
    donor_theorem_id: str | None = None
    attempt: TransformationAttempt | None = None
    draft_results: tuple[ScaleDraftResult, ...] = ()
    failure: ScaleFailure | None = None

    @model_validator(mode="after")
    def _coherent(self) -> ScaleRuleResult:
        if self.status == "accepted":
            if not self.draft_results or not any(
                result.status == "accepted" for result in self.draft_results
            ):
                raise ValueError("accepted rule result requires an accepted draft")
            if self.failure is not None:
                raise ValueError("accepted rule result cannot carry a rule-level failure")
        elif self.status in {"not_applicable", "no_output"}:
            if self.attempt is None:
                raise ValueError(f"{self.status} result requires a terminal attempt")
            if self.failure is not None or self.draft_results:
                raise ValueError(f"{self.status} result cannot carry drafts/failure")
        elif self.status == "no_donor":
            if self.attempt is not None or self.failure is not None or self.draft_results:
                raise ValueError("no_donor is a clean scheduling outcome")
        elif self.failure is None and not self.draft_results:
            raise ValueError("failed/skipped rule result requires failure or draft results")
        return self


class ScaleSourceShard(StrictModel):
    """Atomic append-only journal shard for one selected source theorem."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_scale_source_shard"] = "deterministic_scale_source_shard"
    run_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    source_index: int = Field(ge=0)
    source_theorem_id: str
    source_representation_id: str | None
    source_status: ScaleSourceStatus
    source_failure: ScaleFailure | None = None
    rule_results: tuple[ScaleRuleResult, ...] = ()

    @model_validator(mode="after")
    def _terminal(self) -> ScaleSourceShard:
        if self.source_status == "eligible" and self.source_failure is not None:
            raise ValueError("eligible source cannot carry source_failure")
        if self.source_status == "ineligible" and (
            self.source_failure is None or self.rule_results
        ):
            raise ValueError("ineligible source requires one failure and no rule results")
        return self


class ScaleJournalReceipt(StrictModel):
    """Hash-chain receipt for one immutable source journal shard."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_scale_journal_receipt"] = (
        "deterministic_scale_journal_receipt"
    )
    run_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    source_index: int = Field(ge=0)
    source_theorem_id: str
    shard_filename: str = Field(min_length=1)
    shard_sha256: str = Field(pattern=_HEX64_PATTERN)
    previous_receipt_hash: str = Field(pattern=_HEX64_PATTERN)
    receipt_hash: str = Field(pattern=_HEX64_PATTERN)

    @model_validator(mode="after")
    def _self_authenticating(self) -> ScaleJournalReceipt:
        payload = self.model_dump(mode="json")
        payload.pop("receipt_hash")
        if self.receipt_hash != hash_canonical(payload):
            raise ValueError("journal receipt hash does not match its canonical payload")
        return self


class ScaleLeanReplayAudit(StrictModel):
    """Self-hashed accounting record for one completed exact replay.

    This record is deliberately *not* an authentication or verification
    primitive: anyone able to rewrite a producer shard can also rewrite this
    payload and its hash.  Scientific merge therefore reruns the exact
    Lean-backed materializer itself against the pinned project and context.
    """

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_scale_replay_audit"] = "deterministic_scale_replay_audit"
    audit_hash: str = Field(pattern=_HEX64_PATTERN)
    run_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    run_spec_sha256: str = Field(pattern=_HEX64_PATTERN)
    replay_mode: Literal["exact_lean_backed_replay"] = "exact_lean_backed_replay"
    replayed_source_count: int = Field(ge=1)
    replayed_source_ids_sha256: str = Field(pattern=_HEX64_PATTERN)
    journal_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    partition_sha256: dict[str, str]
    replay_completed: Literal[True] = True
    authentication_strength: Literal["self_hash_only"] = "self_hash_only"
    created_at: datetime.datetime

    @model_validator(mode="after")
    def _self_consistent(self) -> ScaleLeanReplayAudit:
        payload = self.model_dump(mode="json")
        payload.pop("audit_hash")
        if self.audit_hash != hash_canonical(payload):
            raise ValueError("Lean-replay audit hash does not match canonical payload")
        return self


class ScaleInventoryPartitionBinding(StrictModel):
    """Authoritative content binding for one immutable source partition."""

    path: str
    sha256: str = Field(pattern=_HEX64_PATTERN)
    record_count: int = Field(ge=1)


class ScaleUpstreamManifestBinding(StrictModel):
    """Exact trusted producer/selection manifest used to authorize a partition."""

    path: str
    sha256: str = Field(pattern=_HEX64_PATTERN)
    manifest_kind: Literal["output_manifest", "gate3_selection_v2"]


class _Gate3SelectionRecord(StrictModel):
    source: str
    theorem_id: str
    context_id: str
    statement_content_hash: str = Field(pattern=_HEX64_PATTERN)


class _Gate3SelectionAccounting(StrictModel):
    input_records: int = Field(ge=0)
    eligible_records: int = Field(ge=0)
    selected_records: int = Field(ge=0)


class _Gate3SelectionManifest(StrictModel):
    """Canonical Gate-3 selection manifest emitted by ``freeze-gate3-inputs``."""

    schema_version: Literal[2] = 2
    selection_version: Literal["gate3_equal_source_hash_order_v1"]
    per_source: int = Field(ge=1)
    record_count: int = Field(ge=1)
    source_counts: dict[str, int]
    context_id: str
    input_accounting: dict[str, _Gate3SelectionAccounting]
    theorem_partition: str
    theorem_partition_sha256: str = Field(pattern=_HEX64_PATTERN)
    input_checksums: dict[str, str]
    records: tuple[_Gate3SelectionRecord, ...]

    @model_validator(mode="after")
    def _reconcile(self) -> _Gate3SelectionManifest:
        if len(self.records) != self.record_count:
            raise ValueError("Gate-3 selection record_count does not match records")
        observed = Counter(record.source for record in self.records)
        if dict(sorted(observed.items())) != dict(sorted(self.source_counts.items())):
            raise ValueError("Gate-3 selection source_counts do not match records")
        if sum(self.source_counts.values()) != self.record_count:
            raise ValueError("Gate-3 selection source_counts do not reconcile")
        if any(count != self.per_source for count in self.source_counts.values()):
            raise ValueError("Gate-3 selection is not equal-sized by source")
        for source, accounting in self.input_accounting.items():
            if accounting.selected_records != self.source_counts.get(source):
                raise ValueError("Gate-3 selection accounting does not match source_counts")
        for label, digest in self.input_checksums.items():
            if (
                not label
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("Gate-3 selection input_checksums are malformed")
        if any(record.context_id != self.context_id for record in self.records):
            raise ValueError("Gate-3 selection records contain mixed contexts")
        return self


class ScaleSourceInventoryManifest(StrictModel):
    """Independent trust anchor for the extracted theorem/repr_v3 inventory."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_scale_source_inventory_manifest"] = (
        "deterministic_scale_source_inventory_manifest"
    )
    extraction_schema_version: Literal["extract_v2"] = "extract_v2"
    normalization_version: Literal["repr_v3"] = "repr_v3"
    context_id: str
    theorem_partition: ScaleInventoryPartitionBinding
    representation_partition: ScaleInventoryPartitionBinding
    theorem_upstream_manifest: ScaleUpstreamManifestBinding
    representation_upstream_manifest: ScaleUpstreamManifestBinding


class DeterministicScaleRunSpec(StrictModel):
    """Immutable semantic identity of a resumable scale run."""

    schema_version: Literal[2] = 2
    artifact_kind: Literal["deterministic_scale_run_spec"] = "deterministic_scale_run_spec"
    run_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    shard_set_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    theorem_input_path: str
    theorem_input_sha256: str = Field(pattern=_HEX64_PATTERN)
    representation_input_path: str
    representation_input_sha256: str = Field(pattern=_HEX64_PATTERN)
    source_inventory_manifest_path: str
    source_inventory_manifest_sha256: str = Field(pattern=_HEX64_PATTERN)
    theorem_upstream_manifest_path: str
    theorem_upstream_manifest_sha256: str = Field(pattern=_HEX64_PATTERN)
    representation_upstream_manifest_path: str
    representation_upstream_manifest_sha256: str = Field(pattern=_HEX64_PATTERN)
    config_path: str
    config_hash: str = Field(pattern=_HEX64_PATTERN)
    registry_hash: str = Field(pattern=_HEX64_PATTERN)
    benchmark_manifest_path: str
    benchmark_manifest_sha256: str = Field(pattern=_HEX64_PATTERN)
    context_id: str
    context_record_sha256: str = Field(pattern=_HEX64_PATTERN)
    project_dir: str
    project_revision: str
    project_tree_hash: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    code: CodeState
    shard_assignment_scheme: Literal["root_component_greedy_v1"] = "root_component_greedy_v1"
    shard_count: int = Field(ge=1)
    shard_index: int = Field(ge=0)
    source_universe_theorem_ids: tuple[str, ...]
    source_shard_assignments: tuple[int, ...]
    selected_source_theorem_ids: tuple[str, ...]
    max_sources: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _shard_assignment_is_closed(self) -> DeterministicScaleRunSpec:
        if self.shard_index >= self.shard_count:
            raise ValueError("shard_index must be smaller than shard_count")
        if len(self.source_universe_theorem_ids) != len(self.source_shard_assignments):
            raise ValueError("source universe and shard assignments differ in length")
        if not self.source_universe_theorem_ids:
            raise ValueError("source universe cannot be empty")
        if len(set(self.source_universe_theorem_ids)) != len(self.source_universe_theorem_ids):
            raise ValueError("source universe contains duplicate theorem IDs")
        if any(
            assignment < 0 or assignment >= self.shard_count
            for assignment in self.source_shard_assignments
        ):
            raise ValueError("source shard assignment is out of range")
        expected = tuple(
            theorem_id
            for theorem_id, assignment in zip(
                self.source_universe_theorem_ids,
                self.source_shard_assignments,
                strict=True,
            )
            if assignment == self.shard_index
        )
        if self.selected_source_theorem_ids != expected:
            raise ValueError("selected sources do not match the bound shard assignment")
        return self


class DeterministicScaleManifest(StrictModel):
    """Final machine-readable accounting and partition binding."""

    schema_version: Literal[2] = 2
    artifact_kind: Literal["deterministic_scale_manifest"] = "deterministic_scale_manifest"
    run_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    run_spec_sha256: str = Field(pattern=_HEX64_PATTERN)
    shard_set_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    shard_count: int = Field(ge=1)
    shard_index: int = Field(ge=0)
    source_universe_count: int = Field(ge=1)
    source_assignment_sha256: str = Field(pattern=_HEX64_PATTERN)
    source_count: int = Field(ge=0)
    eligible_source_count: int = Field(ge=0)
    ineligible_source_count: int = Field(ge=0)
    journal_shard_count: int = Field(ge=0)
    rule_status_counts: dict[str, int]
    family_accepted_counts: dict[str, int]
    record_counts: dict[str, int]
    partition_sha256: dict[str, str]
    journal_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    journal_receipt_count: int = Field(ge=0)
    journal_receipt_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    journal_chain_tip: str = Field(pattern=_HEX64_PATTERN)
    raw_response_file_count: int = Field(ge=0)
    raw_response_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    resolved_semantic_labels: Literal[0] = 0
    promoted_items: Literal[0] = 0
    output_quality_tier: Literal["provisional"] = "provisional"
    created_at: datetime.datetime

    @model_validator(mode="after")
    def _reconciles(self) -> DeterministicScaleManifest:
        if self.shard_index >= self.shard_count:
            raise ValueError("manifest shard_index must be smaller than shard_count")
        if self.source_count != self.eligible_source_count + self.ineligible_source_count:
            raise ValueError("manifest eligible/ineligible source counts do not reconcile")
        if self.journal_shard_count != self.source_count:
            raise ValueError("manifest journal shard count differs from source_count")
        if self.journal_receipt_count != self.source_count:
            raise ValueError("manifest journal receipt count differs from source_count")
        return self


@dataclass(frozen=True, slots=True)
class DeterministicScaleArtifacts:
    output_dir: Path
    run_spec_path: Path
    manifest_path: Path
    manifest_sha256: str
    partition_paths: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class ScaleSourceInventoryArtifacts:
    manifest_path: Path
    manifest_sha256: str
    theorem_count: int
    representation_count: int


@dataclass(slots=True)
class _AdmissionState:
    root_counts: Counter[str]
    family_root_counts: Counter[tuple[str, str]]
    family_counts: Counter[str]
    candidate_keys: set[tuple[tuple[str, ...], str]]
    variant_ids: set[str]
    pair_ids: set[str]


@dataclass(frozen=True, slots=True)
class _CandidateValidation:
    status: ValidationStatus
    diagnostics: tuple[str, ...]
    request_hash: str


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(text: str) -> float:
    raise ValueError(f"non-finite JSON constant {text!r}")


def _load_jsonl[RecordT: StrictModel](
    path: Path,
    model: type[RecordT],
    *,
    wrapper_key: str | None = None,
) -> tuple[RecordT, ...]:
    records: list[RecordT] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise DeterministicScaleError(f"cannot read JSONL input {path}: {exc}") from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise DeterministicScaleError(f"{path}:{line_number}: blank JSONL line")
            try:
                raw = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_nonfinite,
                )
                if wrapper_key is not None and isinstance(raw, dict) and wrapper_key in raw:
                    raw = raw[wrapper_key]
                records.append(model.model_validate(raw))
            except Exception as exc:
                raise DeterministicScaleError(
                    f"{path}:{line_number}: invalid {model.__name__}: {exc}"
                ) from exc
    return tuple(records)


def _load_json_document(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except Exception as exc:
        raise DeterministicScaleError(f"invalid JSON manifest {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DeterministicScaleError(f"manifest {path} must contain one JSON object")
    return raw


def _resolve_manifest_binding_path(
    *,
    inventory_manifest_path: Path,
    binding: ScaleUpstreamManifestBinding,
) -> Path:
    raw = Path(binding.path)
    candidate = raw if raw.is_absolute() else inventory_manifest_path.parent / raw
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DeterministicScaleError(
            f"trusted upstream manifest is unavailable: {binding.path}"
        ) from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise DeterministicScaleError(
            f"trusted upstream manifest is not a regular file: {resolved}"
        )
    if hash_file(resolved) != binding.sha256:
        raise DeterministicScaleError(f"trusted upstream manifest hash changed: {resolved}")
    return resolved


def _path_checksum(
    checksums: Mapping[str, str],
    *,
    supplied_path: Path,
    repo_root: Path,
) -> str | None:
    """Find one exact path binding written as absolute or repository-relative."""

    resolved = supplied_path.resolve(strict=True)
    candidates = {str(resolved), str(supplied_path)}
    if resolved.is_relative_to(repo_root.resolve()):
        candidates.add(str(resolved.relative_to(repo_root.resolve())))
    observed = {checksums[key] for key in candidates if key in checksums}
    if not observed:
        return None
    if len(observed) != 1:
        raise DeterministicScaleError(f"manifest contains conflicting checksums for {resolved}")
    return next(iter(observed))


def _load_trusted_theorem_manifest(
    path: Path,
    *,
    expected_kind: str,
    theorem_path: Path,
    theorems: Sequence[TheoremRecord],
    repo_root: Path,
) -> None:
    raw = _load_json_document(path)
    if expected_kind == "gate3_selection_v2":
        try:
            gate3_manifest = _Gate3SelectionManifest.model_validate(raw)
        except Exception as exc:
            raise DeterministicScaleError(
                f"invalid canonical Gate-3 selection manifest {path}: {exc}"
            ) from exc
        declared = Path(gate3_manifest.theorem_partition)
        declared_candidates = {
            declared.resolve() if declared.is_absolute() else (path.parent / declared).resolve(),
            (
                (repo_root.resolve() / declared).resolve()
                if not declared.is_absolute()
                else declared.resolve()
            ),
        }
        if theorem_path.resolve(strict=True) not in declared_candidates:
            raise DeterministicScaleError(
                "Gate-3 selection manifest does not name the supplied theorem partition"
            )
        if (
            hash_file(theorem_path) != gate3_manifest.theorem_partition_sha256
            or len(theorems) != gate3_manifest.record_count
        ):
            raise DeterministicScaleError(
                "Gate-3 selection manifest does not bind the exact theorem partition"
            )
        summaries = tuple(
            _Gate3SelectionRecord(
                source=theorem.source,
                theorem_id=theorem.theorem_id,
                context_id=theorem.context_id,
                statement_content_hash=theorem.statement_content_hash,
            )
            for theorem in theorems
        )
        if summaries != gate3_manifest.records:
            raise DeterministicScaleError(
                "Gate-3 theorem partition records differ from the frozen selection"
            )
        return
    if expected_kind != "output_manifest":
        raise DeterministicScaleError(
            f"unsupported theorem upstream manifest kind {expected_kind!r}"
        )
    try:
        output_manifest = OutputManifest.model_validate(raw)
    except Exception as exc:
        raise DeterministicScaleError(f"invalid extraction OutputManifest {path}: {exc}") from exc
    theorem_sha = hash_file(theorem_path)
    if (
        output_manifest.stage is not DataStage.ELABORATED
        or output_manifest.artifact_class is not ArtifactClass.PRODUCTION
        or output_manifest.row_count != len(theorems)
        or _path_checksum(
            output_manifest.output_partition_checksums,
            supplied_path=theorem_path,
            repo_root=repo_root,
        )
        != theorem_sha
        or _path_checksum(
            output_manifest.file_checksums,
            supplied_path=theorem_path,
            repo_root=repo_root,
        )
        != theorem_sha
    ):
        raise DeterministicScaleError(
            "extraction OutputManifest does not bind the exact theorem partition/count"
        )
    if any(
        theorem.source != output_manifest.source
        or theorem.source_revision != output_manifest.source_revision
        for theorem in theorems
    ):
        raise DeterministicScaleError(
            "theorem records differ from extraction OutputManifest source/revision"
        )


def _load_trusted_representation_manifest(
    path: Path,
    *,
    expected_kind: str,
    theorem_path: Path,
    representation_path: Path,
    theorem_count: int,
    representation_count: int,
    context_id: str,
    repo_root: Path,
) -> None:
    if expected_kind != "output_manifest":
        raise DeterministicScaleError(
            "representation provenance must be a canonical OutputManifest"
        )
    raw = _load_json_document(path)
    try:
        manifest = OutputManifest.model_validate(raw)
    except Exception as exc:
        raise DeterministicScaleError(
            f"invalid representation OutputManifest {path}: {exc}"
        ) from exc
    representation_sha = hash_file(representation_path)
    theorem_sha = hash_file(theorem_path)
    if (
        manifest.stage is not DataStage.REPRESENTED
        or manifest.artifact_class is not ArtifactClass.PRODUCTION
        or manifest.row_count != representation_count
        or manifest.attempted_row_count != theorem_count
        or manifest.context_hash != hash_canonical({"context_id": context_id})
        or _path_checksum(
            manifest.output_partition_checksums,
            supplied_path=representation_path,
            repo_root=repo_root,
        )
        != representation_sha
        or _path_checksum(
            manifest.file_checksums,
            supplied_path=representation_path,
            repo_root=repo_root,
        )
        != representation_sha
        or _path_checksum(
            manifest.input_partition_checksums,
            supplied_path=theorem_path,
            repo_root=repo_root,
        )
        != theorem_sha
    ):
        raise DeterministicScaleError(
            "representation OutputManifest does not bind the exact representation "
            "partition and theorem input"
        )


def _validate_trusted_upstream_manifests(
    inventory: ScaleSourceInventoryManifest,
    *,
    inventory_manifest_path: Path,
    theorem_path: Path,
    representation_path: Path,
    theorems: Sequence[TheoremRecord],
    representations: Sequence[RepresentationRecord],
    repo_root: Path,
) -> tuple[Path, Path]:
    theorem_manifest_path = _resolve_manifest_binding_path(
        inventory_manifest_path=inventory_manifest_path,
        binding=inventory.theorem_upstream_manifest,
    )
    representation_manifest_path = _resolve_manifest_binding_path(
        inventory_manifest_path=inventory_manifest_path,
        binding=inventory.representation_upstream_manifest,
    )
    _load_trusted_theorem_manifest(
        theorem_manifest_path,
        expected_kind=inventory.theorem_upstream_manifest.manifest_kind,
        theorem_path=theorem_path,
        theorems=theorems,
        repo_root=repo_root,
    )
    _load_trusted_representation_manifest(
        representation_manifest_path,
        expected_kind=inventory.representation_upstream_manifest.manifest_kind,
        theorem_path=theorem_path,
        representation_path=representation_path,
        theorem_count=len(theorems),
        representation_count=len(representations),
        context_id=inventory.context_id,
        repo_root=repo_root,
    )
    return theorem_manifest_path, representation_manifest_path


def _load_source_inventory_manifest(
    manifest_path: Path,
    *,
    theorem_path: Path,
    representation_path: Path,
) -> ScaleSourceInventoryManifest:
    """Verify the independent inventory trust anchor before parsing/selecting rows."""

    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        manifest = ScaleSourceInventoryManifest.model_validate(raw)
    except Exception as exc:
        raise DeterministicScaleError(
            f"invalid source inventory manifest {manifest_path}: {exc}"
        ) from exc
    bindings = (
        ("theorem", manifest.theorem_partition, theorem_path),
        ("representation", manifest.representation_partition, representation_path),
    )
    for label, binding, supplied_path in bindings:
        raw_bound = Path(binding.path)
        bound_path = raw_bound if raw_bound.is_absolute() else manifest_path.parent / raw_bound
        try:
            resolved_bound = bound_path.resolve(strict=True)
            resolved_supplied = supplied_path.resolve(strict=True)
        except OSError as exc:
            raise DeterministicScaleError(f"{label} inventory partition is unavailable") from exc
        if resolved_bound != resolved_supplied:
            raise DeterministicScaleError(
                f"{label} CLI partition differs from the authoritative inventory manifest"
            )
        if hash_file(resolved_bound) != binding.sha256:
            raise DeterministicScaleError(
                f"{label} partition hash differs from the authoritative inventory manifest"
            )
    return manifest


def _validate_inventory_record_bindings(
    manifest: ScaleSourceInventoryManifest,
    *,
    theorems: Sequence[TheoremRecord],
    representations: Sequence[RepresentationRecord],
) -> None:
    if len(theorems) != manifest.theorem_partition.record_count:
        raise DeterministicScaleError(
            "theorem partition count differs from the authoritative inventory manifest"
        )
    if len(representations) != manifest.representation_partition.record_count:
        raise DeterministicScaleError(
            "representation partition count differs from the authoritative inventory manifest"
        )
    contexts = {
        *(theorem.context_id for theorem in theorems),
        *(representation.context_id for representation in representations),
    }
    if contexts != {manifest.context_id}:
        raise DeterministicScaleError(
            "inventory record contexts differ from the authoritative inventory manifest"
        )


def _canonical_model_bytes(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _write_new_atomic(path: Path, payload: bytes) -> str:
    """Create one immutable file or verify an identical prior write."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise DeterministicScaleError(f"immutable output is not a regular file: {path}")
        if path.read_bytes() != payload:
            raise DeterministicScaleError(
                f"immutable output already exists with other bytes: {path}"
            )
        return hash_file(path)
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    try:
        with partial.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, path)
    finally:
        if partial.exists():
            partial.unlink()
    return hash_file(path)


@contextmanager
def _run_lock(output_dir: Path) -> Iterator[None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / "run.lock"
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeterministicScaleError(
                f"another deterministic materializer owns {lock_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _clean_project_tree_hash(
    project_dir: Path,
    *,
    expected_revision: str | None = None,
    expected_tree_hash: str | None = None,
) -> tuple[str, str]:
    """Bind the exact clean Git tree used to reconstruct source-file context."""

    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        tree_hash = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DeterministicScaleError(
            f"cannot bind clean Lean project tree at {project_dir}: {exc}"
        ) from exc
    if status.strip():
        raise DeterministicScaleError(
            "Lean project checkout is dirty; source-context reconstruction requires "
            "a clean pinned project"
        )
    if not revision or not tree_hash:
        raise DeterministicScaleError("Lean project Git revision/tree hash is empty")
    if expected_revision is not None and revision != expected_revision:
        raise DeterministicScaleError(
            f"Lean project revision changed during/resumed run: {revision} != {expected_revision}"
        )
    if expected_tree_hash is not None and tree_hash != expected_tree_hash:
        raise DeterministicScaleError(
            f"Lean project tree changed during/resumed run: {tree_hash} != {expected_tree_hash}"
        )
    return revision, tree_hash


def _seed(config: DeterministicScaleConfig, rule_id: str, source_ids: Sequence[str]) -> int:
    digest = hash_canonical(
        {
            "schema": "deterministic_scale_seed_v1",
            "base_seed": config.base_seed,
            "rule_id": rule_id,
            "source_theorem_ids": tuple(sorted(source_ids)),
        }
    )
    return int(digest[:16], 16)


def _selection_key(base_seed: int, theorem_id: str) -> tuple[str, str]:
    return (
        hash_canonical(
            {
                "schema": "deterministic_scale_source_order_v1",
                "base_seed": base_seed,
                "theorem_id": theorem_id,
            }
        ),
        theorem_id,
    )


def _root_component_shard_assignments(
    sources: Sequence[TheoremRecord],
    *,
    shard_count: int,
) -> tuple[int, ...]:
    """Assign complete root-ancestry components to balanced deterministic shards.

    Keeping every shared root ancestry in one shard makes unary-family
    per-root admission caps shard-local. Pair-aware N10 is explicitly
    prohibited when ``shard_count > 1`` and runs in a separate global pass.
    """

    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    if not sources:
        raise ValueError("cannot shard an empty source universe")
    parent = list(range(len(sources)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    first_by_root: dict[str, int] = {}
    for index, theorem in enumerate(sources):
        for root in theorem.root_ancestry_ids:
            first = first_by_root.setdefault(root, index)
            union(index, first)

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(sources)):
        components[find(index)].append(index)
    ordered_components = sorted(
        (tuple(indices) for indices in components.values()),
        key=lambda indices: (indices[0], tuple(sources[index].theorem_id for index in indices)),
    )
    if shard_count > len(ordered_components):
        raise DeterministicScaleError(
            "shard_count exceeds the number of disjoint root-ancestry components"
        )
    loads = [0] * shard_count
    assignments = [-1] * len(sources)
    for component in ordered_components:
        shard_index = min(range(shard_count), key=lambda index: (loads[index], index))
        for source_index in component:
            assignments[source_index] = shard_index
        loads[shard_index] += len(component)
    if any(assignment < 0 for assignment in assignments):
        raise AssertionError("internal source sharding left an unassigned source")
    return tuple(assignments)


def _validate_shard_execution_policy(
    config: DeterministicScaleConfig,
    *,
    shard_count: int,
) -> None:
    """Reject policies whose admission or donor semantics are not shard-local."""

    if shard_count > 1 and config.max_accepted_variants_per_family is not None:
        raise DeterministicScaleError(
            "multi-shard materialization requires max_accepted_variants_per_family=null; "
            "a global family cap is not independently shardable"
        )
    if shard_count > 1 and "n10_nearby_theorem" in config.active_rule_ids:
        raise DeterministicScaleError(
            "N10 cannot run inside source shards: donor scheduling and dual-ancestry "
            "admission are global. Use a sharded unary-only profile, then execute N10 "
            "as a dedicated shard_count=1 global pass."
        )


def _candidate_inline_source(
    source: TheoremRecord,
    candidate_code: str,
    *,
    project_dir: Path,
    import_header: str,
) -> str:
    """Recreate the candidate at the source declaration's original context."""

    if source.inline_elaboration_source is not None:
        count = source.inline_elaboration_source.count(source.proof_stripped_declaration)
        if count != 1:
            raise DeterministicScaleError(
                "inline_elaboration_source must contain the source declaration exactly once"
            )
        return source.inline_elaboration_source.replace(
            source.proof_stripped_declaration,
            candidate_code,
            1,
        )
    if source.source_file is not None and source.source_range is not None:
        source_path = (project_dir / source.source_file).resolve()
        if not source_path.is_relative_to(project_dir.resolve()):
            raise DeterministicScaleError("source_file escapes the pinned Lean project")
        try:
            lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError as exc:
            raise DeterministicScaleError(
                f"cannot read source context {source_path}: {exc}"
            ) from exc
        start_line = source.source_range[0]
        if start_line < 1 or start_line > len(lines) + 1:
            raise DeterministicScaleError("source_range start is outside source_file")
        return "".join(lines[: start_line - 1]) + candidate_code + "\n"
    return "\n".join(part for part in (import_header.strip(), candidate_code) if part)


def _candidate_validation(
    backend: LeanInteractBackend,
    *,
    draft: VariantDraft,
    context_id: str,
    inline_source: str,
    timeout_seconds: float,
) -> _CandidateValidation:
    request = LeanRequest(
        request_id=f"det-scale-{draft.draft_id.removeprefix('draft:')[:24]}-validate",
        context_id=context_id,
        code=inline_source,
        declarations=True,
        allow_sorry=True,
        timeout_seconds=timeout_seconds,
        metadata={
            "artifact_kind": "deterministic_scale_candidate_validation",
            "draft_id": draft.draft_id,
        },
    )
    result = run_with_retries(
        backend.run,
        request,
        RetryPolicy(
            max_attempts=2,
            retry_statuses=frozenset(
                {
                    LeanStatus.CRASH,
                    LeanStatus.INTERNAL_ERROR,
                    LeanStatus.TIMEOUT,
                }
            ),
        ),
    ).result
    diagnostics = tuple(str(message.get("data", "")) for message in result.messages)
    if result.status == LeanStatus.VALID:
        status = ValidationStatus.ELABORATES
    elif result.status == LeanStatus.VALID_WITH_SORRY:
        status = ValidationStatus.ELABORATES_WITH_PLACEHOLDER
    else:
        raise _CandidateValidationFailure(
            status=result.status,
            request_hash=result.request_hash,
            diagnostics=diagnostics,
        )
    return _CandidateValidation(
        status=status,
        diagnostics=diagnostics,
        request_hash=result.request_hash,
    )


def _candidate_representation(
    backend: LeanInteractBackend,
    *,
    candidate: TheoremRecord,
    created_at: datetime.datetime,
) -> RepresentationRecord:
    full_name = candidate.declaration_full_name
    if full_name is None:
        raise DeterministicScaleError("candidate has no declaration_full_name")
    records = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=candidate.theorem_id,
                full_name=full_name,
                proof_stripped=candidate.proof_stripped_declaration,
                context_id=candidate.context_id,
                inline_declaration=True,
                inline_source=candidate.inline_elaboration_source,
            )
        ],
        # The full inline source carries the exact source imports. Importing
        # Mathlib here would preload a mathlib source theorem under the same
        # name before its transformed declaration is introduced.
        imports="",
        created_at=created_at,
    )
    if len(records) != 1:
        raise DeterministicScaleError("candidate representation did not return exactly one record")
    record = records[0]
    failures = tuple(
        view for view in _REQUIRED_CANDIDATE_VIEWS if record.view_status[view] != ViewStatus.OK
    )
    if failures or record.alpha_identity_fingerprint is None:
        detail = ",".join(failures) if failures else "alpha_identity_fingerprint"
        raise DeterministicScaleError(f"candidate representation missing required views: {detail}")
    if record.normalization_version != NORMALIZATION_VERSION:
        raise DeterministicScaleError(
            f"candidate representation version is {record.normalization_version}, "
            f"expected {NORMALIZATION_VERSION}"
        )
    return record


def _representation_overlap(
    benchmark: ActiveBenchmarkRegistry,
    representation: RepresentationRecord,
) -> bool:
    values = (
        (
            signature_near_dup_hash(representation.headless)
            if representation.headless is not None
            else None
        ),
        (
            signature_near_dup_hash(representation.signature_pp)
            if representation.signature_pp is not None
            else None
        ),
        (
            signature_near_dup_hash(representation.signature_explicit)
            if representation.signature_explicit is not None
            else None
        ),
        representation.alpha_identity_fingerprint,
    )
    return any(
        value is not None and benchmark.index.contains_representation(value) for value in values
    )


def _protected_overlap(
    benchmark: ActiveBenchmarkRegistry,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
) -> bool:
    return benchmark.index.contains_lean(
        theorem.proof_stripped_declaration
    ) or _representation_overlap(benchmark, representation)


def _protected_draft_result(
    draft: VariantDraft,
    *,
    detail: str,
    request_hash: str | None = None,
) -> ScaleDraftResult:
    return ScaleDraftResult(
        status="protected_benchmark_overlap",
        draft=None,
        redacted_draft_id=draft.draft_id,
        redacted_candidate_code_hash=draft.candidate_code_hash,
        candidate_content_redacted=True,
        failure=_failure(
            stage="candidate_admission",
            code="protected_benchmark_overlap",
            detail=detail,
            source_ids=draft.source_theorem_ids,
            rule_id=draft.rule_id,
            draft_id=draft.draft_id,
            request_hash=request_hash,
        ),
    )


def _candidate_raw_request_ids(
    draft: VariantDraft,
    *,
    candidate_theorem_id: str,
) -> frozenset[str]:
    theorem_suffix = candidate_theorem_id.removeprefix("thm:")[:16]
    representation_prefix = f"repr-{theorem_suffix}-{NORMALIZATION_VERSION}"
    return frozenset(
        {
            f"det-scale-{draft.draft_id.removeprefix('draft:')[:24]}-validate",
            f"{representation_prefix}-combined",
            f"{representation_prefix}-signature_pp",
            f"{representation_prefix}-signature_explicit",
            f"{representation_prefix}-expr",
        }
    )


def _purge_candidate_raw_artifacts(
    raw_response_dir: Path,
    *,
    request_ids: frozenset[str],
) -> int:
    """Remove every raw request/response artifact attributable to one redacted draft."""

    if not raw_response_dir.exists():
        return 0
    submission_digests = {
        hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:8] for request_id in request_ids
    }
    removed = 0
    for path in sorted(raw_response_dir.glob("*.json")):
        matched = False
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
            request = payload.get("request") if isinstance(payload, dict) else None
            request_id = request.get("request_id") if isinstance(request, dict) else None
            matched = request_id in request_ids
        except Exception:
            # A malformed artifact still cannot retain protected bytes. The
            # backend filename carries the first eight hex digits of
            # SHA256(request_id); use that only as a fail-closed fallback.
            matched = any(f".{digest}" in path.name for digest in submission_digests)
        if matched:
            try:
                path.unlink()
            except OSError as exc:
                raise DeterministicScaleError(
                    f"could not purge protected raw Lean artifact {path}"
                ) from exc
            removed += 1
    return removed


def _source_failure(
    theorem: TheoremRecord,
    representation: RepresentationRecord | None,
    *,
    config: DeterministicScaleConfig,
    benchmark: ActiveBenchmarkRegistry,
) -> ScaleFailure | None:
    code: str | None = None
    detail: str | None = None
    if not theorem.is_proposition:
        code, detail = "source_not_proposition", "source theorem is not proposition-valued"
    elif theorem.elaboration_status not in _VALID_SOURCE_STATUSES:
        code, detail = "source_not_elaborated", "source theorem lacks an elaborating status"
    elif representation is None:
        code, detail = "source_representation_missing", "no RepresentationRecord for theorem"
    elif representation.theorem_id != theorem.theorem_id:
        code, detail = "source_representation_lineage", "representation theorem link mismatch"
    elif representation.context_id != theorem.context_id:
        code, detail = "source_representation_context", "representation context mismatch"
    elif representation.normalization_version != config.normalization_version:
        code, detail = (
            "source_representation_version",
            f"expected {config.normalization_version}, got {representation.normalization_version}",
        )
    elif representation.raw_proof_stripped != theorem.proof_stripped_declaration:
        code, detail = (
            "source_representation_text",
            "raw_proof_stripped differs from theorem proof-stripped declaration",
        )
    else:
        failed_views = tuple(
            view
            for view in config.required_source_views
            if representation.view_status.get(view) != ViewStatus.OK
        )
        if failed_views:
            code, detail = (
                "source_required_view_failed",
                "missing views: " + ",".join(failed_views),
            )
        elif representation.alpha_identity_fingerprint is None:
            code, detail = (
                "source_alpha_identity_missing",
                "source has no binder-normalized identity fingerprint",
            )
        elif _protected_overlap(benchmark, theorem, representation):
            code, detail = (
                "source_protected_benchmark_overlap",
                "source matches the active protected benchmark registry",
            )
    if code is None:
        return None
    return ScaleFailure(
        stage="source_preflight",
        code=code,
        detail=detail or code,
        source_theorem_ids=(theorem.theorem_id,),
    )


def _cap_failure(
    *,
    state: _AdmissionState,
    roots: tuple[str, ...],
    family_id: str,
    config: DeterministicScaleConfig,
    source_ids: tuple[str, ...],
    rule_id: str,
) -> ScaleFailure | None:
    if any(
        state.root_counts[root] >= config.max_accepted_variants_per_root_ancestry for root in roots
    ):
        return ScaleFailure(
            stage="admission",
            code="root_ancestry_cap_reached",
            detail="at least one source root ancestry reached its configured candidate cap",
            source_theorem_ids=source_ids,
            rule_id=rule_id,
        )
    if any(
        state.family_root_counts[(family_id, root)]
        >= config.max_accepted_variants_per_family_per_root_ancestry
        for root in roots
    ):
        return ScaleFailure(
            stage="admission",
            code="family_root_ancestry_cap_reached",
            detail="family reached its configured per-root ancestry candidate cap",
            source_theorem_ids=source_ids,
            rule_id=rule_id,
        )
    family_cap = config.max_accepted_variants_per_family
    if family_cap is not None and state.family_counts[family_id] >= family_cap:
        return ScaleFailure(
            stage="admission",
            code="family_global_cap_reached",
            detail="family reached its configured global candidate cap",
            source_theorem_ids=source_ids,
            rule_id=rule_id,
        )
    return None


def _admit(state: _AdmissionState, draft_result: ScaleDraftResult) -> None:
    assert draft_result.status == "accepted"
    assert draft_result.candidate_theorem is not None
    assert draft_result.variant is not None
    assert draft_result.pair is not None
    family_id = draft_result.variant.family_id
    candidate_code_hash = draft_result.variant.candidate_code_hash
    assert family_id is not None
    assert candidate_code_hash is not None
    roots = draft_result.candidate_theorem.root_ancestry_ids
    for root in roots:
        state.root_counts[root] += 1
        state.family_root_counts[(family_id, root)] += 1
    state.family_counts[family_id] += 1
    state.candidate_keys.add((roots, candidate_code_hash))
    if draft_result.variant.variant_id in state.variant_ids:
        raise DeterministicScaleError("duplicate accepted variant ID in journal")
    if draft_result.pair.pair_id in state.pair_ids:
        raise DeterministicScaleError("duplicate accepted pair ID in journal")
    state.variant_ids.add(draft_result.variant.variant_id)
    state.pair_ids.add(draft_result.pair.pair_id)


def _admit_source_shard(state: _AdmissionState, shard: ScaleSourceShard) -> None:
    """Rebuild admission state from one already validated immutable shard."""

    for rule in shard.rule_results:
        for draft_result in rule.draft_results:
            if draft_result.status == "accepted":
                _admit(state, draft_result)


def _failure(
    *,
    stage: str,
    code: str,
    detail: str,
    source_ids: Sequence[str],
    rule_id: str | None = None,
    draft_id: str | None = None,
    request_hash: str | None = None,
) -> ScaleFailure:
    return ScaleFailure(
        stage=stage,
        code=code,
        detail=(detail or code)[:4000],
        source_theorem_ids=tuple(sorted(source_ids)),
        rule_id=rule_id,
        draft_id=draft_id,
        lean_request_hash=request_hash,
    )


def _dispatch_failure_result(
    *,
    exc: Exception,
    rule_id: str,
    family_id: str,
    polarity: Polarity,
    seed: int,
    source_ids: tuple[str, ...],
    donor_id: str | None,
) -> ScaleRuleResult:
    execution = getattr(exc, "execution", None)
    attempt = execution.attempt if isinstance(execution, TransformationExecution) else None
    return ScaleRuleResult(
        status="dispatch_failed",
        rule_id=rule_id,
        family_id=family_id,
        polarity=polarity,
        seed=seed,
        source_theorem_ids=source_ids,
        donor_theorem_id=donor_id,
        attempt=attempt,
        failure=_failure(
            stage=getattr(exc, "stage", "dispatch"),
            code=type(exc).__name__,
            detail=str(exc),
            source_ids=source_ids,
            rule_id=rule_id,
        ),
    )


def _materialize_draft(
    *,
    backend: LeanInteractBackend,
    loaded_registry: LoadedTransformationRegistry,
    unary_runtime: object | None,
    pair_rule: object | None,
    benchmark: ActiveBenchmarkRegistry,
    config: DeterministicScaleConfig,
    run_spec_hash: str,
    source_index: int,
    primary: TheoremRecord,
    primary_representation: RepresentationRecord,
    sources: tuple[TheoremRecord, ...],
    source_representations: tuple[RepresentationRecord, ...],
    attempt: TransformationAttempt,
    draft: VariantDraft,
    polarity: Polarity,
    project_dir: Path,
    import_header: str,
    raw_response_dir: Path,
    state: _AdmissionState,
) -> ScaleDraftResult:
    roots = tuple(sorted({root for source in sources for root in source.root_ancestry_ids}))
    cap = _cap_failure(
        state=state,
        roots=roots,
        family_id=draft.family_id,
        config=config,
        source_ids=draft.source_theorem_ids,
        rule_id=draft.rule_id,
    )
    if cap is not None:
        return ScaleDraftResult(status="cap_skipped", draft=draft, failure=cap)
    if benchmark.index.contains_lean(draft.candidate_code):
        return _protected_draft_result(
            draft,
            detail="candidate raw Lean matches the active benchmark registry",
        )

    try:
        inline_source = _candidate_inline_source(
            primary,
            draft.candidate_code,
            project_dir=project_dir,
            import_header=import_header,
        )
        validation = _candidate_validation(
            backend,
            draft=draft,
            context_id=draft.context_id,
            inline_source=inline_source,
            timeout_seconds=config.candidate_timeout_seconds,
        )
    except _CandidateValidationFailure as exc:
        status: ScaleDraftStatus = (
            "candidate_invalid"
            if exc.status == LeanStatus.INVALID
            else "candidate_infrastructure_error"
        )
        return ScaleDraftResult(
            status=status,
            draft=draft,
            failure=_failure(
                stage="candidate_validation",
                code=(
                    "candidate_lean_invalid"
                    if exc.status == LeanStatus.INVALID
                    else "candidate_validation_infrastructure_error"
                ),
                detail=str(exc),
                source_ids=draft.source_theorem_ids,
                rule_id=draft.rule_id,
                draft_id=draft.draft_id,
                request_hash=exc.request_hash,
            ),
        )
    except Exception as exc:
        return ScaleDraftResult(
            status="candidate_infrastructure_error",
            draft=draft,
            failure=_failure(
                stage="candidate_validation",
                code=f"candidate_validation_{type(exc).__name__}",
                detail=str(exc),
                source_ids=draft.source_theorem_ids,
                rule_id=draft.rule_id,
                draft_id=draft.draft_id,
            ),
        )
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=sources,
        primary_source_id=primary.theorem_id,
        elaboration_status=validation.status,
        elaboration_diagnostics=validation.diagnostics,
        inline_elaboration_source=inline_source,
        metadata={
            "run_spec_hash": run_spec_hash,
            "scale_profile_id": config.profile_id,
            "source_index": source_index,
            "validation_request_hash": validation.request_hash,
            "inline_context_sha256": hashlib.sha256(inline_source.encode("utf-8")).hexdigest(),
            "inline_context_persisted": False,
        },
    )
    try:
        candidate_representation = _candidate_representation(
            backend,
            candidate=candidate,
            created_at=config.record_timestamp_utc,
        )
    except Exception as exc:
        return ScaleDraftResult(
            status="candidate_representation_failed",
            draft=draft,
            failure=_failure(
                stage="candidate_representation",
                code=type(exc).__name__,
                detail=str(exc),
                source_ids=draft.source_theorem_ids,
                rule_id=draft.rule_id,
                draft_id=draft.draft_id,
                request_hash=validation.request_hash,
            ),
        )
    # Representation signatures are part of the protected benchmark boundary.
    # Check them before any audit return: an audit exception/quarantine must
    # never persist a protected draft or leave its raw Lean requests behind.
    if _protected_overlap(benchmark, candidate, candidate_representation):
        _purge_candidate_raw_artifacts(
            raw_response_dir,
            request_ids=_candidate_raw_request_ids(
                draft,
                candidate_theorem_id=candidate.theorem_id,
            ),
        )
        return _protected_draft_result(
            draft,
            detail="candidate representation matches the active benchmark registry",
            request_hash=validation.request_hash,
        )

    try:
        if draft.rule_id == "n10_nearby_theorem":
            if pair_rule is None or len(sources) != 2 or len(source_representations) != 2:
                raise DeterministicScaleError("N10 audit lacks its explicit two-source lineage")
            audit = audit_pair_transformation(
                loaded_registry,
                pair_rule,  # type: ignore[arg-type]
                sources[0],
                source_representations[0],
                sources[1],
                source_representations[1],
                candidate,
                candidate_representation,
                draft,
            )
        else:
            if unary_runtime is None:
                raise DeterministicScaleError("unary audit runtime is missing")
            audit = unary_runtime.audit(  # type: ignore[attr-defined]
                draft.rule_id,
                primary,
                primary_representation,
                candidate,
                candidate_representation,
                draft,
            )
    except Exception as exc:
        return ScaleDraftResult(
            status="audit_quarantined",
            draft=draft,
            failure=_failure(
                stage="candidate_audit",
                code=type(exc).__name__,
                detail=str(exc),
                source_ids=draft.source_theorem_ids,
                rule_id=draft.rule_id,
                draft_id=draft.draft_id,
                request_hash=validation.request_hash,
            ),
        )
    if (
        audit.violation_codes
        or audit.recommended_quality_tier != QualityTier.PROVISIONAL
        or audit.recommended_validation_status not in _VALID_SOURCE_STATUSES
    ):
        return ScaleDraftResult(
            status="audit_quarantined",
            draft=draft,
            failure=_failure(
                stage="candidate_audit",
                code="mechanical_audit_not_clean",
                detail=",".join(audit.violation_codes) or "audit did not recommend provisional",
                source_ids=draft.source_theorem_ids,
                rule_id=draft.rule_id,
                draft_id=draft.draft_id,
                request_hash=validation.request_hash,
            ),
        )
    candidate_key = (candidate.root_ancestry_ids, draft.candidate_code_hash)
    if candidate_key in state.candidate_keys:
        return ScaleDraftResult(
            status="duplicate_candidate",
            draft=draft,
            failure=_failure(
                stage="candidate_admission",
                code="duplicate_candidate_within_ancestry",
                detail="candidate code already exists for the same complete root ancestry set",
                source_ids=draft.source_theorem_ids,
                rule_id=draft.rule_id,
                draft_id=draft.draft_id,
                request_hash=validation.request_hash,
            ),
        )

    variant = build_deterministic_variant_record(
        attempt=attempt,
        draft=draft,
        audit=audit,
        candidate=candidate,
        candidate_representation=candidate_representation,
        polarity=polarity,
        metadata={
            "run_spec_hash": run_spec_hash,
            "scale_profile_id": config.profile_id,
            "source_index": source_index,
        },
    )
    pair = build_deterministic_pair_record(
        source=primary,
        candidate=candidate,
        draft=draft,
        audit=audit,
        all_sources=sources,
        metadata={
            "run_spec_hash": run_spec_hash,
            "scale_profile_id": config.profile_id,
            "source_index": source_index,
        },
    )
    # The full inline context may contain thousands of preceding declarations
    # and proof bodies. It was needed only for Lean validation/representation;
    # persist its hash, never its bytes, in accepted scientific partitions or
    # journal shards.
    candidate = candidate.model_copy(update={"inline_elaboration_source": None})
    result = ScaleDraftResult(
        status="accepted",
        draft=draft,
        candidate_theorem=candidate,
        candidate_representation=candidate_representation,
        audit=audit,
        variant=variant,
        pair=pair,
    )
    _admit(state, result)
    return result


def _execute_rule(
    *,
    backend: LeanInteractBackend,
    loaded_registry: LoadedTransformationRegistry,
    unary_runtime: object | None,
    pair_rule: object | None,
    benchmark: ActiveBenchmarkRegistry,
    config: DeterministicScaleConfig,
    run_spec_hash: str,
    source_index: int,
    primary: TheoremRecord,
    primary_representation: RepresentationRecord,
    sources: tuple[TheoremRecord, ...],
    source_representations: tuple[RepresentationRecord, ...],
    rule_id: str,
    polarity: Polarity,
    project_dir: Path,
    import_header: str,
    raw_response_dir: Path,
    state: _AdmissionState,
) -> ScaleRuleResult:
    source_ids = tuple(sorted(source.theorem_id for source in sources))
    seed = _seed(config, rule_id, source_ids)
    roots = tuple(sorted({root for source in sources for root in source.root_ancestry_ids}))
    cap = _cap_failure(
        state=state,
        roots=roots,
        family_id=rule_id,
        config=config,
        source_ids=source_ids,
        rule_id=rule_id,
    )
    if cap is not None:
        return ScaleRuleResult(
            status="cap_skipped",
            rule_id=rule_id,
            family_id=rule_id,
            polarity=polarity,
            seed=seed,
            source_theorem_ids=source_ids,
            donor_theorem_id=(sources[1].theorem_id if len(sources) == 2 else None),
            failure=cap,
        )
    try:
        if rule_id == "n10_nearby_theorem":
            if pair_rule is None or len(sources) != 2:
                raise DeterministicScaleError("N10 execution lacks exactly two source records")
            execution = execute_pair_transformation(
                loaded_registry,
                pair_rule,  # type: ignore[arg-type]
                sources[0],
                source_representations[0],
                sources[1],
                source_representations[1],
                seed,
            )
        else:
            if unary_runtime is None:
                raise DeterministicScaleError("unary transformation runtime is missing")
            execution = unary_runtime.execute(  # type: ignore[attr-defined]
                rule_id,
                primary,
                primary_representation,
                seed,
            )
    except (TransformationExecutionFailed, PairTransformationDispatchError) as exc:
        return _dispatch_failure_result(
            exc=exc,
            rule_id=rule_id,
            family_id=rule_id,
            polarity=polarity,
            seed=seed,
            source_ids=source_ids,
            donor_id=(sources[1].theorem_id if len(sources) == 2 else None),
        )
    attempt = execution.attempt
    if attempt.terminal_outcome == "not_applicable":
        return ScaleRuleResult(
            status="not_applicable",
            rule_id=rule_id,
            family_id=rule_id,
            polarity=polarity,
            seed=seed,
            source_theorem_ids=source_ids,
            donor_theorem_id=(sources[1].theorem_id if len(sources) == 2 else None),
            attempt=attempt,
        )
    if attempt.terminal_outcome == "no_output":
        return ScaleRuleResult(
            status="no_output",
            rule_id=rule_id,
            family_id=rule_id,
            polarity=polarity,
            seed=seed,
            source_theorem_ids=source_ids,
            donor_theorem_id=(sources[1].theorem_id if len(sources) == 2 else None),
            attempt=attempt,
        )
    draft_results = tuple(
        _materialize_draft(
            backend=backend,
            loaded_registry=loaded_registry,
            unary_runtime=unary_runtime,
            pair_rule=pair_rule,
            benchmark=benchmark,
            config=config,
            run_spec_hash=run_spec_hash,
            source_index=source_index,
            primary=primary,
            primary_representation=primary_representation,
            sources=sources,
            source_representations=source_representations,
            attempt=attempt,
            draft=draft,
            polarity=polarity,
            project_dir=project_dir,
            import_header=import_header,
            raw_response_dir=raw_response_dir,
            state=state,
        )
        for draft in execution.drafts
    )
    accepted = any(result.status == "accepted" for result in draft_results)
    status: ScaleRuleStatus = "accepted" if accepted else draft_results[0].status
    return ScaleRuleResult(
        status=status,
        rule_id=rule_id,
        family_id=rule_id,
        polarity=polarity,
        seed=seed,
        source_theorem_ids=source_ids,
        donor_theorem_id=(sources[1].theorem_id if len(sources) == 2 else None),
        attempt=attempt,
        draft_results=draft_results,
        failure=(
            None
            if accepted or draft_results
            else _failure(
                stage="materialization",
                code="generated_attempt_without_drafts",
                detail="generated attempt unexpectedly returned no drafts",
                source_ids=source_ids,
                rule_id=rule_id,
            )
        ),
    )


def _source_shard_path(journal_dir: Path, index: int, theorem_id: str) -> Path:
    suffix = theorem_id.removeprefix("thm:")[:16]
    return journal_dir / f"{index:08d}-{suffix}.json"


def _journal_receipt_path(receipt_dir: Path, shard_path: Path) -> Path:
    return receipt_dir / f"{shard_path.stem}.receipt.json"


def _load_source_shard(path: Path) -> ScaleSourceShard:
    try:
        payload = path.read_bytes()
        raw = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        shard = ScaleSourceShard.model_validate(raw)
        if payload != _canonical_model_bytes(shard):
            raise ValueError("source shard is not canonical JSON")
        return shard
    except Exception as exc:
        raise DeterministicScaleError(f"invalid immutable source shard {path}: {exc}") from exc


def _build_journal_receipt(
    *,
    shard: ScaleSourceShard,
    shard_path: Path,
    previous_receipt_hash: str,
) -> ScaleJournalReceipt:
    data: dict[str, object] = {
        "schema_version": 1,
        "artifact_kind": "deterministic_scale_journal_receipt",
        "run_spec_hash": shard.run_spec_hash,
        "source_index": shard.source_index,
        "source_theorem_id": shard.source_theorem_id,
        "shard_filename": shard_path.name,
        "shard_sha256": hash_file(shard_path),
        "previous_receipt_hash": previous_receipt_hash,
    }
    return ScaleJournalReceipt.model_validate({"receipt_hash": hash_canonical(data), **data})


def _build_lean_replay_audit(
    *,
    run_spec: DeterministicScaleRunSpec,
    run_spec_path: Path,
    replayed_source_ids: Sequence[str],
    journal_tree_hash: str,
    partition_sha256: Mapping[str, str],
    created_at: datetime.datetime,
) -> ScaleLeanReplayAudit:
    data: dict[str, object] = {
        "schema_version": 1,
        "artifact_kind": "deterministic_scale_replay_audit",
        "run_spec_hash": run_spec.run_spec_hash,
        "run_spec_sha256": hash_file(run_spec_path),
        "replay_mode": "exact_lean_backed_replay",
        "replayed_source_count": len(replayed_source_ids),
        "replayed_source_ids_sha256": hash_canonical(tuple(replayed_source_ids)),
        "journal_tree_hash": journal_tree_hash,
        "partition_sha256": dict(sorted(partition_sha256.items())),
        "replay_completed": True,
        "authentication_strength": "self_hash_only",
        "created_at": created_at,
    }
    canonical = {
        **data,
        "created_at": TypeAdapter(datetime.datetime).dump_python(
            created_at,
            mode="json",
        ),
    }
    return ScaleLeanReplayAudit.model_validate({"audit_hash": hash_canonical(canonical), **data})


def _load_lean_replay_audit(
    *,
    path: Path,
    run_spec: DeterministicScaleRunSpec,
    run_spec_path: Path,
    replayed_source_ids: Sequence[str],
    journal_tree_hash: str,
    partition_sha256: Mapping[str, str],
    created_at: datetime.datetime,
) -> ScaleLeanReplayAudit:
    try:
        payload = path.read_bytes()
        raw = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        audit = ScaleLeanReplayAudit.model_validate(raw)
    except Exception as exc:
        raise DeterministicScaleError(f"invalid Lean-replay audit {path}: {exc}") from exc
    if payload != _canonical_model_bytes(audit):
        raise DeterministicScaleError(f"Lean-replay audit is not canonical JSON: {path}")
    expected = _build_lean_replay_audit(
        run_spec=run_spec,
        run_spec_path=run_spec_path,
        replayed_source_ids=replayed_source_ids,
        journal_tree_hash=journal_tree_hash,
        partition_sha256=partition_sha256,
        created_at=created_at,
    )
    if audit != expected:
        raise DeterministicScaleError(
            "Lean-replay audit does not bind the current journal/partitions"
        )
    return audit


def _load_journal_receipt(
    *,
    path: Path,
    shard: ScaleSourceShard,
    shard_path: Path,
    previous_receipt_hash: str,
) -> ScaleJournalReceipt:
    try:
        payload = path.read_bytes()
        raw = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        receipt = ScaleJournalReceipt.model_validate(raw)
    except Exception as exc:
        raise DeterministicScaleError(f"invalid immutable journal receipt {path}: {exc}") from exc
    if payload != _canonical_model_bytes(receipt):
        raise DeterministicScaleError(f"journal receipt is not canonical JSON: {path}")
    expected = _build_journal_receipt(
        shard=shard,
        shard_path=shard_path,
        previous_receipt_hash=previous_receipt_hash,
    )
    if receipt != expected:
        raise DeterministicScaleError(
            f"journal receipt does not bind the current shard/chain: {path}"
        )
    return receipt


def _representation_payload_hash(record: RepresentationRecord) -> str:
    return representation_content_hash(
        {
            "raw_proof_stripped": record.raw_proof_stripped,
            "headless": record.headless,
            "signature_pp": record.signature_pp,
            "signature_explicit": record.signature_explicit,
            "semantic_atoms": (
                list(record.semantic_atoms) if record.semantic_atoms is not None else None
            ),
            "operator_tree": record.operator_tree,
            "alpha_identity_fingerprint": record.alpha_identity_fingerprint,
        }
    )


def _validate_replayed_generation(
    *,
    result: ScaleRuleResult,
    source: TheoremRecord,
    sources: tuple[TheoremRecord, ...],
    source_representations: tuple[RepresentationRecord, ...],
    loaded_registry: LoadedTransformationRegistry,
    positive_runtime: object,
    negative_runtime: object,
    pair_rule: object | None,
) -> None:
    """Re-execute a deterministic rule and bind the journal to its exact payload."""

    try:
        if result.rule_id == "n10_nearby_theorem":
            if pair_rule is None or len(sources) != 2 or len(source_representations) != 2:
                raise DeterministicScaleError(
                    "resume semantic replay lacks the configured N10 pair runtime"
                )
            execution = execute_pair_transformation(
                loaded_registry,
                pair_rule,  # type: ignore[arg-type]
                sources[0],
                source_representations[0],
                sources[1],
                source_representations[1],
                result.seed,
            )
        else:
            runtime = positive_runtime if result.polarity == Polarity.POSITIVE else negative_runtime
            execution = runtime.execute(  # type: ignore[attr-defined]
                result.rule_id,
                source,
                source_representations[0],
                result.seed,
            )
    except (TransformationExecutionFailed, PairTransformationDispatchError) as exc:
        replayed = getattr(exc, "execution", None)
        replayed_attempt = (
            replayed.attempt if isinstance(replayed, TransformationExecution) else None
        )
        if result.status != "dispatch_failed" or result.attempt != replayed_attempt:
            raise DeterministicScaleError(
                "resume shard dispatch outcome differs from deterministic semantic replay"
            ) from exc
        return

    if result.status == "dispatch_failed" or result.attempt != execution.attempt:
        raise DeterministicScaleError(
            "resume shard attempt differs from deterministic semantic replay"
        )
    terminal_status = {
        "not_applicable": "not_applicable",
        "no_output": "no_output",
    }.get(execution.attempt.terminal_outcome)
    if terminal_status is not None:
        if result.status != terminal_status or result.draft_results:
            raise DeterministicScaleError(
                "resume shard terminal outcome differs from deterministic semantic replay"
            )
        return
    if execution.attempt.terminal_outcome != "generated":
        raise DeterministicScaleError(
            "resume deterministic semantic replay returned an unsupported terminal outcome"
        )

    expected_drafts = {draft.draft_id: draft for draft in execution.drafts}
    observed_ids = tuple(
        sorted(draft_result.persistent_draft_id for draft_result in result.draft_results)
    )
    if observed_ids != execution.attempt.draft_ids or len(observed_ids) != len(
        result.draft_results
    ):
        raise DeterministicScaleError(
            "resume shard draft inventory differs from deterministic semantic replay"
        )
    for draft_result in result.draft_results:
        replayed_draft = expected_drafts.get(draft_result.persistent_draft_id)
        if (
            replayed_draft is None
            or draft_result.persistent_candidate_code_hash != replayed_draft.candidate_code_hash
            or (draft_result.draft is not None and draft_result.draft != replayed_draft)
        ):
            raise DeterministicScaleError(
                "resume shard draft payload differs from deterministic semantic replay"
            )


def _require_exact_resume_replay(
    persisted: ScaleSourceShard,
    rebuilt: ScaleSourceShard,
) -> None:
    """Fail closed unless current Lean-backed materialization exactly replays."""

    if persisted != rebuilt:
        raise DeterministicScaleError(
            "persisted resume shard differs from exact Lean-backed deterministic replay"
        )


def _role_ordered_replay_inputs(
    *,
    rule_id: str,
    primary: TheoremRecord,
    primary_representation: RepresentationRecord,
    donor_theorem_id: str | None,
    theorem_by_id: Mapping[str, TheoremRecord],
    representation_by_theorem: Mapping[str, RepresentationRecord],
) -> tuple[tuple[TheoremRecord, ...], tuple[RepresentationRecord, ...]]:
    """Preserve semantic primary/donor roles independently of canonical IDs."""

    if rule_id != "n10_nearby_theorem":
        return (primary,), (primary_representation,)
    if donor_theorem_id is None:
        raise DeterministicScaleError("N10 semantic replay lacks its scheduled donor")
    donor = theorem_by_id.get(donor_theorem_id)
    donor_representation = representation_by_theorem.get(donor_theorem_id)
    if donor is None or donor_representation is None:
        raise DeterministicScaleError(
            "N10 semantic replay donor leaves the immutable source inventory"
        )
    return (primary, donor), (primary_representation, donor_representation)


def _validate_resume_shard(
    *,
    shard: ScaleSourceShard,
    expected_index: int,
    source: TheoremRecord,
    source_representation: RepresentationRecord | None,
    theorem_by_id: Mapping[str, TheoremRecord],
    representation_by_theorem: Mapping[str, RepresentationRecord],
    expected_donors: Sequence[TheoremRecord],
    config: DeterministicScaleConfig,
    loaded_registry: LoadedTransformationRegistry,
    positive_runtime: object,
    negative_runtime: object,
    pair_rule: object | None,
    benchmark: ActiveBenchmarkRegistry,
    run_spec_hash: str,
) -> None:
    """Revalidate semantic lineage before a persisted shard affects admission state."""

    expected_source_failure = _source_failure(
        source,
        source_representation,
        config=config,
        benchmark=benchmark,
    )
    if (
        shard.run_spec_hash != run_spec_hash
        or shard.source_index != expected_index
        or shard.source_theorem_id != source.theorem_id
    ):
        raise DeterministicScaleError("resume shard source/run identity mismatch")
    if expected_source_failure is not None or source_representation is None:
        if (
            shard.source_status != "ineligible"
            or shard.source_failure != expected_source_failure
            or shard.rule_results
        ):
            raise DeterministicScaleError(
                "resume shard ineligible-source outcome differs from current inputs"
            )
        return
    if (
        shard.source_status != "eligible"
        or shard.source_failure is not None
        or shard.source_representation_id != source_representation.representation_id
    ):
        raise DeterministicScaleError("resume shard eligible-source lineage mismatch")

    registry_rules = {
        str(rule.rule_id): rule
        for family in loaded_registry.config.families
        for rule in family.rules
    }
    grouped: dict[str, list[ScaleRuleResult]] = defaultdict(list)
    observed_order: list[str] = []
    for result in shard.rule_results:
        if result.rule_id not in grouped:
            observed_order.append(result.rule_id)
        grouped[result.rule_id].append(result)
    if observed_order != list(config.active_rule_ids) or set(grouped) != set(
        config.active_rule_ids
    ):
        raise DeterministicScaleError(
            "resume shard rule inventory/order differs from the active scale policy"
        )
    expected_donor_ids = tuple(donor.theorem_id for donor in expected_donors)
    for rule_id in config.active_rule_ids:
        rule_config = registry_rules.get(rule_id)
        if rule_config is None:
            raise DeterministicScaleError(f"active resume rule is absent from registry: {rule_id}")
        results = grouped[rule_id]
        if rule_id != "n10_nearby_theorem" and len(results) != 1:
            raise DeterministicScaleError(
                f"resume shard has {len(results)} outcomes for unary rule {rule_id}"
            )
        if rule_id == "n10_nearby_theorem":
            if not expected_donor_ids:
                if len(results) != 1 or results[0].status != "no_donor":
                    raise DeterministicScaleError(
                        "resume shard N10 outcome differs from empty donor schedule"
                    )
            else:
                donor_ids = tuple(result.donor_theorem_id for result in results)
                if donor_ids != expected_donor_ids[: len(donor_ids)] or not donor_ids:
                    raise DeterministicScaleError(
                        "resume shard N10 donors differ from deterministic donor order"
                    )
                if len(results) > len(expected_donor_ids):
                    raise DeterministicScaleError("resume shard has excess N10 donor attempts")
                accepted_positions = [
                    index for index, result in enumerate(results) if result.status == "accepted"
                ]
                if accepted_positions and accepted_positions != [len(results) - 1]:
                    raise DeterministicScaleError(
                        "resume shard continued N10 scheduling after an accepted donor"
                    )
                if not accepted_positions and len(results) != len(expected_donor_ids):
                    raise DeterministicScaleError(
                        "resume shard truncated deterministic N10 donor attempts"
                    )

        for result in results:
            if (
                result.family_id != str(rule_config.family_id)
                or result.family_id != rule_id
                or result.polarity is not _RULE_POLARITY[rule_id]
            ):
                raise DeterministicScaleError("resume shard rule family/polarity mismatch")
            if rule_id == "n10_nearby_theorem" and result.status == "no_donor":
                no_donor_source_ids = (source.theorem_id,)
                if (
                    result.source_theorem_ids != no_donor_source_ids
                    or result.donor_theorem_id is not None
                    or result.seed != _seed(config, rule_id, no_donor_source_ids)
                    or result.attempt is not None
                    or result.draft_results
                    or result.failure is not None
                ):
                    raise DeterministicScaleError("resume shard empty-donor N10 lineage mismatch")
                continue
            source_ids = (
                (source.theorem_id,)
                if rule_id != "n10_nearby_theorem"
                else tuple(sorted((source.theorem_id, result.donor_theorem_id or "")))
            )
            if "" in source_ids or result.source_theorem_ids != source_ids:
                raise DeterministicScaleError("resume shard rule source lineage mismatch")
            if result.seed != _seed(config, rule_id, source_ids):
                raise DeterministicScaleError("resume shard rule seed mismatch")
            lineage_sources = tuple(theorem_by_id[theorem_id] for theorem_id in source_ids)
            lineage_source_representations = tuple(
                representation_by_theorem[theorem_id] for theorem_id in source_ids
            )
            replay_sources, replay_source_representations = _role_ordered_replay_inputs(
                rule_id=rule_id,
                primary=source,
                primary_representation=source_representation,
                donor_theorem_id=result.donor_theorem_id,
                theorem_by_id=theorem_by_id,
                representation_by_theorem=representation_by_theorem,
            )
            if result.attempt is not None:
                attempt = result.attempt
                try:
                    verify_transformation_attempt_id(attempt)
                except ValueError as exc:
                    raise DeterministicScaleError(
                        "resume shard transformation-attempt ID mismatch"
                    ) from exc
                if (
                    attempt.family_id != result.family_id
                    or attempt.rule_id != rule_id
                    or attempt.rule_version != str(rule_config.rule_version)
                    or attempt.source_theorem_ids != source_ids
                    or attempt.source_representation_ids
                    != tuple(record.representation_id for record in lineage_source_representations)
                    or attempt.context_id != source.context_id
                    or attempt.registry_hash != loaded_registry.registry_hash
                    or attempt.generation_config_hash != loaded_registry.registry_hash
                    or attempt.seed != result.seed
                ):
                    raise DeterministicScaleError(
                        "resume shard transformation-attempt lineage mismatch"
                    )
                persisted_draft_ids = tuple(
                    sorted(
                        draft_result.persistent_draft_id for draft_result in result.draft_results
                    )
                )
                if attempt.draft_ids != persisted_draft_ids:
                    raise DeterministicScaleError(
                        "resume shard attempt draft IDs differ from nested outcomes"
                    )
            if result.status != "cap_skipped":
                _validate_replayed_generation(
                    result=result,
                    source=source,
                    sources=replay_sources,
                    source_representations=replay_source_representations,
                    loaded_registry=loaded_registry,
                    positive_runtime=positive_runtime,
                    negative_runtime=negative_runtime,
                    pair_rule=pair_rule,
                )

            for draft_result in result.draft_results:
                if draft_result.status != "accepted":
                    if (
                        draft_result.failure is None
                        or draft_result.failure.draft_id != draft_result.persistent_draft_id
                    ):
                        raise DeterministicScaleError(
                            "resume shard quarantine failure/draft lineage mismatch"
                        )
                    if draft_result.draft is not None:
                        try:
                            verify_variant_draft_id(draft_result.draft)
                        except ValueError as exc:
                            raise DeterministicScaleError(
                                "resume shard quarantined draft ID mismatch"
                            ) from exc
                        if (
                            draft_result.draft.rule_id != rule_id
                            or draft_result.draft.family_id != result.family_id
                            or draft_result.draft.generation_config_hash
                            != loaded_registry.registry_hash
                            or draft_result.draft.seed != result.seed
                            or draft_result.draft.source_theorem_ids != source_ids
                            or draft_result.draft.source_representation_ids
                            != tuple(
                                record.representation_id
                                for record in lineage_source_representations
                            )
                        ):
                            raise DeterministicScaleError(
                                "resume shard quarantined draft lineage mismatch"
                            )
                    elif (
                        result.attempt is None
                        or draft_result.persistent_draft_id not in result.attempt.draft_ids
                    ):
                        raise DeterministicScaleError(
                            "redacted protected draft is not bound by its attempt"
                        )
                    continue

                draft = draft_result.draft
                candidate = draft_result.candidate_theorem
                candidate_representation = draft_result.candidate_representation
                audit = draft_result.audit
                variant = draft_result.variant
                pair = draft_result.pair
                assert draft is not None
                assert candidate is not None
                assert candidate_representation is not None
                assert audit is not None
                assert variant is not None
                assert pair is not None
                if result.attempt is None:
                    raise DeterministicScaleError("accepted resume draft lacks its attempt")
                try:
                    verify_variant_draft_id(draft)
                    verify_transformation_audit_id(audit)
                    verify_deterministic_variant_id(variant)
                except ValueError as exc:
                    raise DeterministicScaleError(
                        "resume shard accepted draft/audit/variant ID mismatch"
                    ) from exc
                if (
                    draft.rule_id != rule_id
                    or draft.family_id != result.family_id
                    or draft.generation_config_hash != loaded_registry.registry_hash
                    or draft.seed != result.seed
                    or draft.source_theorem_ids != source_ids
                    or draft.source_representation_ids
                    != tuple(record.representation_id for record in lineage_source_representations)
                    or candidate.inline_elaboration_source is not None
                    or candidate.parent_theorem_ids != source_ids
                    or candidate.proof_stripped_declaration != draft.candidate_code
                    or candidate.statement_content_hash != draft.candidate_code_hash
                    or candidate.context_id != source.context_id
                    or candidate.metadata.get("run_spec_hash") != run_spec_hash
                    or candidate.metadata.get("source_index") != expected_index
                ):
                    raise DeterministicScaleError(
                        "resume shard accepted candidate lineage mismatch"
                    )
                validation_request_hash = candidate.metadata.get("validation_request_hash")
                inline_context_sha256 = candidate.metadata.get("inline_context_sha256")
                if (
                    not isinstance(validation_request_hash, str)
                    or not isinstance(inline_context_sha256, str)
                    or len(validation_request_hash) != 64
                    or len(inline_context_sha256) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for value in (validation_request_hash, inline_context_sha256)
                        for character in value
                    )
                ):
                    raise DeterministicScaleError(
                        "resume shard candidate lacks bound validation/inline-context hashes"
                    )
                expected_candidate = build_derived_theorem_record(
                    draft=draft,
                    sources=lineage_sources,
                    primary_source_id=source.theorem_id,
                    elaboration_status=candidate.elaboration_status,
                    elaboration_diagnostics=candidate.elaboration_diagnostics,
                    inline_elaboration_source=draft.candidate_code,
                    metadata={
                        "run_spec_hash": run_spec_hash,
                        "scale_profile_id": config.profile_id,
                        "source_index": expected_index,
                        "validation_request_hash": validation_request_hash,
                        "inline_context_sha256": inline_context_sha256,
                        "inline_context_persisted": False,
                    },
                ).model_copy(update={"inline_elaboration_source": None})
                if candidate != expected_candidate:
                    raise DeterministicScaleError(
                        "resume shard accepted theorem differs from deterministic lineage"
                    )
                expected_representation_id = make_id(
                    REPRESENTATION_PREFIX,
                    {
                        "theorem_id": candidate.theorem_id,
                        "normalization_version": NORMALIZATION_VERSION,
                    },
                )
                if (
                    candidate_representation.representation_id != expected_representation_id
                    or candidate_representation.theorem_id != candidate.theorem_id
                    or candidate_representation.context_id != candidate.context_id
                    or candidate_representation.raw_proof_stripped
                    != candidate.proof_stripped_declaration
                    or candidate_representation.normalization_version != NORMALIZATION_VERSION
                    or candidate_representation.content_hash
                    != _representation_payload_hash(candidate_representation)
                ):
                    raise DeterministicScaleError(
                        "resume shard candidate representation identity/content mismatch"
                    )
                lineage_violations = check_deterministic_variant_lineage(
                    variant,
                    draft,
                    audit,
                    result.attempt,
                )
                if lineage_violations:
                    raise DeterministicScaleError(
                        "resume shard deterministic variant lineage mismatch: "
                        + ",".join(lineage_violations)
                    )
                expected_variant = build_deterministic_variant_record(
                    attempt=result.attempt,
                    draft=draft,
                    audit=audit,
                    candidate=candidate,
                    candidate_representation=candidate_representation,
                    polarity=result.polarity,
                    metadata={
                        "run_spec_hash": run_spec_hash,
                        "scale_profile_id": config.profile_id,
                        "source_index": expected_index,
                    },
                )
                if variant != expected_variant:
                    raise DeterministicScaleError(
                        "resume shard accepted variant differs from deterministic projection"
                    )
                candidate_for_pair_check = candidate.model_copy(
                    update={"inline_elaboration_source": draft.candidate_code}
                )
                expected_pair = build_deterministic_pair_record(
                    source=source,
                    candidate=candidate_for_pair_check,
                    draft=draft,
                    audit=audit,
                    all_sources=lineage_sources,
                    metadata={
                        "run_spec_hash": run_spec_hash,
                        "scale_profile_id": config.profile_id,
                        "source_index": expected_index,
                    },
                )
                if pair != expected_pair or check_pair_groups(pair, source, candidate):
                    raise DeterministicScaleError(
                        "resume shard accepted pair identity/split lineage mismatch"
                    )


def _n10_donor_index(
    sources: Sequence[TheoremRecord],
) -> tuple[dict[str, tuple[TheoremRecord, ...]], dict[str, tuple[str, ...]]]:
    buckets: dict[str, list[TheoremRecord]] = defaultdict(list)
    keys_by_theorem: dict[str, tuple[str, ...]] = {}
    for theorem in sources:
        keys = nearby_theorem_bucket_keys(theorem)
        keys_by_theorem[theorem.theorem_id] = keys
        for key in keys:
            buckets[key].append(theorem)
    return (
        {
            key: tuple(sorted(values, key=lambda theorem: theorem.theorem_id))
            for key, values in buckets.items()
        },
        keys_by_theorem,
    )


def _donors_for(
    primary: TheoremRecord,
    *,
    buckets: Mapping[str, tuple[TheoremRecord, ...]],
    keys_by_theorem: Mapping[str, tuple[str, ...]],
    config: DeterministicScaleConfig,
) -> tuple[TheoremRecord, ...]:
    candidates = {
        donor.theorem_id: donor
        for key in keys_by_theorem.get(primary.theorem_id, ())
        for donor in buckets.get(key, ())
        if donor.theorem_id != primary.theorem_id
        and not (set(primary.root_ancestry_ids) & set(donor.root_ancestry_ids))
        and donor.context_id == primary.context_id
    }
    ordered = sorted(
        candidates.values(),
        key=lambda donor: (
            hash_canonical(
                {
                    "schema": "deterministic_scale_n10_donor_order_v1",
                    "base_seed": config.base_seed,
                    "primary_theorem_id": primary.theorem_id,
                    "donor_theorem_id": donor.theorem_id,
                }
            ),
            donor.theorem_id,
        ),
    )
    return tuple(ordered[: config.max_n10_donor_attempts_per_primary])


def _build_source_shard(
    *,
    backend: LeanInteractBackend,
    loaded_registry: LoadedTransformationRegistry,
    positive_runtime: object,
    negative_runtime: object,
    pair_rule: object | None,
    benchmark: ActiveBenchmarkRegistry,
    config: DeterministicScaleConfig,
    run_spec_hash: str,
    source_index: int,
    theorem: TheoremRecord,
    representation: RepresentationRecord | None,
    representation_by_theorem: Mapping[str, RepresentationRecord],
    donors: Sequence[TheoremRecord],
    project_dir: Path,
    import_header: str,
    raw_response_dir: Path,
    state: _AdmissionState,
) -> ScaleSourceShard:
    preflight = _source_failure(
        theorem,
        representation,
        config=config,
        benchmark=benchmark,
    )
    if preflight is not None or representation is None:
        return ScaleSourceShard(
            run_spec_hash=run_spec_hash,
            source_index=source_index,
            source_theorem_id=theorem.theorem_id,
            source_representation_id=(
                None if representation is None else representation.representation_id
            ),
            source_status="ineligible",
            source_failure=preflight
            or _failure(
                stage="source_preflight",
                code="source_representation_missing",
                detail="no source representation",
                source_ids=(theorem.theorem_id,),
            ),
        )

    results: list[ScaleRuleResult] = []
    for rule_id in config.active_rule_ids:
        polarity = _RULE_POLARITY[rule_id]
        if rule_id != "n10_nearby_theorem":
            runtime = positive_runtime if polarity == Polarity.POSITIVE else negative_runtime
            results.append(
                _execute_rule(
                    backend=backend,
                    loaded_registry=loaded_registry,
                    unary_runtime=runtime,
                    pair_rule=None,
                    benchmark=benchmark,
                    config=config,
                    run_spec_hash=run_spec_hash,
                    source_index=source_index,
                    primary=theorem,
                    primary_representation=representation,
                    sources=(theorem,),
                    source_representations=(representation,),
                    rule_id=rule_id,
                    polarity=polarity,
                    project_dir=project_dir,
                    import_header=import_header,
                    raw_response_dir=raw_response_dir,
                    state=state,
                )
            )
            continue
        if not donors:
            results.append(
                ScaleRuleResult(
                    status="no_donor",
                    rule_id=rule_id,
                    family_id=rule_id,
                    polarity=polarity,
                    seed=_seed(config, rule_id, (theorem.theorem_id,)),
                    source_theorem_ids=(theorem.theorem_id,),
                )
            )
            continue
        accepted = False
        for donor in donors:
            donor_representation = representation_by_theorem.get(donor.theorem_id)
            donor_failure = _source_failure(
                donor,
                donor_representation,
                config=config,
                benchmark=benchmark,
            )
            if donor_failure is not None or donor_representation is None:
                continue
            outcome = _execute_rule(
                backend=backend,
                loaded_registry=loaded_registry,
                unary_runtime=None,
                pair_rule=pair_rule,
                benchmark=benchmark,
                config=config,
                run_spec_hash=run_spec_hash,
                source_index=source_index,
                primary=theorem,
                primary_representation=representation,
                sources=(theorem, donor),
                source_representations=(representation, donor_representation),
                rule_id=rule_id,
                polarity=polarity,
                project_dir=project_dir,
                import_header=import_header,
                raw_response_dir=raw_response_dir,
                state=state,
            )
            results.append(outcome)
            if outcome.status == "accepted":
                accepted = True
                break
        if not accepted and not any(result.rule_id == rule_id for result in results):
            results.append(
                ScaleRuleResult(
                    status="no_donor",
                    rule_id=rule_id,
                    family_id=rule_id,
                    polarity=polarity,
                    seed=_seed(config, rule_id, (theorem.theorem_id,)),
                    source_theorem_ids=(theorem.theorem_id,),
                )
            )
    return ScaleSourceShard(
        run_spec_hash=run_spec_hash,
        source_index=source_index,
        source_theorem_id=theorem.theorem_id,
        source_representation_id=representation.representation_id,
        source_status="eligible",
        rule_results=tuple(results),
    )


def _run_spec_payload(data: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in data.items() if key != "run_spec_hash"}


def _shard_set_spec_payload(data: Mapping[str, object]) -> dict[str, object]:
    """Common semantic spec shared by every independently executed shard."""

    excluded = {
        "run_spec_hash",
        "shard_set_spec_hash",
        "shard_index",
        "selected_source_theorem_ids",
    }
    return {key: value for key, value in data.items() if key not in excluded}


def _build_run_spec(
    *,
    theorem_path: Path,
    representation_path: Path,
    source_inventory_manifest_path: Path,
    theorem_upstream_manifest_path: Path,
    representation_upstream_manifest_path: Path,
    loaded_config: LoadedConfig[DeterministicScaleConfig],
    loaded_registry: LoadedTransformationRegistry,
    benchmark: ActiveBenchmarkRegistry,
    context_id: str,
    context_sha256: str,
    project_dir: Path,
    project_revision: str,
    project_tree_hash: str,
    code: CodeState,
    source_universe_ids: tuple[str, ...],
    source_shard_assignments: tuple[int, ...],
    selected_ids: tuple[str, ...],
    max_sources: int | None,
    shard_count: int,
    shard_index: int,
) -> DeterministicScaleRunSpec:
    data: dict[str, object] = {
        "schema_version": 2,
        "artifact_kind": "deterministic_scale_run_spec",
        "theorem_input_path": str(theorem_path.resolve()),
        "theorem_input_sha256": hash_file(theorem_path),
        "representation_input_path": str(representation_path.resolve()),
        "representation_input_sha256": hash_file(representation_path),
        "source_inventory_manifest_path": str(source_inventory_manifest_path.resolve()),
        "source_inventory_manifest_sha256": hash_file(source_inventory_manifest_path),
        "theorem_upstream_manifest_path": str(theorem_upstream_manifest_path.resolve()),
        "theorem_upstream_manifest_sha256": hash_file(theorem_upstream_manifest_path),
        "representation_upstream_manifest_path": str(
            representation_upstream_manifest_path.resolve()
        ),
        "representation_upstream_manifest_sha256": hash_file(representation_upstream_manifest_path),
        "config_path": str(loaded_config.path.resolve()),
        "config_hash": loaded_config.config_hash,
        "registry_hash": loaded_registry.registry_hash,
        "benchmark_manifest_path": str(benchmark.manifest_path.resolve()),
        "benchmark_manifest_sha256": hash_file(benchmark.manifest_path),
        "context_id": context_id,
        "context_record_sha256": context_sha256,
        "project_dir": str(project_dir.resolve()),
        "project_revision": project_revision,
        "project_tree_hash": project_tree_hash,
        "code": code,
        "shard_assignment_scheme": "root_component_greedy_v1",
        "shard_count": shard_count,
        "shard_index": shard_index,
        "source_universe_theorem_ids": source_universe_ids,
        "source_shard_assignments": source_shard_assignments,
        "selected_source_theorem_ids": selected_ids,
        "max_sources": max_sources,
    }
    shard_set_semantic = DeterministicScaleRunSpec.model_validate(
        {
            "run_spec_hash": "0" * 64,
            "shard_set_spec_hash": "0" * 64,
            **data,
        }
    ).model_dump(mode="json")
    shard_set_spec_hash = hash_canonical(_shard_set_spec_payload(shard_set_semantic))
    semantic = DeterministicScaleRunSpec.model_validate(
        {
            "run_spec_hash": "0" * 64,
            "shard_set_spec_hash": shard_set_spec_hash,
            **data,
        }
    ).model_dump(mode="json")
    run_spec_hash = hash_canonical(_run_spec_payload(semantic))
    return DeterministicScaleRunSpec.model_validate(
        {
            "run_spec_hash": run_spec_hash,
            "shard_set_spec_hash": shard_set_spec_hash,
            **data,
        }
    )


def _tree_hash(root: Path, pattern: str) -> tuple[int, str]:
    entries = tuple(
        (str(path.relative_to(root)), hash_file(path))
        for path in sorted(root.rglob(pattern))
        if path.is_file()
    )
    return len(entries), hash_canonical(entries)


def _project_records(
    shards: Sequence[ScaleSourceShard],
) -> dict[str, tuple[StrictModel, ...]]:
    attempts: list[TransformationAttempt] = []
    drafts: list[VariantDraft] = []
    candidates: list[TheoremRecord] = []
    representations: list[RepresentationRecord] = []
    audits: list[TransformationAudit] = []
    variants: list[VariantRecord] = []
    pairs: list[PairRecord] = []
    quarantine: list[ScaleQuarantineRecord] = []
    failures: list[ScaleFailure] = []
    accepted_draft_ids: set[str] = set()
    for shard in shards:
        if shard.source_failure is not None:
            failures.append(shard.source_failure)
        for rule in shard.rule_results:
            if rule.attempt is not None:
                attempts.append(rule.attempt)
            if rule.failure is not None:
                failures.append(rule.failure)
            for result in rule.draft_results:
                if result.status == "accepted":
                    assert result.draft is not None
                    assert result.candidate_theorem is not None
                    assert result.candidate_representation is not None
                    assert result.audit is not None
                    assert result.variant is not None
                    assert result.pair is not None
                    drafts.append(result.draft)
                    candidates.append(result.candidate_theorem)
                    representations.append(result.candidate_representation)
                    audits.append(result.audit)
                    variants.append(result.variant)
                    pairs.append(result.pair)
                    accepted_draft_ids.add(result.draft.draft_id)
                else:
                    assert result.failure is not None
                    quarantine.append(
                        ScaleQuarantineRecord(
                            status=result.status,
                            source_theorem_ids=rule.source_theorem_ids,
                            rule_id=rule.rule_id,
                            family_id=rule.family_id,
                            polarity=rule.polarity,
                            draft_id=result.persistent_draft_id,
                            candidate_code_hash=result.persistent_candidate_code_hash,
                            failure=result.failure,
                            candidate_content_redacted=(
                                result.status == "protected_benchmark_overlap"
                            ),
                        )
                    )
                if result.failure is not None:
                    failures.append(result.failure)
    projected_ids = {draft.draft_id for draft in drafts}
    if projected_ids != accepted_draft_ids:
        raise DeterministicScaleError(
            "canonical draft projection contains a non-accepted or missing accepted draft"
        )
    if any(candidate.inline_elaboration_source is not None for candidate in candidates):
        raise DeterministicScaleError(
            "canonical candidate theorem partition contains inline source context"
        )
    if any(record.draft_id in accepted_draft_ids for record in quarantine):
        raise DeterministicScaleError("quarantine partition overlaps accepted canonical draft IDs")
    return {
        "attempts": tuple(attempts),
        "drafts": tuple(drafts),
        "candidate_theorems": tuple(candidates),
        "candidate_representations": tuple(representations),
        "audits": tuple(audits),
        "variants": tuple(variants),
        "pairs": tuple(pairs),
        "quarantine": tuple(quarantine),
        "failures": tuple(failures),
    }


def _write_partitions(
    output_dir: Path,
    records: Mapping[str, Sequence[StrictModel]],
) -> tuple[dict[str, Path], dict[str, str]]:
    partition_dir = output_dir / "partitions"
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for name, values in records.items():
        path = partition_dir / f"{name}.jsonl"
        payload = b"".join(
            canonical_json_bytes(value.model_dump(mode="json")) + b"\n" for value in values
        )
        paths[name] = path
        hashes[name] = _write_new_atomic(path, payload)
    return paths, hashes


def _validate_unique_inputs(
    theorems: Sequence[TheoremRecord],
    representations: Sequence[RepresentationRecord],
) -> dict[str, RepresentationRecord]:
    theorem_ids = [theorem.theorem_id for theorem in theorems]
    if len(theorem_ids) != len(set(theorem_ids)):
        raise DeterministicScaleError("theorem input contains duplicate theorem IDs")
    theorem_by_id = {theorem.theorem_id: theorem for theorem in theorems}
    for theorem in theorems:
        source_locator = theorem.source_record_id or theorem.source_record
        if source_locator is None or theorem.declaration_full_name is None:
            raise DeterministicScaleError(
                f"theorem lacks reconstructible extraction provenance: {theorem.theorem_id}"
            )
        expected_theorem_id = make_id(
            THEOREM_PREFIX,
            {
                "source": theorem.source,
                "revision": theorem.source_revision,
                "context_id": theorem.context_id,
                "source_record_id": source_locator,
                "declaration_ordinal": theorem.declaration_ordinal,
                "extracted_signature_hash": theorem.statement_content_hash,
                "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
            },
        )
        if theorem.theorem_id != expected_theorem_id:
            raise DeterministicScaleError(
                f"theorem extraction identity mismatch for {theorem.theorem_id}"
            )
        expected_ancestry_id = make_source_ancestry_id(
            source=theorem.source,
            revision=theorem.source_revision,
            source_locator=source_locator,
            declaration_full_name=theorem.declaration_full_name,
        )
        if (
            theorem.ancestry_id != expected_ancestry_id
            or theorem.root_ancestry_ids != (expected_ancestry_id,)
            or theorem.parent_theorem_ids
        ):
            raise DeterministicScaleError(
                f"theorem extraction ancestry mismatch for {theorem.theorem_id}"
            )
    by_theorem: dict[str, RepresentationRecord] = {}
    for representation in representations:
        if representation.theorem_id not in theorem_by_id:
            raise DeterministicScaleError(
                "representation input references theorem outside the theorem inventory: "
                f"{representation.theorem_id}"
            )
        if representation.theorem_id in by_theorem:
            raise DeterministicScaleError(
                f"duplicate representation for theorem {representation.theorem_id}"
            )
        expected_representation_id = make_id(
            REPRESENTATION_PREFIX,
            {
                "theorem_id": representation.theorem_id,
                "normalization_version": NORMALIZATION_VERSION,
            },
        )
        if representation.representation_id != expected_representation_id:
            raise DeterministicScaleError(
                f"representation ID mismatch for {representation.theorem_id}"
            )
        if representation.content_hash != _representation_payload_hash(representation):
            raise DeterministicScaleError(
                f"representation content hash mismatch for {representation.theorem_id}"
            )
        by_theorem[representation.theorem_id] = representation
    return by_theorem


def freeze_deterministic_scale_source_inventory(
    *,
    repo_root: Path,
    theorem_jsonl: Path,
    representation_jsonl: Path,
    theorem_upstream_manifest: Path,
    representation_upstream_manifest: Path,
    manifest_path: Path,
) -> ScaleSourceInventoryArtifacts:
    """Validate and atomically bind exact extracted theorem/repr_v3 partitions."""

    theorem_path = theorem_jsonl.resolve()
    representation_path = representation_jsonl.resolve()
    theorem_manifest_path = theorem_upstream_manifest.resolve()
    representation_manifest_path = representation_upstream_manifest.resolve()
    output_path = manifest_path.resolve()
    theorems = _load_jsonl(theorem_path, TheoremRecord, wrapper_key="theorem")
    representations = _load_jsonl(representation_path, RepresentationRecord)
    representation_by_theorem = _validate_unique_inputs(theorems, representations)
    theorem_ids = {theorem.theorem_id for theorem in theorems}
    if not theorem_ids:
        raise DeterministicScaleError("cannot freeze an empty theorem inventory")
    if theorem_ids != set(representation_by_theorem):
        raise DeterministicScaleError(
            "source inventory freeze requires exactly one repr_v3 record per theorem"
        )
    contexts = {
        *(theorem.context_id for theorem in theorems),
        *(representation.context_id for representation in representations),
    }
    if len(contexts) != 1:
        raise DeterministicScaleError(
            "source inventory freeze requires one homogeneous Lean context"
        )
    context_id = next(iter(contexts))
    theorem_manifest_raw = _load_json_document(theorem_manifest_path)
    theorem_manifest_kind: Literal["output_manifest", "gate3_selection_v2"]
    if theorem_manifest_raw.get("selection_version") == "gate3_equal_source_hash_order_v1":
        theorem_manifest_kind = "gate3_selection_v2"
    elif theorem_manifest_raw.get("stage") == DataStage.ELABORATED.value:
        theorem_manifest_kind = "output_manifest"
    else:
        raise DeterministicScaleError(
            "theorem upstream manifest is neither the canonical Gate-3 selection "
            "manifest nor an elaborated OutputManifest"
        )
    manifest = ScaleSourceInventoryManifest(
        context_id=context_id,
        theorem_partition=ScaleInventoryPartitionBinding(
            path=Path(os.path.relpath(theorem_path, start=output_path.parent)).as_posix(),
            sha256=hash_file(theorem_path),
            record_count=len(theorems),
        ),
        representation_partition=ScaleInventoryPartitionBinding(
            path=Path(os.path.relpath(representation_path, start=output_path.parent)).as_posix(),
            sha256=hash_file(representation_path),
            record_count=len(representations),
        ),
        theorem_upstream_manifest=ScaleUpstreamManifestBinding(
            path=Path(os.path.relpath(theorem_manifest_path, start=output_path.parent)).as_posix(),
            sha256=hash_file(theorem_manifest_path),
            manifest_kind=theorem_manifest_kind,
        ),
        representation_upstream_manifest=ScaleUpstreamManifestBinding(
            path=Path(
                os.path.relpath(representation_manifest_path, start=output_path.parent)
            ).as_posix(),
            sha256=hash_file(representation_manifest_path),
            manifest_kind="output_manifest",
        ),
    )
    _validate_trusted_upstream_manifests(
        manifest,
        inventory_manifest_path=output_path,
        theorem_path=theorem_path,
        representation_path=representation_path,
        theorems=theorems,
        representations=representations,
        repo_root=repo_root.resolve(),
    )
    manifest_sha256 = _write_new_atomic(output_path, _canonical_model_bytes(manifest))
    _load_source_inventory_manifest(
        output_path,
        theorem_path=theorem_path,
        representation_path=representation_path,
    )
    return ScaleSourceInventoryArtifacts(
        manifest_path=output_path,
        manifest_sha256=manifest_sha256,
        theorem_count=len(theorems),
        representation_count=len(representations),
    )


def run_deterministic_scale_materialization(
    *,
    paths: RepoPaths,
    theorem_jsonl: Path,
    representation_jsonl: Path,
    source_inventory_manifest: Path,
    project_dir: Path,
    output_dir: Path,
    config_path: Path | None = None,
    max_sources: int | None = None,
    shard_count: int = 1,
    shard_index: int = 0,
    resume: bool = False,
    fast_resume: bool = False,
    memory_hard_limit_mb: int | None = None,
) -> DeterministicScaleArtifacts:
    """Materialize deterministic v1 candidates from immutable repr_v3 inputs."""

    if max_sources is not None and max_sources < 1:
        raise ValueError("max_sources must be positive")
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError("shard_index must satisfy 0 <= shard_index < shard_count")
    if fast_resume:
        raise DeterministicScaleError(
            "fast resume is retired: hash-chain receipts are operational integrity "
            "metadata, not scientific verification; use exact --resume Lean replay"
        )
    theorem_path = theorem_jsonl.resolve()
    representation_path = representation_jsonl.resolve()
    inventory_manifest_path = source_inventory_manifest.resolve()
    project = project_dir.resolve()
    output = output_dir.resolve()
    resolved_config = (config_path or paths.root / _DEFAULT_CONFIG).resolve()
    loaded_config = load_config(resolved_config, DeterministicScaleConfig)
    config = loaded_config.config
    if config.normalization_version != NORMALIZATION_VERSION:
        raise DeterministicScaleError(
            f"scale config requires {config.normalization_version}, code builds "
            f"{NORMALIZATION_VERSION}"
        )

    inventory_manifest = _load_source_inventory_manifest(
        inventory_manifest_path,
        theorem_path=theorem_path,
        representation_path=representation_path,
    )
    theorems = _load_jsonl(theorem_path, TheoremRecord, wrapper_key="theorem")
    representations = _load_jsonl(representation_path, RepresentationRecord)
    _validate_inventory_record_bindings(
        inventory_manifest,
        theorems=theorems,
        representations=representations,
    )
    representation_by_theorem = _validate_unique_inputs(theorems, representations)
    theorem_upstream_manifest_path, representation_upstream_manifest_path = (
        _validate_trusted_upstream_manifests(
            inventory_manifest,
            inventory_manifest_path=inventory_manifest_path,
            theorem_path=theorem_path,
            representation_path=representation_path,
            theorems=theorems,
            representations=representations,
            repo_root=paths.root,
        )
    )
    ordered = tuple(
        sorted(theorems, key=lambda theorem: _selection_key(config.base_seed, theorem.theorem_id))
    )
    universe = ordered if max_sources is None else ordered[:max_sources]
    if not universe:
        raise DeterministicScaleError("theorem input selected zero source records")
    _validate_shard_execution_policy(config, shard_count=shard_count)
    source_shard_assignments = _root_component_shard_assignments(
        universe,
        shard_count=shard_count,
    )
    selected_entries = tuple(
        (global_index, theorem)
        for global_index, (theorem, assignment) in enumerate(
            zip(universe, source_shard_assignments, strict=True)
        )
        if assignment == shard_index
    )
    selected = tuple(theorem for _, theorem in selected_entries)
    source_universe_ids = tuple(theorem.theorem_id for theorem in universe)
    selected_ids = tuple(theorem.theorem_id for theorem in selected)
    if not selected:
        raise DeterministicScaleError("deterministic shard assignment selected zero sources")

    # Import lazily to avoid making transformation-domain code depend on the
    # Typer command module during import.
    from leanfaith.cli.pipeline import build_mathlib_context
    from leanfaith.cli.transformations import _validate_authorization
    from leanfaith.lean.project_registry import check_project_revision, load_project_registry

    authorization_path, _, _, _ = _validate_authorization(paths.root)
    benchmark = load_active_benchmark_registry(
        repo_root=paths.root,
        authorization_path=authorization_path,
    )
    context, context_sha256 = build_mathlib_context(paths, project)
    contexts = {theorem.context_id for theorem in selected}
    if contexts != {context.context_id}:
        raise DeterministicScaleError(
            "selected theorem input context does not equal the pinned mathlib context"
        )
    registry = load_project_registry(paths)
    spec = registry.get("mathlib")
    if spec is None:
        raise DeterministicScaleError("project registry has no mathlib entry")
    project_revision = check_project_revision(spec, project)
    clean_revision, project_tree_hash = _clean_project_tree_hash(project)
    if clean_revision != project_revision:
        raise DeterministicScaleError(
            "clean project identity differs from the pinned project revision"
        )
    import_header = context.header_text

    loaded_registry = load_transformation_registry(paths.root)
    positive_registration = build_positive_rule_runtime(loaded_registry)
    negative_registration = build_negative_rule_runtime(loaded_registry)
    executable = (
        set(positive_registration.registered_rule_ids)
        | set(negative_registration.registered_rule_ids)
        | set(negative_registration.pair_aware_rule_ids)
    )
    if set(config.active_rule_ids) - executable:
        raise DeterministicScaleError("configured active rule is not executable")
    pair_rule = (
        negative_registration.pair_rules[0]
        if "n10_nearby_theorem" in config.active_rule_ids
        else None
    )

    code = collect_code_state(paths.root)
    run_spec = _build_run_spec(
        theorem_path=theorem_path,
        representation_path=representation_path,
        source_inventory_manifest_path=inventory_manifest_path,
        theorem_upstream_manifest_path=theorem_upstream_manifest_path,
        representation_upstream_manifest_path=representation_upstream_manifest_path,
        loaded_config=loaded_config,
        loaded_registry=loaded_registry,
        benchmark=benchmark,
        context_id=context.context_id,
        context_sha256=context_sha256,
        project_dir=project,
        project_revision=project_revision,
        project_tree_hash=project_tree_hash,
        code=code,
        source_universe_ids=source_universe_ids,
        source_shard_assignments=source_shard_assignments,
        selected_ids=selected_ids,
        max_sources=max_sources,
        shard_count=shard_count,
        shard_index=shard_index,
    )
    run_spec_path = output / "run_spec.json"
    journal_dir = output / "journal"
    receipt_dir = output / "journal_receipts"
    manifest_path = output / "manifest.json"
    replay_audit_path = output / "full_lean_replay_audit.json"

    with _run_lock(output):
        preexisting = tuple(path for path in output.iterdir() if path.name != "run.lock")
        if preexisting and not resume:
            raise DeterministicScaleError(
                f"output directory is nonempty; pass --resume only for the same run: {output}"
            )
        _write_new_atomic(run_spec_path, _canonical_model_bytes(run_spec))
        _clean_project_tree_hash(
            project,
            expected_revision=run_spec.project_revision,
            expected_tree_hash=run_spec.project_tree_hash,
        )

        expected_shards = tuple(
            _source_shard_path(journal_dir, global_index, theorem.theorem_id)
            for global_index, theorem in selected_entries
        )
        expected_receipts = tuple(
            _journal_receipt_path(receipt_dir, shard_path) for shard_path in expected_shards
        )
        existing_flags = tuple(path.exists() for path in expected_shards)
        complete_run_opened_for_replay = resume and all(existing_flags)
        if replay_audit_path.exists() and not complete_run_opened_for_replay:
            raise DeterministicScaleError(
                "a Lean-replay audit exists for an incomplete/non-resume "
                "execution; archive the output and restart from immutable inputs"
            )
        saw_gap = False
        for index, exists in enumerate(existing_flags):
            if not exists:
                saw_gap = True
            elif saw_gap:
                raise DeterministicScaleError(
                    "resume journal is not a contiguous source-order prefix; "
                    f"unexpected later shard {expected_shards[index]}"
                )
        unexpected = sorted(
            path for path in journal_dir.glob("*.json") if path not in set(expected_shards)
        )
        if unexpected:
            raise DeterministicScaleError(
                f"journal contains shards outside current immutable run spec: {unexpected[:3]}"
            )
        unexpected_receipts = sorted(
            path for path in receipt_dir.glob("*.json") if path not in set(expected_receipts)
        )
        if unexpected_receipts:
            raise DeterministicScaleError(
                "journal receipt directory contains records outside the immutable run spec: "
                f"{unexpected_receipts[:3]}"
            )
        orphan_receipts = [
            receipt_path
            for shard_path, receipt_path in zip(
                expected_shards,
                expected_receipts,
                strict=True,
            )
            if receipt_path.exists() and not shard_path.exists()
        ]
        if orphan_receipts:
            raise DeterministicScaleError(
                f"journal receipt exists without its immutable shard: {orphan_receipts[:3]}"
            )

        eligible_for_n10 = tuple(
            theorem
            for theorem in selected
            if _source_failure(
                theorem,
                representation_by_theorem.get(theorem.theorem_id),
                config=config,
                benchmark=benchmark,
            )
            is None
        )
        donor_buckets, donor_keys = _n10_donor_index(eligible_for_n10)

        settings = BackendSettings(
            project_dir=project,
            context_fingerprint=context.context_fingerprint,
            environment_schema_version=load_environment_lock(paths).environment_schema_version,
            raw_response_dir=output / "raw_lean_responses",
            memory_hard_limit_mb=memory_hard_limit_mb,
        )
        state = _AdmissionState(
            root_counts=Counter(),
            family_root_counts=Counter(),
            family_counts=Counter(),
            candidate_keys=set(),
            variant_ids=set(),
            pair_ids=set(),
        )
        shards: list[ScaleSourceShard] = []
        backend: LeanInteractBackend | None = None

        def require_backend() -> LeanInteractBackend:
            nonlocal backend
            if backend is None:
                LeanInteractBackend.prepare_environment(settings)
                backend = LeanInteractBackend(replace(settings, environment_is_prepared=True))
            return backend

        previous_receipt_hash = "0" * 64
        try:
            for local_index, path in enumerate(expected_shards):
                if not path.exists():
                    break
                persisted_shard = _load_source_shard(path)
                global_index, theorem = selected_entries[local_index]
                receipt_path = expected_receipts[local_index]
                receipt: ScaleJournalReceipt | None = None
                if receipt_path.exists():
                    receipt = _load_journal_receipt(
                        path=receipt_path,
                        shard=persisted_shard,
                        shard_path=path,
                        previous_receipt_hash=previous_receipt_hash,
                    )
                    previous_receipt_hash = receipt.receipt_hash
                expected_donors = (
                    _donors_for(
                        theorem,
                        buckets=donor_buckets,
                        keys_by_theorem=donor_keys,
                        config=config,
                    )
                    if "n10_nearby_theorem" in config.active_rule_ids
                    else ()
                )
                _validate_resume_shard(
                    shard=persisted_shard,
                    expected_index=global_index,
                    source=theorem,
                    source_representation=representation_by_theorem.get(theorem.theorem_id),
                    theorem_by_id={record.theorem_id: record for record in selected},
                    representation_by_theorem=representation_by_theorem,
                    expected_donors=expected_donors,
                    config=config,
                    loaded_registry=loaded_registry,
                    positive_runtime=positive_registration.runtime,
                    negative_runtime=negative_registration.runtime,
                    pair_rule=pair_rule,
                    benchmark=benchmark,
                    run_spec_hash=run_spec.run_spec_hash,
                )
                rebuilt_shard = _build_source_shard(
                    backend=require_backend(),
                    loaded_registry=loaded_registry,
                    positive_runtime=positive_registration.runtime,
                    negative_runtime=negative_registration.runtime,
                    pair_rule=pair_rule,
                    benchmark=benchmark,
                    config=config,
                    run_spec_hash=run_spec.run_spec_hash,
                    source_index=global_index,
                    theorem=theorem,
                    representation=representation_by_theorem.get(theorem.theorem_id),
                    representation_by_theorem=representation_by_theorem,
                    donors=expected_donors,
                    project_dir=project,
                    import_header=import_header,
                    raw_response_dir=settings.raw_response_dir,
                    state=state,
                )
                _require_exact_resume_replay(persisted_shard, rebuilt_shard)
                shards.append(rebuilt_shard)
                if receipt is None:
                    receipt = _build_journal_receipt(
                        shard=rebuilt_shard,
                        shard_path=path,
                        previous_receipt_hash=previous_receipt_hash,
                    )
                    _write_new_atomic(receipt_path, _canonical_model_bytes(receipt))
                    previous_receipt_hash = receipt.receipt_hash

            for local_index in range(len(shards), len(selected)):
                global_index, theorem = selected_entries[local_index]
                donors = (
                    _donors_for(
                        theorem,
                        buckets=donor_buckets,
                        keys_by_theorem=donor_keys,
                        config=config,
                    )
                    if "n10_nearby_theorem" in config.active_rule_ids
                    else ()
                )
                shard = _build_source_shard(
                    backend=require_backend(),
                    loaded_registry=loaded_registry,
                    positive_runtime=positive_registration.runtime,
                    negative_runtime=negative_registration.runtime,
                    pair_rule=pair_rule,
                    benchmark=benchmark,
                    config=config,
                    run_spec_hash=run_spec.run_spec_hash,
                    source_index=global_index,
                    theorem=theorem,
                    representation=representation_by_theorem.get(theorem.theorem_id),
                    representation_by_theorem=representation_by_theorem,
                    donors=donors,
                    project_dir=project,
                    import_header=import_header,
                    raw_response_dir=settings.raw_response_dir,
                    state=state,
                )
                shard_path = expected_shards[local_index]
                _write_new_atomic(shard_path, _canonical_model_bytes(shard))
                receipt = _build_journal_receipt(
                    shard=shard,
                    shard_path=shard_path,
                    previous_receipt_hash=previous_receipt_hash,
                )
                _write_new_atomic(
                    expected_receipts[local_index],
                    _canonical_model_bytes(receipt),
                )
                previous_receipt_hash = receipt.receipt_hash
                shards.append(shard)
        finally:
            if backend is not None:
                backend.close()

        _clean_project_tree_hash(
            project,
            expected_revision=run_spec.project_revision,
            expected_tree_hash=run_spec.project_tree_hash,
        )
        projected = _project_records(shards)
        partition_paths, partition_hashes = _write_partitions(output, projected)
        status_counts = Counter(result.status for shard in shards for result in shard.rule_results)
        journal_count, journal_tree_hash = _tree_hash(journal_dir, "*.json")
        receipt_count, receipt_tree_hash = _tree_hash(receipt_dir, "*.json")
        raw_count, raw_tree_hash = _tree_hash(output / "raw_lean_responses", "*")
        if complete_run_opened_for_replay:
            replay_audit = _build_lean_replay_audit(
                run_spec=run_spec,
                run_spec_path=run_spec_path,
                replayed_source_ids=selected_ids,
                journal_tree_hash=journal_tree_hash,
                partition_sha256=partition_hashes,
                created_at=config.record_timestamp_utc,
            )
            _write_new_atomic(
                replay_audit_path,
                _canonical_model_bytes(replay_audit),
            )
        manifest = DeterministicScaleManifest(
            run_spec_hash=run_spec.run_spec_hash,
            run_spec_sha256=hash_file(run_spec_path),
            shard_set_spec_hash=run_spec.shard_set_spec_hash,
            shard_count=run_spec.shard_count,
            shard_index=run_spec.shard_index,
            source_universe_count=len(run_spec.source_universe_theorem_ids),
            source_assignment_sha256=hash_canonical(
                {
                    "source_universe_theorem_ids": run_spec.source_universe_theorem_ids,
                    "source_shard_assignments": run_spec.source_shard_assignments,
                }
            ),
            source_count=len(shards),
            eligible_source_count=sum(shard.source_status == "eligible" for shard in shards),
            ineligible_source_count=sum(shard.source_status == "ineligible" for shard in shards),
            journal_shard_count=journal_count,
            rule_status_counts=dict(sorted(status_counts.items())),
            family_accepted_counts=dict(sorted(state.family_counts.items())),
            record_counts={name: len(values) for name, values in projected.items()},
            partition_sha256=dict(sorted(partition_hashes.items())),
            journal_tree_hash=journal_tree_hash,
            journal_receipt_count=receipt_count,
            journal_receipt_tree_hash=receipt_tree_hash,
            journal_chain_tip=previous_receipt_hash,
            raw_response_file_count=raw_count,
            raw_response_tree_hash=raw_tree_hash,
            created_at=config.record_timestamp_utc,
        )
        manifest_sha256 = _write_new_atomic(
            manifest_path,
            _canonical_model_bytes(manifest),
        )
        return DeterministicScaleArtifacts(
            output_dir=output,
            run_spec_path=run_spec_path,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            partition_paths=partition_paths,
        )


__all__ = [
    "DeterministicScaleArtifacts",
    "DeterministicScaleConfig",
    "DeterministicScaleError",
    "DeterministicScaleManifest",
    "DeterministicScaleRunSpec",
    "ScaleDraftResult",
    "ScaleFailure",
    "ScaleJournalReceipt",
    "ScaleLeanReplayAudit",
    "ScaleRuleResult",
    "ScaleSourceInventoryArtifacts",
    "ScaleSourceInventoryManifest",
    "ScaleSourceShard",
    "freeze_deterministic_scale_source_inventory",
    "run_deterministic_scale_materialization",
]
