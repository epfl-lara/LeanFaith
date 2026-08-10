from __future__ import annotations

import datetime
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.config.paths import RepoPaths
from leanfaith.transforms.scale_materializer import DeterministicScaleManifest
from leanfaith.transforms.shard_launcher import (
    DeterministicShardLaunchError,
    DeterministicShardLaunchSummary,
    DeterministicShardStatus,
    _child_environment,
    run_deterministic_shards,
)


def _inputs(tmp_path: Path) -> tuple[RepoPaths, Path, Path, Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    theorems = root / "theorems.jsonl"
    representations = root / "representations.jsonl"
    inventory = root / "inventory.json"
    project = root / "mathlib"
    theorems.write_text("{}\n", encoding="utf-8")
    representations.write_text("{}\n", encoding="utf-8")
    inventory.write_text("{}\n", encoding="utf-8")
    project.mkdir()
    return RepoPaths(root=root), theorems, representations, inventory, project


def _argument(command: tuple[str, ...], name: str) -> str:
    return command[command.index(name) + 1]


def _write_complete_manifest(command: tuple[str, ...]) -> None:
    output = Path(_argument(command, "--output-dir"))
    shard_count = int(_argument(command, "--shard-count"))
    shard_index = int(_argument(command, "--shard-index"))
    manifest = DeterministicScaleManifest(
        run_spec_hash="a" * 64,
        run_spec_sha256="b" * 64,
        shard_set_spec_hash="c" * 64,
        shard_count=shard_count,
        shard_index=shard_index,
        source_universe_count=1,
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
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_bytes(
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    )


class RecordingExecutor:
    def __init__(self, *, fail_once: set[int] | None = None) -> None:
        self.fail_once = set() if fail_once is None else set(fail_once)
        self.calls: list[tuple[int, tuple[str, ...], Path, Path]] = []
        self.maximum_active = 0
        self.terminated = False
        self._active = 0
        self._lock = threading.Lock()

    def execute(
        self,
        *,
        shard_index: int,
        command: tuple[str, ...],
        cwd: Path,
        log_path: Path,
    ) -> int:
        with self._lock:
            self.calls.append((shard_index, tuple(command), cwd, log_path))
            self._active += 1
            self.maximum_active = max(self.maximum_active, self._active)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"mock shard {shard_index}\n", encoding="utf-8")
            time.sleep(0.03)
            if shard_index in self.fail_once:
                self.fail_once.remove(shard_index)
                output = Path(_argument(tuple(command), "--output-dir"))
                (output / "journal").mkdir(parents=True, exist_ok=True)
                (output / "journal" / "partial.json").write_text("{}\n", encoding="utf-8")
                return 7
            _write_complete_manifest(tuple(command))
            return 0
        finally:
            with self._lock:
                self._active -= 1

    def terminate_all(self) -> None:
        self.terminated = True


def test_launcher_runs_selected_shards_with_bounded_parallelism(tmp_path: Path) -> None:
    paths, theorems, representations, inventory, project = _inputs(tmp_path)
    executor = RecordingExecutor()
    output_root = tmp_path / "outputs"

    summary = run_deterministic_shards(
        paths=paths,
        theorem_jsonl=theorems,
        representation_jsonl=representations,
        source_inventory_manifest=inventory,
        project_dir=project,
        output_root=output_root,
        shard_count=8,
        shard_indices=(1, 3, 5),
        max_parallel=2,
        process_executor=executor,
        python_executable="/python-test",
    )

    assert summary.ok
    assert summary.succeeded_shards == (1, 3, 5)
    assert summary.skipped_complete_shards == ()
    assert executor.maximum_active == 2
    # Worker start order is intentionally not part of the concurrency contract.
    assert sorted(call[0] for call in executor.calls) == [1, 3, 5]
    for shard_index, command, cwd, log_path in executor.calls:
        assert command[:4] == (
            "/python-test",
            "-m",
            "leanfaith.cli.app",
            "generate-deterministic",
        )
        assert "--materialize-scale" in command
        assert "--resume" not in command
        assert _argument(command, "--shard-index") == str(shard_index)
        assert cwd == paths.root.resolve()
        assert log_path.is_file()
        status_path = output_root / "orchestration/status" / f"shard_{shard_index:02d}.json"
        status = DeterministicShardStatus.model_validate_json(status_path.read_bytes())
        assert status.attempts[-1].outcome == "succeeded"
    assert (output_root / "orchestration/latest_summary.json").is_file()


def test_launcher_skips_complete_and_exact_resumes_incomplete_shard(tmp_path: Path) -> None:
    paths, theorems, representations, inventory, project = _inputs(tmp_path)
    output_root = tmp_path / "outputs"
    first_executor = RecordingExecutor(fail_once={1})
    first = run_deterministic_shards(
        paths=paths,
        theorem_jsonl=theorems,
        representation_jsonl=representations,
        source_inventory_manifest=inventory,
        project_dir=project,
        output_root=output_root,
        shard_count=2,
        max_parallel=2,
        process_executor=first_executor,
        python_executable="/python-test",
    )
    assert first.succeeded_shards == (0,)
    assert first.failed_shards == (1,)

    second_executor = RecordingExecutor()
    second = run_deterministic_shards(
        paths=paths,
        theorem_jsonl=theorems,
        representation_jsonl=representations,
        source_inventory_manifest=inventory,
        project_dir=project,
        output_root=output_root,
        shard_count=2,
        max_parallel=2,
        resume_incomplete=True,
        process_executor=second_executor,
        python_executable="/python-test",
    )

    assert second.ok
    assert second.skipped_complete_shards == (0,)
    assert second.succeeded_shards == (1,)
    assert len(second_executor.calls) == 1
    assert second_executor.calls[0][0] == 1
    assert second_executor.calls[0][1][-1] == "--resume"
    resumed_status = DeterministicShardStatus.model_validate_json(
        (output_root / "orchestration/status/shard_01.json").read_bytes()
    )
    assert len(resumed_status.attempts) == 2
    assert resumed_status.attempts[-1].resumed is True
    assert resumed_status.attempts[-1].outcome == "succeeded"


def test_launcher_refuses_incomplete_state_without_resume(tmp_path: Path) -> None:
    paths, theorems, representations, inventory, project = _inputs(tmp_path)
    output_root = tmp_path / "outputs"
    partial = output_root / "shard_00/journal"
    partial.mkdir(parents=True)
    (partial / "source.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(DeterministicShardLaunchError, match="--resume-incomplete"):
        run_deterministic_shards(
            paths=paths,
            theorem_jsonl=theorems,
            representation_jsonl=representations,
            source_inventory_manifest=inventory,
            project_dir=project,
            output_root=output_root,
            shard_count=2,
            shard_indices=(0,),
            process_executor=RecordingExecutor(),
        )


def test_child_environment_prefers_requested_checkout_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "preserved-checkout/src"
    source.mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", "/prior/source")

    environment = _child_environment(source.parent)

    assert environment["PYTHONPATH"].split(":") == [str(source), "/prior/source"]


def test_launcher_refuses_output_directories_resolving_to_same_location(
    tmp_path: Path,
) -> None:
    paths, theorems, representations, inventory, project = _inputs(tmp_path)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    shared = output_root / "shared"
    shared.mkdir()
    (output_root / "shard_00").symlink_to(shared, target_is_directory=True)
    (output_root / "shard_01").symlink_to(shared, target_is_directory=True)

    with pytest.raises(DeterministicShardLaunchError, match="overlap"):
        run_deterministic_shards(
            paths=paths,
            theorem_jsonl=theorems,
            representation_jsonl=representations,
            source_inventory_manifest=inventory,
            project_dir=project,
            output_root=output_root,
            shard_count=2,
            process_executor=RecordingExecutor(),
        )


def test_shard_launcher_cli_forwards_selection_and_parallelism(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import shard_launcher

    seen: dict[str, object] = {}

    def fake_run(**kwargs: object) -> DeterministicShardLaunchSummary:
        seen.update(kwargs)
        return DeterministicShardLaunchSummary(
            shard_count=4,
            selected_shard_indices=(1, 3),
            max_parallel=2,
            outcome_counts={"succeeded": 2, "skipped_complete": 0},
            succeeded_shards=(1, 3),
            skipped_complete_shards=(),
            failed_shards=(),
            status_paths={},
            completed_at=datetime.datetime(2026, 8, 10, tzinfo=datetime.UTC),
        )

    monkeypatch.setattr(shard_launcher, "run_deterministic_shards", fake_run)
    result = CliRunner().invoke(
        app,
        [
            "run-deterministic-shards",
            "--root",
            str(tmp_path),
            "--theorems",
            str(tmp_path / "theorems.jsonl"),
            "--representations",
            str(tmp_path / "representations.jsonl"),
            "--source-inventory-manifest",
            str(tmp_path / "inventory.json"),
            "--project-dir",
            str(tmp_path / "mathlib"),
            "--output-root",
            str(tmp_path / "outputs"),
            "--shard-count",
            "4",
            "--shard-index",
            "1",
            "--shard-index",
            "3",
            "--max-parallel",
            "2",
            "--resume-incomplete",
        ],
    )

    assert result.exit_code == 0
    assert seen["shard_indices"] == [1, 3]
    assert seen["max_parallel"] == 2
    assert seen["resume_incomplete"] is True
    assert "succeeded=2" in result.output
