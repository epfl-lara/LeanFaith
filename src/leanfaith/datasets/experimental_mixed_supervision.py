"""Immutable mixed proxy corpus for bounded, experimental-only learning.

This module combines *mechanical intentions* from deterministic depth-two
composition with one-orientation, one-family Codex audit opinions.  Neither is
a resolved F1 label.  The resulting records are deliberately usable only by
callers that opt in to smoke training, learning-curve work, or proxy
diagnostics.  They are barred from scientific training, model selection,
calibration, evaluation, gate credit, and release claims.

The module performs no Lean or model calls.  Its LF-022 adapter accepts only
objects already returned by ``verify_completed_lf022_codex_audit``.  Its
composition adapter accepts the receipt-bound exporter record rather than
reconstructing chain lineage itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import ActiveBenchmarkRegistry
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditInput,
    LF022VerifiedCodexAudit,
    LF022VerifiedCodexAuditJudgment,
)
from leanfaith.generation.lf022_lean_check import LF022LeanCheckRecord
from leanfaith.representations.views import normalize_headless, signature_near_dup_hash
from leanfaith.schemas.manifest import CodeState, collect_code_state
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_chain import (
    DeterministicCompositionChainManifest,
    DeterministicCompositionChainRecord,
)
from leanfaith.transforms.composition_full_launcher import (
    CompositionFullLaunchSpec,
    CompositionFullReceipt,
)
from leanfaith.transforms.composition_receipt_export import (
    DeterministicCompositionExportRecord,
    DeterministicCompositionReceiptExportManifest,
)
from leanfaith.transforms.composition_unique_pairs import (
    DeterministicCompositionUniquePairManifest,
    DeterministicCompositionUniquePairRecord,
)
from leanfaith.transforms.v2_d0_materializer import V2D0MaterializationResult
from leanfaith.transforms.v2_e2_materializer import V2E2MaterializationResult

if TYPE_CHECKING:
    from leanfaith.datasets.experimental_first_hop_projection import (
        ExperimentalFirstHopProjectionRecord,
    )

_HEX64 = r"^[0-9a-f]{64}$"
_MIXED_CANDIDATE_ID = r"^experimental_mixed_candidate:[0-9a-f]{64}$"
_MIXED_SIGNAL_ID = r"^experimental_proxy_signal:[0-9a-f]{64}$"
_MIXED_RECORD_ID = r"^experimental_mixed_pair:[0-9a-f]{64}$"
_MIXED_EXCLUSION_ID = r"^experimental_mixed_exclusion:[0-9a-f]{64}$"
_MIXED_DATASET_ID = r"^experimental_mixed_supervision:[0-9a-f]{64}$"
_SPLIT_COMPONENT_ID = r"^split-component:[0-9a-f]{64}$"
_OUTPUT_FILES = frozenset(
    {
        "records.jsonl",
        "split_assignments.jsonl",
        "excluded.jsonl",
        "summary.json",
        "manifest.json",
    }
)

PseudoTarget = Literal["same_claim", "not_same_claim"]
ExperimentalSplit = Literal["train", "validation", "test"]
ExperimentalPurpose = Literal["learning_curve", "proxy_diagnostics", "smoke_training"]
SignalKind = Literal[
    "deterministic_first_hop_e2",
    "deterministic_first_hop_d0",
    "deterministic_composition_p_to_p",
    "deterministic_composition_p_to_n",
    "codex_single_judge_ab",
]
PseudoTargetBasis = Literal[
    "deterministic_first_hop_intention",
    "deterministic_composition_intention",
    "codex_single_judge_ab_proxy",
    "agreeing_mixed_proxy",
]
ExclusionReason = Literal[
    "composition_cycle",
    "composition_mixed_intention",
    "codex_ambiguous",
    "codex_uncertain",
    "codex_expert_review",
    "codex_incoherent",
    "headless_normalization_failed",
    "benchmark_overlap",
    "conflicting_proxy_targets",
]
PartitionStatus = Literal["included", "omitted_not_bound", "omitted_pending_receipt"]


class ExperimentalMixedSupervisionError(ValueError):
    """An adapter, policy, artifact, or replay invariant failed closed."""


def _without_id(payload: Mapping[str, object], field: str) -> dict[str, object]:
    output = dict(payload)
    output.pop(field, None)
    return output


def _sorted_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


class ExperimentalMixedInputBinding(StrictModel):
    """Exact path/hash/size binding for one already-verified input artifact."""

    partition: Literal["first_hop", "lf022_codex", "composition", "policy"]
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=0, strict=True)


class ExperimentalMixedSupervisionConfig(StrictModel):
    """Frozen policy for a retain-all, headless-only proxy corpus."""

    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1)
    selection_seed: str = Field(min_length=1)
    model_input_profile: Literal["headless_only_v1"] = "headless_only_v1"
    retain_all_clean_pairs: Literal[True] = True
    first_hop_partition: PartitionStatus
    lf022_codex_partition: PartitionStatus
    composition_partition: PartitionStatus
    train_percent: int = Field(default=80, ge=1, le=98, strict=True)
    validation_percent: int = Field(default=10, ge=1, le=98, strict=True)
    test_percent: int = Field(default=10, ge=1, le=98, strict=True)

    @model_validator(mode="after")
    def _split_reconciles(self) -> Self:
        if self.train_percent + self.validation_percent + self.test_percent != 100:
            raise ValueError("experimental split percentages must sum to 100")
        if self.first_hop_partition == "omitted_pending_receipt":
            raise ValueError("first-hop omission cannot be attributed to a composition receipt")
        if self.lf022_codex_partition == "omitted_pending_receipt":
            raise ValueError("LF-022 omission cannot be attributed to a composition receipt")
        if self.composition_partition == "omitted_not_bound":
            raise ValueError("composition omission must explicitly state omitted_pending_receipt")
        if not any(
            value == "included"
            for value in (
                self.first_hop_partition,
                self.lf022_codex_partition,
                self.composition_partition,
            )
        ):
            raise ValueError("at least one proxy partition must be included")
        return self


class ExperimentalHeadlessStatementView(StrictModel):
    """One honest headless-only model view.

    ``lean_check_type_pp`` is retained solely as mechanical audit metadata.  It
    is intentionally not named or serialized as ``signature_explicit``.
    """

    normalization_method: Literal["normalized_headless_text_v1"] = "normalized_headless_text_v1"
    context_id: str = Field(min_length=1)
    theorem_ids: tuple[str, ...] = ()
    representation_ids: tuple[str, ...] = ()
    origin_record_ids: tuple[str, ...] = Field(min_length=1)
    headless: str = Field(min_length=1)
    headless_sha256: str = Field(pattern=_HEX64)
    alpha_identity_fingerprint: str | None = Field(default=None, pattern=_HEX64)
    lean_check_type_pp: str | None = None

    @field_validator("theorem_ids", "representation_ids", "origin_record_ids")
    @classmethod
    def _identities_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != _sorted_unique(value):
            raise ValueError("view identities must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _view_is_coherent(self) -> Self:
        if not self.headless.strip() or "\x00" in self.headless:
            raise ValueError("headless view must be safe nonempty text")
        if self.headless_sha256 != sha256_hex(self.headless.encode("utf-8")):
            raise ValueError("headless_sha256 differs from the model-visible text")
        if self.lean_check_type_pp is not None and (
            not self.lean_check_type_pp.strip() or "\x00" in self.lean_check_type_pp
        ):
            raise ValueError("lean_check_type_pp must be safe nonempty text")
        return self


class ExperimentalProxySignal(StrictModel):
    """One explicit proxy signal; never a semantic, gold, or silver label."""

    schema_version: Literal[1] = 1
    signal_id: str = Field(pattern=_MIXED_SIGNAL_ID)
    signal_kind: SignalKind
    pseudo_target: PseudoTarget
    provenance_ids: tuple[str, ...] = Field(min_length=1)
    family_ids: tuple[str, ...] = ()
    chain_sequences: tuple[str, ...] = ()
    intended_relation: Literal["equivalent", "near_miss"] | None = None
    first_hop_unique_pair_id: str | None = None
    first_hop_observation_id: str | None = None
    first_hop_result_id: str | None = None
    first_hop_root_binding_id: str | None = None
    certificate_kind: str | None = None
    certificate_sha256: str | None = Field(default=None, pattern=_HEX64)
    certificate_sha256s: tuple[str, ...] = ()
    composition_export_record_id: str | None = None
    audit_item_id: str | None = None
    lean_check_id: str | None = None
    pair_id: str | None = None
    variant_id: str | None = None
    proposer_family_id: str | None = None
    judge_model: str | None = None
    judge_reasoning_effort: str | None = None
    judge_orientation: Literal["AB"] | None = None
    judge_relation: str | None = None
    judge_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    final_message_sha256: str | None = Field(default=None, pattern=_HEX64)
    parsed_response_sha256: str | None = Field(default=None, pattern=_HEX64)
    response_artifact_set_sha256: str | None = Field(default=None, pattern=_HEX64)
    human_label: Literal[False] = False
    semantic_label: Literal[False] = False
    silver_record: Literal[False] = False

    @field_validator("provenance_ids", "family_ids", "chain_sequences", "certificate_sha256s")
    @classmethod
    def _tuples_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != _sorted_unique(value):
            raise ValueError("signal tuple fields must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _signal_is_coherent(self) -> Self:
        expected = "experimental_proxy_signal:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "signal_id")
        )
        if self.signal_id != expected:
            raise ValueError("signal_id does not match canonical signal content")
        is_codex = self.signal_kind == "codex_single_judge_ab"
        codex_values = (
            self.audit_item_id,
            self.lean_check_id,
            self.pair_id,
            self.variant_id,
            self.proposer_family_id,
            self.judge_model,
            self.judge_reasoning_effort,
            self.judge_orientation,
            self.judge_relation,
            self.judge_confidence,
            self.final_message_sha256,
            self.parsed_response_sha256,
            self.response_artifact_set_sha256,
        )
        if is_codex:
            if any(value is None for value in codex_values):
                raise ValueError("Codex signal requires its complete verified audit binding")
            deterministic_values = (
                self.first_hop_unique_pair_id,
                self.first_hop_observation_id,
                self.first_hop_result_id,
                self.first_hop_root_binding_id,
                self.certificate_kind,
                self.certificate_sha256,
                self.composition_export_record_id,
            )
            if any(value is not None for value in deterministic_values):
                raise ValueError("Codex signal cannot claim deterministic provenance")
            if self.certificate_sha256s:
                raise ValueError("Codex signal cannot claim deterministic certificates")
            if self.intended_relation is not None or self.chain_sequences:
                raise ValueError("Codex opinion cannot be stored as a transform intention")
        elif self.signal_kind.startswith("deterministic_composition_"):
            if any(value is not None for value in codex_values):
                raise ValueError("deterministic signal cannot claim Codex audit fields")
            if any(
                value is not None
                for value in (
                    self.first_hop_unique_pair_id,
                    self.first_hop_observation_id,
                    self.first_hop_result_id,
                    self.first_hop_root_binding_id,
                )
            ):
                raise ValueError("composition signal cannot claim first-hop locator fields")
            if self.composition_export_record_id is None:
                raise ValueError("composition signal lacks its export record")
            if self.intended_relation is None or not self.chain_sequences:
                raise ValueError("composition signal lacks intention or chain sequence")
            if not self.certificate_sha256s:
                raise ValueError("composition signal lacks its replay-bound certificates")
            expected_kind = (
                "deterministic_composition_p_to_p"
                if self.pseudo_target == "same_claim"
                else "deterministic_composition_p_to_n"
            )
            if self.signal_kind != expected_kind:
                raise ValueError("composition signal kind and pseudo-target differ")
            expected_relation = "equivalent" if self.pseudo_target == "same_claim" else "near_miss"
            if self.intended_relation != expected_relation:
                raise ValueError("composition intention and pseudo-target differ")
        else:
            if any(value is not None for value in codex_values):
                raise ValueError("first-hop signal cannot claim Codex audit fields")
            if self.composition_export_record_id is not None or self.chain_sequences:
                raise ValueError("first-hop signal cannot claim composition provenance")
            if any(
                value is None
                for value in (
                    self.first_hop_unique_pair_id,
                    self.first_hop_observation_id,
                    self.first_hop_result_id,
                    self.first_hop_root_binding_id,
                )
            ):
                raise ValueError("first-hop signal lacks its full-audit locators")
            expected_kind = (
                "deterministic_first_hop_e2"
                if self.pseudo_target == "same_claim"
                else "deterministic_first_hop_d0"
            )
            if self.signal_kind != expected_kind:
                raise ValueError("first-hop signal kind and target differ")
            expected_relation = "equivalent" if self.pseudo_target == "same_claim" else "near_miss"
            if self.intended_relation != expected_relation:
                raise ValueError("first-hop intention and target differ")
            if self.pseudo_target == "same_claim":
                if self.certificate_kind is None or self.certificate_sha256 is None:
                    raise ValueError("first-hop E2 signal requires its certificate")
                if self.certificate_sha256s != (self.certificate_sha256,):
                    raise ValueError("first-hop E2 certificate set does not reconcile")
            elif self.certificate_kind is not None or self.certificate_sha256 is not None:
                raise ValueError("first-hop D0 signal cannot claim a positive certificate")
            elif self.certificate_sha256s:
                raise ValueError("first-hop D0 signal cannot claim positive certificates")
        return self


class ExperimentalMixedCandidate(StrictModel):
    """Normalized adapter output prior to pair-level deduplication."""

    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=_MIXED_CANDIDATE_ID)
    exact_pair_key: str = Field(pattern=_HEX64)
    pseudo_target: PseudoTarget
    signal: ExperimentalProxySignal
    split_group_ids: tuple[str, ...] = Field(min_length=1)
    source_datasets: tuple[str, ...] = Field(min_length=1)
    source: ExperimentalHeadlessStatementView
    candidate: ExperimentalHeadlessStatementView
    private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool
    denylist_checked: Literal[True]
    denylist_registry_sha256: str = Field(pattern=_HEX64)
    quality_tier: Literal["provisional"] = "provisional"
    semantic_label_id: None = None
    machine_proxy_only: Literal[True] = True

    @field_validator("split_group_ids", "source_datasets")
    @classmethod
    def _candidate_tuples_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != _sorted_unique(value):
            raise ValueError("candidate tuple fields must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _candidate_is_coherent(self) -> Self:
        expected_pair = _exact_pair_key(self.source, self.candidate)
        if self.exact_pair_key != expected_pair:
            raise ValueError("exact_pair_key differs from context and headless views")
        if self.source.context_id != self.candidate.context_id:
            raise ValueError("source and candidate contexts differ")
        if self.signal.pseudo_target != self.pseudo_target:
            raise ValueError("candidate target differs from its signal")
        required_content_groups = {
            _content_group(self.source),
            _content_group(self.candidate),
        }
        if not required_content_groups.issubset(self.split_group_ids):
            raise ValueError("candidate split groups omit source/candidate content union")
        if self.private_source_content and self.external_transmission_allowed:
            raise ValueError("private source content cannot be externally transmissible")
        if self.release_eligible and (
            self.private_source_content or not self.redistribution_allowed
        ):
            raise ValueError("release eligibility conflicts with source policy")
        expected = "experimental_mixed_candidate:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "candidate_id")
        )
        if self.candidate_id != expected:
            raise ValueError("candidate_id does not match canonical candidate content")
        return self


class ExperimentalMixedExclusion(StrictModel):
    """Machine-readable quarantine item with no target when one is unsafe."""

    schema_version: Literal[1] = 1
    exclusion_id: str = Field(pattern=_MIXED_EXCLUSION_ID)
    reason: ExclusionReason
    origin_ids: tuple[str, ...] = Field(min_length=1)
    signal_ids: tuple[str, ...] = ()
    exact_pair_key: str | None = Field(default=None, pattern=_HEX64)
    observed_pseudo_targets: tuple[PseudoTarget, ...] = ()
    semantic_label_id: None = None
    machine_proxy_only: Literal[True] = True
    scientific_training_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @field_validator("origin_ids", "signal_ids", "observed_pseudo_targets")
    @classmethod
    def _exclusion_tuples_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("exclusion tuple fields must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _exclusion_is_coherent(self) -> Self:
        if self.reason == "conflicting_proxy_targets":
            if self.exact_pair_key is None or self.observed_pseudo_targets != (
                "not_same_claim",
                "same_claim",
            ):
                raise ValueError("target conflict requires the exact pair and both targets")
            if len(self.signal_ids) < 2:
                raise ValueError("target conflict requires at least two signals")
        elif self.observed_pseudo_targets:
            raise ValueError("non-conflict exclusion cannot expose a pseudo-target")
        expected = "experimental_mixed_exclusion:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "exclusion_id")
        )
        if self.exclusion_id != expected:
            raise ValueError("exclusion_id does not match canonical exclusion content")
        return self


class ExperimentalMixedSupervisionRecord(StrictModel):
    """One deduplicated proxy pair with all agreeing provenance retained."""

    schema_version: Literal[2] = 2
    record_id: str = Field(pattern=_MIXED_RECORD_ID)
    dataset_profile_id: str = Field(min_length=1)
    exact_pair_key: str = Field(pattern=_HEX64)
    pseudo_target: PseudoTarget
    pseudo_target_basis: PseudoTargetBasis
    signals: tuple[ExperimentalProxySignal, ...] = Field(min_length=1)
    signal_kinds: tuple[SignalKind, ...] = Field(min_length=1)
    family_ids: tuple[str, ...] = ()
    split_group_ids: tuple[str, ...] = Field(min_length=1)
    split_component_id: str = Field(pattern=_SPLIT_COMPONENT_ID)
    split: ExperimentalSplit
    source_datasets: tuple[str, ...] = Field(min_length=1)
    source: ExperimentalHeadlessStatementView
    candidate: ExperimentalHeadlessStatementView
    private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool
    denylist_checked: Literal[True]
    denylist_registry_sha256: str = Field(pattern=_HEX64)
    model_input_profile: Literal["headless_only_v1"] = "headless_only_v1"
    quality_tier: Literal["provisional"] = "provisional"
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    human_label: Literal[False] = False
    silver_record: Literal[False] = False
    machine_supervision_only: Literal[True] = True
    experimental_training_eligible: Literal[True] = True
    scientific_training_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False
    release_claim_eligible: Literal[False] = False

    @field_validator("signal_kinds", "family_ids", "split_group_ids", "source_datasets")
    @classmethod
    def _record_tuples_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("record tuple fields must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _record_is_coherent(self) -> Self:
        if self.signals != tuple(sorted(self.signals, key=lambda item: item.signal_id)):
            raise ValueError("signals must be in canonical ID order")
        if len({item.signal_id for item in self.signals}) != len(self.signals):
            raise ValueError("record repeats a proxy signal")
        if {item.pseudo_target for item in self.signals} != {self.pseudo_target}:
            raise ValueError("record signals do not agree on one pseudo-target")
        if self.signal_kinds != _sorted_unique(tuple(item.signal_kind for item in self.signals)):
            raise ValueError("signal_kinds do not reconcile")
        expected_families = _sorted_unique(
            tuple(family for item in self.signals for family in item.family_ids)
        )
        if self.family_ids != expected_families:
            raise ValueError("family_ids do not reconcile")
        if self.pseudo_target_basis != _basis(self.signals):
            raise ValueError("pseudo_target_basis does not reconcile")
        if self.exact_pair_key != _exact_pair_key(self.source, self.candidate):
            raise ValueError("record pair key differs from its views")
        if self.source.context_id != self.candidate.context_id:
            raise ValueError("record source/candidate contexts differ")
        if self.private_source_content and self.external_transmission_allowed:
            raise ValueError("private mixed record cannot be externally transmissible")
        if self.release_eligible and (
            self.private_source_content or not self.redistribution_allowed
        ):
            raise ValueError("record release policy is incoherent")
        expected = "experimental_mixed_pair:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "record_id")
        )
        if self.record_id != expected:
            raise ValueError("record_id does not match canonical record content")
        return self


class ExperimentalMixedSplitAssignment(StrictModel):
    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=_MIXED_RECORD_ID)
    split_component_id: str = Field(pattern=_SPLIT_COMPONENT_ID)
    split_group_ids: tuple[str, ...] = Field(min_length=1)
    split: ExperimentalSplit
    pseudo_target: PseudoTarget


class ExperimentalMixedModelExample(StrictModel):
    """Only fields admitted to the immediate headless-only learner."""

    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=_MIXED_RECORD_ID)
    model_input_profile: Literal["headless_only_v1"] = "headless_only_v1"
    source_headless: str = Field(min_length=1)
    candidate_headless: str = Field(min_length=1)
    pseudo_target: PseudoTarget
    split: ExperimentalSplit


class ExperimentalMixedSupervisionSummary(StrictModel):
    schema_version: Literal[1] = 1
    dataset_id: str = Field(pattern=_MIXED_DATASET_ID)
    profile_id: str = Field(min_length=1)
    record_count: int = Field(gt=0, strict=True)
    signal_count: int = Field(gt=0, strict=True)
    exclusion_count: int = Field(ge=0, strict=True)
    component_count: int = Field(gt=0, strict=True)
    counts_by_pseudo_target: dict[str, int]
    counts_by_basis: dict[str, int]
    counts_by_signal_kind: dict[str, int]
    counts_by_split: dict[str, int]
    counts_by_exclusion_reason: dict[str, int]
    semantic_label_count: Literal[0] = 0
    gold_label_count: Literal[0] = 0
    silver_record_count: Literal[0] = 0
    scientific_training_ready_count: Literal[0] = 0
    use_note: Literal["experimental proxy supervision only; not semantic ground truth"] = (
        "experimental proxy supervision only; not semantic ground truth"
    )


class ExperimentalMixedSupervisionManifest(StrictModel):
    """Content-addressed manifest for one exact mixed proxy artifact."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["experimental_mixed_supervision_corpus_v2"] = (
        "experimental_mixed_supervision_corpus_v2"
    )
    dataset_id: str = Field(pattern=_MIXED_DATASET_ID)
    profile_id: str = Field(min_length=1)
    config_hash: str = Field(pattern=_HEX64)
    config: ExperimentalMixedSupervisionConfig
    code: CodeState
    inputs: dict[str, ExperimentalMixedInputBinding]
    record_count: int = Field(gt=0, strict=True)
    signal_count: int = Field(gt=0, strict=True)
    exclusion_count: int = Field(ge=0, strict=True)
    component_count: int = Field(gt=0, strict=True)
    output_sha256: dict[str, str]
    required_opt_in_flag: Literal["--allow-experimental-mixed-supervision"] = (
        "--allow-experimental-mixed-supervision"
    )
    allowed_purposes: tuple[ExperimentalPurpose, ...] = (
        "learning_curve",
        "proxy_diagnostics",
        "smoke_training",
    )
    first_hop_partition: PartitionStatus
    lf022_codex_partition: PartitionStatus
    composition_partition: PartitionStatus
    model_input_profile: Literal["headless_only_v1"] = "headless_only_v1"
    semantic_labels_created: Literal[False] = False
    human_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    scientific_training_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False
    release_claim_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _manifest_is_coherent(self) -> Self:
        if self.profile_id != self.config.profile_id:
            raise ValueError("manifest profile differs from embedded config")
        if self.config_hash != hash_canonical(self.config.model_dump(mode="json")):
            raise ValueError("manifest config hash differs from embedded config")
        if self.model_input_profile != self.config.model_input_profile:
            raise ValueError("manifest model-input profile differs from config")
        if (
            self.first_hop_partition != self.config.first_hop_partition
            or self.lf022_codex_partition != self.config.lf022_codex_partition
            or self.composition_partition != self.config.composition_partition
        ):
            raise ValueError("manifest partition statuses differ from config")
        if self.code.git_dirty or self.code.code_tree_hash is None or self.code.untracked_files:
            raise ValueError("mixed corpus requires a clean, fully tracked code tree")
        if not self.inputs or list(self.inputs) != sorted(self.inputs):
            raise ValueError("manifest inputs must be nonempty and key-sorted")
        if self.allowed_purposes != tuple(sorted(set(self.allowed_purposes))):
            raise ValueError("allowed purposes must be sorted and unique")
        if set(self.output_sha256) != _OUTPUT_FILES - {"manifest.json"}:
            raise ValueError("manifest does not bind the exact non-manifest outputs")
        return self


