#!/usr/bin/env python3
"""Prepare, preflight, or execute one generic LF-021 collector-v4 tranche."""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.research_collection_v4 import (
    LocalHFResearchExecutor,
    ResearchCollectionV4Error,
    derive_research_collection_v4_config,
    execute_research_collection_v4,
    load_research_collection_v4,
    write_preflight_report_v4,
)


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
            "Create an immutable collector-v4 tranche config from a bound v2/v3 "
            "pool config, preflight it without GPU imports, or execute/resume it."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--base-config", type=Path)
    parser.add_argument("--expansion-decision", type=Path)
    parser.add_argument("--expansion-policy", type=Path)
    parser.add_argument("--output-source-matrix", type=Path)
    parser.add_argument("--output-config", type=Path)
    parser.add_argument("--frozen-at", type=_utc)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    try:
        if args.prepare:
            required = (
                args.base_config,
                args.expansion_decision,
                args.expansion_policy,
                args.output_source_matrix,
                args.output_config,
                args.frozen_at,
            )
            if any(value is None for value in required):
                raise ResearchCollectionV4Error(
                    "--prepare requires --base-config, --expansion-decision, "
                    "--expansion-policy, --output-source-matrix, "
                    "--output-config, and --frozen-at"
                )
            base = args.base_config
            decision = args.expansion_decision
            policy = args.expansion_policy
            source_matrix = args.output_source_matrix
            output = args.output_config
            if not base.is_absolute():
                base = paths.root / base
            if not decision.is_absolute():
                decision = paths.root / decision
            if not policy.is_absolute():
                policy = paths.root / policy
            if not source_matrix.is_absolute():
                source_matrix = paths.root / source_matrix
            if not output.is_absolute():
                output = paths.root / output
            path, digest = derive_research_collection_v4_config(
                base_config_path=base,
                expansion_decision_path=decision,
                expansion_policy_path=policy,
                output_source_matrix_path=source_matrix,
                output_config_path=output,
                repo_root=paths.root,
                frozen_at=args.frozen_at,
            )
            print(f"config={path}")
            print(f"config_sha256={digest}")
            print("actual_collection_performed=false")
            print("semantic_labels_created=false")
            print("gate_5g_credit_claimed=false")
            print("gate_5_closed=false")
            return 0

        if args.config is None:
            raise ResearchCollectionV4Error("--preflight and --execute require --config")
        config_path = args.config
        if not config_path.is_absolute():
            config_path = paths.root / config_path
        loaded = load_research_collection_v4(config_path, repo_root=paths.root)
        if args.preflight:
            report_path, report_hash = write_preflight_report_v4(
                loaded,
                repo_root=paths.root,
            )
            print(f"execution_ready={str(loaded.preflight.execution_ready).lower()}")
            print(f"plan_id={loaded.plan.plan_id}")
            print(f"tranche_id={loaded.plan.tranche_id}")
            print(f"pool_dialect={loaded.plan.pool_dialect}")
            print(f"overlap_schema={loaded.plan.overlap_schema}")
            print(f"problems={loaded.plan.problem_count}")
            print(f"families={loaded.plan.family_count}")
            print(f"seed_count_by_family={loaded.plan.seed_count_by_family}")
            print(f"planned_candidates={loaded.plan.expected_candidate_count}")
            print(f"preflight_report={report_path}")
            print(f"preflight_sha256={report_hash}")
            print("gpu_model_execution_performed=false")
            print("semantic_labels_created=false")
            print("gate_5g_credit_claimed=false")
            print("gate_5_closed=false")
            return 0

        run = execute_research_collection_v4(
            loaded,
            repo_root=paths.root,
            executor=LocalHFResearchExecutor(),
        )
    except (ResearchCollectionV4Error, ValueError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"plan_id={loaded.plan.plan_id}")
    print(f"tranche_id={loaded.plan.tranche_id}")
    print(f"output_directory={run.output_directory}")
    print(f"manifest={run.manifest_path}")
    print(f"terminal_candidates={run.manifest.terminal_candidate_count}")
    print(f"status_counts={run.manifest.status_counts}")
    print("actual_collection_performed=true")
    print("parser_executed=false")
    print("semantic_labels_created=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
