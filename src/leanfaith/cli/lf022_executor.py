"""CLI boundary for public, provisional LF-022 proposer execution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_execution import (
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
)
from leanfaith.generation.lf022_executor import (
    LF022ExecutionResult,
    RCPRuntimeCredentials,
    execute_lf022_g_open_task,
)
from leanfaith.generation.rcp_provider import UrllibOpenAICompatibleRCPTransport


def _load_canonical[RecordT: StrictModel](
    path: Path,
    model: type[RecordT],
    *,
    label: str,
) -> RecordT:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or unsafe: {path}")
    raw = path.read_bytes()
    try:
        record = model.model_validate(cast(object, json.loads(raw)))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    canonical = canonical_json_bytes(record.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise ValueError(f"{label} is not canonical JSON")
    return record


def run_lf022_public_provisional(
    *,
    repo_root: Path,
    admission_path: Path,
    task_path: Path,
    output_root: Path,
    execute_public_provisional: bool,
) -> LF022ExecutionResult:
    """Preflight one task by default; resolve credentials only for explicit live mode."""

    admission = _load_canonical(
        admission_path,
        LF022GOpenExecutionAdmission,
        label="LF-022 execution admission",
    )
    task = _load_canonical(
        task_path,
        LF022GOpenExecutionTask,
        label="LF-022 execution task",
    )
    if not execute_public_provisional:
        return execute_lf022_g_open_task(
            repo_root=repo_root,
            output_root=output_root,
            admission=admission,
            task=task,
        )
    base_url = os.environ.get("RCP_BASE_URL", "")
    api_key = os.environ.get("RCP_API_KEY", "")
    if not base_url or not api_key:
        raise ValueError("explicit live execution requires nonempty RCP_BASE_URL and RCP_API_KEY")
    return execute_lf022_g_open_task(
        repo_root=repo_root,
        output_root=output_root,
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=RCPRuntimeCredentials(base_url=base_url, api_key=api_key),
        transport=UrllibOpenAICompatibleRCPTransport(),
    )


__all__ = ["run_lf022_public_provisional"]
