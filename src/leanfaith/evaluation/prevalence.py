"""Pre-registered LF-021 faithful-prevalence estimation.

The primary sampling unit is one unique ``(problem_group,
alpha_identity_fingerprint)`` claim.  Secondary estimates reuse its human
label for retained alpha-identical invocations of the *same* problem and
weight by the multiplicities preserved in the corrected v2 frame.

This module never creates semantic labels and never changes Gate status.  It
only consumes an immutable frame plus an immutable adjudication projection.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import stat
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import ResolutionOutcome

PREVALENCE_ESTIMATOR_VERSION = "lf021_prevalence_estimator_v2"
PREVALENCE_OUTPUT_ROOT = Path("reports/prevalence")
RANDOMIZED_SAMPLING_METHOD = "problem_aware_stratified_csprng_srs_without_replacement_v2"
_HEX64 = r"^[0-9a-f]{64}$"
_REPORT_ID = r"^lf021_prevalence_report_v2:[0-9a-f]{64}$"
_Z_975 = 1.959963984540054


class PrevalenceInputError(ValueError):
    """Raised when frame or adjudication input violates the frozen design."""


class PrevalenceOutcome(StrEnum):
    """Terminal semantic outcome used by the prevalence projection."""

    FAITHFUL = "faithful"
    UNFAITHFUL = "unfaithful"
    AMBIGUOUS = "ambiguous"


class IntervalStatus(StrEnum):
    """Whether an interval is statistically defined under the frozen method."""

    AVAILABLE = "available"
    UNDEFINED_DENOMINATOR = "undefined_denominator"
    UNSUPPORTED_SINGLETON_NONCERTAINTY_STRATUM = "unsupported_singleton_noncertainty_stratum"


class PointEstimateScope(StrEnum):
    """Population scope represented by the reported respondent point estimate."""

    FULL_POPULATION = "full_population"
    RESPONDENTS_ONLY_DESCRIPTIVE = "respondents_only_descriptive"


class TargetPopulationPolicy(StrictModel):
    frame_schema_version: Literal[2]
    primary_unit: Literal["problem_group_x_alpha_identity"]
    eligible_population: Literal[
        "benchmark_clear_compiling_problem_aware_claims_in_frozen_v2_prefix"
    ]
    label_reuse_scope: Literal["same_problem_group_and_alpha_identity_only"]


class PolicyArtifactBinding(StrictModel):
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class TargetPopulationPolicyV2(StrictModel):
    frame_schema_version: Literal[3]
    primary_unit: Literal["problem_group_x_alpha_identity"]
    eligible_population: Literal[
        "benchmark_clear_compiling_problem_aware_claims_in_verified_v3_population"
    ]
    label_reuse_scope: Literal["same_problem_group_and_alpha_identity_only"]
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_rank_algorithm: Literal["hmac_sha256_keyed_rank_v1"]


class PrimaryEstimandPolicy(StrictModel):
    category_estimand: Literal["finite_population_faithful_unfaithful_ambiguous_claim_shares"]
    headline_scalar: Literal["faithful_among_terminal_nonambiguous_claims"]
    estimator: Literal["stratified_horvitz_thompson_known_population_total"]
    confidence_interval: Literal["exact_hypergeometric_inversion_bonferroni_simultaneous_v1"]
    confidence_level: float = Field(ge=0.0, le=1.0)
    singleton_noncertainty_stratum: Literal["supported_by_exact_inversion"]

    @model_validator(mode="after")
    def _confidence_is_frozen(self) -> Self:
        if self.confidence_level != 0.95:
            raise ValueError("primary confidence level is frozen at 0.95")
        return self


class SecondaryEstimandPolicy(StrictModel):
    estimands: tuple[
        Literal["retained_invocation_weighted", "per_generator_family_invocation_weighted"],
        Literal["retained_invocation_weighted", "per_generator_family_invocation_weighted"],
    ]
    estimator: Literal["stratified_hajek_ratio_using_retained_multiplicity"]
    confidence_interval: Literal["stratified_taylor_linearization_normal_v1"]
    confidence_level: float = Field(ge=0.0, le=1.0)
    singleton_noncertainty_stratum: Literal["point_estimate_only_interval_unsupported_fail_closed"]
    interval_role: Literal["descriptive_pointwise_not_primary_confirmatory"]

    @model_validator(mode="after")
    def _frozen_values(self) -> Self:
        if self.confidence_level != 0.95:
            raise ValueError("secondary confidence level is frozen at 0.95")
        if self.estimands != (
            "retained_invocation_weighted",
            "per_generator_family_invocation_weighted",
        ):
            raise ValueError("secondary estimands must use the frozen order and values")
        return self


class AmbiguityPolicy(StrictModel):
    primary: Literal["exclude_terminal_ambiguous_from_binary_denominator"]
    required_three_way_report: Literal[True] = True
    sensitivity: Literal["treat_terminal_ambiguous_as_not_faithful"]


class NonresponsePolicy(StrictModel):
    unresolved_or_missing: Literal["nonresponse_not_terminal_ambiguity"]
    point_estimate: Literal["respondent_hajek_descriptive"]
    bounds: Literal["worst_case_all_unfaithful_vs_all_faithful"]
    every_frame_item_attempted: Literal[True] = True


class SourceProxyPolicy(StrictModel):
    canonical_label: Literal["operational_source_path_proxy"]
    interpretation: Literal["coverage_metadata_not_adjudicated_semantic_domain"]
    forbidden_claim: Literal["semantic_domain_prevalence"]


class ThreeFamilyScopePolicy(StrictModel):
    required_scalable_families: tuple[str, str, str]
    three_family_collection_only: Literal[True] = True
    confirmatory_d4_d5_eligible: Literal[False] = False
    heldout_generator_claim_eligible: Literal[False] = False
    supplemental_qualifications_count_for_gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _families_are_unique(self) -> Self:
        if self.required_scalable_families != tuple(sorted(set(self.required_scalable_families))):
            raise ValueError("required scalable families must be sorted and unique")
        return self


class PrevalenceDesignPolicyV1(StrictModel):
    """Frozen, pre-label LF-021 prevalence design."""

    schema_version: Literal[1] = 1
    policy_id: Literal["lf021_prevalence_design_v1"]
    status: Literal["frozen_prelabel"]
    target_population: TargetPopulationPolicy
    primary: PrimaryEstimandPolicy
    secondary: SecondaryEstimandPolicy
    ambiguity: AmbiguityPolicy
    nonresponse: NonresponsePolicy
    source_proxy: SourceProxyPolicy
    scope: ThreeFamilyScopePolicy
    semantic_labels_inspected_when_frozen: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_5g_closed: Literal[False] = False
    gate_5_closed: Literal[False] = False


class PrevalenceDesignPolicyV2(StrictModel):
    """Frame-schema-3 amendment that preserves every v1 estimand rule."""

    schema_version: Literal[2] = 2
    policy_id: Literal["lf021_prevalence_design_v2"]
    status: Literal["frozen_prelabel"]
    base_v1_design: PolicyArtifactBinding
    target_population: TargetPopulationPolicyV2
    primary: PrimaryEstimandPolicy
    secondary: SecondaryEstimandPolicy
    ambiguity: AmbiguityPolicy
    nonresponse: NonresponsePolicy
    source_proxy: SourceProxyPolicy
    scope: ThreeFamilyScopePolicy
    semantic_labels_inspected_when_frozen: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_5g_closed: Literal[False] = False
    gate_5_closed: Literal[False] = False


class PrevalenceFrameUnitV2(StrictModel):
    """Minimal, strict estimator projection of a corrected v2 frame item."""

    frame_record_id: str = Field(min_length=1)
    problem_group: str = Field(min_length=1)
    alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    sampling_stratum: str = Field(min_length=1)
    stratum_population_size: int = Field(ge=1)
    stratum_sample_size: int = Field(ge=1)
    inclusion_probability_numerator: int = Field(ge=1)
    inclusion_probability_denominator: int = Field(ge=1)
    member_count: int = Field(ge=1)
    member_count_by_family: dict[str, int] = Field(min_length=1)
    member_count_by_source_proxy: dict[str, int] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.stratum_sample_size > self.stratum_population_size:
            raise ValueError("stratum sample size exceeds population size")
        if (
            self.inclusion_probability_numerator != self.stratum_sample_size
            or self.inclusion_probability_denominator != self.stratum_population_size
        ):
            raise ValueError("inclusion probability must be exactly n_h/N_h")
        for name, counts in (
            ("member_count_by_family", self.member_count_by_family),
            ("member_count_by_source_proxy", self.member_count_by_source_proxy),
        ):
            if any(not key or value <= 0 for key, value in counts.items()):
                raise ValueError(f"{name} requires nonempty keys and positive sparse counts")
            if sum(counts.values()) != self.member_count:
                raise ValueError(f"{name} must sum to member_count")
        return self


class AdjudicationProjectionV1(StrictModel):
    """One immutable terminal/unresolved adjudication projected onto a frame item."""

    schema_version: Literal[1] = 1
    adjudication_id: str = Field(min_length=1)
    frame_record_id: str = Field(min_length=1)
    resolution_outcome: ResolutionOutcome
    terminal: bool

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected_terminal = self.resolution_outcome is not ResolutionOutcome.UNRESOLVED
        if self.terminal != expected_terminal:
            raise ValueError("terminal must be false exactly for resolution_outcome=unresolved")
        return self

    @property
    def prevalence_outcome(self) -> PrevalenceOutcome | None:
        mapping = {
            ResolutionOutcome.SAME_CLAIM: PrevalenceOutcome.FAITHFUL,
            ResolutionOutcome.NOT_SAME_CLAIM: PrevalenceOutcome.UNFAITHFUL,
            ResolutionOutcome.AMBIGUOUS: PrevalenceOutcome.AMBIGUOUS,
            ResolutionOutcome.UNRESOLVED: None,
        }
        return mapping[self.resolution_outcome]


class EstimateInterval(StrictModel):
    status: IntervalStatus
    lower: float | None = Field(default=None, ge=0.0, le=1.0)
    upper: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_level: float = Field(ge=0.0, le=1.0)
    method: str = Field(min_length=1)
    reason: str | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.confidence_level != 0.95:
            raise ValueError("interval confidence level is frozen at 0.95")
        if self.status is IntervalStatus.AVAILABLE:
            if self.lower is None or self.upper is None or self.lower > self.upper:
                raise ValueError("available interval requires ordered finite bounds")
        elif self.lower is not None or self.upper is not None:
            raise ValueError("unsupported/undefined interval cannot carry bounds")
        return self


class ScalarPrevalenceEstimate(StrictModel):
    point_estimate: float | None = Field(default=None, ge=0.0, le=1.0)
    interval: EstimateInterval


class NonresponseBounds(StrictModel):
    faithful_nonambiguous_lower: float | None = Field(default=None, ge=0.0, le=1.0)
    faithful_nonambiguous_upper: float | None = Field(default=None, ge=0.0, le=1.0)
    ambiguous_as_unfaithful_lower: float | None = Field(default=None, ge=0.0, le=1.0)
    ambiguous_as_unfaithful_upper: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        for prefix in ("faithful_nonambiguous", "ambiguous_as_unfaithful"):
            lower = getattr(self, f"{prefix}_lower")
            upper = getattr(self, f"{prefix}_upper")
            if (lower is None) != (upper is None):
                raise ValueError(f"{prefix} bounds must both be set or both null")
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"{prefix} bounds are reversed")
        return self


class EstimandResult(StrictModel):
    unit: str = Field(min_length=1)
    estimator: str = Field(min_length=1)
    point_estimate_scope: PointEstimateScope
    faithful: ScalarPrevalenceEstimate
    unfaithful: ScalarPrevalenceEstimate
    ambiguous: ScalarPrevalenceEstimate
    faithful_nonambiguous: ScalarPrevalenceEstimate
    ambiguous_as_unfaithful: ScalarPrevalenceEstimate
    nonresponse_weight_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    nonresponse_bounds: NonresponseBounds
    sampled_item_count: int = Field(ge=0)
    terminal_item_count: int = Field(ge=0)
    weighted_population_total: float = Field(ge=0.0)
    weighted_terminal_total: float = Field(ge=0.0)


class PrevalenceInputBinding(StrictModel):
    frame_schema_version: Literal[3]
    frame_freeze_decision_id: str = Field(pattern=r"^lf021_frame_freeze_decision_v3:[0-9a-f]{64}$")
    frame_freeze_decision_sha256: str = Field(pattern=_HEX64)
    frame_id: str = Field(pattern=r"^lf021_prevalence_frame_v3:[0-9a-f]{64}$")
    frame_artifact: str = Field(min_length=1)
    frame_sha256: str = Field(pattern=_HEX64)
    frame_item_count: int = Field(ge=1)
    population_id: str = Field(pattern=r"^lf021_eligible_population_v3:[0-9a-f]{64}$")
    population_manifest_sha256: str = Field(pattern=_HEX64)
    population_artifact_sha256: str = Field(pattern=_HEX64)
    sampling_method: str = Field(min_length=1)
    sampling_rank_algorithm: Literal["hmac_sha256_keyed_rank_v1"]
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    sampling_seed_provenance_sha256: str = Field(pattern=_HEX64)
    test_replay_only: Literal[False] = False
    adjudication_projection_sha256: str = Field(pattern=_HEX64)
    adjudication_record_count: int = Field(ge=0)
    policy_sha256: str = Field(pattern=_HEX64)


class AdjudicationProjectionAccounting(StrictModel):
    frame_item_count: int = Field(ge=1)
    projection_record_count: int = Field(ge=0)
    terminal_record_count: int = Field(ge=0)
    explicit_unresolved_record_count: int = Field(ge=0)
    missing_record_count: Literal[0] = 0

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.projection_record_count != self.frame_item_count:
            raise ValueError("adjudication projection must contain one row per frame item")
        if (
            self.terminal_record_count + self.explicit_unresolved_record_count
            != self.projection_record_count
        ):
            raise ValueError("adjudication terminal/unresolved counts do not reconcile")
        return self


class PrevalenceScopeLimitations(StrictModel):
    scalable_family_ids: tuple[str, ...]
    three_family_collection_only: Literal[True]
    confirmatory_d4_d5_eligible: Literal[False]
    heldout_generator_claim_eligible: Literal[False]
    supplemental_qualifications_count_for_gate_credit: Literal[False]


class PrevalenceReportV2(StrictModel):
    """Deterministic prevalence report schema.

    A real instance is created only after the corrected frame and adjudication
    projection exist.  Unit tests exercise the schema with synthetic records.
    """

    schema_version: Literal[2] = 2
    report_id: str = Field(pattern=_REPORT_ID)
    estimator_version: Literal["lf021_prevalence_estimator_v2"]
    design_policy_id: Literal["lf021_prevalence_design_v2"]
    input_binding: PrevalenceInputBinding
    adjudication_accounting: AdjudicationProjectionAccounting
    primary_problem_claim: EstimandResult
    secondary_retained_invocation: EstimandResult
    per_family_retained_invocation: dict[str, EstimandResult]
    sampled_source_proxy_invocation_counts: dict[str, int]
    source_proxy_interpretation: Literal[
        "operational coverage metadata; not an adjudicated semantic domain"
    ]
    scope_limitations: PrevalenceScopeLimitations
    labels_created_by_estimator: Literal[False] = False
    supervision_eligibility_changed: Literal[False] = False
    gate_5g_closed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _content_id(self) -> Self:
        expected = "lf021_prevalence_report_v2:" + hash_canonical(
            {
                "schema": "lf021_prevalence_report_v2",
                **self.model_dump(mode="json", exclude={"report_id"}),
            }
        )
        if self.report_id != expected:
            raise ValueError("report_id differs from report content")
        return self


@dataclass(frozen=True, slots=True)
class _JoinedUnit:
    frame: PrevalenceFrameUnitV2
    outcome: PrevalenceOutcome | None

    @property
    def design_weight(self) -> float:
        return self.frame.stratum_population_size / self.frame.stratum_sample_size


@dataclass(frozen=True, slots=True)
class VerifiedPrevalenceFrameBinding:
    frame_freeze_decision_id: str
    frame_freeze_decision_sha256: str
    frame_id: str
    frame_artifact: str
    frame_sha256: str
    frame_item_count: int
    population_id: str
    population_manifest_sha256: str
    population_artifact_sha256: str
    sampling_method: str
    sampling_rank_algorithm: str
    sampling_seed_sha256: str
    sampling_seed_provenance_sha256: str
    test_replay_only: bool


@dataclass(frozen=True, slots=True)
class _JoinedProjection:
    rows: tuple[_JoinedUnit, ...]
    accounting: AdjudicationProjectionAccounting


def load_prevalence_design_policy_v1(
    path: Path,
) -> LoadedConfig[PrevalenceDesignPolicyV1]:
    return load_config(path, PrevalenceDesignPolicyV1)


def load_prevalence_design_policy(
    path: Path,
) -> LoadedConfig[PrevalenceDesignPolicyV2]:
    return load_config(path, PrevalenceDesignPolicyV2)


def verify_prevalence_design_policy_v2(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PrevalenceDesignPolicyV2],
) -> None:
    """Verify that v2 changes only the randomized frame contract."""

    base_path = _bound_repository_artifact(
        repo_root=repo_root,
        artifact=loaded_policy.config.base_v1_design.artifact,
        description="base prevalence design v1",
    )
    try:
        base_bytes = base_path.read_bytes()
    except OSError as exc:
        raise PrevalenceInputError("base prevalence design v1 is unavailable") from exc
    if sha256_hex(base_bytes) != loaded_policy.config.base_v1_design.sha256:
        raise PrevalenceInputError("base prevalence design v1 differs from its binding")
    base = load_prevalence_design_policy_v1(base_path).config
    amended = loaded_policy.config
    for field_name in (
        "primary",
        "secondary",
        "ambiguity",
        "nonresponse",
        "source_proxy",
        "scope",
        "semantic_labels_inspected_when_frozen",
        "semantic_labels_created",
        "gate_5g_closed",
        "gate_5_closed",
    ):
        if getattr(amended, field_name) != getattr(base, field_name):
            raise PrevalenceInputError(f"prevalence design v2 changes frozen v1 field {field_name}")
    if (
        amended.target_population.primary_unit != base.target_population.primary_unit
        or amended.target_population.label_reuse_scope != base.target_population.label_reuse_scope
        or amended.target_population.sampling_method != RANDOMIZED_SAMPLING_METHOD
    ):
        raise PrevalenceInputError(
            "prevalence design v2 changes more than the randomized frame contract"
        )


def _strict_json_object(text: str, *, location: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PrevalenceInputError(f"duplicate JSON key {key!r} at {location}")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                PrevalenceInputError(f"non-finite JSON constant {token!r} at {location}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise PrevalenceInputError(f"invalid JSON at {location}: {exc}") from exc
    if not isinstance(value, dict):
        raise PrevalenceInputError(f"expected a JSON object at {location}")
    return value


def _decode_utf8(payload: bytes, *, location: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrevalenceInputError(f"input is not UTF-8 at {location}: {exc}") from exc


def _validate_full_v2_frame_item(full: Any) -> None:
    expected_cluster = "candidate_cluster_v2:" + hash_canonical(
        {
            "schema": "lf021_problem_group_alpha_cluster_v2",
            "problem_group": full.problem_group,
            "alpha_identity_fingerprint": full.alpha_identity_fingerprint,
        }
    )
    if full.cluster_id != expected_cluster:
        raise PrevalenceInputError("frame cluster_id differs from problem-group/alpha identity")
    if full.representative_invocation_id not in full.contributing_invocation_ids:
        raise PrevalenceInputError("representative invocation is absent from contributors")
    if full.representative_problem_record_id not in full.contributing_problem_record_ids:
        raise PrevalenceInputError("representative problem is absent from contributors")
    if set(full.member_count_by_family) != set(full.contributing_family_ids):
        raise PrevalenceInputError("family multiplicity keys differ from contributing families")
    if set(full.member_count_by_source_proxy) != set(full.contributing_source_proxies):
        raise PrevalenceInputError(
            "source-proxy multiplicity keys differ from contributing source proxies"
        )
    if full.representative_family_id not in full.member_count_by_family:
        raise PrevalenceInputError("representative family is absent from family multiplicities")
    if full.representative_pool_id not in full.contributing_pool_ids:
        raise PrevalenceInputError("representative pool is absent from contributing pools")
    if full.representative_source_proxy not in full.member_count_by_source_proxy:
        raise PrevalenceInputError(
            "representative source proxy is absent from source multiplicities"
        )
    expected_stratum = (
        f"{full.representative_family_id}|{full.representative_pool_id}|"
        f"{full.representative_source_proxy}"
    )
    if full.sampling_stratum != expected_stratum:
        raise PrevalenceInputError("sampling stratum differs from representative lineage")


def load_v2_frame_projection_bytes(
    payload: bytes,
    *,
    location: str,
) -> tuple[PrevalenceFrameUnitV2, ...]:
    """Validate full v2 frame rows, then project only estimator-required fields."""

    try:
        module = import_module("leanfaith.generation.tranche_expansion_v2")
        frame_item_type: Any = module.FrameItemV2
    except (ImportError, AttributeError) as exc:  # pragma: no cover - pre-v2 only.
        raise PrevalenceInputError("corrected tranche-expansion v2 module is unavailable") from exc

    rows: list[PrevalenceFrameUnitV2] = []
    for line_number, text in enumerate(
        _decode_utf8(payload, location=location).splitlines(),
        start=1,
    ):
        if not text.strip():
            continue
        raw = _strict_json_object(text, location=f"{location}:{line_number}")
        full = frame_item_type.model_validate(raw)
        _validate_full_v2_frame_item(full)
        rows.append(
            PrevalenceFrameUnitV2(
                frame_record_id=full.frame_record_id,
                problem_group=full.problem_group,
                alpha_identity_fingerprint=full.alpha_identity_fingerprint,
                sampling_stratum=full.sampling_stratum,
                stratum_population_size=full.stratum_population_size,
                stratum_sample_size=full.stratum_sample_size,
                inclusion_probability_numerator=full.inclusion_probability_numerator,
                inclusion_probability_denominator=full.inclusion_probability_denominator,
                member_count=full.member_count,
                member_count_by_family=full.member_count_by_family,
                member_count_by_source_proxy=full.member_count_by_source_proxy,
            )
        )
    return tuple(rows)


def load_v2_frame_projection(path: Path) -> tuple[PrevalenceFrameUnitV2, ...]:
    """Historical reader only; fixed-salt v2 frames are never inferred from."""

    return load_v2_frame_projection_bytes(path.read_bytes(), location=str(path))


def project_verified_frame_freeze_v3(
    verified: Any,
) -> tuple[tuple[PrevalenceFrameUnitV2, ...], VerifiedPrevalenceFrameBinding]:
    """Project only after the canonical v3 verifier has replayed the sample."""

    decision = verified.decision
    if decision.test_replay_only or verified.seed_provenance.test_replay_only:
        raise PrevalenceInputError(
            "test/replay-only randomized frames are ineligible for prevalence reporting"
        )
    if decision.sampling_method != RANDOMIZED_SAMPLING_METHOD:
        raise PrevalenceInputError("v3 frame sampling method differs from the design")
    frame = tuple(
        PrevalenceFrameUnitV2(
            frame_record_id=item.frame_record_id,
            problem_group=item.problem_group,
            alpha_identity_fingerprint=item.alpha_identity_fingerprint,
            sampling_stratum=item.sampling_stratum,
            stratum_population_size=item.stratum_population_size,
            stratum_sample_size=item.stratum_sample_size,
            inclusion_probability_numerator=item.inclusion_probability_numerator,
            inclusion_probability_denominator=item.inclusion_probability_denominator,
            member_count=item.member_count,
            member_count_by_family=item.member_count_by_family,
            member_count_by_source_proxy=item.member_count_by_source_proxy,
        )
        for item in verified.frame_items
    )
    if len(frame) != decision.frame.item_count:
        raise PrevalenceInputError("verified v3 frame count differs from its decision")
    return frame, VerifiedPrevalenceFrameBinding(
        frame_freeze_decision_id=decision.decision_id,
        frame_freeze_decision_sha256=verified.decision_binding.sha256,
        frame_id=decision.frame.frame_id,
        frame_artifact=decision.frame.artifact,
        frame_sha256=decision.frame.sha256,
        frame_item_count=decision.frame.item_count,
        population_id=decision.population_id,
        population_manifest_sha256=decision.population_manifest.sha256,
        population_artifact_sha256=decision.population_artifact.sha256,
        sampling_method=decision.sampling_method,
        sampling_rank_algorithm=decision.sampling_rank_algorithm,
        sampling_seed_sha256=decision.sampling_seed_sha256,
        sampling_seed_provenance_sha256=(decision.sampling_seed_provenance.sha256),
        test_replay_only=False,
    )


def load_adjudication_projection_bytes(
    payload: bytes,
    *,
    location: str,
) -> tuple[AdjudicationProjectionV1, ...]:
    rows: list[AdjudicationProjectionV1] = []
    for line_number, text in enumerate(
        _decode_utf8(payload, location=location).splitlines(),
        start=1,
    ):
        if not text.strip():
            continue
        raw = _strict_json_object(text, location=f"{location}:{line_number}")
        rows.append(AdjudicationProjectionV1.model_validate(raw))
    return tuple(rows)


def load_adjudication_projection(path: Path) -> tuple[AdjudicationProjectionV1, ...]:
    return load_adjudication_projection_bytes(path.read_bytes(), location=str(path))


def _bound_frame_path(
    *,
    decision_path: Path,
    frame_artifact: str,
) -> Path:
    """Resolve a frame artifact inside its expansion-output root."""

    artifact = Path(frame_artifact)
    if artifact.is_absolute():
        raise PrevalenceInputError("frame artifact must be relative to the decision root")
    output_root = decision_path.resolve().parent.parent
    resolved = (output_root / artifact).resolve()
    if not resolved.is_relative_to(output_root):
        raise PrevalenceInputError("frame artifact escapes the expansion-decision root")
    return resolved


def _bound_repository_artifact(
    *,
    repo_root: Path,
    artifact: str,
    description: str,
) -> Path:
    path = Path(artifact)
    if path.is_absolute():
        raise PrevalenceInputError(f"{description} artifact must be repository-relative")
    root = repo_root.resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise PrevalenceInputError(f"{description} artifact escapes the repository")
    return resolved


def _verify_seed_provenance(
    *,
    repo_root: Path,
    provenance: Any,
    expected_seed_sha256: str,
) -> str:
    """Verify the bytes that precommitted the sampling seed."""

    artifact = getattr(provenance, "artifact", None)
    expected_sha256 = getattr(provenance, "sha256", None)
    if not isinstance(artifact, str) or not isinstance(expected_sha256, str):
        raise PrevalenceInputError(
            "randomized frame lacks an ArtifactBinding sampling_seed_provenance"
        )
    provenance_path = _bound_repository_artifact(
        repo_root=repo_root,
        artifact=artifact,
        description="sampling-seed provenance",
    )
    try:
        provenance_bytes = provenance_path.read_bytes()
    except OSError as exc:
        raise PrevalenceInputError(f"sampling-seed provenance is unavailable: {artifact}") from exc
    if sha256_hex(provenance_bytes) != expected_sha256:
        raise PrevalenceInputError("sampling-seed provenance differs from its binding")
    raw = _strict_json_object(
        _decode_utf8(provenance_bytes, location=str(provenance_path)),
        location=str(provenance_path),
    )
    if raw.get("sampling_seed_sha256") != expected_seed_sha256:
        raise PrevalenceInputError("sampling-seed provenance does not bind the frame sampling seed")
    return expected_sha256


def _verify_repository_binding(
    *,
    repo_root: Path,
    binding: Any,
    description: str,
) -> None:
    artifact = getattr(binding, "artifact", None)
    expected_sha256 = getattr(binding, "sha256", None)
    if not isinstance(artifact, str) or not isinstance(expected_sha256, str):
        raise PrevalenceInputError(f"{description} is not an ArtifactBinding")
    path = _bound_repository_artifact(
        repo_root=repo_root,
        artifact=artifact,
        description=description,
    )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise PrevalenceInputError(f"{description} is unavailable: {artifact}") from exc
    if sha256_hex(payload) != expected_sha256:
        raise PrevalenceInputError(f"{description} differs from its decision binding")


def load_verified_v2_frame_projection_bytes(
    *,
    repo_root: Path,
    expansion_decision_path: Path,
    expansion_decision_bytes: bytes,
    frame_path: Path,
    frame_bytes: bytes,
) -> tuple[tuple[PrevalenceFrameUnitV2, ...], VerifiedPrevalenceFrameBinding]:
    """Verify a frame against its immutable expansion decision.

    Revision-2 decisions currently use fixed-salt hash ranking.  Those files
    are still fully checked against their declared artifact, SHA, and count,
    but then rejected because they are not randomized probability samples.
    A prospective decision schema may pass only with the exact CSPRNG method
    and a content-bound seed-provenance artifact.
    """

    try:
        module = import_module("leanfaith.generation.tranche_expansion_v2")
        decision_type: Any = module.ExpansionDecisionV2
    except (ImportError, AttributeError) as exc:  # pragma: no cover - pre-v2 only.
        raise PrevalenceInputError("tranche-expansion v2 module is unavailable") from exc

    raw = _strict_json_object(
        _decode_utf8(
            expansion_decision_bytes,
            location=str(expansion_decision_path),
        ),
        location=str(expansion_decision_path),
    )
    decision = decision_type.model_validate(raw)
    for binding, description in (
        (decision.policy_artifact, "expansion policy"),
        (decision.base_v1_policy, "base expansion policy"),
        (decision.base_v1_implementation, "base expansion implementation"),
        (decision.implementation_artifact, "expansion implementation"),
    ):
        _verify_repository_binding(
            repo_root=repo_root,
            binding=binding,
            description=description,
        )
    for observation in decision.observations:
        _verify_repository_binding(
            repo_root=repo_root,
            binding=observation.postprocess_manifest,
            description=f"observation {observation.tranche_id}",
        )
    if str(decision.action) not in {
        "freeze_preferred_frame",
        "freeze_reduced_frame",
    }:
        raise PrevalenceInputError("expansion decision is not a frame-freeze action")
    if decision.frame is None:
        raise PrevalenceInputError("frame-freeze decision has no frame binding")

    expected_path = _bound_frame_path(
        decision_path=expansion_decision_path,
        frame_artifact=decision.frame.artifact,
    )
    if frame_path.resolve() != expected_path:
        raise PrevalenceInputError(
            "supplied frame path differs from the expansion decision artifact"
        )
    frame_sha256 = sha256_hex(frame_bytes)
    if frame_sha256 != decision.frame.sha256:
        raise PrevalenceInputError("supplied frame bytes differ from the frame binding")
    frame = load_v2_frame_projection_bytes(frame_bytes, location=str(frame_path))
    if len(frame) != decision.frame.item_count:
        raise PrevalenceInputError("frame item count differs from the expansion decision binding")

    raise PrevalenceInputError(
        "fixed-salt hash ranking is not a randomized probability-sampling "
        "design; only a verified frame-freeze v3 decision is eligible"
    )


def _validate_and_join(
    frame: Sequence[PrevalenceFrameUnitV2],
    adjudications: Sequence[AdjudicationProjectionV1],
    policy: PrevalenceDesignPolicyV2,
) -> _JoinedProjection:
    if not frame:
        raise PrevalenceInputError("prevalence frame is empty")
    by_id: dict[str, PrevalenceFrameUnitV2] = {}
    unique_claims: set[tuple[str, str]] = set()
    strata: dict[str, list[PrevalenceFrameUnitV2]] = defaultdict(list)
    observed_families: set[str] = set()
    for item in frame:
        if item.frame_record_id in by_id:
            raise PrevalenceInputError(f"duplicate frame_record_id {item.frame_record_id}")
        claim_key = (item.problem_group, item.alpha_identity_fingerprint)
        if claim_key in unique_claims:
            raise PrevalenceInputError(
                "duplicate problem-group plus alpha-identity primary sampling unit"
            )
        by_id[item.frame_record_id] = item
        unique_claims.add(claim_key)
        strata[item.sampling_stratum].append(item)
        observed_families.update(item.member_count_by_family)

    for stratum, items in sorted(strata.items()):
        first = items[0]
        expected = (
            first.stratum_population_size,
            first.stratum_sample_size,
            first.inclusion_probability_numerator,
            first.inclusion_probability_denominator,
        )
        if any(
            (
                item.stratum_population_size,
                item.stratum_sample_size,
                item.inclusion_probability_numerator,
                item.inclusion_probability_denominator,
            )
            != expected
            for item in items
        ):
            raise PrevalenceInputError(f"inconsistent design metadata in stratum {stratum!r}")
        if len(items) != first.stratum_sample_size:
            raise PrevalenceInputError(
                f"stratum {stratum!r} has {len(items)} rows, expected n_h="
                f"{first.stratum_sample_size}"
            )

    required_families = set(policy.scope.required_scalable_families)
    if observed_families != required_families:
        raise PrevalenceInputError(
            "frame family set differs from the frozen three-family scope: "
            f"observed={sorted(observed_families)!r}, required={sorted(required_families)!r}"
        )

    label_by_frame: dict[str, AdjudicationProjectionV1] = {}
    adjudication_ids: set[str] = set()
    for record in adjudications:
        if record.adjudication_id in adjudication_ids:
            raise PrevalenceInputError(f"duplicate adjudication_id {record.adjudication_id}")
        adjudication_ids.add(record.adjudication_id)
        if record.frame_record_id not in by_id:
            raise PrevalenceInputError(
                f"adjudication targets unknown frame item {record.frame_record_id}"
            )
        if record.frame_record_id in label_by_frame:
            raise PrevalenceInputError(
                f"duplicate adjudication for frame item {record.frame_record_id}"
            )
        label_by_frame[record.frame_record_id] = record

    missing_ids = sorted(set(by_id) - set(label_by_frame))
    terminal_count = sum(record.terminal for record in adjudications)
    explicit_unresolved_count = sum(not record.terminal for record in adjudications)
    if missing_ids:
        raise PrevalenceInputError(
            "incomplete adjudication projection: "
            f"frame_items={len(frame)} projection_records={len(adjudications)} "
            f"missing={len(missing_ids)} explicit_unresolved="
            f"{explicit_unresolved_count} terminal={terminal_count}"
        )
    accounting = AdjudicationProjectionAccounting(
        frame_item_count=len(frame),
        projection_record_count=len(adjudications),
        terminal_record_count=terminal_count,
        explicit_unresolved_record_count=explicit_unresolved_count,
        missing_record_count=0,
    )
    return _JoinedProjection(
        rows=tuple(
            _JoinedUnit(
                frame=item,
                outcome=label_by_frame[item.frame_record_id].prevalence_outcome,
            )
            for item in sorted(frame, key=lambda row: row.frame_record_id)
        ),
        accounting=accounting,
    )


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0.0:
        return None
    return min(1.0, max(0.0, numerator / denominator))


def _interval(
    *,
    lower: float | None,
    upper: float | None,
    method: str,
    status: IntervalStatus = IntervalStatus.AVAILABLE,
    reason: str | None = None,
) -> EstimateInterval:
    return EstimateInterval(
        status=status,
        lower=lower,
        upper=upper,
        confidence_level=0.95,
        method=method,
        reason=reason,
    )


def _log_comb(n: int, k: int) -> float:
    if k < 0 or k > n:
        return -math.inf
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def _hypergeom_probability(*, population: int, successes: int, draws: int, observed: int) -> float:
    lower = max(0, draws - (population - successes))
    upper = min(draws, successes)
    if observed < lower or observed > upper:
        return 0.0
    log_probability = (
        _log_comb(successes, observed)
        + _log_comb(population - successes, draws - observed)
        - _log_comb(population, draws)
    )
    return math.exp(log_probability)


def _hypergeom_total_interval(
    *,
    population: int,
    draws: int,
    observed: int,
    alpha: float,
) -> tuple[int, int]:
    """Invert equal-tailed finite-population hypergeometric tests."""

    if draws == population:
        return observed, observed
    accepted: list[int] = []
    tail = alpha / 2.0
    for successes in range(population + 1):
        lower_x = max(0, draws - (population - successes))
        upper_x = min(draws, successes)
        if not (lower_x <= observed <= upper_x):
            continue
        cdf = math.fsum(
            _hypergeom_probability(
                population=population,
                successes=successes,
                draws=draws,
                observed=value,
            )
            for value in range(lower_x, observed + 1)
        )
        survival = math.fsum(
            _hypergeom_probability(
                population=population,
                successes=successes,
                draws=draws,
                observed=value,
            )
            for value in range(observed, upper_x + 1)
        )
        if cdf + 1e-15 >= tail and survival + 1e-15 >= tail:
            accepted.append(successes)
    if not accepted:
        raise PrevalenceInputError("exact hypergeometric inversion produced an empty set")
    return min(accepted), max(accepted)


def _weighted_status_totals(
    rows: Sequence[_JoinedUnit],
    multiplicity: Callable[[PrevalenceFrameUnitV2], int],
) -> dict[PrevalenceOutcome | None, float]:
    result: dict[PrevalenceOutcome | None, float] = {
        PrevalenceOutcome.FAITHFUL: 0.0,
        PrevalenceOutcome.UNFAITHFUL: 0.0,
        PrevalenceOutcome.AMBIGUOUS: 0.0,
        None: 0.0,
    }
    buckets: dict[PrevalenceOutcome | None, list[float]] = defaultdict(list)
    for row in rows:
        buckets[row.outcome].append(row.design_weight * multiplicity(row.frame))
    for outcome in result:
        result[outcome] = math.fsum(buckets[outcome])
    return result


def _nonresponse_bounds(
    totals: Mapping[PrevalenceOutcome | None, float],
) -> NonresponseBounds:
    faithful = totals[PrevalenceOutcome.FAITHFUL]
    unfaithful = totals[PrevalenceOutcome.UNFAITHFUL]
    nonresponse = totals[None]
    binary_denominator = faithful + unfaithful + nonresponse
    total = math.fsum(totals.values())
    return NonresponseBounds(
        faithful_nonambiguous_lower=_safe_ratio(faithful, binary_denominator),
        faithful_nonambiguous_upper=_safe_ratio(faithful + nonresponse, binary_denominator),
        ambiguous_as_unfaithful_lower=_safe_ratio(faithful, total),
        ambiguous_as_unfaithful_upper=_safe_ratio(faithful + nonresponse, total),
    )


def _ratio_point(
    rows: Sequence[_JoinedUnit],
    multiplicity: Callable[[PrevalenceFrameUnitV2], int],
    *,
    numerator: Callable[[PrevalenceOutcome | None], bool],
    denominator: Callable[[PrevalenceOutcome | None], bool],
) -> tuple[float | None, float, float]:
    numerator_values: list[float] = []
    denominator_values: list[float] = []
    for row in rows:
        weighted = row.design_weight * multiplicity(row.frame)
        if numerator(row.outcome):
            numerator_values.append(weighted)
        if denominator(row.outcome):
            denominator_values.append(weighted)
    numerator_total = math.fsum(numerator_values)
    denominator_total = math.fsum(denominator_values)
    return _safe_ratio(numerator_total, denominator_total), numerator_total, denominator_total


def _taylor_ratio_interval(
    rows: Sequence[_JoinedUnit],
    multiplicity: Callable[[PrevalenceFrameUnitV2], int],
    *,
    numerator: Callable[[PrevalenceOutcome | None], bool],
    denominator: Callable[[PrevalenceOutcome | None], bool],
) -> tuple[float | None, EstimateInterval]:
    point, _numerator_total, denominator_total = _ratio_point(
        rows,
        multiplicity,
        numerator=numerator,
        denominator=denominator,
    )
    method = "stratified_taylor_linearization_normal_v1"
    if point is None:
        return None, _interval(
            lower=None,
            upper=None,
            method=method,
            status=IntervalStatus.UNDEFINED_DENOMINATOR,
            reason="weighted denominator is zero",
        )

    strata: dict[str, list[_JoinedUnit]] = defaultdict(list)
    for row in rows:
        strata[row.frame.sampling_stratum].append(row)
    variance_terms: list[float] = []
    singleton_strata: list[str] = []
    for stratum, members in sorted(strata.items()):
        first = members[0].frame
        population = first.stratum_population_size
        sample = first.stratum_sample_size
        if sample == population:
            continue
        if sample < 2:
            singleton_strata.append(stratum)
            continue
        residuals = []
        for row in members:
            member_multiplier = multiplicity(row.frame)
            z_value = member_multiplier * float(numerator(row.outcome))
            x_value = member_multiplier * float(denominator(row.outcome))
            residuals.append(z_value - point * x_value)
        mean = math.fsum(residuals) / sample
        sample_variance = math.fsum((value - mean) ** 2 for value in residuals) / (sample - 1)
        sampling_fraction = sample / population
        variance_terms.append(population**2 * (1.0 - sampling_fraction) * sample_variance / sample)
    if singleton_strata:
        return point, _interval(
            lower=None,
            upper=None,
            method=method,
            status=IntervalStatus.UNSUPPORTED_SINGLETON_NONCERTAINTY_STRATUM,
            reason="noncertainty singleton strata: " + ",".join(singleton_strata),
        )
    standard_error = math.sqrt(max(0.0, math.fsum(variance_terms))) / denominator_total
    return point, _interval(
        lower=max(0.0, point - _Z_975 * standard_error),
        upper=min(1.0, point + _Z_975 * standard_error),
        method=method,
    )


def _primary_exact_intervals(
    rows: Sequence[_JoinedUnit],
) -> tuple[
    dict[PrevalenceOutcome, tuple[float, float]],
    tuple[float, float] | None,
    tuple[float, float],
]:
    strata: dict[str, list[_JoinedUnit]] = defaultdict(list)
    for row in rows:
        strata[row.frame.sampling_stratum].append(row)
    # Two missingness assignments (failure/success) for each of three semantic
    # categories in every stratum are covered simultaneously.
    alpha_per_inversion = 0.05 / (6 * len(strata))
    total_population = sum(items[0].frame.stratum_population_size for items in strata.values())
    bounds_by_outcome: dict[PrevalenceOutcome, tuple[int, int]] = {}
    for outcome in PrevalenceOutcome:
        lower_totals: list[int] = []
        upper_totals: list[int] = []
        for items in strata.values():
            population = items[0].frame.stratum_population_size
            sample = items[0].frame.stratum_sample_size
            observed = sum(row.outcome is outcome for row in items)
            nonresponse = sum(row.outcome is None for row in items)
            lower, _ = _hypergeom_total_interval(
                population=population,
                draws=sample,
                observed=observed,
                alpha=alpha_per_inversion,
            )
            _, upper = _hypergeom_total_interval(
                population=population,
                draws=sample,
                observed=observed + nonresponse,
                alpha=alpha_per_inversion,
            )
            lower_totals.append(lower)
            upper_totals.append(upper)
        bounds_by_outcome[outcome] = (sum(lower_totals), sum(upper_totals))

    category_bounds = {
        outcome: (lower / total_population, upper / total_population)
        for outcome, (lower, upper) in bounds_by_outcome.items()
    }
    faithful_lower, faithful_upper = bounds_by_outcome[PrevalenceOutcome.FAITHFUL]
    unfaithful_lower, unfaithful_upper = bounds_by_outcome[PrevalenceOutcome.UNFAITHFUL]
    if faithful_upper + unfaithful_upper == 0:
        faithful_nonambiguous = None
    else:
        binary_lower = _safe_ratio(faithful_lower, faithful_lower + unfaithful_upper)
        binary_upper = _safe_ratio(faithful_upper, faithful_upper + unfaithful_lower)
        faithful_nonambiguous = (
            0.0 if binary_lower is None else binary_lower,
            1.0 if binary_upper is None else binary_upper,
        )
    ambiguous_as_unfaithful = (
        faithful_lower / total_population,
        faithful_upper / total_population,
    )
    return category_bounds, faithful_nonambiguous, ambiguous_as_unfaithful


def _primary_result(rows: Sequence[_JoinedUnit]) -> EstimandResult:
    def multiplicity(_item: PrevalenceFrameUnitV2) -> int:
        return 1

    totals = _weighted_status_totals(rows, multiplicity)
    terminal_total = (
        totals[PrevalenceOutcome.FAITHFUL]
        + totals[PrevalenceOutcome.UNFAITHFUL]
        + totals[PrevalenceOutcome.AMBIGUOUS]
    )
    total = math.fsum(totals.values())
    category_bounds, faithful_binary_bounds, sensitivity_bounds = _primary_exact_intervals(rows)
    method = "exact_hypergeometric_inversion_bonferroni_simultaneous_v1"

    def category(outcome: PrevalenceOutcome) -> ScalarPrevalenceEstimate:
        lower, upper = category_bounds[outcome]
        return ScalarPrevalenceEstimate(
            point_estimate=_safe_ratio(totals[outcome], terminal_total),
            interval=_interval(lower=lower, upper=upper, method=method),
        )

    faithful_nonambiguous = _safe_ratio(
        totals[PrevalenceOutcome.FAITHFUL],
        totals[PrevalenceOutcome.FAITHFUL] + totals[PrevalenceOutcome.UNFAITHFUL],
    )
    ambiguous_as_unfaithful = _safe_ratio(totals[PrevalenceOutcome.FAITHFUL], terminal_total)
    if faithful_binary_bounds is None:
        faithful_nonambiguous_interval = _interval(
            lower=None,
            upper=None,
            method=method,
            status=IntervalStatus.UNDEFINED_DENOMINATOR,
            reason="exact bounds establish zero faithful-plus-unfaithful population",
        )
    else:
        faithful_nonambiguous_interval = _interval(
            lower=faithful_binary_bounds[0],
            upper=faithful_binary_bounds[1],
            method=method,
        )
    return EstimandResult(
        unit="unique_problem_group_plus_alpha_identity_claim",
        estimator=(
            "respondent_hajek_descriptive_with_stratified_ht_category_bounds"
            if totals[None] > 0.0
            else "stratified_horvitz_thompson_known_population_total"
        ),
        point_estimate_scope=(
            PointEstimateScope.RESPONDENTS_ONLY_DESCRIPTIVE
            if totals[None] > 0.0
            else PointEstimateScope.FULL_POPULATION
        ),
        faithful=category(PrevalenceOutcome.FAITHFUL),
        unfaithful=category(PrevalenceOutcome.UNFAITHFUL),
        ambiguous=category(PrevalenceOutcome.AMBIGUOUS),
        faithful_nonambiguous=ScalarPrevalenceEstimate(
            point_estimate=faithful_nonambiguous,
            interval=faithful_nonambiguous_interval,
        ),
        ambiguous_as_unfaithful=ScalarPrevalenceEstimate(
            point_estimate=ambiguous_as_unfaithful,
            interval=_interval(
                lower=sensitivity_bounds[0],
                upper=sensitivity_bounds[1],
                method=method,
            ),
        ),
        nonresponse_weight_fraction=_safe_ratio(totals[None], total),
        nonresponse_bounds=_nonresponse_bounds(totals),
        sampled_item_count=len(rows),
        terminal_item_count=sum(row.outcome is not None for row in rows),
        weighted_population_total=total,
        weighted_terminal_total=terminal_total,
    )


def _secondary_result(
    rows: Sequence[_JoinedUnit],
    *,
    unit: str,
    multiplicity: Callable[[PrevalenceFrameUnitV2], int],
) -> EstimandResult:
    def terminal(value: PrevalenceOutcome | None) -> bool:
        return value is not None

    def nonambiguous(value: PrevalenceOutcome | None) -> bool:
        return value in {
            PrevalenceOutcome.FAITHFUL,
            PrevalenceOutcome.UNFAITHFUL,
        }

    def is_faithful(value: PrevalenceOutcome | None) -> bool:
        return value is PrevalenceOutcome.FAITHFUL

    totals = _weighted_status_totals(rows, multiplicity)
    total = math.fsum(totals.values())
    terminal_total = total - totals[None]

    def ratio(
        numerator: Callable[[PrevalenceOutcome | None], bool],
        denominator: Callable[[PrevalenceOutcome | None], bool],
    ) -> ScalarPrevalenceEstimate:
        point, interval = _taylor_ratio_interval(
            rows,
            multiplicity,
            numerator=numerator,
            denominator=denominator,
        )
        return ScalarPrevalenceEstimate(point_estimate=point, interval=interval)

    return EstimandResult(
        unit=unit,
        estimator="stratified_hajek_ratio_using_retained_multiplicity",
        point_estimate_scope=(
            PointEstimateScope.RESPONDENTS_ONLY_DESCRIPTIVE
            if totals[None] > 0.0
            else PointEstimateScope.FULL_POPULATION
        ),
        faithful=ratio(
            is_faithful,
            terminal,
        ),
        unfaithful=ratio(
            lambda value: value is PrevalenceOutcome.UNFAITHFUL,
            terminal,
        ),
        ambiguous=ratio(
            lambda value: value is PrevalenceOutcome.AMBIGUOUS,
            terminal,
        ),
        faithful_nonambiguous=ratio(is_faithful, nonambiguous),
        ambiguous_as_unfaithful=ratio(is_faithful, terminal),
        nonresponse_weight_fraction=_safe_ratio(totals[None], total),
        nonresponse_bounds=_nonresponse_bounds(totals),
        sampled_item_count=len(rows),
        terminal_item_count=sum(row.outcome is not None for row in rows),
        weighted_population_total=total,
        weighted_terminal_total=terminal_total,
    )


def estimate_prevalence(
    *,
    frame: Sequence[PrevalenceFrameUnitV2],
    adjudications: Sequence[AdjudicationProjectionV1],
    loaded_policy: LoadedConfig[PrevalenceDesignPolicyV2],
    verified_frame_binding: VerifiedPrevalenceFrameBinding,
    adjudication_projection_sha256: str,
) -> PrevalenceReportV2:
    """Compute the frozen primary and secondary estimands deterministically."""

    policy = loaded_policy.config
    if verified_frame_binding.test_replay_only:
        raise PrevalenceInputError(
            "test/replay-only frame cannot produce a scientific prevalence report"
        )
    if (
        verified_frame_binding.sampling_method != policy.target_population.sampling_method
        or verified_frame_binding.sampling_rank_algorithm
        != policy.target_population.sampling_rank_algorithm
    ):
        raise PrevalenceInputError(
            "verified frame sampling contract differs from prevalence design v2"
        )
    if len(frame) != verified_frame_binding.frame_item_count:
        raise PrevalenceInputError("estimator frame count differs from verified frame binding")
    joined = _validate_and_join(frame, adjudications, policy)
    rows = joined.rows
    primary = _primary_result(rows)

    def total_members(item: PrevalenceFrameUnitV2) -> int:
        return item.member_count

    secondary = _secondary_result(
        rows,
        unit="retained_compiling_invocation",
        multiplicity=total_members,
    )

    def family_members(
        family_id: str,
    ) -> Callable[[PrevalenceFrameUnitV2], int]:
        def multiplicity(item: PrevalenceFrameUnitV2) -> int:
            return item.member_count_by_family.get(family_id, 0)

        return multiplicity

    per_family: dict[str, EstimandResult] = {}
    for family in policy.scope.required_scalable_families:
        per_family[family] = _secondary_result(
            rows,
            unit=f"retained_compiling_invocation_for_family:{family}",
            multiplicity=family_members(family),
        )
    source_proxy_counts: dict[str, int] = defaultdict(int)
    for item in frame:
        for source_proxy, count in item.member_count_by_source_proxy.items():
            source_proxy_counts[source_proxy] += count

    payload: dict[str, Any] = {
        "schema_version": 2,
        "estimator_version": PREVALENCE_ESTIMATOR_VERSION,
        "design_policy_id": policy.policy_id,
        "input_binding": {
            "frame_schema_version": policy.target_population.frame_schema_version,
            "frame_freeze_decision_id": (verified_frame_binding.frame_freeze_decision_id),
            "frame_freeze_decision_sha256": (verified_frame_binding.frame_freeze_decision_sha256),
            "frame_id": verified_frame_binding.frame_id,
            "frame_artifact": verified_frame_binding.frame_artifact,
            "frame_sha256": verified_frame_binding.frame_sha256,
            "frame_item_count": len(frame),
            "population_id": verified_frame_binding.population_id,
            "population_manifest_sha256": (verified_frame_binding.population_manifest_sha256),
            "population_artifact_sha256": (verified_frame_binding.population_artifact_sha256),
            "sampling_method": verified_frame_binding.sampling_method,
            "sampling_rank_algorithm": (verified_frame_binding.sampling_rank_algorithm),
            "sampling_seed_sha256": verified_frame_binding.sampling_seed_sha256,
            "sampling_seed_provenance_sha256": (
                verified_frame_binding.sampling_seed_provenance_sha256
            ),
            "test_replay_only": False,
            "adjudication_projection_sha256": adjudication_projection_sha256,
            "adjudication_record_count": len(adjudications),
            "policy_sha256": loaded_policy.config_hash,
        },
        "adjudication_accounting": joined.accounting.model_dump(mode="json"),
        "primary_problem_claim": primary.model_dump(mode="json"),
        "secondary_retained_invocation": secondary.model_dump(mode="json"),
        "per_family_retained_invocation": {
            family: result.model_dump(mode="json") for family, result in per_family.items()
        },
        "sampled_source_proxy_invocation_counts": dict(sorted(source_proxy_counts.items())),
        "source_proxy_interpretation": (
            "operational coverage metadata; not an adjudicated semantic domain"
        ),
        "scope_limitations": {
            "scalable_family_ids": policy.scope.required_scalable_families,
            "three_family_collection_only": True,
            "confirmatory_d4_d5_eligible": False,
            "heldout_generator_claim_eligible": False,
            "supplemental_qualifications_count_for_gate_credit": False,
        },
        "labels_created_by_estimator": False,
        "supervision_eligibility_changed": False,
        "gate_5g_closed": False,
        "gate_5_closed": False,
    }
    report_id = "lf021_prevalence_report_v2:" + hash_canonical(
        {"schema": "lf021_prevalence_report_v2", **payload}
    )
    return PrevalenceReportV2.model_validate({"report_id": report_id, **payload})


def estimate_prevalence_from_files(
    *,
    repo_root: Path,
    frame_decision_path: Path,
    adjudication_path: Path,
    policy_path: Path,
    frame_freeze_policy_path: Path,
) -> PrevalenceReportV2:
    from leanfaith.generation.frame_freeze_v3 import (
        FrameFreezeV3Error,
        verify_frame_freeze_v3,
    )

    try:
        adjudication_bytes = adjudication_path.read_bytes()
    except OSError as exc:
        raise PrevalenceInputError(f"prevalence input is unavailable: {exc}") from exc
    loaded_policy = load_prevalence_design_policy(policy_path)
    verify_prevalence_design_policy_v2(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
    )
    try:
        verified = verify_frame_freeze_v3(
            repo_root=repo_root,
            policy_path=frame_freeze_policy_path,
            decision_path=frame_decision_path,
        )
    except (OSError, ValueError, FrameFreezeV3Error) as exc:
        raise PrevalenceInputError(
            f"randomized frame-freeze v3 verification failed: {exc}"
        ) from exc
    frame, verified_frame_binding = project_verified_frame_freeze_v3(verified)
    adjudications = load_adjudication_projection_bytes(
        adjudication_bytes,
        location=str(adjudication_path),
    )
    return estimate_prevalence(
        frame=frame,
        adjudications=adjudications,
        loaded_policy=loaded_policy,
        verified_frame_binding=verified_frame_binding,
        adjudication_projection_sha256=sha256_hex(adjudication_bytes),
    )


def validate_prevalence_output_path(*, repo_root: Path, output_path: Path) -> Path:
    """Confine reports to the dedicated non-gating prevalence namespace."""

    try:
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise PrevalenceInputError(f"repository root is unavailable: {exc}") from exc
    if not root.is_dir():
        raise PrevalenceInputError("repository root is not a directory")
    prevalence_root = root / PREVALENCE_OUTPUT_ROOT
    candidate = output_path if output_path.is_absolute() else root / output_path
    lexical = Path(os.path.abspath(os.fspath(candidate)))
    if lexical == prevalence_root or not lexical.is_relative_to(prevalence_root):
        raise PrevalenceInputError(
            f"prevalence output must be a JSON file under {PREVALENCE_OUTPUT_ROOT}"
        )
    if lexical.suffix != ".json":
        raise PrevalenceInputError("prevalence output must have a .json suffix")
    relative_parts = lexical.relative_to(prevalence_root).parts
    forbidden = (
        "gate",
        "label",
        "annotation",
        "supervision",
        "split",
        "input",
    )
    for raw_part in relative_parts:
        stem = Path(raw_part.lower()).stem.replace("-", "_")
        words = set(stem.split("_"))
        if any(token in words or f"{token}s" in words for token in forbidden):
            raise PrevalenceInputError(
                "prevalence output cannot target a gate, label, annotation, "
                "supervision, split, or input path"
            )

    current = root
    for part in lexical.relative_to(root).parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise PrevalenceInputError(
                f"cannot inspect prevalence output component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PrevalenceInputError(f"prevalence output path contains a symlink: {current}")
    try:
        resolved_namespace = prevalence_root.resolve(strict=False)
    except OSError as exc:
        raise PrevalenceInputError(f"cannot resolve prevalence output namespace: {exc}") from exc
    if not resolved_namespace.is_relative_to(root):
        raise PrevalenceInputError("canonical reports/prevalence namespace escapes the repository")
    return lexical


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_output_parent_no_follow(
    *,
    repo_root: Path,
    output_path: Path,
) -> tuple[int, tuple[int, ...]]:
    """Open/create every parent through trusted directory descriptors."""

    root = repo_root.resolve(strict=True)
    relative_parent = output_path.parent.relative_to(root)
    opened: list[int] = []
    current_fd = os.open(root, _directory_open_flags())
    opened.append(current_fd)
    try:
        for component in relative_parent.parts:
            try:
                next_fd = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    os.fsync(current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise PrevalenceInputError(
                    f"prevalence output parent is not a trusted directory: {component}"
                ) from exc
            opened.append(next_fd)
            current_fd = next_fd
    except Exception:
        for descriptor in reversed(opened):
            os.close(descriptor)
        raise
    return current_fd, tuple(opened)


def _read_fd_bytes(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    """Return fields that must stay stable while report bytes are read."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _verify_published_inode(
    *,
    repo_root: Path,
    output_path: Path,
    expected: os.stat_result,
) -> None:
    validated = validate_prevalence_output_path(
        repo_root=repo_root,
        output_path=output_path,
    )
    try:
        observed = validated.lstat()
    except OSError as exc:
        raise PrevalenceInputError(
            "published prevalence report is no longer reachable at its trusted path"
        ) from exc
    if stat.S_ISLNK(observed.st_mode) or (
        observed.st_dev,
        observed.st_ino,
    ) != (
        expected.st_dev,
        expected.st_ino,
    ):
        raise PrevalenceInputError("prevalence output path changed during atomic publication")


