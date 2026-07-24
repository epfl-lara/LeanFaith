"""Common prediction/baseline output record (PLAN.md §20.6)."""

from __future__ import annotations

import re
from typing import Any, Literal

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
from leanfaith.schemas.migrations import (
    CURRENT_RECORD_SCHEMA_VERSION,
    LEGACY_INCOMPARABLE,
    LEGACY_RECORD_SCHEMA_VERSION,
    LEGACY_UNKNOWN,
)

_RECORD_ID_PATTERN = rf"^(?:{PAIR_PREFIX}|{NL_LEAN_PREFIX}):[0-9a-f]{{64}}$"

_CANONICAL_RELATIONS = frozenset(item.value for item in RelationLabel)


class PredictionRecord(StrictModel):
    """The §20.6 common output contract emitted by every baseline and model."""

    schema_version: Literal[2] = CURRENT_RECORD_SCHEMA_VERSION
    record_id: str = Field(pattern=_RECORD_ID_PATTERN)
    method: str = Field(min_length=1)
    method_version: str = Field(min_length=1)
    same_claim_probability: float = Field(ge=0.0, le=1.0)
    ambiguity_probability: float = Field(ge=0.0, le=1.0)
    decision: Decision
    relation_scores: dict[str, float] = Field(default_factory=dict)
    optional_auxiliary_scores: dict[str, float] = Field(default_factory=dict)
    model_version: str = Field(min_length=1)
    tokenizer_version: str = Field(min_length=1)
    representation_version: str = Field(min_length=1)
    calibration_version: str = Field(min_length=1)
    elapsed_ms: int = Field(ge=0)
    cost: dict[str, float] = Field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    config_hash: str = Field(pattern=HEX64_PATTERN)
    migration_metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_v1(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        relation_scores = value.get("relation_scores")
        inferred_v1 = "score_same_claim" in value or (
            isinstance(relation_scores, dict)
            and any(key in relation_scores for key in (LEGACY_INCOMPARABLE, LEGACY_UNKNOWN))
        )
        declared_version = value.get("schema_version")
        if declared_version == CURRENT_RECORD_SCHEMA_VERSION:
            return value
        if declared_version != LEGACY_RECORD_SCHEMA_VERSION and not (
            declared_version is None and inferred_v1
        ):
            return value
        data = dict(value)
        if "score_same_claim" in data:
            data["same_claim_probability"] = data.pop("score_same_claim")
        if "score_ambiguous" in data:
            data["ambiguity_probability"] = data.pop("score_ambiguous")
        if "error_type_scores" in data:
            data["optional_auxiliary_scores"] = data.pop("error_type_scores")
        scores = dict(data.get("relation_scores", {}))
        legacy_keys: list[str] = []
        if LEGACY_INCOMPARABLE in scores:
            legacy_keys.append(LEGACY_INCOMPARABLE)
            scores[RelationLabel.INCOMPARABLE.value] = scores.pop(LEGACY_INCOMPARABLE)
        if LEGACY_UNKNOWN in scores:
            legacy_keys.append(LEGACY_UNKNOWN)
            scores.pop(LEGACY_UNKNOWN)
        data["relation_scores"] = scores
        data.setdefault("model_version", str(data.get("method_version", "legacy")))
        data.setdefault("tokenizer_version", "not_applicable")
        data.setdefault("representation_version", "legacy_unknown")
        data.setdefault("calibration_version", "uncalibrated")
        metadata = dict(data.get("migration_metadata", {}))
        metadata["source_schema_version"] = LEGACY_RECORD_SCHEMA_VERSION
        if legacy_keys:
            metadata["legacy_relation_keys"] = ",".join(legacy_keys)
        data["migration_metadata"] = metadata
        data["schema_version"] = CURRENT_RECORD_SCHEMA_VERSION
        return data

    @model_validator(mode="after")
    def _checks(self) -> PredictionRecord:
        for relation, score in self.relation_scores.items():
            if relation not in _CANONICAL_RELATIONS:
                raise ValueError(f"unknown relation {relation!r}; only §11.1 spellings persist")
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"relation score {relation!r} must be in [0,1]")
        for name, score in self.optional_auxiliary_scores.items():
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"optional auxiliary score {name!r} must be in [0,1]")
        ev = id_pattern(EVIDENCE_PREFIX)
        for evidence_id in self.evidence_ids:
            if not re.match(ev, evidence_id):
                raise ValueError(f"evidence ID {evidence_id!r} is not an 'ev:' ID")
        return self
