"""Render the frozen proposer and blinded judge templates."""

from __future__ import annotations

from collections.abc import Mapping

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.models import Polarity, SlotConfig


class PromptRenderError(ValueError):
    """A template token or blinded-prompt invariant failed."""


def _render(template: str, values: Mapping[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        token = "{{" + key + "}}"
        if token not in rendered:
            raise PromptRenderError(f"template is missing required token {token}")
        rendered = rendered.replace(token, value)
    if "{{" in rendered or "}}" in rendered:
        raise PromptRenderError("rendered prompt contains an unresolved template token")
    if "\x00" in rendered or not rendered.strip():
        raise PromptRenderError("rendered prompt is empty or contains NUL")
    return rendered


def render_proposer_prompt(
    loaded: LoadedSFT2AConfig,
    *,
    slot: SlotConfig,
    attempt_number: int,
    attempt_feedback: str | None,
) -> str:
    if not 1 <= attempt_number <= slot.max_attempts:
        raise PromptRenderError("attempt number is outside the frozen three-attempt cap")
    context = loaded.config.root.compile_context.model_dump(mode="json", exclude={"project_dir"})
    feedback = (
        "No prior candidate exists for this slot."
        if attempt_feedback is None
        else "PRIOR ATTEMPT OUTCOME (do not repeat it):\n" + attempt_feedback.strip()
    )
    return _render(
        loaded.proposer_prompt,
        {
            "REFERENCE_GOAL": loaded.config.root.expected_reference_goal_v1,
            "REFERENCE_SIGNATURE": loaded.config.root.reference_signature,
            "COMPILE_CONTEXT": canonical_json_bytes(context).decode("utf-8"),
            "REQUESTED_POLARITY": slot.requested_polarity,
            "SLOT_ID": slot.slot_id,
            "ATTEMPT_NUMBER": str(attempt_number),
            "PREFERRED_MECHANISM": slot.preferred_mechanism,
            "ATTEMPT_FEEDBACK": feedback,
        },
    )


def render_blinded_judge_prompt(
    loaded: LoadedSFT2AConfig,
    *,
    statement_a: str,
    statement_b: str,
) -> str:
    prompt = _render(
        loaded.judge_prompt,
        {"STATEMENT_A": statement_a, "STATEMENT_B": statement_b},
    )
    forbidden = (
        "requested polarity",
        "slot request",
        "preferred mechanism",
        "prior attempt",
        "proposer rationale",
    )
    lowered = prompt.casefold()
    if any(token in lowered for token in forbidden):
        raise PromptRenderError("blinded judge prompt leaked proposer or slot metadata")
    return prompt


def prompt_hash(prompt: str) -> str:
    return sha256_hex(prompt.encode("utf-8"))


def accepted_verdict_for(polarity: Polarity) -> str:
    return "equivalent" if polarity == "preserving" else "non_equivalent"


__all__ = [
    "PromptRenderError",
    "accepted_verdict_for",
    "prompt_hash",
    "render_blinded_judge_prompt",
    "render_proposer_prompt",
]
