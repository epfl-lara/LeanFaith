#!/usr/bin/env python3
"""Prepare, preflight, execute, or verify one frozen extension tranche."""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.post_exhaustion_collection_v6 import (
    LocalHFResearchExecutor,
    PostExhaustionCollectionV6Error,
    execute_post_exhaustion_collection_v6,
    load_post_exhaustion_collection_v6,
    prepare_post_exhaustion_collection_v6,
    verify_post_exhaustion_collection_v6,
    write_post_exhaustion_collection_preflight_v6,
)

_DEFAULT_POLICY = Path("configs/generation/lf021_post_exhaustion_execution_v1.yaml")


def _utc(value: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("frozen time must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        raise argparse.ArgumentTypeError("frozen time must be UTC")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute only a strictly replayed s6/s7 LF-021 extension tranche "
            "over the exact public reference-hidden pools and three local families."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--execute", action="store_true")
    action.add_argument("--verify-only", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--policy", type=Path, default=_DEFAULT_POLICY)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--planning-output-root", type=Path)
    parser.add_argument("--output-config", type=Path)
    parser.add_argument("--frozen-at", type=_utc)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()

    def resolve(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        if args.prepare:
            required = (
                args.authorization,
                args.planning_output_root,
                args.output_config,
                args.frozen_at,
            )
            if any(value is None for value in required):
                raise PostExhaustionCollectionV6Error(
                    "--prepare requires --authorization, --planning-output-root, "
                    "--output-config, and --frozen-at"
                )
            assert args.authorization is not None
            assert args.planning_output_root is not None
            assert args.output_config is not None
            assert args.frozen_at is not None
            output, digest = prepare_post_exhaustion_collection_v6(
                repo_root=paths.root,
                execution_policy_path=resolve(args.policy),
                authorization_path=resolve(args.authorization),
                frozen_at=args.frozen_at,
                planning_output_root=resolve(args.planning_output_root),
                output_config_path=resolve(args.output_config),
            )
            print(f"config={output}")
            print(f"config_sha256={digest}")
            print("actual_collection_performed=false")
            print("semantic_labels_inspected=false")
            print("semantic_labels_created=false")
            print("supervision_eligible=false")
            print("gate_5g_credit_claimed=false")
            print("gate_5_closed=false")
            return 0
        if args.config is None:
            raise PostExhaustionCollectionV6Error(
                "--preflight, --execute, and --verify-only require --config"
            )
        loaded = load_post_exhaustion_collection_v6(
            resolve(args.config),
            repo_root=paths.root,
        )
        if args.preflight:
            report_path, report_hash = write_post_exhaustion_collection_preflight_v6(
                loaded,
                repo_root=paths.root,
            )
            print("execution_ready=true")
            print(f"authorization_id={loaded.authorization.authorization_id}")
            print(f"extension_decision_id={loaded.authorization.extension_decision_id}")
            print(f"planning_plan_id={loaded.planning_plan.plan_id}")
            print(f"tranche_id={loaded.config.config.tranche_id}")
            print(f"tranche_order={loaded.config.config.tranche_order}")
            print(f"planned_candidates={loaded.planning_plan.expected_candidate_count}")
            print(f"preflight_report={report_path}")
            print(f"preflight_sha256={report_hash}")
            print("gpu_model_execution_performed=false")
            print("remote_provider_requests_created=0")
            return 0
        if args.verify_only:
            manifest = verify_post_exhaustion_collection_v6(
                loaded,
                repo_root=paths.root,
            )
        else:
            run = execute_post_exhaustion_collection_v6(
                loaded,
                repo_root=paths.root,
                executor=LocalHFResearchExecutor(),
            )
            manifest = verify_post_exhaustion_collection_v6(
                loaded,
                repo_root=paths.root,
            )
            if manifest != run.manifest:
                raise PostExhaustionCollectionV6Error("immediate collector-v6 replay differs")
    except (PostExhaustionCollectionV6Error, OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"manifest_id={manifest.manifest_id}")
    print(f"tranche_id={manifest.tranche_id}")
    print(f"terminal_candidates={manifest.terminal_candidate_count}")
    print(f"status_counts={manifest.status_counts}")
    print("actual_collection_performed=true")
    print("parser_executed=false")
    print("semantic_labels_inspected=false")
    print("semantic_labels_created=false")
    print("supervision_eligible=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
