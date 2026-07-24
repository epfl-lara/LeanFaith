"""Write-once accounting for LF-021 launcher failures before model execution."""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation.invocation_failure import (
    InvocationCheckpointBinding,
    InvocationCodeBundleBinding,
    InvocationFailurePersistenceError,
    LocalQualificationInvocationFailure,
    LocalQualificationInvocationStage,
    persist_invocation_failure,
)

UTC = datetime.datetime(2026, 7, 23, 22, 30, tzinfo=datetime.UTC)
REVISION = "f" * 40


class _LegacyProviderDecoding(BaseModel):
    """Shape that caused the observed pre-provider Goedel failure."""

    model_config = ConfigDict(strict=True)
    decoding: dict[str, str | int | float | bool | None]


def _goedel_decoding_validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        _LegacyProviderDecoding.model_validate({"decoding": {"eos_token_id": (151_645, 151_643)}})
    return caught.value


def _record(
    exception: BaseException,
    *,
    checkpoint: bool = True,
    code_bundle: bool = True,
    model_execution_started: bool = False,
) -> LocalQualificationInvocationFailure:
    return LocalQualificationInvocationFailure.create(
        stage=LocalQualificationInvocationStage.QUALIFICATION_PRE_PROVIDER,
        exception=exception,
        invoked_at=UTC,
        failed_at=UTC + datetime.timedelta(seconds=2),
        qualification_config_id="lf021_local_qualification_goedel_v1",
        qualification_config_artifact="configs/generation/local_qualification_goedel_v1.yaml",
        qualification_config_file_sha256="1" * 64,
        qualification_config_hash="2" * 64,
        model_family="goedel_formalizer_v2_8b",
        model_repo_id="Goedel-LM/Goedel-Formalizer-V2-8B",
        model_revision=REVISION,
        provider_slot="local_goedel_qualification",
        checkpoint_binding=(
            InvocationCheckpointBinding(
                verification_hash="3" * 64,
                model_repo_id="Goedel-LM/Goedel-Formalizer-V2-8B",
                model_revision=REVISION,
                snapshot_reference=("hf-cache://Goedel-LM/Goedel-Formalizer-V2-8B@" + REVISION),
                checkpoint_bytes=123,
            )
            if checkpoint
            else None
        ),
        code_bundle_binding=(
            InvocationCodeBundleBinding(
                source_artifact="runs/example/code_bundle/example.tar.gz",
                sha256="4" * 64,
                code_tree_hash="5" * 64,
            )
            if code_bundle
            else None
        ),
        model_execution_started=model_execution_started,
    )


def _load_launcher() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "07_qualify_local_kimina.py"
    spec = importlib.util.spec_from_file_location("lf021_qualification_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_observed_goedel_pre_provider_schema_failure_is_diagnostic_not_attempt(
    tmp_path: Path,
) -> None:
    record = _record(_goedel_decoding_validation_error())
    path, digest = persist_invocation_failure(
        record,
        run_directory=tmp_path / "runs" / "goedel",
        artifact_root=tmp_path,
    )

    assert hash_file(path) == digest
    raw = path.read_bytes()
    document = json.loads(raw)
    assert raw == canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
    assert document["stage"] == "qualification_pre_provider"
    assert document["exception_type"] == "pydantic_core._pydantic_core.ValidationError"
    assert document["model_execution_started"] is False
    assert document["counts_as_provider_request"] is False
    assert document["counts_as_llm_attempt"] is False
    assert document["counts_as_semantic_or_model_attempt"] is False
    assert "provider_request_id" not in document
    assert "llm_call_id" not in document
    assert "llm_attempt_id" not in document
    for field in (
        "qualifies_for_gate5g",
        "semantic_labels_created",
        "semantic_pool_eligible",
        "supervision_eligible",
        "training_eligible",
        "release_eligible",
        "calibration_eligible",
        "model_selection_eligible",
        "scientific_evaluation_eligible",
        "scientific_table_eligible",
    ):
        assert document[field] is False


