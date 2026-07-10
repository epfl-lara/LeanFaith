"""LF-002: canonical JSON and hashing behavior."""

from __future__ import annotations

import datetime
import math
from pathlib import Path

import pytest

from leanfaith.config import (
    CanonicalizationError,
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)


def test_key_order_does_not_change_hash() -> None:
    assert hash_canonical({"a": 1, "b": [2, 3]}) == hash_canonical({"b": [2, 3], "a": 1})


def test_value_change_changes_hash() -> None:
    assert hash_canonical({"a": 1}) != hash_canonical({"a": 2})


def test_canonical_bytes_are_compact_sorted_utf8() -> None:
    data = canonical_json_bytes({"b": "∀x", "a": 1})
    assert data == '{"a":1,"b":"∀x"}'.encode()


def test_nested_structures_round_trip() -> None:
    value = {"outer": [{"k": None}, True, 1.5, "s"]}
    assert canonical_json_bytes(value) == b'{"outer":[{"k":null},true,1.5,"s"]}'


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_floats_rejected(bad: float) -> None:
    with pytest.raises(CanonicalizationError, match="non-finite"):
        hash_canonical({"x": bad})


def test_datetime_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="non-JSON value"):
        hash_canonical({"ts": datetime.datetime(2026, 7, 10, tzinfo=datetime.UTC)})


def test_path_object_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="non-JSON value"):
        hash_canonical({"p": Path("/tmp/x")})


def test_non_string_key_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="non-string mapping key"):
        hash_canonical({1: "x"})


def test_bytes_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="non-JSON value"):
        hash_canonical({"b": b"raw"})


def test_error_message_names_path() -> None:
    with pytest.raises(CanonicalizationError, match=r"\$\.a\[1\]\.b"):
        hash_canonical({"a": [0, {"b": {1, 2}}]})


def test_sha256_hex_known_vector() -> None:
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_hash_file_matches_bytes(tmp_path: Path) -> None:
    target = tmp_path / "sample.bin"
    target.write_bytes(b"leanfaith")
    assert hash_file(target) == sha256_hex(b"leanfaith")
