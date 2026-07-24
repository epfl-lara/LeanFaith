"""Revision 4.1 Gate-2 immutable-regression comparator."""

from __future__ import annotations

import json
from pathlib import Path

from leanfaith.config.hashing import hash_file
from leanfaith.lean.extraction_regression import (
    audit_gate2_scale,
    compare_extraction_replays,
    validate_sft_classic_regression,
)
from leanfaith.schemas.source import make_hf_source_record_id


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_extraction_regression_detects_and_accepts_per_row_outcomes(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    input_path.write_text("{}\n{}\n", encoding="utf-8")
    dataset_id, revision, split = "owner/data", "a" * 40, "train"
    source_0 = make_hf_source_record_id(dataset_id, revision, split, 0)
    source_1 = make_hf_source_record_id(dataset_id, revision, split, 1)
    theorem_path = tmp_path / "theorems.jsonl"
    failure_path = tmp_path / "failures.jsonl"
    _write(
        theorem_path,
        [
            {
                "theorem": {
                    "theorem_id": "thm:" + "0" * 64,
                    "source_record_id": source_0,
                    "extraction_route": "question_statement",
                    "declaration_name": "t",
                    "statement_content_hash": "b" * 64,
                }
            }
        ],
    )
    _write(
        failure_path,
        [
            {
                "source_record": source_1,
                "outcome_level": "row",
                "code": "not_a_proposition",
            }
        ],
    )
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(
        json.dumps(
            {
                "dataset_id": dataset_id,
                "revision": revision,
                "split": split,
                "input_sha256": hash_file(input_path),
                "rows": [
                    {
                        "row_index": 0,
                        "outcome": "accepted",
                        "extraction_route": "question_statement",
                        "declaration_name": "t",
                        "statement_content_hash": "b" * 64,
                    },
                    {
                        "row_index": 1,
                        "outcome": "failure",
                        "failure_code": "not_a_proposition",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    report = validate_sft_classic_regression(
        input_path=input_path,
        theorem_path=theorem_path,
        failure_path=failure_path,
        expected_path=expected_path,
    )
    assert report.ok

    bad = json.loads(theorem_path.read_text())
    bad["theorem"]["statement_content_hash"] = "c" * 64
    _write(theorem_path, [bad])
    report = validate_sft_classic_regression(
        input_path=input_path,
        theorem_path=theorem_path,
        failure_path=failure_path,
        expected_path=expected_path,
    )
    assert not report.ok
    assert "row 0" in report.errors[0]


def test_extraction_replay_ignores_volatile_fields_but_not_normalized_outcomes(
    tmp_path: Path,
) -> None:
    left_theorems = tmp_path / "left_theorems.jsonl"
    right_theorems = tmp_path / "right_theorems.jsonl"
    left_failures = tmp_path / "left_failures.jsonl"
    right_failures = tmp_path / "right_failures.jsonl"
    theorem = {
        "theorem": {
            "source_record_id": "a" * 64,
            "theorem_id": "thm:" + "b" * 64,
            "declaration_name": "t",
            "declaration_ordinal": 0,
            "statement_content_hash": "c" * 64,
        },
        "representation": {
            "representation_id": "repr:" + "d" * 64,
            "content_hash": "e" * 64,
        },
        "created_at": "volatile-left",
    }
    _write(left_theorems, [theorem])
    _write(right_theorems, [dict(theorem, created_at="volatile-right")])
    failure = {
        "source_record": "f" * 64,
        "declaration_name": None,
        "code": "source_non_elaboration",
        "outcome_level": "row",
        "detail": "volatile diagnostic",
    }
    _write(left_failures, [failure])
    _write(right_failures, [dict(failure, detail="different diagnostic")])
    assert compare_extraction_replays(
        left_theorem_path=left_theorems,
        left_failure_path=left_failures,
        right_theorem_path=right_theorems,
        right_failure_path=right_failures,
    ).ok

    changed = json.loads(right_theorems.read_text())
    changed["theorem"]["statement_content_hash"] = "0" * 64
    _write(right_theorems, [changed])
    report = compare_extraction_replays(
        left_theorem_path=left_theorems,
        left_failure_path=left_failures,
        right_theorem_path=right_theorems,
        right_failure_path=right_failures,
    )
    assert not report.ok


def test_extraction_replay_rejects_reordered_normalized_outcomes(tmp_path: Path) -> None:
    left_theorems = tmp_path / "left_theorems.jsonl"
    right_theorems = tmp_path / "right_theorems.jsonl"
    left_failures = tmp_path / "left_failures.jsonl"
    right_failures = tmp_path / "right_failures.jsonl"
    rows = [
        {
            "theorem": {
                "source_record_id": str(index) * 64,
                "theorem_id": "thm:" + str(index + 2) * 64,
                "declaration_name": f"t{index}",
                "declaration_ordinal": index,
                "statement_content_hash": str(index + 4) * 64,
            }
        }
        for index in range(2)
    ]
    _write(left_theorems, rows)
    _write(right_theorems, list(reversed(rows)))
    _write(left_failures, [])
    _write(right_failures, [])

    report = compare_extraction_replays(
        left_theorem_path=left_theorems,
        left_failure_path=left_failures,
        right_theorem_path=right_theorems,
        right_failure_path=right_failures,
    )
    assert not report.ok
    assert "ordered normalized outcomes differ" in report.errors[0]


def test_gate2_scale_audit_reconciles_frozen_denominator_and_hashes(tmp_path: Path) -> None:
    dataset_id, revision, split = "owner/data", "a" * 40, "train"
    sample_path = tmp_path / "sample.jsonl"
    _write(
        sample_path,
        [
            {"source_row_index": 3, "row": {"uuid": "u3"}},
            {"source_row_index": 7, "row": {"uuid": "u7"}},
        ],
    )
    source_3 = make_hf_source_record_id(dataset_id, revision, split, 3)
    source_7 = make_hf_source_record_id(dataset_id, revision, split, 7)
    theorem_path = tmp_path / "theorems.jsonl"
    _write(
        theorem_path,
        [
            {
                "theorem": {
                    "theorem_id": "thm:" + "9" * 64,
                    "source_record_id": source_3,
                    "raw_row_hash": "1" * 64,
                    "question_hash": "2" * 64,
                    "lean_code_hash": "3" * 64,
                    "extraction_route": "question_statement",
                    "inline_elaboration_source": "theorem t : True := by sorry",
                    "nl_source_link": "hf://owner/data/train/3",
                    "nl_trust": "uncertain",
                }
            }
        ],
    )
    failure_path = tmp_path / "failures.jsonl"
    _write(
        failure_path,
        [
            {
                "source_record": source_7,
                "outcome_level": "row",
                "code": "source_non_elaboration",
            }
        ],
    )
    sample_manifest_path = tmp_path / "sample_manifest.json"
    sample_manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": dataset_id,
                "revision": revision,
                "split": split,
                "sample_rows": 2,
                "output_sha256": hash_file(sample_path),
                "input_partitions": [{"path": "shard.arrow", "rows": 10, "sha256": "4" * 64}],
            }
        ),
        encoding="utf-8",
    )
    extraction_manifest_path = tmp_path / "extraction_manifest.json"
    extraction_manifest_path.write_text(
        json.dumps(
            {
                "attempted_row_count": 2,
                "row_count": 1,
                "declaration_count": 1,
                "terminal_outcome_counts": {"accepted": 1, "failed": 1},
                "config_hash": "5" * 64,
                "environment_hash": "6" * 64,
                "context_hash": "7" * 64,
                "code_tree_hash": "8" * 64,
                "input_partition_checksums": {str(sample_path): hash_file(sample_path)},
                "output_partition_checksums": {str(theorem_path): hash_file(theorem_path)},
                "failure_partition_checksums": {str(failure_path): hash_file(failure_path)},
            }
        ),
        encoding="utf-8",
    )
    report = audit_gate2_scale(
        sample_path=sample_path,
        sample_manifest_path=sample_manifest_path,
        extraction_manifest_path=extraction_manifest_path,
        theorem_path=theorem_path,
        failure_path=failure_path,
    )
    assert report.ok
    assert report.accepted_rows == report.failed_rows == 1
