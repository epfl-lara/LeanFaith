"""Human annotation record (PLAN.md §18.5, §14.4).

Raw annotator decisions are preserved verbatim; adjudication and resolution
create separate artifacts. UI spellings map to canonical enums only through
the §14.4 tables.
"""

from __future__ import annotations

import datetime
import re

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
from leanfaith.schemas.variant import _check_ecodes

MetadataValue = str | int | float | bool | None

_TARGET_ID_PATTERNS: dict[SemanticLabelTargetKind, str] = {
    SemanticLabelTargetKind.LEAN_PAIR: id_pattern(PAIR_PREFIX),
    SemanticLabelTargetKind.NL_LEAN: id_pattern(NL_LEAN_PREFIX),
}


class AnnotationRecord(StrictModel):
    """One independent expert label (§18.5)."""

    schema_version: int = 1
    annotation_id: str = Field(pattern=id_pattern(ANNOTATION_PREFIX))
    target_kind: SemanticLabelTargetKind
    target_id: str
    annotator_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    same_claim: AnnotationAnswer
    relation: RelationLabel
    error_types: tuple[str, ...] = ()
    confidence: int = Field(ge=1, le=5)
    rationale: str = ""
    reference_issue: ReferenceIssue = ReferenceIssue.NONE
    created_at: datetime.datetime
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> AnnotationRecord:
        require_utc(self.created_at)
        pattern = _TARGET_ID_PATTERNS[self.target_kind]
        if not re.match(pattern, self.target_id):
            raise ValueError(
                f"target_id {self.target_id!r} does not match target_kind {self.target_kind}"
            )
        _check_ecodes(self.error_types)
        needs_rationale = (AnnotationAnswer.NOT_SAME_CLAIM, AnnotationAnswer.AMBIGUOUS)
        if self.same_claim in needs_rationale and not self.rationale.strip():
            raise ValueError("rationale is required for not-same/ambiguous answers (§18.5)")
        return self
