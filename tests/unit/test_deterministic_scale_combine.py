"""Unary plus global-N10 compatibility-manifest tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import hash_canonical
from leanfaith.config.loading import load_config
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas.manifest import collect_code_state
from leanfaith.transforms.scale_combine import (
    DeterministicScaleCombinedArtifacts,
    _LoadedPass,
    _validate_combined_caps,
    _validate_cross_pass_disjointness,
    _validate_family_ownership,
    combine_deterministic_scale_passes,
)
from leanfaith.transforms.scale_materializer import (
    DeterministicScaleConfig,
    DeterministicScaleError,
    DeterministicScaleRunSpec,
    _project_records,
)
from leanfaith.transforms.scale_merge import DeterministicScaleMergedManifest
from tests.unit.test_deterministic_scale_materializer import _accepted_source_shard

_ROOT = Path(__file__).resolve().parents[2]


def _pass(
    tmp_path: Path,
    *,
    role: str,
    projected: dict[str, tuple[object, ...]] | None = None,
    context_id: str | None = None,
) -> _LoadedPass:
    config_name = (
        "deterministic_scale_unary_sharded_v1.yaml"
        if role == "unary"
        else "deterministic_scale_n10_global_v1.yaml"
    )
    config_path = _ROOT / "configs/transformations" / config_name
    config = load_config(config_path, DeterministicScaleConfig).config
    source_id = "thm:" + "a" * 64
    code = collect_code_state(_ROOT)
    spec = DeterministicScaleRunSpec.model_construct(
        theorem_input_path="/inventory/theorems.jsonl",
        theorem_input_sha256="1" * 64,
        representation_input_path="/inventory/representations.jsonl",
        representation_input_sha256="2" * 64,
        source_inventory_manifest_path="/inventory/manifest.json",
        source_inventory_manifest_sha256="3" * 64,
        theorem_upstream_manifest_path="/inventory/theorem-upstream.json",
        theorem_upstream_manifest_sha256="4" * 64,
        representation_upstream_manifest_path="/inventory/repr-upstream.json",
        representation_upstream_manifest_sha256="5" * 64,
        config_path=str(config_path),
        config_hash="6" * 64,
        registry_hash="7" * 64,
        benchmark_manifest_path="/benchmarks/frozen.json",
        benchmark_manifest_sha256="8" * 64,
        context_id=context_id or "ctx:" + "9" * 64,
        context_record_sha256="a" * 64,
        project_dir="/project/mathlib",
        project_revision="b" * 40,
        project_tree_hash="c" * 40,
        code=code,
        source_universe_theorem_ids=(source_id,),
        max_sources=None,
    )
    output = tmp_path / role
    output.mkdir(parents=True)
    manifest_path = output / f"merged_manifest.{'d' * 64}.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    empty_projected = _project_records(())
    manifest = DeterministicScaleMergedManifest.model_construct(
        merged_manifest_hash="d" * 64,
        source_universe_sha256=hash_canonical((source_id,)),
        record_counts={name: len(records) for name, records in empty_projected.items()},
        partition_sha256=dict.fromkeys(empty_projected, "e" * 64),
        merge_replayed_with_lean=True,
    )
    return _LoadedPass(
        role=role,  # type: ignore[arg-type]
        output_dir=output,
        manifest_path=manifest_path,
        manifest=manifest,
        spec=spec,
        config=config,
        projected=projected or empty_projected,
    )


def test_two_pass_combiner_writes_content_addressed_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_combine

    unary = _pass(tmp_path, role="unary")
    n10 = _pass(tmp_path, role="global_n10")

    def fake_load_pass(**kwargs: object) -> _LoadedPass:
        return unary if kwargs["role"] == "unary" else n10

    monkeypatch.setattr(scale_combine, "_load_pass", fake_load_pass)
    artifacts = combine_deterministic_scale_passes(
        paths=RepoPaths(root=_ROOT),
        unary_merged_output_dir=unary.output_dir,
        n10_merged_output_dir=n10.output_dir,
        output_dir=tmp_path / "combined",
    )

    assert artifacts.combined_manifest_hash in artifacts.manifest_path.name
    payload = artifacts.manifest_path.read_text(encoding="utf-8")
    assert '"scientific_pairing_eligible":true' in payload
    assert '"training_eligible":false' in payload
    assert '"n10_nearby_theorem":"global_n10"' in payload


def test_two_pass_combiner_rejects_context_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_combine

    unary = _pass(tmp_path, role="unary")
    n10 = _pass(tmp_path, role="global_n10", context_id="ctx:" + "f" * 64)
    monkeypatch.setattr(
        scale_combine,
        "_load_pass",
        lambda **kwargs: unary if kwargs["role"] == "unary" else n10,
    )

    with pytest.raises(DeterministicScaleError, match="inventory/code/context"):
        combine_deterministic_scale_passes(
            paths=RepoPaths(root=_ROOT),
            unary_merged_output_dir=unary.output_dir,
            n10_merged_output_dir=n10.output_dir,
            output_dir=tmp_path / "combined",
        )


def test_two_pass_combiner_rejects_common_policy_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_combine

    unary = _pass(tmp_path, role="unary")
    n10 = _pass(tmp_path, role="global_n10")
    n10 = _LoadedPass(
        role=n10.role,
        output_dir=n10.output_dir,
        manifest_path=n10.manifest_path,
        manifest=n10.manifest,
        spec=n10.spec,
        config=n10.config.model_copy(update={"base_seed": n10.config.base_seed + 1}),
        projected=n10.projected,
    )
    monkeypatch.setattr(
        scale_combine,
        "_load_pass",
        lambda **kwargs: unary if kwargs["role"] == "unary" else n10,
    )

    with pytest.raises(DeterministicScaleError, match="common execution/admission"):
        combine_deterministic_scale_passes(
            paths=RepoPaths(root=_ROOT),
            unary_merged_output_dir=unary.output_dir,
            n10_merged_output_dir=n10.output_dir,
            output_dir=tmp_path / "combined",
        )


def test_two_pass_validation_rejects_duplicate_ids_and_candidates(
    tmp_path: Path,
) -> None:
    _, _, accepted = _accepted_source_shard()
    projected = _project_records((accepted,))
    unary = _pass(tmp_path, role="unary", projected=projected)
    n10 = _pass(tmp_path, role="global_n10", projected=projected)

    with pytest.raises(DeterministicScaleError, match="inventories overlap"):
        _validate_cross_pass_disjointness(unary, n10)


def test_two_pass_validation_rejects_wrong_n10_family_ownership(
    tmp_path: Path,
) -> None:
    unary = _pass(tmp_path, role="unary")
    n10 = _pass(tmp_path, role="global_n10")
    n10 = _LoadedPass(
        role=n10.role,
        output_dir=n10.output_dir,
        manifest_path=n10.manifest_path,
        manifest=n10.manifest,
        spec=n10.spec,
        config=n10.config.model_copy(update={"active_rule_ids": ("n01_operator",)}),
        projected=n10.projected,
    )

    with pytest.raises(DeterministicScaleError, match="exactly n10_nearby_theorem"):
        _validate_family_ownership(unary, n10)


def test_two_pass_validation_rejects_combined_cap_violation(
    tmp_path: Path,
) -> None:
    _, _, first = _accepted_source_shard()
    _, _, second = _accepted_source_shard(candidate_code="theorem t : True ∧ True := by sorry")
    unary = _pass(tmp_path, role="unary", projected=_project_records((first,)))
    n10 = _pass(tmp_path, role="global_n10", projected=_project_records((second,)))

    with pytest.raises(DeterministicScaleError, match="per-family/root cap"):
        _validate_combined_caps(unary, n10)


def test_two_pass_cli_forwards_exact_merged_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_combine

    seen: dict[str, object] = {}

    def fake_combine(**kwargs: object) -> DeterministicScaleCombinedArtifacts:
        seen.update(kwargs)
        output = tmp_path / "combined"
        return DeterministicScaleCombinedArtifacts(
            output_dir=output,
            manifest_path=output / f"combined_manifest.{'a' * 64}.json",
            manifest_sha256="b" * 64,
            combined_manifest_hash="a" * 64,
        )

    monkeypatch.setattr(
        scale_combine,
        "combine_deterministic_scale_passes",
        fake_combine,
    )
    result = CliRunner().invoke(
        app,
        [
            "combine-deterministic-scale-passes",
            "--root",
            str(tmp_path),
            "--unary-merged-output",
            str(tmp_path / "unary"),
            "--n10-merged-output",
            str(tmp_path / "n10"),
            "--output-dir",
            str(tmp_path / "combined"),
        ],
    )

    assert result.exit_code == 0
    assert seen["unary_merged_output_dir"] == tmp_path / "unary"
    assert seen["n10_merged_output_dir"] == tmp_path / "n10"
