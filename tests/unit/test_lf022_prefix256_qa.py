"""Focused tests for the fail-closed LF-022 prefix-256 operational audit."""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.generation import lf022_historical_replay as historical_replay_module
from leanfaith.generation import lf022_prefix256_qa as qa_module
from leanfaith.generation.lf022_batch import (
    LF022BatchRouteManifest,
    LF022BatchRunReport,
    LF022PublicBatchManifest,
    VerifiedLF022BatchTask,
)
from leanfaith.generation.lf022_execution import (
    LF022ExecutionArtifacts,
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    LF022RCPRouteBinding,
)
from leanfaith.generation.lf022_executor import (
    LF022ExecutionAttemptRecord,
    LF022ExecutionTerminalRecord,
)
from leanfaith.generation.lf022_historical_replay import (
    LF022HistoricalModuleBinding,
    LF022HistoricalReplayError,
    LF022HistoricalReplayResult,
    LF022HistoricalTerminalBinding,
)
from leanfaith.generation.lf022_prefix256_qa import (
    LF022_PREFIX256_REVIEW_SAMPLE_SIZE,
    LF022Prefix256OperationalQAReport,
    LF022Prefix256ReviewerItem,
    run_lf022_prefix256_operational_qa,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.generation.llm_variants import PublicLeanVariantSource
from leanfaith.schemas.enums import (
    GeneratorKind,
    IntendedRelation,
    LLMCallStatus,
    LLMRole,
    ParseStatus,
    Polarity,
    QualityTier,
    ValidationStatus,
)
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.llm import LLMCallRecord, make_llm_attempt_id, make_llm_call_id
from leanfaith.schemas.variant import VariantRecord


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hash_file(path)


def test_historical_replay_compatibility_accepts_terminal_references_and_restores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = {
        "execution_task_id": f"lf022_execution_task:{'1' * 64}",
        "terminal_id": f"lf022_execution_terminal:{'2' * 64}",
        "terminal_artifact": {
            "path": "data/executor/terminal.json",
            "sha256": "3" * 64,
        },
        "status": "provisional_variants_created",
        "terminal_error_code": None,
    }
    module_binding = {
        "module_name": "leanfaith.generation.lf022_batch",
        "path": "src/leanfaith/generation/lf022_batch.py",
        "sha256": "4" * 64,
    }
    original = historical_replay_module._explicit_record_bindings
    with pytest.raises(LF022HistoricalReplayError):
        original(reference)

    sentinel = cast(LF022HistoricalReplayResult, object())

    def fake_replay(**kwargs: object) -> LF022HistoricalReplayResult:
        del kwargs
        compatible = historical_replay_module._explicit_record_bindings
        assert compatible(reference) is None
        assert compatible(module_binding) == []
        return sentinel

    monkeypatch.setattr(
        qa_module,
        "run_lf022_historical_replay",
        fake_replay,
    )
    result = qa_module._run_terminal_reference_compatible_historical_replay(
        repo_root=tmp_path,
        manifest_binding=LF022ArtifactBinding(path="manifest.json", sha256="4" * 64),
        loaded_tasks=(),
        executor_output_root="data/lf022_execution",
    )

    assert result is sentinel
    assert historical_replay_module._explicit_record_bindings is original
    with pytest.raises(LF022HistoricalReplayError):
        original(reference)


def _terminal(
    root: Path,
    *,
    admission: LF022GOpenExecutionAdmission,
    task: LF022GOpenExecutionTask,
    index: int,
    successful: bool,
    duplicate_first: bool,
    proof_body_first: bool,
) -> None:
    task_id = task.execution_task_id
    digest = task_id.split(":", 1)[1]
    relative_parent = Path("data/lf022_execution/tasks") / digest[:2] / digest
    parent = root / relative_parent
    request_path = parent / "attempts/0000/provider_request.json"
    wire_request_path = parent / "attempts/0000/wire_request.json"
    response_body_path = parent / "attempts/0000/wire_response.body"
    response_metadata_path = parent / "attempts/0000/wire_response.json"
    raw_path = parent / "provider_raw/0000.json"
    attempt_path = parent / "attempts/0000/attempt.json"
    llm_attempt_path = parent / "llm_attempts/0000.json"
    call_path = parent / "llm_call.json"
    for base_name in ("admission.json", "task.json", "preflight.json"):
        _write(parent / base_name, f'{{"fixture":"{base_name}"}}\n'.encode())
    request_sha = _write(request_path, b'{"fixture":"request"}\n')
    wire_request_sha = _write(wire_request_path, b'{"fixture":"wire_request"}\n')
    response_body_sha = _write(response_body_path, b'{"fixture":"wire_response_body"}\n')
    response_metadata_sha = _write(
        response_metadata_path,
        b'{"fixture":"wire_response_metadata"}\n',
    )
    raw_sha = _write(raw_path, b'{"fixture":"raw"}\n')
    _write(parent / "attempts/0000/.transport_started", b"started\n")
    _write(parent / "attempts/0000/.transport_completed", b"completed\n")
    llm_attempt_sha = _write(llm_attempt_path, b'{"fixture":"llm_attempt"}\n')
    now = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
    attempt = LF022ExecutionAttemptRecord(
        execution_task_id=task_id,
        provider_request_hash=f"{index + 1:064x}",
        provider_attempt_id=f"provider-attempt:{index + 1:064x}",
        attempt_index=0,
        request_artifact=request_path.relative_to(root).as_posix(),
        request_sha256=request_sha,
        wire_request_artifact=wire_request_path.relative_to(root).as_posix(),
        wire_request_sha256=wire_request_sha,
        wire_response_body_artifact=response_body_path.relative_to(root).as_posix(),
        wire_response_body_sha256=response_body_sha,
        wire_response_metadata_artifact=response_metadata_path.relative_to(root).as_posix(),
        wire_response_metadata_sha256=response_metadata_sha,
        provider_raw_artifact=raw_path.relative_to(root).as_posix(),
        provider_raw_sha256=raw_sha,
        status="response_parsed" if successful else "terminal_http_error",
        retryable=False,
        http_status=200 if successful else 400,
        error_code=None if successful else "output_budget_exhausted",
        returned_model=admission.route.model_id if successful else None,
        started_at=now,
        completed_at=now,
    )
    attempt_sha = _write(
        attempt_path,
        canonical_json_bytes(attempt.model_dump(mode="json")) + b"\n",
    )
    input_ids = (task.source.source_theorem_id, f"request:{index + 1:064x}")
    call_id = make_llm_call_id(
        provider="epfl_rcp",
        provider_slot="moonshot_kimi_k2",
        model=admission.route.model_id,
        model_family=admission.route.canonical_family,
        model_revision=admission.route.route_snapshot_revision,
        role=LLMRole.PROPOSER,
        problem_record_id=None,
        prompt_template_hash="d" * 64,
        prompt_render_hash=f"{index + 1:064x}",
        input_ids=input_ids,
        decoding={},
    )
    call = LLMCallRecord(
        schema_version=2,
        call_id=call_id,
        provider="epfl_rcp",
        provider_slot="moonshot_kimi_k2",
        model=admission.route.model_id,
        model_family=admission.route.canonical_family,
        model_revision=admission.route.route_snapshot_revision,
        role=LLMRole.PROPOSER,
        request_date=now,
        started_at=now,
        completed_at=now,
        execution_mode="external",
        prompt_template_id="lean_variant",
        prompt_template_version="v1",
        prompt_template_hash="d" * 64,
        prompt_render_hash=f"{index + 1:064x}",
        request_artifact=request_path.relative_to(root).as_posix(),
        input_ids=input_ids,
        decoding={},
        raw_output_artifact=raw_path.relative_to(root).as_posix(),
        parsed_output=({"variants": []} if successful else None),
        parse_status=ParseStatus.PARSED if successful else ParseStatus.EMPTY,
        retry_count=0,
        supervision_eligible=False,
        private_source_content=False,
        denylist_checked=True,
        terminal_status=LLMCallStatus.COMPLETED if successful else LLMCallStatus.EXHAUSTED,
        attempt_ids=(make_llm_attempt_id(call_id, 0),),
        latency_ms=0,
        provider_request_hash="e" * 64,
        request_artifact_sha256=request_sha,
        raw_response_sha256=raw_sha,
        metadata={
            "generation_config_hash": hash_canonical(admission.model_dump(mode="json")),
            "lf022_execution_admission_id": admission.admission_id,
            "lf022_execution_task_id": task.execution_task_id,
        },
    )
    call_sha = _write(
        call_path,
        canonical_json_bytes(call.model_dump(mode="json")) + b"\n",
    )
    variants_relative: str | None = None
    variants_sha: str | None = None
    variant_count = 0
    if successful:
        candidate_index = 0 if duplicate_first and index == 1 else index
        candidate = f"theorem generated_{candidate_index:03d} : ({candidate_index} : Nat) = {candidate_index}"
        if proof_body_first and index == 0:
            candidate = "theorem generated_000 : True := by sorry"
        variant = VariantRecord(
            variant_id=f"var:{index + 1:064x}",
            source_theorem_ids=(task.source.source_theorem_id,),
            source_representation_ids=(cast(str, task.source.source_representation_id),),
            context_id=task.source.context_id,
            generator_kind=GeneratorKind.LLM_PROPOSER,
            generator_id=admission.route.model_id,
            generation_config_hash=hash_canonical(admission.model_dump(mode="json")),
            seed=42,
            prompt_artifact=request_path.relative_to(root).as_posix(),
            raw_output_artifact=raw_path.relative_to(root).as_posix(),
            extracted_statement=candidate,
            candidate_code_hash=sha256_hex(candidate.encode("utf-8")),
            intended_relation=IntendedRelation.NEAR_MISS,
            intended_error_types=("E01",),
            candidate_pool="G_open",
            transformation_trace=(
                {
                    "kind": "llm_proposal",
                    "proposal_index": 0,
                    "llm_call_id": call_id,
                },
            ),
            validation_status=ValidationStatus.UNVALIDATED,
            quality_tier=QualityTier.PROVISIONAL,
            polarity_metadata=Polarity.NEGATIVE,
            metadata={
                "llm_call_id": call_id,
                "proposer_family": task.allocation_task.proposer_family_id,
            },
        )
        variants_path = parent / "provisional_variants.jsonl"
        variants_sha = _write(
            variants_path,
            canonical_json_bytes(variant.model_dump(mode="json")) + b"\n",
        )
        variants_relative = variants_path.relative_to(root).as_posix()
        variant_count = 1

    terminal_content: dict[str, object] = {
        "schema_version": 1,
        "execution_admission_id": admission.admission_id,
        "execution_task_id": task_id,
        "status": "provisional_variants_created" if successful else "provider_exhausted",
        "attempt_artifacts": [attempt_path.relative_to(root).as_posix()],
        "attempt_sha256s": [attempt_sha],
        "llm_attempt_artifacts": [llm_attempt_path.relative_to(root).as_posix()],
        "llm_attempt_sha256s": [llm_attempt_sha],
        "llm_call_id": call_id,
        "llm_call_artifact": call_path.relative_to(root).as_posix(),
        "llm_call_sha256": call_sha,
        "variants_artifact": variants_relative,
        "variants_sha256": variants_sha,
        "provisional_variant_count": variant_count,
        "terminal_error_code": None if successful else "output_budget_exhausted",
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
            **terminal_content,
            "terminal_id": make_id("lf022_execution_terminal", terminal_content),
        }
    )
    _write(
        parent / "terminal.json",
        canonical_json_bytes(terminal.model_dump(mode="json")) + b"\n",
    )


