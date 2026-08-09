"""Exact LF-022 proposer qualification to production-route promotion.

Qwen and GLM begin in a repository-global, one-item qualification scope.  A
successful provider response is still only an unvalidated provisional
candidate.  This module can bind that exact response lineage into a
content-addressed *route eligibility* record; it never promotes the generated
candidate or creates a semantic label.

Both certification and later production admission replay the qualification
offline through the normal executor.  Consequently a hand-written success
flag, a different family/model, a different task, or modified response bytes
cannot authorize a production route.
"""

from __future__ import annotations

import datetime
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_execution import (
    LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
    LF022ExecutionError,
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    LF022QualificationClaim,
    LF022QualificationSupersession,
    lf022_qualification_claim_path,
    load_lf022_execution_task_inputs,
    make_lf022_qualification_claim,
    verify_lf022_execution_admission,
    verify_lf022_execution_task,
)
from leanfaith.generation.lf022_executor import (
    LF022ExecutionTerminalRecord,
    LF022ExecutorError,
    execute_lf022_g_open_task,
)
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022ProductionFamilyMatrix,
)
from leanfaith.schemas.enums import LLMRole, QualityTier, ValidationStatus
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.manifest import require_utc
from leanfaith.schemas.variant import VariantRecord

_QUALIFICATION_FAMILIES = frozenset({"qwen3", "glm5"})
_MODEL_BY_FAMILY = {
    "qwen3": "Qwen/Qwen3.5-397B-A17B",
    "glm5": "zai-org/GLM-5.2",
}


class LF022RouteQualificationError(RuntimeError):
    """Qualification evidence or production-route eligibility failed closed."""


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


def _bound_path(
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    label: str,
) -> Path:
    root = repo_root.resolve(strict=True)
    relative = PurePosixPath(_safe_relative(binding.path, field=label))
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise LF022RouteQualificationError(f"{label} contains a symlinked component")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise LF022RouteQualificationError(f"{label} is missing") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise LF022RouteQualificationError(f"{label} must be a repository-local file")
    observed = hash_file(resolved)
    if observed != binding.sha256:
        raise LF022RouteQualificationError(
            f"{label} SHA-256 differs: {observed} != {binding.sha256}"
        )
    return resolved


def _relative(repo_root: Path, path: Path) -> str:
    try:
        return PurePosixPath(path.resolve().relative_to(repo_root.resolve()).as_posix()).as_posix()
    except ValueError as exc:
        raise LF022RouteQualificationError("qualification artifact escapes repository") from exc


def _binding(repo_root: Path, path: Path) -> LF022ArtifactBinding:
    if path.is_symlink() or not path.is_file():
        raise LF022RouteQualificationError(f"qualification artifact is missing or unsafe: {path}")
    return LF022ArtifactBinding(path=_relative(repo_root, path), sha256=hash_file(path))


