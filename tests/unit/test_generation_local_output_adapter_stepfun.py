"""Strict StepFun reasoning-wrapper normalization before the stable v3 parser."""

from __future__ import annotations

import pytest

from leanfaith.generation.local_output_adapter import (
    FinalFenceError,
    FinalFenceErrorCode,
    FinalLeanFence,
    parser_source_sha256,
)
from leanfaith.generation.local_output_adapter_stepfun import (
    BASE_TERMINAL_PARSER_SOURCE_SHA256,
    extract_stepfun_terminal_fence_or_raw_completion,
    stepfun_parser_source_sha256,
)

HEADER = "import Mathlib\n"
STATEMENT = "theorem lf021_stepfun_generated (n : Nat) : n + 1 = 1 + n := by sorry"


def test_observed_stepfun_think_close_plus_terminal_fence_is_accepted() -> None:
    raw = (
        "# Reasoning\n\n"
        "A scratch header is:\n"
        "```lean\n"
        "import Mathlib\n"
        "```\n\n"
        "The final answer follows.\n"
        "</think>```Lean4\n"
        f"{HEADER}"
        f"{STATEMENT}\n"
        "```\n"
    )
    result = extract_stepfun_terminal_fence_or_raw_completion(
        raw,
        registered_header=HEADER,
    )
    assert isinstance(result, FinalLeanFence)
    assert result.included_registered_header
    assert result.candidate_body == STATEMENT


def test_stepfun_adapter_delegates_normal_v3_output_without_rewriting_it() -> None:
    raw = f"reasoning\n```lean4\n{STATEMENT}\n```"
    result = extract_stepfun_terminal_fence_or_raw_completion(
        raw,
        registered_header=HEADER,
    )
    assert isinstance(result, FinalLeanFence)
    assert result.candidate_body == STATEMENT


@pytest.mark.parametrize(
    "raw",
    [
        f"arbitrary</think>```Lean4\n{HEADER}{STATEMENT}\n```",
        f"</think> ```Lean4\n{HEADER}{STATEMENT}\n```",
    ],
)
def test_stepfun_adapter_never_strips_a_nonexact_reasoning_prefix(raw: str) -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_stepfun_terminal_fence_or_raw_completion(
            raw,
            registered_header=HEADER,
        )
    assert error.value.code is FinalFenceErrorCode.MALFORMED_FENCE


def test_stepfun_reasoning_boundary_must_open_the_authoritative_terminal_block() -> None:
    raw = f"</think>```Lean4\n{HEADER}{STATEMENT}\n```\n```lean4\n{STATEMENT}\n```"
    with pytest.raises(FinalFenceError) as error:
        extract_stepfun_terminal_fence_or_raw_completion(
            raw,
            registered_header=HEADER,
        )
    assert error.value.code is FinalFenceErrorCode.MULTIPLE_LEAN_FENCES


def test_stepfun_terminal_block_rejects_trailing_output() -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_stepfun_terminal_fence_or_raw_completion(
            f"</think>```Lean4\n{HEADER}{STATEMENT}\n```\ntrailing text",
            registered_header=HEADER,
        )
    assert error.value.code is FinalFenceErrorCode.TRAILING_OUTPUT


def test_stepfun_terminal_block_keeps_exact_registered_header_policy() -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_stepfun_terminal_fence_or_raw_completion(
            f"</think>```Lean4\nimport Aesop\n{STATEMENT}\n```",
            registered_header=HEADER,
        )
    assert error.value.code is FinalFenceErrorCode.HEADER_MISMATCH


def test_stepfun_adapter_hash_binds_the_unchanged_v3_dependency() -> None:
    assert parser_source_sha256() == BASE_TERMINAL_PARSER_SOURCE_SHA256
    assert len(stepfun_parser_source_sha256()) == 64
