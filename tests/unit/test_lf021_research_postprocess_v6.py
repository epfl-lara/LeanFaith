"""Collector-v5/postprocess-v6 binding, isolation, and replay tests."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.paths import find_repo_root
from leanfaith.generation import research_collection_v5 as collection_v5
from leanfaith.generation import research_postprocess as postprocess_v1
from leanfaith.generation import research_postprocess_v3 as postprocess_v3
from leanfaith.generation.research_postprocess_v6 import (
    ResearchPostprocessV6Error,
    ResearchPostprocessV6InputBinding,
    _require_collector_v5_document,
    _write_terminals_and_reports,
    validate_collection_v5_denominator,
    verify_research_postprocess_v6,
)

ROOT = find_repo_root(Path(__file__).parent)
CONFIG = ROOT / "configs/generation/local_research_collection_algebra_s2_v5.json"
SHA = "a" * 64
CREATED = datetime.datetime(2026, 7, 24, 8, 0, tzinfo=datetime.UTC)


def _artifact(name: str = "fake.json") -> postprocess_v1.PostprocessArtifactBinding:
    return postprocess_v1.PostprocessArtifactBinding(
        artifact=f"tests/fixtures/{name}",
        sha256=SHA,
    )


def _manifest(
    plan: collection_v5.ResearchCollectionPlanV5,
) -> collection_v5.ResearchCollectionManifestV5:
    terminals = {
        f"data/raw/tests/v5/terminals/{index:03d}.json": SHA
        for index in range(plan.expected_candidate_count)
    }
    payload: dict[str, object] = {
        "schema_version": 5,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "tranche_id": plan.tranche_id,
        "pool_dialect": plan.pool_dialect,
        "overlap_schema": plan.overlap_schema,
        "expansion_decision_id": plan.expansion_decision_id,
        "expansion_decision_sha256": plan.expansion_decision_sha256,
        "expansion_policy_id": plan.expansion_policy_id,
        "expansion_policy_sha256": plan.expansion_policy_sha256,
        "shared_execution_record_schema": plan.shared_execution_record_schema,
        "actual_collection_performed": True,
        "problem_count": plan.problem_count,
        "family_count": plan.family_count,
        "seed_count_by_family": plan.seed_count_by_family,
        "expected_candidate_count": plan.expected_candidate_count,
        "terminal_candidate_count": plan.expected_candidate_count,
        "status_counts": {"orchestration_failed": plan.expected_candidate_count},
        "successful_family_count": 0,
        "terminal_artifact_hashes": terminals,
        "family_session_artifact_hashes": {},
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    manifest_id = "research_collection_manifest_v5:" + hash_canonical(
        {"schema": "lf021_research_collection_manifest_v5", **payload}
    )
    return collection_v5.ResearchCollectionManifestV5.model_validate(
        {"manifest_id": manifest_id, **payload}
    )


def _input_binding(
    *,
    pool_dialect: collection_v5.PoolDialect = "gate3_algebra_operational_v1",
) -> ResearchPostprocessV6InputBinding:
    plan = collection_v5.load_research_collection_v5(CONFIG, repo_root=ROOT).plan
    invocation_ids = tuple(item.invocation_id for item in plan.invocations)
    family_ids = tuple(item.family_id for item in plan.family_bindings)
    pool_source, manifest_kind = {
        "gate3_algebra_operational_v1": (
            "mathlib_gate3_docstrings_operational_v1",
            "lf021_gate3_docstrings_operational_problem_pool_v1",
        ),
        "cross_domain_operational_v1": (
            "mathlib_cross_domain_docstrings_operational_v1",
            "lf021_cross_domain_docstrings_operational_problem_pool_v1",
        ),
    }[pool_dialect]
    return ResearchPostprocessV6InputBinding(
        tranche_id=plan.tranche_id,
        pool_dialect=pool_dialect,
        pool_source=pool_source,
        pool_manifest_artifact_kind=manifest_kind,
        collection_config=_artifact("config.json"),
        collection_plan=_artifact("plan.json"),
        collection_manifest=_artifact("manifest.json"),
        collection_plan_id=plan.plan_id,
        collection_plan_hash=plan.plan_hash,
        collection_manifest_id="research_collection_manifest_v5:" + "b" * 64,
        collection_terminal_artifacts={
            f"data/raw/tests/v5/terminals/{index:03d}.json": SHA
            for index in range(plan.expected_candidate_count)
        },
        collection_family_session_artifacts={},
        raw_collection_artifacts_by_invocation={
            invocation_id: {} for invocation_id in invocation_ids
        },
        problem_pool_manifest=_artifact("pool-manifest.json"),
        problem_pool_records=_artifact("pool-records.jsonl"),
        context=_artifact("context.json"),
        import_header=_artifact("header.lean"),
        source_matrix=_artifact("source-matrix.json"),
        reference_theorems=_artifact("reference-theorems.jsonl"),
        reference_representations=_artifact("reference-representations.jsonl"),
        active_registry_artifacts={"active_registry": _artifact("registry.json")},
        active_registry_content_hash=SHA,
        collector_implementation=postprocess_v1.PostprocessArtifactBinding(
            artifact="src/leanfaith/generation/research_collection_v5.py",
            sha256=SHA,
        ),
        primary_parser_implementations={
            family_id: _artifact(f"{family_id}.py") for family_id in family_ids
        },
        recovery_implementation=_artifact("local_output_recovery.py"),
        shared_processing_implementation=postprocess_v1.PostprocessArtifactBinding(
            artifact="src/leanfaith/generation/research_postprocess_v3.py",
            sha256=SHA,
        ),
        implementation=_artifact("research_postprocess_v6.py"),
        problem_count=plan.problem_count,
        seed_count_by_family=plan.seed_count_by_family,
        expected_invocations=plan.expected_candidate_count,
        problem_record_ids=plan.problem_record_ids,
        invocation_ids=invocation_ids,
        family_ids=family_ids,
    )


def _shared_terminal(
    *,
    binding_hash: str,
    invocation_id: str,
    family_id: str,
    problem_record_id: str,
    seed: int,
    materialization_failed: bool = False,
) -> postprocess_v3.ResearchPostprocessV3Terminal:
    parser_executed = materialization_failed
    payload: dict[str, object] = {
        "schema_version": 3,
        "record_kind": "lf021_research_postprocess_terminal_v3",
        "artifact_class": "research",
        "input_binding_hash": binding_hash,
        "invocation_id": invocation_id,
        "invocation_payload_hash": hash_canonical(
            {
                "invocation_id": invocation_id,
                "family_id": family_id,
                "problem_record_id": problem_record_id,
                "seed": seed,
            }
        ),
        "collection_terminal_id": "research_terminal:" + "3" * 64,
        "collection_terminal_sha256": "4" * 64,
        "family_id": family_id,
        "problem_record_id": problem_record_id,
        "seed": seed,
        "status": ("materialization_failed" if materialization_failed else "collection_not_raw"),
        "terminal_stage": "materialization" if materialization_failed else "collection",
        "record_time_basis": CREATED.isoformat().replace("+00:00", "Z"),
        "primary_parser_id": "fixture_parser_v1",
        "primary_parser_source_sha256": "6" * 64,
        "actual_parser_id": "fixture_parser_v1" if materialization_failed else None,
        "actual_parser_source_sha256": "6" * 64 if materialization_failed else None,
        "primary_failure_code": None,
        "recovery_status": "not_needed" if materialization_failed else "not_attempted",
        "recovery_failure_code": None,
        "parser_executed": parser_executed,
        "lean_validation_executed": False,
        "screening_executed": False,
        "semantic_pool_admitted": False,
        "raw_lineage_hashes": {},
        "output_artifact_hashes": {},
        "materialization_outcome": "failed" if materialization_failed else None,
        "screening_status": None,
        "variant_id": None,
        "candidate_theorem_id": None,
        "representation_id": None,
        "screening_id": None,
        "pair_ids": (),
        "nl_lean_id": None,
        "same_claim": None,
        "relation": None,
        "resolution_outcome": None,
        "quality_tier": None,
        "requires_adjudication": False,
        "decision": None,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
        "failure_code": (
            "fixture_materialization_failed"
            if materialization_failed
            else "collection_orchestration_failed"
        ),
        "failure_detail": "fake replay fixture",
    }
    terminal_id = "research_postprocess_v3_terminal:" + hash_canonical(
        {"schema": "lf021_research_postprocess_terminal_v3", **payload}
    )
    return postprocess_v3.ResearchPostprocessV3Terminal.model_validate(
        {"terminal_id": terminal_id, **payload}
    )


def _fake_loaded(
    tmp_path: Path,
) -> tuple[Any, dict[str, postprocess_v3.ResearchPostprocessV3Terminal]]:
    binding = _input_binding()
    plan = collection_v5.load_research_collection_v5(CONFIG, repo_root=ROOT).plan
    invocations = {item.invocation_id: item for item in plan.invocations}
    first_id = next(iter(invocations))
    shared = {
        invocation_id: _shared_terminal(
            binding_hash=binding.shared_processing_input_binding_hash,
            invocation_id=invocation_id,
            family_id=invocation.family_id,
            problem_record_id=invocation.problem_record_id,
            seed=invocation.seed,
            materialization_failed=invocation_id == first_id,
        )
        for invocation_id, invocation in invocations.items()
    }
    base = SimpleNamespace(
        repo_root=tmp_path,
        output_root=tmp_path / "postprocess_v6",
    )
    loaded = cast(Any, SimpleNamespace(input_binding=binding, base=base))
    return loaded, shared


def test_v6_accepts_exact_collector_v5_denominator() -> None:
    plan = collection_v5.load_research_collection_v5(CONFIG, repo_root=ROOT).plan
    validate_collection_v5_denominator(plan, _manifest(plan))

    assert plan.tranche_id == "algebra_s2"
    assert plan.expected_candidate_count == 120


def test_v6_input_binding_supports_both_closed_pool_dialects() -> None:
    algebra = _input_binding(pool_dialect="gate3_algebra_operational_v1")
    cross_domain = _input_binding(pool_dialect="cross_domain_operational_v1")

    assert algebra.pool_source == "mathlib_gate3_docstrings_operational_v1"
    assert cross_domain.pool_source == "mathlib_cross_domain_docstrings_operational_v1"
    assert algebra.binding_hash != cross_domain.binding_hash


def test_v6_binding_rejects_collector_v4_and_mismatched_pool() -> None:
    document = _input_binding().model_dump(mode="json")
    document["collector_schema_version"] = 4
    with pytest.raises(ValidationError):
        ResearchPostprocessV6InputBinding.model_validate(document)

    document = _input_binding().model_dump(mode="json")
    document["pool_source"] = "mathlib_cross_domain_docstrings_operational_v1"
    with pytest.raises(ValidationError, match="dialect"):
        ResearchPostprocessV6InputBinding.model_validate(document)


@pytest.mark.parametrize("version", [4, 6])
def test_v6_document_probe_rejects_other_collector_versions(
    tmp_path: Path,
    version: int,
) -> None:
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": version,
                "plan_id": f"research_collection_plan_v{version}:{'a' * 64}",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ResearchPostprocessV6Error, match=r"collector-v4|only v5"):
        _require_collector_v5_document(path, kind="plan")


def test_v6_fake_bundle_replays_and_isolates_materialization_failure(
    tmp_path: Path,
) -> None:
    loaded, shared = _fake_loaded(tmp_path)

    first = _write_terminals_and_reports(loaded, shared)
    replayed = verify_research_postprocess_v6(loaded)

    assert replayed == first.manifest
    assert first.manifest.terminal_invocations == 120
    assert first.manifest.status_counts == {
        "collection_not_raw": 119,
        "materialization_failed": 1,
    }
    assert (
        sum(
            report.status_counts.get("materialization_failed", 0) for report in first.family_reports
        )
        == 1
    )
    assert len(first.manifest.terminal_artifacts) == 120
    assert len(first.manifest.family_report_artifacts) == 3
    assert first.manifest.semantic_labels_created is False
    assert first.manifest.supervision_eligible is False
    assert first.manifest.gate_5_closed is False


def test_v6_replay_rejects_tampered_terminal(tmp_path: Path) -> None:
    loaded, shared = _fake_loaded(tmp_path)
    result = _write_terminals_and_reports(loaded, shared)
    relative_path = next(iter(result.manifest.terminal_artifacts))
    path = tmp_path / relative_path
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(ResearchPostprocessV6Error, match="terminal hash mismatch"):
        verify_research_postprocess_v6(loaded)


def test_v6_incomplete_denominator_writes_nothing(tmp_path: Path) -> None:
    loaded, shared = _fake_loaded(tmp_path)
    shared.pop(next(iter(shared)))

    with pytest.raises(ResearchPostprocessV6Error, match="denominator"):
        _write_terminals_and_reports(loaded, shared)

    assert not loaded.base.output_root.exists()