def test_exception_message_redacts_environment_and_token_shaped_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "hf_thisMustNeverBePersisted123456"
    api_key = "sk-thisMustAlsoNeverBePersisted123456"
    monkeypatch.setenv("HF_TOKEN", secret)
    record = _record(RuntimeError(f"failed with {secret} and bearer {api_key}"))
    path, _ = persist_invocation_failure(
        record,
        run_directory=tmp_path / "run",
        artifact_root=tmp_path,
    )

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    assert api_key not in raw
    assert raw.count("[REDACTED]") >= 2


def test_invocation_failure_file_is_canonical_idempotent_and_write_once(
    tmp_path: Path,
) -> None:
    record = _record(_goedel_decoding_validation_error())
    first = persist_invocation_failure(
        record,
        run_directory=tmp_path / "run",
        artifact_root=tmp_path,
    )
    second = persist_invocation_failure(
        record,
        run_directory=tmp_path / "run",
        artifact_root=tmp_path,
    )
    assert first == second
    assert (
        LocalQualificationInvocationFailure.model_validate_json(
            first[0].read_text(encoding="utf-8")
        )
        == record
    )

    changed = _record(RuntimeError("different failure"))
    with pytest.raises(
        InvocationFailurePersistenceError,
        match="immutable invocation-failure record conflict",
    ):
        persist_invocation_failure(
            changed,
            run_directory=tmp_path / "run",
            artifact_root=tmp_path,
        )


