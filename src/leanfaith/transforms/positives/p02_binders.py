"""P02 conservative typed-binder regrouping.

The rule edits only declaration-header binder syntax and deliberately does not
implement proposition-level currying/uncurrying.  A small Lean-aware scanner
identifies balanced binder nodes while respecting nested comments, strings,
character literals, and guillemet identifiers.  It never searches/replaces a
name or type globally.

Generation remains provisional.  A candidate audit succeeds only when:

* the exact forward edit and exact inverse replay both match;
* source and candidate elaborate in the same registered context;
* their elaborated type alpha-identity fingerprints match;
* their elaborated binder-dependency graphs match; and
* their semantic-atom sequences match.

Those conditions are mechanical evidence for later family promotion; this
module never resolves an F1 label or promotes its own output.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.representations.pipeline import alpha_canonical_bytes
from leanfaith.schemas.enums import (
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
    ViewStatus,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import Applicability, TransformationAudit, VariantDraft
from leanfaith.transforms.protocol import build_transformation_audit, build_variant_draft

_DECLARATION_KEYWORDS = frozenset({"theorem", "lemma"})
_OPEN_TO_CLOSE = {"(": ")", "{": "}", "⦃": "⦄", "[": "]"}
_CLOSE_TO_OPEN = {close: open_ for open_, close in _OPEN_TO_CLOSE.items()}
_SUPPORTED_KINDS = frozenset({"explicit", "implicit", "strict_implicit"})
_VALID_ELABORATION = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
SemanticVersion = Annotated[
    str,
    Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True),
]


class P02BinderConfig(StrictModel):
    """Versioned, fail-closed execution scope for the P02 v1 rule."""

    schema_version: Literal[1] = 1
    rule_id: Literal["p02_binders"] = "p02_binders"
    rule_version: SemanticVersion
    family_id: Literal["p02_binders"] = "p02_binders"
    implementation_key: Literal["p02_binders"] = "p02_binders"
    candidate_pool: NonEmptyStr
    supported_declaration_kinds: tuple[Literal["lemma", "theorem"], ...]
    supported_binder_kinds: tuple[
        Literal["explicit", "implicit", "strict_implicit"],
        ...,
    ]
    instance_regroup_enabled: Literal[False] = False
    currying_enabled: Literal[False] = False
    require_exact_round_trip: Literal[True] = True
    require_alpha_identity: Literal[True] = True
    require_dependency_graph_identity: Literal[True] = True

    @model_validator(mode="after")
    def _closed_scope(self) -> P02BinderConfig:
        if self.supported_declaration_kinds != ("lemma", "theorem"):
            raise ValueError("supported_declaration_kinds must be exactly [lemma, theorem]")
        if self.supported_binder_kinds != (
            "explicit",
            "implicit",
            "strict_implicit",
        ):
            raise ValueError(
                "supported_binder_kinds must be exactly [explicit, implicit, strict_implicit]"
            )
        return self


class P02BinderError(ValueError):
    """P02 configuration, parsing, replay, or audit failed closed."""


def load_p02_binders_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[P02BinderConfig]:
    """Load the strict P02 config from its declared repository path."""

    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/p02_binders.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise P02BinderError("p02 config path escapes the repository")
    return load_config(resolved, P02BinderConfig)


class BinderKind(StrEnum):
    """Lean binder delimiter kind retained by every P02 edit."""

    EXPLICIT = "explicit"
    IMPLICIT = "implicit"
    STRICT_IMPLICIT = "strict_implicit"
    INSTANCE = "instance"


_KIND_FOR_OPEN = {
    "(": BinderKind.EXPLICIT,
    "{": BinderKind.IMPLICIT,
    "⦃": BinderKind.STRICT_IMPLICIT,
    "[": BinderKind.INSTANCE,
}


@dataclass(frozen=True, slots=True)
class _Token:
    start: int
    end: int
    text: str
    kind: Literal["atom", "guillemet", "string", "char", "symbol", "comment"]


@dataclass(frozen=True, slots=True)
class TypedBinder:
    """One parsed declaration-header typed binder node."""

    index: int
    kind: BinderKind
    start: int
    end: int
    opener: str
    closer: str
    names: tuple[str, ...]
    type_text: str
    type_tokens: tuple[str, ...]
    original_text: str
    has_comment: bool
    type_mentions_group_name: bool


@dataclass(frozen=True, slots=True)
class BinderEdit:
    """One exact, locally invertible P02 regrouping edit."""

    operation: Literal["split_group", "merge_singletons"]
    binder_kind: BinderKind
    start: int
    end: int
    source_text: str
    replacement_text: str
    names: tuple[str, ...]
    type_token_hash: str

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "operation": self.operation,
                "binder_kind": self.binder_kind.value,
                "start": self.start,
                "end": self.end,
                "source_text": self.source_text,
                "replacement_text": self.replacement_text,
                "names": self.names,
                "type_token_hash": self.type_token_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class BinderDependency:
    """One elaborated forall binder and its edges to preceding binders."""

    index: int
    binder_info: str
    domain_hash: str
    depends_on: tuple[int, ...]


class BinderParseError(ValueError):
    """The declaration header cannot be safely interpreted by P02."""


def _lex_lean(source: str) -> tuple[_Token, ...]:
    """Tokenize only the lexical distinctions P02 needs.

    This is not a substitute Lean parser.  Its intentionally small contract is
    to find balanced top-level declaration binders without mistaking syntax in
    comments, strings, character literals, or guillemet identifiers for
    delimiters.  Lean elaboration remains the authority in the audit.
    """

    tokens: list[_Token] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char.isspace():
            index += 1
            continue

        if source.startswith("--", index):
            end = source.find("\n", index + 2)
            end = length if end < 0 else end
            tokens.append(_Token(index, end, source[index:end], "comment"))
            index = end
            continue

        if source.startswith("/-", index):
            start = index
            depth = 1
            index += 2
            while index < length and depth:
                if source.startswith("/-", index):
                    depth += 1
                    index += 2
                elif source.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise BinderParseError("unterminated_block_comment")
            tokens.append(_Token(start, index, source[start:index], "comment"))
            continue

        if char == '"':
            start = index
            index += 1
            escaped = False
            while index < length:
                current = source[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
            else:
                raise BinderParseError("unterminated_string_literal")
            tokens.append(_Token(start, index, source[start:index], "string"))
            continue

        if char == "'" and index + 1 < length:
            # Lean identifiers may end in apostrophes, but do not start with
            # one.  A leading apostrophe is therefore a character literal.
            start = index
            index += 1
            escaped = False
            while index < length:
                current = source[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == "'":
                    break
            else:
                raise BinderParseError("unterminated_character_literal")
            tokens.append(_Token(start, index, source[start:index], "char"))
            continue

        if char == "«":
            start = index
            end = source.find("»", index + 1)
            if end < 0:
                raise BinderParseError("unterminated_guillemet_identifier")
            index = end + 1
            tokens.append(_Token(start, index, source[start:index], "guillemet"))
            continue

        if char in _OPEN_TO_CLOSE or char in _CLOSE_TO_OPEN:
            tokens.append(_Token(index, index + 1, char, "symbol"))
            index += 1
            continue

        matched_operator = next(
            (
                operator
                for operator in (":=", "::", "=>", "->", "<-", "⟨", "⟩")
                if source.startswith(operator, index)
            ),
            None,
        )
        if matched_operator is not None:
            end = index + len(matched_operator)
            tokens.append(_Token(index, end, matched_operator, "symbol"))
            index = end
            continue

        if char in ":,;@":
            tokens.append(_Token(index, index + 1, char, "symbol"))
            index += 1
            continue

        start = index
        while index < length:
            current = source[index]
            if (
                current.isspace()
                or current in '(){}[]⦃⦄":,;@«'
                or source.startswith("--", index)
                or source.startswith("/-", index)
                or any(source.startswith(op, index) for op in (":=", "::", "=>", "->", "<-"))
            ):
                break
            index += 1
        if index == start:
            # A punctuation character irrelevant to binder parsing is a
            # one-character symbol, preserving it in structural type keys.
            index += 1
            tokens.append(_Token(start, index, source[start:index], "symbol"))
        else:
            tokens.append(_Token(start, index, source[start:index], "atom"))
    return tuple(tokens)


def _matching_token(tokens: Sequence[_Token], open_index: int) -> int:
    stack: list[str] = []
    for index in range(open_index, len(tokens)):
        text = tokens[index].text
        if text in _OPEN_TO_CLOSE:
            stack.append(text)
        elif text in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[text]:
                raise BinderParseError("mismatched_delimiter")
            stack.pop()
            if not stack:
                return index
    raise BinderParseError("unterminated_delimiter")


def _direct_colon_index(tokens: Sequence[_Token], start: int, end: int) -> int | None:
    stack: list[str] = []
    colon: int | None = None
    for index in range(start, end):
        text = tokens[index].text
        if text in _OPEN_TO_CLOSE:
            stack.append(text)
        elif text in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[text]:
                raise BinderParseError("mismatched_binder_delimiter")
            stack.pop()
        elif not stack and text == ":":
            if colon is not None:
                return None
            colon = index
        elif not stack and text == ":=":
            return None
    return colon


def _parse_binder(
    source: str,
    all_tokens: Sequence[_Token],
    code_tokens: Sequence[_Token],
    *,
    binder_index: int,
    open_index: int,
    close_index: int,
) -> TypedBinder | None:
    open_token = code_tokens[open_index]
    close_token = code_tokens[close_index]
    colon_index = _direct_colon_index(code_tokens, open_index + 1, close_index)
    if colon_index is None:
        return None
    name_tokens = tuple(code_tokens[open_index + 1 : colon_index])
    type_tokens = tuple(code_tokens[colon_index + 1 : close_index])
    if not name_tokens or not type_tokens:
        return None
    if any(token.kind not in {"atom", "guillemet"} for token in name_tokens):
        return None
    names = tuple(token.text for token in name_tokens)
    if any(name == "_" for name in names) or len(set(names)) != len(names):
        return None
    if any(token.text == ":=" for token in type_tokens):
        return None

    type_start = code_tokens[colon_index].end
    type_end = close_token.start
    type_text = source[type_start:type_end].strip()
    if not type_text:
        return None
    has_comment = any(
        token.kind == "comment" and open_token.start < token.start < close_token.end
        for token in all_tokens
    )
    type_key = tuple(token.text for token in type_tokens)
    return TypedBinder(
        index=binder_index,
        kind=_KIND_FOR_OPEN[open_token.text],
        start=open_token.start,
        end=close_token.end,
        opener=open_token.text,
        closer=close_token.text,
        names=names,
        type_text=type_text,
        type_tokens=type_key,
        original_text=source[open_token.start : close_token.end],
        has_comment=has_comment,
        type_mentions_group_name=bool(set(names) & set(type_key)),
    )


def parse_typed_binders(source: str) -> tuple[TypedBinder, ...]:
    """Parse typed binder nodes in one theorem/lemma declaration header."""

    all_tokens = _lex_lean(source)
    code_tokens = tuple(token for token in all_tokens if token.kind != "comment")
    keyword_index: int | None = None
    stack: list[str] = []
    for index, token in enumerate(code_tokens):
        if token.text in _OPEN_TO_CLOSE:
            stack.append(token.text)
        elif token.text in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[token.text]:
                raise BinderParseError("mismatched_declaration_delimiter")
            stack.pop()
        elif not stack and token.text in _DECLARATION_KEYWORDS:
            keyword_index = index
            break
    if keyword_index is None:
        raise BinderParseError("unsupported_declaration_kind")
    if keyword_index + 1 >= len(code_tokens):
        raise BinderParseError("missing_declaration_name")

    binders: list[TypedBinder] = []
    index = keyword_index + 2
    while index < len(code_tokens):
        token = code_tokens[index]
        if token.text == ":":
            break
        if token.text in _OPEN_TO_CLOSE:
            close_index = _matching_token(code_tokens, index)
            parsed = _parse_binder(
                source,
                all_tokens,
                code_tokens,
                binder_index=len(binders),
                open_index=index,
                close_index=close_index,
            )
            if parsed is not None:
                binders.append(parsed)
            index = close_index + 1
            continue
        index += 1
    return tuple(binders)


def _type_token_hash(tokens: Sequence[str]) -> str:
    return hash_canonical({"lean_type_tokens": tuple(tokens)})


def _split_edit(binder: TypedBinder) -> BinderEdit:
    replacement = " ".join(
        f"{binder.opener}{name} : {binder.type_text}{binder.closer}" for name in binder.names
    )
    return BinderEdit(
        operation="split_group",
        binder_kind=binder.kind,
        start=binder.start,
        end=binder.end,
        source_text=binder.original_text,
        replacement_text=replacement,
        names=binder.names,
        type_token_hash=_type_token_hash(binder.type_tokens),
    )


def _merge_edit(source: str, left: TypedBinder, right: TypedBinder) -> BinderEdit:
    names = (*left.names, *right.names)
    replacement = f"{left.opener}{' '.join(names)} : {left.type_text}{left.closer}"
    return BinderEdit(
        operation="merge_singletons",
        binder_kind=left.kind,
        start=left.start,
        end=right.end,
        source_text=source[left.start : right.end],
        replacement_text=replacement,
        names=names,
        type_token_hash=_type_token_hash(left.type_tokens),
    )


def enumerate_binder_edits(source: str) -> tuple[BinderEdit, ...]:
    """Enumerate conservative, non-overlapping *single-edit* P02 choices."""

    binders = parse_typed_binders(source)
    edits: list[BinderEdit] = []
    for binder in binders:
        if (
            binder.kind.value in _SUPPORTED_KINDS
            and len(binder.names) >= 2
            and not binder.has_comment
            and not binder.type_mentions_group_name
        ):
            edits.append(_split_edit(binder))

    for left, right in pairwise(binders):
        between = source[left.end : right.start]
        names = set(left.names) | set(right.names)
        if (
            left.kind == right.kind
            and left.kind.value in _SUPPORTED_KINDS
            and len(left.names) == len(right.names) == 1
            and left.type_tokens == right.type_tokens
            and not left.has_comment
            and not right.has_comment
            and not between.strip()
            and not (names & set(left.type_tokens))
        ):
            edits.append(_merge_edit(source, left, right))
    return tuple(sorted(edits, key=lambda edit: (edit.start, edit.end, edit.operation)))


def _analysis_reasons(source: str, binders: Sequence[TypedBinder]) -> tuple[str, ...]:
    reasons: set[str] = set()
    if any(binder.kind == BinderKind.INSTANCE for binder in binders):
        reasons.add("instance_binder_regroup_unsupported")
    if any(binder.has_comment for binder in binders):
        reasons.add("comment_inside_binder")
    if any(binder.type_mentions_group_name for binder in binders):
        reasons.add("unsafe_group_name_dependency")
    if not enumerate_binder_edits(source):
        reasons.add("no_eligible_typed_binder_regroup")
    return tuple(sorted(reasons))


def apply_exact_span_trace(
    source: str,
    trace: Sequence[Mapping[str, object]],
) -> str:
    """Apply a sequence of exact span replacements, failing closed on drift."""

    result = source
    for step in trace:
        if step.get("operation") != "replace_exact_span":
            raise ValueError("unsupported_trace_operation")
        start = step.get("start")
        end = step.get("end")
        expected = step.get("expected_text")
        replacement = step.get("replacement_text")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not isinstance(expected, str)
            or not isinstance(replacement, str)
        ):
            raise ValueError("malformed_exact_span_trace")
        if start < 0 or end < start or end > len(result):
            raise ValueError("trace_span_out_of_bounds")
        if result[start:end] != expected:
            raise ValueError("trace_expected_text_mismatch")
        result = result[:start] + replacement + result[end:]
    return result


def _outer_dependencies(
    node: object,
    *,
    preceding_binders: int,
    local_depth: int = 0,
) -> set[int]:
    if not isinstance(node, dict):
        return set()
    kind = node.get("k")
    if kind == "bvar":
        raw_index = node.get("i")
        if isinstance(raw_index, int) and raw_index >= local_depth:
            outer_offset = raw_index - local_depth
            target = preceding_binders - 1 - outer_offset
            return {target} if target >= 0 else set()
        return set()

    dependencies: set[int] = set()
    if kind in {"forall", "lam"}:
        dependencies.update(
            _outer_dependencies(
                node.get("dom"),
                preceding_binders=preceding_binders,
                local_depth=local_depth,
            )
        )
        dependencies.update(
            _outer_dependencies(
                node.get("body"),
                preceding_binders=preceding_binders,
                local_depth=local_depth + 1,
            )
        )
        return dependencies
    if kind == "let":
        for key in ("t", "v"):
            dependencies.update(
                _outer_dependencies(
                    node.get(key),
                    preceding_binders=preceding_binders,
                    local_depth=local_depth,
                )
            )
        dependencies.update(
            _outer_dependencies(
                node.get("body"),
                preceding_binders=preceding_binders,
                local_depth=local_depth + 1,
            )
        )
        return dependencies

    for key in ("dom", "body", "fn", "arg", "base", "t", "v"):
        child = node.get(key)
        if isinstance(child, dict):
            dependencies.update(
                _outer_dependencies(
                    child,
                    preceding_binders=preceding_binders,
                    local_depth=local_depth,
                )
            )
    return dependencies


def binder_dependency_graph(operator_tree: Mapping[str, object]) -> tuple[BinderDependency, ...]:
    """Derive the outer forall dependency graph from an elaborated Expr tree."""

    root = operator_tree.get("root")
    if not isinstance(root, dict):
        raise ValueError("operator_tree_missing_root")
    graph: list[BinderDependency] = []
    node: object = root
    while isinstance(node, dict) and node.get("k") == "forall":
        domain = node.get("dom")
        if not isinstance(domain, dict):
            raise ValueError("forall_missing_domain")
        index = len(graph)
        dependencies = _outer_dependencies(domain, preceding_binders=index)
        graph.append(
            BinderDependency(
                index=index,
                binder_info=str(node.get("bi", "")),
                domain_hash=hashlib.sha256(alpha_canonical_bytes(domain)).hexdigest(),
                depends_on=tuple(sorted(dependencies)),
            )
        )
        node = node.get("body")
    return tuple(graph)


def _trace_for(
    edit: BinderEdit,
    *,
    rule_config_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "replace_exact_span",
            "p02_operation": edit.operation,
            "binder_kind": edit.binder_kind.value,
            "start": edit.start,
            "end": edit.end,
            "expected_text": edit.source_text,
            "replacement_text": edit.replacement_text,
            "rule_config_hash": rule_config_hash,
            "type_token_hash": edit.type_token_hash,
        },
    )


def _inverse_trace_for(
    edit: BinderEdit,
    *,
    rule_config_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "replace_exact_span",
            "p02_operation": (
                "merge_singletons" if edit.operation == "split_group" else "split_group"
            ),
            "binder_kind": edit.binder_kind.value,
            "start": edit.start,
            "end": edit.start + len(edit.replacement_text),
            "expected_text": edit.replacement_text,
            "replacement_text": edit.source_text,
            "rule_config_hash": rule_config_hash,
            "type_token_hash": edit.type_token_hash,
        },
    )


def _expected_structural_diff(
    edit: BinderEdit,
    *,
    rule_config_hash: str,
) -> dict[str, JsonValue]:
    return {
        "operation": edit.operation,
        "binder_kind": edit.binder_kind.value,
        "source_span_start": edit.start,
        "source_span_end": edit.end,
        "source_name_count": len(edit.names),
        "rule_config_hash": rule_config_hash,
        "type_token_hash": edit.type_token_hash,
        "dependency_policy": "elaborated_graph_identity",
        "currying_applied": False,
    }


def _choose_edit(
    edits: Sequence[BinderEdit],
    *,
    theorem_id: str,
    seed: int,
) -> BinderEdit:
    if not edits:
        raise ValueError("no_eligible_typed_binder_regroup")

    def rank(edit: BinderEdit) -> bytes:
        payload = f"p02_binders_v1\0{theorem_id}\0{seed}\0{edit.stable_key}"
        return hashlib.sha256(payload.encode("utf-8")).digest()

    return min(edits, key=rank)


class P02BinderRule:
    """Registered P02 regroup-only rule; outputs are always provisional."""

    rule_id = "p02_binders"
    family_id = "p02_binders"
    polarity = Polarity.POSITIVE
    implementation_key = "p02_binders"

    def __init__(
        self,
        *,
        registry_hash: str,
        config: P02BinderConfig | None = None,
        rule_config_hash: str | None = None,
    ) -> None:
        if len(registry_hash) != 64:
            raise ValueError("registry_hash must be a SHA-256 hex digest")
        int(registry_hash, 16)
        if (config is None) != (rule_config_hash is None):
            raise P02BinderError("config and rule_config_hash must be supplied together")
        if config is None:
            loaded = load_p02_binders_config()
            config = loaded.config
            rule_config_hash = loaded.config_hash
        assert rule_config_hash is not None
        if len(rule_config_hash) != 64:
            raise P02BinderError("rule_config_hash must be a SHA-256 hex digest")
        int(rule_config_hash, 16)
        self.registry_hash = registry_hash
        self.config = config
        self.rule_config_hash = rule_config_hash
        self.rule_version = config.rule_version
        self.audit_config_hash = hash_canonical(
            {
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "registry_hash": registry_hash,
                "rule_config_hash": rule_config_hash,
                "scope": "typed_binder_regroup_only",
                "currying": "disabled",
                "certificate": "alpha_dependency_graph_exact_roundtrip_v1",
            }
        )

    @classmethod
    def from_repository(
        cls,
        *,
        registry_hash: str,
        repo_root: Path | None = None,
    ) -> P02BinderRule:
        loaded = load_p02_binders_config(repo_root)
        return cls(
            registry_hash=registry_hash,
            config=loaded.config,
            rule_config_hash=loaded.config_hash,
        )

    def assess(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> Applicability:
        if not theorem.is_proposition:
            return Applicability(
                applicable=False,
                reason_codes=("source_not_proposition",),
            )
        if theorem.elaboration_status not in _VALID_ELABORATION:
            return Applicability(
                applicable=False,
                reason_codes=("source_does_not_elaborate",),
            )
        if representation.raw_proof_stripped != theorem.proof_stripped_declaration:
            return Applicability(
                applicable=False,
                reason_codes=("source_representation_text_mismatch",),
            )
        missing: list[str] = []
        if representation.alpha_identity_fingerprint is None:
            missing.append("alpha_identity_fingerprint")
        if representation.operator_tree is None:
            missing.append("operator_tree")
        if missing:
            return Applicability(
                applicable=False,
                reason_codes=tuple(f"missing_{name}" for name in sorted(missing)),
                required_capabilities=(
                    "alpha_identity",
                    "binder_dependency_graph",
                    "lean_reelaboration",
                    "round_trip",
                ),
            )
        try:
            binders = parse_typed_binders(theorem.proof_stripped_declaration)
            edits = enumerate_binder_edits(theorem.proof_stripped_declaration)
        except BinderParseError as exc:
            return Applicability(
                applicable=False,
                reason_codes=(str(exc),),
                required_capabilities=(
                    "alpha_identity",
                    "binder_dependency_graph",
                    "lean_reelaboration",
                    "round_trip",
                ),
            )
        if not edits:
            return Applicability(
                applicable=False,
                reason_codes=_analysis_reasons(theorem.proof_stripped_declaration, binders),
                matched_nodes=tuple(sorted(f"binder:{binder.index}" for binder in binders)),
                required_capabilities=(
                    "alpha_identity",
                    "binder_dependency_graph",
                    "lean_reelaboration",
                    "round_trip",
                ),
                metadata={"typed_binder_count": len(binders)},
            )
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=tuple(
                sorted(f"span:{edit.start}:{edit.end}:{edit.operation}" for edit in edits)
            ),
            required_capabilities=(
                "alpha_identity",
                "binder_dependency_graph",
                "lean_reelaboration",
                "round_trip",
            ),
            metadata={
                "eligible_edit_count": len(edits),
                "typed_binder_count": len(binders),
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
            return ()
        edits = enumerate_binder_edits(theorem.proof_stripped_declaration)
        edit = _choose_edit(edits, theorem_id=theorem.theorem_id, seed=seed)
        trace = _trace_for(edit, rule_config_hash=self.rule_config_hash)
        inverse_trace = _inverse_trace_for(
            edit,
            rule_config_hash=self.rule_config_hash,
        )
        candidate = apply_exact_span_trace(theorem.proof_stripped_declaration, trace)
        if apply_exact_span_trace(candidate, inverse_trace) != theorem.proof_stripped_declaration:
            raise ValueError("p02_internal_round_trip_failure")
        return (
            build_variant_draft(
                source_theorem_ids=(theorem.theorem_id,),
                source_representation_ids=(representation.representation_id,),
                context_id=theorem.context_id,
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                family_id=self.family_id,
                seed=seed,
                candidate_code=candidate,
                intended_relation=IntendedRelation.EQUIVALENT,
                candidate_pool=self.config.candidate_pool,
                transformation_trace=trace,
                inverse_trace=inverse_trace,
                expected_atom_mapping={},
                expected_structural_diff=_expected_structural_diff(
                    edit,
                    rule_config_hash=self.rule_config_hash,
                ),
                generation_config_hash=self.registry_hash,
                metadata={
                    "eligible_edit_count": len(edits),
                    "p02_operation": edit.operation,
                },
            ),
        )

    def audit(
        self,
        source: TheoremRecord,
        source_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit:
        violations: list[str] = []
        rule_lineage_ok = (
            draft.family_id == self.family_id
            and draft.rule_id == self.rule_id
            and draft.rule_version == self.rule_version
            and draft.generation_config_hash == self.registry_hash
            and draft.source_theorem_ids == (source.theorem_id,)
            and draft.source_representation_ids == (source_representation.representation_id,)
        )
        if not rule_lineage_ok:
            violations.append("draft_lineage_mismatch")
        representation_lineage_ok = (
            source_representation.theorem_id == source.theorem_id
            and candidate_representation.theorem_id == candidate.theorem_id
        )
        if not representation_lineage_ok:
            violations.append("representation_lineage_mismatch")
        context_ok = (
            source.context_id
            == source_representation.context_id
            == candidate.context_id
            == candidate_representation.context_id
            == draft.context_id
        )
        if not context_ok:
            violations.append("context_mismatch")
        if candidate.proof_stripped_declaration != draft.candidate_code:
            violations.append("candidate_code_mismatch")
        source_representation_text_ok = (
            source_representation.raw_proof_stripped == source.proof_stripped_declaration
        )
        if not source_representation_text_ok:
            violations.append("source_representation_text_mismatch")
        candidate_representation_text_ok = (
            candidate_representation.raw_proof_stripped == candidate.proof_stripped_declaration
        )
        if not candidate_representation_text_ok:
            violations.append("candidate_representation_text_mismatch")

        matching_edit: BinderEdit | None = None
        try:
            matching_edits = tuple(
                edit
                for edit in enumerate_binder_edits(source.proof_stripped_declaration)
                if _trace_for(
                    edit,
                    rule_config_hash=self.rule_config_hash,
                )
                == draft.transformation_trace
                and _inverse_trace_for(
                    edit,
                    rule_config_hash=self.rule_config_hash,
                )
                == draft.inverse_trace
            )
            if len(matching_edits) == 1:
                matching_edit = matching_edits[0]
        except BinderParseError:
            matching_edit = None
        try:
            forward_ok = (
                apply_exact_span_trace(
                    source.proof_stripped_declaration,
                    draft.transformation_trace,
                )
                == draft.candidate_code
            )
        except ValueError:
            forward_ok = False
        if not forward_ok:
            violations.append("forward_trace_failed")
        try:
            roundtrip_ok = (
                draft.inverse_trace is not None
                and apply_exact_span_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except ValueError:
            roundtrip_ok = False
        if not roundtrip_ok:
            violations.append("inverse_roundtrip_failed")

        source_elaborates = source.elaboration_status in _VALID_ELABORATION
        if not source_elaborates:
            violations.append("source_does_not_elaborate")
        candidate_elaborates = candidate.elaboration_status in _VALID_ELABORATION
        if not candidate_elaborates:
            violations.append("candidate_does_not_elaborate")
        required_source_views = (
            source_representation.view_status["signature_explicit"] == ViewStatus.OK
            and source_representation.view_status["semantic_atoms"] == ViewStatus.OK
            and source_representation.view_status["operator_tree"] == ViewStatus.OK
        )
        if not required_source_views:
            violations.append("source_required_view_failed")
        required_candidate_views = (
            candidate_representation.view_status["signature_explicit"] == ViewStatus.OK
            and candidate_representation.view_status["semantic_atoms"] == ViewStatus.OK
            and candidate_representation.view_status["operator_tree"] == ViewStatus.OK
        )
        if not required_candidate_views:
            violations.append("candidate_required_view_failed")

        alpha_equal = (
            source_representation.alpha_identity_fingerprint is not None
            and source_representation.alpha_identity_fingerprint
            == candidate_representation.alpha_identity_fingerprint
        )
        if not alpha_equal:
            violations.append("alpha_identity_mismatch")

        graph_equal = False
        try:
            if (
                source_representation.operator_tree is not None
                and candidate_representation.operator_tree is not None
            ):
                graph_equal = binder_dependency_graph(
                    source_representation.operator_tree
                ) == binder_dependency_graph(candidate_representation.operator_tree)
        except ValueError:
            graph_equal = False
        if not graph_equal:
            violations.append("binder_dependency_graph_mismatch")

        atoms_equal = (
            source_representation.semantic_atoms is not None
            and source_representation.semantic_atoms == candidate_representation.semantic_atoms
        )
        atom_mapping_ok = atoms_equal and not draft.expected_atom_mapping
        if not atom_mapping_ok:
            violations.append("semantic_atoms_mismatch")

        expected_diff_ok = (
            matching_edit is not None
            and draft.expected_structural_diff
            == _expected_structural_diff(
                matching_edit,
                rule_config_hash=self.rule_config_hash,
            )
        )
        structural_diff_ok = (
            forward_ok
            and roundtrip_ok
            and expected_diff_ok
            and draft.candidate_code != source.proof_stripped_declaration
        )
        if not structural_diff_ok:
            violations.append("structural_diff_mismatch")

        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("p02_exact_regroup",),
                required_capabilities=(
                    "alpha_identity",
                    "binder_dependency_graph",
                    "lean_reelaboration",
                    "round_trip",
                ),
            ),
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=(
                candidate.elaboration_status if not violations else ValidationStatus.QUARANTINED
            ),
            recommended_quality_tier=(
                QualityTier.PROVISIONAL if not violations else QualityTier.UNKNOWN
            ),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=structural_diff_ok,
            atom_mapping_ok=atom_mapping_ok,
            inverse_or_roundtrip_ok=roundtrip_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "alpha_identity_equal": alpha_equal,
                "binder_dependency_graph_equal": graph_equal,
                "certificate_kind": "p02_alpha_dependency_roundtrip_v1",
                "context_equal": context_ok,
                "currying_applied": False,
                "source_candidate_elaborated": (source_elaborates and candidate_elaborates),
            },
        )
