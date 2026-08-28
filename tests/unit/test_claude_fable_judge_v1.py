"""Offline tests for the bounded Claude Fable 5 LF-022 judge bridge."""

from __future__ import annotations

import datetime
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation import claude_fable_judge_v1 as fable
from leanfaith.generation.capture_redaction import _PROXY_NAME, _SECRET_NAME
from leanfaith.generation.claude_fable_judge_v1 import (
    ClaudeCliCapture,
    ClaudeCliExecutor,
    ClaudeFableJudgeError,
    ClaudeFableLiveAuthorization,
    load_claude_fable_judge_config,
)
from leanfaith.generation.claude_fable_judge_v1 import (
    run_claude_fable_weak_cells as _run_claude_fable_weak_cells,
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
    ProviderIdentity,
    ProviderRequest,
    load_provider_raw_response,
    provider_raw_response_path,
)
from leanfaith.generation.weak_supervision import JudgePresentation, JudgeSlot
from leanfaith.schemas.ids import make_id

CONFIG = Path("configs/generation/lf022_claude_fable_judge_v1.yaml")
NOW = datetime.datetime(2026, 8, 12, tzinfo=datetime.UTC)
AUTH_NONCE = b"authorization-nonce-for-fable-test"
RUN_NONCE = b"run-nonce-for-fable-test-shard-00"


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


def run_claude_fable_weak_cells(
    *,
    batch_root: Path,
    raw_response_root: Path,
    output_root: Path,
    config_path: Path,
    judge_slot: JudgeSlot,
    offset_pairs: int,
    limit_pairs: int,
    execute_external: bool,
    executor: ClaudeCliExecutor | None = None,
    now: datetime.datetime | None = None,
) -> fable.ClaudeFableRunResult:
    del now
    return _run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_response_root,
        output_root=output_root,
        config_path=config_path,
        judge_slot=judge_slot,
        shard_id="smoke-000",
        offset_pairs=offset_pairs,
        limit_pairs=limit_pairs,
        run_nonce=RUN_NONCE,
        execute_external=execute_external,
        authorization_path=(batch_root.parent / "fable-authorization.json")
        if execute_external
        else None,
        authorization_nonce=AUTH_NONCE if execute_external else None,
        executor=executor,
    )


def _response(*, equivalent: bool = False) -> dict[str, object]:
    if equivalent:
        return {
            "same_claim_answer": "same_claim",
            "relation": "equivalent",
            "A_implies_B": "yes",
            "B_implies_A": "yes",
            "error_types": [],
            "confidence": 0.98,
            "rationale": "The statements express the same claim.",
            "needs_expert_review": False,
        }
    return {
        "same_claim_answer": "not_same_claim",
        "relation": "A_stronger",
        "A_implies_B": "yes",
        "B_implies_A": "no",
        "error_types": ["E01"],
        "confidence": 0.97,
        "rationale": "Statement A has a strictly stronger conclusion.",
        "needs_expert_review": False,
    }


def _capture(*, response: dict[str, object] | None = None, exit_code: int = 0) -> ClaudeCliCapture:
    wrapper = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": response or _response(),
    }
    return ClaudeCliCapture(
        status="completed",
        exit_code=exit_code,
        stdout=canonical_json_bytes(wrapper) + b"\n",
        stderr=b"",
    )


class _Executor:
    def __init__(
        self,
        captures: list[ClaudeCliCapture],
        *,
        required_execution_marker: Path | None = None,
    ) -> None:
        self.captures = captures
        self.required_execution_marker = required_execution_marker
        self.calls: list[tuple[tuple[str, ...], bytes, Path, dict[str, str]]] = []

    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> ClaudeCliCapture:
        del timeout_seconds, termination_grace_seconds
        if self.required_execution_marker is not None:
            marker = LF022WeakExecutionStartedMarker.model_validate_json(
                self.required_execution_marker.read_bytes()
            )
            assert marker.provider_attempt_may_have_started
        self.calls.append((tuple(argv), prompt, cwd, dict(env)))
        if not self.captures:
            raise AssertionError("unexpected external call")
        return self.captures.pop(0)


def _task(*, orientation: str, slot: str = "judge_B") -> JudgePresentation:
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


