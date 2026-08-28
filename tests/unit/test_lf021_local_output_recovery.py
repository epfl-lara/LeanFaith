"""Fail-closed LF-021 expected-declaration recovery parser."""

from __future__ import annotations

import datetime
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from leanfaith.config.hashing import sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.generation.local_output_adapter import (
    FinalFenceError,
    FinalFenceErrorCode,
)
from leanfaith.generation.local_output_recovery import (
    RECOVERY_PARSER_ID,
    RecoveryError,
    RecoveryErrorCode,
    extract_expected_declaration_with_lean,
    extract_recovery_envelope,
    primary_failure_allows_recovery,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

ROOT = find_repo_root(Path(__file__).parent)
UTC = datetime.datetime(2026, 7, 23, 23, 30, tzinfo=datetime.UTC)
HEADER = "import Mathlib"
CONTEXT_FINGERPRINT = "a" * 64
CONTEXT_ID = f"ctx:{CONTEXT_FINGERPRINT}"
NAME = "lf021_recovery_identity"


def _position(source: str, offset: int) -> dict[str, int]:
    prefix = source[:offset]
    return {
        "line": prefix.count("\n") + 1,
        "column": len(prefix.rsplit("\n", 1)[-1]),
    }


def _declaration(source: str, statement: str) -> dict[str, object]:
    start = source.index(f"theorem {NAME}")
    signature_finish = source.index(" :=", start)
    finish = len(source)
    check = source.find("\nset_option pp.fullNames", signature_finish)
    if check >= 0:
        finish = check
    return {
        "name": NAME,
        "full_name": NAME,
        "kind": "theorem",
        "range": {
            "start": _position(source, start),
            "finish": _position(source, finish),
        },
        "signature": {
            "pp": statement.removeprefix(f"theorem {NAME} "),
            "range": {
                "start": _position(source, start + len(f"theorem {NAME} ")),
                "finish": _position(source, signature_finish),
            },
        },
        "type": {"pp": "n = n"},
    }


class _RecoveryBackend:
    def __init__(self) -> None:
        self.requests: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        assert request.code is not None
        self.requests.append(request)
        source = request.code
        normalized = f"theorem {NAME} : ∀ (n : Nat), n = n"
        if "lfDumpSignaturePP" in source:
            return LeanResult(
                request_id=request.request_id,
                request_hash=sha256_hex(source.encode()),
                context_id=request.context_id,
                context_fingerprint=CONTEXT_FINGERPRINT,
                status=LeanStatus.VALID_WITH_SORRY,
                messages=(
                    {
                        "severity": "info",
                        "data": (
                            f'LFSIGPPJSON {{"name":"{NAME}","signature_pp":"∀ (n : Nat), n = n"}}'
                        ),
                    },
                ),
            )
        statement = normalized if normalized in source else f"theorem {NAME} (n : Nat) : n = n"
        return LeanResult(
            request_id=request.request_id,
            request_hash=sha256_hex(source.encode()),
            context_id=request.context_id,
            context_fingerprint=CONTEXT_FINGERPRINT,
            status=LeanStatus.VALID_WITH_SORRY,
            declarations=(_declaration(source, statement),),
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


def _problem() -> ProblemPoolRecord:
    return ProblemPoolRecord.model_construct(
        problem_record_id="problem:" + "b" * 64,
        context_id=CONTEXT_ID,
    )


def _context() -> ContextRecord:
    return ContextRecord.model_construct(
        context_id=CONTEXT_ID,
        header_text=HEADER,
    )


def test_terminal_lean_fence_is_only_candidate_after_reasoning_fences() -> None:
    raw = (
        "A discarded scratch program follows.\n"
        "```python\nprint('not Lean')\n```\n"
        "```lean4\n"
        "import Mathlib\n"
        f"theorem {NAME} (n : Nat) : n = n := by sorry\n"
        "```\n"
    )
    envelope = extract_recovery_envelope(raw)
    assert envelope.envelope_kind == "terminal_lean_fence"
    assert "print" not in envelope.code
    assert envelope.code.startswith("import Mathlib")


@pytest.mark.parametrize(
    "preamble",
    [
        "import Mathlib",
        "import Aesop",
        "open BigOperators Real Nat Topology Rat Classical Polynomial",
        "open scoped BigOperators",
        "set_option maxHeartbeats 0",
        "set_option maxRecDepth 10000",
        "/- harmless /- nested -/ comment -/\n-- another comment",
    ],
)
def test_allowlisted_preambles_reach_lean(preamble: str) -> None:
    backend = _RecoveryBackend()
    result = extract_expected_declaration_with_lean(
        raw_output=f"{preamble}\ntheorem {NAME} (n : Nat) : n = n := by\n  exact rfl",
        expected_declaration_name=NAME,
        registered_header=HEADER,
        problem=_problem(),
        context=_context(),
        backend=cast(LeanInteractBackend, backend),
        created_at=UTC,
    )
    assert result.parsed.statement == f"theorem {NAME} : ∀ (n : Nat), n = n"
    assert len(backend.requests) == 3


@pytest.mark.parametrize(
    "preamble",
    [
        "namespace Unsafe",
        "section",
        "variable (n : Nat)",
        'notation "boom" => True',
        "@[simp] axiom bad : False",
        "def helper := 1",
        "axiom bad : False",
        "instance : Inhabited Nat := ⟨0⟩",
        "set_option pp.all true",
        "set_option maxHeartbeats 0 in",
        "import Batteries",
        "open Real; axiom bad : False",
    ],
)
def test_forbidden_preambles_fail_before_lean(preamble: str) -> None:
    backend = _RecoveryBackend()
    with pytest.raises(RecoveryError) as error:
        extract_expected_declaration_with_lean(
            raw_output=f"{preamble}\ntheorem {NAME} : True := by trivial",
            expected_declaration_name=NAME,
            registered_header=HEADER,
            problem=_problem(),
            context=_context(),
            backend=cast(LeanInteractBackend, backend),
            created_at=UTC,
        )
    assert error.value.code is RecoveryErrorCode.FORBIDDEN_PREAMBLE
    assert backend.requests == []


def test_exact_declaration_name_and_count_are_required_before_lean() -> None:
    backend = _RecoveryBackend()
    with pytest.raises(RecoveryError) as wrong_name:
        extract_expected_declaration_with_lean(
            raw_output="theorem wrong_name : True := by trivial",
            expected_declaration_name=NAME,
            registered_header=HEADER,
            problem=_problem(),
            context=_context(),
            backend=cast(LeanInteractBackend, backend),
            created_at=UTC,
        )
    assert wrong_name.value.code is RecoveryErrorCode.DECLARATION_NAME

    with pytest.raises(RecoveryError) as two:
        extract_expected_declaration_with_lean(
            raw_output=(
                f"theorem {NAME} : True := by trivial\nlemma another_name : True := by trivial"
            ),
            expected_declaration_name=NAME,
            registered_header=HEADER,
            problem=_problem(),
            context=_context(),
            backend=cast(LeanInteractBackend, backend),
            created_at=UTC,
        )
    assert two.value.code is RecoveryErrorCode.DECLARATION_COUNT
    assert backend.requests == []


def test_proof_is_never_carried_into_normalized_output_or_revalidation() -> None:
    backend = _RecoveryBackend()
    sentinel = "RECOVERY_PROOF_SENTINEL_DO_NOT_COPY"
    result = extract_expected_declaration_with_lean(
        raw_output=(
            f"import Mathlib\ntheorem {NAME} (n : Nat) : n = n := by\n  -- {sentinel}\n  exact rfl"
        ),
        expected_declaration_name=NAME,
        registered_header=HEADER,
        problem=_problem(),
        context=_context(),
        backend=cast(LeanInteractBackend, backend),
        created_at=UTC,
    )
    assert sentinel not in result.parsed.statement
    assert sentinel in cast(str, backend.requests[0].code)
    assert all(sentinel not in cast(str, request.code) for request in backend.requests[1:])
    assert backend.requests[-1].code == (
        f"import Mathlib\ntheorem {NAME} : ∀ (n : Nat), n = n := by sorry"
    )


def test_fallback_policy_never_retries_genuine_lean_invalid() -> None:
    assert primary_failure_allows_recovery(
        FinalFenceError(FinalFenceErrorCode.DECLARATION_COUNT, "preamble")
    )
    assert not primary_failure_allows_recovery(
        FinalFenceError(FinalFenceErrorCode.LEAN_INVALID, "type error")
    )
    assert not primary_failure_allows_recovery(ValueError("not a frozen parser failure"))


def test_actual_nine_output_envelopes_have_six_policy_eligible_primary_failures() -> None:
    """Model-free regression over immutable v1 failure/raw artifacts."""

    collection = (
        ROOT
        / "data"
        / "raw"
        / "real_outputs"
        / "public_research_v1"
        / "local_collection_v1"
        / "75e16a5cb7ba937463821c92ef612c25475d91e7af00fb38bc2c970fa3dc2393"
    )
    if not (collection / "postprocess_v1" / "manifest.json").is_file():
        pytest.skip("immutable nine-output postprocess_v1 artifact is not present")
    eligible = []
    for path in sorted(
        (collection / "postprocess_v1" / "invocations").glob("*/processing_terminal.json")
    ):
        terminal = json.loads(path.read_text(encoding="utf-8"))
        if terminal["failure_code"] in {
            FinalFenceErrorCode.DECLARATION_COUNT.value,
            FinalFenceErrorCode.HEADER_MISMATCH.value,
        }:
            invocation = collection / "invocations" / path.parent.name
            raw_record = json.loads(
                (invocation / "local_generation_result.json").read_text(encoding="utf-8")
            )
            envelope = extract_recovery_envelope(raw_record["raw_text"])
            assert envelope.code
            eligible.append(path.parent.name)
    assert len(eligible) == 6
    assert RECOVERY_PARSER_ID == "lean_expected_declaration_recovery_v1"
