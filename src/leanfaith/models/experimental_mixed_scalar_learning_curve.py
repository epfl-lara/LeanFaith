"""Replayable scalar learning curve over the frozen mixed proxy corpus.

This module is intentionally separate from both the earlier deterministic-only
scalar diagnostic and the M0--M3 scientific model path.  It consumes one exact,
content-addressed mixed proxy corpus, exposes only symmetric lexical features
of the two ``headless`` statements, and fits deterministic full-batch logistic
models.  The pseudo-targets are machine supervision rather than F1 labels.

Every entry point fails closed unless the caller explicitly opts in.  No
validation or test record can enter a training prefix; ancestry components are
kept atomic and receive equal total loss weight.  Outputs are immutable and a
verifier deterministically refits every model before accepting an artifact.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.datasets.experimental_mixed_supervision import (
    ExperimentalMixedSupervisionManifest,
    ExperimentalMixedSupervisionRecord,
    verify_experimental_mixed_supervision,
)
from leanfaith.schemas.manifest import CodeState, collect_code_state

_HEX64 = r"^[0-9a-f]{64}$"
_DATASET_ID = r"^experimental_mixed_supervision:[0-9a-f]{64}$"
_RECORD_ID = r"^experimental_mixed_pair:[0-9a-f]{64}$"
_COMPONENT_ID = r"^split-component:[0-9a-f]{64}$"
_PREFIX_ID = r"^experimental-mixed-scalar-prefix:[0-9a-f]{64}$"
_MODEL_ID = r"^experimental-mixed-scalar-model:[0-9a-f]{64}$"
_PREDICTION_ID = r"^experimental-mixed-scalar-prediction:[0-9a-f]{64}$"
_METRIC_ID = r"^experimental-mixed-scalar-metric:[0-9a-f]{64}$"
_EXPERIMENT_ID = r"^experimental-mixed-scalar-curve:[0-9a-f]{64}$"
_TOKEN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*"
    r"|[0-9]+|↔|→|≤|≥|≠|∧|∨|∀|∃|¬|∈|∉|⊆|[^\s]"  # noqa: RUF001
)
_OUTPUT_FILES = frozenset(
    {
        "manifest.json",
        "metrics.jsonl",
        "models.jsonl",
        "predictions.jsonl",
        "prefixes.jsonl",
        "summary.json",
        "summary.md",
    }
)
_TARGET_KEYS = ("not_same_claim", "same_claim")
_SPLIT_KEYS = ("test", "train", "validation")
_SUMMARY_LIMITATIONS = (
    "targets are mixed machine-generated proxies, not semantic or human labels",
    "validation and test partitions are diagnostics only and never enter training",
    "sampling seeds alter component order only, not optimizer initialization",
    "the full-train prefix is identical for every sampling seed",
    "features are symmetric lexical summaries of headless Lean text only",
    "results are ineligible for model selection, calibration, evaluation, or release claims",
)

PseudoTarget = Literal["same_claim", "not_same_claim"]
DiagnosticSplit = Literal["validation", "test"]


class ExperimentalMixedScalarLearningCurveError(ValueError):
    """The mixed-proxy diagnostic failed a policy or replay invariant."""


class ExperimentalMixedScalarLearningCurveConfig(StrictModel):
    """Frozen policy for one record-count-based mixed-proxy curve."""

    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1)
    expected_dataset_id: str = Field(pattern=_DATASET_ID)
    expected_dataset_manifest_sha256: str = Field(pattern=_HEX64)
    feature_schema: Literal["symmetric_headless_lean_scalar_v1"] = (
        "symmetric_headless_lean_scalar_v1"
    )
    representation_views: tuple[Literal["headless"], ...]
    operator_tokens: tuple[str, ...] = Field(min_length=1)
    record_budgets: tuple[int, ...] = Field(min_length=2)
    sampling_seeds: tuple[int, ...] = Field(min_length=2)
    sampling_seed_role: Literal["component_order_only"] = "component_order_only"
    prefix_policy: Literal["seeded_component_order_exact_record_count_greedy_v1"] = (
        "seeded_component_order_exact_record_count_greedy_v1"
    )
    training_split: Literal["train"] = "train"
    diagnostic_splits: tuple[DiagnosticSplit, ...]
    optimizer: Literal["AdamW"] = "AdamW"
    optimizer_randomness: Literal["zero_initialization_full_batch_deterministic"] = (
        "zero_initialization_full_batch_deterministic"
    )
    loss_weighting: Literal["equal_ancestry_component_v1"] = "equal_ancestry_component_v1"
    learning_rate: float = Field(gt=0.0)
    weight_decay: float = Field(ge=0.0)
    update_count: int = Field(gt=0)
    decision_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    dtype: Literal["float64"] = "float64"
    device: Literal["cpu"] = "cpu"
    torch_threads: Literal[1] = 1
    constant_baseline_probability: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _policy_is_coherent(self) -> Self:
        if self.representation_views != ("headless",):
            raise ValueError("the mixed scalar input bundle is exactly headless-only")
        if self.operator_tokens != tuple(dict.fromkeys(self.operator_tokens)):
            raise ValueError("operator_tokens must be ordered and unique")
        if not all(self.operator_tokens):
            raise ValueError("operator_tokens cannot contain an empty token")
        if self.record_budgets != tuple(sorted(set(self.record_budgets))):
            raise ValueError("record_budgets must be positive, sorted, and unique")
        if any(value <= 0 for value in self.record_budgets):
            raise ValueError("record_budgets must be positive")
        if self.sampling_seeds != tuple(sorted(set(self.sampling_seeds))):
            raise ValueError("sampling_seeds must be nonnegative, sorted, and unique")
        if any(value < 0 for value in self.sampling_seeds):
            raise ValueError("sampling_seeds must be nonnegative")
        if self.diagnostic_splits != ("validation", "test"):
            raise ValueError("diagnostic_splits must be exactly validation,test")
        if self.decision_threshold != 0.5:
            raise ValueError("the diagnostic decision threshold is fixed at 0.5")
        if self.constant_baseline_probability != 0.5:
            raise ValueError("the diagnostic constant baseline is fixed at 0.5")
        return self


def load_experimental_mixed_scalar_learning_curve_config(
    path: Path,
) -> LoadedConfig[ExperimentalMixedScalarLearningCurveConfig]:
    return load_config(path, ExperimentalMixedScalarLearningCurveConfig)


class ExperimentalMixedScalarBoundary(StrictModel):
    """Fail-closed boundary copied onto all learned outputs."""

    target_basis: Literal["mixed_machine_proxy"] = "mixed_machine_proxy"
    semantic_prediction: Literal[False] = False
    scientific_training_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False
    release_claim_eligible: Literal[False] = False


class ExperimentalMixedScalarRuntime(StrictModel):
    python_version: str = Field(min_length=1)
    torch_version: str = Field(min_length=1)
    device: Literal["cpu"] = "cpu"
    dtype: Literal["float64"] = "float64"
    torch_threads: Literal[1] = 1
    deterministic_algorithms: Literal[True] = True


class ExperimentalMixedScalarPrefix(ExperimentalMixedScalarBoundary):
    """Exact, component-atomic membership for one training record budget."""

    schema_version: Literal[1] = 1
    prefix_id: str = Field(pattern=_PREFIX_ID)
    dataset_id: str = Field(pattern=_DATASET_ID)
    sampling_seed: int = Field(ge=0)
    sampling_seed_role: Literal["component_order_only"] = "component_order_only"
    record_budget: int = Field(gt=0)
    record_count: int = Field(gt=0)
    component_count: int = Field(gt=0)
    record_ids: tuple[str, ...] = Field(min_length=1)
    component_ids: tuple[str, ...] = Field(min_length=1)
    target_counts: dict[str, int]
    target_basis_counts: dict[str, int]
    record_set_sha256: str = Field(pattern=_HEX64)
    component_set_sha256: str = Field(pattern=_HEX64)
    is_full_train_partition: bool
    contains_validation_records: Literal[False] = False
    contains_test_records: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.record_count != self.record_budget or self.record_count != len(self.record_ids):
            raise ValueError("prefix must contain exactly its requested record budget")
        if self.component_count != len(self.component_ids):
            raise ValueError("prefix component count does not reconcile")
        if self.record_ids != tuple(sorted(set(self.record_ids))):
            raise ValueError("prefix record IDs must be sorted and unique")
        if self.component_ids != tuple(sorted(set(self.component_ids))):
            raise ValueError("prefix component IDs must be sorted and unique")
        if any(not re.match(_RECORD_ID, value) for value in self.record_ids):
            raise ValueError("prefix contains a malformed record ID")
        if any(not re.match(_COMPONENT_ID, value) for value in self.component_ids):
            raise ValueError("prefix contains a malformed component ID")
        if tuple(self.target_counts) != _TARGET_KEYS:
            raise ValueError("prefix target counts are not canonical")
        if any(value <= 0 for value in self.target_counts.values()):
            raise ValueError("every prefix must contain both proxy targets")
        if sum(self.target_counts.values()) != self.record_count:
            raise ValueError("prefix target counts do not reconcile")
        if (
            not self.target_basis_counts
            or tuple(self.target_basis_counts) != tuple(sorted(self.target_basis_counts))
            or any(value <= 0 for value in self.target_basis_counts.values())
            or sum(self.target_basis_counts.values()) != self.record_count
        ):
            raise ValueError("prefix target-basis counts do not reconcile")
        if self.record_set_sha256 != _id_set_sha256(
            "experimental_mixed_scalar_record_set_v1", self.record_ids
        ):
            raise ValueError("prefix record-set hash differs")
        if self.component_set_sha256 != _id_set_sha256(
            "experimental_mixed_scalar_component_set_v1", self.component_ids
        ):
            raise ValueError("prefix component-set hash differs")
        expected = _content_id(
            "experimental-mixed-scalar-prefix",
            self.model_dump(mode="json", exclude={"prefix_id"}),
        )
        if self.prefix_id != expected:
            raise ValueError("prefix_id differs from canonical content")
        return self


class ExperimentalMixedScalarModel(ExperimentalMixedScalarBoundary):
    schema_version: Literal[1] = 1
    model_id: str = Field(pattern=_MODEL_ID)
    model_kind: Literal["symmetric_headless_scalar_logistic_v1"] = (
        "symmetric_headless_scalar_logistic_v1"
    )
    dataset_id: str = Field(pattern=_DATASET_ID)
    profile_id: str = Field(min_length=1)
    config_hash: str = Field(pattern=_HEX64)
    runtime: ExperimentalMixedScalarRuntime
    prefix_id: str = Field(pattern=_PREFIX_ID)
    sampling_seed: int = Field(ge=0)
    sampling_seed_role: Literal["component_order_only"] = "component_order_only"
    record_budget: int = Field(gt=0)
    training_record_count: int = Field(gt=0)
    training_component_count: int = Field(gt=0)
    training_target_counts: dict[str, int]
    training_target_basis_counts: dict[str, int]
    training_record_set_sha256: str = Field(pattern=_HEX64)
    training_component_set_sha256: str = Field(pattern=_HEX64)
    loss_weighting: Literal["equal_ancestry_component_v1"] = "equal_ancestry_component_v1"
    feature_schema: Literal["symmetric_headless_lean_scalar_v1"] = (
        "symmetric_headless_lean_scalar_v1"
    )
    feature_names: tuple[str, ...] = Field(min_length=1)
    feature_means: tuple[float, ...] = Field(min_length=1)
    feature_scales: tuple[float, ...] = Field(min_length=1)
    weights: tuple[float, ...] = Field(min_length=1)
    bias: float
    optimizer: Literal["AdamW"] = "AdamW"
    learning_rate: float = Field(gt=0.0)
    weight_decay: float = Field(ge=0.0)
    update_count: int = Field(gt=0)

    @field_validator("feature_means", "feature_scales", "weights")
    @classmethod
    def _finite_vectors(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("model vectors must be finite")
        return value

    @field_validator("bias")
    @classmethod
    def _finite_bias(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("model bias must be finite")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.training_record_count != self.record_budget:
            raise ValueError("model training count differs from record budget")
        if tuple(self.training_target_counts) != _TARGET_KEYS:
            raise ValueError("model target counts are not canonical")
        if sum(self.training_target_counts.values()) != self.training_record_count:
            raise ValueError("model target counts do not reconcile")
        if (
            not self.training_target_basis_counts
            or tuple(self.training_target_basis_counts)
            != tuple(sorted(self.training_target_basis_counts))
            or sum(self.training_target_basis_counts.values()) != self.training_record_count
        ):
            raise ValueError("model target-basis counts do not reconcile")
        width = len(self.feature_names)
        if (
            width == 0
            or len(set(self.feature_names)) != width
            or len(self.feature_means) != width
            or len(self.feature_scales) != width
            or len(self.weights) != width
        ):
            raise ValueError("feature metadata and weights must have one common width")
        if any(value <= 0.0 for value in self.feature_scales):
            raise ValueError("feature scales must be positive")
        expected = _content_id(
            "experimental-mixed-scalar-model",
            self.model_dump(mode="json", exclude={"model_id"}),
        )
        if self.model_id != expected:
            raise ValueError("model_id differs from canonical content")
        return self


class ExperimentalMixedScalarPrediction(ExperimentalMixedScalarBoundary):
    schema_version: Literal[1] = 1
    prediction_id: str = Field(pattern=_PREDICTION_ID)
    dataset_id: str = Field(pattern=_DATASET_ID)
    model_id: str = Field(pattern=_MODEL_ID)
    record_id: str = Field(pattern=_RECORD_ID)
    split: DiagnosticSplit
    pseudo_target: PseudoTarget
    pseudo_same_claim_score: float = Field(ge=0.0, le=1.0)
    fixed_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    pseudo_prediction: PseudoTarget

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.fixed_threshold != 0.5:
            raise ValueError("prediction threshold must be fixed at 0.5")
        expected_prediction = (
            "same_claim" if self.pseudo_same_claim_score >= 0.5 else "not_same_claim"
        )
        if self.pseudo_prediction != expected_prediction:
            raise ValueError("pseudo prediction differs from the fixed threshold")
        expected = _content_id(
            "experimental-mixed-scalar-prediction",
            self.model_dump(mode="json", exclude={"prediction_id"}),
        )
        if self.prediction_id != expected:
            raise ValueError("prediction_id differs from canonical content")
        return self


class ExperimentalMixedScalarMetrics(ExperimentalMixedScalarBoundary):
    schema_version: Literal[1] = 1
    metric_id: str = Field(pattern=_METRIC_ID)
    dataset_id: str = Field(pattern=_DATASET_ID)
    model_id: str = Field(pattern=_MODEL_ID)
    prefix_id: str = Field(pattern=_PREFIX_ID)
    sampling_seed: int = Field(ge=0)
    record_budget: int = Field(gt=0)
    split: DiagnosticSplit
    record_count: int = Field(gt=0)
    target_counts: dict[str, int]
    pseudo_auprc: float = Field(ge=0.0, le=1.0)
    pseudo_accuracy: float = Field(ge=0.0, le=1.0)
    pseudo_balanced_accuracy: float = Field(ge=0.0, le=1.0)
    pseudo_brier: float = Field(ge=0.0, le=1.0)
    pseudo_log_loss: float = Field(ge=0.0)
    constant_auprc: float = Field(ge=0.0, le=1.0)
    constant_accuracy: float = Field(ge=0.0, le=1.0)
    constant_balanced_accuracy: float = Field(ge=0.0, le=1.0)
    constant_brier: float = Field(ge=0.0, le=1.0)
    constant_log_loss: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if tuple(self.target_counts) != _TARGET_KEYS:
            raise ValueError("metric target counts are not canonical")
        if any(value <= 0 for value in self.target_counts.values()):
            raise ValueError("diagnostic metrics require both proxy targets")
        if sum(self.target_counts.values()) != self.record_count:
            raise ValueError("metric target counts do not reconcile")
        numeric = (
            self.pseudo_auprc,
            self.pseudo_accuracy,
            self.pseudo_balanced_accuracy,
            self.pseudo_brier,
            self.pseudo_log_loss,
            self.constant_auprc,
            self.constant_accuracy,
            self.constant_balanced_accuracy,
            self.constant_brier,
            self.constant_log_loss,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("metrics must be finite")
        expected = _content_id(
            "experimental-mixed-scalar-metric",
            self.model_dump(mode="json", exclude={"metric_id"}),
        )
        if self.metric_id != expected:
            raise ValueError("metric_id differs from canonical content")
        return self


class ExperimentalMixedScalarAggregate(StrictModel):
    schema_version: Literal[1] = 1
    record_budget: int = Field(gt=0)
    split: DiagnosticSplit
    sampling_seed_count: int = Field(gt=0)
    unique_training_record_set_count: int = Field(gt=0)
    pseudo_auprc_mean: float = Field(ge=0.0, le=1.0)
    pseudo_auprc_min: float = Field(ge=0.0, le=1.0)
    pseudo_auprc_max: float = Field(ge=0.0, le=1.0)
    pseudo_balanced_accuracy_mean: float = Field(ge=0.0, le=1.0)
    pseudo_brier_mean: float = Field(ge=0.0, le=1.0)
    descriptive_only: Literal[True] = True

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if not self.pseudo_auprc_min <= self.pseudo_auprc_mean <= self.pseudo_auprc_max:
            raise ValueError("aggregate AUPRC mean lies outside its observed range")
        return self


class ExperimentalMixedScalarSummary(ExperimentalMixedScalarBoundary):
    schema_version: Literal[1] = 1
    experiment_id: str = Field(pattern=_EXPERIMENT_ID)
    dataset_id: str = Field(pattern=_DATASET_ID)
    profile_id: str = Field(min_length=1)
    model_count: int = Field(gt=0)
    prediction_count: int = Field(gt=0)
    metric_count: int = Field(gt=0)
    prefix_count: int = Field(gt=0)
    record_budgets: tuple[int, ...] = Field(min_length=1)
    sampling_seeds: tuple[int, ...] = Field(min_length=1)
    split_record_counts: dict[str, int]
    prefixes: tuple[ExperimentalMixedScalarPrefix, ...] = Field(min_length=1)
    descriptive_aggregates: tuple[ExperimentalMixedScalarAggregate, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = _SUMMARY_LIMITATIONS
    statement: Literal[
        "mixed-proxy learnability diagnostic; not autoformalization-faithfulness evidence"
    ] = "mixed-proxy learnability diagnostic; not autoformalization-faithfulness evidence"

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if tuple(self.split_record_counts) != _SPLIT_KEYS:
            raise ValueError("split record counts are not canonical")
        if sum(self.split_record_counts.values()) <= 0:
            raise ValueError("split record counts are empty")
        if self.prefix_count != len(self.prefixes):
            raise ValueError("summary prefix count does not reconcile")
        if self.limitations != _SUMMARY_LIMITATIONS:
            raise ValueError("summary limitations are policy-fixed")
        return self


class ExperimentalMixedScalarInputBinding(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(gt=0)


class ExperimentalMixedScalarManifest(ExperimentalMixedScalarBoundary):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["experimental_mixed_scalar_learning_curve_v1"] = (
        "experimental_mixed_scalar_learning_curve_v1"
    )
    experiment_id: str = Field(pattern=_EXPERIMENT_ID)
    profile_id: str = Field(min_length=1)
    dataset_id: str = Field(pattern=_DATASET_ID)
    config_hash: str = Field(pattern=_HEX64)
    config: ExperimentalMixedScalarLearningCurveConfig
    code: CodeState
    repository_root: str = Field(min_length=1)
    runtime: ExperimentalMixedScalarRuntime
    dataset_manifest: ExperimentalMixedScalarInputBinding
    prefix_count: int = Field(gt=0)
    model_count: int = Field(gt=0)
    prediction_count: int = Field(gt=0)
    metric_count: int = Field(gt=0)
    output_sha256: dict[str, str]
    required_opt_in_flag: Literal["--allow-experimental-mixed-supervision"] = (
        "--allow-experimental-mixed-supervision"
    )
    allowed_purpose: Literal["learning_curve"] = "learning_curve"
    checkpoint_selected: Literal[False] = False
    calibration_fitted: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.profile_id != self.config.profile_id:
            raise ValueError("manifest profile differs from embedded config")
        if self.dataset_id != self.config.expected_dataset_id:
            raise ValueError("manifest dataset differs from pinned config")
        if self.dataset_manifest.sha256 != self.config.expected_dataset_manifest_sha256:
            raise ValueError("manifest dataset hash differs from pinned config")
        if self.config_hash != hash_canonical(self.config.model_dump(mode="json")):
            raise ValueError("config hash differs from embedded config")
        if self.code.git_dirty or self.code.code_tree_hash is None or self.code.untracked_files:
            raise ValueError("freeze requires a clean, fully tracked code tree")
        if set(self.output_sha256) != _OUTPUT_FILES - {"manifest.json"}:
            raise ValueError("manifest does not bind the exact non-manifest outputs")
        if not Path(self.repository_root).is_absolute():
            raise ValueError("repository_root must be absolute")
        return self


class ExperimentalMixedScalarArtifacts(StrictModel):
    output_dir: Path
    manifest_path: Path
    experiment_id: str = Field(pattern=_EXPERIMENT_ID)
    prefix_count: int = Field(gt=0)
    model_count: int = Field(gt=0)
    prediction_count: int = Field(gt=0)
    metric_count: int = Field(gt=0)
    replayed: bool


def _content_id(prefix: str, payload: Mapping[str, object]) -> str:
    return f"{prefix}:{hash_canonical(payload)}"


def _id_set_sha256(schema: str, values: Sequence[str]) -> str:
    return hash_canonical({"schema": schema, "ids": list(values)})


def _normalized_difference(left: int, right: int) -> float:
    return abs(left - right) / max(1, left + right)


def _tokenize(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(value))


def feature_names(config: ExperimentalMixedScalarLearningCurveConfig) -> tuple[str, ...]:
    return (
        "headless:normalized_char_count_difference",
        "headless:normalized_token_count_difference",
        "headless:token_multiset_jaccard",
        "headless:token_set_jaccard",
        *(
            f"headless:normalized_operator_count_difference:{operator}"
            for operator in config.operator_tokens
        ),
    )


def extract_symmetric_headless_features(
    record: ExperimentalMixedSupervisionRecord,
    *,
    config: ExperimentalMixedScalarLearningCurveConfig,
) -> tuple[float, ...]:
    """Read only the two headless strings and return swap-invariant scalars."""

    left = record.source.headless
    right = record.candidate.headless
    left_tokens = _tokenize(left)
    right_tokens = _tokenize(right)
    left_counter = Counter(left_tokens)
    right_counter = Counter(right_tokens)
    multiset_intersection = sum((left_counter & right_counter).values())
    multiset_union = sum((left_counter | right_counter).values())
    left_set = set(left_tokens)
    right_set = set(right_tokens)
    values = (
        _normalized_difference(len(left), len(right)),
        _normalized_difference(len(left_tokens), len(right_tokens)),
        multiset_intersection / max(1, multiset_union),
        len(left_set & right_set) / max(1, len(left_set | right_set)),
        *(
            _normalized_difference(left.count(operator), right.count(operator))
            for operator in config.operator_tokens
        ),
    )
    if len(values) != len(feature_names(config)) or any(
        not math.isfinite(value) for value in values
    ):
        raise ExperimentalMixedScalarLearningCurveError("invalid headless feature vector")
    return values


def _component_order(
    component_ids: Sequence[str], *, dataset_id: str, sampling_seed: int
) -> tuple[str, ...]:
    unique = tuple(sorted(set(component_ids)))
    if len(unique) != len(component_ids):
        raise ExperimentalMixedScalarLearningCurveError("component input repeats an ID")
    return tuple(
        sorted(
            unique,
            key=lambda component_id: hash_canonical(
                {
                    "schema": "experimental_mixed_scalar_component_order_v1",
                    "dataset_id": dataset_id,
                    "sampling_seed": sampling_seed,
                    "component_id": component_id,
                }
            ),
        )
    )


def _make_prefix(
    records: Sequence[ExperimentalMixedSupervisionRecord],
    *,
    config: ExperimentalMixedScalarLearningCurveConfig,
    sampling_seed: int,
    record_budget: int,
    full_train_count: int,
) -> ExperimentalMixedScalarPrefix:
    ordered_records = tuple(sorted(records, key=lambda item: item.record_id))
    record_ids = tuple(item.record_id for item in ordered_records)
    component_ids = tuple(sorted({item.split_component_id for item in ordered_records}))
    targets = Counter(item.pseudo_target for item in ordered_records)
    bases = Counter(item.pseudo_target_basis for item in ordered_records)
    data: dict[str, object] = {
        "dataset_id": config.expected_dataset_id,
        "sampling_seed": sampling_seed,
        "sampling_seed_role": config.sampling_seed_role,
        "record_budget": record_budget,
        "record_count": len(ordered_records),
        "component_count": len(component_ids),
        "record_ids": record_ids,
        "component_ids": component_ids,
        "target_counts": {
            "not_same_claim": targets["not_same_claim"],
            "same_claim": targets["same_claim"],
        },
        "target_basis_counts": dict(sorted(bases.items())),
        "record_set_sha256": _id_set_sha256("experimental_mixed_scalar_record_set_v1", record_ids),
        "component_set_sha256": _id_set_sha256(
            "experimental_mixed_scalar_component_set_v1", component_ids
        ),
        "is_full_train_partition": record_budget == full_train_count,
    }
    placeholder = ExperimentalMixedScalarPrefix.model_construct(
        **cast(
            Any,
            {
                "prefix_id": f"experimental-mixed-scalar-prefix:{'0' * 64}",
                **data,
            },
        )
    )
    identity_payload = placeholder.model_dump(mode="json", exclude={"prefix_id"})
    return ExperimentalMixedScalarPrefix.model_validate(
        {
            "prefix_id": _content_id("experimental-mixed-scalar-prefix", identity_payload),
            **data,
        }
    )


def component_atomic_record_prefixes(
    records: Sequence[ExperimentalMixedSupervisionRecord],
    *,
    config: ExperimentalMixedScalarLearningCurveConfig,
    sampling_seed: int,
) -> dict[int, tuple[ExperimentalMixedSupervisionRecord, ...]]:
    """Build exact nested record-count prefixes without splitting ancestry."""

    train = tuple(item for item in records if item.split == config.training_split)
    if not train:
        raise ExperimentalMixedScalarLearningCurveError("training partition is empty")
    if config.record_budgets[-1] != len(train):
        raise ExperimentalMixedScalarLearningCurveError(
            "largest record budget must equal the frozen train record count"
        )
    by_component: dict[str, tuple[ExperimentalMixedSupervisionRecord, ...]] = {}
    staged: dict[str, list[ExperimentalMixedSupervisionRecord]] = defaultdict(list)
    for item in train:
        staged[item.split_component_id].append(item)
    for component_id, items in staged.items():
        by_component[component_id] = tuple(sorted(items, key=lambda item: item.record_id))
    ordered_components = _component_order(
        tuple(by_component),
        dataset_id=config.expected_dataset_id,
        sampling_seed=sampling_seed,
    )
    selected: set[str] = set()
    selected_record_count = 0
    output: dict[int, tuple[ExperimentalMixedSupervisionRecord, ...]] = {}
    for budget in config.record_budgets:
        remaining = budget - selected_record_count
        if remaining < 0:
            raise ExperimentalMixedScalarLearningCurveError("record budgets are not nested")
        for component_id in ordered_components:
            if component_id in selected:
                continue
            size = len(by_component[component_id])
            if size > remaining:
                continue
            selected.add(component_id)
            selected_record_count += size
            remaining -= size
            if remaining == 0:
                break
        if remaining != 0:
            raise ExperimentalMixedScalarLearningCurveError(
                f"cannot fill record budget {budget} without splitting an ancestry component"
            )
        prefix = tuple(
            sorted(
                (item for component_id in selected for item in by_component[component_id]),
                key=lambda item: item.record_id,
            )
        )
        if len(prefix) != budget or any(item.split != "train" for item in prefix):
            raise ExperimentalMixedScalarLearningCurveError(
                "component prefix contains a wrong split or record count"
            )
        counts = Counter(item.pseudo_target for item in prefix)
        if set(counts) != {"same_claim", "not_same_claim"}:
            raise ExperimentalMixedScalarLearningCurveError(
                f"record prefix {budget} lacks one proxy target"
            )
        output[budget] = prefix
    if {item.record_id for item in output[config.record_budgets[-1]]} != {
        item.record_id for item in train
    }:
        raise ExperimentalMixedScalarLearningCurveError(
            "full record prefix differs from the complete training partition"
        )
    return output


def ancestry_normalized_loss_weights(
    records: Sequence[ExperimentalMixedSupervisionRecord],
) -> tuple[float, ...]:
    """Give every ancestry component equal total full-batch loss weight."""

    if not records:
        raise ExperimentalMixedScalarLearningCurveError("loss weights require records")
    if any(item.split != "train" for item in records):
        raise ExperimentalMixedScalarLearningCurveError(
            "loss weights reject validation or test records"
        )
    component_sizes = Counter(item.split_component_id for item in records)
    raw = tuple(1.0 / component_sizes[item.split_component_id] for item in records)
    scale = len(records) / sum(raw)
    weights = tuple(value * scale for value in raw)
    totals: dict[str, float] = defaultdict(float)
    for item, weight in zip(records, weights, strict=True):
        totals[item.split_component_id] += weight
    expected = next(iter(totals.values()))
    if any(
        not math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-12) for value in totals.values()
    ):
        raise ExperimentalMixedScalarLearningCurveError(
            "ancestry components do not receive equal total loss weight"
        )
    return weights


def _require_torch() -> Any:
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - optional environment
        raise ExperimentalMixedScalarLearningCurveError(
            "mixed scalar training requires the pinned optional runtime; "
            "run `uv sync --group local-inference`"
        ) from exc
    return torch


def _runtime(
    torch: Any,
    *,
    config: ExperimentalMixedScalarLearningCurveConfig,
) -> ExperimentalMixedScalarRuntime:
    return ExperimentalMixedScalarRuntime(
        python_version=sys.version.split()[0],
        torch_version=str(torch.__version__),
        device=config.device,
        dtype=config.dtype,
        torch_threads=config.torch_threads,
        deterministic_algorithms=True,
    )


def _feature_statistics(
    rows: Sequence[Sequence[float]],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if not rows:
        raise ExperimentalMixedScalarLearningCurveError("feature statistics require rows")
    width = len(rows[0])
    if width == 0 or any(len(row) != width for row in rows):
        raise ExperimentalMixedScalarLearningCurveError("feature widths are inconsistent")
    count = len(rows)
    means = tuple(math.fsum(row[index] for row in rows) / count for index in range(width))
    scales = tuple(
        max(
            math.sqrt(math.fsum((row[index] - means[index]) ** 2 for row in rows) / count),
            1e-12,
        )
        for index in range(width)
    )
    return means, scales


def _standardize(
    rows: Sequence[Sequence[float]],
    means: Sequence[float],
    scales: Sequence[float],
) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple((value - means[index]) / scales[index] for index, value in enumerate(row))
        for row in rows
    )


def fit_experimental_mixed_scalar_model(
    records: Sequence[ExperimentalMixedSupervisionRecord],
    *,
    prefix: ExperimentalMixedScalarPrefix,
    config: ExperimentalMixedScalarLearningCurveConfig,
    config_hash: str,
    runtime: ExperimentalMixedScalarRuntime | None = None,
) -> ExperimentalMixedScalarModel:
    """Fit one deterministic model using only the prefix's training records."""

    ordered_records = tuple(sorted(records, key=lambda item: item.record_id))
    observed_ids = tuple(item.record_id for item in ordered_records)
    if observed_ids != prefix.record_ids:
        raise ExperimentalMixedScalarLearningCurveError(
            "model records differ from the replay-bound prefix membership"
        )
    if any(item.split != "train" for item in ordered_records):
        raise ExperimentalMixedScalarLearningCurveError(
            "validation/test records are forbidden in mixed scalar training"
        )
    torch = _require_torch()
    observed_runtime = runtime or _runtime(torch, config=config)
    if observed_runtime.device != "cpu" or observed_runtime.dtype != "float64":
        raise ExperimentalMixedScalarLearningCurveError(
            "only deterministic CPU float64 is admitted"
        )
    torch.set_num_threads(config.torch_threads)
    torch.use_deterministic_algorithms(True)
    names = feature_names(config)
    raw = tuple(
        extract_symmetric_headless_features(item, config=config) for item in ordered_records
    )
    means, scales = _feature_statistics(raw)
    standardized = _standardize(raw, means, scales)
    labels = tuple(1.0 if item.pseudo_target == "same_claim" else 0.0 for item in ordered_records)
    normalized_weights = ancestry_normalized_loss_weights(ordered_records)
    inputs = torch.tensor(standardized, dtype=torch.float64, device="cpu")
    targets = torch.tensor(labels, dtype=torch.float64, device="cpu")
    loss_weights = torch.tensor(normalized_weights, dtype=torch.float64, device="cpu")
    layer = torch.nn.Linear(len(names), 1, bias=True, dtype=torch.float64, device="cpu")
    with torch.no_grad():
        layer.weight.zero_()
        layer.bias.zero_()
    optimizer = torch.optim.AdamW(
        layer.parameters(),
        lr=config.learning_rate,
        weight_decay=config.weight_decay,
    )
    for _ in range(config.update_count):
        optimizer.zero_grad(set_to_none=True)
        logits = layer(inputs).squeeze(-1)
        per_record_loss = torch.nn.functional.binary_cross_entropy_with_logits(
            logits,
            targets,
            reduction="none",
        )
        loss = (per_record_loss * loss_weights).sum() / loss_weights.sum()
        loss.backward()
        optimizer.step()
    weights = tuple(float(value) for value in layer.weight.detach().reshape(-1).tolist())
    bias = float(layer.bias.detach().item())
    data: dict[str, object] = {
        "dataset_id": config.expected_dataset_id,
        "profile_id": config.profile_id,
        "config_hash": config_hash,
        "runtime": observed_runtime,
        "prefix_id": prefix.prefix_id,
        "sampling_seed": prefix.sampling_seed,
        "sampling_seed_role": config.sampling_seed_role,
        "record_budget": prefix.record_budget,
        "training_record_count": prefix.record_count,
        "training_component_count": prefix.component_count,
        "training_target_counts": prefix.target_counts,
        "training_target_basis_counts": prefix.target_basis_counts,
        "training_record_set_sha256": prefix.record_set_sha256,
        "training_component_set_sha256": prefix.component_set_sha256,
        "loss_weighting": config.loss_weighting,
        "feature_schema": config.feature_schema,
        "feature_names": names,
        "feature_means": means,
        "feature_scales": scales,
        "weights": weights,
        "bias": bias,
        "optimizer": config.optimizer,
        "learning_rate": config.learning_rate,
        "weight_decay": config.weight_decay,
        "update_count": config.update_count,
    }
    placeholder = ExperimentalMixedScalarModel.model_construct(
        **cast(
            Any,
            {
                "model_id": f"experimental-mixed-scalar-model:{'0' * 64}",
                **data,
            },
        )
    )
    identity_payload = placeholder.model_dump(mode="json", exclude={"model_id"})
    return ExperimentalMixedScalarModel.model_validate(
        {
            "model_id": _content_id("experimental-mixed-scalar-model", identity_payload),
            **data,
        }
    )


