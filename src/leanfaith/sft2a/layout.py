"""SFT2A shared-cache and immutable versioned-output routing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.models import SFT2AOpusConfig


class RunLayoutError(RuntimeError):
    """A configured output route is absolute, escaping, or ambiguous."""


def _child(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise RunLayoutError(f"unsafe SFT2A output subdirectory: {relative!r}")
    return root.joinpath(*pure.parts)


@dataclass(frozen=True, slots=True)
class SFT2ARunPaths:
    shared_cache_root: Path
    one_root: Path
    audit: Path
    comparison: Path
    pilot: Path
    legacy_rejudge: Path
    post_audit: Path
    historical_fable_one_root: Path
    historical_fable_audit: Path


def run_paths(loaded: LoadedSFT2AConfig) -> SFT2ARunPaths:
    staging = Path(loaded.config.staging_root)
    if not isinstance(loaded.config, SFT2AOpusConfig):
        return SFT2ARunPaths(
            shared_cache_root=staging,
            one_root=staging / "one_root_v1",
            audit=staging / "audit_lemex_v1",
            comparison=staging / "comparison_fable_opus_v1",
            pilot=staging / "pilot_v1",
            legacy_rejudge=staging / "legacy_rejudge_v1",
            post_audit=staging / "post_audit_release_v1",
            historical_fable_one_root=staging / "one_root_v1",
            historical_fable_audit=staging / "audit_lemex_v1",
        )
    layout = loaded.config.run_layout
    shared = Path(layout.shared_cache_root)
    if shared != staging:
        raise RunLayoutError(
            "Opus shared_cache_root must equal the historical staging root so proposer and Lean "
            "cache keys remain reusable"
        )
    return SFT2ARunPaths(
        shared_cache_root=shared,
        one_root=_child(staging, layout.run_output_subdir),
        audit=_child(staging, layout.audit_output_subdir),
        comparison=_child(staging, layout.comparison_output_subdir),
        pilot=_child(staging, layout.pilot_output_subdir),
        legacy_rejudge=_child(staging, layout.legacy_rejudge_output_subdir),
        post_audit=_child(staging, layout.post_audit_output_subdir),
        historical_fable_one_root=_child(staging, layout.historical_fable_run_subdir),
        historical_fable_audit=staging / "audit_lemex_v1",
    )


__all__ = ["RunLayoutError", "SFT2ARunPaths", "run_paths"]
