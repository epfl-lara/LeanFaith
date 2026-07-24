"""Verdict-only FormalRx comparison primitives (PLAN.md §23.12).

This module intentionally contains no localization or correction adapter.
Model loading, the full paper prompt, and decoding are enabled only at LF-027
after the prompt/checkpoint revision is frozen.
"""

from __future__ import annotations

import math
import re
from enum import StrEnum

_VERDICT = re.compile(
    r"^[ \t]*Semantic Alignment:[ \t]*(Aligned|Misaligned)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


class FormalRxVerdict(StrEnum):
    ALIGNED = "Aligned"
    MISALIGNED = "Misaligned"


def parse_formalrx_verdict(text: str) -> FormalRxVerdict | None:
    """Parse exactly one FormalRx verdict field; malformed output abstains."""

    matches = _VERDICT.findall(text)
    normalized = {match.lower() for match in matches}
    if len(matches) != 1 or len(normalized) != 1:
        return None
    return (
        FormalRxVerdict.ALIGNED if matches[0].lower() == "aligned" else FormalRxVerdict.MISALIGNED
    )


def aligned_probability_from_log_likelihoods(
    aligned_log_likelihood: float,
    misaligned_log_likelihood: float,
) -> float:
    """Normalize teacher-forced exact-continuation sequence log likelihoods."""

    if not math.isfinite(aligned_log_likelihood) or not math.isfinite(misaligned_log_likelihood):
        raise ValueError("FormalRx continuation log likelihoods must be finite")
    maximum = max(aligned_log_likelihood, misaligned_log_likelihood)
    aligned = math.exp(aligned_log_likelihood - maximum)
    misaligned = math.exp(misaligned_log_likelihood - maximum)
    return aligned / (aligned + misaligned)
