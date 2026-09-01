"""Focused invariants for the two-row SFT1 thin smoke."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_file
from leanfaith.sft1.thin_smoke import (
    DEFAULT_CONFIG,
    ThinSmokeConfig,
    build_compile_context,
    build_inputs,
    build_session_body,
    load_thin_smoke_config,
    replay_thin_smoke,
)

ROOT = Path(__file__).resolve().parents[3]


def test_strict_config_binds_exact_two_row_authorization_and_sources() -> None:
    loaded = load_thin_smoke_config(ROOT)
    config = loaded.config
    assert config.authorization.exact_local_row_count == 2
    assert config.authorization.mathlib_only is True
    assert config.authorization.general_n31_bank_allowed is False
    assert all(
        getattr(config.authorization, field) is False
        for field in (
            "census_allowed",
            "hundred_roots_allowed",
            "ten_k_allowed",
            "production_allowed",
            "scale_allowed",
            "training_allowed",
            "publication_allowed",
        )
    )
    assert config.positive.root_name == "PNat.gcd_comm"
    assert config.positive.operation_id == "P18_SYMMETRIZE_EQUALITY_V1"
    assert config.negative.reference_proposition == ("∀ (n : Nat) (hn : n = 0), n + 1 = 1")
    assert config.negative.operation_id == "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
    assert config.negative.candidate_truth == "refuted"
    assert hash_file(ROOT / config.implementation.wave1_path) == (
        config.implementation.wave1_sha256
    )
    assert hash_file(ROOT / config.implementation.helper_path) == (
        config.implementation.helper_sha256
    )
    assert hash_file(ROOT / config.implementation.runner_path) == (
        config.implementation.runner_sha256
    )
    assert config.project.lean_rss_claim_gib == 24


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("authorization", "exact_local_row_count"), 3),
        (("authorization", "general_n31_bank_allowed"), True),
        (("authorization", "production_allowed"), True),
        (("positive", "operation_id"), "P01_ALPHA_RENAME_SINGLE_V1"),
        (("negative", "smoke_only"), False),
        (("project", "persistent_worker_count"), 2),
        (("project", "options"), {"Elab.async": True, "autoImplicit": False}),
    ),
)
def test_policy_drift_fails_closed(path: tuple[str, str], value: object) -> None:
    loaded = load_thin_smoke_config(ROOT)
    payload = deepcopy(loaded.config.model_dump(mode="json"))
    payload[path[0]][path[1]] = value
    with pytest.raises(ValidationError):
        ThinSmokeConfig.model_validate(payload)


def test_one_meta_request_contains_exactly_four_unrolled_endpoints() -> None:
    loaded = load_thin_smoke_config(ROOT)
    context = build_compile_context(ROOT, loaded.config)
    body = build_session_body("sft1-thin-smoke:v1")
    assert body.startswith("run_meta do\n")
    assert body.count("run_meta do") == 1
    assert body.count("LeanFaith.GoalV1.emitClosedProp") == 4
    assert "private def applyP18" in context.command_preamble
    assert "private def replayP18" in context.command_preamble
    assert "namespace LeanFaith.SFT1.Wave1" not in context.command_preamble
    assert "Term.elabTerm" not in body
    assert not re.search(
        r"\b(?:theorem|lemma|axiom|opaque|example)\b|:=\s*by\b|\bsorry\b|addDecl",
        body,
    )
    assert context.options == {"Elab.async": False, "autoImplicit": False}
    assert len(build_inputs(loaded.config)) == 4


def test_negative_helper_is_exact_canary_not_a_general_bank() -> None:
    source = (ROOT / "LeanFaith/Meta/SFT1/ThinSmoke.lean").read_text(encoding="utf-8")
    assert "n31_nat_eq_zero_add_one_v1" in source
    assert "lowerLooseBVars 1 1" in source
    assert "candidateRefutation" in source
    assert "witnessRefutation" in source
    assert "general_n31_bank_activated" in source
    assert "N31TargetBank" not in source
    assert "admittedN31BankIdentities" not in source
    assert "mkSorry" not in source and "sorryAx" not in source


def test_positive_helper_kernel_checks_the_p18_equivalence() -> None:
    source = (ROOT / "LeanFaith/Meta/SFT1/ThinSmoke.lean").read_text(encoding="utf-8")
    assert "P18 equivalence proof" in source
    assert 'equivalence_proof", Json.str "kernel_checked"' in source
    assert "applyP18 source" in source
    assert "replayP18 reference candidate certificate" in source


def test_new_non_test_implementation_stays_under_two_thousand_lines() -> None:
    paths = (
        ROOT / "LeanFaith/Meta/SFT1/ThinSmoke.lean",
        ROOT / "src/leanfaith/sft1/thin_smoke.py",
    )
    assert sum(len(path.read_text(encoding="utf-8").splitlines()) for path in paths) < 2000


def test_live_evidence_and_cache_replay_when_present() -> None:
    loaded = load_thin_smoke_config(ROOT)
    evidence = ROOT / loaded.config.output.evidence_dir
    manifest_path = evidence / loaded.config.output.manifest_file
    if not manifest_path.is_file():
        pytest.skip("bounded live smoke has not run yet")
    rows = [
        json.loads(line)
        for line in (evidence / loaded.config.output.core_rows_file)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    sidecars = [
        json.loads(line)
        for line in (evidence / loaded.config.output.sidecars_file)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(rows) == len(sidecars) == 2
    assert {row["label"] for row in rows} == {False, True}
    assert all(
        set(row) == {"pair_id", "root_id", "reference", "candidate", "label", "operation_id"}
        for row in rows
    )
    assert manifest["lean_request_count"] == 1
    assert manifest["cache_replay_lean_request_count"] == 0
    assert manifest["cache_replay_hits"] == 2
    assert manifest["thin_task_local_p18"] is True
    assert manifest["wave1_engine_live_evidence"] is False
    assert manifest["resource_released"] is True
    assert replay_thin_smoke(ROOT) == 2


def test_default_config_path_is_task_owned() -> None:
    expected = Path("configs/transformations/sft1_value_first_v1/thin_smoke_v1.yaml")
    assert expected == DEFAULT_CONFIG
