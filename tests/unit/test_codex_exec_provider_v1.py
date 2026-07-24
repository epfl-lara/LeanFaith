"""Fail-closed Codex-exec provider foundation."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.generation.codex_exec_provider_v1 import (
    CodexExecArtifactConflict,
    CodexExecPrivacyError,
    CodexExecProviderConfigV1,
    LoadedCodexExecConfigV1,
    MockCodexExecutor,
    PriorCodexProbeObservation,
    ProcessCapture,
    execute_codex_exec_v1,
    make_codex_exec_request_v1,
)

UTC = datetime.datetime(2026, 7, 24, 12, 0, tzinfo=datetime.UTC)
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "const": "ok"},
        "token": {"type": "string", "const": "OK"},
    },
    "required": ["status", "token"],
}
SCHEMA_BYTES = json.dumps(SCHEMA, sort_keys=True, separators=(",", ":")).encode()
PROMPT = b"Return the probe object. Do not call tools.\n"


def _loaded(tmp_path: Path, *, model: str = "gpt-5.6-terra") -> LoadedCodexExecConfigV1:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    config = CodexExecProviderConfigV1(
        config_id="leanfaith_codex_exec_provider_probe_v1",
        status="probe_and_single_public_qualification_only",
        provider="openai_codex_exec",
        model_family="openai_codex",
        model=model,
        reasoning_effort="xhigh",
        codex_cli_version="codex-cli 0.144.1",
        codex_binary_sha256="a" * 64,
        adapter_artifact="src/provider.py",
        adapter_sha256="b" * 64,
        launcher_artifact="scripts/probe.py",
        launcher_sha256="c" * 64,
        prompt_artifact="prompts/probe.txt",
        prompt_sha256=sha256_hex(PROMPT),
        output_schema_artifact="configs/schema.json",
        output_schema_sha256=sha256_hex(SCHEMA_BYTES),
        timeout_seconds=120,
        termination_grace_seconds=2,
        intended_role="high_value_llm_mutation_or_statement_proposer",
        contamination_status="unknown_no_public_training_cutoff_or_immutable_revision",
        prior_probe_observation=PriorCodexProbeObservation(
            observed_at=UTC,
            input_tokens=16_573,
            stale_model_cache_diagnostic_observed=True,
            diagnostic_was_nonfatal=True,
            interpretation=(
                "large_fixed_codex_context_overhead_not_suitable_for_routine_high_volume_generation"
            ),
        ),
        rules=(
            "public inputs only",
            "proposer and never its own validator",
        ),
    )
    return LoadedCodexExecConfigV1(
        config=config,
        path=config_path,
        config_file_sha256=sha256_hex(config_path.read_bytes()),
        effective_config_hash=hash_canonical(config.model_dump(mode="json")),
        prompt=PROMPT,
        output_schema=SCHEMA_BYTES,
        schema_document=SCHEMA,
    )


def _request(loaded: LoadedCodexExecConfigV1):
    return make_codex_exec_request_v1(
        loaded,
        execution_mode="mock",
        input_ids=("problem:public-fixture",),
        reference_hidden=True,
        private_source_content=False,
        external_provider_eligible=True,
    )


def _capture(
    final: bytes = b'{"status":"ok","token":"OK"}',
    *,
    extra_items: tuple[dict[str, object], ...] = (),
    exit_code: int = 0,
    status: str = "completed",
    stderr: bytes = b"",
) -> ProcessCapture:
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "thread-fixture"},
        {"type": "turn.started"},
        *extra_items,
        {
            "type": "item.completed",
            "item": {
                "id": "final",
                "type": "agent_message",
                "text": final.decode(),
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 16_573,
                "cached_input_tokens": 0,
                "output_tokens": 10,
            },
        },
    ]
    stdout = b"".join(json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in events)
    return ProcessCapture(
        status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        final_message=final,
        started_at=UTC,
        completed_at=UTC + datetime.timedelta(seconds=1),
    )


def test_mock_success_is_immutable_hash_bound_and_replayable(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    request = _request(loaded)
    executor = MockCodexExecutor(_capture(stderr=b"nonfatal stale cache warning\n"))

    first = execute_codex_exec_v1(
        loaded,
        request,
        output_root=tmp_path / "runs",
        attempt_index=0,
        executor=executor,
    )
    second = execute_codex_exec_v1(
        loaded,
        request,
        output_root=tmp_path / "runs",
        attempt_index=0,
        executor=executor,
    )

    assert first.terminal.status == "success"
    assert first.terminal.parsed_output == {"status": "ok", "token": "OK"}
    assert first.terminal.usage is not None
    assert first.terminal.usage.input_tokens == 16_573
    assert first.terminal.semantic_labels_created is False
    assert first.terminal.gate_credit_claimed is False
    assert first.replayed is False
    assert second.replayed is True
    assert executor.calls == 1
    assert "--ephemeral" in first.attempt.argv
    assert "--ignore-user-config" in first.attempt.argv
    assert "read-only" in first.attempt.argv
    assert first.attempt.argv[-1] == "-"


def test_request_identity_covers_model_effort_schema_timeout_and_mode(tmp_path: Path) -> None:
    baseline = _request(_loaded(tmp_path / "one"))
    model_changed = _request(_loaded(tmp_path / "two", model="gpt-5.6"))
    external = make_codex_exec_request_v1(
        _loaded(tmp_path / "three"),
        execution_mode="external",
        input_ids=("problem:public-fixture",),
        reference_hidden=True,
        private_source_content=False,
        external_provider_eligible=True,
    )
    assert len({baseline.request_id, model_changed.request_id, external.request_id}) == 3


def test_external_privacy_guard_runs_before_executor(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    with pytest.raises(CodexExecPrivacyError):
        make_codex_exec_request_v1(
            loaded,
            execution_mode="external",
            input_ids=("problem:private",),
            reference_hidden=True,
            private_source_content=True,
            external_provider_eligible=False,
        )


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (
            (
                {
                    "type": "item.completed",
                    "item": {"id": "tool", "type": "command_execution", "text": "pwd"},
                },
            ),
            "tool_item_rejected",
        ),
        (
            (
                {
                    "type": "item.completed",
                    "item": {
                        "id": "another",
                        "type": "agent_message",
                        "text": '{"status":"ok","token":"OK"}',
                    },
                },
            ),
            "multiple_final_answers",
        ),
    ],
)
def test_tool_items_and_multiple_finals_fail_closed(
    tmp_path: Path,
    extra: tuple[dict[str, object], ...],
    expected: str,
) -> None:
    loaded = _loaded(tmp_path)
    run = execute_codex_exec_v1(
        loaded,
        _request(loaded),
        output_root=tmp_path / "runs",
        attempt_index=0,
        executor=MockCodexExecutor(_capture(extra_items=extra)),
    )
    assert run.terminal.status == expected
    assert run.terminal.parsed_output is None


def test_schema_violation_nonzero_timeout_and_secret_all_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        (_capture(b'{"status":"wrong","token":"OK"}'), "schema_violation"),
        (_capture(exit_code=1), "process_error"),
        (_capture(status="timeout"), "timeout"),
    )
    for index, (capture, expected) in enumerate(cases):
        loaded = _loaded(tmp_path / str(index))
        run = execute_codex_exec_v1(
            loaded,
            _request(loaded),
            output_root=tmp_path / "runs",
            attempt_index=index,
            executor=MockCodexExecutor(capture),
        )
        assert run.terminal.status == expected

    secret = "sk-thisMustNeverBeSerialized12345"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    loaded = _loaded(tmp_path / "secret")
    capture = _capture(stderr=f"provider leaked {secret}".encode())
    run = execute_codex_exec_v1(
        loaded,
        _request(loaded),
        output_root=tmp_path / "secret-runs",
        attempt_index=0,
        executor=MockCodexExecutor(capture),
    )
    assert run.terminal.status == "secret_redacted"
    assert secret not in (run.run_directory).read_text() if run.run_directory.is_file() else True
    assert secret not in next(run.run_directory.rglob("stderr.txt")).read_text()


def test_stale_final_output_and_tampered_replay_are_rejected(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path)
    request = _request(loaded)
    root = tmp_path / "runs"
    first = execute_codex_exec_v1(
        loaded,
        request,
        output_root=root,
        attempt_index=0,
        executor=MockCodexExecutor(_capture()),
    )
    stdout = next(first.run_directory.rglob("stdout.jsonl"))
    stdout.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(CodexExecArtifactConflict, match="hash mismatch"):
        execute_codex_exec_v1(
            loaded,
            request,
            output_root=root,
            attempt_index=0,
            executor=MockCodexExecutor(_capture()),
        )
