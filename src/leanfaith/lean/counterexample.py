"""Kernel-checked bounded separator attempts (LF-020, PLAN.md §16.5)."""

from __future__ import annotations

from dataclasses import dataclass

from leanfaith.lean.axiom_audit import CertificateAudit, audit_certificate_messages
from leanfaith.lean.commands import (
    PropositionPairSource,
    RenderedEvidenceCommand,
    SeparatorDirection,
    render_counterexample_check,
    render_counterexample_preflight,
)
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanStatus
from leanfaith.lean.session_policy import RetryOutcome, RetryPolicy, run_with_retries

_RETRY_POLICY = RetryPolicy(
    max_attempts=2,
    retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT}),
)


@dataclass(frozen=True, slots=True)
class CounterexampleAttempt:
    direction: SeparatorDirection
    preflight_command: RenderedEvidenceCommand
    preflight_retry: RetryOutcome
    command: RenderedEvidenceCommand | None
    retry: RetryOutcome | None
    audit: CertificateAudit | None

    @property
    def supported(self) -> bool:
        return self.preflight_retry.result.status == LeanStatus.VALID

    @property
    def found(self) -> bool:
        return bool(
            self.supported
            and self.retry is not None
            and self.retry.result.status == LeanStatus.VALID
            and self.audit is not None
            and self.audit.accepted
        )

    @property
    def policy_rejected(self) -> bool:
        return bool(
            self.retry is not None
            and (
                self.retry.result.status == LeanStatus.VALID_WITH_SORRY
                or (
                    self.retry.result.status == LeanStatus.VALID
                    and (self.audit is None or not self.audit.accepted)
                )
            )
        )


def run_counterexample_attempt(
    backend: LeanBackend,
    *,
    source: PropositionPairSource,
    context_id: str,
    direction: SeparatorDirection,
    timeout_seconds: float,
    request_id_prefix: str,
    allowed_axioms: tuple[str, ...],
    forbidden_axioms: tuple[str, ...],
) -> CounterexampleAttempt:
    """Check decidability, then prove a separator with kernel ``decide``."""

    preflight_command = render_counterexample_preflight(source, direction=direction)
    preflight_retry = run_with_retries(
        backend.run,
        LeanRequest(
            request_id=f"{request_id_prefix}-preflight",
            context_id=context_id,
            code=preflight_command.code,
            allow_sorry=False,
            timeout_seconds=timeout_seconds,
            metadata={
                "evidence_stage": "counterexample_preflight",
                "direction": direction,
            },
        ),
        _RETRY_POLICY,
    )
    if preflight_retry.result.status != LeanStatus.VALID:
        return CounterexampleAttempt(
            direction=direction,
            preflight_command=preflight_command,
            preflight_retry=preflight_retry,
            command=None,
            retry=None,
            audit=None,
        )

    command = render_counterexample_check(source, direction=direction)
    retry = run_with_retries(
        backend.run,
        LeanRequest(
            request_id=f"{request_id_prefix}-decide",
            context_id=context_id,
            code=command.code,
            allow_sorry=False,
            timeout_seconds=timeout_seconds,
            metadata={"evidence_stage": "counterexample", "direction": direction},
        ),
        _RETRY_POLICY,
    )
    result = retry.result
    audit: CertificateAudit | None = None
    if result.status == LeanStatus.VALID and command.certificate_name is not None:
        audit = audit_certificate_messages(
            certificate_name=command.certificate_name,
            messages=result.messages,
            allowed_axioms=allowed_axioms,
            forbidden_axioms=forbidden_axioms,
            forbidden_constants=source.forbidden_declaration_constants,
            has_sorries=bool(result.sorries),
        )
    return CounterexampleAttempt(
        direction=direction,
        preflight_command=preflight_command,
        preflight_retry=preflight_retry,
        command=command,
        retry=retry,
        audit=audit,
    )
