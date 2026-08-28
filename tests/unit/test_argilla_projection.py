from __future__ import annotations

import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from leanfaith.annotation_support.argilla_backend import (
    ArgillaDirectFetchReceiptContentV1,
    ArgillaDirectFetchReceiptV1,
    make_argilla_backend_pin,
)
from leanfaith.annotation_support.argilla_projection import (
    ArgillaProjectionBindingManifestV1,
    ArgillaProjectionError,
    ArgillaRecordItemBindingV1,
    make_argilla_projection_binding_manifest,
    project_captured_argilla_responses,
)
from leanfaith.annotation_support.attestation import (
    HumanAnnotationAssignmentContentV1,
    authenticate_human_assignment,
    authentication_key_id,
)
from leanfaith.annotation_support.export import ArtifactBinding
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.schemas.enums import AnnotationAnswer, ReferenceIssue, RelationLabel

UTC = datetime.UTC
KEY = b"lf023-argilla-projection-test-key!" * 2
PIN = make_argilla_backend_pin(
    endpoint="https://argilla.internal.example",
    workspace_id="11111111-1111-4111-8111-111111111111",
    dataset_id="22222222-2222-4222-8222-222222222222",
    annotator_id="33333333-3333-4333-8333-333333333333",
    api_key_env="LF_ARGILLA_ANNOTATOR_1_API_KEY",
)


def _binding(name: str) -> ArtifactBinding:
    return ArtifactBinding(artifact=name, sha256=sha256_hex(name.encode()))


def _assignment():
    content = HumanAnnotationAssignmentContentV1(
        campaign_id="lf021_prevalence_v1",
        round_id="prevalence_round_1",
        annotator_slot="independent_annotator_1",
        annotator_id="expert_1",
        annotator_principal_hash="a" * 64,
        assignment_mode="operator_attested_human",
        backend_id="argilla",
        assigned_at=datetime.datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        public_bundle_manifest=_binding("public-bundle-manifest.json"),
        private_linkage_manifest=_binding("private-linkage-manifest.json"),
        bundle_manifest_id="lf023_blinded_bundle_manifest_v1:" + "b" * 64,
        guideline=_binding("annotation/guidelines_v1.md"),
        authentication_key_id=authentication_key_id(KEY),
    )
    return authenticate_human_assignment(content, key=KEY)


def _record_id(index: int) -> str:
    return f"44444444-4444-4444-8444-{index:012x}"


def _response_id(index: int) -> str:
    return f"55555555-5555-4555-8555-{index:012x}"


def _token(index: int) -> str:
    return "lf023_blind_item_v1:" + f"{index:064x}"


def _item_bindings() -> tuple[ArgillaRecordItemBindingV1, ...]:
    return tuple(
        ArgillaRecordItemBindingV1(
            opaque_item_token=_token(index),
            backend_record_id=_record_id(index),
        )
        for index in range(1, 241)
    )


def _public_bundle_tokens() -> frozenset[str]:
    return frozenset(item.opaque_item_token for item in _item_bindings())


def _projection_binding() -> ArgillaProjectionBindingManifestV1:
    return make_argilla_projection_binding_manifest(
        assignment=_assignment(),
        pin=PIN,
        item_bindings=_item_bindings(),
        public_bundle_tokens=_public_bundle_tokens(),
    )


def _raw_record(
    index: int,
    *,
    values: dict[str, object] | None = None,
    status: str = "submitted",
    inserted_at: str = "2026-07-28T11:00:00Z",
) -> bytes:
    actual_values = values or {
        "same_claim": {"value": "same_claim"},
        "relation": {"value": "equivalent"},
        "confidence": {"value": 5},
        "rationale": {"value": ""},
        "reference_issue": {"value": "none"},
    }
    return canonical_json_bytes(
        {
            "id": _record_id(index),
            "dataset_id": PIN.dataset_id,
            "responses": [
                {
                    "id": _response_id(index),
                    "record_id": _record_id(index),
                    "user_id": PIN.annotator_id,
                    "status": status,
                    "inserted_at": inserted_at,
                    "updated_at": "2026-07-28T11:30:00Z",
                    "values": actual_values,
                }
            ],
        }
    )