def _fixture(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    success_count: int = 256,
    duplicate_first: bool = False,
    proof_body_first: bool = False,
) -> tuple[Path, Path]:
    batch_id = f"lf022_public_batch:{'a' * 64}"
    admission_id = f"lf022_execution_admission:{'b' * 64}"
    route = LF022RCPRouteBinding.model_construct(
        provider_id="epfl_rcp",
        proposer_family_id="moonshot_kimi_k2",
        model_id="moonshotai/Kimi-K2.7-Code",
        canonical_family="moonshotai/kimi-k2",
        route_snapshot_revision=f"rcp-catalog-sha256:{'f' * 64}",
    )
    code_bundle_path = root / "artifacts/historical_code_bundle.tar.gz"
    code_bundle_sha = _write(code_bundle_path, b"historical-code-bundle-fixture\n")
    code_bundle_binding = LF022ArtifactBinding(
        path=code_bundle_path.relative_to(root).as_posix(),
        sha256=code_bundle_sha,
    )
    admission = LF022GOpenExecutionAdmission.model_construct(
        admission_id=admission_id,
        route=route,
        code_tree_hash="8" * 64,
        artifacts=LF022ExecutionArtifacts.model_construct(
            code_bundle=code_bundle_binding,
        ),
    )
    loaded: list[VerifiedLF022BatchTask] = []
    expected_terminals: dict[str, tuple[LF022ExecutionTerminalRecord, Path, str]] = {}
    for index in range(256):
        source = PublicLeanVariantSource(
            source_theorem_id=f"thm:{index + 1:064x}",
            source_representation_id=f"repr:{index + 1:064x}",
            context_id=f"ctx:{index + 1:064x}",
            imports=("Mathlib",),
            source_statement=f"theorem source_{index:03d} : ({index} : Nat) = {index}",
            source_id="mathlib",
            source_revision="c" * 40,
            source_license="Apache-2.0",
            source_is_public=True,
            external_transmission_allowed=True,
            denylist_checked=True,
        )
        allocation = cast(
            Any,
            type(
                "Allocation",
                (),
                {"proposer_family_id": "moonshot_kimi_k2"},
            )(),
        )
        task = LF022GOpenExecutionTask.model_construct(
            execution_task_id=f"lf022_execution_task:{index + 1:064x}",
            source=source,
            allocation_task=allocation,
            proposal_count=1,
            requested_relations=(IntendedRelation.NEAR_MISS,),
        )
        item = VerifiedLF022BatchTask(
            family="moonshot_kimi_k2",
            admission=admission,
            task=task,
            verified=cast(Any, None),
            task_inputs=cast(Any, None),
        )
        loaded.append(item)
        _terminal(
            root,
            admission=admission,
            task=task,
            index=index,
            successful=index < success_count,
            duplicate_first=duplicate_first,
            proof_body_first=proof_body_first,
        )
        task_digest = task.execution_task_id.split(":", 1)[1]
        terminal_path = (
            root / "data/lf022_execution/tasks" / task_digest[:2] / task_digest / "terminal.json"
        )
        terminal = LF022ExecutionTerminalRecord.model_validate_json(terminal_path.read_bytes())
        expected_terminals[task.execution_task_id] = (
            terminal,
            terminal_path,
            hash_file(terminal_path),
        )

    route_manifest = LF022BatchRouteManifest.model_construct(
        proposer_family_id="moonshot_kimi_k2",
        model_id="moonshotai/Kimi-K2.7-Code",
        admission_id=admission_id,
    )
    manifest = LF022PublicBatchManifest.model_construct(
        batch_id=batch_id,
        batch_directory="data/prefix256_batch",
        executor_output_root="data/lf022_execution",
        routes=(route_manifest,),
        total_task_count=256,
    )
    manifest_path = root / "data/prefix256_batch/batch_manifest.json"
    _write(manifest_path, b'{"fixture":"manifest"}\n')
    monkeypatch.setattr(
        qa_module,
        "load_lf022_public_batch",
        lambda **_: (manifest, tuple(loaded)),
    )
    monkeypatch.setattr(
        qa_module,
        "collect_code_state",
        lambda _: SimpleNamespace(code_tree_hash="9" * 64),
    )

    historical_module_path = root / "artifacts/historical_llm_variants.py"
    historical_module_sha = _write(historical_module_path, b"HISTORICAL_MARKER = True\n")

    def exact_historical_replay(**kwargs: Any) -> LF022HistoricalReplayResult:
        replayed_tasks = cast(tuple[VerifiedLF022BatchTask, ...], kwargs["loaded_tasks"])
        bindings: list[LF022HistoricalTerminalBinding] = []
        for replayed_task in replayed_tasks:
            expected, terminal_path, expected_sha = expected_terminals[
                replayed_task.task.execution_task_id
            ]
            if hash_file(terminal_path) != expected_sha:
                raise qa_module.LF022Prefix256QAError(
                    "terminal differs from exact reconstructed lineage"
                )
            observed = LF022ExecutionTerminalRecord.model_validate_json(terminal_path.read_bytes())
            if observed != expected:
                raise qa_module.LF022Prefix256QAError(
                    "terminal differs from exact reconstructed lineage"
                )
            bindings.append(
                LF022HistoricalTerminalBinding(
                    execution_task_id=replayed_task.task.execution_task_id,
                    terminal_id=observed.terminal_id,
                    terminal_artifact=LF022ArtifactBinding(
                        path=terminal_path.relative_to(root).as_posix(),
                        sha256=expected_sha,
                    ),
                )
            )
        return LF022HistoricalReplayResult(
            code_tree_hash="8" * 64,
            code_bundle_sha256=code_bundle_sha,
            network_calls_performed=0,
            module_bindings=(
                LF022HistoricalModuleBinding(
                    module_name="leanfaith.generation.llm_variants",
                    path=historical_module_path.relative_to(root).as_posix(),
                    sha256=historical_module_sha,
                ),
            ),
            terminal_bindings=tuple(bindings),
        )

    monkeypatch.setattr(
        qa_module,
        "run_lf022_historical_replay",
        exact_historical_replay,
    )

    failed = 256 - success_count
    statuses: dict[str, int] = {}
    if success_count:
        statuses["provisional_variants_created"] = success_count
    if failed:
        statuses["provider_exhausted"] = failed
    statuses = dict(sorted(statuses.items()))
    report_content: dict[str, object] = {
        "schema_version": 2,
        "batch_id": batch_id,
        "mode": "offline",
        "task_count": 256,
        "preflight_only_count": 0,
        "replayed_terminal_count": 256,
        "new_terminal_count": 0,
        "successful_terminal_count": success_count,
        "failed_terminal_count": failed,
        "error_count": 0,
        "network_calls_this_run": 0,
        "terminal_status_counts": statuses,
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
            **report_content,
            "report_id": make_id("lf022_batch_run", report_content),
        }
    )
    report_path = (
        root / manifest.batch_directory / "runs" / f"{report.report_id.split(':', 1)[1]}.json"
    )
    _write(report_path, canonical_json_bytes(report.model_dump(mode="json")))
    return manifest_path, report_path


