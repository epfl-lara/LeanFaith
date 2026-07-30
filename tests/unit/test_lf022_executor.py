"""Offline-only tests for the public LF-022 proposer executor."""

from __future__ import annotations

import datetime
import fcntl
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.code_bundle import freeze_code_bundle
from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import DenylistIndex, FrozenBenchmark, FrozenRegistry
from leanfaith.generation.lf022_execution import (
    LF022ExecutionArtifacts,
    LF022ExecutionError,
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    LF022RCPDecodingContract,
    LF022RCPRetryPolicy,
    LF022RCPRouteBinding,
    make_lf022_g_open_execution_admission,
    make_lf022_g_open_execution_task,
)
from leanfaith.generation.lf022_executor import (
    LF022ExecutorError,
    LF022TaskLockedError,
    RCPRuntimeCredentials,
    execute_lf022_g_open_task,
    prepare_lf022_g_open_execution,
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
) -> LF022ProductionPlanManifest:
    payload: dict[str, object] = {
        "schema_version": 2,
        "profile": "diagnostic_scaffold",
        "scientific_status": "diagnostic_only",
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
        "profile": "diagnostic_scaffold",
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
        signature_pp=statement,
        signature_explicit="theorem public_source (n : Nat) : Eq Nat n n",
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
        source_statement=statement,
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
