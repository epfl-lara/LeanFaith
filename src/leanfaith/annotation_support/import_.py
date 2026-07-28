"""Fail-closed import of locked, blinded LF-023 annotation responses.

The importer is deliberately platform-neutral.  A platform export must first
be projected into :class:`LockedAnnotationResponseEnvelopeV1` records.  Those
records are then checked against the public bundle manifest and the private
token-to-target linkage produced by ``annotation_support.export``.

Importing preserves raw human responses and creates independent
``AnnotationRecord`` objects.  It does not adjudicate, aggregate, promote, or
resolve any semantic label.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from leanfaith.annotation_support.attestation import (
    AnnotationAttestationError,
    HumanAnnotationAssignmentEnvelopeV1,
    HumanSubmissionAttestationEnvelopeV1,
    load_authentication_key,
    verify_human_assignment,
    verify_human_submission_attestation,
)
from leanfaith.annotation_support.export import (
    ArtifactBinding,
    BlindedAnnotationItemV1,
    BlindedBundleManifestV1,
    PrivateLinkageManifestV1,
    PrivateLinkageRecordV1,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas.annotation import AnnotationRecord
from leanfaith.schemas.enums import (
    AnnotationAnswer,
    ReferenceIssue,
    RelationLabel,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.ids import ANNOTATION_PREFIX, make_id
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.variant import _check_ecodes

ANNOTATION_CAMPAIGN_ID: Literal["lf021_prevalence_v1"] = "lf021_prevalence_v1"
ANNOTATOR_SLOTS = ("independent_annotator_1", "independent_annotator_2")
_HEX64 = r"^[0-9a-f]{64}$"
_PATH_SEGMENT = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_BLIND_ITEM_ID = r"^lf023_blind_item_v1:[0-9a-f]{64}$"
_BUNDLE_MANIFEST_ID = r"^lf023_blinded_bundle_manifest_v1:[0-9a-f]{64}$"
_LOCKED_RESPONSE_ID = r"^lf023_locked_response_v1:[0-9a-f]{64}$"
_IMPORT_MANIFEST_ID = r"^lf023_annotation_import_manifest_v1:[0-9a-f]{64}$"


class AnnotationImportError(ValueError):
    """Raised when a response export cannot be linked without ambiguity."""


class IndependentAnnotationResponseV1(StrictModel):
    """Platform-neutral projection of one independent semantic response."""

    same_claim: AnnotationAnswer
    relation: RelationLabel | None
    confidence: int = Field(ge=1, le=5)
    rationale: str
    reference_issue: ReferenceIssue
    error_types: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _semantic_contract(self) -> Self:
        _check_ecodes(self.error_types)
        if tuple(sorted(set(self.error_types))) != self.error_types:
            raise ValueError("error_types must be sorted and unique")
        if self.same_claim is AnnotationAnswer.SAME_CLAIM:
            if self.relation is not RelationLabel.EQUIVALENT:
                raise ValueError("same_claim requires relation=equivalent")
        elif self.same_claim is AnnotationAnswer.NOT_SAME_CLAIM:
            if self.relation not in {
                RelationLabel.A_STRONGER,
                RelationLabel.B_STRONGER,
                RelationLabel.INCOMPARABLE,
                RelationLabel.UNRELATED,
            }:
                raise ValueError("not_same_claim requires a non-equivalent terminal relation")
        elif self.same_claim is AnnotationAnswer.AMBIGUOUS:
            if self.relation is not RelationLabel.AMBIGUOUS:
                raise ValueError("ambiguous requires relation=ambiguous")
        elif self.relation is not None:
            raise ValueError("cannot_assess_yet requires relation=null")
        if self.same_claim is not AnnotationAnswer.SAME_CLAIM and not self.rationale.strip():
            raise ValueError("non-same or unresolved responses require a rationale")
        return self


class LockedAnnotationResponseContentV1(StrictModel):
    """Content covered by one locked-response identifier."""

    schema_version: Literal[1] = 1
    campaign_id: Literal["lf021_prevalence_v1"] = ANNOTATION_CAMPAIGN_ID
    annotator_slot: Literal["independent_annotator_1", "independent_annotator_2"]
    opaque_item_token: str = Field(pattern=_BLIND_ITEM_ID)
    annotator_id: str = Field(min_length=1)
    round_id: str = Field(pattern=_PATH_SEGMENT)
    created_at: datetime.datetime
    locked: Literal[True] = True
    bundle_manifest_id: str = Field(pattern=_BUNDLE_MANIFEST_ID)
    guideline: ArtifactBinding
    backend_submission_id: str = Field(min_length=1)
    response: IndependentAnnotationResponseV1

    @model_validator(mode="after")
    def _utc(self) -> Self:
        require_utc(self.created_at)
        return self


def make_locked_response_id(
    value: LockedAnnotationResponseContentV1 | dict[str, Any],
) -> str:
    """Return the deterministic identifier for one normalized locked response."""

    content = (
        value
        if isinstance(value, LockedAnnotationResponseContentV1)
        else LockedAnnotationResponseContentV1.model_validate(value)
    )
    return "lf023_locked_response_v1:" + hash_canonical(
        {
            "schema": "lf023_locked_response_v1",
            **content.model_dump(mode="json"),
        }
    )


class LockedAnnotationResponseEnvelopeV1(LockedAnnotationResponseContentV1):
    """One immutable independent response returned by an annotation backend."""

    response_id: str = Field(pattern=_LOCKED_RESPONSE_ID)

    @model_validator(mode="after")
    def _content_id(self) -> Self:
        content = LockedAnnotationResponseContentV1.model_validate(
            self.model_dump(mode="json", exclude={"response_id"})
        )
        if self.response_id != make_locked_response_id(content):
            raise ValueError("locked response ID differs from normalized content")
        return self


class AnnotationImportManifestV1(StrictModel):
    """Private, content-addressed manifest for one slot import."""

    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=_IMPORT_MANIFEST_ID)
    manifest_kind: Literal["lf023_annotation_import_v1"]
    campaign_id: Literal["lf021_prevalence_v1"]
    annotator_slot: Literal["independent_annotator_1", "independent_annotator_2"]
    public_bundle_manifest: ArtifactBinding
    private_linkage_manifest: ArtifactBinding
    human_assignment: ArtifactBinding
    human_submission_attestation: ArtifactBinding
    locked_responses: ArtifactBinding
    annotation_records: ArtifactBinding
    item_count: Literal[240]
    response_count: int = Field(ge=1, le=240)
    missing_item_count: int = Field(ge=0, le=239)
    missing_item_tokens_sha256: str = Field(pattern=_HEX64)
    response_lock_set_sha256: str = Field(pattern=_HEX64)
    complete: bool
    raw_responses_preserved: Literal[True] = True
    raw_annotation_records_created: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    resolved_labels_created: Literal[False] = False
    gold_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    assignment_mode: Literal["operator_attested_human", "test_fixture"]
    origin_assurance: Literal["operator_attested", "test_fixture"]
    operator_attestation_verified: Literal[True] = True
    backend_origin_verified: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    fixture_only: bool
    adjudications_created: Literal[False] = False
    private: Literal[True] = True
    release_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _coherent_and_content_addressed(self) -> Self:
        if self.response_count + self.missing_item_count != self.item_count:
            raise ValueError("response and missing counts must reconcile to item_count")
        if self.complete != (self.missing_item_count == 0):
            raise ValueError("complete must be true exactly when no items are missing")
        expected_assurance = (
            "operator_attested"
            if self.assignment_mode == "operator_attested_human"
            else "test_fixture"
        )
        if self.origin_assurance != expected_assurance:
            raise ValueError("origin assurance differs from assignment mode")
        if self.fixture_only != (self.assignment_mode == "test_fixture"):
            raise ValueError("fixture_only differs from assignment mode")
        payload = self.model_dump(mode="json")
        expected = "lf023_annotation_import_manifest_v1:" + hash_canonical(
            {
                "schema": "lf023_annotation_import_manifest_v1",
                **{key: item for key, item in payload.items() if key != "manifest_id"},
            }
        )
        if self.manifest_id != expected:
            raise ValueError("annotation import manifest ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class AnnotationImportRun:
    manifest: AnnotationImportManifestV1
    manifest_path: Path
    locked_responses_path: Path
    annotation_records_path: Path
    responses: tuple[LockedAnnotationResponseEnvelopeV1, ...]
    annotations: tuple[AnnotationRecord, ...]
    assignment: HumanAnnotationAssignmentEnvelopeV1
    submission_attestation: HumanSubmissionAttestationEnvelopeV1


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise AnnotationImportError(f"invalid JSON object: {path}") from exc
    if not isinstance(raw, dict):
        raise AnnotationImportError(f"JSON artifact must contain an object: {path}")
    if canonical_json_bytes(raw) != path.read_bytes():
        raise AnnotationImportError(f"JSON artifact is not canonical: {path}")
    return raw


def _load_jsonl_models[T: StrictModel](
    path: Path,
    model_type: type[T],
) -> tuple[T, ...]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AnnotationImportError(f"cannot read JSONL artifact: {path}") from exc
    if not payload or not payload.endswith(b"\n"):
        raise AnnotationImportError(f"JSONL artifact must be nonempty and end in LF: {path}")
    result: list[T] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnnotationImportError(f"invalid JSONL row {path}:{line_number}") from exc
        if not isinstance(raw, dict) or canonical_json_bytes(raw) != line:
            raise AnnotationImportError(f"noncanonical JSONL row {path}:{line_number}")
        try:
            result.append(model_type.model_validate(raw))
        except ValueError as exc:
            raise AnnotationImportError(f"invalid JSONL row {path}:{line_number}: {exc}") from exc
    return tuple(result)


def _resolve_binding(repo_root: Path, binding: ArtifactBinding) -> Path:
    raw = Path(binding.artifact)
    path = raw if raw.is_absolute() else repo_root / raw
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AnnotationImportError(f"bound artifact is unavailable: {binding.artifact}") from exc
    if not raw.is_absolute() and not resolved.is_relative_to(repo_root.resolve()):
        raise AnnotationImportError(f"bound artifact escapes repository: {binding.artifact}")
    if not resolved.is_file() or hash_file(resolved) != binding.sha256:
        raise AnnotationImportError(f"bound artifact hash differs: {binding.artifact}")
    return resolved


def _binding(repo_root: Path, path: Path) -> ArtifactBinding:
    resolved = path.resolve(strict=True)
    root = repo_root.resolve()
    artifact = (
        resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else str(resolved)
    )
    return ArtifactBinding(artifact=artifact, sha256=hash_file(resolved))


def _require_private(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise AnnotationImportError(f"private annotation artifact is unavailable: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise AnnotationImportError(f"private annotation artifact is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AnnotationImportError(
            f"private annotation artifact cannot be opened without following links: {path}"
        ) from exc
    try:
        opened_stat = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(opened_stat.st_mode) or (opened_stat.st_dev, opened_stat.st_ino) != (
        path_stat.st_dev,
        path_stat.st_ino,
    ):
        raise AnnotationImportError(
            f"private annotation artifact changed during validation: {path}"
        )
    if stat.S_IMODE(opened_stat.st_mode) & 0o077:
        raise AnnotationImportError(f"private annotation artifact is not mode-0600: {path}")


def _resolve_private_input(path: Path) -> Path:
    """Reject links before canonicalization, then recheck the resolved file."""

    _require_private(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AnnotationImportError(f"private annotation artifact is unavailable: {path}") from exc
    _require_private(resolved)
    return resolved


def _write_private_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path.parent
    while True:
        if stat.S_ISLNK(current.lstat().st_mode):
            raise AnnotationImportError(f"annotation output parent is a symlink: {current}")
        if current == current.parent:
            break
        current = current.parent
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
            raise AnnotationImportError(f"immutable annotation artifact differs: {path}") from None
        _require_private(path)
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


def _jsonl_bytes(records: tuple[StrictModel, ...]) -> bytes:
    return b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )


def _response_lock_payloads(
    assignment: HumanAnnotationAssignmentEnvelopeV1,
    attestation: HumanSubmissionAttestationEnvelopeV1,
    responses: tuple[LockedAnnotationResponseEnvelopeV1, ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "schema_version": 1,
            "lock_kind": "lf023_raw_response_logical_lock_v1",
            "campaign_id": assignment.campaign_id,
            "round_id": assignment.round_id,
            "annotator_slot": assignment.annotator_slot,
            "opaque_item_token": response.opaque_item_token,
            "response_id": response.response_id,
            "assignment_id": assignment.assignment_id,
            "submission_attestation_id": attestation.attestation_id,
            "backend_submission_id": response.backend_submission_id,
        }
        for response in responses
    )


def _canonical_response_lock_registry(repo_root: Path) -> Path:
    """Return the one production response-lock registry for this repository."""

    return repo_root.resolve() / "data" / "human" / "response_locks"


def _lock_raw_responses(
    *,
    repo_root: Path,
    output_root: Path,
    assignment: HumanAnnotationAssignmentEnvelopeV1,
    attestation: HumanSubmissionAttestationEnvelopeV1,
    responses: tuple[LockedAnnotationResponseEnvelopeV1, ...],
) -> str:
    """Atomically reject divergent responses for one campaign/round/slot/item."""

    registry_root = (
        _canonical_response_lock_registry(repo_root)
        if assignment.assignment_mode == "operator_attested_human"
        else output_root / "response_locks"
    )
    lock_root = (
        registry_root / assignment.campaign_id / assignment.round_id / assignment.annotator_slot
    )
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry_lock_path = lock_root / ".registry.lock"
    _write_private_immutable(registry_lock_path, b"")
    descriptor = os.open(
        registry_lock_path,
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.chmod(registry_lock_path, 0o600, follow_symlinks=False)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        entries: list[tuple[Path, bytes]] = []
        lock_payloads = _response_lock_payloads(assignment, attestation, responses)
        for response, payload in zip(responses, lock_payloads, strict=True):
            token_digest = response.opaque_item_token.rsplit(":", 1)[-1]
            entries.append((lock_root / f"{token_digest}.json", canonical_json_bytes(payload)))
        for path, encoded in entries:
            if path.exists():
                if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
                    raise AnnotationImportError(
                        "a divergent raw response is already locked for this item"
                    )
                _require_private(path)
        for path, encoded in entries:
            _write_private_immutable(path, encoded)
        return hash_canonical(
            {
                "schema": "lf023_raw_response_lock_set_v1",
                "locks": lock_payloads,
            }
        )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _annotation_from_response(
    response: LockedAnnotationResponseEnvelopeV1,
    linkage: PrivateLinkageRecordV1,
    assignment: HumanAnnotationAssignmentEnvelopeV1,
    submission_attestation: HumanSubmissionAttestationEnvelopeV1,
) -> AnnotationRecord:
    semantic = response.response
    annotation_id = make_id(
        ANNOTATION_PREFIX,
        {
            "schema": "lf023_imported_independent_annotation_v1",
            "response_id": response.response_id,
            "target_pair_id": linkage.target_pair_id,
            "annotator_slot": response.annotator_slot,
            "assignment_id": assignment.assignment_id,
            "submission_attestation_id": submission_attestation.attestation_id,
        },
    )
    return AnnotationRecord(
        annotation_id=annotation_id,
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=linkage.target_pair_id,
        annotator_id=response.annotator_id,
        round_id=response.round_id,
        same_claim=semantic.same_claim,
        relation=semantic.relation,
        error_types=semantic.error_types,
        confidence=semantic.confidence,
        rationale=semantic.rationale,
        reference_issue=semantic.reference_issue,
        created_at=response.created_at,
        metadata={
            "campaign_id": response.campaign_id,
            "annotator_slot": response.annotator_slot,
            "opaque_item_token": response.opaque_item_token,
            "source_frame_record_id": linkage.source_frame_record_id,
            "target_nl_lean_id": linkage.target_nl_lean_id,
            "bundle_manifest_id": response.bundle_manifest_id,
            "guideline_artifact": response.guideline.artifact,
            "guideline_sha256": response.guideline.sha256,
            "locked_response_id": response.response_id,
            "backend_submission_id": response.backend_submission_id,
            "import_role": (
                "raw_operator_attested_annotation"
                if assignment.assignment_mode == "operator_attested_human"
                else "raw_annotation_test_fixture"
            ),
            "human_assignment_id": assignment.assignment_id,
            "human_submission_attestation_id": submission_attestation.attestation_id,
            "annotator_principal_hash": assignment.annotator_principal_hash,
            "assignment_mode": assignment.assignment_mode,
            "origin_assurance": submission_attestation.origin_assurance,
            "operator_attestation_verified": True,
            "backend_origin_verified": False,
            "human_gold_eligible": False,
            "fixture_only": assignment.assignment_mode == "test_fixture",
            "raw_vote_only": True,
            "resolved_label_created": False,
            "gold_label_created": False,
            "training_eligible": False,
        },
    )


def import_blinded_annotation_responses(
    *,
    repo_root: Path,
    public_bundle_manifest_path: Path,
    private_linkage_manifest_path: Path,
    human_assignment_path: Path,
    human_submission_attestation_path: Path,
    authentication_key_path: Path,
    response_path: Path,
    output_root: Path,
    allow_test_fixture: bool = False,
) -> AnnotationImportRun:
    """Import one annotator slot without resolving or adjudicating responses."""

    repo_root = repo_root.resolve(strict=True)
    public_bundle_manifest_path = public_bundle_manifest_path.resolve(strict=True)
    private_linkage_manifest_path = _resolve_private_input(private_linkage_manifest_path)
    human_assignment_path = _resolve_private_input(human_assignment_path)
    human_submission_attestation_path = _resolve_private_input(human_submission_attestation_path)
    authentication_key_path = _resolve_private_input(authentication_key_path)
    response_path = _resolve_private_input(response_path)

    public_manifest = BlindedBundleManifestV1.model_validate(
        _load_json_object(public_bundle_manifest_path)
    )
    private_manifest = PrivateLinkageManifestV1.model_validate(
        _load_json_object(private_linkage_manifest_path)
    )
    public_binding = _binding(repo_root, public_bundle_manifest_path)
    if public_binding not in private_manifest.public_bundle_manifests:
        raise AnnotationImportError("public bundle manifest is not bound by private linkage")
    private_binding = _binding(repo_root, private_linkage_manifest_path)
    assignment_binding = _binding(repo_root, human_assignment_path)
    attestation_binding = _binding(repo_root, human_submission_attestation_path)
    response_binding = _binding(repo_root, response_path)
    try:
        authentication_key = load_authentication_key(authentication_key_path)
        assignment = HumanAnnotationAssignmentEnvelopeV1.model_validate(
            _load_json_object(human_assignment_path)
        )
        submission_attestation = HumanSubmissionAttestationEnvelopeV1.model_validate(
            _load_json_object(human_submission_attestation_path)
        )
        verify_human_assignment(assignment, key=authentication_key)
        verify_human_submission_attestation(submission_attestation, key=authentication_key)
    except (AnnotationAttestationError, ValueError) as exc:
        raise AnnotationImportError(f"human response authentication failed: {exc}") from exc
    if assignment.assignment_mode == "test_fixture" and not allow_test_fixture:
        raise AnnotationImportError("test-fixture assignment is not accepted by production import")
    if (
        assignment.public_bundle_manifest != public_binding
        or assignment.private_linkage_manifest != private_binding
        or assignment.bundle_manifest_id != public_manifest.manifest_id
        or assignment.guideline != private_manifest.annotation_guideline
        or assignment.annotator_slot != public_manifest.annotator_slot
    ):
        raise AnnotationImportError("human assignment differs from frozen annotation artifacts")
    if (
        submission_attestation.assignment_id != assignment.assignment_id
        or submission_attestation.assignment_artifact != assignment_binding
        or submission_attestation.response_artifact != response_binding
        or submission_attestation.authentication_key_id != assignment.authentication_key_id
        or submission_attestation.assignment_mode != assignment.assignment_mode
        or submission_attestation.operator_human_origin_asserted
        != (assignment.assignment_mode == "operator_attested_human")
        or submission_attestation.origin_assurance
        != (
            "operator_attested"
            if assignment.assignment_mode == "operator_attested_human"
            else "test_fixture"
        )
        or not submission_attestation.operator_attestation_verified
        or submission_attestation.backend_origin_verified
        or submission_attestation.human_gold_eligible
        or submission_attestation.fixture_only != (assignment.assignment_mode == "test_fixture")
    ):
        raise AnnotationImportError("submission attestation differs from assignment or response")
    if submission_attestation.attested_at < assignment.assigned_at:
        raise AnnotationImportError("submission was attested before its assignment")

    bundle_path = _resolve_binding(repo_root, public_manifest.bundle)
    bundle_items = _load_jsonl_models(bundle_path, BlindedAnnotationItemV1)
    if len(bundle_items) != public_manifest.item_count:
        raise AnnotationImportError("public bundle item count differs from its manifest")
    bundle_tokens = {item.opaque_item_token for item in bundle_items}
    if len(bundle_tokens) != len(bundle_items):
        raise AnnotationImportError("public bundle contains duplicate opaque item tokens")

    linkage_path = _resolve_binding(repo_root, private_manifest.private_linkage)
    _require_private(linkage_path)
    linkage_records = _load_jsonl_models(linkage_path, PrivateLinkageRecordV1)
    if len(linkage_records) != private_manifest.item_count:
        raise AnnotationImportError("private linkage count differs from its manifest")
    slot_field = (
        "independent_annotator_1_item_id"
        if public_manifest.annotator_slot == "independent_annotator_1"
        else "independent_annotator_2_item_id"
    )
    linkage_by_token = {getattr(item, slot_field): item for item in linkage_records}
    if len(linkage_by_token) != len(linkage_records):
        raise AnnotationImportError("private linkage contains duplicate slot tokens")
    if set(linkage_by_token) != bundle_tokens:
        raise AnnotationImportError("public bundle and private linkage token sets differ")

    responses = _load_jsonl_models(response_path, LockedAnnotationResponseEnvelopeV1)
    response_tokens = [item.opaque_item_token for item in responses]
    response_ids = [item.response_id for item in responses]
    backend_submission_ids = [item.backend_submission_id for item in responses]
    if len(set(response_tokens)) != len(response_tokens):
        raise AnnotationImportError("response export contains duplicate opaque item tokens")
    if len(set(response_ids)) != len(response_ids):
        raise AnnotationImportError("response export contains duplicate response IDs")
    if len(set(backend_submission_ids)) != len(backend_submission_ids):
        raise AnnotationImportError("response export contains duplicate backend submission IDs")
    if any(item.annotator_slot != public_manifest.annotator_slot for item in responses):
        raise AnnotationImportError("response annotator slot differs from bundle manifest")
    if any(item.bundle_manifest_id != public_manifest.manifest_id for item in responses):
        raise AnnotationImportError("response bundle manifest ID differs")
    if any(item.guideline != private_manifest.annotation_guideline for item in responses):
        raise AnnotationImportError("response guideline binding differs from frozen export")
    unknown = sorted(set(response_tokens) - bundle_tokens)
    if unknown:
        raise AnnotationImportError(f"response export contains unknown item token: {unknown[0]}")
    annotators = {(item.annotator_id, item.round_id) for item in responses}
    if len(annotators) != 1:
        raise AnnotationImportError("one slot import must contain one annotator and round")
    if any(
        item.annotator_id != assignment.annotator_id
        or item.round_id != assignment.round_id
        or item.campaign_id != assignment.campaign_id
        for item in responses
    ):
        raise AnnotationImportError(
            "response annotator, round, or campaign differs from assignment"
        )
    if any(item.created_at < assignment.assigned_at for item in responses):
        raise AnnotationImportError("response predates its authenticated assignment")
    if any(item.created_at > submission_attestation.attested_at for item in responses):
        raise AnnotationImportError("response timestamp is later than submission attestation")

    ordered_responses = tuple(sorted(responses, key=lambda item: item.opaque_item_token))
    annotations = tuple(
        _annotation_from_response(
            item,
            linkage_by_token[item.opaque_item_token],
            assignment,
            submission_attestation,
        )
        for item in ordered_responses
    )
    if len({item.annotation_id for item in annotations}) != len(annotations):
        raise AnnotationImportError("imported annotation IDs collided")
    response_lock_set_sha256 = _lock_raw_responses(
        repo_root=repo_root,
        output_root=output_root,
        assignment=assignment,
        attestation=submission_attestation,
        responses=ordered_responses,
    )

    response_bytes = _jsonl_bytes(ordered_responses)
    annotation_bytes = _jsonl_bytes(annotations)
    response_sha = sha256_hex(response_bytes)
    annotation_sha = sha256_hex(annotation_bytes)
    locked_path = output_root / public_manifest.annotator_slot / "locked" / f"{response_sha}.jsonl"
    annotations_path = (
        output_root / public_manifest.annotator_slot / "annotations" / f"{annotation_sha}.jsonl"
    )
    _write_private_immutable(locked_path, response_bytes)
    _write_private_immutable(annotations_path, annotation_bytes)

    missing_tokens = tuple(sorted(bundle_tokens - set(response_tokens)))
    manifest_payload: dict[str, Any] = {
        "schema_version": 1,
        "manifest_kind": "lf023_annotation_import_v1",
        "campaign_id": ANNOTATION_CAMPAIGN_ID,
        "annotator_slot": public_manifest.annotator_slot,
        "public_bundle_manifest": public_binding.model_dump(mode="json"),
        "private_linkage_manifest": private_binding.model_dump(mode="json"),
        "human_assignment": assignment_binding.model_dump(mode="json"),
        "human_submission_attestation": attestation_binding.model_dump(mode="json"),
        "locked_responses": _binding(repo_root, locked_path).model_dump(mode="json"),
        "annotation_records": _binding(repo_root, annotations_path).model_dump(mode="json"),
        "item_count": public_manifest.item_count,
        "response_count": len(responses),
        "missing_item_count": len(missing_tokens),
        "missing_item_tokens_sha256": hash_canonical(
            {
                "schema": "lf023_missing_item_tokens_v1",
                "tokens": missing_tokens,
            }
        ),
        "response_lock_set_sha256": response_lock_set_sha256,
        "complete": not missing_tokens,
        "raw_responses_preserved": True,
        "raw_annotation_records_created": True,
        "semantic_labels_created": False,
        "resolved_labels_created": False,
        "gold_labels_created": False,
        "training_eligible": False,
        "assignment_mode": assignment.assignment_mode,
        "origin_assurance": submission_attestation.origin_assurance,
        "operator_attestation_verified": True,
        "backend_origin_verified": False,
        "human_gold_eligible": False,
        "fixture_only": assignment.assignment_mode == "test_fixture",
        "adjudications_created": False,
        "private": True,
        "release_eligible": False,
    }
    manifest_id = "lf023_annotation_import_manifest_v1:" + hash_canonical(
        {"schema": "lf023_annotation_import_manifest_v1", **manifest_payload}
    )
    manifest = AnnotationImportManifestV1.model_validate(
        {"manifest_id": manifest_id, **manifest_payload}
    )
    manifest_path = output_root / "manifests" / f"{manifest_id.rsplit(':', 1)[-1]}.json"
    _write_private_immutable(
        manifest_path,
        canonical_json_bytes(manifest.model_dump(mode="json")),
    )
    return AnnotationImportRun(
        manifest=manifest,
        manifest_path=manifest_path,
        locked_responses_path=locked_path,
        annotation_records_path=annotations_path,
        responses=ordered_responses,
        annotations=annotations,
        assignment=assignment,
        submission_attestation=submission_attestation,
    )


def load_verified_annotation_import(
    *,
    repo_root: Path,
    manifest_path: Path,
    authentication_key_path: Path,
    allow_test_fixture: bool = False,
) -> AnnotationImportRun:
    """Reload one import and reverify its complete authenticated lineage.

    Downstream agreement and routing must not accept arbitrary annotation
    JSONL.  They consume only manifests that still bind an authenticated
    assignment, the exact backend export, the blinded source artifacts, the
    locked response set, and the deterministically reconstructed raw records.
    """

    repo_root = repo_root.resolve(strict=True)
    manifest_path = _resolve_private_input(manifest_path)
    authentication_key_path = _resolve_private_input(authentication_key_path)
    try:
        manifest = AnnotationImportManifestV1.model_validate(_load_json_object(manifest_path))
        key = load_authentication_key(authentication_key_path)
    except (AnnotationAttestationError, ValueError) as exc:
        raise AnnotationImportError(f"invalid authenticated import manifest: {exc}") from exc
    if manifest.assignment_mode == "test_fixture" and not allow_test_fixture:
        raise AnnotationImportError("test-fixture import is not accepted by production reload")

    public_manifest_path = _resolve_binding(repo_root, manifest.public_bundle_manifest)
    private_manifest_path = _resolve_binding(repo_root, manifest.private_linkage_manifest)
    assignment_path = _resolve_binding(repo_root, manifest.human_assignment)
    attestation_path = _resolve_binding(repo_root, manifest.human_submission_attestation)
    locked_responses_path = _resolve_binding(repo_root, manifest.locked_responses)
    annotation_records_path = _resolve_binding(repo_root, manifest.annotation_records)
    for private_path in (
        private_manifest_path,
        assignment_path,
        attestation_path,
        locked_responses_path,
        annotation_records_path,
    ):
        _require_private(private_path)

    try:
        public_manifest = BlindedBundleManifestV1.model_validate(
            _load_json_object(public_manifest_path)
        )
        private_manifest = PrivateLinkageManifestV1.model_validate(
            _load_json_object(private_manifest_path)
        )
        assignment = HumanAnnotationAssignmentEnvelopeV1.model_validate(
            _load_json_object(assignment_path)
        )
        attestation = HumanSubmissionAttestationEnvelopeV1.model_validate(
            _load_json_object(attestation_path)
        )
        verify_human_assignment(assignment, key=key)
        verify_human_submission_attestation(attestation, key=key)
    except (AnnotationAttestationError, ValueError) as exc:
        raise AnnotationImportError(
            f"human response authentication failed on reload: {exc}"
        ) from exc

    if manifest.public_bundle_manifest not in private_manifest.public_bundle_manifests:
        raise AnnotationImportError("reloaded public bundle is not bound by private linkage")
    if (
        assignment.public_bundle_manifest != manifest.public_bundle_manifest
        or assignment.private_linkage_manifest != manifest.private_linkage_manifest
        or assignment.bundle_manifest_id != public_manifest.manifest_id
        or assignment.guideline != private_manifest.annotation_guideline
        or assignment.annotator_slot != public_manifest.annotator_slot
        or assignment.assignment_mode != manifest.assignment_mode
    ):
        raise AnnotationImportError("reloaded assignment differs from import lineage")
    if (
        attestation.assignment_id != assignment.assignment_id
        or attestation.assignment_artifact != manifest.human_assignment
        or attestation.authentication_key_id != assignment.authentication_key_id
        or attestation.assignment_mode != assignment.assignment_mode
        or attestation.origin_assurance != manifest.origin_assurance
        or attestation.operator_attestation_verified != manifest.operator_attestation_verified
        or attestation.backend_origin_verified != manifest.backend_origin_verified
        or attestation.human_gold_eligible != manifest.human_gold_eligible
        or attestation.fixture_only != manifest.fixture_only
    ):
        raise AnnotationImportError("reloaded submission attestation differs from import lineage")
    original_response_path = _resolve_binding(repo_root, attestation.response_artifact)
    _require_private(original_response_path)

    bundle_path = _resolve_binding(repo_root, public_manifest.bundle)
    bundle_items = _load_jsonl_models(bundle_path, BlindedAnnotationItemV1)
    bundle_tokens = {item.opaque_item_token for item in bundle_items}
    if (
        len(bundle_items) != public_manifest.item_count
        or len(bundle_tokens) != len(bundle_items)
        or public_manifest.item_count != manifest.item_count
    ):
        raise AnnotationImportError("reloaded public bundle count or token set is invalid")

    linkage_path = _resolve_binding(repo_root, private_manifest.private_linkage)
    _require_private(linkage_path)
    linkage_records = _load_jsonl_models(linkage_path, PrivateLinkageRecordV1)
    slot_field = (
        "independent_annotator_1_item_id"
        if public_manifest.annotator_slot == "independent_annotator_1"
        else "independent_annotator_2_item_id"
    )
    linkage_by_token = {getattr(item, slot_field): item for item in linkage_records}
    if (
        len(linkage_records) != private_manifest.item_count
        or len(linkage_by_token) != len(linkage_records)
        or set(linkage_by_token) != bundle_tokens
    ):
        raise AnnotationImportError("reloaded private linkage differs from public bundle")

    original_responses = _load_jsonl_models(
        original_response_path,
        LockedAnnotationResponseEnvelopeV1,
    )
    responses = _load_jsonl_models(
        locked_responses_path,
        LockedAnnotationResponseEnvelopeV1,
    )
    annotations = _load_jsonl_models(annotation_records_path, AnnotationRecord)
    expected_responses = tuple(sorted(original_responses, key=lambda item: item.opaque_item_token))
    if responses != expected_responses:
        raise AnnotationImportError("locked responses differ from the attested backend export")
    response_tokens = [item.opaque_item_token for item in responses]
    response_ids = [item.response_id for item in responses]
    backend_submission_ids = [item.backend_submission_id for item in responses]
    if (
        len(set(response_tokens)) != len(response_tokens)
        or len(set(response_ids)) != len(response_ids)
        or len(set(backend_submission_ids)) != len(backend_submission_ids)
    ):
        raise AnnotationImportError("reloaded responses contain duplicate identities")
    if any(
        item.annotator_slot != assignment.annotator_slot
        or item.annotator_id != assignment.annotator_id
        or item.round_id != assignment.round_id
        or item.campaign_id != assignment.campaign_id
        or item.bundle_manifest_id != assignment.bundle_manifest_id
        or item.guideline != assignment.guideline
        or item.created_at < assignment.assigned_at
        or item.created_at > attestation.attested_at
        for item in responses
    ):
        raise AnnotationImportError("reloaded responses differ from assignment constraints")
    if not set(response_tokens).issubset(bundle_tokens):
        raise AnnotationImportError("reloaded response contains an unknown item token")

    expected_annotations = tuple(
        _annotation_from_response(
            response,
            linkage_by_token[response.opaque_item_token],
            assignment,
            attestation,
        )
        for response in responses
    )
    if annotations != expected_annotations:
        raise AnnotationImportError(
            "raw annotation records do not reconstruct from locked responses"
        )
    missing_tokens = tuple(sorted(bundle_tokens - set(response_tokens)))
    expected_missing_sha = hash_canonical(
        {
            "schema": "lf023_missing_item_tokens_v1",
            "tokens": missing_tokens,
        }
    )
    expected_lock_payloads = _response_lock_payloads(assignment, attestation, responses)
    expected_lock_sha = hash_canonical(
        {
            "schema": "lf023_raw_response_lock_set_v1",
            "locks": expected_lock_payloads,
        }
    )
    if (
        manifest.annotator_slot != assignment.annotator_slot
        or manifest.campaign_id != assignment.campaign_id
        or manifest.response_count != len(responses)
        or manifest.missing_item_count != len(missing_tokens)
        or manifest.missing_item_tokens_sha256 != expected_missing_sha
        or manifest.response_lock_set_sha256 != expected_lock_sha
    ):
        raise AnnotationImportError("reloaded counts or lock-set hashes differ from manifest")

    output_root = manifest_path.parent.parent
    registry_root = (
        _canonical_response_lock_registry(repo_root)
        if assignment.assignment_mode == "operator_attested_human"
        else output_root / "response_locks"
    )
    lock_root = (
        registry_root / assignment.campaign_id / assignment.round_id / assignment.annotator_slot
    )
    for response, payload in zip(responses, expected_lock_payloads, strict=True):
        token_digest = response.opaque_item_token.rsplit(":", 1)[-1]
        lock_path = lock_root / f"{token_digest}.json"
        if not lock_path.is_file() or lock_path.is_symlink():
            raise AnnotationImportError("raw-response logical lock is unavailable")
        _require_private(lock_path)
        if lock_path.read_bytes() != canonical_json_bytes(payload):
            raise AnnotationImportError("raw-response logical lock differs from import lineage")

    return AnnotationImportRun(
        manifest=manifest,
        manifest_path=manifest_path,
        locked_responses_path=locked_responses_path,
        annotation_records_path=annotation_records_path,
        responses=responses,
        annotations=annotations,
        assignment=assignment,
        submission_attestation=attestation,
    )


__all__ = [
    "ANNOTATION_CAMPAIGN_ID",
    "ANNOTATOR_SLOTS",
    "AnnotationImportError",
    "AnnotationImportManifestV1",
    "AnnotationImportRun",
    "IndependentAnnotationResponseV1",
    "LockedAnnotationResponseContentV1",
    "LockedAnnotationResponseEnvelopeV1",
    "import_blinded_annotation_responses",
    "load_verified_annotation_import",
    "make_locked_response_id",
]
