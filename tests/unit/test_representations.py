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
    alpha_canonical_bytes,
    normalize_headless,
    parse_check_type,
    representation_content_hash,
    signature_near_dup_hash,
)
from leanfaith.representations.views import (
    check_command,
    normalize_pp_universe_placeholders,
)

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


def test_alpha_canonical_bytes_normalizes_unicode_universe_names() -> None:
    left = {"k": "const", "n": "Fixture", "us": "[u₁, max u₂ u₁]"}
    right = {"k": "const", "n": "Fixture", "us": "[fresh₉, max other₇ fresh₉]"}

    assert alpha_canonical_bytes(left) == alpha_canonical_bytes(right)


def test_alpha_canonical_bytes_preserves_repeated_vs_distinct_universes() -> None:
    repeated = {"k": "const", "n": "Fixture", "us": "[u₁, u₁]"}
    distinct = {"k": "const", "n": "Fixture", "us": "[u₁, u₂]"}

    assert alpha_canonical_bytes(repeated) != alpha_canonical_bytes(distinct)


def test_normalize_pp_universe_placeholders_is_alpha_invariant() -> None:
    first = "∀ {a : Type u_2} {b : Type u_17}, ULift.{u_2, u_17} a → a"
    second = "∀ {a : Type u_41} {b : Type u_3}, ULift.{u_41, u_3} a → a"

    expected = "∀ {a : Type u_0} {b : Type u_1}, ULift.{u_0, u_1} a → a"
    assert normalize_pp_universe_placeholders(first) == expected
    assert normalize_pp_universe_placeholders(second) == expected


def test_normalize_pp_universe_placeholders_preserves_aliasing_and_user_names() -> None:
    repeated = normalize_pp_universe_placeholders("Type u_9 → Type u_9 → Type userU")
    distinct = normalize_pp_universe_placeholders("Type u_9 → Type u_8 → Type userU")

    assert repeated == "Type u_0 → Type u_0 → Type userU"
    assert distinct == "Type u_0 → Type u_1 → Type userU"
    assert repeated != distinct


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


def test_dump_command_escapes_names_with_quotes_and_backslashes() -> None:
    from leanfaith.representations.pipeline import _dump_command

    cmd = _dump_command("import Mathlib", "-- helper", ["good", "«a\\b»", '«a"b»'])
    # Guillemets stay literal (Lean-parseable), quotes/backslashes are escaped,
    # so no lfDump line becomes a syntax error that fails the whole batch.
    assert 'lfDump "good"' in cmd
    assert r'lfDump "«a\\b»"' in cmd
    assert r'lfDump "«a\"b»"' in cmd
    assert "\\u" not in cmd  # guillemets not ascii-escaped


def test_inline_import_hoisting_ignores_nested_comment_prose() -> None:
    from leanfaith.representations.pipeline import _hoist_inline_imports

    source = """import Mathlib
/- Asymptote source:
   import geometry;
   /- nested example:
      import graph;
   -/
-/
-- import NotARealModule
def caption := \"import markers; /- still a string -/\"
theorem fixture : True := by trivial"""

    imports, body = _hoist_inline_imports(source)

    assert imports == "import Mathlib"
    assert "import geometry;" in body
    assert "import graph;" in body
    assert "import markers;" in body
    assert "theorem fixture" in body


def test_inline_import_hoisting_accepts_comment_then_real_import() -> None:
    from leanfaith.representations.pipeline import _hoist_inline_imports

    imports, body = _hoist_inline_imports(
        "/- local note -/ import LeanFaithFixtures\ntheorem fixture : True := by trivial"
    )

    assert imports == "import LeanFaithFixtures"
    assert body == "theorem fixture : True := by trivial"


def test_imports_with_lean_is_first_and_deduplicated() -> None:
    from leanfaith.representations.pipeline import _imports_with_lean

    assert _imports_with_lean("import Mathlib\nimport Lean\nimport Aesop") == (
        "import Lean\nimport Mathlib\nimport Aesop"
    )


def test_private_environment_lookup_name_is_source_qualified() -> None:
    from leanfaith.representations.pipeline import declaration_environment_lookup_name

    assert (
        declaration_environment_lookup_name(
            "_private.0.ZMod.mul_inv_cancel_aux",
            "Mathlib/Algebra/Field/ZMod.lean",
        )
        == "_private.Mathlib.Algebra.Field.ZMod.0.ZMod.mul_inv_cancel_aux"
    )
    assert (
        declaration_environment_lookup_name("Nat.add_comm", "Mathlib/Data/Nat/Basic.lean")
        == "Nat.add_comm"
    )
    assert declaration_environment_lookup_name("_private.0.hidden", None) == ("_private.0.hidden")


