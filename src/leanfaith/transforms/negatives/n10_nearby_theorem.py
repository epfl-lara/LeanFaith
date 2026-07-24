"""N10 high-overlap nearby-theorem component substitution.

N10 is deliberately a two-source rule.  A primary theorem supplies the
candidate declaration identity and a distinct nearby donor theorem supplies
one curated, type-compatible signature component.  The source signatures must
be positionally identical except for that one component, both theorem
ancestries are recorded, and the resulting candidate is re-audited against
both sources.

This module never infers a semantic negative.  A generated draft records only
the ``near_miss`` intention and remains ``provisional`` after a clean
mechanical audit; any lineage, elaboration, representation, or trace mismatch
is quarantined as ``unknown``.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from leanfaith.config.hashing import hash_canonical
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
from leanfaith.transforms.n01_operator import (
    N01ReplacementEntry,
    N01ReplacementTable,
)
from leanfaith.transforms.protocol import build_transformation_audit, build_variant_draft

NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
SemanticVersion = Annotated[
    str,
    Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True),
]
ErrorCode = Annotated[str, Field(pattern=r"^E(0[1-9]|[12][0-9]|30)$", strict=True)]
_VALID_ELABORATION = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
_DECLARATION_KEYWORDS = frozenset({"lemma", "theorem"})
_OPEN_TO_CLOSE = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}
_CLOSE_TO_OPEN = {close: open_ for open_, close in _OPEN_TO_CLOSE.items()}
_LOGICAL_OR = "\u2228"
_MUTABLE_SYMBOLS = ("≤", "∧", _LOGICAL_OR, "<")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class N10NearbyTheoremError(ValueError):
    """N10 configuration, parsing, replay, or audit failed closed."""


class N10NearbyReplacementEntry(StrictModel):
    """One N10 admission linked to an existing directed N01 replacement."""

    entry_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
    family_id: Literal["n10_nearby_theorem"]
    replacement_entry_id: Annotated[
        str,
        Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True),
    ]
    intended_error_types: tuple[ErrorCode, ...]

    @model_validator(mode="after")
    def _canonical(self) -> N10NearbyReplacementEntry:
        if self.intended_error_types != tuple(sorted(set(self.intended_error_types))):
            raise ValueError("intended_error_types must be sorted and unique")
        if not self.intended_error_types:
            raise ValueError("intended_error_types must be nonempty")
        return self


class N10NearbyTheoremConfig(StrictModel):
    """Versioned, fail-closed v1 N10 execution policy."""

    schema_version: Literal[1] = 1
    rule_id: Literal["n10_nearby_theorem"] = "n10_nearby_theorem"
    rule_version: SemanticVersion
    family_id: Literal["n10_nearby_theorem"] = "n10_nearby_theorem"
    implementation_key: Literal["n10_nearby_theorem"] = "n10_nearby_theorem"
    candidate_pool: NonEmptyStr
    replacement_table_path: NonEmptyStr
    supported_declaration_kinds: tuple[Literal["lemma", "theorem"], ...]
    placeholder_forms: tuple[Literal["by_sorry", "sorry"], ...]
    minimum_signature_tokens: int = Field(ge=1, strict=True)
    minimum_positional_overlap_ppm: int = Field(ge=0, le=1_000_000, strict=True)
    require_exactly_one_curated_component_diff: Literal[True] = True
    require_distinct_source_theorems: Literal[True] = True
    require_disjoint_root_ancestries: Literal[True] = True
    require_source_donor_candidate_elaboration: Literal[True] = True
    failed_proof_search_is_negative_evidence: Literal[False] = False

    @model_validator(mode="after")
    def _closed_scope(self) -> N10NearbyTheoremConfig:
        if self.supported_declaration_kinds != ("lemma", "theorem"):
            raise ValueError("supported_declaration_kinds must be exactly [lemma, theorem]")
        if self.placeholder_forms != ("by_sorry", "sorry"):
            raise ValueError("placeholder_forms must be exactly [by_sorry, sorry]")
        return self


@dataclass(frozen=True, slots=True)
class LoadedN10NearbyTheoremConfig:
    config: N10NearbyTheoremConfig
    config_hash: str
    table: N01ReplacementTable
    table_hash: str
    nearby_entries: tuple[N10NearbyReplacementEntry, ...]
    config_path: Path
    table_path: Path


def load_n10_nearby_theorem_config(
    repo_root: Path | None = None,
    *,
    config_path: Path | None = None,
    table_path: Path | None = None,
) -> LoadedN10NearbyTheoremConfig:
    """Load N10 and its typed extension of the shared v1 replacement table."""

    root = find_repo_root(repo_root).resolve()
    resolved_config = (
        config_path or root / "configs/transformations/n10_nearby_theorem.yaml"
    ).resolve()
    if not resolved_config.is_relative_to(root):
        raise N10NearbyTheoremError("n10 config path escapes the repository")
    loaded_config: LoadedConfig[N10NearbyTheoremConfig] = load_config(
        resolved_config,
        N10NearbyTheoremConfig,
    )
    declared_table = (root / loaded_config.config.replacement_table_path).resolve()
    if not declared_table.is_relative_to(root):
        raise N10NearbyTheoremError("n10 replacement-table path escapes the repository")
    resolved_table = (table_path or declared_table).resolve()
    if resolved_table != declared_table:
        raise N10NearbyTheoremError("n10 table override must equal the config-declared path")
    loaded_table: LoadedConfig[N01ReplacementTable] = load_config(
        resolved_table,
        N01ReplacementTable,
    )
    nearby_entries = tuple(
        N10NearbyReplacementEntry.model_validate(raw)
        for raw in loaded_table.config.nearby_theorem_entries
    )
    if not nearby_entries:
        raise N10NearbyTheoremError("replacement table has no N10 nearby-theorem entries")
    nearby_ids = tuple(entry.entry_id for entry in nearby_entries)
    if len(nearby_ids) != len(set(nearby_ids)):
        raise N10NearbyTheoremError("N10 nearby entry IDs must be unique")
    replacement_ids = {entry.entry_id for entry in loaded_table.config.entries}
    unknown = sorted(
        {
            entry.replacement_entry_id
            for entry in nearby_entries
            if entry.replacement_entry_id not in replacement_ids
        }
    )
    if unknown:
        raise N10NearbyTheoremError(
            f"N10 entries reference unknown N01 replacement entries: {unknown}"
        )
    return LoadedN10NearbyTheoremConfig(
        config=loaded_config.config,
        config_hash=loaded_config.config_hash,
        table=loaded_table.config,
        table_hash=loaded_table.config_hash,
        nearby_entries=nearby_entries,
        config_path=resolved_config,
        table_path=resolved_table,
    )


@dataclass(frozen=True, slots=True)
class _Token:
    start: int
    end: int
    text: str
    kind: Literal["atom", "guillemet", "string", "char", "symbol", "comment"]


@dataclass(frozen=True, slots=True)
class _DeclarationParts:
    kind: str
    name: str
    name_start: int
    name_end: int
    signature_start: int
    signature_end: int
    proof_tail: str
    signature_tokens: tuple[_Token, ...]


@dataclass(frozen=True, slots=True)
class NearbyTheoremSite:
    """One exact curated component supplied by a nearby donor theorem."""

    entry_id: str
    replacement_entry_id: str
    primary_start: int
    primary_end: int
    donor_start: int
    donor_end: int
    primary_token: str
    donor_token: str
    type_precondition: str
    source_atoms: tuple[str, ...]
    target_atoms: tuple[str, ...]
    intended_error_types: tuple[str, ...]
    signature_token_count: int
    positional_overlap_ppm: int

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "donor_end": self.donor_end,
                "donor_start": self.donor_start,
                "donor_token": self.donor_token,
                "entry_id": self.entry_id,
                "intended_error_types": self.intended_error_types,
                "positional_overlap_ppm": self.positional_overlap_ppm,
                "primary_end": self.primary_end,
                "primary_start": self.primary_start,
                "primary_token": self.primary_token,
                "replacement_entry_id": self.replacement_entry_id,
                "signature_token_count": self.signature_token_count,
                "source_atoms": self.source_atoms,
                "target_atoms": self.target_atoms,
                "type_precondition": self.type_precondition,
            }
        )


def _lex_lean(source: str) -> tuple[_Token, ...]:
    """Small deterministic lexer sufficient for proof-stripped declarations."""

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
                raise N10NearbyTheoremError("unterminated_block_comment")
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
                raise N10NearbyTheoremError("unterminated_string_literal")
            tokens.append(_Token(start, index, source[start:index], "string"))
            continue
        if char == "'" and index + 1 < length:
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
                raise N10NearbyTheoremError("unterminated_character_literal")
            tokens.append(_Token(start, index, source[start:index], "char"))
            continue
        if char == "«":
            start = index
            end = source.find("»", index + 1)
            if end < 0:
                raise N10NearbyTheoremError("unterminated_guillemet_identifier")
            index = end + 1
            tokens.append(_Token(start, index, source[start:index], "guillemet"))
            continue
        if char in _OPEN_TO_CLOSE or char in _CLOSE_TO_OPEN:
            tokens.append(_Token(index, index + 1, char, "symbol"))
            index += 1
            continue
        operator = next(
            (
                value
                for value in (
                    ":=",
                    "=>",
                    "->",
                    "::",
                    "<=",
                    ">=",
                    "≠",
                    *_MUTABLE_SYMBOLS,
                )
                if source.startswith(value, index)
            ),
            None,
        )
        if operator is not None:
            end = index + len(operator)
            tokens.append(_Token(index, end, operator, "symbol"))
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
                or current in f'(){{}}[]⦃⦄":,;@«<≤≠∧{_LOGICAL_OR}'
                or source.startswith("--", index)
                or source.startswith("/-", index)
                or any(
                    source.startswith(value, index)
                    for value in (":=", "=>", "->", "::", "<=", ">=")
                )
            ):
                break
            index += 1
        if index == start:
            index += 1
            tokens.append(_Token(start, index, source[start:index], "symbol"))
        else:
            tokens.append(_Token(start, index, source[start:index], "atom"))
    return tuple(tokens)


def _declaration_parts(source: str) -> _DeclarationParts:
    tokens = _lex_lean(source)
    code = tuple(token for token in tokens if token.kind != "comment")
    stack: list[str] = []
    keyword_index: int | None = None
    for index, token in enumerate(code):
        if token.text in _OPEN_TO_CLOSE:
            stack.append(token.text)
        elif token.text in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[token.text]:
                raise N10NearbyTheoremError("mismatched_declaration_delimiter")
            stack.pop()
        elif not stack and token.text in _DECLARATION_KEYWORDS:
            keyword_index = index
            break
    if keyword_index is None or keyword_index + 1 >= len(code):
        raise N10NearbyTheoremError("unsupported_declaration_kind")
    keyword = code[keyword_index]
    name = code[keyword_index + 1]
    if name.kind not in {"atom", "guillemet"}:
        raise N10NearbyTheoremError("invalid_declaration_name")

    result_colon: _Token | None = None
    proof_assignment: _Token | None = None
    stack = []
    for token in code[keyword_index + 2 :]:
        if token.text in _OPEN_TO_CLOSE:
            stack.append(token.text)
        elif token.text in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[token.text]:
                raise N10NearbyTheoremError("mismatched_declaration_delimiter")
            stack.pop()
        elif not stack and token.text == ":" and result_colon is None:
            result_colon = token
        elif not stack and token.text == ":=" and result_colon is not None:
            if proof_assignment is not None:
                raise N10NearbyTheoremError("multiple_top_level_proof_assignments")
            proof_assignment = token
    if result_colon is None:
        raise N10NearbyTheoremError("missing_result_colon")
    if proof_assignment is None:
        raise N10NearbyTheoremError("missing_proof_assignment")
    signature_tokens = tuple(
        token
        for token in code[keyword_index + 2 :]
        if name.end <= token.start and token.end <= proof_assignment.start
    )
    if not signature_tokens:
        raise N10NearbyTheoremError("empty_signature")
    proof_tail = source[proof_assignment.start :]
    if not (
        re.fullmatch(r":=\s*by\s+sorry\s*", proof_tail, flags=re.DOTALL)
        or re.fullmatch(r":=\s*sorry\s*", proof_tail, flags=re.DOTALL)
    ):
        raise N10NearbyTheoremError("unsupported_proof_placeholder")
    return _DeclarationParts(
        kind=keyword.text,
        name=name.text,
        name_start=name.start,
        name_end=name.end,
        signature_start=name.end,
        signature_end=proof_assignment.start,
        proof_tail=proof_tail,
        signature_tokens=signature_tokens,
    )


def _canonical_name(name: str) -> str:
    return name[1:-1] if name.startswith("«") and name.endswith("»") else name


def _replacement_by_id(
    table: N01ReplacementTable,
    entry_id: str,
) -> N01ReplacementEntry:
    for entry in table.entries:
        if entry.entry_id == entry_id:
            return entry
    raise N10NearbyTheoremError(f"unknown replacement entry {entry_id!r}")


def _expected_atoms(
    primary_atoms: Sequence[str],
    entry: N01ReplacementEntry,
) -> Counter[str]:
    expected = Counter(primary_atoms)
    source = Counter(entry.source_atoms)
    for atom, count in source.items():
        if expected[atom] != count:
            raise N10NearbyTheoremError("primary_atom_precondition_failed")
        expected[atom] -= count
        if expected[atom] == 0:
            del expected[atom]
    required = Counter(entry.required_context_atoms)
    if any(Counter(primary_atoms)[atom] < count for atom, count in required.items()):
        raise N10NearbyTheoremError("required_context_atom_missing")
    expected.update(entry.target_atoms)
    return expected


def enumerate_nearby_theorem_sites(
    primary: TheoremRecord,
    primary_representation: RepresentationRecord,
    donor: TheoremRecord,
    donor_representation: RepresentationRecord,
    config: N10NearbyTheoremConfig,
    table: N01ReplacementTable,
    nearby_entries: Sequence[N10NearbyReplacementEntry],
) -> tuple[NearbyTheoremSite, ...]:
    """Return exact one-component, curated, high-overlap pair matches."""

    if primary_representation.semantic_atoms is None:
        raise N10NearbyTheoremError("primary_missing_semantic_atoms")
    if donor_representation.semantic_atoms is None:
        raise N10NearbyTheoremError("donor_missing_semantic_atoms")
    primary_parts = _declaration_parts(primary.proof_stripped_declaration)
    donor_parts = _declaration_parts(donor.proof_stripped_declaration)
    if primary_parts.kind != donor_parts.kind:
        return ()
    primary_tokens = primary_parts.signature_tokens
    donor_tokens = donor_parts.signature_tokens
    if len(primary_tokens) != len(donor_tokens):
        return ()
    if len(primary_tokens) < config.minimum_signature_tokens:
        return ()
    mismatches = tuple(
        index
        for index, (left, right) in enumerate(zip(primary_tokens, donor_tokens, strict=True))
        if (left.kind, left.text) != (right.kind, right.text)
    )
    if len(mismatches) != 1:
        return ()
    mismatch = mismatches[0]
    primary_token = primary_tokens[mismatch]
    donor_token = donor_tokens[mismatch]
    overlap_ppm = ((len(primary_tokens) - 1) * 1_000_000) // len(primary_tokens)
    if overlap_ppm < config.minimum_positional_overlap_ppm:
        return ()

    sites: list[NearbyTheoremSite] = []
    for nearby in nearby_entries:
        replacement = _replacement_by_id(table, nearby.replacement_entry_id)
        if (
            primary_token.kind != "symbol"
            or donor_token.kind != "symbol"
            or primary_token.text != replacement.source_token
            or donor_token.text != replacement.target_token
        ):
            continue
        try:
            expected_donor = _expected_atoms(
                primary_representation.semantic_atoms,
                replacement,
            )
        except N10NearbyTheoremError:
            continue
        if Counter(donor_representation.semantic_atoms) != expected_donor:
            continue
        sites.append(
            NearbyTheoremSite(
                entry_id=nearby.entry_id,
                replacement_entry_id=replacement.entry_id,
                primary_start=primary_token.start,
                primary_end=primary_token.end,
                donor_start=donor_token.start,
                donor_end=donor_token.end,
                primary_token=primary_token.text,
                donor_token=donor_token.text,
                type_precondition=replacement.type_precondition,
                source_atoms=replacement.source_atoms,
                target_atoms=replacement.target_atoms,
                intended_error_types=nearby.intended_error_types,
                signature_token_count=len(primary_tokens),
                positional_overlap_ppm=overlap_ppm,
            )
        )
    return tuple(sorted(sites, key=lambda site: (site.primary_start, site.entry_id)))


def _trace(
    site: NearbyTheoremSite,
    *,
    primary: TheoremRecord,
    primary_representation: RepresentationRecord,
    donor: TheoremRecord,
    donor_representation: RepresentationRecord,
    primary_name: str,
    config_hash: str,
    table_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "substitute_nearby_theorem_component",
            "entry_id": site.entry_id,
            "replacement_entry_id": site.replacement_entry_id,
            "primary_theorem_id": primary.theorem_id,
            "primary_representation_id": primary_representation.representation_id,
            "donor_theorem_id": donor.theorem_id,
            "donor_representation_id": donor_representation.representation_id,
            "primary_root_ancestry_ids": list(primary.root_ancestry_ids),
            "donor_root_ancestry_ids": list(donor.root_ancestry_ids),
            "primary_declaration_name": primary_name,
            "primary_start": site.primary_start,
            "primary_end": site.primary_end,
            "donor_start": site.donor_start,
            "donor_end": site.donor_end,
            "expected_text": site.primary_token,
            "replacement_text": site.donor_token,
            "type_precondition": site.type_precondition,
            "signature_token_count": site.signature_token_count,
            "positional_overlap_ppm": site.positional_overlap_ppm,
            "primary_signature_hash": hashlib.sha256(
                primary.proof_stripped_declaration.encode("utf-8")
            ).hexdigest(),
            "donor_signature_hash": hashlib.sha256(
                donor.proof_stripped_declaration.encode("utf-8")
            ).hexdigest(),
            "rule_config_hash": config_hash,
            "replacement_table_hash": table_hash,
        },
    )


def _inverse_trace(
    site: NearbyTheoremSite,
    *,
    primary: TheoremRecord,
    primary_representation: RepresentationRecord,
    donor: TheoremRecord,
    donor_representation: RepresentationRecord,
    primary_name: str,
    config_hash: str,
    table_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "restore_primary_theorem_component",
            "entry_id": site.entry_id,
            "replacement_entry_id": site.replacement_entry_id,
            "primary_theorem_id": primary.theorem_id,
            "primary_representation_id": primary_representation.representation_id,
            "donor_theorem_id": donor.theorem_id,
            "donor_representation_id": donor_representation.representation_id,
            "primary_root_ancestry_ids": list(primary.root_ancestry_ids),
            "donor_root_ancestry_ids": list(donor.root_ancestry_ids),
            "primary_declaration_name": primary_name,
            "primary_start": site.primary_start,
            "primary_end": site.primary_start + len(site.donor_token),
            "donor_start": site.donor_start,
            "donor_end": site.donor_end,
            "expected_text": site.donor_token,
            "replacement_text": site.primary_token,
            "type_precondition": site.type_precondition,
            "signature_token_count": site.signature_token_count,
            "positional_overlap_ppm": site.positional_overlap_ppm,
            "primary_signature_hash": hashlib.sha256(
                primary.proof_stripped_declaration.encode("utf-8")
            ).hexdigest(),
            "donor_signature_hash": hashlib.sha256(
                donor.proof_stripped_declaration.encode("utf-8")
            ).hexdigest(),
            "rule_config_hash": config_hash,
            "replacement_table_hash": table_hash,
        },
    )


def apply_nearby_theorem_trace(
    source: str,
    trace: Sequence[Mapping[str, object]],
    *,
    expected_config_hash: str | None = None,
    expected_table_hash: str | None = None,
) -> str:
    """Apply exactly one N10 component trace and reject trace drift."""

    if len(trace) != 1:
        raise N10NearbyTheoremError("n10_trace_must_have_exactly_one_step")
    step = trace[0]
    if step.get("operation") not in {
        "substitute_nearby_theorem_component",
        "restore_primary_theorem_component",
    }:
        raise N10NearbyTheoremError("unsupported_n10_trace_operation")
    start = step.get("primary_start")
    end = step.get("primary_end")
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
        raise N10NearbyTheoremError("malformed_n10_trace")
    if start < 0 or end < start or end > len(source):
        raise N10NearbyTheoremError("n10_trace_span_out_of_bounds")
    if source[start:end] != expected:
        raise N10NearbyTheoremError("n10_trace_expected_text_mismatch")
    if expected_config_hash is not None and step.get("rule_config_hash") != expected_config_hash:
        raise N10NearbyTheoremError("n10_rule_config_hash_mismatch")
    if (
        expected_table_hash is not None
        and step.get("replacement_table_hash") != expected_table_hash
    ):
        raise N10NearbyTheoremError("n10_replacement_table_hash_mismatch")
    return source[:start] + replacement + source[end:]


def _expected_structural_diff(
    site: NearbyTheoremSite,
    *,
    primary: TheoremRecord,
    donor: TheoremRecord,
    config_hash: str,
    table_hash: str,
) -> dict[str, JsonValue]:
    return {
        "operation": "substitute_nearby_theorem_component",
        "entry_id": site.entry_id,
        "replacement_entry_id": site.replacement_entry_id,
        "primary_theorem_id": primary.theorem_id,
        "donor_theorem_id": donor.theorem_id,
        "primary_component_start": site.primary_start,
        "primary_component_end": site.primary_end,
        "donor_component_start": site.donor_start,
        "donor_component_end": site.donor_end,
        "primary_token": site.primary_token,
        "donor_token": site.donor_token,
        "type_precondition": site.type_precondition,
        "exact_component_diff_count": 1,
        "signature_token_count": site.signature_token_count,
        "positional_overlap_ppm": site.positional_overlap_ppm,
        "rule_config_hash": config_hash,
        "replacement_table_hash": table_hash,
    }


def _choose_site(
    sites: Sequence[NearbyTheoremSite],
    *,
    primary_id: str,
    donor_id: str,
    seed: int,
) -> NearbyTheoremSite:
    if not sites:
        raise N10NearbyTheoremError("no_curated_nearby_theorem_component")

    def rank(site: NearbyTheoremSite) -> bytes:
        payload = f"n10_nearby_theorem_v1\0{primary_id}\0{donor_id}\0{seed}\0{site.stable_key}"
        return hashlib.sha256(payload.encode("utf-8")).digest()

    return min(sites, key=rank)


def _source_pairs(
    primary: TheoremRecord,
    primary_representation: RepresentationRecord,
    donor: TheoremRecord,
    donor_representation: RepresentationRecord,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    pairs = tuple(
        sorted(
            (
                (primary.theorem_id, primary_representation.representation_id),
                (donor.theorem_id, donor_representation.representation_id),
            )
        )
    )
    return (
        tuple(theorem_id for theorem_id, _ in pairs),
        tuple(representation_id for _, representation_id in pairs),
    )


class N10NearbyTheoremRule:
    """Explicit pair-rule implementation; every output remains provisional."""

    rule_id = "n10_nearby_theorem"
    family_id = "n10_nearby_theorem"
    polarity = Polarity.NEGATIVE
    implementation_key = "n10_nearby_theorem"

    def __init__(
        self,
        *,
        generation_config_hash: str,
        config: N10NearbyTheoremConfig,
        rule_config_hash: str,
        table: N01ReplacementTable,
        table_hash: str,
        nearby_entries: Sequence[N10NearbyReplacementEntry],
    ) -> None:
        for field_name, digest in (
            ("generation_config_hash", generation_config_hash),
            ("rule_config_hash", rule_config_hash),
            ("table_hash", table_hash),
        ):
            if _HEX64.fullmatch(digest) is None:
                raise N10NearbyTheoremError(f"{field_name} must be a SHA-256 hex digest")
        self.generation_config_hash = generation_config_hash
        self.config = config
        self.rule_config_hash = rule_config_hash
        self.table = table
        self.table_hash = table_hash
        self.nearby_entries = tuple(nearby_entries)
        self.rule_version = config.rule_version
        self.audit_config_hash = hash_canonical(
            {
                "certificate": "dual_ancestry_exact_nearby_component_v1",
                "generation_config_hash": generation_config_hash,
                "proof_search_semantics": "none",
                "replacement_table_hash": table_hash,
                "rule_config_hash": rule_config_hash,
            }
        )

    @classmethod
    def from_repository(
        cls,
        *,
        generation_config_hash: str,
        repo_root: Path | None = None,
    ) -> N10NearbyTheoremRule:
        loaded = load_n10_nearby_theorem_config(repo_root)
        return cls(
            generation_config_hash=generation_config_hash,
            config=loaded.config,
            rule_config_hash=loaded.config_hash,
            table=loaded.table,
            table_hash=loaded.table_hash,
            nearby_entries=loaded.nearby_entries,
        )

    def _preflight_reason(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
    ) -> str | None:
        if primary.theorem_id == donor.theorem_id:
            return "source_theorems_not_distinct"
        if primary_representation.representation_id == donor_representation.representation_id:
            return "source_representations_not_distinct"
        if primary.context_id != donor.context_id:
            return "source_contexts_differ"
        if (
            primary_representation.context_id != primary.context_id
            or donor_representation.context_id != donor.context_id
        ):
            return "source_representation_context_mismatch"
        if (
            primary_representation.theorem_id != primary.theorem_id
            or donor_representation.theorem_id != donor.theorem_id
        ):
            return "source_representation_lineage_mismatch"
        if set(primary.root_ancestry_ids) & set(donor.root_ancestry_ids):
            return "source_root_ancestries_not_disjoint"
        if not primary.is_proposition or not donor.is_proposition:
            return "source_not_proposition"
        if (
            primary.elaboration_status not in _VALID_ELABORATION
            or donor.elaboration_status not in _VALID_ELABORATION
        ):
            return "source_does_not_elaborate"
        if (
            primary.declaration_kind not in self.config.supported_declaration_kinds
            or donor.declaration_kind not in self.config.supported_declaration_kinds
        ):
            return "unsupported_declaration_kind"
        if (
            primary_representation.raw_proof_stripped != primary.proof_stripped_declaration
            or donor_representation.raw_proof_stripped != donor.proof_stripped_declaration
        ):
            return "source_representation_text_mismatch"
        required = ("signature_explicit", "semantic_atoms", "operator_tree")
        if any(
            primary_representation.view_status[name] != ViewStatus.OK
            or donor_representation.view_status[name] != ViewStatus.OK
            for name in required
        ):
            return "source_required_view_failed"
        if (
            primary_representation.alpha_identity_fingerprint is None
            or donor_representation.alpha_identity_fingerprint is None
        ):
            return "source_missing_alpha_identity_fingerprint"
        if (
            primary_representation.alpha_identity_fingerprint
            == donor_representation.alpha_identity_fingerprint
        ):
            return "source_alpha_identity_unchanged"
        try:
            primary_parts = _declaration_parts(primary.proof_stripped_declaration)
            donor_parts = _declaration_parts(donor.proof_stripped_declaration)
        except N10NearbyTheoremError as exc:
            return str(exc)
        if (
            primary_parts.kind != primary.declaration_kind
            or donor_parts.kind != donor.declaration_kind
        ):
            return "declaration_kind_metadata_mismatch"
        if (
            primary.declaration_name is None
            or _canonical_name(primary_parts.name) != primary.declaration_name
        ):
            return "primary_declaration_identity_mismatch"
        if (
            donor.declaration_name is None
            or _canonical_name(donor_parts.name) != donor.declaration_name
        ):
            return "donor_declaration_identity_mismatch"
        return None

    def _sites(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
    ) -> tuple[NearbyTheoremSite, ...]:
        reason = self._preflight_reason(
            primary,
            primary_representation,
            donor,
            donor_representation,
        )
        if reason is not None:
            return ()
        return enumerate_nearby_theorem_sites(
            primary,
            primary_representation,
            donor,
            donor_representation,
            self.config,
            self.table,
            self.nearby_entries,
        )

    def assess_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
    ) -> Applicability:
        reason = self._preflight_reason(
            primary,
            primary_representation,
            donor,
            donor_representation,
        )
        if reason is not None:
            return Applicability(
                applicable=False,
                reason_codes=(reason,),
                required_capabilities=(
                    "alpha_identity_fingerprint",
                    "dual_ancestry_lineage",
                    "lean_reelaboration",
                    "nearby_signature_overlap",
                    "operator_tree",
                    "semantic_atom_delta",
                ),
            )
        try:
            sites = self._sites(
                primary,
                primary_representation,
                donor,
                donor_representation,
            )
        except N10NearbyTheoremError as exc:
            return Applicability(
                applicable=False,
                reason_codes=(str(exc),),
                required_capabilities=(
                    "alpha_identity_fingerprint",
                    "dual_ancestry_lineage",
                    "lean_reelaboration",
                    "nearby_signature_overlap",
                    "operator_tree",
                    "semantic_atom_delta",
                ),
            )
        if not sites:
            return Applicability(
                applicable=False,
                reason_codes=("no_curated_nearby_theorem_component",),
                required_capabilities=(
                    "alpha_identity_fingerprint",
                    "dual_ancestry_lineage",
                    "lean_reelaboration",
                    "nearby_signature_overlap",
                    "operator_tree",
                    "semantic_atom_delta",
                ),
            )
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=tuple(
                (
                    f"nearby:{site.primary_start}:{site.primary_end}:"
                    f"{site.donor_start}:{site.donor_end}:{site.entry_id}"
                )
                for site in sites
            ),
            required_capabilities=(
                "alpha_identity_fingerprint",
                "dual_ancestry_lineage",
                "lean_reelaboration",
                "nearby_signature_overlap",
                "operator_tree",
                "semantic_atom_delta",
            ),
            metadata={
                "donor_theorem_id": donor.theorem_id,
                "eligible_site_count": len(sites),
                "minimum_positional_overlap_ppm": self.config.minimum_positional_overlap_ppm,
                "primary_theorem_id": primary.theorem_id,
            },
        )

    def generate_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
        seed: int,
    ) -> Sequence[VariantDraft]:
        sites = self._sites(
            primary,
            primary_representation,
            donor,
            donor_representation,
        )
        if not sites:
            return ()
        site = _choose_site(
            sites,
            primary_id=primary.theorem_id,
            donor_id=donor.theorem_id,
            seed=seed,
        )
        primary_name = primary.declaration_name
        if primary_name is None:
            raise N10NearbyTheoremError("primary_declaration_name_missing")
        trace = _trace(
            site,
            primary=primary,
            primary_representation=primary_representation,
            donor=donor,
            donor_representation=donor_representation,
            primary_name=primary_name,
            config_hash=self.rule_config_hash,
            table_hash=self.table_hash,
        )
        inverse = _inverse_trace(
            site,
            primary=primary,
            primary_representation=primary_representation,
            donor=donor,
            donor_representation=donor_representation,
            primary_name=primary_name,
            config_hash=self.rule_config_hash,
            table_hash=self.table_hash,
        )
        candidate = apply_nearby_theorem_trace(
            primary.proof_stripped_declaration,
            trace,
            expected_config_hash=self.rule_config_hash,
            expected_table_hash=self.table_hash,
        )
        if (
            apply_nearby_theorem_trace(
                candidate,
                inverse,
                expected_config_hash=self.rule_config_hash,
                expected_table_hash=self.table_hash,
            )
            != primary.proof_stripped_declaration
        ):
            raise N10NearbyTheoremError("n10_internal_round_trip_failure")
        source_ids, representation_ids = _source_pairs(
            primary,
            primary_representation,
            donor,
            donor_representation,
        )
        return (
            build_variant_draft(
                source_theorem_ids=source_ids,
                source_representation_ids=representation_ids,
                context_id=primary.context_id,
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
                expected_atom_mapping=dict(zip(site.source_atoms, site.target_atoms, strict=True)),
                expected_structural_diff=_expected_structural_diff(
                    site,
                    primary=primary,
                    donor=donor,
                    config_hash=self.rule_config_hash,
                    table_hash=self.table_hash,
                ),
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "donor_theorem_id": donor.theorem_id,
                    "dual_ancestry_recorded": True,
                    "intention_is_not_label": True,
                    "primary_theorem_id": primary.theorem_id,
                    "semantic_negative_resolved": False,
                },
            ),
        )

    def audit_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit:
        violations: list[str] = []
        source_ids, representation_ids = _source_pairs(
            primary,
            primary_representation,
            donor,
            donor_representation,
        )
        draft_lineage_ok = (
            draft.rule_id == self.rule_id
            and draft.rule_version == self.rule_version
            and draft.family_id == self.family_id
            and draft.generation_config_hash == self.generation_config_hash
            and draft.source_theorem_ids == source_ids
            and draft.source_representation_ids == representation_ids
            and draft.metadata.get("primary_theorem_id") == primary.theorem_id
            and draft.metadata.get("donor_theorem_id") == donor.theorem_id
        )
        if not draft_lineage_ok:
            violations.append("draft_dual_source_lineage_mismatch")

        expected_parents = tuple(sorted((primary.theorem_id, donor.theorem_id)))
        expected_roots = tuple(
            sorted(set(primary.root_ancestry_ids) | set(donor.root_ancestry_ids))
        )
        candidate_lineage_ok = (
            candidate.parent_theorem_ids == expected_parents
            and candidate.root_ancestry_ids == expected_roots
        )
        if not candidate_lineage_ok:
            violations.append("candidate_dual_ancestry_lineage_mismatch")

        context_ok = (
            primary.context_id
            == primary_representation.context_id
            == donor.context_id
            == donor_representation.context_id
            == candidate.context_id
            == candidate_representation.context_id
            == draft.context_id
        )
        if not context_ok:
            violations.append("context_mismatch")
        representation_lineage_ok = (
            primary_representation.theorem_id == primary.theorem_id
            and donor_representation.theorem_id == donor.theorem_id
            and candidate_representation.theorem_id == candidate.theorem_id
        )
        if not representation_lineage_ok:
            violations.append("representation_lineage_mismatch")
        text_ok = (
            primary_representation.raw_proof_stripped == primary.proof_stripped_declaration
            and donor_representation.raw_proof_stripped == donor.proof_stripped_declaration
            and candidate.proof_stripped_declaration
            == candidate_representation.raw_proof_stripped
            == draft.candidate_code
        )
        if not text_ok:
            violations.append("source_or_candidate_representation_text_mismatch")
        candidate_identity_ok = (
            candidate.declaration_kind == primary.declaration_kind
            and candidate.declaration_name == primary.declaration_name
            and candidate.declaration_full_name == primary.declaration_full_name
        )
        if not candidate_identity_ok:
            violations.append("candidate_primary_declaration_identity_mismatch")

        all_elaborate = all(
            theorem.elaboration_status in _VALID_ELABORATION
            for theorem in (primary, donor, candidate)
        )
        if not all_elaborate:
            violations.append("source_donor_or_candidate_does_not_elaborate")
        required = ("signature_explicit", "semantic_atoms", "operator_tree")
        all_views_ok = all(
            representation.view_status[name] == ViewStatus.OK
            for representation in (
                primary_representation,
                donor_representation,
                candidate_representation,
            )
            for name in required
        ) and all(
            representation.alpha_identity_fingerprint is not None
            for representation in (
                primary_representation,
                donor_representation,
                candidate_representation,
            )
        )
        if not all_views_ok:
            violations.append("required_view_failed")

        matching_site: NearbyTheoremSite | None = None
        try:
            matches = tuple(
                site
                for site in enumerate_nearby_theorem_sites(
                    primary,
                    primary_representation,
                    donor,
                    donor_representation,
                    self.config,
                    self.table,
                    self.nearby_entries,
                )
                if _trace(
                    site,
                    primary=primary,
                    primary_representation=primary_representation,
                    donor=donor,
                    donor_representation=donor_representation,
                    primary_name=primary.declaration_name or "",
                    config_hash=self.rule_config_hash,
                    table_hash=self.table_hash,
                )
                == draft.transformation_trace
                and _inverse_trace(
                    site,
                    primary=primary,
                    primary_representation=primary_representation,
                    donor=donor,
                    donor_representation=donor_representation,
                    primary_name=primary.declaration_name or "",
                    config_hash=self.rule_config_hash,
                    table_hash=self.table_hash,
                )
                == draft.inverse_trace
            )
            if len(matches) == 1:
                matching_site = matches[0]
        except N10NearbyTheoremError:
            matching_site = None
        exact_trace_ok = matching_site is not None
        if not exact_trace_ok:
            violations.append("trace_not_from_current_pair_and_table")

        try:
            forward_ok = (
                apply_nearby_theorem_trace(
                    primary.proof_stripped_declaration,
                    draft.transformation_trace,
                    expected_config_hash=self.rule_config_hash,
                    expected_table_hash=self.table_hash,
                )
                == draft.candidate_code
            )
        except N10NearbyTheoremError:
            forward_ok = False
        if not forward_ok:
            violations.append("forward_trace_failed")
        try:
            roundtrip_ok = (
                draft.inverse_trace is not None
                and apply_nearby_theorem_trace(
                    draft.candidate_code,
                    draft.inverse_trace,
                    expected_config_hash=self.rule_config_hash,
                    expected_table_hash=self.table_hash,
                )
                == primary.proof_stripped_declaration
            )
        except N10NearbyTheoremError:
            roundtrip_ok = False
        if not roundtrip_ok:
            violations.append("inverse_roundtrip_failed")

        expected_diff_ok = (
            matching_site is not None
            and draft.expected_structural_diff
            == _expected_structural_diff(
                matching_site,
                primary=primary,
                donor=donor,
                config_hash=self.rule_config_hash,
                table_hash=self.table_hash,
            )
            and draft.expected_atom_mapping
            == dict(
                zip(
                    matching_site.source_atoms,
                    matching_site.target_atoms,
                    strict=True,
                )
            )
            and draft.intended_error_types == matching_site.intended_error_types
            and draft.intended_relation == IntendedRelation.NEAR_MISS
        )
        if not expected_diff_ok:
            violations.append("expected_pair_diff_mismatch")

        atom_mapping_ok = False
        if (
            matching_site is not None
            and primary_representation.semantic_atoms is not None
            and donor_representation.semantic_atoms is not None
            and candidate_representation.semantic_atoms is not None
        ):
            replacement = _replacement_by_id(
                self.table,
                matching_site.replacement_entry_id,
            )
            try:
                expected_atoms = _expected_atoms(
                    primary_representation.semantic_atoms,
                    replacement,
                )
            except N10NearbyTheoremError:
                expected_atoms = Counter()
            atom_mapping_ok = (
                Counter(donor_representation.semantic_atoms) == expected_atoms
                and Counter(candidate_representation.semantic_atoms) == expected_atoms
            )
        if not atom_mapping_ok:
            violations.append("semantic_atom_delta_or_donor_match_failed")

        candidate_matches_donor = (
            primary_representation.alpha_identity_fingerprint
            != donor_representation.alpha_identity_fingerprint
            and candidate_representation.alpha_identity_fingerprint
            == donor_representation.alpha_identity_fingerprint
            and candidate_representation.signature_explicit
            == donor_representation.signature_explicit
            and candidate_representation.operator_tree == donor_representation.operator_tree
        )
        if not candidate_matches_donor:
            violations.append("candidate_does_not_match_donor_signature")
        if draft.candidate_code == primary.proof_stripped_declaration:
            violations.append("candidate_unchanged")

        clean = not violations
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("n10_exact_nearby_theorem_component",),
                required_capabilities=(
                    "alpha_identity_fingerprint",
                    "dual_ancestry_lineage",
                    "lean_reelaboration",
                    "nearby_signature_overlap",
                    "operator_tree",
                    "semantic_atom_delta",
                ),
                metadata={
                    "donor_theorem_id": donor.theorem_id,
                    "primary_theorem_id": primary.theorem_id,
                },
            ),
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=(
                candidate.elaboration_status
                if all_elaborate and clean
                else ValidationStatus.QUARANTINED
            ),
            recommended_quality_tier=(QualityTier.PROVISIONAL if clean else QualityTier.UNKNOWN),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=(
                exact_trace_ok
                and forward_ok
                and roundtrip_ok
                and expected_diff_ok
                and candidate_matches_donor
                and candidate_identity_ok
                and candidate_lineage_ok
            ),
            atom_mapping_ok=atom_mapping_ok,
            inverse_or_roundtrip_ok=roundtrip_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "candidate_matches_donor_signature": candidate_matches_donor,
                "context_equal": context_ok,
                "dual_ancestry_lineage_ok": candidate_lineage_ok,
                "failed_proof_search_used": False,
                "intention_is_not_label": True,
                "semantic_negative_resolved": False,
            },
        )


__all__ = [
    "LoadedN10NearbyTheoremConfig",
    "N10NearbyReplacementEntry",
    "N10NearbyTheoremConfig",
    "N10NearbyTheoremError",
    "N10NearbyTheoremRule",
    "NearbyTheoremSite",
    "apply_nearby_theorem_trace",
    "enumerate_nearby_theorem_sites",
    "load_n10_nearby_theorem_config",
]
