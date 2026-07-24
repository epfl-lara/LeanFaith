#!/usr/bin/env python3
"""Offline exact replay and Lean operational audit for the RCP Kimi v2 run."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.rcp_qualification_verify_v2 import (
    RCPQualificationVerifyV2Error,
    verify_rcp_qualification_v2,
)

_DEFAULT_CONFIG = Path("configs/generation/rcp_kimi_qualification_v2.yaml")
_DEFAULT_OUTPUT = Path(
    "data/raw/real_outputs/rcp_kimi_qualification_v2/v2/"
    "7f5b578dd21cf696dd1222dcc176927383fc44e3b6fb10f7f96e2ff8c23dc497"
)
_DEFAULT_LEAN_RAW = Path(
    "reports/generation/lf021_rcp_kimi_qualification_v2_lean_raw/"
    "f3ca7723f2d4433549c0226011105bbba26056532464e6573afee36b5b161b9e"
    ".d2da253d.json"
)
_DEFAULT_REPORT = Path(
    "reports/generation/lf021_rcp_kimi_qualification_v2_audit/"
    "7f5b578dd21cf696dd1222dcc176927383fc44e3b6fb10f7f96e2ff8c23dc497.json"
)


def _under_root(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the persisted RCP Kimi v2 qualification exactly, offline. "
            "This command performs zero provider and zero network requests."
        )
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--output-directory", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--lean-raw", type=Path, default=_DEFAULT_LEAN_RAW)
    parser.add_argument("--report", type=Path, default=_DEFAULT_REPORT)
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    credential = os.environ.get("RCP_API_KEY", "")
    try:
        run = verify_rcp_qualification_v2(
            repo_root=paths.root,
            config_path=_under_root(args.config, paths.root),
            output_directory=_under_root(args.output_directory, paths.root),
            lean_raw_path=_under_root(args.lean_raw, paths.root),
            report_path=_under_root(args.report, paths.root),
            credential=credential,
        )
    except (RCPQualificationVerifyV2Error, ValueError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"verification_id={run.report.verification_id}")
    print(f"verification_report={run.report_path}")
    print(f"verification_sha256={run.report_sha256}")
    print(f"terminal_status={run.report.terminal_status}")
    print(f"lean_operational_status={run.report.lean_operational_validation.operational_status}")
    print("provider_calls_performed=0")
    print("network_requests_performed=0")
    print("semantic_faithfulness_assessed=false")
    print("semantic_labels_created=false")
    print("gate_credit_claimed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
