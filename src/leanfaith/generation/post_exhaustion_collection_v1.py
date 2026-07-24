"""Reviewed collection authorization after the original LF-021 sequence.

This module is a deliberately non-executing bridge.  It turns one strictly
replayed ``PostExhaustionExtensionDecisionV1`` collect-next decision into:

* a content-addressed, one-tranche authorization record; and
* a content-addressed collector-v6 config/plan boundary whose invocations are
  built with the already reviewed local prompt/checkpoint primitives.

It does not modify collector-v5 or postprocess-v6, and it exposes no collection
executor.  A separately reviewed collector-v6 plus postprocess-v7 remains
required before any extension tranche can be executed.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.generation import post_exhaustion_extension as extension
from leanfaith.generation import research_collection as collection_v1
from leanfaith.generation import research_collection_v2 as collection_v2
from leanfaith.generation import research_collection_v3 as collection_v3
from leanfaith.generation import research_collection_v5 as collection_v5
from leanfaith.generation import research_postprocess_v6 as postprocess_v6
from leanfaith.generation import tranche_expansion as tranche_v1
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.nl_lean import ProblemPoolRecord

_HEX64 = r"^[0-9a-f]{64}$"
_AUTHORIZATION_ID = r"^lf021_reviewed_extension_collection_authorization_v1:[0-9a-f]{64}$"
_CONFIG_ID = r"^lf021_post_exhaustion_collection_config_v6:[0-9a-f]{64}$"
_PLAN_ID = r"^lf021_post_exhaustion_collection_plan_v6:[0-9a-f]{64}$"
_POLICY_ID = "lf021_post_exhaustion_collection_authorization_v1"
_DEFAULT_POLICY = Path("configs/generation/lf021_post_exhaustion_collection_v1.yaml")
_EXPECTED_FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)
_POOL_DIALECTS = {
    "algebra_gate3_docstrings_v1": "gate3_algebra_operational_v1",
    "cross_domain_docstrings_v1": "cross_domain_operational_v1",
}
_POOL_SLUGS = {
    "algebra_gate3_docstrings_v1": "gate3_docstrings_operational_v1",
    "cross_domain_docstrings_v1": "cross_domain_docstrings_operational_v1",
}


class PostExhaustionCollectionV1Error(RuntimeError):
    """The reviewed extension-collection boundary failed closed."""


class PoolArtifactPinV1(StrictModel):
    pool_id: str = Field(min_length=1)
    artifact: tranche_v1.ArtifactBinding


class PostExhaustionCollectionPolicyV1(StrictModel):
    """Frozen, non-executing authorization/config-plan adapter policy."""

    schema_version: Literal[1] = 1
    policy_id: Literal["lf021_post_exhaustion_collection_authorization_v1"]
    status: Literal["frozen_prelabel"]
    extension_policy: tranche_v1.ArtifactBinding
    extension_implementation: tranche_v1.ArtifactBinding
    adapter_implementation: tranche_v1.ArtifactBinding
    collector_v5_implementation: tranche_v1.ArtifactBinding
    postprocess_v6_implementation: tranche_v1.ArtifactBinding
    family_source_matrices: tuple[PoolArtifactPinV1, PoolArtifactPinV1]
    base_collection_templates: tuple[PoolArtifactPinV1, PoolArtifactPinV1]
    required_families: tuple[str, str, str]
    required_transport: Literal["local"]
    future_collector_schema_version: Literal[6]
    required_future_postprocess_schema_version: Literal[7]
    collector_v5_directly_compatible: Literal[False]
    postprocess_v6_directly_compatible: Literal[False]
    config_plan_adapter_only: Literal[True]
    execution_enabled: Literal[False]
    execution_blocker: Literal["reviewed_collector_v6_and_postprocess_v7_not_implemented"]
    authorization_output_root: Literal[
        "reports/generation/lf021_post_exhaustion_collection_authorizations_v1"
    ]
    config_plan_output_root: Literal[
        "reports/generation/lf021_post_exhaustion_collection_config_plans_v1"
    ]
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.required_families != _EXPECTED_FAMILIES:
            raise ValueError("collection adapter family inventory differs")
        expected_pools = tuple(sorted(_POOL_DIALECTS))
        for field_name in ("family_source_matrices", "base_collection_templates"):
            pins = getattr(self, field_name)
            if tuple(item.pool_id for item in pins) != expected_pools:
                raise ValueError(f"{field_name} must bind both pools in sorted order")
            if len({item.artifact.artifact for item in pins}) != len(pins):
                raise ValueError(f"{field_name} contains a reused artifact")
        return self


class ReviewedExtensionCollectionAuthorizationV1(StrictModel):
    """One replayed decision authorizing exactly one extension tranche."""

    schema_version: Literal[1] = 1
    authorization_id: str = Field(pattern=_AUTHORIZATION_ID)
    policy_id: Literal["lf021_post_exhaustion_collection_authorization_v1"]
    authorization_policy: tranche_v1.ArtifactBinding
    authorization_implementation: tranche_v1.ArtifactBinding
    extension_policy: tranche_v1.ArtifactBinding
    extension_implementation: tranche_v1.ArtifactBinding
    extension_decision_id: str
    extension_decision: tranche_v1.ArtifactBinding
    activation_v2_decision_id: str
    activation_v2_decision: tranche_v1.ArtifactBinding
    activation_action: Literal["freeze_reduced_frame", "exhausted_without_frame"]
    original_observation_count: Literal[12]
    extension_prefix_observations: tuple[tranche_v1.ObservationBinding, ...]
    extension_prefix_length: int = Field(ge=0, le=3)
    source_action: Literal["collect_next_extension_tranche"]
    authorized_tranche: tranche_v1.TrancheSpec
    pool_pin: extension.PoolPinV1
    family_pins: tuple[
        extension.FamilyPinV1,
        extension.FamilyPinV1,
        extension.FamilyPinV1,
    ]
    source_matrix: tranche_v1.ArtifactBinding
    scientific_tranche_authorized: Literal[True] = True
    collector_v5_compatible: Literal[False] = False
    postprocess_v6_compatible: Literal[False] = False
    config_plan_adapter_only: Literal[True] = True
    executable_collection_adapter_available: Literal[False] = False
    required_future_collector_schema_version: Literal[6] = 6
    required_future_postprocess_schema_version: Literal[7] = 7
    execution_blocker: Literal["reviewed_collector_v6_and_postprocess_v7_not_implemented"]
    decision_inputs_used: tuple[str, ...]
    forbidden_inputs_used: tuple[()] = ()
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.extension_prefix_length != len(self.extension_prefix_observations):
            raise ValueError("authorization prefix length does not reconcile")
        if self.authorized_tranche.order != 12 + self.extension_prefix_length:
            raise ValueError("authorization tranche order differs from extension prefix")
        if tuple(item.family_id for item in self.family_pins) != _EXPECTED_FAMILIES:
            raise ValueError("authorization family pins differ")
        if tuple(self.authorized_tranche.seeds_by_family) != _EXPECTED_FAMILIES:
            raise ValueError("authorization tranche families differ")
        if self.authorized_tranche.pool_id != self.pool_pin.pool_id:
            raise ValueError("authorization tranche and pool pin differ")
        expected = "lf021_reviewed_extension_collection_authorization_v1:" + hash_canonical(
            {
                "schema": "lf021_reviewed_extension_collection_authorization_v1",
                **self.model_dump(mode="json", exclude={"authorization_id"}),
            }
        )
        if self.authorization_id != expected:
            raise ValueError("extension collection authorization ID differs from content")
        return self


class PostExhaustionCollectionConfigV6(StrictModel):
    """Execution-disabled collector-v6 configuration boundary."""

    schema_version: Literal[6] = 6
    config_id: str = Field(pattern=_CONFIG_ID)
    authorization: tranche_v1.ArtifactBinding
    authorization_id: str = Field(pattern=_AUTHORIZATION_ID)
    extension_decision: tranche_v1.ArtifactBinding
    extension_decision_id: str
    base_collection_template: tranche_v1.ArtifactBinding
    planning_implementation: tranche_v1.ArtifactBinding
    tranche_id: str
    pool_id: str
    pool_dialect: collection_v5.PoolDialect
    frozen_at: datetime.datetime
    artifact_class: Literal["research"] = "research"
    collection_scope: Literal["post_exhaustion_closed_pool_three_local_family_tranche_v6"]
    shared_execution_record_schema: Literal["lf021_research_execution_records_v1"]
    status: Literal["blocked_pending_reviewed_execution_adapter"]
    execution_enabled: Literal[False]
    execution_blocker: Literal["reviewed_collector_v6_and_postprocess_v7_not_implemented"]
    problem_pool_contract: collection_v3.ScalableProblemPoolContract
    problem_pool_records: collection_v1.ResearchArtifactBinding
    problem_pool_manifest: collection_v1.ResearchArtifactBinding
    context: collection_v1.ResearchArtifactBinding
    import_header: collection_v1.ResearchArtifactBinding
    source_matrix: collection_v1.ResearchArtifactBinding
    runtime: collection_v1.ResearchRuntimeBinding
    families: tuple[collection_v1.ResearchCollectionFamily, ...]
    retry: collection_v1.ResearchRetryConfig
    outputs: collection_v1.ResearchCollectionOutputs
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @property
    def config_hash(self) -> str:
        return hash_canonical(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.frozen_at)
        if self.pool_dialect != _POOL_DIALECTS.get(self.pool_id):
            raise ValueError("collector-v6 pool and dialect differ")
        if len(self.families) != 3:
            raise ValueError("collector-v6 boundary requires exactly three families")
        family_ids = tuple(item.family_id for item in self.families)
        if family_ids != _EXPECTED_FAMILIES:
            raise ValueError("collector-v6 boundary families differ")
        if any(tuple(item.seeds) != tuple(sorted(set(item.seeds))) for item in self.families):
            raise ValueError("collector-v6 family seeds are not sorted and unique")
        expected_root = (
            f"data/raw/real_outputs/{_POOL_SLUGS[self.pool_id]}/v6/"
            f"{self.tranche_id}/local_collection"
        )
        expected_preflight = (
            "reports/generation/"
            f"lf021_local_research_collection_preflight_{self.tranche_id}_v6.json"
        )
        if (
            self.outputs.root != expected_root
            or self.outputs.preflight_report != expected_preflight
        ):
            raise ValueError("collector-v6 output paths differ from tranche")
        expected = "lf021_post_exhaustion_collection_config_v6:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_collection_config_v6",
                **self.model_dump(mode="json", exclude={"config_id"}),
            }
        )
        if self.config_id != expected:
            raise ValueError("collector-v6 config ID differs from content")
        return self


class PostExhaustionCollectionPlanV6(StrictModel):
    """Deterministic invocation plan; deliberately not executable."""

    schema_version: Literal[6] = 6
    plan_id: str = Field(pattern=_PLAN_ID)
    config_id: str = Field(pattern=_CONFIG_ID)
    config: tranche_v1.ArtifactBinding
    config_hash: str = Field(pattern=_HEX64)
    authorization_id: str = Field(pattern=_AUTHORIZATION_ID)
    authorization: tranche_v1.ArtifactBinding
    tranche_id: str
    pool_id: str
    pool_dialect: collection_v5.PoolDialect
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    seed_count_by_family: dict[str, int]
    expected_candidate_count: int = Field(ge=1)
    problem_record_ids: tuple[str, ...] = Field(min_length=1)
    family_bindings: tuple[collection_v1.ResearchFamilyBinding, ...]
    invocations: tuple[collection_v1.ResearchCollectionInvocation, ...]
    planning_only: Literal[True] = True
    execution_enabled: Literal[False] = False
    execution_blocker: Literal["reviewed_collector_v6_and_postprocess_v7_not_implemented"]
    actual_collection_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.family_count != len(self.family_bindings):
            raise ValueError("collector-v6 plan family bindings do not reconcile")
        if list(self.seed_count_by_family) != sorted(self.seed_count_by_family):
            raise ValueError("collector-v6 plan seed counts are not sorted")
        if set(self.seed_count_by_family) != set(_EXPECTED_FAMILIES):
            raise ValueError("collector-v6 plan seed-count families differ")
        expected_count = self.problem_count * sum(self.seed_count_by_family.values())
        if (
            expected_count != self.expected_candidate_count
            or len(self.invocations) != expected_count
        ):
            raise ValueError("collector-v6 plan invocation denominator differs")
        if self.problem_record_ids != tuple(sorted(set(self.problem_record_ids))):
            raise ValueError("collector-v6 plan problem IDs are not sorted and unique")
        invocation_ids = tuple(item.invocation_id for item in self.invocations)
        if invocation_ids != tuple(sorted(set(invocation_ids))):
            raise ValueError("collector-v6 plan invocation IDs are not sorted and unique")
        if any(item.collection_config_hash != self.config_hash for item in self.invocations):
            raise ValueError("collector-v6 invocation config hashes differ")
        expected = "lf021_post_exhaustion_collection_plan_v6:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_collection_plan_v6",
                **self.model_dump(mode="json", exclude={"plan_id"}),
            }
        )
        if self.plan_id != expected:
            raise ValueError("collector-v6 plan ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class ReviewedAuthorizationRunV1:
    authorization: ReviewedExtensionCollectionAuthorizationV1
    path: Path
    binding: tranche_v1.ArtifactBinding


@dataclass(frozen=True, slots=True)
class VerifiedExtensionCollectionAuthorizationsV1:
    preferred_stop: extension.VerifiedExtendedStopForFrameV3
    records: tuple[ReviewedExtensionCollectionAuthorizationV1, ...]
    bindings: tuple[tranche_v1.ArtifactBinding, ...]
    postprocess_observations: tuple[tranche_v1.ObservationBinding, ...]
    paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class PostExhaustionCollectionConfigPlanRunV1:
    config: PostExhaustionCollectionConfigV6
    config_path: Path
    plan: PostExhaustionCollectionPlanV6
    plan_path: Path


def _strict_json_object(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PostExhaustionCollectionV1Error(f"duplicate JSON key {key!r}: {path}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_bytes(),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                PostExhaustionCollectionV1Error(f"non-finite JSON value {token!r}: {path}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PostExhaustionCollectionV1Error(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise PostExhaustionCollectionV1Error(f"JSON root is not an object: {path}")
    return value


def _resolve(repo_root: Path, artifact: str) -> Path:
    root = repo_root.resolve()
    raw = Path(artifact)
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()
    if not raw.is_absolute() and not resolved.is_relative_to(root):
        raise PostExhaustionCollectionV1Error(f"artifact escapes repository: {artifact}")
    if candidate.is_symlink() or not resolved.is_file():
        raise PostExhaustionCollectionV1Error(f"artifact is missing or symlinked: {artifact}")
    return resolved


def _verify_binding(
    repo_root: Path,
    binding: tranche_v1.ArtifactBinding | collection_v1.ResearchArtifactBinding,
) -> Path:
    path = _resolve(repo_root, binding.artifact)
    if hash_file(path) != binding.sha256:
        raise PostExhaustionCollectionV1Error(f"artifact hash differs: {binding.artifact}")
    return path


def _binding(repo_root: Path, path: Path) -> tranche_v1.ArtifactBinding:
    return tranche_v1.ArtifactBinding(
        artifact=tranche_v1._relative_or_absolute(repo_root, path),
        sha256=hash_file(path),
    )


def _write_immutable(path: Path, payload: bytes) -> None:
    try:
        tranche_v1._write_immutable(path, payload)
    except tranche_v1.TrancheExpansionError as exc:
        raise PostExhaustionCollectionV1Error(str(exc)) from exc


def load_post_exhaustion_collection_policy_v1(
    path: Path,
) -> LoadedConfig[PostExhaustionCollectionPolicyV1]:
    return load_config(path, PostExhaustionCollectionPolicyV1)


def _pins_by_pool(
    pins: tuple[PoolArtifactPinV1, PoolArtifactPinV1],
) -> dict[str, tranche_v1.ArtifactBinding]:
    return {item.pool_id: item.artifact for item in pins}


def _verify_policy(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PostExhaustionCollectionPolicyV1],
) -> LoadedConfig[extension.PostExhaustionExtensionPolicyV1]:
    policy = loaded_policy.config
    extension_policy_path = _verify_binding(repo_root, policy.extension_policy)
    extension_implementation_path = _verify_binding(repo_root, policy.extension_implementation)
    adapter_implementation_path = _verify_binding(repo_root, policy.adapter_implementation)
    collector_path = _verify_binding(repo_root, policy.collector_v5_implementation)
    postprocess_path = _verify_binding(repo_root, policy.postprocess_v6_implementation)
    for pin in (*policy.family_source_matrices, *policy.base_collection_templates):
        _verify_binding(repo_root, pin.artifact)
    if (
        extension_implementation_path.resolve() != Path(extension.__file__).resolve()
        or adapter_implementation_path.resolve() != Path(__file__).resolve()
        or collector_path.resolve() != Path(collection_v5.__file__).resolve()
        or postprocess_path.resolve() != Path(postprocess_v6.__file__).resolve()
    ):
        raise PostExhaustionCollectionV1Error(
            "adapter policy implementation binding differs from imported modules"
        )
    loaded_extension = extension.load_post_exhaustion_extension_policy(extension_policy_path)
    if loaded_extension.config.required_families != policy.required_families:
        raise PostExhaustionCollectionV1Error("adapter and extension family inventories differ")
    extension_matrix_bindings = {
        item.artifact: item.sha256 for item in loaded_extension.config.family_source_matrices
    }
    adapter_matrix_bindings = {
        item.artifact.artifact: item.artifact.sha256 for item in policy.family_source_matrices
    }
    if extension_matrix_bindings != adapter_matrix_bindings:
        raise PostExhaustionCollectionV1Error(
            "adapter source-matrix bindings differ from extension policy"
        )
    return loaded_extension


def _validate_source_matrix(
    *,
    repo_root: Path,
    pool_id: str,
    binding: tranche_v1.ArtifactBinding,
    extension_policy: extension.PostExhaustionExtensionPolicyV1,
) -> None:
    path = _verify_binding(repo_root, binding)
    raw = load_yaml_mapping(path)
    schema_version = raw.get("schema_version")
    if pool_id == "algebra_gate3_docstrings_v1" and schema_version == 2:
        matrix: (
            collection_v2.ScalableResearchSourceMatrixV2
            | collection_v3.ScalableResearchSourceMatrixV3
        ) = collection_v2.ScalableResearchSourceMatrixV2.model_validate(raw)
    elif pool_id == "cross_domain_docstrings_v1" and schema_version == 3:
        matrix = collection_v3.ScalableResearchSourceMatrixV3.model_validate(raw)
    else:
        raise PostExhaustionCollectionV1Error("source matrix schema differs from the selected pool")
    expected_families = tuple(
        (item.family_id, item.model, item.revision, item.transport)
        for item in extension_policy.family_pins
    )
    observed_families = tuple(
        (item.family_id, item.model, item.revision, item.transport) for item in matrix.families
    )
    pool_pin = next(item for item in extension_policy.pool_pins if item.pool_id == pool_id)
    if (
        observed_families != expected_families
        or matrix.problem_count != pool_pin.problem_count
        or matrix.problem_pool_manifest_sha256 != pool_pin.manifest.sha256
        or matrix.private_source_content
        or matrix.external_transmission_required
        or matrix.heldout.family_id in extension_policy.required_families
        or matrix.heldout.supervision_eligible
    ):
        raise PostExhaustionCollectionV1Error(
            "source matrix differs from local extension family/pool pins"
        )


def review_extension_collect_next_decision_v1(
    *,
    repo_root: Path,
    authorization_policy_path: Path,
    extension_decision_path: Path,
) -> ReviewedExtensionCollectionAuthorizationV1:
    """Strictly replay and review one collect-next extension decision."""

    loaded_adapter = load_post_exhaustion_collection_policy_v1(authorization_policy_path)
    loaded_extension = _verify_policy(
        repo_root=repo_root,
        loaded_policy=loaded_adapter,
    )
    decision = extension.PostExhaustionExtensionDecisionV1.model_validate(
        _strict_json_object(extension_decision_path)
    )
    expected_extension_policy = _binding(repo_root, loaded_extension.path)
    expected_extension_implementation = _binding(repo_root, Path(extension.__file__).resolve())
    if (
        decision.policy_artifact != expected_extension_policy
        or decision.implementation_artifact != expected_extension_implementation
    ):
        raise PostExhaustionCollectionV1Error(
            "collect-next decision differs from active extension lineage"
        )
    activation_path = _verify_binding(repo_root, decision.activation_v2_decision)
    observed_paths = tuple(
        _verify_binding(repo_root, item.postprocess_manifest)
        for item in decision.extension_observations
    )
    replayed = extension.evaluate_post_exhaustion_extension(
        repo_root=repo_root,
        loaded_policy=loaded_extension,
        activation_v2_decision_path=activation_path,
        extension_observed_manifests=observed_paths,
    )
    if replayed != decision:
        raise PostExhaustionCollectionV1Error(
            "collect-next decision does not replay from bound observations"
        )
    if (
        decision.action is not extension.ExtensionDecisionAction.COLLECT_NEXT_EXTENSION_TRANCHE
        or decision.next_tranche is None
        or decision.frame_freeze_handoff_required
        or decision.preferred_eligibility_met
    ):
        raise PostExhaustionCollectionV1Error("decision does not authorize extension collection")
    policy = loaded_extension.config
    prefix_length = len(decision.extension_observations)
    if (
        prefix_length >= len(policy.extension_tranches)
        or decision.next_tranche != policy.extension_tranches[prefix_length]
    ):
        raise PostExhaustionCollectionV1Error(
            "collect-next tranche differs from frozen extension sequence"
        )
    pool_pin = next(
        item for item in policy.pool_pins if item.pool_id == decision.next_tranche.pool_id
    )
    matrix_binding = _pins_by_pool(loaded_adapter.config.family_source_matrices)[pool_pin.pool_id]
    _validate_source_matrix(
        repo_root=repo_root,
        pool_id=pool_pin.pool_id,
        binding=matrix_binding,
        extension_policy=policy,
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "policy_id": _POLICY_ID,
        "authorization_policy": _binding(repo_root, loaded_adapter.path).model_dump(mode="json"),
        "authorization_implementation": _binding(repo_root, Path(__file__).resolve()).model_dump(
            mode="json"
        ),
        "extension_policy": decision.policy_artifact.model_dump(mode="json"),
        "extension_implementation": decision.implementation_artifact.model_dump(mode="json"),
        "extension_decision_id": decision.decision_id,
        "extension_decision": _binding(repo_root, extension_decision_path).model_dump(mode="json"),
        "activation_v2_decision_id": decision.activation_v2_decision_id,
        "activation_v2_decision": decision.activation_v2_decision.model_dump(mode="json"),
        "activation_action": decision.activation_action,
        "original_observation_count": 12,
        "extension_prefix_observations": tuple(
            item.model_dump(mode="json") for item in decision.extension_observations
        ),
        "extension_prefix_length": prefix_length,
        "source_action": decision.action.value,
        "authorized_tranche": decision.next_tranche.model_dump(mode="json"),
        "pool_pin": pool_pin.model_dump(mode="json"),
        "family_pins": tuple(item.model_dump(mode="json") for item in policy.family_pins),
        "source_matrix": matrix_binding.model_dump(mode="json"),
        "scientific_tranche_authorized": True,
        "collector_v5_compatible": False,
        "postprocess_v6_compatible": False,
        "config_plan_adapter_only": True,
        "executable_collection_adapter_available": False,
        "required_future_collector_schema_version": 6,
        "required_future_postprocess_schema_version": 7,
        "execution_blocker": ("reviewed_collector_v6_and_postprocess_v7_not_implemented"),
        "decision_inputs_used": decision.decision_inputs_used,
        "forbidden_inputs_used": (),
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    authorization_id = "lf021_reviewed_extension_collection_authorization_v1:" + hash_canonical(
        {
            "schema": "lf021_reviewed_extension_collection_authorization_v1",
            **payload,
        }
    )
    return ReviewedExtensionCollectionAuthorizationV1.model_validate(
        {"authorization_id": authorization_id, **payload}
    )


def write_reviewed_extension_collection_authorization_v1(
    *,
    repo_root: Path,
    authorization_policy_path: Path,
    extension_decision_path: Path,
    output_root: Path | None = None,
) -> ReviewedAuthorizationRunV1:
    """Persist one reviewed authorization at its content-addressed path."""

    authorization = review_extension_collect_next_decision_v1(
        repo_root=repo_root,
        authorization_policy_path=authorization_policy_path,
        extension_decision_path=extension_decision_path,
    )
    loaded = load_post_exhaustion_collection_policy_v1(authorization_policy_path)
    root = output_root or repo_root / loaded.config.authorization_output_root
    path = root / f"{authorization.authorization_id.rsplit(':', 1)[-1]}.json"
    _write_immutable(
        path,
        canonical_json_bytes(authorization.model_dump(mode="json")),
    )
    return ReviewedAuthorizationRunV1(
        authorization=authorization,
        path=path,
        binding=_binding(repo_root, path),
    )


def load_verified_reviewed_extension_collection_authorization_v1(
    *,
    repo_root: Path,
    authorization_policy_path: Path,
    authorization_path: Path,
) -> ReviewedAuthorizationRunV1:
    """Load one record and reproduce it from its bound collect decision."""

    record = ReviewedExtensionCollectionAuthorizationV1.model_validate(
        _strict_json_object(authorization_path)
    )
    decision_path = _verify_binding(repo_root, record.extension_decision)
    replayed = review_extension_collect_next_decision_v1(
        repo_root=repo_root,
        authorization_policy_path=authorization_policy_path,
        extension_decision_path=decision_path,
    )
    if replayed != record:
        raise PostExhaustionCollectionV1Error(
            "reviewed authorization differs from strict decision replay"
        )
    return ReviewedAuthorizationRunV1(
        authorization=record,
        path=authorization_path,
        binding=_binding(repo_root, authorization_path),
    )


def verify_extension_collection_authorizations_v1(
    *,
    repo_root: Path,
    policy_path: Path,
    extension_stop_decision_path: Path,
    authorization_paths: tuple[Path, ...],
) -> VerifiedExtensionCollectionAuthorizationsV1:
    """Verify the exact ordered per-tranche authorizations of a preferred stop."""

    loaded_adapter = load_post_exhaustion_collection_policy_v1(policy_path)
    loaded_extension = _verify_policy(
        repo_root=repo_root,
        loaded_policy=loaded_adapter,
    )
    preferred_stop = extension.verify_extended_stop_for_frame_v3(
        repo_root=repo_root,
        policy_path=loaded_extension.path,
        decision_path=extension_stop_decision_path,
    )
    extension_observations = preferred_stop.decision.extension_observations
    if len(authorization_paths) != len(extension_observations):
        raise PostExhaustionCollectionV1Error(
            "authorization count differs from preferred-stop extension prefix"
        )
    runs = tuple(
        load_verified_reviewed_extension_collection_authorization_v1(
            repo_root=repo_root,
            authorization_policy_path=policy_path,
            authorization_path=path,
        )
        for path in authorization_paths
    )
    records = tuple(item.authorization for item in runs)
    policy = loaded_extension.config
    for index, record in enumerate(records):
        if (
            record.extension_prefix_observations != extension_observations[:index]
            or record.authorized_tranche != policy.extension_tranches[index]
            or extension_observations[index].tranche_id != record.authorized_tranche.tranche_id
            or record.activation_v2_decision_id != preferred_stop.decision.activation_v2_decision_id
            or record.activation_v2_decision != preferred_stop.decision.activation_v2_decision
        ):
            raise PostExhaustionCollectionV1Error(
                "authorization sequence differs from preferred-stop lineage"
            )
    return VerifiedExtensionCollectionAuthorizationsV1(
        preferred_stop=preferred_stop,
        records=records,
        bindings=tuple(item.binding for item in runs),
        postprocess_observations=extension_observations,
        paths=tuple(item.path for item in runs),
    )


def _template_binding_for_pool(
    policy: PostExhaustionCollectionPolicyV1,
    pool_id: str,
) -> tranche_v1.ArtifactBinding:
    return _pins_by_pool(policy.base_collection_templates)[pool_id]


def _load_problem_records(path: Path) -> tuple[ProblemPoolRecord, ...]:
    records: list[ProblemPoolRecord] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(ProblemPoolRecord.model_validate_json(line))
        except ValueError as exc:
            raise PostExhaustionCollectionV1Error(
                f"invalid problem-pool row {path}:{line_number}"
            ) from exc
    result = tuple(records)
    if not result:
        raise PostExhaustionCollectionV1Error("problem-pool template is empty")
    return result


def write_post_exhaustion_collection_config_plan_v1(
    *,
    repo_root: Path,
    authorization_policy_path: Path,
    authorization_path: Path,
    frozen_at: datetime.datetime,
    output_root: Path | None = None,
) -> PostExhaustionCollectionConfigPlanRunV1:
    """Build a deterministic, execution-disabled collector-v6 config and plan."""

    require_utc(frozen_at)
    loaded_adapter = load_post_exhaustion_collection_policy_v1(authorization_policy_path)
    _verify_policy(repo_root=repo_root, loaded_policy=loaded_adapter)
    reviewed = load_verified_reviewed_extension_collection_authorization_v1(
        repo_root=repo_root,
        authorization_policy_path=authorization_policy_path,
        authorization_path=authorization_path,
    )
    authorization = reviewed.authorization
    policy = loaded_adapter.config
    template_binding = _template_binding_for_pool(policy, authorization.authorized_tranche.pool_id)
    template_path = _verify_binding(repo_root, template_binding)
    try:
        template = collection_v5.load_research_collection_v5(
            template_path,
            repo_root=repo_root,
        )
    except (collection_v5.ResearchCollectionV5Error, OSError, ValueError) as exc:
        raise PostExhaustionCollectionV1Error("base collector-v5 template does not replay") from exc
    template_config = template.config.config
    expected_dialect = _POOL_DIALECTS[authorization.authorized_tranche.pool_id]
    if (
        template_config.problem_pool_contract.pool_dialect != expected_dialect
        or template_config.problem_pool_records.sha256 != authorization.pool_pin.records.sha256
        or template_config.problem_pool_manifest.sha256 != authorization.pool_pin.manifest.sha256
        or len(template.problems) != authorization.pool_pin.problem_count
    ):
        raise PostExhaustionCollectionV1Error(
            "base collection template differs from authorized pool"
        )
    family_by_id = {item.family_id: item for item in template_config.families}
    families = tuple(
        family_by_id[family_id].model_copy(
            update={"seeds": (authorization.authorized_tranche.seeds_by_family[family_id],)}
        )
        for family_id in _EXPECTED_FAMILIES
    )
    source_matrix = authorization.source_matrix
    config_payload: dict[str, Any] = {
        "schema_version": 6,
        "authorization": reviewed.binding.model_dump(mode="json"),
        "authorization_id": authorization.authorization_id,
        "extension_decision": authorization.extension_decision.model_dump(mode="json"),
        "extension_decision_id": authorization.extension_decision_id,
        "base_collection_template": template_binding.model_dump(mode="json"),
        "planning_implementation": _binding(repo_root, Path(__file__).resolve()).model_dump(
            mode="json"
        ),
        "tranche_id": authorization.authorized_tranche.tranche_id,
        "pool_id": authorization.authorized_tranche.pool_id,
        "pool_dialect": expected_dialect,
        "frozen_at": frozen_at.isoformat().replace("+00:00", "Z"),
        "artifact_class": "research",
        "collection_scope": ("post_exhaustion_closed_pool_three_local_family_tranche_v6"),
        "shared_execution_record_schema": "lf021_research_execution_records_v1",
        "status": "blocked_pending_reviewed_execution_adapter",
        "execution_enabled": False,
        "execution_blocker": ("reviewed_collector_v6_and_postprocess_v7_not_implemented"),
        "problem_pool_contract": template_config.problem_pool_contract.model_dump(mode="json"),
        "problem_pool_records": authorization.pool_pin.records.model_dump(mode="json"),
        "problem_pool_manifest": authorization.pool_pin.manifest.model_dump(mode="json"),
        "context": template_config.context.model_dump(mode="json"),
        "import_header": template_config.import_header.model_dump(mode="json"),
        "source_matrix": source_matrix.model_dump(mode="json"),
        "runtime": template_config.runtime.model_dump(mode="json"),
        "families": tuple(item.model_dump(mode="json") for item in families),
        "retry": template_config.retry.model_dump(mode="json"),
        "outputs": {
            "root": (
                f"data/raw/real_outputs/"
                f"{_POOL_SLUGS[authorization.authorized_tranche.pool_id]}/v6/"
                f"{authorization.authorized_tranche.tranche_id}/local_collection"
            ),
            "preflight_report": (
                "reports/generation/"
                f"lf021_local_research_collection_preflight_"
                f"{authorization.authorized_tranche.tranche_id}_v6.json"
            ),
        },
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    config_id = "lf021_post_exhaustion_collection_config_v6:" + hash_canonical(
        {
            "schema": "lf021_post_exhaustion_collection_config_v6",
            **config_payload,
        }
    )
    config = PostExhaustionCollectionConfigV6.model_validate(
        {"config_id": config_id, **config_payload}
    )
    root = output_root or repo_root / policy.config_plan_output_root
    config_path = root / "configs" / f"{config_id.rsplit(':', 1)[-1]}.json"
    _write_immutable(config_path, canonical_json_bytes(config.model_dump(mode="json")))
    config_binding = _binding(repo_root, config_path)

    family_bindings = tuple(
        collection_v1._family_binding(
            family=family,
            loaded=template.qualifications[family.family_id],
            config_file_sha256=hash_file(
                _verify_binding(repo_root, family.qualification_pin_source)
            ),
            runtime=config.runtime,
        )
        for family in families
    )
    header_path = _verify_binding(repo_root, config.import_header)
    invocations = collection_v3._make_invocations(
        config_hash=config.config_hash,
        config=cast(Any, config),
        family_bindings=family_bindings,
        qualifications=template.qualifications,
        problems=template.problems,
        repo_root=repo_root,
        context=template.context,
        header_text=header_path.read_text(encoding="utf-8"),
    )
    problem_ids = tuple(sorted(item.problem_record_id for item in template.problems))
    seed_counts = dict(sorted((item.family_id, len(item.seeds)) for item in config.families))
    plan_payload: dict[str, Any] = {
        "schema_version": 6,
        "config_id": config.config_id,
        "config": config_binding.model_dump(mode="json"),
        "config_hash": config.config_hash,
        "authorization_id": authorization.authorization_id,
        "authorization": reviewed.binding.model_dump(mode="json"),
        "tranche_id": config.tranche_id,
        "pool_id": config.pool_id,
        "pool_dialect": config.pool_dialect,
        "problem_count": len(template.problems),
        "family_count": 3,
        "seed_count_by_family": seed_counts,
        "expected_candidate_count": len(invocations),
        "problem_record_ids": problem_ids,
        "family_bindings": tuple(item.model_dump(mode="json") for item in family_bindings),
        "invocations": tuple(item.model_dump(mode="json") for item in invocations),
        "planning_only": True,
        "execution_enabled": False,
        "execution_blocker": ("reviewed_collector_v6_and_postprocess_v7_not_implemented"),
        "actual_collection_performed": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    plan_id = "lf021_post_exhaustion_collection_plan_v6:" + hash_canonical(
        {
            "schema": "lf021_post_exhaustion_collection_plan_v6",
            **plan_payload,
        }
    )
    plan = PostExhaustionCollectionPlanV6.model_validate({"plan_id": plan_id, **plan_payload})
    plan_path = root / "plans" / f"{plan_id.rsplit(':', 1)[-1]}.json"
    _write_immutable(plan_path, canonical_json_bytes(plan.model_dump(mode="json")))
    return PostExhaustionCollectionConfigPlanRunV1(
        config=config,
        config_path=config_path,
        plan=plan,
        plan_path=plan_path,
    )


__all__ = [
    "PostExhaustionCollectionConfigPlanRunV1",
    "PostExhaustionCollectionConfigV6",
    "PostExhaustionCollectionPlanV6",
    "PostExhaustionCollectionPolicyV1",
    "PostExhaustionCollectionV1Error",
    "ReviewedAuthorizationRunV1",
    "ReviewedExtensionCollectionAuthorizationV1",
    "VerifiedExtensionCollectionAuthorizationsV1",
    "load_post_exhaustion_collection_policy_v1",
    "load_verified_reviewed_extension_collection_authorization_v1",
    "review_extension_collect_next_decision_v1",
    "verify_extension_collection_authorizations_v1",
    "write_post_exhaustion_collection_config_plan_v1",
    "write_reviewed_extension_collection_authorization_v1",
]
