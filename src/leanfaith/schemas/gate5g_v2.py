"""Truthful schemas for the post-exhaustion LF-021 Gate-5G path.

Revision 2 is deliberately separate from :mod:`leanfaith.schemas.gate5g`.
It binds the distinct post-exhaustion stop, population, entropy, and frame
schemas without pretending that they are the original v2/v3 artifacts.
"""

from __future__ import annotations

import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.schemas.gate5g import (
    Gate5GArtifactBinding,
    Gate5GObservationBinding,
    Gate5GReFormApplicability,
    Gate5GScopeLimitations,
    Gate5GStratumAccounting,
)

_HEX64 = r"^[0-9a-f]{64}$"
_AUTHORIZATION_ID = r"^lf021_reviewed_extension_collection_authorization_v1:[0-9a-f]{64}$"
_DECISION_ID = r"^lf021_extended_frame_freeze_decision_v1:[0-9a-f]{64}$"
_FRAME_ID = r"^lf021_extended_prevalence_frame_v1:[0-9a-f]{64}$"
_LINEAGE_ID = r"^lf021_gate5g_lineage:[0-9a-f]{64}$"
_VALIDATION_ID = r"^lf021_extended_gate5g_validation_v2:[0-9a-f]{64}$"
_GATE_REPORT_ID = r"^lf021_extended_gate5g_report_v2:[0-9a-f]{64}$"


class ExtendedGate5GAuthorizationBindingV2(StrictModel):
    """Exact reviewed authorization and the decision it authorized."""

    authorization_id: str = Field(pattern=_AUTHORIZATION_ID)
    authorization: Gate5GArtifactBinding
    extension_decision_id: str = Field(min_length=1)
    extension_decision: Gate5GArtifactBinding
    authorized_tranche_id: str = Field(min_length=1)
    authorized_tranche_order: int = Field(ge=12, le=15)


class ExtendedGate5GLineageBindingsV2(StrictModel):
    """All original and post-exhaustion stopping/authorization lineage."""

    activation_v2_decision_id: str = Field(min_length=1)
    activation_v2_decision: Gate5GArtifactBinding
    extension_stop_decision_id: str = Field(min_length=1)
    extension_stop_decision: Gate5GArtifactBinding
    extension_policy: Gate5GArtifactBinding
    extension_implementation: Gate5GArtifactBinding
    collection_authorization_policy: Gate5GArtifactBinding
    collection_authorization_implementation: Gate5GArtifactBinding
    original_observation_count: Literal[12]
    extension_observation_count: int = Field(ge=1, le=4)
    observations: tuple[Gate5GObservationBinding, ...] = Field(
        min_length=13,
        max_length=16,
    )
    authorizations: tuple[ExtendedGate5GAuthorizationBindingV2, ...] = Field(
        min_length=1,
        max_length=4,
    )
    lineage_manifest_id: str = Field(pattern=_LINEAGE_ID)
    lineage_manifest: Gate5GArtifactBinding

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (
            len(self.observations)
            != self.original_observation_count + self.extension_observation_count
            or len(self.authorizations) != self.extension_observation_count
        ):
            raise ValueError("extended Gate-5G lineage lengths differ")
        authorization_ids = tuple(item.authorization_id for item in self.authorizations)
        authorization_orders = tuple(item.authorized_tranche_order for item in self.authorizations)
        if len(set(authorization_ids)) != len(authorization_ids):
            raise ValueError("extended Gate-5G authorization IDs are not unique")
        if authorization_orders != tuple(range(12, 12 + self.extension_observation_count)):
            raise ValueError("extended Gate-5G authorization orders are not contiguous")
        return self


class ExtendedGate5GInputBindingsV2(StrictModel):
    """Every immutable input consumed by the v2 finalizer."""

    policy: Gate5GArtifactBinding
    implementation: Gate5GArtifactBinding
    prevalence_design_v3: Gate5GArtifactBinding
    prevalence_design_v2: Gate5GArtifactBinding
    prevalence_design_v1: Gate5GArtifactBinding
    prevalence_design_v3_implementation: Gate5GArtifactBinding
    frame_freeze_decision: Gate5GArtifactBinding
    frame_materializer_policy: Gate5GArtifactBinding
    frame_materializer_implementation: Gate5GArtifactBinding
    lineage: ExtendedGate5GLineageBindingsV2
    population_manifest: Gate5GArtifactBinding
    population_artifact: Gate5GArtifactBinding
    frame: Gate5GArtifactBinding
    sampling_seed_provenance: Gate5GArtifactBinding
    sampling_seed: Gate5GArtifactBinding
    sampling_seed_lock: Gate5GArtifactBinding
    external_beacon_provenance: Gate5GArtifactBinding | None
    coverage_report: Gate5GArtifactBinding
    phase_milestone: Gate5GArtifactBinding


