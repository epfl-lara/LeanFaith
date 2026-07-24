"""Regression tests for bounded single-worker resumable extraction."""

from __future__ import annotations

import datetime
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from leanfaith.cli import pipeline
from leanfaith.lean.extract_run import ExtractStats


def _terminal_stats(count: int) -> ExtractStats:
    return ExtractStats(
        sources_processed=count,
        row_outcomes=Counter({"synthetic_terminal": count}),
    )


def test_single_worker_sft_chunks_run_sequentially_and_resume(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls: list[Path] = []

    def fake_chunk(**job: Any) -> ExtractStats:
        out_dir = Path(job["out_dir"])
        calls.append(out_dir)
        stats = _terminal_stats(len(job["rows"]))
        pipeline._write_chunk_marker(
            out_dir,
            job_hash=str(job["job_hash"]),
            payload=stats.as_dict(),
        )
        return stats

    monkeypatch.setattr(pipeline, "_extract_sft_chunk", fake_chunk)
    monkeypatch.setattr(pipeline, "merge_extraction_partitions", lambda *args, **kwargs: None)
    kwargs = {
        "project_dir": tmp_path,
        "context_fingerprint": "a" * 64,
        "context_id": "ctx:" + "a" * 64,
        "raw_response_dir": tmp_path / "raw",
        "rows": [{"row": index} for index in range(5)],
        "source_row_indices": list(range(5)),
        "split": "train",
        "row_offset": 0,
        "out_dir": tmp_path / "out",
        "workers": 1,
        "chunk_size": 2,
        "run_id": "run:test",
        "memory_hard_limit_mb": None,
        "resume_work_dir": tmp_path / "work",
        "code_tree_hash": "b" * 64,
        "code_bundle_hash": "c" * 64,
    }

    first = pipeline._extract_sft_parallel(**kwargs)
    assert first.sources_processed == 5
    assert [path.name for path in calls] == ["chunk-00000", "chunk-00001", "chunk-00002"]

    calls.clear()
    replay = pipeline._extract_sft_parallel(**kwargs)
    assert replay.as_dict() == first.as_dict()
    assert calls == []


def test_single_worker_mathlib_chunks_run_sequentially(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[Path] = []

    def fake_chunk(**job: Any) -> ExtractStats:
        out_dir = Path(job["out_dir"])
        calls.append(out_dir)
        stats = _terminal_stats(len(job["rel_paths"]))
        pipeline._write_chunk_marker(
            out_dir,
            job_hash=str(job["job_hash"]),
            payload=stats.as_dict(),
        )
        return stats

    monkeypatch.setattr(pipeline, "_extract_mathlib_chunk", fake_chunk)
    monkeypatch.setattr(pipeline, "merge_extraction_partitions", lambda *args, **kwargs: None)
    stats = pipeline._extract_mathlib_parallel(
        project_dir=tmp_path,
        context_fingerprint="a" * 64,
        context_id="ctx:" + "a" * 64,
        raw_response_dir=tmp_path / "raw",
        rel_paths=["Mathlib/A.lean", "Mathlib/B.lean", "Mathlib/C.lean"],
        source_revision="revision",
        out_dir=tmp_path / "out",
        workers=1,
        chunk_size=2,
        run_id="run:test",
        memory_hard_limit_mb=None,
        resume_work_dir=tmp_path / "work",
        code_tree_hash="b" * 64,
        code_bundle_hash="c" * 64,
    )
    assert stats.sources_processed == 3
    assert [path.name for path in calls] == ["chunk-00000", "chunk-00001"]


def test_chunk_marker_rejects_modified_partition(tmp_path: Path) -> None:
    out_dir = tmp_path / "chunk"
    partition = out_dir / "theorems" / "sft_classic.jsonl"
    partition.parent.mkdir(parents=True)
    partition.write_text('{"row": 1}\n', encoding="utf-8")
    pipeline._write_chunk_marker(out_dir, job_hash="a" * 64, payload={"rows": 1})

    assert pipeline._read_chunk_marker(out_dir, expected_job_hash="a" * 64) == {"rows": 1}
    partition.write_text('{"row": 2}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="artifact integrity mismatch"):
        pipeline._read_chunk_marker(out_dir, expected_job_hash="a" * 64)


def test_single_worker_constructs_and_closes_one_backend_per_chunk(
    monkeypatch: Any, tmp_path: Path
) -> None:
    constructed = 0
    closed = 0
    active = 0
    maximum_active = 0

    class FakeBackend:
        def __init__(self, settings: Any) -> None:
            nonlocal constructed, active, maximum_active
            constructed += 1
            active += 1
            maximum_active = max(maximum_active, active)

        def close(self) -> None:
            nonlocal closed, active
            closed += 1
            active -= 1

    def fake_extract(backend: Any, rows: list[dict[str, Any]], **kwargs: Any) -> ExtractStats:
        return _terminal_stats(len(rows))

    monkeypatch.setattr(pipeline, "LeanInteractBackend", FakeBackend)
    monkeypatch.setattr(pipeline, "extract_sft_classic_rows", fake_extract)
    monkeypatch.setattr(pipeline, "merge_extraction_partitions", lambda *args, **kwargs: None)
    kwargs = {
        "project_dir": tmp_path,
        "context_fingerprint": "a" * 64,
        "context_id": "ctx:" + "a" * 64,
        "raw_response_dir": tmp_path / "raw",
        "rows": [{"row": index} for index in range(5)],
        "source_row_indices": list(range(5)),
        "split": "train",
        "row_offset": 0,
        "out_dir": tmp_path / "out",
        "workers": 1,
        "chunk_size": 2,
        "run_id": "run:lifecycle",
        "memory_hard_limit_mb": None,
        "resume_work_dir": tmp_path / "work-lifecycle",
        "code_tree_hash": "b" * 64,
        "code_bundle_hash": "c" * 64,
    }

    pipeline._extract_sft_parallel(**kwargs)
    assert (constructed, closed, active, maximum_active) == (3, 3, 0, 1)

    pipeline._extract_sft_parallel(**kwargs)
    assert (constructed, closed, active, maximum_active) == (3, 3, 0, 1)


def test_representation_worker_reuses_only_the_common_incremental_prefix(
    monkeypatch: Any, tmp_path: Path
) -> None:
    from leanfaith.representations import RepresentationBatch, RepresentationBatchResult

    observed_settings: list[Any] = []

    class FakeBackend:
        def __init__(self, settings: Any) -> None:
            observed_settings.append(settings)

        def close(self) -> None:
            pass

    monkeypatch.setattr(pipeline, "LeanInteractBackend", FakeBackend)
    monkeypatch.setattr(
        pipeline,
        "build_representation_batch",
        lambda backend, batch, created_at: RepresentationBatchResult((), ()),
    )
    monkeypatch.setattr(pipeline, "_write_representation_partition", lambda *args, **kwargs: {})
    monkeypatch.setattr(pipeline, "_write_chunk_marker", lambda *args, **kwargs: None)

    pipeline._represent_chunk(
        project_dir=tmp_path,
        context_fingerprint="a" * 64,
        raw_response_dir=tmp_path / "raw",
        batch=RepresentationBatch("ctx:" + "a" * 64, "import Mathlib", ()),
        created_at=datetime.datetime.now(tz=datetime.UTC),
        out_dir=tmp_path / "out",
        source="fixture",
        memory_hard_limit_mb=None,
        job_hash="b" * 64,
    )

    assert len(observed_settings) == 1
    assert observed_settings[0].enable_incremental_optimization is True
