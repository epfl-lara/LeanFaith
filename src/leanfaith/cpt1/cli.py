"""Command-line interface for the task-owned CPT1 builder."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import NoReturn

from leanfaith.cpt1.builder import build_live, load_config, validate_release, verify_live_smoke


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and validate LeanFaith CPT1")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build or resume an exact pinned release")
    build.add_argument("--config", type=Path, required=True)
    build.add_argument("--output-root", type=Path)
    build.add_argument("--limit-per-source", type=int)
    build.add_argument("--streaming", action="store_true")

    validate = subparsers.add_parser("validate", help="re-hash and inspect a release")
    validate.add_argument("--release-root", type=Path, required=True)

    smoke = subparsers.add_parser("verify-smoke", help="compare smoke rows to pinned inputs")
    smoke.add_argument("--config", type=Path, required=True)
    smoke.add_argument("--release-root", type=Path, required=True)
    return parser


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def main() -> None:
    args = _parser().parse_args()
    if args.command == "build":
        config = load_config(args.config)
        result = build_live(
            config,
            output_root=args.output_root,
            limit_per_source=args.limit_per_source,
            streaming=args.streaming,
        )
        print(
            json.dumps(
                {
                    "manifest": str(result.manifest_path),
                    "release_root": str(result.release_root),
                    "resumed_chunks": result.resumed_chunks,
                    "rows": result.rows,
                    "validation": str(result.validation_path),
                    "written_chunks": result.written_chunks,
                },
                sort_keys=True,
            )
        )
        return
    if args.command == "validate":
        print(json.dumps(validate_release(args.release_root), sort_keys=True))
        return
    if args.command == "verify-smoke":
        print(
            json.dumps(
                verify_live_smoke(load_config(args.config), args.release_root), sort_keys=True
            )
        )
        return
    _fail(f"unsupported command {args.command!r}")


if __name__ == "__main__":
    main()
