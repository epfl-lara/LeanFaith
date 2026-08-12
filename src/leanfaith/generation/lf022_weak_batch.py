"""Production-safe LF-022 two-family weak-supervision batch foundation.

The module intentionally contains no network adapter and no promotion path.
It provides three operations:

* prepare four immutable judge requests per admitted public pair;
* execute or resume those requests through an injected provider, recovering
  from a raw response persisted before a process crash;
* replay all artifacts offline and materialize only judgment evidence and
  non-trainable ``WeakConsensusCandidateRecord`` objects.

Neither Codex diagnostics nor two-family agreement is converted into a
semantic, silver, gold, training, or evaluation label here.

Candidate inputs may use historical schema v2 or source-neutral schema v3.
The v3 Codex diagnostic is optional metadata and is never consulted for
dispatch admission or aggregation.
"""

from __future__ import annotations

import datetime
import os
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_production import LF022ProductionFamilyMatrix
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateManifest,
    LF022SupervisionCandidateRecord,
)
from leanfaith.generation.providers import (
    DecodingValue,
    DeterministicFixtureProvider,
    GenerationProvider,
    ProviderIdentity,
    ProviderRequest,
    ReplayProvider,
    bridge_provider_result_to_generic_llm_lineage,
    load_provider_raw_response,
    load_provider_request,
    persist_provider_request,
    provider_raw_response_path,
    verify_generic_llm_call_artifacts,
)
from leanfaith.generation.weak_supervision import (
    JUDGE_TEMPLATE_ID,
    FamilySeparationMatrix,
    JudgeOutputParseError,
    JudgePresentation,
    JudgeSlot,
    build_weak_consensus_candidate,
    judge_provider_input_ids,
    make_swapped_presentations,
    materialize_verified_judgment_evidence,
    parse_blinded_judge_output,
    render_blinded_judge_prompt,
    validate_family_separation,
)
from leanfaith.schemas.enums import LLMCallStatus, LLMRole, ParseStatus
from leanfaith.schemas.evidence import EvidenceRecord
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.llm import (
    LLMAttemptRecord,
    LLMCallRecord,
    LLMExecutionMode,
    check_llm_call_attempt_lineage,
)
from leanfaith.schemas.weak_supervision import WeakConsensusCandidateRecord

WEAK_BATCH_METHOD_VERSION: Literal["lf022_weak_batch_v1"] = "lf022_weak_batch_v1"
_REQUIRED_CELLS = ("judge_A:AB", "judge_A:BA", "judge_B:AB", "judge_B:BA")


class LF022WeakBatchError(RuntimeError):
    """One immutable input, dispatch, replay, or finalization invariant failed."""


class BoundArtifact(StrictModel):
    """Exact byte binding for one input artifact."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)


class JudgeEndpointPin(StrictModel):
    """Exact request identity for one weak-supervision judge slot."""

    provider_slot: JudgeSlot
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    decoding: dict[str, DecodingValue] = Field(default_factory=dict)


class LF022WeakBatchSpec(StrictModel):
    """Offline-only, fully pinned request for one four-cell judge batch."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_weak_batch_v1"] = WEAK_BATCH_METHOD_VERSION
    batch_name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    candidate_manifest: BoundArtifact
    candidate_records: BoundArtifact
    weak_supervision_config: BoundArtifact
    production_family_matrix: BoundArtifact
    randomization_key_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_a: JudgeEndpointPin
    judge_b: JudgeEndpointPin
    primary_eval_family_id: str = Field(min_length=1)
    execution_authorization: Literal[
        "offline_fixture_or_replay_only",
        "live_provider_calls_explicitly_authorized",
    ] = "offline_fixture_or_replay_only"
    live_provider_calls_authorized: bool = Field(default=False, strict=True)
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _families(self) -> Self:
        if self.live_provider_calls_authorized != (
            self.execution_authorization == "live_provider_calls_explicitly_authorized"
        ):
            raise ValueError("execution authorization and live-call flag must agree")
        if self.judge_a.provider_slot != "judge_A" or self.judge_b.provider_slot != "judge_B":
            raise ValueError("judge endpoint pins must occupy their named slots")
        validate_family_separation(
            FamilySeparationMatrix(
                proposer_family="__validated_from_candidate_manifest__",
                judge_a_family=self.judge_a.family_id,
                judge_b_family=self.judge_b.family_id,
                primary_eval_judge_family=self.primary_eval_family_id,
            )
        )
        return self


class LF022WeakDispatchRecord(StrictModel):
    """One immutable judge cell and its canonical provider request."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_weak_batch_v1"] = WEAK_BATCH_METHOD_VERSION
    dispatch_cell_id: str = Field(pattern=id_pattern("lf022_weak_cell"))
    candidate_inventory_record_id: str = Field(pattern=id_pattern("lf022_supervision_candidate"))
    pair_id: str = Field(pattern=id_pattern("pair"))
    proposer_family_id: str = Field(min_length=1)
    judge_family_id: str = Field(min_length=1)
    judge_slot: JudgeSlot
    orientation: Literal["AB", "BA"]
    task: JudgePresentation
    request_artifact: str = Field(min_length=1)
    request_artifact_sha256: str = Field(pattern=HEX64_PATTERN)
    provider_request_hash: str = Field(pattern=HEX64_PATTERN)
    provider_attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    prompt_template_sha256: str = Field(pattern=HEX64_PATTERN)
    prompt_render_sha256: str = Field(pattern=HEX64_PATTERN)
    source_admission_sha256: str = Field(pattern=HEX64_PATTERN)
    semantic_label_created: Literal[False] = False
    silver_promoted: Literal[False] = False
    train_eligible: Literal[False] = False
    eval_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (
            self.pair_id != self.task.pair_id
            or self.judge_slot != self.task.judge_slot
            or self.orientation != self.task.orientation
            or self.source_admission_sha256 != self.task.source_admission_sha256
        ):
            raise ValueError("dispatch cell differs from its blinded judge task")
        expected_id = make_id(
            "lf022_weak_cell",
            self.model_dump(mode="json", exclude={"dispatch_cell_id"}),
        )
        if self.dispatch_cell_id != expected_id:
            raise ValueError("dispatch_cell_id differs from dispatch content")
        return self


class LF022WeakDispatchManifest(StrictModel):
    """Content-addressed prepared-batch manifest."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_weak_batch_v1"] = WEAK_BATCH_METHOD_VERSION
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    batch_name: str
    spec_sha256: str = Field(pattern=HEX64_PATTERN)
    candidate_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    candidate_records_sha256: str = Field(pattern=HEX64_PATTERN)
    weak_supervision_config_sha256: str = Field(pattern=HEX64_PATTERN)
    production_family_matrix_sha256: str = Field(pattern=HEX64_PATTERN)
    randomization_key_sha256: str = Field(pattern=HEX64_PATTERN)
    proposer_family_id: str
    judge_a_family_id: str
    judge_b_family_id: str
    primary_eval_family_id: str
    candidate_manifest_artifact: Literal["inputs/candidate_manifest.json"] = (
        "inputs/candidate_manifest.json"
    )
    candidate_records_artifact: Literal["inputs/candidate_records.jsonl"] = (
        "inputs/candidate_records.jsonl"
    )
    weak_supervision_config_artifact: Literal["inputs/weak_supervision.yaml"] = (
        "inputs/weak_supervision.yaml"
    )
    production_family_matrix_artifact: Literal["inputs/production_family_matrix.json"] = (
        "inputs/production_family_matrix.json"
    )
    dispatch_records_artifact: Literal["dispatch_records.jsonl"] = "dispatch_records.jsonl"
    dispatch_records_sha256: str = Field(pattern=HEX64_PATTERN)
    dispatch_pair_count: int = Field(ge=0, strict=True)
    dispatch_cell_count: int = Field(ge=0, strict=True)
    required_cell_count: int = Field(ge=0, strict=True)
    live_provider_calls_authorized: bool = Field(default=False, strict=True)
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _counts_and_id(self) -> Self:
        if self.dispatch_cell_count != 4 * self.dispatch_pair_count:
            raise ValueError("prepared batch must contain four cells per pair")
        if self.required_cell_count != self.dispatch_cell_count:
            raise ValueError("every prepared cell remains required")
        expected_id = make_id(
            "lf022_weak_batch",
            self.model_dump(mode="json", exclude={"batch_id", "spec_sha256"}),
        )
        if self.batch_id != expected_id:
            raise ValueError("batch_id differs from dispatch manifest content")
        return self


