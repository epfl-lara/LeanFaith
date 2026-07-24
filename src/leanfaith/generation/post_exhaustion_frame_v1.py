"""Truthful extended population/frame materialization after LF-021 exhaustion.

The scientific population rows reuse ``EligiblePopulationItemV3`` because the
strict extension handoff already proves that exact projection.  Population,
entropy, frame, and decision containers have distinct post-exhaustion IDs and
schemas; no v2 stop or v3 decision is fabricated.
"""

from __future__ import annotations

import fcntl
import json
import secrets
from collections import Counter, defaultdict
from dataclasses import dataclass
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
from leanfaith.evaluation.prevalence_design_v3 import (
    load_prevalence_design_policy_v3,
    verify_prevalence_design_policy_v3,
)
from leanfaith.generation import frame_freeze_v3 as frame_v3
from leanfaith.generation import gate5g as gate5g_v1
from leanfaith.generation import post_exhaustion_collection_v1 as collection_v1
from leanfaith.generation import post_exhaustion_extension as extension
from leanfaith.generation import tranche_expansion as v1

_HEX64 = r"^[0-9a-f]{64}$"
_POPULATION_ID = r"^lf021_extended_eligible_population_v1:[0-9a-f]{64}$"
_SEED_ID = r"^lf021_extended_sampling_seed_v1:[0-9a-f]{64}$"
_FRAME_ITEM_ID = r"^lf021_extended_prevalence_item_v1:[0-9a-f]{64}$"
_FRAME_ID = r"^lf021_extended_prevalence_frame_v1:[0-9a-f]{64}$"
_DECISION_ID = r"^lf021_extended_frame_freeze_decision_v1:[0-9a-f]{64}$"
_EXPECTED_FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)


class PostExhaustionFrameError(RuntimeError):
    """Extended population, entropy, authorization, or frame failed closed."""


class PostExhaustionFramePolicyV1(StrictModel):
    """Frozen adapter over exact extension, sampling, and prevalence policies."""

    schema_version: Literal[1] = 1
    policy_id: Literal["lf021_post_exhaustion_frame_materializer_v1"]
    status: Literal["frozen_prelabel"]
    extension_policy: v1.ArtifactBinding
    extension_implementation: v1.ArtifactBinding
    collection_authorization_policy: v1.ArtifactBinding
    collection_authorization_implementation: v1.ArtifactBinding
    frame_v3_policy: v1.ArtifactBinding
    frame_v3_implementation: v1.ArtifactBinding
    safe_publication_implementation: v1.ArtifactBinding
    prevalence_design_v3: v1.ArtifactBinding
    prevalence_design_v3_implementation: v1.ArtifactBinding
    required_original_observation_count: Literal[12]
    required_extension_orders: tuple[Literal[12], Literal[13], Literal[14], Literal[15]]
    required_scalable_family_ids: tuple[str, str, str]
    target_frame_size: Literal[240]
    minimum_per_nonempty_stratum: int = Field(ge=1)
    population_unit: tuple[str, str]
    population_record_schema: Literal["EligiblePopulationItemV3"]
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_rank_algorithm: Literal["hmac_sha256_keyed_rank_v1"]
    sampling_domain_separator: str = Field(min_length=16)
    sampling_rank_message_encoding: Literal["utf8_domain_nul_stratum_nul_cluster_id_v1"]
    sampling_seed_bytes: Literal[32]
    sampling_seed_generation: Literal["single_draw_after_population_freeze"]
    original_seed_registry_root: Literal[
        "reports/generation/lf021_frame_freeze_v3/seeds/by_population"
    ]
    extended_seed_registry_root: Literal[
        "reports/generation/lf021_post_exhaustion_frame_v1/seeds/by_population"
    ]
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.required_extension_orders != (12, 13, 14, 15):
            raise ValueError("extended-frame order inventory differs")
        if self.required_scalable_family_ids != _EXPECTED_FAMILIES:
            raise ValueError("extended-frame family inventory differs")
        if self.population_unit != (
            "problem_group",
            "alpha_identity_fingerprint",
        ):
            raise ValueError("extended-frame scientific unit differs")
        return self


class ExtendedEligiblePopulationManifestV1(StrictModel):
    """Complete extended population frozen durably before entropy."""

    schema_version: Literal[1] = 1
    manifest_kind: Literal["lf021_extended_eligible_population_v1"]
    population_id: str = Field(pattern=_POPULATION_ID)
    policy_id: Literal["lf021_post_exhaustion_frame_materializer_v1"]
    policy_artifact: v1.ArtifactBinding
    implementation_artifact: v1.ArtifactBinding
    extension_stop_decision_id: str = Field(min_length=1)
    extension_stop_decision: v1.ArtifactBinding
    extension_policy: v1.ArtifactBinding
    extension_implementation: v1.ArtifactBinding
    activation_v2_decision_id: str = Field(min_length=1)
    activation_v2_decision: v1.ArtifactBinding
    original_observation_count: Literal[12]
    extension_observation_count: int = Field(ge=1, le=4)
    observations: tuple[v1.ObservationBinding, ...] = Field(min_length=13, max_length=16)
    handoff_projection_id: str = Field(min_length=1)
    handoff_projection_sha256: str = Field(pattern=_HEX64)
    collection_authorizations: tuple[v1.ArtifactBinding, ...] = Field(min_length=1, max_length=4)
    collection_authorization_ids: tuple[str, ...] = Field(min_length=1, max_length=4)
    population_record_schema: Literal["EligiblePopulationItemV3"]
    population_unit: tuple[str, str]
    population_artifact: v1.ArtifactBinding
    population_item_count: int = Field(ge=240)
    population_member_count: int = Field(ge=240)
    representative_family_ids: tuple[str, str, str]
    stratum_population_sizes: dict[str, int] = Field(min_length=3)
    counts: v1.OperationalCounts
    coverage_deficits: tuple[()] = ()
    frozen_at: str = Field(min_length=1)
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        frame_v3._parse_utc(self.frozen_at)
        if self.population_unit != (
            "problem_group",
            "alpha_identity_fingerprint",
        ):
            raise ValueError("extended population unit differs")
        if (
            self.extension_observation_count != len(self.collection_authorizations)
            or self.extension_observation_count != len(self.collection_authorization_ids)
            or len(self.observations)
            != self.original_observation_count + self.extension_observation_count
        ):
            raise ValueError("extended population lineage lengths differ")
        if len(set(self.collection_authorization_ids)) != len(self.collection_authorization_ids):
            raise ValueError("collection authorization IDs are not unique")
        if self.representative_family_ids != _EXPECTED_FAMILIES:
            raise ValueError("all three representative families are required")
        if any(value <= 0 for value in self.stratum_population_sizes.values()):
            raise ValueError("extended population strata must be nonempty")
        if sum(self.stratum_population_sizes.values()) != self.population_item_count:
            raise ValueError("extended population strata do not reconcile")
        if (
            self.counts.unique_compiling_count != self.population_item_count
            or self.counts.benchmark_clear_compile_count != self.population_member_count
        ):
            raise ValueError("extended population counts do not reconcile")
        expected = "lf021_extended_eligible_population_v1:" + hash_canonical(
            {
                "schema": "lf021_extended_eligible_population_manifest_v1",
                **self.model_dump(mode="json", exclude={"population_id"}),
            }
        )
        if self.population_id != expected:
            raise ValueError("extended population ID differs from content")
        return self


