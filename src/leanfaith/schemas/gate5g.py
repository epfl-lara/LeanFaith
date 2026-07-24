"""Canonical label-blind schemas for LF-021 Gate-5G finalization.

Gate 5G freezes the *sampling frame and its mechanical lineage*.  It does not
resolve faithfulness, admit supervision, or close Gate 5.  The schemas in this
module are intentionally independent of any one collector/postprocessor
version so that a final frame can bind a heterogeneous immutable tranche
prefix without weakening the gate-critical invariants.
"""

from __future__ import annotations

import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel

_HEX64 = r"^[0-9a-f]{64}$"
_VALIDATION_ID = r"^lf021_gate5g_validation:[0-9a-f]{64}$"
_GATE_REPORT_ID = r"^lf021_gate5g_report:[0-9a-f]{64}$"
_LINEAGE_ID = r"^lf021_gate5g_lineage:[0-9a-f]{64}$"
_VALIDATED_ID = r"^lf021_validated_real_outputs:[0-9a-f]{64}$"


class Gate5GArtifactBinding(StrictModel):
    """Repository-local immutable artifact."""

    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class Gate5GObservationBinding(Gate5GArtifactBinding):
    """One postprocess observation consumed by the expansion decision."""

    manifest_id: str = Field(min_length=1)
    tranche_id: str = Field(min_length=1)


class Gate5GReplayCertificateV1(StrictModel):
    """Canonical replay certificate consumed by Gate 5G.

    Independent audit prose may remain version-specific.  Before closure, each
    collection and postprocess run must additionally have this small,
    machine-checkable certificate binding the exact manifest bytes and the
    byte-identical replay result.
    """

    schema_version: Literal[1] = 1
    report_kind: Literal[
        "lf021_collection_replay_certificate_v1",
        "lf021_postprocess_replay_certificate_v1",
    ]
    tranche_id: str = Field(min_length=1)
    manifest: Gate5GArtifactBinding
    replayed: Literal[True]
    byte_identical: Literal[True]
    first_tree_sha256: str = Field(pattern=_HEX64)
    replay_tree_sha256: str = Field(pattern=_HEX64)
    expected_record_count: int = Field(ge=1)
    replay_record_count: int = Field(ge=1)
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.first_tree_sha256 != self.replay_tree_sha256:
            raise ValueError("replay certificate tree hashes differ")
        if self.expected_record_count != self.replay_record_count:
            raise ValueError("replay certificate record counts differ")
        return self


class Gate5GFamilyRevisionBinding(StrictModel):
    """Exact model revision and local session records used by one tranche."""

    family_id: str = Field(min_length=1)
    model_repo_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    session_start: Gate5GArtifactBinding
    session_end: Gate5GArtifactBinding


class Gate5GTrancheBindingV1(StrictModel):
    """Exact collection/postprocess/replay lineage for one frozen tranche."""

    tranche_id: str = Field(min_length=1)
    collection_manifest: Gate5GArtifactBinding
    postprocess_manifest: Gate5GObservationBinding
    collection_replay: Gate5GArtifactBinding
    postprocess_replay: Gate5GArtifactBinding
    family_ids: tuple[str, ...] = Field(min_length=1)
    family_revisions: tuple[Gate5GFamilyRevisionBinding, ...] = Field(min_length=1)
    overlap_manifest: Gate5GArtifactBinding
    pool_ids: tuple[str, ...] = Field(min_length=1)
    source_proxies: tuple[str, ...] = Field(min_length=1)
    expected_invocations: int = Field(ge=1)
    collection_terminal_count: int = Field(ge=1)
    postprocess_terminal_count: int = Field(ge=1)
    benchmark_clear_compiling_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        for field_name in ("family_ids", "pool_ids", "source_proxies"):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        if tuple(item.family_id for item in self.family_revisions) != self.family_ids:
            raise ValueError("family revision bindings differ from tranche families")
        session_artifacts = tuple(
            artifact
            for item in self.family_revisions
            for artifact in (item.session_start.artifact, item.session_end.artifact)
        )
        if len(set(session_artifacts)) != len(session_artifacts):
            raise ValueError("family session artifacts must be unique")
        if (
            self.collection_terminal_count != self.expected_invocations
            or self.postprocess_terminal_count != self.expected_invocations
        ):
            raise ValueError("tranche terminal denominators do not reconcile")
        if self.postprocess_manifest.tranche_id != self.tranche_id:
            raise ValueError("postprocess observation has the wrong tranche")
        return self