def _fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    live_authorized: bool = True,
) -> tuple[Path, Path, list[ProviderRequest]]:
    _scrub_ambient_secret_environment(monkeypatch)
    config = load_claude_fable_judge_config(CONFIG).config
    revision = config.endpoint_revision
    fable_endpoint = JudgeEndpointPin(
        provider_slot="judge_B",
        provider=config.provider,
        model=config.registry_model_id,
        family_id=config.model_family,
        revision=revision,
        decoding={
            "effort": config.effort,
            "system_prompt_sha256": config.system_prompt_sha256,
            "output_schema_sha256": config.output_schema_sha256,
            "claude_cli_version": config.claude_cli_version,
            "claude_binary_sha256": config.claude_binary_sha256,
            "structured_output": True,
            "safe_mode": True,
            "tools_disabled": True,
            "session_persistence": False,
        },
    )
    other_endpoint = JudgeEndpointPin(
        provider_slot="judge_A",
        provider="openai_codex_exec",
        model="gpt-5.6-sol",
        family_id="openai_codex_sol",
        revision="provider-deployment-snapshot:" + "1" * 64,
        decoding={"reasoning_effort": "xhigh"},
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
        judge_a=other_endpoint,
        judge_b=fable_endpoint,
        primary_eval_family_id="deepseek_v4",
        execution_authorization=(
            "live_provider_calls_explicitly_authorized"
            if live_authorized
            else "offline_fixture_or_replay_only"
        ),
        live_provider_calls_authorized=live_authorized,
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
                revision=revision,
                transport="fixture",
            ),
            prompt_template_hash=config.judge_template_sha256,
            rendered_prompt=f"strict blinded {orientation} prompt",
            decoding=fable_endpoint.decoding,
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
                judge_slot="judge_B",
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
        fable,
        "_load_prepared_batch",
        lambda _root: (spec, manifest, tuple(dispatches), None),
    )
    monkeypatch.setattr(
        fable,
        "_verify_dispatch_request",
        lambda _root, dispatch, _spec: by_cell[dispatch.dispatch_cell_id],
    )
    monkeypatch.setattr(
        fable,
        "_check_binary",
        lambda config: Path(config.claude_binary_path),
    )
    monkeypatch.setattr(fable, "_utcnow", lambda: NOW)
    loaded = load_claude_fable_judge_config(CONFIG)
    values: dict[str, object] = {
        "schema_version": 1,
        "batch_id": manifest.batch_id,
        "config_sha256": loaded.sha256,
        "judge_slot": "judge_B",
        "shard_id": "smoke-000",
        "offset_pairs": 0,
        "limit_pairs": 1,
        "authorization_nonce_sha256": sha256_hex(AUTH_NONCE),
        "approved_at": (NOW - datetime.timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (NOW + datetime.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "approved_by": "offline-test-reviewer",
        "public_external_execution_authorized": True,
        "private_source_content_authorized": False,
        "semantic_labels_created": False,
        "gate_credit_claimed": False,
    }
    authorization = ClaudeFableLiveAuthorization.model_validate(
        {
            **values,
            "authorization_id": make_id("lf022_fable_live_authorization", values),
        }
    )
    authorization_path = tmp_path / "fable-authorization.json"
    authorization_path.write_bytes(
        canonical_json_bytes(authorization.model_dump(mode="json")) + b"\n"
    )
    return batch_root, batch_root / "raw" / "judge_B", requests


def test_local_cli_and_frozen_config_match() -> None:
    loaded = load_claude_fable_judge_config(CONFIG)
    assert loaded.config.model == "claude-fable-5"
    assert loaded.config.effort == "max"
    assert loaded.config.endpoint_revision == (
        "provider-deployment-snapshot:"
        "f913bc9dc00dc187c2f5429e7deddfe604372422dcff7193e881dc7ada8e3ce4"
    )
    binary = Path("/localhome/milikic/.local/share/claude/versions/2.1.228")
    if binary.is_file():
        assert hash_file(binary) == loaded.config.claude_binary_sha256


def test_wrapper_requires_structured_output_and_strict_judge_schema() -> None:
    parsed = fable._parse_wrapper(_capture().stdout)
    assert parsed.same_claim_answer == "not_same_claim"
    with pytest.raises(ClaudeFableJudgeError, match="type=result"):
        fable._parse_wrapper(canonical_json_bytes({"result": json.dumps(_response())}))
    incoherent = _response()
    incoherent["relation"] = "equivalent"
    with pytest.raises(ClaudeFableJudgeError, match="invalid structured judge response"):
        fable._parse_wrapper(_capture(response=incoherent).stdout)


def test_one_pair_executes_both_orders_and_replays_without_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, requests = _fixture(monkeypatch, tmp_path)
    output = tmp_path / "fable"
    executor = _Executor(
        [_capture(), _capture(response=_response(equivalent=True))],
        required_execution_marker=batch_root / "execution_started.json",
    )
    result = run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=output,
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=True,
        executor=executor,
        now=NOW,
    )
    assert result.manifest.selected_pair_count == 1
    assert result.manifest.selected_cell_count == 2
    assert result.manifest.invoked_cell_count == 2
    assert result.manifest.completed_cell_count == 2
    assert len(executor.calls) == 2
    assert {
        item.task.orientation
        for item in fable._selected_cells(
            fable._load_prepared_batch(batch_root)[2],  # type: ignore[attr-defined]
            slot="judge_B",
            offset_pairs=0,
            limit_pairs=1,
        )
    } == {"AB", "BA"}
    for argv, prompt, cwd, env in executor.calls:
        assert (argv[argv.index("--model")], argv[argv.index("--model") + 1]) == (
            "--model",
            "claude-fable-5",
        )
        assert argv[argv.index("--effort") + 1] == "max"
        assert argv[argv.index("--tools") + 1] == ""
        assert prompt.startswith(b"strict blinded")
        assert cwd != Path.cwd()
        assert "RCP_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "HF_TOKEN" not in env
    for request in requests:
        load_provider_raw_response(provider_raw_response_path(raw_root, request), request=request)

    replay_executor = _Executor([])
    replayed = run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=output,
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=False,
        executor=replay_executor,
        now=NOW,
    )
    assert replayed.manifest.invoked_cell_count == 0
    assert replayed.manifest.reused_cell_count == 2
    assert not replay_executor.calls