class ExtendedSamplingSeedProvenanceV1(StrictModel):
    """One population-bound 256-bit seed drawn after extended population freeze."""

    schema_version: Literal[1] = 1
    provenance_id: str = Field(pattern=_SEED_ID)
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
        frame_v3._parse_utc(self.generated_at)
        if self.sampling_seed.sha256 != self.sampling_seed_sha256:
            raise ValueError("extended seed artifact hash differs")
        if self.source == "external_randomness_beacon_256":
            if self.external_beacon_provenance is None or self.test_replay_only:
                raise ValueError("production beacon provenance is required")
        elif self.external_beacon_provenance is not None:
            raise ValueError("non-beacon seed carries beacon provenance")
        if (self.source == "test_replay_seed_256") != self.test_replay_only:
            raise ValueError("extended test seed scope differs")
        expected = "lf021_extended_sampling_seed_v1:" + hash_canonical(
            {
                "schema": "lf021_extended_sampling_seed_provenance_v1",
                **self.model_dump(mode="json", exclude={"provenance_id"}),
            }
        )
        if self.provenance_id != expected:
            raise ValueError("extended seed provenance ID differs")
        return self


class ExtendedSeedLockV1(StrictModel):
    schema_version: Literal[1] = 1
    population_id: str = Field(pattern=_POPULATION_ID)
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    sampling_seed_provenance: v1.ArtifactBinding


class ExtendedFrameItemV1(StrictModel):
    """One sampled extended-population unit with exact inclusion probability."""

    schema_version: Literal[1] = 1
    frame_record_id: str = Field(pattern=_FRAME_ITEM_ID)
    population_manifest_id: str = Field(pattern=_POPULATION_ID)
    population_manifest: v1.ArtifactBinding
    population_item: frame_v3.EligiblePopulationItemV3
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
        if self.sampling_stratum != self.population_item.sampling_stratum:
            raise ValueError("extended frame stratum differs from population item")
        if self.stratum_sample_size > self.stratum_population_size:
            raise ValueError("extended stratum sample exceeds population")
        if (
            self.inclusion_probability_numerator != self.stratum_sample_size
            or self.inclusion_probability_denominator != self.stratum_population_size
        ):
            raise ValueError("extended inclusion probability differs from n_h/N_h")
        expected = "lf021_extended_prevalence_item_v1:" + hash_canonical(
            {
                "schema": "lf021_extended_prevalence_frame_item_v1",
                **self.model_dump(mode="json", exclude={"frame_record_id"}),
            }
        )
        if self.frame_record_id != expected:
            raise ValueError("extended frame item ID differs from content")
        return self


class ExtendedFrameBindingV1(StrictModel):
    frame_id: str = Field(pattern=_FRAME_ID)
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    item_count: Literal[240]
    population_id: str = Field(pattern=_POPULATION_ID)
    population_manifest: v1.ArtifactBinding
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    sampling_seed_provenance: v1.ArtifactBinding
    test_replay_only: bool

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected = "lf021_extended_prevalence_frame_v1:" + hash_canonical(
            {
                "schema": "lf021_extended_prevalence_frame_binding_v1",
                **self.model_dump(mode="json", exclude={"frame_id"}),
            }
        )
        if self.frame_id != expected:
            raise ValueError("extended frame ID differs from content")
        return self


class ExtendedFrameFreezeDecisionV1(StrictModel):
    """Truthful extended stop, population, entropy, and frame binding."""

    schema_version: Literal[1] = 1
    decision_id: str = Field(pattern=_DECISION_ID)
    policy_id: Literal["lf021_post_exhaustion_frame_materializer_v1"]
    policy_artifact: v1.ArtifactBinding
    implementation_artifact: v1.ArtifactBinding
    source_stop_action: Literal["preferred_eligible_stop"]
    action: Literal["freeze_preferred_frame"]
    next_tranche: None = None
    extension_stop_decision_id: str = Field(min_length=1)
    extension_stop_decision: v1.ArtifactBinding
    extension_policy: v1.ArtifactBinding
    extension_implementation: v1.ArtifactBinding
    activation_v2_decision_id: str = Field(min_length=1)
    activation_v2_decision: v1.ArtifactBinding
    original_observation_count: Literal[12]
    extension_observation_count: int = Field(ge=1, le=4)
    observations: tuple[v1.ObservationBinding, ...] = Field(min_length=13, max_length=16)
    collection_authorizations: tuple[v1.ArtifactBinding, ...] = Field(min_length=1, max_length=4)
    collection_authorization_ids: tuple[str, ...] = Field(min_length=1, max_length=4)
    counts: v1.OperationalCounts
    coverage_deficits: tuple[()] = ()
    population_id: str = Field(pattern=_POPULATION_ID)
    population_manifest: v1.ArtifactBinding
    population_artifact: v1.ArtifactBinding
    population_item_count: int = Field(ge=240)
    population_member_count: int = Field(ge=240)
    representative_family_ids: tuple[str, str, str]
    stratum_population_sizes: dict[str, int] = Field(min_length=3)
    stratum_sample_sizes: dict[str, int] = Field(min_length=3)
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_rank_algorithm: Literal["hmac_sha256_keyed_rank_v1"]
    sampling_domain_separator: str = Field(min_length=16)
    sampling_rank_message_encoding: Literal["utf8_domain_nul_stratum_nul_cluster_id_v1"]
    sampling_seed_sha256: str = Field(pattern=_HEX64)
    sampling_seed_provenance: v1.ArtifactBinding
    test_replay_only: bool
    frame: ExtendedFrameBindingV1
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (
            self.extension_observation_count != len(self.collection_authorizations)
            or self.extension_observation_count != len(self.collection_authorization_ids)
            or len(self.observations)
            != self.original_observation_count + self.extension_observation_count
        ):
            raise ValueError("extended frame lineage lengths differ")
        if self.representative_family_ids != _EXPECTED_FAMILIES:
            raise ValueError("extended frame lacks a representative family")
        if (
            self.counts.unique_compiling_count != self.population_item_count
            or self.counts.benchmark_clear_compile_count != self.population_member_count
        ):
            raise ValueError("extended frame population counts differ")
        if set(self.stratum_sample_sizes) != set(self.stratum_population_sizes):
            raise ValueError("extended sample/population strata differ")
        if sum(self.stratum_population_sizes.values()) != self.population_item_count:
            raise ValueError("extended population strata do not reconcile")
        if sum(self.stratum_sample_sizes.values()) != self.frame.item_count:
            raise ValueError("extended sample strata do not reconcile")
        if (
            self.frame.population_id != self.population_id
            or self.frame.population_manifest != self.population_manifest
            or self.frame.sampling_method != self.sampling_method
            or self.frame.sampling_seed_sha256 != self.sampling_seed_sha256
            or self.frame.sampling_seed_provenance != self.sampling_seed_provenance
            or self.frame.test_replay_only != self.test_replay_only
        ):
            raise ValueError("extended frame binding differs from decision")
        expected = "lf021_extended_frame_freeze_decision_v1:" + hash_canonical(
            {
                "schema": "lf021_extended_frame_freeze_decision_v1",
                **self.model_dump(mode="json", exclude={"decision_id"}),
            }
        )
        if self.decision_id != expected:
            raise ValueError("extended frame decision ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class ExtendedPopulationRunV1:
    manifest: ExtendedEligiblePopulationManifestV1
    manifest_path: Path
    population_path: Path
    items: tuple[frame_v3.EligiblePopulationItemV3, ...]


@dataclass(frozen=True, slots=True)
class ExtendedSeedRunV1:
    provenance: ExtendedSamplingSeedProvenanceV1
    provenance_path: Path
    seed_path: Path
    lock_path: Path


@dataclass(frozen=True, slots=True)
class ExtendedFrameRunV1:
    decision: ExtendedFrameFreezeDecisionV1
    decision_path: Path
    frame_path: Path
    items: tuple[ExtendedFrameItemV1, ...]


@dataclass(frozen=True, slots=True)
class VerifiedExtendedFrameV1:
    decision: ExtendedFrameFreezeDecisionV1
    decision_path: Path
    decision_binding: v1.ArtifactBinding
    verified_stop: extension.VerifiedExtendedStopForFrameV3
    collection_authorizations: collection_v1.VerifiedExtensionCollectionAuthorizationsV1
    population: ExtendedPopulationRunV1
    seed_provenance: ExtendedSamplingSeedProvenanceV1
    seed_provenance_path: Path
    seed_bytes: bytes
    seed_lock: ExtendedSeedLockV1
    seed_lock_path: Path
    frame_path: Path
    frame_items: tuple[ExtendedFrameItemV1, ...]


def _resolve(repo_root: Path, artifact: str) -> Path:
    path = Path(artifact)
    resolved = (path if path.is_absolute() else repo_root / path).resolve()
    if not path.is_absolute() and not resolved.is_relative_to(repo_root.resolve()):
        raise PostExhaustionFrameError(f"artifact escapes repository: {artifact}")
    return resolved


def _binding(repo_root: Path, path: Path) -> v1.ArtifactBinding:
    return v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, path),
        sha256=hash_file(path),
    )


