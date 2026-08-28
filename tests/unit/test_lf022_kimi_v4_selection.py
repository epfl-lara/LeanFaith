"""Adversarial tests for offline-only Kimi-v4 challenge selection."""

from __future__ import annotations

import datetime
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.generation import lf022_kimi_v4_selection as selection_module
from leanfaith.generation.lf022_batch import (
    LF022BatchRouteManifest,
    LF022BatchRunReport,
    LF022BatchTaskBinding,
    LF022PublicBatchManifest,
    VerifiedLF022BatchTask,
)
from leanfaith.generation.lf022_execution import (
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    LF022RCPDecodingContract,
    LF022RCPRouteBinding,
)
from leanfaith.generation.lf022_executor import (
    LF022ExecutionAttemptRecord,
    LF022ExecutionTerminalRecord,
    LF022WireResponseMetadata,
)
from leanfaith.generation.lf022_historical_replay import (
    LF022HistoricalModuleBinding,
    LF022HistoricalReplayResult,
    LF022HistoricalTerminalBinding,
)
from leanfaith.generation.lf022_kimi_v4_selection import (
    LF022_KIMI_V4_SELECTION_ROOT,
    LF022KimiV4SelectionError,
    freeze_lf022_kimi_v4_challenge_selection,
    verify_lf022_kimi_v4_challenge_selection,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.generation.llm_variants import PublicLeanVariantSource
from leanfaith.schemas.enums import IntendedRelation
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.manifest import CodeState

ROOT = Path(__file__).resolve().parents[2]
MODEL = "moonshotai/Kimi-K2.7-Code"
NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)


def _write(root: Path, relative: str, payload: bytes) -> LF022ArtifactBinding:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return LF022ArtifactBinding(path=relative, sha256=hash_file(path))


def _completion_body(*, finish_reason: str, content: str, index: int) -> bytes:
    return canonical_json_bytes(
        {
            "id": f"chatcmpl-selection-{index}",
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": "fixture reasoning",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 16384,
                "total_tokens": 16484,
            },
        }
    )


def _variant_content(candidate: str) -> str:
    return json.dumps(
        {
            "variants": [
                {
                    "candidate_lean": candidate,
                    "intended_relation": "near_miss",
                    "intended_error_types": [],
                    "edit_summary": "fixture mutation",
                    "confidence": 0.5,
                    "assumptions": [],
                    "potential_ambiguity": None,
                }
            ]
        }
    )


