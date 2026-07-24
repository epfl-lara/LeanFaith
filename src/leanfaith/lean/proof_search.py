"""Admission-free definitional and directional proof jobs (LF-020)."""

from __future__ import annotations

from dataclasses import dataclass

from leanfaith.lean.axiom_audit import CertificateAudit, audit_certificate_messages
from leanfaith.lean.commands import (
    Direction,
    PropositionPairSource,
    RenderedEvidenceCommand,
    render_defeq_check,
    render_directional_proof,
)
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanStatus
from leanfaith.lean.session_policy import RetryOutcome, RetryPolicy, run_with_retries

_RETRY_POLICY = RetryPolicy(
    max_attempts=2,
    retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT}),
)


@dataclass(frozen=True, slots=True)
class DefeqCheckResult:
    command: RenderedEvidenceCommand
    retry: RetryOutcome

    @property
    def equal(self) -> bool | None:
        status = self.retry.result.status
        if status == LeanStatus.VALID:
            return True
        if status == LeanStatus.INVALID:
            return False
        return None


@dataclass(frozen=True, slots=True)
class ProofAttemptResult:
    method_id: str
    direction: Direction
    command: RenderedEvidenceCommand
    retry: RetryOutcome
    audit: CertificateAudit | None

    @property
    def proved(self) -> bool:
        return self.retry.result.status == LeanStatus.VALID and bool(
            self.audit is not None and self.audit.accepted
        )

    @property
    def policy_rejected(self) -> bool:
        return self.retry.result.status == LeanStatus.VALID_WITH_SORRY or (
            self.retry.result.status == LeanStatus.VALID
            and bool(self.audit is None or not self.audit.accepted)
        )


def run_defeq_check(
    backend: LeanBackend,
    *,
    source: PropositionPairSource,
    context_id: str,
    timeout_seconds: float,
    request_id: str,
) -> DefeqCheckResult:
    """Run kernel ``rfl`` after a separately successful alias preflight."""

    command = render_defeq_check(source)
    retry = run_with_retries(
        backend.run,
        LeanRequest(
            request_id=request_id,
            context_id=context_id,
            code=command.code,
            allow_sorry=False,
            timeout_seconds=timeout_seconds,
            metadata={"evidence_stage": "defeq"},
        ),
        _RETRY_POLICY,
    )
    return DefeqCheckResult(command=command, retry=retry)


def run_directional_proof_attempt(
    backend: LeanBackend,
    *,
    source: PropositionPairSource,
    context_id: str,
    direction: Direction,
    method_id: str,
    tactic_body: str,
    timeout_seconds: float,
    request_id: str,
    allowed_axioms: tuple[str, ...],
    forbidden_axioms: tuple[str, ...],
) -> ProofAttemptResult:
    """Replay one fixed tactic and audit its certificate dependency closure."""

    command = render_directional_proof(
        source,
        direction=direction,
        tactic_body=tactic_body,
        method_id=method_id,
    )
    retry = run_with_retries(
        backend.run,
        LeanRequest(
            request_id=request_id,
            context_id=context_id,
            code=command.code,
            allow_sorry=False,
            timeout_seconds=timeout_seconds,
            metadata={
                "evidence_stage": "directional_proof",
                "direction": direction,
                "method_id": method_id,
            },
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
    return ProofAttemptResult(
        method_id=method_id,
        direction=direction,
        command=command,
        retry=retry,
        audit=audit,
    )
