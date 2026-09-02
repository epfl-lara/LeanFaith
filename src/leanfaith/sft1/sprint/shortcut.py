"""Shortcut and held-out balanced-accuracy screens for a compacted release.

Three diagnostics guard the 10K release: a candidate-only and a
reference-only classifier must each stay below 0.60 balanced accuracy, and a
mechanism-held-out pair classifier must stay below 0.65, all with 95%
stratified cluster-bootstrap upper bounds.  The classifier is a deliberately
simple hashed bag-of-tokens logistic regression trained with numpy; it is a
screen for surface shortcuts, not a model of interest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import numpy as np

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_'.!?]*|\d+|[^\sA-Za-z0-9_]")
FEATURE_DIM = 1 << 13


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _hash(token: str) -> int:
    value = 2166136261
    for byte in token.encode("utf-8"):
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value % FEATURE_DIM


def featurize(texts: Sequence[str], *, pair_mode: bool = False) -> np.ndarray:
    matrix = np.zeros((len(texts), FEATURE_DIM), dtype=np.float32)
    for row, text in enumerate(texts):
        toks = tokens(text)
        grams = list(toks) + [f"{a} {b}" for a, b in pairwise(toks)]
        for gram in grams:
            matrix[row, _hash(gram)] += 1.0
        matrix[row, _hash(f"__len__{min(len(toks) // 5, 40)}")] += 1.0
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def train_logreg(
    x: np.ndarray, y: np.ndarray, *, epochs: int = 40, lr: float = 0.5, l2: float = 1e-4
) -> np.ndarray:
    weights = np.zeros(x.shape[1] + 1, dtype=np.float32)
    xb = np.hstack([x, np.ones((x.shape[0], 1), dtype=np.float32)])
    positive = y.sum()
    negative = len(y) - positive
    class_weight = np.where(
        y == 1, len(y) / (2 * max(positive, 1)), len(y) / (2 * max(negative, 1))
    )
    rng = np.random.default_rng(0)
    for _ in range(epochs):
        order = rng.permutation(len(y))
        for start in range(0, len(y), 64):
            idx = order[start : start + 64]
            logits = xb[idx] @ weights
            probs = 1.0 / (1.0 + np.exp(-logits))
            gradient = ((probs - y[idx]) * class_weight[idx]) @ xb[idx] / len(idx) + l2 * weights
            weights -= lr * gradient
    return weights


def predict(weights: np.ndarray, x: np.ndarray) -> np.ndarray:
    xb = np.hstack([x, np.ones((x.shape[0], 1), dtype=np.float32)])
    result: np.ndarray = (xb @ weights > 0).astype(np.int8)
    return result


def balanced_accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    positives = y_true == 1
    negatives = y_true == 0
    if positives.sum() == 0 or negatives.sum() == 0:
        return float("nan")
    tpr = float((y_pred[positives] == 1).mean())
    tnr = float((y_pred[negatives] == 0).mean())
    return (tpr + tnr) / 2


@dataclass(frozen=True, slots=True)
class ScreenResult:
    name: str
    balanced_accuracy: float
    upper_bound_95: float
    threshold: float
    folds: int
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "balanced_accuracy": round(self.balanced_accuracy, 4),
            "upper_bound_95": round(self.upper_bound_95, 4),
            "threshold": self.threshold,
            "folds": self.folds,
            "passed": self.passed,
        }


def _cluster_bootstrap_upper(
    y_true: np.ndarray, y_pred: np.ndarray, clusters: np.ndarray, *, samples: int = 400
) -> float:
    rng = np.random.default_rng(1)
    unique = np.unique(clusters)
    by_cluster = {cluster: np.flatnonzero(clusters == cluster) for cluster in unique}
    values = []
    for _ in range(samples):
        chosen = rng.choice(unique, size=len(unique), replace=True)
        idx = np.concatenate([by_cluster[cluster] for cluster in chosen])
        values.append(balanced_accuracy(y_true[idx], y_pred[idx]))
    finite = [value for value in values if value == value]
    return float(np.percentile(finite, 97.5)) if finite else float("nan")


def _held_out_screen(
    name: str,
    features: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray,
    clusters: np.ndarray,
    *,
    threshold: float,
) -> ScreenResult:
    unique_groups = np.unique(groups)
    predictions = np.zeros(len(labels), dtype=np.int8)
    folds = 0
    for group in unique_groups:
        test = groups == group
        train = ~test
        if test.sum() == 0 or train.sum() == 0 or len(np.unique(labels[train])) < 2:
            continue
        weights = train_logreg(features[train], labels[train])
        predictions[test] = predict(weights, features[test])
        folds += 1
    accuracy = balanced_accuracy(labels, predictions)
    upper = _cluster_bootstrap_upper(labels, predictions, clusters)
    passed = bool(accuracy == accuracy and upper == upper and upper < threshold)
    return ScreenResult(name, accuracy, upper, threshold, folds, passed)


def run_screens(records: Sequence[dict[str, object]], *, folds: int = 5) -> dict[str, object]:
    rows = [cast(dict[str, Any], record["row"]) for record in records]
    sidecars = [cast(dict[str, Any], record["sidecar"]) for record in records]
    labels = np.array([1 if row["label"] else 0 for row in rows], dtype=np.int8)
    roots = np.array([str(sidecar["root_name"]) for sidecar in sidecars])
    mechanisms = np.array([str(sidecar["mechanism"]) for sidecar in sidecars])
    root_ids = {root: index for index, root in enumerate(sorted(set(roots)))}
    clusters = np.array([root_ids[root] for root in roots])
    fold_of_root = {root: index % folds for index, root in enumerate(sorted(root_ids))}
    root_folds = np.array([fold_of_root[root] for root in roots])
    candidate_only = featurize([str(row["candidate"]) for row in rows])
    reference_only = featurize([str(row["reference"]) for row in rows])
    pair_features = featurize([f"{row['reference']}\n<SEP>\n{row['candidate']}" for row in rows])
    results = [
        _held_out_screen(
            "candidate_only", candidate_only, labels, root_folds, clusters, threshold=0.60
        ),
        _held_out_screen(
            "reference_only", reference_only, labels, root_folds, clusters, threshold=0.60
        ),
        _held_out_screen(
            "mechanism_held_out", pair_features, labels, mechanisms, clusters, threshold=0.65
        ),
    ]
    return {
        "rows": len(rows),
        "positives": int(labels.sum()),
        "negatives": int(len(labels) - labels.sum()),
        "roots": len(root_ids),
        "mechanisms": sorted(set(mechanisms.tolist())),
        "screens": [result.to_dict() for result in results],
        "passed": all(result.passed for result in results),
    }


def featurize_side_tagged(pairs: Sequence[tuple[str, str]]) -> np.ndarray:
    """Pair features where every token and bigram is tagged with its side."""

    matrix = np.zeros((len(pairs), FEATURE_DIM), dtype=np.float32)
    for row, (reference, candidate) in enumerate(pairs):
        for tag, text in (("R", reference), ("C", candidate)):
            toks = tokens(text)
            grams = [f"{tag}:{t}" for t in toks] + [f"{tag}:{a} {b}" for a, b in pairwise(toks)]
            for gram in grams:
                matrix[row, _hash(gram)] += 1.0
            matrix[row, _hash(f"{tag}:__len__{min(len(toks) // 5, 40)}")] += 1.0
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def run_screens_v2(records: Sequence[dict[str, object]], *, folds: int = 5) -> dict[str, object]:
    """Screens for a stored (orientation-randomized) core view.

    ``candidate_only`` and ``reference_only`` read the stored fields exactly as
    a model would.  The held-out screen uses side-tagged pair features and
    holds out polarity-paired surface families (``core_family``), so each
    fold removes both the positive and the negative half of one family.
    """

    rows = [cast(dict[str, Any], record["row"]) for record in records]
    sidecars = [cast(dict[str, Any], record["sidecar"]) for record in records]
    labels = np.array([1 if row["label"] else 0 for row in rows], dtype=np.int8)
    roots = np.array([str(row["root_id"]) for row in rows])
    families = np.array([str(sidecar.get("core_family", "unassigned")) for sidecar in sidecars])
    root_ids = {root: index for index, root in enumerate(sorted(set(roots)))}
    clusters = np.array([root_ids[root] for root in roots])
    fold_of_root = {root: index % folds for index, root in enumerate(sorted(root_ids))}
    root_folds = np.array([fold_of_root[root] for root in roots])
    candidate_only = featurize([str(row["candidate"]) for row in rows])
    reference_only = featurize([str(row["reference"]) for row in rows])
    pair_features = featurize_side_tagged(
        [(str(row["reference"]), str(row["candidate"])) for row in rows]
    )
    results = [
        _held_out_screen(
            "candidate_only", candidate_only, labels, root_folds, clusters, threshold=0.60
        ),
        _held_out_screen(
            "reference_only", reference_only, labels, root_folds, clusters, threshold=0.60
        ),
        _held_out_screen(
            "family_held_out", pair_features, labels, families, clusters, threshold=0.65
        ),
    ]
    per_family = {}
    for family in sorted(set(families.tolist())):
        mask = families == family
        per_family[family] = {
            "rows": int(mask.sum()),
            "positives": int(labels[mask].sum()),
            "negatives": int(mask.sum() - labels[mask].sum()),
        }
    return {
        "rows": len(rows),
        "positives": int(labels.sum()),
        "negatives": int(len(labels) - labels.sum()),
        "roots": len(root_ids),
        "families": per_family,
        "orientation": {
            "swapped": sum(1 for s in sidecars if s.get("orientation") == "swapped"),
            "original": sum(1 for s in sidecars if s.get("orientation") != "swapped"),
        },
        "feature_mode": "side_tagged_pairs; screens read stored orientation-randomized fields",
        "screens": [result.to_dict() for result in results],
        "passed": all(result.passed for result in results),
    }


# ---------------------------------------------------------------- v3: order-invariant


def record_pair_id(record: Mapping[str, Any]) -> str:
    row = cast(dict[str, Any], record["row"])
    sidecar = cast(dict[str, Any], record["sidecar"])
    return str(row.get("pair_id") or sidecar["pair_id"])


def canonical_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Canonical row order (by pair id) so every computation is order-invariant."""

    ordered = sorted(records, key=record_pair_id)
    ids = [record_pair_id(item) for item in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("pair ids must be unique for canonical ordering")
    return [dict(item) for item in ordered]


def train_logreg_full_batch(
    x: np.ndarray, y: np.ndarray, *, iterations: int = 300, lr: float = 2.0, l2: float = 1e-3
) -> np.ndarray:
    """Class-weighted L2 logistic regression by full-batch gradient descent.

    No sampling and no random state: the result depends only on the (canonically
    ordered) data, so it is invariant to input permutations.
    """

    xb = np.hstack([x.astype(np.float64), np.ones((x.shape[0], 1), dtype=np.float64)])
    target = y.astype(np.float64)
    positive = max(float(target.sum()), 1.0)
    negative = max(float(len(target) - target.sum()), 1.0)
    weight = np.where(target == 1, len(target) / (2 * positive), len(target) / (2 * negative))
    weights = np.zeros(xb.shape[1], dtype=np.float64)
    for _ in range(iterations):
        logits = xb @ weights
        probs = 1.0 / (1.0 + np.exp(-logits))
        gradient = ((probs - target) * weight) @ xb / len(target) + l2 * weights
        weights -= lr * gradient
    return weights


def predict_full(weights: np.ndarray, x: np.ndarray) -> np.ndarray:
    xb = np.hstack([x.astype(np.float64), np.ones((x.shape[0], 1), dtype=np.float64)])
    result: np.ndarray = (xb @ weights > 0).astype(np.int8)
    return result


def stable_fold(key: str, folds: int) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % folds


def family_stratified_root_bootstrap_upper(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    roots: np.ndarray,
    families: np.ndarray,
    *,
    samples: int = 400,
    seed: int = 1,
) -> float:
    """97.5th percentile of balanced accuracy over root resamples drawn within
    each surface family (family root counts preserved)."""

    rng = np.random.default_rng(seed)
    strata: list[list[np.ndarray]] = []
    for family in sorted(set(families.tolist())):
        members = sorted(set(roots[families == family].tolist()))
        strata.append([np.flatnonzero((roots == root) & (families == family)) for root in members])
    values: list[float] = []
    for _ in range(samples):
        parts: list[np.ndarray] = []
        for stratum in strata:
            chosen = rng.integers(0, len(stratum), size=len(stratum))
            parts.extend(stratum[index] for index in chosen)
        idx = np.concatenate(parts)
        values.append(balanced_accuracy(y_true[idx], y_pred[idx]))
    finite = [value for value in values if value == value]
    return float(np.percentile(finite, 97.5)) if finite else float("nan")


def _held_out_predictions(
    features: np.ndarray, labels: np.ndarray, groups: np.ndarray
) -> tuple[np.ndarray, int]:
    predictions = np.zeros(len(labels), dtype=np.int8)
    folds = 0
    for group in sorted(set(groups.tolist())):
        test = groups == group
        train = ~test
        if test.sum() == 0 or train.sum() == 0 or len(set(labels[train].tolist())) < 2:
            continue
        weights = train_logreg_full_batch(features[train], labels[train])
        predictions[test] = predict_full(weights, features[test])
        folds += 1
    return predictions, folds


def _screen_v3(
    name: str,
    features: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray,
    roots: np.ndarray,
    families: np.ndarray,
    *,
    threshold: float,
) -> tuple[ScreenResult, dict[str, float]]:
    predictions, folds = _held_out_predictions(features, labels, groups)
    accuracy = balanced_accuracy(labels, predictions)
    upper = family_stratified_root_bootstrap_upper(labels, predictions, roots, families)
    passed = bool(accuracy == accuracy and upper == upper and upper < threshold)
    per_family: dict[str, float] = {}
    for family in sorted(set(families.tolist())):
        mask = families == family
        per_family[family] = round(balanced_accuracy(labels[mask], predictions[mask]), 4)
    return ScreenResult(name, accuracy, upper, threshold, folds, passed), per_family


def run_screens_v3(records: Sequence[Mapping[str, Any]], *, folds: int = 5) -> dict[str, object]:
    """Order-invariant screens on a stored (orientation-assigned) view.

    Rows are canonically ordered by pair id; classifiers are full-batch and
    deterministic; folds are stable hashes of root ids; the 95% upper bounds
    come from a family-stratified root bootstrap.  Reports global and
    per-family candidate-only and reference-only balanced accuracy.
    """

    ordered = canonical_records(records)
    rows = [cast(dict[str, Any], item["row"]) for item in ordered]
    sidecars = [cast(dict[str, Any], item["sidecar"]) for item in ordered]
    labels = np.array([1 if row["label"] else 0 for row in rows], dtype=np.int8)
    roots = np.array([str(sidecar["root_id"]) for sidecar in sidecars])
    families = np.array([str(sidecar.get("core_family", "unassigned")) for sidecar in sidecars])
    root_folds = np.array([stable_fold(root, folds) for root in roots])
    candidate_only = featurize([str(row["candidate"]) for row in rows])
    reference_only = featurize([str(row["reference"]) for row in rows])
    pair_features = featurize_side_tagged(
        [(str(row["reference"]), str(row["candidate"])) for row in rows]
    )
    cand, cand_family = _screen_v3(
        "candidate_only", candidate_only, labels, root_folds, roots, families, threshold=0.60
    )
    ref, ref_family = _screen_v3(
        "reference_only", reference_only, labels, root_folds, roots, families, threshold=0.60
    )
    fam, fam_family = _screen_v3(
        "family_held_out", pair_features, labels, families, roots, families, threshold=0.65
    )
    family_counts = {
        family: {
            "rows": int((families == family).sum()),
            "positives": int(labels[families == family].sum()),
            "negatives": int((families == family).sum() - labels[families == family].sum()),
            "roots": len(set(roots[families == family].tolist())),
        }
        for family in sorted(set(families.tolist()))
    }
    return {
        "rows": len(rows),
        "positives": int(labels.sum()),
        "negatives": int(len(labels) - labels.sum()),
        "roots": len(set(roots.tolist())),
        "families": family_counts,
        "orientation": {
            "swapped": sum(1 for s in sidecars if s.get("orientation") == "swapped"),
            "original": sum(1 for s in sidecars if s.get("orientation") != "swapped"),
        },
        "method": {
            "ordering": "canonical_by_pair_id",
            "classifier": "full_batch_class_weighted_l2_logistic_regression",
            "folds": "stable_sha256_root_folds",
            "bootstrap": "family_stratified_root_resampling_400",
            "features": "hashed_unigram_bigram; side_tagged_pairs_for_family_held_out",
            "order_invariant": True,
        },
        "screens": [cand.to_dict(), ref.to_dict(), fam.to_dict()],
        "per_family": {
            "candidate_only": cand_family,
            "reference_only": ref_family,
            "family_held_out": fam_family,
        },
        "passed": all(result.passed for result in (cand, ref, fam)),
    }


def permutation_control(
    records: Sequence[Mapping[str, Any]], *, seeds: Sequence[int] = (1, 2)
) -> dict[str, Any]:
    """Deterministic label-permutation control for the v3 screens.

    Labels are permuted by a seeded shuffle over the canonically ordered rows
    (so the control is order-invariant), the same screens are rerun, and the
    actual screens are reported alongside for comparison.
    """

    import random

    ordered = canonical_records(records)
    actual = run_screens_v3(ordered)
    per_seed: dict[str, Any] = {}
    for seed in seeds:
        permuted = [json.loads(json.dumps(item)) for item in ordered]
        labels = [bool(item["row"]["label"]) for item in permuted]
        random.Random(seed).shuffle(labels)
        for item, label in zip(permuted, labels, strict=True):
            item["row"]["label"] = label
            item["sidecar"]["label"] = label
        result = run_screens_v3(permuted)
        per_seed[f"seed_{seed}"] = {
            str(screen["name"]): [screen["balanced_accuracy"], screen["upper_bound_95"]]
            for screen in cast(list[dict[str, Any]], result["screens"])
        }
    return {
        "schema_version": 2,
        "method": "seeded label shuffle over canonically ordered rows; run_screens_v3 rerun",
        "seeds": list(seeds),
        "values_are": "[balanced_accuracy, 95%_upper_bound]",
        "actual": {
            str(screen["name"]): [screen["balanced_accuracy"], screen["upper_bound_95"]]
            for screen in cast(list[dict[str, Any]], actual["screens"])
        },
        "per_seed": per_seed,
        "control_max_upper_bound": max(
            value[1] for seed_values in per_seed.values() for value in seed_values.values()
        )
        if per_seed
        else None,
    }


def load_serialized_view(compacted_dir: Path) -> list[dict[str, Any]]:
    """Exactly the rows and sidecars written to a compacted view's shards."""

    records: list[dict[str, Any]] = []
    for shard_dir in sorted(compacted_dir.glob("shard-*")):
        rows = [
            json.loads(line)
            for line in (shard_dir / "rows.jsonl").read_text("utf-8").splitlines()
            if line
        ]
        sidecars = [
            json.loads(line)
            for line in (shard_dir / "sidecars.jsonl").read_text("utf-8").splitlines()
            if line
        ]
        if len(rows) != len(sidecars):
            raise ValueError(f"row/sidecar count mismatch in {shard_dir}")
        records.extend(
            {"row": row, "sidecar": sidecar} for row, sidecar in zip(rows, sidecars, strict=True)
        )
    return records


def load_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    return records
