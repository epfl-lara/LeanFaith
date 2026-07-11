"""LF-014: representation views (pure) and near-duplicate registry append."""

from __future__ import annotations

import datetime

import pytest

from leanfaith.datasets import (
    DenylistIndex,
    FrozenRegistry,
    append_representation_signatures,
)
from leanfaith.datasets.denylist import unresolved_benchmark
from leanfaith.representations import (
    normalize_headless,
    parse_check_type,
    representation_content_hash,
    signature_near_dup_hash,
)
from leanfaith.representations.views import check_command

_UTC = datetime.datetime(2026, 7, 11, tzinfo=datetime.UTC)

# --- headless normalization ---


def test_headless_drops_name_proof_comments() -> None:
    src = "/-- doc -/\n@[simp] theorem foo (x y : Nat) : x + y = y + x := by sorry"
    assert normalize_headless(src) == "(x y : Nat) : x + y = y + x"


def test_headless_renaming_invariant() -> None:
    a = normalize_headless("theorem foo (n : Nat) : n + 0 = n := by sorry")
    b = normalize_headless("theorem bar (n : Nat) : n + 0 = n := by sorry")
    assert a == b == "(n : Nat) : n + 0 = n"


def test_headless_strips_modifiers_and_term_proof() -> None:
    assert normalize_headless("protected theorem t : True := sorry") == ": True"
    assert normalize_headless("lemma l (n : Nat) : n = n := by sorry") == "(n : Nat) : n = n"


def test_headless_returns_none_without_declaration_head() -> None:
    assert normalize_headless("def d (n : Nat) : Nat := by sorry") is None
    assert normalize_headless("just some text") is None


# --- #check message parsing ---


def test_parse_check_default() -> None:
    msg = "@t2 : ∀ {n : Nat}, n = 0 → n = 0"
    assert parse_check_type(msg, "t2") == "∀ {n : Nat}, n = 0 → n = 0"


def test_parse_check_explicit_with_universes() -> None:
    msg = "@AddConstMapClass.semiconj.{u_1, u_2, u_3} : ∀ {F : Type u_1}, F → F"
    parsed = parse_check_type(msg, "AddConstMapClass.semiconj")
    assert parsed == "∀ {F : Type u_1}, F → F"


def test_parse_check_multiline_collapsed() -> None:
    msg = "@t : ∀ {n : Nat},\n  n = 0 →\n    n = 0"
    assert parse_check_type(msg, "t") == "∀ {n : Nat}, n = 0 → n = 0"


def test_parse_check_name_mismatch_returns_none() -> None:
    assert parse_check_type("@other : X", "t") is None


def test_check_command_batches_names() -> None:
    cmd = check_command("import Mathlib", "set_option pp.explicit true in", ["a", "b"])
    assert cmd.splitlines()[0] == "import Mathlib"
    assert "set_option pp.explicit true in #check @a" in cmd
    assert "#check @b" in cmd


# --- hashing ---


def test_content_hash_order_independent() -> None:
    a = representation_content_hash({"headless": "x", "signature_pp": "y"})
    b = representation_content_hash({"signature_pp": "y", "headless": "x"})
    assert a == b


def test_near_dup_hash_whitespace_robust() -> None:
    assert signature_near_dup_hash("a  +  b") == signature_near_dup_hash("a + b")


# --- registry append (§19.4) ---


def test_append_representation_signatures_is_additive() -> None:
    registry = FrozenRegistry(
        frozen_at=_UTC,
        benchmarks=(unresolved_benchmark("consistency_check", "resolve at Phase 11"),),
    )
    assert not registry.representation_signatures_appended
    sig = signature_near_dup_hash("∀ n : Nat, n = n")
    updated = append_representation_signatures(registry, "consistency_check", (sig,))
    assert updated.representation_signatures_appended
    benchmark = updated.benchmarks[0]
    assert benchmark.representation_hashes == (sig,)
    # Identity/text signatures untouched (additive, not a rewrite).
    assert benchmark.nl_hashes == registry.benchmarks[0].nl_hashes
    index = DenylistIndex(updated)
    assert index.contains_representation(sig)


def test_append_unknown_benchmark_raises() -> None:
    registry = FrozenRegistry(frozen_at=_UTC, benchmarks=(unresolved_benchmark("con_nf", "plan"),))
    with pytest.raises(KeyError, match="not in"):
        append_representation_signatures(registry, "nonexistent", ("h",))


def test_parse_check_type_starting_on_next_line() -> None:
    # The type may begin on the line after the colon when it wraps.
    msg = "@big.{u_1} :\n  ∀ {F : Type u_1}, F → F"
    assert parse_check_type(msg, "big") == "∀ {F : Type u_1}, F → F"


def test_parse_check_no_at_prefix() -> None:
    # Lean drops the @ when all binders are explicit.
    assert parse_check_type("lf_add_comm : ∀ (x y : Nat), x + y = y + x", "lf_add_comm") == (
        "∀ (x y : Nat), x + y = y + x"
    )


# --- review round: normalize_headless robustness (confirmed defects) ---


def test_headless_nested_block_comment_in_docstring() -> None:
    src = "/-- doc /- nested -/ end -/\ntheorem my_add (a b : Nat) : a + b = b + a := by sorry"
    assert normalize_headless(src) == "(a b : Nat) : a + b = b + a"


def test_headless_guillemet_name_with_space() -> None:
    a = normalize_headless("theorem «foo bar» (n : Nat) : n = n := by sorry")
    b = normalize_headless("theorem «qux baz» (n : Nat) : n = n := by sorry")
    assert a == b == "(n : Nat) : n = n"  # name fully removed -> renaming invariant


def test_headless_nested_bracket_attribute() -> None:
    src = "@[aesop safe (rule_sets := [Foo])] theorem t (n : Nat) : n = n := by sorry"
    assert normalize_headless(src) == "(n : Nat) : n = n"


def test_headless_prefers_parsed_signature_over_regex() -> None:
    from leanfaith.representations.pipeline import TheoremForRepresentation, _build_record
    from leanfaith.schemas.ids import make_id

    # A statement whose source would trip the string fallback (string literal
    # containing comment-like text); the parsed signature is used verbatim.
    theorem = TheoremForRepresentation(
        theorem_id=make_id("thm", {"n": "s"}),
        full_name="s",
        proof_stripped='theorem s : "a--b".length = 4 := by sorry',
        context_id="ctx:" + "0" * 64,
        source_signature='(s : String) : "a--b" = s',
    )
    record = _build_record(theorem, "elaborated", "elaborated_explicit", None, _UTC)
    assert record.headless == '(s : String) : "a--b" = s'  # parsed signature, not mangled
    assert record.view_status["headless"].value == "ok"
    assert record.view_status["semantic_atoms"].value == "failed"  # no expr tree supplied
