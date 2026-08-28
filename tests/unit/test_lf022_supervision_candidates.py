"""Fail-closed tests for LF-022 provisional supervision candidate inventories."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import leanfaith.generation.lf022_supervision_candidates as candidates_module
from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_batch import (
    LF022BatchRouteFreezeRequest,
    LF022BatchRouteManifest,
    LF022BatchTaskBinding,
    LF022PublicBatchManifest,
    make_lf022_batch_freeze_request,
)
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditInput,
    LF022VerifiedCodexAuditJudgment,
)
from leanfaith.generation.lf022_execution import (
    LF022ExecutionArtifacts,
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    LF022RCPDecodingContract,
    LF022RCPRetryPolicy,
    LF022RCPRouteBinding,
    make_lf022_g_open_execution_admission,
    make_lf022_g_open_execution_task,
)
from leanfaith.generation.lf022_executor import LF022ExecutionTerminalRecord
from leanfaith.generation.lf022_lean_check import (
    LF022LeanCheckAttempt,
    LF022LeanCheckManifest,
    LF022LeanCheckRecord,
    _check_record_id,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding, LF022ProductionTask
from leanfaith.generation.lf022_supervision_candidates import (
    CandidateArtifactBinding,
    LF022SupervisionCandidateError,
    LF022SupervisionCandidateManifest,
    LF022SupervisionCandidateRecord,
    LF022SupervisionCandidateSpec,
    PriorCodexDiagnostic,
    _judge_visible_payload_hash,
    _lexical_no_symlink_components,
    _load_spec,
    _record_values,
    _resolve_bound_path,
    _source_candidate_item_id,
    _validate_check_source_proposer_binding,
    _validate_variant_proposer_binding,
    _VerifiedLeanCheckSelector,
    _verify_lean_check_selector,
    build_lf022_supervision_candidate_inventory,
)
from leanfaith.generation.llm_variants import PublicLeanVariantSource
from leanfaith.generation.weak_supervision import JudgeResponse, PublicLeanJudgePair
from leanfaith.lean.protocol import LeanStatus
from leanfaith.schemas.enums import (
    GeneratorKind,
    IntendedRelation,
    Polarity,
    QualityTier,
    RelationLabel,
    ValidationStatus,
)
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.variant import VariantRecord


def _spec(**overrides: object) -> LF022SupervisionCandidateSpec:
    values: dict[str, object] = {
        "collection_id": "fixture",
        "proposer_family_id": "moonshot_kimi_k2",
        "proposer_model": "moonshotai/Kimi-K2.7-Code",
        "judge_a_family_id": "qwen3",
        "judge_b_family_id": "deepseek_v4",
        "primary_eval_judge_family_id": "openai_codex",
        "checks": {"path": "checks.jsonl", "sha256": "a" * 64},
        "lean_check_manifest": {"path": "lean_manifest.json", "sha256": "c" * 64},
        "codex_audit_manifest": {"path": "manifest.json", "sha256": "b" * 64},
    }
    values.update(overrides)
    return LF022SupervisionCandidateSpec.model_validate(values)


def _pair(*, optional_natural_language: str | None = None) -> PublicLeanJudgePair:
    return PublicLeanJudgePair(
        pair_id="pair:" + "a" * 64,
        canonical_lean_a="theorem source (n : Nat) : n = n",
        canonical_lean_b="theorem candidate (m : Nat) : m = m",
        optional_natural_language=optional_natural_language,
        source_record_ids=("thm:" + "b" * 64, "var:" + "c" * 64),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
    )


def _item(
    audit_digit: str,
    *,
    lean_check_id: str = "lf022_lean_check:" + "d" * 64,
) -> LF022CodexAuditInput:
    return LF022CodexAuditInput.model_construct(
        audit_item_id="lf022_codex_audit_item:" + audit_digit * 64,
        lean_check_id=lean_check_id,
        variant_id="var:" + "c" * 64,
        pair=_pair(),
    )


def _judgment(audit_digit: str) -> LF022VerifiedCodexAuditJudgment:
    return LF022VerifiedCodexAuditJudgment(
        audit_item_id="lf022_codex_audit_item:" + audit_digit * 64,
        lean_check_id="lf022_lean_check:" + "d" * 64,
        pair_id="pair:" + "a" * 64,
        variant_id="var:" + "c" * 64,
        source_record_ids=("thm:" + "b" * 64, "var:" + "c" * 64),
        source_theorem_id="thm:" + "b" * 64,
        source_representation_id="repr:" + "a" * 64,
        source_revision="fixture-revision",
        proposer_family_id="moonshot_kimi_k2",
        response=JudgeResponse(
            same_claim_answer="same_claim",
            relation=RelationLabel.EQUIVALENT,
            A_implies_B="yes",
            B_implies_A="yes",
            confidence=0.9,
            rationale="The binder rename preserves the claim.",
            needs_expert_review=False,
        ),
        final_message_sha256="e" * 64,
        parsed_response_sha256="f" * 64,
    )


def _variant(**overrides: object) -> VariantRecord:
    statement = "theorem candidate (m : Nat) : m = m"
    values: dict[str, object] = {
        "variant_id": "var:" + "c" * 64,
        "source_theorem_ids": ("thm:" + "b" * 64,),
        "source_representation_ids": ("repr:" + "1" * 64,),
        "context_id": "ctx:" + "2" * 64,
        "generator_kind": GeneratorKind.LLM_PROPOSER,
        "generator_id": "moonshotai/Kimi-K2.7-Code",
        "generation_config_hash": "3" * 64,
        "extracted_statement": statement,
        "candidate_code_hash": sha256_hex(statement.encode("utf-8")),
        "intended_relation": IntendedRelation.EQUIVALENT,
        "candidate_pool": "G_open",
        "validation_status": ValidationStatus.UNVALIDATED,
        "quality_tier": QualityTier.PROVISIONAL,
        "polarity_metadata": Polarity.POSITIVE,
        "metadata": {"proposer_family": "moonshotai/kimi-k2"},
    }
    values.update(overrides)
    return VariantRecord.model_validate(values)


def _artifact(path: str, digit: str) -> LF022ArtifactBinding:
    return LF022ArtifactBinding(path=path, sha256=digit * 64)


def _kimi_admission_and_task(
    *,
    theorem_digit: str,
    representation_digit: str,
    context_digit: str,
) -> tuple[LF022GOpenExecutionAdmission, LF022GOpenExecutionTask]:
    decoding = LF022RCPDecodingContract(
        contract_id="kimi_k2_7_public_smoke_v3",
        temperature=1.0,
        top_p=0.95,
        max_tokens=16384,
        seed=42,
        thinking_mode="forced_thinking",
        reasoning_effort="high",
        chat_template_enable_thinking=True,
    )
    route = LF022RCPRouteBinding(
        provider_id="epfl_rcp",
        model_id="moonshotai/Kimi-K2.7-Code",
        deployment_id="fixture-kimi",
        proposer_family_id="moonshot_kimi_k2",
        canonical_family="moonshotai/kimi-k2",
        catalog_snapshot_id="lf022_provider_catalog:" + "a" * 64,
        route_snapshot_revision="rcp-catalog-sha256:" + "3" * 64,
        underlying_checkpoint_revision_status="provider_not_disclosed",
        execution_scope="public_provisional_g_open",
        decoding=decoding,
    )
    retry = LF022RCPRetryPolicy(
        max_attempts=3,
        base_delay_seconds=1.0,
        maximum_delay_seconds=60.0,
        retryable_http_statuses=(408, 429, 500, 502, 503, 504),
    )
    artifacts = LF022ExecutionArtifacts(
        public_pool_audit=_artifact("inputs/public_pool.json", "1"),
        allocation_plan=_artifact("inputs/allocation.json", "2"),
        provider_catalog_raw=_artifact("inputs/catalog.json", "3"),
        provider_catalog_normalized=_artifact("inputs/catalog-normalized.json", "4"),
        reviewed_route_portfolio=_artifact("inputs/routes.yaml", "5"),
        reviewed_route_contract=_artifact("inputs/route.yaml", "6"),
        reviewed_route_evidence=_artifact("inputs/evidence.json", "7"),
        prompt_template=_artifact("inputs/prompt.txt", "8"),
        code_bundle=_artifact("inputs/code.tar", "9"),
    )
    admission = make_lf022_g_open_execution_admission(
        public_pool_audit_id="lf022_public_pool_audit:" + "c" * 64,
        allocation_plan_id="lf022_production_plan:" + "d" * 64,
        artifacts=artifacts,
        route=route,
        retry_policy=retry,
        code_tree_hash="e" * 64,
    )
    allocation_values: dict[str, object] = {
        "schema_version": 2,
        "task_kind": "non_executable_allocation",
        "admission_record_id": "lf022_source_admission:" + theorem_digit * 64,
        "source_locator_id": theorem_digit * 64,
        "theorem_id": "thm:" + theorem_digit * 64,
        "representation_id": "repr:" + representation_digit * 64,
        "context_id": "ctx:" + context_digit * 64,
        "distribution": "G_open",
        "proposer_family_id": "moonshot_kimi_k2",
        "judge_family_ids": ("qwen3", "glm5"),
        "sci_validator_family_id": None,
        "heldout_eval_family_id": "openai_codex",
        "heldout_eval_supervision_excluded": True,
        "execution_binding_id": None,
        "executable": False,
        "network_execution_authorized": False,
        "semantic_label_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
    }
    allocation = LF022ProductionTask.model_validate(
        {
            **allocation_values,
            "task_id": make_id("lf022_production_task", allocation_values),
        }
    )
    source = PublicLeanVariantSource(
        source_theorem_id=allocation.theorem_id,
        source_representation_id=allocation.representation_id,
        context_id=allocation.context_id,
        imports=("Mathlib",),
        source_statement="theorem source (n : Nat) : n = n",
        source_id="mathlib",
        source_revision="revision",
        source_license="Apache-2.0",
        source_is_public=True,
        external_transmission_allowed=True,
        denylist_checked=True,
    )
    task = make_lf022_g_open_execution_task(
        admission=admission,
        allocation_task=allocation,
        source=source,
    )
    return admission, task


def _write_canonical_model(
    repo_root: Path,
    relative: str,
    model: StrictModel,
) -> LF022ArtifactBinding:
    path = repo_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(model.model_dump(mode="json")) + b"\n")
    return LF022ArtifactBinding(path=relative, sha256=hash_file(path))


def test_same_pair_id_uses_one_canonical_audit_item_for_dispatch() -> None:
    canonical = _source_candidate_item_id(_item("1"))
    values_a = _record_values(
        spec=_spec(),
        item=_item("1"),
        judgment=_judgment("1"),
        canonical_pair_id=_pair().pair_id,
        canonical_source_item_id=canonical,
        codex_model="gpt-5.6-sol",
        codex_reasoning_effort="xhigh",
    )
    values_b = _record_values(
        spec=_spec(),
        item=_item("2", lean_check_id="lf022_lean_check:" + "e" * 64),
        judgment=_judgment("2"),
        canonical_pair_id=_pair().pair_id,
        canonical_source_item_id=canonical,
        codex_model="gpt-5.6-sol",
        codex_reasoning_effort="xhigh",
    )

    record_a = LF022SupervisionCandidateRecord.model_validate(
        {
            **values_a,
            "candidate_inventory_record_id": make_id("lf022_supervision_candidate", values_a),
        }
    )
    record_b = LF022SupervisionCandidateRecord.model_validate(
        {
            **values_b,
            "candidate_inventory_record_id": make_id("lf022_supervision_candidate", values_b),
        }
    )
    assert record_a.dispatch_status == "ready_for_two_family_judging"
    assert record_a.source_candidate_item_id == canonical
    assert record_a.canonical_dispatch_audit_item_id is None
    assert record_a.candidate_state == "unresolved_awaiting_two_family_judging"
    assert record_a.required_judgment_cells == (
        "judge_A:AB",
        "judge_A:BA",
        "judge_B:AB",
        "judge_B:BA",
    )
    assert record_b.dispatch_status == "exact_duplicate_not_dispatched"
    assert record_b.required_judgment_cells == ()


def test_visible_payload_hash_keeps_identical_lean_with_different_nl_distinct() -> None:
    without_nl = _pair(optional_natural_language=None)
    with_nl = _pair(optional_natural_language="Every natural number equals itself.")
    assert without_nl.canonical_lean_a == with_nl.canonical_lean_a
    assert without_nl.canonical_lean_b == with_nl.canonical_lean_b
    assert _judge_visible_payload_hash(without_nl) != _judge_visible_payload_hash(with_nl)


def test_bound_artifact_rejects_a_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(LF022SupervisionCandidateError, match="symlinked component"):
        _resolve_bound_path(
            CandidateArtifactBinding(path=str(link), sha256=hash_file(target)),
            repo_root=tmp_path,
        )


def test_spec_rejects_a_symlinked_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    spec = _spec()
    spec_path = real / "spec.json"
    spec_path.write_bytes(canonical_json_bytes(spec.model_dump(mode="json")) + b"\n")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(LF022SupervisionCandidateError, match="symlinked component"):
        _load_spec(
            repo_root=tmp_path,
            spec_path=alias / "spec.json",
            expected_spec_sha256=hash_file(spec_path),
        )


def test_output_directory_rejects_a_symlinked_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(LF022SupervisionCandidateError, match="symlinked component"):
        _lexical_no_symlink_components(
            alias / "inventory",
            base=tmp_path,
            label="candidate inventory output directory",
            allow_missing_leaf=True,
        )


def test_variant_model_and_family_are_bound_to_frozen_spec() -> None:
    variant = _variant()
    _validate_variant_proposer_binding(
        variant=variant,
        judgment_proposer_family_id="moonshot_kimi_k2",
        judgment_variant_id=variant.variant_id,
        spec=_spec(),
    )

    with pytest.raises(LF022SupervisionCandidateError, match="model/family"):
        _validate_variant_proposer_binding(
            variant=variant,
            judgment_proposer_family_id="moonshot_kimi_k2",
            judgment_variant_id=variant.variant_id,
            spec=_spec(proposer_model="moonshotai/Kimi-K2.6"),
        )
    with pytest.raises(LF022SupervisionCandidateError, match="audit proposer family"):
        _validate_variant_proposer_binding(
            variant=variant,
            judgment_proposer_family_id="qwen3",
            judgment_variant_id=variant.variant_id,
            spec=_spec(),
        )


def test_direct_check_source_binds_the_frozen_allocation_family() -> None:
    variant = _variant()
    source = SimpleNamespace(
        source_theorem_id=variant.source_theorem_ids[0],
        source_representation_id=variant.source_representation_ids[0],
        context_id=variant.context_id,
    )
    matching_task = SimpleNamespace(
        allocation_task=SimpleNamespace(proposer_family_id="moonshot_kimi_k2"),
        source=source,
    )
    _validate_check_source_proposer_binding(
        variant=variant,
        task=matching_task,  # type: ignore[arg-type]
        spec=_spec(),
    )

    mismatched_task = SimpleNamespace(
        allocation_task=SimpleNamespace(proposer_family_id="qwen3"),
        source=source,
    )
    with pytest.raises(LF022SupervisionCandidateError, match="source task proposer family"):
        _validate_check_source_proposer_binding(
            variant=variant,
            task=mismatched_task,  # type: ignore[arg-type]
            spec=_spec(),
        )

    mismatched_source_task = SimpleNamespace(
        allocation_task=SimpleNamespace(proposer_family_id="moonshot_kimi_k2"),
        source=SimpleNamespace(
            source_theorem_id="thm:" + "e" * 64,
            source_representation_id=variant.source_representation_ids[0],
            context_id=variant.context_id,
        ),
    )
    with pytest.raises(LF022SupervisionCandidateError, match="source variant lineage"):
        _validate_check_source_proposer_binding(
            variant=variant,
            task=mismatched_source_task,  # type: ignore[arg-type]
            spec=_spec(),
        )


def _terminal(
    *,
    task_id: str,
    status: str,
    admission_id: str = "lf022_execution_admission:" + "a" * 64,
    variants_artifact: str | None = None,
    variants_sha256: str | None = None,
    variant_count: int = 0,
) -> LF022ExecutionTerminalRecord:
    values: dict[str, object] = {
        "schema_version": 1,
        "execution_admission_id": admission_id,
        "execution_task_id": task_id,
        "status": status,
        "attempt_artifacts": ("attempt.json",),
        "attempt_sha256s": ("1" * 64,),
        "llm_attempt_artifacts": ("llm_attempt.json",),
        "llm_attempt_sha256s": ("2" * 64,),
        "llm_call_id": "call:" + "3" * 64,
        "llm_call_artifact": "call.json",
        "llm_call_sha256": "4" * 64,
        "variants_artifact": variants_artifact,
        "variants_sha256": variants_sha256,
        "provisional_variant_count": variant_count,
        "terminal_error_code": None if status == "provisional_variants_created" else status,
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
    return LF022ExecutionTerminalRecord.model_validate(
        {
            **values,
            "terminal_id": make_id("lf022_execution_terminal", values),
        }
    )


def _valid_check(tmp_path: Path) -> LF022LeanCheckRecord:
    variant = _variant()
    attempt = LF022LeanCheckAttempt(
        attempt_index=0,
        request_hash="5" * 64,
        lean_status=LeanStatus.VALID,
        elapsed_ms=1,
    )
    values: dict[str, object] = {
        "schema_version": 1,
        "method_version": "lf022_provisional_lean_check_v1",
        "variant_id": variant.variant_id,
        "source_variant_artifact": "executor/provisional_variants.jsonl",
        "source_variant_artifact_sha256": "6" * 64,
        "source_variant_line_number": 1,
        "source_variant_line_sha256": "7" * 64,
        "candidate_code_hash": variant.candidate_code_hash,
        "context_id": variant.context_id,
        "source_id": "mathlib",
        "source_revision": "revision",
        "project_dir": str(tmp_path),
        "project_revision": "revision",
        "import_header": "import Mathlib\n",
        "import_header_sha256": sha256_hex(b"import Mathlib\n"),
        "request_id": "fixture-request",
        "request_code_sha256": "8" * 64,
        "lean_status": LeanStatus.VALID,
        "validation_status": ValidationStatus.ELABORATES,
        "outcome": "elaborates",
        "declaration_verified": True,
        "fresh_invalid_confirmation_enabled": True,
        "attempts": (attempt,),
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
    }
    draft = LF022LeanCheckRecord.model_construct(
        check_id="lf022_lean_check:" + "0" * 64,
        **values,
    )
    return LF022LeanCheckRecord.model_validate({**values, "check_id": _check_record_id(draft)})


def _historical_batch_selector_fixture(
    tmp_path: Path,
) -> tuple[
    LF022LeanCheckManifest,
    LF022GOpenExecutionTask,
    LF022GOpenExecutionTask,
    bytes,
    Path,
]:
    admission, success_task = _kimi_admission_and_task(
        theorem_digit="b",
        representation_digit="1",
        context_digit="2",
    )
    same_admission, failed_task = _kimi_admission_and_task(
        theorem_digit="f",
        representation_digit="3",
        context_digit="4",
    )
    assert same_admission == admission
    variant = _variant()
    output_root = tmp_path / "data/lf022_execution"
    task_directories: dict[str, Path] = {}
    for task in (success_task, failed_task):
        digest = task.execution_task_id.split(":", 1)[1]
        task_dir = output_root / "tasks" / digest[:2] / digest
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_bytes(
            canonical_json_bytes(task.model_dump(mode="json")) + b"\n"
        )
        task_directories[task.execution_task_id] = task_dir
    success_dir = task_directories[success_task.execution_task_id]
    failed_dir = task_directories[failed_task.execution_task_id]
    variant_line = canonical_json_bytes(variant.model_dump(mode="json")) + b"\n"
    variants_path = success_dir / "provisional_variants.jsonl"
    variants_path.write_bytes(variant_line)
    relative_variants = variants_path.relative_to(tmp_path).as_posix()
    success = _terminal(
        task_id=success_task.execution_task_id,
        status="provisional_variants_created",
        admission_id=admission.admission_id,
        variants_artifact=relative_variants,
        variants_sha256=hash_file(variants_path),
        variant_count=1,
    )
    failed = _terminal(
        task_id=failed_task.execution_task_id,
        status="provider_exhausted",
        admission_id=admission.admission_id,
    )
    (success_dir / "terminal.json").write_bytes(
        canonical_json_bytes(success.model_dump(mode="json")) + b"\n"
    )
    (failed_dir / "terminal.json").write_bytes(
        canonical_json_bytes(failed.model_dump(mode="json")) + b"\n"
    )
    request = make_lf022_batch_freeze_request(
        batch_directory="batch",
        executor_output_root="data/lf022_execution",
        routes=(
            LF022BatchRouteFreezeRequest(
                proposer_family_id="moonshot_kimi_k2",
                public_pool_audit_id=admission.public_pool_audit_id,
                allocation_plan_id=admission.allocation_plan_id,
                execution_artifacts=admission.artifacts,
                route=admission.route,
                retry_policy=admission.retry_policy,
                code_tree_hash=admission.code_tree_hash,
                allocation_task_ids=tuple(
                    sorted(
                        (
                            success_task.allocation_task.task_id,
                            failed_task.allocation_task.task_id,
                        )
                    )
                ),
            ),
        ),
    )
    request_binding = _write_canonical_model(
        tmp_path,
        "batch/freeze_request.json",
        request,
    )
    admission_binding = _write_canonical_model(
        tmp_path,
        "batch/admission.json",
        admission,
    )
    task_bindings: list[LF022BatchTaskBinding] = []
    frozen_task_paths: dict[str, Path] = {}
    for task in sorted(
        (success_task, failed_task),
        key=lambda item: item.execution_task_id,
    ):
        digest = task.execution_task_id.split(":", 1)[1]
        relative = f"batch/tasks/{digest}.json"
        binding = _write_canonical_model(tmp_path, relative, task)
        frozen_task_paths[task.execution_task_id] = tmp_path / relative
        task_bindings.append(
            LF022BatchTaskBinding(
                allocation_task_id=task.allocation_task.task_id,
                execution_task_id=task.execution_task_id,
                task=binding,
            )
        )
    route = LF022BatchRouteManifest(
        proposer_family_id="moonshot_kimi_k2",
        model_id="moonshotai/Kimi-K2.7-Code",
        execution_scope="public_provisional_g_open",
        qualification_state="production_route_reviewed",
        admission_id=admission.admission_id,
        admission=admission_binding,
        public_pool_audit_id=admission.public_pool_audit_id,
        allocation_plan_id=admission.allocation_plan_id,
        tasks=tuple(task_bindings),
    )
    batch_values: dict[str, object] = {
        "schema_version": 1,
        "status": "frozen_offline_ready",
        "freeze_request": request_binding.model_dump(mode="json"),
        "freeze_request_id": request.request_id,
        "batch_directory": "batch",
        "executor_output_root": "data/lf022_execution",
        "journal_directory": "batch/journal",
        "routes": (route.model_dump(mode="json"),),
        "total_task_count": 2,
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
    batch = LF022PublicBatchManifest.model_validate(
        {
            **batch_values,
            "batch_id": make_id("lf022_public_batch", batch_values),
        }
    )
    selector_path = tmp_path / "batch/batch_manifest.json"
    selector_path.write_bytes(canonical_json_bytes(batch.model_dump(mode="json")) + b"\n")
    manifest = LF022LeanCheckManifest.model_construct(
        selection_batch_id=batch.batch_id,
        selection_batch_manifest="batch/batch_manifest.json",
        selection_batch_manifest_sha256=hash_file(selector_path),
        selection_postgen_selector_id=None,
        selection_postgen_selector=None,
        selection_postgen_selector_sha256=None,
        selected_execution_task_count=2,
    )
    return (
        manifest,
        success_task,
        failed_task,
        variant_line,
        frozen_task_paths[success_task.execution_task_id],
    )


def test_selector_replay_allows_failed_tasks_but_returns_exact_success_variants(
    tmp_path: Path,
) -> None:
    manifest, success_task, failed_task, variant_line, _ = _historical_batch_selector_fixture(
        tmp_path
    )

    verified = _verify_lean_check_selector(repo_root=tmp_path, manifest=manifest)

    assert verified.expected_variants == (
        (success_task.execution_task_id, _variant().variant_id, sha256_hex(variant_line)),
    )
    assert verified.frozen_tasks_by_id == {
        success_task.execution_task_id: success_task,
        failed_task.execution_task_id: failed_task,
    }


def test_selector_replay_rejects_tampered_bound_frozen_task(tmp_path: Path) -> None:
    manifest, _, _, _, frozen_task_path = _historical_batch_selector_fixture(tmp_path)
    frozen_task_path.write_bytes(frozen_task_path.read_bytes() + b" ")

    with pytest.raises(LF022SupervisionCandidateError, match="hash mismatch"):
        _verify_lean_check_selector(repo_root=tmp_path, manifest=manifest)


def test_selector_replay_rejects_adjacent_executor_task_drift(tmp_path: Path) -> None:
    manifest, success_task, failed_task, _, _ = _historical_batch_selector_fixture(tmp_path)
    digest = success_task.execution_task_id.split(":", 1)[1]
    adjacent_path = tmp_path / "data/lf022_execution/tasks" / digest[:2] / digest / "task.json"
    adjacent_path.write_bytes(canonical_json_bytes(failed_task.model_dump(mode="json")) + b"\n")

    with pytest.raises(LF022SupervisionCandidateError, match="adjacent executor task differs"):
        _verify_lean_check_selector(repo_root=tmp_path, manifest=manifest)


def test_v3_builds_directly_from_lean_checks_without_codex_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = _valid_check(tmp_path)
    checks_path = tmp_path / "checks.jsonl"
    checks_path.write_bytes(canonical_json_bytes(check.model_dump(mode="json")) + b"\n")
    lean_manifest_path = tmp_path / "lean_manifest.json"
    lean_manifest_path.write_bytes(b"{}")
    spec = _spec(
        checks={"path": str(checks_path), "sha256": hash_file(checks_path)},
        lean_check_manifest={
            "path": str(lean_manifest_path),
            "sha256": hash_file(lean_manifest_path),
        },
        codex_audit_manifest=None,
    )
    spec_path = tmp_path / "spec.json"
    spec_path.write_bytes(canonical_json_bytes(spec.model_dump(mode="json")) + b"\n")
    _, source_task = _kimi_admission_and_task(
        theorem_digit="b",
        representation_digit="1",
        context_digit="2",
    )
    item = _item("1", lean_check_id=check.check_id)
    selector = _VerifiedLeanCheckSelector(
        expected_variants=(
            (
                source_task.execution_task_id,
                check.variant_id,
                check.source_variant_line_sha256,
            ),
        ),
        frozen_tasks_by_id={source_task.execution_task_id: source_task},
    )
    monkeypatch.setattr(
        candidates_module,
        "_verify_lean_check_manifest",
        lambda **_kwargs: (
            LF022LeanCheckManifest.model_construct(),
            selector,
        ),
    )
    monkeypatch.setattr(
        candidates_module,
        "_load_check_source_lineage",
        lambda **_kwargs: (_variant(), source_task),
    )
    monkeypatch.setattr(
        candidates_module,
        "load_lean_valid_audit_inputs",
        lambda **_kwargs: (item,),
    )
    monkeypatch.setattr(
        candidates_module,
        "verify_completed_lf022_codex_audit",
        lambda **_kwargs: pytest.fail("absent Codex diagnostic must not be replayed"),
    )

    records, manifest = build_lf022_supervision_candidate_inventory(
        repo_root=tmp_path,
        spec_path=spec_path,
        expected_spec_sha256=hash_file(spec_path),
    )

    assert len(records) == 1
    record = records[0]
    assert record.schema_version == 3
    assert record.prior_codex_diagnostic is None
    assert record.candidate_state == "unresolved_awaiting_two_family_judging"
    assert record.training_eligible is False
    assert record.gate_credit_claimed is False
    assert "same_claim" not in record.model_dump(mode="json")
    assert "relation" not in record.model_dump(mode="json")
    assert manifest.schema_version == 3
    assert manifest.codex_diagnostic_status == "absent"
    assert manifest.codex_diagnostic_record_count == 0
    assert manifest.codex_audit_manifest_sha256 is None
    assert manifest.codex_same_claim_counts == {}
    assert manifest.training_eligible is False
    assert manifest.gate_credit_claimed is False

    _, mismatched_executor_task = _kimi_admission_and_task(
        theorem_digit="f",
        representation_digit="3",
        context_digit="4",
    )
    monkeypatch.setattr(
        candidates_module,
        "_load_check_source_lineage",
        lambda **_kwargs: (_variant(), mismatched_executor_task),
    )
    with pytest.raises(LF022SupervisionCandidateError, match="adjacent executor task differs"):
        build_lf022_supervision_candidate_inventory(
            repo_root=tmp_path,
            spec_path=spec_path,
            expected_spec_sha256=hash_file(spec_path),
        )


def test_v2_record_and_manifest_remain_byte_canonical_on_read() -> None:
    pair = _pair()
    record_values: dict[str, object] = {
        "schema_version": 2,
        "collection_id": "legacy",
        "pair_id": pair.pair_id,
        "variant_id": "var:" + "c" * 64,
        "lean_check_id": "lf022_lean_check:" + "d" * 64,
        "proposer_family_id": "moonshot_kimi_k2",
        "proposer_model": "moonshotai/Kimi-K2.7-Code",
        "pair": pair.model_dump(mode="json"),
        "pair_admission_sha256": pair.admission_sha256,
        "judge_visible_payload_sha256": _judge_visible_payload_hash(pair),
        "dispatch_status": "ready_for_two_family_judging",
        "canonical_dispatch_pair_id": pair.pair_id,
        "canonical_dispatch_audit_item_id": "lf022_codex_audit_item:" + "1" * 64,
        "required_judgment_cells": (
            "judge_A:AB",
            "judge_A:BA",
            "judge_B:AB",
            "judge_B:BA",
        ),
        "prior_codex_diagnostic": PriorCodexDiagnostic(
            audit_item_id="lf022_codex_audit_item:" + "1" * 64,
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            same_claim_answer="same_claim",
            relation="equivalent",
            confidence=0.9,
            needs_expert_review=False,
            parsed_response_sha256="f" * 64,
        ).model_dump(mode="json"),
        "promotion_blockers": ("two_family_judgments_missing",),
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    legacy_record = {
        **record_values,
        "candidate_inventory_record_id": make_id("lf022_supervision_candidate", record_values),
    }
    raw_record = canonical_json_bytes(legacy_record)
    parsed_record = LF022SupervisionCandidateRecord.model_validate_json(raw_record)
    assert canonical_json_bytes(parsed_record.model_dump(mode="json")) == raw_record

    manifest_values: dict[str, object] = {
        "schema_version": 2,
        "method_version": "lf022_supervision_candidate_inventory_v2",
        "collection_id": "legacy",
        "spec_sha256": "1" * 64,
        "checks_sha256": "2" * 64,
        "codex_audit_manifest_sha256": "3" * 64,
        "logical_input_binding_sha256": "4" * 64,
        "codex_response_artifact_set_sha256": "5" * 64,
        "proposer_family_id": "moonshot_kimi_k2",
        "proposer_model": "moonshotai/Kimi-K2.7-Code",
        "judge_a_family_id": "qwen3",
        "judge_b_family_id": "deepseek_v4",
        "primary_eval_judge_family_id": "openai_codex",
        "records_artifact": "candidates.jsonl",
        "records_sha256": sha256_hex(raw_record + b"\n"),
        "public_sample_artifact": "public_sample.jsonl",
        "public_sample_sha256": sha256_hex(raw_record + b"\n"),
        "public_sample_count": 1,
        "summary_artifact": "summary.md",
        "summary_sha256": "6" * 64,
        "record_count": 1,
        "unique_judge_visible_payload_count": 1,
        "exact_duplicate_record_count": 0,
        "dispatch_eligible_count": 1,
        "required_future_judge_call_count": 4,
        "codex_same_claim_counts": {"same_claim": 1},
        "dispatch_status_counts": {"ready_for_two_family_judging": 1},
        "codex_is_diagnostic_only": True,
        "two_family_judgments_completed": False,
        "human_pilot_bound": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    legacy_manifest = {
        **manifest_values,
        "inventory_id": make_id(
            "lf022_supervision_inventory",
            {
                key: value
                for key, value in manifest_values.items()
                if key
                not in {
                    "records_artifact",
                    "public_sample_artifact",
                    "summary_artifact",
                    "spec_sha256",
                }
            },
        ),
    }
    raw_manifest = canonical_json_bytes(legacy_manifest)
    parsed_manifest = LF022SupervisionCandidateManifest.model_validate_json(raw_manifest)
    assert canonical_json_bytes(parsed_manifest.model_dump(mode="json")) == raw_manifest


def test_candidate_spec_requires_four_distinct_families() -> None:
    with pytest.raises(ValueError, match="four distinct families"):
        _spec(judge_b_family_id="qwen3")
