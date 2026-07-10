"""LF-003: deterministic semantic IDs (§11.12)."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from leanfaith.schemas import InvalidIdError, id_prefix, is_valid_id, make_id, parse_id


def test_make_id_deterministic_and_order_independent() -> None:
    first = make_id("thm", {"a": 1, "b": "x"})
    second = make_id("thm", {"b": "x", "a": 1})
    assert first == second
    assert first.startswith("thm:")
    assert len(first.split(":", 1)[1]) == 64


def test_different_payloads_differ() -> None:
    assert make_id("thm", {"a": 1}) != make_id("thm", {"a": 2})


def test_different_prefixes_differ() -> None:
    assert make_id("thm", {"a": 1}) != make_id("ctx", {"a": 1})


@pytest.mark.parametrize("prefix", ["Thm", "1thm", "thm-x", "", "thm:"])
def test_invalid_prefix_rejected(prefix: str) -> None:
    with pytest.raises(InvalidIdError, match="prefix"):
        make_id(prefix, {"a": 1})


def test_timestamp_payload_rejected() -> None:
    with pytest.raises(InvalidIdError, match="not canonical-JSON"):
        make_id("thm", {"created": datetime.datetime.now(tz=datetime.UTC)})


def test_path_object_payload_rejected() -> None:
    with pytest.raises(InvalidIdError, match="not canonical-JSON"):
        make_id("thm", {"file": Path("x.lean")})


def test_home_relative_absolute_path_rejected() -> None:
    local = str(Path.home() / "data" / "x.lean")
    with pytest.raises(InvalidIdError, match="machine-local"):
        make_id("thm", {"file": local})


def test_cwd_absolute_path_rejected() -> None:
    local = str(Path.cwd() / "data" / "x.lean")
    with pytest.raises(InvalidIdError, match="machine-local"):
        make_id("thm", {"nested": [{"file": local}]})


def test_tmp_path_rejected() -> None:
    with pytest.raises(InvalidIdError, match="machine-local"):
        make_id("thm", {"file": "/tmp/scratch/x.lean"})


def test_repo_relative_path_allowed() -> None:
    identifier = make_id("thm", {"file": "Mathlib/Data/Nat/Basic.lean"})
    assert is_valid_id(identifier, prefix="thm")


def test_lean_comment_string_allowed() -> None:
    # Lean block comments start with "/-"; only machine-local roots are rejected.
    assert is_valid_id(make_id("thm", {"code": "/- comment -/ theorem t : True := trivial"}))


def test_parse_and_prefix_round_trip() -> None:
    identifier = make_id("anc", {"k": "v"})
    prefix, digest = parse_id(identifier)
    assert prefix == "anc"
    assert identifier == f"{prefix}:{digest}"
    assert id_prefix(identifier) == "anc"


@pytest.mark.parametrize(
    "bad",
    ["thm", "thm:", "thm:xyz", "THM:" + "0" * 64, "thm:" + "0" * 63, "thm:" + "G" * 64],
)
def test_malformed_ids_rejected(bad: str) -> None:
    assert not is_valid_id(bad)
    with pytest.raises(InvalidIdError, match="malformed"):
        parse_id(bad)


def test_prefix_filter() -> None:
    identifier = make_id("pair", {"a": 1})
    assert is_valid_id(identifier, prefix="pair")
    assert not is_valid_id(identifier, prefix="thm")
