"""Profile-aware runtime dispatch for experimental deterministic-v2 E2 families."""

from __future__ import annotations

from pathlib import Path

from leanfaith.config.loading import load_yaml_mapping
from leanfaith.config.paths import find_repo_root
from leanfaith.transforms.v2_e2_p15_runtime import (
    V2E2P15Runtime,
    build_v2_e2_p15_runtime,
)
from leanfaith.transforms.v2_e2_p16_runtime import (
    V2E2P16Runtime,
    build_v2_e2_p16_runtime,
)

type V2E2Runtime = V2E2P15Runtime | V2E2P16Runtime


def build_v2_e2_runtime(
    repo_root: Path | None = None,
    *,
    path: Path,
) -> V2E2Runtime:
    """Load one exact E2 profile and dispatch to its closed runtime schema."""

    root = find_repo_root(repo_root)
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("v2 E2 config escapes the repository")
    profile_id = load_yaml_mapping(resolved).get("profile_id")
    if profile_id == "deterministic_v2_e2_p15_experimental":
        return build_v2_e2_p15_runtime(root, path=resolved)
    if profile_id == "deterministic_v2_e2_p16_experimental":
        return build_v2_e2_p16_runtime(root, path=resolved)
    raise ValueError(f"unsupported deterministic-v2 E2 profile: {profile_id!r}")


__all__ = ["V2E2Runtime", "build_v2_e2_runtime"]
