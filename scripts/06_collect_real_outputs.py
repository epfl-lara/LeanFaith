#!/usr/bin/env python3
"""Compatibility wrapper for the LF-021 real-output collection command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from leanfaith.cli.collect_real_outputs import (
    LF021FoundationError,
    run_lf021_offline_smoke,
    validate_lf021_foundation,
)
from leanfaith.config.paths import RepoPaths


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the fail-closed LF-021 foundation or run the ADR-0005 "
            "one-example offline fixture/replay."
        )
    )
    parser.add_argument("--validate-foundation", action="store_true")
    parser.add_argument("--run-offline-smoke", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.validate_foundation == args.run_offline_smoke:
        parser.error("pass exactly one of --validate-foundation or --run-offline-smoke")

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    if args.run_offline_smoke:
        try:
            result = run_lf021_offline_smoke(
                paths,
                output_dir=args.output_dir,
                argv=(str(Path(__file__)), *sys.argv[1:]),
            )
        except (LF021FoundationError, ValueError, OSError) as exc:
            print(f"FAILED: {exc}")
            return 1
        print(f"output={result.output_dir}")
        print(f"report={result.report_path}")
        print(f"run_manifest={result.run_manifest_path}")
        print("network_calls_made=0")
        print("semantic_labels_created=0")
        print("gate_5_closed=false")
        return 0 if result.report.passed else 1

    try:
        result = validate_lf021_foundation(paths)
    except (LF021FoundationError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"problem_pool_config_hash={result.report.problem_pool_config_hash}")
    print(f"real_outputs_config_hash={result.report.real_outputs_config_hash}")
    print(f"provider_registry_sha256={result.report.provider_registry_sha256}")
    print("execution_authorized=false")
    print("provider_calls_made=0")
    print("semantic_labels_created=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
