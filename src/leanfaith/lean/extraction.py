"""Lean-aware declaration extraction (PLAN.md §12).

Turns a Lean source (a repository file or a dataset snippet) plus its
LeanInteract ``declarations`` response into proof-stripped ``TheoremRecord``s
with source identity, ancestry IDs, a minimal representation, and explicit
failure records. Proof stripping is range-based (§12.2 step 6), never regex
(§12.4): the accepted declaration text runs from the declaration start
through the signature end, then a placeholder replaces the proof. Lean
reports codepoint columns (verified against non-BMP symbols), so Python
string offsets line up directly.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.schemas.enums import NLTrust, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import REPRESENTATION_PREFIX, THEOREM_PREFIX, make_id
from leanfaith.schemas.source import make_source_ancestry_id
from leanfaith.schemas.theorem import (
    CANONICAL_VIEW_NAMES,
    RepresentationRecord,
    TheoremRecord,
)

#: Declaration kinds that are always proposition-valued in Lean 4 (theorem and
#: its `lemma` macro). `example` is anonymous and cannot be referenced.
PROPOSITION_KINDS = frozenset({"theorem", "lemma"})

PLACEHOLDER = " := by sorry"
EXTRACT_NORMALIZATION_VERSION = "repr_v0_extract"
EXTRACTION_SCHEMA_VERSION = "extract_v2"


class ExtractionFailureCode(StrEnum):
    SOURCE_NON_ELABORATION = "source_non_elaboration"
    ELABORATING_NO_DECLARATIONS = "elaborating_no_declarations"
    MISSING_LEAN_FENCE = "missing_lean_fence"
    AMBIGUOUS_LEAN_FENCE = "ambiguous_lean_fence"
    IMPORT_FAILURE = "import_failure"
    TIMEOUT = "timeout"
    WORKER_CRASH = "worker_crash"
    UNSUPPORTED_STRUCTURE = "unsupported_structure"
    DUPLICATE_DECLARATION_NAME = "duplicate_declaration_name"
    NOT_A_PROPOSITION = "not_a_proposition"
    MISSING_SIGNATURE_RANGE = "missing_signature_range"
    ANONYMOUS_DECLARATION = "anonymous_declaration"
    RANGE_OUT_OF_BOUNDS = "range_out_of_bounds"
    PROOF_LEAK_DETECTED = "proof_leak_detected"
    REVALIDATION_FAILED = "revalidation_failed"


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Where a declaration came from (§11.3 source fields)."""

    source: str
    source_revision: str
    source_record: str  # file path (repo) or row id (dataset)
    context_id: str
    source_record_id: str | None = None
    upstream_uuid: str | None = None
    raw_row_hash: str | None = None
    question_hash: str | None = None
    lean_code_hash: str | None = None
    extraction_route: str | None = None
    nl_pair_eligibility: str | None = None
    question_lean_code_agreement: str | None = None
    extraction_schema_version: str = EXTRACTION_SCHEMA_VERSION
    source_split: str | None = None
    source_file: str | None = None
    nl_source_link: str | None = None
    nl_trust: NLTrust | None = None


@dataclass(frozen=True, slots=True)
class ExtractionFailure:
    source_record: str
    declaration_name: str | None
    code: ExtractionFailureCode
    detail: str
    outcome_level: str = "declaration"
    extraction_route: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractedDeclaration:
    """One accepted, proof-stripped declaration and its records.

    ``declaration`` is the exact source declaration dict this record came from,
    so revalidation reconstructs the right position even in a multi-declaration
    source (never assume the first declaration).
    """

    theorem: TheoremRecord
    representation: RepresentationRecord
    proof_stripped: str
    declaration: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    accepted: tuple[ExtractedDeclaration, ...] = ()
    failures: tuple[ExtractionFailure, ...] = ()


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(source):
        if char == "\n":
            starts.append(index + 1)
    return starts


