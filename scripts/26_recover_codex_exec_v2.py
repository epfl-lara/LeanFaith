#!/usr/bin/env python3
"""Offline recovery of the single Codex v1 usage-shape drift."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.codex_exec_recovery_v2 import recover_codex_exec_v2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--attempt-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    attempt = (
        args.attempt_directory
        if args.attempt_directory.is_absolute()
        else paths.root / args.attempt_directory
    )
    output = args.output if args.output.is_absolute() else paths.root / args.output
    module = paths.root / "src/leanfaith/generation/codex_exec_recovery_v2.py"
    record, digest = recover_codex_exec_v2(
        attempt_directory=attempt,
        recovery_module_path=module,
        output_path=output,
    )
    print(f"recovery_id={record.recovery_id}")
    print(f"report={output}")
    print(f"report_sha256={digest}")
    print(f"input_tokens={record.usage.input_tokens}")
    print(f"reasoning_output_tokens={record.usage.reasoning_output_tokens}")
    print("provider_calls_performed=0")
    print("semantic_labels_created=false")
    print("gate_credit_claimed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
