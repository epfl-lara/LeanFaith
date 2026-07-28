"""Semantic atoms and operator tree from the Expr JSON (PLAN.md §13.5/§13.6, LF-015).

Pure functions over the compact Expr JSON emitted by ``LeanFaith/Meta/
ExprJson.lean``. ``semantic_atoms`` is the ordered sequence of substantive
tokens (quantifiers, constant heads, literals) walking the tree; the
``operator_tree`` is the structural tree for GTED/TransTED. Both are versioned.
"""

from __future__ import annotations

from typing import Any

from leanfaith.representations.views import collapse_lean_whitespace

ATOM_VERSION = "atoms_v1"

#: Expr node kinds that contribute a substantive atom token (§13.6). Structural
#: nodes (app/bvar/fvar/sort/mvar) are captured by the operator tree, not atoms.
_QUANTIFIER_KINDS = {"forall": "forall", "lam": "lam"}


#: Ordered child keys per node kind (pre-order traversal order). Iterative to
#: avoid Python recursion limits on deep mathlib application spines.
_CHILD_KEYS = ("dom", "body", "fn", "arg", "base", "t", "v")


def semantic_atoms(tree: dict[str, Any]) -> tuple[str, ...]:
    """Ordered substantive-atom sequence (§13.6): quantifiers, constant heads,
    and literals in traversal order. The multiset is ``collections.Counter``
    of this; the ordered form preserves position for atom-diff audits."""
    out: list[str] = []
    stack: list[dict[str, Any]] = [tree]
    while stack:
        node = stack.pop()
        kind = node.get("k")
        if kind in _QUANTIFIER_KINDS:
            out.append(_QUANTIFIER_KINDS[kind])
            stack.append(node.get("body", {}))
            stack.append(node.get("dom", {}))
        elif kind == "app":
            stack.append(node.get("arg", {}))
            stack.append(node.get("fn", {}))
        elif kind == "const":
            out.append(f"const:{node.get('n', '')}")
        elif kind == "lit":
            if "nat" in node:
                out.append(f"lit:nat:{node['nat']}")
            elif "str" in node:
                out.append("lit:str")
        elif kind == "proj":
            # Include the field index so a projection-index mutation
            # (.1 vs .2, an N07 index change) produces an atom-diff.
            out.append(f"proj:{node.get('s', '')}:{node.get('i', '')}")
            stack.append(node.get("base", {}))
        elif kind == "let":
            stack.append(node.get("body", {}))
            stack.append(node.get("v", {}))
            stack.append(node.get("t", {}))
        # bvar/fvar/sort/mvar: structural, no atom
    return tuple(out)


def _tree_stats(tree: dict[str, Any]) -> tuple[int, int]:
    """(node_count, depth) of the operator tree, computed iteratively."""
    node_count = 0
    max_depth = 0
    stack: list[tuple[dict[str, Any], int]] = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        node_count += 1
        max_depth = max(max_depth, depth)
        for key in _CHILD_KEYS:
            child = node.get(key)
            if isinstance(child, dict):
                stack.append((child, depth + 1))
    return node_count, max_depth


def operator_tree(tree: dict[str, Any]) -> dict[str, Any]:
    """The structural operator tree (§13.5) with a stats summary for
    GTED/TransTED. Stored verbatim as the elaborated Expr structure plus a
    lightweight header identifying the atom/serialization version."""
    node_count, depth = _tree_stats(tree)
    return {
        "atom_version": ATOM_VERSION,
        "node_count": node_count,
        "depth": depth,
        "root": tree,
    }


def parse_lfjson_payload(
    line: str,
) -> tuple[str, dict[str, Any] | None, str | None, str | None]:
    """Parse one current or legacy ``LFJSON`` payload.

    ``signature_pp`` and ``signature_explicit`` were added in ``repr_v3``.
    Their absence remains valid for replaying historical ``repr_v2`` helper
    output. The name lives inside the JSON so names containing spaces
    (guillemet identifiers) are unambiguous.
    """
    import json

    payload = line.split("LFJSON ", 1)[1] if "LFJSON " in line else line
    try:
        obj = json.loads(payload.strip())
    except json.JSONDecodeError:
        return "", None, None, None
    if not isinstance(obj, dict):
        return "", None, None, None
    name = str(obj.get("name", ""))
    tree = obj.get("tree")
    signature_pp = obj.get("signature_pp")
    signature_explicit = obj.get("signature_explicit")
    # Match ``parse_check_type``: Lean may pretty-print a long type over
    # several lines, but representation signatures are stored in a canonical
    # whitespace-collapsed form regardless of whether they came from
    # ``#check`` or direct environment pretty-printing.
    parsed_pp = collapse_lean_whitespace(signature_pp) if isinstance(signature_pp, str) else None
    parsed_explicit = (
        collapse_lean_whitespace(signature_explicit)
        if isinstance(signature_explicit, str)
        else None
    )
    if obj.get("notfound"):
        return name, None, None, None
    return (
        name,
        tree if isinstance(tree, dict) else None,
        parsed_pp or None,
        parsed_explicit or None,
    )


def _parse_prefixed_json(line: str, prefix: str) -> dict[str, Any] | None:
    """Parse one helper-emitted, prefix-delimited compact JSON object."""

    import json

    payload = line.split(prefix, 1)[1] if prefix in line else line
    try:
        obj = json.loads(payload.strip())
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_lfsignature_payload(
    line: str,
    *,
    prefix: str,
    field: str,
) -> tuple[str, str | None]:
    """Parse one independently emitted private-signature helper response."""

    obj = _parse_prefixed_json(line, prefix)
    if obj is None:
        return "", None
    name = str(obj.get("name", ""))
    if obj.get("notfound"):
        return name, None
    signature = obj.get(field)
    if not isinstance(signature, str):
        return name, None
    return name, collapse_lean_whitespace(signature) or None


def parse_lftree_payload(line: str) -> tuple[str, dict[str, Any] | None]:
    """Parse one independently emitted expression-tree helper response."""

    obj = _parse_prefixed_json(line, "LFTREEJSON ")
    if obj is None:
        return "", None
    name = str(obj.get("name", ""))
    tree = obj.get("tree")
    if obj.get("notfound") or not isinstance(tree, dict):
        return name, None
    return name, tree


def parse_lfjson_line(line: str) -> tuple[str, dict[str, Any] | None]:
    """Parse an ``LFJSON`` line into the legacy ``(name, tree)`` API."""

    name, tree, _signature_pp, _signature_explicit = parse_lfjson_payload(line)
    return name, tree
