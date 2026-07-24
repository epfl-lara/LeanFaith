"""Baseline adapters; heavyweight model execution remains phase-gated."""

from leanfaith.baselines.formalrx import (
    FormalRxVerdict,
    aligned_probability_from_log_likelihoods,
    parse_formalrx_verdict,
)

__all__ = [
    "FormalRxVerdict",
    "aligned_probability_from_log_likelihoods",
    "parse_formalrx_verdict",
]
