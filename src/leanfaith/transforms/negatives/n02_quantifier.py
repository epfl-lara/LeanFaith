"""LF-018 N02: exact Unicode quantifier mutation candidates.

The v1 scope is deliberately narrow: replace exactly one ``∀``/``∃`` token in
the proof-free theorem signature using a finite, versioned table.  A generated
draft records a near-miss *intention*, never a semantic label.  Only a
same-context candidate that independently elaborates as a proposition and
passes the exact trace/structure audit remains provisional; all other outputs
are quarantined.
"""

from __future__ import annotations

import hashlib
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
from leanfaith.transforms.protocol import build_transformation_audit, build_variant_draft

NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
SemanticVersion = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True)]
QuantifierToken = Literal["∀", "∃"]

_VALID_ELABORATION = {
    ValidationStatus.ELABORATES,
    ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
}


class N02QuantifierError(ValueError):
    """An N02 config, trace, source, or audit invariant failed closed."""


class _UnsupportedSource(N02QuantifierError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class N02QuantifierMutation(StrictModel):
    """One exact mutation direction in the finite v1 table."""

    mutation_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
    source_token: QuantifierToken
    target_token: QuantifierToken
    intended_error_types: tuple[
        Annotated[str, Field(pattern=r"^E(0[1-9]|[12][0-9]|30)$", strict=True)], ...
    ] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed_entry(self) -> N02QuantifierMutation:
        if self.source_token == self.target_token:
            raise ValueError("source_token and target_token must differ")
        if self.intended_error_types != tuple(sorted(set(self.intended_error_types))):
            raise ValueError("intended_error_types must be sorted and unique")
        return self


class N02QuantifierConfig(StrictModel):
    """Versioned fail-closed scope for N02 v1."""

    schema_version: Literal[1] = 1
    rule_id: Literal["n02_quantifier"] = "n02_quantifier"
    rule_version: SemanticVersion
    family_id: Literal["n02_quantifier"] = "n02_quantifier"
    implementation_key: Literal["n02_quantifier"] = "n02_quantifier"
    candidate_pool: NonEmptyStr
    supported_declaration_kinds: tuple[Literal["lemma", "theorem"], ...]
    placeholder_forms: tuple[Literal["by_sorry", "sorry"], ...]
    mutations: tuple[N02QuantifierMutation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed_table(self) -> N02QuantifierConfig:
        if self.supported_declaration_kinds != tuple(sorted(set(self.supported_declaration_kinds))):
            raise ValueError("supported_declaration_kinds must be sorted and unique")
        if self.placeholder_forms != tuple(sorted(set(self.placeholder_forms))):
            raise ValueError("placeholder_forms must be sorted and unique")
        mutation_ids = tuple(item.mutation_id for item in self.mutations)
        if mutation_ids != tuple(sorted(set(mutation_ids))):
            raise ValueError("mutations must be sorted by unique mutation_id")
        sources = tuple(item.source_token for item in self.mutations)
        if len(sources) != len(set(sources)):
            raise ValueError("each source quantifier may occur only once")
        by_source = {item.source_token: item for item in self.mutations}
        for item in self.mutations:
            inverse = by_source.get(item.target_token)
            if inverse is None or inverse.target_token != item.source_token:
                raise ValueError("the finite quantifier table must be exactly invertible")
        return self


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class QuantifierMutationSite:
    """One exact quantifier token inside a proof-free theorem signature."""

    mutation_id: str
    start: int
    end: int
    token_index: int
    source_token: QuantifierToken
    target_token: QuantifierToken
    intended_error_types: tuple[str, ...]

    @property
    def stable_key(self) -> str:
        return f"{self.start:010d}:{self.end:010d}:{self.token_index:06d}:{self.mutation_id}"


def load_n02_quantifier_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[N02QuantifierConfig]:
    """Load the strict N02 table from its canonical repository path."""

    root = find_repo_root(repo_root)
    resolved = (path or root / "configs/transformations/n02_quantifier.yaml").resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise N02QuantifierError("n02 config path escapes the repository")
    return load_config(resolved, N02QuantifierConfig)


def quantifier_table_hash(config: N02QuantifierConfig) -> str:
    """Hash only the ordered finite mutation table."""

    return hash_canonical(
        {
            "schema_version": config.schema_version,
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
    """Tokenize enough Lean syntax to isolate signature quantifier tokens."""

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
        for operator in (":=", "=>", "->"):
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
    config: N02QuantifierConfig,
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
    assignment_position: int | None = None
    for position in range(declaration_position + 2, len(significant)):
        if significant[position][1].text != ":=":
            continue
        tail = tuple(token.text for _, token in significant[position + 1 :])
        if (tail == ("by", "sorry") and "by_sorry" in config.placeholder_forms) or (
            tail == ("sorry",) and "sorry" in config.placeholder_forms
        ):
            assignment_position = position
    if assignment_position is None:
        raise _UnsupportedSource("unsupported_proof_placeholder")
    return declaration_position + 2, assignment_position


def enumerate_quantifier_sites(
    source: str,
    config: N02QuantifierConfig,
) -> tuple[QuantifierMutationSite, ...]:
    """Return all configured exact quantifier tokens in the signature."""

    tokens = _tokenize(source)
    significant = _significant(tokens)
    start, end = _signature_bounds(tokens, config)
    by_source = {item.source_token: item for item in config.mutations}
    sites: list[QuantifierMutationSite] = []
    for position in range(start, end):
        token_index, token = significant[position]
        if token.text not in {"∀", "∃"}:
            continue
        source_token = cast(QuantifierToken, token.text)
        mutation = by_source.get(source_token)
        if mutation is None:
            continue
        sites.append(
            QuantifierMutationSite(
                mutation_id=mutation.mutation_id,
                start=token.start,
                end=token.end,
                token_index=token_index,
                source_token=mutation.source_token,
                target_token=mutation.target_token,
                intended_error_types=mutation.intended_error_types,
            )
        )
    return tuple(sorted(sites, key=lambda site: site.stable_key))


def _choose_site(
    sites: Sequence[QuantifierMutationSite],
    *,
    theorem_id: str,
    seed: int,
) -> QuantifierMutationSite:
    if not sites:
        raise N02QuantifierError("no_supported_quantifier_site")

    def rank(site: QuantifierMutationSite) -> bytes:
        payload = f"n02-select-v1\0{theorem_id}\0{seed}\0{site.stable_key}"
        return hashlib.sha256(payload.encode("utf-8")).digest()

    return min(sites, key=rank)


def _trace(
    source: str,
    site: QuantifierMutationSite,
    *,
    table_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "replace_quantifier_token_exact",
            "mutation_id": site.mutation_id,
            "start": site.start,
            "end": site.end,
            "expected_text": site.source_token,
            "replacement_text": site.target_token,
            "token_index": site.token_index,
            "input_code_hash": sha256_hex(source.encode("utf-8")),
            "token_hash": sha256_hex(site.source_token.encode("utf-8")),
            "table_hash": table_hash,
        },
    )


def _inverse_trace(
    candidate: str,
    site: QuantifierMutationSite,
    *,
    table_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "replace_quantifier_token_exact",
            "mutation_id": f"inverse_of_{site.mutation_id}",
            "start": site.start,
            "end": site.start + len(site.target_token),
            "expected_text": site.target_token,
            "replacement_text": site.source_token,
            "token_index": site.token_index,
            "input_code_hash": sha256_hex(candidate.encode("utf-8")),
            "token_hash": sha256_hex(site.target_token.encode("utf-8")),
            "table_hash": table_hash,
        },
    )


def apply_quantifier_trace(
    source: str,
    trace: Sequence[Mapping[str, object]],
    *,
    expected_table_hash: str | None = None,
) -> str:
    """Replay one or more exact N02 trace steps and reject stale input."""

    if not trace:
        raise N02QuantifierError("empty_quantifier_trace")
    result = source
    for item in trace:
        if item.get("operation") != "replace_quantifier_token_exact":
            raise N02QuantifierError("unexpected_trace_operation")
        if expected_table_hash is not None and item.get("table_hash") != expected_table_hash:
            raise N02QuantifierError("trace_table_hash_mismatch")
        if item.get("input_code_hash") != sha256_hex(result.encode("utf-8")):
            raise N02QuantifierError("trace_input_code_hash_mismatch")
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
            raise N02QuantifierError("malformed_quantifier_trace")
        if not 0 <= start <= end <= len(result):
            raise N02QuantifierError("trace_span_out_of_bounds")
        if result[start:end] != expected:
            raise N02QuantifierError("trace_expected_text_mismatch")
        if token_hash != sha256_hex(expected.encode("utf-8")):
            raise N02QuantifierError("trace_token_hash_mismatch")
        result = result[:start] + replacement + result[end:]
    return result


class N02QuantifierRule:
    """One-site quantifier mutation whose outputs remain provisional."""

    rule_id = "n02_quantifier"
    family_id = "n02_quantifier"
    polarity = Polarity.NEGATIVE
    implementation_key = "n02_quantifier"

    def __init__(
        self,
        *,
        registry_hash: str,
        config: N02QuantifierConfig | None = None,
        rule_config_hash: str | None = None,
    ) -> None:
        if len(registry_hash) != 64:
            raise N02QuantifierError("registry_hash must be a SHA-256 hex digest")
        int(registry_hash, 16)
        if (config is None) != (rule_config_hash is None):
            raise N02QuantifierError("config and rule_config_hash must be supplied together")
        if config is None:
            loaded = load_n02_quantifier_config()
            config = loaded.config
            rule_config_hash = loaded.config_hash
        assert rule_config_hash is not None
        if len(rule_config_hash) != 64:
            raise N02QuantifierError("rule_config_hash must be a SHA-256 hex digest")
        int(rule_config_hash, 16)
        self.registry_hash = registry_hash
        self.config = config
        self.rule_config_hash = rule_config_hash
        self.table_hash = quantifier_table_hash(config)
        self.rule_version = config.rule_version
        self.audit_config_hash = hash_canonical(
            {
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "rule_config_hash": rule_config_hash,
                "registry_hash": registry_hash,
                "table_hash": self.table_hash,
                "policy": "exact_quantifier_diff_provisional_v1",
            }
        )

    @classmethod
    def from_repository(
        cls,
        *,
        registry_hash: str,
        repo_root: Path | None = None,
    ) -> N02QuantifierRule:
        loaded = load_n02_quantifier_config(repo_root)
        return cls(
            registry_hash=registry_hash,
            config=loaded.config,
            rule_config_hash=loaded.config_hash,
        )

    def _sites(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> tuple[QuantifierMutationSite, ...]:
        if not theorem.is_proposition:
            raise _UnsupportedSource("source_not_proposition")
        if theorem.elaboration_status not in _VALID_ELABORATION:
            raise _UnsupportedSource("source_does_not_elaborate")
        if representation.raw_proof_stripped != theorem.proof_stripped_declaration:
            raise _UnsupportedSource("source_representation_text_mismatch")
        required_views = (
            representation.alpha_identity_fingerprint,
            representation.signature_explicit,
            representation.operator_tree,
        )
        if any(view is None for view in required_views):
            raise _UnsupportedSource("source_required_view_missing")
        sites = enumerate_quantifier_sites(theorem.proof_stripped_declaration, self.config)
        if not sites:
            raise _UnsupportedSource("no_supported_quantifier_site")
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
                f"quantifier:{site.token_index}:{site.start}:{site.end}:{site.mutation_id}"
                for site in sites
            ),
            required_capabilities=(
                "alpha_identity_fingerprint",
                "exact_quantifier_diff",
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
        candidate = apply_quantifier_trace(
            theorem.proof_stripped_declaration,
            trace,
            expected_table_hash=self.table_hash,
        )
        inverse = _inverse_trace(candidate, site, table_hash=self.table_hash)
        if (
            apply_quantifier_trace(
                candidate,
                inverse,
                expected_table_hash=self.table_hash,
            )
            != theorem.proof_stripped_declaration
        ):
            raise N02QuantifierError("n02_internal_round_trip_failure")
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
                expected_structural_diff={
                    "operation": "replace_quantifier_token_exact",
                    "mutation_id": site.mutation_id,
                    "source_quantifier": site.source_token,
                    "target_quantifier": site.target_token,
                    "source_span_start": site.start,
                    "source_span_end": site.end,
                    "token_index": site.token_index,
                    "table_hash": self.table_hash,
                },
                generation_config_hash=self.registry_hash,
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
        lineage_ok = (
            draft.rule_id == self.rule_id
            and draft.rule_version == self.rule_version
            and draft.family_id == self.family_id
            and draft.generation_config_hash == self.registry_hash
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
        if draft.candidate_code == source.proof_stripped_declaration:
            violations.append("candidate_unchanged")
        if draft.intended_relation != IntendedRelation.NEAR_MISS:
            violations.append("intended_relation_mismatch")

        matching_site: QuantifierMutationSite | None = None
        try:
            matches = tuple(
                site
                for site in enumerate_quantifier_sites(
                    source.proof_stripped_declaration,
                    self.config,
                )
                if _trace(source.proof_stripped_declaration, site, table_hash=self.table_hash)
                == draft.transformation_trace
                and _inverse_trace(draft.candidate_code, site, table_hash=self.table_hash)
                == draft.inverse_trace
            )
            if len(matches) == 1:
                matching_site = matches[0]
        except N02QuantifierError:
            matching_site = None
        exact_trace_ok = matching_site is not None
        if not exact_trace_ok:
            violations.append("trace_not_from_current_table")

        try:
            forward_ok = (
                apply_quantifier_trace(
                    source.proof_stripped_declaration,
                    draft.transformation_trace,
                    expected_table_hash=self.table_hash,
                )
                == draft.candidate_code
            )
        except N02QuantifierError:
            forward_ok = False
        if not forward_ok:
            violations.append("forward_trace_failed")
        try:
            roundtrip_ok = (
                draft.inverse_trace is not None
                and apply_quantifier_trace(
                    draft.candidate_code,
                    draft.inverse_trace,
                    expected_table_hash=self.table_hash,
                )
                == source.proof_stripped_declaration
            )
        except N02QuantifierError:
            roundtrip_ok = False
        if not roundtrip_ok:
            violations.append("inverse_roundtrip_failed")

        expected_diff_ok = (
            matching_site is not None
            and draft.expected_structural_diff
            == {
                "operation": "replace_quantifier_token_exact",
                "mutation_id": matching_site.mutation_id,
                "source_quantifier": matching_site.source_token,
                "target_quantifier": matching_site.target_token,
                "source_span_start": matching_site.start,
                "source_span_end": matching_site.end,
                "token_index": matching_site.token_index,
                "table_hash": self.table_hash,
            }
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
        source_views_ok = (
            source_representation.view_status["signature_explicit"] == ViewStatus.OK
            and source_representation.view_status["operator_tree"] == ViewStatus.OK
            and source_representation.alpha_identity_fingerprint is not None
        )
        candidate_views_ok = (
            candidate_representation.view_status["signature_explicit"] == ViewStatus.OK
            and candidate_representation.view_status["operator_tree"] == ViewStatus.OK
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
        tree_changed = (
            source_representation.operator_tree is not None
            and candidate_representation.operator_tree is not None
            and source_representation.operator_tree != candidate_representation.operator_tree
        )
        if not alpha_changed:
            violations.append("alpha_identity_not_changed")
        if not tree_changed:
            violations.append("operator_tree_not_changed")

        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("n02_exact_quantifier_token",),
                required_capabilities=(
                    "alpha_identity_fingerprint",
                    "exact_quantifier_diff",
                    "lean_reelaboration",
                    "operator_tree",
                ),
                metadata={"table_hash": self.table_hash},
            ),
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=(
                candidate.elaboration_status
                if candidate_elaborates and not violations
                else ValidationStatus.QUARANTINED
            ),
            recommended_quality_tier=(
                QualityTier.PROVISIONAL if not violations else QualityTier.UNKNOWN
            ),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=(
                exact_trace_ok
                and forward_ok
                and expected_diff_ok
                and alpha_changed
                and tree_changed
            ),
            inverse_or_roundtrip_ok=roundtrip_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "alpha_identity_changed": alpha_changed,
                "context_equal": context_ok,
                "intention_is_not_label": True,
                "operator_tree_changed": tree_changed,
                "semantic_negative_resolved": False,
                "table_hash": self.table_hash,
            },
        )


__all__ = [
    "N02QuantifierConfig",
    "N02QuantifierError",
    "N02QuantifierMutation",
    "N02QuantifierRule",
    "QuantifierMutationSite",
    "apply_quantifier_trace",
    "enumerate_quantifier_sites",
    "load_n02_quantifier_config",
    "quantifier_table_hash",
]
