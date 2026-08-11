#!/usr/bin/env python3
"""Build a deterministic, audit-only LF-022 inventory snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.generation.lf022_inventory_snapshot import (
    LF022InventorySnapshotError,
    build_lf022_inventory_snapshot,
    write_lf022_inventory_snapshot,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify exact LF-022 generation/check/audit bindings and report gross, "
            "deduplicated, validity, Codex-audit, and overlap counts. Creates no labels."
        )
    )
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--spec-sha256", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = build_lf022_inventory_snapshot(
            repo_root=args.repo_root,
            spec_path=args.spec,
            expected_spec_sha256=args.spec_sha256,
        )
        output_hash = (
            write_lf022_inventory_snapshot(report, output_path=args.output.resolve())
            if args.output is not None
            else None
        )
    except (OSError, ValueError, LF022InventorySnapshotError) as exc:
        print(f"FAILED: {exc}")
        return 2
    if args.output is None:
        print(canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8"))
    else:
        print(f"snapshot={args.output.resolve()}")
        print(f"sha256={output_hash}")
    print(f"snapshot_status={report.snapshot_status}")
    print(f"gross_variant_count={report.overall.gross_variant_count}")
    print(f"unique_content_count={report.overall.unique_content_count}")
    print(f"unique_pair_count={report.overall.unique_pair_count}")
    print(f"lean_valid_count={report.overall.lean_valid_count}")
    print(f"codex_audit_completed_count={report.overall.codex_audit_completed_count}")
    print("semantic_labels_created=false")
    print("training_eligible=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
