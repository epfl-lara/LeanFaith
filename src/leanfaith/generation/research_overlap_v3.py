"""Cross-domain checkpoint-to-public-pool overlap records for LF-021.

This separately versioned record binds the exact cross-domain operational pool.
It preserves the conservative interpretation: temporal non-overlap permits raw
local collection over the exact bound pool, but it is never evidence that a
checkpoint is uncontaminated, held out, unseen, or suitable for evaluation.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.generation.research_overlap import (
    HFCommitProbeEvidence,
    PublicSourceIntroduction,
)

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_OVERLAP_ID = r"^research_overlap_v3:[0-9a-f]{64}$"


class ResearchFamilyOverlapRecordV3(StrictModel):
    """Cautious authorization for the frozen cross-domain public pool."""

    schema_version: Literal[3] = 3
    record_kind: Literal["lf021_research_family_overlap_v3"] = "lf021_research_family_overlap_v3"
    overlap_id: str = Field(pattern=_OVERLAP_ID)
    family_id: str = Field(min_length=1)
    model_repo_id: str = Field(min_length=1)
    model_revision: str = Field(pattern=_HEX40)
    checkpoint_probe: HFCommitProbeEvidence
    pinned_readme_sha256: str = Field(pattern=_HEX64)
    training_lineage_disclosure: str = Field(min_length=1)
    problem_pool_records_sha256: str = Field(pattern=_HEX64)
    problem_pool_manifest_sha256: str = Field(pattern=_HEX64)
    active_benchmark_manifest_sha256: str = Field(pattern=_HEX64)
    active_benchmark_registry_sha256: str = Field(pattern=_HEX64)
    public_source_evidence_sha256: str = Field(pattern=_HEX64)
    problem_count: int = Field(ge=1)
    source_introductions: tuple[PublicSourceIntroduction, ...] = Field(min_length=1)
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
        """Return the immutable payload covered by ``overlap_id``."""

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
        if len(self.source_introductions) != self.problem_count:
            raise ValueError("source-introduction count must equal problem_count")
        problem_record_ids = tuple(
            introduction.problem_record_id for introduction in self.source_introductions
        )
        if problem_record_ids != tuple(sorted(set(problem_record_ids))):
            raise ValueError("source introductions must be sorted and unique")
        problem_ids = tuple(introduction.problem_id for introduction in self.source_introductions)
        if len(problem_ids) != len(set(problem_ids)):
            raise ValueError("source-introduction problem IDs must be unique")
        if any(
            introduction.introduction_created_at <= self.checkpoint_probe.observed_created_at
            for introduction in self.source_introductions
        ):
            raise ValueError("a source introduction does not postdate the model checkpoint")
        expected = "research_overlap_v3:" + hash_canonical(
            {"schema": "lf021_research_family_overlap_v3", **self.id_payload()}
        )
        if self.overlap_id != expected:
            raise ValueError("overlap_id does not match immutable overlap payload")
        return self

    @classmethod
    def create(cls, **values: object) -> Self:
        """Create an overlap record with a deterministic content-derived ID."""

        normalized = dict(values)
        checkpoint = normalized.get("checkpoint_probe")
        if isinstance(checkpoint, HFCommitProbeEvidence):
            normalized["checkpoint_probe"] = checkpoint.model_dump(mode="json")
        introductions = normalized.get("source_introductions")
        if isinstance(introductions, tuple | list):
            normalized["source_introductions"] = [
                item.model_dump(mode="json") if isinstance(item, PublicSourceIntroduction) else item
                for item in introductions
            ]
        payload = {
            "schema_version": 3,
            "record_kind": "lf021_research_family_overlap_v3",
            **normalized,
        }
        provisional = cls.model_construct(
            None,
            overlap_id="research_overlap_v3:" + ("0" * 64),
            **payload,
        )
        complete_payload = provisional.model_dump(mode="json", warnings=False)
        complete_payload.pop("overlap_id")
        overlap_id = "research_overlap_v3:" + hash_canonical(
            {"schema": "lf021_research_family_overlap_v3", **complete_payload}
        )
        return cls.model_validate({"overlap_id": overlap_id, **complete_payload})


__all__ = ["ResearchFamilyOverlapRecordV3"]
