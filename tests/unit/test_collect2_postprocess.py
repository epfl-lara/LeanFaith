"""Real-envelope extraction, golden screening, and dedup for collect2."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.collect2.postprocess import (
    CandidateRejected,
    GoldenBlocklist,
    postprocess_candidate,
)
from leanfaith.representations.views import normalize_headless, signature_near_dup_hash


def _empty_blocklist() -> GoldenBlocklist:
    return GoldenBlocklist(frozenset(), frozenset(), frozenset())


def test_stepfun_real_think_wrapper_extracts_terminal_declaration() -> None:
    raw = """# Analysis
The statement uses a topological subspace.
</think>```Lean4
import Mathlib
theorem stepfun_fixture {X : Type*} [TopologicalSpace X]
    (A : Set X) (hA : IsClosed A) : IsClosed A := by
  sorry
```"""
    result = postprocess_candidate(
        raw,
        problem_id="fixture-stepfun",
        registered_header="import Mathlib",
        blocklist=_empty_blocklist(),
        family="stepfun",
        expected_declaration_name="stepfun_fixture",
    )
    assert result.candidate_statement == (
        "theorem stepfun_fixture {X : Type*} [TopologicalSpace X]\n"
        "    (A : Set X) (hA : IsClosed A) : IsClosed A"
    )
    assert result.candidate_lean.endswith(" := by sorry")
    assert result.candidate_headless == (
        "{X : Type*} [TopologicalSpace X] (A : Set X) (hA : IsClosed A) : IsClosed A"
    )


def test_goedel_real_reasoning_uses_last_fence_and_safe_preamble() -> None:
    raw = """<think>An abandoned sketch follows.
```lean4
theorem scratch : True := by sorry
```
</think>Final answer:
```lean4
import Mathlib
import Aesop

set_option maxHeartbeats 0

open BigOperators Real Nat Topology Rat

theorem goedel_fixture (n : ℕ) (x : Fin n → ℝ) :
    (∀ i, x i = x i) := by sorry
```"""
    result = postprocess_candidate(
        raw,
        problem_id="fixture-goedel",
        registered_header="import Mathlib",
        blocklist=_empty_blocklist(),
        family="goedel",
        expected_declaration_name="goedel_fixture",
    )
    assert result.candidate_statement == (
        "theorem goedel_fixture (n : ℕ) (x : Fin n → ℝ) :\n    (∀ i, x i = x i)"
    )


def test_kimina_real_raw_header_and_proof_are_removed() -> None:
    raw = """import Mathlib
open Real Set
open scoped BigOperators

theorem kimina_fixture (p : ℝ) (hp : p ∈ Icc 0 1) : p ≤ 1 := by sorry"""
    header = "import Mathlib\nopen Real Set\nopen scoped BigOperators"
    result = postprocess_candidate(
        raw,
        problem_id="fixture-kimina",
        registered_header=header,
        blocklist=_empty_blocklist(),
        family="kimina",
        expected_declaration_name="kimina_fixture",
    )
    assert result.candidate_statement == (
        "theorem kimina_fixture (p : ℝ) (hp : p ∈ Icc 0 1) : p ≤ 1"
    )


def test_proof_free_cli_fence_gets_controlled_placeholder() -> None:
    result = postprocess_candidate(
        "```lean4\nlemma cli_fixture (p : Prop) : p → p\n```",
        problem_id="fixture-cli",
        registered_header="import Mathlib",
        blocklist=_empty_blocklist(),
        expected_declaration_name="cli_fixture",
    )
    assert result.candidate_lean == "lemma cli_fixture (p : Prop) : p → p := by sorry"
    assert result.candidate_headless == "(p : Prop) : p → p"


def test_blocklist_rejects_candidate_hash_and_bare_group_name(tmp_path: Path) -> None:
    candidate = "theorem generated : 2 + 2 = 4 := by sorry"
    headless = normalize_headless(candidate)
    assert headless is not None
    blocklist_path = tmp_path / "blocklist.json"
    blocklist_path.write_text(
        json.dumps(
            {
                "version": ["golden_blocklist_v1"],
                "near_dup_hashes": [signature_near_dup_hash(headless)],
                "group_keys": ["proofnet::exercise_4_5a"],
            }
        ),
        encoding="utf-8",
    )
    blocklist = GoldenBlocklist.load(blocklist_path)
    with pytest.raises(CandidateRejected, match="golden_hash") as hash_error:
        postprocess_candidate(
            candidate,
            problem_id="not-golden",
            registered_header="",
            blocklist=blocklist,
        )
    assert hash_error.value.code == "golden_hash"

    with pytest.raises(CandidateRejected, match="golden_problem") as name_error:
        postprocess_candidate(
            "theorem fresh : 3 + 3 = 6 := by sorry",
            problem_id="Rudin|exercise_4_5a",
            registered_header="",
            blocklist=blocklist,
        )
    assert name_error.value.code == "golden_problem"


def test_dedup_uses_name_invariant_headless_hash() -> None:
    seen: set[str] = set()
    first = postprocess_candidate(
        "theorem alpha (p : Prop) : p → p := by sorry",
        problem_id="alpha",
        registered_header="",
        blocklist=_empty_blocklist(),
        seen_hashes=seen,
    )
    with pytest.raises(CandidateRejected, match="duplicate") as error:
        postprocess_candidate(
            "lemma beta (p : Prop) : p → p := by sorry",
            problem_id="beta",
            registered_header="",
            blocklist=_empty_blocklist(),
            seen_hashes=seen,
        )
    assert error.value.code == "duplicate"
    assert seen == {first.near_dup_hash}


def test_truncated_declaration_is_rejected() -> None:
    with pytest.raises(CandidateRejected, match="truncated"):
        postprocess_candidate(
            "theorem broken (p : Prop) : p →",
            problem_id="broken",
            registered_header="",
            blocklist=_empty_blocklist(),
        )
