"""Label-blind LF-021 tranche extension after the frozen sequence exhausts.

The extension cannot activate before the exact twelve-tranche v2 prefix ends
without preferred-frame eligibility. It reuses the bound problem-aware
identity and compilation-only coverage rules, emits collection authorization
only, and never creates a prevalence frame or semantic label.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation import frame_freeze_v3 as frame_v3
from leanfaith.generation import tranche_expansion as v1
from leanfaith.generation import tranche_expansion_v2 as v2

_DECISION_ID = r"^lf021_post_exhaustion_extension_decision_v1:[0-9a-f]{64}$"
_HANDOFF_PROJECTION_ID = r"^lf021_post_exhaustion_population_handoff_v1:[0-9a-f]{64}$"
_HEX64 = r"^[0-9a-f]{64}$"
_ORIGINAL_TRANCHE_COUNT = 12
_EXPECTED_FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)
_EXPECTED_EXTENSION = (
    (
        12,
        "algebra_s6",
        "algebra_gate3_docstrings_v1",
        (36, 6, 6),
        40,
        120,
    ),
    (
        13,
        "cross_domain_s6",
        "cross_domain_docstrings_v1",
        (36, 6, 6),
        20,
        60,
    ),
    (
        14,
        "algebra_s7",
        "algebra_gate3_docstrings_v1",
        (37, 7, 7),
        40,
        120,
    ),
    (
        15,
        "cross_domain_s7",
        "cross_domain_docstrings_v1",
        (37, 7, 7),
        20,
        60,
    ),
)
_EXPECTED_DECISION_INPUTS = (
    "alpha_identity_fingerprint",
    "benchmark_screen",
    "compile_status",
    "family_id",
    "parse_status",
    "pool_id",
    "problem_group",
    "source_proxy",
)
_EXPECTED_FORBIDDEN_INPUTS = (
    "faithfulness_judgment",
    "human_label",
    "llm_judgment",
    "proof_search_result",
    "relation",
    "same_claim",
)


class PostExhaustionExtensionError(RuntimeError):
    """The frozen extension policy, activation, or prefix failed closed."""


class FamilyPinV1(StrictModel):
    family_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    transport: Literal["local"]


class PoolPinV1(StrictModel):
    pool_id: str = Field(min_length=1)
    problem_count: int = Field(ge=1)
    records: v1.ArtifactBinding
    manifest: v1.ArtifactBinding


class ForecastMotivationV1(StrictModel):
    observed_unique_units_after_s2: Literal[96]
    observed_incremental_unique_yields: tuple[int, int, int, int, int, int]
    preferred_size: Literal[240]
    unresolved_coverage_proxy: Literal["Algebra/Category"]
    operational_motivation_only: Literal[True] = True
    statistical_guarantee: Literal[False] = False

    @model_validator(mode="after")
    def _frozen(self) -> Self:
        if self.observed_incremental_unique_yields != (23, 7, 22, 4, 25, 15):
            raise ValueError("forecast motivation must preserve observed tranche yields")
        if sum(self.observed_incremental_unique_yields) != 96:
            raise ValueError("forecast motivation yields do not reconcile")
        return self


class PostExhaustionExtensionPolicyV1(StrictModel):
    """Immutable collection-only extension over exact v1/v2/v3 bytes."""

    schema_version: Literal[1] = 1
    policy_id: Literal["lf021_post_exhaustion_tranche_extension_v1"]
    status: Literal["frozen_prelabel"]
    base_v1_policy: v1.ArtifactBinding
    base_v1_implementation: v1.ArtifactBinding
    base_v2_policy: v1.ArtifactBinding
    base_v2_implementation: v1.ArtifactBinding
    frame_v3_policy: v1.ArtifactBinding
    frame_v3_implementation: v1.ArtifactBinding
    family_source_matrices: tuple[v1.ArtifactBinding, v1.ArtifactBinding]
    required_families: tuple[str, str, str]
    family_pins: tuple[FamilyPinV1, FamilyPinV1, FamilyPinV1]
    pool_pins: tuple[PoolPinV1, PoolPinV1]
    original_tranche_count: Literal[12]
    activation_actions: tuple[
        Literal["freeze_reduced_frame"],
        Literal["exhausted_without_frame"],
    ]
    extension_tranches: tuple[
        v1.TrancheSpec,
        v1.TrancheSpec,
        v1.TrancheSpec,
        v1.TrancheSpec,
    ]
    stop_rule: Literal["bound_v1_preferred_size_and_coverage"]
    frame_creation_enabled: Literal[False] = False
    frame_freezer_line: Literal["lf021_problem_aware_frame_freeze_v3"]
    current_v3_extended_population_directly_eligible: Literal[False] = False
    frame_handoff_requires_separate_reviewed_adapter: Literal[True] = True
    decision_inputs: tuple[str, ...]
    forbidden_inputs: tuple[str, ...]
    forecast_motivation: ForecastMotivationV1
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.required_families != _EXPECTED_FAMILIES:
            raise ValueError("extension families differ from the frozen three-family set")
        if tuple(item.family_id for item in self.family_pins) != _EXPECTED_FAMILIES:
            raise ValueError("family pins differ from required families")
        if tuple(item.pool_id for item in self.pool_pins) != (
            "algebra_gate3_docstrings_v1",
            "cross_domain_docstrings_v1",
        ):
            raise ValueError("extension pools differ from the frozen two-pool set")
        if self.activation_actions != (
            "freeze_reduced_frame",
            "exhausted_without_frame",
        ):
            raise ValueError("activation actions must exclude preferred/collect decisions")
        observed_extension = tuple(
            (
                tranche.order,
                tranche.tranche_id,
                tranche.pool_id,
                tuple(tranche.seeds_by_family[family] for family in _EXPECTED_FAMILIES),
                tranche.expected_problem_count,
                tranche.expected_invocations,
            )
            for tranche in self.extension_tranches
        )
        if observed_extension != _EXPECTED_EXTENSION:
            raise ValueError("extension tranche sequence differs from preregistration")
        if any(tranche.mandatory_before_stopping for tranche in self.extension_tranches):
            raise ValueError("extension must re-evaluate the stop rule after every tranche")
        if self.decision_inputs != _EXPECTED_DECISION_INPUTS:
            raise ValueError("extension decision input allowlist differs")
        if self.forbidden_inputs != _EXPECTED_FORBIDDEN_INPUTS:
            raise ValueError("extension semantic-input denylist differs")
        return self


class ExtensionDecisionAction(StrEnum):
    COLLECT_NEXT_EXTENSION_TRANCHE = "collect_next_extension_tranche"
    PREFERRED_ELIGIBLE_STOP = "preferred_eligible_stop"
    EXTENSION_EXHAUSTED_WITHOUT_PREFERRED = "extension_exhausted_without_preferred_eligibility"


class PostExhaustionExtensionDecisionV1(StrictModel):
    """Content-addressed collection decision; never a frame or label."""

    schema_version: Literal[1] = 1
    decision_id: str = Field(pattern=_DECISION_ID)
    policy_id: Literal["lf021_post_exhaustion_tranche_extension_v1"]
    policy_artifact: v1.ArtifactBinding
    implementation_artifact: v1.ArtifactBinding
    base_v1_policy: v1.ArtifactBinding
    base_v1_implementation: v1.ArtifactBinding
    base_v2_policy: v1.ArtifactBinding
    base_v2_implementation: v1.ArtifactBinding
    frame_v3_policy: v1.ArtifactBinding
    frame_v3_implementation: v1.ArtifactBinding
    activation_v2_decision_id: str
    activation_v2_decision: v1.ArtifactBinding
    activation_action: Literal["freeze_reduced_frame", "exhausted_without_frame"]
    original_observation_count: Literal[12]
    extension_observations: tuple[v1.ObservationBinding, ...]
    counts: v1.OperationalCounts
    coverage_deficits: tuple[str, ...]
    preferred_eligibility_met: bool
    action: ExtensionDecisionAction
    next_tranche: v1.TrancheSpec | None
    extension_sequence_exhausted: bool
    frame: None = None
    frame_creation_performed: Literal[False] = False
    frame_freeze_handoff_required: bool
    frame_handoff_requires_separate_reviewed_adapter: Literal[True] = True
    decision_inputs_used: tuple[str, ...]
    forbidden_inputs_used: tuple[()] = ()
    forecast_used_as_statistical_guarantee: Literal[False] = False
    model_execution_performed: Literal[False] = False
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        extension_count = len(self.extension_observations)
        if extension_count > len(_EXPECTED_EXTENSION):
            raise ValueError("extension observation prefix exceeds preregistration")
        if self.counts.observed_tranche_count != self.original_observation_count + extension_count:
            raise ValueError("combined observation count does not reconcile")
        if self.extension_sequence_exhausted != (extension_count == len(_EXPECTED_EXTENSION)):
            raise ValueError("extension exhaustion flag differs from prefix length")
        if self.preferred_eligibility_met != (
            self.counts.unique_compiling_count >= 240 and not self.coverage_deficits
        ):
            raise ValueError("preferred eligibility differs from bound size/coverage rule")
        if self.action is ExtensionDecisionAction.COLLECT_NEXT_EXTENSION_TRANCHE:
            if (
                self.preferred_eligibility_met
                or self.extension_sequence_exhausted
                or self.next_tranche is None
                or self.frame_freeze_handoff_required
            ):
                raise ValueError("collect-next extension decision is incoherent")
            expected = _EXPECTED_EXTENSION[extension_count]
            if (
                self.next_tranche.order,
                self.next_tranche.tranche_id,
                self.next_tranche.pool_id,
            ) != expected[:3]:
                raise ValueError("next extension tranche differs from frozen sequence")
        elif self.action is ExtensionDecisionAction.PREFERRED_ELIGIBLE_STOP:
            if (
                not self.preferred_eligibility_met
                or self.next_tranche is not None
                or not self.frame_freeze_handoff_required
            ):
                raise ValueError("preferred-eligible stop is incoherent")
        elif (
            self.preferred_eligibility_met
            or not self.extension_sequence_exhausted
            or self.next_tranche is not None
            or self.frame_freeze_handoff_required
        ):
            raise ValueError("extension-exhausted decision is incoherent")
        if self.decision_inputs_used != _EXPECTED_DECISION_INPUTS:
            raise ValueError("decision used inputs outside the frozen allowlist")
        expected_id = "lf021_post_exhaustion_extension_decision_v1:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_extension_decision_v1",
                **self.model_dump(mode="json", exclude={"decision_id"}),
            }
        )
        if self.decision_id != expected_id:
            raise ValueError("extension decision ID differs from content")
        return self


class ExtendedPopulationHandoffProjectionV1(StrictModel):
    """Strict read-only bridge from an extended stop to v3 population rows.

    This is not a prevalence-frame artifact and is deliberately not accepted
    by the frozen v3 decision loader.  It proves that the extension can be
    replayed into the exact ``EligiblePopulationItemV3`` row schema without
    mutating v3.  A separately reviewed materializer is still required before
    those rows may be frozen and sampled.
    """

    schema_version: Literal[1] = 1
    projection_id: str = Field(pattern=_HANDOFF_PROJECTION_ID)
    projection_kind: Literal["lf021_post_exhaustion_population_handoff_v1"]
    extension_decision_id: str = Field(pattern=_DECISION_ID)
    extension_decision: v1.ArtifactBinding
    extension_policy: v1.ArtifactBinding
    extension_implementation: v1.ArtifactBinding
    frame_v3_policy: v1.ArtifactBinding
    frame_v3_implementation: v1.ArtifactBinding
    population_record_schema: Literal["EligiblePopulationItemV3"]
    population_unit: tuple[
        Literal["problem_group"],
        Literal["alpha_identity_fingerprint"],
    ]
    population_item_count: int = Field(ge=240)
    population_member_count: int = Field(ge=240)
    population_items_sha256: str = Field(pattern=_HEX64)
    stratum_population_sizes: dict[str, int] = Field(min_length=1)
    preferred_size: Literal[240]
    coverage_deficits: tuple[()] = ()
    direct_frozen_v3_decision_compatible: Literal[False] = False
    required_consumer: Literal["separately_reviewed_extended_population_materializer_v1"]
    frame_created: Literal[False] = False
    sampling_seed_obtained: Literal[False] = False
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if sum(self.stratum_population_sizes.values()) != self.population_item_count:
            raise ValueError("handoff stratum sizes do not reconcile")
        if any(value <= 0 for value in self.stratum_population_sizes.values()):
            raise ValueError("handoff strata must be nonempty")
        expected = "lf021_post_exhaustion_population_handoff_v1:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_population_handoff_v1",
                **self.model_dump(mode="json", exclude={"projection_id"}),
            }
        )
        if self.projection_id != expected:
            raise ValueError("handoff projection ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedOriginalExhaustion:
    decision: v2.ExpansionDecisionV2
    decision_binding: v1.ArtifactBinding
    base_policy: v1.TrancheExpansionPolicy
    observations: tuple[v1.LoadedObservation, ...]
    problem_groups: dict[str, str]


@dataclass(frozen=True, slots=True)
class ExtensionRunV1:
    decision: PostExhaustionExtensionDecisionV1
    decision_path: Path
    report_path: Path


@dataclass(frozen=True, slots=True)
class VerifiedExtendedStopForFrameV3:
    """Strict replay result exposed to a separately reviewed frame adapter."""

    decision: PostExhaustionExtensionDecisionV1
    decision_path: Path
    decision_binding: v1.ArtifactBinding
    verified_original_exhaustion: VerifiedOriginalExhaustion
    clusters: tuple[v2._ProblemAwareCluster, ...]
    problem_groups: dict[str, str]
    population_items: tuple[frame_v3.EligiblePopulationItemV3, ...]
    handoff_projection: ExtendedPopulationHandoffProjectionV1


def _strict_json_object(payload: bytes, *, location: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PostExhaustionExtensionError(f"duplicate JSON key {key!r}: {location}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                PostExhaustionExtensionError(f"non-finite JSON value {token!r}: {location}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PostExhaustionExtensionError(f"invalid JSON: {location}") from exc
    if not isinstance(value, dict):
        raise PostExhaustionExtensionError(f"JSON root is not an object: {location}")
    return value


def _resolve(repo_root: Path, artifact: str) -> Path:
    path = Path(artifact)
    resolved = (path if path.is_absolute() else repo_root / path).resolve()
    if not path.is_absolute() and not resolved.is_relative_to(repo_root.resolve()):
        raise PostExhaustionExtensionError(f"artifact escapes repository: {artifact}")
    return resolved


def _verify(repo_root: Path, binding: v1.ArtifactBinding) -> Path:
    path = _resolve(repo_root, binding.artifact)
    if not path.is_file() or hash_file(path) != binding.sha256:
        raise PostExhaustionExtensionError(f"bound artifact differs: {binding.artifact}")
    return path


def _binding(repo_root: Path, path: Path) -> v1.ArtifactBinding:
    return v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, path),
        sha256=hash_file(path),
    )


def load_post_exhaustion_extension_policy(
    path: Path,
) -> LoadedConfig[PostExhaustionExtensionPolicyV1]:
    return load_config(path, PostExhaustionExtensionPolicyV1)


def _verify_policy_lineage(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PostExhaustionExtensionPolicyV1],
) -> tuple[
    LoadedConfig[v1.TrancheExpansionPolicy],
    LoadedConfig[v2.TrancheExpansionAmendmentV2],
]:
    policy = loaded_policy.config
    v1_policy_path = _verify(repo_root, policy.base_v1_policy)
    v1_implementation_path = _verify(repo_root, policy.base_v1_implementation)
    v2_policy_path = _verify(repo_root, policy.base_v2_policy)
    v2_implementation_path = _verify(repo_root, policy.base_v2_implementation)
    v3_policy_path = _verify(repo_root, policy.frame_v3_policy)
    v3_implementation_path = _verify(repo_root, policy.frame_v3_implementation)
    for binding in policy.family_source_matrices:
        _verify(repo_root, binding)
    if (
        v1_implementation_path.resolve() != Path(v1.__file__).resolve()
        or v2_implementation_path.resolve() != Path(v2.__file__).resolve()
        or v3_implementation_path.resolve() != Path(frame_v3.__file__).resolve()
    ):
        raise PostExhaustionExtensionError("bound implementation is not the imported module")
    loaded_v1 = v1.load_tranche_expansion_policy(v1_policy_path)
    loaded_v2 = v2.load_amendment_v2(v2_policy_path)
    loaded_v3 = frame_v3.load_frame_freeze_policy_v3(v3_policy_path)
    if (
        loaded_v2.config.base_v1_policy != policy.base_v1_policy
        or loaded_v2.config.base_v1_implementation != policy.base_v1_implementation
        or loaded_v3.config.base_v2_policy != policy.base_v2_policy
        or loaded_v3.config.base_v2_implementation != policy.base_v2_implementation
    ):
        raise PostExhaustionExtensionError("v1/v2/v3 lineage bindings do not chain")
    base = loaded_v1.config
    if (
        len(base.tranches) != policy.original_tranche_count
        or base.required_families != policy.required_families
    ):
        raise PostExhaustionExtensionError("original sequence/family binding differs")
    pool_pins = {
        item.pool_id: (item.problem_count, item.records, item.manifest) for item in policy.pool_pins
    }
    expected_pool_pins = {
        item.pool_id: (item.problem_count, item.records, item.manifest) for item in base.pools
    }
    if pool_pins != expected_pool_pins:
        raise PostExhaustionExtensionError("extension pool pins differ from v1")
    return loaded_v1, loaded_v2


def load_verified_original_exhaustion(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PostExhaustionExtensionPolicyV1],
    activation_v2_decision_path: Path,
) -> VerifiedOriginalExhaustion:
    """Replay the exact twelve-tranche v2 non-preferred activation decision."""

    loaded_v1, loaded_v2 = _verify_policy_lineage(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
    )
    decision_bytes = activation_v2_decision_path.read_bytes()
    decision = v2.ExpansionDecisionV2.model_validate(
        _strict_json_object(
            decision_bytes,
            location=str(activation_v2_decision_path),
        )
    )
    if (
        decision.policy_artifact != loaded_policy.config.base_v2_policy
        or decision.implementation_artifact != loaded_policy.config.base_v2_implementation
        or decision.base_v1_policy != loaded_policy.config.base_v1_policy
        or decision.base_v1_implementation != loaded_policy.config.base_v1_implementation
    ):
        raise PostExhaustionExtensionError("activation decision lineage differs")
    if len(decision.observations) != _ORIGINAL_TRANCHE_COUNT:
        raise PostExhaustionExtensionError(
            "extension activates only after all 12 original tranches"
        )
    if decision.action not in {
        v1.DecisionAction.FREEZE_REDUCED_FRAME,
        v1.DecisionAction.EXHAUSTED_WITHOUT_FRAME,
    }:
        raise PostExhaustionExtensionError(
            "activation requires exhaustion without preferred eligibility"
        )
    if decision.next_tranche is not None:
        raise PostExhaustionExtensionError("activation decision still selects a tranche")
    if (
        decision.counts.unique_compiling_count >= loaded_v1.config.frame.preferred_size
        and not decision.coverage_deficits
    ):
        raise PostExhaustionExtensionError(
            "activation decision already satisfies preferred eligibility"
        )
    observed_paths = tuple(
        _verify(repo_root, item.postprocess_manifest) for item in decision.observations
    )
    recomputed, _historical_fixed_salt_frame = v2.evaluate_tranche_expansion_v2(
        repo_root=repo_root,
        loaded_amendment=loaded_v2,
        observed_manifests=observed_paths,
    )
    if recomputed != decision:
        raise PostExhaustionExtensionError(
            "activation decision does not replay from bound observations"
        )
    observations = tuple(
        v1.load_postprocess_observation(
            repo_root=repo_root,
            policy=loaded_v1.config,
            tranche=loaded_v1.config.tranches[index],
            manifest_path=manifest_path,
        )
        for index, manifest_path in enumerate(observed_paths)
    )
    return VerifiedOriginalExhaustion(
        decision=decision,
        decision_binding=v1.ArtifactBinding(
            artifact=v1._relative_or_absolute(
                repo_root,
                activation_v2_decision_path,
            ),
            sha256=hash_file(activation_v2_decision_path),
        ),
        base_policy=loaded_v1.config,
        observations=observations,
        problem_groups=v2._load_problem_groups(
            repo_root=repo_root,
            policy=loaded_v1.config,
        ),
    )


def _load_extension_observations(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PostExhaustionExtensionPolicyV1],
    verified: VerifiedOriginalExhaustion,
    manifest_paths: tuple[Path, ...],
) -> tuple[v1.LoadedObservation, ...]:
    return tuple(
        v1.load_postprocess_observation(
            repo_root=repo_root,
            policy=verified.base_policy,
            tranche=loaded_policy.config.extension_tranches[index],
            manifest_path=manifest_path,
        )
        for index, manifest_path in enumerate(manifest_paths)
    )


def _cluster_all_observations(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PostExhaustionExtensionPolicyV1],
    verified: VerifiedOriginalExhaustion,
    extension_observations: tuple[v1.LoadedObservation, ...],
) -> tuple[v2._ProblemAwareCluster, ...]:
    all_observations = verified.observations + extension_observations
    if len({item.binding.manifest_id for item in all_observations}) != len(all_observations):
        raise PostExhaustionExtensionError(
            "postprocess manifest is reused across original/extension tranches"
        )
    loaded_v2 = v2.load_amendment_v2(_verify(repo_root, loaded_policy.config.base_v2_policy))
    return v2._cluster_candidates(
        all_observations,
        problem_groups=verified.problem_groups,
        representative_hash_salt=loaded_v2.config.representative_hash_salt,
    )


def evaluate_post_exhaustion_extension(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PostExhaustionExtensionPolicyV1],
    activation_v2_decision_path: Path,
    extension_observed_manifests: tuple[Path, ...],
) -> PostExhaustionExtensionDecisionV1:
    """Evaluate one complete extension prefix without creating a frame."""

    if len(extension_observed_manifests) > len(loaded_policy.config.extension_tranches):
        raise PostExhaustionExtensionError("extension observation prefix exceeds preregistration")
    verified = load_verified_original_exhaustion(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
        activation_v2_decision_path=activation_v2_decision_path,
    )
    extension_observations = _load_extension_observations(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
        verified=verified,
        manifest_paths=extension_observed_manifests,
    )
    all_observations = verified.observations + extension_observations
    clusters = _cluster_all_observations(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
        verified=verified,
        extension_observations=extension_observations,
    )
    counts = v1._operational_counts(
        policy=verified.base_policy,
        observations=all_observations,
        clusters=cast(Any, clusters),
    )
    deficits = v1._coverage_deficits(verified.base_policy, counts)
    preferred_ready = (
        counts.unique_compiling_count >= verified.base_policy.frame.preferred_size and not deficits
    )
    extension_exhausted = len(extension_observations) == len(
        loaded_policy.config.extension_tranches
    )
    if preferred_ready:
        action = ExtensionDecisionAction.PREFERRED_ELIGIBLE_STOP
        next_tranche = None
        frame_handoff_required = True
    elif not extension_exhausted:
        action = ExtensionDecisionAction.COLLECT_NEXT_EXTENSION_TRANCHE
        next_tranche = loaded_policy.config.extension_tranches[len(extension_observations)]
        frame_handoff_required = False
    else:
        action = ExtensionDecisionAction.EXTENSION_EXHAUSTED_WITHOUT_PREFERRED
        next_tranche = None
        frame_handoff_required = False

    implementation_path = Path(__file__).resolve()
    policy = loaded_policy.config
    payload: dict[str, Any] = {
        "schema_version": 1,
        "policy_id": policy.policy_id,
        "policy_artifact": _binding(
            repo_root,
            loaded_policy.path,
        ).model_dump(mode="json"),
        "implementation_artifact": _binding(
            repo_root,
            implementation_path,
        ).model_dump(mode="json"),
        "base_v1_policy": policy.base_v1_policy.model_dump(mode="json"),
        "base_v1_implementation": policy.base_v1_implementation.model_dump(mode="json"),
        "base_v2_policy": policy.base_v2_policy.model_dump(mode="json"),
        "base_v2_implementation": policy.base_v2_implementation.model_dump(mode="json"),
        "frame_v3_policy": policy.frame_v3_policy.model_dump(mode="json"),
        "frame_v3_implementation": policy.frame_v3_implementation.model_dump(mode="json"),
        "activation_v2_decision_id": verified.decision.decision_id,
        "activation_v2_decision": verified.decision_binding.model_dump(mode="json"),
        "activation_action": verified.decision.action.value,
        "original_observation_count": _ORIGINAL_TRANCHE_COUNT,
        "extension_observations": tuple(
            item.binding.model_dump(mode="json") for item in extension_observations
        ),
        "counts": counts.model_dump(mode="json"),
        "coverage_deficits": deficits,
        "preferred_eligibility_met": preferred_ready,
        "action": action.value,
        "next_tranche": (
            next_tranche.model_dump(mode="json") if next_tranche is not None else None
        ),
        "extension_sequence_exhausted": extension_exhausted,
        "frame": None,
        "frame_creation_performed": False,
        "frame_freeze_handoff_required": frame_handoff_required,
        "frame_handoff_requires_separate_reviewed_adapter": True,
        "decision_inputs_used": policy.decision_inputs,
        "forbidden_inputs_used": (),
        "forecast_used_as_statistical_guarantee": False,
        "model_execution_performed": False,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    decision_id = "lf021_post_exhaustion_extension_decision_v1:" + hash_canonical(
        {
            "schema": "lf021_post_exhaustion_extension_decision_v1",
            **payload,
        }
    )
    return PostExhaustionExtensionDecisionV1.model_validate({"decision_id": decision_id, **payload})


def _render_report(decision: PostExhaustionExtensionDecisionV1) -> str:
    next_id = decision.next_tranche.tranche_id if decision.next_tranche is not None else "none"
    lines = [
        "# LF-021 post-exhaustion collection decision",
        "",
        f"- Decision: `{decision.decision_id}`",
        f"- Action: `{decision.action.value}`",
        f"- Activation: `{decision.activation_v2_decision_id}`",
        f"- Extension observations: {len(decision.extension_observations)}",
        f"- Unique problem-aware units: {decision.counts.unique_compiling_count}",
        f"- Preferred eligibility: {str(decision.preferred_eligibility_met).lower()}",
        f"- Next tranche: `{next_id}`",
        f"- Coverage deficits: {len(decision.coverage_deficits)}",
        "- Frame created: false",
        "- Forecast used as statistical guarantee: false",
        "- Semantic labels inspected: false",
        "- Semantic labels created: false",
        "- Supervision eligible: false",
        "- Gate 5G credit claimed: false",
        "- Gate 5 closed: false",
        "",
        "If preferred eligibility is reached, collection stops. The current "
        "frozen v3 implementation does not directly consume the extended "
        "population; a separate reviewed adapter is required before any frame.",
        "",
    ]
    return "\n".join(lines)


def _write_immutable(path: Path, payload: bytes) -> None:
    try:
        v1._write_immutable(path, payload)
    except v1.TrancheExpansionError as exc:
        raise PostExhaustionExtensionError(str(exc)) from exc


def write_post_exhaustion_extension_decision(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PostExhaustionExtensionPolicyV1],
    activation_v2_decision_path: Path,
    extension_observed_manifests: tuple[Path, ...],
    output_root: Path,
) -> ExtensionRunV1:
    decision = evaluate_post_exhaustion_extension(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
        activation_v2_decision_path=activation_v2_decision_path,
        extension_observed_manifests=extension_observed_manifests,
    )
    suffix = decision.decision_id.rsplit(":", 1)[-1]
    decision_path = output_root / "decisions" / f"{suffix}.json"
    report_path = output_root / "decisions" / f"{suffix}.md"
    _write_immutable(
        decision_path,
        canonical_json_bytes(decision.model_dump(mode="json")),
    )
    _write_immutable(report_path, _render_report(decision).encode("utf-8"))
    return ExtensionRunV1(
        decision=decision,
        decision_path=decision_path,
        report_path=report_path,
    )


def verify_extended_stop_for_frame_v3(
    *,
    repo_root: Path,
    policy_path: Path,
    decision_path: Path,
) -> VerifiedExtendedStopForFrameV3:
    """Replay an extended preferred stop into exact v3 population-row objects.

    The return value is the separately versioned strict adapter boundary
    required because the frozen v3 loader accepts only a v2 stop decision.
    This function obtains no entropy and writes no population, seed, or frame.
    """

    loaded_policy = load_post_exhaustion_extension_policy(policy_path)
    _verify_policy_lineage(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
    )
    decision = PostExhaustionExtensionDecisionV1.model_validate(
        _strict_json_object(
            decision_path.read_bytes(),
            location=str(decision_path),
        )
    )
    expected_policy_binding = _binding(repo_root, loaded_policy.path)
    expected_implementation_binding = _binding(repo_root, Path(__file__).resolve())
    policy = loaded_policy.config
    if (
        decision.policy_artifact != expected_policy_binding
        or decision.implementation_artifact != expected_implementation_binding
        or decision.base_v1_policy != policy.base_v1_policy
        or decision.base_v1_implementation != policy.base_v1_implementation
        or decision.base_v2_policy != policy.base_v2_policy
        or decision.base_v2_implementation != policy.base_v2_implementation
        or decision.frame_v3_policy != policy.frame_v3_policy
        or decision.frame_v3_implementation != policy.frame_v3_implementation
    ):
        raise PostExhaustionExtensionError(
            "extended decision differs from the active frozen lineage"
        )
    if decision.action is not ExtensionDecisionAction.PREFERRED_ELIGIBLE_STOP:
        raise PostExhaustionExtensionError(
            "frame handoff requires an extended preferred-eligible stop"
        )
    if (
        not decision.frame_freeze_handoff_required
        or decision.coverage_deficits
        or decision.counts.unique_compiling_count < policy.forecast_motivation.preferred_size
    ):
        raise PostExhaustionExtensionError("extended decision is not preferred-frame eligible")

    activation_path = _verify(repo_root, decision.activation_v2_decision)
    verified_original = load_verified_original_exhaustion(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
        activation_v2_decision_path=activation_path,
    )
    extension_paths = tuple(
        _verify(repo_root, item.postprocess_manifest) for item in decision.extension_observations
    )
    replayed = evaluate_post_exhaustion_extension(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
        activation_v2_decision_path=activation_path,
        extension_observed_manifests=extension_paths,
    )
    if replayed != decision:
        raise PostExhaustionExtensionError(
            "extended decision does not replay from bound observations"
        )

    extension_observations = _load_extension_observations(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
        verified=verified_original,
        manifest_paths=extension_paths,
    )
    clusters = _cluster_all_observations(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
        verified=verified_original,
        extension_observations=extension_observations,
    )
    population_items = tuple(
        sorted(
            (
                frame_v3._population_item(
                    cluster,
                    problem_groups=verified_original.problem_groups,
                )
                for cluster in clusters
            ),
            key=lambda item: item.population_record_id,
        )
    )
    population_keys = {
        (item.problem_group, item.alpha_identity_fingerprint) for item in population_items
    }
    if (
        len(population_items) != decision.counts.unique_compiling_count
        or len(population_keys) != len(population_items)
        or sum(item.member_count for item in population_items)
        != decision.counts.benchmark_clear_compile_count
    ):
        raise PostExhaustionExtensionError(
            "strict v3 population-row projection differs from extended counts"
        )
    population_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in population_items
    )
    decision_binding = _binding(repo_root, decision_path)
    projection_payload: dict[str, Any] = {
        "schema_version": 1,
        "projection_kind": "lf021_post_exhaustion_population_handoff_v1",
        "extension_decision_id": decision.decision_id,
        "extension_decision": decision_binding.model_dump(mode="json"),
        "extension_policy": decision.policy_artifact.model_dump(mode="json"),
        "extension_implementation": decision.implementation_artifact.model_dump(mode="json"),
        "frame_v3_policy": decision.frame_v3_policy.model_dump(mode="json"),
        "frame_v3_implementation": decision.frame_v3_implementation.model_dump(mode="json"),
        "population_record_schema": "EligiblePopulationItemV3",
        "population_unit": (
            "problem_group",
            "alpha_identity_fingerprint",
        ),
        "population_item_count": len(population_items),
        "population_member_count": sum(item.member_count for item in population_items),
        "population_items_sha256": sha256_hex(population_bytes),
        "stratum_population_sizes": dict(
            sorted(Counter(item.sampling_stratum for item in population_items).items())
        ),
        "preferred_size": policy.forecast_motivation.preferred_size,
        "coverage_deficits": (),
        "direct_frozen_v3_decision_compatible": False,
        "required_consumer": ("separately_reviewed_extended_population_materializer_v1"),
        "frame_created": False,
        "sampling_seed_obtained": False,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    projection_id = "lf021_post_exhaustion_population_handoff_v1:" + hash_canonical(
        {
            "schema": "lf021_post_exhaustion_population_handoff_v1",
            **projection_payload,
        }
    )
    projection = ExtendedPopulationHandoffProjectionV1.model_validate(
        {"projection_id": projection_id, **projection_payload}
    )
    return VerifiedExtendedStopForFrameV3(
        decision=decision,
        decision_path=decision_path,
        decision_binding=decision_binding,
        verified_original_exhaustion=verified_original,
        clusters=clusters,
        problem_groups=verified_original.problem_groups,
        population_items=population_items,
        handoff_projection=projection,
    )


__all__ = [
    "ExtendedPopulationHandoffProjectionV1",
    "ExtensionDecisionAction",
    "ExtensionRunV1",
    "PostExhaustionExtensionDecisionV1",
    "PostExhaustionExtensionError",
    "PostExhaustionExtensionPolicyV1",
    "VerifiedExtendedStopForFrameV3",
    "VerifiedOriginalExhaustion",
    "evaluate_post_exhaustion_extension",
    "load_post_exhaustion_extension_policy",
    "load_verified_original_exhaustion",
    "verify_extended_stop_for_frame_v3",
    "write_post_exhaustion_extension_decision",
]
