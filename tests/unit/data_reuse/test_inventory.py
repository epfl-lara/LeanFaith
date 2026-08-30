from __future__ import annotations

from pathlib import Path

from leanfaith.data_reuse.inventory import (
    bounded_group_samples,
    canonical_json_bytes,
    stable_preview_id,
    tree_hash,
)


def test_canonical_json_and_stable_id_ignore_mapping_order() -> None:
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert stable_preview_id("artifact", "source") == stable_preview_id("artifact", "source")
    assert stable_preview_id("artifact", "source") != stable_preview_id("artifact", "other")


def test_tree_hash_binds_relative_paths_and_contents(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "nested" / "b.txt"
    second.parent.mkdir()
    first.write_text("alpha", encoding="utf-8")
    second.write_text("beta", encoding="utf-8")

    original = tree_hash(tmp_path)
    assert original == tree_hash(tmp_path)
    second.write_text("changed", encoding="utf-8")
    assert original != tree_hash(tmp_path)


def test_bounded_group_samples_are_sorted_and_bounded() -> None:
    rows = [
        {"group": "b", "id": "2"},
        {"group": "a", "id": "3"},
        {"group": "a", "id": "1"},
        {"group": "a", "id": "2"},
    ]
    sampled = bounded_group_samples(
        rows,
        group_key=lambda row: row["group"],
        stable_key=lambda row: row["id"],
        limit=2,
    )
    assert list(sampled) == ["a", "b"]
    assert [row["id"] for row in sampled["a"]] == ["1", "2"]
    assert [row["id"] for row in sampled["b"]] == ["2"]
