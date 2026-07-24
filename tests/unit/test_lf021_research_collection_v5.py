"""Collector-v5 typing, provenance, and closed-dialect regression tests."""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.paths import find_repo_root
from leanfaith.generation import research_collection as v1
from leanfaith.generation.research_collection_v5 import (
    ResearchCollectionV5Error,
    ResearchInvocationExecutorV5,
    derive_research_collection_v5_config,
    execute_research_collection_v5,
    load_research_collection_v5,
    write_preflight_report_v5,
)
from leanfaith.generation.tranche_expansion import ExpansionDecision
from leanfaith.schemas.nl_lean import ProblemPoolRecord

ROOT = find_repo_root(Path(__file__).parent)
ALGEBRA_BASE = ROOT / "configs/generation/local_research_collection_v2.yaml"
CROSS_BASE = ROOT / "configs/generation/local_research_collection_cross_domain_s0_v3.yaml"
POLICY = ROOT / "configs/generation/lf021_tranche_expansion_v1.yaml"
ALGEBRA_DECISION = (
    ROOT / "reports/generation/lf021_tranche_expansion_v1/decisions/"
    "4e89b908916de794221493de0d254a649ed5e4b76fc9bf5e773da831bbf733cc.json"
)
ALGEBRA_S2_DECISION = (
    ROOT / "reports/generation/lf021_tranche_expansion_v1/decisions/"
    "024550993e73ef6532a29d0ec1a029b90c74de796e2863a99bde1c9405857365.json"
)
CROSS_DECISION = (
    ROOT / "reports/generation/lf021_tranche_expansion_v1/decisions/"
    "2cc1d3f3f95d27187b261abb91db3bef6e7721c314f77d8948c51b53846b4d69.json"
)
MODULE = ROOT / "src/leanfaith/generation/research_collection_v5.py"
CLI = ROOT / "scripts/27_collect_research_tranche_v5.py"
FIXED_AT = datetime.datetime(2026, 7, 24, 9, 0, tzinfo=datetime.UTC)


def _output_paths(tmp_path: Path, stem: str) -> tuple[Path, Path]:
    directory = ROOT / "data/raw/tests/collector_v5_configs"
    config = directory / f"{stem}_{tmp_path.name}.json"
    return config, config.with_name(config.stem + "_source_matrix.json")


