"""Fail-closed artifact guard for release, selection, calibration, and tables.

Smoke and diagnostic artifacts are never admitted.  Production artifacts
must additionally opt in to the exact requested use with an explicit boolean
field.  Nested smoke artifacts are rejected so a production wrapper cannot
launder an LF-019 result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas import ArtifactClass


class ArtifactUse(StrEnum):
    RELEASE = "release"
    MODEL_SELECTION = "model_selection"
    CALIBRATION = "calibration"
    SCIENTIFIC_TABLE = "scientific_table"


_ELIGIBILITY_FIELDS: dict[ArtifactUse, str] = {
    ArtifactUse.RELEASE: "release_eligible",
    ArtifactUse.MODEL_SELECTION: "model_selection_eligible",
    ArtifactUse.CALIBRATION: "calibration_eligible",
    ArtifactUse.SCIENTIFIC_TABLE: "scientific_table_eligible",
}


class ReleaseGuardDecision(StrictModel):
    """Machine-readable decision for one intended artifact use."""

    use: ArtifactUse
    allowed: bool
    artifact_class: ArtifactClass | None
    eligibility_field: str
    eligibility_value: bool | None
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def _decision_is_coherent(self) -> ReleaseGuardDecision:
        if self.allowed and self.reason_codes:
            raise ValueError("an allowed release-guard decision cannot contain reasons")
        if not self.allowed and not self.reason_codes:
            raise ValueError("a rejected release-guard decision requires a reason")
        return self


class ReleaseGuardError(ValueError):
    """Raised when a guarded artifact is used outside its allowed boundary."""

    def __init__(self, decisions: Sequence[ReleaseGuardDecision]) -> None:
        self.decisions = tuple(decisions)
        details = "; ".join(
            f"{decision.use.value}: {','.join(decision.reason_codes)}"
            for decision in self.decisions
            if not decision.allowed
        )
        super().__init__(f"artifact use rejected by release guard ({details})")


def assess_artifact(artifact: object, *, use: ArtifactUse) -> ReleaseGuardDecision:
    """Assess one artifact without mutating or trusting its Python type."""

    payload = _artifact_mapping(artifact)
    eligibility_field = _ELIGIBILITY_FIELDS[use]
    raw_class = payload.get("artifact_class")
    artifact_class = _parse_artifact_class(raw_class)

    raw_eligibility = payload.get(eligibility_field)
    eligibility_value = raw_eligibility if isinstance(raw_eligibility, bool) else None
    reasons: list[str] = []
    nested_classes = _nested_artifact_classes(payload)
    if ArtifactClass.SMOKE in nested_classes:
        reasons.append("smoke_artifact_forbidden")
    if artifact_class is None:
        reasons.append("artifact_class_missing_or_invalid")
    elif artifact_class != ArtifactClass.PRODUCTION:
        reasons.append("artifact_class_not_production")
    if raw_eligibility is not True:
        reasons.append(
            "explicit_eligibility_required" if raw_eligibility is None else "artifact_not_eligible"
        )
    return ReleaseGuardDecision(
        use=use,
        allowed=not reasons,
        artifact_class=artifact_class,
        eligibility_field=eligibility_field,
        eligibility_value=eligibility_value,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def require_artifact_allowed(artifact: object, *, use: ArtifactUse) -> None:
    """Raise unless one artifact is explicitly production-eligible."""

    decision = assess_artifact(artifact, use=use)
    if not decision.allowed:
        raise ReleaseGuardError((decision,))


def require_artifacts_allowed(artifacts: Sequence[object], *, use: ArtifactUse) -> None:
    """Raise unless a nonempty artifact bundle passes the same guard."""

    if not artifacts:
        raise ValueError("cannot guard an empty artifact bundle")
    decisions = tuple(assess_artifact(artifact, use=use) for artifact in artifacts)
    if any(not decision.allowed for decision in decisions):
        raise ReleaseGuardError(decisions)


def _artifact_mapping(artifact: object) -> Mapping[str, Any]:
    if isinstance(artifact, BaseModel):
        return artifact.model_dump(mode="python")
    if isinstance(artifact, Mapping):
        return artifact
    raise TypeError("guarded artifact must be a Pydantic model or mapping")


def _nested_artifact_classes(value: object) -> frozenset[ArtifactClass]:
    found: set[ArtifactClass] = set()

    def visit(item: object) -> None:
        if isinstance(item, BaseModel):
            visit(item.model_dump(mode="python"))
            return
        if isinstance(item, Mapping):
            raw_class = item.get("artifact_class")
            artifact_class = _parse_artifact_class(raw_class)
            if artifact_class is not None:
                found.add(artifact_class)
            for child in item.values():
                visit(child)
            return
        if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            for child in item:
                visit(child)

    visit(value)
    return frozenset(found)


def _parse_artifact_class(value: object) -> ArtifactClass | None:
    if isinstance(value, ArtifactClass):
        return value
    if not isinstance(value, str):
        return None
    try:
        return ArtifactClass(value)
    except ValueError:
        return None
