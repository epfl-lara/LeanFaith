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
            "Inventory mechanically Lean-valid public LF-022 pairs and the exact four "
            "still-missing two-family swapped-order judge calls. A schema-v3 spec may "
            "optionally bind a complete Codex diagnostic; it never contributes a vote."
        )
    )
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--spec-sha256", required=True)
    parser.add_argument("--repo-root", "--root", dest="repo_root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--require-codex-diagnostic",
        action="store_true",
        help=(
            "Fail unless the spec binds a complete replay-verified Codex diagnostic; "
            "this remains an audit-only assertion."
        ),
    )
    args = parser.parse_args()
    try:
        records, manifest = build_lf022_supervision_candidate_inventory(
            repo_root=args.repo_root,
            spec_path=args.spec,
            expected_spec_sha256=args.spec_sha256,
        )
        diagnostic_status = manifest.codex_diagnostic_status or "complete"
        diagnostic_count = (
            manifest.codex_diagnostic_record_count
            if manifest.codex_diagnostic_record_count is not None
            else manifest.record_count
        )
        if args.require_codex_diagnostic and diagnostic_status != "complete":
            raise LF022SupervisionCandidateError(
                "--require-codex-diagnostic was set but the spec binds no Codex audit"
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
    print(f"codex_diagnostic_status={diagnostic_status}")
    print(f"codex_diagnostic_record_count={diagnostic_count}")
    print("codex_weak_judge_votes=0")
    print("semantic_labels_created=false")
    print("silver_records_created=false")
    print("training_eligible=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
