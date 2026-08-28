"""Fail-closed M0 dual-encoder preparation for mixed proxy supervision.

This module is the first token-model path after the scalar diagnostics, but it
is still deliberately *not* a scientific LeanFaith model.  Its labels are the
machine proxy intentions/opinions in ``ExperimentalMixedSupervisionRecord``.
Consequently every artifact carries a boundary that forbids model selection,
calibration, evaluation, gate credit, and release claims.

The mixed corpus exposes one honest model view: ``headless``.  It does not
expose ``signature_explicit``; ``lean_check_type_pp`` is audit metadata and is
never admitted here.  The input preparation stage therefore emits exactly
``[HEADLESS]\n...`` for each side, binds the frozen corpus and completed
tokenizer audit byte-for-byte, and preserves over-length examples in an
explicit ``long_input`` slice.

Model weights are not downloaded by this module.  A runtime can be built only
from an already-local, content-addressed ModernBERT-base checkpoint after the
tokenizer audit has established a context length and marked that candidate
eligible.  The architecture uses one shared encoder twice, masked-mean pooled
L2-normalized embeddings, and the symmetric feature vector required by Plan
section 21.3: cosine, absolute difference, and elementwise product.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import stat
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Self, cast

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.datasets.experimental_mixed_supervision import (
    ExperimentalMixedSupervisionManifest,
    ExperimentalMixedSupervisionRecord,
    verify_experimental_mixed_supervision,
)
from leanfaith.models.tokenizer_audit import (
    SnapshotBinding,
    TokenizerAuditManifest,
    TokenizerAuditSummary,
    verify_tokenizer_audit,
)
from leanfaith.schemas.manifest import CodeState, collect_code_state

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_DATASET_ID = r"^experimental_mixed_supervision:[0-9a-f]{64}$"
_AUDIT_ID = r"^tokenizer_audit:[0-9a-f]{64}$"
_ARTIFACT_ID = r"^experimental-m0-proxy-inputs:[0-9a-f]{64}$"
_RECORD_ID = r"^experimental_mixed_pair:[0-9a-f]{64}$"
_COMPONENT_ID = r"^split-component:[0-9a-f]{64}$"
_OUTPUT_FILES = frozenset({"examples.jsonl", "manifest.json", "summary.json"})
_NON_MANIFEST_OUTPUTS = frozenset({"examples.jsonl", "summary.json"})
_TRAINING_OUTPUT_FILES = frozenset(
    {
        "epoch_schedule.json",
        "manifest.json",
        "metrics.json",
        "model.safetensors",
        "predictions.jsonl",
    }
)
_TRAINING_NON_MANIFEST_OUTPUTS = _TRAINING_OUTPUT_FILES - {"manifest.json"}
_HEADLESS_MARKER = "[HEADLESS]\n"

ProxyTarget = Literal["same_claim", "not_same_claim"]
ProxySplit = Literal["train", "validation", "test"]
_TARGETS: tuple[ProxyTarget, ProxyTarget] = ("not_same_claim", "same_claim")
_SPLITS: tuple[ProxySplit, ProxySplit, ProxySplit] = ("test", "train", "validation")


class ExperimentalM0ProxyError(RuntimeError):
    """An M0 proxy prerequisite, policy, or replay invariant failed."""


class ExperimentalM0ProxyBoundary(StrictModel):
    """Fail-closed boundary copied onto every proxy artifact."""

    target_basis: Literal["mixed_machine_proxy"] = "mixed_machine_proxy"
    semantic_prediction: Literal[False] = False
    scientific_training_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False
    release_claim_eligible: Literal[False] = False


class M0ProxyBackboneProtocol(StrictModel):
    candidate_key: Literal["modernbert_base"] = "modernbert_base"
    model_id: Literal["answerdotai/ModernBERT-base"] = "answerdotai/ModernBERT-base"
    revision: Literal["8949b909ec900327062f0ebf497f51aef5e6f0c8"] = (
        "8949b909ec900327062f0ebf497f51aef5e6f0c8"
    )
    local_files_only: Literal[True] = True
    trust_remote_code: Literal[False] = False
    scientific_winner_claimed: Literal[False] = False
    hf_snapshot_api_url: Literal[
        "https://huggingface.co/api/models/answerdotai/ModernBERT-base/tree/"
        "8949b909ec900327062f0ebf497f51aef5e6f0c8?recursive=true&expand=true"
    ] = (
        "https://huggingface.co/api/models/answerdotai/ModernBERT-base/tree/"
        "8949b909ec900327062f0ebf497f51aef5e6f0c8?recursive=true&expand=true"
    )
    weight_filename: Literal["model.safetensors"] = "model.safetensors"
    weight_sha256: Literal["340ac08b74eef0d7bdec2d7981a6a3d4249bf0e6aab60634b72ad02c2b8023a9"] = (
        "340ac08b74eef0d7bdec2d7981a6a3d4249bf0e6aab60634b72ad02c2b8023a9"
    )
    weight_byte_count: Literal[598635032] = 598635032
    receipt_basis: Literal["pinned_huggingface_lfs_sha256_v1"] = "pinned_huggingface_lfs_sha256_v1"


class M0ProxyArchitectureProtocol(StrictModel):
    architecture: Literal["shared_dual_encoder_symmetric_head_v1"] = (
        "shared_dual_encoder_symmetric_head_v1"
    )
    input_profile: Literal["headless_only_v1"] = "headless_only_v1"
    input_marker: Literal["[HEADLESS]"] = "[HEADLESS]"
    pooling: Literal["attention_masked_mean"] = "attention_masked_mean"
    embedding_normalization: Literal["l2"] = "l2"
    symmetric_features: tuple[str, ...] = ("cosine", "absolute_difference", "product")
    head: Literal["single_linear_logit"] = "single_linear_logit"
    encoder_instances: Literal[1] = 1
    optional_contrastive_loss_weight: float = Field(default=0.0, ge=0.0, le=0.0)
    optional_ranking_loss_weight: float = Field(default=0.0, ge=0.0, le=0.0)

    @field_validator("symmetric_features")
    @classmethod
    def _features_are_exact(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        expected = ("cosine", "absolute_difference", "product")
        if value != expected:
            raise ValueError(f"M0 symmetric features must be exactly {expected}")
        return value


class M0ProxyTrainingProtocol(StrictModel):
    purpose: Literal["proxy_diagnostics"] = "proxy_diagnostics"
    training_split: Literal["train"] = "train"
    diagnostic_splits: tuple[Literal["validation", "test"], ...] = ("validation", "test")
    training_record_policy: Literal["deterministic_max4_per_ancestry_without_replacement_v1"] = (
        "deterministic_max4_per_ancestry_without_replacement_v1"
    )
    split_policy: Literal["frozen_ancestry_component_atomic"] = "frozen_ancestry_component_atomic"
    max_unique_variants_per_ancestry: Literal[4] = 4
    duplicate_policy: Literal["record_id_unique_no_oversampling"] = (
        "record_id_unique_no_oversampling"
    )
    target_balance_policy: Literal["exact_equal_count_per_complete_batch_v1"] = (
        "exact_equal_count_per_complete_batch_v1"
    )
    loss_weighting: Literal["ancestry_total_one_v1"] = "ancestry_total_one_v1"
    loss_normalization: Literal["fixed_epoch_ancestry_mean_v1"] = "fixed_epoch_ancestry_mean_v1"
    positive_fraction_per_batch: float = Field(default=0.5, ge=0.5, le=0.5)
    optimizer: Literal["AdamW"] = "AdamW"
    learning_rate: float = Field(default=1e-05, ge=1e-05, le=1e-05)
    weight_decay: float = Field(default=0.01, ge=0.01, le=0.01)
    batch_size: Literal[32] = 32
    microbatch_size: Literal[4] = 4
    gradient_accumulation_steps: Literal[8] = 8
    epochs: Literal[1] = 1
    seed: Literal[1729] = 1729
    hyperparameter_tuning: Literal[False] = False
    checkpoint_selection: Literal[False] = False

    @field_validator("diagnostic_splits")
    @classmethod
    def _diagnostics_are_exact(
        cls, value: tuple[Literal["validation", "test"], ...]
    ) -> tuple[Literal["validation", "test"], ...]:
        if value != ("validation", "test"):
            raise ValueError("proxy diagnostic splits must be exactly validation,test")
        return value

    @model_validator(mode="after")
    def _batching_is_coherent(self) -> Self:
        if self.microbatch_size * self.gradient_accumulation_steps != self.batch_size:
            raise ValueError("M0 microbatching must exactly equal the effective batch size")
        return self


class ExperimentalM0ProxyProtocolConfig(ExperimentalM0ProxyBoundary):
    """Frozen protocol; exact corpus/audit bindings are supplied per run."""

    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1)
    protocol_status: Literal["frozen_pending_exact_run_bindings"] = (
        "frozen_pending_exact_run_bindings"
    )
    required_opt_in_flag: Literal["--allow-experimental-mixed-supervision"] = (
        "--allow-experimental-mixed-supervision"
    )
    backbone_registry_path: str = Field(min_length=1)
    backbone_registry_sha256: str = Field(pattern=_HEX64)
    tokenizer_audit_profile_id: str = Field(min_length=1)
    backbone: M0ProxyBackboneProtocol
    architecture: M0ProxyArchitectureProtocol
    training: M0ProxyTrainingProtocol

    @field_validator("backbone_registry_path")
    @classmethod
    def _registry_path_is_relative(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("backbone_registry_path must be repository-relative")
        return value


def load_experimental_m0_proxy_config(
    path: Path,
) -> LoadedConfig[ExperimentalM0ProxyProtocolConfig]:
    """Load the proxy-only M0 protocol."""

    return load_config(path, ExperimentalM0ProxyProtocolConfig)


class M0ProxyRunBinding(StrictModel):
    """Exact corpus and tokenizer-audit artifacts required before preparation."""

    schema_version: Literal[1] = 1
    corpus_dir: str
    dataset_id: str = Field(pattern=_DATASET_ID)
    corpus_manifest_sha256: str = Field(pattern=_HEX64)
    tokenizer_audit_dir: str
    tokenizer_audit_id: str = Field(pattern=_AUDIT_ID)
    tokenizer_audit_manifest_sha256: str = Field(pattern=_HEX64)
    tokenizer_audit_summary_sha256: str = Field(pattern=_HEX64)

    @field_validator("corpus_dir", "tokenizer_audit_dir")
    @classmethod
    def _artifact_dirs_are_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("M0 run artifact directories must be absolute")
        return value

    def portable_content(self) -> M0ProxyContentBinding:
        """Remove operational filesystem locators from scientific identity."""

        return M0ProxyContentBinding(
            dataset_id=self.dataset_id,
            corpus_manifest_sha256=self.corpus_manifest_sha256,
            tokenizer_audit_id=self.tokenizer_audit_id,
            tokenizer_audit_manifest_sha256=self.tokenizer_audit_manifest_sha256,
            tokenizer_audit_summary_sha256=self.tokenizer_audit_summary_sha256,
        )


class M0ProxyContentBinding(StrictModel):
    """Portable byte identities for inputs; contains no local paths."""

    schema_version: Literal[1] = 1
    dataset_id: str = Field(pattern=_DATASET_ID)
    corpus_manifest_sha256: str = Field(pattern=_HEX64)
    tokenizer_audit_id: str = Field(pattern=_AUDIT_ID)
    tokenizer_audit_manifest_sha256: str = Field(pattern=_HEX64)
    tokenizer_audit_summary_sha256: str = Field(pattern=_HEX64)


class M0ProxyInputBinding(StrictModel):
    path: str
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=1)

    def portable_pin(self) -> M0ProxyInputPin:
        return M0ProxyInputPin(sha256=self.sha256, byte_count=self.byte_count)


class M0ProxyInputPin(StrictModel):
    """One portable input file pin."""

    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=1)


class M0ProxyTokenizerDecision(StrictModel):
    audit_id: str = Field(pattern=_AUDIT_ID)
    candidate_key: Literal["modernbert_base"] = "modernbert_base"
    model_id: Literal["answerdotai/ModernBERT-base"] = "answerdotai/ModernBERT-base"
    revision: Literal["8949b909ec900327062f0ebf497f51aef5e6f0c8"] = (
        "8949b909ec900327062f0ebf497f51aef5e6f0c8"
    )
    selected_length: Literal[512, 1024]
    eligible_at_selected_length: Literal[True] = True
    scientific_winner_selected: Literal[False] = False
    snapshot_content_hash: str = Field(pattern=_HEX64)


class M0ProxyExample(ExperimentalM0ProxyBoundary):
    """One token-counted pair admitted to the proxy-only M0 input bundle."""

    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=_RECORD_ID)
    split_component_id: str = Field(pattern=_COMPONENT_ID)
    split: ProxySplit
    pseudo_target: ProxyTarget
    source_text: str = Field(min_length=1)
    candidate_text: str = Field(min_length=1)
    source_text_sha256: str = Field(pattern=_HEX64)
    candidate_text_sha256: str = Field(pattern=_HEX64)
    source_token_count: int = Field(ge=1)
    candidate_token_count: int = Field(ge=1)
    selected_length: Literal[512, 1024]
    long_input: bool
    proxy_training_eligible: bool
    private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool

    @model_validator(mode="after")
    def _example_is_coherent(self) -> Self:
        if not self.source_text.startswith(_HEADLESS_MARKER) or not self.candidate_text.startswith(
            _HEADLESS_MARKER
        ):
            raise ValueError("M0 proxy text must contain only the tagged headless view")
        if self.source_text_sha256 != hashlib.sha256(self.source_text.encode()).hexdigest():
            raise ValueError("source text hash differs")
        if self.candidate_text_sha256 != hashlib.sha256(self.candidate_text.encode()).hexdigest():
            raise ValueError("candidate text hash differs")
        expected_long = (
            max(self.source_token_count, self.candidate_token_count) > self.selected_length
        )
        if self.long_input != expected_long:
            raise ValueError("long_input differs from token counts and selected length")
        expected_training = self.split == "train" and not self.long_input
        if self.proxy_training_eligible != expected_training:
            raise ValueError("proxy training eligibility differs from split/length policy")
        if self.private_source_content and self.external_transmission_allowed:
            raise ValueError("private M0 proxy content cannot be externally transmitted")
        if self.release_eligible and (
            self.private_source_content or not self.redistribution_allowed
        ):
            raise ValueError("M0 proxy release policy is incoherent")
        return self


class M0ProxyInputSummary(ExperimentalM0ProxyBoundary):
    schema_version: Literal[1] = 1
    dataset_id: str = Field(pattern=_DATASET_ID)
    tokenizer_audit_id: str = Field(pattern=_AUDIT_ID)
    selected_length: Literal[512, 1024]
    record_count: int = Field(gt=0)
    training_record_count: int = Field(gt=0)
    training_component_count: int = Field(gt=0)
    long_input_count: int = Field(ge=0)
    counts_by_split: dict[str, int]
    counts_by_target: dict[str, int]
    training_counts_by_target: dict[str, int]
    long_input_counts_by_split: dict[str, int]
    record_set_sha256: str = Field(pattern=_HEX64)
    training_record_set_sha256: str = Field(pattern=_HEX64)
    training_component_set_sha256: str = Field(pattern=_HEX64)
    use_note: Literal["machine-proxy M0 diagnostic only; not semantic training or evaluation"] = (
        "machine-proxy M0 diagnostic only; not semantic training or evaluation"
    )

    @model_validator(mode="after")
    def _counts_reconcile(self) -> Self:
        if tuple(self.counts_by_split) != _SPLITS:
            raise ValueError("summary split counts must be canonical")
        if tuple(self.counts_by_target) != _TARGETS:
            raise ValueError("summary target counts must be canonical")
        if tuple(self.training_counts_by_target) != _TARGETS:
            raise ValueError("training target counts must be canonical")
        if tuple(self.long_input_counts_by_split) != _SPLITS:
            raise ValueError("long-input split counts must be canonical")
        if sum(self.counts_by_split.values()) != self.record_count:
            raise ValueError("summary split counts do not reconcile")
        if sum(self.counts_by_target.values()) != self.record_count:
            raise ValueError("summary target counts do not reconcile")
        if sum(self.training_counts_by_target.values()) != self.training_record_count:
            raise ValueError("summary training target counts do not reconcile")
        if sum(self.long_input_counts_by_split.values()) != self.long_input_count:
            raise ValueError("summary long-input counts do not reconcile")
        if any(value <= 0 for value in self.counts_by_split.values()):
            raise ValueError("proxy input preparation requires train, validation, and test")
        if any(value <= 0 for value in self.training_counts_by_target.values()):
            raise ValueError("proxy training requires both pseudo-targets")
        return self


EpochOmissionReason = Literal["ancestry_cap", "class_balance", "incomplete_batch"]
EpochSelectionStatus = Literal["selected", "omitted"]


class M0EpochSelectionRecord(StrictModel):
    """Complete, text-free accounting for one eligible training example."""

    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=_RECORD_ID)
    split_component_id: str = Field(pattern=_COMPONENT_ID)
    pseudo_target: ProxyTarget
    selection_status: EpochSelectionStatus
    omission_reason: EpochOmissionReason | None = None
    epoch_position: int | None = Field(default=None, ge=0)
    batch_index: int | None = Field(default=None, ge=0)
    loss_weight: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _selection_is_coherent(self) -> Self:
        selected = self.selection_status == "selected"
        selected_values = (self.epoch_position, self.batch_index, self.loss_weight)
        if selected and (
            self.omission_reason is not None or any(v is None for v in selected_values)
        ):
            raise ValueError("selected epoch row lacks schedule fields or has an omission")
        if not selected and (
            self.omission_reason is None or any(v is not None for v in selected_values)
        ):
            raise ValueError("omitted epoch row lacks its exclusive omission reason")
        return self


class M0EpochSchedule(StrictModel):
    """Deterministic, fully accounted one-epoch capped/balanced schedule."""

    schema_version: Literal[2] = 2
    algorithm: Literal["hash_rank_max4_balanced_batches_v2"] = "hash_rank_max4_balanced_batches_v2"
    seed: int = Field(ge=0)
    batch_size: int = Field(gt=0)
    max_unique_variants_per_ancestry: Literal[4] = 4
    input_record_set_sha256: str = Field(pattern=_HEX64)
    selected_record_set_sha256: str = Field(pattern=_HEX64)
    omitted_record_set_sha256: str = Field(pattern=_HEX64)
    record_count: int = Field(gt=0)
    selected_count: int = Field(gt=0)
    omitted_count: int = Field(ge=0)
    batch_count: int = Field(gt=0)
    selected_component_count: int = Field(gt=0)
    loss_normalization_weight: float = Field(gt=0.0)
    selected_counts_by_target: dict[str, int]
    omission_counts_by_reason: dict[str, int]
    records: tuple[M0EpochSelectionRecord, ...] = Field(min_length=1)
    schedule_sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _schedule_is_coherent(self) -> Self:
        if self.batch_size % 2:
            raise ValueError("M0 schedule batch_size must be even")
        if self.records != tuple(sorted(self.records, key=lambda item: item.record_id)):
            raise ValueError("M0 schedule records must be ID-sorted")
        if len({item.record_id for item in self.records}) != len(self.records):
            raise ValueError("M0 schedule repeats a record")
        selected = tuple(item for item in self.records if item.selection_status == "selected")
        omitted = tuple(item for item in self.records if item.selection_status == "omitted")
        if self.record_count != len(self.records):
            raise ValueError("M0 schedule record count differs")
        if self.selected_count != len(selected) or self.omitted_count != len(omitted):
            raise ValueError("M0 schedule selected/omitted counts differ")
        if self.selected_count + self.omitted_count != self.record_count:
            raise ValueError("M0 schedule accounting does not reconcile")
        expected_target = Counter(item.pseudo_target for item in selected)
        if self.selected_counts_by_target != {key: expected_target[key] for key in _TARGETS}:
            raise ValueError("M0 schedule target accounting differs")
        if len(set(self.selected_counts_by_target.values())) != 1:
            raise ValueError("M0 schedule is not exactly target-balanced")
        expected_omissions = Counter(item.omission_reason for item in omitted)
        canonical_omissions = {
            key: expected_omissions[key]
            for key in ("ancestry_cap", "class_balance", "incomplete_batch")
        }
        if self.omission_counts_by_reason != canonical_omissions:
            raise ValueError("M0 schedule omission accounting differs")
        ordered = tuple(sorted(selected, key=lambda item: cast(int, item.epoch_position)))
        if tuple(item.epoch_position for item in ordered) != tuple(range(len(ordered))):
            raise ValueError("M0 epoch positions are not contiguous")
        if len(ordered) != self.batch_count * self.batch_size:
            raise ValueError("M0 schedule does not contain complete batches")
        for batch_index in range(self.batch_count):
            batch = ordered[batch_index * self.batch_size : (batch_index + 1) * self.batch_size]
            if any(item.batch_index != batch_index for item in batch):
                raise ValueError("M0 batch indices differ from epoch positions")
            if Counter(item.pseudo_target for item in batch) != {
                "not_same_claim": self.batch_size // 2,
                "same_claim": self.batch_size // 2,
            }:
                raise ValueError("M0 batch is not exactly target-balanced")
        component_totals: dict[str, float] = defaultdict(float)
        component_sizes: Counter[str] = Counter()
        for item in selected:
            assert item.loss_weight is not None
            component_totals[item.split_component_id] += item.loss_weight
            component_sizes[item.split_component_id] += 1
        if any(size > self.max_unique_variants_per_ancestry for size in component_sizes.values()):
            raise ValueError("M0 schedule exceeds the ancestry cap")
        if any(not math.isclose(total, 1.0, abs_tol=1e-12) for total in component_totals.values()):
            raise ValueError("M0 selected ancestry loss weight does not total one")
        if self.selected_component_count != len(component_totals):
            raise ValueError("M0 selected ancestry count differs")
        expected_normalizer = self.selected_component_count / self.batch_count
        if not math.isclose(
            self.loss_normalization_weight, expected_normalizer, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("M0 fixed epoch loss normalizer differs")
        if self.input_record_set_sha256 != _id_set_hash(
            "experimental_m0_epoch_input_v1", [item.record_id for item in self.records]
        ):
            raise ValueError("M0 schedule input set hash differs")
        if self.selected_record_set_sha256 != _id_set_hash(
            "experimental_m0_epoch_selected_v1", [item.record_id for item in selected]
        ):
            raise ValueError("M0 schedule selected set hash differs")
        if self.omitted_record_set_sha256 != _id_set_hash(
            "experimental_m0_epoch_omitted_v1", [item.record_id for item in omitted]
        ):
            raise ValueError("M0 schedule omitted set hash differs")
        expected_hash = hash_canonical(self.model_dump(mode="json", exclude={"schedule_sha256"}))
        if self.schedule_sha256 != expected_hash:
            raise ValueError("M0 schedule hash differs")
        return self


class M0ProxyInputManifest(ExperimentalM0ProxyBoundary):
    """Content-addressed input preparation; no model checkpoint is claimed."""

    schema_version: Literal[2] = 2
    artifact_id: str = Field(pattern=_ARTIFACT_ID)
    artifact_kind: Literal["experimental_m0_proxy_inputs_v1"] = "experimental_m0_proxy_inputs_v1"
    protocol_config_hash: str = Field(pattern=_HEX64)
    protocol: ExperimentalM0ProxyProtocolConfig
    content_binding: M0ProxyContentBinding
    dataset_id: str = Field(pattern=_DATASET_ID)
    tokenizer_decision: M0ProxyTokenizerDecision
    code: CodeState
    inputs: dict[str, M0ProxyInputPin]
    record_count: int = Field(gt=0)
    training_record_count: int = Field(gt=0)
    output_sha256: dict[str, str]
    model_weights_loaded: Literal[False] = False
    training_executed: Literal[False] = False

    @model_validator(mode="after")
    def _manifest_is_coherent(self) -> Self:
        if self.protocol_config_hash != hash_canonical(self.protocol.model_dump(mode="json")):
            raise ValueError("M0 protocol hash differs from embedded protocol")
        if self.dataset_id != self.content_binding.dataset_id:
            raise ValueError("M0 dataset identity differs from the run binding")
        if self.tokenizer_decision.audit_id != self.content_binding.tokenizer_audit_id:
            raise ValueError("M0 tokenizer decision differs from the run binding")
        if self.code.git_dirty or self.code.code_tree_hash is None or self.code.untracked_files:
            raise ValueError("M0 proxy input freeze requires clean tracked code")
        if not self.inputs or list(self.inputs) != sorted(self.inputs):
            raise ValueError("M0 inputs must be nonempty and key-sorted")
        expected_inputs = {
            "backbone_registry",
            "corpus_manifest",
            "corpus_records",
            "tokenizer_audit_manifest",
            "tokenizer_audit_summary",
        }
        if set(self.inputs) != expected_inputs:
            raise ValueError("M0 manifest input set is not exact")
        if self.inputs["backbone_registry"].sha256 != self.protocol.backbone_registry_sha256:
            raise ValueError("M0 backbone-registry binding differs from protocol")
        if self.inputs["corpus_manifest"].sha256 != self.content_binding.corpus_manifest_sha256:
            raise ValueError("M0 corpus-manifest binding differs from run binding")
        if (
            self.inputs["tokenizer_audit_manifest"].sha256
            != self.content_binding.tokenizer_audit_manifest_sha256
            or self.inputs["tokenizer_audit_summary"].sha256
            != self.content_binding.tokenizer_audit_summary_sha256
        ):
            raise ValueError("M0 tokenizer-audit bindings differ from run binding")
        if set(self.output_sha256) != _NON_MANIFEST_OUTPUTS:
            raise ValueError("M0 manifest must bind exactly the non-manifest outputs")
        expected = "experimental-m0-proxy-inputs:" + hash_canonical(
            self.model_dump(mode="json", exclude={"artifact_id"})
        )
        if self.artifact_id != expected:
            raise ValueError("M0 input artifact_id differs from canonical content")
        return self


@dataclass(frozen=True, slots=True)
class M0ProxyPreparedArtifacts:
    output_dir: Path
    artifact_id: str
    selected_length: Literal[512, 1024]
    record_count: int
    training_record_count: int
    replayed: bool


class _Tokenizer(Protocol):
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]: ...


def _regular_file(path: Path) -> Path:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ExperimentalM0ProxyError(f"required file is unavailable: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ExperimentalM0ProxyError(f"required path is not a regular file: {path}")
    return path


def _real_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ExperimentalM0ProxyError(f"required directory is unavailable: {path}") from exc
    if not resolved.is_dir():
        raise ExperimentalM0ProxyError(f"required path is not a directory: {path}")
    return resolved


def _strict_json(path: Path) -> object:
    try:
        return json.loads(_regular_file(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExperimentalM0ProxyError(f"invalid JSON: {path}") from exc


def _canonical_jsonl(models: Sequence[StrictModel]) -> bytes:
    return b"".join(canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in models)


def _load_mixed_records(path: Path) -> tuple[ExperimentalMixedSupervisionRecord, ...]:
    records: list[ExperimentalMixedSupervisionRecord] = []
    with _regular_file(path).open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                value = json.loads(raw)
                record = ExperimentalMixedSupervisionRecord.model_validate(value)
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ExperimentalM0ProxyError(
                    f"invalid mixed record at {path}:{line_number}"
                ) from exc
            if raw != canonical_json_bytes(record.model_dump(mode="json")) + b"\n":
                raise ExperimentalM0ProxyError(f"noncanonical mixed record at {path}:{line_number}")
            records.append(record)
    result = tuple(records)
    if not result or result != tuple(sorted(result, key=lambda item: item.record_id)):
        raise ExperimentalM0ProxyError("mixed records must be nonempty and ID-sorted")
    return result


def _input_binding(path: Path, expected_sha256: str | None = None) -> M0ProxyInputBinding:
    file_path = _regular_file(path)
    actual = hash_file(file_path)
    if expected_sha256 is not None and actual != expected_sha256:
        raise ExperimentalM0ProxyError(f"input hash differs: {file_path}")
    return M0ProxyInputBinding(
        path=str(file_path),
        sha256=actual,
        byte_count=file_path.stat().st_size,
    )


def _verify_clean_code(code: CodeState) -> str:
    if code.git_dirty or code.code_tree_hash is None or code.untracked_files:
        raise ExperimentalM0ProxyError("M0 proxy input freeze requires a clean tracked repository")
    return code.code_tree_hash


def _id_set_hash(schema: str, values: Sequence[str]) -> str:
    canonical = tuple(sorted(values))
    if canonical != tuple(sorted(set(canonical))):
        raise ExperimentalM0ProxyError(f"{schema} values are not unique")
    return hash_canonical({"schema": schema, "ids": canonical})


def _component_split_map(
    records: Sequence[ExperimentalMixedSupervisionRecord],
) -> dict[str, ProxySplit]:
    result: dict[str, ProxySplit] = {}
    for record in records:
        observed = result.setdefault(record.split_component_id, record.split)
        if observed != record.split:
            raise ExperimentalM0ProxyError(
                f"ancestry component crosses splits: {record.split_component_id}"
            )
    return result


def _make_examples(
    records: Sequence[ExperimentalMixedSupervisionRecord],
    *,
    tokenizer: _Tokenizer,
    selected_length: Literal[512, 1024],
) -> tuple[M0ProxyExample, ...]:
    """Build tagged, token-counted examples without reading proxy provenance as input."""

    _component_split_map(records)
    examples: list[M0ProxyExample] = []
    for record in records:
        if not record.experimental_training_eligible or record.scientific_training_eligible:
            raise ExperimentalM0ProxyError("mixed record has an incompatible eligibility boundary")
        if record.model_input_profile != "headless_only_v1":
            raise ExperimentalM0ProxyError("M0 proxy accepts headless_only_v1 records only")
        source = _HEADLESS_MARKER + record.source.headless
        candidate = _HEADLESS_MARKER + record.candidate.headless
        source_tokens = len(tokenizer.encode(source, add_special_tokens=True))
        candidate_tokens = len(tokenizer.encode(candidate, add_special_tokens=True))
        if source_tokens < 1 or candidate_tokens < 1:
            raise ExperimentalM0ProxyError("tokenizer returned an empty statement")
        long_input = max(source_tokens, candidate_tokens) > selected_length
        examples.append(
            M0ProxyExample(
                record_id=record.record_id,
                split_component_id=record.split_component_id,
                split=record.split,
                pseudo_target=record.pseudo_target,
                source_text=source,
                candidate_text=candidate,
                source_text_sha256=hashlib.sha256(source.encode()).hexdigest(),
                candidate_text_sha256=hashlib.sha256(candidate.encode()).hexdigest(),
                source_token_count=source_tokens,
                candidate_token_count=candidate_tokens,
                selected_length=selected_length,
                long_input=long_input,
                proxy_training_eligible=record.split == "train" and not long_input,
                private_source_content=record.private_source_content,
                redistribution_allowed=record.redistribution_allowed,
                external_transmission_allowed=record.external_transmission_allowed,
                release_eligible=record.release_eligible,
            )
        )
    result = tuple(examples)
    if result != tuple(sorted(result, key=lambda item: item.record_id)):
        raise ExperimentalM0ProxyError("M0 examples differ from canonical record order")
    return result


def _summary(
    examples: Sequence[M0ProxyExample],
    *,
    dataset_id: str,
    audit_id: str,
    selected_length: Literal[512, 1024],
) -> M0ProxyInputSummary:
    if not examples:
        raise ExperimentalM0ProxyError("M0 proxy preparation has no examples")
    _component_split_map_from_examples(examples)
    training = tuple(item for item in examples if item.proxy_training_eligible)
    if not training:
        raise ExperimentalM0ProxyError("M0 proxy preparation has no eligible training examples")
    split_counts = Counter(item.split for item in examples)
    target_counts = Counter(item.pseudo_target for item in examples)
    training_target_counts = Counter(item.pseudo_target for item in training)
    long_counts = Counter(item.split for item in examples if item.long_input)
    components = tuple(sorted({item.split_component_id for item in training}))
    return M0ProxyInputSummary(
        dataset_id=dataset_id,
        tokenizer_audit_id=audit_id,
        selected_length=selected_length,
        record_count=len(examples),
        training_record_count=len(training),
        training_component_count=len(components),
        long_input_count=sum(item.long_input for item in examples),
        counts_by_split={key: split_counts[key] for key in _SPLITS},
        counts_by_target={key: target_counts[key] for key in _TARGETS},
        training_counts_by_target={key: training_target_counts[key] for key in _TARGETS},
        long_input_counts_by_split={key: long_counts[key] for key in _SPLITS},
        record_set_sha256=_id_set_hash(
            "experimental_m0_proxy_record_set_v1", [item.record_id for item in examples]
        ),
        training_record_set_sha256=_id_set_hash(
            "experimental_m0_proxy_training_record_set_v1",
            [item.record_id for item in training],
        ),
        training_component_set_sha256=_id_set_hash(
            "experimental_m0_proxy_training_component_set_v1", components
        ),
    )


def _component_split_map_from_examples(examples: Sequence[M0ProxyExample]) -> dict[str, ProxySplit]:
    result: dict[str, ProxySplit] = {}
    for example in examples:
        observed = result.setdefault(example.split_component_id, example.split)
        if observed != example.split:
            raise ExperimentalM0ProxyError(
                f"prepared ancestry component crosses splits: {example.split_component_id}"
            )
    return result


def ancestry_normalized_proxy_weights(examples: Sequence[M0ProxyExample]) -> tuple[float, ...]:
    """Give every represented ancestry component total static loss weight one."""

    if not examples:
        raise ExperimentalM0ProxyError("proxy loss weighting requires examples")
    if any(not item.proxy_training_eligible or item.split != "train" for item in examples):
        raise ExperimentalM0ProxyError("proxy loss weights accept non-long train records only")
    _component_split_map_from_examples(examples)
    sizes = Counter(item.split_component_id for item in examples)
    weights = tuple(1.0 / sizes[item.split_component_id] for item in examples)
    totals: dict[str, float] = defaultdict(float)
    for item, weight in zip(examples, weights, strict=True):
        totals[item.split_component_id] += weight
    if any(not math.isclose(value, 1.0, abs_tol=1e-12) for value in totals.values()):
        raise ExperimentalM0ProxyError("ancestry loss weight does not total one")
    return weights


def _hash_rank(*parts: object) -> str:
    payload = "\x00".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_m0_epoch_schedule(
    examples: Sequence[M0ProxyExample],
    *,
    batch_size: int,
    seed: int,
    max_unique_variants_per_ancestry: int = 4,
) -> M0EpochSchedule:
    """Cap, balance, and batch exactly once while accounting for every omission."""

    if batch_size <= 0 or batch_size % 2:
        raise ExperimentalM0ProxyError("M0 epoch batch_size must be positive and even")
    if seed < 0 or max_unique_variants_per_ancestry != 4:
        raise ExperimentalM0ProxyError("M0 epoch requires nonnegative seed and frozen cap four")
    if not examples:
        raise ExperimentalM0ProxyError("M0 epoch scheduling requires examples")
    if any(not item.proxy_training_eligible or item.split != "train" for item in examples):
        raise ExperimentalM0ProxyError("M0 epoch scheduling accepts non-long train records only")
    canonical = tuple(sorted(examples, key=lambda item: item.record_id))
    if len({item.record_id for item in canonical}) != len(canonical):
        raise ExperimentalM0ProxyError("M0 epoch input repeats a record ID")
    _component_split_map_from_examples(canonical)

    by_component: dict[str, list[M0ProxyExample]] = defaultdict(list)
    for item in canonical:
        by_component[item.split_component_id].append(item)
    capped: list[M0ProxyExample] = []
    omission_reason: dict[str, EpochOmissionReason] = {}
    for component_id, items in sorted(by_component.items()):
        ranked = sorted(
            items,
            key=lambda item: (
                _hash_rank(seed, "ancestry_cap", component_id, item.record_id),
                item.record_id,
            ),
        )
        capped.extend(ranked[:max_unique_variants_per_ancestry])
        for item in ranked[max_unique_variants_per_ancestry:]:
            omission_reason[item.record_id] = "ancestry_cap"

    by_target = {
        target: sorted(
            (item for item in capped if item.pseudo_target == target),
            key=lambda item: (
                _hash_rank(seed, "target_balance", target, item.record_id),
                item.record_id,
            ),
        )
        for target in _TARGETS
    }
    if any(not items for items in by_target.values()):
        raise ExperimentalM0ProxyError("M0 epoch schedule requires both pseudo-targets")
    equal_count = min(len(items) for items in by_target.values())
    for target, items in by_target.items():
        for item in items[equal_count:]:
            omission_reason[item.record_id] = "class_balance"
        by_target[target] = items[:equal_count]

    per_class = batch_size // 2
    usable_per_class = equal_count - equal_count % per_class
    if usable_per_class == 0:
        raise ExperimentalM0ProxyError("M0 epoch has no complete balanced batch")
    for target, items in by_target.items():
        for item in items[usable_per_class:]:
            omission_reason[item.record_id] = "incomplete_batch"
        by_target[target] = items[:usable_per_class]

    selected_order: list[M0ProxyExample] = []
    for batch_index, start in enumerate(range(0, usable_per_class, per_class)):
        batch = (
            by_target["not_same_claim"][start : start + per_class]
            + by_target["same_claim"][start : start + per_class]
        )
        batch = sorted(
            batch,
            key=lambda item: (
                _hash_rank(seed, "batch", batch_index, item.record_id),
                item.record_id,
            ),
        )
        selected_order.extend(batch)
    selected_positions = {item.record_id: position for position, item in enumerate(selected_order)}
    selected_component_sizes = Counter(item.split_component_id for item in selected_order)
    rows: list[M0EpochSelectionRecord] = []
    for item in canonical:
        if item.record_id in selected_positions:
            position = selected_positions[item.record_id]
            rows.append(
                M0EpochSelectionRecord(
                    record_id=item.record_id,
                    split_component_id=item.split_component_id,
                    pseudo_target=item.pseudo_target,
                    selection_status="selected",
                    epoch_position=position,
                    batch_index=position // batch_size,
                    loss_weight=1.0 / selected_component_sizes[item.split_component_id],
                )
            )
        else:
            reason = omission_reason.get(item.record_id)
            if reason is None:
                raise ExperimentalM0ProxyError(
                    f"M0 epoch failed to account for eligible record {item.record_id}"
                )
            rows.append(
                M0EpochSelectionRecord(
                    record_id=item.record_id,
                    split_component_id=item.split_component_id,
                    pseudo_target=item.pseudo_target,
                    selection_status="omitted",
                    omission_reason=reason,
                )
            )
    rows_tuple = tuple(sorted(rows, key=lambda item: item.record_id))
    selected_rows = tuple(item for item in rows_tuple if item.selection_status == "selected")
    omitted_rows = tuple(item for item in rows_tuple if item.selection_status == "omitted")
    data: dict[str, object] = {
        "schema_version": 2,
        "algorithm": "hash_rank_max4_balanced_batches_v2",
        "seed": seed,
        "batch_size": batch_size,
        "max_unique_variants_per_ancestry": 4,
        "input_record_set_sha256": _id_set_hash(
            "experimental_m0_epoch_input_v1", [item.record_id for item in rows_tuple]
        ),
        "selected_record_set_sha256": _id_set_hash(
            "experimental_m0_epoch_selected_v1", [item.record_id for item in selected_rows]
        ),
        "omitted_record_set_sha256": _id_set_hash(
            "experimental_m0_epoch_omitted_v1", [item.record_id for item in omitted_rows]
        ),
        "record_count": len(rows_tuple),
        "selected_count": len(selected_rows),
        "omitted_count": len(omitted_rows),
        "batch_count": len(selected_rows) // batch_size,
        "selected_component_count": len(selected_component_sizes),
        "loss_normalization_weight": (
            len(selected_component_sizes) / (len(selected_rows) // batch_size)
        ),
        "selected_counts_by_target": {
            key: Counter(item.pseudo_target for item in selected_rows)[key] for key in _TARGETS
        },
        "omission_counts_by_reason": {
            key: Counter(item.omission_reason for item in omitted_rows)[key]
            for key in ("ancestry_cap", "class_balance", "incomplete_batch")
        },
        "records": tuple(item.model_dump(mode="json") for item in rows_tuple),
    }
    return M0EpochSchedule.model_validate({**data, "schedule_sha256": hash_canonical(data)})


def balanced_proxy_batches(
    examples: Sequence[M0ProxyExample],
    *,
    batch_size: int,
    seed: int,
) -> tuple[tuple[M0ProxyExample, ...], ...]:
    """Convenience view over the fully accounted deterministic epoch schedule."""

    schedule = build_m0_epoch_schedule(examples, batch_size=batch_size, seed=seed)
    by_id = {item.record_id: item for item in examples}
    selected = sorted(
        (item for item in schedule.records if item.selection_status == "selected"),
        key=lambda item: cast(int, item.epoch_position),
    )
    return tuple(
        tuple(by_id[item.record_id] for item in selected[start : start + batch_size])
        for start in range(0, len(selected), batch_size)
    )


def _tokenizer_decision(
    *,
    protocol: ExperimentalM0ProxyProtocolConfig,
    run_binding: M0ProxyRunBinding,
    audit_manifest: TokenizerAuditManifest,
    audit_summary: TokenizerAuditSummary,
) -> M0ProxyTokenizerDecision:
    if audit_manifest.audit_id != run_binding.tokenizer_audit_id:
        raise ExperimentalM0ProxyError("tokenizer audit ID differs from run binding")
    if audit_summary.audit_id != run_binding.tokenizer_audit_id:
        raise ExperimentalM0ProxyError("tokenizer summary ID differs from run binding")
    if (
        audit_summary.profile_id != audit_manifest.profile_id
        or audit_summary.selected_length != audit_manifest.selected_length
    ):
        raise ExperimentalM0ProxyError("tokenizer audit summary differs from its manifest")
    if audit_manifest.profile_id != protocol.tokenizer_audit_profile_id:
        raise ExperimentalM0ProxyError("tokenizer audit profile differs from M0 protocol")
    if audit_summary.scientific_winner_selected or audit_manifest.scientific_winner_selected:
        raise ExperimentalM0ProxyError("tokenizer audit unexpectedly selected a scientific winner")
    key = protocol.backbone.candidate_key
    if key not in audit_summary.eligible_candidates:
        raise ExperimentalM0ProxyError("ModernBERT-base is not eligible at selected length")
    summaries = {item.candidate: item for item in audit_summary.candidate_summaries}
    if key not in summaries or key not in audit_manifest.snapshots:
        raise ExperimentalM0ProxyError("tokenizer audit omits ModernBERT-base")
    candidate = summaries[key]
    snapshot = audit_manifest.snapshots[key]
    if (
        candidate.model_id != protocol.backbone.model_id
        or candidate.revision != protocol.backbone.revision
        or snapshot.model_id != protocol.backbone.model_id
        or snapshot.revision != protocol.backbone.revision
        or not candidate.eligible_at_selected_length
    ):
        raise ExperimentalM0ProxyError("ModernBERT-base audit binding differs from protocol")
    return M0ProxyTokenizerDecision(
        audit_id=audit_manifest.audit_id,
        selected_length=audit_manifest.selected_length,
        snapshot_content_hash=snapshot.snapshot_content_hash,
    )


def _load_audited_tokenizer(snapshot: SnapshotBinding) -> _Tokenizer:
    _verify_audited_tokenizer_snapshot(snapshot)
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:  # pragma: no cover - minimal install
        raise ExperimentalM0ProxyError(
            "M0 preparation requires the optional local-inference tokenizer runtime"
        ) from exc
    try:
        tokenizer = AutoTokenizer.from_pretrained(  # type: ignore[no-untyped-call]
            snapshot.path,
            local_files_only=True,
            trust_remote_code=False,
            use_fast=snapshot.use_fast,
        )
    except Exception as exc:
        raise ExperimentalM0ProxyError("cannot load the audited ModernBERT tokenizer") from exc
    return cast(_Tokenizer, tokenizer)


def _verify_audited_tokenizer_snapshot(snapshot: SnapshotBinding) -> Path:
    root = _real_directory(Path(snapshot.path))
    if root.name != snapshot.revision:
        raise ExperimentalM0ProxyError("audited tokenizer snapshot path differs from revision")
    for name, pin in snapshot.files.items():
        path = _regular_file(root / name)
        if hash_file(path) != pin.sha256 or path.stat().st_size != pin.byte_count:
            raise ExperimentalM0ProxyError(f"audited tokenizer file differs: {path}")
    observed_hash = hash_canonical(
        {name: pin.model_dump(mode="json") for name, pin in sorted(snapshot.files.items())}
    )
    if observed_hash != snapshot.snapshot_content_hash:
        raise ExperimentalM0ProxyError("audited tokenizer snapshot content hash differs")
    return root


def _artifact_id(payload: Mapping[str, object]) -> str:
    return "experimental-m0-proxy-inputs:" + hash_canonical(payload)


def _build_payloads(
    *,
    repository_root: Path,
    protocol: ExperimentalM0ProxyProtocolConfig,
    run_binding: M0ProxyRunBinding,
    corpus_manifest: ExperimentalMixedSupervisionManifest,
    audit_manifest: TokenizerAuditManifest,
    audit_summary: TokenizerAuditSummary,
    records: Sequence[ExperimentalMixedSupervisionRecord],
    tokenizer: _Tokenizer,
    code: CodeState,
    inputs: Mapping[str, M0ProxyInputBinding],
) -> tuple[dict[str, bytes], M0ProxyInputSummary]:
    _verify_clean_code(code)
    decision = _tokenizer_decision(
        protocol=protocol,
        run_binding=run_binding,
        audit_manifest=audit_manifest,
        audit_summary=audit_summary,
    )
    if corpus_manifest.dataset_id != run_binding.dataset_id:
        raise ExperimentalM0ProxyError("mixed corpus dataset ID differs from run binding")
    if corpus_manifest.record_count != len(records):
        raise ExperimentalM0ProxyError("mixed corpus record count differs from its manifest")
    if corpus_manifest.model_input_profile != "headless_only_v1":
        raise ExperimentalM0ProxyError("mixed corpus does not expose the required headless view")
    examples = _make_examples(
        records,
        tokenizer=tokenizer,
        selected_length=decision.selected_length,
    )
    summary = _summary(
        examples,
        dataset_id=corpus_manifest.dataset_id,
        audit_id=audit_manifest.audit_id,
        selected_length=decision.selected_length,
    )
    non_manifest = {
        "examples.jsonl": _canonical_jsonl(examples),
        "summary.json": canonical_json_bytes(summary.model_dump(mode="json")) + b"\n",
    }
    protocol_hash = hash_canonical(protocol.model_dump(mode="json"))
    manifest_data: dict[str, object] = {
        "schema_version": 2,
        "artifact_kind": "experimental_m0_proxy_inputs_v1",
        "protocol_config_hash": protocol_hash,
        "protocol": protocol.model_dump(mode="json"),
        "content_binding": run_binding.portable_content().model_dump(mode="json"),
        "dataset_id": corpus_manifest.dataset_id,
        "tokenizer_decision": decision.model_dump(mode="json"),
        "code": code.model_dump(mode="json"),
        "inputs": {
            name: binding.portable_pin().model_dump(mode="json")
            for name, binding in sorted(inputs.items())
        },
        "record_count": len(examples),
        "training_record_count": summary.training_record_count,
        "output_sha256": {
            name: hashlib.sha256(payload).hexdigest() for name, payload in non_manifest.items()
        },
        "model_weights_loaded": False,
        "training_executed": False,
        **ExperimentalM0ProxyBoundary().model_dump(mode="json"),
    }
    manifest = M0ProxyInputManifest.model_validate(
        {
            "artifact_id": _artifact_id(manifest_data),
            **manifest_data,
        }
    )
    return {
        **non_manifest,
        "manifest.json": canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    }, summary


def _resolve_bound_inputs(
    *,
    repository_root: Path,
    protocol: ExperimentalM0ProxyProtocolConfig,
    run_binding: M0ProxyRunBinding,
) -> tuple[
    ExperimentalMixedSupervisionManifest,
    TokenizerAuditManifest,
    TokenizerAuditSummary,
    tuple[ExperimentalMixedSupervisionRecord, ...],
    dict[str, M0ProxyInputBinding],
]:
    registry = repository_root / protocol.backbone_registry_path
    corpus = _real_directory(Path(run_binding.corpus_dir))
    audit = _real_directory(Path(run_binding.tokenizer_audit_dir))
    inputs = {
        "backbone_registry": _input_binding(registry, protocol.backbone_registry_sha256),
        "corpus_manifest": _input_binding(
            corpus / "manifest.json", run_binding.corpus_manifest_sha256
        ),
        "corpus_records": _input_binding(corpus / "records.jsonl"),
        "tokenizer_audit_manifest": _input_binding(
            audit / "manifest.json", run_binding.tokenizer_audit_manifest_sha256
        ),
        "tokenizer_audit_summary": _input_binding(
            audit / "summary.json", run_binding.tokenizer_audit_summary_sha256
        ),
    }
    corpus_manifest = verify_experimental_mixed_supervision(corpus)
    audit_manifest = verify_tokenizer_audit(audit, replay=False)
    audit_summary = TokenizerAuditSummary.model_validate(_strict_json(audit / "summary.json"))
    if corpus_manifest.dataset_id != run_binding.dataset_id:
        raise ExperimentalM0ProxyError("corpus verification returned a different dataset ID")
    records = _load_mixed_records(corpus / "records.jsonl")
    return corpus_manifest, audit_manifest, audit_summary, records, dict(sorted(inputs.items()))


def _reject_symlinks(path: Path, *, allow_missing: bool) -> Path:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        if not cursor.exists():
            if allow_missing:
                continue
            raise ExperimentalM0ProxyError(f"path component is unavailable: {cursor}")
        if cursor.is_symlink():
            raise ExperimentalM0ProxyError(f"output path contains a symlink: {cursor}")
    return absolute


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _verify_payloads(output: Path, payloads: Mapping[str, bytes]) -> bool:
    root = _real_directory(output)
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalM0ProxyError("existing M0 proxy input file set is not exact")
    for name, payload in sorted(payloads.items()):
        if _regular_file(root / name).read_bytes() != payload:
            raise ExperimentalM0ProxyError(f"existing M0 proxy input differs: {name}")
    return True


def _write_or_replay(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    if set(payloads) != _OUTPUT_FILES:
        raise ExperimentalM0ProxyError("M0 proxy output payload set is not exact")
    output = _reject_symlinks(output_dir, allow_missing=True)
    if output.exists():
        return _verify_payloads(output, payloads)
    output.parent.mkdir(parents=True, exist_ok=True)
    _real_directory(output.parent)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for name, payload in sorted(payloads.items()):
            path = temporary / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        try:
            os.rename(temporary, output)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            if temporary.exists():
                shutil.rmtree(temporary)
            return _verify_payloads(output, payloads)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return False


def prepare_experimental_m0_proxy_inputs(
    *,
    repository_root: Path,
    output_dir: Path,
    protocol: ExperimentalM0ProxyProtocolConfig,
    run_binding: M0ProxyRunBinding,
    allow_experimental_mixed_supervision: bool,
) -> M0ProxyPreparedArtifacts:
    """Prepare or exactly replay token-counted proxy inputs; never load model weights."""

    if not allow_experimental_mixed_supervision:
        raise ExperimentalM0ProxyError(
            "M0 proxy preparation requires --allow-experimental-mixed-supervision"
        )
    repository = _real_directory(repository_root)
    _regular_file(repository / "PLAN.md")
    _regular_file(repository / "pyproject.toml")
    output = _reject_symlinks(output_dir, allow_missing=True)
    for protected in (
        repository,
        _real_directory(Path(run_binding.corpus_dir)),
        _real_directory(Path(run_binding.tokenizer_audit_dir)),
    ):
        if _paths_overlap(output, protected):
            raise ExperimentalM0ProxyError(
                "M0 proxy output must be disjoint from repository and bound inputs"
            )
    corpus, audit, summary, records, inputs = _resolve_bound_inputs(
        repository_root=repository,
        protocol=protocol,
        run_binding=run_binding,
    )
    decision = _tokenizer_decision(
        protocol=protocol,
        run_binding=run_binding,
        audit_manifest=audit,
        audit_summary=summary,
    )
    tokenizer = _load_audited_tokenizer(audit.snapshots[decision.candidate_key])
    code = collect_code_state(repository)
    payloads, prepared_summary = _build_payloads(
        repository_root=repository,
        protocol=protocol,
        run_binding=run_binding,
        corpus_manifest=corpus,
        audit_manifest=audit,
        audit_summary=summary,
        records=records,
        tokenizer=tokenizer,
        code=code,
        inputs=inputs,
    )
    replayed = _write_or_replay(output, payloads)
    manifest = M0ProxyInputManifest.model_validate(_strict_json(output / "manifest.json"))
    return M0ProxyPreparedArtifacts(
        output_dir=output,
        artifact_id=manifest.artifact_id,
        selected_length=prepared_summary.selected_length,
        record_count=prepared_summary.record_count,
        training_record_count=prepared_summary.training_record_count,
        replayed=replayed,
    )


def verify_experimental_m0_proxy_inputs(
    output_dir: Path,
    *,
    repository_root: Path | None = None,
    input_locations: Mapping[str, Path] | None = None,
) -> M0ProxyInputManifest:
    """Verify the portable bundle, optionally rechecking code and relocated inputs."""

    root = _real_directory(output_dir)
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalM0ProxyError("M0 proxy input file set is not exact")
    manifest = M0ProxyInputManifest.model_validate(_strict_json(root / "manifest.json"))
    for name, expected in manifest.output_sha256.items():
        if hash_file(_regular_file(root / name)) != expected:
            raise ExperimentalM0ProxyError(f"M0 proxy output hash differs: {name}")
    examples: list[M0ProxyExample] = []
    with (root / "examples.jsonl").open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                example = M0ProxyExample.model_validate(json.loads(raw))
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ExperimentalM0ProxyError(
                    f"invalid M0 proxy example at line {line_number}"
                ) from exc
            if raw != canonical_json_bytes(example.model_dump(mode="json")) + b"\n":
                raise ExperimentalM0ProxyError(
                    f"noncanonical M0 proxy example at line {line_number}"
                )
            examples.append(example)
    summary = M0ProxyInputSummary.model_validate(_strict_json(root / "summary.json"))
    expected_summary = _summary(
        examples,
        dataset_id=manifest.dataset_id,
        audit_id=manifest.tokenizer_decision.audit_id,
        selected_length=manifest.tokenizer_decision.selected_length,
    )
    if summary != expected_summary:
        raise ExperimentalM0ProxyError("M0 proxy summary differs from examples")
    if manifest.record_count != len(examples):
        raise ExperimentalM0ProxyError("M0 proxy manifest record count differs")
    if manifest.training_record_count != summary.training_record_count:
        raise ExperimentalM0ProxyError("M0 proxy manifest training count differs")
    if repository_root is not None:
        observed_code = collect_code_state(_real_directory(repository_root))
        if observed_code != manifest.code:
            raise ExperimentalM0ProxyError("current repository code differs from M0 input freeze")
    if input_locations is not None:
        if set(input_locations) != set(manifest.inputs):
            raise ExperimentalM0ProxyError("relocated M0 input set is not exact")
        for name, pin in manifest.inputs.items():
            observed = _input_binding(input_locations[name])
            if observed.portable_pin() != pin:
                raise ExperimentalM0ProxyError(f"relocated M0 input differs: {name}")
    return manifest


class M0CheckpointFile(StrictModel):
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=1)


class M0OfficialCheckpointReceipt(StrictModel):
    """Portable integrity receipt for the pinned Hub revision and audited tokenizer."""

    schema_version: Literal[1] = 1
    receipt_id: str = Field(pattern=r"^m0-official-checkpoint:[0-9a-f]{64}$")
    model_id: Literal["answerdotai/ModernBERT-base"] = "answerdotai/ModernBERT-base"
    revision: Literal["8949b909ec900327062f0ebf497f51aef5e6f0c8"] = (
        "8949b909ec900327062f0ebf497f51aef5e6f0c8"
    )
    hf_snapshot_api_url: str = Field(min_length=1)
    receipt_basis: Literal["pinned_huggingface_lfs_sha256_v1"] = "pinned_huggingface_lfs_sha256_v1"
    required_files: dict[str, M0CheckpointFile] = Field(min_length=1)
    tokenizer_snapshot_content_hash: str = Field(pattern=_HEX64)
    local_files_only: Literal[True] = True
    trust_remote_code: Literal[False] = False

    @field_validator("required_files")
    @classmethod
    def _files_are_safe(cls, value: dict[str, M0CheckpointFile]) -> dict[str, M0CheckpointFile]:
        for name in value:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or name.startswith("."):
                raise ValueError("checkpoint file names must be safe relative paths")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def _receipt_is_canonical(self) -> Self:
        expected_files = {
            "config.json",
            "model.safetensors",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
        }
        if set(self.required_files) != expected_files:
            raise ValueError("M0 official receipt required file set is not exact")
        tokenizer_files = {
            name: pin for name, pin in self.required_files.items() if name != "model.safetensors"
        }
        observed_tokenizer_hash = hash_canonical(
            {name: pin.model_dump(mode="json") for name, pin in sorted(tokenizer_files.items())}
        )
        if observed_tokenizer_hash != self.tokenizer_snapshot_content_hash:
            raise ValueError("M0 receipt tokenizer pins differ from audited snapshot hash")
        expected = "m0-official-checkpoint:" + hash_canonical(
            self.model_dump(mode="json", exclude={"receipt_id"})
        )
        if self.receipt_id != expected:
            raise ValueError("M0 official checkpoint receipt ID differs")
        return self


class M0LocalCheckpointBinding(StrictModel):
    """Operational local path plus a portable official-byte receipt."""

    schema_version: Literal[2] = 2
    snapshot_path: str
    receipt: M0OfficialCheckpointReceipt

    @field_validator("snapshot_path")
    @classmethod
    def _snapshot_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("checkpoint snapshot_path must be absolute")
        return value


def official_m0_checkpoint_receipt(
    *,
    protocol: ExperimentalM0ProxyProtocolConfig,
    audited_tokenizer_snapshot: SnapshotBinding,
) -> M0OfficialCheckpointReceipt:
    """Build the immutable receipt from checked-in pins and the completed audit."""

    if (
        audited_tokenizer_snapshot.model_id != protocol.backbone.model_id
        or audited_tokenizer_snapshot.revision != protocol.backbone.revision
    ):
        raise ExperimentalM0ProxyError("audited tokenizer identity differs from M0 protocol")
    tokenizer_files = {
        name: M0CheckpointFile(sha256=pin.sha256, byte_count=pin.byte_count)
        for name, pin in audited_tokenizer_snapshot.files.items()
    }
    expected_tokenizer_files = {
        "config.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    }
    if set(tokenizer_files) != expected_tokenizer_files:
        raise ExperimentalM0ProxyError("audited ModernBERT tokenizer file set is not exact")
    required_files = {
        **tokenizer_files,
        protocol.backbone.weight_filename: M0CheckpointFile(
            sha256=protocol.backbone.weight_sha256,
            byte_count=protocol.backbone.weight_byte_count,
        ),
    }
    data: dict[str, object] = {
        "schema_version": 1,
        "model_id": protocol.backbone.model_id,
        "revision": protocol.backbone.revision,
        "hf_snapshot_api_url": protocol.backbone.hf_snapshot_api_url,
        "receipt_basis": protocol.backbone.receipt_basis,
        "required_files": {
            name: pin.model_dump(mode="json") for name, pin in sorted(required_files.items())
        },
        "tokenizer_snapshot_content_hash": audited_tokenizer_snapshot.snapshot_content_hash,
        "local_files_only": True,
        "trust_remote_code": False,
    }
    return M0OfficialCheckpointReceipt.model_validate(
        {**data, "receipt_id": "m0-official-checkpoint:" + hash_canonical(data)}
    )


def _bind_checkpoint_against_receipt(
    snapshot_path: Path,
    *,
    receipt: M0OfficialCheckpointReceipt,
) -> M0LocalCheckpointBinding:
    """Verify local bytes against a portable receipt; no directory-name trust."""

    snapshot = _real_directory(snapshot_path)
    if snapshot.name != receipt.revision:
        raise ExperimentalM0ProxyError("checkpoint snapshot directory is not the pinned revision")
    unsafe_weights = tuple(
        path
        for path in snapshot.rglob("*")
        if path.is_file()
        and (
            path.name.endswith((".bin", ".pt", ".pth"))
            or (path.name.endswith(".safetensors") and path.name != "model.safetensors")
        )
        and "onnx" not in path.parts
    )
    if unsafe_weights:
        raise ExperimentalM0ProxyError("checkpoint contains unreceipted or pickle model weights")
    for name, pin in receipt.required_files.items():
        path = _regular_file(snapshot / name)
        observed = M0CheckpointFile(sha256=hash_file(path), byte_count=path.stat().st_size)
        if observed != pin:
            raise ExperimentalM0ProxyError(f"checkpoint file differs from official receipt: {name}")
    config_value = _strict_json(snapshot / "config.json")
    if not isinstance(config_value, dict) or config_value.get("model_type") != "modernbert":
        raise ExperimentalM0ProxyError(
            "checkpoint config is not the pinned ModernBERT architecture"
        )
    return M0LocalCheckpointBinding(snapshot_path=str(snapshot), receipt=receipt)


def bind_local_modernbert_checkpoint(
    snapshot_path: Path,
    *,
    protocol: ExperimentalM0ProxyProtocolConfig,
    audited_tokenizer_snapshot: SnapshotBinding,
) -> M0LocalCheckpointBinding:
    """Bind exact official weights and the already-audited tokenizer bytes."""

    _verify_audited_tokenizer_snapshot(audited_tokenizer_snapshot)
    receipt = official_m0_checkpoint_receipt(
        protocol=protocol, audited_tokenizer_snapshot=audited_tokenizer_snapshot
    )
    return _bind_checkpoint_against_receipt(snapshot_path, receipt=receipt)


def verify_local_modernbert_checkpoint(binding: M0LocalCheckpointBinding) -> Path:
    snapshot = _real_directory(Path(binding.snapshot_path))
    observed = _bind_checkpoint_against_receipt(snapshot, receipt=binding.receipt)
    if observed != binding:
        raise ExperimentalM0ProxyError("local ModernBERT checkpoint differs from binding")
    return snapshot


ModuleImporter = Callable[[str], object]


def build_m0_dual_encoder_module(
    *,
    encoder: object,
    hidden_size: int,
    module_importer: ModuleImporter = importlib.import_module,
) -> object:
    """Create the real shared M0 module without importing torch at package import time."""

    if hidden_size <= 0:
        raise ExperimentalM0ProxyError("M0 hidden_size must be positive")
    try:
        torch = cast(Any, module_importer("torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM0ProxyError(
            "M0 requires the optional local-inference torch runtime"
        ) from exc
    if not isinstance(encoder, torch.nn.Module):
        raise ExperimentalM0ProxyError("M0 encoder must be a torch.nn.Module")

    class _M0DualEncoder(torch.nn.Module):  # type: ignore[misc, name-defined]
        def __init__(self, shared_encoder: object, width: int) -> None:
            super().__init__()
            self.encoder = cast(Any, shared_encoder)
            self.hidden_size = width
            self.symmetric_head = torch.nn.Linear(2 * width + 1, 1)

        def encode_side(self, *, input_ids: Any, attention_mask: Any) -> Any:
            if input_ids.ndim != 2 or attention_mask.ndim != 2:
                raise ValueError("M0 input_ids and attention_mask must be rank-two")
            if input_ids.shape != attention_mask.shape:
                raise ValueError("M0 input_ids and attention_mask shapes differ")
            if bool((attention_mask.sum(dim=1) <= 0).any().item()):
                raise ValueError("M0 attention mask contains an empty sequence")
            output = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
            hidden = getattr(output, "last_hidden_state", None)
            if hidden is None:
                try:
                    hidden = output[0]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ValueError("M0 encoder output lacks last_hidden_state") from exc
            if hidden.ndim != 3 or hidden.shape[:2] != input_ids.shape:
                raise ValueError("M0 encoder hidden state has an incompatible shape")
            if hidden.shape[-1] != self.hidden_size:
                raise ValueError("M0 encoder hidden width differs from configured hidden_size")
            mask = attention_mask.to(dtype=hidden.dtype).unsqueeze(-1)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp_min(1.0)
            return torch.nn.functional.normalize(pooled, p=2, dim=-1)

        def score_embeddings(
            self, source_embedding: Any, candidate_embedding: Any
        ) -> dict[str, Any]:
            if source_embedding.shape != candidate_embedding.shape:
                raise ValueError("M0 source/candidate embedding shapes differ")
            if source_embedding.ndim != 2 or source_embedding.shape[-1] != self.hidden_size:
                raise ValueError("M0 embeddings have an incompatible shape")
            source_norm = torch.nn.functional.normalize(source_embedding, p=2, dim=-1)
            candidate_norm = torch.nn.functional.normalize(candidate_embedding, p=2, dim=-1)
            product = source_norm * candidate_norm
            cosine = product.sum(dim=-1, keepdim=True)
            symmetric = torch.cat(
                (cosine, torch.abs(source_norm - candidate_norm), product), dim=-1
            )
            logit = self.symmetric_head(symmetric).squeeze(-1)
            return {
                "logits": logit,
                "probabilities": torch.sigmoid(logit),
                "symmetric_features": symmetric,
                "source_embeddings": source_norm,
                "candidate_embeddings": candidate_norm,
            }

        def forward(
            self,
            *,
            source_input_ids: Any,
            source_attention_mask: Any,
            candidate_input_ids: Any,
            candidate_attention_mask: Any,
        ) -> dict[str, Any]:
            source = self.encode_side(
                input_ids=source_input_ids, attention_mask=source_attention_mask
            )
            candidate = self.encode_side(
                input_ids=candidate_input_ids,
                attention_mask=candidate_attention_mask,
            )
            return self.score_embeddings(source, candidate)

    _M0DualEncoder.__name__ = "M0DualEncoder"
    return _M0DualEncoder(encoder, hidden_size)


@dataclass(frozen=True, slots=True)
class LoadedM0ProxyRuntime:
    model: object
    tokenizer: object
    selected_length: Literal[512, 1024]
    checkpoint: M0LocalCheckpointBinding
    audited_tokenizer_snapshot: SnapshotBinding
    initial_model_state_sha256: str
    prepared_tokenization_sha256: str


class M0TrainingRuntimeVersions(StrictModel):
    python: str = Field(min_length=1)
    torch: str = Field(min_length=1)
    transformers: str = Field(min_length=1)
    safetensors: str = Field(min_length=1)
    device: str = Field(min_length=1)
    deterministic_algorithms: Literal[True] = True
    numeric_dtype: Literal["float32"] = "float32"


class M0ProxyPrediction(StrictModel):
    """Text-free diagnostic prediction; never a semantic evaluation result."""

    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=_RECORD_ID)
    split: ProxySplit
    pseudo_target: ProxyTarget
    same_claim_probability: float = Field(ge=0.0, le=1.0)
    private_source_content: bool


class M0ProxyMetricSet(StrictModel):
    record_count: int = Field(gt=0)
    same_claim_count: int = Field(ge=0)
    not_same_claim_count: int = Field(ge=0)
    weighted_bce: float = Field(ge=0.0)
    auprc: float = Field(ge=0.0, le=1.0)
    balanced_accuracy: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _metric_counts_reconcile(self) -> Self:
        if self.same_claim_count + self.not_same_claim_count != self.record_count:
            raise ValueError("M0 metric target counts do not reconcile")
        for value in (self.weighted_bce, self.auprc, self.balanced_accuracy):
            if not math.isfinite(value):
                raise ValueError("M0 metrics must be finite")
        return self


class M0ProxyTrainingMetrics(ExperimentalM0ProxyBoundary):
    schema_version: Literal[2] = 2
    epoch_count: Literal[1] = 1
    optimizer_steps: int = Field(gt=0)
    examples_exposed: int = Field(gt=0)
    selected_component_count: int = Field(gt=0)
    loss_normalization_weight: float = Field(gt=0.0)
    mean_weighted_training_bce: float = Field(ge=0.0)
    initial_state_sha256: str = Field(pattern=_HEX64)
    final_state_sha256: str = Field(pattern=_HEX64)
    diagnostics: dict[str, M0ProxyMetricSet]

    @model_validator(mode="after")
    def _metrics_are_coherent(self) -> Self:
        if tuple(self.diagnostics) != _SPLITS:
            raise ValueError("M0 training diagnostics must contain canonical train/validation/test")
        if not math.isfinite(self.mean_weighted_training_bce):
            raise ValueError("M0 training loss must be finite")
        if self.initial_state_sha256 == self.final_state_sha256:
            raise ValueError("M0 one-epoch training did not change model state")
        return self


class M0ProxyTrainingManifest(ExperimentalM0ProxyBoundary):
    """Portable, content-addressed proof that one proxy epoch executed."""

    schema_version: Literal[2] = 2
    artifact_id: str = Field(pattern=r"^experimental-m0-proxy-training:[0-9a-f]{64}$")
    artifact_kind: Literal["experimental_m0_proxy_training_v2"] = (
        "experimental_m0_proxy_training_v2"
    )
    prepared_input_artifact_id: str = Field(pattern=_ARTIFACT_ID)
    prepared_input_manifest_sha256: str = Field(pattern=_HEX64)
    dataset_id: str = Field(pattern=_DATASET_ID)
    protocol_hash: str = Field(pattern=_HEX64)
    code: CodeState
    pretrained_checkpoint: M0OfficialCheckpointReceipt
    tokenizer_audit_id: str = Field(pattern=_AUDIT_ID)
    tokenizer_snapshot_content_hash: str = Field(pattern=_HEX64)
    runtime: M0TrainingRuntimeVersions
    epoch_schedule_sha256: str = Field(pattern=_HEX64)
    selected_record_set_sha256: str = Field(pattern=_HEX64)
    optimizer: Literal["AdamW"] = "AdamW"
    learning_rate: float = Field(gt=0.0)
    weight_decay: float = Field(ge=0.0)
    seed: int = Field(ge=0)
    epoch_count: Literal[1] = 1
    optimizer_steps: int = Field(gt=0)
    examples_exposed: int = Field(gt=0)
    selected_component_count: int = Field(gt=0)
    loss_normalization_weight: float = Field(gt=0.0)
    effective_batch_size: Literal[32] = 32
    microbatch_size: Literal[4] = 4
    gradient_accumulation_steps: Literal[8] = 8
    initial_model_state_sha256: str = Field(pattern=_HEX64)
    prepared_tokenization_sha256: str = Field(pattern=_HEX64)
    contains_private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool
    output_sha256: dict[str, str]
    model_weights_loaded: Literal[True] = True
    training_executed: Literal[True] = True

    @model_validator(mode="after")
    def _training_manifest_is_coherent(self) -> Self:
        if self.code.git_dirty or self.code.code_tree_hash is None or self.code.untracked_files:
            raise ValueError("M0 proxy training requires clean fully tracked code")
        if set(self.output_sha256) != _TRAINING_NON_MANIFEST_OUTPUTS:
            raise ValueError("M0 training manifest output set is not exact")
        if any(re.fullmatch(_HEX64, value) is None for value in self.output_sha256.values()):
            raise ValueError("M0 training output hashes must be SHA-256")
        if self.contains_private_source_content and (
            self.redistribution_allowed
            or self.external_transmission_allowed
            or self.release_eligible
        ):
            raise ValueError("private-trained M0 artifact cannot be shared or released")
        if self.release_eligible and not self.redistribution_allowed:
            raise ValueError("M0 training release policy is incoherent")
        if self.microbatch_size * self.gradient_accumulation_steps != self.effective_batch_size:
            raise ValueError("M0 training microbatch accounting differs")
        expected = "experimental-m0-proxy-training:" + hash_canonical(
            self.model_dump(mode="json", exclude={"artifact_id"})
        )
        if self.artifact_id != expected:
            raise ValueError("M0 training artifact ID differs from canonical content")
        return self


@dataclass(frozen=True, slots=True)
class M0ProxyTrainingArtifacts:
    output_dir: Path
    artifact_id: str
    optimizer_steps: int
    examples_exposed: int
    replayed: bool


def load_m0_proxy_runtime(
    *,
    prepared_input_dir: Path,
    checkpoint: M0LocalCheckpointBinding,
    audited_tokenizer_snapshot: SnapshotBinding,
    allow_experimental_mixed_supervision: bool,
    module_importer: ModuleImporter = importlib.import_module,
) -> LoadedM0ProxyRuntime:
    """Load an exact local checkpoint only after verified tokenizer preparation."""

    if not allow_experimental_mixed_supervision:
        raise ExperimentalM0ProxyError("M0 runtime requires --allow-experimental-mixed-supervision")
    manifest = verify_experimental_m0_proxy_inputs(prepared_input_dir)
    snapshot = verify_local_modernbert_checkpoint(checkpoint)
    if (
        checkpoint.receipt.model_id != manifest.protocol.backbone.model_id
        or checkpoint.receipt.revision != manifest.protocol.backbone.revision
        or checkpoint.receipt.tokenizer_snapshot_content_hash
        != manifest.tokenizer_decision.snapshot_content_hash
        or audited_tokenizer_snapshot.snapshot_content_hash
        != manifest.tokenizer_decision.snapshot_content_hash
    ):
        raise ExperimentalM0ProxyError("checkpoint identity differs from prepared M0 protocol")
    tokenizer_snapshot = _verify_audited_tokenizer_snapshot(audited_tokenizer_snapshot)
    try:
        transformers = cast(Any, module_importer("transformers"))
        torch = cast(Any, module_importer("torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM0ProxyError(
            "M0 runtime requires optional torch and transformers dependencies"
        ) from exc
    try:
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(tokenizer_snapshot),
            local_files_only=True,
            trust_remote_code=False,
            use_fast=audited_tokenizer_snapshot.use_fast,
        )
        torch.manual_seed(manifest.protocol.training.seed)
        encoder = transformers.AutoModel.from_pretrained(
            str(snapshot), local_files_only=True, trust_remote_code=False
        )
        hidden_size = int(encoder.config.hidden_size)
    except Exception as exc:
        raise ExperimentalM0ProxyError("failed to load exact local ModernBERT checkpoint") from exc
    model = build_m0_dual_encoder_module(
        encoder=encoder,
        hidden_size=hidden_size,
        module_importer=module_importer,
    )
    initial_state = _safetensors_state_bytes(model, module_importer=module_importer)
    examples = _load_prepared_examples(_real_directory(prepared_input_dir))
    prepared_tokenization_sha256 = _prepared_tokenization_sha256(tokenizer, examples)
    return LoadedM0ProxyRuntime(
        model=model,
        tokenizer=tokenizer,
        selected_length=manifest.tokenizer_decision.selected_length,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=audited_tokenizer_snapshot,
        initial_model_state_sha256=hashlib.sha256(initial_state).hexdigest(),
        prepared_tokenization_sha256=prepared_tokenization_sha256,
    )


def _load_prepared_examples(output_dir: Path) -> tuple[M0ProxyExample, ...]:
    examples: list[M0ProxyExample] = []
    with _regular_file(output_dir / "examples.jsonl").open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                item = M0ProxyExample.model_validate(json.loads(raw))
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ExperimentalM0ProxyError(
                    f"invalid prepared M0 example at line {line_number}"
                ) from exc
            if raw != canonical_json_bytes(item.model_dump(mode="json")) + b"\n":
                raise ExperimentalM0ProxyError(
                    f"noncanonical prepared M0 example at line {line_number}"
                )
            examples.append(item)
    result = tuple(examples)
    if result != tuple(sorted(result, key=lambda item: item.record_id)):
        raise ExperimentalM0ProxyError("prepared M0 examples are not ID-sorted")
    return result


def _runtime_versions(*, device: str) -> M0TrainingRuntimeVersions:
    def version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ExperimentalM0ProxyError(f"M0 training runtime lacks {name}") from exc

    return M0TrainingRuntimeVersions(
        python=platform.python_version(),
        torch=version("torch"),
        transformers=version("transformers"),
        safetensors=version("safetensors"),
        device=device,
    )


def _safetensors_state_bytes(model: object, *, module_importer: ModuleImporter) -> bytes:
    try:
        torch = cast(Any, module_importer("torch"))
        safetensors_torch = cast(Any, module_importer("safetensors.torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM0ProxyError("M0 checkpointing requires torch and safetensors") from exc
    if not isinstance(model, torch.nn.Module):
        raise ExperimentalM0ProxyError("M0 training model must be a torch module")
    state: dict[str, Any] = {}
    for name, value in sorted(model.state_dict().items()):
        if not isinstance(value, torch.Tensor):
            raise ExperimentalM0ProxyError("M0 state dictionary contains a non-tensor")
        state[name] = value.detach().to(device="cpu").contiguous()
    if not state:
        raise ExperimentalM0ProxyError("M0 model state is empty")
    try:
        payload = safetensors_torch.save(state)
    except Exception as exc:
        raise ExperimentalM0ProxyError("failed to serialize M0 safetensors checkpoint") from exc
    if not isinstance(payload, bytes) or not payload:
        raise ExperimentalM0ProxyError("M0 safetensors serializer returned no bytes")
    return payload


def _prepared_tokenization_sha256(tokenizer: object, examples: Sequence[M0ProxyExample]) -> str:
    """Bind exact audited token IDs for every prepared private/local input."""

    if not examples:
        raise ExperimentalM0ProxyError("M0 tokenization binding requires prepared examples")
    encode = getattr(tokenizer, "encode", None)
    if not callable(encode):
        raise ExperimentalM0ProxyError("M0 runtime tokenizer lacks encode")
    digest = hashlib.sha256()
    for item in sorted(examples, key=lambda example: example.record_id):
        try:
            source_ids = tuple(
                int(value) for value in encode(item.source_text, add_special_tokens=True)
            )
            candidate_ids = tuple(
                int(value) for value in encode(item.candidate_text, add_special_tokens=True)
            )
        except Exception as exc:
            raise ExperimentalM0ProxyError(
                "M0 tokenizer binding failed without persisting private text"
            ) from exc
        if (
            len(source_ids) != item.source_token_count
            or len(candidate_ids) != item.candidate_token_count
        ):
            raise ExperimentalM0ProxyError("M0 runtime tokenizer counts differ from preparation")
        digest.update(
            canonical_json_bytes(
                {
                    "record_id": item.record_id,
                    "source_token_ids": source_ids,
                    "candidate_token_ids": candidate_ids,
                }
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _tensorize_m0_batch(
    *,
    tokenizer: object,
    examples: Sequence[M0ProxyExample],
    selected_length: int,
    weights: Sequence[float],
    device: str,
    module_importer: ModuleImporter,
) -> dict[str, object]:
    """Separately tokenize both sides and attach labels/static loss weights."""

    if not examples or len(examples) != len(weights):
        raise ExperimentalM0ProxyError("M0 collator requires nonempty aligned examples/weights")
    if any(item.long_input for item in examples):
        raise ExperimentalM0ProxyError("M0 collator cannot silently truncate long-input examples")
    if any(not math.isfinite(weight) or weight <= 0.0 for weight in weights):
        raise ExperimentalM0ProxyError("M0 collator loss weights must be finite positive values")
    try:
        torch = cast(Any, module_importer("torch"))
        tokenize = cast(Callable[..., Mapping[str, Any]], tokenizer)
        source = tokenize(
            [item.source_text for item in examples],
            padding=True,
            truncation=True,
            max_length=selected_length,
            return_tensors="pt",
        )
        candidate = tokenize(
            [item.candidate_text for item in examples],
            padding=True,
            truncation=True,
            max_length=selected_length,
            return_tensors="pt",
        )
    except Exception as exc:
        raise ExperimentalM0ProxyError(
            "M0 tokenizer failed without persisting private text"
        ) from exc
    required = {"input_ids", "attention_mask"}
    if not required.issubset(source) or not required.issubset(candidate):
        raise ExperimentalM0ProxyError("M0 tokenizer output lacks IDs or attention masks")
    for value in (
        source["input_ids"],
        source["attention_mask"],
        candidate["input_ids"],
        candidate["attention_mask"],
    ):
        if value.ndim != 2 or value.shape[0] != len(examples):
            raise ExperimentalM0ProxyError("M0 tokenized batch has an incompatible shape")
        if value.shape[1] > selected_length:
            raise ExperimentalM0ProxyError("M0 tokenizer exceeded selected context length")
    if bool((source["attention_mask"].sum(dim=1) <= 0).any().item()) or bool(
        (candidate["attention_mask"].sum(dim=1) <= 0).any().item()
    ):
        raise ExperimentalM0ProxyError("M0 tokenizer produced an empty sequence")
    return {
        "source_input_ids": source["input_ids"].to(device),
        "source_attention_mask": source["attention_mask"].to(device),
        "candidate_input_ids": candidate["input_ids"].to(device),
        "candidate_attention_mask": candidate["attention_mask"].to(device),
        "labels": torch.tensor(
            [1.0 if item.pseudo_target == "same_claim" else 0.0 for item in examples],
            dtype=torch.float32,
            device=device,
        ),
        "weights": torch.tensor(tuple(weights), dtype=torch.float32, device=device),
        "record_ids": tuple(item.record_id for item in examples),
    }


def _weighted_binary_cross_entropy(
    *,
    logits: object,
    labels: object,
    weights: object,
    torch: Any,
    normalization_weight: float | None = None,
) -> object:
    """Sum ancestry-weighted BCE over one microbatch with a fixed denominator."""

    logits_value = cast(Any, logits)
    labels_value = cast(Any, labels)
    weights_value = cast(Any, weights)
    if logits_value.shape != labels_value.shape or weights_value.shape != labels_value.shape:
        raise ExperimentalM0ProxyError("M0 loss tensors have incompatible shapes")
    if not bool(torch.isfinite(logits_value).all().item()) or not bool(
        torch.isfinite(weights_value).all().item()
    ):
        raise ExperimentalM0ProxyError("M0 loss inputs are nonfinite")
    if bool((weights_value <= 0).any().item()):
        raise ExperimentalM0ProxyError("M0 loss weights must be positive")
    losses = torch.nn.functional.binary_cross_entropy_with_logits(
        logits_value, labels_value, reduction="none"
    )
    denominator = (
        float(weights_value.sum().detach().cpu().item())
        if normalization_weight is None
        else normalization_weight
    )
    if not math.isfinite(denominator) or denominator <= 0.0:
        raise ExperimentalM0ProxyError("M0 loss normalization weight must be finite and positive")
    loss = (losses * weights_value).sum() / denominator
    if not bool(torch.isfinite(loss).item()):
        raise ExperimentalM0ProxyError("M0 weighted training loss is nonfinite")
    return loss


def _tie_safe_average_precision(labels: Sequence[int], scores: Sequence[float]) -> float:
    if not labels or len(labels) != len(scores) or not any(labels):
        raise ExperimentalM0ProxyError("M0 AUPRC requires aligned labels with positives")
    if any(label not in {0, 1} for label in labels):
        raise ExperimentalM0ProxyError("M0 AUPRC labels must be binary")
    ranked: dict[float, list[int]] = defaultdict(list)
    for label, score in zip(labels, scores, strict=True):
        if not math.isfinite(score):
            raise ExperimentalM0ProxyError("M0 AUPRC scores must be finite")
        ranked[score].append(label)
    true_positive = 0
    predicted = 0
    previous_recall = 0.0
    result = 0.0
    positive_count = sum(labels)
    for score in sorted(ranked, reverse=True):
        group = ranked[score]
        true_positive += sum(group)
        predicted += len(group)
        recall = true_positive / positive_count
        precision = true_positive / predicted
        result += (recall - previous_recall) * precision
        previous_recall = recall
    return result


def _metric_set(predictions: Sequence[M0ProxyPrediction]) -> M0ProxyMetricSet:
    if not predictions:
        raise ExperimentalM0ProxyError("M0 diagnostics require predictions")
    labels = [1 if item.pseudo_target == "same_claim" else 0 for item in predictions]
    scores = [item.same_claim_probability for item in predictions]
    if not any(labels) or all(labels):
        raise ExperimentalM0ProxyError("M0 diagnostics require both proxy targets")
    epsilon = 1e-12
    losses = [
        -(label * math.log(max(score, epsilon)) + (1 - label) * math.log(max(1 - score, epsilon)))
        for label, score in zip(labels, scores, strict=True)
    ]
    predicted = [int(score >= 0.5) for score in scores]
    true_positive = sum(p == 1 and y == 1 for p, y in zip(predicted, labels, strict=True))
    true_negative = sum(p == 0 and y == 0 for p, y in zip(predicted, labels, strict=True))
    positive = sum(labels)
    negative = len(labels) - positive
    return M0ProxyMetricSet(
        record_count=len(predictions),
        same_claim_count=positive,
        not_same_claim_count=negative,
        weighted_bce=sum(losses) / len(losses),
        auprc=_tie_safe_average_precision(labels, scores),
        balanced_accuracy=0.5 * (true_positive / positive + true_negative / negative),
    )


def _predict_m0_examples(
    *,
    runtime: LoadedM0ProxyRuntime,
    examples: Sequence[M0ProxyExample],
    batch_size: int,
    device: str,
    module_importer: ModuleImporter,
) -> tuple[M0ProxyPrediction, ...]:
    try:
        torch = cast(Any, module_importer("torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM0ProxyError("M0 prediction requires torch") from exc
    model = cast(Any, runtime.model)
    model.eval()
    output: list[M0ProxyPrediction] = []
    with torch.no_grad():
        for start in range(0, len(examples), batch_size):
            items = examples[start : start + batch_size]
            batch = _tensorize_m0_batch(
                tokenizer=runtime.tokenizer,
                examples=items,
                selected_length=runtime.selected_length,
                weights=[1.0] * len(items),
                device=device,
                module_importer=module_importer,
            )
            result = model(
                source_input_ids=batch["source_input_ids"],
                source_attention_mask=batch["source_attention_mask"],
                candidate_input_ids=batch["candidate_input_ids"],
                candidate_attention_mask=batch["candidate_attention_mask"],
            )
            probabilities = cast(Any, result["probabilities"])
            if probabilities.ndim != 1 or probabilities.shape[0] != len(items):
                raise ExperimentalM0ProxyError("M0 prediction output has an incompatible shape")
            for item, probability in zip(items, probabilities.detach().cpu().tolist(), strict=True):
                value = float(probability)
                if not math.isfinite(value):
                    raise ExperimentalM0ProxyError("M0 produced a nonfinite probability")
                output.append(
                    M0ProxyPrediction(
                        record_id=item.record_id,
                        split=item.split,
                        pseudo_target=item.pseudo_target,
                        same_claim_probability=value,
                        private_source_content=item.private_source_content,
                    )
                )
    return tuple(sorted(output, key=lambda item: item.record_id))


def _write_or_replay_training(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    if set(payloads) != _TRAINING_OUTPUT_FILES:
        raise ExperimentalM0ProxyError("M0 training payload set is not exact")
    output = _reject_symlinks(output_dir, allow_missing=True)

    def verify_existing() -> bool:
        root = _real_directory(output)
        if {path.name for path in root.iterdir()} != _TRAINING_OUTPUT_FILES:
            raise ExperimentalM0ProxyError("existing M0 training output set is not exact")
        for name, payload in sorted(payloads.items()):
            if _regular_file(root / name).read_bytes() != payload:
                raise ExperimentalM0ProxyError(f"existing M0 training output differs: {name}")
        return True

    if output.exists():
        return verify_existing()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for name, payload in sorted(payloads.items()):
            descriptor = os.open(temporary / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        try:
            os.rename(temporary, output)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            if temporary.exists():
                shutil.rmtree(temporary)
            return verify_existing()
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return False


def train_m0_proxy_one_epoch(
    *,
    repository_root: Path,
    prepared_input_dir: Path,
    output_dir: Path,
    checkpoint: M0LocalCheckpointBinding,
    audited_tokenizer_snapshot: SnapshotBinding,
    allow_experimental_mixed_supervision: bool,
    device: str = "cpu",
    module_importer: ModuleImporter = importlib.import_module,
) -> M0ProxyTrainingArtifacts:
    """Trusted entrypoint: load exact bound bytes, then execute one proxy epoch."""

    runtime = load_m0_proxy_runtime(
        prepared_input_dir=prepared_input_dir,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=audited_tokenizer_snapshot,
        allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
        module_importer=module_importer,
    )
    return _train_loaded_m0_proxy_one_epoch(
        repository_root=repository_root,
        prepared_input_dir=prepared_input_dir,
        output_dir=output_dir,
        runtime=runtime,
        allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
        device=device,
        module_importer=module_importer,
    )


def _train_loaded_m0_proxy_one_epoch(
    *,
    repository_root: Path,
    prepared_input_dir: Path,
    output_dir: Path,
    runtime: LoadedM0ProxyRuntime,
    allow_experimental_mixed_supervision: bool,
    device: str,
    module_importer: ModuleImporter,
) -> M0ProxyTrainingArtifacts:
    """Execute exactly one weighted proxy epoch and atomically freeze all outputs."""

    if not allow_experimental_mixed_supervision:
        raise ExperimentalM0ProxyError("M0 training requires explicit experimental opt-in")
    repository = _real_directory(repository_root)
    prepared = _real_directory(prepared_input_dir)
    output = _reject_symlinks(output_dir, allow_missing=True)
    if any(_paths_overlap(output, protected) for protected in (repository, prepared)):
        raise ExperimentalM0ProxyError("M0 training output must be disjoint from code and inputs")
    input_manifest = verify_experimental_m0_proxy_inputs(prepared)
    if runtime.selected_length != input_manifest.tokenizer_decision.selected_length:
        raise ExperimentalM0ProxyError("M0 runtime length differs from prepared inputs")
    _verify_audited_tokenizer_snapshot(runtime.audited_tokenizer_snapshot)
    if (
        runtime.audited_tokenizer_snapshot.model_id != input_manifest.protocol.backbone.model_id
        or runtime.audited_tokenizer_snapshot.revision != input_manifest.protocol.backbone.revision
        or runtime.audited_tokenizer_snapshot.snapshot_content_hash
        != input_manifest.tokenizer_decision.snapshot_content_hash
    ):
        raise ExperimentalM0ProxyError("M0 runtime tokenizer differs from prepared audit")
    receipt = runtime.checkpoint.receipt
    expected_weight = receipt.required_files.get(input_manifest.protocol.backbone.weight_filename)
    if (
        receipt.model_id != input_manifest.protocol.backbone.model_id
        or receipt.revision != input_manifest.protocol.backbone.revision
        or receipt.hf_snapshot_api_url != input_manifest.protocol.backbone.hf_snapshot_api_url
        or receipt.tokenizer_snapshot_content_hash
        != input_manifest.tokenizer_decision.snapshot_content_hash
        or expected_weight is None
        or expected_weight.sha256 != input_manifest.protocol.backbone.weight_sha256
        or expected_weight.byte_count != input_manifest.protocol.backbone.weight_byte_count
    ):
        raise ExperimentalM0ProxyError("M0 runtime checkpoint receipt differs from protocol")
    verify_local_modernbert_checkpoint(runtime.checkpoint)
    examples = _load_prepared_examples(prepared)
    trusted_tokenizer = _load_audited_tokenizer(runtime.audited_tokenizer_snapshot)
    trusted_tokenization_sha256 = _prepared_tokenization_sha256(trusted_tokenizer, examples)
    runtime_tokenization_sha256 = _prepared_tokenization_sha256(runtime.tokenizer, examples)
    if (
        trusted_tokenization_sha256 != runtime.prepared_tokenization_sha256
        or runtime_tokenization_sha256 != runtime.prepared_tokenization_sha256
    ):
        raise ExperimentalM0ProxyError("M0 runtime tokenizer differs from audited preparation")
    train_examples = tuple(item for item in examples if item.proxy_training_eligible)
    schedule = build_m0_epoch_schedule(
        train_examples,
        batch_size=input_manifest.protocol.training.batch_size,
        seed=input_manifest.protocol.training.seed,
        max_unique_variants_per_ancestry=(
            input_manifest.protocol.training.max_unique_variants_per_ancestry
        ),
    )
    by_id = {item.record_id: item for item in train_examples}
    selected_rows = tuple(
        sorted(
            (item for item in schedule.records if item.selection_status == "selected"),
            key=lambda item: cast(int, item.epoch_position),
        )
    )
    selected_examples = tuple(by_id[item.record_id] for item in selected_rows)

    try:
        torch = cast(Any, module_importer("torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM0ProxyError("M0 training requires torch") from exc
    model = cast(Any, runtime.model)
    if not isinstance(model, torch.nn.Module):
        raise ExperimentalM0ProxyError("M0 training model is not a torch module")
    initial_state = _safetensors_state_bytes(model, module_importer=module_importer)
    initial_hash = hashlib.sha256(initial_state).hexdigest()
    if initial_hash != runtime.initial_model_state_sha256:
        raise ExperimentalM0ProxyError("M0 runtime model changed after exact checkpoint loading")
    torch.manual_seed(input_manifest.protocol.training.seed)
    if hasattr(torch, "cuda"):
        torch.cuda.manual_seed_all(input_manifest.protocol.training.seed)
    torch.use_deterministic_algorithms(True)
    model.to(device=device, dtype=torch.float32)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=input_manifest.protocol.training.learning_rate,
        weight_decay=input_manifest.protocol.training.weight_decay,
    )
    model.train()
    weighted_loss_sum = 0.0
    step_count = 0
    batch_size = input_manifest.protocol.training.batch_size
    microbatch_size = input_manifest.protocol.training.microbatch_size
    gradient_accumulation_steps = input_manifest.protocol.training.gradient_accumulation_steps
    for start in range(0, len(selected_examples), batch_size):
        items = selected_examples[start : start + batch_size]
        rows = selected_rows[start : start + batch_size]
        if len(items) != batch_size or len(rows) != batch_size:
            raise ExperimentalM0ProxyError("M0 training encountered an incomplete effective batch")
        optimizer.zero_grad(set_to_none=True)
        logical_batch_loss = 0.0
        microbatch_count = 0
        for micro_start in range(0, batch_size, microbatch_size):
            micro_items = items[micro_start : micro_start + microbatch_size]
            micro_rows = rows[micro_start : micro_start + microbatch_size]
            batch = _tensorize_m0_batch(
                tokenizer=trusted_tokenizer,
                examples=micro_items,
                selected_length=runtime.selected_length,
                weights=[cast(float, item.loss_weight) for item in micro_rows],
                device=device,
                module_importer=module_importer,
            )
            result = model(
                source_input_ids=batch["source_input_ids"],
                source_attention_mask=batch["source_attention_mask"],
                candidate_input_ids=batch["candidate_input_ids"],
                candidate_attention_mask=batch["candidate_attention_mask"],
            )
            loss = cast(
                Any,
                _weighted_binary_cross_entropy(
                    logits=result["logits"],
                    labels=batch["labels"],
                    weights=batch["weights"],
                    torch=torch,
                    normalization_weight=schedule.loss_normalization_weight,
                ),
            )
            loss.backward()
            logical_batch_loss += float(loss.detach().cpu().item())
            microbatch_count += 1
        if microbatch_count != gradient_accumulation_steps:
            raise ExperimentalM0ProxyError("M0 gradient-accumulation count differs from protocol")
        optimizer.step()
        weighted_loss_sum += logical_batch_loss
        step_count += 1
    if step_count != schedule.batch_count:
        raise ExperimentalM0ProxyError("M0 optimizer-step count differs from schedule")
    final_state = _safetensors_state_bytes(model, module_importer=module_importer)
    final_hash = hashlib.sha256(final_state).hexdigest()

    trusted_runtime = LoadedM0ProxyRuntime(
        model=model,
        tokenizer=trusted_tokenizer,
        selected_length=runtime.selected_length,
        checkpoint=runtime.checkpoint,
        audited_tokenizer_snapshot=runtime.audited_tokenizer_snapshot,
        initial_model_state_sha256=runtime.initial_model_state_sha256,
        prepared_tokenization_sha256=runtime.prepared_tokenization_sha256,
    )

    diagnostic_examples = {
        "train": selected_examples,
        "validation": tuple(
            item for item in examples if item.split == "validation" and not item.long_input
        ),
        "test": tuple(item for item in examples if item.split == "test" and not item.long_input),
    }
    predictions_by_split: dict[str, tuple[M0ProxyPrediction, ...]] = {}
    for split in _SPLITS:
        predictions_by_split[split] = _predict_m0_examples(
            runtime=trusted_runtime,
            examples=diagnostic_examples[split],
            batch_size=microbatch_size,
            device=device,
            module_importer=module_importer,
        )
    predictions = tuple(
        sorted(
            (item for split in _SPLITS for item in predictions_by_split[split]),
            key=lambda item: item.record_id,
        )
    )
    metrics = M0ProxyTrainingMetrics(
        optimizer_steps=step_count,
        examples_exposed=len(selected_examples),
        selected_component_count=schedule.selected_component_count,
        loss_normalization_weight=schedule.loss_normalization_weight,
        mean_weighted_training_bce=weighted_loss_sum / step_count,
        initial_state_sha256=initial_hash,
        final_state_sha256=final_hash,
        diagnostics={split: _metric_set(predictions_by_split[split]) for split in _SPLITS},
    )
    schedule_payload = canonical_json_bytes(schedule.model_dump(mode="json")) + b"\n"
    predictions_payload = _canonical_jsonl(predictions)
    metrics_payload = canonical_json_bytes(metrics.model_dump(mode="json")) + b"\n"
    non_manifest = {
        "epoch_schedule.json": schedule_payload,
        "metrics.json": metrics_payload,
        "model.safetensors": final_state,
        "predictions.jsonl": predictions_payload,
    }
    code = collect_code_state(repository)
    _verify_clean_code(code)
    contains_private = any(item.private_source_content for item in examples)
    redistribution_allowed = (not contains_private) and all(
        item.redistribution_allowed for item in examples
    )
    external_allowed = (not contains_private) and all(
        item.external_transmission_allowed for item in examples
    )
    release_eligible = (not contains_private) and all(item.release_eligible for item in examples)
    manifest_data: dict[str, object] = {
        "schema_version": 2,
        "artifact_kind": "experimental_m0_proxy_training_v2",
        "prepared_input_artifact_id": input_manifest.artifact_id,
        "prepared_input_manifest_sha256": hash_file(prepared / "manifest.json"),
        "dataset_id": input_manifest.dataset_id,
        "protocol_hash": input_manifest.protocol_config_hash,
        "code": code.model_dump(mode="json"),
        "pretrained_checkpoint": runtime.checkpoint.receipt.model_dump(mode="json"),
        "tokenizer_audit_id": input_manifest.tokenizer_decision.audit_id,
        "tokenizer_snapshot_content_hash": (
            input_manifest.tokenizer_decision.snapshot_content_hash
        ),
        "runtime": _runtime_versions(device=device).model_dump(mode="json"),
        "epoch_schedule_sha256": schedule.schedule_sha256,
        "selected_record_set_sha256": schedule.selected_record_set_sha256,
        "optimizer": "AdamW",
        "learning_rate": input_manifest.protocol.training.learning_rate,
        "weight_decay": input_manifest.protocol.training.weight_decay,
        "seed": input_manifest.protocol.training.seed,
        "epoch_count": 1,
        "optimizer_steps": step_count,
        "examples_exposed": len(selected_examples),
        "selected_component_count": schedule.selected_component_count,
        "loss_normalization_weight": schedule.loss_normalization_weight,
        "effective_batch_size": batch_size,
        "microbatch_size": microbatch_size,
        "gradient_accumulation_steps": gradient_accumulation_steps,
        "initial_model_state_sha256": runtime.initial_model_state_sha256,
        "prepared_tokenization_sha256": runtime.prepared_tokenization_sha256,
        "contains_private_source_content": contains_private,
        "redistribution_allowed": redistribution_allowed,
        "external_transmission_allowed": external_allowed,
        "release_eligible": release_eligible,
        "output_sha256": {
            name: hashlib.sha256(payload).hexdigest() for name, payload in non_manifest.items()
        },
        "model_weights_loaded": True,
        "training_executed": True,
        **ExperimentalM0ProxyBoundary().model_dump(mode="json"),
    }
    manifest = M0ProxyTrainingManifest.model_validate(
        {
            **manifest_data,
            "artifact_id": "experimental-m0-proxy-training:" + hash_canonical(manifest_data),
        }
    )
    payloads = {
        **non_manifest,
        "manifest.json": canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    }
    replayed = _write_or_replay_training(output, payloads)
    verify_m0_proxy_training(output)
    return M0ProxyTrainingArtifacts(
        output_dir=output,
        artifact_id=manifest.artifact_id,
        optimizer_steps=step_count,
        examples_exposed=len(selected_examples),
        replayed=replayed,
    )


def verify_m0_proxy_training(
    output_dir: Path,
    *,
    repository_root: Path | None = None,
    prepared_input_dir: Path | None = None,
    checkpoint: M0LocalCheckpointBinding | None = None,
    audited_tokenizer_snapshot: SnapshotBinding | None = None,
) -> M0ProxyTrainingManifest:
    """Recompute schedule/metric accounting and verify every frozen training byte."""

    root = _real_directory(output_dir)
    if {path.name for path in root.iterdir()} != _TRAINING_OUTPUT_FILES:
        raise ExperimentalM0ProxyError("M0 training output file set is not exact")
    manifest = M0ProxyTrainingManifest.model_validate(_strict_json(root / "manifest.json"))
    for name, expected in manifest.output_sha256.items():
        if hash_file(_regular_file(root / name)) != expected:
            raise ExperimentalM0ProxyError(f"M0 training output hash differs: {name}")
    schedule = M0EpochSchedule.model_validate(_strict_json(root / "epoch_schedule.json"))
    if schedule.schedule_sha256 != manifest.epoch_schedule_sha256:
        raise ExperimentalM0ProxyError("M0 manifest schedule hash differs")
    if schedule.selected_record_set_sha256 != manifest.selected_record_set_sha256:
        raise ExperimentalM0ProxyError("M0 manifest selected set differs")
    if schedule.batch_count != manifest.optimizer_steps:
        raise ExperimentalM0ProxyError("M0 manifest optimizer steps differ from schedule")
    if schedule.selected_count != manifest.examples_exposed:
        raise ExperimentalM0ProxyError("M0 manifest exposure count differs from schedule")
    if schedule.selected_component_count != manifest.selected_component_count or not math.isclose(
        schedule.loss_normalization_weight,
        manifest.loss_normalization_weight,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ExperimentalM0ProxyError("M0 manifest ancestry normalization differs from schedule")
    predictions: list[M0ProxyPrediction] = []
    with _regular_file(root / "predictions.jsonl").open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                item = M0ProxyPrediction.model_validate(json.loads(raw))
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ExperimentalM0ProxyError(
                    f"invalid M0 prediction at line {line_number}"
                ) from exc
            if raw != canonical_json_bytes(item.model_dump(mode="json")) + b"\n":
                raise ExperimentalM0ProxyError(f"noncanonical M0 prediction at line {line_number}")
            predictions.append(item)
    if len({item.record_id for item in predictions}) != len(predictions):
        raise ExperimentalM0ProxyError("M0 predictions repeat a record")
    metrics = M0ProxyTrainingMetrics.model_validate(_strict_json(root / "metrics.json"))
    grouped = {
        split: tuple(item for item in predictions if item.split == split) for split in _SPLITS
    }
    expected_diagnostics = {split: _metric_set(grouped[split]) for split in _SPLITS}
    if metrics.diagnostics != expected_diagnostics:
        raise ExperimentalM0ProxyError("M0 metrics differ from predictions")
    if (
        metrics.optimizer_steps != manifest.optimizer_steps
        or metrics.examples_exposed != manifest.examples_exposed
        or metrics.selected_component_count != manifest.selected_component_count
        or not math.isclose(
            metrics.loss_normalization_weight,
            manifest.loss_normalization_weight,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or metrics.initial_state_sha256 != manifest.initial_model_state_sha256
        or metrics.final_state_sha256 != manifest.output_sha256["model.safetensors"]
    ):
        raise ExperimentalM0ProxyError("M0 metrics differ from training manifest")
    try:
        safetensors_torch = cast(Any, importlib.import_module("safetensors.torch"))
        state = safetensors_torch.load(_regular_file(root / "model.safetensors").read_bytes())
    except Exception as exc:
        raise ExperimentalM0ProxyError("M0 output is not a valid safetensors checkpoint") from exc
    if not isinstance(state, dict) or not state:
        raise ExperimentalM0ProxyError("M0 safetensors checkpoint has no state")
    for name in ("manifest.json", "metrics.json", "epoch_schedule.json", "predictions.jsonl"):
        if _HEADLESS_MARKER.encode() in _regular_file(root / name).read_bytes():
            raise ExperimentalM0ProxyError("M0 training metadata leaks model-visible source text")
    if (
        repository_root is not None
        and collect_code_state(_real_directory(repository_root)) != manifest.code
    ):
        raise ExperimentalM0ProxyError("current repository code differs from M0 training freeze")
    external_bindings = (prepared_input_dir, checkpoint, audited_tokenizer_snapshot)
    if any(value is not None for value in external_bindings):
        if any(value is None for value in external_bindings):
            raise ExperimentalM0ProxyError(
                "exact M0 training verification requires prepared input, checkpoint, and tokenizer"
            )
        assert prepared_input_dir is not None
        assert checkpoint is not None
        assert audited_tokenizer_snapshot is not None
        prepared = _real_directory(prepared_input_dir)
        prepared_manifest = verify_experimental_m0_proxy_inputs(prepared)
        if (
            prepared_manifest.artifact_id != manifest.prepared_input_artifact_id
            or hash_file(prepared / "manifest.json") != manifest.prepared_input_manifest_sha256
            or prepared_manifest.dataset_id != manifest.dataset_id
            or prepared_manifest.protocol_config_hash != manifest.protocol_hash
        ):
            raise ExperimentalM0ProxyError("prepared M0 input differs from training manifest")
        _verify_audited_tokenizer_snapshot(audited_tokenizer_snapshot)
        if (
            audited_tokenizer_snapshot.snapshot_content_hash
            != manifest.tokenizer_snapshot_content_hash
            or audited_tokenizer_snapshot.model_id != manifest.pretrained_checkpoint.model_id
            or audited_tokenizer_snapshot.revision != manifest.pretrained_checkpoint.revision
        ):
            raise ExperimentalM0ProxyError("audited tokenizer differs from training manifest")
        if checkpoint.receipt != manifest.pretrained_checkpoint:
            raise ExperimentalM0ProxyError("checkpoint receipt differs from training manifest")
        verify_local_modernbert_checkpoint(checkpoint)
    return manifest


__all__ = [
    "ExperimentalM0ProxyBoundary",
    "ExperimentalM0ProxyError",
    "ExperimentalM0ProxyProtocolConfig",
    "LoadedM0ProxyRuntime",
    "M0EpochSchedule",
    "M0EpochSelectionRecord",
    "M0LocalCheckpointBinding",
    "M0OfficialCheckpointReceipt",
    "M0ProxyExample",
    "M0ProxyInputManifest",
    "M0ProxyInputSummary",
    "M0ProxyPreparedArtifacts",
    "M0ProxyRunBinding",
    "M0ProxyTrainingArtifacts",
    "M0ProxyTrainingManifest",
    "M0ProxyTrainingMetrics",
    "ancestry_normalized_proxy_weights",
    "balanced_proxy_batches",
    "bind_local_modernbert_checkpoint",
    "build_m0_dual_encoder_module",
    "build_m0_epoch_schedule",
    "load_experimental_m0_proxy_config",
    "load_m0_proxy_runtime",
    "prepare_experimental_m0_proxy_inputs",
    "train_m0_proxy_one_epoch",
    "verify_experimental_m0_proxy_inputs",
    "verify_local_modernbert_checkpoint",
    "verify_m0_proxy_training",
]
