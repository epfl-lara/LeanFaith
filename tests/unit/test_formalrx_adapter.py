"""Verdict-only FormalRx adapter contract; no diagnostic-task scope creep."""

from __future__ import annotations

import math

import pytest

from leanfaith.baselines.formalrx import (
    FormalRxVerdict,
    aligned_probability_from_log_likelihoods,
    parse_formalrx_verdict,
)


def test_parse_formalrx_verdict() -> None:
    assert (
        parse_formalrx_verdict("[ANSWER_BEGIN]\nSemantic Alignment: Aligned\n")
        is FormalRxVerdict.ALIGNED
    )
    assert (
        parse_formalrx_verdict("Semantic Alignment: Misaligned\nError Type: N/A\n")
        is FormalRxVerdict.MISALIGNED
    )


def test_parse_formalrx_verdict_abstains_on_missing_or_conflicting_fields() -> None:
    assert parse_formalrx_verdict("Error Type: N/A") is None
    assert (
        parse_formalrx_verdict("Semantic Alignment: Aligned\nSemantic Alignment: Misaligned\n")
        is None
    )


def test_teacher_forced_probability_is_stable_and_normalized() -> None:
    probability = aligned_probability_from_log_likelihoods(-1001.0, -1002.0)
    assert probability == pytest.approx(1.0 / (1.0 + math.exp(-1.0)))
    assert aligned_probability_from_log_likelihoods(0.0, 0.0) == 0.5


def test_teacher_forced_probability_rejects_nonfinite_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        aligned_probability_from_log_likelihoods(float("nan"), 0.0)
