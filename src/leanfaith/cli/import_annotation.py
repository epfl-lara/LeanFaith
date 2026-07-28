"""Stable LF-023 import orchestration for locked blinded responses."""

from __future__ import annotations

from pathlib import Path

from leanfaith.annotation_support.import_ import (
    AnnotationImportRun,
    import_blinded_annotation_responses,
)
from leanfaith.config.paths import RepoPaths


def run_import_annotation(
    *,
    paths: RepoPaths,
    public_bundle_manifest_path: Path,
    private_linkage_manifest_path: Path,
    human_assignment_path: Path,
    human_submission_attestation_path: Path,
    authentication_key_path: Path,
    response_path: Path,
    output_root: Path | None = None,
) -> AnnotationImportRun:
    """Validate, privately link, and preserve one annotator-slot submission."""

    output = output_root or paths.data / "human" / "pilot_raw" / "lf021_prevalence_v1"
    return import_blinded_annotation_responses(
        repo_root=paths.root,
        public_bundle_manifest_path=public_bundle_manifest_path,
        private_linkage_manifest_path=private_linkage_manifest_path,
        human_assignment_path=human_assignment_path,
        human_submission_attestation_path=human_submission_attestation_path,
        authentication_key_path=authentication_key_path,
        response_path=response_path,
        output_root=output,
    )
