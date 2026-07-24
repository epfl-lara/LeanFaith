#!/usr/bin/env python3
"""Freeze LF-021 v3 population, seed, or randomized prevalence frame."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.frame_freeze_v3 import (
    FrameFreezeV3Error,
    archive_sampling_seed_v3,
    freeze_eligible_population_v3,
    freeze_frame_v3,
    verify_frame_freeze_v3,
)

_DEFAULT_POLICY = Path("configs/generation/lf021_frame_freeze_v3.yaml")
_DEFAULT_OUTPUT = Path("reports/generation/lf021_frame_freeze_v3")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prospectively freeze the problem-aware eligible population, its "
            "single 256-bit seed, or the randomized v3 prevalence frame."
        )
    )
    parser.add_argument(
        "action",
        choices=("freeze-population", "archive-seed", "freeze-frame", "verify-frame"),
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--policy", type=Path, default=_DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--v2-decision", type=Path)
    parser.add_argument("--frame-decision", type=Path)
    parser.add_argument("--population-manifest", type=Path)
    parser.add_argument("--seed-provenance", type=Path)
    parser.add_argument(
        "--test-replay-seed-file",
        type=Path,
        help=("Explicit deterministic test/replay seed. Never accepted for a production frame."),
    )
    parser.add_argument(
        "--test-replay-only",
        action="store_true",
        help=(
            "Permit a caller-supplied deterministic test seed and mark every "
            "derived artifact ineligible for production Gate finalization."
        ),
    )
    parser.add_argument("--timestamp", default=None)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    policy = _resolve(paths.root, args.policy)
    output = _resolve(paths.root, args.output)
    timestamp = args.timestamp or _now()
    try:
        if args.action == "freeze-population":
            if args.v2_decision is None:
                parser.error("freeze-population requires --v2-decision")
            population_run = freeze_eligible_population_v3(
                repo_root=paths.root,
                policy_path=policy,
                v2_decision_path=_resolve(paths.root, args.v2_decision),
                output_root=output,
                frozen_at=timestamp,
            )
            print(f"population_id={population_run.manifest.population_id}")
            print(f"population_size={population_run.manifest.population_item_count}")
            print(f"population_members={population_run.manifest.population_member_count}")
            print(f"population_manifest={population_run.manifest_path}")
            print(f"population_artifact={population_run.population_path}")
        elif args.action == "archive-seed":
            if args.population_manifest is None:
                parser.error("archive-seed requires --population-manifest")
            if args.test_replay_seed_file is not None and not args.test_replay_only:
                parser.error("--test-replay-seed-file requires --test-replay-only")
            if args.test_replay_only and args.test_replay_seed_file is None:
                parser.error("--test-replay-only requires --test-replay-seed-file for archive-seed")
            seed_bytes = (
                _resolve(paths.root, args.test_replay_seed_file).read_bytes()
                if args.test_replay_seed_file is not None
                else None
            )
            seed_run = archive_sampling_seed_v3(
                repo_root=paths.root,
                population_manifest_path=_resolve(paths.root, args.population_manifest),
                output_root=output,
                generated_at=timestamp,
                seed_bytes=seed_bytes,
                test_replay_only=args.test_replay_only,
            )
            print(f"sampling_seed_sha256={seed_run.provenance.sampling_seed_sha256}")
            print(f"sampling_seed_provenance={seed_run.provenance_path}")
            print(f"sampling_seed_artifact={seed_run.seed_path}")
            print(f"population_seed_lock={seed_run.lock_path}")
            print(f"test_replay_only={str(seed_run.provenance.test_replay_only).lower()}")
        elif args.action == "freeze-frame":
            if (
                args.v2_decision is None
                or args.population_manifest is None
                or args.seed_provenance is None
            ):
                parser.error(
                    "freeze-frame requires --v2-decision, "
                    "--population-manifest, and --seed-provenance"
                )
            frame_run = freeze_frame_v3(
                repo_root=paths.root,
                policy_path=policy,
                v2_decision_path=_resolve(paths.root, args.v2_decision),
                population_manifest_path=_resolve(
                    paths.root,
                    args.population_manifest,
                ),
                seed_provenance_path=_resolve(paths.root, args.seed_provenance),
                output_root=output,
                allow_test_replay=args.test_replay_only,
            )
            print(f"decision_id={frame_run.decision.decision_id}")
            print(f"frame_id={frame_run.decision.frame.frame_id}")
            print(f"frame_size={frame_run.decision.frame.item_count}")
            print(f"frame_artifact={frame_run.frame_path}")
            print(f"decision_artifact={frame_run.decision_path}")
            print(f"test_replay_only={str(frame_run.decision.test_replay_only).lower()}")
        else:
            if args.frame_decision is None:
                parser.error("verify-frame requires --frame-decision")
            verified = verify_frame_freeze_v3(
                repo_root=paths.root,
                policy_path=policy,
                decision_path=_resolve(paths.root, args.frame_decision),
            )
            print(f"decision_id={verified.decision.decision_id}")
            print(f"frame_id={verified.decision.frame.frame_id}")
            print(f"frame_size={len(verified.frame_items)}")
            print(f"frame_artifact={verified.frame_path}")
            print(f"test_replay_only={str(verified.decision.test_replay_only).lower()}")
    except (OSError, ValueError, FrameFreezeV3Error) as exc:
        print(f"FAILED: {exc}")
        return 1

    print("semantic_labels_inspected=false")
    print("semantic_labels_created=false")
    print("supervision_eligible=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