def pos_to_offset(source: str, line_starts: list[int], line: int, column: int) -> int:
    """Codepoint offset of Lean position (1-indexed line, 0-indexed column)."""
    if line < 1 or line > len(line_starts):
        raise ValueError(f"line {line} out of range (1..{len(line_starts)})")
    return line_starts[line - 1] + column


def strip_proof(source: str, declaration: dict[str, Any]) -> str:
    """Proof-stripped declaration text: header+signature + placeholder.

    Raises ``ValueError`` if the signature range is missing or out of bounds.
    """
    decl_range = declaration.get("range") or {}
    signature = declaration.get("signature") or {}
    sig_range = signature.get("range") or {}
    if not decl_range or not sig_range:
        raise ValueError("declaration is missing range or signature.range")
    starts = _line_starts(source)
    start = pos_to_offset(
        source, starts, decl_range["start"]["line"], decl_range["start"]["column"]
    )
    sig_end = pos_to_offset(
        source, starts, sig_range["finish"]["line"], sig_range["finish"]["column"]
    )
    if not (0 <= start <= sig_end <= len(source)):
        raise ValueError(f"range out of bounds: start={start} sig_end={sig_end} len={len(source)}")
    header_and_signature = source[start:sig_end].rstrip()
    return header_and_signature + PLACEHOLDER


def _statement_hash(source: str, declaration: dict[str, Any]) -> str:
    signature = declaration.get("signature") or {}
    sig_range = signature.get("range") or {}
    starts = _line_starts(source)
    start = pos_to_offset(source, starts, sig_range["start"]["line"], sig_range["start"]["column"])
    end = pos_to_offset(source, starts, sig_range["finish"]["line"], sig_range["finish"]["column"])
    return sha256_hex(source[start:end].encode("utf-8"))


def _build_ids(
    identity: SourceIdentity,
    declaration: dict[str, Any],
    statement_hash: str,
    declaration_ordinal: int,
) -> tuple[str, str]:
    full_name = declaration.get("full_name") or declaration.get("name")
    source_locator = identity.source_record_id or identity.source_record
    ancestry_id = make_source_ancestry_id(
        source=identity.source,
        revision=identity.source_revision,
        source_locator=source_locator,
        declaration_full_name=str(full_name),
    )
    theorem_id = make_id(
        THEOREM_PREFIX,
        {
            "source": identity.source,
            "revision": identity.source_revision,
            "context_id": identity.context_id,
            "source_record_id": source_locator,
            "declaration_ordinal": declaration_ordinal,
            "extracted_signature_hash": statement_hash,
            "extraction_schema_version": identity.extraction_schema_version,
        },
    )
    return theorem_id, ancestry_id


def _minimal_representation(
    theorem_id: str,
    context_id: str,
    proof_stripped: str,
    declaration: dict[str, Any],
    created_at: datetime.datetime,
) -> RepresentationRecord:
    signature = declaration.get("signature") or {}
    type_info = declaration.get("type") or {}
    headless = str(signature.get("pp", "")).strip() or None
    signature_pp = str(type_info.get("pp", "")).strip() or None
    view_status = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    view_status["raw_proof_stripped"] = ViewStatus.OK
    view_status["headless"] = ViewStatus.OK if headless else ViewStatus.FAILED
    view_status["signature_pp"] = ViewStatus.OK if signature_pp else ViewStatus.FAILED
    representation_id = make_id(
        REPRESENTATION_PREFIX,
        {"theorem_id": theorem_id, "normalization_version": EXTRACT_NORMALIZATION_VERSION},
    )
    content_hash = hash_canonical(
        {
            "raw_proof_stripped": proof_stripped,
            "headless": headless,
            "signature_pp": signature_pp,
        }
    )
    return RepresentationRecord(
        representation_id=representation_id,
        theorem_id=theorem_id,
        normalization_version=EXTRACT_NORMALIZATION_VERSION,
        context_id=context_id,
        raw_proof_stripped=proof_stripped,
        headless=headless,
        signature_pp=signature_pp,
        view_status=view_status,
        content_hash=content_hash,
        created_at=created_at,
    )


