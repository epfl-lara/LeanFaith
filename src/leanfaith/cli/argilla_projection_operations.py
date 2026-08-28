"""Private LF-023 persistence for Argilla record bindings and raw vote projections.

This module is an operator boundary around the pure
``annotation_support.argilla_projection`` code.  It deliberately creates only
content-addressed, project-owned snapshots of raw responses.  It does not
verify assignment HMACs, establish a human identity, adjudicate, resolve a
semantic label, or make an artifact training eligible.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import Field, ValidationError, field_validator, model_validator

from leanfaith.annotation_support.argilla_backend import (
    ArgillaBackendError,
    ArgillaBackendPinV1,
    ArgillaDirectFetchReceiptV1,
    read_argilla_regular_file_nofollow,
    write_argilla_private_immutable,
)
from leanfaith.annotation_support.argilla_projection import (
    ArgillaProjectionBindingManifestV1,
    ArgillaProjectionManifestV1,
    ArgillaProjectionRun,
    ArgillaRecordItemBindingV1,
    make_argilla_projection_binding_manifest,
    project_captured_argilla_responses,
)
from leanfaith.annotation_support.attestation import HumanAnnotationAssignmentEnvelopeV1
from leanfaith.annotation_support.export import (
    ArtifactBinding,
    BlindedAnnotationItemV1,
    BlindedBundleManifestV1,
)
from leanfaith.annotation_support.import_ import LockedAnnotationResponseEnvelopeV1
from leanfaith.cli.argilla_operations import (
    ArgillaCaptureManifestV1,
    ArgillaExpectedResponseManifestV1,
)
from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.config.models import StrictModel

_ASSIGNMENT_ID = r"^lf023_human_assignment_v1:[0-9a-f]{64}$"
_PIN_ID = r"^lf023_argilla_backend_pin_v1:[0-9a-f]{64}$"


class ArgillaProjectionOperationError(ValueError):
    """Raised when private Argilla projection lineage cannot be verified exactly."""


class ArgillaRecordAllocationInputV1(StrictModel):
    """Strict label-free operator input for a pre-response record allocation."""

    schema_version: Literal[1] = 1
    mapping_kind: Literal["lf023_argilla_record_item_mapping_v1"]
    assignment_id: str = Field(pattern=_ASSIGNMENT_ID)
    backend_pin_id: str = Field(pattern=_PIN_ID)
    item_bindings: tuple[ArgillaRecordItemBindingV1, ...]
    record_allocation_only: Literal[True] = True
    response_values_included: Literal[False] = False
    semantic_labels_included: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    training_eligible: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def _exact_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("Argilla record allocation schema_version must be JSON integer 1")
        return value

    @field_validator("record_allocation_only", mode="before")
    @classmethod
    def _exact_true_flag(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Argilla record allocation record_allocation_only must be true")
        return value

    @field_validator(
        "response_values_included",
        "semantic_labels_included",
        "human_gold_eligible",
        "training_eligible",
        mode="before",
    )
    @classmethod
    def _exact_false_flags(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Argilla record allocation safety flags must be false")
        return value

    @model_validator(mode="after")
    def _complete_sorted_unique_mapping(self) -> Self:
        if len(self.item_bindings) != 240:
            raise ValueError("Argilla record allocation must contain exactly 240 items")
        tokens = tuple(item.opaque_item_token for item in self.item_bindings)
        records = tuple(item.backend_record_id for item in self.item_bindings)
        if len(set(tokens)) != 240:
            raise ValueError("Argilla record allocation contains duplicate item tokens")
        if len(set(records)) != 240:
            raise ValueError("Argilla record allocation contains duplicate backend records")
        if tokens != tuple(sorted(tokens)):
            raise ValueError("Argilla record allocation must be sorted by item token")
        return self


@dataclass(frozen=True, slots=True)
class ArgillaProjectionBindingWriteResult:
    """One reloaded private content-addressed pre-response binding."""

    manifest: ArgillaProjectionBindingManifestV1
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class PersistedArgillaProjectionRun:
    """One reloaded private raw-response projection and its exact lineage."""

    run: ArgillaProjectionRun
    capture_manifest: ArgillaCaptureManifestV1
    binding_manifest: ArgillaProjectionBindingManifestV1
    locked_responses_path: Path
    manifest_path: Path


def _absolute_without_resolve(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _strict_json_object(raw: bytes, *, owner: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ArgillaProjectionOperationError(
                    f"{owner} contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> object:
        raise ArgillaProjectionOperationError(f"{owner} contains non-finite JSON value {value!r}")

    try:
        value: object = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArgillaProjectionOperationError(f"{owner} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ArgillaProjectionOperationError(f"{owner} must contain one JSON object")
    return cast(dict[str, object], value)


def _required_string(value: dict[str, object], field: str, *, owner: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise ArgillaProjectionOperationError(f"{owner} requires nonempty string {field!r}")
    return item


def _require_same_uuid(actual: str, expected: str, *, owner: str) -> None:
    try:
        matches = uuid.UUID(actual) == uuid.UUID(expected)
    except ValueError:
        raise ArgillaProjectionOperationError(f"{owner} is not a UUID") from None
    if not matches:
        raise ArgillaProjectionOperationError(f"{owner} differs from backend pin")


def _require_private_mode(path: Path, *, owner: str) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ArgillaProjectionOperationError(f"{owner} metadata cannot be read") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ArgillaProjectionOperationError(f"{owner} is not a regular file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ArgillaProjectionOperationError(f"{owner} is not mode-0600 private")


def _read_private(path: Path, *, owner: str) -> tuple[Path, bytes]:
    try:
        absolute, raw = read_argilla_regular_file_nofollow(path)
    except ArgillaBackendError:
        raise ArgillaProjectionOperationError(
            f"{owner} cannot be read as a stable regular file"
        ) from None
    _require_private_mode(absolute, owner=owner)
    return absolute, raw


def _read_public(path: Path, *, owner: str) -> tuple[Path, bytes]:
    try:
        return read_argilla_regular_file_nofollow(path)
    except ArgillaBackendError:
        raise ArgillaProjectionOperationError(
            f"{owner} cannot be read as a stable regular file"
        ) from None


def _repo_bound_path(
    *,
    repo_root: Path,
    binding: ArtifactBinding,
    owner: str,
) -> Path:
    root = _absolute_without_resolve(repo_root)
    relative = PurePosixPath(binding.artifact)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in binding.artifact
        or relative.as_posix() != binding.artifact
    ):
        raise ArgillaProjectionOperationError(
            f"{owner} binding must be a normalized repository-relative POSIX path"
        )
    return root.joinpath(*relative.parts)


def _load_exact_public_bundle(
    *,
    repo_root: Path,
    public_bundle_manifest_path: Path,
    assignment: HumanAnnotationAssignmentEnvelopeV1,
) -> tuple[BlindedBundleManifestV1, tuple[BlindedAnnotationItemV1, ...]]:
    expected_manifest_path = _repo_bound_path(
        repo_root=repo_root,
        binding=assignment.public_bundle_manifest,
        owner="public bundle manifest",
    )
    supplied_manifest_path = _absolute_without_resolve(public_bundle_manifest_path)
    if supplied_manifest_path != expected_manifest_path:
        raise ArgillaProjectionOperationError(
            "public bundle manifest path differs from the human assignment binding"
        )
    manifest_path, manifest_raw = _read_public(
        supplied_manifest_path,
        owner="public bundle manifest",
    )
    if sha256_hex(manifest_raw) != assignment.public_bundle_manifest.sha256:
        raise ArgillaProjectionOperationError(
            "public bundle manifest hash differs from the human assignment binding"
        )
    try:
        manifest = BlindedBundleManifestV1.model_validate(
            _strict_json_object(manifest_raw, owner="public bundle manifest")
        )
    except ValidationError:
        raise ArgillaProjectionOperationError(
            "public bundle manifest failed strict schema validation"
        ) from None
    if manifest_raw != canonical_json_bytes(manifest.model_dump(mode="json")):
        raise ArgillaProjectionOperationError("public bundle manifest bytes are not canonical")
    _require_content_addressed_filename(
        manifest_path,
        manifest.manifest_id,
        owner="public bundle manifest",
    )
    if (
        manifest.manifest_id != assignment.bundle_manifest_id
        or manifest.annotator_slot != assignment.annotator_slot
    ):
        raise ArgillaProjectionOperationError(
            "public bundle manifest differs from the human assignment"
        )

    bundle_path = _repo_bound_path(
        repo_root=repo_root,
        binding=manifest.bundle,
        owner="public blinded bundle",
    )
    _, bundle_raw = _read_public(bundle_path, owner="public blinded bundle")
    if sha256_hex(bundle_raw) != manifest.bundle.sha256:
        raise ArgillaProjectionOperationError(
            "public blinded bundle hash differs from its manifest"
        )
    if bundle_path.name != f"{manifest.bundle.sha256}.jsonl":
        raise ArgillaProjectionOperationError(
            "public blinded bundle filename differs from its content address"
        )
    if manifest.bundle_id != f"lf023_blinded_bundle_v1:{manifest.bundle.sha256}":
        raise ArgillaProjectionOperationError(
            "public blinded bundle ID differs from its exact bytes"
        )
    if not bundle_raw or not bundle_raw.endswith(b"\n"):
        raise ArgillaProjectionOperationError(
            "public blinded bundle must be nonempty newline-terminated JSONL"
        )
    items: list[BlindedAnnotationItemV1] = []
    for line_number, line in enumerate(bundle_raw.splitlines(), start=1):
        if not line:
            raise ArgillaProjectionOperationError(
                "public blinded bundle contains a blank JSONL row"
            )
        try:
            item = BlindedAnnotationItemV1.model_validate(
                _strict_json_object(
                    line,
                    owner=f"public blinded bundle row {line_number}",
                )
            )
        except ValidationError:
            raise ArgillaProjectionOperationError(
                f"public blinded bundle row {line_number} failed strict schema validation"
            ) from None
        if line != canonical_json_bytes(item.model_dump(mode="json")):
            raise ArgillaProjectionOperationError(
                f"public blinded bundle row {line_number} is not canonical"
            )
        items.append(item)
    if len(items) != manifest.item_count:
        raise ArgillaProjectionOperationError(
            "public blinded bundle item count differs from its manifest"
        )
    tokens = tuple(item.opaque_item_token for item in items)
    if len(set(tokens)) != len(tokens):
        raise ArgillaProjectionOperationError(
            "public blinded bundle contains duplicate opaque item tokens"
        )
    return manifest, tuple(items)


def _load_private_model[T: StrictModel](
    path: Path,
    *,
    owner: str,
    model_type: type[T],
) -> tuple[Path, bytes, T]:
    absolute, raw = _read_private(path, owner=owner)
    try:
        model = model_type.model_validate(_strict_json_object(raw, owner=owner))
    except ValidationError:
        raise ArgillaProjectionOperationError(f"{owner} failed strict schema validation") from None
    return absolute, raw, model


def _canonical_model_bytes(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _write_private(path: Path, payload: bytes) -> None:
    try:
        write_argilla_private_immutable(path, payload)
    except ArgillaBackendError as exc:
        raise ArgillaProjectionOperationError(str(exc)) from None


def _require_canonical_bytes(raw: bytes, model: StrictModel, *, owner: str) -> None:
    if raw != _canonical_model_bytes(model):
        raise ArgillaProjectionOperationError(f"{owner} bytes are not canonical")


def _require_content_addressed_filename(
    path: Path,
    identifier: str,
    *,
    owner: str,
) -> None:
    digest = identifier.rsplit(":", 1)[-1]
    if path.name != f"{digest}.json":
        raise ArgillaProjectionOperationError(
            f"{owner} filename does not match its content address"
        )


def load_argilla_projection_binding(
    path: Path,
) -> tuple[Path, bytes, ArgillaProjectionBindingManifestV1]:
    """Load and reverify one private canonical pre-response binding."""

    absolute, raw, manifest = _load_private_model(
        path,
        owner="Argilla projection binding",
        model_type=ArgillaProjectionBindingManifestV1,
    )
    _require_canonical_bytes(raw, manifest, owner="Argilla projection binding")
    _require_content_addressed_filename(
        absolute,
        manifest.manifest_id,
        owner="Argilla projection binding",
    )
    return absolute, raw, manifest


def write_argilla_projection_binding(
    *,
    repo_root: Path,
    assignment_path: Path,
    public_bundle_manifest_path: Path,
    pin_path: Path,
    mapping_path: Path,
    output_root: Path,
) -> ArgillaProjectionBindingWriteResult:
    """Persist a label-free record allocation before observing any response."""

    _, _, assignment = _load_private_model(
        assignment_path,
        owner="Argilla human assignment",
        model_type=HumanAnnotationAssignmentEnvelopeV1,
    )
    _, _, pin = _load_private_model(
        pin_path,
        owner="Argilla backend pin",
        model_type=ArgillaBackendPinV1,
    )
    _, _, mapping = _load_private_model(
        mapping_path,
        owner="Argilla record allocation",
        model_type=ArgillaRecordAllocationInputV1,
    )
    _, bundle_items = _load_exact_public_bundle(
        repo_root=repo_root,
        public_bundle_manifest_path=public_bundle_manifest_path,
        assignment=assignment,
    )
    if mapping.assignment_id != assignment.assignment_id:
        raise ArgillaProjectionOperationError(
            "Argilla record allocation belongs to a different assignment"
        )
    if mapping.backend_pin_id != pin.pin_id:
        raise ArgillaProjectionOperationError(
            "Argilla record allocation belongs to a different backend pin"
        )
    mapped_tokens = {item.opaque_item_token for item in mapping.item_bindings}
    bundle_tokens = {item.opaque_item_token for item in bundle_items}
    if mapped_tokens != bundle_tokens:
        raise ArgillaProjectionOperationError(
            "Argilla record allocation token set differs from the exact public blinded bundle"
        )
    try:
        manifest = make_argilla_projection_binding_manifest(
            assignment=assignment,
            pin=pin,
            item_bindings=mapping.item_bindings,
            public_bundle_tokens=frozenset(bundle_tokens),
        )
    except ValueError as exc:
        raise ArgillaProjectionOperationError(
            f"Argilla projection binding rejected: {exc}"
        ) from exc

    output = _absolute_without_resolve(output_root)
    path = output / "bindings" / f"{manifest.manifest_id.rsplit(':', 1)[-1]}.json"
    payload = _canonical_model_bytes(manifest)
    _write_private(path, payload)
    restored_path, restored_raw, restored = load_argilla_projection_binding(path)
    if restored != manifest or restored_raw != payload:
        raise ArgillaProjectionOperationError(
            "persisted Argilla projection binding differs after reload"
        )
    return ArgillaProjectionBindingWriteResult(
        manifest=restored,
        path=restored_path,
        sha256=sha256_hex(restored_raw),
    )


def _safe_capture_member(
    *,
    capture_root: Path,
    binding: ArtifactBinding,
    owner: str,
) -> tuple[Path, bytes]:
    relative = PurePosixPath(binding.artifact)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ArgillaProjectionOperationError(f"{owner} uses an unsafe artifact path")
    path = capture_root.joinpath(*relative.parts)
    absolute, raw = _read_private(path, owner=owner)
    try:
        absolute.relative_to(capture_root)
    except ValueError:
        raise ArgillaProjectionOperationError(f"{owner} escaped the capture root") from None
    if sha256_hex(raw) != binding.sha256:
        raise ArgillaProjectionOperationError(f"{owner} hash differs from capture manifest")
    return absolute, raw


def _load_capture_manifest(
    *,
    capture_root: Path,
    manifest_path: Path,
) -> tuple[Path, bytes, ArgillaCaptureManifestV1]:
    root = _absolute_without_resolve(capture_root)
    try:
        metadata = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise ArgillaProjectionOperationError("Argilla capture root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ArgillaProjectionOperationError("Argilla capture root is not a private directory")
    expected_prefix = root / "manifests"
    absolute, raw, manifest = _load_private_model(
        manifest_path,
        owner="Argilla capture manifest",
        model_type=ArgillaCaptureManifestV1,
    )
    try:
        relative = absolute.relative_to(expected_prefix)
    except ValueError:
        raise ArgillaProjectionOperationError(
            "Argilla capture manifest is outside the capture manifest directory"
        ) from None
    if len(relative.parts) != 1:
        raise ArgillaProjectionOperationError("Argilla capture manifest path is not canonical")
    _require_canonical_bytes(raw, manifest, owner="Argilla capture manifest")
    _require_content_addressed_filename(
        absolute,
        manifest.manifest_id,
        owner="Argilla capture manifest",
    )
    return absolute, raw, manifest


def _load_locked_responses_jsonl(
    raw: bytes,
) -> tuple[LockedAnnotationResponseEnvelopeV1, ...]:
    """Validate canonical JSONL without exposing a general-purpose parser."""

    if not raw or not raw.endswith(b"\n"):
        raise ArgillaProjectionOperationError(
            "persisted Argilla locked responses must be nonempty newline-terminated JSONL"
        )
    responses: list[LockedAnnotationResponseEnvelopeV1] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise ArgillaProjectionOperationError(
                f"persisted Argilla locked responses contain an empty line {line_number}"
            )
        try:
            response = LockedAnnotationResponseEnvelopeV1.model_validate(
                _strict_json_object(
                    line,
                    owner=f"Argilla locked response line {line_number}",
                )
            )
        except ValidationError:
            raise ArgillaProjectionOperationError(
                f"Argilla locked response line {line_number} failed strict schema validation"
            ) from None
        if line != canonical_json_bytes(response.model_dump(mode="json")):
            raise ArgillaProjectionOperationError(
                f"Argilla locked response line {line_number} is not canonical"
            )
        responses.append(response)
    return tuple(responses)


def _capture_projection_inputs(
    *,
    capture_root: Path,
    capture_manifest: ArgillaCaptureManifestV1,
    pin: ArgillaBackendPinV1,
) -> tuple[tuple[ArgillaDirectFetchReceiptV1, ...], dict[str, bytes]]:
    captured_pin_path, captured_pin_raw = _safe_capture_member(
        capture_root=capture_root,
        binding=capture_manifest.backend_pin,
        owner="captured Argilla backend pin",
    )
    try:
        captured_pin = ArgillaBackendPinV1.model_validate(
            _strict_json_object(captured_pin_raw, owner="captured Argilla backend pin")
        )
    except ValidationError:
        raise ArgillaProjectionOperationError(
            "captured Argilla backend pin failed strict schema validation"
        ) from None
    if captured_pin != pin or capture_manifest.backend_pin_id != pin.pin_id:
        raise ArgillaProjectionOperationError(
            "Argilla capture manifest belongs to a different backend pin"
        )
    del captured_pin_path

    _, expected_raw = _safe_capture_member(
        capture_root=capture_root,
        binding=capture_manifest.expected_response_manifest,
        owner="captured Argilla expected-response manifest",
    )
    try:
        expected_manifest = ArgillaExpectedResponseManifestV1.model_validate(
            _strict_json_object(
                expected_raw,
                owner="captured Argilla expected-response manifest",
            )
        )
    except ValidationError:
        raise ArgillaProjectionOperationError(
            "captured Argilla expected-response manifest failed strict schema validation"
        ) from None
    if expected_manifest.backend_pin_id != pin.pin_id:
        raise ArgillaProjectionOperationError(
            "captured expected-response manifest belongs to a different backend pin"
        )
    if expected_manifest.expected_responses != tuple(
        entry.expected_response for entry in capture_manifest.entries
    ):
        raise ArgillaProjectionOperationError(
            "captured expected-response membership differs from capture manifest"
        )

    receipts: list[ArgillaDirectFetchReceiptV1] = []
    raw_records: dict[str, bytes] = {}
    for entry in capture_manifest.entries:
        _, receipt_raw = _safe_capture_member(
            capture_root=capture_root,
            binding=entry.receipt,
            owner="captured Argilla receipt",
        )
        _, raw_dataset = _safe_capture_member(
            capture_root=capture_root,
            binding=entry.raw_dataset_payload,
            owner="captured Argilla raw dataset payload",
        )
        _, raw_record = _safe_capture_member(
            capture_root=capture_root,
            binding=entry.raw_record_payload,
            owner="captured Argilla raw record payload",
        )
        dataset = _strict_json_object(
            raw_dataset,
            owner="captured Argilla raw dataset payload",
        )
        _require_same_uuid(
            _required_string(
                dataset,
                "id",
                owner="captured Argilla raw dataset payload",
            ),
            pin.dataset_id,
            owner="captured Argilla dataset ID",
        )
        _require_same_uuid(
            _required_string(
                dataset,
                "workspace_id",
                owner="captured Argilla raw dataset payload",
            ),
            pin.workspace_id,
            owner="captured Argilla workspace ID",
        )
        try:
            receipt = ArgillaDirectFetchReceiptV1.model_validate(
                _strict_json_object(receipt_raw, owner="captured Argilla receipt")
            )
        except ValidationError:
            raise ArgillaProjectionOperationError(
                "captured Argilla receipt failed strict schema validation"
            ) from None
        expected = entry.expected_response
        if (
            receipt.backend_pin_id != pin.pin_id
            or receipt.backend_record_id != expected.backend_record_id
            or receipt.backend_response_id != expected.backend_response_id
            or receipt.backend_submission_id != expected.backend_submission_id
            or receipt.raw_dataset_payload != entry.raw_dataset_payload
            or receipt.raw_record_payload != entry.raw_record_payload
            or receipt.raw_dataset_payload_size != len(raw_dataset)
            or receipt.raw_record_payload_size != len(raw_record)
            or not receipt.backend_origin_transport_verified
            or receipt.fixture_only
        ):
            raise ArgillaProjectionOperationError(
                "captured Argilla receipt differs from exact capture lineage"
            )
        receipts.append(receipt)
        raw_records[receipt.receipt_id] = raw_record
    if len(receipts) != capture_manifest.entry_count or len(raw_records) != len(receipts):
        raise ArgillaProjectionOperationError(
            "captured Argilla receipt membership is incomplete or duplicated"
        )
    return tuple(receipts), raw_records


def load_persisted_argilla_projection(
    *,
    manifest_path: Path,
    locked_responses_path: Path,
) -> ArgillaProjectionRun:
    """Reload the exact content-addressed raw-vote projection outputs."""

    manifest_absolute, manifest_raw, manifest = _load_private_model(
        manifest_path,
        owner="Argilla projection manifest",
        model_type=ArgillaProjectionManifestV1,
    )
    _require_canonical_bytes(manifest_raw, manifest, owner="Argilla projection manifest")
    _require_content_addressed_filename(
        manifest_absolute,
        manifest.manifest_id,
        owner="Argilla projection manifest",
    )
    if manifest.capture_manifest_id is None:
        raise ArgillaProjectionOperationError(
            "persisted Argilla projection omits backend capture lineage"
        )
    locked_absolute, locked_raw = _read_private(
        locked_responses_path,
        owner="Argilla locked responses",
    )
    if locked_absolute.name != f"{manifest.locked_responses_sha256}.jsonl":
        raise ArgillaProjectionOperationError(
            "Argilla locked-response filename differs from its content address"
        )
    if sha256_hex(locked_raw) != manifest.locked_responses_sha256:
        raise ArgillaProjectionOperationError(
            "Argilla locked-response hash differs from projection manifest"
        )
    responses = _load_locked_responses_jsonl(locked_raw)
    response_ids = tuple(response.response_id for response in responses)
    if response_ids != manifest.locked_response_ids:
        raise ArgillaProjectionOperationError(
            "Argilla locked-response membership differs from projection manifest"
        )
    return ArgillaProjectionRun(
        manifest=manifest,
        responses=responses,
        locked_responses_jsonl=locked_raw,
    )


def project_and_persist_argilla_capture(
    *,
    repo_root: Path,
    assignment_path: Path,
    pin_path: Path,
    binding_manifest_path: Path,
    capture_root: Path,
    capture_manifest_path: Path,
    output_root: Path,
) -> PersistedArgillaProjectionRun:
    """Reverify a backend capture, project raw votes, and persist exact outputs."""

    _, _, assignment = _load_private_model(
        assignment_path,
        owner="Argilla human assignment",
        model_type=HumanAnnotationAssignmentEnvelopeV1,
    )
    _, _, pin = _load_private_model(
        pin_path,
        owner="Argilla backend pin",
        model_type=ArgillaBackendPinV1,
    )
    _, _, binding = load_argilla_projection_binding(binding_manifest_path)
    expected_public_manifest_path = _repo_bound_path(
        repo_root=repo_root,
        binding=assignment.public_bundle_manifest,
        owner="public bundle manifest",
    )
    _, bundle_items = _load_exact_public_bundle(
        repo_root=repo_root,
        public_bundle_manifest_path=expected_public_manifest_path,
        assignment=assignment,
    )
    if {item.opaque_item_token for item in binding.item_bindings} != {
        item.opaque_item_token for item in bundle_items
    }:
        raise ArgillaProjectionOperationError(
            "Argilla projection binding token set differs from the exact public blinded bundle"
        )
    resolved_capture_root = _absolute_without_resolve(capture_root)
    _, _, capture = _load_capture_manifest(
        capture_root=resolved_capture_root,
        manifest_path=capture_manifest_path,
    )
    if binding.assignment_id != assignment.assignment_id:
        raise ArgillaProjectionOperationError(
            "Argilla projection binding belongs to a different assignment"
        )
    if binding.backend_pin_id != pin.pin_id:
        raise ArgillaProjectionOperationError(
            "Argilla projection binding belongs to a different backend pin"
        )
    receipts, raw_records = _capture_projection_inputs(
        capture_root=resolved_capture_root,
        capture_manifest=capture,
        pin=pin,
    )
    try:
        projected = project_captured_argilla_responses(
            assignment=assignment,
            pin=pin,
            binding_manifest=binding,
            receipts=receipts,
            raw_record_payloads=raw_records,
            capture_manifest_id=capture.manifest_id,
        )
    except ValueError as exc:
        raise ArgillaProjectionOperationError(
            f"Argilla response projection rejected: {exc}"
        ) from exc

    output = _absolute_without_resolve(output_root)
    locked_path = (
        output / "locked_responses" / f"{projected.manifest.locked_responses_sha256}.jsonl"
    )
    manifest_path = (
        output / "manifests" / f"{projected.manifest.manifest_id.rsplit(':', 1)[-1]}.json"
    )
    _write_private(locked_path, projected.locked_responses_jsonl)
    _write_private(manifest_path, _canonical_model_bytes(projected.manifest))
    restored = load_persisted_argilla_projection(
        manifest_path=manifest_path,
        locked_responses_path=locked_path,
    )
    if restored != projected:
        raise ArgillaProjectionOperationError(
            "persisted Argilla response projection differs after reload"
        )
    if restored.manifest.capture_manifest_id != capture.manifest_id:
        raise ArgillaProjectionOperationError(
            "persisted Argilla projection differs from capture manifest"
        )
    return PersistedArgillaProjectionRun(
        run=restored,
        capture_manifest=capture,
        binding_manifest=binding,
        locked_responses_path=locked_path,
        manifest_path=manifest_path,
    )


__all__ = [
    "ArgillaProjectionBindingWriteResult",
    "ArgillaProjectionOperationError",
    "ArgillaRecordAllocationInputV1",
    "PersistedArgillaProjectionRun",
    "load_argilla_projection_binding",
    "load_persisted_argilla_projection",
    "project_and_persist_argilla_capture",
    "write_argilla_projection_binding",
]
