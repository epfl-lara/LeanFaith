"""Source manifest record (PLAN.md §9.5).

One manifest per pinned source under ``data/source_manifests/<source>.json``.
Raw partitions are append-only; adapter fixes create new parsed partitions.
"""

from __future__ import annotations

import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import AccessStatus, NLTrust, SourceKind
from leanfaith.schemas.ids import ANCESTRY_PREFIX, HEX64_PATTERN, make_id
from leanfaith.schemas.manifest import require_utc

MetadataValue = str | int | float | bool | None

HF_ROW_IDENTITY_VERSION = "hf-row:v1"
GIT_DECLARATION_IDENTITY_VERSION = "git-declaration:v1"


def make_hf_source_record_id(dataset_id: str, revision: str, split: str, row_index: int) -> str:
    """Stable row-locator ID; source content deliberately does not enter it."""

    if row_index < 0:
        raise ValueError("row_index must be nonnegative")
    payload = "\0".join((HF_ROW_IDENTITY_VERSION, dataset_id, revision, split, str(row_index)))
    return sha256_hex(payload.encode("utf-8"))


def make_git_declaration_source_locator_id(
    *,
    source: str,
    revision: str,
    source_file: str,
    declaration_full_name: str,
) -> str:
    """Stable content-independent locator for one declaration at a Git revision.

    A repository file can contain many proposition declarations, so its path is
    not sufficient as LF-022 source identity.  The fully qualified declaration
    name is unique in a successful Lean environment.  Source ranges,
    declaration ordinals, declaration kinds, and statement content are
    deliberately excluded because they are extraction outputs rather than
    immutable Git locations.
    """

    for field, value in (
        ("source", source),
        ("revision", revision),
        ("source_file", source_file),
        ("declaration_full_name", declaration_full_name),
    ):
        if not value or value != value.strip():
            raise ValueError(f"{field} must be nonempty with no surrounding whitespace")
    if len(revision) not in (40, 64) or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("revision must be an immutable 40- or 64-character lowercase hex ID")
    path = PurePosixPath(source_file)
    if path.is_absolute() or ".." in path.parts or "\\" in source_file or str(path) != source_file:
        raise ValueError("source_file must be a normalized repository-relative POSIX path")
    return hash_canonical(
        {
            "identity_version": GIT_DECLARATION_IDENTITY_VERSION,
            "source": source,
            "revision": revision,
            "source_file": source_file,
            "declaration_full_name": declaration_full_name,
        }
    )


def make_source_ancestry_id(
    *,
    source: str,
    revision: str,
    source_locator: str,
    declaration_full_name: str,
) -> str:
    """Recompute the canonical root ancestry for an extracted declaration.

    Root ancestry is source identity, not mutable theorem metadata.  Keeping
    this constructor beside the source-locator constructors lets downstream
    admission code verify extractor output instead of trusting serialized
    ``ancestry_id`` fields.
    """

    for field, value in (
        ("source", source),
        ("revision", revision),
        ("source_locator", source_locator),
        ("declaration_full_name", declaration_full_name),
    ):
        if not value or value != value.strip():
            raise ValueError(f"{field} must be nonempty with no surrounding whitespace")
    return make_id(
        ANCESTRY_PREFIX,
        {
            "source": source,
            "revision": revision,
            "source_locator": source_locator,
            "declaration": declaration_full_name,
        },
    )


class HFSourceRecordIdentity(StrictModel):
    """Location identity and independent content hashes for one HF row."""

    schema_version: Literal[1] = 1
    identity_version: Literal["hf-row:v1"] = "hf-row:v1"
    source_record_id: str = Field(pattern=HEX64_PATTERN)
    dataset_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    split: str = Field(min_length=1)
    row_index: int = Field(ge=0)
    upstream_uuid: str | None = None
    raw_row_hash: str = Field(pattern=HEX64_PATTERN)
    question_hash: str = Field(pattern=HEX64_PATTERN)
    lean_code_hash: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _locator_matches(self) -> HFSourceRecordIdentity:
        expected = make_hf_source_record_id(
            self.dataset_id, self.revision, self.split, self.row_index
        )
        if self.source_record_id != expected:
            raise ValueError("source_record_id must depend only on immutable HF row location")
        return self


class SourceManifest(StrictModel):
    """Pinned identity, schema, and probe evidence for one source (§9.5)."""

    schema_version: int = 1
    source: str = Field(min_length=1)
    kind: SourceKind
    resolved_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    retrieval_date: datetime.datetime
    access_status: AccessStatus
    license: str | None = None
    terms_notes: str = ""
    adapter_version: str | None = None
    record_schema_version: int | None = None
    columns: tuple[str, ...] = ()
    split_counts: dict[str, int] = Field(default_factory=dict)
    sample_rows: int | None = None
    sample_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    raw_hashes: dict[str, str] = Field(default_factory=dict)
    project_toolchain: str | None = None
    nl_trust: NLTrust | None = None
    phase5_eligible_count: int | None = Field(default=None, ge=0)
    access_basis: str | None = None
    institutional_policy_status: str | None = None
    license_status: str | None = None
    external_api_approved: bool | None = None
    approved_providers: tuple[str, ...] = ()
    redistribution_allowed: bool | None = None
    external_transmission_allowed: bool | None = None
    release_eligibility: bool | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _checks(self) -> SourceManifest:
        require_utc(self.retrieval_date)
        for name, digest in self.raw_hashes.items():
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"raw hash for {name!r} is not a sha256 hex digest")
        for split, count in self.split_counts.items():
            if count < 0:
                raise ValueError(f"negative count for split {split!r}")
        if self.approved_providers and self.external_api_approved is not True:
            raise ValueError(
                "approved_providers requires external_api_approved=true (§9.2 approval record)"
            )
        if self.access_status == AccessStatus.PRIVATE_AUTHENTICATED:
            required = {
                "access_basis": self.access_basis,
                "institutional_policy_status": self.institutional_policy_status,
                "license_status": self.license_status,
                "redistribution_allowed": self.redistribution_allowed,
                "external_transmission_allowed": self.external_transmission_allowed,
                "release_eligibility": self.release_eligibility,
            }
            missing = sorted(name for name, value in required.items() if value is None)
            if missing:
                raise ValueError(
                    "private source manifests require explicit Gate-0 policy fields: "
                    + ", ".join(missing)
                )
            if self.external_api_approved is not False or self.approved_providers:
                raise ValueError(
                    "Revision 4.1 private sources cannot be approved for external APIs"
                )
            if any(
                (
                    self.redistribution_allowed,
                    self.external_transmission_allowed,
                    self.release_eligibility,
                )
            ):
                raise ValueError("private source policy must be internal-only and non-releasable")
        return self
