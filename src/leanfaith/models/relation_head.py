"""Coherent hierarchical relation-probability contract for M2/M3."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RelationProbabilities:
    equivalent: float
    A_stronger: float
    B_stronger: float
    incomparable: float
    unrelated: float
    ambiguous: float

    @property
    def same_claim_probability(self) -> float:
        return self.equivalent

    def swapped(self) -> RelationProbabilities:
        return RelationProbabilities(
            equivalent=self.equivalent,
            A_stronger=self.B_stronger,
            B_stronger=self.A_stronger,
            incomparable=self.incomparable,
            unrelated=self.unrelated,
            ambiguous=self.ambiguous,
        )


def factor_relation_probabilities(
    *,
    ambiguity_probability: float,
    equivalent_given_nonambiguous: float,
    non_equivalent_conditional: dict[str, float],
) -> RelationProbabilities:
    """Factor the terminal relation distribution exactly as preregistered.

    `non_equivalent_conditional` is conditional on both non-ambiguity and
    non-equivalence, and therefore contains only the four non-equivalent
    terminal relations.
    """

    values = (ambiguity_probability, equivalent_given_nonambiguous)
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in values):
        raise ValueError("ambiguity/equivalence probabilities must be finite in [0,1]")
    expected = {"A_stronger", "B_stronger", "incomparable", "unrelated"}
    if set(non_equivalent_conditional) != expected:
        raise ValueError(f"conditional relation keys must be exactly {sorted(expected)}")
    if any(
        not math.isfinite(value) or not 0.0 <= value <= 1.0
        for value in non_equivalent_conditional.values()
    ):
        raise ValueError("conditional relation probabilities must be finite in [0,1]")
    if not math.isclose(sum(non_equivalent_conditional.values()), 1.0, abs_tol=1e-7):
        raise ValueError("conditional non-equivalent probabilities must sum to one")

    nonambiguous = 1.0 - ambiguity_probability
    nonequivalent = nonambiguous * (1.0 - equivalent_given_nonambiguous)
    return RelationProbabilities(
        ambiguous=ambiguity_probability,
        equivalent=nonambiguous * equivalent_given_nonambiguous,
        A_stronger=nonequivalent * non_equivalent_conditional["A_stronger"],
        B_stronger=nonequivalent * non_equivalent_conditional["B_stronger"],
        incomparable=nonequivalent * non_equivalent_conditional["incomparable"],
        unrelated=nonequivalent * non_equivalent_conditional["unrelated"],
    )