def test_failed_first_attempt_is_preserved_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, requests = _fixture(monkeypatch, tmp_path)
    bad = ClaudeCliCapture(status="completed", exit_code=0, stdout=b"not-json", stderr=b"bad")
    executor = _Executor([bad, _capture(), _capture()])
    result = run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=tmp_path / "fable",
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=True,
        executor=executor,
        now=NOW,
    )
    assert result.manifest.invoked_cell_count == 2
    assert result.manifest.process_attempt_count == 3
    assert result.manifest.completed_cell_count == 2
    failed_dir = fable._attempt_dir(tmp_path / "fable", requests[0].request_hash, 0)
    assert (failed_dir / "stdout.json").read_bytes() == b"not-json"
    failed_terminal = fable._load_terminal(failed_dir / "terminal.json")
    assert failed_terminal.status == "wrapper_parse_failed"
    assert failed_terminal.attempt_index == 0
    successful = next(
        item
        for item in result.terminals
        if item.dispatch_cell_id == failed_terminal.dispatch_cell_id
    )
    assert successful.status == "completed"
    assert successful.attempt_index == 1
    assert successful.provider_attempt_id != failed_terminal.provider_attempt_id
    assert (
        result.manifest.successful_attempt_ids_by_dispatch[successful.dispatch_cell_id]
        == successful.provider_attempt_id
    )
    retry_request = fable._request_for_attempt(requests[0], 1)
    load_provider_raw_response(
        provider_raw_response_path(tmp_path / "fable/provider_raw_attempts", retry_request),
        request=retry_request,
    )
    bridged = load_provider_raw_response(
        provider_raw_response_path(raw_root, requests[0]),
        request=requests[0],
    )
    assert bridged.status == "success"


def test_fail_closed_limits_privacy_and_endpoint_pins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, _ = _fixture(monkeypatch, tmp_path)
    with pytest.raises(ClaudeFableJudgeError, match="offline replay is incomplete"):
        run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=tmp_path / "fable",
            config_path=CONFIG,
            judge_slot="judge_B",
            offset_pairs=0,
            limit_pairs=1,
            execute_external=False,
        )
    with pytest.raises(ClaudeFableJudgeError, match="shard exceeds"):
        run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=tmp_path / "fable",
            config_path=CONFIG,
            judge_slot="judge_B",
            offset_pairs=1,
            limit_pairs=1,
            execute_external=False,
        )
    with pytest.raises(ClaudeFableJudgeError, match=r"within 1\.\.64"):
        run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=tmp_path / "fable",
            config_path=CONFIG,
            judge_slot="judge_B",
            offset_pairs=0,
            limit_pairs=65,
            execute_external=True,
        )


def test_live_execution_requires_authorized_prepared_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, _ = _fixture(
        monkeypatch,
        tmp_path,
        live_authorized=False,
    )
    with pytest.raises(ClaudeFableJudgeError, match="does not explicitly authorize"):
        run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=tmp_path / "fable",
            config_path=CONFIG,
            judge_slot="judge_B",
            offset_pairs=0,
            limit_pairs=1,
            execute_external=True,
            executor=_Executor([_capture(), _capture()]),
            now=NOW,
        )


