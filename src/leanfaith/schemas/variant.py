"""VariantRecord and transformation support records (PLAN.md §11.5, §11.10, §15.2).

§7.1 fixes this module as the single definition home for ``VariantRecord`` and
the persistent LF-016 applicability, attempt, draft, audit, and family-promotion
records.  The transformation protocol imports these schemas from here.
``validation_status`` is execution state, never semantic truth (§11.5).
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from leanfaith.config.hashing import CanonicalizationError, to_canonical
from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import (
    GeneratorKind,
    IntendedRelation,
    Polarity,
    QualityTier,
    TransformationFamilyStatus,
    ValidationStatus,
)
from leanfaith.schemas.ids import (
    AUDIT_PREFIX,
    CONTEXT_PREFIX,
    DRAFT_PREFIX,
    EVIDENCE_PREFIX,
    HEX64_PATTERN,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    VARIANT_PREFIX,
    id_pattern,
)

ECODE_PATTERN = r"^E(0[1-9]|[12][0-9]|30)$"
FAMILY_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
TRANSFORMATION_SCHEMA_VERSION: Literal[1] = 1

MetadataValue = str | int | float | bool | None
TransformationAttemptOutcome = Literal[
    "not_applicable",
    "generated",
    "no_output",
    "rejected_disabled",
    "generation_error",
    "infrastructure_error",
]
SCIValidationStatus = Literal[
    "not_requested",
    "pending",
    "validated",
    "retagged",
    "rejected",
    "malformed",
]
FORMALRX_SCI_CATEGORIES = frozenset(
    {
        "S1.1",
        "S1.2",
        "S1.3",
        "S2.1",
        "S2.2",
        "S2.3",
        "S2.4",
        "S2.5",
        "S2.6",
        "S2.7",
        "S3.1",
        "S3.2",
        "S3.3",
        "S3.4",
        "S3.5",
        "C1.1",
        "C1.2",
        "C1.3",
        "C1.4",
        "C2.1",
        "C2.2",
        "C3.1",
        "C3.2",
        "C3.3",
        "C4",
        "C5",
        "I1",
        "I2",
    }
)


def _check_ecodes(codes: tuple[str, ...]) -> None:
    for code in codes:
        if not re.match(ECODE_PATTERN, code):
            raise ValueError(f"unknown error code {code!r}; only E01-E30 are storable (§14.3)")


def _check_sorted_unique(values: tuple[str, ...], *, field_name: str) -> None:
    if list(values) != sorted(set(values)):
        raise ValueError(f"{field_name} must be sorted and unique")


def _check_source_links(
    *,
    theorem_ids: tuple[str, ...],
    representation_ids: tuple[str, ...],
    allow_missing_representations: bool,
) -> None:
    theorem_pattern = id_pattern(THEOREM_PREFIX)
    representation_pattern = id_pattern(REPRESENTATION_PREFIX)
    for theorem_id in theorem_ids:
        if re.fullmatch(theorem_pattern, theorem_id) is None:
            raise ValueError(f"source theorem ID {theorem_id!r} is not a '{THEOREM_PREFIX}:' ID")
    _check_sorted_unique(theorem_ids, field_name="source_theorem_ids")
    for representation_id in representation_ids:
        if re.fullmatch(representation_pattern, representation_id) is None:
            raise ValueError(
                f"source representation ID {representation_id!r} is not a "
                f"'{REPRESENTATION_PREFIX}:' ID"
            )
    if len(set(representation_ids)) != len(representation_ids):
        raise ValueError("source_representation_ids must be unique")
    if representation_ids and len(representation_ids) != len(theorem_ids):
        raise ValueError("source_representation_ids must align one-to-one with source_theorem_ids")
    if theorem_ids and not representation_ids and not allow_missing_representations:
        raise ValueError(
            "source_representation_ids are required and must align one-to-one "
            "with source_theorem_ids"
        )


def _check_nonempty_text(value: str, *, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must contain non-whitespace text")


def _candidate_code_hash(candidate_code: str) -> str:
    return hashlib.sha256(candidate_code.encode("utf-8")).hexdigest()


def _check_json_shaped(value: object, *, field_name: str) -> None:
    try:
        to_canonical(value)
    except CanonicalizationError as exc:
        raise ValueError(f"{field_name} must contain only canonical JSON values: {exc}") from exc


def _check_sci_provenance(
    *,
    requested: str | None,
    validated: str | None,
    status: SCIValidationStatus,
    proposer_family: str | None,
    validator_family: str | None,
) -> None:
    for role, category in (("requested", requested), ("validated", validated)):
        if category is not None and category not in FORMALRX_SCI_CATEGORIES:
            raise ValueError(f"unknown FormalRx SCI {role} category {category!r}")
    if status == "not_requested" and any(
        value is not None for value in (requested, validated, proposer_family, validator_family)
    ):
        raise ValueError("SCI provenance values require a non-'not_requested' validation status")
    if status in {"validated", "retagged"} and validated is None:
        raise ValueError(f"SCI validation status {status!r} requires formalrx_sci_validated")
    if proposer_family is not None and proposer_family == validator_family:
        raise ValueError("SCI proposer and validator must come from distinct model families")


class Applicability(StrictModel):
    """Whether a transformation rule applies to a theorem (§15.2)."""

    applicable: bool
    reason_codes: tuple[str, ...]
    matched_nodes: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _canonical_collections(self) -> Applicability:
        for field_name in ("reason_codes", "matched_nodes", "required_capabilities"):
            _check_sorted_unique(getattr(self, field_name), field_name=field_name)
        if not self.applicable and not self.reason_codes:
            raise ValueError("a non-applicable result requires at least one reason_code")
        return self


class VariantDraft(StrictModel):
    """A seeded, traceable draft produced by a transformation rule (§15.2)."""

    schema_version: Literal[1] = TRANSFORMATION_SCHEMA_VERSION
    draft_id: str = Field(pattern=id_pattern(DRAFT_PREFIX))
    source_theorem_ids: tuple[str, ...] = Field(min_length=1)
    source_representation_ids: tuple[str, ...] = Field(min_length=1)
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    family_id: str = Field(pattern=FAMILY_ID_PATTERN)
    seed: int = Field(strict=True)
    candidate_code: str = Field(min_length=1)
    candidate_code_hash: str = Field(pattern=HEX64_PATTERN)
    intended_relation: IntendedRelation
    intended_error_types: tuple[str, ...] = ()
    formalrx_sci_requested: str | None = None
    formalrx_sci_validated: str | None = None
    formalrx_sci_validation_status: SCIValidationStatus = "not_requested"
    formalrx_sci_proposer_family: str | None = None
    formalrx_sci_validator_family: str | None = None
    candidate_pool: str = Field(min_length=1)
    transformation_trace: tuple[dict[str, JsonValue], ...] = Field(min_length=1)
    inverse_trace: tuple[dict[str, JsonValue], ...] | None = None
    expected_atom_mapping: dict[str, str] = Field(default_factory=dict)
    expected_structural_diff: dict[str, JsonValue] = Field(default_factory=dict)
    generation_config_hash: str = Field(pattern=HEX64_PATTERN)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> VariantDraft:
        _check_source_links(
            theorem_ids=self.source_theorem_ids,
            representation_ids=self.source_representation_ids,
            allow_missing_representations=False,
        )
        _check_nonempty_text(self.rule_id, field_name="rule_id")
        _check_nonempty_text(self.rule_version, field_name="rule_version")
        _check_nonempty_text(self.candidate_code, field_name="candidate_code")
        _check_nonempty_text(self.candidate_pool, field_name="candidate_pool")
        if self.candidate_code_hash != _candidate_code_hash(self.candidate_code):
            raise ValueError("candidate_code_hash does not match candidate_code UTF-8 bytes")
        _check_json_shaped(self.transformation_trace, field_name="transformation_trace")
        _check_json_shaped(self.inverse_trace, field_name="inverse_trace")
        _check_json_shaped(self.expected_structural_diff, field_name="expected_structural_diff")
        _check_sorted_unique(self.intended_error_types, field_name="intended_error_types")
        _check_ecodes(self.intended_error_types)
        _check_sci_provenance(
            requested=self.formalrx_sci_requested,
            validated=self.formalrx_sci_validated,
            status=self.formalrx_sci_validation_status,
            proposer_family=self.formalrx_sci_proposer_family,
            validator_family=self.formalrx_sci_validator_family,
        )
        return self


class TransformationAttempt(StrictModel):
    """One persistent terminal application of a registered rule.

    The attempt record is the lineage boundary between registry dispatch and
    zero or more drafts.  It records non-applicability and failures as first
    class terminal outcomes, so only storing successful drafts can never hide
    a rejected or failed application.
    """

    schema_version: Literal[1] = TRANSFORMATION_SCHEMA_VERSION
    attempt_id: str = Field(pattern=id_pattern("attempt"))
    family_id: str = Field(pattern=FAMILY_ID_PATTERN)
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    source_theorem_ids: tuple[str, ...] = Field(min_length=1)
    source_representation_ids: tuple[str, ...] = Field(min_length=1)
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    registry_hash: str = Field(pattern=HEX64_PATTERN)
    generation_config_hash: str = Field(pattern=HEX64_PATTERN)
    seed: int = Field(strict=True)
    applicability: Applicability | None
    terminal_outcome: TransformationAttemptOutcome
    draft_ids: tuple[str, ...] = ()
    failure_codes: tuple[str, ...] = ()
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _terminal_state(self) -> TransformationAttempt:
        _check_source_links(
            theorem_ids=self.source_theorem_ids,
            representation_ids=self.source_representation_ids,
            allow_missing_representations=False,
        )
        _check_nonempty_text(self.rule_id, field_name="rule_id")
        _check_nonempty_text(self.rule_version, field_name="rule_version")
        draft_pattern = id_pattern(DRAFT_PREFIX)
        for draft_id in self.draft_ids:
            if re.fullmatch(draft_pattern, draft_id) is None:
                raise ValueError(f"draft ID {draft_id!r} is not a '{DRAFT_PREFIX}:' ID")
        _check_sorted_unique(self.draft_ids, field_name="draft_ids")
        _check_sorted_unique(self.failure_codes, field_name="failure_codes")

        if self.terminal_outcome == "generated":
            if self.applicability is None or not self.applicability.applicable:
                raise ValueError("generated outcome requires applicability.applicable=true")
            if not self.draft_ids:
                raise ValueError("generated outcome requires at least one draft_id")
            if self.failure_codes:
                raise ValueError("generated outcome cannot carry failure_codes")
        else:
            if self.draft_ids:
                raise ValueError("non-generated attempt outcomes cannot carry draft_ids")
            if self.terminal_outcome == "not_applicable":
                if self.applicability is None or self.applicability.applicable:
                    raise ValueError(
                        "not_applicable outcome requires applicability.applicable=false"
                    )
            elif self.terminal_outcome == "rejected_disabled":
                if self.applicability is not None and self.applicability.applicable:
                    raise ValueError("rejected_disabled cannot carry applicability.applicable=true")
            elif self.terminal_outcome == "no_output":
                if self.applicability is None or not self.applicability.applicable:
                    raise ValueError("no_output requires applicability.applicable=true")
            elif self.applicability is not None and not self.applicability.applicable:
                raise ValueError(
                    "a non-applicable assessment must terminate as not_applicable or "
                    "rejected_disabled"
                )
            if (
                self.terminal_outcome not in {"not_applicable", "no_output"}
                and not self.failure_codes
            ):
                raise ValueError(f"{self.terminal_outcome} outcome requires a failure_code")
        return self


class TransformationAudit(StrictModel):
    """Mechanical audit of one draft (§15.2); recommendations, never labels."""

    schema_version: Literal[1] = TRANSFORMATION_SCHEMA_VERSION
    audit_id: str = Field(pattern=id_pattern(AUDIT_PREFIX))
    draft_id: str = Field(pattern=id_pattern(DRAFT_PREFIX))
    family_id: str = Field(pattern=FAMILY_ID_PATTERN)
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    candidate_code_hash: str = Field(pattern=HEX64_PATTERN)
    candidate_theorem_id: str | None = Field(default=None, pattern=id_pattern(THEOREM_PREFIX))
    candidate_representation_id: str | None = Field(
        default=None, pattern=id_pattern(REPRESENTATION_PREFIX)
    )
    audit_config_hash: str = Field(pattern=HEX64_PATTERN)
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
        _check_nonempty_text(self.rule_id, field_name="rule_id")
        _check_nonempty_text(self.rule_version, field_name="rule_version")
        if not self.applicability.applicable:
            raise ValueError("a generated draft audit requires applicability.applicable=true")
        ev = id_pattern(EVIDENCE_PREFIX)
        for evidence_id in self.certificate_evidence_ids:
            if re.fullmatch(ev, evidence_id) is None:
                raise ValueError(f"certificate evidence ID {evidence_id!r} is not an 'ev:' ID")
        _check_sorted_unique(self.certificate_evidence_ids, field_name="certificate_evidence_ids")
        _check_sorted_unique(self.violation_codes, field_name="violation_codes")
        if (
            self.elaboration_evidence_id is not None
            and self.elaboration_evidence_id in self.certificate_evidence_ids
        ):
            raise ValueError(
                "elaboration_evidence_id must not be duplicated in certificate_evidence_ids"
            )
        if self.candidate_representation_id is not None and self.candidate_theorem_id is None:
            raise ValueError("candidate_representation_id requires a linked candidate_theorem_id")
        if self.recommended_validation_status in {
            ValidationStatus.ELABORATES,
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        } and (self.candidate_theorem_id is None or self.candidate_representation_id is None):
            raise ValueError(
                "an elaborating audit requires candidate theorem and representation links"
            )
        if self.recommended_quality_tier not in {
            QualityTier.PROVISIONAL,
            QualityTier.UNKNOWN,
        }:
            raise ValueError(
                "a mechanical TransformationAudit may recommend only provisional or unknown; "
                "it cannot self-promote semantic quality"
            )
        return self


class FamilyPromotionDecision(StrictModel):
    """Immutable family-level decision bound to registry, policy, and audit inputs."""

    schema_version: Literal[1] = TRANSFORMATION_SCHEMA_VERSION
    decision_id: str = Field(pattern=id_pattern("promotion"))
    family_id: str = Field(pattern=FAMILY_ID_PATTERN)
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    audit_id: str = Field(pattern=id_pattern(AUDIT_PREFIX))
    parent_registry_hash: str = Field(pattern=HEX64_PATTERN)
    promotion_policy_hash: str = Field(pattern=HEX64_PATTERN)
    audit_manifest_hash: str = Field(pattern=HEX64_PATTERN)
    audit_input_hash: str = Field(pattern=HEX64_PATTERN)
    audit_result_hash: str = Field(pattern=HEX64_PATTERN)
    selected_count: int = Field(ge=0, strict=True)
    denominator_n: int = Field(ge=0, strict=True)
    successes: int = Field(ge=0, strict=True)
    point_precision: float = Field(ge=0.0, le=1.0, strict=True)
    clopper_pearson_lower_95: float = Field(ge=0.0, le=1.0, strict=True)
    blinded: bool = Field(strict=True)
    design_frozen_before_audit: bool = Field(strict=True)
    all_invariants_hold: bool = Field(strict=True)
    held_out_source_domain_audit_passed: bool = Field(strict=True)
    recurrent_semantic_erasure_patterns: tuple[str, ...] = ()
    decision: TransformationFamilyStatus
    unlocked_quality_tier: QualityTier | None = None
    reason_codes: tuple[str, ...] = ()
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bound_decision(self) -> FamilyPromotionDecision:
        _check_nonempty_text(self.rule_id, field_name="rule_id")
        _check_nonempty_text(self.rule_version, field_name="rule_version")
        _check_nonempty_text(self.policy_version, field_name="policy_version")
        _check_sorted_unique(
            self.recurrent_semantic_erasure_patterns,
            field_name="recurrent_semantic_erasure_patterns",
        )
        _check_sorted_unique(self.reason_codes, field_name="reason_codes")
        if self.successes > self.denominator_n:
            raise ValueError("successes cannot exceed denominator_n")
        if self.denominator_n > self.selected_count:
            raise ValueError("denominator_n cannot exceed selected_count")
        expected_precision = self.successes / self.denominator_n if self.denominator_n else 0.0
        if abs(self.point_precision - expected_precision) > 1e-12:
            raise ValueError("point_precision does not equal successes / denominator_n")

        if self.decision == TransformationFamilyStatus.GOLD_PROMOTED:
            if self.unlocked_quality_tier != QualityTier.GOLD_CONSERVATIVE_TRANSFORM:
                raise ValueError("gold_promoted must unlock exactly gold_conservative_transform")
            if self.reason_codes:
                raise ValueError("gold_promoted decision cannot carry failure reason_codes")
            if self.recurrent_semantic_erasure_patterns:
                raise ValueError(
                    "gold_promoted decision cannot carry recurrent semantic-erasure patterns"
                )
            if self.denominator_n < 200:
                raise ValueError("gold_promoted requires an eligible denominator of at least 200")
            if self.successes * 100 < 99 * self.denominator_n:
                raise ValueError("gold_promoted requires point precision at least 0.99")
            if self.clopper_pearson_lower_95 < 0.95:
                raise ValueError("gold_promoted requires Clopper-Pearson lower bound at least 0.95")
            if not (
                self.blinded
                and self.design_frozen_before_audit
                and self.all_invariants_hold
                and self.held_out_source_domain_audit_passed
            ):
                raise ValueError("gold_promoted requires every bound audit condition to pass")
        else:
            if self.unlocked_quality_tier is not None:
                raise ValueError("a non-gold family decision cannot unlock a semantic quality tier")
            if not self.reason_codes:
                raise ValueError("a non-gold family decision requires at least one reason_code")
        return self


class VariantRecord(StrictModel):
    """A generated candidate statement with full provenance (§11.5)."""

    schema_version: Literal[1] = TRANSFORMATION_SCHEMA_VERSION
    variant_id: str = Field(pattern=id_pattern(VARIANT_PREFIX))
    source_theorem_ids: tuple[str, ...] = ()
    source_representation_ids: tuple[str, ...] = ()
    context_id: str | None = Field(default=None, pattern=id_pattern(CONTEXT_PREFIX))
    generator_kind: GeneratorKind
    generator_id: str
    generation_config_hash: str = Field(pattern=HEX64_PATTERN)
    seed: int | None = None
    prompt_artifact: str | None = None
    raw_output_artifact: str | None = None
    extracted_statement: str | None = None
    candidate_code_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    transformation_attempt_id: str | None = Field(default=None, pattern=id_pattern("attempt"))
    draft_id: str | None = Field(default=None, pattern=id_pattern(DRAFT_PREFIX))
    audit_id: str | None = Field(default=None, pattern=id_pattern(AUDIT_PREFIX))
    family_id: str | None = Field(default=None, pattern=FAMILY_ID_PATTERN)
    rule_id: str | None = Field(default=None, min_length=1)
    rule_version: str | None = Field(default=None, min_length=1)
    derived_representation_id: str | None = Field(
        default=None, pattern=id_pattern(REPRESENTATION_PREFIX)
    )
    intended_relation: IntendedRelation
    intended_error_types: tuple[str, ...] = ()
    formalrx_sci_requested: str | None = None
    formalrx_sci_validated: str | None = None
    formalrx_sci_validation_status: SCIValidationStatus = "not_requested"
    formalrx_sci_proposer_family: str | None = None
    formalrx_sci_validator_family: str | None = None
    candidate_pool: str = Field(min_length=1)
    transformation_trace: tuple[dict[str, JsonValue], ...] = ()
    inverse_trace: tuple[dict[str, JsonValue], ...] | None = None
    validation_status: ValidationStatus = ValidationStatus.UNVALIDATED
    validation_evidence_id: str | None = Field(default=None, pattern=id_pattern(EVIDENCE_PREFIX))
    derived_theorem_id: str | None = Field(default=None, pattern=id_pattern(THEOREM_PREFIX))
    quality_tier: QualityTier = QualityTier.PROVISIONAL
    polarity_metadata: Polarity = Polarity.UNKNOWN
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> VariantRecord:
        if (
            self.generator_kind == GeneratorKind.DETERMINISTIC_TRANSFORM
            and not self.source_theorem_ids
        ):
            raise ValueError("a deterministic transform variant requires source theorem IDs")
        _check_source_links(
            theorem_ids=self.source_theorem_ids,
            representation_ids=self.source_representation_ids,
            allow_missing_representations=(
                self.generator_kind != GeneratorKind.DETERMINISTIC_TRANSFORM
            ),
        )
        _check_nonempty_text(self.generator_id, field_name="generator_id")
        _check_nonempty_text(self.candidate_pool, field_name="candidate_pool")
        _check_sorted_unique(self.intended_error_types, field_name="intended_error_types")
        _check_json_shaped(self.transformation_trace, field_name="transformation_trace")
        _check_json_shaped(self.inverse_trace, field_name="inverse_trace")
        _check_ecodes(self.intended_error_types)
        _check_sci_provenance(
            requested=self.formalrx_sci_requested,
            validated=self.formalrx_sci_validated,
            status=self.formalrx_sci_validation_status,
            proposer_family=self.formalrx_sci_proposer_family,
            validator_family=self.formalrx_sci_validator_family,
        )
        if self.quality_tier not in (QualityTier.PROVISIONAL, QualityTier.UNKNOWN):
            raise ValueError(
                "a VariantRecord stays provisional until resolution; supervised quality "
                "comes only from ResolvedLabel (§11.5)"
            )
        if self.candidate_code_hash is not None:
            if self.extracted_statement is None:
                raise ValueError("candidate_code_hash requires extracted_statement")
            if self.candidate_code_hash != _candidate_code_hash(self.extracted_statement):
                raise ValueError(
                    "candidate_code_hash does not match extracted_statement UTF-8 bytes"
                )
        if self.derived_representation_id is not None and self.derived_theorem_id is None:
            raise ValueError("derived_representation_id requires derived_theorem_id")

        if self.generator_kind == GeneratorKind.DETERMINISTIC_TRANSFORM:
            required_transform_fields = {
                "source_representation_ids": self.source_representation_ids,
                "context_id": self.context_id,
                "seed": self.seed,
                "extracted_statement": self.extracted_statement,
                "candidate_code_hash": self.candidate_code_hash,
                "transformation_attempt_id": self.transformation_attempt_id,
                "draft_id": self.draft_id,
                "audit_id": self.audit_id,
                "family_id": self.family_id,
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
            }
            missing = sorted(
                name
                for name, value in required_transform_fields.items()
                if value is None or value == ()
            )
            if missing:
                raise ValueError(
                    "a deterministic transform variant requires complete lineage fields: "
                    + ", ".join(missing)
                )
            if self.generator_id != self.rule_id:
                raise ValueError("deterministic generator_id must equal rule_id")
            if not self.transformation_trace:
                raise ValueError(
                    "a deterministic transform variant requires a nonempty transformation_trace"
                )
            if self.validation_status in {
                ValidationStatus.ELABORATES,
                ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            } and (self.derived_theorem_id is None or self.derived_representation_id is None):
                raise ValueError(
                    "an elaborating deterministic variant requires derived theorem and "
                    "representation links"
                )
        return self


def check_deterministic_variant_lineage(
    variant: VariantRecord,
    draft: VariantDraft,
    audit: TransformationAudit,
    attempt: TransformationAttempt,
) -> list[str]:
    """Return exact cross-record lineage violations for a deterministic variant.

    Model validators make deterministic records fail closed on missing fields;
    this helper verifies that their references identify the *same* immutable
    attempt, draft, audit, source representations, rule, and candidate.

    Attempt applicability records the complete pre-generation source assessment.
    Audit applicability records the selected draft site's mechanical audit
    requirements.  Both must be applicable for generated lineage, but their
    matched-node and metadata payloads are intentionally not required to be
    byte-identical.
    """

    violations: list[str] = []
    if variant.generator_kind != GeneratorKind.DETERMINISTIC_TRANSFORM:
        violations.append("variant_not_deterministic_transform")
        return violations

    comparisons: tuple[tuple[str, object, object], ...] = (
        ("variant.draft_id", variant.draft_id, draft.draft_id),
        ("variant.audit_id", variant.audit_id, audit.audit_id),
        (
            "variant.transformation_attempt_id",
            variant.transformation_attempt_id,
            attempt.attempt_id,
        ),
        ("source_theorem_ids", variant.source_theorem_ids, draft.source_theorem_ids),
        (
            "source_representation_ids",
            variant.source_representation_ids,
            draft.source_representation_ids,
        ),
        ("context_id", variant.context_id, draft.context_id),
        ("family_id", variant.family_id, draft.family_id),
        ("rule_id", variant.rule_id, draft.rule_id),
        ("rule_version", variant.rule_version, draft.rule_version),
        ("seed", variant.seed, draft.seed),
        (
            "generation_config_hash",
            variant.generation_config_hash,
            draft.generation_config_hash,
        ),
        ("candidate_code_hash", variant.candidate_code_hash, draft.candidate_code_hash),
        ("candidate_code", variant.extracted_statement, draft.candidate_code),
        ("intended_relation", variant.intended_relation, draft.intended_relation),
        ("intended_error_types", variant.intended_error_types, draft.intended_error_types),
        (
            "formalrx_sci_requested",
            variant.formalrx_sci_requested,
            draft.formalrx_sci_requested,
        ),
        (
            "formalrx_sci_validated",
            variant.formalrx_sci_validated,
            draft.formalrx_sci_validated,
        ),
        (
            "formalrx_sci_validation_status",
            variant.formalrx_sci_validation_status,
            draft.formalrx_sci_validation_status,
        ),
        (
            "formalrx_sci_proposer_family",
            variant.formalrx_sci_proposer_family,
            draft.formalrx_sci_proposer_family,
        ),
        (
            "formalrx_sci_validator_family",
            variant.formalrx_sci_validator_family,
            draft.formalrx_sci_validator_family,
        ),
        ("candidate_pool", variant.candidate_pool, draft.candidate_pool),
        ("transformation_trace", variant.transformation_trace, draft.transformation_trace),
        ("inverse_trace", variant.inverse_trace, draft.inverse_trace),
        ("attempt.family_id", attempt.family_id, draft.family_id),
        ("attempt.rule_id", attempt.rule_id, draft.rule_id),
        ("attempt.rule_version", attempt.rule_version, draft.rule_version),
        ("attempt.source_theorem_ids", attempt.source_theorem_ids, draft.source_theorem_ids),
        (
            "attempt.source_representation_ids",
            attempt.source_representation_ids,
            draft.source_representation_ids,
        ),
        ("attempt.context_id", attempt.context_id, draft.context_id),
        (
            "attempt.generation_config_hash",
            attempt.generation_config_hash,
            draft.generation_config_hash,
        ),
        ("attempt.seed", attempt.seed, draft.seed),
        ("audit.draft_id", audit.draft_id, draft.draft_id),
        ("audit.family_id", audit.family_id, draft.family_id),
        ("audit.rule_id", audit.rule_id, draft.rule_id),
        ("audit.rule_version", audit.rule_version, draft.rule_version),
        ("audit.context_id", audit.context_id, draft.context_id),
        ("audit.candidate_code_hash", audit.candidate_code_hash, draft.candidate_code_hash),
        (
            "audit.candidate_theorem_id",
            audit.candidate_theorem_id,
            variant.derived_theorem_id,
        ),
        (
            "audit.candidate_representation_id",
            audit.candidate_representation_id,
            variant.derived_representation_id,
        ),
        (
            "audit.recommended_validation_status",
            audit.recommended_validation_status,
            variant.validation_status,
        ),
        (
            "audit.recommended_quality_tier",
            audit.recommended_quality_tier,
            variant.quality_tier,
        ),
    )
    for field_name, actual, expected in comparisons:
        if actual != expected:
            violations.append(f"{field_name}_mismatch")
    if draft.draft_id not in attempt.draft_ids:
        violations.append("draft_missing_from_attempt")
    if attempt.terminal_outcome != "generated":
        violations.append("attempt_not_generated")
    return sorted(set(violations))
