from __future__ import annotations

import concurrent.futures
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.datasets.experimental_machine_supervision import (
    ExperimentalMachineSupervisionRecord,
    ExperimentalStatementView,
)
from leanfaith.models import experimental_scalar_learning_curve as curve


def _config(
    *,
    update_count: int = 10,
    dataset_manifest_sha256: str = "b" * 64,
) -> curve.ExperimentalScalarLearningCurveConfig:
    return curve.ExperimentalScalarLearningCurveConfig(
        profile_id="fixture_scalar_curve",
        expected_dataset_id=f"experimental-machine-supervision:{'a' * 64}",
        expected_dataset_manifest_sha256=dataset_manifest_sha256,
        representation_views=("headless", "signature_explicit"),
        operator_tokens=("∀", "∃", "→", "↔", "=", "≠"),
        component_budgets=(1, 2),
        sampling_seeds=(1, 2),
        diagnostic_splits=("validation", "test"),
        learning_rate=0.05,
        weight_decay=0.001,
        update_count=update_count,
        decision_threshold=0.5,
    )


def _view(suffix: str, statement: str) -> ExperimentalStatementView:
    return ExperimentalStatementView(
        theorem_id=f"thm:{suffix}",
        representation_id=f"repr:{suffix}",
        context_id="ctx:fixture",
        statement_content_hash=(suffix[0] * 64),
        representation_content_hash=(suffix[-1] * 64),
        alpha_identity_fingerprint=(suffix[1] * 64),
        headless=f"theorem t{suffix} {statement}",
        signature_explicit=statement,
    )


def _record(
    suffix: str,
    *,
    target: curve.PseudoTarget,
    split: str,
    component_hex: str,
    left: str,
    right: str,
) -> ExperimentalMachineSupervisionRecord:
    positive = target == "same_claim"
    data: dict[str, object] = {
        "dataset_profile_id": "fixture_dataset",
        "unique_pair_id": f"unique:{suffix}",
        "pair_id": f"pair:{suffix}",
        "exact_pair_key": (suffix[0] * 64),
        "observation_id": f"observation:{suffix}",
        "root_binding_id": f"root:{suffix}",
        "result_id": f"result:{suffix}",
        "result_line_number": 1,
        "family_id": "p_fixture" if positive else "n_fixture",
        "rule_id": "p_fixture" if positive else "n_fixture",
        "evidence_class": "E2" if positive else "D0",
        "pseudo_target": target,
        "intended_relation": "equivalent" if positive else "near_miss",
        "split_group_ids": (f"anc:{component_hex}",),
        "split_component_id": f"split-component:{component_hex * 64}",
        "split": split,
        "source": _view(f"{suffix}1", left),
        "candidate": _view(f"{suffix}2", right),
        "candidate_code_hash": (suffix[-1] * 64),
        "candidate_code_key": (component_hex * 64),
        "alpha_candidate_key": (("f" if component_hex != "f" else "e") * 64),
        "certificate_kind": "fixture_certificate" if positive else None,
        "certificate_sha256": ("c" * 64) if positive else None,
    }
    placeholder = ExperimentalMachineSupervisionRecord.model_construct(
        **cast(
            Any,
            {"record_id": f"experimental-machine-pair:{'0' * 64}", **data},
        )
    )
    payload = placeholder.model_dump(mode="json", exclude={"record_id"})
    return ExperimentalMachineSupervisionRecord.model_validate(
        {
            "record_id": f"experimental-machine-pair:{hash_canonical(payload)}",
            **data,
        }
    )