class ExtendedGate5GValidationReportV2(StrictModel):
    """Content-addressed label-blind validation; it does not close Gate 5G."""

    schema_version: Literal[2] = 2
    validation_id: str = Field(pattern=_VALIDATION_ID)
    validation_status: Literal["ready_to_finalize"]
    input_bindings: ExtendedGate5GInputBindingsV2
    prevalence_design_policy_id: Literal["lf021_prevalence_design_v3"]
    frame_freeze_decision_id: str = Field(pattern=_DECISION_ID)
    frame_id: str = Field(pattern=_FRAME_ID)
    frame_item_count: Literal[240]
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    sampling_seed_source: Literal[
        "external_randomness_beacon_256",
        "os_csprng_secrets_token_bytes_256",
    ]
    original_observation_count: Literal[12]
    extension_observation_count: int = Field(ge=1, le=4)
    observed_tranche_count: int = Field(ge=13, le=16)
    scalable_family_ids: tuple[str, str, str]
    pool_ids: tuple[str, ...] = Field(min_length=1)
    source_proxies: tuple[str, ...] = Field(min_length=1)
    family_item_counts: dict[str, int] = Field(min_length=3, max_length=3)
    pool_item_counts: dict[str, int] = Field(min_length=1)
    source_proxy_item_counts: dict[str, int] = Field(min_length=1)
    strata: tuple[Gate5GStratumAccounting, ...] = Field(min_length=1)
    scope_limitations: Gate5GScopeLimitations
    reform_applicability: Gate5GReFormApplicability
    completed_checks: dict[str, Literal[True]]
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    remote_provider_content_used: Literal[False] = False
    gate_5g_closed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (
            self.observed_tranche_count
            != self.original_observation_count + self.extension_observation_count
        ):
            raise ValueError("extended Gate-5G observation counts differ")
        for field_name in (
            "family_item_counts",
            "pool_item_counts",
            "source_proxy_item_counts",
        ):
            values = getattr(self, field_name)
            if list(values) != sorted(values) or any(value <= 0 for value in values.values()):
                raise ValueError(f"{field_name} must have sorted keys and positive counts")
        if set(self.family_item_counts) != set(self.scalable_family_ids):
            raise ValueError("extended Gate-5G family counts differ from scope")
        if sum(item.sample_size for item in self.strata) != self.frame_item_count:
            raise ValueError("extended Gate-5G stratum samples differ from frame size")
        expected = "lf021_extended_gate5g_validation_v2:" + hash_canonical(
            {
                "schema": "lf021_extended_gate5g_validation_v2",
                **self.model_dump(mode="json", exclude={"validation_id"}),
            }
        )
        if self.validation_id != expected:
            raise ValueError("extended Gate-5G validation ID differs from content")
        return self


class ExtendedGate5GReportV2(StrictModel):
    """Canonical Gate-5G pass for the distinct post-exhaustion lineage."""

    schema_version: Literal[2] = 2
    report_id: str = Field(pattern=_GATE_REPORT_ID)
    gate: Literal["gate_5g"] = "gate_5g"
    decision: Literal["pass"] = "pass"
    lineage_kind: Literal["post_exhaustion_extended_frame_v1"]
    finalized_date: datetime.date
    validation_report: Gate5GArtifactBinding
    validation_id: str = Field(pattern=_VALIDATION_ID)
    prevalence_design_policy_id: Literal["lf021_prevalence_design_v3"]
    frame_freeze_decision_id: str = Field(pattern=_DECISION_ID)
    frame_id: str = Field(pattern=_FRAME_ID)
    frame_item_count: Literal[240]
    scope_limitations: Gate5GScopeLimitations
    completed_checks: dict[str, Literal[True]]
    blocking_checks: tuple[()] = ()
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    remote_provider_content_used: Literal[False] = False
    gate_5g_closed: Literal[True] = True
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _content_id(self) -> Self:
        expected = "lf021_extended_gate5g_report_v2:" + hash_canonical(
            {
                "schema": "lf021_extended_gate5g_report_v2",
                **self.model_dump(mode="json", exclude={"report_id"}),
            }
        )
        if self.report_id != expected:
            raise ValueError("extended Gate-5G report ID differs from content")
        return self


__all__ = [
    "ExtendedGate5GAuthorizationBindingV2",
    "ExtendedGate5GInputBindingsV2",
    "ExtendedGate5GLineageBindingsV2",
    "ExtendedGate5GReportV2",
    "ExtendedGate5GValidationReportV2",
]
