"""Postprocess completed extension collector-v6 bundles through the v3 engine.

This v7 envelope accepts only a completed
:mod:`post_exhaustion_collection_v6` bundle.  It replays the frozen extension
authorization, exact collector denominator, public reference-hidden pool, raw
lineage, parser implementations, active denylist, and immutable shared
postprocess-v3 correctness engine before doing any Lean work.

It creates unresolved operational candidates only.  It does not inspect or
create semantic labels, admit supervision, or claim Gate credit.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation import post_exhaustion_collection_v6 as collection_v6
from leanfaith.generation import research_collection as collection_v1
from leanfaith.generation import research_collection_v3 as collection_v3
from leanfaith.generation import research_collection_v5 as collection_v5
from leanfaith.generation import research_postprocess as postprocess_v1
from leanfaith.generation import research_postprocess_v3 as postprocess_v3
from leanfaith.generation import research_postprocess_v4 as postprocess_v4
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

_HEX64 = r"^[0-9a-f]{64}$"
_TRANCHE = r"^[a-z][a-z0-9_]*$"
_TERMINAL_ID = r"^lf021_post_exhaustion_postprocess_v7_terminal:[0-9a-f]{64}$"
_FAMILY_ID = r"^lf021_post_exhaustion_postprocess_v7_family:[0-9a-f]{64}$"
_MANIFEST_ID = r"^research_postprocess_v7_manifest:[0-9a-f]{64}$"
_COLLECTOR_ARTIFACT = "src/leanfaith/generation/post_exhaustion_collection_v6.py"
_SHARED_PROCESSOR_ARTIFACT = "src/leanfaith/generation/research_postprocess_v3.py"

PoolDialect = collection_v5.PoolDialect
PoolSource = Literal[
    "mathlib_gate3_docstrings_operational_v1",
    "mathlib_cross_domain_docstrings_operational_v1",
]
PoolManifestKind = Literal[
    "lf021_gate3_docstrings_operational_problem_pool_v1",
    "lf021_cross_domain_docstrings_operational_problem_pool_v1",
]


class PostExhaustionPostprocessV7Error(RuntimeError):
    """A collector-v6 input, v7 output, or replay invariant failed."""


class PostExhaustionPostprocessInputBindingV7(StrictModel):
    """Exact extension lineage plus every shared processing dependency."""

    schema_version: Literal[7] = 7
    collector_schema_version: Literal[6] = 6
    collector_plan_record_schema: Literal["lf021_post_exhaustion_collection_plan_v6"] = (
        "lf021_post_exhaustion_collection_plan_v6"
    )
    collector_manifest_record_schema: Literal["lf021_post_exhaustion_collection_manifest_v6"] = (
        "lf021_post_exhaustion_collection_manifest_v6"
    )
    tranche_id: str = Field(pattern=_TRANCHE)
    tranche_order: int = Field(ge=12, le=15)
    pool_id: str
    pool_dialect: PoolDialect
    pool_source: PoolSource
    pool_manifest_artifact_kind: PoolManifestKind
    extension_authorization: postprocess_v1.PostprocessArtifactBinding
    extension_authorization_id: str
    extension_decision: postprocess_v1.PostprocessArtifactBinding
    extension_decision_id: str
    planning_config: postprocess_v1.PostprocessArtifactBinding
    planning_config_id: str
    execution_config: postprocess_v1.PostprocessArtifactBinding
    execution_config_id: str
    execution_config_hash: str = Field(pattern=_HEX64)
    collection_plan: postprocess_v1.PostprocessArtifactBinding
    collection_manifest: postprocess_v1.PostprocessArtifactBinding
    collection_plan_id: str
    collection_plan_hash: str = Field(pattern=_HEX64)
    collection_manifest_id: str = Field(
        pattern=r"^lf021_post_exhaustion_collection_manifest_v6:[0-9a-f]{64}$"
    )
    collection_terminal_artifacts: dict[str, str]
    collection_family_session_artifacts: dict[str, str]
    raw_collection_artifacts_by_invocation: dict[str, dict[str, str]]
    problem_pool_manifest: postprocess_v1.PostprocessArtifactBinding
    problem_pool_records: postprocess_v1.PostprocessArtifactBinding
    context: postprocess_v1.PostprocessArtifactBinding
    import_header: postprocess_v1.PostprocessArtifactBinding
    source_matrix: postprocess_v1.PostprocessArtifactBinding
    reference_theorems: postprocess_v1.PostprocessArtifactBinding
    reference_representations: postprocess_v1.PostprocessArtifactBinding
    active_registry_artifacts: dict[str, postprocess_v1.PostprocessArtifactBinding]
    active_registry_content_hash: str = Field(pattern=_HEX64)
    collector_implementation: postprocess_v1.PostprocessArtifactBinding
    primary_parser_implementations: dict[str, postprocess_v1.PostprocessArtifactBinding]
    recovery_implementation: postprocess_v1.PostprocessArtifactBinding
    shared_processing_implementation: postprocess_v1.PostprocessArtifactBinding
    implementation: postprocess_v1.PostprocessArtifactBinding
    shared_execution_record_schema: Literal["lf021_research_execution_records_v1"] = (
        "lf021_research_execution_records_v1"
    )
    shared_processing_record_schema: Literal["lf021_research_postprocess_records_v3"] = (
        "lf021_research_postprocess_records_v3"
    )
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    seed_count_by_family: dict[str, int]
    expected_invocations: int = Field(ge=1)
    problem_record_ids: tuple[str, ...] = Field(min_length=1)
    invocation_ids: tuple[str, ...] = Field(min_length=1)
    family_ids: tuple[str, ...] = Field(min_length=3, max_length=3)
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @property
    def binding_hash(self) -> str:
        return hash_canonical(
            {
                "schema": "lf021_post_exhaustion_postprocess_input_binding_v7",
                **self.model_dump(mode="json"),
            }
        )

    def shared_v3_binding(self) -> postprocess_v3.ResearchPostprocessV3InputBinding:
        """Project the exact v7 denominator into the immutable v3 engine."""

        return postprocess_v3.ResearchPostprocessV3InputBinding(
            collection_config=self.execution_config,
            collection_plan=self.collection_plan,
            collection_manifest=self.collection_manifest,
            collection_plan_id=self.collection_plan_id,
            collection_plan_hash=self.collection_plan_hash,
            collection_manifest_id=self.collection_manifest_id,
            collection_terminal_artifacts=self.collection_terminal_artifacts,
            collection_family_session_artifacts=self.collection_family_session_artifacts,
            raw_collection_artifacts_by_invocation=self.raw_collection_artifacts_by_invocation,
            problem_pool_manifest=self.problem_pool_manifest,
            problem_pool_records=self.problem_pool_records,
            context=self.context,
            import_header=self.import_header,
            source_matrix=self.source_matrix,
            reference_theorems=self.reference_theorems,
            reference_representations=self.reference_representations,
            active_registry_artifacts=self.active_registry_artifacts,
            active_registry_content_hash=self.active_registry_content_hash,
            collector_implementation=self.collector_implementation,
            primary_parser_implementations=self.primary_parser_implementations,
            recovery_implementation=self.recovery_implementation,
            implementation=self.shared_processing_implementation,
            problem_count=self.problem_count,
            seed_count_by_family=self.seed_count_by_family,
            expected_invocations=self.expected_invocations,
            problem_record_ids=self.problem_record_ids,
            invocation_ids=self.invocation_ids,
            family_ids=self.family_ids,
        )

    @property
    def shared_processing_input_binding_hash(self) -> str:
        return self.shared_v3_binding().binding_hash

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected_pool = {
            "gate3_algebra_operational_v1": (
                "mathlib_gate3_docstrings_operational_v1",
                "lf021_gate3_docstrings_operational_problem_pool_v1",
            ),
            "cross_domain_operational_v1": (
                "mathlib_cross_domain_docstrings_operational_v1",
                "lf021_cross_domain_docstrings_operational_problem_pool_v1",
            ),
        }[self.pool_dialect]
        if (self.pool_source, self.pool_manifest_artifact_kind) != expected_pool:
            raise ValueError("v7 pool dialect/source/manifest kind differ")
        if (
            self.problem_record_ids != tuple(sorted(set(self.problem_record_ids)))
            or len(self.problem_record_ids) != self.problem_count
            or self.invocation_ids != tuple(sorted(set(self.invocation_ids)))
            or len(self.invocation_ids) != self.expected_invocations
            or self.family_ids != tuple(sorted(set(self.family_ids)))
        ):
            raise ValueError("v7 problem, invocation, or family identities differ")
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if (
            list(self.seed_count_by_family) != sorted(self.seed_count_by_family)
            or set(self.seed_count_by_family) != set(self.family_ids)
            or any(value < 1 for value in self.seed_count_by_family.values())
            or expected != self.expected_invocations
        ):
            raise ValueError("v7 seed denominator differs")
        for name in (
            "collection_terminal_artifacts",
            "collection_family_session_artifacts",
        ):
            mapping = getattr(self, name)
            if list(mapping) != sorted(mapping) or any(
                re.fullmatch(_HEX64, value) is None for value in mapping.values()
            ):
                raise ValueError(f"v7 {name} is not sorted SHA-256 lineage")
        if len(self.collection_terminal_artifacts) != expected:
            raise ValueError("v7 terminal artifact denominator differs")
        if list(self.raw_collection_artifacts_by_invocation) != list(self.invocation_ids):
            raise ValueError("v7 raw lineage does not cover the invocation denominator")
        for artifacts in self.raw_collection_artifacts_by_invocation.values():
            if list(artifacts) != sorted(artifacts) or any(
                re.fullmatch(_HEX64, value) is None for value in artifacts.values()
            ):
                raise ValueError("v7 raw lineage contains invalid artifact hashes")
        if (
            list(self.primary_parser_implementations) != sorted(self.primary_parser_implementations)
            or set(self.primary_parser_implementations) != set(self.family_ids)
            or list(self.active_registry_artifacts) != sorted(self.active_registry_artifacts)
        ):
            raise ValueError("v7 parser or registry bindings differ")
        if self.collector_implementation.artifact != _COLLECTOR_ARTIFACT:
            raise ValueError("v7 must bind collector-v6")
        if self.shared_processing_implementation.artifact != _SHARED_PROCESSOR_ARTIFACT:
            raise ValueError("v7 must bind the immutable postprocess-v3 engine")
        self.shared_v3_binding()
        return self


class PostExhaustionPostprocessTerminalV7(StrictModel):
    """One v7 terminal carrying the complete immutable shared-v3 record."""

    schema_version: Literal[7] = 7
    record_kind: Literal["lf021_post_exhaustion_postprocess_terminal_v7"]
    artifact_class: Literal["research"] = "research"
    terminal_id: str = Field(pattern=_TERMINAL_ID)
    input_binding_hash: str = Field(pattern=_HEX64)
    shared_processing_input_binding_hash: str = Field(pattern=_HEX64)
    shared_processing_terminal_id: str
    shared_processing_terminal_sha256: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    tranche_order: int = Field(ge=12, le=15)
    invocation_id: str
    family_id: str
    problem_record_id: str
    seed: int = Field(ge=0)
    status: postprocess_v1.ResearchPostprocessStatus
    parser_executed: bool
    lean_validation_executed: bool
    screening_executed: bool
    semantic_pool_admitted: bool
    output_artifact_hashes: dict[str, str]
    candidate_theorem_id: str | None = None
    same_claim: None = None
    relation: None = None
    resolution_outcome: Literal["unresolved"] | None = None
    quality_tier: Literal["unknown"] | None = None
    requires_adjudication: bool = False
    decision: Literal["REVIEW"] | None = None
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    shared_processing_terminal: postprocess_v3.ResearchPostprocessV3Terminal

    @property
    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "terminal_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        shared = self.shared_processing_terminal
        projection = {
            "invocation_id": self.invocation_id,
            "family_id": self.family_id,
            "problem_record_id": self.problem_record_id,
            "seed": self.seed,
            "status": self.status,
            "parser_executed": self.parser_executed,
            "lean_validation_executed": self.lean_validation_executed,
            "screening_executed": self.screening_executed,
            "semantic_pool_admitted": self.semantic_pool_admitted,
            "output_artifact_hashes": self.output_artifact_hashes,
            "candidate_theorem_id": self.candidate_theorem_id,
            "same_claim": self.same_claim,
            "relation": self.relation,
            "resolution_outcome": self.resolution_outcome,
            "quality_tier": self.quality_tier,
            "requires_adjudication": self.requires_adjudication,
            "decision": self.decision,
            "semantic_labels_created": self.semantic_labels_created,
            "supervision_eligible": self.supervision_eligible,
            "gate_5g_credit_claimed": self.gate_5g_credit_claimed,
            "gate_5_closed": self.gate_5_closed,
        }
        if projection != {key: getattr(shared, key) for key in projection}:
            raise ValueError("v7 terminal projection differs from shared v3")
        if (
            self.shared_processing_terminal_id != shared.terminal_id
            or self.shared_processing_input_binding_hash != shared.input_binding_hash
            or self.shared_processing_terminal_sha256
            != sha256_hex(postprocess_v1._canonical_record_bytes(shared))
            or list(self.output_artifact_hashes) != sorted(self.output_artifact_hashes)
        ):
            raise ValueError("v7 shared terminal binding differs")
        expected = "lf021_post_exhaustion_postprocess_v7_terminal:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_postprocess_terminal_v7",
                **self.id_payload,
            }
        )
        if self.terminal_id != expected:
            raise ValueError("v7 terminal ID differs from content")
        return self


class PostExhaustionPostprocessFamilyReportV7(StrictModel):
    schema_version: Literal[7] = 7
    report_id: str = Field(pattern=_FAMILY_ID)
    input_binding_hash: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    family_id: str
    problem_count: int = Field(ge=1)
    seed_count: int = Field(ge=1)
    expected_invocations: int = Field(ge=1)
    status_counts: dict[str, int]
    recovery_status_counts: dict[str, int]
    admitted_unresolved_count: int = Field(ge=0)
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @property
    def id_payload(self) -> dict[str, object]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key != "report_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected = self.problem_count * self.seed_count
        if (
            self.expected_invocations != expected
            or sum(self.status_counts.values()) != expected
            or sum(self.recovery_status_counts.values()) != expected
            or self.admitted_unresolved_count > expected
        ):
            raise ValueError("v7 family accounting differs")
        expected_id = "lf021_post_exhaustion_postprocess_v7_family:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_postprocess_family_v7",
                **self.id_payload,
            }
        )
        if self.report_id != expected_id:
            raise ValueError("v7 family report ID differs")
        return self


class PostExhaustionPostprocessManifestV7(StrictModel):
    schema_version: Literal[7] = 7
    manifest_id: str = Field(pattern=_MANIFEST_ID)
    input_binding: PostExhaustionPostprocessInputBindingV7
    input_binding_hash: str = Field(pattern=_HEX64)
    shared_processing_input_binding_hash: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    tranche_order: int = Field(ge=12, le=15)
    pool_id: str
    pool_dialect: PoolDialect
    pool_source: PoolSource
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    seed_count_by_family: dict[str, int]
    expected_invocations: int = Field(ge=1)
    terminal_invocations: int = Field(ge=1)
    status_counts: dict[str, int]
    recovery_status_counts: dict[str, int]
    terminal_artifacts: dict[str, str]
    family_report_artifacts: dict[str, str]
    admitted_pair_count: int = Field(ge=0)
    admitted_nl_lean_count: int = Field(ge=0)
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @property
    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "manifest_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        binding = self.input_binding
        if (
            self.input_binding_hash != binding.binding_hash
            or self.shared_processing_input_binding_hash
            != binding.shared_processing_input_binding_hash
            or self.tranche_id != binding.tranche_id
            or self.tranche_order != binding.tranche_order
            or self.pool_id != binding.pool_id
            or self.pool_dialect != binding.pool_dialect
            or self.pool_source != binding.pool_source
            or self.problem_count != binding.problem_count
            or self.seed_count_by_family != binding.seed_count_by_family
            or self.expected_invocations != binding.expected_invocations
            or self.terminal_invocations != self.expected_invocations
            or sum(self.status_counts.values()) != self.expected_invocations
            or sum(self.recovery_status_counts.values()) != self.expected_invocations
            or len(self.terminal_artifacts) != self.expected_invocations
            or len(self.family_report_artifacts) != self.family_count
        ):
            raise ValueError("v7 manifest accounting or lineage differs")
        for name in ("terminal_artifacts", "family_report_artifacts"):
            if list(getattr(self, name)) != sorted(getattr(self, name)):
                raise ValueError(f"{name} must be sorted")
        expected = "research_postprocess_v7_manifest:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_postprocess_manifest_v7",
                **self.id_payload,
            }
        )
        if self.manifest_id != expected:
            raise ValueError("v7 manifest ID differs")
        return self


@dataclass(frozen=True, slots=True)
class LoadedPostExhaustionPostprocessV7:
    collection: collection_v6.LoadedPostExhaustionCollectionV6
    base: postprocess_v3._PostprocessBase
    input_binding: PostExhaustionPostprocessInputBindingV7
    shared_v3: postprocess_v3.LoadedResearchPostprocessV3


@dataclass(frozen=True, slots=True)
class PostExhaustionPostprocessRunV7:
    output_root: Path
    manifest_path: Path
    manifest: PostExhaustionPostprocessManifestV7
    terminals: tuple[PostExhaustionPostprocessTerminalV7, ...]
    family_reports: tuple[PostExhaustionPostprocessFamilyReportV7, ...]


def _repo_binding(
    repo_root: Path,
    path: Path,
) -> postprocess_v1.PostprocessArtifactBinding:
    return postprocess_v4._repo_binding(repo_root, path)


def _validate_collection_denominator(
    loaded: collection_v6.LoadedPostExhaustionCollectionV6,
    manifest: collection_v6.PostExhaustionCollectionManifestV6,
) -> None:
    plan = loaded.planning_plan
    config = loaded.config.config
    if (
        manifest.execution_config_id != config.config_id
        or manifest.execution_config_hash != config.config_hash
        or manifest.authorization_id != loaded.authorization.authorization_id
        or manifest.extension_decision_id != loaded.authorization.extension_decision_id
        or manifest.planning_config_id != loaded.planning_config.config_id
        or manifest.planning_plan_id != plan.plan_id
        or manifest.planning_plan_hash != hash_canonical(plan.model_dump(mode="json"))
        or manifest.tranche_id != config.tranche_id
        or manifest.tranche_order != config.tranche_order
        or manifest.pool_id != config.pool_id
        or manifest.pool_dialect != config.pool_dialect
        or manifest.problem_count != plan.problem_count
        or manifest.seed_count_by_family != plan.seed_count_by_family
        or manifest.expected_candidate_count != plan.expected_candidate_count
        or manifest.terminal_candidate_count != plan.expected_candidate_count
        or len(manifest.terminal_artifact_hashes) != plan.expected_candidate_count
        or not manifest.actual_collection_performed
        or manifest.semantic_labels_inspected
        or manifest.semantic_labels_created
        or manifest.supervision_eligible
        or manifest.gate_5g_credit_claimed
        or manifest.gate_5_closed
    ):
        raise PostExhaustionPostprocessV7Error("collector-v6 denominator or policy differs")


def load_post_exhaustion_postprocess_v7(
    *,
    repo_root: Path,
    collection_root: Path,
    collection_config_path: Path,
    output_root: Path | None = None,
) -> LoadedPostExhaustionPostprocessV7:
    """Bind one completed extension collector-v6 bundle without Lean execution."""

    root = repo_root.resolve()
    try:
        collection_root = collection_v6._require_repo_path_without_symlinks(
            repo_root=root,
            path=collection_root,
            label="collector-v6 collection",
        )
        config_path = collection_v6._require_repo_path_without_symlinks(
            repo_root=root,
            path=collection_config_path,
            label="collector-v6 config",
        )
    except collection_v6.PostExhaustionCollectionV6Error as exc:
        raise PostExhaustionPostprocessV7Error(str(exc)) from exc
    loaded_collection = collection_v6.load_post_exhaustion_collection_v6(
        config_path,
        repo_root=root,
    )
    try:
        manifest = collection_v6.verify_post_exhaustion_collection_v6(
            loaded_collection,
            repo_root=root,
        )
    except collection_v6.PostExhaustionCollectionV6Error as exc:
        raise PostExhaustionPostprocessV7Error("collector-v6 bundle does not replay") from exc
    expected_root = (
        root
        / loaded_collection.config.config.output_root
        / loaded_collection.planning_plan.plan_id.rsplit(":", 1)[-1]
    )
    if collection_root != expected_root.resolve():
        raise PostExhaustionPostprocessV7Error("collector-v6 root differs from execution config")
    _validate_collection_denominator(loaded_collection, manifest)
    plan = loaded_collection.planning_plan
    plan_path = collection_root / "plan.json"
    manifest_path = collection_root / "manifest.json"
    terminals, terminal_paths, terminal_hashes = postprocess_v4._load_collection_terminals(
        repo_root=root,
        collection_root=collection_root,
        plan=cast(Any, plan),
        manifest=cast(Any, manifest),
    )
    session_hashes = postprocess_v4._resolve_collection_artifacts(
        repo_root=root,
        collection_root=collection_root,
        artifacts=manifest.family_session_artifact_hashes,
        label="extension collector-v6 family session",
    )
    config = loaded_collection.planning_config
    problem_path = postprocess_v1._resolve_repo_artifact(root, config.problem_pool_records.artifact)
    context_path = postprocess_v1._resolve_repo_artifact(root, config.context.artifact)
    header_path = postprocess_v1._resolve_repo_artifact(root, config.import_header.artifact)
    source_matrix_path = postprocess_v1._resolve_repo_artifact(root, config.source_matrix.artifact)
    pool_manifest_path = postprocess_v1._resolve_repo_artifact(
        root, config.problem_pool_manifest.artifact
    )
    pool_document = dict(collection_v3._load_canonical_mapping(pool_manifest_path))
    reference_theorems_path = postprocess_v4._reference_artifact(
        repo_root=root,
        pool_document=pool_document,
        pool_dialect=config.pool_dialect,
        field="reference_theorems",
    )
    reference_representations_path = postprocess_v4._reference_artifact(
        repo_root=root,
        pool_document=pool_document,
        pool_dialect=config.pool_dialect,
        field="reference_representations",
    )
    problems = {item.problem_record_id: item for item in loaded_collection.problems}
    context = postprocess_v1._load_canonical(context_path, ContextRecord)
    references = {
        item.theorem_id: item
        for item in postprocess_v1._load_jsonl(reference_theorems_path, TheoremRecord)
    }
    representations = {
        item.theorem_id: item
        for item in postprocess_v1._load_jsonl(reference_representations_path, RepresentationRecord)
    }
    required = {
        theorem_id for problem in problems.values() for theorem_id in problem.reference_theorem_ids
    }
    if (
        tuple(sorted(problems)) != plan.problem_record_ids
        or set(references) != required
        or set(representations) != required
        or any(problem.private_source_content for problem in problems.values())
    ):
        raise PostExhaustionPostprocessV7Error("extension public-pool references differ")
    denylist, registry_bindings = postprocess_v1._registry_bindings(root)
    try:
        destination = collection_v6._require_repo_path_without_symlinks(
            repo_root=root,
            path=output_root if output_root is not None else collection_root / "postprocess_v7",
            label="postprocess-v7 output",
        )
    except collection_v6.PostExhaustionCollectionV6Error as exc:
        raise PostExhaustionPostprocessV7Error(str(exc)) from exc
    if "postprocess_v7" not in destination.parts:
        raise PostExhaustionPostprocessV7Error(
            "postprocess-v7 output path must contain postprocess_v7"
        )
    base = postprocess_v3._PostprocessBase(
        repo_root=root,
        collection_root=collection_root,
        output_root=destination,
        plan=cast(Any, plan),
        manifest=cast(Any, manifest),
        invocations=tuple(plan.invocations),
        collection_terminals=terminals,
        collection_terminal_paths=terminal_paths,
        problems=problems,
        context=context,
        import_header=header_path.read_text(encoding="utf-8"),
        references=references,
        reference_representations=representations,
        denylist=denylist,
    )
    raw_by_invocation: dict[str, dict[str, str]] = {}
    for invocation in base.invocations:
        terminal = terminals[invocation.invocation_id]
        if terminal.status is collection_v1.ResearchTerminalStatus.RAW_COLLECTED:
            _, _, _, hashes = postprocess_v1._verify_semantic_raw_lineage(
                cast(Any, base), invocation, terminal
            )
            raw_by_invocation[invocation.invocation_id] = hashes
        else:
            raw_by_invocation[invocation.invocation_id] = {}
    parser_bindings: dict[str, postprocess_v1.PostprocessArtifactBinding] = {}
    for family in plan.family_bindings:
        parser_path = postprocess_v1._resolve_repo_artifact(root, family.parser_source_artifact)
        binding = _repo_binding(root, parser_path)
        if binding.sha256 != family.parser_source_sha256:
            raise PostExhaustionPostprocessV7Error("primary parser hash differs")
        parser_bindings[family.family_id] = binding
    collector_path = root / _COLLECTOR_ARTIFACT
    observed_collector = _repo_binding(root, collector_path)
    expected_collector = loaded_collection.config.config.collector_implementation
    if (
        observed_collector.artifact != expected_collector.artifact
        or observed_collector.sha256 != expected_collector.sha256
    ):
        raise PostExhaustionPostprocessV7Error("collector-v6 implementation binding differs")
    shared_path = root / _SHARED_PROCESSOR_ARTIFACT
    matrix = loaded_collection.template.source_matrix
    authorization_path = collection_v6._verify_binding(
        root, loaded_collection.config.config.authorization
    )
    extension_decision_path = collection_v6._verify_binding(
        root, loaded_collection.config.config.extension_decision
    )
    planning_config_path = collection_v6._verify_binding(
        root, loaded_collection.config.config.planning_config
    )
    input_binding = PostExhaustionPostprocessInputBindingV7(
        tranche_id=loaded_collection.config.config.tranche_id,
        tranche_order=loaded_collection.config.config.tranche_order,
        pool_id=loaded_collection.config.config.pool_id,
        pool_dialect=loaded_collection.config.config.pool_dialect,
        pool_source=matrix.source,
        pool_manifest_artifact_kind=config.problem_pool_contract.manifest_artifact_kind,
        extension_authorization=_repo_binding(root, authorization_path),
        extension_authorization_id=loaded_collection.authorization.authorization_id,
        extension_decision=_repo_binding(root, extension_decision_path),
        extension_decision_id=loaded_collection.authorization.extension_decision_id,
        planning_config=_repo_binding(root, planning_config_path),
        planning_config_id=loaded_collection.planning_config.config_id,
        execution_config=_repo_binding(root, config_path),
        execution_config_id=loaded_collection.config.config.config_id,
        execution_config_hash=loaded_collection.config.config.config_hash,
        collection_plan=_repo_binding(root, plan_path),
        collection_manifest=_repo_binding(root, manifest_path),
        collection_plan_id=plan.plan_id,
        collection_plan_hash=hash_canonical(plan.model_dump(mode="json")),
        collection_manifest_id=manifest.manifest_id,
        collection_terminal_artifacts=terminal_hashes,
        collection_family_session_artifacts=session_hashes,
        raw_collection_artifacts_by_invocation=dict(sorted(raw_by_invocation.items())),
        problem_pool_manifest=_repo_binding(root, pool_manifest_path),
        problem_pool_records=_repo_binding(root, problem_path),
        context=_repo_binding(root, context_path),
        import_header=_repo_binding(root, header_path),
        source_matrix=_repo_binding(root, source_matrix_path),
        reference_theorems=_repo_binding(root, reference_theorems_path),
        reference_representations=_repo_binding(root, reference_representations_path),
        active_registry_artifacts=registry_bindings,
        active_registry_content_hash=denylist.registry_content_hash,
        collector_implementation=_repo_binding(root, collector_path),
        primary_parser_implementations=dict(sorted(parser_bindings.items())),
        recovery_implementation=_repo_binding(
            root, Path(postprocess_v3.__file__).with_name("local_output_recovery.py")
        ),
        shared_processing_implementation=_repo_binding(root, shared_path),
        implementation=_repo_binding(root, Path(__file__)),
        problem_count=plan.problem_count,
        seed_count_by_family=plan.seed_count_by_family,
        expected_invocations=plan.expected_candidate_count,
        problem_record_ids=plan.problem_record_ids,
        invocation_ids=tuple(item.invocation_id for item in plan.invocations),
        family_ids=tuple(item.family_id for item in plan.family_bindings),
    )
    shared = postprocess_v3.LoadedResearchPostprocessV3(
        base=base,
        input_binding=input_binding.shared_v3_binding(),
    )
    return LoadedPostExhaustionPostprocessV7(
        collection=loaded_collection,
        base=base,
        input_binding=input_binding,
        shared_v3=shared,
    )


def _terminal(
    loaded: LoadedPostExhaustionPostprocessV7,
    shared: postprocess_v3.ResearchPostprocessV3Terminal,
) -> PostExhaustionPostprocessTerminalV7:
    payload: dict[str, object] = {
        "schema_version": 7,
        "record_kind": "lf021_post_exhaustion_postprocess_terminal_v7",
        "artifact_class": "research",
        "input_binding_hash": loaded.input_binding.binding_hash,
        "shared_processing_input_binding_hash": (
            loaded.input_binding.shared_processing_input_binding_hash
        ),
        "shared_processing_terminal_id": shared.terminal_id,
        "shared_processing_terminal_sha256": sha256_hex(
            postprocess_v1._canonical_record_bytes(shared)
        ),
        "tranche_id": loaded.input_binding.tranche_id,
        "tranche_order": loaded.input_binding.tranche_order,
        "invocation_id": shared.invocation_id,
        "family_id": shared.family_id,
        "problem_record_id": shared.problem_record_id,
        "seed": shared.seed,
        "status": shared.status.value,
        "parser_executed": shared.parser_executed,
        "lean_validation_executed": shared.lean_validation_executed,
        "screening_executed": shared.screening_executed,
        "semantic_pool_admitted": shared.semantic_pool_admitted,
        "output_artifact_hashes": shared.output_artifact_hashes,
        "candidate_theorem_id": shared.candidate_theorem_id,
        "same_claim": None,
        "relation": None,
        "resolution_outcome": shared.resolution_outcome,
        "quality_tier": shared.quality_tier,
        "requires_adjudication": shared.requires_adjudication,
        "decision": shared.decision,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
        "shared_processing_terminal": shared.model_dump(mode="json"),
    }
    terminal_id = "lf021_post_exhaustion_postprocess_v7_terminal:" + hash_canonical(
        {
            "schema": "lf021_post_exhaustion_postprocess_terminal_v7",
            **payload,
        }
    )
    return PostExhaustionPostprocessTerminalV7.model_validate(
        {"terminal_id": terminal_id, **payload}
    )


def _write_outputs(
    loaded: LoadedPostExhaustionPostprocessV7,
    shared_by_id: dict[str, postprocess_v3.ResearchPostprocessV3Terminal],
) -> PostExhaustionPostprocessRunV7:
    if set(shared_by_id) != set(loaded.input_binding.invocation_ids):
        raise PostExhaustionPostprocessV7Error("v7 postprocess denominator is incomplete")
    terminals = tuple(_terminal(loaded, shared_by_id[key]) for key in sorted(shared_by_id))
    terminal_artifacts: dict[str, str] = {}
    for terminal in terminals:
        path = (
            loaded.base.output_root
            / "invocations"
            / terminal.invocation_id.rsplit(":", 1)[-1]
            / "processing_terminal.json"
        )
        terminal_artifacts[str(path.relative_to(loaded.base.repo_root))] = (
            postprocess_v1._write_immutable(path, postprocess_v1._canonical_record_bytes(terminal))
        )
    reports: list[PostExhaustionPostprocessFamilyReportV7] = []
    report_artifacts: dict[str, str] = {}
    for family_id in loaded.input_binding.family_ids:
        selected = tuple(item for item in terminals if item.family_id == family_id)
        seed_count = loaded.input_binding.seed_count_by_family[family_id]
        expected = loaded.input_binding.problem_count * seed_count
        if len(selected) != expected:
            raise PostExhaustionPostprocessV7Error("v7 family denominator differs")
        payload: dict[str, object] = {
            "schema_version": 7,
            "input_binding_hash": loaded.input_binding.binding_hash,
            "tranche_id": loaded.input_binding.tranche_id,
            "family_id": family_id,
            "problem_count": loaded.input_binding.problem_count,
            "seed_count": seed_count,
            "expected_invocations": expected,
            "status_counts": dict(sorted(Counter(item.status.value for item in selected).items())),
            "recovery_status_counts": dict(
                sorted(
                    Counter(
                        item.shared_processing_terminal.recovery_status.value for item in selected
                    ).items()
                )
            ),
            "admitted_unresolved_count": sum(
                item.status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
                for item in selected
            ),
            "semantic_labels_inspected": False,
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        }
        report_id = "lf021_post_exhaustion_postprocess_v7_family:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_postprocess_family_v7",
                **payload,
            }
        )
        report = PostExhaustionPostprocessFamilyReportV7.model_validate(
            {"report_id": report_id, **payload}
        )
        path = loaded.base.output_root / "families" / f"{family_id}.json"
        report_artifacts[str(path.relative_to(loaded.base.repo_root))] = (
            postprocess_v1._write_immutable(path, postprocess_v1._canonical_record_bytes(report))
        )
        reports.append(report)
    admitted = tuple(
        item
        for item in terminals
        if item.status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    )
    payload = {
        "schema_version": 7,
        "input_binding": loaded.input_binding.model_dump(mode="json"),
        "input_binding_hash": loaded.input_binding.binding_hash,
        "shared_processing_input_binding_hash": (
            loaded.input_binding.shared_processing_input_binding_hash
        ),
        "tranche_id": loaded.input_binding.tranche_id,
        "tranche_order": loaded.input_binding.tranche_order,
        "pool_id": loaded.input_binding.pool_id,
        "pool_dialect": loaded.input_binding.pool_dialect,
        "pool_source": loaded.input_binding.pool_source,
        "problem_count": loaded.input_binding.problem_count,
        "family_count": 3,
        "seed_count_by_family": loaded.input_binding.seed_count_by_family,
        "expected_invocations": loaded.input_binding.expected_invocations,
        "terminal_invocations": len(terminals),
        "status_counts": dict(sorted(Counter(item.status.value for item in terminals).items())),
        "recovery_status_counts": dict(
            sorted(
                Counter(
                    item.shared_processing_terminal.recovery_status.value for item in terminals
                ).items()
            )
        ),
        "terminal_artifacts": dict(sorted(terminal_artifacts.items())),
        "family_report_artifacts": dict(sorted(report_artifacts.items())),
        "admitted_pair_count": sum(
            len(item.shared_processing_terminal.pair_ids) for item in admitted
        ),
        "admitted_nl_lean_count": len(admitted),
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    manifest_id = "research_postprocess_v7_manifest:" + hash_canonical(
        {
            "schema": "lf021_post_exhaustion_postprocess_manifest_v7",
            **payload,
        }
    )
    manifest = PostExhaustionPostprocessManifestV7.model_validate(
        {"manifest_id": manifest_id, **payload}
    )
    manifest_path = loaded.base.output_root / "manifest.json"
    postprocess_v1._write_immutable(manifest_path, postprocess_v1._canonical_record_bytes(manifest))
    return PostExhaustionPostprocessRunV7(
        output_root=loaded.base.output_root,
        manifest_path=manifest_path,
        manifest=manifest,
        terminals=terminals,
        family_reports=tuple(reports),
    )


def run_post_exhaustion_postprocess_v7(
    loaded: LoadedPostExhaustionPostprocessV7,
    *,
    backend: LeanInteractBackend,
) -> PostExhaustionPostprocessRunV7:
    """Run the immutable v3 correctness engine over the extension denominator."""

    try:
        prepared, terminals = postprocess_v3._prepare_candidates(loaded.shared_v3, backend=backend)
        postprocess_v3._screen_and_admit(loaded.shared_v3, prepared=prepared, terminals=terminals)
    except postprocess_v3.ResearchPostprocessV3Error as exc:
        raise PostExhaustionPostprocessV7Error("shared v3 extension processing failed") from exc
    return _write_outputs(loaded, terminals)


def verify_post_exhaustion_postprocess_v7(
    loaded: LoadedPostExhaustionPostprocessV7,
) -> PostExhaustionPostprocessManifestV7:
    """Verify a v7 envelope without Lean, GPU, model, or provider execution."""

    try:
        manifest = postprocess_v1._load_canonical(
            loaded.base.output_root / "manifest.json",
            PostExhaustionPostprocessManifestV7,
        )
    except (OSError, ValueError, postprocess_v1.ResearchPostprocessError) as exc:
        raise PostExhaustionPostprocessV7Error("persisted v7 manifest is invalid") from exc
    if manifest.input_binding != loaded.input_binding:
        raise PostExhaustionPostprocessV7Error("persisted v7 input binding differs")
    expected_terminal_paths = {
        (loaded.base.repo_root / artifact).resolve() for artifact in manifest.terminal_artifacts
    }
    discovered_terminal_paths = {
        path.resolve()
        for path in (loaded.base.output_root / "invocations").glob("*/processing_terminal.json")
        if path.is_file() and not path.is_symlink()
    }
    if discovered_terminal_paths != expected_terminal_paths:
        raise PostExhaustionPostprocessV7Error("v7 output contains missing or unexpected terminals")
    expected_family_paths = {
        (loaded.base.repo_root / artifact).resolve()
        for artifact in manifest.family_report_artifacts
    }
    discovered_family_paths = {
        path.resolve()
        for path in (loaded.base.output_root / "families").glob("*.json")
        if path.is_file() and not path.is_symlink()
    }
    if discovered_family_paths != expected_family_paths:
        raise PostExhaustionPostprocessV7Error(
            "v7 output contains missing or unexpected family reports"
        )
    terminals: list[PostExhaustionPostprocessTerminalV7] = []
    for artifact, digest in manifest.terminal_artifacts.items():
        try:
            path = collection_v6._require_repo_path_without_symlinks(
                repo_root=loaded.base.repo_root,
                path=Path(artifact),
                label="v7 terminal",
            )
        except collection_v6.PostExhaustionCollectionV6Error as exc:
            raise PostExhaustionPostprocessV7Error(str(exc)) from exc
        if not path.is_file() or hash_file(path) != digest:
            raise PostExhaustionPostprocessV7Error("v7 terminal hash mismatch")
        terminal = postprocess_v1._load_canonical(path, PostExhaustionPostprocessTerminalV7)
        terminals.append(terminal)
        for output, output_digest in terminal.output_artifact_hashes.items():
            try:
                output_path = collection_v6._require_repo_path_without_symlinks(
                    repo_root=loaded.base.repo_root,
                    path=Path(output),
                    label="v7 output artifact",
                )
            except collection_v6.PostExhaustionCollectionV6Error as exc:
                raise PostExhaustionPostprocessV7Error(str(exc)) from exc
            if not output_path.is_file() or hash_file(output_path) != output_digest:
                raise PostExhaustionPostprocessV7Error("v7 output artifact hash mismatch")
    terminals.sort(key=lambda item: item.invocation_id)
    invocation_by_id = {
        item.invocation_id: item for item in loaded.collection.planning_plan.invocations
    }
    if tuple(
        item.invocation_id for item in terminals
    ) != loaded.input_binding.invocation_ids or len(invocation_by_id) != len(terminals):
        raise PostExhaustionPostprocessV7Error("v7 terminal invocation denominator differs")
    for terminal in terminals:
        invocation = invocation_by_id[terminal.invocation_id]
        shared = terminal.shared_processing_terminal
        collection_terminal = loaded.base.collection_terminals.get(terminal.invocation_id)
        collection_terminal_path = loaded.base.collection_terminal_paths.get(terminal.invocation_id)
        if collection_terminal is None or collection_terminal_path is None:
            raise PostExhaustionPostprocessV7Error("v7 terminal lacks frozen collection lineage")
        if (
            terminal.input_binding_hash != loaded.input_binding.binding_hash
            or terminal.shared_processing_input_binding_hash
            != loaded.input_binding.shared_processing_input_binding_hash
            or terminal.tranche_id != loaded.input_binding.tranche_id
            or terminal.tranche_order != loaded.input_binding.tranche_order
            or terminal.family_id != invocation.family_id
            or terminal.problem_record_id != invocation.problem_record_id
            or terminal.seed != invocation.seed
            or shared.invocation_payload_hash != hash_canonical(invocation.model_dump(mode="json"))
            or shared.collection_terminal_id != collection_terminal.terminal_id
            or shared.collection_terminal_sha256 != hash_file(collection_terminal_path)
            or shared.primary_parser_id != invocation.parser_id
            or shared.primary_parser_source_sha256 != invocation.parser_source_sha256
            or shared.raw_lineage_hashes
            != loaded.input_binding.raw_collection_artifacts_by_invocation[terminal.invocation_id]
        ):
            raise PostExhaustionPostprocessV7Error(
                "v7 terminal differs from input, collection, parser, or invocation lineage"
            )
        bound_artifacts = {
            **shared.raw_lineage_hashes,
            **shared.output_artifact_hashes,
        }
        if len(bound_artifacts) != (
            len(shared.raw_lineage_hashes) + len(shared.output_artifact_hashes)
        ):
            raise PostExhaustionPostprocessV7Error("v7 raw/output path collision")
        for artifact, digest in bound_artifacts.items():
            try:
                path = collection_v6._require_repo_path_without_symlinks(
                    repo_root=loaded.base.repo_root,
                    path=Path(artifact),
                    label="v7 shared bound artifact",
                )
            except collection_v6.PostExhaustionCollectionV6Error as exc:
                raise PostExhaustionPostprocessV7Error(str(exc)) from exc
            if not path.is_file() or hash_file(path) != digest:
                raise PostExhaustionPostprocessV7Error("v7 shared bound artifact hash differs")
        parsed_candidates = [
            artifact
            for artifact in shared.output_artifact_hashes
            if artifact.endswith("/parsed_candidate.json")
        ]
        parser_succeeded = shared.recovery_status in {
            postprocess_v3.RecoveryStatus.NOT_NEEDED,
            postprocess_v3.RecoveryStatus.SUCCEEDED,
        }
        if parser_succeeded:
            if len(parsed_candidates) != 1:
                raise PostExhaustionPostprocessV7Error("v7 parsed-candidate provenance is missing")
            parsed_path = postprocess_v1._resolve_repo_artifact(
                loaded.base.repo_root,
                parsed_candidates[0],
            )
            parsed = postprocess_v1._load_canonical(
                parsed_path,
                postprocess_v3._ParsedCandidateRecordV3,
            )
            if (
                parsed.invocation_id != shared.invocation_id
                or parsed.primary_parser_id != shared.primary_parser_id
                or parsed.primary_parser_source_sha256 != shared.primary_parser_source_sha256
                or parsed.actual_parser_id != shared.actual_parser_id
                or parsed.actual_parser_source_sha256 != shared.actual_parser_source_sha256
                or parsed.primary_failure_code != shared.primary_failure_code
                or parsed.recovery_status != shared.recovery_status.value
            ):
                raise PostExhaustionPostprocessV7Error("v7 parsed-candidate provenance differs")
        elif parsed_candidates:
            raise PostExhaustionPostprocessV7Error("failed v7 parse persisted a candidate")
    reports: dict[str, PostExhaustionPostprocessFamilyReportV7] = {}
    for artifact, digest in manifest.family_report_artifacts.items():
        try:
            path = collection_v6._require_repo_path_without_symlinks(
                repo_root=loaded.base.repo_root,
                path=Path(artifact),
                label="v7 family report",
            )
        except collection_v6.PostExhaustionCollectionV6Error as exc:
            raise PostExhaustionPostprocessV7Error(str(exc)) from exc
        if not path.is_file() or hash_file(path) != digest:
            raise PostExhaustionPostprocessV7Error("v7 family report hash mismatch")
        report = postprocess_v1._load_canonical(path, PostExhaustionPostprocessFamilyReportV7)
        if report.family_id in reports:
            raise PostExhaustionPostprocessV7Error("duplicate v7 family report")
        reports[report.family_id] = report
    if tuple(sorted(reports)) != loaded.input_binding.family_ids:
        raise PostExhaustionPostprocessV7Error("v7 family report denominator differs")
    for family_id, report in reports.items():
        selected = tuple(item for item in terminals if item.family_id == family_id)
        if (
            report.input_binding_hash != loaded.input_binding.binding_hash
            or report.tranche_id != loaded.input_binding.tranche_id
            or report.problem_count != loaded.input_binding.problem_count
            or report.seed_count != loaded.input_binding.seed_count_by_family[family_id]
            or report.expected_invocations != len(selected)
            or report.status_counts
            != dict(sorted(Counter(item.status.value for item in selected).items()))
            or report.recovery_status_counts
            != dict(
                sorted(
                    Counter(
                        item.shared_processing_terminal.recovery_status.value for item in selected
                    ).items()
                )
            )
            or report.admitted_unresolved_count
            != sum(
                item.status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
                for item in selected
            )
        ):
            raise PostExhaustionPostprocessV7Error(
                "v7 family report differs from terminal accounting"
            )
    admitted = tuple(
        item
        for item in terminals
        if item.status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    )
    if (
        manifest.status_counts
        != dict(sorted(Counter(item.status.value for item in terminals).items()))
        or manifest.recovery_status_counts
        != dict(
            sorted(
                Counter(
                    item.shared_processing_terminal.recovery_status.value for item in terminals
                ).items()
            )
        )
        or manifest.admitted_pair_count
        != sum(len(item.shared_processing_terminal.pair_ids) for item in admitted)
        or manifest.admitted_nl_lean_count != len(admitted)
    ):
        raise PostExhaustionPostprocessV7Error("v7 manifest differs from terminal accounting")
    return manifest


__all__ = [
    "LoadedPostExhaustionPostprocessV7",
    "PostExhaustionPostprocessFamilyReportV7",
    "PostExhaustionPostprocessInputBindingV7",
    "PostExhaustionPostprocessManifestV7",
    "PostExhaustionPostprocessRunV7",
    "PostExhaustionPostprocessTerminalV7",
    "PostExhaustionPostprocessV7Error",
    "load_post_exhaustion_postprocess_v7",
    "run_post_exhaustion_postprocess_v7",
    "verify_post_exhaustion_postprocess_v7",
]
