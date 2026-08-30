"""CLI for the complete one-source ReForm-8B smoke."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from leanfaith.sft2b.reform_pipeline import run_reform_smoke


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--existing-config",
        type=Path,
        default=Path("configs/sft2b/existing_smoke_v1.json"),
    )
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument(
        "--model-config",
        type=Path,
        default=Path("configs/sft2b/reform_8b_theorem_smoke_v2.json"),
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    result = run_reform_smoke(
        repo_root,
        existing_config_path=repo_root / args.existing_config,
        source_config_path=repo_root / args.source_config,
        model_config_path=repo_root / args.model_config,
    )
    print(
        json.dumps(
            {
                "run_id": result.manifest.run_id,
                "root": str(result.output_root),
                "generation_root": str(result.generation_root),
                "counts": result.manifest.counts,
                "lean_requests": result.manifest.lean_request_count,
                "judge_calls": result.manifest.judge_call_count,
                "restart_lean_requests": result.manifest.restart_lean_request_count,
                "restart_judge_calls": result.manifest.restart_judge_call_count,
                "resumed_existing_manifest": result.resumed_existing_manifest,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
