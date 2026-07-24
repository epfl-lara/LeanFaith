"""N01 finite, type-aware operator replacement.

N01 is a *candidate generator*, not a negative-label oracle.  It applies one
entry from a strict, versioned replacement table to exactly one lexical
operator site in a theorem/lemma proposition.  Applicability is gated by the
source's elaborated semantic atoms, and the audit requires the candidate's
exact expected atom delta after same-context Lean elaboration.

No proof search result is consumed here.  In particular, failed proof search
can never promote an N01 candidate.  Mechanically valid outputs remain
``provisional``; every drift or missing invariant is quarantined as
``unknown``.
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
_MUTABLE_TOKENS = frozenset({"<", "≤", "∧", _LOGICAL_OR})


class N01OperatorError(ValueError):
    """N01 configuration, parsing, replay, or audit failed closed."""


class N01ReplacementEntry(StrictModel):
    """One directed, finite replacement admitted by the v1 table."""

    entry_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
    family_id: Literal["n01_operator"]
    source_token: NonEmptyStr
    target_token: NonEmptyStr
    type_precondition: Literal[
        "nat_binary_order_relation",
        "prop_binary_connective",
    ]
    source_atoms: tuple[NonEmptyStr, ...]
    target_atoms: tuple[NonEmptyStr, ...]
    required_context_atoms: tuple[NonEmptyStr, ...] = ()
    intended_error_types: tuple[ErrorCode, ...]

    @model_validator(mode="after")
    def _finite_and_canonical(self) -> N01ReplacementEntry:
        if self.source_token == self.target_token:
            raise ValueError("source_token and target_token must differ")
        if self.source_token not in _MUTABLE_TOKENS:
            raise ValueError(f"source token {self.source_token!r} is outside N01 v1")
        if self.target_token not in _MUTABLE_TOKENS:
            raise ValueError(f"target token {self.target_token!r} is outside N01 v1")
        for field_name in (
            "source_atoms",
            "target_atoms",
            "required_context_atoms",
            "intended_error_types",
        ):
            values = getattr(self, field_name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{field_name} must be sorted and unique")
        if not self.source_atoms or not self.target_atoms:
            raise ValueError("source_atoms and target_atoms must be nonempty")
        if len(self.source_atoms) != len(self.target_atoms):
            raise ValueError("source_atoms and target_atoms must have equal length")
        if not self.intended_error_types:
            raise ValueError("intended_error_types must be nonempty")
        if set(self.source_atoms) & set(self.target_atoms):
            raise ValueError("source_atoms and target_atoms must be disjoint")
        return self


class N01ReplacementTable(StrictModel):
    """Versioned replacement inventory shared by scoped mutation families."""

    schema_version: Literal[1] = 1
    table_id: Literal["replacement_table_v1"]
    table_version: SemanticVersion
    entries: tuple[N01ReplacementEntry, ...]
    # N10 consumes this separately typed extension from the same curated
    # replacement-table artifact. N01 deliberately treats the entries as
    # opaque JSON and continues to dispatch only ``entries`` above, preserving
    # the existing N01 execution surface.
    nearby_theorem_entries: tuple[dict[str, JsonValue], ...] = ()

    @model_validator(mode="after")
    def _inventory(self) -> N01ReplacementTable:
        if not self.entries:
            raise ValueError("replacement table cannot be empty")
        ids = tuple(entry.entry_id for entry in self.entries)
        if len(ids) != len(set(ids)):
            raise ValueError("replacement-table entry_id values must be unique")
        directed = tuple(
            (entry.source_token, entry.target_token, entry.type_precondition)
            for entry in self.entries
        )
        if len(directed) != len(set(directed)):
            raise ValueError("replacement-table directed entries must be unique")
        nearby_ids: list[str] = []
        for raw in self.nearby_theorem_entries:
            entry_id = raw.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id:
                raise ValueError(
                    "every nearby_theorem_entries item requires a nonempty string entry_id"
                )
            nearby_ids.append(entry_id)
        if len(nearby_ids) != len(set(nearby_ids)):
            raise ValueError("nearby-theorem replacement entry_id values must be unique")
        return self


class N01OperatorConfig(StrictModel):
    """Versioned, fail-closed N01 execution policy."""

    schema_version: Literal[1] = 1
    rule_id: Literal["n01_operator"] = "n01_operator"
    rule_version: SemanticVersion
    family_id: Literal["n01_operator"] = "n01_operator"
    implementation_key: Literal["n01_operator"] = "n01_operator"
    candidate_pool: NonEmptyStr
    replacement_table_path: NonEmptyStr
    supported_declaration_kinds: tuple[Literal["lemma", "theorem"], ...]
    placeholder_forms: tuple[Literal["by_sorry", "sorry"], ...]
    require_exactly_one_site: Literal[True] = True
    require_source_candidate_elaboration: Literal[True] = True
    failed_proof_search_is_negative_evidence: Literal[False] = False

    @model_validator(mode="after")
    def _closed_scope(self) -> N01OperatorConfig:
        if self.supported_declaration_kinds != ("lemma", "theorem"):
            raise ValueError("supported_declaration_kinds must be exactly [lemma, theorem]")
        if self.placeholder_forms != ("by_sorry", "sorry"):
            raise ValueError("placeholder_forms must be exactly [by_sorry, sorry]")
        return self


@dataclass(frozen=True, slots=True)
class LoadedN01OperatorConfig:
    config: N01OperatorConfig
    config_hash: str
    table: N01ReplacementTable
    table_hash: str
    config_path: Path
    table_path: Path


def load_n01_operator_config(
    repo_root: Path | None = None,
    *,
    config_path: Path | None = None,
    table_path: Path | None = None,
) -> LoadedN01OperatorConfig:
    """Load N01 config and bind its declared replacement table."""

    root = find_repo_root(repo_root).resolve()
    resolved_config = (config_path or root / "configs/transformations/n01_operator.yaml").resolve()
    if not resolved_config.is_relative_to(root):
        raise N01OperatorError("n01 config path escapes the repository")
    loaded_config: LoadedConfig[N01OperatorConfig] = load_config(
        resolved_config,
        N01OperatorConfig,
    )
    declared_table = (root / loaded_config.config.replacement_table_path).resolve()
    if not declared_table.is_relative_to(root):
        raise N01OperatorError("n01 replacement-table path escapes the repository")
    resolved_table = (table_path or declared_table).resolve()
    if resolved_table != declared_table:
        raise N01OperatorError("n01 table override must equal the config-declared table path")
    loaded_table: LoadedConfig[N01ReplacementTable] = load_config(
        resolved_table,
        N01ReplacementTable,
    )
    return LoadedN01OperatorConfig(
        config=loaded_config.config,
        config_hash=loaded_config.config_hash,
        table=loaded_table.config,
        table_hash=loaded_table.config_hash,
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
class OperatorSite:
    """One source-text site backed by one replacement-table entry."""

    entry_id: str
    start: int
    end: int
    source_token: str
    target_token: str
    type_precondition: str
    intended_error_types: tuple[str, ...]

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "entry_id": self.entry_id,
                "start": self.start,
                "end": self.end,
                "source_token": self.source_token,
                "target_token": self.target_token,
                "type_precondition": self.type_precondition,
                "intended_error_types": self.intended_error_types,
            }
        )


def _lex_lean(source: str) -> tuple[_Token, ...]:
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
                raise N01OperatorError("unterminated_block_comment")
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
                raise N01OperatorError("unterminated_string_literal")
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
                raise N01OperatorError("unterminated_character_literal")
            tokens.append(_Token(start, index, source[start:index], "char"))
            continue
        if char == "«":
            start = index
            end = source.find("»", index + 1)
            if end < 0:
                raise N01OperatorError("unterminated_guillemet_identifier")
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
                    "≤",
                    "∧",
                    _LOGICAL_OR,
                    "<",
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


def _statement_bounds(tokens: Sequence[_Token]) -> tuple[int, int]:
    code = tuple(token for token in tokens if token.kind != "comment")
    stack: list[str] = []
    keyword_index: int | None = None
    for index, token in enumerate(code):
        if token.text in _OPEN_TO_CLOSE:
            stack.append(token.text)
        elif token.text in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[token.text]:
                raise N01OperatorError("mismatched_declaration_delimiter")
            stack.pop()
        elif not stack and token.text in _DECLARATION_KEYWORDS:
            keyword_index = index
            break
    if keyword_index is None or keyword_index + 1 >= len(code):
        raise N01OperatorError("unsupported_declaration_kind")

    result_colon: _Token | None = None
    assignments: list[_Token] = []
    stack = []
    for token in code[keyword_index + 2 :]:
        if token.text in _OPEN_TO_CLOSE:
            stack.append(token.text)
        elif token.text in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[token.text]:
                raise N01OperatorError("mismatched_declaration_delimiter")
            stack.pop()
        elif not stack and token.text == ":" and result_colon is None:
            result_colon = token
        elif not stack and token.text == ":=" and result_colon is not None:
            assignments.append(token)
    if result_colon is None:
        raise N01OperatorError("missing_result_colon")
    if not assignments:
        raise N01OperatorError("missing_proof_assignment")
    proof_assignment = assignments[-1]
    if proof_assignment.start <= result_colon.end:
        raise N01OperatorError("invalid_statement_bounds")
    return result_colon.end, proof_assignment.start


def _entry_precondition_holds(
    entry: N01ReplacementEntry,
    atoms: Sequence[str],
) -> bool:
    counts = Counter(atoms)
    source_counts = Counter(entry.source_atoms)
    for atom, required in source_counts.items():
        # Exclusive source-operator atoms prevent a site from being
        # ambiguously associated with another overloaded occurrence.
        if counts[atom] != required:
            return False
    required_context = Counter(entry.required_context_atoms)
    return all(counts[atom] >= required for atom, required in required_context.items())


def enumerate_operator_sites(
    source: str,
    semantic_atoms: Sequence[str],
    table: N01ReplacementTable,
) -> tuple[OperatorSite, ...]:
    """Enumerate sites whose lexical token and elaborated precondition agree."""

    tokens = _lex_lean(source)
    statement_start, statement_end = _statement_bounds(tokens)
    sites: list[OperatorSite] = []
    for entry in table.entries:
        if not _entry_precondition_holds(entry, semantic_atoms):
            continue
        matching = tuple(
            token
            for token in tokens
            if token.kind == "symbol"
            and token.text == entry.source_token
            and statement_start <= token.start
            and token.end <= statement_end
        )
        if len(matching) != 1:
            continue
        token = matching[0]
        sites.append(
            OperatorSite(
                entry_id=entry.entry_id,
                start=token.start,
                end=token.end,
                source_token=entry.source_token,
                target_token=entry.target_token,
                type_precondition=entry.type_precondition,
                intended_error_types=entry.intended_error_types,
            )
        )
    return tuple(sorted(sites, key=lambda site: (site.start, site.entry_id)))


def apply_operator_trace(
    source: str,
    trace: Sequence[Mapping[str, object]],
) -> str:
    """Apply one exact N01 site edit and reject all trace drift."""

    if len(trace) != 1:
        raise N01OperatorError("n01_trace_must_have_exactly_one_step")
    step = trace[0]
    if step.get("operation") != "replace_exact_operator":
        raise N01OperatorError("unsupported_n01_trace_operation")
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
        raise N01OperatorError("malformed_n01_trace")
    if start < 0 or end < start or end > len(source):
        raise N01OperatorError("n01_trace_span_out_of_bounds")
    if source[start:end] != expected:
        raise N01OperatorError("n01_trace_expected_text_mismatch")
    return source[:start] + replacement + source[end:]


def _choose_site(
    sites: Sequence[OperatorSite],
    *,
    theorem_id: str,
    seed: int,
) -> OperatorSite:
    if not sites:
        raise N01OperatorError("no_type_compatible_operator_site")

    def rank(site: OperatorSite) -> bytes:
        payload = f"n01_operator_v1\0{theorem_id}\0{seed}\0{site.stable_key}"
        return hashlib.sha256(payload.encode("utf-8")).digest()

    return min(sites, key=rank)


def _entry_by_id(
    table: N01ReplacementTable,
    entry_id: str,
) -> N01ReplacementEntry:
    for entry in table.entries:
        if entry.entry_id == entry_id:
            return entry
    raise N01OperatorError(f"unknown replacement entry {entry_id!r}")


def _trace(
    site: OperatorSite,
    *,
    config_hash: str,
    table_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "replace_exact_operator",
            "entry_id": site.entry_id,
            "start": site.start,
            "end": site.end,
            "expected_text": site.source_token,
            "replacement_text": site.target_token,
            "type_precondition": site.type_precondition,
            "rule_config_hash": config_hash,
            "replacement_table_hash": table_hash,
        },
    )


def _inverse_trace(
    site: OperatorSite,
    *,
    config_hash: str,
    table_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "replace_exact_operator",
            "entry_id": site.entry_id,
            "start": site.start,
            "end": site.start + len(site.target_token),
            "expected_text": site.target_token,
            "replacement_text": site.source_token,
            "type_precondition": site.type_precondition,
            "rule_config_hash": config_hash,
            "replacement_table_hash": table_hash,
        },
    )


def _expected_diff(
    site: OperatorSite,
    *,
    config_hash: str,
    table_hash: str,
) -> dict[str, JsonValue]:
    return {
        "operation": "replace_exact_operator",
        "entry_id": site.entry_id,
        "source_span_start": site.start,
        "source_span_end": site.end,
        "source_token": site.source_token,
        "target_token": site.target_token,
        "type_precondition": site.type_precondition,
        "exact_site_count": 1,
        "rule_config_hash": config_hash,
        "replacement_table_hash": table_hash,
    }


def _expected_candidate_atoms(
    source_atoms: Sequence[str],
    entry: N01ReplacementEntry,
) -> Counter[str]:
    expected = Counter(source_atoms)
    for atom in entry.source_atoms:
        expected[atom] -= 1
        if expected[atom] < 0:
            raise N01OperatorError("source_atom_precondition_underflow")
        if expected[atom] == 0:
            del expected[atom]
    expected.update(entry.target_atoms)
    return expected


class N01OperatorRule:
    """Finite type-aware N01 rule; every output is provisional."""

    rule_id = "n01_operator"
    family_id = "n01_operator"
    polarity = Polarity.NEGATIVE
    implementation_key = "n01_operator"

    def __init__(
        self,
        *,
        generation_config_hash: str,
        config: N01OperatorConfig,
        rule_config_hash: str,
        table: N01ReplacementTable,
        table_hash: str,
    ) -> None:
        for field_name, digest in (
            ("generation_config_hash", generation_config_hash),
            ("rule_config_hash", rule_config_hash),
            ("table_hash", table_hash),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise N01OperatorError(f"{field_name} must be a SHA-256 hex digest")
        self.generation_config_hash = generation_config_hash
        self.config = config
        self.rule_config_hash = rule_config_hash
        self.table = table
        self.table_hash = table_hash
        self.rule_version = config.rule_version
        self.audit_config_hash = hash_canonical(
            {
                "rule_config_hash": rule_config_hash,
                "replacement_table_hash": table_hash,
                "generation_config_hash": generation_config_hash,
                "certificate": "exact_site_elaborated_atom_delta_v1",
                "proof_search_semantics": "none",
            }
        )

    @classmethod
    def from_repository(
        cls,
        *,
        generation_config_hash: str,
        repo_root: Path | None = None,
    ) -> N01OperatorRule:
        loaded = load_n01_operator_config(repo_root)
        return cls(
            generation_config_hash=generation_config_hash,
            config=loaded.config,
            rule_config_hash=loaded.config_hash,
            table=loaded.table,
            table_hash=loaded.table_hash,
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
        if representation.semantic_atoms is None:
            return Applicability(
                applicable=False,
                reason_codes=("missing_semantic_atoms",),
                required_capabilities=("lean_reelaboration", "semantic_atom_delta"),
            )
        if representation.alpha_identity_fingerprint is None:
            return Applicability(
                applicable=False,
                reason_codes=("missing_alpha_identity_fingerprint",),
                required_capabilities=("lean_reelaboration", "semantic_atom_delta"),
            )
        try:
            sites = enumerate_operator_sites(
                theorem.proof_stripped_declaration,
                representation.semantic_atoms,
                self.table,
            )
        except N01OperatorError as exc:
            return Applicability(
                applicable=False,
                reason_codes=(str(exc),),
                required_capabilities=("lean_reelaboration", "semantic_atom_delta"),
            )
        if not sites:
            return Applicability(
                applicable=False,
                reason_codes=("no_type_compatible_operator_site",),
                required_capabilities=("lean_reelaboration", "semantic_atom_delta"),
            )
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=tuple(
                f"operator:{site.start}:{site.end}:{site.entry_id}" for site in sites
            ),
            required_capabilities=("lean_reelaboration", "semantic_atom_delta"),
            metadata={"eligible_site_count": len(sites)},
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
        assert representation.semantic_atoms is not None
        sites = enumerate_operator_sites(
            theorem.proof_stripped_declaration,
            representation.semantic_atoms,
            self.table,
        )
        site = _choose_site(sites, theorem_id=theorem.theorem_id, seed=seed)
        entry = _entry_by_id(self.table, site.entry_id)
        trace = _trace(
            site,
            config_hash=self.rule_config_hash,
            table_hash=self.table_hash,
        )
        inverse = _inverse_trace(
            site,
            config_hash=self.rule_config_hash,
            table_hash=self.table_hash,
        )
        candidate = apply_operator_trace(theorem.proof_stripped_declaration, trace)
        if apply_operator_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise N01OperatorError("n01_internal_round_trip_failure")
        atom_mapping = dict(zip(entry.source_atoms, entry.target_atoms, strict=True))
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
                intended_error_types=entry.intended_error_types,
                candidate_pool=self.config.candidate_pool,
                transformation_trace=trace,
                inverse_trace=inverse,
                expected_atom_mapping=atom_mapping,
                expected_structural_diff=_expected_diff(
                    site,
                    config_hash=self.rule_config_hash,
                    table_hash=self.table_hash,
                ),
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "entry_id": entry.entry_id,
                    "eligible_site_count": len(sites),
                    "semantic_negative_established": False,
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
        lineage_ok = (
            draft.family_id == self.family_id
            and draft.rule_id == self.rule_id
            and draft.rule_version == self.rule_version
            and draft.generation_config_hash == self.generation_config_hash
            and draft.source_theorem_ids == (source.theorem_id,)
            and draft.source_representation_ids == (source_representation.representation_id,)
            and source_representation.theorem_id == source.theorem_id
            and candidate_representation.theorem_id == candidate.theorem_id
        )
        if not lineage_ok:
            violations.append("lineage_mismatch")
        context_ok = (
            source.context_id
            == source_representation.context_id
            == candidate.context_id
            == candidate_representation.context_id
            == draft.context_id
        )
        if not context_ok:
            violations.append("context_mismatch")
        text_ok = (
            source_representation.raw_proof_stripped == source.proof_stripped_declaration
            and candidate.proof_stripped_declaration == draft.candidate_code
            and candidate_representation.raw_proof_stripped == candidate.proof_stripped_declaration
        )
        if not text_ok:
            violations.append("representation_text_mismatch")
        elaboration_ok = (
            source.elaboration_status in _VALID_ELABORATION
            and candidate.elaboration_status in _VALID_ELABORATION
        )
        if not elaboration_ok:
            violations.append("source_or_candidate_does_not_elaborate")
        view_ok = (
            source_representation.view_status["semantic_atoms"] == ViewStatus.OK
            and source_representation.view_status["operator_tree"] == ViewStatus.OK
            and candidate_representation.view_status["semantic_atoms"] == ViewStatus.OK
            and candidate_representation.view_status["operator_tree"] == ViewStatus.OK
        )
        if not view_ok:
            violations.append("required_view_failed")

        matching_site: OperatorSite | None = None
        entry: N01ReplacementEntry | None = None
        if source_representation.semantic_atoms is not None:
            try:
                sites = enumerate_operator_sites(
                    source.proof_stripped_declaration,
                    source_representation.semantic_atoms,
                    self.table,
                )
                matches = tuple(
                    site
                    for site in sites
                    if _trace(
                        site,
                        config_hash=self.rule_config_hash,
                        table_hash=self.table_hash,
                    )
                    == draft.transformation_trace
                    and _inverse_trace(
                        site,
                        config_hash=self.rule_config_hash,
                        table_hash=self.table_hash,
                    )
                    == draft.inverse_trace
                )
                if len(matches) == 1:
                    matching_site = matches[0]
                    entry = _entry_by_id(self.table, matching_site.entry_id)
            except N01OperatorError:
                matching_site = None
        if matching_site is None or entry is None:
            violations.append("unrecognized_or_tampered_site")

        try:
            forward_ok = (
                apply_operator_trace(
                    source.proof_stripped_declaration,
                    draft.transformation_trace,
                )
                == draft.candidate_code
            )
            roundtrip_ok = (
                draft.inverse_trace is not None
                and apply_operator_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except N01OperatorError:
            forward_ok = False
            roundtrip_ok = False
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not roundtrip_ok:
            violations.append("inverse_roundtrip_failed")

        structural_diff_ok = (
            matching_site is not None
            and draft.expected_structural_diff
            == _expected_diff(
                matching_site,
                config_hash=self.rule_config_hash,
                table_hash=self.table_hash,
            )
            and forward_ok
            and roundtrip_ok
            and draft.candidate_code != source.proof_stripped_declaration
        )
        if not structural_diff_ok:
            violations.append("structural_diff_mismatch")

        atom_delta_ok = False
        if (
            entry is not None
            and source_representation.semantic_atoms is not None
            and candidate_representation.semantic_atoms is not None
        ):
            expected_mapping = dict(zip(entry.source_atoms, entry.target_atoms, strict=True))
            atom_delta_ok = (
                draft.expected_atom_mapping == expected_mapping
                and Counter(candidate_representation.semantic_atoms)
                == _expected_candidate_atoms(
                    source_representation.semantic_atoms,
                    entry,
                )
                and draft.intended_error_types == entry.intended_error_types
                and draft.intended_relation == IntendedRelation.NEAR_MISS
            )
        if not atom_delta_ok:
            violations.append("semantic_atom_delta_mismatch")

        alpha_changed = (
            source_representation.alpha_identity_fingerprint is not None
            and candidate_representation.alpha_identity_fingerprint is not None
            and source_representation.alpha_identity_fingerprint
            != candidate_representation.alpha_identity_fingerprint
        )
        if not alpha_changed:
            violations.append("alpha_identity_not_changed")

        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("n01_exact_operator_site",),
                required_capabilities=("lean_reelaboration", "semantic_atom_delta"),
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
            atom_mapping_ok=atom_delta_ok,
            inverse_or_roundtrip_ok=roundtrip_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "alpha_identity_changed": alpha_changed,
                "candidate_elaborated_same_context": elaboration_ok and context_ok,
                "entry_id": entry.entry_id if entry is not None else None,
                "failed_proof_search_used": False,
                "semantic_negative_established": False,
            },
        )
