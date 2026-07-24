#!/usr/bin/env python3
"""Plan or verify the label-blind LF-021 post-exhaustion extension."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.post_exhaustion_extension import (
    PostExhaustionExtensionError,
    load_post_exhaustion_extension_policy,
    verify_extended_stop_for_frame_v3,
    write_post_exhaustion_extension_decision,
)

_DEFAULT_POLICY = Path("configs/generation/lf021_post_exhaustion_extension_v1.yaml")
_DEFAULT_OUTPUT = Path("reports/generation/lf021_post_exhaustion_extension_v1")


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Select the next preregistered LF-021 extension tranche, or "
            "strictly replay a preferred stop into v3 population-row objects. "
            "This command never creates a population, seed, or frame."
        )
    )
    parser.add_argument("action", choices=("plan", "verify-stop"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--policy", type=Path, default=_DEFAULT_POLICY)
    parser.add_argument("--activation-v2-decision", type=Path)
    parser.add_argument("--extension-decision", type=Path)
    parser.add_argument(
        "--extension-postprocess-manifest",
        type=Path,
        action="append",
        default=[],
        help=(
            "immutable extension postprocess manifest in exact algebra_s6, "
            "cross_domain_s6, algebra_s7, cross_domain_s7 prefix order"
        ),
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    policy_path = _resolve(paths.root, args.policy)
    try:
        if args.action == "plan":
            if args.activation_v2_decision is None:
                parser.error("plan requires --activation-v2-decision")
            loaded_policy = load_post_exhaustion_extension_policy(policy_path)
            run = write_post_exhaustion_extension_decision(
                repo_root=paths.root,
                loaded_policy=loaded_policy,
                activation_v2_decision_path=_resolve(
                    paths.root,
                    args.activation_v2_decision,
                ),
                extension_observed_manifests=tuple(
                    _resolve(paths.root, path) for path in args.extension_postprocess_manifest
                ),
                output_root=_resolve(paths.root, args.output),
            )
            print(f"decision_id={run.decision.decision_id}")
            print(f"action={run.decision.action.value}")
            print(
                "next_tranche="
                + (
                    run.decision.next_tranche.tranche_id
                    if run.decision.next_tranche is not None
                    else "none"
                )
            )
            print(f"problem_aware_unique_compiling={run.decision.counts.unique_compiling_count}")
            print(f"coverage_deficits={list(run.decision.coverage_deficits)}")
            print(f"decision_artifact={run.decision_path}")
            print(f"report_artifact={run.report_path}")
        else:
            if args.extension_decision is None:
                parser.error("verify-stop requires --extension-decision")
            verified = verify_extended_stop_for_frame_v3(
                repo_root=paths.root,
                policy_path=policy_path,
                decision_path=_resolve(paths.root, args.extension_decision),
            )
            projection = verified.handoff_projection
            print(f"decision_id={verified.decision.decision_id}")
            print(f"projection_id={projection.projection_id}")
            print(f"population_size={projection.population_item_count}")
            print(f"population_members={projection.population_member_count}")
            print(f"population_items_sha256={projection.population_items_sha256}")
            print("direct_frozen_v3_decision_compatible=false")
            print("required_consumer=separately_reviewed_extended_population_materializer_v1")
    except (OSError, ValueError, PostExhaustionExtensionError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print("frame_creation_performed=false")
    print("sampling_seed_obtained=false")
    print("model_execution_performed=false")
    print("semantic_labels_inspected=false")
    print("semantic_labels_created=false")
    print("supervision_eligible=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
