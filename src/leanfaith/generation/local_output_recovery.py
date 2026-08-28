"""Versioned Lean-backed recovery for LF-021 model-output envelopes.

This parser is deliberately *not* a replacement for the frozen family
parsers in :mod:`leanfaith.generation.local_output_adapter`.  It is an
explicit postprocessing fallback for a narrow operational failure mode:
models sometimes emit one valid theorem/lemma after harmless, redundant
``import``/``open``/``set_option`` preambles.

The recovery boundary is fail closed:

* only an unfenced completion or the final ``lean``/``lean4`` fence is used;
* earlier reasoning fences are ignored, never concatenated;
* the preamble has a small command allowlist;
* LeanInteract must report exactly one declaration with the expected name;
* declaration ranges, rather than proof-text heuristics, delimit the source
  signature;
* the emitted statement is reconstructed from the declaration's elaborated
  ``ConstantInfo.type`` under ``Options.empty`` and is re-elaborated under the
  registered context without the model-supplied preamble or proof.

The raw response remains caller-owned and immutable.  This module never
assigns a semantic label or Gate credit.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import sha256_hex
from leanfaith.generation.local_output_adapter import (
    FinalFenceError,
    FinalFenceErrorCode,
    LeanExtractedCandidate,
    RawLeanCompletion,
)
from leanfaith.generation.prompts import ParsedLeanDeclaration
from leanfaith.lean.extraction import pos_to_offset
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.atoms import parse_lfsignature_payload
from leanfaith.representations.pipeline import _expr_json_helper, _imports_with_lean
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

RECOVERY_PARSER_ID: Literal["lean_expected_declaration_recovery_v1"] = (
    "lean_expected_declaration_recovery_v1"
)

_FENCE_LINE = re.compile(r"^[ \t]*([`~]{3,})([^`~]*)[ \t]*$")
_DECLARATION_HEAD = re.compile(r"(?m)^[ \t]*(?P<kind>theorem|lemma)[ \t]+(?P<name>[^\s:({\[]+)")
_IMPORT = re.compile(r"import[ \t]+(?:Mathlib|Aesop)(?:[ \t]+(?:Mathlib|Aesop))*")
_OPEN_NAME = r"(?:[A-Za-z0-9_'.]+|«[^»\n]+»)"
_OPEN = re.compile(rf"open(?:[ \t]+scoped)?[ \t]+{_OPEN_NAME}(?:[ \t]+{_OPEN_NAME})*")
_SAFE_OPTION = re.compile(r"set_option[ \t]+(?:maxHeartbeats|maxRecDepth)[ \t]+[0-9]+")
_ELABORATING = {LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY}

RECOVERY_ELIGIBLE_PRIMARY_FAILURES = frozenset(
    {
        FinalFenceErrorCode.MISSING_FINAL_FENCE,
        FinalFenceErrorCode.MALFORMED_FENCE,
        FinalFenceErrorCode.MULTIPLE_LEAN_FENCES,
        FinalFenceErrorCode.TRAILING_OUTPUT,
        FinalFenceErrorCode.EMPTY_LEAN_FENCE,
        FinalFenceErrorCode.HEADER_MISMATCH,
        FinalFenceErrorCode.UNSUPPORTED_COMMAND,
        FinalFenceErrorCode.DECLARATION_COUNT,
    }
)


class RecoveryErrorCode(StrEnum):
    """Operational failures of the recovery parser."""

    EMPTY_OUTPUT = "recovery_empty_output"
    MALFORMED_ENVELOPE = "recovery_malformed_envelope"
    TERMINAL_FENCE_REQUIRED = "recovery_terminal_fence_required"
    FORBIDDEN_PREAMBLE = "recovery_forbidden_preamble"
    DECLARATION_COUNT = "recovery_declaration_count"
    DECLARATION_NAME = "recovery_declaration_name"
    CONTEXT_MISMATCH = "recovery_context_mismatch"
    LEAN_INVALID = "recovery_lean_invalid"
    LEAN_DECLARATION_COUNT = "recovery_lean_declaration_count"
    LEAN_RANGE = "recovery_lean_range"
    ELABORATED_TYPE = "recovery_elaborated_type"
    NORMALIZED_INVALID = "recovery_normalized_invalid"


class RecoveryError(ValueError):
    """A completion cannot be safely recovered."""

    def __init__(self, code: RecoveryErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class RecoveryEnvelope:
    """The only candidate body selected from the raw completion."""

    code: str
    code_sha256: str
    envelope_kind: Literal["terminal_lean_fence", "unfenced_raw"]


def recovery_parser_source_sha256() -> str:
    """Hash the executable recovery parser source."""

    return sha256_hex(Path(__file__).read_bytes())


def primary_failure_allows_recovery(exc: BaseException) -> bool:
    """Whether a frozen-parser failure is in the registered fallback class."""

    return isinstance(exc, FinalFenceError) and exc.code in RECOVERY_ELIGIBLE_PRIMARY_FAILURES


def _fenced_blocks(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return non-nested Markdown fences, rejecting every unclosed opener."""

    blocks: list[tuple[int, int, str]] = []
    index = 0
    while index < len(lines):
        opener = _FENCE_LINE.fullmatch(lines[index])
        if opener is None:
            index += 1
            continue
        marker = opener.group(1)
        closing: int | None = None
        for candidate_index in range(index + 1, len(lines)):
            candidate = _FENCE_LINE.fullmatch(lines[candidate_index])
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
            raise RecoveryError(
                RecoveryErrorCode.MALFORMED_ENVELOPE,
                f"unclosed fence at line {index + 1}",
            )
        blocks.append((index, closing, opener.group(2).strip()))
        index = closing + 1
    return blocks


