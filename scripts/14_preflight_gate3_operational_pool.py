#!/usr/bin/env python3
"""Materialize the 40-record Gate-3 operational LF-021 problem pool."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.cli.pipeline import default_mathlib_checkout
from leanfaith.config.paths import RepoPaths
from leanfaith.generation.gate3_operational_pool import (
    Gate3OperationalPoolError,
    run_gate3_operational_pool,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the exact 40-record, local-model-authorized public pool. "
            "This command never loads or executes a generator."
        )
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--mathlib-checkout",
        type=Path,
        default=default_mathlib_checkout(),
        help="Pinned mathlib checkout containing the exact source Git objects.",
    )
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    try:
        result = run_gate3_operational_pool(
            paths=paths,
            mathlib_checkout=args.mathlib_checkout.resolve(),
        )
    except (Gate3OperationalPoolError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"passed={str(result.report.passed).lower()}")
    print(f"problems={result.manifest.problem_count}")
    print(f"eligible={result.manifest.eligible_problem_count}")
    print(f"model_collection_authorized={result.manifest.model_collection_authorized_count}")
    print("collection_scope=local_models_only")
    print("reference_visible_to_generator=false")
    print("human_reviewed=false")
    print("semantic_gold_created=false")
    print("gate_claimed=false")
    print("model_execution_performed=false")
    print("generator_collection_plan_created=false")
    print("recovery_parser_binding_status=unresolved")
    print(f"manifest={result.manifest_path}")
    print(f"adequacy_report={result.adequacy_report_path}")
    print(f"report={result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
