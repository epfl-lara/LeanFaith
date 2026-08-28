"""Plain classification metrics and group-aware uncertainty estimates."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Hashable, Sequence
from itertools import pairwise
from typing import Protocol, TypedDict

from leanfaith.models.m0_dual_encoder import _tie_safe_average_precision

_ECE_BIN_COUNT = 15
_NLL_EPSILON = 1e-15
_LOGIT_EPSILON = 1e-12
_DEFAULT_MIN_TEMPERATURE = 1e-3
_DEFAULT_MAX_TEMPERATURE = 1e3
_DEFAULT_TEMPERATURE_ITERATIONS = 200


class ReliabilityBin(TypedDict):
    bin_index: int
    lower_bound: float
    upper_bound: float
    count: int
    mean_probability: float | None
    empirical_rate: float | None


class ClassificationMetrics(TypedDict):
    accuracy: float
    balanced_accuracy: float
    precision: float
    recall: float
    f1: float
    auprc: float
    roc_auc: float
    brier: float
    nll: float
    ece: float
    reliability: list[ReliabilityBin]


class CoverageAwareMetrics(ClassificationMetrics):
    coverage: float
    total_count: int
    scored_count: int
    abstained_count: int


class PairScoreLike(Protocol):
    @property
    def probability(self) -> float | None: ...

    @property
    def abstained(self) -> bool: ...


MetricFunction = Callable[[Sequence[bool], Sequence[float]], float]


def _validate_inputs(y_true: Sequence[bool], probs: Sequence[float]) -> None:
    if not y_true or len(y_true) != len(probs):
        raise ValueError("labels and probabilities must be non-empty and aligned")
    if any(
        not math.isfinite(probability) or not 0.0 <= probability <= 1.0 for probability in probs
    ):
        raise ValueError("probabilities must be finite values in [0, 1]")


def _require_both_classes(y_true: Sequence[bool]) -> None:
    if not any(y_true) or all(y_true):
        raise ValueError("calibration requires at least one example from each class")


def _logit(probability: float) -> float:
    clipped = min(max(probability, _LOGIT_EPSILON), 1.0 - _LOGIT_EPSILON)
    return math.log(clipped) - math.log1p(-clipped)


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def apply_temperature(probs: Sequence[float], temperature: float) -> list[float]:
    """Apply one positive temperature to binary probabilities via their logits."""

    if not probs:
        raise ValueError("probabilities must be non-empty")
    if any(
        not math.isfinite(probability) or not 0.0 <= probability <= 1.0 for probability in probs
    ):
        raise ValueError("probabilities must be finite values in [0, 1]")
    if not math.isfinite(temperature) or temperature <= 0.0:
        raise ValueError("temperature must be finite and positive")
    return [_sigmoid(_logit(probability) / temperature) for probability in probs]


def fit_temperature(
    y_true: Sequence[bool],
    probs: Sequence[float],
    *,
    min_temperature: float = _DEFAULT_MIN_TEMPERATURE,
    max_temperature: float = _DEFAULT_MAX_TEMPERATURE,
    iterations: int = _DEFAULT_TEMPERATURE_ITERATIONS,
) -> float:
    """Fit a scalar temperature by convex binary NLL minimization.

    The inverse temperature is the coefficient of the recovered binary logit,
    making NLL convex. Its derivative is monotone, so deterministic bisection
    finds the bounded optimum without adding a SciPy dependency.
    """

    _validate_inputs(y_true, probs)
    _require_both_classes(y_true)
    if (
        not math.isfinite(min_temperature)
        or not math.isfinite(max_temperature)
        or min_temperature <= 0.0
        or max_temperature <= min_temperature
    ):
        raise ValueError("temperature bounds must be finite, positive, and increasing")
    if iterations <= 0:
        raise ValueError("iterations must be positive")

    logits = [_logit(probability) for probability in probs]
    if all(logit == 0.0 for logit in logits):
        return min(max(1.0, min_temperature), max_temperature)

    def derivative(inverse_temperature: float) -> float:
        return sum(
            logit * (_sigmoid(inverse_temperature * logit) - float(label))
            for label, logit in zip(y_true, logits, strict=True)
        ) / len(logits)

    lower = 1.0 / max_temperature
    upper = 1.0 / min_temperature
    if derivative(lower) >= 0.0:
        inverse_temperature = lower
    elif derivative(upper) <= 0.0:
        inverse_temperature = upper
    else:
        for _ in range(iterations):
            midpoint = (lower + upper) / 2.0
            if derivative(midpoint) < 0.0:
                lower = midpoint
            else:
                upper = midpoint
        inverse_temperature = (lower + upper) / 2.0
    return 1.0 / inverse_temperature


def select_balanced_accuracy_threshold(y_true: Sequence[bool], probs: Sequence[float]) -> float:
    """Select a decision-interval threshold that maximizes balanced accuracy.

    Each distinct classification pattern contributes the point in its valid
    threshold interval closest to 0.5. Remaining ties choose the lower value.
    """

    _validate_inputs(y_true, probs)
    _require_both_classes(y_true)
    positive_count = sum(y_true)
    negative_count = len(y_true) - positive_count

    def balanced_accuracy(threshold: float) -> float:
        true_positive = sum(
            label and probability >= threshold
            for label, probability in zip(y_true, probs, strict=True)
        )
        true_negative = sum(
            not label and probability < threshold
            for label, probability in zip(y_true, probs, strict=True)
        )
        return 0.5 * (true_positive / positive_count + true_negative / negative_count)

    unique_probabilities = sorted(set(probs))
    candidates = [min(0.5, unique_probabilities[0])]
    for lower, upper in pairwise(unique_probabilities):
        if lower < 0.5 <= upper:
            candidates.append(0.5)
        elif upper < 0.5:
            candidates.append(upper)
        else:
            candidates.append(math.nextafter(lower, upper))
    maximum = unique_probabilities[-1]
    if maximum < 1.0:
        candidates.append(0.5 if maximum < 0.5 else math.nextafter(maximum, 1.0))
    return min(
        candidates,
        key=lambda threshold: (
            -balanced_accuracy(threshold),
            abs(threshold - 0.5),
            threshold,
        ),
    )


def _tie_aware_roc_auc(y_true: Sequence[bool], probs: Sequence[float]) -> float:
    positive_count = sum(y_true)
    negative_count = len(y_true) - positive_count
    if positive_count == 0 or negative_count == 0:
        return math.nan
    ordered = sorted(enumerate(probs), key=lambda item: item[1])
    rank_sum = 0.0
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        average_rank = ((start + 1) + end) / 2.0
        rank_sum += average_rank * sum(y_true[index] for index, _ in ordered[start:end])
        start = end
    return (rank_sum - positive_count * (positive_count + 1) / 2.0) / (
        positive_count * negative_count
    )


def _reliability_table(
    y_true: Sequence[bool], probs: Sequence[float]
) -> tuple[float, list[ReliabilityBin]]:
    counts = [0] * _ECE_BIN_COUNT
    probability_sums = [0.0] * _ECE_BIN_COUNT
    positive_counts = [0] * _ECE_BIN_COUNT
    for label, probability in zip(y_true, probs, strict=True):
        bin_index = min(int(probability * _ECE_BIN_COUNT), _ECE_BIN_COUNT - 1)
        counts[bin_index] += 1
        probability_sums[bin_index] += probability
        positive_counts[bin_index] += int(label)
    ece = 0.0
    reliability: list[ReliabilityBin] = []
    for bin_index, count in enumerate(counts):
        mean_probability = probability_sums[bin_index] / count if count else None
        empirical_rate = positive_counts[bin_index] / count if count else None
        if mean_probability is not None and empirical_rate is not None:
            ece += count / len(y_true) * abs(mean_probability - empirical_rate)
        reliability.append(
            {
                "bin_index": bin_index,
                "lower_bound": bin_index / _ECE_BIN_COUNT,
                "upper_bound": (bin_index + 1) / _ECE_BIN_COUNT,
                "count": count,
                "mean_probability": mean_probability,
                "empirical_rate": empirical_rate,
            }
        )
    return ece, reliability


def compute_classification_metrics(
    y_true: Sequence[bool], probs: Sequence[float], threshold: float
) -> ClassificationMetrics:
    """Compute binary discrimination and calibration metrics without sklearn."""

    _validate_inputs(y_true, probs)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be a finite value in [0, 1]")
    predictions = [probability >= threshold for probability in probs]
    true_positive = sum(
        label and prediction for label, prediction in zip(y_true, predictions, strict=True)
    )
    false_positive = sum(
        not label and prediction for label, prediction in zip(y_true, predictions, strict=True)
    )
    true_negative = sum(
        not label and not prediction for label, prediction in zip(y_true, predictions, strict=True)
    )
    false_negative = sum(
        label and not prediction for label, prediction in zip(y_true, predictions, strict=True)
    )
    positive_count = true_positive + false_negative
    negative_count = true_negative + false_positive
    precision = true_positive / (true_positive + false_positive) if predictions.count(True) else 0.0
    recall = true_positive / positive_count if positive_count else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    class_recalls = []
    if positive_count:
        class_recalls.append(recall)
    if negative_count:
        class_recalls.append(true_negative / negative_count)
    balanced_accuracy = sum(class_recalls) / len(class_recalls)
    auprc = (
        _tie_safe_average_precision([int(label) for label in y_true], probs)
        if positive_count
        else 0.0
    )
    brier = sum(
        (probability - float(label)) ** 2 for label, probability in zip(y_true, probs, strict=True)
    ) / len(y_true)
    nll = -sum(
        float(label) * math.log(min(max(probability, _NLL_EPSILON), 1.0 - _NLL_EPSILON))
        + (1.0 - float(label))
        * math.log(1.0 - min(max(probability, _NLL_EPSILON), 1.0 - _NLL_EPSILON))
        for label, probability in zip(y_true, probs, strict=True)
    ) / len(y_true)
    ece, reliability = _reliability_table(y_true, probs)
    return {
        "accuracy": (true_positive + true_negative) / len(y_true),
        "balanced_accuracy": balanced_accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "auprc": auprc,
        "roc_auc": _tie_aware_roc_auc(y_true, probs),
        "brier": brier,
        "nll": nll,
        "ece": ece,
        "reliability": reliability,
    }


def coverage_aware_summary(
    scores: Sequence[PairScoreLike], y_true: Sequence[bool], threshold: float
) -> CoverageAwareMetrics:
    """Compute metrics on non-abstentions and report the retained coverage."""

    if not scores or len(scores) != len(y_true):
        raise ValueError("scores and labels must be non-empty and aligned")
    retained_labels: list[bool] = []
    retained_probs: list[float] = []
    for score, label in zip(scores, y_true, strict=True):
        if score.abstained != (score.probability is None):
            raise ValueError("abstention flag and probability disagree")
        if score.probability is not None:
            retained_labels.append(label)
            retained_probs.append(score.probability)
    if not retained_labels:
        raise ValueError("classification metrics are undefined at zero coverage")
    metrics = compute_classification_metrics(retained_labels, retained_probs, threshold)
    scored_count = len(retained_labels)
    summary: CoverageAwareMetrics = {
        **metrics,
        "coverage": scored_count / len(scores),
        "total_count": len(scores),
        "scored_count": scored_count,
        "abstained_count": len(scores) - scored_count,
    }
    return summary


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def group_bootstrap_ci(
    y_true: Sequence[bool],
    probs: Sequence[float],
    group_keys: Sequence[Hashable],
    metric_fn: MetricFunction,
    n_boot: int = 1000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Return a deterministic percentile CI from group-level resampling."""

    _validate_inputs(y_true, probs)
    if len(group_keys) != len(y_true):
        raise ValueError("group keys must align with labels")
    if n_boot <= 0:
        raise ValueError("n_boot must be positive")
    grouped_indices: dict[Hashable, list[int]] = {}
    for index, key in enumerate(group_keys):
        grouped_indices.setdefault(key, []).append(index)
    groups = list(grouped_indices)
    rng = random.Random(seed)
    point = float(metric_fn(y_true, probs))
    if not math.isfinite(point):
        raise ValueError("metric_fn returned a non-finite point estimate")
    estimates: list[float] = []
    for _ in range(n_boot):
        sampled_indices: list[int] = []
        for _ in groups:
            sampled_group = groups[rng.randrange(len(groups))]
            sampled_indices.extend(grouped_indices[sampled_group])
        estimate = float(
            metric_fn(
                [y_true[index] for index in sampled_indices],
                [probs[index] for index in sampled_indices],
            )
        )
        if math.isfinite(estimate):
            estimates.append(estimate)
    if not estimates:
        raise ValueError("metric_fn returned no finite bootstrap estimates")
    estimates.sort()
    return point, _quantile(estimates, 0.025), _quantile(estimates, 0.975)


__all__ = [
    "ClassificationMetrics",
    "CoverageAwareMetrics",
    "MetricFunction",
    "PairScoreLike",
    "ReliabilityBin",
    "apply_temperature",
    "compute_classification_metrics",
    "coverage_aware_summary",
    "fit_temperature",
    "group_bootstrap_ci",
    "select_balanced_accuracy_threshold",
]
