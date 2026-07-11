"""LF-013: benchmark denylist freeze and membership (§19.4, §19.7)."""

from __future__ import annotations

import datetime
from pathlib import Path

from leanfaith.datasets import (
    DenylistIndex,
    FrozenRegistry,
    load_frozen_registry,
    normalize_lean,
    normalize_nl,
    write_frozen_registry,
)
from leanfaith.datasets.denylist import (
    build_proofnetverif,
    unresolved_benchmark,
)

_UTC = datetime.datetime(2026, 7, 11, tzinfo=datetime.UTC)

_PNV_ROWS = {
    "valid": [
        {
            "id": "Rudin|ex1",
            "nl_statement": "If r is rational and x irrational, r + x is irrational.",
            "lean4_formalization": "theorem ex1 (x : R) : Irrational x := by sorry",
            "lean4_prediction": "theorem ex1 (x : R) : Irrational (x + 1) := by sorry",
        }
    ],
    "test": [
        {
            "id": "Herstein|ex2",
            "nl_statement": "Every group of prime order is cyclic.",
            "lean4_formalization": "theorem ex2 : True := trivial",
            "lean4_prediction": "theorem ex2 : True := trivial",
        }
    ],
}


def test_normalization_is_aggressive_on_nl_case_preserving_on_lean() -> None:
    assert normalize_nl("  Foo   BAR\n baz ") == "foo bar baz"
    assert normalize_lean("theorem  T :\n  X") == "theorem T : X"
    # Lean identifiers are case sensitive; NL is folded.
    assert normalize_nl("Prime") == normalize_nl("prime")
    assert normalize_lean("Prime") != normalize_lean("prime")


def test_build_proofnetverif_freezes_nl_and_both_lean_sides() -> None:
    frozen = build_proofnetverif(_PNV_ROWS, source_id="PAug/ProofNetVerif", revision="91183e5b")
    assert frozen.resolved
    assert frozen.splits == {"valid": 1, "test": 1}
    assert frozen.row_ids == ("test:Herstein|ex2", "valid:Rudin|ex1")
    assert len(frozen.nl_hashes) == 2
    # valid row has two distinct Lean sides; test row's two sides are identical.
    assert len(frozen.text_hashes) == 3


def test_registry_round_trip(tmp_path: Path) -> None:
    frozen = build_proofnetverif(_PNV_ROWS, source_id="PAug/ProofNetVerif", revision="91183e5b")
    registry = FrozenRegistry(frozen_at=_UTC, benchmarks=(frozen,))
    path = tmp_path / "frozen_ids.json"
    digest = write_frozen_registry(registry, path)
    assert len(digest) == 64
    loaded = load_frozen_registry(path)
    assert loaded == registry


def test_denylist_index_membership() -> None:
    frozen = build_proofnetverif(_PNV_ROWS, source_id="PAug/ProofNetVerif", revision="91183e5b")
    index = DenylistIndex(FrozenRegistry(frozen_at=_UTC, benchmarks=(frozen,)))
    # Exact NL, plus a whitespace/case-reformatted variant, both match.
    assert index.contains_nl("If r is rational and x irrational, r + x is irrational.")
    assert index.contains_nl("IF R IS RATIONAL  and x irrational,\nr + x is irrational.")
    assert not index.contains_nl("A completely unrelated statement.")
    assert index.contains_lean("theorem ex1 (x : R) : Irrational x := by sorry")
    assert index.contains_any(nl="Every group of prime order is cyclic.")


def test_unresolved_benchmark_carries_no_hashes() -> None:
    entry = unresolved_benchmark("con_nf", "resolve from Liu et al. ICLR 2025 release at Phase 11")
    assert not entry.resolved
    assert entry.nl_hashes == ()
    assert "Phase 11" in entry.resolution_plan