def test_prefix256_operational_qa_passes_and_is_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay = _fixture(tmp_path, monkeypatch)
    output_dir = tmp_path / "reports/prefix256_qa"

    first = run_lf022_prefix256_operational_qa(
        repo_root=tmp_path,
        manifest_path=manifest,
        exact_offline_replay_report_path=replay,
        output_dir=output_dir,
    )
    second = run_lf022_prefix256_operational_qa(
        repo_root=tmp_path,
        manifest_path=manifest,
        exact_offline_replay_report_path=replay,
        output_dir=output_dir,
    )

    assert first.report == second.report
    assert first.report.qa_status == "passed"
    assert first.report.failure_codes == ()
    assert first.report.qa_implementation_code_tree_hash == "9" * 64
    assert first.report.historical_code_tree_hash == "8" * 64
    assert first.report.qa_implementation_code_tree_hash != first.report.historical_code_tree_hash
    assert first.report.historical_replay_network_calls == 0
    assert tuple(binding.module_name for binding in first.report.historical_module_bindings) == (
        "leanfaith.generation.llm_variants",
    )
    assert first.report.successful_terminal_count == 256
    assert first.report.verified_variant_count == 256
    assert len(first.report.terminal_replay_bindings) == 256
    assert tuple(
        binding.execution_task_id for binding in first.report.terminal_replay_bindings
    ) == tuple(
        sorted(binding.execution_task_id for binding in first.report.terminal_replay_bindings)
    )
    assert len(first.report.selected_task_ids) == LF022_PREFIX256_REVIEW_SAMPLE_SIZE
    lines = first.reviewer_bundle_path.read_bytes().splitlines()
    assert len(lines) == LF022_PREFIX256_REVIEW_SAMPLE_SIZE
    items = tuple(LF022Prefix256ReviewerItem.model_validate_json(line) for line in lines)
    assert tuple(item.selection_rank for item in items) == tuple(range(32))
    assert all(item.semantic_label_requested is False for item in items)
    persisted = LF022Prefix256OperationalQAReport.model_validate_json(
        first.report_path.read_bytes()
    )
    assert persisted == first.report


