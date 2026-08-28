"""Offline tests for the bounded GPT-5.6 Sol LF-022 judge bridge."""

from __future__ import annotations

import datetime
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation import codex_sol_judge_v1 as sol
from leanfaith.generation.capture_redaction import _PROXY_NAME, _SECRET_NAME
from leanfaith.generation.codex_sol_judge_v1 import (
    CodexSolAuthorizationError,
    CodexSolLiveAuthorization,
    CodexSolPartialAttemptError,
    CodexSolProcessCapture,
    execute_codex_sol_weak_cells,
    load_codex_sol_judge_config,
    replay_codex_sol_weak_cells,
)
from leanfaith.generation.lf022_weak_batch import (
    BoundArtifact,
    JudgeEndpointPin,
    LF022WeakBatchSpec,
    LF022WeakDispatchManifest,
    LF022WeakDispatchRecord,
    LF022WeakExecutionStartedMarker,
)
from leanfaith.generation.providers import (
    DecodingValue,
    ProviderIdentity,
    ProviderRequest,
    load_provider_raw_response,
    provider_raw_response_path,
)
from leanfaith.generation.weak_supervision import (
    JudgePresentation,
    JudgeResponse,
    parse_blinded_judge_output,
)
from leanfaith.schemas.ids import make_id

CONFIG = Path("configs/generation/lf022_codex_sol_judge_v1.yaml")
NOW = datetime.datetime(2026, 8, 12, 12, 0, tzinfo=datetime.UTC)
AUTH_NONCE = b"authorization-nonce-for-sol-test-00"
RUN_NONCE = b"run-nonce-for-sol-test-shard-00000"
TEST_AUTH_SECRET = b"eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzb2wtaXNvbGF0aW9uIn0.signature9876543210"
TEST_AUTH_BYTES = (
    canonical_json_bytes({"tokens": {"access_token": TEST_AUTH_SECRET.decode("ascii")}}) + b"\n"
)


