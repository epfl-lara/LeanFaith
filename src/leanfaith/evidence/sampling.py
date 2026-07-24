"""Deterministic evidence-job sampling independent of mutation intentions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from math import ceil

from leanfaith.config.hashing import hash_canonical
from leanfaith.evidence.config import EvidenceSamplingConfig
from leanfaith.schemas.pair import PairRecord


def _rank(pair_id: str, seed: str) -> str:
    return hash_canonical({"policy": "stratified_hash_v1", "seed": seed, "pair_id": pair_id})


def select_training_evidence_pairs(
    pairs: Iterable[PairRecord],
    *,
    config: EvidenceSamplingConfig,
    stratum_key: Callable[[PairRecord], tuple[str, ...]],
) -> tuple[str, ...]:
    """Select a bounded deterministic sample within precomputed strata.

    The selector receives the stratum function explicitly and never reads
    ``PairRecord.intended_relation``.  Mutation intentions therefore cannot
    decide whether symbolic evidence is collected.
    """

    training = config.training_sample
    if not training.enabled:
        return ()
    buckets: dict[tuple[str, ...], list[PairRecord]] = defaultdict(list)
    seen_pair_ids: set[str] = set()
    for pair in pairs:
        if pair.pair_id in seen_pair_ids:
            raise ValueError(f"duplicate pair_id in evidence sampling input: {pair.pair_id}")
        seen_pair_ids.add(pair.pair_id)
        stratum = stratum_key(pair)
        if len(stratum) != len(training.strata):
            raise ValueError(
                "evidence sampling stratum arity does not match configured strata: "
                f"expected {len(training.strata)}, got {len(stratum)} for {pair.pair_id}"
            )
        if any(not value for value in stratum):
            raise ValueError(
                f"evidence sampling stratum values must be nonempty for {pair.pair_id}"
            )
        buckets[stratum].append(pair)

    selected: set[str] = set()
    for stratum in sorted(buckets):
        records = sorted(
            buckets[stratum],
            key=lambda pair: (_rank(pair.pair_id, training.hash_seed), pair.pair_id),
        )
        fractional = ceil(len(records) * training.fraction_per_stratum)
        target = max(training.minimum_per_stratum, fractional)
        target = min(training.maximum_per_stratum, len(records), target)
        selected.update(pair.pair_id for pair in records[:target])
    return tuple(sorted(selected))
