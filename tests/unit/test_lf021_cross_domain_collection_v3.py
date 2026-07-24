"""Closed-dialect LF-021 collector-v3 planning and injected execution tests."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_yaml_mapping
from leanfaith.config.paths import find_repo_root
from leanfaith.generation import research_collection as v1
from leanfaith.generation.public_research_pool import HeldoutResearchFamily
from leanfaith.generation.research_collection_v3 import (
    LoadedResearchCollectionV3,
    ResearchCollectionPreflightReportV3,
    ResearchCollectionV3Config,
    ResearchInvocationExecutorV3,
    ScalableProblemPoolContract,
    ScalableResearchFamily,
    ScalableResearchSourceMatrixV3,
    _build_plan,
    _load_canonical_mapping,
    _load_problem_records_v3,
    _make_invocations,
    _validate_problem_pool_manifest,
    execute_research_collection_v3,
    load_research_collection_v3,
)
from leanfaith.generation.research_overlap_materialize_v3 import (
    materialize_research_overlap_v3,
)
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

ROOT = find_repo_root(Path(__file__).parent)
V1_CONFIG = ROOT / "configs/generation/local_research_collection_v1.yaml"
V2_CONFIG = ROOT / "configs/generation/local_research_collection_v2.yaml"
V3_CONFIG = ROOT / "configs/generation/local_research_collection_cross_domain_s0_v3.yaml"
POOL_ROOT = ROOT / "data/parsed/real_outputs/cross_domain_docstrings_operational_v1"
POOL_RECORDS = POOL_ROOT / "problem_pool_records.jsonl"
POOL_MANIFEST = POOL_ROOT / "problem_pool_manifest.json"
POOL_CONTEXT = POOL_ROOT / "context.json"
HEADER = ROOT / "examples/lf021_public_research_mathlib_header_v1.lean"
SOURCE_MATRIX = ROOT / "configs/generation/local_research_source_matrix_cross_domain_v3.yaml"
V3_MODULE = ROOT / "src/leanfaith/generation/research_collection_v3.py"
FIXED_AT = datetime.datetime(2026, 7, 24, 0, 30, tzinfo=datetime.UTC)


def _loaded_v3() -> LoadedResearchCollectionV3:
    prior = v1.load_research_collection(V1_CONFIG, repo_root=ROOT)
    problems = _load_problem_records_v3(POOL_RECORDS)
    context = v1._load_json_record(POOL_CONTEXT, ContextRecord)
    seeds = {
        "goedel_formalizer_v2_8b": (30,),
        "kimina_autoformalizer_7b": (0,),
        "stepfun_formalizer_7b": (0,),
    }
    families = tuple(
        family.model_copy(update={"seeds": seeds[family.family_id]})
        for family in prior.config.config.families
    )
    runtime = prior.config.config.runtime.model_copy(
        update={
            "orchestration_adapter": v1.ResearchArtifactBinding(
                artifact="src/leanfaith/generation/research_collection_v3.py",
                sha256=hash_file(V3_MODULE),
            )
        }
    )
    config = ResearchCollectionV3Config(
        config_id="lf021_local_research_collection_v3",
        tranche_id="cross_domain_s0",
        frozen_at=FIXED_AT,
        collection_scope="cross_domain_s0_three_family_v3",
        status="ready",
        execution_enabled=True,
        problem_pool_contract=ScalableProblemPoolContract(
            pool_dialect="cross_domain_operational_v1",
            manifest_artifact_kind=("lf021_cross_domain_docstrings_operational_problem_pool_v1"),
            expected_problem_count=20,
        ),
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
        source_matrix=v1.ResearchArtifactBinding(
            artifact=str(SOURCE_MATRIX.relative_to(ROOT)),
            sha256=hash_file(SOURCE_MATRIX),
        ),
        runtime=runtime,
        families=families,
        retry=prior.config.config.retry,
        outputs=v1.ResearchCollectionOutputs(
            root=("data/raw/tests/cross_domain/v3/cross_domain_s0/local_collection"),
            preflight_report=("reports/generation/test_cross_domain_collection_preflight_v3.json"),
        ),
        rules=("test-only injected execution; no model calls, labels, or Gate claims",),
    )
    loaded_config = LoadedConfig(
        config=config,
        path=V3_CONFIG,
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
    invocations = _make_invocations(
        config_hash=loaded_config.config_hash,
        config=config,
        family_bindings=family_bindings,
        qualifications=prior.qualifications,
        problems=problems,
        repo_root=ROOT,
        context=context,
        header_text=HEADER.read_text(encoding="utf-8"),
    )
    plan = _build_plan(
        loaded_config=loaded_config,
        config_path=V3_CONFIG,
        config_file_sha256="1" * 64,
        repo_root=ROOT,
        problems=problems,
        family_bindings=family_bindings,
        invocations=invocations,
    )
    pool_evidence = _validate_problem_pool_manifest(
        document=_load_canonical_mapping(POOL_MANIFEST),
        contract=config.problem_pool_contract,
        config=config,
        problems=problems,
    )
    matrix_raw = load_yaml_mapping(SOURCE_MATRIX)
    matrix = ScalableResearchSourceMatrixV3.model_validate(matrix_raw)
    preflight = ResearchCollectionPreflightReportV3(
        report_kind="lf021_local_research_collection_preflight_v3",
        execution_ready=True,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        tranche_id=plan.tranche_id,
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
    return LoadedResearchCollectionV3(
        config=loaded_config,
        problems=problems,
        context=context,
        source_matrix=matrix,
        pool_evidence=pool_evidence,
        qualifications=prior.qualifications,
        activation_evidence={},
        plan=plan,
        preflight=preflight,
    )


def test_closed_dialect_rejects_kind_source_confusion() -> None:
    with pytest.raises(ValidationError, match="dialect and manifest artifact kind"):
        ScalableProblemPoolContract(
            pool_dialect="cross_domain_operational_v1",
            manifest_artifact_kind=("lf021_gate3_docstrings_operational_problem_pool_v1"),
            expected_problem_count=20,
        )
    with pytest.raises(ValidationError, match="pool dialect and source"):
        ScalableResearchSourceMatrixV3(
            matrix_id="local_research_source_matrix_v3",
            status="pool_compatible_activation_external_to_matrix",
            pool_dialect="cross_domain_operational_v1",
            source="mathlib_gate3_docstrings_operational_v1",
            problem_count=20,
            problem_pool_manifest_sha256="1" * 64,
            collection_authorization_source=(
                "v3_config_and_replayed_qualification_overlap_evidence"
            ),
            families=(
                ScalableResearchFamily(family_id="a", model="m1", revision="1" * 40),
                ScalableResearchFamily(family_id="b", model="m2", revision="2" * 40),
                ScalableResearchFamily(family_id="c", model="m3", revision="3" * 40),
            ),
            heldout=HeldoutResearchFamily(
                family_id="reform_8b",
                model="GuoxinChen/ReForm-8B",
                revision="1589c832cfad679a280b222e694b987a33befd26",
                supervision_eligible=False,
            ),
            rules=("test",),
        )


def test_closed_gate3_algebra_dialect_retains_exact_v2_validation() -> None:
    prior = v1.load_research_collection(V1_CONFIG, repo_root=ROOT)
    gate3_root = ROOT / "data/parsed/real_outputs/gate3_docstrings_operational_v1"
    records_path = gate3_root / "problem_pool_records.jsonl"
    manifest_path = gate3_root / "problem_pool_manifest.json"
    context_path = gate3_root / "context.json"
    runtime = prior.config.config.runtime.model_copy(
        update={
            "orchestration_adapter": v1.ResearchArtifactBinding(
                artifact="src/leanfaith/generation/research_collection_v3.py",
                sha256=hash_file(V3_MODULE),
            )
        }
    )
    config = ResearchCollectionV3Config(
        config_id="lf021_local_research_collection_v3",
        tranche_id="algebra_replay_test",
        frozen_at=FIXED_AT,
        collection_scope="cross_domain_s0_three_family_v3",
        status="ready",
        execution_enabled=True,
        problem_pool_contract=ScalableProblemPoolContract(
            pool_dialect="gate3_algebra_operational_v1",
            manifest_artifact_kind=("lf021_gate3_docstrings_operational_problem_pool_v1"),
            expected_problem_count=40,
        ),
        problem_pool_records=v1.ResearchArtifactBinding(
            artifact=str(records_path.relative_to(ROOT)),
            sha256=hash_file(records_path),
        ),
        problem_pool_manifest=v1.ResearchArtifactBinding(
            artifact=str(manifest_path.relative_to(ROOT)),
            sha256=hash_file(manifest_path),
        ),
        context=v1.ResearchArtifactBinding(
            artifact=str(context_path.relative_to(ROOT)),
            sha256=hash_file(context_path),
        ),
        import_header=v1.ResearchArtifactBinding(
            artifact=str(HEADER.relative_to(ROOT)),
            sha256=hash_file(HEADER),
        ),
        source_matrix=prior.config.config.source_matrix,
        runtime=runtime,
        families=prior.config.config.families,
        retry=prior.config.config.retry,
        outputs=v1.ResearchCollectionOutputs(
            root=("data/raw/tests/v3/algebra_replay_test/local_collection"),
            preflight_report=("reports/generation/test_algebra_replay_preflight_v3.json"),
        ),
        rules=("closed-dialect validation only",),
    )
    problems = _load_problem_records_v3(records_path)
    evidence = _validate_problem_pool_manifest(
        document=_load_canonical_mapping(manifest_path),
        contract=config.problem_pool_contract,
        config=config,
        problems=problems,
    )

    assert evidence.problem_count == 40
    assert evidence.artifact_kind == ("lf021_gate3_docstrings_operational_problem_pool_v1")
    assert all(problem.source == "mathlib_gate3_docstrings_operational_v1" for problem in problems)


def test_final_cross_domain_config_replays_and_plans_60() -> None:
    loaded = load_research_collection_v3(V3_CONFIG, repo_root=ROOT)

    assert loaded.preflight.execution_ready is True
    assert loaded.plan.tranche_id == "cross_domain_s0"
    assert loaded.plan.problem_count == 20
    assert loaded.plan.family_count == 3
    assert loaded.plan.expected_candidate_count == 60
    assert loaded.plan.seed_count_by_family == {
        "goedel_formalizer_v2_8b": 1,
        "kimina_autoformalizer_7b": 1,
        "stepfun_formalizer_7b": 1,
    }
    assert {
        invocation.seed
        for invocation in loaded.plan.invocations
        if invocation.family_id == "goedel_formalizer_v2_8b"
    } == {30}
    assert len(loaded.activation_evidence) == 3
    assert all(
        evidence.overlap_record.problem_count == 20
        and evidence.overlap_record.contamination_status == "unknown"
        and evidence.qualification.qualification_terminal is not None
        for evidence in loaded.activation_evidence.values()
    )


def test_cross_domain_overlap_materializer_exactly_replays_official_bytes() -> None:
    output = ROOT / "reports/generation/overlap_v3/cross_domain_docstrings_operational_v1"
    original = {path: path.read_bytes() for path in sorted(output.glob("*.json"))}
    replay = materialize_research_overlap_v3(
        repo_root=ROOT,
        qualification_collection_config=V1_CONFIG,
        problem_pool_records=POOL_RECORDS,
        problem_pool_manifest=POOL_MANIFEST,
        output_directory=output,
    )

    assert replay.manifest.problem_count == 20
    assert len(replay.records) == 3
    assert all(record.schema_version == 3 for record in replay.records)
    assert all(record.semantic_labels_created is False for record in replay.records)
    assert all(record.gate_5g_credit_claimed is False for record in replay.records)
    assert {path: path.read_bytes() for path in sorted(output.glob("*.json"))} == original


@dataclass
class _AccountingExecutor(ResearchInvocationExecutorV3):
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
            exception=RuntimeError("injected v3 terminal accounting"),
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


def test_fake_execution_and_exact_resume_cover_cross_domain_s0(
    tmp_path: Path,
) -> None:
    loaded = _loaded_v3()
    first = _AccountingExecutor()
    run = execute_research_collection_v3(
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
    assert len(first.execute_calls) == 60
    assert run.manifest.tranche_id == "cross_domain_s0"
    assert run.manifest.expected_candidate_count == 60
    assert run.manifest.terminal_candidate_count == 60
    assert run.manifest.status_counts == {"orchestration_failed": 60}
    initial_bytes = {
        path: path.read_bytes()
        for path in sorted((run.output_directory / "terminals").glob("*.json"))
    }

    replay_executor = _AccountingExecutor()
    replay = execute_research_collection_v3(
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


def test_v1_v2_bound_collector_and_overlap_bytes_are_unchanged() -> None:
    expected = {
        "src/leanfaith/generation/research_collection.py": (
            "14570660891504bcd9cbe73ce649b6621eca304331319e4cb6eb8d983793f3bc"
        ),
        "src/leanfaith/generation/research_collection_v2.py": (
            "4657f184113b1dd1bdb2a272f822454e2f8de2baa85778b7853721e4a2f6a671"
        ),
        "src/leanfaith/generation/research_overlap.py": (
            "8d0e109e00419e674fe13791e3e7476aaffe68b6d23b38feeb889ad3e821622a"
        ),
        "src/leanfaith/generation/research_overlap_v2.py": (
            "94f1f03f012df0df3c2e0f8c9b83d7d472e646ff16f5fe6eef885282d7a66d76"
        ),
        "src/leanfaith/generation/research_overlap_materialize_v2.py": (
            "757c2ab48cdf3a7b64763a898a7bb304fb2706c315735eb3654b30d221d8d66f"
        ),
        "configs/generation/local_research_collection_v1.yaml": (
            "4efce941b0027263dae0b83dbf1d889082916df59b6c6594559edd7db10c7764"
        ),
        "configs/generation/local_research_collection_v2.yaml": (
            "dbc1ce6c0eca76eaba03e26d8619c0caf63a34dcc468aa53803f4ca526ff7403"
        ),
        "reports/generation/overlap/lf021_goedel_public_pool_v1.json": (
            "3f0da406933844c1dfbac8329a346135cb1909defa3e042adb93d8f4c960d699"
        ),
        (
            "reports/generation/overlap_v2/gate3_docstrings_operational_v1/bundle_manifest.json"
        ): "e226fef20f6eea359ead9c8a8892d7a234d8c8ff826fe7f1a9049b4ae6322180",
        (
            "data/raw/real_outputs/public_research_v1/local_collection_v1/"
            "75e16a5cb7ba937463821c92ef612c25475d91e7af00fb38bc2c970fa3dc2393/"
            "manifest.json"
        ): "3c3682f4aef9fe41cf7a648345776587cbbb31cdf7237ccef8077eb7b1accdab",
        (
            "data/raw/real_outputs/gate3_docstrings_operational_v1/v2/local_collection/"
            "3801b405ec8b7008f8c38f449189a52fe5e74bea3a98f5e3e0abdaa75edac62c/"
            "manifest.json"
        ): "4412238aee05466c00a033c8ba95f5721e8253d600dd877f68e0b156ea95d5cb",
    }

    assert {path: hash_file(ROOT / path) for path in expected} == expected