def _scrub_ambient_secret_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop host env vars the capture redactor treats as exact secrets.

    Harness environments (CI runners, IDE agent sessions) export flag-style
    variables such as ``*_AUTH_REFRESH=1`` whose secret-matching *names* carry
    trivial values; the fail-closed redactor would then rewrite every matching
    byte of the offline fixture captures and flip attempt terminals to
    ``secret_redacted``.  Tests that exercise redaction set their own secrets
    after this scrub, so the redaction contract itself stays fully covered.
    """

    for name in list(os.environ):
        if _SECRET_NAME.search(name) or _PROXY_NAME.search(name):
            monkeypatch.delenv(name, raising=False)


def _response(*, equivalent: bool = False) -> bytes:
    if equivalent:
        payload: dict[str, object] = {
            "same_claim_answer": "same_claim",
            "relation": "equivalent",
            "A_implies_B": "yes",
            "B_implies_A": "yes",
            "error_types": [],
            "confidence": 0.98,
            "rationale": "The statements express the same mathematical claim.",
            "needs_expert_review": False,
        }
    else:
        payload = {
            "same_claim_answer": "not_same_claim",
            "relation": "A_stronger",
            "A_implies_B": "yes",
            "B_implies_A": "no",
            "error_types": ["E01"],
            "confidence": 0.97,
            "rationale": "Statement A has a strictly stronger conclusion.",
            "needs_expert_review": False,
        }
    return canonical_json_bytes(payload)


def _stdout(final: bytes) -> bytes:
    text = final.decode("utf-8")
    events = (
        {"type": "thread.started", "thread_id": "fixture"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item-1", "type": "agent_message", "text": text},
        },
        {"type": "turn.completed", "usage": {}},
    )
    return b"".join(canonical_json_bytes(item) + b"\n" for item in events)


def _capture(
    *,
    response: bytes | None = None,
    exit_code: int = 0,
    status: str = "completed",
) -> CodexSolProcessCapture:
    final = response or _response()
    return CodexSolProcessCapture.model_validate(
        {
            "status": status,
            "exit_code": exit_code,
            "stdout": _stdout(final) if exit_code == 0 else b"process failed\n",
            "stderr": b"" if exit_code == 0 else b"failure\n",
            "final_message": final if exit_code == 0 else None,
        }
    )


class _Executor:
    def __init__(
        self,
        captures: list[CodexSolProcessCapture],
        *,
        raise_after_inspection: bool = False,
    ) -> None:
        self.captures = captures
        self.raise_after_inspection = raise_after_inspection
        self.calls: list[tuple[tuple[str, ...], bytes, Path, dict[str, str]]] = []
        self.isolated_homes: list[Path] = []

    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        max_final_message_bytes: int,
        child_env: Mapping[str, str],
    ) -> CodexSolProcessCapture:
        del (
            final_message_path,
            timeout_seconds,
            termination_grace_seconds,
            max_stdout_bytes,
            max_stderr_bytes,
            max_final_message_bytes,
        )
        assert list(cwd.iterdir()) == []
        source_home = Path(os.environ["CODEX_HOME"])
        isolated_home = Path(child_env["CODEX_HOME"])
        isolated_user_home = Path(child_env["HOME"])
        assert isolated_home != source_home
        assert isolated_user_home != Path(os.environ["HOME"])
        assert isolated_user_home.parent == isolated_home.parent
        assert stat.S_IMODE(isolated_home.stat().st_mode) == 0o700
        assert stat.S_IMODE(isolated_user_home.stat().st_mode) == 0o700
        assert list(isolated_user_home.iterdir()) == []
        assert [item.name for item in isolated_home.iterdir()] == ["auth.json"]
        isolated_auth = isolated_home / "auth.json"
        assert stat.S_IMODE(isolated_auth.stat().st_mode) == 0o600
        assert isolated_auth.read_bytes() == TEST_AUTH_BYTES
        assert not (isolated_home / "logs_2.sqlite").exists()
        self.isolated_homes.append(isolated_home)
        self.calls.append((tuple(argv), prompt, cwd, dict(child_env)))
        if self.raise_after_inspection:
            raise RuntimeError("synthetic executor failure")
        if not self.captures:
            raise AssertionError("unexpected external call")
        return self.captures.pop(0)


def _fake_source_codex_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir(mode=0o700)
    auth_path = source_home / "auth.json"
    auth_path.write_bytes(TEST_AUTH_BYTES)
    auth_path.chmod(0o600)
    with (source_home / "logs_2.sqlite").open("wb") as handle:
        handle.truncate(32 * 1024 * 1024)
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    return source_home


def _task(*, orientation: str, slot: str = "judge_A") -> JudgePresentation:
    return JudgePresentation.model_construct(
        schema_version=1,
        task_id="judge_task:" + ("a" if orientation == "AB" else "b") * 64,
        opaque_task_token="lf022_judge_item_v1:" + "c" * 64,
        pair_id="pair:" + "d" * 64,
        judge_slot=slot,
        orientation=orientation,
        lean_a="theorem a (n : Nat) : n = n",
        lean_b="theorem b (n : Nat) : n ≤ n",
        optional_natural_language=None,
        randomization_key_sha256="e" * 64,
        source_admission_sha256="f" * 64,
        external_transmission_allowed=True,
    )


def _authorization(
    *,
    path: Path,
    batch_id: str,
    config_sha256: str,
    offset_pairs: int = 0,
    limit_pairs: int = 1,
    shard_id: str = "smoke-000",
) -> Path:
    values: dict[str, object] = {
        "schema_version": 1,
        "batch_id": batch_id,
        "config_sha256": config_sha256,
        "judge_slot": "judge_A",
        "shard_id": shard_id,
        "offset_pairs": offset_pairs,
        "limit_pairs": limit_pairs,
        "authorization_nonce_sha256": sha256_hex(AUTH_NONCE),
        "approved_at": (NOW - datetime.timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (NOW + datetime.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "approved_by": "offline-test-reviewer",
        "public_external_execution_authorized": True,
        "private_source_content_authorized": False,
        "semantic_labels_created": False,
        "gate_credit_claimed": False,
    }
    authorization = CodexSolLiveAuthorization.model_validate(
        {
            **values,
            "authorization_id": make_id("lf022_sol_live_authorization", values),
        }
    )
    path.write_bytes(canonical_json_bytes(authorization.model_dump(mode="json")) + b"\n")
    return path


def _fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    endpoint_decoding: dict[str, DecodingValue] | None = None,
) -> tuple[Path, Path, list[ProviderRequest], Path]:
    _scrub_ambient_secret_environment(monkeypatch)
    _fake_source_codex_home(monkeypatch, tmp_path)
    loaded = load_codex_sol_judge_config(CONFIG)
    config = loaded.config
    sol_endpoint = JudgeEndpointPin(
        provider_slot="judge_A",
        provider=config.provider,
        model=config.registry_model_id,
        family_id=config.model_family,
        revision=config.endpoint_revision,
        decoding=endpoint_decoding or config.endpoint_decoding,
    )
    other_endpoint = JudgeEndpointPin(
        provider_slot="judge_B",
        provider="anthropic_claude_code",
        model="anthropic/claude-fable-5",
        family_id="anthropic_fable",
        revision="provider-deployment-snapshot:" + "1" * 64,
        decoding={"effort": "max"},
    )
    spec = LF022WeakBatchSpec.model_construct(
        schema_version=1,
        method_version="lf022_weak_batch_v1",
        batch_name="fixture",
        candidate_manifest=BoundArtifact(path="unused", sha256="1" * 64),
        candidate_records=BoundArtifact(path="unused", sha256="2" * 64),
        weak_supervision_config=BoundArtifact(path="unused", sha256="3" * 64),
        production_family_matrix=BoundArtifact(path="unused", sha256="4" * 64),
        randomization_key_sha256="5" * 64,
        judge_a=sol_endpoint,
        judge_b=other_endpoint,
        primary_eval_family_id="deepseek_v4",
        execution_authorization="live_provider_calls_explicitly_authorized",
        live_provider_calls_authorized=True,
        semantic_labels_created=False,
        silver_records_created=False,
        training_eligible=False,
        evaluation_eligible=False,
        gate_credit_claimed=False,
    )
    requests: list[ProviderRequest] = []
    dispatches: list[LF022WeakDispatchRecord] = []
    for orientation in ("AB", "BA"):
        task = _task(orientation=orientation)
        request = ProviderRequest.create(
            identity=ProviderIdentity(
                provider=config.provider,
                model=config.registry_model_id,
                revision=config.endpoint_revision,
                transport="fixture",
            ),
            prompt_template_hash=config.judge_template_sha256,
            rendered_prompt=f"strict blinded {orientation} prompt",
            decoding=sol_endpoint.decoding,
            input_ids=(task.task_id,),
            private_source_content=False,
        )
        requests.append(request)
        dispatches.append(
            LF022WeakDispatchRecord.model_construct(
                schema_version=1,
                method_version="lf022_weak_batch_v1",
                dispatch_cell_id="lf022_weak_cell:" + ("6" if orientation == "AB" else "7") * 64,
                candidate_inventory_record_id="lf022_supervision_candidate:" + "8" * 64,
                pair_id=task.pair_id,
                proposer_family_id="qwen3",
                judge_family_id=config.model_family,
                judge_slot="judge_A",
                orientation=orientation,
                task=task,
                request_artifact=f"requests/{orientation}.json",
                request_artifact_sha256="9" * 64,
                provider_request_hash=request.request_hash,
                provider_attempt_id=request.attempt_id,
                prompt_template_sha256=config.judge_template_sha256,
                prompt_render_sha256=request.prompt_render_hash,
                source_admission_sha256=task.source_admission_sha256,
                semantic_label_created=False,
                silver_promoted=False,
                train_eligible=False,
                eval_eligible=False,
                gate_credit_claimed=False,
            )
        )
    manifest = LF022WeakDispatchManifest.model_construct(
        batch_id="lf022_weak_batch:" + "a" * 64,
    )
    batch_root = tmp_path / "batch"
    batch_root.mkdir()
    (batch_root / "dispatch_manifest.json").write_text("{}\n", encoding="utf-8")
    by_cell = dict(zip((item.dispatch_cell_id for item in dispatches), requests, strict=True))
    monkeypatch.setattr(
        sol,
        "_load_prepared_batch",
        lambda _root: (spec, manifest, tuple(dispatches), None),
    )
    monkeypatch.setattr(
        sol,
        "_verify_dispatch_request",
        lambda _root, dispatch, _spec: by_cell[dispatch.dispatch_cell_id],
    )
    monkeypatch.setattr(sol, "_check_binary", lambda _config: Path("/bin/true"))
    monkeypatch.setattr(sol, "_utcnow", lambda: NOW)
    authorization = _authorization(
        path=tmp_path / "authorization.json",
        batch_id=manifest.batch_id,
        config_sha256=loaded.sha256,
    )
    return batch_root, tmp_path / "sol-output", requests, authorization


def _execute(
    *,
    batch_root: Path,
    output_root: Path,
    authorization: Path,
    executor: _Executor,
) -> sol.CodexSolRunResult:
    return execute_codex_sol_weak_cells(
        batch_root=batch_root,
        output_root=output_root,
        config_path=CONFIG,
        judge_slot="judge_A",
        shard_id="smoke-000",
        offset_pairs=0,
        limit_pairs=1,
        run_nonce=RUN_NONCE,
        authorization_path=authorization,
        authorization_nonce=AUTH_NONCE,
        execute_external=True,
        executor=executor,
    )


def test_local_cli_and_frozen_config_match() -> None:
    loaded = load_codex_sol_judge_config(CONFIG)
    assert loaded.config.model == "gpt-5.6-sol"
    assert loaded.config.registry_model_id == "openai/gpt-5.6-sol"
    assert loaded.config.reasoning_effort == "xhigh"
    assert loaded.config.output_schema_sha256 == sha256_hex(sol._output_schema_bytes())
    binary = Path(
        "/localhome/milikic/.codex/packages/standalone/releases/"
        "0.144.1-x86_64-unknown-linux-musl/bin/codex"
    )
    if binary.is_file():
        assert hash_file(binary) == loaded.config.codex_binary_sha256


def test_one_pair_executes_both_orders_raw_first_then_replays_without_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, output_root, requests, authorization = _fixture(monkeypatch, tmp_path)
    original_parse = parse_blinded_judge_output

    def assert_raw_first(text: str) -> JudgeResponse:
        request = requests[len(executor.calls) - 1]
        directory = sol._attempt_dir(output_root, request.request_hash, 0)
        assert (directory / "stdout.jsonl").is_file()
        assert (directory / "stderr.txt").is_file()
        assert (directory / "final_message.json").is_file()
        assert not (directory / "parsed_response.json").exists()
        return original_parse(text)

    monkeypatch.setattr(sol, "parse_blinded_judge_output", assert_raw_first)
    monkeypatch.setenv("RCP_API_KEY", "must-not-reach-child")
    executor = _Executor([_capture(), _capture(response=_response(equivalent=True))])
    first = _execute(
        batch_root=batch_root,
        output_root=output_root,
        authorization=authorization,
        executor=executor,
    )
    assert first.manifest.selected_pair_count == 1
    assert first.manifest.invoked_cell_count == 2
    assert first.manifest.completed_cell_count == 2
    assert first.manifest.execution_mode == "external"
    assert len(executor.calls) == 2
    marker = LF022WeakExecutionStartedMarker.model_validate_json(
        (batch_root / "execution_started.json").read_bytes()
    )
    assert marker.batch_id == first.manifest.batch_id
    assert marker.provider_attempt_may_have_started
    assert len(executor.isolated_homes) == 2
    assert len(set(executor.isolated_homes)) == 2
    assert all(not path.exists() for path in executor.isolated_homes)
    for argv, prompt, _, child_env in executor.calls:
        assert argv[0] == "/bin/true"
        assert argv[1:5] == ("exec", "--ephemeral", "--ignore-user-config", "--ignore-rules")
        assert argv[argv.index("--disable") + 1] == "shell_tool"
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
        assert 'model_reasoning_effort="xhigh"' in argv
        assert 'cli_auth_credentials_store="file"' in argv
        assert "web_search=disabled" in argv
        assert "shell_environment_policy.inherit=none" in argv
        assert prompt.startswith(sol._SYSTEM_PROMPT.encode("utf-8") + b"\n\n")
        assert "RCP_API_KEY" not in child_env
        assert child_env["CODEX_HOME"] != os.environ["CODEX_HOME"]

    for path in (*output_root.rglob("*"), *(batch_root / "raw").rglob("*")):
        if path.is_file():
            assert TEST_AUTH_SECRET not in path.read_bytes(), path

    for index, request in enumerate(requests):
        raw_path = provider_raw_response_path(batch_root / "raw/judge_A", request)
        raw_response = load_provider_raw_response(raw_path, request=request)
        assert raw_response.output_text == _response(equivalent=index == 1).decode("utf-8")

    monkeypatch.setattr(sol, "parse_blinded_judge_output", original_parse)
    replay = replay_codex_sol_weak_cells(
        batch_root=batch_root,
        output_root=output_root,
        config_path=CONFIG,
        judge_slot="judge_A",
        shard_id="smoke-000",
        offset_pairs=0,
        limit_pairs=1,
        run_nonce=b"different-replay-run-nonce-00000",
    )
    assert replay.manifest.execution_mode == "offline_replay"
    assert replay.manifest.invoked_cell_count == 0
    assert replay.manifest.reused_cell_count == 2
    assert replay.manifest.run_id != first.manifest.run_id
    assert len(executor.calls) == 2


def test_isolated_codex_home_cleans_up_when_executor_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, output_root, _, authorization = _fixture(monkeypatch, tmp_path)
    executor = _Executor([], raise_after_inspection=True)
    with pytest.raises(RuntimeError, match="synthetic executor failure"):
        _execute(
            batch_root=batch_root,
            output_root=output_root,
            authorization=authorization,
            executor=executor,
        )
    assert len(executor.isolated_homes) == 1
    assert not executor.isolated_homes[0].exists()
    for path in (*output_root.rglob("*"), *(batch_root / "raw").rglob("*")):
        if path.is_file():
            assert TEST_AUTH_SECRET not in path.read_bytes(), path


def test_oauth_secret_from_auth_json_is_redacted_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, output_root, requests, authorization = _fixture(monkeypatch, tmp_path)
    leaked = _capture().model_copy(update={"stderr": b"oauth=" + TEST_AUTH_SECRET + b"\n"})
    executor = _Executor([leaked, _capture(), _capture(response=_response(equivalent=True))])
    result = _execute(
        batch_root=batch_root,
        output_root=output_root,
        authorization=authorization,
        executor=executor,
    )
    assert result.manifest.completed_cell_count == 2
    leaked_terminal = sol._load_terminal(
        sol._attempt_dir(output_root, requests[0].request_hash, 0) / "terminal.json"
    )
    assert leaked_terminal.status == "secret_redacted"
    assert leaked_terminal.redaction_count >= 1
    for path in (*output_root.rglob("*"), *(batch_root / "raw").rglob("*")):
        if path.is_file():
            assert TEST_AUTH_SECRET not in path.read_bytes(), path


def test_subprocess_executor_uses_isolated_home_below_file_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_home = _fake_source_codex_home(monkeypatch, tmp_path)
    inherited_log = source_home / "logs_2.sqlite"
    assert inherited_log.stat().st_size > 4097
    helper = tmp_path / "fake-codex"
    helper.write_text(
        """#!/usr/bin/env python3