def score_experimental_mixed_scalar_model(
    model: ExperimentalMixedScalarModel,
    record: ExperimentalMixedSupervisionRecord,
    *,
    config: ExperimentalMixedScalarLearningCurveConfig,
) -> float:
    values = extract_symmetric_headless_features(record, config=config)
    standardized = tuple(
        (value - model.feature_means[index]) / model.feature_scales[index]
        for index, value in enumerate(values)
    )
    logit = model.bias + math.fsum(
        weight * value for weight, value in zip(model.weights, standardized, strict=True)
    )
    if logit >= 0:
        return 1.0 / (1.0 + math.exp(-min(logit, 750.0)))
    exponential = math.exp(max(logit, -750.0))
    return exponential / (1.0 + exponential)


def _make_prediction(
    *,
    model: ExperimentalMixedScalarModel,
    record: ExperimentalMixedSupervisionRecord,
    config: ExperimentalMixedScalarLearningCurveConfig,
) -> ExperimentalMixedScalarPrediction:
    if record.split not in config.diagnostic_splits:
        raise ExperimentalMixedScalarLearningCurveError(
            "predictions are published only for validation and test diagnostics"
        )
    score = score_experimental_mixed_scalar_model(model, record, config=config)
    data: dict[str, object] = {
        "dataset_id": model.dataset_id,
        "model_id": model.model_id,
        "record_id": record.record_id,
        "split": record.split,
        "pseudo_target": record.pseudo_target,
        "pseudo_same_claim_score": score,
        "fixed_threshold": 0.5,
        "pseudo_prediction": "same_claim" if score >= 0.5 else "not_same_claim",
    }
    placeholder = ExperimentalMixedScalarPrediction.model_construct(
        **cast(
            Any,
            {
                "prediction_id": f"experimental-mixed-scalar-prediction:{'0' * 64}",
                **data,
            },
        )
    )
    identity_payload = placeholder.model_dump(mode="json", exclude={"prediction_id"})
    return ExperimentalMixedScalarPrediction.model_validate(
        {
            "prediction_id": _content_id("experimental-mixed-scalar-prediction", identity_payload),
            **data,
        }
    )


