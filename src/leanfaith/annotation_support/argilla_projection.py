"""Strict projection of captured Argilla votes into locked LF-023 responses.

This module is deliberately narrower than annotation import.  It binds a
pre-response Argilla record allocation to one authenticated assignment,
verifies backend-origin receipts and exact raw record bytes, and projects the
submitted values into :class:`LockedAnnotationResponseEnvelopeV1`.

Projection creates raw, immutable vote records only.  It does not verify the
assignment HMAC, establish human identity or independence, adjudicate, create
gold labels, or make any response training eligible.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from leanfaith.annotation_support.argilla_backend import (
    ArgillaBackendPinV1,
    ArgillaDirectFetchReceiptV1,
)
from leanfaith.annotation_support.attestation import HumanAnnotationAssignmentEnvelopeV1
from leanfaith.annotation_support.export import ArtifactBinding
from leanfaith.annotation_support.import_ import (
    IndependentAnnotationResponseV1,
    LockedAnnotationResponseContentV1,
    LockedAnnotationResponseEnvelopeV1,
    make_locked_response_id,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import AnnotationAnswer, ReferenceIssue, RelationLabel
from leanfaith.schemas.manifest import require_utc

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_BLIND_ITEM_ID = r"^lf023_blind_item_v1:[0-9a-f]{64}$"
_BUNDLE_MANIFEST_ID = r"^lf023_blinded_bundle_manifest_v1:[0-9a-f]{64}$"
_ASSIGNMENT_ID = r"^lf023_human_assignment_v1:[0-9a-f]{64}$"
_PIN_ID = r"^lf023_argilla_backend_pin_v1:[0-9a-f]{64}$"
_CAPTURE_MANIFEST_ID = r"^lf023_argilla_capture_manifest_v1:[0-9a-f]{64}$"
_BINDING_MANIFEST_ID = r"^lf023_argilla_projection_binding_v1:[0-9a-f]{64}$"
_PROJECTION_MANIFEST_ID = r"^lf023_argilla_projection_manifest_v1:[0-9a-f]{64}$"


class ArgillaProjectionError(ValueError):
    """Raised when an Argilla snapshot cannot be projected without inference."""


class ArgillaRecordItemBindingV1(StrictModel):
    """Pre-response binding of one blinded item to one Argilla record."""

    schema_version: Literal[1] = 1
    opaque_item_token: str = Field(pattern=_BLIND_ITEM_ID)
    backend_record_id: str = Field(pattern=_UUID)


class ArgillaProjectionBindingContentV1(StrictModel):
    """Record allocation intended to be fixed before response observation.

    This label-free artifact does not by itself prove its creation order.
    Separate operator integrity evidence must establish that fact.
    """

    schema_version: Literal[1] = 1
    manifest_kind: Literal["lf023_argilla_projection_binding_v1"]
    campaign_id: Literal["lf021_prevalence_v1"]
    round_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    annotator_slot: Literal["independent_annotator_1", "independent_annotator_2"]
    assignment_id: str = Field(pattern=_ASSIGNMENT_ID)
    backend_pin_id: str = Field(pattern=_PIN_ID)
    bundle_manifest_id: str = Field(pattern=_BUNDLE_MANIFEST_ID)
    public_bundle_manifest: ArtifactBinding
    guideline: ArtifactBinding
    value_mapping_version: Literal["lf023_argilla_values_v1"]
    item_count: Literal[240] = 240
    item_bindings: tuple[ArgillaRecordItemBindingV1, ...]
    record_allocation_only: Literal[True] = True
    response_values_included: Literal[False] = False
    creation_order_verified: Literal[False] = False
    requires_separate_operator_integrity_evidence: Literal[True] = True
    semantic_labels_included: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _complete_unique_mapping(self) -> Self:
        if len(self.item_bindings) != self.item_count:
            raise ValueError("Argilla projection binding must cover all 240 blinded items")
        tokens = [item.opaque_item_token for item in self.item_bindings]
        records = [item.backend_record_id for item in self.item_bindings]
        if len(set(tokens)) != self.item_count:
            raise ValueError("Argilla projection binding contains duplicate item tokens")
        if len(set(records)) != self.item_count:
            raise ValueError("Argilla projection binding contains duplicate backend records")
        if tokens != sorted(tokens):
            raise ValueError("Argilla projection item bindings must be sorted by item token")
        return self


def make_argilla_projection_binding_id(
    value: ArgillaProjectionBindingContentV1 | dict[str, Any],
) -> str:
    """Return the content address of one pre-response projection binding."""

    content = (
        value
        if isinstance(value, ArgillaProjectionBindingContentV1)
        else ArgillaProjectionBindingContentV1.model_validate(value)
    )
    return "lf023_argilla_projection_binding_v1:" + hash_canonical(
        {
            "schema": "lf023_argilla_projection_binding_v1",
            **content.model_dump(mode="json"),
        }
    )


class ArgillaProjectionBindingManifestV1(ArgillaProjectionBindingContentV1):
    """Content-addressed pre-response Argilla record allocation."""

    manifest_id: str = Field(pattern=_BINDING_MANIFEST_ID)

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        content = ArgillaProjectionBindingContentV1.model_validate(
            self.model_dump(mode="json", exclude={"manifest_id"})
        )
        if self.manifest_id != make_argilla_projection_binding_id(content):
            raise ValueError("Argilla projection binding ID differs from normalized content")
        return self


def make_argilla_projection_binding_manifest(
    *,
    assignment: HumanAnnotationAssignmentEnvelopeV1,
    pin: ArgillaBackendPinV1,
    item_bindings: tuple[ArgillaRecordItemBindingV1, ...],
    public_bundle_tokens: frozenset[str],
) -> ArgillaProjectionBindingManifestV1:
    """Create a label-free record allocation for later operator attestation."""

    if assignment.backend_id != "argilla":
        raise ArgillaProjectionError("Argilla projection requires an Argilla assignment")
    if assignment.assignment_mode != "operator_attested_human":
        raise ArgillaProjectionError("production Argilla projection rejects fixture assignments")
    content = ArgillaProjectionBindingContentV1(
        manifest_kind="lf023_argilla_projection_binding_v1",
        campaign_id=assignment.campaign_id,
        round_id=assignment.round_id,
        annotator_slot=assignment.annotator_slot,
        assignment_id=assignment.assignment_id,
        backend_pin_id=pin.pin_id,
        bundle_manifest_id=assignment.bundle_manifest_id,
        public_bundle_manifest=assignment.public_bundle_manifest,
        guideline=assignment.guideline,
        value_mapping_version="lf023_argilla_values_v1",
        item_bindings=item_bindings,
    )
    mapped_tokens = {item.opaque_item_token for item in content.item_bindings}
    if mapped_tokens != public_bundle_tokens:
        raise ArgillaProjectionError(
            "Argilla projection item mapping differs from the exact public bundle"
        )
    return ArgillaProjectionBindingManifestV1(
        manifest_id=make_argilla_projection_binding_id(content),
        **content.model_dump(mode="python"),
    )


class ArgillaProjectionManifestContentV1(StrictModel):
    """Non-gold lineage for one deterministic Argilla-to-response projection."""

    schema_version: Literal[1] = 1
    manifest_kind: Literal["lf023_argilla_projection_manifest_v1"]
    binding_manifest_id: str = Field(pattern=_BINDING_MANIFEST_ID)
    assignment_id: str = Field(pattern=_ASSIGNMENT_ID)
    backend_pin_id: str = Field(pattern=_PIN_ID)
    capture_manifest_id: str | None = Field(default=None, pattern=_CAPTURE_MANIFEST_ID)
    receipt_ids: tuple[str, ...]
    locked_response_ids: tuple[str, ...]
    item_count: Literal[240] = 240
    response_count: int = Field(ge=1, le=240)
    missing_item_count: int = Field(ge=0, le=239)
    missing_item_tokens_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locked_responses_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    complete: bool
    backend_origin_verified: Literal[True] = True
    submitted_snapshot_only: Literal[True] = True
    locked_response_bytes_created: Literal[True] = True
    immutable_persistence_verified: Literal[False] = False
    import_logical_lock_created: Literal[False] = False
    assignment_hmac_verified: Literal[False] = False
    human_identity_verified: Literal[False] = False
    annotator_independence_verified: Literal[False] = False
    raw_votes_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    gold_labels_created: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    requires_operator_attestation: Literal[True] = True
    requires_separate_adjudication: Literal[True] = True

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.response_count + self.missing_item_count != self.item_count:
            raise ValueError("Argilla projection response and missing counts do not reconcile")
        if len(self.receipt_ids) != self.response_count:
            raise ValueError("Argilla projection receipt count differs")
        if len(self.locked_response_ids) != self.response_count:
            raise ValueError("Argilla projection locked-response count differs")
        if len(set(self.receipt_ids)) != self.response_count:
            raise ValueError("Argilla projection receipt IDs must be unique")
        if len(set(self.locked_response_ids)) != self.response_count:
            raise ValueError("Argilla projection response IDs must be unique")
        if self.complete != (self.response_count == self.item_count):
            raise ValueError("Argilla projection completeness flag differs")
        return self


def make_argilla_projection_manifest_id(
    value: ArgillaProjectionManifestContentV1 | dict[str, Any],
) -> str:
    """Return the content address of one projection manifest."""

    content = (
        value
        if isinstance(value, ArgillaProjectionManifestContentV1)
        else ArgillaProjectionManifestContentV1.model_validate(value)
    )
    return "lf023_argilla_projection_manifest_v1:" + hash_canonical(
        {
            "schema": "lf023_argilla_projection_manifest_v1",
            **content.model_dump(mode="json"),
        }
    )


class ArgillaProjectionManifestV1(ArgillaProjectionManifestContentV1):
    """Content-addressed manifest for projected raw votes."""

    manifest_id: str = Field(pattern=_PROJECTION_MANIFEST_ID)

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        content = ArgillaProjectionManifestContentV1.model_validate(
            self.model_dump(mode="json", exclude={"manifest_id"})
        )
        if self.manifest_id != make_argilla_projection_manifest_id(content):
            raise ValueError("Argilla projection manifest ID differs from normalized content")
        return self


@dataclass(frozen=True, slots=True)
class ArgillaProjectionRun:
    """In-memory, deterministic result ready for private immutable persistence."""

    manifest: ArgillaProjectionManifestV1
    responses: tuple[LockedAnnotationResponseEnvelopeV1, ...]
    locked_responses_jsonl: bytes


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArgillaProjectionError(f"raw Argilla record contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise ArgillaProjectionError(f"raw Argilla record contains non-finite value {value!r}")


def _parse_raw_record(raw: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArgillaProjectionError("raw Argilla record is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ArgillaProjectionError("raw Argilla record must contain one JSON object")
    return value


def _required_string(value: Mapping[str, object], field: str, *, owner: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise ArgillaProjectionError(f"{owner} requires a nonempty string {field!r}")
    return result


def _wrapped_value(
    values: Mapping[str, object],
    field: str,
    *,
    optional: bool = False,
) -> object:
    if field not in values:
        if optional:
            return None
        raise ArgillaProjectionError(f"Argilla response values omit required field {field!r}")
    wrapper = values[field]
    if not isinstance(wrapper, dict) or set(wrapper) != {"value"}:
        raise ArgillaProjectionError(
            f"Argilla response field {field!r} must be an exact value wrapper"
        )
    return wrapper["value"]


def _parse_created_at(value: object) -> datetime.datetime:
    if not isinstance(value, str) or not value:
        raise ArgillaProjectionError("Argilla response requires inserted_at")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        require_utc(parsed)
    except ValueError as exc:
        raise ArgillaProjectionError(
            "Argilla response inserted_at must be timezone-aware UTC"
        ) from exc
    return parsed


def _parse_response_values(values: object) -> IndependentAnnotationResponseV1:
    if not isinstance(values, dict):
        raise ArgillaProjectionError("Argilla response values must be a JSON object")
    permitted = {
        "same_claim",
        "relation",
        "confidence",
        "rationale",
        "reference_issue",
        "error_types",
    }
    unexpected = set(values) - permitted
    if unexpected:
        raise ArgillaProjectionError(
            "Argilla response values contain unexpected fields: " + ", ".join(sorted(unexpected))
        )

    same_claim_raw = _wrapped_value(values, "same_claim")
    # Argilla omits an unanswered optional question.  Under the frozen
    # response contract that omission has exactly one valid interpretation:
    # ``cannot_assess_yet`` with ``relation=null``.  The semantic model below
    # rejects omission for every terminal answer.
    relation_raw = _wrapped_value(values, "relation", optional=True)
    confidence_raw = _wrapped_value(values, "confidence")
    rationale_raw = _wrapped_value(values, "rationale")
    reference_issue_raw = _wrapped_value(values, "reference_issue")
    error_types_raw = _wrapped_value(values, "error_types", optional=True)

    if not isinstance(same_claim_raw, str):
        raise ArgillaProjectionError("Argilla same_claim value must be a string")
    if relation_raw is not None and not isinstance(relation_raw, str):
        raise ArgillaProjectionError("Argilla relation value must be a string or null")
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, int):
        raise ArgillaProjectionError("Argilla confidence value must be an integer")
    if not isinstance(rationale_raw, str):
        raise ArgillaProjectionError("Argilla rationale value must be a string")
    if not isinstance(reference_issue_raw, str):
        raise ArgillaProjectionError("Argilla reference_issue value must be a string")
    if error_types_raw is None:
        error_types: tuple[str, ...] = ()
    else:
        if not isinstance(error_types_raw, list) or not all(
            isinstance(item, str) for item in error_types_raw
        ):
            raise ArgillaProjectionError("Argilla error_types value must be a string array")
        if len(set(error_types_raw)) != len(error_types_raw):
            raise ArgillaProjectionError("Argilla error_types value contains duplicates")
        error_types = tuple(sorted(error_types_raw))

    try:
        return IndependentAnnotationResponseV1(
            same_claim=AnnotationAnswer(same_claim_raw),
            relation=RelationLabel(relation_raw) if relation_raw is not None else None,
            confidence=confidence_raw,
            rationale=rationale_raw,
            reference_issue=ReferenceIssue(reference_issue_raw),
            error_types=error_types,
        )
    except ValueError as exc:
        raise ArgillaProjectionError(f"invalid Argilla semantic response: {exc}") from exc


def _response_from_receipt(
    *,
    assignment: HumanAnnotationAssignmentEnvelopeV1,
    pin: ArgillaBackendPinV1,
    receipt: ArgillaDirectFetchReceiptV1,
    raw_record: bytes,
    opaque_item_token: str,
) -> LockedAnnotationResponseEnvelopeV1:
    if receipt.artifact_class != "backend_origin" or not receipt.backend_origin_transport_verified:
        raise ArgillaProjectionError("Argilla projection requires backend-origin receipts")
    if receipt.fixture_only:
        raise ArgillaProjectionError("Argilla projection rejects fixture receipts")
    if receipt.backend_pin_id != pin.pin_id:
        raise ArgillaProjectionError("Argilla receipt belongs to a different backend pin")
    if (
        receipt.endpoint != pin.endpoint
        or receipt.workspace_id != pin.workspace_id
        or receipt.dataset_id != pin.dataset_id
        or receipt.annotator_id != pin.annotator_id
    ):
        raise ArgillaProjectionError("Argilla receipt identity differs from backend pin")
    if len(raw_record) != receipt.raw_record_payload_size:
        raise ArgillaProjectionError("raw Argilla record size differs from receipt")
    if sha256_hex(raw_record) != receipt.raw_record_payload.sha256:
        raise ArgillaProjectionError("raw Argilla record hash differs from receipt")

    record = _parse_raw_record(raw_record)
    if _required_string(record, "id", owner="Argilla record") != receipt.backend_record_id:
        raise ArgillaProjectionError("raw Argilla record ID differs from receipt")
    if _required_string(record, "dataset_id", owner="Argilla record") != pin.dataset_id:
        raise ArgillaProjectionError("raw Argilla record dataset differs from pin")
    responses = record.get("responses")
    if not isinstance(responses, list) or len(responses) != 1:
        raise ArgillaProjectionError("raw Argilla record must contain one isolated response")
    raw_response = responses[0]
    if not isinstance(raw_response, dict):
        raise ArgillaProjectionError("raw Argilla response must be a JSON object")
    if (
        _required_string(raw_response, "id", owner="Argilla response")
        != receipt.backend_response_id
    ):
        raise ArgillaProjectionError("raw Argilla response ID differs from receipt")
    if (
        _required_string(raw_response, "record_id", owner="Argilla response")
        != receipt.backend_record_id
    ):
        raise ArgillaProjectionError("raw Argilla response record ID differs from receipt")
    if _required_string(raw_response, "user_id", owner="Argilla response") != pin.annotator_id:
        raise ArgillaProjectionError("raw Argilla response user differs from pin")
    if _required_string(raw_response, "status", owner="Argilla response") != "submitted":
        raise ArgillaProjectionError("raw Argilla response is not submitted")
    created_at = _parse_created_at(raw_response.get("inserted_at"))
    if created_at < assignment.assigned_at:
        raise ArgillaProjectionError("Argilla response predates the human assignment")
    if created_at > receipt.fetched_at:
        raise ArgillaProjectionError("Argilla response timestamp follows its backend fetch")
    response = _parse_response_values(raw_response.get("values"))
    content = LockedAnnotationResponseContentV1(
        campaign_id=assignment.campaign_id,
        annotator_slot=assignment.annotator_slot,
        opaque_item_token=opaque_item_token,
        annotator_id=assignment.annotator_id,
        round_id=assignment.round_id,
        created_at=created_at,
        bundle_manifest_id=assignment.bundle_manifest_id,
        guideline=assignment.guideline,
        backend_submission_id=receipt.backend_submission_id,
        response=response,
    )
    return LockedAnnotationResponseEnvelopeV1(
        response_id=make_locked_response_id(content),
        **content.model_dump(mode="python"),
    )


def project_captured_argilla_responses(
    *,
    assignment: HumanAnnotationAssignmentEnvelopeV1,
    pin: ArgillaBackendPinV1,
    binding_manifest: ArgillaProjectionBindingManifestV1,
    receipts: tuple[ArgillaDirectFetchReceiptV1, ...],
    raw_record_payloads: Mapping[str, bytes],
    capture_manifest_id: str | None = None,
) -> ArgillaProjectionRun:
    """Project backend-origin snapshots into canonical locked raw votes.

    ``raw_record_payloads`` is keyed by direct-fetch ``receipt_id``.  The
    projection accepts partial rounds, but the pre-response binding manifest
    itself must cover the entire frozen 240-item bundle.
    """

    if assignment.backend_id != "argilla":
        raise ArgillaProjectionError("Argilla projection requires an Argilla assignment")
    if assignment.assignment_mode != "operator_attested_human":
        raise ArgillaProjectionError("production Argilla projection rejects fixture assignments")
    expected_binding_fields = {
        "assignment_id": (binding_manifest.assignment_id, assignment.assignment_id),
        "backend_pin_id": (binding_manifest.backend_pin_id, pin.pin_id),
        "campaign_id": (binding_manifest.campaign_id, assignment.campaign_id),
        "round_id": (binding_manifest.round_id, assignment.round_id),
        "annotator_slot": (binding_manifest.annotator_slot, assignment.annotator_slot),
        "bundle_manifest_id": (
            binding_manifest.bundle_manifest_id,
            assignment.bundle_manifest_id,
        ),
        "public_bundle_manifest": (
            binding_manifest.public_bundle_manifest,
            assignment.public_bundle_manifest,
        ),
        "guideline": (binding_manifest.guideline, assignment.guideline),
    }
    mismatches = [
        field for field, (actual, expected) in expected_binding_fields.items() if actual != expected
    ]
    if mismatches:
        raise ArgillaProjectionError(
            "Argilla projection binding mismatch: " + ", ".join(sorted(mismatches))
        )
    if not receipts:
        raise ArgillaProjectionError("Argilla projection requires at least one receipt")
    receipt_ids = [receipt.receipt_id for receipt in receipts]
    if len(set(receipt_ids)) != len(receipt_ids):
        raise ArgillaProjectionError("Argilla projection receipts contain duplicate IDs")
    if set(raw_record_payloads) != set(receipt_ids):
        raise ArgillaProjectionError("raw Argilla payload keys differ from receipt IDs")

    token_by_record = {
        item.backend_record_id: item.opaque_item_token for item in binding_manifest.item_bindings
    }
    projected: list[
        tuple[str, ArgillaDirectFetchReceiptV1, LockedAnnotationResponseEnvelopeV1]
    ] = []
    for receipt in receipts:
        token = token_by_record.get(receipt.backend_record_id)
        if token is None:
            raise ArgillaProjectionError(
                "Argilla receipt record is absent from pre-response binding"
            )
        response = _response_from_receipt(
            assignment=assignment,
            pin=pin,
            receipt=receipt,
            raw_record=raw_record_payloads[receipt.receipt_id],
            opaque_item_token=token,
        )
        projected.append((token, receipt, response))

    projected.sort(key=lambda item: item[0])
    ordered_tokens = tuple(item[0] for item in projected)
    if len(set(ordered_tokens)) != len(ordered_tokens):
        raise ArgillaProjectionError("Argilla projection contains duplicate blinded items")
    ordered_receipts = tuple(item[1] for item in projected)
    ordered_responses = tuple(item[2] for item in projected)
    locked_jsonl = b"".join(
        canonical_json_bytes(response.model_dump(mode="json")) + b"\n"
        for response in ordered_responses
    )
    all_tokens = {item.opaque_item_token for item in binding_manifest.item_bindings}
    missing_tokens = tuple(sorted(all_tokens - set(ordered_tokens)))
    content = ArgillaProjectionManifestContentV1(
        manifest_kind="lf023_argilla_projection_manifest_v1",
        binding_manifest_id=binding_manifest.manifest_id,
        assignment_id=assignment.assignment_id,
        backend_pin_id=pin.pin_id,
        capture_manifest_id=capture_manifest_id,
        receipt_ids=tuple(receipt.receipt_id for receipt in ordered_receipts),
        locked_response_ids=tuple(response.response_id for response in ordered_responses),
        response_count=len(ordered_responses),
        missing_item_count=len(missing_tokens),
        missing_item_tokens_sha256=sha256_hex(canonical_json_bytes(missing_tokens)),
        locked_responses_sha256=sha256_hex(locked_jsonl),
        complete=len(ordered_responses) == binding_manifest.item_count,
    )
    manifest = ArgillaProjectionManifestV1(
        manifest_id=make_argilla_projection_manifest_id(content),
        **content.model_dump(mode="python"),
    )
    return ArgillaProjectionRun(
        manifest=manifest,
        responses=ordered_responses,
        locked_responses_jsonl=locked_jsonl,
    )


__all__ = [
    "ArgillaProjectionBindingContentV1",
    "ArgillaProjectionBindingManifestV1",
    "ArgillaProjectionError",
    "ArgillaProjectionManifestContentV1",
    "ArgillaProjectionManifestV1",
    "ArgillaProjectionRun",
    "ArgillaRecordItemBindingV1",
    "make_argilla_projection_binding_id",
    "make_argilla_projection_binding_manifest",
    "make_argilla_projection_manifest_id",
    "project_captured_argilla_responses",
]
