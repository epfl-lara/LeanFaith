"""Postprocess collector-v4 tranches with the immutable v3 correctness engine.

This v5 envelope consumes only completed collector-v4 bundles.  It binds the
generic tranche and closed-pool identities while reusing the immutable
postprocess-v3 parser, recovery, Lean validation, screening, deduplication, and
unresolved-admission primitives.  One invocation remains the isolation unit.
No semantic labels, supervision admissions, or Gate claims are created.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation import research_collection as collection_v1
from leanfaith.generation import research_collection_v3 as collection_v3
from leanfaith.generation import research_collection_v4 as collection_v4
from leanfaith.generation import research_postprocess as postprocess_v1
from leanfaith.generation import research_postprocess_v3 as postprocess_v3
from leanfaith.generation import research_postprocess_v4 as postprocess_v4
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

_HEX64 = r"^[0-9a-f]{64}$"
_TRANCHE = r"^[a-z][a-z0-9_]*$"
_TERMINAL_ID = r"^research_postprocess_v5_terminal:[0-9a-f]{64}$"
_FAMILY_ID = r"^research_postprocess_v5_family:[0-9a-f]{64}$"
_MANIFEST_ID = r"^research_postprocess_v5_manifest:[0-9a-f]{64}$"
_COLLECTOR_V4_ARTIFACT = "src/leanfaith/generation/research_collection_v4.py"
_SHARED_PROCESSOR_V3_ARTIFACT = "src/leanfaith/generation/research_postprocess_v3.py"

PoolDialect = collection_v4.PoolDialect
PoolSource = Literal[
    "mathlib_gate3_docstrings_operational_v1",
    "mathlib_cross_domain_docstrings_operational_v1",
]
PoolManifestKind = Literal[
    "lf021_gate3_docstrings_operational_problem_pool_v1",
    "lf021_cross_domain_docstrings_operational_problem_pool_v1",
]


class ResearchPostprocessV5Error(RuntimeError):
    """A collector-v4 input, v5 output, or replay invariant failed."""


class ResearchPostprocessV5InputBinding(StrictModel):
    """Exact collector-v4 denominator plus every executable dependency."""

    schema_version: Literal[5] = 5
    tranche_id: str = Field(pattern=_TRANCHE)
    pool_dialect: PoolDialect
    pool_source: PoolSource
    pool_manifest_artifact_kind: PoolManifestKind
    collection_config: postprocess_v1.PostprocessArtifactBinding
    collection_plan: postprocess_v1.PostprocessArtifactBinding
    collection_manifest: postprocess_v1.PostprocessArtifactBinding
    collection_plan_id: str
    collection_plan_hash: str = Field(pattern=_HEX64)
    collection_manifest_id: str
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

    @property
    def binding_hash(self) -> str:
        return hash_canonical(
            {
                "schema": "lf021_research_postprocess_input_binding_v5",
                **self.model_dump(mode="json"),
            }
        )

    def shared_v3_binding(self) -> postprocess_v3.ResearchPostprocessV3InputBinding:
        """Project the exact immutable v3 processing dependency envelope."""

        return postprocess_v3.ResearchPostprocessV3InputBinding(
            collection_config=self.collection_config,
            collection_plan=self.collection_plan,
            collection_manifest=self.collection_manifest,
            collection_plan_id=self.collection_plan_id,
            collection_plan_hash=self.collection_plan_hash,
            collection_manifest_id=self.collection_manifest_id,
            collection_terminal_artifacts=self.collection_terminal_artifacts,
            collection_family_session_artifacts=(self.collection_family_session_artifacts),
            raw_collection_artifacts_by_invocation=(self.raw_collection_artifacts_by_invocation),
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
            raise ValueError("v5 pool dialect, source, and manifest kind disagree")
        if (
            self.problem_record_ids != tuple(sorted(set(self.problem_record_ids)))
            or len(self.problem_record_ids) != self.problem_count
        ):
            raise ValueError("v5 problem IDs do not reconcile")
        if (
            self.invocation_ids != tuple(sorted(set(self.invocation_ids)))
            or len(self.invocation_ids) != self.expected_invocations
        ):
            raise ValueError("v5 invocation IDs do not reconcile")
        if self.family_ids != tuple(sorted(set(self.family_ids))):
            raise ValueError("v5 requires three sorted unique family IDs")
        if (
            list(self.seed_count_by_family) != sorted(self.seed_count_by_family)
            or set(self.seed_count_by_family) != set(self.family_ids)
            or any(count < 1 for count in self.seed_count_by_family.values())
        ):
            raise ValueError("v5 seed counts differ from family IDs")
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if self.expected_invocations != expected:
            raise ValueError("v5 expected denominator differs")
        if (
            list(self.collection_terminal_artifacts) != sorted(self.collection_terminal_artifacts)
            or len(self.collection_terminal_artifacts) != expected
        ):
            raise ValueError("v5 terminal bindings do not reconcile")
        if list(self.raw_collection_artifacts_by_invocation) != list(self.invocation_ids):
            raise ValueError("v5 raw lineage must cover invocations in order")
        for artifacts in self.raw_collection_artifacts_by_invocation.values():
            if list(artifacts) != sorted(artifacts):
                raise ValueError("v5 raw artifacts must be sorted")
        if list(self.primary_parser_implementations) != sorted(
            self.primary_parser_implementations
        ) or set(self.primary_parser_implementations) != set(self.family_ids):
            raise ValueError("v5 parser bindings must cover all families")
        if self.collector_implementation.artifact != _COLLECTOR_V4_ARTIFACT:
            raise ValueError("v5 must bind collector-v4")
        if self.shared_processing_implementation.artifact != _SHARED_PROCESSOR_V3_ARTIFACT:
            raise ValueError("v5 must bind the immutable postprocess-v3 engine")
        self.shared_v3_binding()
        return self


class ResearchPostprocessV5Terminal(StrictModel):
    """One v5 terminal carrying its complete immutable v3 processing record."""

    schema_version: Literal[5] = 5
    record_kind: Literal["lf021_research_postprocess_terminal_v5"] = (
        "lf021_research_postprocess_terminal_v5"
    )
    artifact_class: Literal["research"] = "research"
    terminal_id: str = Field(pattern=_TERMINAL_ID)
    input_binding_hash: str = Field(pattern=_HEX64)
    shared_processing_input_binding_hash: str = Field(pattern=_HEX64)
    shared_processing_terminal_id: str
    shared_processing_terminal_sha256: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    pool_dialect: PoolDialect
    pool_source: PoolSource
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
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    shared_processing_terminal: postprocess_v3.ResearchPostprocessV3Terminal

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
            raise ValueError("v5 terminal projection differs from shared v3 terminal")
        if (
            self.shared_processing_terminal_id != shared.terminal_id
            or self.shared_processing_input_binding_hash != shared.input_binding_hash
            or self.shared_processing_terminal_sha256
            != sha256_hex(postprocess_v1._canonical_record_bytes(shared))
        ):
            raise ValueError("v5 shared terminal binding differs")
        if list(self.output_artifact_hashes) != sorted(self.output_artifact_hashes):
            raise ValueError("v5 output artifacts must be sorted")
        identity = "research_postprocess_v5_terminal:" + hash_canonical(
            {"schema": "lf021_research_postprocess_terminal_v5", **self.id_payload()}
        )
        if self.terminal_id != identity:
            raise ValueError("v5 terminal ID does not match payload")
        return self


class ResearchPostprocessV5FamilyReport(StrictModel):
    """Dynamic per-family v5 accounting."""

    schema_version: Literal[5] = 5
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
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

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
            raise ValueError("v5 family accounting does not reconcile")
        identity = "research_postprocess_v5_family:" + hash_canonical(
            {"schema": "lf021_research_postprocess_family_v5", **self.id_payload()}
        )
        if self.report_id != identity:
            raise ValueError("v5 family report ID does not match payload")
        return self


class ResearchPostprocessV5Manifest(StrictModel):
    """Complete postprocess-v5 accounting and immutable artifact index."""

    schema_version: Literal[5] = 5
    manifest_id: str = Field(pattern=_MANIFEST_ID)
    input_binding: ResearchPostprocessV5InputBinding
    input_binding_hash: str = Field(pattern=_HEX64)
    shared_processing_input_binding_hash: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
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
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

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
            raise ValueError("v5 manifest accounting or identity differs")
        for field in ("terminal_artifacts", "family_report_artifacts"):
            mapping = getattr(self, field)
            if list(mapping) != sorted(mapping):
                raise ValueError(f"{field} must be sorted")
        identity = "research_postprocess_v5_manifest:" + hash_canonical(
            {"schema": "lf021_research_postprocess_manifest_v5", **self.id_payload()}
        )
        if self.manifest_id != identity:
            raise ValueError("v5 manifest ID does not match payload")
        return self


@dataclass(frozen=True, slots=True)
class LoadedResearchPostprocessV5:
    base: postprocess_v3._PostprocessBase
    input_binding: ResearchPostprocessV5InputBinding
    shared_v3: postprocess_v3.LoadedResearchPostprocessV3


@dataclass(frozen=True, slots=True)
class ResearchPostprocessV5Run:
    output_root: Path
    manifest_path: Path
    manifest: ResearchPostprocessV5Manifest
    terminals: tuple[ResearchPostprocessV5Terminal, ...]
    family_reports: tuple[ResearchPostprocessV5FamilyReport, ...]


def validate_collection_v4_denominator(
    plan: collection_v4.ResearchCollectionPlanV4,
    manifest: collection_v4.ResearchCollectionManifestV4,
) -> None:
    """Fail closed on every collector-v4 tranche/cardinality relationship."""

    if (
        manifest.plan_id != plan.plan_id
        or manifest.plan_hash != plan.plan_hash
        or manifest.tranche_id != plan.tranche_id
        or manifest.pool_dialect != plan.pool_dialect
        or manifest.overlap_schema != plan.overlap_schema
        or manifest.shared_execution_record_schema != plan.shared_execution_record_schema
        or manifest.problem_count != plan.problem_count
        or manifest.family_count != plan.family_count
        or manifest.seed_count_by_family != plan.seed_count_by_family
        or manifest.expected_candidate_count != plan.expected_candidate_count
        or manifest.terminal_candidate_count != plan.expected_candidate_count
        or len(plan.problem_record_ids) != plan.problem_count
        or len(plan.family_bindings) != plan.family_count
        or len(plan.invocations) != plan.expected_candidate_count
        or len(manifest.terminal_artifact_hashes) != plan.expected_candidate_count
    ):
        raise ResearchPostprocessV5Error("collector-v4 plan/manifest denominator differs")
    if (
        plan.actual_collection_performed
        or plan.semantic_labels_created
        or plan.gate_5g_credit_claimed
        or plan.gate_5_closed
        or not manifest.actual_collection_performed
        or manifest.semantic_labels_created
        or manifest.gate_5g_credit_claimed
        or manifest.gate_5_closed
    ):
        raise ResearchPostprocessV5Error("collector-v4 policy differs")


def load_research_postprocess_v5(
    *,
    repo_root: Path,
    collection_root: Path,
    collection_config_path: Path,
    output_root: Path | None = None,
) -> LoadedResearchPostprocessV5:
    """Load and bind one completed collector-v4 tranche."""

    root = repo_root.resolve()
    collection = collection_root.resolve()
    config_path = collection_config_path.resolve()
    for path, label in ((collection, "collection"), (config_path, "config")):
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ResearchPostprocessV5Error(
                f"collector-v4 {label} must remain in repository"
            ) from exc
    plan_path = collection / "plan.json"
    manifest_path = collection / "manifest.json"
    plan = postprocess_v1._load_canonical(
        plan_path,
        collection_v4.ResearchCollectionPlanV4,
    )
    manifest = postprocess_v1._load_canonical(
        manifest_path,
        collection_v4.ResearchCollectionManifestV4,
    )
    validate_collection_v4_denominator(plan, manifest)
    loaded_collection = collection_v4.load_research_collection_v4(
        config_path,
        repo_root=root,
    )
    if loaded_collection.plan != plan:
        raise ResearchPostprocessV5Error("persisted collector-v4 plan differs from config replay")
    if (
        plan.collection_config_artifact != str(config_path.relative_to(root))
        or plan.collection_config_file_sha256 != hash_file(config_path)
        or plan.collection_config_hash != loaded_collection.config.config_hash
    ):
        raise ResearchPostprocessV5Error("collector-v4 config binding differs")

    terminals, terminal_paths, terminal_hashes = postprocess_v4._load_collection_terminals(
        repo_root=root,
        collection_root=collection,
        plan=cast(Any, plan),
        manifest=cast(Any, manifest),
    )
    session_hashes = postprocess_v4._resolve_collection_artifacts(
        repo_root=root,
        collection_root=collection,
        artifacts=manifest.family_session_artifact_hashes,
        label="collector-v4 family session",
    )
    config = loaded_collection.config.config
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
        pool_dialect=config.problem_pool_contract.pool_dialect,
        field="reference_theorems",
    )
    reference_representations_path = postprocess_v4._reference_artifact(
        repo_root=root,
        pool_document=pool_document,
        pool_dialect=config.problem_pool_contract.pool_dialect,
        field="reference_representations",
    )
    problems = {item.problem_record_id: item for item in loaded_collection.problems}
    context = postprocess_v1._load_canonical(context_path, ContextRecord)
    references = {
        item.theorem_id: item
        for item in postprocess_v1._load_jsonl(
            reference_theorems_path,
            TheoremRecord,
        )
    }
    representations = {
        item.theorem_id: item
        for item in postprocess_v1._load_jsonl(
            reference_representations_path,
            RepresentationRecord,
        )
    }
    required = {
        theorem_id for problem in problems.values() for theorem_id in problem.reference_theorem_ids
    }
    if (
        tuple(sorted(problems)) != plan.problem_record_ids
        or set(references) != required
        or set(representations) != required
    ):
        raise ResearchPostprocessV5Error("collector-v4 pool references differ")
    denylist, registry_bindings = postprocess_v1._registry_bindings(root)
    destination = (
        output_root.resolve()
        if output_root is not None
        else (collection / "postprocess_v5").resolve()
    )
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ResearchPostprocessV5Error("postprocess-v5 output must remain in repository") from exc
    if "postprocess_v5" not in destination.parts:
        raise ResearchPostprocessV5Error("postprocess-v5 output path must contain postprocess_v5")
    base = postprocess_v3._PostprocessBase(
        repo_root=root,
        collection_root=collection,
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
                cast(Any, base),
                invocation,
                terminal,
            )
            raw_by_invocation[invocation.invocation_id] = hashes
        else:
            raw_by_invocation[invocation.invocation_id] = {}
    parser_bindings: dict[str, postprocess_v1.PostprocessArtifactBinding] = {}
    for family in plan.family_bindings:
        parser_path = postprocess_v1._resolve_repo_artifact(root, family.parser_source_artifact)
        parser_binding = postprocess_v4._repo_binding(root, parser_path)
        if parser_binding.sha256 != family.parser_source_sha256:
            raise ResearchPostprocessV5Error("primary parser hash differs")
        parser_bindings[family.family_id] = parser_binding
    collector_path = root / _COLLECTOR_V4_ARTIFACT
    if hash_file(collector_path) != plan.orchestration_adapter_sha256:
        raise ResearchPostprocessV5Error("collector-v4 implementation hash differs")
    shared_path = root / _SHARED_PROCESSOR_V3_ARTIFACT
    matrix = loaded_collection.source_matrix
    input_binding = ResearchPostprocessV5InputBinding(
        tranche_id=plan.tranche_id,
        pool_dialect=plan.pool_dialect,
        pool_source=matrix.source,
        pool_manifest_artifact_kind=(config.problem_pool_contract.manifest_artifact_kind),
        collection_config=postprocess_v4._repo_binding(root, config_path),
        collection_plan=postprocess_v4._repo_binding(root, plan_path),
        collection_manifest=postprocess_v4._repo_binding(root, manifest_path),
        collection_plan_id=plan.plan_id,
        collection_plan_hash=plan.plan_hash,
        collection_manifest_id=manifest.manifest_id,
        collection_terminal_artifacts=terminal_hashes,
        collection_family_session_artifacts=session_hashes,
        raw_collection_artifacts_by_invocation=dict(sorted(raw_by_invocation.items())),
        problem_pool_manifest=postprocess_v4._repo_binding(root, pool_manifest_path),
        problem_pool_records=postprocess_v4._repo_binding(root, problem_path),
        context=postprocess_v4._repo_binding(root, context_path),
        import_header=postprocess_v4._repo_binding(root, header_path),
        source_matrix=postprocess_v4._repo_binding(root, source_matrix_path),
        reference_theorems=postprocess_v4._repo_binding(root, reference_theorems_path),
        reference_representations=postprocess_v4._repo_binding(
            root, reference_representations_path
        ),
        active_registry_artifacts=registry_bindings,
        active_registry_content_hash=denylist.registry_content_hash,
        collector_implementation=postprocess_v4._repo_binding(root, collector_path),
        primary_parser_implementations=dict(sorted(parser_bindings.items())),
        recovery_implementation=postprocess_v4._repo_binding(
            root,
            Path(postprocess_v3.__file__).with_name("local_output_recovery.py"),
        ),
        shared_processing_implementation=postprocess_v4._repo_binding(root, shared_path),
        implementation=postprocess_v4._repo_binding(root, Path(__file__)),
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
    return LoadedResearchPostprocessV5(
        base=base,
        input_binding=input_binding,
        shared_v3=shared,
    )


def _v5_terminal(
    loaded: LoadedResearchPostprocessV5,
    shared: postprocess_v3.ResearchPostprocessV3Terminal,
) -> ResearchPostprocessV5Terminal:
    payload: dict[str, object] = {
        "schema_version": 5,
        "record_kind": "lf021_research_postprocess_terminal_v5",
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
        "pool_dialect": loaded.input_binding.pool_dialect,
        "pool_source": loaded.input_binding.pool_source,
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
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
        "shared_processing_terminal": shared.model_dump(mode="json"),
    }
    terminal_id = "research_postprocess_v5_terminal:" + hash_canonical(
        {"schema": "lf021_research_postprocess_terminal_v5", **payload}
    )
    return ResearchPostprocessV5Terminal.model_validate({"terminal_id": terminal_id, **payload})


def _write_terminals_and_reports(
    loaded: LoadedResearchPostprocessV5,
    shared_by_id: dict[str, postprocess_v3.ResearchPostprocessV3Terminal],
) -> ResearchPostprocessV5Run:
    if set(shared_by_id) != set(loaded.input_binding.invocation_ids):
        raise ResearchPostprocessV5Error("v5 postprocess denominator is incomplete")
    terminals = tuple(_v5_terminal(loaded, shared_by_id[key]) for key in sorted(shared_by_id))
    terminal_artifacts: dict[str, str] = {}
    for terminal in terminals:
        path = (
            loaded.base.output_root
            / "invocations"
            / terminal.invocation_id.rsplit(":", 1)[-1]
            / "processing_terminal.json"
        )
        terminal_artifacts[str(path.relative_to(loaded.base.repo_root))] = (
            postprocess_v1._write_immutable(
                path,
                postprocess_v1._canonical_record_bytes(terminal),
            )
        )
    reports: list[ResearchPostprocessV5FamilyReport] = []
    report_artifacts: dict[str, str] = {}
    for family_id in loaded.input_binding.family_ids:
        selected = tuple(item for item in terminals if item.family_id == family_id)
        seed_count = loaded.input_binding.seed_count_by_family[family_id]
        expected = loaded.input_binding.problem_count * seed_count
        if len(selected) != expected:
            raise ResearchPostprocessV5Error("v5 family denominator differs")
        payload: dict[str, object] = {
            "schema_version": 5,
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
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        }
        report_id = "research_postprocess_v5_family:" + hash_canonical(
            {"schema": "lf021_research_postprocess_family_v5", **payload}
        )
        report = ResearchPostprocessV5FamilyReport.model_validate(
            {"report_id": report_id, **payload}
        )
        path = loaded.base.output_root / "families" / f"{family_id}.json"
        report_artifacts[str(path.relative_to(loaded.base.repo_root))] = (
            postprocess_v1._write_immutable(
                path,
                postprocess_v1._canonical_record_bytes(report),
            )
        )
        reports.append(report)
    admitted = tuple(
        item
        for item in terminals
        if item.status is postprocess_v1.ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    )
    payload = {
        "schema_version": 5,
        "input_binding": loaded.input_binding.model_dump(mode="json"),
        "input_binding_hash": loaded.input_binding.binding_hash,
        "shared_processing_input_binding_hash": (
            loaded.input_binding.shared_processing_input_binding_hash
        ),
        "tranche_id": loaded.input_binding.tranche_id,
        "pool_dialect": loaded.input_binding.pool_dialect,
        "pool_source": loaded.input_binding.pool_source,
        "problem_count": loaded.input_binding.problem_count,
        "family_count": loaded.input_binding.family_count,
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
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    manifest_id = "research_postprocess_v5_manifest:" + hash_canonical(
        {"schema": "lf021_research_postprocess_manifest_v5", **payload}
    )
    manifest = ResearchPostprocessV5Manifest.model_validate({"manifest_id": manifest_id, **payload})
    manifest_path = loaded.base.output_root / "manifest.json"
    postprocess_v1._write_immutable(
        manifest_path,
        postprocess_v1._canonical_record_bytes(manifest),
    )
    return ResearchPostprocessV5Run(
        output_root=loaded.base.output_root,
        manifest_path=manifest_path,
        manifest=manifest,
        terminals=terminals,
        family_reports=tuple(reports),
    )


def run_research_postprocess_v5(
    loaded: LoadedResearchPostprocessV5,
    *,
    backend: LeanInteractBackend,
) -> ResearchPostprocessV5Run:
    """Run the exact v3 engine over every collector-v4 invocation."""

    try:
        prepared, terminals = postprocess_v3._prepare_candidates(
            loaded.shared_v3,
            backend=backend,
        )
        postprocess_v3._screen_and_admit(
            loaded.shared_v3,
            prepared=prepared,
            terminals=terminals,
        )
    except postprocess_v3.ResearchPostprocessV3Error as exc:
        raise ResearchPostprocessV5Error("shared v3 processing failed") from exc
    return _write_terminals_and_reports(loaded, terminals)


def verify_research_postprocess_v5(
    loaded: LoadedResearchPostprocessV5,
) -> ResearchPostprocessV5Manifest:
    """Replay-verify the v5 envelope without Lean execution or writes."""

    manifest = postprocess_v1._load_canonical(
        loaded.base.output_root / "manifest.json",
        ResearchPostprocessV5Manifest,
    )
    if manifest.input_binding != loaded.input_binding:
        raise ResearchPostprocessV5Error("persisted v5 input binding differs")
    for artifact, digest in manifest.terminal_artifacts.items():
        path = loaded.base.repo_root / artifact
        if hash_file(path) != digest:
            raise ResearchPostprocessV5Error("v5 terminal hash mismatch")
        terminal = postprocess_v1._load_canonical(
            path,
            ResearchPostprocessV5Terminal,
        )
        for output, output_digest in terminal.output_artifact_hashes.items():
            if hash_file(loaded.base.repo_root / output) != output_digest:
                raise ResearchPostprocessV5Error("v5 output artifact hash mismatch")
    for artifact, digest in manifest.family_report_artifacts.items():
        path = loaded.base.repo_root / artifact
        if hash_file(path) != digest:
            raise ResearchPostprocessV5Error("v5 family report hash mismatch")
        postprocess_v1._load_canonical(path, ResearchPostprocessV5FamilyReport)
    return manifest


__all__ = [
    "LoadedResearchPostprocessV5",
    "ResearchPostprocessV5Error",
    "ResearchPostprocessV5FamilyReport",
    "ResearchPostprocessV5InputBinding",
    "ResearchPostprocessV5Manifest",
    "ResearchPostprocessV5Run",
    "ResearchPostprocessV5Terminal",
    "load_research_postprocess_v5",
    "run_research_postprocess_v5",
    "validate_collection_v4_denominator",
    "verify_research_postprocess_v5",
]
