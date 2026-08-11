#!/usr/bin/env python3
"""Build a provisional-only LF-022 two-family-judging candidate inventory."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateError,
    build_lf022_supervision_candidate_inventory,
    write_lf022_supervision_candidate_inventory,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay a completed public LF-022 Codex audit and inventory the exact "
            "four still-missing two-family swapped-order judge calls. Creates no labels."
        )
    )
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--spec-sha256", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        records, manifest = build_lf022_supervision_candidate_inventory(
            repo_root=args.repo_root,
            spec_path=args.spec,
            expected_spec_sha256=args.spec_sha256,
        )
        records_path, sample_path, summary_path, manifest_path = (
            write_lf022_supervision_candidate_inventory(
                output_dir=args.output_dir,
                records=records,
                manifest=manifest,
            )
        )
    except (OSError, ValueError, LF022SupervisionCandidateError) as exc:
        print(f"FAILED: {exc}")
        return 2
    print(f"inventory_id={manifest.inventory_id}")
    print(f"records={records_path}")
    print(f"public_sample={sample_path}")
    print(f"summary={summary_path}")
    print(f"manifest={manifest_path}")
    print(f"record_count={manifest.record_count}")
    print(f"dispatch_eligible_count={manifest.dispatch_eligible_count}")
    print(f"required_future_judge_call_count={manifest.required_future_judge_call_count}")
    print("semantic_labels_created=false")
    print("silver_records_created=false")
    print("training_eligible=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
