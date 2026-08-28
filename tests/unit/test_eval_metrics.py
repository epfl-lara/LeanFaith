from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import pytest

from leanfaith.eval.metrics import (
    apply_temperature,
    compute_classification_metrics,
    fit_temperature,
    group_bootstrap_ci,
    select_balanced_accuracy_threshold,
)
from leanfaith.models.m0_dual_encoder import _tie_safe_average_precision


@pytest.fixture
def classification_case() -> tuple[list[bool], list[float]]:
    return [True, False, True, False], [0.9, 0.8, 0.4, 0.1]


def test_known_classification_metrics(
    classification_case: tuple[list[bool], list[float]],
) -> None:
    labels, probabilities = classification_case

    metrics = compute_classification_metrics(labels, probabilities, threshold=0.5)

    assert metrics["accuracy"] == pytest.approx(0.5)
    assert metrics["balanced_accuracy"] == pytest.approx(0.5)
    assert metrics["precision"] == pytest.approx(0.5)
    assert metrics["recall"] == pytest.approx(0.5)
    assert metrics["f1"] == pytest.approx(0.5)
    assert metrics["brier"] == pytest.approx(0.255)


def test_auprc_matches_training_helper() -> None:
    labels = [True, False, True, False, True]
    probabilities = [0.8, 0.8, 0.3, 0.1, 0.3]

    metrics = compute_classification_metrics(labels, probabilities, threshold=0.5)

    expected = _tie_safe_average_precision([int(label) for label in labels], probabilities)
    assert metrics["auprc"] == pytest.approx(expected)


def test_ece_uses_fifteen_equal_width_bins_with_two_occupied() -> None:
    labels = [False, False, True, True]
    probabilities = [0.10, 0.12, 0.90, 0.92]

    metrics = compute_classification_metrics(labels, probabilities, threshold=0.5)

    occupied = [entry for entry in metrics["reliability"] if entry["count"]]
    assert len(occupied) == 2
    assert [entry["count"] for entry in occupied] == [2, 2]
    assert occupied[0]["mean_probability"] == pytest.approx(0.11)
    assert occupied[0]["empirical_rate"] == pytest.approx(0.0)
    assert occupied[1]["mean_probability"] == pytest.approx(0.91)
    assert occupied[1]["empirical_rate"] == pytest.approx(1.0)
    assert metrics["ece"] == pytest.approx(0.10)


def _accuracy(labels: Sequence[bool], probabilities: Sequence[float]) -> float:
    predictions = [probability >= 0.5 for probability in probabilities]
    return sum(
        label == prediction for label, prediction in zip(labels, predictions, strict=True)
    ) / len(labels)


def test_group_bootstrap_is_deterministic_given_seed(
    classification_case: tuple[list[bool], list[float]],
) -> None:
    labels, probabilities = classification_case
    group_keys = ["problem-a", "problem-a", "problem-b", "problem-c"]

    first = group_bootstrap_ci(
        labels,
        probabilities,
        group_keys,
        _accuracy,
        n_boot=200,
        seed=1729,
    )
    second = group_bootstrap_ci(
        labels,
        probabilities,
        group_keys,
        _accuracy,
        n_boot=200,
        seed=1729,
    )

    assert first == second
    assert first[0] == pytest.approx(0.5)
    assert 0.0 <= first[1] <= first[0] <= first[2] <= 1.0


def test_apply_temperature_scales_recovered_binary_logits() -> None:
    calibrated = apply_temperature([0.2, 0.5, 0.8], temperature=2.0)

    assert calibrated == pytest.approx([1.0 / 3.0, 0.5, 2.0 / 3.0])


def test_fit_temperature_reduces_nll_for_overconfident_probabilities() -> None:
    labels = [True, False, True, False]
    probabilities = [0.99, 0.90, 0.80, 0.01]

    temperature = fit_temperature(labels, probabilities)
    calibrated = apply_temperature(probabilities, temperature)

    assert temperature > 1.0
    assert (
        compute_classification_metrics(labels, calibrated, 0.5)["nll"]
        < (compute_classification_metrics(labels, probabilities, 0.5)["nll"])
    )


def test_fit_temperature_uses_neutral_value_for_flat_half_probabilities() -> None:
    assert fit_temperature([True, False], [0.5, 0.5]) == 1.0


def test_fit_temperature_reports_numerical_bounds_when_optimum_is_external() -> None:
    assert fit_temperature(
        [True, False], [0.9, 0.1], min_temperature=0.1, max_temperature=10.0
    ) == pytest.approx(0.1)
    assert fit_temperature(
        [True, False], [0.1, 0.9], min_temperature=0.1, max_temperature=10.0
    ) == pytest.approx(10.0)


def test_balanced_accuracy_threshold_is_exhaustive_and_deterministic() -> None:
    labels = [True, True, False, False]
    probabilities = [0.9, 0.7, 0.6, 0.1]

    threshold = select_balanced_accuracy_threshold(labels, probabilities)

    assert threshold == math.nextafter(0.6, 0.7)
    assert compute_classification_metrics(labels, probabilities, threshold)[
        "balanced_accuracy"
    ] == pytest.approx(1.0)


def test_balanced_accuracy_threshold_uses_half_inside_optimal_interval() -> None:
    threshold = select_balanced_accuracy_threshold([False, True], [0.4, 0.6])

    assert threshold == 0.5


@pytest.mark.parametrize(
    "function",
    [fit_temperature, select_balanced_accuracy_threshold],
)
def test_calibration_requires_both_classes(
    function: Callable[[Sequence[bool], Sequence[float]], float],
) -> None:
    with pytest.raises(ValueError, match="each class"):
        function([True, True], [0.2, 0.8])
