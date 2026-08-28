from __future__ import annotations

import datetime
import fcntl
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.config.paths import RepoPaths
from leanfaith.transforms.scale_materializer import DeterministicScaleManifest
from leanfaith.transforms.scale_merge import DeterministicScaleMergeArtifacts
from leanfaith.transforms.shard_merge import (
    DeterministicShardMergeError,
    discover_completed_deterministic_shards,
    merge_deterministic_shard_run,
)


def _manifest(*, shard_count: int, shard_index: int, lineage: str = "c") -> bytes:
    manifest = DeterministicScaleManifest(
        run_spec_hash=f"{shard_index + 1:x}" * 64,
        run_spec_sha256="b" * 64,
        shard_set_spec_hash=lineage * 64,
        shard_count=shard_count,
        shard_index=shard_index,
        source_universe_count=shard_count,
        source_assignment_sha256="d" * 64,
        source_count=1,
        eligible_source_count=1,
        ineligible_source_count=0,
        journal_shard_count=1,
        rule_status_counts={"accepted": 1},
        family_accepted_counts={"p01_alpha": 1},
        record_counts={"pairs": 1},
        partition_sha256={"pairs": "e" * 64},
        journal_tree_hash="f" * 64,
        journal_receipt_count=1,
        journal_receipt_tree_hash="1" * 64,
        journal_chain_tip="2" * 64,
        raw_response_file_count=1,
        raw_response_tree_hash="3" * 64,
        created_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.UTC),
    )
    return canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"


def _shard(
    output_root: Path,
    *,
    shard_count: int,
    shard_index: int,
    lineage: str = "c",
) -> Path:
    width = max(2, len(str(shard_count - 1)))
    output = output_root / f"shard_{shard_index:0{width}d}"
    output.mkdir(parents=True)
    (output / "manifest.json").write_bytes(
        _manifest(
            shard_count=shard_count,
            shard_index=shard_index,
            lineage=lineage,
        )
    )
    return output


def test_discovery_returns_complete_set_in_index_order(tmp_path: Path) -> None:
    output_root = tmp_path / "run"
    expected = [_shard(output_root, shard_count=3, shard_index=index) for index in (2, 0, 1)]
    (output_root / "orchestration").mkdir()
    (output_root / "merged").mkdir()

    discovered = discover_completed_deterministic_shards(
        output_root,
        expected_shard_count=3,
    )

    assert discovered == tuple(sorted(expected))


def test_discovery_rejects_incomplete_and_mixed_lineage_sets(tmp_path: Path) -> None:
    incomplete = tmp_path / "incomplete"
    _shard(incomplete, shard_count=3, shard_index=0)
    _shard(incomplete, shard_count=3, shard_index=2)
    with pytest.raises(DeterministicShardMergeError, match=r"missing=\[1\]"):
        discover_completed_deterministic_shards(incomplete)

    mixed = tmp_path / "mixed"
    _shard(mixed, shard_count=2, shard_index=0, lineage="c")
    _shard(mixed, shard_count=2, shard_index=1, lineage="d")
    with pytest.raises(DeterministicShardMergeError, match="mixed deterministic shard lineage"):
        discover_completed_deterministic_shards(mixed)


def test_discovery_rejects_noncanonical_or_incomplete_producer_names(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "run"
    _shard(output_root, shard_count=1, shard_index=0)
    (output_root / "shard_partial").mkdir()
    with pytest.raises(DeterministicShardMergeError, match="malformed shard directory"):
        discover_completed_deterministic_shards(output_root)

    (output_root / "shard_partial").rmdir()
    canonical = output_root / "shard_00"
    noncanonical = output_root / "shard_0"
    canonical.rename(noncanonical)
    with pytest.raises(DeterministicShardMergeError, match="noncanonical shard directory"):
        discover_completed_deterministic_shards(output_root)


def test_discovery_rejects_noncanonical_manifest_bytes(tmp_path: Path) -> None:
    output_root = tmp_path / "run"
    shard = _shard(output_root, shard_count=1, shard_index=0)
    manifest = shard / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")

    with pytest.raises(DeterministicShardMergeError, match="not canonical JSON"):
        discover_completed_deterministic_shards(output_root)


def test_merge_holds_launcher_lock_and_forwards_canonical_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import shard_merge

    output_root = tmp_path / "run"
    expected = [_shard(output_root, shard_count=2, shard_index=index) for index in (1, 0)]
    seen: dict[str, object] = {}

    def fake_merge(**kwargs: object) -> DeterministicScaleMergeArtifacts:
        seen.update(kwargs)
        lock_path = output_root / "orchestration/run.lock"
        with (
            lock_path.open("a+b") as contender,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        output = tmp_path / "merged"
        return DeterministicScaleMergeArtifacts(
            output_dir=output,
            manifest_path=output / f"merged_manifest.{'a' * 64}.json",
            manifest_sha256="b" * 64,
            merged_manifest_hash="a" * 64,
            partition_paths={},
        )

    monkeypatch.setattr(shard_merge, "merge_deterministic_scale_shards", fake_merge)
    paths = RepoPaths(root=tmp_path)
    result = merge_deterministic_shard_run(
        paths=paths,
        output_root=output_root,
        output_dir=tmp_path / "merged",
        expected_shard_count=2,
    )

    assert result.merged_manifest_hash == "a" * 64
    assert seen["paths"] == paths
    assert seen["shard_output_dirs"] == tuple(sorted(expected))
    assert seen["output_dir"] == tmp_path / "merged"


def test_merge_refuses_active_launcher(tmp_path: Path) -> None:
    output_root = tmp_path / "run"
    _shard(output_root, shard_count=1, shard_index=0)
    orchestration = output_root / "orchestration"
    orchestration.mkdir()
    lock_path = orchestration / "run.lock"
    with lock_path.open("a+b") as launcher:
        fcntl.flock(launcher.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DeterministicShardMergeError, match="still active"):
            merge_deterministic_shard_run(
                paths=RepoPaths(root=tmp_path),
                output_root=output_root,
                output_dir=tmp_path / "merged",
            )


def test_shard_run_merge_cli_discovers_without_manual_directory_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import shard_merge

    seen: dict[str, object] = {}

    def fake_merge(**kwargs: object) -> DeterministicScaleMergeArtifacts:
        seen.update(kwargs)
        output = tmp_path / "merged"
        return DeterministicScaleMergeArtifacts(
            output_dir=output,
            manifest_path=output / f"merged_manifest.{'a' * 64}.json",
            manifest_sha256="b" * 64,
            merged_manifest_hash="a" * 64,
            partition_paths={},
        )

    monkeypatch.setattr(shard_merge, "merge_deterministic_shard_run", fake_merge)
    result = CliRunner().invoke(
        app,
        [
            "merge-deterministic-shards",
            "--root",
            str(tmp_path),
            "--output-root",
            str(tmp_path / "run"),
            "--output-dir",
            str(tmp_path / "merged"),
            "--expected-shard-count",
            "16",
        ],
    )

    assert result.exit_code == 0
    assert seen["output_root"] == tmp_path / "run"
    assert seen["output_dir"] == tmp_path / "merged"
    assert seen["expected_shard_count"] == 16
    assert "training_eligible=false" in result.output
