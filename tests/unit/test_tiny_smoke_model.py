"""LF-019 constant classifier remains alpha-only, REVIEW-only, and smoke-only."""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from leanfaith.models.smoke import (
    SMOKE_ALPHA_RESOLUTION_METHOD,
    SmokeTrainingExample,
    TinySmokeModelArtifact,
    TinySmokeTrainingConfig,
    predict_tiny_smoke_classifier,
    train_tiny_smoke_classifier,
)
from leanfaith.schemas import (
    Decision,
    FaithfulnessLevels,
    IntendedRelation,
    PairRecord,
    PredictionRecord,
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    make_id,
)
from tests.unit.record_factories import resolved_label


def _example(
    index: int,
    *,
    same_claim: bool = True,
    features: dict[str, float] | None = None,
    **label_overrides: Any,
) -> SmokeTrainingExample:
    theorem_a_id = make_id("thm", {"smoke": index, "side": "a"})
    theorem_b_id = make_id("thm", {"smoke": index, "side": "b"})
    ancestry_id = make_id("anc", {"smoke": index})
    pair_id = make_id("pair", {"smoke": index, "a": theorem_a_id, "b": theorem_b_id})
    label_id = make_id("lbl", {"smoke": index, "pair": pair_id})
    pair = PairRecord(
        pair_id=pair_id,
        theorem_a_id=theorem_a_id,
        theorem_b_id=theorem_b_id,
        pair_source="lf019_smoke_fixture",
        split_group_ids=(ancestry_id,),
        intended_relation=(
            IntendedRelation.EQUIVALENT if same_claim else IntendedRelation.NEAR_MISS
        ),
        resolved_label_id=label_id,
        split_eligible=False,
        metadata={"smoke_case": index},
    )
    payload: dict[str, Any] = {
        "label_id": label_id,
        "target_id": pair_id,
        "same_claim": same_claim,
        "resolution_outcome": (
            ResolutionOutcome.SAME_CLAIM if same_claim else ResolutionOutcome.NOT_SAME_CLAIM
        ),
        "relation": (RelationLabel.EQUIVALENT if same_claim else RelationLabel.INCOMPARABLE),
        "faithfulness_levels": FaithfulnessLevels(
            F0_representation_equivalent=True if same_claim else None,
            F1_same_claim=same_claim,
            F2_truth_equivalent=None,
        ),
        "quality_tier": QualityTier.PROVISIONAL,
        "resolution_method": SMOKE_ALPHA_RESOLUTION_METHOD,
        "requires_adjudication": False,
        "train_eligibility": True,
        "eval_eligibility": False,
        "policy_version": "lf019_smoke_policy_v1",
    }
    payload.update(label_overrides)
    label = resolved_label(**payload)
    return SmokeTrainingExample(
        pair=pair,
        label=label,
        features=features
        or {
            "headless_equal": 1.0 if same_claim else 0.0,
            "token_overlap": 0.9 if same_claim else 0.2,
        },
    )


def test_tiny_smoke_training_is_deterministic_review_only_and_never_eligible() -> None:
    first = _example(1)
    second = _example(2)
    config = TinySmokeTrainingConfig(seed=19)

    forward = train_tiny_smoke_classifier((first, second), config=config)
    reverse = train_tiny_smoke_classifier((second, first), config=config)

    assert forward == reverse
    assert forward.model.artifact_class.value == "smoke"
    assert not forward.model.release_eligible
    assert not forward.model.model_selection_eligible
    assert not forward.model.calibration_eligible
    assert not forward.model.scientific_table_eligible
    assert forward.model.training_pair_ids == tuple(
        sorted((first.pair.pair_id, second.pair.pair_id))
    )
    assert forward.metrics.prediction_count == 2
    assert forward.metrics.schema_valid_count == 2
    assert forward.metrics.finite_score_count == 2
    assert forward.metrics.descriptive_only
    assert all(isinstance(prediction, PredictionRecord) for prediction in forward.predictions)
    assert all(prediction.same_claim_probability == 0.5 for prediction in forward.predictions)
    assert all(prediction.ambiguity_probability == 0.5 for prediction in forward.predictions)
    assert all(prediction.decision == Decision.REVIEW for prediction in forward.predictions)
    assert all(
        set(prediction.relation_scores) == {relation.value for relation in RelationLabel}
        for prediction in forward.predictions
    )


@pytest.mark.parametrize(
    ("quality_tier", "resolution_method", "match"),
    [
        (
            QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
            SMOKE_ALPHA_RESOLUTION_METHOD,
            "quality_tier=provisional",
        ),
        (QualityTier.SILVER_CONSENSUS, "llm_consensus", "quality_tier=provisional"),
        (QualityTier.PROVISIONAL, "p01_alpha_certificate", "smoke_alpha_certificate"),
        (QualityTier.PROVISIONAL, "smoke_provisional_negative", "smoke_alpha_certificate"),
    ],
)
def test_smoke_example_rejects_nonalpha_or_nonprovisional_label(
    quality_tier: QualityTier,
    resolution_method: str,
    match: str,
) -> None:
    with pytest.raises(ValidationError, match=match):
        _example(
            1,
            quality_tier=quality_tier,
            resolution_method=resolution_method,
        )


def test_smoke_example_rejects_provisional_negative() -> None:
    with pytest.raises(ValidationError, match="same_claim=true"):
        _example(1, same_claim=False)


def test_smoke_example_rejects_broken_reverse_label_link() -> None:
    example = _example(1)
    label = example.label.model_copy(
        update={"label_id": make_id("lbl", {"different": True})},
    )
    with pytest.raises(ValidationError, match="link is invalid"):
        SmokeTrainingExample(pair=example.pair, label=label, features=example.features)


def test_smoke_trainer_accepts_one_alpha_but_requires_unique_pairs_and_features() -> None:
    first = _example(1)
    second = _example(2, features={"other": 1.0})

    result = train_tiny_smoke_classifier((first,))
    assert result.metrics.prediction_count == 1
    with pytest.raises(ValueError, match="unique pair"):
        train_tiny_smoke_classifier((first, first))
    with pytest.raises(ValueError, match="same features"):
        train_tiny_smoke_classifier((first, second))


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf, True, "1"])
def test_smoke_features_reject_nonfinite_non_numeric_and_bool(bad_value: object) -> None:
    with pytest.raises(ValidationError, match=r"numeric|finite"):
        _example(1, features={"bad": bad_value})  # type: ignore[dict-item]


def test_smoke_prediction_requires_exact_finite_feature_contract() -> None:
    result = train_tiny_smoke_classifier((_example(1),))
    with pytest.raises(ValueError, match="feature names"):
        predict_tiny_smoke_classifier(
            result.model,
            pair_id=result.predictions[0].record_id,
            features={"different": 0.0},
        )
    with pytest.raises(ValueError, match="finite"):
        predict_tiny_smoke_classifier(
            result.model,
            pair_id=result.predictions[0].record_id,
            features=dict.fromkeys(result.model.feature_names, math.inf),
        )


def test_smoke_model_artifact_id_is_content_bound() -> None:
    model = train_tiny_smoke_classifier((_example(1),)).model
    payload = model.model_dump(mode="python")
    payload["training_data_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="artifact_id"):
        TinySmokeModelArtifact.model_validate(payload)
