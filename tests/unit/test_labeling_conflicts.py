from __future__ import annotations

import datetime

import pytest
from pydantic import ValidationError

from leanfaith.labeling.conflicts import (
    ResolutionConflictReason,
    ResolutionConflictRecord,
    ResolutionOverrideReason,
    ResolutionOverrideRecord,
    build_resolution_conflict_record,
    build_resolution_override_record,
)
from leanfaith.schemas.enums import SemanticLabelTargetKind
from leanfaith.schemas.ids import make_id

UTC_A = datetime.datetime(2026, 8, 11, 9, 0, tzinfo=datetime.UTC)
UTC_B = datetime.datetime(2026, 8, 12, 10, 30, tzinfo=datetime.UTC)
POLICY_HASH = "a" * 64

PAIR_ID = make_id("pair", {"fixture": "conflict-target"})
NL_LEAN_ID = make_id("nllean", {"fixture": "nl-target"})
CANDIDATE_A = make_id("resolution_candidate", {"fixture": "candidate-a"})
CANDIDATE_B = make_id("resolution_candidate", {"fixture": "candidate-b"})
CANDIDATE_C = make_id("resolution_candidate", {"fixture": "candidate-c"})
EVIDENCE_A = make_id("ev", {"fixture": "evidence-a"})
EVIDENCE_B = make_id("ev", {"fixture": "evidence-b"})
PRIOR_LABEL_ID = make_id("lbl", {"fixture": "prior-label"})


def _conflict(**overrides: object) -> ResolutionConflictRecord:
    values: dict[str, object] = {
        "target_kind": SemanticLabelTargetKind.LEAN_PAIR,
        "target_id": PAIR_ID,
        "candidate_ids": (CANDIDATE_B, CANDIDATE_A),
        "evidence_ids": (EVIDENCE_B, EVIDENCE_A),
        "source_ranks": (3, 1),
        "reason_codes": (
            ResolutionConflictReason.RELATION_DISAGREEMENT,
            ResolutionConflictReason.SAME_CLAIM_DISAGREEMENT,
        ),
        "policy_version": "label_resolution_v1",
        "policy_hash": POLICY_HASH,
        "detected_at": UTC_A,
        "prior_label_id": PRIOR_LABEL_ID,
    }
    values.update(overrides)
    return build_resolution_conflict_record(**values)  # type: ignore[arg-type]


def _override(**overrides: object) -> ResolutionOverrideRecord:
    values: dict[str, object] = {
        "target_kind": SemanticLabelTargetKind.LEAN_PAIR,
        "target_id": PAIR_ID,
        "winner_candidate_id": CANDIDATE_A,
        "overridden_candidate_ids": (CANDIDATE_C, CANDIDATE_B),
        "evidence_ids": (EVIDENCE_B, EVIDENCE_A),
        "source_ranks": (5, 4, 1),
        "reason_codes": (ResolutionOverrideReason.STRONG_OVER_WEAK,),
        "policy_version": "label_resolution_v1",
        "policy_hash": POLICY_HASH,
        "logged_at": UTC_A,
        "prior_label_id": PRIOR_LABEL_ID,
    }
    values.update(overrides)
    return build_resolution_override_record(**values)  # type: ignore[arg-type]


def test_conflict_happy_path_is_canonical_and_content_addressed() -> None:
    conflict = _conflict()

    assert conflict.target_id == PAIR_ID
    assert conflict.candidate_ids == tuple(sorted((CANDIDATE_A, CANDIDATE_B)))
    assert conflict.evidence_ids == tuple(sorted((EVIDENCE_A, EVIDENCE_B)))
    assert conflict.source_ranks == (1, 3)
    assert conflict.reason_codes == tuple(
        sorted(
            (
                ResolutionConflictReason.RELATION_DISAGREEMENT,
                ResolutionConflictReason.SAME_CLAIM_DISAGREEMENT,
            ),
            key=str,
        )
    )
    assert conflict.conflict_id.startswith("resolution_conflict:")


def test_override_happy_path_is_canonical_and_content_addressed() -> None:
    override = _override()

    assert override.winner_candidate_id == CANDIDATE_A
    assert override.overridden_candidate_ids == tuple(sorted((CANDIDATE_B, CANDIDATE_C)))
    assert override.evidence_ids == tuple(sorted((EVIDENCE_A, EVIDENCE_B)))
    assert override.source_ranks == (1, 4, 5)
    assert override.override_id.startswith("resolution_override:")


def test_conflict_id_ignores_timestamp_and_input_permutation() -> None:
    first = _conflict()
    second = _conflict(
        candidate_ids=tuple(reversed((CANDIDATE_B, CANDIDATE_A))),
        evidence_ids=tuple(reversed((EVIDENCE_B, EVIDENCE_A))),
        source_ranks=(1, 3),
        reason_codes=tuple(
            reversed(
                (
                    ResolutionConflictReason.RELATION_DISAGREEMENT,
                    ResolutionConflictReason.SAME_CLAIM_DISAGREEMENT,
                )
            )
        ),
        detected_at=UTC_B,
    )

    assert second.detected_at != first.detected_at
    assert second.conflict_id == first.conflict_id
    assert second.model_dump(exclude={"detected_at"}) == first.model_dump(exclude={"detected_at"})


