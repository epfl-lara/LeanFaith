#!/usr/bin/env python3
"""Build the model-free LF-021 public research problem-pool slice."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.cli.pipeline import default_mathlib_checkout
from leanfaith.config.paths import RepoPaths
from leanfaith.generation.public_research_pool import (
    PublicResearchPoolError,
    run_public_research_pool,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight one or three public mathlib docstring/reference records. "
            "This command never loads or executes a generator."
        )
    )
    parser.add_argument(
        "--profile",
        choices=("one_example_preflight_v1", "three_record_slice_v1"),
        required=True,
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--public-source-repo",
        type=Path,
        default=default_mathlib_checkout(),
        help="Git object store containing the exact public source snapshot.",
    )
    parser.add_argument(
        "--execution-project-dir",
        type=Path,
        default=default_mathlib_checkout(),
        help="Pinned LeanFaith mathlib checkout used only through LeanInteract.",
    )
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    try:
        result = run_public_research_pool(
            paths=paths,
            public_source_repo=args.public_source_repo.resolve(),
            execution_project_dir=args.execution_project_dir.resolve(),
            profile=args.profile,
        )
    except (PublicResearchPoolError, ValueError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"profile={result.report.profile}")
    print(f"passed={str(result.report.passed).lower()}")
    print(f"records={result.report.source_record_count}")
    print(f"eligible={result.report.eligible_problem_count}")
    print(f"three_screen_clear={result.report.clear_three_screen_count}")
    print(f"manifest={result.manifest_path}")
    print(f"report={result.report_path}")
    print("model_execution_performed=false")
    print("semantic_labels_created=false")
    print("gate_5g_closed=false")
    print("gate_5_closed=false")
    return 0 if result.report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
