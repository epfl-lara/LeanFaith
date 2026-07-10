"""Shared enums (PLAN.md §7.1).

The §11.1 canonical semantic enums land with LF-004; this module also holds
cross-cutting operational enums used by manifests.
"""

from __future__ import annotations

from enum import StrEnum


class ArtifactClass(StrEnum):
    """Run/artifact class (§5.1, §22.7): smoke artifacts are barred from releases."""

    PRODUCTION = "production"
    SMOKE = "smoke"
    DIAGNOSTIC = "diagnostic"


class DataStage(StrEnum):
    """Immutable data lifecycle stages (§10)."""

    RAW = "raw"
    PARSED = "parsed"
    ELABORATED = "elaborated"
    REPRESENTED = "represented"
    GENERATED = "generated"
    VALIDATED = "validated"
    EVIDENCE_COLLECTED = "evidence_collected"
    LABELED = "labeled"
    SPLIT = "split"
    FROZEN = "frozen"