def _average_precision(labels: Sequence[int], scores: Sequence[float]) -> float:
    """Tie-safe non-interpolated average precision."""

    if len(labels) != len(scores) or not labels:
        raise ExperimentalMixedScalarLearningCurveError("AUPRC inputs are misaligned")
    positive_count = sum(labels)
    if positive_count == 0 or positive_count == len(labels):
        raise ExperimentalMixedScalarLearningCurveError("AUPRC requires both targets")
    ordered = sorted(zip(scores, labels, strict=True), key=lambda item: item[0], reverse=True)
    true_positive = 0
    false_positive = 0
    prior_recall = 0.0
    area = 0.0
    index = 0
    while index < len(ordered):
        score = ordered[index][0]
        group_positive = 0
        group_count = 0
        while index < len(ordered) and ordered[index][0] == score:
            group_positive += ordered[index][1]
            group_count += 1
            index += 1
        true_positive += group_positive
        false_positive += group_count - group_positive
        recall = true_positive / positive_count
        precision = true_positive / (true_positive + false_positive)
        area += (recall - prior_recall) * precision
        prior_recall = recall
    return area


def _metric_values(
    labels: Sequence[int], scores: Sequence[float], *, threshold: float
) -> dict[str, float]:
    if len(labels) != len(scores) or not labels:
        raise ExperimentalMixedScalarLearningCurveError("metric inputs are misaligned")
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        raise ExperimentalMixedScalarLearningCurveError("metrics require both targets")
    predictions = tuple(score >= threshold for score in scores)
    true_positive = sum(
        prediction and label == 1 for prediction, label in zip(predictions, labels, strict=True)
    )
    true_negative = sum(
        not prediction and label == 0 for prediction, label in zip(predictions, labels, strict=True)
    )
    clipped = tuple(min(max(score, 1e-15), 1.0 - 1e-15) for score in scores)
    return {
        "auprc": _average_precision(labels, scores),
        "accuracy": (true_positive + true_negative) / len(labels),
        "balanced_accuracy": 0.5 * (true_positive / positives + true_negative / negatives),
        "brier": math.fsum(
            (score - label) ** 2 for score, label in zip(scores, labels, strict=True)
        )
        / len(labels),
        "log_loss": -math.fsum(
            label * math.log(score) + (1 - label) * math.log(1.0 - score)
            for score, label in zip(clipped, labels, strict=True)
        )
        / len(labels),
    }


