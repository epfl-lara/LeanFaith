from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from leanfaith.cpt1.builder import (
    BuildConfig,
    Cpt1Error,
    SourceSpec,
    build_fixture,
    validate_release,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _config(tmp_path: Path, blocklist: Path, *, rows_per_shard: int = 2) -> BuildConfig:
    return BuildConfig(
        schema_version="cpt1_v1.0",
        output_root=tmp_path / "unused",
        cache_dir=tmp_path / "cache",
        blocklist_path=blocklist,
        destination_repo="Lemmy00/leanfaith-cpt1-v1",
        rows_per_shard=rows_per_shard,
        compression="zstd",
        feedback_test_excluded_rows=623741,
        sources=(
            SourceSpec(
                name="lean_docs",
                repo_id="formalmathatepfl/lean-docs",
                revision="a" * 40,
                config="default",
                split="train",
                recipe="text",
                native_id_column="id",
                expected_rows=3,
            ),
            SourceSpec(
                name="feedback_training",
                repo_id="formalmathatepfl/feedback_data_training",
                revision="b" * 40,
                config="default",
                split="train",
                recipe="question_answer",
                native_id_column="uuid",
                expected_rows=3,
            ),
        ),
    )


@pytest.fixture
def blocklist(tmp_path: Path) -> Path:
    path = tmp_path / "blocklist.json"
    path.write_text(
        json.dumps(
            {
                "version": ["golden_blocklist_v1"],
                "near_dup_hashes": [_sha("blocked text")],
                "group_keys": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _rows() -> dict[str, list[dict[str, object]]]:
    return {
        "lean_docs": [
            {"id": "l0", "text": "α\n  β"},
            {"id": "l1", "text": "duplicate"},
            {"id": "l2", "text": "blocked\n\ttext"},
        ],
        "feedback_training": [
            {"uuid": "f0", "question": "question", "answer": "answer"},
            {"uuid": "f1", "question": "dupli", "answer": "cate"},
            {"uuid": "f2", "question": "", "answer": ""},
        ],
    }


def test_exact_recipes_dedup_blocklist_and_minimal_schema(
    tmp_path: Path, blocklist: Path
) -> None:
    result = build_fixture(
        _config(tmp_path, blocklist),
        rows_by_source=_rows(),
        output_root=tmp_path / "build",
    )
    data_files = sorted((result.release_root / "data").glob("*.parquet"))
    texts = [
        row["text"]
        for path in data_files
        for row in pq.read_table(path).to_pylist()
    ]
    assert texts == ["α\n  β", "duplicate", "questionanswer", ""]
    assert pq.ParquetFile(data_files[0]).schema_arrow.names == ["text"]
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["output"]["rows"] == 4
    assert manifest["source_counts"]["lean_docs"]["contamination_rows"] == 1
    assert manifest["source_counts"]["feedback_training"]["duplicates"] == 1
    assert manifest["source_counts"]["feedback_training"]["blank_rows"] == 1
    assert manifest["excluded_feedback_test"]["rows"] == 623741
    assert validate_release(result.release_root)["status"] == "passed"


def test_resume_reuses_atomic_chunks_without_duplicate_rows(
    tmp_path: Path, blocklist: Path
) -> None:
    config = _config(tmp_path, blocklist, rows_per_shard=1)
    first = build_fixture(config, rows_by_source=_rows(), output_root=tmp_path / "build")
    first_manifest = first.manifest_path.read_bytes()
    first_checksums = (first.release_root / "SHA256SUMS").read_bytes()
    second = build_fixture(config, rows_by_source=_rows(), output_root=tmp_path / "build")
    assert first.written_chunks == 6
    assert second.written_chunks == 0
    assert second.resumed_chunks == 6
    assert second.rows == first.rows == 4
    assert second.manifest_path.read_bytes() == first_manifest
    assert (second.release_root / "SHA256SUMS").read_bytes() == first_checksums
    journal = (tmp_path / "build" / "_state" / "journal.jsonl").read_text().splitlines()
    assert len(journal) == 6


def test_null_source_text_is_rejected_without_completed_chunk(
    tmp_path: Path, blocklist: Path
) -> None:
    rows = _rows()
    rows["lean_docs"][0]["text"] = None
    with pytest.raises(Cpt1Error, match="non-null string"):
        build_fixture(
            _config(tmp_path, blocklist),
            rows_by_source=rows,
            output_root=tmp_path / "build",
        )
    state = tmp_path / "build" / "_state" / "state.sqlite3"
    assert state.exists()
    assert not list((tmp_path / "build" / "release" / "data").glob("*.parquet"))


def test_question_answer_boundary_has_zero_inserted_bytes(
    tmp_path: Path, blocklist: Path
) -> None:
    rows = {
        "lean_docs": [{"id": "l0", "text": "lean"}],
        "feedback_training": [{"uuid": "f0", "question": "Q\n", "answer": " A"}],
    }
    result = build_fixture(
        _config(tmp_path, blocklist),
        rows_by_source=rows,
        output_root=tmp_path / "build",
    )
    feedback_file = sorted((result.release_root / "data").glob("*.parquet"))[1]
    assert pq.read_table(feedback_file).to_pylist() == [{"text": "Q\n A"}]
