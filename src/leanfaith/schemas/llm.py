"""LLM call and retry-attempt records (PLAN.md §11.11, §17.4).

Every provider call is persisted with exact provenance. Raw output is stored
as an artifact pointer before parsing; parse failures and every retry attempt
are retained (§17.5). Schema-v1 call records remain readable. New real-output
collection writes schema v2 and links append-only ``LLMAttemptRecord`` items.
"""

from __future__ import annotations

import datetime
import re
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import (
    LLMAttemptStatus,
    LLMCallStatus,
    LLMRole,
    ParseStatus,
)
from leanfaith.schemas.ids import (
    HEX64_PATTERN,
    LLM_ATTEMPT_PREFIX,
    LLM_CALL_PREFIX,
    PROBLEM_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.manifest import require_utc

MetadataScalar = str | int | float | bool | None
MetadataValue = MetadataScalar | tuple[MetadataScalar, ...]
LLMExecutionMode = Literal["external", "local", "replay"]


def make_llm_call_id(
    *,
    provider: str,
    provider_slot: str,
    model: str,
    model_family: str,
    model_revision: str,
    role: LLMRole,
    problem_record_id: str | None,
    prompt_template_hash: str,
    prompt_render_hash: str,
    input_ids: tuple[str, ...],
    decoding: dict[str, MetadataValue],
) -> str:
    """Build the semantic ID of one logical schema-v2 provider request."""

    return make_id(
        LLM_CALL_PREFIX,
        {
            "schema": "llm_call_v2",
            "provider": provider,
            "provider_slot": provider_slot,
            "model": model,
            "model_family": model_family,
            "model_revision": model_revision,
            "role": role.value,
            "problem_record_id": problem_record_id,
            "prompt_template_hash": prompt_template_hash,
            "prompt_render_hash": prompt_render_hash,
            "input_ids": input_ids,
            "decoding": decoding,
        },
    )


def make_llm_attempt_id(call_id: str, attempt_index: int) -> str:
    """Build the deterministic ID of one append-only attempt."""

    if attempt_index < 0:
        raise ValueError("attempt_index must be nonnegative")
    return make_id(
        LLM_ATTEMPT_PREFIX,
        {
            "schema": "llm_attempt_v1",
            "call_id": call_id,
            "attempt_index": attempt_index,
        },
    )


class LLMAttemptRecord(StrictModel):
    """One provider attempt, including failures that produced no response."""

    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=id_pattern(LLM_ATTEMPT_PREFIX))
    call_id: str = Field(pattern=id_pattern(LLM_CALL_PREFIX))
    attempt_index: int = Field(ge=0, strict=True)
    execution_mode: LLMExecutionMode
    started_at: datetime.datetime
    completed_at: datetime.datetime
    request_artifact: str = Field(min_length=1)
    raw_response_artifact: str | None = None
    status: LLMAttemptStatus
    provider_request_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    retryable: bool
    latency_ms: int = Field(ge=0, strict=True)
    tokens: dict[str, int] = Field(default_factory=dict)
    provider_request_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    provider_attempt_id: str | None = Field(
        default=None,
        pattern=r"^provider-attempt:[0-9a-f]{64}$",
    )
    request_artifact_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    raw_response_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> LLMAttemptRecord:
        require_utc(self.started_at)
        require_utc(self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("attempt completed_at cannot precede started_at")
        expected_id = make_llm_attempt_id(self.call_id, self.attempt_index)
        if self.attempt_id != expected_id:
            raise ValueError("attempt_id does not match call_id and attempt_index")
        if any(count < 0 for count in self.tokens.values()):
            raise ValueError("attempt token counts must be nonnegative")
        provider_bindings = (
            self.provider_request_hash,
            self.provider_attempt_id,
            self.request_artifact_sha256,
            self.raw_response_sha256,
        )
        if any(value is not None for value in provider_bindings) and any(
            value is None for value in provider_bindings
        ):
            raise ValueError(
                "provider-bound attempts require request hash, provider attempt ID, "
                "request artifact SHA-256, and raw response SHA-256 together"
            )

        response_statuses = {
            LLMAttemptStatus.RESPONSE_RECEIVED,
            LLMAttemptStatus.EMPTY_RESPONSE,
        }
        if self.status in response_statuses:
            if self.raw_response_artifact is None:
                raise ValueError(f"{self.status} requires raw_response_artifact")
            if self.status == LLMAttemptStatus.RESPONSE_RECEIVED:
                if self.error_code is not None or self.error_detail is not None:
                    raise ValueError("response_received cannot carry provider error fields")
                if self.retryable:
                    raise ValueError("response_received is terminal and cannot be retryable")
        else:
            if self.error_code is None:
                raise ValueError(f"{self.status} requires error_code")
        return self


class LLMCallRecord(StrictModel):
    """One provider call with full provenance (§11.11)."""

    schema_version: Literal[1, 2] = 1
    call_id: str = Field(pattern=id_pattern(LLM_CALL_PREFIX))
    provider: str
    model: str
    model_family: str
    role: LLMRole
    model_revision: str | None = None
    request_date: datetime.datetime
    prompt_template_hash: str = Field(pattern=HEX64_PATTERN)
    prompt_render_hash: str = Field(pattern=HEX64_PATTERN)
    input_ids: tuple[str, ...] = ()
    decoding: dict[str, MetadataValue] = Field(default_factory=dict)
    raw_output_artifact: str | None = None
    parsed_output: dict[str, object] | None = None
    parse_status: ParseStatus
    retry_count: int = Field(default=0, ge=0)
    tokens: dict[str, int] = Field(default_factory=dict)
    provider_cost: float | None = None
    supervision_eligible: bool
    private_source_content: bool
    external_api_approval: str | None = None
    denylist_checked: bool
    denylist_hits: tuple[str, ...] = ()
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)
    # Schema-v2 real-output lineage. Optional defaults keep v1 records readable.
    provider_slot: str | None = Field(default=None, min_length=1)
    execution_mode: LLMExecutionMode | None = None
    problem_record_id: str | None = Field(default=None, pattern=id_pattern(PROBLEM_PREFIX))
    problem_id: str | None = Field(default=None, min_length=1)
    problem_group: str | None = Field(default=None, min_length=1)
    prompt_template_id: str | None = Field(default=None, min_length=1)
    prompt_template_version: str | None = Field(default=None, min_length=1)
    request_artifact: str | None = Field(default=None, min_length=1)
    started_at: datetime.datetime | None = None
    completed_at: datetime.datetime | None = None
    terminal_status: LLMCallStatus | None = None
    attempt_ids: tuple[str, ...] = ()
    latency_ms: int | None = Field(default=None, ge=0, strict=True)
    heldout_generator: bool = False
    provider_request_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    request_artifact_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    raw_response_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _checks(self) -> LLMCallRecord:
        require_utc(self.request_date)
        if self.parse_status == ParseStatus.PARSED and self.parsed_output is None:
            raise ValueError("parse_status=parsed requires parsed_output")
        if self.parse_status != ParseStatus.PARSED and self.parsed_output is not None:
            raise ValueError(f"parse_status={self.parse_status} cannot carry parsed_output")
        if (
            self.schema_version == 1
            and self.private_source_content
            and self.external_api_approval is None
        ):
            raise ValueError(
                "calls containing private-source content require a recorded §9.2 "
                "approval reference (ADR or manifest flag)"
            )
        if self.role == LLMRole.PRIMARY_EVAL_JUDGE and self.supervision_eligible:
            raise ValueError(
                "primary_eval_judge output is excluded from all training supervision (§17.2)"
            )
        if self.heldout_generator and self.supervision_eligible:
            raise ValueError("held-out generator calls cannot be supervision eligible")
        if self.schema_version == 1:
            return self

        provider_bindings = (
            self.provider_request_hash,
            self.request_artifact_sha256,
            self.raw_response_sha256,
        )
        if any(value is not None for value in provider_bindings) and any(
            value is None for value in provider_bindings
        ):
            raise ValueError(
                "provider-bound schema-v2 calls require provider request, request "
                "artifact, and raw response hashes together"
            )

        required_v2 = {
            "provider_slot": self.provider_slot,
            "execution_mode": self.execution_mode,
            "model_revision": self.model_revision,
            "prompt_template_id": self.prompt_template_id,
            "prompt_template_version": self.prompt_template_version,
            "request_artifact": self.request_artifact,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "terminal_status": self.terminal_status,
            "latency_ms": self.latency_ms,
        }
        missing = sorted(name for name, value in required_v2.items() if value is None)
        if missing:
            raise ValueError("schema-v2 LLM calls require: " + ", ".join(missing))
        if not self.attempt_ids:
            raise ValueError("schema-v2 LLM calls require at least one attempt_id")
        if len(self.attempt_ids) != len(set(self.attempt_ids)):
            raise ValueError("attempt_ids must be unique and preserve attempt order")
        if any(
            re.fullmatch(id_pattern(LLM_ATTEMPT_PREFIX), attempt_id) is None
            for attempt_id in self.attempt_ids
        ):
            raise ValueError("attempt_ids must all be 'call_attempt:' IDs")
        if self.retry_count != len(self.attempt_ids) - 1:
            raise ValueError("retry_count must equal len(attempt_ids) - 1")

        assert self.started_at is not None
        assert self.completed_at is not None
        require_utc(self.started_at)
        require_utc(self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("call completed_at cannot precede started_at")
        if self.request_date != self.started_at:
            raise ValueError("schema-v2 request_date must equal started_at")

        if self.role == LLMRole.AUTOFORMALIZER:
            required_problem = {
                "problem_record_id": self.problem_record_id,
                "problem_id": self.problem_id,
                "problem_group": self.problem_group,
            }
            missing_problem = sorted(
                name for name, value in required_problem.items() if value is None
            )
            if missing_problem:
                raise ValueError(
                    "schema-v2 autoformalizer calls require: " + ", ".join(missing_problem)
                )
            if self.terminal_status == LLMCallStatus.COMPLETED and any(
                value is None for value in provider_bindings
            ):
                raise ValueError(
                    "completed schema-v2 autoformalizer calls require provider request, "
                    "request artifact, and raw response hash bindings"
                )
        if self.private_source_content and self.execution_mode == "external":
            raise ValueError(
                "Revision 4.1 forbids external-provider transmission of private-source content"
            )

        assert self.provider_slot is not None
        assert self.model_revision is not None
        expected_call_id = make_llm_call_id(
            provider=self.provider,
            provider_slot=self.provider_slot,
            model=self.model,
            model_family=self.model_family,
            model_revision=self.model_revision,
            role=self.role,
            problem_record_id=self.problem_record_id,
            prompt_template_hash=self.prompt_template_hash,
            prompt_render_hash=self.prompt_render_hash,
            input_ids=self.input_ids,
            decoding=self.decoding,
        )
        if self.call_id != expected_call_id:
            raise ValueError("schema-v2 call_id does not match the logical request payload")

        if self.terminal_status == LLMCallStatus.COMPLETED:
            if self.raw_output_artifact is None:
                raise ValueError("completed schema-v2 calls require raw_output_artifact")
        else:
            if self.parsed_output is not None or self.parse_status != ParseStatus.EMPTY:
                raise ValueError(
                    "non-completed schema-v2 calls cannot carry parsed output and "
                    "must use parse_status=empty"
                )
        return self


def check_llm_call_attempt_lineage(
    call: LLMCallRecord,
    attempts: tuple[LLMAttemptRecord, ...],
) -> list[str]:
    """Return cross-record violations for one schema-v2 logical call."""

    if call.schema_version != 2:
        return ["call_schema_version_not_v2"]
    violations: list[str] = []
    if tuple(attempt.attempt_id for attempt in attempts) != call.attempt_ids:
        violations.append("attempt_ids_do_not_match_ordered_attempts")
    if tuple(attempt.attempt_index for attempt in attempts) != tuple(range(len(attempts))):
        violations.append("attempt_indices_not_contiguous")
    if any(attempt.call_id != call.call_id for attempt in attempts):
        violations.append("attempt_call_id_mismatch")
    for attempt in attempts:
        if (
            call.provider_request_hash is not None
            and attempt.provider_request_hash != call.provider_request_hash
        ):
            violations.append("provider_request_hash_mismatch")
    if attempts:
        first, final = attempts[0], attempts[-1]
        if (
            call.request_artifact_sha256 is not None
            and final.request_artifact_sha256 != call.request_artifact_sha256
        ):
            # Retry attempts have distinct append-only ProviderRequest bytes
            # because attempt_index/provider_attempt_id differ.  The logical
            # call binds the final request artifact while provider_request_hash
            # above remains stable across all attempts.
            violations.append("final_request_artifact_sha256_mismatch")
        if call.started_at is not None and first.started_at < call.started_at:
            violations.append("first_attempt_precedes_call")
        if call.completed_at is not None and final.completed_at > call.completed_at:
            violations.append("final_attempt_exceeds_call")
        if call.terminal_status == LLMCallStatus.COMPLETED:
            if final.status is not LLMAttemptStatus.RESPONSE_RECEIVED:
                violations.append("completed_call_final_attempt_has_no_response")
            if call.raw_output_artifact != final.raw_response_artifact:
                violations.append("call_raw_output_does_not_match_final_attempt")
            if (
                call.raw_response_sha256 is not None
                and final.raw_response_sha256 != call.raw_response_sha256
            ):
                violations.append("raw_response_sha256_mismatch")
        elif final.status is LLMAttemptStatus.RESPONSE_RECEIVED:
            violations.append("noncompleted_call_final_attempt_has_response")
    return violations