def test_launcher_persists_pre_provider_failure_and_reraises_same_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the launcher boundary without importing torch or running a GPU."""

    launcher = _load_launcher()
    config_path = tmp_path / "config.yaml"
    fixture_path = tmp_path / "fixture.json"
    header_path = tmp_path / "header.lean"
    for path, content in (
        (config_path, "config: fixture\n"),
        (fixture_path, "{}\n"),
        (header_path, "import Mathlib\n"),
    ):
        path.write_text(content, encoding="utf-8")
    run_directory = tmp_path / "runs" / "goedel"

    model = SimpleNamespace(
        family_id="goedel_formalizer_v2_8b",
        repo_id="Goedel-LM/Goedel-Formalizer-V2-8B",
        revision=REVISION,
        provider_slot="local_goedel_qualification",
    )
    loaded = SimpleNamespace(
        config=SimpleNamespace(
            config_id="lf021_local_qualification_goedel_v1",
            active_model=model,
            qualification_fixture=None,
        ),
        config_hash="2" * 64,
    )
    fixture = SimpleNamespace(
        resolved_project_registry_key="fixtures",
        imports="import Mathlib",
        fixture_id="fixture",
        resolved_generated_declaration_name="generated",
    )
    context = SimpleNamespace(
        context_fingerprint="6" * 64,
        environment_schema_version=1,
    )
    active = SimpleNamespace(
        manifest_path=tmp_path / "manifest.json",
        active_registry_path=tmp_path / "active.json",
        detailed_index_path=tmp_path / "detailed.json",
        input_manifest_path=tmp_path / "input.json",
    )
    checkpoint = SimpleNamespace(
        verification_hash="3" * 64,
        model_repo_id=model.repo_id,
        model_revision=model.revision,
        snapshot_reference=f"hf-cache://{model.repo_id}@{model.revision}",
        checkpoint_bytes=123,
    )
    code_path = run_directory / "code_bundle" / "bundle.tar.gz"
    code_state = SimpleNamespace(code_tree_hash="5" * 64)
    observed = _goedel_decoding_validation_error()

    class FakeBackend:
        def __init__(self, _settings: object) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        launcher,
        "_arguments",
        lambda: argparse.Namespace(
            config=config_path,
            fixture=fixture_path,
            project_dir=tmp_path,
            project_registry_key="fixtures",
            preflight_only=False,
            output_dir=run_directory,
        ),
    )
    monkeypatch.setattr(launcher, "find_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr(launcher, "load_local_qualification_config", lambda *_a, **_k: loaded)
    monkeypatch.setattr(launcher, "_load_offline_fixture", lambda _path: fixture)
    monkeypatch.setattr(launcher, "_offline_context", lambda *_a, **_k: context)
    monkeypatch.setattr(launcher, "_offline_reference", lambda **_k: object())
    monkeypatch.setattr(launcher, "load_active_benchmark_registry", lambda **_k: active)
    monkeypatch.setattr(
        launcher.ProblemPoolDenylistBinding,
        "from_active_registry",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        launcher,
        "_qualification_fixture_header_path",
        lambda *_a, **_k: header_path,
    )
    monkeypatch.setattr(
        launcher,
        "_offline_problem",
        lambda **_k: (SimpleNamespace(), None, None),
    )
    monkeypatch.setattr(launcher, "BackendSettings", lambda **_k: object())
    monkeypatch.setattr(launcher, "LeanInteractBackend", FakeBackend)
    monkeypatch.setattr(launcher, "CandidateScreeningIndex", lambda **_k: object())
    monkeypatch.setattr(
        launcher,
        "preflight_local_qualification_fixture",
        lambda **_k: SimpleNamespace(),
    )
    monkeypatch.setattr(launcher, "_checkpoint_snapshot", lambda *_a: tmp_path)
    monkeypatch.setattr(
        launcher,
        "verify_local_checkpoint_artifacts",
        lambda *_a, **_k: checkpoint,
    )
    monkeypatch.setattr(
        launcher,
        "freeze_code_bundle",
        lambda *_a, **_k: (code_path, "4" * 64, code_state),
    )
    fake_torch = SimpleNamespace(
        __version__="test",
        cuda=SimpleNamespace(get_device_name=lambda _index: "no-gpu-used"),
    )
    fake_transformers = SimpleNamespace(__version__="test")
    monkeypatch.setattr(
        launcher.importlib,
        "import_module",
        lambda name: fake_torch if name == "torch" else fake_transformers,
    )
    monkeypatch.setattr(launcher, "make_runtime_binding", lambda **_k: object())
    monkeypatch.setattr(launcher, "build_local_qualification_formatter", lambda _c: object())
    monkeypatch.setattr(launcher, "TransformersLocalLoader", lambda: object())
    monkeypatch.setattr(launcher, "TransformersCausalGenerator", lambda: object())
    monkeypatch.setattr(launcher, "LocalHFSequentialRuntime", lambda **_k: object())

    def fail_before_provider(**_kwargs: object) -> None:
        raise observed

    monkeypatch.setattr(launcher, "run_local_qualification", fail_before_provider)

    with pytest.raises(ValidationError) as reraised:
        launcher.main()
    assert reraised.value is observed

    failure_path = run_directory / "invocation_failure.json"
    record = LocalQualificationInvocationFailure.model_validate_json(
        failure_path.read_text(encoding="utf-8")
    )
    assert record.stage is LocalQualificationInvocationStage.QUALIFICATION_PRE_PROVIDER
    assert record.model_execution_started is False
    assert record.checkpoint_binding is not None
    assert record.code_bundle_binding is not None
    assert not (run_directory / "provider_request.json").exists()
    assert not (run_directory / "attempt.json").exists()
    assert not (run_directory / "terminal.json").exists()


def test_runtime_wrapper_marks_model_execution_before_delegation() -> None:
    launcher = _load_launcher()
    state = launcher._InvocationState(
        stage=LocalQualificationInvocationStage.QUALIFICATION_PRE_PROVIDER
    )
    observed = RuntimeError("delegate failed")

    class FailingRuntime:
        def generate(self, _request: object) -> None:
            raise observed

    tracked = launcher._TrackedRuntime(delegate=FailingRuntime(), state=state)
    with pytest.raises(RuntimeError) as reraised:
        tracked.generate(object())
    assert reraised.value is observed
    assert state.model_execution_started is True
    assert state.stage is LocalQualificationInvocationStage.MODEL_EXECUTION
