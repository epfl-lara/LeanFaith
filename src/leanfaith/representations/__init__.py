"""Statement representations and normalization (PLAN.md §13, LF-014)."""

from leanfaith.representations.audit import (
    ManualCollisionReview,
    RepresentationReplayReport,
    audit_representations,
    close_manual_collision_audit,
    compare_representation_replays,
)
from leanfaith.representations.pipeline import (
    RepresentationBatch,
    RepresentationBatchResult,
    RepresentationFailure,
    TheoremForRepresentation,
    alpha_canonical_bytes,
    alpha_identity_fingerprint,
    build_representation_batch,
    build_representations,
    declaration_environment_lookup_name,
    inline_replay_environment_lookup_name,
)
from leanfaith.representations.views import (
    NORMALIZATION_VERSION,
    PP_EXPLICIT_INLINE,
    PP_SIGNATURE_INLINE,
    check_command,
    normalize_headless,
    parse_check_type,
    representation_content_hash,
    signature_near_dup_hash,
)

__all__ = [
    "NORMALIZATION_VERSION",
    "PP_EXPLICIT_INLINE",
    "PP_SIGNATURE_INLINE",
    "ManualCollisionReview",
    "RepresentationBatch",
    "RepresentationBatchResult",
    "RepresentationFailure",
    "RepresentationReplayReport",
    "TheoremForRepresentation",
    "alpha_canonical_bytes",
    "alpha_identity_fingerprint",
    "audit_representations",
    "build_representation_batch",
    "build_representations",
    "check_command",
    "close_manual_collision_audit",
    "compare_representation_replays",
    "declaration_environment_lookup_name",
    "inline_replay_environment_lookup_name",
    "normalize_headless",
    "parse_check_type",
    "representation_content_hash",
    "signature_near_dup_hash",
]
