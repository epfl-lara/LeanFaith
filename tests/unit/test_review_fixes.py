"""Regression tests for the consolidated review findings (codex + deep review).

Each test pins one confirmed defect from the LF-010-era adversarial review so
it can never silently return.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config import (
    CanonicalizationError,
    ConfigError,
    StrictModel,
    canonical_json_bytes,
    hash_canonical,
    load_config,
    load_yaml_mapping,
)
from leanfaith.config.models import SecretRef
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.lean.response_normalization import classify_response, normalize_response
from leanfaith.lean.versions import LeanVersionError, parse_lean_version
from leanfaith.schemas import (
    FaithfulnessLevels,
    InvalidIdError,
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    make_id,
)
from tests.unit.record_factories import resolved_label

_CTX = "ctx:" + "0" * 64

# --- labels: contradictory evidence rejected ---


def test_positive_label_with_refuted_direction_rejected() -> None:
    with pytest.raises(ValueError, match="refuted truth direction"):
        resolved_label(
            truth_A_implies_B=False,
            truth_B_implies_A=True,
            faithfulness_levels=FaithfulnessLevels(F1_same_claim=True, F2_truth_equivalent=False),
        )


def test_negative_label_with_refuted_direction_accepted() -> None:
    label = resolved_label(
        same_claim=False,
        resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        relation=RelationLabel.A_STRONGER,
        truth_A_implies_B=True,
        truth_B_implies_A=False,
        faithfulness_levels=FaithfulnessLevels(F1_same_claim=False, F2_truth_equivalent=False),
        quality_tier=QualityTier.GOLD_COUNTEREXAMPLE,
        resolution_method="directional_proof_plus_separator",
        eval_eligibility=True,
    )
    assert label.faithfulness_levels.F2_truth_equivalent is False


# --- normalization: sorry spellings and root-goal separation ---


def _request(**overrides: object) -> LeanRequest:
    payload: dict[str, object] = {
        "request_id": "r",
        "context_id": _CTX,
        "code": "theorem t : True := trivial",
    }
    payload.update(overrides)
    return LeanRequest(**payload)  # type: ignore[arg-type]


def test_single_quote_sorry_diagnostic_recognized() -> None:
    raw = {
        "messages": [{"severity": "warning", "data": "declaration uses 'sorry'"}],
        "sorries": [{"goal": "⊢ True", "proof_state": 0}],
    }
    assert classify_response(raw, root_goals_requested=True) == LeanStatus.VALID_WITH_SORRY


def test_invalid_response_with_root_goals_gets_no_phantom_sorries() -> None:
    raw = {
        "messages": [{"severity": "error", "data": "Type mismatch"}],
        "sorries": [{"goal": "⊢ False", "proof_state": 0}],
    }
    result = normalize_response(
        _request(root_goals=True),
        raw,
        request_hash="h" * 64,
        context_fingerprint="0" * 64,
        elapsed_ms=1,
        raw_response_path=None,
    )
    assert result.status == LeanStatus.INVALID
    assert result.sorries == ()  # root-goal entries are not admissions
    assert result.root_goals == ("⊢ False",)


# --- versions: sentinel headroom ---


def test_implausible_rc_number_rejected() -> None:
    with pytest.raises(LeanVersionError, match="implausible"):
        parse_lean_version("v4.31.0-rc1000000")


# --- hashing: -0.0 normalization ---


def test_negative_zero_normalized() -> None:
    assert hash_canonical({"x": -0.0}) == hash_canonical({"x": 0.0})
    assert canonical_json_bytes({"x": -0.0}) == b'{"x":0.0}'


# --- ids: broadened machine-local guard ---


@pytest.mark.parametrize(
    "bad",
    [
        "/var/tmp/run/x.lean",
        "/storage/someone/data/x.jsonl",
        "/opt/data/lean/corpus.jsonl",  # full-string absolute path shape
        "C:\\Users\\someone\\data.lean",
    ],
)
def test_absolute_path_shapes_rejected(bad: str) -> None:
    with pytest.raises(InvalidIdError):
        make_id("thm", {"file": bad})


def test_machine_local_mapping_keys_rejected() -> None:
    local = str(Path.home() / "private")
    with pytest.raises(InvalidIdError, match="machine-local"):
        make_id("thm", {local: "x"})


def test_lean_code_with_slashes_still_allowed() -> None:
    # Lean code contains spaces, so the full-string path pattern never fires.
    assert make_id("thm", {"code": "/- header -/ theorem t : 1 / 2 = 0 := rfl"})
    assert make_id("thm", {"view": "a / b / c"})


# --- loader: recursive strictness + secret redaction ---


def test_yaml_set_tag_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("globs: !!set\n  ? A\n  ? B\n")
    with pytest.raises(ConfigError, match="unsupported YAML node type"):
        load_yaml_mapping(path)


def test_yaml_bare_date_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("probe_date: 2026-07-10\n")
    with pytest.raises(ConfigError, match="quote dates"):
        load_yaml_mapping(path)


def test_yaml_nested_non_string_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("outer:\n  1: x\n")
    with pytest.raises(ConfigError, match="non-string key"):
        load_yaml_mapping(path)


class _SecretConfig(StrictModel):
    token: SecretRef


def test_validation_error_never_echoes_input(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("token: hf_SUPERSECRETVALUE123\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, _SecretConfig)
    assert "hf_SUPERSECRETVALUE123" not in str(excinfo.value)


def test_datetime_still_rejected_in_canonical() -> None:
    import datetime

    with pytest.raises(CanonicalizationError):
        hash_canonical({"t": datetime.date(2026, 7, 10)})
