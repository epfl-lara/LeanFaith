"""Strict final-fence parsing for local autoformalizer qualification."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import cast

import pytest

from leanfaith.config.hashing import sha256_hex
from leanfaith.generation.local_output_adapter import (
    FinalFenceError,
    FinalFenceErrorCode,
    FinalLeanFence,
    RawLeanCompletion,
    extract_candidate_signature_with_lean_v2,
    extract_candidate_signature_with_lean_v3,
    extract_final_fence_or_raw_completion,
    extract_final_lean_fence,
    extract_terminal_fence_or_raw_completion,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

UTC = datetime.datetime(2026, 7, 23, 22, 0, tzinfo=datetime.UTC)
HEADER = "import LeanFaithFixtures.Basic"
GOEDEL_HEADER = (
    "import Mathlib\n"
    "import Aesop\n\n"
    "set_option maxHeartbeats 0\n\n"
    "open BigOperators Real Nat Topology Rat"
)
STATEMENT = "theorem generated_identity (n : Nat) : n = n := by sorry"
CONTEXT_FINGERPRINT = "0" * 64
CONTEXT_ID = f"ctx:{CONTEXT_FINGERPRINT}"


def test_accepts_reasoning_before_one_final_lean_fence() -> None:
    result = extract_final_lean_fence(
        f"First check the intended domain.\n```lean4\n{STATEMENT}\n```\n",
        registered_header=HEADER,
    )
    assert result.candidate_body == STATEMENT
    assert not result.included_registered_header


def test_v2_accepts_whole_raw_lean_without_weakening_v1() -> None:
    raw = f"{HEADER}\n{STATEMENT}\n"
    result = extract_final_fence_or_raw_completion(
        raw,
        registered_header=HEADER,
    )
    assert isinstance(result, RawLeanCompletion)
    assert result.included_registered_header
    assert result.candidate_body == STATEMENT

    with pytest.raises(FinalFenceError) as error:
        extract_final_lean_fence(raw, registered_header=HEADER)
    assert error.value.code is FinalFenceErrorCode.MISSING_FINAL_FENCE


def test_v2_preserves_existing_v1_final_fence_behavior() -> None:
    raw = f"Reasoning may precede the final fence.\n```lean4\n{STATEMENT}\n```\n"
    expected = extract_final_lean_fence(raw, registered_header=HEADER)
    observed = extract_final_fence_or_raw_completion(raw, registered_header=HEADER)
    assert isinstance(observed, FinalLeanFence)
    assert observed == expected


def test_v2_rejects_mismatched_raw_header() -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_final_fence_or_raw_completion(
            f"import Mathlib\n{STATEMENT}",
            registered_header=HEADER,
        )
    assert error.value.code is FinalFenceErrorCode.HEADER_MISMATCH


@pytest.mark.parametrize(
    "raw",
    [
        f"Here is the requested Lean declaration.\n{STATEMENT}",
    ],
)
def test_v2_rejects_raw_prose_outside_the_declaration(raw: str) -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_final_fence_or_raw_completion(raw, registered_header=HEADER)
    assert error.value.code is FinalFenceErrorCode.DECLARATION_COUNT


def test_v2_rejects_multiple_raw_declarations() -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_final_fence_or_raw_completion(
            f"{STATEMENT}\nlemma generated_second : True := by trivial",
            registered_header=HEADER,
        )
    assert error.value.code is FinalFenceErrorCode.DECLARATION_COUNT


def test_v3_accepts_goedel_reasoning_with_earlier_lean_fence() -> None:
    raw = (
        "We need express the identity over natural numbers.\n"
        "A scratch attempt is:\n"
        "```lean\n"
        "example (n : Nat) : n = n := by rfl\n"
        "```\n"
        "The exact final declaration is:\n"
        "```lean4\n"
        f"{HEADER}\n"
        f"{STATEMENT}\n"
        "```\n"
    )
    result = extract_terminal_fence_or_raw_completion(
        raw,
        registered_header=HEADER,
    )
    assert isinstance(result, FinalLeanFence)
    assert result.included_registered_header
    assert result.candidate_body == STATEMENT


def test_v3_accepts_only_the_exact_registered_goedel_preamble() -> None:
    terminal = f"```lean4\n{GOEDEL_HEADER}\n\n{STATEMENT}\n```"
    result = extract_terminal_fence_or_raw_completion(
        terminal,
        registered_header=GOEDEL_HEADER + "\n",
    )
    assert isinstance(result, FinalLeanFence)
    assert result.included_registered_header
    assert result.candidate_body == STATEMENT

    with pytest.raises(FinalFenceError) as error:
        extract_terminal_fence_or_raw_completion(
            terminal.replace(
                "open BigOperators Real Nat Topology Rat",
                "open BigOperators Real Nat Topology Rat Classical",
            ),
            registered_header=GOEDEL_HEADER + "\n",
        )
    assert error.value.code is FinalFenceErrorCode.HEADER_MISMATCH


def test_v3_preserves_raw_mode_only_when_there_are_no_fences() -> None:
    result = extract_terminal_fence_or_raw_completion(
        f"{HEADER}\n{STATEMENT}",
        registered_header=HEADER,
    )
    assert isinstance(result, RawLeanCompletion)
    assert result.candidate_body == STATEMENT


def test_v3_selects_only_the_last_of_two_lean_fences() -> None:
    raw = (
        f"```lean\ntheorem discarded_attempt : True := by trivial\n```\n```lean\n{STATEMENT}\n```\n"
    )
    result = extract_terminal_fence_or_raw_completion(
        raw,
        registered_header=HEADER,
    )
    assert isinstance(result, FinalLeanFence)
    assert result.candidate_body == STATEMENT


def test_v3_rejects_output_after_terminal_lean_fence() -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_terminal_fence_or_raw_completion(
            f"```lean\n{STATEMENT}\n```\nThis text must not be ignored.",
            registered_header=HEADER,
        )
    assert error.value.code is FinalFenceErrorCode.TRAILING_OUTPUT


def test_v3_rejects_final_non_lean_fence_even_after_lean_reasoning() -> None:
    raw = f"```lean\n{STATEMENT}\n```\n```text\nnot an authoritative Lean result\n```\n"
    with pytest.raises(FinalFenceError) as error:
        extract_terminal_fence_or_raw_completion(raw, registered_header=HEADER)
    assert error.value.code is FinalFenceErrorCode.MISSING_FINAL_FENCE


@pytest.mark.parametrize(
    "raw",
    [
        f"reasoning\n```lean\n{STATEMENT}",
        f"```text\nscratch\n```\n```lean4\n{STATEMENT}",
    ],
)
def test_v3_rejects_unclosed_fences(raw: str) -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_terminal_fence_or_raw_completion(raw, registered_header=HEADER)
    assert error.value.code is FinalFenceErrorCode.MALFORMED_FENCE


@pytest.mark.parametrize(
    "raw",
    [
        "this is not Lean",
        "```text\ntheorem generated_identity : True := by trivial\n```",
        "theorem",
    ],
)
def test_v2_rejects_malformed_raw_envelopes(raw: str) -> None:
    with pytest.raises(FinalFenceError):
        extract_final_fence_or_raw_completion(raw, registered_header=HEADER)


def _position(source: str, offset: int) -> dict[str, int]:
    prefix = source[:offset]
    return {
        "line": prefix.count("\n") + 1,
        "column": len(prefix.rsplit("\n", 1)[-1]),
    }


class _RangeBackend:
    def run(self, request: LeanRequest) -> LeanResult:
        assert request.code is not None
        source = request.code
        start = source.index("theorem generated_identity")
        signature_finish = source.index(" := by sorry", start)
        trailing_marker = "\n\nAn alternative"
        declaration_finish = (
            source.index(trailing_marker) if trailing_marker in source else len(source)
        )
        declaration = {
            "name": "generated_identity",
            "full_name": "generated_identity",
            "kind": "theorem",
            "range": {
                "start": _position(source, start),
                "finish": _position(source, declaration_finish),
            },
            "signature": {
                "pp": "(n : Nat) : n = n",
                "range": {
                    "start": _position(source, start + len("theorem generated_identity ")),
                    "finish": _position(source, signature_finish),
                },
            },
            "type": {"pp": "n = n"},
        }
        return LeanResult(
            request_id=request.request_id,
            request_hash=sha256_hex(source.encode("utf-8")),
            context_id=request.context_id,
            context_fingerprint=CONTEXT_FINGERPRINT,
            status=LeanStatus.VALID_WITH_SORRY,
            declarations=(declaration,),
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


class _InvalidBackend:
    def run(self, request: LeanRequest) -> LeanResult:
        return LeanResult(
            request_id=request.request_id,
            request_hash="1" * 64,
            context_id=request.context_id,
            context_fingerprint=CONTEXT_FINGERPRINT,
            status=LeanStatus.INVALID,
            messages=({"data": "unexpected identifier"},),
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


def _problem() -> ProblemPoolRecord:
    return ProblemPoolRecord.model_construct(
        problem_record_id="problem:" + "2" * 64,
        context_id=CONTEXT_ID,
    )


def _context() -> ContextRecord:
    return ContextRecord.model_construct(
        context_id=CONTEXT_ID,
        header_text=HEADER,
    )


def test_v2_raw_proof_is_stripped_only_from_lean_ranges() -> None:
    result = extract_candidate_signature_with_lean_v2(
        raw_output=f"{HEADER}\n{STATEMENT}",
        expected_declaration_name="generated_identity",
        registered_header=HEADER,
        problem=_problem(),
        context=_context(),
        backend=cast(LeanInteractBackend, _RangeBackend()),
        created_at=UTC,
    )
    assert isinstance(result.fenced, RawLeanCompletion)
    assert result.parsed.statement == "theorem generated_identity (n : Nat) : n = n"


def test_v2_rejects_bytes_after_lean_reported_declaration_range() -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_candidate_signature_with_lean_v2(
            raw_output=f"{STATEMENT}\n\nAn alternative could use `rfl`.",
            expected_declaration_name="generated_identity",
            registered_header=HEADER,
            problem=_problem(),
            context=_context(),
            backend=cast(LeanInteractBackend, _RangeBackend()),
            created_at=UTC,
        )
    assert error.value.code is FinalFenceErrorCode.TRAILING_OUTPUT


def test_v2_lean_validation_rejects_malformed_raw_lean() -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_candidate_signature_with_lean_v2(
            raw_output="theorem generated_identity (n : Nat) : n =",
            expected_declaration_name="generated_identity",
            registered_header=HEADER,
            problem=_problem(),
            context=_context(),
            backend=cast(LeanInteractBackend, _InvalidBackend()),
            created_at=UTC,
        )
    assert error.value.code is FinalFenceErrorCode.LEAN_INVALID


def test_v3_signature_path_rejects_wrong_terminal_declaration_name() -> None:
    wrong_name = STATEMENT.replace("generated_identity", "generated_wrong")
    with pytest.raises(FinalFenceError) as error:
        extract_candidate_signature_with_lean_v3(
            raw_output=(f"```lean\nexample : True := by trivial\n```\n```lean4\n{wrong_name}\n```"),
            expected_declaration_name="generated_identity",
            registered_header=HEADER,
            problem=_problem(),
            context=_context(),
            backend=cast(LeanInteractBackend, _RangeBackend()),
            created_at=UTC,
        )
    assert error.value.code is FinalFenceErrorCode.DECLARATION_NAME


def test_accepts_only_the_exact_registered_header() -> None:
    result = extract_final_lean_fence(
        f"```Lean4\n{HEADER}\n{STATEMENT}\n```",
        registered_header=HEADER,
    )
    assert result.included_registered_header
    assert result.candidate_body == STATEMENT

    with pytest.raises(FinalFenceError) as error:
        extract_final_lean_fence(
            f"```lean4\nimport Mathlib\n{STATEMENT}\n```",
            registered_header=HEADER,
        )
    assert error.value.code is FinalFenceErrorCode.HEADER_MISMATCH


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (
            "import Mathlib\n\ntheorem generated_identity (n : Nat) : n = n := by sorry",
            FinalFenceErrorCode.MISSING_FINAL_FENCE,
        ),
        (
            f"```lean4\n{STATEMENT}\n```\n```lean4\n{STATEMENT}\n```",
            FinalFenceErrorCode.MULTIPLE_LEAN_FENCES,
        ),
        (
            f"```lean4\n{STATEMENT}\n```\nA second alternative follows.",
            FinalFenceErrorCode.TRAILING_OUTPUT,
        ),
        (
            "```lean4\ntheorem first : True\ntheorem second : True\n```",
            FinalFenceErrorCode.DECLARATION_COUNT,
        ),
    ],
)
def test_final_fence_contract_fails_closed(
    raw: str,
    code: FinalFenceErrorCode,
) -> None:
    with pytest.raises(FinalFenceError) as error:
        extract_final_lean_fence(raw, registered_header=HEADER)
    assert error.value.code is code
