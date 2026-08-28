#!/usr/bin/env python3
"""Compatibility wrapper for the canonical Phase-4 transformation command.

LF-016 exposes validation only. The same command grows a generation mode in
LF-017/LF-018 after scoped transformation implementations exist.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.cli.negative_pre_scale import (
    NegativePreScaleAuditError,
    run_negative_pre_scale_audit,
)
from leanfaith.cli.negative_transformations import (
    NegativeRuleValidationError,
    validate_negative_rule_implementations,
)
from leanfaith.cli.positive_transformations import (
    PositiveRuleValidationError,
    validate_positive_rule_implementations,
)
from leanfaith.cli.transformations import (
    TransformationFrameworkValidationError,
    validate_transformation_framework,
)
from leanfaith.config.paths import RepoPaths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate/freeze LF-016 infrastructure; generate zero variants.",
    )
    parser.add_argument(
        "--validate-positives",
        action="store_true",
        help="Validate/hash-bind LF-017 P01/P02/P04-lite; generate zero variants.",
    )
    parser.add_argument(
        "--validate-negatives",
        action="store_true",
        help="Validate/hash-bind LF-018 N01/N02/N03/N07/N10; generate zero variants.",
    )
    parser.add_argument(
        "--run-negative-pre-scale",
        action="store_true",
        help="Run and persist the Lean-backed LF-018 five-family pre-scale slice.",
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    if (
        sum(
            (
                args.validate_only,
                args.validate_positives,
                args.validate_negatives,
                args.run_negative_pre_scale,
            )
        )
        > 1
    ):
        parser.error(
            "--validate-only, --validate-positives, --validate-negatives, and "
            "--run-negative-pre-scale are mutually exclusive"
        )
    if not any(
        (
            args.validate_only,
            args.validate_positives,
            args.validate_negatives,
            args.run_negative_pre_scale,
        )
    ):
        parser.error(
            "pass --validate-only, --validate-positives, --validate-negatives, "
            "or --run-negative-pre-scale"
        )
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    if args.run_negative_pre_scale:
        try:
            pre_scale_result = run_negative_pre_scale_audit(
                paths=paths,
                output_dir=args.output_dir,
                report_path=args.report,
            )
        except NegativePreScaleAuditError as exc:
            print(
                f"FAILED: {exc}; report={exc.artifacts.report_path}; "
                f"output_manifest={exc.artifacts.output_manifest_path}; "
                f"run_manifest={exc.artifacts.run_manifest_path}"
            )
            return 1
        print(f"output={pre_scale_result.output_dir}")
        print(f"report={pre_scale_result.report_path}")
        print(f"run_manifest={pre_scale_result.run_manifest_path}")
        print("generated_drafts=5")
        print("generated_pairs=5")
        print("resolved_semantic_labels=0")
        print("promoted_items=0")
        print("gate_4g_closed=false")
        return 0
    if args.validate_positives:
        try:
            positive_result = validate_positive_rule_implementations(
                paths=paths,
                report_path=args.report,
            )
        except PositiveRuleValidationError as exc:
            print(f"FAILED: {exc}; failure_report={exc.report_path}")
            return 1
        print(f"report={positive_result.report_path}")
        print(f"run_manifest={positive_result.run_manifest_path}")
        print("generated_drafts=0")
        print("resolved_semantic_labels=0")
        return 0
    if args.validate_negatives:
        try:
            negative_result = validate_negative_rule_implementations(
                paths=paths,
                report_path=args.report,
            )
        except NegativeRuleValidationError as exc:
            print(f"FAILED: {exc}; failure_report={exc.report_path}")
            return 1
        print(f"report={negative_result.report_path}")
        print(f"run_manifest={negative_result.run_manifest_path}")
        print("generated_drafts=0")
        print("generated_pairs=0")
        print("resolved_semantic_labels=0")
        print("promoted_items=0")
        print("gate_4g_closed=false")
        return 0
    try:
        validation_result = validate_transformation_framework(
            paths=paths,
            report_path=args.report,
        )
    except TransformationFrameworkValidationError as exc:
        print(f"FAILED: {exc}; failure_report={exc.report_path}")
        return 1
    print(f"registry_snapshot={validation_result.snapshot_path}")
    print(f"report={validation_result.report_path}")
    print(f"run_manifest={validation_result.run_manifest_path}")
    print("generated_drafts=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
