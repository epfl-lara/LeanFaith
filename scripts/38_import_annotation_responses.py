#!/usr/bin/env python3
"""Compatibility wrapper for ``leanfaith import-annotation``."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.annotation_support import AnnotationImportError
from leanfaith.cli.import_annotation import run_import_annotation
from leanfaith.config.paths import RepoPaths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import one locked LF-023 blinded annotation response set."
    )
    parser.add_argument("--bundle-manifest", type=Path, required=True)
    parser.add_argument("--private-linkage-manifest", type=Path, required=True)
    parser.add_argument("--human-assignment", type=Path, required=True)
    parser.add_argument("--human-submission-attestation", type=Path, required=True)
    parser.add_argument("--authentication-key", type=Path, required=True)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()

    def anchored(path: Path | None) -> Path | None:
        if path is None or path.is_absolute():
            return path
        return paths.root / path

    try:
        result = run_import_annotation(
            paths=paths,
            public_bundle_manifest_path=anchored(args.bundle_manifest) or args.bundle_manifest,
            private_linkage_manifest_path=anchored(args.private_linkage_manifest)
            or args.private_linkage_manifest,
            human_assignment_path=anchored(args.human_assignment) or args.human_assignment,
            human_submission_attestation_path=anchored(args.human_submission_attestation)
            or args.human_submission_attestation,
            authentication_key_path=anchored(args.authentication_key) or args.authentication_key,
            response_path=anchored(args.responses) or args.responses,
            output_root=anchored(args.output_root),
        )
    except (AnnotationImportError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 2
    print(f"slot={result.manifest.annotator_slot}")
    print(f"responses={result.manifest.response_count}/240")
    print(f"complete={str(result.manifest.complete).lower()}")
    print(f"manifest={result.manifest_path}")
    print("adjudications_created=0")
    print("promoted_labels_created=0")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
