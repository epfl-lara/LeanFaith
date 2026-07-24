"""Conservative P01 alpha transformation for LF-017.

Only P01 lives here for now.  The implementation deliberately supports a
small, mechanically auditable Lean syntax subset and returns explicit
non-applicability for constructs whose binding scope it cannot prove.  It
never assigns a semantic label: ``IntendedRelation.EQUIVALENT`` is generation
provenance only.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import IntendedRelation, Polarity, QualityTier, ValidationStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import Applicability, TransformationAudit, VariantDraft
from leanfaith.transforms.protocol import build_transformation_audit, build_variant_draft

NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
SemanticVersion = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True)]

_OPEN_TO_CLOSE = {"(": ")", "{": "}", "[": "]", "⟨": "⟩"}
_CLOSE_TO_OPEN = {value: key for key, value in _OPEN_TO_CLOSE.items()}
_BINDER_KIND = {"(": "explicit", "{": "implicit", "[": "instance"}
_DECLARATION_KEYWORDS = frozenset({"theorem", "lemma"})
_BINDING_KEYWORDS = frozenset({"fun", "forall", "λ", "∀", "∃", "∃!"})
_UNSUPPORTED_SCOPE_KEYWORDS = frozenset(
    {
        "by",
        "do",
        "dite",
        "if",
        "let",
        "match",
        "nomatch",
        "where",
        "∏",
        "∑",
        "∫",
        "∀ᶠ",
        "⋂",
        "⋃",  # noqa: RUF001 - Lean n-ary union binder token
        "⨅",
        "⨆",
    }
)
_RESERVED_IDENTIFIERS = frozenset(
    {
        "as",
        "by",
        "class",
        "def",
        "do",
        "else",
        "end",
        "example",
        "exists",
        "false",
        "forall",
        "fun",
        "if",
        "in",
        "instance",
        "let",
        "match",
        "namespace",
        "nomatch",
        "open",
        "private",
        "protected",
        "structure",
        "theorem",
        "then",
        "true",
        "where",
        "with",
    }
)


class P01AlphaError(ValueError):
    """P01 cannot safely parse, replay, or audit a requested transformation."""


class _UnsupportedSyntax(P01AlphaError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class P01AlphaConfig(StrictModel):
    """Versioned, code-owned P01 execution configuration."""

    schema_version: Literal[1] = 1
    rule_id: Literal["p01_alpha"] = "p01_alpha"
    rule_version: SemanticVersion
    family_id: Literal["p01_alpha"] = "p01_alpha"
    implementation_key: Literal["p01_alpha"] = "p01_alpha"
    candidate_pool: NonEmptyStr
    fresh_identifier_prefix: Annotated[
        str,
        Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", min_length=1, strict=True),
    ]
    supported_declaration_kinds: tuple[Literal["theorem", "lemma"], ...]
    supported_binder_kinds: tuple[Literal["explicit", "implicit", "instance"], ...]
    placeholder_forms: tuple[Literal["by_sorry", "sorry"], ...]

    @model_validator(mode="after")
    def _canonical_sets(self) -> P01AlphaConfig:
        for field_name in (
            "supported_declaration_kinds",
            "supported_binder_kinds",
            "placeholder_forms",
        ):
            values = getattr(self, field_name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{field_name} must be sorted and unique")
            if not values:
                raise ValueError(f"{field_name} cannot be empty")
        return self


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int

    @property
    def identifier(self) -> str | None:
        if self.kind == "identifier":
            return self.text
        if self.kind == "quoted_identifier":
            return self.text[1:-1]
        return None


@dataclass(frozen=True, slots=True)
class _Binder:
    ordinal: int
    token_index: int
    group_open_index: int
    group_close_index: int
    kind: str
    identifier: str

    def node_id(self) -> str:
        return f"binder:{self.ordinal:04d}:{self.kind}:{self.identifier}"


@dataclass(frozen=True, slots=True)
class _ParsedDeclaration:
    source: str
    tokens: tuple[_Token, ...]
    significant: tuple[int, ...]
    matching_delimiters: Mapping[int, int]
    declaration_kind: str
    declaration_name: str
    declaration_name_span: tuple[int, int]
    binders: tuple[_Binder, ...]
    result_colon_position: int
    assignment_position: int
    placeholder_form: str


@dataclass(frozen=True, slots=True)
class _Replacement:
    start: int
    end: int
    old: str
    new: str
    role: str


@dataclass(frozen=True, slots=True)
class _RenamePlan:
    binder: _Binder
    new_identifier: str
    replacements: tuple[_Replacement, ...]


def load_p01_alpha_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[P01AlphaConfig]:
    """Load the strict P01 config from the repository."""

    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/p01_alpha.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise P01AlphaError("p01 config path escapes the repository")
    return load_config(resolved, P01AlphaConfig)


def _is_identifier_start(character: str) -> bool:
    category = unicodedata.category(character)
    return character == "_" or character.isalpha() or category.startswith("L")


def _is_identifier_continue(character: str) -> bool:
    category = unicodedata.category(character)
    return (
        _is_identifier_start(character)
        or character.isdigit()
        or character == "'"
        or category.startswith(("M", "N"))
    )


def _tokenize(source: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    while index < len(source):
        start = index
        character = source[index]
        if character.isspace():
            index += 1
            while index < len(source) and source[index].isspace():
                index += 1
            tokens.append(_Token("trivia", source[start:index], start, index))
            continue
        if source.startswith("--", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline
            tokens.append(_Token("trivia", source[start:index], start, index))
            continue
        if source.startswith("/-", index):
            depth = 1
            index += 2
            while index < len(source) and depth:
                if source.startswith("/-", index):
                    depth += 1
                    index += 2
                elif source.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise _UnsupportedSyntax("unterminated_block_comment")
            tokens.append(_Token("trivia", source[start:index], start, index))
            continue
        if character == '"':
            index += 1
            escaped = False
            while index < len(source):
                current = source[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
            else:
                raise _UnsupportedSyntax("unterminated_string_literal")
            tokens.append(_Token("literal", source[start:index], start, index))
            continue
        if character == "'":
            index += 1
            escaped = False
            while index < len(source):
                current = source[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == "'":
                    break
            else:
                raise _UnsupportedSyntax("unterminated_character_literal")
            tokens.append(_Token("literal", source[start:index], start, index))
            continue
        if character == "«":
            close = source.find("»", index + 1)
            if close < 0:
                raise _UnsupportedSyntax("unterminated_quoted_identifier")
            index = close + 1
            tokens.append(_Token("quoted_identifier", source[start:index], start, index))
            continue
        if _is_identifier_start(character):
            index += 1
            while index < len(source) and _is_identifier_continue(source[index]):
                index += 1
            text = source[start:index]
            kind = "keyword" if text in _DECLARATION_KEYWORDS | _BINDING_KEYWORDS else "identifier"
            tokens.append(_Token(kind, text, start, index))
            continue
        if character.isdigit():
            index += 1
            while index < len(source) and (source[index].isalnum() or source[index] in {"_", "."}):
                index += 1
            tokens.append(_Token("literal", source[start:index], start, index))
            continue
        matched = False
        for symbol in (":=", "=>", "->", "//", "∀ᶠ", "∃!", "⟶", "↦"):
            if source.startswith(symbol, index):
                index += len(symbol)
                tokens.append(_Token("symbol", symbol, start, index))
                matched = True
                break
        if matched:
            continue
        index += 1
        tokens.append(_Token("symbol", character, start, index))
    return tuple(tokens)


def _significant(tokens: Sequence[_Token]) -> tuple[int, ...]:
    return tuple(index for index, token in enumerate(tokens) if token.kind != "trivia")


def _delimiter_pairs(tokens: Sequence[_Token], significant: Sequence[int]) -> dict[int, int]:
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for token_index in significant:
        text = tokens[token_index].text
        if text in _OPEN_TO_CLOSE:
            stack.append(token_index)
        elif text in _CLOSE_TO_OPEN:
            if not stack or tokens[stack[-1]].text != _CLOSE_TO_OPEN[text]:
                raise _UnsupportedSyntax("unbalanced_delimiters")
            opener = stack.pop()
            pairs[opener] = token_index
            pairs[token_index] = opener
    if stack:
        raise _UnsupportedSyntax("unbalanced_delimiters")
    return pairs


def _position_map(significant: Sequence[int]) -> dict[int, int]:
    return {token_index: position for position, token_index in enumerate(significant)}


def _top_level_positions(
    tokens: Sequence[_Token],
    significant: Sequence[int],
) -> dict[int, int]:
    depths: dict[int, int] = {}
    depth = 0
    for position, token_index in enumerate(significant):
        token = tokens[token_index]
        if token.text in _CLOSE_TO_OPEN:
            depth -= 1
        depths[position] = depth
        if token.text in _OPEN_TO_CLOSE:
            depth += 1
        if depth < 0:
            raise _UnsupportedSyntax("unbalanced_delimiters")
    if depth:
        raise _UnsupportedSyntax("unbalanced_delimiters")
    return depths


def _parse_group_binders(
    parsed_tokens: Sequence[_Token],
    significant: Sequence[int],
    position_by_token: Mapping[int, int],
    matching: Mapping[int, int],
    open_token_index: int,
) -> tuple[tuple[int, ...], tuple[int, int] | None]:
    close_token_index = matching[open_token_index]
    open_position = position_by_token[open_token_index]
    close_position = position_by_token[close_token_index]
    depth = 0
    colon_position: int | None = None
    for position in range(open_position + 1, close_position):
        token = parsed_tokens[significant[position]]
        if token.text in _CLOSE_TO_OPEN:
            depth -= 1
        if depth == 0 and token.text == ":":
            colon_position = position
            break
        if token.text in _OPEN_TO_CLOSE:
            depth += 1
    if colon_position is None:
        return (), None
    binder_tokens: list[int] = []
    for position in range(open_position + 1, colon_position):
        token_index = significant[position]
        token = parsed_tokens[token_index]
        if token.identifier is None:
            raise _UnsupportedSyntax("unsupported_binder_pattern")
        if token.identifier != "_":
            binder_tokens.append(token_index)
    return tuple(binder_tokens), (colon_position + 1, close_position)


def _declaration_name(
    tokens: Sequence[_Token],
    significant: Sequence[int],
    keyword_position: int,
) -> tuple[str, tuple[int, int], int]:
    position = keyword_position + 1
    if position >= len(significant):
        raise _UnsupportedSyntax("missing_declaration_name")
    first = tokens[significant[position]]
    if first.identifier is None:
        raise _UnsupportedSyntax("unsupported_declaration_name")
    start = first.start
    end = first.end
    parts = [first.text]
    position += 1
    while position + 1 < len(significant):
        dot = tokens[significant[position]]
        next_token = tokens[significant[position + 1]]
        if dot.text != "." or next_token.identifier is None:
            break
        parts.extend((dot.text, next_token.text))
        end = next_token.end
        position += 2
    return "".join(parts), (start, end), position


def _parse_declaration(source: str, config: P01AlphaConfig) -> _ParsedDeclaration:
    tokens = _tokenize(source)
    significant = _significant(tokens)
    if not significant:
        raise _UnsupportedSyntax("empty_declaration")
    matching = _delimiter_pairs(tokens, significant)
    position_by_token = _position_map(significant)
    depths = _top_level_positions(tokens, significant)
    keyword_positions = [
        position
        for position, token_index in enumerate(significant)
        if depths[position] == 0 and tokens[token_index].text in _DECLARATION_KEYWORDS
    ]
    if len(keyword_positions) != 1:
        raise _UnsupportedSyntax("ambiguous_declaration_head")
    keyword_position = keyword_positions[0]
    kind = tokens[significant[keyword_position]].text
    if kind not in config.supported_declaration_kinds:
        raise _UnsupportedSyntax("unsupported_declaration_kind")
    name, name_span, scan_start = _declaration_name(tokens, significant, keyword_position)

    assignment_positions = [
        position
        for position in range(scan_start, len(significant))
        if depths[position] == 0 and tokens[significant[position]].text == ":="
    ]
    if len(assignment_positions) != 1:
        raise _UnsupportedSyntax("ambiguous_proof_assignment")
    assignment_position = assignment_positions[0]
    tail = [
        tokens[significant[position]].text
        for position in range(assignment_position + 1, len(significant))
    ]
    if tail == ["by", "sorry"] and "by_sorry" in config.placeholder_forms:
        placeholder_form = "by_sorry"
    elif tail == ["sorry"] and "sorry" in config.placeholder_forms:
        placeholder_form = "sorry"
    else:
        raise _UnsupportedSyntax("unsupported_proof_placeholder")

    result_colons = [
        position
        for position in range(scan_start, assignment_position)
        if depths[position] == 0 and tokens[significant[position]].text == ":"
    ]
    if len(result_colons) != 1:
        raise _UnsupportedSyntax("ambiguous_result_colon")
    result_colon_position = result_colons[0]

    binders: list[_Binder] = []
    position = scan_start
    while position < result_colon_position:
        token_index = significant[position]
        token = tokens[token_index]
        if depths[position] != 0:
            raise _UnsupportedSyntax("ambiguous_header_scope")
        if token.text == ".":
            # Universe parameters after the declaration name: ``foo.{u}``.
            if position + 1 >= result_colon_position:
                raise _UnsupportedSyntax("malformed_universe_parameters")
            universe_open_index = significant[position + 1]
            if tokens[universe_open_index].text != "{":
                raise _UnsupportedSyntax("malformed_universe_parameters")
            position = position_by_token[matching[universe_open_index]] + 1
            continue
        if token.text not in _BINDER_KIND:
            raise _UnsupportedSyntax("unsupported_header_token")
        close_token_index = matching[token_index]
        close_position = position_by_token[close_token_index]
        binder_token_indices, _type_range = _parse_group_binders(
            tokens,
            significant,
            position_by_token,
            matching,
            token_index,
        )
        if not binder_token_indices and token.text != "[":
            raise _UnsupportedSyntax("unsupported_untyped_binder")
        for binder_token_index in binder_token_indices:
            binder = _Binder(
                ordinal=len(binders),
                token_index=binder_token_index,
                group_open_index=token_index,
                group_close_index=close_token_index,
                kind=_BINDER_KIND[token.text],
                identifier=tokens[binder_token_index].identifier or "",
            )
            if binder.kind in config.supported_binder_kinds:
                binders.append(binder)
        position = close_position + 1

    return _ParsedDeclaration(
        source=source,
        tokens=tokens,
        significant=significant,
        matching_delimiters=matching,
        declaration_kind=kind,
        declaration_name=name,
        declaration_name_span=name_span,
        binders=tuple(binders),
        result_colon_position=result_colon_position,
        assignment_position=assignment_position,
        placeholder_form=placeholder_form,
    )


def _binder_sequence(
    parsed: _ParsedDeclaration,
    start_position: int,
    end_position: int,
) -> tuple[set[str], set[int], tuple[tuple[int, int], ...]]:
    """Parse a conservative binder sequence before ``=>`` or ``,`.

    Returns bound names, declaration token indices, and type-expression ranges
    that remain in the enclosing scope.
    """

    names: set[str] = set()
    declarations: set[int] = set()
    type_ranges: list[tuple[int, int]] = []
    position_by_token = _position_map(parsed.significant)
    position = start_position
    ungrouped: list[int] = []
    colon_position: int | None = None
    while position < end_position:
        token_index = parsed.significant[position]
        token = parsed.tokens[token_index]
        if token.text in _BINDER_KIND:
            if ungrouped or colon_position is not None:
                raise _UnsupportedSyntax("mixed_ungrouped_binder_syntax")
            close = parsed.matching_delimiters[token_index]
            close_position = position_by_token[close]
            if close_position >= end_position:
                raise _UnsupportedSyntax("binder_group_crosses_separator")
            group_declarations, type_range = _parse_group_binders(
                parsed.tokens,
                parsed.significant,
                position_by_token,
                parsed.matching_delimiters,
                token_index,
            )
            if not group_declarations and token.text != "[":
                raise _UnsupportedSyntax("unsupported_untyped_binder")
            for declaration in group_declarations:
                identifier = parsed.tokens[declaration].identifier
                if identifier is not None and identifier != "_":
                    names.add(identifier)
                    declarations.add(declaration)
            if type_range is not None and type_range[0] < type_range[1]:
                type_ranges.append(type_range)
            position = close_position + 1
            continue
        if token.text == ":":
            if not ungrouped or colon_position is not None:
                raise _UnsupportedSyntax("unsupported_binder_colon")
            colon_position = position
            position += 1
            continue
        if colon_position is None:
            if token.identifier is None:
                raise _UnsupportedSyntax("unsupported_binder_pattern")
            if token.identifier != "_":
                ungrouped.append(token_index)
                names.add(token.identifier)
                declarations.add(token_index)
        position += 1
    if colon_position is not None and colon_position + 1 < end_position:
        type_ranges.append((colon_position + 1, end_position))
    return names, declarations, tuple(type_ranges)


def _find_separator(
    parsed: _ParsedDeclaration,
    start_position: int,
    end_position: int,
    separators: frozenset[str],
) -> int:
    position_by_token = _position_map(parsed.significant)
    position = start_position
    while position < end_position:
        token_index = parsed.significant[position]
        token = parsed.tokens[token_index]
        if token.text in _OPEN_TO_CLOSE:
            position = position_by_token[parsed.matching_delimiters[token_index]] + 1
            continue
        if token.text in separators:
            return position
        position += 1
    raise _UnsupportedSyntax("missing_binding_separator")


def _is_variable_occurrence(parsed: _ParsedDeclaration, position: int) -> bool:
    previous = parsed.tokens[parsed.significant[position - 1]].text if position > 0 else None
    following = (
        parsed.tokens[parsed.significant[position + 1]].text
        if position + 1 < len(parsed.significant)
        else None
    )
    # ``Foo.x`` is a qualified-name suffix; ``(x := value)`` is a named
    # argument label.  Neither denotes the bound local named ``x``.
    is_named_argument_label = following == ":=" and previous in {"(", ",", "{"}
    return previous != "." and not is_named_argument_label


def _collect_occurrences(
    parsed: _ParsedDeclaration,
    old_identifier: str,
    start_position: int,
    end_position: int,
    *,
    active: bool,
) -> list[int]:
    occurrences: list[int] = []
    position_by_token = _position_map(parsed.significant)
    position = start_position
    while position < end_position:
        token_index = parsed.significant[position]
        token = parsed.tokens[token_index]
        if token.text in _UNSUPPORTED_SCOPE_KEYWORDS:
            raise _UnsupportedSyntax(f"unsupported_scope_{token.text}")
        if token.text in _BINDING_KEYWORDS:
            separator = _find_separator(
                parsed,
                position + 1,
                end_position,
                frozenset({"=>", ","}),
            )
            names, _declarations, type_ranges = _binder_sequence(
                parsed,
                position + 1,
                separator,
            )
            for type_start, type_end in type_ranges:
                occurrences.extend(
                    _collect_occurrences(
                        parsed,
                        old_identifier,
                        type_start,
                        type_end,
                        active=active,
                    )
                )
            occurrences.extend(
                _collect_occurrences(
                    parsed,
                    old_identifier,
                    separator + 1,
                    end_position,
                    active=active and old_identifier not in names,
                )
            )
            return occurrences
        if token.text in _OPEN_TO_CLOSE:
            close_token_index = parsed.matching_delimiters[token_index]
            close_position = position_by_token[close_token_index]
            if close_position >= end_position:
                raise _UnsupportedSyntax("delimiter_crosses_scope")
            if token.text == "{":
                set_separator: int | None
                try:
                    set_separator = _find_separator(
                        parsed,
                        position + 1,
                        close_position,
                        frozenset({"//", "|"}),
                    )
                except _UnsupportedSyntax:
                    set_separator = None
                if set_separator is not None:
                    names, declarations, type_ranges = _binder_sequence(
                        parsed,
                        position + 1,
                        set_separator,
                    )
                    if len(declarations) != 1:
                        raise _UnsupportedSyntax("unsupported_set_binder_pattern")
                    for type_start, type_end in type_ranges:
                        occurrences.extend(
                            _collect_occurrences(
                                parsed,
                                old_identifier,
                                type_start,
                                type_end,
                                active=active,
                            )
                        )
                    occurrences.extend(
                        _collect_occurrences(
                            parsed,
                            old_identifier,
                            set_separator + 1,
                            close_position,
                            active=active and old_identifier not in names,
                        )
                    )
                    position = close_position + 1
                    continue
            after_close = close_position + 1
            arrow_after = after_close < end_position and parsed.tokens[
                parsed.significant[after_close]
            ].text in {"→", "->"}
            if arrow_after:
                group_declarations, type_range = _parse_group_binders(
                    parsed.tokens,
                    parsed.significant,
                    position_by_token,
                    parsed.matching_delimiters,
                    token_index,
                )
            else:
                group_declarations, type_range = (), None
            if group_declarations:
                if type_range is not None:
                    occurrences.extend(
                        _collect_occurrences(
                            parsed,
                            old_identifier,
                            type_range[0],
                            type_range[1],
                            active=active,
                        )
                    )
                group_names: set[str] = set()
                for index in group_declarations:
                    identifier = parsed.tokens[index].identifier
                    if identifier is not None:
                        group_names.add(identifier)
                occurrences.extend(
                    _collect_occurrences(
                        parsed,
                        old_identifier,
                        after_close + 1,
                        end_position,
                        active=active and old_identifier not in group_names,
                    )
                )
                return occurrences
            occurrences.extend(
                _collect_occurrences(
                    parsed,
                    old_identifier,
                    position + 1,
                    close_position,
                    active=active,
                )
            )
            position = close_position + 1
            continue
        if (
            active
            and token.identifier == old_identifier
            and _is_variable_occurrence(parsed, position)
        ):
            occurrences.append(token_index)
        position += 1
    return occurrences


def _rename_plan(
    parsed: _ParsedDeclaration,
    binder: _Binder,
    new_identifier: str,
) -> _RenamePlan:
    if new_identifier in _RESERVED_IDENTIFIERS:
        raise P01AlphaError("fresh identifier is a reserved Lean keyword")
    position_by_token = _position_map(parsed.significant)
    close_position = position_by_token[binder.group_close_index]
    occurrence_indices: list[int] = []
    active = True
    position = close_position + 1
    # Command binders are sequential: a later binder's *type* is still in the
    # outer scope, while a same-named later binder shadows the selected local
    # only after its closing delimiter.
    while position < parsed.result_colon_position:
        token_index = parsed.significant[position]
        token = parsed.tokens[token_index]
        if token.text not in _BINDER_KIND:
            raise _UnsupportedSyntax("unsupported_header_scope")
        close_token_index = parsed.matching_delimiters[token_index]
        later_close_position = position_by_token[close_token_index]
        declaration_indices, type_range = _parse_group_binders(
            parsed.tokens,
            parsed.significant,
            position_by_token,
            parsed.matching_delimiters,
            token_index,
        )
        if type_range is not None:
            occurrence_indices.extend(
                _collect_occurrences(
                    parsed,
                    binder.identifier,
                    type_range[0],
                    type_range[1],
                    active=active,
                )
            )
        later_names = {
            parsed.tokens[index].identifier
            for index in declaration_indices
            if parsed.tokens[index].identifier is not None
        }
        if binder.identifier in later_names:
            active = False
        position = later_close_position + 1
    occurrence_indices.extend(
        _collect_occurrences(
            parsed,
            binder.identifier,
            parsed.result_colon_position + 1,
            parsed.assignment_position,
            active=active,
        )
    )
    replacements = [
        _Replacement(
            start=parsed.tokens[binder.token_index].start,
            end=parsed.tokens[binder.token_index].end,
            old=parsed.tokens[binder.token_index].text,
            new=new_identifier,
            role="binder_declaration",
        )
    ]
    replacements.extend(
        _Replacement(
            start=parsed.tokens[token_index].start,
            end=parsed.tokens[token_index].end,
            old=parsed.tokens[token_index].text,
            new=new_identifier,
            role="bound_occurrence",
        )
        for token_index in occurrence_indices
    )
    return _RenamePlan(
        binder=binder,
        new_identifier=new_identifier,
        replacements=tuple(sorted(replacements, key=lambda item: item.start)),
    )


def _apply_replacements(
    source: str,
    replacements: Sequence[_Replacement],
) -> tuple[str, tuple[dict[str, object], ...]]:
    output: list[str] = []
    inverse: list[dict[str, object]] = []
    source_cursor = 0
    candidate_cursor = 0
    for replacement in replacements:
        if replacement.start < source_cursor:
            raise P01AlphaError("overlapping alpha-renaming replacements")
        if source[replacement.start : replacement.end] != replacement.old:
            raise P01AlphaError("replacement source span no longer matches")
        unchanged = source[source_cursor : replacement.start]
        output.extend((unchanged, replacement.new))
        candidate_cursor += len(unchanged)
        candidate_start = candidate_cursor
        candidate_end = candidate_start + len(replacement.new)
        inverse.append(
            {
                "candidate_end": candidate_end,
                "candidate_start": candidate_start,
                "from_identifier": replacement.new,
                "operation": "alpha_rename_inverse",
                "role": replacement.role,
                "to_identifier": replacement.old,
            }
        )
        candidate_cursor = candidate_end
        source_cursor = replacement.end
    output.append(source[source_cursor:])
    return "".join(output), tuple(inverse)


def replay_inverse_trace(
    candidate_code: str,
    inverse_trace: Sequence[Mapping[str, object]],
) -> str:
    """Replay an exact P01 inverse trace, failing closed on stale spans."""

    replacements: list[_Replacement] = []
    for entry in inverse_trace:
        if entry.get("operation") != "alpha_rename_inverse":
            raise P01AlphaError("inverse trace contains a non-P01 operation")
        start = entry.get("candidate_start")
        end = entry.get("candidate_end")
        old = entry.get("from_identifier")
        new = entry.get("to_identifier")
        role = entry.get("role")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or not isinstance(old, str)
            or not isinstance(new, str)
            or not isinstance(role, str)
        ):
            raise P01AlphaError("inverse trace entry has invalid fields")
        replacements.append(_Replacement(start, end, old, new, role))
    restored, _ = _apply_replacements(candidate_code, replacements)
    return restored


def _fresh_identifier(
    parsed: _ParsedDeclaration,
    binder: _Binder,
    *,
    theorem_id: str,
    seed: int,
    prefix: str,
) -> str:
    identifiers = {
        identifier for token in parsed.tokens if (identifier := token.identifier) is not None
    }
    salt = 0
    while True:
        digest = hashlib.sha256(
            f"p01-alpha-v1\0{theorem_id}\0{seed}\0{binder.ordinal}\0{salt}".encode()
        ).hexdigest()
        candidate = f"{prefix}_{digest[:12]}"
        if candidate not in identifiers and candidate not in _RESERVED_IDENTIFIERS:
            return candidate
        salt += 1


def _selected_binder(
    parsed: _ParsedDeclaration,
    theorem_id: str,
    seed: int,
) -> _Binder:
    if not parsed.binders:
        raise P01AlphaError("no eligible binder")
    digest = hashlib.sha256(f"p01-select-v1\0{theorem_id}\0{seed}".encode()).digest()
    index = int.from_bytes(digest[:8], "big") % len(parsed.binders)
    return parsed.binders[index]


def _source_trace(
    parsed: _ParsedDeclaration,
    plan: _RenamePlan,
    candidate_code: str,
) -> tuple[dict[str, object], ...]:
    return (
        {
            "binder_kind": plan.binder.kind,
            "binder_ordinal": plan.binder.ordinal,
            "candidate_code_hash": sha256_hex(candidate_code.encode()),
            "declaration_name": parsed.declaration_name,
            "from_identifier": plan.binder.identifier,
            "operation": "alpha_rename",
            "placeholder_form": parsed.placeholder_form,
            "replacement_spans": [
                {
                    "end": replacement.end,
                    "role": replacement.role,
                    "start": replacement.start,
                }
                for replacement in plan.replacements
            ],
            "source_code_hash": sha256_hex(parsed.source.encode()),
            "to_identifier": plan.new_identifier,
        },
    )


class P01AlphaRule:
    """Seeded, capture-avoiding alpha rename over a conservative Lean subset."""

    rule_id = "p01_alpha"
    family_id = "p01_alpha"
    polarity = Polarity.POSITIVE
    implementation_key = "p01_alpha"

    def __init__(
        self,
        *,
        generation_config_hash: str,
        config: P01AlphaConfig,
        rule_config_hash: str,
    ) -> None:
        if len(generation_config_hash) != 64:
            raise P01AlphaError("generation_config_hash must be a sha256 hex digest")
        self.generation_config_hash = generation_config_hash
        self.config = config
        self.rule_config_hash = rule_config_hash
        self.rule_version = config.rule_version

    @classmethod
    def from_repository(
        cls,
        *,
        generation_config_hash: str,
        repo_root: Path | None = None,
    ) -> P01AlphaRule:
        loaded = load_p01_alpha_config(repo_root)
        return cls(
            generation_config_hash=generation_config_hash,
            config=loaded.config,
            rule_config_hash=loaded.config_hash,
        )

    def _parse_inputs(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> _ParsedDeclaration:
        if not theorem.is_proposition:
            raise _UnsupportedSyntax("source_not_proposition")
        source = theorem.proof_stripped_declaration
        if representation.raw_proof_stripped != source:
            raise _UnsupportedSyntax("source_representation_mismatch")
        if representation.alpha_identity_fingerprint is None:
            raise _UnsupportedSyntax("missing_alpha_identity_fingerprint")
        return _parse_declaration(source, self.config)

    def assess(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> Applicability:
        try:
            parsed = self._parse_inputs(theorem, representation)
            if not parsed.binders:
                raise _UnsupportedSyntax("no_eligible_binder")
            # Prove that every advertised binder has a supported lexical scope.
            for binder in parsed.binders:
                fresh = _fresh_identifier(
                    parsed,
                    binder,
                    theorem_id=theorem.theorem_id,
                    seed=0,
                    prefix=self.config.fresh_identifier_prefix,
                )
                _rename_plan(parsed, binder, fresh)
        except _UnsupportedSyntax as exc:
            return Applicability(applicable=False, reason_codes=(exc.reason_code,))
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=tuple(sorted(binder.node_id() for binder in parsed.binders)),
            required_capabilities=(
                "alpha_identity_fingerprint",
                "lean_reelaboration",
            ),
            metadata={
                "eligible_binder_count": len(parsed.binders),
                "rule_config_hash": self.rule_config_hash,
            },
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> Sequence[VariantDraft]:
        applicability = self.assess(theorem, representation)
        if not applicability.applicable:
            raise P01AlphaError(
                "P01 generate called for a non-applicable theorem: "
                + ",".join(applicability.reason_codes)
            )
        parsed = self._parse_inputs(theorem, representation)
        binder = _selected_binder(parsed, theorem.theorem_id, seed)
        new_identifier = _fresh_identifier(
            parsed,
            binder,
            theorem_id=theorem.theorem_id,
            seed=seed,
            prefix=self.config.fresh_identifier_prefix,
        )
        plan = _rename_plan(parsed, binder, new_identifier)
        candidate_code, inverse_trace = _apply_replacements(parsed.source, plan.replacements)
        if replay_inverse_trace(candidate_code, inverse_trace) != parsed.source:
            raise P01AlphaError("P01 inverse trace does not restore the source declaration")
        draft = build_variant_draft(
            source_theorem_ids=(theorem.theorem_id,),
            source_representation_ids=(representation.representation_id,),
            context_id=theorem.context_id,
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            family_id=self.family_id,
            seed=seed,
            candidate_code=candidate_code,
            intended_relation=IntendedRelation.EQUIVALENT,
            intended_error_types=(),
            candidate_pool=self.config.candidate_pool,
            transformation_trace=_source_trace(parsed, plan, candidate_code),
            inverse_trace=inverse_trace,
            expected_atom_mapping={},
            expected_structural_diff={
                "binder_renames": 1,
                "changed_identifier_tokens": len(plan.replacements),
                "declaration_name_preserved": True,
                "kind": "alpha_rename",
                "placeholder_preserved": True,
                "rule_config_hash": self.rule_config_hash,
            },
            generation_config_hash=self.generation_config_hash,
            metadata={"intention_is_not_label": True},
        )
        return (draft,)

    def audit(
        self,
        source: TheoremRecord,
        source_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit:
        applicability = self.assess(source, source_representation)
        violations: list[str] = []
        inverse_ok = False
        try:
            inverse_ok = (
                draft.inverse_trace is not None
                and replay_inverse_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except P01AlphaError:
            inverse_ok = False
        if not inverse_ok:
            violations.append("inverse_roundtrip_failed")
        if candidate.proof_stripped_declaration != draft.candidate_code:
            violations.append("candidate_code_mismatch")
        if source.proof_stripped_declaration == draft.candidate_code:
            violations.append("candidate_unchanged")
        alpha_ok = (
            source_representation.alpha_identity_fingerprint is not None
            and source_representation.alpha_identity_fingerprint
            == candidate_representation.alpha_identity_fingerprint
        )
        if not alpha_ok:
            violations.append("alpha_identity_mismatch")
        atoms_ok = (
            source_representation.semantic_atoms is not None
            and source_representation.semantic_atoms == candidate_representation.semantic_atoms
        )
        if not atoms_ok:
            violations.append("semantic_atoms_mismatch")
        elaborates = candidate.elaboration_status in {
            ValidationStatus.ELABORATES,
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        }
        if not elaborates:
            violations.append("candidate_not_elaborated")
        status = (
            candidate.elaboration_status
            if elaborates and not violations
            else ValidationStatus.QUARANTINED
        )
        return build_transformation_audit(
            draft=draft,
            applicability=applicability,
            audit_config_hash=self.rule_config_hash,
            recommended_validation_status=status,
            recommended_quality_tier=(
                QualityTier.PROVISIONAL if not violations else QualityTier.UNKNOWN
            ),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=inverse_ok,
            atom_mapping_ok=atoms_ok,
            inverse_or_roundtrip_ok=inverse_ok,
            violation_codes=tuple(sorted(violations)),
            metadata={
                "alpha_identity_ok": alpha_ok,
                "intention_is_not_label": True,
            },
        )


__all__ = [
    "P01AlphaConfig",
    "P01AlphaError",
    "P01AlphaRule",
    "load_p01_alpha_config",
    "replay_inverse_trace",
]
