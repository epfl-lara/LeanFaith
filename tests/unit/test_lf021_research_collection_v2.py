"""Scalable LF-021 collection-v2 planning and injected execution tests."""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_yaml_mapping
from leanfaith.config.paths import find_repo_root
from leanfaith.generation import research_collection as v1
from leanfaith.generation.research_collection_v2 import (
    LoadedResearchCollectionV2,
    ResearchCollectionPreflightReportV2,
    ResearchCollectionV2Config,
    ResearchInvocationExecutorV2,
    ScalableProblemPoolContract,
    ScalableResearchFamily,
    ScalableResearchSourceMatrixV2,
    _build_plan,
    _load_canonical_mapping,
    _load_problem_records_v2,
    _make_invocations,
    _resolve_pool_binding,
    _validate_problem_pool_manifest,
    execute_research_collection_v2,
    load_research_collection_v2,
)
from leanfaith.generation.research_overlap import PublicSourceIntroduction
from leanfaith.generation.research_overlap_materialize_v2 import (
    materialize_research_overlap_v2,
)
from leanfaith.generation.research_overlap_v2 import ResearchFamilyOverlapRecordV2
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

ROOT = find_repo_root(Path(__file__).parent)
V1_CONFIG = ROOT / "configs/generation/local_research_collection_v1.yaml"
V2_CONFIG = ROOT / "configs/generation/local_research_collection_v2.yaml"
POOL_ROOT = ROOT / "data/parsed/real_outputs/gate3_docstrings_operational_v1"
POOL_RECORDS = POOL_ROOT / "problem_pool_records.jsonl"
POOL_MANIFEST = POOL_ROOT / "problem_pool_manifest.json"
POOL_CONTEXT = POOL_ROOT / "context.json"
HEADER = ROOT / "examples/lf021_public_research_mathlib_header_v1.lean"
V2_MODULE = ROOT / "src/leanfaith/generation/research_collection_v2.py"
FIXED_AT = datetime.datetime(2026, 7, 23, 23, 30, tzinfo=datetime.UTC)


