"""Admission-gated symbolic evidence derivation for LF-024.

This module is the evidence-to-label firewall, not a label resolver.  It
derives only F0 and closed-truth (F2) facts that the registered symbolic
evidence can establish.  In particular, typechecking, claim alignment, and
raw LLM or human judgments never create an F1/same-claim decision here.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Literal, Self

from pydantic import Field, StrictBool, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import (
    ArtifactClass,
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
)
from leanfaith.schemas.evidence import (
    AuditValue,
    CounterexampleValue,
    DefeqValue,
    EvidenceRecord,
    ProofValue,
)
from leanfaith.schemas.ids import (
    DRAFT_PREFIX,
    EVIDENCE_PREFIX,
    HEX64_PATTERN,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    THEOREM_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.variant import FAMILY_ID_PATTERN

EVIDENCE_ADMISSION_PREFIX = "evidence_admission"
EVIDENCE_DERIVATION_PREFIX = "evidence_derivation"

_TARGET_ID_PATTERNS: dict[EvidenceTargetKind, str] = {
    EvidenceTargetKind.THEOREM: id_pattern(THEOREM_PREFIX),
    EvidenceTargetKind.LEAN_PAIR: id_pattern(PAIR_PREFIX),
    EvidenceTargetKind.NL_LEAN: id_pattern(NL_LEAN_PREFIX),
    EvidenceTargetKind.TRANSFORMATION_DRAFT: id_pattern(DRAFT_PREFIX),
    EvidenceTargetKind.TRANSFORMATION_FAMILY: FAMILY_ID_PATTERN,
}


class EvidenceAggregationError(ValueError):
    """The evidence/admission graph is incomplete, inconsistent, or conflicting."""


class EvidenceFactConflictError(EvidenceAggregationError):
    """Accepted evidence supports both truth values for one direction."""

    def __init__(
        self,
        *,
        direction: Literal["A_to_B", "B_to_A"],
        true_support_ids: Iterable[str],
        false_support_ids: Iterable[str],
    ) -> None:
        self.direction = direction
        self.true_support_ids = tuple(sorted(set(true_support_ids)))
        self.false_support_ids = tuple(sorted(set(false_support_ids)))
        super().__init__(
            f"conflicting admitted evidence derives both true and false for {direction}"
        )


def _sorted_unique_ids(values: tuple[str, ...], *, field_name: str, prefix: str) -> None:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{field_name} must be sorted and unique")
    pattern = id_pattern(prefix)
    if any(re.fullmatch(pattern, value) is None for value in values):
        raise ValueError(f"{field_name} contains an invalid {prefix} ID")


def _target_matches(kind: EvidenceTargetKind, target_id: str) -> bool:
    return re.fullmatch(_TARGET_ID_PATTERNS[kind], target_id) is not None


class EvidenceAdmissionRecord(StrictModel):
    """Content-addressed replay admission for a closed set of evidence records.

    An admission may describe smoke or diagnostic evidence for auditing, but
    only a replay-passed production admission is production eligible.  The
    equality is enforced in both directions so callers cannot downgrade or
    upgrade eligibility independently of the bound replay result.
    """

    schema_version: Literal[1] = 1
    admission_id: str = Field(pattern=id_pattern(EVIDENCE_ADMISSION_PREFIX))
    target_kind: EvidenceTargetKind
    target_id: str
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    artifact_class: ArtifactClass
    manifest_artifact_id: str = Field(min_length=1)
    manifest_artifact_sha256: str = Field(pattern=HEX64_PATTERN)
    replay_artifact_id: str = Field(min_length=1)
    replay_artifact_sha256: str = Field(pattern=HEX64_PATTERN)
    replay_passed: StrictBool
    production_eligible: StrictBool
    policy_sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _content_addressed_and_coherent(self) -> Self:
        if not _target_matches(self.target_kind, self.target_id):
            raise ValueError("target_id does not match target_kind")
        _sorted_unique_ids(
            self.evidence_ids,
            field_name="evidence_ids",
            prefix=EVIDENCE_PREFIX,
        )
        expected_eligibility = (
            self.artifact_class is ArtifactClass.PRODUCTION and self.replay_passed
        )
        if self.production_eligible is not expected_eligibility:
            raise ValueError(
                "production_eligible must equal (artifact_class=production and replay_passed=true)"
            )
        expected_id = make_id(
            EVIDENCE_ADMISSION_PREFIX,
            self.model_dump(mode="json", exclude={"admission_id"}),
        )
        if self.admission_id != expected_id:
            raise ValueError("admission_id does not match admission content")
        return self


def build_evidence_admission_record(
    *,
    target_kind: EvidenceTargetKind,
    target_id: str,
    evidence_ids: Sequence[str],
    artifact_class: ArtifactClass,
    manifest_artifact_id: str,
    manifest_artifact_sha256: str,
    replay_artifact_id: str,
    replay_artifact_sha256: str,
    replay_passed: bool,
    policy_sha256: str,
) -> EvidenceAdmissionRecord:
    """Build one canonical admission independent of caller input order.

    Duplicate evidence IDs are rejected instead of being silently collapsed;
    this keeps accidental double counting visible at the admission boundary.
    Pydantic's strict fields perform the remaining type and hash validation.
    """

    ordered_ids = tuple(sorted(evidence_ids))
    if len(ordered_ids) != len(set(ordered_ids)):
        raise EvidenceAggregationError("duplicate evidence IDs in admission request")
    values: dict[str, object] = {
        "schema_version": 1,
        "target_kind": target_kind,
        "target_id": target_id,
        "evidence_ids": ordered_ids,
        "artifact_class": artifact_class,
        "manifest_artifact_id": manifest_artifact_id,
        "manifest_artifact_sha256": manifest_artifact_sha256,
        "replay_artifact_id": replay_artifact_id,
        "replay_artifact_sha256": replay_artifact_sha256,
        "replay_passed": replay_passed,
        "production_eligible": (
            artifact_class is ArtifactClass.PRODUCTION and replay_passed is True
        ),
        "policy_sha256": policy_sha256,
    }
    admission_id = make_id(EVIDENCE_ADMISSION_PREFIX, values)
    return EvidenceAdmissionRecord.model_validate({"admission_id": admission_id, **values})


class EvidenceDerivationRecord(StrictModel):
    """Deterministic F0/F2 projection of admitted evidence.

    ``accepted_reason_codes`` and ``ignored_reason_codes`` form a complete,
    auditable partition of the input evidence.  Audit records used to admit a
    proof or counterexample are accepted support in their own right; an audit
    that supports nothing is ignored.
    """

    schema_version: Literal[1] = 1
    derivation_id: str = Field(pattern=id_pattern(EVIDENCE_DERIVATION_PREFIX))
    target_kind: EvidenceTargetKind
    target_id: str
    admission_ids: tuple[str, ...]
    input_evidence_ids: tuple[str, ...]
    accepted_evidence_ids: tuple[str, ...]
    ignored_evidence_ids: tuple[str, ...]
    accepted_reason_codes: dict[str, tuple[str, ...]]
    ignored_reason_codes: dict[str, tuple[str, ...]]
    F0_representation_equivalent: bool | None
    F1_same_claim: Literal[None] = None
    truth_A_implies_B: bool | None
    truth_B_implies_A: bool | None
    F2_truth_equivalent: bool | None
    F0_support_evidence_ids: tuple[str, ...]
    truth_A_implies_B_support_evidence_ids: tuple[str, ...]
    truth_B_implies_A_support_evidence_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _content_addressed_and_partitioned(self) -> Self:
        if not _target_matches(self.target_kind, self.target_id):
            raise ValueError("target_id does not match target_kind")
        _sorted_unique_ids(
            self.admission_ids,
            field_name="admission_ids",
            prefix=EVIDENCE_ADMISSION_PREFIX,
        )
        for field_name in (
            "input_evidence_ids",
            "accepted_evidence_ids",
            "ignored_evidence_ids",
            "F0_support_evidence_ids",
            "truth_A_implies_B_support_evidence_ids",
            "truth_B_implies_A_support_evidence_ids",
        ):
            _sorted_unique_ids(
                getattr(self, field_name),
                field_name=field_name,
                prefix=EVIDENCE_PREFIX,
            )
        accepted = set(self.accepted_evidence_ids)
        ignored = set(self.ignored_evidence_ids)
        inputs = set(self.input_evidence_ids)
        if accepted & ignored or accepted | ignored != inputs:
            raise ValueError("accepted and ignored evidence must partition all inputs")
        if set(self.accepted_reason_codes) != accepted:
            raise ValueError("accepted_reason_codes keys must equal accepted_evidence_ids")
        if set(self.ignored_reason_codes) != ignored:
            raise ValueError("ignored_reason_codes keys must equal ignored_evidence_ids")
        for mapping_name, mapping in (
            ("accepted_reason_codes", self.accepted_reason_codes),
            ("ignored_reason_codes", self.ignored_reason_codes),
        ):
            for evidence_id, reasons in mapping.items():
                if not reasons or reasons != tuple(sorted(set(reasons))):
                    raise ValueError(
                        f"{mapping_name}[{evidence_id!r}] must be nonempty, sorted, and unique"
                    )
        supports = (
            set(self.F0_support_evidence_ids)
            | set(self.truth_A_implies_B_support_evidence_ids)
            | set(self.truth_B_implies_A_support_evidence_ids)
        )
        if not supports <= accepted:
            raise ValueError("all support evidence must be accepted")
        if self.F0_representation_equivalent is True and not self.F0_support_evidence_ids:
            raise ValueError("F0=true requires support evidence")
        if self.F0_representation_equivalent is not True and self.F0_support_evidence_ids:
            raise ValueError("F0 support is permitted only for F0=true")
        for value, support, name in (
            (
                self.truth_A_implies_B,
                self.truth_A_implies_B_support_evidence_ids,
                "truth_A_implies_B",
            ),
            (
                self.truth_B_implies_A,
                self.truth_B_implies_A_support_evidence_ids,
                "truth_B_implies_A",
            ),
        ):
            if value is not None and not support:
                raise ValueError(f"{name} requires support evidence")
            if value is None and support:
                raise ValueError(f"{name} support requires a derived truth value")
        expected_f2: bool | None
        if self.truth_A_implies_B is True and self.truth_B_implies_A is True:
            expected_f2 = True
        elif self.truth_A_implies_B is False or self.truth_B_implies_A is False:
            expected_f2 = False
        else:
            expected_f2 = None
        if self.F2_truth_equivalent is not expected_f2:
            raise ValueError("F2_truth_equivalent disagrees with directional truth fields")
        expected_id = make_id(
            EVIDENCE_DERIVATION_PREFIX,
            self.model_dump(mode="json", exclude={"derivation_id"}),
        )
        if self.derivation_id != expected_id:
            raise ValueError("derivation_id does not match derivation content")
        return self


def _audit_is_accepted(record: EvidenceRecord) -> bool:
    return (
        record.kind is EvidenceKind.AXIOM_AUDIT
        and record.status is EvidenceExecutionStatus.SUCCESS
        and isinstance(record.value, AuditValue)
        and bool(record.value.checks)
        and all(value is True for value in record.value.checks.values())
        and not record.value.violation_codes
    )


def _add_reason(reasons: dict[str, set[str]], evidence_id: str, reason_code: str) -> None:
    reasons[evidence_id].add(reason_code)


def derive_admitted_evidence(
    *,
    target_kind: EvidenceTargetKind,
    target_id: str,
    evidence_records: Sequence[EvidenceRecord],
    admissions: Sequence[EvidenceAdmissionRecord],
) -> EvidenceDerivationRecord:
    """Derive F0/F2 facts from an exactly admitted evidence collection.

    The input graph is closed: every evidence record must be covered by
    exactly one admission, and every admitted evidence ID must be present.
    Operationally unsuccessful or non-production evidence remains in the
    derivation as ignored provenance rather than silently disappearing.
    """

    if not _target_matches(target_kind, target_id):
        raise EvidenceAggregationError("target_id does not match target_kind")

    evidence_by_id: dict[str, EvidenceRecord] = {}
    for record in evidence_records:
        if record.evidence_id in evidence_by_id:
            raise EvidenceAggregationError(f"duplicate evidence ID {record.evidence_id}")
        if record.target_kind is not target_kind or record.target_id != target_id:
            raise EvidenceAggregationError(
                f"evidence {record.evidence_id} targets a different record"
            )
        evidence_by_id[record.evidence_id] = record

    admissions_by_id: dict[str, EvidenceAdmissionRecord] = {}
    admission_by_evidence_id: dict[str, EvidenceAdmissionRecord] = {}
    for admission in admissions:
        if admission.admission_id in admissions_by_id:
            raise EvidenceAggregationError(f"duplicate admission ID {admission.admission_id}")
        if admission.target_kind is not target_kind or admission.target_id != target_id:
            raise EvidenceAggregationError(
                f"admission {admission.admission_id} targets a different record"
            )
        admissions_by_id[admission.admission_id] = admission
        for evidence_id in admission.evidence_ids:
            if evidence_id not in evidence_by_id:
                raise EvidenceAggregationError(
                    f"admission {admission.admission_id} references missing evidence {evidence_id}"
                )
            if evidence_id in admission_by_evidence_id:
                raise EvidenceAggregationError(
                    f"evidence {evidence_id} is linked to multiple admissions"
                )
            admission_by_evidence_id[evidence_id] = admission

    missing_admissions = sorted(set(evidence_by_id) - set(admission_by_evidence_id))
    if missing_admissions:
        raise EvidenceAggregationError(
            "evidence records lack admissions: " + ", ".join(missing_admissions)
        )

    accepted_reasons: dict[str, set[str]] = defaultdict(set)
    ignored_reasons: dict[str, set[str]] = defaultdict(set)
    eligible_success_ids: set[str] = set()
    for evidence_id, record in evidence_by_id.items():
        admission = admission_by_evidence_id[evidence_id]
        if not admission.production_eligible:
            _add_reason(ignored_reasons, evidence_id, "admission_not_production_eligible")
        if record.status is not EvidenceExecutionStatus.SUCCESS:
            _add_reason(
                ignored_reasons,
                evidence_id,
                f"evidence_status_{record.status.value}",
            )
        if admission.production_eligible and record.status is EvidenceExecutionStatus.SUCCESS:
            eligible_success_ids.add(evidence_id)

    valid_audit_ids = {
        evidence_id
        for evidence_id in eligible_success_ids
        if _audit_is_accepted(evidence_by_id[evidence_id])
    }
    audit_ids_by_raw_artifact: dict[str, list[str]] = defaultdict(list)
    for evidence_id in sorted(valid_audit_ids):
        artifact = evidence_by_id[evidence_id].raw_artifact
        if artifact is not None:
            audit_ids_by_raw_artifact[artifact].append(evidence_id)

    f0_support: set[str] = set()
    direction_true: dict[str, set[str]] = {"A_to_B": set(), "B_to_A": set()}
    direction_false: dict[str, set[str]] = {"A_to_B": set(), "B_to_A": set()}

    for evidence_id in sorted(eligible_success_ids):
        record = evidence_by_id[evidence_id]
        value = record.value

        if record.kind is EvidenceKind.DEFEQ and isinstance(value, DefeqValue):
            if value.outcome == "equal":
                f0_support.add(evidence_id)
                _add_reason(accepted_reasons, evidence_id, "supports_F0_true")
            else:
                _add_reason(ignored_reasons, evidence_id, "defeq_not_equal_is_not_F0_false")
            continue

        if record.kind in {
            EvidenceKind.PROOF_A_IMPLIES_B,
            EvidenceKind.PROOF_B_IMPLIES_A,
        } and isinstance(value, ProofValue):
            direction = "A_to_B" if record.kind is EvidenceKind.PROOF_A_IMPLIES_B else "B_to_A"
            if value.outcome == "not_proved":
                _add_reason(ignored_reasons, evidence_id, "proof_not_proved_is_unknown")
                continue
            linked_id = record.metadata.get("axiom_audit_evidence_id")
            if not isinstance(linked_id, str) or not linked_id:
                _add_reason(ignored_reasons, evidence_id, "proved_missing_axiom_audit_link")
                continue
            linked = evidence_by_id.get(linked_id)
            if linked is None:
                raise EvidenceAggregationError(
                    f"proof evidence {evidence_id} links missing audit {linked_id}"
                )
            if linked.kind is not EvidenceKind.AXIOM_AUDIT:
                raise EvidenceAggregationError(
                    f"proof evidence {evidence_id} links non-audit evidence {linked_id}"
                )
            if linked_id not in valid_audit_ids:
                _add_reason(ignored_reasons, evidence_id, "proved_axiom_audit_not_accepted")
                continue
            direction_true[direction].update({evidence_id, linked_id})
            _add_reason(accepted_reasons, evidence_id, f"supports_truth_{direction}_true")
            _add_reason(accepted_reasons, linked_id, f"audits_truth_{direction}_true")
            continue

        if record.kind is EvidenceKind.COUNTEREXAMPLE and isinstance(value, CounterexampleValue):
            if value.outcome == "not_found":
                _add_reason(ignored_reasons, evidence_id, "counterexample_not_found_is_unknown")
                continue
            if value.outcome == "unsupported":
                _add_reason(ignored_reasons, evidence_id, "counterexample_unsupported")
                continue
            if value.direction == "equivalence_only":
                _add_reason(
                    ignored_reasons,
                    evidence_id,
                    "counterexample_equivalence_only_has_no_named_direction",
                )
                continue
            matching_audits = (
                audit_ids_by_raw_artifact.get(record.raw_artifact, ())
                if record.raw_artifact is not None
                else ()
            )
            if not matching_audits:
                _add_reason(
                    ignored_reasons,
                    evidence_id,
                    "found_counterexample_missing_matching_axiom_audit",
                )
                continue
            direction_false[value.direction].add(evidence_id)
            direction_false[value.direction].update(matching_audits)
            _add_reason(
                accepted_reasons,
                evidence_id,
                f"supports_truth_{value.direction}_false",
            )
            for audit_id in matching_audits:
                _add_reason(
                    accepted_reasons,
                    audit_id,
                    f"audits_truth_{value.direction}_false",
                )
            continue

        # These records can be useful to later policy layers, but not to the
        # mechanical F0/F2 derivation implemented here.
        if record.kind is EvidenceKind.TYPECHECK:
            _add_reason(ignored_reasons, evidence_id, "typecheck_does_not_set_F1")
        elif record.kind is EvidenceKind.LLM_JUDGMENT:
            _add_reason(ignored_reasons, evidence_id, "raw_llm_judgment_does_not_set_F1")
        elif record.kind is EvidenceKind.HUMAN_ANNOTATION:
            _add_reason(ignored_reasons, evidence_id, "raw_human_judgment_does_not_set_F1")
        elif record.kind is EvidenceKind.CLAIM_ALIGNMENT:
            _add_reason(ignored_reasons, evidence_id, "claim_alignment_alone_does_not_set_F1")
        elif record.kind is EvidenceKind.AXIOM_AUDIT:
            # A support reason may be added later/by an earlier linked record.
            pass
        elif record.kind is EvidenceKind.TRANSFORMATION_AUDIT:
            _add_reason(ignored_reasons, evidence_id, "transformation_audit_alone_does_not_set_F1")
        else:  # pragma: no cover - closed enum guard for future schema revisions
            _add_reason(ignored_reasons, evidence_id, "unsupported_evidence_kind")

    for direction in ("A_to_B", "B_to_A"):
        if direction_true[direction] and direction_false[direction]:
            raise EvidenceFactConflictError(
                direction=direction,  # type: ignore[arg-type]
                true_support_ids=direction_true[direction],
                false_support_ids=direction_false[direction],
            )

    # Classify successful, admitted audits only after every potential support
    # link has been considered.
    for audit_id in sorted(valid_audit_ids):
        if audit_id not in accepted_reasons:
            _add_reason(ignored_reasons, audit_id, "axiom_audit_without_accepted_subject")
    for evidence_id in sorted(eligible_success_ids):
        record = evidence_by_id[evidence_id]
        if record.kind is EvidenceKind.AXIOM_AUDIT and evidence_id not in valid_audit_ids:
            _add_reason(ignored_reasons, evidence_id, "axiom_audit_checks_not_accepted")

    accepted_ids = set(accepted_reasons)
    # Non-production and unsuccessful evidence is always ignored, even if a
    # malformed link attempted to add a support reason.
    accepted_ids &= eligible_success_ids
    for evidence_id in sorted(set(evidence_by_id) - accepted_ids):
        if evidence_id not in ignored_reasons:
            _add_reason(ignored_reasons, evidence_id, "no_derivable_F0_or_F2_fact")
    for evidence_id in tuple(ignored_reasons):
        if evidence_id in accepted_ids:
            del ignored_reasons[evidence_id]

    def direction_value(direction: str) -> bool | None:
        if direction_true[direction]:
            return True
        if direction_false[direction]:
            return False
        return None

    truth_a_to_b = direction_value("A_to_B")
    truth_b_to_a = direction_value("B_to_A")
    if truth_a_to_b is True and truth_b_to_a is True:
        f2: bool | None = True
    elif truth_a_to_b is False or truth_b_to_a is False:
        f2 = False
    else:
        f2 = None

    values: dict[str, object] = {
        "schema_version": 1,
        "target_kind": target_kind,
        "target_id": target_id,
        "admission_ids": tuple(sorted(admissions_by_id)),
        "input_evidence_ids": tuple(sorted(evidence_by_id)),
        "accepted_evidence_ids": tuple(sorted(accepted_ids)),
        "ignored_evidence_ids": tuple(sorted(set(evidence_by_id) - accepted_ids)),
        "accepted_reason_codes": {
            evidence_id: tuple(sorted(accepted_reasons[evidence_id]))
            for evidence_id in sorted(accepted_ids)
        },
        "ignored_reason_codes": {
            evidence_id: tuple(sorted(ignored_reasons[evidence_id]))
            for evidence_id in sorted(set(evidence_by_id) - accepted_ids)
        },
        "F0_representation_equivalent": True if f0_support else None,
        "F1_same_claim": None,
        "truth_A_implies_B": truth_a_to_b,
        "truth_B_implies_A": truth_b_to_a,
        "F2_truth_equivalent": f2,
        "F0_support_evidence_ids": tuple(sorted(f0_support)),
        "truth_A_implies_B_support_evidence_ids": tuple(
            sorted(direction_true["A_to_B"] | direction_false["A_to_B"])
        ),
        "truth_B_implies_A_support_evidence_ids": tuple(
            sorted(direction_true["B_to_A"] | direction_false["B_to_A"])
        ),
    }
    derivation_id = make_id(EVIDENCE_DERIVATION_PREFIX, values)
    return EvidenceDerivationRecord.model_validate({"derivation_id": derivation_id, **values})


def verify_evidence_derivation(
    *,
    derivation: EvidenceDerivationRecord,
    evidence_records: Sequence[EvidenceRecord],
    admissions: Sequence[EvidenceAdmissionRecord],
) -> None:
    """Reject a persisted derivation that differs from deterministic replay."""

    expected = derive_admitted_evidence(
        target_kind=derivation.target_kind,
        target_id=derivation.target_id,
        evidence_records=evidence_records,
        admissions=admissions,
    )
    if derivation != expected:
        raise EvidenceAggregationError("evidence derivation differs from deterministic replay")


__all__ = [
    "EVIDENCE_ADMISSION_PREFIX",
    "EVIDENCE_DERIVATION_PREFIX",
    "EvidenceAdmissionRecord",
    "EvidenceAggregationError",
    "EvidenceDerivationRecord",
    "EvidenceFactConflictError",
    "build_evidence_admission_record",
    "derive_admitted_evidence",
    "verify_evidence_derivation",
]
