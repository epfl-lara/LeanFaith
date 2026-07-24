#!/usr/bin/env python3
"""Run one offline mock or explicitly requested real Codex-exec provider probe."""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.codex_exec_provider_v1 import (
    MockCodexExecutor,
    ProcessCapture,
    SubprocessCodexExecutor,
    execute_codex_exec_v1,
    load_codex_exec_config_v1,
    make_codex_exec_request_v1,
    verify_codex_cli_pin,
)


def _mock_capture(final: bytes, *, input_tokens: int) -> ProcessCapture:
    now = datetime.datetime(2026, 7, 24, 12, 0, tzinfo=datetime.UTC)
    text = final.decode("utf-8")
    events = (
        {"type": "thread.started", "thread_id": "mock-thread-v1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "mock-final", "type": "agent_message", "text": text},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": 0,
                "output_tokens": 16,
            },
        },
    )
    stdout = b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        for event in events
    )
    return ProcessCapture(
        status="completed",
        exit_code=0,
        stdout=stdout,
        stderr=b"offline mock: prior live probe observed a nonfatal stale model-cache diagnostic\n",
        final_message=final,
        started_at=now,
        completed_at=now + datetime.timedelta(milliseconds=25),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run exactly one immutable Codex-exec provider attempt. Defaults to an "
            "offline mock; --real is explicit and performs external model I/O."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--real", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--input-id", action="append", required=True)
    parser.add_argument("--attempt-index", type=int, default=0)
    parser.add_argument("--reference-hidden", action="store_true")
    parser.add_argument(
        "--mock-final-json",
        default='{"status":"ok","token":"OK"}',
        help="Offline mock final object; ignored by --real.",
    )
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    config_path = args.config if args.config.is_absolute() else paths.root / args.config
    output_root = (
        args.output_root if args.output_root.is_absolute() else paths.root / args.output_root
    )
    loaded = load_codex_exec_config_v1(config_path, repo_root=paths.root)
    execution_mode = "external" if args.real else "mock"
    request = make_codex_exec_request_v1(
        loaded,
        execution_mode=execution_mode,
        input_ids=tuple(args.input_id),
        reference_hidden=args.reference_hidden,
        private_source_content=False,
        external_provider_eligible=True,
    )
    if args.real:
        verify_codex_cli_pin(loaded.config)
        executor = SubprocessCodexExecutor()
    else:
        document = json.loads(args.mock_final_json)
        final = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        executor = MockCodexExecutor(
            _mock_capture(
                final,
                input_tokens=loaded.config.prior_probe_observation.input_tokens,
            )
        )
    run = execute_codex_exec_v1(
        loaded,
        request,
        output_root=output_root,
        attempt_index=args.attempt_index,
        executor=executor,
    )
    terminal_dir = (
        run.run_directory
        / "attempts"
        / (
            f"{run.attempt.attempt_index:04d}-"
            f"{run.attempt.attempt_id.removeprefix('codex_exec_attempt_v1:')}"
        )
    )
    print(f"request_id={run.request.request_id}")
    print(f"attempt_id={run.attempt.attempt_id}")
    print(f"terminal_id={run.terminal.terminal_id}")
    print(f"terminal_status={run.terminal.status}")
    print(f"terminal_artifact={terminal_dir / 'terminal.json'}")
    print(f"replayed={str(run.replayed).lower()}")
    print(f"input_tokens={run.terminal.usage.input_tokens if run.terminal.usage else 'unknown'}")
    print("semantic_labels_created=false")
    print("supervision_eligible=false")
    print("gate_credit_claimed=false")
    return 0 if run.terminal.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
