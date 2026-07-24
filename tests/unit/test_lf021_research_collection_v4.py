"""Generic collector-v4 closed-dialect and deterministic-resume tests."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

from leanfaith.config.paths import find_repo_root
from leanfaith.generation import research_collection as v1
from leanfaith.generation.research_collection_v4 import (
    ResearchInvocationExecutorV4,
    derive_research_collection_v4_config,
    execute_research_collection_v4,
    load_research_collection_v4,
)
from leanfaith.schemas.nl_lean import ProblemPoolRecord

ROOT = find_repo_root(Path(__file__).parent)
ALGEBRA_BASE = ROOT / "configs/generation/local_research_collection_v2.yaml"
CROSS_BASE = ROOT / "configs/generation/local_research_collection_cross_domain_s0_v3.yaml"
POLICY = ROOT / "configs/generation/lf021_tranche_expansion_v1.yaml"
ALGEBRA_DECISION = (
    ROOT / "reports/generation/lf021_tranche_expansion_v1/decisions/"
    "4e89b908916de794221493de0d254a649ed5e4b76fc9bf5e773da831bbf733cc.json"
)
CROSS_DECISION = (
    ROOT / "reports/generation/lf021_tranche_expansion_v1/decisions/"
    "2cc1d3f3f95d27187b261abb91db3bef6e7721c314f77d8948c51b53846b4d69.json"
)
FIXED_AT = datetime.datetime(2026, 7, 24, 8, 0, tzinfo=datetime.UTC)
FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)


def _derive(
    tmp_path: Path,
    *,
    base: Path,
    decision: Path,
    expected_tranche_id: str,
):
    relative = Path("data/raw/tests/collector_v4_configs") / (
        f"{expected_tranche_id}_{tmp_path.name}.json"
    )
    output = ROOT / relative
    matrix_output = output.with_name(output.stem + "_source_matrix.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        derive_research_collection_v4_config(
            base_config_path=base,
            expansion_decision_path=decision,
            expansion_policy_path=POLICY,
            output_source_matrix_path=matrix_output,
            output_config_path=output,
            repo_root=ROOT,
            frozen_at=FIXED_AT,
        )
        return load_research_collection_v4(output, repo_root=ROOT)
    finally:
        output.unlink(missing_ok=True)
        matrix_output.unlink(missing_ok=True)


def test_generic_config_derivation_preserves_pool_specific_overlap_versions(
    tmp_path: Path,
) -> None:
    algebra = _derive(
        tmp_path,
        base=ALGEBRA_BASE,
        decision=ALGEBRA_DECISION,
        expected_tranche_id="algebra_s1",
    )
    cross = _derive(
        tmp_path,
        base=CROSS_BASE,
        decision=CROSS_DECISION,
        expected_tranche_id="cross_domain_s0",
    )

    assert algebra.plan.pool_dialect == "gate3_algebra_operational_v1"
    assert algebra.plan.overlap_schema == "lf021_research_family_overlap_v2"
    assert algebra.plan.problem_count == 40
    assert algebra.plan.expected_candidate_count == 120
    assert cross.plan.pool_dialect == "cross_domain_operational_v1"
    assert cross.plan.overlap_schema == "lf021_research_family_overlap_v3"
    assert cross.plan.problem_count == 20
    assert cross.plan.expected_candidate_count == 60
    assert all(
        evidence.overlap_schema == algebra.plan.overlap_schema
        for evidence in algebra.activation_evidence.values()
    )
    assert all(
        evidence.overlap_schema == cross.plan.overlap_schema
        for evidence in cross.activation_evidence.values()
    )
    assert (
        algebra.config.config.collection_scope
        == "preregistered_closed_pool_three_family_tranche_v4"
    )


@dataclass
class _AccountingExecutor(ResearchInvocationExecutorV4):
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
            exception=RuntimeError("injected collector-v4 accounting terminal"),
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


def test_collector_v4_fake_execution_exactly_resumes(tmp_path: Path) -> None:
    loaded = _derive(
        tmp_path,
        base=CROSS_BASE,
        decision=CROSS_DECISION,
        expected_tranche_id="cross_domain_s0",
    )
    first = _AccountingExecutor()
    run = execute_research_collection_v4(
        loaded,
        repo_root=tmp_path,
        executor=first,
        clock=lambda: FIXED_AT,
    )

    assert first.begin_calls == list(FAMILIES)
    assert first.end_calls == list(FAMILIES)
    assert len(first.execute_calls) == 60
    assert run.manifest.status_counts == {"orchestration_failed": 60}
    initial = {
        path: path.read_bytes()
        for path in sorted((run.output_directory / "terminals").glob("*.json"))
    }

    replay_executor = _AccountingExecutor()
    replay = execute_research_collection_v4(
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
    } == initial
