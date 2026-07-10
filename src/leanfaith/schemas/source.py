"""Source manifest record (PLAN.md §9.5).

One manifest per pinned source under ``data/source_manifests/<source>.json``.
Raw partitions are append-only; adapter fixes create new parsed partitions.
"""

from __future__ import annotations

import datetime

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import AccessStatus, NLTrust, SourceKind
from leanfaith.schemas.ids import HEX64_PATTERN
from leanfaith.schemas.manifest import require_utc

MetadataValue = str | int | float | bool | None


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
    external_api_approved: bool | None = None
    approved_providers: tuple[str, ...] = ()
    redistribution_allowed: bool | None = None
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
        return self
