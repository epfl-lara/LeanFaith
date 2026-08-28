"""Content-addressed provenance for one exact E2 infrastructure recovery."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel

_HEX64 = r"^[0-9a-f]{64}$"


class V2E2RecoverySpec(StrictModel):
    """Immutable authorization to retry exactly one failed E2 attempt."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_v2_e2_recovery_spec"] = (
        "deterministic_v2_e2_recovery_spec"
    )
    recovery_spec_id: str = Field(pattern=r"^v2e2_recovery_spec:[0-9a-f]{64}$")
    parent_root_path: str = Field(min_length=1)
    parent_root_file_count: int = Field(ge=4)
    parent_root_tree_hash: str = Field(pattern=_HEX64)
    parent_run_spec_sha256: str = Field(pattern=_HEX64)
    parent_manifest_sha256: str = Field(pattern=_HEX64)
    parent_results_sha256: str = Field(pattern=_HEX64)
    parent_journal_tree_hash: str = Field(pattern=_HEX64)
    target_result_id: str = Field(pattern=r"^v2e2_result:[0-9a-f]{64}$")
    target_attempt_id: str = Field(pattern=r"^attempt:[0-9a-f]{64}$")
    target_draft_id: str = Field(pattern=r"^draft:[0-9a-f]{64}$")
    target_source_theorem_id: str = Field(min_length=1)
    target_source_representation_id: str = Field(min_length=1)
    target_result_line_number: int = Field(ge=1)
    target_batch_index: int = Field(ge=0)
    target_batch_line_number: int = Field(ge=1)
    profile_id: str = Field(min_length=1)
    profile_config_hash: str = Field(pattern=_HEX64)
    candidate_timeout_seconds: float = Field(default=600.0, gt=0)
    infrastructure_max_attempts: Literal[2] = 2
    retry_statuses: tuple[Literal["crash", "internal_error", "timeout"], ...] = (
        "crash",
        "internal_error",
        "timeout",
    )
    fresh_session_between_infrastructure_attempts: Literal[True] = True
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _self_authenticating(self) -> V2E2RecoverySpec:
        if self.retry_statuses != ("crash", "internal_error", "timeout"):
            raise ValueError("recovery retry statuses are not canonical")
        if self.candidate_timeout_seconds != 600.0:
            raise ValueError("recovery candidate timeout must be exactly 600 seconds")
        payload = self.model_dump(mode="json")
        payload.pop("recovery_spec_id")
        expected = f"v2e2_recovery_spec:{hash_canonical(payload)}"
        if self.recovery_spec_id != expected:
            raise ValueError("recovery_spec_id does not match its payload")
        return self


class RecoveryLeanAttempt(StrictModel):
    """One append-only Lean response used by the recovery decision."""

    attempt_index: int = Field(ge=0)
    request_id: str = Field(min_length=1)
    status: Literal[
        "valid",
        "valid_with_sorry",
        "invalid",
        "timeout",
        "crash",
        "setup_error",
        "unsupported",
        "internal_error",
    ]
    request_hash: str = Field(pattern=_HEX64)
    context_id: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    allow_sorry: bool
    transport_isolation_attempt: str | None = Field(default=None, min_length=1)
    raw_response_relative_path: str = Field(min_length=1)
    raw_response_sha256: str = Field(pattern=_HEX64)


class RecoveryPipelineAttempt(RecoveryLeanAttempt):
    """One ordered candidate or representation request made by recovery."""

    sequence_index: int = Field(ge=0)
    stage: Literal["candidate_validation", "candidate_representation"]


