from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.datasets.experimental_mixed_supervision import (
    ExperimentalHeadlessStatementView,
    ExperimentalMixedSupervisionRecord,
)
from leanfaith.models import experimental_mixed_scalar_learning_curve as curve
from leanfaith.schemas.manifest import CodeState


def _config(*, update_count: int = 5) -> curve.ExperimentalMixedScalarLearningCurveConfig:
    return curve.ExperimentalMixedScalarLearningCurveConfig(
        profile_id="fixture_mixed_scalar",
        expected_dataset_id=f"experimental_mixed_supervision:{'a' * 64}",
        expected_dataset_manifest_sha256="b" * 64,
        representation_views=("headless",),
        operator_tokens=("∀", "∃", "→", "↔", "=", "≠"),
        record_budgets=(2, 4, 6),
        sampling_seeds=(1, 2),
        diagnostic_splits=("validation", "test"),
        learning_rate=0.05,
        weight_decay=0.001,
        update_count=update_count,
    )


def _view(text: str, *, digit: str) -> ExperimentalHeadlessStatementView:
    return ExperimentalHeadlessStatementView.model_construct(
        headless=text,
        context_id="ctx:fixture",
        headless_sha256=digit * 64,
        origin_record_ids=(f"origin:{digit}",),
    )


def _record(
    index: int,
    *,
    split: str,
    target: curve.PseudoTarget,
    component_digit: str,
) -> ExperimentalMixedSupervisionRecord:
    left = f"(n : Nat) : n = {index % 3}"
    right = (
        f"(m : Nat) : m = {index % 3}"
        if target == "same_claim"
        else f"(n : Nat) : n ≠ {(index + 1) % 3}"
    )
    return ExperimentalMixedSupervisionRecord.model_construct(
        record_id=f"experimental_mixed_pair:{index:064x}",
        pseudo_target=target,
        pseudo_target_basis=(
            "deterministic_first_hop_intention" if index % 3 else "codex_single_judge_ab_proxy"
        ),
        split_component_id=f"split-component:{component_digit * 64}",
        split=split,
        source=_view(left, digit=f"{index:x}"[-1]),
        candidate=_view(right, digit=f"{index + 1:x}"[-1]),
    )


def _records() -> tuple[ExperimentalMixedSupervisionRecord, ...]:
    return (
        _record(1, split="train", target="same_claim", component_digit="1"),
        _record(2, split="train", target="not_same_claim", component_digit="1"),
        _record(3, split="train", target="same_claim", component_digit="2"),
        _record(4, split="train", target="not_same_claim", component_digit="3"),
        _record(5, split="train", target="same_claim", component_digit="4"),
        _record(6, split="train", target="not_same_claim", component_digit="4"),
        _record(7, split="validation", target="same_claim", component_digit="5"),
        _record(8, split="validation", target="not_same_claim", component_digit="6"),
        _record(9, split="test", target="same_claim", component_digit="7"),
        _record(10, split="test", target="not_same_claim", component_digit="8"),
    )


def test_pinned_config_binds_exact_frozen_mixed_corpus_and_record_budgets() -> None:
    loaded = curve.load_experimental_mixed_scalar_learning_curve_config(
        Path("configs/models/experimental_mixed_scalar_learning_curve_v2.yaml")
    )

    assert loaded.config.expected_dataset_id == (
        "experimental_mixed_supervision:"
        "f3e41e400587e493904985737325ba683d51e51be88ba718ba790142a26add77"
    )
    assert loaded.config.expected_dataset_manifest_sha256 == (
        "4e4c4fd54ff1419561d904e536c75e632657979b119b9432a8bfdc6e8c361d7f"
    )
    assert loaded.config.record_budgets == (2000, 5000, 9313)
    assert loaded.config.sampling_seeds == (1729, 2718, 3141)
    assert loaded.config.representation_views == ("headless",)
    assert loaded.config.loss_weighting == "equal_ancestry_component_v1"
    assert len(curve.feature_names(loaded.config)) == 25

    with pytest.raises(ValueError, match="headless"):
        curve.ExperimentalMixedScalarLearningCurveConfig.model_validate(
            {
                **_config().model_dump(mode="json"),
                "representation_views": ["headless", "signature_explicit"],
            }
        )


def test_features_are_swap_invariant_and_read_only_headless_text() -> None:
    config = _config()
    record = _records()[0]
    baseline = curve.extract_symmetric_headless_features(record, config=config)
    swapped = record.model_copy(update={"source": record.candidate, "candidate": record.source})
    metadata_changed = record.model_copy(
        update={
            "pseudo_target": "not_same_claim",
            "pseudo_target_basis": "codex_single_judge_ab_proxy",
        }
    )

    assert curve.extract_symmetric_headless_features(swapped, config=config) == baseline
    assert curve.extract_symmetric_headless_features(metadata_changed, config=config) == baseline
    assert len(baseline) == len(curve.feature_names(config))


def test_record_count_prefixes_are_exact_nested_component_atomic_and_train_only() -> None:
    config = _config()
    records = _records()
    first = curve.component_atomic_record_prefixes(records, config=config, sampling_seed=1)
    replay = curve.component_atomic_record_prefixes(records, config=config, sampling_seed=1)
    other_seed = curve.component_atomic_record_prefixes(records, config=config, sampling_seed=2)

    assert first == replay
    assert {budget: len(items) for budget, items in first.items()} == {2: 2, 4: 4, 6: 6}
    assert {item.split for items in first.values() for item in items} == {"train"}
    assert {item.record_id for item in first[2]}.issubset({item.record_id for item in first[4]})
    assert {item.record_id for item in first[4]}.issubset({item.record_id for item in first[6]})
    assert {item.record_id for item in first[6]} == {item.record_id for item in other_seed[6]}
    for items in first.values():
        selected_ids = {item.record_id for item in items}
        for component_id in {item.split_component_id for item in items}:
            full_component = {
                item.record_id
                for item in records
                if item.split == "train" and item.split_component_id == component_id
            }
            assert full_component.issubset(selected_ids)