import hashlib
import os
import pathlib
import stat
import sys

home = pathlib.Path(os.environ["CODEX_HOME"])
user_home = pathlib.Path(os.environ["HOME"])
auth = home / "auth.json"
assert stat.S_IMODE(home.stat().st_mode) == 0o700
assert stat.S_IMODE(user_home.stat().st_mode) == 0o700
assert user_home.parent == home.parent
assert list(user_home.iterdir()) == []
assert stat.S_IMODE(auth.stat().st_mode) == 0o600
assert sorted(item.name for item in home.iterdir()) == ["auth.json"]
assert hashlib.sha256(auth.read_bytes()).hexdigest() == sys.argv[2]
with (home / "logs_2.sqlite").open("ab") as handle:
    handle.write(b"bounded-log-write")
pathlib.Path(sys.argv[1]).write_bytes(b'{"ok":true}')
sys.stdout.buffer.write(b'{"type":"turn.completed"}\\n')
sys.stderr.write(f"isolated-home={home}\\n")
""",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    final_path = tmp_path / "subprocess-final.json"
    config = load_codex_sol_judge_config(CONFIG).config
    isolated_home: Path | None = None
    with sol._isolated_codex_environment(config) as child_env:
        isolated_home = Path(child_env["CODEX_HOME"])
        capture = sol.SubprocessCodexSolCliExecutor().execute(
            argv=(str(helper), str(final_path), sha256_hex(TEST_AUTH_BYTES)),
            prompt=b"offline helper input",
            cwd=tmp_path,
            final_message_path=final_path,
            timeout_seconds=10,
            termination_grace_seconds=1,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
            max_final_message_bytes=4096,
            child_env=child_env,
        )
        assert (isolated_home / "logs_2.sqlite").read_bytes() == b"bounded-log-write"
    assert isolated_home is not None and not isolated_home.exists()
    assert capture.status == "completed"
    assert capture.exit_code == 0
    assert capture.final_message == b'{"ok":true}'
    assert TEST_AUTH_SECRET not in capture.stdout
    assert TEST_AUTH_SECRET not in capture.stderr
    assert TEST_AUTH_SECRET not in (capture.final_message or b"")


def test_attempt_specific_retry_and_generic_attempt_zero_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, output_root, requests, authorization = _fixture(monkeypatch, tmp_path)
    executor = _Executor(
        [
            _capture(exit_code=2),
            _capture(),
            _capture(exit_code=2),
            _capture(response=_response(equivalent=True)),
        ]
    )
    result = _execute(
        batch_root=batch_root,
        output_root=output_root,
        authorization=authorization,
        executor=executor,
    )
    assert result.manifest.process_attempt_count == 4
    assert all(item.attempt_index == 1 for item in result.terminals)
    for base_request in requests:
        retry = sol._attempt_request(base_request, 1)
        assert retry.request_hash == base_request.request_hash
        assert retry.attempt_id != base_request.attempt_id
        attempt_path = provider_raw_response_path(output_root / "provider_raw_attempts", retry)
        bridge_path = provider_raw_response_path(batch_root / "raw/judge_A", base_request)
        load_provider_raw_response(attempt_path, request=retry)
        load_provider_raw_response(bridge_path, request=base_request)


def test_exhausted_failures_replay_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, output_root, _, authorization = _fixture(monkeypatch, tmp_path)
    executor = _Executor([_capture(exit_code=2) for _ in range(4)])
    first = _execute(
        batch_root=batch_root,
        output_root=output_root,
        authorization=authorization,
        executor=executor,
    )
    assert first.manifest.completed_cell_count == 0
    assert first.manifest.exhausted_cell_count == 2
    replay = replay_codex_sol_weak_cells(
        batch_root=batch_root,
        output_root=output_root,
        config_path=CONFIG,
        judge_slot="judge_A",
        shard_id="smoke-000",
        offset_pairs=0,
        limit_pairs=1,
        run_nonce=b"exhausted-replay-run-nonce-000000",
    )
    assert replay.manifest.exhausted_cell_count == 2
    assert replay.manifest.invoked_cell_count == 0
    assert len(executor.calls) == 4


def test_partial_with_raw_files_is_not_recalled_at_same_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, output_root, requests, authorization = _fixture(monkeypatch, tmp_path)
    base = requests[0]
    attempt = sol._attempt_request(base, 0)
    directory = sol._attempt_dir(output_root, base.request_hash, 0)
    sol._prepare_attempt_artifacts(
        config=load_codex_sol_judge_config(CONFIG).config,
        request=attempt,
        directory=directory,
        binary=Path("/bin/true"),
    )
    (directory / "stdout.jsonl").write_bytes(b"abandoned\n")
    (directory / "stderr.txt").write_bytes(b"")
    executor = _Executor([])
    with pytest.raises(CodexSolPartialAttemptError, match="refusing recall"):
        _execute(
            batch_root=batch_root,
            output_root=output_root,
            authorization=authorization,
            executor=executor,
        )
    assert executor.calls == []
    assert not (directory / "terminal.json").exists()


def test_incomplete_partial_fails_closed_without_external_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, output_root, requests, authorization = _fixture(monkeypatch, tmp_path)
    base = requests[0]
    attempt = sol._attempt_request(base, 0)
    sol._prepare_attempt_artifacts(
        config=load_codex_sol_judge_config(CONFIG).config,
        request=attempt,
        directory=sol._attempt_dir(output_root, base.request_hash, 0),
        binary=Path("/bin/true"),
    )
    executor = _Executor([])
    with pytest.raises(CodexSolPartialAttemptError, match="refusing recall"):
        _execute(
            batch_root=batch_root,
            output_root=output_root,
            authorization=authorization,
            executor=executor,
        )
    assert executor.calls == []


def test_interruption_is_terminalized_then_propagated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, output_root, requests, authorization = _fixture(monkeypatch, tmp_path)
    executor = _Executor([_capture(exit_code=130, status="interrupted")])
    with pytest.raises(KeyboardInterrupt):
        _execute(
            batch_root=batch_root,
            output_root=output_root,
            authorization=authorization,
            executor=executor,
        )
    directory = sol._attempt_dir(output_root, requests[0].request_hash, 0)
    terminal = sol._load_terminal(directory / "terminal.json")
    assert terminal.status == "interrupted"
    assert (directory / "process_receipt.json").is_file()
    assert len(executor.calls) == 1


def test_live_requires_both_artifact_and_explicit_boolean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, output_root, _, authorization = _fixture(monkeypatch, tmp_path)
    with pytest.raises(CodexSolAuthorizationError, match="execute_external=True"):
        execute_codex_sol_weak_cells(
            batch_root=batch_root,
            output_root=output_root,
            config_path=CONFIG,
            judge_slot="judge_A",
            shard_id="smoke-000",
            offset_pairs=0,
            limit_pairs=1,
            run_nonce=RUN_NONCE,
            authorization_path=authorization,
            authorization_nonce=AUTH_NONCE,
            execute_external=False,
            executor=_Executor([]),
        )


def test_endpoint_decoding_is_exact_not_subset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = load_codex_sol_judge_config(CONFIG).config
    weakened = dict(config.endpoint_decoding)
    weakened.pop("shell_tool_disabled")
    batch_root, output_root, _, authorization = _fixture(
        monkeypatch,
        tmp_path,
        endpoint_decoding=weakened,
    )
    with pytest.raises(sol.CodexSolJudgeError, match="endpoint differs"):
        _execute(
            batch_root=batch_root,
            output_root=output_root,
            authorization=authorization,
            executor=_Executor([]),
        )


def test_strict_stdout_rejects_tool_use_and_final_schema_is_coherent() -> None:
    final = _response()
    assert sol._validate_codex_stdout(_stdout(final), final) is None
    tool_events = (
        {"type": "thread.started"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "command_execution"}},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": final.decode("utf-8")},
        },
        {"type": "turn.completed"},
    )
    tool_stdout = b"".join(canonical_json_bytes(item) + b"\n" for item in tool_events)
    assert "rejected tool" in str(sol._validate_codex_stdout(tool_stdout, final))
    incoherent = json.loads(final)
    incoherent["relation"] = "equivalent"
    with pytest.raises(sol.CodexSolJudgeError, match="invalid structured judge response"):
        sol._parse_final(canonical_json_bytes(incoherent))


def test_proxy_credentials_are_redacted_before_any_durable_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, output_root, requests, authorization = _fixture(monkeypatch, tmp_path)
    credential = "proxy-user:proxy-password-987654"
    proxy_url = f"https://{credential}@proxy.example:8443"
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    leaked = _capture().model_copy(update={"stderr": f"proxy={proxy_url}\n".encode()})
    executor = _Executor([leaked, _capture(), _capture()])
    result = _execute(
        batch_root=batch_root,
        output_root=output_root,
        authorization=authorization,
        executor=executor,
    )
    assert result.manifest.completed_cell_count == 2
    leaked_terminal = sol._load_terminal(
        sol._attempt_dir(output_root, requests[0].request_hash, 0) / "terminal.json"
    )
    assert leaked_terminal.status == "secret_redacted"
    assert leaked_terminal.redaction_count >= 1
    for path in (*output_root.rglob("*"), *(batch_root / "raw").rglob("*")):
        if path.is_file():
            payload = path.read_bytes()
            assert credential.encode() not in payload, path
            assert proxy_url.encode() not in payload, path


def test_selected_cells_preserve_prepared_hmac_order_not_lexical_orientation() -> None:
    ba = _task(orientation="BA")
    ab = _task(orientation="AB")
    dispatches = tuple(
        LF022WeakDispatchRecord.model_construct(
            schema_version=1,
            method_version="lf022_weak_batch_v1",
            dispatch_cell_id="lf022_weak_cell:" + marker * 64,
            candidate_inventory_record_id="lf022_supervision_candidate:" + "8" * 64,
            pair_id=task.pair_id,
            proposer_family_id="qwen3",
            judge_family_id="openai_codex_sol",
            judge_slot="judge_A",
            orientation=task.orientation,
            task=task,
            request_artifact="unused",
            request_artifact_sha256="9" * 64,
            provider_request_hash="a" * 64,
            provider_attempt_id="provider-attempt:" + "b" * 64,
            prompt_template_sha256="c" * 64,
            prompt_render_sha256="d" * 64,
            source_admission_sha256="f" * 64,
            semantic_label_created=False,
            silver_promoted=False,
            train_eligible=False,
            eval_eligible=False,
            gate_credit_claimed=False,
        )
        for marker, task in (("7", ba), ("6", ab))
    )
    selected = sol._selected_cells(dispatches, slot="judge_A", offset_pairs=0, limit_pairs=1)
    assert [item.orientation for item in selected] == ["BA", "AB"]
