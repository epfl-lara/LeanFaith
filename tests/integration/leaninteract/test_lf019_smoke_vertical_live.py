"""Lean-backed LF-019 integrated smoke slice."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from leanfaith.cli.smoke_vertical import run_lf019_smoke_once
from leanfaith.config.paths import RepoPaths

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_lf019_executes_every_active_family_with_smoke_only_semantics() -> None:
    paths = RepoPaths.discover()
    nonce = uuid.uuid4().hex
    output = paths.data / "generated" / "deterministic" / "test_lf019" / nonce
    report = paths.reports / "transformation_audits" / "test_lf019" / f"{nonce}.json"
    artifacts = None
    try:
        artifacts = run_lf019_smoke_once(
            paths=paths,
            output_dir=output,
            report_path=report,
        )
        payload = json.loads(report.read_text(encoding="utf-8"))

        assert payload["mechanical_pass"] is True
        assert payload["gate_4g_closed"] is False
        assert payload["accepted_source_count"] == 10
        assert payload["generated_pair_count"] == 8
        assert payload["evidence_count"] == 8
        assert payload["smoke_label_count"] == 1
        assert payload["gold_label_count"] == 0
        assert payload["promoted_item_count"] == 0
        assert payload["check_results"]["deterministic_semantic_replay_passed"] is False
        assert all(
            passed
            for name, passed in payload["check_results"].items()
            if name != "deterministic_semantic_replay_passed"
        )
        assert len(_jsonl(output / "pairs.jsonl")) == 8
        assert len(_jsonl(output / "audits.jsonl")) == 8
        failures = _jsonl(output / "failures.jsonl")
        assert len(failures) == 1
        assert failures[0]["expected"] is True
    finally:
        shutil.rmtree(output, ignore_errors=True)
        report.unlink(missing_ok=True)
        if report.parent.exists() and not any(report.parent.iterdir()):
            report.parent.rmdir()
        if artifacts is not None:
            run_id = artifacts.run_manifest_path.parent.name
            shutil.rmtree(artifacts.run_manifest_path.parent, ignore_errors=True)
            shutil.rmtree(
                paths.data / "evidence" / "lf019_smoke_v1" / run_id,
                ignore_errors=True,
            )
            shutil.rmtree(
                paths.data / "labels" / "provisional" / "lf019_smoke_v1" / run_id,
                ignore_errors=True,
            )
            (paths.data / "split_manifests" / f"lf019_smoke_{run_id}.json").unlink(missing_ok=True)
            shutil.rmtree(
                paths.artifacts / "checkpoints" / "smoke" / "lf019" / run_id,
                ignore_errors=True,
            )
            shutil.rmtree(
                paths.artifacts / "predictions" / "smoke" / run_id,
                ignore_errors=True,
            )
