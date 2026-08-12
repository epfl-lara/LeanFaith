"""Focused offline tests for the one-item public Codex LF-022 proposer."""

from __future__ import annotations

import datetime
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation import lf022_codex_proposer as proposer_module
from leanfaith.generation.lf022_batch import (
    LF022BatchRouteFreezeRequest,
    LF022BatchRouteManifest,
    LF022BatchTaskBinding,
    LF022PublicBatchManifest,
    make_lf022_batch_freeze_request,
)
from leanfaith.generation.lf022_codex_proposer import (
    CodexProcessCapture,
    LF022CodexProposerError,
    _build_argv,
    _validate_codex_stdout,
    load_lf022_codex_proposer_config,
    run_lf022_codex_proposer,
)
from leanfaith.generation.lf022_execution import LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.schemas.enums import QualityTier, ValidationStatus
from leanfaith.schemas.ids import make_id
from tests.unit.test_lf022_executor import REPOSITORY_ROOT, _fixture

NOW = datetime.datetime(2026, 8, 12, 12, 0, tzinfo=datetime.UTC)


def _write(root: Path, relative: str, payload: bytes) -> LF022ArtifactBinding:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return LF022ArtifactBinding(path=relative, sha256=hash_file(path))


def _write_record(root: Path, relative: str, value: object) -> LF022ArtifactBinding:
    return _write(root, relative, canonical_json_bytes(value))


