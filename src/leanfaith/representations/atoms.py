"""Semantic atoms and operator tree from the Expr JSON (PLAN.md §13.5/§13.6, LF-015).

Pure functions over the compact Expr JSON emitted by ``LeanFaith/Meta/
ExprJson.lean``. ``semantic_atoms`` is the ordered sequence of substantive
tokens (quantifiers, constant heads, literals) walking the tree; the
``operator_tree`` is the structural tree for GTED/TransTED. Both are versioned.
"""

from __future__ import annotations

from typing import Any

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


def parse_lfjson_line(line: str) -> tuple[str, dict[str, Any] | None]:
    """Parse an ``LFJSON {"name":..., "tree":...|"notfound":true}`` emit line
    into (name, tree-or-None). The name lives inside the JSON so names
    containing spaces (guillemet identifiers) are unambiguous."""
    import json

    payload = line.split("LFJSON ", 1)[1] if "LFJSON " in line else line
    try:
        obj = json.loads(payload.strip())
    except json.JSONDecodeError:
        return "", None
    if not isinstance(obj, dict):
        return "", None
    name = str(obj.get("name", ""))
    tree = obj.get("tree")
    if obj.get("notfound") or not isinstance(tree, dict):
        return name, None
    return name, tree