def _make_metrics(
    *,
    model: ExperimentalMixedScalarModel,
    prefix: ExperimentalMixedScalarPrefix,
    predictions: Sequence[ExperimentalMixedScalarPrediction],
    config: ExperimentalMixedScalarLearningCurveConfig,
) -> ExperimentalMixedScalarMetrics:
    if not predictions:
        raise ExperimentalMixedScalarLearningCurveError("metrics require predictions")
    split = predictions[0].split
    if any(item.model_id != model.model_id or item.split != split for item in predictions):
        raise ExperimentalMixedScalarLearningCurveError("metric predictions mix models/splits")
    labels = tuple(1 if item.pseudo_target == "same_claim" else 0 for item in predictions)
    scores = tuple(item.pseudo_same_claim_score for item in predictions)
    observed = _metric_values(labels, scores, threshold=0.5)
    constant = _metric_values(
        labels,
        tuple(config.constant_baseline_probability for _ in labels),
        threshold=0.5,
    )
    counts = Counter(item.pseudo_target for item in predictions)
    data: dict[str, object] = {
        "dataset_id": model.dataset_id,
        "model_id": model.model_id,
        "prefix_id": prefix.prefix_id,
        "sampling_seed": model.sampling_seed,
        "record_budget": model.record_budget,
        "split": split,
        "record_count": len(predictions),
        "target_counts": {
            "not_same_claim": counts["not_same_claim"],
            "same_claim": counts["same_claim"],
        },
        "pseudo_auprc": observed["auprc"],
        "pseudo_accuracy": observed["accuracy"],
        "pseudo_balanced_accuracy": observed["balanced_accuracy"],
        "pseudo_brier": observed["brier"],
        "pseudo_log_loss": observed["log_loss"],
        "constant_auprc": constant["auprc"],
        "constant_accuracy": constant["accuracy"],
        "constant_balanced_accuracy": constant["balanced_accuracy"],
        "constant_brier": constant["brier"],
        "constant_log_loss": constant["log_loss"],
    }
    placeholder = ExperimentalMixedScalarMetrics.model_construct(
        **cast(
            Any,
            {
                "metric_id": f"experimental-mixed-scalar-metric:{'0' * 64}",
                **data,
            },
        )
    )
    identity_payload = placeholder.model_dump(mode="json", exclude={"metric_id"})
    return ExperimentalMixedScalarMetrics.model_validate(
        {
            "metric_id": _content_id("experimental-mixed-scalar-metric", identity_payload),
            **data,
        }
    )


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in records)


