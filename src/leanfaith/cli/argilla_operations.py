"""Fail-closed LF-023 Argilla backend registration and capture orchestration."""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, ValidationError, field_validator, model_validator

from leanfaith.annotation_support.argilla_backend import (
    ArgillaBackendError,
    ArgillaBackendPinV1,
    ArgillaDirectFetchRun,
    ArgillaExpectedResponseV1,
    ArgillaV28RestTransport,
    fetch_argilla_responses,
    make_argilla_backend_pin,
    read_argilla_regular_file_nofollow,
    write_argilla_private_immutable,
)
from leanfaith.annotation_support.export import ArtifactBinding
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import require_utc

_PIN_ID = r"^lf023_argilla_backend_pin_v1:[0-9a-f]{64}$"
_CAPTURE_MANIFEST_ID = r"^lf023_argilla_capture_manifest_v1:[0-9a-f]{64}$"


class ArgillaCliInputError(ValueError):
    """Raised when an LF-023 Argilla operator input is unsafe or incoherent."""


class ArgillaExpectedResponseManifestV1(StrictModel):
    """Label-free response identities authorized for one pinned direct fetch."""

    schema_version: Literal[1] = 1
    manifest_kind: Literal["lf023_argilla_expected_responses_v1"]
    backend_pin_id: str = Field(pattern=_PIN_ID)
    expected_responses: tuple[ArgillaExpectedResponseV1, ...]
    semantic_labels_included: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    training_eligible: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def _exact_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError(
                "Argilla expected-response manifest schema_version must be the JSON integer 1"
            )
        return value

    @field_validator(
        "semantic_labels_included",
        "human_gold_eligible",
        "training_eligible",
        mode="before",
    )
    @classmethod
    def _exact_false_flags(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Argilla expected-response safety flags must be JSON boolean false")
        return value

    @model_validator(mode="after")
    def _nonempty_unique_identities(self) -> Self:
        if not self.expected_responses:
            raise ValueError("Argilla expected-response manifest must be nonempty")
        for field in (
            "backend_record_id",
            "backend_response_id",
            "backend_submission_id",
        ):
            values = [getattr(item, field) for item in self.expected_responses]
            if len(values) != len(set(values)):
                raise ValueError(f"Argilla expected-response manifest duplicates {field}")
        return self


class ArgillaCaptureEntryV1(StrictModel):
    """Ordered membership for one response and its exact retained artifacts."""

    expected_response: ArgillaExpectedResponseV1
    receipt: ArtifactBinding
    raw_dataset_payload: ArtifactBinding
    raw_record_payload: ArtifactBinding


class ArgillaCaptureManifestContentV1(StrictModel):
    """Normalized content of one private direct-capture batch manifest."""

    schema_version: Literal[1] = 1
    manifest_kind: Literal["lf023_argilla_capture_manifest_v1"]
    backend_pin_id: str = Field(pattern=_PIN_ID)
    backend_pin: ArtifactBinding
    expected_response_manifest: ArtifactBinding
    captured_at: datetime.datetime
    entries: tuple[ArgillaCaptureEntryV1, ...]
    entry_count: int = Field(ge=1)
    submitted_snapshot_only: Literal[True] = True
    backend_immutability_verified: Literal[False] = False
    project_logical_lock_included: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gold_labels_created: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    private: Literal[True] = True
    release_eligible: Literal[False] = False

    @field_validator("schema_version", "entry_count", mode="before")
    @classmethod
    def _exact_integer_fields(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Argilla capture integer fields require JSON integers")
        return value

    @field_validator(
        "submitted_snapshot_only",
        "private",
        mode="before",
    )
    @classmethod
    def _exact_true_flags(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Argilla capture true flags require JSON boolean true")
        return value

    @field_validator(
        "backend_immutability_verified",
        "project_logical_lock_included",
        "semantic_labels_created",
        "gold_labels_created",
        "human_gold_eligible",
        "training_eligible",
        "release_eligible",
        mode="before",
    )
    @classmethod
    def _exact_false_flags(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Argilla capture false flags require JSON boolean false")
        return value

    @model_validator(mode="after")
    def _coherent_entries(self) -> Self:
        require_utc(self.captured_at)
        if self.entry_count != len(self.entries):
            raise ValueError("Argilla capture entry_count differs from ordered entries")
        identities = tuple(
            (
                item.expected_response.backend_record_id,
                item.expected_response.backend_response_id,
                item.expected_response.backend_submission_id,
            )
            for item in self.entries
        )
        if len(set(identities)) != len(identities):
            raise ValueError("Argilla capture entries contain duplicate response identities")
        return self


class ArgillaCaptureManifestV1(ArgillaCaptureManifestContentV1):
    """Private content-addressed binding for one direct-capture batch."""

    manifest_id: str = Field(pattern=_CAPTURE_MANIFEST_ID)

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"manifest_id"})
        expected = "lf023_argilla_capture_manifest_v1:" + hash_canonical(
            {"schema": "lf023_argilla_capture_manifest_v1", **payload}
        )
        if self.manifest_id != expected:
            raise ValueError("Argilla capture manifest ID differs from normalized content")
        return self


@dataclass(frozen=True, slots=True)
class ArgillaPinWriteResult:
    """One idempotently retained content-addressed backend pin."""

    pin: ArgillaBackendPinV1
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class ArgillaCaptureResult:
    """Backend-origin snapshots plus non-secret input bindings."""

    run: ArgillaDirectFetchRun
    pin_path: Path
    pin_sha256: str
    expected_manifest_path: Path
    expected_manifest_sha256: str
    output_root: Path
    manifest: ArgillaCaptureManifestV1
    manifest_path: Path


def _absolute_without_resolve(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _strict_json_object(raw: bytes, *, owner: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ArgillaCliInputError(f"{owner} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> object:
        raise ArgillaCliInputError(f"{owner} contains non-finite JSON value {value!r}")

    try:
        value: object = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArgillaCliInputError(f"{owner} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ArgillaCliInputError(f"{owner} must contain one JSON object")
    return cast(dict[str, object], value)


def _read_operator_artifact(path: Path, *, owner: str) -> tuple[Path, bytes]:
    try:
        return read_argilla_regular_file_nofollow(path)
    except ArgillaBackendError:
        raise ArgillaCliInputError(f"{owner} cannot be read as a stable regular file") from None


def _write_private_idempotent(path: Path, payload: bytes) -> None:
    try:
        write_argilla_private_immutable(path, payload)
    except ArgillaBackendError as exc:
        message = str(exc)
        if "differs" in message:
            message = f"content-addressed Argilla pin path contains divergent bytes: {path}"
        raise ArgillaCliInputError(message) from None


def write_argilla_backend_pin(
    *,
    endpoint: str,
    workspace_id: str,
    dataset_id: str,
    annotator_id: str,
    api_key_env: str,
    output_dir: Path,
) -> ArgillaPinWriteResult:
    """Create and retain a pin containing only identities and a secret reference."""

    pin = make_argilla_backend_pin(
        endpoint=endpoint,
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        annotator_id=annotator_id,
        api_key_env=api_key_env,
    )
    payload = canonical_json_bytes(pin.model_dump(mode="json")) + b"\n"
    digest = pin.pin_id.rsplit(":", 1)[-1]
    directory = _absolute_without_resolve(output_dir)
    path = directory / f"{digest}.json"
    _write_private_idempotent(path, payload)
    return ArgillaPinWriteResult(pin=pin, path=path, sha256=sha256_hex(payload))


def _load_pin(path: Path) -> tuple[Path, bytes, ArgillaBackendPinV1]:
    absolute, raw = _read_operator_artifact(path, owner="Argilla backend pin")
    try:
        pin = ArgillaBackendPinV1.model_validate(
            _strict_json_object(raw, owner="Argilla backend pin")
        )
    except ValidationError:
        raise ArgillaCliInputError("Argilla backend pin failed strict schema validation") from None
    return absolute, raw, pin


def _load_expected_manifest(
    path: Path,
) -> tuple[Path, bytes, ArgillaExpectedResponseManifestV1]:
    absolute, raw = _read_operator_artifact(path, owner="Argilla expected-response manifest")
    try:
        manifest = ArgillaExpectedResponseManifestV1.model_validate(
            _strict_json_object(raw, owner="Argilla expected-response manifest")
        )
    except ValidationError:
        raise ArgillaCliInputError(
            "Argilla expected-response manifest failed strict schema validation"
        ) from None
    return absolute, raw, manifest


def _retained_binding(*, output_root: Path, path: Path) -> ArtifactBinding:
    absolute, raw = read_argilla_regular_file_nofollow(path)
    try:
        relative = absolute.relative_to(output_root)
    except ValueError:
        raise ArgillaCliInputError(
            "Argilla capture artifact escaped the private output root"
        ) from None
    return ArtifactBinding(artifact=relative.as_posix(), sha256=sha256_hex(raw))


def _persist_capture_manifest(
    *,
    pin: ArgillaBackendPinV1,
    pin_raw: bytes,
    expected_manifest: ArgillaExpectedResponseManifestV1,
    expected_manifest_raw: bytes,
    run: ArgillaDirectFetchRun,
    output_root: Path,
) -> tuple[ArgillaCaptureManifestV1, Path]:
    count = len(expected_manifest.expected_responses)
    memberships = (
        len(run.receipts),
        len(run.receipt_paths),
        len(run.raw_dataset_payload_paths),
        len(run.raw_record_payload_paths),
    )
    if any(item != count for item in memberships):
        raise ArgillaCliInputError("Argilla direct-fetch membership differs from expectation")

    pin_sha256 = sha256_hex(pin_raw)
    expected_sha256 = sha256_hex(expected_manifest_raw)
    retained_pin_path = output_root / "inputs" / "backend_pins" / f"{pin_sha256}.json"
    retained_expected_path = (
        output_root / "inputs" / "expected_response_manifests" / f"{expected_sha256}.json"
    )
    write_argilla_private_immutable(retained_pin_path, pin_raw)
    write_argilla_private_immutable(retained_expected_path, expected_manifest_raw)

    entries: list[ArgillaCaptureEntryV1] = []
    captured_at: datetime.datetime | None = None
    for index, expected in enumerate(expected_manifest.expected_responses):
        receipt = run.receipts[index]
        if (
            receipt.backend_pin_id != pin.pin_id
            or receipt.backend_record_id != expected.backend_record_id
            or receipt.backend_response_id != expected.backend_response_id
            or receipt.backend_submission_id != expected.backend_submission_id
            or not receipt.backend_origin_transport_verified
            or receipt.fixture_only
        ):
            raise ArgillaCliInputError(
                "Argilla direct-fetch receipt differs from pinned ordered membership"
            )
        if captured_at is None:
            captured_at = receipt.fetched_at
        elif receipt.fetched_at != captured_at:
            raise ArgillaCliInputError("Argilla direct-fetch receipts use different timestamps")
        entries.append(
            ArgillaCaptureEntryV1(
                expected_response=expected,
                receipt=_retained_binding(
                    output_root=output_root,
                    path=run.receipt_paths[index],
                ),
                raw_dataset_payload=_retained_binding(
                    output_root=output_root,
                    path=run.raw_dataset_payload_paths[index],
                ),
                raw_record_payload=_retained_binding(
                    output_root=output_root,
                    path=run.raw_record_payload_paths[index],
                ),
            )
        )
    assert captured_at is not None
    content = ArgillaCaptureManifestContentV1(
        manifest_kind="lf023_argilla_capture_manifest_v1",
        backend_pin_id=pin.pin_id,
        backend_pin=_retained_binding(output_root=output_root, path=retained_pin_path),
        expected_response_manifest=_retained_binding(
            output_root=output_root,
            path=retained_expected_path,
        ),
        captured_at=captured_at,
        entries=tuple(entries),
        entry_count=count,
    )
    manifest_id = "lf023_argilla_capture_manifest_v1:" + hash_canonical(
        {
            "schema": "lf023_argilla_capture_manifest_v1",
            **content.model_dump(mode="json"),
        }
    )
    manifest = ArgillaCaptureManifestV1.model_validate(
        {"manifest_id": manifest_id, **content.model_dump(mode="json")}
    )
    manifest_path = output_root / "manifests" / f"{manifest_id.rsplit(':', 1)[-1]}.json"
    write_argilla_private_immutable(
        manifest_path,
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    )
    return manifest, manifest_path


def capture_argilla_submitted_responses(
    *,
    pin_path: Path,
    expected_manifest_path: Path,
    output_root: Path,
    fetched_at: datetime.datetime | None = None,
) -> ArgillaCaptureResult:
    """Fetch submitted snapshots using only the API key named by the pin."""

    resolved_pin_path, pin_raw, pin = _load_pin(pin_path)
    resolved_manifest_path, manifest_raw, expected_manifest = _load_expected_manifest(
        expected_manifest_path
    )
    if expected_manifest.backend_pin_id != pin.pin_id:
        raise ArgillaCliInputError("expected-response manifest belongs to a different backend pin")
    api_key = os.environ.get(pin.api_key_env)
    if api_key is None:
        raise ArgillaCliInputError(
            f"required Argilla API-key environment variable is unset: {pin.api_key_env}"
        )
    if not api_key:
        raise ArgillaCliInputError(
            f"required Argilla API-key environment variable is empty: {pin.api_key_env}"
        )
    resolved_output_root = _absolute_without_resolve(output_root)
    run = fetch_argilla_responses(
        pin=pin,
        expected_responses=expected_manifest.expected_responses,
        transport=ArgillaV28RestTransport(),
        api_key=api_key,
        output_root=resolved_output_root,
        fetched_at=fetched_at or datetime.datetime.now(tz=datetime.UTC),
        artifact_class="backend_origin",
    )
    manifest, manifest_path = _persist_capture_manifest(
        pin=pin,
        pin_raw=pin_raw,
        expected_manifest=expected_manifest,
        expected_manifest_raw=manifest_raw,
        run=run,
        output_root=resolved_output_root,
    )
    return ArgillaCaptureResult(
        run=run,
        pin_path=resolved_pin_path,
        pin_sha256=sha256_hex(pin_raw),
        expected_manifest_path=resolved_manifest_path,
        expected_manifest_sha256=sha256_hex(manifest_raw),
        output_root=resolved_output_root,
        manifest=manifest,
        manifest_path=manifest_path,
    )


__all__ = [
    "ArgillaCaptureManifestV1",
    "ArgillaCaptureResult",
    "ArgillaCliInputError",
    "ArgillaExpectedResponseManifestV1",
    "ArgillaPinWriteResult",
    "capture_argilla_submitted_responses",
    "write_argilla_backend_pin",
]
