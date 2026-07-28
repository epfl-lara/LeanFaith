"""Blinded LF-022 judge tasks, strict parsing, swap audits, and aggregation.

Judge votes are persisted as ``EvidenceRecord`` objects.  Aggregation can
create only ``WeakConsensusCandidateRecord`` objects, which are schema-barred
from training, evaluation, or silver promotion until the registered human
pilot and audit route is completed.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import (
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
    LLMCallStatus,
    LLMRole,
    ParseStatus,
    RelationLabel,
)
from leanfaith.schemas.evidence import EvidenceRecord, JudgmentValue
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    PAIR_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.variant import _check_ecodes
from leanfaith.schemas.weak_supervision import (
    WeakConsensusCandidateRecord,
    WeakConsensusStatus,
    make_weak_consensus_id,
)

JUDGE_TEMPLATE_ID = "lean_pair_blinded"
JUDGE_TEMPLATE_VERSION = "v1"
DEFAULT_JUDGE_TEMPLATE = (
    Path(__file__).resolve().parents[3] / "prompts" / "judges" / "lean_pair_blinded_v1.txt"
)

_MIN_RANDOMIZATION_BYTES = 32
_TASK_DOMAIN = b"leanfaith-lf022-judge-task-v1\0"
_ORDER_DOMAIN = b"leanfaith-lf022-judge-order-v1\0"
_TEMPLATE_HASH_TOKEN = "{{PROMPT_TEMPLATE_SHA256}}"
_INPUT_JSON_TOKEN = "{{INPUT_JSON}}"

JudgeOrientation = Literal["AB", "BA"]
JudgeSlot = Literal["judge_A", "judge_B"]


class JudgePromptErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    EXTERNAL_TRANSMISSION_FORBIDDEN = "external_transmission_forbidden"
    TEMPLATE_NOT_FOUND = "template_not_found"
    TEMPLATE_NOT_UTF8 = "template_not_utf8"
    TEMPLATE_CONTRACT = "template_contract"


class JudgePromptError(ValueError):
    def __init__(self, code: JudgePromptErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {detail}")


class JudgeOutputErrorCode(StrEnum):
    EMPTY_OUTPUT = "empty_output"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    INCOHERENT = "incoherent"


class JudgeOutputParseError(ValueError):
    def __init__(self, code: JudgeOutputErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {detail}")


class JudgeResponse(StrictModel):
    """Strict role-specific schema for one visible-order judgment."""

    same_claim_answer: Literal[
        "same_claim",
        "not_same_claim",
        "ambiguous",
        "uncertain",
    ]
    relation: RelationLabel | None
    a_implies_b: Literal["yes", "no", "unknown"] = Field(alias="A_implies_B")
    b_implies_a: Literal["yes", "no", "unknown"] = Field(alias="B_implies_A")
    error_types: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=1200)
    needs_expert_review: bool

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len(set(self.error_types)) != len(self.error_types):
            raise ValueError("error_types must be unique")
        _check_ecodes(tuple(sorted(self.error_types)))
        if self.same_claim_answer == "same_claim":
            if self.relation is not RelationLabel.EQUIVALENT:
                raise ValueError("same_claim requires relation=equivalent")
        elif self.same_claim_answer == "not_same_claim":
            if self.relation not in {
                RelationLabel.A_STRONGER,
                RelationLabel.B_STRONGER,
                RelationLabel.INCOMPARABLE,
                RelationLabel.UNRELATED,
            }:
                raise ValueError("not_same_claim requires a non-equivalent terminal relation")
        elif self.same_claim_answer == "ambiguous":
            if self.relation is not RelationLabel.AMBIGUOUS:
                raise ValueError("ambiguous requires relation=ambiguous")
            if not self.needs_expert_review:
                raise ValueError("ambiguous judgments require expert review")
        elif self.relation is not None:
            raise ValueError("uncertain requires relation=null")
        if self.same_claim_answer == "uncertain" and not self.needs_expert_review:
            raise ValueError("uncertain judgments require expert review")
        return self

    def to_evidence_value(self) -> JudgmentValue:
        return JudgmentValue(
            answer=self.same_claim_answer,
            relation=self.relation.value if self.relation is not None else None,
            a_implies_b=self.a_implies_b,
            b_implies_a=self.b_implies_a,
            error_types=tuple(sorted(self.error_types)),
            confidence=self.confidence,
            rationale=self.rationale,
            needs_expert_review=self.needs_expert_review,
        )


class JudgePresentation(StrictModel):
    """Private task record; only ``visible_payload`` is sent to the judge."""

    schema_version: Literal[1] = 1
    task_id: str = Field(pattern=id_pattern("judge_task"))
    opaque_task_token: str = Field(pattern=r"^lf022_judge_item_v1:[0-9a-f]{64}$")
    pair_id: str = Field(pattern=id_pattern(PAIR_PREFIX))
    judge_slot: JudgeSlot
    orientation: JudgeOrientation
    lean_a: str = Field(min_length=1)
    lean_b: str = Field(min_length=1)
    optional_natural_language: str | None = None
    randomization_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_admission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_transmission_allowed: Literal[True] = True

    @model_validator(mode="after")
    def _safe(self) -> Self:
        for name in ("lean_a", "lean_b"):
            value = getattr(self, name)
            if not value.strip() or "\x00" in value:
                raise ValueError(f"{name} must be safe nonempty text")
        if self.optional_natural_language is not None and (
            not self.optional_natural_language.strip() or "\x00" in self.optional_natural_language
        ):
            raise ValueError("optional_natural_language must be null or safe nonempty text")
        return self

    def visible_payload(self) -> dict[str, object]:
        """Return the exact provenance-free object rendered into the prompt."""

        return {
            "opaque_task_token": self.opaque_task_token,
            "lean_a": self.lean_a,
            "lean_b": self.lean_b,
            "optional_natural_language": self.optional_natural_language,
        }


@dataclass(frozen=True, slots=True)
class RenderedJudgePrompt:
    template_id: str
    template_version: str
    template_sha256: str
    render_sha256: str
    task_id: str
    text: str


@dataclass(frozen=True, slots=True)
class FamilySeparationMatrix:
    proposer_family: str
    judge_a_family: str
    judge_b_family: str
    primary_eval_judge_family: str


class PublicLeanJudgePair(StrictModel):
    """Authoritative public-source admission projected into a judge task."""

    pair_id: str = Field(pattern=id_pattern(PAIR_PREFIX))
    canonical_lean_a: str = Field(min_length=1)
    canonical_lean_b: str = Field(min_length=1)
    optional_natural_language: str | None = None
    source_record_ids: tuple[str, ...] = Field(min_length=1)
    source_is_public: Literal[True]
    private_source_content: Literal[False]
    external_transmission_allowed: Literal[True]
    denylist_checked: Literal[True]
    denylist_hits: tuple[()] = ()

    @model_validator(mode="after")
    def _safe_and_canonical(self) -> Self:
        for name in ("canonical_lean_a", "canonical_lean_b"):
            value = getattr(self, name)
            if not value.strip() or "\x00" in value:
                raise ValueError(f"{name} must be safe nonempty text")
        if self.optional_natural_language is not None and (
            not self.optional_natural_language.strip() or "\x00" in self.optional_natural_language
        ):
            raise ValueError("optional_natural_language must be null or safe nonempty text")
        if tuple(sorted(set(self.source_record_ids))) != self.source_record_ids:
            raise ValueError("source_record_ids must be sorted and unique")
        return self

    @property
    def admission_sha256(self) -> str:
        return sha256_hex(canonical_json_bytes(self.model_dump(mode="json")))


def validate_family_separation(matrix: FamilySeparationMatrix) -> None:
    """Enforce the four-family confirmatory supervision boundary."""

    values = {
        "proposer": matrix.proposer_family.strip(),
        "judge_A": matrix.judge_a_family.strip(),
        "judge_B": matrix.judge_b_family.strip(),
        "primary_eval": matrix.primary_eval_judge_family.strip(),
    }
    if any(not value for value in values.values()):
        raise ValueError("all family-separation roles must be nonempty")
    if len(set(values.values())) != len(values):
        raise ValueError(
            "proposer, judge_A, judge_B, and primary_eval must be four distinct families"
        )


def _task_token(*, entropy: bytes, pair_id: str, judge_slot: str, orientation: str) -> str:
    message = b"\0".join(
        (pair_id.encode("utf-8"), judge_slot.encode("utf-8"), orientation.encode("utf-8"))
    )
    return (
        "lf022_judge_item_v1:"
        + hmac.new(entropy, _TASK_DOMAIN + message, hashlib.sha256).hexdigest()
    )


def make_swapped_presentations(
    *,
    source: PublicLeanJudgePair,
    judge_slot: JudgeSlot,
    randomization_key: bytes,
) -> tuple[JudgePresentation, JudgePresentation]:
    """Build an AB/BA pair and return it in HMAC-randomized dispatch order."""

    if len(randomization_key) < _MIN_RANDOMIZATION_BYTES:
        raise ValueError(
            f"randomization_key must contain at least {_MIN_RANDOMIZATION_BYTES} bytes"
        )
    pair_id = source.pair_id
    key_hash = sha256_hex(randomization_key)
    tasks: list[JudgePresentation] = []
    for orientation, lean_a, lean_b in (
        ("AB", source.canonical_lean_a, source.canonical_lean_b),
        ("BA", source.canonical_lean_b, source.canonical_lean_a),
    ):
        orientation_value: JudgeOrientation = "AB" if orientation == "AB" else "BA"
        token = _task_token(
            entropy=randomization_key,
            pair_id=pair_id,
            judge_slot=judge_slot,
            orientation=orientation_value,
        )
        task_id = make_id(
            "judge_task",
            {
                "schema": "judge_task_v1",
                "pair_id": pair_id,
                "judge_slot": judge_slot,
                "orientation": orientation_value,
                "randomization_key_sha256": key_hash,
                "opaque_task_token": token,
            },
        )
        tasks.append(
            JudgePresentation(
                task_id=task_id,
                opaque_task_token=token,
                pair_id=pair_id,
                judge_slot=judge_slot,
                orientation=orientation_value,
                lean_a=lean_a,
                lean_b=lean_b,
                optional_natural_language=source.optional_natural_language,
                randomization_key_sha256=key_hash,
                source_admission_sha256=source.admission_sha256,
                external_transmission_allowed=True,
            )
        )
    tasks.sort(
        key=lambda task: hmac.new(
            randomization_key,
            _ORDER_DOMAIN + task.task_id.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    )
    return tasks[0], tasks[1]


def _load_template(path: Path) -> tuple[str, str]:
    if not path.is_file():
        raise JudgePromptError(
            JudgePromptErrorCode.TEMPLATE_NOT_FOUND,
            f"template is not a regular file: {path}",
        )
    raw = path.read_bytes()
    try:
        template = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JudgePromptError(
            JudgePromptErrorCode.TEMPLATE_NOT_UTF8,
            f"template is not UTF-8: {path}",
        ) from exc
    expected = {_TEMPLATE_HASH_TOKEN, _INPUT_JSON_TOKEN}
    observed = set(re.findall(r"\{\{[A-Z0-9_]+\}\}", template))
    if observed != expected or any(template.count(token) != 1 for token in expected):
        raise JudgePromptError(
            JudgePromptErrorCode.TEMPLATE_CONTRACT,
            "template must contain each required placeholder exactly once and no others",
        )
    return template, sha256_hex(raw)


def render_blinded_judge_prompt(
    task: JudgePresentation,
    *,
    template_path: Path = DEFAULT_JUDGE_TEMPLATE,
) -> RenderedJudgePrompt:
    """Render only the visible, provenance-free task projection."""

    if not task.external_transmission_allowed:
        raise JudgePromptError(
            JudgePromptErrorCode.EXTERNAL_TRANSMISSION_FORBIDDEN,
            "task provenance forbids external transmission",
        )
    template, template_sha256 = _load_template(template_path)
    payload = canonical_json_bytes(task.visible_payload()).decode("utf-8")
    text = template.replace(_TEMPLATE_HASH_TOKEN, template_sha256).replace(
        _INPUT_JSON_TOKEN, payload
    )
    forbidden = (
        task.pair_id,
        task.judge_slot,
        task.orientation,
        task.randomization_key_sha256,
    )
    if any(value in text for value in forbidden):
        raise JudgePromptError(
            JudgePromptErrorCode.INVALID_INPUT,
            "rendered prompt leaks private task provenance",
        )
    return RenderedJudgePrompt(
        template_id=JUDGE_TEMPLATE_ID,
        template_version=JUDGE_TEMPLATE_VERSION,
        template_sha256=template_sha256,
        render_sha256=sha256_hex(text.encode("utf-8")),
        task_id=task.task_id,
        text=text,
    )


def parse_blinded_judge_output(raw_output: str) -> JudgeResponse:
    """Parse exactly one finite, duplicate-key-free JSON object."""

    if not raw_output.strip():
        raise JudgeOutputParseError(JudgeOutputErrorCode.EMPTY_OUTPUT, "response is empty")

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number {value!r}")

    def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw_output,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise JudgeOutputParseError(JudgeOutputErrorCode.INVALID_JSON, str(exc)) from exc
    if not isinstance(payload, dict):
        raise JudgeOutputParseError(
            JudgeOutputErrorCode.INVALID_SCHEMA,
            "top-level response must be an object",
        )
    try:
        return JudgeResponse.model_validate(payload)
    except ValueError as exc:
        message = str(exc)
        code = (
            JudgeOutputErrorCode.INCOHERENT
            if "requires relation" in message
            or "requires expert review" in message
            or "judgments require expert review" in message
            else JudgeOutputErrorCode.INVALID_SCHEMA
        )
        raise JudgeOutputParseError(code, message) from exc


def remap_judgment_to_canonical_order(
    value: JudgmentValue,
    *,
    orientation: JudgeOrientation,
) -> JudgmentValue:
    """Map a visible-order judgment back to the canonical pair order."""

    if orientation == "AB":
        return value
    relation_map = {
        "A_stronger": "B_stronger",
        "B_stronger": "A_stronger",
        "equivalent": "equivalent",
        "incomparable": "incomparable",
        "unrelated": "unrelated",
        "ambiguous": "ambiguous",
        None: None,
    }
    return JudgmentValue(
        answer=value.answer,
        relation=relation_map[value.relation],  # type: ignore[arg-type]
        a_implies_b=value.b_implies_a,
        b_implies_a=value.a_implies_b,
        error_types=value.error_types,
        confidence=value.confidence,
        rationale=value.rationale,
        needs_expert_review=value.needs_expert_review,
    )


def judge_provider_input_ids(task: JudgePresentation) -> tuple[str, str]:
    """Return the exact semantic input IDs required on a judge provider call."""

    return task.pair_id, task.task_id


def materialize_judgment_evidence(
    *,
    pair_id: str,
    call_id: str,
    judge_family: str,
    judge_slot: JudgeSlot,
    proposer_family: str,
    orientation: JudgeOrientation,
    response: JudgeResponse,
    method_version: str,
    config_hash: str,
    raw_artifact: str,
    created_at: datetime.datetime,
) -> EvidenceRecord:
    """Persist one parsed weak vote without creating a semantic label."""

    if judge_family == proposer_family:
        raise ValueError("judge family must differ from proposer family")
    visible_value = response.to_evidence_value()
    canonical_value = remap_judgment_to_canonical_order(
        visible_value,
        orientation=orientation,
    )
    evidence_id = make_id(
        EVIDENCE_PREFIX,
        {
            "schema": "llm_judgment_v1",
            "pair_id": pair_id,
            "call_id": call_id,
            "judge_family": judge_family,
            "judge_slot": judge_slot,
            "orientation": orientation,
        },
    )
    return EvidenceRecord(
        evidence_id=evidence_id,
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=pair_id,
        kind=EvidenceKind.LLM_JUDGMENT,
        status=EvidenceExecutionStatus.SUCCESS,
        value=canonical_value,
        method_version=method_version,
        config_hash=config_hash,
        raw_artifact=raw_artifact,
        created_at=created_at,
        metadata={
            "llm_call_id": call_id,
            "judge_family": judge_family,
            "judge_slot": judge_slot,
            "proposer_family": proposer_family,
            "orientation": orientation,
            "semantic_label_created": False,
        },
    )


def materialize_verified_judgment_evidence(
    *,
    call: LLMCallRecord,
    task: JudgePresentation,
    source: PublicLeanJudgePair,
    family_matrix: FamilySeparationMatrix,
    proposer_family: str,
    method_version: str,
    config_hash: str,
    artifact_root: Path,
    created_at: datetime.datetime,
    template_path: Path = DEFAULT_JUDGE_TEMPLATE,
) -> EvidenceRecord:
    """Materialize one vote only from a hash-verified, raw-first judge call."""

    from leanfaith.generation.providers import (
        ProviderError,
        verify_generic_llm_call_artifacts,
    )

    validate_family_separation(family_matrix)
    expected_family = (
        family_matrix.judge_a_family
        if task.judge_slot == "judge_A"
        else family_matrix.judge_b_family
    )
    if family_matrix.proposer_family != proposer_family or call.model_family != expected_family:
        raise ProviderError("judge call differs from the frozen family-separation matrix")
    if source.pair_id != task.pair_id or task.source_admission_sha256 != source.admission_sha256:
        raise ProviderError("judge task differs from its public-source admission")
    rendered = render_blinded_judge_prompt(task, template_path=template_path)
    expected_input_ids = judge_provider_input_ids(task)
    if (
        call.schema_version != 2
        or call.role is not LLMRole.JUDGE
        or call.terminal_status is not LLMCallStatus.COMPLETED
        or call.parse_status is not ParseStatus.PARSED
    ):
        raise ProviderError(
            "verified judgment materialization requires a completed, parsed schema-v2 judge call"
        )
    if (
        call.provider_slot != task.judge_slot
        or call.prompt_template_id != JUDGE_TEMPLATE_ID
        or call.prompt_template_version != JUDGE_TEMPLATE_VERSION
        or call.prompt_template_hash != rendered.template_sha256
        or call.prompt_render_hash != rendered.render_sha256
        or call.input_ids != expected_input_ids
    ):
        raise ProviderError("judge call differs from the frozen blinded task")
    if call.metadata.get("weak_supervision_config_hash") != config_hash:
        raise ProviderError("judge call is not bound to the weak-supervision config")
    if call.metadata.get("proposer_family") != proposer_family:
        raise ProviderError("judge call proposer lineage differs")
    response_artifact = verify_generic_llm_call_artifacts(
        call=call,
        expected_role=LLMRole.JUDGE,
        expected_input_ids=expected_input_ids,
        private_source_content=False,
        denylist_checked=True,
        denylist_hits=(),
        artifact_root=artifact_root,
    )
    assert response_artifact.output_text is not None
    response = parse_blinded_judge_output(response_artifact.output_text)
    if call.parsed_output != response.model_dump(mode="json", by_alias=True):
        raise ProviderError("persisted parsed judge payload differs from verified raw response")
    if call.raw_output_artifact is None:
        raise ProviderError("verified judge call lacks its raw artifact path")
    return materialize_judgment_evidence(
        pair_id=task.pair_id,
        call_id=call.call_id,
        judge_family=call.model_family,
        judge_slot=task.judge_slot,
        proposer_family=proposer_family,
        orientation=task.orientation,
        response=response,
        method_version=method_version,
        config_hash=config_hash,
        raw_artifact=call.raw_output_artifact,
        created_at=created_at,
    )


def _semantic_projection(value: JudgmentValue) -> tuple[object, ...]:
    """Fields subject to swap/cross-family agreement.

    E-codes remain analysis metadata because a directional inverse policy for
    every E-code is not registered.
    """

    return (
        value.answer,
        value.relation,
        value.a_implies_b,
        value.b_implies_a,
    )


def _evidence_metadata(record: EvidenceRecord, key: str) -> str:
    value = record.metadata.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"judgment evidence requires string metadata {key!r}")
    return value


def build_weak_consensus_candidate(
    *,
    pair_id: str,
    proposer_family: str,
    family_matrix: FamilySeparationMatrix,
    judgments: tuple[EvidenceRecord, ...],
    created_at: datetime.datetime,
) -> WeakConsensusCandidateRecord:
    """Aggregate two families by two orientations into a non-promoted candidate."""

    validate_family_separation(family_matrix)
    if proposer_family != family_matrix.proposer_family:
        raise ValueError("aggregation proposer differs from the frozen family matrix")
    expected_slots = {
        family_matrix.judge_a_family: "judge_A",
        family_matrix.judge_b_family: "judge_B",
    }
    usable: dict[tuple[str, str], EvidenceRecord] = {}
    all_evidence_ids: list[str] = []
    all_call_ids: list[str] = []
    for record in judgments:
        if (
            record.kind is not EvidenceKind.LLM_JUDGMENT
            or record.status is not EvidenceExecutionStatus.SUCCESS
            or not isinstance(record.value, JudgmentValue)
            or record.target_id != pair_id
        ):
            continue
        family = _evidence_metadata(record, "judge_family")
        orientation = _evidence_metadata(record, "orientation")
        judge_slot = _evidence_metadata(record, "judge_slot")
        evidence_proposer = _evidence_metadata(record, "proposer_family")
        if evidence_proposer != proposer_family:
            raise ValueError("judgment evidence proposer lineage differs from aggregation")
        if family == family_matrix.primary_eval_judge_family:
            raise ValueError("primary evaluation judge cannot enter weak supervision")
        if family not in expected_slots or judge_slot != expected_slots[family]:
            raise ValueError("judgment family/slot differs from the frozen family matrix")
        if orientation not in {"AB", "BA"}:
            raise ValueError("judgment orientation must be AB or BA")
        key = (family, orientation)
        if key in usable:
            raise ValueError(f"duplicate judgment for family/orientation {key}")
        usable[key] = record
        all_evidence_ids.append(record.evidence_id)
        all_call_ids.append(_evidence_metadata(record, "llm_call_id"))

    families = sorted({family for family, _ in usable})
    status: WeakConsensusStatus
    consensus_value: JudgmentValue | None = None
    if len(families) != 2 or any(
        (family, orientation) not in usable for family in families for orientation in ("AB", "BA")
    ):
        status = "incomplete"
    else:
        canonical_by_family: dict[str, JudgmentValue] = {}
        swap_inconsistent = False
        for family in families:
            forward = usable[(family, "AB")].value
            backward = usable[(family, "BA")].value
            assert isinstance(forward, JudgmentValue)
            assert isinstance(backward, JudgmentValue)
            if _semantic_projection(forward) != _semantic_projection(backward):
                swap_inconsistent = True
            canonical_by_family[family] = forward
        if swap_inconsistent:
            status = "swap_inconsistent"
        else:
            first, second = (canonical_by_family[family] for family in families)
            if first.answer == "uncertain" and second.answer == "uncertain":
                status = "all_abstain"
            elif first.answer == "ambiguous" and second.answer == "ambiguous":
                status = "ambiguous_consensus"
            elif (
                _semantic_projection(first) != _semantic_projection(second)
                or first.answer == "uncertain"
            ):
                status = "disagreement"
            else:
                status = "candidate_consensus"
                consensus_value = JudgmentValue(
                    answer=first.answer,
                    relation=first.relation,
                    a_implies_b=first.a_implies_b,
                    b_implies_a=first.b_implies_a,
                    error_types=tuple(sorted(set(first.error_types) | set(second.error_types))),
                    confidence=min(
                        value
                        for value in (first.confidence, second.confidence)
                        if value is not None
                    )
                    if first.confidence is not None and second.confidence is not None
                    else None,
                    rationale=None,
                    needs_expert_review=True,
                )

    judge_families = tuple(families)
    if len(judge_families) < 2:
        # The strict persisted schema requires the configured two-family
        # design even for incomplete runtime inputs.  Callers must supply the
        # intended families explicitly by retaining at least one terminal
        # parsed vote from each family; malformed/no-response calls remain in
        # LLMCallRecord and are summarized separately.
        raise ValueError("candidate aggregation requires evidence from two judge families")
    if len(judge_families) > 2:
        raise ValueError("candidate aggregation accepts exactly two judge families")
    typed_families = (judge_families[0], judge_families[1])
    sorted_evidence_ids = tuple(sorted(all_evidence_ids))
    candidate_id = make_weak_consensus_id(
        pair_id=pair_id,
        proposer_family=proposer_family,
        judge_families=typed_families,
        judgment_evidence_ids=sorted_evidence_ids,
        status=status,
    )
    blockers = {
        "human_pilot_not_bound",
        "promotion_audit_missing",
        "silver_not_promoted",
    }
    if status != "candidate_consensus":
        blockers.add(status)
    return WeakConsensusCandidateRecord(
        candidate_id=candidate_id,
        pair_id=pair_id,
        proposer_family=proposer_family,
        judge_families=typed_families,
        judgment_evidence_ids=sorted_evidence_ids,
        llm_call_ids=tuple(sorted(set(all_call_ids))),
        status=status,
        consensus_value=consensus_value,
        promotion_blockers=tuple(sorted(blockers)),
        created_at=created_at,
        metadata={"e_code_agreement_role": "exploratory_only"},
    )
