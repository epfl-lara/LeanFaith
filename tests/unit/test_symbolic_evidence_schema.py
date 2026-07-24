"""LF-020 evidence execution-status and semantic-value separation."""

from __future__ import annotations

import datetime

import pytest
from pydantic import ValidationError

from leanfaith.schemas.enums import (
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
)
from leanfaith.schemas.evidence import (
    ClaimAlignmentValue,
    CounterexampleValue,
    DefeqValue,
    EvidenceRecord,
    ProofValue,
)
from leanfaith.schemas.ids import EVIDENCE_PREFIX, PAIR_PREFIX, make_id

PAIR_ID = make_id(PAIR_PREFIX, {"suite": "symbolic-evidence"})


def _record(
    *,
    kind: EvidenceKind,
    status: EvidenceExecutionStatus,
    value: object,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=make_id(
            EVIDENCE_PREFIX,
            {"kind": kind.value, "status": status.value, "value": repr(value)},
        ),
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        kind=kind,
        status=status,
        value=value,
        method_version="symbolic_evidence_test_v1",
        config_hash="a" * 64,
        created_at=datetime.datetime(2026, 7, 23, tzinfo=datetime.UTC),
    )


@pytest.mark.parametrize(
    "status",
    [
        EvidenceExecutionStatus.NOT_RUN,
        EvidenceExecutionStatus.TIMEOUT,
        EvidenceExecutionStatus.ERROR,
        EvidenceExecutionStatus.ABSTAIN,
    ],
)
def test_nonsemantic_execution_statuses_reject_values(
    status: EvidenceExecutionStatus,
) -> None:
    with pytest.raises(ValidationError, match="cannot carry a semantic value"):
        _record(
            kind=EvidenceKind.DEFEQ,
            status=status,
            value=DefeqValue(outcome="not_equal"),
        )


def test_unsupported_proof_has_no_value() -> None:
    record = _record(
        kind=EvidenceKind.PROOF_A_IMPLIES_B,
        status=EvidenceExecutionStatus.UNSUPPORTED,
        value=None,
    )
    assert record.value is None


def test_unsupported_counterexample_is_explicit_but_never_successful() -> None:
    unsupported = CounterexampleValue(
        outcome="unsupported",
        direction="equivalence_only",
        encoding="kernel_decide_v1",
    )
    assert (
        _record(
            kind=EvidenceKind.COUNTEREXAMPLE,
            status=EvidenceExecutionStatus.UNSUPPORTED,
            value=unsupported,
        ).value
        == unsupported
    )
    with pytest.raises(ValidationError, match="requires status=unsupported"):
        _record(
            kind=EvidenceKind.COUNTEREXAMPLE,
            status=EvidenceExecutionStatus.SUCCESS,
            value=unsupported,
        )


def test_unsupported_claim_alignment_requires_matching_outcome() -> None:
    rejected = ClaimAlignmentValue(
        alignment_version="alignment_v1",
        binder_map={},
        premise_map={},
        conclusion_role_map={"A": "B"},
        direction="both",
        outcome="rejected",
    )
    with pytest.raises(ValidationError, match="outcome=unsupported"):
        _record(
            kind=EvidenceKind.CLAIM_ALIGNMENT,
            status=EvidenceExecutionStatus.UNSUPPORTED,
            value=rejected,
        )


def test_not_proved_remains_a_successful_search_outcome_not_a_negative_label() -> None:
    value = ProofValue(outcome="not_proved")
    record = _record(
        kind=EvidenceKind.PROOF_A_IMPLIES_B,
        status=EvidenceExecutionStatus.SUCCESS,
        value=value,
    )
    assert isinstance(record.value, ProofValue)
    assert record.value.outcome == "not_proved"
    assert "label" not in record.model_dump(mode="json")


def test_found_counterexample_requires_witness_but_not_found_does_not() -> None:
    with pytest.raises(ValidationError, match="requires domain, encoding, and witness"):
        CounterexampleValue(
            outcome="found",
            direction="A_to_B",
            domain="Bool",
            encoding="kernel_decide_v1",
        )
    value = CounterexampleValue(
        outcome="not_found",
        direction="equivalence_only",
        domain="Fin 8",
        encoding="kernel_decide_v1",
    )
    record = _record(
        kind=EvidenceKind.COUNTEREXAMPLE,
        status=EvidenceExecutionStatus.SUCCESS,
        value=value,
    )
    assert isinstance(record.value, CounterexampleValue)
    assert record.value.outcome == "not_found"
