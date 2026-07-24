"""Prospective, randomized LF-021 problem-aware prevalence-frame freeze.

Revision 2 remains immutable and is used only to construct the scientific
``(problem_group, alpha_identity_fingerprint)`` population and to determine
when collection would stop.  Its checked-in fixed-salt frame is never reused.

Revision 3 freezes the complete eligible population before obtaining exactly
one 256-bit seed, archives that seed as a content-addressed artifact, and
selects within frozen strata using a versioned HMAC-SHA-256 rank.  The module
does not inspect or create semantic labels, admit supervision, or close a Gate.
"""

from __future__ import annotations

import fcntl
import hmac
import json
import secrets
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation import tranche_expansion as v1
from leanfaith.generation import tranche_expansion_v2 as v2

_HEX64 = r"^[0-9a-f]{64}$"
_POPULATION_RECORD_ID = r"^lf021_eligible_population_item_v3:[0-9a-f]{64}$"
_POPULATION_ID = r"^lf021_eligible_population_v3:[0-9a-f]{64}$"
_SEED_PROVENANCE_ID = r"^lf021_sampling_seed_v3:[0-9a-f]{64}$"
_FRAME_RECORD_ID = r"^lf021_prevalence_item_v3:[0-9a-f]{64}$"
_FRAME_ID = r"^lf021_prevalence_frame_v3:[0-9a-f]{64}$"
_DECISION_ID = r"^lf021_frame_freeze_decision_v3:[0-9a-f]{64}$"
_SAMPLING_METHOD = "problem_aware_stratified_csprng_srs_without_replacement_v2"
_RANK_ALGORITHM = "hmac_sha256_keyed_rank_v1"
_V2_FIXED_SALT_METHOD = "problem_aware_stratified_hash_srs_without_replacement_v2"


class FrameFreezeV3Error(RuntimeError):
    """The v3 population, seed, frame, or immutable binding failed closed."""


class FrameFreezePolicyV3(StrictModel):
    """Frozen frame-only amendment over exact revision-2 bytes."""

    schema_version: Literal[3] = 3
    policy_id: Literal["lf021_problem_aware_frame_freeze_v3"]
    status: Literal["frozen_prelabel"]
    base_v2_policy: v1.ArtifactBinding
    base_v2_implementation: v1.ArtifactBinding
    population_unit: tuple[str, str]
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_rank_algorithm: Literal["hmac_sha256_keyed_rank_v1"]
    sampling_domain_separator: str = Field(min_length=16)
    sampling_rank_message_encoding: Literal["utf8_domain_nul_stratum_nul_cluster_id_v1"]
    sampling_seed_bytes: Literal[32]
    sampling_seed_generation: Literal["single_draw_after_population_freeze"]
    fixed_salt_v2_frame_eligible: Literal[False] = False
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.population_unit != (
            "problem_group",
            "alpha_identity_fingerprint",
        ):
            raise ValueError("v3 population unit must be problem_group x alpha")
        return self


class PopulationMemberV3(StrictModel):
    """One retained compiling invocation in a scientific population unit."""

    invocation_id: str = Field(min_length=1)
    problem_group: str = Field(min_length=1)
    problem_record_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    pool_id: str = Field(min_length=1)
    source_proxy: str = Field(min_length=1)
    postprocess_manifest_id: str = Field(min_length=1)
    terminal_artifact: v1.ArtifactBinding
    screening_artifact: v1.ArtifactBinding
    representation_artifact: v1.ArtifactBinding


class EligiblePopulationItemV3(StrictModel):
    """One fully reconciled problem-aware unit in the frozen population."""

    schema_version: Literal[3] = 3
    population_record_id: str = Field(pattern=_POPULATION_RECORD_ID)
    cluster_id: str
    problem_group: str = Field(min_length=1)
    alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    representative_invocation_id: str = Field(min_length=1)
    representative_family_id: str = Field(min_length=1)
    representative_pool_id: str = Field(min_length=1)
    representative_source_proxy: str = Field(min_length=1)
    representative_problem_record_id: str = Field(min_length=1)
    terminal_artifact: v1.ArtifactBinding
    screening_artifact: v1.ArtifactBinding
    representation_artifact: v1.ArtifactBinding
    members: tuple[PopulationMemberV3, ...] = Field(min_length=1)
    contributing_invocation_ids: tuple[str, ...] = Field(min_length=1)
    contributing_problem_record_ids: tuple[str, ...] = Field(min_length=1)
    contributing_family_ids: tuple[str, ...] = Field(min_length=1)
    contributing_pool_ids: tuple[str, ...] = Field(min_length=1)
    contributing_source_proxies: tuple[str, ...] = Field(min_length=1)
    postprocess_manifest_ids: tuple[str, ...] = Field(min_length=1)
    member_count: int = Field(ge=1)
    member_count_by_family: dict[str, int] = Field(min_length=1)
    member_count_by_pool: dict[str, int] = Field(min_length=1)
    member_count_by_source_proxy: dict[str, int] = Field(min_length=1)
    sampling_stratum: str = Field(min_length=1)
    same_claim: None = None
    relation: None = None
    resolution_outcome: Literal["unresolved"] = "unresolved"
    quality_tier: Literal["unknown"] = "unknown"
    requires_adjudication: Literal[True] = True
    decision: Literal["REVIEW"] = "REVIEW"
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected_cluster = "candidate_cluster_v2:" + hash_canonical(
            {
                "schema": "lf021_problem_group_alpha_cluster_v2",
                "problem_group": self.problem_group,
                "alpha_identity_fingerprint": self.alpha_identity_fingerprint,
            }
        )
        if self.cluster_id != expected_cluster:
            raise ValueError("cluster ID differs from problem_group x alpha")
        if self.members != tuple(sorted(self.members, key=lambda item: item.invocation_id)):
            raise ValueError("population members must be invocation-sorted")
        member_by_invocation = {item.invocation_id: item for item in self.members}
        if len(member_by_invocation) != len(self.members):
            raise ValueError("population member invocation IDs must be unique")
        if any(item.problem_group != self.problem_group for item in self.members):
            raise ValueError("population member crosses problem group")
        representative = member_by_invocation.get(self.representative_invocation_id)
        if representative is None:
            raise ValueError("representative invocation is absent from members")
        if (
            representative.family_id != self.representative_family_id
            or representative.pool_id != self.representative_pool_id
            or representative.source_proxy != self.representative_source_proxy
            or representative.problem_record_id != self.representative_problem_record_id
            or representative.terminal_artifact != self.terminal_artifact
            or representative.screening_artifact != self.screening_artifact
            or representative.representation_artifact != self.representation_artifact
        ):
            raise ValueError("representative fields differ from representative member")

        expected_sets: dict[str, tuple[str, ...]] = {
            "contributing_invocation_ids": tuple(sorted(member_by_invocation)),
            "contributing_problem_record_ids": tuple(
                sorted({item.problem_record_id for item in self.members})
            ),
            "contributing_family_ids": tuple(sorted({item.family_id for item in self.members})),
            "contributing_pool_ids": tuple(sorted({item.pool_id for item in self.members})),
            "contributing_source_proxies": tuple(
                sorted({item.source_proxy for item in self.members})
            ),
            "postprocess_manifest_ids": tuple(
                sorted({item.postprocess_manifest_id for item in self.members})
            ),
        }
        for field_name, expected in expected_sets.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name} differs from population members")
        if self.member_count != len(self.members):
            raise ValueError("member_count differs from population members")
        expected_family_counts = dict(
            sorted(Counter(item.family_id for item in self.members).items())
        )
        expected_pool_counts = dict(sorted(Counter(item.pool_id for item in self.members).items()))
        expected_proxy_counts = dict(
            sorted(Counter(item.source_proxy for item in self.members).items())
        )
        if self.member_count_by_family != expected_family_counts:
            raise ValueError("family multiplicities differ from population members")
        if self.member_count_by_pool != expected_pool_counts:
            raise ValueError("pool multiplicities differ from population members")
        if self.member_count_by_source_proxy != expected_proxy_counts:
            raise ValueError("source-proxy multiplicities differ from population members")
        if any(value <= 0 for value in self.member_count_by_family.values()):
            raise ValueError("family multiplicities must be strictly positive")
        if any(value <= 0 for value in self.member_count_by_pool.values()):
            raise ValueError("pool multiplicities must be strictly positive")
        if any(value <= 0 for value in self.member_count_by_source_proxy.values()):
            raise ValueError("source-proxy multiplicities must be strictly positive")
        expected_stratum = (
            f"{self.representative_family_id}|"
            f"{self.representative_pool_id}|"
            f"{self.representative_source_proxy}"
        )
        if self.sampling_stratum != expected_stratum:
            raise ValueError("sampling stratum differs from representative fields")
        expected_id = "lf021_eligible_population_item_v3:" + hash_canonical(
            {
                "schema": "lf021_eligible_population_item_v3",
                **self.model_dump(mode="json", exclude={"population_record_id"}),
            }
        )
        if self.population_record_id != expected_id:
            raise ValueError("population record ID differs from content")
        return self


