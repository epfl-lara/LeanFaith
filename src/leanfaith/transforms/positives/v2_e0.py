"""Conservative experimental E0 presentation rules P11 and P12.

Both rules make one exact, invertible source-span edit.  Generation produces
only a provisional draft.  Their audit requires independent same-context Lean
elaboration, exact alpha-canonical theorem-type identity, semantic-atom
identity, and exact inverse replay before a materializer may create a
provisional ``VariantRecord``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from pydantic import JsonValue

from leanfaith.config.hashing import hash_canonical
from leanfaith.representations import alpha_canonical_bytes
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

_VALID_ELABORATION = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_']*"
_GUARD = rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*(?:\s+{_IDENTIFIER})?"
_BOUNDED = re.compile(
    rf"(?P<quant>[∀∃])\s+(?P<var>{_IDENTIFIER})\s+∈\s+"
    rf"(?P<guard>{_GUARD})\s*,"
)
_EXPLICIT_FORALL = re.compile(
    rf"(?P<quant>∀)\s+(?P<var>{_IDENTIFIER})\s*,\s*"
    rf"(?P=var)\s+∈\s+(?P<guard>{_GUARD})\s+→"
)
_EXPLICIT_EXISTS = re.compile(
    rf"(?P<quant>∃)\s+(?P<var>{_IDENTIFIER})\s*,\s*"
    rf"(?P=var)\s+∈\s+(?P<guard>{_GUARD})\s+∧"
)
_PROP_BINDER = re.compile(rf"\((?P<names>{_IDENTIFIER}(?:\s+{_IDENTIFIER})*)\s*:\s*Prop\)")
_ARROW_ROOT = re.compile(rf"^\s*(?P<prop>{_IDENTIFIER})\s*→")
_NAMED_ARROW_ROOT = re.compile(
    rf"^\s*\((?P<binder>{_IDENTIFIER})\s*:\s*(?P<prop>{_IDENTIFIER})\)\s*→"
)


class V2E0RuleError(ValueError):
    """A P11/P12 source shape, trace, or mechanical audit failed closed."""


@dataclass(frozen=True, slots=True)
class PresentationSite:
    operation: str
    start: int
    end: int
    source_text: str
    replacement_text: str
    metadata: tuple[tuple[str, str], ...]

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "operation": self.operation,
                "start": self.start,
                "end": self.end,
                "source_text": self.source_text,
                "replacement_text": self.replacement_text,
                "metadata": dict(self.metadata),
            }
        )


def _mask_noncode(source: str) -> str:
    """Replace comments and quoted tokens by spaces while preserving offsets."""

    chars = list(source)
    index = 0
    while index < len(source):
        start = index
        if source.startswith("--", index):
            end = source.find("\n", index + 2)
            end = len(source) if end < 0 else end
        elif source.startswith("/-", index):
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
                raise V2E0RuleError("unterminated_block_comment")
            end = index
        elif source[index] in {'"', "'"}:
            delimiter = source[index]
            index += 1
            escaped = False
            while index < len(source):
                character = source[index]
                index += 1
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == delimiter:
                    break
            else:
                raise V2E0RuleError("unterminated_quoted_token")
            end = index
        elif source[index] == "«":
            close = source.find("»", index + 1)
            if close < 0:
                raise V2E0RuleError("unterminated_guillemet_identifier")
            end = close + 1
        else:
            index += 1
            continue
        for offset in range(start, end):
            if chars[offset] != "\n":
                chars[offset] = " "
        index = end
    return "".join(chars)


def _signature_bounds(source: str) -> tuple[str, int, int]:
    mask = _mask_noncode(source)
    declaration = re.search(r"\b(theorem|lemma)\s+(?:«[^»]+»|[A-Za-z_][A-Za-z0-9_'.]*)", mask)
    if declaration is None:
        raise V2E0RuleError("unsupported_declaration_shape")
    assignment = mask.rfind(":=")
    if assignment < declaration.end():
        raise V2E0RuleError("missing_proof_placeholder")
    tail = mask[assignment:].split()
    if tail not in ([":=", "by", "sorry"], [":=", "sorry"]):
        raise V2E0RuleError("unsupported_proof_placeholder")

    depth = 0
    conclusion_start: int | None = None
    open_to_close = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}
    closes = {value: key for key, value in open_to_close.items()}
    stack: list[str] = []
    for position in range(declaration.end(), assignment):
        character = mask[position]
        if character in open_to_close:
            stack.append(character)
            depth += 1
        elif character in closes:
            if not stack or stack[-1] != closes[character]:
                raise V2E0RuleError("mismatched_delimiter")
            stack.pop()
            depth -= 1
        elif character == ":" and depth == 0:
            conclusion_start = position + 1
            break
    if conclusion_start is None:
        raise V2E0RuleError("missing_conclusion_colon")
    return mask, conclusion_start, assignment


def _site_trace(
    site: PresentationSite,
    *,
    rule_id: str,
    generation_config_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "replace_exact_span",
            "presentation_operation": site.operation,
            "start": site.start,
            "end": site.end,
            "expected_text": site.source_text,
            "replacement_text": site.replacement_text,
            "rule_id": rule_id,
            "generation_config_hash": generation_config_hash,
            "site_metadata": dict(site.metadata),
        },
    )


def _inverse_trace(
    site: PresentationSite,
    *,
    rule_id: str,
    generation_config_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "replace_exact_span",
            "presentation_operation": f"inverse_{site.operation}",
            "start": site.start,
            "end": site.start + len(site.replacement_text),
            "expected_text": site.replacement_text,
            "replacement_text": site.source_text,
            "rule_id": rule_id,
            "generation_config_hash": generation_config_hash,
            "site_metadata": dict(site.metadata),
        },
    )


def apply_presentation_trace(
    source: str,
    trace: tuple[dict[str, JsonValue], ...],
) -> str:
    if len(trace) != 1:
        raise V2E0RuleError("presentation trace must contain exactly one edit")
    edit = trace[0]
    if edit.get("operation") != "replace_exact_span":
        raise V2E0RuleError("unsupported presentation trace operation")
    start = edit.get("start")
    end = edit.get("end")
    expected = edit.get("expected_text")
    replacement = edit.get("replacement_text")
    if not (
        isinstance(start, int)
        and isinstance(end, int)
        and isinstance(expected, str)
        and isinstance(replacement, str)
        and 0 <= start <= end <= len(source)
    ):
        raise V2E0RuleError("malformed presentation trace")
    if source[start:end] != expected:
        raise V2E0RuleError("presentation trace expected text mismatch")
    return source[:start] + replacement + source[end:]


def enumerate_p11_sites(source: str) -> tuple[PresentationSite, ...]:
    mask, conclusion_start, conclusion_end = _signature_bounds(source)
    segment = mask[conclusion_start:conclusion_end]
    sites: list[PresentationSite] = []
    for pattern in (_BOUNDED, _EXPLICIT_FORALL, _EXPLICIT_EXISTS):
        for match in pattern.finditer(segment):
            quantifier = match.group("quant")
            variable = match.group("var")
            guard = match.group("guard").strip()
            if pattern is _BOUNDED:
                connective = "→" if quantifier == "∀" else "∧"
                replacement = f"{quantifier} {variable}, {variable} ∈ {guard} {connective}"
                operation = "bounded_to_explicit"
            else:
                replacement = f"{quantifier} {variable} ∈ {guard},"
                operation = "explicit_to_bounded"
            start = conclusion_start + match.start()
            end = conclusion_start + match.end()
            sites.append(
                PresentationSite(
                    operation=operation,
                    start=start,
                    end=end,
                    source_text=source[start:end],
                    replacement_text=replacement,
                    metadata=(
                        ("guard", guard),
                        ("quantifier", quantifier),
                        ("variable", variable),
                    ),
                )
            )
    return tuple(sorted(sites, key=lambda item: (item.start, item.end, item.operation)))


def _proposition_names(header: str) -> frozenset[str]:
    return frozenset(
        name for match in _PROP_BINDER.finditer(header) for name in match.group("names").split()
    )


def enumerate_p12_sites(source: str) -> tuple[PresentationSite, ...]:
    mask, conclusion_start, conclusion_end = _signature_bounds(source)
    propositions = _proposition_names(mask[:conclusion_start])
    conclusion = mask[conclusion_start:conclusion_end]
    sites: list[PresentationSite] = []
    named = _NAMED_ARROW_ROOT.match(conclusion)
    if named is not None and named.group("prop") in propositions:
        binder = named.group("binder")
        remainder = mask[conclusion_start + named.end() : conclusion_end]
        if re.search(rf"\b{re.escape(binder)}\b", remainder) is None:
            start = conclusion_start + named.start()
            end = conclusion_start + named.end()
            leading = source[start : conclusion_start + named.start("binder") - 1]
            replacement = f"{leading}{named.group('prop')} →"
            sites.append(
                PresentationSite(
                    operation="binder_to_arrow",
                    start=start,
                    end=end,
                    source_text=source[start:end],
                    replacement_text=replacement,
                    metadata=(("binder", binder), ("proposition", named.group("prop"))),
                )
            )
    arrow = _ARROW_ROOT.match(conclusion)
    if arrow is not None and arrow.group("prop") in propositions:
        binder = "_h_p12"
        suffix = 0
        while re.search(rf"\b{re.escape(binder)}\b", mask):
            suffix += 1
            binder = f"_h_p12_{suffix}"
        start = conclusion_start + arrow.start()
        end = conclusion_start + arrow.end()
        leading_length = len(conclusion[: arrow.start("prop")])
        leading = source[start : start + leading_length]
        replacement = f"{leading}({binder} : {arrow.group('prop')}) →"
        sites.append(
            PresentationSite(
                operation="arrow_to_binder",
                start=start,
                end=end,
                source_text=source[start:end],
                replacement_text=replacement,
                metadata=(("binder", binder), ("proposition", arrow.group("prop"))),
            )
        )
    return tuple(sites)


_P12_V110_PROP_OPERATORS = frozenset(
    {
        "↔",
        "∧",
        "∨",  # noqa: RUF001 - intentional Lean logical-or glyph
        "∈",
        "∉",
        "≠",
        "≤",
        "≥",
        "⊂",
        "⊆",
    }
)


def _p12_v110_outer_parentheses(expression: str) -> str:
    """Remove only parentheses that enclose the complete expression."""

    result = expression.strip()
    while result.startswith("(") and result.endswith(")"):
        depth = 0
        closes_at_end = False
        for index, character in enumerate(result):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth < 0:
                    return result
                if depth == 0:
                    closes_at_end = index == len(result) - 1
                    break
        if not closes_at_end:
            break
        result = result[1:-1].strip()
    return result


def _p12_v110_top_level_arrow(expression: str) -> int | None:
    """Return the first root arrow, excluding arrows nested in delimiters."""

    stack: list[str] = []
    open_to_close = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}
    close_to_open = {close: open_ for open_, close in open_to_close.items()}
    for index, character in enumerate(expression):
        if character in open_to_close:
            stack.append(character)
        elif character in close_to_open:
            if not stack or stack[-1] != close_to_open[character]:
                return None
            stack.pop()
        elif (
            character == "→"
            and not stack
            and index > 0
            and index + 1 < len(expression)
            and expression[index - 1].isspace()
            and expression[index + 1].isspace()
        ):
            return index
    return None


def _p12_v110_contains_arrow_glyph(expression: str) -> bool:
    """Reject every arrow glyph in a proposed domain, including decorations."""

    return "→" in expression


def _p12_v110_is_syntactic_prop(expression: str, proposition_names: frozenset[str]) -> bool:
    """Recognize a deliberately narrow, source-visible proposition grammar.

    P12 must not turn a data-function binder such as ``Nat → Nat`` into a
    purported proof binder.  The v1.1 expansion therefore accepts only a
    proposition variable declared as ``Prop`` or a domain with a visible
    proposition constructor/operator at its root.  Candidate re-elaboration
    and the E0 whole-type identity audit remain authoritative afterwards.
    """

    candidate = _p12_v110_outer_parentheses(expression)
    if not candidate:
        return False
    # This check must precede relation/connective recognition: a domain such
    # as ``(x = 0 → True)`` contains equality but is outside P12 v1.1's
    # single-root-arrow contract.  Nested arrows and decorated arrow operators
    # belong to later, separately versioned families.
    if _p12_v110_contains_arrow_glyph(candidate):
        return False
    if candidate in proposition_names or candidate in {"False", "True"}:
        return True
    if candidate.startswith("¬") or re.match(r"^Not(?:\s|\()", candidate):
        return True
    # An unparenthesized binder/control prefix means the apparent arrow belongs
    # below that construct rather than at the theorem conclusion root.
    if re.match(r"^(?:∀|∃|fun\b|if\b|let\b|match\b)", candidate):
        return False

    stack: list[str] = []
    open_to_close = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}
    close_to_open = {close: open_ for open_, close in open_to_close.items()}
    for index, character in enumerate(candidate):
        if character in open_to_close:
            stack.append(character)
        elif character in close_to_open:
            if not stack or stack[-1] != close_to_open[character]:
                return False
            stack.pop()
        elif not stack and character in _P12_V110_PROP_OPERATORS:
            return True
        elif (
            not stack
            and character in {"=", "<", ">"}
            and index > 0
            and index + 1 < len(candidate)
            and candidate[index - 1].isspace()
            and candidate[index + 1].isspace()
        ):
            # Requiring token-separating whitespace rejects Bool equality
            # ``==``, pipelines ``<|``/``|>``, assignment ``:=``, and other
            # data-valued operators that merely contain a relation glyph.
            return True
    return False


def _p12_v110_named_binder(
    conclusion: str,
    proposition_names: frozenset[str],
) -> tuple[str, str, int] | None:
    """Parse one immediate explicit binder followed by a root arrow."""

    leading = len(conclusion) - len(conclusion.lstrip())
    if leading == len(conclusion) or conclusion[leading] != "(":
        return None
    depth = 0
    close: int | None = None
    for index in range(leading, len(conclusion)):
        character = conclusion[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                close = index
                break
            if depth < 0:
                return None
    if close is None:
        return None
    tail = conclusion[close + 1 :]
    arrow_match = re.match(r"\s+→(?=\s)", tail)
    if arrow_match is None:
        return None
    binder_text = conclusion[leading + 1 : close]
    binder_match = re.match(
        rf"^\s*(?P<binder>{_IDENTIFIER})\s*:\s*(?P<domain>.+?)\s*$", binder_text
    )
    if binder_match is None:
        return None
    domain = binder_match.group("domain")
    if not _p12_v110_is_syntactic_prop(domain, proposition_names):
        return None
    arrow_end = close + 1 + arrow_match.end()
    return binder_match.group("binder"), domain, arrow_end


def enumerate_p12_v110_sites(source: str) -> tuple[PresentationSite, ...]:
    """Expand P12 to root arrows with visibly propositional complex domains.

    This is a versioned expansion rather than a mutation of P12 v1.0.  It
    supports equality, order/membership relations, logical connectives,
    negation, ``True``/``False``, and declared ``Prop`` variables.  Arbitrary
    predicate applications and quantifier/control prefixes fail closed.
    """

    mask, conclusion_start, conclusion_end = _signature_bounds(source)
    proposition_names = _proposition_names(mask[:conclusion_start])
    conclusion = mask[conclusion_start:conclusion_end]
    named = _p12_v110_named_binder(conclusion, proposition_names)
    if named is not None:
        binder, domain, arrow_end = named
        remainder = mask[conclusion_start + arrow_end : conclusion_end]
        if re.search(rf"\b{re.escape(binder)}\b", remainder) is None:
            leading_length = len(conclusion) - len(conclusion.lstrip())
            start = conclusion_start
            end = conclusion_start + arrow_end
            if "--" in source[start:end] or "/-" in source[start:end]:
                return ()
            leading = source[start : start + leading_length]
            sites = (
                PresentationSite(
                    operation="binder_to_arrow_v110",
                    start=start,
                    end=end,
                    source_text=source[start:end],
                    replacement_text=f"{leading}({domain.strip()}) →",
                    metadata=(
                        ("binder", binder),
                        ("domain_grammar", "visible_proposition_root"),
                    ),
                ),
            )
            return sites
        return ()

    arrow_index = _p12_v110_top_level_arrow(conclusion)
    if arrow_index is None:
        return ()
    domain = conclusion[:arrow_index]
    if not _p12_v110_is_syntactic_prop(domain, proposition_names):
        return ()
    leading_length = len(domain) - len(domain.lstrip())
    domain_text = source[conclusion_start + leading_length : conclusion_start + arrow_index].strip()
    if not domain_text:
        return ()
    binder = "_h_p12v110"
    suffix = 0
    while re.search(rf"\b{re.escape(binder)}\b", mask):
        suffix += 1
        binder = f"_h_p12v110_{suffix}"
    start = conclusion_start
    end = conclusion_start + arrow_index + 1
    if "--" in source[start:end] or "/-" in source[start:end]:
        return ()
    leading = source[start : start + leading_length]
    return (
        PresentationSite(
            operation="arrow_to_binder_v110",
            start=start,
            end=end,
            source_text=source[start:end],
            replacement_text=f"{leading}({binder} : {domain_text}) →",
            metadata=(
                ("binder", binder),
                ("domain_grammar", "visible_proposition_root"),
            ),
        ),
    )


def _choose_site(
    sites: tuple[PresentationSite, ...],
    *,
    rule_id: str,
    theorem_id: str,
    seed: int,
) -> PresentationSite:
    if not sites:
        raise V2E0RuleError("no eligible presentation site")

    def rank(site: PresentationSite) -> bytes:
        value = f"{rule_id}\0{theorem_id}\0{seed}\0{site.stable_key}"
        return hashlib.sha256(value.encode("utf-8")).digest()

    return min(sites, key=rank)


class _E0PresentationRule:
    polarity = Polarity.POSITIVE
    implementation_key: str
    rule_id: str
    family_id: str
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", generation_config_hash):
            raise V2E0RuleError("generation_config_hash must be SHA-256 hex")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "v2_e0_presentation_audit_v1",
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "generation_config_hash": generation_config_hash,
                "requirements": (
                    "same_context_reelaboration",
                    "exact_inverse_replay",
                    "alpha_canonical_identity",
                    "semantic_atom_identity",
                ),
            }
        )

    def _sites(self, source: str) -> tuple[PresentationSite, ...]:
        raise NotImplementedError

    def assess(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> Applicability:
        reasons: list[str] = []
        if not theorem.is_proposition:
            reasons.append("source_not_proposition")
        if theorem.elaboration_status not in _VALID_ELABORATION:
            reasons.append("source_does_not_elaborate")
        if representation.theorem_id != theorem.theorem_id:
            reasons.append("source_representation_lineage_mismatch")
        if representation.context_id != theorem.context_id:
            reasons.append("source_representation_context_mismatch")
        if representation.raw_proof_stripped != theorem.proof_stripped_declaration:
            reasons.append("source_representation_text_mismatch")
        for view in ("signature_explicit", "semantic_atoms", "operator_tree"):
            if representation.view_status[view] != ViewStatus.OK:
                reasons.append(f"source_{view}_missing")
        if representation.alpha_identity_fingerprint is None:
            reasons.append("source_alpha_identity_fingerprint_missing")
        if reasons:
            return Applicability(
                applicable=False,
                reason_codes=tuple(sorted(set(reasons))),
                required_capabilities=(
                    "alpha_canonical_identity",
                    "exact_inverse_replay",
                    "lean_reelaboration",
                    "semantic_atom_identity",
                ),
            )
        try:
            sites = self._sites(theorem.proof_stripped_declaration)
        except V2E0RuleError as exc:
            return Applicability(
                applicable=False,
                reason_codes=(str(exc),),
                required_capabilities=(
                    "alpha_canonical_identity",
                    "exact_inverse_replay",
                    "lean_reelaboration",
                    "semantic_atom_identity",
                ),
            )
        if not sites:
            return Applicability(
                applicable=False,
                reason_codes=("no_eligible_presentation_site",),
                required_capabilities=(
                    "alpha_canonical_identity",
                    "exact_inverse_replay",
                    "lean_reelaboration",
                    "semantic_atom_identity",
                ),
            )
        return Applicability(
            applicable=True,
            reason_codes=(),
            # ``Applicability`` makes ordering part of its canonical identity.
            # Source-order is not lexicographic once offsets have different
            # digit counts (for example, ``span:98`` sorts after ``span:123``),
            # so normalize the public node identifiers explicitly.
            matched_nodes=tuple(
                sorted(f"span:{site.start}:{site.end}:{site.operation}" for site in sites)
            ),
            required_capabilities=(
                "alpha_canonical_identity",
                "exact_inverse_replay",
                "lean_reelaboration",
                "semantic_atom_identity",
            ),
            metadata={"eligible_site_count": len(sites)},
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> tuple[VariantDraft, ...]:
        applicability = self.assess(theorem, representation)
        if not applicability.applicable:
            return ()
        site = _choose_site(
            self._sites(theorem.proof_stripped_declaration),
            rule_id=self.rule_id,
            theorem_id=theorem.theorem_id,
            seed=seed,
        )
        forward = _site_trace(
            site,
            rule_id=self.rule_id,
            generation_config_hash=self.generation_config_hash,
        )
        inverse = _inverse_trace(
            site,
            rule_id=self.rule_id,
            generation_config_hash=self.generation_config_hash,
        )
        candidate = apply_presentation_trace(theorem.proof_stripped_declaration, forward)
        if apply_presentation_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise V2E0RuleError("internal_inverse_replay_failure")
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
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff={
                    "evidence_class": "E0",
                    "operation": site.operation,
                    "source_span_end": site.end,
                    "source_span_start": site.start,
                },
                generation_config_hash=self.generation_config_hash,
                metadata={"generation_intention_only": True},
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
        if not (
            draft.rule_id == self.rule_id
            and draft.family_id == self.family_id
            and draft.rule_version == self.rule_version
            and draft.generation_config_hash == self.generation_config_hash
            and draft.source_theorem_ids == (source.theorem_id,)
            and draft.source_representation_ids == (source_representation.representation_id,)
        ):
            violations.append("draft_lineage_mismatch")
        if not (
            source.context_id
            == source_representation.context_id
            == candidate.context_id
            == candidate_representation.context_id
            == draft.context_id
        ):
            violations.append("context_mismatch")
        if not (
            source_representation.theorem_id == source.theorem_id
            and candidate_representation.theorem_id == candidate.theorem_id
        ):
            violations.append("representation_lineage_mismatch")
        if source_representation.raw_proof_stripped != source.proof_stripped_declaration:
            violations.append("source_representation_text_mismatch")
        if candidate.proof_stripped_declaration != draft.candidate_code:
            violations.append("candidate_code_mismatch")
        if candidate_representation.raw_proof_stripped != candidate.proof_stripped_declaration:
            violations.append("candidate_representation_text_mismatch")

        try:
            matching_sites = tuple(
                site
                for site in self._sites(source.proof_stripped_declaration)
                if _site_trace(
                    site,
                    rule_id=self.rule_id,
                    generation_config_hash=self.generation_config_hash,
                )
                == draft.transformation_trace
                and _inverse_trace(
                    site,
                    rule_id=self.rule_id,
                    generation_config_hash=self.generation_config_hash,
                )
                == draft.inverse_trace
            )
            site_contract_ok = len(matching_sites) == 1
            forward_ok = (
                apply_presentation_trace(
                    source.proof_stripped_declaration,
                    draft.transformation_trace,
                )
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_presentation_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except V2E0RuleError:
            site_contract_ok = False
            forward_ok = False
            inverse_ok = False
        if not site_contract_ok:
            violations.append("site_contract_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        source_elaborates = source.elaboration_status in _VALID_ELABORATION
        candidate_elaborates = candidate.elaboration_status in _VALID_ELABORATION
        if not source_elaborates:
            violations.append("source_does_not_elaborate")
        if not candidate_elaborates:
            violations.append("candidate_does_not_elaborate")
        for side, representation in (
            ("source", source_representation),
            ("candidate", candidate_representation),
        ):
            for view in ("signature_explicit", "semantic_atoms", "operator_tree"):
                if representation.view_status[view] != ViewStatus.OK:
                    violations.append(f"{side}_{view}_missing")

        alpha_fingerprint_equal = (
            source_representation.alpha_identity_fingerprint is not None
            and source_representation.alpha_identity_fingerprint
            == candidate_representation.alpha_identity_fingerprint
        )
        alpha_bytes_equal = False
        if (
            source_representation.operator_tree is not None
            and candidate_representation.operator_tree is not None
        ):
            alpha_bytes_equal = alpha_canonical_bytes(
                source_representation.operator_tree
            ) == alpha_canonical_bytes(candidate_representation.operator_tree)
        if not alpha_fingerprint_equal:
            violations.append("alpha_identity_fingerprint_mismatch")
        if not alpha_bytes_equal:
            violations.append("alpha_canonical_bytes_mismatch")
        atoms_equal = (
            source_representation.semantic_atoms is not None
            and source_representation.semantic_atoms == candidate_representation.semantic_atoms
        )
        if not atoms_equal:
            violations.append("semantic_atoms_mismatch")

        structural_ok = (
            site_contract_ok
            and forward_ok
            and inverse_ok
            and draft.candidate_code != source.proof_stripped_declaration
        )
        clean = not violations
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=(f"{self.rule_id}_exact_presentation",),
                required_capabilities=(
                    "alpha_canonical_identity",
                    "exact_inverse_replay",
                    "lean_reelaboration",
                    "semantic_atom_identity",
                ),
            ),
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=(
                candidate.elaboration_status if clean else ValidationStatus.QUARANTINED
            ),
            recommended_quality_tier=(QualityTier.PROVISIONAL if clean else QualityTier.UNKNOWN),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=structural_ok,
            atom_mapping_ok=atoms_equal,
            inverse_or_roundtrip_ok=inverse_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "alpha_canonical_bytes_equal": alpha_bytes_equal,
                "alpha_identity_fingerprint_equal": alpha_fingerprint_equal,
                "evidence_class": "E0",
                "resolved_semantic_label": False,
                "source_candidate_elaborated": source_elaborates and candidate_elaborates,
                "training_eligible": False,
            },
        )


class P11BoundedQuantifierRule(_E0PresentationRule):
    rule_id = "p11_bounded_quantifiers"
    family_id = "p11_bounded_quantifiers"
    implementation_key = "p11_bounded_quantifiers"

    def _sites(self, source: str) -> tuple[PresentationSite, ...]:
        return enumerate_p11_sites(source)


class P12ProofArrowBinderRule(_E0PresentationRule):
    rule_id = "p12_proof_arrow_binder"
    family_id = "p12_proof_arrow_binder"
    implementation_key = "p12_proof_arrow_binder"

    def _sites(self, source: str) -> tuple[PresentationSite, ...]:
        return enumerate_p12_sites(source)


class P12ProofArrowBinderV110Rule(_E0PresentationRule):
    """P12 v1.1 complex root proof-arrow presentation expansion."""

    rule_id = "p12_proof_arrow_binder"
    family_id = "p12_proof_arrow_binder"
    implementation_key = "p12_proof_arrow_binder"
    rule_version = "1.1.0"

    def _sites(self, source: str) -> tuple[PresentationSite, ...]:
        return enumerate_p12_v110_sites(source)


__all__ = [
    "P11BoundedQuantifierRule",
    "P12ProofArrowBinderRule",
    "P12ProofArrowBinderV110Rule",
    "PresentationSite",
    "V2E0RuleError",
    "apply_presentation_trace",
    "enumerate_p11_sites",
    "enumerate_p12_sites",
    "enumerate_p12_v110_sites",
]
