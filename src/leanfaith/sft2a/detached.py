"""Detached, locked, resource-claimed launch contract for the SFT2A pilot."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.host_resources import claim_resources, release_resources
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.legacy import _atomic_exact
from leanfaith.sft2a.models import (
    DetachedLaunchPolicy,
    ProductionPilotReadinessConfig,
    SFT2AProductionConfig,
)
from leanfaith.sft2a.pilot import (
    _pilot_output,
    prepare_pilot_sample,
    run_multi_root_pilot,
    verify_pilot_replay,
)
from leanfaith.sft2a.pilot_audit import run_pilot_lemex_audit
from leanfaith.sft2a.readiness import (
    LoadedPilotReadiness,
    implementation_identity,
    require_pilot_authorization,
)


class DetachedPilotError(RuntimeError):
    """Detached launch, duplicate suppression, health, or cleanup failed."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _paths(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness,
) -> tuple[Path, DetachedLaunchPolicy, dict[str, Path]]:
    if not isinstance(readiness.config, ProductionPilotReadinessConfig):
        raise DetachedPilotError("pilot readiness lacks the detached launch contract")
    policy = readiness.config.detached_launch
    output = _pilot_output(loaded, readiness)
    paths = {
        "run_lock": output / policy.run_lock_relative_path,
        "log": output / policy.combined_log_relative_path,
        "journal": output / policy.journal_relative_path,
        "terminal": output / policy.terminal_status_relative_path,
        "launch_receipt": output / policy.launch_receipt_relative_path,
        "launch_lock": output / "detached/launch.lock",
    }
    return output, policy, paths


@contextmanager
def _exclusive_lock(path: Path, *, label: str) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise DetachedPilotError(f"{label} must be a regular file")
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DetachedPilotError(f"{label} is already held; duplicate start refused") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_is_free(path: Path) -> bool:
    try:
        with _exclusive_lock(path, label="pilot run lock"):
            return True
    except DetachedPilotError:
        return False


