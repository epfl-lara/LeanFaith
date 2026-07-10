"""Common prediction/baseline output record (PLAN.md §20.6)."""

from __future__ import annotations

import re

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import Decision, RelationLabel
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    HEX64_PATTERN,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    id_pattern,
)
from leanfaith.schemas.variant import _check_ecodes

_RECORD_ID_PATTERN = rf"^(?:{PAIR_PREFIX}|{NL_LEAN_PREFIX}):[0-9a-f]{{64}}$"

_CANONICAL_RELATIONS = frozenset(item.value for item in RelationLabel)


class PredictionRecord(StrictModel):
    """The §20.6 common output contract emitted by every baseline and model."""

    record_id: str = Field(pattern=_RECORD_ID_PATTERN)
    method: str = Field(min_length=1)
    method_version: str = Field(min_length=1)
    score_same_claim: float = Field(ge=0.0, le=1.0)
    score_ambiguous: float = Field(ge=0.0, le=1.0)
    decision: Decision
    relation_scores: dict[str, float] = Field(default_factory=dict)
    error_type_scores: dict[str, float] = Field(default_factory=dict)
    elapsed_ms: int = Field(ge=0)
    cost: dict[str, float] = Field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    config_hash: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _checks(self) -> PredictionRecord:
        for relation in self.relation_scores:
            if relation not in _CANONICAL_RELATIONS:
                raise ValueError(f"unknown relation {relation!r}; only §11.1 spellings persist")
        _check_ecodes(tuple(self.error_type_scores))
        ev = id_pattern(EVIDENCE_PREFIX)
        for evidence_id in self.evidence_ids:
            if not re.match(ev, evidence_id):
                raise ValueError(f"evidence ID {evidence_id!r} is not an 'ev:' ID")
        return self
