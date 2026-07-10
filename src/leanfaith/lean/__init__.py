"""Lean boundary (PLAN.md §8).

Only ``leanfaith.lean.leaninteract_backend`` may import LeanInteract; every
higher layer talks to the Appendix A.5 protocol in ``leanfaith.lean.protocol``.
"""

from leanfaith.lean.protocol import (
    LeanBackend,
    LeanRequest,
    LeanResult,
    LeanStatus,
    RequestValidationError,
    compute_request_hash,
    validate_request,
)

__all__ = [
    "LeanBackend",
    "LeanRequest",
    "LeanResult",
    "LeanStatus",
    "RequestValidationError",
    "compute_request_hash",
    "validate_request",
]
