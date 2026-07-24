#!/usr/bin/env python3
"""Compatibility wrapper for the canonical Revision-4.1 representation CLI.

Gate-facing automation should call ``leanfaith represent`` directly. This
wrapper exists for the phase-to-script contract in PLAN.md and delegates to
the same bounded, per-theorem implementation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from leanfaith.cli.pipeline import default_mathlib_checkout, run_represent
from leanfaith.config.paths import RepoPaths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("input", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--project-dir", type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--chunk-size", type=int, default=20)
    parser.add_argument("--code-bundle", type=Path)
    parser.add_argument("--frozen-manifest", type=Path)
    parser.add_argument("--resume-work-dir", type=Path)
    args = parser.parse_args()

    paths = RepoPaths.discover(Path.cwd())
    manifest, counts = run_represent(
        paths=paths,
        source=args.source,
        theorem_jsonl=args.input,
        project_dir=args.project_dir or default_mathlib_checkout(),
        out_dir=args.out_dir or paths.data / "representations",
        limit=None,
        workers=args.workers,
        chunk_size=args.chunk_size,
        code_bundle_path=args.code_bundle,
        frozen_manifest_path=args.frozen_manifest,
        resume_work_dir=args.resume_work_dir,
    )
    print(f"manifest={manifest}")
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