def extract_recovery_envelope(raw_output: str) -> RecoveryEnvelope:
    """Select only the terminal Lean fence, or the entire unfenced output."""

    if not raw_output.strip():
        raise RecoveryError(RecoveryErrorCode.EMPTY_OUTPUT, "completion is empty")
    lines = raw_output.splitlines()
    has_fence_marker = any(_FENCE_LINE.fullmatch(line) is not None for line in lines)
    if not has_fence_marker:
        code = raw_output.strip()
        return RecoveryEnvelope(
            code=code,
            code_sha256=sha256_hex(code.encode("utf-8")),
            envelope_kind="unfenced_raw",
        )

    blocks = _fenced_blocks(lines)
    if not blocks:
        raise RecoveryError(
            RecoveryErrorCode.MALFORMED_ENVELOPE,
            "completion contains a fence marker but no closed fence",
        )
    opening, closing, language = blocks[-1]
    if any(line.strip() for line in lines[closing + 1 :]):
        raise RecoveryError(
            RecoveryErrorCode.TERMINAL_FENCE_REQUIRED,
            "the final fence must be the final non-whitespace output",
        )
    if language.casefold() not in {"lean", "lean4"}:
        raise RecoveryError(
            RecoveryErrorCode.TERMINAL_FENCE_REQUIRED,
            "the final fenced block is not lean/lean4",
        )
    code = "\n".join(lines[opening + 1 : closing]).strip()
    if not code:
        raise RecoveryError(
            RecoveryErrorCode.EMPTY_OUTPUT,
            "the terminal Lean fence is empty",
        )
    return RecoveryEnvelope(
        code=code,
        code_sha256=sha256_hex(code.encode("utf-8")),
        envelope_kind="terminal_lean_fence",
    )


