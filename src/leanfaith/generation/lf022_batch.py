"""Deterministic LF-022 public batch freezing and resumable orchestration.

This module deliberately composes the reviewed single-task executor instead of
creating a second provider boundary.  A batch request binds exact per-route
admissions and allocation-task IDs.  Freezing replays every public-pool,
allocation, route, source, representation, context, license, and denylist
binding before writing immutable execution tasks.

Offline preflight/replay is the default.  Live RCP calls require an explicit
flag and runtime-only credentials.  All generated candidates remain
provisional and are never labels, promotions, training data, or evaluation
data.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_execution import (
    LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
    LF022ExecutionArtifacts,
    LF022ExecutionError,
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    LF022QualificationClaim,
    LF022RCPRetryPolicy,
    LF022RCPRouteBinding,
    VerifiedLF022ExecutionAdmission,
    VerifiedLF022ExecutionTaskInputs,
    load_lf022_execution_task_inputs,
    make_lf022_g_open_execution_admission,
    make_lf022_g_open_execution_task,
    make_lf022_qualification_claim,
    verify_lf022_execution_admission,
    verify_lf022_execution_task,
)
from leanfaith.generation.lf022_executor import (
    LF022ExecutionResult,
    RCPRuntimeCredentials,
    execute_lf022_g_open_task,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.generation.llm_variants import PublicLeanVariantSource
from leanfaith.generation.rcp_provider import RCPHTTPTransport, RCPWireResponse
from leanfaith.schemas.enums import IntendedRelation
from leanfaith.schemas.ids import id_pattern, make_id
from leanfaith.schemas.manifest import ManifestError, collect_code_state

_ROUTE_ORDER = {
    "moonshot_kimi_k2": 0,
    "qwen3": 1,
    "glm5": 2,
}
_QUALIFICATION_FAMILIES = frozenset({"qwen3", "glm5"})
_PRIVATE_MARKERS = ("formalmathatepfl/sft_classic", "sft_classic")


class LF022BatchError(RuntimeError):
    """A batch freeze, replay, or execution invariant failed closed."""


class LF022BatchLockedError(LF022BatchError):
    """Another process currently owns the same frozen batch."""


def _content_id(prefix: str, model: StrictModel, *, id_field: str) -> str:
    return make_id(prefix, model.model_dump(mode="json", exclude={id_field}))


def _safe_relative(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    if (
        not value.strip()
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
    ):
        raise ValueError(f"{field} must be a normalized repository-relative path")
    return value


def _repo_path(
    repo_root: Path,
    relative: str,
    *,
    label: str,
    require_file: bool,
) -> Path:
    _safe_relative(relative, field=label)
    root = repo_root.resolve(strict=True)
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink():
            raise LF022BatchError(f"{label} contains a symlinked component")
        if current.exists() and not current.is_dir() and part != PurePosixPath(relative).parts[-1]:
            raise LF022BatchError(f"{label} parent is not a directory")
    try:
        current.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise LF022BatchError(f"{label} escapes the repository") from exc
    if require_file and (not current.is_file() or current.is_symlink()):
        raise LF022BatchError(f"{label} is missing or unsafe")
    return current


def _relative(repo_root: Path, path: Path) -> str:
    try:
        return PurePosixPath(path.resolve().relative_to(repo_root.resolve()).as_posix()).as_posix()
    except ValueError as exc:
        raise LF022BatchError("batch artifact escapes the repository") from exc


def _load_canonical[RecordT: StrictModel](
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    model: type[RecordT],
    label: str,
) -> tuple[RecordT, bytes]:
    path = _repo_path(repo_root, binding.path, label=label, require_file=True)
    raw = path.read_bytes()
    if hash_file(path) != binding.sha256:
        raise LF022BatchError(f"{label} hash differs from its reviewed binding")
    try:
        record = model.model_validate(cast(object, json.loads(raw)))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LF022BatchError(f"invalid {label}: {exc}") from exc
    canonical = canonical_json_bytes(record.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise LF022BatchError(f"{label} is not canonical JSON")
    return record, raw


def _write_immutable(path: Path, payload: bytes) -> str:
    if path.is_symlink():
        raise LF022BatchError(f"immutable output cannot be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022BatchError(f"immutable output already differs: {path}")
        return hash_file(path)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.partial"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if not path.is_file() or path.read_bytes() != payload:
                raise LF022BatchError(f"concurrent immutable output differs: {path}") from None
    finally:
        temporary.unlink(missing_ok=True)
    return hash_file(path)


def _binding(repo_root: Path, path: Path, payload: bytes) -> LF022ArtifactBinding:
    sha256 = _write_immutable(path, payload)
    return LF022ArtifactBinding(path=_relative(repo_root, path), sha256=sha256)


class LF022BatchRouteFreezeRequest(StrictModel):
    """Reviewed exact task selection for one admitted RCP proposer route."""

    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3", "glm5"]
    public_pool_audit_id: str = Field(pattern=id_pattern("lf022_public_pool_audit"))
    allocation_plan_id: str = Field(pattern=id_pattern("lf022_production_plan"))
    execution_artifacts: LF022ExecutionArtifacts
    route: LF022RCPRouteBinding
    retry_policy: LF022RCPRetryPolicy
    code_tree_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    allocation_task_ids: tuple[str, ...] = Field(min_length=1)
    proposal_count: Literal[1] = 1
    requested_relations: tuple[IntendedRelation, ...] = (IntendedRelation.NEAR_MISS,)

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        if self.route.proposer_family_id != self.proposer_family_id:
            raise ValueError("route proposer family differs from route freeze request")
        if tuple(sorted(set(self.allocation_task_ids))) != self.allocation_task_ids:
            raise ValueError("allocation_task_ids must be sorted and unique")
        if len(set(self.requested_relations)) != len(self.requested_relations):
            raise ValueError("requested_relations must be unique")
        pending_qualification = (
            self.proposer_family_id in _QUALIFICATION_FAMILIES
            and self.route.execution_scope == "one_item_proposer_qualification_only"
        )
        if pending_qualification and len(self.allocation_task_ids) != 1:
            raise ValueError("unqualified Qwen/GLM routes require exactly one allocation task")
        if (
            self.proposer_family_id == "moonshot_kimi_k2"
            and self.route.execution_scope != "public_provisional_g_open"
        ):
            raise ValueError("Kimi route must use the reviewed public production scope")
        return self


class LF022BatchFreezeRequest(StrictModel):
    """Content-addressed request for immutable public execution tasks."""

    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=id_pattern("lf022_batch_request"))
    batch_directory: str
    executor_output_root: str
    routes: tuple[LF022BatchRouteFreezeRequest, ...] = Field(min_length=1)
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    optional_natural_language_forbidden: Literal[True] = True
    execute_requires_explicit_flag: Literal[True] = True
    outputs_provisional_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_promotion_enabled: Literal[False] = False
    gold_promotion_enabled: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        _safe_relative(self.batch_directory, field="batch_directory")
        _safe_relative(self.executor_output_root, field="executor_output_root")
        if self.executor_output_root != LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT:
            raise ValueError(
                "executor_output_root must use the canonical global LF-022 executor root"
            )
        families = tuple(route.proposer_family_id for route in self.routes)
        if len(families) != len(set(families)):
            raise ValueError("batch route proposer families must be unique")
        if families != tuple(sorted(families, key=_ROUTE_ORDER.__getitem__)):
            raise ValueError("batch routes must use canonical Kimi/Qwen/GLM order")
        expected = _content_id("lf022_batch_request", self, id_field="request_id")
        if self.request_id != expected:
            raise ValueError("request_id does not match canonical batch request")
        return self


def make_lf022_batch_freeze_request(
    *,
    batch_directory: str,
    executor_output_root: str,
    routes: tuple[LF022BatchRouteFreezeRequest, ...],
) -> LF022BatchFreezeRequest:
    payload: dict[str, object] = {
        "schema_version": 1,
        "batch_directory": batch_directory,
        "executor_output_root": executor_output_root,
        "routes": [route.model_dump(mode="json") for route in routes],
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "optional_natural_language_forbidden": True,
        "execute_requires_explicit_flag": True,
        "outputs_provisional_only": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022BatchFreezeRequest.model_validate(
        {**payload, "request_id": make_id("lf022_batch_request", payload)}
    )


class LF022BatchTaskBinding(StrictModel):
    """One exact executable task copied into a frozen batch."""

    allocation_task_id: str = Field(pattern=id_pattern("lf022_production_task"))
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    task: LF022ArtifactBinding


class LF022BatchRouteManifest(StrictModel):
    """Frozen admission and tasks for one exact proposer route."""

    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3", "glm5"]
    model_id: str
    execution_scope: Literal[
        "public_provisional_g_open",
        "one_item_proposer_qualification_only",
    ]
    qualification_state: Literal[
        "production_route_reviewed",
        "pending_one_item_mechanical_qualification",
        "production_live_qualified",
    ]
    admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    admission: LF022ArtifactBinding
    qualification_claim: LF022ArtifactBinding | None = None
    public_pool_audit_id: str = Field(pattern=id_pattern("lf022_public_pool_audit"))
    allocation_plan_id: str = Field(pattern=id_pattern("lf022_production_plan"))
    tasks: tuple[LF022BatchTaskBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _scope(self) -> Self:
        task_ids = tuple(task.execution_task_id for task in self.tasks)
        if tuple(sorted(set(task_ids))) != task_ids:
            raise ValueError("route execution tasks must be sorted and unique")
        pending_qualification = (
            self.proposer_family_id in _QUALIFICATION_FAMILIES
            and self.execution_scope == "one_item_proposer_qualification_only"
        )
        qualified_production = (
            self.proposer_family_id in _QUALIFICATION_FAMILIES
            and self.execution_scope == "public_provisional_g_open"
        )
        if pending_qualification:
            if (
                self.qualification_state != "pending_one_item_mechanical_qualification"
                or len(self.tasks) != 1
                or self.qualification_claim is None
            ):
                raise ValueError("Qwen/GLM remain restricted to one qualification task")
        elif qualified_production:
            if (
                self.qualification_state != "production_live_qualified"
                or self.qualification_claim is not None
            ):
                raise ValueError("Qwen/GLM production requires replay-verified live qualification")
        elif (
            self.execution_scope != "public_provisional_g_open"
            or self.qualification_state != "production_route_reviewed"
            or self.qualification_claim is not None
        ):
            raise ValueError("Kimi batch route must use its reviewed public provisional scope")
        return self


class LF022PublicBatchManifest(StrictModel):
    """Content-addressed, public-only collection batch."""

    schema_version: Literal[1] = 1
    batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    status: Literal["frozen_offline_ready"] = "frozen_offline_ready"
    freeze_request: LF022ArtifactBinding
    freeze_request_id: str = Field(pattern=id_pattern("lf022_batch_request"))
    batch_directory: str
    executor_output_root: str
    journal_directory: str
    routes: tuple[LF022BatchRouteManifest, ...] = Field(min_length=1)
    total_task_count: int = Field(ge=1, strict=True)
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    optional_natural_language_forbidden: Literal[True] = True
    execute_requires_explicit_flag: Literal[True] = True
    outputs_provisional_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_promotion_enabled: Literal[False] = False
    gold_promotion_enabled: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        _safe_relative(self.batch_directory, field="batch_directory")
        _safe_relative(self.executor_output_root, field="executor_output_root")
        _safe_relative(self.journal_directory, field="journal_directory")
        if self.executor_output_root != LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT:
            raise ValueError(
                "executor_output_root must use the canonical global LF-022 executor root"
            )
        families = tuple(route.proposer_family_id for route in self.routes)
        if families != tuple(sorted(set(families), key=_ROUTE_ORDER.__getitem__)):
            raise ValueError("manifest routes must be canonically sorted and unique")
        if self.total_task_count != sum(len(route.tasks) for route in self.routes):
            raise ValueError("total_task_count does not match route tasks")
        expected = _content_id("lf022_public_batch", self, id_field="batch_id")
        if self.batch_id != expected:
            raise ValueError("batch_id does not match canonical batch manifest")
        return self


@dataclass(frozen=True, slots=True)
class FrozenLF022PublicBatch:
    manifest: LF022PublicBatchManifest
    manifest_path: Path


def _unique_index[RecordT: StrictModel](
    records: tuple[RecordT, ...],
    *,
    attribute: str,
    label: str,
) -> dict[str, RecordT]:
    result: dict[str, RecordT] = {}
    for record in records:
        key = cast(str, getattr(record, attribute))
        if key in result:
            raise LF022BatchError(f"duplicate {label} key {key}")
        result[key] = record
    return result


def _source_for_allocation(
    *,
    admission: LF022GOpenExecutionAdmission,
    inputs: VerifiedLF022ExecutionTaskInputs,
    allocation_admission_record_id: str,
) -> PublicLeanVariantSource:
    source_records = _unique_index(
        inputs.source_records,
        attribute="admission_record_id",
        label="source admission",
    )
    theorems = _unique_index(inputs.theorems, attribute="theorem_id", label="theorem")
    contexts = _unique_index(inputs.contexts, attribute="context_id", label="context")
    source_record = source_records.get(allocation_admission_record_id)
    if source_record is None:
        raise LF022BatchError("allocation source is absent from exact public source artifacts")
    theorem = theorems.get(source_record.theorem_id)
    context = contexts.get(source_record.context_id)
    if theorem is None or context is None:
        raise LF022BatchError("allocation theorem or context is absent")
    authorizations = tuple(
        item
        for item in inputs.authorization_registry.authorizations
        if item.authorization_id == source_record.public_source_authorization_id
    )
    if len(authorizations) != 1:
        raise LF022BatchError("allocation source lacks one exact public authorization")
    authorization = authorizations[0]
    serialized = canonical_json_bytes(
        {
            "source": theorem.source,
            "source_revision": theorem.source_revision,
            "statement": theorem.proof_stripped_declaration,
            "imports": context.imports,
        }
    ).decode("utf-8")
    if any(marker in serialized.casefold() for marker in _PRIVATE_MARKERS):
        raise LF022BatchError("private source content is forbidden from an LF-022 batch")
    if (
        theorem.source != source_record.source
        or theorem.source_revision != source_record.source_revision
        or authorization.source != theorem.source
        or authorization.source_revision != theorem.source_revision
        or not authorization.source_is_public
        or not authorization.external_transmission_allowed
    ):
        raise LF022BatchError("source content differs from its public authorization")
    return PublicLeanVariantSource(
        source_theorem_id=theorem.theorem_id,
        source_representation_id=source_record.representation_id,
        context_id=context.context_id,
        imports=context.imports,
        source_statement=theorem.proof_stripped_declaration,
        optional_natural_language=None,
        source_id=theorem.source,
        source_revision=theorem.source_revision,
        source_license=authorization.license_id,
        source_is_public=True,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )


def freeze_lf022_public_batch(
    *,
    repo_root: Path,
    request_binding: LF022ArtifactBinding,
) -> FrozenLF022PublicBatch:
    """Replay exact admissions/sources and freeze immutable task JSON files."""

    request, request_raw = _load_canonical(
        repo_root=repo_root,
        binding=request_binding,
        model=LF022BatchFreezeRequest,
        label="LF-022 batch freeze request",
    )
    output = _repo_path(
        repo_root,
        request.batch_directory,
        label="batch_directory",
        require_file=False,
    )
    if output.exists() and not output.is_dir():
        raise LF022BatchError("batch_directory is not a directory")
    output.mkdir(parents=True, exist_ok=True)
    request_copy = _binding(repo_root, output / "freeze_request.json", request_raw)

    route_manifests: list[LF022BatchRouteManifest] = []
    for route_request in request.routes:
        admission = make_lf022_g_open_execution_admission(
            public_pool_audit_id=route_request.public_pool_audit_id,
            allocation_plan_id=route_request.allocation_plan_id,
            artifacts=route_request.execution_artifacts,
            route=route_request.route,
            retry_policy=route_request.retry_policy,
            code_tree_hash=route_request.code_tree_hash,
        )
        admission_raw = canonical_json_bytes(admission.model_dump(mode="json"))
        if admission.route.proposer_family_id != route_request.proposer_family_id:
            raise LF022BatchError("batch route family differs from its reviewed admission")
        pending_qualification = (
            route_request.proposer_family_id in _QUALIFICATION_FAMILIES
            and admission.route.execution_scope == "one_item_proposer_qualification_only"
        )
        if pending_qualification:
            if admission.route.execution_scope != "one_item_proposer_qualification_only":
                raise LF022BatchError(
                    "Qwen/GLM cannot enter a production batch before qualification"
                )
        elif admission.route.execution_scope != "public_provisional_g_open":
            raise LF022BatchError("Kimi route lacks reviewed public provisional scope")

        try:
            verified = verify_lf022_execution_admission(
                repo_root=repo_root,
                admission=admission,
            )
            task_inputs = load_lf022_execution_task_inputs(
                repo_root=repo_root,
                verified=verified,
            )
        except LF022ExecutionError as exc:
            raise LF022BatchError(
                f"{route_request.proposer_family_id} reviewed admission rejected: {exc}"
            ) from exc
        allocation_index = {
            task.task_id: task for task in verified.plan.tasks if task.distribution == "G_open"
        }
        selected = []
        for allocation_task_id in route_request.allocation_task_ids:
            allocation = allocation_index.get(allocation_task_id)
            if allocation is None:
                raise LF022BatchError(
                    f"allocation task is absent from G_open plan: {allocation_task_id}"
                )
            if allocation.proposer_family_id != route_request.proposer_family_id:
                raise LF022BatchError("allocation task belongs to a different proposer family")
            source = _source_for_allocation(
                admission=admission,
                inputs=task_inputs,
                allocation_admission_record_id=allocation.admission_record_id,
            )
            task = make_lf022_g_open_execution_task(
                admission=admission,
                allocation_task=allocation,
                source=source,
                proposal_count=route_request.proposal_count,
                requested_relations=route_request.requested_relations,
            )
            try:
                verify_lf022_execution_task(
                    repo_root=repo_root,
                    admission=admission,
                    verified=verified,
                    task=task,
                    inputs=task_inputs,
                )
            except LF022ExecutionError as exc:
                raise LF022BatchError(
                    f"frozen allocation task rejected: {allocation_task_id}"
                ) from exc
            selected.append(task)
        selected.sort(key=lambda task: task.execution_task_id)

        qualification_claim_binding: LF022ArtifactBinding | None = None
        if pending_qualification:
            claim = make_lf022_qualification_claim(
                admission=admission,
                task=selected[0],
            )
            claim_path = (
                repo_root
                / LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT
                / "qualification_claims"
                / f"{route_request.proposer_family_id}.json"
            )
            qualification_claim_binding = _binding(
                repo_root,
                claim_path,
                canonical_json_bytes(claim.model_dump(mode="json")),
            )

        admission_binding = _binding(
            repo_root,
            output / "admissions" / f"{route_request.proposer_family_id}.json",
            admission_raw,
        )
        task_bindings = []
        for task in selected:
            task_path = (
                output
                / "tasks"
                / route_request.proposer_family_id
                / f"{task.execution_task_id.split(':', 1)[1]}.json"
            )
            task_binding = _binding(
                repo_root,
                task_path,
                canonical_json_bytes(task.model_dump(mode="json")),
            )
            task_bindings.append(
                LF022BatchTaskBinding(
                    allocation_task_id=task.allocation_task.task_id,
                    execution_task_id=task.execution_task_id,
                    task=task_binding,
                )
            )
        route_manifests.append(
            LF022BatchRouteManifest(
                proposer_family_id=route_request.proposer_family_id,
                model_id=admission.route.model_id,
                execution_scope=admission.route.execution_scope,
                qualification_state=(
                    "pending_one_item_mechanical_qualification"
                    if pending_qualification
                    else "production_live_qualified"
                    if route_request.proposer_family_id in _QUALIFICATION_FAMILIES
                    else "production_route_reviewed"
                ),
                admission_id=admission.admission_id,
                admission=admission_binding,
                qualification_claim=qualification_claim_binding,
                public_pool_audit_id=admission.public_pool_audit_id,
                allocation_plan_id=admission.allocation_plan_id,
                tasks=tuple(task_bindings),
            )
        )

    journal_directory = f"{request.batch_directory}/journal"
    manifest_payload: dict[str, object] = {
        "schema_version": 1,
        "status": "frozen_offline_ready",
        "freeze_request": request_copy.model_dump(mode="json"),
        "freeze_request_id": request.request_id,
        "batch_directory": request.batch_directory,
        "executor_output_root": request.executor_output_root,
        "journal_directory": journal_directory,
        "routes": [route.model_dump(mode="json") for route in route_manifests],
        "total_task_count": sum(len(route.tasks) for route in route_manifests),
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "optional_natural_language_forbidden": True,
        "execute_requires_explicit_flag": True,
        "outputs_provisional_only": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    manifest = LF022PublicBatchManifest.model_validate(
        {
            **manifest_payload,
            "batch_id": make_id("lf022_public_batch", manifest_payload),
        }
    )
    manifest_path = output / "batch_manifest.json"
    _write_immutable(
        manifest_path,
        canonical_json_bytes(manifest.model_dump(mode="json")),
    )
    return FrozenLF022PublicBatch(manifest=manifest, manifest_path=manifest_path)


class LF022BatchRunPolicy(StrictModel):
    """Bounded runtime controls; contains no provider credentials."""

    max_concurrency: int = Field(default=1, ge=1, le=8, strict=True)
    minimum_request_interval_seconds: float = Field(default=0.0, ge=0.0, le=300.0)


class LF022BatchJournalEvent(StrictModel):
    """Append-only task status derived from the single-task executor."""

    schema_version: Literal[1] = 1
    event_id: str = Field(pattern=id_pattern("lf022_batch_event"))
    batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3", "glm5"]
    phase: Literal["preflight", "terminal", "error"]
    status: str = Field(min_length=1)
    preflight_id: str | None = Field(default=None, pattern=id_pattern("lf022_execution_preflight"))
    terminal_id: str | None = Field(default=None, pattern=id_pattern("lf022_execution_terminal"))
    terminal_artifact: LF022ArtifactBinding | None = None
    error_code: Literal["executor_rejected"] | None = None
    output_quality_tier: Literal["provisional"] = "provisional"
    semantic_labels_created: Literal[False] = False
    silver_promotion_enabled: Literal[False] = False
    gold_promotion_enabled: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.phase == "preflight":
            if self.preflight_id is None or self.terminal_id is not None or self.error_code:
                raise ValueError("preflight event fields are inconsistent")
        elif self.phase == "terminal":
            if (
                self.preflight_id is None
                or self.terminal_id is None
                or self.terminal_artifact is None
                or self.error_code is not None
            ):
                raise ValueError("terminal event fields are incomplete")
        elif (
            self.preflight_id is not None
            or self.terminal_id is not None
            or self.terminal_artifact is not None
            or self.error_code != "executor_rejected"
        ):
            raise ValueError("error event fields are inconsistent")
        expected = _content_id("lf022_batch_event", self, id_field="event_id")
        if self.event_id != expected:
            raise ValueError("event_id does not match canonical journal event")
        return self


class LF022BatchRunReport(StrictModel):
    """Immutable summary of one offline or explicitly live batch pass."""

    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=id_pattern("lf022_batch_run"))
    batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    mode: Literal["offline", "live"]
    task_count: int = Field(ge=1, strict=True)
    preflight_only_count: int = Field(ge=0, strict=True)
    replayed_terminal_count: int = Field(ge=0, strict=True)
    new_terminal_count: int = Field(ge=0, strict=True)
    error_count: int = Field(ge=0, strict=True)
    network_calls_this_run: int = Field(ge=0, strict=True)
    terminal_status_counts: dict[str, int]
    failed_task_ids: tuple[str, ...]
    max_concurrency: int = Field(ge=1, le=8, strict=True)
    minimum_request_interval_seconds: float = Field(ge=0.0, le=300.0)
    semantic_labels_created: Literal[False] = False
    silver_promotion_enabled: Literal[False] = False
    gold_promotion_enabled: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if (
            self.preflight_only_count
            + self.replayed_terminal_count
            + self.new_terminal_count
            + self.error_count
            != self.task_count
        ):
            raise ValueError("batch run outcome counts do not reconcile")
        if tuple(sorted(set(self.failed_task_ids))) != self.failed_task_ids:
            raise ValueError("failed_task_ids must be sorted and unique")
        if sum(self.terminal_status_counts.values()) != (
            self.replayed_terminal_count + self.new_terminal_count
        ):
            raise ValueError("terminal status counts do not reconcile")
        if list(self.terminal_status_counts) != sorted(self.terminal_status_counts):
            raise ValueError("terminal_status_counts must be sorted")
        if self.mode == "offline" and self.network_calls_this_run != 0:
            raise ValueError("offline batch run cannot perform network calls")
        expected = _content_id("lf022_batch_run", self, id_field="report_id")
        if self.report_id != expected:
            raise ValueError("report_id does not match canonical run report")
        return self


@dataclass(frozen=True, slots=True)
class LF022BatchRunResult:
    report: LF022BatchRunReport
    report_path: Path


class RateLimitedRCPTransport:
    """Thread-safe request-start limiter over the reviewed RCP transport."""

    def __init__(
        self,
        *,
        underlying: RCPHTTPTransport,
        minimum_interval_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if minimum_interval_seconds < 0 or minimum_interval_seconds > 300:
            raise ValueError("minimum_interval_seconds must be in [0, 300]")
        self._underlying = underlying
        self._interval = minimum_interval_seconds
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._next_start = 0.0

    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> RCPWireResponse:
        with self._lock:
            now = self._monotonic()
            delay = max(0.0, self._next_start - now)
            if delay:
                self._sleeper(delay)
                now = self._monotonic()
            self._next_start = max(now, self._next_start) + self._interval
        return self._underlying.post_json(
            url=url,
            api_key=api_key,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class _LoadedBatchTask:
    family: str
    admission: LF022GOpenExecutionAdmission
    task: LF022GOpenExecutionTask
    verified: VerifiedLF022ExecutionAdmission
    task_inputs: VerifiedLF022ExecutionTaskInputs


def _journal_event(
    *,
    repo_root: Path,
    manifest: LF022PublicBatchManifest,
    loaded: _LoadedBatchTask,
    phase: Literal["preflight", "terminal", "error"],
    result: LF022ExecutionResult | None,
) -> LF022BatchJournalEvent:
    terminal_binding = (
        LF022ArtifactBinding(
            path=_relative(repo_root, result.terminal_path),
            sha256=hash_file(result.terminal_path),
        )
        if phase == "terminal" and result is not None and result.terminal_path is not None
        else None
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "batch_id": manifest.batch_id,
        "execution_task_id": loaded.task.execution_task_id,
        "proposer_family_id": loaded.family,
        "phase": phase,
        "status": (
            "preflight_ready"
            if phase == "preflight"
            else result.terminal.status
            if phase == "terminal" and result is not None and result.terminal is not None
            else "executor_rejected"
        ),
        "preflight_id": result.preflight.preflight_id if result is not None else None,
        "terminal_id": (
            result.terminal.terminal_id
            if phase == "terminal" and result is not None and result.terminal is not None
            else None
        ),
        "terminal_artifact": (
            terminal_binding.model_dump(mode="json") if terminal_binding is not None else None
        ),
        "error_code": "executor_rejected" if phase == "error" else None,
        "output_quality_tier": "provisional",
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022BatchJournalEvent.model_validate(
        {**payload, "event_id": make_id("lf022_batch_event", payload)}
    )


def _persist_event(
    *,
    repo_root: Path,
    manifest: LF022PublicBatchManifest,
    event: LF022BatchJournalEvent,
) -> None:
    event_path = (
        repo_root
        / manifest.journal_directory
        / event.execution_task_id.split(":", 1)[1]
        / f"{event.phase}-{event.event_id.split(':', 1)[1]}.json"
    )
    _write_immutable(event_path, canonical_json_bytes(event.model_dump(mode="json")))


def _load_batch(
    *,
    repo_root: Path,
    manifest_binding: LF022ArtifactBinding,
) -> tuple[LF022PublicBatchManifest, tuple[_LoadedBatchTask, ...]]:
    manifest, _ = _load_canonical(
        repo_root=repo_root,
        binding=manifest_binding,
        model=LF022PublicBatchManifest,
        label="LF-022 public batch manifest",
    )
    request, _ = _load_canonical(
        repo_root=repo_root,
        binding=manifest.freeze_request,
        model=LF022BatchFreezeRequest,
        label="frozen LF-022 batch request",
    )
    if (
        request.request_id != manifest.freeze_request_id
        or request.batch_directory != manifest.batch_directory
        or request.executor_output_root != manifest.executor_output_root
        or len(request.routes) != len(manifest.routes)
    ):
        raise LF022BatchError("batch manifest differs from its frozen request")
    loaded: list[_LoadedBatchTask] = []
    for route, route_request in zip(manifest.routes, request.routes, strict=True):
        if route.proposer_family_id != route_request.proposer_family_id:
            raise LF022BatchError("batch route order differs from its frozen request")
        admission, _ = _load_canonical(
            repo_root=repo_root,
            binding=route.admission,
            model=LF022GOpenExecutionAdmission,
            label=f"{route.proposer_family_id} frozen admission",
        )
        expected_admission = make_lf022_g_open_execution_admission(
            public_pool_audit_id=route_request.public_pool_audit_id,
            allocation_plan_id=route_request.allocation_plan_id,
            artifacts=route_request.execution_artifacts,
            route=route_request.route,
            retry_policy=route_request.retry_policy,
            code_tree_hash=route_request.code_tree_hash,
        )
        if (
            admission != expected_admission
            or admission.admission_id != route.admission_id
            or admission.route.proposer_family_id != route.proposer_family_id
            or admission.route.model_id != route.model_id
            or admission.route.execution_scope != route.execution_scope
            or admission.public_pool_audit_id != route.public_pool_audit_id
            or admission.allocation_plan_id != route.allocation_plan_id
        ):
            raise LF022BatchError("batch route differs from its frozen admission")
        try:
            verified = verify_lf022_execution_admission(
                repo_root=repo_root,
                admission=admission,
            )
            task_inputs = load_lf022_execution_task_inputs(
                repo_root=repo_root,
                verified=verified,
            )
        except LF022ExecutionError as exc:
            raise LF022BatchError(
                f"{route.proposer_family_id} frozen admission replay rejected: {exc}"
            ) from exc
        route_tasks: list[LF022GOpenExecutionTask] = []
        for task_binding in route.tasks:
            task, _ = _load_canonical(
                repo_root=repo_root,
                binding=task_binding.task,
                model=LF022GOpenExecutionTask,
                label="frozen batch task",
            )
            if (
                task.execution_task_id != task_binding.execution_task_id
                or task.allocation_task.task_id != task_binding.allocation_task_id
                or task.proposal_count != route_request.proposal_count
                or task.requested_relations != route_request.requested_relations
            ):
                raise LF022BatchError("batch task binding differs from task content")
            try:
                verify_lf022_execution_task(
                    repo_root=repo_root,
                    admission=admission,
                    verified=verified,
                    task=task,
                    inputs=task_inputs,
                )
            except LF022ExecutionError as exc:
                raise LF022BatchError(
                    f"frozen task replay rejected: {task.execution_task_id}"
                ) from exc
            loaded.append(
                _LoadedBatchTask(
                    family=route.proposer_family_id,
                    admission=admission,
                    task=task,
                    verified=verified,
                    task_inputs=task_inputs,
                )
            )
            route_tasks.append(task)
        if (
            tuple(sorted(task.allocation_task.task_id for task in route_tasks))
            != route_request.allocation_task_ids
        ):
            raise LF022BatchError("batch tasks differ from the frozen request selection")
        pending_qualification = (
            route.proposer_family_id in _QUALIFICATION_FAMILIES
            and route.execution_scope == "one_item_proposer_qualification_only"
        )
        if pending_qualification:
            claim_binding = route.qualification_claim
            if claim_binding is None:
                raise LF022BatchError("qualification route lacks its global claim")
            expected_claim_path = (
                f"{LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT}/qualification_claims/"
                f"{route.proposer_family_id}.json"
            )
            if claim_binding.path != expected_claim_path:
                raise LF022BatchError("qualification claim is outside the canonical registry")
            claim, _ = _load_canonical(
                repo_root=repo_root,
                binding=claim_binding,
                model=LF022QualificationClaim,
                label=f"{route.proposer_family_id} qualification claim",
            )
            expected_claim = make_lf022_qualification_claim(
                admission=admission,
                task=route_tasks[0],
            )
            if claim != expected_claim:
                raise LF022BatchError("qualification claim differs from the frozen route")
    loaded.sort(key=lambda item: item.task.execution_task_id)
    if len({item.task.execution_task_id for item in loaded}) != len(loaded):
        raise LF022BatchError("batch contains duplicate execution task IDs")
    if len(loaded) != manifest.total_task_count:
        raise LF022BatchError("loaded task count differs from batch manifest")
    return manifest, tuple(loaded)


def run_lf022_public_batch(
    *,
    repo_root: Path,
    manifest_binding: LF022ArtifactBinding,
    policy: LF022BatchRunPolicy,
    execute_public_provisional: bool = False,
    credentials: RCPRuntimeCredentials | None = None,
    transport: RCPHTTPTransport | None = None,
) -> LF022BatchRunResult:
    """Preflight/replay a frozen batch, or explicitly execute provisional tasks."""

    manifest, tasks = _load_batch(
        repo_root=repo_root,
        manifest_binding=manifest_binding,
    )
    if execute_public_provisional and (credentials is None or transport is None):
        raise LF022BatchError("explicit live batch execution requires runtime credentials")
    if not execute_public_provisional and (credentials is not None or transport is not None):
        raise LF022BatchError("offline batch execution rejects credentials and transports")
    try:
        observed_code_tree_hash = collect_code_state(repo_root).code_tree_hash
    except ManifestError as exc:
        raise LF022BatchError(f"cannot verify current code tree: {exc}") from exc
    if observed_code_tree_hash is None:
        raise LF022BatchError("current code tree hash is unavailable")

    batch_dir = _repo_path(
        repo_root,
        manifest.batch_directory,
        label="batch_directory",
        require_file=False,
    )
    lock_path = batch_dir / ".batch.lock"
    if lock_path.is_symlink():
        raise LF022BatchError("batch lock cannot be a symlink")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise LF022BatchLockedError("batch is already running") from exc
    except BaseException:
        os.close(descriptor)
        raise

    rate_limited = (
        RateLimitedRCPTransport(
            underlying=cast(RCPHTTPTransport, transport),
            minimum_interval_seconds=policy.minimum_request_interval_seconds,
        )
        if execute_public_provisional
        else None
    )

    def run_one(loaded: _LoadedBatchTask) -> tuple[_LoadedBatchTask, LF022ExecutionResult | None]:
        try:
            result = execute_lf022_g_open_task(
                repo_root=repo_root,
                output_root=repo_root / manifest.executor_output_root,
                admission=loaded.admission,
                task=loaded.task,
                execute_public_provisional=execute_public_provisional,
                credentials=credentials,
                transport=rate_limited,
                verified_admission=loaded.verified,
                verified_task_inputs=loaded.task_inputs,
                observed_code_tree_hash=observed_code_tree_hash,
            )
        except Exception:
            event = _journal_event(
                repo_root=repo_root,
                manifest=manifest,
                loaded=loaded,
                phase="error",
                result=None,
            )
            _persist_event(repo_root=repo_root, manifest=manifest, event=event)
            return loaded, None
        preflight_event = _journal_event(
            repo_root=repo_root,
            manifest=manifest,
            loaded=loaded,
            phase="preflight",
            result=result,
        )
        _persist_event(repo_root=repo_root, manifest=manifest, event=preflight_event)
        if result.terminal is not None:
            terminal_event = _journal_event(
                repo_root=repo_root,
                manifest=manifest,
                loaded=loaded,
                phase="terminal",
                result=result,
            )
            _persist_event(repo_root=repo_root, manifest=manifest, event=terminal_event)
        return loaded, result

    results: list[tuple[_LoadedBatchTask, LF022ExecutionResult | None]] = []
    try:
        with ThreadPoolExecutor(
            max_workers=policy.max_concurrency,
            thread_name_prefix="lf022-public",
        ) as executor:
            futures = [executor.submit(run_one, loaded) for loaded in tasks]
            for future in as_completed(futures):
                results.append(future.result())
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    results.sort(key=lambda pair: pair[0].task.execution_task_id)

    preflight_only = 0
    replayed = 0
    new_terminal = 0
    network_calls = 0
    failures: list[str] = []
    terminal_counts: Counter[str] = Counter()
    for loaded, result in results:
        if result is None:
            failures.append(loaded.task.execution_task_id)
            continue
        network_calls += result.network_calls_this_run
        if result.terminal is None:
            preflight_only += 1
        else:
            terminal_counts[result.terminal.status] += 1
            if result.replayed:
                replayed += 1
            else:
                new_terminal += 1
    report_payload: dict[str, object] = {
        "schema_version": 1,
        "batch_id": manifest.batch_id,
        "mode": "live" if execute_public_provisional else "offline",
        "task_count": len(tasks),
        "preflight_only_count": preflight_only,
        "replayed_terminal_count": replayed,
        "new_terminal_count": new_terminal,
        "error_count": len(failures),
        "network_calls_this_run": network_calls,
        "terminal_status_counts": dict(sorted(terminal_counts.items())),
        "failed_task_ids": sorted(failures),
        "max_concurrency": policy.max_concurrency,
        "minimum_request_interval_seconds": policy.minimum_request_interval_seconds,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    report = LF022BatchRunReport.model_validate(
        {**report_payload, "report_id": make_id("lf022_batch_run", report_payload)}
    )
    report_path = (
        repo_root / manifest.batch_directory / "runs" / f"{report.report_id.split(':', 1)[1]}.json"
    )
    _write_immutable(report_path, canonical_json_bytes(report.model_dump(mode="json")))
    return LF022BatchRunResult(report=report, report_path=report_path)


__all__ = [
    "FrozenLF022PublicBatch",
    "LF022BatchError",
    "LF022BatchFreezeRequest",
    "LF022BatchJournalEvent",
    "LF022BatchLockedError",
    "LF022BatchRouteFreezeRequest",
    "LF022BatchRunPolicy",
    "LF022BatchRunReport",
    "LF022BatchRunResult",
    "LF022PublicBatchManifest",
    "RateLimitedRCPTransport",
    "freeze_lf022_public_batch",
    "make_lf022_batch_freeze_request",
    "run_lf022_public_batch",
]