class LF022WeakTerminalRecord(StrictModel):
    """One terminal provider lineage bound to a prepared dispatch cell."""

    schema_version: Literal[1] = 1
    terminal_id: str = Field(pattern=id_pattern("lf022_weak_terminal"))
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    dispatch_cell_id: str = Field(pattern=id_pattern("lf022_weak_cell"))
    provider_request_hash: str = Field(pattern=HEX64_PATTERN)
    call: LLMCallRecord
    attempt: LLMAttemptRecord
    raw_first_artifact_verified: Literal[True] = True
    semantic_label_created: Literal[False] = False
    silver_promoted: Literal[False] = False
    train_eligible: Literal[False] = False
    eval_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _lineage(self) -> Self:
        if self.call.provider_request_hash != self.provider_request_hash:
            raise ValueError("terminal call differs from prepared provider request")
        violations = check_llm_call_attempt_lineage(self.call, (self.attempt,))
        if violations:
            raise ValueError("invalid terminal call/attempt lineage: " + ", ".join(violations))
        expected_id = make_id(
            "lf022_weak_terminal",
            self.model_dump(mode="json", exclude={"terminal_id"}),
        )
        if self.terminal_id != expected_id:
            raise ValueError("terminal_id differs from terminal content")
        return self


class LF022WeakExecutionManifest(StrictModel):
    """Deterministic summary of one complete terminal corpus."""

    schema_version: Literal[1] = 1
    execution_id: str = Field(pattern=id_pattern("lf022_weak_execution"))
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    dispatch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    terminal_records_artifact: Literal["terminal_records.jsonl"] = "terminal_records.jsonl"
    terminal_records_sha256: str = Field(pattern=HEX64_PATTERN)
    terminal_count: int = Field(ge=0, strict=True)
    parse_status_counts: dict[str, int]
    call_status_counts: dict[str, int]
    all_cells_terminal: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _counts_and_id(self) -> Self:
        if self.terminal_count != sum(self.parse_status_counts.values()):
            raise ValueError("parse-status counts do not reconcile")
        if self.terminal_count != sum(self.call_status_counts.values()):
            raise ValueError("call-status counts do not reconcile")
        expected_id = make_id(
            "lf022_weak_execution",
            self.model_dump(mode="json", exclude={"execution_id"}),
        )
        if self.execution_id != expected_id:
            raise ValueError("execution_id differs from execution content")
        return self


