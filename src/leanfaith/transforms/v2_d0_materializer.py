"""Persistent result schema for experimental LF-034 D0 materialization."""

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

D0ProfileId = Literal[
    "deterministic_v2_d0_n11_experimental",
    "deterministic_v2_d0_n12_experimental",
    "deterministic_v2_d0_n13_experimental",
    "deterministic_v2_d0_n14_experimental",
    "deterministic_v2_d0_n15_experimental",
    "deterministic_v2_d0_n16_experimental",
    "deterministic_v2_d0_n17_experimental",
    "deterministic_v2_d0_n18_experimental",
]
D0RuleId = Literal[
    "n11_bound_variable_substitution",
    "n12_implication_converse",
    "n13_witness_dependency",
    "n14_negation_scope",
    "n15_conjunct_omission",
    "n16_domain_guard_removal",
    "n17_role_sensitive_arguments",
    "n18_root_equality_polarity",
]


class V2D0MaterializationResult(StrictModel):
    """One terminal D0 attempt with zero semantic or training credit."""

    schema_version: Literal[1] = 1
    result_id: str
    profile_id: D0ProfileId
    profile_config_hash: str
    rule_id: D0RuleId
    terminal_status: Literal[
        "not_applicable",
        "no_output",
        "candidate_invalid",
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
    def _coherent(self) -> V2D0MaterializationResult:
        expected_rule = {
            "deterministic_v2_d0_n11_experimental": "n11_bound_variable_substitution",
            "deterministic_v2_d0_n12_experimental": "n12_implication_converse",
            "deterministic_v2_d0_n13_experimental": "n13_witness_dependency",
            "deterministic_v2_d0_n14_experimental": "n14_negation_scope",
            "deterministic_v2_d0_n15_experimental": "n15_conjunct_omission",
            "deterministic_v2_d0_n16_experimental": "n16_domain_guard_removal",
            "deterministic_v2_d0_n17_experimental": "n17_role_sensitive_arguments",
            "deterministic_v2_d0_n18_experimental": "n18_root_equality_polarity",
        }[self.profile_id]
        if self.rule_id != expected_rule or self.attempt.rule_id != self.rule_id:
            raise ValueError("D0 profile, rule, and attempt identities do not align")
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
                raise ValueError("v2 D0 output must remain provisional")
            if self.variant.quality_tier != QualityTier.PROVISIONAL:
                raise ValueError("v2 D0 VariantRecord must remain provisional")
        elif self.variant is not None:
            raise ValueError("only a clean provisional result may carry a VariantRecord")
        expected = make_id("v2d0_result", _result_payload(self))
        if self.result_id != expected:
            raise ValueError("v2 D0 result_id does not match its semantic payload")
        return self


def _result_payload(result: V2D0MaterializationResult) -> dict[str, object]:
    return {
        key: value for key, value in result.model_dump(mode="json").items() if key != "result_id"
    }


def build_v2_d0_result(**data: object) -> V2D0MaterializationResult:
    """Construct the result and bind its full immutable semantic payload."""

    placeholder = V2D0MaterializationResult.model_construct(
        _fields_set=None,
        result_id=f"v2d0_result:{'0' * 64}",
        **data,
    )
    return V2D0MaterializationResult.model_validate(
        {"result_id": make_id("v2d0_result", _result_payload(placeholder)), **data}
    )


__all__ = [
    "D0ProfileId",
    "D0RuleId",
    "V2D0MaterializationResult",
    "build_v2_d0_result",
]
