"""LF-017 P04-lite: finite notation/direct-form identity rewrites.

P04-lite is intentionally not a simplifier.  It recognizes only exact lexical
tokens from a versioned table, changes one seeded site, records an exact
forward/inverse span trace, and remains provisional until a same-context
LeanInteract re-elaboration proves that the elaborated declaration type is
unchanged.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

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
from leanfaith.transforms.protocol import build_transformation_audit, build_variant_draft

NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
SemanticVersion = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True)]
RewriteDirection = Literal["direct_to_notation", "notation_to_direct"]

_VALID_ELABORATION = {
    ValidationStatus.ELABORATES,
    ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
}
_PLACEHOLDER_TAILS = (("by", "sorry"), ("sorry",))


class P04NotationError(ValueError):
    """P04-lite cannot safely recognize, replay, or audit a rewrite."""


class _UnsupportedSource(P04NotationError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class P04NotationEntry(StrictModel):
    """One exact, bidirectional notation table entry."""

    entry_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
    notation: NonEmptyStr
    direct_form: NonEmptyStr
    elaborated_constant: NonEmptyStr

    @model_validator(mode="after")
    def _lexical_tokens(self) -> P04NotationEntry:
        if self.notation == self.direct_form:
            raise ValueError("notation and direct_form must differ")
        for field_name in ("notation", "direct_form"):
            token = getattr(self, field_name)
            if token.strip() != token or any(character.isspace() for character in token):
                raise ValueError(f"{field_name} must be one exact non-whitespace token")
            lexed = _tokenize(token)
            significant = tuple(item for item in lexed if item.kind != "trivia")
            if len(significant) != 1 or significant[0].kind != "identifier":
                raise ValueError(f"{field_name} must lex as exactly one unquoted identifier token")
        return self


class P04NotationConfig(StrictModel):
    """Versioned, code-owned P04-lite table and execution policy."""

    schema_version: Literal[1] = 1
    rule_id: Literal["p04_notation_lite"] = "p04_notation_lite"
    rule_version: SemanticVersion
    family_id: Literal["p04_notation_lite"] = "p04_notation_lite"
    implementation_key: Literal["p04_notation_lite"] = "p04_notation_lite"
    candidate_pool: NonEmptyStr
    supported_declaration_kinds: tuple[Literal["lemma", "theorem"], ...]
    placeholder_forms: tuple[Literal["by_sorry", "sorry"], ...]
    entries: tuple[P04NotationEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical_table(self) -> P04NotationConfig:
        if tuple(sorted(set(self.supported_declaration_kinds))) != (
            self.supported_declaration_kinds
        ):
            raise ValueError("supported_declaration_kinds must be sorted and unique")
        if tuple(sorted(set(self.placeholder_forms))) != self.placeholder_forms:
            raise ValueError("placeholder_forms must be sorted and unique")
        entry_ids = tuple(entry.entry_id for entry in self.entries)
        if entry_ids != tuple(sorted(set(entry_ids))):
            raise ValueError("entries must be sorted by unique entry_id")
        notations = tuple(entry.notation for entry in self.entries)
        direct_forms = tuple(entry.direct_form for entry in self.entries)
        if len(set(notations)) != len(notations):
            raise ValueError("notation tokens must be unique")
        if len(set(direct_forms)) != len(direct_forms):
            raise ValueError("direct-form tokens must be unique")
        if set(notations) & set(direct_forms):
            raise ValueError("notation and direct-form token sets must be disjoint")
        return self


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class NotationRewriteSite:
    """One exact token occurrence eligible for a configured P04-lite rewrite."""

    entry_id: str
    direction: RewriteDirection
    start: int
    end: int
    token_index: int
    source_token: str
    target_token: str
    elaborated_constant: str

    @property
    def stable_key(self) -> str:
        return (
            f"{self.start:010d}:{self.end:010d}:{self.token_index:06d}:"
            f"{self.entry_id}:{self.direction}"
        )


def load_p04_notation_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[P04NotationConfig]:
    """Load the strict P04-lite table from the repository."""

    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/p04_notation_lite.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise P04NotationError("p04 config path escapes the repository")
    return load_config(resolved, P04NotationConfig)


def notation_table_hash(config: P04NotationConfig) -> str:
    """Hash exactly the ordered notation entries, independently of other policy."""

    return hash_canonical(
        {
            "schema_version": config.schema_version,
            "entries": [entry.model_dump(mode="json") for entry in config.entries],
        }
    )


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
    """Tokenize only the lexical distinctions required by P04-lite.

    Comments, strings, character literals, and guillemet identifiers are
    emitted as opaque tokens.  Therefore table text inside them can never
    become a rewrite site.
    """

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
        if _is_identifier_start(character):
            index += 1
            while index < len(source) and _is_identifier_continue(source[index]):
                index += 1
            tokens.append(_Token("identifier", source[start:index], start, index))
            continue
        for operator in (":=", "=>", "->", "⦃", "⦄"):
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


def _signature_token_bounds(
    tokens: Sequence[_Token],
    config: P04NotationConfig,
) -> tuple[int, int]:
    significant = _significant(tokens)
    if len(significant) < 4:
        raise _UnsupportedSource("unsupported_declaration_shape")
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
    name_token = significant[declaration_position + 1][1]
    if name_token.kind not in {"identifier", "quoted_identifier"}:
        raise _UnsupportedSource("unsupported_declaration_name")

    assignment_position: int | None = None
    placeholder_form: str | None = None
    for position in range(declaration_position + 2, len(significant)):
        if significant[position][1].text != ":=":
            continue
        tail = tuple(token.text for _, token in significant[position + 1 :])
        if tail == ("by", "sorry") and "by_sorry" in config.placeholder_forms:
            assignment_position = position
            placeholder_form = "by_sorry"
        elif tail == ("sorry",) and "sorry" in config.placeholder_forms:
            assignment_position = position
            placeholder_form = "sorry"
    if assignment_position is None or placeholder_form is None:
        raise _UnsupportedSource("unsupported_proof_placeholder")
    return declaration_position + 2, assignment_position


def enumerate_notation_sites(
    source: str,
    config: P04NotationConfig,
) -> tuple[NotationRewriteSite, ...]:
    """Enumerate exact standalone table tokens in the theorem signature."""

    tokens = _tokenize(source)
    significant = _significant(tokens)
    signature_start, signature_end = _signature_token_bounds(tokens, config)
    by_notation = {entry.notation: entry for entry in config.entries}
    by_direct = {entry.direct_form: entry for entry in config.entries}
    sites: list[NotationRewriteSite] = []
    for position in range(signature_start, signature_end):
        token_index, token = significant[position]
        if token.kind != "identifier":
            continue
        entry = by_notation.get(token.text)
        direction: RewriteDirection
        if entry is not None:
            direction = "notation_to_direct"
            target = entry.direct_form
        else:
            entry = by_direct.get(token.text)
            if entry is None:
                continue
            direction = "direct_to_notation"
            target = entry.notation

        previous = significant[position - 1][1].text if position else None
        following = significant[position + 1][1].text if position + 1 < len(significant) else None
        # Qualified identifiers and explicit-head syntax are deliberately out
        # of the lite subset.  This also prevents rewriting `Nat` in
        # `Nat.succ`, even though the lexer correctly sees an exact token.
        if previous in {".", "@"} or following == ".":
            continue
        sites.append(
            NotationRewriteSite(
                entry_id=entry.entry_id,
                direction=direction,
                start=token.start,
                end=token.end,
                token_index=token_index,
                source_token=token.text,
                target_token=target,
                elaborated_constant=entry.elaborated_constant,
            )
        )
    return tuple(sorted(sites, key=lambda site: site.stable_key))


def _choose_site(
    sites: Sequence[NotationRewriteSite],
    *,
    theorem_id: str,
    seed: int,
) -> NotationRewriteSite:
    if not sites:
        raise P04NotationError("no_approved_notation_site")

    def rank(site: NotationRewriteSite) -> bytes:
        payload = f"p04-select-v1\0{theorem_id}\0{seed}\0{site.stable_key}"
        return hashlib.sha256(payload.encode("utf-8")).digest()

    return min(sites, key=rank)


def _trace(
    *,
    source: str,
    site: NotationRewriteSite,
    table_hash: str,
) -> tuple[dict[str, object], ...]:
    return (
        {
            "direction": site.direction,
            "elaborated_constant": site.elaborated_constant,
            "end": site.end,
            "entry_id": site.entry_id,
            "expected_text": site.source_token,
            "input_code_hash": sha256_hex(source.encode("utf-8")),
            "operation": "replace_notation_token_exact",
            "replacement_text": site.target_token,
            "start": site.start,
            "table_hash": table_hash,
            "token_index": site.token_index,
            "token_hash": sha256_hex(site.source_token.encode("utf-8")),
        },
    )


def _inverse_trace(
    *,
    candidate: str,
    site: NotationRewriteSite,
    table_hash: str,
) -> tuple[dict[str, object], ...]:
    inverse_direction: RewriteDirection = (
        "notation_to_direct" if site.direction == "direct_to_notation" else "direct_to_notation"
    )
    return (
        {
            "direction": inverse_direction,
            "elaborated_constant": site.elaborated_constant,
            "end": site.start + len(site.target_token),
            "entry_id": site.entry_id,
            "expected_text": site.target_token,
            "input_code_hash": sha256_hex(candidate.encode("utf-8")),
            "operation": "replace_notation_token_exact",
            "replacement_text": site.source_token,
            "start": site.start,
            "table_hash": table_hash,
            "token_index": site.token_index,
            "token_hash": sha256_hex(site.target_token.encode("utf-8")),
        },
    )


def apply_notation_trace(
    source: str,
    trace: Sequence[Mapping[str, object]],
    *,
    expected_table_hash: str | None = None,
) -> str:
    """Apply a nonempty exact P04 trace and fail closed on any source drift."""

    if not trace:
        raise P04NotationError("empty_notation_trace")
    result = source
    for item in trace:
        if item.get("operation") != "replace_notation_token_exact":
            raise P04NotationError("unexpected_trace_operation")
        if expected_table_hash is not None and item.get("table_hash") != expected_table_hash:
            raise P04NotationError("trace_table_hash_mismatch")
        input_hash = item.get("input_code_hash")
        if input_hash != sha256_hex(result.encode("utf-8")):
            raise P04NotationError("trace_input_code_hash_mismatch")
        start = item.get("start")
        end = item.get("end")
        expected = item.get("expected_text")
        replacement = item.get("replacement_text")
        token_hash = item.get("token_hash")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(expected, str)
            or not isinstance(replacement, str)
            or not isinstance(token_hash, str)
        ):
            raise P04NotationError("malformed_notation_trace")
        if not 0 <= start <= end <= len(result):
            raise P04NotationError("trace_span_out_of_bounds")
        if result[start:end] != expected:
            raise P04NotationError("trace_expected_text_mismatch")
        if token_hash != sha256_hex(expected.encode("utf-8")):
            raise P04NotationError("trace_token_hash_mismatch")
        result = result[:start] + replacement + result[end:]
    return result


class P04NotationLiteRule:
    """One-site seeded notation rewrite with exact elaborated identity audit."""

    rule_id = "p04_notation_lite"
    family_id = "p04_notation_lite"
    polarity = Polarity.POSITIVE
    implementation_key = "p04_notation_lite"

    def __init__(
        self,
        *,
        generation_config_hash: str,
        config: P04NotationConfig,
        rule_config_hash: str,
    ) -> None:
        if len(generation_config_hash) != 64:
            raise P04NotationError("generation_config_hash must be a SHA-256 hex digest")
        int(generation_config_hash, 16)
        self.generation_config_hash = generation_config_hash
        self.config = config
        self.rule_config_hash = rule_config_hash
        self.table_hash = notation_table_hash(config)
        self.rule_version = config.rule_version

    @classmethod
    def from_repository(
        cls,
        *,
        generation_config_hash: str,
        repo_root: Path | None = None,
    ) -> P04NotationLiteRule:
        loaded = load_p04_notation_config(repo_root)
        return cls(
            generation_config_hash=generation_config_hash,
            config=loaded.config,
            rule_config_hash=loaded.config_hash,
        )

    def _sites(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> tuple[NotationRewriteSite, ...]:
        if not theorem.is_proposition:
            raise _UnsupportedSource("source_not_proposition")
        if theorem.elaboration_status not in _VALID_ELABORATION:
            raise _UnsupportedSource("source_does_not_elaborate")
        if representation.raw_proof_stripped != theorem.proof_stripped_declaration:
            raise _UnsupportedSource("source_representation_mismatch")
        missing: list[str] = []
        if representation.alpha_identity_fingerprint is None:
            missing.append("alpha_identity_fingerprint")
        if representation.signature_explicit is None:
            missing.append("signature_explicit")
        if representation.semantic_atoms is None:
            missing.append("semantic_atoms")
        if representation.operator_tree is None:
            missing.append("operator_tree")
        if missing:
            raise _UnsupportedSource("missing_" + "_and_".join(sorted(missing)))
        sites = enumerate_notation_sites(theorem.proof_stripped_declaration, self.config)
        semantic_atoms = representation.semantic_atoms
        assert semantic_atoms is not None  # guarded above; keeps the type narrow
        atoms = set(semantic_atoms)
        sites = tuple(site for site in sites if f"const:{site.elaborated_constant}" in atoms)
        if not sites:
            raise _UnsupportedSource("no_approved_identity_notation_site")
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
                f"token:{site.token_index}:{site.start}:{site.end}:{site.entry_id}:{site.direction}"
                for site in sites
            ),
            required_capabilities=(
                "alpha_identity_fingerprint",
                "exact_notation_table",
                "lean_reelaboration",
                "operator_tree",
                "semantic_atoms",
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
            source=theorem.proof_stripped_declaration,
            site=site,
            table_hash=self.table_hash,
        )
        candidate = apply_notation_trace(
            theorem.proof_stripped_declaration,
            trace,
            expected_table_hash=self.table_hash,
        )
        inverse = _inverse_trace(candidate=candidate, site=site, table_hash=self.table_hash)
        if (
            apply_notation_trace(candidate, inverse, expected_table_hash=self.table_hash)
            != theorem.proof_stripped_declaration
        ):
            raise P04NotationError("p04_internal_round_trip_failure")
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
                inverse_trace=inverse,
                expected_atom_mapping={
                    site.elaborated_constant: site.elaborated_constant,
                },
                expected_structural_diff={
                    "direction": site.direction,
                    "elaborated_constant": site.elaborated_constant,
                    "entry_id": site.entry_id,
                    "operation": "replace_notation_token_exact",
                    "source_span_end": site.end,
                    "source_span_start": site.start,
                    "table_hash": self.table_hash,
                    "token_index": site.token_index,
                },
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "intention_is_not_label": True,
                    "rule_config_hash": self.rule_config_hash,
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
        lineage_equal = (
            draft.rule_id == self.rule_id
            and draft.rule_version == self.rule_version
            and draft.family_id == self.family_id
            and draft.generation_config_hash == self.generation_config_hash
            and draft.source_theorem_ids == (source.theorem_id,)
            and draft.source_representation_ids == (source_representation.representation_id,)
        )
        if not lineage_equal:
            violations.append("draft_lineage_mismatch")
        representation_lineage_equal = (
            source_representation.theorem_id == source.theorem_id
            and candidate_representation.theorem_id == candidate.theorem_id
        )
        if not representation_lineage_equal:
            violations.append("representation_lineage_mismatch")
        context_equal = (
            source.context_id
            == source_representation.context_id
            == candidate.context_id
            == candidate_representation.context_id
            == draft.context_id
        )
        if not context_equal:
            violations.append("context_mismatch")
        source_representation_text_equal = (
            source_representation.raw_proof_stripped == source.proof_stripped_declaration
        )
        if not source_representation_text_equal:
            violations.append("source_representation_text_mismatch")
        candidate_representation_text_equal = (
            candidate.proof_stripped_declaration
            == candidate_representation.raw_proof_stripped
            == draft.candidate_code
        )
        if not candidate_representation_text_equal:
            violations.append("candidate_code_or_representation_mismatch")
        if draft.candidate_code == source.proof_stripped_declaration:
            violations.append("candidate_unchanged")

        matching_site: NotationRewriteSite | None = None
        try:
            source_sites = self._sites(source, source_representation)
            matches = tuple(
                site
                for site in source_sites
                if _trace(
                    source=source.proof_stripped_declaration,
                    site=site,
                    table_hash=self.table_hash,
                )
                == draft.transformation_trace
                and _inverse_trace(
                    candidate=draft.candidate_code,
                    site=site,
                    table_hash=self.table_hash,
                )
                == draft.inverse_trace
            )
            if len(matches) == 1:
                matching_site = matches[0]
        except P04NotationError:
            matching_site = None
        exact_table_site = matching_site is not None
        if not exact_table_site:
            violations.append("trace_not_from_current_notation_table")

        try:
            forward_ok = (
                apply_notation_trace(
                    source.proof_stripped_declaration,
                    draft.transformation_trace,
                    expected_table_hash=self.table_hash,
                )
                == draft.candidate_code
            )
        except P04NotationError:
            forward_ok = False
        if not forward_ok:
            violations.append("forward_trace_failed")
        try:
            roundtrip_ok = (
                draft.inverse_trace is not None
                and apply_notation_trace(
                    draft.candidate_code,
                    draft.inverse_trace,
                    expected_table_hash=self.table_hash,
                )
                == source.proof_stripped_declaration
            )
        except P04NotationError:
            roundtrip_ok = False
        if not roundtrip_ok:
            violations.append("inverse_roundtrip_failed")

        expected_diff_ok = (
            matching_site is not None
            and draft.expected_structural_diff
            == {
                "direction": matching_site.direction,
                "elaborated_constant": matching_site.elaborated_constant,
                "entry_id": matching_site.entry_id,
                "operation": "replace_notation_token_exact",
                "source_span_end": matching_site.end,
                "source_span_start": matching_site.start,
                "table_hash": self.table_hash,
                "token_index": matching_site.token_index,
            }
            and draft.expected_atom_mapping
            == {
                matching_site.elaborated_constant: matching_site.elaborated_constant,
            }
        )
        if not expected_diff_ok:
            violations.append("expected_certificate_mismatch")

        source_elaborates = source.elaboration_status in _VALID_ELABORATION
        candidate_elaborates = candidate.elaboration_status in _VALID_ELABORATION
        if not source_elaborates:
            violations.append("source_not_elaborated")
        if not candidate_elaborates:
            violations.append("candidate_not_elaborated")
            direction = (
                draft.transformation_trace[0].get("direction")
                if draft.transformation_trace
                else None
            )
            if direction == "direct_to_notation":
                violations.append("target_notation_unavailable_or_invalid")

        source_required_views = (
            source_representation.view_status["signature_explicit"] == ViewStatus.OK
            and source_representation.view_status["semantic_atoms"] == ViewStatus.OK
            and source_representation.view_status["operator_tree"] == ViewStatus.OK
            and source_representation.alpha_identity_fingerprint is not None
        )
        candidate_required_views = (
            candidate_representation.view_status["signature_explicit"] == ViewStatus.OK
            and candidate_representation.view_status["semantic_atoms"] == ViewStatus.OK
            and candidate_representation.view_status["operator_tree"] == ViewStatus.OK
            and candidate_representation.alpha_identity_fingerprint is not None
        )
        if not source_required_views:
            violations.append("source_required_view_failed")
        if not candidate_required_views:
            violations.append("candidate_required_view_failed")

        alpha_equal = (
            source_representation.alpha_identity_fingerprint is not None
            and source_representation.alpha_identity_fingerprint
            == candidate_representation.alpha_identity_fingerprint
        )
        signature_equal = (
            source_representation.signature_explicit is not None
            and source_representation.signature_explicit
            == candidate_representation.signature_explicit
        )
        atoms_equal = (
            source_representation.semantic_atoms is not None
            and source_representation.semantic_atoms == candidate_representation.semantic_atoms
        )
        tree_equal = (
            source_representation.operator_tree is not None
            and source_representation.operator_tree == candidate_representation.operator_tree
        )
        for equal, reason in (
            (alpha_equal, "alpha_identity_mismatch"),
            (signature_equal, "signature_explicit_mismatch"),
            (atoms_equal, "semantic_atoms_mismatch"),
            (tree_equal, "operator_tree_mismatch"),
        ):
            if not equal:
                violations.append(reason)

        status = (
            candidate.elaboration_status
            if candidate_elaborates and candidate_required_views and not violations
            else ValidationStatus.QUARANTINED
        )
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("p04_exact_notation_token",),
                required_capabilities=(
                    "alpha_identity_fingerprint",
                    "exact_notation_table",
                    "lean_reelaboration",
                    "operator_tree",
                    "semantic_atoms",
                ),
                metadata={"table_hash": self.table_hash},
            ),
            audit_config_hash=hash_canonical(
                {
                    "rule_config_hash": self.rule_config_hash,
                    "table_hash": self.table_hash,
                    "identity_policy": "alpha_signature_atoms_operator_tree_exact_v1",
                }
            ),
            recommended_validation_status=status,
            recommended_quality_tier=(
                QualityTier.PROVISIONAL if not violations else QualityTier.UNKNOWN
            ),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=exact_table_site and forward_ok and expected_diff_ok,
            atom_mapping_ok=atoms_equal and expected_diff_ok,
            inverse_or_roundtrip_ok=roundtrip_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "alpha_identity_equal": alpha_equal,
                "context_equal": context_equal,
                "elaborated_identity_exact": (
                    alpha_equal and signature_equal and atoms_equal and tree_equal
                ),
                "intention_is_not_label": True,
                "operator_tree_equal": tree_equal,
                "rule_lineage_equal": lineage_equal,
                "signature_explicit_equal": signature_equal,
                "table_hash": self.table_hash,
            },
        )


__all__ = [
    "NotationRewriteSite",
    "P04NotationConfig",
    "P04NotationEntry",
    "P04NotationError",
    "P04NotationLiteRule",
    "apply_notation_trace",
    "enumerate_notation_sites",
    "load_p04_notation_config",
    "notation_table_hash",
]