def _derive_and_load(
    tmp_path: Path,
    *,
    stem: str,
    base: Path,
    decision: Path,
):
    config, matrix = _output_paths(tmp_path, stem)
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        output, digest = derive_research_collection_v5_config(
            base_config_path=base,
            expansion_decision_path=decision,
            expansion_policy_path=POLICY,
            output_source_matrix_path=matrix,
            output_config_path=config,
            repo_root=ROOT,
            frozen_at=FIXED_AT,
        )
        assert output == config
        assert digest == hash_file(config)
        return load_research_collection_v5(config, repo_root=ROOT)
    finally:
        config.unlink(missing_ok=True)
        matrix.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("stem", "base", "decision_path", "dialect", "overlap", "problems", "candidates"),
    [
        (
            "algebra_s2",
            ALGEBRA_BASE,
            ALGEBRA_S2_DECISION,
            "gate3_algebra_operational_v1",
            "lf021_research_family_overlap_v2",
            40,
            120,
        ),
        (
            "cross_domain_s0",
            CROSS_BASE,
            CROSS_DECISION,
            "cross_domain_operational_v1",
            "lf021_research_family_overlap_v3",
            20,
            60,
        ),
    ],
)
def test_v5_derivation_load_and_preflight_bind_exact_decision_for_both_dialects(
    tmp_path: Path,
    stem: str,
    base: Path,
    decision_path: Path,
    dialect: str,
    overlap: str,
    problems: int,
    candidates: int,
) -> None:
    loaded = _derive_and_load(
        tmp_path,
        stem=stem,
        base=base,
        decision=decision_path,
    )
    decision = ExpansionDecision.model_validate_json(decision_path.read_text(encoding="utf-8"))
    config = loaded.config.config

    assert config.schema_version == 5
    assert config.config_id == "lf021_local_research_collection_v5"
    assert decision.next_tranche is not None
    assert config.tranche_id == decision.next_tranche.tranche_id
    assert config.expansion_decision.artifact == str(decision_path.relative_to(ROOT))
    assert config.expansion_decision.sha256 == hash_file(decision_path)
    assert config.orchestration_cli.artifact == str(CLI.relative_to(ROOT))
    assert config.orchestration_cli.sha256 == hash_file(CLI)
    assert config.runtime.orchestration_adapter.artifact == str(MODULE.relative_to(ROOT))
    assert config.runtime.orchestration_adapter.sha256 == hash_file(MODULE)
    assert loaded.source_matrix.schema_version == 5
    assert loaded.source_matrix.matrix_id.startswith("local_research_source_matrix_v5:")
    assert loaded.plan.schema_version == 5
    assert loaded.plan.plan_id.startswith("research_collection_plan_v5:")
    assert loaded.plan.expansion_decision_id == decision.decision_id
    assert loaded.plan.expansion_decision_sha256 == hash_file(decision_path)
    assert loaded.plan.orchestration_cli_sha256 == hash_file(CLI)
    assert loaded.plan.pool_dialect == dialect
    assert loaded.plan.overlap_schema == overlap
    assert loaded.plan.problem_count == problems
    assert loaded.plan.expected_candidate_count == candidates
    assert loaded.preflight.schema_version == 5
    assert loaded.preflight.report_kind == "lf021_local_research_collection_preflight_v5"
    assert loaded.preflight.execution_ready is True
    assert loaded.preflight.checks["exact_v5_module_and_cli_bound"] is True

    assert config.semantic_labels_created is False
    assert config.gate_5g_credit_claimed is False
    assert config.gate_5_closed is False
    assert loaded.plan.semantic_labels_created is False
    assert loaded.plan.gate_5g_credit_claimed is False
    assert loaded.plan.gate_5_closed is False
    assert loaded.preflight.semantic_labels_created is False
    assert loaded.preflight.gate_5g_credit_claimed is False
    assert loaded.preflight.gate_5_closed is False

    report_path, report_hash = write_preflight_report_v5(loaded, repo_root=tmp_path)
    assert report_path.is_file()
    assert report_hash == hash_file(report_path)
    assert json.loads(report_path.read_text(encoding="utf-8"))["semantic_labels_created"] is False


def test_v5_loader_rejects_tampered_expansion_decision_binding(tmp_path: Path) -> None:
    config, matrix = _output_paths(tmp_path, "tampered_algebra_s1")
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        derive_research_collection_v5_config(
            base_config_path=ALGEBRA_BASE,
            expansion_decision_path=ALGEBRA_DECISION,
            expansion_policy_path=POLICY,
            output_source_matrix_path=matrix,
            output_config_path=config,
            repo_root=ROOT,
            frozen_at=FIXED_AT,
        )
        document = json.loads(config.read_text(encoding="utf-8"))
        document["expansion_decision"]["sha256"] = "0" * 64
        config.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with pytest.raises(ResearchCollectionV5Error):
            load_research_collection_v5(config, repo_root=ROOT)
    finally:
        config.unlink(missing_ok=True)
        matrix.unlink(missing_ok=True)


def test_v5_derivation_rejects_rehashed_nonpolicy_next_tranche(
    tmp_path: Path,
) -> None:
    config, matrix = _output_paths(tmp_path, "tampered_policy_tranche")
    decision_path = config.with_name(config.stem + "_decision.json")
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        document = json.loads(ALGEBRA_DECISION.read_text(encoding="utf-8"))
        document["next_tranche"]["tranche_id"] = "algebra_policy_bypass"
        document["decision_id"] = "lf021_expansion_decision:" + hash_canonical(
            {
                "schema": "lf021_expansion_decision_v1",
                **{key: value for key, value in document.items() if key != "decision_id"},
            }
        )
        decision_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with pytest.raises(
            ResearchCollectionV5Error,
            match="next tranche differs from the bound policy",
        ):
            derive_research_collection_v5_config(
                base_config_path=ALGEBRA_BASE,
                expansion_decision_path=decision_path,
                expansion_policy_path=POLICY,
                output_source_matrix_path=matrix,
                output_config_path=config,
                repo_root=ROOT,
                frozen_at=FIXED_AT,
            )
    finally:
        config.unlink(missing_ok=True)
        matrix.unlink(missing_ok=True)
        decision_path.unlink(missing_ok=True)


