"""Fail-closed Gate-4G finalization over immutable LF-019 artifacts."""

from __future__ import annotations

import datetime
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from leanfaith.config.code_bundle import freeze_code_bundle
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas import ArtifactClass, DataStage, OutputManifest, RunManifest
from leanfaith.schemas.manifest import CodeState, run_manifest_path, write_manifest
from leanfaith.transforms.gate4g import (
    GATE_4G_SMOKE_CHECKS,
    Gate4GFinalizationError,
    finalize_gate4g,
)

_NOW = datetime.datetime(2026, 7, 23, 12, 0, 0, tzinfo=datetime.UTC)
_RULES = (
    "p01_alpha",
    "p02_binders",
    "p04_notation_lite",
    "n01_operator",
    "n02_quantifier",
    "n03_drop_hypothesis",
    "n07_literal_bound",
    "n10_nearby_theorem",
)


@dataclass(frozen=True, slots=True)
class _FinalizerFixture:
    paths: RepoPaths
    run_a_report: Path
    run_b_report: Path
    phase_report: Path
    lf019_milestone: Path
    tamper_target: Path


def _write_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    return hash_file(path)


def _init_clean_repo(root: Path) -> None:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "LeanFaith Test"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "leanfaith@example.invalid"],
        cwd=root,
        check=True,
    )
    (root / "PLAN.md").write_text("# Test plan\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=root, check=True)


def _output_manifest(
    *,
    root: Path,
    run_id: str,
    code: CodeState,
    source: str,
    relative_data: str,
    relative_manifest: str,
) -> tuple[Path, str, Path]:
    data_path = root / relative_data
    data_hash = _write_json(
        data_path,
        {
            "artifact_class": "smoke",
            "run_id": run_id,
            "release_eligible": False,
            "model_selection_eligible": False,
            "calibration_eligible": False,
            "scientific_table_eligible": False,
        },
    )
    assert code.code_tree_hash is not None
    manifest = OutputManifest(
        stage=DataStage.GENERATED,
        artifact_class=ArtifactClass.SMOKE,
        run_id=run_id,
        source=source,
        source_revision="a" * 64,
        config_hash="b" * 64,
        record_schema_version=1,
        row_count=1,
        file_checksums={relative_data: data_hash},
        output_partition_checksums={relative_data: data_hash},
        code_tree_hash=code.code_tree_hash,
        code=code,
        created_at=_NOW,
    )
    manifest_path = root / relative_manifest
    return manifest_path, write_manifest(manifest, manifest_path), data_path


def _build_run(
    *,
    paths: RepoPaths,
    run_id: str,
    code: CodeState,
    bundle_path: Path,
    bundle_hash: str,
    semantic_fingerprint: str,
    replay: bool,
    expected_semantic_fingerprint: str | None,
) -> tuple[Path, Path]:
    root = paths.root
    base = f"data/generated/deterministic/lf019_smoke_v1/{run_id}"
    main_manifest, main_hash, tamper_target = _output_manifest(
        root=root,
        run_id=run_id,
        code=code,
        source="lf019_smoke_fixture",
        relative_data=f"{base}/pairs.json",
        relative_manifest=f"{base}/manifest.json",
    )
    evidence_manifest, evidence_hash, evidence_data = _output_manifest(
        root=root,
        run_id=run_id,
        code=code,
        source="lf019_smoke_evidence",
        relative_data=f"data/evidence/lf019_smoke_v1/{run_id}/evidence.json",
        relative_manifest=f"data/evidence/lf019_smoke_v1/{run_id}/manifest.json",
    )
    label_manifest, label_hash, label_data = _output_manifest(
        root=root,
        run_id=run_id,
        code=code,
        source="lf019_smoke_labels",
        relative_data=f"data/labels/provisional/lf019_smoke_v1/{run_id}/labels.json",
        relative_manifest=(f"data/labels/provisional/lf019_smoke_v1/{run_id}/manifest.json"),
    )
    artifact_hashes = {
        str(main_manifest.relative_to(root)): main_hash,
        str(tamper_target.relative_to(root)): hash_file(tamper_target),
        str(evidence_manifest.relative_to(root)): evidence_hash,
        str(evidence_data.relative_to(root)): hash_file(evidence_data),
        str(label_manifest.relative_to(root)): label_hash,
        str(label_data.relative_to(root)): hash_file(label_data),
    }
    catalog_relative = f"{base}/artifact_catalog.json"
    catalog_path = root / catalog_relative
    catalog_hash = _write_json(
        catalog_path,
        {
            "schema_version": 1,
            "artifact_class": "smoke",
            "release_eligible": False,
            "model_selection_eligible": False,
            "calibration_eligible": False,
            "scientific_table_eligible": False,
            "run_id": run_id,
            "artifact_paths": sorted(artifact_hashes),
            "artifact_hashes": dict(sorted(artifact_hashes.items())),
        },
    )
    checks = dict.fromkeys(GATE_4G_SMOKE_CHECKS, True)
    checks["deterministic_semantic_replay_passed"] = replay
    report_relative = f"reports/transformation_audits/lf019_smoke/{run_id}.json"
    report_path = root / report_relative
    report_hash = _write_json(
        report_path,
        {
            "schema_version": 1,
            "artifact_kind": "lf019_smoke_vertical_audit",
            "artifact_class": "smoke",
            "release_eligible": False,
            "model_selection_eligible": False,
            "calibration_eligible": False,
            "scientific_table_eligible": False,
            "mechanical_pass": True,
            "clean_checkout_pass": True,
            "lf019_accepted": replay,
            "gate_4g_closed": replay,
            "gate_4a_closed": False,
            "gate_4b_closed": False,
            "run_id": run_id,
            "registry_hash": "c" * 64,
            "config_hash": "d" * 64,
            "bound_input_hashes": {
                str(bundle_path.relative_to(root)): bundle_hash,
            },
            "context_ids": ["ctx:" + "e" * 64],
            "configured_source_count": 10,
            "accepted_source_count": 10,
            "expected_failure_count": 1,
            "unexpected_failure_count": 0,
            "family_results": [
                {
                    "rule_id": rule,
                    "pair_id": f"pair:{index:064x}",
                }
                for index, rule in enumerate(_RULES, start=1)
            ],
            "generated_pair_count": 8,
            "evidence_count": 8,
            "smoke_label_count": 1,
            "gold_label_count": 0,
            "promoted_item_count": 0,
            "split_component_count": 7,
            "check_results": checks,
            "output_manifest_path": str(main_manifest.relative_to(root)),
            "output_manifest_sha256": main_hash,
            "artifact_catalog_path": catalog_relative,
            "artifact_catalog_sha256": catalog_hash,
        },
    )
    assert code.code_tree_hash is not None
    run_manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.SMOKE,
        command="leanfaith generate-deterministic --run-smoke-vertical-slice",
        argv=("leanfaith", "generate-deterministic", "--run-smoke-vertical-slice"),
        code=code,
        input_hashes={"code_bundle": bundle_hash},
        output_hashes={
            **artifact_hashes,
            report_relative: report_hash,
            str(main_manifest.relative_to(root)): main_hash,
            catalog_relative: catalog_hash,
        },
        execution={
            "release_eligible": False,
            "model_selection_eligible": False,
            "calibration_eligible": False,
            "scientific_table_eligible": False,
            "semantic_fingerprint": semantic_fingerprint,
            "expected_semantic_fingerprint": expected_semantic_fingerprint,
        },
        created_at=_NOW,
    )
    write_manifest(run_manifest, run_manifest_path(paths, run_id))
    return report_path, tamper_target


