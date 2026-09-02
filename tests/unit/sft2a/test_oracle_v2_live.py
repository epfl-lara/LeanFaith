"""Bounded live Lean fixtures for the v2 signature oracle (opt-in; claims one Lean worker).

Run with ``LEANFAITH_SFT2A_LIVE_LEAN=1`` on the shared host after checking the resource ledger.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from leanfaith.sft2a.config import load_sft2a_config
from leanfaith.sft2a.sprint_pilot_v52 import ORACLE_V2_FIXTURES, run_oracle_v2_live_gate

pytestmark = pytest.mark.lean

_LIVE = os.environ.get("LEANFAITH_SFT2A_LIVE_LEAN") == "1"


def test_fixture_set_covers_required_shapes() -> None:
    ids = {fixture.fixture_id for fixture in ORACLE_V2_FIXTURES}
    assert {
        "type_star_universe",
        "explicit_declared_universes",
        "explicit_undeclared_universe",
        "two_universe_metavariables_stay_distinct",
        "dependent_binders",
        "section_variable_unbound",
        "section_variable_closed",
        "rebound_open_context",
    } <= ids
    assert {fixture.expected_status for fixture in ORACLE_V2_FIXTURES} == {"valid", "invalid"}


@pytest.mark.skipif(not _LIVE, reason="set LEANFAITH_SFT2A_LIVE_LEAN=1 to run the live oracle gate")
def test_oracle_v2_live_gate_passes_all_fixtures(tmp_path: Path) -> None:
    base = load_sft2a_config(Path("configs/sft2a/closure_aware_v5_2_sprint_v1.yaml"))
    receipt = run_oracle_v2_live_gate(
        base, output_root=tmp_path, resource_task="SFT2A-SPRINT-ORACLE-V2-GATE-TEST"
    )
    assert receipt["all_passed"] is True
    assert receipt["provider_calls_executed"] == 0
    assert receipt["persistent_backends_created"] == 1
