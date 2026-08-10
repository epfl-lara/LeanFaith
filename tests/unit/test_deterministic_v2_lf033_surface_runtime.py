"""Exact runtime/profile boundary for capped LF-033 P05/P08 E0 studies."""

from pathlib import Path

from leanfaith.transforms.positives.p05_p08_surface import (
    MAX_VARIANTS_PER_SOURCE_PER_FAMILY,
    P05_POSITIVE_SLOT_CAP,
    P08_POSITIVE_SLOT_CAP,
)
from leanfaith.transforms.v2_e0_runtime import build_v2_e0_runtime

_PROFILE = Path("configs/transformations/v2_e0_lf033_surface_experimental.yaml")


def test_lf033_surface_profile_is_exact_separate_and_capped() -> None:
    runtime = build_v2_e0_runtime(path=_PROFILE)

    assert runtime.loaded.config.profile_id == ("deterministic_v2_e0_lf033_surface_experimental")
    assert runtime.rule_ids == ("p05_resolved_names", "p08_type_ascriptions")
    assert P05_POSITIVE_SLOT_CAP == 0.10
    assert P08_POSITIVE_SLOT_CAP == 0.10
    assert MAX_VARIANTS_PER_SOURCE_PER_FAMILY == 1
    assert runtime.loaded.config.resolved_label_count == 0
    assert runtime.loaded.config.promoted_item_count == 0
    assert runtime.loaded.config.training_eligible is False


def test_lf033_surface_profile_does_not_broaden_lf032() -> None:
    lf032 = build_v2_e0_runtime(path=Path("configs/transformations/v2_e0_lf032_experimental.yaml"))
    lf033 = build_v2_e0_runtime(path=_PROFILE)

    assert set(lf032.rule_ids).isdisjoint(lf033.rule_ids)
