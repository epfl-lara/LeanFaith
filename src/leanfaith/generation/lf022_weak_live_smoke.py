"""Fail-closed one-pair live RCP smoke for LF-022 weak judges.

The existing :mod:`leanfaith.generation.lf022_weak_batch` foundation prepares
four blinded judge cells per candidate but deliberately authorizes no network
calls.  This module adds a *separate* role-specific authorization boundary for
one selected public candidate.  It never edits or subsets the parent candidate
inventory and it can execute at most the selected pair's four AB/BA cells.

The smoke is qualification evidence only.  It creates unresolved judge-call
lineage, not semantic labels, silver/gold records, training/evaluation data, or
Gate credit.  Runtime credentials are accepted only as in-memory values and
are never serialized.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.code_bundle import validate_code_bundle
from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_execution import LF022RCPRetryPolicy
from leanfaith.generation.lf022_production import (
    LF022ProductionFamilyMatrix,
    LF022ProviderCatalogSnapshot,
)
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateManifest,
)
from leanfaith.generation.lf022_weak_batch import (
    BoundArtifact,
    LF022WeakBatchError,
    LF022WeakBatchSpec,
    LF022WeakDispatchManifest,
    LF022WeakDispatchRecord,
    LF022WeakTerminalRecord,
    _endpoint,
    _load_prepared_batch,
    _verify_dispatch_request,
)
from leanfaith.generation.providers import (
    ProviderIdentity,
    ProviderRawResponse,
    ProviderRequest,
    bridge_provider_result_to_generic_llm_lineage,
    load_provider_raw_response,
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
)
from leanfaith.generation.weak_supervision import (
    JUDGE_TEMPLATE_ID,
    FamilySeparationMatrix,
    JudgeOutputParseError,
    JudgeSlot,
    build_weak_consensus_candidate,
    judge_provider_input_ids,
    materialize_verified_judgment_evidence,
    parse_blinded_judge_output,
    render_blinded_judge_prompt,
)
from leanfaith.schemas.enums import LLMRole, ParseStatus
from leanfaith.schemas.evidence import EvidenceRecord
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.llm import check_llm_call_attempt_lineage
from leanfaith.schemas.manifest import collect_code_state

_RCP_BASE_URL = "https://inference.rcp.epfl.ch/v1"
_METHOD: Literal["lf022_weak_live_smoke_v1"] = "lf022_weak_live_smoke_v1"
_KIMI_FAMILY = "moonshot_kimi_k2"
_KIMI_MODEL = "moonshotai/Kimi-K2.7-Code"
_DEEPSEEK_FAMILY = "deepseek_v4"
_DEEPSEEK_MODEL = "deepseek-ai/DeepSeek-V4-Pro"
_QWEN_PROPOSER_FAMILY = "qwen3"
_QWEN_PROPOSER_MODEL = "Qwen/Qwen3.5-397B-A17B"


class LF022WeakLiveSmokeError(RuntimeError):
    """A selector, admission, artifact, or execution invariant failed."""


@dataclass(frozen=True, slots=True, repr=False)
class LF022WeakRuntimeCredentials:
    """Runtime-only RCP credentials; the API key is never serialized."""

    base_url: str
    api_key: str

    def __repr__(self) -> str:
        return f"LF022WeakRuntimeCredentials(base_url={self.base_url!r}, api_key='<redacted>')"


class LF022WeakJudgeDecodingContract(StrictModel):
    """Exact reviewed wire fields for the two one-pair smoke judge routes."""

    schema_version: Literal[1] = 1
    contract_id: Literal[
        "kimi_k2_7_weak_judge_smoke_v1",
        "deepseek_v4_weak_judge_smoke_v1",
    ]
    temperature: float = Field(ge=0.0, le=2.0)
    top_p: float = Field(gt=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0, strict=True)
    min_p: float | None = Field(default=None, ge=0.0, le=1.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    repetition_penalty: float | None = Field(default=None, ge=0.0)
    max_tokens: int = Field(ge=1, le=65_536, strict=True)
    seed: int | None = Field(default=None, ge=0, strict=True)
    stream: Literal[False] = False
    reasoning_effort: Literal["high"] = "high"
    chat_template_enable_thinking: Literal[True] = True

    @model_validator(mode="after")
    def _exact_reviewed_contract(self) -> Self:
        shared: dict[str, object] = {
            "top_k": None,
            "min_p": None,
            "presence_penalty": None,
            "repetition_penalty": None,
            "max_tokens": 8192,
            "seed": 42,
            "stream": False,
            "reasoning_effort": "high",
            "chat_template_enable_thinking": True,
        }
        expected = {
            "kimi_k2_7_weak_judge_smoke_v1": {
                **shared,
                "temperature": 1.0,
                "top_p": 0.95,
            },
            "deepseek_v4_weak_judge_smoke_v1": {
                **shared,
                "temperature": 0.0,
                "top_p": 1.0,
            },
        }[self.contract_id]
        observed = self.model_dump(mode="json", exclude={"schema_version", "contract_id"})
        if observed != expected:
            raise ValueError(f"decoding differs from exact judge contract {self.contract_id!r}")
        return self

    def provider_decoding(self) -> dict[str, str | int | float | bool | None]:
        return {
            "contract_id": self.contract_id,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "presence_penalty": self.presence_penalty,
            "repetition_penalty": self.repetition_penalty,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stream": self.stream,
            "reasoning_effort": self.reasoning_effort,
            "chat_template_enable_thinking": self.chat_template_enable_thinking,
        }

    def wire_fields(self) -> dict[str, object]:
        result: dict[str, object] = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "stream": self.stream,
            "reasoning_effort": self.reasoning_effort,
            "chat_template_kwargs": {
                "enable_thinking": self.chat_template_enable_thinking,
            },
        }
        for field in ("top_k", "min_p", "presence_penalty", "repetition_penalty", "seed"):
            value = getattr(self, field)
            if value is not None:
                result[field] = value
        return result


class LF022WeakJudgeRouteClaim(StrictModel):
    """Explicit one-pair route admission, not a scale judge qualification."""

    schema_version: Literal[1] = 1
    claim_id: str = Field(pattern=id_pattern("lf022_weak_judge_claim"))
    role: Literal["judge"] = "judge"
    provider: Literal["epfl_rcp"] = "epfl_rcp"
    model_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    production_matrix_revision: str = Field(pattern=r"^provider-deployment-snapshot:[0-9a-f]{64}$")
    production_matrix_catalog_artifact: BoundArtifact
    rcp_catalog_revision: str = Field(pattern=r"^rcp-catalog-sha256:[0-9a-f]{64}$")
    raw_rcp_catalog_artifact: BoundArtifact
    decoding: LF022WeakJudgeDecodingContract
    judge_prompt_sha256: str = Field(pattern=HEX64_PATTERN)
    qualification_scope: Literal["one_pair_four_cell_weak_judge_smoke"] = (
        "one_pair_four_cell_weak_judge_smoke"
    )
    smoke_route_admitted: Literal[True] = True
    scale_judge_qualified: Literal[False] = False
    private_source_content_allowed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        matrix_catalog_sha = self.production_matrix_catalog_artifact.sha256
        raw_catalog_sha = self.raw_rcp_catalog_artifact.sha256
        if self.production_matrix_revision != (
            f"provider-deployment-snapshot:{matrix_catalog_sha}"
        ):
            raise ValueError("production-matrix revision differs from normalized catalog")
        if self.rcp_catalog_revision != f"rcp-catalog-sha256:{raw_catalog_sha}":
            raise ValueError("RCP catalog revision differs from raw /models artifact")
        expected = make_id(
            "lf022_weak_judge_claim",
            self.model_dump(mode="json", exclude={"claim_id"}),
        )
        if self.claim_id != expected:
            raise ValueError("judge qualification claim ID differs from claim content")
        return self


class LF022WeakLiveSmokeConfig(StrictModel):
    """Pinned inputs for selecting and admitting one live weak-judge smoke."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_weak_live_smoke_v1"] = _METHOD
    selection_rule: Literal["lowest_candidate_inventory_record_id"] = (
        "lowest_candidate_inventory_record_id"
    )
    parent_batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    parent_batch_spec_sha256: str = Field(pattern=HEX64_PATTERN)
    parent_dispatch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    parent_inventory_id: str = Field(pattern=id_pattern("lf022_supervision_inventory"))
    parent_candidate_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    parent_candidate_records_sha256: str = Field(pattern=HEX64_PATTERN)
    production_family_matrix_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_a_claim: BoundArtifact
    judge_b_claim: BoundArtifact
    code_bundle: BoundArtifact
    code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    producer_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    retry_policy: LF022RCPRetryPolicy
    maximum_selected_pairs: Literal[1] = 1
    maximum_network_calls: Literal[4] = 4
    concurrency: Literal[1] = 1
    explicit_live_flag_required: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _single_attempt(self) -> Self:
        if self.retry_policy.max_attempts != 1:
            raise ValueError("one-pair weak-judge smoke freezes max_attempts=1")
        return self


@dataclass(frozen=True, slots=True)
class LF022WeakLiveSmokeFreezeResult:
    """Paths and hashes emitted by the deterministic offline freeze."""

    judge_a_claim_path: Path
    judge_b_claim_path: Path
    config_path: Path
    config_sha256: str


@dataclass(frozen=True, slots=True)
class LF022WeakJudgeRouteSpec:
    """Read-only exact judge route used by batch authoring and live admission."""

    family_id: str
    model_id: str
    decoding: LF022WeakJudgeDecodingContract