class Gate5GLineageManifestV1(StrictModel):
    """Content-addressed complete prefix used to freeze the final frame."""

    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=_LINEAGE_ID)
    tranches: tuple[Gate5GTrancheBindingV1, ...] = Field(min_length=1)
    scalable_family_ids: tuple[str, ...] = Field(min_length=3, max_length=3)
    pool_ids: tuple[str, ...] = Field(min_length=1)
    source_proxies: tuple[str, ...] = Field(min_length=1)
    total_expected_invocations: int = Field(ge=1)
    total_collection_terminals: int = Field(ge=1)
    total_postprocess_terminals: int = Field(ge=1)
    total_benchmark_clear_compiling: int = Field(ge=1)
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        tranche_ids = tuple(item.tranche_id for item in self.tranches)
        if tranche_ids != tuple(dict.fromkeys(tranche_ids)):
            raise ValueError("lineage tranche IDs must be unique and ordered")
        for field_name in ("scalable_family_ids", "pool_ids", "source_proxies"):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        observed_families = tuple(
            sorted({family for item in self.tranches for family in item.family_ids})
        )
        observed_pools = tuple(sorted({pool for item in self.tranches for pool in item.pool_ids}))
        observed_proxies = tuple(
            sorted({proxy for item in self.tranches for proxy in item.source_proxies})
        )
        if observed_families != self.scalable_family_ids:
            raise ValueError("lineage family inventory does not reconcile")
        if observed_pools != self.pool_ids:
            raise ValueError("lineage pool inventory does not reconcile")
        if observed_proxies != self.source_proxies:
            raise ValueError("lineage source-proxy inventory does not reconcile")
        totals = {
            "total_expected_invocations": sum(item.expected_invocations for item in self.tranches),
            "total_collection_terminals": sum(
                item.collection_terminal_count for item in self.tranches
            ),
            "total_postprocess_terminals": sum(
                item.postprocess_terminal_count for item in self.tranches
            ),
            "total_benchmark_clear_compiling": sum(
                item.benchmark_clear_compiling_count for item in self.tranches
            ),
        }
        for field_name, expected in totals.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name} does not reconcile")
        expected_id = "lf021_gate5g_lineage:" + hash_canonical(
            {
                "schema": "lf021_gate5g_lineage_v1",
                **self.model_dump(mode="json", exclude={"manifest_id"}),
            }
        )
        if self.manifest_id != expected_id:
            raise ValueError("lineage manifest ID differs from content")
        return self


class Gate5GScopeLimitations(StrictModel):
    """Mandatory limitation attached to the current three-family frame."""

    scalable_family_ids: tuple[str, str, str]
    three_family_collection_only: Literal[True]
    reduced_data_ablation: Literal[True]
    confirmatory_d4_d5_eligible: Literal[False]
    heldout_generator_claim_eligible: Literal[False]
    supplemental_qualifications_count_for_gate_credit: Literal[False]
    reduced_data_reasons: tuple[
        Literal["confirmatory_d4_d5_unavailable"],
        Literal["heldout_generator_claim_unavailable"],
        Literal["three_family_collection_only"],
    ]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.scalable_family_ids != tuple(sorted(set(self.scalable_family_ids))):
            raise ValueError("scope family IDs must be three sorted unique values")
        expected = (
            "confirmatory_d4_d5_unavailable",
            "heldout_generator_claim_unavailable",
            "three_family_collection_only",
        )
        if self.reduced_data_reasons != expected:
            raise ValueError("noncanonical three-family reduced-data reasons")
        return self


