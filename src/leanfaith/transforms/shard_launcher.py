"""Concurrent, resumable orchestration for deterministic scale shards.

The launcher deliberately operates one level above the deterministic
materializer.  Every child remains an ordinary
``generate-deterministic --materialize-scale`` process with its own output
directory and LeanInteract lifecycle; this module only bounds how many of
those independent shard processes run at once.
"""

from __future__ import annotations

import datetime
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Literal, Protocol

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.transforms.scale_materializer import DeterministicScaleManifest


class DeterministicShardLaunchError(RuntimeError):
    """Raised when concurrent shard execution cannot proceed safely."""


ShardAttemptOutcome = Literal["running", "succeeded", "failed"]


class DeterministicShardAttempt(StrictModel):
    """One durable child-process attempt for a shard."""

    attempt_number: int = Field(ge=1)
    resumed: bool
    command: tuple[str, ...]
    started_at: datetime.datetime
    finished_at: datetime.datetime | None = None
    exit_code: int | None = None
    outcome: ShardAttemptOutcome
    failure_message: str | None = None

    @model_validator(mode="after")
    def _terminal_fields_match_outcome(self) -> DeterministicShardAttempt:
        if self.outcome == "running":
            if self.finished_at is not None or self.exit_code is not None:
                raise ValueError("running shard attempt cannot have terminal fields")
            if self.failure_message is not None:
                raise ValueError("running shard attempt cannot have a failure message")
        else:
            if self.finished_at is None or self.exit_code is None:
                raise ValueError("terminal shard attempt requires finish time and exit code")
            if self.outcome == "succeeded" and self.exit_code != 0:
                raise ValueError("successful shard attempt must exit zero")
            if self.outcome == "succeeded" and self.failure_message is not None:
                raise ValueError("successful shard attempt cannot have a failure message")
            if self.outcome == "failed" and self.failure_message is None:
                raise ValueError("failed shard attempt requires a failure message")
        return self


class DeterministicShardStatus(StrictModel):
    """Mutable-by-replacement operational status for one immutable shard command."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_shard_orchestration_status"] = (
        "deterministic_shard_orchestration_status"
    )
    shard_index: int = Field(ge=0)
    shard_count: int = Field(ge=1)
    command_identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_dir: str
    log_path: str
    attempts: tuple[DeterministicShardAttempt, ...] = ()

    @model_validator(mode="after")
    def _shard_index_is_in_range(self) -> DeterministicShardStatus:
        if self.shard_index >= self.shard_count:
            raise ValueError("status shard_index must be smaller than shard_count")
        expected = tuple(range(1, len(self.attempts) + 1))
        observed = tuple(attempt.attempt_number for attempt in self.attempts)
        if observed != expected:
            raise ValueError("shard attempt numbers must be contiguous")
        return self


class DeterministicShardLaunchSummary(StrictModel):
    """Terminal result of one selected concurrent-launch invocation."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_shard_launch_summary"] = (
        "deterministic_shard_launch_summary"
    )
    shard_count: int = Field(ge=1)
    selected_shard_indices: tuple[int, ...]
    max_parallel: int = Field(ge=1)
    outcome_counts: dict[str, int]
    succeeded_shards: tuple[int, ...]
    skipped_complete_shards: tuple[int, ...]
    failed_shards: tuple[int, ...]
    status_paths: dict[str, str]
    completed_at: datetime.datetime

    @property
    def ok(self) -> bool:
        return not self.failed_shards


class ShardProcessExecutor(Protocol):
    """Injectable process boundary used by production and mocked unit tests."""

    def execute(
        self,
        *,
        shard_index: int,
        command: Sequence[str],
        cwd: Path,
        log_path: Path,
    ) -> int: ...

    def terminate_all(self) -> None: ...