def _records() -> tuple[ExperimentalMachineSupervisionRecord, ...]:
    return (
        _record(
            "a1",
            target="same_claim",
            split="train",
            component_hex="1",
            left=": ∀ x : Nat, x = x",
            right=": ∀ y : Nat, y = y",
        ),
        _record(
            "b2",
            target="not_same_claim",
            split="train",
            component_hex="1",
            left=": ∀ x : Nat, x = 0",
            right=": ∃ x : Nat, x = 0",
        ),
        _record(
            "c3",
            target="same_claim",
            split="train",
            component_hex="2",
            left=": True ↔ False",
            right=": False ↔ True",
        ),
        _record(
            "d4",
            target="not_same_claim",
            split="train",
            component_hex="2",
            left=": True → False",
            right=": False → True",
        ),
        _record(
            "e5",
            target="same_claim",
            split="validation",
            component_hex="3",
            left=": ∀ x : Nat, x = x",
            right=": ∀ y : Nat, y = y",
        ),
        _record(
            "f6",
            target="not_same_claim",
            split="validation",
            component_hex="4",
            left=": ∀ x : Nat, x = 0",
            right=": ∃ x : Nat, x = 0",
        ),
        _record(
            "97",
            target="same_claim",
            split="test",
            component_hex="5",
            left=": True ↔ False",
            right=": False ↔ True",
        ),
        _record(
            "88",
            target="not_same_claim",
            split="test",
            component_hex="6",
            left=": True → False",
            right=": False → True",
        ),
    )


def test_pinned_config_is_exact_and_has_fifty_symmetric_features() -> None:
    loaded = curve.load_experimental_scalar_learning_curve_config(
        Path("configs/models/experimental_scalar_learning_curve_v1.yaml")
    )

    assert loaded.config.component_budgets == (64, 128, 256, 512, 1024, 1260)
    assert loaded.config.sampling_seeds == (1729, 2718, 3141)
    assert len(curve.feature_names(loaded.config)) == 50
    assert loaded.config.constant_baseline_probability == 0.5
    assert loaded.config.decision_threshold == 0.5
    assert loaded.config.loss_weighting == "equal_ancestry_component_v1"
    assert loaded.config.sampling_seed_role == "component_order_only"

    with pytest.raises(ValueError, match="threshold is fixed"):
        curve.ExperimentalScalarLearningCurveConfig.model_validate(
            {
                **_config().model_dump(mode="json"),
                "decision_threshold": 0.4,
            }
        )


def test_features_are_swap_invariant_and_blind_to_metadata() -> None:
    config = _config()
    record = _records()[0]
    baseline = curve.extract_symmetric_features(record, config=config)
    swapped = record.model_copy(update={"source": record.candidate, "candidate": record.source})
    metadata_changed = record.model_copy(
        update={"family_id": "do_not_read", "rule_id": "do_not_read"}
    )

    assert curve.extract_symmetric_features(swapped, config=config) == baseline
    assert curve.extract_symmetric_features(metadata_changed, config=config) == baseline
    assert len(baseline) == len(curve.feature_names(config))


def test_component_prefixes_are_atomic_nested_and_deterministic() -> None:
    config = _config()
    records = _records()
    first = curve.component_atomic_prefixes(records, config=config, sampling_seed=1)
    replay = curve.component_atomic_prefixes(records, config=config, sampling_seed=1)

    assert first == replay
    assert len(first[1]) == 2
    assert len(first[2]) == 4
    assert {record.split for record in first[2]} == {"train"}
    assert {record.split_component_id for record in first[1]}.issubset(
        {record.split_component_id for record in first[2]}
    )
    assert {record.pseudo_target for record in first[1]} == {
        "same_claim",
        "not_same_claim",
    }


def test_loss_weights_give_every_ancestry_component_equal_total_weight() -> None:
    records = _records()[:3]
    weights = curve._ancestry_normalized_loss_weights(records)
    totals: dict[str, float] = {}
    for record, weight in zip(records, weights, strict=True):
        totals[record.split_component_id] = totals.get(record.split_component_id, 0.0) + weight

    assert len(totals) == 2
    assert len(set(totals.values())) == 1
    assert sum(weights) == pytest.approx(len(records))


