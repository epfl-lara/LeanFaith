"""LF-007: Lean version parsing and the binding advertised range (§6.2)."""

from __future__ import annotations

import pytest

from leanfaith.lean.versions import (
    ADVERTISED_MAX,
    ADVERTISED_MIN,
    LeanVersionError,
    in_advertised_range,
    parse_lean_version,
)


def test_parse_stable_and_rc() -> None:
    stable = parse_lean_version("v4.31.0")
    rc = parse_lean_version("v4.31.0-rc1")
    assert str(stable) == "v4.31.0"
    assert str(rc) == "v4.31.0-rc1"
    assert rc.is_release_candidate
    assert not stable.is_release_candidate


def test_parse_toolchain_file_format() -> None:
    version = parse_lean_version("leanprover/lean4:v4.31.0-rc1")
    assert str(version) == "v4.31.0-rc1"


def test_rc_precedes_stable() -> None:
    assert parse_lean_version("v4.31.0-rc1") < parse_lean_version("v4.31.0")
    assert parse_lean_version("v4.31.0-rc1") < parse_lean_version("v4.31.0-rc2")
    assert parse_lean_version("v4.30.0") < parse_lean_version("v4.31.0-rc1")


@pytest.mark.parametrize("bad", ["4.31.0", "v4.31", "v4.31.0-beta1", "lean4:v4.31.0", ""])
def test_unparsable_versions_rejected(bad: str) -> None:
    with pytest.raises(LeanVersionError):
        parse_lean_version(bad)


def test_advertised_range_bounds() -> None:
    assert str(ADVERTISED_MIN) == "v4.8.0-rc1"
    assert str(ADVERTISED_MAX) == "v4.31.0-rc1"


@pytest.mark.parametrize(
    ("version", "inside"),
    [
        ("v4.8.0-rc1", True),
        ("v4.8.0", True),
        ("v4.30.0", True),
        ("v4.31.0-rc1", True),
        ("v4.31.0", False),  # stable v4.31.0 is OUTSIDE the advertised range (§6.2)
        ("v4.32.0-rc1", False),  # mathlib master toolchain: never usable (§6.2)
        ("v4.7.0", False),
    ],
)
def test_in_advertised_range(version: str, inside: bool) -> None:
    assert in_advertised_range(parse_lean_version(version)) is inside
