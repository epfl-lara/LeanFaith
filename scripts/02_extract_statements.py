#!/usr/bin/env python3
"""Phase 2 extraction driver (PLAN.md §7.2, §12): extract proof-stripped
theorem statements from mathlib files and sft_classic valid rows.

Usage: uv run python scripts/02_extract_statements.py [mathlib_files] [sft_rows]
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.lean.extract_run import (
    extract_dataset_snippets,
    extract_repository_files,
    write_extraction_manifest,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.schemas import collect_code_state, new_run_id
from leanfaith.sources.mathlib import build_inventory

MATHLIB_CHECKOUT = Path("/storage/milikic/leanfaith/mathlib4")
MATHLIB_REV = "d568c8c09630de097a046763c17b9ea99f95f950"
SFT_REV = "0bf9f424309f668c2c2dd214aef6ec5d1d5c042f"
CTX_FP = "e" * 64  # project-level REPL context (mathlib env); §8.11 finer contexts later
CTX = f"ctx:{CTX_FP}"


def main() -> int:
    n_files = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    n_rows = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    paths = RepoPaths.discover(Path.cwd())
    out_dir = paths.data / "extracted"
    run_id = new_run_id(datetime.datetime.now(tz=datetime.UTC))
    code = collect_code_state(paths.root)

    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=MATHLIB_CHECKOUT,
            context_fingerprint=CTX_FP,
            environment_schema_version=1,
            raw_response_dir=paths.data / "raw" / "lean_extract",
        )
    )
    try:
        inventory = build_inventory(
            MATHLIB_CHECKOUT,
            source="mathlib",
            revision=MATHLIB_REV,
            root_module="Mathlib",
            globs=("Mathlib/**/*.lean",),
            limit=n_files,
        )
        rel_paths = [f.relative_path for f in inventory.files]
        mstats = extract_repository_files(
            backend,
            MATHLIB_CHECKOUT,
            rel_paths,
            source="mathlib",
            source_revision=MATHLIB_REV,
            context_id=CTX,
            out_dir=out_dir,
        )
        write_extraction_manifest(
            mstats,
            source="mathlib",
            source_revision=MATHLIB_REV,
            run_id=run_id,
            code=code,
            out_dir=out_dir,
            root=paths.root,
        )
        print("[mathlib]", json.dumps(mstats.as_dict()))

        sample = paths.data / "raw" / "sources" / "sft_classic" / "probe_sample.jsonl"
        rows = [json.loads(line) for line in sample.read_text().splitlines()]
        valid_rows = [r for r in rows if r.get("valid")][:n_rows]
        sstats = extract_dataset_snippets(
            backend,
            valid_rows,
            source="sft_classic",
            source_revision=SFT_REV,
            context_id=CTX,
            out_dir=out_dir,
        )
        write_extraction_manifest(
            sstats,
            source="sft_classic",
            source_revision=SFT_REV,
            run_id=run_id,
            code=code,
            out_dir=out_dir,
            root=paths.root,
        )
        print("[sft_classic]", json.dumps(sstats.as_dict()))
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