def _load_model[RecordT: StrictModel](
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    model: type[RecordT],
    label: str,
) -> RecordT:
    path = _bound_path(repo_root=repo_root, binding=binding, label=label)
    raw = path.read_bytes()
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022RouteQualificationError(f"invalid {label}: {exc}") from exc
    canonical = canonical_json_bytes(record.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise LF022RouteQualificationError(f"{label} is not canonical JSON")
    return record


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise LF022RouteQualificationError("eligibility output cannot be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022RouteQualificationError(f"existing production eligibility differs: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022RouteQualificationError(
                    f"concurrent production eligibility differs: {path}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


class LF022QualifiedProposerProductionEligibility(StrictModel):
    """One exact, replay-verified qualification authorizing only a route."""

    schema_version: Literal[1] = 1
    eligibility_id: str = Field(pattern=id_pattern("lf022_route_eligibility"))
    status: Literal["live_qualification_replay_verified"]
    proposer_family_id: Literal["qwen3", "glm5"]
    model_id: str
    deployment_id: str
    canonical_family: str
    provider_id: Literal["epfl_rcp"]
    catalog_snapshot_id: str = Field(pattern=id_pattern("lf022_provider_catalog"))
    route_snapshot_revision: str = Field(pattern=r"^rcp-catalog-sha256:[0-9a-f]{64}$")
    decoding_contract_id: Literal[
        "qwen3_5_proposer_qualification_v1",
        "qwen3_5_proposer_qualification_v2",
        "glm5_2_proposer_qualification_v1",
        "glm5_2_proposer_qualification_v2",
    ]
    decoding_contract_hash: str = Field(pattern=HEX64_PATTERN)
    family_matrix_id: str = Field(pattern=id_pattern("lf022_family_matrix"))
    family_matrix: LF022ArtifactBinding
    qualification_contract: LF022ArtifactBinding
    qualification_claim_id: str = Field(pattern=id_pattern("lf022_qualification_claim"))
    qualification_claim: LF022ArtifactBinding
    qualification_admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    qualification_admission: LF022ArtifactBinding
    qualification_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    qualification_task: LF022ArtifactBinding
    qualification_terminal_id: str = Field(pattern=id_pattern("lf022_execution_terminal"))
    qualification_terminal: LF022ArtifactBinding
    qualification_variants: LF022ArtifactBinding
    qualification_llm_call_id: str = Field(pattern=id_pattern("call"))
    qualification_llm_call: LF022ArtifactBinding
    qualification_provider_request_hash: str = Field(pattern=HEX64_PATTERN)
    qualification_completed_at: datetime.datetime
    qualification_task_count: Literal[1] = 1
    qualification_variant_count: Literal[1] = 1
    qualification_execution_mode: Literal["external"] = "external"
    exact_replay_verified: Literal[True] = True
    production_execution_scope: Literal["public_provisional_g_open"]
    judge_family_ids: tuple[str, str]
    permitted_validator_family_ids: tuple[str, ...] = Field(min_length=1)
    proposer_validator_same_family_forbidden: Literal[True] = True
    heldout_eval_family_id: str
    heldout_eval_supervision_excluded: Literal[True] = True
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    output_quality_tier: Literal["provisional"] = "provisional"
    outputs_unresolved: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_promotion_enabled: Literal[False] = False
    gold_promotion_enabled: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent_and_content_addressed(self) -> Self:
        require_utc(self.qualification_completed_at)
        if self.model_id != _MODEL_BY_FAMILY[self.proposer_family_id]:
            raise ValueError("eligibility model differs from exact proposer family")
        expected_contracts = {
            "qwen3": {
                "qwen3_5_proposer_qualification_v1",
                "qwen3_5_proposer_qualification_v2",
            },
            "glm5": {
                "glm5_2_proposer_qualification_v1",
                "glm5_2_proposer_qualification_v2",
            },
        }[self.proposer_family_id]
        if self.decoding_contract_id not in expected_contracts:
            raise ValueError("eligibility decoding contract differs from proposer family")
        if len(set(self.judge_family_ids)) != 2:
            raise ValueError("qualification task must bind two distinct judge families")
        if self.proposer_family_id in self.judge_family_ids:
            raise ValueError("proposer family cannot judge its own qualification task")
        validators = self.permitted_validator_family_ids
        if tuple(sorted(set(validators))) != validators:
            raise ValueError("permitted validator families must be sorted and unique")
        if self.proposer_family_id in validators:
            raise ValueError("proposer family cannot validate its own generated candidates")
        if self.heldout_eval_family_id in {
            *self.judge_family_ids,
            *validators,
            self.proposer_family_id,
        }:
            raise ValueError("held-out evaluation family cannot supervise production data")
        expected = make_id(
            "lf022_route_eligibility",
            self.model_dump(mode="json", exclude={"eligibility_id"}),
        )
        if self.eligibility_id != expected:
            raise ValueError("eligibility_id does not match canonical eligibility content")
        return self


@dataclass(frozen=True, slots=True)
class CertifiedLF022ProposerRoute:
    """Persisted production-route eligibility and its canonical path."""

    eligibility: LF022QualifiedProposerProductionEligibility
    eligibility_path: Path


@dataclass(frozen=True, slots=True)
class SupersededLF022Qualification:
    """One persisted authorization for a fresh qualification attempt."""

    supersession: LF022QualificationSupersession
    supersession_path: Path


def _task_directory(repo_root: Path, task_id: str) -> Path:
    digest = task_id.removeprefix("lf022_execution_task:")
    return repo_root / LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT / "tasks" / digest[:2] / digest


def _qualification_supersession_path(
    repo_root: Path,
    supersession: LF022QualificationSupersession,
) -> Path:
    digest = supersession.supersession_id.split(":", 1)[1]
    return (
        repo_root
        / LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT
        / "qualification_supersessions"
        / supersession.proposer_family_id
        / f"{digest}.json"
    )


def _failed_qualification_replay(
    *,
    repo_root: Path,
    admission: LF022GOpenExecutionAdmission,
    task: LF022GOpenExecutionTask,
) -> tuple[LF022ExecutionTerminalRecord, Path]:
    """Replay one immutable failed qualification without contacting a provider."""

    if (
        admission.route.proposer_family_id not in _QUALIFICATION_FAMILIES
        or admission.route.execution_scope != "one_item_proposer_qualification_only"
        or admission.artifacts.qualification_supersession is not None
    ):
        raise LF022RouteQualificationError(
            "only a prior unsuperseded Qwen/GLM qualification may be superseded"
        )
    try:
        verified = verify_lf022_execution_admission(
            repo_root=repo_root,
            admission=admission,
        )
        inputs = load_lf022_execution_task_inputs(repo_root=repo_root, verified=verified)
        verify_lf022_execution_task(
            repo_root=repo_root,
            admission=admission,
            verified=verified,
            task=task,
            inputs=inputs,
        )
        replay = execute_lf022_g_open_task(
            repo_root=repo_root,
            output_root=repo_root / LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
            admission=admission,
            task=task,
            verified_admission=verified,
            verified_task_inputs=inputs,
            # This is a historical replay of an immutable attempt.  The
            # admission's archived code bundle, not the current worktree,
            # defines the code identity that produced the terminal.
            observed_code_tree_hash=admission.code_tree_hash,
        )
    except (LF022ExecutionError, LF022ExecutorError, ValueError) as exc:
        raise LF022RouteQualificationError(
            f"failed qualification exact replay rejected: {exc}"
        ) from exc
    if (
        not replay.replayed
        or replay.network_calls_this_run != 0
        or replay.terminal is None
        or replay.terminal_path is None
        or replay.terminal.status not in {"provider_exhausted", "proposer_parse_failed"}
        or replay.terminal.provisional_variant_count != 0
        or replay.terminal.variants_artifact is not None
        or replay.terminal.terminal_error_code is None
    ):
        raise LF022RouteQualificationError(
            "supersession requires one exact offline-replayed failed qualification"
        )
    return replay.terminal, replay.terminal_path


def supersede_lf022_failed_qualification(
    *,
    repo_root: Path,
    previous_admission_binding: LF022ArtifactBinding,
    previous_task_binding: LF022ArtifactBinding,
    next_decoding_contract_id: Literal[
        "qwen3_5_proposer_qualification_v2",
        "glm5_2_proposer_qualification_v2",
    ],
) -> SupersededLF022Qualification:
    """Persist an append-only retry authority after replaying a failed terminal."""

    admission = _load_model(
        repo_root=repo_root,
        binding=previous_admission_binding,
        model=LF022GOpenExecutionAdmission,
        label="previous qualification admission",
    )
    task = _load_model(
        repo_root=repo_root,
        binding=previous_task_binding,
        model=LF022GOpenExecutionTask,
        label="previous qualification task",
    )
    expected_transition = {
        "qwen3": (
            "qwen3_5_proposer_qualification_v1",
            "qwen3_5_proposer_qualification_v2",
        ),
        "glm5": (
            "glm5_2_proposer_qualification_v1",
            "glm5_2_proposer_qualification_v2",
        ),
    }.get(admission.route.proposer_family_id)
    if expected_transition != (
        admission.route.decoding.contract_id,
        next_decoding_contract_id,
    ):
        raise LF022RouteQualificationError(
            "qualification supersession is not the reviewed v1-to-v2 family transition"
        )
    terminal, terminal_path = _failed_qualification_replay(
        repo_root=repo_root,
        admission=admission,
        task=task,
    )
    claim = make_lf022_qualification_claim(admission=admission, task=task)
    claim_path = lf022_qualification_claim_path(
        output_root=repo_root / LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
        admission=admission,
        claim=claim,
    )
    observed_claim = _load_model(
        repo_root=repo_root,
        binding=_binding(repo_root, claim_path),
        model=LF022QualificationClaim,
        label="previous qualification claim",
    )
    if observed_claim != claim:
        raise LF022RouteQualificationError(
            "previous qualification claim differs from the failed replay"
        )
    payload: dict[str, object] = {
        "schema_version": 1,
        "proposer_family_id": admission.route.proposer_family_id,
        "model_id": admission.route.model_id,
        "previous_claim_id": claim.claim_id,
        "previous_claim": _binding(repo_root, claim_path).model_dump(mode="json"),
        "previous_admission_id": admission.admission_id,
        "previous_admission": previous_admission_binding.model_dump(mode="json"),
        "previous_task_id": task.execution_task_id,
        "previous_task": previous_task_binding.model_dump(mode="json"),
        "previous_terminal_id": terminal.terminal_id,
        "previous_terminal": _binding(repo_root, terminal_path).model_dump(mode="json"),
        "previous_terminal_status": terminal.status,
        "previous_terminal_error_code": terminal.terminal_error_code,
        "previous_decoding_contract_id": admission.route.decoding.contract_id,
        "next_decoding_contract_id": next_decoding_contract_id,
        "reason": "replay_verified_failed_qualification",
        "exact_failed_replay_verified": True,
        "replay_network_calls": 0,
        "semantic_labels_created": False,
        "training_eligible": False,
        "gate_credit_claimed": False,
    }
    supersession = LF022QualificationSupersession.model_validate(
        {
            **payload,
            "supersession_id": make_id("lf022_qualification_supersession", payload),
        }
    )
    path = _qualification_supersession_path(repo_root, supersession)
    _write_immutable(
        path,
        canonical_json_bytes(supersession.model_dump(mode="json")) + b"\n",
    )
    persisted = verify_lf022_qualification_supersession(
        repo_root=repo_root,
        supersession_binding=_binding(repo_root, path),
    )
    if persisted != supersession:
        raise LF022RouteQualificationError("persisted qualification supersession differs")
    return SupersededLF022Qualification(
        supersession=persisted,
        supersession_path=path,
    )


def verify_lf022_qualification_supersession(
    *,
    repo_root: Path,
    supersession_binding: LF022ArtifactBinding,
) -> LF022QualificationSupersession:
    """Replay and verify an immutable failed-only qualification supersession."""

    supersession = _load_model(
        repo_root=repo_root,
        binding=supersession_binding,
        model=LF022QualificationSupersession,
        label="qualification supersession",
    )
    expected_path = (
        _qualification_supersession_path(
            repo_root,
            supersession,
        )
        .relative_to(repo_root)
        .as_posix()
    )
    if supersession_binding.path != expected_path:
        raise LF022RouteQualificationError(
            "qualification supersession is outside the content-addressed registry"
        )
    admission = _load_model(
        repo_root=repo_root,
        binding=supersession.previous_admission,
        model=LF022GOpenExecutionAdmission,
        label="superseded qualification admission",
    )
    task = _load_model(
        repo_root=repo_root,
        binding=supersession.previous_task,
        model=LF022GOpenExecutionTask,
        label="superseded qualification task",
    )
    claim = _load_model(
        repo_root=repo_root,
        binding=supersession.previous_claim,
        model=LF022QualificationClaim,
        label="superseded qualification claim",
    )
    terminal, terminal_path = _failed_qualification_replay(
        repo_root=repo_root,
        admission=admission,
        task=task,
    )
    expected_claim = make_lf022_qualification_claim(admission=admission, task=task)
    if (
        supersession.proposer_family_id != admission.route.proposer_family_id
        or supersession.model_id != admission.route.model_id
        or supersession.previous_admission_id != admission.admission_id
        or supersession.previous_task_id != task.execution_task_id
        or supersession.previous_claim_id != claim.claim_id
        or claim != expected_claim
        or supersession.previous_terminal_id != terminal.terminal_id
        or supersession.previous_terminal != _binding(repo_root, terminal_path)
        or supersession.previous_terminal_status != terminal.status
        or supersession.previous_terminal_error_code != terminal.terminal_error_code
        or supersession.previous_decoding_contract_id != admission.route.decoding.contract_id
    ):
        raise LF022RouteQualificationError(
            "qualification supersession differs from exact failed replay lineage"
        )
    expected_next = {
        "qwen3": "qwen3_5_proposer_qualification_v2",
        "glm5": "glm5_2_proposer_qualification_v2",
    }[supersession.proposer_family_id]
    if supersession.next_decoding_contract_id != expected_next:
        raise LF022RouteQualificationError(
            "qualification supersession names an unreviewed recovery contract"
        )
    return supersession


def _load_variants(
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
) -> tuple[VariantRecord, ...]:
    path = _bound_path(
        repo_root=repo_root,
        binding=binding,
        label="qualification variants",
    )
    lines = path.read_bytes().splitlines(keepends=True)
    records: list[VariantRecord] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.endswith(b"\n"):
            raise LF022RouteQualificationError(
                f"qualification variants:{line_number} lacks final newline"
            )
        try:
            record = VariantRecord.model_validate_json(line)
        except ValueError as exc:
            raise LF022RouteQualificationError(
                f"invalid qualification variant:{line_number}: {exc}"
            ) from exc
        if line != canonical_json_bytes(record.model_dump(mode="json")) + b"\n":
            raise LF022RouteQualificationError(
                f"qualification variant:{line_number} is not canonical JSONL"
            )
        records.append(record)
    return tuple(records)


def _eligibility_payload(
    *,
    repo_root: Path,
    admission: LF022GOpenExecutionAdmission,
    task: LF022GOpenExecutionTask,
    claim_path: Path,
    terminal: LF022ExecutionTerminalRecord,
    terminal_path: Path,
    matrix: LF022ProductionFamilyMatrix,
    matrix_binding: LF022ArtifactBinding,
) -> dict[str, object]:
    if terminal.variants_artifact is None:
        raise LF022RouteQualificationError("successful qualification lacks variants artifact")
    variants_path = repo_root / terminal.variants_artifact
    call_path = repo_root / terminal.llm_call_artifact
    call = _load_model(
        repo_root=repo_root,
        binding=_binding(repo_root, call_path),
        model=LLMCallRecord,
        label="qualification LLM call",
    )
    task_dir = _task_directory(repo_root, task.execution_task_id)
    validators = tuple(
        sorted(
            family
            for family in matrix.sci_validator_family_ids
            if family not in {admission.route.proposer_family_id, matrix.heldout_eval_family_id}
        )
    )
    return {
        "schema_version": 1,
        "status": "live_qualification_replay_verified",
        "proposer_family_id": admission.route.proposer_family_id,
        "model_id": admission.route.model_id,
        "deployment_id": admission.route.deployment_id,
        "canonical_family": admission.route.canonical_family,
        "provider_id": admission.route.provider_id,
        "catalog_snapshot_id": admission.route.catalog_snapshot_id,
        "route_snapshot_revision": admission.route.route_snapshot_revision,
        "decoding_contract_id": admission.route.decoding.contract_id,
        "decoding_contract_hash": hash_canonical(admission.route.decoding.model_dump(mode="json")),
        "family_matrix_id": matrix.matrix_id,
        "family_matrix": matrix_binding.model_dump(mode="json"),
        "qualification_contract": admission.artifacts.reviewed_route_contract.model_dump(
            mode="json"
        ),
        "qualification_claim_id": make_lf022_qualification_claim(
            admission=admission,
            task=task,
        ).claim_id,
        "qualification_claim": _binding(repo_root, claim_path).model_dump(mode="json"),
        "qualification_admission_id": admission.admission_id,
        "qualification_admission": _binding(repo_root, task_dir / "admission.json").model_dump(
            mode="json"
        ),
        "qualification_task_id": task.execution_task_id,
        "qualification_task": _binding(repo_root, task_dir / "task.json").model_dump(mode="json"),
        "qualification_terminal_id": terminal.terminal_id,
        "qualification_terminal": _binding(repo_root, terminal_path).model_dump(mode="json"),
        "qualification_variants": _binding(repo_root, variants_path).model_dump(mode="json"),
        "qualification_llm_call_id": call.call_id,
        "qualification_llm_call": _binding(repo_root, call_path).model_dump(mode="json"),
        "qualification_provider_request_hash": cast(str, call.provider_request_hash),
        "qualification_completed_at": cast(
            str,
            call.model_dump(mode="json")["completed_at"],
        ),
        "qualification_task_count": 1,
        "qualification_variant_count": 1,
        "qualification_execution_mode": "external",
        "exact_replay_verified": True,
        "production_execution_scope": "public_provisional_g_open",
        "judge_family_ids": list(task.allocation_task.judge_family_ids),
        "permitted_validator_family_ids": list(validators),
        "proposer_validator_same_family_forbidden": True,
        "heldout_eval_family_id": matrix.heldout_eval_family_id,
        "heldout_eval_supervision_excluded": True,
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "output_quality_tier": "provisional",
        "outputs_unresolved": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }


def certify_lf022_proposer_production_eligibility(
    *,
    repo_root: Path,
    qualification_admission_binding: LF022ArtifactBinding,
    qualification_task_binding: LF022ArtifactBinding,
) -> CertifiedLF022ProposerRoute:
    """Replay one live qualification and persist canonical route eligibility.

    The call performs no provider request.  It succeeds only when the normal
    executor can replay an already-persisted successful terminal lineage.
    """

    admission = _load_model(
        repo_root=repo_root,
        binding=qualification_admission_binding,
        model=LF022GOpenExecutionAdmission,
        label="qualification admission",
    )
    task = _load_model(
        repo_root=repo_root,
        binding=qualification_task_binding,
        model=LF022GOpenExecutionTask,
        label="qualification task",
    )
    if (
        admission.route.proposer_family_id not in _QUALIFICATION_FAMILIES
        or admission.route.execution_scope != "one_item_proposer_qualification_only"
    ):
        raise LF022RouteQualificationError(
            "only an exact one-item Qwen/GLM qualification may create eligibility"
        )
    try:
        verified = verify_lf022_execution_admission(
            repo_root=repo_root,
            admission=admission,
        )
        inputs = load_lf022_execution_task_inputs(repo_root=repo_root, verified=verified)
        verify_lf022_execution_task(
            repo_root=repo_root,
            admission=admission,
            verified=verified,
            task=task,
            inputs=inputs,
        )
        replay = execute_lf022_g_open_task(
            repo_root=repo_root,
            output_root=repo_root / LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
            admission=admission,
            task=task,
            verified_admission=verified,
            verified_task_inputs=inputs,
            # Certification is an audit of an immutable historical attempt.
            # The admission's already-validated code bundle defines the code
            # identity that produced the terminal, so later repository work
            # must not make that successful attempt uncertifiable.
            observed_code_tree_hash=admission.code_tree_hash,
        )
    except (LF022ExecutionError, LF022ExecutorError, ValueError) as exc:
        raise LF022RouteQualificationError(f"qualification exact replay rejected: {exc}") from exc
    if (
        not replay.replayed
        or replay.network_calls_this_run != 0
        or replay.terminal is None
        or replay.terminal_path is None
        or replay.terminal.status != "provisional_variants_created"
        or replay.terminal.provisional_variant_count != 1
    ):
        raise LF022RouteQualificationError(
            "production eligibility requires one successful, offline-replayed qualification result"
        )
    expected_claim = make_lf022_qualification_claim(admission=admission, task=task)
    claim_path = lf022_qualification_claim_path(
        output_root=repo_root / LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
        admission=admission,
        claim=expected_claim,
    )
    observed_claim = _load_model(
        repo_root=repo_root,
        binding=_binding(repo_root, claim_path),
        model=LF022QualificationClaim,
        label="qualification claim",
    )
    if observed_claim != expected_claim:
        raise LF022RouteQualificationError(
            "repository-global qualification claim differs from replayed task"
        )
    matrix_binding = verified.plan.artifacts.family_matrix
    payload = _eligibility_payload(
        repo_root=repo_root,
        admission=admission,
        task=task,
        claim_path=claim_path,
        terminal=replay.terminal,
        terminal_path=replay.terminal_path,
        matrix=verified.family_matrix,
        matrix_binding=matrix_binding,
    )
    eligibility = LF022QualifiedProposerProductionEligibility.model_validate(
        {
            **payload,
            "eligibility_id": make_id("lf022_route_eligibility", payload),
        }
    )
    path = (
        repo_root
        / LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT
        / "production_eligibility"
        / f"{admission.route.proposer_family_id}.json"
    )
    _write_immutable(
        path,
        canonical_json_bytes(eligibility.model_dump(mode="json")) + b"\n",
    )
    binding = _binding(repo_root, path)
    verified_eligibility = verify_lf022_proposer_production_eligibility(
        repo_root=repo_root,
        eligibility_binding=binding,
    )
    if verified_eligibility != eligibility:
        raise LF022RouteQualificationError("persisted eligibility replay differs")
    return CertifiedLF022ProposerRoute(
        eligibility=eligibility,
        eligibility_path=path,
    )


def verify_lf022_proposer_production_eligibility(
    *,
    repo_root: Path,
    eligibility_binding: LF022ArtifactBinding,
) -> LF022QualifiedProposerProductionEligibility:
    """Independently replay an exact eligibility binding without network I/O."""

    eligibility = _load_model(
        repo_root=repo_root,
        binding=eligibility_binding,
        model=LF022QualifiedProposerProductionEligibility,
        label="proposer production eligibility",
    )
    expected_path = (
        f"{LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT}/production_eligibility/"
        f"{eligibility.proposer_family_id}.json"
    )
    if eligibility_binding.path != expected_path:
        raise LF022RouteQualificationError(
            "production eligibility is outside the canonical family registry"
        )
    admission = _load_model(
        repo_root=repo_root,
        binding=eligibility.qualification_admission,
        model=LF022GOpenExecutionAdmission,
        label="bound qualification admission",
    )
    task = _load_model(
        repo_root=repo_root,
        binding=eligibility.qualification_task,
        model=LF022GOpenExecutionTask,
        label="bound qualification task",
    )
    claim = _load_model(
        repo_root=repo_root,
        binding=eligibility.qualification_claim,
        model=LF022QualificationClaim,
        label="bound qualification claim",
    )
    if (
        admission.route.execution_scope != "one_item_proposer_qualification_only"
        or admission.route.proposer_family_id != eligibility.proposer_family_id
        or admission.route.model_id != eligibility.model_id
        or admission.route.deployment_id != eligibility.deployment_id
        or admission.route.canonical_family != eligibility.canonical_family
        or admission.route.provider_id != eligibility.provider_id
        or admission.route.catalog_snapshot_id != eligibility.catalog_snapshot_id
        or admission.route.route_snapshot_revision != eligibility.route_snapshot_revision
        or admission.route.decoding.contract_id != eligibility.decoding_contract_id
        or hash_canonical(admission.route.decoding.model_dump(mode="json"))
        != eligibility.decoding_contract_hash
        or admission.artifacts.reviewed_route_contract != eligibility.qualification_contract
        or admission.admission_id != eligibility.qualification_admission_id
        or task.execution_task_id != eligibility.qualification_task_id
    ):
        raise LF022RouteQualificationError(
            "eligibility identity differs from its exact qualification route"
        )
    expected_claim = make_lf022_qualification_claim(admission=admission, task=task)
    if claim != expected_claim or claim.claim_id != eligibility.qualification_claim_id:
        raise LF022RouteQualificationError(
            "eligibility claim differs from exact qualification task"
        )
    try:
        verified = verify_lf022_execution_admission(
            repo_root=repo_root,
            admission=admission,
        )
        inputs = load_lf022_execution_task_inputs(repo_root=repo_root, verified=verified)
        verify_lf022_execution_task(
            repo_root=repo_root,
            admission=admission,
            verified=verified,
            task=task,
            inputs=inputs,
        )
        replay = execute_lf022_g_open_task(
            repo_root=repo_root,
            output_root=repo_root / LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
            admission=admission,
            task=task,
            verified_admission=verified,
            verified_task_inputs=inputs,
            # Eligibility verification replays the same immutable historical
            # qualification and therefore uses its validated archived code
            # identity rather than whichever worktree happens to inspect it.
            observed_code_tree_hash=admission.code_tree_hash,
        )
    except (LF022ExecutionError, LF022ExecutorError, ValueError) as exc:
        raise LF022RouteQualificationError(
            f"eligibility qualification replay rejected: {exc}"
        ) from exc
    if (
        not replay.replayed
        or replay.network_calls_this_run != 0
        or replay.terminal is None
        or replay.terminal_path is None
        or replay.terminal.status != "provisional_variants_created"
        or replay.terminal.provisional_variant_count != 1
        or replay.terminal.terminal_id != eligibility.qualification_terminal_id
        or _binding(repo_root, replay.terminal_path) != eligibility.qualification_terminal
    ):
        raise LF022RouteQualificationError(
            "eligibility terminal differs from exact offline qualification replay"
        )
    if verified.plan.artifacts.family_matrix != eligibility.family_matrix:
        raise LF022RouteQualificationError(
            "eligibility family matrix differs from qualification plan"
        )
    matrix = _load_model(
        repo_root=repo_root,
        binding=eligibility.family_matrix,
        model=LF022ProductionFamilyMatrix,
        label="eligibility family matrix",
    )
    if matrix.matrix_id != eligibility.family_matrix_id:
        raise LF022RouteQualificationError("eligibility family-matrix ID differs")
    if (
        task.allocation_task.judge_family_ids != eligibility.judge_family_ids
        or task.allocation_task.heldout_eval_family_id != eligibility.heldout_eval_family_id
        or not task.allocation_task.heldout_eval_supervision_excluded
    ):
        raise LF022RouteQualificationError(
            "eligibility supervision families differ from qualification allocation"
        )
    expected_validators = tuple(
        sorted(
            family
            for family in matrix.sci_validator_family_ids
            if family not in {eligibility.proposer_family_id, matrix.heldout_eval_family_id}
        )
    )
    if eligibility.permitted_validator_family_ids != expected_validators:
        raise LF022RouteQualificationError(
            "eligibility validator families differ from the exact family matrix"
        )
    terminal = replay.terminal
    if terminal.variants_artifact is None:
        raise LF022RouteQualificationError("qualification terminal lacks variants")
    if (
        _binding(repo_root, repo_root / terminal.variants_artifact)
        != eligibility.qualification_variants
    ):
        raise LF022RouteQualificationError("eligibility variant binding differs from terminal")
    variants = _load_variants(
        repo_root=repo_root,
        binding=eligibility.qualification_variants,
    )
    if len(variants) != 1 or any(
        variant.quality_tier is not QualityTier.PROVISIONAL
        or variant.validation_status is not ValidationStatus.UNVALIDATED
        for variant in variants
    ):
        raise LF022RouteQualificationError(
            "qualification output must remain one unvalidated provisional variant"
        )
    if (
        _binding(repo_root, repo_root / terminal.llm_call_artifact)
        != eligibility.qualification_llm_call
    ):
        raise LF022RouteQualificationError("eligibility LLM-call binding differs from terminal")
    call = _load_model(
        repo_root=repo_root,
        binding=eligibility.qualification_llm_call,
        model=LLMCallRecord,
        label="eligibility LLM call",
    )
    if (
        call.call_id != eligibility.qualification_llm_call_id
        or call.provider != eligibility.provider_id
        or call.model != eligibility.model_id
        or call.model_family != eligibility.canonical_family
        or call.role is not LLMRole.PROPOSER
        or call.execution_mode != "external"
        or call.provider_request_hash != eligibility.qualification_provider_request_hash
        or call.private_source_content
        or call.supervision_eligible
        or call.completed_at != eligibility.qualification_completed_at
    ):
        raise LF022RouteQualificationError(
            "eligibility LLM call is not the exact public proposer qualification"
        )
    return eligibility


__all__ = [
    "CertifiedLF022ProposerRoute",
    "LF022QualifiedProposerProductionEligibility",
    "LF022RouteQualificationError",
    "SupersededLF022Qualification",
    "certify_lf022_proposer_production_eligibility",
    "supersede_lf022_failed_qualification",
    "verify_lf022_proposer_production_eligibility",
    "verify_lf022_qualification_supersession",
]