def _verify(repo_root: Path, binding: v1.ArtifactBinding) -> Path:
    path = _resolve(repo_root, binding.artifact)
    if not path.is_file() or hash_file(path) != binding.sha256:
        raise PostExhaustionFrameError(f"bound artifact differs: {binding.artifact}")
    return path


def _strict_json(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    value = extension._strict_json_object(payload, location=str(path))
    if canonical_json_bytes(value) != payload:
        raise PostExhaustionFrameError(f"JSON is not canonical: {path}")
    return value


def _strict_jsonl_object(
    payload: bytes,
    *,
    location: str,
) -> dict[str, Any]:
    value = extension._strict_json_object(payload, location=location)
    if canonical_json_bytes(value) != payload:
        raise PostExhaustionFrameError(f"JSONL row is not canonical: {location}")
    return value


def _write_immutable(
    *,
    repo_root: Path,
    path: Path,
    payload: bytes,
    label: str,
) -> None:
    try:
        gate5g_v1._write_immutable(
            path,
            payload,
            repo_root=repo_root,
            label=label,
        )
    except gate5g_v1.Gate5GFinalizationError as exc:
        raise PostExhaustionFrameError(str(exc)) from exc


def load_post_exhaustion_frame_policy_v1(
    path: Path,
) -> LoadedConfig[PostExhaustionFramePolicyV1]:
    return load_config(path, PostExhaustionFramePolicyV1)


def _verify_policy_lineage(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PostExhaustionFramePolicyV1],
) -> tuple[
    LoadedConfig[extension.PostExhaustionExtensionPolicyV1],
    LoadedConfig[frame_v3.FrameFreezePolicyV3],
]:
    policy = loaded_policy.config
    extension_policy_path = _verify(repo_root, policy.extension_policy)
    extension_implementation_path = _verify(repo_root, policy.extension_implementation)
    collection_policy_path = _verify(
        repo_root,
        policy.collection_authorization_policy,
    )
    collection_implementation_path = _verify(
        repo_root,
        policy.collection_authorization_implementation,
    )
    frame_policy_path = _verify(repo_root, policy.frame_v3_policy)
    frame_implementation_path = _verify(repo_root, policy.frame_v3_implementation)
    safe_publication_path = _verify(
        repo_root,
        policy.safe_publication_implementation,
    )
    prevalence_path = _verify(repo_root, policy.prevalence_design_v3)
    prevalence_implementation_path = _verify(
        repo_root,
        policy.prevalence_design_v3_implementation,
    )
    if (
        extension_implementation_path.resolve() != Path(extension.__file__).resolve()
        or collection_implementation_path.resolve() != Path(collection_v1.__file__).resolve()
        or frame_implementation_path.resolve() != Path(frame_v3.__file__).resolve()
        or safe_publication_path.resolve() != Path(gate5g_v1.__file__).resolve()
    ):
        raise PostExhaustionFrameError("bound implementation is not imported code")
    from leanfaith.evaluation import prevalence_design_v3

    if prevalence_implementation_path.resolve() != Path(prevalence_design_v3.__file__).resolve():
        raise PostExhaustionFrameError("bound prevalence v3 implementation is not imported code")
    loaded_extension = extension.load_post_exhaustion_extension_policy(extension_policy_path)
    loaded_frame = frame_v3.load_frame_freeze_policy_v3(frame_policy_path)
    loaded_collection = collection_v1.load_post_exhaustion_collection_policy_v1(
        collection_policy_path
    )
    loaded_prevalence = load_prevalence_design_policy_v3(prevalence_path)
    verify_prevalence_design_policy_v3(
        repo_root=repo_root,
        loaded_policy=loaded_prevalence,
    )
    loaded_base = v1.load_tranche_expansion_policy(
        _verify(repo_root, loaded_extension.config.base_v1_policy)
    )
    base = loaded_extension.config
    if (
        base.original_tranche_count != policy.required_original_observation_count
        or tuple(item.order for item in base.extension_tranches) != policy.required_extension_orders
        or base.required_families != policy.required_scalable_family_ids
        or loaded_collection.config.extension_policy != policy.extension_policy
        or loaded_collection.config.extension_implementation != policy.extension_implementation
        or loaded_frame.config.sampling_method != policy.sampling_method
        or loaded_frame.config.sampling_rank_algorithm != policy.sampling_rank_algorithm
        or loaded_frame.config.sampling_domain_separator != policy.sampling_domain_separator
        or loaded_frame.config.sampling_rank_message_encoding
        != policy.sampling_rank_message_encoding
        or loaded_prevalence.config.target_population.sampling_method != policy.sampling_method
        or loaded_prevalence.config.target_population.sampling_rank_algorithm
        != policy.sampling_rank_algorithm
        or loaded_prevalence.config.scope.required_scalable_families
        != policy.required_scalable_family_ids
        or policy.minimum_per_nonempty_stratum
        != loaded_base.config.frame.minimum_per_nonempty_stratum
    ):
        raise PostExhaustionFrameError("post-exhaustion policy lineage differs")
    return loaded_extension, loaded_frame


