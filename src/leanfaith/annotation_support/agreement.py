"""Agreement statistics for independent, pre-adjudication annotations.

The functions in this module never consume adjudicated labels and never turn
agreement into a semantic resolution.  ``cannot_assess_yet`` and ``relation =
null`` remain explicit categories so that workflow uncertainty cannot be
silently dropped to improve agreement.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.schemas.annotation import AnnotationRecord
from leanfaith.schemas.enums import AnnotationAnswer, RelationLabel

_REPORT_ID = r"^lf023_annotation_agreement_v1:[0-9a-f]{64}$"
_HEX64 = r"^[0-9a-f]{64}$"
_SAME_CLAIM_CATEGORIES = tuple(item.value for item in AnnotationAnswer)
_RELATION_CATEGORIES = (*tuple(item.value for item in RelationLabel), "null")
_ANNOTATOR_SLOTS = frozenset({"independent_annotator_1", "independent_annotator_2"})


class AnnotationAgreementError(ValueError):
    """Raised when two raw-label collections cannot be paired safely."""


class KappaResultV1(StrictModel):
    status: Literal["defined", "undefined_degenerate_marginals"]
    value: float | None = Field(default=None, ge=-1.0, le=1.0)
    observed_agreement: float = Field(ge=0.0, le=1.0)
    expected_agreement: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.status == "defined" and self.value is None:
            raise ValueError("defined kappa requires a numeric value")
        if self.status != "defined" and self.value is not None:
            raise ValueError("undefined kappa cannot carry a numeric value")
        return self


class CategoryAgreementV1(StrictModel):
    category: str = Field(min_length=1)
    first_count: int = Field(ge=0)
    second_count: int = Field(ge=0)
    both_count: int = Field(ge=0)
    either_count: int = Field(ge=0)
    both_over_either: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.both_count > min(self.first_count, self.second_count):
            raise ValueError("both_count exceeds an annotator marginal")
        if self.either_count < max(self.first_count, self.second_count):
            raise ValueError("either_count is below an annotator marginal")
        expected = None if self.either_count == 0 else self.both_count / self.either_count
        if self.both_over_either != expected:
            raise ValueError("both_over_either differs from exact counts")
        return self


class AnnotationAgreementReportV1(StrictModel):
    """Deterministic report over one paired independent-label collection."""

    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=_REPORT_ID)
    report_kind: Literal["lf023_raw_independent_annotation_agreement_v1"]
    target_count: int = Field(ge=1)
    first_annotator_id: str = Field(min_length=1)
    second_annotator_id: str = Field(min_length=1)
    first_round_id: str = Field(min_length=1)
    second_round_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    guideline_sha256: str = Field(pattern=_HEX64)
    first_annotator_slot: str = Field(min_length=1)
    second_annotator_slot: str = Field(min_length=1)
    first_annotation_set_sha256: str = Field(pattern=_HEX64)
    second_annotation_set_sha256: str = Field(pattern=_HEX64)
    assignment_mode: Literal["operator_attested_human", "test_fixture"]
    origin_assurance: Literal["operator_attested", "test_fixture"]
    operator_attestation_verified: Literal[True] = True
    backend_origin_verified: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    same_claim_raw_agreement: float = Field(ge=0.0, le=1.0)
    same_claim_agreement_count: int = Field(ge=0)
    same_claim_kappa: KappaResultV1
    same_claim_categories: tuple[CategoryAgreementV1, ...]
    relation_raw_agreement: float = Field(ge=0.0, le=1.0)
    relation_agreement_count: int = Field(ge=0)
    relation_kappa: KappaResultV1
    relation_categories: tuple[CategoryAgreementV1, ...]
    uses_raw_pre_adjudication_records_only: Literal[True] = True
    adjudicated_labels_substituted: Literal[False] = False
    gate_closed_by_report: Literal[False] = False

    @model_validator(mode="after")
    def _coherent_and_content_addressed(self) -> Self:
        if self.first_annotator_id == self.second_annotator_id:
            raise ValueError("agreement requires two distinct annotators")
        if self.first_round_id != self.second_round_id:
            raise ValueError("agreement inputs must come from the same round")
        expected_assurance = (
            "operator_attested"
            if self.assignment_mode == "operator_attested_human"
            else "test_fixture"
        )
        if self.origin_assurance != expected_assurance:
            raise ValueError("agreement origin assurance differs from assignment mode")
        if {self.first_annotator_slot, self.second_annotator_slot} != _ANNOTATOR_SLOTS:
            raise ValueError("agreement requires the two registered independent annotator slots")
        if self.same_claim_agreement_count / self.target_count != self.same_claim_raw_agreement:
            raise ValueError("same-claim raw agreement differs from exact counts")
        if self.relation_agreement_count / self.target_count != self.relation_raw_agreement:
            raise ValueError("relation raw agreement differs from exact counts")
        if tuple(item.category for item in self.same_claim_categories) != (_SAME_CLAIM_CATEGORIES):
            raise ValueError("same-claim categories differ from the canonical order")
        if tuple(item.category for item in self.relation_categories) != (_RELATION_CATEGORIES):
            raise ValueError("relation categories differ from the canonical order")
        payload = self.model_dump(mode="json")
        expected = "lf023_annotation_agreement_v1:" + hash_canonical(
            {
                "schema": "lf023_annotation_agreement_v1",
                **{key: item for key, item in payload.items() if key != "report_id"},
            }
        )
        if self.report_id != expected:
            raise ValueError("agreement report ID differs from content")
        return self


def _index(
    records: Sequence[AnnotationRecord],
    *,
    side: str,
    allow_test_fixture: bool,
) -> tuple[dict[tuple[str, str], AnnotationRecord], str, str, str, str, str, str, str]:
    if not records:
        raise AnnotationAgreementError(f"{side} annotation collection is empty")
    by_target: dict[tuple[str, str], AnnotationRecord] = {}
    annotators: set[str] = set()
    rounds: set[str] = set()
    campaigns: set[str] = set()
    slots: set[str] = set()
    principals: set[str] = set()
    guidelines: set[str] = set()
    assignment_modes: set[str] = set()
    for record in records:
        key = (record.target_kind.value, record.target_id)
        if key in by_target:
            raise AnnotationAgreementError(f"{side} has duplicate target {key!r}")
        by_target[key] = record
        annotators.add(record.annotator_id)
        rounds.add(record.round_id)
        campaign = record.metadata.get("campaign_id")
        slot = record.metadata.get("annotator_slot")
        principal = record.metadata.get("annotator_principal_hash")
        guideline = record.metadata.get("guideline_sha256")
        assignment_mode = record.metadata.get("assignment_mode")
        if not isinstance(campaign, str) or not campaign:
            raise AnnotationAgreementError(f"{side} lacks a bound campaign_id")
        if not isinstance(slot, str) or slot not in _ANNOTATOR_SLOTS:
            raise AnnotationAgreementError(f"{side} lacks a registered annotator_slot")
        if (
            not isinstance(principal, str)
            or len(principal) != 64
            or any(character not in "0123456789abcdef" for character in principal)
        ):
            raise AnnotationAgreementError(f"{side} lacks an authenticated annotator principal")
        if (
            not isinstance(guideline, str)
            or len(guideline) != 64
            or any(character not in "0123456789abcdef" for character in guideline)
        ):
            raise AnnotationAgreementError(f"{side} lacks a bound guideline hash")
        if assignment_mode not in {"operator_attested_human", "test_fixture"}:
            raise AnnotationAgreementError(f"{side} lacks an authenticated assignment mode")
        origin_assurance = record.metadata.get("origin_assurance")
        fixture_only = record.metadata.get("fixture_only")
        import_role = record.metadata.get("import_role")
        if (
            record.metadata.get("raw_vote_only") is not True
            or record.metadata.get("resolved_label_created") is not False
            or record.metadata.get("gold_label_created") is not False
            or record.metadata.get("training_eligible") is not False
        ):
            raise AnnotationAgreementError(f"{side} is not a raw-only annotation")
        if assignment_mode == "operator_attested_human":
            if (
                origin_assurance != "operator_attested"
                or record.metadata.get("operator_attestation_verified") is not True
                or record.metadata.get("backend_origin_verified") is not False
                or record.metadata.get("human_gold_eligible") is not False
                or fixture_only is not False
                or import_role != "raw_operator_attested_annotation"
            ):
                raise AnnotationAgreementError(f"{side} has inconsistent operator assertions")
        elif not allow_test_fixture:
            raise AnnotationAgreementError(f"{side} contains test-fixture annotations")
        elif (
            origin_assurance != "test_fixture"
            or record.metadata.get("operator_attestation_verified") is not True
            or record.metadata.get("backend_origin_verified") is not False
            or record.metadata.get("human_gold_eligible") is not False
            or fixture_only is not True
            or import_role != "raw_annotation_test_fixture"
        ):
            raise AnnotationAgreementError(f"{side} has inconsistent fixture metadata")
        campaigns.add(campaign)
        slots.add(slot)
        principals.add(principal)
        guidelines.add(guideline)
        assignment_modes.add(assignment_mode)
    if (
        len(annotators) != 1
        or len(rounds) != 1
        or len(campaigns) != 1
        or len(slots) != 1
        or len(principals) != 1
        or len(guidelines) != 1
        or len(assignment_modes) != 1
    ):
        raise AnnotationAgreementError(
            f"{side} must contain one annotator, principal, round, campaign, slot, "
            "guideline, and assignment mode"
        )
    return (
        by_target,
        next(iter(annotators)),
        next(iter(rounds)),
        next(iter(campaigns)),
        next(iter(slots)),
        next(iter(principals)),
        next(iter(guidelines)),
        next(iter(assignment_modes)),
    )


def _kappa(
    first: Sequence[str],
    second: Sequence[str],
    *,
    categories: tuple[str, ...],
) -> KappaResultV1:
    size = len(first)
    observed = sum(left == right for left, right in zip(first, second, strict=True)) / size
    first_counts = Counter(first)
    second_counts = Counter(second)
    expected = sum(
        (first_counts[category] / size) * (second_counts[category] / size)
        for category in categories
    )
    denominator = 1.0 - expected
    if abs(denominator) <= 1e-15:
        return KappaResultV1(
            status="undefined_degenerate_marginals",
            value=None,
            observed_agreement=observed,
            expected_agreement=expected,
        )
    return KappaResultV1(
        status="defined",
        value=(observed - expected) / denominator,
        observed_agreement=observed,
        expected_agreement=expected,
    )


def _category_reports(
    first: Sequence[str],
    second: Sequence[str],
    *,
    categories: tuple[str, ...],
) -> tuple[CategoryAgreementV1, ...]:
    result: list[CategoryAgreementV1] = []
    for category in categories:
        first_count = sum(value == category for value in first)
        second_count = sum(value == category for value in second)
        both = sum(
            left == category and right == category
            for left, right in zip(first, second, strict=True)
        )
        either = sum(
            left == category or right == category for left, right in zip(first, second, strict=True)
        )
        result.append(
            CategoryAgreementV1(
                category=category,
                first_count=first_count,
                second_count=second_count,
                both_count=both,
                either_count=either,
                both_over_either=None if either == 0 else both / either,
            )
        )
    return tuple(result)


def _relation_value(record: AnnotationRecord) -> str:
    relation = record.relation
    return "null" if relation is None else relation.value


def compute_annotation_agreement(
    first_records: Sequence[AnnotationRecord],
    second_records: Sequence[AnnotationRecord],
    *,
    allow_test_fixture: bool = False,
) -> AnnotationAgreementReportV1:
    """Compute paired raw agreement without resolving any target."""

    (
        first,
        first_annotator,
        first_round,
        first_campaign,
        first_slot,
        first_principal,
        first_guideline,
        first_mode,
    ) = _index(first_records, side="first", allow_test_fixture=allow_test_fixture)
    (
        second,
        second_annotator,
        second_round,
        second_campaign,
        second_slot,
        second_principal,
        second_guideline,
        second_mode,
    ) = _index(second_records, side="second", allow_test_fixture=allow_test_fixture)
    if set(first) != set(second):
        missing_from_first = len(set(second) - set(first))
        missing_from_second = len(set(first) - set(second))
        raise AnnotationAgreementError(
            "annotation target sets differ: "
            f"missing_from_first={missing_from_first} "
            f"missing_from_second={missing_from_second}"
        )
    if first_annotator == second_annotator:
        raise AnnotationAgreementError("agreement requires two distinct annotators")
    if first_principal == second_principal:
        raise AnnotationAgreementError("agreement requires two distinct human principals")
    if first_campaign != second_campaign:
        raise AnnotationAgreementError("agreement inputs come from different campaigns")
    if first_round != second_round:
        raise AnnotationAgreementError("agreement inputs come from different rounds")
    if first_guideline != second_guideline:
        raise AnnotationAgreementError("agreement inputs use different guidelines")
    if first_mode != second_mode:
        raise AnnotationAgreementError("agreement inputs use different assignment modes")
    if {first_slot, second_slot} != _ANNOTATOR_SLOTS:
        raise AnnotationAgreementError("agreement requires the two independent annotator slots")
    keys = sorted(first)
    first_same = [first[key].same_claim.value for key in keys]
    second_same = [second[key].same_claim.value for key in keys]
    first_relation = [_relation_value(first[key]) for key in keys]
    second_relation = [_relation_value(second[key]) for key in keys]
    same_count = sum(left == right for left, right in zip(first_same, second_same, strict=True))
    relation_count = sum(
        left == right for left, right in zip(first_relation, second_relation, strict=True)
    )
    payload = {
        "schema_version": 1,
        "report_kind": "lf023_raw_independent_annotation_agreement_v1",
        "target_count": len(keys),
        "first_annotator_id": first_annotator,
        "second_annotator_id": second_annotator,
        "first_round_id": first_round,
        "second_round_id": second_round,
        "campaign_id": first_campaign,
        "guideline_sha256": first_guideline,
        "first_annotator_slot": first_slot,
        "second_annotator_slot": second_slot,
        "first_annotation_set_sha256": hash_canonical(
            {
                "schema": "lf023_raw_annotation_set_v1",
                "records": tuple(first[key].model_dump(mode="json") for key in sorted(first)),
            }
        ),
        "second_annotation_set_sha256": hash_canonical(
            {
                "schema": "lf023_raw_annotation_set_v1",
                "records": tuple(second[key].model_dump(mode="json") for key in sorted(second)),
            }
        ),
        "assignment_mode": first_mode,
        "origin_assurance": (
            "operator_attested" if first_mode == "operator_attested_human" else "test_fixture"
        ),
        "operator_attestation_verified": True,
        "backend_origin_verified": False,
        "human_gold_eligible": False,
        "same_claim_raw_agreement": same_count / len(keys),
        "same_claim_agreement_count": same_count,
        "same_claim_kappa": _kappa(
            first_same,
            second_same,
            categories=_SAME_CLAIM_CATEGORIES,
        ).model_dump(mode="json"),
        "same_claim_categories": tuple(
            item.model_dump(mode="json")
            for item in _category_reports(
                first_same,
                second_same,
                categories=_SAME_CLAIM_CATEGORIES,
            )
        ),
        "relation_raw_agreement": relation_count / len(keys),
        "relation_agreement_count": relation_count,
        "relation_kappa": _kappa(
            first_relation,
            second_relation,
            categories=_RELATION_CATEGORIES,
        ).model_dump(mode="json"),
        "relation_categories": tuple(
            item.model_dump(mode="json")
            for item in _category_reports(
                first_relation,
                second_relation,
                categories=_RELATION_CATEGORIES,
            )
        ),
        "uses_raw_pre_adjudication_records_only": True,
        "adjudicated_labels_substituted": False,
        "gate_closed_by_report": False,
    }
    report_id = "lf023_annotation_agreement_v1:" + hash_canonical(
        {"schema": "lf023_annotation_agreement_v1", **payload}
    )
    return AnnotationAgreementReportV1.model_validate({"report_id": report_id, **payload})


__all__ = [
    "AnnotationAgreementError",
    "AnnotationAgreementReportV1",
    "CategoryAgreementV1",
    "KappaResultV1",
    "compute_annotation_agreement",
]
