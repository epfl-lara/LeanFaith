"""Human annotation record (PLAN.md §18.5, §14.4).

Raw annotator decisions are preserved verbatim; adjudication and resolution
create separate artifacts. UI spellings map to canonical enums only through
the §14.4 tables.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Literal

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import (
    AnnotationAnswer,
    ReferenceIssue,
    RelationLabel,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.ids import (
    ANNOTATION_PREFIX,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    id_pattern,
)
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.migrations import (
    CURRENT_RECORD_SCHEMA_VERSION,
    LEGACY_RECORD_SCHEMA_VERSION,
    migrate_legacy_relation,
)
from leanfaith.schemas.variant import _check_ecodes

MetadataValue = str | int | float | bool | None

_TARGET_ID_PATTERNS: dict[SemanticLabelTargetKind, str] = {
    SemanticLabelTargetKind.LEAN_PAIR: id_pattern(PAIR_PREFIX),
    SemanticLabelTargetKind.NL_LEAN: id_pattern(NL_LEAN_PREFIX),
}


class AnnotationRecord(StrictModel):
    """One independent expert label (§18.5)."""

    schema_version: Literal[2] = CURRENT_RECORD_SCHEMA_VERSION
    annotation_id: str = Field(pattern=id_pattern(ANNOTATION_PREFIX))
    target_kind: SemanticLabelTargetKind
    target_id: str
    annotator_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    same_claim: AnnotationAnswer
    relation: RelationLabel | None
    error_types: tuple[str, ...] = ()
    confidence: int = Field(ge=1, le=5)
    rationale: str = ""
    reference_issue: ReferenceIssue = ReferenceIssue.NONE
    created_at: datetime.datetime
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_v1(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return value
        data = dict(value)
        legacy_relation = data.get("relation")
        relation, provenance = migrate_legacy_relation(legacy_relation)
        data["schema_version"] = CURRENT_RECORD_SCHEMA_VERSION
        data["relation"] = relation
        metadata = dict(data.get("metadata", {}))
        metadata["source_schema_version"] = LEGACY_RECORD_SCHEMA_VERSION
        metadata["legacy_relation"] = str(legacy_relation)
        if provenance:
            metadata["near_miss"] = True
        data["metadata"] = metadata
        return data

    @model_validator(mode="after")
    def _checks(self) -> AnnotationRecord:
        require_utc(self.created_at)
        pattern = _TARGET_ID_PATTERNS[self.target_kind]
        if not re.match(pattern, self.target_id):
            raise ValueError(
                f"target_id {self.target_id!r} does not match target_kind {self.target_kind}"
            )
        _check_ecodes(self.error_types)
        needs_rationale = (
            AnnotationAnswer.NOT_SAME_CLAIM,
            AnnotationAnswer.AMBIGUOUS,
            AnnotationAnswer.CANNOT_ASSESS_YET,
        )
        if self.same_claim in needs_rationale and not self.rationale.strip():
            raise ValueError(
                "rationale is required for not-same/ambiguous/cannot-assess answers (§18.5)"
            )
        if (
            self.same_claim == AnnotationAnswer.SAME_CLAIM
            and self.relation != RelationLabel.EQUIVALENT
        ):
            raise ValueError("same_claim annotation requires relation=equivalent")
        if self.same_claim == AnnotationAnswer.NOT_SAME_CLAIM and self.relation not in {
            RelationLabel.A_STRONGER,
            RelationLabel.B_STRONGER,
            RelationLabel.INCOMPARABLE,
            RelationLabel.UNRELATED,
        }:
            raise ValueError(
                "not_same_claim annotation requires a non-equivalent terminal relation"
            )
        if (
            self.same_claim == AnnotationAnswer.AMBIGUOUS
            and self.relation != RelationLabel.AMBIGUOUS
        ):
            raise ValueError("ambiguous annotation requires relation=ambiguous")
        if self.same_claim == AnnotationAnswer.CANNOT_ASSESS_YET and self.relation is not None:
            raise ValueError("cannot_assess_yet annotation requires relation=null")
        return self
