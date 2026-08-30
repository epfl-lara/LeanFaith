from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from leanfaith.cpt2.scale import RELEASE_SCHEMA, ScaleSettings, run_scale
from leanfaith.cpt2.source import SourceSnapshot


def _source(tmp_path: Path) -> tuple[Path, SourceSnapshot]:
    path = tmp_path / "source.parquet"
    prefix_a = "theorem pair_a : True := "
    prefix_b = "theorem pair_b : True := "
    sources = [
        prefix_a + "by trivial\n",
        prefix_a + "by exact True.intro\n",
        prefix_b + "by trivial\n",
        prefix_b + "by exact True.intro\n",
        "#check True\n",
    ]
    table = pa.Table.from_arrays(
        [
            pa.array(sources, type=pa.large_string()),
            pa.array(["ok"] * len(sources), type=pa.large_string()),
            pa.array([True, False, True, False, False], type=pa.bool_()),
        ],
        names=["source_code", "validation", "isValid"],
    )
    pq.write_table(table, path, row_group_size=3)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = SourceSnapshot(
        repo_id="fixture/compiler_data",
        requested_revision="a" * 40,
        resolved_revision="a" * 40,
        parquet_path=path.name,
        parquet_sha256=digest,
        parquet_bytes=path.stat().st_size,
        row_count=len(sources),
        row_group_count=2,
        schema=(
            ("source_code", "large_string"),
            ("validation", "large_string"),
            ("isValid", "bool"),
        ),
    )
    return path, snapshot


def test_full_scale_is_minimal_group_disjoint_label_exact_and_resumable(tmp_path: Path) -> None:
    source_path, snapshot = _source(tmp_path)
    blocklist = tmp_path / "blocklist.json"
    blocklist.write_text(
        json.dumps({"version": ["golden_blocklist_v1"], "near_dup_hashes": []}),
        encoding="utf-8",
    )
    settings = ScaleSettings(
        output_root=tmp_path / "build",
        validation_rows=2,
        validation_true=1,
        validation_false=1,
        row_groups_per_release_shard=2,
        workers=1,
    )
    first = run_scale(
        snapshot=snapshot,
        source_path=source_path,
        settings=settings,
        blocklist_path=blocklist,
        task_code_sha256="b" * 64,
        code_revision="c" * 40,
    )
    assert first.input_rows == 5
    assert first.output_rows == 4
    assert first.train_rows == 2
    assert first.validation_rows == 2
    assert first.written_source_shards == 2
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_labels"] == {"false": 3, "true": 2}
    assert manifest["output_labels"] == {"false": 2, "true": 2}
    assert manifest["skips"] == {"unmatched_selected_splitter": 1}
    assert manifest["splitter"]["scale_lean_rows"] == 0
    assert manifest["training_started"] is False
    seen: dict[str, str] = {}
    for path in sorted(first.release_root.glob("*.parquet")):
        parquet = pq.ParquetFile(path)
        assert parquet.schema_arrow == RELEASE_SCHEMA
        split = "validation" if path.name.startswith("validation-") else "train"
        for row in parquet.read().to_pylist():
            assert tuple(row) == ("theorem", "body", "label")
            prior = seen.setdefault(row["theorem"], split)
            assert prior == split
    initial_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first.release_root.glob("*.parquet")
    }
    second = run_scale(
        snapshot=snapshot,
        source_path=source_path,
        settings=settings,
        blocklist_path=blocklist,
        task_code_sha256="b" * 64,
        code_revision="c" * 40,
    )
    assert second.written_source_shards == 0
    assert second.resumed_source_shards == 2
    assert second.written_release_shards == 0
    assert second.resumed_release_shards == 1
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in second.release_root.glob("*.parquet")
    } == initial_hashes