class DeterministicCompositionReplayBinding(StrictModel):
    """Complete in-memory join needed before a receipt-export row is usable.

    The receipt export intentionally omits context, ancestry, model views, and
    per-chain certificates.  Supplying only an export row is therefore unsafe.
    This object binds those fields back to the audited unique-pair and chain
    records plus the exact theorem/representation records used by the model.
    """

    schema_version: Literal[2] = 2
    full_launch_spec: CompositionFullLaunchSpec
    full_launch_spec_artifact: ExperimentalMixedInputBinding
    full_receipt: CompositionFullReceipt
    full_receipt_artifact: ExperimentalMixedInputBinding
    full_status_artifact: ExperimentalMixedInputBinding
    export_manifest: DeterministicCompositionReceiptExportManifest
    export_manifest_artifact: ExperimentalMixedInputBinding
    export_partition: Literal["inventory", "cycles", "quarantine"]
    export_partition_artifact: ExperimentalMixedInputBinding
    export_line_number: int = Field(ge=1, strict=True)
    export_line_sha256: str = Field(pattern=_HEX64)
    chain_manifest: DeterministicCompositionChainManifest
    chain_manifest_artifact: ExperimentalMixedInputBinding
    chain_records_artifact: ExperimentalMixedInputBinding
    unique_pair_manifest: DeterministicCompositionUniquePairManifest
    unique_pair_manifest_artifact: ExperimentalMixedInputBinding
    unique_pair_records_artifact: ExperimentalMixedInputBinding
    unique_pair: DeterministicCompositionUniquePairRecord
    chains: tuple[DeterministicCompositionChainRecord, ...] = Field(min_length=1)
    source_theorem_artifacts: tuple[ExperimentalMixedInputBinding, ...] = Field(min_length=1)
    source_representation_artifacts: tuple[ExperimentalMixedInputBinding, ...] = Field(min_length=1)
    second_hop_result_artifacts: dict[str, ExperimentalMixedInputBinding]
    source_theorem: TheoremRecord
    source_representation: RepresentationRecord
    final_theorems: tuple[TheoremRecord, ...] = Field(min_length=1)
    final_representations: tuple[RepresentationRecord, ...] = Field(min_length=1)

    @field_validator("source_theorem_artifacts", "source_representation_artifacts")
    @classmethod
    def _source_artifacts_are_canonical(
        cls,
        value: tuple[ExperimentalMixedInputBinding, ...],
    ) -> tuple[ExperimentalMixedInputBinding, ...]:
        if value != tuple(sorted(value, key=lambda item: (item.sha256, item.path))):
            raise ValueError("composition source artifacts must be in canonical hash/path order")
        if len({item.sha256 for item in value}) != len(value):
            raise ValueError("composition source artifacts repeat a content hash")
        return value

    @model_validator(mode="after")
    def _binding_is_coherent(self) -> Self:
        artifact_bindings = (
            self.full_launch_spec_artifact,
            self.full_receipt_artifact,
            self.full_status_artifact,
            self.export_manifest_artifact,
            self.export_partition_artifact,
            self.chain_manifest_artifact,
            self.chain_records_artifact,
            self.unique_pair_manifest_artifact,
            self.unique_pair_records_artifact,
            *self.source_theorem_artifacts,
            *self.source_representation_artifacts,
            *self.second_hop_result_artifacts.values(),
        )
        if any(binding.partition != "composition" for binding in artifact_bindings):
            raise ValueError("all composition replay artifacts must use composition partition")
        if self.full_receipt.launch_id != self.full_launch_spec.launch_id:
            raise ValueError("composition receipt and launch spec differ")
        if (
            self.export_manifest.full_launch_id != self.full_launch_spec.launch_id
            or self.export_manifest.full_receipt_id != self.full_receipt.receipt_id
        ):
            raise ValueError("composition export differs from full launch/receipt")
        if self.chain_manifest.chain_set_id != self.export_manifest.input_chain_set_id:
            raise ValueError("composition chain manifest differs from export")
        if (
            self.unique_pair_manifest.unique_pair_set_id
            != self.export_manifest.input_unique_pair_set_id
            or self.unique_pair_manifest.input_chain_set_id != self.chain_manifest.chain_set_id
        ):
            raise ValueError("composition unique-pair manifest differs from export/chain")
        if self.chains != tuple(sorted(self.chains, key=lambda item: item.chain_id)):
            raise ValueError("chains must be in canonical identity order")
        if len({item.chain_id for item in self.chains}) != len(self.chains):
            raise ValueError("chains repeat an identity")
        if self.final_theorems != tuple(
            sorted(self.final_theorems, key=lambda item: item.theorem_id)
        ):
            raise ValueError("final_theorems must be in canonical identity order")
        if len({item.theorem_id for item in self.final_theorems}) != len(self.final_theorems):
            raise ValueError("final_theorems repeat an identity")
        if self.final_representations != tuple(
            sorted(self.final_representations, key=lambda item: item.representation_id)
        ):
            raise ValueError("final_representations must be in canonical identity order")
        if len({item.representation_id for item in self.final_representations}) != len(
            self.final_representations
        ):
            raise ValueError("final_representations repeat an identity")
        if self.export_manifest.input_chain_set_id != self.unique_pair.input_chain_set_id:
            raise ValueError("composition export/unique-pair chain sets differ")
        selected_root_ids = {item.second_hop_root_binding_id for item in self.chains}
        if set(self.second_hop_result_artifacts) != selected_root_ids:
            raise ValueError(
                "composition replay must bind exactly the selected second-hop result roots"
            )
        if {item.chain_id for item in self.chains} != set(self.unique_pair.chain_ids):
            raise ValueError("composition binding does not contain exactly the aliased chains")
        if self.source_theorem.theorem_id != self.unique_pair.original_source_theorem_id:
            raise ValueError("composition source theorem differs from the unique pair")
        if (
            self.source_representation.representation_id
            != self.unique_pair.original_source_representation_id
            or self.source_representation.theorem_id != self.source_theorem.theorem_id
            or self.source_representation.context_id != self.source_theorem.context_id
        ):
            raise ValueError("composition source representation join differs")
        if self.source_theorem.root_ancestry_ids != self.unique_pair.root_ancestry_ids:
            raise ValueError("composition source ancestry differs from the unique pair")
        if (
            self.source_theorem.statement_content_hash
            != self.unique_pair.source_statement_content_hash
            or self.source_representation.alpha_identity_fingerprint
            != self.unique_pair.source_alpha_identity_fingerprint
        ):
            raise ValueError("composition source content differs from the unique pair")
        final_theorems = {item.theorem_id: item for item in self.final_theorems}
        final_representations = {
            item.representation_id: item for item in self.final_representations
        }
        if set(final_theorems) != set(self.unique_pair.final_theorem_ids):
            raise ValueError("composition final theorem set differs from the unique pair")
        if set(final_representations) != set(self.unique_pair.final_representation_ids):
            raise ValueError("composition final representation set differs from the unique pair")
        final_headless: set[str] = set()
        for chain in self.chains:
            if (
                chain.context_id != self.unique_pair.context_id
                or chain.root_ancestry_ids != self.unique_pair.root_ancestry_ids
                or chain.original_source_theorem_id != self.unique_pair.original_source_theorem_id
                or chain.original_source_representation_id
                != self.unique_pair.original_source_representation_id
                or chain.final_candidate_code_hash != self.unique_pair.final_candidate_code_hash
                or chain.final_alpha_identity_fingerprint
                != self.unique_pair.final_alpha_identity_fingerprint
            ):
                raise ValueError("composition chain differs from unique-pair lineage")
            theorem = final_theorems.get(chain.final_theorem_id)
            representation = final_representations.get(chain.final_representation_id)
            if theorem is None or representation is None:
                raise ValueError("composition chain lacks its final theorem/view")
            if (
                representation.theorem_id != theorem.theorem_id
                or theorem.context_id != chain.context_id
                or representation.context_id != chain.context_id
                or theorem.root_ancestry_ids != chain.root_ancestry_ids
                or theorem.statement_content_hash != chain.final_candidate_code_hash
                or representation.alpha_identity_fingerprint
                != chain.final_alpha_identity_fingerprint
                or representation.headless is None
            ):
                raise ValueError("composition final theorem/view join differs from chain")
            normalized = normalize_headless(theorem.proof_stripped_declaration)
            if normalized is None or normalized != representation.headless:
                raise ValueError("composition final headless view is not replay-equivalent")
            final_headless.add(normalized)
        if len(final_headless) != 1:
            raise ValueError("aliased composition chains yield different model-visible views")
        source_headless = normalize_headless(self.source_theorem.proof_stripped_declaration)
        if (
            source_headless is None
            or self.source_representation.headless is None
            or source_headless != self.source_representation.headless
        ):
            raise ValueError("composition source headless view is not replay-equivalent")
        return self