def _existing_report_matches(
    *,
    parent_fd: int,
    filename: str,
    payload: bytes,
) -> os.stat_result | None:
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PrevalenceInputError(
            "existing prevalence output is not a trusted regular file"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PrevalenceInputError("existing prevalence output is not a regular file")
        if _read_fd_bytes(descriptor) != payload:
            raise PrevalenceInputError(
                f"refusing to overwrite divergent prevalence report {filename}"
            )
        if _stable_file_identity(os.fstat(descriptor)) != _stable_file_identity(metadata):
            raise PrevalenceInputError("existing prevalence output changed while it was read")
        return metadata
    finally:
        os.close(descriptor)


def write_prevalence_report(
    report: PrevalenceReportV2,
    output_path: Path,
    *,
    repo_root: Path,
) -> str:
    """Atomically publish canonical bytes without following output symlinks."""

    output_path = validate_prevalence_output_path(
        repo_root=repo_root,
        output_path=output_path,
    )
    payload = canonical_json_bytes(report.model_dump(mode="json")) + b"\n"
    parent_fd, opened = _open_output_parent_no_follow(
        repo_root=repo_root,
        output_path=output_path,
    )
    filename = output_path.name
    temporary = f".{filename}.tmp.{os.getpid()}.{secrets.token_hex(16)}"
    published = False
    try:
        existing = _existing_report_matches(
            parent_fd=parent_fd,
            filename=filename,
            payload=payload,
        )
        if existing is not None:
            _verify_published_inode(
                repo_root=repo_root,
                output_path=output_path,
                expected=existing,
            )
            return sha256_hex(payload)

        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                chunk_size = os.write(descriptor, view[written:])
                if chunk_size == 0:
                    raise PrevalenceInputError("prevalence report staging write made no progress")
                written += chunk_size
            os.fsync(descriptor)
            expected = os.fstat(descriptor)
        finally:
            os.close(descriptor)

        try:
            os.link(
                temporary,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            published = True
        except FileExistsError:
            existing = _existing_report_matches(
                parent_fd=parent_fd,
                filename=filename,
                payload=payload,
            )
            if existing is None:
                raise PrevalenceInputError(
                    "prevalence output raced with a disappearing target"
                ) from None
            expected = existing
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
        try:
            _verify_published_inode(
                repo_root=repo_root,
                output_path=output_path,
                expected=expected,
            )
        except Exception:
            if published:
                try:
                    current = os.stat(
                        filename,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) == (
                        expected.st_dev,
                        expected.st_ino,
                    ):
                        os.unlink(filename, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                except OSError:
                    pass
            raise
        return sha256_hex(payload)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)
        for descriptor in reversed(opened):
            os.close(descriptor)


__all__ = [
    "PREVALENCE_ESTIMATOR_VERSION",
    "PREVALENCE_OUTPUT_ROOT",
    "RANDOMIZED_SAMPLING_METHOD",
    "AdjudicationProjectionAccounting",
    "AdjudicationProjectionV1",
    "EstimandResult",
    "EstimateInterval",
    "IntervalStatus",
    "PointEstimateScope",
    "PrevalenceDesignPolicyV1",
    "PrevalenceDesignPolicyV2",
    "PrevalenceFrameUnitV2",
    "PrevalenceInputError",
    "PrevalenceOutcome",
    "PrevalenceReportV2",
    "ScalarPrevalenceEstimate",
    "VerifiedPrevalenceFrameBinding",
    "estimate_prevalence",
    "estimate_prevalence_from_files",
    "load_adjudication_projection",
    "load_adjudication_projection_bytes",
    "load_prevalence_design_policy",
    "load_prevalence_design_policy_v1",
    "load_v2_frame_projection",
    "load_v2_frame_projection_bytes",
    "load_verified_v2_frame_projection_bytes",
    "project_verified_frame_freeze_v3",
    "validate_prevalence_output_path",
    "verify_prevalence_design_policy_v2",
    "write_prevalence_report",
]
