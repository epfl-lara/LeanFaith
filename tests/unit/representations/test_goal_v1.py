"""Pure, bounded checks for the versioned goal_v1.0 contract."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
import yaml

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.goal_v1 import (
    GOAL_MARKER,
    RENDERER_VERSION,
    SPEC_HASH,
    SPEC_PAYLOAD,
    SURFACE_PROVENANCE_TAG,
    CompileContext,
    ElaboratedInput,
    GoalV1Error,
    SurfaceRenderError,
    render_elaborated_batch,
    render_surface,
    signature_to_goal_v1,
    validate_goal_v1,
)

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_CONFIG = _REPO_ROOT / "configs" / "representations" / "goal_v1_v1.yaml"


def _context(**overrides: object) -> CompileContext:
    values: dict[str, object] = {
        "project_id": "fixtures",
        "project_revision": "workspace",
        "lean_version": "v4.31.0-rc1",
        "import_header": "import LeanFaithFixtures",
        "command_preamble": "set_option autoImplicit false",
        "namespace_context": (),
        "open_context": ("Nat",),
        "scoped_context": (),
        "options": {"autoImplicit": False, "maxHeartbeats": 0},
    }
    values.update(overrides)
    return CompileContext(**values)  # type: ignore[arg-type]


def _load_config() -> dict[str, object]:
    loaded = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def test_spec_hash_and_yaml_frozen_contract_match() -> None:
    config = _load_config()

    assert hash_canonical(SPEC_PAYLOAD) == SPEC_HASH
    assert config["status"] == "frozen"
    assert config["spec"] == SPEC_PAYLOAD
    assert config["spec_hash"] == SPEC_HASH
    assert RENDERER_VERSION == "goal_v1.0"


def test_config_pins_the_exact_renderer_and_python_sources() -> None:
    config = _load_config()
    sources = cast(dict[str, dict[str, str]], config["implementation_sources"])

    for source in sources.values():
        source_path = _REPO_ROOT / source["path"]
        assert sha256_hex(source_path.read_bytes()) == source["sha256"]


def test_one_example_surface_smoke_is_joinable_without_an_inverse() -> None:
    config = _load_config()
    smoke = cast(dict[str, str], config["one_example_smoke"])
    grammar = cast(dict[str, object], SPEC_PAYLOAD["grammar"])
    context = _context()

    sidecar = render_surface(
        raw_statement=smoke["raw_statement"],
        declaration_kind="theorem",
        compile_context=context,
        parsed_signature=smoke["parsed_signature"],
    )

    assert sidecar.core_text() == smoke["expected_goal_v1"]
    assert sidecar.record.goal_v1_source == "surface"
    assert sidecar.record.renderer_version == RENDERER_VERSION
    assert sidecar.record.spec_hash == SPEC_HASH
    assert sidecar.record.warnings == (SURFACE_PROVENANCE_TAG,)
    assert grammar["surface_provenance_tag"] == SURFACE_PROVENANCE_TAG
    assert sidecar.record.raw_statement_hash == sha256_hex(smoke["raw_statement"].encode("utf-8"))
    assert sidecar.record.compile_context_id == context.compile_context_id
    assert sidecar.raw_statement == smoke["raw_statement"]
    assert "goalV1Smoke" not in sidecar.core_text()
    assert "Nat.le_of_lt" not in sidecar.core_text()
    assert ":=" not in sidecar.core_text()
    assert sidecar.core_text().count("⊢") == 1
    assert json.dumps(sidecar.to_dict(), sort_keys=True, ensure_ascii=False)


def test_compile_context_hash_is_deterministic_but_context_sensitive() -> None:
    first = _context(options={"maxHeartbeats": 0, "autoImplicit": False})
    reordered = _context(options={"autoImplicit": False, "maxHeartbeats": 0})
    changed = _context(import_header="import Lean")

    assert first.compile_context_id == reordered.compile_context_id
    assert first.compile_context_id != changed.compile_context_id
    assert first.compile_context_id == f"ctx:{first.fingerprint}"


def test_compile_context_rejects_non_import_commands_in_import_header() -> None:
    with pytest.raises(ValueError, match="import commands only"):
        _context(import_header="import Lean\nopen Nat")


def test_dependent_universe_and_named_instance_surface_view() -> None:
    raw = """universe u
theorem complex {α : Type u} [inst : Inhabited α] (x : α)
    (h : ∀ y : α, y = y) : ((fun z => z) x = x) ∧ x = x := by
  exact ⟨rfl, h x⟩
