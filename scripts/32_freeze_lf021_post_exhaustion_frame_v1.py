#!/usr/bin/env python3
"""Materialize or verify the separately versioned LF-021 extended frame."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.post_exhaustion_frame_v1 import (
    PostExhaustionFrameError,
    archive_extended_sampling_seed_v1,
    freeze_extended_eligible_population_v1,
    freeze_extended_frame_v1,
    verify_extended_frame_freeze_v1,
)

_DEFAULT_POLICY = Path("configs/generation/lf021_post_exhaustion_frame_v1.yaml")
_DEFAULT_OUTPUT = Path("reports/generation/lf021_post_exhaustion_frame_v1")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _authorizations(root: Path, values: list[Path]) -> tuple[Path, ...]:
    return tuple(_resolve(root, item) for item in values)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze a truthful post-exhaustion population, its one 256-bit "
            "seed, or its separately versioned prevalence frame."
        )
    )
    parser.add_argument(
        "action",
        choices=(
            "freeze-population",
            "archive-seed",
            "freeze-frame",
            "verify-frame",
        ),
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--policy", type=Path, default=_DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--extension-decision", type=Path)
    parser.add_argument(
        "--collection-authorization",
        type=Path,
        action="append",
        default=[],
    )
    parser.add_argument("--frame-decision", type=Path)
    parser.add_argument("--population-manifest", type=Path)
    parser.add_argument("--seed-provenance", type=Path)
    parser.add_argument(
        "--test-replay-seed-file",
        type=Path,
        help="Deterministic test-only seed; never production Gate eligible.",
    )
    parser.add_argument("--test-replay-only", action="store_true")
    parser.add_argument("--timestamp")
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    root = paths.root
    policy = _resolve(root, args.policy)
    output = _resolve(root, args.output)
    timestamp = args.timestamp or _now()
    decision = (
        _resolve(root, args.extension_decision) if args.extension_decision is not None else None
    )
    authorizations = _authorizations(root, args.collection_authorization)
    try:
        if args.action == "freeze-population":
            if decision is None or not authorizations:
                parser.error(
                    "freeze-population requires --extension-decision and "
                    "one or more --collection-authorization"
                )
            population_run = freeze_extended_eligible_population_v1(
                repo_root=root,
                policy_path=policy,
                extension_decision_path=decision,
                collection_authorization_paths=authorizations,
                output_root=output,
                frozen_at=timestamp,
            )
            print(f"population_id={population_run.manifest.population_id}")
            print(f"population_size={population_run.manifest.population_item_count}")
            print(f"population_manifest={population_run.manifest_path}")
            print(f"population_artifact={population_run.population_path}")
        elif args.action == "archive-seed":
            if args.population_manifest is None:
                parser.error("archive-seed requires --population-manifest")
            if args.test_replay_seed_file is not None and not args.test_replay_only:
                parser.error("--test-replay-seed-file requires --test-replay-only")
            if args.test_replay_only and args.test_replay_seed_file is None:
                parser.error("--test-replay-only requires --test-replay-seed-file")
            seed_bytes = (
                _resolve(root, args.test_replay_seed_file).read_bytes()
                if args.test_replay_seed_file is not None
                else None
            )
            seed_run = archive_extended_sampling_seed_v1(
                repo_root=root,
                policy_path=policy,
                population_manifest_path=_resolve(
                    root,
                    args.population_manifest,
                ),
                output_root=output,
                generated_at=timestamp,
                seed_bytes=seed_bytes,
                test_replay_only=args.test_replay_only,
            )
            print(f"sampling_seed_sha256={seed_run.provenance.sampling_seed_sha256}")
            print(f"sampling_seed_provenance={seed_run.provenance_path}")
            print(f"population_seed_lock={seed_run.lock_path}")
        elif args.action == "freeze-frame":
            if (
                decision is None
                or not authorizations
                or args.population_manifest is None
                or args.seed_provenance is None
            ):
                parser.error(
                    "freeze-frame requires --extension-decision, one or more "
                    "--collection-authorization, --population-manifest, and "
                    "--seed-provenance"
                )
            frame_run = freeze_extended_frame_v1(
                repo_root=root,
                policy_path=policy,
                extension_decision_path=decision,
                collection_authorization_paths=authorizations,
                population_manifest_path=_resolve(
                    root,
                    args.population_manifest,
                ),
                seed_provenance_path=_resolve(root, args.seed_provenance),
                output_root=output,
                allow_test_replay=args.test_replay_only,
            )
            print(f"decision_id={frame_run.decision.decision_id}")
            print(f"frame_id={frame_run.decision.frame.frame_id}")
            print(f"frame_size={len(frame_run.items)}")
            print(f"frame_artifact={frame_run.frame_path}")
        else:
            if args.frame_decision is None:
                parser.error("verify-frame requires --frame-decision")
            verified = verify_extended_frame_freeze_v1(
                repo_root=root,
                policy_path=policy,
                decision_path=_resolve(root, args.frame_decision),
            )
            print(f"decision_id={verified.decision.decision_id}")
            print(f"frame_id={verified.decision.frame.frame_id}")
            print(f"frame_size={len(verified.frame_items)}")
            print(f"test_replay_only={str(verified.decision.test_replay_only).lower()}")
    except (OSError, ValueError, PostExhaustionFrameError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print("semantic_labels_created=false")
    print("supervision_eligible=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
