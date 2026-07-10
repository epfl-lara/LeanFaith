"""NLPLeanRecord (PLAN.md §11.9)."""

from __future__ import annotations

import re

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import NLTrust
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    LABEL_PREFIX,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    THEOREM_PREFIX,
    id_pattern,
)

MetadataValue = str | int | float | bool | None


class ReferencePairLink(StrictModel):
    """One candidate-vs-reference comparison, stored as its own PairRecord (§11.9)."""

    reference_theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    pair_id: str = Field(pattern=id_pattern(PAIR_PREFIX))


class NLPLeanRecord(StrictModel):
    """One NL problem x one Lean candidate, with optional references (§11.9)."""

    schema_version: int = 1
    nl_lean_id: str = Field(pattern=id_pattern(NL_LEAN_PREFIX))
    problem_id: str
    problem_group: str
    source: str
    source_revision: str
    nl_statement: str = Field(min_length=1)
    nl_trust: NLTrust
    candidate_theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    generator_id: str | None = None
    reference_theorem_ids: tuple[str, ...] = ()
    reference_pairs: tuple[ReferencePairLink, ...] = ()
    resolved_label_id: str | None = Field(default=None, pattern=id_pattern(LABEL_PREFIX))
    evidence_ids: tuple[str, ...] = ()
    split_group_ids: tuple[str, ...] = Field(min_length=1)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> NLPLeanRecord:
        thm = id_pattern(THEOREM_PREFIX)
        for reference in self.reference_theorem_ids:
            if not re.match(thm, reference):
                raise ValueError(f"reference theorem ID {reference!r} is not a 'thm:' ID")
        declared = set(self.reference_theorem_ids)
        for link in self.reference_pairs:
            if link.reference_theorem_id not in declared:
                raise ValueError(
                    f"reference pair {link.pair_id} names undeclared reference "
                    f"{link.reference_theorem_id} (§11.9)"
                )
        ev = id_pattern(EVIDENCE_PREFIX)
        for evidence_id in self.evidence_ids:
            if not re.match(ev, evidence_id):
                raise ValueError(f"evidence ID {evidence_id!r} is not an 'ev:' ID")
        if list(self.split_group_ids) != sorted(set(self.split_group_ids)):
            raise ValueError("split_group_ids must be sorted and unique (§19.5)")
        if self.problem_group not in self.split_group_ids:
            raise ValueError("split_group_ids must include the NL problem group (§19.5)")
        return self