def _receipt(index: int, raw_record: bytes) -> ArgillaDirectFetchReceiptV1:
    raw_dataset = canonical_json_bytes(
        {
            "id": PIN.dataset_id,
            "workspace_id": PIN.workspace_id,
        }
    )
    content = ArgillaDirectFetchReceiptContentV1(
        evidence_kind="argilla_backend_origin_submitted_snapshot_v1",
        artifact_class="backend_origin",
        backend_pin_id=PIN.pin_id,
        backend_id="argilla",
        endpoint=PIN.endpoint,
        workspace_id=PIN.workspace_id,
        dataset_id=PIN.dataset_id,
        annotator_id=PIN.annotator_id,
        backend_record_id=_record_id(index),
        backend_response_id=_response_id(index),
        backend_submission_id=_response_id(index),
        transport_id="argilla_v2_8_rest_get_record_v1",
        fetched_at=datetime.datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        raw_dataset_payload=ArtifactBinding(
            artifact="raw/dataset.json",
            sha256=sha256_hex(raw_dataset),
        ),
        raw_dataset_payload_size=len(raw_dataset),
        raw_record_payload=ArtifactBinding(
            artifact=f"raw/record-{index}.json",
            sha256=sha256_hex(raw_record),
        ),
        raw_record_payload_size=len(raw_record),
        backend_origin_transport_verified=True,
        fixture_only=False,
    )
    receipt_id = "lf023_argilla_direct_fetch_receipt_v1:" + hash_canonical(
        {
            "schema": "lf023_argilla_direct_fetch_receipt_v1",
            **content.model_dump(mode="json"),
        }
    )
    return ArgillaDirectFetchReceiptV1(
        receipt_id=receipt_id,
        **content.model_dump(mode="python"),
    )


def _project(*indices: int):
    raws = {index: _raw_record(index) for index in indices}
    receipts = tuple(_receipt(index, raws[index]) for index in indices)
    return project_captured_argilla_responses(
        assignment=_assignment(),
        pin=PIN,
        binding_manifest=_projection_binding(),
        receipts=receipts,
        raw_record_payloads={
            receipt.receipt_id: raws[index]
            for index, receipt in zip(indices, receipts, strict=True)
        },
    )


def test_projection_creates_canonical_locked_raw_vote_without_gold_claims() -> None:
    run = _project(1)

    assert len(run.responses) == 1
    response = run.responses[0]
    assert response.opaque_item_token == _token(1)
    assert response.annotator_id == "expert_1"
    assert response.backend_submission_id == _response_id(1)
    assert response.response.same_claim is AnnotationAnswer.SAME_CLAIM
    assert response.response.relation is RelationLabel.EQUIVALENT
    assert response.response.reference_issue is ReferenceIssue.NONE
    assert response.created_at == datetime.datetime(2026, 7, 28, 11, 0, tzinfo=UTC)
    assert run.locked_responses_jsonl.endswith(b"\n")
    assert run.manifest.response_count == 1
    assert run.manifest.missing_item_count == 239
    assert run.manifest.complete is False
    assert run.manifest.backend_origin_verified is True
    assert run.manifest.assignment_hmac_verified is False
    assert run.manifest.human_gold_eligible is False
    assert run.manifest.semantic_labels_created is False
    assert run.manifest.gold_labels_created is False
    assert run.manifest.training_eligible is False


def test_projection_is_independent_of_receipt_input_order() -> None:
    first = _project(2, 1)
    second = _project(1, 2)

    assert tuple(item.opaque_item_token for item in first.responses) == (_token(1), _token(2))
    assert first.locked_responses_jsonl == second.locked_responses_jsonl
    assert first.manifest == second.manifest


