"""LF-034 N13: swap one root ``forall``/``exists`` witness dependency.

The executable scope is intentionally narrow.  The theorem conclusion must be
exactly ``forall x : A, exists y : B, R`` with explicit, singly named binders.
The elaborated witness type ``B`` must not depend on ``x`` and ``R`` must use
both binders.  Generation produces ``exists y : B, forall x : A, R``.

The source and candidate are both re-elaborated in one frozen Lean context and
the audit reconstructs the exact permitted Expr-tree permutation.  The family
emits only provisional D0 structural evidence: it creates no semantic label,
promotion, or training eligibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

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
    "exact_witness_dependency_certificate",
    "independent_witness_type",
    "same_context_reelaboration",
)
_IDENTIFIER = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_']*|«[^»]+»)$")


class N13WitnessDependencyError(ValueError):
    """An N13 source, trace, or structural certificate failed closed."""


@dataclass(frozen=True, slots=True)
class _QuantifierBinder:
    name: str
    surface: str
    type_text: str


@dataclass(frozen=True, slots=True)
class WitnessDependencySite:
    conclusion_start: int
    conclusion_end: int
    source_text: str
    candidate_text: str
    universal_name: str
    witness_name: str
    universal_type_text: str
    witness_type_text: str
    header_binder_count: int
    source_root_hash: str
    expected_candidate_root_hash: str

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "conclusion_start": self.conclusion_start,
                "conclusion_end": self.conclusion_end,
                "source_text_hash": hash_canonical({"text": self.source_text}),
                "candidate_text_hash": hash_canonical({"text": self.candidate_text}),
                "universal_name": self.universal_name,
                "witness_name": self.witness_name,
                "universal_type_hash": hash_canonical({"text": self.universal_type_text}),
                "witness_type_hash": hash_canonical({"text": self.witness_type_text}),
                "header_binder_count": self.header_binder_count,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class WitnessDependencyCertificate:
    header_binder_count: int
    source_root_hash: str
    candidate_root_hash: str
    universal_type_hash: str
    witness_type_hash: str
    predicate_hash: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "header_binder_count": self.header_binder_count,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "universal_type_hash": self.universal_type_hash,
            "witness_type_hash": self.witness_type_hash,
            "predicate_hash": self.predicate_hash,
        }


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise N13WitnessDependencyError("operator_tree_missing_root")
    return cast(dict[str, Any], tree["root"])


def _shift_outer_bvars(node: object, delta: int, *, cutoff: int = 0) -> object:
    if isinstance(node, list):
        return [_shift_outer_bvars(item, delta, cutoff=cutoff) for item in node]
    if not isinstance(node, dict):
        return node
    kind = node.get("k")
    if kind == "bvar":
        index = node.get("i")
        if not isinstance(index, int) or isinstance(index, bool):
            raise N13WitnessDependencyError("malformed_bvar_index")
        if index < cutoff:
            return dict(node)
        shifted = index + delta
        if shifted < cutoff:
            raise N13WitnessDependencyError("witness_type_depends_on_universal")
        return {**node, "i": shifted}
    result: dict[str, object] = {}
    for key, value in node.items():
        child_cutoff = cutoff
        if kind in {"forall", "lam", "let"} and key == "body":
            child_cutoff += 1
        result[key] = _shift_outer_bvars(value, delta, cutoff=child_cutoff)
    return result


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


def _swap_innermost_two_bvars(node: object, *, cutoff: int = 0) -> object:
    if isinstance(node, list):
        return [_swap_innermost_two_bvars(item, cutoff=cutoff) for item in node]
    if not isinstance(node, dict):
        return node
    kind = node.get("k")
    if kind == "bvar":
        index = node.get("i")
        if not isinstance(index, int) or isinstance(index, bool):
            raise N13WitnessDependencyError("malformed_bvar_index")
        if index == cutoff:
            return {**node, "i": cutoff + 1}
        if index == cutoff + 1:
            return {**node, "i": cutoff}
        return dict(node)
    result: dict[str, object] = {}
    for key, value in node.items():
        child_cutoff = cutoff
        if kind in {"forall", "lam", "let"} and key == "body":
            child_cutoff += 1
        result[key] = _swap_innermost_two_bvars(value, cutoff=child_cutoff)
    return result


def _exists_parts(
    node: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if node.get("k") != "app":
        raise N13WitnessDependencyError("expected_exists_application")
    exists_fn = node.get("fn")
    predicate = node.get("arg")
    if not isinstance(exists_fn, dict) or not isinstance(predicate, dict):
        raise N13WitnessDependencyError("malformed_exists_application")
    if exists_fn.get("k") != "app":
        raise N13WitnessDependencyError("malformed_exists_type_application")
    exists_const = exists_fn.get("fn")
    witness_type = exists_fn.get("arg")
    if (
        not isinstance(exists_const, dict)
        or exists_const.get("k") != "const"
        or exists_const.get("n") != "Exists"
        or not isinstance(witness_type, dict)
    ):
        raise N13WitnessDependencyError("expected_exists_constant")
    if predicate.get("k") != "lam" or predicate.get("bi") != "default":
        raise N13WitnessDependencyError("exists_binder_not_explicit")
    lambda_domain = predicate.get("dom")
    body = predicate.get("body")
    if not isinstance(lambda_domain, dict) or not isinstance(body, dict):
        raise N13WitnessDependencyError("malformed_exists_predicate")
    if alpha_canonical_bytes(lambda_domain) != alpha_canonical_bytes(witness_type):
        raise N13WitnessDependencyError("exists_type_lambda_domain_mismatch")
    return exists_fn, witness_type, predicate, body


def _transform_target(node: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    if node.get("k") != "forall" or node.get("bi") != "default":
        raise N13WitnessDependencyError("universal_binder_not_explicit")
    universal_type = node.get("dom")
    exists_node = node.get("body")
    if not isinstance(universal_type, dict) or not isinstance(exists_node, dict):
        raise N13WitnessDependencyError("malformed_universal")
    exists_fn, witness_type, predicate, relation = _exists_parts(exists_node)
    if _contains_outer_bvar(witness_type, 0):
        raise N13WitnessDependencyError("witness_type_depends_on_universal")
    if not _contains_outer_bvar(relation, 0):
        raise N13WitnessDependencyError("relation_does_not_use_witness")
    if not _contains_outer_bvar(relation, 1):
        raise N13WitnessDependencyError("relation_does_not_use_universal")
    lowered_witness = _shift_outer_bvars(witness_type, -1)
    raised_universal = _shift_outer_bvars(universal_type, 1)
    swapped_relation = _swap_innermost_two_bvars(relation)
    if not all(
        isinstance(item, dict) for item in (lowered_witness, raised_universal, swapped_relation)
    ):
        raise N13WitnessDependencyError("malformed_witness_dependency_subtree")
    lowered_witness = cast(dict[str, Any], lowered_witness)
    raised_universal = cast(dict[str, Any], raised_universal)
    swapped_relation = cast(dict[str, Any], swapped_relation)
    candidate_forall = {**node, "dom": raised_universal, "body": swapped_relation}
    candidate_predicate = {
        **predicate,
        "dom": lowered_witness,
        "body": candidate_forall,
    }
    candidate_exists_fn = {**exists_fn, "arg": lowered_witness}
    candidate_exists = {**exists_node, "fn": candidate_exists_fn, "arg": candidate_predicate}
    return candidate_exists, universal_type, witness_type, relation


def _replace_after_header(
    root: dict[str, Any],
    header_binder_count: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if header_binder_count == 0:
        return cast(
            tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]],
            _transform_target(root),
        )
    if root.get("k") != "forall":
        raise N13WitnessDependencyError("header_binder_expr_mismatch")
    body = root.get("body")
    if not isinstance(body, dict):
        raise N13WitnessDependencyError("malformed_header_forall")
    candidate_body, universal_type, witness_type, relation = _replace_after_header(
        body,
        header_binder_count - 1,
    )
    return (
        {**root, "body": candidate_body},
        universal_type,
        witness_type,
        relation,
    )


def build_witness_dependency_root(
    source_root: dict[str, Any],
    header_binder_count: int,
) -> dict[str, Any]:
    candidate, _universal_type, _witness_type, _relation = _replace_after_header(
        source_root,
        header_binder_count,
    )
    return candidate


def certify_witness_dependency(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    header_binder_count: int,
) -> WitnessDependencyCertificate:
    expected, universal_type, witness_type, relation = _replace_after_header(
        source_root,
        header_binder_count,
    )
    if alpha_canonical_bytes(expected) != alpha_canonical_bytes(candidate_root):
        raise N13WitnessDependencyError("candidate_not_exact_witness_dependency_swap")
    return WitnessDependencyCertificate(
        header_binder_count=header_binder_count,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        universal_type_hash=hash_canonical(universal_type),
        witness_type_hash=hash_canonical(witness_type),
        predicate_hash=hash_canonical(relation),
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
                raise N13WitnessDependencyError("mismatched_quantifier_delimiter")
        elif character == "," and not stack:
            return index
    raise N13WitnessDependencyError("quantifier_comma_missing")


def _find_top_level_colon(text: str) -> int:
    pairs = {"(": ")", "[": "]", "{": "}", "⦃": "⦄"}
    stack: list[str] = []
    found: int | None = None
    for index, character in enumerate(text):
        if character in pairs:
            stack.append(character)
        elif character in pairs.values():
            if not stack or pairs[stack.pop()] != character:
                raise N13WitnessDependencyError("mismatched_binder_delimiter")
        elif character == ":" and not stack:
            if found is not None:
                raise N13WitnessDependencyError("multiple_binder_colons")
            found = index
    if stack or found is None:
        raise N13WitnessDependencyError("typed_binder_required")
    return found


def _parse_quantifier_binder(surface: str) -> _QuantifierBinder:
    cleaned = surface.strip()
    if cleaned.startswith("("):
        if not cleaned.endswith(")"):
            raise N13WitnessDependencyError("malformed_parenthesized_binder")
        inner = cleaned[1:-1].strip()
    else:
        if cleaned.startswith(("{", "[", "⦃")):
            raise N13WitnessDependencyError("quantifier_binder_not_explicit")
        inner = cleaned
    colon = _find_top_level_colon(inner)
    name = inner[:colon].strip()
    type_text = inner[colon + 1 :].strip()
    if _IDENTIFIER.fullmatch(name) is None or not type_text:
        raise N13WitnessDependencyError("single_named_typed_binder_required")
    return _QuantifierBinder(name=name, surface=cleaned, type_text=type_text)


def _header_binder_count(source: str) -> int:
    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise N13WitnessDependencyError(str(exc)) from exc
    return sum(len(group.names) for group in groups)


def enumerate_n13_sites(
    source: str,
    operator_tree_view: dict[str, Any],
) -> tuple[WitnessDependencySite, ...]:
    try:
        mask, raw_start, raw_end = _signature_bounds(source)
        leading = len(source[raw_start:raw_end]) - len(source[raw_start:raw_end].lstrip())
        trailing = len(source[raw_start:raw_end]) - len(source[raw_start:raw_end].rstrip())
        conclusion_start = raw_start + leading
        conclusion_end = raw_end - trailing
        if mask[conclusion_start] != "∀":
            return ()
        first_comma = _find_top_level_comma(mask, conclusion_start + 1, conclusion_end)
        second_start = first_comma + 1
        while second_start < conclusion_end and mask[second_start].isspace():
            second_start += 1
        if second_start >= conclusion_end or mask[second_start] != "∃":
            return ()
        second_comma = _find_top_level_comma(mask, second_start + 1, conclusion_end)
        universal_surface = source[conclusion_start + 1 : first_comma]
        witness_surface = source[second_start + 1 : second_comma]
        if (
            universal_surface != mask[conclusion_start + 1 : first_comma]
            or witness_surface != mask[second_start + 1 : second_comma]
        ):
            return ()
        universal = _parse_quantifier_binder(universal_surface)
        witness = _parse_quantifier_binder(witness_surface)
        predicate = source[second_comma + 1 : conclusion_end].strip()
        if not predicate:
            return ()
        source_text = source[conclusion_start:conclusion_end]
        candidate_text = f"∃ {witness.surface}, ∀ {universal.surface}, {predicate}"
        header_count = _header_binder_count(source)
        root = cast(dict[str, Any], operator_tree_view["root"])
        expected = build_witness_dependency_root(root, header_count)
    except (
        KeyError,
        IndexError,
        TypeError,
        V2E0RuleError,
        N13WitnessDependencyError,
    ):
        return ()
    return (
        WitnessDependencySite(
            conclusion_start=conclusion_start,
            conclusion_end=conclusion_end,
            source_text=source_text,
            candidate_text=candidate_text,
            universal_name=universal.name,
            witness_name=witness.name,
            universal_type_text=universal.type_text,
            witness_type_text=witness.type_text,
            header_binder_count=header_count,
            source_root_hash=hash_canonical(root),
            expected_candidate_root_hash=hash_canonical(expected),
        ),
    )


def _trace(
    site: WitnessDependencySite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    expected = site.candidate_text if inverse else site.source_text
    replacement = site.source_text if inverse else site.candidate_text
    return (
        {
            "operation": "replace_exact_span",
            "n13_operation": (
                "inverse_witness_dependency_swap" if inverse else "witness_dependency_swap"
            ),
            "start": site.conclusion_start,
            "end": site.conclusion_start + len(expected),
            "expected_text": expected,
            "replacement_text": replacement,
            "header_binder_count": site.header_binder_count,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_n13_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    if len(trace) != 1 or trace[0].get("operation") != "replace_exact_span":
        raise N13WitnessDependencyError("expected_one_replace_trace")
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
        raise N13WitnessDependencyError("malformed_trace")
    if source[start:end] != expected:
        raise N13WitnessDependencyError("trace_expected_text_mismatch")
    return source[:start] + replacement + source[end:]


def _expected_structural_diff(site: WitnessDependencySite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "forall_exists_to_exists_forall",
        "evidence_class": "D0",
        "header_binder_count": site.header_binder_count,
        "universal_name": site.universal_name,
        "witness_name": site.witness_name,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
    }


class N13WitnessDependencyRule:
    polarity = Polarity.NEGATIVE
    rule_id = "n13_witness_dependency"
    family_id = "n13_witness_dependency"
    implementation_key = "n13_witness_dependency"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise N13WitnessDependencyError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise N13WitnessDependencyError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "n13_witness_dependency_audit_v1",
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
            enumerate_n13_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if len(sites) != 1:
            reasons.append("no_unique_witness_dependency_site")
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
            matched_nodes=(f"forall_exists:{site.stable_key}",),
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
        (site,) = enumerate_n13_sites(
            theorem.proof_stripped_declaration,
            cast(dict[str, Any], representation.operator_tree),
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_n13_trace(theorem.proof_stripped_declaration, forward)
        if apply_n13_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise N13WitnessDependencyError("internal_inverse_replay_failure")
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
                intended_error_types=("E05", "E22"),
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(site),
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "generation_intention_only": True,
                    "near_miss": True,
                    "resolved_semantic_label": False,
                    "structural_direction": "forall_exists_to_exists_forall",
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

        site: WitnessDependencySite | None = None
        forward_ok = False
        inverse_ok = False
        try:
            (site,) = enumerate_n13_sites(
                source.proof_stripped_declaration,
                cast(dict[str, Any], source_representation.operator_tree),
            )
            forward_ok = (
                _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
                == draft.transformation_trace
                and apply_n13_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace
                == _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
                and draft.inverse_trace is not None
                and apply_n13_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
            if _expected_structural_diff(site) != draft.expected_structural_diff:
                violations.append("expected_structural_diff_mismatch")
        except (ValueError, TypeError, N13WitnessDependencyError):
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: WitnessDependencyCertificate | None = None
        if site is not None:
            try:
                certificate = certify_witness_dependency(
                    _operator_root(source_representation),
                    _operator_root(candidate_representation),
                    site.header_binder_count,
                )
            except N13WitnessDependencyError as exc:
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
                matched_nodes=("n13_exact_witness_dependency_swap",),
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
                "structural_direction": "forall_exists_to_exists_forall",
                "training_eligible": False,
                "witness_dependency_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
            },
        )


__all__ = [
    "N13WitnessDependencyError",
    "N13WitnessDependencyRule",
    "WitnessDependencyCertificate",
    "WitnessDependencySite",
    "apply_n13_trace",
    "build_witness_dependency_root",
    "certify_witness_dependency",
    "enumerate_n13_sites",
]
