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


def _walk_atoms(node: dict[str, Any], out: list[str]) -> None:
    kind = node.get("k")
    if kind in _QUANTIFIER_KINDS:
        out.append(_QUANTIFIER_KINDS[kind])
        _walk_atoms(node.get("dom", {}), out)
        _walk_atoms(node.get("body", {}), out)
    elif kind == "app":
        _walk_atoms(node.get("fn", {}), out)
        _walk_atoms(node.get("arg", {}), out)
    elif kind == "const":
        out.append(f"const:{node.get('n', '')}")
    elif kind == "lit":
        if "nat" in node:
            out.append(f"lit:nat:{node['nat']}")
        elif "str" in node:
            out.append("lit:str")
    elif kind == "proj":
        out.append(f"proj:{node.get('s', '')}")
        _walk_atoms(node.get("base", {}), out)
    elif kind == "let":
        _walk_atoms(node.get("t", {}), out)
        _walk_atoms(node.get("v", {}), out)
        _walk_atoms(node.get("body", {}), out)
    # bvar/fvar/sort/mvar: structural, no atom


def semantic_atoms(tree: dict[str, Any]) -> tuple[str, ...]:
    """Ordered substantive-atom sequence (§13.6): quantifiers, constant heads,
    and literals in traversal order. The multiset is ``collections.Counter``
    of this; the ordered form preserves position for atom-diff audits."""
    out: list[str] = []
    _walk_atoms(tree, out)
    return tuple(out)


def _tree_stats(node: dict[str, Any]) -> tuple[int, int]:
    """(node_count, depth) of the operator tree."""
    children = []
    for key in ("dom", "body", "fn", "arg", "base", "t", "v"):
        child = node.get(key)
        if isinstance(child, dict):
            children.append(child)
    if not children:
        return 1, 1
    counts_depths = [_tree_stats(c) for c in children]
    return 1 + sum(c for c, _ in counts_depths), 1 + max(d for _, d in counts_depths)


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
    """Parse an ``LFJSON <name> <json|notfound>`` emit line into
    (name, tree-or-None)."""
    import json

    payload = line.split("LFJSON ", 1)[1] if "LFJSON " in line else line
    name, _, rest = payload.partition(" ")
    rest = rest.strip()
    if rest == "notfound" or not rest:
        return name, None
    try:
        return name, json.loads(rest)
    except json.JSONDecodeError:
        return name, None