def test_v5_derivation_rejects_rehashed_operational_counts(
    tmp_path: Path,
) -> None:
    config, matrix = _output_paths(tmp_path, "tampered_operational_counts")
    decision_path = config.with_name(config.stem + "_decision.json")
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        document = json.loads(ALGEBRA_DECISION.read_text(encoding="utf-8"))
        contributions = document["counts"]["unique_contribution_by_family"]
        contributions["goedel_formalizer_v2_8b"] += 1
        document["decision_id"] = "lf021_expansion_decision:" + hash_canonical(
            {
                "schema": "lf021_expansion_decision_v1",
                **{key: value for key, value in document.items() if key != "decision_id"},
            }
        )
        decision_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with pytest.raises(
            ResearchCollectionV5Error,
            match="differs from exact policy replay",
        ):
            derive_research_collection_v5_config(
                base_config_path=ALGEBRA_BASE,
                expansion_decision_path=decision_path,
                expansion_policy_path=POLICY,
                output_source_matrix_path=matrix,
                output_config_path=config,
                repo_root=ROOT,
                frozen_at=FIXED_AT,
            )
    finally:
        config.unlink(missing_ok=True)
        matrix.unlink(missing_ok=True)
        decision_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("root", "/tmp/v5/algebra_s1/local_collection"),
        ("root", "../v5/algebra_s1/local_collection"),
        ("root", "data/raw/real_outputs/wrong/v5/algebra_s1/local_collection"),
        ("preflight_report", "/tmp/preflight_algebra_s1_v5.json"),
        ("preflight_report", "../reports/preflight_algebra_s1_v5.json"),
    ],
)
def test_v5_loader_rejects_noncanonical_output_paths(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    config, matrix = _output_paths(tmp_path, f"tampered_output_{field}")
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        derive_research_collection_v5_config(
            base_config_path=ALGEBRA_BASE,
            expansion_decision_path=ALGEBRA_DECISION,
            expansion_policy_path=POLICY,
            output_source_matrix_path=matrix,
            output_config_path=config,
            repo_root=ROOT,
            frozen_at=FIXED_AT,
        )
        document = json.loads(config.read_text(encoding="utf-8"))
        document["outputs"][field] = value
        config.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with pytest.raises(
            ValueError,
            match=r"repository-relative|exact tranche",
        ):
            load_research_collection_v5(config, repo_root=ROOT)
    finally:
        config.unlink(missing_ok=True)
        matrix.unlink(missing_ok=True)


def test_v5_cli_imports_only_the_v5_collector_boundary() -> None:
    source = CLI.read_text(encoding="utf-8")
    assert "leanfaith.generation.research_collection_v5" in source
    assert "research_collection_v4" not in source


@dataclass
class _AccountingExecutor(ResearchInvocationExecutorV5):
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
            exception=RuntimeError("injected collector-v5 accounting terminal"),
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


def test_v5_fake_execution_preserves_v4_resume_semantics(tmp_path: Path) -> None:
    loaded = _derive_and_load(
        tmp_path,
        stem="cross_domain_s0_execution",
        base=CROSS_BASE,
        decision=CROSS_DECISION,
    )
    first = _AccountingExecutor()
    run = execute_research_collection_v5(
        loaded,
        repo_root=tmp_path,
        executor=first,
        clock=lambda: FIXED_AT,
    )

    assert len(first.begin_calls) == 3
    assert len(first.end_calls) == 3
    assert len(first.execute_calls) == 60
    assert run.manifest.schema_version == 5
    assert run.manifest.manifest_id.startswith("research_collection_manifest_v5:")
    assert run.manifest.status_counts == {"orchestration_failed": 60}
    assert run.manifest.semantic_labels_created is False
    assert run.manifest.gate_5g_credit_claimed is False
    assert run.manifest.gate_5_closed is False
    initial = {
        path: path.read_bytes()
        for path in sorted((run.output_directory / "terminals").glob("*.json"))
    }

    replay_executor = _AccountingExecutor()
    replay = execute_research_collection_v5(
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
