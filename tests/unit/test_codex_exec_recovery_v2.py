"""Offline v2 recovery of the observed Codex usage-field drift."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from leanfaith.generation.codex_exec_provider_v1 import (
    MockCodexExecutor,
    ProcessCapture,
    execute_codex_exec_v1,
)
from leanfaith.generation.codex_exec_recovery_v2 import recover_codex_exec_v2
from tests.unit.test_codex_exec_provider_v1 import _loaded, _request


def test_recovery_accepts_only_the_hash_bound_reasoning_usage_extension(
    tmp_path: Path,
) -> None:
    final = b'{"status":"ok","token":"OK"}'
    events = (
        {"type": "thread.started", "thread_id": "recovery-thread"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "final", "type": "agent_message", "text": final.decode()},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 9_742,
                "cached_input_tokens": 0,
                "output_tokens": 187,
                "reasoning_output_tokens": 127,
            },
        },
    )
    stdout = b"".join(json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in events)
    now = datetime.datetime(2026, 7, 24, tzinfo=datetime.UTC)
    loaded = _loaded(tmp_path)
    run = execute_codex_exec_v1(
        loaded,
        _request(loaded),
        output_root=tmp_path / "runs",
        attempt_index=0,
        executor=MockCodexExecutor(
            ProcessCapture(
                status="completed",
                exit_code=0,
                stdout=stdout,
                stderr=b"",
                final_message=final,
                started_at=now,
                completed_at=now + datetime.timedelta(seconds=1),
            )
        ),
    )
    assert run.terminal.status == "usage_missing_or_invalid"
    attempt_dir = next((run.run_directory / "attempts").iterdir())
    module = Path(__file__).resolve().parents[2] / (
        "src/leanfaith/generation/codex_exec_recovery_v2.py"
    )
    record, _ = recover_codex_exec_v2(
        attempt_directory=attempt_dir,
        recovery_module_path=module,
        output_path=tmp_path / "recovery.json",
    )
    assert record.recovered_status == "operationally_parsed"
    assert record.usage.input_tokens == 9_742
    assert record.usage.reasoning_output_tokens == 127
    assert record.provider_calls_performed == 0
    assert record.semantic_labels_created is False
