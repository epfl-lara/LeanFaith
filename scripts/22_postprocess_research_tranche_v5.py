#!/usr/bin/env python3
"""Run or verify postprocess-v5 over a completed collector-v4 tranche."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.research_postprocess_v5 import (
    ResearchPostprocessV5Error,
    load_research_postprocess_v5,
    run_research_postprocess_v5,
    verify_research_postprocess_v5,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Postprocess one completed collector-v4 tranche through the "
            "immutable postprocess-v3 correctness engine."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--run", action="store_true")
    action.add_argument("--verify-only", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--collection-config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument(
        "--mathlib-project-dir",
        type=Path,
        default=Path("/storage/milikic/leanfaith/mathlib4"),
    )
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()

    def resolve(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        loaded = load_research_postprocess_v5(
            repo_root=paths.root,
            collection_root=resolve(args.collection_root),
            collection_config_path=resolve(args.collection_config),
            output_root=(resolve(args.output_root) if args.output_root is not None else None),
        )
        if args.verify_only:
            manifest = verify_research_postprocess_v5(loaded)
        else:
            backend = LeanInteractBackend(
                BackendSettings(
                    project_dir=args.mathlib_project_dir.resolve(),
                    context_fingerprint=loaded.base.context.context_fingerprint,
                    environment_schema_version=(loaded.base.context.environment_schema_version),
                    raw_response_dir=loaded.base.output_root / "lean_raw",
                )
            )
            try:
                result = run_research_postprocess_v5(
                    loaded,
                    backend=backend,
                )
            finally:
                backend.close()
            manifest = verify_research_postprocess_v5(loaded)
            if manifest != result.manifest:
                raise ResearchPostprocessV5Error(
                    "immediate v5 replay differs from the written manifest"
                )
    except (ResearchPostprocessV5Error, ValueError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"manifest_id={manifest.manifest_id}")
    print(f"tranche_id={manifest.tranche_id}")
    print(f"terminal_invocations={manifest.terminal_invocations}")
    print(f"status_counts={manifest.status_counts}")
    print(f"admitted_nl_lean_count={manifest.admitted_nl_lean_count}")
    print("semantic_labels_created=false")
    print("supervision_eligible=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
