#!/usr/bin/env python3
"""Process or replay an arbitrary completed LF-021 collection-v2 run."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.research_collection_v2 import ResearchCollectionV2Error
from leanfaith.generation.research_postprocess import ResearchPostprocessError
from leanfaith.generation.research_postprocess_v3 import (
    ResearchPostprocessV3Error,
    load_research_postprocess_v3,
    run_research_postprocess_v3,
    verify_research_postprocess_v3,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend

_DEFAULT_CONFIG = Path("configs/generation/local_research_collection_v2.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Parse, conservatively recover, Lean-validate, screen, and persist "
            "unresolved REVIEW-only records for an exact collection-v2 run."
        )
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument(
        "--collection-config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help="frozen collection-v2 config used to reproduce plan.json",
    )
    parser.add_argument(
        "--mathlib-project-dir",
        type=Path,
        help="required for processing; omitted for --verify-only replay",
    )
    parser.add_argument("--output-root", type=Path)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify an existing postprocess_v3 bundle without Lean execution",
    )
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    config_path = args.collection_config
    if not config_path.is_absolute():
        config_path = paths.root / config_path
    try:
        loaded = load_research_postprocess_v3(
            repo_root=paths.root,
            collection_root=args.collection_root,
            collection_config_path=config_path,
            output_root=args.output_root,
        )
        if args.verify_only:
            manifest = verify_research_postprocess_v3(loaded)
        else:
            if args.mathlib_project_dir is None:
                parser.error("--mathlib-project-dir is required unless --verify-only is used")
            backend = LeanInteractBackend(
                BackendSettings(
                    project_dir=args.mathlib_project_dir,
                    context_fingerprint=loaded.base.context.context_fingerprint,
                    environment_schema_version=(loaded.base.context.environment_schema_version),
                    raw_response_dir=loaded.base.output_root / "lean_raw",
                )
            )
            try:
                result = run_research_postprocess_v3(loaded, backend=backend)
            finally:
                backend.close()
            manifest = verify_research_postprocess_v3(loaded)
            if manifest != result.manifest:
                raise ResearchPostprocessV3Error(
                    "immediate v3 replay differs from the written manifest"
                )
    except (
        ResearchCollectionV2Error,
        ResearchPostprocessError,
        ResearchPostprocessV3Error,
        OSError,
        ValueError,
    ) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"manifest_id={manifest.manifest_id}")
    print(f"input_binding_hash={manifest.input_binding_hash}")
    print(f"problem_count={manifest.problem_count}")
    print(f"family_count={manifest.family_count}")
    print(f"seed_count_by_family={manifest.seed_count_by_family}")
    print(f"expected_invocations={manifest.expected_invocations}")
    print(f"terminal_invocations={manifest.terminal_invocations}")
    print(f"status_counts={manifest.status_counts}")
    print(f"recovery_status_counts={manifest.recovery_status_counts}")
    print(f"admitted_pair_count={manifest.admitted_pair_count}")
    print(f"admitted_nl_lean_count={manifest.admitted_nl_lean_count}")
    print("semantic_labels_created=false")
    print("supervision_eligible=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
