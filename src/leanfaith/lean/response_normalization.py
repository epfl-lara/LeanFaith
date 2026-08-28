"""Response and exception normalization to the A.5 contract (PLAN.md §8.6, A.6).

Pure functions over already-serialized response dictionaries — this module
never imports LeanInteract (§8.12 item 12); the adapter passes
``response.model_dump(mode="json")`` payloads in.

Pinned 0.11.4 caveat (verified against the fixture REPL): with
``root_goals=True`` the REPL reports root goals through the ``sorries``
channel and flips strict validity even for fully proved code. Real
placeholders are identified by the ``declaration uses `sorry``` diagnostic,
so classification keys on messages first and treats sorries as placeholders
only when root goals were not requested or a sorry diagnostic is present.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus

#: Both diagnostic spellings observed across Lean releases (backtick and
#: single-quote); matching either avoids misclassifying a real admission.
_SORRY_DIAGNOSTICS = ("declaration uses `sorry`", "declaration uses 'sorry'")

#: Exception types raised by the pinned LeanServer.run/run_dict (§8.5, verified
#: in the LF-006 probe environment) and their canonical §8.6 statuses.
#: MemoryError is raised by AutoLeanServer after exhausted memory-triggered
#: restart attempts — a process/recovery failure, hence CRASH (§8.6).
_CRASH_EXCEPTIONS = (
    ConnectionAbortedError,
    ChildProcessError,
    BrokenPipeError,
    EOFError,
    MemoryError,
)

# LeanInteract includes the REPL process stderr in the exception text when the
# child closes unexpectedly.  Lean itself emits this exact message when the
# runtime cannot create another worker thread.  Treat it as a process/resource
# failure even if a LeanInteract version wraps it in a generic exception type.
# This is deliberately a narrow marker: ordinary RuntimeError instances remain
# INTERNAL_ERROR, and no semantic status is inferred from process stderr.
_THREAD_CREATION_FAILURE_MARKER = "failed to create thread"
_THREAD_CREATION_FAILURE_PREFIX = "resource_limit_thread_creation"
_MAX_INFRASTRUCTURE_ERROR_CHARS = 2000


def _messages(raw: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(dict(message) for message in raw.get("messages") or ())


def _sorries(raw: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(dict(entry) for entry in raw.get("sorries") or ())


def _has_error(messages: tuple[dict[str, Any], ...]) -> bool:
    return any(message.get("severity") == "error" for message in messages)


def _has_sorry_diagnostic(messages: tuple[dict[str, Any], ...]) -> bool:
    return any(
        diagnostic in str(message.get("data", ""))
        for message in messages
        if message.get("severity") in ("warning", "error")
        for diagnostic in _SORRY_DIAGNOSTICS
    )


def classify_response(raw: Mapping[str, Any], *, root_goals_requested: bool) -> LeanStatus:
    """Map a CommandResponse dump to the canonical §8.6 status."""
    messages = _messages(raw)
    if _has_error(messages):
        return LeanStatus.INVALID
    if _has_sorry_diagnostic(messages):
        return LeanStatus.VALID_WITH_SORRY
    if _sorries(raw) and not root_goals_requested:
        return LeanStatus.VALID_WITH_SORRY
    return LeanStatus.VALID


def normalize_response(
    request: LeanRequest,
    raw: Mapping[str, Any],
    *,
    request_hash: str,
    context_fingerprint: str,
    elapsed_ms: int,
    raw_response_path: str | None,
) -> LeanResult:
    """Build the terminal LeanResult for a successfully returned response."""
    messages = _messages(raw)
    sorries = _sorries(raw)
    status = classify_response(raw, root_goals_requested=request.root_goals)

    root_goals: tuple[str, ...] = ()
    if request.root_goals:
        # 0.11.4 reports root goals through the sorries channel (see module
        # doc). Entries are admissions only when a sorry diagnostic exists —
        # regardless of overall status — so INVALID responses with clean
        # proofs never gain phantom admissions.
        root_goals = tuple(
            str(entry.get("goal", "")) for entry in sorries if entry.get("goal") is not None
        )
        if not _has_sorry_diagnostic(messages):
            sorries = ()

    declarations = tuple(dict(entry) for entry in raw.get("declarations") or ())
    infotree_payload = raw.get("infotree")
    if isinstance(infotree_payload, list):
        infotree = tuple(
            entry if isinstance(entry, dict) else {"node": entry} for entry in infotree_payload
        )
    elif infotree_payload is None:
        infotree = ()
    else:
        infotree = ({"node": infotree_payload},)

    return LeanResult(
        request_id=request.request_id,
        request_hash=request_hash,
        context_id=request.context_id,
        context_fingerprint=context_fingerprint,
        status=status,
        messages=messages,
        sorries=sorries,
        declarations=declarations,
        root_goals=root_goals,
        infotree=infotree,
        elapsed_ms=elapsed_ms,
        raw_response_path=raw_response_path,
    )


def normalize_repl_error(
    request: LeanRequest,
    message: str,
    *,
    request_hash: str,
    context_fingerprint: str,
    elapsed_ms: int,
    raw_response_path: str | None,
) -> LeanResult:
    """A REPL-level ``LeanError`` ({"message": ...}) is an adapter-visible
    infrastructure failure, never a semantic verdict (§8.6, A.6)."""
    return LeanResult(
        request_id=request.request_id,
        request_hash=request_hash,
        context_id=request.context_id,
        context_fingerprint=context_fingerprint,
        status=LeanStatus.INTERNAL_ERROR,
        elapsed_ms=elapsed_ms,
        raw_response_path=raw_response_path,
        infrastructure_error=f"LeanError: {message}",
    )


def status_for_exception(exc: BaseException) -> LeanStatus:
    """Canonical §8.6/A.6 status for a per-request exception."""
    if isinstance(exc, TimeoutError):
        return LeanStatus.TIMEOUT
    if isinstance(exc, _CRASH_EXCEPTIONS) or _THREAD_CREATION_FAILURE_MARKER in str(exc).lower():
        return LeanStatus.CRASH
    return LeanStatus.INTERNAL_ERROR


def _bounded_infrastructure_error(exc: BaseException) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    if _THREAD_CREATION_FAILURE_MARKER in detail.lower():
        detail = f"{_THREAD_CREATION_FAILURE_PREFIX}: {detail}"
    return detail[:_MAX_INFRASTRUCTURE_ERROR_CHARS]


def normalize_exception(
    request: LeanRequest,
    exc: BaseException,
    *,
    request_hash: str,
    context_fingerprint: str,
    elapsed_ms: int,
    raw_response_path: str | None,
) -> LeanResult:
    """Build the terminal LeanResult for an exception, preserving a safe digest."""
    return LeanResult(
        request_id=request.request_id,
        request_hash=request_hash,
        context_id=request.context_id,
        context_fingerprint=context_fingerprint,
        status=status_for_exception(exc),
        elapsed_ms=elapsed_ms,
        raw_response_path=raw_response_path,
        infrastructure_error=_bounded_infrastructure_error(exc),
    )
