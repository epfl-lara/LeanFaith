"""Bounded four-slot ReForm-8B generation entry point."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from leanfaith.sft2b.formalizer import run_reform_8b_generation
from leanfaith.sft2b.new_source import load_new_source
from leanfaith.sft2b.numina_source import load_numina_source
from leanfaith.sft2b.pins import verify_runtime_pins
from leanfaith.sft2b.pipeline import run_existing_smoke


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--existing-config", type=Path, default=Path("configs/sft2b/existing_smoke_v1.json")
    )
    parser.add_argument(
        "--source-config", type=Path, default=Path("configs/sft2b/new_source_smoke_v1.json")
    )
    parser.add_argument(
        "--model-config", type=Path, default=Path("configs/sft2b/reform_8b_smoke_v1.json")
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    existing = run_existing_smoke(repo_root, repo_root / args.existing_config)
    if existing.manifest.counts.get("core") != 1:
        raise RuntimeError("the accepted existing-candidate gate has not passed")
    helper = repo_root / "src/leanfaith/sft2b/lean_helper.lean"
    pins = verify_runtime_pins(repo_root, helper_path=helper)
    source_config_path = repo_root / args.source_config
    source_config = json.loads(source_config_path.read_text(encoding="utf-8"))
    if source_config.get("schema_version") == "sft2b_numina_source_smoke_v1":
        source, _ = load_numina_source(
            repo_root,
            config_path=source_config_path,
            helper_path=helper,
            pins=pins,
        )
    else:
        source, _ = load_new_source(
            repo_root,
            config_path=source_config_path,
            helper_path=helper,
            pins=pins,
        )
    result = run_reform_8b_generation(
        repo_root,
        config_path=repo_root / args.model_config,
        source=source,
    )
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "root": str(result.root),
                "attempts": len(result.attempts),
                "extracted_candidates": len(result.candidates),
                "model_calls": result.model_calls,
                "model_loaded": result.model_loaded,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
