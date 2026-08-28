"""LF-024 admission-gated mechanical evidence derivation tests."""

from __future__ import annotations

import datetime
from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from leanfaith.labeling.aggregation import (
    EvidenceAdmissionRecord,
    EvidenceAggregationError,
    EvidenceDerivationRecord,
    EvidenceFactConflictError,
    build_evidence_admission_record,
    derive_admitted_evidence,
    verify_evidence_derivation,
)
from leanfaith.schemas.enums import (
    ArtifactClass,
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
)
from leanfaith.schemas.evidence import (
    AuditValue,
    ClaimAlignmentValue,
    CounterexampleValue,
    DefeqValue,
    EvidenceRecord,
    JudgmentValue,
    ProofValue,
    TypecheckValue,
)
from leanfaith.schemas.ids import make_id

NOW = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)
PAIR_ID = make_id("pair", {"fixture": "lf024-aggregation"})
OTHER_PAIR_ID = make_id("pair", {"fixture": "lf024-aggregation-other"})


def _evidence(
    name: str,
    *,
    kind: EvidenceKind,
    value: object,
    status: EvidenceExecutionStatus = EvidenceExecutionStatus.SUCCESS,
    target_id: str = PAIR_ID,
    raw_artifact: str | None = None,
    metadata: dict[str, str | int | float | bool | None] | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=make_id("ev", {"fixture": "lf024-aggregation", "name": name}),
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=target_id,
        kind=kind,
        status=status,
        value=value,  # type: ignore[arg-type]
        method_version="lf024_aggregation_fixture_v1",
        config_hash="d" * 64,
        raw_artifact=raw_artifact,
        created_at=NOW,
        metadata=metadata or {},
    )


def _admission(
    records: Sequence[EvidenceRecord],
    *,
    target_id: str = PAIR_ID,
    artifact_class: ArtifactClass = ArtifactClass.PRODUCTION,
    replay_passed: bool = True,
    suffix: str = "default",
) -> EvidenceAdmissionRecord:
    return build_evidence_admission_record(
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=target_id,
        evidence_ids=tuple(record.evidence_id for record in records),
        artifact_class=artifact_class,
        manifest_artifact_id=f"manifest:{suffix}",
        manifest_artifact_sha256="a" * 64,
        replay_artifact_id=f"replay:{suffix}",
        replay_artifact_sha256="b" * 64,
        replay_passed=replay_passed,
        policy_sha256="c" * 64,
    )


def _derive(
    records: Sequence[EvidenceRecord],
    admissions: Sequence[EvidenceAdmissionRecord] | None = None,
) -> EvidenceDerivationRecord:
    return derive_admitted_evidence(
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        evidence_records=records,
        admissions=admissions or (_admission(records),),
    )


def _accepted_audit(
    name: str,
    *,
    raw_artifact: str | None = None,
    checks: dict[str, bool | None] | None = None,
    violations: tuple[str, ...] = (),
) -> EvidenceRecord:
    return _evidence(
        name,
        kind=EvidenceKind.AXIOM_AUDIT,
        value=AuditValue(
            checks=checks if checks is not None else {"admission_free": True},
            violation_codes=violations,
        ),
        raw_artifact=raw_artifact,
    )


