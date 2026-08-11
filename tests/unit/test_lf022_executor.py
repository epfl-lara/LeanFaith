"""Offline-only tests for the public LF-022 proposer executor."""

from __future__ import annotations

import datetime
import fcntl
import json
import subprocess
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.cli.lf022_batch import select_public_g_open_plan_window
from leanfaith.config.code_bundle import freeze_code_bundle
from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import DenylistIndex, FrozenBenchmark, FrozenRegistry
from leanfaith.generation.lf022_admission_freeze import (
    LF022AdmissionFreezeError,
    freeze_lf022_diagnostic_execution_admission,
    freeze_lf022_scientific_kimi_execution_admission,
    freeze_lf022_scientific_qualified_execution_admission,
)
from leanfaith.generation.lf022_batch import (
    LF022BatchError,
    LF022BatchFreezeRequest,
    LF022BatchRouteFreezeRequest,
    LF022BatchRunPolicy,
    RateLimitedRCPTransport,
    audit_lf022_g_open_source_eligibility,
    freeze_lf022_public_batch,
    make_lf022_batch_freeze_request,
    run_lf022_public_batch,
)
from leanfaith.generation.lf022_execution import (
    LF022ExecutionArtifacts,
    LF022ExecutionError,
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    LF022RCPDecodingContract,
    LF022RCPRetryPolicy,
    LF022RCPRouteBinding,
    load_lf022_execution_task_inputs,
    make_lf022_g_open_execution_admission,
    make_lf022_g_open_execution_task,
    make_lf022_named_signature,
    make_lf022_qualification_claim,
    verify_lf022_execution_admission,
)
from leanfaith.generation.lf022_executor import (
    LF022ExecutorError,
    LF022TaskLockedError,
    RCPRuntimeCredentials,
    _historical_response_error_matches,
    execute_lf022_g_open_task,
    prepare_lf022_g_open_execution,
)
from leanfaith.generation.lf022_kimi_v4_eligibility import (
    LF022_KIMI_V4_ELIGIBILITY_PATH,
    LF022KimiV4ProductionEligibility,
)
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022JSONLArtifactBinding,
    LF022ProductionArtifactSet,
    LF022ProductionFamilyMatrix,
    LF022ProductionPlanManifest,
    LF022ProductionTask,
    LF022ProviderDeployment,
    make_lf022_benchmark_registry_manifest,
    make_lf022_denylist_clearance_record,
    make_lf022_production_family_matrix,
    make_lf022_production_source_record,
    make_lf022_provider_catalog_snapshot,
    make_lf022_public_source_authorization,
    make_lf022_public_source_authorization_registry,
)
from leanfaith.generation.lf022_public_pool import (
    LF022PublicPoolAudit,
    LF022PublicPoolOutputArtifacts,
)
from leanfaith.generation.lf022_route_qualification import (
    LF022QualifiedProposerProductionEligibility,
    LF022RouteQualificationError,
    certify_lf022_proposer_production_eligibility,
    supersede_lf022_failed_qualification,
    verify_lf022_proposer_production_eligibility,
    verify_lf022_qualification_supersession,
)
from leanfaith.generation.llm_variants import PublicLeanVariantSource
from leanfaith.generation.rcp_provider import (
    RCPHTTPTransport,
    RCPTransportUnknownError,
    RCPWireResponse,
)
from leanfaith.schemas.enums import QualityTier, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.manifest import collect_code_state
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import VariantRecord

NOW = datetime.datetime(2026, 7, 30, 12, 0, tzinfo=datetime.UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_historical_output_budget_error_replays_without_rewriting_artifact() -> None:
    assert _historical_response_error_matches(
        recorded="empty_response",
        observed="output_budget_exhausted",
    )
    assert not _historical_response_error_matches(
        recorded="invalid_response_shape",
        observed="output_budget_exhausted",
    )


def _write_json(root: Path, relative: str, value: object) -> LF022ArtifactBinding:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))
    return LF022ArtifactBinding(path=relative, sha256=hash_file(path))


def _write_bytes(root: Path, relative: str, value: bytes) -> LF022ArtifactBinding:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return LF022ArtifactBinding(path=relative, sha256=hash_file(path))


def _copy_repo_artifact(root: Path, relative: str) -> LF022ArtifactBinding:
    return _write_bytes(root, relative, (REPOSITORY_ROOT / relative).read_bytes())


def _write_jsonl(
    root: Path,
    relative: str,
    records: tuple[StrictModel, ...],
) -> LF022JSONLArtifactBinding:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )
    path.write_bytes(payload)
    return LF022JSONLArtifactBinding(
        path=relative,
        sha256=hash_file(path),
        record_count=len(records),
    )


def _code_bundle(root: Path, *, code_tree_hash: str) -> LF022ArtifactBinding:
    path, digest, state = freeze_code_bundle(root, root / "artifacts")
    assert state.code_tree_hash == code_tree_hash
    return LF022ArtifactBinding(
        path=path.relative_to(root).as_posix(),
        sha256=digest,
    )


def _task(
    *,
    distribution: str,
    proposer_family_id: str,
    source_admission_id: str,
    theorem_id: str,
    representation_id: str,
    context_id: str,
) -> LF022ProductionTask:
    judge_roles = {
        "moonshot_kimi_k2": ("qwen3", "glm5"),
        "qwen3": ("moonshot_kimi_k2", "glm5"),
        "glm5": ("moonshot_kimi_k2", "qwen3"),
    }[proposer_family_id]
    payload: dict[str, object] = {
        "schema_version": 2,
        "task_kind": "non_executable_allocation",
        "admission_record_id": source_admission_id,
        "source_locator_id": "1" * 64,
        "theorem_id": theorem_id,
        "representation_id": representation_id,
        "context_id": context_id,
        "distribution": distribution,
        "proposer_family_id": proposer_family_id,
        "judge_family_ids": list(judge_roles),
        "sci_validator_family_id": ("deepseek_v4" if distribution == "G_sci" else None),
        "heldout_eval_family_id": "openai_codex",
        "heldout_eval_supervision_excluded": True,
        "execution_binding_id": None,
        "executable": False,
        "network_execution_authorized": False,
        "semantic_label_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
    }
    return LF022ProductionTask.model_validate(
        {
            **payload,
            "task_id": make_id("lf022_production_task", payload),
        }
    )


def _plan(
    *,
    g_sci: LF022ProductionTask,
    g_open: LF022ProductionTask,
    artifacts: LF022ProductionArtifactSet,
    family_matrix: LF022ProductionFamilyMatrix,
    profile: Literal[
        "diagnostic_scaffold",
        "pilot_scaffold",
        "scientific_production_scaffold",
    ] = "diagnostic_scaffold",
) -> LF022ProductionPlanManifest:
    payload: dict[str, object] = {
        "schema_version": 2,
        "profile": profile,
        "scientific_status": {
            "diagnostic_scaffold": "diagnostic_only",
            "pilot_scaffold": "pilot_only",
            "scientific_production_scaffold": "scientific_allocation_scaffold",
        }[profile],
        "artifact_class": "allocation_scaffold",
        "status": "non_executable_allocation_complete",
        "admission_id": f"lf022_production_admission:{'2' * 64}",
        "family_matrix_id": family_matrix.matrix_id,
        "family_matrix_sha256": hash_canonical(family_matrix.model_dump(mode="json")),
        "artifacts": artifacts.model_dump(mode="json"),
        "unique_source_count": 1,
        "source_admission_record_ids": [g_open.admission_record_id],
        "tasks": [
            g_sci.model_dump(mode="json"),
            g_open.model_dump(mode="json"),
        ],
        "execution_binding_status": "absent",
        "execution_bindings_present": False,
        "network_execution_authorized": False,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
    }
    return LF022ProductionPlanManifest.model_validate(
        {
            **payload,
            "manifest_id": make_id("lf022_production_plan", payload),
        }
    )


def _audit(
    *,
    root: Path,
    plan_binding: LF022ArtifactBinding,
    dummy: LF022ArtifactBinding,
    family_matrix: LF022ArtifactBinding,
    source_pool: LF022JSONLArtifactBinding,
    theorem_records: LF022JSONLArtifactBinding,
    representation_records: LF022JSONLArtifactBinding,
    context_records: LF022JSONLArtifactBinding,
    source_authorization_registry: LF022ArtifactBinding,
    benchmark_registry_manifest: LF022ArtifactBinding,
    active_benchmark_registry: LF022ArtifactBinding,
    active_benchmark_registry_content_hash: str,
    denylist_clearance_records: LF022JSONLArtifactBinding,
    theorem_id: str,
    profile: Literal[
        "diagnostic_scaffold",
        "pilot_scaffold",
        "scientific_production_scaffold",
    ] = "diagnostic_scaffold",
) -> tuple[LF022PublicPoolAudit, LF022ArtifactBinding]:
    outputs = LF022PublicPoolOutputArtifacts(
        family_matrix=family_matrix,
        upstream_extraction_output_manifest=dummy,
        upstream_representation_output_manifest=dummy,
        mathlib_source_frame=dummy,
        extraction_manifests={"mathlib@rev": dummy},
        source_authorizations={"auth": dummy},
        public_source_authorization_registry=source_authorization_registry,
        benchmark_registry_manifest=benchmark_registry_manifest,
        denylist_clearance_records=denylist_clearance_records,
        source_pool=source_pool,
        theorem_records=theorem_records,
        representation_records=representation_records,
        context_records=context_records,
        admission=dummy,
        production_plan=plan_binding,
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "selection_version": "lf022_public_pool_hash_rank_v1",
        "profile": profile,
        "requested_count": 1,
        "input_theorems": theorem_records.model_dump(mode="json"),
        "input_representations": representation_records.model_dump(mode="json"),
        "input_contexts": context_records.model_dump(mode="json"),
        "input_extraction_output_manifest": dummy.model_dump(mode="json"),
        "input_representation_output_manifest": dummy.model_dump(mode="json"),
        "input_mathlib_source_frame": dummy.model_dump(mode="json"),
        "extraction_run_id": "run_fixture",
        "representation_run_id": "run_fixture",
        "mathlib_source_frame_id": f"mathlib_frame:{'5' * 64}",
        "active_benchmark_registry": active_benchmark_registry.model_dump(mode="json"),
        "active_benchmark_registry_content_hash": active_benchmark_registry_content_hash,
        "input_theorem_count": 1,
        "input_representation_count": 1,
        "input_context_count": 1,
        "orphan_representation_count": 0,
        "unused_context_count": 0,
        "eligible_count": 1,
        "eligible_unique_ancestry_count": 1,
        "eligible_not_selected_count": 0,
        "selected_count": 1,
        "selected_unique_ancestry_count": 1,
        "rejection_counts": dict.fromkeys(
            (
                "private_source",
                "unapproved_source",
                "not_fully_elaborated_proposition",
                "transform_source_ineligible",
                "not_source_ancestry",
                "ancestry_binding_mismatch",
                "missing_representation",
                "representation_binding_mismatch",
                "representation_content_hash_mismatch",
                "missing_or_mismatched_context",
                "required_view_unavailable",
                "unstable_source_locator",
                "denylist_identifier_hit",
                "denylist_content_hit",
            ),
            0,
        ),
        "selected_source_counts": {"mathlib": 1},
        "selection_order_theorem_ids": [theorem_id],
        "outputs": outputs.model_dump(mode="json"),
        "public_sources_only": True,
        "private_sft_classic_forbidden": True,
        "network_execution_authorized": False,
        "semantic_labels_created": False,
    }
    audit = LF022PublicPoolAudit.model_validate(
        {
            **payload,
            "audit_id": make_id("lf022_public_pool_audit", payload),
        }
    )
    return audit, _write_json(
        root,
        "artifacts/public_pool_audit.json",
        audit.model_dump(mode="json"),
    )


def _initialize_git_fixture(root: Path) -> None:
    (root / ".gitignore").write_text(
        "\n".join(
            (
                "/artifacts/",
                "/configs/",
                "/data/",
                "/prompts/",
                "/reports/",
                "/admission.json",
                "/task.json",
                "",
            )
        ),
        encoding="utf-8",
    )
    (root / "fixture_code.py").write_text("VALUE = 1\n", encoding="utf-8")
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "fixture@example.invalid"),
        ("git", "config", "user.name", "LeanFaith Fixture"),
        ("git", "add", ".gitignore", "fixture_code.py"),
        ("git", "commit", "-qm", "fixture"),
    ):
        subprocess.run(command, cwd=root, check=True)


