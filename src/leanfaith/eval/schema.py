"""Canonical golden-benchmark records (refocus Track A1).

One canonical statement-pair record per unique (group, reference, candidate)
triple, carrying every dataset membership so published per-dataset slices stay
reportable after cross-dataset deduplication (EPLA ⊃ GTED overlap, BEq is
ProofNet-derived). See PLAN.md Track A1.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel

GoldenDataset = Literal[
    "epla_minif2f",
    "epla_proofnet",
    "beq_o1",
    "beq_rauto",
    "gted_minif2f",
    "gted_proofnet",
    "proofnetverif",
]

LabelProvenance = Literal["expert_human", "auto_typecheck_fail", "formal_verified"]

Partition = Literal["final_test", "dev", "golden_train", "quarantine"]

#: Datasets whose labels are direct expert judgments end-to-end. ProofNetVerif
#: is excluded: rows whose candidate failed typecheck were auto-labeled
#: incorrect, so its provenance is decided per row, not per dataset.
EXPERT_DATASETS: frozenset[str] = frozenset(
    {"epla_minif2f", "epla_proofnet", "beq_o1", "beq_rauto", "gted_minif2f", "gted_proofnet"}
)


class DatasetMembership(StrictModel):
    """One dataset's claim over a canonical pair."""

    dataset: GoldenDataset
    row_id: str
    label: bool
    label_provenance: LabelProvenance
    candidate_compiles: bool | None = None
    generator_model: str | None = None


class GoldenPair(StrictModel):
    """One canonical Lean reference/candidate statement pair."""

    pair_id: str
    #: ``minif2f::<name>`` / ``proofnet::<name>`` — the underlying problem
    #: identity every split decision groups by, across ALL datasets.
    group_key: str
    problem_source: Literal["minif2f", "proofnet"]
    problem_name: str
    header: str
    reference_lean: str
    candidate_lean: str
    reference_headless: str
    candidate_headless: str
    #: True when ``normalize_headless`` failed and the raw-minus-proof-tail
    #: fallback was used for that side.
    reference_headless_fallback: bool = False
    candidate_headless_fallback: bool = False
    memberships: tuple[DatasetMembership, ...] = Field(min_length=1)
    #: Resolved binary label: unanimous expert label when expert memberships
    #: agree; the sole membership's label otherwise.
    label: bool
    label_provenance: LabelProvenance
    #: True when memberships disagree on the label. Conflicted pairs are kept
    #: in the record set but excluded from headline metrics by default.
    label_conflict: bool = False
    partition: Partition


def make_pair_id(group_key: str, reference_hash: str, candidate_hash: str) -> str:
    """Deterministic pair identity over the canonical content triple."""

    return hash_canonical(
        {
            "group_key": group_key,
            "reference": reference_hash,
            "candidate": candidate_hash,
        }
    )


class PartitionManifest(StrictModel):
    """Frozen record of the golden partition (committed to the repo)."""

    version: Literal["golden_partition_v1"] = "golden_partition_v1"
    seed: int
    created_utc: str
    git_revision: str
    #: Every group and the bucket all of its rows follow.
    group_partitions: dict[str, Partition]
    #: pair counts per partition per dataset, for at-a-glance review.
    counts: dict[str, dict[str, int]]
    #: sha256 of the canonical pairs JSONL this partition was computed from.
    canonical_pairs_sha256: str
    canonical_pairs_path: str
    total_pairs: int
    conflicted_pairs: int
