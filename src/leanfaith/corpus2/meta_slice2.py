"""Reproducible, resumable public-mathlib yield probe for Meta-engine slice 2.

The production entry point selects the same frozen 500 public declarations,
runs contiguous 20-name Lean shards with deterministic size-scaled timeouts and
midpoint bisection, then independently audits emitted candidates in 100-item
shards.  Every attempt is immutable and content-bound beneath the run root;
only an atomic ``result.json`` marks a reusable attempt.  No row is sent to an
external service.

The verifier reconstructs both attempt trees and both aggregate streams.  It
also recomputes every source/candidate pretty-text SHA-256 in Python and
requires exactly one terminal disposition per requested declaration.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import statistics
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_file

METHOD_VERSION = "meta_engine_slice2_yield_probe_v4"
SELECTION_DOMAIN = "leanfaith_meta_slice2_yield_probe_v1"
SELECTION_PREFIX = b"leanfaith_meta_slice2_yield_probe_v1\0"

PRODUCTION_SAMPLE_SIZE = 500
PRODUCTION_TIMEOUT_SECONDS = 900
PRODUCTION_ADDRESS_SPACE_BYTES = 25_769_803_776
PRODUCTION_LEAN_MEMORY_MB = 24_576
PRODUCTION_SOURCE = "mathlib"
PRODUCTION_SOURCE_REVISION = "d568c8c09630de097a046763c17b9ea99f95f950"

PRODUCTION_THEOREM_STORE = Path(
    "/storage/milikic/leanfaith/immutable/extractions/"
    "mathlib_d568c8c_manifest_b1831204/theorems/mathlib.jsonl"
)
PRODUCTION_THEOREM_STORE_SHA256 = "7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7"
PRODUCTION_EXTRACTION_MANIFEST = Path(
    "/storage/milikic/leanfaith/immutable/extractions/"
    "mathlib_d568c8c_manifest_b1831204/manifests/mathlib.json"
)
PRODUCTION_EXTRACTION_MANIFEST_SHA256 = (
    "b183120468eb8f88f832d4336c206c14fb5f2a4fd3b9d968165228a6185bad06"
)
PRODUCTION_MATHLIB_PROJECT = Path("/storage/milikic/leanfaith/mathlib4")
PRODUCTION_STORAGE_ROOT = Path("/storage/milikic")

NAMES_FILENAME = "declaration_names.txt"
STDOUT_FILENAME = "lean.stdout.jsonl"
SUMMARY_FILENAME = "summary.json"
MANIFEST_FILENAME = "manifest.json"
AUDIT_STDOUT_FILENAME = "audit.stdout.jsonl"
SHARDS_DIRNAME = "shards"
RUN_LOCK_FILENAME = ".run.lock"
ATTEMPT_NAMES_FILENAME = "names.txt"
ATTEMPT_CERTIFICATES_FILENAME = "certificates.jsonl"
ATTEMPT_DRIVER_FILENAME = "driver.lean"
ATTEMPT_STDOUT_FILENAME = "stdout.jsonl"
ATTEMPT_STDERR_FILENAME = "stderr.txt"
ATTEMPT_LOG_FILENAME = "log.txt"
ATTEMPT_PROCESS_FILENAME = "process.json"
ATTEMPT_RESULT_FILENAME = "result.json"

ATTEMPT_SCHEMA_VERSION = 2
PRIMARY_SHARD_SIZE = 20
AUDIT_SHARD_SIZE = 100
PRIMARY_TIMEOUT_FLOOR_SECONDS = 180
AUDIT_TIMEOUT_FLOOR_SECONDS = 120
PRIMARY_TIMEOUT_SECONDS_PER_ITEM = 30
AUDIT_TIMEOUT_SECONDS_PER_ITEM = 5
ATTEMPT_TIMEOUT_FORMULA = "min(ceiling_seconds, max(floor_seconds, seconds_per_item * item_count))"

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL_STATUSES = frozenset({"complete", "sourceTextRejected", "notfound", "notProp", "error"})
_PROCESSED_TERMINAL_STATUSES = frozenset({"complete", "sourceTextRejected"})
_RUNNER_TERMINAL_STATUSES = frozenset({"externalTimeout", "externalProcessError"})
_RUNNER_STATUS_BY_REASON = {
    "timeout": "externalTimeout",
    "nonzero_exit": "externalProcessError",
    "invalid_output": "externalProcessError",
}
_RUNNER_REASON_CODES = frozenset(_RUNNER_STATUS_BY_REASON)
_ATTEMPT_DIRECTORY_PATTERN = re.compile(r"attempt-(primary|audit)-[0-9]{8}-[0-9]{8}-r[0-9]{3}\Z")
_ROOT_ARTIFACT_FILENAMES = frozenset(
    {NAMES_FILENAME, STDOUT_FILENAME, AUDIT_STDOUT_FILENAME, SUMMARY_FILENAME}
)
_COMMON_ATTEMPT_FILENAMES = frozenset(
    {
        ATTEMPT_DRIVER_FILENAME,
        ATTEMPT_STDOUT_FILENAME,
        ATTEMPT_STDERR_FILENAME,
        ATTEMPT_LOG_FILENAME,
        ATTEMPT_PROCESS_FILENAME,
        ATTEMPT_RESULT_FILENAME,
    }
)
_FAMILY_EVIDENCE_CLASS = {
    "P20": "P-DEF",
    "P21": "P-DEF",
    "P23": "P-SCHEMA",
    "P24": "P-SCHEMA",
}
_P21_OPERATIONS = frozenset({"betaIntroduce", "zetaIntroduce", "betaEliminate", "zetaEliminate"})


class MetaSlice2Error(RuntimeError):
    """A frozen input, execution, or output invariant failed closed."""


@dataclass(frozen=True, slots=True)
class MetaSlice2Config:
    """All inputs and execution semantics for one fresh yield-probe run."""

    output_root: Path
    theorem_store_path: Path
    theorem_store_sha256: str
    extraction_manifest_path: Path
    extraction_manifest_sha256: str
    transform_engine_path: Path
    mathlib_project_path: Path
    expected_source: str = PRODUCTION_SOURCE
    expected_source_revision: str = PRODUCTION_SOURCE_REVISION
    sample_size: int = PRODUCTION_SAMPLE_SIZE
    timeout_seconds: int = PRODUCTION_TIMEOUT_SECONDS
    address_space_bytes: int = PRODUCTION_ADDRESS_SPACE_BYTES
    lean_memory_mb: int = PRODUCTION_LEAN_MEMORY_MB
    enforce_production_bindings: bool = True
    enforce_storage_root: bool = True
    verify_mathlib_revision: bool = True


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Deterministic projection of the extraction onto unique public names."""

    names: tuple[str, ...]
    theorem_rows: int
    eligible_rows: int
    eligible_unique_names: int
    duplicate_eligible_names: int
    excluded_transform_ineligible: int
    excluded_private: int

    def manifest_payload(self) -> dict[str, object]:
        return {
            "selection_domain": SELECTION_DOMAIN,
            "requested_count": len(self.names),
            "theorem_rows": self.theorem_rows,
            "eligible_rows": self.eligible_rows,
            "eligible_unique_names": self.eligible_unique_names,
            "duplicate_eligible_names": self.duplicate_eligible_names,
            "excluded_transform_ineligible": self.excluded_transform_ineligible,
            "excluded_private": self.excluded_private,
            "selected_names_sha256": hashlib.sha256(_names_bytes(self.names)).hexdigest(),
        }


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome returned by a real or fixture Lean executor."""

    returncode: int | None
    timed_out: bool
    elapsed_seconds: float
    pid: int | None = None
    process_group_id: int | None = None
    process_start_ticks: int | None = None
    boot_id: str | None = None
    term_sent: bool = False
    kill_sent: bool = False
    group_gone: bool = True


@dataclass(frozen=True, slots=True)
class CandidateCertificate:
    """Minimal immutable input to Lean's independent site reconstruction."""

    declaration: str
    family: str
    operation: str
    site_path: str
    candidate_type_hash: str

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.declaration,
            self.family,
            self.operation,
            self.site_path,
            self.candidate_type_hash,
        )


@dataclass(frozen=True, slots=True)
class ParsedProbeOutput:
    """Independently checked primary output plus audit inputs."""

    summary: dict[str, object]
    certificates: tuple[CandidateCertificate, ...]


@dataclass(frozen=True, slots=True)
class _AttemptSpec:
    stage: str
    start: int
    stop: int
    ordinal: int
    parent_attempt_id: str | None

    @property
    def logical_id(self) -> str:
        prefix = "p" if self.stage == "primary" else "a"
        return f"{prefix}{self.start:08d}-{self.stop:08d}"

    @property
    def attempt_id(self) -> str:
        return f"attempt-{self.stage}-{self.start:08d}-{self.stop:08d}-r{self.ordinal:03d}"


@dataclass(frozen=True, slots=True)
class _AttemptOutcome:
    spec: _AttemptSpec
    result: dict[str, object]
    result_path: Path


@dataclass(frozen=True, slots=True)
class _AttemptMaterial:
    input_filename: str
    input_bytes: bytes
    driver_bytes: bytes
    item_count: int


@dataclass(slots=True)
class _RunContext:
    config: MetaSlice2Config
    selection: SelectionResult
    executor: LeanExecutor | None
    execute_missing: bool
    manifest: dict[str, object] | None
    visited_attempt_ids: set[str]


class LeanExecutor(Protocol):
    """Injectable execution boundary used by the focused CPU tests."""

    def run(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        process_state_path: Path,
        attempt_id: str,
    ) -> ExecutionResult: ...


def _boot_id() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    _require_regular_file(path, label="Linux boot identity")
    value = path.read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9a-f-]{36}", value) is None:
        raise MetaSlice2Error("Linux boot identity is malformed")
    return value


def _process_stat(pid: int) -> tuple[int, int]:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError) as exc:
        raise MetaSlice2Error(f"process {pid} disappeared before identity capture") from exc
    close = text.rfind(")")
    fields = text[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19:
        raise MetaSlice2Error(f"process {pid} has malformed /proc identity")
    try:
        process_group_id = int(fields[2])
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise MetaSlice2Error(f"process {pid} has non-integer /proc identity") from exc
    return process_group_id, start_ticks


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_state_payload(
    *,
    attempt_id: str,
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: int,
    started_at: str,
    phase: str,
    pid: int | None,
    process_group_id: int | None,
    process_start_ticks: int | None,
    boot_id: str | None,
    returncode: int | None,
    timed_out: bool,
    interrupted: bool,
    term_sent: bool,
    kill_sent: bool,
    group_gone: bool,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "command": list(command),
        "command_sha256": hashlib.sha256(canonical_json_bytes(list(command))).hexdigest(),
        "cwd": str(_resolve(cwd)),
        "timeout_seconds": timeout_seconds,
        "started_at": started_at,
        "updated_at": _utc_now(),
        "phase": phase,
        "pid": pid,
        "process_group_id": process_group_id,
        "process_start_ticks": process_start_ticks,
        "boot_id": boot_id,
        "returncode": returncode,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "term_sent": term_sent,
        "kill_sent": kill_sent,
        "group_gone": group_gone,
    }


def _terminate_process_group(
    process: subprocess.Popen[bytes],
) -> tuple[bool, bool, bool]:
    if not _process_group_exists(process.pid):
        process.poll()
        return False, False, True
    term_sent = False
    kill_sent = False
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
        term_sent = True
    deadline = time.monotonic() + 5
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.05)
    if _process_group_exists(process.pid):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
            kill_sent = True
        deadline = time.monotonic() + 5
        while _process_group_exists(process.pid) and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.05)
    # Never perform an unbounded reap here. A task stuck in uninterruptible
    # sleep may survive even SIGKILL; its durable process journal must remain
    # ``cleanup_pending`` so a later resume can retry without duplicating work.
    process.poll()
    return term_sent, kill_sent, not _process_group_exists(process.pid)


