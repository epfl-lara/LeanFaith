"""Preregistered compilation-only tranche expansion for LF-021.

This module decides whether to collect the next *already declared* generation
tranche or freeze a human-prevalence sampling frame.  It deliberately has no
model execution, theorem-faithfulness, proof-search, label-resolution, or Gate
closure capability.

The decision surface is restricted to:

* immutable collection/postprocess lineage;
* parser and Lean-compilation outcomes;
* benchmark screening and exact alpha-identity deduplication;
* generator-family, problem-pool, and deterministic source-path proxy counts.

The exact sequence and stop rule live in a versioned YAML policy.  Observed
immutable postprocess manifests must implement the version-neutral operational
view below and form a complete prefix of that sequence.  A later tranche can
therefore never be substituted for a failed or inconvenient one.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
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
from leanfaith.schemas.nl_lean import ProblemPoolRecord

_HEX64 = r"^[0-9a-f]{64}$"
_FAMILY = r"^[a-z0-9][a-z0-9_]*$"
_POOL = r"^[a-z0-9][a-z0-9_]*$"
_TRANCHE = r"^[a-z0-9][a-z0-9_]*$"
_DECISION_ID = r"^lf021_expansion_decision:[0-9a-f]{64}$"
_FRAME_ID = r"^lf021_prevalence_frame:[0-9a-f]{64}$"
_FRAME_RECORD_ID = r"^lf021_prevalence_item:[0-9a-f]{64}$"


class TrancheExpansionError(RuntimeError):
    """The policy, an observation, or a deterministic output failed closed."""


class ArtifactBinding(StrictModel):
    """Exact bytes consumed by the policy."""

    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class PoolSpec(StrictModel):
    """One separately curated, immutable problem pool."""

    pool_id: str = Field(pattern=_POOL)
    problem_count: int = Field(ge=1)
    records: ArtifactBinding
    manifest: ArtifactBinding
    declared_source_proxies: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.declared_source_proxies != tuple(sorted(set(self.declared_source_proxies))):
            raise ValueError("declared source proxies must be sorted and unique")
        return self


class TrancheSpec(StrictModel):
    """One immutable position in the preregistered collection sequence."""

    order: int = Field(ge=0)
    tranche_id: str = Field(pattern=_TRANCHE)
    pool_id: str = Field(pattern=_POOL)
    seeds_by_family: dict[str, int]
    expected_problem_count: int = Field(ge=1)
    expected_invocations: int = Field(ge=1)
    mandatory_before_stopping: bool = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if list(self.seeds_by_family) != sorted(self.seeds_by_family):
            raise ValueError("tranche family seeds must be sorted")
        if len(self.seeds_by_family) < 3:
            raise ValueError("every tranche requires at least three families")
        if any(re.fullmatch(_FAMILY, family) is None for family in self.seeds_by_family):
            raise ValueError("invalid family ID in tranche")
        if len(set(self.seeds_by_family.values())) < 1:
            raise ValueError("tranche seeds are empty")
        if any(seed < 0 for seed in self.seeds_by_family.values()):
            raise ValueError("tranche seeds must be nonnegative")
        if self.expected_invocations != self.expected_problem_count * len(self.seeds_by_family):
            raise ValueError("expected invocations must equal problems x families")
        return self


class CoveragePolicy(StrictModel):
    """Compilation-only coverage required for the preferred frame."""

    minimum_unique_contribution_per_family: int = Field(ge=1)
    minimum_unique_per_pool: dict[str, int]
    minimum_unique_per_family_pool_cell: int = Field(ge=1)
    minimum_unique_per_declared_source_proxy: int = Field(ge=1)

    @model_validator(mode="after")
    def _sorted(self) -> Self:
        if list(self.minimum_unique_per_pool) != sorted(self.minimum_unique_per_pool):
            raise ValueError("minimum_unique_per_pool must be sorted")
        if any(value < 1 for value in self.minimum_unique_per_pool.values()):
            raise ValueError("pool coverage minima must be positive")
        return self


class FramePolicy(StrictModel):
    """Deterministic target and sampling rule."""

    minimum_size: int = Field(ge=1)
    preferred_size: int = Field(ge=1)
    maximum_size: int = Field(ge=1)
    stratum_definition: Literal["representative_family_x_pool_x_source_proxy"]
    minimum_per_nonempty_stratum: int = Field(ge=1)
    representative_hash_salt: str = Field(min_length=16)
    sampling_hash_salt: str = Field(min_length=16)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.minimum_size <= self.preferred_size <= self.maximum_size:
            raise ValueError("frame sizes must satisfy minimum <= preferred <= maximum")
        return self


class TrancheExpansionPolicy(StrictModel):
    """Versioned LF-021 decision policy."""

    schema_version: Literal[1] = 1
    policy_id: Literal["lf021_compilation_only_tranche_expansion_v1"]
    status: Literal["frozen"]
    decision_inputs: tuple[
        Literal[
            "parse_status",
            "compile_status",
            "dedup_identity",
            "benchmark_screen",
            "family_id",
            "pool_id",
            "source_proxy",
        ],
        ...,
    ]
    forbidden_inputs: tuple[
        Literal[
            "same_claim",
            "relation",
            "faithfulness_judgment",
            "llm_judgment",
            "human_label",
            "proof_search_result",
        ],
        ...,
    ]
    required_families: tuple[str, ...] = Field(min_length=3)
    pools: tuple[PoolSpec, ...] = Field(min_length=2)
    tranches: tuple[TrancheSpec, ...] = Field(min_length=2)
    coverage: CoveragePolicy
    frame: FramePolicy
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        required_inputs = {
            "parse_status",
            "compile_status",
            "dedup_identity",
            "benchmark_screen",
            "family_id",
            "pool_id",
            "source_proxy",
        }
        if set(self.decision_inputs) != required_inputs or len(self.decision_inputs) != len(
            required_inputs
        ):
            raise ValueError("decision_inputs must be the exact compilation-only allowlist")
        required_forbidden = {
            "same_claim",
            "relation",
            "faithfulness_judgment",
            "llm_judgment",
            "human_label",
            "proof_search_result",
        }
        if set(self.forbidden_inputs) != required_forbidden or len(self.forbidden_inputs) != len(
            required_forbidden
        ):
            raise ValueError("forbidden_inputs must be the exact semantic-label denylist")
        if self.required_families != tuple(sorted(set(self.required_families))):
            raise ValueError("required families must be sorted and unique")
        if len(self.required_families) < 3:
            raise ValueError("LF-021 expansion requires at least three generator families")

        pool_ids = tuple(pool.pool_id for pool in self.pools)
        if pool_ids != tuple(sorted(set(pool_ids))):
            raise ValueError("pools must be sorted by unique pool ID")
        if set(self.coverage.minimum_unique_per_pool) != set(pool_ids):
            raise ValueError("coverage minima must cover every pool")
        pool_by_id = {pool.pool_id: pool for pool in self.pools}

        if tuple(item.order for item in self.tranches) != tuple(range(len(self.tranches))):
            raise ValueError("tranche orders must be consecutive from zero")
        if len({item.tranche_id for item in self.tranches}) != len(self.tranches):
            raise ValueError("tranche IDs must be unique")
        pool_seed_keys: set[tuple[str, tuple[tuple[str, int], ...]]] = set()
        for tranche in self.tranches:
            pool = pool_by_id.get(tranche.pool_id)
            if pool is None:
                raise ValueError(f"tranche references unknown pool: {tranche.pool_id}")
            if tuple(tranche.seeds_by_family) != self.required_families:
                raise ValueError("every tranche must contain the exact required families")
            if tranche.expected_problem_count != pool.problem_count:
                raise ValueError("tranche problem count differs from its pool")
            key = (tranche.pool_id, tuple(tranche.seeds_by_family.items()))
            if key in pool_seed_keys:
                raise ValueError("pool/family seed tranche is repeated")
            pool_seed_keys.add(key)

        mandatory = tuple(item.order for item in self.tranches if item.mandatory_before_stopping)
        if mandatory != tuple(range(len(mandatory))) or len(mandatory) < 2:
            raise ValueError("mandatory tranches must form a prefix of at least two")
        mandatory_pools = {self.tranches[index].pool_id for index in mandatory}
        if mandatory_pools != set(pool_ids):
            raise ValueError("mandatory prefix must exercise every separately curated pool")
        return self


class ObservationBinding(StrictModel):
    """One immutable postprocess manifest assigned to a tranche."""

    tranche_id: str = Field(pattern=_TRANCHE)
    postprocess_manifest: ArtifactBinding
    manifest_id: str
    postprocess_schema_version: int = Field(ge=3)
    input_binding_hash: str = Field(pattern=_HEX64)


class OperationalPostprocessInputView(StrictModel):
    """Version-neutral subset required from a postprocess input binding."""

    problem_pool_manifest: ArtifactBinding
    problem_pool_records: ArtifactBinding
    problem_count: int = Field(ge=1)
    family_count: int = Field(ge=3)
    seed_count_by_family: dict[str, int]
    expected_invocations: int = Field(ge=1)
    family_ids: tuple[str, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.family_ids != tuple(sorted(set(self.family_ids))):
            raise ValueError("operational family IDs must be sorted and unique")
        if list(self.seed_count_by_family) != sorted(self.seed_count_by_family):
            raise ValueError("operational seed counts must be sorted")
        if set(self.seed_count_by_family) != set(self.family_ids):
            raise ValueError("operational seed counts differ from families")
        if self.family_count != len(self.family_ids):
            raise ValueError("operational family count differs from IDs")
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if self.expected_invocations != expected:
            raise ValueError("operational invocation denominator does not reconcile")
        return self


class OperationalPostprocessManifestView(StrictModel):
    """Stable compilation-only view required from operational postprocessors."""

    schema_version: int = Field(ge=3)
    manifest_id: str = Field(pattern=r"^research_postprocess_v[0-9]+_manifest:[0-9a-f]{64}$")
    input_binding: OperationalPostprocessInputView
    input_binding_hash: str = Field(pattern=_HEX64)
    problem_count: int = Field(ge=1)
    family_count: int = Field(ge=3)
    seed_count_by_family: dict[str, int]
    expected_invocations: int = Field(ge=1)
    terminal_invocations: int = Field(ge=1)
    terminal_artifacts: dict[str, str]
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (
            self.problem_count != self.input_binding.problem_count
            or self.family_count != self.input_binding.family_count
            or self.seed_count_by_family != self.input_binding.seed_count_by_family
            or self.expected_invocations != self.input_binding.expected_invocations
            or self.terminal_invocations != self.expected_invocations
        ):
            raise ValueError("operational manifest denominator differs from input binding")
        if list(self.terminal_artifacts) != sorted(self.terminal_artifacts):
            raise ValueError("operational terminal bindings must be sorted")
        if len(self.terminal_artifacts) != self.expected_invocations:
            raise ValueError("operational terminal artifact denominator differs")
        if any(re.fullmatch(_HEX64, value) is None for value in self.terminal_artifacts.values()):
            raise ValueError("operational terminal artifact hash is invalid")
        return self


class OperationalPostprocessTerminalView(StrictModel):
    """Stable nonsemantic terminal view required from operational postprocessors."""

    schema_version: int = Field(ge=3)
    terminal_id: str
    invocation_id: str
    family_id: str
    problem_record_id: str
    seed: int = Field(ge=0)
    status: str
    parser_executed: bool
    lean_validation_executed: bool
    screening_executed: bool
    output_artifact_hashes: dict[str, str]
    candidate_theorem_id: str | None
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if list(self.output_artifact_hashes) != sorted(self.output_artifact_hashes):
            raise ValueError("operational output bindings must be sorted")
        if any(
            re.fullmatch(_HEX64, value) is None for value in self.output_artifact_hashes.values()
        ):
            raise ValueError("operational output artifact hash is invalid")
        if self.screening_executed and not self.lean_validation_executed:
            raise ValueError("screening cannot precede Lean validation")
        if self.lean_validation_executed and not self.parser_executed:
            raise ValueError("Lean validation cannot precede parsing")
        return self


class OperationalCounts(StrictModel):
    """Only nonsemantic statistics used by the expansion rule."""

    observed_tranche_count: int = Field(ge=0)
    total_invocations: int = Field(ge=0)
    raw_collected_count: int = Field(ge=0)
    parser_success_count: int = Field(ge=0)
    compile_success_count: int = Field(ge=0)
    benchmark_rejected_count: int = Field(ge=0)
    benchmark_clear_compile_count: int = Field(ge=0)
    duplicate_member_count: int = Field(ge=0)
    unique_compiling_count: int = Field(ge=0)
    unique_contribution_by_family: dict[str, int]
    unique_representative_by_family: dict[str, int]
    unique_contribution_by_pool: dict[str, int]
    unique_representative_by_pool: dict[str, int]
    unique_contribution_by_family_pool: dict[str, int]
    unique_contribution_by_source_proxy: dict[str, int]

    @model_validator(mode="after")
    def _sorted(self) -> Self:
        for field_name in (
            "unique_contribution_by_family",
            "unique_representative_by_family",
            "unique_contribution_by_pool",
            "unique_representative_by_pool",
            "unique_contribution_by_family_pool",
            "unique_contribution_by_source_proxy",
        ):
            mapping = getattr(self, field_name)
            if list(mapping) != sorted(mapping):
                raise ValueError(f"{field_name} must be sorted")
            if any(value < 0 for value in mapping.values()):
                raise ValueError(f"{field_name} contains a negative count")
        if self.benchmark_clear_compile_count != (
            self.compile_success_count - self.benchmark_rejected_count
        ):
            raise ValueError("benchmark-clear count does not reconcile")
        if self.duplicate_member_count != (
            self.benchmark_clear_compile_count - self.unique_compiling_count
        ):
            raise ValueError("deduplication count does not reconcile")
        if not (
            self.unique_compiling_count
            <= self.benchmark_clear_compile_count
            <= self.compile_success_count
            <= self.parser_success_count
            <= self.raw_collected_count
            <= self.total_invocations
        ):
            raise ValueError("stage counts are not monotone")
        return self


class DecisionAction(StrEnum):
    """Next operation selected without semantic information."""

    COLLECT_NEXT_TRANCHE = "collect_next_tranche"
    FREEZE_PREFERRED_FRAME = "freeze_preferred_frame"
    FREEZE_REDUCED_FRAME = "freeze_reduced_frame"
    EXHAUSTED_WITHOUT_FRAME = "exhausted_without_frame"


class FrameItem(StrictModel):
    """One deterministic, still-unlabeled item selected for human review."""

    schema_version: Literal[1] = 1
    frame_record_id: str = Field(pattern=_FRAME_RECORD_ID)
    cluster_id: str
    alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    representative_invocation_id: str
    representative_family_id: str
    representative_pool_id: str
    representative_source_proxy: str
    representative_problem_record_id: str
    contributing_invocation_ids: tuple[str, ...] = Field(min_length=1)
    contributing_family_ids: tuple[str, ...] = Field(min_length=1)
    contributing_pool_ids: tuple[str, ...] = Field(min_length=1)
    contributing_source_proxies: tuple[str, ...] = Field(min_length=1)
    postprocess_manifest_ids: tuple[str, ...] = Field(min_length=1)
    terminal_artifact: ArtifactBinding
    screening_artifact: ArtifactBinding
    representation_artifact: ArtifactBinding
    sampling_stratum: str
    sampling_rank_hash: str = Field(pattern=_HEX64)
    stratum_population_size: int = Field(ge=1)
    stratum_sample_size: int = Field(ge=1)
    inclusion_probability_numerator: int = Field(ge=1)
    inclusion_probability_denominator: int = Field(ge=1)
    same_claim: None = None
    relation: None = None
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        for field_name in (
            "contributing_invocation_ids",
            "contributing_family_ids",
            "contributing_pool_ids",
            "contributing_source_proxies",
            "postprocess_manifest_ids",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        if self.stratum_sample_size > self.stratum_population_size:
            raise ValueError("stratum sample exceeds population")
        if (
            self.inclusion_probability_numerator != self.stratum_sample_size
            or self.inclusion_probability_denominator != self.stratum_population_size
        ):
            raise ValueError("inclusion propensity must equal n_h / N_h")
        expected = "lf021_prevalence_item:" + hash_canonical(
            {
                "schema": "lf021_prevalence_frame_item_v1",
                **self.model_dump(mode="json", exclude={"frame_record_id"}),
            }
        )
        if self.frame_record_id != expected:
            raise ValueError("frame record ID differs from content")
        return self


class FrameBinding(StrictModel):
    """Content-addressed prevalence frame."""

    frame_id: str = Field(pattern=_FRAME_ID)
    artifact: str
    sha256: str = Field(pattern=_HEX64)
    item_count: int = Field(ge=1)
    sampling_method: Literal["stratified_hash_srs_without_replacement_v1"]
    propensity_definition: Literal["stratum_sample_size/stratum_population_size"]


class ExpansionDecision(StrictModel):
    """Immutable decision and optional frame binding."""

    schema_version: Literal[1] = 1
    decision_id: str = Field(pattern=_DECISION_ID)
    policy_id: Literal["lf021_compilation_only_tranche_expansion_v1"]
    policy_artifact: ArtifactBinding
    implementation_artifact: ArtifactBinding
    observations: tuple[ObservationBinding, ...]
    counts: OperationalCounts
    coverage_deficits: tuple[str, ...]
    action: DecisionAction
    next_tranche: TrancheSpec | None
    frame: FrameBinding | None
    reduced_data_ablation: bool
    reduced_data_flags: tuple[str, ...]
    decision_inputs_used: tuple[str, ...]
    forbidden_inputs_used: tuple[()] = ()
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.action is DecisionAction.COLLECT_NEXT_TRANCHE:
            if self.next_tranche is None or self.frame is not None:
                raise ValueError("collection decision requires next tranche and no frame")
        elif self.action in {
            DecisionAction.FREEZE_PREFERRED_FRAME,
            DecisionAction.FREEZE_REDUCED_FRAME,
        }:
            if self.next_tranche is not None or self.frame is None:
                raise ValueError("frame decision requires frame and no next tranche")
        elif self.next_tranche is not None or self.frame is not None:
            raise ValueError("exhausted decision has no next tranche or frame")
        if (
            (self.action is DecisionAction.FREEZE_REDUCED_FRAME) != self.reduced_data_ablation
            and self.action is not DecisionAction.EXHAUSTED_WITHOUT_FRAME
        ):
            raise ValueError("reduced-data flag differs from frame action")
        if self.action is DecisionAction.EXHAUSTED_WITHOUT_FRAME and not (
            self.reduced_data_ablation and self.reduced_data_flags
        ):
            raise ValueError("exhausted no-frame outcome must be explicitly reduced")
        if self.forbidden_inputs_used:
            raise ValueError("semantic inputs are forbidden")
        expected = "lf021_expansion_decision:" + hash_canonical(
            {
                "schema": "lf021_expansion_decision_v1",
                **self.model_dump(mode="json", exclude={"decision_id"}),
            }
        )
        if self.decision_id != expected:
            raise ValueError("decision ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class LoadedObservation:
    """Verified postprocess artifact and its nonsemantic candidate rows."""

    tranche: TrancheSpec
    binding: ObservationBinding
    manifest: OperationalPostprocessManifestView
    terminals: tuple[OperationalPostprocessTerminalView, ...]
    problem_source_proxies: dict[str, str]
    candidates: tuple[_CandidateMember, ...]


@dataclass(frozen=True, slots=True)
class _CandidateMember:
    invocation_id: str
    family_id: str
    pool_id: str
    source_proxy: str
    problem_record_id: str
    alpha_identity_fingerprint: str
    postprocess_manifest_id: str
    terminal_artifact: ArtifactBinding
    screening_artifact: ArtifactBinding
    representation_artifact: ArtifactBinding


@dataclass(frozen=True, slots=True)
class _CandidateCluster:
    cluster_id: str
    alpha_identity_fingerprint: str
    representative: _CandidateMember
    members: tuple[_CandidateMember, ...]


@dataclass(frozen=True, slots=True)
class ExpansionRun:
    """Written decision plus optional frame and Markdown report."""

    decision: ExpansionDecision
    decision_path: Path
    report_path: Path
    frame_path: Path | None


def _resolve_repo_artifact(repo_root: Path, artifact: str) -> Path:
    path = Path(artifact)
    if not path.is_absolute():
        path = repo_root / path
    resolved = path.resolve()
    root = repo_root.resolve()
    if not Path(artifact).is_absolute() and not resolved.is_relative_to(root):
        raise TrancheExpansionError(f"repo-relative artifact escapes root: {artifact}")
    return resolved


def _relative_or_absolute(repo_root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(repo_root.resolve()))
    except ValueError:
        return str(resolved)


def _verify_binding(repo_root: Path, binding: ArtifactBinding) -> Path:
    path = _resolve_repo_artifact(repo_root, binding.artifact)
    if not path.is_file():
        raise TrancheExpansionError(f"bound artifact is missing: {binding.artifact}")
    observed = hash_file(path)
    if observed != binding.sha256:
        raise TrancheExpansionError(
            f"bound artifact hash changed: {binding.artifact}; "
            f"expected={binding.sha256}; observed={observed}"
        )
    return path


def load_tranche_expansion_policy(
    path: Path,
) -> LoadedConfig[TrancheExpansionPolicy]:
    """Load the frozen policy and verify its curated pools immediately."""

    return load_config(path, TrancheExpansionPolicy)


def _pool_map(policy: TrancheExpansionPolicy) -> dict[str, PoolSpec]:
    return {pool.pool_id: pool for pool in policy.pools}


def _artifact_from_output(
    *,
    repo_root: Path,
    output_hashes: dict[str, str],
    suffix: str,
) -> ArtifactBinding:
    matches = [
        ArtifactBinding(artifact=artifact, sha256=digest)
        for artifact, digest in output_hashes.items()
        if artifact.endswith(suffix)
    ]
    if len(matches) != 1:
        raise TrancheExpansionError(
            f"expected exactly one {suffix} output artifact, found {len(matches)}"
        )
    _verify_binding(repo_root, matches[0])
    return matches[0]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrancheExpansionError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TrancheExpansionError(f"JSON artifact is not an object: {path}")
    return value


def _artifact_binding_from_raw(value: object, *, field_name: str) -> ArtifactBinding:
    """Project a versioned artifact object onto the stable hash binding."""

    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an artifact-binding object")
    try:
        payload = {
            "artifact": value["artifact"],
            "sha256": value["sha256"],
        }
    except KeyError as exc:
        raise ValueError(f"{field_name} lacks {exc.args[0]}") from exc
    return ArtifactBinding.model_validate(payload)


def _operational_manifest_view(document: dict[str, Any]) -> OperationalPostprocessManifestView:
    """Extract only the version-neutral operational fields from a manifest."""

    input_binding = document.get("input_binding")
    if not isinstance(input_binding, dict):
        raise ValueError("postprocess manifest lacks an input_binding object")
    terminal_artifacts = document.get("terminal_artifacts")
    if not isinstance(terminal_artifacts, dict):
        raise ValueError("postprocess manifest lacks terminal_artifacts")
    input_view = OperationalPostprocessInputView.model_validate(
        {
            "problem_pool_manifest": _artifact_binding_from_raw(
                input_binding.get("problem_pool_manifest"),
                field_name="input_binding.problem_pool_manifest",
            ).model_dump(mode="json"),
            "problem_pool_records": _artifact_binding_from_raw(
                input_binding.get("problem_pool_records"),
                field_name="input_binding.problem_pool_records",
            ).model_dump(mode="json"),
            "problem_count": input_binding.get("problem_count"),
            "family_count": input_binding.get("family_count"),
            "seed_count_by_family": input_binding.get("seed_count_by_family"),
            "expected_invocations": input_binding.get("expected_invocations"),
            "family_ids": input_binding.get("family_ids"),
        }
    )
    return OperationalPostprocessManifestView.model_validate(
        {
            "schema_version": document.get("schema_version"),
            "manifest_id": document.get("manifest_id"),
            "input_binding": input_view.model_dump(mode="json"),
            "input_binding_hash": document.get("input_binding_hash"),
            "problem_count": document.get("problem_count"),
            "family_count": document.get("family_count"),
            "seed_count_by_family": document.get("seed_count_by_family"),
            "expected_invocations": document.get("expected_invocations"),
            "terminal_invocations": document.get("terminal_invocations"),
            "terminal_artifacts": terminal_artifacts,
            "semantic_labels_created": document.get("semantic_labels_created"),
            "supervision_eligible": document.get("supervision_eligible"),
            "gate_5g_credit_claimed": document.get("gate_5g_credit_claimed"),
            "gate_5_closed": document.get("gate_5_closed"),
        }
    )


def _operational_terminal_view(document: dict[str, Any]) -> OperationalPostprocessTerminalView:
    """Extract only the version-neutral nonsemantic fields from a terminal."""

    return OperationalPostprocessTerminalView.model_validate(
        {
            "schema_version": document.get("schema_version"),
            "terminal_id": document.get("terminal_id"),
            "invocation_id": document.get("invocation_id"),
            "family_id": document.get("family_id"),
            "problem_record_id": document.get("problem_record_id"),
            "seed": document.get("seed"),
            "status": document.get("status"),
            "parser_executed": document.get("parser_executed"),
            "lean_validation_executed": document.get("lean_validation_executed"),
            "screening_executed": document.get("screening_executed"),
            "output_artifact_hashes": document.get("output_artifact_hashes"),
            "candidate_theorem_id": document.get("candidate_theorem_id"),
            "semantic_labels_created": document.get("semantic_labels_created"),
            "supervision_eligible": document.get("supervision_eligible"),
            "gate_5g_credit_claimed": document.get("gate_5g_credit_claimed"),
            "gate_5_closed": document.get("gate_5_closed"),
        }
    )


def _load_problem_proxies(
    *,
    repo_root: Path,
    pool: PoolSpec,
) -> dict[str, str]:
    records_path = _verify_binding(repo_root, pool.records)
    _verify_binding(repo_root, pool.manifest)
    try:
        lines = records_path.read_text(encoding="utf-8").splitlines()
        records = tuple(
            ProblemPoolRecord.model_validate(json.loads(line)) for line in lines if line.strip()
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise TrancheExpansionError(f"invalid problem-pool records: {pool.pool_id}") from exc
    if len(records) != pool.problem_count:
        raise TrancheExpansionError(f"problem count differs for pool {pool.pool_id}")
    result: dict[str, str] = {}
    for record in records:
        proxy = record.metadata.get("domain_proxy")
        if not isinstance(proxy, str) or not proxy:
            raise TrancheExpansionError(
                f"problem lacks deterministic domain_proxy: {record.problem_record_id}"
            )
        if proxy not in pool.declared_source_proxies:
            raise TrancheExpansionError(f"undeclared source proxy {proxy!r} in pool {pool.pool_id}")
        if record.problem_record_id in result:
            raise TrancheExpansionError("duplicate problem record ID in pool")
        result[record.problem_record_id] = proxy
    observed = tuple(sorted(set(result.values())))
    if observed != pool.declared_source_proxies:
        raise TrancheExpansionError(
            f"declared source proxies differ from pool records: {pool.pool_id}"
        )
    return result


def load_postprocess_observation(
    *,
    repo_root: Path,
    policy: TrancheExpansionPolicy,
    tranche: TrancheSpec,
    manifest_path: Path,
) -> LoadedObservation:
    """Verify and reduce one operational postprocess manifest to allowed data."""

    manifest_path = manifest_path.resolve()
    manifest_sha = hash_file(manifest_path)
    try:
        manifest = _operational_manifest_view(_read_json(manifest_path))
    except ValueError as exc:
        raise TrancheExpansionError(
            f"invalid operational postprocess manifest: {manifest_path}"
        ) from exc
    if (
        manifest.semantic_labels_created
        or manifest.supervision_eligible
        or manifest.gate_5g_credit_claimed
        or manifest.gate_5_closed
    ):
        raise TrancheExpansionError("postprocess observation is not unlabeled/non-gating")

    pool = _pool_map(policy)[tranche.pool_id]
    if manifest.problem_count != pool.problem_count:
        raise TrancheExpansionError("observed problem count differs from tranche")
    if tuple(manifest.input_binding.family_ids) != policy.required_families:
        raise TrancheExpansionError("observed families differ from the policy")
    if set(manifest.seed_count_by_family) != set(policy.required_families) or any(
        count != 1 for count in manifest.seed_count_by_family.values()
    ):
        raise TrancheExpansionError("each observed tranche must contain one seed per family")
    if manifest.expected_invocations != tranche.expected_invocations:
        raise TrancheExpansionError("observed invocation denominator differs from tranche")
    if (
        manifest.input_binding.problem_pool_records.sha256 != pool.records.sha256
        or manifest.input_binding.problem_pool_manifest.sha256 != pool.manifest.sha256
    ):
        raise TrancheExpansionError("observed pool hashes differ from the tranche")

    proxies = _load_problem_proxies(repo_root=repo_root, pool=pool)
    terminal_rows: list[tuple[OperationalPostprocessTerminalView, ArtifactBinding]] = []
    for artifact, digest in manifest.terminal_artifacts.items():
        binding = ArtifactBinding(artifact=artifact, sha256=digest)
        path = _verify_binding(repo_root, binding)
        try:
            terminal = _operational_terminal_view(_read_json(path))
        except ValueError as exc:
            raise TrancheExpansionError(f"invalid operational terminal: {artifact}") from exc
        terminal_rows.append((terminal, binding))
    terminals = tuple(
        item[0]
        for item in sorted(
            terminal_rows,
            key=lambda row: row[0].invocation_id,
        )
    )
    terminal_binding_by_id = {item[0].invocation_id: item[1] for item in terminal_rows}
    if len(terminals) != tranche.expected_invocations:
        raise TrancheExpansionError("terminal denominator differs from tranche")
    if len(terminal_binding_by_id) != len(terminals):
        raise TrancheExpansionError("duplicate invocation ID in operational terminals")

    observed_seeds: dict[str, set[int]] = defaultdict(set)
    candidates: list[_CandidateMember] = []
    for terminal in terminals:
        observed_seeds[terminal.family_id].add(terminal.seed)
        source_proxy = proxies.get(terminal.problem_record_id)
        if source_proxy is None:
            raise TrancheExpansionError("terminal references a problem outside its bound pool")
        if not terminal.screening_executed:
            continue
        screening_binding = _artifact_from_output(
            repo_root=repo_root,
            output_hashes=terminal.output_artifact_hashes,
            suffix="/screening.json",
        )
        representation_binding = _artifact_from_output(
            repo_root=repo_root,
            output_hashes=terminal.output_artifact_hashes,
            suffix="/materialized_representation.json",
        )
        screening = _read_json(_resolve_repo_artifact(repo_root, screening_binding.artifact))
        representation = _read_json(
            _resolve_repo_artifact(repo_root, representation_binding.artifact)
        )
        benchmark_hits = screening.get("benchmark_hits")
        if not isinstance(benchmark_hits, list):
            raise TrancheExpansionError("screening benchmark_hits is not a list")
        alpha = representation.get("alpha_identity_fingerprint")
        if not isinstance(alpha, str) or re.fullmatch(_HEX64, alpha) is None:
            raise TrancheExpansionError("compiled candidate lacks alpha fingerprint")
        if screening.get("alpha_identity_fingerprint") != alpha:
            raise TrancheExpansionError("screening/representation alpha mismatch")
        if terminal.candidate_theorem_id != screening.get("candidate_theorem_id"):
            raise TrancheExpansionError("terminal/screening theorem identity mismatch")
        if benchmark_hits:
            continue
        candidates.append(
            _CandidateMember(
                invocation_id=terminal.invocation_id,
                family_id=terminal.family_id,
                pool_id=tranche.pool_id,
                source_proxy=source_proxy,
                problem_record_id=terminal.problem_record_id,
                alpha_identity_fingerprint=alpha,
                postprocess_manifest_id=manifest.manifest_id,
                terminal_artifact=terminal_binding_by_id[terminal.invocation_id],
                screening_artifact=screening_binding,
                representation_artifact=representation_binding,
            )
        )

    expected_seeds = {family: {seed} for family, seed in tranche.seeds_by_family.items()}
    if dict(sorted(observed_seeds.items())) != expected_seeds:
        raise TrancheExpansionError(
            f"observed seeds differ for tranche {tranche.tranche_id}: "
            f"expected={expected_seeds}; observed={dict(observed_seeds)}"
        )
    observation_binding = ObservationBinding(
        tranche_id=tranche.tranche_id,
        postprocess_manifest=ArtifactBinding(
            artifact=_relative_or_absolute(repo_root, manifest_path),
            sha256=manifest_sha,
        ),
        manifest_id=manifest.manifest_id,
        postprocess_schema_version=manifest.schema_version,
        input_binding_hash=manifest.input_binding_hash,
    )
    return LoadedObservation(
        tranche=tranche,
        binding=observation_binding,
        manifest=manifest,
        terminals=terminals,
        problem_source_proxies=dict(sorted(proxies.items())),
        candidates=tuple(sorted(candidates, key=lambda item: item.invocation_id)),
    )


def _cluster_candidates(
    observations: tuple[LoadedObservation, ...],
    *,
    representative_hash_salt: str,
) -> tuple[_CandidateCluster, ...]:
    by_alpha: dict[str, list[_CandidateMember]] = defaultdict(list)
    seen_invocations: set[str] = set()
    for observation in observations:
        for candidate in observation.candidates:
            if candidate.invocation_id in seen_invocations:
                raise TrancheExpansionError("invocation appears in more than one tranche")
            seen_invocations.add(candidate.invocation_id)
            by_alpha[candidate.alpha_identity_fingerprint].append(candidate)
    clusters: list[_CandidateCluster] = []
    for alpha, raw_members in sorted(by_alpha.items()):
        members = tuple(sorted(raw_members, key=lambda item: item.invocation_id))
        representative = min(
            members,
            key=lambda item: (
                hash_canonical(
                    {
                        "schema": "lf021_dedup_representative_rank_v1",
                        "salt": representative_hash_salt,
                        "invocation_id": item.invocation_id,
                    }
                ),
                item.invocation_id,
            ),
        )
        cluster_id = "candidate_cluster:" + hash_canonical(
            {
                "schema": "lf021_global_alpha_cluster_v1",
                "alpha_identity_fingerprint": alpha,
            }
        )
        clusters.append(
            _CandidateCluster(
                cluster_id=cluster_id,
                alpha_identity_fingerprint=alpha,
                representative=representative,
                members=members,
            )
        )
    return tuple(clusters)


def _operational_counts(
    *,
    policy: TrancheExpansionPolicy,
    observations: tuple[LoadedObservation, ...],
    clusters: tuple[_CandidateCluster, ...],
) -> OperationalCounts:
    families = policy.required_families
    pools = tuple(pool.pool_id for pool in policy.pools)
    parser_success = sum(
        terminal.lean_validation_executed
        for observation in observations
        for terminal in observation.terminals
    )
    compile_success = sum(
        terminal.screening_executed
        for observation in observations
        for terminal in observation.terminals
    )
    benchmark_rejected = compile_success - sum(
        len(observation.candidates) for observation in observations
    )
    benchmark_clear = compile_success - benchmark_rejected

    contribution_family: Counter[str] = Counter()
    representative_family: Counter[str] = Counter()
    contribution_pool: Counter[str] = Counter()
    representative_pool: Counter[str] = Counter()
    contribution_cell: Counter[str] = Counter()
    contribution_proxy: Counter[str] = Counter()
    for cluster in clusters:
        family_ids = {member.family_id for member in cluster.members}
        pool_ids = {member.pool_id for member in cluster.members}
        cells = {f"{member.family_id}|{member.pool_id}" for member in cluster.members}
        proxies = {member.source_proxy for member in cluster.members}
        contribution_family.update(family_ids)
        contribution_pool.update(pool_ids)
        contribution_cell.update(cells)
        contribution_proxy.update(proxies)
        representative_family[cluster.representative.family_id] += 1
        representative_pool[cluster.representative.pool_id] += 1

    return OperationalCounts(
        observed_tranche_count=len(observations),
        total_invocations=sum(item.manifest.expected_invocations for item in observations),
        raw_collected_count=sum(
            terminal.status != "collection_not_raw"
            for observation in observations
            for terminal in observation.terminals
        ),
        parser_success_count=parser_success,
        compile_success_count=compile_success,
        benchmark_rejected_count=benchmark_rejected,
        benchmark_clear_compile_count=benchmark_clear,
        duplicate_member_count=benchmark_clear - len(clusters),
        unique_compiling_count=len(clusters),
        unique_contribution_by_family=dict(
            sorted((family, contribution_family[family]) for family in families)
        ),
        unique_representative_by_family=dict(
            sorted((family, representative_family[family]) for family in families)
        ),
        unique_contribution_by_pool=dict(sorted((pool, contribution_pool[pool]) for pool in pools)),
        unique_representative_by_pool=dict(
            sorted((pool, representative_pool[pool]) for pool in pools)
        ),
        unique_contribution_by_family_pool=dict(
            sorted(
                (
                    f"{family}|{pool}",
                    contribution_cell[f"{family}|{pool}"],
                )
                for family in families
                for pool in pools
            )
        ),
        unique_contribution_by_source_proxy=dict(
            sorted(
                (
                    proxy,
                    contribution_proxy[proxy],
                )
                for pool in policy.pools
                for proxy in pool.declared_source_proxies
            )
        ),
    )


def _coverage_deficits(
    policy: TrancheExpansionPolicy,
    counts: OperationalCounts,
) -> tuple[str, ...]:
    deficits: list[str] = []
    for family, count in counts.unique_contribution_by_family.items():
        minimum = policy.coverage.minimum_unique_contribution_per_family
        if count < minimum:
            deficits.append(f"family:{family}:{count}<{minimum}")
    for pool, count in counts.unique_contribution_by_pool.items():
        minimum = policy.coverage.minimum_unique_per_pool[pool]
        if count < minimum:
            deficits.append(f"pool:{pool}:{count}<{minimum}")
    for cell, count in counts.unique_contribution_by_family_pool.items():
        minimum = policy.coverage.minimum_unique_per_family_pool_cell
        if count < minimum:
            deficits.append(f"family_pool:{cell}:{count}<{minimum}")
    for proxy, count in counts.unique_contribution_by_source_proxy.items():
        minimum = policy.coverage.minimum_unique_per_declared_source_proxy
        if count < minimum:
            deficits.append(f"source_proxy:{proxy}:{count}<{minimum}")
    return tuple(sorted(deficits))


def _allocate_strata(
    sizes: dict[str, int],
    *,
    target: int,
    minimum_per_stratum: int,
) -> dict[str, int]:
    if not sizes or any(value < minimum_per_stratum for value in sizes.values()):
        raise TrancheExpansionError("frame strata do not satisfy their preregistered minimum")
    base_total = minimum_per_stratum * len(sizes)
    if base_total > target or target > sum(sizes.values()):
        raise TrancheExpansionError("frame target is incompatible with stratum populations")
    allocation = dict.fromkeys(sizes, minimum_per_stratum)
    remaining = target - base_total
    capacities = {key: sizes[key] - minimum_per_stratum for key in sizes}
    total_capacity = sum(capacities.values())
    if remaining == 0:
        return dict(sorted(allocation.items()))
    if total_capacity < remaining or total_capacity == 0:
        raise TrancheExpansionError("insufficient stratum capacity")

    remainders: list[tuple[int, str]] = []
    assigned = 0
    for key in sorted(sizes):
        numerator = remaining * capacities[key]
        quotient, remainder = divmod(numerator, total_capacity)
        allocation[key] += quotient
        assigned += quotient
        remainders.append((remainder, key))
    for _remainder, key in sorted(remainders, key=lambda item: (-item[0], item[1]))[
        : remaining - assigned
    ]:
        allocation[key] += 1
    if sum(allocation.values()) != target or any(allocation[key] > sizes[key] for key in sizes):
        raise TrancheExpansionError("Hamilton stratum allocation did not reconcile")
    return dict(sorted(allocation.items()))


def _build_frame_items(
    clusters: tuple[_CandidateCluster, ...],
    *,
    target: int,
    policy: TrancheExpansionPolicy,
) -> tuple[FrameItem, ...]:
    by_stratum: dict[str, list[_CandidateCluster]] = defaultdict(list)
    for cluster in clusters:
        representative = cluster.representative
        stratum = (
            f"{representative.family_id}|{representative.pool_id}|{representative.source_proxy}"
        )
        by_stratum[stratum].append(cluster)
    sizes = dict(sorted((key, len(value)) for key, value in by_stratum.items()))
    if target == len(clusters):
        allocation = sizes
    else:
        allocation = _allocate_strata(
            sizes,
            target=target,
            minimum_per_stratum=policy.frame.minimum_per_nonempty_stratum,
        )

    selected: list[FrameItem] = []
    for stratum in sorted(by_stratum):
        ranked = sorted(
            by_stratum[stratum],
            key=lambda cluster: (
                hash_canonical(
                    {
                        "schema": "lf021_prevalence_sampling_rank_v1",
                        "salt": policy.frame.sampling_hash_salt,
                        "cluster_id": cluster.cluster_id,
                    }
                ),
                cluster.cluster_id,
            ),
        )
        n_h = allocation[stratum]
        n_population = sizes[stratum]
        for cluster in ranked[:n_h]:
            representative = cluster.representative
            rank_hash = hash_canonical(
                {
                    "schema": "lf021_prevalence_sampling_rank_v1",
                    "salt": policy.frame.sampling_hash_salt,
                    "cluster_id": cluster.cluster_id,
                }
            )
            payload: dict[str, Any] = {
                "schema_version": 1,
                "cluster_id": cluster.cluster_id,
                "alpha_identity_fingerprint": cluster.alpha_identity_fingerprint,
                "representative_invocation_id": representative.invocation_id,
                "representative_family_id": representative.family_id,
                "representative_pool_id": representative.pool_id,
                "representative_source_proxy": representative.source_proxy,
                "representative_problem_record_id": representative.problem_record_id,
                "contributing_invocation_ids": tuple(
                    sorted(member.invocation_id for member in cluster.members)
                ),
                "contributing_family_ids": tuple(
                    sorted({member.family_id for member in cluster.members})
                ),
                "contributing_pool_ids": tuple(
                    sorted({member.pool_id for member in cluster.members})
                ),
                "contributing_source_proxies": tuple(
                    sorted({member.source_proxy for member in cluster.members})
                ),
                "postprocess_manifest_ids": tuple(
                    sorted({member.postprocess_manifest_id for member in cluster.members})
                ),
                "terminal_artifact": representative.terminal_artifact.model_dump(mode="json"),
                "screening_artifact": representative.screening_artifact.model_dump(mode="json"),
                "representation_artifact": representative.representation_artifact.model_dump(
                    mode="json"
                ),
                "sampling_stratum": stratum,
                "sampling_rank_hash": rank_hash,
                "stratum_population_size": n_population,
                "stratum_sample_size": n_h,
                "inclusion_probability_numerator": n_h,
                "inclusion_probability_denominator": n_population,
                "same_claim": None,
                "relation": None,
                "semantic_labels_created": False,
                "supervision_eligible": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            }
            frame_id = "lf021_prevalence_item:" + hash_canonical(
                {"schema": "lf021_prevalence_frame_item_v1", **payload}
            )
            selected.append(FrameItem.model_validate({"frame_record_id": frame_id, **payload}))
    result = tuple(sorted(selected, key=lambda item: item.frame_record_id))
    if len(result) != target:
        raise TrancheExpansionError("selected frame does not match target")
    return result


def _jsonl_bytes(records: tuple[FrameItem, ...]) -> bytes:
    return b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )


def evaluate_tranche_expansion(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[TrancheExpansionPolicy],
    observed_manifests: tuple[Path, ...],
) -> tuple[ExpansionDecision, bytes | None]:
    """Evaluate a complete observation prefix and optionally build frame bytes."""

    policy = loaded_policy.config
    if len(observed_manifests) > len(policy.tranches):
        raise TrancheExpansionError("more observations than preregistered tranches")
    for pool in policy.pools:
        _verify_binding(repo_root, pool.records)
        _verify_binding(repo_root, pool.manifest)

    observations = tuple(
        load_postprocess_observation(
            repo_root=repo_root,
            policy=policy,
            tranche=policy.tranches[index],
            manifest_path=path,
        )
        for index, path in enumerate(observed_manifests)
    )
    if len({item.binding.manifest_id for item in observations}) != len(observations):
        raise TrancheExpansionError("postprocess manifest reused across tranches")
    clusters = _cluster_candidates(
        observations,
        representative_hash_salt=policy.frame.representative_hash_salt,
    )
    counts = _operational_counts(policy=policy, observations=observations, clusters=clusters)
    deficits = _coverage_deficits(policy, counts)
    mandatory_count = sum(item.mandatory_before_stopping for item in policy.tranches)
    preferred_ready = (
        len(observations) >= mandatory_count
        and counts.unique_compiling_count >= policy.frame.preferred_size
        and not deficits
    )

    frame_bytes: bytes | None = None
    frame_binding: FrameBinding | None = None
    next_tranche: TrancheSpec | None = None
    flags: list[str] = []
    reduced = False
    if preferred_ready:
        action = DecisionAction.FREEZE_PREFERRED_FRAME
        frame_items = _build_frame_items(
            clusters,
            target=policy.frame.preferred_size,
            policy=policy,
        )
        frame_bytes = _jsonl_bytes(frame_items)
    elif len(observations) < len(policy.tranches):
        action = DecisionAction.COLLECT_NEXT_TRANCHE
        next_tranche = policy.tranches[len(observations)]
    elif counts.unique_compiling_count >= policy.frame.minimum_size:
        action = DecisionAction.FREEZE_REDUCED_FRAME
        reduced = True
        target = min(policy.frame.preferred_size, counts.unique_compiling_count)
        if counts.unique_compiling_count < policy.frame.preferred_size:
            flags.append(
                "preferred_frame_shortfall:"
                f"{counts.unique_compiling_count}<{policy.frame.preferred_size}"
            )
        flags.extend(f"coverage_deficit:{item}" for item in deficits)
        flags.append("preregistered_tranches_exhausted")
        frame_items = _build_frame_items(clusters, target=target, policy=policy)
        frame_bytes = _jsonl_bytes(frame_items)
    else:
        action = DecisionAction.EXHAUSTED_WITHOUT_FRAME
        reduced = True
        flags.extend(f"coverage_deficit:{item}" for item in deficits)
        flags.extend(
            (
                "minimum_frame_shortfall:"
                f"{counts.unique_compiling_count}<{policy.frame.minimum_size}",
                "preregistered_tranches_exhausted",
            )
        )

    if frame_bytes is not None:
        frame_sha = sha256_hex(frame_bytes)
        implementation_sha = hash_file(Path(__file__).resolve())
        frame_id = "lf021_prevalence_frame:" + hash_canonical(
            {
                "schema": "lf021_prevalence_frame_v1",
                "policy_config_hash": loaded_policy.config_hash,
                "implementation_sha256": implementation_sha,
                "observation_manifest_ids": [item.binding.manifest_id for item in observations],
                "frame_sha256": frame_sha,
                "item_count": len(frame_bytes.splitlines()),
            }
        )
        frame_binding = FrameBinding(
            frame_id=frame_id,
            artifact=f"frames/{frame_id.rsplit(':', 1)[-1]}.jsonl",
            sha256=frame_sha,
            item_count=len(frame_bytes.splitlines()),
            sampling_method="stratified_hash_srs_without_replacement_v1",
            propensity_definition="stratum_sample_size/stratum_population_size",
        )

    policy_binding = ArtifactBinding(
        artifact=_relative_or_absolute(repo_root, loaded_policy.path),
        sha256=hash_file(loaded_policy.path),
    )
    implementation_path = Path(__file__).resolve()
    implementation_binding = ArtifactBinding(
        artifact=_relative_or_absolute(repo_root, implementation_path),
        sha256=hash_file(implementation_path),
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "policy_id": policy.policy_id,
        "policy_artifact": policy_binding.model_dump(mode="json"),
        "implementation_artifact": implementation_binding.model_dump(mode="json"),
        "observations": tuple(item.binding.model_dump(mode="json") for item in observations),
        "counts": counts.model_dump(mode="json"),
        "coverage_deficits": deficits,
        "action": action.value,
        "next_tranche": (
            next_tranche.model_dump(mode="json") if next_tranche is not None else None
        ),
        "frame": frame_binding.model_dump(mode="json") if frame_binding is not None else None,
        "reduced_data_ablation": reduced,
        "reduced_data_flags": tuple(sorted(set(flags))),
        "decision_inputs_used": policy.decision_inputs,
        "forbidden_inputs_used": (),
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    decision_id = "lf021_expansion_decision:" + hash_canonical(
        {"schema": "lf021_expansion_decision_v1", **payload}
    )
    decision = ExpansionDecision.model_validate({"decision_id": decision_id, **payload})
    return decision, frame_bytes


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise TrancheExpansionError(f"immutable artifact differs: {path}")
        return
    path.write_bytes(payload)


def _render_report(decision: ExpansionDecision) -> str:
    next_id = decision.next_tranche.tranche_id if decision.next_tranche else "none"
    frame_id = decision.frame.frame_id if decision.frame else "none"
    lines = [
        "# LF-021 compilation-only tranche decision",
        "",
        f"- Decision: `{decision.decision_id}`",
        f"- Action: `{decision.action.value}`",
        f"- Observed tranches: {decision.counts.observed_tranche_count}",
        f"- Next tranche: `{next_id}`",
        f"- Parser successes: {decision.counts.parser_success_count}",
        f"- Lean-compiling candidates: {decision.counts.compile_success_count}",
        f"- Benchmark-clear compiling candidates before global deduplication: "
        f"{decision.counts.benchmark_clear_compile_count}",
        f"- Duplicate candidate members removed: {decision.counts.duplicate_member_count}",
        f"- Globally unique, benchmark-clear candidates: {decision.counts.unique_compiling_count}",
        f"- Frozen frame: `{frame_id}`",
        f"- Reduced-data ablation: `{str(decision.reduced_data_ablation).lower()}`",
        "- Semantic labels inspected: `false`",
        "- Semantic labels created: `false`",
        "- Gate 5G credit claimed: `false`",
        "- Gate 5 closed: `false`",
        "",
        "## Coverage deficits",
        "",
    ]
    if decision.coverage_deficits:
        lines.extend(f"- `{item}`" for item in decision.coverage_deficits)
    else:
        lines.append("- none")
    lines.extend(["", "## Reduced-data flags", ""])
    if decision.reduced_data_flags:
        lines.extend(f"- `{item}`" for item in decision.reduced_data_flags)
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "This report is operational only. Compilation is not a faithfulness label.",
            "",
        ]
    )
    return "\n".join(lines)


def run_tranche_expansion(
    *,
    repo_root: Path,
    policy_path: Path,
    observed_manifests: tuple[Path, ...],
    output_root: Path,
) -> ExpansionRun:
    """Evaluate and write content-addressed decision artifacts."""

    loaded_policy = load_tranche_expansion_policy(policy_path)
    decision, frame_bytes = evaluate_tranche_expansion(
        repo_root=repo_root,
        loaded_policy=loaded_policy,
        observed_manifests=observed_manifests,
    )
    suffix = decision.decision_id.rsplit(":", 1)[-1]
    decision_path = output_root / "decisions" / f"{suffix}.json"
    report_path = output_root / "decisions" / f"{suffix}.md"
    frame_path: Path | None = None
    if decision.frame is not None:
        if frame_bytes is None:
            raise TrancheExpansionError("frame binding exists without frame bytes")
        frame_path = output_root / decision.frame.artifact
        if sha256_hex(frame_bytes) != decision.frame.sha256:
            raise TrancheExpansionError("frame hash changed before persistence")
        _write_immutable(frame_path, frame_bytes)
    _write_immutable(
        decision_path,
        canonical_json_bytes(decision.model_dump(mode="json")),
    )
    _write_immutable(report_path, _render_report(decision).encode("utf-8"))
    return ExpansionRun(
        decision=decision,
        decision_path=decision_path,
        report_path=report_path,
        frame_path=frame_path,
    )


__all__ = [
    "ArtifactBinding",
    "CoveragePolicy",
    "DecisionAction",
    "ExpansionDecision",
    "ExpansionRun",
    "FrameBinding",
    "FrameItem",
    "FramePolicy",
    "LoadedObservation",
    "ObservationBinding",
    "OperationalCounts",
    "OperationalPostprocessInputView",
    "OperationalPostprocessManifestView",
    "OperationalPostprocessTerminalView",
    "PoolSpec",
    "TrancheExpansionError",
    "TrancheExpansionPolicy",
    "TrancheSpec",
    "evaluate_tranche_expansion",
    "load_postprocess_observation",
    "load_tranche_expansion_policy",
    "run_tranche_expansion",
]
