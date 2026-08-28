from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import leanfaith.eval.cli as eval_cli
from leanfaith.config.hashing import hash_file
from leanfaith.eval.cli import _load_dev_strict_predictions, app
from leanfaith.eval.metrics import compute_classification_metrics


@pytest.fixture(autouse=True)
def _frozen_partition_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "golden_partition_v1.json"
    path.write_text(
        json.dumps(
            {
                "canonical_pairs_sha256": "fixture-canonical-sha256",
                "counts": {"dev": {"canonical_pairs": 4}},
                "group_partitions": {f"group-{index}": "dev" for index in range(4)},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_cli, "_FROZEN_PARTITION_MANIFEST", path)


def _write_strict_run(root: Path, *, partition: str = "dev") -> Path:
    strict_run = root / "dev_fixture_deadbeef0000"
    strict_run.mkdir(parents=True)
    rows: list[dict[str, Any]] = []
    for index, (label, probability) in enumerate(
        [(True, 0.9), (True, 0.7), (False, 0.6), (False, 0.1)]
    ):
        rows.append(
            {
                "pair_id": f"pair-{index}",
                "group_key": f"group-{index}",
                "datasets": ["epla_minif2f"],
                "label": label,
                "label_conflict": False,
                "label_provenance": "expert_human",
                "probability": probability,
                "abstained": False,
                "token_length": 32,
            }
        )
    (strict_run / "predictions.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    labels = [bool(row["label"]) for row in rows]
    probabilities = [float(row["probability"]) for row in rows]
    headline = {
        **compute_classification_metrics(labels, probabilities, threshold=0.5),
        "coverage": 1.0,
        "total_count": len(rows),
        "scored_count": len(rows),
        "abstained_count": 0,
        "n_pairs": len(rows),
        "prevalence": sum(labels) / len(labels),
    }
    (strict_run / "metrics.json").write_text(
        json.dumps(
            {
                "partition": partition,
                "threshold": 0.5,
                "track": "strict_zero_shot",
                "breakdowns": {"headline_expert": headline},
                "trivial_baselines": {"always_majority_accuracy": 0.5},
            }
        ),
        encoding="utf-8",
    )
    (strict_run / "evaluate_run_manifest.json").write_text(
        json.dumps(
            {
                "command": "evaluate",
                "partition": partition,
                "threshold": 0.5,
                "pairs": {"sha256": "fixture-canonical-sha256"},
            }
        ),
        encoding="utf-8",
    )
    return strict_run


def test_calibrate_command_writes_hashed_dev_only_artifacts(tmp_path: Path) -> None:
    strict_run = _write_strict_run(tmp_path)
    out = tmp_path / "calibrated"

    result = CliRunner().invoke(
        app,
        [
            "calibrate",
            "--strict-run",
            str(strict_run),
            "--out",
            str(out),
            "--n-boot",
            "20",
        ],
    )

    assert result.exit_code == 0, result.output
    calibration = json.loads((out / "calibration.json").read_text(encoding="utf-8"))
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    manifest = json.loads((out / "calibrate_run_manifest.json").read_text(encoding="utf-8"))
    assert calibration["fit_partition"] == "dev"
    assert calibration["fit_count"] == 4
    assert calibration["gold_calibrated_balanced_accuracy"] == 1.0
    assert metrics["track"] == "gold_calibrated"
    assert metrics["partition"] == "dev"
    assert metrics["comparison_headline_expert"]["strict_zero_shot"]["threshold"] == 0.5
    assert metrics["comparison_headline_expert"]["gold_calibrated"]["balanced_accuracy"] == 1.0
    assert manifest["inputs"]["predictions"]["sha256"] == hash_file(
        strict_run / "predictions.jsonl"
    )
    assert manifest["outputs"]["metrics"]["sha256"] == hash_file(out / "metrics.json")


def test_calibrate_command_rejects_non_dev_directory_before_reading(tmp_path: Path) -> None:
    strict_run = tmp_path / "final_test_fixture_deadbeef0000"
    strict_run.mkdir()

    with pytest.raises(typer.BadParameter, match=r"directories named dev_\*"):
        _load_dev_strict_predictions(strict_run)


def test_calibrate_command_rejects_non_dev_manifest(tmp_path: Path) -> None:
    strict_run = _write_strict_run(tmp_path, partition="final_test")

    result = CliRunner().invoke(app, ["calibrate", "--strict-run", str(strict_run)])

    assert result.exit_code != 0
    assert "calibration is dev-only" in result.output


def test_calibrate_command_refuses_nonempty_output_directory(tmp_path: Path) -> None:
    strict_run = _write_strict_run(tmp_path)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "metrics.json").write_text("do not overwrite", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["calibrate", "--strict-run", str(strict_run), "--out", str(occupied)],
    )

    assert result.exit_code != 0
    assert (occupied / "metrics.json").read_text(encoding="utf-8") == "do not overwrite"
