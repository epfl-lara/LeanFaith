"""Offline derivation of one repr_v3 diagnostic source from a parent pool."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.cli.lf022_batch import create_public_batch_request, freeze_public_batch
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.datasets.denylist import FrozenRegistry
from leanfaith.generation.lf022_admission_freeze import (
    freeze_lf022_diagnostic_execution_admission,
)
from leanfaith.generation.lf022_diagnostic_subpool import (
    LF022DiagnosticSubpoolError,
    derive_lf022_diagnostic_subpool,
    verify_lf022_diagnostic_subpool,
)
from leanfaith.generation.lf022_execution import LF022ExecutionError, _load_strict_json
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.schemas.ids import make_id
from tests.unit.test_lf022_executor import _fixture


@pytest.mark.parametrize(
    ("model_id", "family_id"),
    (
        ("Qwen/Qwen3.5-397B-A17B", "qwen3"),
        ("zai-org/GLM-5.2", "glm5"),
    ),
)
def test_derivation_exact_replays_and_freezes_one_offline_batch(
    tmp_path: Path,
    model_id: str,
    family_id: str,
) -> None:
    parent_admission, _ = _fixture(tmp_path, model_id=model_id)
    output = tmp_path / f"artifacts/derived_{family_id}"
    first = derive_lf022_diagnostic_subpool(
        repo_root=tmp_path,
        parent_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
        proposer_family_id=family_id,  # type: ignore[arg-type]
        output_directory=output,
    )
    second = derive_lf022_diagnostic_subpool(
        repo_root=tmp_path,
        parent_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
        proposer_family_id=family_id,  # type: ignore[arg-type]
        output_directory=output,
    )

    audit = first.materialized.audit
    plan = first.materialized.plan
    assert second == first
    assert audit.schema_version == 2
    assert audit.selected_count == 1
    assert audit.outputs.parent_pool_derivation == first.derivation_binding
    assert first.derivation.parent_pool_audit_id != audit.audit_id
    assert first.derivation.proposer_family_id == family_id
    assert first.derivation.outputs_provisional_only is True
    assert first.derivation.resolution_outcome == "unresolved"
    assert first.derivation.semantic_labels_created is False
    assert first.derivation.training_eligible is False
    assert first.derivation.evaluation_eligible is False
    assert len(plan.tasks) == 2
    assert {task.distribution for task in plan.tasks} == {"G_sci", "G_open"}
    assert {task.proposer_family_id for task in plan.tasks} == {family_id}
    assert all(not task.executable for task in plan.tasks)

    verified = verify_lf022_diagnostic_subpool(
        repo_root=tmp_path,
        audit=audit,
        expected_proposer_family_id=family_id,  # type: ignore[arg-type]
        expected_code_tree_hash=first.derivation.attesting_code_tree_hash,
    )
    assert verified.representation.normalization_version == "repr_v3"
    assert verified.clearance.clear

    frozen = freeze_lf022_diagnostic_execution_admission(
        repo_root=tmp_path,
        public_pool_audit_path=tmp_path / first.materialized.audit_binding.path,
        proposer_family_id=family_id,  # type: ignore[arg-type]
        code_bundle_path=tmp_path / parent_admission.artifacts.code_bundle.path,
        provider_catalog_raw_path=(tmp_path / parent_admission.artifacts.provider_catalog_raw.path),
        output_path=output / "execution_admission.json",
    )
    g_open_task = next(task for task in plan.tasks if task.distribution == "G_open")
    request = create_public_batch_request(
        repo_root=tmp_path,
        admission_path=frozen.admission_path,
        allocation_task_ids=(g_open_task.task_id,),
        output_path=output / "batch_request.json",
        batch_directory=f"artifacts/derived_{family_id}/batch",
        executor_output_root="data/lf022_execution",
    )
    batch = freeze_public_batch(
        repo_root=tmp_path,
        request_path=request.request_path,
    )
    assert batch.manifest.total_task_count == 1
    assert batch.manifest.public_sources_only is True
    assert batch.manifest.private_source_content_forbidden is True
    assert batch.manifest.outputs_provisional_only is True
    assert batch.manifest.semantic_labels_created is False
    assert batch.manifest.training_eligible is False
    assert batch.manifest.evaluation_eligible is False
    assert batch.manifest.gate_credit_claimed is False


def test_derivation_rejects_parent_drift_and_cross_family_replay(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    derived = derive_lf022_diagnostic_subpool(
        repo_root=tmp_path,
        parent_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
        proposer_family_id="qwen3",
        output_directory=tmp_path / "artifacts/derived_qwen",
    )

    with pytest.raises(
        LF022DiagnosticSubpoolError,
        match="different proposer family",
    ):
        verify_lf022_diagnostic_subpool(
            repo_root=tmp_path,
            audit=derived.materialized.audit,
            expected_proposer_family_id="glm5",
        )

    parent_source = tmp_path / "artifacts/source_pool.jsonl"
    parent_source.write_bytes(parent_source.read_bytes() + b"\n")
    with pytest.raises(
        LF022DiagnosticSubpoolError,
        match="parent source pool hash differs",
    ):
        verify_lf022_diagnostic_subpool(
            repo_root=tmp_path,
            audit=derived.materialized.audit,
            expected_proposer_family_id="qwen3",
        )


def test_derivation_rejects_recontent_addressed_wrong_task_lineage(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    derived = derive_lf022_diagnostic_subpool(
        repo_root=tmp_path,
        parent_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
        proposer_family_id="qwen3",
        output_directory=tmp_path / "artifacts/derived_qwen",
    )
    plan = derived.materialized.plan
    task_payload = plan.tasks[0].model_dump(mode="json", exclude={"task_id"})
    task_payload["theorem_id"] = make_id("thm", {"wrong_lineage": True})
    wrong_task = type(plan.tasks[0]).model_validate(
        {
            **task_payload,
            "task_id": make_id("lf022_production_task", task_payload),
        }
    )
    plan_payload = plan.model_dump(mode="json", exclude={"manifest_id"})
    plan_payload["tasks"][0] = wrong_task.model_dump(mode="json")  # type: ignore[index]
    wrong_plan = type(plan).model_validate(
        {
            **plan_payload,
            "manifest_id": make_id("lf022_production_plan", plan_payload),
        }
    )
    plan_path = tmp_path / derived.materialized.audit.outputs.production_plan.path
    plan_path.write_bytes(canonical_json_bytes(wrong_plan.model_dump(mode="json")))
    plan_binding = LF022ArtifactBinding(
        path=derived.materialized.audit.outputs.production_plan.path,
        sha256=hash_file(plan_path),
    )
    outputs = derived.materialized.audit.outputs.model_copy(
        update={"production_plan": plan_binding}
    )
    audit_payload = derived.materialized.audit.model_dump(
        mode="json",
        exclude={"audit_id"},
    )
    audit_payload["outputs"] = outputs.model_dump(mode="json")
    wrong_audit = type(derived.materialized.audit).model_validate(
        {
            **audit_payload,
            "audit_id": make_id("lf022_public_pool_audit", audit_payload),
        }
    )

    with pytest.raises(
        LF022DiagnosticSubpoolError,
        match="admission/plan does not reconcile",
    ):
        verify_lf022_diagnostic_subpool(
            repo_root=tmp_path,
            audit=wrong_audit,
            expected_proposer_family_id="qwen3",
        )


def test_derivation_requires_clean_code_tree(tmp_path: Path) -> None:
    _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    (tmp_path / "fixture_code.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(
        LF022DiagnosticSubpoolError,
        match="clean, hashable code tree",
    ):
        derive_lf022_diagnostic_subpool(
            repo_root=tmp_path,
            parent_pool_audit_path=tmp_path / "artifacts/public_pool_audit.json",
            proposer_family_id="qwen3",
            output_directory=tmp_path / "artifacts/rejected",
        )


def test_execution_json_loader_accepts_one_canonical_newline_only(
    tmp_path: Path,
) -> None:
    registry = FrozenRegistry(
        frozen_at="2026-07-30T00:00:00Z",
        benchmarks=(),
        representation_signatures_appended=True,
    )
    payload = canonical_json_bytes(registry.model_dump(mode="json"))
    path = tmp_path / "registry.json"
    path.write_bytes(payload + b"\n")
    assert _load_strict_json(path, FrozenRegistry, label="registry") == registry

    path.write_bytes(payload + b"\n ")
    with pytest.raises(LF022ExecutionError, match="not canonical JSON"):
        _load_strict_json(path, FrozenRegistry, label="registry")
