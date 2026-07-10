"""VariantRecord and transformation support records (PLAN.md §11.5, §11.10, §15.2).

§7.1 fixes this module as the single definition home for ``VariantRecord``,
``Applicability``, ``VariantDraft``, and ``TransformationAudit``; the LF-016
transformation protocol imports them from here. ``validation_status`` is
execution state, never semantic truth (§11.5).
"""

from __future__ import annotations

import re

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import (
    GeneratorKind,
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
)
from leanfaith.schemas.ids import (
    DRAFT_PREFIX,
    EVIDENCE_PREFIX,
    HEX64_PATTERN,
    THEOREM_PREFIX,
    VARIANT_PREFIX,
    id_pattern,
)

ECODE_PATTERN = r"^E(0[1-9]|[12][0-9]|30)$"
FAMILY_ID_PATTERN = r"^[a-z][a-z0-9_]*$"

MetadataValue = str | int | float | bool | None


def _check_ecodes(codes: tuple[str, ...]) -> None:
    for code in codes:
        if not re.match(ECODE_PATTERN, code):
            raise ValueError(f"unknown error code {code!r}; only E01-E30 are storable (§14.3)")


class Applicability(StrictModel):
    """Whether a transformation rule applies to a theorem (§15.2)."""

    applicable: bool
    reason_codes: tuple[str, ...]
    matched_nodes: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class VariantDraft(StrictModel):
    """A seeded, traceable draft produced by a transformation rule (§15.2)."""

    draft_id: str = Field(pattern=id_pattern(DRAFT_PREFIX))
    source_theorem_ids: tuple[str, ...] = Field(min_length=1)
    rule_id: str
    rule_version: str
    family_id: str = Field(pattern=FAMILY_ID_PATTERN)
    seed: int
    candidate_code: str
    intended_relation: IntendedRelation
    intended_error_types: tuple[str, ...] = ()
    candidate_pool: str
    transformation_trace: tuple[dict[str, object], ...] = ()
    inverse_trace: tuple[dict[str, object], ...] | None = None
    expected_atom_mapping: dict[str, str] = Field(default_factory=dict)
    expected_structural_diff: dict[str, object] = Field(default_factory=dict)
    generation_config_hash: str = Field(pattern=HEX64_PATTERN)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> VariantDraft:
        thm = id_pattern(THEOREM_PREFIX)
        for source in self.source_theorem_ids:
            if not re.match(thm, source):
                raise ValueError(f"source theorem ID {source!r} is not a '{THEOREM_PREFIX}:' ID")
        _check_ecodes(self.intended_error_types)
        return self


class TransformationAudit(StrictModel):
    """Mechanical audit of one draft (§15.2); recommendations, never labels."""

    audit_id: str = Field(pattern=id_pattern("audit"))
    draft_id: str = Field(pattern=id_pattern(DRAFT_PREFIX))
    applicability: Applicability
    elaboration_evidence_id: str | None = Field(default=None, pattern=id_pattern(EVIDENCE_PREFIX))
    structural_diff_ok: bool | None = None
    atom_mapping_ok: bool | None = None
    inverse_or_roundtrip_ok: bool | None = None
    certificate_evidence_ids: tuple[str, ...] = ()
    violation_codes: tuple[str, ...] = ()
    recommended_validation_status: ValidationStatus
    recommended_quality_tier: QualityTier
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _evidence_ids(self) -> TransformationAudit:
        ev = id_pattern(EVIDENCE_PREFIX)
        for evidence_id in self.certificate_evidence_ids:
            if not re.match(ev, evidence_id):
                raise ValueError(f"certificate evidence ID {evidence_id!r} is not an 'ev:' ID")
        return self


class VariantRecord(StrictModel):
    """A generated candidate statement with full provenance (§11.5)."""

    schema_version: int = 1
    variant_id: str = Field(pattern=id_pattern(VARIANT_PREFIX))
    source_theorem_ids: tuple[str, ...] = ()
    generator_kind: GeneratorKind
    generator_id: str
    generation_config_hash: str = Field(pattern=HEX64_PATTERN)
    seed: int | None = None
    prompt_artifact: str | None = None
    raw_output_artifact: str | None = None
    extracted_statement: str | None = None
    intended_relation: IntendedRelation
    intended_error_types: tuple[str, ...] = ()
    candidate_pool: str
    transformation_trace: tuple[dict[str, object], ...] = ()
    inverse_trace: tuple[dict[str, object], ...] | None = None
    validation_status: ValidationStatus = ValidationStatus.UNVALIDATED
    validation_evidence_id: str | None = Field(default=None, pattern=id_pattern(EVIDENCE_PREFIX))
    derived_theorem_id: str | None = Field(default=None, pattern=id_pattern(THEOREM_PREFIX))
    quality_tier: QualityTier = QualityTier.PROVISIONAL
    polarity_metadata: Polarity = Polarity.UNKNOWN
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> VariantRecord:
        thm = id_pattern(THEOREM_PREFIX)
        for source in self.source_theorem_ids:
            if not re.match(thm, source):
                raise ValueError(f"source theorem ID {source!r} is not a '{THEOREM_PREFIX}:' ID")
        _check_ecodes(self.intended_error_types)
        if self.quality_tier not in (QualityTier.PROVISIONAL, QualityTier.UNKNOWN):
            raise ValueError(
                "a VariantRecord stays provisional until resolution; supervised quality "
                "comes only from ResolvedLabel (§11.5)"
            )
        if (
            self.generator_kind == GeneratorKind.DETERMINISTIC_TRANSFORM
            and not self.source_theorem_ids
        ):
            raise ValueError("a deterministic transform variant requires source theorem IDs")
        return self