class LF022WeakSmokeSelector(StrictModel):
    """Content-addressed deterministic selection from an immutable inventory."""

    schema_version: Literal[1] = 1
    selector_id: str = Field(pattern=id_pattern("lf022_weak_selector"))
    method_version: Literal["lf022_weak_live_smoke_v1"] = _METHOD
    selection_rule: Literal["lowest_candidate_inventory_record_id"]
    parent_inventory_id: str = Field(pattern=id_pattern("lf022_supervision_inventory"))
    parent_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    parent_records_sha256: str = Field(pattern=HEX64_PATTERN)
    eligible_candidate_count: int = Field(ge=1, strict=True)
    selected_candidate_inventory_record_id: str = Field(
        pattern=id_pattern("lf022_supervision_candidate")
    )
    selected_pair_id: str = Field(pattern=id_pattern("pair"))
    selected_record_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_pair_count: Literal[1] = 1
    required_dispatch_cell_count: Literal[4] = 4
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        expected = make_id(
            "lf022_weak_selector",
            self.model_dump(mode="json", exclude={"selector_id"}),
        )
        if self.selector_id != expected:
            raise ValueError("selector ID differs from selector content")
        return self


class LF022WeakLiveAdmission(StrictModel):
    """Network authorization for exactly one selector and four prepared cells."""

    schema_version: Literal[1] = 1
    admission_id: str = Field(pattern=id_pattern("lf022_weak_live_admission"))
    method_version: Literal["lf022_weak_live_smoke_v1"] = _METHOD
    config_sha256: str = Field(pattern=HEX64_PATTERN)
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    dispatch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    batch_spec_sha256: str = Field(pattern=HEX64_PATTERN)
    selector: LF022WeakSmokeSelector
    judge_a_claim_id: str = Field(pattern=id_pattern("lf022_weak_judge_claim"))
    judge_b_claim_id: str = Field(pattern=id_pattern("lf022_weak_judge_claim"))
    code_bundle_sha256: str = Field(pattern=HEX64_PATTERN)
    code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    producer_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    retry_policy: LF022RCPRetryPolicy
    allowed_dispatch_cell_ids: tuple[str, str, str, str]
    maximum_network_calls: Literal[4] = 4
    concurrency: Literal[1] = 1
    public_source_only: Literal[True] = True
    private_source_content: Literal[False] = False
    live_execution_requires_explicit_flag: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if self.retry_policy.max_attempts != 1:
            raise ValueError("live smoke admission requires exactly one attempt per cell")
        if tuple(sorted(set(self.allowed_dispatch_cell_ids))) != self.allowed_dispatch_cell_ids:
            raise ValueError("allowed dispatch cells must be sorted and unique")
        expected = make_id(
            "lf022_weak_live_admission",
            self.model_dump(mode="json", exclude={"admission_id"}),
        )
        if self.admission_id != expected:
            raise ValueError("live admission ID differs from admission content")
        return self


AttemptStatus = Literal[
    "response_received",
    "invalid_response",
    "retryable_http_error",
    "terminal_http_error",
    "transport_unknown",
]


class LF022WeakLiveAttempt(StrictModel):
    """Raw-first artifacts for the smoke's single attempt at one judge cell."""

    schema_version: Literal[1] = 1
    admission_id: str = Field(pattern=id_pattern("lf022_weak_live_admission"))
    dispatch_cell_id: str = Field(pattern=id_pattern("lf022_weak_cell"))
    request_artifact: str = Field(min_length=1)
    request_sha256: str = Field(pattern=HEX64_PATTERN)
    wire_request_artifact: str = Field(min_length=1)
    wire_request_sha256: str = Field(pattern=HEX64_PATTERN)
    wire_response_body_artifact: str | None = None
    wire_response_body_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    wire_response_metadata_artifact: str | None = None
    wire_response_metadata_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    provider_raw_artifact: str = Field(min_length=1)
    provider_raw_sha256: str = Field(pattern=HEX64_PATTERN)
    status: AttemptStatus
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_code: str | None = None
    started_at: datetime.datetime
    completed_at: datetime.datetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        response_fields = (
            self.wire_response_body_artifact,
            self.wire_response_body_sha256,
            self.wire_response_metadata_artifact,
            self.wire_response_metadata_sha256,
        )
        if any(item is None for item in response_fields) != all(
            item is None for item in response_fields
        ):
            raise ValueError("wire response bindings must be all present or all absent")
        if self.status == "transport_unknown":
            if self.http_status is not None or response_fields[0] is not None:
                raise ValueError("transport_unknown cannot carry a wire response")
        elif response_fields[0] is None or self.http_status is None:
            raise ValueError("non-transport attempts require a persisted wire response")
        if self.status == "response_received" and self.error_code is not None:
            raise ValueError("response_received cannot carry an error code")
        if self.status != "response_received" and not self.error_code:
            raise ValueError("failed attempt requires an error code")
        return self


class LF022WeakWireMetadata(StrictModel):
    """Persisted HTTP metadata bound to exact response-body bytes."""

    schema_version: Literal[1] = 1
    status_code: int = Field(ge=100, le=599, strict=True)
    headers: dict[str, str]
    body_sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _sorted_headers(self) -> Self:
        if list(self.headers) != sorted(self.headers):
            raise ValueError("wire response headers must be sorted")
        return self


class LF022WeakLiveSmokeTerminal(StrictModel):
    """One selected cell's exact live attempt plus generic judge lineage."""

    schema_version: Literal[1] = 1
    terminal_id: str = Field(pattern=id_pattern("lf022_weak_live_terminal"))
    admission_id: str = Field(pattern=id_pattern("lf022_weak_live_admission"))
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    dispatch_cell_id: str = Field(pattern=id_pattern("lf022_weak_cell"))
    live_attempt: LF022WeakLiveAttempt
    weak_terminal: LF022WeakTerminalRecord
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if (
            self.live_attempt.admission_id != self.admission_id
            or self.live_attempt.dispatch_cell_id != self.dispatch_cell_id
            or self.weak_terminal.batch_id != self.batch_id
            or self.weak_terminal.dispatch_cell_id != self.dispatch_cell_id
        ):
            raise ValueError("live terminal lineage differs from its cell/admission")
        expected = make_id(
            "lf022_weak_live_terminal",
            self.model_dump(mode="json", exclude={"terminal_id"}),
        )
        if self.terminal_id != expected:
            raise ValueError("live terminal ID differs from terminal content")
        return self


class LF022WeakLiveSmokeManifest(StrictModel):
    """Complete four-cell smoke summary; never a semantic-label manifest."""

    schema_version: Literal[1] = 1
    execution_id: str = Field(pattern=id_pattern("lf022_weak_live_execution"))
    admission_id: str = Field(pattern=id_pattern("lf022_weak_live_admission"))
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    producer_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    terminals_artifact: Literal["live_smoke/terminals.jsonl"] = "live_smoke/terminals.jsonl"
    terminals_sha256: str = Field(pattern=HEX64_PATTERN)
    terminal_count: Literal[4] = 4
    status_counts: dict[str, int]
    transport_attempt_count: Literal[4] = 4
    evidence_artifact: Literal["live_smoke/judgment_evidence.jsonl"] = (
        "live_smoke/judgment_evidence.jsonl"
    )
    evidence_sha256: str = Field(pattern=HEX64_PATTERN)
    parsed_evidence_count: int = Field(ge=0, le=4, strict=True)
    weak_candidates_artifact: Literal["live_smoke/weak_consensus_candidates.jsonl"] = (
        "live_smoke/weak_consensus_candidates.jsonl"
    )
    weak_candidates_sha256: str = Field(pattern=HEX64_PATTERN)
    weak_candidate_count: Literal[1] = 1
    all_selected_cells_terminal: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if sum(self.status_counts.values()) != self.terminal_count:
            raise ValueError("live smoke terminal status counts do not reconcile")
        expected = make_id(
            "lf022_weak_live_execution",
            self.model_dump(mode="json", exclude={"execution_id"}),
        )
        if self.execution_id != expected:
            raise ValueError("live smoke execution ID differs from manifest content")
        return self


