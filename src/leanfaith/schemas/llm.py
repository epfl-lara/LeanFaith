"""LLM call record (PLAN.md §11.11).

Every provider call is persisted with exact provenance. Raw output is stored
as an artifact pointer before parsing; parse failures are retained (§17.5).
"""

from __future__ import annotations

import datetime

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import LLMRole, ParseStatus
from leanfaith.schemas.ids import HEX64_PATTERN, LLM_CALL_PREFIX, id_pattern
from leanfaith.schemas.manifest import require_utc

MetadataValue = str | int | float | bool | None


class LLMCallRecord(StrictModel):
    """One provider call with full provenance (§11.11)."""

    schema_version: int = 1
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

    @model_validator(mode="after")
    def _checks(self) -> LLMCallRecord:
        require_utc(self.request_date)
        if self.parse_status == ParseStatus.PARSED and self.parsed_output is None:
            raise ValueError("parse_status=parsed requires parsed_output")
        if self.parse_status != ParseStatus.PARSED and self.parsed_output is not None:
            raise ValueError(f"parse_status={self.parse_status} cannot carry parsed_output")
        if self.private_source_content and self.external_api_approval is None:
            raise ValueError(
                "calls containing private-source content require a recorded §9.2 "
                "approval reference (ADR or manifest flag)"
            )
        if self.role == LLMRole.PRIMARY_EVAL_JUDGE and self.supervision_eligible:
            raise ValueError(
                "primary_eval_judge output is excluded from all training supervision (§17.2)"
            )
        return self
