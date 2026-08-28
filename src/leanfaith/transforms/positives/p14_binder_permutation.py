"""LF-033 P14: exact adjacent independent data-binder permutation.

P14 swaps one adjacent pair of explicit declaration-header binders.  The
surface edit is deliberately small, but the semantic certificate is built
from the complete elaborated expression: section variables and auto-implicit
binders may precede the binders written in the declaration.  Audit therefore
searches the full outer-forall chain and accepts only a *unique* adjacent
forall permutation whose exact de-Bruijn transform equals the independently
elaborated candidate.

This is E2 evidence only.  It never resolves an F1 label, promotes an item, or
makes a pair training-eligible.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Literal, cast

from pydantic import JsonValue

from leanfaith.config.hashing import hash_canonical
from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.representations.atoms import semantic_atoms
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
from leanfaith.transforms.positives.p02_binders import (
    BinderKind,
    BinderParseError,
    TypedBinder,
    _lex_lean,
    enumerate_binder_edits,
    parse_typed_binders,
)
from leanfaith.transforms.positives.p17_hypothesis_packing import (
    _free_bvars,
    _operator_root,
    _rebuild_prefix,
    _shift_free_bvars,
)
from leanfaith.transforms.protocol import (
    build_transformation_audit,
    build_variant_draft,
    verify_variant_draft_id,
)

PermutationOperation = Literal["swap_within_typed_group", "swap_adjacent_singletons"]
DataEvidence = Literal["direct_sort", "typed_bvar", "ground_constant"]

_VALID_ELABORATION = frozenset(
    {ValidationStatus.ELABORATES, ValidationStatus.ELABORATES_WITH_PLACEHOLDER}
)
_GROUND_DATA_CONSTANTS = frozenset({"Nat", "Int", "Rat", "Real", "Bool", "String", "Char", "Unit"})
_SIMPLE_IDENTIFIER = re.compile(r"^(?:[^\W\d]|_)[\w']*$", re.UNICODE)
_UNSAFE_TYPE_SURFACE = re.compile(r"(?<![A-Za-z0-9_'])_(?![A-Za-z0-9_'])|\?|\$\(|`")
_REQUIRED_CAPABILITIES = (
    "both_selected_binders_used",
    "exact_adjacent_forall_permutation",
    "exact_inverse_replay",
    "explicit_data_binder_certificate",
    "full_outer_forall_unique_match",
    "same_context_reelaboration",
)


class P14BinderPermutationError(ValueError):
    """A P14 surface edit or elaborated certificate failed closed."""


@dataclass(frozen=True, slots=True)
class BinderPermutationSite:
    operation: PermutationOperation
    start: int
    end: int
    source_text: str
    candidate_text: str
    left_name: str
    right_name: str
    left_surface_index: int
    right_surface_index: int
    left_type_hash: str
    right_type_hash: str
    p02_site_count: int

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "operation": self.operation,
                "start": self.start,
                "end": self.end,
                "source_text": self.source_text,
                "candidate_text": self.candidate_text,
                "left_name": self.left_name,
                "right_name": self.right_name,
                "left_surface_index": self.left_surface_index,
                "right_surface_index": self.right_surface_index,
                "left_type_hash": self.left_type_hash,
                "right_type_hash": self.right_type_hash,
                "p02_site_count": self.p02_site_count,
            }
        )


@dataclass(frozen=True, slots=True)
class BinderPermutationCertificate:
    operation: PermutationOperation
    selected_surface_indices: tuple[int, int]
    selected_outer_indices: tuple[int, int]
    hidden_outer_offset: int
    source_root_hash: str
    candidate_root_hash: str
    left_domain_hash: str
    right_domain_hash: str
    lowered_right_domain_hash: str
    raised_left_domain_hash: str
    source_residual_hash: str
    inverse_residual_hash: str
    right_domain_free_bvars: tuple[int, ...]
    left_data_evidence: DataEvidence
    right_data_evidence: DataEvidence
    left_referenced_outer_index: int | None
    right_referenced_outer_index: int | None
    source_alpha_fingerprint: str
    candidate_alpha_fingerprint: str
    witness_schema: Literal["adjacent_independent_forall_commutation_v1"] = (
        "adjacent_independent_forall_commutation_v1"
    )

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "operation": self.operation,
            "selected_surface_indices": list(self.selected_surface_indices),
            "selected_outer_indices": list(self.selected_outer_indices),
            "hidden_outer_offset": self.hidden_outer_offset,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "left_domain_hash": self.left_domain_hash,
            "right_domain_hash": self.right_domain_hash,
            "lowered_right_domain_hash": self.lowered_right_domain_hash,
            "raised_left_domain_hash": self.raised_left_domain_hash,
            "source_residual_hash": self.source_residual_hash,
            "inverse_residual_hash": self.inverse_residual_hash,
            "right_domain_free_bvars": list(self.right_domain_free_bvars),
            "right_depends_on_left": False,
            "left_depends_on_right": False,
            "left_data_evidence": self.left_data_evidence,
            "right_data_evidence": self.right_data_evidence,
            "left_referenced_outer_index": self.left_referenced_outer_index,
            "right_referenced_outer_index": self.right_referenced_outer_index,
            "residual_uses_left": True,
            "residual_uses_right": True,
            "source_alpha_fingerprint": self.source_alpha_fingerprint,
            "candidate_alpha_fingerprint": self.candidate_alpha_fingerprint,
            "witness_schema": self.witness_schema,
        }


@dataclass(frozen=True, slots=True)
class _TreeWitness:
    position: int
    expected_candidate_root: dict[str, Any]
    left_domain: dict[str, Any]
    right_domain: dict[str, Any]
    lowered_right_domain: dict[str, Any]
    raised_left_domain: dict[str, Any]
    residual: dict[str, Any]
    left_data_evidence: DataEvidence
    right_data_evidence: DataEvidence
    left_referenced_outer_index: int | None
    right_referenced_outer_index: int | None


def _outer_foralls(root: dict[str, Any]) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    current = root
    while current.get("k") == "forall":
        domain = current.get("dom")
        body = current.get("body")
        if not isinstance(domain, dict) or not isinstance(body, dict):
            raise P14BinderPermutationError("malformed_outer_forall")
        nodes.append(current)
        current = body
    return tuple(nodes), current


def _swap_relative_zero_one(node: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Swap the two selected free variables while respecting local binders."""

    kind = node.get("k")
    if kind == "bvar":
        index = node.get("i")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise P14BinderPermutationError("malformed_bound_variable")
        relative = index - depth
        if relative == 0:
            return {**node, "i": depth + 1}
        if relative == 1:
            return {**node, "i": depth}
        return dict(node)
    if kind in {"forall", "lam"}:
        domain = node.get("dom")
        body = node.get("body")
        if not isinstance(domain, dict) or not isinstance(body, dict):
            raise P14BinderPermutationError("malformed_local_binder")
        return {
            **node,
            "dom": _swap_relative_zero_one(domain, depth),
            "body": _swap_relative_zero_one(body, depth + 1),
        }
    if kind == "let":
        type_node = node.get("t")
        value = node.get("v")
        body = node.get("body")
        if not all(isinstance(child, dict) for child in (type_node, value, body)):
            raise P14BinderPermutationError("malformed_let_expression")
        return {
            **node,
            "t": _swap_relative_zero_one(cast(dict[str, Any], type_node), depth),
            "v": _swap_relative_zero_one(cast(dict[str, Any], value), depth),
            "body": _swap_relative_zero_one(cast(dict[str, Any], body), depth + 1),
        }
    updated = dict(node)
    for key in ("fn", "arg", "base"):
        child = node.get(key)
        if isinstance(child, dict):
            updated[key] = _swap_relative_zero_one(child, depth)
    return updated