def test_projection_parses_unresolved_response_without_inventing_relation() -> None:
    raw = _raw_record(
        1,
        values={
            "same_claim": {"value": "cannot_assess_yet"},
            "confidence": {"value": 2},
            "rationale": {"value": "A domain expert must inspect this definition."},
            "reference_issue": {"value": "suspected"},
            "error_types": {"value": ["E07", "E01"]},
        },
    )
    receipt = _receipt(1, raw)

    run = project_captured_argilla_responses(
        assignment=_assignment(),
        pin=PIN,
        binding_manifest=_projection_binding(),
        receipts=(receipt,),
        raw_record_payloads={receipt.receipt_id: raw},
    )

    response = run.responses[0].response
    assert response.same_claim is AnnotationAnswer.CANNOT_ASSESS_YET
    assert response.relation is None
    assert response.reference_issue is ReferenceIssue.SUSPECTED
    assert response.error_types == ("E01", "E07")


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (
            {
                "same_claim": {"value": "same_claim", "extra": "forbidden"},
                "relation": {"value": "equivalent"},
                "confidence": {"value": 5},
                "rationale": {"value": ""},
                "reference_issue": {"value": "none"},
            },
            "exact value wrapper",
        ),
        (
            {
                "same_claim": {"value": "same_claim"},
                "relation": {"value": "equivalent"},
                "confidence": {"value": True},
                "rationale": {"value": ""},
                "reference_issue": {"value": "none"},
            },
            "must be an integer",
        ),
        (
            {
                "same_claim": {"value": "same_claim"},
                "relation": {"value": "equivalent"},
                "confidence": {"value": 5},
                "rationale": {"value": ""},
                "reference_issue": {"value": "no_issue"},
            },
            "invalid Argilla semantic response",
        ),
        (
            {
                "same_claim": {"value": "same_claim"},
                "relation": {"value": "equivalent"},
                "confidence": {"value": 5},
                "rationale": {"value": ""},
                "reference_issue": {"value": "none"},
                "diagnostic": {"value": "forbidden"},
            },
            "unexpected fields",
        ),
        (
            {
                "same_claim": {"value": "same_claim"},
                "confidence": {"value": 5},
                "rationale": {"value": ""},
                "reference_issue": {"value": "none"},
            },
            "invalid Argilla semantic response",
        ),
    ],
)
def test_projection_rejects_noncanonical_argilla_values(
    mutation: dict[str, object],
    match: str,
) -> None:
    raw = _raw_record(1, values=mutation)
    receipt = _receipt(1, raw)

    with pytest.raises(ArgillaProjectionError, match=match):
        project_captured_argilla_responses(
            assignment=_assignment(),
            pin=PIN,
            binding_manifest=_projection_binding(),
            receipts=(receipt,),
            raw_record_payloads={receipt.receipt_id: raw},
        )


def test_projection_rejects_unbound_record_before_reading_values() -> None:
    raw = _raw_record(241)
    receipt = _receipt(241, raw)

    with pytest.raises(ArgillaProjectionError, match="absent from pre-response binding"):
        project_captured_argilla_responses(
            assignment=_assignment(),
            pin=PIN,
            binding_manifest=_projection_binding(),
            receipts=(receipt,),
            raw_record_payloads={receipt.receipt_id: raw},
        )


def test_projection_rejects_raw_payload_hash_drift() -> None:
    raw = _raw_record(1)
    receipt = _receipt(1, raw)
    changed = raw + b" "

    with pytest.raises(ArgillaProjectionError, match="size differs"):
        project_captured_argilla_responses(
            assignment=_assignment(),
            pin=PIN,
            binding_manifest=_projection_binding(),
            receipts=(receipt,),
            raw_record_payloads={receipt.receipt_id: changed},
        )


def test_projection_rejects_payload_set_not_equal_to_receipts() -> None:
    raw = _raw_record(1)
    receipt = _receipt(1, raw)

    with pytest.raises(ArgillaProjectionError, match="keys differ"):
        project_captured_argilla_responses(
            assignment=_assignment(),
            pin=PIN,
            binding_manifest=_projection_binding(),
            receipts=(receipt,),
            raw_record_payloads={},
        )


def test_projection_binding_requires_sorted_complete_unique_allocation() -> None:
    assignment = _assignment()
    items = list(_item_bindings())
    items[1] = items[0]

    with pytest.raises(ValidationError, match="duplicate item tokens"):
        make_argilla_projection_binding_manifest(
            assignment=assignment,
            pin=PIN,
            item_bindings=tuple(items),
            public_bundle_tokens=_public_bundle_tokens(),
        )

    with pytest.raises(ValidationError, match="cover all 240"):
        make_argilla_projection_binding_manifest(
            assignment=assignment,
            pin=PIN,
            item_bindings=_item_bindings()[:-1],
            public_bundle_tokens=_public_bundle_tokens(),
        )


def test_projection_binding_manifest_is_content_addressed() -> None:
    manifest = _projection_binding()
    payload: dict[str, Any] = manifest.model_dump(mode="json")
    payload["round_id"] = "changed_round"

    with pytest.raises(ValidationError, match="ID differs"):
        ArgillaProjectionBindingManifestV1.model_validate(payload)
