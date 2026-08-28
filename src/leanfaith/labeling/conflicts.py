"""Content-addressed LF-024 conflict and precedence-override records.

These records preserve resolver decisions without creating or mutating a
``ResolvedLabel``.  Their semantic IDs intentionally exclude operational UTC
timestamps, while every other decision-bearing field is hash-bound.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import SemanticLabelTargetKind
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    LABEL_PREFIX,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.manifest import require_utc

RESOLUTION_CANDIDATE_PREFIX = "resolution_candidate"
RESOLUTION_CONFLICT_PREFIX = "resolution_conflict"
RESOLUTION_OVERRIDE_PREFIX = "resolution_override"

_CONFLICT_SCHEMA = "resolution_conflict_v1"
_OVERRIDE_SCHEMA = "resolution_override_v1"

ResolutionCandidateId = Annotated[
    str,
    Field(pattern=id_pattern(RESOLUTION_CANDIDATE_PREFIX), strict=True),
]
EvidenceId = Annotated[str, Field(pattern=id_pattern(EVIDENCE_PREFIX), strict=True)]
SourceRank = Annotated[int, Field(ge=1, strict=True)]

_TARGET_ID_PATTERNS: dict[SemanticLabelTargetKind, str] = {
    SemanticLabelTargetKind.LEAN_PAIR: id_pattern(PAIR_PREFIX),
    SemanticLabelTargetKind.NL_LEAN: id_pattern(NL_LEAN_PREFIX),
}


class ResolutionConflictReason(StrEnum):
    """Canonical strong-evidence conflict reasons from label policy v1."""

    SAME_CLAIM_DISAGREEMENT = "same_claim_disagreement"
    RESOLUTION_OUTCOME_DISAGREEMENT = "resolution_outcome_disagreement"
    RELATION_DISAGREEMENT = "relation_disagreement"
    TRUTH_A_IMPLIES_B_DISAGREEMENT = "truth_a_implies_b_disagreement"
    TRUTH_B_IMPLIES_A_DISAGREEMENT = "truth_b_implies_a_disagreement"
    MUTUALLY_INCONSISTENT_CERTIFICATES = "mutually_inconsistent_certificates"


class ResolutionOverrideReason(StrEnum):
    """Canonical non-conflicting precedence actions from label policy v1."""

    STRONG_OVER_WEAK = "strong_over_weak"
    WEAK_OVER_WEAK = "weak_over_weak"
    STRONG_OVER_STRONG_AGREEING = "strong_over_strong_agreeing"


def _validate_target(target_kind: SemanticLabelTargetKind, target_id: str) -> None:
    if re.fullmatch(_TARGET_ID_PATTERNS[target_kind], target_id) is None:
        raise ValueError(f"target_id {target_id!r} does not match target_kind {target_kind}")


def _sorted_unique(values: Iterable[str], *, field_name: str) -> tuple[str, ...]:
    materialized = tuple(values)
    normalized = tuple(sorted(set(materialized)))
    if len(normalized) != len(materialized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _sorted_unique_ranks(values: Iterable[int]) -> tuple[int, ...]:
    materialized = tuple(values)
    normalized = tuple(sorted(set(materialized)))
    if len(normalized) != len(materialized):
        raise ValueError("source_ranks must not contain duplicates")
    return normalized


def _conflict_id_payload(record: ResolutionConflictRecord) -> dict[str, object]:
    return {
        "schema": _CONFLICT_SCHEMA,
        "target_kind": record.target_kind.value,
        "target_id": record.target_id,
        "candidate_ids": record.candidate_ids,
        "evidence_ids": record.evidence_ids,
        "source_ranks": record.source_ranks,
        "reason_codes": tuple(reason.value for reason in record.reason_codes),
        "policy_version": record.policy_version,
        "policy_hash": record.policy_hash,
        "prior_label_id": record.prior_label_id,
    }


def _override_id_payload(record: ResolutionOverrideRecord) -> dict[str, object]:
    return {
        "schema": _OVERRIDE_SCHEMA,
        "target_kind": record.target_kind.value,
        "target_id": record.target_id,
        "winner_candidate_id": record.winner_candidate_id,
        "overridden_candidate_ids": record.overridden_candidate_ids,
        "evidence_ids": record.evidence_ids,
        "source_ranks": record.source_ranks,
        "reason_codes": tuple(reason.value for reason in record.reason_codes),
        "policy_version": record.policy_version,
        "policy_hash": record.policy_hash,
        "prior_label_id": record.prior_label_id,
    }


class ResolutionConflictRecord(StrictModel):
    """Append-only record for disagreement among strong resolution sources."""

    schema_version: Literal[1] = 1
    conflict_id: str = Field(pattern=id_pattern(RESOLUTION_CONFLICT_PREFIX))
    target_kind: SemanticLabelTargetKind
    target_id: str
    candidate_ids: tuple[ResolutionCandidateId, ...] = ()
    evidence_ids: tuple[EvidenceId, ...] = ()
    source_ranks: tuple[SourceRank, ...] = Field(min_length=1)
    reason_codes: tuple[ResolutionConflictReason, ...] = Field(min_length=1)
    policy_version: str = Field(min_length=1, strict=True)
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    prior_label_id: str | None = Field(default=None, pattern=id_pattern(LABEL_PREFIX))
    detected_at: datetime.datetime

    _utc = field_validator("detected_at")(require_utc)

    @model_validator(mode="after")
    def _coherent_and_content_addressed(self) -> Self:
        _validate_target(self.target_kind, self.target_id)
        if self.candidate_ids != tuple(sorted(set(self.candidate_ids))):
            raise ValueError("candidate_ids must be sorted and unique")
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))):
            raise ValueError("evidence_ids must be sorted and unique")
        if self.source_ranks != tuple(sorted(set(self.source_ranks))):
            raise ValueError("source_ranks must be sorted and unique")
        if self.reason_codes != tuple(sorted(set(self.reason_codes), key=str)):
            raise ValueError("reason_codes must be sorted and unique")
        if len(self.candidate_ids) + len(self.evidence_ids) < 2:
            raise ValueError(
                "a resolution conflict requires at least two combined candidate/evidence IDs"
            )
        expected = make_id(RESOLUTION_CONFLICT_PREFIX, _conflict_id_payload(self))
        if self.conflict_id != expected:
            raise ValueError("conflict_id differs from semantic content")
        return self


class ResolutionOverrideRecord(StrictModel):
    """Append-only log of a non-conflicting resolution precedence action."""

    schema_version: Literal[1] = 1
    override_id: str = Field(pattern=id_pattern(RESOLUTION_OVERRIDE_PREFIX))
    target_kind: SemanticLabelTargetKind
    target_id: str
    winner_candidate_id: ResolutionCandidateId
    overridden_candidate_ids: tuple[ResolutionCandidateId, ...] = Field(min_length=1)
    evidence_ids: tuple[EvidenceId, ...] = ()
    source_ranks: tuple[SourceRank, ...] = Field(min_length=1)
    reason_codes: tuple[ResolutionOverrideReason, ...] = Field(min_length=1)
    policy_version: str = Field(min_length=1, strict=True)
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    prior_label_id: str | None = Field(default=None, pattern=id_pattern(LABEL_PREFIX))
    logged_at: datetime.datetime

    _utc = field_validator("logged_at")(require_utc)

    @model_validator(mode="after")
    def _coherent_and_content_addressed(self) -> Self:
        _validate_target(self.target_kind, self.target_id)
        if self.overridden_candidate_ids != tuple(sorted(set(self.overridden_candidate_ids))):
            raise ValueError("overridden_candidate_ids must be sorted and unique")
        if self.winner_candidate_id in self.overridden_candidate_ids:
            raise ValueError("winner_candidate_id must differ from every overridden candidate")
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))):
            raise ValueError("evidence_ids must be sorted and unique")
        if self.source_ranks != tuple(sorted(set(self.source_ranks))):
            raise ValueError("source_ranks must be sorted and unique")
        if self.reason_codes != tuple(sorted(set(self.reason_codes), key=str)):
            raise ValueError("reason_codes must be sorted and unique")
        expected = make_id(RESOLUTION_OVERRIDE_PREFIX, _override_id_payload(self))
        if self.override_id != expected:
            raise ValueError("override_id differs from semantic content")
        return self


def build_resolution_conflict_record(
    *,
    target_kind: SemanticLabelTargetKind,
    target_id: str,
    candidate_ids: Iterable[str] = (),
    evidence_ids: Iterable[str] = (),
    source_ranks: Iterable[int],
    reason_codes: Iterable[ResolutionConflictReason],
    policy_version: str,
    policy_hash: str,
    detected_at: datetime.datetime,
    prior_label_id: str | None = None,
) -> ResolutionConflictRecord:
    """Build a conflict record whose ID excludes only ``detected_at``."""

    candidates = _sorted_unique(candidate_ids, field_name="candidate_ids")
    evidence = _sorted_unique(evidence_ids, field_name="evidence_ids")
    ranks = _sorted_unique_ranks(source_ranks)
    reasons = tuple(sorted(set(reason_codes), key=str))
    provisional = ResolutionConflictRecord.model_construct(
        schema_version=1,
        conflict_id=f"{RESOLUTION_CONFLICT_PREFIX}:{'0' * 64}",
        target_kind=target_kind,
        target_id=target_id,
        candidate_ids=candidates,
        evidence_ids=evidence,
        source_ranks=ranks,
        reason_codes=reasons,
        policy_version=policy_version,
        policy_hash=policy_hash,
        prior_label_id=prior_label_id,
        detected_at=detected_at,
    )
    return ResolutionConflictRecord.model_validate(
        {
            **provisional.model_dump(mode="python", exclude={"conflict_id"}),
            "conflict_id": make_id(
                RESOLUTION_CONFLICT_PREFIX,
                _conflict_id_payload(provisional),
            ),
        }
    )


def build_resolution_override_record(
    *,
    target_kind: SemanticLabelTargetKind,
    target_id: str,
    winner_candidate_id: str,
    overridden_candidate_ids: Iterable[str],
    evidence_ids: Iterable[str] = (),
    source_ranks: Iterable[int],
    reason_codes: Iterable[ResolutionOverrideReason],
    policy_version: str,
    policy_hash: str,
    logged_at: datetime.datetime,
    prior_label_id: str | None = None,
) -> ResolutionOverrideRecord:
    """Build an override record whose ID excludes only ``logged_at``."""

    overridden = _sorted_unique(
        overridden_candidate_ids,
        field_name="overridden_candidate_ids",
    )
    evidence = _sorted_unique(evidence_ids, field_name="evidence_ids")
    ranks = _sorted_unique_ranks(source_ranks)
    reasons = tuple(sorted(set(reason_codes), key=str))
    provisional = ResolutionOverrideRecord.model_construct(
        schema_version=1,
        override_id=f"{RESOLUTION_OVERRIDE_PREFIX}:{'0' * 64}",
        target_kind=target_kind,
        target_id=target_id,
        winner_candidate_id=winner_candidate_id,
        overridden_candidate_ids=overridden,
        evidence_ids=evidence,
        source_ranks=ranks,
        reason_codes=reasons,
        policy_version=policy_version,
        policy_hash=policy_hash,
        prior_label_id=prior_label_id,
        logged_at=logged_at,
    )
    return ResolutionOverrideRecord.model_validate(
        {
            **provisional.model_dump(mode="python", exclude={"override_id"}),
            "override_id": make_id(
                RESOLUTION_OVERRIDE_PREFIX,
                _override_id_payload(provisional),
            ),
        }
    )


__all__ = [
    "RESOLUTION_CONFLICT_PREFIX",
    "RESOLUTION_OVERRIDE_PREFIX",
    "ResolutionConflictReason",
    "ResolutionConflictRecord",
    "ResolutionOverrideReason",
    "ResolutionOverrideRecord",
    "build_resolution_conflict_record",
    "build_resolution_override_record",
]
