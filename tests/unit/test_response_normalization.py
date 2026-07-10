"""LF-008: pure normalization of responses and exceptions (§8.6, A.6)."""

from __future__ import annotations

import pytest

from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.lean.response_normalization import (
    classify_response,
    normalize_exception,
    normalize_repl_error,
    normalize_response,
    status_for_exception,
)

_CTX = "ctx:" + "0" * 64


def _request(**overrides: object) -> LeanRequest:
    payload: dict[str, object] = {
        "request_id": "req-1",
        "context_id": _CTX,
        "code": "theorem t : True := trivial",
    }
    payload.update(overrides)
    return LeanRequest(**payload)  # type: ignore[arg-type]


_ERROR_MESSAGE = {"severity": "error", "data": "Type mismatch", "start_pos": {"line": 1}}
_SORRY_WARNING = {"severity": "warning", "data": "declaration uses `sorry`"}
_ROOT_GOAL_SORRY = {"goal": "x y : Nat\n⊢ x + y = y + x", "proof_state": 0}


def test_error_message_means_invalid() -> None:
    raw = {"messages": [_ERROR_MESSAGE], "sorries": []}
    assert classify_response(raw, root_goals_requested=False) == LeanStatus.INVALID


def test_sorry_diagnostic_means_placeholder_valid() -> None:
    raw = {"messages": [_SORRY_WARNING], "sorries": [_ROOT_GOAL_SORRY]}
    assert classify_response(raw, root_goals_requested=False) == LeanStatus.VALID_WITH_SORRY


def test_clean_response_is_valid() -> None:
    raw = {"messages": [], "sorries": []}
    assert classify_response(raw, root_goals_requested=False) == LeanStatus.VALID


def test_sorries_without_root_goals_request_mean_placeholder() -> None:
    raw = {"messages": [], "sorries": [_ROOT_GOAL_SORRY]}
    assert classify_response(raw, root_goals_requested=False) == LeanStatus.VALID_WITH_SORRY


def test_root_goal_sorries_do_not_flip_status() -> None:
    # Pinned 0.11.4 caveat: root_goals=True reports goals via sorries even for
    # fully proved code; status must stay VALID.
    raw = {"messages": [], "sorries": [_ROOT_GOAL_SORRY]}
    assert classify_response(raw, root_goals_requested=True) == LeanStatus.VALID


def test_normalize_response_extracts_root_goals() -> None:
    raw = {"messages": [], "sorries": [_ROOT_GOAL_SORRY], "declarations": []}
    result = normalize_response(
        _request(root_goals=True),
        raw,
        request_hash="h" * 64,
        context_fingerprint="0" * 64,
        elapsed_ms=5,
        raw_response_path=None,
    )
    assert result.status == LeanStatus.VALID
    assert result.root_goals == ("x y : Nat\n⊢ x + y = y + x",)
    assert result.sorries == ()  # root-goal channel entries are not placeholders


def test_normalize_response_keeps_real_sorries_with_root_goals() -> None:
    raw = {"messages": [_SORRY_WARNING], "sorries": [_ROOT_GOAL_SORRY]}
    result = normalize_response(
        _request(root_goals=True),
        raw,
        request_hash="h" * 64,
        context_fingerprint="0" * 64,
        elapsed_ms=5,
        raw_response_path=None,
    )
    assert result.status == LeanStatus.VALID_WITH_SORRY
    assert len(result.sorries) == 1


def test_normalize_response_preserves_messages_and_declarations() -> None:
    raw = {
        "messages": [_ERROR_MESSAGE],
        "sorries": [],
        "declarations": [{"name": "t", "kind": "theorem"}],
    }
    result = normalize_response(
        _request(declarations=True),
        raw,
        request_hash="h" * 64,
        context_fingerprint="0" * 64,
        elapsed_ms=5,
        raw_response_path="artifacts/raw_lean_responses/x.json",
    )
    assert result.status == LeanStatus.INVALID
    assert result.messages[0]["data"] == "Type mismatch"
    assert result.declarations[0]["name"] == "t"
    assert result.raw_response_path == "artifacts/raw_lean_responses/x.json"


def test_repl_error_is_infrastructure_not_semantic() -> None:
    result = normalize_repl_error(
        _request(),
        "unknown environment",
        request_hash="h" * 64,
        context_fingerprint="0" * 64,
        elapsed_ms=5,
        raw_response_path=None,
    )
    assert result.status == LeanStatus.INTERNAL_ERROR
    assert result.infrastructure_error is not None
    assert "unknown environment" in result.infrastructure_error


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (TimeoutError("slow"), LeanStatus.TIMEOUT),
        (ConnectionAbortedError("died"), LeanStatus.CRASH),
        (ChildProcessError("not running"), LeanStatus.CRASH),
        (BrokenPipeError("pipe"), LeanStatus.CRASH),
        (EOFError("eof"), LeanStatus.CRASH),
        (RuntimeError("odd"), LeanStatus.INTERNAL_ERROR),
    ],
)
def test_exception_status_mapping(exc: BaseException, status: LeanStatus) -> None:
    assert status_for_exception(exc) == status


def test_normalize_exception_preserves_digest() -> None:
    result = normalize_exception(
        _request(),
        TimeoutError("The Lean server did not respond"),
        request_hash="h" * 64,
        context_fingerprint="0" * 64,
        elapsed_ms=30000,
        raw_response_path=None,
    )
    assert result.status == LeanStatus.TIMEOUT
    assert result.infrastructure_error is not None
    assert result.infrastructure_error.startswith("TimeoutError:")
