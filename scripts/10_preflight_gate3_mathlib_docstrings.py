#!/usr/bin/env python3
"""Build the model-free Gate-3 mathlib adjacent-docstring candidate pool."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.cli.pipeline import default_mathlib_checkout
from leanfaith.config.paths import RepoPaths
from leanfaith.generation.gate3_docstring_pool import (
    DEFAULT_OUTPUT_ROOT,
    Gate3DocstringPoolError,
    run_gate3_docstring_pool,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Screen adjacent contributor docstrings on frozen Gate-3 mathlib "
            "TheoremRecords. This command never loads or executes a model and "
            "never creates a semantic label."
        )
    )
    parser.add_argument("--profile", choices=("one_example", "full"), required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--mathlib-checkout",
        type=Path,
        default=default_mathlib_checkout(),
        help="Pinned mathlib Git checkout at the Gate-3 source revision.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Content-addressed research artifact directory.",
    )
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    try:
        result = run_gate3_docstring_pool(
            paths=paths,
            mathlib_checkout=args.mathlib_checkout,
            output_root=args.output_root,
            profile=args.profile,
        )
    except (Gate3DocstringPoolError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"profile={result.report.profile}")
    print(f"passed={str(result.report.passed).lower()}")
    print(f"attempted={result.manifest.attempted_mathlib_records}")
    print(f"adjacent_docstrings={result.manifest.adjacent_docstring_records}")
    print(f"eligible_groups={result.report.eligible_distinct_ancestry_groups}")
    print(f"selected={result.report.selected_count}")
    print(f"shortfall={result.report.shortfall}")
    print(f"manifest={result.manifest_path}")
    print(f"report={result.report_path}")
    print("model_execution_performed=false")
    print("semantic_labels_created=false")
    print("candidate_source_records_only=true")
    print("self_containedness_status=unreviewed")
    print("problem_pool_admitted=false")
    print("model_collection_authorized=false")
    print("gate_claimed=false")
    return 0 if result.report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