def _fixture(
    root: Path,
    *,
    model_id: str = "moonshotai/Kimi-K2.7-Code",
    matrix_deployment_override: str | None = None,
    clearance_identifier_hits: tuple[str, ...] = (),
    clearance_theorem_hash_override: str | None = None,
    raw_catalog_omit_model_id: str | None = None,
    qualification_contract_replacement: tuple[bytes, bytes] | None = None,
    qualification_contract_path_override: str | None = None,
    profile: Literal[
        "diagnostic_scaffold",
        "pilot_scaffold",
        "scientific_production_scaffold",
    ] = "diagnostic_scaffold",
) -> tuple[
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
]:
    _initialize_git_fixture(root)
    route_spec = {
        "moonshotai/Kimi-K2.7-Code": {
            "family_id": "moonshot_kimi_k2",
            "canonical_family": "moonshotai/kimi-k2",
            "contract_id": "kimi_k2_7_public_smoke_v3",
            "temperature": 1.0,
            "top_p": 0.95,
            "max_tokens": 16_384,
            "thinking_mode": "forced_thinking",
            "execution_scope": "public_provisional_g_open",
        },
        "Qwen/Qwen3.5-397B-A17B": {
            "family_id": "qwen3",
            "canonical_family": "qwen/qwen3",
            "contract_id": "qwen3_5_proposer_qualification_v1",
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
            "max_tokens": 4_096,
            "thinking_mode": "enabled",
            "execution_scope": "one_item_proposer_qualification_only",
        },
        "zai-org/GLM-5.2": {
            "family_id": "glm5",
            "canonical_family": "zai-org/glm-5.2",
            "contract_id": "glm5_2_proposer_qualification_v1",
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": 8_192,
            "thinking_mode": "enabled",
            "execution_scope": "one_item_proposer_qualification_only",
        },
    }[model_id]
    proposer_family_id = str(route_spec["family_id"])
    theorem_id = f"thm:{'7' * 64}"
    context_fingerprint = "9" * 64
    context_id = f"ctx:{context_fingerprint}"
    source_revision = "c" * 40
    header = "import Mathlib"
    context = ContextRecord(
        schema_version=1,
        environment_schema_version=1,
        context_id=context_id,
        context_fingerprint=context_fingerprint,
        project_kind="mathlib",
        project_uri="https://github.com/leanprover-community/mathlib4",
        project_revision=source_revision,
        project_registry_key="mathlib",
        lean_version="4.31.0",
        lean_interact_version="0.11.4",
        repl_revision="d" * 40,
        imports=("Mathlib",),
        header_text=header,
        header_hash=sha256_hex(header.encode("utf-8")),
    )
    statement = "theorem public_source (n : Nat) : n = n"
    ancestry_id = f"anc:{'e' * 64}"
    theorem = TheoremRecord(
        schema_version=1,
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="mathlib",
        source_revision=source_revision,
        source_split="public_fixture",
        source_record="Mathlib/PublicFixture.lean",
        source_record_id="1" * 64,
        source_file="Mathlib/PublicFixture.lean",
        context_id=context_id,
        declaration_kind="theorem",
        declaration_name="public_source",
        declaration_full_name="LeanFaith.Public.public_source",
        proof_stripped_declaration=statement,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES,
        statement_content_hash=sha256_hex(statement.encode("utf-8")),
        nl_source_link="https://github.com/leanprover-community/mathlib4",
        metadata={"transform_source_eligible": True},
    )
    representation_id = make_id(
        "repr",
        {
            "theorem_id": theorem_id,
            "normalization_version": "repr_v3",
        },
    )
    view_status = {
        "raw_proof_stripped": ViewStatus.OK,
        "headless": ViewStatus.OK,
        "signature_pp": ViewStatus.OK,
        "signature_explicit": ViewStatus.OK,
        "alpha_structural": ViewStatus.NOT_ATTEMPTED,
        "notation_light": ViewStatus.NOT_ATTEMPTED,
        "semantic_atoms": ViewStatus.OK,
        "operator_tree": ViewStatus.OK,
    }
    representation = RepresentationRecord(
        schema_version=1,
        representation_id=representation_id,
        theorem_id=theorem_id,
        normalization_version="repr_v3",
        context_id=context_id,
        raw_proof_stripped=statement,
        headless="(n : Nat) : n = n",
        signature_pp="∀ (n : Nat), n = n",
        signature_explicit="∀ (n : Nat), Eq Nat n n",
        semantic_atoms=("Eq", "Nat"),
        operator_tree={"kind": "forall"},
        alpha_identity_fingerprint="2" * 64,
        view_status=view_status,
        content_hash=hash_canonical({"statement": statement, "version": "repr_v3"}),
        created_at=NOW,
    )
    dummy = _write_bytes(root, "artifacts/dummy.json", b"{}\n")
    theorem_records = _write_jsonl(
        root,
        "artifacts/theorems.jsonl",
        (theorem,),
    )
    representation_records = _write_jsonl(
        root,
        "artifacts/representations.jsonl",
        (representation,),
    )
    context_records = _write_jsonl(
        root,
        "artifacts/contexts.jsonl",
        (context,),
    )
    authorization = make_lf022_public_source_authorization(
        source="mathlib",
        source_revision=source_revision,
        license_id="Apache-2.0",
        license_evidence_uri="https://github.com/leanprover-community/mathlib4",
        context_project_uri=context.project_uri,
        upstream_theorem_records=theorem_records,
        upstream_context_records=context_records,
        upstream_extraction_output_manifest=dummy,
        upstream_representation_records=representation_records,
        upstream_representation_output_manifest=dummy,
        mathlib_source_frame=dummy,
        extraction_manifest=dummy,
    )
    authorization_registry = make_lf022_public_source_authorization_registry(
        policy_version="fixture_v1",
        authorizations=(authorization,),
    )
    authorization_registry_binding = _write_json(
        root,
        "artifacts/public_source_authorization_registry.json",
        authorization_registry.model_dump(mode="json"),
    )
    active_registry = FrozenRegistry(
        frozen_at=NOW,
        benchmarks=(
            FrozenBenchmark(
                registry_key="fixture_eval",
                source_id="fixture/eval",
                revision="f" * 40,
                resolved=True,
            ),
        ),
    )
    active_registry_binding = _write_json(
        root,
        "artifacts/active_benchmark_registry.json",
        active_registry.model_dump(mode="json"),
    )
    active_registry_content_hash = DenylistIndex(active_registry).registry_content_hash
    benchmark_manifest = make_lf022_benchmark_registry_manifest(
        policy_version=active_registry.policy_version,
        active_registry=active_registry_binding,
    )
    benchmark_manifest_binding = _write_json(
        root,
        "artifacts/benchmark_registry_manifest.json",
        benchmark_manifest.model_dump(mode="json"),
    )
    clearance = make_lf022_denylist_clearance_record(
        benchmark_manifest_id=benchmark_manifest.manifest_id,
        active_registry_file_sha256=active_registry_binding.sha256,
        active_registry_content_hash=active_registry_content_hash,
        source_locator_id="1" * 64,
        theorem_id=theorem_id,
        theorem_statement_content_hash=(
            clearance_theorem_hash_override or theorem.statement_content_hash
        ),
        representation_id=representation_id,
        representation_content_hash=representation.content_hash,
        identifier_hits=clearance_identifier_hits,
        content_hits=(),
    )
    clearance_records = _write_jsonl(
        root,
        "artifacts/denylist_clearances.jsonl",
        (clearance,),
    )
    source_record = make_lf022_production_source_record(
        source_locator_id="1" * 64,
        source="mathlib",
        source_revision=source_revision,
        theorem_id=theorem_id,
        theorem_statement_content_hash=theorem.statement_content_hash,
        representation_id=representation_id,
        representation_content_hash=representation.content_hash,
        normalization_version="repr_v3",
        context_id=context_id,
        context_fingerprint=context.context_fingerprint,
        context_header_hash=context.header_hash,
        public_source_authorization_id=authorization.authorization_id,
        denylist_clearance_id=clearance.clearance_id,
    )
    source_pool = _write_jsonl(
        root,
        "artifacts/source_pool.jsonl",
        (source_record,),
    )
    g_sci = _task(
        distribution="G_sci",
        proposer_family_id=proposer_family_id,
        source_admission_id=source_record.admission_record_id,
        theorem_id=theorem_id,
        representation_id=representation_id,
        context_id=context_id,
    )
    g_open = _task(
        distribution="G_open",
        proposer_family_id=proposer_family_id,
        source_admission_id=source_record.admission_record_id,
        theorem_id=theorem_id,
        representation_id=representation_id,
        context_id=context_id,
    )
    catalog_source = REPOSITORY_ROOT / "configs/generation/lf022_rcp_catalog_snapshot_v1.json"
    catalog = make_lf022_provider_catalog_snapshot(
        provider_id="epfl_rcp",
        deployments=tuple(
            LF022ProviderDeployment.model_validate(item)
            for item in json.loads(catalog_source.read_bytes())["deployments"]
        ),
    )
    normalized_catalog = _write_json(
        root,
        "configs/generation/lf022_rcp_catalog_snapshot_v1.json",
        catalog.model_dump(mode="json"),
    )
    family_matrix_source = REPOSITORY_ROOT / (
        "configs/generation/lf022_production_family_matrix_v1.json"
    )
    family_matrix = LF022ProductionFamilyMatrix.model_validate_json(
        family_matrix_source.read_bytes()
    )
    if matrix_deployment_override is not None:
        family_registry = tuple(
            pin.model_copy(update={"provider_deployment_id": matrix_deployment_override})
            if pin.family_id == proposer_family_id
            else pin
            for pin in family_matrix.family_registry
        )
        family_matrix = make_lf022_production_family_matrix(
            family_registry=family_registry,
            proposer_family_ids=family_matrix.proposer_family_ids,
            judge_family_ids=family_matrix.judge_family_ids,
            sci_validator_family_ids=family_matrix.sci_validator_family_ids,
            heldout_eval_family_id=family_matrix.heldout_eval_family_id,
        )
    family_matrix_binding = _write_json(
        root,
        "configs/generation/lf022_production_family_matrix_v1.json",
        family_matrix.model_dump(mode="json"),
    )
    production_artifacts = LF022ProductionArtifactSet(
        family_matrix=family_matrix_binding,
        public_source_authorization_registry=authorization_registry_binding,
        benchmark_registry_manifest=benchmark_manifest_binding,
        active_benchmark_registry=active_registry_binding,
        denylist_clearance_records=clearance_records,
        source_pool=source_pool,
        theorem_records=theorem_records,
        representation_records=representation_records,
        context_records=context_records,
    )
    plan = _plan(
        g_sci=g_sci,
        g_open=g_open,
        artifacts=production_artifacts,
        family_matrix=family_matrix,
        profile=profile,
    )
    plan_binding = _write_json(
        root,
        "artifacts/production_plan.json",
        plan.model_dump(mode="json"),
    )
    audit, audit_binding = _audit(
        root=root,
        plan_binding=plan_binding,
        dummy=dummy,
        family_matrix=family_matrix_binding,
        source_pool=source_pool,
        theorem_records=theorem_records,
        representation_records=representation_records,
        context_records=context_records,
        source_authorization_registry=authorization_registry_binding,
        benchmark_registry_manifest=benchmark_manifest_binding,
        active_benchmark_registry=active_registry_binding,
        active_benchmark_registry_content_hash=active_registry_content_hash,
        denylist_clearance_records=clearance_records,
        theorem_id=theorem_id,
        profile=profile,
    )
    raw_catalog = _write_bytes(
        root,
        "artifacts/catalog_raw.json",
        canonical_json_bytes(
            {
                "data": [
                    {"id": deployment.model_id}
                    for deployment in catalog.deployments
                    if deployment.model_id != raw_catalog_omit_model_id
                ]
            }
        ),
    )
    deployment_id = model_id
    prompt_source = REPOSITORY_ROOT / "prompts/proposers/lean_variant_v1.txt"
    prompt = _write_bytes(
        root,
        "prompts/proposers/lean_variant_v1.txt",
        prompt_source.read_bytes(),
    )
    code_tree_hash = collect_code_state(root).code_tree_hash
    assert code_tree_hash is not None
    code_bundle = _code_bundle(root, code_tree_hash=code_tree_hash)
    decoding = LF022RCPDecodingContract(
        contract_id=str(route_spec["contract_id"]),
        temperature=float(route_spec["temperature"]),
        top_p=float(route_spec["top_p"]),
        top_k=route_spec.get("top_k"),
        min_p=route_spec.get("min_p"),
        presence_penalty=route_spec.get("presence_penalty"),
        repetition_penalty=route_spec.get("repetition_penalty"),
        max_tokens=int(route_spec["max_tokens"]),
        seed=42,
        thinking_mode=str(route_spec["thinking_mode"]),
        reasoning_effort="high",
        chat_template_enable_thinking=True,
    )
    route = LF022RCPRouteBinding(
        provider_id="epfl_rcp",
        model_id=model_id,
        deployment_id=deployment_id,
        proposer_family_id=proposer_family_id,
        canonical_family=str(route_spec["canonical_family"]),
        catalog_snapshot_id=catalog.snapshot_id,
        route_snapshot_revision=f"rcp-catalog-sha256:{raw_catalog.sha256}",
        underlying_checkpoint_revision_status="provider_not_disclosed",
        execution_scope=str(route_spec["execution_scope"]),
        decoding=decoding,
    )
    retry = LF022RCPRetryPolicy(
        max_attempts=3,
        base_delay_seconds=1,
        maximum_delay_seconds=60,
        retryable_http_statuses=(408, 409, 425, 429, 500, 502, 503, 504),
    )
    contract_relative = {
        "moonshotai/Kimi-K2.7-Code": ("configs/generation/lf022_rcp_public_smoke_v3.yaml"),
        "Qwen/Qwen3.5-397B-A17B": (
            "configs/generation/lf022_qwen3_5_proposer_qualification_v1.yaml"
        ),
        "zai-org/GLM-5.2": ("configs/generation/lf022_glm5_2_proposer_qualification_v1.yaml"),
    }[model_id]
    contract_bytes = (REPOSITORY_ROOT / contract_relative).read_bytes()
    if qualification_contract_replacement is not None:
        before, after = qualification_contract_replacement
        assert before in contract_bytes
        contract_bytes = contract_bytes.replace(before, after, 1)
    reviewed_route_contract = _write_bytes(
        root,
        qualification_contract_path_override or contract_relative,
        contract_bytes,
    )
    accepted_wire_evidence = {
        "Qwen/Qwen3.5-397B-A17B": (
            "data/raw/llm_variants/lf022_rcp_public_smoke_v3/"
            "61e201acc254d89cb5e9686bd56a7f4e03c0ea2f8169ae39e22cc31be48a0589/"
            "calls/judge_A_AB/wire_request.json",
            "data/raw/llm_variants/lf022_rcp_public_smoke_v3/"
            "61e201acc254d89cb5e9686bd56a7f4e03c0ea2f8169ae39e22cc31be48a0589/"
            "calls/judge_A_AB/wire_response.json",
        ),
        "zai-org/GLM-5.2": (
            "data/raw/llm_variants/lf022_rcp_public_smoke_v3/"
            "61e201acc254d89cb5e9686bd56a7f4e03c0ea2f8169ae39e22cc31be48a0589/"
            "calls/judge_B_AB/wire_request.json",
            "data/raw/llm_variants/lf022_rcp_public_smoke_v3/"
            "61e201acc254d89cb5e9686bd56a7f4e03c0ea2f8169ae39e22cc31be48a0589/"
            "calls/judge_B_AB/wire_response.json",
        ),
    }.get(model_id)
    if accepted_wire_evidence is not None:
        _copy_repo_artifact(
            root,
            (
                "data/raw/llm_variants/lf022_rcp_public_smoke_v3/"
                "61e201acc254d89cb5e9686bd56a7f4e03c0ea2f8169ae39e22cc31be48a0589/"
                "manifest.json"
            ),
        )
        for artifact in accepted_wire_evidence:
            _copy_repo_artifact(root, artifact)
    admission = make_lf022_g_open_execution_admission(
        public_pool_audit_id=audit.audit_id,
        allocation_plan_id=plan.manifest_id,
        artifacts=LF022ExecutionArtifacts(
            public_pool_audit=audit_binding,
            allocation_plan=plan_binding,
            provider_catalog_raw=raw_catalog,
            provider_catalog_normalized=normalized_catalog,
            reviewed_route_portfolio=_copy_repo_artifact(
                root,
                "configs/generation/rcp_provider_portfolio_v2.yaml",
            ),
            reviewed_route_contract=reviewed_route_contract,
            reviewed_route_evidence=_copy_repo_artifact(
                root,
                "reports/generation/lf022_rcp_public_smoke_qualification_v1.json",
            ),
            prompt_template=prompt,
            code_bundle=code_bundle,
        ),
        route=route,
        retry_policy=retry,
        code_tree_hash=code_tree_hash,
    )
    source = PublicLeanVariantSource(
        source_theorem_id=theorem_id,
        source_representation_id=representation_id,
        context_id=context_id,
        imports=("Mathlib",),
        source_statement=make_lf022_named_signature(
            theorem=theorem,
            representation=representation,
        ),
        source_id="mathlib",
        source_revision=source_revision,
        source_license="Apache-2.0",
        source_is_public=True,
        external_transmission_allowed=True,
        denylist_checked=True,
    )
    task = make_lf022_g_open_execution_task(
        admission=admission,
        allocation_task=g_open,
        source=source,
    )
    return admission, task


