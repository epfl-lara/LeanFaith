"""Diagnostic learning curve over provisional deterministic intentions.

This module is deliberately separate from M0--M3 and from the semantic
``PredictionRecord`` contract.  It learns only the pseudo-target attached to
the opt-in experimental machine-supervision corpus.  Its outputs cannot be
used for model selection, calibration, evaluation, or scientific claims.
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
from typing import Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.datasets.experimental_machine_supervision import (
    ExperimentalMachineSupervisionManifest,
    ExperimentalMachineSupervisionRecord,
    load_experimental_machine_supervision,
    verify_experimental_machine_supervision,
)
from leanfaith.schemas.manifest import CodeState, collect_code_state

_HEX64 = r"^[0-9a-f]{64}$"
_DATASET_ID = r"^experimental-machine-supervision:[0-9a-f]{64}$"
_EXPERIMENT_ID = r"^experimental-scalar-curve:[0-9a-f]{64}$"
_MODEL_ID = r"^experimental-scalar-model:[0-9a-f]{64}$"
_PREDICTION_ID = r"^experimental-scalar-prediction:[0-9a-f]{64}$"
_METRIC_ID = r"^experimental-scalar-metric:[0-9a-f]{64}$"
_RECORD_ID = r"^experimental-machine-pair:[0-9a-f]{64}$"
_COMPONENT_ID = r"^split-component:[0-9a-f]{64}$"
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
        "summary.json",
        "summary.md",
    }
)
_TARGET_KEYS = ("not_same_claim", "same_claim")
_SUMMARY_LIMITATIONS = (
    "targets are deterministic transformation intentions, not semantic labels",
    "component-order seeds are descriptive resamples, not independent training randomness",
    "the full component budget is the same training set for every sampling seed",
    "validation and test results cannot support model selection, calibration, or evaluation claims",
    "family support is imbalanced and sparse families do not support family-level conclusions",
)

DiagnosticSplit = Literal["validation", "test"]
PseudoTarget = Literal["same_claim", "not_same_claim"]


class ExperimentalScalarLearningCurveError(ValueError):
    """The diagnostic curve failed a policy, provenance, or replay check."""


class ExperimentalScalarLearningCurveConfig(StrictModel):
    """Fixed, untuned policy for the first pseudo-target learning curve."""

    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1)
    expected_dataset_id: str = Field(pattern=_DATASET_ID)
    expected_dataset_manifest_sha256: str = Field(pattern=_HEX64)
    feature_schema: Literal["symmetric_lean_scalar_v1"] = "symmetric_lean_scalar_v1"
    representation_views: tuple[Literal["headless", "signature_explicit"], ...]
    operator_tokens: tuple[str, ...] = Field(min_length=1)
    component_budgets: tuple[int, ...] = Field(min_length=1)
    sampling_seeds: tuple[int, ...] = Field(min_length=2)
    sampling_seed_role: Literal["component_order_only"] = "component_order_only"
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
    def _fixed_policy_is_canonical(self) -> ExperimentalScalarLearningCurveConfig:
        if self.representation_views != ("headless", "signature_explicit"):
            raise ValueError("the diagnostic input bundle is exactly headless+signature_explicit")
        if self.operator_tokens != tuple(dict.fromkeys(self.operator_tokens)):
            raise ValueError("operator_tokens must be ordered and unique")
        if not all(self.operator_tokens):
            raise ValueError("operator_tokens cannot contain an empty token")
        if self.component_budgets != tuple(sorted(set(self.component_budgets))):
            raise ValueError("component_budgets must be positive, sorted, and unique")
        if any(value <= 0 for value in self.component_budgets):
            raise ValueError("component_budgets must be positive")
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


def load_experimental_scalar_learning_curve_config(
    path: Path,
) -> LoadedConfig[ExperimentalScalarLearningCurveConfig]:
    return load_config(path, ExperimentalScalarLearningCurveConfig)


class ExperimentalBoundary(StrictModel):
    """Fail-closed eligibility boundary copied onto every learned artifact."""

    target_basis: Literal["deterministic_transformation_intention"] = (
        "deterministic_transformation_intention"
    )
    semantic_prediction: Literal[False] = False
    scientific_training_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    release_claim_eligible: Literal[False] = False


class ExperimentalScalarRuntime(StrictModel):
    python_version: str = Field(min_length=1)
    torch_version: str = Field(min_length=1)
    device: Literal["cpu"] = "cpu"
    dtype: Literal["float64"] = "float64"
    torch_threads: Literal[1] = 1
    deterministic_algorithms: Literal[True] = True


class ExperimentalScalarModel(ExperimentalBoundary):
    schema_version: Literal[1] = 1
    model_id: str = Field(pattern=_MODEL_ID)
    model_kind: Literal["symmetric_scalar_logistic_v1"] = "symmetric_scalar_logistic_v1"
    dataset_id: str = Field(pattern=_DATASET_ID)
    profile_id: str = Field(min_length=1)
    config_hash: str = Field(pattern=_HEX64)
    runtime: ExperimentalScalarRuntime
    sampling_seed: int = Field(ge=0)
    sampling_seed_role: Literal["component_order_only"] = "component_order_only"
    component_budget: int = Field(gt=0)
    training_component_ids: tuple[str, ...] = Field(min_length=1)
    training_record_count: int = Field(gt=0)
    training_target_counts: dict[str, int]
    training_family_counts: dict[str, int]
    training_record_set_sha256: str = Field(pattern=_HEX64)
    loss_weighting: Literal["equal_ancestry_component_v1"] = "equal_ancestry_component_v1"
    feature_schema: Literal["symmetric_lean_scalar_v1"] = "symmetric_lean_scalar_v1"
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
    def _coherent(self) -> ExperimentalScalarModel:
        if self.training_component_ids != tuple(sorted(set(self.training_component_ids))):
            raise ValueError("training_component_ids must be sorted and unique")
        if any(not re.match(_COMPONENT_ID, value) for value in self.training_component_ids):
            raise ValueError("training_component_ids contain a malformed component")
        if self.component_budget != len(self.training_component_ids):
            raise ValueError("component budget differs from selected component count")
        if tuple(self.training_target_counts) != _TARGET_KEYS:
            raise ValueError("training_target_counts must have exactly both pseudo-targets")
        if any(value <= 0 for value in self.training_target_counts.values()):
            raise ValueError("every training prefix must contain both pseudo-targets")
        if sum(self.training_target_counts.values()) != self.training_record_count:
            raise ValueError("training target counts do not reconcile")
        if tuple(self.training_family_counts) != tuple(sorted(self.training_family_counts)):
            raise ValueError("training_family_counts must use canonical family order")
        if not self.training_family_counts or any(
            value <= 0 for value in self.training_family_counts.values()
        ):
            raise ValueError("training_family_counts must contain positive counts")
        if sum(self.training_family_counts.values()) != self.training_record_count:
            raise ValueError("training family counts do not reconcile")
        width = len(self.feature_names)
        if (
            width == 0
            or len(set(self.feature_names)) != width
            or len(self.feature_means) != width
            or len(self.feature_scales) != width
            or len(self.weights) != width
        ):
            raise ValueError("feature names/statistics/weights must have one common width")
        if any(value <= 0.0 for value in self.feature_scales):
            raise ValueError("feature scales must be positive")
        expected = _model_id(self.model_dump(mode="json", exclude={"model_id"}))
        if self.model_id != expected:
            raise ValueError("model_id does not match canonical model content")
        return self


class ExperimentalScalarPrediction(ExperimentalBoundary):
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
    def _coherent(self) -> ExperimentalScalarPrediction:
        if self.fixed_threshold != 0.5:
            raise ValueError("prediction threshold must be the fixed value 0.5")
        expected_prediction = (
            "same_claim"
            if self.pseudo_same_claim_score >= self.fixed_threshold
            else "not_same_claim"
        )
        if self.pseudo_prediction != expected_prediction:
            raise ValueError("pseudo_prediction differs from the fixed threshold")
        expected = _prediction_id(self.model_dump(mode="json", exclude={"prediction_id"}))
        if self.prediction_id != expected:
            raise ValueError("prediction_id does not match canonical prediction content")
        return self


class ExperimentalScalarMetrics(ExperimentalBoundary):
    schema_version: Literal[1] = 1
    metric_id: str = Field(pattern=_METRIC_ID)
    dataset_id: str = Field(pattern=_DATASET_ID)
    model_id: str = Field(pattern=_MODEL_ID)
    sampling_seed: int = Field(ge=0)
    component_budget: int = Field(gt=0)
    training_record_count: int = Field(gt=0)
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
    def _coherent(self) -> ExperimentalScalarMetrics:
        if tuple(self.target_counts) != _TARGET_KEYS:
            raise ValueError("metric target_counts must contain exactly both pseudo-targets")
        if any(value <= 0 for value in self.target_counts.values()):
            raise ValueError("diagnostic metrics require both pseudo-targets")
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
            raise ValueError("diagnostic metrics must be finite")
        expected = _metric_id(self.model_dump(mode="json", exclude={"metric_id"}))
        if self.metric_id != expected:
            raise ValueError("metric_id does not match canonical metric content")
        return self


class ExperimentalScalarPrefixSupport(StrictModel):
    schema_version: Literal[1] = 1
    sampling_seed: int = Field(ge=0)
    sampling_seed_role: Literal["component_order_only"] = "component_order_only"
    component_budget: int = Field(gt=0)
    component_count: int = Field(gt=0)
    record_count: int = Field(gt=0)
    target_counts: dict[str, int]
    family_counts: dict[str, int]
    training_record_set_sha256: str = Field(pattern=_HEX64)
    is_full_budget: bool
    duplicates_training_set_across_sampling_seeds: bool

    @model_validator(mode="after")
    def _coherent(self) -> ExperimentalScalarPrefixSupport:
        if self.component_count != self.component_budget:
            raise ValueError("prefix component count differs from budget")
        if tuple(self.target_counts) != _TARGET_KEYS:
            raise ValueError("prefix target counts are not canonical")
        if sum(self.target_counts.values()) != self.record_count:
            raise ValueError("prefix target counts do not reconcile")
        if tuple(self.family_counts) != tuple(sorted(self.family_counts)):
            raise ValueError("prefix family counts are not canonical")
        if sum(self.family_counts.values()) != self.record_count:
            raise ValueError("prefix family counts do not reconcile")
        if self.is_full_budget and not self.duplicates_training_set_across_sampling_seeds:
            raise ValueError("the full-budget training set must duplicate across sampling seeds")
        return self


class ExperimentalScalarDescriptiveAggregate(StrictModel):
    schema_version: Literal[1] = 1
    component_budget: int = Field(gt=0)
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
    def _coherent(self) -> ExperimentalScalarDescriptiveAggregate:
        if not self.pseudo_auprc_min <= self.pseudo_auprc_mean <= self.pseudo_auprc_max:
            raise ValueError("aggregate AUPRC mean must lie within its observed range")
        return self


class ExperimentalScalarSummary(ExperimentalBoundary):
    schema_version: Literal[1] = 1
    experiment_id: str = Field(pattern=_EXPERIMENT_ID)
    dataset_id: str = Field(pattern=_DATASET_ID)
    profile_id: str = Field(min_length=1)
    model_count: int = Field(gt=0)
    prediction_count: int = Field(gt=0)
    metric_count: int = Field(gt=0)
    component_budgets: tuple[int, ...] = Field(min_length=1)
    sampling_seeds: tuple[int, ...] = Field(min_length=1)
    diagnostic_splits: tuple[DiagnosticSplit, ...]
    family_counts_by_split: dict[str, dict[str, int]]
    prefix_support: tuple[ExperimentalScalarPrefixSupport, ...] = Field(min_length=1)
    descriptive_aggregates: tuple[ExperimentalScalarDescriptiveAggregate, ...] = Field(min_length=1)
    aggregation_scope: Literal["descriptive_across_component_order_seeds_only"] = (
        "descriptive_across_component_order_seeds_only"
    )
    limitations: tuple[str, ...] = _SUMMARY_LIMITATIONS
    statement: Literal[
        "pseudo-target learnability diagnostic; not autoformalization-faithfulness evidence"
    ] = "pseudo-target learnability diagnostic; not autoformalization-faithfulness evidence"

    @model_validator(mode="after")
    def _coherent(self) -> ExperimentalScalarSummary:
        if self.limitations != _SUMMARY_LIMITATIONS:
            raise ValueError("summary limitations are fixed by policy")
        if tuple(self.family_counts_by_split) != ("test", "train", "validation"):
            raise ValueError("family_counts_by_split must use canonical split order")
        return self


class ExperimentalScalarInputBinding(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=1)


class ExperimentalScalarLearningCurveManifest(ExperimentalBoundary):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["experimental_scalar_learning_curve"] = (
        "experimental_scalar_learning_curve"
    )
    experiment_id: str = Field(pattern=_EXPERIMENT_ID)
    profile_id: str = Field(min_length=1)
    dataset_id: str = Field(pattern=_DATASET_ID)
    config_hash: str = Field(pattern=_HEX64)
    config: ExperimentalScalarLearningCurveConfig
    code: CodeState
    repository_root: str = Field(min_length=1)
    runtime: ExperimentalScalarRuntime
    dataset_manifest: ExperimentalScalarInputBinding
    model_count: int = Field(gt=0)
    prediction_count: int = Field(gt=0)
    metric_count: int = Field(gt=0)
    output_sha256: dict[str, str]
    required_opt_in_flag: Literal["--allow-experimental-machine-supervision"] = (
        "--allow-experimental-machine-supervision"
    )
    allowed_purpose: Literal["learning_curve"] = "learning_curve"
    checkpoint_selected: Literal[False] = False
    calibration_fitted: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> ExperimentalScalarLearningCurveManifest:
        if self.profile_id != self.config.profile_id:
            raise ValueError("manifest profile differs from embedded config")
        if self.dataset_id != self.config.expected_dataset_id:
            raise ValueError("manifest dataset differs from the pinned config")
        if self.dataset_manifest.sha256 != self.config.expected_dataset_manifest_sha256:
            raise ValueError("manifest dataset hash differs from the pinned config")
        if self.config_hash != hash_canonical(self.config.model_dump(mode="json")):
            raise ValueError("config_hash differs from the embedded config")
        if self.code.git_dirty or self.code.code_tree_hash is None or self.code.untracked_files:
            raise ValueError("learning-curve freeze requires a clean, fully tracked code tree")
        if set(self.output_sha256) != _OUTPUT_FILES - {"manifest.json"}:
            raise ValueError("output_sha256 does not bind the exact non-manifest outputs")
        if not Path(self.repository_root).is_absolute():
            raise ValueError("repository_root must be absolute")
        return self


class ExperimentalScalarLearningCurveArtifacts(StrictModel):
    output_dir: Path
    manifest_path: Path
    experiment_id: str = Field(pattern=_EXPERIMENT_ID)
    model_count: int = Field(gt=0)
    prediction_count: int = Field(gt=0)
    metric_count: int = Field(gt=0)
    replayed: bool


def _normalized_difference(left: int, right: int) -> float:
    return abs(left - right) / max(1, left + right)


def _tokenize(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(value))


def feature_names(config: ExperimentalScalarLearningCurveConfig) -> tuple[str, ...]:
    names: list[str] = []
    for view in config.representation_views:
        names.extend(
            (
                f"{view}:normalized_char_count_difference",
                f"{view}:normalized_token_count_difference",
                f"{view}:token_multiset_jaccard",
                f"{view}:token_set_jaccard",
            )
        )
        names.extend(
            f"{view}:normalized_operator_count_difference:{operator}"
            for operator in config.operator_tokens
        )
    return tuple(names)


def extract_symmetric_features(
    record: ExperimentalMachineSupervisionRecord,
    *,
    config: ExperimentalScalarLearningCurveConfig,
) -> tuple[float, ...]:
    """Use only the two approved text views; all metadata remains invisible."""

    output: list[float] = []
    for view in config.representation_views:
        left = cast(str, getattr(record.source, view))
        right = cast(str, getattr(record.candidate, view))
        left_tokens = _tokenize(left)
        right_tokens = _tokenize(right)
        left_counter = Counter(left_tokens)
        right_counter = Counter(right_tokens)
        multiset_intersection = sum((left_counter & right_counter).values())
        multiset_union = sum((left_counter | right_counter).values())
        left_set = set(left_tokens)
        right_set = set(right_tokens)
        output.extend(
            (
                _normalized_difference(len(left), len(right)),
                _normalized_difference(len(left_tokens), len(right_tokens)),
                multiset_intersection / max(1, multiset_union),
                len(left_set & right_set) / max(1, len(left_set | right_set)),
            )
        )
        output.extend(
            _normalized_difference(left.count(operator), right.count(operator))
            for operator in config.operator_tokens
        )
    values = tuple(output)
    if len(values) != len(feature_names(config)) or any(
        not math.isfinite(value) for value in values
    ):
        raise ExperimentalScalarLearningCurveError("feature extraction produced invalid output")
    return values


def _component_order(
    component_ids: Sequence[str],
    *,
    dataset_id: str,
    sampling_seed: int,
) -> tuple[str, ...]:
    unique = tuple(sorted(set(component_ids)))
    if len(unique) != len(component_ids):
        raise ExperimentalScalarLearningCurveError("component input must be unique")
    return tuple(
        sorted(
            unique,
            key=lambda component_id: hash_canonical(
                {
                    "schema": "experimental_scalar_component_order_v1",
                    "dataset_id": dataset_id,
                    "sampling_seed": sampling_seed,
                    "component_id": component_id,
                }
            ),
        )
    )


def component_atomic_prefixes(
    records: Sequence[ExperimentalMachineSupervisionRecord],
    *,
    config: ExperimentalScalarLearningCurveConfig,
    sampling_seed: int,
) -> dict[int, tuple[ExperimentalMachineSupervisionRecord, ...]]:
    train = tuple(record for record in records if record.split == "train")
    by_component: dict[str, list[ExperimentalMachineSupervisionRecord]] = defaultdict(list)
    for record in train:
        by_component[record.split_component_id].append(record)
    ordered = _component_order(
        tuple(by_component),
        dataset_id=config.expected_dataset_id,
        sampling_seed=sampling_seed,
    )
    if config.component_budgets[-1] != len(ordered):
        raise ExperimentalScalarLearningCurveError(
            "largest component budget must equal the frozen train component count"
        )
    output: dict[int, tuple[ExperimentalMachineSupervisionRecord, ...]] = {}
    for budget in config.component_budgets:
        selected_components = set(ordered[:budget])
        selected = tuple(
            sorted(
                (record for component in selected_components for record in by_component[component]),
                key=lambda record: record.record_id,
            )
        )
        counts = Counter(record.pseudo_target for record in selected)
        if set(counts) != {"same_claim", "not_same_claim"}:
            raise ExperimentalScalarLearningCurveError(
                f"component prefix {budget} lacks one pseudo-target"
            )
        output[budget] = selected
    return output


def _require_torch() -> Any:
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - depends on optional environment
        raise ExperimentalScalarLearningCurveError(
            "experimental scalar training requires the pinned optional runtime; "
            "run `uv sync --group local-inference`"
        ) from exc
    return torch


def _runtime(
    torch: Any,
    *,
    config: ExperimentalScalarLearningCurveConfig,
) -> ExperimentalScalarRuntime:
    return ExperimentalScalarRuntime(
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
        raise ExperimentalScalarLearningCurveError("feature statistics require training rows")
    width = len(rows[0])
    if width == 0 or any(len(row) != width for row in rows):
        raise ExperimentalScalarLearningCurveError("feature rows have inconsistent widths")
    count = len(rows)
    means = tuple(sum(row[index] for row in rows) / count for index in range(width))
    scales = tuple(
        max(
            math.sqrt(sum((row[index] - means[index]) ** 2 for row in rows) / count),
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


def _model_id(payload: Mapping[str, object]) -> str:
    return f"experimental-scalar-model:{hash_canonical(payload)}"


def _prediction_id(payload: Mapping[str, object]) -> str:
    return f"experimental-scalar-prediction:{hash_canonical(payload)}"


def _metric_id(payload: Mapping[str, object]) -> str:
    return f"experimental-scalar-metric:{hash_canonical(payload)}"


def _record_set_sha256(
    records: Sequence[ExperimentalMachineSupervisionRecord],
) -> str:
    return hash_canonical(
        {
            "schema": "experimental_scalar_training_record_set_v1",
            "record_ids": sorted(record.record_id for record in records),
        }
    )


def _ancestry_normalized_loss_weights(
    records: Sequence[ExperimentalMachineSupervisionRecord],
) -> tuple[float, ...]:
    if not records:
        raise ExperimentalScalarLearningCurveError("loss weights require training records")
    component_sizes = Counter(record.split_component_id for record in records)
    raw = tuple(1.0 / component_sizes[record.split_component_id] for record in records)
    scale = len(records) / sum(raw)
    weights = tuple(value * scale for value in raw)
    component_totals: dict[str, float] = defaultdict(float)
    for record, weight in zip(records, weights, strict=True):
        component_totals[record.split_component_id] += weight
    totals = tuple(component_totals.values())
    if not totals or any(
        not math.isclose(value, totals[0], rel_tol=0.0, abs_tol=1e-12) for value in totals
    ):
        raise ExperimentalScalarLearningCurveError(
            "ancestry-normalized weights do not give components equal total weight"
        )
    return weights


def fit_experimental_scalar_model(
    records: Sequence[ExperimentalMachineSupervisionRecord],
    *,
    config: ExperimentalScalarLearningCurveConfig,
    config_hash: str,
    sampling_seed: int,
    component_budget: int,
    runtime: ExperimentalScalarRuntime | None = None,
) -> ExperimentalScalarModel:
    """Fit one deterministic CPU logistic model; no holdout data are consulted."""

    torch = _require_torch()
    observed_runtime = runtime or _runtime(torch, config=config)
    if observed_runtime.device != "cpu" or observed_runtime.dtype != "float64":
        raise ExperimentalScalarLearningCurveError("only deterministic CPU float64 is admitted")
    torch.set_num_threads(config.torch_threads)
    torch.use_deterministic_algorithms(True)
    names = feature_names(config)
    raw = tuple(extract_symmetric_features(record, config=config) for record in records)
    means, scales = _feature_statistics(raw)
    standardized = _standardize(raw, means, scales)
    labels = tuple(1.0 if record.pseudo_target == "same_claim" else 0.0 for record in records)
    normalized_loss_weights = _ancestry_normalized_loss_weights(records)
    inputs = torch.tensor(standardized, dtype=torch.float64, device="cpu")
    targets = torch.tensor(labels, dtype=torch.float64, device="cpu")
    loss_weights = torch.tensor(
        normalized_loss_weights,
        dtype=torch.float64,
        device="cpu",
    )
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
    components = tuple(sorted({record.split_component_id for record in records}))
    counts = Counter(record.pseudo_target for record in records)
    family_counts = Counter(record.family_id for record in records)
    data: dict[str, object] = {
        "dataset_id": config.expected_dataset_id,
        "profile_id": config.profile_id,
        "config_hash": config_hash,
        "runtime": observed_runtime,
        "sampling_seed": sampling_seed,
        "sampling_seed_role": config.sampling_seed_role,
        "component_budget": component_budget,
        "training_component_ids": components,
        "training_record_count": len(records),
        "training_target_counts": {
            "not_same_claim": counts["not_same_claim"],
            "same_claim": counts["same_claim"],
        },
        "training_family_counts": dict(sorted(family_counts.items())),
        "training_record_set_sha256": _record_set_sha256(records),
        "loss_weighting": config.loss_weighting,
        "feature_names": names,
        "feature_means": means,
        "feature_scales": scales,
        "weights": weights,
        "bias": bias,
        "learning_rate": config.learning_rate,
        "weight_decay": config.weight_decay,
        "update_count": config.update_count,
    }
    placeholder = ExperimentalScalarModel.model_construct(
        **cast(
            Any,
            {"model_id": f"experimental-scalar-model:{'0' * 64}", **data},
        )
    )
    identity_payload = placeholder.model_dump(mode="json", exclude={"model_id"})
    return ExperimentalScalarModel.model_validate({"model_id": _model_id(identity_payload), **data})


def score_experimental_scalar_model(
    model: ExperimentalScalarModel,
    record: ExperimentalMachineSupervisionRecord,
    *,
    config: ExperimentalScalarLearningCurveConfig,
) -> float:
    values = extract_symmetric_features(record, config=config)
    standardized = tuple(
        (value - model.feature_means[index]) / model.feature_scales[index]
        for index, value in enumerate(values)
    )
    logit = model.bias + sum(
        weight * value for weight, value in zip(model.weights, standardized, strict=True)
    )
    if logit >= 0:
        return 1.0 / (1.0 + math.exp(-min(logit, 750.0)))
    exponential = math.exp(max(logit, -750.0))
    return exponential / (1.0 + exponential)


def _make_prediction(
    *,
    model: ExperimentalScalarModel,
    record: ExperimentalMachineSupervisionRecord,
    config: ExperimentalScalarLearningCurveConfig,
) -> ExperimentalScalarPrediction:
    score = score_experimental_scalar_model(model, record, config=config)
    data: dict[str, object] = {
        "dataset_id": model.dataset_id,
        "model_id": model.model_id,
        "record_id": record.record_id,
        "split": record.split,
        "pseudo_target": record.pseudo_target,
        "pseudo_same_claim_score": score,
        "fixed_threshold": config.decision_threshold,
        "pseudo_prediction": (
            "same_claim" if score >= config.decision_threshold else "not_same_claim"
        ),
    }
    placeholder = ExperimentalScalarPrediction.model_construct(
        **cast(
            Any,
            {"prediction_id": f"experimental-scalar-prediction:{'0' * 64}", **data},
        )
    )
    identity_payload = placeholder.model_dump(mode="json", exclude={"prediction_id"})
    return ExperimentalScalarPrediction.model_validate(
        {"prediction_id": _prediction_id(identity_payload), **data}
    )


def _average_precision(labels: Sequence[int], scores: Sequence[float]) -> float:
    """Tie-safe non-interpolated average precision."""

    if len(labels) != len(scores) or not labels:
        raise ExperimentalScalarLearningCurveError("AUPRC inputs must be nonempty and aligned")
    positive_count = sum(labels)
    if positive_count == 0 or positive_count == len(labels):
        raise ExperimentalScalarLearningCurveError("AUPRC requires both pseudo-targets")
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
    labels: Sequence[int],
    scores: Sequence[float],
    *,
    threshold: float,
) -> dict[str, float]:
    if len(labels) != len(scores) or not labels:
        raise ExperimentalScalarLearningCurveError("metric inputs must be nonempty and aligned")
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        raise ExperimentalScalarLearningCurveError("metrics require both pseudo-targets")
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
        "brier": sum((score - label) ** 2 for score, label in zip(scores, labels, strict=True))
        / len(labels),
        "log_loss": -sum(
            label * math.log(score) + (1 - label) * math.log(1.0 - score)
            for score, label in zip(clipped, labels, strict=True)
        )
        / len(labels),
    }


def _make_metrics(
    *,
    model: ExperimentalScalarModel,
    predictions: Sequence[ExperimentalScalarPrediction],
    config: ExperimentalScalarLearningCurveConfig,
) -> ExperimentalScalarMetrics:
    if not predictions:
        raise ExperimentalScalarLearningCurveError("metrics require predictions")
    split = predictions[0].split
    if any(item.model_id != model.model_id or item.split != split for item in predictions):
        raise ExperimentalScalarLearningCurveError("metric predictions mix models or splits")
    labels = tuple(1 if item.pseudo_target == "same_claim" else 0 for item in predictions)
    scores = tuple(item.pseudo_same_claim_score for item in predictions)
    observed = _metric_values(labels, scores, threshold=config.decision_threshold)
    constant_scores = tuple(config.constant_baseline_probability for _ in labels)
    constant = _metric_values(labels, constant_scores, threshold=config.decision_threshold)
    counts = Counter(item.pseudo_target for item in predictions)
    data: dict[str, object] = {
        "dataset_id": model.dataset_id,
        "model_id": model.model_id,
        "sampling_seed": model.sampling_seed,
        "component_budget": model.component_budget,
        "training_record_count": model.training_record_count,
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
    placeholder = ExperimentalScalarMetrics.model_construct(
        **cast(
            Any,
            {"metric_id": f"experimental-scalar-metric:{'0' * 64}", **data},
        )
    )
    identity_payload = placeholder.model_dump(mode="json", exclude={"metric_id"})
    return ExperimentalScalarMetrics.model_validate(
        {"metric_id": _metric_id(identity_payload), **data}
    )


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )


def _no_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key {key!r}")
        output[key] = value
    return output


def _reject_nonfinite_json(value: str) -> float:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _strict_json(path: Path) -> dict[str, object]:
    regular = _regular_file(path)

    try:
        raw = regular.read_bytes()
        value = json.loads(
            raw,
            object_pairs_hook=_no_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (OSError, ValueError) as exc:
        raise ExperimentalScalarLearningCurveError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExperimentalScalarLearningCurveError(f"expected one JSON object at {path}")
    if raw != canonical_json_bytes(value) + b"\n":
        raise ExperimentalScalarLearningCurveError(f"non-canonical JSON at {path}")
    return value


def _load_jsonl[ModelT: StrictModel](path: Path, model: type[ModelT]) -> tuple[ModelT, ...]:
    output: list[ModelT] = []
    try:
        lines = _regular_file(path).read_bytes().splitlines(keepends=True)
    except OSError as exc:
        raise ExperimentalScalarLearningCurveError(f"cannot read {path}: {exc}") from exc
    for line_number, raw_line in enumerate(lines, start=1):
        try:
            raw = json.loads(
                raw_line,
                object_pairs_hook=_no_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json,
            )
            record = model.model_validate(raw)
        except ValueError as exc:
            raise ExperimentalScalarLearningCurveError(
                f"invalid {model.__name__} at {path}:{line_number}: {exc}"
            ) from exc
        expected = canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
        if raw_line != expected:
            raise ExperimentalScalarLearningCurveError(
                f"non-canonical {model.__name__} at {path}:{line_number}"
            )
        output.append(record)
    return tuple(output)


def _experiment_id(
    *,
    config_hash: str,
    dataset_manifest_sha256: str,
    code_tree_hash: str,
    runtime: ExperimentalScalarRuntime,
    models: Sequence[ExperimentalScalarModel],
) -> str:
    return "experimental-scalar-curve:" + hash_canonical(
        {
            "schema": "experimental_scalar_learning_curve_v1",
            "config_hash": config_hash,
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "code_tree_hash": code_tree_hash,
            "runtime": runtime.model_dump(mode="json"),
            "model_ids": [model.model_id for model in models],
        }
    )


def _family_counts(
    records: Sequence[ExperimentalMachineSupervisionRecord],
) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for split in ("test", "train", "validation"):
        counts = Counter(record.family_id for record in records if record.split == split)
        output[split] = dict(sorted(counts.items()))
    return output


def _prefix_support(
    models: Sequence[ExperimentalScalarModel],
    *,
    config: ExperimentalScalarLearningCurveConfig,
) -> tuple[ExperimentalScalarPrefixSupport, ...]:
    full_budget = config.component_budgets[-1]
    record_set_counts = Counter(
        (model.component_budget, model.training_record_set_sha256) for model in models
    )
    return tuple(
        ExperimentalScalarPrefixSupport(
            sampling_seed=model.sampling_seed,
            sampling_seed_role=model.sampling_seed_role,
            component_budget=model.component_budget,
            component_count=len(model.training_component_ids),
            record_count=model.training_record_count,
            target_counts=model.training_target_counts,
            family_counts=model.training_family_counts,
            training_record_set_sha256=model.training_record_set_sha256,
            is_full_budget=model.component_budget == full_budget,
            duplicates_training_set_across_sampling_seeds=(
                record_set_counts[(model.component_budget, model.training_record_set_sha256)] > 1
            ),
        )
        for model in sorted(models, key=lambda item: (item.component_budget, item.sampling_seed))
    )


def _mean_within_observed_range(values: Sequence[float]) -> float:
    """Return a stable mean clamped only against floating-point roundoff."""

    if not values:
        raise ExperimentalScalarLearningCurveError("descriptive mean requires values")
    lower = min(values)
    upper = max(values)
    return min(upper, max(lower, math.fsum(values) / len(values)))


def _descriptive_aggregates(
    metrics: Sequence[ExperimentalScalarMetrics],
    models: Sequence[ExperimentalScalarModel],
    *,
    config: ExperimentalScalarLearningCurveConfig,
) -> tuple[ExperimentalScalarDescriptiveAggregate, ...]:
    model_by_id = {model.model_id: model for model in models}
    output: list[ExperimentalScalarDescriptiveAggregate] = []
    for budget in config.component_budgets:
        for split in config.diagnostic_splits:
            group = tuple(
                metric
                for metric in metrics
                if metric.component_budget == budget and metric.split == split
            )
            if len(group) != len(config.sampling_seeds):
                raise ExperimentalScalarLearningCurveError(
                    "descriptive aggregate lacks one configured sampling seed"
                )
            auprc = tuple(metric.pseudo_auprc for metric in group)
            balanced = tuple(metric.pseudo_balanced_accuracy for metric in group)
            brier = tuple(metric.pseudo_brier for metric in group)
            output.append(
                ExperimentalScalarDescriptiveAggregate(
                    component_budget=budget,
                    split=split,
                    sampling_seed_count=len(group),
                    unique_training_record_set_count=len(
                        {
                            model_by_id[metric.model_id].training_record_set_sha256
                            for metric in group
                        }
                    ),
                    pseudo_auprc_mean=_mean_within_observed_range(auprc),
                    pseudo_auprc_min=min(auprc),
                    pseudo_auprc_max=max(auprc),
                    pseudo_balanced_accuracy_mean=math.fsum(balanced) / len(balanced),
                    pseudo_brier_mean=math.fsum(brier) / len(brier),
                )
            )
    return tuple(output)


def _summary_markdown(
    summary: ExperimentalScalarSummary,
    metrics: Sequence[ExperimentalScalarMetrics],
) -> bytes:
    lines = [
        "# Experimental scalar pseudo-target learning curve",
        "",
        f"Experiment: `{summary.experiment_id}`",
        f"Dataset: `{summary.dataset_id}`",
        "",
        "> This is a pseudo-target learnability diagnostic, not evidence of theorem-statement",
        "> faithfulness. It is ineligible for model selection, calibration, evaluation, and",
        "> scientific or release claims.",
        "",
        "| Components | Seed | Train records | Split | Pseudo-AUPRC | "
        "Balanced accuracy | Brier | Constant AUPRC |",
        "|---:|---:|---:|---|---:|---:|---:|---:|",
    ]
    for metric in metrics:
        lines.append(
            f"| {metric.component_budget} | {metric.sampling_seed} | "
            f"{metric.training_record_count} | {metric.split} | "
            f"{metric.pseudo_auprc:.6f} | {metric.pseudo_balanced_accuracy:.6f} | "
            f"{metric.pseudo_brier:.6f} | {metric.constant_auprc:.6f} |"
        )
    lines.extend(
        (
            "",
            "## Descriptive aggregates across component-order seeds",
            "",
            "These are descriptive resamples of component order, not independent stochastic",
            "training runs. At the full budget every seed uses the same training records.",
            "",
            "| Components | Split | Seeds | Unique train sets | Mean AUPRC | AUPRC range | "
            "Mean balanced accuracy | Mean Brier |",
            "|---:|---|---:|---:|---:|---:|---:|---:|",
        )
    )
    for aggregate in summary.descriptive_aggregates:
        lines.append(
            f"| {aggregate.component_budget} | {aggregate.split} | "
            f"{aggregate.sampling_seed_count} | "
            f"{aggregate.unique_training_record_set_count} | "
            f"{aggregate.pseudo_auprc_mean:.6f} | "
            f"{aggregate.pseudo_auprc_min:.6f}-{aggregate.pseudo_auprc_max:.6f} | "
            f"{aggregate.pseudo_balanced_accuracy_mean:.6f} | "
            f"{aggregate.pseudo_brier_mean:.6f} |"
        )
    lines.extend(("", "## Prefix support", ""))
    for support in summary.prefix_support:
        families = ", ".join(f"{family}={count}" for family, count in support.family_counts.items())
        targets = ", ".join(f"{target}={count}" for target, count in support.target_counts.items())
        duplicate_note = "; full-budget duplicate across seeds" if support.is_full_budget else ""
        lines.append(
            f"- components={support.component_budget}, seed={support.sampling_seed}: "
            f"records={support.record_count}; targets: {targets}; families: {families}"
            f"{duplicate_note}."
        )
    lines.extend(("", "## Fixed limitations", ""))
    lines.extend(f"- {limitation}." for limitation in summary.limitations)
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _make_summary(
    *,
    experiment_id: str,
    records: Sequence[ExperimentalMachineSupervisionRecord],
    models: Sequence[ExperimentalScalarModel],
    predictions: Sequence[ExperimentalScalarPrediction],
    metrics: Sequence[ExperimentalScalarMetrics],
    config: ExperimentalScalarLearningCurveConfig,
) -> ExperimentalScalarSummary:
    return ExperimentalScalarSummary(
        experiment_id=experiment_id,
        dataset_id=config.expected_dataset_id,
        profile_id=config.profile_id,
        model_count=len(models),
        prediction_count=len(predictions),
        metric_count=len(metrics),
        component_budgets=config.component_budgets,
        sampling_seeds=config.sampling_seeds,
        diagnostic_splits=config.diagnostic_splits,
        family_counts_by_split=_family_counts(records),
        prefix_support=_prefix_support(models, config=config),
        descriptive_aggregates=_descriptive_aggregates(metrics, models, config=config),
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
            raise ExperimentalScalarLearningCurveError(
                f"required path is absent: {current}"
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ExperimentalScalarLearningCurveError(f"path contains a symlink: {current}")
    return absolute


def _regular_file(path: Path) -> Path:
    safe = _reject_symlinks(path, allow_missing=False)
    metadata = os.lstat(safe)
    if not stat.S_ISREG(metadata.st_mode):
        raise ExperimentalScalarLearningCurveError(f"expected regular file: {safe}")
    return safe


def _real_directory(path: Path) -> Path:
    safe = _reject_symlinks(path, allow_missing=False)
    metadata = os.lstat(safe)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ExperimentalScalarLearningCurveError(f"expected directory: {safe}")
    return safe


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validated_repository_root(path: Path) -> Path:
    """Bind provenance to the checkout that contains this executing module."""

    root = _real_directory(path)
    _regular_file(root / "PLAN.md")
    _regular_file(root / "pyproject.toml")
    expected_module = root / "src/leanfaith/models/experimental_scalar_learning_curve.py"
    if Path(__file__).resolve() != expected_module:
        raise ExperimentalScalarLearningCurveError(
            "repository root does not contain the executing LeanFaith module"
        )
    return root


def _verify_payloads(output: Path, payloads: Mapping[str, bytes]) -> bool:
    safe = _real_directory(output)
    if {path.name for path in safe.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalScalarLearningCurveError("existing output file set is not exact")
    for name, payload in sorted(payloads.items()):
        if _regular_file(safe / name).read_bytes() != payload:
            raise ExperimentalScalarLearningCurveError(
                f"existing learning-curve output differs: {safe / name}"
            )
    return True


def _write_or_replay(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    if set(payloads) != _OUTPUT_FILES:
        raise ExperimentalScalarLearningCurveError("output payload set is not exact")
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
        directory_descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return False


def _run_models(
    records: Sequence[ExperimentalMachineSupervisionRecord],
    *,
    config: ExperimentalScalarLearningCurveConfig,
    config_hash: str,
    runtime: ExperimentalScalarRuntime,
) -> tuple[
    tuple[ExperimentalScalarModel, ...],
    tuple[ExperimentalScalarPrediction, ...],
    tuple[ExperimentalScalarMetrics, ...],
]:
    diagnostic = {
        split: tuple(record for record in records if record.split == split)
        for split in config.diagnostic_splits
    }
    if any(not partition for partition in diagnostic.values()):
        raise ExperimentalScalarLearningCurveError("a diagnostic split is empty")
    models: list[ExperimentalScalarModel] = []
    predictions: list[ExperimentalScalarPrediction] = []
    metrics: list[ExperimentalScalarMetrics] = []
    for sampling_seed in config.sampling_seeds:
        prefixes = component_atomic_prefixes(records, config=config, sampling_seed=sampling_seed)
        for budget in config.component_budgets:
            model = fit_experimental_scalar_model(
                prefixes[budget],
                config=config,
                config_hash=config_hash,
                sampling_seed=sampling_seed,
                component_budget=budget,
                runtime=runtime,
            )
            models.append(model)
            for split in config.diagnostic_splits:
                current = tuple(
                    _make_prediction(model=model, record=record, config=config)
                    for record in diagnostic[split]
                )
                predictions.extend(current)
                metrics.append(_make_metrics(model=model, predictions=current, config=config))
    return (
        tuple(sorted(models, key=lambda item: item.model_id)),
        tuple(sorted(predictions, key=lambda item: item.prediction_id)),
        tuple(
            sorted(
                metrics,
                key=lambda item: (item.component_budget, item.sampling_seed, item.split),
            )
        ),
    )


def run_experimental_scalar_learning_curve(
    *,
    repo_root: Path,
    dataset_dir: Path,
    output_dir: Path,
    config: ExperimentalScalarLearningCurveConfig,
    config_hash: str | None = None,
    allow_experimental_machine_supervision: bool,
) -> ExperimentalScalarLearningCurveArtifacts:
    """Fit and freeze the diagnostic curve after an explicit opt-in."""

    if not allow_experimental_machine_supervision:
        raise ExperimentalScalarLearningCurveError(
            "running requires --allow-experimental-machine-supervision"
        )
    repo = _validated_repository_root(repo_root)
    dataset = _real_directory(dataset_dir)
    output = _reject_symlinks(output_dir, allow_missing=True)
    if _paths_overlap(output, dataset) or _paths_overlap(output, repo):
        raise ExperimentalScalarLearningCurveError(
            "output must be disjoint from both repository and dataset directories"
        )
    effective_config_hash = config_hash or hash_canonical(config.model_dump(mode="json"))
    if effective_config_hash != hash_canonical(config.model_dump(mode="json")):
        raise ExperimentalScalarLearningCurveError("config_hash differs from effective config")
    dataset_manifest_path = _regular_file(dataset / "manifest.json")
    if hash_file(dataset_manifest_path) != config.expected_dataset_manifest_sha256:
        raise ExperimentalScalarLearningCurveError("dataset manifest hash differs from config")
    dataset_manifest = verify_experimental_machine_supervision(dataset)
    if dataset_manifest.dataset_id != config.expected_dataset_id:
        raise ExperimentalScalarLearningCurveError("dataset ID differs from config")
    records = load_experimental_machine_supervision(
        dataset,
        allow_experimental_machine_supervision=True,
        purpose="learning_curve",
    )
    code = collect_code_state(repo)
    if code.git_dirty or code.code_tree_hash is None or code.untracked_files:
        raise ExperimentalScalarLearningCurveError(
            "learning-curve freeze requires a clean, fully tracked code tree"
        )
    torch = _require_torch()
    runtime = _runtime(torch, config=config)
    models, predictions, metrics = _run_models(
        records,
        config=config,
        config_hash=effective_config_hash,
        runtime=runtime,
    )
    code_tree_hash = code.code_tree_hash
    assert code_tree_hash is not None
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
        models=models,
        predictions=predictions,
        metrics=metrics,
        config=config,
    )
    non_manifest: dict[str, bytes] = {
        "metrics.jsonl": _canonical_jsonl(metrics),
        "models.jsonl": _canonical_jsonl(models),
        "predictions.jsonl": _canonical_jsonl(predictions),
        "summary.json": canonical_json_bytes(summary.model_dump(mode="json")) + b"\n",
        "summary.md": _summary_markdown(summary, metrics),
    }
    manifest = ExperimentalScalarLearningCurveManifest(
        experiment_id=experiment_id,
        profile_id=config.profile_id,
        dataset_id=config.expected_dataset_id,
        config_hash=effective_config_hash,
        config=config,
        code=code,
        repository_root=str(repo),
        runtime=runtime,
        dataset_manifest=ExperimentalScalarInputBinding(
            path=str(dataset_manifest_path),
            sha256=config.expected_dataset_manifest_sha256,
            byte_count=dataset_manifest_path.stat().st_size,
        ),
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
    post_dataset_manifest = verify_experimental_machine_supervision(dataset)
    if (
        post_dataset_manifest != dataset_manifest
        or hash_file(dataset_manifest_path) != config.expected_dataset_manifest_sha256
        or dataset_manifest_path.stat().st_size != manifest.dataset_manifest.byte_count
    ):
        raise ExperimentalScalarLearningCurveError(
            "bound dataset changed while the learning curve was being built"
        )
    post_code = collect_code_state(repo)
    if post_code != code:
        raise ExperimentalScalarLearningCurveError(
            "repository code state changed while the learning curve was being built"
        )
    replayed = _write_or_replay(output, payloads)
    verify_experimental_scalar_learning_curve(output, dataset_dir=dataset)
    return ExperimentalScalarLearningCurveArtifacts(
        output_dir=output,
        manifest_path=output / "manifest.json",
        experiment_id=experiment_id,
        model_count=len(models),
        prediction_count=len(predictions),
        metric_count=len(metrics),
        replayed=replayed,
    )


def verify_experimental_scalar_learning_curve(
    output_dir: Path,
    *,
    dataset_dir: Path | None = None,
) -> ExperimentalScalarLearningCurveManifest:
    """Refit deterministically and verify every published byte and binding."""

    root = _real_directory(output_dir)
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalScalarLearningCurveError("learning-curve output file set is not exact")
    for name in _OUTPUT_FILES:
        _regular_file(root / name)
    try:
        manifest = ExperimentalScalarLearningCurveManifest.model_validate(
            _strict_json(root / "manifest.json")
        )
        summary = ExperimentalScalarSummary.model_validate(_strict_json(root / "summary.json"))
    except ValueError as exc:
        raise ExperimentalScalarLearningCurveError(
            f"invalid learning-curve metadata: {exc}"
        ) from exc
    for name, expected in manifest.output_sha256.items():
        if hash_file(_regular_file(root / name)) != expected:
            raise ExperimentalScalarLearningCurveError(f"output hash differs: {name}")
    models = _load_jsonl(root / "models.jsonl", ExperimentalScalarModel)
    predictions = _load_jsonl(root / "predictions.jsonl", ExperimentalScalarPrediction)
    metrics = _load_jsonl(root / "metrics.jsonl", ExperimentalScalarMetrics)
    if (len(models), len(predictions), len(metrics)) != (
        manifest.model_count,
        manifest.prediction_count,
        manifest.metric_count,
    ):
        raise ExperimentalScalarLearningCurveError("artifact counts differ from manifest")
    resolved_dataset = (
        Path(manifest.dataset_manifest.path).parent if dataset_dir is None else dataset_dir
    )
    resolved_dataset = _real_directory(resolved_dataset)
    repository_root = _validated_repository_root(Path(manifest.repository_root))
    if _paths_overlap(root, resolved_dataset) or _paths_overlap(root, repository_root):
        raise ExperimentalScalarLearningCurveError(
            "published output overlaps its repository or dataset input"
        )
    current_code = collect_code_state(repository_root)
    if current_code != manifest.code:
        raise ExperimentalScalarLearningCurveError("bound repository code state differs")
    torch = _require_torch()
    current_runtime = _runtime(torch, config=manifest.config)
    if current_runtime != manifest.runtime:
        raise ExperimentalScalarLearningCurveError("bound deterministic runtime differs")
    dataset_manifest_path = _regular_file(resolved_dataset / "manifest.json")
    if (
        hash_file(dataset_manifest_path) != manifest.dataset_manifest.sha256
        or dataset_manifest_path.stat().st_size != manifest.dataset_manifest.byte_count
    ):
        raise ExperimentalScalarLearningCurveError("bound dataset manifest differs")
    source_manifest: ExperimentalMachineSupervisionManifest = (
        verify_experimental_machine_supervision(resolved_dataset)
    )
    if source_manifest.dataset_id != manifest.dataset_id:
        raise ExperimentalScalarLearningCurveError("bound dataset ID differs")
    records = load_experimental_machine_supervision(
        resolved_dataset,
        allow_experimental_machine_supervision=True,
        purpose="learning_curve",
    )
    by_record = {record.record_id: record for record in records}
    if len(by_record) != len(records):
        raise ExperimentalScalarLearningCurveError("bound dataset has duplicate record IDs")
    expected_models, expected_predictions, expected_metrics = _run_models(
        records,
        config=manifest.config,
        config_hash=manifest.config_hash,
        runtime=manifest.runtime,
    )
    if models != expected_models:
        raise ExperimentalScalarLearningCurveError(
            "published models differ from deterministic refit"
        )
    if predictions != expected_predictions:
        raise ExperimentalScalarLearningCurveError(
            "published predictions differ from deterministic replay"
        )
    if metrics != expected_metrics:
        raise ExperimentalScalarLearningCurveError(
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
        raise ExperimentalScalarLearningCurveError("experiment ID differs from refit content")
    expected_summary = _make_summary(
        experiment_id=expected_experiment_id,
        records=records,
        models=expected_models,
        predictions=expected_predictions,
        metrics=expected_metrics,
        config=manifest.config,
    )
    if summary != expected_summary:
        raise ExperimentalScalarLearningCurveError(
            "published summary differs from deterministic replay"
        )
    expected_non_manifest = {
        "metrics.jsonl": _canonical_jsonl(expected_metrics),
        "models.jsonl": _canonical_jsonl(expected_models),
        "predictions.jsonl": _canonical_jsonl(expected_predictions),
        "summary.json": canonical_json_bytes(expected_summary.model_dump(mode="json")) + b"\n",
        "summary.md": _summary_markdown(expected_summary, expected_metrics),
    }
    for name, expected_payload in expected_non_manifest.items():
        if _regular_file(root / name).read_bytes() != expected_payload:
            raise ExperimentalScalarLearningCurveError(
                f"published artifact differs from deterministic replay: {name}"
            )
        if manifest.output_sha256[name] != hashlib.sha256(expected_payload).hexdigest():
            raise ExperimentalScalarLearningCurveError(
                f"manifest output binding differs from deterministic replay: {name}"
            )
    return manifest


__all__ = [
    "ExperimentalScalarDescriptiveAggregate",
    "ExperimentalScalarLearningCurveArtifacts",
    "ExperimentalScalarLearningCurveConfig",
    "ExperimentalScalarLearningCurveError",
    "ExperimentalScalarLearningCurveManifest",
    "ExperimentalScalarMetrics",
    "ExperimentalScalarModel",
    "ExperimentalScalarPrediction",
    "ExperimentalScalarPrefixSupport",
    "component_atomic_prefixes",
    "extract_symmetric_features",
    "feature_names",
    "fit_experimental_scalar_model",
    "load_experimental_scalar_learning_curve_config",
    "run_experimental_scalar_learning_curve",
    "score_experimental_scalar_model",
    "verify_experimental_scalar_learning_curve",
]
