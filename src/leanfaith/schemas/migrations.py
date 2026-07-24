"""Explicit persisted-record migrations for Revision 4.1.

New writers serialize schema version 2 and the six terminal semantic
relations.  Version-1 readers migrate the two removed relation spellings;
there is deliberately no permissive alias on :class:`RelationLabel`, because
that would let new records continue to emit legacy values.
"""

from __future__ import annotations

from typing import Final

from leanfaith.schemas.enums import RelationLabel

CURRENT_RECORD_SCHEMA_VERSION: Final = 2
LEGACY_RECORD_SCHEMA_VERSION: Final = 1
LEGACY_INCOMPARABLE: Final = "incomparable_near_miss"
LEGACY_UNKNOWN: Final = "unknown"


def migrate_legacy_relation(value: object) -> tuple[RelationLabel | None, tuple[str, ...]]:
    """Map a version-1 semantic relation and return provenance tags.

    ``unknown`` represented insufficient evidence rather than a terminal
    relation, so it becomes ``None``.  Evidence-level ``unknown`` values use
    their own schemas and are unaffected.
    """

    if value == LEGACY_INCOMPARABLE:
        return RelationLabel.INCOMPARABLE, ("near_miss",)
    if value == LEGACY_UNKNOWN:
        return None, ()
    if value is None:
        return None, ()
    if not isinstance(value, str):
        raise TypeError(f"legacy relation must be a string or null, got {type(value).__name__}")
    return RelationLabel(value), ()
