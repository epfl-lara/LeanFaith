"""Authenticated LF-023 human-assignment and submission attestations.

Content hashes and the operator HMAC prove artifact integrity and that a
trusted local operator made the recorded assertions.  They do not
independently prove human identity, annotator independence, or backend origin.
This module adds a deliberately small trust boundary for the operational
annotation handoff:

* an assignment is fixed before responses are created;
* a trusted local operator authenticates the assignment with a private key;
* a second authenticated record binds the exact project-captured backend
  response export; and
* import verifies both records before it may describe a response as human.

The authentication construction is domain-separated HMAC-SHA256 from the
Python standard library.  Authentication keys are operational secrets and
must remain mode-0600 files outside version control.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from leanfaith.annotation_support.export import ArtifactBinding
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import require_utc

_HEX64 = r"^[0-9a-f]{64}$"
_PATH_SEGMENT = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_BLIND_BUNDLE_MANIFEST_ID = r"^lf023_blinded_bundle_manifest_v1:[0-9a-f]{64}$"
_ASSIGNMENT_ID = r"^lf023_human_assignment_v1:[0-9a-f]{64}$"
_ATTESTATION_ID = r"^lf023_human_submission_attestation_v1:[0-9a-f]{64}$"
_ASSIGNMENT_DOMAIN = b"leanfaith-lf023-human-assignment-hmac-sha256-v1\x00"
_ATTESTATION_DOMAIN = b"leanfaith-lf023-human-submission-attestation-hmac-sha256-v1\x00"
_PRODUCTION_BACKENDS = frozenset(
    {
        "argilla",
        "label_studio",
        "streamlit_documented_fallback",
    }
)


class AnnotationAttestationError(ValueError):
    """Raised when trusted-operator artifact authentication fails."""


def load_authentication_key(path: Path) -> bytes:
    """Load a private authentication key after filesystem checks.

    The caller resolves and checks mode/symlink constraints before reaching
    this helper.  Keeping the length check here makes direct library use fail
    closed as well.
    """

    key = path.read_bytes()
    if len(key) < 32:
        raise AnnotationAttestationError("annotation authentication key must be at least 32 bytes")
    return key


def authentication_key_id(key: bytes) -> str:
    """Return a non-secret identifier for an authentication key."""

    if len(key) < 32:
        raise AnnotationAttestationError("annotation authentication key must be at least 32 bytes")
    return sha256_hex(key)


class HumanAnnotationAssignmentContentV1(StrictModel):
    """Pre-response assignment covered by an authenticated envelope."""

    schema_version: Literal[1] = 1
    campaign_id: Literal["lf021_prevalence_v1"]
    round_id: str = Field(pattern=_PATH_SEGMENT)
    annotator_slot: Literal["independent_annotator_1", "independent_annotator_2"]
    annotator_id: str = Field(min_length=1)
    annotator_principal_hash: str = Field(pattern=_HEX64)
    assignment_mode: Literal["operator_attested_human", "test_fixture"]
    backend_id: str = Field(min_length=1)
    assigned_at: datetime.datetime
    public_bundle_manifest: ArtifactBinding
    private_linkage_manifest: ArtifactBinding
    bundle_manifest_id: str = Field(pattern=_BLIND_BUNDLE_MANIFEST_ID)
    guideline: ArtifactBinding
    authentication_key_id: str = Field(pattern=_HEX64)
    independent_assignment: Literal[True] = True
    response_not_yet_observed: Literal[True] = True

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.assigned_at)
        if self.assignment_mode == "operator_attested_human":
            if self.backend_id not in _PRODUCTION_BACKENDS:
                raise ValueError(
                    "operator-attested assignment requires a registered production backend"
                )
        elif self.backend_id != "pytest_fixture_backend":
            raise ValueError("test-fixture assignment requires the fixture backend")
        return self


def make_human_assignment_id(
    value: HumanAnnotationAssignmentContentV1 | dict[str, Any],
) -> str:
    """Return the deterministic assignment identifier."""

    content = (
        value
        if isinstance(value, HumanAnnotationAssignmentContentV1)
        else HumanAnnotationAssignmentContentV1.model_validate(value)
    )
    return "lf023_human_assignment_v1:" + hash_canonical(
        {
            "schema": "lf023_human_assignment_v1",
            **content.model_dump(mode="json"),
        }
    )


def _authentication_tag(domain: bytes, payload: dict[str, Any], key: bytes) -> str:
    return hmac.new(key, domain + canonical_json_bytes(payload), hashlib.sha256).hexdigest()


class HumanAnnotationAssignmentEnvelopeV1(HumanAnnotationAssignmentContentV1):
    """Authenticated pre-response assignment."""

    assignment_id: str = Field(pattern=_ASSIGNMENT_ID)
    authentication_tag: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _content_id(self) -> Self:
        content = HumanAnnotationAssignmentContentV1.model_validate(
            self.model_dump(mode="json", exclude={"assignment_id", "authentication_tag"})
        )
        if self.assignment_id != make_human_assignment_id(content):
            raise ValueError("human assignment ID differs from normalized content")
        return self


def authenticate_human_assignment(
    content: HumanAnnotationAssignmentContentV1,
    *,
    key: bytes,
) -> HumanAnnotationAssignmentEnvelopeV1:
    """Authenticate one assignment with the registered private key."""

    if content.authentication_key_id != authentication_key_id(key):
        raise AnnotationAttestationError("assignment authentication key ID differs")
    assignment_id = make_human_assignment_id(content)
    payload = {
        "schema": "lf023_human_assignment_authentication_v1",
        "assignment_id": assignment_id,
        "content": content.model_dump(mode="json"),
    }
    return HumanAnnotationAssignmentEnvelopeV1(
        **content.model_dump(mode="python"),
        assignment_id=assignment_id,
        authentication_tag=_authentication_tag(_ASSIGNMENT_DOMAIN, payload, key),
    )


def verify_human_assignment(
    assignment: HumanAnnotationAssignmentEnvelopeV1,
    *,
    key: bytes,
) -> None:
    """Verify assignment key identity and authentication tag."""

    if assignment.authentication_key_id != authentication_key_id(key):
        raise AnnotationAttestationError("assignment authentication key ID differs")
    content = HumanAnnotationAssignmentContentV1.model_validate(
        assignment.model_dump(mode="json", exclude={"assignment_id", "authentication_tag"})
    )
    payload = {
        "schema": "lf023_human_assignment_authentication_v1",
        "assignment_id": assignment.assignment_id,
        "content": content.model_dump(mode="json"),
    }
    expected = _authentication_tag(_ASSIGNMENT_DOMAIN, payload, key)
    if not hmac.compare_digest(assignment.authentication_tag, expected):
        raise AnnotationAttestationError("human assignment authentication failed")


class HumanSubmissionAttestationContentV1(StrictModel):
    """Trusted binding of an assignment to one exact response-export snapshot.

    ``backend_export_locked`` is the version-1 serialized spelling for a
    project-owned immutable export artifact. It does not claim that the
    mutable backend response row itself is locked.
    """

    schema_version: Literal[1] = 1
    assignment_id: str = Field(pattern=_ASSIGNMENT_ID)
    assignment_artifact: ArtifactBinding
    response_artifact: ArtifactBinding
    backend_export_id: str = Field(min_length=1)
    verifier_id: str = Field(min_length=1)
    attested_at: datetime.datetime
    authentication_key_id: str = Field(pattern=_HEX64)
    assignment_mode: Literal["operator_attested_human", "test_fixture"]
    operator_human_origin_asserted: bool
    origin_assurance: Literal["operator_attested", "test_fixture"]
    operator_attestation_verified: Literal[True] = True
    backend_origin_verified: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    fixture_only: bool
    backend_export_locked: Literal[True] = True
    raw_votes_only: Literal[True] = True
    resolved_labels_created: Literal[False] = False
    gold_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _utc(self) -> Self:
        require_utc(self.attested_at)
        if self.operator_human_origin_asserted != (
            self.assignment_mode == "operator_attested_human"
        ):
            raise ValueError("operator human-origin assertion differs from assignment mode")
        expected_assurance = (
            "operator_attested"
            if self.assignment_mode == "operator_attested_human"
            else "test_fixture"
        )
        if self.origin_assurance != expected_assurance:
            raise ValueError("origin assurance differs from assignment mode")
        if self.fixture_only != (self.assignment_mode == "test_fixture"):
            raise ValueError("submission fixture flag differs from assignment mode")
        return self


def make_human_submission_attestation_id(
    value: HumanSubmissionAttestationContentV1 | dict[str, Any],
) -> str:
    """Return the deterministic submission-attestation identifier."""

    content = (
        value
        if isinstance(value, HumanSubmissionAttestationContentV1)
        else HumanSubmissionAttestationContentV1.model_validate(value)
    )
    return "lf023_human_submission_attestation_v1:" + hash_canonical(
        {
            "schema": "lf023_human_submission_attestation_v1",
            **content.model_dump(mode="json"),
        }
    )


class HumanSubmissionAttestationEnvelopeV1(HumanSubmissionAttestationContentV1):
    """Authenticated operator attestation for an exact response artifact."""

    attestation_id: str = Field(pattern=_ATTESTATION_ID)
    authentication_tag: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _content_id(self) -> Self:
        content = HumanSubmissionAttestationContentV1.model_validate(
            self.model_dump(mode="json", exclude={"attestation_id", "authentication_tag"})
        )
        if self.attestation_id != make_human_submission_attestation_id(content):
            raise ValueError("human submission attestation ID differs from normalized content")
        return self


def authenticate_human_submission(
    content: HumanSubmissionAttestationContentV1,
    *,
    key: bytes,
) -> HumanSubmissionAttestationEnvelopeV1:
    """Authenticate one exact human-response export."""

    if content.authentication_key_id != authentication_key_id(key):
        raise AnnotationAttestationError("submission authentication key ID differs")
    attestation_id = make_human_submission_attestation_id(content)
    payload = {
        "schema": "lf023_human_submission_attestation_authentication_v1",
        "attestation_id": attestation_id,
        "content": content.model_dump(mode="json"),
    }
    return HumanSubmissionAttestationEnvelopeV1(
        **content.model_dump(mode="python"),
        attestation_id=attestation_id,
        authentication_tag=_authentication_tag(_ATTESTATION_DOMAIN, payload, key),
    )


def verify_human_submission_attestation(
    attestation: HumanSubmissionAttestationEnvelopeV1,
    *,
    key: bytes,
) -> None:
    """Verify submission key identity and authentication tag."""

    if attestation.authentication_key_id != authentication_key_id(key):
        raise AnnotationAttestationError("submission authentication key ID differs")
    content = HumanSubmissionAttestationContentV1.model_validate(
        attestation.model_dump(mode="json", exclude={"attestation_id", "authentication_tag"})
    )
    payload = {
        "schema": "lf023_human_submission_attestation_authentication_v1",
        "attestation_id": attestation.attestation_id,
        "content": content.model_dump(mode="json"),
    }
    expected = _authentication_tag(_ATTESTATION_DOMAIN, payload, key)
    if not hmac.compare_digest(attestation.authentication_tag, expected):
        raise AnnotationAttestationError("human submission authentication failed")


__all__ = [
    "AnnotationAttestationError",
    "HumanAnnotationAssignmentContentV1",
    "HumanAnnotationAssignmentEnvelopeV1",
    "HumanSubmissionAttestationContentV1",
    "HumanSubmissionAttestationEnvelopeV1",
    "authenticate_human_assignment",
    "authenticate_human_submission",
    "authentication_key_id",
    "load_authentication_key",
    "make_human_assignment_id",
    "make_human_submission_attestation_id",
    "verify_human_assignment",
    "verify_human_submission_attestation",
]
