"""A100/H100-only entry point for the prepared one-source ReForm-32B generation."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from leanfaith.config.hashing import hash_file
from leanfaith.sft2b.formalizer import run_formalizer_generation
from leanfaith.sft2b.numina_source import load_numina_source
from leanfaith.sft2b.pins import verify_runtime_pins
from leanfaith.sft2b.pipeline import run_existing_smoke
from leanfaith.sft2b.reform_32b import load_reform_32b_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--existing-config",
        type=Path,
        default=Path("configs/sft2b/existing_smoke_v1.json"),
    )
    parser.add_argument(
        "--source-config",
        type=Path,
        default=Path("configs/sft2b/numina_multiples_smoke_v1.json"),
    )
    parser.add_argument(
        "--placement-config",
        type=Path,
        default=Path("configs/sft2b/reform_32b_placement_v1.json"),
    )
    parser.add_argument("--snapshot-path", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    placement_path = repo_root / args.placement_config
    placement = json.loads(placement_path.read_text(encoding="utf-8"))
    source_config_path = repo_root / args.source_config
    if hash_file(source_config_path) != placement["source_config_sha256"]:
        raise RuntimeError("ReForm-32B source config hash mismatch")
    existing = run_existing_smoke(repo_root, repo_root / args.existing_config)
    if existing.manifest.counts.get("core") != 1:
        raise RuntimeError("the accepted existing-candidate gate has not passed")
    helper = repo_root / "src/leanfaith/sft2b/lean_helper.lean"
    pins = verify_runtime_pins(repo_root, helper_path=helper)
    source, _ = load_numina_source(
        repo_root,
        config_path=source_config_path,
        helper_path=helper,
        pins=pins,
    )
    config, minimum_vram = load_reform_32b_config(
        repo_root,
        placement_path=placement_path,
        snapshot_path=args.snapshot_path,
    )
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("ReForm-32B placement has no CUDA GPU")
    observed_vram = int(torch.cuda.get_device_properties(0).total_memory)
    if observed_vram < minimum_vram:
        raise RuntimeError(
            f"ReForm-32B requires at least {minimum_vram} bytes VRAM; observed {observed_vram}"
        )
    result = run_formalizer_generation(repo_root, config=config, source=source)
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "root": str(result.root),
                "attempts": len(result.attempts),
                "candidates": len(result.candidates),
                "model_calls": result.model_calls,
                "model_loaded": result.model_loaded,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