def _batch_fixture(root: Path) -> tuple[Path, str]:
    admission, task = _fixture(root)
    task_binding = _write_record(
        root,
        "data/codex_source_batch/tasks/kimi/task.json",
        task.model_dump(mode="json"),
    )
    admission_binding = _write_record(
        root,
        "data/codex_source_batch/admission.json",
        admission.model_dump(mode="json"),
    )
    route_request = LF022BatchRouteFreezeRequest(
        proposer_family_id="moonshot_kimi_k2",
        public_pool_audit_id=admission.public_pool_audit_id,
        allocation_plan_id=admission.allocation_plan_id,
        execution_artifacts=admission.artifacts,
        route=admission.route,
        retry_policy=admission.retry_policy,
        code_tree_hash=admission.code_tree_hash,
        allocation_task_ids=(task.allocation_task.task_id,),
        proposal_count=1,
        requested_relations=task.requested_relations,
    )
    freeze_request = make_lf022_batch_freeze_request(
        batch_directory="data/codex_source_batch",
        executor_output_root=LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
        routes=(route_request,),
    )
    freeze_request_binding = _write_record(
        root,
        "data/codex_source_batch/request.json",
        freeze_request.model_dump(mode="json"),
    )
    route = LF022BatchRouteManifest(
        proposer_family_id="moonshot_kimi_k2",
        model_id=admission.route.model_id,
        execution_scope="public_provisional_g_open",
        qualification_state="production_route_reviewed",
        admission_id=admission.admission_id,
        admission=admission_binding,
        public_pool_audit_id=admission.public_pool_audit_id,
        allocation_plan_id=admission.allocation_plan_id,
        tasks=(
            LF022BatchTaskBinding(
                allocation_task_id=task.allocation_task.task_id,
                execution_task_id=task.execution_task_id,
                task=task_binding,
            ),
        ),
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "frozen_offline_ready",
        "freeze_request": freeze_request_binding.model_dump(mode="json"),
        "freeze_request_id": freeze_request.request_id,
        "batch_directory": "data/codex_source_batch",
        "executor_output_root": LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
        "journal_directory": "data/codex_source_batch/journal",
        "routes": [route.model_dump(mode="json")],
        "total_task_count": 1,
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "optional_natural_language_forbidden": True,
        "execute_requires_explicit_flag": True,
        "outputs_provisional_only": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    manifest = LF022PublicBatchManifest.model_validate(
        {**payload, "batch_id": make_id("lf022_public_batch", payload)}
    )
    manifest_path = root / "data/codex_source_batch/batch_manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest.model_dump(mode="json")))
    return manifest_path, task.execution_task_id


def _config_fixture(root: Path) -> Path:
    bindings: dict[str, LF022ArtifactBinding] = {}
    for key, relative in (
        ("provider_catalog", "configs/generation/lf022_codex_catalog_snapshot_v1.json"),
        ("prompt_template", "prompts/proposers/lean_variant_v2.txt"),
        ("output_schema", "configs/generation/lf022_codex_proposer_output_v1.json"),
    ):
        bindings[key] = _write(root, relative, (REPOSITORY_ROOT / relative).read_bytes())
    config = {
        "schema_version": 1,
        "config_id": "lf022_codex_proposer_smoke_v1",
        "status": "one_public_task_smoke_only",
        "provider": "openai_codex_exec",
        "provider_slot": "lf022_codex_proposer_terra_v1",
        "model_family": "openai_codex",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "xhigh",
        "codex_cli_version": "codex-cli fixture",
        "codex_binary_sha256": "a" * 64,
        **{key: value.model_dump(mode="json") for key, value in bindings.items()},
        "timeout_seconds": 30,
        "termination_grace_seconds": 1,
        "maximum_task_count": 1,
        "maximum_concurrency": 1,
        "execute_requires_explicit_flag": True,
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "own_validator_allowed": False,
        "outputs_provisional_only": True,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    path = root / "configs/generation/codex_proposer_test.json"
    path.write_bytes(canonical_json_bytes(config))
    return path


class _SuccessfulExecutor:
    calls = 0

    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> CodexProcessCapture:
        del argv, prompt, cwd, final_message_path, timeout_seconds, termination_grace_seconds
        self.calls += 1
        final = canonical_json_bytes(
            {
                "variants": [
                    {
                        "candidate_lean": ("theorem public_candidate (n : Nat) : n + 1 = n"),
                        "intended_relation": "near_miss",
                        "intended_error_types": ["E21"],
                        "edit_summary": "Changed the right-hand side by one.",
                        "confidence": 0.7,
                        "assumptions": [],
                        "potential_ambiguity": None,
                    }
                ]
            }
        )
        events = (
            {"type": "thread.started", "thread_id": "fixture"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": final.decode("utf-8")},
            },
            {"type": "turn.completed", "usage": {}},
        )
        stdout = b"".join(canonical_json_bytes(event) + b"\n" for event in events)
        return CodexProcessCapture(
            status="completed",
            exit_code=0,
            stdout=stdout,
            stderr=b"",
            final_message=final,
            started_at=NOW,
            completed_at=NOW,
        )


class _FailedExecutor:
    def execute(
        self,
        *,
        argv: Sequence[str],
        prompt: bytes,
        cwd: Path,
        final_message_path: Path,
        timeout_seconds: int,
        termination_grace_seconds: int,
    ) -> CodexProcessCapture:
        del argv, prompt, cwd, final_message_path, timeout_seconds, termination_grace_seconds
        return CodexProcessCapture(
            status="completed",
            exit_code=17,
            stdout=b"",
            stderr=b"fixture failure",
            final_message=None,
            started_at=NOW,
            completed_at=NOW,
        )


def test_codex_proposer_creates_only_unvalidated_provisional_variant_and_replays(
    tmp_path: Path,
) -> None:
    manifest_path, task_id = _batch_fixture(tmp_path)
    config_path = _config_fixture(tmp_path)
    output_root = tmp_path / "data/codex_proposer_output"
    executor = _SuccessfulExecutor()

    first = run_lf022_codex_proposer(
        repo_root=tmp_path,
        config_path=config_path,
        batch_manifest_path=manifest_path,
        execution_task_ids=(task_id,),
        output_root=output_root,
        execute_public_provisional=True,
        executor=executor,
        verify_cli_pin=False,
    )
    assert executor.calls == 1
    assert first.invoked_count == 1
    assert first.reused_count == 0
    assert first.terminals[0].status == "provisional_variants_created"
    assert first.manifest is not None
    assert first.manifest.semantic_labels_created is False
    assert first.manifest.supervision_eligible is False
    variants_path = tmp_path / str(first.terminals[0].variants_artifact)
    variant = json.loads(variants_path.read_text(encoding="utf-8"))
    assert variant["quality_tier"] == QualityTier.PROVISIONAL.value
    assert variant["validation_status"] == ValidationStatus.UNVALIDATED.value
    assert variant["generator_id"] == "gpt-5.6-terra"
    assert variant["metadata"]["proposer_family"] == "openai_codex"

    second = run_lf022_codex_proposer(
        repo_root=tmp_path,
        config_path=config_path,
        batch_manifest_path=manifest_path,
        execution_task_ids=(task_id,),
        output_root=output_root,
        execute_public_provisional=True,
        executor=executor,
        verify_cli_pin=False,
    )
    assert executor.calls == 1
    assert second.invoked_count == 0
    assert second.reused_count == 1
    assert second.terminals == first.terminals


def test_codex_proposer_preserves_and_recovers_an_interrupted_nonterminal_attempt(
    tmp_path: Path,
) -> None:
    manifest_path, task_id = _batch_fixture(tmp_path)
    config_path = _config_fixture(tmp_path)
    output_root = tmp_path / "data/codex_proposer_output"
    prepared = run_lf022_codex_proposer(
        repo_root=tmp_path,
        config_path=config_path,
        batch_manifest_path=manifest_path,
        execution_task_ids=(task_id,),
        output_root=output_root,
        execute_public_provisional=False,
        verify_cli_pin=False,
    ).prepared[0]
    stale_workspace = prepared.item_directory / "workspace"
    stale_workspace.mkdir(parents=True)
    (stale_workspace / "partial.txt").write_text("preserve me", encoding="utf-8")

    result = run_lf022_codex_proposer(
        repo_root=tmp_path,
        config_path=config_path,
        batch_manifest_path=manifest_path,
        execution_task_ids=(task_id,),
        output_root=output_root,
        execute_public_provisional=True,
        executor=_SuccessfulExecutor(),
        verify_cli_pin=False,
    )

    assert result.terminals[0].status == "provisional_variants_created"
    archive = prepared.item_directory / "interrupted_attempts/attempt-000001"
    assert (archive / "workspace/partial.txt").read_text(encoding="utf-8") == "preserve me"
    recovery = json.loads((archive / "recovery.json").read_text(encoding="utf-8"))
    assert recovery == {
        "reason": "terminal_missing",
        "recovered_entries": ["workspace"],
        "schema_version": 1,
    }


def test_codex_proposer_rejects_nested_items_symlink_before_external_call(
    tmp_path: Path,
) -> None:
    manifest_path, task_id = _batch_fixture(tmp_path)
    config_path = _config_fixture(tmp_path)
    output_root = tmp_path / "data/codex_proposer_output"
    output_root.mkdir(parents=True)
    escaped = tmp_path / "data/escaped-items"
    escaped.mkdir(parents=True)
    (output_root / "items").symlink_to(escaped, target_is_directory=True)
    executor = _SuccessfulExecutor()

    with pytest.raises(LF022CodexProposerError, match="symlink entry"):
        run_lf022_codex_proposer(
            repo_root=tmp_path,
            config_path=config_path,
            batch_manifest_path=manifest_path,
            execution_task_ids=(task_id,),
            output_root=output_root,
            execute_public_provisional=True,
            executor=executor,
            verify_cli_pin=False,
        )

    assert executor.calls == 0
    assert tuple(escaped.iterdir()) == ()


def test_codex_proposer_rejects_output_ancestor_symlink_before_external_call(
    tmp_path: Path,
) -> None:
    manifest_path, task_id = _batch_fixture(tmp_path)
    config_path = _config_fixture(tmp_path)
    real_parent = tmp_path / "real-output-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-output-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    executor = _SuccessfulExecutor()

    with pytest.raises(LF022CodexProposerError, match="symlink component"):
        run_lf022_codex_proposer(
            repo_root=tmp_path,
            config_path=config_path,
            batch_manifest_path=manifest_path,
            execution_task_ids=(task_id,),
            output_root=linked_parent / "run",
            execute_public_provisional=True,
            executor=executor,
            verify_cli_pin=False,
        )

    assert executor.calls == 0
    assert tuple(real_parent.iterdir()) == ()


def test_codex_cli_pin_is_checked_for_each_new_call_but_not_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, task_id = _batch_fixture(tmp_path)
    config_path = _config_fixture(tmp_path)
    checks: list[str] = []
    monkeypatch.setattr(
        proposer_module,
        "verify_codex_proposer_cli_pin",
        lambda config: checks.append(config.model),
    )
    executor = _SuccessfulExecutor()
    kwargs = {
        "repo_root": tmp_path,
        "config_path": config_path,
        "batch_manifest_path": manifest_path,
        "execution_task_ids": (task_id,),
        "execute_public_provisional": True,
        "executor": executor,
        "verify_cli_pin": True,
    }
    run_lf022_codex_proposer(
        **kwargs,
        output_root=tmp_path / "data/first",
    )
    run_lf022_codex_proposer(
        **kwargs,
        output_root=tmp_path / "data/second",
    )
    run_lf022_codex_proposer(
        **kwargs,
        output_root=tmp_path / "data/first",
    )

    assert checks == ["gpt-5.6-terra", "gpt-5.6-terra"]


def test_codex_proposer_rejects_replay_artifact_drift(tmp_path: Path) -> None:
    manifest_path, task_id = _batch_fixture(tmp_path)
    config_path = _config_fixture(tmp_path)
    output_root = tmp_path / "data/codex_proposer_output"
    executor = _SuccessfulExecutor()
    result = run_lf022_codex_proposer(
        repo_root=tmp_path,
        config_path=config_path,
        batch_manifest_path=manifest_path,
        execution_task_ids=(task_id,),
        output_root=output_root,
        execute_public_provisional=True,
        executor=executor,
        verify_cli_pin=False,
    )
    stdout = tmp_path / result.terminals[0].stdout_artifact
    stdout.write_bytes(stdout.read_bytes() + b"tamper")

    with pytest.raises(LF022CodexProposerError, match="stdout hash drifted"):
        run_lf022_codex_proposer(
            repo_root=tmp_path,
            config_path=config_path,
            batch_manifest_path=manifest_path,
            execution_task_ids=(task_id,),
            output_root=output_root,
            execute_public_provisional=True,
            executor=executor,
            verify_cli_pin=False,
        )


def test_codex_proposer_persists_process_failure_without_variants(tmp_path: Path) -> None:
    manifest_path, task_id = _batch_fixture(tmp_path)
    result = run_lf022_codex_proposer(
        repo_root=tmp_path,
        config_path=_config_fixture(tmp_path),
        batch_manifest_path=manifest_path,
        execution_task_ids=(task_id,),
        output_root=tmp_path / "data/codex_failed",
        execute_public_provisional=True,
        executor=_FailedExecutor(),
        verify_cli_pin=False,
    )
    terminal = result.terminals[0]
    assert terminal.status == "process_failed"
    assert terminal.error_code == "codex_exit_17"
    assert terminal.provisional_variant_count == 0
    assert terminal.variants_artifact is None


def test_codex_proposer_rejects_schema_hash_drift(tmp_path: Path) -> None:
    manifest_path, task_id = _batch_fixture(tmp_path)
    config_path = _config_fixture(tmp_path)
    schema = tmp_path / "configs/generation/lf022_codex_proposer_output_v1.json"
    schema.write_bytes(schema.read_bytes() + b"\n")
    with pytest.raises(LF022CodexProposerError, match="output schema hash differs"):
        run_lf022_codex_proposer(
            repo_root=tmp_path,
            config_path=config_path,
            batch_manifest_path=manifest_path,
            execution_task_ids=(task_id,),
            output_root=tmp_path / "data/codex_schema_drift",
        )


def test_codex_proposer_rejects_private_marker_in_bound_task(tmp_path: Path) -> None:
    manifest_path, task_id = _batch_fixture(tmp_path)
    manifest = LF022PublicBatchManifest.model_validate_json(manifest_path.read_bytes())
    task_path = tmp_path / manifest.routes[0].tasks[0].task.path
    task_path.write_bytes(task_path.read_bytes().replace(b'"mathlib"', b'"sft_classic"'))
    task_binding = (
        manifest.routes[0]
        .tasks[0]
        .model_copy(
            update={
                "task": LF022ArtifactBinding(
                    path=manifest.routes[0].tasks[0].task.path,
                    sha256=hash_file(task_path),
                )
            }
        )
    )
    route = manifest.routes[0].model_copy(update={"tasks": (task_binding,)})
    payload = manifest.model_dump(mode="json", exclude={"batch_id"})
    payload["routes"] = [route.model_dump(mode="json")]
    rebound = LF022PublicBatchManifest.model_validate(
        {**payload, "batch_id": make_id("lf022_public_batch", payload)}
    )
    manifest_path.write_bytes(canonical_json_bytes(rebound.model_dump(mode="json")))
    with pytest.raises(LF022CodexProposerError, match="invalid frozen LF-022 source task"):
        run_lf022_codex_proposer(
            repo_root=tmp_path,
            config_path=_config_fixture(tmp_path),
            batch_manifest_path=manifest_path,
            execution_task_ids=(task_id,),
            output_root=tmp_path / "data/codex_private_rejected",
        )


def test_codex_proposer_cli_requires_explicit_external_flag() -> None:
    result = CliRunner().invoke(app, ["propose-lf022-codex", "--help"])
    assert result.exit_code == 0
    assert "--execute-public-provisional" in result.stdout


def test_codex_proposer_launch_disables_shell_tool(tmp_path: Path) -> None:
    loaded = load_lf022_codex_proposer_config(_config_fixture(tmp_path), repo_root=tmp_path)
    argv = _build_argv(
        loaded=loaded,
        output_schema_path=tmp_path / "schema.json",
        final_message_path=tmp_path / "final.json",
    )
    assert argv[argv.index("--disable") : argv.index("--disable") + 2] == (
        "--disable",
        "shell_tool",
    )


def test_codex_stdout_rejects_tool_execution_even_with_final_message() -> None:
    final = b'{"variants":[]}'
    events = (
        {"type": "thread.started", "thread_id": "fixture"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "pwd"},
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": final.decode("utf-8")},
        },
        {"type": "turn.completed", "usage": {}},
    )
    stdout = b"".join(canonical_json_bytes(event) + b"\n" for event in events)
    assert _validate_codex_stdout(stdout, final) == (
        "stdout rejected tool/item type 'command_execution'"
    )


def test_codex_stdout_rejects_duplicate_keys() -> None:
    stdout = b'{"type":"thread.started","type":"thread.started","thread_id":"fixture"}\n'
    assert "duplicate key" in str(_validate_codex_stdout(stdout, b"{}"))