def _loaded_v2(*, seeds: tuple[int, ...]) -> LoadedResearchCollectionV2:
    prior = v1.load_research_collection(V1_CONFIG, repo_root=ROOT)
    problems = _load_problem_records_v2(POOL_RECORDS)
    context = v1._load_json_record(POOL_CONTEXT, ContextRecord)
    families = tuple(
        family.model_copy(update={"seeds": seeds}) for family in prior.config.config.families
    )
    runtime = prior.config.config.runtime.model_copy(
        update={
            "orchestration_adapter": v1.ResearchArtifactBinding(
                artifact="src/leanfaith/generation/research_collection_v2.py",
                sha256=hash_file(V2_MODULE),
            )
        }
    )
    config = ResearchCollectionV2Config(
        config_id="lf021_local_research_collection_v2",
        frozen_at=FIXED_AT,
        collection_scope="public_arbitrary_problem_three_family_multiseed_v2",
        status="ready",
        execution_enabled=True,
        problem_pool_contract=ScalableProblemPoolContract(expected_problem_count=40),
        problem_pool_records=v1.ResearchArtifactBinding(
            artifact=str(POOL_RECORDS.relative_to(ROOT)),
            sha256=hash_file(POOL_RECORDS),
        ),
        problem_pool_manifest=v1.ResearchArtifactBinding(
            artifact=str(POOL_MANIFEST.relative_to(ROOT)),
            sha256=hash_file(POOL_MANIFEST),
        ),
        context=v1.ResearchArtifactBinding(
            artifact=str(POOL_CONTEXT.relative_to(ROOT)),
            sha256=hash_file(POOL_CONTEXT),
        ),
        import_header=v1.ResearchArtifactBinding(
            artifact=str(HEADER.relative_to(ROOT)),
            sha256=hash_file(HEADER),
        ),
        source_matrix=prior.config.config.source_matrix,
        runtime=runtime,
        families=families,
        retry=prior.config.config.retry,
        outputs=v1.ResearchCollectionOutputs(
            root="data/raw/tests/v2/local_collection",
            preflight_report="reports/generation/test_collection_preflight_v2.json",
        ),
        rules=("test-only injected execution; no model calls or labels",),
    )
    loaded_config = LoadedConfig(
        config=config,
        path=ROOT / "configs/generation/local_research_collection_v2.yaml",
        raw=config.model_dump(mode="json"),
        config_hash=hash_canonical(config.model_dump(mode="json")),
    )
    family_bindings = tuple(
        sorted(
            (
                v1._family_binding(
                    family=family,
                    loaded=prior.qualifications[family.family_id],
                    config_file_sha256=hash_file(ROOT / family.qualification_pin_source.artifact),
                    runtime=runtime,
                )
                for family in families
            ),
            key=lambda binding: binding.family_id,
        )
    )
    qualifications = prior.qualifications
    invocations = _make_invocations(
        config_hash=loaded_config.config_hash,
        config=config,
        family_bindings=family_bindings,
        qualifications=qualifications,
        problems=problems,
        repo_root=ROOT,
        context=context,
        header_text=HEADER.read_text(encoding="utf-8"),
    )
    plan = _build_plan(
        loaded_config=loaded_config,
        config_path=loaded_config.path,
        config_file_sha256="1" * 64,
        repo_root=ROOT,
        problems=problems,
        family_bindings=family_bindings,
        invocations=invocations,
    )
    manifest_document = _load_canonical_mapping(POOL_MANIFEST)
    pool_evidence = _validate_problem_pool_manifest(
        document=manifest_document,
        contract=config.problem_pool_contract,
        config=config,
        problems=problems,
    )
    preflight = ResearchCollectionPreflightReportV2(
        report_kind="lf021_local_research_collection_preflight_v2",
        execution_ready=True,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        problem_count=plan.problem_count,
        seed_count_by_family=plan.seed_count_by_family,
        planned_candidate_count=plan.expected_candidate_count,
        family_binding_hashes={
            binding.family_id: binding.binding_hash for binding in family_bindings
        },
        invocation_ids=tuple(item.invocation_id for item in invocations),
        checks={"test_fixture_reconciled": True},
        blocking_prerequisites=(),
    )
    prior_matrix = load_yaml_mapping(ROOT / config.source_matrix.artifact)
    matrix = ScalableResearchSourceMatrixV2(
        matrix_id="local_research_source_matrix_v2",
        status="pool_compatible_activation_external_to_matrix",
        source="mathlib_gate3_docstrings_operational_v1",
        problem_count=40,
        problem_pool_manifest_sha256=hash_file(POOL_MANIFEST),
        collection_authorization_source=("v2_config_and_replayed_qualification_overlap_evidence"),
        families=tuple(
            sorted(
                (
                    ScalableResearchFamily(
                        family_id=item["family_id"],
                        model=item["model"],
                        revision=item["revision"],
                    )
                    for item in prior_matrix["families"]
                ),
                key=lambda item: item.family_id,
            )
        ),
        heldout=prior_matrix["heldout"],
        rules=("identity only; activation is external to this matrix",),
    )
    return LoadedResearchCollectionV2(
        config=loaded_config,
        problems=problems,
        context=context,
        source_matrix=matrix,
        pool_evidence=pool_evidence,
        qualifications=qualifications,
        activation_evidence={},
        plan=plan,
        preflight=preflight,
    )


def test_v2_rejects_legacy_config_identity_and_scope() -> None:
    prior = v1.load_research_collection(V1_CONFIG, repo_root=ROOT)
    legacy = prior.config.config.model_dump(mode="json")
    with pytest.raises(ValidationError):
        ResearchCollectionV2Config.model_validate(legacy)


def test_final_v2_config_replays_every_activation_and_plans_120() -> None:
    loaded = load_research_collection_v2(V2_CONFIG, repo_root=ROOT)

    assert loaded.preflight.execution_ready is True
    assert loaded.plan.problem_count == 40
    assert loaded.plan.family_count == 3
    assert loaded.plan.expected_candidate_count == 120
    assert len(loaded.activation_evidence) == 3
    assert all(
        evidence.overlap_record is not None
        and evidence.overlap_record.problem_count == 40
        and evidence.overlap_record.contamination_status == "unknown"
        and evidence.qualification.qualification_terminal is not None
        for evidence in loaded.activation_evidence.values()
    )


def test_exact_operational_pool_manifest_is_accepted_and_bound() -> None:
    loaded = _loaded_v2(seeds=(0,))
    evidence = loaded.pool_evidence

    assert evidence.problem_count == 40
    assert evidence.problem_record_ids == tuple(
        problem.problem_record_id for problem in loaded.problems
    )
    assert evidence.problem_groups == tuple(problem.problem_group for problem in loaded.problems)
    assert len(evidence.declaration_full_names) == 40
    for binding in evidence.critical_artifact_bindings:
        assert hash_file(_resolve_pool_binding(ROOT, binding)) == binding.sha256