def _no_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key {key!r}")
        output[key] = value
    return output


def _reject_nonfinite_json(value: str) -> float:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _parse_json_bytes(raw: bytes, *, source: Path) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_no_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExperimentalMixedScalarLearningCurveError(f"invalid JSON at {source}: {exc}") from exc


def _strict_json(path: Path) -> dict[str, object]:
    regular = _regular_file(path)
    raw = regular.read_bytes()
    value = _parse_json_bytes(raw, source=regular)
    if not isinstance(value, dict):
        raise ExperimentalMixedScalarLearningCurveError(f"expected JSON object at {regular}")
    if raw != canonical_json_bytes(value) + b"\n":
        raise ExperimentalMixedScalarLearningCurveError(f"non-canonical JSON at {regular}")
    return value


def _load_jsonl[ModelT: StrictModel](path: Path, model: type[ModelT]) -> tuple[ModelT, ...]:
    regular = _regular_file(path)
    output: list[ModelT] = []
    for line_number, raw_line in enumerate(regular.read_bytes().splitlines(keepends=True), 1):
        raw = _parse_json_bytes(raw_line, source=regular)
        try:
            item = model.model_validate(raw)
        except ValueError as exc:
            raise ExperimentalMixedScalarLearningCurveError(
                f"invalid {model.__name__} at {regular}:{line_number}: {exc}"
            ) from exc
        if raw_line != canonical_json_bytes(item.model_dump(mode="json")) + b"\n":
            raise ExperimentalMixedScalarLearningCurveError(
                f"non-canonical {model.__name__} at {regular}:{line_number}"
            )
        output.append(item)
    return tuple(output)