class SubprocessLeanExecutor:
    """Run Lean locally with closed stdin and file-backed output streams."""

    def run(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        process_state_path: Path,
        attempt_id: str,
    ) -> ExecutionResult:
        started = time.monotonic()
        started_at = _utc_now()
        with stdout_path.open("xb") as stdout_handle, stderr_path.open("xb") as stderr_handle:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            try:
                process_group_id, process_start_ticks = _process_stat(process.pid)
                if process_group_id != process.pid:
                    raise MetaSlice2Error(
                        "new Lean process did not lead its isolated process group"
                    )
                boot_id = _boot_id()
                _write_json_atomic(
                    process_state_path,
                    _process_state_payload(
                        attempt_id=attempt_id,
                        command=command,
                        cwd=cwd,
                        timeout_seconds=timeout_seconds,
                        started_at=started_at,
                        phase="running",
                        pid=process.pid,
                        process_group_id=process_group_id,
                        process_start_ticks=process_start_ticks,
                        boot_id=boot_id,
                        returncode=None,
                        timed_out=False,
                        interrupted=False,
                        term_sent=False,
                        kill_sent=False,
                        group_gone=False,
                    ),
                )
            except BaseException:
                _terminate_process_group(process)
                raise
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                term_sent, kill_sent, group_gone = _terminate_process_group(process)
                _write_json_atomic(
                    process_state_path,
                    _process_state_payload(
                        attempt_id=attempt_id,
                        command=command,
                        cwd=cwd,
                        timeout_seconds=timeout_seconds,
                        started_at=started_at,
                        phase="finished" if group_gone else "cleanup_pending",
                        pid=process.pid,
                        process_group_id=process_group_id,
                        process_start_ticks=process_start_ticks,
                        boot_id=boot_id,
                        returncode=None,
                        timed_out=True,
                        interrupted=False,
                        term_sent=term_sent,
                        kill_sent=kill_sent,
                        group_gone=group_gone,
                    ),
                )
                if not group_gone:
                    raise MetaSlice2Error(
                        "timed-out Lean process group survived TERM and KILL"
                    ) from None
                return ExecutionResult(
                    returncode=None,
                    timed_out=True,
                    elapsed_seconds=time.monotonic() - started,
                    pid=process.pid,
                    process_group_id=process_group_id,
                    process_start_ticks=process_start_ticks,
                    boot_id=boot_id,
                    term_sent=term_sent,
                    kill_sent=kill_sent,
                    group_gone=group_gone,
                )
            except BaseException:
                term_sent, kill_sent, group_gone = _terminate_process_group(process)
                _write_json_atomic(
                    process_state_path,
                    _process_state_payload(
                        attempt_id=attempt_id,
                        command=command,
                        cwd=cwd,
                        timeout_seconds=timeout_seconds,
                        started_at=started_at,
                        phase="finished" if group_gone else "cleanup_pending",
                        pid=process.pid,
                        process_group_id=process_group_id,
                        process_start_ticks=process_start_ticks,
                        boot_id=boot_id,
                        returncode=process.poll(),
                        timed_out=False,
                        interrupted=True,
                        term_sent=term_sent,
                        kill_sent=kill_sent,
                        group_gone=group_gone,
                    ),
                )
                raise
        term_sent = False
        kill_sent = False
        group_gone = not _process_group_exists(process_group_id)
        if not group_gone:
            term_sent, kill_sent, group_gone = _terminate_process_group(process)
        _write_json_atomic(
            process_state_path,
            _process_state_payload(
                attempt_id=attempt_id,
                command=command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                started_at=started_at,
                phase="finished" if group_gone else "cleanup_pending",
                pid=process.pid,
                process_group_id=process_group_id,
                process_start_ticks=process_start_ticks,
                boot_id=boot_id,
                returncode=returncode,
                timed_out=False,
                interrupted=False,
                term_sent=term_sent,
                kill_sent=kill_sent,
                group_gone=group_gone,
            ),
        )
        if not group_gone:
            raise MetaSlice2Error("Lean process group survived process completion cleanup")
        return ExecutionResult(
            returncode=returncode,
            timed_out=False,
            elapsed_seconds=time.monotonic() - started,
            pid=process.pid,
            process_group_id=process_group_id,
            process_start_ticks=process_start_ticks,
            boot_id=boot_id,
            term_sent=term_sent,
            kill_sent=kill_sent,
            group_gone=group_gone,
        )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def production_config(output_root: Path) -> MetaSlice2Config:
    """Construct the only configuration exposed by the production CLI."""
    return MetaSlice2Config(
        output_root=output_root,
        theorem_store_path=PRODUCTION_THEOREM_STORE,
        theorem_store_sha256=PRODUCTION_THEOREM_STORE_SHA256,
        extraction_manifest_path=PRODUCTION_EXTRACTION_MANIFEST,
        extraction_manifest_sha256=PRODUCTION_EXTRACTION_MANIFEST_SHA256,
        transform_engine_path=_repo_root() / "LeanFaith" / "Meta" / "TransformEngine.lean",
        mathlib_project_path=PRODUCTION_MATHLIB_PROJECT,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _selection_rank(name: str) -> tuple[str, str]:
    return hashlib.sha256(SELECTION_PREFIX + name.encode("utf-8")).hexdigest(), name


def _parse_json_object(text: str, *, context: str) -> dict[str, object]:
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MetaSlice2Error(f"{context} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise MetaSlice2Error(f"{context} must be a JSON object with string keys")
    return cast(dict[str, object], value)


def _mapping_field(row: Mapping[str, object], key: str, *, context: str) -> dict[str, object]:
    value = row.get(key)
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value):
        raise MetaSlice2Error(f"{context}.{key} must be an object")
    return cast(dict[str, object], value)


def _list_field(row: Mapping[str, object], key: str, *, context: str) -> list[object]:
    value = row.get(key)
    if not isinstance(value, list):
        raise MetaSlice2Error(f"{context}.{key} must be an array")
    return cast(list[object], value)


def _string_field(row: Mapping[str, object], key: str, *, context: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise MetaSlice2Error(f"{context}.{key} must be a non-empty string")
    return value


def _bool_field(row: Mapping[str, object], key: str, *, context: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise MetaSlice2Error(f"{context}.{key} must be a boolean")
    return value


def _nonnegative_int_field(row: Mapping[str, object], key: str, *, context: str) -> int:
    value = row.get(key)
    if type(value) is not int or value < 0:
        raise MetaSlice2Error(f"{context}.{key} must be a non-negative integer")
    return value


def _hash_field(row: Mapping[str, object], key: str, *, context: str) -> str:
    value = _string_field(row, key, context=context)
    if _HEX64.fullmatch(value) is None:
        raise MetaSlice2Error(f"{context}.{key} must be a lowercase SHA-256")
    return value


def _validate_hash_literal(value: str, *, label: str) -> None:
    if _HEX64.fullmatch(value) is None:
        raise MetaSlice2Error(f"{label} must be a lowercase SHA-256")


def _resolve(path: Path) -> Path:
    return path.resolve(strict=False)


def _require_regular_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise MetaSlice2Error(f"{label} must be a regular non-symlink file: {path}")


def _validate_config(config: MetaSlice2Config) -> None:
    _validate_hash_literal(config.theorem_store_sha256, label="theorem_store_sha256")
    _validate_hash_literal(
        config.extraction_manifest_sha256,
        label="extraction_manifest_sha256",
    )
    if config.sample_size <= 0:
        raise MetaSlice2Error("sample_size must be positive")
    if config.timeout_seconds <= 0:
        raise MetaSlice2Error("timeout_seconds must be positive")
    if config.address_space_bytes <= 0 or config.lean_memory_mb <= 0:
        raise MetaSlice2Error("Lean memory limits must be positive")
    if not config.expected_source or not config.expected_source_revision:
        raise MetaSlice2Error("source and source revision bindings must be non-empty")
    if config.output_root == config.output_root.parent:
        raise MetaSlice2Error("output root cannot be a filesystem root")
    if config.enforce_storage_root and not _resolve(config.output_root).is_relative_to(
        PRODUCTION_STORAGE_ROOT
    ):
        raise MetaSlice2Error("all yield-probe artifacts must be under /storage/milikic")
    if config.enforce_production_bindings:
        exact_paths = {
            "theorem store": (config.theorem_store_path, PRODUCTION_THEOREM_STORE),
            "extraction manifest": (
                config.extraction_manifest_path,
                PRODUCTION_EXTRACTION_MANIFEST,
            ),
            "mathlib project": (config.mathlib_project_path, PRODUCTION_MATHLIB_PROJECT),
            "transform engine": (
                config.transform_engine_path,
                _repo_root() / "LeanFaith" / "Meta" / "TransformEngine.lean",
            ),
        }
        for label, (actual_path, expected_path) in exact_paths.items():
            if _resolve(actual_path) != _resolve(expected_path):
                raise MetaSlice2Error(f"production {label} binding differs from {expected_path}")
        exact_values: tuple[tuple[str, object, object], ...] = (
            ("theorem store SHA-256", config.theorem_store_sha256, PRODUCTION_THEOREM_STORE_SHA256),
            (
                "extraction manifest SHA-256",
                config.extraction_manifest_sha256,
                PRODUCTION_EXTRACTION_MANIFEST_SHA256,
            ),
            ("source", config.expected_source, PRODUCTION_SOURCE),
            ("source revision", config.expected_source_revision, PRODUCTION_SOURCE_REVISION),
            ("sample size", config.sample_size, PRODUCTION_SAMPLE_SIZE),
            ("timeout", config.timeout_seconds, PRODUCTION_TIMEOUT_SECONDS),
            (
                "address-space limit",
                config.address_space_bytes,
                PRODUCTION_ADDRESS_SPACE_BYTES,
            ),
            ("Lean memory limit", config.lean_memory_mb, PRODUCTION_LEAN_MEMORY_MB),
        )
        for label, actual_value, expected_value in exact_values:
            if actual_value != expected_value:
                raise MetaSlice2Error(f"production {label} binding differs from {expected_value}")
        if not config.enforce_storage_root or not config.verify_mathlib_revision:
            raise MetaSlice2Error("production run cannot disable storage or revision checks")


def _verify_file_hash(path: Path, expected: str, *, label: str) -> None:
    _require_regular_file(path, label=label)
    actual = hash_file(path)
    if actual != expected:
        raise MetaSlice2Error(f"{label} SHA-256 mismatch: expected {expected}, got {actual}")


def _git_revision(path: Path) -> str:
    if path.is_symlink() or not path.is_dir():
        raise MetaSlice2Error(f"git checkout must be a non-symlink directory: {path}")
    completed = subprocess.run(
        ("git", "-C", str(path), "rev-parse", "HEAD"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
        text=True,
    )
    revision = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        error = completed.stderr.strip() or f"exit {completed.returncode}"
        raise MetaSlice2Error(f"cannot read git revision for {path}: {error}")
    return revision


def _git_contains_commit(path: Path, revision: str) -> bool:
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        return False
    completed = subprocess.run(
        ("git", "-C", str(path), "cat-file", "-e", f"{revision}^{{commit}}"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return completed.returncode == 0


def _validate_extraction_manifest(config: MetaSlice2Config) -> dict[str, object]:
    _verify_file_hash(
        config.extraction_manifest_path,
        config.extraction_manifest_sha256,
        label="extraction manifest",
    )
    manifest = _parse_json_object(
        config.extraction_manifest_path.read_text(encoding="utf-8"),
        context="extraction manifest",
    )
    if manifest.get("source") != config.expected_source:
        raise MetaSlice2Error("extraction manifest source binding mismatch")
    if manifest.get("source_revision") != config.expected_source_revision:
        raise MetaSlice2Error("extraction manifest source revision mismatch")
    if manifest.get("stage") != "elaborated":
        raise MetaSlice2Error("extraction manifest is not an elaborated artifact")
    if manifest.get("artifact_class") != "production":
        raise MetaSlice2Error("extraction manifest is not a production artifact")
    checksums = _mapping_field(manifest, "output_partition_checksums", context="manifest")
    matching = [
        value
        for key, value in checksums.items()
        if key.endswith("/theorems/mathlib.jsonl") and isinstance(value, str)
    ]
    if matching != [config.theorem_store_sha256]:
        raise MetaSlice2Error("manifest does not bind the exact theorem-store checksum")
    return manifest


def select_declarations(config: MetaSlice2Config) -> SelectionResult:
    """Validate the frozen extraction and select exact names by salted SHA-256."""
    _validate_config(config)
    _verify_file_hash(
        config.theorem_store_path,
        config.theorem_store_sha256,
        label="theorem store",
    )
    manifest = _validate_extraction_manifest(config)

    theorem_rows = 0
    eligible_rows = 0
    excluded_transform_ineligible = 0
    excluded_private = 0
    names: set[str] = set()
    with config.theorem_store_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            theorem_rows += 1
            outer = _parse_json_object(raw_line, context=f"theorem row {line_number}")
            theorem = _mapping_field(outer, "theorem", context=f"theorem row {line_number}")
            context = f"theorem row {line_number}"
            if theorem.get("source") != config.expected_source:
                raise MetaSlice2Error(f"{context} is not public {config.expected_source}")
            if theorem.get("source_revision") != config.expected_source_revision:
                raise MetaSlice2Error(f"{context} has an unexpected source revision")
            declaration = _string_field(theorem, "declaration_full_name", context=context)
            metadata = _mapping_field(theorem, "metadata", context=context)
            if "_private." in declaration:
                excluded_private += 1
                continue
            eligible = (
                theorem.get("is_proposition") is True
                and theorem.get("elaboration_status") == "elaborates"
                and metadata.get("transform_source_eligible") is True
            )
            if not eligible:
                excluded_transform_ineligible += 1
                continue
            if any(character in declaration for character in ("\0", "\n", "\r")):
                raise MetaSlice2Error(f"{context} has a control character in its name")
            eligible_rows += 1
            names.add(declaration)

    row_count = manifest.get("row_count")
    if type(row_count) is not int or row_count != theorem_rows:
        raise MetaSlice2Error("extraction manifest row_count does not match the theorem-store rows")
    ordered = tuple(sorted(names, key=_selection_rank))
    if len(ordered) < config.sample_size:
        raise MetaSlice2Error(
            f"only {len(ordered)} unique eligible public names for sample size {config.sample_size}"
        )
    selected = ordered[: config.sample_size]
    if len(selected) != config.sample_size or len(set(selected)) != config.sample_size:
        raise MetaSlice2Error("selector did not produce the exact unique requested count")
    return SelectionResult(
        names=selected,
        theorem_rows=theorem_rows,
        eligible_rows=eligible_rows,
        eligible_unique_names=len(ordered),
        duplicate_eligible_names=eligible_rows - len(ordered),
        excluded_transform_ineligible=excluded_transform_ineligible,
        excluded_private=excluded_private,
    )


def _attempt_timeout_policy_payload(config: MetaSlice2Config) -> dict[str, object]:
    return {
        "ceiling_seconds": config.timeout_seconds,
        "formula": ATTEMPT_TIMEOUT_FORMULA,
        "primary": {
            "floor_seconds": PRIMARY_TIMEOUT_FLOOR_SECONDS,
            "seconds_per_item": PRIMARY_TIMEOUT_SECONDS_PER_ITEM,
        },
        "audit": {
            "floor_seconds": AUDIT_TIMEOUT_FLOOR_SECONDS,
            "seconds_per_item": AUDIT_TIMEOUT_SECONDS_PER_ITEM,
        },
    }


def _attempt_timeout_seconds(
    config: MetaSlice2Config,
    *,
    stage: str,
    item_count: int,
) -> int:
    if item_count <= 0:
        raise MetaSlice2Error("attempt item_count must be positive")
    if stage == "primary":
        floor_seconds = PRIMARY_TIMEOUT_FLOOR_SECONDS
        seconds_per_item = PRIMARY_TIMEOUT_SECONDS_PER_ITEM
    elif stage == "audit":
        floor_seconds = AUDIT_TIMEOUT_FLOOR_SECONDS
        seconds_per_item = AUDIT_TIMEOUT_SECONDS_PER_ITEM
    else:
        raise MetaSlice2Error(f"unsupported attempt stage {stage!r}")
    return min(
        config.timeout_seconds,
        max(floor_seconds, seconds_per_item * item_count),
    )


def _config_payload(config: MetaSlice2Config) -> dict[str, object]:
    return {
        "method_version": METHOD_VERSION,
        "selection_domain": SELECTION_DOMAIN,
        "sample_size": config.sample_size,
        "attempt_timeout_policy": _attempt_timeout_policy_payload(config),
        "primary_shard_size": PRIMARY_SHARD_SIZE,
        "audit_shard_size": AUDIT_SHARD_SIZE,
        "fallback_policy": "recursive_contiguous_midpoint_bisection",
        "address_space_bytes": config.address_space_bytes,
        "lean_memory_mb": config.lean_memory_mb,
        "expected_source": config.expected_source,
        "expected_source_revision": config.expected_source_revision,
        "theorem_store": {
            "path": str(_resolve(config.theorem_store_path)),
            "sha256": config.theorem_store_sha256,
        },
        "extraction_manifest": {
            "path": str(_resolve(config.extraction_manifest_path)),
            "sha256": config.extraction_manifest_sha256,
        },
        "transform_engine_path": str(_resolve(config.transform_engine_path)),
        "mathlib_project_path": str(_resolve(config.mathlib_project_path)),
        "public_only": True,
        "external_transmission": False,
        "private_source_content": False,
    }


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: object) -> None:
    _write_atomic(path, canonical_json_bytes(value) + b"\n")


def _names_bytes(names: Sequence[str]) -> bytes:
    return ("".join(f"{name}\n" for name in names)).encode("utf-8")


def _engine_helper_body(engine_path: Path) -> str:
    _require_regular_file(engine_path, label="TransformEngine source")
    text = engine_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    import_rows = [index for index, line in enumerate(lines) if line.strip().startswith("import ")]
    imports = [lines[index].strip() for index in import_rows]
    if "import Lean" not in imports or any(
        not imported.startswith("import Lean") for imported in imports
    ):
        raise MetaSlice2Error("TransformEngine imports must be Lean modules supplied by Mathlib")
    body = "\n".join(
        line for index, line in enumerate(lines) if index not in set(import_rows)
    ).strip()
    required_markers = (
        "namespace LeanFaith.Meta.TransformEngineHelper",
        "lfTransformBatch",
        "lfAuditTransform",
        "end LeanFaith.Meta.TransformEngineHelper",
    )
    if not all(marker in body for marker in required_markers):
        raise MetaSlice2Error("TransformEngine helper body lacks the batch-driver contract")
    return body + "\n"


def _driver_bytes(config: MetaSlice2Config, names_path: Path) -> bytes:
    helper_body = _engine_helper_body(config.transform_engine_path)
    names_literal = _lean_string(str(_resolve(names_path)))
    driver = (
        "import Mathlib\n\n"
        f"{helper_body}\n"
        "-- The external process-group timeout is the production compute bound;\n"
        "-- Lean's default heartbeat budget is cumulative across this shard command.\n"
        "set_option maxHeartbeats 0 in\n"
        f"lfTransformBatch {names_literal}\n"
    )
    return driver.encode("utf-8")


def _lean_string(value: str) -> str:
    if any(character in value for character in ("\0", "\n", "\r")):
        raise MetaSlice2Error("Lean audit literal contains a forbidden control character")
    return json.dumps(value, ensure_ascii=False)


def _audit_driver_bytes(
    config: MetaSlice2Config,
    certificates: Sequence[CandidateCertificate],
) -> bytes:
    helper_body = _engine_helper_body(config.transform_engine_path)
    commands = []
    for certificate in certificates:
        arguments = " ".join(
            _lean_string(value)
            for value in (
                certificate.declaration,
                certificate.family,
                certificate.operation,
                certificate.site_path,
                certificate.candidate_type_hash,
            )
        )
        commands.append(f"lfAuditTransform {arguments}")
    command_body = "\n".join(commands)
    driver = f"import Mathlib\n\n{helper_body}\n{command_body}\n"
    return driver.encode("utf-8")


def _command(config: MetaSlice2Config, driver_path: Path) -> tuple[str, ...]:
    return (
        "/usr/bin/prlimit",
        f"--as={config.address_space_bytes}",
        "--",
        "lake",
        "env",
        "lean",
        f"-M{config.lean_memory_mb}",
        "-j1",
        str(_resolve(driver_path)),
    )


def _artifact(path: Path) -> dict[str, object]:
    _require_regular_file(path, label="output artifact")
    return {
        "path": str(_resolve(path)),
        "sha256": hash_file(path),
        "size_bytes": path.stat().st_size,
    }


def _artifact_inventory(output_root: Path) -> dict[str, object]:
    result: dict[str, object] = {}
    for path in sorted(output_root.rglob("*")):
        relative = path.relative_to(output_root).as_posix()
        if relative == RUN_LOCK_FILENAME:
            _require_regular_file(path, label="yield-probe run lock")
            continue
        if relative == MANIFEST_FILENAME:
            _require_regular_file(path, label="yield-probe manifest")
            continue
        if path.is_symlink():
            raise MetaSlice2Error(f"output inventory contains a symlink: {relative}")
        if path.is_dir():
            parts = Path(relative).parts
            if parts == (SHARDS_DIRNAME,):
                continue
            if (
                len(parts) != 2
                or parts[0] != SHARDS_DIRNAME
                or _ATTEMPT_DIRECTORY_PATTERN.fullmatch(parts[1]) is None
            ):
                raise MetaSlice2Error(f"unexpected output directory: {relative}")
            continue
        if not path.is_file():
            raise MetaSlice2Error(f"output inventory contains a non-file: {relative}")
        if path.name.endswith(".partial"):
            raise MetaSlice2Error(f"output inventory contains an unfinished file: {relative}")
        parts = Path(relative).parts
        if len(parts) == 1:
            if parts[0] not in _ROOT_ARTIFACT_FILENAMES:
                raise MetaSlice2Error(f"unexpected root output artifact: {relative}")
        elif len(parts) == 3 and parts[0] == SHARDS_DIRNAME:
            match = _ATTEMPT_DIRECTORY_PATTERN.fullmatch(parts[1])
            if match is None:
                raise MetaSlice2Error(f"malformed attempt output path: {relative}")
            allowed = set(_COMMON_ATTEMPT_FILENAMES)
            allowed.add(
                ATTEMPT_NAMES_FILENAME
                if match.group(1) == "primary"
                else ATTEMPT_CERTIFICATES_FILENAME
            )
            if parts[2] not in allowed:
                raise MetaSlice2Error(f"unexpected attempt artifact: {relative}")
        else:
            raise MetaSlice2Error(f"unexpected output artifact path: {relative}")
        result[relative] = _artifact(path)
    return result


def _progress_artifact_inventory(output_root: Path) -> dict[str, object]:
    inventory = _artifact_inventory(output_root)
    for relative in tuple(inventory):
        path = Path(relative)
        if path.name != ATTEMPT_PROCESS_FILENAME or len(path.parts) != 3:
            continue
        if not (output_root / path.parent / ATTEMPT_RESULT_FILENAME).is_file():
            inventory.pop(relative)
    return inventory


def _log_bytes(command: Sequence[str], stdout_path: Path, stderr_path: Path) -> bytes:
    stdout = stdout_path.read_bytes() if stdout_path.is_file() else b""
    stderr = stderr_path.read_bytes() if stderr_path.is_file() else b""
    command_line = json.dumps(list(command), ensure_ascii=False, separators=(",", ":"))
    return (
        f"command={command_line}\n--- stdout ---\n".encode()
        + stdout
        + b"\n--- stderr ---\n"
        + stderr
    )


def _candidate_row(
    row: Mapping[str, object],
    *,
    context: str,
    selected: frozenset[str],
) -> dict[str, object]:
    if row.get("schemaVersion") != 2:
        raise MetaSlice2Error(f"{context}.schemaVersion must be 2")
    declaration = _string_field(row, "declaration", context=context)
    if declaration not in selected:
        raise MetaSlice2Error(f"{context} names an unrequested declaration")
    if row.get("recordKind") != "candidate":
        raise MetaSlice2Error(f"{context}.recordKind must be candidate")
    if row.get("status") != "ok":
        raise MetaSlice2Error(f"{context}.status must be ok")
    family = _string_field(row, "family", context=context)
    evidence_class = _string_field(row, "evidenceClass", context=context)
    expected_class = _FAMILY_EVIDENCE_CLASS.get(family)
    if expected_class is None:
        raise MetaSlice2Error(f"{context} has unsupported family {family}")
    if evidence_class != expected_class:
        raise MetaSlice2Error(f"{context} evidence class {evidence_class} does not match {family}")
    operation = _string_field(row, "operation", context=context)
    operation_kind = _string_field(row, "operationKind", context=context)
    if family == "P20":
        operation_valid = operation.startswith("unfold:") and len(operation) > len("unfold:")
        expected_operation_kind = "unfold"
    elif family == "P21":
        operation_valid = operation in _P21_OPERATIONS
        expected_operation_kind = operation
    elif family == "P23":
        operation_valid = re.fullmatch(r"(?:curry|uncurry):[0-9]+", operation) is not None
        expected_operation_kind = operation.split(":", maxsplit=1)[0]
    else:
        operation_valid = re.fullmatch(r"swapAdjacent:[0-9]+", operation) is not None
        expected_operation_kind = "swapAdjacent"
    if not operation_valid or operation_kind != expected_operation_kind:
        raise MetaSlice2Error(f"{context} has a family/operationKind mismatch")
    site_path = _string_field(row, "sitePath", context=context)
    if not site_path.startswith("/"):
        raise MetaSlice2Error(f"{context}.sitePath must be a stable absolute coordinate")
    binder_depth = _nonnegative_int_field(row, "binderDepth", context=context)
    nested_site = _bool_field(row, "nestedSite", context=context)
    if nested_site != (site_path != "/" or binder_depth != 0):
        raise MetaSlice2Error(f"{context}.nestedSite does not match path/depth")
    source = _string_field(row, "source", context=context)
    candidate = _string_field(row, "candidate", context=context)
    if source == candidate:
        raise MetaSlice2Error(f"{context} does not change the pretty-printed type")
    if _string_field(row, "sourcePretty", context=context) != source:
        raise MetaSlice2Error(f"{context}.sourcePretty alias differs from source")
    if _string_field(row, "candidatePretty", context=context) != candidate:
        raise MetaSlice2Error(f"{context}.candidatePretty alias differs from candidate")
    source_hash = _hash_field(row, "sourceTypeHash", context=context)
    candidate_hash = _hash_field(row, "candidateTypeHash", context=context)
    if source_hash != _sha256_text(source):
        raise MetaSlice2Error(f"{context}.sourceTypeHash failed independent SHA-256 audit")
    if candidate_hash != _sha256_text(candidate):
        raise MetaSlice2Error(f"{context}.candidateTypeHash failed independent SHA-256 audit")
    if source_hash == candidate_hash:
        raise MetaSlice2Error(f"{context} source/candidate hashes are identical")
    source_site = _string_field(row, "sourceSite", context=context)
    candidate_site = _string_field(row, "candidateSite", context=context)
    source_site_hash = _hash_field(row, "sourceSiteHash", context=context)
    candidate_site_hash = _hash_field(row, "candidateSiteHash", context=context)
    if source_site_hash != _sha256_text(source_site):
        raise MetaSlice2Error(f"{context}.sourceSiteHash failed independent SHA-256 audit")
    if candidate_site_hash != _sha256_text(candidate_site):
        raise MetaSlice2Error(f"{context}.candidateSiteHash failed independent SHA-256 audit")
    if not _bool_field(row, "candidateElaborates", context=context):
        raise MetaSlice2Error(f"{context} candidate did not elaborate")
    whole_type_defeq = _bool_field(row, "wholeTypeDefEq", context=context)
    if evidence_class == "P-DEF" and not whole_type_defeq:
        raise MetaSlice2Error(f"{context} P-DEF candidate is not whole-type defeq")
    evidence = _mapping_field(row, "evidence", context=context)
    witness = _mapping_field(row, "witness", context=context)
    if witness.get("sourceSiteHash") != source_site_hash:
        raise MetaSlice2Error(f"{context} witness source-site binding differs")
    if witness.get("candidateSiteHash") != candidate_site_hash:
        raise MetaSlice2Error(f"{context} witness candidate-site binding differs")
    if evidence_class == "P-DEF":
        if evidence.get("relation") != "definitionalEquality" or row.get("axioms") != "none":
            raise MetaSlice2Error(f"{context} has malformed P-DEF evidence")
        if evidence.get("wholeTypeDefEqRequired") is not True:
            raise MetaSlice2Error(f"{context} P-DEF evidence does not require whole-type defeq")
    elif row.get("axioms") != "constructive":
        raise MetaSlice2Error(f"{context} has malformed P-SCHEMA axiom evidence")
    if family == "P20":
        constant = _string_field(witness, "constant", context=f"{context}.witness")
        arguments = _list_field(witness, "arguments", context=f"{context}.witness")
        binder_info = _list_field(
            witness,
            "argumentBinderInfo",
            context=f"{context}.witness",
        )
        universe_arguments = _list_field(
            witness,
            "universeArguments",
            context=f"{context}.witness",
        )
        argument_count = _nonnegative_int_field(
            witness,
            "argumentCount",
            context=f"{context}.witness",
        )
        delta_steps = _nonnegative_int_field(
            evidence,
            "deltaSteps",
            context=f"{context}.evidence",
        )
        if (
            operation != f"unfold:{constant}"
            or delta_steps != 1
            or evidence.get("safeDefinition") is not True
            or evidence.get("transparentDefinition") is not True
            or evidence.get("typedSubterm") is not True
            or evidence.get("contextReconstructed") is not True
            or evidence.get("inverseFoldCertified") is not True
            or witness.get("definitionSafety") != "safe"
            or witness.get("reducibility") not in {"reducible", "semireducible"}
            or witness.get("inverseOperation") != "fold"
            or witness.get("inverseUsesPreservedApplication") is not True
            or witness.get("foldSearch") is not False
            or witness.get("unfoldResidualStructuralMatch") is not True
            or witness.get("residualHash") != candidate_site_hash
            or argument_count != len(arguments)
            or argument_count != len(binder_info)
            or not all(isinstance(value, str) and value for value in arguments)
            or not all(
                isinstance(value, str)
                and value
                in {"default", "implicit", "strictImplicit", "instImplicit", "overapplied"}
                for value in binder_info
            )
            or not all(isinstance(value, str) and value for value in universe_arguments)
        ):
            raise MetaSlice2Error(f"{context} lacks the exact no-search inverse-fold certificate")
    elif family == "P21":
        redex_kind = "beta" if operation.startswith("beta") else "zeta"
        direction = "introduce" if operation.endswith("Introduce") else "eliminate"
        expected_residual = source_site_hash if direction == "introduce" else candidate_site_hash
        redex_count = _nonnegative_int_field(
            evidence,
            "redexCount",
            context=f"{context}.evidence",
        )
        if (
            evidence.get("redexKind") != redex_kind
            or redex_count != 1
            or evidence.get("contextReconstructed") is not True
            or witness.get("direction") != direction
            or witness.get("residualRule") != "instantiate1"
            or witness.get("captureFreeByKernelSubstitution") is not True
            or witness.get("residualHash") != expected_residual
        ):
            raise MetaSlice2Error(f"{context} has a malformed beta/zeta residual certificate")
    return {
        "declaration": declaration,
        "family": family,
        "operation": operation,
        "operation_kind": operation_kind,
        "site_path": site_path,
        "binder_depth": binder_depth,
        "nested_site": nested_site,
        "source": source,
        "candidate_hash": candidate_hash,
        "evidence_class": evidence_class,
        "whole_type_defeq": whole_type_defeq,
    }


def _terminal_row(
    row: Mapping[str, object],
    *,
    context: str,
    selected: frozenset[str],
    allow_runner_dispositions: bool,
) -> dict[str, object]:
    if row.get("schemaVersion") != 2:
        raise MetaSlice2Error(f"{context}.schemaVersion must be 2")
    declaration = _string_field(row, "declaration", context=context)
    if declaration not in selected:
        raise MetaSlice2Error(f"{context} names an unrequested declaration")
    if row.get("recordKind") != "status":
        raise MetaSlice2Error(f"{context}.recordKind must be status")
    status = _string_field(row, "status", context=context)
    if status in _RUNNER_TERMINAL_STATUSES:
        if not allow_runner_dispositions:
            raise MetaSlice2Error(f"{context} contains a runner disposition in raw Lean output")
    elif status not in _TERMINAL_STATUSES:
        raise MetaSlice2Error(f"{context} has unsupported terminal status {status}")
    candidate_count = _nonnegative_int_field(row, "candidateCount", context=context)
    emitted_count = _nonnegative_int_field(row, "emittedCount", context=context)
    duplicate_count = _nonnegative_int_field(row, "duplicateCount", context=context)
    rejected_count = _nonnegative_int_field(row, "rejectedCount", context=context)
    if candidate_count != emitted_count + duplicate_count:
        raise MetaSlice2Error(f"{context} candidate/emitted/duplicate counts do not reconcile")
    if status != "complete" and (
        candidate_count != 0 or emitted_count != 0 or duplicate_count != 0 or rejected_count != 0
    ):
        raise MetaSlice2Error(f"{context} rejected declaration reports candidates")
    error = row.get("error")
    if error is not None and (not isinstance(error, str) or not error):
        raise MetaSlice2Error(f"{context}.error must be null or a non-empty string")
    if status == "error" and not isinstance(error, str):
        raise MetaSlice2Error(f"{context} error terminal lacks an error message")
    if status in _PROCESSED_TERMINAL_STATUSES:
        source = _string_field(row, "source", context=context)
        source_hash = _hash_field(row, "sourceTypeHash", context=context)
        if source_hash != _sha256_text(source):
            raise MetaSlice2Error(f"{context}.sourceTypeHash failed independent SHA-256 audit")
    else:
        source = None
        source_hash = None
    runner: dict[str, object] | None = None
    if status == "complete":
        if row.get("sourceTextRoundtripVerified") is not True:
            raise MetaSlice2Error(f"{context} lacks a verified source-text roundtrip")
        discovered_count = _nonnegative_int_field(row, "discoveredCount", context=context)
        _nonnegative_int_field(row, "pathCount", context=context)
        if discovered_count != candidate_count + rejected_count:
            raise MetaSlice2Error(
                f"{context} discovered/candidate/rejected counts do not reconcile"
            )
    elif status == "sourceTextRejected":
        if (
            row.get("sourceTextRoundtripVerified") is not False
            or row.get("reasonCode") != "source_pretty_roundtrip_mismatch"
            or error is not None
        ):
            raise MetaSlice2Error(f"{context} has a malformed source-text rejection")
        discovered_count = 0
    elif status in _RUNNER_TERMINAL_STATUSES:
        if row.get("terminalOrigin") != "runner" or error is not None:
            raise MetaSlice2Error(f"{context} has a malformed runner-origin disposition")
        reason_code = _string_field(row, "reasonCode", context=context)
        if reason_code not in _RUNNER_REASON_CODES:
            raise MetaSlice2Error(f"{context} has unsupported runner reason {reason_code}")
        if status != _RUNNER_STATUS_BY_REASON[reason_code]:
            raise MetaSlice2Error(f"{context} status contradicts its runner reason")
        attempt_id = _string_field(row, "attemptId", context=context)
        attempt_path = _string_field(row, "attemptPath", context=context)
        attempt_result_sha256 = _hash_field(
            row,
            "attemptResultSha256",
            context=context,
        )
        timeout_seconds = _nonnegative_int_field(row, "timeoutSeconds", context=context)
        if timeout_seconds <= 0:
            raise MetaSlice2Error(f"{context}.timeoutSeconds must be positive")
        timed_out = _bool_field(row, "timedOut", context=context)
        returncode = row.get("returncode")
        if returncode is not None and type(returncode) is not int:
            raise MetaSlice2Error(f"{context}.returncode must be an integer or null")
        if reason_code == "timeout":
            if not timed_out or returncode is not None:
                raise MetaSlice2Error(f"{context} has inconsistent timeout evidence")
        elif reason_code == "nonzero_exit":
            if timed_out or type(returncode) is not int or returncode == 0:
                raise MetaSlice2Error(f"{context} has inconsistent nonzero-exit evidence")
        elif timed_out or returncode != 0:
            raise MetaSlice2Error(f"{context} has inconsistent invalid-output evidence")
        runner = {
            "declaration": declaration,
            "reason_code": reason_code,
            "attempt_id": attempt_id,
            "attempt_path": attempt_path,
            "attempt_result_sha256": attempt_result_sha256,
            "timeout_seconds": timeout_seconds,
            "timed_out": timed_out,
            "returncode": returncode,
        }
        discovered_count = 0
    else:
        discovered_count = 0
    return {
        "declaration": declaration,
        "status": status,
        "candidate_count": candidate_count,
        "emitted_count": emitted_count,
        "duplicate_count": duplicate_count,
        "rejected_count": rejected_count,
        "discovered_count": discovered_count,
        "source": source,
        "source_hash": source_hash,
        "error": error,
        "runner": runner,
    }


def _nearest_rank_p95(values: Sequence[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _parse_probe_output(
    stdout_path: Path,
    *,
    selection: SelectionResult,
    names_path: Path,
    allow_runner_dispositions: bool = False,
) -> ParsedProbeOutput:
    """Fail-closed audit of line-delimited Lean output and its yield summary."""
    _require_regular_file(stdout_path, label="Lean stdout")
    selected = frozenset(selection.names)
    candidates: list[dict[str, object]] = []
    terminals: dict[str, dict[str, object]] = {}
    seen_candidate_keys: set[tuple[str, str, str, str, str]] = set()
    seen_candidate_hashes: set[tuple[str, str]] = set()
    source_by_declaration: dict[str, str] = {}
    source_hash_by_declaration: dict[str, str] = {}
    batch: dict[str, object] | None = None

    with stdout_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            context = f"Lean stdout line {line_number}"
            row = _parse_json_object(raw_line, context=context)
            kind = row.get("kind")
            if kind == "candidate":
                if batch is not None:
                    raise MetaSlice2Error(f"{context} occurs after the batch terminal")
                parsed = _candidate_row(row, context=context, selected=selected)
                declaration = cast(str, parsed["declaration"])
                if declaration in terminals:
                    raise MetaSlice2Error(
                        f"{context} emits a candidate after its declaration terminal"
                    )
                family = cast(str, parsed["family"])
                operation = cast(str, parsed["operation"])
                site_path = cast(str, parsed["site_path"])
                candidate_hash = cast(str, parsed["candidate_hash"])
                key = (declaration, family, operation, site_path, candidate_hash)
                if key in seen_candidate_keys:
                    raise MetaSlice2Error(f"{context} duplicates an emitted candidate")
                seen_candidate_keys.add(key)
                declaration_hash = (declaration, candidate_hash)
                if declaration_hash in seen_candidate_hashes:
                    raise MetaSlice2Error(
                        f"{context} repeats a candidate hash that Lean should deduplicate"
                    )
                seen_candidate_hashes.add(declaration_hash)
                source = cast(str, parsed["source"])
                source_hash = _hash_field(row, "sourceTypeHash", context=context)
                previous_source = source_by_declaration.setdefault(declaration, source)
                previous_hash = source_hash_by_declaration.setdefault(declaration, source_hash)
                if previous_source != source or previous_hash != source_hash:
                    raise MetaSlice2Error(
                        f"{context} changes the source type within one declaration"
                    )
                candidates.append(parsed)
            elif kind == "terminal":
                if batch is not None:
                    raise MetaSlice2Error(f"{context} occurs after the batch terminal")
                parsed_terminal = _terminal_row(
                    row,
                    context=context,
                    selected=selected,
                    allow_runner_dispositions=allow_runner_dispositions,
                )
                declaration = cast(str, parsed_terminal["declaration"])
                if declaration in terminals:
                    raise MetaSlice2Error(f"{context} duplicates a terminal declaration")
                terminals[declaration] = parsed_terminal
            elif kind == "batch":
                if batch is not None:
                    raise MetaSlice2Error(f"{context} duplicates the batch terminal")
                if row.get("recordKind") != "batch":
                    raise MetaSlice2Error(f"{context}.recordKind must be batch")
                if row.get("schemaVersion") != 2:
                    raise MetaSlice2Error(f"{context}.schemaVersion must be 2")
                status = _string_field(row, "status", context=context)
                if status not in {"complete", "partial"}:
                    raise MetaSlice2Error(f"{context} reports unsupported batch status {status}")
                if row.get("namesFile") != str(_resolve(names_path)):
                    raise MetaSlice2Error(f"{context} namesFile differs from the bound input")
                declaration_count = _nonnegative_int_field(row, "declarationCount", context=context)
                completed_count = _nonnegative_int_field(row, "completedCount", context=context)
                failed_count = _nonnegative_int_field(row, "failedCount", context=context)
                if declaration_count != len(
                    selection.names
                ) or completed_count + failed_count != len(selection.names):
                    raise MetaSlice2Error(f"{context} batch counts do not reconcile")
                if status != ("complete" if failed_count == 0 else "partial"):
                    raise MetaSlice2Error(f"{context} batch status contradicts failedCount")
                if allow_runner_dispositions:
                    accounted_count = _nonnegative_int_field(
                        row,
                        "accountedCount",
                        context=context,
                    )
                    terminal_count = _nonnegative_int_field(
                        row,
                        "terminalCount",
                        context=context,
                    )
                    if (
                        row.get("producer") != "runner-aggregate-v4"
                        or accounted_count != len(selection.names)
                        or terminal_count != len(selection.names)
                    ):
                        raise MetaSlice2Error(f"{context} has a malformed runner aggregate")
                batch = {
                    "status": status,
                    "declaration_count": declaration_count,
                    "completed_count": completed_count,
                    "failed_count": failed_count,
                }
            else:
                raise MetaSlice2Error(f"{context} has unknown kind {kind!r}")

    if batch is None:
        raise MetaSlice2Error("Lean stdout lacks the batch terminal")
    if set(terminals) != selected:
        missing = sorted(selected.difference(terminals))
        extra = sorted(set(terminals).difference(selected))
        raise MetaSlice2Error(
            f"terminal declarations do not reconcile: missing={missing[:5]}, extra={extra[:5]}"
        )
    derived_completed = sum(
        row["status"] in _PROCESSED_TERMINAL_STATUSES for row in terminals.values()
    )
    derived_failed = len(terminals) - derived_completed
    if batch["completed_count"] != derived_completed or batch["failed_count"] != derived_failed:
        raise MetaSlice2Error("batch counts contradict per-declaration terminal statuses")
    emitted_by_declaration = Counter(cast(str, row["declaration"]) for row in candidates)
    for declaration, terminal in terminals.items():
        emitted_count = cast(int, terminal["emitted_count"])
        if emitted_by_declaration[declaration] != emitted_count:
            raise MetaSlice2Error(
                f"terminal emittedCount differs from candidate lines for {declaration}"
            )
        if emitted_count > 0 and terminal["status"] != "complete":
            raise MetaSlice2Error(f"candidates belong to rejected declaration {declaration}")
        candidate_source = source_by_declaration.get(declaration)
        candidate_source_hash = source_hash_by_declaration.get(declaration)
        if candidate_source is not None and (
            terminal["status"] != "complete"
            or terminal.get("source") != candidate_source
            or terminal.get("source_hash") != candidate_source_hash
        ):
            raise MetaSlice2Error(f"candidate/terminal source binding differs for {declaration}")

    family_counts = Counter(cast(str, row["family"]) for row in candidates)
    operation_counts = Counter(cast(str, row["operation_kind"]) for row in candidates)
    family_operation_counts = Counter(
        f"{row['family']}:{row['operation_kind']}" for row in candidates
    )
    evidence_counts = Counter(cast(str, row["evidence_class"]) for row in candidates)
    terminal_status_counts = Counter(cast(str, row["status"]) for row in terminals.values())
    runner_dispositions = [
        cast(dict[str, object], terminals[declaration]["runner"])
        for declaration in selection.names
        if terminals[declaration]["runner"] is not None
    ]
    lean_engine_dispositions = [
        {
            "declaration": declaration,
            "status": terminals[declaration]["status"],
            "error": terminals[declaration]["error"],
        }
        for declaration in selection.names
        if terminals[declaration]["status"] in {"error", "notProp", "notfound"}
    ]
    source_text_rejected_declarations = sorted(
        declaration
        for declaration, row in terminals.items()
        if row["status"] == "sourceTextRejected"
    )
    nested_count = sum(cast(bool, row["nested_site"]) for row in candidates)
    duplicate_count = sum(cast(int, row["duplicate_count"]) for row in terminals.values())
    rejected_count = sum(cast(int, row["rejected_count"]) for row in terminals.values())
    discovered_count = sum(cast(int, row["discovered_count"]) for row in terminals.values())
    emitted_counts = [
        cast(int, terminals[declaration]["emitted_count"]) for declaration in selection.names
    ]
    covered = sum(count > 0 for count in emitted_counts)
    total = len(candidates)
    summary: dict[str, object] = {
        "method_version": METHOD_VERSION,
        "selected_declaration_count": len(selection.names),
        "terminal_declaration_count": len(terminals),
        "successful_declaration_count": terminal_status_counts["complete"],
        "source_text_rejections": {
            "count": len(source_text_rejected_declarations),
            "reason_code": "source_pretty_roundtrip_mismatch",
            "declarations": source_text_rejected_declarations,
        },
        "runner_execution_rejections": {
            "count": len(runner_dispositions),
            "dispositions": runner_dispositions,
        },
        "lean_engine_dispositions": {
            "count": len(lean_engine_dispositions),
            "dispositions": lean_engine_dispositions,
        },
        "total_candidate_count": total,
        "validated_candidate_count": total + duplicate_count,
        "discovered_candidate_count": discovered_count,
        "duplicate_rejection_count": duplicate_count,
        "validation_rejection_count": rejected_count,
        "per_family_counts": dict(sorted(family_counts.items())),
        "per_operation_counts": dict(sorted(operation_counts.items())),
        "per_family_operation_counts": dict(sorted(family_operation_counts.items())),
        "evidence_class_counts": dict(sorted(evidence_counts.items())),
        "terminal_status_counts": dict(sorted(terminal_status_counts.items())),
        "rejection_counts": {
            "duplicate_candidate": duplicate_count,
            "candidate_validation": rejected_count,
            "source_text_roundtrip": len(source_text_rejected_declarations),
            "terminal_error": terminal_status_counts["error"],
            "terminal_notProp": terminal_status_counts["notProp"],
            "terminal_notfound": terminal_status_counts["notfound"],
            "runner_execution": sum(
                terminal_status_counts[status] for status in _RUNNER_TERMINAL_STATUSES
            ),
        },
        "declaration_coverage": {
            "with_candidate": covered,
            "without_candidate": len(selection.names) - covered,
            "share": covered / len(selection.names),
        },
        "nested_candidates": {
            "count": nested_count,
            "share": nested_count / total if total else 0.0,
        },
        "candidate_count_distribution": {
            "mean": statistics.fmean(emitted_counts),
            "median": float(statistics.median(emitted_counts)),
            "p95": _nearest_rank_p95(emitted_counts),
            "max": max(emitted_counts, default=0),
        },
        "batch": batch,
        "selection": selection.manifest_payload(),
    }
    certificates = tuple(
        CandidateCertificate(
            declaration=cast(str, row["declaration"]),
            family=cast(str, row["family"]),
            operation=cast(str, row["operation"]),
            site_path=cast(str, row["site_path"]),
            candidate_type_hash=cast(str, row["candidate_hash"]),
        )
        for row in candidates
    )
    return ParsedProbeOutput(summary=summary, certificates=certificates)


def summarize_lean_output(
    stdout_path: Path,
    *,
    selection: SelectionResult,
    names_path: Path,
) -> dict[str, object]:
    """Fail-closed public summary of the primary line-delimited Lean output."""
    return _parse_probe_output(
        stdout_path,
        selection=selection,
        names_path=names_path,
    ).summary


def verify_audit_output(
    stdout_path: Path,
    *,
    certificates: Sequence[CandidateCertificate],
) -> dict[str, object]:
    """Require exact successful reconstruction for every emitted certificate."""
    _require_regular_file(stdout_path, label="Lean audit stdout")
    expected = {certificate.key for certificate in certificates}
    if len(expected) != len(certificates):
        raise MetaSlice2Error("primary candidates do not have unique audit keys")
    observed: set[tuple[str, str, str, str, str]] = set()
    with stdout_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            context = f"Lean audit stdout line {line_number}"
            row = _parse_json_object(raw_line, context=context)
            if row.get("schemaVersion") != 2:
                raise MetaSlice2Error(f"{context}.schemaVersion must be 2")
            if row.get("kind") != "audit" or row.get("recordKind") != "audit":
                raise MetaSlice2Error(f"{context} is not an audit record")
            declaration = _string_field(row, "declaration", context=context)
            family = _string_field(row, "family", context=context)
            operation = _string_field(row, "operation", context=context)
            site_path = _string_field(row, "sitePath", context=context)
            expected_hash = _hash_field(row, "expectedCandidateTypeHash", context=context)
            actual_hash = _hash_field(row, "actualCandidateTypeHash", context=context)
            key = (declaration, family, operation, site_path, expected_hash)
            if key not in expected:
                raise MetaSlice2Error(f"{context} does not match an emitted candidate")
            if key in observed:
                raise MetaSlice2Error(f"{context} duplicates an independent audit key")
            observed.add(key)
            if actual_hash != expected_hash:
                raise MetaSlice2Error(f"{context} reconstructed a different candidate hash")
            if not _bool_field(row, "verified", context=context):
                raise MetaSlice2Error(f"{context} did not independently verify")
            inverse_fold_verified = _bool_field(
                row,
                "inverseFoldVerified",
                context=context,
            )
            if inverse_fold_verified != (family == "P20"):
                raise MetaSlice2Error(f"{context} has an invalid inverse-fold audit result")
            if row.get("status") != "verified" or row.get("reason") != "verified":
                raise MetaSlice2Error(f"{context} has a non-verified terminal status")
            if row.get("auditMode") != "independent-site-reconstruction":
                raise MetaSlice2Error(f"{context} used an unexpected audit mode")
    if observed != expected:
        missing = sorted(expected.difference(observed))
        raise MetaSlice2Error(f"independent audit is missing {len(missing)} candidate records")
    return {
        "mode": "independent-site-reconstruction",
        "requested_count": len(certificates),
        "verified_count": len(observed),
        "failed_count": 0,
        "coverage": 1.0,
    }


def _privacy_payload() -> dict[str, object]:
    return {
        "public_only": True,
        "private_source_content": False,
        "external_transmission": False,
    }


def _source_state(config: MetaSlice2Config) -> dict[str, object]:
    _require_regular_file(config.transform_engine_path, label="TransformEngine source")
    if config.mathlib_project_path.is_symlink() or not config.mathlib_project_path.is_dir():
        raise MetaSlice2Error("mathlib project must be a non-symlink directory")
    mathlib_revision = (
        _git_revision(config.mathlib_project_path)
        if config.verify_mathlib_revision
        else config.expected_source_revision
    )
    if mathlib_revision != config.expected_source_revision:
        raise MetaSlice2Error(
            "mathlib checkout revision differs from the extraction source revision"
        )
    return {
        "repository_git_revision": _git_revision(_repo_root()),
        "mathlib_git_revision": mathlib_revision,
        "runner_sha256": hash_file(Path(__file__)),
        "transform_engine_sha256": hash_file(config.transform_engine_path),
        "config_sha256": hashlib.sha256(canonical_json_bytes(_config_payload(config))).hexdigest(),
    }


def _validate_source_state(
    config: MetaSlice2Config,
    source_state: Mapping[str, object],
) -> None:
    repository_revision = _string_field(
        source_state,
        "repository_git_revision",
        context="yield-probe source_state",
    )
    if not _git_contains_commit(_repo_root(), repository_revision):
        raise MetaSlice2Error("recorded repository revision is not an available commit")
    if source_state.get("mathlib_git_revision") != config.expected_source_revision:
        raise MetaSlice2Error("recorded mathlib revision differs from the extraction")
    if (
        config.verify_mathlib_revision
        and _git_revision(config.mathlib_project_path) != config.expected_source_revision
    ):
        raise MetaSlice2Error("live mathlib revision differs from the extraction")
    expected = {
        "runner_sha256": hash_file(Path(__file__)),
        "transform_engine_sha256": hash_file(config.transform_engine_path),
        "config_sha256": hashlib.sha256(canonical_json_bytes(_config_payload(config))).hexdigest(),
    }
    for key, value in expected.items():
        if _hash_field(source_state, key, context="yield-probe source_state") != value:
            raise MetaSlice2Error(f"current {key} differs from the recorded source binding")


def _manifest_base(
    config: MetaSlice2Config,
    *,
    selection: SelectionResult,
    source_state: Mapping[str, object],
    started_at: str,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "method_version": METHOD_VERSION,
        "status": "running",
        "started_at": started_at,
        "config": _config_payload(config),
        "selection": selection.manifest_payload(),
        "source_state": dict(source_state),
        "shard_policy": {
            "primary_base_size": PRIMARY_SHARD_SIZE,
            "audit_base_size": AUDIT_SHARD_SIZE,
            "attempt_timeout_policy": _attempt_timeout_policy_payload(config),
            "fallback": "recursive_contiguous_midpoint_bisection",
            "failed_stdout_salvage": False,
            "audit_singleton_failure": "fatal",
        },
        "privacy": _privacy_payload(),
        "attempts": [],
        "outputs": {},
    }


def _ensure_output_root(config: MetaSlice2Config) -> bool:
    config.output_root.parent.mkdir(parents=True, exist_ok=True)
    if config.output_root.exists() or config.output_root.is_symlink():
        if config.output_root.is_symlink() or not config.output_root.is_dir():
            raise MetaSlice2Error("yield-probe output root must be a non-symlink directory")
        return False
    config.output_root.mkdir(mode=0o700)
    return True


@contextmanager
def _exclusive_run_lock(output_root: Path) -> Iterator[None]:
    lock_path = output_root / RUN_LOCK_FILENAME
    if (lock_path.exists() or lock_path.is_symlink()) and (
        lock_path.is_symlink() or not lock_path.is_file()
    ):
        raise MetaSlice2Error("yield-probe run lock must be a regular non-symlink file")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MetaSlice2Error("another yield-probe runner holds the output-root lock") from exc
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _load_bound_manifest(
    config: MetaSlice2Config,
    *,
    require_completed: bool,
) -> dict[str, object]:
    path = config.output_root / MANIFEST_FILENAME
    _require_regular_file(path, label="yield-probe manifest")
    manifest = _parse_json_object(path.read_text(encoding="utf-8"), context="yield-probe manifest")
    if manifest.get("schema_version") != 2 or manifest.get("method_version") != METHOD_VERSION:
        raise MetaSlice2Error("yield-probe manifest schema/method mismatch")
    status = manifest.get("status")
    allowed = {"completed"} if require_completed else {"running", "failure", "completed"}
    if status not in allowed:
        raise MetaSlice2Error(f"yield-probe manifest has unsupported status {status!r}")
    if status == "completed" and set(manifest) != {
        "schema_version",
        "method_version",
        "status",
        "started_at",
        "completed_at",
        "config",
        "selection",
        "source_state",
        "shard_policy",
        "shard_plan",
        "privacy",
        "attempts",
        "outputs",
        "summary",
    }:
        raise MetaSlice2Error("completed yield-probe manifest fields drifted")
    if status in {"running", "failure"}:
        allowed_fields = {
            "schema_version",
            "method_version",
            "status",
            "started_at",
            "config",
            "selection",
            "source_state",
            "shard_policy",
            "privacy",
            "attempts",
            "outputs",
            "updated_at",
            "resumed_at",
            "failed_at",
            "failure",
        }
        if not set(manifest).issubset(allowed_fields):
            raise MetaSlice2Error("resumable yield-probe manifest fields drifted")
    if manifest.get("config") != _config_payload(config):
        raise MetaSlice2Error("yield-probe manifest config differs from the verifier config")
    if manifest.get("privacy") != _privacy_payload():
        raise MetaSlice2Error("yield-probe privacy boundary is not explicit")
    return manifest


def _verify_recorded_inventory_subset(
    config: MetaSlice2Config,
    manifest: Mapping[str, object],
) -> None:
    recorded = _mapping_field(manifest, "outputs", context="yield-probe manifest")
    for relative, raw_entry in recorded.items():
        if relative in {MANIFEST_FILENAME, RUN_LOCK_FILENAME}:
            raise MetaSlice2Error(f"manifest cannot inventory mutable control file {relative}")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise MetaSlice2Error(f"unsafe output inventory path {relative}")
        entry = _mapping_field({"entry": raw_entry}, "entry", context=f"output {relative}")
        path = config.output_root / relative_path
        if entry.get("path") != str(_resolve(path)):
            raise MetaSlice2Error(f"output path binding mismatch for {relative}")
        expected_hash = _hash_field(entry, "sha256", context=f"output {relative}")
        expected_size = _nonnegative_int_field(entry, "size_bytes", context=f"output {relative}")
        _require_regular_file(path, label=f"output {relative}")
        if path.stat().st_size != expected_size or hash_file(path) != expected_hash:
            raise MetaSlice2Error(f"output artifact drift for {relative}")


def _verify_recorded_attempt_subset(
    config: MetaSlice2Config,
    manifest: Mapping[str, object],
) -> None:
    raw_attempts = _list_field(manifest, "attempts", context="yield-probe manifest")
    current = {cast(str, row["attempt_id"]): row for row in _manifest_attempts(config)}
    recorded_ids: list[str] = []
    for index, raw_attempt in enumerate(raw_attempts):
        attempt = _mapping_field(
            {"attempt": raw_attempt},
            "attempt",
            context=f"yield-probe attempt {index}",
        )
        attempt_id = _string_field(attempt, "attempt_id", context="yield-probe attempt")
        recorded_ids.append(attempt_id)
        if current.get(attempt_id) != attempt:
            raise MetaSlice2Error(f"recorded attempt inventory drift for {attempt_id}")
    if recorded_ids != sorted(set(recorded_ids)):
        raise MetaSlice2Error("recorded attempt inventory is not unique and sorted")


def _write_or_verify(path: Path, payload: bytes, *, label: str) -> None:
    if path.exists() or path.is_symlink():
        _require_regular_file(path, label=label)
        if path.read_bytes() != payload:
            raise MetaSlice2Error(f"{label} differs from deterministic replay")
        return
    _write_atomic(path, payload)


def _selection_slice(selection: SelectionResult, names: Sequence[str]) -> SelectionResult:
    return SelectionResult(
        names=tuple(names),
        theorem_rows=selection.theorem_rows,
        eligible_rows=selection.eligible_rows,
        eligible_unique_names=selection.eligible_unique_names,
        duplicate_eligible_names=selection.duplicate_eligible_names,
        excluded_transform_ineligible=selection.excluded_transform_ineligible,
        excluded_private=selection.excluded_private,
    )


def _certificate_payload(certificate: CandidateCertificate) -> dict[str, object]:
    return {
        "declaration": certificate.declaration,
        "family": certificate.family,
        "operation": certificate.operation,
        "site_path": certificate.site_path,
        "candidate_type_hash": certificate.candidate_type_hash,
    }


def _certificates_bytes(certificates: Sequence[CandidateCertificate]) -> bytes:
    return b"".join(
        canonical_json_bytes(_certificate_payload(certificate)) + b"\n"
        for certificate in certificates
    )


def _jsonl_bytes(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _base_ranges(count: int, size: int) -> tuple[tuple[int, int], ...]:
    return tuple((start, min(start + size, count)) for start in range(0, count, size))


def _attempt_dir(config: MetaSlice2Config, spec: _AttemptSpec) -> Path:
    return config.output_root / SHARDS_DIRNAME / spec.attempt_id


def _attempt_result_path(config: MetaSlice2Config, spec: _AttemptSpec) -> Path:
    return _attempt_dir(config, spec) / ATTEMPT_RESULT_FILENAME


def _attempt_bindings(
    config: MetaSlice2Config,
    *,
    attempt_timeout_seconds: int,
) -> dict[str, object]:
    return {
        "config_sha256": hashlib.sha256(canonical_json_bytes(_config_payload(config))).hexdigest(),
        "runner_sha256": hash_file(Path(__file__)),
        "transform_engine_sha256": hash_file(config.transform_engine_path),
        "source_revision": config.expected_source_revision,
        "theorem_store_sha256": config.theorem_store_sha256,
        "extraction_manifest_sha256": config.extraction_manifest_sha256,
        "attempt_timeout_seconds": attempt_timeout_seconds,
    }


def _attempt_artifact_inventory(
    attempt_dir: Path,
    *,
    input_filename: str,
) -> dict[str, object]:
    names = {
        input_filename,
        ATTEMPT_DRIVER_FILENAME,
        ATTEMPT_STDOUT_FILENAME,
        ATTEMPT_STDERR_FILENAME,
        ATTEMPT_LOG_FILENAME,
        ATTEMPT_PROCESS_FILENAME,
    }
    actual = {path.name for path in attempt_dir.iterdir() if path.name != ATTEMPT_RESULT_FILENAME}
    if actual != names:
        raise MetaSlice2Error(
            f"attempt artifact inventory differs: expected={sorted(names)}, actual={sorted(actual)}"
        )
    result: dict[str, object] = {}
    for name in sorted(names):
        result[name] = _artifact(attempt_dir / name)
    return result


def _primary_rows_and_validation(
    stdout_path: Path,
    *,
    selection: SelectionResult,
    names_path: Path,
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    parsed = _parse_probe_output(stdout_path, selection=selection, names_path=names_path)
    rows_by_declaration: dict[str, list[dict[str, object]]] = {name: [] for name in selection.names}
    with stdout_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            row = _parse_json_object(raw_line, context=f"Lean stdout line {line_number}")
            if row.get("kind") == "batch":
                continue
            declaration = _string_field(
                row, "declaration", context=f"Lean stdout line {line_number}"
            )
            rows_by_declaration[declaration].append(row)
    rows = tuple(row for name in selection.names for row in rows_by_declaration[name])
    batch = _mapping_field(parsed.summary, "batch", context="primary attempt summary")
    validation = {
        "candidate_count": len(parsed.certificates),
        "terminal_count": len(selection.names),
        "batch_status": batch.get("status"),
        "summary_sha256": hashlib.sha256(canonical_json_bytes(parsed.summary)).hexdigest(),
        "certificates_sha256": hashlib.sha256(_certificates_bytes(parsed.certificates)).hexdigest(),
    }
    return rows, validation


def _audit_rows_and_validation(
    stdout_path: Path,
    *,
    certificates: Sequence[CandidateCertificate],
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    summary = verify_audit_output(stdout_path, certificates=certificates)
    expected = {certificate.key for certificate in certificates}
    by_key: dict[tuple[str, str, str, str, str], dict[str, object]] = {}
    with stdout_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            row = _parse_json_object(raw_line, context=f"Lean audit stdout line {line_number}")
            key = (
                _string_field(row, "declaration", context="audit row"),
                _string_field(row, "family", context="audit row"),
                _string_field(row, "operation", context="audit row"),
                _string_field(row, "sitePath", context="audit row"),
                _hash_field(row, "expectedCandidateTypeHash", context="audit row"),
            )
            if key not in expected or key in by_key:
                raise MetaSlice2Error("audit row ordering projection is not one-to-one")
            by_key[key] = row
    rows = tuple(by_key[certificate.key] for certificate in certificates)
    validation = {
        "candidate_count": len(certificates),
        "verified_count": len(rows),
        "summary_sha256": hashlib.sha256(canonical_json_bytes(summary)).hexdigest(),
        "certificates_sha256": hashlib.sha256(_certificates_bytes(certificates)).hexdigest(),
    }
    return rows, validation


def _attempt_validation(
    spec: _AttemptSpec,
    stdout_path: Path,
    input_path: Path,
    *,
    selection: SelectionResult | None,
    certificates: Sequence[CandidateCertificate] | None,
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    if spec.stage == "primary":
        if selection is None or certificates is not None:
            raise AssertionError("primary attempt validation lacks its selection")
        return _primary_rows_and_validation(
            stdout_path,
            selection=selection,
            names_path=input_path,
        )
    if selection is not None or certificates is None:
        raise AssertionError("audit attempt validation lacks its certificates")
    return _audit_rows_and_validation(stdout_path, certificates=certificates)


def _validation_failure(
    spec: _AttemptSpec,
    stdout_path: Path,
    input_path: Path,
    *,
    selection: SelectionResult | None,
    certificates: Sequence[CandidateCertificate] | None,
) -> str | None:
    try:
        _attempt_validation(
            spec,
            stdout_path,
            input_path,
            selection=selection,
            certificates=certificates,
        )
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _manifest_attempts(config: MetaSlice2Config) -> list[dict[str, object]]:
    shards_root = config.output_root / SHARDS_DIRNAME
    if not shards_root.exists():
        return []
    if shards_root.is_symlink() or not shards_root.is_dir():
        raise MetaSlice2Error("shards root must be a non-symlink directory")
    attempts: list[dict[str, object]] = []
    for result_path in sorted(shards_root.glob(f"attempt-*/{ATTEMPT_RESULT_FILENAME}")):
        result = _parse_json_object(
            result_path.read_text(encoding="utf-8"), context="attempt result"
        )
        attempts.append(
            {
                "attempt_id": _string_field(result, "attempt_id", context="attempt result"),
                "stage": _string_field(result, "stage", context="attempt result"),
                "outcome": _string_field(result, "outcome", context="attempt result"),
                "result_path": result_path.relative_to(config.output_root).as_posix(),
                "result_sha256": hash_file(result_path),
                "result_size_bytes": result_path.stat().st_size,
            }
        )
    return sorted(attempts, key=lambda row: cast(str, row["attempt_id"]))


def _record_progress(context: _RunContext) -> None:
    if context.manifest is None:
        return
    context.manifest.update(
        {
            "status": "running",
            "updated_at": _utc_now(),
            "attempts": _manifest_attempts(context.config),
            "outputs": _progress_artifact_inventory(context.config.output_root),
        }
    )
    context.manifest.pop("failure", None)
    context.manifest.pop("failed_at", None)
    _write_json_atomic(context.config.output_root / MANIFEST_FILENAME, context.manifest)


def _attempt_result_payload(
    context: _RunContext,
    spec: _AttemptSpec,
    material: _AttemptMaterial,
    *,
    attempt_timeout_seconds: int,
    started_at: str,
    outcome: str,
    execution: Mapping[str, object] | None,
    validation: Mapping[str, object] | None,
    failure: Mapping[str, object] | None,
) -> dict[str, object]:
    attempt_dir = _attempt_dir(context.config, spec)
    if outcome == "abandoned":
        resolution = "interrupted_retry"
    elif outcome == "accepted":
        resolution = "leaf"
    elif material.item_count > 1:
        resolution = "bisect"
    elif spec.stage == "primary":
        resolution = "runner_terminal"
    else:
        resolution = "fatal"
    return {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "stage": spec.stage,
        "logical_id": spec.logical_id,
        "attempt_id": spec.attempt_id,
        "attempt_ordinal": spec.ordinal,
        "parent_attempt_id": spec.parent_attempt_id,
        "range": {"start": spec.start, "stop": spec.stop},
        "item_count": material.item_count,
        "timeout_seconds": attempt_timeout_seconds,
        "input": {
            "filename": material.input_filename,
            "sha256": hashlib.sha256(material.input_bytes).hexdigest(),
        },
        "bindings": _attempt_bindings(
            context.config,
            attempt_timeout_seconds=attempt_timeout_seconds,
        ),
        "started_at": started_at,
        "finished_at": _utc_now(),
        "outcome": outcome,
        "resolution": resolution,
        "execution": dict(execution) if execution is not None else None,
        "validation": dict(validation) if validation is not None else None,
        "failure": dict(failure) if failure is not None else None,
        "artifacts": _attempt_artifact_inventory(
            attempt_dir,
            input_filename=material.input_filename,
        ),
    }


def _ensure_attempt_regular_file(path: Path) -> None:
    if not path.exists():
        _write_atomic(path, b"")
    _require_regular_file(path, label="attempt artifact")


def _validate_process_state(
    path: Path,
    *,
    config: MetaSlice2Config,
    spec: _AttemptSpec,
    command: Sequence[str],
    attempt_timeout_seconds: int,
) -> dict[str, object]:
    _require_regular_file(path, label=f"{spec.attempt_id} process state")
    state = _parse_json_object(path.read_text(encoding="utf-8"), context="attempt process state")
    if set(state) != {
        "schema_version",
        "attempt_id",
        "command",
        "command_sha256",
        "cwd",
        "timeout_seconds",
        "started_at",
        "updated_at",
        "phase",
        "pid",
        "process_group_id",
        "process_start_ticks",
        "boot_id",
        "returncode",
        "timed_out",
        "interrupted",
        "term_sent",
        "kill_sent",
        "group_gone",
    }:
        raise MetaSlice2Error(f"process-state fields drifted for {spec.attempt_id}")
    expected_command = list(command)
    if (
        state.get("schema_version") != 1
        or state.get("attempt_id") != spec.attempt_id
        or state.get("command") != expected_command
        or state.get("command_sha256")
        != hashlib.sha256(canonical_json_bytes(expected_command)).hexdigest()
        or state.get("cwd") != str(_resolve(config.mathlib_project_path))
        or state.get("timeout_seconds") != attempt_timeout_seconds
    ):
        raise MetaSlice2Error(f"process-state execution binding drift for {spec.attempt_id}")
    _string_field(state, "started_at", context="attempt process state")
    _string_field(state, "updated_at", context="attempt process state")
    phase = _string_field(state, "phase", context="attempt process state")
    if phase not in {"prepared", "running", "cleanup_pending", "finished", "recovered"}:
        raise MetaSlice2Error(f"process-state phase is invalid for {spec.attempt_id}")
    pid = state.get("pid")
    process_group_id = state.get("process_group_id")
    process_start_ticks = state.get("process_start_ticks")
    boot_id = state.get("boot_id")
    if pid is None:
        identity_absent_allowed = phase in {"prepared", "recovered"} or (
            phase == "finished" and not config.enforce_production_bindings
        )
        if (
            not identity_absent_allowed
            or process_group_id is not None
            or process_start_ticks is not None
            or not isinstance(boot_id, str)
            or not boot_id
        ):
            raise MetaSlice2Error(f"process-state identity is absent for {spec.attempt_id}")
    elif (
        type(pid) is not int
        or pid <= 1
        or type(process_group_id) is not int
        or process_group_id != pid
        or type(process_start_ticks) is not int
        or process_start_ticks < 0
        or not isinstance(boot_id, str)
        or not boot_id
    ):
        raise MetaSlice2Error(f"process-state identity is malformed for {spec.attempt_id}")
    if (phase == "prepared" and pid is not None) or (phase == "running" and pid is None):
        raise MetaSlice2Error(f"process-state phase/identity mismatch for {spec.attempt_id}")
    returncode = state.get("returncode")
    if returncode is not None and type(returncode) is not int:
        raise MetaSlice2Error(f"process-state return code is malformed for {spec.attempt_id}")
    for key in ("timed_out", "interrupted", "term_sent", "kill_sent", "group_gone"):
        _bool_field(state, key, context="attempt process state")
    if state.get("kill_sent") is True and state.get("term_sent") is not True:
        raise MetaSlice2Error(f"process-state kill evidence is malformed for {spec.attempt_id}")
    if phase in {"prepared", "running"} and (
        returncode is not None
        or state.get("timed_out") is not False
        or state.get("interrupted") is not False
        or state.get("term_sent") is not False
        or state.get("kill_sent") is not False
        or state.get("group_gone") is not False
    ):
        raise MetaSlice2Error(f"pending process-state evidence is malformed for {spec.attempt_id}")
    if phase in {"finished", "recovered"} and state.get("group_gone") is not True:
        raise MetaSlice2Error(f"finished process group is not confirmed gone for {spec.attempt_id}")
    return state


def _terminate_recorded_process_group(process_group_id: int) -> tuple[bool, bool, bool]:
    if not _process_group_exists(process_group_id):
        return False, False, True
    term_sent = False
    kill_sent = False
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGTERM)
        term_sent = True
    deadline = time.monotonic() + 5
    while _process_group_exists(process_group_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _process_group_exists(process_group_id):
        with suppress(ProcessLookupError):
            os.killpg(process_group_id, signal.SIGKILL)
            kill_sent = True
        deadline = time.monotonic() + 5
        while _process_group_exists(process_group_id) and time.monotonic() < deadline:
            time.sleep(0.05)
    return term_sent, kill_sent, not _process_group_exists(process_group_id)


def _recorded_group_members(process_group_id: int) -> tuple[tuple[int, int, str, Path], ...]:
    members: list[tuple[int, int, str, Path]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            observed_group, start_ticks = _process_stat(pid)
            if observed_group != process_group_id:
                continue
            command = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", errors="replace")
            )
            cwd = (entry / "cwd").resolve(strict=True)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        members.append((pid, start_ticks, command, cwd))
    return tuple(sorted(members))


def _driver_process_matches(
    *,
    expected_driver: str,
    expected_cwd: Path,
) -> tuple[tuple[int, int, int], ...]:
    matches: list[tuple[int, int, int]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            process_group_id, start_ticks = _process_stat(pid)
            command = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", errors="replace")
            )
            cwd = (entry / "cwd").resolve(strict=True)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if expected_driver in command and cwd == expected_cwd:
            matches.append((pid, process_group_id, start_ticks))
    return tuple(sorted(matches))


def _recover_recorded_process(
    context: _RunContext,
    spec: _AttemptSpec,
    process_path: Path,
    command: Sequence[str],
    attempt_timeout_seconds: int,
) -> None:
    if not process_path.exists():
        _write_json_atomic(
            process_path,
            _process_state_payload(
                attempt_id=spec.attempt_id,
                command=command,
                cwd=context.config.mathlib_project_path,
                timeout_seconds=attempt_timeout_seconds,
                started_at=_utc_now(),
                phase="prepared",
                pid=None,
                process_group_id=None,
                process_start_ticks=None,
                boot_id=_boot_id(),
                returncode=None,
                timed_out=False,
                interrupted=False,
                term_sent=False,
                kill_sent=False,
                group_gone=False,
            ),
        )
    state = _validate_process_state(
        process_path,
        config=context.config,
        spec=spec,
        command=command,
        attempt_timeout_seconds=attempt_timeout_seconds,
    )
    pid = state.get("pid")
    process_group_id = state.get("process_group_id")
    if pid is None:
        if state.get("phase") == "finished" and state.get("group_gone") is True:
            return
        expected_driver = str(
            _resolve(_attempt_dir(context.config, spec) / ATTEMPT_DRIVER_FILENAME)
        )
        expected_cwd = _resolve(context.config.mathlib_project_path)
        matches = _driver_process_matches(
            expected_driver=expected_driver,
            expected_cwd=expected_cwd,
        )
        groups = {match[1] for match in matches}
        if len(groups) > 1:
            raise MetaSlice2Error(
                f"multiple live process groups match interrupted attempt {spec.attempt_id}"
            )
        if not groups:
            recovered_pid: int | None = None
            recovered_group: int | None = None
            recovered_start: int | None = None
            term_sent = False
            kill_sent = False
            group_gone = True
        else:
            recovered_group = groups.pop()
            members = _recorded_group_members(recovered_group)
            if not members or any(member[3] != expected_cwd for member in members):
                raise MetaSlice2Error(
                    f"discovered process group identity mismatch: {spec.attempt_id}"
                )
            leader = next((member for member in members if member[0] == recovered_group), None)
            recovered_pid = recovered_group
            recovered_start = (
                leader[1] if leader is not None else min(member[1] for member in members)
            )
            term_sent, kill_sent, group_gone = _terminate_recorded_process_group(recovered_group)
            if not group_gone:
                raise MetaSlice2Error(
                    f"discovered process group survived cleanup: {spec.attempt_id}"
                )
        _write_json_atomic(
            process_path,
            _process_state_payload(
                attempt_id=spec.attempt_id,
                command=command,
                cwd=context.config.mathlib_project_path,
                timeout_seconds=attempt_timeout_seconds,
                started_at=cast(str, state["started_at"]),
                phase="recovered",
                pid=recovered_pid,
                process_group_id=recovered_group,
                process_start_ticks=recovered_start,
                boot_id=_boot_id(),
                returncode=None,
                timed_out=False,
                interrupted=True,
                term_sent=term_sent,
                kill_sent=kill_sent,
                group_gone=group_gone,
            ),
        )
        return
    assert type(pid) is int
    assert type(process_group_id) is int
    if not _process_group_exists(process_group_id):
        term_sent = False
        kill_sent = False
        group_gone = True
    else:
        if state.get("boot_id") != _boot_id():
            raise MetaSlice2Error(
                f"cannot clean process group from an earlier boot: {spec.attempt_id}"
            )
        members = _recorded_group_members(process_group_id)
        if not members:
            raise MetaSlice2Error(
                f"live process group has no inspectable members: {spec.attempt_id}"
            )
        expected_driver = str(
            _resolve(_attempt_dir(context.config, spec) / ATTEMPT_DRIVER_FILENAME)
        )
        expected_cwd = _resolve(context.config.mathlib_project_path)
        start_ticks = state.get("process_start_ticks")
        assert type(start_ticks) is int
        leader = next((member for member in members if member[0] == pid), None)
        if leader is not None and (leader[1] != start_ticks or expected_driver not in leader[2]):
            raise MetaSlice2Error(f"recorded process identity was reused: {spec.attempt_id}")
        if any(
            member_start < start_ticks or member_cwd != expected_cwd
            for _, member_start, _, member_cwd in members
        ):
            raise MetaSlice2Error(f"recorded process group identity mismatch: {spec.attempt_id}")
        term_sent, kill_sent, group_gone = _terminate_recorded_process_group(process_group_id)
        if not group_gone:
            raise MetaSlice2Error(f"recorded process group survived cleanup: {spec.attempt_id}")
    _write_json_atomic(
        process_path,
        _process_state_payload(
            attempt_id=spec.attempt_id,
            command=command,
            cwd=context.config.mathlib_project_path,
            timeout_seconds=attempt_timeout_seconds,
            started_at=cast(str, state["started_at"]),
            phase="recovered",
            pid=pid,
            process_group_id=process_group_id,
            process_start_ticks=cast(int, state["process_start_ticks"]),
            boot_id=cast(str, state["boot_id"]),
            returncode=cast(int | None, state["returncode"]),
            timed_out=cast(bool, state["timed_out"]),
            interrupted=True,
            term_sent=cast(bool, state["term_sent"]) or term_sent,
            kill_sent=cast(bool, state["kill_sent"]) or kill_sent,
            group_gone=group_gone,
        ),
    )


def _recover_interrupted_attempt(
    context: _RunContext,
    spec: _AttemptSpec,
    material: _AttemptMaterial,
) -> None:
    if material.item_count != spec.stop - spec.start:
        raise MetaSlice2Error(f"attempt item count differs from its range: {spec.attempt_id}")
    attempt_timeout_seconds = _attempt_timeout_seconds(
        context.config,
        stage=spec.stage,
        item_count=material.item_count,
    )
    attempt_dir = _attempt_dir(context.config, spec)
    allowed = {
        material.input_filename,
        ATTEMPT_DRIVER_FILENAME,
        ATTEMPT_STDOUT_FILENAME,
        ATTEMPT_STDERR_FILENAME,
        ATTEMPT_LOG_FILENAME,
        ATTEMPT_PROCESS_FILENAME,
    }
    actual = {path.name for path in attempt_dir.iterdir()}
    if not actual.issubset(allowed):
        raise MetaSlice2Error(f"interrupted attempt {spec.attempt_id} has unexpected artifacts")
    _write_or_verify(
        attempt_dir / material.input_filename,
        material.input_bytes,
        label=f"{spec.attempt_id} input",
    )
    _write_or_verify(
        attempt_dir / ATTEMPT_DRIVER_FILENAME,
        material.driver_bytes,
        label=f"{spec.attempt_id} driver",
    )
    stdout_path = attempt_dir / ATTEMPT_STDOUT_FILENAME
    stderr_path = attempt_dir / ATTEMPT_STDERR_FILENAME
    _ensure_attempt_regular_file(stdout_path)
    _ensure_attempt_regular_file(stderr_path)
    command = _command(context.config, attempt_dir / ATTEMPT_DRIVER_FILENAME)
    _recover_recorded_process(
        context,
        spec,
        attempt_dir / ATTEMPT_PROCESS_FILENAME,
        command,
        attempt_timeout_seconds,
    )
    log_path = attempt_dir / ATTEMPT_LOG_FILENAME
    if not log_path.exists():
        _write_atomic(
            log_path,
            _log_bytes(command, stdout_path, stderr_path),
        )
    _require_regular_file(log_path, label="interrupted attempt log")
    result = _attempt_result_payload(
        context,
        spec,
        material,
        attempt_timeout_seconds=attempt_timeout_seconds,
        started_at=_utc_now(),
        outcome="abandoned",
        execution=None,
        validation=None,
        failure={"reason_code": "interrupted_before_atomic_result"},
    )
    _write_json_atomic(_attempt_result_path(context.config, spec), result)
    _record_progress(context)


def _run_new_attempt(
    context: _RunContext,
    spec: _AttemptSpec,
    material: _AttemptMaterial,
    *,
    selection: SelectionResult | None,
    certificates: Sequence[CandidateCertificate] | None,
) -> _AttemptOutcome:
    if context.executor is None:
        raise MetaSlice2Error(f"missing attempt cannot be replayed: {spec.attempt_id}")
    if material.item_count != spec.stop - spec.start:
        raise MetaSlice2Error(f"attempt item count differs from its range: {spec.attempt_id}")
    attempt_timeout_seconds = _attempt_timeout_seconds(
        context.config,
        stage=spec.stage,
        item_count=material.item_count,
    )
    attempt_dir = _attempt_dir(context.config, spec)
    attempt_dir.parent.mkdir(parents=True, exist_ok=True)
    attempt_dir.mkdir(mode=0o700)
    input_path = attempt_dir / material.input_filename
    driver_path = attempt_dir / ATTEMPT_DRIVER_FILENAME
    stdout_path = attempt_dir / ATTEMPT_STDOUT_FILENAME
    stderr_path = attempt_dir / ATTEMPT_STDERR_FILENAME
    log_path = attempt_dir / ATTEMPT_LOG_FILENAME
    process_path = attempt_dir / ATTEMPT_PROCESS_FILENAME
    _write_atomic(input_path, material.input_bytes)
    _write_atomic(driver_path, material.driver_bytes)
    command = _command(context.config, driver_path)
    started_at = _utc_now()
    _write_json_atomic(
        process_path,
        _process_state_payload(
            attempt_id=spec.attempt_id,
            command=command,
            cwd=context.config.mathlib_project_path,
            timeout_seconds=attempt_timeout_seconds,
            started_at=started_at,
            phase="prepared",
            pid=None,
            process_group_id=None,
            process_start_ticks=None,
            boot_id=_boot_id(),
            returncode=None,
            timed_out=False,
            interrupted=False,
            term_sent=False,
            kill_sent=False,
            group_gone=False,
        ),
    )
    _record_progress(context)
    result = context.executor.run(
        command=command,
        cwd=context.config.mathlib_project_path,
        timeout_seconds=attempt_timeout_seconds,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        process_state_path=process_path,
        attempt_id=spec.attempt_id,
    )
    process_phase: object = None
    if process_path.exists() and not process_path.is_symlink():
        process_phase = _parse_json_object(
            process_path.read_text(encoding="utf-8"),
            context="attempt process state",
        ).get("phase")
    if not process_path.exists() or process_phase == "prepared":
        if context.config.enforce_production_bindings:
            raise MetaSlice2Error("production executor did not persist process identity")
        _write_json_atomic(
            process_path,
            _process_state_payload(
                attempt_id=spec.attempt_id,
                command=command,
                cwd=context.config.mathlib_project_path,
                timeout_seconds=attempt_timeout_seconds,
                started_at=started_at,
                phase="finished",
                pid=result.pid,
                process_group_id=result.process_group_id,
                process_start_ticks=result.process_start_ticks,
                boot_id=result.boot_id or "fixture",
                returncode=result.returncode,
                timed_out=result.timed_out,
                interrupted=False,
                term_sent=result.term_sent,
                kill_sent=result.kill_sent,
                group_gone=result.group_gone,
            ),
        )
    process_state = _validate_process_state(
        process_path,
        config=context.config,
        spec=spec,
        command=command,
        attempt_timeout_seconds=attempt_timeout_seconds,
    )
    if (
        process_state.get("phase") != "finished"
        or process_state.get("pid") != result.pid
        or process_state.get("process_group_id") != result.process_group_id
        or process_state.get("process_start_ticks") != result.process_start_ticks
        or process_state.get("boot_id") != (result.boot_id or "fixture")
        or process_state.get("returncode") != result.returncode
        or process_state.get("timed_out") != result.timed_out
        or process_state.get("interrupted") is not False
        or process_state.get("term_sent") != result.term_sent
        or process_state.get("kill_sent") != result.kill_sent
        or process_state.get("group_gone") != result.group_gone
        or not result.group_gone
    ):
        raise MetaSlice2Error("executor process-state result does not reconcile")
    _ensure_attempt_regular_file(stdout_path)
    _ensure_attempt_regular_file(stderr_path)
    _write_atomic(log_path, _log_bytes(command, stdout_path, stderr_path))
    if not math.isfinite(result.elapsed_seconds) or result.elapsed_seconds < 0:
        raise MetaSlice2Error("executor returned an invalid elapsed time")
    if result.timed_out:
        if result.returncode is not None:
            raise MetaSlice2Error("timed-out executor result cannot have a return code")
        outcome = "timeout"
        validation: dict[str, object] | None = None
        failure: dict[str, object] | None = {
            "reason_code": "timeout",
            "detail": f"timed out after {attempt_timeout_seconds}s",
        }
    elif result.returncode != 0:
        if type(result.returncode) is not int:
            raise MetaSlice2Error("non-timeout executor result must have an integer return code")
        outcome = "nonzero_exit"
        validation = None
        failure = {
            "reason_code": "nonzero_exit",
            "detail": f"Lean exited with status {result.returncode}",
        }
    else:
        validation_error = _validation_failure(
            spec,
            stdout_path,
            input_path,
            selection=selection,
            certificates=certificates,
        )
        if validation_error is None:
            _, validation = _attempt_validation(
                spec,
                stdout_path,
                input_path,
                selection=selection,
                certificates=certificates,
            )
            outcome = "accepted"
            failure = None
        else:
            outcome = "invalid_output"
            validation = None
            failure = {
                "reason_code": "invalid_output",
                "detail": validation_error,
                "detail_sha256": _sha256_text(validation_error),
            }
    execution = {
        "command": list(command),
        "cwd": str(_resolve(context.config.mathlib_project_path)),
        "timeout_seconds": attempt_timeout_seconds,
        "stdin": "closed",
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "elapsed_seconds": result.elapsed_seconds,
        "pid": result.pid,
        "process_group_id": result.process_group_id,
        "process_start_ticks": result.process_start_ticks,
        "boot_id": result.boot_id or "fixture",
        "term_sent": result.term_sent,
        "kill_sent": result.kill_sent,
        "group_gone": result.group_gone,
    }
    payload = _attempt_result_payload(
        context,
        spec,
        material,
        attempt_timeout_seconds=attempt_timeout_seconds,
        started_at=started_at,
        outcome=outcome,
        execution=execution,
        validation=validation,
        failure=failure,
    )
    result_path = _attempt_result_path(context.config, spec)
    _write_json_atomic(result_path, payload)
    _record_progress(context)
    return _AttemptOutcome(spec=spec, result=payload, result_path=result_path)


def _validate_attempt_result(
    context: _RunContext,
    spec: _AttemptSpec,
    material: _AttemptMaterial,
    *,
    selection: SelectionResult | None,
    certificates: Sequence[CandidateCertificate] | None,
) -> _AttemptOutcome:
    if material.item_count != spec.stop - spec.start:
        raise MetaSlice2Error(f"attempt item count differs from its range: {spec.attempt_id}")
    attempt_timeout_seconds = _attempt_timeout_seconds(
        context.config,
        stage=spec.stage,
        item_count=material.item_count,
    )
    result_path = _attempt_result_path(context.config, spec)
    _require_regular_file(result_path, label=f"{spec.attempt_id} result")
    result = _parse_json_object(result_path.read_text(encoding="utf-8"), context="attempt result")
    expected_result_keys = {
        "schema_version",
        "method_version",
        "stage",
        "logical_id",
        "attempt_id",
        "attempt_ordinal",
        "parent_attempt_id",
        "range",
        "item_count",
        "timeout_seconds",
        "input",
        "bindings",
        "started_at",
        "finished_at",
        "outcome",
        "resolution",
        "execution",
        "validation",
        "failure",
        "artifacts",
    }
    if set(result) != expected_result_keys:
        raise MetaSlice2Error(f"attempt result fields drifted for {spec.attempt_id}")
    exact = {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "stage": spec.stage,
        "logical_id": spec.logical_id,
        "attempt_id": spec.attempt_id,
        "attempt_ordinal": spec.ordinal,
        "parent_attempt_id": spec.parent_attempt_id,
        "range": {"start": spec.start, "stop": spec.stop},
        "item_count": material.item_count,
        "timeout_seconds": attempt_timeout_seconds,
        "input": {
            "filename": material.input_filename,
            "sha256": hashlib.sha256(material.input_bytes).hexdigest(),
        },
        "bindings": _attempt_bindings(
            context.config,
            attempt_timeout_seconds=attempt_timeout_seconds,
        ),
    }
    outcome = _string_field(result, "outcome", context="attempt result")
    if outcome == "abandoned":
        expected_resolution = "interrupted_retry"
    elif outcome == "accepted":
        expected_resolution = "leaf"
    elif material.item_count > 1:
        expected_resolution = "bisect"
    elif spec.stage == "primary":
        expected_resolution = "runner_terminal"
    else:
        expected_resolution = "fatal"
    exact["resolution"] = expected_resolution
    for key, value in exact.items():
        if result.get(key) != value:
            raise MetaSlice2Error(f"attempt result binding drift for {spec.attempt_id}.{key}")
    _string_field(result, "started_at", context="attempt result")
    _string_field(result, "finished_at", context="attempt result")
    attempt_dir = _attempt_dir(context.config, spec)
    input_path = attempt_dir / material.input_filename
    driver_path = attempt_dir / ATTEMPT_DRIVER_FILENAME
    stdout_path = attempt_dir / ATTEMPT_STDOUT_FILENAME
    stderr_path = attempt_dir / ATTEMPT_STDERR_FILENAME
    log_path = attempt_dir / ATTEMPT_LOG_FILENAME
    process_path = attempt_dir / ATTEMPT_PROCESS_FILENAME
    for label, path in (
        ("input", input_path),
        ("driver", driver_path),
        ("stdout", stdout_path),
        ("stderr", stderr_path),
        ("log", log_path),
        ("process state", process_path),
    ):
        _require_regular_file(path, label=f"{spec.attempt_id} {label}")
    if input_path.read_bytes() != material.input_bytes:
        raise MetaSlice2Error(f"attempt input drift for {spec.attempt_id}")
    if driver_path.read_bytes() != material.driver_bytes:
        raise MetaSlice2Error(f"attempt driver drift for {spec.attempt_id}")
    command = _command(context.config, driver_path)
    process_state = _validate_process_state(
        process_path,
        config=context.config,
        spec=spec,
        command=command,
        attempt_timeout_seconds=attempt_timeout_seconds,
    )
    if log_path.read_bytes() != _log_bytes(command, stdout_path, stderr_path):
        raise MetaSlice2Error(f"attempt log drift for {spec.attempt_id}")
    artifacts = _mapping_field(result, "artifacts", context="attempt result")
    if artifacts != _attempt_artifact_inventory(
        attempt_dir,
        input_filename=material.input_filename,
    ):
        raise MetaSlice2Error(f"attempt artifact hashes drifted for {spec.attempt_id}")
    execution_raw = result.get("execution")
    validation_raw = result.get("validation")
    failure_raw = result.get("failure")
    if outcome == "abandoned":
        if (
            execution_raw is not None
            or validation_raw is not None
            or failure_raw != {"reason_code": "interrupted_before_atomic_result"}
            or process_state.get("phase") not in {"finished", "recovered"}
            or process_state.get("group_gone") is not True
        ):
            raise MetaSlice2Error(f"abandoned attempt metadata is malformed for {spec.attempt_id}")
        return _AttemptOutcome(spec=spec, result=result, result_path=result_path)
    execution = _mapping_field(result, "execution", context="attempt result")
    if set(execution) != {
        "command",
        "cwd",
        "timeout_seconds",
        "stdin",
        "returncode",
        "timed_out",
        "elapsed_seconds",
        "pid",
        "process_group_id",
        "process_start_ticks",
        "boot_id",
        "term_sent",
        "kill_sent",
        "group_gone",
    }:
        raise MetaSlice2Error(f"attempt execution fields drifted for {spec.attempt_id}")
    if (
        execution.get("command") != list(command)
        or execution.get("cwd") != str(_resolve(context.config.mathlib_project_path))
        or execution.get("timeout_seconds") != attempt_timeout_seconds
        or execution.get("stdin") != "closed"
    ):
        raise MetaSlice2Error(f"attempt execution binding drift for {spec.attempt_id}")
    elapsed = execution.get("elapsed_seconds")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed))
        or elapsed < 0
    ):
        raise MetaSlice2Error(f"attempt elapsed time is malformed for {spec.attempt_id}")
    timed_out = execution.get("timed_out")
    returncode = execution.get("returncode")
    if not isinstance(timed_out, bool) or (returncode is not None and type(returncode) is not int):
        raise MetaSlice2Error(f"attempt process outcome is malformed for {spec.attempt_id}")
    pid = execution.get("pid")
    process_group_id = execution.get("process_group_id")
    process_start_ticks = execution.get("process_start_ticks")
    boot_id = execution.get("boot_id")
    if pid is None:
        if (
            context.config.enforce_production_bindings
            or process_group_id is not None
            or process_start_ticks is not None
            or boot_id != "fixture"
        ):
            raise MetaSlice2Error(f"attempt process identity is absent for {spec.attempt_id}")
    elif (
        type(pid) is not int
        or pid <= 1
        or type(process_group_id) is not int
        or process_group_id != pid
        or type(process_start_ticks) is not int
        or process_start_ticks < 0
        or not isinstance(boot_id, str)
        or not boot_id
    ):
        raise MetaSlice2Error(f"attempt process identity is malformed for {spec.attempt_id}")
    term_sent = execution.get("term_sent")
    kill_sent = execution.get("kill_sent")
    group_gone = execution.get("group_gone")
    if (
        not isinstance(term_sent, bool)
        or not isinstance(kill_sent, bool)
        or group_gone is not True
        or (kill_sent and not term_sent)
    ):
        raise MetaSlice2Error(f"attempt cleanup evidence is malformed for {spec.attempt_id}")
    for key in (
        "timeout_seconds",
        "pid",
        "process_group_id",
        "process_start_ticks",
        "boot_id",
        "returncode",
        "timed_out",
        "term_sent",
        "kill_sent",
        "group_gone",
    ):
        if process_state.get(key) != execution.get(key):
            raise MetaSlice2Error(f"process-state/result drift for {spec.attempt_id}.{key}")
    if process_state.get("phase") != "finished" or process_state.get("interrupted") is not False:
        raise MetaSlice2Error(f"completed process-state is malformed for {spec.attempt_id}")
    if outcome == "accepted":
        if timed_out or returncode != 0 or failure_raw is not None:
            raise MetaSlice2Error(
                f"accepted attempt process result is dishonest for {spec.attempt_id}"
            )
        _, validation = _attempt_validation(
            spec,
            stdout_path,
            input_path,
            selection=selection,
            certificates=certificates,
        )
        if validation_raw != validation:
            raise MetaSlice2Error(f"accepted attempt validation drift for {spec.attempt_id}")
    elif outcome == "timeout":
        expected_failure = {
            "reason_code": "timeout",
            "detail": f"timed out after {attempt_timeout_seconds}s",
        }
        if (
            not timed_out
            or returncode is not None
            or validation_raw is not None
            or failure_raw != expected_failure
        ):
            raise MetaSlice2Error(f"timeout attempt metadata is malformed for {spec.attempt_id}")
    elif outcome == "nonzero_exit":
        expected_failure = {
            "reason_code": "nonzero_exit",
            "detail": f"Lean exited with status {returncode}",
        }
        if (
            timed_out
            or type(returncode) is not int
            or returncode == 0
            or validation_raw is not None
            or failure_raw != expected_failure
        ):
            raise MetaSlice2Error(f"nonzero attempt metadata is malformed for {spec.attempt_id}")
    elif outcome == "invalid_output":
        error = _validation_failure(
            spec,
            stdout_path,
            input_path,
            selection=selection,
            certificates=certificates,
        )
        expected_invalid_failure = (
            None
            if error is None
            else {
                "reason_code": "invalid_output",
                "detail": error,
                "detail_sha256": _sha256_text(error),
            }
        )
        if (
            timed_out
            or returncode != 0
            or validation_raw is not None
            or failure_raw != expected_invalid_failure
        ):
            raise MetaSlice2Error(
                f"invalid-output attempt metadata is malformed for {spec.attempt_id}"
            )
    else:
        raise MetaSlice2Error(f"unsupported attempt outcome {outcome!r}")
    return _AttemptOutcome(spec=spec, result=result, result_path=result_path)


def _node_attempt_specs(
    context: _RunContext,
    *,
    stage: str,
    start: int,
    stop: int,
    parent_attempt_id: str | None,
) -> list[_AttemptSpec]:
    shards_root = context.config.output_root / SHARDS_DIRNAME
    if not shards_root.exists():
        return []
    prefix = f"attempt-{stage}-{start:08d}-{stop:08d}-r"
    specs: list[_AttemptSpec] = []
    for path in sorted(shards_root.glob(f"{prefix}*")):
        if path.is_symlink() or not path.is_dir():
            raise MetaSlice2Error(f"attempt path must be a non-symlink directory: {path}")
        suffix = path.name.removeprefix(prefix)
        if re.fullmatch(r"[0-9]{3}", suffix) is None:
            raise MetaSlice2Error(f"malformed attempt directory name: {path.name}")
        specs.append(
            _AttemptSpec(
                stage=stage,
                start=start,
                stop=stop,
                ordinal=int(suffix),
                parent_attempt_id=parent_attempt_id,
            )
        )
    if [spec.ordinal for spec in specs] != list(range(1, len(specs) + 1)):
        raise MetaSlice2Error(f"attempt ordinals are not contiguous for {prefix}")
    return specs


def _obtain_attempt(
    context: _RunContext,
    *,
    stage: str,
    start: int,
    stop: int,
    parent_attempt_id: str | None,
    material_factory: Callable[[_AttemptSpec], _AttemptMaterial],
    selection: SelectionResult | None,
    certificates: Sequence[CandidateCertificate] | None,
) -> _AttemptOutcome:
    specs = _node_attempt_specs(
        context,
        stage=stage,
        start=start,
        stop=stop,
        parent_attempt_id=parent_attempt_id,
    )
    outcomes: list[_AttemptOutcome] = []
    for index, spec in enumerate(specs):
        material = material_factory(spec)
        result_path = _attempt_result_path(context.config, spec)
        if not result_path.exists():
            if index != len(specs) - 1:
                raise MetaSlice2Error(
                    f"non-final attempt is missing result metadata: {spec.attempt_id}"
                )
            if not context.execute_missing:
                raise MetaSlice2Error(f"completed run has interrupted attempt {spec.attempt_id}")
            _recover_interrupted_attempt(context, spec, material)
        outcome = _validate_attempt_result(
            context,
            spec,
            material,
            selection=selection,
            certificates=certificates,
        )
        context.visited_attempt_ids.add(spec.attempt_id)
        outcomes.append(outcome)
    non_abandoned = [item for item in outcomes if item.result.get("outcome") != "abandoned"]
    if len(non_abandoned) > 1 or (
        non_abandoned and outcomes[-1].spec.attempt_id != non_abandoned[0].spec.attempt_id
    ):
        raise MetaSlice2Error(
            f"attempt retries continued after a terminal result for {stage}:{start}:{stop}"
        )
    if non_abandoned:
        return non_abandoned[0]
    if not context.execute_missing:
        raise MetaSlice2Error(f"completed run lacks a terminal attempt for {stage}:{start}:{stop}")
    ordinal = len(specs) + 1
    if ordinal > 999:
        raise MetaSlice2Error("attempt retry ordinal exceeds the frozen three-digit format")
    spec = _AttemptSpec(
        stage=stage,
        start=start,
        stop=stop,
        ordinal=ordinal,
        parent_attempt_id=parent_attempt_id,
    )
    material = material_factory(spec)
    outcome = _run_new_attempt(
        context,
        spec,
        material,
        selection=selection,
        certificates=certificates,
    )
    context.visited_attempt_ids.add(spec.attempt_id)
    return _validate_attempt_result(
        context,
        spec,
        material,
        selection=selection,
        certificates=certificates,
    )


def _primary_material(
    config: MetaSlice2Config,
    spec: _AttemptSpec,
    names: Sequence[str],
) -> _AttemptMaterial:
    names_path = _attempt_dir(config, spec) / ATTEMPT_NAMES_FILENAME
    return _AttemptMaterial(
        input_filename=ATTEMPT_NAMES_FILENAME,
        input_bytes=_names_bytes(names),
        driver_bytes=_driver_bytes(config, names_path),
        item_count=len(names),
    )


def _audit_material(
    config: MetaSlice2Config,
    spec: _AttemptSpec,
    certificates: Sequence[CandidateCertificate],
) -> _AttemptMaterial:
    return _AttemptMaterial(
        input_filename=ATTEMPT_CERTIFICATES_FILENAME,
        input_bytes=_certificates_bytes(certificates),
        driver_bytes=_audit_driver_bytes(config, certificates),
        item_count=len(certificates),
    )


def _runner_terminal(
    config: MetaSlice2Config,
    declaration: str,
    outcome: _AttemptOutcome,
) -> dict[str, object]:
    execution = _mapping_field(outcome.result, "execution", context="singleton attempt")
    failure = _mapping_field(outcome.result, "failure", context="singleton attempt")
    timeout_seconds = _nonnegative_int_field(
        outcome.result,
        "timeout_seconds",
        context="singleton attempt",
    )
    if timeout_seconds <= 0 or execution.get("timeout_seconds") != timeout_seconds:
        raise MetaSlice2Error("singleton attempt has inconsistent timeout evidence")
    reason_code = _string_field(failure, "reason_code", context="singleton attempt")
    if reason_code not in _RUNNER_REASON_CODES:
        raise MetaSlice2Error("singleton attempt lacks a runner disposition reason")
    return {
        "schemaVersion": 2,
        "kind": "terminal",
        "recordKind": "status",
        "declaration": declaration,
        "status": _RUNNER_STATUS_BY_REASON[reason_code],
        "terminalOrigin": "runner",
        "reasonCode": reason_code,
        "attemptId": outcome.spec.attempt_id,
        "attemptPath": outcome.result_path.parent.relative_to(config.output_root).as_posix(),
        "attemptResultSha256": hash_file(outcome.result_path),
        "timeoutSeconds": timeout_seconds,
        "timedOut": execution.get("timed_out"),
        "returncode": execution.get("returncode"),
        "candidateCount": 0,
        "emittedCount": 0,
        "duplicateCount": 0,
        "rejectedCount": 0,
        "error": None,
    }


def _collect_primary_range(
    context: _RunContext,
    start: int,
    stop: int,
    *,
    parent_attempt_id: str | None,
) -> tuple[dict[str, object], ...]:
    names = context.selection.names[start:stop]
    outcome = _obtain_attempt(
        context,
        stage="primary",
        start=start,
        stop=stop,
        parent_attempt_id=parent_attempt_id,
        material_factory=lambda spec: _primary_material(context.config, spec, names),
        selection=_selection_slice(context.selection, names),
        certificates=None,
    )
    attempt_outcome = _string_field(outcome.result, "outcome", context="primary attempt")
    if attempt_outcome == "accepted":
        rows, _ = _primary_rows_and_validation(
            outcome.result_path.parent / ATTEMPT_STDOUT_FILENAME,
            selection=_selection_slice(context.selection, names),
            names_path=outcome.result_path.parent / ATTEMPT_NAMES_FILENAME,
        )
        return rows
    if stop - start == 1:
        return (_runner_terminal(context.config, names[0], outcome),)
    midpoint = start + (stop - start) // 2
    return _collect_primary_range(
        context,
        start,
        midpoint,
        parent_attempt_id=outcome.spec.attempt_id,
    ) + _collect_primary_range(
        context,
        midpoint,
        stop,
        parent_attempt_id=outcome.spec.attempt_id,
    )


def _primary_aggregate_rows(context: _RunContext) -> tuple[dict[str, object], ...]:
    rows: tuple[dict[str, object], ...] = ()
    for start, stop in _base_ranges(len(context.selection.names), PRIMARY_SHARD_SIZE):
        rows += _collect_primary_range(context, start, stop, parent_attempt_id=None)
    terminals = [row for row in rows if row.get("kind") == "terminal"]
    completed_count = sum(row.get("status") in _PROCESSED_TERMINAL_STATUSES for row in terminals)
    failed_count = len(terminals) - completed_count
    batch = {
        "schemaVersion": 2,
        "kind": "batch",
        "recordKind": "batch",
        "producer": "runner-aggregate-v4",
        "status": "complete" if failed_count == 0 else "partial",
        "namesFile": str(_resolve(context.config.output_root / NAMES_FILENAME)),
        "declarationCount": len(context.selection.names),
        "accountedCount": len(terminals),
        "terminalCount": len(terminals),
        "completedCount": completed_count,
        "failedCount": failed_count,
    }
    return (*rows, batch)


def _collect_audit_range(
    context: _RunContext,
    certificates: Sequence[CandidateCertificate],
    start: int,
    stop: int,
    *,
    parent_attempt_id: str | None,
) -> tuple[dict[str, object], ...]:
    shard = tuple(certificates[start:stop])
    outcome = _obtain_attempt(
        context,
        stage="audit",
        start=start,
        stop=stop,
        parent_attempt_id=parent_attempt_id,
        material_factory=lambda spec: _audit_material(context.config, spec, shard),
        selection=None,
        certificates=shard,
    )
    attempt_outcome = _string_field(outcome.result, "outcome", context="audit attempt")
    if attempt_outcome == "accepted":
        rows, _ = _audit_rows_and_validation(
            outcome.result_path.parent / ATTEMPT_STDOUT_FILENAME,
            certificates=shard,
        )
        return rows
    if stop - start == 1:
        raise MetaSlice2Error(
            f"independent audit singleton failed: {outcome.spec.attempt_id} ({attempt_outcome})"
        )
    midpoint = start + (stop - start) // 2
    return _collect_audit_range(
        context,
        certificates,
        start,
        midpoint,
        parent_attempt_id=outcome.spec.attempt_id,
    ) + _collect_audit_range(
        context,
        certificates,
        midpoint,
        stop,
        parent_attempt_id=outcome.spec.attempt_id,
    )


def _audit_aggregate_rows(
    context: _RunContext,
    certificates: Sequence[CandidateCertificate],
) -> tuple[dict[str, object], ...]:
    rows: tuple[dict[str, object], ...] = ()
    for start, stop in _base_ranges(len(certificates), AUDIT_SHARD_SIZE):
        rows += _collect_audit_range(
            context,
            certificates,
            start,
            stop,
            parent_attempt_id=None,
        )
    return rows


def _attempt_summary(config: MetaSlice2Config) -> dict[str, object]:
    attempts = _manifest_attempts(config)
    by_stage = Counter(cast(str, row["stage"]) for row in attempts)
    by_outcome = Counter(cast(str, row["outcome"]) for row in attempts)
    return {
        "total": len(attempts),
        "by_stage": dict(sorted(by_stage.items())),
        "by_outcome": dict(sorted(by_outcome.items())),
    }


def _shard_plan(
    declaration_count: int,
    certificate_count: int,
) -> dict[str, object]:
    return {
        "primary_base_ranges": [
            {"start": start, "stop": stop}
            for start, stop in _base_ranges(declaration_count, PRIMARY_SHARD_SIZE)
        ],
        "audit_base_ranges": [
            {"start": start, "stop": stop}
            for start, stop in _base_ranges(certificate_count, AUDIT_SHARD_SIZE)
        ],
    }


def _all_attempt_ids(config: MetaSlice2Config) -> set[str]:
    shards_root = config.output_root / SHARDS_DIRNAME
    if not shards_root.exists():
        return set()
    result: set[str] = set()
    for path in shards_root.iterdir():
        if path.is_symlink() or not path.is_dir() or not path.name.startswith("attempt-"):
            raise MetaSlice2Error(f"unexpected entry beneath shards/: {path.name}")
        if not (path / ATTEMPT_RESULT_FILENAME).is_file():
            raise MetaSlice2Error(f"completed attempt lacks atomic result: {path.name}")
        result.add(path.name)
    return result


def _verify_visited_attempts(context: _RunContext) -> None:
    actual = _all_attempt_ids(context.config)
    if actual != context.visited_attempt_ids:
        extra = sorted(actual.difference(context.visited_attempt_ids))
        missing = sorted(context.visited_attempt_ids.difference(actual))
        raise MetaSlice2Error(
            "attempt tree differs from deterministic replay: "
            f"extra={extra[:5]}, missing={missing[:5]}"
        )


def _prepare_existing_manifest(
    config: MetaSlice2Config,
    *,
    selection: SelectionResult,
) -> dict[str, object]:
    manifest = _load_bound_manifest(config, require_completed=False)
    if manifest.get("selection") != selection.manifest_payload():
        raise MetaSlice2Error("yield-probe selection differs from deterministic replay")
    source_state = _mapping_field(manifest, "source_state", context="yield-probe manifest")
    _validate_source_state(config, source_state)
    expected_policy = _manifest_base(
        config,
        selection=selection,
        source_state=source_state,
        started_at=cast(str, manifest.get("started_at", "")),
    )["shard_policy"]
    if manifest.get("shard_policy") != expected_policy:
        raise MetaSlice2Error("yield-probe shard policy differs from the frozen contract")
    _verify_recorded_inventory_subset(config, manifest)
    _artifact_inventory(config.output_root)
    _verify_recorded_attempt_subset(config, manifest)
    names_path = config.output_root / NAMES_FILENAME
    _require_regular_file(names_path, label="declaration names")
    if names_path.read_bytes() != _names_bytes(selection.names):
        raise MetaSlice2Error("declaration names differ from deterministic selection")
    return manifest


def run_meta_slice2(
    config: MetaSlice2Config,
    *,
    executor: LeanExecutor | None = None,
) -> dict[str, object]:
    """Run or strictly resume the deterministic sharded Meta slice-2 probe."""
    _validate_config(config)
    selection = select_declarations(config)
    live_source_state = _source_state(config)
    fresh = _ensure_output_root(config)
    with _exclusive_run_lock(config.output_root):
        manifest_path = config.output_root / MANIFEST_FILENAME
        if fresh:
            names_path = config.output_root / NAMES_FILENAME
            _write_atomic(names_path, _names_bytes(selection.names))
            manifest = _manifest_base(
                config,
                selection=selection,
                source_state=live_source_state,
                started_at=_utc_now(),
            )
            manifest["outputs"] = _artifact_inventory(config.output_root)
            _write_json_atomic(manifest_path, manifest)
        else:
            manifest = _prepare_existing_manifest(config, selection=selection)
            if manifest.get("status") == "completed":
                verify_meta_slice2(config)
                return manifest
            manifest["status"] = "running"
            manifest["resumed_at"] = _utc_now()
            manifest.pop("failure", None)
            manifest.pop("failed_at", None)
            _write_json_atomic(manifest_path, manifest)
        context = _RunContext(
            config=config,
            selection=selection,
            executor=executor or SubprocessLeanExecutor(),
            execute_missing=True,
            manifest=manifest,
            visited_attempt_ids=set(),
        )
        try:
            primary_rows = _primary_aggregate_rows(context)
            primary_bytes = _jsonl_bytes(primary_rows)
            _write_or_verify(
                config.output_root / STDOUT_FILENAME,
                primary_bytes,
                label="primary aggregate",
            )
            parsed = _parse_probe_output(
                config.output_root / STDOUT_FILENAME,
                selection=selection,
                names_path=config.output_root / NAMES_FILENAME,
                allow_runner_dispositions=True,
            )
            audit_rows = _audit_aggregate_rows(context, parsed.certificates)
            audit_bytes = _jsonl_bytes(audit_rows)
            _write_or_verify(
                config.output_root / AUDIT_STDOUT_FILENAME,
                audit_bytes,
                label="audit aggregate",
            )
            audit_summary = verify_audit_output(
                config.output_root / AUDIT_STDOUT_FILENAME,
                certificates=parsed.certificates,
            )
            _verify_visited_attempts(context)
            summary = dict(parsed.summary)
            summary["independent_audit"] = audit_summary
            summary["execution_attempts"] = _attempt_summary(config)
            _write_or_verify(
                config.output_root / SUMMARY_FILENAME,
                canonical_json_bytes(summary) + b"\n",
                label="yield summary",
            )
            manifest.update(
                {
                    "status": "completed",
                    "completed_at": _utc_now(),
                    "summary": summary,
                    "shard_plan": _shard_plan(len(selection.names), len(parsed.certificates)),
                    "attempts": _manifest_attempts(config),
                    "outputs": _artifact_inventory(config.output_root),
                }
            )
            manifest.pop("updated_at", None)
            manifest.pop("resumed_at", None)
            _write_json_atomic(manifest_path, manifest)
            verify_meta_slice2(config)
            return manifest
        except BaseException as exc:
            for completed_only in ("completed_at", "summary", "shard_plan"):
                manifest.pop(completed_only, None)
            manifest.update(
                {
                    "status": "failure",
                    "failed_at": _utc_now(),
                    "failure": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
            with suppress(Exception):
                manifest["attempts"] = _manifest_attempts(config)
                manifest["outputs"] = _progress_artifact_inventory(config.output_root)
                _write_json_atomic(manifest_path, manifest)
            raise


def verify_meta_slice2(config: MetaSlice2Config) -> dict[str, object]:
    """Strictly replay selection, attempt trees, aggregates, audit, and inventory."""
    _validate_config(config)
    if config.output_root.is_symlink() or not config.output_root.is_dir():
        raise MetaSlice2Error("yield-probe output root must be a non-symlink directory")
    manifest = _load_bound_manifest(config, require_completed=True)
    selection = select_declarations(config)
    if manifest.get("selection") != selection.manifest_payload():
        raise MetaSlice2Error("completed manifest selection statistics drifted")
    source_state = _mapping_field(manifest, "source_state", context="yield-probe manifest")
    _validate_source_state(config, source_state)
    expected_policy = _manifest_base(
        config,
        selection=selection,
        source_state=source_state,
        started_at=cast(str, manifest.get("started_at", "")),
    )["shard_policy"]
    if manifest.get("shard_policy") != expected_policy:
        raise MetaSlice2Error("completed shard policy differs from the frozen contract")
    recorded_inventory = _mapping_field(manifest, "outputs", context="yield-probe manifest")
    if recorded_inventory != _artifact_inventory(config.output_root):
        raise MetaSlice2Error("completed output artifact inventory drifted")
    names_path = config.output_root / NAMES_FILENAME
    _require_regular_file(names_path, label="declaration names")
    if names_path.read_bytes() != _names_bytes(selection.names):
        raise MetaSlice2Error("declaration names differ from deterministic selection")
    context = _RunContext(
        config=config,
        selection=selection,
        executor=None,
        execute_missing=False,
        manifest=None,
        visited_attempt_ids=set(),
    )
    primary_rows = _primary_aggregate_rows(context)
    if (config.output_root / STDOUT_FILENAME).read_bytes() != _jsonl_bytes(primary_rows):
        raise MetaSlice2Error("primary aggregate differs from deterministic shard replay")
    parsed = _parse_probe_output(
        config.output_root / STDOUT_FILENAME,
        selection=selection,
        names_path=names_path,
        allow_runner_dispositions=True,
    )
    audit_rows = _audit_aggregate_rows(context, parsed.certificates)
    if (config.output_root / AUDIT_STDOUT_FILENAME).read_bytes() != _jsonl_bytes(audit_rows):
        raise MetaSlice2Error("audit aggregate differs from deterministic shard replay")
    audit_summary = verify_audit_output(
        config.output_root / AUDIT_STDOUT_FILENAME,
        certificates=parsed.certificates,
    )
    _verify_visited_attempts(context)
    summary = dict(parsed.summary)
    summary["independent_audit"] = audit_summary
    summary["execution_attempts"] = _attempt_summary(config)
    recorded_summary = _parse_json_object(
        (config.output_root / SUMMARY_FILENAME).read_text(encoding="utf-8"),
        context="yield-probe summary",
    )
    if recorded_summary != summary or manifest.get("summary") != summary:
        raise MetaSlice2Error("yield summary differs from independently replayed output")
    expected_plan = _shard_plan(len(selection.names), len(parsed.certificates))
    if manifest.get("shard_plan") != expected_plan:
        raise MetaSlice2Error("completed manifest shard plan differs from replay")
    if manifest.get("attempts") != _manifest_attempts(config):
        raise MetaSlice2Error("completed manifest attempt inventory differs from replay")
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    output_root = cast(Path, args.output_root)
    config = production_config(output_root)
    if args.command == "run":
        manifest = run_meta_slice2(config)
        print(json.dumps(manifest["summary"], sort_keys=True))
        return 0
    if args.command == "verify":
        summary = verify_meta_slice2(config)
        print(json.dumps(summary, sort_keys=True))
        return 0
    raise AssertionError(f"unreachable command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
