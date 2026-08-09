"""Regression tests for bounded single-worker resumable extraction."""

from __future__ import annotations

import datetime
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanfaith.cli import pipeline
from leanfaith.config.paths import RepoPaths
from leanfaith.lean.extract_run import ExtractStats


@pytest.fixture(autouse=True)
def _stub_scale_environment_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests must not build a real Lake project."""

    monkeypatch.setattr(pipeline, "_prepare_scale_lean_environment", lambda **_kwargs: None)


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
        assert job["environment_is_prepared"] is True
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
        assert job["environment_is_prepared"] is True
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


def test_scale_preflight_runs_once_and_chunks_skip_redundant_builds(
    monkeypatch: Any, tmp_path: Path
) -> None:
    preflights: list[dict[str, Any]] = []
    chunk_build_modes: list[bool] = []

    monkeypatch.setattr(
        pipeline,
        "_prepare_scale_lean_environment",
        lambda **kwargs: preflights.append(kwargs),
    )

    def fake_chunk(**job: Any) -> ExtractStats:
        chunk_build_modes.append(bool(job["environment_is_prepared"]))
        stats = _terminal_stats(len(job["rel_paths"]))
        pipeline._write_chunk_marker(
            Path(job["out_dir"]),
            job_hash=str(job["job_hash"]),
            payload=stats.as_dict(),
        )
        return stats

    monkeypatch.setattr(pipeline, "_extract_mathlib_chunk", fake_chunk)
    monkeypatch.setattr(pipeline, "merge_extraction_partitions", lambda *args, **kwargs: None)
    kwargs = {
        "project_dir": tmp_path,
        "context_fingerprint": "a" * 64,
        "context_id": "ctx:" + "a" * 64,
        "raw_response_dir": tmp_path / "raw",
        "rel_paths": ["Mathlib/A.lean", "Mathlib/B.lean", "Mathlib/C.lean"],
        "source_revision": "revision",
        "out_dir": tmp_path / "out",
        "workers": 1,
        "chunk_size": 1,
        "run_id": "run:preflight",
        "memory_hard_limit_mb": None,
        "resume_work_dir": tmp_path / "work-preflight",
        "code_tree_hash": "b" * 64,
        "code_bundle_hash": "c" * 64,
    }

    pipeline._extract_mathlib_parallel(**kwargs)
    assert len(preflights) == 1
    assert chunk_build_modes == [True, True, True]

    pipeline._extract_mathlib_parallel(**kwargs)
    assert len(preflights) == 1
    assert chunk_build_modes == [True, True, True]


def test_scale_preflight_failure_starts_no_chunk(monkeypatch: Any, tmp_path: Path) -> None:
    chunk_calls = 0

    def fail_preflight(**_kwargs: Any) -> None:
        raise RuntimeError("preflight failed")

    def fake_chunk(**_job: Any) -> ExtractStats:
        nonlocal chunk_calls
        chunk_calls += 1
        return _terminal_stats(1)

    monkeypatch.setattr(pipeline, "_prepare_scale_lean_environment", fail_preflight)
    monkeypatch.setattr(pipeline, "_extract_sft_chunk", fake_chunk)
    with pytest.raises(RuntimeError, match="preflight failed"):
        pipeline._extract_sft_parallel(
            project_dir=tmp_path,
            context_fingerprint="a" * 64,
            context_id="ctx:" + "a" * 64,
            raw_response_dir=tmp_path / "raw",
            rows=[{"row": 0}],
            source_row_indices=[0],
            split="train",
            row_offset=0,
            out_dir=tmp_path / "out",
            workers=1,
            chunk_size=1,
            run_id="run:preflight-failure",
            memory_hard_limit_mb=None,
            resume_work_dir=tmp_path / "work-preflight-failure",
            code_tree_hash="b" * 64,
            code_bundle_hash="c" * 64,
        )
    assert chunk_calls == 0


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
    observed_settings: list[Any] = []

    class FakeBackend:
        def __init__(self, settings: Any) -> None:
            nonlocal constructed, active, maximum_active
            constructed += 1
            active += 1
            maximum_active = max(maximum_active, active)
            observed_settings.append(settings)

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
    assert len(observed_settings) == 3
    assert all(not settings.enable_incremental_optimization for settings in observed_settings)
    assert all(
        settings.method_version == pipeline.SFT_CLASSIC_METHOD_VERSION
        for settings in observed_settings
    )

    pipeline._extract_sft_parallel(**kwargs)
    assert (constructed, closed, active, maximum_active) == (3, 3, 0, 1)


def test_direct_sft_path_uses_stateless_backend_and_binds_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_settings: list[Any] = []
    observed_manifest: dict[str, Any] = {}
    context_id = "ctx:" + "a" * 64
    input_path = tmp_path / "sft.jsonl"
    input_path.write_text('{"uuid":"fixture"}\n', encoding="utf-8")

    class FakeBackend:
        def __init__(self, settings: Any) -> None:
            observed_settings.append(settings)

        def close(self) -> None:
            pass

    monkeypatch.setattr(pipeline, "LeanInteractBackend", FakeBackend)
    monkeypatch.setattr(
        pipeline,
        "build_mathlib_context",
        lambda paths, project_dir: (
            SimpleNamespace(context_id=context_id, context_fingerprint="a" * 64),
            "b" * 64,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "collect_code_state",
        lambda root: SimpleNamespace(code_tree_hash="c" * 64),
    )
    monkeypatch.setattr(pipeline, "hash_file", lambda path: "d" * 64)
    monkeypatch.setattr(
        pipeline,
        "extract_sft_classic_rows",
        lambda backend, rows, **kwargs: _terminal_stats(len(rows)),
    )

    def fake_manifest(stats: ExtractStats, **kwargs: Any) -> Path:
        observed_manifest.update(kwargs)
        return tmp_path / "manifest.json"

    monkeypatch.setattr(pipeline, "write_extraction_manifest", fake_manifest)

    manifest, stats = pipeline.run_extract(
        paths=RepoPaths(root=tmp_path),
        source="sft_classic",
        project_dir=tmp_path / "mathlib",
        input_path=input_path,
        out_dir=tmp_path / "out",
        limit=None,
        split="train",
        row_offset=0,
        workers=1,
        chunk_size=100,
    )

    assert manifest == tmp_path / "manifest.json"
    assert stats["sources_processed"] == 1
    assert len(observed_settings) == 1
    assert observed_settings[0].enable_incremental_optimization is False
    assert observed_settings[0].method_version == pipeline.SFT_CLASSIC_METHOD_VERSION
    config = observed_manifest["config_payload"]
    assert config["execution_isolation_policy"] == pipeline.SFT_CLASSIC_EXECUTION_POLICY
    assert config["lean_incremental_optimization"] is False
    assert config["lean_method_version"] == pipeline.SFT_CLASSIC_METHOD_VERSION
    assert config["leaninteract_environment_setup"] == (pipeline.DEFAULT_ENVIRONMENT_SETUP_VERSION)


@pytest.mark.parametrize(
    ("constant_name", "replacement"),
    [
        ("SFT_CLASSIC_EXECUTION_POLICY", "changed_isolation_policy_v2"),
        ("SFT_CLASSIC_METHOD_VERSION", "changed_lean_method_v2"),
    ],
)
def test_sft_resume_rejects_stateless_policy_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    constant_name: str,
    replacement: str,
) -> None:
    observed_job_payloads: list[dict[str, Any]] = []
    real_hash_canonical = pipeline.hash_canonical

    def capture_hash(payload: object) -> str:
        if isinstance(payload, dict) and payload.get("source") == "sft_classic":
            observed_job_payloads.append(dict(payload))
        return real_hash_canonical(payload)

    def fake_chunk(**job: Any) -> ExtractStats:
        stats = _terminal_stats(len(job["rows"]))
        pipeline._write_chunk_marker(
            Path(job["out_dir"]),
            job_hash=str(job["job_hash"]),
            payload=stats.as_dict(),
        )
        return stats

    monkeypatch.setattr(pipeline, "hash_canonical", capture_hash)
    monkeypatch.setattr(pipeline, "_extract_sft_chunk", fake_chunk)
    monkeypatch.setattr(pipeline, "merge_extraction_partitions", lambda *args, **kwargs: None)
    kwargs = {
        "project_dir": tmp_path,
        "context_fingerprint": "a" * 64,
        "context_id": "ctx:" + "a" * 64,
        "raw_response_dir": tmp_path / "raw",
        "rows": [{"row": 0}],
        "source_row_indices": [0],
        "split": "train",
        "row_offset": 0,
        "out_dir": tmp_path / "out",
        "workers": 1,
        "chunk_size": 1,
        "run_id": "run:stateless-identity",
        "memory_hard_limit_mb": None,
        "resume_work_dir": tmp_path / "work",
        "code_tree_hash": "b" * 64,
        "code_bundle_hash": "c" * 64,
    }

    pipeline._extract_sft_parallel(**kwargs)
    assert observed_job_payloads[-1]["execution_isolation_policy"] == (
        pipeline.SFT_CLASSIC_EXECUTION_POLICY
    )
    assert observed_job_payloads[-1]["lean_incremental_optimization"] is False
    assert observed_job_payloads[-1]["lean_method_version"] == (pipeline.SFT_CLASSIC_METHOD_VERSION)

    monkeypatch.setattr(pipeline, constant_name, replacement)
    with pytest.raises(ValueError, match="resume chunk job hash mismatch"):
        pipeline._extract_sft_parallel(**kwargs)


def test_run_extract_replays_exact_mathlib_file_frame(monkeypatch: Any, tmp_path: Path) -> None:
    observed: dict[str, Any] = {}
    context_id = "ctx:" + "a" * 64
    frame_path = tmp_path / "frame.json"
    frame_path.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(
        pipeline,
        "build_mathlib_context",
        lambda paths, project_dir: (
            SimpleNamespace(context_id=context_id, context_fingerprint="a" * 64),
            "b" * 64,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_mathlib_spec",
        lambda paths: SimpleNamespace(
            revision="c" * 40,
            root_module="Mathlib",
            globs=("Mathlib/**/*.lean",),
        ),
    )

    def fake_inventory(*args: Any, **kwargs: Any) -> object:
        observed["inventory_limit"] = kwargs.get("limit")
        return object()

    monkeypatch.setattr(pipeline, "build_inventory", fake_inventory)
    fake_frame = SimpleNamespace(
        frame_id="mathlib_file_frame_v1:" + "d" * 64,
        members=(
            SimpleNamespace(relative_path="Mathlib/Topology/B.lean"),
            SimpleNamespace(relative_path="Mathlib/Algebra/A.lean"),
        ),
        model_dump=lambda mode: {"frame_id": "mathlib_file_frame_v1:" + "d" * 64},
    )
    monkeypatch.setattr(
        pipeline,
        "load_and_verify_mathlib_file_frame",
        lambda *args, **kwargs: fake_frame,
    )
    monkeypatch.setattr(
        pipeline,
        "collect_code_state",
        lambda root: SimpleNamespace(code_tree_hash="e" * 64),
    )
    monkeypatch.setattr(pipeline, "hash_file", lambda path: "f" * 64)

    def fake_extract(**kwargs: Any) -> ExtractStats:
        observed["rel_paths"] = kwargs["rel_paths"]
        return _terminal_stats(len(kwargs["rel_paths"]))

    monkeypatch.setattr(pipeline, "_extract_mathlib_parallel", fake_extract)

    def fake_manifest(stats: ExtractStats, **kwargs: Any) -> Path:
        observed["manifest_kwargs"] = kwargs
        return tmp_path / "manifest.json"

    monkeypatch.setattr(pipeline, "write_extraction_manifest", fake_manifest)

    manifest, stats = pipeline.run_extract(
        paths=RepoPaths(root=tmp_path),
        source="mathlib",
        project_dir=tmp_path / "mathlib",
        input_path=None,
        out_dir=tmp_path / "out",
        limit=None,
        split="train",
        row_offset=0,
        workers=1,
        chunk_size=10,
        resume_work_dir=tmp_path / "work",
        mathlib_file_frame_path=frame_path,
        mathlib_frame_selection_seed="frame-seed",
    )

    assert manifest == tmp_path / "manifest.json"
    assert stats["sources_processed"] == 2
    assert observed["inventory_limit"] is None
    assert observed["rel_paths"] == [
        "Mathlib/Topology/B.lean",
        "Mathlib/Algebra/A.lean",
    ]
    manifest_kwargs = observed["manifest_kwargs"]
    assert frame_path in manifest_kwargs["input_paths"]
    assert manifest_kwargs["config_payload"]["mathlib_file_frame_id"] == (
        "mathlib_file_frame_v1:" + "d" * 64
    )
    assert manifest_kwargs["config_payload"]["mathlib_file_frame_sha256"] == (
        pipeline.sha256_hex(
            pipeline.canonical_json_bytes(fake_frame.model_dump(mode="json")) + b"\n"
        )
    )
    assert manifest_kwargs["config_payload"]["leaninteract_environment_setup"] == (
        pipeline.SCALE_ENVIRONMENT_SETUP_VERSION
    )


@pytest.mark.parametrize(
    ("source", "frame_path", "seed", "previous_frame_path", "limit", "message"),
    [
        ("mathlib", Path("frame.json"), None, None, None, "must be provided together"),
        ("mathlib", None, "seed", None, None, "must be provided together"),
        ("mathlib", Path("frame.json"), "seed", None, 1, "mutually exclusive"),
        (
            "mathlib",
            None,
            None,
            Path("previous.json"),
            None,
            "requires --mathlib-file-frame",
        ),
        (
            "sft_classic",
            Path("frame.json"),
            "seed",
            None,
            None,
            "require source=mathlib",
        ),
    ],
)
def test_run_extract_rejects_invalid_mathlib_frame_option_combinations(
    tmp_path: Path,
    source: str,
    frame_path: Path | None,
    seed: str | None,
    previous_frame_path: Path | None,
    limit: int | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        pipeline.run_extract(
            paths=RepoPaths(root=tmp_path),
            source=source,
            project_dir=tmp_path,
            input_path=None,
            out_dir=tmp_path / "out",
            limit=limit,
            split="train",
            row_offset=0,
            mathlib_file_frame_path=frame_path,
            mathlib_frame_selection_seed=seed,
            mathlib_previous_file_frame_path=previous_frame_path,
        )


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
        environment_is_prepared=True,
    )

    assert len(observed_settings) == 1
    assert observed_settings[0].enable_incremental_optimization is True
    assert observed_settings[0].environment_is_prepared is True
