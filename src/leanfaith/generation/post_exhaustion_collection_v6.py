"""Executable, append-only collector for the frozen LF-021 extension sequence.

The older :mod:`post_exhaustion_collection_v1` module is intentionally a
non-executing authorization and config-plan adapter.  This module does not
change it.  Instead, it adds a separately versioned execution envelope which
accepts only a config/plan emitted by that adapter and only after replaying the
exact frozen post-exhaustion ``collect_next_extension_tranche`` decision.

The runtime unit remains one public reference-hidden problem x one of the
three pinned local model families x the exact frozen seed.  Collection creates
raw operational evidence only: no semantic label, supervision admission, or
Gate claim.
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation import post_exhaustion_collection_v1 as planning_v1
from leanfaith.generation import research_collection as collection_v1
from leanfaith.generation import research_collection_v3 as collection_v3
from leanfaith.generation import research_collection_v5 as collection_v5
from leanfaith.generation import tranche_expansion as tranche_v1
from leanfaith.generation.local_qualification import LocalQualificationConfig
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.nl_lean import ProblemPoolRecord

_HEX64 = r"^[0-9a-f]{64}$"
_TRANCHE = r"^[a-z][a-z0-9_]*$"
_EXECUTION_CONFIG_ID = r"^lf021_post_exhaustion_execution_config_v6:[0-9a-f]{64}$"
_MANIFEST_ID = r"^lf021_post_exhaustion_collection_manifest_v6:[0-9a-f]{64}$"
_POLICY_ID = "lf021_post_exhaustion_execution_v1"
_DEFAULT_POLICY = Path("configs/generation/lf021_post_exhaustion_execution_v1.yaml")
_COLLECTOR_ARTIFACT = "src/leanfaith/generation/post_exhaustion_collection_v6.py"
_COLLECTOR_CLI = "scripts/33_collect_post_exhaustion_tranche_v6.py"
_POSTPROCESS_ARTIFACT = "src/leanfaith/generation/post_exhaustion_postprocess_v7.py"
_POSTPROCESS_CLI = "scripts/34_postprocess_post_exhaustion_tranche_v7.py"
_EXPECTED_FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)
_EXPECTED_TRANCHES = (
    "algebra_s6",
    "cross_domain_s6",
    "algebra_s7",
    "cross_domain_s7",
)
_POOL_SLUGS = {
    "gate3_algebra_operational_v1": "gate3_docstrings_operational_v1",
    "cross_domain_operational_v1": "cross_domain_docstrings_operational_v1",
}


class PostExhaustionCollectionV6Error(RuntimeError):
    """An extension execution authorization or artifact failed closed."""


class PostExhaustionCollectionV6ExecutionBlocked(PostExhaustionCollectionV6Error):
    """Execution was requested without a complete replayed authorization."""


class PostExhaustionCollectionV6ArtifactConflict(PostExhaustionCollectionV6Error):
    """An append-only artifact exists with different or invalid bytes."""


class PostExhaustionExecutionPolicyV1(StrictModel):
    """Frozen bridge from the reviewed plan-only adapter to v6/v7 execution."""

    schema_version: Literal[1] = 1
    policy_id: Literal["lf021_post_exhaustion_execution_v1"]
    status: Literal["frozen_prelabel"]
    authorization_policy: tranche_v1.ArtifactBinding
    authorization_adapter_implementation: tranche_v1.ArtifactBinding
    collector_implementation: tranche_v1.ArtifactBinding
    collector_cli: tranche_v1.ArtifactBinding
    postprocess_implementation: tranche_v1.ArtifactBinding
    postprocess_cli: tranche_v1.ArtifactBinding
    required_planning_config_schema_version: Literal[6]
    required_planning_plan_schema_version: Literal[6]
    collection_manifest_schema_version: Literal[6]
    postprocess_schema_version: Literal[7]
    required_families: tuple[str, str, str]
    required_transport: Literal["local"]
    exact_extension_tranche_ids: tuple[str, str, str, str]
    execution_enabled: Literal[True]
    preparation_output_root: Literal[
        "reports/generation/lf021_post_exhaustion_execution_configs_v6"
    ]
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.required_families != _EXPECTED_FAMILIES:
            raise ValueError("v6 execution family inventory differs")
        if self.exact_extension_tranche_ids != _EXPECTED_TRANCHES:
            raise ValueError("v6 execution tranche sequence differs")
        return self


class ExecutablePostExhaustionCollectionConfigV6(StrictModel):
    """One executable envelope around an immutable plan-only v6 config/plan."""

    schema_version: Literal[6] = 6
    config_id: str = Field(pattern=_EXECUTION_CONFIG_ID)
    frozen_at: datetime.datetime
    execution_policy: tranche_v1.ArtifactBinding
    authorization_policy: tranche_v1.ArtifactBinding
    authorization: tranche_v1.ArtifactBinding
    authorization_id: str
    extension_decision: tranche_v1.ArtifactBinding
    extension_decision_id: str
    planning_config: tranche_v1.ArtifactBinding
    planning_config_id: str
    planning_config_hash: str = Field(pattern=_HEX64)
    planning_plan: tranche_v1.ArtifactBinding
    planning_plan_id: str
    planning_plan_hash: str = Field(pattern=_HEX64)
    collector_implementation: tranche_v1.ArtifactBinding
    collector_cli: tranche_v1.ArtifactBinding
    required_postprocess_implementation: tranche_v1.ArtifactBinding
    required_postprocess_cli: tranche_v1.ArtifactBinding
    tranche_id: str = Field(pattern=_TRANCHE)
    tranche_order: int = Field(ge=12, le=15)
    pool_id: str
    pool_dialect: collection_v5.PoolDialect
    artifact_class: Literal["research"] = "research"
    collection_scope: Literal["post_exhaustion_closed_pool_three_local_family_tranche_v6"]
    required_transport: Literal["local"]
    execution_enabled: Literal[True]
    output_root: str
    preflight_report: str
    semantic_labels_inspected: Literal[False] = False
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
        if self.tranche_id != _EXPECTED_TRANCHES[self.tranche_order - 12]:
            raise ValueError("v6 execution tranche order and ID differ")
        pool_slug = _POOL_SLUGS[self.pool_dialect]
        expected_root = f"data/raw/real_outputs/{pool_slug}/v6/{self.tranche_id}/local_collection"
        expected_preflight = (
            "reports/generation/lf021_post_exhaustion_collection_preflights_v6/"
            f"{self.tranche_id}/{self.planning_plan_id.rsplit(':', 1)[-1]}.json"
        )
        if self.output_root != expected_root or self.preflight_report != expected_preflight:
            raise ValueError("v6 execution output paths differ from frozen plan")
        expected = "lf021_post_exhaustion_execution_config_v6:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_execution_config_v6",
                **self.model_dump(mode="json", exclude={"config_id"}),
            }
        )
        if self.config_id != expected:
            raise ValueError("v6 execution config ID differs from content")
        return self


class PostExhaustionCollectionPreflightV6(StrictModel):
    """Model-free replay report for one executable extension tranche."""

    schema_version: Literal[6] = 6
    report_kind: Literal["lf021_post_exhaustion_collection_preflight_v6"]
    passed: Literal[True] = True
    execution_ready: Literal[True] = True
    execution_config_id: str = Field(pattern=_EXECUTION_CONFIG_ID)
    execution_config_hash: str = Field(pattern=_HEX64)
    authorization_id: str
    extension_decision_id: str
    planning_config_id: str
    planning_plan_id: str
    planning_plan_hash: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    tranche_order: int = Field(ge=12, le=15)
    pool_id: str
    pool_dialect: collection_v5.PoolDialect
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    seed_count_by_family: dict[str, int]
    planned_candidate_count: int = Field(ge=1)
    invocation_ids: tuple[str, ...] = Field(min_length=1)
    checks: dict[str, Literal[True]]
    actual_collection_performed: Literal[False] = False
    gpu_model_execution_performed: Literal[False] = False
    remote_provider_requests_created: Literal[0] = 0
    private_source_records_used: Literal[0] = 0
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if (
            expected != self.planned_candidate_count
            or len(self.invocation_ids) != expected
            or self.invocation_ids != tuple(sorted(set(self.invocation_ids)))
            or not self.checks
            or not all(self.checks.values())
        ):
            raise ValueError("v6 preflight denominator or checks differ")
        return self


class PostExhaustionCollectionManifestV6(StrictModel):
    """Complete append-only accounting for one extension collector run."""

    schema_version: Literal[6] = 6
    manifest_id: str = Field(pattern=_MANIFEST_ID)
    execution_config_id: str = Field(pattern=_EXECUTION_CONFIG_ID)
    execution_config_hash: str = Field(pattern=_HEX64)
    execution_config: tranche_v1.ArtifactBinding
    authorization_id: str
    extension_decision_id: str
    planning_config_id: str
    planning_plan_id: str
    planning_plan_hash: str = Field(pattern=_HEX64)
    tranche_id: str = Field(pattern=_TRANCHE)
    tranche_order: int = Field(ge=12, le=15)
    pool_id: str
    pool_dialect: collection_v5.PoolDialect
    shared_execution_record_schema: Literal["lf021_research_execution_records_v1"]
    actual_collection_performed: Literal[True] = True
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    seed_count_by_family: dict[str, int]
    expected_candidate_count: int = Field(ge=1)
    terminal_candidate_count: int = Field(ge=1)
    status_counts: dict[str, int]
    successful_family_count: int = Field(ge=0, le=3)
    terminal_artifact_hashes: dict[str, str]
    family_session_artifact_hashes: dict[str, str]
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
        expected = self.problem_count * sum(self.seed_count_by_family.values())
        if (
            expected != self.expected_candidate_count
            or self.terminal_candidate_count != expected
            or sum(self.status_counts.values()) != expected
            or len(self.terminal_artifact_hashes) != expected
        ):
            raise ValueError("v6 collection manifest accounting differs")
        for field_name in (
            "terminal_artifact_hashes",
            "family_session_artifact_hashes",
        ):
            mapping = getattr(self, field_name)
            if list(mapping) != sorted(mapping):
                raise ValueError(f"{field_name} must be sorted")
        expected_id = "lf021_post_exhaustion_collection_manifest_v6:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_collection_manifest_v6",
                **self.id_payload,
            }
        )
        if self.manifest_id != expected_id:
            raise ValueError("v6 collection manifest ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class LoadedPostExhaustionCollectionV6:
    config: LoadedConfig[ExecutablePostExhaustionCollectionConfigV6]
    policy: LoadedConfig[PostExhaustionExecutionPolicyV1]
    authorization: planning_v1.ReviewedExtensionCollectionAuthorizationV1
    planning_config: planning_v1.PostExhaustionCollectionConfigV6
    planning_plan: planning_v1.PostExhaustionCollectionPlanV6
    template: collection_v5.LoadedResearchCollectionV5
    problems: tuple[ProblemPoolRecord, ...]
    qualifications: Mapping[str, LoadedConfig[LocalQualificationConfig]]
    preflight: PostExhaustionCollectionPreflightV6


@dataclass(frozen=True, slots=True)
class PostExhaustionCollectionRunV6:
    output_directory: Path
    plan_path: Path
    execution_config_path: Path
    manifest_path: Path
    manifest: PostExhaustionCollectionManifestV6
    terminals: tuple[collection_v1.ResearchCollectionTerminal, ...]


class PostExhaustionInvocationExecutorV6(Protocol):
    """The audited local executor boundary reused without remote transports."""

    def begin_family(
        self,
        *,
        family: collection_v1.ResearchFamilyBinding,
        qualification: LoadedConfig[LocalQualificationConfig],
        runtime: collection_v1.ResearchRuntimeBinding,
        invocations: tuple[collection_v1.ResearchCollectionInvocation, ...],
        family_directory: Path,
    ) -> None: ...

    def execute(
        self,
        *,
        invocation: collection_v1.ResearchCollectionInvocation,
        problem: ProblemPoolRecord,
        qualification: LoadedConfig[LocalQualificationConfig],
        invocation_directory: Path,
        artifact_root: Path,
    ) -> collection_v1.ResearchCollectionTerminal: ...

    def end_family(
        self,
        *,
        family: collection_v1.ResearchFamilyBinding,
        completed_invocation_ids: tuple[str, ...],
        family_directory: Path,
    ) -> None: ...


def _strict_json(path: Path) -> dict[str, Any]:
    try:
        return planning_v1._strict_json_object(path)
    except planning_v1.PostExhaustionCollectionV1Error as exc:
        raise PostExhaustionCollectionV6Error(str(exc)) from exc


def _resolve(repo_root: Path, artifact: str) -> Path:
    try:
        return planning_v1._resolve(repo_root, artifact)
    except planning_v1.PostExhaustionCollectionV1Error as exc:
        raise PostExhaustionCollectionV6Error(str(exc)) from exc


def _require_repo_path_without_symlinks(
    *,
    repo_root: Path,
    path: Path,
    label: str,
) -> Path:
    """Reject repository escapes and every existing symlink path component."""

    root = repo_root.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise PostExhaustionCollectionV6ArtifactConflict(
            f"{label} escapes repository: {path}"
        ) from exc
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise PostExhaustionCollectionV6ArtifactConflict(
                f"{label} contains a symlink component: {cursor}"
            )
    if not candidate.resolve(strict=False).is_relative_to(root):
        raise PostExhaustionCollectionV6ArtifactConflict(
            f"{label} resolves outside repository: {path}"
        )
    return candidate


def _verify_binding(
    repo_root: Path,
    binding: tranche_v1.ArtifactBinding | collection_v1.ResearchArtifactBinding,
) -> Path:
    strict_path = _require_repo_path_without_symlinks(
        repo_root=repo_root,
        path=Path(binding.artifact),
        label=f"bound artifact {binding.artifact}",
    )
    try:
        path = planning_v1._verify_binding(repo_root, binding)
    except planning_v1.PostExhaustionCollectionV1Error as exc:
        raise PostExhaustionCollectionV6Error(str(exc)) from exc
    if path.resolve() != strict_path.resolve():
        raise PostExhaustionCollectionV6Error(
            f"bound artifact path differs after strict resolution: {binding.artifact}"
        )
    if not path.is_relative_to(repo_root.resolve()):
        raise PostExhaustionCollectionV6Error(
            f"extension artifact must remain in repository: {binding.artifact}"
        )
    return path


def _binding(repo_root: Path, path: Path) -> tranche_v1.ArtifactBinding:
    return tranche_v1.ArtifactBinding(
        artifact=tranche_v1._relative_or_absolute(repo_root, path),
        sha256=hash_file(path),
    )


def _write_immutable(path: Path, payload: bytes) -> str:
    try:
        return collection_v1._write_immutable(path, payload)
    except collection_v1.ResearchCollectionArtifactConflict as exc:
        raise PostExhaustionCollectionV6ArtifactConflict(str(exc)) from exc


def _canonical(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def load_post_exhaustion_execution_policy_v1(
    path: Path,
) -> LoadedConfig[PostExhaustionExecutionPolicyV1]:
    return load_config(path, PostExhaustionExecutionPolicyV1)


def _verify_execution_policy(
    *,
    repo_root: Path,
    loaded: LoadedConfig[PostExhaustionExecutionPolicyV1],
) -> None:
    policy = loaded.config
    expected_paths = {
        "authorization_policy": (
            _verify_binding(repo_root, policy.authorization_policy),
            _DEFAULT_POLICY.with_name("lf021_post_exhaustion_collection_v1.yaml"),
        ),
        "authorization_adapter_implementation": (
            _verify_binding(repo_root, policy.authorization_adapter_implementation),
            Path(planning_v1.__file__),
        ),
        "collector_implementation": (
            _verify_binding(repo_root, policy.collector_implementation),
            Path(__file__),
        ),
        "collector_cli": (
            _verify_binding(repo_root, policy.collector_cli),
            repo_root / _COLLECTOR_CLI,
        ),
        "postprocess_implementation": (
            _verify_binding(repo_root, policy.postprocess_implementation),
            repo_root / _POSTPROCESS_ARTIFACT,
        ),
        "postprocess_cli": (
            _verify_binding(repo_root, policy.postprocess_cli),
            repo_root / _POSTPROCESS_CLI,
        ),
    }
    for label, (observed, expected) in expected_paths.items():
        expected_path = expected if expected.is_absolute() else repo_root / expected
        if observed.resolve() != expected_path.resolve():
            raise PostExhaustionCollectionV6Error(
                f"execution policy {label} path differs from imported implementation"
            )


def _load_planning_artifacts(
    *,
    repo_root: Path,
    execution_config: ExecutablePostExhaustionCollectionConfigV6,
) -> tuple[
    planning_v1.ReviewedExtensionCollectionAuthorizationV1,
    planning_v1.PostExhaustionCollectionConfigV6,
    planning_v1.PostExhaustionCollectionPlanV6,
    Path,
    Path,
]:
    authorization_path = _verify_binding(repo_root, execution_config.authorization)
    planning_config_path = _verify_binding(repo_root, execution_config.planning_config)
    planning_plan_path = _verify_binding(repo_root, execution_config.planning_plan)
    try:
        reviewed = planning_v1.load_verified_reviewed_extension_collection_authorization_v1(
            repo_root=repo_root,
            authorization_policy_path=_verify_binding(
                repo_root, execution_config.authorization_policy
            ),
            authorization_path=authorization_path,
        )
    except planning_v1.PostExhaustionCollectionV1Error as exc:
        raise PostExhaustionCollectionV6Error(
            "reviewed extension authorization does not replay"
        ) from exc
    planning_config = planning_v1.PostExhaustionCollectionConfigV6.model_validate(
        _strict_json(planning_config_path)
    )
    planning_plan = planning_v1.PostExhaustionCollectionPlanV6.model_validate(
        _strict_json(planning_plan_path)
    )
    if (
        execution_config.authorization_id != reviewed.authorization.authorization_id
        or execution_config.extension_decision != reviewed.authorization.extension_decision
        or execution_config.extension_decision_id != reviewed.authorization.extension_decision_id
        or execution_config.planning_config_id != planning_config.config_id
        or execution_config.planning_config_hash != planning_config.config_hash
        or execution_config.planning_plan_id != planning_plan.plan_id
        or execution_config.planning_plan_hash
        != hash_canonical(planning_plan.model_dump(mode="json"))
        or planning_plan.config != execution_config.planning_config
        or planning_plan.config_id != planning_config.config_id
        or planning_plan.config_hash != planning_config.config_hash
        or planning_plan.authorization != execution_config.authorization
        or planning_plan.authorization_id != reviewed.authorization.authorization_id
    ):
        raise PostExhaustionCollectionV6Error(
            "execution config differs from reviewed planning artifacts"
        )
    return (
        reviewed.authorization,
        planning_config,
        planning_plan,
        planning_config_path,
        planning_plan_path,
    )


def _replay_planning_denominator(
    *,
    repo_root: Path,
    authorization: planning_v1.ReviewedExtensionCollectionAuthorizationV1,
    planning_config: planning_v1.PostExhaustionCollectionConfigV6,
    planning_plan: planning_v1.PostExhaustionCollectionPlanV6,
) -> collection_v5.LoadedResearchCollectionV5:
    template_path = _verify_binding(repo_root, planning_config.base_collection_template)
    try:
        template = collection_v5.load_research_collection_v5(
            template_path,
            repo_root=repo_root,
        )
    except (collection_v5.ResearchCollectionV5Error, OSError, ValueError) as exc:
        raise PostExhaustionCollectionV6Error("base collector-v5 template does not replay") from exc
    family_by_id = {item.family_id: item for item in planning_config.families}
    if tuple(family_by_id) != _EXPECTED_FAMILIES:
        raise PostExhaustionCollectionV6Error("planning family order differs")
    expected_bindings = tuple(
        collection_v1._family_binding(
            family=family_by_id[family_id],
            loaded=template.qualifications[family_id],
            config_file_sha256=hash_file(
                _verify_binding(
                    repo_root,
                    family_by_id[family_id].qualification_pin_source,
                )
            ),
            runtime=planning_config.runtime,
        )
        for family_id in _EXPECTED_FAMILIES
    )
    header_path = _verify_binding(repo_root, planning_config.import_header)
    expected_invocations = collection_v3._make_invocations(
        config_hash=planning_config.config_hash,
        config=cast(Any, planning_config),
        family_bindings=expected_bindings,
        qualifications=template.qualifications,
        problems=template.problems,
        repo_root=repo_root,
        context=template.context,
        header_text=header_path.read_text(encoding="utf-8"),
    )
    authorized = authorization.authorized_tranche
    if (
        planning_config.authorization_id != authorization.authorization_id
        or planning_config.extension_decision != authorization.extension_decision
        or planning_config.extension_decision_id != authorization.extension_decision_id
        or planning_config.tranche_id != authorized.tranche_id
        or planning_config.pool_id != authorized.pool_id
        or planning_plan.tranche_id != authorized.tranche_id
        or planning_plan.pool_id != authorized.pool_id
        or planning_plan.problem_count != authorized.expected_problem_count
        or planning_plan.expected_candidate_count != authorized.expected_invocations
        or planning_plan.family_bindings != expected_bindings
        or planning_plan.invocations != expected_invocations
        or planning_plan.problem_record_ids
        != tuple(sorted(item.problem_record_id for item in template.problems))
        or any(item.private_source_content for item in template.problems)
        or any(item.eligibility != "eligible" for item in template.problems)
        or any(not item.reference_theorem_ids for item in template.problems)
        or planning_config.problem_pool_contract.require_reference_hidden is not True
        or planning_config.problem_pool_contract.require_public_records is not True
        or planning_config.problem_pool_contract.require_no_semantic_labels is not True
        or planning_config.problem_pool_contract.require_no_gate_claims is not True
    ):
        raise PostExhaustionCollectionV6Error(
            "planning denominator differs from exact local public extension authorization"
        )
    expected_seeds = {family_id: (seed,) for family_id, seed in authorized.seeds_by_family.items()}
    if {item.family_id: item.seeds for item in planning_config.families} != expected_seeds:
        raise PostExhaustionCollectionV6Error("planning seeds differ from authorized tranche")
    matrix_families = tuple(
        (item.family_id, item.transport) for item in template.source_matrix.families
    )
    if matrix_families != tuple((family_id, "local") for family_id in _EXPECTED_FAMILIES):
        raise PostExhaustionCollectionV6Error("extension source matrix is not three-family local")
    return template


def prepare_post_exhaustion_collection_v6(
    *,
    repo_root: Path,
    execution_policy_path: Path,
    authorization_path: Path,
    frozen_at: datetime.datetime,
    planning_output_root: Path,
    output_config_path: Path,
) -> tuple[Path, str]:
    """Create, but do not execute, a replay-bound extension execution config."""

    require_utc(frozen_at)
    root = repo_root.resolve()
    output = _require_repo_path_without_symlinks(
        repo_root=root,
        path=output_config_path,
        label="execution config",
    )
    planning_root = _require_repo_path_without_symlinks(
        repo_root=root,
        path=planning_output_root,
        label="planning output",
    )
    loaded_policy = load_post_exhaustion_execution_policy_v1(execution_policy_path)
    _verify_execution_policy(repo_root=root, loaded=loaded_policy)
    planning_run = planning_v1.write_post_exhaustion_collection_config_plan_v1(
        repo_root=root,
        authorization_policy_path=_verify_binding(root, loaded_policy.config.authorization_policy),
        authorization_path=authorization_path,
        frozen_at=frozen_at,
        output_root=planning_root,
    )
    reviewed = planning_v1.load_verified_reviewed_extension_collection_authorization_v1(
        repo_root=root,
        authorization_policy_path=_verify_binding(root, loaded_policy.config.authorization_policy),
        authorization_path=authorization_path,
    )
    authorization = reviewed.authorization
    planning_config = planning_run.config
    planning_plan = planning_run.plan
    payload: dict[str, object] = {
        "schema_version": 6,
        "frozen_at": frozen_at.isoformat().replace("+00:00", "Z"),
        "execution_policy": _binding(root, loaded_policy.path).model_dump(mode="json"),
        "authorization_policy": loaded_policy.config.authorization_policy.model_dump(mode="json"),
        "authorization": reviewed.binding.model_dump(mode="json"),
        "authorization_id": authorization.authorization_id,
        "extension_decision": authorization.extension_decision.model_dump(mode="json"),
        "extension_decision_id": authorization.extension_decision_id,
        "planning_config": _binding(root, planning_run.config_path).model_dump(mode="json"),
        "planning_config_id": planning_config.config_id,
        "planning_config_hash": planning_config.config_hash,
        "planning_plan": _binding(root, planning_run.plan_path).model_dump(mode="json"),
        "planning_plan_id": planning_plan.plan_id,
        "planning_plan_hash": hash_canonical(planning_plan.model_dump(mode="json")),
        "collector_implementation": loaded_policy.config.collector_implementation.model_dump(
            mode="json"
        ),
        "collector_cli": loaded_policy.config.collector_cli.model_dump(mode="json"),
        "required_postprocess_implementation": (
            loaded_policy.config.postprocess_implementation.model_dump(mode="json")
        ),
        "required_postprocess_cli": loaded_policy.config.postprocess_cli.model_dump(mode="json"),
        "tranche_id": authorization.authorized_tranche.tranche_id,
        "tranche_order": authorization.authorized_tranche.order,
        "pool_id": authorization.authorized_tranche.pool_id,
        "pool_dialect": planning_config.pool_dialect,
        "artifact_class": "research",
        "collection_scope": "post_exhaustion_closed_pool_three_local_family_tranche_v6",
        "required_transport": "local",
        "execution_enabled": True,
        "output_root": planning_config.outputs.root,
        "preflight_report": (
            "reports/generation/lf021_post_exhaustion_collection_preflights_v6/"
            f"{authorization.authorized_tranche.tranche_id}/"
            f"{planning_plan.plan_id.rsplit(':', 1)[-1]}.json"
        ),
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    config_id = "lf021_post_exhaustion_execution_config_v6:" + hash_canonical(
        {"schema": "lf021_post_exhaustion_execution_config_v6", **payload}
    )
    config = ExecutablePostExhaustionCollectionConfigV6.model_validate(
        {"config_id": config_id, **payload}
    )
    digest = _write_immutable(output, _canonical(config))
    return output, digest


def load_post_exhaustion_collection_v6(
    config_path: Path,
    *,
    repo_root: Path,
) -> LoadedPostExhaustionCollectionV6:
    """Replay all extension, planning, public-pool, and local-family bindings."""

    root = repo_root.resolve()
    path = _require_repo_path_without_symlinks(
        repo_root=root,
        path=config_path,
        label="v6 execution config",
    )
    if not path.is_file():
        raise PostExhaustionCollectionV6Error("v6 execution config is missing")
    loaded_config = load_config(path, ExecutablePostExhaustionCollectionConfigV6)
    execution_config = loaded_config.config
    policy_path = _verify_binding(root, execution_config.execution_policy)
    loaded_policy = load_post_exhaustion_execution_policy_v1(policy_path)
    _verify_execution_policy(repo_root=root, loaded=loaded_policy)
    policy = loaded_policy.config
    if (
        execution_config.execution_policy != _binding(root, loaded_policy.path)
        or execution_config.authorization_policy != policy.authorization_policy
        or execution_config.collector_implementation != policy.collector_implementation
        or execution_config.collector_cli != policy.collector_cli
        or execution_config.required_postprocess_implementation != policy.postprocess_implementation
        or execution_config.required_postprocess_cli != policy.postprocess_cli
        or execution_config.tranche_id not in policy.exact_extension_tranche_ids
    ):
        raise PostExhaustionCollectionV6Error("v6 execution config differs from frozen policy")
    (
        authorization,
        planning_config,
        planning_plan,
        _,
        _,
    ) = _load_planning_artifacts(
        repo_root=root,
        execution_config=execution_config,
    )
    template = _replay_planning_denominator(
        repo_root=root,
        authorization=authorization,
        planning_config=planning_config,
        planning_plan=planning_plan,
    )
    if (
        execution_config.tranche_id != authorization.authorized_tranche.tranche_id
        or execution_config.tranche_order != authorization.authorized_tranche.order
        or execution_config.pool_id != authorization.authorized_tranche.pool_id
        or execution_config.pool_dialect != planning_config.pool_dialect
        or execution_config.output_root != planning_config.outputs.root
    ):
        raise PostExhaustionCollectionV6Error("v6 execution projection differs from authorization")
    preflight = PostExhaustionCollectionPreflightV6(
        report_kind="lf021_post_exhaustion_collection_preflight_v6",
        execution_config_id=execution_config.config_id,
        execution_config_hash=execution_config.config_hash,
        authorization_id=authorization.authorization_id,
        extension_decision_id=authorization.extension_decision_id,
        planning_config_id=planning_config.config_id,
        planning_plan_id=planning_plan.plan_id,
        planning_plan_hash=hash_canonical(planning_plan.model_dump(mode="json")),
        tranche_id=execution_config.tranche_id,
        tranche_order=execution_config.tranche_order,
        pool_id=execution_config.pool_id,
        pool_dialect=execution_config.pool_dialect,
        problem_count=planning_plan.problem_count,
        seed_count_by_family=planning_plan.seed_count_by_family,
        planned_candidate_count=planning_plan.expected_candidate_count,
        invocation_ids=tuple(item.invocation_id for item in planning_plan.invocations),
        checks={
            "all_twelve_original_tranches_precede_extension": True,
            "authorization_and_collect_next_decision_replayed": True,
            "config_plan_adapter_artifacts_replayed": True,
            "exact_extension_order_pool_and_seeds_match": True,
            "exactly_three_pinned_local_families": True,
            "execution_and_postprocess_implementations_hash_bound": True,
            "public_reference_hidden_pool_replayed": True,
            "zero_private_or_remote_inputs": True,
            "zero_semantic_labels_supervision_or_gate_claims": True,
        },
    )
    return LoadedPostExhaustionCollectionV6(
        config=loaded_config,
        policy=loaded_policy,
        authorization=authorization,
        planning_config=planning_config,
        planning_plan=planning_plan,
        template=template,
        problems=template.problems,
        qualifications=template.qualifications,
        preflight=preflight,
    )


def write_post_exhaustion_collection_preflight_v6(
    loaded: LoadedPostExhaustionCollectionV6,
    *,
    repo_root: Path,
) -> tuple[Path, str]:
    path = repo_root / loaded.config.config.preflight_report
    return path, _write_immutable(path, _canonical(loaded.preflight))


def _terminal_path(
    root: Path,
    invocation: collection_v1.ResearchCollectionInvocation,
) -> Path:
    return root / "terminals" / f"{invocation.invocation_id.rsplit(':', 1)[-1]}.json"


def _load_existing_terminal(
    *,
    root: Path,
    invocation: collection_v1.ResearchCollectionInvocation,
) -> collection_v1.ResearchCollectionTerminal | None:
    path = _terminal_path(root, invocation)
    if path.is_symlink():
        raise PostExhaustionCollectionV6ArtifactConflict(f"persisted terminal is a symlink: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise PostExhaustionCollectionV6ArtifactConflict(
            f"persisted terminal is not a regular file: {path}"
        )
    try:
        terminal = collection_v1._load_canonical(
            path,
            collection_v1.ResearchCollectionTerminal,
        )
    except (OSError, ValueError, collection_v1.ResearchCollectionError) as exc:
        raise PostExhaustionCollectionV6ArtifactConflict(
            f"invalid persisted terminal: {path}"
        ) from exc
    if (
        terminal.invocation_id != invocation.invocation_id
        or terminal.invocation_payload_hash != hash_canonical(invocation.model_dump(mode="json"))
        or terminal.family_id != invocation.family_id
        or terminal.problem_record_id != invocation.problem_record_id
        or terminal.seed != invocation.seed
    ):
        raise PostExhaustionCollectionV6ArtifactConflict(
            f"terminal differs from invocation: {path}"
        )
    return terminal


def _has_model_attempt_boundary(
    root: Path,
    invocation: collection_v1.ResearchCollectionInvocation,
) -> bool:
    invocation_directory = root / "invocations" / invocation.invocation_id.rsplit(":", 1)[-1]
    for name in ("provider_request.json", "provider_boundary.json"):
        path = invocation_directory / name
        if path.is_symlink():
            raise PostExhaustionCollectionV6ArtifactConflict(
                f"model-attempt boundary is a symlink: {path}"
            )
        if path.exists():
            if not path.is_file():
                raise PostExhaustionCollectionV6ArtifactConflict(
                    f"model-attempt boundary is not a regular file: {path}"
                )
            return True
    return False


def _verify_terminal_artifacts(
    *,
    repo_root: Path,
    root: Path,
    invocation: collection_v1.ResearchCollectionInvocation,
    terminal: collection_v1.ResearchCollectionTerminal,
) -> None:
    """Replay every artifact hash before a terminal can be resumed or sealed."""

    invocation_directory = root / "invocations" / invocation.invocation_id.rsplit(":", 1)[-1]
    fixed_paths = {
        "llm_attempt": invocation_directory / "llm_attempt.json",
        "llm_call": invocation_directory / "llm_call.json",
        "local_generation_result": invocation_directory / "local_generation_result.json",
        "local_runtime_failure": invocation_directory / "local_runtime_failure.json",
        "model_attempt_boundary": invocation_directory / "model_attempt_boundary.json",
        "provider_boundary": invocation_directory / "provider_boundary.json",
        "provider_lineage_failure": invocation_directory / "provider_lineage_failure.json",
        "provider_request": invocation_directory / "provider_request.json",
    }
    if terminal.family_session_id is not None:
        fixed_paths["family_session_start"] = (
            root
            / "families"
            / terminal.family_id
            / "sessions"
            / terminal.family_session_id.rsplit(":", 1)[-1]
            / "family_session_start.json"
        )
    observed: dict[str, Path] = {}
    for key in terminal.artifact_hashes:
        if key == "provider_raw_response":
            call_path = fixed_paths["llm_call"]
            if call_path.is_symlink() or not call_path.is_file():
                raise PostExhaustionCollectionV6ArtifactConflict(
                    f"raw terminal lacks its LLM call: {terminal.invocation_id}"
                )
            call = _strict_json(call_path)
            raw_artifact = call.get("raw_output_artifact")
            if not isinstance(raw_artifact, str) or not raw_artifact:
                raise PostExhaustionCollectionV6ArtifactConflict(
                    f"LLM call lacks a raw-output artifact: {terminal.invocation_id}"
                )
            observed[key] = _require_repo_path_without_symlinks(
                repo_root=repo_root,
                path=Path(raw_artifact),
                label="provider raw response",
            )
        else:
            try:
                observed[key] = fixed_paths[key]
            except KeyError as exc:
                raise PostExhaustionCollectionV6ArtifactConflict(
                    f"terminal contains an unsupported artifact key: {key}"
                ) from exc
    for key, path in observed.items():
        _require_repo_path_without_symlinks(
            repo_root=repo_root,
            path=path,
            label=f"terminal artifact {key}",
        )
        if not path.is_file() or hash_file(path) != terminal.artifact_hashes[key]:
            raise PostExhaustionCollectionV6ArtifactConflict(
                f"terminal artifact hash differs for {key}: {terminal.invocation_id}"
            )


def _verify_family_session(
    *,
    repo_root: Path,
    output_root: Path,
    terminal: collection_v1.ResearchCollectionTerminal,
) -> None:
    if terminal.family_session_id is None:
        return
    path = (
        output_root
        / "families"
        / terminal.family_id
        / "sessions"
        / terminal.family_session_id.rsplit(":", 1)[-1]
        / "family_session_start.json"
    )
    expected = terminal.artifact_hashes.get("family_session_start")
    if (
        expected is None
        or path.is_symlink()
        or not path.is_file()
        or hash_file(path) != expected
        or not path.resolve().is_relative_to(repo_root.resolve())
    ):
        raise PostExhaustionCollectionV6ArtifactConflict(
            f"terminal family-session binding differs: {terminal.invocation_id}"
        )


def _run_root(loaded: LoadedPostExhaustionCollectionV6, repo_root: Path) -> Path:
    return (
        repo_root
        / loaded.config.config.output_root
        / loaded.planning_plan.plan_id.rsplit(":", 1)[-1]
    )


def execute_post_exhaustion_collection_v6(
    loaded: LoadedPostExhaustionCollectionV6,
    *,
    repo_root: Path,
    executor: PostExhaustionInvocationExecutorV6,
    clock: collection_v1.Clock = lambda: datetime.datetime.now(datetime.UTC),
) -> PostExhaustionCollectionRunV6:
    """Execute or resume exactly one immutable terminal per extension invocation."""

    if not loaded.preflight.execution_ready or not loaded.config.config.execution_enabled:
        raise PostExhaustionCollectionV6ExecutionBlocked("v6 extension preflight is not ready")
    root = _run_root(loaded, repo_root)
    _require_repo_path_without_symlinks(
        repo_root=repo_root,
        path=root,
        label="v6 collection root",
    )
    plan_path = root / "plan.json"
    execution_config_path = root / "execution_config.json"
    _write_immutable(plan_path, _canonical(loaded.planning_plan))
    _write_immutable(execution_config_path, _canonical(loaded.config.config))
    problem_by_id = {item.problem_record_id: item for item in loaded.problems}
    terminals: list[collection_v1.ResearchCollectionTerminal] = []
    binding_by_family = {
        binding.family_id: binding for binding in loaded.planning_plan.family_bindings
    }
    for family_id in sorted(binding_by_family):
        family_invocations = tuple(
            invocation
            for invocation in loaded.planning_plan.invocations
            if invocation.family_id == family_id
        )
        pending: list[collection_v1.ResearchCollectionInvocation] = []
        for invocation in family_invocations:
            terminal = _load_existing_terminal(root=root, invocation=invocation)
            if terminal is None:
                if _has_model_attempt_boundary(root, invocation):
                    raise collection_v1.ResearchCollectionPostBoundaryError(
                        "an invocation has a durable model-attempt boundary but no terminal; "
                        "automatic resume is forbidden"
                    )
                pending.append(invocation)
            else:
                _verify_terminal_artifacts(
                    repo_root=repo_root,
                    root=root,
                    invocation=invocation,
                    terminal=terminal,
                )
                _verify_family_session(
                    repo_root=repo_root,
                    output_root=root,
                    terminal=terminal,
                )
                terminals.append(terminal)
        if not pending:
            continue
        binding = binding_by_family[family_id]
        family_directory = root / "families" / family_id
        try:
            executor.begin_family(
                family=binding,
                qualification=loaded.qualifications[family_id],
                runtime=loaded.planning_config.runtime,
                invocations=tuple(pending),
                family_directory=family_directory,
            )
        except Exception as exc:
            for invocation in pending:
                terminal = collection_v1.make_orchestration_failure_terminal(
                    invocation,
                    exception=exc,
                    at=clock(),
                )
                _write_immutable(_terminal_path(root, invocation), _canonical(terminal))
                terminals.append(terminal)
            continue
        completed: list[str] = []
        try:
            for invocation in pending:
                invocation_directory = (
                    root / "invocations" / invocation.invocation_id.rsplit(":", 1)[-1]
                )
                try:
                    terminal = executor.execute(
                        invocation=invocation,
                        problem=problem_by_id[invocation.problem_record_id],
                        qualification=loaded.qualifications[invocation.family_id],
                        invocation_directory=invocation_directory,
                        artifact_root=repo_root,
                    )
                except Exception as exc:
                    crossed = _has_model_attempt_boundary(root, invocation)
                    if crossed:
                        raise collection_v1.ResearchCollectionPostBoundaryError(
                            "executor raised after a model-attempt boundary; "
                            "the v6 extension run remains incomplete"
                        ) from exc
                    terminal = collection_v1.make_orchestration_failure_terminal(
                        invocation,
                        exception=exc,
                        at=clock(),
                    )
                if (
                    terminal.invocation_id != invocation.invocation_id
                    or terminal.invocation_payload_hash
                    != hash_canonical(invocation.model_dump(mode="json"))
                    or terminal.family_id != invocation.family_id
                    or terminal.problem_record_id != invocation.problem_record_id
                    or terminal.seed != invocation.seed
                ):
                    raise PostExhaustionCollectionV6ArtifactConflict(
                        "executor terminal differs from the frozen invocation"
                    )
                _verify_terminal_artifacts(
                    repo_root=repo_root,
                    root=root,
                    invocation=invocation,
                    terminal=terminal,
                )
                _write_immutable(_terminal_path(root, invocation), _canonical(terminal))
                terminals.append(terminal)
                completed.append(invocation.invocation_id)
        finally:
            executor.end_family(
                family=binding,
                completed_invocation_ids=tuple(completed),
                family_directory=family_directory,
            )
    terminals.sort(key=lambda item: item.invocation_id)
    if len(terminals) != loaded.planning_plan.expected_candidate_count or tuple(
        item.invocation_id for item in terminals
    ) != tuple(item.invocation_id for item in loaded.planning_plan.invocations):
        raise PostExhaustionCollectionV6ArtifactConflict(
            "v6 extension did not produce the exact invocation denominator"
        )
    for terminal in terminals:
        _verify_family_session(
            repo_root=repo_root,
            output_root=root,
            terminal=terminal,
        )
        invocation = next(
            item
            for item in loaded.planning_plan.invocations
            if item.invocation_id == terminal.invocation_id
        )
        _verify_terminal_artifacts(
            repo_root=repo_root,
            root=root,
            invocation=invocation,
            terminal=terminal,
        )
    terminal_hashes = {
        str(_terminal_path(root, invocation).relative_to(repo_root)): hash_file(
            _terminal_path(root, invocation)
        )
        for invocation in loaded.planning_plan.invocations
    }
    family_session_hashes = {
        str(path.relative_to(repo_root)): hash_file(path)
        for path in sorted((root / "families").glob("**/*.json"))
        if path.is_file() and not path.is_symlink()
    }
    statuses = Counter(item.status.value for item in terminals)
    successful = {
        item.family_id
        for item in terminals
        if item.status is collection_v1.ResearchTerminalStatus.RAW_COLLECTED
    }
    config_path = loaded.config.path
    payload: dict[str, object] = {
        "schema_version": 6,
        "execution_config_id": loaded.config.config.config_id,
        "execution_config_hash": loaded.config.config.config_hash,
        "execution_config": _binding(repo_root, config_path).model_dump(mode="json"),
        "authorization_id": loaded.authorization.authorization_id,
        "extension_decision_id": loaded.authorization.extension_decision_id,
        "planning_config_id": loaded.planning_config.config_id,
        "planning_plan_id": loaded.planning_plan.plan_id,
        "planning_plan_hash": hash_canonical(loaded.planning_plan.model_dump(mode="json")),
        "tranche_id": loaded.config.config.tranche_id,
        "tranche_order": loaded.config.config.tranche_order,
        "pool_id": loaded.config.config.pool_id,
        "pool_dialect": loaded.config.config.pool_dialect,
        "shared_execution_record_schema": "lf021_research_execution_records_v1",
        "actual_collection_performed": True,
        "problem_count": loaded.planning_plan.problem_count,
        "family_count": 3,
        "seed_count_by_family": loaded.planning_plan.seed_count_by_family,
        "expected_candidate_count": loaded.planning_plan.expected_candidate_count,
        "terminal_candidate_count": len(terminals),
        "status_counts": dict(sorted(statuses.items())),
        "successful_family_count": len(successful),
        "terminal_artifact_hashes": dict(sorted(terminal_hashes.items())),
        "family_session_artifact_hashes": dict(sorted(family_session_hashes.items())),
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    manifest_id = "lf021_post_exhaustion_collection_manifest_v6:" + hash_canonical(
        {"schema": "lf021_post_exhaustion_collection_manifest_v6", **payload}
    )
    manifest = PostExhaustionCollectionManifestV6.model_validate(
        {"manifest_id": manifest_id, **payload}
    )
    manifest_path = root / "manifest.json"
    _write_immutable(manifest_path, _canonical(manifest))
    return PostExhaustionCollectionRunV6(
        output_directory=root,
        plan_path=plan_path,
        execution_config_path=execution_config_path,
        manifest_path=manifest_path,
        manifest=manifest,
        terminals=tuple(terminals),
    )


def verify_post_exhaustion_collection_v6(
    loaded: LoadedPostExhaustionCollectionV6,
    *,
    repo_root: Path,
) -> PostExhaustionCollectionManifestV6:
    """Replay a completed extension bundle without importing GPU runtimes."""

    root = _run_root(loaded, repo_root)
    _require_repo_path_without_symlinks(
        repo_root=repo_root,
        path=root,
        label="v6 collection root",
    )
    plan_path = root / "plan.json"
    copied_config_path = root / "execution_config.json"
    manifest_path = root / "manifest.json"
    if (
        plan_path.is_symlink()
        or copied_config_path.is_symlink()
        or manifest_path.is_symlink()
        or plan_path.read_bytes() != _canonical(loaded.planning_plan)
        or copied_config_path.read_bytes() != _canonical(loaded.config.config)
    ):
        raise PostExhaustionCollectionV6ArtifactConflict("v6 collection plan/config copy differs")
    try:
        manifest = collection_v1._load_canonical(
            manifest_path,
            PostExhaustionCollectionManifestV6,
        )
    except (OSError, ValueError, collection_v1.ResearchCollectionError) as exc:
        raise PostExhaustionCollectionV6ArtifactConflict(
            "v6 collection manifest is invalid"
        ) from exc
    if (
        manifest.execution_config_id != loaded.config.config.config_id
        or manifest.execution_config_hash != loaded.config.config.config_hash
        or manifest.execution_config != _binding(repo_root, loaded.config.path)
        or manifest.authorization_id != loaded.authorization.authorization_id
        or manifest.extension_decision_id != loaded.authorization.extension_decision_id
        or manifest.planning_config_id != loaded.planning_config.config_id
        or manifest.planning_plan_id != loaded.planning_plan.plan_id
        or manifest.planning_plan_hash
        != hash_canonical(loaded.planning_plan.model_dump(mode="json"))
        or manifest.expected_candidate_count != loaded.planning_plan.expected_candidate_count
    ):
        raise PostExhaustionCollectionV6ArtifactConflict(
            "v6 manifest differs from strict config/authorization replay"
        )
    expected_terminal_paths = {
        _terminal_path(root, invocation).resolve()
        for invocation in loaded.planning_plan.invocations
    }
    discovered_terminal_paths = {
        path.resolve()
        for path in (root / "terminals").glob("*.json")
        if path.is_file() and not path.is_symlink()
    }
    if discovered_terminal_paths != expected_terminal_paths:
        raise PostExhaustionCollectionV6ArtifactConflict(
            "v6 terminal directory contains missing or unexpected artifacts"
        )
    observed_terminals: list[collection_v1.ResearchCollectionTerminal] = []
    for invocation in loaded.planning_plan.invocations:
        terminal = _load_existing_terminal(root=root, invocation=invocation)
        if terminal is None:
            raise PostExhaustionCollectionV6ArtifactConflict(
                "v6 collection terminal denominator is incomplete"
            )
        path = _terminal_path(root, invocation)
        artifact = str(path.relative_to(repo_root))
        if manifest.terminal_artifact_hashes.get(artifact) != hash_file(path):
            raise PostExhaustionCollectionV6ArtifactConflict(
                f"v6 terminal hash differs: {invocation.invocation_id}"
            )
        _verify_family_session(
            repo_root=repo_root,
            output_root=root,
            terminal=terminal,
        )
        _verify_terminal_artifacts(
            repo_root=repo_root,
            root=root,
            invocation=invocation,
            terminal=terminal,
        )
        observed_terminals.append(terminal)
    observed_status = dict(
        sorted(Counter(item.status.value for item in observed_terminals).items())
    )
    if observed_status != manifest.status_counts:
        raise PostExhaustionCollectionV6ArtifactConflict("v6 terminal status accounting differs")
    observed_sessions = {
        str(path.relative_to(repo_root)): hash_file(path)
        for path in sorted((root / "families").glob("**/*.json"))
        if path.is_file() and not path.is_symlink()
    }
    if observed_sessions != manifest.family_session_artifact_hashes:
        raise PostExhaustionCollectionV6ArtifactConflict("v6 family-session artifact map differs")
    return manifest


LocalHFResearchExecutor = collection_v1.LocalHFResearchExecutor


__all__ = [
    "ExecutablePostExhaustionCollectionConfigV6",
    "LoadedPostExhaustionCollectionV6",
    "LocalHFResearchExecutor",
    "PostExhaustionCollectionManifestV6",
    "PostExhaustionCollectionPreflightV6",
    "PostExhaustionCollectionRunV6",
    "PostExhaustionCollectionV6ArtifactConflict",
    "PostExhaustionCollectionV6Error",
    "PostExhaustionCollectionV6ExecutionBlocked",
    "PostExhaustionExecutionPolicyV1",
    "PostExhaustionInvocationExecutorV6",
    "execute_post_exhaustion_collection_v6",
    "load_post_exhaustion_collection_v6",
    "load_post_exhaustion_execution_policy_v1",
    "prepare_post_exhaustion_collection_v6",
    "verify_post_exhaustion_collection_v6",
    "write_post_exhaustion_collection_preflight_v6",
]
