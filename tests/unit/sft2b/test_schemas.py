from __future__ import annotations

import pytest
from pydantic import ValidationError

from leanfaith.sft2b.schemas import (
    CoreRow,
    JudgeDecision,
    JudgeId,
    JudgeVote,
    MajorityDecision,
    majority_outcome,
    stable_id,
)

_SHA = "a" * 64
_CANDIDATE_ID = f"sft2b_candidate:{'b' * 64}"


def _vote(judge: JudgeId, decision: JudgeDecision) -> JudgeVote:
    payload = {
        "candidate_id": _CANDIDATE_ID,
        "judge": judge,
        "model_id": f"model-{judge.value}",
        "prompt_sha256": _SHA,
        "judge_input_sha256": _SHA,
    }
    return JudgeVote(
        vote_id=stable_id("sft2b_vote", payload),
        candidate_id=_CANDIDATE_ID,
        judge=judge,
        provider=f"provider-{judge.value}",
        model_id=f"model-{judge.value}",
        cli_version="v1",
        prompt_sha256=_SHA,
        judge_input_sha256=_SHA,
        response_sha256=_SHA,
        decision=decision,
        probability_equivalent=0.5,
        rationale="Independent intended-claim comparison.",
        relation_class="same_or_changed_claim",
        saw_expected_label=False,
        saw_other_votes=False,
    )


def test_two_equivalent_votes_route_to_true_core_label() -> None:
    votes = (
        _vote(JudgeId.CODEX, JudgeDecision.EQUIVALENT),
        _vote(JudgeId.LEMEX, JudgeDecision.EQUIVALENT),
        _vote(JudgeId.CLAUDE, JudgeDecision.UNKNOWN),
    )

    outcome = majority_outcome(_CANDIDATE_ID, votes)

    assert outcome.decision == MajorityDecision.EQUIVALENT
    assert outcome.label is True


def test_no_two_vote_semantic_majority_routes_to_unknown() -> None:
    votes = (
        _vote(JudgeId.CODEX, JudgeDecision.EQUIVALENT),
        _vote(JudgeId.LEMEX, JudgeDecision.NON_EQUIVALENT),
        _vote(JudgeId.CLAUDE, JudgeDecision.UNKNOWN),
    )

    outcome = majority_outcome(_CANDIDATE_ID, votes)

    assert outcome.decision == MajorityDecision.UNKNOWN
    assert outcome.label is None


def test_core_row_contract_is_exactly_three_fields() -> None:
    row = CoreRow(reference="x : Nat\n⊢ x = x", candidate="x : Nat\n⊢ x = x", label=True)
    assert set(row.model_dump()) == {"reference", "candidate", "label"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CoreRow.model_validate(
            {
                "reference": "⊢ True",
                "candidate": "⊢ True",
                "label": True,
                "valid": True,
            }
        )
