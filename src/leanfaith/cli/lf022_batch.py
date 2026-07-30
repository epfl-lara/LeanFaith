"""CLI adapters for deterministic public LF-022 batches."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from leanfaith.config.hashing import hash_file
from leanfaith.generation.lf022_batch import (
    FrozenLF022PublicBatch,
    LF022BatchRunPolicy,
    LF022BatchRunResult,
    freeze_lf022_public_batch,
    run_lf022_public_batch,
)
from leanfaith.generation.lf022_executor import RCPRuntimeCredentials
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.generation.rcp_provider import UrllibOpenAICompatibleRCPTransport


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


__all__ = ["freeze_public_batch", "run_public_batch"]
