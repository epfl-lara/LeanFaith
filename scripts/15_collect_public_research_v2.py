#!/usr/bin/env python3
"""Preflight or execute a frozen scalable LF-021 public collection."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.research_collection_v2 import (
    LocalHFResearchExecutor,
    ResearchCollectionV2Error,
    execute_research_collection_v2,
    load_research_collection_v2,
    write_preflight_report_v2,
)

_DEFAULT_CONFIG = Path("configs/generation/local_research_collection_v2.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate an arbitrary-problem x three-family frozen research plan "
            "without a model, or explicitly execute/resume its local raw outputs."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--preflight",
        action="store_true",
        help="Replay every bound prerequisite and write only the model-free report.",
    )
    action.add_argument(
        "--execute",
        action="store_true",
        help="Execute/resume the exact local plan; never parses or labels outputs.",
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    config_path = args.config
    if not config_path.is_absolute():
        config_path = paths.root / config_path
    try:
        loaded = load_research_collection_v2(config_path, repo_root=paths.root)
        if args.preflight:
            report_path, report_hash = write_preflight_report_v2(
                loaded,
                repo_root=paths.root,
            )
            print(f"execution_ready={str(loaded.preflight.execution_ready).lower()}")
            print(f"plan_id={loaded.plan.plan_id}")
            print(f"problems={loaded.plan.problem_count}")
            print(f"families={loaded.plan.family_count}")
            print(f"seed_count_by_family={loaded.plan.seed_count_by_family}")
            print(f"planned_candidates={loaded.plan.expected_candidate_count}")
            print(f"preflight_report={report_path}")
            print(f"preflight_sha256={report_hash}")
            print("gpu_model_execution_performed=false")
            print("provider_requests_created=0")
            print("semantic_labels_created=false")
            print("gate_5g_credit_claimed=false")
            print("gate_5_closed=false")
            return 0

        run = execute_research_collection_v2(
            loaded,
            repo_root=paths.root,
            executor=LocalHFResearchExecutor(),
        )
    except (ResearchCollectionV2Error, ValueError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"plan_id={loaded.plan.plan_id}")
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
