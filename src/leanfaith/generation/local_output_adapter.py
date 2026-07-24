"""Strict completion parser and Lean-backed signature extractor for LF-021.

The adapter consumes completion-only text after the provider response has
already been persisted.  It accepts reasoning before one final Lean fence,
but it never strips a proof with text heuristics: Lean declaration ranges are
used to recover the proposition signature and the generated value is dropped.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import sha256_hex
from leanfaith.generation.prompts import ParsedLeanDeclaration
from leanfaith.lean.extraction import (
    PLACEHOLDER,
    ExtractedDeclaration,
    SourceIdentity,
    extract_from_declarations,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.schemas.enums import ValidationStatus
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

FINAL_FENCE_PARSER_ID: Literal["lean_final_fence_signature_v1"] = "lean_final_fence_signature_v1"
RAW_OR_FINAL_PARSER_ID: Literal["lean_final_fence_or_raw_signature_v2"] = (
    "lean_final_fence_or_raw_signature_v2"
)
TERMINAL_FENCE_OR_RAW_PARSER_ID: Literal["lean_terminal_fence_or_raw_signature_v3"] = (
    "lean_terminal_fence_or_raw_signature_v3"
)
# Compatibility name used by the still-v1 qualification pipeline.
PARSER_ID = FINAL_FENCE_PARSER_ID
type ParserId = Literal[
    "lean_final_fence_signature_v1",
    "lean_final_fence_or_raw_signature_v2",
    "lean_terminal_fence_or_raw_signature_v3",
]
_FENCE = re.compile(r"^[ \t]*([`~]{3,})([^`~]*)[ \t]*$")
_DECLARATION_HEAD = re.compile(
    r"^[ \t]*(?P<kind>theorem|lemma)[ \t]+(?P<name>[^\s:({\[]+)",
    re.MULTILINE | re.UNICODE,
)
_TOP_LEVEL_COMMAND = re.compile(
    r"^[ \t]*(?P<kind>"
    r"theorem|lemma|def|abbrev|opaque|example|axiom|instance|"
    r"structure|class|inductive|namespace|section|end|open|"
    r"import|set_option|variable|universe"
    r")\b",
    re.MULTILINE,
)
_ELABORATING = {LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY}


class FinalFenceErrorCode(StrEnum):
    """Terminal operational failures of the card-oriented output adapter."""

    EMPTY_OUTPUT = "empty_output"
    MISSING_FINAL_FENCE = "missing_final_fence"
    MALFORMED_FENCE = "malformed_fence"
    MULTIPLE_LEAN_FENCES = "multiple_lean_fences"
    TRAILING_OUTPUT = "trailing_output"
    EMPTY_LEAN_FENCE = "empty_lean_fence"
    HEADER_MISMATCH = "header_mismatch"
    UNSUPPORTED_COMMAND = "unsupported_command"
    DECLARATION_COUNT = "declaration_count"
    DECLARATION_NAME = "declaration_name"
    LEAN_INVALID = "lean_invalid"
    LEAN_DECLARATION_COUNT = "lean_declaration_count"
    LEAN_EXTRACTION = "lean_extraction"


class FinalFenceError(ValueError):
    """The persisted completion cannot yield one registered proposition."""

    def __init__(self, code: FinalFenceErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class FinalLeanFence:
    """One final fenced candidate before Lean validation."""

    code: str
    code_sha256: str
    candidate_body: str
    candidate_body_sha256: str
    included_registered_header: bool


@dataclass(frozen=True, slots=True)
class RawLeanCompletion:
    """One unfenced completion whose entire text is the Lean candidate."""

    code: str
    code_sha256: str
    candidate_body: str
    candidate_body_sha256: str
    included_registered_header: bool


type LeanCompletionEnvelope = FinalLeanFence | RawLeanCompletion


@dataclass(frozen=True, slots=True)
class LeanExtractedCandidate:
    """Lean-validated, structurally proof-stripped model output."""

    parsed: ParsedLeanDeclaration
    fenced: LeanCompletionEnvelope
    source_sha256: str
    lean_status: LeanStatus


def parser_source_sha256() -> str:
    """Hash the executable parser source used by a qualification run."""

    return sha256_hex(Path(__file__).read_bytes())


def _fenced_blocks(lines: list[str]) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    index = 0
    while index < len(lines):
        opener = _FENCE.fullmatch(lines[index])
        if opener is None:
            index += 1
            continue
        marker = opener.group(1)
        language = opener.group(2).strip()
        closing: int | None = None
        for candidate_index in range(index + 1, len(lines)):
            candidate = _FENCE.fullmatch(lines[candidate_index])
            if candidate is None:
                continue
            candidate_marker = candidate.group(1)
            if (
                candidate_marker[0] == marker[0]
                and len(candidate_marker) >= len(marker)
                and not candidate.group(2).strip()
            ):
                closing = candidate_index
                break
        if closing is None:
            raise FinalFenceError(
                FinalFenceErrorCode.MALFORMED_FENCE,
                f"unclosed fence at line {index + 1}",
            )
        blocks.append((index, closing, language))
        index = closing + 1
    return blocks


def extract_final_lean_fence(
    raw_output: str,
    *,
    registered_header: str,
) -> FinalLeanFence:
    """Extract exactly one final Lean fence and enforce the header policy."""

    if not raw_output.strip():
        raise FinalFenceError(FinalFenceErrorCode.EMPTY_OUTPUT, "completion is empty")
    lines = raw_output.splitlines()
    blocks = _fenced_blocks(lines)
    lean_blocks = [block for block in blocks if block[2].strip().casefold() in {"lean", "lean4"}]
    if not lean_blocks:
        raise FinalFenceError(
            FinalFenceErrorCode.MISSING_FINAL_FENCE,
            "completion has no lean/lean4 fence",
        )
    if len(lean_blocks) != 1:
        raise FinalFenceError(
            FinalFenceErrorCode.MULTIPLE_LEAN_FENCES,
            f"expected one Lean fence, observed {len(lean_blocks)}",
        )
    opening, closing, _ = lean_blocks[0]
    if any(line.strip() for line in lines[closing + 1 :]):
        raise FinalFenceError(
            FinalFenceErrorCode.TRAILING_OUTPUT,
            "the Lean fence must be the final non-whitespace output",
        )
    code = "\n".join(lines[opening + 1 : closing]).strip()
    if not code:
        raise FinalFenceError(
            FinalFenceErrorCode.EMPTY_LEAN_FENCE,
            "the final Lean fence is empty",
        )

    header = registered_header.rstrip()
    included_header = False
    candidate_body = code
    if header and (code == header or code.startswith(header + "\n")):
        included_header = True
        candidate_body = code[len(header) :].lstrip("\n")
    elif re.search(r"(?m)^[ \t]*(?:import|namespace|section|open|set_option)\b", code):
        raise FinalFenceError(
            FinalFenceErrorCode.HEADER_MISMATCH,
            "generated context commands do not exactly match the registered header",
        )
    if not candidate_body.strip():
        raise FinalFenceError(
            FinalFenceErrorCode.DECLARATION_COUNT,
            "registered header is not followed by a theorem or lemma",
        )

    commands = tuple(_TOP_LEVEL_COMMAND.finditer(candidate_body))
    declaration_heads = tuple(_DECLARATION_HEAD.finditer(candidate_body))
    if len(commands) != 1 or len(declaration_heads) != 1:
        raise FinalFenceError(
            FinalFenceErrorCode.DECLARATION_COUNT,
            "candidate body must contain exactly one top-level theorem or lemma",
        )
    if commands[0].group("kind") not in {"theorem", "lemma"}:
        raise FinalFenceError(
            FinalFenceErrorCode.UNSUPPORTED_COMMAND,
            f"unsupported top-level command {commands[0].group('kind')!r}",
        )
    return FinalLeanFence(
        code=code,
        code_sha256=sha256_hex(code.encode("utf-8")),
        candidate_body=candidate_body.strip(),
        candidate_body_sha256=sha256_hex(candidate_body.strip().encode("utf-8")),
        included_registered_header=included_header,
    )


def extract_final_fence_or_raw_completion(
    raw_output: str,
    *,
    registered_header: str,
) -> LeanCompletionEnvelope:
    """Parse the v2 Kimina envelope without weakening the v1 fence contract.

    A valid v1 final fence is returned unchanged.  If, and only if, there is no
    Lean fence, the *whole* completion may instead be an optional exact copy of
    the registered header followed immediately by one theorem or lemma.  This
    function deliberately performs only envelope checks.  Lean validation and
    declaration ranges remain authoritative for syntax, declaration extent,
    and proof stripping.
    """

    try:
        return extract_final_lean_fence(
            raw_output,
            registered_header=registered_header,
        )
    except FinalFenceError as exc:
        if exc.code is not FinalFenceErrorCode.MISSING_FINAL_FENCE:
            raise

    if not raw_output.strip():
        raise FinalFenceError(FinalFenceErrorCode.EMPTY_OUTPUT, "completion is empty")

    # A non-Lean or otherwise unrecognized fence is not raw Lean.
    if any(_FENCE.fullmatch(line) is not None for line in raw_output.splitlines()):
        raise FinalFenceError(
            FinalFenceErrorCode.MALFORMED_FENCE,
            "raw-completion mode does not accept fence markers",
        )

    code = raw_output.strip()
    header = registered_header.rstrip()
    included_header = False
    candidate_body = code
    if header and (code == header or code.startswith(header + "\n")):
        included_header = True
        candidate_body = code[len(header) :].lstrip("\n")
    elif re.match(
        r"^[ \t]*(?:import|namespace|section|open|set_option|"
        r"variable|universe|include|omit|attribute|local|"
        r"noncomputable|scoped|export)\b",
        code,
    ):
        raise FinalFenceError(
            FinalFenceErrorCode.HEADER_MISMATCH,
            "raw context commands do not exactly match the registered header",
        )

    candidate_body = candidate_body.strip()
    if not candidate_body:
        raise FinalFenceError(
            FinalFenceErrorCode.DECLARATION_COUNT,
            "registered header is not followed by a theorem or lemma",
        )

    head = _DECLARATION_HEAD.match(candidate_body)
    if head is None:
        raise FinalFenceError(
            FinalFenceErrorCode.DECLARATION_COUNT,
            "raw completion must begin directly with one theorem or lemma",
        )
    commands = tuple(_TOP_LEVEL_COMMAND.finditer(candidate_body))
    declaration_heads = tuple(_DECLARATION_HEAD.finditer(candidate_body))
    if len(commands) != 1 or len(declaration_heads) != 1:
        raise FinalFenceError(
            FinalFenceErrorCode.DECLARATION_COUNT,
            "raw completion must contain exactly one top-level theorem or lemma",
        )
    if commands[0].start() != 0 or declaration_heads[0].start() != 0:
        raise FinalFenceError(
            FinalFenceErrorCode.DECLARATION_COUNT,
            "raw completion contains text before the declaration",
        )
    if commands[0].group("kind") not in {"theorem", "lemma"}:
        raise FinalFenceError(
            FinalFenceErrorCode.UNSUPPORTED_COMMAND,
            f"unsupported top-level command {commands[0].group('kind')!r}",
        )

    return RawLeanCompletion(
        code=code,
        code_sha256=sha256_hex(code.encode("utf-8")),
        candidate_body=candidate_body,
        candidate_body_sha256=sha256_hex(candidate_body.encode("utf-8")),
        included_registered_header=included_header,
    )


def extract_terminal_fence_or_raw_completion(
    raw_output: str,
    *,
    registered_header: str,
) -> LeanCompletionEnvelope:
    """Parse a reasoning completion ending in one authoritative Lean fence.

    Unlike the independently versioned v1/v2 contracts, v3 permits earlier
    closed fences in model reasoning.  Only the last fenced block is eligible
    as the candidate, and it must be a ``lean``/``lean4`` block whose closing
    marker is the final non-whitespace output.  The terminal block itself is
    then checked by the unchanged v1 parser, preserving its registered-header
    and single-declaration rules.

    Exact raw-Lean mode is preserved only when the completion contains no
    fence marker at all.  Consequently, malformed, unclosed, or terminal
    non-Lean fences cannot fall back to raw mode.
    """

    if not raw_output.strip():
        raise FinalFenceError(FinalFenceErrorCode.EMPTY_OUTPUT, "completion is empty")

    lines = raw_output.splitlines()
    if not any(_FENCE.fullmatch(line) is not None for line in lines):
        return extract_final_fence_or_raw_completion(
            raw_output,
            registered_header=registered_header,
        )

    blocks = _fenced_blocks(lines)
    assert blocks
    opening, closing, language = blocks[-1]
    if any(line.strip() for line in lines[closing + 1 :]):
        raise FinalFenceError(
            FinalFenceErrorCode.TRAILING_OUTPUT,
            "the terminal Lean fence must be the final non-whitespace output",
        )
    if language.strip().casefold() not in {"lean", "lean4"}:
        raise FinalFenceError(
            FinalFenceErrorCode.MISSING_FINAL_FENCE,
            "the final fenced block is not a lean/lean4 fence",
        )

    terminal_fence = "\n".join(lines[opening : closing + 1])
    return extract_final_lean_fence(
        terminal_fence,
        registered_header=registered_header,
    )


def _position_to_offset(source: str, position: object) -> int:
    if not isinstance(position, dict):
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_EXTRACTION,
            "Lean declaration range position is missing",
        )
    line = position.get("line")
    column = position.get("column")
    if not isinstance(line, int) or not isinstance(column, int):
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_EXTRACTION,
            "Lean declaration range position is invalid",
        )
    line_starts = [0]
    for index, char in enumerate(source):
        if char == "\n":
            line_starts.append(index + 1)
    if line < 1 or line > len(line_starts):
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_EXTRACTION,
            "Lean declaration range line is out of bounds",
        )
    offset = line_starts[line - 1] + column
    if offset < 0 or offset > len(source):
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_EXTRACTION,
            "Lean declaration range offset is out of bounds",
        )
    return offset


def _require_exact_raw_declaration_extent(
    *,
    source: str,
    envelope: LeanCompletionEnvelope,
    declaration: dict[str, object],
) -> None:
    """Reject anything outside the one Lean-reported raw declaration."""

    if not isinstance(envelope, RawLeanCompletion):
        return
    decl_range = declaration.get("range")
    if not isinstance(decl_range, dict):
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_EXTRACTION,
            "Lean declaration is missing its source range",
        )
    start = _position_to_offset(source, decl_range.get("start"))
    finish = _position_to_offset(source, decl_range.get("finish"))
    if finish < start:
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_EXTRACTION,
            "Lean declaration range is reversed",
        )

    prefix = source[:start]
    expected_prefix = ""
    if source.endswith(envelope.candidate_body):
        expected_prefix = source[: -len(envelope.candidate_body)]
    if prefix.rstrip() != expected_prefix.rstrip():
        raise FinalFenceError(
            FinalFenceErrorCode.HEADER_MISMATCH,
            "Lean found text other than the registered header before the declaration",
        )
    if source[finish:].strip():
        raise FinalFenceError(
            FinalFenceErrorCode.TRAILING_OUTPUT,
            "raw completion contains output after the Lean declaration",
        )


def _extract_candidate_signature_with_lean(
    *,
    raw_output: str,
    expected_declaration_name: str,
    registered_header: str,
    problem: ProblemPoolRecord,
    context: ContextRecord,
    backend: LeanInteractBackend,
    created_at: datetime.datetime,
    parser_id: ParserId,
    envelope_extractor: Callable[..., LeanCompletionEnvelope],
    require_exact_raw_extent: bool,
) -> LeanExtractedCandidate:
    """Shared Lean-backed implementation for the independently versioned modes."""

    if problem.context_id != context.context_id:
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_INVALID,
            "problem and registered context IDs differ",
        )
    if context.header_text != registered_header:
        raise FinalFenceError(
            FinalFenceErrorCode.HEADER_MISMATCH,
            "registered header text differs from ContextRecord",
        )
    fenced = envelope_extractor(raw_output, registered_header=registered_header)
    head = _DECLARATION_HEAD.search(fenced.candidate_body)
    assert head is not None
    if head.group("name") != expected_declaration_name:
        raise FinalFenceError(
            FinalFenceErrorCode.DECLARATION_NAME,
            f"expected {expected_declaration_name!r}, observed {head.group('name')!r}",
        )

    if fenced.included_registered_header:
        source = fenced.code
    else:
        joiner = "" if not registered_header or registered_header.endswith("\n") else "\n"
        source = registered_header + joiner + fenced.candidate_body
    request_prefix = {
        FINAL_FENCE_PARSER_ID: "lf021-card-parse-",
        RAW_OR_FINAL_PARSER_ID: "lf021-raw-or-fence-parse-",
        TERMINAL_FENCE_OR_RAW_PARSER_ID: "lf021-terminal-fence-or-raw-parse-",
    }[parser_id]
    request = LeanRequest(
        request_id=request_prefix + fenced.code_sha256[:20],
        context_id=context.context_id,
        code=source,
        declarations=True,
        allow_sorry=True,
        timeout_seconds=300.0,
        metadata={
            "problem_record_id": problem.problem_record_id,
            "parser_id": parser_id,
        },
    )
    try:
        result = backend.run(request)
    except Exception as exc:
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_INVALID,
            f"LeanInteract execution failed: {type(exc).__name__}: {exc}",
        ) from exc
    if result.status not in _ELABORATING:
        diagnostics = "; ".join(str(message.get("data", "")).strip() for message in result.messages)
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_INVALID,
            diagnostics or result.status.value,
        )

    candidates = [
        declaration
        for declaration in result.declarations
        if str(declaration.get("name") or "") == expected_declaration_name
        and str(declaration.get("kind") or "") in {"theorem", "lemma"}
    ]
    if len(candidates) != 1:
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_DECLARATION_COUNT,
            f"expected one registered Lean declaration, observed {len(candidates)}",
        )
    if require_exact_raw_extent:
        _require_exact_raw_declaration_extent(
            source=source,
            envelope=fenced,
            declaration=candidates[0],
        )
    extraction = extract_from_declarations(
        SourceIdentity(
            source="lf021_local_qualification",
            source_revision="v1",
            source_record=problem.problem_record_id,
            source_record_id=sha256_hex(problem.problem_record_id.encode("utf-8")),
            context_id=context.context_id,
            extraction_route=parser_id,
            nl_pair_eligibility="unverified",
        ),
        source,
        candidates,
        created_at=created_at,
        elaboration_status=(
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER
            if result.status is LeanStatus.VALID_WITH_SORRY
            else ValidationStatus.ELABORATES
        ),
        lean_result_id=result.request_hash,
    )
    if len(extraction.accepted) != 1 or extraction.failures:
        detail = "; ".join(f"{item.code.value}:{item.detail}" for item in extraction.failures)
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_EXTRACTION,
            detail or "Lean extraction did not return exactly one declaration",
        )
    accepted: ExtractedDeclaration = extraction.accepted[0]
    if not accepted.proof_stripped.endswith(PLACEHOLDER):
        raise FinalFenceError(
            FinalFenceErrorCode.LEAN_EXTRACTION,
            "proof-stripped declaration lacks the controlled placeholder",
        )
    statement = accepted.proof_stripped[: -len(PLACEHOLDER)].rstrip()
    parsed = ParsedLeanDeclaration(
        declaration_kind=("theorem" if accepted.theorem.declaration_kind == "theorem" else "lemma"),
        declaration_name=expected_declaration_name,
        statement=statement,
        statement_sha256=sha256_hex(statement.encode("utf-8")),
    )
    return LeanExtractedCandidate(
        parsed=parsed,
        fenced=fenced,
        source_sha256=sha256_hex(source.encode("utf-8")),
        lean_status=result.status,
    )


def extract_candidate_signature_with_lean(
    *,
    raw_output: str,
    expected_declaration_name: str,
    registered_header: str,
    problem: ProblemPoolRecord,
    context: ContextRecord,
    backend: LeanInteractBackend,
    created_at: datetime.datetime,
) -> LeanExtractedCandidate:
    """Use Lean ranges to remove any generated proof and recover one signature."""

    return _extract_candidate_signature_with_lean(
        raw_output=raw_output,
        expected_declaration_name=expected_declaration_name,
        registered_header=registered_header,
        problem=problem,
        context=context,
        backend=backend,
        created_at=created_at,
        parser_id=FINAL_FENCE_PARSER_ID,
        envelope_extractor=extract_final_lean_fence,
        require_exact_raw_extent=False,
    )


def extract_candidate_signature_with_lean_v2(
    *,
    raw_output: str,
    expected_declaration_name: str,
    registered_header: str,
    problem: ProblemPoolRecord,
    context: ContextRecord,
    backend: LeanInteractBackend,
    created_at: datetime.datetime,
) -> LeanExtractedCandidate:
    """Parse final-fenced or exact raw Lean, then strip the proof by Lean ranges."""

    return _extract_candidate_signature_with_lean(
        raw_output=raw_output,
        expected_declaration_name=expected_declaration_name,
        registered_header=registered_header,
        problem=problem,
        context=context,
        backend=backend,
        created_at=created_at,
        parser_id=RAW_OR_FINAL_PARSER_ID,
        envelope_extractor=extract_final_fence_or_raw_completion,
        require_exact_raw_extent=True,
    )


def extract_candidate_signature_with_lean_v3(
    *,
    raw_output: str,
    expected_declaration_name: str,
    registered_header: str,
    problem: ProblemPoolRecord,
    context: ContextRecord,
    backend: LeanInteractBackend,
    created_at: datetime.datetime,
) -> LeanExtractedCandidate:
    """Parse one terminal Lean fence (or exact raw Lean) and strip by Lean ranges."""

    return _extract_candidate_signature_with_lean(
        raw_output=raw_output,
        expected_declaration_name=expected_declaration_name,
        registered_header=registered_header,
        problem=problem,
        context=context,
        backend=backend,
        created_at=created_at,
        parser_id=TERMINAL_FENCE_OR_RAW_PARSER_ID,
        envelope_extractor=extract_terminal_fence_or_raw_completion,
        require_exact_raw_extent=True,
    )


__all__ = [
    "FINAL_FENCE_PARSER_ID",
    "PARSER_ID",
    "RAW_OR_FINAL_PARSER_ID",
    "TERMINAL_FENCE_OR_RAW_PARSER_ID",
    "FinalFenceError",
    "FinalFenceErrorCode",
    "FinalLeanFence",
    "LeanCompletionEnvelope",
    "LeanExtractedCandidate",
    "RawLeanCompletion",
    "extract_candidate_signature_with_lean",
    "extract_candidate_signature_with_lean_v2",
    "extract_candidate_signature_with_lean_v3",
    "extract_final_fence_or_raw_completion",
    "extract_final_lean_fence",
    "extract_terminal_fence_or_raw_completion",
    "parser_source_sha256",
]