def _task_terminal(
    root: Path,
    *,
    admission: LF022GOpenExecutionAdmission,
    task: LF022GOpenExecutionTask,
    index: int,
    category: str,
    fake_budget_raw: bool,
) -> tuple[LF022ExecutionTerminalRecord, LF022ArtifactBinding]:
    task_id = task.execution_task_id
    digest = task_id.split(":", 1)[1]
    parent_relative = Path("data/lf022_execution/tasks") / digest[:2] / digest
    parent = root / parent_relative
    parent.mkdir(parents=True, exist_ok=True)

    if category == "budget":
        finish_reason = "stop" if fake_budget_raw else "length"
        content = ""
        attempt_status = "invalid_response"
        attempt_error = "output_budget_exhausted"
        terminal_status = "provider_exhausted"
        # This legacy string is deliberately not a sufficient category signal.
        terminal_error = "empty_response"
    elif category == "proof":
        finish_reason = "stop"
        content = _variant_content(f"theorem proof_{index} : True := True.intro")
        attempt_status = "proposer_parse_failed"
        attempt_error = "proof_bearing_candidate"
        terminal_status = "proposer_parse_failed"
        terminal_error = "proof_bearing_candidate"
    else:
        finish_reason = "stop"
        content = _variant_content(f"theorem success_{index} : ({index} : Nat) = {index}")
        attempt_status = "response_parsed"
        attempt_error = None
        terminal_status = "provisional_variants_created"
        terminal_error = None

    request = _write(
        root, (parent_relative / "attempts/0000/request.json").as_posix(), b"request\n"
    )
    wire_request = _write(
        root,
        (parent_relative / "attempts/0000/wire_request.json").as_posix(),
        b"wire request\n",
    )
    provider_raw = _write(
        root,
        (parent_relative / "attempts/0000/provider_raw.json").as_posix(),
        b"provider raw\n",
    )
    body = _write(
        root,
        (parent_relative / "attempts/0000/wire_response.body").as_posix(),
        _completion_body(finish_reason=finish_reason, content=content, index=index),
    )
    metadata = LF022WireResponseMetadata(
        status_code=200,
        headers={},
        body_sha256=body.sha256,
    )
    metadata_binding = _write(
        root,
        (parent_relative / "attempts/0000/wire_response.json").as_posix(),
        canonical_json_bytes(metadata.model_dump(mode="json")) + b"\n",
    )
    attempt = LF022ExecutionAttemptRecord(
        execution_task_id=task_id,
        provider_request_hash=f"{index + 1:064x}",
        provider_attempt_id=f"provider-attempt:{index + 1:064x}",
        attempt_index=0,
        request_artifact=request.path,
        request_sha256=request.sha256,
        wire_request_artifact=wire_request.path,
        wire_request_sha256=wire_request.sha256,
        wire_response_body_artifact=body.path,
        wire_response_body_sha256=body.sha256,
        wire_response_metadata_artifact=metadata_binding.path,
        wire_response_metadata_sha256=metadata_binding.sha256,
        provider_raw_artifact=provider_raw.path,
        provider_raw_sha256=provider_raw.sha256,
        status=cast(Any, attempt_status),
        retryable=False,
        http_status=200,
        error_code=attempt_error,
        provider_request_id=f"chatcmpl-selection-{index}" if attempt_error is None else None,
        returned_model=MODEL if attempt_error is None else None,
        tokens={},
        started_at=NOW,
        completed_at=NOW,
    )
    attempt_binding = _write(
        root,
        (parent_relative / "attempts/0000/attempt.json").as_posix(),
        canonical_json_bytes(attempt.model_dump(mode="json")) + b"\n",
    )
    llm_attempt = _write(
        root,
        (parent_relative / "llm_attempts/0000.json").as_posix(),
        b"llm attempt\n",
    )
    llm_call = _write(
        root,
        (parent_relative / "llm_call.json").as_posix(),
        b"llm call\n",
    )
    variants: LF022ArtifactBinding | None = None
    if category == "success":
        variants = _write(
            root,
            (parent_relative / "provisional_variants.jsonl").as_posix(),
            b"variant fixture\n",
        )

    terminal_payload: dict[str, object] = {
        "schema_version": 1,
        "execution_admission_id": admission.admission_id,
        "execution_task_id": task_id,
        "status": terminal_status,
        "attempt_artifacts": [attempt_binding.path],
        "attempt_sha256s": [attempt_binding.sha256],
        "llm_attempt_artifacts": [llm_attempt.path],
        "llm_attempt_sha256s": [llm_attempt.sha256],
        "llm_call_id": f"call:{index + 1:064x}",
        "llm_call_artifact": llm_call.path,
        "llm_call_sha256": llm_call.sha256,
        "variants_artifact": variants.path if variants else None,
        "variants_sha256": variants.sha256 if variants else None,
        "provisional_variant_count": 1 if variants else 0,
        "terminal_error_code": terminal_error,
        "raw_before_parse_verified": True,
        "exact_replay_supported": True,
        "output_quality_tier": "provisional",
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    terminal = LF022ExecutionTerminalRecord.model_validate(
        {
            **terminal_payload,
            "terminal_id": make_id("lf022_execution_terminal", terminal_payload),
        }
    )
    terminal_binding = _write(
        root,
        (parent_relative / "terminal.json").as_posix(),
        canonical_json_bytes(terminal.model_dump(mode="json")) + b"\n",
    )
    return terminal, terminal_binding


def _fixture(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_budget_index: int | None = None,
    duplicate_success_source: bool = False,
    all_same_theorem: bool = False,
    incomplete_replay: bool = False,
) -> tuple[
    LF022ArtifactBinding,
    LF022ArtifactBinding,
    LF022ArtifactBinding,
    LF022ArtifactBinding,
]:
    _write(
        root,
        "prompts/proposers/lean_variant_v2.txt",
        (ROOT / "prompts/proposers/lean_variant_v2.txt").read_bytes(),
    )
    manifest_binding = _write(root, "data/prefix256/batch_manifest.json", b"manifest\n")
    admission_binding = _write(root, "data/prefix256/admission.json", b"admission\n")
    code_bundle = _write(root, "artifacts/historical-code.tar.gz", b"code bundle\n")

    decoding = LF022RCPDecodingContract(
        contract_id="kimi_k2_7_public_smoke_v3",
        temperature=1.0,
        top_p=0.95,
        max_tokens=16_384,
        seed=42,
        thinking_mode="forced_thinking",
        reasoning_effort="high",
        chat_template_enable_thinking=True,
    )
    route = LF022RCPRouteBinding.model_construct(
        proposer_family_id="moonshot_kimi_k2",
        model_id=MODEL,
        decoding=decoding,
    )
    admission = LF022GOpenExecutionAdmission.model_construct(
        admission_id=f"lf022_execution_admission:{'a' * 64}",
        route=route,
        artifacts=SimpleNamespace(code_bundle=code_bundle),
        code_tree_hash="d" * 64,
    )
    loaded: list[VerifiedLF022BatchTask] = []
    task_bindings: list[LF022BatchTaskBinding] = []
    historical_terminals: list[LF022HistoricalTerminalBinding] = []
    for index in range(256):
        allocation_task_id = f"lf022_production_task:{index + 1:064x}"
        source_number = 1 if duplicate_success_source and index == 8 else index + 1
        source_admission_id = f"lf022_source_admission:{source_number:064x}"
        allocation = SimpleNamespace(
            task_id=allocation_task_id,
            admission_record_id=source_admission_id,
        )
        source = PublicLeanVariantSource(
            source_theorem_id=f"thm:{1 if all_same_theorem else index + 1:064x}",
            source_representation_id=f"repr:{index + 1:064x}",
            context_id=f"ctx:{index + 1:064x}",
            imports=("Mathlib",),
            source_statement=f"theorem source_{index} : ({index} : Nat) = {index}",
            optional_natural_language=None,
            source_id="mathlib",
            source_revision="b" * 40,
            source_license="Apache-2.0",
            source_is_public=True,
            external_transmission_allowed=True,
            denylist_checked=True,
            denylist_hits=(),
        )
        task = LF022GOpenExecutionTask.model_construct(
            execution_task_id=f"lf022_execution_task:{index + 1:064x}",
            allocation_task=allocation,
            source=source,
            proposal_count=1,
            requested_relations=(IntendedRelation.NEAR_MISS,),
        )
        task_binding = _write(
            root,
            f"data/prefix256/tasks/{index + 1:064x}.json",
            f"task {index}\n".encode(),
        )
        task_bindings.append(
            LF022BatchTaskBinding(
                allocation_task_id=allocation_task_id,
                execution_task_id=task.execution_task_id,
                task=task_binding,
            )
        )
        loaded.append(
            VerifiedLF022BatchTask(
                family="moonshot_kimi_k2",
                admission=admission,
                task=task,
                verified=cast(Any, None),
                task_inputs=cast(Any, None),
            )
        )
        category = "budget" if index < 6 else "proof" if index < 8 else "success"
        terminal, terminal_binding = _task_terminal(
            root,
            admission=admission,
            task=task,
            index=index,
            category=category,
            fake_budget_raw=index == fake_budget_index,
        )
        historical_terminals.append(
            LF022HistoricalTerminalBinding(
                execution_task_id=task.execution_task_id,
                terminal_id=terminal.terminal_id,
                terminal_artifact=terminal_binding,
            )
        )

    route_manifest = LF022BatchRouteManifest.model_construct(
        proposer_family_id="moonshot_kimi_k2",
        model_id=MODEL,
        admission_id=admission.admission_id,
        admission=admission_binding,
        tasks=tuple(task_bindings),
    )
    manifest = LF022PublicBatchManifest.model_construct(
        batch_id=f"lf022_public_batch:{'c' * 64}",
        batch_directory="data/prefix256",
        executor_output_root="data/lf022_execution",
        routes=(route_manifest,),
        total_task_count=256,
    )
    monkeypatch.setattr(
        selection_module,
        "load_lf022_public_batch",
        lambda **_: (manifest, tuple(loaded)),
    )
    historical_module = _write(
        root,
        "artifacts/historical-lf022-executor.py",
        b"HISTORICAL_FIXTURE = True\n",
    )
    historical_replay = LF022HistoricalReplayResult(
        code_tree_hash=admission.code_tree_hash,
        code_bundle_sha256=code_bundle.sha256,
        module_bindings=(
            LF022HistoricalModuleBinding(
                module_name="leanfaith.generation.lf022_executor",
                path=historical_module.path,
                sha256=historical_module.sha256,
            ),
        ),
        terminal_bindings=tuple(
            sorted(historical_terminals, key=lambda item: item.execution_task_id)
        ),
    )
    monkeypatch.setattr(
        selection_module,
        "run_lf022_historical_replay",
        lambda **_: historical_replay,
    )

    replayed = 255 if incomplete_replay else 256
    preflight = 1 if incomplete_replay else 0
    success_count = 247 if incomplete_replay else 248
    status_counts = dict(
        sorted(
            {
                "proposer_parse_failed": 2,
                "provider_exhausted": 6,
                "provisional_variants_created": success_count,
            }.items()
        )
    )
    report_payload: dict[str, object] = {
        "schema_version": 2,
        "batch_id": manifest.batch_id,
        "mode": "offline",
        "task_count": 256,
        "preflight_only_count": preflight,
        "replayed_terminal_count": replayed,
        "new_terminal_count": 0,
        "successful_terminal_count": success_count,
        "failed_terminal_count": 8,
        "error_count": 0,
        "network_calls_this_run": 0,
        "terminal_status_counts": status_counts,
        "failed_task_ids": [],
        "max_concurrency": 1,
        "minimum_request_interval_seconds": 0.0,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    report = LF022BatchRunReport.model_validate(
        {
            **report_payload,
            "report_id": make_id("lf022_batch_run", report_payload),
        }
    )
    replay_binding = _write(
        root,
        f"data/prefix256/runs/{report.report_id.split(':', 1)[1]}.json",
        canonical_json_bytes(report.model_dump(mode="json")),
    )
    contract_mapping = dict(
        load_yaml_mapping(ROOT / "configs/generation/lf022_kimi_k2_7_proposer_v4.yaml")
    )
    contract_mapping["prior_lineage"] = {
        "batch_id": manifest.batch_id,
        "execution_admission_id": admission.admission_id,
        "batch_manifest": manifest_binding.model_dump(mode="json"),
        "execution_admission": admission_binding.model_dump(mode="json"),
        "exact_offline_replay_report_id": report.report_id,
        "exact_offline_replay_report": replay_binding.model_dump(mode="json"),
    }
    config_binding = _write(
        root,
        "configs/generation/lf022_kimi_k2_7_proposer_v4.yaml",
        canonical_json_bytes(contract_mapping),
    )
    for role, relative in selection_module._CURRENT_IMPLEMENTATION_PATHS:
        del role
        path = root / relative
        if not path.exists():
            _write(root, relative, f"fixture implementation: {relative}\n".encode())
    current_code_bundle = _write(
        root,
        "artifacts/current-code-bundle.tar.gz",
        b"current code bundle fixture\n",
    )
    monkeypatch.setattr(
        selection_module,
        "collect_code_state",
        lambda _: CodeState(
            git_revision="e" * 40,
            git_dirty=False,
            base_git_commit="e" * 40,
            code_tree_hash="f" * 64,
            tracked_diff_hash="0" * 64,
            untracked_files=(),
        ),
    )

    def validate_current_bundle(path: Path, expected_code_tree_hash: str) -> str:
        assert expected_code_tree_hash == "f" * 64
        assert path == root / current_code_bundle.path
        return hash_file(path)

    monkeypatch.setattr(
        selection_module,
        "validate_code_bundle",
        validate_current_bundle,
    )
    return manifest_binding, replay_binding, config_binding, current_code_bundle


def test_freezes_exact_6_2_8_selection_with_capability_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(
        tmp_path,
        monkeypatch,
        duplicate_success_source=True,
    )
    first = freeze_lf022_kimi_v4_challenge_selection(
        repo_root=tmp_path,
        v3_manifest_binding=manifest,
        exact_offline_replay_report_binding=replay,
        v4_contract_binding=contract,
        current_code_bundle_binding=current_bundle,
    )
    second = freeze_lf022_kimi_v4_challenge_selection(
        repo_root=tmp_path,
        v3_manifest_binding=manifest,
        exact_offline_replay_report_binding=replay,
        v4_contract_binding=contract,
        current_code_bundle_binding=current_bundle,
    )

    assert first.selection == second.selection
    assert tuple(item.role for item in first.selection.selected) == (
        ("budget_exhausted",) * 6 + ("proof_bearing",) * 2 + ("prior_success",) * 8
    )
    assert first.selection.capability_allocation_task_id == ("lf022_production_task:" + f"{1:064x}")
    # Success index 8 shares the first budget case's source and is skipped.
    assert first.selection.selected[8].allocation_task_id.endswith(f"{10:064x}")
    assert len({item.source_admission_record_id for item in first.selection.selected}) == 16
    assert len(first.selection.historical_terminal_bindings) == 256
    assert first.selection.historical_replay_network_calls == 0
    assert first.selection.historical_code_tree_hash == "d" * 64
    assert first.selection.current_implementation.code_tree_hash == "f" * 64
    assert first.selection.current_implementation.code_bundle == current_bundle
    assert tuple(item.role for item in first.selection.current_implementation.files) == tuple(
        role for role, _ in selection_module._CURRENT_IMPLEMENTATION_PATHS
    )
    assert first.selection.live_calls_performed is False
    assert first.selection.execution_admission_created is False
    assert first.selection.promotion_enabled is False
    verified = verify_lf022_kimi_v4_challenge_selection(
        repo_root=tmp_path,
        selection_binding=LF022ArtifactBinding(
            path=first.selection_path.relative_to(tmp_path).as_posix(),
            sha256=hash_file(first.selection_path),
        ),
    )
    assert verified == first.selection


def test_cli_freezes_then_replays_the_offline_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    frozen = runner.invoke(
        app,
        [
            "freeze-lf022-kimi-v4-challenge",
            "--root",
            str(tmp_path),
            "--v3-manifest",
            manifest.path,
            "--exact-offline-replay-report",
            replay.path,
            "--v4-contract",
            contract.path,
            "--current-code-bundle",
            current_bundle.path,
        ],
    )
    assert frozen.exit_code == 0, frozen.output
    assert "network_requests=0" in frozen.output
    assert "capability_rank=0" in frozen.output
    selections = tuple((tmp_path / LF022_KIMI_V4_SELECTION_ROOT).glob("*.json"))
    assert len(selections) == 1

    replayed = runner.invoke(
        app,
        [
            "verify-lf022-kimi-v4-challenge",
            "--root",
            str(tmp_path),
            "--selection",
            selections[0].relative_to(tmp_path).as_posix(),
        ],
    )
    assert replayed.exit_code == 0, replayed.output
    assert "replayed_terminals=256" in replayed.output
    assert "network_requests=0" in replayed.output


def test_freeze_rejects_dirty_current_selection_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        selection_module,
        "collect_code_state",
        lambda _: CodeState(
            git_revision="e" * 40,
            git_dirty=True,
            base_git_commit="e" * 40,
            code_tree_hash="f" * 64,
            tracked_diff_hash="1" * 64,
            untracked_files=("new_parser.py",),
        ),
    )
    with pytest.raises(LF022KimiV4SelectionError, match="requires a clean current Git worktree"):
        freeze_lf022_kimi_v4_challenge_selection(
            repo_root=tmp_path,
            v3_manifest_binding=manifest,
            exact_offline_replay_report_binding=replay,
            v4_contract_binding=contract,
            current_code_bundle_binding=current_bundle,
        )


def test_verifier_rejects_current_parser_module_drift_before_reclassification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(tmp_path, monkeypatch)
    frozen = freeze_lf022_kimi_v4_challenge_selection(
        repo_root=tmp_path,
        v3_manifest_binding=manifest,
        exact_offline_replay_report_binding=replay,
        v4_contract_binding=contract,
        current_code_bundle_binding=current_bundle,
    )
    selector_path = tmp_path / "src/leanfaith/generation/lf022_kimi_v4_selection.py"
    selector_path.write_bytes(selector_path.read_bytes() + b"# drift\n")
    selector_called = False

    def reject_selector_call(*_: object, **__: object) -> object:
        nonlocal selector_called
        selector_called = True
        raise AssertionError("selector ran before current implementation verification")

    monkeypatch.setattr(selection_module, "_select", reject_selector_call)
    with pytest.raises(LF022KimiV4SelectionError, match="current code differs"):
        verify_lf022_kimi_v4_challenge_selection(
            repo_root=tmp_path,
            selection_binding=LF022ArtifactBinding(
                path=frozen.selection_path.relative_to(tmp_path).as_posix(),
                sha256=hash_file(frozen.selection_path),
            ),
        )
    assert selector_called is False


def test_freeze_cli_contains_invalid_root_failure(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing-repository"
    result = CliRunner().invoke(
        app,
        [
            "freeze-lf022-kimi-v4-challenge",
            "--root",
            str(missing_root),
            "--current-code-bundle",
            "artifacts/current-code-bundle.tar.gz",
        ],
    )
    assert result.exit_code == 2
    assert "Kimi-v4 challenge freeze rejected" in result.output


def test_freeze_cli_contains_malformed_current_bundle_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(tmp_path, monkeypatch)

    def reject_malformed_bundle(_: Path, __: str) -> str:
        raise tarfile.ReadError("not a readable code bundle")

    monkeypatch.setattr(
        selection_module,
        "validate_code_bundle",
        reject_malformed_bundle,
    )
    result = CliRunner().invoke(
        app,
        [
            "freeze-lf022-kimi-v4-challenge",
            "--root",
            str(tmp_path),
            "--v3-manifest",
            manifest.path,
            "--exact-offline-replay-report",
            replay.path,
            "--v4-contract",
            contract.path,
            "--current-code-bundle",
            current_bundle.path,
        ],
    )
    assert result.exit_code == 2
    assert "Kimi-v4 challenge freeze rejected" in result.output
    assert "failed validation" in result.output


def test_verify_cli_contains_missing_path_failure(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "verify-lf022-kimi-v4-challenge",
            "--root",
            str(tmp_path),
            "--selection",
            "missing-selection.json",
        ],
    )
    assert result.exit_code == 2
    assert "Kimi-v4 challenge replay rejected" in result.output


def test_legacy_terminal_string_cannot_fake_budget_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(
        tmp_path,
        monkeypatch,
        fake_budget_index=5,
    )
    with pytest.raises(LF022KimiV4SelectionError, match="insufficient unique-source"):
        freeze_lf022_kimi_v4_challenge_selection(
            repo_root=tmp_path,
            v3_manifest_binding=manifest,
            exact_offline_replay_report_binding=replay,
            v4_contract_binding=contract,
            current_code_bundle_binding=current_bundle,
        )


def test_compatible_alternate_batch_binding_cannot_be_cherry_picked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(tmp_path, monkeypatch)
    # The patched loader still presents an otherwise compatible 256-task Kimi
    # population.  Its different manifest path/bytes must nevertheless fail
    # the preregistered lineage binding before selection.
    alternate_manifest = _write(
        tmp_path,
        "data/alternate_prefix256/batch_manifest.json",
        b"compatible alternate manifest\n",
    )
    assert alternate_manifest != manifest
    with pytest.raises(LF022KimiV4SelectionError, match="exact preregistered"):
        freeze_lf022_kimi_v4_challenge_selection(
            repo_root=tmp_path,
            v3_manifest_binding=alternate_manifest,
            exact_offline_replay_report_binding=replay,
            v4_contract_binding=contract,
            current_code_bundle_binding=current_bundle,
        )


def test_incomplete_offline_replay_cannot_freeze_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(
        tmp_path,
        monkeypatch,
        incomplete_replay=True,
    )
    with pytest.raises(LF022KimiV4SelectionError, match="complete 256-terminal"):
        freeze_lf022_kimi_v4_challenge_selection(
            repo_root=tmp_path,
            v3_manifest_binding=manifest,
            exact_offline_replay_report_binding=replay,
            v4_contract_binding=contract,
            current_code_bundle_binding=current_bundle,
        )


def test_distinct_admission_ids_cannot_hide_one_repeated_theorem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(
        tmp_path,
        monkeypatch,
        all_same_theorem=True,
    )
    with pytest.raises(LF022KimiV4SelectionError, match="insufficient unique-source"):
        freeze_lf022_kimi_v4_challenge_selection(
            repo_root=tmp_path,
            v3_manifest_binding=manifest,
            exact_offline_replay_report_binding=replay,
            v4_contract_binding=contract,
            current_code_bundle_binding=current_bundle,
        )


def test_verifier_rejects_terminal_bytes_changed_after_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(tmp_path, monkeypatch)
    frozen = freeze_lf022_kimi_v4_challenge_selection(
        repo_root=tmp_path,
        v3_manifest_binding=manifest,
        exact_offline_replay_report_binding=replay,
        v4_contract_binding=contract,
        current_code_bundle_binding=current_bundle,
    )
    selection_binding = LF022ArtifactBinding(
        path=frozen.selection_path.relative_to(tmp_path).as_posix(),
        sha256=hash_file(frozen.selection_path),
    )
    terminal_path = tmp_path / frozen.selection.population[0].terminal.path
    terminal_path.write_bytes(terminal_path.read_bytes() + b" ")
    with pytest.raises(LF022KimiV4SelectionError):
        verify_lf022_kimi_v4_challenge_selection(
            repo_root=tmp_path,
            selection_binding=selection_binding,
        )


def test_self_consistent_post_replay_lineage_rewrite_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay, contract, current_bundle = _fixture(tmp_path, monkeypatch)
    task_id = f"lf022_execution_task:{9:064x}"
    digest = task_id.split(":", 1)[1]
    task_root = tmp_path / "data/lf022_execution/tasks" / digest[:2] / digest
    terminal_path = task_root / "terminal.json"
    terminal = LF022ExecutionTerminalRecord.model_validate_json(terminal_path.read_bytes())
    attempt_path = tmp_path / terminal.attempt_artifacts[-1]
    attempt = LF022ExecutionAttemptRecord.model_validate_json(attempt_path.read_bytes())

    assert attempt.wire_response_body_artifact is not None
    assert attempt.wire_response_metadata_artifact is not None
    body_path = tmp_path / attempt.wire_response_body_artifact
    body_path.write_bytes(
        _completion_body(
            finish_reason="stop",
            content=_variant_content("theorem rewritten_success : True"),
            index=999,
        )
    )
    metadata_path = tmp_path / attempt.wire_response_metadata_artifact
    rewritten_metadata = LF022WireResponseMetadata(
        status_code=200,
        headers={},
        body_sha256=hash_file(body_path),
    )
    metadata_path.write_bytes(
        canonical_json_bytes(rewritten_metadata.model_dump(mode="json")) + b"\n"
    )
    provider_raw_path = tmp_path / attempt.provider_raw_artifact
    provider_raw_path.write_bytes(b"self-consistently rewritten raw response\n")

    attempt_payload = attempt.model_dump(mode="json")
    attempt_payload.update(
        {
            "wire_response_body_sha256": hash_file(body_path),
            "wire_response_metadata_sha256": hash_file(metadata_path),
            "provider_raw_sha256": hash_file(provider_raw_path),
            "provider_request_id": "chatcmpl-selection-999",
        }
    )
    rewritten_attempt = LF022ExecutionAttemptRecord.model_validate(attempt_payload)
    attempt_path.write_bytes(
        canonical_json_bytes(rewritten_attempt.model_dump(mode="json")) + b"\n"
    )

    terminal_payload = terminal.model_dump(mode="json", exclude={"terminal_id"})
    terminal_payload["attempt_sha256s"] = [hash_file(attempt_path)]
    rewritten_terminal = LF022ExecutionTerminalRecord.model_validate(
        {
            **terminal_payload,
            "terminal_id": make_id("lf022_execution_terminal", terminal_payload),
        }
    )
    terminal_path.write_bytes(
        canonical_json_bytes(rewritten_terminal.model_dump(mode="json")) + b"\n"
    )

    with pytest.raises(
        LF022KimiV4SelectionError,
        match="current terminal differs from admitted-code historical replay",
    ):
        freeze_lf022_kimi_v4_challenge_selection(
            repo_root=tmp_path,
            v3_manifest_binding=manifest,
            exact_offline_replay_report_binding=replay,
            v4_contract_binding=contract,
            current_code_bundle_binding=current_bundle,
        )
