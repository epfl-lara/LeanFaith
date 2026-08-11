"""Fail-closed LF-024 resolver tests."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from pathlib import Path

import pytest

from leanfaith.labeling.aggregation import (
    EvidenceAdmissionRecord,
    build_evidence_admission_record,
)
from leanfaith.labeling.conflicts import (
    ResolutionConflictReason,
    ResolutionOverrideReason,
)
from leanfaith.labeling.quality import (
    ActiveLabelResolutionPolicy,
    AuthorityArtifactBinding,
    AuthorityArtifactKind,
    CandidateCommitment,
    ResolutionCandidate,
    ResolutionSource,
    load_active_label_resolution_policy,
    make_authority_artifact_binding,
    make_resolution_candidate,
)
from leanfaith.labeling.resolution import (
    ResolutionArtifacts,
    ResolutionInputError,
    resolve_target,
    verify_resolution_artifacts,
)
from leanfaith.schemas.enums import (
    ArtifactClass,
    Decision,
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
    NLTrust,
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.evidence import (
    AuditValue,
    CounterexampleValue,
    DefeqValue,
    EvidenceRecord,
    JudgmentValue,
    ProofValue,
)
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.nl_lean import NLPLeanRecord
from leanfaith.schemas.pair import PairRecord

NOW = datetime.datetime(2026, 8, 11, 13, 0, tzinfo=datetime.UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
PAIR_ID = make_id("pair", {"fixture": "lf024-resolution"})
OTHER_PAIR_ID = make_id("pair", {"fixture": "lf024-resolution-other"})
NL_LEAN_ID = make_id("nllean", {"fixture": "lf024-resolution-nl"})
THEOREM_A_ID = make_id("thm", {"fixture": "lf024-resolution-a"})
THEOREM_B_ID = make_id("thm", {"fixture": "lf024-resolution-b"})


@pytest.fixture(scope="module")
def policy() -> ActiveLabelResolutionPolicy:
    return load_active_label_resolution_policy(REPO_ROOT)


def _pair(
    records: Sequence[EvidenceRecord] = (),
    *,
    pair_id: str = PAIR_ID,
) -> PairRecord:
    return PairRecord(
        pair_id=pair_id,
        theorem_a_id=THEOREM_A_ID,
        theorem_b_id=THEOREM_B_ID,
        pair_source="lf024_resolution_fixture",
        split_group_ids=("ancestry:lf024-resolution",),
        evidence_ids=tuple(sorted(record.evidence_id for record in records)),
    )


def _nl_target() -> NLPLeanRecord:
    return NLPLeanRecord(
        nl_lean_id=NL_LEAN_ID,
        problem_id="lf024-resolution-problem",
        problem_group="problem:lf024-resolution",
        source="fixture",
        source_revision="v1",
        nl_statement="Show that the candidate states the intended claim.",
        nl_trust=NLTrust.TRUSTED,
        candidate_theorem_id=THEOREM_B_ID,
        split_group_ids=("problem:lf024-resolution",),
    )


def _evidence(
    name: str,
    *,
    kind: EvidenceKind,
    value: object,
    target_id: str = PAIR_ID,
    target_kind: EvidenceTargetKind = EvidenceTargetKind.LEAN_PAIR,
    raw_artifact: str | None = None,
    metadata: dict[str, str | int | float | bool | None] | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=make_id(
            "ev",
            {
                "fixture": "lf024-resolution",
                "name": name,
                "target_id": target_id,
            },
        ),
        target_kind=target_kind,
        target_id=target_id,
        kind=kind,
        status=EvidenceExecutionStatus.SUCCESS,
        value=value,  # type: ignore[arg-type]
        method_version="lf024_resolution_fixture_v1",
        config_hash="d" * 64,
        raw_artifact=raw_artifact,
        created_at=NOW,
        metadata=metadata or {},
    )


def _admission(
    records: Sequence[EvidenceRecord],
    policy: ActiveLabelResolutionPolicy,
    *,
    target_id: str = PAIR_ID,
    target_kind: EvidenceTargetKind = EvidenceTargetKind.LEAN_PAIR,
    suffix: str = "default",
) -> EvidenceAdmissionRecord:
    return build_evidence_admission_record(
        target_kind=target_kind,
        target_id=target_id,
        evidence_ids=tuple(record.evidence_id for record in records),
        artifact_class=ArtifactClass.PRODUCTION,
        manifest_artifact_id=f"manifest:{suffix}",
        manifest_artifact_sha256="a" * 64,
        replay_artifact_id=f"replay:{suffix}",
        replay_artifact_sha256="b" * 64,
        replay_passed=True,
        policy_sha256=policy.policy_file_sha256,
    )


def _artifact(
    kind: AuthorityArtifactKind,
    suffix: str,
) -> AuthorityArtifactBinding:
    return make_authority_artifact_binding(
        artifact_kind=kind,
        artifact_id=f"authority:{suffix}",
        artifact_sha256=make_id("blob", {"suffix": suffix}).split(":", 1)[1],
    )


def _human_candidate(
    policy: ActiveLabelResolutionPolicy,
    evidence_ids: tuple[str, ...],
    *,
    same_claim: bool | None = True,
    relation: RelationLabel | None = RelationLabel.EQUIVALENT,
    outcome: ResolutionOutcome = ResolutionOutcome.SAME_CLAIM,
    f0: bool | None = None,
    truth_a_implies_b: bool | None = None,
    truth_b_implies_a: bool | None = None,
    f2: bool | None = None,
    suffix: str = "human",
) -> ResolutionCandidate:
    return make_resolution_candidate(
        policy=policy,
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        source=ResolutionSource.HUMAN_ADJUDICATION,
        quality_tier=QualityTier.GOLD_HUMAN,
        resolution_method="expert_adjudication",
        authority_artifacts=(_artifact(AuthorityArtifactKind.HUMAN_ADJUDICATION, suffix),),
        accepted_evidence_ids=evidence_ids,
        commitment=CandidateCommitment.TERMINAL,
        same_claim=same_claim,
        resolution_outcome=outcome,
        relation=relation,
        F0_representation_equivalent=f0,
        truth_A_implies_B=truth_a_implies_b,
        truth_B_implies_A=truth_b_implies_a,
        F2_truth_equivalent=f2,
        provenance=(f"fixture:{suffix}",),
    )


def _benchmark_candidate(
    policy: ActiveLabelResolutionPolicy,
    *,
    target_kind: SemanticLabelTargetKind = SemanticLabelTargetKind.LEAN_PAIR,
    target_id: str = PAIR_ID,
    suffix: str = "benchmark",
) -> ResolutionCandidate:
    return make_resolution_candidate(
        policy=policy,
        target_kind=target_kind,
        target_id=target_id,
        source=ResolutionSource.FROZEN_BENCHMARK_POLICY,
        quality_tier=QualityTier.BENCHMARK,
        resolution_method="benchmark_import",
        authority_artifacts=(_artifact(AuthorityArtifactKind.FROZEN_BENCHMARK_LABEL, suffix),),
        accepted_evidence_ids=(),
        commitment=CandidateCommitment.TERMINAL,
        same_claim=True,
        resolution_outcome=ResolutionOutcome.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
        provenance=(f"fixture:{suffix}",),
    )


def _consensus_candidate(
    policy: ActiveLabelResolutionPolicy,
    evidence_ids: tuple[str, ...],
    *,
    same_claim: bool,
    relation: RelationLabel,
    f0: bool | None = None,
    suffix: str,
) -> ResolutionCandidate:
    return make_resolution_candidate(
        policy=policy,
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        source=ResolutionSource.PROMOTED_INDEPENDENT_CONSENSUS,
        quality_tier=QualityTier.SILVER_CONSENSUS,
        resolution_method="independent_consensus_audited",
        authority_artifacts=(
            _artifact(
                AuthorityArtifactKind.INDEPENDENT_CONSENSUS_PROMOTION,
                suffix,
            ),
        ),
        accepted_evidence_ids=evidence_ids,
        commitment=CandidateCommitment.TERMINAL,
        same_claim=same_claim,
        resolution_outcome=(
            ResolutionOutcome.SAME_CLAIM if same_claim else ResolutionOutcome.NOT_SAME_CLAIM
        ),
        relation=relation,
        F0_representation_equivalent=f0,
        provenance=(f"fixture:{suffix}",),
    )


def _judgment(
    name: str,
    *,
    kind: EvidenceKind = EvidenceKind.HUMAN_ANNOTATION,
) -> EvidenceRecord:
    return _evidence(
        name,
        kind=kind,
        value=JudgmentValue(
            answer="same_claim",
            relation="equivalent",
            confidence=1.0,
        ),
    )


def _audited_f2_records() -> tuple[EvidenceRecord, ...]:
    defeq = _evidence(
        "f0-defeq",
        kind=EvidenceKind.DEFEQ,
        value=DefeqValue(outcome="equal"),
    )
    audit_ab = _evidence(
        "f2-audit-ab",
        kind=EvidenceKind.AXIOM_AUDIT,
        value=AuditValue(checks={"admission_free": True}),
    )
    audit_ba = _evidence(
        "f2-audit-ba",
        kind=EvidenceKind.AXIOM_AUDIT,
        value=AuditValue(checks={"admission_free": True}),
    )
    proof_ab = _evidence(
        "f2-proof-ab",
        kind=EvidenceKind.PROOF_A_IMPLIES_B,
        value=ProofValue(outcome="proved"),
        metadata={"axiom_audit_evidence_id": audit_ab.evidence_id},
    )
    proof_ba = _evidence(
        "f2-proof-ba",
        kind=EvidenceKind.PROOF_B_IMPLIES_A,
        value=ProofValue(outcome="proved"),
        metadata={"axiom_audit_evidence_id": audit_ba.evidence_id},
    )
    return defeq, audit_ab, proof_ab, audit_ba, proof_ba


def _counterexample_records(prefix: str) -> tuple[EvidenceRecord, EvidenceRecord]:
    artifact = f"evidence/{prefix}.json"
    audit = _evidence(
        f"{prefix}-audit",
        kind=EvidenceKind.AXIOM_AUDIT,
        value=AuditValue(checks={"kernel_checked": True}),
        raw_artifact=artifact,
    )
    counterexample = _evidence(
        f"{prefix}-counterexample",
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
    return audit, counterexample


def test_no_candidate_resolves_to_exact_unresolved_review_contract(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    result = resolve_target(
        target=_pair(),
        evidence_records=(),
        admissions=(),
        candidates=(),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.same_claim is None
    assert result.label.resolution_outcome is ResolutionOutcome.UNRESOLVED
    assert result.label.relation is None
    assert result.label.quality_tier is QualityTier.UNKNOWN
    assert result.label.requires_adjudication is True
    assert result.label.train_eligibility is False
    assert result.label.eval_eligibility is False
    assert result.label.decision is Decision.REVIEW
    assert result.audit.status == "unresolved"
    assert result.audit.reason_codes == ("no_admissible_semantic_candidate",)
    assert result.target.resolved_label_id == result.label.label_id


def test_f0_f2_evidence_remains_f1_unresolved(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    records = _audited_f2_records()
    result = resolve_target(
        target=_pair(records),
        evidence_records=records,
        admissions=(_admission(records, policy, suffix="f0-f2"),),
        candidates=(),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.same_claim is None
    assert result.label.faithfulness_levels.F0_representation_equivalent is True
    assert result.label.faithfulness_levels.F1_same_claim is None
    assert result.label.truth_A_implies_B is True
    assert result.label.truth_B_implies_A is True
    assert result.label.faithfulness_levels.F2_truth_equivalent is True
    assert result.label.decision is Decision.REVIEW


def test_terminal_authority_candidate_resolves_and_revalidates_linked_target(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    human = _judgment("terminal-human")
    candidate = _human_candidate(policy, (human.evidence_id,))
    result = resolve_target(
        target=_pair((human,)),
        evidence_records=(human,),
        admissions=(_admission((human,), policy, suffix="terminal"),),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.same_claim is True
    assert result.label.relation is RelationLabel.EQUIVALENT
    assert result.label.quality_tier is QualityTier.GOLD_HUMAN
    assert result.label.train_eligibility is False
    assert result.label.eval_eligibility is False
    assert result.label.decision is None
    assert result.audit.selected_candidate_id == candidate.candidate_id
    assert isinstance(result.target, PairRecord)
    PairRecord.model_validate(result.target.model_dump(mode="python"))


def test_trusted_human_f0_is_used_only_when_mechanical_f0_is_absent(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    human = _judgment("trusted-f0-human")
    candidate = _human_candidate(
        policy,
        (human.evidence_id,),
        f0=False,
        suffix="trusted-f0",
    )
    result = resolve_target(
        target=_pair((human,)),
        evidence_records=(human,),
        admissions=(_admission((human,), policy, suffix="trusted-f0"),),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.faithfulness_levels.F0_representation_equivalent is False
    assert result.label.same_claim is True


def test_trusted_f0_disagreement_with_mechanical_certificate_routes_to_review(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    human = _judgment("trusted-f0-conflict-human")
    defeq = _evidence(
        "trusted-f0-conflict-defeq",
        kind=EvidenceKind.DEFEQ,
        value=DefeqValue(outcome="equal"),
    )
    records = (human, defeq)
    candidate = _human_candidate(
        policy,
        (human.evidence_id,),
        f0=False,
        suffix="trusted-f0-conflict",
    )
    result = resolve_target(
        target=_pair(records),
        evidence_records=records,
        admissions=(_admission(records, policy, suffix="trusted-f0-conflict"),),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.same_claim is None
    assert result.label.faithfulness_levels.F0_representation_equivalent is True
    assert len(result.conflicts) == 1
    assert result.conflicts[0].evidence_ids == (defeq.evidence_id,)
    assert (
        ResolutionConflictReason.MUTUALLY_INCONSISTENT_CERTIFICATES
        in result.conflicts[0].reason_codes
    )


def test_nontrusted_consensus_f0_is_rejected_at_candidate_boundary(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    judgments = tuple(
        _judgment(f"nontrusted-f0-{index}", kind=EvidenceKind.LLM_JUDGMENT) for index in range(4)
    )
    with pytest.raises(ValueError, match="cannot self-assert F0"):
        _consensus_candidate(
            policy,
            tuple(sorted(record.evidence_id for record in judgments)),
            same_claim=False,
            relation=RelationLabel.INCOMPARABLE,
            f0=False,
            suffix="nontrusted-f0",
        )


def test_partial_separator_never_creates_terminal_f1(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    audit, counterexample = _counterexample_records("partial")
    records = (audit, counterexample)
    candidate = make_resolution_candidate(
        policy=policy,
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        source=ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        quality_tier=QualityTier.GOLD_COUNTEREXAMPLE,
        resolution_method="separator_certificate",
        authority_artifacts=(_artifact(AuthorityArtifactKind.CERTIFICATE_OR_SEPARATOR, "partial"),),
        accepted_evidence_ids=(counterexample.evidence_id,),
        commitment=CandidateCommitment.PARTIAL_NEGATIVE,
        same_claim=False,
        resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        relation=None,
        provenance=("fixture:partial",),
    )
    result = resolve_target(
        target=_pair(records),
        evidence_records=records,
        admissions=(_admission(records, policy, suffix="partial"),),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.same_claim is None
    assert result.label.resolution_outcome is ResolutionOutcome.UNRESOLVED
    assert result.label.faithfulness_levels.F2_truth_equivalent is False
    assert result.audit.reason_codes == ("best_authority_is_partial",)


def test_same_rank_strong_disagreement_is_a_conflict_despite_shared_evidence(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    human = _judgment("strong-conflict-shared")
    positive = _human_candidate(
        policy,
        (human.evidence_id,),
        suffix="strong-positive",
    )
    negative = _human_candidate(
        policy,
        (human.evidence_id,),
        same_claim=False,
        relation=RelationLabel.UNRELATED,
        outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        suffix="strong-negative",
    )
    result = resolve_target(
        target=_pair((human,)),
        evidence_records=(human,),
        admissions=(_admission((human,), policy, suffix="strong-conflict"),),
        candidates=(positive, negative),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.same_claim is None
    assert result.audit.reason_codes == ("strong_evidence_conflict",)
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.source_ranks == (1,)
    assert conflict.evidence_ids == (human.evidence_id,)
    assert ResolutionConflictReason.SAME_CLAIM_DISAGREEMENT in conflict.reason_codes


def test_higher_precedence_agreeing_candidate_logs_override(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    human = _judgment("override-human")
    winner = _human_candidate(policy, (human.evidence_id,), suffix="override-winner")
    benchmark = _benchmark_candidate(policy, suffix="override-benchmark")
    result = resolve_target(
        target=_pair((human,)),
        evidence_records=(human,),
        admissions=(_admission((human,), policy, suffix="override"),),
        candidates=(benchmark, winner),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.audit.selected_candidate_id == winner.candidate_id
    assert len(result.overrides) == 1
    assert result.overrides[0].winner_candidate_id == winner.candidate_id
    assert result.overrides[0].overridden_candidate_ids == (benchmark.candidate_id,)
    assert result.overrides[0].reason_codes == (
        ResolutionOverrideReason.STRONG_OVER_STRONG_AGREEING,
    )


def test_equal_rank_weak_disagreement_routes_to_review_without_false_conflict(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    records = tuple(
        _judgment(f"weak-{index}", kind=EvidenceKind.LLM_JUDGMENT) for index in range(4)
    )
    evidence_ids = tuple(sorted(record.evidence_id for record in records))
    positive = _consensus_candidate(
        policy,
        evidence_ids,
        same_claim=True,
        relation=RelationLabel.EQUIVALENT,
        suffix="weak-positive",
    )
    negative = _consensus_candidate(
        policy,
        evidence_ids,
        same_claim=False,
        relation=RelationLabel.INCOMPARABLE,
        suffix="weak-negative",
    )
    result = resolve_target(
        target=_pair(records),
        evidence_records=records,
        admissions=(_admission(records, policy, suffix="weak"),),
        candidates=(positive, negative),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.same_claim is None
    assert result.conflicts == ()
    assert result.audit.reason_codes == ("equal_rank_candidate_disagreement",)


def test_weak_candidate_contradicting_mechanical_evidence_is_excluded(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    audit, counterexample = _counterexample_records("weak-mechanical-conflict")
    judgments = tuple(
        _judgment(f"weak-mechanical-{index}", kind=EvidenceKind.LLM_JUDGMENT) for index in range(4)
    )
    records = (audit, counterexample, *judgments)
    candidate = _consensus_candidate(
        policy,
        tuple(sorted(record.evidence_id for record in judgments)),
        same_claim=True,
        relation=RelationLabel.EQUIVALENT,
        suffix="weak-mechanical-conflict",
    )

    result = resolve_target(
        target=_pair(records),
        evidence_records=records,
        admissions=(_admission(records, policy, suffix="weak-mechanical-conflict"),),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.same_claim is None
    assert result.label.faithfulness_levels.F2_truth_equivalent is False
    assert result.conflicts == ()
    assert result.audit.reason_codes == (
        "no_admissible_semantic_candidate",
        "weak_candidate_mechanical_conflict",
    )


def test_conflicting_mechanical_evidence_is_recorded_and_routes_to_review(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    artifact = "evidence/mechanical-conflict.json"
    audit = _evidence(
        "mechanical-conflict-audit",
        kind=EvidenceKind.AXIOM_AUDIT,
        value=AuditValue(checks={"kernel_checked": True}),
        raw_artifact=artifact,
    )
    proof = _evidence(
        "mechanical-conflict-proof",
        kind=EvidenceKind.PROOF_A_IMPLIES_B,
        value=ProofValue(outcome="proved"),
        metadata={"axiom_audit_evidence_id": audit.evidence_id},
    )
    counterexample = _evidence(
        "mechanical-conflict-counterexample",
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
    records = (audit, proof, counterexample)
    result = resolve_target(
        target=_pair(records),
        evidence_records=records,
        admissions=(_admission(records, policy, suffix="mechanical-conflict"),),
        candidates=(),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.derivation is None
    assert result.label.same_claim is None
    assert len(result.conflicts) == 1
    assert len(result.conflicts[0].evidence_ids) == 3
    assert set(result.label.evidence_ids_used) == {
        audit.evidence_id,
        proof.evidence_id,
        counterexample.evidence_id,
    }
    assert result.audit.reason_codes == ("strong_evidence_conflict",)


def test_positive_candidate_conflicting_with_refuted_f2_routes_to_review(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    audit, counterexample = _counterexample_records("candidate-f2-conflict")
    human = _judgment("candidate-f2-human")
    records = (audit, counterexample, human)
    candidate = _human_candidate(
        policy,
        (human.evidence_id,),
        suffix="candidate-f2-positive",
    )
    result = resolve_target(
        target=_pair(records),
        evidence_records=records,
        admissions=(_admission(records, policy, suffix="candidate-f2"),),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.same_claim is None
    assert result.label.faithfulness_levels.F2_truth_equivalent is False
    assert len(result.conflicts) == 1
    assert (
        ResolutionConflictReason.MUTUALLY_INCONSISTENT_CERTIFICATES
        in result.conflicts[0].reason_codes
    )


def test_auxiliary_truth_conflict_carries_mechanical_support_lineage(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    audit, counterexample = _counterexample_records("auxiliary-truth-conflict")
    human = _judgment("auxiliary-truth-human")
    records = (audit, counterexample, human)
    candidate = _human_candidate(
        policy,
        (human.evidence_id,),
        same_claim=False,
        relation=RelationLabel.INCOMPARABLE,
        outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        truth_a_implies_b=True,
        suffix="auxiliary-truth-conflict",
    )

    result = resolve_target(
        target=_pair(records),
        evidence_records=records,
        admissions=(_admission(records, policy, suffix="auxiliary-truth-conflict"),),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
    )

    assert result.label.same_claim is None
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert ResolutionConflictReason.TRUTH_A_IMPLIES_B_DISAGREEMENT in conflict.reason_codes
    assert set(conflict.evidence_ids) == {audit.evidence_id, counterexample.evidence_id}
    assert set(conflict.evidence_ids) <= set(result.label.evidence_ids_used)


def test_input_permutations_produce_identical_content_addressed_artifacts(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    human = _judgment("permutation-human")
    defeq = _evidence(
        "permutation-defeq",
        kind=EvidenceKind.DEFEQ,
        value=DefeqValue(outcome="equal"),
    )
    records = (human, defeq)
    winner = _human_candidate(policy, (human.evidence_id,), suffix="permutation-winner")
    benchmark = _benchmark_candidate(policy, suffix="permutation-benchmark")
    admission = _admission(records, policy, suffix="permutation")

    forward = resolve_target(
        target=_pair(records),
        evidence_records=records,
        admissions=(admission,),
        candidates=(winner, benchmark),
        policy=policy,
        resolved_at=NOW,
    )
    backward = resolve_target(
        target=_pair(records),
        evidence_records=tuple(reversed(records)),
        admissions=(admission,),
        candidates=(benchmark, winner),
        policy=policy,
        resolved_at=NOW,
    )

    assert backward == forward


def test_malformed_or_duplicate_input_graph_is_rejected(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    missing_evidence_id = make_id("ev", {"fixture": "missing"})
    missing_target = _pair().model_copy(update={"evidence_ids": (missing_evidence_id,)})
    with pytest.raises(ResolutionInputError, match="exact closed evidence set"):
        resolve_target(
            target=missing_target,
            evidence_records=(),
            admissions=(),
            candidates=(),
            policy=policy,
            resolved_at=NOW,
        )

    evidence = _judgment("stale-admission-policy")
    admission = build_evidence_admission_record(
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        evidence_ids=(evidence.evidence_id,),
        artifact_class=ArtifactClass.PRODUCTION,
        manifest_artifact_id="manifest:stale-policy",
        manifest_artifact_sha256="a" * 64,
        replay_artifact_id="replay:stale-policy",
        replay_artifact_sha256="b" * 64,
        replay_passed=True,
        policy_sha256="f" * 64,
    )
    with pytest.raises(ResolutionInputError, match="active label-resolution policy"):
        resolve_target(
            target=_pair((evidence,)),
            evidence_records=(evidence,),
            admissions=(admission,),
            candidates=(),
            policy=policy,
            resolved_at=NOW,
        )

    human = _judgment("duplicate-candidate")
    candidate = _human_candidate(policy, (human.evidence_id,), suffix="duplicate")
    with pytest.raises(ResolutionInputError, match="duplicate ResolutionCandidate"):
        resolve_target(
            target=_pair((human,)),
            evidence_records=(human,),
            admissions=(_admission((human,), policy, suffix="duplicate"),),
            candidates=(candidate, candidate),
            policy=policy,
            resolved_at=NOW,
        )


def test_duplicate_admission_coverage_rejects_independently_of_input_order(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    evidence = _judgment("duplicate-admission-coverage")
    production = _admission((evidence,), policy, suffix="duplicate-production")
    diagnostic = build_evidence_admission_record(
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        evidence_ids=(evidence.evidence_id,),
        artifact_class=ArtifactClass.DIAGNOSTIC,
        manifest_artifact_id="manifest:duplicate-diagnostic",
        manifest_artifact_sha256="a" * 64,
        replay_artifact_id="replay:duplicate-diagnostic",
        replay_artifact_sha256="b" * 64,
        replay_passed=True,
        policy_sha256=policy.policy_file_sha256,
    )
    candidate = _human_candidate(
        policy,
        (evidence.evidence_id,),
        suffix="duplicate-admission-coverage",
    )

    for admissions in ((production, diagnostic), (diagnostic, production)):
        with pytest.raises(ResolutionInputError) as error:
            resolve_target(
                target=_pair((evidence,)),
                evidence_records=(evidence,),
                admissions=admissions,
                candidates=(candidate,),
                policy=policy,
                resolved_at=NOW,
            )
        assert str(error.value) == (
            "evidence is linked to multiple admissions: " + evidence.evidence_id
        )


def test_prior_label_is_required_exactly_for_re_resolution(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    human = _judgment("prior-human")
    candidate = _human_candidate(policy, (human.evidence_id,), suffix="prior")
    admission = _admission((human,), policy, suffix="prior")
    original = _pair((human,))
    first = resolve_target(
        target=original,
        evidence_records=(human,),
        admissions=(admission,),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
    )

    with pytest.raises(ResolutionInputError, match="unlinked target"):
        resolve_target(
            target=original,
            evidence_records=(human,),
            admissions=(admission,),
            candidates=(candidate,),
            policy=policy,
            resolved_at=NOW,
            prior_label=first.label,
        )
    with pytest.raises(ResolutionInputError, match="requires the exact prior_label"):
        resolve_target(
            target=first.target,
            evidence_records=(human,),
            admissions=(admission,),
            candidates=(candidate,),
            policy=policy,
            resolved_at=NOW,
        )

    second = resolve_target(
        target=first.target,
        evidence_records=(human,),
        admissions=(admission,),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
        prior_label=first.label,
    )
    assert second.audit.prior_label_id == first.label.label_id
    assert second.label == first.label


def test_re_resolution_cannot_downgrade_or_change_prior_label_without_incident_artifact(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    human = _judgment("prior-downgrade-human")
    weak_votes = tuple(
        _judgment(f"prior-downgrade-weak-{index}", kind=EvidenceKind.LLM_JUDGMENT)
        for index in range(4)
    )
    records = (human, *weak_votes)
    admission = _admission(records, policy, suffix="prior-downgrade")
    original = _pair(records)
    first = resolve_target(
        target=original,
        evidence_records=records,
        admissions=(admission,),
        candidates=(
            _human_candidate(policy, (human.evidence_id,), suffix="prior-downgrade-human"),
        ),
        policy=policy,
        resolved_at=NOW,
    )
    weaker = _consensus_candidate(
        policy,
        tuple(sorted(record.evidence_id for record in weak_votes)),
        same_claim=False,
        relation=RelationLabel.INCOMPARABLE,
        suffix="prior-downgrade-weak",
    )

    with pytest.raises(ResolutionInputError, match="typed supersession/incident"):
        resolve_target(
            target=first.target,
            evidence_records=records,
            admissions=(admission,),
            candidates=(weaker,),
            policy=policy,
            resolved_at=NOW,
            prior_label=first.label,
        )


def test_round_trip_and_exact_replay_verification(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    human = _judgment("replay-human")
    candidate = _human_candidate(policy, (human.evidence_id,), suffix="replay")
    admission = _admission((human,), policy, suffix="replay")
    original = _pair((human,))
    result = resolve_target(
        target=original,
        evidence_records=(human,),
        admissions=(admission,),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
    )
    restored = ResolutionArtifacts.model_validate(result.model_dump(mode="json"))
    assert restored == result
    verify_resolution_artifacts(
        artifacts=restored,
        original_target=original,
        evidence_records=(human,),
        admissions=(admission,),
        candidates=(candidate,),
        policy=policy,
    )

    tampered = result.model_copy(
        update={
            "label": result.label.model_copy(update={"adjudication_notes": "unbound tampering"})
        }
    )
    with pytest.raises(ResolutionInputError, match="differ from deterministic replay"):
        verify_resolution_artifacts(
            artifacts=tampered,
            original_target=original,
            evidence_records=(human,),
            admissions=(admission,),
            candidates=(candidate,),
            policy=policy,
        )


def test_nl_target_uses_same_resolver_and_validated_reverse_link(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    target = _nl_target()
    candidate = _benchmark_candidate(
        policy,
        target_kind=SemanticLabelTargetKind.NL_LEAN,
        target_id=NL_LEAN_ID,
        suffix="nl-benchmark",
    )
    result = resolve_target(
        target=target,
        evidence_records=(),
        admissions=(),
        candidates=(candidate,),
        policy=policy,
        resolved_at=NOW,
    )

    assert isinstance(result.target, NLPLeanRecord)
    assert result.label.target_kind is SemanticLabelTargetKind.NL_LEAN
    assert result.label.same_claim is True
    assert result.label.train_eligibility is False
    assert result.label.eval_eligibility is False
    assert result.target.resolved_label_id == result.label.label_id
    NLPLeanRecord.model_validate(result.target.model_dump(mode="python"))
