"""LF-034 N14: move one root negation outside one universal binder.

The executable scope is intentionally narrow.  The theorem conclusion must be
exactly ``forall x : A, Not (P x)`` with one explicit, singly named binder.
Generation produces ``Not (forall x : A, P x)``.  Nested quantifiers,
multiple negations, De Morgan rewrites, and classical dualization are excluded.

Both statements are re-elaborated in one frozen Lean context.  The audit
reconstructs the exact permitted Expr-tree scope move and emits only
provisional D0 evidence: no semantic label, promotion, or training eligibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import JsonValue

from leanfaith.config.hashing import hash_canonical
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
from leanfaith.transforms.positives.p02_binders import BinderParseError, parse_typed_binders
from leanfaith.transforms.positives.v2_e0 import V2E0RuleError, _signature_bounds
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
_REQUIRED_CAPABILITIES = (
    "exact_inverse_replay",
    "exact_negation_scope_certificate",
    "same_context_reelaboration",
    "single_explicit_universal_binder",
)
_IDENTIFIER = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_']*|«[^»]+»)$")
NegationScopeDirection = Literal[
    "forall_not_to_not_forall",
    "not_forall_to_forall_not",
]


class N14NegationScopeError(ValueError):
    """An N14 source, trace, or structural certificate failed closed."""


@dataclass(frozen=True, slots=True)
class _QuantifierBinder:
    name: str
    surface: str
    type_text: str


@dataclass(frozen=True, slots=True)
class NegationScopeSite:
    conclusion_start: int
    conclusion_end: int
    source_text: str
    candidate_text: str
    universal_name: str
    universal_type_text: str
    header_binder_count: int
    source_root_hash: str
    expected_candidate_root_hash: str
    direction: NegationScopeDirection

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "conclusion_start": self.conclusion_start,
                "conclusion_end": self.conclusion_end,
                "source_text_hash": hash_canonical({"text": self.source_text}),
                "candidate_text_hash": hash_canonical({"text": self.candidate_text}),
                "universal_name": self.universal_name,
                "universal_type_hash": hash_canonical({"text": self.universal_type_text}),
                "header_binder_count": self.header_binder_count,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
                "direction": self.direction,
            }
        )


@dataclass(frozen=True, slots=True)
class NegationScopeCertificate:
    header_binder_count: int
    source_root_hash: str
    candidate_root_hash: str
    universal_type_hash: str
    predicate_hash: str
    direction: NegationScopeDirection

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "header_binder_count": self.header_binder_count,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "universal_type_hash": self.universal_type_hash,
            "predicate_hash": self.predicate_hash,
            "direction": self.direction,
        }


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise N14NegationScopeError("operator_tree_missing_root")
    return cast(dict[str, Any], tree["root"])


def _contains_outer_bvar(node: object, target: int, *, cutoff: int = 0) -> bool:
    if isinstance(node, list):
        return any(_contains_outer_bvar(item, target, cutoff=cutoff) for item in node)
    if not isinstance(node, dict):
        return False
    kind = node.get("k")
    if kind == "bvar":
        index = node.get("i")
        return isinstance(index, int) and not isinstance(index, bool) and index == target + cutoff
    for key, value in node.items():
        child_cutoff = cutoff
        if kind in {"forall", "lam", "let"} and key == "body":
            child_cutoff += 1
        if _contains_outer_bvar(value, target, cutoff=child_cutoff):
            return True
    return False


def _not_parts(node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if node.get("k") != "app":
        raise N14NegationScopeError("expected_not_application")
    not_fn = node.get("fn")
    predicate = node.get("arg")
    if (
        not isinstance(not_fn, dict)
        or not_fn.get("k") != "const"
        or not_fn.get("n") != "Not"
        or not isinstance(predicate, dict)
    ):
        raise N14NegationScopeError("expected_not_constant")
    return not_fn, predicate


def _is_not_application(node: dict[str, Any]) -> bool:
    fn = node.get("fn")
    return (
        node.get("k") == "app"
        and isinstance(fn, dict)
        and fn.get("k") == "const"
        and fn.get("n") == "Not"
    )


def _transform_target(
    node: dict[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    NegationScopeDirection,
]:
    direction: NegationScopeDirection
    if node.get("k") == "forall":
        if node.get("bi") != "default":
            raise N14NegationScopeError("universal_binder_not_explicit")
        universal_type = node.get("dom")
        not_node = node.get("body")
        if not isinstance(universal_type, dict) or not isinstance(not_node, dict):
            raise N14NegationScopeError("malformed_universal")
        _not_fn, predicate = _not_parts(not_node)
        candidate_forall = {**node, "body": predicate}
        candidate = {**not_node, "arg": candidate_forall}
        direction = "forall_not_to_not_forall"
    else:
        _not_fn, forall_node = _not_parts(node)
        if forall_node.get("k") != "forall" or forall_node.get("bi") != "default":
            raise N14NegationScopeError("universal_binder_not_explicit")
        universal_type = forall_node.get("dom")
        predicate_value = forall_node.get("body")
        if not isinstance(universal_type, dict) or not isinstance(predicate_value, dict):
            raise N14NegationScopeError("malformed_universal")
        predicate = predicate_value
        candidate_not = {**node, "arg": predicate}
        candidate = {**forall_node, "body": candidate_not}
        direction = "not_forall_to_forall_not"
    if predicate.get("k") == "forall":
        raise N14NegationScopeError("multiple_quantifiers_excluded")
    if _is_not_application(predicate):
        raise N14NegationScopeError("multiple_negations_excluded")
    if not _contains_outer_bvar(predicate, 0):
        raise N14NegationScopeError("predicate_does_not_use_universal")
    return candidate, universal_type, predicate, direction


def _replace_after_header(
    root: dict[str, Any],
    header_binder_count: int,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    NegationScopeDirection,
]:
    if header_binder_count == 0:
        return _transform_target(root)
    if root.get("k") != "forall":
        raise N14NegationScopeError("header_binder_expr_mismatch")
    body = root.get("body")
    if not isinstance(body, dict):
        raise N14NegationScopeError("malformed_header_forall")
    candidate_body, universal_type, predicate, direction = _replace_after_header(
        body,
        header_binder_count - 1,
    )
    return {**root, "body": candidate_body}, universal_type, predicate, direction


def build_negation_scope_root(
    source_root: dict[str, Any],
    header_binder_count: int,
) -> dict[str, Any]:
    candidate, _universal_type, _predicate, _direction = _replace_after_header(
        source_root,
        header_binder_count,
    )
    return candidate


def certify_negation_scope(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    header_binder_count: int,
) -> NegationScopeCertificate:
    expected, universal_type, predicate, direction = _replace_after_header(
        source_root,
        header_binder_count,
    )
    if alpha_canonical_bytes(expected) != alpha_canonical_bytes(candidate_root):
        raise N14NegationScopeError("candidate_not_exact_negation_scope_move")
    return NegationScopeCertificate(
        header_binder_count=header_binder_count,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        universal_type_hash=hash_canonical(universal_type),
        predicate_hash=hash_canonical(predicate),
        direction=direction,
    )


def _find_top_level_comma(mask: str, start: int, end: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}", "⦃": "⦄"}
    stack: list[str] = []
    for index in range(start, end):
        character = mask[index]
        if character in pairs:
            stack.append(character)
        elif character in pairs.values():
            if not stack or pairs[stack.pop()] != character:
                raise N14NegationScopeError("mismatched_quantifier_delimiter")
        elif character == "," and not stack:
            return index
    raise N14NegationScopeError("quantifier_comma_missing")


def _matching_delimiter(mask: str, start: int, end: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}", "⦃": "⦄"}
    opener = mask[start]
    if opener not in pairs:
        raise N14NegationScopeError("expected_opening_delimiter")
    stack = [opener]
    for index in range(start + 1, end):
        character = mask[index]
        if character in pairs:
            stack.append(character)
        elif character in pairs.values():
            if not stack or pairs[stack.pop()] != character:
                raise N14NegationScopeError("mismatched_quantifier_delimiter")
            if not stack:
                return index
    raise N14NegationScopeError("unclosed_quantifier_delimiter")


def _find_top_level_colon(text: str) -> int:
    pairs = {"(": ")", "[": "]", "{": "}", "⦃": "⦄"}
    stack: list[str] = []
    found: int | None = None
    for index, character in enumerate(text):
        if character in pairs:
            stack.append(character)
        elif character in pairs.values():
            if not stack or pairs[stack.pop()] != character:
                raise N14NegationScopeError("mismatched_binder_delimiter")
        elif character == ":" and not stack:
            if found is not None:
                raise N14NegationScopeError("multiple_binder_colons")
            found = index
    if stack or found is None:
        raise N14NegationScopeError("typed_binder_required")
    return found


def _parse_quantifier_binder(surface: str) -> _QuantifierBinder:
    cleaned = surface.strip()
    if cleaned.startswith("("):
        if not cleaned.endswith(")"):
            raise N14NegationScopeError("malformed_parenthesized_binder")
        inner = cleaned[1:-1].strip()
    else:
        if cleaned.startswith(("{", "[", "⦃")):
            raise N14NegationScopeError("quantifier_binder_not_explicit")
        inner = cleaned
    colon = _find_top_level_colon(inner)
    name = inner[:colon].strip()
    type_text = inner[colon + 1 :].strip()
    if _IDENTIFIER.fullmatch(name) is None or not type_text:
        raise N14NegationScopeError("single_named_typed_binder_required")
    return _QuantifierBinder(name=name, surface=cleaned, type_text=type_text)


def _header_binder_count(source: str) -> int:
    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise N14NegationScopeError(str(exc)) from exc
    return sum(len(group.names) for group in groups)


def enumerate_n14_sites(
    source: str,
    operator_tree_view: dict[str, Any],
) -> tuple[NegationScopeSite, ...]:
    try:
        mask, raw_start, raw_end = _signature_bounds(source)
        raw_conclusion = source[raw_start:raw_end]
        leading = len(raw_conclusion) - len(raw_conclusion.lstrip())
        trailing = len(raw_conclusion) - len(raw_conclusion.rstrip())
        conclusion_start = raw_start + leading
        conclusion_end = raw_end - trailing
        direction: NegationScopeDirection
        first = mask[conclusion_start]
        if first == "∀":
            universal_start = conclusion_start
            universal_end = conclusion_end
            direction = "forall_not_to_not_forall"
        elif first == "¬":
            universal_start = conclusion_start + 1
            while universal_start < conclusion_end and mask[universal_start].isspace():
                universal_start += 1
            universal_end = conclusion_end
            if universal_start < universal_end and mask[universal_start] == "(":
                close = _matching_delimiter(mask, universal_start, conclusion_end)
                if close != conclusion_end - 1:
                    return ()
                universal_start += 1
                universal_end = close
                while universal_start < universal_end and mask[universal_start].isspace():
                    universal_start += 1
                while universal_end > universal_start and mask[universal_end - 1].isspace():
                    universal_end -= 1
            if universal_start >= universal_end or mask[universal_start] != "∀":
                return ()
            direction = "not_forall_to_forall_not"
        else:
            return ()
        comma = _find_top_level_comma(mask, universal_start + 1, universal_end)
        binder_surface = source[universal_start + 1 : comma]
        if binder_surface != mask[universal_start + 1 : comma]:
            return ()
        binder = _parse_quantifier_binder(binder_surface)
        predicate_start = comma + 1
        while predicate_start < universal_end and mask[predicate_start].isspace():
            predicate_start += 1
        if direction == "forall_not_to_not_forall":
            if predicate_start >= universal_end or mask[predicate_start] != "¬":
                return ()
            predicate_start += 1
        predicate = source[predicate_start:universal_end].strip()
        if not predicate:
            return ()
        source_text = source[conclusion_start:conclusion_end]
        candidate_text = (
            f"¬ (∀ {binder.surface}, {predicate})"
            if direction == "forall_not_to_not_forall"
            else f"∀ {binder.surface}, ¬ ({predicate})"
        )
        header_count = _header_binder_count(source)
        root = cast(dict[str, Any], operator_tree_view["root"])
        expected, _universal_type, _predicate, expression_direction = _replace_after_header(
            root,
            header_count,
        )
        if expression_direction != direction:
            return ()
    except (
        KeyError,
        IndexError,
        TypeError,
        V2E0RuleError,
        N14NegationScopeError,
    ):
        return ()
    return (
        NegationScopeSite(
            conclusion_start=conclusion_start,
            conclusion_end=conclusion_end,
            source_text=source_text,
            candidate_text=candidate_text,
            universal_name=binder.name,
            universal_type_text=binder.type_text,
            header_binder_count=header_count,
            source_root_hash=hash_canonical(root),
            expected_candidate_root_hash=hash_canonical(expected),
            direction=direction,
        ),
    )


def _trace(
    site: NegationScopeSite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    expected = site.candidate_text if inverse else site.source_text
    replacement = site.source_text if inverse else site.candidate_text
    return (
        {
            "operation": "replace_exact_span",
            "n14_operation": "inverse_negation_scope_move" if inverse else "negation_scope_move",
            "start": site.conclusion_start,
            "end": site.conclusion_start + len(expected),
            "expected_text": expected,
            "replacement_text": replacement,
            "header_binder_count": site.header_binder_count,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "direction": site.direction,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_n14_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    if len(trace) != 1 or trace[0].get("operation") != "replace_exact_span":
        raise N14NegationScopeError("expected_one_replace_trace")
    step = trace[0]
    start = step.get("start")
    end = step.get("end")
    expected = step.get("expected_text")
    replacement = step.get("replacement_text")
    if not (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and isinstance(expected, str)
        and isinstance(replacement, str)
        and 0 <= start <= end <= len(source)
    ):
        raise N14NegationScopeError("malformed_trace")
    if source[start:end] != expected:
        raise N14NegationScopeError("trace_expected_text_mismatch")
    return source[:start] + replacement + source[end:]


def _expected_structural_diff(site: NegationScopeSite) -> dict[str, JsonValue]:
    return {
        "delta_kind": site.direction,
        "evidence_class": "D0",
        "header_binder_count": site.header_binder_count,
        "universal_name": site.universal_name,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
    }


class N14NegationScopeRule:
    polarity = Polarity.NEGATIVE
    rule_id = "n14_negation_scope"
    family_id = "n14_negation_scope"
    implementation_key = "n14_negation_scope"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise N14NegationScopeError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise N14NegationScopeError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "n14_negation_scope_audit_v1",
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "generation_config_hash": generation_config_hash,
                "requirements": _REQUIRED_CAPABILITIES,
            }
        )

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
        for view in ("operator_tree", "semantic_atoms", "signature_explicit"):
            if representation.view_status[view] != ViewStatus.OK:
                reasons.append(f"source_{view}_missing")
        sites = (
            enumerate_n14_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if len(sites) != 1:
            reasons.append("no_unique_negation_scope_site")
        if reasons:
            return Applicability(
                applicable=False,
                reason_codes=tuple(sorted(set(reasons))),
                required_capabilities=_REQUIRED_CAPABILITIES,
            )
        site = sites[0]
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=(f"forall_not:{site.stable_key}",),
            required_capabilities=_REQUIRED_CAPABILITIES,
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> tuple[VariantDraft, ...]:
        if not self.assess(theorem, representation).applicable:
            return ()
        (site,) = enumerate_n14_sites(
            theorem.proof_stripped_declaration,
            cast(dict[str, Any], representation.operator_tree),
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_n14_trace(theorem.proof_stripped_declaration, forward)
        if apply_n14_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise N14NegationScopeError("internal_inverse_replay_failure")
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
                intended_error_types=("E04", "E26"),
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(site),
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "generation_intention_only": True,
                    "near_miss": True,
                    "resolved_semantic_label": False,
                    "structural_direction": site.direction,
                    "training_eligible": False,
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
        if candidate.proof_stripped_declaration != draft.candidate_code:
            violations.append("candidate_code_mismatch")
        if source.elaboration_status not in _VALID_ELABORATION:
            violations.append("source_does_not_elaborate")
        if candidate.elaboration_status not in _VALID_ELABORATION:
            violations.append("candidate_does_not_elaborate")
        for side, record in (
            ("source", source_representation),
            ("candidate", candidate_representation),
        ):
            for view in ("operator_tree", "semantic_atoms", "signature_explicit"):
                if record.view_status[view] != ViewStatus.OK:
                    violations.append(f"{side}_{view}_missing")

        site: NegationScopeSite | None = None
        forward_ok = False
        inverse_ok = False
        try:
            (site,) = enumerate_n14_sites(
                source.proof_stripped_declaration,
                cast(dict[str, Any], source_representation.operator_tree),
            )
            forward_ok = (
                _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
                == draft.transformation_trace
                and apply_n14_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace
                == _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
                and draft.inverse_trace is not None
                and apply_n14_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
            if _expected_structural_diff(site) != draft.expected_structural_diff:
                violations.append("expected_structural_diff_mismatch")
        except (ValueError, TypeError, N14NegationScopeError):
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: NegationScopeCertificate | None = None
        if site is not None:
            try:
                certificate = certify_negation_scope(
                    _operator_root(source_representation),
                    _operator_root(candidate_representation),
                    site.header_binder_count,
                )
            except N14NegationScopeError as exc:
                violations.append(str(exc))
        atoms_present = (
            source_representation.semantic_atoms is not None
            and candidate_representation.semantic_atoms is not None
        )
        if not atoms_present:
            violations.append("semantic_atoms_missing")
        fingerprints_differ = (
            source_representation.alpha_identity_fingerprint is not None
            and candidate_representation.alpha_identity_fingerprint is not None
            and source_representation.alpha_identity_fingerprint
            != candidate_representation.alpha_identity_fingerprint
        )
        if not fingerprints_differ:
            violations.append("alpha_fingerprint_delta_missing")

        clean = not violations
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("n14_exact_negation_scope_move",),
                required_capabilities=_REQUIRED_CAPABILITIES,
            ),
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=(
                candidate.elaboration_status if clean else ValidationStatus.QUARANTINED
            ),
            recommended_quality_tier=(QualityTier.PROVISIONAL if clean else QualityTier.UNKNOWN),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=certificate is not None,
            atom_mapping_ok=atoms_present,
            inverse_or_roundtrip_ok=inverse_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "evidence_class": "D0",
                "failed_proof_search_used": False,
                "resolved_semantic_label": False,
                "structural_direction": certificate.direction if certificate is not None else None,
                "training_eligible": False,
                "negation_scope_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
            },
        )


__all__ = [
    "N14NegationScopeError",
    "N14NegationScopeRule",
    "NegationScopeCertificate",
    "NegationScopeSite",
    "apply_n14_trace",
    "build_negation_scope_root",
    "certify_negation_scope",
    "enumerate_n14_sites",
]
