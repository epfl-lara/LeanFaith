"""Plumbing-only classifier artifact for the LF-019 smoke slice.

The smoke model proves serialization and inference wiring; it is not a
semantic model.  Training accepts only explicit provisional alpha-certificate
labels, predictions are the fixed uninformative probability ``0.5``, and every
decision is ``REVIEW``.  The release guard therefore has both type-level and
runtime evidence that these artifacts cannot enter scientific use.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.schemas import (
    ArtifactClass,
    Decision,
    PairRecord,
    PredictionRecord,
    QualityTier,
    RelationLabel,
    ResolvedLabel,
    SemanticLabelTargetKind,
    check_label_target_link,
)
from leanfaith.schemas.ids import HEX64_PATTERN

SMOKE_ALPHA_RESOLUTION_METHOD: Literal["smoke_alpha_certificate"] = "smoke_alpha_certificate"
TINY_SMOKE_MODEL_KIND: Literal["tiny_smoke_constant_v1"] = "tiny_smoke_constant_v1"
_CONSTANT_PROBABILITY = 0.5


class SmokeArtifactBoundary(StrictModel):
    """Eligibility fields shared by every LF-019 artifact."""

    artifact_class: Literal[ArtifactClass.SMOKE] = ArtifactClass.SMOKE
    release_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    scientific_table_eligible: Literal[False] = False


class SmokeTrainingExample(SmokeArtifactBoundary):
    """One explicit provisional alpha label and its plumbing feature vector."""

    pair: PairRecord
    label: ResolvedLabel
    features: dict[str, float] = Field(min_length=1)

    @field_validator("features", mode="before")
    @classmethod
    def _raw_features_are_numeric(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError("smoke features must be a mapping")
        for name, feature in value.items():
            if not isinstance(name, str) or not name:
                raise ValueError("smoke feature names must be nonempty strings")
            if isinstance(feature, bool) or not isinstance(feature, int | float):
                raise ValueError(f"smoke feature {name!r} must be numeric, not bool")
        return value

    @field_validator("features")
    @classmethod
    def _features_are_finite(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(feature) for feature in value.values()):
            raise ValueError("smoke features must be finite")
        return value

    @model_validator(mode="after")
    def _label_is_explicit_alpha_only(self) -> SmokeTrainingExample:
        if self.label.target_kind != SemanticLabelTargetKind.LEAN_PAIR:
            raise ValueError("tiny smoke training accepts Lean-pair labels only")
        violations = check_label_target_link(self.label, self.pair)
        if violations:
            raise ValueError("smoke label/pair link is invalid: " + "; ".join(violations))
        if self.label.quality_tier != QualityTier.PROVISIONAL:
            raise ValueError("tiny smoke training accepts quality_tier=provisional only")
        if self.label.resolution_method != SMOKE_ALPHA_RESOLUTION_METHOD:
            raise ValueError(
                "tiny smoke training accepts only resolution_method=smoke_alpha_certificate"
            )
        if self.label.same_claim is not True:
            raise ValueError("smoke alpha certificate requires same_claim=true")
        if not self.label.train_eligibility:
            raise ValueError("smoke alpha label must explicitly set train_eligibility=true")
        if self.label.eval_eligibility:
            raise ValueError("smoke provisional labels cannot be evaluation-eligible")
        return self


class TinySmokeTrainingConfig(StrictModel):
    """Deterministic plumbing configuration; there is no scientific optimizer."""

    seed: int = Field(default=19019, ge=0)
    constant_probability: float = Field(default=_CONSTANT_PROBABILITY, ge=0.0, le=1.0)
    force_review: Literal[True] = True

    @model_validator(mode="after")
    def _constants_are_fixed(self) -> TinySmokeTrainingConfig:
        if self.constant_probability != _CONSTANT_PROBABILITY:
            raise ValueError("LF-019 smoke probability is fixed at 0.5")
        return self


class TinySmokeModelArtifact(SmokeArtifactBoundary):
    """Serialized nonproduction constant classifier emitted by LF-019."""

    schema_version: Literal[1] = 1
    artifact_id: str = Field(pattern=HEX64_PATTERN)
    model_kind: Literal["tiny_smoke_constant_v1"] = TINY_SMOKE_MODEL_KIND
    feature_names: tuple[str, ...] = Field(min_length=1)
    constant_probability: float = Field(default=_CONSTANT_PROBABILITY, ge=0.0, le=1.0)
    training_pair_ids: tuple[str, ...] = Field(min_length=1)
    training_label_ids: tuple[str, ...] = Field(min_length=1)
    training_data_hash: str = Field(pattern=HEX64_PATTERN)
    config: TinySmokeTrainingConfig
    accepted_quality_tier: Literal[QualityTier.PROVISIONAL] = QualityTier.PROVISIONAL
    accepted_resolution_method: Literal["smoke_alpha_certificate"] = SMOKE_ALPHA_RESOLUTION_METHOD
    plumbing_only: Literal[True] = True

    @model_validator(mode="after")
    def _model_is_canonical(self) -> TinySmokeModelArtifact:
        if self.constant_probability != _CONSTANT_PROBABILITY:
            raise ValueError("LF-019 smoke probability is fixed at 0.5")
        if self.feature_names != tuple(sorted(set(self.feature_names))):
            raise ValueError("feature_names must be sorted and unique")
        if self.training_pair_ids != tuple(sorted(set(self.training_pair_ids))):
            raise ValueError("training_pair_ids must be sorted and unique")
        if len(set(self.training_label_ids)) != len(self.training_label_ids):
            raise ValueError("training_label_ids must be unique")
        if len(self.training_pair_ids) != len(self.training_label_ids):
            raise ValueError("training pair and label counts must match")
        expected = _smoke_model_artifact_id(
            feature_names=self.feature_names,
            training_pair_ids=self.training_pair_ids,
            training_label_ids=self.training_label_ids,
            training_data_hash=self.training_data_hash,
            config=self.config,
        )
        if self.artifact_id != expected:
            raise ValueError("smoke model artifact_id does not match canonical content")
        return self


class SmokeMetrics(SmokeArtifactBoundary):
    """Structural/count metrics only; no semantic quality metric is permitted."""

    schema_version: Literal[1] = 1
    model_artifact_id: str = Field(pattern=HEX64_PATTERN)
    prediction_count: int = Field(ge=1)
    schema_valid_count: int = Field(ge=0)
    finite_score_count: int = Field(ge=0)
    prediction_hash: str = Field(pattern=HEX64_PATTERN)
    descriptive_only: Literal[True] = True

    @model_validator(mode="after")
    def _counts_reconcile(self) -> SmokeMetrics:
        if self.schema_valid_count != self.prediction_count:
            raise ValueError("every smoke prediction must pass the canonical schema")
        if self.finite_score_count != self.prediction_count:
            raise ValueError("every smoke prediction must have finite scores")
        return self


class TinySmokeTrainingResult(SmokeArtifactBoundary):
    """In-memory LF-019 model, predictions, and structural metrics."""

    model: TinySmokeModelArtifact
    predictions: tuple[PredictionRecord, ...]
    metrics: SmokeMetrics

    @model_validator(mode="after")
    def _artifacts_are_bound(self) -> TinySmokeTrainingResult:
        if not self.predictions:
            raise ValueError("smoke result requires predictions")
        pair_ids = tuple(prediction.record_id for prediction in self.predictions)
        if pair_ids != self.model.training_pair_ids:
            raise ValueError("smoke predictions must follow canonical training-pair order")
        if any(
            prediction.model_version != self.model.artifact_id for prediction in self.predictions
        ):
            raise ValueError("every smoke prediction must bind the model artifact")
        if self.metrics.model_artifact_id != self.model.artifact_id:
            raise ValueError("smoke metrics must bind the model artifact")
        if self.metrics.prediction_count != len(self.predictions):
            raise ValueError("smoke prediction count does not match predictions")
        expected_hash = hash_canonical(
            [prediction.model_dump(mode="json") for prediction in self.predictions]
        )
        if self.metrics.prediction_hash != expected_hash:
            raise ValueError("smoke metrics prediction_hash does not match predictions")
        return self


def train_tiny_smoke_classifier(
    examples: Sequence[SmokeTrainingExample],
    *,
    config: TinySmokeTrainingConfig | None = None,
) -> TinySmokeTrainingResult:
    """Fit the constant LF-019 plumbing artifact on alpha certificates only."""

    if not examples:
        raise ValueError("tiny smoke training requires at least one alpha example")
    config = config or TinySmokeTrainingConfig()
    canonical_examples = tuple(sorted(examples, key=lambda example: example.pair.pair_id))
    pair_ids = tuple(example.pair.pair_id for example in canonical_examples)
    label_ids = tuple(example.label.label_id for example in canonical_examples)
    if pair_ids != tuple(sorted(set(pair_ids))):
        raise ValueError("tiny smoke training requires unique pair IDs")
    if len(set(label_ids)) != len(label_ids):
        raise ValueError("tiny smoke training requires unique label IDs")

    feature_names = tuple(sorted(canonical_examples[0].features))
    if any(tuple(sorted(example.features)) != feature_names for example in canonical_examples):
        raise ValueError("all tiny smoke examples must expose exactly the same features")

    training_data_hash = hash_canonical(
        [
            {
                "pair_id": example.pair.pair_id,
                "label_id": example.label.label_id,
                "same_claim": example.label.same_claim,
                "quality_tier": example.label.quality_tier.value,
                "resolution_method": example.label.resolution_method,
                "features": {name: example.features[name] for name in feature_names},
                "artifact_class": example.artifact_class.value,
            }
            for example in canonical_examples
        ]
    )
    artifact_id = _smoke_model_artifact_id(
        feature_names=feature_names,
        training_pair_ids=pair_ids,
        training_label_ids=label_ids,
        training_data_hash=training_data_hash,
        config=config,
    )
    model = TinySmokeModelArtifact(
        artifact_id=artifact_id,
        feature_names=feature_names,
        training_pair_ids=pair_ids,
        training_label_ids=label_ids,
        training_data_hash=training_data_hash,
        config=config,
    )
    predictions = tuple(
        predict_tiny_smoke_classifier(
            model,
            pair_id=example.pair.pair_id,
            features=example.features,
        )
        for example in canonical_examples
    )
    metrics = SmokeMetrics(
        model_artifact_id=model.artifact_id,
        prediction_count=len(predictions),
        schema_valid_count=len(predictions),
        finite_score_count=sum(_prediction_scores_are_finite(item) for item in predictions),
        prediction_hash=hash_canonical(
            [prediction.model_dump(mode="json") for prediction in predictions]
        ),
    )
    return TinySmokeTrainingResult(model=model, predictions=predictions, metrics=metrics)


def predict_tiny_smoke_classifier(
    model: TinySmokeModelArtifact,
    *,
    pair_id: str,
    features: Mapping[str, int | float],
) -> PredictionRecord:
    """Validate plumbing features and return the forced-REVIEW prediction."""

    if tuple(sorted(features)) != model.feature_names:
        raise ValueError("prediction feature names do not match the smoke model")
    for name in model.feature_names:
        feature = features[name]
        if isinstance(feature, bool) or not isinstance(feature, int | float):
            raise ValueError(f"smoke feature {name!r} must be numeric, not bool")
        if not math.isfinite(float(feature)):
            raise ValueError(f"smoke feature {name!r} must be finite")
    return PredictionRecord(
        record_id=pair_id,
        method="lf019_tiny_smoke_constant",
        method_version=TINY_SMOKE_MODEL_KIND,
        same_claim_probability=_CONSTANT_PROBABILITY,
        ambiguity_probability=_CONSTANT_PROBABILITY,
        decision=Decision.REVIEW,
        relation_scores={
            RelationLabel.EQUIVALENT.value: _CONSTANT_PROBABILITY,
            RelationLabel.A_STRONGER.value: 0.0,
            RelationLabel.B_STRONGER.value: 0.0,
            RelationLabel.INCOMPARABLE.value: 0.0,
            RelationLabel.UNRELATED.value: 0.0,
            RelationLabel.AMBIGUOUS.value: _CONSTANT_PROBABILITY,
        },
        model_version=model.artifact_id,
        tokenizer_version="not_applicable_smoke",
        representation_version="lf019_smoke_features_v1",
        calibration_version="uncalibrated_smoke",
        elapsed_ms=0,
        config_hash=model.artifact_id,
    )


def _smoke_model_artifact_id(
    *,
    feature_names: tuple[str, ...],
    training_pair_ids: tuple[str, ...],
    training_label_ids: tuple[str, ...],
    training_data_hash: str,
    config: TinySmokeTrainingConfig,
) -> str:
    return hash_canonical(
        {
            "schema_version": 1,
            "artifact_class": ArtifactClass.SMOKE.value,
            "model_kind": TINY_SMOKE_MODEL_KIND,
            "feature_names": feature_names,
            "constant_probability": _CONSTANT_PROBABILITY,
            "training_pair_ids": training_pair_ids,
            "training_label_ids": training_label_ids,
            "training_data_hash": training_data_hash,
            "config": config.model_dump(mode="json"),
            "release_eligible": False,
            "model_selection_eligible": False,
            "calibration_eligible": False,
            "scientific_table_eligible": False,
            "accepted_quality_tier": QualityTier.PROVISIONAL.value,
            "accepted_resolution_method": SMOKE_ALPHA_RESOLUTION_METHOD,
            "plumbing_only": True,
        }
    )


def _prediction_scores_are_finite(prediction: PredictionRecord) -> bool:
    return all(
        math.isfinite(value)
        for value in (
            prediction.same_claim_probability,
            prediction.ambiguity_probability,
            *prediction.relation_scores.values(),
            *prediction.optional_auxiliary_scores.values(),
        )
    )
