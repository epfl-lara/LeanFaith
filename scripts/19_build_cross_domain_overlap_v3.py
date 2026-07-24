#!/usr/bin/env python3
"""Materialize overlap records for the exact LF-021 cross-domain pool."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.research_overlap_materialize_v3 import (
    ResearchOverlapMaterializationError,
    materialize_research_overlap_v3,
)

_QUALIFICATION_CONFIG = Path("configs/generation/local_research_collection_v1.yaml")
_POOL_RECORDS = Path(
    "data/parsed/real_outputs/cross_domain_docstrings_operational_v1/problem_pool_records.jsonl"
)
_POOL_MANIFEST = Path(
    "data/parsed/real_outputs/cross_domain_docstrings_operational_v1/problem_pool_manifest.json"
)
_OUTPUT = Path("reports/generation/overlap_v3/cross_domain_docstrings_operational_v1")


def _under_root(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build or exactly replay three overlap-v3 records. "
            "Performs no model call, parsing, labeling, or Gate closure."
        )
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--qualification-config",
        type=Path,
        default=_QUALIFICATION_CONFIG,
    )
    parser.add_argument("--pool-records", type=Path, default=_POOL_RECORDS)
    parser.add_argument("--pool-manifest", type=Path, default=_POOL_MANIFEST)
    parser.add_argument("--output", type=Path, default=_OUTPUT)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    try:
        materialized = materialize_research_overlap_v3(
            repo_root=paths.root,
            qualification_collection_config=_under_root(
                paths.root,
                args.qualification_config,
            ),
            problem_pool_records=_under_root(paths.root, args.pool_records),
            problem_pool_manifest=_under_root(paths.root, args.pool_manifest),
            output_directory=_under_root(paths.root, args.output),
        )
    except (ResearchOverlapMaterializationError, ValueError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"bundle_id={materialized.manifest.bundle_id}")
    print(f"problem_count={materialized.manifest.problem_count}")
    print(f"family_count={materialized.manifest.family_count}")
    print(f"manifest={materialized.manifest_path}")
    print(f"family_artifacts={materialized.manifest.family_artifacts}")
    print("model_execution_performed=false")
    print("semantic_labels_created=false")
    print("gate_5g_credit_claimed=false")
    print("gate_5_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
