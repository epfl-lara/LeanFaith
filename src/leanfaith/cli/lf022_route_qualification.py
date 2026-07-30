"""CLI adapter for offline LF-022 proposer qualification certification."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from leanfaith.config.hashing import hash_file
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.generation.lf022_route_qualification import (
    CertifiedLF022ProposerRoute,
    certify_lf022_proposer_production_eligibility,
)


def _binding(repo_root: Path, path: Path, *, label: str) -> LF022ArtifactBinding:
    root = repo_root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} must be a repository-local regular file") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} must be a repository-local regular file")
    return LF022ArtifactBinding(
        path=PurePosixPath(relative.as_posix()).as_posix(),
        sha256=hash_file(candidate),
    )


def certify_proposer_route(
    *,
    repo_root: Path,
    qualification_admission_path: Path,
    qualification_task_path: Path,
) -> CertifiedLF022ProposerRoute:
    """Replay persisted live evidence and create no provider request."""

    return certify_lf022_proposer_production_eligibility(
        repo_root=repo_root,
        qualification_admission_binding=_binding(
            repo_root,
            qualification_admission_path,
            label="qualification admission",
        ),
        qualification_task_binding=_binding(
            repo_root,
            qualification_task_path,
            label="qualification task",
        ),
    )


__all__ = ["certify_proposer_route"]
