"""Frozen ``goal_v1.0`` theorem representation.

The model view is deliberately not a Lean source language.  This module keeps
raw compilable source and its exact compilation context in a separate sidecar,
and exposes only two forward renderers:

* an elaborated renderer that asks one already-loaded Lean backend to inspect a
  batch of ``ConstantInfo.type`` values; and
* a deterministic surface fallback for trusted theorem/lemma signatures.

There is intentionally no goal-to-declaration inverse.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanStatus
from leanfaith.representations.views import collapse_lean_whitespace

RENDERER_VERSION = "goal_v1.0"
GOAL_MARKER = "LFGOALV1JSON "
SUPPORTED_DECLARATION_KINDS = frozenset({"theorem", "lemma"})

# The YAML freeze duplicates this JSON-native payload byte-for-byte.  The
# literal SPEC_HASH is checked against it so downstream manifests have one
# stable value to pin without reading implementation details.
SPEC_PAYLOAD: dict[str, object] = {
    "representation_id": "goal_v1.0",
    "renderer_version": RENDERER_VERSION,
    "declaration_kinds": ["lemma", "theorem"],
    "grammar": {
        "local_line": "<one-or-more local names> : <Lean type>",
        "target_line": "⊢ <Lean proposition>",
        "turnstile_count": 1,
        "line_policy": "adjacent equal-type locals group; target is final",
        "let_target_policy": (
            "top-level let layout is serialized on the final target line with semicolon separators"
        ),
    },
    "preserve": [
        "local_order",
        "local_names",
        "dependent_types",
        "generated_instance_names_when_elaborated",
        "coercions",
        "notation",
        "universes_in_types",
    ],
    "remove": [
        "attributes",
        "declaration_keyword",
        "declaration_name",
        "command_shell",
        "imports",
        "options",
        "comments",
        "proof_delimiter",
        "proof_body",
    ],
    "sources": ["elaborated", "surface"],
    "surface_fail_closed": [
        "anonymous_instance_binder",
        "anonymous_top_level_arrow",
        "duplicate_or_shadowed_local_name",
        "implicit_or_untyped_binder",
        "ambiguous_declaration_or_proof_boundary",
        "unsupported_declaration_kind",
    ],
    "compile_context_fields": [
        "project_id",
        "project_revision",
        "lean_version",
        "import_header",
        "command_preamble",
        "namespace_context",
        "open_context",
        "scoped_context",
        "options",
    ],
    "compile_context_application_order": [
        "import_header",
        "command_preamble",
        "options",
        "open_context",
        "scoped_context",
        "namespace_context",
    ],
    "elaborated_input_modes": ["inline_candidate", "loaded_constant_lookup"],
    "sorry_policy": "any backend-reported sorry fails the batch unless allow_sorry is true",
    "inverse": "forbidden",
    "elaborated_option_profile": {
        "base": "Options.empty",
        "pp.universes": False,
        "pp.coercions": True,
        "pp.notation": True,
        "pp.mvars": False,
        "pp.inaccessibleNames": True,
        "pp.implementationDetailHyps": True,
        "render_width": 1000000,
    },
}

# Filled once from hash_canonical(SPEC_PAYLOAD), then protected by tests.
SPEC_HASH = "7ec7b82923b4eb78a737f47653dfc7d7b5eb619373159ec1cf5ed0d794759ae9"

CompileOptionValue = str | int | float | bool
GoalV1Source = Literal["elaborated", "surface"]


class GoalV1Error(ValueError):
    """Base class for deterministic representation failures."""


class SurfaceFailureCode(StrEnum):
    UNSUPPORTED_DECLARATION_KIND = "unsupported_declaration_kind"
    DECLARATION_NOT_FOUND = "declaration_not_found"
    AMBIGUOUS_DECLARATION = "ambiguous_declaration"
    DECLARATION_KIND_MISMATCH = "declaration_kind_mismatch"
    MISSING_DECLARATION_NAME = "missing_declaration_name"
    AMBIGUOUS_PROOF_BOUNDARY = "ambiguous_proof_boundary"
    UNBALANCED_DELIMITER = "unbalanced_delimiter"
    MISSING_TARGET_SEPARATOR = "missing_target_separator"
    EMPTY_TARGET = "empty_target"
    UNTYPED_BINDER = "untyped_binder"
    ANONYMOUS_INSTANCE_BINDER = "anonymous_instance_binder"
    DUPLICATE_LOCAL_NAME = "duplicate_or_shadowed_local_name"
    ANONYMOUS_TOP_LEVEL_ARROW = "anonymous_top_level_arrow"
    INVALID_GOAL = "invalid_goal"


class SurfaceRenderError(GoalV1Error):
    """A fail-closed surface rendering outcome with a stable code."""

    def __init__(self, code: SurfaceFailureCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CompileContext:
    """Exact non-model context needed to compile the retained raw source."""

    project_id: str
    project_revision: str
    lean_version: str
    import_header: str
    command_preamble: str = ""
    namespace_context: tuple[str, ...] = ()
    open_context: tuple[str, ...] = ()
    scoped_context: tuple[str, ...] = ()
    options: Mapping[str, CompileOptionValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("project_id", "project_revision", "lean_version"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"compile context {field_name} must be nonempty")
        if not self.import_header.strip():
            raise ValueError("compile context import_header must be nonempty")
        for line in self.import_header.splitlines():
            stripped = line.strip()
            import_pattern = r"(?:(?:public|meta)\s+)*import\s+\S+(?:\s+\S+)*"
            if stripped and not re.fullmatch(import_pattern, stripped):
                raise ValueError(
                    "compile context import_header accepts import commands only; "
                    "put other commands in structured fields or command_preamble"
                )
        for field_name in ("namespace_context", "open_context", "scoped_context"):
            for name in getattr(self, field_name):
                if not name.strip() or any(char.isspace() for char in name):
                    raise ValueError(f"{field_name} entries must be nonempty Lean names")
        for option_name, value in self.options.items():
            if not option_name.strip() or any(char.isspace() for char in option_name):
                raise ValueError("compile option names must be nonempty and contain no whitespace")
            if not isinstance(value, (str, int, float, bool)):
                raise TypeError(f"compile option {option_name!r} has unsupported value {value!r}")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "lean_version": self.lean_version,
            "import_header": self.import_header,
            "command_preamble": self.command_preamble,
            "namespace_context": list(self.namespace_context),
            "open_context": list(self.open_context),
            "scoped_context": list(self.scoped_context),
            "options": dict(sorted(self.options.items())),
        }

    @property
    def fingerprint(self) -> str:
        return hash_canonical(self.canonical_payload())

    @property
    def compile_context_id(self) -> str:
        return f"ctx:{self.fingerprint}"


@dataclass(frozen=True, slots=True)
class GoalV1Record:
    representation_id: str
    goal_v1: str
    goal_v1_source: GoalV1Source
    renderer_version: str
    spec_hash: str
    raw_statement_hash: str
    declaration_kind: str
    compile_context_id: str
    typed_alpha_fingerprint: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "representation_id": self.representation_id,
            "goal_v1": self.goal_v1,
            "goal_v1_source": self.goal_v1_source,
            "renderer_version": self.renderer_version,
            "spec_hash": self.spec_hash,
            "raw_statement_hash": self.raw_statement_hash,
            "declaration_kind": self.declaration_kind,
            "compile_context_id": self.compile_context_id,
            "typed_alpha_fingerprint": self.typed_alpha_fingerprint,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class GoalV1Sidecar:
    """Joinable metadata plus raw source; only ``record.goal_v1`` is model-facing."""

    record: GoalV1Record
    raw_statement: str
    compile_context: CompileContext

    def __post_init__(self) -> None:
        raw_hash = sha256_hex(self.raw_statement.encode("utf-8"))
        if raw_hash != self.record.raw_statement_hash:
            raise ValueError("raw_statement does not match raw_statement_hash")
        if self.compile_context.compile_context_id != self.record.compile_context_id:
            raise ValueError("compile_context does not match compile_context_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "record": self.record.to_dict(),
            "raw_statement": self.raw_statement,
            "compile_context": self.compile_context.canonical_payload(),
        }

    def core_text(self) -> str:
        """The only field downstream pair rows copy from this sidecar."""

        return self.record.goal_v1


@dataclass(frozen=True, slots=True)
class ElaboratedInput:
    declaration_name: str
    declaration_kind: str
    raw_statement: str
    typed_alpha_fingerprint: str | None = None
    lookup_only: bool = False


@dataclass(frozen=True, slots=True)
class ElaboratedFailure:
    declaration_name: str
    detail: str


@dataclass(frozen=True, slots=True)
class ElaboratedBatchResult:
    sidecars: tuple[GoalV1Sidecar, ...]
    failures: tuple[ElaboratedFailure, ...]
    request_hash: str
    elapsed_ms: int
    raw_response_path: str | None


@dataclass(frozen=True, slots=True)
class _Binder:
    names: tuple[str, ...]
    type_text: str


@dataclass(frozen=True, slots=True)
class _MaskedSource:
    text: str
    masked: str


_DECLARATION_KEYWORD = re.compile(r"\b(theorem|lemma|def)\b")
_IDENTIFIER = re.compile(r"[^\s(){}\[\]:=,]+")
_FORALL = re.compile(r"^(?:∀|forall)\s+")


def _validate_kind(kind: str) -> None:
    if kind not in SUPPORTED_DECLARATION_KINDS:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNSUPPORTED_DECLARATION_KIND,
            f"goal_v1.0 accepts theorem/lemma, got {kind!r}",
        )


def _mask_comments(source: str) -> _MaskedSource:
    """Mask nested Lean comments while preserving strings, guillemets, and offsets."""

    out = list(source)
    block_depth = 0
    in_line_comment = False
    in_string = False
    in_guillemet = False
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        next_two = source[index : index + 2]
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            else:
                out[index] = " "
            index += 1
            continue
        if block_depth:
            if next_two == "/-":
                out[index] = out[index + 1] = " "
                block_depth += 1
                index += 2
            elif next_two == "-/":
                out[index] = out[index + 1] = " "
                block_depth -= 1
                index += 2
            else:
                if char != "\n":
                    out[index] = " "
                index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if in_guillemet:
            if char == "»":
                in_guillemet = False
            index += 1
            continue
        if next_two == "--":
            out[index] = out[index + 1] = " "
            in_line_comment = True
            index += 2
            continue
        if next_two == "/-":
            out[index] = out[index + 1] = " "
            block_depth = 1
            index += 2
            continue
        if char == '"':
            in_string = True
        elif char == "«":
            in_guillemet = True
        index += 1
    if block_depth:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNBALANCED_DELIMITER,
            "unterminated block comment",
        )
    if in_string:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNBALANCED_DELIMITER,
            "unterminated string literal",
        )
    return _MaskedSource(source, "".join(out))


def _mask_literals_for_declaration_search(masked: str) -> str:
    """Hide strings and guillemet names while retaining declaration offsets."""

    out = list(masked)
    in_string = False
    in_guillemet = False
    escaped = False
    for index, char in enumerate(masked):
        if in_string:
            if char != "\n":
                out[index] = " "
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if in_guillemet:
            if char != "\n":
                out[index] = " "
            if char == "»":
                in_guillemet = False
            continue
        if char == '"':
            out[index] = " "
            in_string = True
        elif char == "«":
            out[index] = " "
            in_guillemet = True
    return "".join(out)


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _consume_name(masked: str, index: int) -> int:
    index = _skip_space(masked, index)
    if index >= len(masked):
        raise SurfaceRenderError(
            SurfaceFailureCode.MISSING_DECLARATION_NAME,
            "declaration keyword has no name",
        )
    if masked[index] == "«":
        finish = masked.find("»", index + 1)
        if finish < 0:
            raise SurfaceRenderError(
                SurfaceFailureCode.UNBALANCED_DELIMITER,
                "unterminated guillemet declaration name",
            )
        return finish + 1
    match = _IDENTIFIER.match(masked, index)
    if match is None:
        raise SurfaceRenderError(
            SurfaceFailureCode.MISSING_DECLARATION_NAME,
            f"cannot parse declaration name at offset {index}",
        )
    return match.end()


def _matching_delimiter(masked: str, start: int) -> int:
    pairs = {"(": ")", "{": "}", "[": "]"}
    opening = masked[start]
    expected = pairs[opening]
    stack = [expected]
    in_string = False
    in_guillemet = False
    escaped = False
    for index in range(start + 1, len(masked)):
        char = masked[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if in_guillemet:
            if char == "»":
                in_guillemet = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "«":
            in_guillemet = True
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif char in ")}]":
            if not stack or char != stack[-1]:
                raise SurfaceRenderError(
                    SurfaceFailureCode.UNBALANCED_DELIMITER,
                    f"unexpected {char!r} at offset {index}",
                )
            stack.pop()
            if not stack:
                return index
    raise SurfaceRenderError(
        SurfaceFailureCode.UNBALANCED_DELIMITER,
        f"missing closing {expected!r}",
    )


def _top_level_positions(text: str, token: str) -> list[int]:
    positions: list[int] = []
    stack: list[str] = []
    pairs = {"(": ")", "{": "}", "[": "]"}
    in_string = False
    in_guillemet = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if in_guillemet:
            if char == "»":
                in_guillemet = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char == "«":
            in_guillemet = True
        elif char in pairs:
            stack.append(pairs[char])
        elif char in ")}]":
            if not stack or stack.pop() != char:
                raise SurfaceRenderError(
                    SurfaceFailureCode.UNBALANCED_DELIMITER,
                    f"unexpected {char!r} at offset {index}",
                )
        elif not stack and text.startswith(token, index):
            positions.append(index)
            index += len(token)
            continue
        index += 1
    if stack:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNBALANCED_DELIMITER,
            f"missing closing {stack[-1]!r}",
        )
    return positions


def _extract_signature(raw_statement: str, declaration_kind: str) -> str:
    masked_source = _mask_comments(raw_statement)
    declaration_search = _mask_literals_for_declaration_search(masked_source.masked)
    declarations = list(_DECLARATION_KEYWORD.finditer(declaration_search))
    if not declarations:
        raise SurfaceRenderError(
            SurfaceFailureCode.DECLARATION_NOT_FOUND,
            "no theorem/lemma declaration found",
        )
    if len(declarations) != 1:
        raise SurfaceRenderError(
            SurfaceFailureCode.AMBIGUOUS_DECLARATION,
            f"expected one declaration, found {len(declarations)}",
        )
    declaration = declarations[0]
    observed_kind = declaration.group(1)
    if observed_kind != declaration_kind:
        raise SurfaceRenderError(
            SurfaceFailureCode.DECLARATION_KIND_MISMATCH,
            f"metadata says {declaration_kind!r}, source says {observed_kind!r}",
        )
    signature_start = _consume_name(masked_source.masked, declaration.end())
    suffix_masked = masked_source.masked[signature_start:]
    proof_delimiters = _top_level_positions(suffix_masked, ":=")
    equation_delimiters = _top_level_positions(suffix_masked, "=>")
    pattern_equations = any(line.lstrip().startswith("|") for line in suffix_masked.splitlines())
    if not proof_delimiters and (equation_delimiters or pattern_equations):
        raise SurfaceRenderError(
            SurfaceFailureCode.AMBIGUOUS_PROOF_BOUNDARY,
            "equation-style declaration has no unambiguous ':=' proof boundary",
        )
    if proof_delimiters:
        for proof_delimiter in proof_delimiters:
            signature_end = signature_start + proof_delimiter
            signature = masked_source.masked[signature_start:signature_end].strip()
            if not signature:
                continue
            target_split = _split_top_level_once(signature, ":")
            if target_split is None:
                continue
            target = collapse_lean_whitespace(target_split[1])
            if target.startswith("let "):
                if not _is_canonical_let_target(target):
                    continue
            elif ":=" in target:
                continue
            return signature
        raise SurfaceRenderError(
            SurfaceFailureCode.AMBIGUOUS_PROOF_BOUNDARY,
            "no top-level ':=' delimiter leaves a valid supported theorem signature",
        )
    signature = masked_source.masked[signature_start:].strip()
    if not signature:
        raise SurfaceRenderError(
            SurfaceFailureCode.MISSING_TARGET_SEPARATOR,
            "declaration signature is empty",
        )
    return signature


def _split_top_level_once(text: str, token: str) -> tuple[str, str] | None:
    positions = _top_level_positions(text, token)
    if not positions:
        return None
    index = positions[0]
    return text[:index], text[index + len(token) :]


def _parse_names(text: str) -> tuple[str, ...]:
    names = tuple(text.split())
    if not names or any(name in {"_", "·"} for name in names):
        raise SurfaceRenderError(
            SurfaceFailureCode.UNTYPED_BINDER,
            f"binder has no stable explicit name: {text!r}",
        )
    if any(any(char in name for char in "(){}[],:=") for name in names):
        raise SurfaceRenderError(
            SurfaceFailureCode.UNTYPED_BINDER,
            f"unsupported binder name syntax: {text!r}",
        )
    return names


def _parse_binder_content(content: str, opening: str) -> _Binder:
    split = _split_top_level_once(content.strip(), ":")
    if split is None:
        if opening == "[":
            raise SurfaceRenderError(
                SurfaceFailureCode.ANONYMOUS_INSTANCE_BINDER,
                f"surface mode cannot recover Lean's generated instance name for [{content}]",
            )
        raise SurfaceRenderError(
            SurfaceFailureCode.UNTYPED_BINDER,
            f"binder lacks an explicit type: {content!r}",
        )
    raw_names, raw_type = split
    names = _parse_names(raw_names.strip())
    type_text = collapse_lean_whitespace(raw_type)
    if not type_text:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNTYPED_BINDER,
            f"binder has an empty type: {content!r}",
        )
    return _Binder(names, type_text)


def _parse_leading_binders(text: str) -> tuple[list[_Binder], str]:
    binders: list[_Binder] = []
    index = _skip_space(text, 0)
    while index < len(text) and text[index] in "({[":
        finish = _matching_delimiter(text, index)
        binders.append(_parse_binder_content(text[index + 1 : finish], text[index]))
        index = _skip_space(text, finish + 1)
    return binders, text[index:]


def _peel_forall_binders(target: str) -> tuple[list[_Binder], str]:
    binders: list[_Binder] = []
    remaining = target.strip()
    while (match := _FORALL.match(remaining)) is not None:
        body = remaining[match.end() :]
        comma_positions = _top_level_positions(body, ",")
        if not comma_positions:
            raise SurfaceRenderError(
                SurfaceFailureCode.UNTYPED_BINDER,
                "top-level forall has no comma-delimited body",
            )
        clause = body[: comma_positions[0]].strip()
        clause_binders, clause_remainder = _parse_leading_binders(clause)
        if clause_binders:
            if clause_remainder.strip():
                raise SurfaceRenderError(
                    SurfaceFailureCode.UNTYPED_BINDER,
                    f"unsupported forall binder clause: {clause!r}",
                )
            binders.extend(clause_binders)
        else:
            binders.append(_parse_binder_content(clause, "("))
        remaining = body[comma_positions[0] + 1 :].strip()
    return binders, remaining


def _strip_balanced_outer_parentheses(text: str) -> str:
    stripped = text.strip()
    while stripped.startswith("("):
        finish = _matching_delimiter(stripped, 0)
        if finish != len(stripped) - 1:
            break
        stripped = stripped[1:-1].strip()
    return stripped


def _has_top_level_arrow(text: str) -> bool:
    unwrapped = _strip_balanced_outer_parentheses(text)
    return bool(_top_level_positions(unwrapped, "→") or _top_level_positions(unwrapped, "->"))


def _group_binders(binders: Sequence[_Binder]) -> list[str]:
    grouped: list[_Binder] = []
    for binder in binders:
        if grouped and grouped[-1].type_text == binder.type_text:
            previous = grouped[-1]
            grouped[-1] = _Binder(previous.names + binder.names, previous.type_text)
        else:
            grouped.append(binder)
    return [f"{' '.join(binder.names)} : {binder.type_text}" for binder in grouped]


def _is_canonical_let_target(target: str) -> bool:
    stripped = target.strip()
    if not stripped.startswith("let "):
        return False
    assignments = _top_level_positions(stripped, ":=")
    separators = _top_level_positions(stripped, ";")
    return bool(assignments and separators and separators[-1] > assignments[-1])


def _canonicalize_surface_target(target: str) -> str:
    collapsed = collapse_lean_whitespace(target)
    if not collapsed:
        raise SurfaceRenderError(SurfaceFailureCode.EMPTY_TARGET, "target is empty")
    if collapsed.startswith("let "):
        if _is_canonical_let_target(collapsed):
            return collapsed
        raise SurfaceRenderError(
            SurfaceFailureCode.AMBIGUOUS_PROOF_BOUNDARY,
            "surface top-level let target must retain an assignment and body separator",
        )
    if ":=" in collapsed:
        raise SurfaceRenderError(
            SurfaceFailureCode.AMBIGUOUS_PROOF_BOUNDARY,
            "surface target contains ':=' outside a semicolon-delimited top-level let",
        )
    return collapsed


def _canonicalize_elaborated_goal(goal: str) -> str:
    lines = [line.rstrip() for line in goal.splitlines()]
    target_indices = [index for index, line in enumerate(lines) if line.startswith("⊢ ")]
    if len(target_indices) != 1:
        return goal
    target_index = target_indices[0]
    if target_index == len(lines) - 1:
        return "\n".join(lines)
    segments = [lines[target_index][2:].strip()]
    segments.extend(line.strip() for line in lines[target_index + 1 :])
    if any(not segment or segment.startswith("|") for segment in segments):
        raise GoalV1Error("unsupported multiline target layout")
    if any(
        not (segment.startswith("let ") or segment.startswith("have ")) for segment in segments[:-1]
    ):
        raise GoalV1Error("unsupported multiline target layout")
    canonical_segments = [
        ("let " + segment[5:] if segment.startswith("have ") else segment).removesuffix(";")
        for segment in segments
    ]
    canonical_target = "; ".join(canonical_segments)
    if not _is_canonical_let_target(canonical_target):
        raise GoalV1Error("malformed top-level let target layout")
    return "\n".join([*lines[:target_index], f"⊢ {canonical_target}"])


def validate_goal_v1(goal: str) -> None:
    lines = goal.splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise GoalV1Error("goal_v1 must contain nonempty physical lines")
    if goal.count("⊢") != 1 or not lines[-1].startswith("⊢ "):
        raise GoalV1Error("goal_v1 must contain exactly one final turnstile target")
    if any("⊢" in line for line in lines[:-1]):
        raise GoalV1Error("goal_v1 local lines must not contain a turnstile")
    if any(" : " not in line for line in lines[:-1]):
        raise GoalV1Error("every goal_v1 local line must contain ' : '")
    if any(":=" in line for line in lines[:-1]):
        raise GoalV1Error("goal_v1 local lines must not contain a proof/value delimiter")
    target = lines[-1][2:]
    if ":=" in target and not _is_canonical_let_target(target):
        raise GoalV1Error("goal_v1 target contains an unsupported proof/value delimiter")


def signature_to_goal_v1(signature: str) -> str:
    """Render a trusted name-free theorem signature without invoking Lean."""

    masked_signature = _mask_comments(signature).masked.strip()
    binders, after_binders = _parse_leading_binders(masked_signature)
    target_split = _split_top_level_once(after_binders, ":")
    if target_split is None:
        raise SurfaceRenderError(
            SurfaceFailureCode.MISSING_TARGET_SEPARATOR,
            "signature has no top-level ':' before its target",
        )
    before_target, target = target_split
    if before_target.strip():
        raise SurfaceRenderError(
            SurfaceFailureCode.MISSING_TARGET_SEPARATOR,
            f"unsupported text before target separator: {before_target!r}",
        )
    forall_binders, target = _peel_forall_binders(target)
    binders.extend(forall_binders)
    target = _canonicalize_surface_target(target)
    if _has_top_level_arrow(target):
        raise SurfaceRenderError(
            SurfaceFailureCode.ANONYMOUS_TOP_LEVEL_ARROW,
            "surface mode cannot recover Lean's generated name for an arrow premise",
        )
    names = [name for binder in binders for name in binder.names]
    if len(names) != len(set(names)):
        raise SurfaceRenderError(
            SurfaceFailureCode.DUPLICATE_LOCAL_NAME,
            "surface mode cannot reproduce Lean's sanitized shadowed local names",
        )
    goal = "\n".join([*_group_binders(binders), f"⊢ {target}"])
    try:
        validate_goal_v1(goal)
    except GoalV1Error as exc:
        raise SurfaceRenderError(SurfaceFailureCode.INVALID_GOAL, str(exc)) from exc
    return goal


def _build_sidecar(
    *,
    goal_v1: str,
    source: GoalV1Source,
    raw_statement: str,
    declaration_kind: str,
    compile_context: CompileContext,
    typed_alpha_fingerprint: str | None = None,
    warnings: tuple[str, ...] = (),
) -> GoalV1Sidecar:
    validate_goal_v1(goal_v1)
    raw_hash = sha256_hex(raw_statement.encode("utf-8"))
    representation_id = "repr:" + hash_canonical(
        {
            "renderer_version": RENDERER_VERSION,
            "spec_hash": SPEC_HASH,
            "goal_v1_source": source,
            "goal_v1": goal_v1,
            "raw_statement_hash": raw_hash,
            "declaration_kind": declaration_kind,
            "compile_context_id": compile_context.compile_context_id,
        }
    )
    return GoalV1Sidecar(
        record=GoalV1Record(
            representation_id=representation_id,
            goal_v1=goal_v1,
            goal_v1_source=source,
            renderer_version=RENDERER_VERSION,
            spec_hash=SPEC_HASH,
            raw_statement_hash=raw_hash,
            declaration_kind=declaration_kind,
            compile_context_id=compile_context.compile_context_id,
            typed_alpha_fingerprint=typed_alpha_fingerprint,
            warnings=warnings,
        ),
        raw_statement=raw_statement,
        compile_context=compile_context,
    )


def render_surface(
    *,
    raw_statement: str,
    declaration_kind: str,
    compile_context: CompileContext,
    parsed_signature: str | None = None,
) -> GoalV1Sidecar:
    """Render the tagged surface fallback, failing closed on ambiguous syntax."""

    _validate_kind(declaration_kind)
    signature = (
        parsed_signature
        if parsed_signature is not None
        else _extract_signature(raw_statement, declaration_kind)
    )
    goal = signature_to_goal_v1(signature)
    warnings = (
        ("trusted_complete_parsed_signature",)
        if parsed_signature is not None
        else ("raw_signature_extraction_self_contained_only",)
    )
    return _build_sidecar(
        goal_v1=goal,
        source="surface",
        raw_statement=raw_statement,
        declaration_kind=declaration_kind,
        compile_context=compile_context,
        warnings=warnings,
    )


def _helper_body() -> str:
    helper_path = find_repo_root(Path(__file__).parent) / "LeanFaith" / "Meta" / "GoalV1.lean"
    lines = helper_path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.startswith("import "))


def _lean_option_value(value: CompileOptionValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _qualified_declaration_name(
    compile_context: CompileContext,
    declaration_name: str,
    *,
    lookup_only: bool,
) -> str:
    if not compile_context.namespace_context or lookup_only:
        return declaration_name
    prefix = ".".join(compile_context.namespace_context) + "."
    if not declaration_name.startswith(prefix):
        raise ValueError(
            f"declaration {declaration_name!r} must be fully qualified with {prefix!r} "
            "when namespace_context is nonempty"
        )
    return declaration_name


def _elaborated_command(
    compile_context: CompileContext,
    declarations: Sequence[ElaboratedInput],
) -> str:
    import_lines = [
        line.strip() for line in compile_context.import_header.splitlines() if line.strip()
    ]
    imports = "\n".join(["import Lean", *(line for line in import_lines if line != "import Lean")])
    lines = [imports, _helper_body()]
    if compile_context.command_preamble.strip():
        lines.append(compile_context.command_preamble.rstrip())
    lines.extend(
        f"set_option {option_name} {_lean_option_value(value)}"
        for option_name, value in sorted(compile_context.options.items())
    )
    if compile_context.open_context:
        lines.append("open " + " ".join(compile_context.open_context))
    if compile_context.scoped_context:
        lines.append("open scoped " + " ".join(compile_context.scoped_context))
    lines.extend(f"namespace {name}" for name in compile_context.namespace_context)
    lines.extend(item.raw_statement.rstrip() for item in declarations if not item.lookup_only)
    lines.extend(f"end {name}" for name in reversed(compile_context.namespace_context))
    lines.extend(
        "lfGoalV1 "
        + json.dumps(
            _qualified_declaration_name(
                compile_context,
                item.declaration_name,
                lookup_only=item.lookup_only,
            ),
            ensure_ascii=False,
        )
        for item in declarations
    )
    return "\n".join(line for line in lines if line.strip())


def _parse_goal_payloads(
    messages: Sequence[dict[str, object]],
    expected_names: set[str],
) -> dict[str, tuple[str | None, str | None]]:
    selected: dict[str, tuple[str | None, str | None]] = {}
    for message in messages:
        for line in str(message.get("data", "")).splitlines():
            marker = line.find(GOAL_MARKER)
            if marker < 0:
                continue
            try:
                payload = json.loads(line[marker + len(GOAL_MARKER) :])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            name = payload.get("name")
            if not isinstance(name, str) or name not in expected_names:
                continue
            value = payload.get("goal_v1")
            constant_kind = payload.get("constant_kind")
            # Last matching helper payload is authoritative. A not-found or
            # malformed later payload clears a source-authored spoof.
            selected[name] = (
                value if isinstance(value, str) else None,
                constant_kind if isinstance(constant_kind, str) else None,
            )
    return selected


def render_elaborated_batch(
    backend: LeanBackend,
    *,
    declarations: Sequence[ElaboratedInput],
    compile_context: CompileContext,
    request_id: str,
    allow_sorry: bool = False,
    timeout_seconds: float = 300.0,
) -> ElaboratedBatchResult:
    """Render several constants in one request to an already-loaded backend."""

    if not declarations:
        raise ValueError("elaborated batch must contain at least one declaration")
    names = [item.declaration_name for item in declarations]
    if len(names) != len(set(names)):
        raise ValueError("elaborated declaration names must be unique within a batch")
    for item in declarations:
        _validate_kind(item.declaration_kind)
        if not item.declaration_name.strip():
            raise ValueError("elaborated declaration name must be nonempty")
        if not item.raw_statement.strip():
            raise ValueError(f"raw statement for {item.declaration_name!r} must be nonempty")
        _qualified_declaration_name(
            compile_context,
            item.declaration_name,
            lookup_only=item.lookup_only,
        )

    request = LeanRequest(
        request_id=request_id,
        context_id=compile_context.compile_context_id,
        code=_elaborated_command(compile_context, declarations),
        allow_sorry=allow_sorry,
        timeout_seconds=timeout_seconds,
    )
    result = backend.run(request)
    result_detail = result.infrastructure_error or "; ".join(
        str(message.get("data", "")) for message in result.messages
    )
    reported_sorry = result.status == LeanStatus.VALID_WITH_SORRY or bool(result.sorries)
    if reported_sorry and not allow_sorry:
        detail = result_detail or "Lean reported sorry but allow_sorry is false"
        failures = tuple(ElaboratedFailure(name, detail) for name in names)
        return ElaboratedBatchResult(
            sidecars=(),
            failures=failures,
            request_hash=result.request_hash,
            elapsed_ms=result.elapsed_ms,
            raw_response_path=result.raw_response_path,
        )
    processable_statuses = {
        LeanStatus.VALID,
        LeanStatus.VALID_WITH_SORRY,
        LeanStatus.INVALID,
    }
    if result.status not in processable_statuses:
        detail = result_detail or f"Lean renderer failed with status {result.status.value}"
        failures = tuple(ElaboratedFailure(name, detail) for name in names)
        return ElaboratedBatchResult(
            sidecars=(),
            failures=failures,
            request_hash=result.request_hash,
            elapsed_ms=result.elapsed_ms,
            raw_response_path=result.raw_response_path,
        )

    parsed = _parse_goal_payloads(result.messages, set(names))
    sidecars: list[GoalV1Sidecar] = []
    failures_list: list[ElaboratedFailure] = []
    for item in declarations:
        payload = parsed.get(item.declaration_name)
        if payload is None:
            failures_list.append(
                ElaboratedFailure(
                    item.declaration_name,
                    "missing or malformed LFGOALV1JSON payload"
                    + (f": {result_detail}" if result_detail else ""),
                )
            )
            continue
        goal, constant_kind = payload
        if goal is None:
            failures_list.append(
                ElaboratedFailure(
                    item.declaration_name,
                    "missing or malformed LFGOALV1JSON payload"
                    + (f": {result_detail}" if result_detail else ""),
                )
            )
            continue
        try:
            goal = _canonicalize_elaborated_goal(goal)
        except GoalV1Error as exc:
            failures_list.append(ElaboratedFailure(item.declaration_name, str(exc)))
            continue
        if constant_kind != "theorem":
            failures_list.append(
                ElaboratedFailure(
                    item.declaration_name,
                    f"environment constant kind is {constant_kind!r}, expected theorem",
                )
            )
            continue
        warnings = [
            "already_loaded_constant_lookup"
            if item.lookup_only
            else "inline_candidate_compiled_in_batch"
        ]
        if result.status == LeanStatus.INVALID:
            warnings.append("batch_had_lean_errors")
        if reported_sorry:
            warnings.append("compiled_with_sorry")
        try:
            sidecar = _build_sidecar(
                goal_v1=goal,
                source="elaborated",
                raw_statement=item.raw_statement,
                declaration_kind=item.declaration_kind,
                compile_context=compile_context,
                typed_alpha_fingerprint=item.typed_alpha_fingerprint,
                warnings=tuple(warnings),
            )
        except GoalV1Error as exc:
            failures_list.append(ElaboratedFailure(item.declaration_name, str(exc)))
            continue
        sidecars.append(sidecar)
    return ElaboratedBatchResult(
        sidecars=tuple(sidecars),
        failures=tuple(failures_list),
        request_hash=result.request_hash,
        elapsed_ms=result.elapsed_ms,
        raw_response_path=result.raw_response_path,
    )
