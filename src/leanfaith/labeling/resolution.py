"""Deterministic LF-024 label resolution.

The resolver combines two deliberately separate inputs:

* mechanically admitted Lean evidence, which may populate only F0/F2; and
* authority-bound semantic candidates, which may populate F1.

No generation intention, typecheck result, failed proof search, missing
counterexample, raw annotator vote, or raw LLM vote is accepted as a semantic
candidate here.  The public boundary accepts candidates only through an opaque
``VerifiedCandidateSet`` capability.  No production factory for a non-empty
capability exists yet.  When verified authority is absent or conflicting, the
resolver writes the exact unresolved/REVIEW contract instead of guessing.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any, Literal, Self, final

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.labeling.aggregation import (
    EvidenceAdmissionRecord,
    EvidenceAggregationError,
    EvidenceDerivationRecord,
    EvidenceFactConflictError,
    derive_admitted_evidence,
)
from leanfaith.labeling.conflicts import (
    ResolutionConflictReason,
    ResolutionConflictRecord,
    ResolutionOverrideReason,
    ResolutionOverrideRecord,
    build_resolution_conflict_record,
    build_resolution_override_record,
)
from leanfaith.labeling.quality import (
    ActiveLabelResolutionPolicy,
    CandidateCommitment,
    ResolutionCandidate,
    ResolutionSource,
)
from leanfaith.schemas.enums import (
    Decision,
    EvidenceExecutionStatus,
    EvidenceTargetKind,
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.evidence import EvidenceRecord
from leanfaith.schemas.ids import LABEL_PREFIX, id_pattern, make_id
from leanfaith.schemas.label import FaithfulnessLevels, ResolvedLabel, check_label_target_link
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.nl_lean import NLPLeanRecord
from leanfaith.schemas.pair import PairRecord

RESOLUTION_AUDIT_PREFIX = "resolution_audit"

ResolutionTarget = PairRecord | NLPLeanRecord


class ResolutionInputError(ValueError):
    """The resolver input graph is incomplete, stale, or cross-target."""


_VERIFIED_CANDIDATE_SET_SEAL = object()


@final
class VerifiedCandidateSet:
    """Opaque, process-local proof that candidate replay passed.

    This object is deliberately neither a Pydantic model nor serializable.  Its
    constructor is disabled and this module currently mints only the singleton
    empty capability.  A future typed authority-replay adapter must establish
    the complete source inventory and mint a target/policy-bound non-empty
    capability inside this trust boundary.  In particular,
    ``StructuralCandidateSetVerification`` is not such an authority proof.
    """

    __slots__ = ("__candidates", "__seal")

    def __new__(cls) -> Self:
        raise TypeError("VerifiedCandidateSet is an opaque capability with no public constructor")

    def __repr__(self) -> str:
        return "VerifiedCandidateSet(<opaque>)"

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise TypeError("VerifiedCandidateSet is immutable")

    def __delattr__(self, name: str) -> None:
        del name
        raise TypeError("VerifiedCandidateSet is immutable")

    def __copy__(self) -> Self:
        raise TypeError("VerifiedCandidateSet cannot be copied")

    def __deepcopy__(self, memo: object) -> Self:
        del memo
        raise TypeError("VerifiedCandidateSet cannot be copied")

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("VerifiedCandidateSet cannot be serialized")


def _make_empty_verified_candidate_set() -> VerifiedCandidateSet:
    """Mint the sole currently enabled capability: the empty candidate set."""

    capability = object.__new__(VerifiedCandidateSet)
    object.__setattr__(capability, "_VerifiedCandidateSet__candidates", ())
    object.__setattr__(
        capability,
        "_VerifiedCandidateSet__seal",
        _VERIFIED_CANDIDATE_SET_SEAL,
    )
    return capability


EMPTY_VERIFIED_CANDIDATE_SET = _make_empty_verified_candidate_set()


def _candidates_from_verified_set(
    verified_candidates: VerifiedCandidateSet,
) -> tuple[ResolutionCandidate, ...]:
    """Open a capability only after checking its exact runtime seal."""

    if type(verified_candidates) is not VerifiedCandidateSet:
        raise ResolutionInputError("resolver requires an exact VerifiedCandidateSet capability")
    try:
        seal = verified_candidates._VerifiedCandidateSet__seal  # type: ignore[attr-defined]
        candidates = verified_candidates._VerifiedCandidateSet__candidates  # type: ignore[attr-defined]
    except AttributeError as exc:
        raise ResolutionInputError("VerifiedCandidateSet capability is unsealed") from exc
    if seal is not _VERIFIED_CANDIDATE_SET_SEAL:
        raise ResolutionInputError("VerifiedCandidateSet capability has an invalid seal")
    if not isinstance(candidates, tuple) or not all(
        isinstance(item, ResolutionCandidate) for item in candidates
    ):
        raise ResolutionInputError("VerifiedCandidateSet capability contains invalid records")
    return candidates


class ResolutionAuditRecord(StrictModel):
    """Content-addressed audit of one deterministic resolution decision."""

    schema_version: Literal[1] = 1
    audit_id: str = Field(pattern=id_pattern(RESOLUTION_AUDIT_PREFIX))
    target_kind: SemanticLabelTargetKind
    target_id: str
    target_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    linked_target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1)
    policy_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_ids: tuple[str, ...]
    input_evidence_ids: tuple[str, ...]
    admission_ids: tuple[str, ...]
    derivation_id: str | None = Field(
        default=None,
        pattern=id_pattern("evidence_derivation"),
    )
    selected_candidate_id: str | None = Field(
        default=None,
        pattern=id_pattern("resolution_candidate"),
    )
    output_label_id: str = Field(pattern=id_pattern(LABEL_PREFIX))
    prior_label_id: str | None = Field(default=None, pattern=id_pattern(LABEL_PREFIX))
    conflict_ids: tuple[str, ...]
    override_ids: tuple[str, ...]
    status: Literal["resolved", "unresolved"]
    reason_codes: tuple[str, ...]
    resolved_at: datetime.datetime

    _utc = field_validator("resolved_at")(require_utc)

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        for field_name in (
            "candidate_ids",
            "input_evidence_ids",
            "admission_ids",
            "conflict_ids",
            "override_ids",
            "reason_codes",
        ):
            value = getattr(self, field_name)
            if value != tuple(sorted(set(value))):
                raise ValueError(f"{field_name} must be sorted and unique")
        if self.status == "resolved" and self.selected_candidate_id is None:
            raise ValueError("resolved audit requires selected_candidate_id")
        if self.status == "unresolved" and self.selected_candidate_id is not None:
            raise ValueError("unresolved audit cannot select a candidate")
        expected = make_id(
            RESOLUTION_AUDIT_PREFIX,
            self.model_dump(mode="json", exclude={"audit_id", "resolved_at"}),
        )
        if self.audit_id != expected:
            raise ValueError("audit_id does not match resolution content")
        return self


class ResolutionArtifacts(StrictModel):
    """All append-only products of one resolver invocation."""

    schema_version: Literal[1] = 1
    target: PairRecord | NLPLeanRecord
    label: ResolvedLabel
    audit: ResolutionAuditRecord
    derivation: EvidenceDerivationRecord | None
    conflicts: tuple[ResolutionConflictRecord, ...]
    overrides: tuple[ResolutionOverrideRecord, ...]

    @model_validator(mode="after")
    def _links(self) -> Self:
        violations = check_label_target_link(self.label, self.target)
        if violations:
            raise ValueError("invalid resolved-label reverse link: " + "; ".join(violations))
        if self.audit.output_label_id != self.label.label_id:
            raise ValueError("resolution audit does not name its label")
        if tuple(item.conflict_id for item in self.conflicts) != self.audit.conflict_ids:
            raise ValueError("resolution audit conflict IDs differ from records")
        if tuple(item.override_id for item in self.overrides) != self.audit.override_ids:
            raise ValueError("resolution audit override IDs differ from records")
        derivation_id = None if self.derivation is None else self.derivation.derivation_id
        if self.audit.derivation_id != derivation_id:
            raise ValueError("resolution audit derivation ID differs from record")
        return self


def _target_identity(
    target: ResolutionTarget,
) -> tuple[SemanticLabelTargetKind, EvidenceTargetKind, str, tuple[str, ...]]:
    if isinstance(target, PairRecord):
        return (
            SemanticLabelTargetKind.LEAN_PAIR,
            EvidenceTargetKind.LEAN_PAIR,
            target.pair_id,
            target.evidence_ids,
        )
    return (
        SemanticLabelTargetKind.NL_LEAN,
        EvidenceTargetKind.NL_LEAN,
        target.nl_lean_id,
        target.evidence_ids,
    )


def _validate_prior_label(
    *,
    target: ResolutionTarget,
    target_kind: SemanticLabelTargetKind,
    target_id: str,
    prior_label: ResolvedLabel | None,
) -> str | None:
    current = target.resolved_label_id
    if current is None and prior_label is not None:
        raise ResolutionInputError("prior_label supplied for an unlinked target")
    if current is not None and prior_label is None:
        raise ResolutionInputError("linked target requires the exact prior_label for re-resolution")
    if prior_label is None:
        return None
    if (
        prior_label.label_id != current
        or prior_label.target_kind is not target_kind
        or prior_label.target_id != target_id
    ):
        raise ResolutionInputError("prior_label does not match the target's current label link")
    return prior_label.label_id


def _validate_input_graph(
    *,
    target_kind: SemanticLabelTargetKind,
    evidence_kind: EvidenceTargetKind,
    target_id: str,
    linked_evidence_ids: tuple[str, ...],
    evidence_records: Sequence[EvidenceRecord],
    admissions: Sequence[EvidenceAdmissionRecord],
    candidates: Sequence[ResolutionCandidate],
    policy: ActiveLabelResolutionPolicy,
) -> None:
    evidence_ids = tuple(record.evidence_id for record in evidence_records)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ResolutionInputError("duplicate EvidenceRecord IDs")
    if len(linked_evidence_ids) != len(set(linked_evidence_ids)):
        raise ResolutionInputError("target contains duplicate evidence links")
    if set(evidence_ids) != set(linked_evidence_ids):
        raise ResolutionInputError(
            "resolver requires the exact closed evidence set linked by the target"
        )
    for record in evidence_records:
        if record.target_kind is not evidence_kind or record.target_id != target_id:
            raise ResolutionInputError("evidence record targets a different item")

    admission_ids = tuple(item.admission_id for item in admissions)
    if len(admission_ids) != len(set(admission_ids)):
        raise ResolutionInputError("duplicate EvidenceAdmissionRecord IDs")
    for admission in admissions:
        if admission.policy_sha256 != policy.policy_file_sha256:
            raise ResolutionInputError(
                "evidence admission is not bound to the active label-resolution policy"
            )

    candidate_ids = tuple(item.candidate_id for item in candidates)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ResolutionInputError("duplicate ResolutionCandidate IDs")
    admission_coverage: dict[str, list[EvidenceAdmissionRecord]] = {}
    for admission in admissions:
        for evidence_id in admission.evidence_ids:
            admission_coverage.setdefault(evidence_id, []).append(admission)
    multiply_admitted_evidence_ids = sorted(
        evidence_id
        for evidence_id, covering_admissions in admission_coverage.items()
        if len(covering_admissions) > 1
    )
    if multiply_admitted_evidence_ids:
        raise ResolutionInputError(
            "evidence is linked to multiple admissions: "
            + ", ".join(multiply_admitted_evidence_ids)
        )
    admission_by_evidence = {
        evidence_id: admission for admission in admissions for evidence_id in admission.evidence_ids
    }
    evidence_by_id = {record.evidence_id: record for record in evidence_records}
    for candidate in candidates:
        if (
            candidate.target_kind is not target_kind
            or candidate.target_id != target_id
            or candidate.policy_version != policy.policy_version
            or candidate.policy_file_sha256 != policy.policy_file_sha256
        ):
            raise ResolutionInputError("resolution candidate has stale/cross-target policy binding")
        for evidence_id in candidate.accepted_evidence_ids:
            cited_record = evidence_by_id.get(evidence_id)
            cited_admission = admission_by_evidence.get(evidence_id)
            if cited_record is None or cited_admission is None:
                raise ResolutionInputError("candidate cites missing evidence/admission")
            if (
                cited_record.status is not EvidenceExecutionStatus.SUCCESS
                or not cited_admission.production_eligible
            ):
                raise ResolutionInputError(
                    "candidate cites evidence without successful production admission"
                )


def _candidate_conflict_reasons(
    first: ResolutionCandidate,
    second: ResolutionCandidate,
) -> set[ResolutionConflictReason]:
    reasons: set[ResolutionConflictReason] = set()
    if first.same_claim != second.same_claim:
        reasons.add(ResolutionConflictReason.SAME_CLAIM_DISAGREEMENT)
    if first.resolution_outcome is not second.resolution_outcome:
        reasons.add(ResolutionConflictReason.RESOLUTION_OUTCOME_DISAGREEMENT)
    if (
        first.relation is not None
        and second.relation is not None
        and first.relation is not second.relation
    ):
        reasons.add(ResolutionConflictReason.RELATION_DISAGREEMENT)
    first_f0 = _trusted_candidate_f0(first)
    second_f0 = _trusted_candidate_f0(second)
    if first_f0 is not None and second_f0 is not None and first_f0 is not second_f0:
        reasons.add(ResolutionConflictReason.MUTUALLY_INCONSISTENT_CERTIFICATES)
    for left, right, reason in (
        (
            first.truth_A_implies_B,
            second.truth_A_implies_B,
            ResolutionConflictReason.TRUTH_A_IMPLIES_B_DISAGREEMENT,
        ),
        (
            first.truth_B_implies_A,
            second.truth_B_implies_A,
            ResolutionConflictReason.TRUTH_B_IMPLIES_A_DISAGREEMENT,
        ),
    ):
        if left is not None and right is not None and left is not right:
            reasons.add(reason)
    return reasons


def _candidate_evidence_conflict_reasons(
    candidate: ResolutionCandidate,
    derivation: EvidenceDerivationRecord,
) -> set[ResolutionConflictReason]:
    reasons: set[ResolutionConflictReason] = set()
    if (
        _trusted_candidate_f0(candidate) is False
        and derivation.F0_representation_equivalent is True
    ):
        reasons.add(ResolutionConflictReason.MUTUALLY_INCONSISTENT_CERTIFICATES)
    if candidate.same_claim is True and derivation.F2_truth_equivalent is False:
        reasons.add(ResolutionConflictReason.MUTUALLY_INCONSISTENT_CERTIFICATES)
    if candidate.relation is RelationLabel.A_STRONGER and derivation.truth_A_implies_B is False:
        reasons.add(ResolutionConflictReason.TRUTH_A_IMPLIES_B_DISAGREEMENT)
    if candidate.relation is RelationLabel.B_STRONGER and derivation.truth_B_implies_A is False:
        reasons.add(ResolutionConflictReason.TRUTH_B_IMPLIES_A_DISAGREEMENT)
    if (
        candidate.truth_A_implies_B is not None
        and derivation.truth_A_implies_B is not None
        and candidate.truth_A_implies_B is not derivation.truth_A_implies_B
    ):
        reasons.add(ResolutionConflictReason.TRUTH_A_IMPLIES_B_DISAGREEMENT)
    if (
        candidate.truth_B_implies_A is not None
        and derivation.truth_B_implies_A is not None
        and candidate.truth_B_implies_A is not derivation.truth_B_implies_A
    ):
        reasons.add(ResolutionConflictReason.TRUTH_B_IMPLIES_A_DISAGREEMENT)
    return reasons


def _trusted_candidate_f0(candidate: ResolutionCandidate) -> bool | None:
    """Return F0 only from policy-authorized human/benchmark adjudication.

    Mechanical certificates remain the primary source.  Consensus candidates
    and transformation intentions cannot set F0 through this semantic layer.
    """

    if candidate.source not in {
        ResolutionSource.HUMAN_ADJUDICATION,
        ResolutionSource.FROZEN_BENCHMARK_POLICY,
    }:
        return None
    return candidate.F0_representation_equivalent


def _make_label(
    *,
    target_kind: SemanticLabelTargetKind,
    target_id: str,
    policy: ActiveLabelResolutionPolicy,
    derivation: EvidenceDerivationRecord | None,
    selected: ResolutionCandidate | None,
    evidence_ids_used: tuple[str, ...],
    notes: str,
) -> ResolvedLabel:
    truth_a = None if derivation is None else derivation.truth_A_implies_B
    truth_b = None if derivation is None else derivation.truth_B_implies_A
    mechanical_f0 = None if derivation is None else derivation.F0_representation_equivalent
    candidate_f0 = None if selected is None else _trusted_candidate_f0(selected)
    f0 = mechanical_f0 if mechanical_f0 is not None else candidate_f0
    f2 = None if derivation is None else derivation.F2_truth_equivalent
    if selected is None:
        payload: dict[str, Any] = {
            "schema_version": 2,
            "target_kind": target_kind,
            "target_id": target_id,
            "same_claim": None,
            "resolution_outcome": ResolutionOutcome.UNRESOLVED,
            "relation": None,
            "faithfulness_levels": FaithfulnessLevels(
                F0_representation_equivalent=f0,
                F1_same_claim=None,
                F2_truth_equivalent=f2,
            ),
            "truth_A_implies_B": truth_a,
            "truth_B_implies_A": truth_b,
            "error_types": (),
            "quality_tier": QualityTier.UNKNOWN,
            "resolution_method": None,
            "evidence_ids_used": evidence_ids_used,
            "adjudication_notes": notes,
            "requires_adjudication": True,
            "train_eligibility": False,
            "eval_eligibility": False,
            "policy_version": policy.policy_version,
            "decision": Decision.REVIEW,
            "relation_provenance": (),
            "migration_metadata": {},
        }
    else:
        payload = {
            "schema_version": 2,
            "target_kind": target_kind,
            "target_id": target_id,
            "same_claim": selected.same_claim,
            "resolution_outcome": selected.resolution_outcome,
            "relation": selected.relation,
            "faithfulness_levels": FaithfulnessLevels(
                F0_representation_equivalent=f0,
                F1_same_claim=selected.same_claim,
                F2_truth_equivalent=f2,
            ),
            "truth_A_implies_B": truth_a,
            "truth_B_implies_A": truth_b,
            "error_types": selected.error_types,
            "quality_tier": selected.quality_tier,
            "resolution_method": selected.resolution_method,
            "evidence_ids_used": evidence_ids_used,
            "adjudication_notes": notes,
            "requires_adjudication": False,
            # LF-024 has no verified production-admission capability yet.
            # Semantic resolution therefore cannot authorize downstream use.
            "train_eligibility": False,
            "eval_eligibility": False,
            "policy_version": policy.policy_version,
            "decision": None,
            "relation_provenance": selected.provenance,
            "migration_metadata": {},
        }
    label_id = make_id(
        LABEL_PREFIX,
        {
            "schema": "resolved_label_lf024_v1",
            **ResolvedLabel.model_construct(**payload).model_dump(
                mode="json", exclude={"label_id"}
            ),
        },
    )
    return ResolvedLabel.model_validate({"label_id": label_id, **payload})


def _link_target(target: ResolutionTarget, label_id: str) -> ResolutionTarget:
    payload = {**target.model_dump(mode="python"), "resolved_label_id": label_id}
    if isinstance(target, PairRecord):
        return PairRecord.model_validate(payload)
    return NLPLeanRecord.model_validate(payload)


def _build_audit(
    *,
    target_kind: SemanticLabelTargetKind,
    target_id: str,
    target_input_sha256: str,
    linked_target_sha256: str,
    policy: ActiveLabelResolutionPolicy,
    candidates: Sequence[ResolutionCandidate],
    evidence_records: Sequence[EvidenceRecord],
    admissions: Sequence[EvidenceAdmissionRecord],
    derivation: EvidenceDerivationRecord | None,
    selected: ResolutionCandidate | None,
    label: ResolvedLabel,
    prior_label_id: str | None,
    conflicts: tuple[ResolutionConflictRecord, ...],
    overrides: tuple[ResolutionOverrideRecord, ...],
    reason_codes: tuple[str, ...],
    resolved_at: datetime.datetime,
) -> ResolutionAuditRecord:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "target_kind": target_kind,
        "target_id": target_id,
        "target_input_sha256": target_input_sha256,
        "linked_target_sha256": linked_target_sha256,
        "policy_version": policy.policy_version,
        "policy_file_sha256": policy.policy_file_sha256,
        "gate_file_sha256": policy.gate_file_sha256,
        "candidate_ids": tuple(sorted(item.candidate_id for item in candidates)),
        "input_evidence_ids": tuple(sorted(item.evidence_id for item in evidence_records)),
        "admission_ids": tuple(sorted(item.admission_id for item in admissions)),
        "derivation_id": None if derivation is None else derivation.derivation_id,
        "selected_candidate_id": None if selected is None else selected.candidate_id,
        "output_label_id": label.label_id,
        "prior_label_id": prior_label_id,
        "conflict_ids": tuple(item.conflict_id for item in conflicts),
        "override_ids": tuple(item.override_id for item in overrides),
        "status": "unresolved" if selected is None else "resolved",
        "reason_codes": tuple(sorted(set(reason_codes))),
        "resolved_at": resolved_at,
    }
    audit_id = make_id(
        RESOLUTION_AUDIT_PREFIX,
        ResolutionAuditRecord.model_construct(audit_id="", **payload).model_dump(
            mode="json", exclude={"audit_id", "resolved_at"}
        ),
    )
    return ResolutionAuditRecord.model_validate({"audit_id": audit_id, **payload})


def _resolve_target_diagnostic(
    *,
    target: ResolutionTarget,
    evidence_records: Sequence[EvidenceRecord],
    admissions: Sequence[EvidenceAdmissionRecord],
    candidates: Sequence[ResolutionCandidate],
    policy: ActiveLabelResolutionPolicy,
    resolved_at: datetime.datetime,
    prior_label: ResolvedLabel | None = None,
) -> ResolutionArtifacts:
    """Exercise resolver semantics with explicit raw candidates.

    This private core exists for focused resolver tests and future typed
    authority-replay integration.  It is not a production authority boundary;
    public callers must use :func:`resolve_target` with an opaque
    ``VerifiedCandidateSet`` capability.

    Input ordering has no effect.  Malformed lineage raises
    ``ResolutionInputError``; genuine strong semantic/evidence conflicts
    instead produce an append-only conflict record and an unresolved label.
    """

    require_utc(resolved_at)
    target_kind, evidence_kind, target_id, linked_evidence_ids = _target_identity(target)
    prior_label_id = _validate_prior_label(
        target=target,
        target_kind=target_kind,
        target_id=target_id,
        prior_label=prior_label,
    )
    _validate_input_graph(
        target_kind=target_kind,
        evidence_kind=evidence_kind,
        target_id=target_id,
        linked_evidence_ids=linked_evidence_ids,
        evidence_records=evidence_records,
        admissions=admissions,
        candidates=candidates,
        policy=policy,
    )
    ordered_candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))

    conflicts: list[ResolutionConflictRecord] = []
    derivation: EvidenceDerivationRecord | None
    try:
        derivation = derive_admitted_evidence(
            target_kind=evidence_kind,
            target_id=target_id,
            evidence_records=evidence_records,
            admissions=admissions,
        )
    except EvidenceFactConflictError as exc:
        derivation = None
        conflicts.append(
            build_resolution_conflict_record(
                target_kind=target_kind,
                target_id=target_id,
                candidate_ids=(),
                evidence_ids=tuple(sorted({*exc.true_support_ids, *exc.false_support_ids})),
                source_ranks=(3,),
                reason_codes=(
                    ResolutionConflictReason.TRUTH_A_IMPLIES_B_DISAGREEMENT
                    if exc.direction == "A_to_B"
                    else ResolutionConflictReason.TRUTH_B_IMPLIES_A_DISAGREEMENT,
                ),
                policy_version=policy.policy_version,
                policy_hash=policy.policy_file_sha256,
                detected_at=resolved_at,
                prior_label_id=prior_label_id,
            )
        )
    except EvidenceAggregationError as exc:
        raise ResolutionInputError(str(exc)) from exc

    strong = tuple(item for item in ordered_candidates if item.source_rank <= 3)
    for index, first in enumerate(strong):
        for second in strong[index + 1 :]:
            candidate_conflict_reasons = _candidate_conflict_reasons(first, second)
            if candidate_conflict_reasons:
                conflicts.append(
                    build_resolution_conflict_record(
                        target_kind=target_kind,
                        target_id=target_id,
                        candidate_ids=(first.candidate_id, second.candidate_id),
                        evidence_ids=tuple(
                            sorted(
                                {
                                    *first.accepted_evidence_ids,
                                    *second.accepted_evidence_ids,
                                }
                            )
                        ),
                        source_ranks=tuple(sorted({first.source_rank, second.source_rank})),
                        reason_codes=candidate_conflict_reasons,
                        policy_version=policy.policy_version,
                        policy_hash=policy.policy_file_sha256,
                        detected_at=resolved_at,
                        prior_label_id=prior_label_id,
                    )
                )
    mechanically_inconsistent_candidate_ids: set[str] = set()
    if derivation is not None:
        for candidate in ordered_candidates:
            evidence_conflict_reasons = _candidate_evidence_conflict_reasons(candidate, derivation)
            if evidence_conflict_reasons:
                mechanically_inconsistent_candidate_ids.add(candidate.candidate_id)
                if candidate.source_rank > 3:
                    # Mechanical certificates outrank weak supervision.  The
                    # weak candidate is excluded below and, when a consistent
                    # strong candidate exists, appears in its override log.
                    continue
                support_ids: set[str] = set()
                if (
                    _trusted_candidate_f0(candidate) is False
                    and derivation.F0_representation_equivalent is True
                ):
                    support_ids.update(derivation.F0_support_evidence_ids)
                if candidate.same_claim is True and derivation.F2_truth_equivalent is False:
                    support_ids.update(derivation.truth_A_implies_B_support_evidence_ids)
                    support_ids.update(derivation.truth_B_implies_A_support_evidence_ids)
                if (
                    ResolutionConflictReason.TRUTH_A_IMPLIES_B_DISAGREEMENT
                    in evidence_conflict_reasons
                ):
                    support_ids.update(derivation.truth_A_implies_B_support_evidence_ids)
                if (
                    ResolutionConflictReason.TRUTH_B_IMPLIES_A_DISAGREEMENT
                    in evidence_conflict_reasons
                ):
                    support_ids.update(derivation.truth_B_implies_A_support_evidence_ids)
                conflicts.append(
                    build_resolution_conflict_record(
                        target_kind=target_kind,
                        target_id=target_id,
                        candidate_ids=(candidate.candidate_id,),
                        evidence_ids=tuple(sorted(support_ids)),
                        source_ranks=(candidate.source_rank,),
                        reason_codes=evidence_conflict_reasons,
                        policy_version=policy.policy_version,
                        policy_hash=policy.policy_file_sha256,
                        detected_at=resolved_at,
                        prior_label_id=prior_label_id,
                    )
                )

    # Different conflict pairs can produce the same content-addressed record.
    conflicts_by_id = {item.conflict_id: item for item in conflicts}
    conflict_records = tuple(conflicts_by_id[key] for key in sorted(conflicts_by_id))
    eligible_candidates = tuple(
        item
        for item in ordered_candidates
        if item.candidate_id not in mechanically_inconsistent_candidate_ids
    )
    selected: ResolutionCandidate | None = None
    overrides: tuple[ResolutionOverrideRecord, ...] = ()
    resolution_reasons: set[str] = set()
    if conflict_records:
        resolution_reasons.add("strong_evidence_conflict")
    elif not eligible_candidates:
        resolution_reasons.add("no_admissible_semantic_candidate")
        if mechanically_inconsistent_candidate_ids:
            resolution_reasons.add("weak_candidate_mechanical_conflict")
    else:
        best_rank = min(item.source_rank for item in eligible_candidates)
        best = tuple(item for item in eligible_candidates if item.source_rank == best_rank)
        terminal_best = tuple(
            item for item in best if item.commitment is CandidateCommitment.TERMINAL
        )
        terminal_keys = {
            (item.same_claim, item.resolution_outcome, item.relation) for item in terminal_best
        }
        if len(terminal_keys) > 1:
            # Equal-rank weak disagreements have no policy winner. Equal-rank
            # strong disagreements were already recorded above.
            resolution_reasons.add("equal_rank_candidate_disagreement")
        elif not terminal_best:
            resolution_reasons.add("best_authority_is_partial")
        else:
            selected = min(terminal_best, key=lambda item: item.candidate_id)
            overridden = tuple(
                item for item in ordered_candidates if item.candidate_id != selected.candidate_id
            )
            if overridden:
                override_reasons: set[ResolutionOverrideReason] = set()
                if any(item.source_rank <= 3 for item in overridden):
                    override_reasons.add(ResolutionOverrideReason.STRONG_OVER_STRONG_AGREEING)
                if selected.source_rank <= 3 and any(item.source_rank == 4 for item in overridden):
                    override_reasons.add(ResolutionOverrideReason.STRONG_OVER_WEAK)
                if selected.source_rank == 4:
                    override_reasons.add(ResolutionOverrideReason.WEAK_OVER_WEAK)
                overrides = (
                    build_resolution_override_record(
                        target_kind=target_kind,
                        target_id=target_id,
                        winner_candidate_id=selected.candidate_id,
                        overridden_candidate_ids=tuple(item.candidate_id for item in overridden),
                        evidence_ids=tuple(
                            sorted(
                                {
                                    evidence_id
                                    for item in ordered_candidates
                                    for evidence_id in item.accepted_evidence_ids
                                }
                            )
                        ),
                        source_ranks=tuple(
                            sorted({item.source_rank for item in ordered_candidates})
                        ),
                        reason_codes=override_reasons,
                        policy_version=policy.policy_version,
                        policy_hash=policy.policy_file_sha256,
                        logged_at=resolved_at,
                        prior_label_id=prior_label_id,
                    ),
                )

    used_evidence: set[str] = set()
    if derivation is not None:
        used_evidence.update(derivation.accepted_evidence_ids)
    used_evidence.update(
        evidence_id
        for candidate in ordered_candidates
        for evidence_id in candidate.accepted_evidence_ids
    )
    used_evidence.update(
        evidence_id for conflict in conflict_records for evidence_id in conflict.evidence_ids
    )
    notes_parts = [*sorted(resolution_reasons)]
    notes_parts.extend(f"conflict:{item.conflict_id}" for item in conflict_records)
    notes_parts.extend(f"override:{item.override_id}" for item in overrides)
    notes = "; ".join(notes_parts)
    label = _make_label(
        target_kind=target_kind,
        target_id=target_id,
        policy=policy,
        derivation=derivation,
        selected=selected,
        evidence_ids_used=tuple(sorted(used_evidence)),
        notes=notes,
    )
    if prior_label is not None and label != prior_label:
        raise ResolutionInputError(
            "changing an existing resolved label requires a typed supersession/incident "
            "artifact; LF-024 core permits idempotent replay only"
        )
    linked_target = _link_target(target, label.label_id)
    audit = _build_audit(
        target_kind=target_kind,
        target_id=target_id,
        target_input_sha256=hash_canonical(target.model_dump(mode="json")),
        linked_target_sha256=hash_canonical(linked_target.model_dump(mode="json")),
        policy=policy,
        candidates=ordered_candidates,
        evidence_records=evidence_records,
        admissions=admissions,
        derivation=derivation,
        selected=selected,
        label=label,
        prior_label_id=prior_label_id,
        conflicts=conflict_records,
        overrides=overrides,
        reason_codes=tuple(sorted(resolution_reasons)),
        resolved_at=resolved_at,
    )
    return ResolutionArtifacts(
        target=linked_target,
        label=label,
        audit=audit,
        derivation=derivation,
        conflicts=conflict_records,
        overrides=overrides,
    )


def resolve_target(
    *,
    target: ResolutionTarget,
    evidence_records: Sequence[EvidenceRecord],
    admissions: Sequence[EvidenceAdmissionRecord],
    verified_candidates: VerifiedCandidateSet,
    policy: ActiveLabelResolutionPolicy,
    resolved_at: datetime.datetime,
    prior_label: ResolvedLabel | None = None,
) -> ResolutionArtifacts:
    """Resolve one target using only an opaque verified-candidate capability.

    The only capability currently available to public callers is
    ``EMPTY_VERIFIED_CANDIDATE_SET``.  Consequently this boundary can emit only
    unresolved semantic labels until typed authority replay is implemented.
    """

    candidates = _candidates_from_verified_set(verified_candidates)
    return _resolve_target_diagnostic(
        target=target,
        evidence_records=evidence_records,
        admissions=admissions,
        candidates=candidates,
        policy=policy,
        resolved_at=resolved_at,
        prior_label=prior_label,
    )


def _verify_resolution_artifacts_diagnostic(
    *,
    artifacts: ResolutionArtifacts,
    original_target: ResolutionTarget,
    evidence_records: Sequence[EvidenceRecord],
    admissions: Sequence[EvidenceAdmissionRecord],
    candidates: Sequence[ResolutionCandidate],
    policy: ActiveLabelResolutionPolicy,
    prior_label: ResolvedLabel | None = None,
) -> None:
    """Replay private diagnostic semantics from explicit candidate records."""

    replay = _resolve_target_diagnostic(
        target=original_target,
        evidence_records=evidence_records,
        admissions=admissions,
        candidates=candidates,
        policy=policy,
        resolved_at=artifacts.audit.resolved_at,
        prior_label=prior_label,
    )
    if artifacts != replay:
        raise ResolutionInputError("resolution artifacts differ from deterministic replay")


def verify_resolution_artifacts(
    *,
    artifacts: ResolutionArtifacts,
    original_target: ResolutionTarget,
    evidence_records: Sequence[EvidenceRecord],
    admissions: Sequence[EvidenceAdmissionRecord],
    verified_candidates: VerifiedCandidateSet,
    policy: ActiveLabelResolutionPolicy,
    prior_label: ResolvedLabel | None = None,
) -> None:
    """Reject artifacts that differ from replay of the same sealed capability."""

    replay = resolve_target(
        target=original_target,
        evidence_records=evidence_records,
        admissions=admissions,
        verified_candidates=verified_candidates,
        policy=policy,
        resolved_at=artifacts.audit.resolved_at,
        prior_label=prior_label,
    )
    if artifacts != replay:
        raise ResolutionInputError("resolution artifacts differ from deterministic replay")


__all__ = [
    "EMPTY_VERIFIED_CANDIDATE_SET",
    "RESOLUTION_AUDIT_PREFIX",
    "ResolutionArtifacts",
    "ResolutionAuditRecord",
    "ResolutionInputError",
    "VerifiedCandidateSet",
    "resolve_target",
    "verify_resolution_artifacts",
]