def test_v2_plan_is_deterministic_for_40_by_3_by_1() -> None:
    left = _loaded_v2(seeds=(0,))
    right = _loaded_v2(seeds=(0,))

    assert left.plan == right.plan
    assert left.plan.plan_id.startswith("research_collection_plan_v2:")
    assert left.plan.problem_count == 40
    assert left.plan.family_count == 3
    assert left.plan.seed_count_by_family == {
        "goedel_formalizer_v2_8b": 1,
        "kimina_autoformalizer_7b": 1,
        "stepfun_formalizer_7b": 1,
    }
    assert left.plan.expected_candidate_count == 120
    assert len(left.plan.invocations) == 120
    assert len({item.expected_declaration_name for item in left.plan.invocations}) == 120
    assert all(item.semantic_labels_created is False for item in left.plan.invocations)
    assert all(item.gate_5g_credit_claimed is False for item in left.plan.invocations)


def test_v2_plan_reconciles_40_by_3_by_3() -> None:
    loaded = _loaded_v2(seeds=(0, 1, 2))

    assert loaded.plan.expected_candidate_count == 360
    assert len(loaded.plan.invocations) == 360
    assert set(loaded.plan.seed_count_by_family.values()) == {3}
    matrix = {
        (item.problem_record_id, item.family_id, item.seed) for item in loaded.plan.invocations
    }
    assert len(matrix) == 360


def test_overlap_v2_accepts_40_and_rejects_count_drift() -> None:
    prior = v1.load_research_collection(V1_CONFIG, repo_root=ROOT)
    baseline = next(
        evidence.overlap_record
        for evidence in prior.activation_evidence.values()
        if evidence.overlap_record is not None
    )
    problems = _load_problem_records_v2(POOL_RECORDS)
    introductions = tuple(
        PublicSourceIntroduction(
            problem_record_id=problem.problem_record_id,
            problem_id=problem.problem_id,
            introduction_commit=cast_str(problem.metadata["temporal_introduction_commit"]),
            introduction_created_at=(
                baseline.checkpoint_probe.observed_created_at + datetime.timedelta(days=index + 1)
            ),
        )
        for index, problem in enumerate(problems)
    )
    overlap = ResearchFamilyOverlapRecordV2.create(
        family_id=baseline.family_id,
        model_repo_id=baseline.model_repo_id,
        model_revision=baseline.model_revision,
        checkpoint_probe=baseline.checkpoint_probe,
        pinned_readme_sha256=baseline.pinned_readme_sha256,
        training_lineage_disclosure=baseline.training_lineage_disclosure,
        problem_pool_records_sha256=hash_file(POOL_RECORDS),
        problem_pool_manifest_sha256=hash_file(POOL_MANIFEST),
        active_benchmark_manifest_sha256=(
            _load_canonical_mapping(POOL_MANIFEST)["active_benchmark_manifest_artifact"]["sha256"]  # type: ignore[index]
        ),
        active_benchmark_registry_sha256=(
            _load_canonical_mapping(POOL_MANIFEST)["active_benchmark_registry_sha256"]
        ),
        public_source_evidence_sha256="2" * 64,
        problem_count=40,
        source_introductions=introductions,
        interpretation=("temporal_non_overlap_only_semantic_and_pretraining_contamination_unknown"),
    )
    assert overlap.problem_count == 40
    tampered = overlap.model_dump(mode="json")
    tampered["problem_count"] = 39
    with pytest.raises(ValidationError, match="source-introduction count"):
        ResearchFamilyOverlapRecordV2.model_validate(tampered)


