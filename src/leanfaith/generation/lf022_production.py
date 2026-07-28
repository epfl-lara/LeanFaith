"""Fail-closed LF-022 allocation-plan construction.

The records in this module are deliberately *not* executable generation
requests.  They allocate admitted public theorems to proposer and review
families while binding the exact source-authorization, extraction,
representation, benchmark-registry, screening, context, and family-matrix
artifacts used to make that allocation.  A later executor must provide a
separate, reviewed execution binding; this module never authorizes network
access, semantic labels, or promotion.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import DenylistIndex, FrozenRegistry
from leanfaith.generation.real_outputs import candidate_benchmark_hits
from leanfaith.schemas.enums import ValidationStatus, ViewStatus
from leanfaith.schemas.ids import (
    CONTEXT_PREFIX,
    HEX64_PATTERN,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.source import make_git_declaration_source_locator_id
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

LF022Distribution = Literal["G_sci", "G_open"]
LF022PlanProfile = Literal[
    "diagnostic_scaffold",
    "pilot_scaffold",
    "scientific_production_scaffold",
]

_DISTRIBUTIONS: tuple[LF022Distribution, ...] = ("G_sci", "G_open")
_DISTRIBUTION_ORDER = {name: index for index, name in enumerate(_DISTRIBUTIONS)}
_IMMUTABLE_REVISION_PATTERN = r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"
_PRIVATE_SOURCE_PATTERNS = (
    re.compile(r"(?:^|[:/])formalmathatepfl/sft_classic(?:$|[@/#?])"),
    re.compile(r"(?:^|[:/])sft_classic(?:$|[@/#?])"),
)
_PROFILE_MINIMUM_SOURCES: dict[LF022PlanProfile, int] = {
    "diagnostic_scaffold": 1,
    "pilot_scaffold": 12,
    # The accepted plan targets at least 10k unique compiling examples from
    # each of G_sci and G_open.  Each admitted source creates one task per arm.
    "scientific_production_scaffold": 10_000,
}


class LF022ProductionPlanError(ValueError):
    """An LF-022 allocation input failed a fail-closed admission check."""


def lf022_source_locator_id(theorem: TheoremRecord) -> str:
    """Return the stable source locator used by LF-022 authorization records.

    Dataset adapters already provide a locator-only ``source_record_id``. Git
    library extraction predates that field and identifies a declaration by its
    immutable revision, repository-relative file, and fully qualified
    declaration name. The fallback deliberately excludes statement content and
    extraction-derived ranges/ordinals so later parser or normalization changes
    do not change source identity.
    """

    if theorem.source_record_id is not None:
        return theorem.source_record_id
    if theorem.source_file is None or theorem.declaration_full_name is None:
        raise LF022ProductionPlanError(
            f"theorem lacks a stable source locator: {theorem.theorem_id}"
        )
    try:
        return make_git_declaration_source_locator_id(
            source=theorem.source,
            revision=theorem.source_revision,
            source_file=theorem.source_file,
            declaration_full_name=theorem.declaration_full_name,
        )
    except ValueError as exc:
        raise LF022ProductionPlanError(
            f"theorem lacks a stable source locator: {theorem.theorem_id}: {exc}"
        ) from exc


def _repo_relative(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    if (
        not value.strip()
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in value
    ):
        raise ValueError(f"{field} must be a normalized nonempty repository-relative path")
    return value


def _content_id(prefix: str, model: StrictModel, *, id_field: str) -> str:
    return make_id(prefix, model.model_dump(mode="json", exclude={id_field}))


def canonical_model_family(model_id: str) -> str:
    """Return a conservative family key used for supervision separation."""

    normalized = re.sub(r"\s+", "", model_id).strip().lower()
    if "/" not in normalized:
        raise ValueError("model_id must be a registry-qualified identifier")
    owner, name = normalized.split("/", 1)
    if not owner or not name:
        raise ValueError("model_id must contain nonempty owner and model components")
    if owner == "moonshotai" and re.match(r"^kimi[-_.]?k2(?:[.-]\d+)?(?:[-_.].*)?$", name):
        return "moonshotai/kimi-k2"
    if owner == "qwen" and re.match(r"^qwen3(?:[.-]\d+)?(?:[-_.].*)?$", name):
        return "qwen/qwen3"
    return f"{owner}/{name}"


class LF022ArtifactBinding(StrictModel):
    """Exact repository-relative regular-file binding."""

    path: str
    sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _path_is_safe(self) -> Self:
        _repo_relative(self.path, field="artifact.path")
        return self


class LF022JSONLArtifactBinding(LF022ArtifactBinding):
    """Exact JSONL binding with a frozen expected record count."""

    record_count: int = Field(ge=1, strict=True)


class LF022ProviderDeployment(StrictModel):
    """One model deployment present in a provider catalog snapshot."""

    model_id: str = Field(min_length=3)
    deployment_id: str = Field(min_length=1)


class LF022ProviderCatalogSnapshot(StrictModel):
    """Normalized, content-addressed provider catalog captured by a probe."""

    schema_version: Literal[1] = 1
    snapshot_id: str = Field(pattern=id_pattern("lf022_provider_catalog"))
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    deployments: tuple[LF022ProviderDeployment, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        keys = [(item.model_id, item.deployment_id) for item in self.deployments]
        if len(keys) != len(set(keys)):
            raise ValueError("provider catalog deployments must be unique")
        if list(keys) != sorted(keys):
            raise ValueError("provider catalog deployments must be sorted")
        expected = _content_id("lf022_provider_catalog", self, id_field="snapshot_id")
        if self.snapshot_id != expected:
            raise ValueError("snapshot_id does not match canonical provider-catalog content")
        return self


def make_lf022_provider_catalog_snapshot(
    *,
    provider_id: str,
    deployments: tuple[LF022ProviderDeployment, ...],
) -> LF022ProviderCatalogSnapshot:
    ordered = tuple(sorted(deployments, key=lambda item: (item.model_id, item.deployment_id)))
    payload: dict[str, object] = {
        "schema_version": 1,
        "provider_id": provider_id,
        "deployments": [item.model_dump(mode="json") for item in ordered],
    }
    return LF022ProviderCatalogSnapshot.model_validate(
        {**payload, "snapshot_id": make_id("lf022_provider_catalog", payload)}
    )


class LF022FamilyPin(StrictModel):
    """Honest model identity for an exact checkpoint or provider deployment.

    RCP and similar providers need not disclose the underlying checkpoint SHA.
    In that case the pin binds the exact probed catalog artifact and deployment
    ID and explicitly records that the checkpoint revision is undisclosed.
    """

    family_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    model_id: str = Field(min_length=3)
    canonical_family: str = Field(min_length=3)
    pin_kind: Literal["exact_hf_checkpoint", "provider_deployment_snapshot"]
    checkpoint_revision: str | None = Field(
        default=None,
        pattern=_IMMUTABLE_REVISION_PATTERN,
    )
    provider_id: str | None = None
    provider_deployment_id: str | None = None
    provider_catalog_artifact: LF022ArtifactBinding | None = None
    underlying_checkpoint_revision_status: Literal["exact", "provider_not_disclosed"]

    @model_validator(mode="after")
    def _pin_is_coherent(self) -> Self:
        expected_family = canonical_model_family(self.model_id)
        if self.canonical_family != expected_family:
            raise ValueError(
                f"canonical_family must be {expected_family!r} for model_id {self.model_id!r}"
            )
        provider_fields = (
            self.provider_id,
            self.provider_deployment_id,
            self.provider_catalog_artifact,
        )
        if self.pin_kind == "exact_hf_checkpoint":
            if self.checkpoint_revision is None:
                raise ValueError("exact_hf_checkpoint requires checkpoint_revision")
            if any(value is not None for value in provider_fields):
                raise ValueError("exact_hf_checkpoint cannot contain provider deployment fields")
            if self.underlying_checkpoint_revision_status != "exact":
                raise ValueError("exact_hf_checkpoint must report revision status exact")
        else:
            if self.checkpoint_revision is not None:
                raise ValueError(
                    "provider deployment cannot pretend an undisclosed checkpoint SHA is exact"
                )
            if any(value is None for value in provider_fields):
                raise ValueError(
                    "provider_deployment_snapshot requires provider, deployment, and catalog"
                )
            if self.underlying_checkpoint_revision_status != "provider_not_disclosed":
                raise ValueError(
                    "provider deployment must report provider_not_disclosed checkpoint status"
                )
        return self


def _rotated(values: tuple[str, ...], offset: int) -> tuple[str, ...]:
    start = offset % len(values)
    return values[start:] + values[:start]


def _role_assignment_ids(
    *,
    proposer_id: str,
    judge_family_ids: tuple[str, ...],
    sci_validator_family_ids: tuple[str, ...],
    rotation_index: int,
) -> tuple[str, str, str] | None:
    eligible_judges = tuple(
        family_id
        for family_id in _rotated(judge_family_ids, rotation_index)
        if family_id != proposer_id
    )
    validators = _rotated(sci_validator_family_ids, rotation_index)
    for first_index, judge_a in enumerate(eligible_judges):
        for judge_b in eligible_judges[first_index + 1 :]:
            validator = next(
                (
                    family_id
                    for family_id in validators
                    if family_id not in {proposer_id, judge_a, judge_b}
                ),
                None,
            )
            if validator is not None:
                return judge_a, judge_b, validator
    return None


class LF022ProductionFamilyMatrix(StrictModel):
    """Unique family registry, role eligibility, and held-out evaluator."""

    schema_version: Literal[2] = 2
    matrix_id: str = Field(pattern=id_pattern("lf022_family_matrix"))
    family_registry: tuple[LF022FamilyPin, ...] = Field(min_length=5)
    proposer_family_ids: tuple[str, ...] = Field(min_length=3)
    judge_family_ids: tuple[str, ...] = Field(min_length=2)
    sci_validator_family_ids: tuple[str, ...] = Field(min_length=1)
    heldout_eval_family_id: str
    heldout_eval_supervision_excluded: Literal[True] = True

    @model_validator(mode="after")
    def _eligible_and_content_addressed(self) -> Self:
        family_ids = [pin.family_id for pin in self.family_registry]
        canonical_families = [pin.canonical_family for pin in self.family_registry]
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("family_registry family_ids must be unique")
        if len(canonical_families) != len(set(canonical_families)):
            raise ValueError("family_registry entries must be unique canonical model families")
        registry_ids = set(family_ids)
        for field_name in (
            "proposer_family_ids",
            "judge_family_ids",
            "sci_validator_family_ids",
        ):
            role_ids = getattr(self, field_name)
            if len(role_ids) != len(set(role_ids)):
                raise ValueError(f"{field_name} must be unique")
            unknown = set(role_ids) - registry_ids
            if unknown:
                raise ValueError(
                    f"{field_name} references unregistered families: {sorted(unknown)}"
                )
        if self.heldout_eval_family_id not in registry_ids:
            raise ValueError("heldout_eval_family_id must reference the family registry")
        training_ids = (
            set(self.proposer_family_ids)
            | set(self.judge_family_ids)
            | set(self.sci_validator_family_ids)
        )
        if self.heldout_eval_family_id in training_ids:
            raise ValueError("held-out evaluation family must be wholly excluded from supervision")
        if len(training_ids) < 4:
            raise ValueError("G_sci planning requires at least four distinct training families")
        for proposer_id in self.proposer_family_ids:
            assignment = _role_assignment_ids(
                proposer_id=proposer_id,
                judge_family_ids=self.judge_family_ids,
                sci_validator_family_ids=self.sci_validator_family_ids,
                rotation_index=0,
            )
            if assignment is None:
                raise ValueError(
                    "every proposer requires two distinct eligible judges and a "
                    f"separately distinct SCI validator; no assignment for {proposer_id}"
                )
        expected = _content_id("lf022_family_matrix", self, id_field="matrix_id")
        if self.matrix_id != expected:
            raise ValueError("matrix_id does not match canonical family-matrix content")
        return self

    @property
    def pins_by_id(self) -> dict[str, LF022FamilyPin]:
        return {pin.family_id: pin for pin in self.family_registry}


def make_lf022_production_family_matrix(
    *,
    family_registry: tuple[LF022FamilyPin, ...],
    proposer_family_ids: tuple[str, ...],
    judge_family_ids: tuple[str, ...],
    sci_validator_family_ids: tuple[str, ...],
    heldout_eval_family_id: str,
) -> LF022ProductionFamilyMatrix:
    payload: dict[str, object] = {
        "schema_version": 2,
        "family_registry": [pin.model_dump(mode="json") for pin in family_registry],
        "proposer_family_ids": list(proposer_family_ids),
        "judge_family_ids": list(judge_family_ids),
        "sci_validator_family_ids": list(sci_validator_family_ids),
        "heldout_eval_family_id": heldout_eval_family_id,
        "heldout_eval_supervision_excluded": True,
    }
    return LF022ProductionFamilyMatrix.model_validate(
        {**payload, "matrix_id": make_id("lf022_family_matrix", payload)}
    )


class LF022AuthorizedExtractionMember(StrictModel):
    """One theorem explicitly present in an approved extraction manifest."""

    source_locator_id: str = Field(pattern=HEX64_PATTERN)
    theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    statement_content_hash: str = Field(pattern=HEX64_PATTERN)


class LF022AuthorizedExtractionManifest(StrictModel):
    """Frozen extraction membership for one approved public source revision."""

    schema_version: Literal[1] = 1
    extraction_manifest_id: str = Field(pattern=id_pattern("lf022_extraction_manifest"))
    source: str = Field(min_length=1)
    source_revision: str = Field(pattern=_IMMUTABLE_REVISION_PATTERN)
    members: tuple[LF022AuthorizedExtractionMember, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        _reject_private_source(self.source)
        keys = [
            (item.source_locator_id, item.theorem_id, item.statement_content_hash)
            for item in self.members
        ]
        if len(keys) != len(set(keys)) or list(keys) != sorted(keys):
            raise ValueError("authorized extraction members must be sorted and unique")
        expected = _content_id(
            "lf022_extraction_manifest",
            self,
            id_field="extraction_manifest_id",
        )
        if self.extraction_manifest_id != expected:
            raise ValueError("extraction_manifest_id does not match canonical content")
        return self


def make_lf022_authorized_extraction_manifest(
    *,
    source: str,
    source_revision: str,
    members: tuple[LF022AuthorizedExtractionMember, ...],
) -> LF022AuthorizedExtractionManifest:
    ordered = tuple(
        sorted(
            members,
            key=lambda item: (
                item.source_locator_id,
                item.theorem_id,
                item.statement_content_hash,
            ),
        )
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "source": source,
        "source_revision": source_revision,
        "members": [item.model_dump(mode="json") for item in ordered],
    }
    return LF022AuthorizedExtractionManifest.model_validate(
        {**payload, "extraction_manifest_id": make_id("lf022_extraction_manifest", payload)}
    )


class LF022PublicSourceAuthorization(StrictModel):
    """An approved source/revision/license tied to exact extraction bytes."""

    authorization_id: str = Field(pattern=id_pattern("lf022_public_source"))
    source: str = Field(min_length=1)
    source_revision: str = Field(pattern=_IMMUTABLE_REVISION_PATTERN)
    license_id: str = Field(min_length=1)
    license_evidence_uri: str = Field(min_length=1)
    extraction_manifest: LF022ArtifactBinding
    license_status: Literal["approved_public_research_compatible"]
    source_is_public: Literal[True] = True
    redistribution_allowed: Literal[True] = True
    external_transmission_allowed: Literal[True] = True

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        _reject_private_source(self.source)
        expected = _content_id("lf022_public_source", self, id_field="authorization_id")
        if self.authorization_id != expected:
            raise ValueError("authorization_id does not match canonical source authorization")
        return self


class LF022PublicSourceAuthorizationRegistry(StrictModel):
    """Frozen reviewed registry; source records cannot assert licenses themselves."""

    schema_version: Literal[1] = 1
    registry_id: str = Field(pattern=id_pattern("lf022_public_source_registry"))
    policy_version: str = Field(min_length=1)
    authorizations: tuple[LF022PublicSourceAuthorization, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        keys = [(item.source, item.source_revision) for item in self.authorizations]
        if len(keys) != len(set(keys)) or list(keys) != sorted(keys):
            raise ValueError("public-source authorizations must be sorted and unique")
        expected = _content_id("lf022_public_source_registry", self, id_field="registry_id")
        if self.registry_id != expected:
            raise ValueError("registry_id does not match canonical authorization registry")
        return self


def make_lf022_public_source_authorization_registry(
    *,
    policy_version: str,
    authorizations: tuple[LF022PublicSourceAuthorization, ...],
) -> LF022PublicSourceAuthorizationRegistry:
    ordered = tuple(sorted(authorizations, key=lambda item: (item.source, item.source_revision)))
    payload: dict[str, object] = {
        "schema_version": 1,
        "policy_version": policy_version,
        "authorizations": [item.model_dump(mode="json") for item in ordered],
    }
    return LF022PublicSourceAuthorizationRegistry.model_validate(
        {**payload, "registry_id": make_id("lf022_public_source_registry", payload)}
    )


def make_lf022_public_source_authorization(
    *,
    source: str,
    source_revision: str,
    license_id: str,
    license_evidence_uri: str,
    extraction_manifest: LF022ArtifactBinding,
) -> LF022PublicSourceAuthorization:
    payload: dict[str, object] = {
        "source": source,
        "source_revision": source_revision,
        "license_id": license_id,
        "license_evidence_uri": license_evidence_uri,
        "extraction_manifest": extraction_manifest.model_dump(mode="json"),
        "license_status": "approved_public_research_compatible",
        "source_is_public": True,
        "redistribution_allowed": True,
        "external_transmission_allowed": True,
    }
    return LF022PublicSourceAuthorization.model_validate(
        {**payload, "authorization_id": make_id("lf022_public_source", payload)}
    )


class LF022BenchmarkRegistryManifest(StrictModel):
    """Pointer binding the exact active frozen_ids registry used by the checker."""

    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=id_pattern("lf022_benchmark_registry"))
    policy_version: str = Field(min_length=1)
    active_registry: LF022ArtifactBinding
    checker_version: Literal["candidate_benchmark_hits_v1"]

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        expected = _content_id("lf022_benchmark_registry", self, id_field="manifest_id")
        if self.manifest_id != expected:
            raise ValueError("manifest_id does not match canonical benchmark binding")
        return self


def make_lf022_benchmark_registry_manifest(
    *,
    policy_version: str,
    active_registry: LF022ArtifactBinding,
) -> LF022BenchmarkRegistryManifest:
    payload: dict[str, object] = {
        "schema_version": 1,
        "policy_version": policy_version,
        "active_registry": active_registry.model_dump(mode="json"),
        "checker_version": "candidate_benchmark_hits_v1",
    }
    return LF022BenchmarkRegistryManifest.model_validate(
        {**payload, "manifest_id": make_id("lf022_benchmark_registry", payload)}
    )


class LF022DenylistClearanceRecord(StrictModel):
    """Persisted output of the exact active-registry checker for one theorem."""

    schema_version: Literal[2] = 2
    clearance_id: str = Field(pattern=id_pattern("lf022_denylist_clearance"))
    benchmark_manifest_id: str = Field(pattern=id_pattern("lf022_benchmark_registry"))
    active_registry_file_sha256: str = Field(pattern=HEX64_PATTERN)
    active_registry_content_hash: str = Field(pattern=HEX64_PATTERN)
    source_locator_id: str = Field(pattern=HEX64_PATTERN)
    theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    theorem_statement_content_hash: str = Field(pattern=HEX64_PATTERN)
    representation_id: str = Field(pattern=id_pattern(REPRESENTATION_PREFIX))
    representation_content_hash: str = Field(pattern=HEX64_PATTERN)
    identifier_hits: tuple[str, ...] = ()
    content_hits: tuple[str, ...] = ()
    all_identifier_and_content_screens_executed: Literal[True] = True
    checker_version: Literal["candidate_benchmark_hits_v1"]

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        for field_name in ("identifier_hits", "content_hits"):
            values = getattr(self, field_name)
            if list(values) != sorted(set(values)):
                raise ValueError(f"{field_name} must be sorted and unique")
        expected = _content_id("lf022_denylist_clearance", self, id_field="clearance_id")
        if self.clearance_id != expected:
            raise ValueError("clearance_id does not match canonical checker output")
        return self

    @property
    def clear(self) -> bool:
        return not self.identifier_hits and not self.content_hits


def make_lf022_denylist_clearance_record(
    *,
    benchmark_manifest_id: str,
    active_registry_file_sha256: str,
    active_registry_content_hash: str,
    source_locator_id: str,
    theorem_id: str,
    theorem_statement_content_hash: str,
    representation_id: str,
    representation_content_hash: str,
    identifier_hits: tuple[str, ...],
    content_hits: tuple[str, ...],
) -> LF022DenylistClearanceRecord:
    payload: dict[str, object] = {
        "schema_version": 2,
        "benchmark_manifest_id": benchmark_manifest_id,
        "active_registry_file_sha256": active_registry_file_sha256,
        "active_registry_content_hash": active_registry_content_hash,
        "source_locator_id": source_locator_id,
        "theorem_id": theorem_id,
        "theorem_statement_content_hash": theorem_statement_content_hash,
        "representation_id": representation_id,
        "representation_content_hash": representation_content_hash,
        "identifier_hits": sorted(set(identifier_hits)),
        "content_hits": sorted(set(content_hits)),
        "all_identifier_and_content_screens_executed": True,
        "checker_version": "candidate_benchmark_hits_v1",
    }
    return LF022DenylistClearanceRecord.model_validate(
        {**payload, "clearance_id": make_id("lf022_denylist_clearance", payload)}
    )


def _reject_private_source(source: str) -> None:
    normalized = source.strip().lower()
    if not normalized:
        raise ValueError("source must be nonempty")
    if any(pattern.search(normalized) for pattern in _PRIVATE_SOURCE_PATTERNS):
        raise ValueError("private sft_classic content is forbidden from LF-022 plans")


class LF022ProductionSourceRecord(StrictModel):
    """One theorem requesting admission through reviewed external artifacts."""

    schema_version: Literal[2] = 2
    admission_record_id: str = Field(pattern=id_pattern("lf022_source_admission"))
    source_locator_id: str = Field(pattern=HEX64_PATTERN)
    source: str = Field(min_length=1)
    source_revision: str = Field(pattern=_IMMUTABLE_REVISION_PATTERN)
    theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    theorem_statement_content_hash: str = Field(pattern=HEX64_PATTERN)
    representation_id: str = Field(pattern=id_pattern(REPRESENTATION_PREFIX))
    representation_content_hash: str = Field(pattern=HEX64_PATTERN)
    normalization_version: str = Field(min_length=1)
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    context_fingerprint: str = Field(pattern=HEX64_PATTERN)
    context_header_hash: str = Field(pattern=HEX64_PATTERN)
    public_source_authorization_id: str = Field(pattern=id_pattern("lf022_public_source"))
    denylist_clearance_id: str = Field(pattern=id_pattern("lf022_denylist_clearance"))

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        _reject_private_source(self.source)
        if self.context_id != f"{CONTEXT_PREFIX}:{self.context_fingerprint}":
            raise ValueError("source context_id does not match context_fingerprint")
        expected = _content_id("lf022_source_admission", self, id_field="admission_record_id")
        if self.admission_record_id != expected:
            raise ValueError("admission_record_id does not match canonical source request")
        return self


def make_lf022_production_source_record(
    *,
    source_locator_id: str,
    source: str,
    source_revision: str,
    theorem_id: str,
    theorem_statement_content_hash: str,
    representation_id: str,
    representation_content_hash: str,
    normalization_version: str,
    context_id: str,
    context_fingerprint: str,
    context_header_hash: str,
    public_source_authorization_id: str,
    denylist_clearance_id: str,
) -> LF022ProductionSourceRecord:
    payload: dict[str, object] = {
        "schema_version": 2,
        "source_locator_id": source_locator_id,
        "source": source,
        "source_revision": source_revision,
        "theorem_id": theorem_id,
        "theorem_statement_content_hash": theorem_statement_content_hash,
        "representation_id": representation_id,
        "representation_content_hash": representation_content_hash,
        "normalization_version": normalization_version,
        "context_id": context_id,
        "context_fingerprint": context_fingerprint,
        "context_header_hash": context_header_hash,
        "public_source_authorization_id": public_source_authorization_id,
        "denylist_clearance_id": denylist_clearance_id,
    }
    return LF022ProductionSourceRecord.model_validate(
        {**payload, "admission_record_id": make_id("lf022_source_admission", payload)}
    )


class LF022ProductionArtifactSet(StrictModel):
    """Every artifact needed to reproduce an offline allocation."""

    family_matrix: LF022ArtifactBinding
    public_source_authorization_registry: LF022ArtifactBinding
    benchmark_registry_manifest: LF022ArtifactBinding
    active_benchmark_registry: LF022ArtifactBinding
    denylist_clearance_records: LF022JSONLArtifactBinding
    source_pool: LF022JSONLArtifactBinding
    theorem_records: LF022JSONLArtifactBinding
    representation_records: LF022JSONLArtifactBinding
    context_records: LF022JSONLArtifactBinding


class LF022ProductionAdmission(StrictModel):
    """Authorization to construct a non-executable allocation scaffold."""

    schema_version: Literal[2] = 2
    admission_id: str = Field(pattern=id_pattern("lf022_production_admission"))
    profile: LF022PlanProfile
    artifact_class: Literal["allocation_scaffold"]
    status: Literal["non_executable_inputs_admitted"]
    family_matrix_id: str = Field(pattern=id_pattern("lf022_family_matrix"))
    family_matrix_sha256: str = Field(pattern=HEX64_PATTERN)
    artifacts: LF022ProductionArtifactSet
    distributions: tuple[Literal["G_sci"], Literal["G_open"]]
    public_sources_only: Literal[True]
    private_sft_classic_forbidden: Literal[True]
    heldout_eval_supervision_excluded: Literal[True]
    execution_binding_status: Literal["absent"]
    execution_bindings_present: Literal[False]
    network_execution_authorized: Literal[False]
    semantic_labels_created: Literal[False]
    silver_promotion_enabled: Literal[False]
    gold_promotion_enabled: Literal[False]

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        expected = _content_id("lf022_production_admission", self, id_field="admission_id")
        if self.admission_id != expected:
            raise ValueError("admission_id does not match canonical admission content")
        return self


def make_lf022_production_admission(
    *,
    family_matrix: LF022ProductionFamilyMatrix,
    artifacts: LF022ProductionArtifactSet,
    profile: LF022PlanProfile = "diagnostic_scaffold",
) -> LF022ProductionAdmission:
    matrix_sha256 = hash_canonical(family_matrix.model_dump(mode="json"))
    payload: dict[str, object] = {
        "schema_version": 2,
        "profile": profile,
        "artifact_class": "allocation_scaffold",
        "status": "non_executable_inputs_admitted",
        "family_matrix_id": family_matrix.matrix_id,
        "family_matrix_sha256": matrix_sha256,
        "artifacts": artifacts.model_dump(mode="json"),
        "distributions": list(_DISTRIBUTIONS),
        "public_sources_only": True,
        "private_sft_classic_forbidden": True,
        "heldout_eval_supervision_excluded": True,
        "execution_binding_status": "absent",
        "execution_bindings_present": False,
        "network_execution_authorized": False,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
    }
    return LF022ProductionAdmission.model_validate(
        {**payload, "admission_id": make_id("lf022_production_admission", payload)}
    )


class LF022ProductionTask(StrictModel):
    """One deterministic allocation row, explicitly not an executable request."""

    schema_version: Literal[2] = 2
    task_id: str = Field(pattern=id_pattern("lf022_production_task"))
    task_kind: Literal["non_executable_allocation"]
    admission_record_id: str = Field(pattern=id_pattern("lf022_source_admission"))
    source_locator_id: str = Field(pattern=HEX64_PATTERN)
    theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    representation_id: str = Field(pattern=id_pattern(REPRESENTATION_PREFIX))
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    distribution: LF022Distribution
    proposer_family_id: str
    judge_family_ids: tuple[str, str]
    sci_validator_family_id: str | None
    heldout_eval_family_id: str
    heldout_eval_supervision_excluded: Literal[True]
    execution_binding_id: None = None
    executable: Literal[False]
    network_execution_authorized: Literal[False]
    semantic_label_created: Literal[False]
    silver_promotion_enabled: Literal[False]
    gold_promotion_enabled: Literal[False]

    @model_validator(mode="after")
    def _roles_and_id(self) -> Self:
        if len(set(self.judge_family_ids)) != 2:
            raise ValueError("each task requires two distinct judge families")
        training_roles = {self.proposer_family_id, *self.judge_family_ids}
        if self.sci_validator_family_id is not None:
            training_roles.add(self.sci_validator_family_id)
        if len(training_roles) != 3 + (self.sci_validator_family_id is not None):
            raise ValueError("proposer, judges, and SCI validator must be distinct")
        if self.heldout_eval_family_id in training_roles:
            raise ValueError("held-out evaluation family cannot appear in supervision roles")
        if self.distribution == "G_sci" and self.sci_validator_family_id is None:
            raise ValueError("G_sci requires a separately distinct SCI validator")
        if self.distribution == "G_open" and self.sci_validator_family_id is not None:
            raise ValueError("G_open must not invoke the SCI validator")
        expected = _content_id("lf022_production_task", self, id_field="task_id")
        if self.task_id != expected:
            raise ValueError("task_id does not match canonical task content")
        return self


class LF022ProductionPlanManifest(StrictModel):
    """Content-addressed offline allocation; never an execution manifest."""

    schema_version: Literal[2] = 2
    manifest_id: str = Field(pattern=id_pattern("lf022_production_plan"))
    profile: LF022PlanProfile
    scientific_status: Literal[
        "diagnostic_only",
        "pilot_only",
        "scientific_allocation_scaffold",
    ]
    artifact_class: Literal["allocation_scaffold"]
    status: Literal["non_executable_allocation_complete"]
    admission_id: str = Field(pattern=id_pattern("lf022_production_admission"))
    family_matrix_id: str = Field(pattern=id_pattern("lf022_family_matrix"))
    family_matrix_sha256: str = Field(pattern=HEX64_PATTERN)
    artifacts: LF022ProductionArtifactSet
    unique_source_count: int = Field(ge=1)
    source_admission_record_ids: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[LF022ProductionTask, ...] = Field(min_length=2)
    execution_binding_status: Literal["absent"]
    execution_bindings_present: Literal[False]
    network_execution_authorized: Literal[False]
    semantic_labels_created: Literal[False]
    silver_promotion_enabled: Literal[False]
    gold_promotion_enabled: Literal[False]

    @model_validator(mode="after")
    def _complete_and_content_addressed(self) -> Self:
        if tuple(sorted(set(self.source_admission_record_ids))) != (
            self.source_admission_record_ids
        ):
            raise ValueError("source_admission_record_ids must be sorted and unique")
        if self.unique_source_count != len(self.source_admission_record_ids):
            raise ValueError("unique_source_count does not match admitted source IDs")
        expected_status = {
            "diagnostic_scaffold": "diagnostic_only",
            "pilot_scaffold": "pilot_only",
            "scientific_production_scaffold": "scientific_allocation_scaffold",
        }[self.profile]
        if self.scientific_status != expected_status:
            raise ValueError("scientific_status does not match allocation profile")
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("allocation task IDs must be unique")
        expected_order = sorted(
            self.tasks,
            key=lambda task: (
                task.admission_record_id,
                _DISTRIBUTION_ORDER[task.distribution],
            ),
        )
        if list(self.tasks) != expected_order:
            raise ValueError("allocation tasks must use canonical source/distribution order")
        by_source: dict[str, set[str]] = {}
        for task in self.tasks:
            by_source.setdefault(task.admission_record_id, set()).add(task.distribution)
        if set(by_source) != set(self.source_admission_record_ids):
            raise ValueError("task source IDs do not match plan source IDs")
        if any(distributions != set(_DISTRIBUTIONS) for distributions in by_source.values()):
            raise ValueError("every admitted source requires exactly G_sci and G_open tasks")
        expected = _content_id("lf022_production_plan", self, id_field="manifest_id")
        if self.manifest_id != expected:
            raise ValueError("manifest_id does not match canonical allocation-plan content")
        return self


def _resolve_bound_file(root: Path, binding: LF022ArtifactBinding) -> Path:
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise LF022ProductionPlanError(f"repository root cannot be resolved: {root}") from exc
    relative = PurePosixPath(_repo_relative(binding.path, field="artifact.path"))
    current = canonical_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LF022ProductionPlanError(
                f"artifact path contains a symlinked component: {binding.path}"
            )
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise LF022ProductionPlanError(f"bound artifact is missing: {binding.path}") from exc
    if not resolved.is_relative_to(canonical_root) or not resolved.is_file():
        raise LF022ProductionPlanError(
            f"bound artifact must be a regular file inside the repository: {binding.path}"
        )
    actual = hash_file(resolved)
    if actual != binding.sha256:
        raise LF022ProductionPlanError(
            f"artifact hash mismatch for {binding.path}: expected {binding.sha256}, got {actual}"
        )
    return resolved


def _load_json[RecordT: StrictModel](
    root: Path,
    binding: LF022ArtifactBinding,
    model: type[RecordT],
) -> RecordT:
    path = _resolve_bound_file(root, binding)
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        return model.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LF022ProductionPlanError(
            f"invalid bound JSON artifact {binding.path}: {exc}"
        ) from exc


def _load_jsonl[RecordT: StrictModel](
    root: Path,
    binding: LF022JSONLArtifactBinding,
    model: type[RecordT],
) -> tuple[RecordT, ...]:
    path = _resolve_bound_file(root, binding)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LF022ProductionPlanError(
            f"cannot read bound JSONL artifact {binding.path}: {exc}"
        ) from exc
    if not lines or any(not line.strip() for line in lines):
        raise LF022ProductionPlanError(
            f"bound JSONL artifact {binding.path} must be nonempty and contain no blank rows"
        )
    if len(lines) != binding.record_count:
        raise LF022ProductionPlanError(
            f"record count mismatch for {binding.path}: "
            f"expected {binding.record_count}, got {len(lines)}"
        )
    records: list[RecordT] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            records.append(model.model_validate(cast(object, json.loads(line))))
        except (json.JSONDecodeError, ValueError) as exc:
            raise LF022ProductionPlanError(f"invalid {binding.path}:{line_number}: {exc}") from exc
    return tuple(records)


def _unique_index[RecordT: StrictModel](
    records: tuple[RecordT, ...],
    *,
    attribute: str,
    kind: str,
) -> dict[str, RecordT]:
    result: dict[str, RecordT] = {}
    for record in records:
        identifier = cast(str, getattr(record, attribute))
        if identifier in result:
            raise LF022ProductionPlanError(f"duplicate {kind} record: {identifier}")
        result[identifier] = record
    return result


def _validate_family_matrix(
    *,
    root: Path,
    binding: LF022ArtifactBinding,
    supplied: LF022ProductionFamilyMatrix,
) -> None:
    persisted = _load_json(root, binding, LF022ProductionFamilyMatrix)
    if persisted != supplied:
        raise LF022ProductionPlanError("supplied family matrix differs from bound artifact")
    for pin in supplied.family_registry:
        if pin.pin_kind != "provider_deployment_snapshot":
            continue
        assert pin.provider_catalog_artifact is not None
        assert pin.provider_id is not None
        assert pin.provider_deployment_id is not None
        catalog = _load_json(
            root,
            pin.provider_catalog_artifact,
            LF022ProviderCatalogSnapshot,
        )
        if catalog.provider_id != pin.provider_id:
            raise LF022ProductionPlanError(f"provider catalog mismatch for family {pin.family_id}")
        deployments = {(item.model_id, item.deployment_id) for item in catalog.deployments}
        if (pin.model_id, pin.provider_deployment_id) not in deployments:
            raise LF022ProductionPlanError(
                f"provider deployment is absent from bound catalog for {pin.family_id}"
            )


def _validate_source_binding(
    *,
    source_record: LF022ProductionSourceRecord,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
    context: ContextRecord,
    authorization: LF022PublicSourceAuthorization,
    extraction_members: set[tuple[str, str, str]],
    clearance: LF022DenylistClearanceRecord,
    benchmark_manifest: LF022BenchmarkRegistryManifest,
    active_registry_binding: LF022ArtifactBinding,
    denylist_index: DenylistIndex,
) -> None:
    if lf022_source_locator_id(theorem) != source_record.source_locator_id:
        raise LF022ProductionPlanError(
            f"theorem/source locator mismatch for {source_record.theorem_id}"
        )
    if (
        theorem.source != source_record.source
        or theorem.source_revision != source_record.source_revision
    ):
        raise LF022ProductionPlanError(
            f"theorem source/revision mismatch for {source_record.theorem_id}"
        )
    _reject_private_source(theorem.source)
    if not theorem.is_proposition or theorem.elaboration_status is not ValidationStatus.ELABORATES:
        raise LF022ProductionPlanError(
            f"source theorem is not a fully elaborated proposition: {theorem.theorem_id}"
        )
    if theorem.statement_content_hash != source_record.theorem_statement_content_hash:
        raise LF022ProductionPlanError(
            f"theorem content hash mismatch for {source_record.theorem_id}"
        )
    if (
        source_record.public_source_authorization_id != authorization.authorization_id
        or authorization.source != source_record.source
        or authorization.source_revision != source_record.source_revision
    ):
        raise LF022ProductionPlanError(
            f"public-source authorization mismatch for {source_record.admission_record_id}"
        )
    member = (
        source_record.source_locator_id,
        source_record.theorem_id,
        source_record.theorem_statement_content_hash,
    )
    if member not in extraction_members:
        raise LF022ProductionPlanError(
            f"theorem is absent from authorized extraction manifest: {source_record.theorem_id}"
        )
    if (
        theorem.context_id != source_record.context_id
        or representation.context_id != source_record.context_id
        or context.context_id != source_record.context_id
    ):
        raise LF022ProductionPlanError(
            f"context mismatch for source admission {source_record.admission_record_id}"
        )
    if (
        context.context_fingerprint != source_record.context_fingerprint
        or context.header_hash != source_record.context_header_hash
    ):
        raise LF022ProductionPlanError(
            f"context binding mismatch for source admission {source_record.admission_record_id}"
        )
    if (
        representation.theorem_id != theorem.theorem_id
        or representation.representation_id != source_record.representation_id
        or representation.content_hash != source_record.representation_content_hash
        or representation.normalization_version != source_record.normalization_version
    ):
        raise LF022ProductionPlanError(
            f"representation binding mismatch for theorem {source_record.theorem_id}"
        )
    for view in ("headless", "signature_explicit"):
        if (
            getattr(representation, view) is None
            or representation.view_status[view] is not ViewStatus.OK
        ):
            raise LF022ProductionPlanError(
                f"required allocation view {view} is unavailable for {source_record.theorem_id}"
            )

    expected_clearance = (
        source_record.denylist_clearance_id,
        benchmark_manifest.manifest_id,
        active_registry_binding.sha256,
        denylist_index.registry_content_hash,
        source_record.source_locator_id,
        source_record.theorem_id,
        source_record.theorem_statement_content_hash,
        source_record.representation_id,
        source_record.representation_content_hash,
    )
    observed_clearance = (
        clearance.clearance_id,
        clearance.benchmark_manifest_id,
        clearance.active_registry_file_sha256,
        clearance.active_registry_content_hash,
        clearance.source_locator_id,
        clearance.theorem_id,
        clearance.theorem_statement_content_hash,
        clearance.representation_id,
        clearance.representation_content_hash,
    )
    if observed_clearance != expected_clearance:
        raise LF022ProductionPlanError(
            f"denylist checker binding mismatch for {source_record.admission_record_id}"
        )
    identifier_hits = tuple(
        sorted(
            identifier
            for identifier in (
                source_record.source_locator_id,
                source_record.theorem_id,
                source_record.representation_id,
            )
            if denylist_index.contains_row_id(identifier)
        )
    )
    content_hits = candidate_benchmark_hits(
        denylist_index=denylist_index,
        theorem=theorem,
        representation=representation,
    )
    if clearance.identifier_hits != identifier_hits or clearance.content_hits != content_hits:
        raise LF022ProductionPlanError(
            f"denylist checker output does not replay for {source_record.theorem_id}"
        )
    if not clearance.clear:
        raise LF022ProductionPlanError(
            f"benchmark denylist hit forbids source {source_record.theorem_id}"
        )


def _make_task(
    *,
    source: LF022ProductionSourceRecord,
    distribution: LF022Distribution,
    proposer: LF022FamilyPin,
    judges: tuple[LF022FamilyPin, LF022FamilyPin],
    sci_validator: LF022FamilyPin,
    heldout_eval: LF022FamilyPin,
) -> LF022ProductionTask:
    payload: dict[str, object] = {
        "schema_version": 2,
        "task_kind": "non_executable_allocation",
        "admission_record_id": source.admission_record_id,
        "source_locator_id": source.source_locator_id,
        "theorem_id": source.theorem_id,
        "representation_id": source.representation_id,
        "context_id": source.context_id,
        "distribution": distribution,
        "proposer_family_id": proposer.family_id,
        "judge_family_ids": [judges[0].family_id, judges[1].family_id],
        "sci_validator_family_id": sci_validator.family_id if distribution == "G_sci" else None,
        "heldout_eval_family_id": heldout_eval.family_id,
        "heldout_eval_supervision_excluded": True,
        "execution_binding_id": None,
        "executable": False,
        "network_execution_authorized": False,
        "semantic_label_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
    }
    return LF022ProductionTask.model_validate(
        {**payload, "task_id": make_id("lf022_production_task", payload)}
    )


def build_lf022_production_plan(
    *,
    repo_root: Path,
    admission: LF022ProductionAdmission,
    family_matrix: LF022ProductionFamilyMatrix,
) -> LF022ProductionPlanManifest:
    """Replay every bound input and build a deterministic allocation scaffold."""

    matrix_sha256 = hash_canonical(family_matrix.model_dump(mode="json"))
    if (
        admission.family_matrix_id != family_matrix.matrix_id
        or admission.family_matrix_sha256 != matrix_sha256
    ):
        raise LF022ProductionPlanError("admission binds a different family matrix")
    _validate_family_matrix(
        root=repo_root,
        binding=admission.artifacts.family_matrix,
        supplied=family_matrix,
    )

    source_registry = _load_json(
        repo_root,
        admission.artifacts.public_source_authorization_registry,
        LF022PublicSourceAuthorizationRegistry,
    )
    authorizations = {
        (item.source, item.source_revision): item for item in source_registry.authorizations
    }
    extraction_members: dict[str, set[tuple[str, str, str]]] = {}
    for authorized_source in source_registry.authorizations:
        extraction = _load_json(
            repo_root,
            authorized_source.extraction_manifest,
            LF022AuthorizedExtractionManifest,
        )
        if (
            extraction.source != authorized_source.source
            or extraction.source_revision != authorized_source.source_revision
        ):
            raise LF022ProductionPlanError(
                f"authorized extraction source mismatch for {authorized_source.authorization_id}"
            )
        extraction_members[authorized_source.authorization_id] = {
            (
                member.source_locator_id,
                member.theorem_id,
                member.statement_content_hash,
            )
            for member in extraction.members
        }

    benchmark_manifest = _load_json(
        repo_root,
        admission.artifacts.benchmark_registry_manifest,
        LF022BenchmarkRegistryManifest,
    )
    if benchmark_manifest.active_registry != admission.artifacts.active_benchmark_registry:
        raise LF022ProductionPlanError(
            "benchmark manifest active-registry binding differs from artifact set"
        )
    active_registry = _load_json(
        repo_root,
        admission.artifacts.active_benchmark_registry,
        FrozenRegistry,
    )
    denylist_index = DenylistIndex(active_registry)

    source_records = _load_jsonl(
        repo_root,
        admission.artifacts.source_pool,
        LF022ProductionSourceRecord,
    )
    theorem_records = _load_jsonl(
        repo_root,
        admission.artifacts.theorem_records,
        TheoremRecord,
    )
    representation_records = _load_jsonl(
        repo_root,
        admission.artifacts.representation_records,
        RepresentationRecord,
    )
    context_records = _load_jsonl(
        repo_root,
        admission.artifacts.context_records,
        ContextRecord,
    )
    clearance_records = _load_jsonl(
        repo_root,
        admission.artifacts.denylist_clearance_records,
        LF022DenylistClearanceRecord,
    )

    source_index = _unique_index(
        source_records, attribute="admission_record_id", kind="source admission"
    )
    _unique_index(source_records, attribute="source_locator_id", kind="source locator")
    source_theorem_index = _unique_index(
        source_records, attribute="theorem_id", kind="source theorem"
    )
    source_representation_index = _unique_index(
        source_records, attribute="representation_id", kind="source representation"
    )
    theorem_index = _unique_index(theorem_records, attribute="theorem_id", kind="theorem")
    representation_index = _unique_index(
        representation_records, attribute="representation_id", kind="representation"
    )
    context_index = _unique_index(context_records, attribute="context_id", kind="context")
    clearance_index = _unique_index(
        clearance_records, attribute="clearance_id", kind="denylist clearance"
    )

    if set(theorem_index) != set(source_theorem_index):
        raise LF022ProductionPlanError(
            "theorem artifact must contain exactly the source-pool theorem IDs"
        )
    if set(representation_index) != set(source_representation_index):
        raise LF022ProductionPlanError(
            "representation artifact must contain exactly the source-pool representation IDs"
        )
    expected_context_ids = {record.context_id for record in source_records}
    if set(context_index) != expected_context_ids:
        raise LF022ProductionPlanError(
            "context artifact must contain exactly the source-pool context IDs"
        )
    expected_clearance_ids = {record.denylist_clearance_id for record in source_records}
    if set(clearance_index) != expected_clearance_ids:
        raise LF022ProductionPlanError(
            "clearance artifact must contain exactly the source-pool clearance IDs"
        )

    ordered_sources = tuple(
        sorted(source_index.values(), key=lambda record: record.admission_record_id)
    )
    minimum = _PROFILE_MINIMUM_SOURCES[admission.profile]
    if len(ordered_sources) < minimum:
        raise LF022ProductionPlanError(
            f"{admission.profile} requires at least {minimum} unique admitted sources"
        )
    for source_record in ordered_sources:
        authorization = authorizations.get((source_record.source, source_record.source_revision))
        if authorization is None:
            raise LF022ProductionPlanError(
                f"source/revision is absent from public authorization registry: "
                f"{source_record.source}@{source_record.source_revision}"
            )
        _validate_source_binding(
            source_record=source_record,
            theorem=theorem_index[source_record.theorem_id],
            representation=representation_index[source_record.representation_id],
            context=context_index[source_record.context_id],
            authorization=authorization,
            extraction_members=extraction_members[authorization.authorization_id],
            clearance=clearance_index[source_record.denylist_clearance_id],
            benchmark_manifest=benchmark_manifest,
            active_registry_binding=admission.artifacts.active_benchmark_registry,
            denylist_index=denylist_index,
        )

    tasks: list[LF022ProductionTask] = []
    pins_by_id = family_matrix.pins_by_id
    proposer_ids = family_matrix.proposer_family_ids
    proposer_counts: Counter[str] = Counter()
    for source_index_value, source_record in enumerate(ordered_sources):
        proposer_id = proposer_ids[source_index_value % len(proposer_ids)]
        proposer_counts[proposer_id] += 1
        assignment = _role_assignment_ids(
            proposer_id=proposer_id,
            judge_family_ids=family_matrix.judge_family_ids,
            sci_validator_family_ids=family_matrix.sci_validator_family_ids,
            rotation_index=source_index_value,
        )
        if assignment is None:  # pragma: no cover - matrix validation proves this
            raise LF022ProductionPlanError(
                f"no per-task family assignment for proposer {proposer_id}"
            )
        judge_a_id, judge_b_id, sci_validator_id = assignment
        for distribution in _DISTRIBUTIONS:
            tasks.append(
                _make_task(
                    source=source_record,
                    distribution=distribution,
                    proposer=pins_by_id[proposer_id],
                    judges=(pins_by_id[judge_a_id], pins_by_id[judge_b_id]),
                    sci_validator=pins_by_id[sci_validator_id],
                    heldout_eval=pins_by_id[family_matrix.heldout_eval_family_id],
                )
            )
    if admission.profile == "scientific_production_scaffold":
        largest = max(proposer_counts.values())
        if largest * 100 > len(ordered_sources) * 40:
            raise LF022ProductionPlanError(
                "scientific production proposer allocation exceeds the 40% family cap"
            )

    scientific_status = {
        "diagnostic_scaffold": "diagnostic_only",
        "pilot_scaffold": "pilot_only",
        "scientific_production_scaffold": "scientific_allocation_scaffold",
    }[admission.profile]
    payload: dict[str, object] = {
        "schema_version": 2,
        "profile": admission.profile,
        "scientific_status": scientific_status,
        "artifact_class": "allocation_scaffold",
        "status": "non_executable_allocation_complete",
        "admission_id": admission.admission_id,
        "family_matrix_id": family_matrix.matrix_id,
        "family_matrix_sha256": matrix_sha256,
        "artifacts": admission.artifacts.model_dump(mode="json"),
        "unique_source_count": len(ordered_sources),
        "source_admission_record_ids": [source.admission_record_id for source in ordered_sources],
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "execution_binding_status": "absent",
        "execution_bindings_present": False,
        "network_execution_authorized": False,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
    }
    return LF022ProductionPlanManifest.model_validate(
        {**payload, "manifest_id": make_id("lf022_production_plan", payload)}
    )


def _production_plan_output_path(repo_root: Path, relative_path: str) -> Path:
    try:
        canonical_root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise LF022ProductionPlanError(f"repository root cannot be resolved: {repo_root}") from exc
    relative = PurePosixPath(_repo_relative(relative_path, field="plan.path"))
    parent = canonical_root
    for part in relative.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise LF022ProductionPlanError(
                f"allocation-plan path contains a symlinked component: {relative_path}"
            )
        if parent.exists() and not parent.is_dir():
            raise LF022ProductionPlanError(
                f"allocation-plan parent is not a directory: {relative_path}"
            )
        parent.mkdir(exist_ok=True)
    if not parent.resolve(strict=True).is_relative_to(canonical_root):
        raise LF022ProductionPlanError(
            f"allocation-plan path escapes the repository: {relative_path}"
        )
    output = parent / relative.name
    if output.is_symlink():
        raise LF022ProductionPlanError(
            f"allocation-plan output cannot be a symlink: {relative_path}"
        )
    return output


def write_lf022_production_plan(
    *,
    repo_root: Path,
    relative_path: str,
    plan: LF022ProductionPlanManifest,
) -> LF022ArtifactBinding:
    """Write immutable canonical JSON, accepting only byte-identical replay."""

    output = _production_plan_output_path(repo_root, relative_path)
    payload = canonical_json_bytes(plan.model_dump(mode="json"))
    if output.exists():
        if not output.is_file() or output.read_bytes() != payload:
            raise LF022ProductionPlanError(
                f"immutable allocation-plan output already differs: {relative_path}"
            )
    else:
        try:
            with output.open("xb") as handle:
                handle.write(payload)
        except FileExistsError:
            if not output.is_file() or output.read_bytes() != payload:
                raise LF022ProductionPlanError(
                    f"concurrent allocation-plan output differs: {relative_path}"
                ) from None
    return LF022ArtifactBinding(path=relative_path, sha256=hash_file(output))


def load_lf022_production_plan(
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
) -> LF022ProductionPlanManifest:
    """Hash-check and strictly load one canonical allocation-plan manifest."""

    path = _resolve_bound_file(repo_root, binding)
    try:
        raw_bytes = path.read_bytes()
        plan = LF022ProductionPlanManifest.model_validate(cast(object, json.loads(raw_bytes)))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LF022ProductionPlanError(
            f"invalid allocation-plan artifact {binding.path}: {exc}"
        ) from exc
    if raw_bytes != canonical_json_bytes(plan.model_dump(mode="json")):
        raise LF022ProductionPlanError(
            f"allocation-plan artifact is not canonical JSON: {binding.path}"
        )
    return plan


__all__ = [
    "LF022ArtifactBinding",
    "LF022AuthorizedExtractionManifest",
    "LF022AuthorizedExtractionMember",
    "LF022BenchmarkRegistryManifest",
    "LF022DenylistClearanceRecord",
    "LF022FamilyPin",
    "LF022JSONLArtifactBinding",
    "LF022ProductionAdmission",
    "LF022ProductionArtifactSet",
    "LF022ProductionFamilyMatrix",
    "LF022ProductionPlanError",
    "LF022ProductionPlanManifest",
    "LF022ProductionSourceRecord",
    "LF022ProductionTask",
    "LF022ProviderCatalogSnapshot",
    "LF022ProviderDeployment",
    "LF022PublicSourceAuthorization",
    "LF022PublicSourceAuthorizationRegistry",
    "build_lf022_production_plan",
    "canonical_model_family",
    "lf022_source_locator_id",
    "load_lf022_production_plan",
    "make_lf022_authorized_extraction_manifest",
    "make_lf022_benchmark_registry_manifest",
    "make_lf022_denylist_clearance_record",
    "make_lf022_production_admission",
    "make_lf022_production_family_matrix",
    "make_lf022_production_source_record",
    "make_lf022_provider_catalog_snapshot",
    "make_lf022_public_source_authorization",
    "make_lf022_public_source_authorization_registry",
    "write_lf022_production_plan",
]
