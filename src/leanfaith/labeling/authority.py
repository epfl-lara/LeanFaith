"""Fail-closed local authority-artifact infrastructure for LF-024.

This module verifies only artifact transport and immutable bindings.  It does
not decide that any artifact is semantic authority: every route is disabled
in version 1, every receipt is non-production, and no API here can emit a
``ResolutionCandidate`` or ``ResolvedLabel``.  Route-specific adapters may be
added later only after their source artifacts and replay checks are typed.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import JsonValue, canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.labeling.quality import (
    ActiveLabelResolutionPolicy,
    AuthorityArtifactBinding,
    AuthorityArtifactKind,
    CandidateCommitment,
    ResolutionSource,
    make_authority_artifact_binding,
)
from leanfaith.schemas.enums import (
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    HEX64_PATTERN,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.nl_lean import NLPLeanRecord
from leanfaith.schemas.pair import PairRecord
from leanfaith.schemas.variant import _check_ecodes

AUTHORITY_VERIFICATION_PREFIX = "authority_verification"
AUTHORITY_VERIFIER_VERSION: Literal["authority_artifact_transport_v1"] = (
    "authority_artifact_transport_v1"
)


class AuthorityArtifactError(ValueError):
    """A local authority artifact is unavailable, mutable, or malformed."""


class AuthorityRouteDisabledError(AuthorityArtifactError):
    """A caller attempted to use a route before its typed verifier exists."""


class AuthorityArtifactSerialization(StrEnum):
    CANONICAL_JSON = "canonical_json"
    CANONICAL_JSONL = "canonical_jsonl"


_ID_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AuthorityArtifactReferenceV1(StrictModel):
    """Expected identity and bytes of one local authority-support artifact."""

    schema_version: Literal[1] = 1
    artifact_kind: AuthorityArtifactKind
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)
    serialization: AuthorityArtifactSerialization
    expected_artifact_id: str | None = Field(default=None, min_length=1)
    artifact_id_field: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _reference_shape(self) -> Self:
        if "\x00" in self.artifact:
            raise ValueError("artifact path cannot contain NUL")
        if self.serialization is AuthorityArtifactSerialization.CANONICAL_JSON:
            if self.expected_artifact_id is None or self.artifact_id_field is None:
                raise ValueError(
                    "canonical JSON authority references require expected_artifact_id "
                    "and artifact_id_field"
                )
            if _ID_FIELD_PATTERN.fullmatch(self.artifact_id_field) is None:
                raise ValueError("artifact_id_field must be a simple JSON object field name")
        elif self.expected_artifact_id is not None or self.artifact_id_field is not None:
            raise ValueError(
                "canonical JSONL authority references are identified only by exact file SHA-256"
            )
        return self


CanonicalArtifactPayload = dict[str, JsonValue] | tuple[dict[str, JsonValue], ...]


@dataclass(frozen=True, slots=True)
class LoadedAuthorityArtifact:
    """Operational result of a stable, canonical, contained local read."""

    reference: AuthorityArtifactReferenceV1
    resolved_path: Path
    containment_root: Path
    payload: CanonicalArtifactPayload
    binding: AuthorityArtifactBinding


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorityArtifactError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise AuthorityArtifactError(f"non-finite JSON constant {value!r}")


def _parse_json_object(
    payload: bytes,
    *,
    location: str,
    trailing_lf: bool,
) -> dict[str, JsonValue]:
    json_payload = payload[:-1] if trailing_lf and payload.endswith(b"\n") else payload
    try:
        value = json.loads(
            json_payload,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityArtifactError(f"invalid UTF-8 JSON at {location}: {exc}") from exc
    if not isinstance(value, dict):
        raise AuthorityArtifactError(f"authority JSON at {location} must be an object")
    canonical = canonical_json_bytes(value) + (b"\n" if trailing_lf else b"")
    if canonical != payload:
        raise AuthorityArtifactError(f"authority JSON at {location} is not canonical")
    return value


def _parse_canonical_payload(
    payload: bytes,
    *,
    serialization: AuthorityArtifactSerialization,
) -> CanonicalArtifactPayload:
    if serialization is AuthorityArtifactSerialization.CANONICAL_JSON:
        # Repository JSON artifacts conventionally contain exactly one
        # canonical object followed by exactly one LF.  Make that wire format
        # part of the authority hash rather than accepting two spellings.
        return _parse_json_object(payload, location="document", trailing_lf=True)
    if not payload or not payload.endswith(b"\n"):
        raise AuthorityArtifactError("authority JSONL must be nonempty and end in LF")
    lines = payload.splitlines()
    if any(not line for line in lines):
        raise AuthorityArtifactError("authority JSONL cannot contain blank rows")
    return tuple(
        _parse_json_object(line, location=f"row {line_number}", trailing_lf=False)
        for line_number, line in enumerate(lines, start=1)
    )


def _absolute_without_symlink_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _directory_open_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise AuthorityArtifactError(
            "authority artifact loading requires O_DIRECTORY and O_NOFOLLOW support"
        )
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_chain(path: Path) -> int:
    """Open an absolute directory one component at a time without symlinks."""

    if not path.is_absolute():
        raise AuthorityArtifactError("authority containment root must be absolute")
    flags = _directory_open_flags()
    try:
        current_fd = os.open(path.anchor, flags)
        try:
            for component in path.parts[1:]:
                next_fd = os.open(component, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
        except BaseException:
            os.close(current_fd)
            raise
    except OSError as exc:
        raise AuthorityArtifactError(
            f"authority containment root contains a symlink or is unavailable: {path}"
        ) from exc
    return current_fd


def _validate_containment_root(path: Path, *, description: str) -> Path:
    normalized = _absolute_without_symlink_resolution(path)
    root_fd = _open_directory_chain(normalized)
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise AuthorityArtifactError(f"{description} is not a directory: {normalized}")
    finally:
        os.close(root_fd)
    return normalized


def _reject_symlink_components(path: Path, *, root: Path) -> None:
    """Reject every artifact component that is a symlink, including the leaf."""

    relative = path.relative_to(root)
    if not relative.parts:
        raise AuthorityArtifactError("authority artifact cannot be a containment root")
    current = root
    for index, component in enumerate(relative.parts):
        current /= component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise AuthorityArtifactError(f"authority artifact is unavailable: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise AuthorityArtifactError(
                f"authority artifact paths cannot contain symlinks: {current}"
            )
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise AuthorityArtifactError(f"authority artifact parent is not a directory: {current}")
    if not stat.S_ISREG(metadata.st_mode):
        raise AuthorityArtifactError(f"authority artifact is not a regular file: {path}")


def _resolve_contained_path(
    reference: AuthorityArtifactReferenceV1,
    *,
    repo_root: Path,
    private_roots: tuple[Path, ...],
) -> tuple[Path, Path]:
    root = _validate_containment_root(repo_root, description="repository root")

    resolved_private: list[Path] = []
    for private_root in private_roots:
        resolved = _validate_containment_root(
            private_root,
            description="registered private root",
        )
        if resolved not in resolved_private:
            resolved_private.append(resolved)

    raw_path = Path(reference.artifact)
    candidate = _absolute_without_symlink_resolution(
        raw_path if raw_path.is_absolute() else root / raw_path
    )

    if not raw_path.is_absolute():
        if not candidate.is_relative_to(root):
            raise AuthorityArtifactError("relative authority artifact escapes repository root")
        _reject_symlink_components(candidate, root=root)
        return candidate, root

    for allowed_root in (root, *resolved_private):
        if candidate.is_relative_to(allowed_root):
            _reject_symlink_components(candidate, root=allowed_root)
            return candidate, allowed_root
    raise AuthorityArtifactError("absolute authority artifact is outside registered roots")


def _read_all_fd(file_descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(file_descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _open_contained_file(path: Path, *, root: Path) -> int:
    """Open a file from an already-authorized root without following symlinks."""

    relative = path.relative_to(root)
    directory_fd = _open_directory_chain(root)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, _directory_open_flags(), dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(relative.parts[-1], flags, dir_fd=directory_fd)
    except OSError as exc:
        raise AuthorityArtifactError(
            f"authority artifact contains a symlink or is unavailable: {path}"
        ) from exc
    finally:
        os.close(directory_fd)
    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
        os.close(file_fd)
        raise AuthorityArtifactError(f"authority artifact is not a regular file: {path}")
    return file_fd


def _stable_read(path: Path, *, root: Path, expected_sha256: str) -> bytes:
    """Securely read twice and require stable identity, bytes, and hash."""

    file_fd = _open_contained_file(path, root=root)
    try:
        pre_stat = os.fstat(file_fd)
        payload = _read_all_fd(file_fd)
        os.lseek(file_fd, 0, os.SEEK_SET)
        post_payload = _read_all_fd(file_fd)
        post_stat = os.fstat(file_fd)
    except OSError as exc:
        raise AuthorityArtifactError(f"cannot read authority artifact: {path}") from exc
    finally:
        os.close(file_fd)

    def stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    if stat_identity(pre_stat) != stat_identity(post_stat):
        raise AuthorityArtifactError("authority artifact changed during read")
    payload_hash = sha256_hex(payload)
    post_hash = sha256_hex(post_payload)
    if payload != post_payload or payload_hash != post_hash:
        raise AuthorityArtifactError("authority artifact hash changed during read")
    if payload_hash != expected_sha256:
        raise AuthorityArtifactError(
            f"authority artifact SHA-256 mismatch: expected {expected_sha256}, got {payload_hash}"
        )
    return payload


def load_local_authority_artifact(
    reference: AuthorityArtifactReferenceV1,
    *,
    repo_root: Path,
    private_roots: tuple[Path, ...] = (),
) -> LoadedAuthorityArtifact:
    """Load one exact local artifact without granting semantic authority."""

    path, containment_root = _resolve_contained_path(
        reference,
        repo_root=repo_root,
        private_roots=private_roots,
    )
    raw = _stable_read(path, root=containment_root, expected_sha256=reference.sha256)
    payload = _parse_canonical_payload(raw, serialization=reference.serialization)
    if isinstance(payload, dict):
        assert reference.artifact_id_field is not None
        assert reference.expected_artifact_id is not None
        observed = payload.get(reference.artifact_id_field)
        if observed != reference.expected_artifact_id:
            raise AuthorityArtifactError(
                f"authority artifact ID mismatch in field {reference.artifact_id_field!r}"
            )
        artifact_id = reference.expected_artifact_id
    else:
        artifact_id = f"sha256:{reference.sha256}"
    binding = make_authority_artifact_binding(
        artifact_kind=reference.artifact_kind,
        artifact_id=artifact_id,
        artifact_sha256=reference.sha256,
    )
    return LoadedAuthorityArtifact(
        reference=reference,
        resolved_path=path,
        containment_root=containment_root,
        payload=payload,
        binding=binding,
    )


_NEGATIVE_RELATIONS = frozenset(
    {
        RelationLabel.A_STRONGER,
        RelationLabel.B_STRONGER,
        RelationLabel.INCOMPARABLE,
        RelationLabel.UNRELATED,
    }
)


class AuthoritySemanticProjectionV1(StrictModel):
    """Route-derived semantic content bound by a disabled verification receipt."""

    schema_version: Literal[1] = 1
    same_claim: bool | None
    resolution_outcome: ResolutionOutcome
    relation: RelationLabel | None
    F0_representation_equivalent: bool | None = None
    truth_A_implies_B: bool | None = None
    truth_B_implies_A: bool | None = None
    F2_truth_equivalent: bool | None = None
    error_types: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _semantic_coherence(self) -> Self:
        if self.error_types != tuple(sorted(set(self.error_types))):
            raise ValueError("error_types must be sorted and unique")
        _check_ecodes(self.error_types)
        if self.same_claim is True:
            if not (
                self.resolution_outcome is ResolutionOutcome.SAME_CLAIM
                and self.relation is RelationLabel.EQUIVALENT
            ):
                raise ValueError("same-claim projection requires equivalent relation")
            if any(code != "E29" for code in self.error_types):
                raise ValueError("same-claim projection admits only cosmetic E29")
        elif self.same_claim is False:
            if not (
                self.resolution_outcome is ResolutionOutcome.NOT_SAME_CLAIM
                and self.relation in _NEGATIVE_RELATIONS | {None}
            ):
                raise ValueError("not-same projection requires a negative or partial relation")
        elif not (
            self.resolution_outcome is ResolutionOutcome.AMBIGUOUS
            and self.relation is RelationLabel.AMBIGUOUS
        ):
            raise ValueError("null same_claim is reserved for terminal ambiguity")

        if self.truth_A_implies_B is True and self.truth_B_implies_A is True:
            expected_f2: bool | None = True
        elif self.truth_A_implies_B is False or self.truth_B_implies_A is False:
            expected_f2 = False
        else:
            expected_f2 = None
        if self.F2_truth_equivalent is not expected_f2:
            raise ValueError("F2_truth_equivalent disagrees with directional truth fields")
        if self.same_claim is True and expected_f2 is False:
            raise ValueError("same-claim projection conflicts with F2 refutation")
        return self


_ROUTE_ENABLEMENT = MappingProxyType(
    {
        AuthorityArtifactKind.HUMAN_ADJUDICATION: False,
        AuthorityArtifactKind.FROZEN_BENCHMARK_LABEL: False,
        AuthorityArtifactKind.CONSERVATIVE_FAMILY_PROMOTION: False,
        AuthorityArtifactKind.CERTIFICATE_OR_SEPARATOR: False,
        AuthorityArtifactKind.INDEPENDENT_CONSENSUS_PROMOTION: False,
    }
)


def authority_route_enabled(kind: AuthorityArtifactKind) -> bool:
    """Return false for every semantic authority route in infrastructure v1."""

    return _ROUTE_ENABLEMENT.get(kind, False)


def require_authority_route_enabled(kind: AuthorityArtifactKind) -> None:
    """Fail before any semantic projection can become a candidate or label."""

    if not authority_route_enabled(kind):
        raise AuthorityRouteDisabledError(
            f"authority route {kind.value!r} is disabled pending a typed route verifier"
        )


_METHOD_SOURCE: dict[str, ResolutionSource] = {
    "expert_adjudication": ResolutionSource.HUMAN_ADJUDICATION,
    "expert_binder_aligned_claim_comparison": ResolutionSource.HUMAN_ADJUDICATION,
    "benchmark_import": ResolutionSource.FROZEN_BENCHMARK_POLICY,
    "p01_alpha_certificate": ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
    "separator_certificate": ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
    "directional_proof_plus_separator": ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
    "independent_consensus_audited": ResolutionSource.PROMOTED_INDEPENDENT_CONSENSUS,
}

_SOURCE_TIER_KIND: dict[tuple[ResolutionSource, QualityTier], AuthorityArtifactKind] = {
    (
        ResolutionSource.HUMAN_ADJUDICATION,
        QualityTier.GOLD_HUMAN,
    ): AuthorityArtifactKind.HUMAN_ADJUDICATION,
    (
        ResolutionSource.FROZEN_BENCHMARK_POLICY,
        QualityTier.BENCHMARK,
    ): AuthorityArtifactKind.FROZEN_BENCHMARK_LABEL,
    (
        ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
    ): AuthorityArtifactKind.CONSERVATIVE_FAMILY_PROMOTION,
    (
        ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        QualityTier.GOLD_COUNTEREXAMPLE,
    ): AuthorityArtifactKind.CERTIFICATE_OR_SEPARATOR,
    (
        ResolutionSource.PROMOTED_INDEPENDENT_CONSENSUS,
        QualityTier.SILVER_CONSENSUS,
    ): AuthorityArtifactKind.INDEPENDENT_CONSENSUS_PROMOTION,
}

_TARGET_PATTERNS = {
    SemanticLabelTargetKind.LEAN_PAIR: id_pattern(PAIR_PREFIX),
    SemanticLabelTargetKind.NL_LEAN: id_pattern(NL_LEAN_PREFIX),
}

_REQUIRED_CHECKS = (
    "artifact_canonical",
    "artifact_hash_stable",
    "artifact_id_matches",
    "artifact_path_contained",
    "authority_route_disabled",
    "policy_and_gate_bound",
    "caller_projection_hash_bound_unverified",
    "target_content_bound",
)


class AuthorityVerificationReceiptV1(StrictModel):
    """Content-addressed, explicitly non-production infrastructure receipt."""

    schema_version: Literal[1] = 1
    receipt_id: str = Field(pattern=id_pattern(AUTHORITY_VERIFICATION_PREFIX))
    verifier_version: Literal["authority_artifact_transport_v1"] = AUTHORITY_VERIFIER_VERSION
    primary_artifact: AuthorityArtifactBinding
    supporting_artifacts: tuple[AuthorityArtifactBinding, ...] = ()
    target_kind: SemanticLabelTargetKind
    target_id: str
    target_sha256: str = Field(pattern=HEX64_PATTERN)
    policy_version: Literal["label_resolution_v1"] = "label_resolution_v1"
    policy_file_sha256: str = Field(pattern=HEX64_PATTERN)
    gate_file_sha256: str = Field(pattern=HEX64_PATTERN)
    source: ResolutionSource
    quality_tier: QualityTier
    resolution_method: str = Field(min_length=1)
    commitment: CandidateCommitment
    semantic_projection: AuthoritySemanticProjectionV1
    accepted_evidence_ids: tuple[str, ...] = ()
    provenance: tuple[str, ...] = Field(min_length=1)
    verification_check_codes: tuple[str, ...]
    route_enabled: Literal[False] = False
    production_eligible: Literal[False] = False
    candidate_emitted: Literal[False] = False
    label_emitted: Literal[False] = False

    @model_validator(mode="after")
    def _disabled_content_addressed_receipt(self) -> Self:
        if re.fullmatch(_TARGET_PATTERNS[self.target_kind], self.target_id) is None:
            raise ValueError("target_id does not match target_kind")
        support_keys = tuple(
            (item.artifact_kind.value, item.artifact_id, item.artifact_sha256)
            for item in self.supporting_artifacts
        )
        if support_keys != tuple(sorted(set(support_keys))):
            raise ValueError("supporting_artifacts must be sorted and unique")
        primary_key = (
            self.primary_artifact.artifact_kind.value,
            self.primary_artifact.artifact_id,
            self.primary_artifact.artifact_sha256,
        )
        if primary_key in support_keys:
            raise ValueError("primary artifact cannot be duplicated as supporting authority")
        if self.accepted_evidence_ids != tuple(sorted(set(self.accepted_evidence_ids))):
            raise ValueError("accepted_evidence_ids must be sorted and unique")
        evidence_pattern = id_pattern(EVIDENCE_PREFIX)
        if any(
            re.fullmatch(evidence_pattern, evidence_id) is None
            for evidence_id in self.accepted_evidence_ids
        ):
            raise ValueError("accepted_evidence_ids must contain only canonical ev: IDs")
        if self.provenance != tuple(sorted(set(self.provenance))):
            raise ValueError("provenance must be sorted, unique, and nonempty")
        if any(not item.strip() for item in self.provenance):
            raise ValueError("provenance entries must be nonempty")
        if self.verification_check_codes != _REQUIRED_CHECKS:
            raise ValueError("verification_check_codes differ from disabled v1 contract")
        if _METHOD_SOURCE.get(self.resolution_method) is not self.source:
            raise ValueError("resolution method does not match authority source")
        required_kind = _SOURCE_TIER_KIND.get((self.source, self.quality_tier))
        if required_kind is None or self.primary_artifact.artifact_kind is not required_kind:
            raise ValueError("primary artifact kind does not match source and quality tier")
        if authority_route_enabled(self.primary_artifact.artifact_kind):
            raise ValueError("disabled infrastructure receipt cannot bind an enabled route")

        projection = self.semantic_projection
        if self.commitment is CandidateCommitment.PARTIAL_NEGATIVE:
            if not (
                self.resolution_method == "separator_certificate"
                and self.quality_tier is QualityTier.GOLD_COUNTEREXAMPLE
                and projection.same_claim is False
                and projection.relation is None
            ):
                raise ValueError("partial negative is reserved for an unclassified separator")
        elif projection.same_claim is False and projection.relation is None:
            raise ValueError("terminal negative receipt requires a terminal relation")
        if self.quality_tier is QualityTier.GOLD_CONSERVATIVE_TRANSFORM and (
            projection.same_claim is not True
        ):
            raise ValueError("conservative authority receipt must be same-claim positive")
        if self.quality_tier is QualityTier.GOLD_COUNTEREXAMPLE and (
            projection.same_claim is not False
        ):
            raise ValueError("counterexample authority receipt must be not-same negative")

        expected_id = make_id(
            AUTHORITY_VERIFICATION_PREFIX,
            self.model_dump(mode="json", exclude={"receipt_id"}),
        )
        if self.receipt_id != expected_id:
            raise ValueError("receipt_id does not match verification content")
        return self


AuthorityTarget = PairRecord | NLPLeanRecord


def _target_binding(
    target: AuthorityTarget,
) -> tuple[SemanticLabelTargetKind, str, str]:
    if isinstance(target, PairRecord):
        kind = SemanticLabelTargetKind.LEAN_PAIR
        target_id = target.pair_id
    else:
        kind = SemanticLabelTargetKind.NL_LEAN
        target_id = target.nl_lean_id
    return kind, target_id, hash_canonical(target.model_dump(mode="json"))


def build_disabled_authority_verification_receipt(
    *,
    primary: LoadedAuthorityArtifact,
    supporting: tuple[LoadedAuthorityArtifact, ...],
    target: AuthorityTarget,
    policy: ActiveLabelResolutionPolicy,
    source: ResolutionSource,
    quality_tier: QualityTier,
    resolution_method: str,
    commitment: CandidateCommitment,
    semantic_projection: AuthoritySemanticProjectionV1,
    accepted_evidence_ids: tuple[str, ...],
    provenance: tuple[str, ...],
) -> AuthorityVerificationReceiptV1:
    """Record verified bytes while keeping the semantic route disabled.

    ``semantic_projection`` is an input only because no route-specific parser
    exists yet.  Consequently this receipt is schema-forced non-production and
    cannot be converted to a candidate by this module.
    """

    policy_methods = {item.method: item.tier for item in policy.registered_methods}
    if policy_methods.get(resolution_method) is not quality_tier:
        raise AuthorityArtifactError("resolution method/tier is not registered by policy")
    target_kind, target_id, target_sha256 = _target_binding(target)
    ordered_support = tuple(
        sorted(
            (item.binding for item in supporting),
            key=lambda item: (item.artifact_kind.value, item.artifact_id, item.artifact_sha256),
        )
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "verifier_version": AUTHORITY_VERIFIER_VERSION,
        "primary_artifact": primary.binding,
        "supporting_artifacts": ordered_support,
        "target_kind": target_kind,
        "target_id": target_id,
        "target_sha256": target_sha256,
        "policy_version": policy.policy_version,
        "policy_file_sha256": policy.policy_file_sha256,
        "gate_file_sha256": policy.gate_file_sha256,
        "source": source,
        "quality_tier": quality_tier,
        "resolution_method": resolution_method,
        "commitment": commitment,
        "semantic_projection": semantic_projection,
        "accepted_evidence_ids": tuple(sorted(accepted_evidence_ids)),
        "provenance": tuple(sorted(provenance)),
        "verification_check_codes": _REQUIRED_CHECKS,
        "route_enabled": False,
        "production_eligible": False,
        "candidate_emitted": False,
        "label_emitted": False,
    }
    receipt_id = make_id(
        AUTHORITY_VERIFICATION_PREFIX,
        AuthorityVerificationReceiptV1.model_construct(receipt_id="", **payload).model_dump(
            mode="json", exclude={"receipt_id"}
        ),
    )
    return AuthorityVerificationReceiptV1.model_validate({"receipt_id": receipt_id, **payload})


__all__ = [
    "AUTHORITY_VERIFICATION_PREFIX",
    "AUTHORITY_VERIFIER_VERSION",
    "AuthorityArtifactError",
    "AuthorityArtifactReferenceV1",
    "AuthorityArtifactSerialization",
    "AuthorityRouteDisabledError",
    "AuthoritySemanticProjectionV1",
    "AuthorityVerificationReceiptV1",
    "LoadedAuthorityArtifact",
    "authority_route_enabled",
    "build_disabled_authority_verification_receipt",
    "load_local_authority_artifact",
    "require_authority_route_enabled",
]
