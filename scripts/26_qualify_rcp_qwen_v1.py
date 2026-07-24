#!/usr/bin/env python3
"""Execute once or offline-verify the hash-bound RCP Qwen qualification."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation import rcp_qualification_v1 as shared
from leanfaith.generation.rcp_qwen_qualification_v1 import (
    RCPQwenQualificationError,
    execute_one_qwen_qualification,
    load_completed_qwen_run,
    load_qwen_qualification,
    resolve_credentials,
    verify_qwen_qualification,
)

_DEFAULT_CONFIG = Path("configs/generation/rcp_qwen_qualification_v1.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute exactly one public reference-hidden Qwen3.6 theorem-generation "
            "request, or verify the immutable bundle offline. No bulk mode exists."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--execute-one", action="store_true")
    action.add_argument("--verify-only", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument(
        "--mathlib-project-dir",
        type=Path,
        help="required once to create the LeanInteract operational audit",
    )
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    config_path = args.config if args.config.is_absolute() else paths.root / args.config
    try:
        loaded = load_qwen_qualification(config_path, repo_root=paths.root)
        if args.execute_one:
            if (loaded.output_directory / "terminal.json").exists():
                run = load_completed_qwen_run(loaded, repo_root=paths.root)
                print("execution_resumed=true")
                print("catalog_requests_performed=0")
                print("chat_completion_requests_performed=0")
            else:
                credentials = resolve_credentials(loaded.loaded_config.config)
                run = execute_one_qwen_qualification(
                    loaded,
                    credentials=credentials,
                    repo_root=paths.root,
                    transport=shared.UrllibRCPTransport(),
                )
                print("execution_resumed=false")
                print("catalog_requests_performed=1")
                print("chat_completion_requests_performed=1")
            print(f"run_key={loaded.run_key}")
            print(f"output_directory={run.output_directory}")
            print(f"terminal_status={run.terminal.status.value}")
            print(f"manifest_id={run.manifest.manifest_id}")
            print("no_call_ablation_requests_performed=0")
            print("dedicated_capability_requests_performed=0")
            print("field_application_proven=false")
            print("semantic_labels_created=false")
            print("supervision_eligible=false")
            print("gate_credit_claimed=false")
            return 0 if run.terminal.status.value == "raw_collected" else 2

        credential = os.environ.get("RCP_API_KEY", "")
        verified = verify_qwen_qualification(
            loaded,
            repo_root=paths.root,
            credential=credential,
            mathlib_project_dir=args.mathlib_project_dir,
        )
    except (RCPQwenQualificationError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"run_key={loaded.run_key}")
    print(f"verification_id={verified.report.verification_id}")
    print(f"verification_report={verified.report_path}")
    print(f"verification_sha256={verified.report_sha256}")
    print(f"lean_operational_status={verified.operational_validation.status}")
    print("provider_calls_performed=0")
    print("network_requests_performed=0")
    print("field_application_proven=false")
    print("semantic_faithfulness_assessed=false")
    print("semantic_labels_created=false")
    print("gate_credit_claimed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
