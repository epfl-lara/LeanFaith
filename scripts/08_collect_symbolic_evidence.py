#!/usr/bin/env python3
"""Compatibility wrapper for ``leanfaith collect-evidence`` (LF-020)."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.cli.collect_evidence import (
    EvidenceCollectionInputError,
    run_collect_evidence,
)
from leanfaith.config.paths import RepoPaths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect LF-020 symbolic evidence without resolving labels."
    )
    parser.add_argument("--contexts", action="append", required=True, type=Path)
    parser.add_argument("--theorems", action="append", required=True, type=Path)
    parser.add_argument("--representations", action="append", required=True, type=Path)
    parser.add_argument("--pairs", required=True, type=Path)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--upstream-evidence", action="append", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--alignments", type=Path)
    parser.add_argument(
        "--artifact-class",
        choices=("auto", "production", "smoke", "diagnostic"),
        default="auto",
    )
    parser.add_argument("--memory-hard-limit-mb", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    try:
        result = run_collect_evidence(
            paths=paths,
            context_paths=args.contexts,
            theorem_paths=args.theorems,
            representation_paths=args.representations,
            pair_path=args.pairs,
            project_dir=args.project_dir,
            upstream_evidence_paths=args.upstream_evidence or (),
            out_dir=args.out_dir,
            cache_dir=args.cache_dir,
            artifact_dir=args.artifact_dir,
            alignment_path=args.alignments,
            artifact_class=args.artifact_class,
            memory_hard_limit_mb=args.memory_hard_limit_mb,
            limit=args.limit,
        )
    except EvidenceCollectionInputError as exc:
        print(f"FAILED: {exc}")
        return 2
    print(f"output={result.output_dir}")
    print(f"manifest={result.output_manifest_path}")
    print(f"run_manifest={result.run_manifest_path}")
    print(f"artifact_class={result.artifact_class.value}")
    print(f"pairs={result.pair_count}")
    print(f"evidence={result.evidence_count}")
    print(f"failures={result.failure_count}")
    print(f"cache_hits={result.cache_hits}")
    print(f"cache_misses={result.cache_misses}")
    print("resolved_labels_created=0")
    return 1 if result.failure_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