def _data_evidence(
    domain: dict[str, Any],
    *,
    binder_position: int,
    binders: Sequence[dict[str, Any]],
) -> tuple[DataEvidence, int | None]:
    kind = domain.get("k")
    if kind == "sort":
        # `P : Prop` is a proposition variable, not a proof of P.
        return "direct_sort", None
    if kind == "const" and domain.get("n") in _GROUND_DATA_CONSTANTS:
        return "ground_constant", None
    if kind == "bvar":
        index = domain.get("i")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise P14BinderPermutationError("malformed_data_domain_reference")
        referenced = binder_position - 1 - index
        if not 0 <= referenced < binder_position:
            raise P14BinderPermutationError("data_domain_reference_out_of_scope")
        referenced_domain = binders[referenced].get("dom")
        if not isinstance(referenced_domain, dict) or referenced_domain.get("k") != "sort":
            raise P14BinderPermutationError("data_domain_reference_not_type_binder")
        if referenced_domain.get("u") == "0":
            raise P14BinderPermutationError("proof_binder_excluded")
        return "typed_bvar", referenced
    raise P14BinderPermutationError("unsupported_data_domain")


def _tree_witness_at(root: dict[str, Any], position: int) -> _TreeWitness:
    binders, _tail = _outer_foralls(root)
    if not 0 <= position < len(binders) - 1:
        raise P14BinderPermutationError("binder_position_out_of_range")
    left = binders[position]
    right = binders[position + 1]
    if left.get("bi") != "default" or right.get("bi") != "default":
        raise P14BinderPermutationError("nonexplicit_binder_excluded")
    left_domain = cast(dict[str, Any], left["dom"])
    right_domain = cast(dict[str, Any], right["dom"])
    residual = cast(dict[str, Any], right["body"])
    right_free = _free_bvars(right_domain)
    if 0 in right_free:
        raise P14BinderPermutationError("right_domain_depends_on_left")
    residual_free = _free_bvars(residual)
    if not {0, 1}.issubset(residual_free):
        raise P14BinderPermutationError("selected_binder_unused_in_residual")
    left_evidence, left_ref = _data_evidence(
        left_domain,
        binder_position=position,
        binders=binders,
    )
    right_evidence, right_ref = _data_evidence(
        right_domain,
        binder_position=position + 1,
        binders=binders,
    )
    lowered_right = _shift_free_bvars(right_domain, delta=-1, cutoff=1)
    raised_left = _shift_free_bvars(left_domain, delta=1, cutoff=0)
    swapped_residual = _swap_relative_zero_one(residual)
    inner_left = {**left, "dom": raised_left, "body": swapped_residual}
    outer_right = {**right, "dom": lowered_right, "body": inner_left}
    expected = _rebuild_prefix(binders[:position], outer_right)
    return _TreeWitness(
        position=position,
        expected_candidate_root=expected,
        left_domain=left_domain,
        right_domain=right_domain,
        lowered_right_domain=lowered_right,
        raised_left_domain=raised_left,
        residual=residual,
        left_data_evidence=left_evidence,
        right_data_evidence=right_evidence,
        left_referenced_outer_index=left_ref,
        right_referenced_outer_index=right_ref,
    )


