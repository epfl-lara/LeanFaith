from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Never, cast

import pytest

import leanfaith.generation.real_outputs as real_outputs
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.paths import RepoPaths
from leanfaith.generation import extended_gate5g
from leanfaith.generation import post_exhaustion_collection_v1 as planning
from leanfaith.generation import post_exhaustion_collection_v6 as collection
from leanfaith.generation import post_exhaustion_extension as extension
from leanfaith.generation import post_exhaustion_postprocess_v7 as postprocess
from leanfaith.generation import research_collection as collection_v1
from leanfaith.generation import tranche_expansion as tranche
from leanfaith.generation.local_hf import (
    LoadedLocalHFModel,
    LocalHFGeneratedText,
    LocalHFModelPin,
)
from leanfaith.generation.tranche_expansion import (
    LoadedObservation,
    TrancheExpansionPolicy,
    TrancheSpec,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.pipeline import RepresentationBatch, RepresentationBatchResult
from leanfaith.schemas.enums import ViewStatus
from leanfaith.schemas.ids import REPRESENTATION_PREFIX, make_id
from leanfaith.schemas.theorem import CANONICAL_VIEW_NAMES, RepresentationRecord
from tests.unit.test_lf021_post_exhaustion_extension import _synthetic_context

ROOT = Path(__file__).resolve().parents[2]
AUTHORIZATION_POLICY = ROOT / "configs/generation/lf021_post_exhaustion_collection_v1.yaml"
EXECUTION_POLICY = ROOT / "configs/generation/lf021_post_exhaustion_execution_v1.yaml"
FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)


