#!/usr/bin/env python3
"""Evaluate the frozen LF-021 compilation-only tranche expansion policy."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.tranche_expansion import (
    TrancheExpansionError,
    run_tranche_expansion,
)

_DEFAULT_POLICY = Path("configs/generation/lf021_tranche_expansion_v1.yaml")
_DEFAULT_OUTPUT = Path("reports/generation/lf021_tranche_expansion_v1")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Choose the next preregistered LF-021 tranche or freeze an unlabeled "
            "prevalence frame from immutable operational postprocess manifests."
        )
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--policy", type=Path, default=_DEFAULT_POLICY)
    parser.add_argument(
        "--postprocess-manifest",
        type=Path,
        action="append",
        default=[],
        help=(
            "operational postprocess manifest in exact tranche order; repeat for "
            "a complete prefix of the frozen sequence"
        ),
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    policy = args.policy if args.policy.is_absolute() else paths.root / args.policy
    output = args.output if args.output.is_absolute() else paths.root / args.output
    manifests = tuple(
        path if path.is_absolute() else paths.root / path for path in args.postprocess_manifest
    )
    try:
        run = run_tranche_expansion(
            repo_root=paths.root,
            policy_path=policy,
            observed_manifests=manifests,
            output_root=output,
        )
    except (OSError, ValueError, TrancheExpansionError) as exc:
        print(f"FAILED: {exc}")
        return 1

    decision = run.decision
    print(f"decision_id={decision.decision_id}")
    print(f"action={decision.action.value}")
    print(f"observed_tranches={decision.counts.observed_tranche_count}")
    print(f"next_tranche={decision.next_tranche.tranche_id if decision.next_tranche else None}")
    print(f"unique_compiling={decision.counts.unique_compiling_count}")
    print(f"coverage_deficits={list(decision.coverage_deficits)}")
    print(f"frame_id={decision.frame.frame_id if decision.frame else None}")
    print(f"frame_size={decision.frame.item_count if decision.frame else 0}")
    print(f"reduced_data_ablation={str(decision.reduced_data_ablation).lower()}")
    print(f"decision_artifact={run.decision_path}")
    print(f"report_artifact={run.report_path}")
    print(f"frame_artifact={run.frame_path}")
    print("model_execution_performed=false")
    print("semantic_labels_inspected=false")
    print("semantic_labels_created=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
