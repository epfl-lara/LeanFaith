"""Scalable LF-021 postprocess-v3 contracts and collection-v2 compatibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.paths import find_repo_root
from leanfaith.generation import research_collection_v2 as collection_v2
from leanfaith.generation import research_postprocess as postprocess_v1
from leanfaith.generation.research_postprocess_v3 import (
    ResearchPostprocessV3Error,
    ResearchPostprocessV3FamilyReport,
    ResearchPostprocessV3InputBinding,
    validate_collection_v2_denominator,
)

ROOT = find_repo_root(Path(__file__).parent)
CONFIG = ROOT / "configs/generation/local_research_collection_v2.yaml"
SHA = "a" * 64


def _manifest_for_plan(
    plan: collection_v2.ResearchCollectionPlanV2,
) -> collection_v2.ResearchCollectionManifestV2:
    terminal_artifacts = {
        f"data/raw/tests/v2/terminals/{index:03d}.json": SHA
        for index in range(plan.expected_candidate_count)
    }
    payload: dict[str, object] = {
        "schema_version": 2,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "shared_execution_record_schema": plan.shared_execution_record_schema,
        "actual_collection_performed": True,
        "problem_count": plan.problem_count,
        "family_count": plan.family_count,
        "seed_count_by_family": plan.seed_count_by_family,
        "expected_candidate_count": plan.expected_candidate_count,
        "terminal_candidate_count": plan.expected_candidate_count,
        "status_counts": {"orchestration_failed": plan.expected_candidate_count},
        "successful_family_count": 0,
        "terminal_artifact_hashes": terminal_artifacts,
        "family_session_artifact_hashes": {},
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    manifest_id = "research_collection_manifest_v2:" + hash_canonical(
        {"schema": "lf021_research_collection_manifest_v2", **payload}
    )
    return collection_v2.ResearchCollectionManifestV2.model_validate(
        {"manifest_id": manifest_id, **payload}
    )


def _artifact() -> postprocess_v1.PostprocessArtifactBinding:
    return postprocess_v1.PostprocessArtifactBinding(
        artifact="tests/fixtures/fake.json",
        sha256=SHA,
    )


def test_v3_accepts_actual_collection_v2_40_by_3_by_1_schema() -> None:
    loaded = collection_v2.load_research_collection_v2(CONFIG, repo_root=ROOT)
    plan = loaded.plan
    manifest = _manifest_for_plan(plan)

    validate_collection_v2_denominator(plan, manifest)

    assert isinstance(plan, collection_v2.ResearchCollectionPlanV2)
    assert isinstance(manifest, collection_v2.ResearchCollectionManifestV2)
    assert plan.problem_count == 40
    assert plan.expected_candidate_count == 120


def test_v3_rejects_collection_v2_cardinality_mismatch() -> None:
    loaded = collection_v2.load_research_collection_v2(CONFIG, repo_root=ROOT)
    manifest = _manifest_for_plan(loaded.plan).model_copy(update={"terminal_candidate_count": 119})

    with pytest.raises(
        ResearchPostprocessV3Error,
        match="denominator",
    ):
        validate_collection_v2_denominator(loaded.plan, manifest)


def test_v3_input_binding_scales_to_120_invocations() -> None:
    plan = collection_v2.load_research_collection_v2(CONFIG, repo_root=ROOT).plan
    invocation_ids = tuple(item.invocation_id for item in plan.invocations)
    family_ids = tuple(item.family_id for item in plan.family_bindings)
    terminal_artifacts = {
        f"data/raw/tests/v2/terminals/{index:03d}.json": SHA
        for index in range(plan.expected_candidate_count)
    }
    binding = ResearchPostprocessV3InputBinding(
        collection_config=_artifact(),
        collection_plan=_artifact(),
        collection_manifest=_artifact(),
        collection_plan_id=plan.plan_id,
        collection_plan_hash=plan.plan_hash,
        collection_manifest_id="research_collection_manifest_v2:" + "b" * 64,
        collection_terminal_artifacts=terminal_artifacts,
        collection_family_session_artifacts={},
        raw_collection_artifacts_by_invocation={
            invocation_id: {} for invocation_id in invocation_ids
        },
        problem_pool_manifest=_artifact(),
        problem_pool_records=_artifact(),
        context=_artifact(),
        import_header=_artifact(),
        source_matrix=_artifact(),
        reference_theorems=_artifact(),
        reference_representations=_artifact(),
        active_registry_artifacts={"active_registry": _artifact()},
        active_registry_content_hash=SHA,
        collector_implementation=_artifact(),
        primary_parser_implementations={family_id: _artifact() for family_id in family_ids},
        recovery_implementation=_artifact(),
        implementation=_artifact(),
        problem_count=plan.problem_count,
        seed_count_by_family=plan.seed_count_by_family,
        expected_invocations=plan.expected_candidate_count,
        problem_record_ids=plan.problem_record_ids,
        invocation_ids=invocation_ids,
        family_ids=family_ids,
    )

    assert binding.problem_count == 40
    assert binding.expected_invocations == 120
    assert len(binding.raw_collection_artifacts_by_invocation) == 120


def test_v3_family_report_uses_dynamic_problem_seed_denominator() -> None:
    payload: dict[str, object] = {
        "schema_version": 3,
        "input_binding_hash": SHA,
        "family_id": "family",
        "problem_count": 40,
        "seed_count": 1,
        "expected_invocations": 40,
        "terminal_invocations": 40,
        "status_counts": {"parse_failed": 40},
        "recovery_status_counts": {"failed": 40},
        "collection_raw_count": 40,
        "parser_success_count": 0,
        "admitted_unresolved_count": 0,
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    report_id = "research_postprocess_v3_family:" + hash_canonical(
        {"schema": "lf021_research_postprocess_family_v3", **payload}
    )
    report = ResearchPostprocessV3FamilyReport.model_validate({"report_id": report_id, **payload})

    assert report.expected_invocations == 40
    with pytest.raises(ValueError, match="denominator"):
        ResearchPostprocessV3FamilyReport.model_validate(
            {**report.model_dump(mode="json"), "terminal_invocations": 39}
        )