def _fixture(tmp_path: Path) -> _FinalizerFixture:
    root = tmp_path / "repo"
    _init_clean_repo(root)
    paths = RepoPaths(root=root)
    bundle_path, bundle_hash, code = freeze_code_bundle(
        root,
        root / "artifacts" / "code_bundles",
    )
    assert code.git_dirty is False
    fingerprint = "f" * 64
    run_a_id = "run_20260723T120000Z_00000001"
    run_b_id = "run_20260723T120001Z_00000002"
    run_a, _ = _build_run(
        paths=paths,
        run_id=run_a_id,
        code=code,
        bundle_path=bundle_path,
        bundle_hash=bundle_hash,
        semantic_fingerprint=fingerprint,
        replay=False,
        expected_semantic_fingerprint=None,
    )
    run_b, tamper_target = _build_run(
        paths=paths,
        run_id=run_b_id,
        code=code,
        bundle_path=bundle_path,
        bundle_hash=bundle_hash,
        semantic_fingerprint=fingerprint,
        replay=True,
        expected_semantic_fingerprint=fingerprint,
    )
    bindings = (
        f"{run_a_id}\n{hash_file(run_a)}\n"
        f"{run_b_id}\n{hash_file(run_b)}\n"
        "Gate 4G closes from the bound replay.\n"
        "Gate 4A remains open.\n"
        "Gate 4B remains open.\n"
    )
    phase = root / "reports/milestones/phase_4_transforms.md"
    phase.parent.mkdir(parents=True, exist_ok=True)
    phase.write_text("# Phase 4\n" + bindings, encoding="utf-8")
    milestone = root / "reports/milestones/lf_019_smoke_vertical_slice.md"
    milestone.write_text("# LF-019\n" + bindings, encoding="utf-8")
    return _FinalizerFixture(
        paths=paths,
        run_a_report=run_a,
        run_b_report=run_b,
        phase_report=phase,
        lf019_milestone=milestone,
        tamper_target=tamper_target,
    )