def _mask_comments_and_strings(source: str) -> str:
    """Mask comments/string contents while preserving offsets and newlines."""

    chars = list(source)
    index = 0
    block_depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        if block_depth:
            if source.startswith("/-", index):
                chars[index] = chars[index + 1] = " "
                block_depth += 1
                index += 2
            elif source.startswith("-/", index):
                chars[index] = chars[index + 1] = " "
                block_depth -= 1
                index += 2
            else:
                if source[index] != "\n":
                    chars[index] = " "
                index += 1
            continue
        if in_string:
            if source[index] != "\n":
                chars[index] = " "
            if escaped:
                escaped = False
            elif source[index] == "\\":
                escaped = True
            elif source[index] == '"':
                in_string = False
            index += 1
            continue
        if source.startswith("--", index):
            while index < len(source) and source[index] != "\n":
                chars[index] = " "
                index += 1
            continue
        if source.startswith("/-", index):
            chars[index] = chars[index + 1] = " "
            block_depth = 1
            index += 2
            continue
        if source[index] == '"':
            chars[index] = " "
            in_string = True
        index += 1
    if block_depth:
        raise RecoveryError(
            RecoveryErrorCode.MALFORMED_ENVELOPE,
            "unterminated block comment in candidate",
        )
    if in_string:
        # Lean will diagnose an unterminated string, but fail before executing
        # a malformed preamble so the allowlist remains authoritative.
        raise RecoveryError(
            RecoveryErrorCode.MALFORMED_ENVELOPE,
            "unterminated string literal in candidate",
        )
    return "".join(chars)


def _validate_preamble(preamble: str) -> None:
    masked = _mask_comments_and_strings(preamble)
    for line_number, line in enumerate(masked.splitlines(), start=1):
        command = line.strip()
        if not command:
            continue
        if (
            _IMPORT.fullmatch(command) is None
            and _OPEN.fullmatch(command) is None
            and _SAFE_OPTION.fullmatch(command) is None
        ):
            raise RecoveryError(
                RecoveryErrorCode.FORBIDDEN_PREAMBLE,
                f"line {line_number} is outside the recovery allowlist: {command!r}",
            )


def _candidate_declaration(
    code: str,
    *,
    expected_declaration_name: str,
) -> tuple[str, str, int]:
    masked = _mask_comments_and_strings(code)
    heads = tuple(_DECLARATION_HEAD.finditer(masked))
    if len(heads) != 1:
        raise RecoveryError(
            RecoveryErrorCode.DECLARATION_COUNT,
            f"expected one source theorem/lemma head, observed {len(heads)}",
        )
    head = heads[0]
    observed_name = head.group("name")
    if observed_name != expected_declaration_name:
        raise RecoveryError(
            RecoveryErrorCode.DECLARATION_NAME,
            f"expected {expected_declaration_name!r}, observed {observed_name!r}",
        )
    preamble = code[: head.start()]
    _validate_preamble(preamble)
    return head.group("kind"), preamble, head.start()


def _position_offset(source: str, value: object, *, field: str) -> int:
    if not isinstance(value, dict):
        raise RecoveryError(RecoveryErrorCode.LEAN_RANGE, f"{field} is missing")
    line = value.get("line")
    column = value.get("column")
    if not isinstance(line, int) or not isinstance(column, int):
        raise RecoveryError(RecoveryErrorCode.LEAN_RANGE, f"{field} is invalid")
    starts = [0]
    for index, char in enumerate(source):
        if char == "\n":
            starts.append(index + 1)
    try:
        return pos_to_offset(source, starts, line, column)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError(RecoveryErrorCode.LEAN_RANGE, f"{field}: {exc}") from exc


def _diagnostics(result_messages: tuple[dict[str, object], ...] | list[dict[str, object]]) -> str:
    values = [
        str(message.get("data", "")).strip()
        for message in result_messages
        if str(message.get("data", "")).strip()
    ]
    return "; ".join(values)


def _run(
    backend: LeanInteractBackend,
    request: LeanRequest,
    *,
    error_code: RecoveryErrorCode,
) -> LeanResult:
    try:
        result = backend.run(request)
    except Exception as exc:
        raise RecoveryError(
            error_code,
            f"LeanInteract execution failed: {type(exc).__name__}: {exc}",
        ) from exc
    if result.status not in _ELABORATING:
        raise RecoveryError(
            error_code,
            _diagnostics(result.messages) or result.status.value,
        )
    return result