def test_private_expr_dump_uses_environment_name_but_preserves_theorem_identity() -> None:
    from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
    from leanfaith.representations import (
        RepresentationBatch,
        TheoremForRepresentation,
        build_representation_batch,
    )
    from leanfaith.schemas.ids import make_id

    display_name = "_private.0.Fixture.hidden"
    lookup_name = "_private.LeanFaithFixtures.Basic.0.Fixture.hidden"

    class PrivateLookupBackend:
        def __init__(self) -> None:
            self.requests: list[LeanRequest] = []

        def run(self, request: LeanRequest) -> LeanResult:
            self.requests.append(request)
            assert request.request_id.endswith("-combined")
            assert "#check" not in (request.code or "")
            assert f'lfDump "{lookup_name}"' in (request.code or "")
            messages = (
                {
                    "severity": "info",
                    "data": (
                        "LFJSON "
                        f'{{"name":"{lookup_name}","tree":'
                        '{"k":"const","n":"True","us":"[]"}}'
                    ),
                },
            )
            return LeanResult(
                request_id=request.request_id,
                request_hash="a" * 64,
                context_id=request.context_id,
                context_fingerprint="a" * 64,
                status=LeanStatus.VALID,
                messages=messages,
            )

    theorem_id = make_id("thm", {"private_lookup": 1})
    context_id = "ctx:" + "a" * 64
    theorem = TheoremForRepresentation(
        theorem_id=theorem_id,
        full_name=display_name,
        proof_stripped="private theorem hidden : True := by sorry",
        context_id=context_id,
        environment_lookup_name=lookup_name,
    )
    backend = PrivateLookupBackend()
    result = build_representation_batch(
        backend,  # type: ignore[arg-type]
        RepresentationBatch(context_id, "import LeanFaithFixtures", (theorem,)),
        created_at=_UTC,
    )
    (record,) = result.ordered_representation_records
    assert len(backend.requests) == 1
    assert record.theorem_id == theorem_id
    assert record.alpha_identity_fingerprint is not None
    assert record.view_status["signature_pp"].value == "failed"
    assert record.view_status["signature_explicit"].value == "failed"
    assert record.view_status["semantic_atoms"].value == "ok"
    assert record.view_status["operator_tree"].value == "ok"


def test_representation_batch_empty_and_mixed_contexts_fail_before_backend() -> None:
    from leanfaith.representations import (
        RepresentationBatch,
        TheoremForRepresentation,
        build_representation_batch,
    )
    from leanfaith.schemas.ids import make_id

    class NoCallBackend:
        calls = 0

        def run(self, request: object) -> None:
            del request
            self.calls += 1
            raise AssertionError("backend must not be called")

    backend = NoCallBackend()
    context_a = "ctx:" + "a" * 64
    context_b = "ctx:" + "b" * 64
    empty = build_representation_batch(
        backend,  # type: ignore[arg-type]
        RepresentationBatch(context_a, "import Mathlib", ()),
        created_at=_UTC,
    )
    assert not empty.ordered_representation_records
    theorem = TheoremForRepresentation(
        theorem_id=make_id("thm", {"mixed": 1}),
        full_name="x",
        proof_stripped="theorem x : True := by sorry",
        context_id=context_b,
    )
    with pytest.raises(ValueError, match="mixed contexts"):
        build_representation_batch(
            backend,  # type: ignore[arg-type]
            RepresentationBatch(context_a, "import Mathlib", (theorem,)),
            created_at=_UTC,
        )
    assert backend.calls == 0


def test_representation_request_ids_are_deterministic_per_theorem_and_view() -> None:
    from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
    from leanfaith.representations import (
        RepresentationBatch,
        TheoremForRepresentation,
        build_representation_batch,
    )
    from leanfaith.schemas.ids import make_id

    class InvalidBackend:
        def __init__(self) -> None:
            self.request_ids: list[str] = []

        def run(self, request: LeanRequest) -> LeanResult:
            self.request_ids.append(request.request_id)
            return LeanResult(
                request_id=request.request_id,
                request_hash="a" * 64,
                context_id=request.context_id,
                context_fingerprint="a" * 64,
                status=LeanStatus.INVALID,
            )

    theorem_id = make_id("thm", {"request_ids": 1})
    context_id = "ctx:" + "a" * 64
    theorem = TheoremForRepresentation(
        theorem_id=theorem_id,
        full_name="missing_fixture",
        proof_stripped="theorem missing_fixture : True := by sorry",
        context_id=context_id,
    )
    backend = InvalidBackend()
    build_representation_batch(
        backend,  # type: ignore[arg-type]
        RepresentationBatch(context_id, "import Mathlib", (theorem,)),
        created_at=_UTC,
    )

    prefix = f"repr-{theorem_id.removeprefix('thm:')[:16]}-repr_v2"
    assert backend.request_ids == [
        f"{prefix}-combined",
        f"{prefix}-signature_pp",
        f"{prefix}-signature_explicit",
        f"{prefix}-expr",
    ]