def test_exhausted_failures_replay_without_reinvocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, _ = _fixture(monkeypatch, tmp_path)
    failed = ClaudeCliCapture(
        status="completed",
        exit_code=1,
        stdout=b"",
        stderr=b"provider failed",
    )
    output = tmp_path / "fable"
    first = run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=output,
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=True,
        executor=_Executor([failed, failed, failed, failed]),
        now=NOW,
    )
    assert first.manifest.completed_cell_count == 0
    assert {item.attempt_index for item in first.terminals} == {1}
    replay_executor = _Executor([])
    replay = run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=output,
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=False,
        executor=replay_executor,
        now=NOW + datetime.timedelta(seconds=1),
    )
    assert replay.terminals == first.terminals
    assert not replay_executor.calls


def test_partial_attempt_fails_closed_without_recall_or_advance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, requests = _fixture(monkeypatch, tmp_path)
    output = tmp_path / "fable"
    partial_dir = fable._attempt_dir(output, requests[0].request_hash, 0)
    partial_dir.mkdir(parents=True)
    (partial_dir / "stdout.json").write_bytes(b"partial capture")
    executor = _Executor([])
    with pytest.raises(fable.ClaudeFablePartialAttemptError, match="refusing recall"):
        run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=output,
            config_path=CONFIG,
            judge_slot="judge_B",
            offset_pairs=0,
            limit_pairs=1,
            execute_external=True,
            executor=executor,
            now=NOW,
        )
    assert executor.calls == []
    assert not (partial_dir / "terminal.json").exists()


def test_completed_receipt_recovery_reuses_attempt_zero_without_duplicate_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, requests = _fixture(monkeypatch, tmp_path)
    output = tmp_path / "fable"
    original_materialize = fable._materialize_terminal_from_receipt

    def crash_after_receipt(**_kwargs: object) -> fable.ClaudeFableAttemptTerminal:
        raise RuntimeError("injected crash after durable process receipt")

    monkeypatch.setattr(fable, "_materialize_terminal_from_receipt", crash_after_receipt)
    first_executor = _Executor([_capture()])
    with pytest.raises(RuntimeError, match="injected crash"):
        run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=output,
            config_path=CONFIG,
            judge_slot="judge_B",
            offset_pairs=0,
            limit_pairs=1,
            execute_external=True,
            executor=first_executor,
            now=NOW,
        )
    attempt_zero = fable._attempt_dir(output, requests[0].request_hash, 0)
    assert len(first_executor.calls) == 1
    assert (attempt_zero / "process_receipt.json").is_file()
    assert not (attempt_zero / "terminal.json").exists()

    monkeypatch.setattr(
        fable,
        "_materialize_terminal_from_receipt",
        original_materialize,
    )
    resumed_executor = _Executor([_capture()])
    result = run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=output,
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=True,
        executor=resumed_executor,
        now=NOW,
    )

    recovered = next(
        terminal
        for terminal in result.terminals
        if terminal.dispatch_cell_id
        == fable._load_prepared_batch(batch_root)[2][0].dispatch_cell_id  # type: ignore[attr-defined]
    )
    assert recovered.status == "completed"
    assert recovered.attempt_index == 0
    assert len(resumed_executor.calls) == 1
    assert result.manifest.process_attempt_count == 1
    assert result.manifest.invoked_cell_count == 1
    assert result.manifest.reused_cell_count == 1
    assert not fable._attempt_dir(output, requests[0].request_hash, 1).exists()


def test_replay_verifies_argv_and_request_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, requests = _fixture(monkeypatch, tmp_path)
    output = tmp_path / "fable"
    run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=output,
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=True,
        executor=_Executor([_capture(), _capture()]),
        now=NOW,
    )
    attempt_dir = fable._attempt_dir(output, requests[0].request_hash, 0)
    (attempt_dir / "argv.json").write_bytes(b"[]\n")
    with pytest.raises(ClaudeFableJudgeError, match="argv differs"):
        run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=output,
            config_path=CONFIG,
            judge_slot="judge_B",
            offset_pairs=0,
            limit_pairs=1,
            execute_external=False,
            executor=_Executor([]),
            now=NOW,
        )


def test_interruption_is_persisted_and_propagated_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, requests = _fixture(monkeypatch, tmp_path)
    output = tmp_path / "fable"
    executor = _Executor(
        [
            ClaudeCliCapture(
                status="interrupted",
                exit_code=-15,
                stdout=b"partial",
                stderr=b"interrupted",
            )
        ]
    )
    with pytest.raises(KeyboardInterrupt):
        run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=output,
            config_path=CONFIG,
            judge_slot="judge_B",
            offset_pairs=0,
            limit_pairs=1,
            execute_external=True,
            executor=executor,
            now=NOW,
        )
    terminal = fable._load_terminal(
        fable._attempt_dir(output, requests[0].request_hash, 0) / "terminal.json"
    )
    assert terminal.status == "interrupted"
    assert len(executor.calls) == 1


