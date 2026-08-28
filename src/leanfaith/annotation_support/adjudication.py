"""Deterministic routing into human adjudication.

This module identifies the frozen LF-023 adjudication triggers and emits a
private administrative queue.  Queue construction never chooses a semantic
outcome and never treats agreement as an adjudication.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.schemas.annotation import AnnotationRecord
from leanfaith.schemas.enums import AnnotationAnswer, ReferenceIssue

_QUEUE_ITEM_ID = r"^lf023_adjudication_queue_item_v1:[0-9a-f]{64}$"
_QUEUE_ID = r"^lf023_adjudication_queue_v1:[0-9a-f]{64}$"
_HEX64 = r"^[0-9a-f]{64}$"
_ANNOTATOR_SLOTS = frozenset({"independent_annotator_1", "independent_annotator_2"})


class AdjudicationRoutingError(ValueError):
    """Raised when raw independent labels cannot be paired safely."""


class AdjudicationTrigger(StrEnum):
    """Version-1 routing triggers frozen by ``annotation/codebook_v1.yaml``."""

    SAME_CLAIM_DISAGREEMENT = "same_claim_disagreement"
    TERMINAL_RELATION_DISAGREEMENT = "terminal_relation_disagreement"
    EITHER_CANNOT_ASSESS_YET = "either_response_cannot_assess_yet"
    EITHER_REFERENCE_ISSUE_DEFINITE = "either_reference_issue_definite"
    EITHER_CONFIDENCE_AT_MOST_2 = "either_confidence_at_most_2"
    VERSIONED_POLICY_TRIGGER = "versioned_policy_trigger"


class AdjudicationQueueItemV1(StrictModel):
    """One target requiring a future, genuine human adjudication."""

    schema_version: Literal[1] = 1
    queue_item_id: str = Field(pattern=_QUEUE_ITEM_ID)
    target_kind: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    raw_annotation_ids: tuple[str, str]
    triggers: tuple[AdjudicationTrigger, ...] = Field(min_length=1)
    raw_responses_locked: Literal[True] = True
    requires_human_adjudication: Literal[True] = True
    semantic_resolution: Literal[None] = None
    auto_resolved: Literal[False] = False

    @model_validator(mode="after")
    def _coherent_and_content_addressed(self) -> Self:
        if self.raw_annotation_ids[0] == self.raw_annotation_ids[1]:
            raise ValueError("adjudication queue requires two distinct raw annotations")
        if tuple(sorted(set(self.triggers), key=str)) != self.triggers:
            raise ValueError("adjudication triggers must be sorted and unique")
        payload = self.model_dump(mode="json")
        expected = "lf023_adjudication_queue_item_v1:" + hash_canonical(
            {
                "schema": "lf023_adjudication_queue_item_v1",
                **{key: item for key, item in payload.items() if key != "queue_item_id"},
            }
        )
        if self.queue_item_id != expected:
            raise ValueError("adjudication queue item ID differs from content")
        return self


class AdjudicationQueueV1(StrictModel):
    """Content-addressed routing result with no semantic labels."""

    schema_version: Literal[1] = 1
    queue_id: str = Field(pattern=_QUEUE_ID)
    queue_kind: Literal["lf023_human_adjudication_queue_v1"]
    input_target_count: int = Field(ge=1)
    routed_target_count: int = Field(ge=0)
    campaign_id: str = Field(min_length=1)
    first_round_id: str = Field(min_length=1)
    second_round_id: str = Field(min_length=1)
    guideline_sha256: str = Field(pattern=_HEX64)
    first_annotator_slot: str = Field(min_length=1)
    second_annotator_slot: str = Field(min_length=1)
    first_annotation_set_sha256: str = Field(pattern=_HEX64)
    second_annotation_set_sha256: str = Field(pattern=_HEX64)
    policy_trigger_set_sha256: str = Field(pattern=_HEX64)
    routing_policy_id: Literal["annotation_codebook_v1#adjudication_triggers"]
    assignment_mode: Literal["operator_attested_human", "test_fixture"]
    origin_assurance: Literal["operator_attested", "test_fixture"]
    operator_attestation_verified: Literal[True] = True
    backend_origin_verified: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    items: tuple[AdjudicationQueueItemV1, ...]
    semantic_labels_created: Literal[False] = False
    adjudications_created: Literal[False] = False
    automatic_resolutions_created: Literal[False] = False
    human_action_required: bool

    @model_validator(mode="after")
    def _coherent_and_content_addressed(self) -> Self:
        if self.routed_target_count != len(self.items):
            raise ValueError("routed target count differs from queue length")
        if self.human_action_required != bool(self.items):
            raise ValueError("human_action_required must reflect queue nonemptiness")
        if self.first_round_id != self.second_round_id:
            raise ValueError("adjudication inputs must come from the same round")
        expected_assurance = (
            "operator_attested"
            if self.assignment_mode == "operator_attested_human"
            else "test_fixture"
        )
        if self.origin_assurance != expected_assurance:
            raise ValueError("adjudication origin assurance differs from assignment mode")
        if {self.first_annotator_slot, self.second_annotator_slot} != _ANNOTATOR_SLOTS:
            raise ValueError("adjudication requires the two independent annotator slots")
        if tuple(sorted(self.items, key=lambda item: item.target_id)) != self.items:
            raise ValueError("adjudication queue items must be target-ID sorted")
        target_keys = {(item.target_kind, item.target_id) for item in self.items}
        if len(target_keys) != len(self.items):
            raise ValueError("adjudication queue contains duplicate targets")
        payload = self.model_dump(mode="json")
        expected = "lf023_adjudication_queue_v1:" + hash_canonical(
            {
                "schema": "lf023_adjudication_queue_v1",
                **{key: item for key, item in payload.items() if key != "queue_id"},
            }
        )
        if self.queue_id != expected:
            raise ValueError("adjudication queue ID differs from content")
        return self


def _index(
    records: Sequence[AnnotationRecord],
    *,
    side: str,
    allow_test_fixture: bool,
) -> tuple[dict[tuple[str, str], AnnotationRecord], str, str, str, str, str, str, str]:
    if not records:
        raise AdjudicationRoutingError(f"{side} annotation collection is empty")
    result: dict[tuple[str, str], AnnotationRecord] = {}
    annotators: set[str] = set()
    rounds: set[str] = set()
    campaigns: set[str] = set()
    slots: set[str] = set()
    principals: set[str] = set()
    guidelines: set[str] = set()
    assignment_modes: set[str] = set()
    for record in records:
        key = (record.target_kind.value, record.target_id)
        if key in result:
            raise AdjudicationRoutingError(f"{side} has duplicate target {key!r}")
        result[key] = record
        annotators.add(record.annotator_id)
        rounds.add(record.round_id)
        campaign = record.metadata.get("campaign_id")
        slot = record.metadata.get("annotator_slot")
        principal = record.metadata.get("annotator_principal_hash")
        guideline = record.metadata.get("guideline_sha256")
        assignment_mode = record.metadata.get("assignment_mode")
        if not isinstance(campaign, str) or not campaign:
            raise AdjudicationRoutingError(f"{side} lacks a bound campaign_id")
        if not isinstance(slot, str) or slot not in _ANNOTATOR_SLOTS:
            raise AdjudicationRoutingError(f"{side} lacks a registered annotator_slot")
        if (
            not isinstance(principal, str)
            or len(principal) != 64
            or any(character not in "0123456789abcdef" for character in principal)
        ):
            raise AdjudicationRoutingError(f"{side} lacks an authenticated annotator principal")
        if (
            not isinstance(guideline, str)
            or len(guideline) != 64
            or any(character not in "0123456789abcdef" for character in guideline)
        ):
            raise AdjudicationRoutingError(f"{side} lacks a bound guideline hash")
        if assignment_mode not in {"operator_attested_human", "test_fixture"}:
            raise AdjudicationRoutingError(f"{side} lacks an authenticated assignment mode")
        origin_assurance = record.metadata.get("origin_assurance")
        fixture_only = record.metadata.get("fixture_only")
        import_role = record.metadata.get("import_role")
        if (
            record.metadata.get("raw_vote_only") is not True
            or record.metadata.get("resolved_label_created") is not False
            or record.metadata.get("gold_label_created") is not False
            or record.metadata.get("training_eligible") is not False
        ):
            raise AdjudicationRoutingError(f"{side} is not a raw-only annotation")
        if assignment_mode == "operator_attested_human":
            if (
                origin_assurance != "operator_attested"
                or record.metadata.get("operator_attestation_verified") is not True
                or record.metadata.get("backend_origin_verified") is not False
                or record.metadata.get("human_gold_eligible") is not False
                or fixture_only is not False
                or import_role != "raw_operator_attested_annotation"
            ):
                raise AdjudicationRoutingError(f"{side} has inconsistent operator assertions")
        elif not allow_test_fixture:
            raise AdjudicationRoutingError(f"{side} contains test-fixture annotations")
        elif (
            origin_assurance != "test_fixture"
            or record.metadata.get("operator_attestation_verified") is not True
            or record.metadata.get("backend_origin_verified") is not False
            or record.metadata.get("human_gold_eligible") is not False
            or fixture_only is not True
            or import_role != "raw_annotation_test_fixture"
        ):
            raise AdjudicationRoutingError(f"{side} has inconsistent fixture metadata")
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
        raise AdjudicationRoutingError(
            f"{side} must contain one annotator, principal, round, campaign, slot, "
            "guideline, and assignment mode"
        )
    return (
        result,
        next(iter(annotators)),
        next(iter(rounds)),
        next(iter(campaigns)),
        next(iter(slots)),
        next(iter(principals)),
        next(iter(guidelines)),
        next(iter(assignment_modes)),
    )


def adjudication_triggers(
    first: AnnotationRecord,
    second: AnnotationRecord,
    *,
    versioned_policy_trigger: bool = False,
) -> tuple[AdjudicationTrigger, ...]:
    """Return only routing reasons; never return or infer an outcome."""

    if (first.target_kind, first.target_id) != (second.target_kind, second.target_id):
        raise AdjudicationRoutingError("cannot compare annotations for different targets")
    if first.annotator_id == second.annotator_id:
        raise AdjudicationRoutingError("adjudication routing requires independent annotators")
    triggers: set[AdjudicationTrigger] = set()
    if first.same_claim is not second.same_claim:
        triggers.add(AdjudicationTrigger.SAME_CLAIM_DISAGREEMENT)
    if first.relation is not second.relation:
        triggers.add(AdjudicationTrigger.TERMINAL_RELATION_DISAGREEMENT)
    if AnnotationAnswer.CANNOT_ASSESS_YET in {first.same_claim, second.same_claim}:
        triggers.add(AdjudicationTrigger.EITHER_CANNOT_ASSESS_YET)
    if ReferenceIssue.DEFINITE in {first.reference_issue, second.reference_issue}:
        triggers.add(AdjudicationTrigger.EITHER_REFERENCE_ISSUE_DEFINITE)
    if min(first.confidence, second.confidence) <= 2:
        triggers.add(AdjudicationTrigger.EITHER_CONFIDENCE_AT_MOST_2)
    if versioned_policy_trigger:
        triggers.add(AdjudicationTrigger.VERSIONED_POLICY_TRIGGER)
    return tuple(sorted(triggers, key=str))


def _queue_item(
    first: AnnotationRecord,
    second: AnnotationRecord,
    triggers: tuple[AdjudicationTrigger, ...],
) -> AdjudicationQueueItemV1:
    raw_ids = tuple(sorted((first.annotation_id, second.annotation_id)))
    payload = {
        "schema_version": 1,
        "target_kind": first.target_kind.value,
        "target_id": first.target_id,
        "raw_annotation_ids": raw_ids,
        "triggers": tuple(item.value for item in triggers),
        "raw_responses_locked": True,
        "requires_human_adjudication": True,
        "semantic_resolution": None,
        "auto_resolved": False,
    }
    queue_item_id = "lf023_adjudication_queue_item_v1:" + hash_canonical(
        {"schema": "lf023_adjudication_queue_item_v1", **payload}
    )
    return AdjudicationQueueItemV1.model_validate({"queue_item_id": queue_item_id, **payload})


def build_adjudication_queue(
    first_records: Sequence[AnnotationRecord],
    second_records: Sequence[AnnotationRecord],
    *,
    policy_trigger_targets: Iterable[tuple[str, str]] = (),
    allow_test_fixture: bool = False,
) -> AdjudicationQueueV1:
    """Route triggered targets while leaving every semantic outcome unset."""

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
    if first_annotator == second_annotator:
        raise AdjudicationRoutingError("adjudication routing requires distinct annotators")
    if first_principal == second_principal:
        raise AdjudicationRoutingError("adjudication requires distinct human principals")
    if first_campaign != second_campaign:
        raise AdjudicationRoutingError("adjudication inputs come from different campaigns")
    if first_round != second_round:
        raise AdjudicationRoutingError("adjudication inputs come from different rounds")
    if first_guideline != second_guideline:
        raise AdjudicationRoutingError("adjudication inputs use different guidelines")
    if first_mode != second_mode:
        raise AdjudicationRoutingError("adjudication inputs use different assignment modes")
    if {first_slot, second_slot} != _ANNOTATOR_SLOTS:
        raise AdjudicationRoutingError("adjudication requires the two independent annotator slots")
    if set(first) != set(second):
        raise AdjudicationRoutingError("annotation target sets differ")
    policy_targets = set(policy_trigger_targets)
    unknown_policy_targets = policy_targets - set(first)
    if unknown_policy_targets:
        raise AdjudicationRoutingError("versioned policy trigger targets an unknown item")
    items: list[AdjudicationQueueItemV1] = []
    for key in sorted(first):
        triggers = adjudication_triggers(
            first[key],
            second[key],
            versioned_policy_trigger=key in policy_targets,
        )
        if triggers:
            items.append(_queue_item(first[key], second[key], triggers))
    ordered = tuple(sorted(items, key=lambda item: item.target_id))
    payload = {
        "schema_version": 1,
        "queue_kind": "lf023_human_adjudication_queue_v1",
        "input_target_count": len(first),
        "routed_target_count": len(ordered),
        "campaign_id": first_campaign,
        "first_round_id": first_round,
        "second_round_id": second_round,
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
        "policy_trigger_set_sha256": hash_canonical(
            {
                "schema": "lf023_adjudication_policy_trigger_set_v1",
                "targets": tuple(sorted(policy_targets)),
            }
        ),
        "routing_policy_id": "annotation_codebook_v1#adjudication_triggers",
        "assignment_mode": first_mode,
        "origin_assurance": (
            "operator_attested" if first_mode == "operator_attested_human" else "test_fixture"
        ),
        "operator_attestation_verified": True,
        "backend_origin_verified": False,
        "human_gold_eligible": False,
        "items": tuple(item.model_dump(mode="json") for item in ordered),
        "semantic_labels_created": False,
        "adjudications_created": False,
        "automatic_resolutions_created": False,
        "human_action_required": bool(ordered),
    }
    queue_id = "lf023_adjudication_queue_v1:" + hash_canonical(
        {"schema": "lf023_adjudication_queue_v1", **payload}
    )
    return AdjudicationQueueV1.model_validate({"queue_id": queue_id, **payload})


__all__ = [
    "AdjudicationQueueItemV1",
    "AdjudicationQueueV1",
    "AdjudicationRoutingError",
    "AdjudicationTrigger",
    "adjudication_triggers",
    "build_adjudication_queue",
]
