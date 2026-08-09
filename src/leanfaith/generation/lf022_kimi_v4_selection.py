"""Offline-only Kimi-v4 requalification challenge selection.

This module freezes a deterministic 16-case challenge from one exact Kimi-v3
prefix-256 execution lineage.  It deliberately has no provider transport,
execution-admission, or promotion boundary.  In particular, producing or
verifying this artifact does *not* authorize a Kimi-v4 call.

Legacy ``empty_response`` terminals are not trusted as category labels.  The
selector binds and reparses the final HTTP-200 wire body with the current
response/parser stack.  Only a current ``finish_reason=length`` parse outcome
can enter the output-budget-exhausted stratum.
"""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_batch import (
    LF022BatchError,
    LF022BatchRunReport,
    LF022PublicBatchManifest,
    VerifiedLF022BatchTask,
    load_lf022_public_batch,
)
from leanfaith.generation.lf022_execution import LF022RCPDecodingContract
from leanfaith.generation.lf022_executor import (
    LF022ExecutionAttemptRecord,
    LF022ExecutionTerminalRecord,
    LF022WireResponseMetadata,
)
from leanfaith.generation.lf022_historical_replay import (
    LF022HistoricalModuleBinding,
    LF022HistoricalReplayError,
    LF022HistoricalTerminalBinding,
    run_lf022_historical_replay,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.generation.llm_variants import (
    VariantOutputErrorCode,
    VariantOutputParseError,
    parse_variant_proposer_output,
)
from leanfaith.generation.rcp_provider import RCPResponseError, parse_chat_completion
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id

LF022_KIMI_V4_SELECTION_RULE = "lf022_kimi_v4_challenge_selection_v1"
LF022_KIMI_V4_SELECTION_ROOT = "artifacts/generation/lf022_kimi_v4_challenge_selection_v1"
LF022_KIMI_V3_TASK_COUNT = 256
LF022_KIMI_V4_CHALLENGE_SIZE = 16

KimiV4ChallengeRole = Literal[
    "budget_exhausted",
    "proof_bearing",
    "prior_success",
]
CurrentParserOutcome = Literal[
    "output_budget_exhausted",
    "proof_bearing_candidate",
    "strict_variant_success",
    "other_response",
    "non_http_200",
]

_ROLE_COUNTS: dict[KimiV4ChallengeRole, int] = {
    "budget_exhausted": 6,
    "proof_bearing": 2,
    "prior_success": 8,
}
_ORDERED_ROLES: tuple[KimiV4ChallengeRole, ...] = (
    "budget_exhausted",
    "proof_bearing",
    "prior_success",
)


class LF022KimiV4SelectionError(RuntimeError):
    """The offline challenge lineage or deterministic selection failed closed."""


class _KimiV4PromptContract(StrictModel):
    artifact: Literal["prompts/proposers/lean_variant_v2.txt"]
    sha256: str = Field(pattern=HEX64_PATTERN)
    template_id: Literal["lean_variant"]
    template_version: Literal["v2"]


class _KimiV4PriorContract(StrictModel):
    contract_id: Literal["kimi_k2_7_public_smoke_v3"]
    max_tokens: Literal[16384]
    immutable_lineage_required: Literal[True]


class _KimiV3ReviewedLineage(StrictModel):
    """The one preregistered scientific v3 population; not a compatibility class."""

    batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    execution_admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    batch_manifest: LF022ArtifactBinding
    execution_admission: LF022ArtifactBinding
    exact_offline_replay_report_id: str = Field(pattern=id_pattern("lf022_batch_run"))
    exact_offline_replay_report: LF022ArtifactBinding


class _KimiV4CapabilityPolicy(StrictModel):
    max_tokens_32768_provider_support: Literal["unverified_until_one_live_call"]
    official_per_model_output_limit_disclosed: Literal[False]
    mass_execution_before_capability_success: Literal[False]


class _KimiV4FailurePolicy(StrictModel):
    finish_reason_length: Literal["output_budget_exhausted"]
    output_budget_exhausted_retry_identical_payload: Literal[False]
    partial_content_parse_allowed: Literal[False]
    strict_proposer_parser: Literal[True]
    parser_relaxation_allowed: Literal[False]


class _KimiV4RequalificationPolicy(StrictModel):
    challenge_size: Literal[16]
    prior_output_budget_exhausted_items: Literal[6]
    prior_proof_bearing_items: Literal[2]
    prior_success_controls: Literal[8]
    max_concurrency: Literal[1]
    minimum_strict_parse_successes: Literal[14]
    maximum_output_budget_exhausted: Literal[0]
    maximum_http_200_empty_responses: Literal[0]
    prior_proof_bearing_error_may_repeat: Literal[False]
    exact_replay_required: Literal[True]


class _KimiV4PromotionPolicy(StrictModel):
    enabled: Literal[False]
    requires_separate_qualification_artifact: Literal[True]
    successful_v3_tasks_are_not_rerun: Literal[True]


class LF022KimiV4ChallengeContract(StrictModel):
    """Typed projection of the reviewed, still-nonadmissible v4 contract."""

    schema_version: Literal[1]
    artifact_class: Literal["proposer_requalification_contract"]
    contract_id: Literal["kimi_k2_7_public_proposer_v4"]
    role: Literal["proposer"]
    qualification_status: Literal["pending_capability_and_challenge_requalification"]
    model_id: Literal["moonshotai/Kimi-K2.7-Code"]
    family_id: Literal["moonshot_kimi_k2"]
    canonical_family: Literal["moonshotai/kimi-k2"]
    provider: Literal["epfl_rcp"]
    transport: Literal["rcp_openai_compatible"]
    execution_scope: Literal["public_provisional_g_open"]
    prompt: _KimiV4PromptContract
    proposal_count: Literal[1]
    decoding: LF022RCPDecodingContract
    prior_contract: _KimiV4PriorContract
    prior_lineage: _KimiV3ReviewedLineage
    capability_policy: _KimiV4CapabilityPolicy
    failure_policy: _KimiV4FailurePolicy
    requalification: _KimiV4RequalificationPolicy
    promotion: _KimiV4PromotionPolicy

    @model_validator(mode="after")
    def _exact_contract(self) -> Self:
        if self.decoding.contract_id != self.contract_id:
            raise ValueError("v4 decoding contract ID differs from the document")
        return self


class LF022KimiV3ChallengePopulationItem(StrictModel):
    """One exact task/terminal lineage in the 256-case source population."""

    allocation_task_id: str = Field(pattern=id_pattern("lf022_production_task"))
    source_admission_record_id: str = Field(pattern=id_pattern("lf022_source_admission"))
    source_theorem_id: str = Field(pattern=id_pattern("thm"))
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    task: LF022ArtifactBinding
    terminal_id: str = Field(pattern=id_pattern("lf022_execution_terminal"))
    terminal: LF022ArtifactBinding
    terminal_status: str = Field(min_length=1)
    terminal_error_code: str | None = None
    final_attempt: LF022ArtifactBinding
    final_wire_response_body: LF022ArtifactBinding | None = None
    final_http_status: int | None = Field(default=None, ge=100, le=599)
    current_parser_outcome: CurrentParserOutcome
    current_parser_error_code: str | None = None
    eligible_role: KimiV4ChallengeRole | None = None


class LF022KimiV4SelectedChallengeItem(StrictModel):
    """One selected case in fixed capability/challenge order."""

    selection_rank: int = Field(ge=0, lt=LF022_KIMI_V4_CHALLENGE_SIZE, strict=True)
    role: KimiV4ChallengeRole
    role_rank: int = Field(ge=0, lt=8, strict=True)
    allocation_task_id: str = Field(pattern=id_pattern("lf022_production_task"))
    source_admission_record_id: str = Field(pattern=id_pattern("lf022_source_admission"))
    source_theorem_id: str = Field(pattern=id_pattern("thm"))
    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    task: LF022ArtifactBinding
    terminal_id: str = Field(pattern=id_pattern("lf022_execution_terminal"))
    terminal: LF022ArtifactBinding
    final_wire_response_body: LF022ArtifactBinding | None = None
    current_parser_outcome: CurrentParserOutcome


class LF022KimiV4ChallengeSelection(StrictModel):
    """Content-addressed, offline-only 16-case Kimi-v4 challenge selection."""

    schema_version: Literal[1] = 1
    selection_id: str = Field(pattern=id_pattern("lf022_kimi_v4_selection"))
    selection_rule: Literal["lf022_kimi_v4_challenge_selection_v1"]
    status: Literal["frozen_offline_selection_only"]
    v3_batch_id: str = Field(pattern=id_pattern("lf022_public_batch"))
    v3_batch_manifest: LF022ArtifactBinding
    v3_admission_id: str = Field(pattern=id_pattern("lf022_execution_admission"))
    v3_admission: LF022ArtifactBinding
    v3_contract_id: Literal["kimi_k2_7_public_smoke_v3"]
    v3_contract_hash: str = Field(pattern=HEX64_PATTERN)
    exact_offline_replay_report_id: str = Field(pattern=id_pattern("lf022_batch_run"))
    exact_offline_replay_report: LF022ArtifactBinding
    replayed_terminal_count: Literal[256]
    replay_network_calls: Literal[0]
    replay_orchestration_errors: Literal[0]
    historical_code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    historical_code_bundle: LF022ArtifactBinding
    historical_module_bindings: tuple[LF022HistoricalModuleBinding, ...] = Field(min_length=1)
    historical_replay_network_calls: Literal[0]
    historical_terminal_bindings: tuple[LF022HistoricalTerminalBinding, ...] = Field(
        min_length=LF022_KIMI_V3_TASK_COUNT,
        max_length=LF022_KIMI_V3_TASK_COUNT,
    )
    v4_contract_id: Literal["kimi_k2_7_public_proposer_v4"]
    v4_contract_hash: str = Field(pattern=HEX64_PATTERN)
    v4_contract: LF022ArtifactBinding
    v4_prompt: LF022ArtifactBinding
    population: tuple[LF022KimiV3ChallengePopulationItem, ...] = Field(
        min_length=LF022_KIMI_V3_TASK_COUNT,
        max_length=LF022_KIMI_V3_TASK_COUNT,
    )
    selected: tuple[LF022KimiV4SelectedChallengeItem, ...] = Field(
        min_length=LF022_KIMI_V4_CHALLENGE_SIZE,
        max_length=LF022_KIMI_V4_CHALLENGE_SIZE,
    )
    capability_allocation_task_id: str = Field(pattern=id_pattern("lf022_production_task"))
    selected_budget_exhausted_count: Literal[6]
    selected_proof_bearing_count: Literal[2]
    selected_prior_success_count: Literal[8]
    unique_selected_source_count: Literal[16]
    unique_selected_theorem_count: Literal[16]
    offline_selection_only: Literal[True] = True
    live_calls_performed: Literal[False] = False
    execution_admission_created: Literal[False] = False
    promotion_enabled: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        allocation_ids = tuple(item.allocation_task_id for item in self.population)
        if allocation_ids != tuple(sorted(set(allocation_ids))):
            raise ValueError("population must be allocation-ID sorted and unique")
        execution_ids = tuple(item.execution_task_id for item in self.population)
        if len(set(execution_ids)) != LF022_KIMI_V3_TASK_COUNT:
            raise ValueError("population execution task IDs must be unique")
        historical_by_task = {
            item.execution_task_id: item for item in self.historical_terminal_bindings
        }
        if set(historical_by_task) != set(execution_ids):
            raise ValueError("historical replay task set differs from the population")
        for population_record in self.population:
            historical = historical_by_task[population_record.execution_task_id]
            if (
                historical.terminal_id != population_record.terminal_id
                or historical.terminal_artifact != population_record.terminal
            ):
                raise ValueError("population terminal differs from historical replay")
        expected_roles = ("budget_exhausted",) * 6 + ("proof_bearing",) * 2 + ("prior_success",) * 8
        roles = tuple(item.role for item in self.selected)
        if roles != expected_roles:
            raise ValueError("selected roles differ from fixed 6/2/8 ordering")
        if tuple(item.selection_rank for item in self.selected) != tuple(range(16)):
            raise ValueError("selection ranks must be contiguous")
        expected_role_ranks = tuple(range(6)) + tuple(range(2)) + tuple(range(8))
        if tuple(item.role_rank for item in self.selected) != expected_role_ranks:
            raise ValueError("role ranks differ from fixed category order")
        selected_allocations = tuple(item.allocation_task_id for item in self.selected)
        if len(set(selected_allocations)) != 16:
            raise ValueError("selected allocation task IDs must be unique")
        sources = tuple(item.source_admission_record_id for item in self.selected)
        if len(set(sources)) != 16:
            raise ValueError("selected source admission IDs must be unique")
        theorems = tuple(item.source_theorem_id for item in self.selected)
        if len(set(theorems)) != 16:
            raise ValueError("selected source theorem IDs must be unique")
        if self.capability_allocation_task_id != self.selected[0].allocation_task_id:
            raise ValueError("capability task must be the first selected budget case")
        by_allocation = {item.allocation_task_id: item for item in self.population}
        for selected_item in self.selected:
            population_item = by_allocation.get(selected_item.allocation_task_id)
            if population_item is None:
                raise ValueError("selected item is absent from the bound population")
            if (
                population_item.eligible_role != selected_item.role
                or population_item.source_admission_record_id
                != selected_item.source_admission_record_id
                or population_item.source_theorem_id != selected_item.source_theorem_id
                or population_item.execution_task_id != selected_item.execution_task_id
                or population_item.task != selected_item.task
                or population_item.terminal_id != selected_item.terminal_id
                or population_item.terminal != selected_item.terminal
                or population_item.final_wire_response_body
                != selected_item.final_wire_response_body
                or population_item.current_parser_outcome != selected_item.current_parser_outcome
            ):
                raise ValueError("selected item differs from its population lineage")
        if self.selected != _select(self.population):
            raise ValueError("selected items differ from deterministic category-first rule")
        expected = make_id(
            "lf022_kimi_v4_selection",
            self.model_dump(mode="json", exclude={"selection_id"}),
        )
        if self.selection_id != expected:
            raise ValueError("selection_id does not match canonical challenge content")
        return self


@dataclass(frozen=True, slots=True)
class FrozenLF022KimiV4ChallengeSelection:
    selection: LF022KimiV4ChallengeSelection
    selection_path: Path


def _safe_relative(value: str, *, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value.strip()
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
    ):
        raise LF022KimiV4SelectionError(f"{label} must be a normalized repository path")
    return path


def _repo_file(
    *,
    repo_root: Path,
    path: str | Path,
    label: str,
) -> Path:
    root = repo_root.resolve(strict=True)
    value = Path(path)
    candidate = value if value.is_absolute() else root / value
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LF022KimiV4SelectionError(f"{label} escapes the repository") from exc
    _safe_relative(PurePosixPath(relative.as_posix()).as_posix(), label=label)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise LF022KimiV4SelectionError(f"{label} contains a symlinked component")
    if not current.is_file():
        raise LF022KimiV4SelectionError(f"{label} is missing or not a regular file")
    return current


def _binding(repo_root: Path, path: Path) -> LF022ArtifactBinding:
    resolved = _repo_file(repo_root=repo_root, path=path, label="artifact")
    return LF022ArtifactBinding(
        path=resolved.relative_to(repo_root.resolve()).as_posix(),
        sha256=hash_file(resolved),
    )


def _bound_file(
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    label: str,
    expected_parent: PurePosixPath | None = None,
) -> Path:
    path = _repo_file(repo_root=repo_root, path=binding.path, label=label)
    if expected_parent is not None and not PurePosixPath(binding.path).is_relative_to(
        expected_parent
    ):
        raise LF022KimiV4SelectionError(f"{label} escapes its canonical task directory")
    if hash_file(path) != binding.sha256:
        raise LF022KimiV4SelectionError(f"{label} hash differs from its binding")
    return path


def _load_canonical[RecordT: StrictModel](
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    model: type[RecordT],
    label: str,
    trailing_newline: bool,
) -> RecordT:
    path = _bound_file(repo_root=repo_root, binding=binding, label=label)
    raw = path.read_bytes()
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022KimiV4SelectionError(f"invalid {label}: {exc}") from exc
    expected = canonical_json_bytes(record.model_dump(mode="json"))
    if trailing_newline:
        expected += b"\n"
    if raw != expected:
        raise LF022KimiV4SelectionError(f"{label} is not canonical JSON")
    return record


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise LF022KimiV4SelectionError("selection output cannot be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022KimiV4SelectionError(f"existing challenge selection differs: {path}")
        return
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022KimiV4SelectionError(
                    f"concurrent challenge selection differs: {path}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _selection_output_path(repo_root: Path, digest: str) -> Path:
    root = repo_root.resolve(strict=True)
    relative_parent = _safe_relative(
        LF022_KIMI_V4_SELECTION_ROOT,
        label="challenge selection registry",
    )
    current = root
    for part in relative_parent.parts:
        current /= part
        if current.is_symlink():
            raise LF022KimiV4SelectionError(
                "challenge selection registry contains a symlinked component"
            )
        if current.exists() and not current.is_dir():
            raise LF022KimiV4SelectionError(
                "challenge selection registry contains a non-directory component"
            )
        current.mkdir(exist_ok=True)
    return current / f"{digest}.json"


def _load_v4_contract(
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
) -> tuple[LF022KimiV4ChallengeContract, LF022ArtifactBinding]:
    path = _bound_file(repo_root=repo_root, binding=binding, label="Kimi-v4 contract")
    try:
        mapping = dict(load_yaml_mapping(path))
        decoding = dict(cast(dict[str, object], mapping["decoding"]))
        decoding.update(schema_version=1, contract_id=mapping["contract_id"])
        mapping["decoding"] = decoding
        contract = LF022KimiV4ChallengeContract.model_validate(mapping)
    except (KeyError, TypeError, ValueError) as exc:
        raise LF022KimiV4SelectionError(f"invalid Kimi-v4 contract: {exc}") from exc
    prompt_path = _repo_file(
        repo_root=repo_root,
        path=contract.prompt.artifact,
        label="Kimi-v4 prompt",
    )
    if hash_file(prompt_path) != contract.prompt.sha256:
        raise LF022KimiV4SelectionError("Kimi-v4 prompt differs from the contract")
    return contract, _binding(repo_root, prompt_path)


def _load_replay_report(
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    manifest: LF022PublicBatchManifest,
) -> LF022BatchRunReport:
    report = _load_canonical(
        repo_root=repo_root,
        binding=binding,
        model=LF022BatchRunReport,
        label="exact offline replay report",
        trailing_newline=False,
    )
    expected_path = (
        PurePosixPath(manifest.batch_directory)
        / "runs"
        / f"{report.report_id.split(':', 1)[1]}.json"
    ).as_posix()
    if binding.path != expected_path:
        raise LF022KimiV4SelectionError(
            "exact replay report is outside the frozen batch run registry"
        )
    if (
        report.schema_version != 2
        or report.batch_id != manifest.batch_id
        or report.mode != "offline"
        or report.task_count != LF022_KIMI_V3_TASK_COUNT
        or report.preflight_only_count != 0
        or report.replayed_terminal_count != LF022_KIMI_V3_TASK_COUNT
        or report.new_terminal_count != 0
        or report.error_count != 0
        or report.network_calls_this_run != 0
        or report.failed_task_ids
        or report.successful_terminal_count is None
        or report.failed_terminal_count is None
    ):
        raise LF022KimiV4SelectionError(
            "challenge selection requires a complete 256-terminal exact offline replay"
        )
    return report


def _bound_child(
    *,
    repo_root: Path,
    path: str,
    sha256: str,
    task_parent: PurePosixPath,
    label: str,
) -> LF022ArtifactBinding:
    binding = LF022ArtifactBinding(path=path, sha256=sha256)
    _bound_file(
        repo_root=repo_root,
        binding=binding,
        label=label,
        expected_parent=task_parent,
    )
    return binding


def _current_response_class(
    *,
    body: bytes,
    model_id: str,
    loaded: VerifiedLF022BatchTask,
) -> tuple[CurrentParserOutcome, str | None, bool]:
    try:
        completion = parse_chat_completion(body, expected_model=model_id)
    except RCPResponseError as exc:
        outcome: CurrentParserOutcome = (
            "output_budget_exhausted" if exc.code == "output_budget_exhausted" else "other_response"
        )
        return outcome, exc.code, False
    try:
        proposals = parse_variant_proposer_output(completion.content)
        request = loaded.task.prompt_request()
        strict_success = len(proposals.variants) == request.proposal_count and all(
            variant.intended_relation in request.requested_relations
            for variant in proposals.variants
        )
        if not strict_success:
            return "other_response", "request_mismatch", False
    except VariantOutputParseError as exc:
        if exc.code is VariantOutputErrorCode.PROOF_BEARING_CANDIDATE:
            return "proof_bearing_candidate", exc.code.value, False
        return "other_response", exc.code.value, False
    except ValueError:
        return "other_response", "request_mismatch", False
    return "strict_variant_success", None, True


def _population_item(
    *,
    repo_root: Path,
    executor_output_root: str,
    task_binding: LF022ArtifactBinding,
    loaded: VerifiedLF022BatchTask,
    historical_terminal: LF022HistoricalTerminalBinding,
) -> LF022KimiV3ChallengePopulationItem:
    task_id = loaded.task.execution_task_id
    digest = task_id.split(":", 1)[1]
    task_parent = PurePosixPath(executor_output_root) / "tasks" / digest[:2] / digest
    task_path = _bound_file(
        repo_root=repo_root,
        binding=task_binding,
        label="frozen v3 task",
    )
    terminal_path = _repo_file(
        repo_root=repo_root,
        path=(task_parent / "terminal.json").as_posix(),
        label="v3 execution terminal",
    )
    terminal_binding = _binding(repo_root, terminal_path)
    terminal = _load_canonical(
        repo_root=repo_root,
        binding=terminal_binding,
        model=LF022ExecutionTerminalRecord,
        label="v3 execution terminal",
        trailing_newline=True,
    )
    if (
        terminal.execution_task_id != task_id
        or terminal.execution_admission_id != loaded.admission.admission_id
    ):
        raise LF022KimiV4SelectionError("terminal differs from its frozen v3 task")
    if (
        historical_terminal.execution_task_id != task_id
        or historical_terminal.terminal_id != terminal.terminal_id
        or historical_terminal.terminal_artifact != terminal_binding
    ):
        raise LF022KimiV4SelectionError(
            "current terminal differs from admitted-code historical replay"
        )

    attempts: list[tuple[LF022ExecutionAttemptRecord, LF022ArtifactBinding]] = []
    for index, (artifact, sha256) in enumerate(
        zip(terminal.attempt_artifacts, terminal.attempt_sha256s, strict=True)
    ):
        attempt_binding = _bound_child(
            repo_root=repo_root,
            path=artifact,
            sha256=sha256,
            task_parent=task_parent,
            label=f"v3 attempt {index}",
        )
        attempt = _load_canonical(
            repo_root=repo_root,
            binding=attempt_binding,
            model=LF022ExecutionAttemptRecord,
            label=f"v3 attempt {index}",
            trailing_newline=True,
        )
        if attempt.execution_task_id != task_id or attempt.attempt_index != index:
            raise LF022KimiV4SelectionError("attempt order differs from terminal lineage")
        _bound_child(
            repo_root=repo_root,
            path=attempt.request_artifact,
            sha256=attempt.request_sha256,
            task_parent=task_parent,
            label=f"v3 provider request {index}",
        )
        _bound_child(
            repo_root=repo_root,
            path=attempt.wire_request_artifact,
            sha256=attempt.wire_request_sha256,
            task_parent=task_parent,
            label=f"v3 wire request {index}",
        )
        _bound_child(
            repo_root=repo_root,
            path=attempt.provider_raw_artifact,
            sha256=attempt.provider_raw_sha256,
            task_parent=task_parent,
            label=f"v3 provider raw response {index}",
        )
        if attempt.wire_response_body_artifact is not None:
            assert attempt.wire_response_body_sha256 is not None
            assert attempt.wire_response_metadata_artifact is not None
            assert attempt.wire_response_metadata_sha256 is not None
            response_body = _bound_child(
                repo_root=repo_root,
                path=attempt.wire_response_body_artifact,
                sha256=attempt.wire_response_body_sha256,
                task_parent=task_parent,
                label=f"v3 wire response body {index}",
            )
            response_metadata_binding = _bound_child(
                repo_root=repo_root,
                path=attempt.wire_response_metadata_artifact,
                sha256=attempt.wire_response_metadata_sha256,
                task_parent=task_parent,
                label=f"v3 wire response metadata {index}",
            )
            response_metadata = _load_canonical(
                repo_root=repo_root,
                binding=response_metadata_binding,
                model=LF022WireResponseMetadata,
                label=f"v3 wire response metadata {index}",
                trailing_newline=True,
            )
            if (
                response_metadata.body_sha256 != response_body.sha256
                or response_metadata.status_code != attempt.http_status
            ):
                raise LF022KimiV4SelectionError(
                    "attempt wire body/metadata differ from their binding"
                )
        attempts.append((attempt, attempt_binding))

    # The terminal also binds normalized LLM attempts, call metadata, and any
    # variants.  They are not category signals, but their bytes must still be
    # present before this population can be called exact.
    for index, (artifact, sha256) in enumerate(
        zip(terminal.llm_attempt_artifacts, terminal.llm_attempt_sha256s, strict=True)
    ):
        _bound_child(
            repo_root=repo_root,
            path=artifact,
            sha256=sha256,
            task_parent=task_parent,
            label=f"v3 LLM attempt {index}",
        )
    _bound_child(
        repo_root=repo_root,
        path=terminal.llm_call_artifact,
        sha256=terminal.llm_call_sha256,
        task_parent=task_parent,
        label="v3 LLM call",
    )
    if terminal.variants_artifact is not None and terminal.variants_sha256 is not None:
        _bound_child(
            repo_root=repo_root,
            path=terminal.variants_artifact,
            sha256=terminal.variants_sha256,
            task_parent=task_parent,
            label="v3 variants",
        )

    final_attempt, final_attempt_binding = attempts[-1]
    final_body_binding: LF022ArtifactBinding | None = None
    parser_outcome: CurrentParserOutcome = "non_http_200"
    parser_error: str | None = None
    strict_success = False
    if final_attempt.wire_response_body_artifact is not None:
        assert final_attempt.wire_response_body_sha256 is not None
        assert final_attempt.wire_response_metadata_artifact is not None
        assert final_attempt.wire_response_metadata_sha256 is not None
        final_body_binding = _bound_child(
            repo_root=repo_root,
            path=final_attempt.wire_response_body_artifact,
            sha256=final_attempt.wire_response_body_sha256,
            task_parent=task_parent,
            label="final v3 wire response body",
        )
        metadata_binding = _bound_child(
            repo_root=repo_root,
            path=final_attempt.wire_response_metadata_artifact,
            sha256=final_attempt.wire_response_metadata_sha256,
            task_parent=task_parent,
            label="final v3 wire response metadata",
        )
        metadata = _load_canonical(
            repo_root=repo_root,
            binding=metadata_binding,
            model=LF022WireResponseMetadata,
            label="final v3 wire response metadata",
            trailing_newline=True,
        )
        if (
            metadata.body_sha256 != final_body_binding.sha256
            or metadata.status_code != final_attempt.http_status
        ):
            raise LF022KimiV4SelectionError("wire body/metadata differ from final attempt")
        if metadata.status_code == 200:
            body_path = _bound_file(
                repo_root=repo_root,
                binding=final_body_binding,
                label="final v3 wire response body",
                expected_parent=task_parent,
            )
            parser_outcome, parser_error, strict_success = _current_response_class(
                body=body_path.read_bytes(),
                model_id=loaded.admission.route.model_id,
                loaded=loaded,
            )

    role: KimiV4ChallengeRole | None = None
    if parser_outcome == "output_budget_exhausted" and terminal.status == "provider_exhausted":
        role = "budget_exhausted"
    elif parser_outcome == "proof_bearing_candidate" and terminal.status == "proposer_parse_failed":
        role = "proof_bearing"
    elif strict_success and terminal.status == "provisional_variants_created":
        role = "prior_success"

    return LF022KimiV3ChallengePopulationItem(
        allocation_task_id=loaded.task.allocation_task.task_id,
        source_admission_record_id=loaded.task.allocation_task.admission_record_id,
        source_theorem_id=loaded.task.source.source_theorem_id,
        execution_task_id=task_id,
        task=_binding(repo_root, task_path),
        terminal_id=terminal.terminal_id,
        terminal=terminal_binding,
        terminal_status=terminal.status,
        terminal_error_code=terminal.terminal_error_code,
        final_attempt=final_attempt_binding,
        final_wire_response_body=final_body_binding,
        final_http_status=final_attempt.http_status,
        current_parser_outcome=parser_outcome,
        current_parser_error_code=parser_error,
        eligible_role=role,
    )


def _select(
    population: tuple[LF022KimiV3ChallengePopulationItem, ...],
) -> tuple[LF022KimiV4SelectedChallengeItem, ...]:
    selected: list[LF022KimiV4SelectedChallengeItem] = []
    seen_sources: set[str] = set()
    seen_theorems: set[str] = set()
    for role in _ORDERED_ROLES:
        candidates = sorted(
            (item for item in population if item.eligible_role == role),
            key=lambda item: item.allocation_task_id,
        )
        role_selected: list[LF022KimiV3ChallengePopulationItem] = []
        for item in candidates:
            if (
                item.source_admission_record_id in seen_sources
                or item.source_theorem_id in seen_theorems
            ):
                continue
            role_selected.append(item)
            seen_sources.add(item.source_admission_record_id)
            seen_theorems.add(item.source_theorem_id)
            if len(role_selected) == _ROLE_COUNTS[role]:
                break
        if len(role_selected) != _ROLE_COUNTS[role]:
            raise LF022KimiV4SelectionError(
                f"insufficient unique-source/theorem {role} cases for fixed challenge"
            )
        for role_rank, item in enumerate(role_selected):
            selected.append(
                LF022KimiV4SelectedChallengeItem(
                    selection_rank=len(selected),
                    role=role,
                    role_rank=role_rank,
                    allocation_task_id=item.allocation_task_id,
                    source_admission_record_id=item.source_admission_record_id,
                    source_theorem_id=item.source_theorem_id,
                    execution_task_id=item.execution_task_id,
                    task=item.task,
                    terminal_id=item.terminal_id,
                    terminal=item.terminal,
                    final_wire_response_body=item.final_wire_response_body,
                    current_parser_outcome=item.current_parser_outcome,
                )
            )
    return tuple(selected)


def _build_selection(
    *,
    repo_root: Path,
    v3_manifest_binding: LF022ArtifactBinding,
    exact_offline_replay_report_binding: LF022ArtifactBinding,
    v4_contract_binding: LF022ArtifactBinding,
) -> LF022KimiV4ChallengeSelection:
    v4_contract, v4_prompt_binding = _load_v4_contract(
        repo_root=repo_root,
        binding=v4_contract_binding,
    )
    reviewed_lineage = v4_contract.prior_lineage
    if (
        v3_manifest_binding != reviewed_lineage.batch_manifest
        or exact_offline_replay_report_binding != reviewed_lineage.exact_offline_replay_report
    ):
        raise LF022KimiV4SelectionError(
            "inputs differ from the exact preregistered Kimi-v3 scientific lineage"
        )
    try:
        manifest, loaded_tasks = load_lf022_public_batch(
            repo_root=repo_root,
            manifest_binding=v3_manifest_binding,
        )
    except (LF022BatchError, ValueError) as exc:
        raise LF022KimiV4SelectionError(f"invalid Kimi-v3 batch: {exc}") from exc
    if len(manifest.routes) != 1 or len(loaded_tasks) != LF022_KIMI_V3_TASK_COUNT:
        raise LF022KimiV4SelectionError("Kimi-v3 source must be one exact prefix-256 route")
    route = manifest.routes[0]
    admission = loaded_tasks[0].admission
    if (
        route.proposer_family_id != "moonshot_kimi_k2"
        or route.model_id != "moonshotai/Kimi-K2.7-Code"
        or route.admission_id != admission.admission_id
        or admission.route.decoding.contract_id != "kimi_k2_7_public_smoke_v3"
        or any(item.admission != admission for item in loaded_tasks)
    ):
        raise LF022KimiV4SelectionError("source batch is not one exact Kimi-v3 admission")
    replay = _load_replay_report(
        repo_root=repo_root,
        binding=exact_offline_replay_report_binding,
        manifest=manifest,
    )
    if (
        manifest.batch_id != reviewed_lineage.batch_id
        or route.admission_id != reviewed_lineage.execution_admission_id
        or route.admission != reviewed_lineage.execution_admission
        or replay.report_id != reviewed_lineage.exact_offline_replay_report_id
    ):
        raise LF022KimiV4SelectionError(
            "inputs differ from the exact preregistered Kimi-v3 scientific lineage"
        )

    task_bindings = {task.execution_task_id: task.task for task in route.tasks}
    loaded_task_ids = {item.task.execution_task_id for item in loaded_tasks}
    if len(task_bindings) != LF022_KIMI_V3_TASK_COUNT or set(task_bindings) != loaded_task_ids:
        raise LF022KimiV4SelectionError("v3 manifest task bindings are not unique and complete")
    try:
        historical_replay = run_lf022_historical_replay(
            repo_root=repo_root,
            manifest_binding=v3_manifest_binding,
            loaded_tasks=loaded_tasks,
            executor_output_root=manifest.executor_output_root,
        )
    except LF022HistoricalReplayError as exc:
        raise LF022KimiV4SelectionError(f"admitted-code historical replay failed: {exc}") from exc
    historical_code_bundle = admission.artifacts.code_bundle
    _bound_file(
        repo_root=repo_root,
        binding=historical_code_bundle,
        label="historical code bundle",
    )
    if (
        historical_replay.code_tree_hash != admission.code_tree_hash
        or historical_replay.code_bundle_sha256 != historical_code_bundle.sha256
        or historical_replay.network_calls_performed != 0
        or len(historical_replay.terminal_bindings) != LF022_KIMI_V3_TASK_COUNT
    ):
        raise LF022KimiV4SelectionError("historical replay differs from the admitted code lineage")
    historical_by_task = {
        item.execution_task_id: item for item in historical_replay.terminal_bindings
    }
    if set(historical_by_task) != loaded_task_ids:
        raise LF022KimiV4SelectionError(
            "historical replay terminal task set differs from the source batch"
        )
    population = tuple(
        sorted(
            (
                _population_item(
                    repo_root=repo_root,
                    executor_output_root=manifest.executor_output_root,
                    task_binding=task_bindings[item.task.execution_task_id],
                    loaded=item,
                    historical_terminal=historical_by_task[item.task.execution_task_id],
                )
                for item in loaded_tasks
            ),
            key=lambda item: item.allocation_task_id,
        )
    )
    observed_counts = Counter(item.terminal_status for item in population)
    if (
        replay.terminal_status_counts != dict(sorted(observed_counts.items()))
        or replay.successful_terminal_count != observed_counts["provisional_variants_created"]
        or replay.failed_terminal_count
        != LF022_KIMI_V3_TASK_COUNT - observed_counts["provisional_variants_created"]
    ):
        raise LF022KimiV4SelectionError(
            "exact replay report counts differ from the bound 256 terminal artifacts"
        )
    selected = _select(population)
    payload: dict[str, object] = {
        "schema_version": 1,
        "selection_rule": LF022_KIMI_V4_SELECTION_RULE,
        "status": "frozen_offline_selection_only",
        "v3_batch_id": manifest.batch_id,
        "v3_batch_manifest": v3_manifest_binding.model_dump(mode="json"),
        "v3_admission_id": admission.admission_id,
        "v3_admission": route.admission.model_dump(mode="json"),
        "v3_contract_id": admission.route.decoding.contract_id,
        "v3_contract_hash": hash_canonical(admission.route.decoding.model_dump(mode="json")),
        "exact_offline_replay_report_id": replay.report_id,
        "exact_offline_replay_report": exact_offline_replay_report_binding.model_dump(mode="json"),
        "replayed_terminal_count": replay.replayed_terminal_count,
        "replay_network_calls": replay.network_calls_this_run,
        "replay_orchestration_errors": replay.error_count,
        "historical_code_tree_hash": historical_replay.code_tree_hash,
        "historical_code_bundle": historical_code_bundle.model_dump(mode="json"),
        "historical_module_bindings": [
            item.model_dump(mode="json") for item in historical_replay.module_bindings
        ],
        "historical_replay_network_calls": historical_replay.network_calls_performed,
        "historical_terminal_bindings": [
            item.model_dump(mode="json") for item in historical_replay.terminal_bindings
        ],
        "v4_contract_id": v4_contract.contract_id,
        "v4_contract_hash": hash_canonical(v4_contract.model_dump(mode="json")),
        "v4_contract": v4_contract_binding.model_dump(mode="json"),
        "v4_prompt": v4_prompt_binding.model_dump(mode="json"),
        "population": [item.model_dump(mode="json") for item in population],
        "selected": [item.model_dump(mode="json") for item in selected],
        "capability_allocation_task_id": selected[0].allocation_task_id,
        "selected_budget_exhausted_count": 6,
        "selected_proof_bearing_count": 2,
        "selected_prior_success_count": 8,
        "unique_selected_source_count": 16,
        "unique_selected_theorem_count": 16,
        "offline_selection_only": True,
        "live_calls_performed": False,
        "execution_admission_created": False,
        "promotion_enabled": False,
        "semantic_labels_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022KimiV4ChallengeSelection.model_validate(
        {
            **payload,
            "selection_id": make_id("lf022_kimi_v4_selection", payload),
        }
    )


def freeze_lf022_kimi_v4_challenge_selection(
    *,
    repo_root: Path,
    v3_manifest_binding: LF022ArtifactBinding,
    exact_offline_replay_report_binding: LF022ArtifactBinding,
    v4_contract_binding: LF022ArtifactBinding,
) -> FrozenLF022KimiV4ChallengeSelection:
    """Freeze the deterministic challenge without executing or admitting v4."""

    selection = _build_selection(
        repo_root=repo_root,
        v3_manifest_binding=v3_manifest_binding,
        exact_offline_replay_report_binding=exact_offline_replay_report_binding,
        v4_contract_binding=v4_contract_binding,
    )
    digest = selection.selection_id.split(":", 1)[1]
    path = _selection_output_path(repo_root, digest)
    _write_immutable(
        path,
        canonical_json_bytes(selection.model_dump(mode="json")) + b"\n",
    )
    verified = verify_lf022_kimi_v4_challenge_selection(
        repo_root=repo_root,
        selection_binding=_binding(repo_root, path),
    )
    if verified != selection:
        raise LF022KimiV4SelectionError("persisted challenge selection differs")
    return FrozenLF022KimiV4ChallengeSelection(selection=selection, selection_path=path)


def verify_lf022_kimi_v4_challenge_selection(
    *,
    repo_root: Path,
    selection_binding: LF022ArtifactBinding,
) -> LF022KimiV4ChallengeSelection:
    """Recompute and verify one immutable offline-only challenge selection."""

    selection = _load_canonical(
        repo_root=repo_root,
        binding=selection_binding,
        model=LF022KimiV4ChallengeSelection,
        label="Kimi-v4 challenge selection",
        trailing_newline=True,
    )
    expected_path = (
        PurePosixPath(LF022_KIMI_V4_SELECTION_ROOT)
        / f"{selection.selection_id.split(':', 1)[1]}.json"
    ).as_posix()
    if selection_binding.path != expected_path:
        raise LF022KimiV4SelectionError(
            "challenge selection is outside its content-addressed registry"
        )
    expected = _build_selection(
        repo_root=repo_root,
        v3_manifest_binding=selection.v3_batch_manifest,
        exact_offline_replay_report_binding=selection.exact_offline_replay_report,
        v4_contract_binding=selection.v4_contract,
    )
    if selection != expected:
        raise LF022KimiV4SelectionError(
            "challenge selection differs from deterministic replay and raw-body classification"
        )
    return selection


__all__ = [
    "CurrentParserOutcome",
    "FrozenLF022KimiV4ChallengeSelection",
    "KimiV4ChallengeRole",
    "LF022KimiV3ChallengePopulationItem",
    "LF022KimiV4ChallengeContract",
    "LF022KimiV4ChallengeSelection",
    "LF022KimiV4SelectedChallengeItem",
    "LF022KimiV4SelectionError",
    "freeze_lf022_kimi_v4_challenge_selection",
    "verify_lf022_kimi_v4_challenge_selection",
]
