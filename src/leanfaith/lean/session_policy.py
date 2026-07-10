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


def semantic_identity(results: Sequence[LeanResult]) -> tuple[tuple[str, str], ...]:
    """(request_hash, status) projection used by the §8.7 equivalence check:
    one-worker and multiworker runs must agree on it exactly."""
    return tuple((result.request_hash, result.status.value) for result in results)
