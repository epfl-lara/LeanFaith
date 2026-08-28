"""Group-first stratified partition of the golden pairs (Track A1).

Every underlying problem (``group_key``) lands in exactly one bucket across
ALL datasets — the leakage rule. BEq groups are forced whole into
``final_test`` (independent annotators). The remaining groups are assigned by
deterministic greedy stratified allocation (pure hashing over few, uneven
groups cannot deliver balanced buckets — Codex review finding 11). Strata are
(dataset family x label) counts over expert-labeled, non-conflicted pairs.

ProofNetVerif rows never enter the headline test: PNV pairs whose group falls
in ``final_test`` are overridden to ``quarantine``.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from leanfaith.eval.schema import GoldenPair, Partition

_BUCKETS: tuple[Partition, ...] = ("final_test", "dev", "golden_train")
_RATIOS: dict[Partition, float] = {"final_test": 0.50, "dev": 0.25, "golden_train": 0.25}
_BEQ_DATASETS = frozenset({"beq_o1", "beq_rauto"})


def _stratum_counts(pairs: list[GoldenPair]) -> dict[tuple[str, bool], int]:
    counts: dict[tuple[str, bool], int] = defaultdict(int)
    for pair in pairs:
        if pair.label_conflict:
            continue
        families = {membership.dataset.split("_", 1)[0] for membership in pair.memberships}
        expert = families & {"epla", "gted"}
        for family in expert:
            counts[(family, pair.label)] += 1
        if not expert:
            # PNV-only mass gets its own stratum so the auxiliary benchmark
            # is spread across buckets too instead of piling into one.
            counts[("pnv", pair.label)] += 1
    return counts


def _tie_break(seed: int, group_key: str) -> str:
    return hashlib.sha256(f"{seed}||{group_key}".encode()).hexdigest()


@dataclass(frozen=True)
class PartitionResult:
    group_partitions: dict[str, Partition]
    pairs: list[GoldenPair]


def assign_partitions(pairs: list[GoldenPair], *, seed: int) -> PartitionResult:
    """Assign every group to one bucket and every pair to its final partition."""

    by_group: dict[str, list[GoldenPair]] = defaultdict(list)
    for pair in pairs:
        by_group[pair.group_key].append(pair)

    group_partitions: dict[str, Partition] = {}
    open_groups: list[str] = []
    for group_key, group_pairs in by_group.items():
        datasets = {m.dataset for pair in group_pairs for m in pair.memberships}
        if datasets & _BEQ_DATASETS:
            group_partitions[group_key] = "final_test"
        else:
            open_groups.append(group_key)

    # Population targets over the OPEN groups only (forced BEq mass already
    # sits in final_test; targets steer the remainder toward the ratios).
    open_totals: dict[tuple[str, bool], int] = defaultdict(int)
    group_strata: dict[str, dict[tuple[str, bool], int]] = {}
    for group_key in open_groups:
        strata = _stratum_counts(by_group[group_key])
        group_strata[group_key] = strata
        for stratum, count in strata.items():
            open_totals[stratum] += count

    targets: dict[Partition, dict[tuple[str, bool], float]] = {
        bucket: {stratum: _RATIOS[bucket] * total for stratum, total in open_totals.items()}
        for bucket in _BUCKETS
    }
    assigned: dict[Partition, dict[tuple[str, bool], float]] = {
        bucket: defaultdict(float) for bucket in _BUCKETS
    }

    def group_weight(group_key: str) -> int:
        return sum(group_strata[group_key].values())

    ordered = sorted(open_groups, key=lambda key: (-group_weight(key), _tie_break(seed, key)))
    for group_key in ordered:
        strata = group_strata[group_key]
        # A PNV-only group can never contribute to the headline test (its
        # pairs would all be quarantined there) — keep it usable.
        candidates: tuple[Partition, ...] = (
            ("dev", "golden_train")
            if strata and all(stratum[0] == "pnv" for stratum in strata)
            else _BUCKETS
        )
        best_bucket: Partition | None = None
        best_cost = float("inf")
        for bucket in candidates:
            cost = 0.0
            if strata:
                for stratum, count in strata.items():
                    target = max(targets[bucket].get(stratum, 0.0), 1e-9)
                    cost += (assigned[bucket][stratum] + count) / target
                cost /= len(strata)
            else:
                # Groups with no expert stratum mass (PNV-only): balance by
                # overall fill fraction instead.
                total_target = max(sum(targets[bucket].values()), 1e-9)
                cost = sum(assigned[bucket].values()) / total_target
            if cost < best_cost - 1e-12 or (
                abs(cost - best_cost) <= 1e-12
                and best_bucket is not None
                and _tie_break(seed, f"{group_key}::{bucket}")
                < _tie_break(seed, f"{group_key}::{best_bucket}")
            ):
                best_cost = cost
                best_bucket = bucket
        assert best_bucket is not None
        group_partitions[group_key] = best_bucket
        for stratum, count in strata.items():
            assigned[best_bucket][stratum] += count

    updated: list[GoldenPair] = []
    for pair in sorted(pairs, key=lambda item: item.pair_id):
        bucket = group_partitions[pair.group_key]
        partition: Partition = bucket
        only_pnv = all(m.dataset == "proofnetverif" for m in pair.memberships)
        if only_pnv and bucket == "final_test":
            partition = "quarantine"
        updated.append(pair.model_copy(update={"partition": partition}))
    return PartitionResult(group_partitions=group_partitions, pairs=updated)


def partition_counts(pairs: list[GoldenPair]) -> dict[str, dict[str, int]]:
    """pair counts per partition per dataset (membership-based)."""

    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for pair in pairs:
        for membership in pair.memberships:
            counts[pair.partition][membership.dataset] += 1
        counts[pair.partition]["canonical_pairs"] += 1
    return {bucket: dict(inner) for bucket, inner in counts.items()}


def build_blocklist(pairs: list[GoldenPair]) -> dict[str, list[str]]:
    """Contamination blocklist: normalized statement hashes + group keys.

    Covers EVERY golden pair (golden_train included): weak/CPT corpora must
    never contain golden text; golden_train reaches training only through the
    explicit golden fine-tune channel.
    """

    from leanfaith.representations.views import signature_near_dup_hash

    hashes: set[str] = set()
    groups: set[str] = set()
    for pair in pairs:
        hashes.add(signature_near_dup_hash(pair.reference_headless))
        hashes.add(signature_near_dup_hash(pair.candidate_headless))
        groups.add(pair.group_key)
    return {
        "version": ["golden_blocklist_v1"],
        "near_dup_hashes": sorted(hashes),
        "group_keys": sorted(groups),
    }
