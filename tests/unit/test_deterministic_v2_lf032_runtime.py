"""Integration contract for the complete LF-032 experimental E0 profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.transforms.v2_e0_runtime import (
    V2E0ExecutionError,
    build_v2_e0_runtime,
    load_v2_e0_execution_config,
)

_PROFILE = Path("configs/transformations/v2_e0_lf032_experimental.yaml")
_RULE_IDS = (
    "p06_implicit_arguments",
    "p07_coercion_surface",
    "p09_projections",
    "p10_constructors",
    "p11_bounded_quantifiers",
    "p12_proof_arrow_binder",
)


def test_lf032_profile_binds_exact_six_family_slice() -> None:
    loaded = load_v2_e0_execution_config(path=_PROFILE)
    runtime = build_v2_e0_runtime(path=_PROFILE)

    assert loaded.config.profile_id == "deterministic_v2_e0_lf032_experimental"
    assert runtime.rule_ids == _RULE_IDS
    assert loaded.config.resolved_label_count == 0
    assert loaded.config.promoted_item_count == 0
    assert loaded.config.training_eligible is False


def test_original_p11_p12_profile_remains_byte_separate() -> None:
    legacy = build_v2_e0_runtime()
    complete = build_v2_e0_runtime(path=_PROFILE)

    assert legacy.rule_ids == ("p11_bounded_quantifiers", "p12_proof_arrow_binder")
    assert legacy.generation_config_hash != complete.generation_config_hash
    assert legacy.portfolio_hash == complete.portfolio_hash


def test_profile_path_must_remain_inside_repository(tmp_path: Path) -> None:
    escaped = tmp_path / "profile.yaml"
    escaped.write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(V2E0ExecutionError, match="escapes the repository"):
        load_v2_e0_execution_config(path=escaped)