def _verified_stop_and_authorizations(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PostExhaustionFramePolicyV1],
    extension_decision_path: Path,
    collection_authorization_paths: tuple[Path, ...],
) -> tuple[
    extension.VerifiedExtendedStopForFrameV3,
    collection_v1.VerifiedExtensionCollectionAuthorizationsV1,
]:
    verified = extension.verify_extended_stop_for_frame_v3(
        repo_root=repo_root,
        policy_path=_verify(repo_root, loaded_policy.config.extension_policy),
        decision_path=extension_decision_path,
    )
    authorizations = collection_v1.verify_extension_collection_authorizations_v1(
        repo_root=repo_root,
        policy_path=_verify(
            repo_root,
            loaded_policy.config.collection_authorization_policy,
        ),
        extension_stop_decision_path=extension_decision_path,
        authorization_paths=collection_authorization_paths,
    )
    if (
        len(authorizations.records) != len(verified.decision.extension_observations)
        or authorizations.postprocess_observations != verified.decision.extension_observations
    ):
        raise PostExhaustionFrameError(
            "reviewed collection authorizations differ from extended stop"
        )
    return verified, authorizations


def _population_bytes(
    items: tuple[frame_v3.EligiblePopulationItemV3, ...],
) -> bytes:
    return b"".join(canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in items)


