"""Server-mode and retry policy for the Lean boundary (PLAN.md §8.7, LF-009).

LeanInteract-free: modes and retry decisions live here; the adapter maps them
onto ``LeanServer`` / ``AutoLeanServer`` / ``LeanServerPool``. Retries are
bounded, apply only to infrastructure statuses (never to semantic results,
and never as evidence), and append lineage rather than overwriting raw
failures (§28.4).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus


class ServerMode(StrEnum):
    """§8.7 server modes; AUTO stays experimental with a stable fallback."""

    STABLE = "stable"
    AUTO = "auto"
    POOL = "pool"


#: Statuses that may ever be retried. TIMEOUT is retryable only when the
#: policy opts in; semantic statuses (VALID*/INVALID) and SETUP_ERROR are
#: terminal by definition (§8.3, §8.7).
_RETRYABLE = frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry decision (§8.7)."""

    max_attempts: int = 2
    retry_statuses: frozenset[LeanStatus] = field(
        default_factory=lambda: frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR})
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        illegal = self.retry_statuses - _RETRYABLE
        if illegal:
            raise ValueError(
                f"statuses {sorted(s.value for s in illegal)} are terminal and never retryable"
            )


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """Final result plus full attempt lineage (§28.4)."""

    result: LeanResult
    attempts: tuple[LeanResult, ...]

    @property
    def retried(self) -> bool:
        return len(self.attempts) > 1


@dataclass(frozen=True, slots=True)
class BatchRetryOutcome:
    """Ordered final results and per-request attempt lineage for one batch."""

    results: tuple[LeanResult, ...]
    attempts: tuple[tuple[LeanResult, ...], ...]

    @property
    def retried_count(self) -> int:
        """Number of input requests that required at least one retry."""

        return sum(len(lineage) > 1 for lineage in self.attempts)


def run_with_retries(
    run: Callable[[LeanRequest], LeanResult],
    request: LeanRequest,
    policy: RetryPolicy | None = None,
) -> RetryOutcome:
    """Execute ``run`` with bounded retries on infrastructure statuses.

    Each attempt carries ``metadata["attempt"]`` so the adapter persists raw
    failures side by side instead of overwriting them (the attempt counter is
    metadata and therefore excluded from the request hash).
    """
    policy = policy or RetryPolicy()
    attempts: list[LeanResult] = []
    for attempt_index in range(policy.max_attempts):
        attempt_request = replace(
            request,
            metadata={**dict(request.metadata), "attempt": str(attempt_index)},
        )
        result = run(attempt_request)
        attempts.append(result)
        if result.status not in policy.retry_statuses:
            break
    return RetryOutcome(result=attempts[-1], attempts=tuple(attempts))


def run_batch_with_retries(
    run_batch: Callable[[Sequence[LeanRequest]], Sequence[LeanResult]],
    requests: Sequence[LeanRequest],
    policy: RetryPolicy | None = None,
    *,
    before_retry: Callable[[int, tuple[int, ...]], None] | None = None,
) -> BatchRetryOutcome:
    """Run independent requests concurrently while retrying infrastructure failures.

    Each batch attempt contains only the still-retryable requests. Results and
    full attempt lineages are restored to the original input order. A batch
    runner must return exactly one result per submitted request; violating this
    contract fails closed instead of silently misaligning theorem records.
    """

    policy = policy or RetryPolicy()
    if not requests:
        return BatchRetryOutcome(results=(), attempts=())

    histories: list[list[LeanResult]] = [[] for _ in requests]
    pending = list(range(len(requests)))
    for attempt_index in range(policy.max_attempts):
        if attempt_index > 0 and before_retry is not None:
            before_retry(attempt_index, tuple(pending))
        attempt_requests = [
            replace(
                requests[index],
                metadata={**dict(requests[index].metadata), "attempt": str(attempt_index)},
            )
            for index in pending
        ]
        attempt_results = tuple(run_batch(attempt_requests))
        if len(attempt_results) != len(attempt_requests):
            raise ValueError("batch runner returned a different number of results than requests")
        next_pending: list[int] = []
        for index, result in zip(pending, attempt_results, strict=True):
            histories[index].append(result)
            if result.status in policy.retry_statuses:
                next_pending.append(index)
        pending = next_pending
        if not pending:
            break

    lineages = tuple(tuple(history) for history in histories)
    if any(not lineage for lineage in lineages):
        raise AssertionError("every batch request must have at least one result")
    return BatchRetryOutcome(
        results=tuple(lineage[-1] for lineage in lineages),
        attempts=lineages,
    )


def semantic_identity(results: Sequence[LeanResult]) -> tuple[tuple[str, str], ...]:
    """(request_hash, status) projection used by the §8.7 equivalence check:
    one-worker and multiworker runs must agree on it exactly."""
    return tuple((result.request_hash, result.status.value) for result in results)