class EligiblePopulationManifestV3(StrictModel):
    """Binding for the complete eligible population frozen before the seed."""

    schema_version: Literal[3] = 3
    population_id: str = Field(pattern=_POPULATION_ID)
    policy_id: Literal["lf021_problem_aware_frame_freeze_v3"]
    policy_artifact: v1.ArtifactBinding
    v2_stop_decision_id: str
    v2_stop_decision: v1.ArtifactBinding
    v2_fixed_salt_frame_reused: Literal[False] = False
    population_artifact: v1.ArtifactBinding
    population_item_count: int = Field(ge=1)
    population_member_count: int = Field(ge=1)
    stratum_population_sizes: dict[str, int] = Field(min_length=1)
    frozen_at: str = Field(min_length=1)
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        _parse_utc(self.frozen_at)
        if any(value <= 0 for value in self.stratum_population_sizes.values()):
            raise ValueError("population strata must be nonempty")
        if sum(self.stratum_population_sizes.values()) != self.population_item_count:
            raise ValueError("stratum population sizes do not reconcile")
        expected = "lf021_eligible_population_v3:" + hash_canonical(
            {
                "schema": "lf021_eligible_population_manifest_v3",
                **self.model_dump(mode="json", exclude={"population_id"}),
            }
        )
        if self.population_id != expected:
            raise ValueError("population ID differs from content")
        return self


class SamplingSeedProvenanceV3(StrictModel):
    """One population-bound 256-bit seed draw archived before annotation."""

    schema_version: Literal[3] = 3
    provenance_id: str = Field(pattern=_SEED_PROVENANCE_ID)
    source: Literal[
        "os_csprng_secrets_token_bytes_256",
        "external_randomness_beacon_256",
        "test_replay_seed_256",
    ]
    entropy_bits: Literal[256] = 256
    generated_at: str = Field(min_length=1)
    single_draw: Literal[True] = True
    population_id: str = Field(pattern=_POPULATION_ID)
    population_manifest: v1.ArtifactBinding
    population_artifact: v1.ArtifactBinding
    sampling_seed: v1.ArtifactBinding
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    external_beacon_provenance: v1.ArtifactBinding | None = None
    test_replay_only: bool
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        _parse_utc(self.generated_at)
        if self.sampling_seed.sha256 != self.sampling_seed_sha256:
            raise ValueError("seed artifact hash differs from sampling_seed_sha256")
        if self.source == "external_randomness_beacon_256":
            if self.external_beacon_provenance is None or self.test_replay_only:
                raise ValueError("external beacon seed requires production provenance")
        elif self.external_beacon_provenance is not None:
            raise ValueError("non-beacon seed cannot carry beacon provenance")
        if (self.source == "test_replay_seed_256") != self.test_replay_only:
            raise ValueError("test-replay source and scope flag differ")
        expected = "lf021_sampling_seed_v3:" + hash_canonical(
            {
                "schema": "lf021_sampling_seed_provenance_v3",
                **self.model_dump(mode="json", exclude={"provenance_id"}),
            }
        )
        if self.provenance_id != expected:
            raise ValueError("seed provenance ID differs from content")
        return self


class ExternalRandomnessBeaconProvenanceV3(StrictModel):
    """Canonical local wrapper proving when external entropy was obtained."""

    schema_version: Literal[3] = 3
    record_kind: Literal["lf021_external_randomness_beacon_v3"]
    beacon_id: str = Field(min_length=1)
    source_reference: str = Field(min_length=1)
    obtained_at: str = Field(min_length=1)
    sampling_seed_sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        _parse_utc(self.obtained_at)
        return self


class SeedLockV3(StrictModel):
    """One immutable population-to-seed-provenance assignment."""

    schema_version: Literal[3] = 3
    population_id: str = Field(pattern=_POPULATION_ID)
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    sampling_seed_provenance: v1.ArtifactBinding


class FrameItemV3(StrictModel):
    """One probability-sampled population item with exact provenance."""

    schema_version: Literal[3] = 3
    frame_record_id: str = Field(pattern=_FRAME_RECORD_ID)
    population_manifest_id: str = Field(pattern=_POPULATION_ID)
    population_manifest: v1.ArtifactBinding
    population_record_id: str = Field(pattern=_POPULATION_RECORD_ID)
    cluster_id: str
    problem_group: str = Field(min_length=1)
    alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    representative_invocation_id: str = Field(min_length=1)
    representative_family_id: str = Field(min_length=1)
    representative_pool_id: str = Field(min_length=1)
    representative_source_proxy: str = Field(min_length=1)
    representative_problem_record_id: str = Field(min_length=1)
    terminal_artifact: v1.ArtifactBinding
    screening_artifact: v1.ArtifactBinding
    representation_artifact: v1.ArtifactBinding
    members: tuple[PopulationMemberV3, ...] = Field(min_length=1)
    contributing_invocation_ids: tuple[str, ...] = Field(min_length=1)
    contributing_problem_record_ids: tuple[str, ...] = Field(min_length=1)
    contributing_family_ids: tuple[str, ...] = Field(min_length=1)
    contributing_pool_ids: tuple[str, ...] = Field(min_length=1)
    contributing_source_proxies: tuple[str, ...] = Field(min_length=1)
    postprocess_manifest_ids: tuple[str, ...] = Field(min_length=1)
    member_count: int = Field(ge=1)
    member_count_by_family: dict[str, int] = Field(min_length=1)
    member_count_by_pool: dict[str, int] = Field(min_length=1)
    member_count_by_source_proxy: dict[str, int] = Field(min_length=1)
    sampling_stratum: str = Field(min_length=1)
    stratum_population_size: int = Field(ge=1)
    stratum_sample_size: int = Field(ge=1)
    inclusion_probability_numerator: int = Field(ge=1)
    inclusion_probability_denominator: int = Field(ge=1)
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_rank_algorithm: Literal["hmac_sha256_keyed_rank_v1"]
    sampling_rank_digest: str = Field(pattern=_HEX64)
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    sampling_seed_provenance: v1.ArtifactBinding
    test_replay_only: bool
    same_claim: None = None
    relation: None = None
    resolution_outcome: Literal["unresolved"] = "unresolved"
    quality_tier: Literal["unknown"] = "unknown"
    requires_adjudication: Literal[True] = True
    decision: Literal["REVIEW"] = "REVIEW"
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        population_payload = {
            "schema_version": 3,
            "population_record_id": self.population_record_id,
            "cluster_id": self.cluster_id,
            "problem_group": self.problem_group,
            "alpha_identity_fingerprint": self.alpha_identity_fingerprint,
            "representative_invocation_id": self.representative_invocation_id,
            "representative_family_id": self.representative_family_id,
            "representative_pool_id": self.representative_pool_id,
            "representative_source_proxy": self.representative_source_proxy,
            "representative_problem_record_id": self.representative_problem_record_id,
            "terminal_artifact": self.terminal_artifact.model_dump(mode="json"),
            "screening_artifact": self.screening_artifact.model_dump(mode="json"),
            "representation_artifact": self.representation_artifact.model_dump(mode="json"),
            "members": tuple(item.model_dump(mode="json") for item in self.members),
            "contributing_invocation_ids": self.contributing_invocation_ids,
            "contributing_problem_record_ids": self.contributing_problem_record_ids,
            "contributing_family_ids": self.contributing_family_ids,
            "contributing_pool_ids": self.contributing_pool_ids,
            "contributing_source_proxies": self.contributing_source_proxies,
            "postprocess_manifest_ids": self.postprocess_manifest_ids,
            "member_count": self.member_count,
            "member_count_by_family": self.member_count_by_family,
            "member_count_by_pool": self.member_count_by_pool,
            "member_count_by_source_proxy": self.member_count_by_source_proxy,
            "sampling_stratum": self.sampling_stratum,
            "same_claim": None,
            "relation": None,
            "resolution_outcome": "unresolved",
            "quality_tier": "unknown",
            "requires_adjudication": True,
            "decision": "REVIEW",
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        }
        EligiblePopulationItemV3.model_validate(population_payload)
        if self.stratum_sample_size > self.stratum_population_size:
            raise ValueError("stratum sample exceeds population")
        if (
            self.inclusion_probability_numerator != self.stratum_sample_size
            or self.inclusion_probability_denominator != self.stratum_population_size
        ):
            raise ValueError("inclusion probability must equal n_h/N_h")
        expected = "lf021_prevalence_item_v3:" + hash_canonical(
            {
                "schema": "lf021_prevalence_frame_item_v3",
                **self.model_dump(mode="json", exclude={"frame_record_id"}),
            }
        )
        if self.frame_record_id != expected:
            raise ValueError("frame record ID differs from content")
        return self