def freeze_extended_eligible_population_v1(
    *,
    repo_root: Path,
    policy_path: Path,
    extension_decision_path: Path,
    collection_authorization_paths: tuple[Path, ...],
    output_root: Path,
    frozen_at: str,
) -> ExtendedPopulationRunV1:
    """Freeze the exact strict-handoff population before requesting entropy."""

    frame_v3._parse_utc(frozen_at)
    loaded = load_post_exhaustion_frame_policy_v1(policy_path)
    _verify_policy_lineage(repo_root=repo_root, loaded_policy=loaded)
    verified, authorizations = _verified_stop_and_authorizations(
        repo_root=repo_root,
        loaded_policy=loaded,
        extension_decision_path=extension_decision_path,
        collection_authorization_paths=collection_authorization_paths,
    )
    items = verified.population_items
    representative_families = tuple(sorted({item.representative_family_id for item in items}))
    if representative_families != loaded.config.required_scalable_family_ids:
        raise PostExhaustionFrameError(
            "preferred population lacks all three representative-family strata"
        )
    population_bytes = _population_bytes(items)
    if sha256_hex(population_bytes) != verified.handoff_projection.population_items_sha256:
        raise PostExhaustionFrameError(
            "strict handoff population bytes differ before materialization"
        )
    population_sha = sha256_hex(population_bytes)
    population_path = output_root / "populations" / f"{population_sha}.jsonl"
    population_binding = v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, population_path),
        sha256=population_sha,
    )
    original = verified.verified_original_exhaustion
    observations = original.decision.observations + verified.decision.extension_observations
    projection_bytes = canonical_json_bytes(verified.handoff_projection.model_dump(mode="json"))
    payload: dict[str, Any] = {
        "schema_version": 1,
        "manifest_kind": "lf021_extended_eligible_population_v1",
        "policy_id": loaded.config.policy_id,
        "policy_artifact": _binding(repo_root, loaded.path).model_dump(mode="json"),
        "implementation_artifact": _binding(
            repo_root,
            Path(__file__).resolve(),
        ).model_dump(mode="json"),
        "extension_stop_decision_id": verified.decision.decision_id,
        "extension_stop_decision": verified.decision_binding.model_dump(mode="json"),
        "extension_policy": loaded.config.extension_policy.model_dump(mode="json"),
        "extension_implementation": loaded.config.extension_implementation.model_dump(mode="json"),
        "activation_v2_decision_id": original.decision.decision_id,
        "activation_v2_decision": original.decision_binding.model_dump(mode="json"),
        "original_observation_count": len(original.decision.observations),
        "extension_observation_count": len(verified.decision.extension_observations),
        "observations": tuple(item.model_dump(mode="json") for item in observations),
        "handoff_projection_id": verified.handoff_projection.projection_id,
        "handoff_projection_sha256": sha256_hex(projection_bytes),
        "collection_authorizations": tuple(
            item.model_dump(mode="json") for item in authorizations.bindings
        ),
        "collection_authorization_ids": tuple(
            item.authorization_id for item in authorizations.records
        ),
        "population_record_schema": "EligiblePopulationItemV3",
        "population_unit": loaded.config.population_unit,
        "population_artifact": population_binding.model_dump(mode="json"),
        "population_item_count": len(items),
        "population_member_count": sum(item.member_count for item in items),
        "representative_family_ids": representative_families,
        "stratum_population_sizes": dict(
            sorted(Counter(item.sampling_stratum for item in items).items())
        ),
        "counts": verified.decision.counts.model_dump(mode="json"),
        "coverage_deficits": (),
        "frozen_at": frozen_at,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    population_id = "lf021_extended_eligible_population_v1:" + hash_canonical(
        {
            "schema": "lf021_extended_eligible_population_manifest_v1",
            **payload,
        }
    )
    manifest = ExtendedEligiblePopulationManifestV1.model_validate(
        {"population_id": population_id, **payload}
    )
    manifest_path = (
        output_root / "populations" / f"{population_id.rsplit(':', 1)[-1]}.manifest.json"
    )
    _write_immutable(
        repo_root=repo_root,
        path=population_path,
        payload=population_bytes,
        label="extended eligible population",
    )
    _write_immutable(
        repo_root=repo_root,
        path=manifest_path,
        payload=canonical_json_bytes(manifest.model_dump(mode="json")),
        label="extended eligible population manifest",
    )
    if (
        not population_path.is_file()
        or not manifest_path.is_file()
        or hash_file(population_path) != population_sha
    ):
        raise PostExhaustionFrameError("extended population was not durable before entropy")
    return ExtendedPopulationRunV1(
        manifest=manifest,
        manifest_path=manifest_path,
        population_path=population_path,
        items=items,
    )


def load_extended_eligible_population_v1(
    *,
    repo_root: Path,
    manifest_path: Path,
) -> ExtendedPopulationRunV1:
    manifest = ExtendedEligiblePopulationManifestV1.model_validate(_strict_json(manifest_path))
    population_path = _verify(repo_root, manifest.population_artifact)
    rows: list[frame_v3.EligiblePopulationItemV3] = []
    for line_number, line in enumerate(
        population_path.read_bytes().splitlines(),
        start=1,
    ):
        if line.strip():
            rows.append(
                frame_v3.EligiblePopulationItemV3.model_validate(
                    _strict_jsonl_object(
                        line,
                        location=f"{population_path}:{line_number}",
                    )
                )
            )
    items = tuple(rows)
    if items != tuple(sorted(items, key=lambda item: item.population_record_id)):
        raise PostExhaustionFrameError("extended population rows are not canonical-order")
    if len({item.population_record_id for item in items}) != len(items):
        raise PostExhaustionFrameError("duplicate extended population item")
    if (
        len(items) != manifest.population_item_count
        or sum(item.member_count for item in items) != manifest.population_member_count
        or dict(sorted(Counter(item.sampling_stratum for item in items).items()))
        != manifest.stratum_population_sizes
        or tuple(sorted({item.representative_family_id for item in items}))
        != manifest.representative_family_ids
    ):
        raise PostExhaustionFrameError("extended population totals differ")
    return ExtendedPopulationRunV1(
        manifest=manifest,
        manifest_path=manifest_path,
        population_path=population_path,
        items=items,
    )


def _load_seed_provenance(
    *,
    repo_root: Path,
    path: Path,
) -> tuple[ExtendedSamplingSeedProvenanceV1, bytes]:
    provenance = ExtendedSamplingSeedProvenanceV1.model_validate(_strict_json(path))
    seed_path = _verify(repo_root, provenance.sampling_seed)
    seed = seed_path.read_bytes()
    if len(seed) != 32 or sha256_hex(seed) != provenance.sampling_seed_sha256:
        raise PostExhaustionFrameError("extended seed is not exact 256-bit content")
    if provenance.external_beacon_provenance is not None:
        beacon_path = _verify(repo_root, provenance.external_beacon_provenance)
        beacon = frame_v3.ExternalRandomnessBeaconProvenanceV3.model_validate(
            _strict_json(beacon_path)
        )
        if beacon.sampling_seed_sha256 != provenance.sampling_seed_sha256:
            raise PostExhaustionFrameError("extended beacon differs from archived seed")
    return provenance, seed


def _seed_hashes_in_registry(path: Path) -> set[str]:
    result: set[str] = set()
    if not path.is_dir():
        return result
    for lock_path in sorted(path.glob("*.json")):
        try:
            raw = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PostExhaustionFrameError(
                f"invalid seed-registry lock {lock_path}: {exc}"
            ) from exc
        digest = raw.get("sampling_seed_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise PostExhaustionFrameError(f"invalid seed digest in registry {lock_path}")
        result.add(digest)
    return result


def _archive_seed_locked(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PostExhaustionFramePolicyV1],
    population_manifest_path: Path,
    output_root: Path,
    generated_at: str,
    seed_bytes: bytes | None,
    external_beacon_provenance_path: Path | None,
    test_replay_only: bool,
) -> ExtendedSeedRunV1:
    population = load_extended_eligible_population_v1(
        repo_root=repo_root,
        manifest_path=population_manifest_path,
    )
    generated = frame_v3._parse_utc(generated_at)
    if generated < frame_v3._parse_utc(population.manifest.frozen_at):
        raise PostExhaustionFrameError("seed timestamp predates extended population freeze")
    suffix = population.manifest.population_id.rsplit(":", 1)[-1]
    lock_path = output_root / "seeds" / "by_population" / f"{suffix}.json"
    if lock_path.exists():
        lock = ExtendedSeedLockV1.model_validate(_strict_json(lock_path))
        provenance_path = _verify(repo_root, lock.sampling_seed_provenance)
        provenance, existing_seed = _load_seed_provenance(
            repo_root=repo_root,
            path=provenance_path,
        )
        if seed_bytes is not None and seed_bytes != existing_seed:
            raise PostExhaustionFrameError(
                "extended population already has a different frozen seed"
            )
        return ExtendedSeedRunV1(
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
    beacon_binding: v1.ArtifactBinding | None = None
    if seed_bytes is None:
        if external_beacon_provenance_path is not None or test_replay_only:
            raise PostExhaustionFrameError("OS CSPRNG draw cannot carry beacon/test inputs")
        seed_bytes = secrets.token_bytes(32)
        source = "os_csprng_secrets_token_bytes_256"
    elif external_beacon_provenance_path is not None:
        if test_replay_only:
            raise PostExhaustionFrameError("external beacon cannot be test-only")
        beacon = frame_v3.ExternalRandomnessBeaconProvenanceV3.model_validate(
            _strict_json(external_beacon_provenance_path)
        )
        if (
            len(seed_bytes) != 32
            or beacon.sampling_seed_sha256 != sha256_hex(seed_bytes)
            or frame_v3._parse_utc(beacon.obtained_at)
            < frame_v3._parse_utc(population.manifest.frozen_at)
            or generated < frame_v3._parse_utc(beacon.obtained_at)
        ):
            raise PostExhaustionFrameError("beacon does not postdate and bind extended population")
        beacon_binding = _binding(repo_root, external_beacon_provenance_path)
        source = "external_randomness_beacon_256"
    else:
        if not test_replay_only:
            raise PostExhaustionFrameError("caller seed is test-only without beacon provenance")
        source = "test_replay_seed_256"
    if len(seed_bytes) != 32:
        raise PostExhaustionFrameError("extended sampling seed must be 32 bytes")
    seed_sha = sha256_hex(seed_bytes)
    registries = (
        repo_root / loaded_policy.config.original_seed_registry_root,
        output_root / "seeds" / "by_population",
    )
    if any(seed_sha in _seed_hashes_in_registry(path) for path in registries):
        raise PostExhaustionFrameError("sampling seed is already assigned to a population")
    seed_path = output_root / "seeds" / f"{seed_sha}.bin"
    _write_immutable(
        repo_root=repo_root,
        path=seed_path,
        payload=seed_bytes,
        label="extended sampling seed",
    )
    seed_binding = _binding(repo_root, seed_path)
    manifest_binding = _binding(repo_root, population_manifest_path)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "source": source,
        "entropy_bits": 256,
        "generated_at": generated_at,
        "single_draw": True,
        "population_id": population.manifest.population_id,
        "population_manifest": manifest_binding.model_dump(mode="json"),
        "population_artifact": population.manifest.population_artifact.model_dump(mode="json"),
        "sampling_seed": seed_binding.model_dump(mode="json"),
        "sampling_seed_sha256": seed_sha,
        "external_beacon_provenance": (
            beacon_binding.model_dump(mode="json") if beacon_binding is not None else None
        ),
        "test_replay_only": test_replay_only,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    provenance_id = "lf021_extended_sampling_seed_v1:" + hash_canonical(
        {
            "schema": "lf021_extended_sampling_seed_provenance_v1",
            **payload,
        }
    )
    provenance = ExtendedSamplingSeedProvenanceV1.model_validate(
        {"provenance_id": provenance_id, **payload}
    )
    provenance_path = output_root / "seeds" / f"{provenance_id.rsplit(':', 1)[-1]}.provenance.json"
    _write_immutable(
        repo_root=repo_root,
        path=provenance_path,
        payload=canonical_json_bytes(provenance.model_dump(mode="json")),
        label="extended sampling-seed provenance",
    )
    lock = ExtendedSeedLockV1(
        population_id=population.manifest.population_id,
        sampling_seed_sha256=seed_sha,
        sampling_seed_provenance=_binding(repo_root, provenance_path),
    )
    _write_immutable(
        repo_root=repo_root,
        path=lock_path,
        payload=canonical_json_bytes(lock.model_dump(mode="json")),
        label="extended population seed lock",
    )
    return ExtendedSeedRunV1(
        provenance=provenance,
        provenance_path=provenance_path,
        seed_path=seed_path,
        lock_path=lock_path,
    )


def archive_extended_sampling_seed_v1(
    *,
    repo_root: Path,
    policy_path: Path,
    population_manifest_path: Path,
    output_root: Path,
    generated_at: str,
    seed_bytes: bytes | None = None,
    external_beacon_provenance_path: Path | None = None,
    test_replay_only: bool = False,
) -> ExtendedSeedRunV1:
    """Serialize exactly one seed draw for the durable extended population."""

    loaded = load_post_exhaustion_frame_policy_v1(policy_path)
    _verify_policy_lineage(repo_root=repo_root, loaded_policy=loaded)
    population = load_extended_eligible_population_v1(
        repo_root=repo_root,
        manifest_path=population_manifest_path,
    )
    suffix = population.manifest.population_id.rsplit(":", 1)[-1]
    operation_lock_path = output_root / "seeds" / "by_population" / f"{suffix}.operation.lock"
    operation_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with operation_lock_path.open("a+b") as operation_lock:
        fcntl.flock(operation_lock.fileno(), fcntl.LOCK_EX)
        try:
            if (
                not population.population_path.is_file()
                or not population.manifest_path.is_file()
                or hash_file(population.population_path)
                != population.manifest.population_artifact.sha256
            ):
                raise PostExhaustionFrameError("extended population is not durable before entropy")
            return _archive_seed_locked(
                repo_root=repo_root,
                loaded_policy=loaded,
                population_manifest_path=population_manifest_path,
                output_root=output_root,
                generated_at=generated_at,
                seed_bytes=seed_bytes,
                external_beacon_provenance_path=external_beacon_provenance_path,
                test_replay_only=test_replay_only,
            )
        finally:
            fcntl.flock(operation_lock.fileno(), fcntl.LOCK_UN)


def _allocation(
    *,
    items: tuple[frame_v3.EligiblePopulationItemV3, ...],
    policy: PostExhaustionFramePolicyV1,
) -> tuple[
    dict[str, list[frame_v3.EligiblePopulationItemV3]],
    dict[str, int],
    dict[str, int],
]:
    by_stratum: dict[str, list[frame_v3.EligiblePopulationItemV3]] = defaultdict(list)
    for item in items:
        by_stratum[item.sampling_stratum].append(item)
    sizes = dict(sorted((key, len(value)) for key, value in by_stratum.items()))
    allocation = (
        sizes
        if len(items) == policy.target_frame_size
        else v1._allocate_strata(
            sizes,
            target=policy.target_frame_size,
            minimum_per_stratum=policy.minimum_per_nonempty_stratum,
        )
    )
    return by_stratum, sizes, allocation


def _frame_item(
    item: frame_v3.EligiblePopulationItemV3,
    *,
    manifest: v1.ArtifactBinding,
    population_id: str,
    population_size: int,
    sample_size: int,
    rank_digest: str,
    policy: PostExhaustionFramePolicyV1,
    provenance: v1.ArtifactBinding,
    seed_sha256: str,
    test_replay_only: bool,
) -> ExtendedFrameItemV1:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "population_manifest_id": population_id,
        "population_manifest": manifest.model_dump(mode="json"),
        "population_item": item.model_dump(mode="json"),
        "sampling_stratum": item.sampling_stratum,
        "stratum_population_size": population_size,
        "stratum_sample_size": sample_size,
        "inclusion_probability_numerator": sample_size,
        "inclusion_probability_denominator": population_size,
        "sampling_method": policy.sampling_method,
        "sampling_rank_algorithm": policy.sampling_rank_algorithm,
        "sampling_rank_digest": rank_digest,
        "sampling_seed_sha256": seed_sha256,
        "sampling_seed_provenance": provenance.model_dump(mode="json"),
        "test_replay_only": test_replay_only,
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
    record_id = "lf021_extended_prevalence_item_v1:" + hash_canonical(
        {"schema": "lf021_extended_prevalence_frame_item_v1", **payload}
    )
    return ExtendedFrameItemV1.model_validate({"frame_record_id": record_id, **payload})


def load_extended_frame_items_v1(path: Path) -> tuple[ExtendedFrameItemV1, ...]:
    rows: list[ExtendedFrameItemV1] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), start=1):
        if line.strip():
            rows.append(
                ExtendedFrameItemV1.model_validate(
                    _strict_jsonl_object(
                        line,
                        location=f"{path}:{line_number}",
                    )
                )
            )
    result = tuple(rows)
    if result != tuple(sorted(result, key=lambda item: item.frame_record_id)):
        raise PostExhaustionFrameError("extended frame rows are not canonical-order")
    if len({item.frame_record_id for item in result}) != len(result):
        raise PostExhaustionFrameError("duplicate extended frame item")
    return result