def test_single_view_failure_is_retried_and_persisted(tmp_path: object) -> None:
    import json
    from pathlib import Path

    from leanfaith.cli.pipeline import _write_representation_partition
    from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
    from leanfaith.representations import (
        RepresentationBatch,
        TheoremForRepresentation,
        build_representation_batch,
    )
    from leanfaith.schemas.ids import make_id

    class ExplicitFailureBackend:
        def __init__(self) -> None:
            self.request_ids: list[str] = []

        def run(self, request: LeanRequest) -> LeanResult:
            self.request_ids.append(request.request_id)
            if request.request_id.endswith("-combined"):
                messages = (
                    {"severity": "info", "data": "@fixture : True"},
                    {
                        "severity": "info",
                        "data": 'LFJSON {"name":"fixture","tree":{"k":"const","n":"True","us":"[]"}}',
                    },
                )
                status = LeanStatus.VALID
            else:
                messages = ()
                status = LeanStatus.INVALID
            return LeanResult(
                request_id=request.request_id,
                request_hash="a" * 64,
                context_id=request.context_id,
                context_fingerprint="a" * 64,
                status=status,
                messages=messages,
            )

    out_dir = Path(str(tmp_path))
    theorem_id = make_id("thm", {"isolated_view": 1})
    context_id = "ctx:" + "a" * 64
    theorem = TheoremForRepresentation(
        theorem_id=theorem_id,
        full_name="fixture",
        proof_stripped="theorem fixture : True := by sorry",
        context_id=context_id,
    )
    backend = ExplicitFailureBackend()
    result = build_representation_batch(
        backend,  # type: ignore[arg-type]
        RepresentationBatch(context_id, "import Mathlib", (theorem,)),
        created_at=_UTC,
    )

    (record,) = result.ordered_representation_records
    assert record.view_status["signature_pp"].value == "ok"
    assert record.view_status["signature_explicit"].value == "failed"
    assert record.view_status["semantic_atoms"].value == "ok"
    assert [(failure.theorem_id, failure.view) for failure in result.per_theorem_failures] == [
        (theorem_id, "signature_explicit")
    ]
    assert backend.request_ids[-1].endswith("-signature_explicit")

    _write_representation_partition(result, out_dir=out_dir, source="fixture")
    failures = [
        json.loads(line)
        for line in (out_dir / "failures" / "fixture.jsonl").read_text().splitlines()
    ]
    assert [(row["theorem_id"], row["view"]) for row in failures] == [
        (theorem_id, "signature_explicit")
    ]


def test_malformed_theorem_retains_raw_view_and_explicit_failures() -> None:
    from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
    from leanfaith.representations import (
        RepresentationBatch,
        TheoremForRepresentation,
        build_representation_batch,
    )
    from leanfaith.schemas.ids import make_id

    class InvalidBackend:
        def run(self, request: LeanRequest) -> LeanResult:
            return LeanResult(
                request_id=request.request_id,
                request_hash="a" * 64,
                context_id=request.context_id,
                context_fingerprint="a" * 64,
                status=LeanStatus.INVALID,
            )

    context_id = "ctx:" + "a" * 64
    theorem = TheoremForRepresentation(
        theorem_id=make_id("thm", {"malformed": 1}),
        full_name="malformed_fixture",
        proof_stripped="this is not a Lean declaration",
        context_id=context_id,
        inline_declaration=True,
    )
    result = build_representation_batch(
        InvalidBackend(),  # type: ignore[arg-type]
        RepresentationBatch(context_id, "import Mathlib", (theorem,)),
        created_at=_UTC,
    )

    (record,) = result.ordered_representation_records
    assert record.raw_proof_stripped == theorem.proof_stripped
    assert record.view_status["raw_proof_stripped"].value == "ok"
    assert record.view_status["headless"].value == "failed"
    assert {failure.view for failure in result.per_theorem_failures} == {
        "headless",
        "signature_pp",
        "signature_explicit",
        "semantic_atoms",
        "operator_tree",
    }
