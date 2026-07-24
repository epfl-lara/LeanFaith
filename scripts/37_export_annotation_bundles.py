#!/usr/bin/env python3
"""Compatibility wrapper for ``leanfaith export-annotation``."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.annotation_support import AnnotationExportError, BlindingError
from leanfaith.cli.export_annotation import (
    AnnotationExportInputError,
    run_export_annotation,
)
from leanfaith.config.paths import RepoPaths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the exact LF-021 frame into two blinded annotation bundles."
    )
    parser.add_argument("--frame", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--randomization-key", action="append", type=Path, default=[])
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    try:
        result = run_export_annotation(
            paths=paths,
            frame_path=args.frame,
            output_root=args.output_root,
            randomization_key_paths=tuple(args.randomization_key),
        )
    except (AnnotationExportError, AnnotationExportInputError, BlindingError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 2

    for bundle in result.bundles:
        print(f"{bundle.manifest.annotator_slot}_bundle={bundle.bundle_path}")
        print(f"{bundle.manifest.annotator_slot}_manifest={bundle.manifest_path}")
    print(f"private_linkage={result.private_linkage_path}")
    print(f"private_manifest={result.private_manifest_path}")
    print("items_per_annotator=240")
    print("semantic_labels_created=0")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
