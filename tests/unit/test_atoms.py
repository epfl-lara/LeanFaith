"""LF-015: semantic-atom and operator-tree extraction from Expr JSON (pure)."""

from __future__ import annotations

from leanfaith.representations.atoms import (
    ATOM_VERSION,
    operator_tree,
    parse_lfjson_line,
    semantic_atoms,
)

# ∀ (n : Nat), n + 0 = n  — a small real-shaped Expr tree (Nat.add / Eq / OfNat).
_TREE = {
    "k": "forall",
    "dom": {"k": "const", "n": "Nat"},
    "body": {
        "k": "app",
        "fn": {
            "k": "app",
            "fn": {"k": "const", "n": "Eq"},
            "arg": {
                "k": "app",
                "fn": {
                    "k": "app",
                    "fn": {"k": "const", "n": "HAdd.hAdd"},
                    "arg": {"k": "bvar", "i": 0},
                },
                "arg": {"k": "lit", "nat": "0"},
            },
        },
        "arg": {"k": "bvar", "i": 0},
    },
}


def test_semantic_atoms_ordered_sequence() -> None:
    atoms = semantic_atoms(_TREE)
    # Quantifier first, then constants/literals in traversal order; bvars omitted.
    assert atoms[0] == "forall"
    assert "const:Nat" in atoms
    assert "const:Eq" in atoms
    assert "const:HAdd.hAdd" in atoms
    assert "lit:nat:0" in atoms
    assert not any(a.startswith("bvar") for a in atoms)


def test_semantic_atoms_multiset_detects_operator_change() -> None:
    import collections

    base = collections.Counter(semantic_atoms(_TREE))
    mutated = {**_TREE}
    mutated_body = dict(_TREE["body"])  # swap Eq -> Ne at the head
    mutated_body["fn"] = {**_TREE["body"]["fn"], "fn": {"k": "const", "n": "Ne"}}
    mutated["body"] = mutated_body
    mut = collections.Counter(semantic_atoms(mutated))
    # The atom multiset differs exactly by the changed head constant.
    assert base != mut
    assert (base - mut) == collections.Counter({"const:Eq": 1})
    assert (mut - base) == collections.Counter({"const:Ne": 1})


def test_operator_tree_stats_and_version() -> None:
    tree = operator_tree(_TREE)
    assert tree["atom_version"] == ATOM_VERSION
    assert tree["node_count"] > 5
    assert tree["depth"] >= 4
    assert tree["root"] == _TREE


def test_parse_lfjson_line() -> None:
    name, tree = parse_lfjson_line('LFJSON {"name":"Nat.add_comm","tree":{"k":"const","n":"X"}}')
    assert name == "Nat.add_comm"
    assert tree == {"k": "const", "n": "X"}


def test_parse_lfjson_notfound() -> None:
    name, tree = parse_lfjson_line('LFJSON {"name":"Missing.decl","notfound":true}')
    assert name == "Missing.decl"
    assert tree is None


def test_parse_lfjson_name_with_space() -> None:
    # Guillemet identifiers can contain spaces; the name lives inside the JSON.
    name, tree = parse_lfjson_line('LFJSON {"name":"«foo bar»","tree":{"k":"sort"}}')
    assert name == "«foo bar»"
    assert tree == {"k": "sort"}


def test_parse_lfjson_malformed_json() -> None:
    name, tree = parse_lfjson_line("LFJSON {not valid json")
    assert name == ""
    assert tree is None


def test_deep_expr_does_not_blow_recursion() -> None:
    # A 5000-deep application spine must not hit the Python recursion limit.
    node: dict = {"k": "const", "n": "base"}
    for _ in range(5000):
        node = {"k": "app", "fn": node, "arg": {"k": "const", "n": "arg"}}
    atoms = semantic_atoms(node)
    assert atoms.count("const:arg") == 5000
    assert operator_tree(node)["node_count"] == 10001