@dataclass(frozen=True, slots=True)
class ExperimentalMixedAdapterResult:
    candidates: tuple[ExperimentalMixedCandidate, ...] = ()
    exclusions: tuple[ExperimentalMixedExclusion, ...] = ()


@dataclass(frozen=True, slots=True)
class ExperimentalMixedSupervisionArtifacts:
    output_dir: Path
    manifest_path: Path
    records_path: Path
    split_assignments_path: Path
    exclusions_path: Path
    summary_path: Path
    dataset_id: str
    record_count: int
    exclusion_count: int
    replayed: bool


def _headless_view(
    *,
    context_id: str,
    declaration: str,
    theorem_ids: Sequence[str] = (),
    representation_ids: Sequence[str] = (),
    origin_record_ids: Sequence[str],
    alpha_identity_fingerprint: str | None = None,
    lean_check_type_pp: str | None = None,
) -> ExperimentalHeadlessStatementView | None:
    headless = normalize_headless(declaration)
    if headless is None:
        return None
    return ExperimentalHeadlessStatementView(
        context_id=context_id,
        theorem_ids=_sorted_unique(theorem_ids),
        representation_ids=_sorted_unique(representation_ids),
        origin_record_ids=_sorted_unique(origin_record_ids),
        headless=headless,
        headless_sha256=sha256_hex(headless.encode("utf-8")),
        alpha_identity_fingerprint=alpha_identity_fingerprint,
        lean_check_type_pp=lean_check_type_pp,
    )


def _exact_pair_key(
    source: ExperimentalHeadlessStatementView,
    candidate: ExperimentalHeadlessStatementView,
) -> str:
    return hash_canonical(
        {
            "schema": "experimental_mixed_exact_pair_v1",
            "context_id": source.context_id,
            "source_headless_sha256": source.headless_sha256,
            "candidate_headless_sha256": candidate.headless_sha256,
        }
    )


def _content_group(view: ExperimentalHeadlessStatementView) -> str:
    """Connect any pair in which the same model-visible statement reappears."""

    return "statement-content:" + view.headless_sha256


def _registry_sha256(registry: ActiveBenchmarkRegistry) -> str:
    path = _regular_file(registry.active_registry_path)
    return hash_file(path)


