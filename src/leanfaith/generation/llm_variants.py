"""Strict LF-022 proposer prompt, parser, and provisional variant materializer.

This module deliberately has no network transport.  Provider execution must
persist the raw response and its :class:`~leanfaith.schemas.llm.LLMCallRecord`
before invoking the parser.  Parsed generation intentions remain provenance;
the resulting ``VariantRecord`` is always provisional and never a semantic
label.
"""

from __future__ import annotations

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
    GeneratorKind,
    IntendedRelation,
    LLMCallStatus,
    LLMRole,
    ParseStatus,
    Polarity,
    QualityTier,
    ValidationStatus,
)
from leanfaith.schemas.ids import (
    CONTEXT_PREFIX,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    VARIANT_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.variant import (
    FORMALRX_SCI_CATEGORIES,
    VariantRecord,
    _check_ecodes,
)

PROPOSER_TEMPLATE_ID = "lean_variant"
PROPOSER_TEMPLATE_VERSION = "v1"
PROPOSER_TEMPLATE_VERSION_V2 = "v2"
DEFAULT_PROPOSER_TEMPLATE = (
    Path(__file__).resolve().parents[3] / "prompts" / "proposers" / "lean_variant_v1.txt"
)
PROPOSER_TEMPLATE_V2 = (
    Path(__file__).resolve().parents[3] / "prompts" / "proposers" / "lean_variant_v2.txt"
)

_PROPOSER_TEMPLATE_VERSION_BY_SHA256 = {
    "0f7b74aab06659e745980879cf9a13cdbcdd29927c1ddbb7ca47c6840e541f36": "v1",
    "f4b6792b9ed1dc4000c72e3aa552be00950f312b4418e2fa5c3d822618cf0944": "v2",
}

_TEMPLATE_HASH_TOKEN = "{{PROMPT_TEMPLATE_SHA256}}"
_INPUT_JSON_TOKEN = "{{INPUT_JSON}}"
_TEMPLATE_TOKEN = re.compile(r"\{\{[A-Z0-9_]+\}\}")
_DECLARATION_HEAD = re.compile(r"^(?:theorem|lemma)\s+[^\s:({\[]+", re.UNICODE)
_COMMAND_HEAD = re.compile(
    r"(?m)^[ \t]*(?:theorem|lemma|def|abbrev|opaque|example|axiom|instance|"
    r"structure|class|inductive|namespace|section|end|import|set_option)\b"
)
_PROOF_TOKEN_OR_WHERE = re.compile(
    r"(?<![\w'])\b(?:by|sorry|admit)\b|^[ \t]*where\b",
    re.UNICODE | re.MULTILINE,
)


class VariantPromptErrorCode(StrEnum):
    INVALID_SOURCE = "invalid_source"
    PRIVATE_SOURCE = "private_source"
    EXTERNAL_TRANSMISSION_FORBIDDEN = "external_transmission_forbidden"
    DENYLIST_NOT_CLEARED = "denylist_not_cleared"
    TEMPLATE_NOT_FOUND = "template_not_found"
    TEMPLATE_NOT_UTF8 = "template_not_utf8"
    TEMPLATE_CONTRACT = "template_contract"


class VariantPromptError(ValueError):
    """A proposer prompt could not be rendered under the public-source policy."""

    def __init__(self, code: VariantPromptErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {detail}")


class VariantOutputErrorCode(StrEnum):
    EMPTY_OUTPUT = "empty_output"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    PROOF_BEARING_CANDIDATE = "proof_bearing_candidate"
    UNSUPPORTED_DECLARATION = "unsupported_declaration"
    MULTIPLE_DECLARATIONS = "multiple_declarations"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    REQUEST_MISMATCH = "request_mismatch"


class VariantOutputParseError(ValueError):
    """A raw proposer response violates the frozen LF-022 contract."""

    def __init__(self, code: VariantOutputErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {detail}")


class VariantProposal(StrictModel):
    """One unverified proposal parsed from the strict JSON response."""

    candidate_lean: str = Field(min_length=1)
    intended_relation: IntendedRelation
    intended_error_types: tuple[str, ...] = ()
    edit_summary: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    assumptions: tuple[str, ...] = ()
    potential_ambiguity: str | None = None

    @model_validator(mode="after")
    def _canonical_fields(self) -> Self:
        if len(set(self.intended_error_types)) != len(self.intended_error_types):
            raise ValueError("intended_error_types must be unique")
        _check_ecodes(tuple(sorted(self.intended_error_types)))
        if any(not value.strip() for value in self.assumptions):
            raise ValueError("assumptions cannot contain empty strings")
        if len(set(self.assumptions)) != len(self.assumptions):
            raise ValueError("assumptions must be unique")
        if self.potential_ambiguity is not None and not self.potential_ambiguity.strip():
            raise ValueError("potential_ambiguity must be null or nonempty")
        return self


class VariantProposalBatch(StrictModel):
    """Strict top-level proposer response."""

    variants: tuple[VariantProposal, ...] = Field(min_length=1)


class PublicLeanVariantSource(StrictModel):
    """Public, denylist-cleared source material eligible for external prompting."""

    source_theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    source_representation_id: str | None = Field(
        default=None, pattern=id_pattern(REPRESENTATION_PREFIX)
    )
    context_id: str | None = Field(default=None, pattern=id_pattern(CONTEXT_PREFIX))
    imports: tuple[str, ...]
    source_statement: str = Field(min_length=1)
    optional_natural_language: str | None = None
    source_id: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    source_license: str = Field(min_length=1)
    source_is_public: bool
    external_transmission_allowed: bool
    denylist_checked: bool
    denylist_hits: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _safe_source(self) -> Self:
        if not self.source_statement.strip() or "\x00" in self.source_statement:
            raise ValueError("source_statement must be nonempty text without NUL bytes")
        if self.optional_natural_language is not None and (
            not self.optional_natural_language.strip() or "\x00" in self.optional_natural_language
        ):
            raise ValueError("optional_natural_language must be null or safe nonempty text")
        if any(not item.strip() or "\x00" in item for item in self.imports):
            raise ValueError("imports must contain safe nonempty strings")
        if len(set(self.imports)) != len(self.imports):
            raise ValueError("imports must be unique")
        return self


class VariantPromptRequest(StrictModel):
    """Requested generation strata rendered into one canonical prompt."""

    request_id: str = Field(min_length=1)
    source: PublicLeanVariantSource
    proposal_count: int = Field(ge=1, le=32)
    requested_relations: tuple[IntendedRelation, ...] = Field(min_length=1)
    requested_error_types: tuple[str, ...] = ()
    requested_sci_categories: tuple[str, ...] = ()
    generation_distribution: Literal["G_sci", "G_open"]

    @model_validator(mode="after")
    def _request_contract(self) -> Self:
        if len(set(self.requested_relations)) != len(self.requested_relations):
            raise ValueError("requested_relations must be unique")
        if len(set(self.requested_error_types)) != len(self.requested_error_types):
            raise ValueError("requested_error_types must be unique")
        _check_ecodes(tuple(sorted(self.requested_error_types)))
        if len(set(self.requested_sci_categories)) != len(self.requested_sci_categories):
            raise ValueError("requested_sci_categories must be unique")
        unknown_sci = sorted(set(self.requested_sci_categories).difference(FORMALRX_SCI_CATEGORIES))
        if unknown_sci:
            raise ValueError(f"unknown FormalRx SCI categories: {unknown_sci}")
        if self.generation_distribution == "G_sci" and len(self.requested_sci_categories) != 1:
            raise ValueError(
                "G_sci requests require exactly one requested SCI category because "
                "VariantRecord stores one requested category"
            )
        if self.generation_distribution == "G_open" and self.requested_sci_categories:
            raise ValueError("G_open requests cannot carry requested SCI categories")
        return self


@dataclass(frozen=True, slots=True)
class RenderedVariantPrompt:
    template_id: str
    template_version: str
    template_sha256: str
    render_sha256: str
    request_id: str
    text: str


def _load_template(path: Path) -> tuple[str, str]:
    if not path.is_file():
        raise VariantPromptError(
            VariantPromptErrorCode.TEMPLATE_NOT_FOUND,
            f"template is not a regular file: {path}",
        )
    raw = path.read_bytes()
    try:
        template = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VariantPromptError(
            VariantPromptErrorCode.TEMPLATE_NOT_UTF8,
            f"template is not valid UTF-8: {path}",
        ) from exc
    expected = {_TEMPLATE_HASH_TOKEN, _INPUT_JSON_TOKEN}
    observed = set(_TEMPLATE_TOKEN.findall(template))
    if observed != expected or any(template.count(token) != 1 for token in expected):
        raise VariantPromptError(
            VariantPromptErrorCode.TEMPLATE_CONTRACT,
            "template must contain each required placeholder exactly once and no others",
        )
    return template, sha256_hex(raw)


def render_variant_proposer_prompt(
    request: VariantPromptRequest,
    *,
    template_path: Path = DEFAULT_PROPOSER_TEMPLATE,
) -> RenderedVariantPrompt:
    """Render a deterministic prompt after fail-closed source eligibility checks."""

    source = request.source
    if not source.source_is_public:
        raise VariantPromptError(
            VariantPromptErrorCode.PRIVATE_SOURCE,
            "LF-022 external prompts accept only explicitly public source material",
        )
    if not source.external_transmission_allowed:
        raise VariantPromptError(
            VariantPromptErrorCode.EXTERNAL_TRANSMISSION_FORBIDDEN,
            "source provenance forbids external transmission",
        )
    if not source.denylist_checked or source.denylist_hits:
        raise VariantPromptError(
            VariantPromptErrorCode.DENYLIST_NOT_CLEARED,
            "source must be denylist-checked with zero hits",
        )

    template, template_sha256 = _load_template(template_path)
    template_version = _PROPOSER_TEMPLATE_VERSION_BY_SHA256.get(template_sha256)
    if template_version is None:
        raise VariantPromptError(
            VariantPromptErrorCode.TEMPLATE_CONTRACT,
            "template hash is not a reviewed Lean variant prompt version",
        )
    input_payload = {
        "request_id": request.request_id,
        "source_statement_id": source.source_theorem_id,
        "imports": list(source.imports),
        "source_statement": source.source_statement,
        "optional_natural_language": source.optional_natural_language,
        "proposal_count": request.proposal_count,
        "requested_relations": [value.value for value in request.requested_relations],
        "requested_error_types": list(request.requested_error_types),
        "requested_sci_categories": list(request.requested_sci_categories),
        "generation_distribution": request.generation_distribution,
    }
    input_json = canonical_json_bytes(input_payload).decode("utf-8")
    text = template.replace(_TEMPLATE_HASH_TOKEN, template_sha256).replace(
        _INPUT_JSON_TOKEN, input_json
    )
    return RenderedVariantPrompt(
        template_id=PROPOSER_TEMPLATE_ID,
        template_version=template_version,
        template_sha256=template_sha256,
        render_sha256=sha256_hex(text.encode("utf-8")),
        request_id=request.request_id,
        text=text,
    )


def _normalized_candidate(statement: str) -> str:
    return " ".join(statement.replace("\r\n", "\n").strip().split())


def _has_top_level_declaration_value(statement: str) -> bool:
    """Distinguish a declaration body from valid ``:=`` inside its proposition.

    Assignments nested in delimiters are term syntax (for example structure
    literals and named arguments).  At delimiter depth zero, a ``let`` binding
    is also part of the proposition.  Any other top-level ``:=`` introduces the
    theorem/lemma value and is rejected by the proof-stripped contract.
    """

    depths = {"(": 0, "[": 0, "{": 0}
    closing = {")": "(", "]": "[", "}": "{"}
    in_string = False
    escaped = False
    index = 0
    while index < len(statement):
        character = statement[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            index += 1
            continue
        if statement.startswith("--", index):
            newline = statement.find("\n", index + 2)
            index = len(statement) if newline < 0 else newline + 1
            continue
        if statement.startswith(":=", index):
            if any(depths.values()):
                index += 2
                continue
            line_or_sequence = re.split(r"[;\n]", statement[:index])[-1]
            if re.search(r"\blet(?:\s+rec)?\b[^;\n]*$", line_or_sequence):
                index += 2
                continue
            return True
        if character in depths:
            depths[character] += 1
        elif character in closing:
            opener = closing[character]
            depths[opener] = max(0, depths[opener] - 1)
        index += 1
    return False


def _validate_candidate(statement: str) -> None:
    stripped = statement.strip()
    if not _DECLARATION_HEAD.match(stripped) or ":" not in stripped:
        raise VariantOutputParseError(
            VariantOutputErrorCode.UNSUPPORTED_DECLARATION,
            "candidate_lean must be one named theorem or lemma statement",
        )
    if _PROOF_TOKEN_OR_WHERE.search(stripped) or _has_top_level_declaration_value(stripped):
        raise VariantOutputParseError(
            VariantOutputErrorCode.PROOF_BEARING_CANDIDATE,
            "candidate_lean contains a proof/value token",
        )
    if len(_COMMAND_HEAD.findall(stripped)) != 1:
        raise VariantOutputParseError(
            VariantOutputErrorCode.MULTIPLE_DECLARATIONS,
            "candidate_lean must contain exactly one declaration",
        )


def normalize_variant_candidate(statement: str) -> str:
    """Return the versioned whitespace normalization used for duplicate checks.

    Operational collection audits reuse this exact function so a batch cannot
    pass with duplicates that the proposer parser itself would reject inside a
    single response.
    """

    return _normalized_candidate(statement)


def validate_variant_candidate(statement: str) -> None:
    """Apply the production parser's proof-stripped declaration boundary checks."""

    _validate_candidate(statement)


def parse_variant_proposer_output(raw_output: str) -> VariantProposalBatch:
    """Parse exactly one JSON object and reject ambiguous/proof-bearing candidates."""

    if not raw_output.strip():
        raise VariantOutputParseError(VariantOutputErrorCode.EMPTY_OUTPUT, "response is empty")

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number {value!r}")

    def reject_duplicate_keys(items: list[tuple[str, object]]) -> dict[str, object]:
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
            object_pairs_hook=reject_duplicate_keys,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise VariantOutputParseError(
            VariantOutputErrorCode.INVALID_JSON,
            f"response is not one strict JSON value: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise VariantOutputParseError(
            VariantOutputErrorCode.INVALID_SCHEMA,
            "top-level response must be an object",
        )
    try:
        batch = VariantProposalBatch.model_validate(payload)
    except ValueError as exc:
        raise VariantOutputParseError(
            VariantOutputErrorCode.INVALID_SCHEMA,
            str(exc),
        ) from exc

    seen: set[str] = set()
    for proposal in batch.variants:
        _validate_candidate(proposal.candidate_lean)
        normalized = _normalized_candidate(proposal.candidate_lean)
        if normalized in seen:
            raise VariantOutputParseError(
                VariantOutputErrorCode.DUPLICATE_CANDIDATE,
                "response contains duplicate normalized candidate statements",
            )
        seen.add(normalized)
    return batch


def variant_provider_input_ids(request: VariantPromptRequest) -> tuple[str, str]:
    """Return the exact semantic input IDs required on the provider call."""

    return request.source.source_theorem_id, request.request_id


def _validate_batch_against_request(
    batch: VariantProposalBatch,
    request: VariantPromptRequest,
) -> None:
    if len(batch.variants) != request.proposal_count:
        raise VariantOutputParseError(
            VariantOutputErrorCode.REQUEST_MISMATCH,
            "response variant count differs from proposal_count: "
            f"expected {request.proposal_count}, got {len(batch.variants)}",
        )
    outside_requested = sorted(
        {
            proposal.intended_relation.value
            for proposal in batch.variants
            if proposal.intended_relation not in request.requested_relations
        }
    )
    if outside_requested:
        raise VariantOutputParseError(
            VariantOutputErrorCode.REQUEST_MISMATCH,
            f"response contains relations outside requested_relations: {outside_requested}",
        )


def materialize_provisional_variants(
    *,
    batch: VariantProposalBatch,
    request: VariantPromptRequest,
    proposer_family: str,
    proposer_model: str,
    llm_call_id: str,
    generation_config_hash: str,
    prompt_artifact: str,
    raw_output_artifact: str,
    seed: int | None,
) -> tuple[VariantRecord, ...]:
    """Convert parsed proposals to provisional ``VariantRecord`` items.

    This function cannot emit a label or ``silver_consensus`` tier.  SCI
    categories remain requested-only until a distinct-family validator writes
    a later immutable record.
    """

    _validate_batch_against_request(batch, request)
    source = request.source
    records: list[VariantRecord] = []
    requested_sci = (
        request.requested_sci_categories[0] if len(request.requested_sci_categories) == 1 else None
    )
    for index, proposal in enumerate(batch.variants):
        statement = proposal.candidate_lean.strip()
        statement_hash = sha256_hex(statement.encode("utf-8"))
        variant_id = make_id(
            VARIANT_PREFIX,
            {
                "schema": "llm_variant_v1",
                "llm_call_id": llm_call_id,
                "source_theorem_id": source.source_theorem_id,
                "candidate_code_hash": statement_hash,
                "proposal_index": index,
            },
        )
        if proposal.intended_relation is IntendedRelation.EQUIVALENT:
            polarity = Polarity.POSITIVE
        elif proposal.intended_relation is IntendedRelation.UNKNOWN:
            polarity = Polarity.UNKNOWN
        else:
            polarity = Polarity.NEGATIVE
        records.append(
            VariantRecord(
                variant_id=variant_id,
                source_theorem_ids=(source.source_theorem_id,),
                source_representation_ids=(
                    (source.source_representation_id,)
                    if source.source_representation_id is not None
                    else ()
                ),
                context_id=source.context_id,
                generator_kind=GeneratorKind.LLM_PROPOSER,
                generator_id=proposer_model,
                generation_config_hash=generation_config_hash,
                seed=seed,
                prompt_artifact=prompt_artifact,
                raw_output_artifact=raw_output_artifact,
                extracted_statement=statement,
                candidate_code_hash=statement_hash,
                intended_relation=proposal.intended_relation,
                intended_error_types=tuple(sorted(proposal.intended_error_types)),
                formalrx_sci_requested=requested_sci,
                formalrx_sci_validation_status=(
                    "pending" if requested_sci is not None else "not_requested"
                ),
                formalrx_sci_proposer_family=(
                    proposer_family if requested_sci is not None else None
                ),
                candidate_pool=request.generation_distribution,
                transformation_trace=(
                    {
                        "kind": "llm_proposal",
                        "proposal_index": index,
                        "llm_call_id": llm_call_id,
                        "edit_summary": proposal.edit_summary,
                        "confidence": proposal.confidence,
                    },
                ),
                validation_status=ValidationStatus.UNVALIDATED,
                quality_tier=QualityTier.PROVISIONAL,
                polarity_metadata=polarity,
                metadata={
                    "llm_call_id": llm_call_id,
                    "proposer_family": proposer_family,
                    "potential_ambiguity": proposal.potential_ambiguity,
                    "assumptions_json": json.dumps(
                        list(proposal.assumptions),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            )
        )
    return tuple(records)


def materialize_verified_provisional_variants(
    *,
    request: VariantPromptRequest,
    call: LLMCallRecord,
    artifact_root: Path,
    generation_config_hash: str,
    template_path: Path = DEFAULT_PROPOSER_TEMPLATE,
) -> tuple[VariantRecord, ...]:
    """Materialize only from a hash-verified, raw-first proposer call.

    This is the production boundary.  The lower-level materializer remains
    useful for pure unit tests, but production orchestration must not accept
    caller-supplied call IDs or artifact paths.
    """

    # Local import avoids a module cycle: providers owns the generic lineage
    # bridge and imports the shared LLM schemas used here.
    from leanfaith.generation.providers import (
        ProviderError,
        verify_generic_llm_call_artifacts,
    )

    rendered = render_variant_proposer_prompt(request, template_path=template_path)
    expected_input_ids = variant_provider_input_ids(request)
    if (
        call.schema_version != 2
        or call.role is not LLMRole.PROPOSER
        or call.terminal_status is not LLMCallStatus.COMPLETED
        or call.parse_status is not ParseStatus.PARSED
    ):
        raise ProviderError(
            "verified variant materialization requires a completed, parsed schema-v2 proposer call"
        )
    if (
        call.prompt_template_id != rendered.template_id
        or call.prompt_template_version != rendered.template_version
        or call.prompt_template_hash != rendered.template_sha256
        or call.prompt_render_hash != rendered.render_sha256
        or call.input_ids != expected_input_ids
    ):
        raise ProviderError("proposer call differs from the frozen prompt request")
    if call.metadata.get("generation_config_hash") != generation_config_hash:
        raise ProviderError("proposer call is not bound to the requested generation config")
    response = verify_generic_llm_call_artifacts(
        call=call,
        expected_role=LLMRole.PROPOSER,
        expected_input_ids=expected_input_ids,
        private_source_content=False,
        denylist_checked=True,
        denylist_hits=(),
        artifact_root=artifact_root,
    )
    assert response.output_text is not None
    batch = parse_variant_proposer_output(response.output_text)
    parsed_payload = batch.model_dump(mode="json")
    if call.parsed_output != parsed_payload:
        raise ProviderError("persisted parsed proposer payload differs from verified raw response")
    if call.request_artifact is None or call.raw_output_artifact is None:
        raise ProviderError("verified proposer call lacks artifact paths")
    seed_value = call.decoding.get("seed")
    if seed_value is not None and (not isinstance(seed_value, int) or isinstance(seed_value, bool)):
        raise ProviderError("proposer decoding seed must be an integer when present")
    return materialize_provisional_variants(
        batch=batch,
        request=request,
        proposer_family=call.model_family,
        proposer_model=call.model,
        llm_call_id=call.call_id,
        generation_config_hash=generation_config_hash,
        prompt_artifact=call.request_artifact,
        raw_output_artifact=call.raw_output_artifact,
        seed=seed_value,
    )


__all__ = [
    "DEFAULT_PROPOSER_TEMPLATE",
    "PROPOSER_TEMPLATE_ID",
    "PROPOSER_TEMPLATE_V2",
    "PROPOSER_TEMPLATE_VERSION",
    "PROPOSER_TEMPLATE_VERSION_V2",
    "PublicLeanVariantSource",
    "RenderedVariantPrompt",
    "VariantOutputErrorCode",
    "VariantOutputParseError",
    "VariantPromptError",
    "VariantPromptErrorCode",
    "VariantPromptRequest",
    "VariantProposal",
    "VariantProposalBatch",
    "materialize_provisional_variants",
    "materialize_verified_provisional_variants",
    "normalize_variant_candidate",
    "parse_variant_proposer_output",
    "render_variant_proposer_prompt",
    "validate_variant_candidate",
    "variant_provider_input_ids",
]