def test_fixed_scalar_fit_is_deterministic_finite_and_nonsemantic() -> None:
    # Keep torch inside a child process so this test preserves the repository's
    # import-boundary assertion that optional runtimes are not loaded eagerly.
    code = """
from leanfaith.config.hashing import hash_canonical
from leanfaith.models import experimental_scalar_learning_curve as curve
from tests.unit.test_experimental_scalar_learning_curve import _config, _records

config = _config(update_count=5)
records = _records()
prefix = curve.component_atomic_prefixes(records, config=config, sampling_seed=1)[2]
config_hash = hash_canonical(config.model_dump(mode="json"))
first = curve.fit_experimental_scalar_model(
    prefix,
    config=config,
    config_hash=config_hash,
    sampling_seed=1,
    component_budget=2,
)
second = curve.fit_experimental_scalar_model(
    prefix,
    config=config,
    config_hash=config_hash,
    sampling_seed=1,
    component_budget=2,
)
score = curve.score_experimental_scalar_model(first, records[4], config=config)
assert first == second
assert 0.0 <= score <= 1.0
assert first.semantic_prediction is False
assert first.scientific_training_eligible is False
assert first.model_selection_eligible is False
assert first.calibration_eligible is False
assert first.evaluation_eligible is False
assert first.release_claim_eligible is False
print(first.model_id)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("experimental-scalar-model:")


def test_tie_safe_average_precision_matches_prevalence() -> None:
    assert curve._average_precision((1, 0, 1, 0), (0.5, 0.5, 0.5, 0.5)) == 0.5


def test_immutable_writer_replays_and_rejects_tamper(tmp_path: Path) -> None:
    payloads = {name: f"{name}\n".encode() for name in curve._OUTPUT_FILES}
    output = tmp_path / "curve"

    assert curve._write_or_replay(output, payloads) is False
    assert curve._write_or_replay(output, payloads) is True

    (output / "metrics.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(curve.ExperimentalScalarLearningCurveError, match="differs"):
        curve._write_or_replay(output, payloads)


def test_immutable_writer_is_safe_under_concurrent_identical_publish(tmp_path: Path) -> None:
    payloads = {name: f"{name}\n".encode() for name in curve._OUTPUT_FILES}
    output = tmp_path / "concurrent-curve"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: curve._write_or_replay(output, payloads), range(2)))

    assert sorted(results) == [False, True]
    assert {path.name for path in output.iterdir()} == curve._OUTPUT_FILES


def test_immutable_writer_rejects_symlinked_artifact(tmp_path: Path) -> None:
    payloads = {name: f"{name}\n".encode() for name in curve._OUTPUT_FILES}
    output = tmp_path / "symlink-curve"
    curve._write_or_replay(output, payloads)
    external = tmp_path / "external"
    external.write_bytes(payloads["metrics.jsonl"])
    (output / "metrics.jsonl").unlink()
    (output / "metrics.jsonl").symlink_to(external)

    with pytest.raises(curve.ExperimentalScalarLearningCurveError, match="symlink"):
        curve._write_or_replay(output, payloads)


def test_output_must_be_disjoint_from_repository_and_dataset(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    dataset = tmp_path / "dataset"
    repo.mkdir()
    dataset.mkdir()

    with pytest.raises(curve.ExperimentalScalarLearningCurveError, match="disjoint"):
        curve.run_experimental_scalar_learning_curve(
            repo_root=repo,
            dataset_dir=dataset,
            output_dir=repo / "output",
            config=_config(),
            allow_experimental_machine_supervision=True,
        )


def test_run_rejects_unrelated_repository_root(tmp_path: Path) -> None:
    repo = tmp_path / "unrelated-repo"
    dataset = tmp_path / "dataset"
    output = tmp_path / "output"
    repo.mkdir()
    dataset.mkdir()
    (repo / "PLAN.md").write_text("not LeanFaith\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'other'\n")

    with pytest.raises(
        curve.ExperimentalScalarLearningCurveError,
        match="does not contain the executing LeanFaith module",
    ):
        curve.run_experimental_scalar_learning_curve(
            repo_root=repo,
            dataset_dir=dataset,
            output_dir=output,
            config=_config(),
            allow_experimental_machine_supervision=True,
        )


def _fixture_curve_script(tmp_path: Path, *, verify_only: bool = False) -> str:
    action = (
        "curve.verify_experimental_scalar_learning_curve(output, dataset_dir=dataset)"
        if verify_only
        else """
first = curve.run_experimental_scalar_learning_curve(
    repo_root=repo,
    dataset_dir=dataset,
    output_dir=output,
    config=config,
    allow_experimental_machine_supervision=True,
)
second = curve.run_experimental_scalar_learning_curve(
    repo_root=repo,
    dataset_dir=dataset,
    output_dir=output,
    config=config,
    allow_experimental_machine_supervision=True,
)
assert first.replayed is False
assert second.replayed is True
assert curve.verify_experimental_scalar_learning_curve(output).experiment_id == first.experiment_id
"""
    )
    return f"""