def test_ancestry_weights_equalize_components_and_reject_holdouts() -> None:
    records = _records()
    train = records[:4]
    weights = curve.ancestry_normalized_loss_weights(train)
    totals: dict[str, float] = {}
    for record, weight in zip(train, weights, strict=True):
        totals[record.split_component_id] = totals.get(record.split_component_id, 0.0) + weight

    assert len({round(value, 12) for value in totals.values()}) == 1
    assert sum(weights) == pytest.approx(len(train))
    with pytest.raises(curve.ExperimentalMixedScalarLearningCurveError, match="validation"):
        curve.ancestry_normalized_loss_weights((*train, records[6]))


def test_prefix_artifact_binds_full_membership_and_proxy_boundary() -> None:
    config = _config()
    selected = curve.component_atomic_record_prefixes(_records(), config=config, sampling_seed=1)[4]
    prefix = curve._make_prefix(
        selected,
        config=config,
        sampling_seed=1,
        record_budget=4,
        full_train_count=6,
    )

    assert prefix.record_count == 4
    assert prefix.record_ids == tuple(sorted(item.record_id for item in selected))
    assert prefix.semantic_prediction is False
    assert prefix.scientific_training_eligible is False
    assert prefix.model_selection_eligible is False
    assert prefix.evaluation_eligible is False
    assert prefix.contains_validation_records is False
    assert prefix.contains_test_records is False


def test_fit_is_deterministic_and_refuses_nonprefix_or_holdout_records() -> None:
    code = """
from leanfaith.config.hashing import hash_canonical
from leanfaith.models import experimental_mixed_scalar_learning_curve as curve
from tests.unit.test_experimental_mixed_scalar_learning_curve import _config, _records

config = _config(update_count=3)
records = _records()
selected = curve.component_atomic_record_prefixes(records, config=config, sampling_seed=1)[4]
prefix = curve._make_prefix(
    selected, config=config, sampling_seed=1, record_budget=4, full_train_count=6
)
config_hash = hash_canonical(config.model_dump(mode="json"))
first = curve.fit_experimental_mixed_scalar_model(
    selected, prefix=prefix, config=config, config_hash=config_hash
)
second = curve.fit_experimental_mixed_scalar_model(
    selected, prefix=prefix, config=config, config_hash=config_hash
)
assert first == second
assert first.training_record_count == 4
assert first.semantic_prediction is False

try:
    curve.fit_experimental_mixed_scalar_model(
        (*selected[:-1], records[6]), prefix=prefix, config=config, config_hash=config_hash
    )
except curve.ExperimentalMixedScalarLearningCurveError:
    pass
else:
    raise AssertionError("holdout-contaminated training unexpectedly succeeded")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_clean_code_boundary_fails_closed() -> None:
    dirty = CodeState(
        git_revision="1" * 40,
        git_dirty=True,
        base_git_commit="1" * 40,
        code_tree_hash="2" * 64,
        tracked_diff_hash="3" * 64,
        untracked_files=(),
    )
    untracked = dirty.model_copy(update={"git_dirty": False, "untracked_files": ("x.py",)})

    with pytest.raises(curve.ExperimentalMixedScalarLearningCurveError, match="clean"):
        curve._verify_clean_code(dirty)
    with pytest.raises(curve.ExperimentalMixedScalarLearningCurveError, match="clean"):
        curve._verify_clean_code(untracked)


def test_immutable_writer_replays_exact_bytes_and_rejects_tampering(tmp_path: Path) -> None:
    payloads = {name: f"{name}\n".encode() for name in curve._OUTPUT_FILES}
    output = tmp_path / "artifact"

    assert curve._write_or_replay(output, payloads) is False
    assert curve._write_or_replay(output, payloads) is True
    (output / "summary.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(curve.ExperimentalMixedScalarLearningCurveError, match="differs"):
        curve._write_or_replay(output, payloads)


def test_cli_requires_explicit_opt_in_and_exposes_verifier(tmp_path: Path) -> None:
    runner = CliRunner()
    rejected = runner.invoke(
        app,
        [
            "run-experimental-mixed-scalar-learning-curve",
            "--dataset-dir",
            str(tmp_path / "missing"),
            "--output-dir",
            str(tmp_path / "output"),
        ],
    )
    verify_help = runner.invoke(
        app,
        ["verify-experimental-mixed-scalar-learning-curve", "--help"],
    )

    assert rejected.exit_code == 1
    assert "requires --allow-experimental-mixed-supervision" in rejected.output
    assert verify_help.exit_code == 0, verify_help.output


def test_model_schema_rejects_tampered_canonical_id() -> None:
    config = _config(update_count=1)
    records = _records()
    selected = curve.component_atomic_record_prefixes(records, config=config, sampling_seed=1)[2]
    prefix = curve._make_prefix(
        selected,
        config=config,
        sampling_seed=1,
        record_budget=2,
        full_train_count=6,
    )
    payload = prefix.model_dump(mode="json")
    payload["record_set_sha256"] = "f" * 64

    with pytest.raises(ValueError, match="record-set hash"):
        curve.ExperimentalMixedScalarPrefix.model_validate(cast(Any, payload))