def test_admission_factory_sorts_is_content_addressed_and_rejects_duplicates() -> None:
    first = _evidence("factory-first", kind=EvidenceKind.DEFEQ, value=DefeqValue(outcome="equal"))
    second = _evidence(
        "factory-second", kind=EvidenceKind.DEFEQ, value=DefeqValue(outcome="not_equal")
    )
    forward = _admission((first, second))
    backward = _admission((second, first))
    assert forward == backward
    assert forward.evidence_ids == tuple(sorted((first.evidence_id, second.evidence_id)))
    assert forward.production_eligible is True

    with pytest.raises(EvidenceAggregationError, match="duplicate evidence IDs"):
        _admission((first, first))

    payload = forward.model_dump(mode="json")
    payload["manifest_artifact_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="admission_id does not match"):
        EvidenceAdmissionRecord.model_validate(payload)


@pytest.mark.parametrize(
    ("artifact_class", "replay_passed"),
    [
        (ArtifactClass.SMOKE, True),
        (ArtifactClass.DIAGNOSTIC, True),
        (ArtifactClass.PRODUCTION, False),
    ],
)
def test_only_replayed_production_admission_is_eligible(
    artifact_class: ArtifactClass, replay_passed: bool
) -> None:
    record = _evidence(
        f"ineligible-{artifact_class.value}-{replay_passed}",
        kind=EvidenceKind.DEFEQ,
        value=DefeqValue(outcome="equal"),
    )
    admission = _admission((record,), artifact_class=artifact_class, replay_passed=replay_passed)
    assert admission.production_eligible is False
    result = _derive((record,), (admission,))
    assert result.F0_representation_equivalent is None
    assert result.accepted_evidence_ids == ()
    assert result.ignored_reason_codes[record.evidence_id] == ("admission_not_production_eligible",)


def test_defeq_and_two_audited_proofs_derive_f0_and_f2_but_never_f1() -> None:
    defeq = _evidence("defeq-equal", kind=EvidenceKind.DEFEQ, value=DefeqValue(outcome="equal"))
    audit_ab = _accepted_audit("proof-ab-audit")
    audit_ba = _accepted_audit("proof-ba-audit")
    proof_ab = _evidence(
        "proof-ab",
        kind=EvidenceKind.PROOF_A_IMPLIES_B,
        value=ProofValue(outcome="proved", tactic="exact"),
        metadata={"axiom_audit_evidence_id": audit_ab.evidence_id},
    )
    proof_ba = _evidence(
        "proof-ba",
        kind=EvidenceKind.PROOF_B_IMPLIES_A,
        value=ProofValue(outcome="proved", tactic="exact"),
        metadata={"axiom_audit_evidence_id": audit_ba.evidence_id},
    )
    records = (proof_ba, audit_ab, defeq, proof_ab, audit_ba)
    result = _derive(records)
    assert result.F0_representation_equivalent is True
    assert result.F1_same_claim is None
    assert result.truth_A_implies_B is True
    assert result.truth_B_implies_A is True
    assert result.F2_truth_equivalent is True
    assert set(result.F0_support_evidence_ids) == {defeq.evidence_id}
    assert set(result.truth_A_implies_B_support_evidence_ids) == {
        proof_ab.evidence_id,
        audit_ab.evidence_id,
    }
    assert set(result.truth_B_implies_A_support_evidence_ids) == {
        proof_ba.evidence_id,
        audit_ba.evidence_id,
    }


def test_proved_requires_a_successful_linked_all_true_audit() -> None:
    bad_audit = _accepted_audit(
        "proof-bad-audit",
        checks={"admission_free": True, "axioms_allowed": False},
        violations=("forbidden_axiom",),
    )
    proof = _evidence(
        "proof-with-bad-audit",
        kind=EvidenceKind.PROOF_A_IMPLIES_B,
        value=ProofValue(outcome="proved"),
        metadata={"axiom_audit_evidence_id": bad_audit.evidence_id},
    )
    result = _derive((proof, bad_audit))
    assert result.truth_A_implies_B is None
    assert result.F2_truth_equivalent is None
    assert "proved_axiom_audit_not_accepted" in result.ignored_reason_codes[proof.evidence_id]
    assert "axiom_audit_checks_not_accepted" in result.ignored_reason_codes[bad_audit.evidence_id]

    missing_link = _evidence(
        "proof-missing-link",
        kind=EvidenceKind.PROOF_A_IMPLIES_B,
        value=ProofValue(outcome="proved"),
    )
    missing_result = _derive((missing_link,))
    assert missing_result.truth_A_implies_B is None
    assert missing_result.ignored_reason_codes[missing_link.evidence_id] == (
        "proved_missing_axiom_audit_link",
    )


def test_found_counterexample_with_same_artifact_audit_sets_named_direction_false() -> None:
    artifact = "evidence/counterexample-a-to-b.json"
    audit = _accepted_audit("counterexample-audit", raw_artifact=artifact)
    counterexample = _evidence(
        "counterexample-a-to-b",
        kind=EvidenceKind.COUNTEREXAMPLE,
        value=CounterexampleValue(
            outcome="found",
            direction="A_to_B",
            domain="finset",
            encoding="kernel_decide_v1",
            witness_artifact=artifact,
        ),
        raw_artifact=artifact,
    )
    result = _derive((counterexample, audit))
    assert result.truth_A_implies_B is False
    assert result.truth_B_implies_A is None
    assert result.F2_truth_equivalent is False
    assert set(result.truth_A_implies_B_support_evidence_ids) == {
        counterexample.evidence_id,
        audit.evidence_id,
    }


def test_counterexample_without_matching_artifact_audit_is_ignored() -> None:
    audit = _accepted_audit("mismatching-counterexample-audit", raw_artifact="audit.json")
    counterexample = _evidence(
        "counterexample-without-match",
        kind=EvidenceKind.COUNTEREXAMPLE,
        value=CounterexampleValue(
            outcome="found",
            direction="B_to_A",
            domain="finset",
            encoding="kernel_decide_v1",
            witness_artifact="witness.json",
        ),
        raw_artifact="counterexample.json",
    )
    result = _derive((counterexample, audit))
    assert result.truth_B_implies_A is None
    assert (
        "found_counterexample_missing_matching_axiom_audit"
        in result.ignored_reason_codes[counterexample.evidence_id]
    )


def test_forbidden_shortcuts_never_set_f1_or_truth() -> None:
    standalone_audit = _accepted_audit("standalone-audit", raw_artifact="equivalence-only.json")
    records = (
        _evidence("typecheck", kind=EvidenceKind.TYPECHECK, value=TypecheckValue(outcome="valid")),
        _evidence(
            "llm-judgment",
            kind=EvidenceKind.LLM_JUDGMENT,
            value=JudgmentValue(answer="same_claim", relation="equivalent", confidence=1.0),
        ),
        _evidence(
            "human-judgment",
            kind=EvidenceKind.HUMAN_ANNOTATION,
            value=JudgmentValue(answer="same_claim", relation="equivalent", confidence=1.0),
        ),
        _evidence(
            "alignment",
            kind=EvidenceKind.CLAIM_ALIGNMENT,
            value=ClaimAlignmentValue(
                alignment_version="v1",
                binder_map={},
                premise_map={},
                conclusion_role_map={},
                direction="both",
                outcome="certified",
            ),
        ),
        _evidence(
            "transformation-audit",
            kind=EvidenceKind.TRANSFORMATION_AUDIT,
            value=AuditValue(checks={"rule_applied": True}),
        ),
        standalone_audit,
        _evidence(
            "defeq-not-equal", kind=EvidenceKind.DEFEQ, value=DefeqValue(outcome="not_equal")
        ),
        _evidence(
            "proof-not-proved",
            kind=EvidenceKind.PROOF_A_IMPLIES_B,
            value=ProofValue(outcome="not_proved"),
        ),
        _evidence(
            "counterexample-not-found",
            kind=EvidenceKind.COUNTEREXAMPLE,
            value=CounterexampleValue(outcome="not_found", direction="equivalence_only"),
        ),
        _evidence(
            "counterexample-unsupported",
            kind=EvidenceKind.COUNTEREXAMPLE,
            status=EvidenceExecutionStatus.UNSUPPORTED,
            value=CounterexampleValue(outcome="unsupported", direction="equivalence_only"),
        ),
        _evidence(
            "counterexample-equivalence-only",
            kind=EvidenceKind.COUNTEREXAMPLE,
            value=CounterexampleValue(
                outcome="found",
                direction="equivalence_only",
                domain="finset",
                encoding="kernel_decide_v1",
                witness_artifact="equivalence-only.json",
            ),
            raw_artifact="equivalence-only.json",
        ),
    )
    result = _derive(records)
    assert result.F0_representation_equivalent is None
    assert result.F1_same_claim is None
    assert result.truth_A_implies_B is None
    assert result.truth_B_implies_A is None
    assert result.F2_truth_equivalent is None
    assert result.accepted_evidence_ids == ()
    assert set(result.ignored_evidence_ids) == {record.evidence_id for record in records}


def test_true_and_false_for_the_same_direction_is_a_hard_conflict() -> None:
    artifact = "evidence/shared-certificate.json"
    audit = _accepted_audit("conflict-audit", raw_artifact=artifact)
    proof = _evidence(
        "conflict-proof",
        kind=EvidenceKind.PROOF_A_IMPLIES_B,
        value=ProofValue(outcome="proved"),
        metadata={"axiom_audit_evidence_id": audit.evidence_id},
    )
    counterexample = _evidence(
        "conflict-counterexample",
        kind=EvidenceKind.COUNTEREXAMPLE,
        value=CounterexampleValue(
            outcome="found",
            direction="A_to_B",
            domain="finset",
            encoding="kernel_decide_v1",
            witness_artifact=artifact,
        ),
        raw_artifact=artifact,
    )
    with pytest.raises(EvidenceFactConflictError, match="both true and false for A_to_B") as caught:
        _derive((proof, counterexample, audit))
    assert caught.value.direction == "A_to_B"
    assert caught.value.true_support_ids == tuple(sorted((proof.evidence_id, audit.evidence_id)))
    assert caught.value.false_support_ids == tuple(
        sorted((counterexample.evidence_id, audit.evidence_id))
    )


def test_closed_admission_graph_rejects_duplicates_missing_links_and_wrong_targets() -> None:
    first = _evidence("graph-first", kind=EvidenceKind.DEFEQ, value=DefeqValue(outcome="equal"))
    second = _evidence("graph-second", kind=EvidenceKind.DEFEQ, value=DefeqValue(outcome="equal"))
    first_admission = _admission((first,), suffix="first")

    with pytest.raises(EvidenceAggregationError, match="duplicate evidence ID"):
        _derive((first, first), (first_admission,))
    with pytest.raises(EvidenceAggregationError, match="lack admissions"):
        _derive((first, second), (first_admission,))

    missing_record_admission = _admission((second,), suffix="missing-record")
    with pytest.raises(EvidenceAggregationError, match="references missing evidence"):
        _derive((first,), (first_admission, missing_record_admission))

    overlapping = _admission((first,), suffix="overlapping")
    with pytest.raises(EvidenceAggregationError, match="multiple admissions"):
        _derive((first,), (first_admission, overlapping))
    with pytest.raises(EvidenceAggregationError, match="duplicate admission ID"):
        _derive((first,), (first_admission, first_admission))

    wrong_target_admission = _admission((first,), target_id=OTHER_PAIR_ID, suffix="wrong-target")
    with pytest.raises(EvidenceAggregationError, match="targets a different record"):
        _derive((first,), (wrong_target_admission,))

    wrong_target_evidence = _evidence(
        "wrong-target-evidence",
        kind=EvidenceKind.DEFEQ,
        value=DefeqValue(outcome="equal"),
        target_id=OTHER_PAIR_ID,
    )
    with pytest.raises(EvidenceAggregationError, match="targets a different record"):
        derive_admitted_evidence(
            target_kind=EvidenceTargetKind.LEAN_PAIR,
            target_id=PAIR_ID,
            evidence_records=(wrong_target_evidence,),
            admissions=(_admission((wrong_target_evidence,), target_id=OTHER_PAIR_ID),),
        )


def test_missing_proof_audit_record_is_an_unlinked_graph_error() -> None:
    absent_audit_id = make_id("ev", {"fixture": "absent-proof-audit"})
    proof = _evidence(
        "proof-links-absent-audit",
        kind=EvidenceKind.PROOF_A_IMPLIES_B,
        value=ProofValue(outcome="proved"),
        metadata={"axiom_audit_evidence_id": absent_audit_id},
    )
    with pytest.raises(EvidenceAggregationError, match="links missing audit"):
        _derive((proof,))


def test_derivation_is_order_independent_and_replay_verifier_rejects_tampering() -> None:
    defeq = _evidence("replay-defeq", kind=EvidenceKind.DEFEQ, value=DefeqValue(outcome="equal"))
    not_equal = _evidence(
        "replay-not-equal", kind=EvidenceKind.DEFEQ, value=DefeqValue(outcome="not_equal")
    )
    first_admission = _admission((defeq,), suffix="replay-first")
    second_admission = _admission((not_equal,), suffix="replay-second")
    forward = _derive((defeq, not_equal), (first_admission, second_admission))
    backward = _derive((not_equal, defeq), (second_admission, first_admission))
    assert forward == backward
    verify_evidence_derivation(
        derivation=forward,
        evidence_records=(not_equal, defeq),
        admissions=(second_admission, first_admission),
    )

    tampered = forward.model_copy(update={"F0_representation_equivalent": None})
    with pytest.raises(EvidenceAggregationError, match="differs from deterministic replay"):
        verify_evidence_derivation(
            derivation=tampered,
            evidence_records=(defeq, not_equal),
            admissions=(first_admission, second_admission),
        )
