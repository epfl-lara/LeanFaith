"""Stable orchestration for the LF-021 blinded prevalence-annotation export."""

from __future__ import annotations

from pathlib import Path

from leanfaith.annotation_support import (
    EXACT_FRAME_RELATIVE_PATH,
    AnnotationExportRun,
    export_blinded_annotation_bundles,
)
from leanfaith.config.paths import RepoPaths


class AnnotationExportInputError(ValueError):
    """Raised when CLI randomization-key inputs are incomplete or unsafe."""


def _read_randomization_keys(paths: tuple[Path, ...]) -> tuple[bytes, bytes] | None:
    if not paths:
        return None
    if len(paths) != 2:
        raise AnnotationExportInputError(
            "provide exactly two --randomization-key files, one per independent annotator"
        )
    resolved = tuple(path.resolve(strict=True) for path in paths)
    if resolved[0] == resolved[1]:
        raise AnnotationExportInputError("independent annotators require distinct key files")
    keys = tuple(path.read_bytes() for path in resolved)
    if any(len(key) < 32 for key in keys):
        raise AnnotationExportInputError("each randomization key must contain at least 32 bytes")
    return keys[0], keys[1]


def run_export_annotation(
    *,
    paths: RepoPaths,
    frame_path: Path | None = None,
    output_root: Path | None = None,
    randomization_key_paths: tuple[Path, ...] = (),
) -> AnnotationExportRun:
    """Export the exact frozen frame into two blinded annotator bundles."""

    frame = frame_path or paths.root / EXACT_FRAME_RELATIVE_PATH
    output = output_root or paths.annotation / "exports" / "lf021_prevalence_v1"
    entropy_by_slot = _read_randomization_keys(randomization_key_paths)
    return export_blinded_annotation_bundles(
        repo_root=paths.root,
        frame_path=frame,
        output_root=output,
        entropy_by_slot=entropy_by_slot,
    )
