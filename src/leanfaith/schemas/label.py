"""ResolvedLabel and cross-record label-link checks (PLAN.md §11.8, §3.5, §14).

Labels are created only by the resolver; evidence records never mutate them.
The invariants here are schema-mechanical (§11.8); policy-level resolution
precedence lives in the LF-024 resolver.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import (
    Decision,
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    LABEL_PREFIX,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    id_pattern,
)
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

_RELATIONS_FOR_NOT_SAME = frozenset(
    {
        RelationLabel.A_STRONGER,
        RelationLabel.B_STRONGER,
        RelationLabel.INCOMPARABLE,
        RelationLabel.UNRELATED,
    }
)

_AMBIGUOUS_TIERS = frozenset({QualityTier.GOLD_HUMAN, QualityTier.BENCHMARK})


class FaithfulnessLevels(StrictModel):
    """F0/F1/F2 (§3.2): related but distinct levels stored side by side."""

    F0_representation_equivalent: bool | None = None
    F1_same_claim: bool | None = None
    F2_truth_equivalent: bool | None = None


class ResolvedLabel(StrictModel):
    """The single label artifact for one pair or NL-Lean record (§11.8)."""

    schema_version: Literal[2] = CURRENT_RECORD_SCHEMA_VERSION
    label_id: str = Field(pattern=id_pattern(LABEL_PREFIX))
    target_kind: SemanticLabelTargetKind
    target_id: str
    same_claim: bool | None
    resolution_outcome: ResolutionOutcome
    relation: RelationLabel | None
    faithfulness_levels: FaithfulnessLevels
    truth_A_implies_B: bool | None = None
    truth_B_implies_A: bool | None = None
    error_types: tuple[str, ...] = ()
    quality_tier: QualityTier
    resolution_method: str | None
    evidence_ids_used: tuple[str, ...] = ()
    adjudication_notes: str = ""
    requires_adjudication: bool
    train_eligibility: bool
    eval_eligibility: bool
    policy_version: str
    decision: Decision | None = None
    relation_provenance: tuple[str, ...] = ()
    migration_metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_v1(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if data.get("schema_version") == LEGACY_RECORD_SCHEMA_VERSION:
            legacy_relation = data.get("relation")
            relation, provenance = migrate_legacy_relation(legacy_relation)
            data["schema_version"] = CURRENT_RECORD_SCHEMA_VERSION
            data["relation"] = relation
            if provenance:
                existing = tuple(data.get("relation_provenance", ()))
                data["relation_provenance"] = tuple(dict.fromkeys((*existing, *provenance)))
            metadata = dict(data.get("migration_metadata", {}))
            metadata.update(
                {
                    "source_schema_version": LEGACY_RECORD_SCHEMA_VERSION,
                    "legacy_relation": str(legacy_relation),
                }
            )
            data["migration_metadata"] = metadata
            if relation is None:
                data.update(
                    {
                        "same_claim": None,
                        "resolution_outcome": ResolutionOutcome.UNRESOLVED,
                        "quality_tier": QualityTier.UNKNOWN,
                        "requires_adjudication": True,
                        "train_eligibility": False,
                        "eval_eligibility": False,
                    }
                )
                levels = dict(data.get("faithfulness_levels", {}))
                levels["F1_same_claim"] = None
                data["faithfulness_levels"] = levels
        if data.get("resolution_outcome") == ResolutionOutcome.UNRESOLVED:
            data.setdefault("decision", Decision.REVIEW)
        return data

    @model_validator(mode="after")
    def _invariants(self) -> ResolvedLabel:
        # target integrity
        pattern = _TARGET_ID_PATTERNS[self.target_kind]
        if not re.match(pattern, self.target_id):
            raise ValueError(
                f"target_id {self.target_id!r} does not match target_kind {self.target_kind}"
            )
        ev = id_pattern(EVIDENCE_PREFIX)
        for evidence_id in self.evidence_ids_used:
            if not re.match(ev, evidence_id):
                raise ValueError(f"evidence ID {evidence_id!r} is not an 'ev:' ID")
        _check_ecodes(self.error_types)

        # F1 == same_claim (§11.8)
        if self.faithfulness_levels.F1_same_claim != self.same_claim:
            raise ValueError("F1_same_claim must equal same_claim (§11.8)")

        # outcome ↔ same_claim (§3.1, §11.8)
        outcome = self.resolution_outcome
        if outcome == ResolutionOutcome.SAME_CLAIM and self.same_claim is not True:
            raise ValueError("resolution_outcome=same_claim requires same_claim=true")
        if outcome == ResolutionOutcome.NOT_SAME_CLAIM and self.same_claim is not False:
            raise ValueError("resolution_outcome=not_same_claim requires same_claim=false")
        if (
            outcome in (ResolutionOutcome.AMBIGUOUS, ResolutionOutcome.UNRESOLVED)
            and self.same_claim is not None
        ):
            raise ValueError(f"resolution_outcome={outcome} requires same_claim=null (§3.1)")

        # review route vs terminal ambiguity (§3.5)
        if outcome == ResolutionOutcome.UNRESOLVED and (
            self.quality_tier != QualityTier.UNKNOWN
            or not self.requires_adjudication
            or self.decision != Decision.REVIEW
        ):
            raise ValueError(
                "unresolved review route requires quality_tier=unknown and "
                "requires_adjudication=true and decision=REVIEW (§3.5)"
            )
        if outcome == ResolutionOutcome.AMBIGUOUS and (
            self.quality_tier not in _AMBIGUOUS_TIERS or self.requires_adjudication
        ):
            raise ValueError(
                "terminal ambiguity requires quality_tier gold_human|benchmark and "
                "requires_adjudication=false (§3.5)"
            )

        # relation consistency (§3.3, §14.2)
        if self.same_claim is True and self.relation != RelationLabel.EQUIVALENT:
            raise ValueError("same_claim=true requires relation=equivalent")
        if self.same_claim is False and self.relation not in _RELATIONS_FOR_NOT_SAME:
            raise ValueError(
                "same_claim=false requires a directional/incomparable/unrelated relation"
            )
        if outcome == ResolutionOutcome.AMBIGUOUS and self.relation != RelationLabel.AMBIGUOUS:
            raise ValueError("terminal ambiguity requires relation=ambiguous")
        if outcome == ResolutionOutcome.UNRESOLVED and self.relation is not None:
            raise ValueError("unresolved review route requires relation=null")

        # F2 derives mechanically from accepted truth evidence (§14.7)
        f2 = self.faithfulness_levels.F2_truth_equivalent
        truths = (self.truth_A_implies_B, self.truth_B_implies_A)
        if truths[0] is True and truths[1] is True:
            expected_f2: bool | None = True
        elif truths[0] is False or truths[1] is False:
            expected_f2 = False
        else:
            expected_f2 = None
        if f2 != expected_f2:
            raise ValueError(
                f"F2_truth_equivalent={f2} inconsistent with directional truths {truths} (§14.7)"
            )
        if self.same_claim is True and expected_f2 is False:
            raise ValueError(
                "same_claim=true contradicts an accepted refuted truth direction; "
                "conflicting strong evidence routes to review, never into a positive "
                "label (§14.6, §16.8)"
            )

        # eligibility vs tier (§14.5)
        if self.quality_tier == QualityTier.UNKNOWN and self.train_eligibility:
            raise ValueError("quality_tier=unknown provides no binary training target (§14.5)")
        if self.quality_tier == QualityTier.PROVISIONAL and self.eval_eligibility:
            raise ValueError("provisional labels are never evaluation-eligible (§14.5)")

        # equivalent labels may carry at most cosmetic error codes (§14.4)
        if self.same_claim is True and any(code != "E29" for code in self.error_types):
            raise ValueError("same_claim=true admits only E29 (cosmetic) error codes (§14.4)")
        return self


def check_label_target_link(label: ResolvedLabel, target: object) -> list[str]:
    """Cross-record §11.8 one-to-one link check; returns violations.

    ``target`` is a PairRecord or NLPLeanRecord (typed as ``object`` to avoid
    import cycles; the caller passes the record).
    """
    violations: list[str] = []
    target_id = getattr(target, "pair_id", None) or getattr(target, "nl_lean_id", None)
    if target_id is None:
        return [f"target of type {type(target).__name__} has no pair_id/nl_lean_id"]
    expected_kind = (
        SemanticLabelTargetKind.LEAN_PAIR
        if hasattr(target, "pair_id")
        else SemanticLabelTargetKind.NL_LEAN
    )
    if label.target_kind != expected_kind:
        violations.append(f"label target_kind {label.target_kind} != record kind {expected_kind}")
    if label.target_id != target_id:
        violations.append(f"label target_id {label.target_id} != record ID {target_id}")
    reverse = getattr(target, "resolved_label_id", None)
    if reverse != label.label_id:
        violations.append(
            f"record resolved_label_id {reverse!r} does not point back to {label.label_id}"
        )
    return violations
