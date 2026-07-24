"""Conservative checkpoint-to-public-pool overlap records for LF-021."""

from __future__ import annotations

import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import require_utc

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_OVERLAP_ID = r"^research_overlap:[0-9a-f]{64}$"


class HFCommitProbeEvidence(StrictModel):
    """Replayable parameters and observation from official HF commit history."""

    provider: Literal["huggingface_hub"] = "huggingface_hub"
    method: Literal["HfApi.list_repo_commits"] = "HfApi.list_repo_commits"
    repo_type: Literal["model"] = "model"
    repo_id: str
    requested_revision: str = Field(pattern=_HEX40)
    observed_commit_id: str = Field(pattern=_HEX40)
    observed_created_at: datetime.datetime
    probed_at: datetime.datetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.observed_created_at)
        require_utc(self.probed_at)
        if self.observed_commit_id != self.requested_revision:
            raise ValueError("official probe did not return the requested exact revision")
        if self.probed_at < self.observed_created_at:
            raise ValueError("probe timestamp cannot precede repository commit")
        return self


class PublicSourceIntroduction(StrictModel):
    problem_record_id: str
    problem_id: str
    introduction_commit: str = Field(pattern=_HEX40)
    introduction_created_at: datetime.datetime

    @model_validator(mode="after")
    def _time(self) -> Self:
        require_utc(self.introduction_created_at)
        return self


class ResearchFamilyOverlapRecord(StrictModel):
    """Cautious authorization for this exact pool, never an unseen-data claim."""

    schema_version: Literal[1] = 1
    record_kind: Literal["lf021_research_family_overlap"] = "lf021_research_family_overlap"
    overlap_id: str = Field(pattern=_OVERLAP_ID)
    family_id: str
    model_repo_id: str
    model_revision: str = Field(pattern=_HEX40)
    checkpoint_probe: HFCommitProbeEvidence
    pinned_readme_sha256: str = Field(pattern=_HEX64)
    training_lineage_disclosure: str
    problem_pool_records_sha256: str = Field(pattern=_HEX64)
    problem_pool_manifest_sha256: str = Field(pattern=_HEX64)
    active_benchmark_manifest_sha256: str = Field(pattern=_HEX64)
    active_benchmark_registry_sha256: str = Field(pattern=_HEX64)
    public_source_manifest_sha256: str = Field(pattern=_HEX64)
    source_introductions: tuple[PublicSourceIntroduction, ...] = Field(
        min_length=3,
        max_length=3,
    )
    all_source_introductions_postdate_checkpoint: Literal[True] = True
    exact_pool_collection_allowed: Literal[True] = True
    contamination_status: Literal["unknown"] = "unknown"
    heldout_claim_allowed: Literal[False] = False
    unseen_claim_allowed: Literal[False] = False
    source_independent_claim_allowed: Literal[False] = False
    evaluation_claim_allowed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    interpretation: Literal[
        "temporal_non_overlap_only_semantic_and_pretraining_contamination_unknown"
    ]

    def id_payload(self) -> dict[str, object]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key != "overlap_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (
            self.checkpoint_probe.repo_id != self.model_repo_id
            or self.checkpoint_probe.requested_revision != self.model_revision
        ):
            raise ValueError("checkpoint probe differs from overlap model pin")
        problem_ids = [item.problem_record_id for item in self.source_introductions]
        if problem_ids != sorted(set(problem_ids)):
            raise ValueError("source introductions must be sorted and unique")
        if any(
            item.introduction_created_at <= self.checkpoint_probe.observed_created_at
            for item in self.source_introductions
        ):
            raise ValueError("a source introduction does not postdate the model checkpoint")
        expected = "research_overlap:" + hash_canonical(
            {"schema": "lf021_research_family_overlap_v1", **self.id_payload()}
        )
        if self.overlap_id != expected:
            raise ValueError("overlap_id does not match immutable overlap payload")
        return self

    @classmethod
    def create(cls, **values: object) -> Self:
        payload = {
            "schema_version": 1,
            "record_kind": "lf021_research_family_overlap",
            **values,
        }
        overlap_id = "research_overlap:" + hash_canonical(
            {"schema": "lf021_research_family_overlap_v1", **payload}
        )
        return cls.model_validate({"overlap_id": overlap_id, **payload})


__all__ = [
    "HFCommitProbeEvidence",
    "PublicSourceIntroduction",
    "ResearchFamilyOverlapRecord",
]