def _canonical(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _jsonl(models: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical(model) for model in models)


def _reject_symlink_components(path: Path, *, label: str) -> None:
    """Reject an existing symlink at any component, including the trusted root."""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LF022WeakLiveSmokeError(f"{label} contains a symlink component: {current}")


def _immutable(path: Path, payload: bytes, *, label: str) -> str:
    _reject_symlink_components(path, label=f"immutable {label} path")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise LF022WeakLiveSmokeError(f"immutable {label} conflicts at {path}")
        return hash_file(path)
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
                raise LF022WeakLiveSmokeError(
                    f"concurrent immutable {label} conflict at {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_model(path: Path, model_type: type[StrictModel]) -> StrictModel:
    _reject_symlink_components(path, label=model_type.__name__)
    if path.is_symlink() or not path.is_file():
        raise LF022WeakLiveSmokeError(f"missing or unsafe artifact: {path}")
    raw = path.read_bytes()
    try:
        model = model_type.model_validate_json(raw)
    except ValueError as exc:
        raise LF022WeakLiveSmokeError(f"invalid {model_type.__name__}: {path}: {exc}") from exc
    if raw not in {_canonical(model), _canonical(model).rstrip(b"\n")}:
        raise LF022WeakLiveSmokeError(f"artifact is not canonical JSON: {path}")
    return model


def _resolve(binding: BoundArtifact, *, root: Path) -> Path:
    _reject_symlink_components(root, label="bound-artifact root")
    path = Path(binding.path)
    if not path.is_absolute():
        path = root / path
    _reject_symlink_components(path, label="bound artifact")
    if path.is_symlink() or not path.is_file() or hash_file(path) != binding.sha256:
        raise LF022WeakLiveSmokeError(f"bound artifact is absent or drifted: {path}")
    return path


def _relative(path: Path, *, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise LF022WeakLiveSmokeError(f"artifact escapes batch root: {path}") from exc


def _record_sha(record: StrictModel) -> str:
    return sha256_hex(_canonical(record))


def lf022_weak_judge_route_for_slot(slot: JudgeSlot) -> LF022WeakJudgeRouteSpec:
    """Return the single reviewed judge deployment/decoding for one slot."""

    shared: dict[str, object] = {
        "top_k": None,
        "min_p": None,
        "presence_penalty": None,
        "repetition_penalty": None,
        "max_tokens": 8192,
        "seed": 42,
        "stream": False,
        "reasoning_effort": "high",
        "chat_template_enable_thinking": True,
    }
    if slot == "judge_A":
        decoding = LF022WeakJudgeDecodingContract.model_validate(
            {
                **shared,
                "contract_id": "kimi_k2_7_weak_judge_smoke_v1",
                "temperature": 1.0,
                "top_p": 0.95,
            }
        )
        return LF022WeakJudgeRouteSpec(
            family_id=_KIMI_FAMILY,
            model_id=_KIMI_MODEL,
            decoding=decoding,
        )
    decoding = LF022WeakJudgeDecodingContract.model_validate(
        {
            **shared,
            "contract_id": "deepseek_v4_weak_judge_smoke_v1",
            "temperature": 0.0,
            "top_p": 1.0,
        }
    )
    return LF022WeakJudgeRouteSpec(
        family_id=_DEEPSEEK_FAMILY,
        model_id=_DEEPSEEK_MODEL,
        decoding=decoding,
    )


def _repo_relative(path: Path, *, repo_root: Path, label: str) -> str:
    _reject_symlink_components(repo_root, label="repository root")
    _reject_symlink_components(path, label=label)
    if path.is_symlink():
        raise LF022WeakLiveSmokeError(f"{label} cannot be a symlink")
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError as exc:
        raise LF022WeakLiveSmokeError(f"{label} must be inside the repository") from exc


def _raw_catalog_model_ids(path: Path) -> set[str]:
    _reject_symlink_components(path, label="raw RCP catalog")
    if path.is_symlink() or not path.is_file():
        raise LF022WeakLiveSmokeError(f"missing or unsafe raw RCP catalog: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document["data"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LF022WeakLiveSmokeError("invalid raw RCP /models catalog") from exc
    if not isinstance(rows, list):
        raise LF022WeakLiveSmokeError("raw RCP /models data must be a list")
    return {row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}


def freeze_lf022_weak_live_smoke_inputs(
    *,
    repo_root: Path,
    batch_root: Path,
    production_catalog_path: Path,
    raw_rcp_catalog_path: Path,
    code_bundle_path: Path,
    output_dir: Path,
) -> LF022WeakLiveSmokeFreezeResult:
    """Freeze exact Kimi/DeepSeek smoke claims and config with zero network I/O.

    This is the only supported authoring path for live-smoke claims/config.  It
    derives judge routes from the already prepared weak batch and refuses a
    dirty code tree so the later admission is reproducible rather than a
    hand-authored JSON assertion.
    """

    _reject_symlink_components(repo_root, label="repository root")
    _reject_symlink_components(batch_root, label="weak batch root")
    _reject_symlink_components(output_dir, label="live-smoke freeze output")
    code_state = collect_code_state(repo_root)
    if code_state.git_dirty:
        raise LF022WeakLiveSmokeError("weak live-smoke freeze requires a clean current code tree")
    if code_state.code_tree_hash is None:
        raise LF022WeakLiveSmokeError("current code tree has no content-addressed hash")
    try:
        bundle_sha = validate_code_bundle(code_bundle_path, code_state.code_tree_hash)
    except (OSError, ValueError) as exc:
        raise LF022WeakLiveSmokeError(f"invalid current code bundle: {exc}") from exc

    try:
        spec, dispatch_manifest, dispatches, _ = _load_prepared_batch(batch_root)
    except LF022WeakBatchError as exc:
        raise LF022WeakLiveSmokeError(f"prepared weak batch is invalid: {exc}") from exc
    candidate_manifest_model = _load_model(
        batch_root / "inputs/candidate_manifest.json",
        LF022SupervisionCandidateManifest,
    )
    assert isinstance(candidate_manifest_model, LF022SupervisionCandidateManifest)
    candidate_manifest = candidate_manifest_model
    if (
        candidate_manifest.proposer_family_id != _QWEN_PROPOSER_FAMILY
        or candidate_manifest.proposer_model != _QWEN_PROPOSER_MODEL
        or dispatch_manifest.proposer_family_id != _QWEN_PROPOSER_FAMILY
    ):
        raise LF022WeakLiveSmokeError(
            "live weak smoke is bound to the exact Qwen supervision inventory"
        )

    catalog_model = _load_model(
        production_catalog_path,
        LF022ProviderCatalogSnapshot,
    )
    assert isinstance(catalog_model, LF022ProviderCatalogSnapshot)
    if catalog_model.provider_id != "epfl_rcp":
        raise LF022WeakLiveSmokeError("normalized catalog is not the EPFL RCP catalog")
    catalog_models = {item.model_id for item in catalog_model.deployments}
    raw_models = _raw_catalog_model_ids(raw_rcp_catalog_path)
    required_models = {_KIMI_MODEL, _DEEPSEEK_MODEL}
    if not required_models.issubset(catalog_models) or not required_models.issubset(raw_models):
        raise LF022WeakLiveSmokeError(
            "normalized and raw catalogs must both contain exact Kimi and DeepSeek routes"
        )

    matrix_model = _load_model(
        batch_root / "inputs/production_family_matrix.json",
        LF022ProductionFamilyMatrix,
    )
    assert isinstance(matrix_model, LF022ProductionFamilyMatrix)
    production_sha = hash_file(production_catalog_path)
    judge_slots: tuple[JudgeSlot, JudgeSlot] = ("judge_A", "judge_B")
    expected_routes: dict[JudgeSlot, LF022WeakJudgeRouteSpec] = {
        slot: lf022_weak_judge_route_for_slot(slot) for slot in judge_slots
    }
    for slot, route in expected_routes.items():
        family_id = route.family_id
        model_id = route.model_id
        endpoint = _endpoint(spec, slot)
        decoding = route.decoding
        pin = matrix_model.pins_by_id.get(family_id)
        if (
            endpoint.provider != "epfl_rcp"
            or endpoint.family_id != family_id
            or endpoint.model != model_id
            or endpoint.revision != f"provider-deployment-snapshot:{production_sha}"
            or endpoint.decoding != decoding.provider_decoding()
            or pin is None
            or family_id not in matrix_model.judge_family_ids
            or pin.provider_id != "epfl_rcp"
            or pin.model_id != model_id
            or pin.provider_catalog_artifact is None
            or pin.provider_catalog_artifact.sha256 != production_sha
        ):
            raise LF022WeakLiveSmokeError(
                f"prepared {slot} is not the exact reviewed {family_id} judge route"
            )
    if dispatch_manifest.primary_eval_family_id in {
        _KIMI_FAMILY,
        _DEEPSEEK_FAMILY,
    }:
        raise LF022WeakLiveSmokeError("held-out evaluator overlaps the smoke judges")
    prompt_hashes = {item.prompt_template_sha256 for item in dispatches}
    if len(prompt_hashes) != 1:
        raise LF022WeakLiveSmokeError("prepared batch contains multiple judge templates")
    prompt_sha = next(iter(prompt_hashes))

    # All frozen inputs are copied below one repository-local root.  This makes
    # the freeze deterministic and prevents later source-file replacement.
    output_relative = _repo_relative(
        output_dir,
        repo_root=repo_root,
        label="live-smoke freeze output",
    )
    del output_relative
    production_copy = output_dir / "inputs/production_catalog.json"
    raw_copy = output_dir / "inputs/raw_rcp_catalog.json"
    bundle_copy = output_dir / "inputs/code_bundle.tar.gz"
    _immutable(
        production_copy,
        production_catalog_path.read_bytes(),
        label="frozen normalized production catalog",
    )
    _immutable(
        raw_copy,
        raw_rcp_catalog_path.read_bytes(),
        label="frozen raw RCP catalog",
    )
    _immutable(bundle_copy, code_bundle_path.read_bytes(), label="frozen code bundle")
    if hash_file(bundle_copy) != bundle_sha:
        raise LF022WeakLiveSmokeError("copied code bundle differs from validated bundle")

    raw_sha = hash_file(raw_copy)
    claim_paths: dict[JudgeSlot, Path] = {
        "judge_A": output_dir / "judge_A_claim.json",
        "judge_B": output_dir / "judge_B_claim.json",
    }
    claims: dict[JudgeSlot, LF022WeakJudgeRouteClaim] = {}
    for slot, route in expected_routes.items():
        family_id = route.family_id
        model_id = route.model_id
        values: dict[str, object] = {
            "schema_version": 1,
            "role": "judge",
            "provider": "epfl_rcp",
            "model_id": model_id,
            "family_id": family_id,
            "production_matrix_revision": (f"provider-deployment-snapshot:{production_sha}"),
            "production_matrix_catalog_artifact": {
                "path": "inputs/production_catalog.json",
                "sha256": production_sha,
            },
            "rcp_catalog_revision": f"rcp-catalog-sha256:{raw_sha}",
            "raw_rcp_catalog_artifact": {
                "path": "inputs/raw_rcp_catalog.json",
                "sha256": raw_sha,
            },
            "decoding": route.decoding.model_dump(mode="json"),
            "judge_prompt_sha256": prompt_sha,
            "qualification_scope": "one_pair_four_cell_weak_judge_smoke",
            "smoke_route_admitted": True,
            "scale_judge_qualified": False,
            "private_source_content_allowed": False,
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "training_eligible": False,
            "evaluation_eligible": False,
            "gate_credit_claimed": False,
        }
        claim = LF022WeakJudgeRouteClaim.model_validate(
            {
                **values,
                "claim_id": make_id("lf022_weak_judge_claim", values),
            }
        )
        claims[slot] = claim
        _immutable(
            claim_paths[slot],
            _canonical(claim),
            label=f"frozen {slot} route claim",
        )

    config = LF022WeakLiveSmokeConfig(
        parent_batch_id=dispatch_manifest.batch_id,
        parent_batch_spec_sha256=hash_file(batch_root / "batch_spec.json"),
        parent_dispatch_manifest_sha256=hash_file(batch_root / "dispatch_manifest.json"),
        parent_inventory_id=candidate_manifest.inventory_id,
        parent_candidate_manifest_sha256=hash_file(batch_root / "inputs/candidate_manifest.json"),
        parent_candidate_records_sha256=hash_file(batch_root / "inputs/candidate_records.jsonl"),
        production_family_matrix_sha256=hash_file(
            batch_root / "inputs/production_family_matrix.json"
        ),
        judge_a_claim=BoundArtifact(
            path="judge_A_claim.json",
            sha256=hash_file(claim_paths["judge_A"]),
        ),
        judge_b_claim=BoundArtifact(
            path="judge_B_claim.json",
            sha256=hash_file(claim_paths["judge_B"]),
        ),
        code_bundle=BoundArtifact(
            path=_repo_relative(
                bundle_copy,
                repo_root=repo_root,
                label="frozen code bundle",
            ),
            sha256=bundle_sha,
        ),
        code_tree_hash=code_state.code_tree_hash,
        producer_commit=code_state.git_revision,
        retry_policy=LF022RCPRetryPolicy(
            max_attempts=1,
            request_timeout_seconds=60,
            base_delay_seconds=0.0,
            maximum_delay_seconds=0.0,
            retryable_http_statuses=(408, 429, 500, 502, 503, 504),
        ),
    )
    config_path = output_dir / "live_smoke_config.json"
    config_sha = _immutable(
        config_path,
        _canonical(config),
        label="frozen live-smoke config",
    )
    return LF022WeakLiveSmokeFreezeResult(
        judge_a_claim_path=claim_paths["judge_A"],
        judge_b_claim_path=claim_paths["judge_B"],
        config_path=config_path,
        config_sha256=config_sha,
    )


def _claim_for_slot(
    *,
    batch_root: Path,
    config_root: Path,
    binding: BoundArtifact,
    slot: JudgeSlot,
    spec: LF022WeakBatchSpec,
    dispatches: Sequence[LF022WeakDispatchRecord],
) -> LF022WeakJudgeRouteClaim:
    path = _resolve(binding, root=config_root)
    model = _load_model(path, LF022WeakJudgeRouteClaim)
    assert isinstance(model, LF022WeakJudgeRouteClaim)
    claim = model
    normalized_catalog_path = _resolve(
        claim.production_matrix_catalog_artifact,
        root=config_root,
    )
    raw_catalog_path = _resolve(claim.raw_rcp_catalog_artifact, root=config_root)
    normalized_model = _load_model(
        normalized_catalog_path,
        LF022ProviderCatalogSnapshot,
    )
    assert isinstance(normalized_model, LF022ProviderCatalogSnapshot)
    if normalized_model.provider_id != claim.provider or claim.model_id not in {
        item.model_id for item in normalized_model.deployments
    }:
        raise LF022WeakLiveSmokeError(f"{slot} model is absent from normalized production catalog")
    try:
        raw_document = json.loads(raw_catalog_path.read_text(encoding="utf-8"))
        raw_entries = raw_document["data"]
        raw_model_ids = {
            item["id"]
            for item in raw_entries
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LF022WeakLiveSmokeError(f"invalid raw RCP catalog for {slot}") from exc
    if not isinstance(raw_entries, list) or claim.model_id not in raw_model_ids:
        raise LF022WeakLiveSmokeError(f"{slot} model is absent from raw RCP /models body")

    endpoint = _endpoint(spec, slot)
    slot_dispatches = tuple(item for item in dispatches if item.judge_slot == slot)
    if not slot_dispatches:
        raise LF022WeakLiveSmokeError(f"selector has no dispatches for {slot}")
    if (
        endpoint.provider != claim.provider
        or endpoint.model != claim.model_id
        or endpoint.family_id != claim.family_id
        or endpoint.revision != claim.production_matrix_revision
        or endpoint.decoding != claim.decoding.provider_decoding()
    ):
        raise LF022WeakLiveSmokeError(
            f"{slot} route claim differs from the exact prepared judge endpoint"
        )
    if any(item.judge_family_id != claim.family_id for item in slot_dispatches):
        raise LF022WeakLiveSmokeError(f"{slot} dispatch family differs from route claim")
    expected_prompt_sha = render_blinded_judge_prompt(slot_dispatches[0].task).template_sha256
    if claim.judge_prompt_sha256 != expected_prompt_sha:
        raise LF022WeakLiveSmokeError(f"{slot} claim binds a different judge prompt")

    matrix_model = _load_model(
        batch_root / "inputs/production_family_matrix.json",
        LF022ProductionFamilyMatrix,
    )
    assert isinstance(matrix_model, LF022ProductionFamilyMatrix)
    pin = matrix_model.pins_by_id.get(claim.family_id)
    if (
        pin is None
        or claim.family_id not in matrix_model.judge_family_ids
        or pin.provider_id != claim.provider
        or pin.model_id != claim.model_id
        or pin.provider_catalog_artifact is None
        or pin.provider_catalog_artifact.sha256 != claim.production_matrix_catalog_artifact.sha256
    ):
        raise LF022WeakLiveSmokeError(
            f"{slot} claim is not the exact judge-role deployment in the family matrix"
        )
    return claim


def prepare_lf022_weak_live_smoke(
    *,
    repo_root: Path,
    batch_root: Path,
    config_path: Path,
    expected_config_sha256: str,
) -> tuple[LF022WeakSmokeSelector, LF022WeakLiveAdmission]:
    """Select and admit one pair without performing network calls."""

    _reject_symlink_components(repo_root, label="repository root")
    _reject_symlink_components(batch_root, label="weak batch root")
    _reject_symlink_components(config_path, label="live-smoke config")
    if hash_file(config_path) != expected_config_sha256:
        raise LF022WeakLiveSmokeError("live weak-smoke config hash differs")
    config_model = _load_model(config_path, LF022WeakLiveSmokeConfig)
    assert isinstance(config_model, LF022WeakLiveSmokeConfig)
    config = config_model
    code_bundle_path = _resolve(config.code_bundle, root=repo_root)
    try:
        validated_bundle_sha = validate_code_bundle(
            code_bundle_path,
            config.code_tree_hash,
        )
    except (OSError, ValueError) as exc:
        raise LF022WeakLiveSmokeError(f"invalid code bundle: {exc}") from exc
    if validated_bundle_sha != config.code_bundle.sha256:
        raise LF022WeakLiveSmokeError("validated code-bundle SHA differs from config")
    code_state = collect_code_state(repo_root)
    if (
        code_state.code_tree_hash != config.code_tree_hash
        or code_state.git_revision != config.producer_commit
    ):
        raise LF022WeakLiveSmokeError(
            "current execution tree/commit differs from the admitted code bundle"
        )

    try:
        spec, dispatch_manifest, all_dispatches, candidates = _load_prepared_batch(batch_root)
    except LF022WeakBatchError as exc:
        raise LF022WeakLiveSmokeError(f"prepared weak batch is invalid: {exc}") from exc
    candidate_manifest_model = _load_model(
        batch_root / "inputs/candidate_manifest.json",
        LF022SupervisionCandidateManifest,
    )
    assert isinstance(candidate_manifest_model, LF022SupervisionCandidateManifest)
    candidate_manifest = candidate_manifest_model
    if (
        config.parent_batch_id != dispatch_manifest.batch_id
        or config.parent_batch_spec_sha256 != hash_file(batch_root / "batch_spec.json")
        or config.parent_dispatch_manifest_sha256
        != hash_file(batch_root / "dispatch_manifest.json")
        or config.parent_inventory_id != candidate_manifest.inventory_id
        or config.parent_candidate_manifest_sha256
        != hash_file(batch_root / "inputs/candidate_manifest.json")
        or config.parent_candidate_records_sha256
        != hash_file(batch_root / "inputs/candidate_records.jsonl")
        or config.production_family_matrix_sha256
        != hash_file(batch_root / "inputs/production_family_matrix.json")
    ):
        raise LF022WeakLiveSmokeError(
            "live weak-smoke config differs from its prepared parent batch"
        )
    eligible = tuple(
        sorted(
            (
                candidate
                for candidate in candidates.values()
                if candidate.schema_version == 3
                and candidate.dispatch_status == "ready_for_two_family_judging"
            ),
            key=lambda item: item.candidate_inventory_record_id,
        )
    )
    if not eligible:
        raise LF022WeakLiveSmokeError(
            "one-pair live smoke requires a ready schema-v3 supervision candidate"
        )
    selected = eligible[0]
    if (
        candidate_manifest.proposer_family_id != _QWEN_PROPOSER_FAMILY
        or candidate_manifest.proposer_model != _QWEN_PROPOSER_MODEL
        or dispatch_manifest.proposer_family_id != _QWEN_PROPOSER_FAMILY
        or selected.proposer_family_id != _QWEN_PROPOSER_FAMILY
        or selected.proposer_model != _QWEN_PROPOSER_MODEL
    ):
        raise LF022WeakLiveSmokeError(
            "live weak smoke is bound to an exact Qwen proposer candidate"
        )
    selected_values: dict[str, object] = {
        "schema_version": 1,
        "method_version": _METHOD,
        "selection_rule": config.selection_rule,
        "parent_inventory_id": candidate_manifest.inventory_id,
        "parent_manifest_sha256": hash_file(batch_root / "inputs/candidate_manifest.json"),
        "parent_records_sha256": hash_file(batch_root / "inputs/candidate_records.jsonl"),
        "eligible_candidate_count": len(eligible),
        "selected_candidate_inventory_record_id": selected.candidate_inventory_record_id,
        "selected_pair_id": selected.pair_id,
        "selected_record_sha256": _record_sha(selected),
        "selected_pair_count": 1,
        "required_dispatch_cell_count": 4,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    selector = LF022WeakSmokeSelector.model_validate(
        {
            **selected_values,
            "selector_id": make_id("lf022_weak_selector", selected_values),
        }
    )
    selected_dispatches = tuple(
        sorted(
            (
                item
                for item in all_dispatches
                if item.candidate_inventory_record_id == selected.candidate_inventory_record_id
            ),
            key=lambda item: item.dispatch_cell_id,
        )
    )
    cells = {(item.judge_slot, item.orientation) for item in selected_dispatches}
    if len(selected_dispatches) != 4 or cells != {
        ("judge_A", "AB"),
        ("judge_A", "BA"),
        ("judge_B", "AB"),
        ("judge_B", "BA"),
    }:
        raise LF022WeakLiveSmokeError("selected pair lacks exact AB/BA two-family coverage")

    config_root = config_path.parent
    judge_a = _claim_for_slot(
        batch_root=batch_root,
        config_root=config_root,
        binding=config.judge_a_claim,
        slot="judge_A",
        spec=spec,
        dispatches=selected_dispatches,
    )
    judge_b = _claim_for_slot(
        batch_root=batch_root,
        config_root=config_root,
        binding=config.judge_b_claim,
        slot="judge_B",
        spec=spec,
        dispatches=selected_dispatches,
    )
    if len({selected.proposer_family_id, judge_a.family_id, judge_b.family_id}) != 3:
        raise LF022WeakLiveSmokeError(
            "proposer, judge_A, and judge_B must be three distinct model families"
        )
    if dispatch_manifest.primary_eval_family_id in {
        selected.proposer_family_id,
        judge_a.family_id,
        judge_b.family_id,
    }:
        raise LF022WeakLiveSmokeError("held-out evaluator cannot enter smoke supervision")

    values: dict[str, object] = {
        "schema_version": 1,
        "method_version": _METHOD,
        "config_sha256": expected_config_sha256,
        "batch_id": dispatch_manifest.batch_id,
        "dispatch_manifest_sha256": hash_file(batch_root / "dispatch_manifest.json"),
        "batch_spec_sha256": hash_file(batch_root / "batch_spec.json"),
        "selector": selector.model_dump(mode="json"),
        "judge_a_claim_id": judge_a.claim_id,
        "judge_b_claim_id": judge_b.claim_id,
        "code_bundle_sha256": config.code_bundle.sha256,
        "code_tree_hash": config.code_tree_hash,
        "producer_commit": config.producer_commit,
        "retry_policy": config.retry_policy.model_dump(mode="json"),
        "allowed_dispatch_cell_ids": tuple(item.dispatch_cell_id for item in selected_dispatches),
        "maximum_network_calls": 4,
        "concurrency": 1,
        "public_source_only": True,
        "private_source_content": False,
        "live_execution_requires_explicit_flag": True,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    admission = LF022WeakLiveAdmission.model_validate(
        {
            **values,
            "admission_id": make_id("lf022_weak_live_admission", values),
        }
    )
    root = batch_root / "live_smoke"
    input_root = root / "inputs"
    _immutable(root / "config.json", config_path.read_bytes(), label="live smoke config")
    _immutable(root / "selector.json", _canonical(selector), label="live smoke selector")
    _immutable(root / "judge_A_claim.json", _canonical(judge_a), label="judge_A claim")
    _immutable(root / "judge_B_claim.json", _canonical(judge_b), label="judge_B claim")
    _immutable(root / "admission.json", _canonical(admission), label="live smoke admission")
    _immutable(
        input_root / "code_bundle.tar.gz",
        code_bundle_path.read_bytes(),
        label="live smoke code bundle",
    )
    for slot, claim in (("judge_A", judge_a), ("judge_B", judge_b)):
        matrix_catalog = _resolve(
            claim.production_matrix_catalog_artifact,
            root=config_root,
        )
        raw_catalog = _resolve(claim.raw_rcp_catalog_artifact, root=config_root)
        _immutable(
            input_root / f"{slot}_production_catalog.json",
            matrix_catalog.read_bytes(),
            label=f"{slot} production catalog",
        )
        _immutable(
            input_root / f"{slot}_raw_rcp_catalog.json",
            raw_catalog.read_bytes(),
            label=f"{slot} raw RCP catalog",
        )
    return selector, admission


def _artifact(path: str, *, batch_root: Path) -> Path:
    _reject_symlink_components(batch_root, label="weak batch root")
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise LF022WeakLiveSmokeError("live smoke artifact path is unsafe")
    unresolved = batch_root
    for part in candidate.parts:
        unresolved = unresolved / part
        if unresolved.is_symlink():
            raise LF022WeakLiveSmokeError("live smoke artifact path contains a symlink")
    resolved = (batch_root / candidate).resolve()
    try:
        resolved.relative_to(batch_root.resolve())
    except ValueError as exc:
        raise LF022WeakLiveSmokeError("live smoke artifact escapes batch root") from exc
    return resolved


def _wire_paths(cell_dir: Path) -> tuple[Path, Path]:
    return cell_dir / "wire_response.body", cell_dir / "wire_response.json"


def _load_wire_response(cell_dir: Path) -> RCPWireResponse | None:
    body_path, metadata_path = _wire_paths(cell_dir)
    if not body_path.exists() and not metadata_path.exists():
        return None
    if not body_path.is_file() or body_path.is_symlink() or not metadata_path.is_file():
        raise LF022WeakLiveSmokeError("partial or unsafe wire response artifacts")
    model = _load_model(metadata_path, LF022WeakWireMetadata)
    assert isinstance(model, LF022WeakWireMetadata)
    if hash_file(body_path) != model.body_sha256:
        raise LF022WeakLiveSmokeError("wire response body hash differs from metadata")
    return RCPWireResponse(
        status_code=model.status_code,
        body=body_path.read_bytes(),
        headers=model.headers,
    )


def _persist_wire_response(cell_dir: Path, response: RCPWireResponse) -> tuple[Path, Path]:
    body_path, metadata_path = _wire_paths(cell_dir)
    body_sha = _immutable(body_path, response.body, label="wire response body")
    metadata = LF022WeakWireMetadata(
        status_code=response.status_code,
        headers=response.headers,
        body_sha256=body_sha,
    )
    _immutable(metadata_path, _canonical(metadata), label="wire response metadata")
    return body_path, metadata_path


def _request_for_claim(
    *,
    dispatch: LF022WeakDispatchRecord,
    claim: LF022WeakJudgeRouteClaim,
) -> ProviderRequest:
    rendered = render_blinded_judge_prompt(dispatch.task)
    return ProviderRequest.create(
        identity=ProviderIdentity(
            provider=claim.provider,
            model=claim.model_id,
            revision=claim.production_matrix_revision,
            transport="local",
        ),
        prompt_template_hash=rendered.template_sha256,
        rendered_prompt=rendered.text,
        decoding=claim.decoding.provider_decoding(),
        input_ids=judge_provider_input_ids(dispatch.task),
        private_source_content=False,
        attempt_index=0,
    )


def _verify_live_attempt(
    *,
    attempt: LF022WeakLiveAttempt,
    batch_root: Path,
    admission: LF022WeakLiveAdmission,
    dispatch: LF022WeakDispatchRecord,
) -> ProviderRequest:
    if (
        attempt.admission_id != admission.admission_id
        or attempt.dispatch_cell_id != dispatch.dispatch_cell_id
    ):
        raise LF022WeakLiveSmokeError("live attempt differs from admission/cell")
    request_path = _artifact(attempt.request_artifact, batch_root=batch_root)
    wire_request_path = _artifact(attempt.wire_request_artifact, batch_root=batch_root)
    raw_path = _artifact(attempt.provider_raw_artifact, batch_root=batch_root)
    if (
        hash_file(request_path) != attempt.request_sha256
        or hash_file(wire_request_path) != attempt.wire_request_sha256
        or hash_file(raw_path) != attempt.provider_raw_sha256
    ):
        raise LF022WeakLiveSmokeError("live attempt artifact hash drifted")
    request = load_provider_request(request_path)
    raw = load_provider_raw_response(raw_path, request=request)
    if attempt.status == "response_received":
        if raw.status != "success":
            raise LF022WeakLiveSmokeError("successful live attempt lacks provider success")
    elif raw.status != "error" or raw.error_type != attempt.error_code:
        raise LF022WeakLiveSmokeError("failed live attempt differs from provider raw error")
    if attempt.wire_response_body_artifact is not None:
        assert attempt.wire_response_metadata_artifact is not None
        body_path = _artifact(attempt.wire_response_body_artifact, batch_root=batch_root)
        metadata_path = _artifact(attempt.wire_response_metadata_artifact, batch_root=batch_root)
        if (
            hash_file(body_path) != attempt.wire_response_body_sha256
            or hash_file(metadata_path) != attempt.wire_response_metadata_sha256
        ):
            raise LF022WeakLiveSmokeError("live attempt wire response hash drifted")
    return request


def _verify_wire_to_provider_raw(
    *,
    attempt: LF022WeakLiveAttempt,
    request: ProviderRequest,
    claim: LF022WeakJudgeRouteClaim,
    policy: LF022RCPRetryPolicy,
    batch_root: Path,
) -> None:
    raw_path = _artifact(attempt.provider_raw_artifact, batch_root=batch_root)
    raw = load_provider_raw_response(raw_path, request=request)
    if attempt.status == "transport_unknown":
        if raw.status != "error" or raw.error_type != "transport_unknown":
            raise LF022WeakLiveSmokeError("transport-unknown raw projection differs")
        return
    assert attempt.wire_response_body_artifact is not None
    assert attempt.wire_response_metadata_artifact is not None
    body_path = _artifact(attempt.wire_response_body_artifact, batch_root=batch_root)
    metadata_path = _artifact(
        attempt.wire_response_metadata_artifact,
        batch_root=batch_root,
    )
    metadata_model = _load_model(metadata_path, LF022WeakWireMetadata)
    assert isinstance(metadata_model, LF022WeakWireMetadata)
    wire = RCPWireResponse(
        status_code=metadata_model.status_code,
        body=body_path.read_bytes(),
        headers=metadata_model.headers,
    )
    http_error = classify_http_response(wire, policy=policy, now=attempt.completed_at)
    if attempt.status in {"retryable_http_error", "terminal_http_error"}:
        if (
            http_error is None
            or raw.status != "error"
            or raw.error_type != http_error.code
            or attempt.error_code != http_error.code
            or attempt.http_status != wire.status_code
            or (attempt.status == "retryable_http_error") != http_error.retryable
        ):
            raise LF022WeakLiveSmokeError("HTTP body/status projection differs on replay")
        return
    if http_error is not None or wire.status_code != 200:
        raise LF022WeakLiveSmokeError("successful/invalid replay contains HTTP failure")
    if attempt.http_status != wire.status_code:
        raise LF022WeakLiveSmokeError("wire response HTTP status differs from attempt")
    try:
        completion = parse_chat_completion(wire.body, expected_model=claim.model_id)
    except RCPResponseError as exc:
        if (
            attempt.status != "invalid_response"
            or raw.status != "error"
            or raw.error_type != exc.code
            or attempt.error_code != exc.code
        ):
            raise LF022WeakLiveSmokeError(
                "invalid wire response projection differs on replay"
            ) from exc
    else:
        if (
            attempt.status != "response_received"
            or raw.status != "success"
            or raw.output_text != completion.content
            or attempt.error_code is not None
        ):
            raise LF022WeakLiveSmokeError("wire completion/provider raw projection differs")


def _execute_cell(
    *,
    batch_root: Path,
    spec: LF022WeakBatchSpec,
    dispatch_manifest: LF022WeakDispatchManifest,
    dispatch: LF022WeakDispatchRecord,
    admission: LF022WeakLiveAdmission,
    claim: LF022WeakJudgeRouteClaim,
    credentials: LF022WeakRuntimeCredentials,
    transport: RCPHTTPTransport,
    clock: Callable[[], datetime.datetime],
    after_wire_response_persisted: Callable[[str], None] | None,
) -> tuple[LF022WeakLiveSmokeTerminal, int]:
    smoke_root = batch_root / "live_smoke"
    terminal_path = smoke_root / "terminals" / f"{dispatch.dispatch_cell_id}.json"
    if terminal_path.exists():
        terminal_model = _load_model(terminal_path, LF022WeakLiveSmokeTerminal)
        assert isinstance(terminal_model, LF022WeakLiveSmokeTerminal)
        persisted_request = _verify_live_attempt(
            attempt=terminal_model.live_attempt,
            batch_root=batch_root,
            admission=admission,
            dispatch=dispatch,
        )
        expected_request = _verify_dispatch_request(batch_root, dispatch, spec)
        if persisted_request != expected_request:
            raise LF022WeakLiveSmokeError("replayed request differs from prepared dispatch")
        _verify_wire_to_provider_raw(
            attempt=terminal_model.live_attempt,
            request=persisted_request,
            claim=claim,
            policy=admission.retry_policy,
            batch_root=batch_root,
        )
        expected_wire = (
            canonical_json_bytes(
                make_chat_completion_payload(
                    model_id=claim.model_id,
                    rendered_prompt=expected_request.rendered_prompt,
                    decoding=claim.decoding,
                )
            )
            + b"\n"
        )
        wire_path = _artifact(
            terminal_model.live_attempt.wire_request_artifact,
            batch_root=batch_root,
        )
        if wire_path.read_bytes() != expected_wire:
            raise LF022WeakLiveSmokeError("replayed wire request differs from judge contract")
        raw_path = _artifact(
            terminal_model.live_attempt.provider_raw_artifact,
            batch_root=batch_root,
        )
        raw = load_provider_raw_response(raw_path, request=persisted_request)
        call = terminal_model.weak_terminal.call
        if raw.status == "success" and raw.output_text:
            try:
                parsed = parse_blinded_judge_output(raw.output_text)
            except JudgeOutputParseError:
                expected_parse = ParseStatus.PARSE_FAILED
                expected_output = None
            else:
                expected_parse = ParseStatus.PARSED
                expected_output = parsed.model_dump(mode="json", by_alias=True)
        else:
            expected_parse = ParseStatus.EMPTY
            expected_output = None
        if (
            call.parse_status is not expected_parse
            or call.parsed_output != expected_output
            or terminal_model.weak_terminal.provider_request_hash != persisted_request.request_hash
        ):
            raise LF022WeakLiveSmokeError("replayed parsed judgment/lineage drifted")
        from leanfaith.generation.lf022_weak_batch import _verify_terminal

        _verify_terminal(
            batch_root=batch_root,
            manifest=dispatch_manifest,
            spec=spec,
            dispatch=dispatch,
            terminal=terminal_model.weak_terminal,
        )
        return terminal_model, 0

    request = _verify_dispatch_request(batch_root, dispatch, spec)
    if request != _request_for_claim(dispatch=dispatch, claim=claim):
        raise LF022WeakLiveSmokeError("prepared request differs from admitted judge claim")
    cell_dir = smoke_root / "cells" / dispatch.dispatch_cell_id
    request_path = cell_dir / "provider_request.json"
    persist_provider_request(request, request_path)
    wire = make_chat_completion_payload(
        model_id=claim.model_id,
        rendered_prompt=request.rendered_prompt,
        decoding=claim.decoding,
    )
    wire_request_path = cell_dir / "wire_request.json"
    _immutable(
        wire_request_path,
        canonical_json_bytes(wire) + b"\n",
        label="wire request",
    )

    attempt_path = cell_dir / "attempt.json"
    if attempt_path.exists():
        attempt_model = _load_model(attempt_path, LF022WeakLiveAttempt)
        assert isinstance(attempt_model, LF022WeakLiveAttempt)
        attempt = attempt_model
        persisted_request = _verify_live_attempt(
            attempt=attempt,
            batch_root=batch_root,
            admission=admission,
            dispatch=dispatch,
        )
        if persisted_request != request:
            raise LF022WeakLiveSmokeError("partial-resume request differs from prepared dispatch")
        expected_wire = canonical_json_bytes(wire) + b"\n"
        if wire_request_path.read_bytes() != expected_wire:
            raise LF022WeakLiveSmokeError("partial-resume wire request differs from judge contract")
        _verify_wire_to_provider_raw(
            attempt=attempt,
            request=request,
            claim=claim,
            policy=admission.retry_policy,
            batch_root=batch_root,
        )
        raw_path = _artifact(attempt.provider_raw_artifact, batch_root=batch_root)
        result = persist_provider_raw_response(
            batch_root / "live_smoke/raw" / dispatch.judge_slot,
            load_provider_raw_response(raw_path, request=request),
            replayed=True,
        )
        started_at = attempt.started_at
        completed_at = attempt.completed_at
        network_calls = 0
    else:
        started_at = clock()
        response = _load_wire_response(cell_dir)
        transport_started = cell_dir / ".transport_started"
        transport_completed = cell_dir / ".transport_completed"
        network_calls = 0
        raw_response: ProviderRawResponse
        status: AttemptStatus
        http_status: int | None
        error_code: str | None
        if response is None:
            if transport_completed.exists():
                raise LF022WeakLiveSmokeError(
                    "transport-completed marker exists without persisted response"
                )
            if transport_started.exists() and not transport_completed.exists():
                raw_response = ProviderRawResponse.error(
                    request,
                    error_type="transport_unknown",
                    error_detail="prior invocation may have sent request without response",
                )
                status = "transport_unknown"
                http_status = None
                error_code = "transport_unknown"
            else:
                _immutable(transport_started, b"started\n", label="transport marker")
                try:
                    response = transport.post_json(
                        url=credentials.base_url.rstrip("/") + "/chat/completions",
                        api_key=credentials.api_key,
                        payload=wire,
                        timeout_seconds=admission.retry_policy.request_timeout_seconds,
                    )
                    network_calls = 1
                except RCPTransportUnknownError:
                    raw_response = ProviderRawResponse.error(
                        request,
                        error_type="transport_unknown",
                        error_detail="RCP transport returned no response",
                    )
                    status = "transport_unknown"
                    http_status = None
                    error_code = "transport_unknown"
                else:
                    _persist_wire_response(cell_dir, response)
                    _immutable(transport_completed, b"completed\n", label="transport marker")
                    if after_wire_response_persisted is not None:
                        after_wire_response_persisted(dispatch.dispatch_cell_id)
        if response is not None:
            http_error = classify_http_response(
                response,
                policy=admission.retry_policy,
                now=clock(),
            )
            if http_error is not None:
                raw_response = ProviderRawResponse.error(
                    request,
                    error_type=http_error.code,
                    error_detail=None,
                )
                status = "retryable_http_error" if http_error.retryable else "terminal_http_error"
                http_status = response.status_code
                error_code = http_error.code
            else:
                try:
                    completion = parse_chat_completion(
                        response.body,
                        expected_model=claim.model_id,
                    )
                except RCPResponseError as exc:
                    raw_response = ProviderRawResponse.error(
                        request,
                        error_type=exc.code,
                        error_detail=None,
                    )
                    status = "invalid_response"
                    http_status = response.status_code
                    error_code = exc.code
                else:
                    raw_response = ProviderRawResponse.success(request, completion.content)
                    status = "response_received"
                    http_status = response.status_code
                    error_code = None
        result = persist_provider_raw_response(
            batch_root / "live_smoke/raw" / dispatch.judge_slot,
            raw_response,
        )
        completed_at = clock()
        body_path, metadata_path = _wire_paths(cell_dir)
        attempt = LF022WeakLiveAttempt(
            admission_id=admission.admission_id,
            dispatch_cell_id=dispatch.dispatch_cell_id,
            request_artifact=_relative(request_path, root=batch_root),
            request_sha256=hash_file(request_path),
            wire_request_artifact=_relative(wire_request_path, root=batch_root),
            wire_request_sha256=hash_file(wire_request_path),
            wire_response_body_artifact=(
                _relative(body_path, root=batch_root) if body_path.exists() else None
            ),
            wire_response_body_sha256=(hash_file(body_path) if body_path.exists() else None),
            wire_response_metadata_artifact=(
                _relative(metadata_path, root=batch_root) if metadata_path.exists() else None
            ),
            wire_response_metadata_sha256=(
                hash_file(metadata_path) if metadata_path.exists() else None
            ),
            provider_raw_artifact=_relative(result.raw_response_path, root=batch_root),
            provider_raw_sha256=result.raw_response_sha256,
            status=status,
            http_status=http_status,
            error_code=error_code,
            started_at=started_at,
            completed_at=completed_at,
        )
        _immutable(attempt_path, _canonical(attempt), label="live smoke attempt")

    parsed_output: Mapping[str, object] | None = None
    if result.response.status == "success" and result.response.output_text:
        try:
            parsed = parse_blinded_judge_output(result.response.output_text)
        except JudgeOutputParseError:
            parse_status = ParseStatus.PARSE_FAILED
        else:
            parse_status = ParseStatus.PARSED
            parsed_output = parsed.model_dump(mode="json", by_alias=True)
    else:
        parse_status = ParseStatus.EMPTY
    rendered = render_blinded_judge_prompt(dispatch.task)
    lineage = bridge_provider_result_to_generic_llm_lineage(
        request=request,
        result=result,
        # The generic LF-022 terminal verifier is intentionally bound to the
        # immutable request artifact prepared for the dispatch cell.  The
        # live-smoke copy remains bound separately by ``LF022WeakLiveAttempt``
        # so replay verifies both artifacts without changing the parent batch
        # lineage.
        request_artifact_path=_artifact(dispatch.request_artifact, batch_root=batch_root),
        artifact_root=batch_root,
        role=LLMRole.JUDGE,
        provider_slot=dispatch.judge_slot,
        model_family=dispatch.judge_family_id,
        prompt_template_id=JUDGE_TEMPLATE_ID,
        prompt_template_version=rendered.template_version,
        execution_mode="external",
        parse_status=parse_status,
        parsed_output=parsed_output,
        private_source_content=False,
        denylist_checked=True,
        denylist_hits=(),
        started_at=started_at,
        completed_at=completed_at,
        supervision_eligible=False,
        metadata={
            "lf022_weak_live_admission_id": admission.admission_id,
            "weak_supervision_config_hash": dispatch_manifest.weak_supervision_config_sha256,
            "weak_batch_id": admission.batch_id,
            "weak_dispatch_cell_id": dispatch.dispatch_cell_id,
            "proposer_family": dispatch.proposer_family_id,
            "judge_orientation": dispatch.orientation,
            "smoke_route_admitted": True,
            "scale_judge_qualified": False,
            "semantic_label_created": False,
            "training_eligible": False,
        },
    )
    if check_llm_call_attempt_lineage(lineage.call, (lineage.attempt,)):
        raise LF022WeakLiveSmokeError("generic judge lineage is inconsistent")
    weak_values: dict[str, object] = {
        "schema_version": 1,
        "batch_id": admission.batch_id,
        "dispatch_cell_id": dispatch.dispatch_cell_id,
        "provider_request_hash": request.request_hash,
        "call": lineage.call.model_dump(mode="json"),
        "attempt": lineage.attempt.model_dump(mode="json"),
        "raw_first_artifact_verified": True,
        "semantic_label_created": False,
        "silver_promoted": False,
        "train_eligible": False,
        "eval_eligible": False,
        "gate_credit_claimed": False,
    }
    weak = LF022WeakTerminalRecord.model_validate(
        {
            **weak_values,
            "terminal_id": make_id("lf022_weak_terminal", weak_values),
        }
    )
    values: dict[str, object] = {
        "schema_version": 1,
        "admission_id": admission.admission_id,
        "batch_id": admission.batch_id,
        "dispatch_cell_id": dispatch.dispatch_cell_id,
        "live_attempt": attempt.model_dump(mode="json"),
        "weak_terminal": weak.model_dump(mode="json"),
        "semantic_labels_created": False,
        "silver_records_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    terminal = LF022WeakLiveSmokeTerminal.model_validate(
        {
            **values,
            "terminal_id": make_id("lf022_weak_live_terminal", values),
        }
    )
    _immutable(terminal_path, _canonical(terminal), label="live smoke terminal")
    return terminal, network_calls


def _verify_finalized_smoke_commit(
    *,
    batch_root: Path,
    admission: LF022WeakLiveAdmission,
) -> LF022WeakLiveSmokeManifest | None:
    """Fail closed on a complete run's commit marker before any transport use.

    ``execution_manifest.json`` is the last artifact written by a successful
    execution.  Once it exists, missing per-cell state is corruption rather
    than resumable work: sending that cell again could duplicate a completed
    provider request.  Verify the complete aggregate and per-cell corpus before
    the normal replay path is allowed to proceed.
    """

    smoke_root = batch_root / "live_smoke"
    manifest_path = smoke_root / "execution_manifest.json"
    _reject_symlink_components(manifest_path, label="live-smoke execution manifest")
    if not manifest_path.exists():
        return None
    manifest_model = _load_model(manifest_path, LF022WeakLiveSmokeManifest)
    assert isinstance(manifest_model, LF022WeakLiveSmokeManifest)
    manifest = manifest_model
    if (
        manifest.admission_id != admission.admission_id
        or manifest.batch_id != admission.batch_id
        or manifest.code_tree_hash != admission.code_tree_hash
        or manifest.producer_commit != admission.producer_commit
    ):
        raise LF022WeakLiveSmokeError("finalized live-smoke manifest differs from admission")

    aggregate_bindings = (
        (manifest.terminals_artifact, manifest.terminals_sha256, "terminal corpus"),
        (manifest.evidence_artifact, manifest.evidence_sha256, "judgment evidence"),
        (
            manifest.weak_candidates_artifact,
            manifest.weak_candidates_sha256,
            "weak candidate corpus",
        ),
    )
    for artifact, expected_sha256, label in aggregate_bindings:
        path = _artifact(artifact, batch_root=batch_root)
        if path.is_symlink() or not path.is_file() or hash_file(path) != expected_sha256:
            raise LF022WeakLiveSmokeError(
                f"finalized live-smoke {label} is absent, unsafe, or drifted"
            )

    terminal_corpus_path = _artifact(manifest.terminals_artifact, batch_root=batch_root)
    terminal_corpus: list[LF022WeakLiveSmokeTerminal] = []
    for line_number, raw in enumerate(
        terminal_corpus_path.read_bytes().splitlines(keepends=True), start=1
    ):
        if not raw.endswith(b"\n") or not raw.strip():
            raise LF022WeakLiveSmokeError(
                f"invalid finalized terminal JSONL framing at line {line_number}"
            )
        try:
            terminal = LF022WeakLiveSmokeTerminal.model_validate_json(raw)
        except ValueError as exc:
            raise LF022WeakLiveSmokeError(
                f"invalid finalized terminal at line {line_number}: {exc}"
            ) from exc
        if raw != _canonical(terminal):
            raise LF022WeakLiveSmokeError(f"non-canonical finalized terminal at line {line_number}")
        terminal_corpus.append(terminal)
    terminal_corpus.sort(key=lambda item: item.dispatch_cell_id)
    expected_cell_ids = admission.allowed_dispatch_cell_ids
    observed_cell_ids = tuple(item.dispatch_cell_id for item in terminal_corpus)
    if observed_cell_ids != expected_cell_ids or len(terminal_corpus) != manifest.terminal_count:
        raise LF022WeakLiveSmokeError(
            "finalized terminal corpus differs from the admitted four cells"
        )
    for terminal in terminal_corpus:
        cell_terminal_path = smoke_root / "terminals" / f"{terminal.dispatch_cell_id}.json"
        if not cell_terminal_path.exists():
            raise LF022WeakLiveSmokeError(
                "finalized live smoke is missing a committed per-cell terminal"
            )
        cell_model = _load_model(cell_terminal_path, LF022WeakLiveSmokeTerminal)
        assert isinstance(cell_model, LF022WeakLiveSmokeTerminal)
        if cell_model != terminal:
            raise LF022WeakLiveSmokeError(
                "committed per-cell terminal differs from finalized terminal corpus"
            )
    return manifest


def execute_lf022_weak_live_smoke(
    *,
    repo_root: Path,
    batch_root: Path,
    admission_path: Path,
    expected_admission_sha256: str,
    execute_public_provisional: bool = False,
    credentials: LF022WeakRuntimeCredentials | None = None,
    transports: Mapping[JudgeSlot, RCPHTTPTransport] | None = None,
    clock: Callable[[], datetime.datetime] | None = None,
    after_wire_response_persisted: Callable[[str], None] | None = None,
) -> tuple[tuple[LF022WeakLiveSmokeTerminal, ...], LF022WeakLiveSmokeManifest]:
    """Run/resume the admitted four cells serially through injected transports."""

    _reject_symlink_components(repo_root, label="repository root")
    _reject_symlink_components(batch_root, label="weak batch root")
    _reject_symlink_components(admission_path, label="live-smoke admission")
    if hash_file(admission_path) != expected_admission_sha256:
        raise LF022WeakLiveSmokeError("live smoke admission hash differs")
    admission_model = _load_model(admission_path, LF022WeakLiveAdmission)
    assert isinstance(admission_model, LF022WeakLiveAdmission)
    admission = admission_model
    if hash_file(batch_root / "live_smoke/config.json") != admission.config_sha256:
        raise LF022WeakLiveSmokeError("self-contained live config differs from admission")
    bundle_path = batch_root / "live_smoke/inputs/code_bundle.tar.gz"
    try:
        observed_bundle_sha = validate_code_bundle(bundle_path, admission.code_tree_hash)
    except (OSError, ValueError) as exc:
        raise LF022WeakLiveSmokeError(f"self-contained code bundle is invalid: {exc}") from exc
    if observed_bundle_sha != admission.code_bundle_sha256:
        raise LF022WeakLiveSmokeError("self-contained code bundle differs from admission")
    code_state = collect_code_state(repo_root)
    if (
        code_state.code_tree_hash != admission.code_tree_hash
        or code_state.git_revision != admission.producer_commit
    ):
        raise LF022WeakLiveSmokeError("runtime code tree/commit differs from admission")
    if not execute_public_provisional:
        raise LF022WeakLiveSmokeError("live smoke requires an explicit execution flag")
    if credentials is None or transports is None:
        raise LF022WeakLiveSmokeError("live smoke requires runtime credentials and transports")
    if credentials.base_url.rstrip("/") != _RCP_BASE_URL or not credentials.api_key:
        raise LF022WeakLiveSmokeError("RCP credentials differ from admitted endpoint")

    try:
        spec, dispatch_manifest, all_dispatches, candidates = _load_prepared_batch(batch_root)
    except LF022WeakBatchError as exc:
        raise LF022WeakLiveSmokeError(f"prepared weak batch is invalid: {exc}") from exc
    if (
        dispatch_manifest.batch_id != admission.batch_id
        or hash_file(batch_root / "dispatch_manifest.json") != admission.dispatch_manifest_sha256
        or hash_file(batch_root / "batch_spec.json") != admission.batch_spec_sha256
    ):
        raise LF022WeakLiveSmokeError("prepared batch differs from live admission")
    selected = candidates.get(admission.selector.selected_candidate_inventory_record_id)
    if (
        hash_file(batch_root / "inputs/candidate_manifest.json")
        != admission.selector.parent_manifest_sha256
        or hash_file(batch_root / "inputs/candidate_records.jsonl")
        != admission.selector.parent_records_sha256
    ):
        raise LF022WeakLiveSmokeError("parent candidate inventory differs from selector")
    if (
        selected is None
        or selected.pair_id != admission.selector.selected_pair_id
        or _record_sha(selected) != admission.selector.selected_record_sha256
    ):
        raise LF022WeakLiveSmokeError("selected candidate differs from parent inventory")
    if (
        selected.proposer_family_id != _QWEN_PROPOSER_FAMILY
        or selected.proposer_model != _QWEN_PROPOSER_MODEL
        or dispatch_manifest.proposer_family_id != _QWEN_PROPOSER_FAMILY
    ):
        raise LF022WeakLiveSmokeError("selected candidate is not the exact Qwen proposer")
    dispatch_by_id = {item.dispatch_cell_id: item for item in all_dispatches}
    try:
        dispatches = tuple(dispatch_by_id[item] for item in admission.allowed_dispatch_cell_ids)
    except KeyError as exc:
        raise LF022WeakLiveSmokeError("admitted dispatch cell is absent") from exc
    if any(
        item.candidate_inventory_record_id != selected.candidate_inventory_record_id
        for item in dispatches
    ):
        raise LF022WeakLiveSmokeError("admitted cells do not belong to selected candidate")

    judge_a_model = _load_model(
        batch_root / "live_smoke/judge_A_claim.json", LF022WeakJudgeRouteClaim
    )
    judge_b_model = _load_model(
        batch_root / "live_smoke/judge_B_claim.json", LF022WeakJudgeRouteClaim
    )
    assert isinstance(judge_a_model, LF022WeakJudgeRouteClaim)
    assert isinstance(judge_b_model, LF022WeakJudgeRouteClaim)
    claims: dict[JudgeSlot, LF022WeakJudgeRouteClaim] = {
        "judge_A": judge_a_model,
        "judge_B": judge_b_model,
    }
    if (
        judge_a_model.claim_id != admission.judge_a_claim_id
        or judge_b_model.claim_id != admission.judge_b_claim_id
    ):
        raise LF022WeakLiveSmokeError("persisted judge claims differ from admission")
    for slot, claim in claims.items():
        matrix_catalog = batch_root / f"live_smoke/inputs/{slot}_production_catalog.json"
        raw_catalog = batch_root / f"live_smoke/inputs/{slot}_raw_rcp_catalog.json"
        if (
            hash_file(matrix_catalog) != claim.production_matrix_catalog_artifact.sha256
            or hash_file(raw_catalog) != claim.raw_rcp_catalog_artifact.sha256
        ):
            raise LF022WeakLiveSmokeError(f"self-contained {slot} catalog evidence drifted")

    now = clock or (lambda: datetime.datetime.now(tz=datetime.UTC))
    smoke_root = batch_root / "live_smoke"
    smoke_root.mkdir(parents=True, exist_ok=True)
    lock_path = smoke_root / ".lock"
    if lock_path.is_symlink():
        raise LF022WeakLiveSmokeError("live smoke lock cannot be a symlink")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise LF022WeakLiveSmokeError("live weak smoke is already locked") from exc
    network_calls = 0
    terminals: list[LF022WeakLiveSmokeTerminal] = []
    try:
        _verify_finalized_smoke_commit(
            batch_root=batch_root,
            admission=admission,
        )
        for dispatch in dispatches:
            transport = transports.get(dispatch.judge_slot)
            if transport is None:
                raise LF022WeakLiveSmokeError(f"missing transport for {dispatch.judge_slot}")
            terminal, calls = _execute_cell(
                batch_root=batch_root,
                spec=spec,
                dispatch_manifest=dispatch_manifest,
                dispatch=dispatch,
                admission=admission,
                claim=claims[dispatch.judge_slot],
                credentials=credentials,
                transport=transport,
                clock=now,
                after_wire_response_persisted=after_wire_response_persisted,
            )
            network_calls += calls
            if network_calls > admission.maximum_network_calls:
                raise LF022WeakLiveSmokeError("live smoke exceeded four network calls")
            terminals.append(terminal)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    terminals.sort(key=lambda item: item.dispatch_cell_id)
    terminal_sha = _immutable(
        smoke_root / "terminals.jsonl",
        _jsonl(terminals),
        label="live smoke terminal corpus",
    )
    family_matrix = FamilySeparationMatrix(
        proposer_family=dispatch_manifest.proposer_family_id,
        judge_a_family=dispatch_manifest.judge_a_family_id,
        judge_b_family=dispatch_manifest.judge_b_family_id,
        primary_eval_judge_family=dispatch_manifest.primary_eval_family_id,
    )
    dispatch_by_cell = {item.dispatch_cell_id: item for item in dispatches}
    evidence: list[EvidenceRecord] = []
    for terminal in terminals:
        dispatch = dispatch_by_cell[terminal.dispatch_cell_id]
        call = terminal.weak_terminal.call
        if call.parse_status is not ParseStatus.PARSED:
            continue
        evidence.append(
            materialize_verified_judgment_evidence(
                call=call,
                task=dispatch.task,
                source=selected.pair,
                family_matrix=family_matrix,
                proposer_family=dispatch.proposer_family_id,
                method_version=_METHOD,
                config_hash=dispatch_manifest.weak_supervision_config_sha256,
                artifact_root=batch_root,
                created_at=call.completed_at or call.request_date,
            )
        )
    evidence.sort(key=lambda item: item.evidence_id)
    evidence_sha = _immutable(
        smoke_root / "judgment_evidence.jsonl",
        _jsonl(evidence),
        label="live smoke judgment evidence",
    )
    created_at = max(
        item.weak_terminal.call.completed_at or item.weak_terminal.call.request_date
        for item in terminals
    )
    weak_candidate = build_weak_consensus_candidate(
        pair_id=selected.pair_id,
        proposer_family=selected.proposer_family_id,
        family_matrix=family_matrix,
        judgments=tuple(evidence),
        created_at=created_at,
    )
    weak_sha = _immutable(
        smoke_root / "weak_consensus_candidates.jsonl",
        _jsonl((weak_candidate,)),
        label="live smoke weak candidate",
    )
    values: dict[str, object] = {
        "schema_version": 1,
        "admission_id": admission.admission_id,
        "batch_id": admission.batch_id,
        "code_tree_hash": admission.code_tree_hash,
        "producer_commit": admission.producer_commit,
        "terminals_artifact": "live_smoke/terminals.jsonl",
        "terminals_sha256": terminal_sha,
        "terminal_count": 4,
        "status_counts": dict(
            sorted(Counter(item.live_attempt.status for item in terminals).items())
        ),
        "transport_attempt_count": 4,
        "evidence_artifact": "live_smoke/judgment_evidence.jsonl",
        "evidence_sha256": evidence_sha,
        "parsed_evidence_count": len(evidence),
        "weak_candidates_artifact": "live_smoke/weak_consensus_candidates.jsonl",
        "weak_candidates_sha256": weak_sha,
        "weak_candidate_count": 1,
        "all_selected_cells_terminal": True,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    manifest = LF022WeakLiveSmokeManifest.model_validate(
        {
            **values,
            "execution_id": make_id("lf022_weak_live_execution", values),
        }
    )
    _immutable(
        smoke_root / "execution_manifest.json",
        _canonical(manifest),
        label="live smoke execution manifest",
    )
    return tuple(terminals), manifest


class _OfflineReplayTransport:
    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> RCPWireResponse:
        del url, api_key, payload, timeout_seconds
        raise LF022WeakLiveSmokeError(
            "offline replay encountered a missing terminal and refused transport"
        )


def replay_lf022_weak_live_smoke(
    *,
    repo_root: Path,
    batch_root: Path,
    admission_path: Path,
    expected_admission_sha256: str,
) -> tuple[tuple[LF022WeakLiveSmokeTerminal, ...], LF022WeakLiveSmokeManifest]:
    """Verify the complete smoke from immutable artifacts with zero network I/O."""

    admission_model = _load_model(admission_path, LF022WeakLiveAdmission)
    assert isinstance(admission_model, LF022WeakLiveAdmission)
    no_network = _OfflineReplayTransport()
    return execute_lf022_weak_live_smoke(
        repo_root=repo_root,
        batch_root=batch_root,
        admission_path=admission_path,
        expected_admission_sha256=expected_admission_sha256,
        execute_public_provisional=True,
        credentials=LF022WeakRuntimeCredentials(
            base_url=_RCP_BASE_URL,
            api_key="offline-replay-not-a-provider-credential",
        ),
        transports={"judge_A": no_network, "judge_B": no_network},
    )


__all__ = [
    "LF022WeakJudgeDecodingContract",
    "LF022WeakJudgeRouteClaim",
    "LF022WeakJudgeRouteSpec",
    "LF022WeakLiveAdmission",
    "LF022WeakLiveSmokeConfig",
    "LF022WeakLiveSmokeError",
    "LF022WeakLiveSmokeFreezeResult",
    "LF022WeakLiveSmokeManifest",
    "LF022WeakLiveSmokeTerminal",
    "LF022WeakRuntimeCredentials",
    "LF022WeakSmokeSelector",
    "execute_lf022_weak_live_smoke",
    "freeze_lf022_weak_live_smoke_inputs",
    "lf022_weak_judge_route_for_slot",
    "prepare_lf022_weak_live_smoke",
    "replay_lf022_weak_live_smoke",
]
