"""LF-019 release guard rejects smoke artifacts for every scientific use."""

from __future__ import annotations

import pytest

from leanfaith.models.smoke import train_tiny_smoke_classifier
from leanfaith.release import (
    ArtifactUse,
    ReleaseGuardError,
    assess_artifact,
    require_artifact_allowed,
    require_artifacts_allowed,
)
from leanfaith.schemas import ArtifactClass
from tests.unit.test_tiny_smoke_model import _example


@pytest.mark.parametrize("use", list(ArtifactUse))
def test_guard_rejects_smoke_model_for_every_scientific_use(use: ArtifactUse) -> None:
    model = train_tiny_smoke_classifier((_example(1), _example(2))).model

    decision = assess_artifact(model, use=use)

    assert not decision.allowed
    assert decision.artifact_class == ArtifactClass.SMOKE
    assert "smoke_artifact_forbidden" in decision.reason_codes
    with pytest.raises(ReleaseGuardError, match="smoke_artifact_forbidden"):
        require_artifact_allowed(model, use=use)


def test_guard_rejects_smoke_hidden_inside_production_wrapper() -> None:
    result = train_tiny_smoke_classifier((_example(1), _example(2)))
    wrapper = {
        "artifact_class": "production",
        "release_eligible": True,
        "payload": result.model_dump(mode="python"),
    }

    decision = assess_artifact(wrapper, use=ArtifactUse.RELEASE)

    assert not decision.allowed
    assert decision.artifact_class == ArtifactClass.PRODUCTION
    assert decision.reason_codes == ("smoke_artifact_forbidden",)


@pytest.mark.parametrize(
    ("artifact", "reason"),
    [
        ({"release_eligible": True}, "artifact_class_missing_or_invalid"),
        (
            {"artifact_class": "diagnostic", "release_eligible": True},
            "artifact_class_not_production",
        ),
        (
            {"artifact_class": "production", "release_eligible": False},
            "artifact_not_eligible",
        ),
        (
            {"artifact_class": "production"},
            "explicit_eligibility_required",
        ),
    ],
)
def test_guard_fails_closed_on_missing_or_ineligible_metadata(
    artifact: dict[str, object],
    reason: str,
) -> None:
    decision = assess_artifact(artifact, use=ArtifactUse.RELEASE)
    assert not decision.allowed
    assert reason in decision.reason_codes


@pytest.mark.parametrize(
    ("use", "field"),
    [
        (ArtifactUse.RELEASE, "release_eligible"),
        (ArtifactUse.MODEL_SELECTION, "model_selection_eligible"),
        (ArtifactUse.CALIBRATION, "calibration_eligible"),
        (ArtifactUse.SCIENTIFIC_TABLE, "scientific_table_eligible"),
    ],
)
def test_guard_allows_only_explicit_production_eligibility(
    use: ArtifactUse,
    field: str,
) -> None:
    artifact = {"artifact_class": "production", field: True}
    decision = assess_artifact(artifact, use=use)
    assert decision.allowed
    assert decision.reason_codes == ()
    require_artifact_allowed(artifact, use=use)


def test_bundle_guard_rejects_any_smoke_member_and_empty_bundle() -> None:
    smoke = train_tiny_smoke_classifier((_example(1), _example(2))).metrics
    production = {"artifact_class": "production", "release_eligible": True}

    with pytest.raises(ReleaseGuardError, match="smoke_artifact_forbidden"):
        require_artifacts_allowed((production, smoke), use=ArtifactUse.RELEASE)
    with pytest.raises(ValueError, match="empty"):
        require_artifacts_allowed((), use=ArtifactUse.RELEASE)
