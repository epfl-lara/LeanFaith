#!/usr/bin/env python3
"""Postprocess one completed LF-021 local research collection."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.research_postprocess import (
    ResearchPostprocessError,
    load_research_postprocess,
    run_research_postprocess,
    verify_research_postprocess,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Parse, Lean-validate, screen, and persist unresolved records for "
            "the exact completed 3x3 LF-021 public research collection."
        )
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument(
        "--mathlib-project-dir",
        type=Path,
        help="required for processing; omitted for --verify-only replay",
    )
    parser.add_argument("--output-root", type=Path)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify an already-persisted postprocessing bundle without Lean execution",
    )
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    data_root = paths.data / "parsed" / "real_outputs" / "public_research_v1"
    try:
        loaded = load_research_postprocess(
            repo_root=paths.root,
            collection_root=args.collection_root,
            problem_pool_records_path=data_root / "problem_pool_records.jsonl",
            context_path=data_root / "context.json",
            import_header_path=paths.examples / "lf021_public_research_mathlib_header_v1.lean",
            reference_theorems_path=data_root / "reference_theorems.jsonl",
            reference_representations_path=data_root / "reference_representations.jsonl",
            output_root=args.output_root,
        )
        if args.verify_only:
            manifest = verify_research_postprocess(loaded)
        else:
            if args.mathlib_project_dir is None:
                parser.error("--mathlib-project-dir is required unless --verify-only is used")
            backend = LeanInteractBackend(
                BackendSettings(
                    project_dir=args.mathlib_project_dir,
                    context_fingerprint=loaded.context.context_fingerprint,
                    environment_schema_version=loaded.context.environment_schema_version,
                    raw_response_dir=loaded.output_root / "lean_raw",
                )
            )
            try:
                result = run_research_postprocess(loaded, backend=backend)
            finally:
                backend.close()
            manifest = verify_research_postprocess(loaded)
            if manifest != result.manifest:
                raise ResearchPostprocessError(
                    "immediate replay verification differs from the written manifest"
                )
    except (ResearchPostprocessError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"manifest_id={manifest.manifest_id}")
    print(f"input_binding_hash={manifest.input_binding_hash}")
    print(f"expected_invocations={manifest.expected_invocations}")
    print(f"terminal_invocations={manifest.terminal_invocations}")
    print(f"status_counts={manifest.status_counts}")
    print(f"admitted_pair_count={manifest.admitted_pair_count}")
    print(f"admitted_nl_lean_count={manifest.admitted_nl_lean_count}")
    print("semantic_labels_created=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
