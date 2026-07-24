"""LF-018 N07: finite one-site numeric-bound mutation candidates.

N07 v1 is intentionally much narrower than the family name might suggest.  It
changes exactly one ASCII natural-number literal, ``0`` or ``1``, when that
literal is a direct operand of one of the configured comparison tokens.  The
finite table is exact and invertible; arbitrary arithmetic edits, projection
index changes, and argument swapping are not implemented.

An N07 output is only a near-miss *candidate*.  Same-context Lean elaboration,
an exact atom delta, an exact structural delta, and the inverse trace are
mechanical validity checks.  They never resolve the candidate as a semantic
negative, and failed proof search is not consumed.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import Field, JsonValue, model_validator

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import (
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
    ViewStatus,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import Applicability, TransformationAudit, VariantDraft
from leanfaith.transforms.protocol import (
    build_transformation_audit,
    build_variant_draft,
    verify_variant_draft_id,
)

NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
SemanticVersion = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True)]
ErrorCode = Annotated[str, Field(pattern=r"^E(0[1-9]|[12][0-9]|30)$", strict=True)]
LiteralToken = Literal["0", "1"]
ComparisonToken = Literal["<", ">", "≤", "≥"]
OperandSide = Literal["left", "right"]

_VALID_ELABORATION = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
_LOGICAL_OR = "\u2228"


class N07LiteralBoundError(ValueError):
    """An N07 config, source, trace, or audit invariant failed closed."""


class _UnsupportedSource(N07LiteralBoundError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class N07LiteralMutation(StrictModel):
    """One direction in the exact finite ``0``/``1`` mutation table."""

    mutation_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
    source_literal: LiteralToken
    target_literal: LiteralToken
    source_atom: NonEmptyStr
    target_atom: NonEmptyStr
    intended_error_types: tuple[ErrorCode, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed_entry(self) -> N07LiteralMutation:
        if self.source_literal == self.target_literal:
            raise ValueError("source_literal and target_literal must differ")
        if self.source_atom != f"lit:nat:{self.source_literal}":
            raise ValueError("source_atom must exactly encode source_literal")
        if self.target_atom != f"lit:nat:{self.target_literal}":
            raise ValueError("target_atom must exactly encode target_literal")
        if self.intended_error_types != tuple(sorted(set(self.intended_error_types))):
            raise ValueError("intended_error_types must be sorted and unique")
        return self


class N07LiteralBoundConfig(StrictModel):
    """Versioned, fail-closed N07 v1 policy."""

    schema_version: Literal[1] = 1
    rule_id: Literal["n07_literal_bound"] = "n07_literal_bound"
    rule_version: SemanticVersion
    family_id: Literal["n07_literal_bound"] = "n07_literal_bound"
    implementation_key: Literal["n07_literal_bound"] = "n07_literal_bound"
    candidate_pool: Literal["typed_negative_candidate"]
    supported_declaration_kinds: tuple[Literal["lemma", "theorem"], ...]
    placeholder_forms: tuple[Literal["by_sorry", "sorry"], ...]
    comparison_operators: tuple[ComparisonToken, ...]
    require_direct_comparison_operand: Literal[True] = True
    require_source_candidate_elaboration: Literal[True] = True
    failed_proof_search_is_negative_evidence: Literal[False] = False
    mutations: tuple[N07LiteralMutation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed_table(self) -> N07LiteralBoundConfig:
        if self.supported_declaration_kinds != ("lemma", "theorem"):
            raise ValueError("supported_declaration_kinds must be exactly [lemma, theorem]")
        if self.placeholder_forms != ("by_sorry", "sorry"):
            raise ValueError("placeholder_forms must be exactly [by_sorry, sorry]")
        if self.comparison_operators != tuple(sorted(set(self.comparison_operators))):
            raise ValueError("comparison_operators must be sorted and unique")
        if self.comparison_operators != ("<", ">", "≤", "≥"):
            raise ValueError("N07 v1 comparison operators must be exactly <, >, ≤, ≥")
        mutation_ids = tuple(item.mutation_id for item in self.mutations)
        if mutation_ids != tuple(sorted(set(mutation_ids))):
            raise ValueError("mutations must be sorted by unique mutation_id")
        by_source = {item.source_literal: item for item in self.mutations}
        if len(by_source) != len(self.mutations):
            raise ValueError("each source literal may occur only once")
        if set(by_source) != {"0", "1"}:
            raise ValueError("N07 v1 must define exactly the source literals 0 and 1")
        expected_ids = {"0": "zero_to_one", "1": "one_to_zero"}
        for item in self.mutations:
            if item.mutation_id != expected_ids[item.source_literal]:
                raise ValueError("N07 v1 mutation IDs are code-owned and literal-specific")
            if item.intended_error_types != ("E17",):
                raise ValueError("N07 v1 mutations must use exactly E17")
            inverse = by_source.get(item.target_literal)
            if (
                inverse is None
                or inverse.target_literal != item.source_literal
                or inverse.source_atom != item.target_atom
                or inverse.target_atom != item.source_atom
            ):
                raise ValueError("the N07 literal table must be exactly invertible")
        return self


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class LiteralBoundSite:
    """One exact numeric literal directly adjacent to a comparison token."""

    mutation_id: str
    start: int
    end: int
    token_index: int
    source_literal: LiteralToken
    target_literal: LiteralToken
    source_atom: str
    target_atom: str
    comparison_operator: ComparisonToken
    operand_side: OperandSide
    intended_error_types: tuple[str, ...]

    @property
    def stable_key(self) -> str:
        return (
            f"{self.start:010d}:{self.end:010d}:{self.token_index:06d}:"
            f"{self.mutation_id}:{self.comparison_operator}:{self.operand_side}"
        )


def load_n07_literal_bound_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[N07LiteralBoundConfig]:
    """Load the strict N07 config from its canonical repository path."""

    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/n07_literal_bound.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise N07LiteralBoundError("n07 config path escapes the repository")
    return load_config(resolved, N07LiteralBoundConfig)


def literal_bound_table_hash(config: N07LiteralBoundConfig) -> str:
    """Hash the finite literal table and its exact comparison-site scope."""

    return hash_canonical(
        {
            "schema_version": config.schema_version,
            "comparison_operators": config.comparison_operators,
            "require_direct_comparison_operand": config.require_direct_comparison_operand,
            "mutations": [item.model_dump(mode="json") for item in config.mutations],
        }
    )


def _quoted_end(source: str, start: int, delimiter: str) -> int:
    index = start + 1
    escaped = False
    while index < len(source):
        character = source[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == delimiter:
            return index + 1
        index += 1
    raise _UnsupportedSource("unterminated_quoted_token")


def _tokenize(source: str) -> tuple[_Token, ...]:
    """Tokenize the distinctions required to isolate exact numeric sites."""

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
                raise _UnsupportedSource("unterminated_block_comment")
            tokens.append(_Token("trivia", source[start:index], start, index))
            continue
        if character == '"':
            index = _quoted_end(source, index, '"')
            tokens.append(_Token("string", source[start:index], start, index))
            continue
        if character == "'":
            index = _quoted_end(source, index, "'")
            tokens.append(_Token("character", source[start:index], start, index))
            continue
        if character == "«":
            close = source.find("»", index + 1)
            if close < 0:
                raise _UnsupportedSource("unterminated_guillemet_identifier")
            index = close + 1
            tokens.append(_Token("quoted_identifier", source[start:index], start, index))
            continue
        if character.isalpha() or character == "_":
            index += 1
            while index < len(source) and (source[index].isalnum() or source[index] in {"_", "'"}):
                index += 1
            tokens.append(_Token("identifier", source[start:index], start, index))
            continue
        if character.isdigit():
            index += 1
            while index < len(source) and (source[index].isalnum() or source[index] == "_"):
                index += 1
            kind = "number" if source[start:index].isdigit() else "unsupported_number"
            tokens.append(_Token(kind, source[start:index], start, index))
            continue
        for operator in (":=", "=>", "->", "<=", ">=", "≤", "≥"):
            if source.startswith(operator, index):
                index += len(operator)
                tokens.append(_Token("symbol", source[start:index], start, index))
                break
        else:
            index += 1
            tokens.append(_Token("symbol", source[start:index], start, index))
    return tuple(tokens)


def _significant(tokens: Sequence[_Token]) -> tuple[tuple[int, _Token], ...]:
    return tuple((index, token) for index, token in enumerate(tokens) if token.kind != "trivia")


def _signature_bounds(
    tokens: Sequence[_Token],
    config: N07LiteralBoundConfig,
) -> tuple[int, int]:
    significant = _significant(tokens)
    declaration_position = next(
        (
            position
            for position, (_index, token) in enumerate(significant)
            if token.kind == "identifier" and token.text in config.supported_declaration_kinds
        ),
        None,
    )
    if declaration_position is None or declaration_position + 1 >= len(significant):
        raise _UnsupportedSource("unsupported_declaration_kind")
    if significant[declaration_position + 1][1].kind not in {
        "identifier",
        "quoted_identifier",
    }:
        raise _UnsupportedSource("unsupported_declaration_name")
    assignment_positions: list[int] = []
    for position in range(declaration_position + 2, len(significant)):
        if significant[position][1].text != ":=":
            continue
        tail = tuple(token.text for _, token in significant[position + 1 :])
        valid = (tail == ("by", "sorry") and "by_sorry" in config.placeholder_forms) or (
            tail == ("sorry",) and "sorry" in config.placeholder_forms
        )
        if valid:
            assignment_positions.append(position)
    if len(assignment_positions) != 1:
        raise _UnsupportedSource("unsupported_proof_placeholder")
    return declaration_position + 2, assignment_positions[0]


def _lexically_standalone_number(source: str, token: _Token) -> bool:
    """Reject decimal/scientific/base-prefixed and member-access fragments."""

    before = source[token.start - 1] if token.start else ""
    after = source[token.end] if token.end < len(source) else ""
    blocked = {".", "_"}
    return not (before in blocked or after in blocked or before.isalnum() or after.isalnum())


def enumerate_literal_bound_sites(
    source: str,
    config: N07LiteralBoundConfig,
) -> tuple[LiteralBoundSite, ...]:
    """Enumerate only configured literals that directly border a comparison."""

    tokens = _tokenize(source)
    significant = _significant(tokens)
    start, end = _signature_bounds(tokens, config)
    mutations = {item.source_literal: item for item in config.mutations}
    comparison_tokens = set(config.comparison_operators)
    left_operand_boundaries = {":", "(", "[", "{", ",", "∧", _LOGICAL_OR, "→", "->"}
    right_operand_boundaries = {")", "]", "}", ",", "∧", _LOGICAL_OR, "→", "->"}
    sites: list[LiteralBoundSite] = []
    for position in range(start, end):
        token_index, token = significant[position]
        if token.kind != "number" or token.text not in mutations:
            continue
        if not _lexically_standalone_number(source, token):
            continue
        neighbors: list[tuple[ComparisonToken, OperandSide]] = []
        if (
            position > start
            and significant[position - 1][1].text in comparison_tokens
            and (
                position + 1 == end or significant[position + 1][1].text in right_operand_boundaries
            )
        ):
            neighbors.append(
                (
                    cast(ComparisonToken, significant[position - 1][1].text),
                    "right",
                )
            )
        if (
            position + 1 < end
            and significant[position + 1][1].text in comparison_tokens
            and position > start
            and significant[position - 1][1].text in left_operand_boundaries
        ):
            neighbors.append(
                (
                    cast(ComparisonToken, significant[position + 1][1].text),
                    "left",
                )
            )
        if len(neighbors) != 1:
            continue
        mutation = mutations[token.text]
        comparison_operator, operand_side = neighbors[0]
        sites.append(
            LiteralBoundSite(
                mutation_id=mutation.mutation_id,
                start=token.start,
                end=token.end,
                token_index=token_index,
                source_literal=mutation.source_literal,
                target_literal=mutation.target_literal,
                source_atom=mutation.source_atom,
                target_atom=mutation.target_atom,
                comparison_operator=comparison_operator,
                operand_side=operand_side,
                intended_error_types=mutation.intended_error_types,
            )
        )
    return tuple(sorted(sites, key=lambda site: site.stable_key))


def _choose_site(
    sites: Sequence[LiteralBoundSite],
    *,
    theorem_id: str,
    seed: int,
) -> LiteralBoundSite:
    if not sites:
        raise N07LiteralBoundError("no_supported_literal_bound_site")

    def rank(site: LiteralBoundSite) -> bytes:
        payload = f"n07-select-v1\0{theorem_id}\0{seed}\0{site.stable_key}"
        return hashlib.sha256(payload.encode("utf-8")).digest()

    return min(sites, key=rank)


def _trace(
    source: str,
    site: LiteralBoundSite,
    *,
    table_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "replace_numeric_bound_literal_exact",
            "mutation_id": site.mutation_id,
            "start": site.start,
            "end": site.end,
            "expected_text": site.source_literal,
            "replacement_text": site.target_literal,
            "token_index": site.token_index,
            "comparison_operator": site.comparison_operator,
            "operand_side": site.operand_side,
            "input_code_hash": sha256_hex(source.encode("utf-8")),
            "literal_hash": sha256_hex(site.source_literal.encode("utf-8")),
            "table_hash": table_hash,
        },
    )


def _inverse_trace(
    candidate: str,
    site: LiteralBoundSite,
    *,
    table_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "replace_numeric_bound_literal_exact",
            "mutation_id": f"inverse_of_{site.mutation_id}",
            "start": site.start,
            "end": site.start + len(site.target_literal),
            "expected_text": site.target_literal,
            "replacement_text": site.source_literal,
            "token_index": site.token_index,
            "comparison_operator": site.comparison_operator,
            "operand_side": site.operand_side,
            "input_code_hash": sha256_hex(candidate.encode("utf-8")),
            "literal_hash": sha256_hex(site.target_literal.encode("utf-8")),
            "table_hash": table_hash,
        },
    )


def _trace_matches_surface(
    source: str,
    *,
    start: int,
    end: int,
    expected: str,
    token_index: int,
    comparison_operator: str,
    operand_side: str,
) -> bool:
    """Check that trace metadata identifies the actual standalone token site."""

    try:
        significant = _significant(_tokenize(source))
    except _UnsupportedSource:
        return False
    matching_positions = tuple(
        position
        for position, (actual_token_index, token) in enumerate(significant)
        if actual_token_index == token_index
        and token.start == start
        and token.end == end
        and token.kind == "number"
        and token.text == expected
    )
    if len(matching_positions) != 1:
        return False
    position = matching_positions[0]
    if operand_side == "right":
        return position > 0 and significant[position - 1][1].text == comparison_operator
    return (
        operand_side == "left"
        and position + 1 < len(significant)
        and significant[position + 1][1].text == comparison_operator
    )


def apply_literal_bound_trace(
    source: str,
    trace: Sequence[Mapping[str, object]],
    *,
    expected_table_hash: str | None = None,
) -> str:
    """Replay exact N07 trace steps while rejecting stale or altered inputs."""

    if not trace:
        raise N07LiteralBoundError("empty_literal_bound_trace")
    result = source
    for item in trace:
        if item.get("operation") != "replace_numeric_bound_literal_exact":
            raise N07LiteralBoundError("unexpected_trace_operation")
        if expected_table_hash is not None and item.get("table_hash") != expected_table_hash:
            raise N07LiteralBoundError("trace_table_hash_mismatch")
        if item.get("input_code_hash") != sha256_hex(result.encode("utf-8")):
            raise N07LiteralBoundError("trace_input_code_hash_mismatch")
        start = item.get("start")
        end = item.get("end")
        expected = item.get("expected_text")
        replacement = item.get("replacement_text")
        literal_hash = item.get("literal_hash")
        mutation_id = item.get("mutation_id")
        token_index = item.get("token_index")
        comparison_operator = item.get("comparison_operator")
        operand_side = item.get("operand_side")
        valid_mutation_ids = (
            {"zero_to_one", "inverse_of_one_to_zero"}
            if expected == "0" and replacement == "1"
            else {"one_to_zero", "inverse_of_zero_to_one"}
        )
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(expected, str)
            or not isinstance(replacement, str)
            or expected not in {"0", "1"}
            or replacement not in {"0", "1"}
            or expected == replacement
            or not isinstance(literal_hash, str)
            or not isinstance(mutation_id, str)
            or mutation_id not in valid_mutation_ids
            or not isinstance(token_index, int)
            or isinstance(token_index, bool)
            or comparison_operator not in {"<", ">", "≤", "≥"}
            or operand_side not in {"left", "right"}
        ):
            raise N07LiteralBoundError("malformed_literal_bound_trace")
        if not 0 <= start <= end <= len(result):
            raise N07LiteralBoundError("trace_span_out_of_bounds")
        if result[start:end] != expected:
            raise N07LiteralBoundError("trace_expected_text_mismatch")
        if literal_hash != sha256_hex(expected.encode("utf-8")):
            raise N07LiteralBoundError("trace_literal_hash_mismatch")
        if not _trace_matches_surface(
            result,
            start=start,
            end=end,
            expected=expected,
            token_index=token_index,
            comparison_operator=comparison_operator,
            operand_side=operand_side,
        ):
            raise N07LiteralBoundError("trace_surface_site_mismatch")
        result = result[:start] + replacement + result[end:]
    return result


def _expected_structural_diff(
    site: LiteralBoundSite,
    *,
    table_hash: str,
) -> dict[str, JsonValue]:
    return {
        "operation": "replace_numeric_bound_literal_exact",
        "mutation_id": site.mutation_id,
        "source_literal": site.source_literal,
        "target_literal": site.target_literal,
        "source_atom": site.source_atom,
        "target_atom": site.target_atom,
        "comparison_operator": site.comparison_operator,
        "operand_side": site.operand_side,
        "source_span_start": site.start,
        "source_span_end": site.end,
        "token_index": site.token_index,
        "table_hash": table_hash,
    }


def _exact_atom_delta(
    source_atoms: Sequence[str] | None,
    candidate_atoms: Sequence[str] | None,
    *,
    source_atom: str,
    target_atom: str,
) -> bool:
    if source_atoms is None or candidate_atoms is None:
        return False
    source_counter = Counter(source_atoms)
    candidate_counter = Counter(candidate_atoms)
    removed = source_counter - candidate_counter
    added = candidate_counter - source_counter
    # Lean's elaborated numeral encoding may contain the same literal more
    # than once (for example in both the value and OfNat witness).  The exact
    # invariant is therefore a balanced, non-zero replacement of *only* the
    # configured literal atoms, rather than an assumed multiplicity of one.
    return (
        set(removed) == {source_atom}
        and set(added) == {target_atom}
        and removed[source_atom] == added[target_atom]
        and removed[source_atom] > 0
    )


class N07LiteralBoundRule:
    """One-site finite numeric-bound mutation; outputs stay provisional."""

    rule_id = "n07_literal_bound"
    family_id = "n07_literal_bound"
    polarity = Polarity.NEGATIVE
    implementation_key = "n07_literal_bound"

    def __init__(
        self,
        *,
        registry_hash: str,
        config: N07LiteralBoundConfig | None = None,
        rule_config_hash: str | None = None,
    ) -> None:
        if len(registry_hash) != 64:
            raise N07LiteralBoundError("registry_hash must be a SHA-256 hex digest")
        int(registry_hash, 16)
        if (config is None) != (rule_config_hash is None):
            raise N07LiteralBoundError("config and rule_config_hash must be supplied together")
        if config is None:
            loaded = load_n07_literal_bound_config()
            config = loaded.config
            rule_config_hash = loaded.config_hash
        assert rule_config_hash is not None
        if len(rule_config_hash) != 64:
            raise N07LiteralBoundError("rule_config_hash must be a SHA-256 hex digest")
        int(rule_config_hash, 16)
        self.registry_hash = registry_hash
        self.config = config
        self.rule_config_hash = rule_config_hash
        self.table_hash = literal_bound_table_hash(config)
        self.rule_version = config.rule_version
        self.audit_config_hash = hash_canonical(
            {
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "rule_config_hash": rule_config_hash,
                "registry_hash": registry_hash,
                "table_hash": self.table_hash,
                "policy": "exact_numeric_bound_delta_provisional_v1",
            }
        )

    @classmethod
    def from_repository(
        cls,
        *,
        registry_hash: str,
        repo_root: Path | None = None,
    ) -> N07LiteralBoundRule:
        loaded = load_n07_literal_bound_config(repo_root)
        return cls(
            registry_hash=registry_hash,
            config=loaded.config,
            rule_config_hash=loaded.config_hash,
        )

    def _sites(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> tuple[LiteralBoundSite, ...]:
        if not theorem.is_proposition:
            raise _UnsupportedSource("source_not_proposition")
        if theorem.elaboration_status not in _VALID_ELABORATION:
            raise _UnsupportedSource("source_does_not_elaborate")
        if representation.theorem_id != theorem.theorem_id:
            raise _UnsupportedSource("source_representation_lineage_mismatch")
        if representation.context_id != theorem.context_id:
            raise _UnsupportedSource("source_context_mismatch")
        if representation.raw_proof_stripped != theorem.proof_stripped_declaration:
            raise _UnsupportedSource("source_representation_text_mismatch")
        required_views = (
            representation.alpha_identity_fingerprint,
            representation.signature_explicit,
            representation.semantic_atoms,
            representation.operator_tree,
        )
        if any(view is None for view in required_views):
            raise _UnsupportedSource("source_required_view_missing")
        sites = enumerate_literal_bound_sites(
            theorem.proof_stripped_declaration,
            self.config,
        )
        source_atom_counts = Counter(representation.semantic_atoms or ())
        sites = tuple(site for site in sites if source_atom_counts[site.source_atom] > 0)
        if not sites:
            raise _UnsupportedSource("no_supported_literal_bound_site")
        return sites

    def assess(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> Applicability:
        try:
            sites = self._sites(theorem, representation)
        except _UnsupportedSource as exc:
            return Applicability(applicable=False, reason_codes=(exc.reason_code,))
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=tuple(
                sorted(
                    (
                        f"literal_bound:{site.token_index}:{site.start}:{site.end}:"
                        f"{site.mutation_id}:{site.comparison_operator}:{site.operand_side}"
                    )
                    for site in sites
                )
            ),
            required_capabilities=(
                "alpha_identity_fingerprint",
                "exact_literal_atom_delta",
                "exact_numeric_bound_diff",
                "lean_reelaboration",
                "operator_tree",
            ),
            metadata={
                "eligible_site_count": len(sites),
                "rule_config_hash": self.rule_config_hash,
                "table_hash": self.table_hash,
            },
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> Sequence[VariantDraft]:
        sites = self._sites(theorem, representation)
        site = _choose_site(sites, theorem_id=theorem.theorem_id, seed=seed)
        trace = _trace(
            theorem.proof_stripped_declaration,
            site,
            table_hash=self.table_hash,
        )
        candidate = apply_literal_bound_trace(
            theorem.proof_stripped_declaration,
            trace,
            expected_table_hash=self.table_hash,
        )
        inverse = _inverse_trace(candidate, site, table_hash=self.table_hash)
        if (
            apply_literal_bound_trace(
                candidate,
                inverse,
                expected_table_hash=self.table_hash,
            )
            != theorem.proof_stripped_declaration
        ):
            raise N07LiteralBoundError("n07_internal_round_trip_failure")
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
                intended_relation=IntendedRelation.NEAR_MISS,
                intended_error_types=site.intended_error_types,
                candidate_pool=self.config.candidate_pool,
                transformation_trace=trace,
                inverse_trace=inverse,
                expected_atom_mapping={site.source_atom: site.target_atom},
                expected_structural_diff=_expected_structural_diff(
                    site,
                    table_hash=self.table_hash,
                ),
                generation_config_hash=self.registry_hash,
                metadata={
                    "failed_proof_search_used": False,
                    "intention_is_not_label": True,
                    "rule_config_hash": self.rule_config_hash,
                    "semantic_negative_resolved": False,
                    "table_hash": self.table_hash,
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
        try:
            verify_variant_draft_id(draft)
        except ValueError:
            violations.append("draft_id_mismatch")
        lineage_ok = (
            draft.rule_id == self.rule_id
            and draft.rule_version == self.rule_version
            and draft.family_id == self.family_id
            and draft.generation_config_hash == self.registry_hash
            and draft.candidate_pool == self.config.candidate_pool
            and draft.source_theorem_ids == (source.theorem_id,)
            and draft.source_representation_ids == (source_representation.representation_id,)
        )
        if not lineage_ok:
            violations.append("draft_lineage_mismatch")
        context_ok = (
            source.context_id
            == source_representation.context_id
            == candidate.context_id
            == candidate_representation.context_id
            == draft.context_id
        )
        if not context_ok:
            violations.append("context_mismatch")
        representation_lineage_ok = (
            source_representation.theorem_id == source.theorem_id
            and candidate_representation.theorem_id == candidate.theorem_id
        )
        if not representation_lineage_ok:
            violations.append("representation_lineage_mismatch")
        ancestry_ok = (
            candidate.parent_theorem_ids == (source.theorem_id,)
            and candidate.root_ancestry_ids == source.root_ancestry_ids
        )
        if not ancestry_ok:
            violations.append("candidate_ancestry_mismatch")
        source_text_ok = (
            source_representation.raw_proof_stripped == source.proof_stripped_declaration
        )
        candidate_text_ok = (
            candidate.proof_stripped_declaration
            == candidate_representation.raw_proof_stripped
            == draft.candidate_code
        )
        if not source_text_ok:
            violations.append("source_representation_text_mismatch")
        if not candidate_text_ok:
            violations.append("candidate_code_or_representation_mismatch")
        candidate_hashes_ok = candidate.statement_content_hash == sha256_hex(
            candidate.proof_stripped_declaration.encode("utf-8")
        ) and draft.candidate_code_hash == sha256_hex(draft.candidate_code.encode("utf-8"))
        if not candidate_hashes_ok:
            violations.append("statement_content_hash_mismatch")
        if draft.candidate_code == source.proof_stripped_declaration:
            violations.append("candidate_unchanged")
        if draft.intended_relation != IntendedRelation.NEAR_MISS:
            violations.append("intended_relation_mismatch")

        matching_site: LiteralBoundSite | None = None
        seed_site_ok = False
        try:
            auditable_sites = tuple(
                site
                for site in enumerate_literal_bound_sites(
                    source.proof_stripped_declaration,
                    self.config,
                )
                if Counter(source_representation.semantic_atoms or ())[site.source_atom] > 0
            )
            matches = tuple(
                site
                for site in auditable_sites
                if _trace(source.proof_stripped_declaration, site, table_hash=self.table_hash)
                == draft.transformation_trace
                and _inverse_trace(draft.candidate_code, site, table_hash=self.table_hash)
                == draft.inverse_trace
            )
            if len(matches) == 1:
                matching_site = matches[0]
                seed_site_ok = (
                    _choose_site(
                        auditable_sites,
                        theorem_id=source.theorem_id,
                        seed=draft.seed,
                    )
                    == matching_site
                )
        except N07LiteralBoundError:
            matching_site = None
        exact_trace_ok = matching_site is not None
        if not exact_trace_ok:
            violations.append("trace_not_from_current_table")
        if not seed_site_ok:
            violations.append("seed_site_selection_mismatch")

        try:
            forward_ok = (
                apply_literal_bound_trace(
                    source.proof_stripped_declaration,
                    draft.transformation_trace,
                    expected_table_hash=self.table_hash,
                )
                == draft.candidate_code
            )
        except N07LiteralBoundError:
            forward_ok = False
        if not forward_ok:
            violations.append("forward_trace_failed")
        try:
            roundtrip_ok = (
                draft.inverse_trace is not None
                and apply_literal_bound_trace(
                    draft.candidate_code,
                    draft.inverse_trace,
                    expected_table_hash=self.table_hash,
                )
                == source.proof_stripped_declaration
            )
        except N07LiteralBoundError:
            roundtrip_ok = False
        if not roundtrip_ok:
            violations.append("inverse_roundtrip_failed")

        expected_diff_ok = (
            matching_site is not None
            and draft.expected_structural_diff
            == _expected_structural_diff(matching_site, table_hash=self.table_hash)
            and draft.expected_atom_mapping
            == {matching_site.source_atom: matching_site.target_atom}
            and draft.intended_error_types == matching_site.intended_error_types
        )
        if not expected_diff_ok:
            violations.append("expected_structural_diff_mismatch")

        source_elaborates = source.elaboration_status in _VALID_ELABORATION
        candidate_elaborates = candidate.elaboration_status in _VALID_ELABORATION
        if not source_elaborates:
            violations.append("source_does_not_elaborate")
        if not candidate_elaborates:
            violations.append("candidate_does_not_elaborate")
        required_view_names = (
            "signature_explicit",
            "semantic_atoms",
            "operator_tree",
        )
        source_views_ok = (
            all(
                source_representation.view_status.get(name) == ViewStatus.OK
                for name in required_view_names
            )
            and source_representation.alpha_identity_fingerprint is not None
        )
        candidate_views_ok = (
            all(
                candidate_representation.view_status.get(name) == ViewStatus.OK
                for name in required_view_names
            )
            and candidate_representation.alpha_identity_fingerprint is not None
        )
        if not source_views_ok:
            violations.append("source_required_view_failed")
        if not candidate_views_ok:
            violations.append("candidate_required_view_failed")

        alpha_changed = (
            source_representation.alpha_identity_fingerprint is not None
            and candidate_representation.alpha_identity_fingerprint is not None
            and source_representation.alpha_identity_fingerprint
            != candidate_representation.alpha_identity_fingerprint
        )
        signature_changed = (
            source_representation.signature_explicit is not None
            and candidate_representation.signature_explicit is not None
            and source_representation.signature_explicit
            != candidate_representation.signature_explicit
        )
        tree_changed = (
            source_representation.operator_tree is not None
            and candidate_representation.operator_tree is not None
            and source_representation.operator_tree != candidate_representation.operator_tree
        )
        atom_delta_ok = matching_site is not None and _exact_atom_delta(
            source_representation.semantic_atoms,
            candidate_representation.semantic_atoms,
            source_atom=matching_site.source_atom,
            target_atom=matching_site.target_atom,
        )
        if not alpha_changed:
            violations.append("alpha_identity_not_changed")
        if not signature_changed:
            violations.append("signature_explicit_not_changed")
        if not tree_changed:
            violations.append("operator_tree_not_changed")
        if not atom_delta_ok:
            violations.append("unexpected_semantic_atom_delta")

        clean = not violations
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("n07_exact_numeric_bound_literal",),
                required_capabilities=(
                    "alpha_identity_fingerprint",
                    "exact_literal_atom_delta",
                    "exact_numeric_bound_diff",
                    "lean_reelaboration",
                    "operator_tree",
                ),
                metadata={"table_hash": self.table_hash},
            ),
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=(
                candidate.elaboration_status
                if candidate_elaborates and clean
                else ValidationStatus.QUARANTINED
            ),
            recommended_quality_tier=(QualityTier.PROVISIONAL if clean else QualityTier.UNKNOWN),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=(
                exact_trace_ok
                and seed_site_ok
                and forward_ok
                and expected_diff_ok
                and alpha_changed
                and signature_changed
                and tree_changed
            ),
            atom_mapping_ok=atom_delta_ok,
            inverse_or_roundtrip_ok=roundtrip_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "alpha_identity_changed": alpha_changed,
                "context_equal": context_ok,
                "failed_proof_search_used": False,
                "intention_is_not_label": True,
                "operator_tree_changed": tree_changed,
                "semantic_negative_resolved": False,
                "signature_explicit_changed": signature_changed,
                "table_hash": self.table_hash,
            },
        )


__all__ = [
    "LiteralBoundSite",
    "N07LiteralBoundConfig",
    "N07LiteralBoundError",
    "N07LiteralBoundRule",
    "N07LiteralMutation",
    "apply_literal_bound_trace",
    "enumerate_literal_bound_sites",
    "literal_bound_table_hash",
    "load_n07_literal_bound_config",
]