def _append_journal(path: Path, event: Mapping[str, object]) -> None:
    payload = {"event_id": "sft2a-detached:" + hash_canonical(event), **event}
    line = canonical_json_bytes(payload) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            for raw in handle.read().splitlines():
                observed = json.loads(raw)
                if isinstance(observed, dict) and observed.get("event_id") == payload["event_id"]:
                    if canonical_json_bytes(observed) + b"\n" != line:
                        raise DetachedPilotError("detached journal event conflict")
                    return
            handle.seek(0, os.SEEK_END)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_replace(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(canonical_json_bytes(dict(value)) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _tmux_session_exists(session_name: str) -> bool:
    result = subprocess.run(
        ("tmux", "has-session", "-t", session_name),
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def pilot_health(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness,
) -> dict[str, object]:
    """Return read-only tmux, process, journal, and durable-status health evidence."""

    _output, policy, paths = _paths(loaded, readiness)
    session_live = _tmux_session_exists(policy.session_name)
    pane_pid: int | None = None
    process = ""
    if session_live:
        pane = subprocess.run(
            ("tmux", "list-panes", "-t", policy.session_name, "-F", "#{pane_pid}"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if pane.isdigit():
            pane_pid = int(pane)
            process = subprocess.run(
                (
                    "ps",
                    "-o",
                    "pid=,ppid=,stat=,etime=,cmd=",
                    "--forest",
                    "-p",
                    str(pane_pid),
                    "--ppid",
                    str(pane_pid),
                ),
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
    journal_rows = 0
    latest_event: object = None
    if paths["journal"].is_file():
        rows = [
            json.loads(line)
            for line in paths["journal"].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        journal_rows = len(rows)
        latest_event = rows[-1] if rows else None
    terminal = (
        json.loads(paths["terminal"].read_text(encoding="utf-8"))
        if paths["terminal"].is_file()
        else None
    )
    return {
        "session_name": policy.session_name,
        "session_live": session_live,
        "pane_pid": pane_pid,
        "process_tree": process,
        "run_lock_held": not _lock_is_free(paths["run_lock"]),
        "journal_path": str(paths["journal"]),
        "journal_rows": journal_rows,
        "latest_event": latest_event,
        "combined_log_path": str(paths["log"]),
        "combined_log_bytes": paths["log"].stat().st_size if paths["log"].is_file() else 0,
        "terminal_status": terminal,
        "healthy_start": session_live and pane_pid is not None and journal_rows >= 2,
        "attach_command": f"tmux attach -t {policy.session_name}",
        "status_command": (
            "uv run python -m leanfaith.sft2a "
            f"--config {loaded.path} --pilot-config {readiness.path} pilot-health"
        ),
    }


def _ensure_detached_startable(
    policy: DetachedLaunchPolicy,
    paths: Mapping[str, Path],
    *,
    resume: bool,
) -> None:
    if _tmux_session_exists(policy.session_name):
        raise DetachedPilotError("named pilot tmux session already exists")
    if not _lock_is_free(paths["run_lock"]):
        raise DetachedPilotError("pilot run lock is held; duplicate restart refused")
    terminal: dict[str, object] | None = None
    if paths["terminal"].is_file():
        value = json.loads(paths["terminal"].read_text(encoding="utf-8"))
        terminal = value if isinstance(value, dict) else None
    if terminal is not None and terminal.get("status") == "complete":
        raise DetachedPilotError("pilot is already complete; restart refused")
    if terminal is not None and not resume:
        raise DetachedPilotError("pilot has prior terminal state; use the explicit resume command")


def preflight_detached_launch(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness,
    *,
    resume: bool,
    implementation: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Prepare the fresh sample and stop immediately before the tmux boundary."""

    if not isinstance(loaded.config, SFT2AProductionConfig):
        raise DetachedPilotError("detached pilot requires the production-default config")
    require_pilot_authorization(readiness)
    identity = dict(implementation or implementation_identity(loaded.repo_root))
    output, policy, paths = _paths(loaded, readiness)
    sample = prepare_pilot_sample(loaded, readiness, implementation=identity)
    if (
        sample.get("sample_sha256") != readiness.config.expected_sample_sha256
        or sample.get("pilot_authorized") is not True
        or sample.get("provider_calls_executed") != 0
        or sample.get("lean_requests_executed") != 0
        or sample.get("implementation") != identity
    ):
        raise DetachedPilotError("authorized detached preflight sample lineage differs")
    _ensure_detached_startable(policy, paths, resume=resume)
    return {
        "version": "leanfaith_sft2a_detached_preflight_v1",
        "boundary": "tmux_start_not_executed",
        "resume": resume,
        "session_name": policy.session_name,
        "output_root": str(output),
        "sample_sha256": sample["sample_sha256"],
        "sample_manifest_sha256": hash_file(output / "sample_manifest.json"),
        "config_hash": loaded.config_hash,
        "readiness_config_hash": readiness.config_hash,
        "authorization_receipt_sha256": readiness.config.authorization_receipt.sha256,
        "implementation": identity,
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "tmux_sessions_started": 0,
    }


def launch_detached_pilot(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness,
    *,
    resume: bool,
) -> dict[str, object]:
    """Start one named tmux worker only after hash-bound authorization."""

    if not isinstance(loaded.config, SFT2AProductionConfig):
        raise DetachedPilotError("detached pilot requires the production-default config")
    require_pilot_authorization(readiness)
    identity = implementation_identity(loaded.repo_root)
    preflight = preflight_detached_launch(
        loaded,
        readiness,
        resume=resume,
        implementation=identity,
    )
    _output, policy, paths = _paths(loaded, readiness)
    with _exclusive_lock(paths["launch_lock"], label="pilot launch lock"):
        _ensure_detached_startable(policy, paths, resume=resume)
        command = (
            sys.executable,
            "-m",
            "leanfaith.sft2a",
            "--config",
            str(loaded.path),
            "--pilot-config",
            str(readiness.path),
            "detached-pilot-worker",
        )
        request = {
            "version": "leanfaith_sft2a_detached_launch_request_v1",
            "requested_at": _utc_now(),
            "resume": resume,
            "session_name": policy.session_name,
            "sanitized_command": shlex.join(command),
            "config_file_sha256": hash_file(loaded.path),
            "config_hash": loaded.config_hash,
            "readiness_file_sha256": hash_file(readiness.path),
            "readiness_config_hash": readiness.config_hash,
            "implementation": identity,
        }
        _append_journal(paths["journal"], {"event": "launch_requested", **request})
        completed = subprocess.run(
            ("tmux", "new-session", "-d", "-s", policy.session_name, shlex.join(command)),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise DetachedPilotError(f"tmux launch failed: {completed.stderr.strip()}")
    deadline = time.monotonic() + 30.0
    health = pilot_health(loaded, readiness)
    while not bool(health["healthy_start"]) and time.monotonic() < deadline:
        time.sleep(0.5)
        health = pilot_health(loaded, readiness)
        health_terminal = health.get("terminal_status")
        if isinstance(health_terminal, dict) and health_terminal.get("status") == "failed":
            break
    if not bool(health["healthy_start"]):
        raise DetachedPilotError(f"detached pilot failed startup health check: {health}")
    return {"preflight": preflight, "launch_request": request, "health": health}


def run_detached_worker(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness,
) -> dict[str, object]:
    """Hold the run lock and one Lean claim across pilot, replay, and audit phases."""

    if not isinstance(loaded.config, SFT2AProductionConfig):
        raise DetachedPilotError("detached worker requires the production-default config")
    require_pilot_authorization(readiness)
    identity = implementation_identity(loaded.repo_root)
    output, policy, paths = _paths(loaded, readiness)
    paths["log"].parent.mkdir(parents=True, exist_ok=True)
    log_descriptor = os.open(paths["log"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null_descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(log_descriptor, sys.stdout.fileno())
        os.dup2(log_descriptor, sys.stderr.fileno())
        os.dup2(null_descriptor, sys.stdin.fileno())
    finally:
        os.close(log_descriptor)
        os.close(null_descriptor)
    started_at = _utc_now()
    with _exclusive_lock(paths["run_lock"], label="pilot run lock"):
        if paths["terminal"].is_file():
            terminal = json.loads(paths["terminal"].read_text(encoding="utf-8"))
            if isinstance(terminal, dict) and terminal.get("status") == "complete":
                raise DetachedPilotError("completed pilot cannot be restarted")
        _append_journal(
            paths["journal"],
            {"event": "worker_started", "at": started_at, "pid": os.getpid()},
        )
        reservation = None
        try:
            reservation = claim_resources(
                task=policy.resource_task,
                lean_workers=policy.lean_workers,
                lean_rss_gib=policy.lean_rss_gib,
                gpu=False,
                pid=os.getpid(),
                owner_session=policy.session_name,
                worktree=loaded.repo_root,
            )
            _append_journal(
                paths["journal"],
                {
                    "event": "resource_claimed",
                    "at": _utc_now(),
                    "task": reservation.task,
                    "lean_workers": reservation.lean_workers,
                    "lean_rss_gib": reservation.lean_rss_gib,
                },
            )
            receipt = {
                "version": "leanfaith_sft2a_detached_launch_receipt_v1",
                "session_name": policy.session_name,
                "pane_worker_pid": os.getpid(),
                "started_at": started_at,
                "implementation": identity,
                "config_file_sha256": hash_file(loaded.path),
                "config_hash": loaded.config_hash,
                "readiness_file_sha256": hash_file(readiness.path),
                "readiness_config_hash": readiness.config_hash,
                "ceilings": readiness.config.ceilings.model_dump(mode="json"),
                "output_root": str(output),
                "shared_cache_root": loaded.config.run_layout.shared_cache_root,
                "run_lock": str(paths["run_lock"]),
                "combined_log": str(paths["log"]),
                "journal": str(paths["journal"]),
                "resource_claim": policy.resource_task,
                "resume_command": (
                    "uv run python -m leanfaith.sft2a "
                    f"--config {loaded.path} --pilot-config {readiness.path} "
                    "resume-authorized-pilot"
                ),
                "health_command": (
                    "uv run python -m leanfaith.sft2a "
                    f"--config {loaded.path} --pilot-config {readiness.path} pilot-health"
                ),
                "stop_conditions": [
                    "provider_or_spend_ceiling",
                    "Lean_infrastructure_failure",
                    "hash_or_schema_conflict",
                    "successful_pilot_replay_and_Lemex_audit",
                ],
            }
            if paths["launch_receipt"].is_file():
                existing = json.loads(paths["launch_receipt"].read_text(encoding="utf-8"))
                stable_keys = (
                    "implementation",
                    "config_file_sha256",
                    "config_hash",
                    "readiness_file_sha256",
                    "readiness_config_hash",
                    "ceilings",
                    "output_root",
                    "shared_cache_root",
                    "resource_claim",
                )
                if not isinstance(existing, dict) or any(
                    existing.get(key) != receipt.get(key) for key in stable_keys
                ):
                    raise DetachedPilotError("resume launch receipt lineage differs")
            else:
                _atomic_exact(paths["launch_receipt"], canonical_json_bytes(receipt) + b"\n")
            _append_journal(paths["journal"], {"event": "pilot_phase_started", "at": _utc_now()})
            pilot = run_multi_root_pilot(loaded, readiness)
            _append_journal(
                paths["journal"],
                {"event": "pilot_phase_complete", "at": _utc_now(), "roots": pilot["root_count"]},
            )
            replay = verify_pilot_replay(loaded, readiness)
            _append_journal(
                paths["journal"],
                {
                    "event": "replay_phase_complete",
                    "at": _utc_now(),
                    "receipt": hash_canonical(replay),
                },
            )
            audit = run_pilot_lemex_audit(loaded, readiness)
            terminal = {
                "version": "leanfaith_sft2a_detached_terminal_v1",
                "status": "complete",
                "completed_at": _utc_now(),
                "pilot_manifest_sha256": hash_file(output / "manifest.json"),
                "replay_receipt_sha256": hash_file(output / "pilot_reproducibility_receipt.json"),
                "audit_manifest_sha256": hash_file(output / "audit_lemex_v1/manifest.json"),
                "scale_blocked": bool(audit["systematic_disagreement_blocks_scale"]),
            }
            _atomic_replace(paths["terminal"], terminal)
            _append_journal(paths["journal"], {"event": "worker_complete", **terminal})
            return terminal
        except Exception as exc:
            terminal = {
                "version": "leanfaith_sft2a_detached_terminal_v1",
                "status": "failed",
                "failed_at": _utc_now(),
                "error_type": type(exc).__name__,
                "detail": str(exc),
            }
            _atomic_replace(paths["terminal"], terminal)
            _append_journal(paths["journal"], {"event": "worker_failed", **terminal})
            raise
        finally:
            if reservation is not None:
                released = release_resources(task=policy.resource_task)
                _append_journal(
                    paths["journal"],
                    {
                        "event": "resource_released",
                        "at": _utc_now(),
                        "task": released.task,
                    },
                )


__all__ = [
    "DetachedPilotError",
    "launch_detached_pilot",
    "pilot_health",
    "preflight_detached_launch",
    "run_detached_worker",
]