class FrameBindingV3(StrictModel):
    """Content-addressed randomized v3 prevalence frame."""

    frame_id: str = Field(pattern=_FRAME_ID)
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    item_count: int = Field(ge=1)
    population_id: str = Field(pattern=_POPULATION_ID)
    population_manifest: v1.ArtifactBinding
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    sampling_seed_provenance: v1.ArtifactBinding
    test_replay_only: bool

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected = "lf021_prevalence_frame_v3:" + hash_canonical(
            {
                "schema": "lf021_prevalence_frame_binding_v3",
                **self.model_dump(mode="json", exclude={"frame_id"}),
            }
        )
        if self.frame_id != expected:
            raise ValueError("frame ID differs from content")
        return self


class FrameFreezeDecisionV3(StrictModel):
    """Complete population, entropy, algorithm, and selected-frame binding."""

    schema_version: Literal[3] = 3
    decision_id: str = Field(pattern=_DECISION_ID)
    policy_id: Literal["lf021_problem_aware_frame_freeze_v3"]
    policy_artifact: v1.ArtifactBinding
    implementation_artifact: v1.ArtifactBinding
    v2_stop_decision_id: str
    v2_stop_decision: v1.ArtifactBinding
    observations: tuple[v1.ObservationBinding, ...]
    counts: v1.OperationalCounts
    coverage_deficits: tuple[str, ...]
    action: Literal["freeze_preferred_frame", "freeze_reduced_frame"]
    next_tranche: None = None
    v2_stop_action: Literal["freeze_preferred_frame", "freeze_reduced_frame"]
    v2_fixed_salt_sampling_method: Literal[
        "problem_aware_stratified_hash_srs_without_replacement_v2"
    ]
    v2_fixed_salt_frame_reused: Literal[False] = False
    population_id: str = Field(pattern=_POPULATION_ID)
    population_manifest: v1.ArtifactBinding
    population_artifact: v1.ArtifactBinding
    population_item_count: int = Field(ge=1)
    population_member_count: int = Field(ge=1)
    stratum_population_sizes: dict[str, int] = Field(min_length=1)
    stratum_sample_sizes: dict[str, int] = Field(min_length=1)
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_rank_algorithm: Literal["hmac_sha256_keyed_rank_v1"]
    sampling_domain_separator: str = Field(min_length=16)
    sampling_rank_message_encoding: Literal["utf8_domain_nul_stratum_nul_cluster_id_v1"]
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    sampling_seed_provenance: v1.ArtifactBinding
    test_replay_only: bool
    frame: FrameBindingV3
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.action != self.v2_stop_action:
            raise ValueError("v3 action differs from verified v2 stop action")
        if self.counts.unique_compiling_count != self.population_item_count:
            raise ValueError("v2 scientific count differs from v3 population")
        if self.counts.benchmark_clear_compile_count != self.population_member_count:
            raise ValueError("v2 member count differs from v3 population")
        if set(self.stratum_sample_sizes) != set(self.stratum_population_sizes):
            raise ValueError("sample/population stratum keys differ")
        if any(
            self.stratum_sample_sizes[key] < 1
            or self.stratum_sample_sizes[key] > self.stratum_population_sizes[key]
            for key in self.stratum_population_sizes
        ):
            raise ValueError("invalid stratum sample allocation")
        if sum(self.stratum_population_sizes.values()) != self.population_item_count:
            raise ValueError("population strata do not reconcile")
        if sum(self.stratum_sample_sizes.values()) != self.frame.item_count:
            raise ValueError("sample strata do not reconcile")
        if (
            self.frame.population_id != self.population_id
            or self.frame.population_manifest != self.population_manifest
            or self.frame.sampling_method != self.sampling_method
            or self.frame.sampling_seed_sha256 != self.sampling_seed_sha256
            or self.frame.sampling_seed_provenance != self.sampling_seed_provenance
            or self.frame.test_replay_only != self.test_replay_only
        ):
            raise ValueError("frame binding differs from decision inputs")
        expected = "lf021_frame_freeze_decision_v3:" + hash_canonical(
            {
                "schema": "lf021_frame_freeze_decision_v3",
                **self.model_dump(mode="json", exclude={"decision_id"}),
            }
        )
        if self.decision_id != expected:
            raise ValueError("frame-freeze decision ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedV2Stop:
    decision: v2.ExpansionDecisionV2
    decision_binding: v1.ArtifactBinding
    base_policy: v1.TrancheExpansionPolicy
    clusters: tuple[v2._ProblemAwareCluster, ...]
    problem_groups: dict[str, str]


@dataclass(frozen=True, slots=True)
class PopulationRunV3:
    manifest: EligiblePopulationManifestV3
    manifest_path: Path
    population_path: Path
    items: tuple[EligiblePopulationItemV3, ...]


@dataclass(frozen=True, slots=True)
class SeedRunV3:
    provenance: SamplingSeedProvenanceV3
    provenance_path: Path
    seed_path: Path
    lock_path: Path


@dataclass(frozen=True, slots=True)
class FrameRunV3:
    decision: FrameFreezeDecisionV3
    decision_path: Path
    frame_path: Path
    items: tuple[FrameItemV3, ...]


@dataclass(frozen=True, slots=True)
class VerifiedFrameFreezeV3:
    """Strict, read-only projection of a completely replayed v3 frame."""

    decision: FrameFreezeDecisionV3
    decision_path: Path
    decision_binding: v1.ArtifactBinding
    verified_v2_stop: VerifiedV2Stop
    population: PopulationRunV3
    seed_provenance: SamplingSeedProvenanceV3
    seed_provenance_path: Path
    seed_bytes: bytes
    seed_lock: SeedLockV3
    seed_lock_path: Path
    frame_path: Path
    frame_items: tuple[FrameItemV3, ...]


def _parse_utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("timestamp must use UTC Z suffix")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp is not ISO-8601 UTC") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("timestamp must be UTC")
    return parsed


def _strict_json_object(payload: bytes, *, location: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FrameFreezeV3Error(f"duplicate JSON key {key!r}: {location}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                FrameFreezeV3Error(f"non-finite JSON value {token!r}: {location}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameFreezeV3Error(f"invalid JSON: {location}") from exc
    if not isinstance(value, dict):
        raise FrameFreezeV3Error(f"JSON root is not an object: {location}")
    return value


def _resolve(repo_root: Path, artifact: str) -> Path:
    path = Path(artifact)
    resolved = (path if path.is_absolute() else repo_root / path).resolve()
    if not path.is_absolute() and not resolved.is_relative_to(repo_root.resolve()):
        raise FrameFreezeV3Error(f"artifact escapes repository: {artifact}")
    return resolved


def _binding(repo_root: Path, path: Path) -> v1.ArtifactBinding:
    return v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, path),
        sha256=hash_file(path),
    )


def _verify(repo_root: Path, binding: v1.ArtifactBinding) -> Path:
    path = _resolve(repo_root, binding.artifact)
    if not path.is_file() or hash_file(path) != binding.sha256:
        raise FrameFreezeV3Error(f"bound artifact differs: {binding.artifact}")
    return path


def _write_immutable(path: Path, payload: bytes) -> None:
    try:
        v1._write_immutable(path, payload)
    except v1.TrancheExpansionError as exc:
        raise FrameFreezeV3Error(str(exc)) from exc


def load_frame_freeze_policy_v3(path: Path) -> LoadedConfig[FrameFreezePolicyV3]:
    """Load the frozen frame-only revision-3 policy."""

    return load_config(path, FrameFreezePolicyV3)


def load_verified_v2_stop(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[FrameFreezePolicyV3],
    decision_path: Path,
) -> VerifiedV2Stop:
    """Recompute an exact v2 stopping decision; never reuse its fixed-salt frame."""

    policy = loaded_policy.config
    v2_policy_path = _verify(repo_root, policy.base_v2_policy)
    v2_implementation_path = _verify(repo_root, policy.base_v2_implementation)
    if v2_implementation_path.resolve() != Path(v2.__file__).resolve():
        raise FrameFreezeV3Error("bound v2 implementation is not the imported module")
    decision_bytes = decision_path.read_bytes()
    decision = v2.ExpansionDecisionV2.model_validate(
        _strict_json_object(decision_bytes, location=str(decision_path))
    )
    if decision.policy_artifact != policy.base_v2_policy:
        raise FrameFreezeV3Error("v2 decision policy differs from v3 binding")
    if decision.implementation_artifact != policy.base_v2_implementation:
        raise FrameFreezeV3Error("v2 decision implementation differs from v3 binding")
    if decision.action not in {
        v1.DecisionAction.FREEZE_PREFERRED_FRAME,
        v1.DecisionAction.FREEZE_REDUCED_FRAME,
    }:
        raise FrameFreezeV3Error("v3 activates only when v2 would stop collection")
    if decision.frame is None or decision.frame.sampling_method != _V2_FIXED_SALT_METHOD:
        raise FrameFreezeV3Error("v2 stop decision lacks its expected historical frame marker")

    observed_paths = tuple(
        _resolve(repo_root, item.postprocess_manifest.artifact) for item in decision.observations
    )
    loaded_v2 = v2.load_amendment_v2(v2_policy_path)
    recomputed, _fixed_salt_bytes = v2.evaluate_tranche_expansion_v2(
        repo_root=repo_root,
        loaded_amendment=loaded_v2,
        observed_manifests=observed_paths,
    )
    if recomputed != decision:
        raise FrameFreezeV3Error("v2 stop decision does not replay from bound observations")

    base_policy = v1.load_tranche_expansion_policy(
        _verify(repo_root, loaded_v2.config.base_v1_policy)
    ).config
    observations = tuple(
        v1.load_postprocess_observation(
            repo_root=repo_root,
            policy=base_policy,
            tranche=base_policy.tranches[index],
            manifest_path=manifest_path,
        )
        for index, manifest_path in enumerate(observed_paths)
    )
    problem_groups = v2._load_problem_groups(repo_root=repo_root, policy=base_policy)
    clusters = v2._cluster_candidates(
        observations,
        problem_groups=problem_groups,
        representative_hash_salt=loaded_v2.config.representative_hash_salt,
    )
    return VerifiedV2Stop(
        decision=decision,
        decision_binding=v1.ArtifactBinding(
            artifact=v1._relative_or_absolute(repo_root, decision_path),
            sha256=sha256_hex(decision_bytes),
        ),
        base_policy=base_policy,
        clusters=clusters,
        problem_groups=problem_groups,
    )


def _population_item(
    cluster: v2._ProblemAwareCluster,
    *,
    problem_groups: dict[str, str],
) -> EligiblePopulationItemV3:
    members = tuple(
        PopulationMemberV3(
            invocation_id=member.invocation_id,
            problem_group=problem_groups[member.problem_record_id],
            problem_record_id=member.problem_record_id,
            family_id=member.family_id,
            pool_id=member.pool_id,
            source_proxy=member.source_proxy,
            postprocess_manifest_id=member.postprocess_manifest_id,
            terminal_artifact=member.terminal_artifact,
            screening_artifact=member.screening_artifact,
            representation_artifact=member.representation_artifact,
        )
        for member in sorted(cluster.members, key=lambda item: item.invocation_id)
    )
    representative = next(
        item for item in members if item.invocation_id == cluster.representative.invocation_id
    )
    family_counts = dict(sorted(Counter(item.family_id for item in members).items()))
    pool_counts = dict(sorted(Counter(item.pool_id for item in members).items()))
    proxy_counts = dict(sorted(Counter(item.source_proxy for item in members).items()))
    payload: dict[str, Any] = {
        "schema_version": 3,
        "cluster_id": cluster.cluster_id,
        "problem_group": cluster.problem_group,
        "alpha_identity_fingerprint": cluster.alpha_identity_fingerprint,
        "representative_invocation_id": representative.invocation_id,
        "representative_family_id": representative.family_id,
        "representative_pool_id": representative.pool_id,
        "representative_source_proxy": representative.source_proxy,
        "representative_problem_record_id": representative.problem_record_id,
        "terminal_artifact": representative.terminal_artifact.model_dump(mode="json"),
        "screening_artifact": representative.screening_artifact.model_dump(mode="json"),
        "representation_artifact": representative.representation_artifact.model_dump(mode="json"),
        "members": tuple(item.model_dump(mode="json") for item in members),
        "contributing_invocation_ids": tuple(item.invocation_id for item in members),
        "contributing_problem_record_ids": tuple(
            sorted({item.problem_record_id for item in members})
        ),
        "contributing_family_ids": tuple(sorted(family_counts)),
        "contributing_pool_ids": tuple(sorted({item.pool_id for item in members})),
        "contributing_source_proxies": tuple(sorted(proxy_counts)),
        "postprocess_manifest_ids": tuple(
            sorted({item.postprocess_manifest_id for item in members})
        ),
        "member_count": len(members),
        "member_count_by_family": family_counts,
        "member_count_by_pool": pool_counts,
        "member_count_by_source_proxy": proxy_counts,
        "sampling_stratum": (
            f"{representative.family_id}|{representative.pool_id}|{representative.source_proxy}"
        ),
        "same_claim": None,
        "relation": None,
        "resolution_outcome": "unresolved",
        "quality_tier": "unknown",
        "requires_adjudication": True,
        "decision": "REVIEW",
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    record_id = "lf021_eligible_population_item_v3:" + hash_canonical(
        {"schema": "lf021_eligible_population_item_v3", **payload}
    )
    return EligiblePopulationItemV3.model_validate({"population_record_id": record_id, **payload})


def build_eligible_population_items_v3(
    verified_stop: VerifiedV2Stop,
) -> tuple[EligiblePopulationItemV3, ...]:
    """Build canonical population rows independent of cluster input order."""

    result = tuple(
        sorted(
            (
                _population_item(cluster, problem_groups=verified_stop.problem_groups)
                for cluster in verified_stop.clusters
            ),
            key=lambda item: item.population_record_id,
        )
    )
    keys = {(item.problem_group, item.alpha_identity_fingerprint) for item in result}
    if len(keys) != len(result):
        raise FrameFreezeV3Error("eligible population scientific keys are not unique")
    if len(result) != verified_stop.decision.counts.unique_compiling_count:
        raise FrameFreezeV3Error("eligible population differs from v2 scientific count")
    if sum(item.member_count for item in result) != (
        verified_stop.decision.counts.benchmark_clear_compile_count
    ):
        raise FrameFreezeV3Error("eligible population member count differs from v2")
    return result


def _population_bytes(items: tuple[EligiblePopulationItemV3, ...]) -> bytes:
    return b"".join(canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in items)


def freeze_eligible_population_v3(
    *,
    repo_root: Path,
    policy_path: Path,
    v2_decision_path: Path,
    output_root: Path,
    frozen_at: str,
) -> PopulationRunV3:
    """Freeze all eligible units before any sampling entropy is obtained."""

    _parse_utc(frozen_at)
    loaded = load_frame_freeze_policy_v3(policy_path)
    verified = load_verified_v2_stop(
        repo_root=repo_root,
        loaded_policy=loaded,
        decision_path=v2_decision_path,
    )
    items = build_eligible_population_items_v3(verified)
    payload = _population_bytes(items)
    population_sha = sha256_hex(payload)
    population_path = output_root / "populations" / f"{population_sha}.jsonl"
    population_binding = v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, population_path),
        sha256=population_sha,
    )
    stratum_sizes = dict(sorted(Counter(item.sampling_stratum for item in items).items()))
    policy_binding = v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, loaded.path),
        sha256=hash_file(loaded.path),
    )
    manifest_payload: dict[str, Any] = {
        "schema_version": 3,
        "policy_id": loaded.config.policy_id,
        "policy_artifact": policy_binding.model_dump(mode="json"),
        "v2_stop_decision_id": verified.decision.decision_id,
        "v2_stop_decision": verified.decision_binding.model_dump(mode="json"),
        "v2_fixed_salt_frame_reused": False,
        "population_artifact": population_binding.model_dump(mode="json"),
        "population_item_count": len(items),
        "population_member_count": sum(item.member_count for item in items),
        "stratum_population_sizes": stratum_sizes,
        "frozen_at": frozen_at,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    population_id = "lf021_eligible_population_v3:" + hash_canonical(
        {"schema": "lf021_eligible_population_manifest_v3", **manifest_payload}
    )
    manifest = EligiblePopulationManifestV3.model_validate(
        {"population_id": population_id, **manifest_payload}
    )
    manifest_path = (
        output_root / "populations" / f"{population_id.rsplit(':', 1)[-1]}.manifest.json"
    )
    _write_immutable(population_path, payload)
    _write_immutable(
        manifest_path,
        canonical_json_bytes(manifest.model_dump(mode="json")),
    )
    return PopulationRunV3(
        manifest=manifest,
        manifest_path=manifest_path,
        population_path=population_path,
        items=items,
    )


def load_eligible_population_v3(
    *,
    repo_root: Path,
    manifest_path: Path,
) -> PopulationRunV3:
    """Read one immutable population and recheck every row and total."""

    manifest_bytes = manifest_path.read_bytes()
    manifest = EligiblePopulationManifestV3.model_validate(
        _strict_json_object(manifest_bytes, location=str(manifest_path))
    )
    population_path = _verify(repo_root, manifest.population_artifact)
    rows: list[EligiblePopulationItemV3] = []
    for line_number, line in enumerate(
        population_path.read_bytes().splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        rows.append(
            EligiblePopulationItemV3.model_validate(
                _strict_json_object(
                    line,
                    location=f"{population_path}:{line_number}",
                )
            )
        )
    items = tuple(rows)
    if items != tuple(sorted(items, key=lambda item: item.population_record_id)):
        raise FrameFreezeV3Error("eligible population rows are not canonical-order")
    if len({item.population_record_id for item in items}) != len(items):
        raise FrameFreezeV3Error("duplicate eligible population record ID")
    if (
        len(items) != manifest.population_item_count
        or sum(item.member_count for item in items) != manifest.population_member_count
    ):
        raise FrameFreezeV3Error("eligible population totals differ from manifest")
    stratum_sizes = dict(sorted(Counter(item.sampling_stratum for item in items).items()))
    if stratum_sizes != manifest.stratum_population_sizes:
        raise FrameFreezeV3Error("eligible population strata differ from manifest")
    return PopulationRunV3(
        manifest=manifest,
        manifest_path=manifest_path,
        population_path=population_path,
        items=items,
    )


def _load_seed_provenance(
    *,
    repo_root: Path,
    provenance_path: Path,
) -> tuple[SamplingSeedProvenanceV3, bytes]:
    provenance = SamplingSeedProvenanceV3.model_validate(
        _strict_json_object(
            provenance_path.read_bytes(),
            location=str(provenance_path),
        )
    )
    seed_path = _verify(repo_root, provenance.sampling_seed)
    if provenance.external_beacon_provenance is not None:
        beacon_path = _verify(repo_root, provenance.external_beacon_provenance)
        beacon = ExternalRandomnessBeaconProvenanceV3.model_validate(
            _strict_json_object(
                beacon_path.read_bytes(),
                location=str(beacon_path),
            )
        )
        if beacon.sampling_seed_sha256 != provenance.sampling_seed_sha256:
            raise FrameFreezeV3Error(
                "external beacon provenance differs from archived sampling seed"
            )
    seed_bytes = seed_path.read_bytes()
    if len(seed_bytes) != 32 or sha256_hex(seed_bytes) != provenance.sampling_seed_sha256:
        raise FrameFreezeV3Error("archived sampling seed is not exact 256-bit content")
    return provenance, seed_bytes


def _archive_sampling_seed_v3_locked(
    *,
    repo_root: Path,
    population_manifest_path: Path,
    output_root: Path,
    generated_at: str,
    seed_bytes: bytes | None = None,
    external_beacon_provenance_path: Path | None = None,
    test_replay_only: bool = False,
) -> SeedRunV3:
    """Archive exactly one seed for a frozen population; replay reuses it."""

    population = load_eligible_population_v3(
        repo_root=repo_root,
        manifest_path=population_manifest_path,
    )
    generated = _parse_utc(generated_at)
    if generated < _parse_utc(population.manifest.frozen_at):
        raise FrameFreezeV3Error("sampling seed predates eligible-population freeze")
    population_suffix = population.manifest.population_id.rsplit(":", 1)[-1]
    lock_path = output_root / "seeds" / "by_population" / f"{population_suffix}.json"
    if lock_path.exists():
        lock = SeedLockV3.model_validate(
            _strict_json_object(lock_path.read_bytes(), location=str(lock_path))
        )
        if lock.population_id != population.manifest.population_id:
            raise FrameFreezeV3Error("seed lock population differs")
        provenance_path = _verify(repo_root, lock.sampling_seed_provenance)
        provenance, existing_seed = _load_seed_provenance(
            repo_root=repo_root,
            provenance_path=provenance_path,
        )
        if seed_bytes is not None and seed_bytes != existing_seed:
            raise FrameFreezeV3Error("population already has a different frozen seed")
        return SeedRunV3(
            provenance=provenance,
            provenance_path=provenance_path,
            seed_path=_resolve(repo_root, provenance.sampling_seed.artifact),
            lock_path=lock_path,
        )

    source: Literal[
        "os_csprng_secrets_token_bytes_256",
        "external_randomness_beacon_256",
        "test_replay_seed_256",
    ]
    external_beacon_binding: v1.ArtifactBinding | None = None
    if seed_bytes is None:
        if external_beacon_provenance_path is not None or test_replay_only:
            raise FrameFreezeV3Error("OS CSPRNG generation cannot carry beacon/test seed inputs")
        seed_bytes = secrets.token_bytes(32)
        source = "os_csprng_secrets_token_bytes_256"
    elif external_beacon_provenance_path is not None:
        if test_replay_only:
            raise FrameFreezeV3Error("external beacon seed cannot be test-only")
        if not external_beacon_provenance_path.is_file():
            raise FrameFreezeV3Error("external beacon provenance is missing")
        external_beacon = ExternalRandomnessBeaconProvenanceV3.model_validate(
            _strict_json_object(
                external_beacon_provenance_path.read_bytes(),
                location=str(external_beacon_provenance_path),
            )
        )
        if external_beacon.sampling_seed_sha256 != sha256_hex(seed_bytes):
            raise FrameFreezeV3Error("external beacon provenance does not bind supplied seed")
        if _parse_utc(external_beacon.obtained_at) < _parse_utc(population.manifest.frozen_at):
            raise FrameFreezeV3Error(
                "external randomness beacon predates eligible-population freeze"
            )
        if generated < _parse_utc(external_beacon.obtained_at):
            raise FrameFreezeV3Error("sampling-seed archive timestamp predates external beacon")
        external_beacon_binding = v1.ArtifactBinding(
            artifact=v1._relative_or_absolute(
                repo_root,
                external_beacon_provenance_path,
            ),
            sha256=hash_file(external_beacon_provenance_path),
        )
        source = "external_randomness_beacon_256"
    else:
        if not test_replay_only:
            raise FrameFreezeV3Error(
                "caller-supplied seed is test/replay-only unless a bound "
                "external beacon provenance is supplied"
            )
        source = "test_replay_seed_256"
    if len(seed_bytes) != 32:
        raise FrameFreezeV3Error("sampling seed must contain exactly 32 bytes")
    seed_sha = sha256_hex(seed_bytes)

    for other_lock_path in sorted((output_root / "seeds" / "by_population").glob("*.json")):
        other_lock = SeedLockV3.model_validate(
            _strict_json_object(
                other_lock_path.read_bytes(),
                location=str(other_lock_path),
            )
        )
        if other_lock.sampling_seed_sha256 == seed_sha:
            raise FrameFreezeV3Error("sampling seed is already assigned to another population")

    seed_path = output_root / "seeds" / f"{seed_sha}.bin"
    _write_immutable(seed_path, seed_bytes)
    seed_binding = v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, seed_path),
        sha256=seed_sha,
    )
    population_manifest_binding = v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, population_manifest_path),
        sha256=hash_file(population_manifest_path),
    )
    provenance_payload: dict[str, Any] = {
        "schema_version": 3,
        "source": source,
        "entropy_bits": 256,
        "generated_at": generated_at,
        "single_draw": True,
        "population_id": population.manifest.population_id,
        "population_manifest": population_manifest_binding.model_dump(mode="json"),
        "population_artifact": population.manifest.population_artifact.model_dump(mode="json"),
        "sampling_seed": seed_binding.model_dump(mode="json"),
        "sampling_seed_sha256": seed_sha,
        "external_beacon_provenance": (
            external_beacon_binding.model_dump(mode="json")
            if external_beacon_binding is not None
            else None
        ),
        "test_replay_only": test_replay_only,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    provenance_id = "lf021_sampling_seed_v3:" + hash_canonical(
        {"schema": "lf021_sampling_seed_provenance_v3", **provenance_payload}
    )
    provenance = SamplingSeedProvenanceV3.model_validate(
        {"provenance_id": provenance_id, **provenance_payload}
    )
    provenance_path = output_root / "seeds" / f"{provenance_id.rsplit(':', 1)[-1]}.provenance.json"
    _write_immutable(
        provenance_path,
        canonical_json_bytes(provenance.model_dump(mode="json")),
    )
    provenance_binding = v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, provenance_path),
        sha256=hash_file(provenance_path),
    )
    lock = SeedLockV3(
        population_id=population.manifest.population_id,
        sampling_seed_sha256=seed_sha,
        sampling_seed_provenance=provenance_binding,
    )
    _write_immutable(lock_path, canonical_json_bytes(lock.model_dump(mode="json")))
    return SeedRunV3(
        provenance=provenance,
        provenance_path=provenance_path,
        seed_path=seed_path,
        lock_path=lock_path,
    )