@pytest.fixture
def repo_tmp(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    path = ROOT / "data/raw/tests/post_exhaustion_v6" / f"{tmp_path.name}-{os.getpid()}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


class _AlwaysFailBeforeBoundary:
    def __init__(self) -> None:
        self.begin_calls = 0

    def begin_family(self, **_: object) -> Never:
        self.begin_calls += 1
        raise RuntimeError("synthetic pre-boundary failure")

    def execute(self, **_: object) -> Never:
        raise AssertionError("execute must not be reached")

    def end_family(self, **_: object) -> Never:
        raise AssertionError("end_family must not be reached")


class _MustNotRun:
    def begin_family(self, **_: object) -> Never:
        raise AssertionError("resume must not load a model")

    def execute(self, **_: object) -> Never:
        raise AssertionError("resume must not execute a model")

    def end_family(self, **_: object) -> Never:
        raise AssertionError("resume must not unload a model")


class _FailAfterBoundary:
    def begin_family(self, **_: object) -> None:
        return None

    def execute(self, **kwargs: object) -> Never:
        invocation_directory = cast(Path, kwargs["invocation_directory"])
        invocation_directory.mkdir(parents=True, exist_ok=True)
        (invocation_directory / "provider_boundary.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        raise RuntimeError("synthetic post-boundary failure")

    def end_family(self, **_: object) -> None:
        return None


class _FixtureChatTokenizer:
    def apply_chat_template(
        self,
        messages: object,
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> str:
        del tokenize, add_generation_prompt
        return repr(messages)


class _FixtureLoader:
    def load(self, pin: LocalHFModelPin) -> LoadedLocalHFModel:
        del pin
        return LoadedLocalHFModel(tokenizer=_FixtureChatTokenizer(), model=object())

    def unload(self, loaded: LoadedLocalHFModel) -> None:
        del loaded


class _FixtureGenerator:
    def generate(self, **kwargs: object) -> LocalHFGeneratedText:
        prompt = cast(str, kwargs["formatted_prompt"])
        names = sorted(set(re.findall(r"\blf021_research_[A-Za-z0-9_]+_s\d+\b", prompt)))
        if len(names) != 1:
            raise AssertionError(f"expected one frozen declaration name, observed {names!r}")
        return LocalHFGeneratedText(
            raw_text=f"```lean4\ntheorem {names[0]} : True := by trivial\n```",
            prompt_tokens=10,
            output_tokens=10,
        )


def _source_position(source: str, offset: int) -> dict[str, int]:
    prefix = source[:offset]
    return {
        "line": prefix.count("\n") + 1,
        "column": len(prefix.rsplit("\n", 1)[-1]),
    }


class _FixtureLeanBackend:
    """Lean-shaped declaration metadata for the v7 wrapper success fixture."""

    def __init__(self) -> None:
        self.requests: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        source = request.code
        if source is None:
            raise AssertionError("the success fixture requires Lean source")
        matches = tuple(
            re.finditer(
                r"\btheorem\s+(?P<name>lf021_research_[A-Za-z0-9_]+_s\d+)\b",
                source,
            )
        )
        if len(matches) != 1:
            raise AssertionError(f"expected one generated theorem, observed {len(matches)}")
        head = matches[0]
        proof = re.search(r"\s*:=\s*by\s+(?:trivial|sorry)\b", source[head.end() :])
        if proof is None:
            raise AssertionError("generated theorem lacks the fixture proof suffix")
        signature_start = head.end()
        while signature_start < len(source) and source[signature_start].isspace():
            signature_start += 1
        signature_finish = head.end() + proof.start()
        declaration = {
            "name": head.group("name"),
            "full_name": head.group("name"),
            "kind": "theorem",
            "range": {
                "start": _source_position(source, head.start()),
                "finish": _source_position(source, len(source)),
            },
            "signature": {
                "pp": ": True",
                "range": {
                    "start": _source_position(source, signature_start),
                    "finish": _source_position(source, signature_finish),
                },
            },
            "type": {"pp": "True"},
        }
        context_fingerprint = request.context_id.removeprefix("ctx:")
        return LeanResult(
            request_id=request.request_id,
            request_hash=sha256_hex(source.encode("utf-8")),
            context_id=request.context_id,
            context_fingerprint=context_fingerprint,
            status=LeanStatus.VALID_WITH_SORRY,
            declarations=(declaration,),
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


def _patch_fixture_representation_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    def build(
        backend: LeanInteractBackend,
        batch: RepresentationBatch,
        *,
        created_at: datetime.datetime,
    ) -> RepresentationBatchResult:
        del backend
        theorem = batch.ordered_theorem_inputs[0]
        statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
        for name in (
            "raw_proof_stripped",
            "headless",
            "signature_pp",
            "signature_explicit",
            "semantic_atoms",
            "operator_tree",
        ):
            statuses[name] = ViewStatus.OK
        representation = RepresentationRecord(
            representation_id=make_id(
                REPRESENTATION_PREFIX,
                {
                    "theorem_id": theorem.theorem_id,
                    "normalization_version": "repr_v2",
                },
            ),
            theorem_id=theorem.theorem_id,
            normalization_version="repr_v2",
            context_id=batch.context_id,
            raw_proof_stripped=theorem.proof_stripped,
            headless=theorem.source_signature or ": True",
            signature_pp=theorem.source_signature or ": True",
            signature_explicit="True",
            semantic_atoms=("True",),
            operator_tree={"kind": "const", "name": "True"},
            alpha_identity_fingerprint="d" * 64,
            view_status=statuses,
            content_hash=hash_canonical(
                {
                    "theorem_id": theorem.theorem_id,
                    "views": "post_exhaustion_v7_success_fixture",
                }
            ),
            created_at=created_at,
        )
        return RepresentationBatchResult((representation,), ())

    monkeypatch.setattr(real_outputs, "build_representation_batch", build)


def _prepare_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> collection.LoadedPostExhaustionCollectionV6:
    original_loader = tranche.load_postprocess_observation
    loaded_extension, activation, _, _ = _synthetic_context(tmp_path, monkeypatch)
    synthetic_loader = tranche.load_postprocess_observation

    def load_synthetic_or_real(
        *,
        repo_root: Path,
        policy: TrancheExpansionPolicy,
        tranche: TrancheSpec,
        manifest_path: Path,
    ) -> LoadedObservation:
        try:
            return synthetic_loader(
                repo_root=repo_root,
                policy=policy,
                tranche=tranche,
                manifest_path=manifest_path,
            )
        except KeyError:
            return original_loader(
                repo_root=repo_root,
                policy=policy,
                tranche=tranche,
                manifest_path=manifest_path,
            )

    monkeypatch.setattr(
        tranche,
        "load_postprocess_observation",
        load_synthetic_or_real,
    )
    decision = extension.evaluate_post_exhaustion_extension(
        repo_root=ROOT,
        loaded_policy=loaded_extension,
        activation_v2_decision_path=activation,
        extension_observed_manifests=(),
    )
    decision_path = tmp_path / "extension-decision.json"
    decision_path.write_bytes(canonical_json_bytes(decision.model_dump(mode="json")))
    authorization = planning.write_reviewed_extension_collection_authorization_v1(
        repo_root=ROOT,
        authorization_policy_path=AUTHORIZATION_POLICY,
        extension_decision_path=decision_path,
        output_root=tmp_path / "authorizations",
    )
    output_config = tmp_path / "execution-config.json"
    collection.prepare_post_exhaustion_collection_v6(
        repo_root=ROOT,
        execution_policy_path=EXECUTION_POLICY,
        authorization_path=authorization.path,
        frozen_at=datetime.datetime(2026, 7, 24, tzinfo=datetime.UTC),
        planning_output_root=tmp_path / "planning",
        output_config_path=output_config,
    )
    loaded = collection.load_post_exhaustion_collection_v6(
        output_config,
        repo_root=ROOT,
    )
    run_root = (
        ROOT / loaded.config.config.output_root / loaded.planning_plan.plan_id.rsplit(":", 1)[-1]
    )
    request.addfinalizer(lambda: shutil.rmtree(run_root, ignore_errors=True))
    return loaded


def test_execution_policy_preserves_frozen_v5_v6_and_is_local_only() -> None:
    policy = collection.load_post_exhaustion_execution_policy_v1(EXECUTION_POLICY).config
    assert policy.required_families == FAMILIES
    assert policy.required_transport == "local"
    assert policy.exact_extension_tranche_ids == (
        "algebra_s6",
        "cross_domain_s6",
        "algebra_s7",
        "cross_domain_s7",
    )
    assert policy.execution_enabled
    assert not policy.semantic_labels_inspected
    assert not policy.semantic_labels_created
    assert not policy.supervision_eligible
    assert not policy.gate_5g_credit_claimed
    assert not policy.gate_5_closed
    assert hash_file(ROOT / "src/leanfaith/generation/research_collection_v5.py") == (
        "720f23b7f0ae3c528b9d95cc8e7580421f366d2b7044d74c7f7fd98e0403c56b"
    )
    assert hash_file(ROOT / "src/leanfaith/generation/research_postprocess_v6.py") == (
        "a5f3fa6ce0da674973c5fbb9d32c7fe5516d65af91b725c20eb7873410c47fc9"
    )
    assert hash_file(ROOT / "src/leanfaith/generation/post_exhaustion_collection_v1.py") == (
        "cad47d171dfa3a3b8ba11ee58800a87c4f52f774926122e9b24985761b0e0ddf"
    )


def test_prepare_and_load_replay_exact_s6_authorization(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    loaded = _prepare_loaded(repo_tmp, monkeypatch, request)
    assert loaded.config.config.tranche_id == "algebra_s6"
    assert loaded.config.config.tranche_order == 12
    assert loaded.planning_plan.problem_count == 40
    assert loaded.planning_plan.expected_candidate_count == 120
    assert tuple(item.family_id for item in loaded.planning_plan.family_bindings) == FAMILIES
    assert loaded.planning_plan.seed_count_by_family == {
        "goedel_formalizer_v2_8b": 1,
        "kimina_autoformalizer_7b": 1,
        "stepfun_formalizer_7b": 1,
    }
    assert all(not item.private_source_content for item in loaded.problems)
    assert all(item.reference_theorem_ids for item in loaded.problems)
    assert loaded.preflight.execution_ready
    assert loaded.preflight.remote_provider_requests_created == 0
    assert loaded.preflight.private_source_records_used == 0
    assert not list(repo_tmp.rglob("manifest.json"))


def test_tampered_execution_config_fails_content_identity(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    loaded = _prepare_loaded(repo_tmp, monkeypatch, request)
    payload = loaded.config.config.model_dump(mode="json")
    payload["tranche_order"] = 13
    with pytest.raises(ValueError):
        collection.ExecutablePostExhaustionCollectionConfigV6.model_validate(payload)


def test_append_only_failure_run_resumes_without_model_or_gpu(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    loaded = _prepare_loaded(repo_tmp, monkeypatch, request)
    executor = _AlwaysFailBeforeBoundary()
    fixed = datetime.datetime(2026, 7, 24, 12, tzinfo=datetime.UTC)
    first = collection.execute_post_exhaustion_collection_v6(
        loaded,
        repo_root=ROOT,
        executor=executor,
        clock=lambda: fixed,
    )
    assert executor.begin_calls == 3
    assert first.manifest.status_counts == {"orchestration_failed": 120}
    assert not first.manifest.semantic_labels_inspected
    assert not first.manifest.semantic_labels_created
    assert not first.manifest.supervision_eligible
    assert not first.manifest.gate_5g_credit_claimed
    assert not first.manifest.gate_5_closed

    replay = collection.execute_post_exhaustion_collection_v6(
        loaded,
        repo_root=ROOT,
        executor=cast(collection.PostExhaustionInvocationExecutorV6, _MustNotRun()),
        clock=lambda: fixed,
    )
    assert replay.manifest == first.manifest
    assert (
        collection.verify_post_exhaustion_collection_v6(
            loaded,
            repo_root=ROOT,
        )
        == first.manifest
    )


def test_postprocess_v7_handles_collection_failures_without_lean(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    loaded = _prepare_loaded(repo_tmp, monkeypatch, request)
    fixed = datetime.datetime(2026, 7, 24, 12, tzinfo=datetime.UTC)
    run = collection.execute_post_exhaustion_collection_v6(
        loaded,
        repo_root=ROOT,
        executor=_AlwaysFailBeforeBoundary(),
        clock=lambda: fixed,
    )
    bound = postprocess.load_post_exhaustion_postprocess_v7(
        repo_root=ROOT,
        collection_root=run.output_directory,
        collection_config_path=loaded.config.path,
    )
    processed = postprocess.run_post_exhaustion_postprocess_v7(
        bound,
        backend=cast(LeanInteractBackend, object()),
    )
    assert processed.manifest.terminal_invocations == 120
    assert processed.manifest.admitted_nl_lean_count == 0
    assert processed.manifest.status_counts == {"collection_not_raw": 120}
    assert not processed.manifest.semantic_labels_inspected
    assert not processed.manifest.semantic_labels_created
    assert not processed.manifest.supervision_eligible
    assert postprocess.verify_post_exhaustion_postprocess_v7(bound) == processed.manifest


def test_postprocess_v7_consumes_raw_collector_v6_and_replays_without_labels_or_gates(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    loaded = _prepare_loaded(repo_tmp, monkeypatch, request)
    fixed = datetime.datetime(2026, 7, 24, 12, tzinfo=datetime.UTC)
    run = collection.execute_post_exhaustion_collection_v6(
        loaded,
        repo_root=ROOT,
        executor=collection.LocalHFResearchExecutor(
            clock=lambda: fixed,
            monotonic_clock=lambda: 1.0,
            loader=_FixtureLoader(),
            generator=_FixtureGenerator(),
        ),
        clock=lambda: fixed,
    )
    assert run.manifest.status_counts == {"raw_collected": 120}
    assert run.manifest.semantic_labels_inspected is False
    assert run.manifest.semantic_labels_created is False
    assert run.manifest.supervision_eligible is False
    assert run.manifest.gate_5g_credit_claimed is False
    assert run.manifest.gate_5_closed is False

    bound = postprocess.load_post_exhaustion_postprocess_v7(
        repo_root=ROOT,
        collection_root=run.output_directory,
        collection_config_path=loaded.config.path,
    )
    _patch_fixture_representation_builder(monkeypatch)
    backend = _FixtureLeanBackend()
    processed = postprocess.run_post_exhaustion_postprocess_v7(
        bound,
        backend=cast(LeanInteractBackend, backend),
    )

    assert processed.manifest.terminal_invocations == 120
    assert processed.manifest.admitted_nl_lean_count == 1
    assert processed.manifest.status_counts == {
        "admitted_unresolved": 1,
        "screen_rejected": 119,
    }
    assert backend.requests
    for record in processed.terminals + processed.family_reports + (processed.manifest,):
        assert record.semantic_labels_inspected is False
        assert record.semantic_labels_created is False
        assert record.supervision_eligible is False
        assert record.gate_5g_credit_claimed is False
        assert record.gate_5_closed is False

    before = {
        path.relative_to(processed.output_root): hash_file(path)
        for path in sorted(processed.output_root.rglob("*"))
        if path.is_file()
    }
    replay = postprocess.verify_post_exhaustion_postprocess_v7(bound)
    after = {
        path.relative_to(processed.output_root): hash_file(path)
        for path in sorted(processed.output_root.rglob("*"))
        if path.is_file()
    }
    assert replay == processed.manifest
    assert after == before


def test_v7_observation_is_consumable_and_gate_replays_exact_execution_lineage(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    loaded = _prepare_loaded(repo_tmp, monkeypatch, request)
    run = collection.execute_post_exhaustion_collection_v6(
        loaded,
        repo_root=ROOT,
        executor=_AlwaysFailBeforeBoundary(),
        clock=lambda: datetime.datetime(2026, 7, 24, 12, tzinfo=datetime.UTC),
    )
    bound = postprocess.load_post_exhaustion_postprocess_v7(
        repo_root=ROOT,
        collection_root=run.output_directory,
        collection_config_path=loaded.config.path,
    )
    processed = postprocess.run_post_exhaustion_postprocess_v7(
        bound,
        backend=cast(LeanInteractBackend, object()),
    )
    extension_policy = extension.load_post_exhaustion_extension_policy(
        ROOT / "configs/generation/lf021_post_exhaustion_extension_v1.yaml"
    )
    base_policy = tranche.load_tranche_expansion_policy(
        ROOT / extension_policy.config.base_v1_policy.artifact
    ).config
    observation = tranche.load_postprocess_observation(
        repo_root=ROOT,
        policy=base_policy,
        tranche=extension_policy.config.extension_tranches[0],
        manifest_path=processed.manifest_path,
    )
    assert observation.binding.manifest_id == processed.manifest.manifest_id
    assert observation.binding.postprocess_schema_version == 7
    assert observation.candidates == ()

    verified = SimpleNamespace(
        collection_authorizations=SimpleNamespace(
            records=(loaded.authorization,),
            bindings=(loaded.config.config.authorization,),
        ),
        verified_stop=SimpleNamespace(
            decision=SimpleNamespace(extension_observations=(observation.binding,))
        ),
    )
    extended_gate5g._verify_authorizations_are_local(
        RepoPaths(ROOT),
        cast(Any, verified),
    )


def test_post_boundary_resume_fails_before_loading_a_model(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    loaded = _prepare_loaded(repo_tmp, monkeypatch, request)
    fixed = datetime.datetime(2026, 7, 24, 12, tzinfo=datetime.UTC)
    with pytest.raises(
        collection_v1.ResearchCollectionPostBoundaryError,
        match="model-attempt boundary",
    ):
        collection.execute_post_exhaustion_collection_v6(
            loaded,
            repo_root=ROOT,
            executor=_FailAfterBoundary(),
            clock=lambda: fixed,
        )
    with pytest.raises(
        collection_v1.ResearchCollectionPostBoundaryError,
        match="automatic resume is forbidden",
    ):
        collection.execute_post_exhaustion_collection_v6(
            loaded,
            repo_root=ROOT,
            executor=cast(collection.PostExhaustionInvocationExecutorV6, _MustNotRun()),
            clock=lambda: fixed,
        )


def test_symlinked_output_component_is_rejected(repo_tmp: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = repo_tmp / "linked-output"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(
        collection.PostExhaustionCollectionV6ArtifactConflict,
        match="symlink component",
    ):
        collection._require_repo_path_without_symlinks(
            repo_root=ROOT,
            path=link / "manifest.json",
            label="test output",
        )


def test_verify_only_imports_with_cuda_hidden() -> None:
    environment = dict(os.environ)
    environment["CUDA_VISIBLE_DEVICES"] = ""
    environment["NVIDIA_VISIBLE_DEVICES"] = "void"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from leanfaith.generation.post_exhaustion_collection_v6 "
                "import load_post_exhaustion_execution_policy_v1; "
                "p=load_post_exhaustion_execution_policy_v1("
                "Path('configs/generation/lf021_post_exhaustion_execution_v1.yaml')); "
                "assert p.config.required_transport == 'local'"
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_collection_manifest_tamper_is_rejected(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    loaded = _prepare_loaded(repo_tmp, monkeypatch, request)
    fixed = datetime.datetime(2026, 7, 24, 12, tzinfo=datetime.UTC)
    run = collection.execute_post_exhaustion_collection_v6(
        loaded,
        repo_root=ROOT,
        executor=_AlwaysFailBeforeBoundary(),
        clock=lambda: fixed,
    )
    payload = run.manifest.model_dump(mode="json")
    payload["status_counts"] = {"orchestration_failed": 119, "raw_collected": 1}
    with pytest.raises(ValueError, match="manifest ID differs"):
        collection.PostExhaustionCollectionManifestV6.model_validate(payload)


def test_no_remote_or_semantic_api_surface() -> None:
    assert not hasattr(collection, "RemoteResearchExecutor")
    assert not hasattr(collection, "create_semantic_label")
    assert not hasattr(postprocess, "resolve_same_claim")
    assert collection.LocalHFResearchExecutor is collection_v1.LocalHFResearchExecutor
