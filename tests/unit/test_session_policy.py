"""LF-009: retry policy and lineage (§8.7, §28.4) — LeanInteract-free."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import (
    RetryPolicy,
    ServerMode,
    run_with_retries,
    semantic_identity,
)

_CTX = "ctx:" + "0" * 64


def _request() -> LeanRequest:
    return LeanRequest(request_id="r", context_id=_CTX, code="theorem t : True := trivial")


def _result(status: LeanStatus, attempt: str = "0") -> LeanResult:
    return LeanResult(
        request_id="r",
        request_hash="h" * 64,
        context_id=_CTX,
        context_fingerprint="0" * 64,
        status=status,
        infrastructure_error=f"attempt {attempt}",
    )


def _scripted(statuses: list[LeanStatus]) -> tuple[list[LeanRequest], object]:
    seen: list[LeanRequest] = []
    iterator: Iterator[LeanStatus] = iter(statuses)

    def run(request: LeanRequest) -> LeanResult:
        seen.append(request)
        return _result(next(iterator), attempt=str(request.metadata.get("attempt")))

    return seen, run


def test_crash_then_success_retries_with_lineage() -> None:
    seen, run = _scripted([LeanStatus.CRASH, LeanStatus.VALID])
    outcome = run_with_retries(run, _request(), RetryPolicy(max_attempts=3))  # type: ignore[arg-type]
    assert outcome.result.status == LeanStatus.VALID
    assert [r.status for r in outcome.attempts] == [LeanStatus.CRASH, LeanStatus.VALID]
    assert outcome.retried
    assert [request.metadata["attempt"] for request in seen] == ["0", "1"]


def test_retries_are_bounded() -> None:
    seen, run = _scripted([LeanStatus.CRASH] * 5)
    outcome = run_with_retries(run, _request(), RetryPolicy(max_attempts=2))  # type: ignore[arg-type]
    assert outcome.result.status == LeanStatus.CRASH
    assert len(outcome.attempts) == 2
    assert len(seen) == 2


def test_semantic_results_never_retried() -> None:
    for status in (LeanStatus.VALID, LeanStatus.INVALID, LeanStatus.VALID_WITH_SORRY):
        _seen, run = _scripted([status, LeanStatus.VALID])
        outcome = run_with_retries(run, _request(), RetryPolicy(max_attempts=3))  # type: ignore[arg-type]
        assert len(outcome.attempts) == 1
        assert not outcome.retried


def test_timeout_not_retried_by_default() -> None:
    _, run = _scripted([LeanStatus.TIMEOUT, LeanStatus.VALID])
    outcome = run_with_retries(run, _request())  # type: ignore[arg-type]
    assert outcome.result.status == LeanStatus.TIMEOUT
    assert len(outcome.attempts) == 1


def test_timeout_retryable_when_configured() -> None:
    policy = RetryPolicy(
        max_attempts=2,
        retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.TIMEOUT}),
    )
    _, run = _scripted([LeanStatus.TIMEOUT, LeanStatus.VALID])
    outcome = run_with_retries(run, _request(), policy)  # type: ignore[arg-type]
    assert outcome.result.status == LeanStatus.VALID


def test_semantic_statuses_rejected_in_policy() -> None:
    with pytest.raises(ValueError, match="never retryable"):
        RetryPolicy(retry_statuses=frozenset({LeanStatus.INVALID}))


def test_policy_requires_positive_attempts() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RetryPolicy(max_attempts=0)


def test_semantic_identity_projection() -> None:
    results = [_result(LeanStatus.VALID), _result(LeanStatus.INVALID)]
    assert semantic_identity(results) == (
        ("h" * 64, "valid"),
        ("h" * 64, "invalid"),
    )


def test_server_mode_spellings() -> None:
    assert {mode.value for mode in ServerMode} == {"stable", "auto", "pool"}