def test_gate4g_finalizer_binds_replay_and_keeps_promotion_gates_open(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    result = finalize_gate4g(
        paths=fixture.paths,
        run_a_report_path=fixture.run_a_report,
        run_b_report_path=fixture.run_b_report,
        phase_report_path=fixture.phase_report,
        lf019_milestone_path=fixture.lf019_milestone,
    )

    assert result.report.gate_4g_closed
    assert not result.report.gate_4a_closed
    assert not result.report.gate_4b_closed
    assert result.report.run_a.semantic_fingerprint == result.report.run_b.semantic_fingerprint
    assert result.report.run_a.code_bundle_sha256 == result.report.run_b.code_bundle_sha256
    gate_path = fixture.paths.root / result.report_path
    assert hash_file(gate_path) == result.report_sha256
    assert (
        finalize_gate4g(
            paths=fixture.paths,
            run_a_report_path=fixture.run_a_report,
            run_b_report_path=fixture.run_b_report,
            phase_report_path=fixture.phase_report,
            lf019_milestone_path=fixture.lf019_milestone,
        ).report_sha256
        == result.report_sha256
    )


def test_gate4g_finalizer_fails_without_writing_on_catalog_tamper(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.tamper_target.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(Gate4GFinalizationError, match="hash mismatch"):
        finalize_gate4g(
            paths=fixture.paths,
            run_a_report_path=fixture.run_a_report,
            run_b_report_path=fixture.run_b_report,
            phase_report_path=fixture.phase_report,
            lf019_milestone_path=fixture.lf019_milestone,
        )

    assert not (fixture.paths.root / "reports/gates/gate_4g.json").exists()


def test_gate4g_finalizer_rejects_stale_milestone_and_open_run_b(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    payload = json.loads(fixture.run_b_report.read_text(encoding="utf-8"))
    payload["gate_4g_closed"] = False
    fixture.run_b_report.write_bytes(canonical_json_bytes(payload) + b"\n")

    with pytest.raises(Gate4GFinalizationError, match="did not accept and close"):
        finalize_gate4g(
            paths=fixture.paths,
            run_a_report_path=fixture.run_a_report,
            run_b_report_path=fixture.run_b_report,
            phase_report_path=fixture.phase_report,
            lf019_milestone_path=fixture.lf019_milestone,
        )

    fixture = _fixture(tmp_path / "second")
    fixture.lf019_milestone.write_text(
        "# stale\nGate 4A remains open.\nGate 4B remains open.\n",
        encoding="utf-8",
    )
    with pytest.raises(Gate4GFinalizationError, match="stale or incomplete"):
        finalize_gate4g(
            paths=fixture.paths,
            run_a_report_path=fixture.run_a_report,
            run_b_report_path=fixture.run_b_report,
            phase_report_path=fixture.phase_report,
            lf019_milestone_path=fixture.lf019_milestone,
        )
