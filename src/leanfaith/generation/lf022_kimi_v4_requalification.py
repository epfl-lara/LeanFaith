"""Fail-closed live requalification for the frozen Kimi-v4 challenge.

The offline selector binds 16 public Lean-only requests.  This module executes
exactly that selection in two stages: one capability request, followed by the
remaining 15 requests only after the capability response passes the existing
strict proposer parser.  All request/response bytes are immutable and replayed
without network access.  A completed challenge is still qualification evidence
only: it creates no semantic labels and cannot admit production execution.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_execution import LF022GOpenExecutionTask
from leanfaith.generation.lf022_kimi_v4_selection import (
    LF022KimiV4ChallengeContract,
    LF022KimiV4ChallengeSelection,
    LF022KimiV4SelectedChallengeItem,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.generation.llm_variants import (
    VariantOutputErrorCode,
    VariantOutputParseError,
    parse_variant_proposer_output,
    render_variant_proposer_prompt,
    variant_provider_input_ids,
)
from leanfaith.generation.providers import (
    ProviderIdentity,
    ProviderRawResponse,
    ProviderRequest,
    load_provider_request,
    persist_provider_raw_response,
    persist_provider_request,
)
from leanfaith.generation.rcp_provider import (
    RCPHTTPTransport,
    RCPResponseError,
    RCPTransportUnknownError,
    RCPWireResponse,
    classify_http_response,
    make_chat_completion_payload,
    parse_chat_completion,
    retry_delay_seconds,
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.manifest import require_utc

LF022_KIMI_V4_REQUALIFICATION_ROOT = "data/lf022_kimi_v4_requalification/v1"

KimiV4Stage = Literal["capability", "remaining", "replay"]
KimiV4AttemptStatus = Literal[
    "strict_variant_success",
    "proposer_parse_failed",
    "invalid_response",
    "retryable_http_error",
    "terminal_http_error",
    "transport_unknown",
]
KimiV4TerminalStatus = Literal[
    "strict_variant_success",
    "proposer_parse_failed",
    "provider_exhausted",
    "transport_unknown",
]


class LF022KimiV4RequalificationError(RuntimeError):
    """The live challenge or exact offline replay failed closed."""


@dataclass(frozen=True, slots=True, repr=False)
class KimiV4RuntimeCredentials:
    """Runtime-only credentials; the API key is never serialized."""

    base_url: str
    api_key: str

    def __repr__(self) -> str:
        return f"KimiV4RuntimeCredentials(base_url={self.base_url!r}, api_key='<redacted>')"


class LF022KimiV4TaskRecord(StrictModel):
    """One requalification request derived from one frozen selected case."""

    schema_version: Literal[1] = 1
    task_id: str = Field(pattern=id_pattern("lf022_kimi_v4_task"))
    selection_id: str = Field(pattern=id_pattern("lf022_kimi_v4_selection"))
    selection_rank: int = Field(ge=0, lt=16, strict=True)
    capability: bool
    historical_role: Literal["budget_exhausted", "proof_bearing", "prior_success"]
    source_theorem_id: str = Field(pattern=id_pattern("thm"))
    historical_execution_task: LF022ArtifactBinding
    v4_contract: LF022ArtifactBinding
    prompt_render_sha256: str = Field(pattern=HEX64_PATTERN)
    model_id: Literal["moonshotai/Kimi-K2.7-Code"]
    decoding_contract_id: Literal["kimi_k2_7_public_proposer_v4"]
    source_is_public: Literal[True] = True
    private_source_content: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if self.capability != (self.selection_rank == 0):
            raise ValueError("only selection rank zero is the capability task")
        expected = make_id(
            "lf022_kimi_v4_task",
            self.model_dump(mode="json", exclude={"task_id"}),
        )
        if self.task_id != expected:
            raise ValueError("task_id does not match canonical requalification input")
        return self


class LF022KimiV4WireMetadata(StrictModel):
    schema_version: Literal[1] = 1
    status_code: int = Field(ge=100, le=599, strict=True)
    headers: dict[str, str]
    body_sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _sorted_headers(self) -> Self:
        if list(self.headers) != sorted(self.headers):
            raise ValueError("wire response headers must be sorted")
        return self


class LF022KimiV4AttemptRecord(StrictModel):
    schema_version: Literal[1] = 1
    task_id: str = Field(pattern=id_pattern("lf022_kimi_v4_task"))
    attempt_index: int = Field(ge=0, strict=True)
    provider_request_hash: str = Field(pattern=HEX64_PATTERN)
    provider_attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    provider_request: LF022ArtifactBinding
    wire_request: LF022ArtifactBinding
    wire_response_body: LF022ArtifactBinding | None = None
    wire_response_metadata: LF022ArtifactBinding | None = None
    provider_raw: LF022ArtifactBinding
    parsed_variants: LF022ArtifactBinding | None = None
    status: KimiV4AttemptStatus
    retryable: bool
    http_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: float | None = Field(default=None, ge=0.0)
    error_code: str | None = None
    provider_request_id: str | None = None
    returned_model: str | None = None
    finish_reason: str | None = None
    tokens: dict[str, int] = Field(default_factory=dict)
    started_at: datetime.datetime
    completed_at: datetime.datetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.started_at)
        require_utc(self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("attempt completion precedes start")
        response = self.wire_response_body is not None
        if response != (self.wire_response_metadata is not None):
            raise ValueError("wire body and metadata bindings must be complete")
        if self.status == "transport_unknown":
            if response or self.http_status is not None:
                raise ValueError("transport_unknown cannot bind a response")
        elif not response:
            raise ValueError("non-transport attempt requires a persisted response")
        if self.status == "strict_variant_success":
            if (
                self.error_code is not None
                or self.retryable
                or self.http_status != 200
                or self.parsed_variants is None
                or self.returned_model != "moonshotai/Kimi-K2.7-Code"
            ):
                raise ValueError("strict success fields are inconsistent")
        elif self.error_code is None or self.parsed_variants is not None:
            raise ValueError("failed attempt requires an error and no parsed variants")
        if self.retryable != (self.status == "retryable_http_error"):
            raise ValueError("only retryable HTTP errors may be retried")
        return self


class LF022KimiV4TerminalRecord(StrictModel):
    schema_version: Literal[1] = 1
    terminal_id: str = Field(pattern=id_pattern("lf022_kimi_v4_terminal"))
    task_id: str = Field(pattern=id_pattern("lf022_kimi_v4_task"))
    selection_id: str = Field(pattern=id_pattern("lf022_kimi_v4_selection"))
    selection_rank: int = Field(ge=0, lt=16, strict=True)
    status: KimiV4TerminalStatus
    error_code: str | None = None
    attempts: tuple[LF022ArtifactBinding, ...] = Field(min_length=1, max_length=3)
    parsed_variants: LF022ArtifactBinding | None = None
    network_calls_total: int = Field(ge=0, le=3, strict=True)
    exact_replay_supported: Literal[True] = True
    output_quality_tier: Literal["provisional"] = "provisional"
    semantic_labels_created: Literal[False] = False
    silver_promotion_enabled: Literal[False] = False
    gold_promotion_enabled: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        success = self.status == "strict_variant_success"
        if success != (self.parsed_variants is not None) or success != (self.error_code is None):
            raise ValueError("terminal success fields are inconsistent")
        expected = make_id(
            "lf022_kimi_v4_terminal",
            self.model_dump(mode="json", exclude={"terminal_id"}),
        )
        if self.terminal_id != expected:
            raise ValueError("terminal_id does not match canonical result")
        return self


class LF022KimiV4QualificationRecord(StrictModel):
    """Separate challenge result; never a production admission."""

    schema_version: Literal[1] = 1
    qualification_id: str = Field(pattern=id_pattern("lf022_kimi_v4_qualification"))
    selection_id: str = Field(pattern=id_pattern("lf022_kimi_v4_selection"))
    status: Literal["passed", "failed"]
    terminals: tuple[LF022ArtifactBinding, ...] = Field(min_length=16, max_length=16)
    terminal_status_counts: dict[str, int]
    terminal_error_counts: dict[str, int]
    strict_parse_success_count: int = Field(ge=0, le=16, strict=True)
    output_budget_exhausted_count: int = Field(ge=0, le=16, strict=True)
    http_200_empty_response_count: int = Field(ge=0, le=16, strict=True)
    prior_proof_bearing_repeat_count: int = Field(ge=0, le=2, strict=True)
    capability_passed: bool
    minimum_strict_parse_successes: Literal[14] = 14
    maximum_output_budget_exhausted: Literal[0] = 0
    maximum_http_200_empty_responses: Literal[0] = 0
    prior_proof_bearing_error_may_repeat: Literal[False] = False
    production_admission_created: Literal[False] = False
    promotion_enabled: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        passed = (
            self.capability_passed
            and self.strict_parse_success_count >= 14
            and self.output_budget_exhausted_count == 0
            and self.http_200_empty_response_count == 0
            and self.prior_proof_bearing_repeat_count == 0
        )
        if self.status != ("passed" if passed else "failed"):
            raise ValueError("qualification status differs from frozen criteria")
        expected = make_id(
            "lf022_kimi_v4_qualification",
            self.model_dump(mode="json", exclude={"qualification_id"}),
        )
        if self.qualification_id != expected:
            raise ValueError("qualification_id does not match canonical challenge result")
        return self


@dataclass(frozen=True, slots=True)
class LF022KimiV4StageResult:
    stage: KimiV4Stage
    terminals: tuple[LF022KimiV4TerminalRecord, ...]
    terminal_paths: tuple[Path, ...]
    network_calls_this_run: int
    qualification: LF022KimiV4QualificationRecord | None
    qualification_path: Path | None


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
        raise LF022KimiV4RequalificationError(f"{label} is not a safe repository path")
    return path


def _repo_path(repo_root: Path, relative: str, *, label: str) -> Path:
    root = repo_root.resolve(strict=True)
    path = _safe_relative(relative, label=label)
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise LF022KimiV4RequalificationError(f"{label} contains a symlink")
    if not current.is_file():
        raise LF022KimiV4RequalificationError(f"{label} is missing")
    return current


def _binding(repo_root: Path, path: Path) -> LF022ArtifactBinding:
    root = repo_root.resolve(strict=True)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise LF022KimiV4RequalificationError("artifact is missing, unsafe, or external")
    return LF022ArtifactBinding(
        path=path.resolve().relative_to(root).as_posix(),
        sha256=hash_file(path),
    )


def _bound(repo_root: Path, binding: LF022ArtifactBinding, *, label: str) -> Path:
    path = _repo_path(repo_root, binding.path, label=label)
    if hash_file(path) != binding.sha256:
        raise LF022KimiV4RequalificationError(f"{label} differs from its binding")
    return path


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise LF022KimiV4RequalificationError("output path cannot be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022KimiV4RequalificationError(f"immutable output conflict: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
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
                raise LF022KimiV4RequalificationError(
                    f"concurrent immutable output conflict: {path}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _canonical(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _load_record[RecordT: StrictModel](path: Path, model: type[RecordT], *, label: str) -> RecordT:
    if path.is_symlink() or not path.is_file():
        raise LF022KimiV4RequalificationError(f"{label} is missing or unsafe")
    raw = path.read_bytes()
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022KimiV4RequalificationError(f"invalid {label}: {exc}") from exc
    if raw != _canonical(record):
        raise LF022KimiV4RequalificationError(f"{label} is not canonical JSON")
    return record


def _load_contract(
    repo_root: Path,
    selection: LF022KimiV4ChallengeSelection,
) -> LF022KimiV4ChallengeContract:
    path = _bound(repo_root, selection.v4_contract, label="Kimi-v4 contract")
    try:
        mapping = dict(load_yaml_mapping(path))
        decoding = dict(mapping["decoding"])
        decoding.update(schema_version=1, contract_id=mapping["contract_id"])
        mapping["decoding"] = decoding
        contract = LF022KimiV4ChallengeContract.model_validate(mapping)
    except (KeyError, TypeError, ValueError) as exc:
        raise LF022KimiV4RequalificationError(f"invalid Kimi-v4 contract: {exc}") from exc
    if hash_canonical(contract.model_dump(mode="json")) != selection.v4_contract_hash:
        raise LF022KimiV4RequalificationError("Kimi-v4 contract hash differs from selection")
    return contract


def _load_historical_task(
    repo_root: Path,
    selected: LF022KimiV4SelectedChallengeItem,
) -> LF022GOpenExecutionTask:
    path = _bound(repo_root, selected.task, label="selected historical task")
    try:
        task = LF022GOpenExecutionTask.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise LF022KimiV4RequalificationError(f"invalid historical task: {exc}") from exc
    if (
        task.execution_task_id != selected.execution_task_id
        or task.source.source_theorem_id != selected.source_theorem_id
        or not task.source.source_is_public
        or not task.source.external_transmission_allowed
        or task.source.denylist_hits
        or task.source.optional_natural_language is not None
    ):
        raise LF022KimiV4RequalificationError("selected task is not exact public Lean-only input")
    return task


def _load_route_revision(repo_root: Path, selection: LF022KimiV4ChallengeSelection) -> str:
    path = _bound(repo_root, selection.v3_admission, label="historical Kimi admission")
    try:
        admission = json.loads(path.read_bytes())
        route = admission["route"]
        admission_id = admission["admission_id"]
        model_id = route["model_id"]
        revision = route["route_snapshot_revision"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LF022KimiV4RequalificationError(f"invalid historical admission: {exc}") from exc
    if (
        admission_id != selection.v3_admission_id
        or model_id != "moonshotai/Kimi-K2.7-Code"
        or not isinstance(revision, str)
        or not revision.startswith("rcp-catalog-sha256:")
    ):
        raise LF022KimiV4RequalificationError("historical admission identity differs")
    return revision


def _run_root(repo_root: Path, selection: LF022KimiV4ChallengeSelection) -> Path:
    return (
        repo_root.resolve(strict=True)
        / LF022_KIMI_V4_REQUALIFICATION_ROOT
        / selection.selection_id.split(":", 1)[1]
    )


def _task_directory(run_root: Path, rank: int) -> Path:
    return run_root / "tasks" / f"{rank:02d}"


def _prepare_task(
    *,
    repo_root: Path,
    selection: LF022KimiV4ChallengeSelection,
    contract: LF022KimiV4ChallengeContract,
    selected: LF022KimiV4SelectedChallengeItem,
) -> tuple[LF022KimiV4TaskRecord, LF022GOpenExecutionTask, str, str]:
    historical = _load_historical_task(repo_root, selected)
    prompt_path = _bound(repo_root, selection.v4_prompt, label="Kimi-v4 prompt")
    rendered = render_variant_proposer_prompt(
        historical.prompt_request(),
        template_path=prompt_path,
    )
    if rendered.template_sha256 != contract.prompt.sha256:
        raise LF022KimiV4RequalificationError("rendered prompt differs from v4 contract")
    payload: dict[str, object] = {
        "schema_version": 1,
        "selection_id": selection.selection_id,
        "selection_rank": selected.selection_rank,
        "capability": selected.selection_rank == 0,
        "historical_role": selected.role,
        "source_theorem_id": selected.source_theorem_id,
        "historical_execution_task": selected.task.model_dump(mode="json"),
        "v4_contract": selection.v4_contract.model_dump(mode="json"),
        "prompt_render_sha256": rendered.render_sha256,
        "model_id": contract.model_id,
        "decoding_contract_id": contract.decoding.contract_id,
        "source_is_public": True,
        "private_source_content": False,
        "semantic_labels_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    record = LF022KimiV4TaskRecord.model_validate(
        {**payload, "task_id": make_id("lf022_kimi_v4_task", payload)}
    )
    return record, historical, rendered.text, rendered.template_sha256


def _provider_request(
    *,
    record: LF022KimiV4TaskRecord,
    historical: LF022GOpenExecutionTask,
    rendered_prompt: str,
    prompt_sha256: str,
    contract: LF022KimiV4ChallengeContract,
    route_revision: str,
    attempt_index: int,
) -> ProviderRequest:
    return ProviderRequest.create(
        identity=ProviderIdentity(
            provider="epfl_rcp",
            model=contract.model_id,
            revision=route_revision,
            transport="external_disabled",
        ),
        prompt_template_hash=prompt_sha256,
        rendered_prompt=rendered_prompt,
        decoding=contract.decoding.provider_decoding(),
        input_ids=(record.task_id, *variant_provider_input_ids(historical.prompt_request())),
        private_source_content=False,
        attempt_index=attempt_index,
    )


def _response_paths(*, task_dir: Path, response: RCPWireResponse) -> tuple[Path, Path]:
    body_path = task_dir / "wire" / "response.body"
    _write_immutable(body_path, response.body)
    metadata = LF022KimiV4WireMetadata(
        status_code=response.status_code,
        headers=dict(sorted(response.headers.items())),
        body_sha256=hash_file(body_path),
    )
    metadata_path = task_dir / "wire" / "response.json"
    _write_immutable(metadata_path, _canonical(metadata))
    return body_path, metadata_path


def _load_persisted_response(attempt_dir: Path) -> RCPWireResponse | None:
    body_path = attempt_dir / "wire" / "response.body"
    metadata_path = attempt_dir / "wire" / "response.json"
    if not body_path.exists() and not metadata_path.exists():
        return None
    if body_path.is_symlink() or not body_path.is_file():
        raise LF022KimiV4RequalificationError("persisted wire body is missing or unsafe")
    metadata = _load_record(
        metadata_path,
        LF022KimiV4WireMetadata,
        label="persisted Kimi-v4 wire metadata",
    )
    if hash_file(body_path) != metadata.body_sha256:
        raise LF022KimiV4RequalificationError("persisted wire metadata differs from body")
    return RCPWireResponse(
        status_code=metadata.status_code,
        headers=metadata.headers,
        body=body_path.read_bytes(),
    )


def _terminal_from_attempts(
    *,
    repo_root: Path,
    selection: LF022KimiV4ChallengeSelection,
    task: LF022KimiV4TaskRecord,
    attempts: list[tuple[LF022KimiV4AttemptRecord, Path]],
) -> LF022KimiV4TerminalRecord:
    final = attempts[-1][0]
    status: KimiV4TerminalStatus
    if final.status == "strict_variant_success":
        status = "strict_variant_success"
    elif final.status == "proposer_parse_failed":
        status = "proposer_parse_failed"
    elif final.status == "transport_unknown":
        status = "transport_unknown"
    else:
        status = "provider_exhausted"
    payload: dict[str, object] = {
        "schema_version": 1,
        "task_id": task.task_id,
        "selection_id": selection.selection_id,
        "selection_rank": task.selection_rank,
        "status": status,
        "error_code": final.error_code,
        "attempts": [_binding(repo_root, path).model_dump(mode="json") for _, path in attempts],
        "parsed_variants": (
            final.parsed_variants.model_dump(mode="json")
            if final.parsed_variants is not None
            else None
        ),
        "network_calls_total": len(attempts),
        "exact_replay_supported": True,
        "output_quality_tier": "provisional",
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022KimiV4TerminalRecord.model_validate(
        {**payload, "terminal_id": make_id("lf022_kimi_v4_terminal", payload)}
    )


def _verify_existing_terminal(
    *,
    repo_root: Path,
    selection: LF022KimiV4ChallengeSelection,
    contract: LF022KimiV4ChallengeContract,
    selected: LF022KimiV4SelectedChallengeItem,
    route_revision: str,
    terminal_path: Path,
) -> LF022KimiV4TerminalRecord:
    """Re-hash and re-parse the complete lineage behind one terminal."""

    terminal = _load_record(terminal_path, LF022KimiV4TerminalRecord, label="Kimi-v4 terminal")
    if (
        terminal.selection_id != selection.selection_id
        or terminal.selection_rank != selected.selection_rank
    ):
        raise LF022KimiV4RequalificationError("terminal belongs to another challenge item")
    task, historical, rendered_prompt, prompt_sha256 = _prepare_task(
        repo_root=repo_root,
        selection=selection,
        contract=contract,
        selected=selected,
    )
    persisted_task = _load_record(
        terminal_path.parent / "task.json",
        LF022KimiV4TaskRecord,
        label="Kimi-v4 task",
    )
    if persisted_task != task or terminal.task_id != task.task_id:
        raise LF022KimiV4RequalificationError("terminal task differs from frozen selection")

    attempts: list[tuple[LF022KimiV4AttemptRecord, Path]] = []
    for index, binding in enumerate(terminal.attempts):
        attempt_path = _bound(repo_root, binding, label="Kimi-v4 attempt binding")
        expected_attempt_path = terminal_path.parent / "attempts" / f"{index:04d}" / "attempt.json"
        if attempt_path != expected_attempt_path:
            raise LF022KimiV4RequalificationError("attempt path is noncanonical")
        attempt = _load_record(attempt_path, LF022KimiV4AttemptRecord, label="Kimi-v4 attempt")
        if attempt.task_id != task.task_id or attempt.attempt_index != index:
            raise LF022KimiV4RequalificationError("attempt order or task identity differs")
        expected_request = _provider_request(
            record=task,
            historical=historical,
            rendered_prompt=rendered_prompt,
            prompt_sha256=prompt_sha256,
            contract=contract,
            route_revision=route_revision,
            attempt_index=index,
        )
        request_path = _bound(repo_root, attempt.provider_request, label="Kimi-v4 provider request")
        if load_provider_request(request_path) != expected_request:
            raise LF022KimiV4RequalificationError("provider request differs from frozen input")
        wire_path = _bound(repo_root, attempt.wire_request, label="Kimi-v4 wire request")
        expected_wire = (
            canonical_json_bytes(
                make_chat_completion_payload(
                    model_id=contract.model_id,
                    rendered_prompt=rendered_prompt,
                    decoding=contract.decoding,
                )
            )
            + b"\n"
        )
        if wire_path.read_bytes() != expected_wire:
            raise LF022KimiV4RequalificationError("wire request differs from frozen contract")

        raw_path = _bound(repo_root, attempt.provider_raw, label="Kimi-v4 provider raw")
        try:
            provider_raw = ProviderRawResponse.model_validate_json(raw_path.read_bytes())
        except ValueError as exc:
            raise LF022KimiV4RequalificationError(f"invalid provider raw response: {exc}") from exc
        if (
            provider_raw.request_hash != expected_request.request_hash
            or provider_raw.attempt_id != expected_request.attempt_id
        ):
            raise LF022KimiV4RequalificationError("provider raw response differs from request")

        observed_status: KimiV4AttemptStatus
        observed_error: str | None = None
        observed_parsed = None
        observed_content: str | None = None
        if attempt.status == "transport_unknown":
            if (
                attempt.wire_response_body is not None
                or provider_raw.error_type != "transport_unknown"
            ):
                raise LF022KimiV4RequalificationError("transport-unknown lineage is inconsistent")
            observed_status = "transport_unknown"
            observed_error = "transport_unknown"
        else:
            assert attempt.wire_response_body is not None
            assert attempt.wire_response_metadata is not None
            body_path = _bound(
                repo_root,
                attempt.wire_response_body,
                label="Kimi-v4 wire response body",
            )
            metadata_path = _bound(
                repo_root,
                attempt.wire_response_metadata,
                label="Kimi-v4 wire response metadata",
            )
            metadata = _load_record(
                metadata_path,
                LF022KimiV4WireMetadata,
                label="Kimi-v4 wire response metadata",
            )
            if metadata.body_sha256 != hash_file(body_path):
                raise LF022KimiV4RequalificationError("wire metadata differs from body")
            response = RCPWireResponse(
                status_code=metadata.status_code,
                headers=metadata.headers,
                body=body_path.read_bytes(),
            )
            http_error = classify_http_response(
                response,
                policy=contract.retry_policy,
                now=attempt.completed_at,
            )
            if http_error is not None:
                observed_status = (
                    "retryable_http_error" if http_error.retryable else "terminal_http_error"
                )
                observed_error = http_error.code
            else:
                try:
                    completion = parse_chat_completion(
                        response.body,
                        expected_model=contract.model_id,
                    )
                    observed_content = completion.content
                    observed_parsed = parse_variant_proposer_output(completion.content)
                    request_contract = historical.prompt_request()
                    if len(observed_parsed.variants) != 1 or any(
                        item.intended_relation not in request_contract.requested_relations
                        for item in observed_parsed.variants
                    ):
                        raise VariantOutputParseError(
                            VariantOutputErrorCode.REQUEST_MISMATCH,
                            "Kimi-v4 output differs from the frozen request",
                        )
                    observed_status = "strict_variant_success"
                except RCPResponseError as exc:
                    observed_status = "invalid_response"
                    observed_error = exc.code
                except VariantOutputParseError as exc:
                    observed_status = "proposer_parse_failed"
                    observed_error = exc.code.value
        if attempt.status != observed_status or attempt.error_code != observed_error:
            raise LF022KimiV4RequalificationError("attempt status differs from raw response")
        if observed_parsed is not None:
            if attempt.parsed_variants is None or provider_raw.status != "success":
                raise LF022KimiV4RequalificationError("strict success lacks parsed artifacts")
            parsed_path = _bound(
                repo_root,
                attempt.parsed_variants,
                label="Kimi-v4 parsed variants",
            )
            expected_parsed = canonical_json_bytes(observed_parsed.model_dump(mode="json")) + b"\n"
            if (
                parsed_path.read_bytes() != expected_parsed
                or provider_raw.output_text != observed_content
            ):
                raise LF022KimiV4RequalificationError("parsed proposer artifact differs")
        elif (
            attempt.parsed_variants is not None
            or provider_raw.status != "error"
            or provider_raw.error_type != observed_error
        ):
            raise LF022KimiV4RequalificationError("failed attempt carries success artifacts")
        attempts.append((attempt, attempt_path))

    reconstructed = _terminal_from_attempts(
        repo_root=repo_root,
        selection=selection,
        task=task,
        attempts=attempts,
    )
    if reconstructed != terminal:
        raise LF022KimiV4RequalificationError("terminal differs from exact reconstructed lineage")
    return terminal


def _execute_task(
    *,
    repo_root: Path,
    selection: LF022KimiV4ChallengeSelection,
    contract: LF022KimiV4ChallengeContract,
    selected: LF022KimiV4SelectedChallengeItem,
    route_revision: str,
    execute_live: bool,
    credentials: KimiV4RuntimeCredentials | None,
    transport: RCPHTTPTransport | None,
    sleeper: Callable[[float], None],
    clock: Callable[[], datetime.datetime],
) -> tuple[LF022KimiV4TerminalRecord | None, Path | None, int]:
    run_root = _run_root(repo_root, selection)
    task_dir = _task_directory(run_root, selected.selection_rank)
    terminal_path = task_dir / "terminal.json"
    if terminal_path.exists():
        terminal = _verify_existing_terminal(
            repo_root=repo_root,
            selection=selection,
            contract=contract,
            selected=selected,
            route_revision=route_revision,
            terminal_path=terminal_path,
        )
        return terminal, terminal_path, 0

    record, historical, rendered_prompt, prompt_sha256 = _prepare_task(
        repo_root=repo_root,
        selection=selection,
        contract=contract,
        selected=selected,
    )
    task_dir.mkdir(parents=True, exist_ok=True)
    _write_immutable(task_dir / "task.json", _canonical(record))
    if not execute_live:
        return None, None, 0
    if credentials is None or transport is None:
        raise LF022KimiV4RequalificationError("live execution requires credentials and transport")
    if credentials.base_url.rstrip("/") != "https://inference.rcp.epfl.ch/v1":
        raise LF022KimiV4RequalificationError("RCP base URL differs from the reviewed endpoint")
    if not credentials.api_key:
        raise LF022KimiV4RequalificationError("RCP API key is empty")

    lock_path = task_dir / ".lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise LF022KimiV4RequalificationError("Kimi-v4 task is already locked") from exc
    attempts: list[tuple[LF022KimiV4AttemptRecord, Path]] = []
    network_calls = 0
    try:
        if terminal_path.exists():
            terminal = _verify_existing_terminal(
                repo_root=repo_root,
                selection=selection,
                contract=contract,
                selected=selected,
                route_revision=route_revision,
                terminal_path=terminal_path,
            )
            return terminal, terminal_path, 0
        for attempt_index in range(contract.retry_policy.max_attempts):
            attempt_dir = task_dir / "attempts" / f"{attempt_index:04d}"
            attempt_path = attempt_dir / "attempt.json"
            if attempt_path.exists():
                attempt = _load_record(
                    attempt_path, LF022KimiV4AttemptRecord, label="Kimi-v4 attempt"
                )
                attempts.append((attempt, attempt_path))
                if attempt.status != "retryable_http_error":
                    break
                continue

            request = _provider_request(
                record=record,
                historical=historical,
                rendered_prompt=rendered_prompt,
                prompt_sha256=prompt_sha256,
                contract=contract,
                route_revision=route_revision,
                attempt_index=attempt_index,
            )
            request_path = attempt_dir / "provider_request.json"
            persist_provider_request(request, request_path)
            wire_payload = make_chat_completion_payload(
                model_id=contract.model_id,
                rendered_prompt=rendered_prompt,
                decoding=contract.decoding,
            )
            wire_request_path = attempt_dir / "wire_request.json"
            _write_immutable(wire_request_path, canonical_json_bytes(wire_payload) + b"\n")
            started = clock()
            marker = attempt_dir / ".transport_started"
            response = _load_persisted_response(attempt_dir)
            if response is None and marker.exists():
                raw = persist_provider_raw_response(
                    task_dir / "provider_raw",
                    ProviderRawResponse.error(request, error_type="transport_unknown"),
                )
                attempt = LF022KimiV4AttemptRecord(
                    task_id=record.task_id,
                    attempt_index=attempt_index,
                    provider_request_hash=request.request_hash,
                    provider_attempt_id=request.attempt_id,
                    provider_request=_binding(repo_root, request_path),
                    wire_request=_binding(repo_root, wire_request_path),
                    provider_raw=_binding(repo_root, raw.raw_response_path),
                    status="transport_unknown",
                    retryable=False,
                    error_code="transport_unknown",
                    started_at=started,
                    completed_at=clock(),
                )
            elif response is None:
                _write_immutable(marker, b"started\n")
                try:
                    response = transport.post_json(
                        url=credentials.base_url.rstrip("/") + "/chat/completions",
                        api_key=credentials.api_key,
                        payload=wire_payload,
                        timeout_seconds=contract.retry_policy.request_timeout_seconds,
                    )
                    network_calls += 1
                except RCPTransportUnknownError:
                    raw = persist_provider_raw_response(
                        task_dir / "provider_raw",
                        ProviderRawResponse.error(request, error_type="transport_unknown"),
                    )
                    attempt = LF022KimiV4AttemptRecord(
                        task_id=record.task_id,
                        attempt_index=attempt_index,
                        provider_request_hash=request.request_hash,
                        provider_attempt_id=request.attempt_id,
                        provider_request=_binding(repo_root, request_path),
                        wire_request=_binding(repo_root, wire_request_path),
                        provider_raw=_binding(repo_root, raw.raw_response_path),
                        status="transport_unknown",
                        retryable=False,
                        error_code="transport_unknown",
                        started_at=started,
                        completed_at=clock(),
                    )
            if response is not None:
                body_path, metadata_path = _response_paths(task_dir=attempt_dir, response=response)
                http_error = classify_http_response(
                    response, policy=contract.retry_policy, now=clock()
                )
                completion = None
                parsed_binding = None
                error_code = None
                if http_error is not None:
                    error_code = http_error.code
                    status: KimiV4AttemptStatus = (
                        "retryable_http_error" if http_error.retryable else "terminal_http_error"
                    )
                else:
                    try:
                        completion = parse_chat_completion(
                            response.body, expected_model=contract.model_id
                        )
                        parsed = parse_variant_proposer_output(completion.content)
                        request_contract = historical.prompt_request()
                        if len(parsed.variants) != 1 or any(
                            item.intended_relation not in request_contract.requested_relations
                            for item in parsed.variants
                        ):
                            raise VariantOutputParseError(
                                VariantOutputErrorCode.REQUEST_MISMATCH,
                                "Kimi-v4 output differs from the frozen request",
                            )
                        parsed_path = attempt_dir / "parsed_variants.json"
                        _write_immutable(
                            parsed_path,
                            canonical_json_bytes(parsed.model_dump(mode="json")) + b"\n",
                        )
                        parsed_binding = _binding(repo_root, parsed_path)
                        status = "strict_variant_success"
                    except (RCPResponseError, VariantOutputParseError) as exc:
                        error_code = (
                            exc.code.value if isinstance(exc, VariantOutputParseError) else exc.code
                        )
                        status = (
                            "proposer_parse_failed"
                            if isinstance(exc, VariantOutputParseError)
                            else "invalid_response"
                        )
                raw_response = (
                    ProviderRawResponse.success(request, completion.content)
                    if completion is not None and parsed_binding is not None
                    else ProviderRawResponse.error(
                        request, error_type=error_code or "invalid_response"
                    )
                )
                raw = persist_provider_raw_response(task_dir / "provider_raw", raw_response)
                attempt = LF022KimiV4AttemptRecord(
                    task_id=record.task_id,
                    attempt_index=attempt_index,
                    provider_request_hash=request.request_hash,
                    provider_attempt_id=request.attempt_id,
                    provider_request=_binding(repo_root, request_path),
                    wire_request=_binding(repo_root, wire_request_path),
                    wire_response_body=_binding(repo_root, body_path),
                    wire_response_metadata=_binding(repo_root, metadata_path),
                    provider_raw=_binding(repo_root, raw.raw_response_path),
                    parsed_variants=parsed_binding,
                    status=status,
                    retryable=bool(http_error and http_error.retryable),
                    http_status=response.status_code,
                    retry_after_seconds=(
                        http_error.retry_after_seconds if http_error is not None else None
                    ),
                    error_code=error_code,
                    provider_request_id=(
                        completion.provider_request_id if completion is not None else None
                    ),
                    returned_model=(completion.returned_model if completion is not None else None),
                    finish_reason=(completion.finish_reason if completion is not None else None),
                    tokens=(completion.usage if completion is not None else {}),
                    started_at=started,
                    completed_at=clock(),
                )
            _write_immutable(attempt_path, _canonical(attempt))
            attempts.append((attempt, attempt_path))
            if attempt.status != "retryable_http_error":
                break
            if attempt_index + 1 < contract.retry_policy.max_attempts:
                sleeper(
                    retry_delay_seconds(
                        policy=contract.retry_policy,
                        attempt_index=attempt_index,
                        retry_after=attempt.retry_after_seconds,
                    )
                )
        terminal = _terminal_from_attempts(
            repo_root=repo_root,
            selection=selection,
            task=record,
            attempts=attempts,
        )
        _write_immutable(terminal_path, _canonical(terminal))
        return terminal, terminal_path, network_calls
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _qualification(
    *,
    repo_root: Path,
    selection: LF022KimiV4ChallengeSelection,
    terminals: tuple[LF022KimiV4TerminalRecord, ...],
    paths: tuple[Path, ...],
) -> tuple[LF022KimiV4QualificationRecord, Path]:
    status_counts = Counter(item.status for item in terminals)
    error_counts = Counter(item.error_code for item in terminals if item.error_code is not None)
    proof_repeat = sum(
        item.selection_rank in {6, 7} and item.error_code == "proof_bearing_candidate"
        for item in terminals
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "selection_id": selection.selection_id,
        "status": (
            "passed"
            if terminals[0].status == "strict_variant_success"
            and status_counts["strict_variant_success"] >= 14
            and error_counts["output_budget_exhausted"] == 0
            and error_counts["empty_response"] == 0
            and proof_repeat == 0
            else "failed"
        ),
        "terminals": [_binding(repo_root, path).model_dump(mode="json") for path in paths],
        "terminal_status_counts": dict(sorted(status_counts.items())),
        "terminal_error_counts": dict(sorted(error_counts.items())),
        "strict_parse_success_count": status_counts["strict_variant_success"],
        "output_budget_exhausted_count": error_counts["output_budget_exhausted"],
        "http_200_empty_response_count": error_counts["empty_response"],
        "prior_proof_bearing_repeat_count": proof_repeat,
        "capability_passed": terminals[0].status == "strict_variant_success",
        "minimum_strict_parse_successes": 14,
        "maximum_output_budget_exhausted": 0,
        "maximum_http_200_empty_responses": 0,
        "prior_proof_bearing_error_may_repeat": False,
        "production_admission_created": False,
        "promotion_enabled": False,
        "semantic_labels_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    record = LF022KimiV4QualificationRecord.model_validate(
        {
            **payload,
            "qualification_id": make_id("lf022_kimi_v4_qualification", payload),
        }
    )
    path = _run_root(repo_root, selection) / "qualification.json"
    _write_immutable(path, _canonical(record))
    return record, path


def run_verified_kimi_v4_requalification(
    *,
    repo_root: Path,
    selection: LF022KimiV4ChallengeSelection,
    stage: KimiV4Stage,
    execute_public_requalification: bool = False,
    credentials: KimiV4RuntimeCredentials | None = None,
    transport: RCPHTTPTransport | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], datetime.datetime] | None = None,
) -> LF022KimiV4StageResult:
    """Run one gated stage or replay every already-persisted result offline."""

    contract = _load_contract(repo_root, selection)
    route_revision = _load_route_revision(repo_root, selection)
    if stage == "replay" and execute_public_requalification:
        raise LF022KimiV4RequalificationError("replay mode cannot authorize network execution")
    indices = (
        (0,)
        if stage == "capability"
        else tuple(range(16))
        if stage == "replay"
        else tuple(range(1, 16))
    )
    run_root = _run_root(repo_root, selection)
    run_root.mkdir(parents=True, exist_ok=True)
    _write_immutable(
        run_root / "selection.json",
        canonical_json_bytes(selection.model_dump(mode="json")) + b"\n",
    )
    if stage == "remaining":
        capability_path = _task_directory(run_root, 0) / "terminal.json"
        if not capability_path.exists():
            raise LF022KimiV4RequalificationError(
                "remaining challenge is forbidden before capability terminal exists"
            )
        capability = _verify_existing_terminal(
            repo_root=repo_root,
            selection=selection,
            contract=contract,
            selected=selection.selected[0],
            route_revision=route_revision,
            terminal_path=capability_path,
        )
        if capability.status != "strict_variant_success":
            raise LF022KimiV4RequalificationError(
                "remaining challenge is forbidden because capability did not strictly pass"
            )

    sleep = sleeper or time.sleep
    now = clock or (lambda: datetime.datetime.now(tz=datetime.UTC))
    terminals: list[LF022KimiV4TerminalRecord] = []
    paths: list[Path] = []
    network_calls = 0
    for index in indices:
        terminal, terminal_path, calls = _execute_task(
            repo_root=repo_root,
            selection=selection,
            contract=contract,
            selected=selection.selected[index],
            route_revision=route_revision,
            execute_live=execute_public_requalification,
            credentials=credentials,
            transport=transport,
            sleeper=sleep,
            clock=now,
        )
        network_calls += calls
        if terminal is not None and terminal_path is not None:
            terminals.append(terminal)
            paths.append(terminal_path)
    qualification = None
    qualification_path = None
    all_paths = tuple(_task_directory(run_root, rank) / "terminal.json" for rank in range(16))
    if all(path.exists() for path in all_paths):
        all_terminals = tuple(
            _verify_existing_terminal(
                repo_root=repo_root,
                selection=selection,
                contract=contract,
                selected=selection.selected[rank],
                route_revision=route_revision,
                terminal_path=path,
            )
            for rank, path in enumerate(all_paths)
        )
        qualification, qualification_path = _qualification(
            repo_root=repo_root,
            selection=selection,
            terminals=all_terminals,
            paths=all_paths,
        )
    return LF022KimiV4StageResult(
        stage=stage,
        terminals=tuple(terminals),
        terminal_paths=tuple(paths),
        network_calls_this_run=network_calls,
        qualification=qualification,
        qualification_path=qualification_path,
    )


__all__ = [
    "KimiV4RuntimeCredentials",
    "LF022KimiV4AttemptRecord",
    "LF022KimiV4QualificationRecord",
    "LF022KimiV4RequalificationError",
    "LF022KimiV4StageResult",
    "LF022KimiV4TaskRecord",
    "LF022KimiV4TerminalRecord",
    "run_verified_kimi_v4_requalification",
]
