"""EvidenceRecord and kind-specific values (PLAN.md §11.7, §16.2).

Evidence never mutates labels; the resolver creates a new label artifact
(§5.1). ``not_proved`` and counterexample ``not_found`` never map to a
negative label (§11.7).
"""

from __future__ import annotations

import datetime
import re
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import (
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
)
from leanfaith.schemas.ids import (
    DRAFT_PREFIX,
    EVIDENCE_PREFIX,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    THEOREM_PREFIX,
    id_pattern,
)
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.variant import FAMILY_ID_PATTERN, _check_ecodes

MetadataValue = str | int | float | bool | None

_TARGET_ID_PATTERNS: dict[EvidenceTargetKind, str] = {
    EvidenceTargetKind.THEOREM: id_pattern(THEOREM_PREFIX),
    EvidenceTargetKind.LEAN_PAIR: id_pattern(PAIR_PREFIX),
    EvidenceTargetKind.NL_LEAN: id_pattern(NL_LEAN_PREFIX),
    EvidenceTargetKind.TRANSFORMATION_DRAFT: id_pattern(DRAFT_PREFIX),
    EvidenceTargetKind.TRANSFORMATION_FAMILY: FAMILY_ID_PATTERN,
}


class TypecheckValue(StrictModel):
    kind: Literal["typecheck"] = "typecheck"
    outcome: Literal["valid", "valid_with_sorry", "invalid"]


class DefeqValue(StrictModel):
    kind: Literal["defeq"] = "defeq"
    outcome: Literal["equal", "not_equal"]


class ProofValue(StrictModel):
    """Closed whole-proposition proof outcome; populates F2 only (§16.3)."""

    kind: Literal["proof"] = "proof"
    outcome: Literal["proved", "not_proved"]
    tactic: str | None = None
    axioms: tuple[str, ...] = ()


class ClaimAlignmentValue(StrictModel):
    """Binder-aligned claim certificate (§11.7, §16.3 mode 2)."""

    kind: Literal["claim_alignment"] = "claim_alignment"
    alignment_version: str
    binder_map: dict[str, str]
    premise_map: dict[str, str]
    conclusion_role_map: dict[str, str]
    direction: Literal["A_to_B", "B_to_A", "both"]
    outcome: Literal["certified", "rejected", "unsupported"]


class CounterexampleValue(StrictModel):
    kind: Literal["counterexample"] = "counterexample"
    outcome: Literal["found", "not_found", "unsupported"]
    domain: str | None = None
    encoding: str | None = None
    witness_artifact: str | None = None
    axioms: tuple[str, ...] = ()


class JudgmentValue(StrictModel):
    """Canonicalized LLM/human judgment fields (§11.7)."""

    kind: Literal["judgment"] = "judgment"
    answer: str
    relation: str | None = None
    a_implies_b: Literal["yes", "no", "unknown"] | None = None
    b_implies_a: Literal["yes", "no", "unknown"] | None = None
    error_types: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str | None = None
    needs_expert_review: bool | None = None

    @model_validator(mode="after")
    def _codes(self) -> JudgmentValue:
        _check_ecodes(self.error_types)
        return self


class AuditValue(StrictModel):
    """Transformation/axiom audit summary payload."""

    kind: Literal["audit"] = "audit"
    checks: dict[str, bool | None] = Field(default_factory=dict)
    violation_codes: tuple[str, ...] = ()
    detail_artifact: str | None = None


EvidenceValue = (
    TypecheckValue
    | DefeqValue
    | ProofValue
    | ClaimAlignmentValue
    | CounterexampleValue
    | JudgmentValue
    | AuditValue
)

_KIND_TO_VALUE_TYPE: dict[EvidenceKind, type[StrictModel]] = {
    EvidenceKind.TYPECHECK: TypecheckValue,
    EvidenceKind.DEFEQ: DefeqValue,
    EvidenceKind.PROOF_A_IMPLIES_B: ProofValue,
    EvidenceKind.PROOF_B_IMPLIES_A: ProofValue,
    EvidenceKind.CLAIM_ALIGNMENT: ClaimAlignmentValue,
    EvidenceKind.COUNTEREXAMPLE: CounterexampleValue,
    EvidenceKind.AXIOM_AUDIT: AuditValue,
    EvidenceKind.LLM_JUDGMENT: JudgmentValue,
    EvidenceKind.HUMAN_ANNOTATION: JudgmentValue,
    EvidenceKind.TRANSFORMATION_AUDIT: AuditValue,
}

#: Evidence kinds meaningful only for pair-like targets.
_PAIR_ONLY_KINDS = frozenset(
    {
        EvidenceKind.DEFEQ,
        EvidenceKind.PROOF_A_IMPLIES_B,
        EvidenceKind.PROOF_B_IMPLIES_A,
        EvidenceKind.CLAIM_ALIGNMENT,
        EvidenceKind.COUNTEREXAMPLE,
    }
)


class EvidenceRecord(StrictModel):
    """One evidence observation about one target (§11.7)."""

    schema_version: int = 1
    evidence_id: str = Field(pattern=id_pattern(EVIDENCE_PREFIX))
    target_kind: EvidenceTargetKind
    target_id: str
    kind: EvidenceKind
    status: EvidenceExecutionStatus
    value: EvidenceValue | None = None
    method_version: str
    config_hash: str | None = None
    raw_artifact: str | None = None
    created_at: datetime.datetime
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> EvidenceRecord:
        require_utc(self.created_at)
        pattern = _TARGET_ID_PATTERNS[self.target_kind]
        if not re.match(pattern, self.target_id):
            raise ValueError(
                f"target_id {self.target_id!r} does not match target_kind "
                f"{self.target_kind} pattern {pattern}"
            )
        if self.status == EvidenceExecutionStatus.SUCCESS and self.value is None:
            raise ValueError("successful evidence must carry a value (§11.7)")
        if self.status == EvidenceExecutionStatus.NOT_RUN and self.value is not None:
            raise ValueError("not_run evidence cannot carry a value")
        if self.value is not None:
            expected = _KIND_TO_VALUE_TYPE[self.kind]
            if not isinstance(self.value, expected):
                raise ValueError(
                    f"evidence kind {self.kind} requires value type {expected.__name__}, "
                    f"got {type(self.value).__name__}"
                )
        if self.kind in _PAIR_ONLY_KINDS and self.target_kind != EvidenceTargetKind.LEAN_PAIR:
            raise ValueError(
                f"evidence kind {self.kind} compares two sides and requires a lean_pair "
                f"target, got {self.target_kind}"
            )
        return self