def archive_sampling_seed_v3(
    *,
    repo_root: Path,
    population_manifest_path: Path,
    output_root: Path,
    generated_at: str,
    seed_bytes: bytes | None = None,
    external_beacon_provenance_path: Path | None = None,
    test_replay_only: bool = False,
) -> SeedRunV3:
    """Serialize the single population-bound entropy draw across processes."""

    population = load_eligible_population_v3(
        repo_root=repo_root,
        manifest_path=population_manifest_path,
    )
    population_suffix = population.manifest.population_id.rsplit(":", 1)[-1]
    operation_lock_path = (
        output_root / "seeds" / "by_population" / f"{population_suffix}.operation.lock"
    )
    operation_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with operation_lock_path.open("a+b") as operation_lock:
        fcntl.flock(operation_lock.fileno(), fcntl.LOCK_EX)
        try:
            return _archive_sampling_seed_v3_locked(
                repo_root=repo_root,
                population_manifest_path=population_manifest_path,
                output_root=output_root,
                generated_at=generated_at,
                seed_bytes=seed_bytes,
                external_beacon_provenance_path=external_beacon_provenance_path,
                test_replay_only=test_replay_only,
            )
        finally:
            fcntl.flock(operation_lock.fileno(), fcntl.LOCK_UN)


def _rank_digest(
    *,
    seed: bytes,
    domain_separator: str,
    sampling_stratum: str,
    cluster_id: str,
) -> str:
    message = (
        domain_separator.encode("utf-8")
        + b"\x00"
        + sampling_stratum.encode("utf-8")
        + b"\x00"
        + cluster_id.encode("utf-8")
    )
    return hmac.new(seed, message, sha256).hexdigest()


