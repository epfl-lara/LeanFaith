from __future__ import annotations

import dataclasses
import datetime
import json
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from leanfaith.annotation_support.argilla_backend import (
    ArgillaBackendError,
    ArgillaBackendPinV1,
    ArgillaDirectFetchRun,
    ArgillaExpectedResponseV1,
    ArgillaTransportResult,
    ArgillaV28RestTransport,
    fetch_argilla_responses,
    make_argilla_backend_pin,
)
from leanfaith.config.hashing import canonical_json_bytes, sha256_hex

UTC = datetime.UTC
FETCHED_AT = datetime.datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
API_KEY = "argilla-test-key-never-persisted-0123456789"
ARGILLA_RECORD_ID = "11111111-1111-4111-8111-111111111111"
ARGILLA_RESPONSE_ID = "22222222-2222-4222-8222-222222222222"
ARGILLA_DATASET_ID = "33333333-3333-4333-8333-333333333333"
ARGILLA_ANNOTATOR_ID = "44444444-4444-4444-8444-444444444444"
ARGILLA_WORKSPACE_ID = "55555555-5555-4555-8555-555555555555"


def _pin() -> ArgillaBackendPinV1:
    return make_argilla_backend_pin(
        endpoint="https://argilla.internal.example",
        workspace_id=ARGILLA_WORKSPACE_ID,
        dataset_id=ARGILLA_DATASET_ID,
        annotator_id=ARGILLA_ANNOTATOR_ID,
        api_key_env="ARGILLA_API_KEY",
    )


def _expected(index: int = 1) -> ArgillaExpectedResponseV1:
    suffix = f"{index:012x}"
    return ArgillaExpectedResponseV1(
        backend_record_id=f"aaaaaaaa-aaaa-4aaa-8aaa-{suffix}",
        backend_response_id=f"bbbbbbbb-bbbb-4bbb-8bbb-{suffix}",
        backend_submission_id=f"bbbbbbbb-bbbb-4bbb-8bbb-{suffix}",
    )


def _result(
    expected: ArgillaExpectedResponseV1,
    *,
    pin: ArgillaBackendPinV1 | None = None,
) -> ArgillaTransportResult:
    active_pin = pin or _pin()
    raw_dataset = canonical_json_bytes(
        {
            "id": active_pin.dataset_id,
            "workspace_id": active_pin.workspace_id,
        }
    )
    raw_record = canonical_json_bytes(
        {
            "id": expected.backend_record_id,
            "dataset_id": active_pin.dataset_id,
            "responses": [
                {
                    "id": expected.backend_response_id,
                    "record_id": expected.backend_record_id,
                    "user_id": active_pin.annotator_id,
                    "status": "submitted",
                    "values": {"same_claim": "same_claim"},
                }
            ],
        }
    )
    return ArgillaTransportResult(
        raw_dataset_payload=raw_dataset,
        raw_record_payload=raw_record,
        backend_id="argilla",
        endpoint=active_pin.endpoint,
        workspace_id=active_pin.workspace_id,
        dataset_id=active_pin.dataset_id,
        annotator_id=active_pin.annotator_id,
        backend_record_id=expected.backend_record_id,
        backend_response_id=expected.backend_response_id,
        backend_submission_id=expected.backend_submission_id,
        submitted=True,
        transport_id="argilla_test_transport_v1",
    )


class FakeTransport:
    def __init__(self, results: dict[str, ArgillaTransportResult]) -> None:
        self.results = results
        self.calls: list[dict[str, str]] = []

    def fetch_response(
        self,
        *,
        endpoint: str,
        workspace_id: str,
        dataset_id: str,
        annotator_id: str,
        backend_record_id: str,
        backend_response_id: str,
        api_key: str,
    ) -> ArgillaTransportResult:
        self.calls.append(
            {
                "endpoint": endpoint,
                "workspace_id": workspace_id,
                "dataset_id": dataset_id,
                "annotator_id": annotator_id,
                "backend_record_id": backend_record_id,
                "backend_response_id": backend_response_id,
                "api_key": api_key,
            }
        )
        return self.results[backend_response_id]