def test_override_id_ignores_timestamp_and_input_permutation() -> None:
    first = _override()
    second = _override(
        overridden_candidate_ids=tuple(reversed((CANDIDATE_C, CANDIDATE_B))),
        evidence_ids=tuple(reversed((EVIDENCE_B, EVIDENCE_A))),
        source_ranks=(1, 5, 4),
        logged_at=UTC_B,
    )

    assert second.logged_at != first.logged_at
    assert second.override_id == first.override_id
    assert second.model_dump(exclude={"logged_at"}) == first.model_dump(exclude={"logged_at"})


@pytest.mark.parametrize(
    ("builder", "kwargs"),
    [
        (
            _conflict,
            {
                "target_kind": SemanticLabelTargetKind.NL_LEAN,
                "target_id": PAIR_ID,
            },
        ),
        (
            _override,
            {
                "target_kind": SemanticLabelTargetKind.LEAN_PAIR,
                "target_id": NL_LEAN_ID,
            },
        ),
    ],
)
def test_target_prefix_must_match_target_kind(
    builder: object,
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="does not match target_kind"):
        builder(**kwargs)  # type: ignore[operator]


def test_direct_conflict_record_rejects_unsorted_and_duplicate_identities() -> None:
    conflict = _conflict()
    payload = conflict.model_dump(mode="python")

    with pytest.raises(ValidationError, match="candidate_ids must be sorted and unique"):
        ResolutionConflictRecord.model_validate(
            {**payload, "candidate_ids": tuple(reversed(conflict.candidate_ids))}
        )
    with pytest.raises(ValidationError, match="evidence_ids must be sorted and unique"):
        ResolutionConflictRecord.model_validate(
            {**payload, "evidence_ids": (EVIDENCE_A, EVIDENCE_A)}
        )
    with pytest.raises(ValidationError, match="source_ranks must be sorted and unique"):
        ResolutionConflictRecord.model_validate({**payload, "source_ranks": (3, 1)})


def test_direct_override_record_rejects_unsorted_and_duplicate_identities() -> None:
    override = _override()
    payload = override.model_dump(mode="python")

    with pytest.raises(ValidationError, match="overridden_candidate_ids must be sorted and unique"):
        ResolutionOverrideRecord.model_validate(
            {
                **payload,
                "overridden_candidate_ids": tuple(reversed(override.overridden_candidate_ids)),
            }
        )
    with pytest.raises(ValidationError, match="evidence_ids must be sorted and unique"):
        ResolutionOverrideRecord.model_validate(
            {**payload, "evidence_ids": (EVIDENCE_A, EVIDENCE_A)}
        )


def test_factories_reject_duplicate_identities() -> None:
    with pytest.raises(ValueError, match="candidate_ids must not contain duplicates"):
        _conflict(candidate_ids=(CANDIDATE_A, CANDIDATE_A))
    with pytest.raises(ValueError, match="overridden_candidate_ids must not contain duplicates"):
        _override(overridden_candidate_ids=(CANDIDATE_B, CANDIDATE_B))


def test_conflict_requires_two_combined_candidate_or_evidence_identities() -> None:
    with pytest.raises(ValidationError, match="at least two combined"):
        _conflict(candidate_ids=(CANDIDATE_A,), evidence_ids=())


def test_override_requires_distinct_winner_and_overridden_candidate() -> None:
    with pytest.raises(ValidationError, match="winner_candidate_id must differ"):
        _override(overridden_candidate_ids=(CANDIDATE_A,))


@pytest.mark.parametrize(
    ("record_factory", "id_field", "corruption"),
    [
        (_conflict, "conflict_id", {"policy_version": "label_resolution_v2"}),
        (_override, "override_id", {"policy_hash": "b" * 64}),
    ],
)
def test_content_corruption_is_rejected(
    record_factory: object,
    id_field: str,
    corruption: dict[str, object],
) -> None:
    record = record_factory()  # type: ignore[operator]
    model_type = type(record)
    payload = record.model_dump(mode="python")
    original_id = payload[id_field]

    with pytest.raises(ValidationError, match="differs from semantic content"):
        model_type.model_validate({**payload, **corruption, id_field: original_id})


@pytest.mark.parametrize(
    ("record_factory", "timestamp_field"),
    [
        (_conflict, "detected_at"),
        (_override, "logged_at"),
    ],
)
def test_operational_timestamp_must_be_timezone_aware_utc(
    record_factory: object,
    timestamp_field: str,
) -> None:
    naive = datetime.datetime(2026, 8, 11, 9, 0)
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        record_factory(**{timestamp_field: naive})  # type: ignore[operator]
