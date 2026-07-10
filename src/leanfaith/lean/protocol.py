"""Canonical LeanFaith backend protocol (PLAN.md Appendix A.5; §8.4 mirrors it).

The class definitions between the CANONICAL BLOCK markers reproduce the
Appendix A.5 code block byte-for-byte; a golden test extracts the block from
PLAN.md and asserts verbatim inclusion here plus §8.4/A.5 parity. Helpers
below the block implement the §8.4 invariants (one payload, request hash).

This module must never import LeanInteract; a repo-wide test enforces that
only ``leanfaith.lean.leaninteract_backend`` does (§8.12 item 12).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from leanfaith.config.hashing import hash_canonical

# --- BEGIN CANONICAL A.5 BLOCK (do not edit without revising PLAN.md) ---


class LeanStatus(StrEnum):
    VALID = "valid"
    VALID_WITH_SORRY = "valid_with_sorry"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    CRASH = "crash"
    SETUP_ERROR = "setup_error"
    UNSUPPORTED = "unsupported"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class LeanRequest:
    request_id: str
    context_id: str
    code: str | None = None
    file_path: Path | None = None
    declarations: bool = False
    root_goals: bool = False
    infotree: Literal["none", "substantive", "full"] = "none"
    allow_sorry: bool = False
    timeout_seconds: float = 30.0
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LeanResult:
    request_id: str
    request_hash: str
    context_id: str
    context_fingerprint: str
    status: LeanStatus
    messages: tuple[dict, ...] = ()
    sorries: tuple[dict, ...] = ()
    declarations: tuple[dict, ...] = ()
    root_goals: tuple[str, ...] = ()
    infotree: tuple[dict, ...] = ()
    elapsed_ms: int = 0
    raw_response_path: str | None = None
    infrastructure_error: str | None = None


class LeanBackend(Protocol):
    def run(self, request: LeanRequest) -> LeanResult: ...
    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]: ...
    def close(self) -> None: ...


# --- END CANONICAL A.5 BLOCK ---


class RequestValidationError(ValueError):
    """Raised when a LeanRequest violates the §8.4 invariants."""


def validate_request(request: LeanRequest) -> None:
    """Enforce the §8.4 invariants: exactly one payload, sane timeout."""
    if (request.code is None) == (request.file_path is None):
        raise RequestValidationError(
            f"request {request.request_id}: exactly one of code/file_path must be set (§8.4)"
        )
    if request.timeout_seconds <= 0:
        raise RequestValidationError(
            f"request {request.request_id}: timeout_seconds must be positive"
        )
    if not request.request_id:
        raise RequestValidationError("request_id must be nonempty")
    if not request.context_id:
        raise RequestValidationError(f"request {request.request_id}: context_id must be nonempty")


def compute_request_hash(
    request: LeanRequest,
    *,
    context_fingerprint: str,
    environment_schema_version: int,
    method_version: str,
) -> str:
    """§8.4 request hash: payload, context, timeout, allow_sorry, InfoTree
    level, method version, and environment schema version.

    ``request_id`` and ``metadata`` are deliberately excluded: they identify
    the submission, not the semantic computation, and must not fragment the
    cache (§16.9 discipline).
    """
    validate_request(request)
    payload = {
        "code": request.code,
        "file_path": str(request.file_path) if request.file_path is not None else None,
        "declarations": request.declarations,
        "root_goals": request.root_goals,
        "infotree": request.infotree,
        "allow_sorry": request.allow_sorry,
        "timeout_seconds": request.timeout_seconds,
        "context_id": request.context_id,
        "context_fingerprint": context_fingerprint,
        "environment_schema_version": environment_schema_version,
        "method_version": method_version,
    }
    return hash_canonical(payload)
