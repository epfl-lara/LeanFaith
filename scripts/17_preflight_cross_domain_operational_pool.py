#!/usr/bin/env python3
"""Materialize or exactly verify the model-free LF-021 cross-domain pool."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.cli.pipeline import default_mathlib_checkout
from leanfaith.config.paths import RepoPaths
from leanfaith.generation.cross_domain_operational_pool import (
    CrossDomainOperationalPoolError,
    run_cross_domain_operational_pool,
    verify_cross_domain_operational_pool,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed curation and preflight of cross-domain mathlib docstrings. "
            "No generator is loaded, no semantic label is created, and no Gate is claimed."
        )
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--mathlib-checkout",
        type=Path,
        default=default_mathlib_checkout(),
    )
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root else RepoPaths.discover()
    try:
        if args.verify_only:
            result = verify_cross_domain_operational_pool(paths=paths)
        else:
            result = run_cross_domain_operational_pool(
                paths=paths,
                mathlib_checkout=args.mathlib_checkout.resolve(),
            )
    except (CrossDomainOperationalPoolError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"passed={str(result.report.passed).lower()}")
    print(f"reviewed={result.report.reviewed_count}")
    print(f"admitted={result.report.admitted_count}")
    print(f"excluded={result.report.excluded_count}")
    print(
        "admitted_by_proxy="
        + ",".join(
            f"{proxy}:{count}" for proxy, count in sorted(result.report.admitted_by_proxy.items())
        )
    )
    print(
        "excluded_by_proxy="
        + ",".join(
            f"{proxy}:{count}" for proxy, count in sorted(result.report.excluded_by_proxy.items())
        )
    )
    print("collection_scope=local_models_only")
    print("reference_visible_to_generator=false")
    print("human_reviewed=false")
    print("semantic_labels_created=false")
    print("gate_claimed=false")
    print("model_execution_performed=false")
    print("generator_collection_plan_created=false")
    print(f"manifest_id={result.manifest.manifest_id}")
    print(f"manifest={result.manifest_path}")
    print(f"adequacy_report={result.adequacy_report_path}")
    print(f"report={result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