def _success_response(model_id: str) -> RCPWireResponse:
    content = json.dumps(
        {
            "variants": [
                {
                    "candidate_lean": ("theorem public_candidate (n : Nat) : n + 0 = n"),
                    "intended_relation": "near_miss",
                    "intended_error_types": ["E21"],
                    "edit_summary": "Changed the claim while preserving syntax.",
                    "confidence": 0.7,
                    "assumptions": [],
                    "potential_ambiguity": None,
                }
            ]
        }
    )
    return RCPWireResponse(
        status_code=200,
        headers={},
        body=canonical_json_bytes(
            {
                "id": "fixture-call",
                "model": model_id,
                "choices": [
                    {
                        "message": {"content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
        ),
    )


@pytest.mark.parametrize(
    ("model_id", "family_id"),
    (
        ("Qwen/Qwen3.5-397B-A17B", "qwen3"),
        ("zai-org/GLM-5.2", "glm5"),
    ),
)
def test_freeze_diagnostic_execution_admission_replays_exact_fixture(
    tmp_path: Path,
    model_id: str,
    family_id: str,
) -> None:
    expected, _ = _fixture(tmp_path, model_id=model_id)
    output = tmp_path / f"artifacts/{family_id}_execution_admission.json"
    first = freeze_lf022_diagnostic_execution_admission(
        repo_root=tmp_path,
        public_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
        proposer_family_id=family_id,  # type: ignore[arg-type]
        code_bundle_path=tmp_path / expected.artifacts.code_bundle.path,
        provider_catalog_raw_path=tmp_path / expected.artifacts.provider_catalog_raw.path,
        output_path=output,
    )
    second = freeze_lf022_diagnostic_execution_admission(
        repo_root=tmp_path,
        public_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
        proposer_family_id=family_id,  # type: ignore[arg-type]
        code_bundle_path=tmp_path / expected.artifacts.code_bundle.path,
        provider_catalog_raw_path=tmp_path / expected.artifacts.provider_catalog_raw.path,
        output_path=output,
    )
    assert first.admission == expected
    assert second == first
    assert output.read_bytes() == (canonical_json_bytes(expected.model_dump(mode="json")) + b"\n")
    assert first.admission.public_sources_only is True
    assert first.admission.private_source_content_forbidden is True
    assert first.admission.outputs_provisional_only is True
    assert first.admission.semantic_labels_created is False
    assert first.admission.training_eligible is False
    assert first.admission.evaluation_eligible is False
    assert first.admission.gate_credit_claimed is False


def test_freeze_diagnostic_kimi_v3_execution_admission_is_archived(
    tmp_path: Path,
) -> None:
    expected, _ = _fixture(tmp_path)
    output = tmp_path / "artifacts/kimi_diagnostic_execution_admission.json"
    with pytest.raises(LF022AdmissionFreezeError, match="Kimi-v3 diagnostic admission is archived"):
        freeze_lf022_diagnostic_execution_admission(
            repo_root=tmp_path,
            public_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
            proposer_family_id="moonshot_kimi_k2",
            code_bundle_path=tmp_path / expected.artifacts.code_bundle.path,
            provider_catalog_raw_path=tmp_path / expected.artifacts.provider_catalog_raw.path,
            output_path=output,
        )
    assert not output.exists()


def test_freeze_diagnostic_execution_admission_rejects_cross_family_plan(
    tmp_path: Path,
) -> None:
    expected, _ = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    with pytest.raises(
        LF022AdmissionFreezeError,
        match="assigned to the selected proposer",
    ):
        freeze_lf022_diagnostic_execution_admission(
            repo_root=tmp_path,
            public_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
            proposer_family_id="glm5",
            code_bundle_path=tmp_path / expected.artifacts.code_bundle.path,
            provider_catalog_raw_path=tmp_path / expected.artifacts.provider_catalog_raw.path,
            output_path=tmp_path / "artifacts/rejected.json",
        )


def test_freeze_diagnostic_execution_admission_cli_is_offline(
    tmp_path: Path,
) -> None:
    expected, _ = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    output = tmp_path / "artifacts/qwen_cli_execution_admission.json"
    result = CliRunner().invoke(
        app,
        [
            "freeze-lf022-proposer-admission",
            "--root",
            str(tmp_path),
            "--public-pool-audit",
            "artifacts/public_pool_audit.json",
            "--proposer-family",
            "qwen3",
            "--code-bundle",
            expected.artifacts.code_bundle.path,
            "--provider-catalog-raw",
            expected.artifacts.provider_catalog_raw.path,
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "family=qwen3" in result.output
    assert "network_calls_this_run=0" in result.output
    assert "training_eligible=false" in result.output
    assert LF022GOpenExecutionAdmission.model_validate_json(output.read_bytes()) == expected


def test_freeze_diagnostic_kimi_v3_cli_is_archived(tmp_path: Path) -> None:
    expected, _ = _fixture(tmp_path)
    output = tmp_path / "artifacts/kimi_diagnostic_cli_admission.json"
    result = CliRunner().invoke(
        app,
        [
            "freeze-lf022-proposer-admission",
            "--root",
            str(tmp_path),
            "--public-pool-audit",
            "artifacts/public_pool_audit.json",
            "--proposer-family",
            "moonshot_kimi_k2",
            "--code-bundle",
            expected.artifacts.code_bundle.path,
            "--provider-catalog-raw",
            expected.artifacts.provider_catalog_raw.path,
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 2
    assert "Kimi-v3 admission is archived" in result.output
    assert not output.exists()


def test_freeze_scientific_kimi_execution_admission_is_archived(
    tmp_path: Path,
) -> None:
    expected, _ = _fixture(
        tmp_path,
        profile="scientific_production_scaffold",
    )
    output = tmp_path / "artifacts/kimi_scientific_execution_admission.json"
    with pytest.raises(LF022AdmissionFreezeError, match="archived after the failed prefix-256"):
        freeze_lf022_scientific_kimi_execution_admission(
            repo_root=tmp_path,
            public_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
            code_bundle_path=tmp_path / expected.artifacts.code_bundle.path,
            provider_catalog_raw_path=tmp_path / expected.artifacts.provider_catalog_raw.path,
            output_path=output,
        )
    assert not output.exists()


def test_freeze_scientific_kimi_admission_cli_is_archived(tmp_path: Path) -> None:
    expected, _ = _fixture(
        tmp_path,
        profile="scientific_production_scaffold",
    )
    output = tmp_path / "artifacts/kimi_scientific_cli_admission.json"
    result = CliRunner().invoke(
        app,
        [
            "freeze-lf022-scientific-kimi-admission",
            "--root",
            str(tmp_path),
            "--public-pool-audit",
            "artifacts/public_pool_audit.json",
            "--code-bundle",
            expected.artifacts.code_bundle.path,
            "--provider-catalog-raw",
            expected.artifacts.provider_catalog_raw.path,
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 2
    assert "archived after the failed prefix-256" in result.output
    assert not output.exists()


def test_freeze_scientific_kimi_v4_admission_binds_exact_eligibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical, task = _fixture(
        tmp_path,
        profile="scientific_production_scaffold",
    )
    v4_contract = _copy_repo_artifact(
        tmp_path,
        "configs/generation/lf022_kimi_k2_7_proposer_v4.yaml",
    )
    v4_prompt = _copy_repo_artifact(
        tmp_path,
        "prompts/proposers/lean_variant_v2.txt",
    )
    plan = LF022ProductionPlanManifest.model_validate_json(
        (tmp_path / historical.artifacts.allocation_plan.path).read_bytes()
    )
    matrix = LF022ProductionFamilyMatrix.model_validate_json(
        (tmp_path / plan.artifacts.family_matrix.path).read_bytes()
    )
    judges = tuple(
        sorted(
            family
            for family in matrix.judge_family_ids
            if family not in {"moonshot_kimi_k2", matrix.heldout_eval_family_id}
        )
    )
    validators = tuple(
        sorted(
            family
            for family in matrix.sci_validator_family_ids
            if family not in {"moonshot_kimi_k2", matrix.heldout_eval_family_id}
        )
    )
    v4_decoding = LF022RCPDecodingContract.model_validate(
        {
            **historical.route.decoding.model_dump(mode="json"),
            "contract_id": "kimi_k2_7_public_proposer_v4",
            "max_tokens": 32_768,
        }
    )
    placeholder = historical.artifacts.reviewed_route_evidence
    eligibility_payload: dict[str, object] = {
        "schema_version": 1,
        "status": "kimi_v4_challenge_replay_verified",
        "proposer_family_id": "moonshot_kimi_k2",
        "model_id": "moonshotai/Kimi-K2.7-Code",
        "deployment_id": "moonshotai/Kimi-K2.7-Code",
        "canonical_family": "moonshotai/kimi-k2",
        "provider_id": "epfl_rcp",
        "catalog_snapshot_id": historical.route.catalog_snapshot_id,
        "route_snapshot_revision": historical.route.route_snapshot_revision,
        "decoding_contract_id": "kimi_k2_7_public_proposer_v4",
        "decoding_contract_hash": hash_canonical(v4_decoding.model_dump(mode="json")),
        "v4_contract": v4_contract.model_dump(mode="json"),
        "v4_prompt": v4_prompt.model_dump(mode="json"),
        "selection_id": f"lf022_kimi_v4_selection:{'2' * 64}",
        "selection": placeholder.model_dump(mode="json"),
        "selection_code_tree_hash": "3" * 64,
        "selection_code_bundle": placeholder.model_dump(mode="json"),
        "qualification_id": f"lf022_kimi_v4_qualification:{'4' * 64}",
        "qualification": placeholder.model_dump(mode="json"),
        "qualification_status": "passed",
        "qualification_terminal_count": 16,
        "strict_parse_success_count": 16,
        "replay_network_calls": 0,
        "family_matrix_id": matrix.matrix_id,
        "family_matrix": plan.artifacts.family_matrix.model_dump(mode="json"),
        "judge_family_ids": list(judges),
        "permitted_validator_family_ids": list(validators),
        "heldout_eval_family_id": matrix.heldout_eval_family_id,
        "heldout_eval_supervision_excluded": True,
        "production_execution_scope": "public_provisional_g_open",
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "output_quality_tier": "provisional",
        "outputs_unresolved": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    eligibility = LF022KimiV4ProductionEligibility.model_validate(
        {
            **eligibility_payload,
            "eligibility_id": make_id(
                "lf022_kimi_v4_route_eligibility",
                eligibility_payload,
            ),
        }
    )
    eligibility_binding = _write_json(
        tmp_path,
        LF022_KIMI_V4_ELIGIBILITY_PATH,
        eligibility.model_dump(mode="json"),
    )
    from leanfaith.generation import lf022_kimi_v4_eligibility

    monkeypatch.setattr(
        lf022_kimi_v4_eligibility,
        "verify_lf022_kimi_v4_production_eligibility",
        lambda **_: eligibility,
    )
    current_tree_hash = collect_code_state(tmp_path).code_tree_hash
    assert current_tree_hash is not None
    code_bundle = _code_bundle(tmp_path, code_tree_hash=current_tree_hash)
    output = tmp_path / "artifacts/kimi_v4_scientific_execution_admission.json"
    frozen = freeze_lf022_scientific_kimi_execution_admission(
        repo_root=tmp_path,
        public_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
        proposer_production_eligibility_path=tmp_path / eligibility_binding.path,
        code_bundle_path=tmp_path / code_bundle.path,
        provider_catalog_raw_path=tmp_path / historical.artifacts.provider_catalog_raw.path,
        output_path=output,
    )
    assert frozen.admission.schema_version == 2
    assert frozen.admission.route.decoding.contract_id == "kimi_k2_7_public_proposer_v4"
    assert frozen.admission.route.decoding.max_tokens == 32_768
    assert frozen.admission.artifacts.prompt_template == v4_prompt
    assert frozen.admission.artifacts.proposer_production_eligibility == eligibility_binding
    assert frozen.admission.outputs_provisional_only is True
    assert frozen.admission.semantic_labels_created is False
    assert frozen.admission.training_eligible is False
    assert frozen.admission.evaluation_eligible is False
    assert frozen.admission.gate_credit_claimed is False
    assert task.allocation_task.proposer_family_id == "moonshot_kimi_k2"


@pytest.mark.parametrize(
    ("model_id", "family_id", "contract_id"),
    (
        (
            "Qwen/Qwen3.5-397B-A17B",
            "qwen3",
            "qwen3_5_proposer_qualification_v2",
        ),
        ("zai-org/GLM-5.2", "glm5", "glm5_2_proposer_qualification_v2"),
    ),
)
def test_freeze_scientific_qualified_admission_binds_exact_v2_eligibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_id: str,
    family_id: Literal["qwen3", "glm5"],
    contract_id: str,
) -> None:
    qualification_admission, task = _fixture(
        tmp_path,
        model_id=model_id,
        profile="scientific_production_scaffold",
    )
    eligibility, eligibility_binding = _write_fake_v2_eligibility(
        tmp_path,
        admission=qualification_admission,
        task=task,
    )
    from leanfaith.generation import lf022_route_qualification

    def replay_exact_eligibility(
        *,
        repo_root: Path,
        eligibility_binding: LF022ArtifactBinding,
    ) -> LF022QualifiedProposerProductionEligibility:
        assert repo_root == tmp_path
        assert eligibility_binding == eligibility_binding_expected
        return eligibility

    eligibility_binding_expected = eligibility_binding
    monkeypatch.setattr(
        lf022_route_qualification,
        "verify_lf022_proposer_production_eligibility",
        replay_exact_eligibility,
    )
    output = tmp_path / f"artifacts/{family_id}_scientific_execution_admission.json"
    frozen = freeze_lf022_scientific_qualified_execution_admission(
        repo_root=tmp_path,
        public_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
        proposer_family_id=family_id,
        proposer_production_eligibility_path=tmp_path / eligibility_binding.path,
        code_bundle_path=tmp_path / qualification_admission.artifacts.code_bundle.path,
        provider_catalog_raw_path=(
            tmp_path / qualification_admission.artifacts.provider_catalog_raw.path
        ),
        output_path=output,
    )
    assert frozen.admission.schema_version == 2
    assert frozen.admission.route.execution_scope == "public_provisional_g_open"
    assert frozen.admission.route.decoding.contract_id == contract_id
    assert frozen.admission.artifacts.proposer_production_eligibility == eligibility_binding
    assert frozen.admission.outputs_provisional_only is True
    assert frozen.admission.semantic_labels_created is False
    assert frozen.admission.training_eligible is False
    assert frozen.admission.evaluation_eligible is False
    assert frozen.admission.gate_credit_claimed is False
    mismatched = eligibility.model_copy(
        update={"qualification_contract": qualification_admission.artifacts.reviewed_route_evidence}
    )
    monkeypatch.setattr(
        lf022_route_qualification,
        "verify_lf022_proposer_production_eligibility",
        lambda **_: mismatched,
    )
    with pytest.raises(
        LF022ExecutionError,
        match="proposer production eligibility belongs to a different route or matrix",
    ):
        verify_lf022_execution_admission(
            repo_root=tmp_path,
            admission=frozen.admission,
        )


def test_scientific_freezer_rejects_mismatched_qualification_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qualification_admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
        profile="scientific_production_scaffold",
    )
    eligibility, eligibility_binding = _write_fake_v2_eligibility(
        tmp_path,
        admission=qualification_admission,
        task=task,
    )
    mismatched = eligibility.model_copy(
        update={"qualification_contract": qualification_admission.artifacts.reviewed_route_evidence}
    )
    from leanfaith.generation import lf022_route_qualification

    monkeypatch.setattr(
        lf022_route_qualification,
        "verify_lf022_proposer_production_eligibility",
        lambda **_: mismatched,
    )
    with pytest.raises(
        LF022AdmissionFreezeError,
        match="different v2 route or matrix",
    ):
        freeze_lf022_scientific_qualified_execution_admission(
            repo_root=tmp_path,
            public_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
            proposer_family_id="qwen3",
            proposer_production_eligibility_path=tmp_path / eligibility_binding.path,
            code_bundle_path=tmp_path / qualification_admission.artifacts.code_bundle.path,
            provider_catalog_raw_path=(
                tmp_path / qualification_admission.artifacts.provider_catalog_raw.path
            ),
            output_path=tmp_path / "artifacts/rejected_qwen_scientific.json",
        )


@pytest.mark.parametrize(
    ("family_id", "contract_id"),
    (
        ("qwen3", "qwen3_5_proposer_qualification_v2"),
        ("glm5", "glm5_2_proposer_qualification_v2"),
    ),
)
def test_repository_v2_eligibility_artifact_replays_offline_when_present(
    family_id: str,
    contract_id: str,
) -> None:
    path = REPOSITORY_ROOT / "data/lf022_execution/production_eligibility" / f"{family_id}.json"
    if not path.is_file():
        pytest.skip("ignored replay-verified qualification artifact is not installed")
    eligibility = verify_lf022_proposer_production_eligibility(
        repo_root=REPOSITORY_ROOT,
        eligibility_binding=LF022ArtifactBinding(
            path=path.relative_to(REPOSITORY_ROOT).as_posix(),
            sha256=hash_file(path),
        ),
    )
    assert eligibility.proposer_family_id == family_id
    assert eligibility.decoding_contract_id == contract_id
    assert eligibility.exact_replay_verified is True
    assert eligibility.outputs_unresolved is True
    assert eligibility.semantic_labels_created is False
    assert eligibility.training_eligible is False


def test_freeze_scientific_qualified_admission_cli_is_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qualification_admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
        profile="scientific_production_scaffold",
    )
    eligibility, eligibility_binding = _write_fake_v2_eligibility(
        tmp_path,
        admission=qualification_admission,
        task=task,
    )
    from leanfaith.generation import lf022_route_qualification

    monkeypatch.setattr(
        lf022_route_qualification,
        "verify_lf022_proposer_production_eligibility",
        lambda **_: eligibility,
    )
    output = tmp_path / "artifacts/qwen_scientific_cli_admission.json"
    result = CliRunner().invoke(
        app,
        [
            "freeze-lf022-scientific-qualified-admission",
            "--root",
            str(tmp_path),
            "--public-pool-audit",
            "artifacts/public_pool_audit.json",
            "--proposer-family",
            "qwen3",
            "--proposer-production-eligibility",
            eligibility_binding.path,
            "--code-bundle",
            qualification_admission.artifacts.code_bundle.path,
            "--provider-catalog-raw",
            qualification_admission.artifacts.provider_catalog_raw.path,
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "family=qwen3" in result.output
    assert "qualification_replay_verified=true" in result.output
    assert "network_calls_this_run=0" in result.output
    assert "training_eligible=false" in result.output
    admission = LF022GOpenExecutionAdmission.model_validate_json(output.read_bytes())
    assert admission.route.decoding.contract_id == "qwen3_5_proposer_qualification_v2"
    assert admission.artifacts.proposer_production_eligibility == eligibility_binding


def test_scientific_kimi_admission_is_archived_even_for_diagnostic_pool(tmp_path: Path) -> None:
    expected, _ = _fixture(tmp_path)
    with pytest.raises(
        LF022AdmissionFreezeError,
        match="archived after the failed prefix-256",
    ):
        freeze_lf022_scientific_kimi_execution_admission(
            repo_root=tmp_path,
            public_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
            code_bundle_path=tmp_path / expected.artifacts.code_bundle.path,
            provider_catalog_raw_path=tmp_path / expected.artifacts.provider_catalog_raw.path,
            output_path=tmp_path / "artifacts/rejected_scientific_admission.json",
        )


def test_scientific_kimi_plan_windows_are_stable_and_canonical() -> None:
    kimi_open_tasks = tuple(
        _task(
            distribution="G_open",
            proposer_family_id="moonshot_kimi_k2",
            source_admission_id=f"lf022_source_admission:{index:064x}",
            theorem_id=f"thm:{index:064x}",
            representation_id=f"repr:{index:064x}",
            context_id=f"ctx:{index:064x}",
        )
        for index in range(9_207)
    )
    unrelated_tasks = (
        _task(
            distribution="G_sci",
            proposer_family_id="moonshot_kimi_k2",
            source_admission_id=f"lf022_source_admission:{9_207:064x}",
            theorem_id=f"thm:{9_207:064x}",
            representation_id=f"repr:{9_207:064x}",
            context_id=f"ctx:{9_207:064x}",
        ),
        _task(
            distribution="G_open",
            proposer_family_id="qwen3",
            source_admission_id=f"lf022_source_admission:{9_208:064x}",
            theorem_id=f"thm:{9_208:064x}",
            representation_id=f"repr:{9_208:064x}",
            context_id=f"ctx:{9_208:064x}",
        ),
    )
    plan_tasks = (
        unrelated_tasks[0],
        *kimi_open_tasks[:256],
        unrelated_tasks[1],
        *kimi_open_tasks[256:],
    )

    one = select_public_g_open_plan_window(
        plan_tasks=plan_tasks,
        proposer_family_id="moonshot_kimi_k2",
        allocation_offset=0,
        allocation_limit=1,
    )
    first_256 = select_public_g_open_plan_window(
        plan_tasks=plan_tasks,
        proposer_family_id="moonshot_kimi_k2",
        allocation_offset=0,
        allocation_limit=256,
    )
    full = select_public_g_open_plan_window(
        plan_tasks=plan_tasks,
        proposer_family_id="moonshot_kimi_k2",
        allocation_offset=0,
        allocation_limit=9_207,
    )

    assert one == (kimi_open_tasks[0].task_id,)
    assert first_256 == tuple(sorted(task.task_id for task in kimi_open_tasks[:256]))
    assert full == tuple(sorted(task.task_id for task in kimi_open_tasks))
    assert len(full) == len(set(full)) == 9_207
    with pytest.raises(ValueError, match="exceeds"):
        select_public_g_open_plan_window(
            plan_tasks=plan_tasks,
            proposer_family_id="moonshot_kimi_k2",
            allocation_offset=9_207,
            allocation_limit=1,
        )


class FakeTransport(RCPHTTPTransport):
    def __init__(self, responses: list[RCPWireResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.payloads: list[Mapping[str, object]] = []

    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> RCPWireResponse:
        del api_key, timeout_seconds
        assert url == "https://inference.rcp.epfl.ch/v1/chat/completions"
        self.calls += 1
        self.payloads.append(payload)
        if not self.responses:
            raise AssertionError("unexpected network call")
        return self.responses.pop(0)


class UnknownTransport(RCPHTTPTransport):
    def __init__(self) -> None:
        self.calls = 0

    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> RCPWireResponse:
        del url, api_key, payload, timeout_seconds
        self.calls += 1
        raise RCPTransportUnknownError("delivery state is unknown")


def _credentials() -> RCPRuntimeCredentials:
    return RCPRuntimeCredentials(
        base_url="https://inference.rcp.epfl.ch/v1",
        api_key="unit-test-secret",
    )


def _qualification_task_dir(root: Path, task: LF022GOpenExecutionTask) -> Path:
    digest = task.execution_task_id.removeprefix("lf022_execution_task:")
    return root / "data/lf022_execution/tasks" / digest[:2] / digest


def _qualify_and_certify(
    root: Path,
    *,
    model_id: str,
) -> tuple[
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    LF022QualifiedProposerProductionEligibility,
    LF022ArtifactBinding,
]:
    admission, task = _fixture(root, model_id=model_id)
    transport = FakeTransport([_success_response(model_id)])
    live = execute_lf022_g_open_task(
        repo_root=root,
        output_root=root / "data/lf022_execution",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=transport,
        clock=lambda: NOW,
    )
    assert live.terminal is not None
    assert live.terminal.status == "provisional_variants_created"
    assert live.network_calls_this_run == 1
    assert transport.calls == 1
    task_dir = _qualification_task_dir(root, task)
    admission_path = task_dir / "admission.json"
    task_path = task_dir / "task.json"
    certified = certify_lf022_proposer_production_eligibility(
        repo_root=root,
        qualification_admission_binding=LF022ArtifactBinding(
            path=str(admission_path.relative_to(root)),
            sha256=hash_file(admission_path),
        ),
        qualification_task_binding=LF022ArtifactBinding(
            path=str(task_path.relative_to(root)),
            sha256=hash_file(task_path),
        ),
    )
    eligibility_binding = LF022ArtifactBinding(
        path=str(certified.eligibility_path.relative_to(root)),
        sha256=hash_file(certified.eligibility_path),
    )
    return admission, task, certified.eligibility, eligibility_binding


def _write_fake_v2_eligibility(
    root: Path,
    *,
    admission: LF022GOpenExecutionAdmission,
    task: LF022GOpenExecutionTask,
) -> tuple[LF022QualifiedProposerProductionEligibility, LF022ArtifactBinding]:
    """Build a schema-valid v2 fixture; tests monkeypatch its historical replay."""

    family = admission.route.proposer_family_id
    assert family in {"qwen3", "glm5"}
    contract_id = {
        "qwen3": "qwen3_5_proposer_qualification_v2",
        "glm5": "glm5_2_proposer_qualification_v2",
    }[family]
    contract_path = {
        "qwen3": "configs/generation/lf022_qwen3_5_proposer_qualification_v2.yaml",
        "glm5": "configs/generation/lf022_glm5_2_proposer_qualification_v2.yaml",
    }[family]
    contract = _copy_repo_artifact(root, contract_path)
    decoding = LF022RCPDecodingContract.model_validate(
        {
            **admission.route.decoding.model_dump(mode="json"),
            "contract_id": contract_id,
            "max_tokens": 16_384 if family == "qwen3" else 8_192,
        }
    )
    plan = LF022ProductionPlanManifest.model_validate_json(
        (root / admission.artifacts.allocation_plan.path).read_bytes()
    )
    matrix = LF022ProductionFamilyMatrix.model_validate_json(
        (root / plan.artifacts.family_matrix.path).read_bytes()
    )
    validators = tuple(
        sorted(
            candidate
            for candidate in matrix.sci_validator_family_ids
            if candidate not in {family, matrix.heldout_eval_family_id}
        )
    )
    placeholder = admission.artifacts.reviewed_route_evidence
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "live_qualification_replay_verified",
        "proposer_family_id": family,
        "model_id": admission.route.model_id,
        "deployment_id": admission.route.deployment_id,
        "canonical_family": admission.route.canonical_family,
        "provider_id": admission.route.provider_id,
        "catalog_snapshot_id": admission.route.catalog_snapshot_id,
        "route_snapshot_revision": admission.route.route_snapshot_revision,
        "decoding_contract_id": contract_id,
        "decoding_contract_hash": hash_canonical(decoding.model_dump(mode="json")),
        "family_matrix_id": matrix.matrix_id,
        "family_matrix": plan.artifacts.family_matrix.model_dump(mode="json"),
        "qualification_contract": contract.model_dump(mode="json"),
        "qualification_claim_id": f"lf022_qualification_claim:{'1' * 64}",
        "qualification_claim": placeholder.model_dump(mode="json"),
        "qualification_admission_id": admission.admission_id,
        "qualification_admission": placeholder.model_dump(mode="json"),
        "qualification_task_id": task.execution_task_id,
        "qualification_task": placeholder.model_dump(mode="json"),
        "qualification_terminal_id": f"lf022_execution_terminal:{'2' * 64}",
        "qualification_terminal": placeholder.model_dump(mode="json"),
        "qualification_variants": placeholder.model_dump(mode="json"),
        "qualification_llm_call_id": f"call:{'3' * 64}",
        "qualification_llm_call": placeholder.model_dump(mode="json"),
        "qualification_provider_request_hash": "4" * 64,
        "qualification_completed_at": NOW.isoformat().replace("+00:00", "Z"),
        "qualification_task_count": 1,
        "qualification_variant_count": 1,
        "qualification_execution_mode": "external",
        "exact_replay_verified": True,
        "production_execution_scope": "public_provisional_g_open",
        "judge_family_ids": list(task.allocation_task.judge_family_ids),
        "permitted_validator_family_ids": list(validators),
        "proposer_validator_same_family_forbidden": True,
        "heldout_eval_family_id": matrix.heldout_eval_family_id,
        "heldout_eval_supervision_excluded": True,
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "output_quality_tier": "provisional",
        "outputs_unresolved": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    eligibility = LF022QualifiedProposerProductionEligibility.model_validate(
        {
            **payload,
            "eligibility_id": make_id("lf022_route_eligibility", payload),
        }
    )
    binding = _write_json(
        root,
        f"data/lf022_execution/production_eligibility/{family}.json",
        eligibility.model_dump(mode="json"),
    )
    return eligibility, binding


def _v2_production_route(
    admission: LF022GOpenExecutionAdmission,
) -> LF022RCPRouteBinding:
    family = admission.route.proposer_family_id
    contract_id = {
        "qwen3": "qwen3_5_proposer_qualification_v2",
        "glm5": "glm5_2_proposer_qualification_v2",
    }[family]
    decoding = LF022RCPDecodingContract.model_validate(
        {
            **admission.route.decoding.model_dump(mode="json"),
            "contract_id": contract_id,
            "max_tokens": 16_384 if family == "qwen3" else 8_192,
        }
    )
    route_payload = admission.route.model_dump(mode="json")
    route_payload.update(
        {
            "execution_scope": "public_provisional_g_open",
            "decoding": decoding.model_dump(mode="json"),
        }
    )
    return LF022RCPRouteBinding.model_validate(route_payload)


def _task_dir(root: Path, task: LF022GOpenExecutionTask) -> Path:
    digest = task.execution_task_id.removeprefix("lf022_execution_task:")
    return root / "data/out/tasks" / digest[:2] / digest


@pytest.mark.parametrize(
    ("model_id", "contract_data", "family", "canonical", "scope", "expected_wire"),
    (
        (
            "moonshotai/Kimi-K2.7-Code",
            {
                "contract_id": "kimi_k2_7_public_smoke_v3",
                "temperature": 1.0,
                "top_p": 0.95,
                "max_tokens": 16_384,
                "thinking_mode": "forced_thinking",
            },
            "moonshot_kimi_k2",
            "moonshotai/kimi-k2",
            "public_provisional_g_open",
            {
                "temperature": 1.0,
                "top_p": 0.95,
                "max_tokens": 16_384,
                "seed": 42,
                "stream": False,
                "reasoning_effort": "high",
                "chat_template_kwargs": {"enable_thinking": True},
            },
        ),
        (
            "Qwen/Qwen3.5-397B-A17B",
            {
                "contract_id": "qwen3_5_proposer_qualification_v1",
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repetition_penalty": 1.0,
                "max_tokens": 4_096,
                "thinking_mode": "enabled",
            },
            "qwen3",
            "qwen/qwen3",
            "one_item_proposer_qualification_only",
            {
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repetition_penalty": 1.0,
                "max_tokens": 4_096,
                "seed": 42,
                "stream": False,
                "reasoning_effort": "high",
                "chat_template_kwargs": {"enable_thinking": True},
            },
        ),
        (
            "zai-org/GLM-5.2",
            {
                "contract_id": "glm5_2_proposer_qualification_v1",
                "temperature": 0.0,
                "top_p": 1.0,
                "max_tokens": 8_192,
                "thinking_mode": "enabled",
            },
            "glm5",
            "zai-org/glm-5.2",
            "one_item_proposer_qualification_only",
            {
                "temperature": 0.0,
                "top_p": 1.0,
                "max_tokens": 8_192,
                "seed": 42,
                "stream": False,
                "reasoning_effort": "high",
                "chat_template_kwargs": {"enable_thinking": True},
            },
        ),
    ),
)
def test_route_specific_thinking_contract(
    model_id: str,
    contract_data: dict[str, object],
    family: str,
    canonical: str,
    scope: str,
    expected_wire: dict[str, object],
) -> None:
    contract = LF022RCPDecodingContract(
        **contract_data,
        seed=42,
        reasoning_effort="high",
        chat_template_enable_thinking=True,
    )
    route = LF022RCPRouteBinding(
        provider_id="epfl_rcp",
        model_id=model_id,
        deployment_id=model_id,
        proposer_family_id=family,
        canonical_family=canonical,
        catalog_snapshot_id=f"lf022_provider_catalog:{'d' * 64}",
        route_snapshot_revision=f"rcp-catalog-sha256:{'e' * 64}",
        underlying_checkpoint_revision_status="provider_not_disclosed",
        execution_scope=scope,
        decoding=contract,
    )
    assert route.decoding.wire_fields() == expected_wire


def test_unreviewed_vl_route_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside the exact reviewed"):
        LF022RCPRouteBinding(
            provider_id="epfl_rcp",
            model_id="Qwen/Qwen3-VL-235B-A22B-Thinking",
            deployment_id="Qwen/Qwen3-VL-235B-A22B-Thinking",
            proposer_family_id="qwen3",
            canonical_family="qwen/qwen3",
            catalog_snapshot_id=f"lf022_provider_catalog:{'d' * 64}",
            route_snapshot_revision=f"rcp-catalog-sha256:{'e' * 64}",
            underlying_checkpoint_revision_status="provider_not_disclosed",
            execution_scope="one_item_proposer_qualification_only",
            decoding=LF022RCPDecodingContract(
                contract_id="qwen3_5_proposer_qualification_v1",
                temperature=0.6,
                top_p=0.95,
                top_k=20,
                min_p=0.0,
                presence_penalty=0.0,
                repetition_penalty=1.0,
                max_tokens=4_096,
                seed=42,
                thinking_mode="enabled",
                reasoning_effort="high",
                chat_template_enable_thinking=True,
            ),
        )


def test_kimi_v4_contract_requires_replay_verified_eligibility(tmp_path: Path) -> None:
    historical, _ = _fixture(tmp_path)
    decoding = LF022RCPDecodingContract(
        contract_id="kimi_k2_7_public_proposer_v4",
        temperature=1.0,
        top_p=0.95,
        max_tokens=32_768,
        seed=42,
        thinking_mode="forced_thinking",
        reasoning_effort="high",
        chat_template_enable_thinking=True,
    )
    route = LF022RCPRouteBinding(
        provider_id="epfl_rcp",
        model_id="moonshotai/Kimi-K2.7-Code",
        deployment_id="moonshotai/Kimi-K2.7-Code",
        proposer_family_id="moonshot_kimi_k2",
        canonical_family="moonshotai/kimi-k2",
        catalog_snapshot_id=historical.route.catalog_snapshot_id,
        route_snapshot_revision=historical.route.route_snapshot_revision,
        underlying_checkpoint_revision_status="provider_not_disclosed",
        execution_scope="public_provisional_g_open",
        decoding=decoding,
    )
    with pytest.raises(ValueError, match="qualified production scope requires"):
        make_lf022_g_open_execution_admission(
            public_pool_audit_id=historical.public_pool_audit_id,
            allocation_plan_id=historical.allocation_plan_id,
            artifacts=historical.artifacts,
            route=route,
            retry_policy=historical.retry_policy,
            code_tree_hash=historical.code_tree_hash,
        )


@pytest.mark.parametrize(
    "model_id",
    (
        "moonshotai/Kimi-K2.7-Code",
        "Qwen/Qwen3.5-397B-A17B",
        "zai-org/GLM-5.2",
    ),
)
def test_exact_kimi_qwen_glm_routes_pass_offline_preflight(
    tmp_path: Path,
    model_id: str,
) -> None:
    admission, task = _fixture(tmp_path, model_id=model_id)
    transport = FakeTransport([])
    output_root = (
        tmp_path / "data/lf022_execution"
        if admission.route.execution_scope == "one_item_proposer_qualification_only"
        else tmp_path / "data/out"
    )
    result = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=output_root,
        admission=admission,
        task=task,
        transport=transport,
    )
    assert result.terminal is None
    assert result.network_calls_this_run == 0
    assert transport.calls == 0


def test_route_must_match_exact_family_matrix_pin(tmp_path: Path) -> None:
    admission, task = _fixture(
        tmp_path,
        matrix_deployment_override="moonshotai/WRONG-DEPLOYMENT",
    )
    transport = FakeTransport([])
    with pytest.raises(
        LF022ExecutionError,
        match="route identity differs from exact family-matrix pin",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            transport=transport,
        )
    assert transport.calls == 0


def test_qualification_route_contract_is_role_and_path_bound(tmp_path: Path) -> None:
    admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
        qualification_contract_path_override=(
            "configs/generation/not_the_reviewed_qwen_contract.yaml"
        ),
    )
    with pytest.raises(
        LF022ExecutionError,
        match="does not bind its canonical proposer contract",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
        )

    second_root = tmp_path / "role"
    second_root.mkdir()
    admission, task = _fixture(
        second_root,
        model_id="zai-org/GLM-5.2",
        qualification_contract_replacement=(b"role: proposer", b"role: judge"),
    )
    with pytest.raises(
        LF022ExecutionError,
        match="invalid proposer qualification contract",
    ):
        execute_lf022_g_open_task(
            repo_root=second_root,
            output_root=second_root / "data/out",
            admission=admission,
            task=task,
        )


def test_prior_judge_smoke_cannot_be_claimed_as_proposer_evidence(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        model_id="zai-org/GLM-5.2",
        qualification_contract_replacement=(
            b"proposer_evidence: false",
            b"proposer_evidence: true",
        ),
    )
    with pytest.raises(
        LF022ExecutionError,
        match="invalid proposer qualification contract",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
        )


@pytest.mark.parametrize(
    "model_id",
    ("Qwen/Qwen3.5-397B-A17B", "zai-org/GLM-5.2"),
)
def test_exact_live_v1_qualification_cannot_activate_production(
    tmp_path: Path,
    model_id: str,
) -> None:
    admission, task, eligibility, eligibility_binding = _qualify_and_certify(
        tmp_path,
        model_id=model_id,
    )
    assert eligibility.proposer_family_id == admission.route.proposer_family_id
    assert eligibility.model_id == model_id
    assert eligibility.deployment_id == admission.route.deployment_id
    assert eligibility.qualification_task_id == task.execution_task_id
    assert eligibility.qualification_task_count == 1
    assert eligibility.qualification_variant_count == 1
    assert eligibility.outputs_unresolved is True
    assert eligibility.semantic_labels_created is False
    assert eligibility.training_eligible is False
    assert eligibility.gate_credit_claimed is False
    assert eligibility.proposer_family_id not in eligibility.judge_family_ids
    assert eligibility.proposer_family_id not in eligibility.permitted_validator_family_ids
    assert eligibility.heldout_eval_family_id not in eligibility.permitted_validator_family_ids
    assert (
        verify_lf022_proposer_production_eligibility(
            repo_root=tmp_path,
            eligibility_binding=eligibility_binding,
        )
        == eligibility
    )

    production_artifacts = admission.artifacts.model_copy(
        update={"proposer_production_eligibility": eligibility_binding}
    )
    assert admission.schema_version == 1
    assert "proposer_production_eligibility" not in admission.artifacts.model_dump(mode="json")
    production_route_payload = admission.route.model_dump(mode="json")
    production_route_payload["execution_scope"] = "public_provisional_g_open"
    with pytest.raises(ValueError, match="v1 decoding is restricted"):
        make_lf022_g_open_execution_admission(
            public_pool_audit_id=admission.public_pool_audit_id,
            allocation_plan_id=admission.allocation_plan_id,
            artifacts=production_artifacts,
            route=LF022RCPRouteBinding.model_validate(production_route_payload),
            retry_policy=admission.retry_policy,
            code_tree_hash=admission.code_tree_hash,
        )


@pytest.mark.parametrize(
    "model_id",
    ("Qwen/Qwen3.5-397B-A17B", "zai-org/GLM-5.2"),
)
def test_production_scope_rejects_qualification_bypass(
    tmp_path: Path,
    model_id: str,
) -> None:
    admission, _ = _fixture(tmp_path, model_id=model_id)
    production_route = _v2_production_route(admission)
    with pytest.raises(ValueError, match="requires exactly one bound proposer eligibility"):
        make_lf022_g_open_execution_admission(
            public_pool_audit_id=admission.public_pool_audit_id,
            allocation_plan_id=admission.allocation_plan_id,
            artifacts=admission.artifacts,
            route=production_route,
            retry_policy=admission.retry_policy,
            code_tree_hash=admission.code_tree_hash,
        )


def test_cross_family_qualification_cannot_authorize_production(
    tmp_path: Path,
) -> None:
    qwen_admission, _, _, qwen_eligibility = _qualify_and_certify(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
    )
    glm_contract = _copy_repo_artifact(
        tmp_path,
        "configs/generation/lf022_glm5_2_proposer_qualification_v2.yaml",
    )
    _copy_repo_artifact(
        tmp_path,
        (
            "data/raw/llm_variants/lf022_rcp_public_smoke_v3/"
            "61e201acc254d89cb5e9686bd56a7f4e03c0ea2f8169ae39e22cc31be48a0589/"
            "calls/judge_B_AB/wire_request.json"
        ),
    )
    _copy_repo_artifact(
        tmp_path,
        (
            "data/raw/llm_variants/lf022_rcp_public_smoke_v3/"
            "61e201acc254d89cb5e9686bd56a7f4e03c0ea2f8169ae39e22cc31be48a0589/"
            "calls/judge_B_AB/wire_response.json"
        ),
    )
    glm_route = LF022RCPRouteBinding(
        provider_id="epfl_rcp",
        model_id="zai-org/GLM-5.2",
        deployment_id="zai-org/GLM-5.2",
        proposer_family_id="glm5",
        canonical_family="zai-org/glm-5.2",
        catalog_snapshot_id=qwen_admission.route.catalog_snapshot_id,
        route_snapshot_revision=qwen_admission.route.route_snapshot_revision,
        underlying_checkpoint_revision_status="provider_not_disclosed",
        execution_scope="public_provisional_g_open",
        decoding=LF022RCPDecodingContract(
            contract_id="glm5_2_proposer_qualification_v2",
            temperature=0.0,
            top_p=1.0,
            max_tokens=8192,
            seed=42,
            thinking_mode="enabled",
            reasoning_effort="high",
            chat_template_enable_thinking=True,
        ),
    )
    artifacts = qwen_admission.artifacts.model_copy(
        update={
            "reviewed_route_contract": glm_contract,
            "proposer_production_eligibility": qwen_eligibility,
        }
    )
    admission = make_lf022_g_open_execution_admission(
        public_pool_audit_id=qwen_admission.public_pool_audit_id,
        allocation_plan_id=qwen_admission.allocation_plan_id,
        artifacts=artifacts,
        route=glm_route,
        retry_policy=qwen_admission.retry_policy,
        code_tree_hash=qwen_admission.code_tree_hash,
    )
    with pytest.raises(
        LF022ExecutionError,
        match=r"eligibility belongs to a different route|eligibility identity differs",
    ):
        verify_lf022_execution_admission(repo_root=tmp_path, admission=admission)


def test_qualification_terminal_cannot_claim_semantic_labels(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
    )
    live = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/lf022_execution",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([_success_response(admission.route.model_id)]),
        clock=lambda: NOW,
    )
    assert live.terminal_path is not None
    payload = json.loads(live.terminal_path.read_bytes())
    payload["semantic_labels_created"] = True
    live.terminal_path.write_bytes(canonical_json_bytes(payload) + b"\n")
    task_dir = _qualification_task_dir(tmp_path, task)
    with pytest.raises(
        LF022RouteQualificationError,
        match="qualification exact replay rejected",
    ):
        certify_lf022_proposer_production_eligibility(
            repo_root=tmp_path,
            qualification_admission_binding=LF022ArtifactBinding(
                path=str((task_dir / "admission.json").relative_to(tmp_path)),
                sha256=hash_file(task_dir / "admission.json"),
            ),
            qualification_task_binding=LF022ArtifactBinding(
                path=str((task_dir / "task.json").relative_to(tmp_path)),
                sha256=hash_file(task_dir / "task.json"),
            ),
        )


def test_unbound_reasoning_capability_is_rejected_before_transport(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
        qualification_contract_replacement=(
            b"reasoning_effort_values:\n    - high",
            b"reasoning_effort_values: []",
        ),
    )
    transport = FakeTransport([_success_response(admission.route.model_id)])
    with pytest.raises(
        LF022ExecutionError,
        match="invalid proposer qualification contract",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/lf022_execution",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
        )
    assert transport.calls == 0


def test_reasoning_capability_must_be_one_exact_replay_manifest_call(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
        qualification_contract_replacement=(
            b"accepted_call_label: judge_A_AB",
            b"accepted_call_label: judge_A_BA",
        ),
    )
    transport = FakeTransport([])
    with pytest.raises(
        LF022ExecutionError,
        match="not one exact replay-manifest call",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/lf022_execution",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
        )
    assert transport.calls == 0


def test_reasoning_capability_requires_bound_successful_response(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
    )
    response_path = (
        tmp_path
        / "data/raw/llm_variants/lf022_rcp_public_smoke_v3"
        / "61e201acc254d89cb5e9686bd56a7f4e03c0ea2f8169ae39e22cc31be48a0589"
        / "calls/judge_A_AB/wire_response.json"
    )
    response = json.loads(response_path.read_bytes())
    response["choices"][0]["finish_reason"] = "length"
    response_path.write_bytes(canonical_json_bytes(response) + b"\n")

    manifest_path = response_path.parents[2] / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    accepted_call = next(
        call for call in manifest["call_artifacts"] if call["call_label"] == "judge_A_AB"
    )
    accepted_call["wire_response_sha256"] = hash_file(response_path)
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")

    evidence_path = tmp_path / admission.artifacts.reviewed_route_evidence.path
    evidence = json.loads(evidence_path.read_bytes())
    evidence["verified_success"]["manifest_sha256"] = hash_file(manifest_path)
    evidence_path.write_bytes(canonical_json_bytes(evidence) + b"\n")
    evidence_binding = LF022ArtifactBinding(
        path=admission.artifacts.reviewed_route_evidence.path,
        sha256=hash_file(evidence_path),
    )

    contract_path = tmp_path / admission.artifacts.reviewed_route_contract.path
    old_evidence_sha = admission.artifacts.reviewed_route_evidence.sha256.encode()
    contract_bytes = contract_path.read_bytes()
    assert contract_bytes.count(old_evidence_sha) == 2
    contract_path.write_bytes(
        contract_bytes.replace(
            old_evidence_sha,
            evidence_binding.sha256.encode(),
        )
    )
    contract_binding = LF022ArtifactBinding(
        path=admission.artifacts.reviewed_route_contract.path,
        sha256=hash_file(contract_path),
    )
    artifacts = admission.artifacts.model_copy(
        update={
            "reviewed_route_contract": contract_binding,
            "reviewed_route_evidence": evidence_binding,
        }
    )
    mutated_admission = make_lf022_g_open_execution_admission(
        public_pool_audit_id=admission.public_pool_audit_id,
        allocation_plan_id=admission.allocation_plan_id,
        artifacts=artifacts,
        route=admission.route,
        retry_policy=admission.retry_policy,
        code_tree_hash=admission.code_tree_hash,
    )
    mutated_task = make_lf022_g_open_execution_task(
        admission=mutated_admission,
        allocation_task=task.allocation_task,
        source=task.source,
    )
    transport = FakeTransport([])
    with pytest.raises(
        LF022ExecutionError,
        match="does not prove successful exact route handling",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/lf022_execution",
            admission=mutated_admission,
            task=mutated_task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
        )
    assert transport.calls == 0


@pytest.mark.parametrize(
    "model_id",
    ("Qwen/Qwen3.5-397B-A17B", "zai-org/GLM-5.2"),
)
def test_qualification_routes_enforce_one_proposal(
    tmp_path: Path,
    model_id: str,
) -> None:
    admission, task = _fixture(tmp_path, model_id=model_id)
    with pytest.raises(
        LF022ExecutionError,
        match="require exactly one requested proposal",
    ):
        make_lf022_g_open_execution_task(
            admission=admission,
            allocation_task=task.allocation_task,
            source=task.source,
            proposal_count=2,
        )


def test_lean_only_executor_rejects_optional_natural_language(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    source = task.source.model_copy(
        update={"optional_natural_language": "Do not transmit this caller text."}
    )
    with pytest.raises(
        ValueError,
        match="forbids optional natural-language prompt content",
    ):
        make_lf022_g_open_execution_task(
            admission=admission,
            allocation_task=task.allocation_task,
            source=source,
        )


def test_raw_catalog_must_cover_every_normalized_deployment(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        raw_catalog_omit_model_id="deepseek-ai/DeepSeek-V4-Pro",
    )
    transport = FakeTransport([])
    with pytest.raises(
        LF022ExecutionError,
        match="contains IDs absent from raw /models response",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
        )
    assert transport.calls == 0


def test_current_code_tree_must_match_admission_before_preflight(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    (tmp_path / "fixture_code.py").write_text("VALUE = 2\n", encoding="utf-8")
    transport = FakeTransport([])
    with pytest.raises(
        LF022ExecutorError,
        match="current repository code tree differs",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
        )
    assert transport.calls == 0


def test_denylist_hit_is_rejected_before_transport(tmp_path: Path) -> None:
    admission, task = _fixture(
        tmp_path,
        clearance_identifier_hits=("fixture_eval:protected",),
    )
    transport = FakeTransport([])
    with pytest.raises(
        LF022ExecutionError,
        match="denylist clearance does not exactly and clearly bind",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
        )
    assert transport.calls == 0


def test_denylist_clearance_binding_mismatch_is_rejected_before_transport(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        clearance_theorem_hash_override="0" * 64,
    )
    transport = FakeTransport([])
    with pytest.raises(
        LF022ExecutionError,
        match="denylist clearance does not exactly and clearly bind",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
        )
    assert transport.calls == 0


def test_tampered_denylist_clearance_artifact_is_rejected_before_transport(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    clearance_path = tmp_path / "artifacts" / "denylist_clearances.jsonl"
    clearance_path.write_bytes(clearance_path.read_bytes() + b"\n")
    transport = FakeTransport([])
    with pytest.raises(LF022ExecutionError, match="SHA-256 mismatch"):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
        )
    assert transport.calls == 0


def test_private_source_is_rejected_before_execution(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    private_source = task.source.model_copy(
        update={
            "source_id": "formalmathatepfl/sft_classic",
            "source_is_public": False,
            "external_transmission_allowed": False,
        }
    )
    with pytest.raises(ValueError, match="private sft_classic"):
        make_lf022_g_open_execution_task(
            admission=admission,
            allocation_task=task.allocation_task,
            source=private_source,
        )


def test_prompt_statement_must_match_bound_public_pool(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    tampered_source = task.source.model_copy(
        update={"source_statement": ("theorem public_source (n : Nat) : n + 0 = n")}
    )
    tampered_task = make_lf022_g_open_execution_task(
        admission=admission,
        allocation_task=task.allocation_task,
        source=tampered_source,
    )
    with pytest.raises(
        LF022ExecutionError,
        match="prompt source content differs from the bound public pool",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=tampered_task,
        )


def test_default_is_network_free_preflight(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    transport = FakeTransport([_success_response(admission.route.model_id)])
    result = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        transport=transport,
    )
    assert result.terminal is None
    assert result.network_calls_this_run == 0
    assert transport.calls == 0


def test_stale_lock_file_does_not_block_execution(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    prepared = prepare_lf022_g_open_execution(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
    )
    lock_path = prepared.task_directory / ".lock"
    lock_path.write_text("stale but unlocked\n", encoding="utf-8")
    transport = FakeTransport([_success_response(admission.route.model_id)])
    result = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=transport,
        clock=lambda: NOW,
    )
    assert result.terminal is not None
    assert result.terminal.status == "provisional_variants_created"
    assert transport.calls == 1


def test_live_advisory_lock_blocks_second_executor_without_transport(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    prepared = prepare_lf022_g_open_execution(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
    )
    lock_path = prepared.task_directory / ".lock"
    with lock_path.open("a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        transport = FakeTransport([])
        with pytest.raises(LF022TaskLockedError, match="already locked"):
            execute_lf022_g_open_task(
                repo_root=tmp_path,
                output_root=tmp_path / "data/out",
                admission=admission,
                task=task,
                execute_public_provisional=True,
                credentials=_credentials(),
                transport=transport,
                clock=lambda: NOW,
            )
        assert transport.calls == 0


def test_cli_requires_explicit_live_flag(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    admission_path = tmp_path / "admission.json"
    task_path = tmp_path / "task.json"
    admission_path.write_bytes(canonical_json_bytes(admission.model_dump(mode="json")))
    task_path.write_bytes(canonical_json_bytes(task.model_dump(mode="json")))
    result = CliRunner().invoke(
        app,
        [
            "run-lf022-public-provisional",
            "--root",
            str(tmp_path),
            "--admission",
            str(admission_path),
            "--task",
            str(task_path),
            "--output-root",
            str(tmp_path / "data/out"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "mode=preflight" in result.output
    assert "network_calls_this_run=0" in result.output
    assert "semantic_labels_created=0" in result.output


def test_cli_rejects_live_kimi_v3_but_keeps_offline_preflight(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    admission_path = tmp_path / "admission.json"
    task_path = tmp_path / "task.json"
    admission_path.write_bytes(canonical_json_bytes(admission.model_dump(mode="json")))
    task_path.write_bytes(canonical_json_bytes(task.model_dump(mode="json")))
    result = CliRunner().invoke(
        app,
        [
            "run-lf022-public-provisional",
            "--root",
            str(tmp_path),
            "--admission",
            str(admission_path),
            "--task",
            str(task_path),
            "--output-root",
            str(tmp_path / "data/lf022_execution"),
            "--execute-public-provisional",
        ],
        env={
            "RCP_BASE_URL": "https://inference.rcp.epfl.ch/v1",
            "RCP_API_KEY": "must-not-be-used",
        },
    )
    assert result.exit_code == 2
    assert "live Kimi-v3 execution is archived" in result.output


def test_scientific_kimi_v3_allows_offline_preflight_but_rejects_live(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        profile="scientific_production_scaffold",
    )
    offline = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/lf022_execution",
        admission=admission,
        task=task,
    )
    assert offline.terminal is None
    assert offline.network_calls_this_run == 0

    transport = FakeTransport([_success_response(admission.route.model_id)])
    with pytest.raises(LF022ExecutorError, match="live Kimi-v3 scientific execution is archived"):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/lf022_execution",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
            clock=lambda: NOW,
        )
    assert transport.calls == 0


def test_success_is_provisional_raw_first_and_exactly_replayed(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    transport = FakeTransport([_success_response(admission.route.model_id)])
    first = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=transport,
        clock=lambda: NOW,
    )
    assert first.terminal is not None
    assert first.terminal.status == "provisional_variants_created"
    assert first.terminal.provisional_variant_count == 1
    assert first.terminal.semantic_labels_created is False
    assert first.terminal.silver_promotion_enabled is False
    assert first.terminal.training_eligible is False
    assert transport.calls == 1
    assert len(transport.payloads) == 1
    wire_payload = dict(transport.payloads[0])
    assert wire_payload.pop("model") == admission.route.model_id
    messages = wire_payload.pop("messages")
    assert isinstance(messages, list)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert all(message["content"] for message in messages)
    assert wire_payload == admission.route.decoding.wire_fields()
    assert first.terminal.variants_artifact is not None
    variant_line = (tmp_path / first.terminal.variants_artifact).read_bytes()
    variant = VariantRecord.model_validate_json(variant_line)
    assert variant.quality_tier is QualityTier.PROVISIONAL
    assert variant.validation_status is ValidationStatus.UNVALIDATED
    attempt_path = tmp_path / first.terminal.attempt_artifacts[0]
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    raw_body = tmp_path / attempt["wire_response_body_artifact"]
    assert raw_body.is_file()

    replay_transport = FakeTransport([])
    second = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=replay_transport,
        clock=lambda: NOW,
    )
    assert second.replayed is True
    assert second.network_calls_this_run == 0
    assert second.terminal == first.terminal
    assert replay_transport.calls == 0


def test_terminal_replay_rejects_tampered_attempt_raw_lineage(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    first = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([_success_response(admission.route.model_id)]),
        clock=lambda: NOW,
    )
    assert first.terminal is not None
    attempt = json.loads(
        (tmp_path / first.terminal.attempt_artifacts[0]).read_text(encoding="utf-8")
    )
    (tmp_path / attempt["provider_raw_artifact"]).write_bytes(b"{}\n")

    with pytest.raises(
        LF022ExecutorError,
        match=r"invalid provider raw response|provider raw response bytes differ",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=FakeTransport([]),
            clock=lambda: NOW,
        )


def test_terminal_replay_rejects_tampered_llm_attempt_binding(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    first = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([_success_response(admission.route.model_id)]),
        clock=lambda: NOW,
    )
    assert first.terminal is not None
    llm_attempt_path = tmp_path / first.terminal.llm_attempt_artifacts[0]
    payload = json.loads(llm_attempt_path.read_bytes())
    payload["latency_ms"] += 1
    llm_attempt_path.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(
        LF022ExecutorError,
        match="LLM attempt artifact path or hash drifted",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=FakeTransport([]),
            clock=lambda: NOW,
        )


def test_terminal_replay_rejects_coherently_rehashed_training_eligibility(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    first = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([_success_response(admission.route.model_id)]),
        clock=lambda: NOW,
    )
    assert first.terminal is not None
    assert first.terminal_path is not None

    call_path = tmp_path / first.terminal.llm_call_artifact
    call_payload = json.loads(call_path.read_bytes())
    call_payload["supervision_eligible"] = True
    call_payload["metadata"]["training_eligible"] = True
    call_path.write_bytes(canonical_json_bytes(call_payload) + b"\n")

    terminal_payload = json.loads(first.terminal_path.read_bytes())
    terminal_payload["llm_call_sha256"] = hash_file(call_path)
    terminal_payload.pop("terminal_id")
    terminal_payload["terminal_id"] = make_id(
        "lf022_execution_terminal",
        terminal_payload,
    )
    first.terminal_path.write_bytes(canonical_json_bytes(terminal_payload) + b"\n")

    with pytest.raises(
        LF022ExecutorError,
        match="LLM call artifact hash drifted",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=FakeTransport([]),
            clock=lambda: NOW,
        )


def test_terminal_replay_recomputes_semantics_from_attempts_and_call(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    first = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([_success_response(admission.route.model_id)]),
        clock=lambda: NOW,
    )
    assert first.terminal_path is not None
    payload = json.loads(first.terminal_path.read_bytes())
    payload.update(
        {
            "status": "provider_exhausted",
            "variants_artifact": None,
            "variants_sha256": None,
            "provisional_variant_count": 0,
            "terminal_error_code": "fabricated_failure",
        }
    )
    payload.pop("terminal_id")
    payload["terminal_id"] = make_id("lf022_execution_terminal", payload)
    first.terminal_path.write_bytes(canonical_json_bytes(payload) + b"\n")

    with pytest.raises(
        LF022ExecutorError,
        match="terminal semantics differ from verified attempts and proposer call",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=FakeTransport([]),
            clock=lambda: NOW,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        (
            "execution_task_id",
            f"lf022_execution_task:{'0' * 64}",
            "persisted attempt task identity or attempt index differs",
        ),
        (
            "attempt_index",
            1,
            "persisted attempt task identity or attempt index differs",
        ),
        (
            "request_sha256",
            "0" * 64,
            "provider request hash differs from attempt record",
        ),
    ),
)
def test_recovery_fully_verifies_existing_attempt_before_reuse(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    admission, task = _fixture(tmp_path)
    first = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([_success_response(admission.route.model_id)]),
        clock=lambda: NOW,
    )
    assert first.terminal is not None
    assert first.terminal_path is not None
    first.terminal_path.unlink()
    attempt_path = tmp_path / first.terminal.attempt_artifacts[0]
    payload = json.loads(attempt_path.read_bytes())
    payload[field] = value
    attempt_path.write_bytes(canonical_json_bytes(payload) + b"\n")
    transport = FakeTransport([])
    with pytest.raises(LF022ExecutorError, match=message):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
            clock=lambda: NOW,
        )
    assert transport.calls == 0


def test_retry_after_then_success_preserves_both_attempts(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    transport = FakeTransport(
        [
            RCPWireResponse(
                status_code=429,
                headers={"retry-after": "7"},
                body=b'{"error":"busy"}',
            ),
            _success_response(admission.route.model_id),
        ]
    )
    delays: list[float] = []
    result = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=transport,
        sleeper=delays.append,
        clock=lambda: NOW,
    )
    assert result.terminal is not None
    assert result.terminal.status == "provisional_variants_created"
    assert len(result.terminal.attempt_artifacts) == 2
    assert result.network_calls_this_run == 2
    assert delays == [7.0]
    call = LLMCallRecord.model_validate_json(
        (tmp_path / result.terminal.llm_call_artifact).read_bytes()
    )
    assert call.retry_count == 1
    assert len(call.attempt_ids) == 2


def test_parse_failure_is_not_retried(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    response = RCPWireResponse(
        status_code=200,
        headers={},
        body=canonical_json_bytes(
            {
                "id": "fixture-bad-json",
                "model": admission.route.model_id,
                "choices": [{"message": {"content": "not task json"}}],
            }
        ),
    )
    transport = FakeTransport([response, _success_response(admission.route.model_id)])
    result = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=transport,
        clock=lambda: NOW,
    )
    assert result.terminal is not None
    assert result.terminal.status == "proposer_parse_failed"
    assert result.terminal.provisional_variant_count == 0
    assert transport.calls == 1


def test_failed_qualification_supersession_is_append_only_and_replay_verified(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
    )
    bad_response = RCPWireResponse(
        status_code=200,
        headers={},
        body=canonical_json_bytes(
            {
                "id": "fixture-bad-qualification",
                "model": admission.route.model_id,
                "choices": [{"message": {"content": "not task json"}}],
            }
        ),
    )
    result = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/lf022_execution",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([bad_response]),
        clock=lambda: NOW,
    )
    assert result.terminal is not None
    assert result.terminal.status == "proposer_parse_failed"
    task_dir = _qualification_task_dir(tmp_path, task)
    admission_path = task_dir / "admission.json"
    task_path = task_dir / "task.json"
    terminal_path = task_dir / "terminal.json"
    hashes_before = tuple(hash_file(path) for path in (admission_path, task_path, terminal_path))
    superseded = supersede_lf022_failed_qualification(
        repo_root=tmp_path,
        previous_admission_binding=LF022ArtifactBinding(
            path=admission_path.relative_to(tmp_path).as_posix(),
            sha256=hash_file(admission_path),
        ),
        previous_task_binding=LF022ArtifactBinding(
            path=task_path.relative_to(tmp_path).as_posix(),
            sha256=hash_file(task_path),
        ),
        next_decoding_contract_id="qwen3_5_proposer_qualification_v2",
    )
    binding = LF022ArtifactBinding(
        path=superseded.supersession_path.relative_to(tmp_path).as_posix(),
        sha256=hash_file(superseded.supersession_path),
    )
    assert (
        verify_lf022_qualification_supersession(
            repo_root=tmp_path,
            supersession_binding=binding,
        )
        == superseded.supersession
    )
    assert superseded.supersession.previous_terminal_status == "proposer_parse_failed"
    assert superseded.supersession.replay_network_calls == 0
    assert tuple(hash_file(path) for path in (admission_path, task_path, terminal_path)) == (
        hashes_before
    )
    v2_contract = _copy_repo_artifact(
        tmp_path,
        "configs/generation/lf022_qwen3_5_proposer_qualification_v2.yaml",
    )
    new_code_tree_hash = collect_code_state(tmp_path).code_tree_hash
    assert new_code_tree_hash is not None
    new_code_bundle = _code_bundle(tmp_path, code_tree_hash=new_code_tree_hash)
    v2_decoding = LF022RCPDecodingContract(
        contract_id="qwen3_5_proposer_qualification_v2",
        temperature=0.6,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.0,
        repetition_penalty=1.0,
        max_tokens=16_384,
        seed=42,
        thinking_mode="enabled",
        reasoning_effort="high",
        chat_template_enable_thinking=True,
    )
    v2_route_payload = admission.route.model_dump(mode="json")
    v2_route_payload["decoding"] = v2_decoding.model_dump(mode="json")
    v2_route = LF022RCPRouteBinding.model_validate(v2_route_payload)
    v2_artifact_payload = admission.artifacts.model_dump(mode="json")
    v2_artifact_payload.update(
        {
            "reviewed_route_contract": v2_contract.model_dump(mode="json"),
            "code_bundle": new_code_bundle.model_dump(mode="json"),
            "qualification_supersession": binding.model_dump(mode="json"),
        }
    )
    v2_admission = make_lf022_g_open_execution_admission(
        public_pool_audit_id=admission.public_pool_audit_id,
        allocation_plan_id=admission.allocation_plan_id,
        artifacts=LF022ExecutionArtifacts.model_validate(v2_artifact_payload),
        route=v2_route,
        retry_policy=admission.retry_policy,
        code_tree_hash=new_code_tree_hash,
    )
    v2_task = make_lf022_g_open_execution_task(
        admission=v2_admission,
        allocation_task=task.allocation_task,
        source=task.source,
    )
    recovery_preflight = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/lf022_execution",
        admission=v2_admission,
        task=v2_task,
    )
    assert recovery_preflight.terminal is None
    assert recovery_preflight.network_calls_this_run == 0
    assert (
        tmp_path
        / "data/lf022_execution/qualification_claims/qwen3"
        / f"{make_lf022_qualification_claim(admission=v2_admission, task=v2_task).claim_id.split(':', 1)[1]}.json"
    ).is_file()
    recovery_live = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/lf022_execution",
        admission=v2_admission,
        task=v2_task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([_success_response(v2_admission.route.model_id)]),
        clock=lambda: NOW,
    )
    assert recovery_live.terminal is not None
    assert recovery_live.terminal.status == "provisional_variants_created"
    v2_task_dir = _qualification_task_dir(tmp_path, v2_task)
    post_qualification_change = tmp_path / "src/post_qualification_change.py"
    post_qualification_change.parent.mkdir(parents=True, exist_ok=True)
    post_qualification_change.write_text("VALUE = 'newer worktree'\n", encoding="utf-8")
    assert collect_code_state(tmp_path).code_tree_hash != v2_admission.code_tree_hash
    certified = certify_lf022_proposer_production_eligibility(
        repo_root=tmp_path,
        qualification_admission_binding=LF022ArtifactBinding(
            path=(v2_task_dir / "admission.json").relative_to(tmp_path).as_posix(),
            sha256=hash_file(v2_task_dir / "admission.json"),
        ),
        qualification_task_binding=LF022ArtifactBinding(
            path=(v2_task_dir / "task.json").relative_to(tmp_path).as_posix(),
            sha256=hash_file(v2_task_dir / "task.json"),
        ),
    )
    assert certified.eligibility.decoding_contract_id == ("qwen3_5_proposer_qualification_v2")


def test_successful_qualification_cannot_be_superseded(tmp_path: Path) -> None:
    admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
    )
    result = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/lf022_execution",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([_success_response(admission.route.model_id)]),
        clock=lambda: NOW,
    )
    assert result.terminal is not None
    assert result.terminal.status == "provisional_variants_created"
    task_dir = _qualification_task_dir(tmp_path, task)
    with pytest.raises(
        LF022RouteQualificationError,
        match="failed qualification",
    ):
        supersede_lf022_failed_qualification(
            repo_root=tmp_path,
            previous_admission_binding=LF022ArtifactBinding(
                path=(task_dir / "admission.json").relative_to(tmp_path).as_posix(),
                sha256=hash_file(task_dir / "admission.json"),
            ),
            previous_task_binding=LF022ArtifactBinding(
                path=(task_dir / "task.json").relative_to(tmp_path).as_posix(),
                sha256=hash_file(task_dir / "task.json"),
            ),
            next_decoding_contract_id="qwen3_5_proposer_qualification_v2",
        )


def test_transport_unknown_qualification_cannot_be_superseded(tmp_path: Path) -> None:
    admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
    )
    result = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/lf022_execution",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=UnknownTransport(),
        clock=lambda: NOW,
    )
    assert result.terminal is not None
    assert result.terminal.status == "transport_unknown"
    task_dir = _qualification_task_dir(tmp_path, task)
    with pytest.raises(
        LF022RouteQualificationError,
        match="failed qualification",
    ):
        supersede_lf022_failed_qualification(
            repo_root=tmp_path,
            previous_admission_binding=LF022ArtifactBinding(
                path=(task_dir / "admission.json").relative_to(tmp_path).as_posix(),
                sha256=hash_file(task_dir / "admission.json"),
            ),
            previous_task_binding=LF022ArtifactBinding(
                path=(task_dir / "task.json").relative_to(tmp_path).as_posix(),
                sha256=hash_file(task_dir / "task.json"),
            ),
            next_decoding_contract_id="qwen3_5_proposer_qualification_v2",
        )


def test_transport_unknown_is_terminal_and_replays_without_retry(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    transport = UnknownTransport()
    first = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=transport,
        clock=lambda: NOW,
    )
    assert first.terminal is not None
    assert first.terminal.status == "transport_unknown"
    assert first.terminal.provisional_variant_count == 0
    assert first.network_calls_this_run == 1
    assert transport.calls == 1

    replay_transport = FakeTransport([])
    replay = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=replay_transport,
        clock=lambda: NOW,
    )
    assert replay.replayed is True
    assert replay.network_calls_this_run == 0
    assert replay_transport.calls == 0


def test_transport_completed_without_response_fails_closed_without_call(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    prepared = prepare_lf022_g_open_execution(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
    )
    attempt_dir = prepared.task_directory / "attempts/0000"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / ".transport_completed").write_text(
        "completed\n",
        encoding="utf-8",
    )
    transport = FakeTransport([])
    with pytest.raises(
        LF022ExecutorError,
        match="transport-completed marker exists without persisted wire response",
    ):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
            clock=lambda: NOW,
        )
    assert transport.calls == 0


def test_interruption_after_raw_response_resumes_without_second_call(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    transport = FakeTransport([_success_response(admission.route.model_id)])

    def interrupt() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=transport,
            clock=lambda: NOW,
            after_wire_response_persisted=interrupt,
        )
    assert transport.calls == 1

    replay_transport = FakeTransport([])
    resumed = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/out",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=replay_transport,
        clock=lambda: NOW,
    )
    assert resumed.terminal is not None
    assert resumed.terminal.status == "provisional_variants_created"
    assert resumed.network_calls_this_run == 0
    assert replay_transport.calls == 0


def _batch_request_binding(
    root: Path,
    *,
    admission: LF022GOpenExecutionAdmission,
    task: LF022GOpenExecutionTask,
    batch_directory: str = "data/lf022_batch",
    request_path: str = "data/lf022_batch_request.json",
    executor_output_root: str = "data/lf022_execution",
) -> LF022ArtifactBinding:
    request = make_lf022_batch_freeze_request(
        batch_directory=batch_directory,
        executor_output_root=executor_output_root,
        routes=(
            LF022BatchRouteFreezeRequest(
                proposer_family_id=admission.route.proposer_family_id,
                public_pool_audit_id=admission.public_pool_audit_id,
                allocation_plan_id=admission.allocation_plan_id,
                execution_artifacts=admission.artifacts,
                route=admission.route,
                retry_policy=admission.retry_policy,
                code_tree_hash=admission.code_tree_hash,
                allocation_task_ids=(task.allocation_task.task_id,),
            ),
        ),
    )
    return _write_json(
        root,
        request_path,
        request.model_dump(mode="json"),
    )


def test_batch_request_cli_constructs_one_exact_qualification_route_offline(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        model_id="Qwen/Qwen3.5-397B-A17B",
    )
    admission_path = tmp_path / "artifacts/reviewed_qualification_admission.json"
    admission_path.write_bytes(canonical_json_bytes(admission.model_dump(mode="json")) + b"\n")
    output = tmp_path / "data/qwen_qualification_batch_request.json"
    result = CliRunner().invoke(
        app,
        [
            "make-lf022-public-batch-request",
            "--root",
            str(tmp_path),
            "--admission",
            str(admission_path),
            "--allocation-task-id",
            task.allocation_task.task_id,
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "network_calls_this_run=0" in result.output
    request = LF022BatchFreezeRequest.model_validate_json(output.read_bytes())
    assert request.routes[0].proposer_family_id == "qwen3"
    assert request.routes[0].allocation_task_ids == (task.allocation_task.task_id,)
    assert request.private_source_content_forbidden is True
    replay = CliRunner().invoke(
        app,
        [
            "make-lf022-public-batch-request",
            "--root",
            str(tmp_path),
            "--admission",
            str(admission_path),
            "--allocation-task-id",
            task.allocation_task.task_id,
            "--output",
            str(output),
        ],
    )
    assert replay.exit_code == 0, replay.output
    escaped = tmp_path.parent / "escaped_lf022_batch_request.json"
    escaped.unlink(missing_ok=True)
    rejected = CliRunner().invoke(
        app,
        [
            "make-lf022-public-batch-request",
            "--root",
            str(tmp_path),
            "--admission",
            str(admission_path),
            "--allocation-task-id",
            task.allocation_task.task_id,
            "--output",
            "../escaped_lf022_batch_request.json",
        ],
    )
    assert rejected.exit_code == 2
    assert not escaped.exists()


def test_new_scientific_kimi_v3_batch_request_is_archived_before_output(
    tmp_path: Path,
) -> None:
    admission, _ = _fixture(
        tmp_path,
        profile="scientific_production_scaffold",
    )
    admission_path = tmp_path / "artifacts/kimi_scientific_admission.json"
    admission_path.write_bytes(canonical_json_bytes(admission.model_dump(mode="json")) + b"\n")
    output = tmp_path / "data/kimi_scientific_batch_request.json"

    rejected = CliRunner().invoke(
        app,
        [
            "make-lf022-public-batch-request",
            "--root",
            str(tmp_path),
            "--admission",
            str(admission_path),
            "--allocation-offset",
            "0",
            "--allocation-limit",
            "1",
            "--output",
            str(output),
        ],
    )

    assert rejected.exit_code == 2
    assert "new Kimi-v3 batch requests are archived" in rejected.output
    assert not output.exists()


def test_new_scientific_kimi_v3_batch_freeze_is_archived_before_output(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(
        tmp_path,
        profile="scientific_production_scaffold",
    )
    batch_directory = tmp_path / "data/lf022_batch"
    with pytest.raises(LF022BatchError, match="new Kimi-v3 batch freezing is archived"):
        freeze_lf022_public_batch(
            repo_root=tmp_path,
            request_binding=_batch_request_binding(
                tmp_path,
                admission=admission,
                task=task,
            ),
        )
    assert not batch_directory.exists()


def test_batch_freeze_and_offline_replay_are_deterministic(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    request_binding = _batch_request_binding(
        tmp_path,
        admission=admission,
        task=task,
    )
    frozen = freeze_lf022_public_batch(
        repo_root=tmp_path,
        request_binding=request_binding,
    )
    replayed_freeze = freeze_lf022_public_batch(
        repo_root=tmp_path,
        request_binding=request_binding,
    )
    assert replayed_freeze.manifest == frozen.manifest
    assert replayed_freeze.manifest_path.read_bytes() == frozen.manifest_path.read_bytes()
    assert frozen.manifest.total_task_count == 1
    assert frozen.manifest.semantic_labels_created is False
    assert frozen.manifest.training_eligible is False
    route = frozen.manifest.routes[0]
    frozen_task = LF022GOpenExecutionTask.model_validate_json(
        (tmp_path / route.tasks[0].task.path).read_bytes()
    )
    assert frozen_task.source.optional_natural_language is None
    assert frozen_task.source.source_statement == "theorem public_source : ∀ (n : Nat), n = n"
    assert frozen_task.source_statement_version == "named_signature_v2"
    assert frozen_task.training_eligible is False

    manifest_binding = LF022ArtifactBinding(
        path=str(frozen.manifest_path.relative_to(tmp_path)),
        sha256=hash_file(frozen.manifest_path),
    )
    first = run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=manifest_binding,
        policy=LF022BatchRunPolicy(max_concurrency=2),
    )
    second = run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=manifest_binding,
        policy=LF022BatchRunPolicy(max_concurrency=2),
    )
    assert first.report == second.report
    assert first.report.preflight_only_count == 1
    assert first.report.network_calls_this_run == 0
    assert first.report.error_count == 0
    events = sorted((tmp_path / frozen.manifest.journal_directory).glob("*/*.json"))
    assert len(events) == 1


def test_named_signature_is_bound_and_rejects_command_injection(tmp_path: Path) -> None:
    _fixture(tmp_path)
    theorem = TheoremRecord.model_validate_json(
        (tmp_path / "artifacts/theorems.jsonl").read_bytes().splitlines()[0]
    )
    representation = RepresentationRecord.model_validate_json(
        (tmp_path / "artifacts/representations.jsonl").read_bytes().splitlines()[0]
    )
    assert (
        make_lf022_named_signature(
            theorem=theorem.model_copy(update={"declaration_name": "public_source!"}),
            representation=representation,
        )
        == "theorem public_source! : ∀ (n : Nat), n = n"
    )
    assert (
        make_lf022_named_signature(
            theorem=theorem.model_copy(update={"declaration_name": "public_source?"}),
            representation=representation,
        )
        == "theorem public_source? : ∀ (n : Nat), n = n"
    )
    assert (
        make_lf022_named_signature(
            theorem=theorem,
            representation=representation.model_copy(
                update={"signature_pp": "{ re := 1, im := 0 } = Complex.ofReal 1"}
            ),
        )
        == "theorem public_source : { re := 1, im := 0 } = Complex.ofReal 1"
    )
    assert (
        make_lf022_named_signature(
            theorem=theorem,
            representation=representation.model_copy(
                update={"signature_pp": "(let value := 1; value) = 1"}
            ),
        )
        == "theorem public_source : (let value := 1; value) = 1"
    )
    with pytest.raises(LF022ExecutionError, match="named theorem"):
        make_lf022_named_signature(
            theorem=theorem.model_copy(update={"declaration_name": "public_source\n#check False"}),
            representation=representation,
        )
    with pytest.raises(LF022ExecutionError, match="named theorem"):
        make_lf022_named_signature(
            theorem=theorem,
            representation=representation.model_copy(update={"theorem_id": f"thm:{'0' * 64}"}),
        )
    with pytest.raises(LF022ExecutionError, match="named theorem"):
        make_lf022_named_signature(
            theorem=theorem,
            representation=representation.model_copy(update={"signature_pp": None}),
        )
    failed_status = dict(representation.view_status)
    failed_status["signature_pp"] = ViewStatus.FAILED
    with pytest.raises(LF022ExecutionError, match="named theorem"):
        make_lf022_named_signature(
            theorem=theorem,
            representation=representation.model_copy(update={"view_status": failed_status}),
        )


def test_source_eligibility_audit_uses_prebuilt_exact_input_indexes(
    tmp_path: Path,
) -> None:
    admission, _ = _fixture(
        tmp_path,
        profile="scientific_production_scaffold",
    )
    verified = verify_lf022_execution_admission(
        repo_root=tmp_path,
        admission=admission,
    )
    inputs = load_lf022_execution_task_inputs(
        repo_root=tmp_path,
        verified=verified,
    )
    indexed_only = replace(
        inputs,
        source_records=(),
        theorems=(),
        representations=(),
        contexts=(),
        clearances=(),
    )

    assert (
        audit_lf022_g_open_source_eligibility(
            repo_root=tmp_path,
            admission=admission,
            verified=verified,
            inputs=indexed_only,
        )
        == 1
    )


@pytest.mark.parametrize(
    "model_id",
    (
        "Qwen/Qwen3.5-397B-A17B",
        "zai-org/GLM-5.2",
    ),
)
def test_unqualified_batch_route_is_frozen_as_one_item_only(
    tmp_path: Path,
    model_id: str,
) -> None:
    admission, task = _fixture(tmp_path, model_id=model_id)
    frozen = freeze_lf022_public_batch(
        repo_root=tmp_path,
        request_binding=_batch_request_binding(
            tmp_path,
            admission=admission,
            task=task,
        ),
    )
    route = frozen.manifest.routes[0]
    assert route.execution_scope == "one_item_proposer_qualification_only"
    assert route.qualification_state == "pending_one_item_mechanical_qualification"
    assert len(route.tasks) == 1


def test_qwen_batch_request_rejects_more_than_one_task(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    with pytest.raises(ValueError, match="exactly one allocation task"):
        LF022BatchRouteFreezeRequest(
            proposer_family_id="qwen3",
            public_pool_audit_id=admission.public_pool_audit_id,
            allocation_plan_id=admission.allocation_plan_id,
            execution_artifacts=admission.artifacts,
            route=admission.route,
            retry_policy=admission.retry_policy,
            code_tree_hash=admission.code_tree_hash,
            allocation_task_ids=tuple(
                sorted(
                    (
                        task.allocation_task.task_id,
                        f"lf022_production_task:{'2' * 64}",
                    )
                )
            ),
        )


def test_batch_freeze_rejects_tampered_plan_binding(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    request_binding = _batch_request_binding(
        tmp_path,
        admission=admission,
        task=task,
    )
    (tmp_path / admission.artifacts.allocation_plan.path).write_text("{}\n", encoding="utf-8")
    with pytest.raises(LF022BatchError, match="SHA-256 mismatch"):
        freeze_lf022_public_batch(
            repo_root=tmp_path,
            request_binding=request_binding,
        )


def test_batch_freeze_rejects_noncanonical_prompt_before_execution(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    malicious_prompt = _write_bytes(
        tmp_path,
        "prompts/proposers/unreviewed_private_prompt.txt",
        b"PRIVATE_SENSITIVE_PAYLOAD\n",
    )
    artifacts = admission.artifacts.model_copy(update={"prompt_template": malicious_prompt})
    unreviewed = make_lf022_g_open_execution_admission(
        public_pool_audit_id=admission.public_pool_audit_id,
        allocation_plan_id=admission.allocation_plan_id,
        artifacts=artifacts,
        route=admission.route,
        retry_policy=admission.retry_policy,
        code_tree_hash=admission.code_tree_hash,
    )
    with pytest.raises(LF022BatchError, match="exact reviewed proposer prompt"):
        freeze_lf022_public_batch(
            repo_root=tmp_path,
            request_binding=_batch_request_binding(
                tmp_path,
                admission=unreviewed,
                task=task,
            ),
        )


def test_cached_verification_cannot_authorize_a_different_prompt(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path)
    verified = verify_lf022_execution_admission(
        repo_root=tmp_path,
        admission=admission,
    )
    task_inputs = load_lf022_execution_task_inputs(
        repo_root=tmp_path,
        verified=verified,
    )
    malicious_prompt = _write_bytes(
        tmp_path,
        "prompts/proposers/unreviewed_cached_private_prompt.txt",
        b"PRIVATE_SENSITIVE_PAYLOAD\n",
    )
    unreviewed = make_lf022_g_open_execution_admission(
        public_pool_audit_id=admission.public_pool_audit_id,
        allocation_plan_id=admission.allocation_plan_id,
        artifacts=admission.artifacts.model_copy(update={"prompt_template": malicious_prompt}),
        route=admission.route,
        retry_policy=admission.retry_policy,
        code_tree_hash=admission.code_tree_hash,
    )
    unreviewed_task = make_lf022_g_open_execution_task(
        admission=unreviewed,
        allocation_task=task.allocation_task,
        source=task.source,
    )
    with pytest.raises(LF022ExecutorError, match="different admission"):
        prepare_lf022_g_open_execution(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=unreviewed,
            task=unreviewed_task,
            verified_admission=verified,
            verified_task_inputs=task_inputs,
            observed_code_tree_hash=unreviewed.code_tree_hash,
        )

    forged_cache = replace(verified, admission_id=unreviewed.admission_id)
    with pytest.raises(LF022ExecutorError, match="exact reviewed proposer prompt"):
        prepare_lf022_g_open_execution(
            repo_root=tmp_path,
            output_root=tmp_path / "data/out",
            admission=unreviewed,
            task=unreviewed_task,
            verified_admission=forged_cache,
            verified_task_inputs=task_inputs,
            observed_code_tree_hash=unreviewed.code_tree_hash,
        )


def test_batch_request_rejects_noncanonical_executor_output_root(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    with pytest.raises(ValueError, match="canonical global LF-022 executor root"):
        _batch_request_binding(
            tmp_path,
            admission=admission,
            task=task,
            executor_output_root="data/a_second_executor_root",
        )


def test_single_task_qualification_rejects_alternate_output_root(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    canonical_transport = FakeTransport([_success_response(admission.route.model_id)])
    first = execute_lf022_g_open_task(
        repo_root=tmp_path,
        output_root=tmp_path / "data/lf022_execution",
        admission=admission,
        task=task,
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=canonical_transport,
        clock=lambda: NOW,
    )
    assert canonical_transport.calls == 1
    assert first.terminal is not None

    alternate_transport = FakeTransport([])
    with pytest.raises(LF022ExecutorError, match="canonical global LF-022 executor root"):
        execute_lf022_g_open_task(
            repo_root=tmp_path,
            output_root=tmp_path / "data/alternate_qwen_root",
            admission=admission,
            task=task,
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=alternate_transport,
            clock=lambda: NOW,
        )
    assert alternate_transport.calls == 0


def test_legacy_cli_rejects_alternate_qualification_output_root(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    admission_path = tmp_path / "admission.json"
    task_path = tmp_path / "task.json"
    admission_path.write_bytes(canonical_json_bytes(admission.model_dump(mode="json")))
    task_path.write_bytes(canonical_json_bytes(task.model_dump(mode="json")))
    result = CliRunner().invoke(
        app,
        [
            "run-lf022-public-provisional",
            "--root",
            str(tmp_path),
            "--admission",
            str(admission_path),
            "--task",
            str(task_path),
            "--output-root",
            str(tmp_path / "data/alternate_qwen_cli_root"),
        ],
    )
    assert result.exit_code == 2
    assert "canonical global LF-022 executor root" in result.output


def test_qualification_task_replays_globally_across_batch_directories(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    first = freeze_lf022_public_batch(
        repo_root=tmp_path,
        request_binding=_batch_request_binding(
            tmp_path,
            admission=admission,
            task=task,
        ),
    )
    second = freeze_lf022_public_batch(
        repo_root=tmp_path,
        request_binding=_batch_request_binding(
            tmp_path,
            admission=admission,
            task=task,
            batch_directory="data/lf022_batch_second",
            request_path="data/lf022_batch_request_second.json",
        ),
    )
    first_binding = LF022ArtifactBinding(
        path=str(first.manifest_path.relative_to(tmp_path)),
        sha256=hash_file(first.manifest_path),
    )
    second_binding = LF022ArtifactBinding(
        path=str(second.manifest_path.relative_to(tmp_path)),
        sha256=hash_file(second.manifest_path),
    )

    live_transport = FakeTransport([_success_response(admission.route.model_id)])
    first_run = run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=first_binding,
        policy=LF022BatchRunPolicy(),
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=live_transport,
    )
    assert first_run.report.network_calls_this_run == 1

    replay_transport = FakeTransport([])
    second_run = run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=second_binding,
        policy=LF022BatchRunPolicy(),
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=replay_transport,
    )
    assert replay_transport.calls == 0
    assert second_run.report.replayed_terminal_count == 1
    assert second_run.report.network_calls_this_run == 0


def test_batch_run_requires_frozen_request_replay(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    frozen = freeze_lf022_public_batch(
        repo_root=tmp_path,
        request_binding=_batch_request_binding(
            tmp_path,
            admission=admission,
            task=task,
        ),
    )
    (tmp_path / frozen.manifest.freeze_request.path).unlink()
    with pytest.raises(LF022BatchError, match="missing or unsafe"):
        run_lf022_public_batch(
            repo_root=tmp_path,
            manifest_binding=LF022ArtifactBinding(
                path=str(frozen.manifest_path.relative_to(tmp_path)),
                sha256=hash_file(frozen.manifest_path),
            ),
            policy=LF022BatchRunPolicy(),
        )


def test_batch_run_rejects_forged_copied_route_metadata(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    frozen = freeze_lf022_public_batch(
        repo_root=tmp_path,
        request_binding=_batch_request_binding(
            tmp_path,
            admission=admission,
            task=task,
        ),
    )
    payload = frozen.manifest.model_dump(mode="json")
    routes = payload["routes"]
    assert isinstance(routes, list)
    route = routes[0]
    assert isinstance(route, dict)
    route["model_id"] = "fabricated/model"
    payload_without_id = {key: value for key, value in payload.items() if key != "batch_id"}
    payload["batch_id"] = make_id("lf022_public_batch", payload_without_id)
    forged = _write_json(
        tmp_path,
        "data/forged_batch_manifest.json",
        payload,
    )
    with pytest.raises(LF022BatchError, match="frozen admission"):
        run_lf022_public_batch(
            repo_root=tmp_path,
            manifest_binding=forged,
            policy=LF022BatchRunPolicy(),
        )


def test_live_batch_is_explicit_and_resumes_without_a_second_call(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    frozen = freeze_lf022_public_batch(
        repo_root=tmp_path,
        request_binding=_batch_request_binding(
            tmp_path,
            admission=admission,
            task=task,
        ),
    )
    manifest_binding = LF022ArtifactBinding(
        path=str(frozen.manifest_path.relative_to(tmp_path)),
        sha256=hash_file(frozen.manifest_path),
    )
    with pytest.raises(LF022BatchError, match="requires runtime credentials"):
        run_lf022_public_batch(
            repo_root=tmp_path,
            manifest_binding=manifest_binding,
            policy=LF022BatchRunPolicy(),
            execute_public_provisional=True,
        )

    live_transport = FakeTransport([_success_response(admission.route.model_id)])
    live = run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=manifest_binding,
        policy=LF022BatchRunPolicy(max_concurrency=1),
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=live_transport,
    )
    assert live_transport.calls == 1
    assert live.report.new_terminal_count == 1
    assert live.report.successful_terminal_count == 1
    assert live.report.failed_terminal_count == 0
    assert live.report.network_calls_this_run == 1
    assert live.report.terminal_status_counts == {"provisional_variants_created": 1}

    replay_transport = FakeTransport([])
    replay = run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=manifest_binding,
        policy=LF022BatchRunPolicy(max_concurrency=1),
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=replay_transport,
    )
    assert replay_transport.calls == 0
    assert replay.report.replayed_terminal_count == 1
    assert replay.report.successful_terminal_count == 1
    assert replay.report.failed_terminal_count == 0
    assert replay.report.network_calls_this_run == 0
    events = sorted((tmp_path / frozen.manifest.journal_directory).glob("*/*.json"))
    assert len(events) == 2


def test_live_batch_rejects_more_than_one_worker_before_loading_manifest(
    tmp_path: Path,
) -> None:
    with pytest.raises(LF022BatchError, match="live LF-022 execution requires max_concurrency=1"):
        run_lf022_public_batch(
            repo_root=tmp_path,
            manifest_binding=LF022ArtifactBinding(
                path="does-not-need-to-exist.json",
                sha256="a" * 64,
            ),
            policy=LF022BatchRunPolicy(max_concurrency=2),
            execute_public_provisional=True,
            credentials=_credentials(),
            transport=FakeTransport([]),
        )


def test_batch_report_distinguishes_failed_terminal_from_executor_error(
    tmp_path: Path,
) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    frozen = freeze_lf022_public_batch(
        repo_root=tmp_path,
        request_binding=_batch_request_binding(
            tmp_path,
            admission=admission,
            task=task,
        ),
    )
    manifest_binding = LF022ArtifactBinding(
        path=frozen.manifest_path.relative_to(tmp_path).as_posix(),
        sha256=hash_file(frozen.manifest_path),
    )
    bad_response = RCPWireResponse(
        status_code=200,
        headers={},
        body=canonical_json_bytes(
            {
                "id": "fixture-bad-batch",
                "model": admission.route.model_id,
                "choices": [{"message": {"content": "not task json"}}],
            }
        ),
    )
    result = run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=manifest_binding,
        policy=LF022BatchRunPolicy(),
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([bad_response]),
    )
    assert result.report.successful_terminal_count == 0
    assert result.report.failed_terminal_count == 1
    assert result.report.error_count == 0
    assert result.report.terminal_status_counts == {"proposer_parse_failed": 1}


def test_rate_limiter_applies_to_every_transport_start() -> None:
    clock_values = iter((0.0, 0.0, 2.0, 2.0))
    sleeps: list[float] = []
    underlying = FakeTransport(
        [
            _success_response("moonshotai/Kimi-K2.7-Code"),
            _success_response("moonshotai/Kimi-K2.7-Code"),
        ]
    )
    limited = RateLimitedRCPTransport(
        underlying=underlying,
        minimum_interval_seconds=2.0,
        monotonic=lambda: next(clock_values),
        sleeper=sleeps.append,
    )
    payload: Mapping[str, object] = {"model": "moonshotai/Kimi-K2.7-Code"}
    url = "https://inference.rcp.epfl.ch/v1/chat/completions"
    limited.post_json(url=url, api_key="secret", payload=payload, timeout_seconds=1)
    limited.post_json(url=url, api_key="secret", payload=payload, timeout_seconds=1)
    assert underlying.calls == 2
    assert sleeps == [2.0]


def test_rate_limiter_serializes_complete_in_flight_requests() -> None:
    class ConcurrencyObservingTransport:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.active = 0
            self.maximum_active = 0

        def post_json(
            self,
            *,
            url: str,
            api_key: str,
            payload: Mapping[str, object],
            timeout_seconds: int,
        ) -> RCPWireResponse:
            del url, api_key, payload, timeout_seconds
            with self._lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            time.sleep(0.02)
            with self._lock:
                self.active -= 1
            return _success_response("moonshotai/Kimi-K2.7-Code")

    underlying = ConcurrencyObservingTransport()
    limited = RateLimitedRCPTransport(
        underlying=underlying,
        minimum_interval_seconds=0.0,
        maximum_in_flight_requests=1,
    )
    payload: Mapping[str, object] = {"model": "moonshotai/Kimi-K2.7-Code"}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(
                limited.post_json,
                url="https://inference.rcp.epfl.ch/v1/chat/completions",
                api_key="secret",
                payload=payload,
                timeout_seconds=1,
            )
            for _ in range(4)
        ]
        assert all(future.result().status_code == 200 for future in futures)
    assert underlying.maximum_active == 1


@pytest.mark.parametrize("maximum", (0, 9))
def test_rate_limiter_rejects_invalid_in_flight_window(maximum: int) -> None:
    with pytest.raises(ValueError, match="maximum_in_flight_requests"):
        RateLimitedRCPTransport(
            underlying=FakeTransport([]),
            minimum_interval_seconds=0.0,
            maximum_in_flight_requests=maximum,
        )


def test_batch_request_rejects_duplicate_route_family(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path)
    kimi = LF022BatchRouteFreezeRequest(
        proposer_family_id="moonshot_kimi_k2",
        public_pool_audit_id=admission.public_pool_audit_id,
        allocation_plan_id=admission.allocation_plan_id,
        execution_artifacts=admission.artifacts,
        route=admission.route,
        retry_policy=admission.retry_policy,
        code_tree_hash=admission.code_tree_hash,
        allocation_task_ids=(task.allocation_task.task_id,),
    )
    payload = {
        "schema_version": 1,
        "request_id": f"lf022_batch_request:{'c' * 64}",
        "batch_directory": "data/batch",
        "executor_output_root": "data/lf022_execution",
        "routes": [kimi, kimi],
    }
    with pytest.raises(ValueError, match="must be unique"):
        LF022BatchFreezeRequest.model_validate(payload)


def test_batch_cli_freezes_then_preflights_without_credentials(tmp_path: Path) -> None:
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    request_binding = _batch_request_binding(
        tmp_path,
        admission=admission,
        task=task,
    )
    runner = CliRunner()
    frozen = runner.invoke(
        app,
        [
            "freeze-lf022-public-batch",
            "--root",
            str(tmp_path),
            "--request",
            str(tmp_path / request_binding.path),
        ],
    )
    assert frozen.exit_code == 0, frozen.output
    assert "tasks=1" in frozen.output
    assert "network_calls_this_run=0" in frozen.output

    executed = runner.invoke(
        app,
        [
            "run-lf022-public-batch",
            "--root",
            str(tmp_path),
            "--manifest",
            str(tmp_path / "data/lf022_batch/batch_manifest.json"),
            "--max-concurrency",
            "2",
        ],
        env={"RCP_BASE_URL": "", "RCP_API_KEY": ""},
    )
    assert executed.exit_code == 0, executed.output
    assert "mode=offline" in executed.output
    assert "preflight_only=1" in executed.output
    assert "network_calls_this_run=0" in executed.output