"""

    sidecar = render_surface(
        raw_statement=raw,
        declaration_kind="theorem",
        compile_context=_context(command_preamble="universe u"),
        parsed_signature=(
            "{α : Type u} [inst : Inhabited α] (x : α) "
            "(h : ∀ y : α, y = y) : ((fun z => z) x = x) ∧ x = x"
        ),
    )

    assert sidecar.core_text() == (
        "α : Type u\ninst : Inhabited α\nx : α\nh : ∀ y : α, y = y\n⊢ ((fun z => z) x = x) ∧ x = x"
    )


def test_top_level_named_forall_moves_into_local_context() -> None:
    assert signature_to_goal_v1(": ∀ (x y : Nat), x = x") == "x y : Nat\n⊢ x = x"


def test_constructor_commas_do_not_split_forall_binders() -> None:
    assert signature_to_goal_v1(": ∀ h : ⟨1, 2⟩ = (1, 2), True") == ("h : ⟨1, 2⟩ = (1, 2)\n⊢ True")
    assert signature_to_goal_v1(": let p := ⟨1, 2⟩; p = ⟨1, 2⟩") == (
        "⊢ let p := ⟨1, 2⟩; p = ⟨1, 2⟩"
    )


@pytest.mark.parametrize(
    "signature",
    [
        ": ∀ h : ∃ x : Nat, x = x, True",
        ": ∀ h : Σ x : Nat, Fin x, True",
        ": ∀ h : ∀ x : Nat, x = x, True",
        ": ∀ h : ¬ ∃ x : Nat, x = x, True",
        ": ∀ h : True ∧ ∃ x : Nat, x = x, True",
        ": ∀ h : Nat × Σ x : Nat, Fin x, True",
        ": ∀ h : P ∨ ∀ x : Nat, Q x, True",
        ": ∀ h : Foo x, y, True",
        ": ∀ h : ⟪1, 2⟫ = x, True",
    ],
)
def test_unparenthesized_comma_binding_forall_types_fail_closed(signature: str) -> None:
    with pytest.raises(SurfaceRenderError, match="parenthesize the binder"):
        signature_to_goal_v1(signature)


def test_parenthesized_comma_binding_forall_types_preserve_meaning() -> None:
    assert signature_to_goal_v1(": ∀ (h : ∃ x : Nat, x = x), True") == (
        "h : ∃ x : Nat, x = x\n⊢ True"
    )
    assert signature_to_goal_v1(": ∀ (h : Σ x : Nat, Fin x), True") == (
        "h : Σ x : Nat, Fin x\n⊢ True"
    )
    assert signature_to_goal_v1(": ∀ (h : Nat), ∃ x : Nat, x = x") == (
        "h : Nat\n⊢ ∃ x : Nat, x = x"
    )


def test_guillemet_binder_names_preserve_internal_whitespace_byte_for_byte() -> None:
    assert signature_to_goal_v1("(«x  y» : Nat) : True") == "«x  y» : Nat\n⊢ True"


@pytest.mark.parametrize(
    "name",
    ['"x"', "'x'", "if", "rec", "x.y", "+"],
)
def test_surface_binder_names_retain_their_original_token_kind(name: str) -> None:
    with pytest.raises(SurfaceRenderError, match="unsupported binder name syntax"):
        signature_to_goal_v1(f"({name} : Nat) : True")


def test_explicit_and_elaborated_generated_local_names_are_distinguished() -> None:
    assert signature_to_goal_v1("(x' : Nat) : True") == "x' : Nat\n⊢ True"
    validate_goal_v1("a✝ a✝¹ : Nat\n⊢ True")
    with pytest.raises(GoalV1Error, match="unsupported binder name syntax"):
        validate_goal_v1('"x" : Nat\n⊢ True')


def test_generated_term_binding_names_are_elaborated_only() -> None:
    validate_goal_v1("⊢ let a✝ := 1; a✝ = 1")
    validate_goal_v1("h : let a✝¹ := 1; a✝¹ = 1\n⊢ True")
    with pytest.raises(SurfaceRenderError, match="unsupported let binding head"):
        signature_to_goal_v1(": let a✝ := 1; a✝ = 1")


def test_escaped_and_unescaped_duplicate_local_names_fail_closed() -> None:
    with pytest.raises(SurfaceRenderError, match="duplicate_or_shadowed_local_name"):
        signature_to_goal_v1("(x : Nat) («x» : Nat) : True")
    with pytest.raises(GoalV1Error, match="local names must be unique"):
        validate_goal_v1("x «x» : Nat\n⊢ True")


@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        (
            "(h : let x := 1; let y := x; y = 1) : True",
            "h : let x := 1; let y := x; y = 1\n⊢ True",
        ),
        (
            "(h : have x := 1; x = 1) : True",
            "h : let x := 1; x = 1\n⊢ True",
        ),
        (
            ": ∀ h : (let x := 1; x = 1), True",
            "h : (let x := 1; x = 1)\n⊢ True",
        ),
        (
            '(h : ":=" = ":=") : True',
            'h : ":=" = ":="\n⊢ True',
        ),
        (
            '(h : "⊢" = "⊢") : True',
            'h : "⊢" = "⊢"\n⊢ True',
        ),
    ],
)
def test_local_types_share_binding_and_literal_validation(signature: str, expected: str) -> None:
    assert signature_to_goal_v1(signature) == expected


def test_literal_turnstiles_do_not_count_as_structural_markers() -> None:
    assert signature_to_goal_v1(': let s := "⊢"; s = "⊢"') == ('⊢ let s := "⊢"; s = "⊢"')


def test_incomplete_binding_in_local_type_fails_closed() -> None:
    with pytest.raises(SurfaceRenderError, match="binder type is outside"):
        signature_to_goal_v1("(h : let x := 1; let y) : True")
    with pytest.raises(GoalV1Error, match="incomplete let binding"):
        validate_goal_v1("h : let x := 1; let y\n⊢ True")


def test_comment_tokens_inside_strings_do_not_change_the_signature() -> None:
    raw = """@[simp] theorem stringComment (s : String)
      (h : s = "-- not a comment /- either -/") : s = s := by
      exact Eq.refl s
    """

    sidecar = render_surface(
        raw_statement=raw,
        declaration_kind="theorem",
        compile_context=_context(),
        parsed_signature='(s : String) (h : s = "-- not a comment /- either -/") : s = s',
    )

    assert sidecar.core_text() == ('s : String\nh : s = "-- not a comment /- either -/"\n⊢ s = s')
    assert "Eq.refl" not in sidecar.core_text()


def test_declaration_keywords_inside_strings_and_guillemet_names_are_not_candidates() -> None:
    raw = """theorem «theorem» (s : String) (h : s = "lemma def theorem") : s = s := rfl"""

    sidecar = render_surface(
        raw_statement=raw,
        declaration_kind="theorem",
        compile_context=_context(),
        parsed_signature='(s : String) (h : s = "lemma def theorem") : s = s',
    )

    assert sidecar.core_text() == 's : String\nh : s = "lemma def theorem"\n⊢ s = s'


def test_equation_style_proof_without_colon_equals_fails_closed() -> None:
    raw = """theorem equationStyle : Nat → Nat
      | 0 => 0
      | n => n
    """

    with pytest.raises(SurfaceRenderError) as raised:
        render_surface(
            raw_statement=raw,
            declaration_kind="theorem",
            compile_context=_context(),
        )

    assert raised.value.code.value == "ambiguous_proof_boundary"


def test_incomplete_raw_declaration_without_proof_boundary_fails_closed() -> None:
    with pytest.raises(SurfaceRenderError) as raised:
        render_surface(
            raw_statement="theorem incomplete : True",
            declaration_kind="theorem",
            compile_context=_context(),
        )

    assert raised.value.code.value == "ambiguous_proof_boundary"


def test_semicolon_delimited_let_proposition_is_preserved_by_surface_path() -> None:
    raw = "theorem letClaim : let x := 1; x = 1 := by rfl"

    sidecar = render_surface(
        raw_statement=raw,
        declaration_kind="theorem",
        compile_context=_context(),
        parsed_signature=": let x := 1; x = 1",
    )

    assert sidecar.core_text() == "⊢ let x := 1; x = 1"


@pytest.mark.parametrize(
    ("raw", "signature", "expected"),
    [
        (
            "theorem chainedLet : let x := 1; let y := x; y = 1 := by rfl",
            ": let x := 1; let y := x; y = 1",
            "⊢ let x := 1; let y := x; y = 1",
        ),
        (
            "theorem chainedHave : have x := 1; have y := x; y = 1 := by rfl",
            ": have x := 1; have y := x; y = 1",
            "⊢ let x := 1; let y := x; y = 1",
        ),
        (
            "theorem nestedExists : ∃ n : Nat, let x := n; let y := x; y = n := by exact ⟨1, rfl⟩",
            ": ∃ n : Nat, let x := n; let y := x; y = n",
            "⊢ ∃ n : Nat, let x := n; let y := x; y = n",
        ),
        (
            "theorem nestedForall : ∀ n : Nat, let x := n; let y := x; y = n := by intro n; rfl",
            ": ∀ n : Nat, let x := n; let y := x; y = n",
            "n : Nat\n⊢ let x := n; let y := x; y = n",
        ),
        (
            "theorem nestedConjunction : True ∧ (let x := 1; let y := x; y = 1) := by simp",
            ": True ∧ (let x := 1; let y := x; y = 1)",
            "⊢ True ∧ (let x := 1; let y := x; y = 1)",
        ),
    ],
)
def test_complete_binding_chains_are_structurally_extracted(
    raw: str,
    signature: str,
    expected: str,
) -> None:
    sidecar = render_surface(
        raw_statement=raw,
        declaration_kind="theorem",
        compile_context=_context(),
        parsed_signature=signature,
    )

    assert sidecar.core_text() == expected


def test_nested_named_argument_and_parenthesized_binding_values_are_unambiguous() -> None:
    assert signature_to_goal_v1(": (id (α := Nat) 1) = 1") == "⊢ (id (α := Nat) 1) = 1"
    assert signature_to_goal_v1(": Foo (α := id (β := Nat))") == ("⊢ Foo (α := id (β := Nat))")
    assert signature_to_goal_v1(": let x := (let y := 1; y); x = 1") == (
        "⊢ let x := (let y := 1; y); x = 1"
    )


@pytest.mark.parametrize(
    "target",
    [
        "Foo (let_fun x := y)",
        "Foo (haveI x := y)",
        "Foo (letI x := y)",
        "Foo (bind! x := y)",
        "True ∧ (bind! x := y)",
        'Foo ("x" := y)',
        "Foo ('x' := y)",
        "Foo (_ := y)",
        "Foo (if := y)",
        "Foo (foo := 1, bar)",
    ],
)
def test_only_simple_parenthesized_named_arguments_may_leave_nested_assignments(
    target: str,
) -> None:
    with pytest.raises(SurfaceRenderError):
        signature_to_goal_v1(f": {target}")
    with pytest.raises(GoalV1Error):
        validate_goal_v1(f"⊢ {target}")


@pytest.mark.parametrize(
    ("raw", "signature", "expected"),
    [
        (
            'theorem emptyString : let s := ""; s = "" := by rfl',
            ': let s := ""; s = ""',
            '⊢ let s := ""; s = ""',
        ),
        (
            'theorem quotedTokens : let s := "have; := )"; s = "have; := )" := by rfl',
            ': let s := "have; := )"; s = "have; := )"',
            '⊢ let s := "have; := )"; s = "have; := )"',
        ),
        (
            "theorem charSeparator : let c := ';'; c = ';' := by rfl",
            ": let c := ';'; c = ';'",
            "⊢ let c := ';'; c = ';'",
        ),
        (
            "theorem charDelimiter : (let c := ')'; c = ')') := by rfl",
            ": (let c := ')'; c = ')')",
            "⊢ (let c := ')'; c = ')')",
        ),
        (
            "theorem rawTabChar : let c := '\t'; c = '\t' := by rfl",
            ": let c := '\t'; c = '\t'",
            "⊢ let c := '\t'; c = '\t'",
        ),
        (
            'theorem rawString : let s := r#"a"  "let y := 1; ) -- /-"#; '
            's = r#"a"  "let y := 1; ) -- /-"# := by rfl',
            ': let s := r#"a"  "let y := 1; ) -- /-"#; s = r#"a"  "let y := 1; ) -- /-"#',
            '⊢ let s := r#"a"  "let y := 1; ) -- /-"#; s = r#"a"  "let y := 1; ) -- /-"#',
        ),
        (
            'theorem zeroHashRawString : let s := r"\\"; s = r"\\" := by rfl',
            ': let s := r"\\"; s = r"\\"',
            '⊢ let s := r"\\"; s = r"\\"',
        ),
    ],
)
def test_binding_literals_are_opaque_but_remain_nonempty(
    raw: str,
    signature: str,
    expected: str,
) -> None:
    sidecar = render_surface(
        raw_statement=raw,
        declaration_kind="theorem",
        compile_context=_context(),
        parsed_signature=signature,
    )

    assert sidecar.core_text() == expected


@pytest.mark.parametrize(
    "target",
    [
        "let x := 1; let y",
        "let x := 1; let y := x",
        "let x := 1; let y := x;",
        "let x := ; x = 1",
        "let x := 1 x = 1",
        "let x := let y := 1; y; x = 1",
        "True ∧ (let x := 1; let y)",
        "let c := ';'",
        "let x := 1; let c := ';'",
        'let s := r#";"#',
        "let x := a <;>",
        "let x := a ;;",
        "let x := a ;>",
        "let x ::= 1; True",
        "True ::= False",
        "let x := 1; ,",
        "let x := ,; True",
        "let x : := 1; True",
        "let x, := 1; True",
        "let x := 1; x =",
        "let x := 1 +; True",
        "let x := 1; True;",
        'let "x" := 1; True',
        "let 'x' := 1; True",
        'let r#"x"# := 1; True',
        "let if := 1; True",
        "let then := 1; True",
        "let x := if; True",
        "let x := fun; True",
        "let x := 1; if",
        "let x := 1; by",
        "let x := if True then; True",
        "let x := 1; if True then",
        "let x : Nat : Foo := 1; True",
        "let «x» junk «y» := 1; True",
        "let x := if then True else True; True",
        "let x := if True then else True; True",
        "let x := show from True; True",
        "let x := forall , True; True",
        "let x := 1; True ∧ if",
        "let x := 1; (if)",
    ],
)
def test_incomplete_or_ambiguous_binding_targets_fail_on_all_surface_boundaries(
    target: str,
) -> None:
    raw = f"theorem incomplete : {target} := by rfl"

    with pytest.raises(SurfaceRenderError, match="ambiguous_proof_boundary"):
        render_surface(
            raw_statement=raw,
            declaration_kind="theorem",
            compile_context=_context(),
            parsed_signature=f": {target}",
        )
    with pytest.raises(SurfaceRenderError, match="ambiguous_proof_boundary"):
        signature_to_goal_v1(f": {target}")
    with pytest.raises(GoalV1Error):
        validate_goal_v1(f"⊢ {target}")


@pytest.mark.parametrize(
    "target",
    [
        "let_fun x := 1; x = 1",
        "let_delayed x := 1; x = 1",
        "let_tmp x := 1; x = 1",
        "haveI x := 1; x = 1",
        "letI x := 1; x = 1",
        "bind! x := 1; x = 1",
    ],
)
def test_assignment_bearing_target_syntax_never_supplies_the_raw_proof_boundary(
    target: str,
) -> None:
    with pytest.raises(SurfaceRenderError, match="ambiguous_proof_boundary"):
        render_surface(
            raw_statement=f"theorem assignmentSyntax : {target} := by rfl",
            declaration_kind="theorem",
            compile_context=_context(),
            parsed_signature=f": {target}",
        )
    with pytest.raises(SurfaceRenderError, match="ambiguous_proof_boundary"):
        signature_to_goal_v1(f": {target}")
    with pytest.raises(GoalV1Error, match="unclaimed ':='"):
        validate_goal_v1(f"⊢ {target}")


@pytest.mark.parametrize(
    "raw",
    [
        "theorem validButUntagged : True := by trivial",
        "theorem missingProofLetFun : let_fun x := 1; x = 1",
        "theorem missingProofHaveI : haveI x := 1; True",
        "theorem missingProofBind : bind! x := 1; True",
        "theorem missingProofShortLetFun : let_fun x := 1",
        "theorem missingProofLetExpr : let_expr x := True | True",
        "theorem missingProofLetLambda : let_λ x := 1\n x = 1",
        "theorem missingProofAssign : assign x := True",
        "theorem missingProofMultiBind : bind! x y := z",
        "theorem missingProofPatternBind : bind! (x y) := z",
    ],
)
def test_raw_declaration_extraction_never_guesses_a_signature_or_proof_boundary(raw: str) -> None:
    with pytest.raises(SurfaceRenderError, match="trusted complete parsed_signature"):
        render_surface(
            raw_statement=raw,
            declaration_kind="theorem",
            compile_context=_context(),
        )


def test_surface_sidecar_requires_nonempty_raw_source() -> None:
    with pytest.raises(ValueError, match="raw_statement must be nonempty"):
        render_surface(
            raw_statement="  \n",
            declaration_kind="theorem",
            compile_context=_context(),
            parsed_signature=": True",
        )


def test_proof_with_a_lexical_top_level_assignment_fails_closed() -> None:
    raw = "theorem proofLet : True := by let x := 1; exact True.intro"

    with pytest.raises(SurfaceRenderError, match="trusted complete parsed_signature"):
        render_surface(
            raw_statement=raw,
            declaration_kind="theorem",
            compile_context=_context(),
        )


@pytest.mark.parametrize(
    "raw",
    [
        "theorem emptyProof : True :=",
        "theorem commentOnlyProof : True := /- no proof -/",
        "theorem lineCommentOnlyProof : True := -- no proof\n",
    ],
)
def test_empty_or_comment_only_raw_proof_fails_closed(raw: str) -> None:
    with pytest.raises(SurfaceRenderError, match="trusted complete parsed_signature"):
        render_surface(
            raw_statement=raw,
            declaration_kind="theorem",
            compile_context=_context(),
        )


@pytest.mark.parametrize(
    "raw",
    [
        "theorem bareByProof : True := by",
        "theorem bareFunProof : True := fun",
        "theorem danglingProofOperator : True := x =",
    ],
)
def test_obviously_incomplete_raw_proof_fails_closed(raw: str) -> None:
    with pytest.raises(SurfaceRenderError, match="trusted complete parsed_signature"):
        render_surface(
            raw_statement=raw,
            declaration_kind="theorem",
            compile_context=_context(),
        )


def test_dotted_have_token_is_not_rewritten_as_a_binding_keyword() -> None:
    assert signature_to_goal_v1(": foo.have x") == "⊢ foo.have x"
    with pytest.raises(SurfaceRenderError, match="unclaimed ':='"):
        signature_to_goal_v1(": Foo. have x := 1; True")


def test_atomic_symbolic_notation_is_not_mistaken_for_a_dangling_operator() -> None:
    assert signature_to_goal_v1(": ⊤") == "⊢ ⊤"
    assert signature_to_goal_v1(": True ∧ ⊤") == "⊢ True ∧ ⊤"
    assert signature_to_goal_v1(": x = ∅") == "⊢ x = ∅"
    assert signature_to_goal_v1(": let p := ⊤; p") == "⊢ let p := ⊤; p"


@pytest.mark.parametrize("token", ["∧", "∨", "≤", "↔", "¬", "∃"])
def test_nonatomic_standalone_operator_tokens_fail_closed(token: str) -> None:
    with pytest.raises(SurfaceRenderError, match="incomplete operator"):
        signature_to_goal_v1(f": {token}")
    with pytest.raises(GoalV1Error, match="incomplete operator"):
        validate_goal_v1(f"⊢ {token}")


def test_complete_if_and_fun_binding_fragments_remain_supported() -> None:
    assert signature_to_goal_v1(": let p := if True then True else False; p") == (
        "⊢ let p := if True then True else False; p"
    )
    assert signature_to_goal_v1(": let f := fun x => x; True") == ("⊢ let f := fun x => x; True")
    assert signature_to_goal_v1(": True ∧ if p then True else False") == (
        "⊢ True ∧ if p then True else False"
    )


@pytest.mark.parametrize(
    "target",
    [
        "∃ x : Nat",
        "Σ x : Nat",
        "¬ ∃ x : Nat",
        "True ∧ if p then True",
        "True ∧ fun x",
        "True ∧ show True",
        "True ∧ by trivial",
        "(True ∧)",
        "f (x,)",
        "f (,x)",
        "f (x +)",
        "(if p ∧ then q else r)",
        "(if p then q ∧ else r)",
        "(if p then q else r ∧)",
        "(fun x + => x)",
        "(fun x => x +)",
        "(show True ∧ from True)",
        "(show True from True ∧)",
        "(∃ x : Nat ∧, True)",
        "(∃ x : Nat, True ∧)",
        "let x := (1 +); x = 1",
        "let x := f (1,); x = 1",
    ],
)
def test_nested_incomplete_structured_terms_fail_closed(target: str) -> None:
    with pytest.raises(SurfaceRenderError):
        signature_to_goal_v1(f": {target}")
    with pytest.raises(GoalV1Error):
        validate_goal_v1(f"⊢ {target}")


@pytest.mark.parametrize(
    "signature",
    [
        ": `(term| have h := p; h) = `(term| have h := p; h)",
        "(h : `(term| foo) = `(term| foo)) : True",
        "(n : Name) : n = `have",
    ],
)
def test_name_and_syntax_quotations_fail_closed_without_keyword_rewriting(signature: str) -> None:
    with pytest.raises(SurfaceRenderError) as raised:
        signature_to_goal_v1(signature)

    assert raised.value.code.value == "syntax_quotation"


def test_final_validation_rejects_syntax_quotation_targets() -> None:
    with pytest.raises(GoalV1Error, match="syntax quotations are unsupported"):
        validate_goal_v1("⊢ `(term| have h := p; h) = `(term| have h := p; h)")


def test_layout_only_let_proposition_fails_closed_in_surface_path() -> None:
    raw = """theorem letClaim :
      let x := 1
      x = 1 := by rfl
    """

    with pytest.raises(SurfaceRenderError) as raised:
        render_surface(
            raw_statement=raw,
            declaration_kind="theorem",
            compile_context=_context(),
            parsed_signature=": let x := 1 x = 1",
        )

    assert raised.value.code.value == "ambiguous_proof_boundary"


@pytest.mark.parametrize(
    ("raw", "signature", "kind", "expected_code"),
    [
        (
            "def rejected (x : Nat) : Nat := x",
            "(x : Nat) : Nat",
            "def",
            "unsupported_declaration_kind",
        ),
        (
            "theorem anonInst {α : Type} [Inhabited α] (x : α) : True := by trivial",
            "{α : Type} [Inhabited α] (x : α) : True",
            "theorem",
            "anonymous_instance_binder",
        ),
        (
            "theorem arrow : True → True := fun h => h",
            ": True → True",
            "theorem",
            "anonymous_top_level_arrow",
        ),
        (
            "theorem shadow (x : Nat) (x : Nat) : True := by trivial",
            "(x : Nat) (x : Nat) : True",
            "theorem",
            "duplicate_or_shadowed_local_name",
        ),
    ],
)
def test_surface_ambiguities_fail_closed(
    raw: str,
    signature: str,
    kind: str,
    expected_code: str,
) -> None:
    with pytest.raises(SurfaceRenderError) as raised:
        render_surface(
            raw_statement=raw,
            declaration_kind=kind,
            compile_context=_context(),
            parsed_signature=signature,
        )

    assert raised.value.code.value == expected_code


def test_bounded_multi_source_surface_pilot_matches_declared_outcomes() -> None:
    config = _load_config()
    pilot = cast(dict[str, object], config["pilot"])
    cases = cast(list[dict[str, str]], pilot["cases"])
    consistency_case = next(
        case for case in cases if case["case_id"] == "consistency_check_amc12a_2019_p21"
    )
    assert consistency_case["fixture_kind"] == "derived_from_upstream_formal_statement"
    assert consistency_case["upstream_field"] == "formal_statement"
    assert consistency_case["upstream_formal_statement_sha256"] == (
        "bf963db06cfe1d75498daba3defa86a95bf720d225b21f57f496b82c027c1ddc"
    )
    assert consistency_case["upstream_goal_sha256"] == (
        "1b04a53200851b24a7d89474957231af6ad25e51d6a895f84d54ab1bcfee292b"
    )
    successes = 0
    expected_failures = 0

    for case in cases:
        if "expected_failure" in case:
            with pytest.raises(SurfaceRenderError) as raised:
                render_surface(
                    raw_statement=case["raw_statement"],
                    declaration_kind="theorem",
                    compile_context=_context(
                        project_id=case["source_family"],
                        project_revision=case["source_revision"],
                    ),
                    parsed_signature=case["parsed_signature"],
                )
            assert raised.value.code.value == case["expected_failure"]
            expected_failures += 1
            continue
        sidecar = render_surface(
            raw_statement=case["raw_statement"],
            declaration_kind="theorem",
            compile_context=_context(
                project_id=case["source_family"],
                project_revision=case["source_revision"],
            ),
            parsed_signature=case["parsed_signature"],
        )
        assert sidecar.core_text() == case["expected_goal_v1"]
        assert sidecar.record.goal_v1_source == "surface"
        assert sidecar.raw_statement == case["raw_statement"]
        successes += 1

    assert len(cases) == 6
    assert successes == 4
    assert expected_failures == 2


class _OneRequestBackend:
    def __init__(self, context: CompileContext) -> None:
        self.context = context
        self.requests: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        assert request.code is not None
        assert "theorem first" in request.code
        assert "lemma second" in request.code
        messages: tuple[dict[str, object], ...] = (
            {
                "severity": "info",
                "data": GOAL_MARKER
                + json.dumps(
                    {
                        "name": "first",
                        "constant_kind": "theorem",
                        "goal_v1": "x : Nat\n⊢ x = x",
                    }
                ),
            },
            {
                "severity": "info",
                "data": GOAL_MARKER
                + json.dumps(
                    {
                        "name": "second",
                        "constant_kind": "theorem",
                        "goal_v1": "⊢ True",
                    }
                ),
            },
        )
        return LeanResult(
            request_id=request.request_id,
            request_hash="a" * 64,
            context_id=request.context_id,
            context_fingerprint=self.context.fingerprint,
            status=LeanStatus.VALID,
            messages=messages,
            elapsed_ms=7,
            raw_response_path="raw.json",
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        raise AssertionError(f"goal_v1 uses one homogeneous request, got {len(requests)}")

    def close(self) -> None:
        return None


class _StaticBackend(_OneRequestBackend):
    def __init__(
        self,
        context: CompileContext,
        *,
        status: LeanStatus,
        messages: tuple[dict[str, object], ...],
        sorries: tuple[dict[str, object], ...] = (),
    ) -> None:
        super().__init__(context)
        self.status = status
        self.messages = messages
        self.sorries = sorries

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        return LeanResult(
            request_id=request.request_id,
            request_hash="b" * 64,
            context_id=request.context_id,
            context_fingerprint=self.context.fingerprint,
            status=self.status,
            messages=self.messages,
            sorries=self.sorries,
            elapsed_ms=5,
        )


def test_elaborated_batch_uses_one_protocol_request_and_preserves_raw_source() -> None:
    context = _context()
    backend = _OneRequestBackend(context)
    declarations = (
        ElaboratedInput("first", "theorem", "theorem first (x : Nat) : x = x := rfl"),
        ElaboratedInput("second", "lemma", "lemma second : True := True.intro"),
    )

    result = render_elaborated_batch(
        backend,
        declarations=declarations,
        compile_context=context,
        request_id="goal-v1-batch",
    )

    assert len(backend.requests) == 1
    assert not result.failures
    assert [sidecar.record.goal_v1_source for sidecar in result.sidecars] == [
        "elaborated",
        "elaborated",
    ]
    assert [sidecar.raw_statement for sidecar in result.sidecars] == [
        item.raw_statement for item in declarations
    ]
    assert result.request_hash == "a" * 64
    assert result.elapsed_ms == 7
    assert result.raw_response_path == "raw.json"


def test_elaborated_command_applies_context_and_skips_loaded_constant_source() -> None:
    context = _context(
        command_preamble='namespace GoalV1Scope\nscoped notation "GOALV1NAT" => Nat\nend GoalV1Scope',
        namespace_context=("Outer", "Inner"),
        open_context=("Nat",),
        scoped_context=("GoalV1Scope",),
        options={"Elab.async": False, "maxHeartbeats": 0},
    )
    inline_raw = "theorem inline : True := True.intro"
    loaded_raw = "theorem lf_add_comm (x y : Nat) : x + y = y + x := Nat.add_comm x y"
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "Outer.Inner.inline",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ True",
                }
            )
        },
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "lf_add_comm",
                    "constant_kind": "theorem",
                    "goal_v1": "x y : Nat\n⊢ x + y = y + x",
                }
            )
        },
    )
    backend = _StaticBackend(context, status=LeanStatus.VALID, messages=messages)
    declarations = (
        ElaboratedInput("Outer.Inner.inline", "theorem", inline_raw),
        ElaboratedInput("lf_add_comm", "theorem", loaded_raw, lookup_only=True),
    )

    result = render_elaborated_batch(
        backend,
        declarations=declarations,
        compile_context=context,
        request_id="goal-v1-context",
    )

    assert not result.failures
    code = backend.requests[0].code
    assert code is not None
    assert 'scoped notation "GOALV1NAT" => Nat' in code
    assert "set_option Elab.async false" in code
    assert "set_option maxHeartbeats 0" in code
    assert "open Nat" in code
    assert "open scoped GoalV1Scope" in code
    assert "namespace Outer\nnamespace Inner" in code
    assert "end Inner\nend Outer" in code
    assert inline_raw in code
    assert loaded_raw not in code
    assert result.sidecars[1].raw_statement == loaded_raw
    assert result.sidecars[1].record.warnings == ("already_loaded_constant_lookup",)


def test_elaborated_kind_is_checked_against_environment() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "notATheorem",
                    "constant_kind": "definition",
                    "goal_v1": "⊢ True",
                }
            )
        },
    )
    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID, messages=messages),
        declarations=(
            ElaboratedInput(
                "notATheorem",
                "theorem",
                "theorem notATheorem : True := True.intro",
            ),
        ),
        compile_context=context,
        request_id="goal-v1-kind",
    )

    assert not result.sidecars
    assert (
        result.failures[0].detail == "environment constant kind is 'definition', expected theorem"
    )


def test_elaborated_sorry_policy_is_enforced() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "withSorry",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ True",
                }
            )
        },
    )
    declaration = ElaboratedInput("withSorry", "theorem", "theorem withSorry : True := by sorry")

    rejected = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID_WITH_SORRY, messages=messages),
        declarations=(declaration,),
        compile_context=context,
        request_id="goal-v1-sorry-reject",
    )
    accepted = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID_WITH_SORRY, messages=messages),
        declarations=(declaration,),
        compile_context=context,
        request_id="goal-v1-sorry-accept",
        allow_sorry=True,
    )

    assert not rejected.sidecars
    assert len(rejected.failures) == 1
    assert not accepted.failures
    assert accepted.sidecars[0].record.warnings[-1] == "compiled_with_sorry"


def test_mixed_invalid_batch_with_reported_sorry_fails_closed() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "withSorry",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ True",
                }
            )
        },
        {"severity": "error", "data": "broken declaration failed"},
    )
    declarations = (
        ElaboratedInput("withSorry", "theorem", "theorem withSorry : True := by sorry"),
        ElaboratedInput("broken", "theorem", "theorem broken : Missing := by exact missing"),
    )

    result = render_elaborated_batch(
        _StaticBackend(
            context,
            status=LeanStatus.INVALID,
            messages=messages,
            sorries=({"declaration": "withSorry"},),
        ),
        declarations=declarations,
        compile_context=context,
        request_id="goal-v1-invalid-sorry",
    )

    assert not result.sidecars
    assert [failure.declaration_name for failure in result.failures] == [
        "withSorry",
        "broken",
    ]


@pytest.mark.parametrize(
    "diagnostic",
    ["declaration uses `sorry`", "declaration uses 'sorry'"],
)
def test_mixed_invalid_batch_with_diagnostic_only_sorry_fails_closed(
    diagnostic: str,
) -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "severity": "info",
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "withSorry",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ True",
                }
            ),
        },
        {"severity": "warning", "data": f"warning: {diagnostic}"},
        {"severity": "error", "data": "broken declaration failed"},
    )
    declarations = (
        ElaboratedInput("withSorry", "theorem", "theorem withSorry : True := by sorry"),
        ElaboratedInput("broken", "theorem", "theorem broken : Missing := by exact missing"),
    )

    result = render_elaborated_batch(
        _StaticBackend(
            context,
            status=LeanStatus.INVALID,
            messages=messages,
            sorries=(),
        ),
        declarations=declarations,
        compile_context=context,
        request_id="goal-v1-invalid-diagnostic-sorry",
    )

    assert not result.sidecars
    assert [failure.declaration_name for failure in result.failures] == [
        "withSorry",
        "broken",
    ]


def test_elaborated_multiline_let_goal_is_canonicalized() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "letClaim",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ have x := 1;\n  x = 1",
                }
            )
        },
    )
    raw = "theorem letClaim : let x := 1; x = 1 := by rfl"

    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID, messages=messages),
        declarations=(ElaboratedInput("letClaim", "theorem", raw),),
        compile_context=context,
        request_id="goal-v1-let",
    )

    assert not result.failures
    assert result.sidecars[0].core_text() == "⊢ let x := 1; x = 1"


@pytest.mark.parametrize(
    ("name", "payload_goal", "expected"),
    [
        (
            "chain",
            "⊢ have x := 1;\n  have y := x;\n  y = 1",
            "⊢ let x := 1; let y := x; y = 1",
        ),
        (
            "existsChain",
            "⊢ ∃ n : Nat,\n  have x := n;\n  have y := x;\n  y = n",
            "⊢ ∃ n : Nat, let x := n; let y := x; y = n",
        ),
        (
            "conjunctionChain",
            "⊢ True ∧ (have x := 1;\n  have y := x;\n  y = 1)",
            "⊢ True ∧ (let x := 1; let y := x; y = 1)",
        ),
        (
            "emptyStringBinding",
            '⊢ have s := "";\n  s = ""',
            '⊢ let s := ""; s = ""',
        ),
        (
            "charBinding",
            "⊢ have c := ';';\n  c = ';'",
            "⊢ let c := ';'; c = ';'",
        ),
        (
            "generatedBinding",
            "⊢ have a✝ := 1;\n  a✝ = 1",
            "⊢ let a✝ := 1; a✝ = 1",
        ),
    ],
)
def test_elaborated_binding_chains_share_the_structural_canonicalizer(
    name: str,
    payload_goal: str,
    expected: str,
) -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": name,
                    "constant_kind": "theorem",
                    "goal_v1": payload_goal,
                }
            )
        },
    )

    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID, messages=messages),
        declarations=(ElaboratedInput(name, "theorem", f"theorem {name} : True := by trivial"),),
        compile_context=context,
        request_id=f"goal-v1-{name}",
    )

    assert not result.failures
    assert result.sidecars[0].core_text() == expected


@pytest.mark.parametrize(
    ("name", "payload_goal", "expected"),
    [
        (
            "localBinding",
            "h : have x := 1;\n  have y := x;\n  y = 1\n⊢ True",
            "h : let x := 1; let y := x; y = 1\n⊢ True",
        ),
        (
            "colonOnlyLocalBinding",
            "h :\n  have x := 1;\n  have y := x;\n  y = 1\n⊢ True",
            "h : let x := 1; let y := x; y = 1\n⊢ True",
        ),
        (
            "colonOnlyThenLocal",
            "h :\n  have x := 1;\n  have y := x;\n  y = 1\nk : Nat\n⊢ True",
            "h : let x := 1; let y := x; y = 1\nk : Nat\n⊢ True",
        ),
        (
            "localLiteral",
            'h : ":=" = ":="\n⊢ True',
            'h : ":=" = ":="\n⊢ True',
        ),
        (
            "escapedLocalName",
            "«foo : have x := 1; x» : Nat\n⊢ True",
            "«foo : have x := 1; x» : Nat\n⊢ True",
        ),
    ],
)
def test_elaborated_local_types_share_structural_validation(
    name: str,
    payload_goal: str,
    expected: str,
) -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": name,
                    "constant_kind": "theorem",
                    "goal_v1": payload_goal,
                }
            )
        },
    )

    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID, messages=messages),
        declarations=(ElaboratedInput(name, "theorem", f"theorem {name} : True := by trivial"),),
        compile_context=context,
        request_id=f"goal-v1-{name}",
    )

    assert not result.failures
    assert result.sidecars[0].core_text() == expected


@pytest.mark.parametrize("local_name", ['"x"', "'x'", "if", "rec", "x.y", "+"])
def test_elaborated_local_names_are_structurally_validated(local_name: str) -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "badLocalName",
                    "constant_kind": "theorem",
                    "goal_v1": f"{local_name} : Nat\n⊢ True",
                }
            )
        },
    )

    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID, messages=messages),
        declarations=(
            ElaboratedInput(
                "badLocalName",
                "theorem",
                "theorem badLocalName : True := by trivial",
            ),
        ),
        compile_context=context,
        request_id=f"goal-v1-bad-local-{local_name}",
    )

    assert not result.sidecars
    assert len(result.failures) == 1
    assert "unsupported binder name syntax" in result.failures[0].detail


def test_elaborated_incomplete_binding_payload_fails_closed() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "incompleteChain",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ have x := 1;\n  have y",
                }
            )
        },
    )

    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID, messages=messages),
        declarations=(
            ElaboratedInput(
                "incompleteChain",
                "theorem",
                "theorem incompleteChain : True := by trivial",
            ),
        ),
        compile_context=context,
        request_id="goal-v1-incomplete-chain",
    )

    assert not result.sidecars
    assert len(result.failures) == 1
    assert "incomplete have binding" in result.failures[0].detail


@pytest.mark.parametrize(
    ("name", "payload_goal", "detail"),
    [
        ("emptyBindingType", "⊢ have x : := 1; True", "binding type is empty"),
        ("garbageBindingHead", "⊢ have x y := 1; True", "unsupported have binding head"),
        (
            "incompleteBindingBody",
            "⊢ have x := 1; x =",
            "expression ends with an incomplete operator",
        ),
    ],
)
def test_elaborated_malformed_binding_fragments_fail_closed(
    name: str,
    payload_goal: str,
    detail: str,
) -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": name,
                    "constant_kind": "theorem",
                    "goal_v1": payload_goal,
                }
            )
        },
    )

    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID, messages=messages),
        declarations=(ElaboratedInput(name, "theorem", f"theorem {name} : True := by trivial"),),
        compile_context=context,
        request_id=f"goal-v1-{name}",
    )

    assert not result.sidecars
    assert len(result.failures) == 1
    assert detail in result.failures[0].detail


def test_elaborated_colon_only_local_without_a_type_fails_closed() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "emptyLocalType",
                    "constant_kind": "theorem",
                    "goal_v1": "h :\n⊢ True",
                }
            )
        },
    )

    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID, messages=messages),
        declarations=(
            ElaboratedInput(
                "emptyLocalType",
                "theorem",
                "theorem emptyLocalType : True := by trivial",
            ),
        ),
        compile_context=context,
        request_id="goal-v1-empty-local-type",
    )

    assert not result.sidecars
    assert len(result.failures) == 1
    assert "structural name/type separator" in result.failures[0].detail


def test_elaborated_char_literal_cannot_supply_a_missing_body_separator() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "incompleteCharBinding",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ have c := ';'",
                }
            )
        },
    )

    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID, messages=messages),
        declarations=(
            ElaboratedInput(
                "incompleteCharBinding",
                "theorem",
                "theorem incompleteCharBinding : True := by trivial",
            ),
        ),
        compile_context=context,
        request_id="goal-v1-incomplete-char-binding",
    )

    assert not result.sidecars
    assert len(result.failures) == 1
    assert "missing ';' body separator" in result.failures[0].detail


def test_invalid_batch_preserves_payloads_that_were_rendered() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "first",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ True",
                }
            )
        },
        {"severity": "error", "data": "second declaration failed"},
    )
    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.INVALID, messages=messages),
        declarations=(
            ElaboratedInput("first", "theorem", "theorem first : True := True.intro"),
            ElaboratedInput("second", "theorem", "theorem second : Missing := by exact missing"),
        ),
        compile_context=context,
        request_id="goal-v1-partial",
    )

    assert [sidecar.record.goal_v1 for sidecar in result.sidecars] == ["⊢ True"]
    assert result.sidecars[0].record.warnings[-1] == "batch_had_lean_errors"
    assert [failure.declaration_name for failure in result.failures] == ["second"]


def test_elaborated_parser_takes_last_matching_payload_fail_closed() -> None:
    class _SpoofBackend(_OneRequestBackend):
        def run(self, request: LeanRequest) -> LeanResult:
            base = super().run(request)
            return LeanResult(
                request_id=base.request_id,
                request_hash=base.request_hash,
                context_id=base.context_id,
                context_fingerprint=base.context_fingerprint,
                status=base.status,
                messages=({"severity": "info", "data": f'{GOAL_MARKER}{{"name":"first"}}'},),
            )

    context = _context()
    result = render_elaborated_batch(
        _SpoofBackend(context),
        declarations=(
            ElaboratedInput("first", "theorem", "theorem first (x : Nat) : x = x := rfl"),
            ElaboratedInput("second", "lemma", "lemma second : True := True.intro"),
        ),
        compile_context=context,
        request_id="goal-v1-spoof",
    )

    assert not result.sidecars
    assert {failure.declaration_name for failure in result.failures} == {"first", "second"}


def test_goal_v1_module_does_not_import_leaninteract_backend() -> None:
    source = (_REPO_ROOT / "src" / "leanfaith" / "representations" / "goal_v1.py").read_text(
        encoding="utf-8"
    )
    assert "leanfaith.lean.leaninteract_backend" not in source
