"""Write-once accounting for LF-021 launcher failures before model execution."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

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