class SubprocessShardExecutor:
    """Run child shards without a shell and terminate whole process groups on interrupt."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[bytes]] = {}

    def execute(
        self,
        *,
        shard_index: int,
        command: Sequence[str],
        cwd: Path,
        log_path: Path,
    ) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = _utcnow().isoformat()
        with log_path.open("ab", buffering=0) as log:
            log.write(
                (f"\n=== LeanFaith shard {shard_index} attempt started {started} ===\n").encode()
            )
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=_child_environment(cwd),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=True,
            )
            with self._lock:
                if shard_index in self._processes:
                    _terminate_process_group(process)
                    raise DeterministicShardLaunchError(
                        f"shard {shard_index} already has a live child process"
                    )
                self._processes[shard_index] = process
            exit_code = -1
            try:
                exit_code = process.wait()
            finally:
                with self._lock:
                    self._processes.pop(shard_index, None)
            log.write(
                (
                    f"=== LeanFaith shard {shard_index} attempt finished "
                    f"{_utcnow().isoformat()} exit_code={exit_code} ===\n"
                ).encode()
            )
        return exit_code

    def terminate_all(self) -> None:
        with self._lock:
            processes = tuple(self._processes.values())
        for process in processes:
            _terminate_process_group(process)


class _PreparedShard(StrictModel):
    shard_index: int = Field(ge=0)
    output_dir: str
    log_path: str
    status_path: str
    base_command: tuple[str, ...]
    command_identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    resume: bool


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _child_environment(repo_root: Path) -> dict[str, str]:
    """Load child code from the requested repository, not the launcher's checkout.

    This matters when an immutable shard set is continued from a preserved
    verification checkout.  The interpreter may belong to the active virtual
    environment, but ``leanfaith`` itself must come from ``repo_root/src`` so
    the materializer's code-state binding describes the code actually run.
    """

    source_root = repo_root.resolve() / "src"
    if not source_root.is_dir():
        raise DeterministicShardLaunchError(
            f"child repository has no src package directory: {source_root}"
        )
    environment = dict(os.environ)
    prior = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root) if not prior else os.pathsep.join((str(source_root), prior))
    )
    return environment


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def _canonical_model_bytes(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _write_status(path: Path, model: StrictModel) -> None:
    """Atomically replace an operational status document, rejecting symlinks."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise DeterministicShardLaunchError(f"status path is not a regular file: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_model_bytes(model))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_status(path: Path) -> DeterministicShardStatus | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise DeterministicShardLaunchError(f"status path is not a regular file: {path}")
    try:
        return DeterministicShardStatus.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise DeterministicShardLaunchError(f"invalid shard status: {path}: {exc}") from exc


