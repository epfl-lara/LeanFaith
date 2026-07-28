"""Fail-closed operator artifacts for the LF-023 human workflow.

These operations deliberately stop before semantic resolution.  They create
authenticated assignment/submission records and immutable administrative
agreement or adjudication-routing artifacts from fully reverified imports.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from leanfaith.annotation_support.adjudication import (
    AdjudicationQueueV1,
    build_adjudication_queue,
)
from leanfaith.annotation_support.agreement import (
    AnnotationAgreementReportV1,
    compute_annotation_agreement,
)
from leanfaith.annotation_support.attestation import (
    AnnotationAttestationError,
    HumanAnnotationAssignmentContentV1,
    HumanAnnotationAssignmentEnvelopeV1,
    HumanSubmissionAttestationContentV1,
    HumanSubmissionAttestationEnvelopeV1,
    authenticate_human_assignment,
    authenticate_human_submission,
    authentication_key_id,
    load_authentication_key,
    verify_human_assignment,
    verify_human_submission_attestation,
)
from leanfaith.annotation_support.export import (
    ArtifactBinding,
    BlindedAnnotationItemV1,
    BlindedBundleManifestV1,
    PrivateLinkageManifestV1,
)
from leanfaith.annotation_support.import_ import (
    AnnotationImportManifestV1,
    LockedAnnotationResponseEnvelopeV1,
    _binding,
    _load_json_object,
    _load_jsonl_models,
    _require_private,
    _resolve_binding,
    _resolve_private_input,
    _write_private_immutable,
    load_verified_annotation_import,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import require_utc

_HEX64 = r"^[0-9a-f]{64}$"
_AGREEMENT_ARTIFACT_ID = r"^lf023_authenticated_agreement_artifact_v1:[0-9a-f]{64}$"
_ADJUDICATION_ARTIFACT_ID = r"^lf023_authenticated_adjudication_artifact_v1:[0-9a-f]{64}$"


class AnnotationOperationError(ValueError):
    """Raised when an operator artifact cannot be created without ambiguity."""


@dataclass(frozen=True, slots=True)
class HumanAssignmentRun:
    assignment: HumanAnnotationAssignmentEnvelopeV1
    path: Path


@dataclass(frozen=True, slots=True)
class SubmissionAttestationRun:
    attestation: HumanSubmissionAttestationEnvelopeV1
    path: Path


class AdjudicationPolicyTargetV1(StrictModel):
    """One target selected by the frozen versioned routing policy."""

    target_kind: Literal["lean_pair"]
    target_id: str = Field(min_length=1)


class AdjudicationPolicyTriggerSetV1(StrictModel):
    """Canonical optional policy-trigger input for queue construction."""

    schema_version: Literal[1] = 1
    policy_id: Literal["annotation_codebook_v1#adjudication_triggers"]
    targets: tuple[AdjudicationPolicyTargetV1, ...] = ()

    @model_validator(mode="after")
    def _ordered_unique(self) -> Self:
        keys = tuple((item.target_kind, item.target_id) for item in self.targets)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("adjudication policy targets must be sorted and unique")
        return self


class AuthenticatedAgreementArtifactV1(StrictModel):
    """Immutable report whose two raw imports were reauthenticated."""

    schema_version: Literal[1] = 1
    artifact_id: str = Field(pattern=_AGREEMENT_ARTIFACT_ID)
    artifact_kind: Literal["lf023_authenticated_agreement_artifact_v1"]
    first_import_manifest: ArtifactBinding
    second_import_manifest: ArtifactBinding
    report: AnnotationAgreementReportV1
    authenticated_imports_reverified: Literal[True] = True
    source_imports_complete: Literal[True] = True
    raw_annotations_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    resolved_labels_created: Literal[False] = False
    gold_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    private: Literal[True] = True
    release_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        payload = self.model_dump(mode="json")
        expected = "lf023_authenticated_agreement_artifact_v1:" + hash_canonical(
            {
                "schema": "lf023_authenticated_agreement_artifact_v1",
                **{key: value for key, value in payload.items() if key != "artifact_id"},
            }
        )
        if self.artifact_id != expected:
            raise ValueError("authenticated agreement artifact ID differs from content")
        return self


class AuthenticatedAdjudicationArtifactV1(StrictModel):
    """Immutable human-routing queue created from reauthenticated raw imports."""

    schema_version: Literal[1] = 1
    artifact_id: str = Field(pattern=_ADJUDICATION_ARTIFACT_ID)
    artifact_kind: Literal["lf023_authenticated_adjudication_artifact_v1"]
    first_import_manifest: ArtifactBinding
    second_import_manifest: ArtifactBinding
    policy_trigger_set: ArtifactBinding | None
    queue: AdjudicationQueueV1
    authenticated_imports_reverified: Literal[True] = True
    source_imports_complete: Literal[True] = True
    raw_annotations_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    adjudications_created: Literal[False] = False
    automatic_resolutions_created: Literal[False] = False
    gold_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    private: Literal[True] = True
    release_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if self.queue.semantic_labels_created or self.queue.adjudications_created:
            raise ValueError("routing artifact cannot contain a semantic adjudication")
        payload = self.model_dump(mode="json")
        expected = "lf023_authenticated_adjudication_artifact_v1:" + hash_canonical(
            {
                "schema": "lf023_authenticated_adjudication_artifact_v1",
                **{key: value for key, value in payload.items() if key != "artifact_id"},
            }
        )
        if self.artifact_id != expected:
            raise ValueError("authenticated adjudication artifact ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class AgreementArtifactRun:
    artifact: AuthenticatedAgreementArtifactV1
    path: Path


@dataclass(frozen=True, slots=True)
class AdjudicationArtifactRun:
    artifact: AuthenticatedAdjudicationArtifactV1
    path: Path


def _private_key(path: Path) -> bytes:
    resolved = _resolve_private_input(path)
    try:
        return load_authentication_key(resolved)
    except AnnotationAttestationError as exc:
        raise AnnotationOperationError(str(exc)) from exc


def _write_and_reload[T: StrictModel](
    *,
    path: Path,
    value: T,
    model_type: type[T],
) -> T:
    payload = canonical_json_bytes(value.model_dump(mode="json"))
    _write_private_immutable(path, payload)
    _require_private(path.resolve(strict=True))
    try:
        return model_type.model_validate(_load_json_object(path.resolve(strict=True)))
    except ValueError as exc:
        raise AnnotationOperationError(f"immutable artifact reload failed: {exc}") from exc


def create_authenticated_human_assignment(
    *,
    repo_root: Path,
    public_bundle_manifest_path: Path,
    private_linkage_manifest_path: Path,
    authentication_key_path: Path,
    round_id: str,
    annotator_slot: Literal["independent_annotator_1", "independent_annotator_2"],
    annotator_id: str,
    annotator_principal_hash: str,
    backend_id: str,
    assigned_at: datetime.datetime,
    output_path: Path,
) -> HumanAssignmentRun:
    """Create one production assignment before any response is observed."""

    repo_root = repo_root.resolve(strict=True)
    public_path = public_bundle_manifest_path.resolve(strict=True)
    private_path = _resolve_private_input(private_linkage_manifest_path)
    public_manifest = BlindedBundleManifestV1.model_validate(_load_json_object(public_path))
    private_manifest = PrivateLinkageManifestV1.model_validate(_load_json_object(private_path))
    public_binding = _binding(repo_root, public_path)
    private_binding = _binding(repo_root, private_path)
    if public_binding not in private_manifest.public_bundle_manifests:
        raise AnnotationOperationError("bundle manifest is not bound by private linkage")
    if public_manifest.annotator_slot != annotator_slot:
        raise AnnotationOperationError("requested slot differs from bundle manifest")
    key = _private_key(authentication_key_path)
    try:
        content = HumanAnnotationAssignmentContentV1(
            campaign_id="lf021_prevalence_v1",
            round_id=round_id,
            annotator_slot=annotator_slot,
            annotator_id=annotator_id,
            annotator_principal_hash=annotator_principal_hash,
            assignment_mode="operator_attested_human",
            backend_id=backend_id,
            assigned_at=assigned_at,
            public_bundle_manifest=public_binding,
            private_linkage_manifest=private_binding,
            bundle_manifest_id=public_manifest.manifest_id,
            guideline=private_manifest.annotation_guideline,
            authentication_key_id=authentication_key_id(key),
        )
        assignment = authenticate_human_assignment(content, key=key)
    except (AnnotationAttestationError, ValueError) as exc:
        raise AnnotationOperationError(f"human assignment rejected: {exc}") from exc
    restored = _write_and_reload(
        path=output_path,
        value=assignment,
        model_type=HumanAnnotationAssignmentEnvelopeV1,
    )
    try:
        verify_human_assignment(restored, key=key)
    except AnnotationAttestationError as exc:
        raise AnnotationOperationError("written human assignment failed authentication") from exc
    return HumanAssignmentRun(assignment=restored, path=output_path.resolve(strict=True))


def attest_human_submission(
    *,
    repo_root: Path,
    human_assignment_path: Path,
    response_path: Path,
    authentication_key_path: Path,
    backend_export_id: str,
    verifier_id: str,
    attested_at: datetime.datetime,
    confirm_operator_human_origin_assertion: bool,
    confirm_backend_export_locked: bool,
    output_path: Path,
) -> SubmissionAttestationRun:
    """Bind an authenticated production assignment to one exact backend export."""

    if not confirm_operator_human_origin_assertion or not confirm_backend_export_locked:
        raise AnnotationOperationError(
            "operator must explicitly confirm its human-origin assertion and the locked "
            "backend export"
        )
    require_utc(attested_at)
    repo_root = repo_root.resolve(strict=True)
    assignment_path = _resolve_private_input(human_assignment_path)
    response_path = _resolve_private_input(response_path)
    key = _private_key(authentication_key_path)
    try:
        assignment = HumanAnnotationAssignmentEnvelopeV1.model_validate(
            _load_json_object(assignment_path)
        )
        verify_human_assignment(assignment, key=key)
    except (AnnotationAttestationError, ValueError) as exc:
        raise AnnotationOperationError(f"human assignment authentication failed: {exc}") from exc
    if assignment.assignment_mode != "operator_attested_human":
        raise AnnotationOperationError(
            "production attestation requires an operator-attested assignment"
        )

    public_path = _resolve_binding(repo_root, assignment.public_bundle_manifest)
    private_path = _resolve_binding(repo_root, assignment.private_linkage_manifest)
    _require_private(private_path)
    public_manifest = BlindedBundleManifestV1.model_validate(_load_json_object(public_path))
    private_manifest = PrivateLinkageManifestV1.model_validate(_load_json_object(private_path))
    if (
        assignment.public_bundle_manifest not in private_manifest.public_bundle_manifests
        or assignment.bundle_manifest_id != public_manifest.manifest_id
        or assignment.guideline != private_manifest.annotation_guideline
        or assignment.annotator_slot != public_manifest.annotator_slot
    ):
        raise AnnotationOperationError("assignment no longer matches frozen annotation artifacts")

    responses = _load_jsonl_models(response_path, LockedAnnotationResponseEnvelopeV1)
    if not responses:
        raise AnnotationOperationError("cannot attest an empty backend response export")
    tokens = [item.opaque_item_token for item in responses]
    response_ids = [item.response_id for item in responses]
    backend_ids = [item.backend_submission_id for item in responses]
    if (
        len(tokens) != len(set(tokens))
        or len(response_ids) != len(set(response_ids))
        or len(backend_ids) != len(set(backend_ids))
    ):
        raise AnnotationOperationError("backend response export contains duplicate identities")
    if any(
        item.campaign_id != assignment.campaign_id
        or item.annotator_slot != assignment.annotator_slot
        or item.annotator_id != assignment.annotator_id
        or item.round_id != assignment.round_id
        or item.bundle_manifest_id != assignment.bundle_manifest_id
        or item.guideline != assignment.guideline
        or item.created_at < assignment.assigned_at
        or item.created_at > attested_at
        for item in responses
    ):
        raise AnnotationOperationError("backend response export differs from assignment")
    bundle_path = _resolve_binding(repo_root, public_manifest.bundle)
    bundle_items = _load_jsonl_models(
        bundle_path,
        BlindedAnnotationItemV1,
    )
    bundle_tokens = {item.opaque_item_token for item in bundle_items}
    if not set(tokens).issubset(bundle_tokens):
        raise AnnotationOperationError("backend response export contains an unknown item token")

    try:
        content = HumanSubmissionAttestationContentV1(
            assignment_id=assignment.assignment_id,
            assignment_artifact=_binding(repo_root, assignment_path),
            response_artifact=_binding(repo_root, response_path),
            backend_export_id=backend_export_id,
            verifier_id=verifier_id,
            attested_at=attested_at,
            authentication_key_id=authentication_key_id(key),
            assignment_mode="operator_attested_human",
            operator_human_origin_asserted=True,
            origin_assurance="operator_attested",
            operator_attestation_verified=True,
            backend_origin_verified=False,
            human_gold_eligible=False,
            fixture_only=False,
        )
        attestation = authenticate_human_submission(content, key=key)
    except (AnnotationAttestationError, ValueError) as exc:
        raise AnnotationOperationError(f"human submission attestation rejected: {exc}") from exc
    restored = _write_and_reload(
        path=output_path,
        value=attestation,
        model_type=HumanSubmissionAttestationEnvelopeV1,
    )
    try:
        verify_human_submission_attestation(restored, key=key)
    except AnnotationAttestationError as exc:
        raise AnnotationOperationError(
            "written human submission attestation failed authentication"
        ) from exc
    return SubmissionAttestationRun(attestation=restored, path=output_path.resolve(strict=True))


def _require_complete_distinct_imports(
    first: AnnotationImportManifestV1,
    second: AnnotationImportManifestV1,
) -> None:
    if not first.complete or not second.complete:
        raise AnnotationOperationError("agreement and routing require two complete imports")
    if first.manifest_id == second.manifest_id:
        raise AnnotationOperationError("agreement and routing require two distinct imports")
    if (
        first.assignment_mode != "operator_attested_human"
        or second.assignment_mode != "operator_attested_human"
    ):
        raise AnnotationOperationError("production agreement and routing require human imports")


def write_authenticated_agreement(
    *,
    repo_root: Path,
    first_import_manifest_path: Path,
    second_import_manifest_path: Path,
    authentication_key_path: Path,
    output_path: Path,
) -> AgreementArtifactRun:
    """Write and reload an immutable raw agreement artifact."""

    first = load_verified_annotation_import(
        repo_root=repo_root,
        manifest_path=first_import_manifest_path,
        authentication_key_path=authentication_key_path,
    )
    second = load_verified_annotation_import(
        repo_root=repo_root,
        manifest_path=second_import_manifest_path,
        authentication_key_path=authentication_key_path,
    )
    _require_complete_distinct_imports(first.manifest, second.manifest)
    report = compute_annotation_agreement(first.annotations, second.annotations)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "lf023_authenticated_agreement_artifact_v1",
        "first_import_manifest": _binding(repo_root, first.manifest_path).model_dump(mode="json"),
        "second_import_manifest": _binding(repo_root, second.manifest_path).model_dump(mode="json"),
        "report": report.model_dump(mode="json"),
        "authenticated_imports_reverified": True,
        "source_imports_complete": True,
        "raw_annotations_only": True,
        "semantic_labels_created": False,
        "resolved_labels_created": False,
        "gold_labels_created": False,
        "training_eligible": False,
        "private": True,
        "release_eligible": False,
    }
    artifact_id = "lf023_authenticated_agreement_artifact_v1:" + hash_canonical(
        {"schema": "lf023_authenticated_agreement_artifact_v1", **payload}
    )
    artifact = AuthenticatedAgreementArtifactV1.model_validate(
        {"artifact_id": artifact_id, **payload}
    )
    _write_private_immutable(
        output_path,
        canonical_json_bytes(artifact.model_dump(mode="json")),
    )
    restored = load_authenticated_agreement_artifact(
        repo_root=repo_root,
        path=output_path,
        authentication_key_path=authentication_key_path,
    )
    return AgreementArtifactRun(artifact=restored, path=output_path.resolve(strict=True))


def _load_policy_trigger_set(path: Path | None) -> AdjudicationPolicyTriggerSetV1:
    if path is None:
        return AdjudicationPolicyTriggerSetV1(
            policy_id="annotation_codebook_v1#adjudication_triggers"
        )
    resolved = _resolve_private_input(path)
    try:
        policy = AdjudicationPolicyTriggerSetV1.model_validate(_load_json_object(resolved))
    except ValueError as exc:
        raise AnnotationOperationError(f"invalid policy-trigger artifact: {exc}") from exc
    return policy


def write_authenticated_adjudication_queue(
    *,
    repo_root: Path,
    first_import_manifest_path: Path,
    second_import_manifest_path: Path,
    authentication_key_path: Path,
    output_path: Path,
    policy_trigger_set_path: Path | None = None,
) -> AdjudicationArtifactRun:
    """Write and reload an immutable routing queue without resolving labels."""

    first = load_verified_annotation_import(
        repo_root=repo_root,
        manifest_path=first_import_manifest_path,
        authentication_key_path=authentication_key_path,
    )
    second = load_verified_annotation_import(
        repo_root=repo_root,
        manifest_path=second_import_manifest_path,
        authentication_key_path=authentication_key_path,
    )
    _require_complete_distinct_imports(first.manifest, second.manifest)
    policy = _load_policy_trigger_set(policy_trigger_set_path)
    policy_binding = (
        None if policy_trigger_set_path is None else _binding(repo_root, policy_trigger_set_path)
    )
    queue = build_adjudication_queue(
        first.annotations,
        second.annotations,
        policy_trigger_targets=tuple((item.target_kind, item.target_id) for item in policy.targets),
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "lf023_authenticated_adjudication_artifact_v1",
        "first_import_manifest": _binding(repo_root, first.manifest_path).model_dump(mode="json"),
        "second_import_manifest": _binding(repo_root, second.manifest_path).model_dump(mode="json"),
        "policy_trigger_set": (
            None if policy_binding is None else policy_binding.model_dump(mode="json")
        ),
        "queue": queue.model_dump(mode="json"),
        "authenticated_imports_reverified": True,
        "source_imports_complete": True,
        "raw_annotations_only": True,
        "semantic_labels_created": False,
        "adjudications_created": False,
        "automatic_resolutions_created": False,
        "gold_labels_created": False,
        "training_eligible": False,
        "private": True,
        "release_eligible": False,
    }
    artifact_id = "lf023_authenticated_adjudication_artifact_v1:" + hash_canonical(
        {"schema": "lf023_authenticated_adjudication_artifact_v1", **payload}
    )
    artifact = AuthenticatedAdjudicationArtifactV1.model_validate(
        {"artifact_id": artifact_id, **payload}
    )
    _write_private_immutable(
        output_path,
        canonical_json_bytes(artifact.model_dump(mode="json")),
    )
    restored = load_authenticated_adjudication_artifact(
        repo_root=repo_root,
        path=output_path,
        authentication_key_path=authentication_key_path,
    )
    return AdjudicationArtifactRun(artifact=restored, path=output_path.resolve(strict=True))


def load_authenticated_agreement_artifact(
    *,
    repo_root: Path,
    path: Path,
    authentication_key_path: Path,
) -> AuthenticatedAgreementArtifactV1:
    """Reload an agreement and reauthenticate both complete raw imports."""

    resolved = _resolve_private_input(path)
    artifact = AuthenticatedAgreementArtifactV1.model_validate(_load_json_object(resolved))
    first = load_verified_annotation_import(
        repo_root=repo_root,
        manifest_path=_resolve_binding(repo_root, artifact.first_import_manifest),
        authentication_key_path=authentication_key_path,
    )
    second = load_verified_annotation_import(
        repo_root=repo_root,
        manifest_path=_resolve_binding(repo_root, artifact.second_import_manifest),
        authentication_key_path=authentication_key_path,
    )
    _require_complete_distinct_imports(first.manifest, second.manifest)
    expected = compute_annotation_agreement(first.annotations, second.annotations)
    if artifact.report != expected:
        raise AnnotationOperationError(
            "agreement artifact differs from its reauthenticated raw imports"
        )
    return artifact


def load_authenticated_adjudication_artifact(
    *,
    repo_root: Path,
    path: Path,
    authentication_key_path: Path,
) -> AuthenticatedAdjudicationArtifactV1:
    """Reload a queue and reauthenticate its raw imports and trigger policy."""

    resolved = _resolve_private_input(path)
    artifact = AuthenticatedAdjudicationArtifactV1.model_validate(_load_json_object(resolved))
    first = load_verified_annotation_import(
        repo_root=repo_root,
        manifest_path=_resolve_binding(repo_root, artifact.first_import_manifest),
        authentication_key_path=authentication_key_path,
    )
    second = load_verified_annotation_import(
        repo_root=repo_root,
        manifest_path=_resolve_binding(repo_root, artifact.second_import_manifest),
        authentication_key_path=authentication_key_path,
    )
    _require_complete_distinct_imports(first.manifest, second.manifest)
    policy_path = (
        None
        if artifact.policy_trigger_set is None
        else _resolve_binding(repo_root, artifact.policy_trigger_set)
    )
    policy = _load_policy_trigger_set(policy_path)
    expected = build_adjudication_queue(
        first.annotations,
        second.annotations,
        policy_trigger_targets=tuple((item.target_kind, item.target_id) for item in policy.targets),
    )
    if artifact.queue != expected:
        raise AnnotationOperationError(
            "adjudication artifact differs from its reauthenticated raw inputs"
        )
    return artifact


__all__ = [
    "AdjudicationArtifactRun",
    "AdjudicationPolicyTargetV1",
    "AdjudicationPolicyTriggerSetV1",
    "AgreementArtifactRun",
    "AnnotationOperationError",
    "AuthenticatedAdjudicationArtifactV1",
    "AuthenticatedAgreementArtifactV1",
    "HumanAssignmentRun",
    "SubmissionAttestationRun",
    "attest_human_submission",
    "create_authenticated_human_assignment",
    "load_authenticated_adjudication_artifact",
    "load_authenticated_agreement_artifact",
    "write_authenticated_adjudication_queue",
    "write_authenticated_agreement",
]
