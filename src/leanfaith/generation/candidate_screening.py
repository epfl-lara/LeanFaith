"""Reusable candidate-side benchmark and duplicate screening for LF-021."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from pydantic import Field

from leanfaith.config.models import StrictModel
from leanfaith.generation.problem_pool import ProblemPoolDenylistBinding
from leanfaith.generation.real_outputs import (
    CandidateScreeningRecord,
    candidate_benchmark_hits,
)
from leanfaith.schemas.ids import HEX64_PATTERN, THEOREM_PREFIX, id_pattern
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord


class PriorCandidateIdentity(StrictModel):
    """Minimal immutable identity needed to reject a prior exact candidate."""

    theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    alpha_identity_fingerprint: str = Field(pattern=HEX64_PATTERN)


@dataclass(frozen=True, slots=True)
class CandidateScreeningIndex:
    """Active benchmark denylist plus prior candidate identities."""

    denylist: ProblemPoolDenylistBinding
    prior_candidates: tuple[PriorCandidateIdentity, ...] = ()

    def __post_init__(self) -> None:
        theorem_ids = [candidate.theorem_id for candidate in self.prior_candidates]
        if theorem_ids != sorted(set(theorem_ids)):
            raise ValueError("prior candidate identities must be sorted and unique")


def screen_materialized_candidate(
    *,
    index: CandidateScreeningIndex,
    problem_record_id: str,
    call_id: str,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
    created_at: datetime.datetime,
) -> CandidateScreeningRecord:
    """Compute, bind, and return the actual benchmark/dedup screen."""

    alpha = representation.alpha_identity_fingerprint
    if alpha is None:
        raise ValueError("candidate screening requires alpha_identity_fingerprint")
    duplicate_ids = tuple(
        sorted(
            candidate.theorem_id
            for candidate in index.prior_candidates
            if candidate.theorem_id != theorem.theorem_id
            and candidate.alpha_identity_fingerprint == alpha
        )
    )
    canonical_id = min((theorem.theorem_id, *duplicate_ids))
    return CandidateScreeningRecord.create(
        problem_record_id=problem_record_id,
        call_id=call_id,
        theorem=theorem,
        representation=representation,
        frozen_registry_hash=index.denylist.registry_content_hash,
        benchmark_hits=candidate_benchmark_hits(
            denylist_index=index.denylist.index,
            theorem=theorem,
            representation=representation,
        ),
        duplicate_candidate_theorem_ids=duplicate_ids,
        canonical_candidate_theorem_id=canonical_id,
        created_at=created_at,
    )


__all__ = [
    "CandidateScreeningIndex",
    "PriorCandidateIdentity",
    "screen_materialized_candidate",
]
