"""Focused LF-019 smoke-orchestration contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.cli.smoke_vertical import (
    LF019SmokeArtifacts,
    LF019SmokeConfig,
    LF019SmokeReplayArtifacts,
    _component_assignments,
)
from leanfaith.config.loading import load_config
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas import PairRecord, make_id


def _pair(name: str, groups: tuple[str, ...]) -> PairRecord:
    theorem_a = make_id("thm", {"case": name, "side": "a"})
    theorem_b = make_id("thm", {"case": name, "side": "b"})
    return PairRecord(
        pair_id=make_id("pair", {"case": name}),
        theorem_a_id=theorem_a,
        theorem_b_id=theorem_b,
        pair_source="lf019_smoke",
        split_group_ids=tuple(sorted(groups)),
        split_eligible=True,
        metadata={"artifact_class": "smoke"},
    )


def _artifacts(root: Path, name: str, *, gate_closed: bool) -> LF019SmokeArtifacts:
    return LF019SmokeArtifacts(
        output_dir=root / "data" / name,
        report_path=root / "reports" / f"{name}.json",
        output_manifest_path=root / "data" / name / "manifest.json",
        run_manifest_path=root / "runs" / name / "manifest.json",
        catalog_path=root / "data" / name / "artifact_catalog.json",
        semantic_fingerprint="a" * 64,
        mechanical_pass=True,
        gate_4g_closed=gate_closed,
    )


def test_checked_in_lf019_config_is_closed_smoke_only_inventory() -> None:
    paths = RepoPaths.discover()
    loaded = load_config(
        paths.root / "configs/transformations/lf019_smoke_v1.yaml",
        LF019SmokeConfig,
    )

    assert loaded.config.artifact_class == "smoke"
    assert not loaded.config.release_eligible
    assert not loaded.config.model_selection_eligible
    assert len(loaded.config.inventory_only_statements) == 1
    assert len(loaded.config.expected_failure_statements) == 1
    assert loaded.config.positive_fixture_path.endswith("lf019_positive_fixtures_v1.yaml")
    assert loaded.config.negative_fixture_path.endswith("lf018_pre_scale_v1.yaml")


def test_smoke_split_assigns_connected_groups_atomically() -> None:
    group_a = make_id("anc", {"group": "a"})
    group_b = make_id("anc", {"group": "b"})
    group_c = make_id("anc", {"group": "c"})
    pairs = (
        _pair("a", (group_a,)),
        _pair("bridge", (group_a, group_b)),
        _pair("c", (group_c,)),
    )

    manifest = _component_assignments(pairs, seed=19019)
    by_pair = {assignment.pair_id: assignment for assignment in manifest.assignments}

    assert by_pair[pairs[0].pair_id].component_id == by_pair[pairs[1].pair_id].component_id
    assert by_pair[pairs[0].pair_id].split == by_pair[pairs[1].pair_id].split
    assert by_pair[pairs[2].pair_id].component_id != by_pair[pairs[0].pair_id].component_id
    assert manifest.group_overlap_count == 0
    assert manifest.train_component_count >= 1
    assert manifest.validation_component_count >= 1


def test_smoke_cli_reports_paired_replay_without_claiming_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = LF019SmokeReplayArtifacts(
        run_a=_artifacts(tmp_path, "run_a", gate_closed=False),
        run_b=_artifacts(tmp_path, "run_b", gate_closed=False),
    )
    monkeypatch.setattr(
        "leanfaith.cli.smoke_vertical.run_lf019_smoke_replay",
        lambda **_: replay,
    )

    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--run-smoke-vertical-slice",
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "LF-019 smoke OK" in result.output
    assert "gate_4g_closed=false" in result.output
    assert "gate_4a_closed=false" in result.output
    assert "gate_4b_closed=false" in result.output


def test_code_bundle_option_is_rejected_outside_smoke_mode(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--validate-only",
            "--root",
            str(tmp_path),
            "--code-bundle",
            str(tmp_path / "bundle.tar.gz"),
        ],
    )

    assert result.exit_code == 2
    assert "--code-bundle is supported only" in result.output