def test_child_environment_is_allowlisted() -> None:
    env = fable._child_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "CLAUDE_CODE_OAUTH_TOKEN": "claude-auth",
            "ANTHROPIC_API_KEY": "anthropic-auth",
            "RCP_API_KEY": "rcp-secret",
            "OPENAI_API_KEY": "openai-secret",
            "HF_TOKEN": "hf-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
        }
    )
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-auth",
        "ANTHROPIC_API_KEY": "anthropic-auth",
    }


def test_echoed_auth_secret_is_redacted_before_any_durable_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, requests = _fixture(monkeypatch, tmp_path)
    secret = "anthropic-test-secret-123456789"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    leaked = _capture().model_copy(update={"stderr": f"auth={secret}\n".encode()})
    output = tmp_path / "fable"
    result = run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=output,
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=True,
        executor=_Executor([leaked, _capture(), _capture()]),
        now=NOW,
    )
    assert result.manifest.completed_cell_count == 2
    leaked_terminal = fable._load_terminal(
        fable._attempt_dir(output, requests[0].request_hash, 0) / "terminal.json"
    )
    assert leaked_terminal.status == "secret_redacted"
    assert leaked_terminal.redaction_count >= 1
    for path in (*output.rglob("*"), *raw_root.rglob("*")):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


def test_live_execution_requires_exact_authorization_artifact_and_nonce(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, _ = _fixture(monkeypatch, tmp_path)
    with pytest.raises(fable.ClaudeFableAuthorizationError, match="artifact and nonce"):
        _run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=tmp_path / "fable",
            config_path=CONFIG,
            judge_slot="judge_B",
            shard_id="smoke-000",
            offset_pairs=0,
            limit_pairs=1,
            run_nonce=RUN_NONCE,
            execute_external=True,
            executor=_Executor([]),
        )


def test_run_identity_changes_with_created_at_without_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, _ = _fixture(monkeypatch, tmp_path)
    output = tmp_path / "fable"
    first = run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=output,
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=True,
        executor=_Executor([_capture(), _capture()]),
        now=NOW,
    )
    second = run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=output,
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=False,
        executor=_Executor([]),
        now=NOW + datetime.timedelta(seconds=1),
    )
    assert first.manifest.run_id != second.manifest.run_id
    assert first.manifest_path.is_file()
    assert second.manifest_path.is_file()


def test_process_lock_and_explicit_offset_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, _ = _fixture(monkeypatch, tmp_path)
    output = tmp_path / "fable"
    with (
        fable._process_lock(output),
        pytest.raises(ClaudeFableJudgeError, match="owns the output lock"),
    ):
        run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=output,
            config_path=CONFIG,
            judge_slot="judge_B",
            offset_pairs=0,
            limit_pairs=1,
            execute_external=False,
        )


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
            judge_family_id="anthropic_fable",
            judge_slot="judge_B",
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
    selected = fable._selected_cells(dispatches, slot="judge_B", offset_pairs=0, limit_pairs=1)
    assert [item.orientation for item in selected] == ["BA", "AB"]


def test_bounded_capture_becomes_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, _ = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(fable, "_MAX_CAPTURE_BYTES", 4)
    oversized = ClaudeCliCapture(
        status="completed",
        exit_code=0,
        stdout=b"12345",
        stderr=b"",
    )
    result = run_claude_fable_weak_cells(
        batch_root=batch_root,
        raw_response_root=raw_root,
        output_root=tmp_path / "fable",
        config_path=CONFIG,
        judge_slot="judge_B",
        offset_pairs=0,
        limit_pairs=1,
        execute_external=True,
        executor=_Executor([oversized, oversized, oversized, oversized]),
        now=NOW,
    )
    assert {item.status for item in result.terminals} == {"capture_too_large"}


def test_symlinked_output_root_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_root, raw_root, _ = _fixture(monkeypatch, tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ClaudeFableJudgeError, match="symlink"):
        run_claude_fable_weak_cells(
            batch_root=batch_root,
            raw_response_root=raw_root,
            output_root=link,
            config_path=CONFIG,
            judge_slot="judge_B",
            offset_pairs=0,
            limit_pairs=1,
            execute_external=True,
        )
