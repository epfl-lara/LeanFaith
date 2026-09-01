from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.sft2b.pins import (
    REPR_API_HASH,
    REPR_FREEZE_COMMIT,
    REPR_IMPLEMENTATION_SET_HASH,
    REPR_SPEC_HASH,
    verify_runtime_pins,
)
from leanfaith.sft2b.reuse import load_existing_301

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_HELPER = _REPO_ROOT / "src/leanfaith/sft2b/lean_helper.lean"
_RECIPE = _REPO_ROOT / "configs/sft2b/existing_301_v1.json"
_SMOKE_PAIR = "pair:e899befb44b83b09dd0f82777d48ea44ec3efac642b85650e0040a4f0e2fcf29"
_LOCAL_301_PREREQUISITES = (
    _REPO_ROOT / "data/raw/real_outputs/public_research_v1",
    _REPO_ROOT / "data/raw/real_outputs/gate3_docstrings_operational_v1",
    _REPO_ROOT / "data/raw/real_outputs/cross_domain_docstrings_operational_v1",
    _REPO_ROOT / "data/parsed/real_outputs/public_research_v1/reference_representations.jsonl",
    _REPO_ROOT / "data/parsed/real_outputs/cross_domain_docstrings_operational_v1/"
    "reference_representations.jsonl",
)


def test_frozen_repr_and_task_helper_replay_from_working_bytes() -> None:
    pins = verify_runtime_pins(_REPO_ROOT, helper_path=_HELPER)

    assert pins.repr_freeze_commit == REPR_FREEZE_COMMIT
    assert pins.repr_spec_hash == REPR_SPEC_HASH
    assert pins.repr_implementation_set_hash == REPR_IMPLEMENTATION_SET_HASH
    assert pins.repr_api_hash == REPR_API_HASH


def test_exact_301_recipe_recovers_only_unknown_three_voter_inputs() -> None:
    if any(not path.exists() for path in _LOCAL_301_PREREQUISITES):
        pytest.skip("frozen ignored existing-301 evidence is unavailable in this worktree")
    pins = verify_runtime_pins(_REPO_ROOT, helper_path=_HELPER)
    rows, receipt = load_existing_301(
        _REPO_ROOT,
        recipe_path=_RECIPE,
        helper_path=_HELPER,
        pins=pins,
    )

    assert len(rows) == 301
    assert receipt.unique_reference_count == 50
    assert receipt.family_counts == {
        "public_research": 3,
        "algebra": 195,
        "cross_domain": 103,
    }
    assert len(receipt.consumed_files) == 1828
    assert (
        receipt.consumed_bundle_sha256
        == "42c2501bc17daed82594e4be84150e3b27011204b2aff7ad56d130812d97c2dc"
    )
    assert receipt.all_unknown is True
    selected = [row for row in rows if row.candidate.legacy_pair_id == _SMOKE_PAIR]
    assert len(selected) == 1
    assert selected[0].source.training_eligible is False
