"""Persistent result schema for experimental LF-033 E2 materialization."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import QualityTier
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import (
    TransformationAttempt,
    TransformationAudit,
    VariantDraft,
    VariantRecord,
)

E2ProfileId = Literal[
    "deterministic_v2_e2_p14_experimental",
    "deterministic_v2_e2_p15_experimental",
    "deterministic_v2_e2_p16_experimental",
    "deterministic_v2_e2_p17_experimental",
    "deterministic_v2_e2_p18_experimental",
]
E2RuleId = Literal[
    "p14_independent_binder_permutation",
    "p15_root_iff_reversal",
    "p16_conjunction_reassociation",
    "p17_hypothesis_packing",
    "p18_root_equality_symmetry",
]

_PROFILE_RULE = {
    "deterministic_v2_e2_p14_experimental": "p14_independent_binder_permutation",
    "deterministic_v2_e2_p15_experimental": "p15_root_iff_reversal",
    "deterministic_v2_e2_p16_experimental": "p16_conjunction_reassociation",
    "deterministic_v2_e2_p17_experimental": "p17_hypothesis_packing",
    "deterministic_v2_e2_p18_experimental": "p18_root_equality_symmetry",
}


class V2E2MaterializationResult(StrictModel):
    """One terminal E2 attempt with zero semantic or training credit."""

    schema_version: Literal[1] = 1
    result_id: str
    profile_id: E2ProfileId
    profile_config_hash: str
    rule_id: E2RuleId
    evidence_class: Literal["E2"] = "E2"
    terminal_status: Literal[
        "not_applicable",
        "no_output",
        "candidate_invalid",
        "candidate_infrastructure_error",
        "candidate_representation_failed",
        "audit_quarantined",
        "provisional_variant",
    ]
    attempt: TransformationAttempt
    draft: VariantDraft | None = None
    candidate_theorem: TheoremRecord | None = None
    candidate_representation: RepresentationRecord | None = None
    audit: TransformationAudit | None = None
    variant: VariantRecord | None = None
    failure_codes: tuple[str, ...] = ()
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> V2E2MaterializationResult:
        if _PROFILE_RULE[self.profile_id] != self.rule_id or self.attempt.rule_id != self.rule_id:
            raise ValueError("E2 profile, rule, and attempt identities do not align")
        if self.failure_codes != tuple(sorted(set(self.failure_codes))):
            raise ValueError("failure_codes must be sorted and unique")
        if self.terminal_status == "provisional_variant":
            if any(
                item is None
                for item in (
                    self.draft,
                    self.candidate_theorem,
                    self.candidate_representation,
                    self.audit,
                    self.variant,
                )
            ):
                raise ValueError("provisional_variant requires complete mechanical lineage")
            assert self.audit is not None
            assert self.variant is not None
            if self.audit.violation_codes:
                raise ValueError("a provisional variant cannot carry audit violations")
            if self.audit.recommended_quality_tier != QualityTier.PROVISIONAL:
                raise ValueError("v2 E2 output must remain provisional")
            if self.variant.quality_tier != QualityTier.PROVISIONAL:
                raise ValueError("v2 E2 VariantRecord must remain provisional")
            if self.audit.metadata.get("evidence_class") != "E2":
                raise ValueError("v2 E2 audit must declare E2 evidence")
        elif self.variant is not None:
            raise ValueError("only a clean provisional result may carry a VariantRecord")
        expected = make_id("v2e2_result", _result_payload(self))
        if self.result_id != expected:
            raise ValueError("v2 E2 result_id does not match its semantic payload")
        return self


def _result_payload(result: V2E2MaterializationResult) -> dict[str, object]:
    return {
        key: value for key, value in result.model_dump(mode="json").items() if key != "result_id"
    }


def build_v2_e2_result(**data: object) -> V2E2MaterializationResult:
    """Construct the result and bind its full immutable semantic payload."""

    placeholder = V2E2MaterializationResult.model_construct(
        _fields_set=None,
        result_id=f"v2e2_result:{'0' * 64}",
        **data,
    )
    return V2E2MaterializationResult.model_validate(
        {"result_id": make_id("v2e2_result", _result_payload(placeholder)), **data}
    )


__all__ = [
    "E2ProfileId",
    "E2RuleId",
    "V2E2MaterializationResult",
    "build_v2_e2_result",
]
