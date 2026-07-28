"""Fail-closed Argilla backend-origin evidence for LF-023.

This module deliberately proves less than a human-gold admission:

* a response was fetched directly through a pinned Argilla transport;
* the returned backend/workspace/dataset/record/response identities match the
  predeclared expectation; and
* the exact raw backend payload is retained and content-addressed.

It does not authenticate a human identity, establish annotator independence,
adjudicate a response, or create a semantic label.  Operator HMAC artifacts
remain a separate integrity layer in :mod:`leanfaith.annotation_support.attestation`.
Combining the two evidence sources into human-gold admission requires a later,
separately reviewed policy and schema.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, Protocol, Self
from urllib.parse import quote, urlsplit

import httpx
from pydantic import Field, field_validator, model_validator

from leanfaith.annotation_support.export import ArtifactBinding
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import require_utc

_HEX64 = r"^[0-9a-f]{64}$"
_TRANSPORT_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_PIN_ID = r"^lf023_argilla_backend_pin_v1:[0-9a-f]{64}$"
_RECEIPT_ID = r"^lf023_argilla_direct_fetch_receipt_v1:[0-9a-f]{64}$"


class ArgillaBackendError(ValueError):
    """Raised when Argilla origin evidence cannot be established exactly."""


class ArgillaBackendPinV1(StrictModel):
    """Immutable identity and secret-reference pin for one annotator backend."""

    schema_version: Literal[1] = 1
    pin_id: str = Field(pattern=_PIN_ID)
    backend_id: Literal["argilla"] = "argilla"
    self_hosted: Literal[True] = True
    endpoint: str = Field(min_length=1)
    workspace_id: str = Field(pattern=_UUID)
    dataset_id: str = Field(pattern=_UUID)
    annotator_id: str = Field(pattern=_UUID)
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")

    @field_validator("endpoint")
    @classmethod
    def _strict_endpoint(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value:
            raise ValueError("Argilla endpoint must be stripped text without NUL bytes")
        parsed = urlsplit(value)
        if parsed.scheme != "https":
            raise ValueError("pinned production Argilla endpoint must use https")
        if not parsed.hostname or not parsed.netloc:
            raise ValueError("Argilla endpoint requires an explicit host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Argilla endpoint cannot contain user information")
        if parsed.query or parsed.fragment:
            raise ValueError("Argilla endpoint cannot contain a query or fragment")
        if value.endswith("/"):
            raise ValueError("Argilla endpoint must not end in a slash")
        if parsed.path:
            raise ValueError("Argilla endpoint must be an origin without an API path")
        return value

    @model_validator(mode="after")
    def _content_id(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"pin_id"})
        expected = "lf023_argilla_backend_pin_v1:" + hash_canonical(
            {"schema": "lf023_argilla_backend_pin_v1", **payload}
        )
        if self.pin_id != expected:
            raise ValueError("Argilla backend pin ID differs from normalized content")
        return self


def make_argilla_backend_pin(
    *,
    endpoint: str,
    workspace_id: str,
    dataset_id: str,
    annotator_id: str,
    api_key_env: str,
) -> ArgillaBackendPinV1:
    """Create a content-addressed backend pin without resolving its API key."""

    content = {
        "schema_version": 1,
        "backend_id": "argilla",
        "self_hosted": True,
        "endpoint": endpoint,
        "workspace_id": workspace_id,
        "dataset_id": dataset_id,
        "annotator_id": annotator_id,
        "api_key_env": api_key_env,
    }
    pin_id = "lf023_argilla_backend_pin_v1:" + hash_canonical(
        {"schema": "lf023_argilla_backend_pin_v1", **content}
    )
    return ArgillaBackendPinV1.model_validate({"pin_id": pin_id, **content})


class ArgillaExpectedResponseV1(StrictModel):
    """Exact response identity expected before the direct backend fetch."""

    schema_version: Literal[1] = 1
    backend_record_id: str = Field(pattern=_UUID)
    backend_response_id: str = Field(pattern=_UUID)
    backend_submission_id: str = Field(pattern=_UUID)

    @model_validator(mode="after")
    def _argilla_response_is_submission(self) -> Self:
        if self.backend_submission_id != self.backend_response_id:
            raise ValueError("Argilla 2.8 uses the response UUID as the submission identity")
        return self


@dataclass(frozen=True, slots=True)
class ArgillaTransportResult:
    """Non-persisted result returned by an injected Argilla transport.

    A production transport is responsible for deriving these identities from
    the authenticated endpoint response and request route.  The adapter checks
    all of them against the pin and expectation before persisting anything.
    """

    raw_dataset_payload: bytes
    raw_record_payload: bytes
    backend_id: str
    endpoint: str
    workspace_id: str
    dataset_id: str
    annotator_id: str
    backend_record_id: str
    backend_response_id: str
    backend_submission_id: str
    submitted: bool
    transport_id: str


class ArgillaDirectFetchTransport(Protocol):
    """Injectable authenticated transport boundary.

    The API key is passed only to this call.  It is never placed in a schema,
    request artifact, receipt, filename, exception, or hash by this module.
    """

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
        """Fetch one exact response directly from the pinned backend."""


@dataclass(frozen=True, slots=True, init=False)
class ArgillaV28RestTransport:
    """Argilla 2.8 ``GET /api/v1/records/{record_id}`` transport.

    The endpoint returns a record snapshot containing its responses.  A
    ``submitted`` response is not assumed to be immutable: this transport only
    verifies the state and identities present in the fetched snapshot.
    """

    transport_id: ClassVar[str] = "argilla_v2_8_rest_get_record_v1"
    _client: httpx.Client | None
    _transport: httpx.BaseTransport | None
    _timeout_seconds: float

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("provide either an httpx client or transport, not both")
        if timeout_seconds <= 0:
            raise ValueError("Argilla HTTP timeout must be positive")
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_transport", transport)
        object.__setattr__(self, "_timeout_seconds", timeout_seconds)

    def _uses_production_network_transport(self) -> bool:
        """Return the immutable constructor-selected network mode."""

        return self._client is None and self._transport is None

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
        """Fetch and verify one submitted Argilla response snapshot."""

        workspace_id = _canonical_uuid(workspace_id, field="workspace_id")
        record_id = _canonical_uuid(backend_record_id, field="backend_record_id")
        response_id = _canonical_uuid(backend_response_id, field="backend_response_id")
        expected_dataset_id = _canonical_uuid(dataset_id, field="dataset_id")
        expected_annotator_id = _canonical_uuid(annotator_id, field="annotator_id")
        dataset_url = f"{endpoint}/api/v1/datasets/{quote(expected_dataset_id, safe='')}"
        record_url = f"{endpoint}/api/v1/records/{quote(record_id, safe='')}"
        headers = {
            "Accept": "application/json",
            "X-Argilla-Api-Key": api_key,
        }

        try:
            if self._client is not None:
                dataset_request = self._client.build_request("GET", dataset_url, headers=headers)
                dataset_response = self._client.send(dataset_request, follow_redirects=False)
                record_request = self._client.build_request("GET", record_url, headers=headers)
                record_response = self._client.send(record_request, follow_redirects=False)
            else:
                with httpx.Client(
                    timeout=self._timeout_seconds,
                    transport=self._transport,
                    follow_redirects=False,
                ) as client:
                    dataset_response = client.get(dataset_url, headers=headers)
                    record_response = client.get(record_url, headers=headers)
        except httpx.HTTPError:
            raise ArgillaBackendError("Argilla 2.8 identity request failed") from None

        if dataset_response.status_code != 200:
            raise ArgillaBackendError(
                f"Argilla 2.8 dataset request returned HTTP {dataset_response.status_code}"
            )
        dataset = _parse_raw_json_object(dataset_response.content)
        actual_dataset_id = _required_json_string(dataset, "id", owner="Argilla dataset")
        actual_workspace_id = _required_json_string(
            dataset,
            "workspace_id",
            owner="Argilla dataset",
        )
        _require_same_uuid(actual_dataset_id, expected_dataset_id, field="dataset ID")
        _require_same_uuid(actual_workspace_id, workspace_id, field="workspace ID")

        if record_response.status_code != 200:
            raise ArgillaBackendError(
                f"Argilla 2.8 record request returned HTTP {record_response.status_code}"
            )
        raw_payload = record_response.content
        record = _parse_raw_json_object(raw_payload)
        actual_record_id = _required_json_string(record, "id", owner="Argilla record")
        record_dataset_id = _required_json_string(record, "dataset_id", owner="Argilla record")
        _require_same_uuid(actual_record_id, record_id, field="record ID")
        _require_same_uuid(record_dataset_id, expected_dataset_id, field="record dataset ID")

        raw_responses = record.get("responses")
        if not isinstance(raw_responses, list):
            raise ArgillaBackendError("Argilla record responses must be a JSON array")
        if len(raw_responses) != 1:
            raise ArgillaBackendError(
                "Argilla record snapshot must contain exactly one response to preserve "
                "annotator isolation"
            )
        matching: list[dict[str, object]] = []
        for index, item in enumerate(raw_responses):
            if not isinstance(item, dict):
                raise ArgillaBackendError(
                    f"Argilla record response at index {index} must be a JSON object"
                )
            item_id = _required_json_string(item, "id", owner=f"Argilla response at index {index}")
            if _canonical_uuid(item_id, field=f"response[{index}].id") == response_id:
                matching.append(item)
        if len(matching) != 1:
            raise ArgillaBackendError(
                "Argilla record must contain exactly one response with the expected UUID"
            )

        matched = matching[0]
        actual_response_id = _required_json_string(matched, "id", owner="matched Argilla response")
        actual_response_record_id = _required_json_string(
            matched, "record_id", owner="matched Argilla response"
        )
        actual_annotator_id = _required_json_string(
            matched, "user_id", owner="matched Argilla response"
        )
        status = _required_json_string(matched, "status", owner="matched Argilla response")
        _require_same_uuid(actual_response_id, response_id, field="response ID")
        _require_same_uuid(actual_response_record_id, record_id, field="response record ID")
        canonical_annotator_id = _canonical_uuid(actual_annotator_id, field="response user_id")
        _require_same_uuid(
            canonical_annotator_id,
            expected_annotator_id,
            field="response user ID",
        )
        if status != "submitted":
            raise ArgillaBackendError("Argilla response snapshot is not submitted")

        return ArgillaTransportResult(
            raw_dataset_payload=dataset_response.content,
            raw_record_payload=raw_payload,
            backend_id="argilla",
            endpoint=endpoint,
            workspace_id=workspace_id,
            dataset_id=expected_dataset_id,
            annotator_id=expected_annotator_id,
            backend_record_id=record_id,
            backend_response_id=response_id,
            backend_submission_id=response_id,
            submitted=True,
            transport_id=self.transport_id,
        )


class ArgillaDirectFetchReceiptContentV1(StrictModel):
    """Evidence content for one exact submitted-response snapshot."""

    schema_version: Literal[1] = 1
    evidence_kind: Literal[
        "argilla_backend_origin_submitted_snapshot_v1",
        "argilla_test_transport_submitted_snapshot_v1",
    ]
    artifact_class: Literal["backend_origin", "test_fixture"]
    backend_pin_id: str = Field(pattern=_PIN_ID)
    backend_id: Literal["argilla"]
    endpoint: str
    workspace_id: str = Field(pattern=_UUID)
    dataset_id: str = Field(pattern=_UUID)
    annotator_id: str = Field(pattern=_UUID)
    backend_record_id: str = Field(pattern=_UUID)
    backend_response_id: str = Field(pattern=_UUID)
    backend_submission_id: str = Field(pattern=_UUID)
    transport_id: str = Field(pattern=_TRANSPORT_ID)
    fetched_at: datetime.datetime
    raw_dataset_payload: ArtifactBinding
    raw_dataset_payload_size: int = Field(ge=2)
    raw_record_payload: ArtifactBinding
    raw_record_payload_size: int = Field(ge=2)
    backend_response_submitted: Literal[True] = True
    submitted_snapshot_only: Literal[True] = True
    backend_immutability_verified: Literal[False] = False
    project_logical_lock_included: Literal[False] = False
    payload_identity_verified: Literal[True] = True
    backend_origin_transport_verified: bool
    fixture_only: bool
    operator_hmac_evidence_included: Literal[False] = False
    operator_attestation_verified: Literal[False] = False
    human_identity_verified: Literal[False] = False
    annotator_independence_verified: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gold_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    requires_separate_operator_integrity_evidence: Literal[True] = True
    requires_separate_adjudication: Literal[True] = True

    @model_validator(mode="after")
    def _utc(self) -> Self:
        require_utc(self.fetched_at)
        is_fixture = self.artifact_class == "test_fixture"
        if self.fixture_only != is_fixture:
            raise ValueError("Argilla receipt fixture flag differs from artifact class")
        if self.backend_origin_transport_verified == is_fixture:
            raise ValueError(
                "Argilla backend-origin transport assurance differs from artifact class"
            )
        expected_kind = (
            "argilla_test_transport_submitted_snapshot_v1"
            if is_fixture
            else "argilla_backend_origin_submitted_snapshot_v1"
        )
        if self.evidence_kind != expected_kind:
            raise ValueError("Argilla evidence kind differs from artifact class")
        return self


class ArgillaDirectFetchReceiptV1(ArgillaDirectFetchReceiptContentV1):
    """Content-addressed evidence for one exact direct backend fetch."""

    receipt_id: str = Field(pattern=_RECEIPT_ID)

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        payload = self.model_dump(mode="json")
        expected = "lf023_argilla_direct_fetch_receipt_v1:" + hash_canonical(
            {
                "schema": "lf023_argilla_direct_fetch_receipt_v1",
                **{key: value for key, value in payload.items() if key != "receipt_id"},
            }
        )
        if self.receipt_id != expected:
            raise ValueError("Argilla direct-fetch receipt ID differs from normalized content")
        return self


@dataclass(frozen=True, slots=True)
class ArgillaDirectFetchRun:
    """Persisted results for one duplicate-free direct-fetch batch."""

    receipts: tuple[ArgillaDirectFetchReceiptV1, ...]
    receipt_paths: tuple[Path, ...]
    raw_dataset_payload_paths: tuple[Path, ...]
    raw_record_payload_paths: tuple[Path, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArgillaBackendError(f"raw Argilla payload contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise ArgillaBackendError(f"raw Argilla payload contains non-finite JSON value {value!r}")


def _parse_raw_json_object(raw_payload: bytes) -> dict[str, object]:
    if not raw_payload:
        raise ArgillaBackendError("raw Argilla payload is empty")
    try:
        decoded: object = json.loads(
            raw_payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArgillaBackendError("raw Argilla payload is not strict UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ArgillaBackendError("raw Argilla payload must contain one JSON object")
    return decoded


def _json_contains_secret(value: object, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, list):
        return any(_json_contains_secret(item, secret) for item in value)
    if isinstance(value, dict):
        return any(
            secret in key or _json_contains_secret(item, secret) for key, item in value.items()
        )
    return False


def _payload_contains_secret(raw_payload: bytes, secret: str) -> bool:
    if secret.encode("utf-8") in raw_payload:
        return True
    return _json_contains_secret(_parse_raw_json_object(raw_payload), secret)


def _required_json_string(
    value: dict[str, object],
    field: str,
    *,
    owner: str,
) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise ArgillaBackendError(f"{owner} requires a nonempty string {field!r}")
    return item


def _canonical_uuid(value: str, *, field: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        raise ArgillaBackendError(f"Argilla {field} must be a UUID") from None
    return str(parsed)


def _require_same_uuid(actual: str, expected: str, *, field: str) -> None:
    if _canonical_uuid(actual, field=field) != _canonical_uuid(expected, field=field):
        raise ArgillaBackendError(f"Argilla {field} differs from the pinned identity")


def _validate_submitted_snapshot_payloads(
    *,
    raw_dataset_payload: bytes,
    raw_record_payload: bytes,
    pin: ArgillaBackendPinV1,
    expected: ArgillaExpectedResponseV1,
) -> None:
    """Re-derive every retained identity without trusting transport metadata."""

    dataset = _parse_raw_json_object(raw_dataset_payload)
    actual_dataset_id = _required_json_string(dataset, "id", owner="Argilla dataset")
    actual_workspace_id = _required_json_string(
        dataset,
        "workspace_id",
        owner="Argilla dataset",
    )
    _require_same_uuid(actual_dataset_id, pin.dataset_id, field="dataset ID")
    _require_same_uuid(actual_workspace_id, pin.workspace_id, field="workspace ID")

    record = _parse_raw_json_object(raw_record_payload)
    actual_record_id = _required_json_string(record, "id", owner="Argilla record")
    actual_record_dataset_id = _required_json_string(
        record,
        "dataset_id",
        owner="Argilla record",
    )
    _require_same_uuid(actual_record_id, expected.backend_record_id, field="record ID")
    _require_same_uuid(
        actual_record_dataset_id,
        pin.dataset_id,
        field="record dataset ID",
    )
    raw_responses = record.get("responses")
    if not isinstance(raw_responses, list) or len(raw_responses) != 1:
        raise ArgillaBackendError(
            "retained Argilla record must contain exactly one isolated response"
        )
    response = raw_responses[0]
    if not isinstance(response, dict):
        raise ArgillaBackendError("retained Argilla response must be a JSON object")
    actual_response_id = _required_json_string(
        response,
        "id",
        owner="retained Argilla response",
    )
    actual_response_record_id = _required_json_string(
        response,
        "record_id",
        owner="retained Argilla response",
    )
    actual_annotator_id = _required_json_string(
        response,
        "user_id",
        owner="retained Argilla response",
    )
    status = _required_json_string(
        response,
        "status",
        owner="retained Argilla response",
    )
    _require_same_uuid(
        actual_response_id,
        expected.backend_response_id,
        field="response ID",
    )
    _require_same_uuid(
        actual_response_record_id,
        expected.backend_record_id,
        field="response record ID",
    )
    _require_same_uuid(actual_annotator_id, pin.annotator_id, field="response user ID")
    if status != "submitted":
        raise ArgillaBackendError("retained Argilla response snapshot is not submitted")


def _validate_transport_result(
    *,
    result: ArgillaTransportResult,
    pin: ArgillaBackendPinV1,
    expected: ArgillaExpectedResponseV1,
) -> None:
    fields = {
        "backend_id": (result.backend_id, pin.backend_id),
        "endpoint": (result.endpoint, pin.endpoint),
        "workspace_id": (result.workspace_id, pin.workspace_id),
        "dataset_id": (result.dataset_id, pin.dataset_id),
        "annotator_id": (result.annotator_id, pin.annotator_id),
        "backend_record_id": (result.backend_record_id, expected.backend_record_id),
        "backend_response_id": (result.backend_response_id, expected.backend_response_id),
        "backend_submission_id": (
            result.backend_submission_id,
            expected.backend_submission_id,
        ),
    }
    mismatches = [name for name, (actual, wanted) in fields.items() if actual != wanted]
    if mismatches:
        raise ArgillaBackendError(
            "direct Argilla fetch identity mismatch: " + ", ".join(sorted(mismatches))
        )
    if not result.submitted:
        raise ArgillaBackendError("direct Argilla response snapshot is not submitted")
    if not result.transport_id or not re.fullmatch(_TRANSPORT_ID, result.transport_id):
        raise ArgillaBackendError("Argilla transport ID is invalid")
    _validate_submitted_snapshot_payloads(
        raw_dataset_payload=result.raw_dataset_payload,
        raw_record_payload=result.raw_record_payload,
        pin=pin,
        expected=expected,
    )


def _absolute_without_resolve(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def _reject_symlink_chain(path: Path) -> None:
    current = _absolute_without_resolve(path)
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(metadata.st_mode):
                raise ArgillaBackendError(f"Argilla artifact path contains a symlink: {current}")
        if current == current.parent:
            return
        current = current.parent


def _prepare_private_output_root(output_root: Path) -> Path:
    absolute = _absolute_without_resolve(output_root)
    _reject_symlink_chain(absolute)
    absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_chain(absolute)
    if not absolute.is_dir():
        raise ArgillaBackendError(f"Argilla output root is not a directory: {absolute}")
    os.chmod(absolute, 0o700, follow_symlinks=False)
    return absolute


def _write_private_immutable(path: Path, payload: bytes) -> None:
    _reject_symlink_chain(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_chain(path.parent)
    os.chmod(path.parent, 0o700, follow_symlinks=False)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ArgillaBackendError(f"immutable Argilla artifact differs: {path}") from None
        if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) & 0o077:
            raise ArgillaBackendError(
                f"private Argilla artifact is not mode-0600: {path}"
            ) from None
        return
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600, follow_symlinks=False)


def _make_receipt(
    *,
    pin: ArgillaBackendPinV1,
    expected: ArgillaExpectedResponseV1,
    result: ArgillaTransportResult,
    fetched_at: datetime.datetime,
    raw_dataset_binding: ArtifactBinding,
    raw_record_binding: ArtifactBinding,
    artifact_class: Literal["backend_origin", "test_fixture"],
) -> ArgillaDirectFetchReceiptV1:
    fixture_only = artifact_class == "test_fixture"
    content = {
        "schema_version": 1,
        "evidence_kind": (
            "argilla_test_transport_submitted_snapshot_v1"
            if fixture_only
            else "argilla_backend_origin_submitted_snapshot_v1"
        ),
        "artifact_class": artifact_class,
        "backend_pin_id": pin.pin_id,
        "backend_id": "argilla",
        "endpoint": pin.endpoint,
        "workspace_id": pin.workspace_id,
        "dataset_id": pin.dataset_id,
        "annotator_id": pin.annotator_id,
        "backend_record_id": expected.backend_record_id,
        "backend_response_id": expected.backend_response_id,
        "backend_submission_id": expected.backend_submission_id,
        "transport_id": result.transport_id,
        "fetched_at": fetched_at,
        "raw_dataset_payload": raw_dataset_binding,
        "raw_dataset_payload_size": len(result.raw_dataset_payload),
        "raw_record_payload": raw_record_binding,
        "raw_record_payload_size": len(result.raw_record_payload),
        "backend_response_submitted": True,
        "submitted_snapshot_only": True,
        "backend_immutability_verified": False,
        "project_logical_lock_included": False,
        "payload_identity_verified": True,
        "backend_origin_transport_verified": not fixture_only,
        "fixture_only": fixture_only,
        "operator_hmac_evidence_included": False,
        "operator_attestation_verified": False,
        "human_identity_verified": False,
        "annotator_independence_verified": False,
        "human_gold_eligible": False,
        "semantic_labels_created": False,
        "gold_labels_created": False,
        "training_eligible": False,
        "requires_separate_operator_integrity_evidence": True,
        "requires_separate_adjudication": True,
    }
    normalized = ArgillaDirectFetchReceiptContentV1.model_validate(content)
    receipt_id = "lf023_argilla_direct_fetch_receipt_v1:" + hash_canonical(
        {
            "schema": "lf023_argilla_direct_fetch_receipt_v1",
            **normalized.model_dump(mode="json"),
        }
    )
    return ArgillaDirectFetchReceiptV1.model_validate({"receipt_id": receipt_id, **content})


def fetch_argilla_responses(
    *,
    pin: ArgillaBackendPinV1,
    expected_responses: tuple[ArgillaExpectedResponseV1, ...],
    transport: ArgillaDirectFetchTransport,
    api_key: str,
    output_root: Path,
    fetched_at: datetime.datetime,
    artifact_class: Literal["backend_origin", "test_fixture"] = "backend_origin",
) -> ArgillaDirectFetchRun:
    """Fetch and retain a duplicate-free set of exact Argilla responses.

    This function creates backend-origin evidence only.  Every persisted
    receipt hard-codes all label, gold, and training eligibility flags to
    false.
    """

    require_utc(fetched_at)
    production_transport = (
        type(transport) is ArgillaV28RestTransport
        and transport._uses_production_network_transport()
    )
    if artifact_class == "backend_origin" and not production_transport:
        raise ArgillaBackendError(
            "backend-origin receipts require the concrete production Argilla transport"
        )
    if artifact_class == "test_fixture" and production_transport:
        raise ArgillaBackendError(
            "test-fixture receipts cannot use the production Argilla transport"
        )
    if not api_key:
        raise ArgillaBackendError("Argilla API key must be nonempty")
    if len(api_key.encode("utf-8")) < 16:
        raise ArgillaBackendError("Argilla API key is unexpectedly short")
    if not expected_responses:
        raise ArgillaBackendError("Argilla direct-fetch batch must be nonempty")
    response_ids = [item.backend_response_id for item in expected_responses]
    submission_ids = [item.backend_submission_id for item in expected_responses]
    record_ids = [item.backend_record_id for item in expected_responses]
    if len(set(response_ids)) != len(response_ids):
        raise ArgillaBackendError("expected Argilla responses contain duplicate response IDs")
    if len(set(submission_ids)) != len(submission_ids):
        raise ArgillaBackendError("expected Argilla responses contain duplicate submission IDs")
    if len(set(record_ids)) != len(record_ids):
        raise ArgillaBackendError("expected Argilla responses contain duplicate record IDs")

    unresolved_root = _absolute_without_resolve(output_root)
    _reject_symlink_chain(unresolved_root)
    if unresolved_root.exists() and not unresolved_root.is_dir():
        raise ArgillaBackendError(f"Argilla output root is not a directory: {unresolved_root}")
    fetched: list[tuple[ArgillaExpectedResponseV1, ArgillaTransportResult]] = []
    for expected in expected_responses:
        result = transport.fetch_response(
            endpoint=pin.endpoint,
            workspace_id=pin.workspace_id,
            dataset_id=pin.dataset_id,
            annotator_id=pin.annotator_id,
            backend_record_id=expected.backend_record_id,
            backend_response_id=expected.backend_response_id,
            api_key=api_key,
        )
        _validate_transport_result(result=result, pin=pin, expected=expected)
        if _payload_contains_secret(
            result.raw_dataset_payload, api_key
        ) or _payload_contains_secret(
            result.raw_record_payload,
            api_key,
        ):
            raise ArgillaBackendError("Argilla backend echoed the API key; refusing persistence")
        fetched.append((expected, result))

    raw_record_hashes = [sha256_hex(result.raw_record_payload) for _, result in fetched]
    if len(set(raw_record_hashes)) != len(raw_record_hashes):
        raise ArgillaBackendError(
            "distinct Argilla response identities returned duplicate raw record bytes"
        )

    root = _prepare_private_output_root(unresolved_root)
    receipts: list[ArgillaDirectFetchReceiptV1] = []
    receipt_paths: list[Path] = []
    raw_dataset_paths: list[Path] = []
    raw_record_paths: list[Path] = []
    for expected, result in fetched:
        dataset_filename = sha256_hex(pin.dataset_id.encode("utf-8"))
        dataset_relative = Path("raw") / "datasets" / f"{dataset_filename}.json"
        dataset_path = root / dataset_relative
        _write_private_immutable(dataset_path, result.raw_dataset_payload)
        raw_dataset_binding = ArtifactBinding(
            artifact=dataset_relative.as_posix(),
            sha256=sha256_hex(result.raw_dataset_payload),
        )
        record_filename = sha256_hex(expected.backend_response_id.encode("utf-8"))
        record_relative = Path("raw") / "records" / f"{record_filename}.json"
        record_path = root / record_relative
        _write_private_immutable(record_path, result.raw_record_payload)
        raw_record_binding = ArtifactBinding(
            artifact=record_relative.as_posix(),
            sha256=sha256_hex(result.raw_record_payload),
        )
        receipt = _make_receipt(
            pin=pin,
            expected=expected,
            result=result,
            fetched_at=fetched_at,
            raw_dataset_binding=raw_dataset_binding,
            raw_record_binding=raw_record_binding,
            artifact_class=artifact_class,
        )
        receipt_relative = Path("receipts") / f"{receipt.receipt_id.rsplit(':', 1)[-1]}.json"
        receipt_path = root / receipt_relative
        receipt_bytes = canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n"
        if api_key.encode("utf-8") in receipt_bytes:
            raise ArgillaBackendError("Argilla API key entered receipt bytes")
        _write_private_immutable(receipt_path, receipt_bytes)
        receipts.append(receipt)
        receipt_paths.append(receipt_path)
        raw_dataset_paths.append(dataset_path)
        raw_record_paths.append(record_path)

    return ArgillaDirectFetchRun(
        receipts=tuple(receipts),
        receipt_paths=tuple(receipt_paths),
        raw_dataset_payload_paths=tuple(raw_dataset_paths),
        raw_record_payload_paths=tuple(raw_record_paths),
    )