def _frame_item(
    population_item: EligiblePopulationItemV3,
    *,
    population_manifest_id: str,
    population_manifest_binding: v1.ArtifactBinding,
    stratum_population_size: int,
    stratum_sample_size: int,
    rank_digest: str,
    policy: FrameFreezePolicyV3,
    seed_sha256: str,
    seed_provenance_binding: v1.ArtifactBinding,
    test_replay_only: bool,
) -> FrameItemV3:
    base = population_item.model_dump(
        mode="json",
        exclude={
            "schema_version",
            "same_claim",
            "relation",
            "semantic_labels_created",
            "supervision_eligible",
            "gate_5g_credit_claimed",
            "gate_5_closed",
        },
    )
    payload: dict[str, Any] = {
        "schema_version": 3,
        "population_manifest_id": population_manifest_id,
        "population_manifest": population_manifest_binding.model_dump(mode="json"),
        **base,
        "stratum_population_size": stratum_population_size,
        "stratum_sample_size": stratum_sample_size,
        "inclusion_probability_numerator": stratum_sample_size,
        "inclusion_probability_denominator": stratum_population_size,
        "sampling_method": policy.sampling_method,
        "sampling_rank_algorithm": policy.sampling_rank_algorithm,
        "sampling_rank_digest": rank_digest,
        "sampling_seed_sha256": seed_sha256,
        "sampling_seed_provenance": seed_provenance_binding.model_dump(mode="json"),
        "test_replay_only": test_replay_only,
        "same_claim": None,
        "relation": None,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    record_id = "lf021_prevalence_item_v3:" + hash_canonical(
        {"schema": "lf021_prevalence_frame_item_v3", **payload}
    )
    return FrameItemV3.model_validate({"frame_record_id": record_id, **payload})


def load_frame_items_v3(path: Path) -> tuple[FrameItemV3, ...]:
    """Strictly load v3 rows; fixed-salt revision-2 rows fail schema validation."""

    result: list[FrameItemV3] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), start=1):
        if not line.strip():
            continue
        result.append(
            FrameItemV3.model_validate(_strict_json_object(line, location=f"{path}:{line_number}"))
        )
    rows = tuple(result)
    if rows != tuple(sorted(rows, key=lambda item: item.frame_record_id)):
        raise FrameFreezeV3Error("v3 frame rows are not canonical-order")
    if len({item.frame_record_id for item in rows}) != len(rows):
        raise FrameFreezeV3Error("duplicate v3 frame record ID")
    return rows


