"""StepFun-specific terminal-envelope normalization for LF-021.

StepFun's pinned chat template starts assistant reasoning with ``<think>`` and
the model closes that wrapper immediately before its final Lean fence:

``</think>```Lean4``

The stable v3 parser intentionally accepts fence openers only on otherwise
empty lines.  This versioned adapter recognizes exactly the StepFun wrapper
boundary above, removes only ``</think>``, and delegates every fence,
registered-header, declaration, LeanInteract, and proof-stripping check to the
unchanged v3 parser.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import sha256_hex
from leanfaith.generation.local_output_adapter import (
    FinalFenceError,
    FinalFenceErrorCode,
    LeanCompletionEnvelope,
    LeanExtractedCandidate,
    extract_candidate_signature_with_lean_v3,
    extract_final_lean_fence,
    extract_terminal_fence_or_raw_completion,
)
from leanfaith.generation.local_output_adapter import (
    parser_source_sha256 as base_parser_source_sha256,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

STEPFUN_TERMINAL_PARSER_ID: Literal["lean_stepfun_think_terminal_fence_v1"] = (
    "lean_stepfun_think_terminal_fence_v1"
)
BASE_TERMINAL_PARSER_SOURCE_SHA256 = (
    "e02e67675810b81effbb97740dd4a27070be9ef0eef2627b25fb4b9b749d1da4"
)
_THINK_CLOSE_FENCE = re.compile(r"^</think>(?P<fence>[`~]{3,}[^`~]*)$")


def stepfun_parser_source_sha256() -> str:
    """Hash this adapter and fail if its pinned v3 dependency drifted."""

    observed_base = base_parser_source_sha256()
    if observed_base != BASE_TERMINAL_PARSER_SOURCE_SHA256:
        raise RuntimeError(
            "StepFun parser's pinned v3 dependency differs from the executable source"
        )
    return sha256_hex(Path(__file__).read_bytes())


def _normalize_stepfun_terminal_wrapper(
    raw_output: str,
    *,
    registered_header: str,
) -> str:
    """Remove one exact terminal ``</think>`` prefix, or leave v3 input intact."""

    lines = raw_output.splitlines()
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _THINK_CLOSE_FENCE.fullmatch(line)) is not None
    ]
    if not matches:
        return raw_output
    if len(matches) != 1:
        raise FinalFenceError(
            FinalFenceErrorCode.MALFORMED_FENCE,
            f"expected at most one exact StepFun reasoning boundary, observed {len(matches)}",
        )

    opening, match = matches[0]
    normalized_lines = list(lines)
    normalized_lines[opening] = match.group("fence")

    # The recognized boundary itself must open the authoritative terminal
    # block.  This independent suffix check prevents the wrapper marker from
    # being accepted on an earlier scratch fence.
    extract_final_lean_fence(
        "\n".join(normalized_lines[opening:]),
        registered_header=registered_header,
    )
    return "\n".join(normalized_lines)


def extract_stepfun_terminal_fence_or_raw_completion(
    raw_output: str,
    *,
    registered_header: str,
) -> LeanCompletionEnvelope:
    """Normalize the exact StepFun wrapper and apply the complete v3 contract."""

    stepfun_parser_source_sha256()
    normalized = _normalize_stepfun_terminal_wrapper(
        raw_output,
        registered_header=registered_header,
    )
    return extract_terminal_fence_or_raw_completion(
        normalized,
        registered_header=registered_header,
    )


def extract_stepfun_candidate_signature_with_lean(
    *,
    raw_output: str,
    expected_declaration_name: str,
    registered_header: str,
    problem: ProblemPoolRecord,
    context: ContextRecord,
    backend: LeanInteractBackend,
    created_at: datetime.datetime,
) -> LeanExtractedCandidate:
    """Normalize the exact wrapper, then use v3 Lean-backed proof stripping."""

    stepfun_parser_source_sha256()
    normalized = _normalize_stepfun_terminal_wrapper(
        raw_output,
        registered_header=registered_header,
    )
    return extract_candidate_signature_with_lean_v3(
        raw_output=normalized,
        expected_declaration_name=expected_declaration_name,
        registered_header=registered_header,
        problem=problem,
        context=context,
        backend=backend,
        created_at=created_at,
    )


__all__ = [
    "BASE_TERMINAL_PARSER_SOURCE_SHA256",
    "STEPFUN_TERMINAL_PARSER_ID",
    "extract_stepfun_candidate_signature_with_lean",
    "extract_stepfun_terminal_fence_or_raw_completion",
    "stepfun_parser_source_sha256",
]
