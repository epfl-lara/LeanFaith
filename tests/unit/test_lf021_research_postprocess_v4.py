"""Collector-v3/postprocess-v4 dynamic and immutable contracts."""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.paths import find_repo_root
from leanfaith.generation import research_collection as collection_v1
from leanfaith.generation import research_collection_v3 as collection_v3
from leanfaith.generation import research_postprocess as postprocess_v1
from leanfaith.generation import research_postprocess_v3 as postprocess_v3
from leanfaith.generation.research_postprocess_v4 import (
    ResearchPostprocessV4Error,
    ResearchPostprocessV4InputBinding,
    ResearchPostprocessV4Terminal,
    _v4_terminal,
    _write_terminals_and_reports,
    validate_collection_v3_denominator,
)

ROOT = find_repo_root(Path(__file__).parent)
CONFIG = ROOT / "configs/generation/local_research_collection_cross_domain_s0_v3.yaml"
SHA = "a" * 64
CREATED = datetime.datetime(2026, 7, 24, tzinfo=datetime.UTC)


def _artifact(
    name: str = "fake.json",
) -> postprocess_v1.PostprocessArtifactBinding:
    return postprocess_v1.PostprocessArtifactBinding(
        artifact=f"tests/fixtures/{name}",
        sha256=SHA,
    )


