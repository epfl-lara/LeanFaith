"""Closure-aware judge parsing and one-retry malformed-output taxonomy for v5."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from leanfaith.sft2a.models import JudgeOutput, JudgeOutputV5
from leanfaith.sft2a.providers import ProviderCallResult

_NEGATIVE_RATIONALE = re.compile(
    r"\b(?:non[- ]equivalent|not equivalent|different (?:claim|assertion)|claims? differ|"
    r"unsatisfiable|strictly (?:weaker|stronger)|does not express the same|cannot recover)\b",
    re.IGNORECASE,
)
_POSITIVE_RATIONALE = re.compile(
    r"\b(?:logically equivalent|interprovable|same intended (?:claim|assertion)|"
    r"express(?:es)? the same|recovers? the original|equivalent restatement)\b",
    re.IGNORECASE,
)


class JudgeProvider(Protocol):
    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult: ...


@dataclass(frozen=True, slots=True)
class ConsistentJudgeResult:
    judgment: JudgeOutput | JudgeOutputV5 | None
    calls: tuple[ProviderCallResult, ...]
    malformed_attempts: tuple[dict[str, object], ...]
    final_prompt: str

    @property
    def malformed_retries(self) -> int:
        return max(0, len(self.calls) - 1)


def verdict_rationale_contradiction(judgment: JudgeOutput | JudgeOutputV5) -> str | None:
    """Detect explicit verdict/rationale polarity contradictions, not mere uncertainty."""

    rationale = " ".join(judgment.rationale.split())
    if judgment.verdict == "equivalent" and _NEGATIVE_RATIONALE.search(rationale):
        return "equivalent_verdict_with_non_equivalent_rationale"
    if judgment.verdict == "non_equivalent" and _POSITIVE_RATIONALE.search(rationale):
        return "non_equivalent_verdict_with_equivalent_rationale"
    return None


def _parse(structured: Mapping[str, object], *, closure_aware: bool) -> JudgeOutput | JudgeOutputV5:
    model = JudgeOutputV5 if closure_aware else JudgeOutput
    return model.model_validate(structured)


def call_consistent_judge(
    provider: JudgeProvider,
    *,
    prompt: str,
    input_ids: Sequence[str],
    closure_aware: bool,
    malformed_retries: int,
) -> ConsistentJudgeResult:
    """Retry only malformed schema/contradiction output; never relabel a disagreement."""

    calls: list[ProviderCallResult] = []
    malformed: list[dict[str, object]] = []
    active_prompt = prompt
    for retry_index in range(malformed_retries + 1):
        call = provider.call(
            prompt=active_prompt,
            input_ids=(*input_ids, f"malformed_retry:{retry_index}"),
        )
        calls.append(call)
        reason: str | None = None
        judgment: JudgeOutput | JudgeOutputV5 | None = None
        try:
            judgment = _parse(call.structured, closure_aware=closure_aware)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors(include_input=False, include_url=False)
            )
            reason = f"schema:{type(exc).__name__}:{details}"
        if judgment is not None:
            reason = verdict_rationale_contradiction(judgment)
        if reason is None:
            return ConsistentJudgeResult(
                judgment=judgment,
                calls=tuple(calls),
                malformed_attempts=tuple(malformed),
                final_prompt=active_prompt,
            )
        malformed.append(
            {
                "attempt": retry_index + 1,
                "call_key": call.call_key,
                "reason": reason,
                "structured": dict(call.structured),
            }
        )
        if retry_index < malformed_retries:
            active_prompt = (
                prompt
                + "\n\nMALFORMED OUTPUT RETRY: Your prior structured verdict was rejected: "
                + reason
                + ". Re-evaluate the entire closed proposition and return one internally "
                "consistent JSON object. A binary equivalent/non_equivalent verdict requires "
                "error_type=none and high or medium confidence. Low confidence requires "
                "verdict=unknown with a non-none error_type. Do not mention this retry."
            )
    return ConsistentJudgeResult(
        judgment=None,
        calls=tuple(calls),
        malformed_attempts=tuple(malformed),
        final_prompt=active_prompt,
    )


__all__ = [
    "ConsistentJudgeResult",
    "call_consistent_judge",
    "verdict_rationale_contradiction",
]
