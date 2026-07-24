"""Model-free and injected-runtime tests for the LF-021 research collector."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.paths import find_repo_root
from leanfaith.generation.local_hf import (
    LoadedLocalHFModel,
    LocalHFGeneratedText,
    LocalHFModelPin,
)
from leanfaith.generation.research_collection import (
    LoadedResearchCollection,
    LocalHFResearchExecutor,
    ResearchCollectionArtifactConflict,
    ResearchCollectionPostBoundaryError,
    ResearchCollectionTerminal,
    ResearchFamilyActivation,
    ResearchFamilyBinding,
    ResearchInvocationExecutor,
    ResearchTerminalStatus,
    execute_research_collection,
    load_research_collection,
    make_orchestration_failure_terminal,
)
from leanfaith.generation.research_overlap import ResearchFamilyOverlapRecord
from leanfaith.schemas.nl_lean import ProblemPoolRecord

ROOT = find_repo_root(Path(__file__).parent)
CONFIG = ROOT / "configs/generation/local_research_collection_v1.yaml"
FIXED_AT = datetime.datetime(2026, 7, 23, 23, 0, tzinfo=datetime.UTC)


@pytest.fixture(scope="module")
def loaded() -> LoadedResearchCollection:
    return load_research_collection(CONFIG, repo_root=ROOT)


def test_preflight_is_ready_exact_and_nonsemantic(
    loaded: LoadedResearchCollection,
) -> None:
    report = loaded.preflight

    assert report.execution_ready is True
    assert report.gpu_model_execution_performed is False
    assert report.provider_requests_created == 0
    assert report.terminal_candidates_created == 0
    assert report.semantic_labels_created is False
    assert report.counts_as_smoke_qualification is False
    assert report.gate_5g_credit_claimed is False
    assert report.gate_5_closed is False
    assert len(loaded.problems) == 3
    assert len(loaded.plan.family_bindings) == 3
    assert len(loaded.plan.invocations) == 9
    assert [binding.family_id for binding in loaded.plan.family_bindings] == [
        "goedel_formalizer_v2_8b",
        "kimina_autoformalizer_7b",
        "stepfun_formalizer_7b",
    ]
    assert all(
        evidence.qualification_terminal is not None
        and evidence.qualification_terminal.status.value == "qualified_smoke"
        and evidence.overlap_record is not None
        for evidence in loaded.activation_evidence.values()
    )


def test_overlap_records_allow_collection_but_no_unseen_or_evaluation_claim() -> None:
    paths = sorted((ROOT / "reports/generation/overlap").glob("lf021_*_public_pool_v1.json"))
    assert len(paths) == 3

    for path in paths:
        record = ResearchFamilyOverlapRecord.model_validate_json(path.read_text(encoding="utf-8"))
        assert record.exact_pool_collection_allowed is True
        assert record.contamination_status == "unknown"
        assert record.heldout_claim_allowed is False
        assert record.unseen_claim_allowed is False
        assert record.source_independent_claim_allowed is False
        assert record.evaluation_claim_allowed is False
        assert record.semantic_labels_created is False
        assert record.gate_5g_credit_claimed is False
        assert record.gate_5_closed is False

        tampered = record.model_dump(mode="json")
        tampered["model_revision"] = "0" * 40
        with pytest.raises(ValidationError):
            ResearchFamilyOverlapRecord.model_validate(tampered)


def test_activation_requires_complete_artifact_hash_pairs() -> None:
    with pytest.raises(ValidationError, match="artifact and hash"):
        ResearchFamilyActivation(
            status="blocked",
            qualification_bundle_artifact="runs/example/bundle_manifest.json",
            blocker="hash absent",
        )


@dataclass
class _AccountingExecutor(ResearchInvocationExecutor):
    begin_calls: list[str] = field(default_factory=list)
    execute_calls: list[str] = field(default_factory=list)
    end_calls: list[str] = field(default_factory=list)
    interrupt_after: int | None = None

    def begin_family(
        self,
        *,
        family: ResearchFamilyBinding,
        qualification: object,
        runtime: object,
        invocations: tuple[object, ...],
        family_directory: Path,
    ) -> None:
        del qualification, runtime, invocations, family_directory
        self.begin_calls.append(family.family_id)

    def execute(
        self,
        *,
        invocation: object,
        problem: ProblemPoolRecord,
        qualification: object,
        invocation_directory: Path,
        artifact_root: Path,
    ) -> ResearchCollectionTerminal:
        del problem, qualification, invocation_directory, artifact_root
        typed = invocation
        invocation_id = typed.invocation_id  # type: ignore[attr-defined]
        if self.interrupt_after is not None and len(self.execute_calls) >= self.interrupt_after:
            raise KeyboardInterrupt
        self.execute_calls.append(invocation_id)
        return make_orchestration_failure_terminal(
            typed,  # type: ignore[arg-type]
            exception=RuntimeError("injected terminal accounting"),
            at=FIXED_AT,
        )

    def end_family(
        self,
        *,
        family: ResearchFamilyBinding,
        completed_invocation_ids: tuple[str, ...],
        family_directory: Path,
    ) -> None:
        del completed_invocation_ids, family_directory
        self.end_calls.append(family.family_id)


def test_one_lifecycle_per_family_and_completed_resume_loads_nothing(
    loaded: LoadedResearchCollection,
    tmp_path: Path,
) -> None:
    first = _AccountingExecutor()
    run = execute_research_collection(
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
    assert len(first.execute_calls) == 9
    assert run.manifest.status_counts == {"orchestration_failed": 9}

    resumed = _AccountingExecutor()
    replay = execute_research_collection(
        loaded,
        repo_root=tmp_path,
        executor=resumed,
        clock=lambda: FIXED_AT,
    )
    assert resumed.begin_calls == []
    assert resumed.execute_calls == []
    assert resumed.end_calls == []
    assert replay.manifest == run.manifest


def test_keyboard_interrupt_preserves_first_terminal_and_resume_skips_it(
    loaded: LoadedResearchCollection,
    tmp_path: Path,
) -> None:
    interrupted = _AccountingExecutor(interrupt_after=1)
    with pytest.raises(KeyboardInterrupt):
        execute_research_collection(
            loaded,
            repo_root=tmp_path,
            executor=interrupted,
            clock=lambda: FIXED_AT,
        )
    assert len(interrupted.execute_calls) == 1
    terminal_paths = sorted(
        (
            tmp_path
            / loaded.config.config.outputs.root
            / loaded.plan.plan_id.rsplit(":", 1)[-1]
            / "terminals"
        ).glob("*.json")
    )
    assert len(terminal_paths) == 1
    persisted_terminal_path = terminal_paths[0]
    persisted_terminal_bytes = persisted_terminal_path.read_bytes()

    resumed = _AccountingExecutor()
    run = execute_research_collection(
        loaded,
        repo_root=tmp_path,
        executor=resumed,
        clock=lambda: FIXED_AT,
    )
    assert len(resumed.execute_calls) == 8
    assert resumed.begin_calls[0] == "goedel_formalizer_v2_8b"
    goedel_invocations = {
        item.invocation_id
        for item in loaded.plan.invocations
        if item.family_id == "goedel_formalizer_v2_8b"
    }
    assert len(goedel_invocations.intersection(resumed.execute_calls)) == 2
    assert persisted_terminal_path.read_bytes() == persisted_terminal_bytes
    assert run.manifest.terminal_candidate_count == 9


@dataclass
class _PostBoundaryCrash(_AccountingExecutor):
    def execute(
        self,
        *,
        invocation: object,
        problem: ProblemPoolRecord,
        qualification: object,
        invocation_directory: Path,
        artifact_root: Path,
    ) -> ResearchCollectionTerminal:
        del invocation, problem, qualification, artifact_root
        invocation_directory.mkdir(parents=True, exist_ok=True)
        (invocation_directory / "provider_request.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        raise RuntimeError("injected post-boundary crash")


def test_post_boundary_crash_is_never_relabelled_pre_provider(
    loaded: LoadedResearchCollection,
    tmp_path: Path,
) -> None:
    with pytest.raises(ResearchCollectionPostBoundaryError):
        execute_research_collection(
            loaded,
            repo_root=tmp_path,
            executor=_PostBoundaryCrash(),
            clock=lambda: FIXED_AT,
        )
    assert not list((tmp_path / loaded.config.config.outputs.root).glob("**/terminals/*.json"))


class _ChatTokenizer:
    def apply_chat_template(
        self,
        messages: object,
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> str:
        del tokenize, add_generation_prompt
        return f"formatted:{messages!r}"


@dataclass
class _FakeLoader:
    loads: int = 0
    unloads: int = 0

    def load(self, pin: LocalHFModelPin) -> LoadedLocalHFModel:
        del pin
        self.loads += 1
        return LoadedLocalHFModel(tokenizer=_ChatTokenizer(), model=object())

    def unload(self, loaded: LoadedLocalHFModel) -> None:
        del loaded
        self.unloads += 1


@dataclass
class _FakeGenerator:
    calls: int = 0

    def generate(self, **kwargs: object) -> LocalHFGeneratedText:
        del kwargs
        self.calls += 1
        return LocalHFGeneratedText(
            raw_text=f"theorem generated_{self.calls} : True := by trivial",
            prompt_tokens=10,
            output_tokens=8,
        )


@dataclass
class _InterruptingGenerator:
    calls: int = 0

    def generate(self, **kwargs: object) -> LocalHFGeneratedText:
        del kwargs
        self.calls += 1
        raise KeyboardInterrupt


def test_local_executor_loads_once_for_three_family_candidates(
    loaded: LoadedResearchCollection,
    tmp_path: Path,
) -> None:
    family = loaded.plan.family_bindings[0]
    invocations = tuple(
        item for item in loaded.plan.invocations if item.family_id == family.family_id
    )
    problems = {item.problem_record_id: item for item in loaded.problems}
    loader = _FakeLoader()
    generator = _FakeGenerator()
    executor = LocalHFResearchExecutor(loader=loader, generator=generator)
    family_directory = tmp_path / "families" / family.family_id

    executor.begin_family(
        family=family,
        qualification=loaded.qualifications[family.family_id],
        runtime=loaded.config.config.runtime,
        invocations=invocations,
        family_directory=family_directory,
    )
    terminals = tuple(
        executor.execute(
            invocation=invocation,
            problem=problems[invocation.problem_record_id],
            qualification=loaded.qualifications[family.family_id],
            invocation_directory=tmp_path / "invocations" / str(index),
            artifact_root=tmp_path,
        )
        for index, invocation in enumerate(invocations)
    )
    executor.end_family(
        family=family,
        completed_invocation_ids=tuple(item.invocation_id for item in invocations),
        family_directory=family_directory,
    )

    assert loader.loads == 1
    assert generator.calls == 3
    assert loader.unloads == 1
    assert all(item.status is ResearchTerminalStatus.RAW_COLLECTED for item in terminals)
    assert all(item.semantic_labels_created is False for item in terminals)
    assert all(item.gate_5g_credit_claimed is False for item in terminals)


def test_real_executor_resumes_persisted_model_attempt_without_second_call(
    loaded: LoadedResearchCollection,
    tmp_path: Path,
) -> None:
    first_loader = _FakeLoader()
    interrupting_generator = _InterruptingGenerator()
    with pytest.raises(KeyboardInterrupt):
        execute_research_collection(
            loaded,
            repo_root=tmp_path,
            executor=LocalHFResearchExecutor(
                clock=lambda: FIXED_AT,
                loader=first_loader,
                generator=interrupting_generator,
            ),
            clock=lambda: FIXED_AT,
        )
    assert first_loader.loads == 1
    assert first_loader.unloads == 1
    assert interrupting_generator.calls == 1

    output_root = (
        tmp_path / loaded.config.config.outputs.root / loaded.plan.plan_id.rsplit(":", 1)[-1]
    )
    boundaries = tuple(output_root.glob("invocations/*/provider_boundary.json"))
    attempts = tuple(output_root.glob("invocations/*/model_attempt_boundary.json"))
    assert len(boundaries) == 1
    assert len(attempts) == 1
    boundary_path = boundaries[0]
    boundary_bytes = boundary_path.read_bytes()

    resumed_loader = _FakeLoader()
    resumed_generator = _FakeGenerator()
    resumed_at = FIXED_AT + datetime.timedelta(hours=1)
    run = execute_research_collection(
        loaded,
        repo_root=tmp_path,
        executor=LocalHFResearchExecutor(
            clock=lambda: resumed_at,
            loader=resumed_loader,
            generator=resumed_generator,
        ),
        clock=lambda: resumed_at,
    )

    assert boundary_path.read_bytes() == boundary_bytes
    assert resumed_loader.loads == 3
    assert resumed_loader.unloads == 3
    assert resumed_generator.calls == 8
    assert run.manifest.status_counts == {
        "raw_collected": 8,
        "runtime_failed": 1,
    }
    incomplete = next(
        item for item in run.terminals if item.status is ResearchTerminalStatus.RUNTIME_FAILED
    )
    assert incomplete.error_code == "IncompletePriorModelAttempt"
    assert incomplete.resumed_from_persisted_runtime_result is True
    assert incomplete.model_invocation_attempted is True
    assert incomplete.artifact_hashes["provider_boundary"]
    assert incomplete.artifact_hashes["model_attempt_boundary"]
    assert incomplete.artifact_hashes["family_session_start"]
    assert len(run.manifest.family_session_artifact_hashes) == 8


@dataclass
class _LoadFailureExecutor(_AccountingExecutor):
    failed_family: str = "goedel_formalizer_v2_8b"

    def begin_family(
        self,
        *,
        family: ResearchFamilyBinding,
        qualification: object,
        runtime: object,
        invocations: tuple[object, ...],
        family_directory: Path,
    ) -> None:
        super().begin_family(
            family=family,
            qualification=qualification,
            runtime=runtime,
            invocations=invocations,
            family_directory=family_directory,
        )
        if family.family_id == self.failed_family:
            raise RuntimeError("injected family load failure")


def test_family_load_failure_accounts_every_pending_candidate(
    loaded: LoadedResearchCollection,
    tmp_path: Path,
) -> None:
    executor = _LoadFailureExecutor()
    run = execute_research_collection(
        loaded,
        repo_root=tmp_path,
        executor=executor,
        clock=lambda: FIXED_AT,
    )

    failed = tuple(item for item in run.terminals if item.family_id == executor.failed_family)
    assert len(failed) == 3
    assert all(
        item.status is ResearchTerminalStatus.ORCHESTRATION_FAILED
        and item.model_invocation_attempted is False
        and item.artifact_hashes == {}
        for item in failed
    )
    assert len(executor.execute_calls) == 6


def test_real_executor_manifest_binds_family_sessions_and_rejects_tamper(
    loaded: LoadedResearchCollection,
    tmp_path: Path,
) -> None:
    loader = _FakeLoader()
    generator = _FakeGenerator()
    executor = LocalHFResearchExecutor(
        clock=lambda: FIXED_AT,
        loader=loader,
        generator=generator,
    )
    run = execute_research_collection(
        loaded,
        repo_root=tmp_path,
        executor=executor,
        clock=lambda: FIXED_AT,
    )

    assert loader.loads == 3
    assert loader.unloads == 3
    assert generator.calls == 9
    assert run.manifest.status_counts == {"raw_collected": 9}
    session_hashes = run.manifest.family_session_artifact_hashes
    assert len(session_hashes) == 6
    assert sum(path.endswith("family_session_start.json") for path in session_hashes) == 3
    assert sum(path.endswith("family_session_end.json") for path in session_hashes) == 3

    replay_loader = _FakeLoader()
    replay = execute_research_collection(
        loaded,
        repo_root=tmp_path,
        executor=LocalHFResearchExecutor(
            clock=lambda: FIXED_AT,
            loader=replay_loader,
            generator=_FakeGenerator(),
        ),
        clock=lambda: FIXED_AT,
    )
    assert replay.manifest == run.manifest
    assert replay_loader.loads == 0

    bound_end = next(path for path in session_hashes if path.endswith("family_session_end.json"))
    (tmp_path / bound_end).unlink()
    with pytest.raises(ResearchCollectionArtifactConflict):
        execute_research_collection(
            loaded,
            repo_root=tmp_path,
            executor=LocalHFResearchExecutor(
                clock=lambda: FIXED_AT,
                loader=_FakeLoader(),
                generator=_FakeGenerator(),
            ),
            clock=lambda: FIXED_AT,
        )