def _manifest_for_plan(
    plan: collection_v3.ResearchCollectionPlanV3,
) -> collection_v3.ResearchCollectionManifestV3:
    terminal_artifacts = {
        f"data/raw/tests/v3/terminals/{index:03d}.json": SHA
        for index in range(plan.expected_candidate_count)
    }
    payload: dict[str, object] = {
        "schema_version": 3,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "tranche_id": plan.tranche_id,
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
    manifest_id = "research_collection_manifest_v3:" + hash_canonical(
        {"schema": "lf021_research_collection_manifest_v3", **payload}
    )
    return collection_v3.ResearchCollectionManifestV3.model_validate(
        {"manifest_id": manifest_id, **payload}
    )


def _input_binding() -> ResearchPostprocessV4InputBinding:
    plan = collection_v3.load_research_collection_v3(
        CONFIG,
        repo_root=ROOT,
    ).plan
    invocation_ids = tuple(item.invocation_id for item in plan.invocations)
    family_ids = tuple(item.family_id for item in plan.family_bindings)
    terminal_artifacts = {
        f"data/raw/tests/v3/terminals/{index:03d}.json": SHA
        for index in range(plan.expected_candidate_count)
    }
    return ResearchPostprocessV4InputBinding(
        tranche_id=plan.tranche_id,
        pool_dialect="cross_domain_operational_v1",
        pool_source="mathlib_cross_domain_docstrings_operational_v1",
        pool_manifest_artifact_kind=("lf021_cross_domain_docstrings_operational_problem_pool_v1"),
        collection_config=_artifact("config.yaml"),
        collection_plan=_artifact("plan.json"),
        collection_manifest=_artifact("manifest.json"),
        collection_plan_id=plan.plan_id,
        collection_plan_hash=plan.plan_hash,
        collection_manifest_id="research_collection_manifest_v3:" + "b" * 64,
        collection_terminal_artifacts=terminal_artifacts,
        collection_family_session_artifacts={},
        raw_collection_artifacts_by_invocation={
            invocation_id: {} for invocation_id in invocation_ids
        },
        problem_pool_manifest=_artifact("pool-manifest.json"),
        problem_pool_records=_artifact("pool-records.jsonl"),
        context=_artifact("context.json"),
        import_header=_artifact("header.lean"),
        source_matrix=_artifact("source-matrix.yaml"),
        reference_theorems=_artifact("reference-theorems.jsonl"),
        reference_representations=_artifact("reference-representations.jsonl"),
        active_registry_artifacts={"active_registry": _artifact("registry.json")},
        active_registry_content_hash=SHA,
        collector_implementation=postprocess_v1.PostprocessArtifactBinding(
            artifact="src/leanfaith/generation/research_collection_v3.py",
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
        implementation=_artifact("research_postprocess_v4.py"),
        problem_count=plan.problem_count,
        seed_count_by_family=plan.seed_count_by_family,
        expected_invocations=plan.expected_candidate_count,
        problem_record_ids=plan.problem_record_ids,
        invocation_ids=invocation_ids,
        family_ids=family_ids,
    )


def _shared_failure_terminal(
    *,
    binding_hash: str,
    invocation_id: str = "research_invocation:" + "1" * 64,
    family_id: str = "goedel_formalizer_v2_8b",
    problem_record_id: str = "problem:" + "5" * 64,
    seed: int = 30,
) -> postprocess_v3.ResearchPostprocessV3Terminal:
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
        "status": "collection_not_raw",
        "terminal_stage": "collection",
        "record_time_basis": CREATED.isoformat().replace("+00:00", "Z"),
        "primary_parser_id": "goedel_final_fence_parser_v1",
        "primary_parser_source_sha256": "6" * 64,
        "actual_parser_id": None,
        "actual_parser_source_sha256": None,
        "primary_failure_code": None,
        "recovery_status": "not_attempted",
        "recovery_failure_code": None,
        "parser_executed": False,
        "lean_validation_executed": False,
        "screening_executed": False,
        "semantic_pool_admitted": False,
        "raw_lineage_hashes": {},
        "output_artifact_hashes": {},
        "materialization_outcome": None,
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
        "failure_code": "collection_orchestration_failed",
        "failure_detail": "test fixture",
    }
    terminal_id = "research_postprocess_v3_terminal:" + hash_canonical(
        {"schema": "lf021_research_postprocess_terminal_v3", **payload}
    )
    return postprocess_v3.ResearchPostprocessV3Terminal.model_validate(
        {"terminal_id": terminal_id, **payload}
    )


def test_v4_accepts_frozen_collector_v3_20_by_3_by_1_contract() -> None:
    loaded = collection_v3.load_research_collection_v3(CONFIG, repo_root=ROOT)
    manifest = _manifest_for_plan(loaded.plan)

    validate_collection_v3_denominator(loaded.plan, manifest)

    assert loaded.plan.tranche_id == "cross_domain_s0"
    assert loaded.plan.problem_count == 20
    assert loaded.plan.expected_candidate_count == 60


def test_v4_rejects_collector_v3_cardinality_and_tranche_mismatch() -> None:
    plan = collection_v3.load_research_collection_v3(CONFIG, repo_root=ROOT).plan
    manifest = _manifest_for_plan(plan)

    with pytest.raises(ResearchPostprocessV4Error, match="denominator"):
        validate_collection_v3_denominator(
            plan,
            manifest.model_copy(update={"terminal_candidate_count": 59}),
        )
    with pytest.raises(ResearchPostprocessV4Error, match="denominator"):
        validate_collection_v3_denominator(
            plan,
            manifest.model_copy(update={"tranche_id": "different_tranche"}),
        )


def test_v4_binding_is_dynamic_and_projects_to_exact_v3_engine() -> None:
    binding = _input_binding()
    shared = binding.shared_v3_binding()

    assert binding.problem_count == 20
    assert binding.expected_invocations == 60
    assert len(binding.raw_collection_artifacts_by_invocation) == 60
    assert shared.expected_invocations == 60
    assert shared.implementation.artifact == ("src/leanfaith/generation/research_postprocess_v3.py")
    assert binding.shared_processing_input_binding_hash == shared.binding_hash


def test_v4_binding_rejects_pool_dialect_and_denominator_drift() -> None:
    document = _input_binding().model_dump(mode="json")
    document["pool_source"] = "mathlib_gate3_docstrings_operational_v1"
    with pytest.raises(ValidationError, match="pool dialect"):
        ResearchPostprocessV4InputBinding.model_validate(document)

    document = _input_binding().model_dump(mode="json")
    document["expected_invocations"] = 59
    with pytest.raises(ValidationError, match="invocation IDs"):
        ResearchPostprocessV4InputBinding.model_validate(document)


def test_v4_binding_accepts_truthful_algebra_dialect_for_later_tranches() -> None:
    document = _input_binding().model_dump(mode="json")
    document.update(
        {
            "tranche_id": "algebra_s1",
            "pool_dialect": "gate3_algebra_operational_v1",
            "pool_source": "mathlib_gate3_docstrings_operational_v1",
            "pool_manifest_artifact_kind": ("lf021_gate3_docstrings_operational_problem_pool_v1"),
        }
    )

    binding = ResearchPostprocessV4InputBinding.model_validate(document)

    assert binding.tranche_id == "algebra_s1"
    assert binding.pool_dialect == "gate3_algebra_operational_v1"


def test_v4_terminal_projection_is_versioned_and_round_trips_immutably() -> None:
    binding = _input_binding()
    shared = _shared_failure_terminal(binding_hash=binding.shared_processing_input_binding_hash)
    loaded = cast(
        Any,
        SimpleNamespace(input_binding=binding),
    )
    terminal = _v4_terminal(loaded, shared)
    replayed = ResearchPostprocessV4Terminal.model_validate(terminal.model_dump(mode="json"))

    assert replayed == terminal
    assert terminal.schema_version == 4
    assert terminal.tranche_id == "cross_domain_s0"
    assert terminal.shared_processing_terminal == shared
    assert terminal.semantic_labels_created is False
    assert terminal.gate_5g_credit_claimed is False

    tampered = terminal.model_dump(mode="json")
    tampered["family_id"] = "kimina_autoformalizer_7b"
    with pytest.raises(ValidationError, match="projection differs"):
        ResearchPostprocessV4Terminal.model_validate(tampered)


def test_v4_fake_dynamic_run_writes_60_and_replays_immutably(
    tmp_path: Path,
) -> None:
    binding = _input_binding()
    plan = collection_v3.load_research_collection_v3(
        CONFIG,
        repo_root=ROOT,
    ).plan
    invocation_by_id = {item.invocation_id: item for item in plan.invocations}
    shared = {
        invocation_id: _shared_failure_terminal(
            binding_hash=binding.shared_processing_input_binding_hash,
            invocation_id=invocation_id,
            family_id=invocation.family_id,
            problem_record_id=invocation.problem_record_id,
            seed=invocation.seed,
        )
        for invocation_id, invocation in invocation_by_id.items()
    }
    base = SimpleNamespace(
        repo_root=tmp_path,
        output_root=tmp_path / "postprocess_v4",
        collection_terminals={
            invocation_id: SimpleNamespace(
                status=collection_v1.ResearchTerminalStatus.ORCHESTRATION_FAILED
            )
            for invocation_id in invocation_by_id
        },
    )
    loaded = cast(
        Any,
        SimpleNamespace(
            input_binding=binding,
            base=base,
        ),
    )

    first = _write_terminals_and_reports(loaded, shared)
    second = _write_terminals_and_reports(loaded, shared)

    assert first.manifest == second.manifest
    assert first.manifest.terminal_invocations == 60
    assert len(first.manifest.terminal_artifacts) == 60
    assert len(first.manifest.family_report_artifacts) == 3
    assert first.manifest.status_counts == {"collection_not_raw": 60}

    changed = dict(shared)
    first_id = min(changed)
    changed[first_id] = changed[first_id].model_copy(
        update={"failure_detail": "different immutable bytes"}
    )
    with pytest.raises((ValidationError, postprocess_v1.ResearchPostprocessArtifactConflict)):
        _write_terminals_and_reports(loaded, changed)