def build_binder_permutation_root(source_root: dict[str, Any], position: int) -> dict[str, Any]:
    """Public exact-tree helper used by property and integration tests."""

    return _tree_witness_at(source_root, position).expected_candidate_root


def _simple_names(group: TypedBinder) -> bool:
    return (
        not group.has_comment
        and _UNSAFE_TYPE_SURFACE.search(group.type_text) is None
        and all(_SIMPLE_IDENTIFIER.fullmatch(name) is not None for name in group.names)
    )


def _name_spans(group: TypedBinder) -> tuple[tuple[int, int, str], ...]:
    tokens = _lex_lean(group.original_text)
    colon = next((index for index, token in enumerate(tokens) if token.text == ":"), None)
    if colon is None:
        raise P14BinderPermutationError("typed_binder_missing_colon")
    name_tokens = [token for token in tokens[1:colon] if token.kind in {"atom", "guillemet"}]
    if tuple(token.text for token in name_tokens) != group.names:
        raise P14BinderPermutationError("surface_name_span_mismatch")
    return tuple(
        (group.start + token.start, group.start + token.end, token.text) for token in name_tokens
    )


def enumerate_p14_surface_sites(source: str) -> tuple[BinderPermutationSite, ...]:
    """Enumerate exact source-visible P14 sites without assigning a label."""

    try:
        groups = parse_typed_binders(source)
        p02_site_count = len(enumerate_binder_edits(source))
    except BinderParseError:
        return ()
    expanded: list[tuple[TypedBinder, str]] = [
        (group, name) for group in groups for name in group.names
    ]
    counts: dict[str, int] = {}
    for _group, name in expanded:
        counts[name] = counts.get(name, 0) + 1
    surface_index: dict[tuple[int, str], int] = {
        (id(group), name): index for index, (group, name) in enumerate(expanded)
    }
    sites: list[BinderPermutationSite] = []

    for group in groups:
        if group.kind != BinderKind.EXPLICIT or not _simple_names(group) or len(group.names) < 2:
            continue
        spans = _name_spans(group)
        for left, right in pairwise(spans):
            left_start, left_end, left_name = left
            right_start, right_end, right_name = right
            if left_name == right_name or counts[left_name] != 1 or counts[right_name] != 1:
                continue
            gap = source[left_end:right_start]
            if not gap or not gap.isspace():
                continue
            source_text = source[left_start:right_end]
            candidate_text = right_name + gap + left_name
            left_index = surface_index[(id(group), left_name)]
            sites.append(
                BinderPermutationSite(
                    operation="swap_within_typed_group",
                    start=left_start,
                    end=right_end,
                    source_text=source_text,
                    candidate_text=candidate_text,
                    left_name=left_name,
                    right_name=right_name,
                    left_surface_index=left_index,
                    right_surface_index=left_index + 1,
                    left_type_hash=hash_canonical({"type": group.type_tokens}),
                    right_type_hash=hash_canonical({"type": group.type_tokens}),
                    p02_site_count=p02_site_count,
                )
            )

    for left_group, right_group in pairwise(groups):
        if (
            left_group.kind != BinderKind.EXPLICIT
            or right_group.kind != BinderKind.EXPLICIT
            or len(left_group.names) != 1
            or len(right_group.names) != 1
            or not _simple_names(left_group)
            or not _simple_names(right_group)
        ):
            continue
        left_name, right_name = left_group.names[0], right_group.names[0]
        if left_name == right_name or counts[left_name] != 1 or counts[right_name] != 1:
            continue
        gap = source[left_group.end : right_group.start]
        if gap and not gap.isspace():
            continue
        source_text = source[left_group.start : right_group.end]
        candidate_text = right_group.original_text + gap + left_group.original_text
        left_index = surface_index[(id(left_group), left_name)]
        if surface_index[(id(right_group), right_name)] != left_index + 1:
            continue
        sites.append(
            BinderPermutationSite(
                operation="swap_adjacent_singletons",
                start=left_group.start,
                end=right_group.end,
                source_text=source_text,
                candidate_text=candidate_text,
                left_name=left_name,
                right_name=right_name,
                left_surface_index=left_index,
                right_surface_index=left_index + 1,
                left_type_hash=hash_canonical({"type": left_group.type_tokens}),
                right_type_hash=hash_canonical({"type": right_group.type_tokens}),
                p02_site_count=p02_site_count,
            )
        )
    return tuple(sorted(sites, key=lambda site: (site.start, site.end, site.stable_key)))