def _text_is_protected(registry: ActiveBenchmarkRegistry, *values: str) -> bool:
    for value in values:
        if registry.index.contains_lean(value):
            return True
        if registry.index.contains_representation(signature_near_dup_hash(value)):
            return True
    return False


def _theorem_view_is_protected(
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
    values = [theorem.proof_stripped_declaration]
    for name in ("headless", "signature_pp", "signature_explicit"):
        value = getattr(representation, name)
        if value is not None:
            values.append(value)
    if _text_is_protected(registry, *values):
        return True
    alpha = representation.alpha_identity_fingerprint
    return alpha is not None and registry.index.contains_representation(alpha)


def _first_hop_view_is_protected(
    registry: ActiveBenchmarkRegistry,
    *,
    theorem_id: str,
    representation_id: str,
    headless: str,
    alpha_identity_fingerprint: str,
) -> bool:
    return (
        registry.index.contains_row_id(theorem_id)
        or registry.index.contains_row_id(representation_id)
        or _text_is_protected(registry, headless)
        or registry.index.contains_representation(alpha_identity_fingerprint)
    )


def _make_signal(**values: object) -> ExperimentalProxySignal:
    payload = {"signal_id": "experimental_proxy_signal:" + "0" * 64, **values}
    provisional = ExperimentalProxySignal.model_construct(_fields_set=None, **payload)
    payload["signal_id"] = "experimental_proxy_signal:" + hash_canonical(
        _without_id(provisional.model_dump(mode="json"), "signal_id")
    )
    return ExperimentalProxySignal.model_validate(payload)


def _make_candidate(**values: object) -> ExperimentalMixedCandidate:
    payload = {"candidate_id": "experimental_mixed_candidate:" + "0" * 64, **values}
    provisional = ExperimentalMixedCandidate.model_construct(_fields_set=None, **payload)
    payload["candidate_id"] = "experimental_mixed_candidate:" + hash_canonical(
        _without_id(provisional.model_dump(mode="json"), "candidate_id")
    )
    return ExperimentalMixedCandidate.model_validate(payload)


def _make_exclusion(**values: object) -> ExperimentalMixedExclusion:
    payload = {"exclusion_id": "experimental_mixed_exclusion:" + "0" * 64, **values}
    provisional = ExperimentalMixedExclusion.model_construct(_fields_set=None, **payload)
    payload["exclusion_id"] = "experimental_mixed_exclusion:" + hash_canonical(
        _without_id(provisional.model_dump(mode="json"), "exclusion_id")
    )
    return ExperimentalMixedExclusion.model_validate(payload)


def adapt_selectable_first_hop_projection(
    record: ExperimentalFirstHopProjectionRecord,
    *,
    benchmark_registry: ActiveBenchmarkRegistry,
) -> ExperimentalMixedAdapterResult:
    """Project one already-verified selectable first-hop row into the mixed schema."""

    # Revalidation is deliberate: callers cannot bypass the projection schema
    # with ``model_construct`` and then receive a mixed-corpus proxy target.
    from leanfaith.datasets.experimental_first_hop_projection import (
        ExperimentalFirstHopProjectionRecord,
    )

    try:
        record = ExperimentalFirstHopProjectionRecord.model_validate(record.model_dump(mode="json"))
    except ValueError as exc:
        raise ExperimentalMixedSupervisionError(
            f"invalid first-hop projection record: {exc}"
        ) from exc
    if (
        record.selection_status != "selectable"
        or not record.experimental_mixed_input_eligible
        or record.exclusion_reasons
        or record.evidence_tier not in {"E2", "D0"}
        or record.pseudo_target is None
        or record.source is None
        or record.candidate is None
        or not record.benchmark_screened_source
        or not record.benchmark_screened_candidate
    ):
        raise ExperimentalMixedSupervisionError(
            "first-hop projection is not a clean selectable benchmark-screened row"
        )
    source_record = record.source
    candidate_record = record.candidate
    source = ExperimentalHeadlessStatementView(
        context_id=source_record.context_id,
        theorem_ids=(source_record.theorem_id,),
        representation_ids=(source_record.representation_id,),
        origin_record_ids=_sorted_unique(
            (record.projection_record_id, record.unique_pair_id, source_record.theorem_id)
        ),
        headless=source_record.normalized_headless_text_v1,
        headless_sha256=source_record.normalized_headless_sha256,
        alpha_identity_fingerprint=source_record.alpha_identity_fingerprint,
    )
    candidate = ExperimentalHeadlessStatementView(
        context_id=candidate_record.context_id,
        theorem_ids=(candidate_record.theorem_id,),
        representation_ids=(candidate_record.representation_id,),
        origin_record_ids=_sorted_unique(
            (
                record.projection_record_id,
                record.unique_pair_id,
                candidate_record.theorem_id,
            )
        ),
        headless=candidate_record.normalized_headless_text_v1,
        headless_sha256=candidate_record.normalized_headless_sha256,
        alpha_identity_fingerprint=candidate_record.alpha_identity_fingerprint,
    )
    if _first_hop_view_is_protected(
        benchmark_registry,
        theorem_id=source_record.theorem_id,
        representation_id=source_record.representation_id,
        headless=source.headless,
        alpha_identity_fingerprint=source_record.alpha_identity_fingerprint,
    ) or _first_hop_view_is_protected(
        benchmark_registry,
        theorem_id=candidate_record.theorem_id,
        representation_id=candidate_record.representation_id,
        headless=candidate.headless,
        alpha_identity_fingerprint=candidate_record.alpha_identity_fingerprint,
    ):
        return ExperimentalMixedAdapterResult(
            exclusions=(
                _make_exclusion(
                    reason="benchmark_overlap",
                    origin_ids=(record.projection_record_id, record.unique_pair_id),
                ),
            )
        )
    is_positive = record.evidence_tier == "E2"
    signal = _make_signal(
        signal_kind=("deterministic_first_hop_e2" if is_positive else "deterministic_first_hop_d0"),
        pseudo_target=record.pseudo_target,
        provenance_ids=_sorted_unique(
            (
                record.projection_record_id,
                record.unique_pair_id,
                record.pair_id,
                *record.observation_ids,
            )
        ),
        family_ids=record.family_ids,
        intended_relation="equivalent" if is_positive else "near_miss",
        first_hop_unique_pair_id=record.unique_pair_id,
        first_hop_observation_id=record.selected_observation_id,
        first_hop_result_id=record.result_id,
        first_hop_root_binding_id=record.root_binding_id,
        certificate_kind=record.certificate_kind,
        certificate_sha256=record.certificate_sha256,
        certificate_sha256s=(
            (record.certificate_sha256,) if record.certificate_sha256 is not None else ()
        ),
    )
    normalized = _make_candidate(
        exact_pair_key=_exact_pair_key(source, candidate),
        pseudo_target=record.pseudo_target,
        signal=signal,
        split_group_ids=_sorted_unique(
            (
                *record.source_root_ancestry_ids,
                _content_group(source),
                _content_group(candidate),
            )
        ),
        source_datasets=(record.source_category,),
        source=source,
        candidate=candidate,
        private_source_content=record.private_source_content,
        redistribution_allowed=record.redistribution_allowed,
        external_transmission_allowed=record.external_transmission_allowed,
        release_eligible=record.release_eligible,
        denylist_checked=True,
        denylist_registry_sha256=_registry_sha256(benchmark_registry),
    )
    return ExperimentalMixedAdapterResult(candidates=(normalized,))


def adapt_deterministic_composition_export(
    record: DeterministicCompositionExportRecord,
    *,
    replay: DeterministicCompositionReplayBinding,
    benchmark_registry: ActiveBenchmarkRegistry,
) -> ExperimentalMixedAdapterResult:
    """Project one receipt-export row without strengthening its intention."""

    try:
        replay = DeterministicCompositionReplayBinding.model_validate(
            replay.model_dump(mode="json")
        )
    except ValueError as exc:
        raise ExperimentalMixedSupervisionError(
            f"invalid deterministic composition replay binding: {exc}"
        ) from exc
    _verify_composition_replay_artifacts(replay, record=record)
    if record.input_unique_pair_id != replay.unique_pair.unique_pair_id:
        raise ExperimentalMixedSupervisionError("composition export/unique-pair IDs differ")
    if (
        record.original_source_theorem_id != replay.source_theorem.theorem_id
        or record.original_source_representation_id
        != replay.source_representation.representation_id
        or set(record.final_theorem_ids) != {item.theorem_id for item in replay.final_theorems}
        or set(record.final_representation_ids)
        != {item.representation_id for item in replay.final_representations}
        or set(record.chain_ids) != {item.chain_id for item in replay.chains}
        or record.source_dataset != replay.source_theorem.source
    ):
        raise ExperimentalMixedSupervisionError("composition export differs from replay binding")
    if record.disposition == "cycle_audit":
        return ExperimentalMixedAdapterResult(
            exclusions=(
                _make_exclusion(
                    reason="composition_cycle",
                    origin_ids=(record.export_record_id,),
                ),
            )
        )
    if record.disposition == "mixed_intention_quarantine":
        return ExperimentalMixedAdapterResult(
            exclusions=(
                _make_exclusion(
                    reason="composition_mixed_intention",
                    origin_ids=(record.export_record_id,),
                ),
            )
        )
    if (
        not record.alpha_novel
        or record.source_alpha_return
        or record.mixed_intention_collision
        or len(record.chain_kinds) != 1
    ):
        raise ExperimentalMixedSupervisionError(
            "provisional composition inventory row has incoherent novelty/intention flags"
        )
    source = _headless_view(
        context_id=replay.unique_pair.context_id,
        declaration=replay.source_theorem.proof_stripped_declaration,
        theorem_ids=(record.original_source_theorem_id,),
        representation_ids=(record.original_source_representation_id,),
        origin_record_ids=(record.export_record_id, record.input_unique_pair_id),
        alpha_identity_fingerprint=record.source_alpha_identity_fingerprint,
    )
    candidate = _headless_view(
        context_id=replay.unique_pair.context_id,
        declaration=replay.final_theorems[0].proof_stripped_declaration,
        theorem_ids=record.final_theorem_ids,
        representation_ids=record.final_representation_ids,
        origin_record_ids=(record.export_record_id, record.input_unique_pair_id),
        alpha_identity_fingerprint=record.final_alpha_identity_fingerprint,
    )
    if source is None or candidate is None:
        return ExperimentalMixedAdapterResult(
            exclusions=(
                _make_exclusion(
                    reason="headless_normalization_failed",
                    origin_ids=(record.export_record_id,),
                ),
            )
        )
    if (
        normalize_headless(record.source_lean) != source.headless
        or normalize_headless(record.final_lean) != candidate.headless
    ):
        raise ExperimentalMixedSupervisionError(
            "composition export text differs from replay-bound model views"
        )
    if _theorem_view_is_protected(
        benchmark_registry,
        replay.source_theorem,
        replay.source_representation,
    ) or any(
        _theorem_view_is_protected(
            benchmark_registry,
            next(
                theorem
                for theorem in replay.final_theorems
                if theorem.theorem_id == representation.theorem_id
            ),
            representation,
        )
        for representation in replay.final_representations
    ):
        return ExperimentalMixedAdapterResult(
            exclusions=(
                _make_exclusion(
                    reason="benchmark_overlap",
                    origin_ids=(record.export_record_id,),
                ),
            )
        )
    target: PseudoTarget = "same_claim" if record.chain_kinds == ("P_to_P",) else "not_same_claim"
    kind: SignalKind = (
        "deterministic_composition_p_to_p"
        if target == "same_claim"
        else "deterministic_composition_p_to_n"
    )
    signal = _make_signal(
        signal_kind=kind,
        pseudo_target=target,
        provenance_ids=_sorted_unique(
            (
                record.export_record_id,
                record.input_unique_pair_id,
                *record.chain_ids,
            )
        ),
        family_ids=record.chain_sequences,
        chain_sequences=record.chain_sequences,
        intended_relation="equivalent" if target == "same_claim" else "near_miss",
        certificate_sha256s=_sorted_unique(
            tuple(
                digest
                for chain in replay.chains
                for digest in (
                    chain.first_hop_certificate_sha256,
                    chain.second_hop_certificate_sha256,
                )
                if digest is not None
            )
        ),
        composition_export_record_id=record.export_record_id,
    )
    groups = _sorted_unique(
        (
            *replay.unique_pair.root_ancestry_ids,
            _content_group(source),
            _content_group(candidate),
        )
    )
    candidate_record = _make_candidate(
        exact_pair_key=_exact_pair_key(source, candidate),
        pseudo_target=target,
        signal=signal,
        split_group_ids=groups,
        source_datasets=(record.source_dataset,),
        source=source,
        candidate=candidate,
        private_source_content=record.private_source_content,
        redistribution_allowed=record.redistribution_allowed,
        external_transmission_allowed=record.external_transmission_allowed,
        release_eligible=record.release_eligible,
        denylist_checked=True,
        denylist_registry_sha256=_registry_sha256(benchmark_registry),
    )
    return ExperimentalMixedAdapterResult(candidates=(candidate_record,))


def _lean_check_type_pp(check: LF022LeanCheckRecord) -> str | None:
    values: set[str] = set()
    for declaration in check.attempts[-1].declarations:
        type_value = declaration.get("type")
        if not isinstance(type_value, dict):
            continue
        pp_value = type_value.get("pp")
        if isinstance(pp_value, str) and pp_value.strip():
            values.add(pp_value.strip())
    return next(iter(values)) if len(values) == 1 else None


def _codex_response_is_internally_coherent(judgment: LF022VerifiedCodexAuditJudgment) -> bool:
    response = judgment.response
    relation = response.relation.value if response.relation is not None else None
    if response.same_claim_answer == "same_claim":
        return response.a_implies_b != "no" and response.b_implies_a != "no"
    if response.same_claim_answer != "not_same_claim":
        return True
    if relation == "A_stronger":
        return response.a_implies_b != "no" and response.b_implies_a != "yes"
    if relation == "B_stronger":
        return response.b_implies_a != "no" and response.a_implies_b != "yes"
    if relation in {"incomparable", "unrelated"}:
        return response.a_implies_b != "yes" and response.b_implies_a != "yes"
    return False


def adapt_verified_lf022_codex_judgment(
    judgment: LF022VerifiedCodexAuditJudgment,
    *,
    item: LF022CodexAuditInput,
    check: LF022LeanCheckRecord,
    source_theorem: TheoremRecord,
    source_representation: RepresentationRecord,
    benchmark_registry: ActiveBenchmarkRegistry,
    judge_model: str,
    judge_reasoning_effort: str,
    response_artifact_set_sha256: str,
) -> ExperimentalMixedAdapterResult:
    """Project one already-replay-verified AB judgment into a proxy candidate."""

    if (
        judgment.audit_item_id != item.audit_item_id
        or judgment.lean_check_id != item.lean_check_id
        or judgment.lean_check_id != check.check_id
        or judgment.variant_id != item.variant_id
        or judgment.variant_id != check.variant_id
        or judgment.pair_id != item.pair.pair_id
        or judgment.source_record_ids != item.pair.source_record_ids
        or item.presentation.orientation != "AB"
        or item.presentation.lean_a != item.pair.canonical_lean_a
        or item.presentation.lean_b != item.pair.canonical_lean_b
    ):
        raise ExperimentalMixedSupervisionError("verified LF-022 judgment binding differs")
    if check.outcome not in {"elaborates", "elaborates_with_placeholder"}:
        raise ExperimentalMixedSupervisionError("LF-022 proxy requires a Lean-valid check")
    if not check.declaration_verified:
        raise ExperimentalMixedSupervisionError("LF-022 proxy requires declaration verification")
    if (
        source_theorem.theorem_id not in judgment.source_record_ids
        or source_representation.theorem_id != source_theorem.theorem_id
        or source_theorem.context_id != check.context_id
        or source_representation.context_id != check.context_id
        or source_theorem.source != check.source_id
        or sha256_hex(item.pair.canonical_lean_b.encode("utf-8")) != check.candidate_code_hash
    ):
        raise ExperimentalMixedSupervisionError("LF-022 source theorem/view/check binding differs")
    canonical_source_headless = normalize_headless(item.pair.canonical_lean_a)
    theorem_source_headless = normalize_headless(source_theorem.proof_stripped_declaration)
    if (
        canonical_source_headless is None
        or theorem_source_headless is None
        or source_representation.headless is None
        or canonical_source_headless != theorem_source_headless
        or canonical_source_headless != source_representation.headless
    ):
        raise ExperimentalMixedSupervisionError(
            "LF-022 public source differs from canonical source representation"
        )
    response = judgment.response
    if response.needs_expert_review:
        return ExperimentalMixedAdapterResult(
            exclusions=(
                _make_exclusion(
                    reason="codex_expert_review",
                    origin_ids=(judgment.audit_item_id, judgment.lean_check_id),
                ),
            )
        )
    if response.same_claim_answer in {"ambiguous", "uncertain"}:
        reason: ExclusionReason = (
            "codex_ambiguous" if response.same_claim_answer == "ambiguous" else "codex_uncertain"
        )
        return ExperimentalMixedAdapterResult(
            exclusions=(
                _make_exclusion(
                    reason=reason,
                    origin_ids=(judgment.audit_item_id, judgment.lean_check_id),
                ),
            )
        )
    if not _codex_response_is_internally_coherent(judgment):
        return ExperimentalMixedAdapterResult(
            exclusions=(
                _make_exclusion(
                    reason="codex_incoherent",
                    origin_ids=(judgment.audit_item_id, judgment.lean_check_id),
                ),
            )
        )
    source = _headless_view(
        context_id=check.context_id,
        declaration=item.pair.canonical_lean_a,
        theorem_ids=(source_theorem.theorem_id,),
        representation_ids=(source_representation.representation_id,),
        origin_record_ids=judgment.source_record_ids,
        alpha_identity_fingerprint=source_representation.alpha_identity_fingerprint,
    )
    candidate = _headless_view(
        context_id=check.context_id,
        declaration=item.pair.canonical_lean_b,
        origin_record_ids=(judgment.variant_id, judgment.lean_check_id),
        lean_check_type_pp=_lean_check_type_pp(check),
    )
    if source is None or candidate is None:
        return ExperimentalMixedAdapterResult(
            exclusions=(
                _make_exclusion(
                    reason="headless_normalization_failed",
                    origin_ids=(judgment.audit_item_id, judgment.lean_check_id),
                ),
            )
        )
    if _theorem_view_is_protected(
        benchmark_registry,
        source_theorem,
        source_representation,
    ) or _text_is_protected(
        benchmark_registry,
        item.pair.canonical_lean_b,
        candidate.headless,
    ):
        return ExperimentalMixedAdapterResult(
            exclusions=(
                _make_exclusion(
                    reason="benchmark_overlap",
                    origin_ids=(judgment.audit_item_id, judgment.lean_check_id),
                ),
            )
        )
    target: PseudoTarget = (
        "same_claim" if response.same_claim_answer == "same_claim" else "not_same_claim"
    )
    signal = _make_signal(
        signal_kind="codex_single_judge_ab",
        pseudo_target=target,
        provenance_ids=_sorted_unique(
            (
                judgment.audit_item_id,
                judgment.lean_check_id,
                judgment.pair_id,
                judgment.variant_id,
            )
        ),
        family_ids=(judgment.proposer_family_id,),
        audit_item_id=judgment.audit_item_id,
        lean_check_id=judgment.lean_check_id,
        pair_id=judgment.pair_id,
        variant_id=judgment.variant_id,
        proposer_family_id=judgment.proposer_family_id,
        judge_model=judge_model,
        judge_reasoning_effort=judge_reasoning_effort,
        judge_orientation="AB",
        judge_relation=response.relation.value if response.relation is not None else None,
        judge_confidence=response.confidence,
        final_message_sha256=judgment.final_message_sha256,
        parsed_response_sha256=judgment.parsed_response_sha256,
        response_artifact_set_sha256=response_artifact_set_sha256,
    )
    normalized = _make_candidate(
        exact_pair_key=_exact_pair_key(source, candidate),
        pseudo_target=target,
        signal=signal,
        split_group_ids=_sorted_unique(
            (
                *source_theorem.root_ancestry_ids,
                _content_group(source),
                _content_group(candidate),
            )
        ),
        source_datasets=(source_theorem.source,),
        source=source,
        candidate=candidate,
        private_source_content=False,
        redistribution_allowed=True,
        external_transmission_allowed=True,
        release_eligible=True,
        denylist_checked=True,
        denylist_registry_sha256=_registry_sha256(benchmark_registry),
    )
    return ExperimentalMixedAdapterResult(candidates=(normalized,))


def adapt_verified_lf022_codex_audit(
    verified: LF022VerifiedCodexAudit,
    *,
    source_theorems: Mapping[str, TheoremRecord],
    source_representations: Mapping[str, RepresentationRecord],
    benchmark_registry: ActiveBenchmarkRegistry,
) -> ExperimentalMixedAdapterResult:
    """Purely project a complete verified audit; never read its paths again."""

    if (
        verified.manifest.completed_count != len(verified.items)
        or verified.manifest.completed_count != len(verified.judgments)
        or verified.manifest.exhausted_count != 0
        or verified.manifest.attempt_status_counts != {"completed": len(verified.items)}
    ):
        raise ExperimentalMixedSupervisionError(
            "LF-022 adapter requires a complete clean verified audit"
        )
    items = {item.audit_item_id: item for item in verified.items}
    checks = {check.check_id: check for check in verified.checks}
    if len(items) != len(verified.items) or len(checks) != len(verified.checks):
        raise ExperimentalMixedSupervisionError("verified LF-022 audit repeats an identity")
    candidates: list[ExperimentalMixedCandidate] = []
    exclusions: list[ExperimentalMixedExclusion] = []
    for judgment in verified.judgments:
        item = items.get(judgment.audit_item_id)
        check = checks.get(judgment.lean_check_id)
        if item is None or check is None:
            raise ExperimentalMixedSupervisionError("verified judgment lacks item/check binding")
        theorem_ids = tuple(
            value for value in judgment.source_record_ids if value.startswith("thm:")
        )
        if len(theorem_ids) != 1:
            raise ExperimentalMixedSupervisionError(
                "verified LF-022 judgment lacks one canonical source theorem ID"
            )
        source_theorem = source_theorems.get(theorem_ids[0])
        source_representation = source_representations.get(theorem_ids[0])
        if source_theorem is None or source_representation is None:
            raise ExperimentalMixedSupervisionError(
                "verified LF-022 judgment lacks canonical source theorem ancestry/view"
            )
        result = adapt_verified_lf022_codex_judgment(
            judgment,
            item=item,
            check=check,
            source_theorem=source_theorem,
            source_representation=source_representation,
            benchmark_registry=benchmark_registry,
            judge_model=verified.manifest.model,
            judge_reasoning_effort=verified.manifest.reasoning_effort,
            response_artifact_set_sha256=verified.response_artifact_set_sha256,
        )
        candidates.extend(result.candidates)
        exclusions.extend(result.exclusions)
    return ExperimentalMixedAdapterResult(
        candidates=tuple(sorted(candidates, key=lambda item: item.candidate_id)),
        exclusions=tuple(sorted(exclusions, key=lambda item: item.exclusion_id)),
    )


def _basis(signals: Sequence[ExperimentalProxySignal]) -> PseudoTargetBasis:
    kinds = {signal.signal_kind for signal in signals}
    if kinds == {"codex_single_judge_ab"}:
        return "codex_single_judge_ab_proxy"
    if all(kind.startswith("deterministic_first_hop_") for kind in kinds):
        return "deterministic_first_hop_intention"
    if all(kind.startswith("deterministic_composition_") for kind in kinds):
        return "deterministic_composition_intention"
    return "agreeing_mixed_proxy"


def _signal_partition(signal: ExperimentalProxySignal) -> str:
    if signal.signal_kind.startswith("deterministic_first_hop_"):
        return "first_hop"
    if signal.signal_kind.startswith("deterministic_composition_"):
        return "composition"
    return "lf022_codex"


def _validate_partition_policy(
    records: Sequence[ExperimentalMixedSupervisionRecord],
    *,
    inputs: Mapping[str, ExperimentalMixedInputBinding],
    config: ExperimentalMixedSupervisionConfig,
) -> None:
    signal_counts: Counter[str] = Counter(
        _signal_partition(signal) for record in records for signal in record.signals
    )
    input_counts: Counter[str] = Counter(binding.partition for binding in inputs.values())
    if input_counts["policy"] == 0:
        raise ExperimentalMixedSupervisionError(
            "mixed corpus requires a bound active benchmark registry policy artifact"
        )
    statuses = {
        "first_hop": config.first_hop_partition,
        "lf022_codex": config.lf022_codex_partition,
        "composition": config.composition_partition,
    }
    for partition, status in statuses.items():
        if status == "included":
            if signal_counts[partition] == 0 or input_counts[partition] == 0:
                raise ExperimentalMixedSupervisionError(
                    f"included partition lacks records or bound inputs: {partition}"
                )
        elif signal_counts[partition] or input_counts[partition]:
            raise ExperimentalMixedSupervisionError(
                f"omitted partition unexpectedly contributes data: {partition}"
            )
    registry_hashes = {record.denylist_registry_sha256 for record in records}
    policy_hashes = {binding.sha256 for binding in inputs.values() if binding.partition == "policy"}
    if len(registry_hashes) != 1 or not registry_hashes.issubset(policy_hashes):
        raise ExperimentalMixedSupervisionError(
            "records must share and bind the exact active benchmark registry artifact"
        )


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
    parent: dict[str, str] = {}

    def find(value: str) -> str:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            successor = parent[value]
            parent[value] = root
            value = successor
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        low, high = sorted((left_root, right_root))
        parent[high] = low

    seen: set[str] = set()
    normalized: list[tuple[str, tuple[str, ...]]] = []
    for item_id, raw_groups in items:
        if item_id in seen:
            raise ExperimentalMixedSupervisionError(f"duplicate component item: {item_id}")
        seen.add(item_id)
        groups = _sorted_unique(raw_groups)
        if not groups:
            raise ExperimentalMixedSupervisionError(f"item has no split groups: {item_id}")
        normalized.append((item_id, groups))
        for group in groups:
            parent.setdefault(group, group)
        for group in groups[1:]:
            union(groups[0], group)
    members: dict[str, set[str]] = defaultdict(set)
    for group in parent:
        members[find(group)].add(group)
    component_by_root = {
        root: _component_id(tuple(sorted(values))) for root, values in members.items()
    }
    return {item_id: component_by_root[find(groups[0])] for item_id, groups in normalized}


def _split_for_component(
    component_id: str,
    *,
    config: ExperimentalMixedSupervisionConfig,
) -> ExperimentalSplit:
    bucket = (
        int(
            hash_canonical(
                {
                    "schema": "experimental_mixed_supervision_split_v1",
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


@dataclass(frozen=True, slots=True)
class _MergedCandidate:
    exact_pair_key: str
    pseudo_target: PseudoTarget
    signals: tuple[ExperimentalProxySignal, ...]
    split_group_ids: tuple[str, ...]
    source_datasets: tuple[str, ...]
    source: ExperimentalHeadlessStatementView
    candidate: ExperimentalHeadlessStatementView
    private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool
    denylist_checked: Literal[True]
    denylist_registry_sha256: str


def _deduplicate_candidates(
    candidates: Sequence[ExperimentalMixedCandidate],
) -> tuple[tuple[_MergedCandidate, ...], tuple[ExperimentalMixedExclusion, ...]]:
    by_pair: dict[str, list[ExperimentalMixedCandidate]] = defaultdict(list)
    seen_candidate_ids: set[str] = set()
    for candidate in candidates:
        if candidate.candidate_id in seen_candidate_ids:
            continue
        seen_candidate_ids.add(candidate.candidate_id)
        by_pair[candidate.exact_pair_key].append(candidate)
    merged: list[_MergedCandidate] = []
    exclusions: list[ExperimentalMixedExclusion] = []
    for pair_key, group in sorted(by_pair.items()):
        targets = _sorted_unique(tuple(item.pseudo_target for item in group))
        if targets == ("not_same_claim", "same_claim"):
            exclusions.append(
                _make_exclusion(
                    reason="conflicting_proxy_targets",
                    origin_ids=_sorted_unique(tuple(item.candidate_id for item in group)),
                    signal_ids=_sorted_unique(tuple(item.signal.signal_id for item in group)),
                    exact_pair_key=pair_key,
                    observed_pseudo_targets=targets,
                )
            )
            continue
        canonical = min(group, key=lambda item: item.candidate_id)
        registry_hashes = {item.denylist_registry_sha256 for item in group}
        if len(registry_hashes) != 1:
            raise ExperimentalMixedSupervisionError(
                "one exact pair was screened against different benchmark registries"
            )
        signals_by_id = {item.signal.signal_id: item.signal for item in group}
        merged.append(
            _MergedCandidate(
                exact_pair_key=pair_key,
                pseudo_target=canonical.pseudo_target,
                signals=tuple(signals_by_id[key] for key in sorted(signals_by_id)),
                split_group_ids=_sorted_unique(
                    tuple(value for item in group for value in item.split_group_ids)
                ),
                source_datasets=_sorted_unique(
                    tuple(value for item in group for value in item.source_datasets)
                ),
                source=canonical.source,
                candidate=canonical.candidate,
                private_source_content=any(item.private_source_content for item in group),
                redistribution_allowed=all(item.redistribution_allowed for item in group),
                external_transmission_allowed=all(
                    item.external_transmission_allowed for item in group
                ),
                release_eligible=all(item.release_eligible for item in group),
                denylist_checked=True,
                denylist_registry_sha256=registry_hashes.pop(),
            )
        )
    return tuple(merged), tuple(exclusions)


def _build_records(
    candidates: Sequence[ExperimentalMixedCandidate],
    *,
    config: ExperimentalMixedSupervisionConfig,
) -> tuple[
    tuple[ExperimentalMixedSupervisionRecord, ...],
    tuple[ExperimentalMixedExclusion, ...],
]:
    merged, conflict_exclusions = _deduplicate_candidates(candidates)
    if not merged:
        raise ExperimentalMixedSupervisionError("no conflict-free proxy pairs remain")
    components = _union_component_ids(
        tuple((item.exact_pair_key, item.split_group_ids) for item in merged)
    )
    records: list[ExperimentalMixedSupervisionRecord] = []
    for item in merged:
        component = components[item.exact_pair_key]
        payload: dict[str, object] = {
            "record_id": "experimental_mixed_pair:" + "0" * 64,
            "dataset_profile_id": config.profile_id,
            "exact_pair_key": item.exact_pair_key,
            "pseudo_target": item.pseudo_target,
            "pseudo_target_basis": _basis(item.signals),
            "signals": item.signals,
            "signal_kinds": _sorted_unique(tuple(signal.signal_kind for signal in item.signals)),
            "family_ids": _sorted_unique(
                tuple(family for signal in item.signals for family in signal.family_ids)
            ),
            "split_group_ids": item.split_group_ids,
            "split_component_id": component,
            "split": _split_for_component(component, config=config),
            "source_datasets": item.source_datasets,
            "source": item.source,
            "candidate": item.candidate,
            "private_source_content": item.private_source_content,
            "redistribution_allowed": item.redistribution_allowed,
            "external_transmission_allowed": item.external_transmission_allowed,
            "release_eligible": item.release_eligible,
            "denylist_checked": item.denylist_checked,
            "denylist_registry_sha256": item.denylist_registry_sha256,
        }
        provisional = ExperimentalMixedSupervisionRecord.model_construct(
            _fields_set=None, **payload
        )
        payload["record_id"] = "experimental_mixed_pair:" + hash_canonical(
            _without_id(provisional.model_dump(mode="json"), "record_id")
        )
        records.append(ExperimentalMixedSupervisionRecord.model_validate(payload))
    return (
        tuple(sorted(records, key=lambda item: item.record_id)),
        tuple(sorted(conflict_exclusions, key=lambda item: item.exclusion_id)),
    )


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_line(record) for record in records)


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
            raise ExperimentalMixedSupervisionError(f"required path is absent: {current}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ExperimentalMixedSupervisionError(f"path contains a symlink: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ExperimentalMixedSupervisionError(
                f"path parent component is not a directory: {current}"
            )
    return absolute


def _regular_file(path: Path) -> Path:
    safe = _reject_symlink_components(path, allow_missing=False)
    if not safe.is_file():
        raise ExperimentalMixedSupervisionError(f"input is not a regular file: {safe}")
    return safe


def _real_directory(path: Path) -> Path:
    safe = _reject_symlink_components(path, allow_missing=False)
    if not safe.is_dir():
        raise ExperimentalMixedSupervisionError(f"input is not a directory: {safe}")
    return safe


def bind_experimental_mixed_input(
    path: Path,
    *,
    partition: Literal["first_hop", "lf022_codex", "composition", "policy"],
) -> ExperimentalMixedInputBinding:
    """Bind one artifact to the proxy partition whose lineage it supports."""

    safe = _regular_file(path)
    return ExperimentalMixedInputBinding(
        partition=partition,
        path=str(safe),
        sha256=hash_file(safe),
        byte_count=safe.stat().st_size,
    )


def _verify_input_binding(name: str, binding: ExperimentalMixedInputBinding) -> None:
    path = _regular_file(Path(binding.path))
    if hash_file(path) != binding.sha256 or path.stat().st_size != binding.byte_count:
        raise ExperimentalMixedSupervisionError(f"external input differs: {name}")


def _load_canonical_jsonl[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
) -> tuple[ModelT, ...]:
    safe = _regular_file(path)
    output: list[ModelT] = []
    for line_number, raw in enumerate(safe.read_bytes().splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n") or not raw.strip():
            raise ExperimentalMixedSupervisionError(
                f"invalid JSONL framing at {safe}:{line_number}"
            )
        try:
            item = model.model_validate_json(raw)
        except ValueError as exc:
            raise ExperimentalMixedSupervisionError(
                f"invalid {model.__name__} at {safe}:{line_number}: {exc}"
            ) from exc
        if raw != _canonical_line(item):
            raise ExperimentalMixedSupervisionError(
                f"non-canonical {model.__name__} at {safe}:{line_number}"
            )
        output.append(item)
    return tuple(output)


def _load_canonical_model[ModelT: StrictModel](path: Path, model: type[ModelT]) -> ModelT:
    safe = _regular_file(path)
    raw = safe.read_bytes()
    try:
        item = model.model_validate_json(raw)
    except ValueError as exc:
        raise ExperimentalMixedSupervisionError(f"invalid {model.__name__}: {exc}") from exc
    if raw != _canonical_line(item):
        raise ExperimentalMixedSupervisionError(f"non-canonical {model.__name__}: {safe}")
    return item


def _bound_jsonl_line(
    binding: ExperimentalMixedInputBinding,
    *,
    line_number: int,
) -> bytes:
    """Return one exactly framed line after verifying the complete bound file."""

    _verify_input_binding("composition replay artifact", binding)
    lines = _regular_file(Path(binding.path)).read_bytes().splitlines(keepends=True)
    if line_number > len(lines):
        raise ExperimentalMixedSupervisionError(
            f"bound JSONL line is absent: {binding.path}:{line_number}"
        )
    raw = lines[line_number - 1]
    if not raw.endswith(b"\n") or not raw.strip():
        raise ExperimentalMixedSupervisionError(
            f"invalid bound JSONL framing: {binding.path}:{line_number}"
        )
    return raw


def _load_bound_source_partition[ModelT: StrictModel](
    binding: ExperimentalMixedInputBinding,
    model: type[ModelT],
    *,
    wrapper_key: str,
) -> tuple[ModelT, ...]:
    """Load a hash-bound direct or extraction-wrapper source partition."""

    _verify_input_binding("composition source partition", binding)
    output: list[ModelT] = []
    safe = _regular_file(Path(binding.path))
    for line_number, raw in enumerate(safe.read_bytes().splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n") or not raw.strip():
            raise ExperimentalMixedSupervisionError(
                f"invalid source JSONL framing at {safe}:{line_number}"
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExperimentalMixedSupervisionError(
                f"invalid source JSON at {safe}:{line_number}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ExperimentalMixedSupervisionError(
                f"source row is not an object at {safe}:{line_number}"
            )
        selected = payload.get(wrapper_key, payload)
        if not isinstance(selected, dict):
            raise ExperimentalMixedSupervisionError(
                f"invalid source wrapper at {safe}:{line_number}"
            )
        try:
            output.append(model.model_validate(selected))
        except ValueError as exc:
            raise ExperimentalMixedSupervisionError(
                f"invalid {model.__name__} at {safe}:{line_number}: {exc}"
            ) from exc
    return tuple(output)


def _verify_composition_replay_artifacts(
    replay: DeterministicCompositionReplayBinding,
    *,
    record: DeterministicCompositionExportRecord,
) -> None:
    """Prove every projected composition object is a member of receipt-bound files."""

    exact_models: tuple[
        tuple[str, ExperimentalMixedInputBinding, StrictModel, type[StrictModel]], ...
    ] = (
        (
            "full launch spec",
            replay.full_launch_spec_artifact,
            replay.full_launch_spec,
            CompositionFullLaunchSpec,
        ),
        (
            "full receipt",
            replay.full_receipt_artifact,
            replay.full_receipt,
            CompositionFullReceipt,
        ),
        (
            "composition export manifest",
            replay.export_manifest_artifact,
            replay.export_manifest,
            DeterministicCompositionReceiptExportManifest,
        ),
        (
            "composition chain manifest",
            replay.chain_manifest_artifact,
            replay.chain_manifest,
            DeterministicCompositionChainManifest,
        ),
        (
            "composition unique-pair manifest",
            replay.unique_pair_manifest_artifact,
            replay.unique_pair_manifest,
            DeterministicCompositionUniquePairManifest,
        ),
    )
    for label, binding, expected, model in exact_models:
        _verify_input_binding(label, binding)
        loaded = _load_canonical_model(Path(binding.path), model)
        if loaded != expected:
            raise ExperimentalMixedSupervisionError(f"{label} object differs from bound bytes")

    if (
        replay.full_receipt.launch_spec_sha256 != replay.full_launch_spec_artifact.sha256
        or replay.full_receipt.final_status_sha256 != replay.full_status_artifact.sha256
        or replay.export_manifest.full_launch_spec_sha256 != replay.full_launch_spec_artifact.sha256
        or replay.export_manifest.full_receipt_sha256 != replay.full_receipt_artifact.sha256
        or replay.export_manifest.input_chain_manifest_sha256
        != replay.chain_manifest_artifact.sha256
        or replay.export_manifest.input_unique_pair_manifest_sha256
        != replay.unique_pair_manifest_artifact.sha256
        or replay.unique_pair_manifest.input_chain_manifest_sha256
        != replay.chain_manifest_artifact.sha256
    ):
        raise ExperimentalMixedSupervisionError(
            "composition receipt/export/manifest artifact hashes do not join"
        )
    _verify_input_binding("composition final status", replay.full_status_artifact)

    expected_partition_hash = {
        "inventory": replay.export_manifest.inventory_sha256,
        "cycles": replay.export_manifest.cycles_sha256,
        "quarantine": replay.export_manifest.quarantine_sha256,
    }[replay.export_partition]
    expected_disposition = {
        "inventory": "provisional_inventory",
        "cycles": "cycle_audit",
        "quarantine": "mixed_intention_quarantine",
    }[replay.export_partition]
    if (
        replay.export_partition_artifact.sha256 != expected_partition_hash
        or record.disposition != expected_disposition
    ):
        raise ExperimentalMixedSupervisionError(
            "composition export row is bound to the wrong receipt-export partition"
        )
    export_raw = _bound_jsonl_line(
        replay.export_partition_artifact,
        line_number=replay.export_line_number,
    )
    if sha256_hex(export_raw) != replay.export_line_sha256 or export_raw != _canonical_line(record):
        raise ExperimentalMixedSupervisionError(
            "composition export record is not the bound partition member"
        )

    _verify_input_binding("composition chain records", replay.chain_records_artifact)
    _verify_input_binding("composition unique-pair records", replay.unique_pair_records_artifact)
    if (
        replay.chain_records_artifact.sha256 != replay.chain_manifest.chain_output_sha256
        or replay.unique_pair_records_artifact.sha256
        != replay.unique_pair_manifest.unique_output_sha256
        or replay.unique_pair_manifest.input_chain_records_sha256
        != replay.chain_records_artifact.sha256
    ):
        raise ExperimentalMixedSupervisionError(
            "composition record partitions differ from their manifests"
        )
    all_chains = _load_canonical_jsonl(
        Path(replay.chain_records_artifact.path), DeterministicCompositionChainRecord
    )
    all_pairs = _load_canonical_jsonl(
        Path(replay.unique_pair_records_artifact.path),
        DeterministicCompositionUniquePairRecord,
    )
    if len(all_chains) != replay.chain_manifest.chain_count or len(all_pairs) != (
        replay.unique_pair_manifest.unique_pair_count
    ):
        raise ExperimentalMixedSupervisionError(
            "composition record partition counts differ from manifests"
        )
    chain_by_id = {item.chain_id: item for item in all_chains}
    pair_by_id = {item.unique_pair_id: item for item in all_pairs}
    if len(chain_by_id) != len(all_chains) or len(pair_by_id) != len(all_pairs):
        raise ExperimentalMixedSupervisionError("composition partitions repeat identities")
    if pair_by_id.get(replay.unique_pair.unique_pair_id) != replay.unique_pair or any(
        chain_by_id.get(item.chain_id) != item for item in replay.chains
    ):
        raise ExperimentalMixedSupervisionError(
            "composition replay objects are absent from bound audited partitions"
        )

    theorem_hashes = tuple(sorted(binding.sha256 for binding in replay.source_theorem_artifacts))
    representation_hashes = tuple(
        sorted(binding.sha256 for binding in replay.source_representation_artifacts)
    )
    if (
        theorem_hashes != replay.export_manifest.source_theorem_partition_sha256s
        or representation_hashes != replay.export_manifest.source_representation_partition_sha256s
    ):
        raise ExperimentalMixedSupervisionError(
            "composition source partitions differ from receipt export"
        )
    source_theorems = tuple(
        item
        for binding in replay.source_theorem_artifacts
        for item in _load_bound_source_partition(binding, TheoremRecord, wrapper_key="theorem")
    )
    source_representations = tuple(
        item
        for binding in replay.source_representation_artifacts
        for item in _load_bound_source_partition(
            binding,
            RepresentationRecord,
            wrapper_key="representation",
        )
    )
    if replay.source_theorem not in source_theorems or (
        replay.source_representation not in source_representations
    ):
        raise ExperimentalMixedSupervisionError(
            "composition source theorem/view are absent from bound source partitions"
        )

    roots = {item.root_binding_id: item for item in replay.chain_manifest.second_hop_roots}
    receipt_roots = {item.root_binding_id: item for item in replay.full_receipt.roots}
    if set(roots) != set(receipt_roots) or any(
        roots[root_id].results.sha256 != receipt_roots[root_id].results_sha256 for root_id in roots
    ):
        raise ExperimentalMixedSupervisionError(
            "composition chain manifest does not bind all and only receipt result roots"
        )
    final_theorems = {item.theorem_id: item for item in replay.final_theorems}
    final_representations = {item.representation_id: item for item in replay.final_representations}
    for chain in replay.chains:
        artifact = replay.second_hop_result_artifacts[chain.second_hop_root_binding_id]
        root = roots.get(chain.second_hop_root_binding_id)
        receipt_root = receipt_roots.get(chain.second_hop_root_binding_id)
        if (
            root is None
            or receipt_root is None
            or artifact.sha256 != root.results.sha256
            or artifact.byte_count != root.results.byte_count
            or artifact.sha256 != receipt_root.results_sha256
        ):
            raise ExperimentalMixedSupervisionError(
                "composition second-hop result artifact differs from receipt/chain manifest"
            )
        raw = _bound_jsonl_line(artifact, line_number=chain.second_hop_result_line_number)
        result_model: type[V2E2MaterializationResult] | type[V2D0MaterializationResult]
        result_model = (
            V2E2MaterializationResult if root.run_kind == "e2" else V2D0MaterializationResult
        )
        try:
            result = result_model.model_validate_json(raw)
        except ValueError as exc:
            raise ExperimentalMixedSupervisionError(
                f"invalid receipt-bound second-hop result: {exc}"
            ) from exc
        if raw != _canonical_line(result):
            raise ExperimentalMixedSupervisionError(
                "receipt-bound second-hop result is not canonical"
            )
        theorem = final_theorems[chain.final_theorem_id]
        representation = final_representations[chain.final_representation_id]
        if (
            result.result_id != chain.second_hop_result_id
            or result.profile_id != chain.second_hop_profile_id
            or result.rule_id != chain.second_hop_rule_id
            or result.terminal_status != "provisional_variant"
            or result.candidate_theorem != theorem
            or result.candidate_representation != representation
            or result.draft is None
            or result.audit is None
            or result.variant is None
            or result.attempt.attempt_id != chain.second_hop_attempt_id
            or result.draft.draft_id != chain.second_hop_draft_id
            or result.audit.audit_id != chain.second_hop_audit_id
            or result.variant.variant_id != chain.second_hop_variant_id
        ):
            raise ExperimentalMixedSupervisionError(
                "composition chain/certificate/view differs from bound second-hop result"
            )


def _summary(
    records: Sequence[ExperimentalMixedSupervisionRecord],
    exclusions: Sequence[ExperimentalMixedExclusion],
    *,
    dataset_id: str,
    profile_id: str,
) -> ExperimentalMixedSupervisionSummary:
    return ExperimentalMixedSupervisionSummary(
        dataset_id=dataset_id,
        profile_id=profile_id,
        record_count=len(records),
        signal_count=sum(len(record.signals) for record in records),
        exclusion_count=len(exclusions),
        component_count=len({record.split_component_id for record in records}),
        counts_by_pseudo_target=dict(
            sorted(Counter(record.pseudo_target for record in records).items())
        ),
        counts_by_basis=dict(
            sorted(Counter(record.pseudo_target_basis for record in records).items())
        ),
        counts_by_signal_kind=dict(
            sorted(
                Counter(
                    signal.signal_kind for record in records for signal in record.signals
                ).items()
            )
        ),
        counts_by_split=dict(sorted(Counter(record.split for record in records).items())),
        counts_by_exclusion_reason=dict(
            sorted(Counter(item.reason for item in exclusions).items())
        ),
    )


def _dataset_id(
    *,
    config_hash: str,
    code_tree_hash: str,
    inputs: Mapping[str, ExperimentalMixedInputBinding],
    records: Sequence[ExperimentalMixedSupervisionRecord],
    exclusions: Sequence[ExperimentalMixedExclusion],
) -> str:
    return "experimental_mixed_supervision:" + hash_canonical(
        {
            "schema": "experimental_mixed_supervision_dataset_v2",
            "config_hash": config_hash,
            "code_tree_hash": code_tree_hash,
            "inputs": {
                name: {
                    "partition": binding.partition,
                    "sha256": binding.sha256,
                    "byte_count": binding.byte_count,
                }
                for name, binding in sorted(inputs.items())
            },
            "record_ids": [record.record_id for record in records],
            "exclusion_ids": [item.exclusion_id for item in exclusions],
        }
    )


def _verify_existing_output(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    safe = _real_directory(output_dir)
    if {path.name for path in safe.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalMixedSupervisionError("existing output file set is not exact")
    for name, expected in payloads.items():
        if _regular_file(safe / name).read_bytes() != expected:
            raise ExperimentalMixedSupervisionError(f"existing mixed output differs: {name}")
    return True


def _write_or_replay(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    if set(payloads) != _OUTPUT_FILES:
        raise ExperimentalMixedSupervisionError("output payload set is not exact")
    output = _reject_symlink_components(output_dir, allow_missing=True)
    if output.exists():
        return _verify_existing_output(output, payloads)
    output.parent.mkdir(parents=True, exist_ok=True)
    _real_directory(output.parent)
    if output.exists():
        return _verify_existing_output(output, payloads)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for name, payload in sorted(payloads.items()):
            path = temporary / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        os.rename(temporary, output)
    except FileExistsError:
        if temporary.exists():
            shutil.rmtree(temporary)
        return _verify_existing_output(output, payloads)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return False


def freeze_experimental_mixed_supervision(
    *,
    repo_root: Path,
    output_dir: Path,
    config: ExperimentalMixedSupervisionConfig,
    candidates: Sequence[ExperimentalMixedCandidate],
    adapter_exclusions: Sequence[ExperimentalMixedExclusion],
    inputs: Mapping[str, ExperimentalMixedInputBinding],
) -> ExperimentalMixedSupervisionArtifacts:
    """Freeze or exactly replay one mixed proxy artifact without Lean/model calls."""

    repo = _real_directory(repo_root)
    output = _reject_symlink_components(output_dir, allow_missing=True)
    if output == repo or output in repo.parents or repo in output.parents:
        raise ExperimentalMixedSupervisionError("output directory must be disjoint from repo root")
    normalized_inputs = dict(sorted(inputs.items()))
    if not normalized_inputs or any(not name for name in normalized_inputs):
        raise ExperimentalMixedSupervisionError("at least one named input binding is required")
    for name, binding in normalized_inputs.items():
        _verify_input_binding(name, binding)
    code = collect_code_state(repo)
    if code.git_dirty or code.code_tree_hash is None or code.untracked_files:
        raise ExperimentalMixedSupervisionError(
            "mixed corpus freeze requires a clean, fully tracked code tree"
        )
    records, conflict_exclusions = _build_records(candidates, config=config)
    _validate_partition_policy(records, inputs=normalized_inputs, config=config)
    exclusions_by_id = {
        item.exclusion_id: item for item in (*adapter_exclusions, *conflict_exclusions)
    }
    exclusions = tuple(exclusions_by_id[key] for key in sorted(exclusions_by_id))
    config_hash = hash_canonical(config.model_dump(mode="json"))
    dataset_id = _dataset_id(
        config_hash=config_hash,
        code_tree_hash=code.code_tree_hash,
        inputs=normalized_inputs,
        records=records,
        exclusions=exclusions,
    )
    assignments = tuple(
        ExperimentalMixedSplitAssignment(
            record_id=record.record_id,
            split_component_id=record.split_component_id,
            split_group_ids=record.split_group_ids,
            split=record.split,
            pseudo_target=record.pseudo_target,
        )
        for record in records
    )
    summary = _summary(
        records,
        exclusions,
        dataset_id=dataset_id,
        profile_id=config.profile_id,
    )
    non_manifest_payloads = {
        "records.jsonl": _canonical_jsonl(records),
        "split_assignments.jsonl": _canonical_jsonl(assignments),
        "excluded.jsonl": _canonical_jsonl(exclusions),
        "summary.json": _canonical_line(summary),
    }
    manifest = ExperimentalMixedSupervisionManifest(
        dataset_id=dataset_id,
        profile_id=config.profile_id,
        config_hash=config_hash,
        config=config,
        code=code,
        inputs=normalized_inputs,
        record_count=len(records),
        signal_count=summary.signal_count,
        exclusion_count=len(exclusions),
        component_count=summary.component_count,
        first_hop_partition=config.first_hop_partition,
        lf022_codex_partition=config.lf022_codex_partition,
        composition_partition=config.composition_partition,
        output_sha256={
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in non_manifest_payloads.items()
        },
    )
    payloads = {
        **non_manifest_payloads,
        "manifest.json": _canonical_line(manifest),
    }
    for name, binding in normalized_inputs.items():
        _verify_input_binding(name, binding)
    replayed = _write_or_replay(output, payloads)
    verify_experimental_mixed_supervision(output)
    return ExperimentalMixedSupervisionArtifacts(
        output_dir=output,
        manifest_path=output / "manifest.json",
        records_path=output / "records.jsonl",
        split_assignments_path=output / "split_assignments.jsonl",
        exclusions_path=output / "excluded.jsonl",
        summary_path=output / "summary.json",
        dataset_id=dataset_id,
        record_count=len(records),
        exclusion_count=len(exclusions),
        replayed=replayed,
    )


def verify_experimental_mixed_supervision(
    output_dir: Path,
    *,
    verify_external_inputs: bool = True,
) -> ExperimentalMixedSupervisionManifest:
    """Verify the complete corpus from bytes without executing Lean or a model."""

    root = _real_directory(output_dir)
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalMixedSupervisionError("mixed corpus file set is not exact")
    manifest = _load_canonical_model(root / "manifest.json", ExperimentalMixedSupervisionManifest)
    summary = _load_canonical_model(root / "summary.json", ExperimentalMixedSupervisionSummary)
    for name, expected in manifest.output_sha256.items():
        if hash_file(_regular_file(root / name)) != expected:
            raise ExperimentalMixedSupervisionError(f"output hash differs: {name}")
    records = _load_canonical_jsonl(root / "records.jsonl", ExperimentalMixedSupervisionRecord)
    assignments = _load_canonical_jsonl(
        root / "split_assignments.jsonl", ExperimentalMixedSplitAssignment
    )
    exclusions = _load_canonical_jsonl(root / "excluded.jsonl", ExperimentalMixedExclusion)
    if tuple(sorted(records, key=lambda item: item.record_id)) != records:
        raise ExperimentalMixedSupervisionError("records are not in canonical ID order")
    if tuple(sorted(exclusions, key=lambda item: item.exclusion_id)) != exclusions:
        raise ExperimentalMixedSupervisionError("exclusions are not in canonical ID order")
    if len({item.record_id for item in records}) != len(records):
        raise ExperimentalMixedSupervisionError("duplicate mixed record ID")
    if len({item.exclusion_id for item in exclusions}) != len(exclusions):
        raise ExperimentalMixedSupervisionError("duplicate mixed exclusion ID")
    if len(records) != manifest.record_count or len(exclusions) != manifest.exclusion_count:
        raise ExperimentalMixedSupervisionError("artifact counts differ from manifest")
    if any(item.dataset_profile_id != manifest.profile_id for item in records):
        raise ExperimentalMixedSupervisionError("record profile differs from manifest")
    expected_components = _union_component_ids(
        tuple((record.record_id, record.split_group_ids) for record in records)
    )
    for record in records:
        if record.split_component_id != expected_components[record.record_id]:
            raise ExperimentalMixedSupervisionError("record ancestry component is noncanonical")
        if record.split != _split_for_component(record.split_component_id, config=manifest.config):
            raise ExperimentalMixedSupervisionError("record split is noncanonical")
    expected_assignments = tuple(
        ExperimentalMixedSplitAssignment(
            record_id=record.record_id,
            split_component_id=record.split_component_id,
            split_group_ids=record.split_group_ids,
            split=record.split,
            pseudo_target=record.pseudo_target,
        )
        for record in records
    )
    if assignments != expected_assignments:
        raise ExperimentalMixedSupervisionError("split assignments differ from records")
    expected_summary = _summary(
        records,
        exclusions,
        dataset_id=manifest.dataset_id,
        profile_id=manifest.profile_id,
    )
    if summary != expected_summary:
        raise ExperimentalMixedSupervisionError("summary differs from corpus content")
    code_tree_hash = manifest.code.code_tree_hash
    if code_tree_hash is None:
        raise ExperimentalMixedSupervisionError("manifest lacks code-tree hash")
    if (
        _dataset_id(
            config_hash=manifest.config_hash,
            code_tree_hash=code_tree_hash,
            inputs=manifest.inputs,
            records=records,
            exclusions=exclusions,
        )
        != manifest.dataset_id
    ):
        raise ExperimentalMixedSupervisionError("dataset ID differs from corpus content")
    if manifest.signal_count != sum(len(record.signals) for record in records):
        raise ExperimentalMixedSupervisionError("signal count differs from records")
    if manifest.component_count != len({record.split_component_id for record in records}):
        raise ExperimentalMixedSupervisionError("component count differs from records")
    _validate_partition_policy(records, inputs=manifest.inputs, config=manifest.config)
    if verify_external_inputs:
        for name, binding in manifest.inputs.items():
            _verify_input_binding(name, binding)
    return manifest


def load_experimental_mixed_supervision(
    output_dir: Path,
    *,
    allow_experimental_mixed_supervision: bool,
    purpose: str,
) -> tuple[ExperimentalMixedModelExample, ...]:
    """Load only with explicit opt-in for a named experimental purpose."""

    if not allow_experimental_mixed_supervision:
        raise ExperimentalMixedSupervisionError(
            "loading requires --allow-experimental-mixed-supervision"
        )
    if purpose not in {"learning_curve", "proxy_diagnostics", "smoke_training"}:
        raise ExperimentalMixedSupervisionError(
            "mixed proxy corpus is forbidden for scientific training, selection, "
            "calibration, or eval"
        )
    manifest = verify_experimental_mixed_supervision(output_dir)
    if purpose not in manifest.allowed_purposes:
        raise ExperimentalMixedSupervisionError(f"purpose is not admitted: {purpose}")
    records = _load_canonical_jsonl(
        output_dir / "records.jsonl", ExperimentalMixedSupervisionRecord
    )
    return tuple(
        ExperimentalMixedModelExample(
            record_id=record.record_id,
            source_headless=record.source.headless,
            candidate_headless=record.candidate.headless,
            pseudo_target=record.pseudo_target,
            split=record.split,
        )
        for record in records
    )


__all__ = [
    "DeterministicCompositionReplayBinding",
    "ExperimentalHeadlessStatementView",
    "ExperimentalMixedAdapterResult",
    "ExperimentalMixedCandidate",
    "ExperimentalMixedExclusion",
    "ExperimentalMixedInputBinding",
    "ExperimentalMixedModelExample",
    "ExperimentalMixedSplitAssignment",
    "ExperimentalMixedSupervisionArtifacts",
    "ExperimentalMixedSupervisionConfig",
    "ExperimentalMixedSupervisionError",
    "ExperimentalMixedSupervisionManifest",
    "ExperimentalMixedSupervisionRecord",
    "ExperimentalMixedSupervisionSummary",
    "ExperimentalProxySignal",
    "adapt_deterministic_composition_export",
    "adapt_selectable_first_hop_projection",
    "adapt_verified_lf022_codex_audit",
    "adapt_verified_lf022_codex_judgment",
    "bind_experimental_mixed_input",
    "freeze_experimental_mixed_supervision",
    "load_experimental_mixed_supervision",
    "verify_experimental_mixed_supervision",
]