def freeze_frame_v3(
    *,
    repo_root: Path,
    policy_path: Path,
    v2_decision_path: Path,
    population_manifest_path: Path,
    seed_provenance_path: Path,
    output_root: Path,
    allow_test_replay: bool = False,
) -> FrameRunV3:
    """Select and persist one randomized frame from an exact frozen population."""

    loaded = load_frame_freeze_policy_v3(policy_path)
    verified = load_verified_v2_stop(
        repo_root=repo_root,
        loaded_policy=loaded,
        decision_path=v2_decision_path,
    )
    population = load_eligible_population_v3(
        repo_root=repo_root,
        manifest_path=population_manifest_path,
    )
    if (
        population.manifest.v2_stop_decision_id != verified.decision.decision_id
        or population.manifest.v2_stop_decision != verified.decision_binding
        or population.manifest.policy_artifact
        != v1.ArtifactBinding(
            artifact=v1._relative_or_absolute(repo_root, loaded.path),
            sha256=hash_file(loaded.path),
        )
    ):
        raise FrameFreezeV3Error("population lineage differs from v3 inputs")
    expected_items = build_eligible_population_items_v3(verified)
    if population.items != expected_items:
        raise FrameFreezeV3Error("frozen population differs from replayed v2 population")

    provenance, seed = _load_seed_provenance(
        repo_root=repo_root,
        provenance_path=seed_provenance_path,
    )
    if provenance.test_replay_only and not allow_test_replay:
        raise FrameFreezeV3Error("test/replay sampling seed cannot freeze a production frame")
    population_manifest_binding = v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, population_manifest_path),
        sha256=hash_file(population_manifest_path),
    )
    if (
        provenance.population_id != population.manifest.population_id
        or provenance.population_manifest != population_manifest_binding
        or provenance.population_artifact != population.manifest.population_artifact
        or _parse_utc(provenance.generated_at) < _parse_utc(population.manifest.frozen_at)
    ):
        raise FrameFreezeV3Error("sampling seed provenance differs from population")
    seed_provenance_binding = v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, seed_provenance_path),
        sha256=hash_file(seed_provenance_path),
    )

    if verified.decision.frame is None:
        raise FrameFreezeV3Error("v2 stopping decision unexpectedly lacks frame target")
    target = verified.decision.frame.item_count
    expected_target = (
        verified.base_policy.frame.preferred_size
        if verified.decision.action is v1.DecisionAction.FREEZE_PREFERRED_FRAME
        else min(
            verified.base_policy.frame.preferred_size,
            verified.decision.counts.unique_compiling_count,
        )
    )
    if target != expected_target:
        raise FrameFreezeV3Error("v2 frame target differs from frozen base policy")

    by_stratum: dict[str, list[EligiblePopulationItemV3]] = defaultdict(list)
    for item in population.items:
        by_stratum[item.sampling_stratum].append(item)
    stratum_sizes = dict(sorted((key, len(value)) for key, value in by_stratum.items()))
    allocation = (
        stratum_sizes
        if target == len(population.items)
        else v1._allocate_strata(
            stratum_sizes,
            target=target,
            minimum_per_stratum=(verified.base_policy.frame.minimum_per_nonempty_stratum),
        )
    )

    selected: list[FrameItemV3] = []
    for stratum in sorted(by_stratum):
        ranked = sorted(
            by_stratum[stratum],
            key=lambda item: (
                _rank_digest(
                    seed=seed,
                    domain_separator=loaded.config.sampling_domain_separator,
                    sampling_stratum=stratum,
                    cluster_id=item.cluster_id,
                ),
                item.cluster_id,
            ),
        )
        for item in ranked[: allocation[stratum]]:
            digest = _rank_digest(
                seed=seed,
                domain_separator=loaded.config.sampling_domain_separator,
                sampling_stratum=stratum,
                cluster_id=item.cluster_id,
            )
            selected.append(
                _frame_item(
                    item,
                    population_manifest_id=population.manifest.population_id,
                    population_manifest_binding=population_manifest_binding,
                    stratum_population_size=stratum_sizes[stratum],
                    stratum_sample_size=allocation[stratum],
                    rank_digest=digest,
                    policy=loaded.config,
                    seed_sha256=provenance.sampling_seed_sha256,
                    seed_provenance_binding=seed_provenance_binding,
                    test_replay_only=provenance.test_replay_only,
                )
            )
    items = tuple(sorted(selected, key=lambda item: item.frame_record_id))
    if len(items) != target:
        raise FrameFreezeV3Error("randomized frame size differs from target")
    frame_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in items
    )
    frame_sha = sha256_hex(frame_bytes)
    frame_path = output_root / "frames" / f"{frame_sha}.jsonl"
    frame_payload: dict[str, Any] = {
        "artifact": v1._relative_or_absolute(repo_root, frame_path),
        "sha256": frame_sha,
        "item_count": len(items),
        "population_id": population.manifest.population_id,
        "population_manifest": population_manifest_binding.model_dump(mode="json"),
        "sampling_method": loaded.config.sampling_method,
        "sampling_seed_sha256": provenance.sampling_seed_sha256,
        "sampling_seed_provenance": seed_provenance_binding.model_dump(mode="json"),
        "test_replay_only": provenance.test_replay_only,
    }
    frame_id = "lf021_prevalence_frame_v3:" + hash_canonical(
        {"schema": "lf021_prevalence_frame_binding_v3", **frame_payload}
    )
    frame = FrameBindingV3.model_validate({"frame_id": frame_id, **frame_payload})
    implementation_path = Path(__file__).resolve()
    decision_payload: dict[str, Any] = {
        "schema_version": 3,
        "policy_id": loaded.config.policy_id,
        "policy_artifact": {
            "artifact": v1._relative_or_absolute(repo_root, loaded.path),
            "sha256": hash_file(loaded.path),
        },
        "implementation_artifact": {
            "artifact": v1._relative_or_absolute(repo_root, implementation_path),
            "sha256": hash_file(implementation_path),
        },
        "v2_stop_decision_id": verified.decision.decision_id,
        "v2_stop_decision": verified.decision_binding.model_dump(mode="json"),
        "observations": tuple(
            item.model_dump(mode="json") for item in verified.decision.observations
        ),
        "counts": verified.decision.counts.model_dump(mode="json"),
        "coverage_deficits": verified.decision.coverage_deficits,
        "action": verified.decision.action.value,
        "next_tranche": None,
        "v2_stop_action": verified.decision.action.value,
        "v2_fixed_salt_sampling_method": _V2_FIXED_SALT_METHOD,
        "v2_fixed_salt_frame_reused": False,
        "population_id": population.manifest.population_id,
        "population_manifest": population_manifest_binding.model_dump(mode="json"),
        "population_artifact": population.manifest.population_artifact.model_dump(mode="json"),
        "population_item_count": population.manifest.population_item_count,
        "population_member_count": population.manifest.population_member_count,
        "stratum_population_sizes": stratum_sizes,
        "stratum_sample_sizes": allocation,
        "sampling_method": loaded.config.sampling_method,
        "sampling_rank_algorithm": loaded.config.sampling_rank_algorithm,
        "sampling_domain_separator": loaded.config.sampling_domain_separator,
        "sampling_rank_message_encoding": (loaded.config.sampling_rank_message_encoding),
        "sampling_seed_sha256": provenance.sampling_seed_sha256,
        "sampling_seed_provenance": seed_provenance_binding.model_dump(mode="json"),
        "test_replay_only": provenance.test_replay_only,
        "frame": frame.model_dump(mode="json"),
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    decision_id = "lf021_frame_freeze_decision_v3:" + hash_canonical(
        {"schema": "lf021_frame_freeze_decision_v3", **decision_payload}
    )
    decision = FrameFreezeDecisionV3.model_validate(
        {"decision_id": decision_id, **decision_payload}
    )
    decision_path = output_root / "decisions" / f"{decision_id.rsplit(':', 1)[-1]}.json"
    _write_immutable(frame_path, frame_bytes)
    _write_immutable(
        decision_path,
        canonical_json_bytes(decision.model_dump(mode="json")),
    )
    loaded_frame = load_frame_items_v3(frame_path)
    if loaded_frame != items or hash_file(frame_path) != frame.sha256:
        raise FrameFreezeV3Error("persisted randomized frame differs from decision")
    return FrameRunV3(
        decision=decision,
        decision_path=decision_path,
        frame_path=frame_path,
        items=items,
    )


def verify_frame_freeze_v3(
    *,
    repo_root: Path,
    policy_path: Path,
    decision_path: Path,
) -> VerifiedFrameFreezeV3:
    """Replay every v3 lineage, entropy, allocation, and row binding.

    This is the canonical read-only contract for Gate finalization and the
    prevalence estimator.  It deliberately accepts a test/replay frame but
    exposes ``test_replay_only`` through both the decision and seed provenance;
    production consumers must reject that flag.
    """

    loaded = load_frame_freeze_policy_v3(policy_path)
    decision = FrameFreezeDecisionV3.model_validate(
        _strict_json_object(decision_path.read_bytes(), location=str(decision_path))
    )
    decision_binding = _binding(repo_root, decision_path)
    expected_policy_binding = _binding(repo_root, loaded.path)
    if decision.policy_id != loaded.config.policy_id:
        raise FrameFreezeV3Error("frame decision policy ID differs from supplied policy")
    if decision.policy_artifact != expected_policy_binding:
        raise FrameFreezeV3Error("frame decision policy artifact differs from supplied policy")
    implementation_path = Path(__file__).resolve()
    if decision.implementation_artifact != _binding(repo_root, implementation_path):
        raise FrameFreezeV3Error("frame decision implementation artifact differs")

    bound_v2_decision_path = _verify(repo_root, decision.v2_stop_decision)
    verified = load_verified_v2_stop(
        repo_root=repo_root,
        loaded_policy=loaded,
        decision_path=bound_v2_decision_path,
    )
    if (
        decision.v2_stop_decision_id != verified.decision.decision_id
        or decision.v2_stop_decision != verified.decision_binding
        or decision.observations != verified.decision.observations
        or decision.counts != verified.decision.counts
        or decision.coverage_deficits != verified.decision.coverage_deficits
        or decision.action != verified.decision.action.value
        or decision.v2_stop_action != verified.decision.action.value
        or decision.next_tranche is not None
    ):
        raise FrameFreezeV3Error("frame decision projection differs from verified v2 stop")
    if (
        decision.v2_fixed_salt_sampling_method != _V2_FIXED_SALT_METHOD
        or decision.v2_fixed_salt_frame_reused
    ):
        raise FrameFreezeV3Error("historical fixed-salt v2 frame was reused")

    population_manifest_path = _verify(repo_root, decision.population_manifest)
    population = load_eligible_population_v3(
        repo_root=repo_root,
        manifest_path=population_manifest_path,
    )
    population_manifest_binding = _binding(repo_root, population_manifest_path)
    expected_population_items = build_eligible_population_items_v3(verified)
    if (
        population.manifest.policy_artifact != expected_policy_binding
        or population.manifest.v2_stop_decision_id != verified.decision.decision_id
        or population.manifest.v2_stop_decision != verified.decision_binding
        or population.manifest.population_id != decision.population_id
        or population.manifest.population_artifact != decision.population_artifact
        or population.manifest.population_item_count != decision.population_item_count
        or population.manifest.population_member_count != decision.population_member_count
        or population.manifest.stratum_population_sizes != decision.stratum_population_sizes
        or population.items != expected_population_items
        or population_manifest_binding != decision.population_manifest
    ):
        raise FrameFreezeV3Error("frame decision population binding does not replay")

    seed_provenance_path = _verify(repo_root, decision.sampling_seed_provenance)
    provenance, seed = _load_seed_provenance(
        repo_root=repo_root,
        provenance_path=seed_provenance_path,
    )
    population_suffix = population.manifest.population_id.rsplit(":", 1)[-1]
    seed_lock_path = seed_provenance_path.parent / "by_population" / f"{population_suffix}.json"
    if not seed_lock_path.is_file():
        raise FrameFreezeV3Error("population-bound sampling-seed lock is missing")
    seed_lock = SeedLockV3.model_validate(
        _strict_json_object(seed_lock_path.read_bytes(), location=str(seed_lock_path))
    )
    if provenance.external_beacon_provenance is not None:
        beacon_path = _verify(repo_root, provenance.external_beacon_provenance)
        beacon = ExternalRandomnessBeaconProvenanceV3.model_validate(
            _strict_json_object(
                beacon_path.read_bytes(),
                location=str(beacon_path),
            )
        )
        if _parse_utc(beacon.obtained_at) < _parse_utc(population.manifest.frozen_at) or _parse_utc(
            provenance.generated_at
        ) < _parse_utc(beacon.obtained_at):
            raise FrameFreezeV3Error(
                "external randomness beacon timing differs from frozen population"
            )
    if (
        provenance.population_id != population.manifest.population_id
        or provenance.population_manifest != population_manifest_binding
        or provenance.population_artifact != population.manifest.population_artifact
        or provenance.sampling_seed_sha256 != decision.sampling_seed_sha256
        or provenance.test_replay_only != decision.test_replay_only
        or _parse_utc(provenance.generated_at) < _parse_utc(population.manifest.frozen_at)
        or seed_lock.population_id != population.manifest.population_id
        or seed_lock.sampling_seed_sha256 != provenance.sampling_seed_sha256
        or seed_lock.sampling_seed_provenance != _binding(repo_root, seed_provenance_path)
    ):
        raise FrameFreezeV3Error("frame decision sampling-seed binding does not replay")

    if (
        decision.sampling_method != loaded.config.sampling_method
        or decision.sampling_rank_algorithm != loaded.config.sampling_rank_algorithm
        or decision.sampling_domain_separator != loaded.config.sampling_domain_separator
        or decision.sampling_rank_message_encoding != loaded.config.sampling_rank_message_encoding
    ):
        raise FrameFreezeV3Error("frame decision sampling algorithm differs from policy")
    if verified.decision.frame is None:
        raise FrameFreezeV3Error("verified v2 stop unexpectedly lacks a frame target")
    target = verified.decision.frame.item_count
    if decision.frame.item_count != target:
        raise FrameFreezeV3Error("v3 frame target differs from verified v2 stop")

    by_stratum: dict[str, list[EligiblePopulationItemV3]] = defaultdict(list)
    for item in population.items:
        by_stratum[item.sampling_stratum].append(item)
    stratum_sizes = dict(sorted((stratum, len(items)) for stratum, items in by_stratum.items()))
    allocation = (
        stratum_sizes
        if target == len(population.items)
        else v1._allocate_strata(
            stratum_sizes,
            target=target,
            minimum_per_stratum=(verified.base_policy.frame.minimum_per_nonempty_stratum),
        )
    )
    if (
        stratum_sizes != decision.stratum_population_sizes
        or allocation != decision.stratum_sample_sizes
    ):
        raise FrameFreezeV3Error("frame stratum population or allocation does not replay")

    expected_frame_items: list[FrameItemV3] = []
    for stratum in sorted(by_stratum):
        ranked = sorted(
            by_stratum[stratum],
            key=lambda item: (
                _rank_digest(
                    seed=seed,
                    domain_separator=loaded.config.sampling_domain_separator,
                    sampling_stratum=stratum,
                    cluster_id=item.cluster_id,
                ),
                item.cluster_id,
            ),
        )
        for item in ranked[: allocation[stratum]]:
            expected_frame_items.append(
                _frame_item(
                    item,
                    population_manifest_id=population.manifest.population_id,
                    population_manifest_binding=population_manifest_binding,
                    stratum_population_size=stratum_sizes[stratum],
                    stratum_sample_size=allocation[stratum],
                    rank_digest=_rank_digest(
                        seed=seed,
                        domain_separator=loaded.config.sampling_domain_separator,
                        sampling_stratum=stratum,
                        cluster_id=item.cluster_id,
                    ),
                    policy=loaded.config,
                    seed_sha256=provenance.sampling_seed_sha256,
                    seed_provenance_binding=_binding(
                        repo_root,
                        seed_provenance_path,
                    ),
                    test_replay_only=provenance.test_replay_only,
                )
            )
    expected_items = tuple(sorted(expected_frame_items, key=lambda item: item.frame_record_id))

    frame_path = _verify(
        repo_root,
        v1.ArtifactBinding(
            artifact=decision.frame.artifact,
            sha256=decision.frame.sha256,
        ),
    )
    frame_items = load_frame_items_v3(frame_path)
    if (
        frame_items != expected_items
        or len(frame_items) != decision.frame.item_count
        or decision.frame.population_id != population.manifest.population_id
        or decision.frame.population_manifest != population_manifest_binding
        or decision.frame.sampling_method != loaded.config.sampling_method
        or decision.frame.sampling_seed_sha256 != provenance.sampling_seed_sha256
        or decision.frame.sampling_seed_provenance != _binding(repo_root, seed_provenance_path)
        or decision.frame.test_replay_only != provenance.test_replay_only
    ):
        raise FrameFreezeV3Error("randomized v3 frame does not replay exactly")

    return VerifiedFrameFreezeV3(
        decision=decision,
        decision_path=decision_path,
        decision_binding=decision_binding,
        verified_v2_stop=verified,
        population=population,
        seed_provenance=provenance,
        seed_provenance_path=seed_provenance_path,
        seed_bytes=seed,
        seed_lock=seed_lock,
        seed_lock_path=seed_lock_path,
        frame_path=frame_path,
        frame_items=frame_items,
    )


__all__ = [
    "EligiblePopulationItemV3",
    "EligiblePopulationManifestV3",
    "ExternalRandomnessBeaconProvenanceV3",
    "FrameBindingV3",
    "FrameFreezeDecisionV3",
    "FrameFreezePolicyV3",
    "FrameFreezeV3Error",
    "FrameItemV3",
    "FrameRunV3",
    "PopulationMemberV3",
    "PopulationRunV3",
    "SamplingSeedProvenanceV3",
    "SeedRunV3",
    "VerifiedFrameFreezeV3",
    "archive_sampling_seed_v3",
    "build_eligible_population_items_v3",
    "freeze_eligible_population_v3",
    "freeze_frame_v3",
    "load_eligible_population_v3",
    "load_frame_freeze_policy_v3",
    "load_frame_items_v3",
    "load_verified_v2_stop",
    "verify_frame_freeze_v3",
]