class V2E2RecoveryReceipt(StrictModel):
    """Proof that one line, and only one line, changed in a recovered root."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_v2_e2_recovery_receipt"] = (
        "deterministic_v2_e2_recovery_receipt"
    )
    recovery_receipt_id: str = Field(pattern=r"^v2e2_recovery_receipt:[0-9a-f]{64}$")
    recovery_spec_id: str = Field(pattern=r"^v2e2_recovery_spec:[0-9a-f]{64}$")
    recovery_spec_sha256: str = Field(pattern=_HEX64)
    replacement_result_id: str = Field(pattern=r"^v2e2_result:[0-9a-f]{64}$")
    replacement_terminal_status: Literal[
        "not_applicable",
        "no_output",
        "candidate_invalid",
        "candidate_representation_failed",
        "audit_quarantined",
        "provisional_variant",
    ]
    replacement_result_sha256: str = Field(pattern=_HEX64)
    lean_attempts: tuple[RecoveryLeanAttempt, ...] = Field(min_length=1, max_length=2)
    pipeline_attempts: tuple[RecoveryPipelineAttempt, ...] = Field(min_length=1)
    checked_in_toolchain: str = Field(min_length=1)
    resolved_lean_version: str = Field(min_length=1)
    resolved_lean_version_output: str = Field(min_length=1)
    output_run_spec_sha256: str = Field(pattern=_HEX64)
    output_results_sha256: str = Field(pattern=_HEX64)
    output_manifest_sha256: str = Field(pattern=_HEX64)
    output_journal_tree_hash: str = Field(pattern=_HEX64)
    output_root_file_count_without_receipt: int = Field(ge=4)
    output_root_tree_hash_without_receipt: str = Field(pattern=_HEX64)
    unchanged_result_line_count: int = Field(ge=0)
    replaced_result_line_count: Literal[1] = 1
    unchanged_journal_file_count: int = Field(ge=0)
    changed_journal_file_count: Literal[1] = 1
    parent_root_unchanged_after_recovery: Literal[True] = True
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _self_authenticating(self) -> V2E2RecoveryReceipt:
        if tuple(item.attempt_index for item in self.lean_attempts) != tuple(
            range(len(self.lean_attempts))
        ):
            raise ValueError("Lean recovery attempts must be contiguous and ordered")
        if tuple(item.sequence_index for item in self.pipeline_attempts) != tuple(
            range(len(self.pipeline_attempts))
        ):
            raise ValueError("Lean recovery pipeline attempts must be contiguous and ordered")
        candidate_pipeline = tuple(
            item for item in self.pipeline_attempts if item.stage == "candidate_validation"
        )
        if len(candidate_pipeline) != len(self.lean_attempts):
            raise ValueError("candidate attempt lineage differs from the pipeline lineage")
        for candidate, pipeline in zip(self.lean_attempts, candidate_pipeline, strict=True):
            pipeline_payload = pipeline.model_dump(mode="json")
            pipeline_payload.pop("sequence_index")
            pipeline_payload.pop("stage")
            if candidate.model_dump(mode="json") != pipeline_payload:
                raise ValueError("candidate attempt does not match its pipeline attempt")
        payload = self.model_dump(mode="json")
        payload.pop("recovery_receipt_id")
        expected = f"v2e2_recovery_receipt:{hash_canonical(payload)}"
        if self.recovery_receipt_id != expected:
            raise ValueError("recovery_receipt_id does not match its payload")
        return self


def build_recovery_spec(**data: object) -> V2E2RecoverySpec:
    placeholder = V2E2RecoverySpec.model_construct(
        _fields_set=None,
        recovery_spec_id=f"v2e2_recovery_spec:{'0' * 64}",
        **data,
    )
    payload = placeholder.model_dump(mode="json")
    payload.pop("recovery_spec_id")
    return V2E2RecoverySpec.model_validate(
        {"recovery_spec_id": f"v2e2_recovery_spec:{hash_canonical(payload)}", **data}
    )


def build_recovery_receipt(**data: object) -> V2E2RecoveryReceipt:
    normalized = dict(data)
    attempts = normalized.get("lean_attempts")
    if isinstance(attempts, list | tuple):
        normalized["lean_attempts"] = tuple(
            item
            if isinstance(item, RecoveryLeanAttempt)
            else RecoveryLeanAttempt.model_validate(item)
            for item in attempts
        )
    pipeline_attempts = normalized.get("pipeline_attempts")
    if isinstance(pipeline_attempts, list | tuple):
        normalized["pipeline_attempts"] = tuple(
            item
            if isinstance(item, RecoveryPipelineAttempt)
            else RecoveryPipelineAttempt.model_validate(item)
            for item in pipeline_attempts
        )
    placeholder = V2E2RecoveryReceipt.model_construct(
        _fields_set=None,
        recovery_receipt_id=f"v2e2_recovery_receipt:{'0' * 64}",
        **normalized,
    )
    payload = placeholder.model_dump(mode="json")
    payload.pop("recovery_receipt_id")
    return V2E2RecoveryReceipt.model_validate(
        {
            "recovery_receipt_id": f"v2e2_recovery_receipt:{hash_canonical(payload)}",
            **normalized,
        }
    )


__all__ = [
    "RecoveryLeanAttempt",
    "RecoveryPipelineAttempt",
    "V2E2RecoveryReceipt",
    "V2E2RecoverySpec",
    "build_recovery_receipt",
    "build_recovery_spec",
]