def _only_expected_declaration(
    declarations: tuple[dict[str, object], ...] | list[dict[str, object]],
    *,
    expected_declaration_name: str,
    error_code: RecoveryErrorCode,
) -> dict[str, object]:
    if len(declarations) != 1:
        raise RecoveryError(
            error_code,
            f"expected exactly one Lean declaration, observed {len(declarations)}",
        )
    declaration = declarations[0]
    if str(declaration.get("name") or "") != expected_declaration_name or str(
        declaration.get("kind") or ""
    ) not in {"theorem", "lemma"}:
        raise RecoveryError(
            error_code,
            "Lean did not report exactly the expected theorem/lemma",
        )
    return declaration


def extract_expected_declaration_with_lean(
    *,
    raw_output: str,
    expected_declaration_name: str,
    registered_header: str,
    problem: ProblemPoolRecord,
    context: ContextRecord,
    backend: LeanInteractBackend,
    created_at: datetime.datetime,
) -> LeanExtractedCandidate:
    """Recover, normalize, and revalidate exactly one expected declaration."""

    del created_at  # output time remains owned by the postprocessing record
    if problem.context_id != context.context_id:
        raise RecoveryError(
            RecoveryErrorCode.CONTEXT_MISMATCH,
            "problem and registered context IDs differ",
        )
    if context.header_text != registered_header:
        raise RecoveryError(
            RecoveryErrorCode.CONTEXT_MISMATCH,
            "registered header differs from ContextRecord",
        )

    envelope = extract_recovery_envelope(raw_output)
    kind, preamble, _ = _candidate_declaration(
        envelope.code,
        expected_declaration_name=expected_declaration_name,
    )
    header = registered_header.rstrip()
    joiner = "\n" if header else ""
    candidate_source = header + joiner + envelope.code
    original_result = _run(
        backend,
        LeanRequest(
            request_id=f"lf021-recovery-source-{envelope.code_sha256[:20]}",
            context_id=context.context_id,
            code=candidate_source,
            declarations=True,
            allow_sorry=True,
            timeout_seconds=300.0,
            metadata={
                "problem_record_id": problem.problem_record_id,
                "parser_id": RECOVERY_PARSER_ID,
                "recovery_stage": "source",
            },
        ),
        error_code=RecoveryErrorCode.LEAN_INVALID,
    )
    declaration = _only_expected_declaration(
        original_result.declarations,
        expected_declaration_name=expected_declaration_name,
        error_code=RecoveryErrorCode.LEAN_DECLARATION_COUNT,
    )
    decl_range = declaration.get("range")
    signature = declaration.get("signature")
    if not isinstance(decl_range, dict) or not isinstance(signature, dict):
        raise RecoveryError(
            RecoveryErrorCode.LEAN_RANGE,
            "Lean declaration lacks declaration/signature ranges",
        )
    sig_range = signature.get("range")
    if not isinstance(sig_range, dict):
        raise RecoveryError(
            RecoveryErrorCode.LEAN_RANGE,
            "Lean declaration lacks signature.range",
        )
    decl_start = _position_offset(
        candidate_source,
        decl_range.get("start"),
        field="declaration.range.start",
    )
    decl_finish = _position_offset(
        candidate_source,
        decl_range.get("finish"),
        field="declaration.range.finish",
    )
    sig_finish = _position_offset(
        candidate_source,
        sig_range.get("finish"),
        field="signature.range.finish",
    )
    if not (0 <= decl_start <= sig_finish <= decl_finish <= len(candidate_source)):
        raise RecoveryError(
            RecoveryErrorCode.LEAN_RANGE,
            "Lean declaration/signature ranges are reversed or out of bounds",
        )
    candidate_offset = len(header + joiner)
    if decl_start < candidate_offset:
        raise RecoveryError(
            RecoveryErrorCode.LEAN_RANGE,
            "Lean declaration begins inside the registered header",
        )
    observed_preamble = candidate_source[candidate_offset:decl_start]
    if observed_preamble != preamble:
        raise RecoveryError(
            RecoveryErrorCode.LEAN_RANGE,
            "Lean declaration range does not follow the validated preamble",
        )
    if _mask_comments_and_strings(candidate_source[decl_finish:]).strip():
        raise RecoveryError(
            RecoveryErrorCode.DECLARATION_COUNT,
            "candidate contains non-comment output after the Lean declaration",
        )

    source_signature = candidate_source[decl_start:sig_finish].rstrip()
    proof_free_declaration = source_signature + " := by sorry"
    normalized_probe_source = "\n".join(
        part
        for part in (
            _imports_with_lean(header),
            preamble.rstrip(),
            proof_free_declaration,
            _expr_json_helper(),
            f"lfDumpSignaturePP {json.dumps(expected_declaration_name, ensure_ascii=False)}",
        )
        if part
    )
    probe_hash = sha256_hex(normalized_probe_source.encode("utf-8"))
    probe_result = _run(
        backend,
        LeanRequest(
            request_id=f"lf021-recovery-type-{probe_hash[:20]}",
            context_id=context.context_id,
            code=normalized_probe_source,
            declarations=False,
            allow_sorry=True,
            timeout_seconds=300.0,
            metadata={
                "problem_record_id": problem.problem_record_id,
                "parser_id": RECOVERY_PARSER_ID,
                "recovery_stage": "elaborated_type",
            },
        ),
        error_code=RecoveryErrorCode.ELABORATED_TYPE,
    )
    elaborated_types: list[str] = []
    for message in probe_result.messages:
        parsed_name, parsed = parse_lfsignature_payload(
            str(message.get("data", "")),
            prefix="LFSIGPPJSON ",
            field="signature_pp",
        )
        if parsed_name == expected_declaration_name and parsed is not None:
            elaborated_types.append(parsed)
    if len(elaborated_types) != 1:
        raise RecoveryError(
            RecoveryErrorCode.ELABORATED_TYPE,
            f"expected one option-isolated signature type, observed {len(elaborated_types)}",
        )
    elaborated_type = elaborated_types[0]
    normalized_statement = f"{kind} {expected_declaration_name} : {elaborated_type}"
    normalized_source = "\n".join(
        part
        for part in (
            header,
            normalized_statement + " := by sorry",
        )
        if part
    )
    normalized_hash = sha256_hex(normalized_source.encode("utf-8"))
    normalized_result = _run(
        backend,
        LeanRequest(
            request_id=f"lf021-recovery-normalized-{normalized_hash[:20]}",
            context_id=context.context_id,
            code=normalized_source,
            declarations=True,
            allow_sorry=True,
            timeout_seconds=300.0,
            metadata={
                "problem_record_id": problem.problem_record_id,
                "parser_id": RECOVERY_PARSER_ID,
                "recovery_stage": "normalized_revalidation",
            },
        ),
        error_code=RecoveryErrorCode.NORMALIZED_INVALID,
    )
    normalized_declaration = _only_expected_declaration(
        normalized_result.declarations,
        expected_declaration_name=expected_declaration_name,
        error_code=RecoveryErrorCode.NORMALIZED_INVALID,
    )
    if str(normalized_declaration.get("kind") or "") != kind:
        raise RecoveryError(
            RecoveryErrorCode.NORMALIZED_INVALID,
            "normalized declaration kind differs",
        )

    parsed_kind: Literal["theorem", "lemma"] = "theorem" if kind == "theorem" else "lemma"
    return LeanExtractedCandidate(
        parsed=ParsedLeanDeclaration(
            declaration_kind=parsed_kind,
            declaration_name=expected_declaration_name,
            statement=normalized_statement,
            statement_sha256=sha256_hex(normalized_statement.encode("utf-8")),
        ),
        fenced=RawLeanCompletion(
            code=envelope.code,
            code_sha256=envelope.code_sha256,
            candidate_body=envelope.code,
            candidate_body_sha256=envelope.code_sha256,
            included_registered_header=False,
        ),
        source_sha256=sha256_hex(candidate_source.encode("utf-8")),
        lean_status=normalized_result.status,
    )


__all__ = [
    "RECOVERY_ELIGIBLE_PRIMARY_FAILURES",
    "RECOVERY_PARSER_ID",
    "RecoveryEnvelope",
    "RecoveryError",
    "RecoveryErrorCode",
    "extract_expected_declaration_with_lean",
    "extract_recovery_envelope",
    "primary_failure_allows_recovery",
    "recovery_parser_source_sha256",
]