def test_prefix256_operational_qa_persists_threshold_no_go(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay = _fixture(tmp_path, monkeypatch, success_count=242)
    result = run_lf022_prefix256_operational_qa(
        repo_root=tmp_path,
        manifest_path=manifest,
        exact_offline_replay_report_path=replay,
        output_dir=tmp_path / "reports/threshold_no_go",
    )

    assert result.report.qa_status == "failed"
    assert result.report.failure_codes == ("successful_terminal_count_below_243",)
    assert result.report.successful_terminal_count == 242
    assert len(result.reviewer_bundle_path.read_bytes().splitlines()) == 32


def test_prefix256_operational_qa_persists_small_failure_reviewer_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay = _fixture(tmp_path, monkeypatch, success_count=17)
    result = run_lf022_prefix256_operational_qa(
        repo_root=tmp_path,
        manifest_path=manifest,
        exact_offline_replay_report_path=replay,
        output_dir=tmp_path / "reports/small_sample_no_go",
    )

    assert result.report.qa_status == "failed"
    assert result.report.failure_codes == (
        "review_sample_below_32",
        "successful_terminal_count_below_243",
    )
    assert len(result.report.selected_task_ids) == 17
    assert len(result.reviewer_bundle_path.read_bytes().splitlines()) == 17
    assert result.report_path.is_file()


def test_prefix256_operational_qa_persists_empty_failure_reviewer_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay = _fixture(tmp_path, monkeypatch, success_count=0)
    result = run_lf022_prefix256_operational_qa(
        repo_root=tmp_path,
        manifest_path=manifest,
        exact_offline_replay_report_path=replay,
        output_dir=tmp_path / "reports/empty_sample_no_go",
    )

    assert result.report.qa_status == "failed"
    assert result.report.failure_codes == (
        "review_sample_below_32",
        "successful_terminal_count_below_243",
    )
    assert result.report.selected_task_ids == ()
    assert result.reviewer_bundle_path.read_bytes() == b""
    assert result.report_path.is_file()


def test_prefix256_report_rejects_forged_pass_failure_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay = _fixture(tmp_path, monkeypatch, success_count=242)
    result = run_lf022_prefix256_operational_qa(
        repo_root=tmp_path,
        manifest_path=manifest,
        exact_offline_replay_report_path=replay,
        output_dir=tmp_path / "reports/derived_failure_set",
    )
    forged = result.report.model_dump(mode="json")
    forged["qa_status"] = "passed"
    forged["failure_codes"] = []
    forged["qa_id"] = make_id(
        "lf022_prefix256_qa",
        {key: value for key, value in forged.items() if key != "qa_id"},
    )

    with pytest.raises(ValueError, match="exact field-derived failure set"):
        LF022Prefix256OperationalQAReport.model_validate(forged)


@pytest.mark.parametrize(
    ("duplicate_first", "proof_body_first", "failure_code"),
    (
        (True, False, "duplicate_normalized_outputs_present"),
        (False, True, "candidate_hygiene_failure_present"),
    ),
)
def test_prefix256_operational_qa_rejects_global_hygiene_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duplicate_first: bool,
    proof_body_first: bool,
    failure_code: str,
) -> None:
    manifest, replay = _fixture(
        tmp_path,
        monkeypatch,
        duplicate_first=duplicate_first,
        proof_body_first=proof_body_first,
    )
    result = run_lf022_prefix256_operational_qa(
        repo_root=tmp_path,
        manifest_path=manifest,
        exact_offline_replay_report_path=replay,
        output_dir=tmp_path / "reports/hygiene_no_go",
    )

    assert result.report.qa_status == "failed"
    assert failure_code in result.report.failure_codes


