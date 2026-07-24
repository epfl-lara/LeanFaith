#!/usr/bin/env python3
"""Seal replay certificates and a mixed post-exhaustion Gate-5G lineage."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.post_exhaustion_gate5g_lineage_v1 import (
    PostExhaustionGate5GLineageError,
    build_post_exhaustion_gate5g_lineage_v1,
    publish_post_exhaustion_gate5g_lineage_spec_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly replay an already frozen production post-exhaustion "
            "frame, then atomically seal its Gate-5G replay certificates and "
            "mixed original/extension lineage. This command executes no "
            "models, Lean, providers, labels, or gates."
        )
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--prepare-spec",
        action="store_true",
        help=(
            "Strictly replay --frame-decision and publish its sole canonical, "
            "content-addressed production lineage spec. This mode accepts no "
            "output override."
        ),
    )
    parser.add_argument("--frame-decision", type=Path)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()

    def resolve(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        if args.prepare_spec:
            if args.frame_decision is None:
                parser.error("--prepare-spec requires --frame-decision")
            if args.spec is not None or args.output_root is not None:
                parser.error("--prepare-spec forbids --spec and --output-root")
            spec_run = publish_post_exhaustion_gate5g_lineage_spec_v1(
                repo_root=paths.root,
                frame_decision_path=resolve(args.frame_decision),
            )
            print(f"spec_id={spec_run.spec.spec_id}")
            print(f"spec_path={spec_run.spec_path}")
            print(f"spec_sha256={spec_run.spec_binding.sha256}")
            print("lineage_written=false")
            print("model_execution_performed=false")
            print("lean_execution_performed=false")
            print("semantic_labels_inspected=false")
            print("semantic_labels_created=false")
            print("supervision_eligible=false")
            print("gate_5g_credit_claimed=false")
            print("gate_5g_closed=false")
            print("gate_5_closed=false")
            return 0
        if args.spec is None:
            parser.error("lineage build requires --spec")
        if args.frame_decision is not None:
            parser.error("--frame-decision is valid only with --prepare-spec")
        run = build_post_exhaustion_gate5g_lineage_v1(
            repo_root=paths.root,
            spec_path=resolve(args.spec),
            output_root=(resolve(args.output_root) if args.output_root is not None else None),
        )
    except (OSError, ValueError, PostExhaustionGate5GLineageError) as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"spec_id={run.spec.spec_id}")
    print(f"lineage_id={run.lineage.manifest_id}")
    print(f"lineage_path={run.lineage_path}")
    print(f"tranche_count={len(run.lineage.tranches)}")
    print(f"collection_replay_count={len(run.collection_replay_paths)}")
    print(f"postprocess_replay_count={len(run.postprocess_replay_paths)}")
    print("model_execution_performed=false")
    print("lean_execution_performed=false")
    print("semantic_labels_inspected=false")
    print("semantic_labels_created=false")
    print("supervision_eligible=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5g_closed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