from pathlib import Path
from leanfaith.config.hashing import hash_file
from leanfaith.datasets.experimental_machine_supervision import ExperimentalMachineSupervisionManifest
from leanfaith.models import experimental_scalar_learning_curve as curve
from leanfaith.schemas.manifest import CodeState
from tests.unit.test_experimental_scalar_learning_curve import _config, _records

root = Path({str(tmp_path)!r})
repo = Path.cwd()
dataset = root / "dataset"
output = root / "output"
repo.mkdir(exist_ok=True)
dataset.mkdir(exist_ok=True)
manifest_path = dataset / "manifest.json"
if not manifest_path.exists():
    manifest_path.write_bytes(b"fixture-dataset-manifest\\n")
config = _config(update_count=2, dataset_manifest_sha256=hash_file(manifest_path))
source_manifest = ExperimentalMachineSupervisionManifest.model_construct(
    dataset_id=config.expected_dataset_id,
)
code = CodeState(
    git_revision="1" * 40,
    git_dirty=False,
    base_git_commit="1" * 40,
    code_tree_hash="2" * 64,
    tracked_diff_hash=None,
    untracked_files=(),
)
curve.verify_experimental_machine_supervision = lambda _path: source_manifest
curve.load_experimental_machine_supervision = lambda *_args, **_kwargs: _records()
curve.collect_code_state = lambda _path: code
{action}
print("ok")
"""


def test_end_to_end_run_replay_verify_and_deterministic_refit_tamper(
    tmp_path: Path,
) -> None:
    built = subprocess.run(
        [sys.executable, "-c", _fixture_curve_script(tmp_path)],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    assert built.stdout.strip() == "ok"

    output = tmp_path / "output"
    summary = json.loads((output / "summary.json").read_bytes())
    assert len(summary["prefix_support"]) == 4
    assert all(
        set(item)
        >= {
            "component_count",
            "record_count",
            "target_counts",
            "family_counts",
            "training_record_set_sha256",
        }
        for item in summary["prefix_support"]
    )
    full_budget_support = [item for item in summary["prefix_support"] if item["is_full_budget"]]
    assert len(full_budget_support) == 2
    assert all(
        item["duplicates_training_set_across_sampling_seeds"] for item in full_budget_support
    )
    full_budget_aggregates = [
        item for item in summary["descriptive_aggregates"] if item["component_budget"] == 2
    ]
    assert {item["unique_training_record_set_count"] for item in full_budget_aggregates} == {1}

    model_lines = (output / "models.jsonl").read_bytes().splitlines()
    first_model = json.loads(model_lines[0])
    first_model["weights"][0] += 0.125
    first_model["model_id"] = curve._model_id(
        {key: value for key, value in first_model.items() if key != "model_id"}
    )
    model_lines[0] = canonical_json_bytes(first_model)
    model_payload = b"\n".join(model_lines) + b"\n"
    (output / "models.jsonl").write_bytes(model_payload)
    manifest = json.loads((output / "manifest.json").read_bytes())
    manifest["output_sha256"]["models.jsonl"] = hashlib.sha256(model_payload).hexdigest()
    (output / "manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")

    verified = subprocess.run(
        [sys.executable, "-c", _fixture_curve_script(tmp_path, verify_only=True)],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode != 0
    assert "published models differ from deterministic refit" in verified.stderr


def test_cli_requires_explicit_opt_in_and_exposes_verifier(tmp_path: Path) -> None:
    runner = CliRunner()
    rejected = runner.invoke(
        app,
        [
            "run-experimental-scalar-learning-curve",
            "--root",
            str(Path.cwd()),
            "--dataset-dir",
            str(tmp_path / "missing-dataset"),
            "--output-dir",
            str(tmp_path / "output"),
        ],
    )
    verify_help = runner.invoke(
        app,
        ["verify-experimental-scalar-learning-curve", "--help"],
    )

    assert rejected.exit_code == 1
    assert "requires --allow-experimental-machine-supervision" in rejected.output
    assert verify_help.exit_code == 0, verify_help.output