def test_overlap_materializer_builds_three_records_and_exactly_replays(
    tmp_path: Path,
) -> None:
    output = ROOT / "reports/generation/overlap_v2" / (f"test_{tmp_path.name}")
    try:
        first = materialize_research_overlap_v2(
            repo_root=ROOT,
            qualification_collection_config=V1_CONFIG,
            problem_pool_records=POOL_RECORDS,
            problem_pool_manifest=POOL_MANIFEST,
            output_directory=output,
        )
        original = {path: path.read_bytes() for path in sorted(output.glob("*.json"))}
        replay = materialize_research_overlap_v2(
            repo_root=ROOT,
            qualification_collection_config=V1_CONFIG,
            problem_pool_records=POOL_RECORDS,
            problem_pool_manifest=POOL_MANIFEST,
            output_directory=output,
        )
        assert replay.manifest == first.manifest
        assert len(first.records) == 3
        assert all(record.problem_count == 40 for record in first.records)
        assert all(record.contamination_status == "unknown" for record in first.records)
        assert all(record.heldout_claim_allowed is False for record in first.records)
        assert {path: path.read_bytes() for path in sorted(output.glob("*.json"))} == original
    finally:
        for path in output.glob("*.json"):
            path.unlink()
        output.rmdir()


def cast_str(value: object) -> str:
    assert isinstance(value, str)
    return value


@dataclass
class _AccountingExecutor(ResearchInvocationExecutorV2):
    begin_calls: list[str] = field(default_factory=list)
    execute_calls: list[str] = field(default_factory=list)
    end_calls: list[str] = field(default_factory=list)

    def begin_family(
        self,
        *,
        family: v1.ResearchFamilyBinding,
        qualification: object,
        runtime: object,
        invocations: tuple[v1.ResearchCollectionInvocation, ...],
        family_directory: Path,
    ) -> None:
        del qualification, runtime, invocations, family_directory
        self.begin_calls.append(family.family_id)

    def execute(
        self,
        *,
        invocation: v1.ResearchCollectionInvocation,
        problem: ProblemPoolRecord,
        qualification: object,
        invocation_directory: Path,
        artifact_root: Path,
    ) -> v1.ResearchCollectionTerminal:
        del problem, qualification, invocation_directory, artifact_root
        self.execute_calls.append(invocation.invocation_id)
        return v1.make_orchestration_failure_terminal(
            invocation,
            exception=RuntimeError("injected v2 terminal accounting"),
            at=FIXED_AT,
        )

    def end_family(
        self,
        *,
        family: v1.ResearchFamilyBinding,
        completed_invocation_ids: tuple[str, ...],
        family_directory: Path,
    ) -> None:
        del completed_invocation_ids, family_directory
        self.end_calls.append(family.family_id)


def test_fake_execution_and_exact_resume_cover_all_120_invocations(
    tmp_path: Path,
) -> None:
    loaded = _loaded_v2(seeds=(0,))
    first = _AccountingExecutor()
    run = execute_research_collection_v2(
        loaded,
        repo_root=tmp_path,
        executor=first,
        clock=lambda: FIXED_AT,
    )

    assert first.begin_calls == [
        "goedel_formalizer_v2_8b",
        "kimina_autoformalizer_7b",
        "stepfun_formalizer_7b",
    ]
    assert first.end_calls == first.begin_calls
    assert len(first.execute_calls) == 120
    assert run.manifest.expected_candidate_count == 120
    assert run.manifest.terminal_candidate_count == 120
    assert run.manifest.status_counts == {"orchestration_failed": 120}
    assert run.manifest.semantic_labels_created is False
    assert run.manifest.gate_5g_credit_claimed is False
    assert run.manifest.gate_5_closed is False
    initial_bytes = {
        path: path.read_bytes()
        for path in sorted((run.output_directory / "terminals").glob("*.json"))
    }

    replay_executor = _AccountingExecutor()
    replay = execute_research_collection_v2(
        loaded,
        repo_root=tmp_path,
        executor=replay_executor,
        clock=lambda: FIXED_AT + datetime.timedelta(hours=1),
    )
    assert replay_executor.begin_calls == []
    assert replay_executor.execute_calls == []
    assert replay_executor.end_calls == []
    assert replay.manifest == run.manifest
    assert {
        path: path.read_bytes()
        for path in sorted((run.output_directory / "terminals").glob("*.json"))
    } == initial_bytes


def test_pool_manifest_tampering_fails_closed() -> None:
    loaded = _loaded_v2(seeds=(0,))
    document = json.loads(POOL_MANIFEST.read_text(encoding="utf-8"))
    document["problem_count"] = 39
    with pytest.raises(
        v1.ResearchCollectionError,
        match="problem count",
    ):
        _validate_problem_pool_manifest(
            document=document,
            contract=loaded.config.config.problem_pool_contract,
            config=loaded.config.config,
            problems=loaded.problems,
        )
