"""PairRecord (PLAN.md §11.6, §19.5)."""

from __future__ import annotations

import re

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import IntendedRelation
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    LABEL_PREFIX,
    PAIR_PREFIX,
    THEOREM_PREFIX,
    id_pattern,
)
from leanfaith.schemas.theorem import TheoremRecord

MetadataValue = str | int | float | bool | None


class PairRecord(StrictModel):
    """One Lean-Lean comparison; labels/evidence are referenced, never inlined (§11.6)."""

    schema_version: int = 1
    pair_id: str = Field(pattern=id_pattern(PAIR_PREFIX))
    theorem_a_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    theorem_b_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    pair_source: str
    nl_problem_group: str | None = None
    split_group_ids: tuple[str, ...] = Field(min_length=1)
    generator_id: str | None = None
    transformation_family: str | None = None
    intended_relation: IntendedRelation | None = None
    resolved_label_id: str | None = Field(default=None, pattern=id_pattern(LABEL_PREFIX))
    evidence_ids: tuple[str, ...] = ()
    lexical_stats: dict[str, float] = Field(default_factory=dict)
    structural_stats: dict[str, float] = Field(default_factory=dict)
    split_eligible: bool = True
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> PairRecord:
        if list(self.split_group_ids) != sorted(set(self.split_group_ids)):
            raise ValueError("split_group_ids must be sorted and unique (§11.6)")
        ev = id_pattern(EVIDENCE_PREFIX)
        for evidence_id in self.evidence_ids:
            if not re.match(ev, evidence_id):
                raise ValueError(f"evidence ID {evidence_id!r} is not an 'ev:' ID")
        return self


def check_pair_groups(
    pair: PairRecord, theorem_a: TheoremRecord, theorem_b: TheoremRecord
) -> list[str]:
    """Cross-record §19.5 check: return violations of the split-group union rule.

    ``split_group_ids`` must contain both sides' root ancestries and, when the
    pair has an NL problem group, that group as well. Returns a list of
    human-readable violations (empty when consistent).
    """
    violations: list[str] = []
    if pair.theorem_a_id != theorem_a.theorem_id:
        violations.append(f"theorem_a_id {pair.theorem_a_id} does not match {theorem_a.theorem_id}")
    if pair.theorem_b_id != theorem_b.theorem_id:
        violations.append(f"theorem_b_id {pair.theorem_b_id} does not match {theorem_b.theorem_id}")
    groups = set(pair.split_group_ids)
    for side, theorem in (("A", theorem_a), ("B", theorem_b)):
        for root in theorem.root_ancestry_ids:
            if root not in groups:
                violations.append(f"side {side} root ancestry {root} missing from split_group_ids")
    if pair.nl_problem_group is not None and pair.nl_problem_group not in groups:
        violations.append(f"nl_problem_group {pair.nl_problem_group} missing from split_group_ids")
    return violations
