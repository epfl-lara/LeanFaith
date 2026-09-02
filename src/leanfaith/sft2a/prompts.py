"""Render the frozen proposer and blinded judge templates."""

from __future__ import annotations

import re
from collections.abc import Mapping

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.models import Polarity, SlotConfig


class PromptRenderError(ValueError):
    """A template token or blinded-prompt invariant failed."""


_TEMPLATE_TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


def _render(template: str, values: Mapping[str, str]) -> str:
    # Validate control tokens on the template skeleton before interpolating model-facing Lean.
    # Lean legitimately renders adjacent closing braces (for example `{y | y ∉ {x}}`), so
    # scanning the interpolated prompt for a raw `}}` confuses mathematical data with control
    # syntax and makes a valid certified candidate unrecoverably crash the root worker.
    template_tokens = set(_TEMPLATE_TOKEN.findall(template))
    missing = set(values) - template_tokens
    if missing:
        token = "{{" + sorted(missing)[0] + "}}"
        raise PromptRenderError(f"template is missing required token {token}")
    unresolved = template_tokens - set(values)
    if unresolved:
        token = "{{" + sorted(unresolved)[0] + "}}"
        raise PromptRenderError(f"rendered prompt contains unresolved template token {token}")
    skeleton = _TEMPLATE_TOKEN.sub("", template)
    if "{{" in skeleton or "}}" in skeleton:
        raise PromptRenderError("template contains a malformed template delimiter")
    rendered = template
    for key, value in values.items():
        token = "{{" + key + "}}"
        rendered = rendered.replace(token, value)
    if "\x00" in rendered or not rendered.strip():
        raise PromptRenderError("rendered prompt is empty or contains NUL")
    return rendered


AUTHORING_VIEW_TOKEN = "{{AUTHORING_VIEW}}"
AUTHORING_VIEW_UNAVAILABLE = (
    "UNAVAILABLE: no validated authoring view exists for this reference. Introduce fresh valid "
    "binder names yourself (for example inst_1, h_1); never copy a name containing \u271d."
)


def render_proposer_prompt(
    loaded: LoadedSFT2AConfig,
    *,
    slot: SlotConfig,
    attempt_number: int,
    attempt_feedback: str | None,
    reference_goal: str | None = None,
    authoring_view: str | None = None,
    compile_context: Mapping[str, object] | None = None,
) -> str:
    if not 1 <= attempt_number <= slot.max_attempts:
        raise PromptRenderError("attempt number is outside the frozen three-attempt cap")
    context: Mapping[str, object] = (
        dict(compile_context)
        if compile_context is not None
        else loaded.config.root.compile_context.model_dump(mode="json", exclude={"project_dir"})
    )
    feedback = (
        "No prior candidate exists for this slot."
        if attempt_feedback is None
        else "PRIOR ATTEMPT OUTCOME (do not repeat it):\n" + attempt_feedback.strip()
    )
    values = {
        "REFERENCE_GOAL": reference_goal or loaded.config.root.expected_reference_goal_v1,
        "REFERENCE_SIGNATURE": loaded.config.root.reference_signature,
        "COMPILE_CONTEXT": canonical_json_bytes(dict(context)).decode("utf-8"),
        "REQUESTED_POLARITY": slot.requested_polarity,
        "SLOT_ID": slot.slot_id,
        "ATTEMPT_NUMBER": str(attempt_number),
        "PREFERRED_MECHANISM": slot.preferred_mechanism,
        "ATTEMPT_FEEDBACK": feedback,
    }
    # The v3 sprint proposer template carries the SAFE AUTHORING VIEW token; earlier frozen
    # templates do not, and their rendering must stay byte-identical.
    if AUTHORING_VIEW_TOKEN in loaded.proposer_prompt:
        values["AUTHORING_VIEW"] = (
            authoring_view if authoring_view is not None else AUTHORING_VIEW_UNAVAILABLE
        )
    elif authoring_view is not None:
        raise PromptRenderError("proposer template has no SAFE AUTHORING VIEW token")
    return _render(loaded.proposer_prompt, values)


def render_blinded_judge_prompt(
    loaded: LoadedSFT2AConfig,
    *,
    statement_a: str,
    statement_b: str,
) -> str:
    forbidden = (
        "requested polarity",
        "slot request",
        "preferred mechanism",
        "prior attempt",
        "proposer rationale",
    )
    # Blinding is a property of the frozen template, not of the mathematical statements. A
    # declaration or rendered proposition may contain ordinary words that overlap this metadata.
    lowered = _TEMPLATE_TOKEN.sub("", loaded.judge_prompt).casefold()
    if any(token in lowered for token in forbidden):
        raise PromptRenderError("blinded judge prompt leaked proposer or slot metadata")
    return _render(
        loaded.judge_prompt,
        {"STATEMENT_A": statement_a, "STATEMENT_B": statement_b},
    )


def prompt_hash(prompt: str) -> str:
    return sha256_hex(prompt.encode("utf-8"))


def accepted_verdict_for(polarity: Polarity) -> str:
    return "equivalent" if polarity == "preserving" else "non_equivalent"


__all__ = [
    "AUTHORING_VIEW_TOKEN",
    "AUTHORING_VIEW_UNAVAILABLE",
    "PromptRenderError",
    "accepted_verdict_for",
    "prompt_hash",
    "render_blinded_judge_prompt",
    "render_proposer_prompt",
]
