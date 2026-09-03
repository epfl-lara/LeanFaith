from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.sprint.engine import OPERATIONS, operation_mask, operations_in_mask
from leanfaith.sft1.sprint.inventory import wave2_applicability, write_wave2_census
from leanfaith.sft1.sprint.runner import load_sprint_config
from leanfaith.sft1.sprint.square import (
    SQUARE_FIXTURES,
    SQUARE_OPERATIONS,
    SquareError,
    SquareSelection,
    cap_square_selection,
    collapse_exact_repeated_records,
)

ROOT = find_repo_root(Path(__file__))
CONFIG_DIR = ROOT / "configs/transformations/sft1_value_first_v1"
SOURCE_CONFIGS = (
    "wave2_mathlib_v1.yaml",
    "wave2_physlib_v1.yaml",
    "wave2_cslib_v1.yaml",
)
COMBINED_CONFIG = "wave2_combined_v1.yaml"
NEW_OPERATIONS = {
    "P21_ZETA_REDUCE_V1",
    "P32_ADD_ASSOC_LOCAL_V1",
    "P32_ADD_COMM_LOCAL_V1",
    "P35_SET_INTER_MEMBERSHIP_V1",
    "N26_INCREMENT_BOUND_PROOF_V1",
}
EVALUATED_DROPPED_OPERATIONS = {"P21_BETA_REDUCE_V1"}


def test_historical_and_wave2_configs_load_without_mutating_old_operation_set() -> None:
    historical = load_sprint_config(ROOT, CONFIG_DIR / "sprint_v1.yaml")
    assert len(historical.config.engine.operations) == 9
    for name in SOURCE_CONFIGS:
        loaded = load_sprint_config(ROOT, CONFIG_DIR / name)
        assert set(loaded.config.engine.operations) >= NEW_OPERATIONS
        assert loaded.config.output.staging_root.endswith(
            f"wave2/{loaded.config.project.project_id}"
        )
    combined = load_sprint_config(ROOT, CONFIG_DIR / COMBINED_CONFIG)
    assert combined.config.sprint_id == "sft1_wave2_combined_v1"
    assert combined.config.output.staging_root.endswith("wave2/combined")


def test_operation_mask_round_trip_including_wave2_batch() -> None:
    selected = tuple(operation for operation in OPERATIONS if operation in NEW_OPERATIONS)
    assert operations_in_mask(operation_mask(selected)) == selected


def test_wave2_master_contract_is_additive_and_model_rows_are_minimal() -> None:
    payload = yaml.safe_load((CONFIG_DIR / "wave2_v1.yaml").read_text(encoding="utf-8"))
    assert payload["base_commit"] == "1e2019b7ffe3bea99068d0c3055487863ff8db74"
    assert payload["release"]["prefix"] == "wave2/core_v1"
    assert payload["release"]["maximum_rows"] == 500_000
    assert payload["execution"]["model_facing_fields"] == ["reference", "candidate", "label"]
    assert payload["execution"]["census_and_filter_before_lean"] is True
    assert {
        item["operation_id"] for item in payload["operations"]["evaluated_dropped"]
    } == EVALUATED_DROPPED_OPERATIONS


def test_string_applicability_is_conservative_and_operation_specific() -> None:
    row = {
        "statement": (
            "theorem sample (n i : Nat) : i ∈ Finset.range n ↔ (fun x => x + (1 + 2)) i = i + 3"
        )
    }
    operations = set(wave2_applicability(row))
    assert {
        *EVALUATED_DROPPED_OPERATIONS,
        "P32_ADD_ASSOC_LOCAL_V1",
        "P32_ADD_COMM_LOCAL_V1",
        "N26_INCREMENT_BOUND_PROOF_V1",
        "N25_TOGGLE_EQ_NE_PROOF_V1",
    } <= operations


def test_wave2_census_writes_deterministic_square_target_files(tmp_path: Path) -> None:
    rows = [
        {
            "name": "Demo.range",
            "module": "Mathlib.Demo",
            "path": "Mathlib/Demo.lean",
            "line": 1,
            "statement": "theorem Demo.range (n i : Nat) : i ∈ Finset.range n ↔ i < n",
        },
        {
            "name": "Demo.add",
            "module": "Mathlib.Demo",
            "path": "Mathlib/Demo.lean",
            "line": 2,
            "statement": "theorem Demo.add (a b c : Nat) : (a + b) + c = a + (b + c)",
        },
    ]
    report = write_wave2_census(rows, tmp_path, project_id="mathlib", project_revision="a" * 40)
    assert report["inventory_rows"] == 2
    assert report["operation_counts"]["N26_INCREMENT_BOUND_PROOF_V1"] == 1
    for name in (
        "square_wave2_n26.json",
        "square_wave2_n32.json",
        "square_wave2_n25.json",
        "square_wave2_n31.json",
    ):
        payload = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        assert payload["project_id"] == "mathlib"
        assert isinstance(payload["roots"], list)


def test_wave2_square_operations_close_only_proof_backed_negatives() -> None:
    expected = {
        "SQUARE_WAVE2_N26_V1": "N26_INCREMENT_BOUND_PROOF_V1",
        "SQUARE_WAVE2_N32_V1": "N32_SWAP_ROLE_ORDER_PROOF_V1",
        "SQUARE_WAVE2_N25_V1": "N25_TOGGLE_EQ_NE_PROOF_V1",
        "SQUARE_WAVE2_N31_V1": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    }
    assert {key: SQUARE_OPERATIONS[key]["negative"] for key in expected} == expected
    assert expected.keys() - {"SQUARE_WAVE2_N31_V1"} <= SQUARE_FIXTURES.keys()


def test_square_row_ceiling_keeps_only_complete_groups() -> None:
    kept = [
        {"sidecar": {"root_id": f"root-{root}", "operation_id": "SQUARE_WAVE2_N25_V1"}}
        for root in range(3)
        for _ in range(4)
    ]
    keys = [f"root-{root}|SQUARE_WAVE2_N25_V1" for root in range(3)]
    capped = cap_square_selection(SquareSelection(kept, keys, [], [], 0), 8)
    assert len(capped.kept) == 8
    assert capped.accepted_roots == keys[:2]
    assert capped.capacity_squares == keys[2:]


def test_overlapping_runs_collapse_only_exact_repeated_pair_ids() -> None:
    first = {
        "row": {"reference": "a", "candidate": "b", "label": True},
        "row_hash": "row-a",
        "sidecar": {"pair_id": "pair:a"},
    }
    second = {
        "row": {"reference": "c", "candidate": "d", "label": False},
        "row_hash": "row-b",
        "sidecar": {"pair_id": "pair:b"},
    }
    kept, repeated = collapse_exact_repeated_records([first, second, dict(first)])
    assert kept == [first, second]
    assert repeated == 1

    collision = {**first, "row_hash": "different"}
    with pytest.raises(SquareError, match="pair ID collision across source runs"):
        collapse_exact_repeated_records([first, collision])