def _load_mixed_records(dataset_dir: Path) -> tuple[ExperimentalMixedSupervisionRecord, ...]:
    records = _load_jsonl(
        dataset_dir / "records.jsonl",
        ExperimentalMixedSupervisionRecord,
    )
    if not records or tuple(sorted(records, key=lambda item: item.record_id)) != records:
        raise ExperimentalMixedScalarLearningCurveError(
            "mixed records are empty or not in canonical record-ID order"
        )
    if len({item.record_id for item in records}) != len(records):
        raise ExperimentalMixedScalarLearningCurveError("mixed records repeat a record ID")
    return records


def _mean_within_observed_range(values: Sequence[float]) -> float:
    if not values:
        raise ExperimentalMixedScalarLearningCurveError("descriptive mean requires values")
    lower = min(values)
    upper = max(values)
    return min(upper, max(lower, math.fsum(values) / len(values)))


def _descriptive_aggregates(
    metrics: Sequence[ExperimentalMixedScalarMetrics],
    models: Sequence[ExperimentalMixedScalarModel],
    *,
    config: ExperimentalMixedScalarLearningCurveConfig,
) -> tuple[ExperimentalMixedScalarAggregate, ...]:
    model_by_id = {item.model_id: item for item in models}
    output: list[ExperimentalMixedScalarAggregate] = []
    for budget in config.record_budgets:
        for split in config.diagnostic_splits:
            group = tuple(
                item for item in metrics if item.record_budget == budget and item.split == split
            )
            if len(group) != len(config.sampling_seeds):
                raise ExperimentalMixedScalarLearningCurveError(
                    "descriptive aggregate lacks one configured sampling seed"
                )
            auprc = tuple(item.pseudo_auprc for item in group)
            balanced = tuple(item.pseudo_balanced_accuracy for item in group)
            brier = tuple(item.pseudo_brier for item in group)
            output.append(
                ExperimentalMixedScalarAggregate(
                    record_budget=budget,
                    split=split,
                    sampling_seed_count=len(group),
                    unique_training_record_set_count=len(
                        {model_by_id[item.model_id].training_record_set_sha256 for item in group}
                    ),
                    pseudo_auprc_mean=_mean_within_observed_range(auprc),
                    pseudo_auprc_min=min(auprc),
                    pseudo_auprc_max=max(auprc),
                    pseudo_balanced_accuracy_mean=math.fsum(balanced) / len(balanced),
                    pseudo_brier_mean=math.fsum(brier) / len(brier),
                )
            )
    return tuple(output)


def _make_summary(
    *,
    experiment_id: str,
    records: Sequence[ExperimentalMixedSupervisionRecord],
    prefixes: Sequence[ExperimentalMixedScalarPrefix],
    models: Sequence[ExperimentalMixedScalarModel],
    predictions: Sequence[ExperimentalMixedScalarPrediction],
    metrics: Sequence[ExperimentalMixedScalarMetrics],
    config: ExperimentalMixedScalarLearningCurveConfig,
) -> ExperimentalMixedScalarSummary:
    split_counts = Counter(item.split for item in records)
    return ExperimentalMixedScalarSummary(
        experiment_id=experiment_id,
        dataset_id=config.expected_dataset_id,
        profile_id=config.profile_id,
        model_count=len(models),
        prediction_count=len(predictions),
        metric_count=len(metrics),
        prefix_count=len(prefixes),
        record_budgets=config.record_budgets,
        sampling_seeds=config.sampling_seeds,
        split_record_counts={
            "test": split_counts["test"],
            "train": split_counts["train"],
            "validation": split_counts["validation"],
        },
        prefixes=tuple(prefixes),
        descriptive_aggregates=_descriptive_aggregates(metrics, models, config=config),
    )


def _summary_markdown(
    summary: ExperimentalMixedScalarSummary,
    metrics: Sequence[ExperimentalMixedScalarMetrics],
) -> bytes:
    lines = [
        "# Experimental mixed-proxy scalar learning curve",
        "",
        f"Experiment: `{summary.experiment_id}`",
        f"Dataset: `{summary.dataset_id}`",
        "",
        "> This diagnostic learns mixed machine proxy targets, not semantic faithfulness.",
        "> It cannot select or calibrate a model and cannot support evaluation or release claims.",
        "",
        "| Train records | Seed | Split | Pseudo-AUPRC | Balanced accuracy | "
        "Brier | Constant AUPRC |",
        "|---:|---:|---|---:|---:|---:|---:|",
    ]
    for item in metrics:
        lines.append(
            f"| {item.record_budget} | {item.sampling_seed} | {item.split} | "
            f"{item.pseudo_auprc:.6f} | {item.pseudo_balanced_accuracy:.6f} | "
            f"{item.pseudo_brier:.6f} | {item.constant_auprc:.6f} |"
        )
    lines.extend(("", "## Exact component-atomic prefixes", ""))
    for prefix in summary.prefixes:
        targets = ", ".join(f"{name}={count}" for name, count in prefix.target_counts.items())
        bases = ", ".join(f"{name}={count}" for name, count in prefix.target_basis_counts.items())
        full = "; full train partition" if prefix.is_full_train_partition else ""
        lines.append(
            f"- records={prefix.record_count}, components={prefix.component_count}, "
            f"seed={prefix.sampling_seed}; targets: {targets}; bases: {bases}{full}."
        )
    lines.extend(("", "## Fixed limitations", ""))
    lines.extend(f"- {item}." for item in summary.limitations)
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _experiment_id(
    *,
    config_hash: str,
    dataset_manifest_sha256: str,
    code_tree_hash: str,
    runtime: ExperimentalMixedScalarRuntime,
    models: Sequence[ExperimentalMixedScalarModel],
) -> str:
    return _content_id(
        "experimental-mixed-scalar-curve",
        {
            "schema": "experimental_mixed_scalar_learning_curve_v1",
            "config_hash": config_hash,
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "code_tree_hash": code_tree_hash,
            "runtime": runtime.model_dump(mode="json"),
            "model_ids": [item.model_id for item in models],
        },
    )


