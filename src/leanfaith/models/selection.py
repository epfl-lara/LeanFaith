"""Decision-complete backbone-pilot winner rule (ADR-0004)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PilotCandidateResult:
    model_id: str
    auprc_deficit_simultaneous_upper: float
    relation_f1_deficit_simultaneous_upper: float
    cached_reference_batch32_pairs_per_second: float
    peak_memory_bytes: int
    loaded_parameter_count: int


def select_backbone(
    candidates: tuple[PilotCandidateResult, ...],
    *,
    auprc_margin: float = 0.01,
    relation_margin: float = 0.02,
    throughput_tie_fraction: float = 0.05,
) -> PilotCandidateResult:
    """Apply quality bounds, then deterministic Pareto tie-breaking."""

    if not candidates:
        raise ValueError("backbone pilot has no eligible candidates")
    survivors = [
        candidate
        for candidate in candidates
        if candidate.auprc_deficit_simultaneous_upper <= auprc_margin
        and candidate.relation_f1_deficit_simultaneous_upper <= relation_margin
    ]
    if not survivors:
        raise ValueError("no candidate satisfies both preregistered quality bounds")
    if any(candidate.cached_reference_batch32_pairs_per_second <= 0 for candidate in survivors):
        raise ValueError("throughput must be positive")
    fastest = max(candidate.cached_reference_batch32_pairs_per_second for candidate in survivors)
    tied = [
        candidate
        for candidate in survivors
        if candidate.cached_reference_batch32_pairs_per_second
        >= fastest * (1.0 - throughput_tie_fraction)
    ]
    return min(
        tied,
        key=lambda candidate: (
            candidate.peak_memory_bytes,
            candidate.loaded_parameter_count,
            candidate.model_id,
        ),
    )
