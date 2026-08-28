"""Non-label LF-022 aggregation records.

``WeakConsensusCandidateRecord`` is intentionally not a ``ResolvedLabel``.
It preserves agreement/disagreement for later human pilot and promotion
audits while making train/evaluation eligibility impossible in schema v1.
"""

from __future__ import annotations

import datetime
import re
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.evidence import JudgmentValue
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    LLM_CALL_PREFIX,
    PAIR_PREFIX,
    WEAK_CONSENSUS_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.manifest import require_utc

WeakConsensusStatus = Literal[
    "candidate_consensus",
    "disagreement",
    "swap_inconsistent",
    "incomplete",
    "all_abstain",
    "ambiguous_consensus",
]


def make_weak_consensus_id(
    *,
    pair_id: str,
    proposer_family: str,
    judge_families: tuple[str, str],
    judgment_evidence_ids: tuple[str, ...],
    status: WeakConsensusStatus,
) -> str:
    """Build the deterministic identity of one candidate-only aggregation."""

    return make_id(
        WEAK_CONSENSUS_PREFIX,
        {
            "schema": "weak_consensus_candidate_v1",
            "pair_id": pair_id,
            "proposer_family": proposer_family,
            "judge_families": judge_families,
            "judgment_evidence_ids": judgment_evidence_ids,
            "status": status,
        },
    )


class WeakConsensusCandidateRecord(StrictModel):
    """Two-family weak-vote aggregate awaiting human promotion policy."""

    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=id_pattern(WEAK_CONSENSUS_PREFIX))
    pair_id: str = Field(pattern=id_pattern(PAIR_PREFIX))
    proposer_family: str = Field(min_length=1)
    judge_families: tuple[str, str]
    judgment_evidence_ids: tuple[str, ...]
    llm_call_ids: tuple[str, ...]
    status: WeakConsensusStatus
    consensus_value: JudgmentValue | None = None
    promotion_blockers: tuple[str, ...] = Field(min_length=1)
    semantic_label_created: Literal[False] = False
    silver_promoted: Literal[False] = False
    train_eligible: Literal[False] = False
    eval_eligible: Literal[False] = False
    requires_adjudication: Literal[True] = True
    created_at: datetime.datetime
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _candidate_only(self) -> Self:
        require_utc(self.created_at)
        if list(self.judge_families) != sorted(set(self.judge_families)):
            raise ValueError("judge_families must contain two sorted distinct families")
        if self.proposer_family in self.judge_families:
            raise ValueError("weak judges must differ from the item proposer family")
        evidence_pattern = id_pattern(EVIDENCE_PREFIX)
        if any(re.fullmatch(evidence_pattern, item) is None for item in self.judgment_evidence_ids):
            raise ValueError("judgment_evidence_ids must contain only canonical evidence IDs")
        call_pattern = id_pattern(LLM_CALL_PREFIX)
        if any(re.fullmatch(call_pattern, item) is None for item in self.llm_call_ids):
            raise ValueError("llm_call_ids must contain only canonical call IDs")
        if len(set(self.judgment_evidence_ids)) != len(self.judgment_evidence_ids):
            raise ValueError("judgment_evidence_ids must be unique")
        if len(set(self.llm_call_ids)) != len(self.llm_call_ids):
            raise ValueError("llm_call_ids must be unique")
        if list(self.promotion_blockers) != sorted(set(self.promotion_blockers)):
            raise ValueError("promotion_blockers must be sorted and unique")
        expected_id = make_weak_consensus_id(
            pair_id=self.pair_id,
            proposer_family=self.proposer_family,
            judge_families=self.judge_families,
            judgment_evidence_ids=self.judgment_evidence_ids,
            status=self.status,
        )
        if self.candidate_id != expected_id:
            raise ValueError("candidate_id does not match aggregation content")

        if self.status == "candidate_consensus":
            if self.consensus_value is None:
                raise ValueError("candidate_consensus requires consensus_value")
            if self.consensus_value.answer not in {"same_claim", "not_same_claim"}:
                raise ValueError("only same/not-same consensus enters the promotion-candidate pool")
        elif self.consensus_value is not None:
            raise ValueError(f"{self.status} cannot carry a consensus semantic value")
        return self
