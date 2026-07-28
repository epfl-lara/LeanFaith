#!/usr/bin/env python3
"""Compatibility wrapper for ``leanfaith validate-lf022``."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.cli.lf022 import run_lf022_validation
from leanfaith.config.paths import RepoPaths
from leanfaith.generation.providers import ProviderError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate fail-closed LF-022 configs or replay one immutable response."
    )
    parser.add_argument(
        "--variants-config",
        type=Path,
        default=Path("configs/generation/llm_variants_v1.yaml"),
    )
    parser.add_argument(
        "--judges-config",
        type=Path,
        default=Path("configs/judges/weak_supervision.yaml"),
    )
    parser.add_argument("--replay-kind", choices=("proposer", "judge"))
    parser.add_argument("--request", type=Path)
    parser.add_argument("--raw-response-root", type=Path)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/milestones/lf022_foundation_validation.json"),
    )
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()

    def anchored(path: Path | None) -> Path | None:
        if path is None or path.is_absolute():
            return path
        return paths.root / path

    try:
        result = run_lf022_validation(
            paths=paths,
            variants_config_path=anchored(args.variants_config) or args.variants_config,
            judges_config_path=anchored(args.judges_config) or args.judges_config,
            report_path=anchored(args.report) or args.report,
            replay_kind=args.replay_kind,
            request_path=anchored(args.request),
            raw_response_root=anchored(args.raw_response_root),
        )
    except (OSError, ProviderError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 2

    print(f"report={result.report_path}")
    print(f"sha256={result.report_sha256}")
    if result.report.replay is not None:
        print(f"replay={result.report.replay.replay_kind}")
        print(f"parsed_items={result.report.replay.parsed_item_count}")
    else:
        print("replay=none")
    print("live_calls=0")
    print("semantic_labels_created=0")
    print("silver_records_created=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