class LF022WeakExecutionStartedMarker(StrictModel):
    """Durable fail-closed marker written before any live provider process."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_weak_execution_started_v1"] = "lf022_weak_execution_started_v1"
    marker_id: str = Field(pattern=id_pattern("lf022_weak_execution_started"))
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    dispatch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    provider_attempt_may_have_started: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        expected_id = make_id(
            "lf022_weak_execution_started",
            self.model_dump(mode="json", exclude={"marker_id"}),
        )
        if self.marker_id != expected_id:
            raise ValueError("execution-start marker ID differs from content")
        return self


class LF022WeakFinalizationManifest(StrictModel):
    """Offline replay result containing evidence and provisional aggregates only."""

    schema_version: Literal[1] = 1
    finalization_id: str = Field(pattern=id_pattern("lf022_weak_finalization"))
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    execution_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    calls_artifact: Literal["calls.jsonl"] = "calls.jsonl"
    calls_sha256: str = Field(pattern=HEX64_PATTERN)
    attempts_artifact: Literal["attempts.jsonl"] = "attempts.jsonl"
    attempts_sha256: str = Field(pattern=HEX64_PATTERN)
    evidence_artifact: Literal["judgment_evidence.jsonl"] = "judgment_evidence.jsonl"
    evidence_sha256: str = Field(pattern=HEX64_PATTERN)
    candidates_artifact: Literal["weak_consensus_candidates.jsonl"] = (
        "weak_consensus_candidates.jsonl"
    )
    candidates_sha256: str = Field(pattern=HEX64_PATTERN)
    pair_count: int = Field(ge=0, strict=True)
    call_count: int = Field(ge=0, strict=True)
    parsed_evidence_count: int = Field(ge=0, strict=True)
    weak_candidate_count: int = Field(ge=0, strict=True)
    consensus_status_counts: dict[str, int]
    parse_status_counts: dict[str, int]
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _counts_and_id(self) -> Self:
        if self.pair_count != self.weak_candidate_count:
            raise ValueError("finalization requires one weak candidate per pair")
        if self.weak_candidate_count != sum(self.consensus_status_counts.values()):
            raise ValueError("consensus-status counts do not reconcile")
        if self.call_count != sum(self.parse_status_counts.values()):
            raise ValueError("parse-status counts do not reconcile")
        expected_id = make_id(
            "lf022_weak_finalization",
            self.model_dump(mode="json", exclude={"finalization_id"}),
        )
        if self.finalization_id != expected_id:
            raise ValueError("finalization_id differs from content")
        return self


def _canonical_model_bytes(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _canonical_jsonl(models: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_model_bytes(model) for model in models)


def _persist_immutable(path: Path, payload: bytes, *, label: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise LF022WeakBatchError(f"immutable {label} conflicts at {path}")
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
                raise LF022WeakBatchError(
                    f"concurrent immutable {label} conflict at {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_canonical_model(path: Path, model_type: type[StrictModel]) -> StrictModel:
    if path.is_symlink() or not path.is_file():
        raise LF022WeakBatchError(f"artifact is missing or unsafe: {path}")
    raw = path.read_bytes()
    try:
        model = model_type.model_validate_json(raw)
    except ValueError as exc:
        raise LF022WeakBatchError(f"invalid {model_type.__name__} at {path}: {exc}") from exc
    if raw not in {_canonical_model_bytes(model), _canonical_model_bytes(model).rstrip(b"\n")}:
        raise LF022WeakBatchError(f"artifact is not canonical JSON: {path}")
    return model


def persist_lf022_weak_execution_started_marker(
    *,
    batch_root: Path,
    dispatch_manifest: LF022WeakDispatchManifest,
) -> Path:
    """Persist the deterministic batch marker before the first live provider call.

    The marker is intentionally conservative: a crash after this write but
    before the provider process starts still leaves the batch requiring review.
    That false positive is preferable to silently re-selecting a pair whose
    external execution may already have begun.
    """

    dispatch_path = batch_root / "dispatch_manifest.json"
    if dispatch_path.is_symlink() or not dispatch_path.is_file():
        raise LF022WeakBatchError("execution-start marker dispatch manifest is missing or unsafe")
    values: dict[str, object] = {
        "schema_version": 1,
        "method_version": "lf022_weak_execution_started_v1",
        "batch_id": dispatch_manifest.batch_id,
        "dispatch_manifest_sha256": hash_file(dispatch_path),
        "provider_attempt_may_have_started": True,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    marker = LF022WeakExecutionStartedMarker.model_validate(
        {
            **values,
            "marker_id": make_id("lf022_weak_execution_started", values),
        }
    )
    marker_path = batch_root / "execution_started.json"
    _persist_immutable(
        marker_path,
        _canonical_model_bytes(marker),
        label="weak execution-start marker",
    )
    return marker_path


def _load_canonical_jsonl(path: Path, model_type: type[StrictModel]) -> tuple[StrictModel, ...]:
    if path.is_symlink() or not path.is_file():
        raise LF022WeakBatchError(f"JSONL artifact is missing or unsafe: {path}")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise LF022WeakBatchError(f"JSONL artifact lacks terminal newline: {path}")
    records: list[StrictModel] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            records.append(model_type.model_validate_json(line))
        except ValueError as exc:
            raise LF022WeakBatchError(
                f"invalid {model_type.__name__} at {path}:{line_number}: {exc}"
            ) from exc
    if raw != _canonical_jsonl(records):
        raise LF022WeakBatchError(f"JSONL artifact is not canonical: {path}")
    return tuple(records)


def _resolve_bound(binding: BoundArtifact, *, root: Path) -> Path:
    path = Path(binding.path)
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or not path.is_file():
        raise LF022WeakBatchError(f"bound artifact is missing or unsafe: {path}")
    if hash_file(path) != binding.sha256:
        raise LF022WeakBatchError(f"bound artifact hash mismatch: {path}")
    return path


def _load_spec(path: Path, *, expected_sha256: str) -> LF022WeakBatchSpec:
    if hash_file(path) != expected_sha256:
        raise LF022WeakBatchError("weak-batch spec hash differs from expected SHA-256")
    model = _load_canonical_model(path, LF022WeakBatchSpec)
    assert isinstance(model, LF022WeakBatchSpec)
    return model


def _validate_weak_config(path: Path) -> None:
    document = load_yaml_mapping(path)
    if document.get("protocol_version") != "weak_supervision_v1":
        raise LF022WeakBatchError("weak-supervision config protocol differs")
    admission = document.get("admission")
    promotion = document.get("promotion")
    aggregation = document.get("aggregation")
    if not isinstance(admission, dict) or admission.get("live_calls_authorized") is not False:
        raise LF022WeakBatchError("foundation batch requires live calls to remain disabled")
    if (
        not isinstance(promotion, dict)
        or promotion.get("human_pilot_required_before_promotion") is not True
    ):
        raise LF022WeakBatchError("weak-supervision promotion boundary differs")
    if (
        not isinstance(aggregation, dict)
        or aggregation.get("automatic_silver_promotion") is not False
    ):
        raise LF022WeakBatchError("automatic silver promotion must remain disabled")


def _validate_family_pins(
    spec: LF022WeakBatchSpec, matrix_path: Path
) -> LF022ProductionFamilyMatrix:
    try:
        matrix = LF022ProductionFamilyMatrix.model_validate_json(matrix_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise LF022WeakBatchError(f"invalid production family matrix: {exc}") from exc
    by_id = matrix.pins_by_id
    for endpoint in (spec.judge_a, spec.judge_b):
        item = by_id.get(endpoint.family_id)
        if item is None:
            raise LF022WeakBatchError(
                f"judge family is absent from production matrix: {endpoint.family_id}"
            )
        if item.provider_id != endpoint.provider or item.model_id != endpoint.model:
            raise LF022WeakBatchError(
                f"judge provider/model differs from production matrix: {endpoint.family_id}"
            )
        expected_revision = item.checkpoint_revision
        if expected_revision is None and item.provider_catalog_artifact is not None:
            expected_revision = (
                f"provider-deployment-snapshot:{item.provider_catalog_artifact.sha256}"
            )
        if endpoint.revision != expected_revision:
            raise LF022WeakBatchError(
                f"judge revision pin differs from production matrix: {endpoint.family_id}"
            )
        if endpoint.family_id not in matrix.judge_family_ids:
            raise LF022WeakBatchError(
                f"judge endpoint is not admitted for the judge role: {endpoint.family_id}"
            )
    if matrix.heldout_eval_family_id != spec.primary_eval_family_id:
        raise LF022WeakBatchError("primary evaluation family differs from production matrix")
    if matrix.heldout_eval_supervision_excluded is not True:
        raise LF022WeakBatchError("production matrix does not exclude the held-out judge")
    return matrix


def _validate_candidate_inventory_records(
    *,
    manifest: LF022SupervisionCandidateManifest,
    candidates: Sequence[LF022SupervisionCandidateRecord],
) -> None:
    """Require one internally coherent candidate inventory.

    Manifest v4 is an authoring wrapper over byte-preserved source-neutral v3
    records.  Its schema rename removes a false circular spec-hash claim; it
    intentionally does not rewrite the selected record bytes.
    """

    if len(candidates) != manifest.record_count:
        raise LF022WeakBatchError("candidate record count differs from candidate manifest")
    if len({item.candidate_inventory_record_id for item in candidates}) != len(candidates):
        raise LF022WeakBatchError("candidate inventory repeats one record ID")
    expected_record_schema = 3 if manifest.schema_version == 4 else manifest.schema_version
    for candidate in candidates:
        if candidate.schema_version != expected_record_schema:
            raise LF022WeakBatchError(
                "candidate record schema differs from candidate manifest schema"
            )
        if (
            candidate.collection_id != manifest.collection_id
            or candidate.proposer_family_id != manifest.proposer_family_id
            or candidate.proposer_model != manifest.proposer_model
        ):
            raise LF022WeakBatchError(
                "candidate record collection/proposer differs from candidate manifest"
            )
    status_counts = dict(sorted(Counter(item.dispatch_status for item in candidates).items()))
    if status_counts != manifest.dispatch_status_counts:
        raise LF022WeakBatchError("candidate-record dispatch counts differ from candidate manifest")
    payload_groups: dict[str, list[LF022SupervisionCandidateRecord]] = defaultdict(list)
    for candidate in candidates:
        payload_groups[candidate.judge_visible_payload_sha256].append(candidate)
    if len(payload_groups) != manifest.unique_judge_visible_payload_count:
        raise LF022WeakBatchError(
            "candidate-record unique payload count differs from candidate manifest"
        )
    if len(candidates) - len(payload_groups) != manifest.exact_duplicate_record_count:
        raise LF022WeakBatchError(
            "candidate-record duplicate count differs from candidate manifest"
        )
    dispatch_count = status_counts.get("ready_for_two_family_judging", 0)
    if (
        dispatch_count != manifest.dispatch_eligible_count
        or 4 * dispatch_count != manifest.required_future_judge_call_count
    ):
        raise LF022WeakBatchError(
            "candidate-record dispatch workload differs from candidate manifest"
        )
    diagnostic_count = sum(item.prior_codex_diagnostic is not None for item in candidates)
    expected_diagnostic_count = (
        manifest.record_count
        if manifest.schema_version == 2
        else manifest.codex_diagnostic_record_count
    )
    if diagnostic_count != expected_diagnostic_count:
        raise LF022WeakBatchError(
            "candidate-record Codex diagnostics differ from candidate manifest"
        )
    diagnostic_counts = dict(
        sorted(
            Counter(
                item.prior_codex_diagnostic.same_claim_answer
                for item in candidates
                if item.prior_codex_diagnostic is not None
            ).items()
        )
    )
    if diagnostic_counts != manifest.codex_same_claim_counts:
        raise LF022WeakBatchError(
            "candidate-record Codex verdict counts differ from candidate manifest"
        )
    for group in payload_groups.values():
        ready = [item for item in group if item.dispatch_status == "ready_for_two_family_judging"]
        if len(ready) != 1:
            raise LF022WeakBatchError(
                "candidate payload must contain exactly one canonical dispatch record"
            )
        canonical = ready[0]
        if manifest.schema_version == 2:
            assert canonical.prior_codex_diagnostic is not None
            assert canonical.canonical_dispatch_audit_item_id is not None
            canonical_source_id = canonical.prior_codex_diagnostic.audit_item_id
            if canonical.canonical_dispatch_audit_item_id != canonical_source_id:
                raise LF022WeakBatchError("candidate v2 canonical source differs")
        else:
            assert canonical.source_candidate_item_id is not None
            assert canonical.canonical_dispatch_source_item_id is not None
            canonical_source_id = canonical.source_candidate_item_id
            if canonical.canonical_dispatch_source_item_id != canonical_source_id:
                raise LF022WeakBatchError("candidate v3 canonical source differs")
        for candidate in group:
            bound_source_id = (
                candidate.canonical_dispatch_audit_item_id
                if manifest.schema_version == 2
                else candidate.canonical_dispatch_source_item_id
            )
            if (
                candidate.canonical_dispatch_pair_id != canonical.pair_id
                or bound_source_id != canonical_source_id
            ):
                raise LF022WeakBatchError(
                    "candidate duplicate does not bind its payload's canonical dispatch record"
                )


def _endpoint(spec: LF022WeakBatchSpec, slot: JudgeSlot) -> JudgeEndpointPin:
    return spec.judge_a if slot == "judge_A" else spec.judge_b


def _request_for_task(task: JudgePresentation, endpoint: JudgeEndpointPin) -> ProviderRequest:
    rendered = render_blinded_judge_prompt(task)
    return ProviderRequest.create(
        identity=ProviderIdentity(
            provider=endpoint.provider,
            model=endpoint.model,
            revision=endpoint.revision,
            transport="fixture",
        ),
        prompt_template_hash=rendered.template_sha256,
        rendered_prompt=rendered.text,
        decoding=endpoint.decoding,
        input_ids=judge_provider_input_ids(task),
        private_source_content=False,
    )


def prepare_lf022_weak_batch(
    *,
    repo_root: Path,
    spec_path: Path,
    expected_spec_sha256: str,
    randomization_key: bytes,
    output_dir: Path,
) -> tuple[tuple[LF022WeakDispatchRecord, ...], LF022WeakDispatchManifest]:
    """Prepare and immutably write every blinded task and provider request."""

    repo_root = repo_root.resolve()
    spec_path = spec_path if spec_path.is_absolute() else repo_root / spec_path
    spec = _load_spec(spec_path, expected_sha256=expected_spec_sha256)
    if len(randomization_key) < 32:
        raise LF022WeakBatchError("randomization key must contain at least 32 bytes")
    if sha256_hex(randomization_key) != spec.randomization_key_sha256:
        raise LF022WeakBatchError("randomization key differs from frozen hash")

    candidate_manifest_path = _resolve_bound(spec.candidate_manifest, root=repo_root)
    candidate_records_path = _resolve_bound(spec.candidate_records, root=repo_root)
    weak_config_path = _resolve_bound(spec.weak_supervision_config, root=repo_root)
    family_matrix_path = _resolve_bound(spec.production_family_matrix, root=repo_root)
    _validate_weak_config(weak_config_path)
    production_matrix = _validate_family_pins(spec, family_matrix_path)

    candidate_manifest_model = _load_canonical_model(
        candidate_manifest_path, LF022SupervisionCandidateManifest
    )
    assert isinstance(candidate_manifest_model, LF022SupervisionCandidateManifest)
    candidate_manifest = candidate_manifest_model
    candidate_models = _load_canonical_jsonl(
        candidate_records_path, LF022SupervisionCandidateRecord
    )
    candidates = tuple(
        item for item in candidate_models if isinstance(item, LF022SupervisionCandidateRecord)
    )
    if candidate_manifest.records_sha256 != spec.candidate_records.sha256:
        raise LF022WeakBatchError("candidate manifest records hash differs from batch spec")
    if candidate_manifest.proposer_family_id not in production_matrix.proposer_family_ids:
        raise LF022WeakBatchError(
            "candidate proposer is not admitted for the proposer role: "
            f"{candidate_manifest.proposer_family_id}"
        )
    _validate_candidate_inventory_records(
        manifest=candidate_manifest,
        candidates=candidates,
    )
    family_matrix = FamilySeparationMatrix(
        proposer_family=candidate_manifest.proposer_family_id,
        judge_a_family=spec.judge_a.family_id,
        judge_b_family=spec.judge_b.family_id,
        primary_eval_judge_family=spec.primary_eval_family_id,
    )
    validate_family_separation(family_matrix)
    if (
        candidate_manifest.judge_a_family_id != spec.judge_a.family_id
        or candidate_manifest.judge_b_family_id != spec.judge_b.family_id
        or candidate_manifest.primary_eval_judge_family_id != spec.primary_eval_family_id
    ):
        raise LF022WeakBatchError("candidate inventory differs from frozen judge-family design")

    output_dir.mkdir(parents=True, exist_ok=True)
    _persist_immutable(output_dir / "batch_spec.json", spec_path.read_bytes(), label="batch spec")
    _persist_immutable(
        output_dir / "inputs/candidate_manifest.json",
        candidate_manifest_path.read_bytes(),
        label="candidate manifest input",
    )
    _persist_immutable(
        output_dir / "inputs/candidate_records.jsonl",
        candidate_records_path.read_bytes(),
        label="candidate records input",
    )
    _persist_immutable(
        output_dir / "inputs/weak_supervision.yaml",
        weak_config_path.read_bytes(),
        label="weak-supervision config input",
    )
    _persist_immutable(
        output_dir / "inputs/production_family_matrix.json",
        family_matrix_path.read_bytes(),
        label="production family matrix input",
    )
    records: list[LF022WeakDispatchRecord] = []
    pair_ids: set[str] = set()
    for candidate in candidates:
        if candidate.dispatch_status != "ready_for_two_family_judging":
            continue
        if candidate.required_judgment_cells != _REQUIRED_CELLS:
            raise LF022WeakBatchError("dispatch candidate lacks the exact four-cell requirement")
        if candidate.proposer_family_id != family_matrix.proposer_family:
            raise LF022WeakBatchError("candidate proposer differs from family matrix")
        if candidate.pair_id in pair_ids:
            raise LF022WeakBatchError("more than one dispatch candidate uses the same pair_id")
        pair_ids.add(candidate.pair_id)
        for slot in ("judge_A", "judge_B"):
            typed_slot: JudgeSlot = slot
            endpoint = _endpoint(spec, typed_slot)
            tasks = make_swapped_presentations(
                source=candidate.pair,
                judge_slot=typed_slot,
                randomization_key=randomization_key,
            )
            for task in tasks:
                request = _request_for_task(task, endpoint)
                request_artifact = f"requests/{task.task_id}.json"
                request_path = output_dir / request_artifact
                request_sha256 = persist_provider_request(request, request_path)
                rendered = render_blinded_judge_prompt(task)
                values: dict[str, object] = {
                    "schema_version": 1,
                    "method_version": WEAK_BATCH_METHOD_VERSION,
                    "candidate_inventory_record_id": candidate.candidate_inventory_record_id,
                    "pair_id": candidate.pair_id,
                    "proposer_family_id": candidate.proposer_family_id,
                    "judge_family_id": endpoint.family_id,
                    "judge_slot": typed_slot,
                    "orientation": task.orientation,
                    "task": task.model_dump(mode="json"),
                    "request_artifact": request_artifact,
                    "request_artifact_sha256": request_sha256,
                    "provider_request_hash": request.request_hash,
                    "provider_attempt_id": request.attempt_id,
                    "prompt_template_sha256": rendered.template_sha256,
                    "prompt_render_sha256": rendered.render_sha256,
                    "source_admission_sha256": candidate.pair.admission_sha256,
                    "semantic_label_created": False,
                    "silver_promoted": False,
                    "train_eligible": False,
                    "eval_eligible": False,
                    "gate_credit_claimed": False,
                }
                cell_id = make_id("lf022_weak_cell", values)
                records.append(
                    LF022WeakDispatchRecord.model_validate({**values, "dispatch_cell_id": cell_id})
                )
    records.sort(key=lambda item: item.dispatch_cell_id)
    record_bytes = _canonical_jsonl(records)
    records_sha256 = sha256_hex(record_bytes)
    manifest_values: dict[str, object] = {
        "schema_version": 1,
        "method_version": WEAK_BATCH_METHOD_VERSION,
        "batch_name": spec.batch_name,
        "spec_sha256": expected_spec_sha256,
        "candidate_manifest_sha256": spec.candidate_manifest.sha256,
        "candidate_records_sha256": spec.candidate_records.sha256,
        "weak_supervision_config_sha256": spec.weak_supervision_config.sha256,
        "production_family_matrix_sha256": spec.production_family_matrix.sha256,
        "randomization_key_sha256": spec.randomization_key_sha256,
        "proposer_family_id": family_matrix.proposer_family,
        "judge_a_family_id": family_matrix.judge_a_family,
        "judge_b_family_id": family_matrix.judge_b_family,
        "primary_eval_family_id": family_matrix.primary_eval_judge_family,
        "candidate_manifest_artifact": "inputs/candidate_manifest.json",
        "candidate_records_artifact": "inputs/candidate_records.jsonl",
        "weak_supervision_config_artifact": "inputs/weak_supervision.yaml",
        "production_family_matrix_artifact": "inputs/production_family_matrix.json",
        "dispatch_records_artifact": "dispatch_records.jsonl",
        "dispatch_records_sha256": records_sha256,
        "dispatch_pair_count": len(pair_ids),
        "dispatch_cell_count": len(records),
        "required_cell_count": len(records),
        "live_provider_calls_authorized": spec.live_provider_calls_authorized,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    batch_id = make_id(
        "lf022_weak_batch",
        {key: value for key, value in manifest_values.items() if key != "spec_sha256"},
    )
    manifest = LF022WeakDispatchManifest.model_validate({**manifest_values, "batch_id": batch_id})
    _persist_immutable(
        output_dir / "dispatch_records.jsonl", record_bytes, label="dispatch records"
    )
    _persist_immutable(
        output_dir / "dispatch_manifest.json",
        _canonical_model_bytes(manifest),
        label="dispatch manifest",
    )
    return tuple(records), manifest


def _load_prepared_batch(
    batch_root: Path,
) -> tuple[
    LF022WeakBatchSpec,
    LF022WeakDispatchManifest,
    tuple[LF022WeakDispatchRecord, ...],
    dict[str, LF022SupervisionCandidateRecord],
]:
    manifest_model = _load_canonical_model(
        batch_root / "dispatch_manifest.json", LF022WeakDispatchManifest
    )
    assert isinstance(manifest_model, LF022WeakDispatchManifest)
    manifest = manifest_model
    dispatch_models = _load_canonical_jsonl(
        batch_root / manifest.dispatch_records_artifact, LF022WeakDispatchRecord
    )
    dispatches = tuple(
        item for item in dispatch_models if isinstance(item, LF022WeakDispatchRecord)
    )
    if (
        hash_file(batch_root / manifest.dispatch_records_artifact)
        != manifest.dispatch_records_sha256
    ):
        raise LF022WeakBatchError("dispatch records hash differs from manifest")
    if len(dispatches) != manifest.dispatch_cell_count:
        raise LF022WeakBatchError("dispatch record count differs from manifest")

    # The spec can be outside the batch root, so discover it from the caller's
    # repository binding recorded in a small immutable copy created by CLI or tests.
    spec_path = batch_root / "batch_spec.json"
    spec = _load_spec(spec_path, expected_sha256=manifest.spec_sha256)
    copied_inputs = {
        manifest.candidate_manifest_artifact: manifest.candidate_manifest_sha256,
        manifest.candidate_records_artifact: manifest.candidate_records_sha256,
        manifest.weak_supervision_config_artifact: manifest.weak_supervision_config_sha256,
        manifest.production_family_matrix_artifact: manifest.production_family_matrix_sha256,
    }
    for artifact, expected_hash in copied_inputs.items():
        path = batch_root / artifact
        if path.is_symlink() or not path.is_file() or hash_file(path) != expected_hash:
            raise LF022WeakBatchError(f"self-contained input differs: {artifact}")
    _validate_weak_config(batch_root / manifest.weak_supervision_config_artifact)
    production_matrix = _validate_family_pins(
        spec, batch_root / manifest.production_family_matrix_artifact
    )
    candidate_manifest_model = _load_canonical_model(
        batch_root / manifest.candidate_manifest_artifact,
        LF022SupervisionCandidateManifest,
    )
    assert isinstance(candidate_manifest_model, LF022SupervisionCandidateManifest)
    if (
        candidate_manifest_model.records_sha256 != manifest.candidate_records_sha256
        or candidate_manifest_model.proposer_family_id != manifest.proposer_family_id
        or candidate_manifest_model.judge_a_family_id != manifest.judge_a_family_id
        or candidate_manifest_model.judge_b_family_id != manifest.judge_b_family_id
        or candidate_manifest_model.primary_eval_judge_family_id != manifest.primary_eval_family_id
    ):
        raise LF022WeakBatchError("self-contained candidate manifest differs")
    if candidate_manifest_model.proposer_family_id not in production_matrix.proposer_family_ids:
        raise LF022WeakBatchError(
            "self-contained candidate proposer is not admitted for the proposer role"
        )
    candidate_path = batch_root / manifest.candidate_records_artifact
    if hash_file(candidate_path) != manifest.candidate_records_sha256:
        raise LF022WeakBatchError("self-contained candidate records hash differs")
    candidate_models = _load_canonical_jsonl(candidate_path, LF022SupervisionCandidateRecord)
    ordered_candidates = tuple(
        item for item in candidate_models if isinstance(item, LF022SupervisionCandidateRecord)
    )
    _validate_candidate_inventory_records(
        manifest=candidate_manifest_model,
        candidates=ordered_candidates,
    )
    candidates = {item.candidate_inventory_record_id: item for item in ordered_candidates}
    for dispatch in dispatches:
        candidate = candidates.get(dispatch.candidate_inventory_record_id)
        if candidate is None or candidate.pair_id != dispatch.pair_id:
            raise LF022WeakBatchError("dispatch cell lacks its exact candidate record")
        _verify_dispatch_request(batch_root, dispatch, spec)
    return spec, manifest, dispatches, candidates


def _verify_dispatch_request(
    batch_root: Path,
    dispatch: LF022WeakDispatchRecord,
    spec: LF022WeakBatchSpec,
) -> ProviderRequest:
    path = batch_root / dispatch.request_artifact
    if hash_file(path) != dispatch.request_artifact_sha256:
        raise LF022WeakBatchError("provider request artifact hash differs from dispatch cell")
    request = load_provider_request(path)
    endpoint = _endpoint(spec, dispatch.judge_slot)
    rendered = render_blinded_judge_prompt(dispatch.task)
    expected_values = (
        endpoint.provider,
        endpoint.model,
        endpoint.revision,
        endpoint.decoding,
        endpoint.family_id,
        dispatch.task.orientation,
    )
    observed_values = (
        request.provider,
        request.model,
        request.revision,
        request.decoding,
        dispatch.judge_family_id,
        dispatch.orientation,
    )
    if observed_values != expected_values:
        raise LF022WeakBatchError("dispatch request family/orientation pin differs")
    if (
        request.request_hash != dispatch.provider_request_hash
        or request.attempt_id != dispatch.provider_attempt_id
        or request.input_ids != judge_provider_input_ids(dispatch.task)
        or request.prompt_template_hash != dispatch.prompt_template_sha256
        or request.prompt_render_hash != dispatch.prompt_render_sha256
        or rendered.template_sha256 != dispatch.prompt_template_sha256
        or rendered.render_sha256 != dispatch.prompt_render_sha256
    ):
        raise LF022WeakBatchError("dispatch request hash/prompt/input binding differs")
    return request


def _terminal_path(batch_root: Path, cell_id: str) -> Path:
    return batch_root / "terminals" / f"{cell_id}.json"


def _verify_terminal(
    *,
    batch_root: Path,
    manifest: LF022WeakDispatchManifest,
    spec: LF022WeakBatchSpec,
    dispatch: LF022WeakDispatchRecord,
    terminal: LF022WeakTerminalRecord,
) -> None:
    request = _verify_dispatch_request(batch_root, dispatch, spec)
    if (
        terminal.batch_id != manifest.batch_id
        or terminal.dispatch_cell_id != dispatch.dispatch_cell_id
    ):
        raise LF022WeakBatchError("terminal differs from prepared batch/cell")
    endpoint = _endpoint(spec, dispatch.judge_slot)
    call = terminal.call
    if (
        call.provider_request_hash != dispatch.provider_request_hash
        or call.request_artifact != dispatch.request_artifact
        or call.request_artifact_sha256 != dispatch.request_artifact_sha256
        or call.prompt_template_hash != dispatch.prompt_template_sha256
        or call.prompt_render_hash != dispatch.prompt_render_sha256
        or call.provider_slot != dispatch.judge_slot
        or call.model_family != endpoint.family_id
        or call.input_ids != judge_provider_input_ids(dispatch.task)
        or call.metadata.get("proposer_family") != dispatch.proposer_family_id
        or call.metadata.get("judge_orientation") != dispatch.orientation
        or call.metadata.get("weak_batch_id") != manifest.batch_id
        or call.metadata.get("weak_dispatch_cell_id") != dispatch.dispatch_cell_id
    ):
        raise LF022WeakBatchError("terminal family/orientation/lineage differs from dispatch")
    raw_path = batch_root / (call.raw_output_artifact or "")
    raw = load_provider_raw_response(raw_path, request=request)
    if hash_file(raw_path) != call.raw_response_sha256:
        raise LF022WeakBatchError("terminal raw response hash differs")
    if call.terminal_status is LLMCallStatus.COMPLETED:
        verified = verify_generic_llm_call_artifacts(
            call=call,
            expected_role=LLMRole.JUDGE,
            expected_input_ids=judge_provider_input_ids(dispatch.task),
            private_source_content=False,
            denylist_checked=True,
            denylist_hits=(),
            artifact_root=batch_root,
        )
        if verified != raw:
            raise LF022WeakBatchError("generic replay differs from terminal raw response")


def execute_or_resume_lf022_weak_batch(
    *,
    batch_root: Path,
    providers: Mapping[JudgeSlot, GenerationProvider],
    raw_response_roots: Mapping[JudgeSlot, Path],
    now: Callable[[], datetime.datetime] | None = None,
) -> tuple[tuple[LF022WeakTerminalRecord, ...], LF022WeakExecutionManifest]:
    """Execute injected providers or resume safely from terminal/raw artifacts.

    The CLI exposes only the replay form.  A future live adapter must be
    separately admitted before it can call this library function.
    """

    clock = now or (lambda: datetime.datetime.now(tz=datetime.UTC))
    spec, manifest, dispatches, _ = _load_prepared_batch(batch_root)
    terminals: list[LF022WeakTerminalRecord] = []
    for dispatch in dispatches:
        terminal_path = _terminal_path(batch_root, dispatch.dispatch_cell_id)
        if terminal_path.exists():
            model = _load_canonical_model(terminal_path, LF022WeakTerminalRecord)
            assert isinstance(model, LF022WeakTerminalRecord)
            _verify_terminal(
                batch_root=batch_root,
                manifest=manifest,
                spec=spec,
                dispatch=dispatch,
                terminal=model,
            )
            terminals.append(model)
            continue

        request = _verify_dispatch_request(batch_root, dispatch, spec)
        endpoint = _endpoint(spec, dispatch.judge_slot)
        provider = providers.get(dispatch.judge_slot)
        raw_root = raw_response_roots.get(dispatch.judge_slot)
        if provider is None or raw_root is None:
            raise LF022WeakBatchError(f"missing provider/raw root for {dispatch.judge_slot}")
        expected_identity = (endpoint.provider, endpoint.model, endpoint.revision)
        observed_identity = (
            provider.identity.provider,
            provider.identity.model,
            provider.identity.revision,
        )
        if observed_identity != expected_identity:
            raise LF022WeakBatchError("injected provider differs from frozen endpoint pin")
        if not isinstance(provider, DeterministicFixtureProvider | ReplayProvider) or (
            provider.identity.transport not in {"fixture", "replay"}
        ):
            raise LF022WeakBatchError("offline weak batch accepts only fixture or replay providers")
        expected_raw_path = provider_raw_response_path(raw_root, request)
        started_at = clock()
        if expected_raw_path.exists():
            replay = ReplayProvider(
                identity=ProviderIdentity(
                    provider=endpoint.provider,
                    model=endpoint.model,
                    revision=endpoint.revision,
                    transport="replay",
                ),
                raw_response_root=raw_root,
            )
            result = replay.generate(request)
            execution_mode: LLMExecutionMode = "replay"
        else:
            result = provider.generate(request)
            execution_mode = "replay" if result.replayed else "local"
        completed_at = clock()
        if result.raw_response_path != expected_raw_path:
            raise LF022WeakBatchError("provider wrote outside the canonical raw-first path")
        # Verify raw bytes before parsing.  A parse failure therefore cannot
        # erase the exact provider response.
        load_provider_raw_response(result.raw_response_path, request=request)
        parsed_output: dict[str, object] | None = None
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
        lineage = bridge_provider_result_to_generic_llm_lineage(
            request=request,
            result=result,
            request_artifact_path=batch_root / dispatch.request_artifact,
            artifact_root=batch_root,
            role=LLMRole.JUDGE,
            provider_slot=dispatch.judge_slot,
            model_family=dispatch.judge_family_id,
            prompt_template_id=JUDGE_TEMPLATE_ID,
            prompt_template_version=render_blinded_judge_prompt(dispatch.task).template_version,
            execution_mode=execution_mode,
            parse_status=parse_status,
            parsed_output=parsed_output,
            private_source_content=False,
            denylist_checked=True,
            denylist_hits=(),
            started_at=started_at,
            completed_at=completed_at,
            supervision_eligible=True,
            metadata={
                "weak_supervision_config_hash": manifest.weak_supervision_config_sha256,
                "proposer_family": dispatch.proposer_family_id,
                "judge_orientation": dispatch.orientation,
                "weak_batch_id": manifest.batch_id,
                "weak_dispatch_cell_id": dispatch.dispatch_cell_id,
                "semantic_label_created": False,
                "training_eligible": False,
            },
        )
        terminal_values: dict[str, object] = {
            "schema_version": 1,
            "batch_id": manifest.batch_id,
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
        terminal_id = make_id("lf022_weak_terminal", terminal_values)
        terminal = LF022WeakTerminalRecord.model_validate(
            {**terminal_values, "terminal_id": terminal_id}
        )
        _persist_immutable(
            terminal_path,
            _canonical_model_bytes(terminal),
            label="weak judge terminal",
        )
        terminals.append(terminal)

    terminals.sort(key=lambda item: item.dispatch_cell_id)
    terminal_bytes = _canonical_jsonl(terminals)
    terminal_hash = _persist_immutable(
        batch_root / "terminal_records.jsonl",
        terminal_bytes,
        label="terminal records",
    )
    values: dict[str, object] = {
        "schema_version": 1,
        "batch_id": manifest.batch_id,
        "dispatch_manifest_sha256": hash_file(batch_root / "dispatch_manifest.json"),
        "terminal_records_artifact": "terminal_records.jsonl",
        "terminal_records_sha256": terminal_hash,
        "terminal_count": len(terminals),
        "parse_status_counts": dict(
            sorted(Counter(item.call.parse_status.value for item in terminals).items())
        ),
        "call_status_counts": dict(
            sorted(
                Counter(
                    item.call.terminal_status.value
                    if item.call.terminal_status is not None
                    else "missing"
                    for item in terminals
                ).items()
            )
        ),
        "all_cells_terminal": True,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    execution_id = make_id("lf022_weak_execution", values)
    execution = LF022WeakExecutionManifest.model_validate({**values, "execution_id": execution_id})
    _persist_immutable(
        batch_root / "execution_manifest.json",
        _canonical_model_bytes(execution),
        label="execution manifest",
    )
    return tuple(terminals), execution


def replay_lf022_weak_batch(
    *, batch_root: Path
) -> tuple[tuple[LF022WeakTerminalRecord, ...], LF022WeakExecutionManifest]:
    """Complete terminal lineage using only already-persisted raw responses."""

    spec, _, _, _ = _load_prepared_batch(batch_root)
    providers: dict[JudgeSlot, GenerationProvider] = {}
    roots: dict[JudgeSlot, Path] = {}
    for endpoint in (spec.judge_a, spec.judge_b):
        raw_root = batch_root / "raw" / endpoint.provider_slot
        providers[endpoint.provider_slot] = ReplayProvider(
            identity=ProviderIdentity(
                provider=endpoint.provider,
                model=endpoint.model,
                revision=endpoint.revision,
                transport="replay",
            ),
            raw_response_root=raw_root,
        )
        roots[endpoint.provider_slot] = raw_root
    return execute_or_resume_lf022_weak_batch(
        batch_root=batch_root,
        providers=providers,
        raw_response_roots=roots,
    )


def finalize_lf022_weak_batch(
    *, batch_root: Path
) -> tuple[
    tuple[EvidenceRecord, ...],
    tuple[WeakConsensusCandidateRecord, ...],
    LF022WeakFinalizationManifest,
]:
    """Replay a complete terminal corpus into evidence and weak candidates."""

    spec, dispatch_manifest, dispatches, candidates_by_id = _load_prepared_batch(batch_root)
    execution_model = _load_canonical_model(
        batch_root / "execution_manifest.json", LF022WeakExecutionManifest
    )
    assert isinstance(execution_model, LF022WeakExecutionManifest)
    execution = execution_model
    terminal_models = _load_canonical_jsonl(
        batch_root / execution.terminal_records_artifact, LF022WeakTerminalRecord
    )
    terminals = tuple(item for item in terminal_models if isinstance(item, LF022WeakTerminalRecord))
    if execution.batch_id != dispatch_manifest.batch_id:
        raise LF022WeakBatchError("execution manifest differs from prepared batch")
    if (
        hash_file(batch_root / execution.terminal_records_artifact)
        != execution.terminal_records_sha256
    ):
        raise LF022WeakBatchError("terminal records hash differs from execution manifest")
    if len(terminals) != len(dispatches) or len(terminals) != execution.terminal_count:
        raise LF022WeakBatchError("offline finalization requires one terminal per dispatch cell")
    dispatch_by_id = {item.dispatch_cell_id: item for item in dispatches}
    terminal_by_cell = {item.dispatch_cell_id: item for item in terminals}
    if set(dispatch_by_id) != set(terminal_by_cell):
        raise LF022WeakBatchError("terminal and dispatch cell sets differ")

    family_matrix = FamilySeparationMatrix(
        proposer_family=dispatch_manifest.proposer_family_id,
        judge_a_family=dispatch_manifest.judge_a_family_id,
        judge_b_family=dispatch_manifest.judge_b_family_id,
        primary_eval_judge_family=dispatch_manifest.primary_eval_family_id,
    )
    evidence: list[EvidenceRecord] = []
    evidence_by_pair: dict[str, list[EvidenceRecord]] = defaultdict(list)
    calls: list[LLMCallRecord] = []
    attempts: list[LLMAttemptRecord] = []
    for cell_id in sorted(dispatch_by_id):
        dispatch = dispatch_by_id[cell_id]
        terminal = terminal_by_cell[cell_id]
        _verify_terminal(
            batch_root=batch_root,
            manifest=dispatch_manifest,
            spec=spec,
            dispatch=dispatch,
            terminal=terminal,
        )
        calls.append(terminal.call)
        attempts.append(terminal.attempt)
        if (
            terminal.call.terminal_status is LLMCallStatus.COMPLETED
            and terminal.call.parse_status is ParseStatus.PARSED
        ):
            candidate = candidates_by_id[dispatch.candidate_inventory_record_id]
            item = materialize_verified_judgment_evidence(
                call=terminal.call,
                task=dispatch.task,
                source=candidate.pair,
                family_matrix=family_matrix,
                proposer_family=dispatch.proposer_family_id,
                method_version=WEAK_BATCH_METHOD_VERSION,
                config_hash=dispatch_manifest.weak_supervision_config_sha256,
                artifact_root=batch_root,
                created_at=terminal.call.completed_at or terminal.call.request_date,
            )
            evidence.append(item)
            evidence_by_pair[dispatch.pair_id].append(item)

    pair_candidate_ids: dict[str, str] = {}
    for dispatch in dispatches:
        previous = pair_candidate_ids.setdefault(
            dispatch.pair_id, dispatch.candidate_inventory_record_id
        )
        if previous != dispatch.candidate_inventory_record_id:
            raise LF022WeakBatchError("one pair maps to multiple candidate records")
    weak_candidates: list[WeakConsensusCandidateRecord] = []
    for pair_id in sorted(pair_candidate_ids):
        pair_terminals = [
            terminal_by_cell[item.dispatch_cell_id]
            for item in dispatches
            if item.pair_id == pair_id
        ]
        created_at = max(
            (item.call.completed_at or item.call.request_date) for item in pair_terminals
        )
        weak_candidates.append(
            build_weak_consensus_candidate(
                pair_id=pair_id,
                proposer_family=dispatch_manifest.proposer_family_id,
                family_matrix=family_matrix,
                judgments=tuple(evidence_by_pair.get(pair_id, ())),
                created_at=created_at,
            )
        )
    evidence.sort(key=lambda item: item.evidence_id)
    calls.sort(key=lambda item: item.call_id)
    attempts.sort(key=lambda item: item.attempt_id)
    weak_candidates.sort(key=lambda item: item.pair_id)
    final_root = batch_root / "final"
    calls_hash = _persist_immutable(
        final_root / "calls.jsonl", _canonical_jsonl(calls), label="final calls"
    )
    attempts_hash = _persist_immutable(
        final_root / "attempts.jsonl", _canonical_jsonl(attempts), label="final attempts"
    )
    evidence_hash = _persist_immutable(
        final_root / "judgment_evidence.jsonl",
        _canonical_jsonl(evidence),
        label="judgment evidence",
    )
    candidates_hash = _persist_immutable(
        final_root / "weak_consensus_candidates.jsonl",
        _canonical_jsonl(weak_candidates),
        label="weak consensus candidates",
    )
    values: dict[str, object] = {
        "schema_version": 1,
        "batch_id": dispatch_manifest.batch_id,
        "execution_manifest_sha256": hash_file(batch_root / "execution_manifest.json"),
        "calls_artifact": "calls.jsonl",
        "calls_sha256": calls_hash,
        "attempts_artifact": "attempts.jsonl",
        "attempts_sha256": attempts_hash,
        "evidence_artifact": "judgment_evidence.jsonl",
        "evidence_sha256": evidence_hash,
        "candidates_artifact": "weak_consensus_candidates.jsonl",
        "candidates_sha256": candidates_hash,
        "pair_count": len(pair_candidate_ids),
        "call_count": len(calls),
        "parsed_evidence_count": len(evidence),
        "weak_candidate_count": len(weak_candidates),
        "consensus_status_counts": dict(
            sorted(Counter(item.status for item in weak_candidates).items())
        ),
        "parse_status_counts": dict(
            sorted(Counter(item.parse_status.value for item in calls).items())
        ),
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    finalization_id = make_id("lf022_weak_finalization", values)
    finalization = LF022WeakFinalizationManifest.model_validate(
        {**values, "finalization_id": finalization_id}
    )
    _persist_immutable(
        final_root / "finalization_manifest.json",
        _canonical_model_bytes(finalization),
        label="finalization manifest",
    )
    return tuple(evidence), tuple(weak_candidates), finalization


def copy_batch_spec_for_execution(
    *, spec_path: Path, expected_spec_sha256: str, output_dir: Path
) -> Path:
    """Bind the exact external spec into a self-contained prepared batch."""

    if hash_file(spec_path) != expected_spec_sha256:
        raise LF022WeakBatchError("weak-batch spec hash differs before copy")
    destination = output_dir / "batch_spec.json"
    _persist_immutable(destination, spec_path.read_bytes(), label="batch spec")
    return destination


__all__ = [
    "BoundArtifact",
    "JudgeEndpointPin",
    "LF022WeakBatchError",
    "LF022WeakBatchSpec",
    "LF022WeakDispatchManifest",
    "LF022WeakDispatchRecord",
    "LF022WeakExecutionManifest",
    "LF022WeakExecutionStartedMarker",
    "LF022WeakFinalizationManifest",
    "LF022WeakTerminalRecord",
    "copy_batch_spec_for_execution",
    "execute_or_resume_lf022_weak_batch",
    "finalize_lf022_weak_batch",
    "persist_lf022_weak_execution_started_marker",
    "prepare_lf022_weak_batch",
    "replay_lf022_weak_batch",
]