def _choose_site(
    sites: Sequence[BinderPermutationSite], *, theorem_id: str, seed: int
) -> BinderPermutationSite:
    if not sites:
        raise P14BinderPermutationError("no_eligible_surface_site")

    def rank(site: BinderPermutationSite) -> bytes:
        value = f"p14_independent_binder_permutation\0{theorem_id}\0{seed}\0{site.stable_key}"
        return hashlib.sha256(value.encode()).digest()

    return min(sites, key=rank)


def _trace(
    site: BinderPermutationSite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    expected = site.candidate_text if inverse else site.source_text
    replacement = site.source_text if inverse else site.candidate_text
    return (
        {
            "operation": "replace_exact_span",
            "p14_operation": f"{'inverse_' if inverse else ''}{site.operation}",
            "start": site.start,
            "end": site.start + len(expected),
            "expected_text": expected,
            "replacement_text": replacement,
            "left_name": site.left_name,
            "right_name": site.right_name,
            "surface_indices": [site.left_surface_index, site.right_surface_index],
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_p14_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    if len(trace) != 1 or trace[0].get("operation") != "replace_exact_span":
        raise P14BinderPermutationError("expected_one_replace_trace")
    step = trace[0]
    start, end = step.get("start"), step.get("end")
    expected, replacement = step.get("expected_text"), step.get("replacement_text")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or not isinstance(expected, str)
        or not isinstance(replacement, str)
        or not 0 <= start < end <= len(source)
    ):
        raise P14BinderPermutationError("invalid_replace_trace")
    if source[start:end] != expected:
        raise P14BinderPermutationError("trace_expected_text_mismatch")
    return source[:start] + replacement + source[end:]


def _expected_structural_diff(site: BinderPermutationSite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "adjacent_independent_binder_permutation",
        "evidence_class": "E2",
        "operation": site.operation,
        "left_name": site.left_name,
        "right_name": site.right_name,
        "selected_surface_indices": [site.left_surface_index, site.right_surface_index],
        "left_type_hash": site.left_type_hash,
        "right_type_hash": site.right_type_hash,
        "p02_site_count": site.p02_site_count,
    }


def _matching_tree_witnesses(
    source_root: dict[str, Any], candidate_root: dict[str, Any]
) -> tuple[_TreeWitness, ...]:
    binders, _tail = _outer_foralls(source_root)
    matches: list[_TreeWitness] = []
    candidate_bytes = alpha_canonical_bytes(candidate_root)
    for position in range(max(0, len(binders) - 1)):
        try:
            witness = _tree_witness_at(source_root, position)
        except P14BinderPermutationError:
            continue
        if alpha_canonical_bytes(witness.expected_candidate_root) == candidate_bytes:
            matches.append(witness)
    return tuple(matches)


def certify_binder_permutation(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    *,
    site: BinderPermutationSite,
    source_alpha_fingerprint: str,
    candidate_alpha_fingerprint: str,
) -> BinderPermutationCertificate:
    matches = _matching_tree_witnesses(source_root, candidate_root)
    if len(matches) != 1:
        raise P14BinderPermutationError("candidate_tree_match_not_unique")
    witness = matches[0]
    inverse_matches = _matching_tree_witnesses(candidate_root, source_root)
    if len(inverse_matches) != 1 or inverse_matches[0].position != witness.position:
        raise P14BinderPermutationError("tree_inverse_not_unique")
    if source_alpha_fingerprint == candidate_alpha_fingerprint:
        raise P14BinderPermutationError("alpha_fingerprint_delta_missing")
    inverse_residual = cast(
        dict[str, Any], _outer_foralls(candidate_root)[0][witness.position + 1]["body"]
    )
    inverse_residual_hash = hash_canonical(_swap_relative_zero_one(inverse_residual))
    source_residual_hash = hash_canonical(witness.residual)
    if inverse_residual_hash != source_residual_hash:
        raise P14BinderPermutationError("inverse_residual_mismatch")
    return BinderPermutationCertificate(
        operation=site.operation,
        selected_surface_indices=(site.left_surface_index, site.right_surface_index),
        selected_outer_indices=(witness.position, witness.position + 1),
        hidden_outer_offset=witness.position - site.left_surface_index,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        left_domain_hash=hash_canonical(witness.left_domain),
        right_domain_hash=hash_canonical(witness.right_domain),
        lowered_right_domain_hash=hash_canonical(witness.lowered_right_domain),
        raised_left_domain_hash=hash_canonical(witness.raised_left_domain),
        source_residual_hash=source_residual_hash,
        inverse_residual_hash=inverse_residual_hash,
        right_domain_free_bvars=tuple(sorted(_free_bvars(witness.right_domain))),
        left_data_evidence=witness.left_data_evidence,
        right_data_evidence=witness.right_data_evidence,
        left_referenced_outer_index=witness.left_referenced_outer_index,
        right_referenced_outer_index=witness.right_referenced_outer_index,
        source_alpha_fingerprint=source_alpha_fingerprint,
        candidate_alpha_fingerprint=candidate_alpha_fingerprint,
    )


class P14BinderPermutationRule:
    """One exact E2 adjacent binder swap; never a resolved F1 label."""

    polarity = Polarity.POSITIVE
    rule_id = "p14_independent_binder_permutation"
    family_id = "p14_independent_binder_permutation"
    implementation_key = "p14_independent_binder_permutation"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise P14BinderPermutationError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise P14BinderPermutationError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "p14_independent_binder_permutation_audit_v1",
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "generation_config_hash": generation_config_hash,
                "requirements": _REQUIRED_CAPABILITIES,
            }
        )

    def assess(self, theorem: TheoremRecord, representation: RepresentationRecord) -> Applicability:
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
        sites = enumerate_p14_surface_sites(theorem.proof_stripped_declaration)
        if not sites:
            reasons.append("no_eligible_surface_binder_pair")
        tree_eligible = 0
        if representation.operator_tree is not None:
            binders, _tail = _outer_foralls(_operator_root(representation))
            for position in range(max(0, len(binders) - 1)):
                try:
                    _tree_witness_at(_operator_root(representation), position)
                except P14BinderPermutationError:
                    continue
                tree_eligible += 1
        if tree_eligible == 0:
            reasons.append("no_eligible_elaborated_binder_pair")
        if reasons:
            return Applicability(
                applicable=False,
                reason_codes=tuple(sorted(set(reasons))),
                required_capabilities=_REQUIRED_CAPABILITIES,
            )
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=tuple(sorted(f"surface:{site.stable_key}" for site in sites)),
            required_capabilities=_REQUIRED_CAPABILITIES,
            metadata={
                "eligible_surface_site_count": len(sites),
                "tree_prefilter_count": tree_eligible,
            },
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> tuple[VariantDraft, ...]:
        if not self.assess(theorem, representation).applicable:
            return ()
        sites = enumerate_p14_surface_sites(theorem.proof_stripped_declaration)
        site = _choose_site(sites, theorem_id=theorem.theorem_id, seed=seed)
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_p14_trace(theorem.proof_stripped_declaration, forward)
        if apply_p14_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise P14BinderPermutationError("internal_inverse_replay_failure")
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
                intended_error_types=(),
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(site),
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "evidence_class": "E2",
                    "positive_intention_only": True,
                    "resolved_semantic_label": False,
                    "structural_direction": "swap_adjacent_independent_universal_binders",
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

        matching_sites: list[BinderPermutationSite] = []
        forward_ok = False
        inverse_ok = False
        try:
            matching_sites = [
                site
                for site in enumerate_p14_surface_sites(source.proof_stripped_declaration)
                if _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
                == draft.transformation_trace
                and _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
                == draft.inverse_trace
                and _expected_structural_diff(site) == draft.expected_structural_diff
            ]
            forward_ok = (
                apply_p14_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_p14_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (P14BinderPermutationError, TypeError, ValueError):
            pass
        if len(matching_sites) != 1:
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: BinderPermutationCertificate | None = None
        if len(matching_sites) == 1:
            source_fingerprint = source_representation.alpha_identity_fingerprint
            candidate_fingerprint = candidate_representation.alpha_identity_fingerprint
            if source_fingerprint is None or candidate_fingerprint is None:
                violations.append("alpha_fingerprint_missing")
            else:
                try:
                    source_root = _operator_root(source_representation)
                    candidate_root = _operator_root(candidate_representation)
                    recomputed_source_fingerprint = alpha_identity_fingerprint(source_root)
                    recomputed_candidate_fingerprint = alpha_identity_fingerprint(candidate_root)
                    if source_fingerprint != recomputed_source_fingerprint:
                        violations.append("source_alpha_fingerprint_mismatch")
                    if candidate_fingerprint != recomputed_candidate_fingerprint:
                        violations.append("candidate_alpha_fingerprint_mismatch")
                    certificate = certify_binder_permutation(
                        source_root,
                        candidate_root,
                        site=matching_sites[0],
                        source_alpha_fingerprint=recomputed_source_fingerprint,
                        candidate_alpha_fingerprint=recomputed_candidate_fingerprint,
                    )
                except P14BinderPermutationError as exc:
                    violations.append(str(exc))

        atom_mapping_ok = False
        if certificate is not None:
            source_root = _operator_root(source_representation)
            candidate_root = _operator_root(candidate_representation)
            atom_mapping_ok = source_representation.semantic_atoms == semantic_atoms(
                source_root
            ) and candidate_representation.semantic_atoms == semantic_atoms(candidate_root)
        if not atom_mapping_ok:
            violations.append("semantic_atom_representation_mismatch")

        clean = not violations
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("p14_exact_adjacent_forall_permutation",),
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
            atom_mapping_ok=atom_mapping_ok,
            inverse_or_roundtrip_ok=inverse_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "binder_permutation_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
                "evidence_class": "E2",
                "failed_proof_search_used": False,
                "hidden_outer_offset": (
                    certificate.hidden_outer_offset if certificate is not None else None
                ),
                "resolved_semantic_label": False,
                "structural_direction": "swap_adjacent_independent_universal_binders",
                "training_eligible": False,
            },
        )


__all__ = [
    "BinderPermutationCertificate",
    "BinderPermutationSite",
    "P14BinderPermutationError",
    "P14BinderPermutationRule",
    "apply_p14_trace",
    "build_binder_permutation_root",
    "certify_binder_permutation",
    "enumerate_p14_surface_sites",
]