def test_prefix256_operational_qa_rejects_post_replay_raw_artifact_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay = _fixture(tmp_path, monkeypatch)
    task_digest = f"{1:064x}"
    raw = (
        tmp_path
        / "data/lf022_execution/tasks"
        / task_digest[:2]
        / task_digest
        / "provider_raw/0000.json"
    )
    raw.write_bytes(b'{"tampered":true}\n')

    with pytest.raises(qa_module.LF022Prefix256QAError, match="hash differs"):
        run_lf022_prefix256_operational_qa(
            repo_root=tmp_path,
            manifest_path=manifest,
            exact_offline_replay_report_path=replay,
            output_dir=tmp_path / "reports/rejected_drift",
        )


def test_prefix256_operational_qa_reconstructs_terminal_after_aggregate_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay = _fixture(tmp_path, monkeypatch)
    task_digest = f"{1:064x}"
    task_directory = tmp_path / "data/lf022_execution/tasks" / task_digest[:2] / task_digest
    variants_path = task_directory / "provisional_variants.jsonl"
    original = VariantRecord.model_validate_json(variants_path.read_bytes())
    changed_statement = "theorem coherently_mutated_after_replay : (99 : Nat) = 99"
    changed = VariantRecord.model_validate(
        {
            **original.model_dump(mode="json"),
            "extracted_statement": changed_statement,
            "candidate_code_hash": sha256_hex(changed_statement.encode("utf-8")),
        }
    )
    variants_sha = _write(
        variants_path,
        canonical_json_bytes(changed.model_dump(mode="json")) + b"\n",
    )
    terminal_path = task_directory / "terminal.json"
    original_terminal = LF022ExecutionTerminalRecord.model_validate_json(terminal_path.read_bytes())
    terminal_content = original_terminal.model_dump(mode="json", exclude={"terminal_id"})
    terminal_content["variants_sha256"] = variants_sha
    changed_terminal = LF022ExecutionTerminalRecord.model_validate(
        {
            **terminal_content,
            "terminal_id": make_id("lf022_execution_terminal", terminal_content),
        }
    )
    _write(
        terminal_path,
        canonical_json_bytes(changed_terminal.model_dump(mode="json")) + b"\n",
    )

    with pytest.raises(
        qa_module.LF022Prefix256QAError,
        match="exact reconstructed lineage",
    ):
        run_lf022_prefix256_operational_qa(
            repo_root=tmp_path,
            manifest_path=manifest,
            exact_offline_replay_report_path=replay,
            output_dir=tmp_path / "reports/rejected_coherent_mutation",
        )


def test_prefix256_operational_qa_rejects_extra_task_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay = _fixture(tmp_path, monkeypatch)
    task_digest = f"{1:064x}"
    extra = (
        tmp_path
        / "data/lf022_execution/tasks"
        / task_digest[:2]
        / task_digest
        / "attempts/9999/orphan.json"
    )
    _write(extra, b'{"unexpected":true}\n')

    with pytest.raises(
        qa_module.LF022Prefix256QAError,
        match="artifact inventory differs",
    ):
        run_lf022_prefix256_operational_qa(
            repo_root=tmp_path,
            manifest_path=manifest,
            exact_offline_replay_report_path=replay,
            output_dir=tmp_path / "reports/rejected_extra_artifact",
        )


def test_prefix256_cli_reports_pass_without_semantic_credit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, replay = _fixture(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "qa-lf022-prefix256",
            "--root",
            str(tmp_path),
            "--manifest",
            str(manifest),
            "--offline-replay-report",
            str(replay),
            "--output-dir",
            str(tmp_path / "reports/cli_qa"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "qa_status=passed" in result.output
    assert "review_sample=32" in result.output
    assert "semantic_labels_created=0" in result.output
    assert "training_eligible=false" in result.output