def build_theorem_record(
    identity: SourceIdentity,
    source: str,
    declaration: dict[str, Any],
    *,
    elaboration_status: ValidationStatus,
    created_at: datetime.datetime,
    lean_result_id: str | None = None,
    diagnostics: tuple[str, ...] = (),
    declaration_ordinal: int = 0,
) -> ExtractedDeclaration | ExtractionFailure:
    """Build the TheoremRecord + minimal RepresentationRecord for one accepted
    source declaration (no parents; derived-variant ancestry is the transform
    stage's job), or an ExtractionFailure if it cannot be proof-stripped."""
    name = declaration.get("name")
    full_name = declaration.get("full_name") or name
    kind = str(declaration.get("kind", ""))
    if kind not in PROPOSITION_KINDS:
        return ExtractionFailure(
            identity.source_record, name, ExtractionFailureCode.NOT_A_PROPOSITION, kind
        )
    if not full_name:
        return ExtractionFailure(
            identity.source_record, None, ExtractionFailureCode.ANONYMOUS_DECLARATION, ""
        )
    try:
        proof_stripped = strip_proof(source, declaration)
    except ValueError as exc:
        code = (
            ExtractionFailureCode.MISSING_SIGNATURE_RANGE
            if "signature" in str(exc)
            else ExtractionFailureCode.RANGE_OUT_OF_BOUNDS
        )
        return ExtractionFailure(identity.source_record, name, code, str(exc))

    statement_hash = _statement_hash(source, declaration)
    theorem_id, ancestry_id = _build_ids(identity, declaration, statement_hash, declaration_ordinal)
    decl_range = declaration.get("range") or {}
    source_range = None
    if decl_range:
        source_range = (decl_range["start"]["line"], decl_range["finish"]["line"])
    quality = _quality_flags(source, declaration, proof_stripped)

    theorem = TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        # A source theorem is its own root; it has no parents.
        root_ancestry_ids=(ancestry_id,),
        parent_theorem_ids=(),
        source=identity.source,
        source_revision=identity.source_revision,
        source_split=identity.source_split,
        source_record=identity.source_record,
        source_record_id=identity.source_record_id,
        upstream_uuid=identity.upstream_uuid,
        raw_row_hash=identity.raw_row_hash,
        question_hash=identity.question_hash,
        lean_code_hash=identity.lean_code_hash,
        extraction_route=identity.extraction_route,
        nl_pair_eligibility=identity.nl_pair_eligibility,
        question_lean_code_agreement=identity.question_lean_code_agreement,
        source_file=identity.source_file,
        source_range=source_range,
        context_id=identity.context_id,
        declaration_kind=kind,
        declaration_name=name,
        declaration_full_name=full_name,
        declaration_ordinal=declaration_ordinal,
        proof_stripped_declaration=proof_stripped,
        lean_result_id=lean_result_id,
        is_proposition=True,
        elaboration_status=elaboration_status,
        elaboration_diagnostics=diagnostics,
        statement_content_hash=statement_hash,
        nl_source_link=identity.nl_source_link,
        nl_trust=identity.nl_trust,
        metadata=quality,
    )
    representation = _minimal_representation(
        theorem_id, identity.context_id, proof_stripped, declaration, created_at
    )
    return ExtractedDeclaration(
        theorem=theorem,
        representation=representation,
        proof_stripped=proof_stripped,
        declaration=declaration,
    )


#: Conclusions that carry no mathematical content (semantic-erasure sources).
_TRIVIAL_CONCLUSIONS = frozenset({"True", "true"})


