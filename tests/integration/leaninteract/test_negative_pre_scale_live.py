"""End-to-end persisted LF-018 five-family pre-scale audit."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from leanfaith.cli.negative_pre_scale import run_negative_pre_scale_audit
from leanfaith.config.paths import RepoPaths

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_all_five_negative_families_persist_complete_unlabeled_lineage() -> None:
    paths = RepoPaths.discover()
    nonce = uuid.uuid4().hex
    output = paths.data / "generated" / "deterministic" / "test_lf018" / nonce
    report = paths.root / "reports" / "transformation_audits" / "test_lf018" / f"{nonce}.json"
    artifacts = None
    try:
        artifacts = run_negative_pre_scale_audit(
            paths=paths,
            output_dir=output,
            report_path=report,
        )

        report_data = json.loads(report.read_text(encoding="utf-8"))
        output_manifest = json.loads(artifacts.output_manifest_path.read_text(encoding="utf-8"))
        run_manifest = json.loads(artifacts.run_manifest_path.read_text(encoding="utf-8"))
        case_results = report_data["case_results"]

        assert report_data["mechanical_pass"] is True
        assert report_data["check_results"] == {
            "all_outputs_provisional": True,
            "all_scoped_negative_families_executed": True,
            "attempt_draft_audit_variant_pair_lineage_complete": True,
            "candidate_statements_reelaborated": True,
            "n10_dual_source_ancestry_persisted": True,
            "source_and_candidate_views_lean_backed": True,
            "zero_promotions": True,
            "zero_resolved_semantic_labels": True,
        }
        assert len(case_results) == 5
        assert len(_jsonl(output / "source_theorems.jsonl")) == 6
        assert len(_jsonl(output / "source_representations.jsonl")) == 6
        for partition in (
            "attempts",
            "drafts",
            "candidate_theorems",
            "candidate_representations",
            "audits",
            "variants",
            "pairs",
        ):
            assert len(_jsonl(output / f"{partition}.jsonl")) == 5
        assert _jsonl(output / "failures.jsonl") == []

        n10 = next(item for item in case_results if item["rule_id"] == "n10_nearby_theorem")
        assert len(n10["source_theorem_ids"]) == 2
        assert len(n10["source_representation_ids"]) == 2
        assert len(n10["root_ancestry_ids"]) == 2
        assert output_manifest["code"] == run_manifest["code"]
        assert output_manifest["context_hash"] == report_data["context_record_sha256"]
    finally:
        shutil.rmtree(output, ignore_errors=True)
        report.unlink(missing_ok=True)
        if artifacts is not None:
            shutil.rmtree(artifacts.run_manifest_path.parent, ignore_errors=True)