def _argilla_v28_record_payload(
    *,
    record_id: str = ARGILLA_RECORD_ID,
    dataset_id: str = ARGILLA_DATASET_ID,
    response_id: str = ARGILLA_RESPONSE_ID,
    response_record_id: str = ARGILLA_RECORD_ID,
    annotator_id: str = ARGILLA_ANNOTATOR_ID,
    response_status: str = "submitted",
    responses: list[object] | None = None,
) -> bytes:
    response_items = responses
    if response_items is None:
        response_items = [
            {
                "id": response_id,
                "values": {"same_claim": {"value": "equivalent"}},
                "status": response_status,
                "record_id": response_record_id,
                "user_id": annotator_id,
                "inserted_at": "2026-07-28T11:59:00Z",
                "updated_at": "2026-07-28T12:00:00Z",
            }
        ]
    return canonical_json_bytes(
        {
            "id": record_id,
            "status": "completed",
            "fields": {"candidate": "theorem candidate : True"},
            "responses": response_items,
            "dataset_id": dataset_id,
            "inserted_at": "2026-07-28T11:00:00Z",
            "updated_at": "2026-07-28T12:00:00Z",
        }
    )


def _argilla_v28_dataset_payload(
    *,
    dataset_id: str = ARGILLA_DATASET_ID,
    workspace_id: str = ARGILLA_WORKSPACE_ID,
) -> bytes:
    return canonical_json_bytes(
        {
            "id": dataset_id,
            "name": "lf023-production",
            "workspace_id": workspace_id,
        }
    )


