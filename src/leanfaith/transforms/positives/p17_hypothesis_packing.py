"""LF-033 P17: pack or unpack two final propositional hypotheses.

The first executable P17 profile is deliberately narrow.  It edits only the
final declaration-header binder(s): either two adjacent explicit singleton
proof binders ``(hP : P) (hQ : Q)`` become one fresh binder
``(h_p17 : P ∧ Q)``, or one explicit singleton binder over the exact root
``And P Q`` becomes two fresh singleton binders.  ``P`` and ``Q`` must be bare
references to earlier explicit proposition variables, the theorem body must
not depend on the proof terms, and no binder is crossed.

The elaborated expression-tree certificate performs the authoritative
de-Bruijn shifts and verifies the exact candidate tree.  Surface edits are
also exactly invertible.  P17 is E2 evidence only: it never emits a resolved
same-claim label, promotion, or training eligibility.
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import JsonValue

from leanfaith.config.hashing import hash_canonical
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
    parse_typed_binders,
)
from leanfaith.transforms.positives.v2_e0 import (
    V2E0RuleError,
    _signature_bounds,
    enumerate_p12_sites,
)
from leanfaith.transforms.protocol import (
    build_transformation_audit,
    build_variant_draft,
    verify_variant_draft_id,
)

PackingOperation = Literal["pack_two", "unpack_pair"]
_VALID_ELABORATION = frozenset(
    {ValidationStatus.ELABORATES, ValidationStatus.ELABORATES_WITH_PLACEHOLDER}
)
_REQUIRED_CAPABILITIES = (
    "exact_hypothesis_packing_certificate",
    "exact_inverse_replay",
    "exact_surface_expr_alignment",
    "final_header_site",
    "nondependent_propositional_hypotheses",
    "p12_span_disjointness",
    "preserved_hypothesis_order",
    "same_context_reelaboration",
)
_FRESH_PACKED_NAME = "h_p17"
_FRESH_LEFT_NAME = "h_p17_left"
_FRESH_RIGHT_NAME = "h_p17_right"
_BINDER_INFO = {
    BinderKind.EXPLICIT: "default",
    BinderKind.IMPLICIT: "implicit",
    BinderKind.STRICT_IMPLICIT: "strictImplicit",
    BinderKind.INSTANCE: "instImplicit",
}


class P17HypothesisPackingError(ValueError):
    """A P17 source, trace, or E2 certificate failed closed."""


@dataclass(frozen=True, slots=True)
class _SurfaceBinder:
    group: TypedBinder
    name: str


@dataclass(frozen=True, slots=True)
class HypothesisPackingSite:
    operation: PackingOperation
    start: int
    end: int
    source_text: str
    candidate_text: str
    header_binder_count: int
    left_proposition_text: str
    right_proposition_text: str
    left_proposition_hash: str
    right_proposition_hash: str
    common_residual_hash: str
    dependency_proof_hash: str
    ordered_role_atom_hash: str
    selected_surface_indices: tuple[int, ...]
    selected_outer_indices: tuple[int, ...]
    proposition_outer_indices: tuple[int, int]
    p12_site_count: int
    source_root_hash: str
    expected_candidate_root_hash: str

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "operation": self.operation,
                "start": self.start,
                "end": self.end,
                "source_text_hash": hash_canonical({"text": self.source_text}),
                "candidate_text_hash": hash_canonical({"text": self.candidate_text}),
                "header_binder_count": self.header_binder_count,
                "left_proposition_text": self.left_proposition_text,
                "right_proposition_text": self.right_proposition_text,
                "left_proposition_hash": self.left_proposition_hash,
                "right_proposition_hash": self.right_proposition_hash,
                "common_residual_hash": self.common_residual_hash,
                "dependency_proof_hash": self.dependency_proof_hash,
                "ordered_role_atom_hash": self.ordered_role_atom_hash,
                "selected_surface_indices": self.selected_surface_indices,
                "selected_outer_indices": self.selected_outer_indices,
                "proposition_outer_indices": self.proposition_outer_indices,
                "p12_site_count": self.p12_site_count,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class HypothesisPackingCertificate:
    operation: PackingOperation
    source_header_binder_count: int
    candidate_header_binder_count: int
    source_root_hash: str
    candidate_root_hash: str
    left_proposition_hash: str
    right_proposition_hash: str
    common_residual_hash: str
    dependency_proof_hash: str
    ordered_role_atom_hash: str
    selected_surface_indices: tuple[int, ...]
    selected_outer_indices: tuple[int, ...]
    proposition_outer_indices: tuple[int, int]

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "operation": self.operation,
            "source_header_binder_count": self.source_header_binder_count,
            "candidate_header_binder_count": self.candidate_header_binder_count,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "left_proposition_hash": self.left_proposition_hash,
            "right_proposition_hash": self.right_proposition_hash,
            "common_residual_hash": self.common_residual_hash,
            "dependency_proof_hash": self.dependency_proof_hash,
            "ordered_role_atom_hash": self.ordered_role_atom_hash,
            "selected_surface_indices": list(self.selected_surface_indices),
            "selected_outer_indices": list(self.selected_outer_indices),
            "proposition_outer_indices": list(self.proposition_outer_indices),
        }


@dataclass(frozen=True, slots=True)
class _TreePackingWitness:
    operation: PackingOperation
    expected_candidate_root: dict[str, Any]
    left_proposition: dict[str, Any]
    right_proposition: dict[str, Any]
    common_residual: dict[str, Any]
    common_residual_hash: str
    dependency_proof_hash: str
    ordered_role_atom_hash: str
    selected_surface_indices: tuple[int, ...]
    selected_outer_indices: tuple[int, ...]
    proposition_outer_indices: tuple[int, int]


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise P17HypothesisPackingError("operator_tree_missing_root")
    return cast(dict[str, Any], tree["root"])


def _surface_binders(source: str) -> tuple[_SurfaceBinder, ...]:
    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise P17HypothesisPackingError(str(exc)) from exc
    return tuple(_SurfaceBinder(group=group, name=name) for group in groups for name in group.names)


def _outer_foralls(
    root: dict[str, Any], count: int
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    if count < 0:
        raise P17HypothesisPackingError("negative_header_binder_count")
    nodes: list[dict[str, Any]] = []
    current = root
    for _ in range(count):
        if current.get("k") != "forall":
            raise P17HypothesisPackingError("header_binder_expr_mismatch")
        domain = current.get("dom")
        body = current.get("body")
        if not isinstance(domain, dict) or not isinstance(body, dict):
            raise P17HypothesisPackingError("malformed_header_forall")
        nodes.append(current)
        current = body
    return tuple(nodes), current


def _validate_surface_tree_alignment(
    surface: tuple[_SurfaceBinder, ...], nodes: tuple[dict[str, Any], ...]
) -> None:
    if len(surface) != len(nodes):
        raise P17HypothesisPackingError("surface_tree_binder_count_mismatch")
    for item, node in zip(surface, nodes, strict=True):
        if node.get("bi") != _BINDER_INFO[item.group.kind]:
            raise P17HypothesisPackingError("surface_tree_binder_kind_mismatch")


def _free_bvars(node: dict[str, Any], depth: int = 0) -> frozenset[int]:
    kind = node.get("k")
    if kind == "bvar":
        index = node.get("i")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise P17HypothesisPackingError("malformed_bound_variable")
        return frozenset({index - depth}) if index >= depth else frozenset()
    dependencies: set[int] = set()
    if kind in {"forall", "lam"}:
        domain = node.get("dom")
        body = node.get("body")
        if not isinstance(domain, dict) or not isinstance(body, dict):
            raise P17HypothesisPackingError("malformed_local_binder")
        dependencies.update(_free_bvars(domain, depth))
        dependencies.update(_free_bvars(body, depth + 1))
        return frozenset(dependencies)
    if kind == "let":
        for key in ("t", "v"):
            child = node.get(key)
            if not isinstance(child, dict):
                raise P17HypothesisPackingError("malformed_let_expression")
            dependencies.update(_free_bvars(child, depth))
        body = node.get("body")
        if not isinstance(body, dict):
            raise P17HypothesisPackingError("malformed_let_expression")
        dependencies.update(_free_bvars(body, depth + 1))
        return frozenset(dependencies)
    for key in ("fn", "arg", "base"):
        child = node.get(key)
        if isinstance(child, dict):
            dependencies.update(_free_bvars(child, depth))
    return frozenset(dependencies)


def _shift_free_bvars(
    node: dict[str, Any], *, delta: int, cutoff: int, depth: int = 0
) -> dict[str, Any]:
    """Shift free indices at or beyond ``cutoff`` relative to ``node``."""

    kind = node.get("k")
    if kind == "bvar":
        index = node.get("i")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise P17HypothesisPackingError("malformed_bound_variable")
        relative = index - depth
        if relative >= cutoff:
            shifted = index + delta
            if shifted < depth:
                raise P17HypothesisPackingError("bound_variable_shift_underflow")
            return {**node, "i": shifted}
        return dict(node)
    if kind in {"forall", "lam"}:
        domain = node.get("dom")
        body = node.get("body")
        if not isinstance(domain, dict) or not isinstance(body, dict):
            raise P17HypothesisPackingError("malformed_local_binder")
        return {
            **node,
            "dom": _shift_free_bvars(domain, delta=delta, cutoff=cutoff, depth=depth),
            "body": _shift_free_bvars(body, delta=delta, cutoff=cutoff, depth=depth + 1),
        }
    if kind == "let":
        type_node = node.get("t")
        value = node.get("v")
        body = node.get("body")
        if not all(isinstance(child, dict) for child in (type_node, value, body)):
            raise P17HypothesisPackingError("malformed_let_expression")
        return {
            **node,
            "t": _shift_free_bvars(
                cast(dict[str, Any], type_node), delta=delta, cutoff=cutoff, depth=depth
            ),
            "v": _shift_free_bvars(
                cast(dict[str, Any], value), delta=delta, cutoff=cutoff, depth=depth
            ),
            "body": _shift_free_bvars(
                cast(dict[str, Any], body), delta=delta, cutoff=cutoff, depth=depth + 1
            ),
        }
    updated = dict(node)
    for key in ("fn", "arg", "base"):
        child = node.get(key)
        if isinstance(child, dict):
            updated[key] = _shift_free_bvars(child, delta=delta, cutoff=cutoff, depth=depth)
    return updated


def _and(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    head: dict[str, Any] = {"k": "const", "n": "And", "us": "[]"}
    return {"k": "app", "fn": {"k": "app", "fn": head, "arg": left}, "arg": right}


def _and_parts(node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if node.get("k") != "app":
        raise P17HypothesisPackingError("packed_domain_not_exact_and")
    partial = node.get("fn")
    right = node.get("arg")
    if not isinstance(partial, dict) or not isinstance(right, dict):
        raise P17HypothesisPackingError("packed_domain_not_exact_and")
    head = partial.get("fn")
    left = partial.get("arg")
    if (
        partial.get("k") != "app"
        or not isinstance(head, dict)
        or head.get("k") != "const"
        or head.get("n") != "And"
        or not isinstance(left, dict)
    ):
        raise P17HypothesisPackingError("packed_domain_not_exact_and")
    return left, right


def _rebuild_prefix(prefix: tuple[dict[str, Any], ...], tail: dict[str, Any]) -> dict[str, Any]:
    result = tail
    for binder in reversed(prefix):
        result = {**binder, "body": result}
    return result


def _is_prop_sort(node: object) -> bool:
    return isinstance(node, dict) and node.get("k") == "sort" and node.get("u") == "0"


def _bare_prop_reference(
    node: dict[str, Any],
    *,
    binder_position: int,
    binders: tuple[dict[str, Any], ...],
    surface: tuple[_SurfaceBinder, ...],
) -> tuple[str, int]:
    if node.get("k") != "bvar":
        raise P17HypothesisPackingError("hypothesis_domain_not_bare_proposition_variable")
    index = node.get("i")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise P17HypothesisPackingError("malformed_hypothesis_domain_reference")
    referenced_position = binder_position - 1 - index
    if not 0 <= referenced_position < binder_position:
        raise P17HypothesisPackingError("hypothesis_domain_reference_out_of_scope")
    if not _is_prop_sort(binders[referenced_position].get("dom")):
        raise P17HypothesisPackingError("data_binder_excluded")
    return surface[referenced_position].name, referenced_position


def _build_pack_root(
    root: dict[str, Any], header_binder_count: int, surface: tuple[_SurfaceBinder, ...]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if header_binder_count < 2:
        raise P17HypothesisPackingError("pack_requires_two_final_binders")
    binders, body = _outer_foralls(root, header_binder_count)
    _validate_surface_tree_alignment(surface, binders)
    left_binder, right_binder = binders[-2:]
    if left_binder.get("bi") != "default" or right_binder.get("bi") != "default":
        raise P17HypothesisPackingError("instance_or_implicit_hypothesis_excluded")
    left_domain = cast(dict[str, Any], left_binder["dom"])
    right_domain = cast(dict[str, Any], right_binder["dom"])
    _left_name, left_position = _bare_prop_reference(
        left_domain,
        binder_position=header_binder_count - 2,
        binders=binders,
        surface=surface,
    )
    _right_name, right_position = _bare_prop_reference(
        right_domain,
        binder_position=header_binder_count - 1,
        binders=binders,
        surface=surface,
    )
    if left_position == right_position:
        raise P17HypothesisPackingError("duplicate_hypothesis_propositions_excluded")
    if 0 in _free_bvars(right_domain):
        raise P17HypothesisPackingError("right_hypothesis_depends_on_left_proof")
    if _free_bvars(body) & {0, 1}:
        raise P17HypothesisPackingError("theorem_body_depends_on_packed_proof")
    shifted_right = _shift_free_bvars(right_domain, delta=-1, cutoff=1)
    shifted_body = _shift_free_bvars(body, delta=-1, cutoff=2)
    packed = {
        "k": "forall",
        "bi": "default",
        "dom": _and(left_domain, shifted_right),
        "body": shifted_body,
    }
    return _rebuild_prefix(binders[:-2], packed), left_domain, shifted_right


def _build_unpack_root(
    root: dict[str, Any], header_binder_count: int, surface: tuple[_SurfaceBinder, ...]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if header_binder_count < 1:
        raise P17HypothesisPackingError("unpack_requires_one_final_binder")
    binders, body = _outer_foralls(root, header_binder_count)
    _validate_surface_tree_alignment(surface, binders)
    packed = binders[-1]
    if packed.get("bi") != "default":
        raise P17HypothesisPackingError("instance_or_implicit_hypothesis_excluded")
    left_domain, right_domain = _and_parts(cast(dict[str, Any], packed["dom"]))
    _left_name, left_position = _bare_prop_reference(
        left_domain,
        binder_position=header_binder_count - 1,
        binders=binders,
        surface=surface,
    )
    _right_name, right_position = _bare_prop_reference(
        right_domain,
        binder_position=header_binder_count - 1,
        binders=binders,
        surface=surface,
    )
    if left_position == right_position:
        raise P17HypothesisPackingError("duplicate_hypothesis_propositions_excluded")
    if 0 in _free_bvars(body):
        raise P17HypothesisPackingError("theorem_body_depends_on_packed_proof")
    shifted_right = _shift_free_bvars(right_domain, delta=1, cutoff=0)
    shifted_body = _shift_free_bvars(body, delta=1, cutoff=1)
    right_binder = {
        "k": "forall",
        "bi": "default",
        "dom": shifted_right,
        "body": shifted_body,
    }
    left_binder = {
        "k": "forall",
        "bi": "default",
        "dom": left_domain,
        "body": right_binder,
    }
    return _rebuild_prefix(binders[:-1], left_binder), left_domain, right_domain


def _tree_witness(
    root: dict[str, Any],
    header_binder_count: int,
    operation: PackingOperation,
    surface: tuple[_SurfaceBinder, ...],
) -> _TreePackingWitness:
    binders, body = _outer_foralls(root, header_binder_count)
    selected: tuple[int, ...]
    if operation == "pack_two":
        expected, left, right = _build_pack_root(root, header_binder_count, surface)
        source_right = cast(dict[str, Any], binders[-1]["dom"])
        _left_name, left_position = _bare_prop_reference(
            left,
            binder_position=header_binder_count - 2,
            binders=binders,
            surface=surface,
        )
        _right_name, right_position = _bare_prop_reference(
            source_right,
            binder_position=header_binder_count - 1,
            binders=binders,
            surface=surface,
        )
        common_residual = _shift_free_bvars(body, delta=-2, cutoff=2)
        selected = (header_binder_count - 2, header_binder_count - 1)
        dependency_payload = {
            "operation": operation,
            "left_domain_free_bvars": sorted(_free_bvars(left)),
            "right_domain_free_bvars_in_source_scope": sorted(_free_bvars(source_right)),
            "body_free_bvars_in_source_scope": sorted(_free_bvars(body)),
            "prohibited_right_dependency_indices": [0],
            "prohibited_body_dependency_indices": [0, 1],
            "right_dependency_clear": 0 not in _free_bvars(source_right),
            "body_dependency_clear": not bool(_free_bvars(body) & {0, 1}),
        }
    else:
        expected, left, right = _build_unpack_root(root, header_binder_count, surface)
        _left_name, left_position = _bare_prop_reference(
            left,
            binder_position=header_binder_count - 1,
            binders=binders,
            surface=surface,
        )
        _right_name, right_position = _bare_prop_reference(
            right,
            binder_position=header_binder_count - 1,
            binders=binders,
            surface=surface,
        )
        common_residual = _shift_free_bvars(body, delta=-1, cutoff=1)
        selected = (header_binder_count - 1,)
        dependency_payload = {
            "operation": operation,
            "left_domain_free_bvars": sorted(_free_bvars(left)),
            "right_domain_free_bvars": sorted(_free_bvars(right)),
            "body_free_bvars_in_source_scope": sorted(_free_bvars(body)),
            "prohibited_body_dependency_indices": [0],
            "body_dependency_clear": 0 not in _free_bvars(body),
        }
    role_atom_payload = {
        "P": list(semantic_atoms(left)),
        "Q": list(semantic_atoms(right)),
        "residual": list(semantic_atoms(common_residual)),
    }
    return _TreePackingWitness(
        operation=operation,
        expected_candidate_root=expected,
        left_proposition=left,
        right_proposition=right,
        common_residual=common_residual,
        common_residual_hash=hash_canonical(common_residual),
        dependency_proof_hash=hash_canonical(dependency_payload),
        ordered_role_atom_hash=hash_canonical(role_atom_payload),
        selected_surface_indices=selected,
        selected_outer_indices=selected,
        proposition_outer_indices=(left_position, right_position),
    )


def _candidate_common_residual(
    candidate_root: dict[str, Any],
    candidate_header_binder_count: int,
    operation: PackingOperation,
) -> dict[str, Any]:
    _binders, body = _outer_foralls(candidate_root, candidate_header_binder_count)
    if operation == "pack_two":
        return _shift_free_bvars(body, delta=-1, cutoff=1)
    return _shift_free_bvars(body, delta=-2, cutoff=2)


def build_hypothesis_packing_root(
    source_root: dict[str, Any],
    header_binder_count: int,
    operation: PackingOperation,
    *,
    surface_names: tuple[str, ...],
    surface_kinds: tuple[BinderKind, ...] | None = None,
) -> dict[str, Any]:
    """Build the exact expected tree for a validated P17 source.

    This public test helper accepts the expanded surface names and optional
    binder kinds.  Production site enumeration additionally binds the exact
    source spans and type tokens.
    """

    kinds = surface_kinds or (BinderKind.EXPLICIT,) * len(surface_names)
    if len(kinds) != len(surface_names):
        raise P17HypothesisPackingError("surface_name_kind_count_mismatch")
    synthetic = tuple(
        _SurfaceBinder(
            group=TypedBinder(
                index=index,
                kind=kind,
                start=0,
                end=1,
                opener="(",
                closer=")",
                names=(name,),
                type_text="Prop",
                type_tokens=("Prop",),
                original_text="()",
                has_comment=False,
                type_mentions_group_name=False,
            ),
            name=name,
        )
        for index, (name, kind) in enumerate(zip(surface_names, kinds, strict=True))
    )
    return _tree_witness(
        source_root,
        header_binder_count,
        operation,
        synthetic,
    ).expected_candidate_root


def certify_hypothesis_packing(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    header_binder_count: int,
    operation: PackingOperation,
    *,
    surface: tuple[_SurfaceBinder, ...],
) -> HypothesisPackingCertificate:
    witness = _tree_witness(source_root, header_binder_count, operation, surface)
    if operation == "pack_two":
        candidate_count = header_binder_count - 1
    else:
        candidate_count = header_binder_count + 1
    if alpha_canonical_bytes(witness.expected_candidate_root) != alpha_canonical_bytes(
        candidate_root
    ):
        raise P17HypothesisPackingError("candidate_not_exact_hypothesis_packing")
    candidate_residual = _candidate_common_residual(candidate_root, candidate_count, operation)
    if alpha_canonical_bytes(candidate_residual) != alpha_canonical_bytes(witness.common_residual):
        raise P17HypothesisPackingError("candidate_common_residual_mismatch")
    return HypothesisPackingCertificate(
        operation=operation,
        source_header_binder_count=header_binder_count,
        candidate_header_binder_count=candidate_count,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        left_proposition_hash=hash_canonical(witness.left_proposition),
        right_proposition_hash=hash_canonical(witness.right_proposition),
        common_residual_hash=witness.common_residual_hash,
        dependency_proof_hash=witness.dependency_proof_hash,
        ordered_role_atom_hash=witness.ordered_role_atom_hash,
        selected_surface_indices=witness.selected_surface_indices,
        selected_outer_indices=witness.selected_outer_indices,
        proposition_outer_indices=witness.proposition_outer_indices,
    )


def _identifier_occurs(source: str, name: str) -> bool:
    return re.search(rf"(?<![\w']){re.escape(name)}(?![\w'])", source) is not None


def _final_header_tail_is_exact(mask: str, end: int, conclusion_start: int) -> bool:
    return re.fullmatch(r"\s*:\s*", mask[end:conclusion_start]) is not None


def _pack_site(
    source: str,
    mask: str,
    conclusion_start: int,
    root: dict[str, Any],
    surface: tuple[_SurfaceBinder, ...],
    p12_site_count: int,
) -> HypothesisPackingSite:
    if len(surface) < 2:
        raise P17HypothesisPackingError("pack_requires_two_final_binders")
    left, right = surface[-2:]
    if left.group is right.group or len(left.group.names) != 1 or len(right.group.names) != 1:
        raise P17HypothesisPackingError("grouped_hypothesis_binder_excluded")
    if left.group.kind != BinderKind.EXPLICIT or right.group.kind != BinderKind.EXPLICIT:
        raise P17HypothesisPackingError("instance_or_implicit_hypothesis_excluded")
    if left.group.has_comment or right.group.has_comment:
        raise P17HypothesisPackingError("commented_hypothesis_binder_excluded")
    if source[left.group.end : right.group.start].strip():
        raise P17HypothesisPackingError("nonadjacent_final_hypotheses")
    if not _final_header_tail_is_exact(mask, right.group.end, conclusion_start):
        raise P17HypothesisPackingError("hypotheses_not_final_header_binders")
    witness = _tree_witness(root, len(surface), "pack_two", surface)
    left_domain = witness.left_proposition
    right_domain = witness.right_proposition
    binders, _body = _outer_foralls(root, len(surface))
    left_ref, _ = _bare_prop_reference(
        left_domain,
        binder_position=len(surface) - 2,
        binders=binders,
        surface=surface,
    )
    # ``right_domain`` was shifted into candidate scope.  Its source surface
    # is authoritative only after the source-tree check above.
    source_right_domain = cast(dict[str, Any], binders[-1]["dom"])
    right_ref, _ = _bare_prop_reference(
        source_right_domain,
        binder_position=len(surface) - 1,
        binders=binders,
        surface=surface,
    )
    if left.group.type_tokens != (left_ref,) or right.group.type_tokens != (right_ref,):
        raise P17HypothesisPackingError("surface_tree_hypothesis_type_mismatch")
    if _identifier_occurs(source, _FRESH_PACKED_NAME):
        raise P17HypothesisPackingError("fresh_packed_name_collision")
    replacement = f"({_FRESH_PACKED_NAME} : {left.group.type_text} ∧ {right.group.type_text})"
    return HypothesisPackingSite(
        operation="pack_two",
        start=left.group.start,
        end=right.group.end,
        source_text=source[left.group.start : right.group.end],
        candidate_text=replacement,
        header_binder_count=len(surface),
        left_proposition_text=left.group.type_text,
        right_proposition_text=right.group.type_text,
        left_proposition_hash=hash_canonical(left_domain),
        right_proposition_hash=hash_canonical(right_domain),
        common_residual_hash=witness.common_residual_hash,
        dependency_proof_hash=witness.dependency_proof_hash,
        ordered_role_atom_hash=witness.ordered_role_atom_hash,
        selected_surface_indices=witness.selected_surface_indices,
        selected_outer_indices=witness.selected_outer_indices,
        proposition_outer_indices=witness.proposition_outer_indices,
        p12_site_count=p12_site_count,
        source_root_hash=hash_canonical(root),
        expected_candidate_root_hash=hash_canonical(witness.expected_candidate_root),
    )


def _unpack_site(
    source: str,
    mask: str,
    conclusion_start: int,
    root: dict[str, Any],
    surface: tuple[_SurfaceBinder, ...],
    p12_site_count: int,
) -> HypothesisPackingSite:
    if not surface:
        raise P17HypothesisPackingError("unpack_requires_one_final_binder")
    packed = surface[-1]
    if len(packed.group.names) != 1:
        raise P17HypothesisPackingError("grouped_hypothesis_binder_excluded")
    if packed.group.kind != BinderKind.EXPLICIT:
        raise P17HypothesisPackingError("instance_or_implicit_hypothesis_excluded")
    if packed.group.has_comment:
        raise P17HypothesisPackingError("commented_hypothesis_binder_excluded")
    if not _final_header_tail_is_exact(mask, packed.group.end, conclusion_start):
        raise P17HypothesisPackingError("hypothesis_not_final_header_binder")
    witness = _tree_witness(root, len(surface), "unpack_pair", surface)
    left_domain = witness.left_proposition
    right_domain = witness.right_proposition
    binders, _body = _outer_foralls(root, len(surface))
    left_ref, _ = _bare_prop_reference(
        left_domain,
        binder_position=len(surface) - 1,
        binders=binders,
        surface=surface,
    )
    right_ref, _ = _bare_prop_reference(
        right_domain,
        binder_position=len(surface) - 1,
        binders=binders,
        surface=surface,
    )
    if packed.group.type_tokens != (left_ref, "∧", right_ref):
        raise P17HypothesisPackingError("surface_tree_packed_type_mismatch")
    if _identifier_occurs(source, _FRESH_LEFT_NAME) or _identifier_occurs(
        source, _FRESH_RIGHT_NAME
    ):
        raise P17HypothesisPackingError("fresh_unpacked_name_collision")
    replacement = f"({_FRESH_LEFT_NAME} : {left_ref}) ({_FRESH_RIGHT_NAME} : {right_ref})"
    return HypothesisPackingSite(
        operation="unpack_pair",
        start=packed.group.start,
        end=packed.group.end,
        source_text=packed.group.original_text,
        candidate_text=replacement,
        header_binder_count=len(surface),
        left_proposition_text=left_ref,
        right_proposition_text=right_ref,
        left_proposition_hash=hash_canonical(left_domain),
        right_proposition_hash=hash_canonical(right_domain),
        common_residual_hash=witness.common_residual_hash,
        dependency_proof_hash=witness.dependency_proof_hash,
        ordered_role_atom_hash=witness.ordered_role_atom_hash,
        selected_surface_indices=witness.selected_surface_indices,
        selected_outer_indices=witness.selected_outer_indices,
        proposition_outer_indices=witness.proposition_outer_indices,
        p12_site_count=p12_site_count,
        source_root_hash=hash_canonical(root),
        expected_candidate_root_hash=hash_canonical(witness.expected_candidate_root),
    )


def enumerate_p17_sites(
    source: str, operator_tree_view: dict[str, Any]
) -> tuple[HypothesisPackingSite, ...]:
    """Return one conservative final-header pack/unpack site, or no site."""

    try:
        p12_sites = enumerate_p12_sites(source)
        mask, conclusion_start, _conclusion_end = _signature_bounds(source)
        root = cast(dict[str, Any], operator_tree_view["root"])
        surface = _surface_binders(source)
        candidates: list[HypothesisPackingSite] = []
        with suppress(P17HypothesisPackingError):
            candidates.append(
                _pack_site(source, mask, conclusion_start, root, surface, len(p12_sites))
            )
        with suppress(P17HypothesisPackingError):
            candidates.append(
                _unpack_site(source, mask, conclusion_start, root, surface, len(p12_sites))
            )
        if len(candidates) != 1:
            raise P17HypothesisPackingError("expected_one_final_hypothesis_packing_site")
        site = candidates[0]
        if any(not (site.end <= p12.start or p12.end <= site.start) for p12 in p12_sites):
            raise P17HypothesisPackingError("p12_p17_surface_span_overlap")
        return (site,)
    except (
        BinderParseError,
        KeyError,
        P17HypothesisPackingError,
        TypeError,
        V2E0RuleError,
    ):
        return ()


def _trace(
    site: HypothesisPackingSite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    expected = site.candidate_text if inverse else site.source_text
    replacement = site.source_text if inverse else site.candidate_text
    return (
        {
            "operation": "replace_exact_span",
            "p17_operation": (
                f"inverse_{site.operation}_hypotheses"
                if inverse
                else f"{site.operation}_hypotheses"
            ),
            "start": site.start,
            "end": site.start + len(expected),
            "expected_text": expected,
            "replacement_text": replacement,
            "source_operation": site.operation,
            "header_binder_count": site.header_binder_count,
            "left_proposition_hash": site.left_proposition_hash,
            "right_proposition_hash": site.right_proposition_hash,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_p17_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    if len(trace) != 1 or trace[0].get("operation") != "replace_exact_span":
        raise P17HypothesisPackingError("expected_one_replace_trace")
    step = trace[0]
    start, end = step.get("start"), step.get("end")
    expected, replacement = step.get("expected_text"), step.get("replacement_text")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not isinstance(expected, str)
        or not isinstance(replacement, str)
        or not 0 <= start < end <= len(source)
    ):
        raise P17HypothesisPackingError("invalid_replace_trace")
    if source[start:end] != expected:
        raise P17HypothesisPackingError("trace_expected_text_mismatch")
    return source[:start] + replacement + source[end:]


def _expected_structural_diff(site: HypothesisPackingSite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "final_hypothesis_packing",
        "evidence_class": "E2",
        "operation": site.operation,
        "source_header_binder_count": site.header_binder_count,
        "candidate_header_binder_count": (
            site.header_binder_count - 1
            if site.operation == "pack_two"
            else site.header_binder_count + 1
        ),
        "left_proposition_hash": site.left_proposition_hash,
        "right_proposition_hash": site.right_proposition_hash,
        "common_residual_hash": site.common_residual_hash,
        "dependency_proof_hash": site.dependency_proof_hash,
        "ordered_role_atom_hash": site.ordered_role_atom_hash,
        "selected_surface_indices": list(site.selected_surface_indices),
        "selected_outer_indices": list(site.selected_outer_indices),
        "proposition_outer_indices": list(site.proposition_outer_indices),
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
        "hypothesis_order_preserved": True,
        "p12_site_count": site.p12_site_count,
        "p12_surface_span_disjoint": True,
    }


class P17HypothesisPackingRule:
    """One exact E2 final-header packing step; never an F1 label."""

    polarity = Polarity.POSITIVE
    rule_id = "p17_hypothesis_packing"
    family_id = "p17_hypothesis_packing"
    implementation_key = "p17_hypothesis_packing"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise P17HypothesisPackingError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise P17HypothesisPackingError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "p17_hypothesis_packing_audit_v1",
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
        sites = (
            enumerate_p17_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if len(sites) != 1:
            reasons.append("no_unique_final_hypothesis_packing_site")
        if reasons:
            return Applicability(
                applicable=False,
                reason_codes=tuple(sorted(set(reasons))),
                required_capabilities=_REQUIRED_CAPABILITIES,
            )
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=(f"final_hypotheses:{sites[0].stable_key}",),
            required_capabilities=_REQUIRED_CAPABILITIES,
            metadata={"eligible_site_count": 1, "p17_operation": sites[0].operation},
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> tuple[VariantDraft, ...]:
        if not self.assess(theorem, representation).applicable:
            return ()
        (site,) = enumerate_p17_sites(
            theorem.proof_stripped_declaration,
            cast(dict[str, Any], representation.operator_tree),
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_p17_trace(theorem.proof_stripped_declaration, forward)
        if apply_p17_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise P17HypothesisPackingError("internal_inverse_replay_failure")
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
                    "positive_intention_only": True,
                    "resolved_semantic_label": False,
                    "structural_direction": f"{site.operation}_final_hypotheses",
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

        matching: list[HypothesisPackingSite] = []
        forward_ok = False
        inverse_ok = False
        try:
            sites = enumerate_p17_sites(
                source.proof_stripped_declaration,
                cast(dict[str, Any], source_representation.operator_tree),
            )
            matching = [
                site
                for site in sites
                if _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
                == draft.transformation_trace
                and _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
                == draft.inverse_trace
                and _expected_structural_diff(site) == draft.expected_structural_diff
            ]
            forward_ok = (
                apply_p17_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_p17_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (P17HypothesisPackingError, TypeError, ValueError):
            pass
        if len(matching) != 1:
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: HypothesisPackingCertificate | None = None
        expected_candidate_root: dict[str, Any] | None = None
        if len(matching) == 1:
            try:
                surface = _surface_binders(source.proof_stripped_declaration)
                certificate = certify_hypothesis_packing(
                    _operator_root(source_representation),
                    _operator_root(candidate_representation),
                    matching[0].header_binder_count,
                    matching[0].operation,
                    surface=surface,
                )
                expected_candidate_root = _tree_witness(
                    _operator_root(source_representation),
                    matching[0].header_binder_count,
                    matching[0].operation,
                    surface,
                ).expected_candidate_root
            except P17HypothesisPackingError as exc:
                violations.append(str(exc))

        source_atoms = source_representation.semantic_atoms
        candidate_atoms = candidate_representation.semantic_atoms
        atom_mapping_ok = (
            certificate is not None
            and expected_candidate_root is not None
            and source_atoms == semantic_atoms(_operator_root(source_representation))
            and candidate_atoms == semantic_atoms(expected_candidate_root)
        )
        if not atom_mapping_ok:
            violations.append("semantic_atom_packing_mismatch")
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
                matched_nodes=("p17_exact_final_hypothesis_packing",),
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
                "evidence_class": "E2",
                "failed_proof_search_used": False,
                "hypothesis_packing_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
                "hypothesis_order_preserved": certificate is not None,
                "p12_site_count": matching[0].p12_site_count if len(matching) == 1 else None,
                "p12_surface_span_disjoint": certificate is not None,
                "resolved_semantic_label": False,
                "structural_direction": (
                    f"{matching[0].operation}_final_hypotheses" if len(matching) == 1 else None
                ),
                "training_eligible": False,
            },
        )


__all__ = [
    "HypothesisPackingCertificate",
    "HypothesisPackingSite",
    "P17HypothesisPackingError",
    "P17HypothesisPackingRule",
    "apply_p17_trace",
    "build_hypothesis_packing_root",
    "certify_hypothesis_packing",
    "enumerate_p17_sites",
]
