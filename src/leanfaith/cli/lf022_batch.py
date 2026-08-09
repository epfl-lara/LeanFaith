"""CLI adapters for deterministic public LF-022 batches."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation.lf022_batch import (
    FrozenLF022PublicBatch,
    LF022BatchFreezeRequest,
    LF022BatchRouteFreezeRequest,
    LF022BatchRunPolicy,
    LF022BatchRunResult,
    freeze_lf022_public_batch,
    make_lf022_batch_freeze_request,
    run_lf022_public_batch,
)
from leanfaith.generation.lf022_execution import (
    LF022GOpenExecutionAdmission,
    verify_lf022_execution_admission,
)
from leanfaith.generation.lf022_executor import RCPRuntimeCredentials
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022ProductionTask,
)
from leanfaith.generation.rcp_provider import UrllibOpenAICompatibleRCPTransport


@dataclass(frozen=True, slots=True)
class CreatedLF022BatchRequest:
    """One immutable, offline-created batch request."""

    request: LF022BatchFreezeRequest
    request_path: Path


def select_public_g_open_plan_window(
    *,
    plan_tasks: tuple[LF022ProductionTask, ...],
    proposer_family_id: str,
    allocation_offset: int,
    allocation_limit: int,
) -> tuple[str, ...]:
    """Select one exact plan-order window and return canonical sorted task IDs."""

    if allocation_offset < 0 or allocation_limit < 1:
        raise ValueError("allocation offset must be nonnegative and limit must be positive")
    available_in_plan_order = tuple(
        task.task_id
        for task in plan_tasks
        if task.distribution == "G_open" and task.proposer_family_id == proposer_family_id
    )
    selection_end = allocation_offset + allocation_limit
    if allocation_offset >= len(available_in_plan_order) or selection_end > len(
        available_in_plan_order
    ):
        raise ValueError(
            "allocation offset/limit exceeds the admitted public G_open plan-order range"
        )
    return tuple(sorted(available_in_plan_order[allocation_offset:selection_end]))


def _binding(repo_root: Path, path: Path, *, label: str) -> LF022ArtifactBinding:
    root = repo_root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} must be a repository-local regular file") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} must be a repository-local regular file")
    normalized = PurePosixPath(relative.as_posix()).as_posix()
    return LF022ArtifactBinding(path=normalized, sha256=hash_file(candidate))


def _output_path(repo_root: Path, path: Path) -> Path:
    root = repo_root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("batch request output must remain inside the repository") from exc
    if "." in relative.parts or ".." in relative.parts:
        raise ValueError("batch request output must use a normalized repository path")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("batch request output cannot traverse symlinks")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError("batch request output cannot be a symlink")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ValueError("existing batch request output differs")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise ValueError("concurrent batch request output differs") from None
    finally:
        temporary.unlink(missing_ok=True)


def create_public_batch_request(
    *,
    repo_root: Path,
    admission_path: Path,
    allocation_task_ids: tuple[str, ...],
    output_path: Path,
    batch_directory: str,
    executor_output_root: str,
    allocation_offset: int | None = None,
    allocation_limit: int | None = None,
) -> CreatedLF022BatchRequest:
    """Verify one admission and create its exact request without network I/O."""

    repo_root = repo_root.resolve(strict=True)
    admission_binding = _binding(repo_root, admission_path, label="execution admission")
    raw = (repo_root / admission_binding.path).read_bytes()
    try:
        admission = LF022GOpenExecutionAdmission.model_validate_json(raw)
    except ValueError as exc:
        raise ValueError(f"invalid execution admission: {exc}") from exc
    canonical = canonical_json_bytes(admission.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise ValueError("execution admission is not canonical JSON")
    verified = verify_lf022_execution_admission(
        repo_root=repo_root,
        admission=admission,
    )
    available = {
        task.task_id
        for task in verified.plan.tasks
        if task.distribution == "G_open"
        and task.proposer_family_id == admission.route.proposer_family_id
    }
    if allocation_task_ids:
        if allocation_offset is not None or allocation_limit is not None:
            raise ValueError(
                "explicit allocation task IDs cannot be combined with offset/limit selection"
            )
        selected = tuple(sorted(set(allocation_task_ids)))
        if selected != allocation_task_ids:
            raise ValueError("allocation task IDs must be sorted and unique")
    else:
        if allocation_offset is None or allocation_limit is None:
            raise ValueError(
                "provide either sorted allocation task IDs or both allocation offset and limit"
            )
        selected = select_public_g_open_plan_window(
            plan_tasks=verified.plan.tasks,
            proposer_family_id=admission.route.proposer_family_id,
            allocation_offset=allocation_offset,
            allocation_limit=allocation_limit,
        )
    missing = tuple(task_id for task_id in selected if task_id not in available)
    if missing:
        raise ValueError(
            "allocation task IDs are absent from the admitted public G_open route: "
            + ", ".join(missing)
        )
    proposer_family_id = admission.route.proposer_family_id
    if proposer_family_id not in {"moonshot_kimi_k2", "qwen3", "glm5"}:
        raise ValueError("execution admission uses an unsupported proposer family")
    route = LF022BatchRouteFreezeRequest(
        proposer_family_id=cast(
            Literal["moonshot_kimi_k2", "qwen3", "glm5"],
            proposer_family_id,
        ),
        public_pool_audit_id=admission.public_pool_audit_id,
        allocation_plan_id=admission.allocation_plan_id,
        execution_artifacts=admission.artifacts,
        route=admission.route,
        retry_policy=admission.retry_policy,
        code_tree_hash=admission.code_tree_hash,
        allocation_task_ids=selected,
    )
    request = make_lf022_batch_freeze_request(
        batch_directory=batch_directory,
        executor_output_root=executor_output_root,
        routes=(route,),
    )
    destination = _output_path(repo_root, output_path)
    _write_immutable(
        destination,
        canonical_json_bytes(request.model_dump(mode="json")) + b"\n",
    )
    return CreatedLF022BatchRequest(
        request=request,
        request_path=destination,
    )


def freeze_public_batch(
    *,
    repo_root: Path,
    request_path: Path,
) -> FrozenLF022PublicBatch:
    """Freeze a reviewed request without resolving credentials or using the network."""

    return freeze_lf022_public_batch(
        repo_root=repo_root,
        request_binding=_binding(
            repo_root,
            request_path,
            label="batch freeze request",
        ),
    )


def run_public_batch(
    *,
    repo_root: Path,
    manifest_path: Path,
    max_concurrency: int,
    minimum_request_interval_seconds: float,
    execute_public_provisional: bool,
) -> LF022BatchRunResult:
    """Preflight/replay by default; resolve RCP credentials only in explicit live mode."""

    binding = _binding(repo_root, manifest_path, label="batch manifest")
    policy = LF022BatchRunPolicy(
        max_concurrency=max_concurrency,
        minimum_request_interval_seconds=minimum_request_interval_seconds,
    )
    if not execute_public_provisional:
        return run_lf022_public_batch(
            repo_root=repo_root,
            manifest_binding=binding,
            policy=policy,
        )
    base_url = os.environ.get("RCP_BASE_URL", "")
    api_key = os.environ.get("RCP_API_KEY", "")
    if not base_url or not api_key:
        raise ValueError("live batch execution requires RCP_BASE_URL and RCP_API_KEY")
    return run_lf022_public_batch(
        repo_root=repo_root,
        manifest_binding=binding,
        policy=policy,
        execute_public_provisional=True,
        credentials=RCPRuntimeCredentials(base_url=base_url, api_key=api_key),
        transport=UrllibOpenAICompatibleRCPTransport(),
    )


__all__ = [
    "CreatedLF022BatchRequest",
    "create_public_batch_request",
    "freeze_public_batch",
    "run_public_batch",
    "select_public_g_open_plan_window",
]