class Gate5GReFormApplicability(StrictModel):
    """Explicit ReForm x Lean-Workbook leakage disposition."""

    applicable: bool
    status: Literal["not_applicable", "checked_clear"]
    reason: str = Field(min_length=1)
    overlap_report: Gate5GArtifactBinding | None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.applicable:
            if self.status != "checked_clear" or self.overlap_report is None:
                raise ValueError("applicable ReForm lineage requires a clear overlap report")
        elif self.status != "not_applicable" or self.overlap_report is not None:
            raise ValueError("non-applicable ReForm lineage must be explicit and report-free")
        return self


class Gate5GStratumAccounting(StrictModel):
    """Population/sample reconciliation for one frozen sampling stratum."""

    stratum: str = Field(min_length=1)
    population_size: int = Field(ge=1)
    sample_size: int = Field(ge=1)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.sample_size > self.population_size:
            raise ValueError("stratum sample exceeds population")
        return self


class ValidatedRealOutputsManifestV1(StrictModel):
    """Final label-blind real-output manifest prepared before Gate 5G."""

    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=_VALIDATED_ID)
    frame_freeze_decision: Gate5GArtifactBinding
    frame_freeze_decision_id: str = Field(pattern=r"^lf021_frame_freeze_decision_v3:[0-9a-f]{64}$")
    frame: Gate5GArtifactBinding
    frame_id: str = Field(pattern=r"^lf021_prevalence_frame_v3:[0-9a-f]{64}$")
    frame_item_count: int = Field(ge=200, le=300)
    lineage_manifest: Gate5GArtifactBinding
    lineage_manifest_id: str = Field(pattern=_LINEAGE_ID)
    coverage_report: Gate5GArtifactBinding
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    sampling_seed_provenance: Gate5GArtifactBinding
    family_item_counts: dict[str, int] = Field(min_length=3, max_length=3)
    pool_item_counts: dict[str, int] = Field(min_length=1)
    source_proxy_item_counts: dict[str, int] = Field(min_length=1)
    strata: tuple[Gate5GStratumAccounting, ...] = Field(min_length=1)
    scope_limitations: Gate5GScopeLimitations
    reform_applicability: Gate5GReFormApplicability
    benchmark_clear_count: int = Field(ge=200, le=300)
    compiling_count: int = Field(ge=200, le=300)
    unresolved_count: int = Field(ge=200, le=300)
    semantic_label_count: Literal[0]
    supervision_eligible_count: Literal[0]
    semantic_labels_created: Literal[False] = False
    gate_5g_closed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (
            self.benchmark_clear_count != self.frame_item_count
            or self.compiling_count != self.frame_item_count
            or self.unresolved_count != self.frame_item_count
        ):
            raise ValueError("validated frame status counts do not reconcile")
        for name in ("family_item_counts", "pool_item_counts", "source_proxy_item_counts"):
            values = getattr(self, name)
            if list(values) != sorted(values) or any(value <= 0 for value in values.values()):
                raise ValueError(f"{name} must have sorted keys and positive counts")
        if set(self.family_item_counts) != set(self.scope_limitations.scalable_family_ids):
            raise ValueError("validated family counts differ from scope")
        if sum(item.sample_size for item in self.strata) != self.frame_item_count:
            raise ValueError("stratum sample sizes do not equal frame size")
        if len({item.stratum for item in self.strata}) != len(self.strata):
            raise ValueError("duplicate validated stratum")
        expected_id = "lf021_validated_real_outputs:" + hash_canonical(
            {
                "schema": "lf021_validated_real_outputs_v1",
                **self.model_dump(mode="json", exclude={"manifest_id"}),
            }
        )
        if self.manifest_id != expected_id:
            raise ValueError("validated manifest ID differs from content")
        return self