def _validate_complete_manifest(
    output_dir: Path,
    *,
    shard_count: int,
    shard_index: int,
) -> bool:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return False
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DeterministicShardLaunchError(
            f"shard manifest is not a regular file: {manifest_path}"
        )
    try:
        manifest = DeterministicScaleManifest.model_validate_json(manifest_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise DeterministicShardLaunchError(
            f"invalid completed shard manifest: {manifest_path}: {exc}"
        ) from exc
    if manifest.shard_count != shard_count or manifest.shard_index != shard_index:
        raise DeterministicShardLaunchError(
            f"shard manifest identity differs from launcher selection: {manifest_path}"
        )
    return True


def _has_materializer_state(output_dir: Path) -> bool:
    if not output_dir.exists():
        return False
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise DeterministicShardLaunchError(
            f"shard output path is not a real directory: {output_dir}"
        )
    return any(path.name != "run.lock" for path in output_dir.iterdir())


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_output_dirs(
    output_dirs: Mapping[int, Path],
    *,
    protected_paths: Sequence[Path],
) -> None:
    resolved = {index: path.resolve() for index, path in output_dirs.items()}
    ordered = sorted(resolved.items())
    for position, (left_index, left) in enumerate(ordered):
        if left.exists() and left.is_symlink():
            raise DeterministicShardLaunchError(
                f"shard {left_index} output directory cannot be a symlink: {left}"
            )
        for right_index, right in ordered[position + 1 :]:
            if _paths_overlap(left, right):
                raise DeterministicShardLaunchError(
                    "selected shard output directories overlap: "
                    f"shard {left_index}={left}, shard {right_index}={right}"
                )
        for protected in protected_paths:
            if _paths_overlap(left, protected.resolve()):
                raise DeterministicShardLaunchError(
                    f"shard {left_index} output directory overlaps an input/project path: "
                    f"{left} versus {protected.resolve()}"
                )


@contextmanager
def _launcher_lock(orchestration_dir: Path) -> Iterator[IO[bytes]]:
    orchestration_dir.mkdir(parents=True, exist_ok=True)
    lock_path = orchestration_dir / "run.lock"
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeterministicShardLaunchError(f"another shard launcher owns {lock_path}") from exc
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _base_child_command(
    *,
    paths: RepoPaths,
    theorem_jsonl: Path,
    representation_jsonl: Path,
    source_inventory_manifest: Path,
    project_dir: Path,
    output_dir: Path,
    scale_config: Path | None,
    max_sources: int | None,
    shard_count: int,
    shard_index: int,
    memory_hard_limit_mb: int | None,
    python_executable: str,
) -> tuple[str, ...]:
    command = [
        python_executable,
        "-m",
        "leanfaith.cli.app",
        "generate-deterministic",
        "--materialize-scale",
        "--root",
        str(paths.root.resolve()),
        "--theorems",
        str(theorem_jsonl.resolve()),
        "--representations",
        str(representation_jsonl.resolve()),
        "--source-inventory-manifest",
        str(source_inventory_manifest.resolve()),
        "--project-dir",
        str(project_dir.resolve()),
        "--output-dir",
        str(output_dir.resolve()),
        "--shard-count",
        str(shard_count),
        "--shard-index",
        str(shard_index),
    ]
    if scale_config is not None:
        command.extend(("--scale-config", str(scale_config.resolve())))
    if max_sources is not None:
        command.extend(("--max-sources", str(max_sources)))
    if memory_hard_limit_mb is not None:
        command.extend(("--memory-hard-limit-mb", str(memory_hard_limit_mb)))
    return tuple(command)


def _prepare_shards(
    *,
    paths: RepoPaths,
    theorem_jsonl: Path,
    representation_jsonl: Path,
    source_inventory_manifest: Path,
    project_dir: Path,
    output_root: Path,
    scale_config: Path | None,
    max_sources: int | None,
    shard_count: int,
    shard_indices: Sequence[int],
    resume_incomplete: bool,
    memory_hard_limit_mb: int | None,
    python_executable: str,
) -> tuple[tuple[_PreparedShard, ...], tuple[int, ...]]:
    width = max(2, len(str(shard_count - 1)))
    output_dirs = {
        index: output_root.resolve() / f"shard_{index:0{width}d}" for index in shard_indices
    }
    _validate_output_dirs(
        output_dirs,
        protected_paths=(
            theorem_jsonl,
            representation_jsonl,
            source_inventory_manifest,
            project_dir,
        ),
    )
    orchestration_dir = output_root.resolve() / "orchestration"
    prepared: list[_PreparedShard] = []
    skipped: list[int] = []
    for shard_index in shard_indices:
        output_dir = output_dirs[shard_index]
        log_path = orchestration_dir / "logs" / f"shard_{shard_index:0{width}d}.log"
        status_path = orchestration_dir / "status" / f"shard_{shard_index:0{width}d}.json"
        base_command = _base_child_command(
            paths=paths,
            theorem_jsonl=theorem_jsonl,
            representation_jsonl=representation_jsonl,
            source_inventory_manifest=source_inventory_manifest,
            project_dir=project_dir,
            output_dir=output_dir,
            scale_config=scale_config,
            max_sources=max_sources,
            shard_count=shard_count,
            shard_index=shard_index,
            memory_hard_limit_mb=memory_hard_limit_mb,
            python_executable=python_executable,
        )
        identity_hash = hash_canonical({"command_without_resume": base_command})
        prior = _load_status(status_path)
        if prior is not None and (
            prior.shard_index != shard_index
            or prior.shard_count != shard_count
            or prior.command_identity_hash != identity_hash
            or Path(prior.output_dir).resolve() != output_dir
            or Path(prior.log_path).resolve() != log_path.resolve()
        ):
            raise DeterministicShardLaunchError(
                f"existing shard status is bound to another command: {status_path}"
            )
        complete = _validate_complete_manifest(
            output_dir,
            shard_count=shard_count,
            shard_index=shard_index,
        )
        prior_succeeded = bool(
            prior is not None and prior.attempts and prior.attempts[-1].outcome == "succeeded"
        )
        if complete and prior_succeeded:
            skipped.append(shard_index)
            continue
        has_state = _has_materializer_state(output_dir)
        if (has_state or prior is not None) and not resume_incomplete:
            raise DeterministicShardLaunchError(
                f"shard {shard_index} has incomplete/prior state; pass --resume-incomplete: "
                f"{output_dir}"
            )
        prepared.append(
            _PreparedShard(
                shard_index=shard_index,
                output_dir=str(output_dir),
                log_path=str(log_path.resolve()),
                status_path=str(status_path.resolve()),
                base_command=base_command,
                command_identity_hash=identity_hash,
                resume=has_state,
            )
        )
    return tuple(prepared), tuple(skipped)


def _run_prepared_shard(
    prepared: _PreparedShard,
    *,
    shard_count: int,
    executor: ShardProcessExecutor,
    cwd: Path,
) -> tuple[int, Literal["succeeded", "failed"]]:
    status_path = Path(prepared.status_path)
    prior = _load_status(status_path)
    attempts = () if prior is None else prior.attempts
    command = prepared.base_command + (("--resume",) if prepared.resume else ())
    running_attempt = DeterministicShardAttempt(
        attempt_number=len(attempts) + 1,
        resumed=prepared.resume,
        command=command,
        started_at=_utcnow(),
        outcome="running",
    )
    running_status = DeterministicShardStatus(
        shard_index=prepared.shard_index,
        shard_count=shard_count,
        command_identity_hash=prepared.command_identity_hash,
        output_dir=prepared.output_dir,
        log_path=prepared.log_path,
        attempts=(*attempts, running_attempt),
    )
    _write_status(status_path, running_status)
    exit_code = -1
    failure_message: str | None = None
    try:
        exit_code = executor.execute(
            shard_index=prepared.shard_index,
            command=command,
            cwd=cwd,
            log_path=Path(prepared.log_path),
        )
        if exit_code != 0:
            failure_message = f"child process exited with code {exit_code}"
        elif not _validate_complete_manifest(
            Path(prepared.output_dir),
            shard_count=shard_count,
            shard_index=prepared.shard_index,
        ):
            failure_message = "child exited zero without a completed manifest"
    except Exception as exc:  # status must preserve orchestration failures
        failure_message = f"launcher/process error: {type(exc).__name__}: {exc}"
    outcome: Literal["succeeded", "failed"] = "succeeded" if failure_message is None else "failed"
    terminal_attempt = running_attempt.model_copy(
        update={
            "finished_at": _utcnow(),
            "exit_code": exit_code,
            "outcome": outcome,
            "failure_message": failure_message,
        }
    )
    terminal_status = running_status.model_copy(update={"attempts": (*attempts, terminal_attempt)})
    # Revalidate copies because Pydantic does not validate model_copy updates.
    terminal_status = DeterministicShardStatus.model_validate(
        terminal_status.model_dump(mode="python")
    )
    _write_status(status_path, terminal_status)
    return prepared.shard_index, outcome


def run_deterministic_shards(
    *,
    paths: RepoPaths,
    theorem_jsonl: Path,
    representation_jsonl: Path,
    source_inventory_manifest: Path,
    project_dir: Path,
    output_root: Path,
    shard_count: int,
    shard_indices: Sequence[int] | None = None,
    max_parallel: int = 1,
    resume_incomplete: bool = False,
    scale_config: Path | None = None,
    max_sources: int | None = None,
    memory_hard_limit_mb: int | None = None,
    process_executor: ShardProcessExecutor | None = None,
    python_executable: str = sys.executable,
) -> DeterministicShardLaunchSummary:
    """Run selected independent materializer shards with bounded concurrency."""

    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    if max_parallel < 1:
        raise ValueError("max_parallel must be positive")
    if max_sources is not None and max_sources < 1:
        raise ValueError("max_sources must be positive")
    if memory_hard_limit_mb is not None and memory_hard_limit_mb < 256:
        raise ValueError("memory_hard_limit_mb must be at least 256")
    selected = tuple(range(shard_count)) if shard_indices is None else tuple(shard_indices)
    if not selected:
        raise ValueError("at least one shard index must be selected")
    if len(set(selected)) != len(selected):
        raise ValueError("selected shard indices must be unique")
    if any(index < 0 or index >= shard_count for index in selected):
        raise ValueError("selected shard index is outside shard_count")
    selected = tuple(sorted(selected))
    for label, path, expected in (
        ("theorem JSONL", theorem_jsonl, "file"),
        ("representation JSONL", representation_jsonl, "file"),
        ("source inventory manifest", source_inventory_manifest, "file"),
        ("project directory", project_dir, "directory"),
    ):
        if expected == "file" and (not path.is_file() or path.is_symlink()):
            raise DeterministicShardLaunchError(f"{label} is not a regular file: {path}")
        if expected == "directory" and (not path.is_dir() or path.is_symlink()):
            raise DeterministicShardLaunchError(f"{label} is not a real directory: {path}")
    if scale_config is not None and (not scale_config.is_file() or scale_config.is_symlink()):
        raise DeterministicShardLaunchError(f"scale config is not a regular file: {scale_config}")

    output_root = output_root.resolve()
    orchestration_dir = output_root / "orchestration"
    executor = process_executor or SubprocessShardExecutor()
    with _launcher_lock(orchestration_dir):
        prepared, skipped = _prepare_shards(
            paths=paths,
            theorem_jsonl=theorem_jsonl,
            representation_jsonl=representation_jsonl,
            source_inventory_manifest=source_inventory_manifest,
            project_dir=project_dir,
            output_root=output_root,
            scale_config=scale_config,
            max_sources=max_sources,
            shard_count=shard_count,
            shard_indices=selected,
            resume_incomplete=resume_incomplete,
            memory_hard_limit_mb=memory_hard_limit_mb,
            python_executable=python_executable,
        )
        results: list[tuple[int, Literal["succeeded", "failed"]]] = []
        futures: list[Future[tuple[int, Literal["succeeded", "failed"]]]] = []
        pool = ThreadPoolExecutor(
            max_workers=min(max_parallel, max(1, len(prepared))),
            thread_name_prefix="deterministic-shard",
        )
        try:
            futures = [
                pool.submit(
                    _run_prepared_shard,
                    item,
                    shard_count=shard_count,
                    executor=executor,
                    cwd=paths.root.resolve(),
                )
                for item in prepared
            ]
            for future in as_completed(futures):
                results.append(future.result())
        except KeyboardInterrupt as exc:
            executor.terminate_all()
            for future in futures:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
            raise DeterministicShardLaunchError(
                "shard launcher interrupted; rerun with --resume-incomplete"
            ) from exc
        else:
            pool.shutdown(wait=True)

        succeeded = tuple(sorted(index for index, outcome in results if outcome == "succeeded"))
        failed = tuple(sorted(index for index, outcome in results if outcome == "failed"))
        counts: Counter[str] = Counter(outcome for _, outcome in results)
        counts["skipped_complete"] = len(skipped)
        status_paths = {
            str(index): str(
                orchestration_dir
                / "status"
                / f"shard_{index:0{max(2, len(str(shard_count - 1)))}d}.json"
            )
            for index in selected
        }
        summary = DeterministicShardLaunchSummary(
            shard_count=shard_count,
            selected_shard_indices=selected,
            max_parallel=max_parallel,
            outcome_counts=dict(sorted(counts.items())),
            succeeded_shards=succeeded,
            skipped_complete_shards=tuple(sorted(skipped)),
            failed_shards=failed,
            status_paths=status_paths,
            completed_at=_utcnow(),
        )
        _write_status(orchestration_dir / "latest_summary.json", summary)
        return summary


__all__ = [
    "DeterministicShardAttempt",
    "DeterministicShardLaunchError",
    "DeterministicShardLaunchSummary",
    "DeterministicShardStatus",
    "ShardProcessExecutor",
    "SubprocessShardExecutor",
    "run_deterministic_shards",
]
