#!/usr/bin/env python3
"""Run or verify the model-free LF-021 non-Algebra feasibility probe."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.cli.pipeline import default_mathlib_checkout
from leanfaith.config.paths import RepoPaths
from leanfaith.generation.cross_domain_docstring_probe import (
    DEFAULT_OUTPUT_ROOT,
    CrossDomainProbeError,
    run_cross_domain_probe,
    verify_cross_domain_probe,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe trusted contributor docstrings and Lean references across "
            "non-Algebra mathlib directory proxies. No model is loaded, no "
            "semantic label is created, and no Gate claim is made."
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
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root else RepoPaths.discover()
    try:
        if args.verify_only:
            result = verify_cross_domain_probe(
                paths=paths,
                output_root=args.output_root,
            )
        else:
            result = run_cross_domain_probe(
                paths=paths,
                mathlib_checkout=args.mathlib_checkout,
                output_root=args.output_root,
            )
    except (CrossDomainProbeError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"passed={str(result.report.passed).lower()}")
    print(f"selected={result.report.selected_count}")
    print(f"domains={','.join(result.report.selected_domain_proxies)}")
    print(f"manifest_id={result.manifest.manifest_id}")
    print(f"manifest={result.manifest_path}")
    print(f"report={result.report_path}")
    print("model_execution_performed=false")
    print("semantic_labels_created=false")
    print("problem_pool_admitted=false")
    print("model_collection_authorized=false")
    print("gate_claimed=false")
    return 0 if result.report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
