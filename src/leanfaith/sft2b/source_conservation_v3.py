"""Per-source conservation accounting between immutable SFT2B source releases."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.sft2b.schemas import NonEmpty, Sha256, SourceRecord, StableId

SourceView = Literal["absent", "core", "quarantine", "tail"]
DeltaDirection = Literal["added", "moved", "quarantined", "readmitted", "removed"]
ConservationAction = Literal[
    "added",
    "moved_core_to_tail",
    "moved_tail_to_core",
    "quarantined_from_core",
    "quarantined_from_tail",
    "readmitted_to_core",
    "readmitted_to_tail",
    "removed",
    "retained_core",
    "retained_quarantine",
    "retained_tail",
]
DeltaReasonCode = Literal[
    "core_boundary_reselection",
    "dedup_displacement_addition",
    "dedup_displacement_movement",
    "human_review_quarantine",
    "human_review_readmission",
    "meta_instruction_quarantine",
    "newly_eligible_source",
    "source_contract_correction",
]


class ExplicitDeltaReasonV3(StrictModel):
    """Builder evidence required for every source added to or removed from v3."""

    schema_version: Literal["sft2b_explicit_delta_reason_v3"] = "sft2b_explicit_delta_reason_v3"
    source_id: StableId
    direction: DeltaDirection
    reason_code: DeltaReasonCode
    rationale: NonEmpty
    evidence_sha256: Sha256
    related_source_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_reason(self) -> ExplicitDeltaReasonV3:
        if tuple(sorted(set(self.related_source_ids))) != self.related_source_ids:
            raise ValueError("related source IDs must be unique and sorted")
        allowed: dict[DeltaDirection, set[DeltaReasonCode]] = {
            "added": {
                "dedup_displacement_addition",
                "newly_eligible_source",
                "source_contract_correction",
            },
            "moved": {
                "core_boundary_reselection",
                "dedup_displacement_movement",
            },
            "quarantined": {
                "human_review_quarantine",
                "meta_instruction_quarantine",
                "source_contract_correction",
            },
            "readmitted": {"human_review_readmission"},
            "removed": {"source_contract_correction"},
        }
        if self.reason_code not in allowed[self.direction]:
            raise ValueError("delta reason code is incompatible with its direction")
        if (
            self.reason_code
            in {
                "dedup_displacement_addition",
                "dedup_displacement_movement",
            }
            and not self.related_source_ids
        ):
            raise ValueError("dedup displacement must name the displaced source")
        return self


class SourceConservationEventV3(StrictModel):
    """One exhaustive v2-to-v3 disposition for one stable source identity."""

    schema_version: Literal["sft2b_source_conservation_event_v3"] = (
        "sft2b_source_conservation_event_v3"
    )
    source_id: StableId
    action: ConservationAction
    v2_view: SourceView
    v3_view: SourceView
    v2_record_sha256: Sha256 | None
    v3_record_sha256: Sha256 | None
    delta_reason: ExplicitDeltaReasonV3 | None

    @model_validator(mode="after")
    def validate_transition(self) -> SourceConservationEventV3:
        expected: ConservationAction
        if self.v2_view == "absent":
            if self.v3_view == "absent":
                raise ValueError("conservation event cannot be absent from both releases")
            expected = "added"
        elif self.v3_view == "absent":
            expected = "removed"
        elif self.v2_view == "core" and self.v3_view == "tail":
            expected = "moved_core_to_tail"
        elif self.v2_view == "tail" and self.v3_view == "core":
            expected = "moved_tail_to_core"
        elif self.v2_view == "core" and self.v3_view == "quarantine":
            expected = "quarantined_from_core"
        elif self.v2_view == "tail" and self.v3_view == "quarantine":
            expected = "quarantined_from_tail"
        elif self.v2_view == "quarantine" and self.v3_view == "core":
            expected = "readmitted_to_core"
        elif self.v2_view == "quarantine" and self.v3_view == "tail":
            expected = "readmitted_to_tail"
        elif self.v2_view == "core" and self.v3_view == "core":
            expected = "retained_core"
        elif self.v2_view == "tail" and self.v3_view == "tail":
            expected = "retained_tail"
        elif self.v2_view == "quarantine" and self.v3_view == "quarantine":
            expected = "retained_quarantine"
        else:  # pragma: no cover - the SourceView union is exhaustive
            raise ValueError("unsupported conservation transition")
        if self.action != expected:
            raise ValueError("conservation action does not match its views")
        if (self.v2_view == "absent") != (self.v2_record_sha256 is None):
            raise ValueError("v2 view/record hash mismatch")
        if (self.v3_view == "absent") != (self.v3_record_sha256 is None):
            raise ValueError("v3 view/record hash mismatch")
        reason_direction = {
            "added": "added",
            "moved_core_to_tail": "moved",
            "moved_tail_to_core": "moved",
            "quarantined_from_core": "quarantined",
            "quarantined_from_tail": "quarantined",
            "readmitted_to_core": "readmitted",
            "readmitted_to_tail": "readmitted",
            "removed": "removed",
        }.get(self.action)
        if reason_direction is not None:
            if self.delta_reason is None or self.delta_reason.direction != reason_direction:
                raise ValueError("release disposition requires a matching explicit reason")
            if self.delta_reason.source_id != self.source_id:
                raise ValueError("conservation event/reason source IDs differ")
        elif self.delta_reason is not None:
            raise ValueError("retained source cannot carry a delta reason")
        if (
            self.v2_record_sha256 is not None
            and self.v3_record_sha256 is not None
            and self.v2_record_sha256 != self.v3_record_sha256
        ):
            raise ValueError("stable source record mutated between releases")
        return self


class SourceConservationReceiptV3(StrictModel):
    """Hash-bound summary over a separately serialized exhaustive event stream."""

    schema_version: Literal["sft2b_source_conservation_receipt_v3"] = (
        "sft2b_source_conservation_receipt_v3"
    )
    v2_sources_sha256: Sha256
    v2_core_view_sha256: Sha256
    v2_quarantine_view_sha256: Sha256
    v2_tail_view_sha256: Sha256
    v3_sources_sha256: Sha256
    v3_core_view_sha256: Sha256
    v3_quarantine_view_sha256: Sha256
    v3_tail_view_sha256: Sha256
    event_stream_sha256: Sha256
    event_count: Annotated[int, Field(ge=1)]
    v2_source_count: Annotated[int, Field(ge=0)]
    v3_source_count: Annotated[int, Field(ge=0)]
    action_counts: dict[ConservationAction, Annotated[int, Field(ge=0)]]
    reason_counts: dict[DeltaReasonCode, Annotated[int, Field(ge=0)]]
    v2_partition_complete: Literal[True]
    v3_partition_complete: Literal[True]
    every_delta_explained: Literal[True]

    @model_validator(mode="after")
    def validate_summary_algebra(self) -> SourceConservationReceiptV3:
        if sum(self.action_counts.values()) != self.event_count:
            raise ValueError("conservation action counts do not cover the event stream")
        if self.v2_source_count != self.event_count - self.action_counts.get("added", 0):
            raise ValueError("v2 source count does not replay from conservation actions")
        if self.v3_source_count != self.event_count - self.action_counts.get("removed", 0):
            raise ValueError("v3 source count does not replay from conservation actions")
        reasoned_actions: tuple[ConservationAction, ...] = (
            "added",
            "moved_core_to_tail",
            "moved_tail_to_core",
            "quarantined_from_core",
            "quarantined_from_tail",
            "readmitted_to_core",
            "readmitted_to_tail",
            "removed",
        )
        expected_reasons = sum(self.action_counts.get(action, 0) for action in reasoned_actions)
        if sum(self.reason_counts.values()) != expected_reasons:
            raise ValueError("conservation reason counts do not cover every release delta")
        return self


def _record_sha256(source: SourceRecord) -> Sha256:
    return hash_canonical(source.model_dump(mode="json"))


def _view_map(
    rows: Mapping[str, SourceRecord],
    *,
    core_ids: Sequence[str],
    quarantine_ids: Sequence[str],
    tail_ids: Sequence[str],
    release: str,
) -> dict[str, SourceView]:
    core = set(core_ids)
    quarantine = set(quarantine_ids)
    tail = set(tail_ids)
    if (
        len(core) != len(core_ids)
        or len(quarantine) != len(quarantine_ids)
        or len(tail) != len(tail_ids)
    ):
        raise ValueError(f"{release} source view contains duplicate IDs")
    if core & tail or core & quarantine or tail & quarantine:
        raise ValueError(f"{release} source views overlap")
    if core | tail | quarantine != set(rows):
        raise ValueError(f"{release} source views do not partition sources")
    return {
        source_id: ("core" if source_id in core else "tail" if source_id in tail else "quarantine")
        for source_id in rows
    }


def build_conservation_events(
    *,
    v2_rows: Mapping[str, SourceRecord],
    v2_core_ids: Sequence[str],
    v2_quarantine_ids: Sequence[str],
    v2_tail_ids: Sequence[str],
    v3_rows: Mapping[str, SourceRecord],
    v3_core_ids: Sequence[str],
    v3_quarantine_ids: Sequence[str],
    v3_tail_ids: Sequence[str],
    delta_reasons: Sequence[ExplicitDeltaReasonV3],
) -> tuple[SourceConservationEventV3, ...]:
    """Build an exhaustive transition stream and reject unexplained source deltas."""

    if set(v2_rows) != {row.source_id for row in v2_rows.values()}:
        raise ValueError("v2 source mapping keys drifted from row IDs")
    if set(v3_rows) != {row.source_id for row in v3_rows.values()}:
        raise ValueError("v3 source mapping keys drifted from row IDs")
    v2_views = _view_map(
        v2_rows,
        core_ids=v2_core_ids,
        quarantine_ids=v2_quarantine_ids,
        tail_ids=v2_tail_ids,
        release="v2",
    )
    v3_views = _view_map(
        v3_rows,
        core_ids=v3_core_ids,
        quarantine_ids=v3_quarantine_ids,
        tail_ids=v3_tail_ids,
        release="v3",
    )
    reasons = {reason.source_id: reason for reason in delta_reasons}
    if len(reasons) != len(delta_reasons):
        raise ValueError("duplicate explicit delta reason")
    events: list[SourceConservationEventV3] = []
    consumed_reasons: set[str] = set()
    for source_id in sorted(set(v2_rows) | set(v3_rows)):
        v2_view = v2_views.get(source_id, "absent")
        v3_view = v3_views.get(source_id, "absent")
        if v2_view == "absent":
            action: ConservationAction = "added"
        elif v3_view == "absent":
            action = "removed"
        elif v2_view == "core" and v3_view == "tail":
            action = "moved_core_to_tail"
        elif v2_view == "tail" and v3_view == "core":
            action = "moved_tail_to_core"
        elif v2_view == "core" and v3_view == "quarantine":
            action = "quarantined_from_core"
        elif v2_view == "tail" and v3_view == "quarantine":
            action = "quarantined_from_tail"
        elif v2_view == "quarantine" and v3_view == "core":
            action = "readmitted_to_core"
        elif v2_view == "quarantine" and v3_view == "tail":
            action = "readmitted_to_tail"
        elif v2_view == "core":
            action = "retained_core"
        elif v2_view == "quarantine":
            action = "retained_quarantine"
        else:
            action = "retained_tail"
        reason = (
            reasons.get(source_id)
            if action
            in {
                "added",
                "moved_core_to_tail",
                "moved_tail_to_core",
                "quarantined_from_core",
                "quarantined_from_tail",
                "readmitted_to_core",
                "readmitted_to_tail",
                "removed",
            }
            else None
        )
        if reason is not None:
            consumed_reasons.add(source_id)
        events.append(
            SourceConservationEventV3(
                source_id=source_id,
                action=action,
                v2_view=v2_view,
                v3_view=v3_view,
                v2_record_sha256=(
                    _record_sha256(v2_rows[source_id]) if source_id in v2_rows else None
                ),
                v3_record_sha256=(
                    _record_sha256(v3_rows[source_id]) if source_id in v3_rows else None
                ),
                delta_reason=reason,
            )
        )
    unused = set(reasons) - consumed_reasons
    if unused:
        raise ValueError(f"delta reasons do not describe source deltas: {sorted(unused)}")
    return tuple(events)


def summarize_conservation(
    events: Sequence[SourceConservationEventV3],
) -> tuple[dict[ConservationAction, int], dict[DeltaReasonCode, int]]:
    actions = Counter(event.action for event in events)
    reasons = Counter(
        event.delta_reason.reason_code for event in events if event.delta_reason is not None
    )
    return dict(sorted(actions.items())), dict(sorted(reasons.items()))
