"""Command-line entry point for the bounded existing-301 SFT2B smoke."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from leanfaith.sft2b.pipeline import run_existing_smoke


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/sft2b/existing_smoke_v1.json"),
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run_existing_smoke(args.repo_root.resolve(), args.config.resolve())
    print(
        json.dumps(
            {
                "manifest": str(result.output_root / "manifest.json"),
                "run_id": result.manifest.run_id,
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
