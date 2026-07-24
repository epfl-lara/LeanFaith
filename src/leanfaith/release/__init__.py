"""Fail-closed release and scientific-use guards."""

from leanfaith.release.guard import (
    ArtifactUse,
    ReleaseGuardDecision,
    ReleaseGuardError,
    assess_artifact,
    require_artifact_allowed,
    require_artifacts_allowed,
)

__all__ = [
    "ArtifactUse",
    "ReleaseGuardDecision",
    "ReleaseGuardError",
    "assess_artifact",
    "require_artifact_allowed",
    "require_artifacts_allowed",
]