class Gate5GInputBindings(StrictModel):
    """All finalized artifacts that a validation report binds."""

    policy: Gate5GArtifactBinding
    implementation: Gate5GArtifactBinding
    prevalence_design_policy: Gate5GArtifactBinding
    base_prevalence_design_policy: Gate5GArtifactBinding
    frame_freeze_decision: Gate5GArtifactBinding
    frame_freeze_policy: Gate5GArtifactBinding
    frame_freeze_implementation: Gate5GArtifactBinding
    v2_stop_decision: Gate5GArtifactBinding
    population_manifest: Gate5GArtifactBinding
    population_artifact: Gate5GArtifactBinding
    frame: Gate5GArtifactBinding
    sampling_seed_provenance: Gate5GArtifactBinding
    sampling_seed: Gate5GArtifactBinding
    sampling_seed_lock: Gate5GArtifactBinding
    external_beacon_provenance: Gate5GArtifactBinding | None
    lineage_manifest: Gate5GArtifactBinding
    validated_manifest: Gate5GArtifactBinding
    coverage_report: Gate5GArtifactBinding
    phase_milestone: Gate5GArtifactBinding


class Gate5GValidationReportV1(StrictModel):
    """Content-addressed ready-to-finalize report; never itself closes a gate."""

    schema_version: Literal[1] = 1
    validation_id: str = Field(pattern=_VALIDATION_ID)
    validation_status: Literal["ready_to_finalize"]
    input_bindings: Gate5GInputBindings
    prevalence_design_policy_id: Literal["lf021_prevalence_design_v2"]
    frame_freeze_decision_id: str = Field(pattern=r"^lf021_frame_freeze_decision_v3:[0-9a-f]{64}$")
    frame_id: str = Field(pattern=r"^lf021_prevalence_frame_v3:[0-9a-f]{64}$")
    frame_item_count: int = Field(ge=200, le=300)
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    observed_tranche_count: int = Field(ge=1)
    scalable_family_ids: tuple[str, str, str]
    pool_ids: tuple[str, ...] = Field(min_length=1)
    source_proxies: tuple[str, ...] = Field(min_length=1)
    scope_limitations: Gate5GScopeLimitations
    reform_applicability: Gate5GReFormApplicability
    completed_checks: dict[str, Literal[True]]
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_closed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _content_id(self) -> Self:
        expected = "lf021_gate5g_validation:" + hash_canonical(
            {
                "schema": "lf021_gate5g_validation_v1",
                **self.model_dump(mode="json", exclude={"validation_id"}),
            }
        )
        if self.validation_id != expected:
            raise ValueError("Gate-5G validation ID differs from content")
        return self


class Gate5GReportV1(StrictModel):
    """Canonical passed Gate-5G report, emitted only in explicit finalize mode."""

    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=_GATE_REPORT_ID)
    gate: Literal["gate_5g"] = "gate_5g"
    decision: Literal["pass"] = "pass"
    finalized_date: datetime.date
    validation_report: Gate5GArtifactBinding
    validation_id: str = Field(pattern=_VALIDATION_ID)
    prevalence_design_policy_id: Literal["lf021_prevalence_design_v2"]
    frame_freeze_decision_id: str = Field(pattern=r"^lf021_frame_freeze_decision_v3:[0-9a-f]{64}$")
    frame_id: str = Field(pattern=r"^lf021_prevalence_frame_v3:[0-9a-f]{64}$")
    frame_item_count: int = Field(ge=200, le=300)
    scope_limitations: Gate5GScopeLimitations
    completed_checks: dict[str, Literal[True]]
    blocking_checks: tuple[()] = ()
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_closed: Literal[True] = True
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _content_id(self) -> Self:
        expected = "lf021_gate5g_report:" + hash_canonical(
            {
                "schema": "lf021_gate5g_report_v1",
                **self.model_dump(mode="json", exclude={"report_id"}),
            }
        )
        if self.report_id != expected:
            raise ValueError("Gate-5G report ID differs from content")
        return self


__all__ = [
    "Gate5GArtifactBinding",
    "Gate5GFamilyRevisionBinding",
    "Gate5GInputBindings",
    "Gate5GLineageManifestV1",
    "Gate5GObservationBinding",
    "Gate5GReFormApplicability",
    "Gate5GReplayCertificateV1",
    "Gate5GReportV1",
    "Gate5GScopeLimitations",
    "Gate5GStratumAccounting",
    "Gate5GTrancheBindingV1",
    "Gate5GValidationReportV1",
    "ValidatedRealOutputsManifestV1",
]
