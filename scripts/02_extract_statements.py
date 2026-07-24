#!/usr/bin/env python3
"""Compatibility wrapper for the canonical Revision-4.1 extraction pipeline.

Usage: ``uv run python scripts/02_extract_statements.py [mathlib_files] [sft_rows]``.
Gate-facing automation should call ``leanfaith extract`` directly; this script
delegates to the same implementation and never filters/reindexes source rows.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from leanfaith.cli.pipeline import default_mathlib_checkout, run_extract
from leanfaith.config.paths import RepoPaths


def main() -> int:
    n_files = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    n_rows = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    paths = RepoPaths.discover(Path.cwd())
    out_dir = paths.data / "extracted"
    project_dir = default_mathlib_checkout()

    mathlib_manifest, mathlib_stats = run_extract(
        paths=paths,
        source="mathlib",
        project_dir=project_dir,
        input_path=None,
        out_dir=out_dir,
        limit=n_files,
        split="train",
        row_offset=0,
    )
    print(f"[mathlib] manifest={mathlib_manifest} {json.dumps(mathlib_stats, sort_keys=True)}")

    sample = paths.data / "raw" / "sources" / "sft_classic" / "probe_sample.jsonl"
    sft_manifest, sft_stats = run_extract(
        paths=paths,
        source="sft_classic",
        project_dir=project_dir,
        input_path=sample,
        out_dir=out_dir,
        limit=n_rows,
        split="train",
        row_offset=0,
    )
    print(f"[sft_classic] manifest={sft_manifest} {json.dumps(sft_stats, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
