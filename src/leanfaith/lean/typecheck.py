"""Proposition-pair preflight used before interpreting symbolic checks."""

from __future__ import annotations

from dataclasses import dataclass

from leanfaith.lean.commands import (
    PropositionPairSource,
    RenderedEvidenceCommand,
    render_alias_preflight,
)
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import RetryOutcome, RetryPolicy, run_with_retries

_RETRY_POLICY = RetryPolicy(
    max_attempts=2,
    retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT}),
)


@dataclass(frozen=True, slots=True)
class PropositionPreflight:
    command: RenderedEvidenceCommand
    retry: RetryOutcome

    @property
    def valid(self) -> bool:
        return self.retry.result.status == LeanStatus.VALID


def run_proposition_preflight(
    backend: LeanBackend,
    *,
    source: PropositionPairSource,
    context_id: str,
    timeout_seconds: float,
    request_id: str,
) -> PropositionPreflight:
    """Elaborate both proof-free proposition aliases under the exact context."""

    command = render_alias_preflight(source)
    request = LeanRequest(
        request_id=request_id,
        context_id=context_id,
        code=command.code,
        allow_sorry=False,
        timeout_seconds=timeout_seconds,
        metadata={"evidence_stage": "proposition_preflight"},
    )
    retry = run_with_retries(backend.run, request, _RETRY_POLICY)
    return PropositionPreflight(command=command, retry=retry)


def terminal_result(preflight: PropositionPreflight) -> LeanResult:
    """Explicit helper used by callers and tests."""

    return preflight.retry.result
