"""Regression tests for the historical corpus-v0 reproducibility adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import hash_file
from leanfaith.corpus2.from_mixed_v0 import convert
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.train2.trainer import TrainingRecord


class FakeTokenizer:
    """Return a predictable length without loading the archived model snapshot."""

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        if "OVERLENGTH" in text:
            return list(range(1_025))
        return [1, 2] if add_special_tokens else [1]


def _row(
    record_id: str,
    split: str,
    reference: str,
    candidate: str,
    *,
    target: str = "same_claim",
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "source": {"headless": reference},
        "candidate": {"headless": candidate},
        "split": split,
        "pseudo_target": target,
        "split_component_id": f"group-{record_id}",
        "family_ids": ["fixture"],
        "pseudo_target_basis": "fixture-label",
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_convert_reproduces_projection_and_filters(tmp_path: Path) -> None:
    blocked_reference = "(n : Nat) : n = n"
    rows = [
        _row("train-keep", "train", "(a : Nat) : a = a", "(b : Nat) : b = b"),
        _row(
            "validation-keep",
            "validation",
            "(a : Nat) : a + 0 = a",
            "(a : Nat) : a + 1 = a",
            target="different_claim",
        ),
        _row("test-keep", "test", "True", "Not False"),
        _row("blocked", "train", blocked_reference, "False"),
        _row("identical", "train", "True", " True "),
        _row("overlength", "train", "OVERLENGTH reference", "candidate"),
    ]
    mixed_path = tmp_path / "mixed.jsonl"
    blocklist_path = tmp_path / "blocklist.json"
    out_dir = tmp_path / "out"
    _write_jsonl(mixed_path, rows)
    blocklist_path.write_text(
        json.dumps({"near_dup_hashes": [signature_near_dup_hash(blocked_reference)]}),
        encoding="utf-8",
    )

    manifest = convert(
        mixed_path=mixed_path,
        blocklist_path=blocklist_path,
        out_dir=out_dir,
        snapshot_dir=tmp_path / "unused-tokenizer",
        tokenizer=FakeTokenizer(),
    )

    assert manifest["command"] == "corpus2_from_mixed_v0"
    assert manifest["counts"] == {
        "train": 1,
        "validation": 1,
        "test": 1,
        "blocklist_dropped": 1,
        "identical_dropped": 1,
        "overlength_dropped": 1,
    }
    for split, expected_id in (
        ("train", "train-keep"),
        ("validation", "validation-keep"),
        ("test", "test-keep"),
    ):
        output_path = out_dir / f"records_{split}_v0.jsonl"
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        record = TrainingRecord.model_validate(payload)
        assert record.record_id == expected_id
        assert manifest["outputs"][split]["sha256"] == hash_file(output_path)  # type: ignore[index]

    written_manifest = json.loads((out_dir / "corpus_v0_manifest.json").read_text())
    assert written_manifest == manifest
