"""Deterministic cheap splitters for CPT2 theorem/proof rows.

All splitters return byte-for-byte source slices.  The selected ``by`` token is
not retained in either field, so the lossless invariant is always
``theorem + "by" + body == source``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

RAW_REVERSE_METHOD = "raw_reverse_v1"
MASKED_REVERSE_METHOD = "masked_reverse_v1"
DECLARATION_AWARE_METHOD = "declaration_aware_v3"

_DECLARATION_TOKEN = re.compile(r"(?<![\w'])\b(?:theorem|lemma)\b(?![\w'])")
_FLEXIBLE_DELIMITER = re.compile(r":=\s+by\b")
_GROUP_OPEN = "([{"
_GROUP_CLOSE = ")]}"
_GROUP_MATCH = dict(zip(_GROUP_CLOSE, _GROUP_OPEN, strict=True))


@dataclass(frozen=True, slots=True)
class SplitResult:
    """One exact source partition around a selected ``by`` token."""

    method: str
    theorem: str
    body: str
    by_offset: int

    def reconstruct(self) -> str:
        return self.theorem + "by" + self.body


def _is_identifier_char(char: str) -> bool:
    return char == "_" or char == "'" or char.isalnum()


def _char_literal_finish(source: str, start: int) -> int | None:
    """Return the end of a compact Lean character literal, if present.

    Apostrophes are valid in identifiers, so this deliberately recognizes only
    a closed, whitespace-free literal beginning outside an identifier.
    """

    if start > 0 and _is_identifier_char(source[start - 1]):
        return None
    index = start + 1
    if index >= len(source) or source[index] in "\r\n'":
        return None
    escaped = False
    while index < len(source) and index - start <= 32:
        char = source[index]
        if char in "\r\n":
            return None
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "'":
            return index + 1
        index += 1
    return None


def mask_lean_source(source: str) -> str:
    """Mask comments, strings, chars, and quoted identifiers without moving offsets."""

    masked = list(source)
    index = 0
    block_depth = 0
    line_comment = False
    in_string = False
    escaped = False
    quoted_identifier = False

    def hide(position: int) -> None:
        if source[position] not in "\r\n":
            masked[position] = " "

    while index < len(source):
        if line_comment:
            if source[index] in "\r\n":
                line_comment = False
            else:
                hide(index)
            index += 1
            continue
        if block_depth:
            if source.startswith("/-", index):
                hide(index)
                hide(index + 1)
                block_depth += 1
                index += 2
            elif source.startswith("-/", index):
                hide(index)
                hide(index + 1)
                block_depth -= 1
                index += 2
            else:
                hide(index)
                index += 1
            continue
        if in_string:
            char = source[index]
            hide(index)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if quoted_identifier:
            char = source[index]
            hide(index)
            if char == "»":
                quoted_identifier = False
            index += 1
            continue
        if source.startswith("--", index):
            hide(index)
            hide(index + 1)
            line_comment = True
            index += 2
            continue
        if source.startswith("/-", index):
            hide(index)
            hide(index + 1)
            block_depth = 1
            index += 2
            continue
        if source[index] == '"':
            hide(index)
            in_string = True
            index += 1
            continue
        if source[index] == "«":
            hide(index)
            quoted_identifier = True
            index += 1
            continue
        if source[index] == "'":
            finish = _char_literal_finish(source, index)
            if finish is not None:
                for position in range(index, finish):
                    hide(position)
                index = finish
                continue
        index += 1
    return "".join(masked)


def _build_result(source: str, *, method: str, by_offset: int) -> SplitResult | None:
    if by_offset < 0 or source[by_offset : by_offset + 2] != "by":
        return None
    result = SplitResult(
        method=method,
        theorem=source[:by_offset],
        body=source[by_offset + 2 :],
        by_offset=by_offset,
    )
    if result.reconstruct() != source:
        raise AssertionError("CPT2 splitter violated exact source round-trip")
    return result


def split_raw_reverse(source: str) -> SplitResult | None:
    """Split at the last literal ``:= by`` in the raw source."""

    delimiter = source.rfind(":= by")
    if delimiter < 0:
        return None
    return _build_result(
        source,
        method=RAW_REVERSE_METHOD,
        by_offset=delimiter + len(":= "),
    )


def split_masked_reverse(source: str) -> SplitResult | None:
    """Split at the last literal ``:= by`` outside masked syntax."""

    delimiter = mask_lean_source(source).rfind(":= by")
    if delimiter < 0:
        return None
    return _build_result(
        source,
        method=MASKED_REVERSE_METHOD,
        by_offset=delimiter + len(":= "),
    )


def _group_depth_at_zero(masked: str, start: int, finish: int) -> bool:
    stack: list[str] = []
    for char in masked[start:finish]:
        if char in _GROUP_OPEN:
            stack.append(char)
        elif char in _GROUP_CLOSE and stack and stack[-1] == _GROUP_MATCH[char]:
            stack.pop()
    return not stack


def declaration_delimiters(source: str, *, start: int = 0) -> tuple[int, ...]:
    """Return declaration-level ``by`` offsets after ``start``.

    This helper is also used after a Lean-reported signature range.  It is
    intentionally a delimiter scanner, not a declaration parser.
    """

    masked = mask_lean_source(source)
    offsets: list[int] = []
    for match in _FLEXIBLE_DELIMITER.finditer(masked, start):
        if _group_depth_at_zero(masked, start, match.start()):
            offsets.append(match.end() - 2)
    return tuple(offsets)


def _command_declaration_starts(masked: str) -> tuple[int, ...]:
    candidates: list[tuple[int, int]] = []
    allowed_prefix = re.compile(
        r"^(?:(?:private|protected|noncomputable|unsafe|local)\s+|@\[[^\]]*\]\s*)*$"
    )
    for match in _DECLARATION_TOKEN.finditer(masked):
        line_start = masked.rfind("\n", 0, match.start()) + 1
        prefix = masked[line_start : match.start()]
        stripped = prefix.lstrip(" \t")
        if not allowed_prefix.fullmatch(stripped):
            continue
        indentation = len(prefix) - len(stripped)
        candidates.append((match.start(), indentation))
    if not candidates:
        return ()
    minimum_indentation = min(indentation for _, indentation in candidates)
    return tuple(start for start, indentation in candidates if indentation == minimum_indentation)


def _delimiter_belongs_to_let(masked: str, declaration_start: int, by_offset: int) -> bool:
    """Whether the delimiter closes a ``let`` in the theorem target.

    A target may contain proof-valued lets before the declaration's own proof,
    e.g. ``: let x := by ...; P x := by ...``. The last ``let`` before a
    delimiter owns it exactly when no earlier assignment follows that token.
    """

    delimiter = masked.rfind(":=", declaration_start, by_offset)
    if delimiter < 0:
        return False
    segment = masked[declaration_start:delimiter]
    lets = tuple(re.finditer(r"\blet\b", segment))
    if not lets:
        return False
    last_let = lets[-1].start()
    return segment.rfind(":=") < last_let


def split_declaration_aware(source: str) -> SplitResult | None:
    """Split the last theorem/lemma at its proof-level ``:= … by``."""

    masked = mask_lean_source(source)
    declarations = _command_declaration_starts(masked)
    if not declarations:
        return None
    for declaration_start in reversed(declarations):
        delimiters = declaration_delimiters(source, start=declaration_start)
        by_offset = next(
            (
                delimiter
                for delimiter in delimiters
                if not _delimiter_belongs_to_let(masked, declaration_start, delimiter)
            ),
            None,
        )
        if by_offset is not None:
            return _build_result(
                source,
                method=DECLARATION_AWARE_METHOD,
                by_offset=by_offset,
            )
    return None


SPLITTERS: Mapping[str, Callable[[str], SplitResult | None]] = {
    RAW_REVERSE_METHOD: split_raw_reverse,
    MASKED_REVERSE_METHOD: split_masked_reverse,
    DECLARATION_AWARE_METHOD: split_declaration_aware,
}


def split_source(source: str, method: str) -> SplitResult | None:
    """Apply a named, frozen splitter version."""

    try:
        splitter = SPLITTERS[method]
    except KeyError as exc:
        raise ValueError(f"unknown CPT2 splitter {method!r}") from exc
    return splitter(source)


def source_features(source: str, split: SplitResult | None) -> dict[str, bool | int]:
    """Cheap strata used to freeze the 500-row oracle sample."""

    masked = mask_lean_source(source)
    declaration_count = sum(1 for _ in _DECLARATION_TOKEN.finditer(masked))
    by_count = sum(1 for _ in re.finditer(r"\bby\b", masked))
    masked_syntax = any(
        original != visible and original not in "\r\n"
        for original, visible in zip(source, masked, strict=True)
    )
    return {
        "source_length": len(source),
        "multiple_declarations": declaration_count > 1,
        "nested_by": split is not None and by_count > 1,
        "comments_strings_or_chars": masked_syntax,
    }