def _run_models(
    records: Sequence[ExperimentalMixedSupervisionRecord],
    *,
    config: ExperimentalMixedScalarLearningCurveConfig,
    config_hash: str,
    runtime: ExperimentalMixedScalarRuntime,
) -> tuple[
    tuple[ExperimentalMixedScalarPrefix, ...],
    tuple[ExperimentalMixedScalarModel, ...],
    tuple[ExperimentalMixedScalarPrediction, ...],
    tuple[ExperimentalMixedScalarMetrics, ...],
]:
    diagnostic = {
        split: tuple(item for item in records if item.split == split)
        for split in config.diagnostic_splits
    }
    if any(not items for items in diagnostic.values()):
        raise ExperimentalMixedScalarLearningCurveError("a diagnostic split is empty")
    if any(
        set(Counter(item.pseudo_target for item in items)) != {"same_claim", "not_same_claim"}
        for items in diagnostic.values()
    ):
        raise ExperimentalMixedScalarLearningCurveError(
            "every diagnostic split must contain both proxy targets"
        )
    full_train_count = sum(item.split == "train" for item in records)
    prefixes: list[ExperimentalMixedScalarPrefix] = []
    models: list[ExperimentalMixedScalarModel] = []
    predictions: list[ExperimentalMixedScalarPrediction] = []
    metrics: list[ExperimentalMixedScalarMetrics] = []
    for sampling_seed in config.sampling_seeds:
        selections = component_atomic_record_prefixes(
            records,
            config=config,
            sampling_seed=sampling_seed,
        )
        prior_ids: set[str] = set()
        for budget in config.record_budgets:
            selected = selections[budget]
            prefix = _make_prefix(
                selected,
                config=config,
                sampling_seed=sampling_seed,
                record_budget=budget,
                full_train_count=full_train_count,
            )
            current_ids = set(prefix.record_ids)
            if not prior_ids.issubset(current_ids):
                raise ExperimentalMixedScalarLearningCurveError(
                    "record-count prefixes are not nested within a sampling seed"
                )
            prior_ids = current_ids
            model = fit_experimental_mixed_scalar_model(
                selected,
                prefix=prefix,
                config=config,
                config_hash=config_hash,
                runtime=runtime,
            )
            prefixes.append(prefix)
            models.append(model)
            for split in config.diagnostic_splits:
                current_predictions = tuple(
                    _make_prediction(model=model, record=item, config=config)
                    for item in diagnostic[split]
                )
                predictions.extend(current_predictions)
                metrics.append(
                    _make_metrics(
                        model=model,
                        prefix=prefix,
                        predictions=current_predictions,
                        config=config,
                    )
                )
    full_hashes = {
        item.record_set_sha256
        for item in prefixes
        if item.record_budget == config.record_budgets[-1]
    }
    if len(full_hashes) != 1:
        raise ExperimentalMixedScalarLearningCurveError(
            "full train prefix must be identical across sampling seeds"
        )
    return (
        tuple(sorted(prefixes, key=lambda item: (item.record_budget, item.sampling_seed))),
        tuple(sorted(models, key=lambda item: item.model_id)),
        tuple(sorted(predictions, key=lambda item: item.prediction_id)),
        tuple(
            sorted(
                metrics,
                key=lambda item: (item.record_budget, item.sampling_seed, item.split),
            )
        ),
    )


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlinks(path: Path, *, allow_missing: bool) -> Path:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise ExperimentalMixedScalarLearningCurveError(
                f"required path is absent: {current}"
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ExperimentalMixedScalarLearningCurveError(f"path contains a symlink: {current}")
    return absolute


def _regular_file(path: Path) -> Path:
    safe = _reject_symlinks(path, allow_missing=False)
    metadata = os.lstat(safe)
    if not stat.S_ISREG(metadata.st_mode):
        raise ExperimentalMixedScalarLearningCurveError(f"expected regular file: {safe}")
    return safe


def _real_directory(path: Path) -> Path:
    safe = _reject_symlinks(path, allow_missing=False)
    metadata = os.lstat(safe)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ExperimentalMixedScalarLearningCurveError(f"expected directory: {safe}")
    return safe


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validated_repository_root(path: Path) -> Path:
    root = _real_directory(path)
    _regular_file(root / "PLAN.md")
    _regular_file(root / "pyproject.toml")
    expected = root / "src/leanfaith/models/experimental_mixed_scalar_learning_curve.py"
    if Path(__file__).resolve() != expected:
        raise ExperimentalMixedScalarLearningCurveError(
            "repository root does not contain the executing mixed scalar module"
        )
    return root


def _verify_payloads(output: Path, payloads: Mapping[str, bytes]) -> bool:
    safe = _real_directory(output)
    if {path.name for path in safe.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalMixedScalarLearningCurveError("existing output file set is not exact")
    for name, payload in sorted(payloads.items()):
        if _regular_file(safe / name).read_bytes() != payload:
            raise ExperimentalMixedScalarLearningCurveError(
                f"existing mixed scalar output differs: {safe / name}"
            )
    return True


def _write_or_replay(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    if set(payloads) != _OUTPUT_FILES:
        raise ExperimentalMixedScalarLearningCurveError("output payload set is not exact")
    output = _reject_symlinks(output_dir, allow_missing=True)
    if output.exists():
        return _verify_payloads(output, payloads)
    output.parent.mkdir(parents=True, exist_ok=True)
    _real_directory(output.parent)
    output = _reject_symlinks(output, allow_missing=True)
    if output.exists():
        return _verify_payloads(output, payloads)
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
        descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return False


def _verify_clean_code(code: CodeState) -> str:
    if code.git_dirty or code.code_tree_hash is None or code.untracked_files:
        raise ExperimentalMixedScalarLearningCurveError(
            "mixed scalar freeze requires a clean, fully tracked code tree"
        )
    return code.code_tree_hash


def run_experimental_mixed_scalar_learning_curve(
    *,
    repo_root: Path,
    dataset_dir: Path,
    output_dir: Path,
    config: ExperimentalMixedScalarLearningCurveConfig,
    config_hash: str | None = None,
    allow_experimental_mixed_supervision: bool,
) -> ExperimentalMixedScalarArtifacts:
    """Fit and freeze the mixed-proxy curve after an explicit opt-in."""

    if not allow_experimental_mixed_supervision:
        raise ExperimentalMixedScalarLearningCurveError(
            "running requires --allow-experimental-mixed-supervision"
        )
    repo = _validated_repository_root(repo_root)
    dataset = _real_directory(dataset_dir)
    output = _reject_symlinks(output_dir, allow_missing=True)
    if _paths_overlap(output, dataset) or _paths_overlap(output, repo):
        raise ExperimentalMixedScalarLearningCurveError(
            "output must be disjoint from both repository and dataset directories"
        )
    expected_config_hash = hash_canonical(config.model_dump(mode="json"))
    effective_config_hash = config_hash or expected_config_hash
    if effective_config_hash != expected_config_hash:
        raise ExperimentalMixedScalarLearningCurveError("config hash differs from config")
    dataset_manifest_path = _regular_file(dataset / "manifest.json")
    if hash_file(dataset_manifest_path) != config.expected_dataset_manifest_sha256:
        raise ExperimentalMixedScalarLearningCurveError(
            "dataset manifest hash differs from pinned config"
        )
    dataset_manifest = verify_experimental_mixed_supervision(
        dataset,
        verify_external_inputs=False,
    )
    if dataset_manifest.dataset_id != config.expected_dataset_id:
        raise ExperimentalMixedScalarLearningCurveError("dataset ID differs from config")
    if "learning_curve" not in dataset_manifest.allowed_purposes:
        raise ExperimentalMixedScalarLearningCurveError(
            "mixed corpus does not admit learning-curve use"
        )
    records = _load_mixed_records(dataset)
    code = collect_code_state(repo)
    code_tree_hash = _verify_clean_code(code)
    torch = _require_torch()
    runtime = _runtime(torch, config=config)
    prefixes, models, predictions, metrics = _run_models(
        records,
        config=config,
        config_hash=effective_config_hash,
        runtime=runtime,
    )
    experiment_id = _experiment_id(
        config_hash=effective_config_hash,
        dataset_manifest_sha256=config.expected_dataset_manifest_sha256,
        code_tree_hash=code_tree_hash,
        runtime=runtime,
        models=models,
    )
    summary = _make_summary(
        experiment_id=experiment_id,
        records=records,
        prefixes=prefixes,
        models=models,
        predictions=predictions,
        metrics=metrics,
        config=config,
    )
    non_manifest: dict[str, bytes] = {
        "metrics.jsonl": _canonical_jsonl(metrics),
        "models.jsonl": _canonical_jsonl(models),
        "predictions.jsonl": _canonical_jsonl(predictions),
        "prefixes.jsonl": _canonical_jsonl(prefixes),
        "summary.json": canonical_json_bytes(summary.model_dump(mode="json")) + b"\n",
        "summary.md": _summary_markdown(summary, metrics),
    }
    manifest = ExperimentalMixedScalarManifest(
        experiment_id=experiment_id,
        profile_id=config.profile_id,
        dataset_id=config.expected_dataset_id,
        config_hash=effective_config_hash,
        config=config,
        code=code,
        repository_root=str(repo),
        runtime=runtime,
        dataset_manifest=ExperimentalMixedScalarInputBinding(
            path=str(dataset_manifest_path),
            sha256=config.expected_dataset_manifest_sha256,
            byte_count=dataset_manifest_path.stat().st_size,
        ),
        prefix_count=len(prefixes),
        model_count=len(models),
        prediction_count=len(predictions),
        metric_count=len(metrics),
        output_sha256={
            name: hashlib.sha256(payload).hexdigest() for name, payload in non_manifest.items()
        },
    )
    payloads = {
        **non_manifest,
        "manifest.json": canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    }
    post_dataset_manifest = verify_experimental_mixed_supervision(
        dataset,
        verify_external_inputs=False,
    )
    if (
        post_dataset_manifest != dataset_manifest
        or hash_file(dataset_manifest_path) != config.expected_dataset_manifest_sha256
        or dataset_manifest_path.stat().st_size != manifest.dataset_manifest.byte_count
    ):
        raise ExperimentalMixedScalarLearningCurveError(
            "bound mixed corpus changed during training"
        )
    if collect_code_state(repo) != code:
        raise ExperimentalMixedScalarLearningCurveError(
            "repository code state changed during training"
        )
    replayed = _write_or_replay(output, payloads)
    verify_experimental_mixed_scalar_learning_curve(output, dataset_dir=dataset)
    return ExperimentalMixedScalarArtifacts(
        output_dir=output,
        manifest_path=output / "manifest.json",
        experiment_id=experiment_id,
        prefix_count=len(prefixes),
        model_count=len(models),
        prediction_count=len(predictions),
        metric_count=len(metrics),
        replayed=replayed,
    )


def verify_experimental_mixed_scalar_learning_curve(
    output_dir: Path,
    *,
    dataset_dir: Path | None = None,
) -> ExperimentalMixedScalarManifest:
    """Refit deterministically and verify every byte and input binding."""

    root = _real_directory(output_dir)
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalMixedScalarLearningCurveError("mixed scalar output file set is not exact")
    for name in _OUTPUT_FILES:
        _regular_file(root / name)
    try:
        manifest = ExperimentalMixedScalarManifest.model_validate(
            _strict_json(root / "manifest.json")
        )
        summary = ExperimentalMixedScalarSummary.model_validate(_strict_json(root / "summary.json"))
    except ValueError as exc:
        raise ExperimentalMixedScalarLearningCurveError(
            f"invalid mixed scalar metadata: {exc}"
        ) from exc
    for name, expected in manifest.output_sha256.items():
        if hash_file(root / name) != expected:
            raise ExperimentalMixedScalarLearningCurveError(f"output hash differs: {name}")
    prefixes = _load_jsonl(root / "prefixes.jsonl", ExperimentalMixedScalarPrefix)
    models = _load_jsonl(root / "models.jsonl", ExperimentalMixedScalarModel)
    predictions = _load_jsonl(root / "predictions.jsonl", ExperimentalMixedScalarPrediction)
    metrics = _load_jsonl(root / "metrics.jsonl", ExperimentalMixedScalarMetrics)
    if (len(prefixes), len(models), len(predictions), len(metrics)) != (
        manifest.prefix_count,
        manifest.model_count,
        manifest.prediction_count,
        manifest.metric_count,
    ):
        raise ExperimentalMixedScalarLearningCurveError(
            "published artifact counts differ from manifest"
        )
    resolved_dataset = (
        Path(manifest.dataset_manifest.path).parent if dataset_dir is None else dataset_dir
    )
    resolved_dataset = _real_directory(resolved_dataset)
    repository_root = _validated_repository_root(Path(manifest.repository_root))
    if _paths_overlap(root, resolved_dataset) or _paths_overlap(root, repository_root):
        raise ExperimentalMixedScalarLearningCurveError(
            "published output overlaps its repository or dataset input"
        )
    current_code = collect_code_state(repository_root)
    if current_code != manifest.code:
        raise ExperimentalMixedScalarLearningCurveError("bound repository code state differs")
    _verify_clean_code(current_code)
    torch = _require_torch()
    if _runtime(torch, config=manifest.config) != manifest.runtime:
        raise ExperimentalMixedScalarLearningCurveError("bound deterministic runtime differs")
    dataset_manifest_path = _regular_file(resolved_dataset / "manifest.json")
    if (
        hash_file(dataset_manifest_path) != manifest.dataset_manifest.sha256
        or dataset_manifest_path.stat().st_size != manifest.dataset_manifest.byte_count
    ):
        raise ExperimentalMixedScalarLearningCurveError("bound dataset manifest differs")
    source_manifest: ExperimentalMixedSupervisionManifest = verify_experimental_mixed_supervision(
        resolved_dataset,
        verify_external_inputs=False,
    )
    if source_manifest.dataset_id != manifest.dataset_id:
        raise ExperimentalMixedScalarLearningCurveError("bound dataset ID differs")
    records = _load_mixed_records(resolved_dataset)
    expected_prefixes, expected_models, expected_predictions, expected_metrics = _run_models(
        records,
        config=manifest.config,
        config_hash=manifest.config_hash,
        runtime=manifest.runtime,
    )
    if prefixes != expected_prefixes:
        raise ExperimentalMixedScalarLearningCurveError(
            "published prefixes differ from deterministic replay"
        )
    if models != expected_models:
        raise ExperimentalMixedScalarLearningCurveError(
            "published models differ from deterministic refit"
        )
    if predictions != expected_predictions:
        raise ExperimentalMixedScalarLearningCurveError(
            "published predictions differ from deterministic replay"
        )
    if metrics != expected_metrics:
        raise ExperimentalMixedScalarLearningCurveError(
            "published metrics differ from deterministic replay"
        )
    code_tree_hash = manifest.code.code_tree_hash
    assert code_tree_hash is not None
    expected_experiment_id = _experiment_id(
        config_hash=manifest.config_hash,
        dataset_manifest_sha256=manifest.dataset_manifest.sha256,
        code_tree_hash=code_tree_hash,
        runtime=manifest.runtime,
        models=expected_models,
    )
    if manifest.experiment_id != expected_experiment_id:
        raise ExperimentalMixedScalarLearningCurveError("experiment ID differs from refit")
    expected_summary = _make_summary(
        experiment_id=expected_experiment_id,
        records=records,
        prefixes=expected_prefixes,
        models=expected_models,
        predictions=expected_predictions,
        metrics=expected_metrics,
        config=manifest.config,
    )
    if summary != expected_summary:
        raise ExperimentalMixedScalarLearningCurveError(
            "published summary differs from deterministic replay"
        )
    expected_non_manifest = {
        "metrics.jsonl": _canonical_jsonl(expected_metrics),
        "models.jsonl": _canonical_jsonl(expected_models),
        "predictions.jsonl": _canonical_jsonl(expected_predictions),
        "prefixes.jsonl": _canonical_jsonl(expected_prefixes),
        "summary.json": canonical_json_bytes(expected_summary.model_dump(mode="json")) + b"\n",
        "summary.md": _summary_markdown(expected_summary, expected_metrics),
    }
    for name, expected_payload in expected_non_manifest.items():
        if _regular_file(root / name).read_bytes() != expected_payload:
            raise ExperimentalMixedScalarLearningCurveError(
                f"published artifact differs from replay: {name}"
            )
        if manifest.output_sha256[name] != hashlib.sha256(expected_payload).hexdigest():
            raise ExperimentalMixedScalarLearningCurveError(
                f"manifest binding differs from replay: {name}"
            )
    return manifest


__all__ = [
    "ExperimentalMixedScalarAggregate",
    "ExperimentalMixedScalarArtifacts",
    "ExperimentalMixedScalarLearningCurveConfig",
    "ExperimentalMixedScalarLearningCurveError",
    "ExperimentalMixedScalarManifest",
    "ExperimentalMixedScalarMetrics",
    "ExperimentalMixedScalarModel",
    "ExperimentalMixedScalarPrediction",
    "ExperimentalMixedScalarPrefix",
    "ancestry_normalized_loss_weights",
    "component_atomic_record_prefixes",
    "extract_symmetric_headless_features",
    "feature_names",
    "fit_experimental_mixed_scalar_model",
    "load_experimental_mixed_scalar_learning_curve_config",
    "run_experimental_mixed_scalar_learning_curve",
    "score_experimental_mixed_scalar_model",
    "verify_experimental_mixed_scalar_learning_curve",
]