def _argilla_v28_fetch(
    raw_payload: bytes,
    *,
    record_status_code: int = 200,
    dataset_payload: bytes | None = None,
    dataset_status_code: int = 200,
) -> tuple[ArgillaTransportResult, tuple[httpx.Request, httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/api/v1/datasets/" in request.url.path:
            return httpx.Response(
                dataset_status_code,
                content=dataset_payload or _argilla_v28_dataset_payload(),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(
            record_status_code,
            content=raw_payload,
            headers={"Content-Type": "application/json"},
        )

    transport = ArgillaV28RestTransport(transport=httpx.MockTransport(handler))
    result = transport.fetch_response(
        endpoint="https://argilla.internal.example",
        workspace_id=ARGILLA_WORKSPACE_ID,
        dataset_id=ARGILLA_DATASET_ID,
        annotator_id=ARGILLA_ANNOTATOR_ID,
        backend_record_id=ARGILLA_RECORD_ID,
        backend_response_id=ARGILLA_RESPONSE_ID,
        api_key=API_KEY,
    )
    assert len(requests) == 2
    return result, (requests[0], requests[1])


def test_argilla_v28_rest_transport_fetches_exact_submitted_snapshot() -> None:
    raw_payload = _argilla_v28_record_payload()

    result, requests = _argilla_v28_fetch(raw_payload)
    dataset_request, record_request = requests

    assert dataset_request.method == "GET"
    assert str(dataset_request.url) == (
        "https://argilla.internal.example/api/v1/datasets/" + ARGILLA_DATASET_ID
    )
    assert record_request.method == "GET"
    assert str(record_request.url) == (
        "https://argilla.internal.example/api/v1/records/" + ARGILLA_RECORD_ID
    )
    assert dataset_request.headers["X-Argilla-Api-Key"] == API_KEY
    assert record_request.headers["X-Argilla-Api-Key"] == API_KEY
    assert result.raw_dataset_payload == _argilla_v28_dataset_payload()
    assert result.raw_record_payload == raw_payload
    assert result.backend_record_id == ARGILLA_RECORD_ID
    assert result.backend_response_id == ARGILLA_RESPONSE_ID
    assert result.backend_submission_id == ARGILLA_RESPONSE_ID
    assert result.dataset_id == ARGILLA_DATASET_ID
    assert result.annotator_id == ARGILLA_ANNOTATOR_ID
    assert result.submitted is True
    assert result.transport_id == "argilla_v2_8_rest_get_record_v1"


@pytest.mark.parametrize(
    "dataset_payload",
    [
        _argilla_v28_dataset_payload(dataset_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        _argilla_v28_dataset_payload(workspace_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    ],
    ids=["dataset-id", "workspace-id"],
)
def test_argilla_v28_rest_transport_rejects_dataset_or_workspace_mismatch(
    dataset_payload: bytes,
) -> None:
    with pytest.raises(ArgillaBackendError):
        _argilla_v28_fetch(
            _argilla_v28_record_payload(),
            dataset_payload=dataset_payload,
        )


@pytest.mark.parametrize(
    "raw_payload",
    [
        _argilla_v28_record_payload(record_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        _argilla_v28_record_payload(dataset_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        _argilla_v28_record_payload(response_record_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        _argilla_v28_record_payload(annotator_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        _argilla_v28_record_payload(response_status="draft"),
    ],
    ids=[
        "record-id",
        "dataset-id",
        "response-record-id",
        "annotator-id",
        "not-submitted",
    ],
)
def test_argilla_v28_rest_transport_rejects_identity_or_status_mismatch(
    raw_payload: bytes,
) -> None:
    with pytest.raises(ArgillaBackendError):
        _argilla_v28_fetch(raw_payload)


@pytest.mark.parametrize(
    "responses",
    [
        [],
        [
            {
                "id": ARGILLA_RESPONSE_ID,
                "status": "submitted",
                "record_id": ARGILLA_RECORD_ID,
                "user_id": ARGILLA_ANNOTATOR_ID,
            },
            {
                "id": ARGILLA_RESPONSE_ID,
                "status": "submitted",
                "record_id": ARGILLA_RECORD_ID,
                "user_id": ARGILLA_ANNOTATOR_ID,
            },
        ],
        [
            {
                "id": ARGILLA_RESPONSE_ID,
                "status": "submitted",
                "record_id": ARGILLA_RECORD_ID,
                "user_id": ARGILLA_ANNOTATOR_ID,
            },
            {
                "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "status": "submitted",
                "record_id": ARGILLA_RECORD_ID,
                "user_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            },
        ],
    ],
    ids=["missing", "duplicate", "peer-response"],
)
def test_argilla_v28_rest_transport_requires_one_exact_response(
    responses: list[object],
) -> None:
    with pytest.raises(ArgillaBackendError, match="exactly one response"):
        _argilla_v28_fetch(_argilla_v28_record_payload(responses=responses))


def test_argilla_v28_rest_transport_rejects_http_failure_without_leaking_key() -> None:
    with pytest.raises(ArgillaBackendError, match="HTTP 403") as failure:
        _argilla_v28_fetch(
            b'{"detail":"forbidden"}',
            record_status_code=403,
        )

    assert API_KEY not in str(failure.value)


def test_argilla_v28_rest_transport_rejects_dataset_http_failure_without_leaking_key() -> None:
    with pytest.raises(ArgillaBackendError, match="dataset request returned HTTP 403") as failure:
        _argilla_v28_fetch(
            _argilla_v28_record_payload(),
            dataset_status_code=403,
        )

    assert API_KEY not in str(failure.value)


def _run(
    tmp_path: Path,
    *,
    expected: tuple[ArgillaExpectedResponseV1, ...] | None = None,
    transport: FakeTransport | None = None,
    output_root: Path | None = None,
    api_key: str = API_KEY,
) -> ArgillaDirectFetchRun:
    items = (_expected(),) if expected is None else expected
    active_transport = transport or FakeTransport(
        {item.backend_response_id: _result(item) for item in items}
    )
    return fetch_argilla_responses(
        pin=_pin(),
        expected_responses=items,
        transport=active_transport,
        api_key=api_key,
        output_root=output_root or tmp_path / "argilla-origin",
        fetched_at=FETCHED_AT,
        artifact_class="test_fixture",
    )


def test_direct_fetch_persists_exact_private_origin_receipts_without_api_key(
    tmp_path: Path,
) -> None:
    expected = (_expected(1), _expected(2))
    transport = FakeTransport({item.backend_response_id: _result(item) for item in expected})

    run = _run(tmp_path, expected=expected, transport=transport)

    assert len(run.receipts) == 2
    assert [call["api_key"] for call in transport.calls] == [API_KEY, API_KEY]
    for expected_item, receipt, raw_dataset_path, raw_record_path, receipt_path in zip(
        expected,
        run.receipts,
        run.raw_dataset_payload_paths,
        run.raw_record_payload_paths,
        run.receipt_paths,
        strict=True,
    ):
        assert receipt.backend_pin_id == _pin().pin_id
        assert receipt.backend_response_id == expected_item.backend_response_id
        assert receipt.backend_submission_id == expected_item.backend_submission_id
        assert receipt.backend_response_submitted is True
        assert receipt.submitted_snapshot_only is True
        assert receipt.backend_immutability_verified is False
        assert receipt.project_logical_lock_included is False
        assert receipt.payload_identity_verified is True
        assert receipt.backend_origin_transport_verified is False
        assert receipt.fixture_only is True
        assert receipt.artifact_class == "test_fixture"
        assert receipt.evidence_kind == "argilla_test_transport_submitted_snapshot_v1"
        assert receipt.operator_hmac_evidence_included is False
        assert receipt.operator_attestation_verified is False
        assert receipt.human_identity_verified is False
        assert receipt.annotator_independence_verified is False
        assert receipt.human_gold_eligible is False
        assert receipt.semantic_labels_created is False
        assert receipt.gold_labels_created is False
        assert receipt.training_eligible is False
        assert receipt.requires_separate_operator_integrity_evidence is True
        assert receipt.requires_separate_adjudication is True
        assert receipt.raw_dataset_payload.sha256 == sha256_hex(raw_dataset_path.read_bytes())
        assert receipt.raw_dataset_payload_size == len(raw_dataset_path.read_bytes())
        assert receipt.raw_record_payload.sha256 == sha256_hex(raw_record_path.read_bytes())
        assert receipt.raw_record_payload_size == len(raw_record_path.read_bytes())
        assert stat.S_IMODE(raw_dataset_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(raw_record_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
        persisted = (
            raw_dataset_path.read_bytes() + raw_record_path.read_bytes() + receipt_path.read_bytes()
        )
        assert API_KEY.encode() not in persisted
        assert "authentication_tag" not in receipt_path.read_text()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend_id", "not-argilla"),
        ("endpoint", "https://other.example/api"),
        ("workspace_id", "other-workspace"),
        ("dataset_id", "other-dataset"),
        ("annotator_id", "other-annotator"),
        ("backend_record_id", "other-record"),
        ("backend_response_id", "other-response"),
        ("backend_submission_id", "other-response"),
        ("submitted", False),
    ],
)
def test_direct_fetch_rejects_every_identity_or_submission_mismatch(
    tmp_path: Path,
    field: str,
    value: str | bool,
) -> None:
    expected = _expected()
    result = dataclasses.replace(
        _result(expected),
        **{field: value},  # type: ignore[arg-type]
    )
    transport = FakeTransport({expected.backend_response_id: result})

    with pytest.raises(ArgillaBackendError):
        _run(tmp_path, transport=transport)

    assert not (tmp_path / "argilla-origin").exists()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://argilla.internal.example",
        "https://user:secret@argilla.internal.example",
        "https://argilla.internal.example/",
        "https://argilla.internal.example/api",
        "https://argilla.internal.example/api?token=secret",
        "https://argilla.internal.example/api#fragment",
        "https://argilla.internal.example/a/../b",
    ],
)
def test_backend_pin_rejects_unpinned_or_credential_bearing_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        make_argilla_backend_pin(
            endpoint=endpoint,
            workspace_id=ARGILLA_WORKSPACE_ID,
            dataset_id=ARGILLA_DATASET_ID,
            annotator_id=ARGILLA_ANNOTATOR_ID,
            api_key_env="ARGILLA_API_KEY",
        )


def test_backend_pin_and_expected_response_require_canonical_uuid_identities() -> None:
    with pytest.raises(ValidationError):
        make_argilla_backend_pin(
            endpoint="https://argilla.internal.example",
            workspace_id="workspace",
            dataset_id="dataset",
            annotator_id="annotator",
            api_key_env="ARGILLA_API_KEY",
        )
    with pytest.raises(ValidationError):
        ArgillaExpectedResponseV1(
            backend_record_id="record",
            backend_response_id="response",
            backend_submission_id="response",
        )


def test_backend_pin_is_strict_content_addressed_and_contains_only_secret_reference() -> None:
    pin = _pin()
    assert pin.api_key_env == "ARGILLA_API_KEY"
    assert API_KEY not in json.dumps(pin.model_dump(mode="json"))

    with pytest.raises(ValidationError, match="pin ID"):
        ArgillaBackendPinV1.model_validate(
            {**pin.model_dump(mode="json"), "pin_id": "lf023_argilla_backend_pin_v1:" + "0" * 64}
        )
    with pytest.raises(ValidationError):
        ArgillaBackendPinV1.model_validate({**pin.model_dump(mode="json"), "api_key": API_KEY})


@pytest.mark.parametrize("duplicate_field", ["response", "submission", "record"])
def test_duplicate_expected_backend_identities_fail_before_transport(
    tmp_path: Path,
    duplicate_field: str,
) -> None:
    first = _expected(1)
    second = _expected(2)
    if duplicate_field == "response":
        second = second.model_copy(update={"backend_response_id": first.backend_response_id})
    elif duplicate_field == "submission":
        second = second.model_copy(update={"backend_submission_id": first.backend_submission_id})
    else:
        second = second.model_copy(update={"backend_record_id": first.backend_record_id})
    transport = FakeTransport({})

    with pytest.raises(ArgillaBackendError, match="duplicate"):
        _run(tmp_path, expected=(first, second), transport=transport)

    assert transport.calls == []


def test_empty_direct_fetch_batch_is_rejected(tmp_path: Path) -> None:
    transport = FakeTransport({})

    with pytest.raises(ArgillaBackendError, match="nonempty"):
        _run(tmp_path, expected=(), transport=transport)

    assert transport.calls == []


def test_distinct_identities_cannot_share_identical_raw_backend_bytes(tmp_path: Path) -> None:
    first = _expected(1)
    second = _expected(2)
    shared_raw = _result(first).raw_record_payload
    transport = FakeTransport(
        {
            first.backend_response_id: dataclasses.replace(
                _result(first),
                raw_record_payload=shared_raw,
            ),
            second.backend_response_id: dataclasses.replace(
                _result(second),
                raw_record_payload=shared_raw,
            ),
        }
    )

    with pytest.raises(ArgillaBackendError, match="pinned identity"):
        _run(tmp_path, expected=(first, second), transport=transport)

    assert not (tmp_path / "argilla-origin").exists()


@pytest.mark.parametrize(
    "raw_payload",
    [
        b'{"id":"response-1","id":"duplicate"}',
        b'{"value":NaN}',
        b"[]",
        b"",
        b"\xff",
    ],
)
def test_raw_backend_payload_must_be_strict_json_object(
    tmp_path: Path,
    raw_payload: bytes,
) -> None:
    expected = _expected()
    transport = FakeTransport(
        {
            expected.backend_response_id: dataclasses.replace(
                _result(expected),
                raw_record_payload=raw_payload,
            )
        }
    )

    with pytest.raises(ArgillaBackendError):
        _run(tmp_path, transport=transport)

    assert not (tmp_path / "argilla-origin").exists()


def test_forged_matching_transport_metadata_cannot_hide_empty_payloads(
    tmp_path: Path,
) -> None:
    expected = _expected()
    forged = dataclasses.replace(
        _result(expected),
        raw_dataset_payload=b"{}",
        raw_record_payload=b"{}",
        transport_id="argilla_v2_8_rest_get_record_v1",
    )
    transport = FakeTransport({expected.backend_response_id: forged})

    with pytest.raises(ArgillaBackendError, match="requires a nonempty string"):
        _run(tmp_path, transport=transport)

    assert not (tmp_path / "argilla-origin").exists()


def test_injected_transport_cannot_mint_backend_origin_receipts(tmp_path: Path) -> None:
    expected = _expected()
    transport = FakeTransport({expected.backend_response_id: _result(expected)})

    with pytest.raises(ArgillaBackendError, match="concrete production Argilla transport"):
        fetch_argilla_responses(
            pin=_pin(),
            expected_responses=(expected,),
            transport=transport,
            api_key=API_KEY,
            output_root=tmp_path / "argilla-origin",
            fetched_at=FETCHED_AT,
        )

    assert transport.calls == []


def test_injected_mock_transport_mode_cannot_be_mutated_into_backend_origin(
    tmp_path: Path,
) -> None:
    expected = _expected()
    mock_transport = httpx.MockTransport(lambda request: httpx.Response(500, request=request))
    transport = ArgillaV28RestTransport(transport=mock_transport)

    for field, value in (
        ("production_network_transport", True),
        ("_transport", None),
    ):
        with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
            setattr(transport, field, value)

    with pytest.raises(ArgillaBackendError, match="concrete production Argilla transport"):
        fetch_argilla_responses(
            pin=_pin(),
            expected_responses=(expected,),
            transport=transport,
            api_key=API_KEY,
            output_root=tmp_path / "argilla-origin",
            fetched_at=FETCHED_AT,
            artifact_class="backend_origin",
        )

    assert not (tmp_path / "argilla-origin").exists()


def test_backend_api_key_echo_is_rejected_before_any_persistence(tmp_path: Path) -> None:
    expected = _expected()
    record = json.loads(_result(expected).raw_record_payload)
    record["debug_authorization"] = API_KEY
    transport = FakeTransport(
        {
            expected.backend_response_id: dataclasses.replace(
                _result(expected),
                raw_record_payload=canonical_json_bytes(record),
            )
        }
    )

    with pytest.raises(ArgillaBackendError, match="echoed the API key"):
        _run(tmp_path, transport=transport)

    assert not (tmp_path / "argilla-origin").exists()


def test_json_escaped_backend_api_key_echo_is_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    expected = _expected()
    escaped_key = API_KEY.replace("a", r"\u0061")
    valid_record = _result(expected).raw_record_payload.decode()
    raw_record = (valid_record[:-1] + ',"debug_authorization":"' + escaped_key + '"}').encode()
    assert API_KEY.encode() not in raw_record
    transport = FakeTransport(
        {
            expected.backend_response_id: dataclasses.replace(
                _result(expected),
                raw_record_payload=raw_record,
            )
        }
    )

    with pytest.raises(ArgillaBackendError, match="echoed the API key"):
        _run(tmp_path, transport=transport)

    assert not (tmp_path / "argilla-origin").exists()


def test_symlinked_output_root_is_rejected_before_transport(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    transport = FakeTransport({})

    with pytest.raises(ArgillaBackendError, match="symlink"):
        _run(tmp_path, transport=transport, output_root=linked_root)

    assert transport.calls == []


def test_symlinked_existing_raw_artifact_is_rejected(tmp_path: Path) -> None:
    expected = _expected()
    output_root = tmp_path / "argilla-origin"
    raw_root = output_root / "raw" / "records"
    raw_root.mkdir(parents=True)
    target = tmp_path / "target.json"
    target.write_text("{}")
    filename = sha256_hex(expected.backend_response_id.encode("utf-8"))
    (raw_root / f"{filename}.json").symlink_to(target)

    with pytest.raises(ArgillaBackendError, match="differs"):
        _run(tmp_path, output_root=output_root)


def test_exact_direct_fetch_replay_is_idempotent(tmp_path: Path) -> None:
    first = _run(tmp_path)
    second = _run(tmp_path)

    assert second.receipts == first.receipts
    assert second.raw_dataset_payload_paths == first.raw_dataset_payload_paths
    assert second.raw_record_payload_paths == first.raw_record_payload_paths
    assert second.receipt_paths == first.receipt_paths
