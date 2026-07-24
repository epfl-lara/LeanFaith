#!/usr/bin/env python3
"""Curate frozen Gate-3 mathlib docstrings for local LF-021 collection."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.cli.pipeline import default_mathlib_checkout
from leanfaith.config.paths import RepoPaths
from leanfaith.generation.gate3_docstring_curation import (
    DEFAULT_OUTPUT_ROOT,
    Gate3DocstringCurationError,
    run_gate3_docstring_curation,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply the frozen Codex-agent/LLM-assisted operational curation "
            "to 57 Gate-3 mathlib docstring candidates and kernel-check the "
            "reference statement for every admitted record. This is not "
            "human review or semantic labeling."
        )
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--mathlib-checkout",
        type=Path,
        default=default_mathlib_checkout(),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
    )
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    try:
        result = run_gate3_docstring_curation(
            paths=paths,
            mathlib_checkout=args.mathlib_checkout,
            output_root=args.output_root,
        )
    except (Gate3DocstringCurationError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print("passed=true")
    print(f"reviewed={result.report.reviewed_count}")
    print(f"admitted={result.report.admitted_count}")
    print(f"excluded={result.report.excluded_count}")
    print(f"ambiguous_exclusions={result.report.ambiguous_exclusion_count}")
    print(f"manifest={result.manifest_path}")
    print(f"report={result.report_path}")
    print("reviewer_type=codex_agent")
    print("human_reviewed=false")
    print("semantic_labels_created=false")
    print("model_execution_performed=false")
    print("gate_claimed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