def _quality_flags(
    source: str, declaration: dict[str, Any], proof_stripped: str
) -> dict[str, str | int | float | bool | None]:
    """Downstream-usable quality signals for source-theorem selection.

    A theorem whose conclusion is ``True`` or whose signature hides a tactic
    block in an autoParam binder (``: T := by ...``) is a malformed
    autoformalization — valid to Lean but useless as a transform source
    (observed in sft_classic). These are flagged, never dropped (§10 rule 5).
    """
    type_pp = str((declaration.get("type") or {}).get("pp", "")).strip()
    signature = declaration.get("signature") or {}
    sig_range = signature.get("range") or {}
    autoparam_tactic = False
    if sig_range:
        starts = _line_starts(source)
        sig_src = source[
            pos_to_offset(
                source, starts, sig_range["start"]["line"], sig_range["start"]["column"]
            ) : pos_to_offset(
                source, starts, sig_range["finish"]["line"], sig_range["finish"]["column"]
            )
        ]
        autoparam_tactic = ":= by" in sig_src or ":= fun" in sig_src
    return {
        "conclusion_pp": type_pp or None,
        "trivial_conclusion": type_pp in _TRIVIAL_CONCLUSIONS,
        "autoparam_tactic_in_signature": autoparam_tactic,
        "transform_source_eligible": bool(type_pp)
        and type_pp not in _TRIVIAL_CONCLUSIONS
        and not autoparam_tactic,
    }


def reconstruct_for_revalidation(
    source: str, declaration: dict[str, Any], proof_stripped: str
) -> str:
    """Build a self-contained snippet that re-elaborates the stripped statement
    in its original context: everything before the declaration (imports,
    options, opens, and any earlier declarations) followed by the proof-
    stripped declaration (§12.2 step 7)."""
    decl_range = declaration.get("range") or {}
    starts = _line_starts(source)
    decl_start = pos_to_offset(
        source, starts, decl_range["start"]["line"], decl_range["start"]["column"]
    )
    prefix = source[:decl_start]
    joiner = "" if prefix.endswith("\n") or not prefix else "\n"
    return prefix + joiner + proof_stripped


def extract_from_declarations(
    identity: SourceIdentity,
    source: str,
    declarations: list[dict[str, Any]],
    *,
    created_at: datetime.datetime,
    elaboration_status: ValidationStatus = ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    lean_result_id: str | None = None,
    accepted_kinds: frozenset[str] = PROPOSITION_KINDS,
) -> ExtractionResult:
    """Extract every proposition-valued declaration from one source's response."""
    accepted: list[ExtractedDeclaration] = []
    failures: list[ExtractionFailure] = []
    seen_names = [str(d.get("full_name") or d.get("name") or "") for d in declarations]
    for declaration_ordinal, declaration in enumerate(declarations):
        kind = str(declaration.get("kind", ""))
        if kind not in accepted_kinds:
            failures.append(
                ExtractionFailure(
                    identity.source_record,
                    declaration.get("name"),
                    ExtractionFailureCode.NOT_A_PROPOSITION,
                    f"declaration kind {kind!r} is not proposition-valued",
                    extraction_route=identity.extraction_route,
                )
            )
            continue
        # Multi-declaration ambiguity: a name appearing twice cannot be
        # unambiguously referenced; quarantine both (§12.3).
        full_name = str(declaration.get("full_name") or declaration.get("name") or "")
        if full_name and seen_names.count(full_name) > 1:
            failures.append(
                ExtractionFailure(
                    identity.source_record,
                    declaration.get("name"),
                    ExtractionFailureCode.DUPLICATE_DECLARATION_NAME,
                    f"duplicate declaration name {full_name!r}",
                )
            )
            continue
        built = build_theorem_record(
            identity,
            source,
            declaration,
            elaboration_status=elaboration_status,
            created_at=created_at,
            lean_result_id=lean_result_id,
            declaration_ordinal=declaration_ordinal,
        )
        if isinstance(built, ExtractionFailure):
            failures.append(built)
        else:
            accepted.append(built)
    return ExtractionResult(accepted=tuple(accepted), failures=tuple(failures))