def freeze_extended_frame_v1(
    *,
    repo_root: Path,
    policy_path: Path,
    extension_decision_path: Path,
    collection_authorization_paths: tuple[Path, ...],
    population_manifest_path: Path,
    seed_provenance_path: Path,
    output_root: Path,
    allow_test_replay: bool = False,
) -> ExtendedFrameRunV1:
    """Select a 240-unit HMAC-ranked frame from truthful extended lineage."""

    loaded = load_post_exhaustion_frame_policy_v1(policy_path)
    _verify_policy_lineage(repo_root=repo_root, loaded_policy=loaded)
    verified, authorizations = _verified_stop_and_authorizations(
        repo_root=repo_root,
        loaded_policy=loaded,
        extension_decision_path=extension_decision_path,
        collection_authorization_paths=collection_authorization_paths,
    )
    population = load_extended_eligible_population_v1(
        repo_root=repo_root,
        manifest_path=population_manifest_path,
    )
    if (
        population.manifest.policy_artifact != _binding(repo_root, loaded.path)
        or population.manifest.implementation_artifact
        != _binding(repo_root, Path(__file__).resolve())
        or population.manifest.extension_stop_decision != verified.decision_binding
        or population.manifest.collection_authorizations != authorizations.bindings
        or population.items != verified.population_items
    ):
        raise PostExhaustionFrameError("extended population differs from replay")
    provenance, seed = _load_seed_provenance(
        repo_root=repo_root,
        path=seed_provenance_path,
    )
    if provenance.test_replay_only and not allow_test_replay:
        raise PostExhaustionFrameError("test/replay seed cannot freeze a production extended frame")
    manifest_binding = _binding(repo_root, population_manifest_path)
    if (
        provenance.population_id != population.manifest.population_id
        or provenance.population_manifest != manifest_binding
        or provenance.population_artifact != population.manifest.population_artifact
        or frame_v3._parse_utc(provenance.generated_at)
        < frame_v3._parse_utc(population.manifest.frozen_at)
    ):
        raise PostExhaustionFrameError("extended seed differs from population")
    provenance_binding = _binding(repo_root, seed_provenance_path)
    by_stratum, sizes, allocation = _allocation(
        items=population.items,
        policy=loaded.config,
    )
    selected: list[ExtendedFrameItemV1] = []
    for stratum in sorted(by_stratum):
        ranked = sorted(
            by_stratum[stratum],
            key=lambda item: (
                frame_v3._rank_digest(
                    seed=seed,
                    domain_separator=loaded.config.sampling_domain_separator,
                    sampling_stratum=stratum,
                    cluster_id=item.cluster_id,
                ),
                item.cluster_id,
            ),
        )
        for item in ranked[: allocation[stratum]]:
            digest = frame_v3._rank_digest(
                seed=seed,
                domain_separator=loaded.config.sampling_domain_separator,
                sampling_stratum=stratum,
                cluster_id=item.cluster_id,
            )
            selected.append(
                _frame_item(
                    item,
                    manifest=manifest_binding,
                    population_id=population.manifest.population_id,
                    population_size=sizes[stratum],
                    sample_size=allocation[stratum],
                    rank_digest=digest,
                    policy=loaded.config,
                    provenance=provenance_binding,
                    seed_sha256=provenance.sampling_seed_sha256,
                    test_replay_only=provenance.test_replay_only,
                )
            )
    items = tuple(sorted(selected, key=lambda item: item.frame_record_id))
    if len(items) != loaded.config.target_frame_size:
        raise PostExhaustionFrameError("extended randomized frame size differs")
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
        "population_manifest": manifest_binding.model_dump(mode="json"),
        "sampling_method": loaded.config.sampling_method,
        "sampling_seed_sha256": provenance.sampling_seed_sha256,
        "sampling_seed_provenance": provenance_binding.model_dump(mode="json"),
        "test_replay_only": provenance.test_replay_only,
    }
    frame_id = "lf021_extended_prevalence_frame_v1:" + hash_canonical(
        {"schema": "lf021_extended_prevalence_frame_binding_v1", **frame_payload}
    )
    frame = ExtendedFrameBindingV1.model_validate({"frame_id": frame_id, **frame_payload})
    original = verified.verified_original_exhaustion
    observations = original.decision.observations + verified.decision.extension_observations
    payload: dict[str, Any] = {
        "schema_version": 1,
        "policy_id": loaded.config.policy_id,
        "policy_artifact": _binding(repo_root, loaded.path).model_dump(mode="json"),
        "implementation_artifact": _binding(
            repo_root,
            Path(__file__).resolve(),
        ).model_dump(mode="json"),
        "source_stop_action": "preferred_eligible_stop",
        "action": "freeze_preferred_frame",
        "next_tranche": None,
        "extension_stop_decision_id": verified.decision.decision_id,
        "extension_stop_decision": verified.decision_binding.model_dump(mode="json"),
        "extension_policy": loaded.config.extension_policy.model_dump(mode="json"),
        "extension_implementation": loaded.config.extension_implementation.model_dump(mode="json"),
        "activation_v2_decision_id": original.decision.decision_id,
        "activation_v2_decision": original.decision_binding.model_dump(mode="json"),
        "original_observation_count": len(original.decision.observations),
        "extension_observation_count": len(verified.decision.extension_observations),
        "observations": tuple(item.model_dump(mode="json") for item in observations),
        "collection_authorizations": tuple(
            item.model_dump(mode="json") for item in authorizations.bindings
        ),
        "collection_authorization_ids": tuple(
            item.authorization_id for item in authorizations.records
        ),
        "counts": verified.decision.counts.model_dump(mode="json"),
        "coverage_deficits": (),
        "population_id": population.manifest.population_id,
        "population_manifest": manifest_binding.model_dump(mode="json"),
        "population_artifact": population.manifest.population_artifact.model_dump(mode="json"),
        "population_item_count": population.manifest.population_item_count,
        "population_member_count": population.manifest.population_member_count,
        "representative_family_ids": population.manifest.representative_family_ids,
        "stratum_population_sizes": sizes,
        "stratum_sample_sizes": allocation,
        "sampling_method": loaded.config.sampling_method,
        "sampling_rank_algorithm": loaded.config.sampling_rank_algorithm,
        "sampling_domain_separator": loaded.config.sampling_domain_separator,
        "sampling_rank_message_encoding": (loaded.config.sampling_rank_message_encoding),
        "sampling_seed_sha256": provenance.sampling_seed_sha256,
        "sampling_seed_provenance": provenance_binding.model_dump(mode="json"),
        "test_replay_only": provenance.test_replay_only,
        "frame": frame.model_dump(mode="json"),
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    decision_id = "lf021_extended_frame_freeze_decision_v1:" + hash_canonical(
        {"schema": "lf021_extended_frame_freeze_decision_v1", **payload}
    )
    decision = ExtendedFrameFreezeDecisionV1.model_validate({"decision_id": decision_id, **payload})
    decision_path = output_root / "decisions" / f"{decision_id.rsplit(':', 1)[-1]}.json"
    _write_immutable(
        repo_root=repo_root,
        path=frame_path,
        payload=frame_bytes,
        label="extended prevalence frame",
    )
    _write_immutable(
        repo_root=repo_root,
        path=decision_path,
        payload=canonical_json_bytes(decision.model_dump(mode="json")),
        label="extended frame-freeze decision",
    )
    if load_extended_frame_items_v1(frame_path) != items:
        raise PostExhaustionFrameError("persisted extended frame differs")
    return ExtendedFrameRunV1(
        decision=decision,
        decision_path=decision_path,
        frame_path=frame_path,
        items=items,
    )


def verify_extended_frame_freeze_v1(
    *,
    repo_root: Path,
    policy_path: Path,
    decision_path: Path,
) -> VerifiedExtendedFrameV1:
    """Replay every extended stop, authorization, population, seed, and row."""

    loaded = load_post_exhaustion_frame_policy_v1(policy_path)
    _verify_policy_lineage(repo_root=repo_root, loaded_policy=loaded)
    decision = ExtendedFrameFreezeDecisionV1.model_validate(_strict_json(decision_path))
    if decision.policy_artifact != _binding(
        repo_root, loaded.path
    ) or decision.implementation_artifact != _binding(repo_root, Path(__file__).resolve()):
        raise PostExhaustionFrameError("extended frame code/policy binding differs")
    extension_path = _verify(repo_root, decision.extension_stop_decision)
    authorization_paths = tuple(
        _verify(repo_root, item) for item in decision.collection_authorizations
    )
    verified, authorizations = _verified_stop_and_authorizations(
        repo_root=repo_root,
        loaded_policy=loaded,
        extension_decision_path=extension_path,
        collection_authorization_paths=authorization_paths,
    )
    original = verified.verified_original_exhaustion
    expected_observations = (
        original.decision.observations + verified.decision.extension_observations
    )
    if (
        decision.source_stop_action != "preferred_eligible_stop"
        or decision.action != "freeze_preferred_frame"
        or decision.next_tranche is not None
        or decision.extension_stop_decision_id != verified.decision.decision_id
        or decision.extension_stop_decision != verified.decision_binding
        or decision.extension_policy != loaded.config.extension_policy
        or decision.extension_implementation != loaded.config.extension_implementation
        or decision.activation_v2_decision_id != original.decision.decision_id
        or decision.activation_v2_decision != original.decision_binding
        or decision.observations != expected_observations
        or decision.collection_authorizations != authorizations.bindings
        or decision.collection_authorization_ids
        != tuple(item.authorization_id for item in authorizations.records)
        or decision.counts != verified.decision.counts
        or decision.coverage_deficits
    ):
        raise PostExhaustionFrameError("extended frame stop projection differs")
    population_manifest_path = _verify(repo_root, decision.population_manifest)
    population = load_extended_eligible_population_v1(
        repo_root=repo_root,
        manifest_path=population_manifest_path,
    )
    manifest_binding = _binding(repo_root, population_manifest_path)
    if (
        population.manifest.extension_stop_decision != verified.decision_binding
        or population.manifest.activation_v2_decision != original.decision_binding
        or population.manifest.observations != expected_observations
        or population.manifest.handoff_projection_id != verified.handoff_projection.projection_id
        or population.manifest.handoff_projection_sha256
        != sha256_hex(canonical_json_bytes(verified.handoff_projection.model_dump(mode="json")))
        or population.manifest.collection_authorizations != authorizations.bindings
        or population.items != verified.population_items
        or population.manifest.population_id != decision.population_id
        or population.manifest.population_artifact != decision.population_artifact
        or population.manifest.population_item_count != decision.population_item_count
        or population.manifest.population_member_count != decision.population_member_count
        or population.manifest.representative_family_ids != decision.representative_family_ids
        or population.manifest.stratum_population_sizes != decision.stratum_population_sizes
        or manifest_binding != decision.population_manifest
    ):
        raise PostExhaustionFrameError("extended population replay differs")
    provenance_path = _verify(repo_root, decision.sampling_seed_provenance)
    provenance, seed = _load_seed_provenance(
        repo_root=repo_root,
        path=provenance_path,
    )
    suffix = population.manifest.population_id.rsplit(":", 1)[-1]
    lock_path = provenance_path.parent / "by_population" / f"{suffix}.json"
    if not lock_path.is_file():
        raise PostExhaustionFrameError("extended seed lock is missing")
    lock = ExtendedSeedLockV1.model_validate(_strict_json(lock_path))
    provenance_binding = _binding(repo_root, provenance_path)
    if (
        provenance.population_id != population.manifest.population_id
        or provenance.population_manifest != manifest_binding
        or provenance.population_artifact != population.manifest.population_artifact
        or provenance.sampling_seed_sha256 != decision.sampling_seed_sha256
        or provenance.test_replay_only != decision.test_replay_only
        or frame_v3._parse_utc(provenance.generated_at)
        < frame_v3._parse_utc(population.manifest.frozen_at)
        or lock.population_id != population.manifest.population_id
        or lock.sampling_seed_sha256 != provenance.sampling_seed_sha256
        or lock.sampling_seed_provenance != provenance_binding
    ):
        raise PostExhaustionFrameError("extended seed replay differs")
    if provenance.external_beacon_provenance is not None:
        beacon = frame_v3.ExternalRandomnessBeaconProvenanceV3.model_validate(
            _strict_json(_verify(repo_root, provenance.external_beacon_provenance))
        )
        if frame_v3._parse_utc(beacon.obtained_at) < frame_v3._parse_utc(
            population.manifest.frozen_at
        ) or frame_v3._parse_utc(provenance.generated_at) < frame_v3._parse_utc(beacon.obtained_at):
            raise PostExhaustionFrameError("extended beacon timing differs")
    if (
        decision.sampling_method != loaded.config.sampling_method
        or decision.sampling_rank_algorithm != loaded.config.sampling_rank_algorithm
        or decision.sampling_domain_separator != loaded.config.sampling_domain_separator
        or decision.sampling_rank_message_encoding != loaded.config.sampling_rank_message_encoding
    ):
        raise PostExhaustionFrameError("extended sampling policy differs")
    by_stratum, sizes, allocation = _allocation(
        items=population.items,
        policy=loaded.config,
    )
    if sizes != decision.stratum_population_sizes or allocation != decision.stratum_sample_sizes:
        raise PostExhaustionFrameError("extended allocation replay differs")
    expected: list[ExtendedFrameItemV1] = []
    for stratum in sorted(by_stratum):
        ranked = sorted(
            by_stratum[stratum],
            key=lambda item: (
                frame_v3._rank_digest(
                    seed=seed,
                    domain_separator=loaded.config.sampling_domain_separator,
                    sampling_stratum=stratum,
                    cluster_id=item.cluster_id,
                ),
                item.cluster_id,
            ),
        )
        for item in ranked[: allocation[stratum]]:
            expected.append(
                _frame_item(
                    item,
                    manifest=manifest_binding,
                    population_id=population.manifest.population_id,
                    population_size=sizes[stratum],
                    sample_size=allocation[stratum],
                    rank_digest=frame_v3._rank_digest(
                        seed=seed,
                        domain_separator=loaded.config.sampling_domain_separator,
                        sampling_stratum=stratum,
                        cluster_id=item.cluster_id,
                    ),
                    policy=loaded.config,
                    provenance=provenance_binding,
                    seed_sha256=provenance.sampling_seed_sha256,
                    test_replay_only=provenance.test_replay_only,
                )
            )
    expected_items = tuple(sorted(expected, key=lambda item: item.frame_record_id))
    frame_path = _verify(
        repo_root,
        v1.ArtifactBinding(
            artifact=decision.frame.artifact,
            sha256=decision.frame.sha256,
        ),
    )
    frame_items = load_extended_frame_items_v1(frame_path)
    if (
        frame_items != expected_items
        or decision.frame.item_count != loaded.config.target_frame_size
        or decision.frame.population_id != population.manifest.population_id
        or decision.frame.population_manifest != manifest_binding
        or decision.frame.sampling_seed_sha256 != provenance.sampling_seed_sha256
        or decision.frame.sampling_seed_provenance != provenance_binding
        or decision.frame.test_replay_only != provenance.test_replay_only
    ):
        raise PostExhaustionFrameError("extended frame replay differs")
    return VerifiedExtendedFrameV1(
        decision=decision,
        decision_path=decision_path,
        decision_binding=_binding(repo_root, decision_path),
        verified_stop=verified,
        collection_authorizations=authorizations,
        population=population,
        seed_provenance=provenance,
        seed_provenance_path=provenance_path,
        seed_bytes=seed,
        seed_lock=lock,
        seed_lock_path=lock_path,
        frame_path=frame_path,
        frame_items=frame_items,
    )


__all__ = [
    "ExtendedEligiblePopulationManifestV1",
    "ExtendedFrameBindingV1",
    "ExtendedFrameFreezeDecisionV1",
    "ExtendedFrameItemV1",
    "ExtendedFrameRunV1",
    "ExtendedPopulationRunV1",
    "ExtendedSamplingSeedProvenanceV1",
    "ExtendedSeedLockV1",
    "ExtendedSeedRunV1",
    "PostExhaustionFrameError",
    "PostExhaustionFramePolicyV1",
    "VerifiedExtendedFrameV1",
    "archive_extended_sampling_seed_v1",
    "freeze_extended_eligible_population_v1",
    "freeze_extended_frame_v1",
    "load_extended_eligible_population_v1",
    "load_extended_frame_items_v1",
    "load_post_exhaustion_frame_policy_v1",
    "verify_extended_frame_freeze_v1",
]
