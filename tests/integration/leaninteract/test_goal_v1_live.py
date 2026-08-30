"""One loaded Lean environment: cross-path smoke, then bounded REPR pilot."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import yaml

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.goal_v1 import (
    ClosedExprInput,
    ClosedExprSourceMaterial,
    CompileContext,
    ElaboratedInput,
    SurfaceRenderError,
    render_closed_expr_in_session,
    render_elaborated_batch,
    render_surface,
)

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_FIXTURES = _REPO_ROOT / "tests" / "lean_fixtures"
_CONFIG = _REPO_ROOT / "configs" / "representations" / "goal_v1_v1.yaml"
_TRANSFORM_ENGINE = _REPO_ROOT / "LeanFaith" / "Meta" / "TransformEngine.lean"


def _compile_context() -> CompileContext:
    transform_engine = _import_stripped_transform_engine()
    return CompileContext(
        project_id="fixtures",
        project_revision="workspace",
        lean_version="v4.31.0-rc1",
        import_header="import LeanFaithFixtures",
        command_preamble="""namespace GoalV1ContextOpen
def ContextNat : Type := Nat
end GoalV1ContextOpen
namespace GoalV1ContextScope
scoped notation "GoalV1ContextNat" => GoalV1ContextOpen.ContextNat
end GoalV1ContextScope
universe u
"""
        + transform_engine,
        namespace_context=("GoalV1Structured",),
        open_context=("Lean", "Lean.Meta", "GoalV1ContextOpen"),
        scoped_context=("GoalV1ContextScope",),
        options={"Elab.async": False, "autoImplicit": False},
    )


def _qualified(name: str) -> str:
    return f"GoalV1Structured.{name}"


class _DiagnosticOnlySorryBackend:
    """Expose a real INVALID response after dropping only its structured sorry payload."""

    def __init__(self, delegate: LeanInteractBackend) -> None:
        self.delegate = delegate

    def run(self, request: LeanRequest) -> LeanResult:
        result = self.delegate.run(request)
        assert result.status == LeanStatus.INVALID
        assert result.sorries
        assert any(
            message.get("severity") in {"warning", "error"}
            and "declaration uses `sorry`" in str(message.get("data", ""))
            for message in result.messages
        )
        return replace(result, sorries=())

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


class _CapturingBackend:
    """Capture the one direct-Expr request while reusing the live backend."""

    def __init__(self, delegate: LeanInteractBackend) -> None:
        self.delegate = delegate
        self.requests: list[LeanRequest] = []
        self.results: list[LeanResult] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        result = self.delegate.run(request)
        self.results.append(result)
        return result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        raise AssertionError(f"direct Expr route must use one request, got {len(requests)}")

    def close(self) -> None:
        return None


def _import_stripped_transform_engine() -> str:
    source = _TRANSFORM_ENGINE.read_text(encoding="utf-8")
    return "\n".join(line for line in source.splitlines() if not line.startswith("import "))


def _closed_expr_live_body() -> str:
    return '''run_meta do
  let expectFailure
      (caseId expectedCode : String) (action : MetaM String) : MetaM Unit := do
    let observed? ←
      try
        let _ ← action
        pure (none : Option String)
      catch ex =>
        if ex.isInterrupt then throw ex
        pure (some (← ex.toMessageData.toString))
    match observed? with
    | none => throwError m!"{caseId}: expected {expectedCode}, but rendering succeeded"
    | some observed =>
        unless observed.contains expectedCode do
          throwError m!"{caseId}: expected {expectedCode}, got {observed}"

  let env ← getEnv
  let constantCountBefore := env.constants.toList.length
  let some namedCi := (← getEnv).find? ``LeanFaithPrivateFixture.publicComplex
    | throwError "goal_v1_live_named_constant_missing"
  let namedGoal ← LeanFaith.GoalV1.renderConstantType namedCi
  let directGoal ← LeanFaith.GoalV1.renderClosedProp namedCi.type
  unless namedGoal == directGoal do
    throwError "goal_v1_live_named_direct_mismatch"
  let annotatedGoal ← LeanFaith.GoalV1.renderClosedProp (.mdata {} namedCi.type)
  unless annotatedGoal == directGoal do
    throwError "goal_v1_live_metadata_changed_telescope_rendering"

  let some sourceCi := (← getEnv).find? `lf_trivial
    | throwError "goal_v1_live_source_constant_missing"
  let sourceExpr := LeanFaith.Meta.TransformEngineHelper.lfTransformCanonicalType sourceCi
  let candidates ← LeanFaith.Meta.TransformEngineHelper.lfWholeCandidates sourceExpr
  let candidate? := candidates.find? fun candidate =>
    candidate.family == "P21" && candidate.operation == "zetaIntroduce" &&
      candidate.sitePath == "/"
  let some candidate := candidate?
    | throwError "goal_v1_live_structural_candidate_missing"
  unless ← LeanFaith.Meta.TransformEngineHelper.lfCheckedProp candidate.candidate do
    throwError "goal_v1_live_candidate_not_prop"
  unless ← LeanFaith.Meta.TransformEngineHelper.lfWholeDefEq sourceExpr candidate.candidate do
    throwError "goal_v1_live_candidate_not_whole_defeq"

  let assigned ← mkFreshExprMVar (mkSort Level.zero)
  assigned.mvarId!.assign (mkConst ``True)
  let assignedText ← LeanFaith.GoalV1.renderClosedProp assigned
  unless assignedText == "⊢ True" do
    throwError "goal_v1_live_assigned_expr_mvar_not_instantiated"
  withLocalDeclD `ambient (mkSort Level.zero) fun _ => do
    let isolatedText ← LeanFaith.GoalV1.renderClosedProp (mkConst ``True)
    unless isolatedText == "⊢ True" do
      throwError "goal_v1_live_ambient_local_context_leaked"

  expectFailure "expr_mvar" "goal_v1_unresolved_expr_mvar" do
    let e ← mkFreshExprMVar (mkSort Level.zero)
    LeanFaith.GoalV1.renderClosedProp e
  expectFailure "universe_mvar" "goal_v1_unresolved_universe_mvar" do
    let u ← mkFreshLevelMVar
    let e : Expr := .forallE `α (.sort u) (mkConst ``True) .default
    LeanFaith.GoalV1.renderClosedProp e
  withLocalDeclD `p (mkSort Level.zero) fun p =>
    expectFailure "free_variable" "goal_v1_free_variable" <|
      LeanFaith.GoalV1.renderClosedProp p
  expectFailure "loose_bvar" "goal_v1_loose_bound_variable" <|
    LeanFaith.GoalV1.renderClosedProp (.bvar 0)
  expectFailure "non_prop" "goal_v1_not_prop" <|
    LeanFaith.GoalV1.renderClosedProp (mkConst ``Nat)
  let structuralArrow : Expr :=
    .forallE Name.anonymous (mkConst ``True) (mkConst ``True) .default
  let arrowText ← LeanFaith.GoalV1.renderClosedProp structuralArrow
  unless arrowText == "⊢ True → True" do
    throwError "goal_v1_live_structural_arrow_not_preserved"
  let generatedArrow ← mkArrow (mkConst ``True) (mkConst ``True)
  let generatedArrowText ← LeanFaith.GoalV1.renderClosedProp generatedArrow
  unless generatedArrowText == "⊢ True → True" do
    throwError "goal_v1_live_generated_arrow_not_preserved"
  let anonymousImplicit : Expr :=
    .forallE Name.anonymous (mkSort Level.zero) (mkConst ``True) .implicit
  expectFailure "anonymous_implicit_binder" "goal_v1_unsupported_anonymous_telescope_binder" <|
    LeanFaith.GoalV1.renderClosedProp anonymousImplicit
  let dependentAnonymous : Expr :=
    .forallE Name.anonymous (mkConst ``Nat)
      (mkApp3 (mkConst ``Eq [Level.succ Level.zero])
        (mkConst ``Nat) (.bvar 0) (.bvar 0)) .default
  expectFailure "dependent_anonymous_binder" "goal_v1_unsupported_anonymous_telescope_binder" <|
    LeanFaith.GoalV1.renderClosedProp dependentAnonymous
  expectFailure
      "annotated_anonymous_binder" "goal_v1_unsupported_anonymous_telescope_binder" <|
    LeanFaith.GoalV1.renderClosedProp (.mdata {} dependentAnonymous)

  LeanFaith.GoalV1.emitClosedProp
    "reference" "scope:goal-v1-live-pair" "loaded_constant_type" sourceExpr
  LeanFaith.GoalV1.emitClosedProp
    "candidate" "scope:goal-v1-live-pair" "sft1_transformed_expr" candidate.candidate
  let constantCountAfter := (← getEnv).constants.toList.length
  unless constantCountAfter == constantCountBefore do
    throwError "goal_v1_live_closed_expr_route_modified_environment"'''


def _multi_source_surface_pilot() -> tuple[int, int, int]:
    config = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    cases = cast(list[dict[str, str]], config["pilot"]["cases"])
    successes = 0
    expected_failures = 0
    start = time.perf_counter()
    for case in cases:
        context = CompileContext(
            project_id=case["source_family"],
            project_revision=case["source_revision"],
            lean_version="recorded_by_source",
            import_header="import Lean",
        )
        try:
            sidecar = render_surface(
                raw_statement=case["raw_statement"],
                declaration_kind="theorem",
                compile_context=context,
                parsed_signature=case["parsed_signature"],
            )
        except SurfaceRenderError as exc:
            assert exc.code.value == case["expected_failure"]
            expected_failures += 1
            continue
        assert sidecar.core_text() == case["expected_goal_v1"]
        assert sidecar.raw_statement == case["raw_statement"]
        assert sidecar.record.goal_v1_source == "surface"
        successes += 1
    elapsed_ms = round((time.perf_counter() - start) * 1000)
    return successes, expected_failures, elapsed_ms


def test_goal_v1_cross_path_smoke_then_bounded_pilot(
    tmp_path: Path,
) -> None:
    """The smoke assertion occurs before any pilot work in this test."""

    context = _compile_context()
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=context.fingerprint,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            workers=1,
            enable_parallel_elaboration=False,
        )
    )
    try:
        # Gate 1: one complete theorem through both paths, including a cache replay.
        smoke_raw = "theorem goalV1Smoke (x y : ℕ) (h : x < y) : x ≤ y := Nat.le_of_lt h"
        surface_smoke = render_surface(
            raw_statement=smoke_raw,
            declaration_kind="theorem",
            compile_context=context,
            parsed_signature="(x y : ℕ) (h : x < y) : x ≤ y",
        )
        smoke_input = ElaboratedInput(_qualified("goalV1Smoke"), "theorem", smoke_raw)
        elaborated_smoke = render_elaborated_batch(
            backend,
            declarations=(smoke_input,),
            compile_context=context,
            request_id="goal-v1-smoke",
        )
        assert not elaborated_smoke.failures
        assert len(elaborated_smoke.sidecars) == 1
        assert elaborated_smoke.sidecars[0].core_text() == surface_smoke.core_text()
        assert elaborated_smoke.sidecars[0].core_text() == ("x y : ℕ\nh : x < y\n⊢ x ≤ y")
        assert elaborated_smoke.sidecars[0].raw_statement == smoke_raw
        assert surface_smoke.raw_statement == smoke_raw
        assert ":=" not in elaborated_smoke.sidecars[0].core_text()
        assert "goalV1Smoke" not in elaborated_smoke.sidecars[0].core_text()

        replay = render_elaborated_batch(
            backend,
            declarations=(smoke_input,),
            compile_context=context,
            request_id="goal-v1-smoke-replay",
        )
        assert not replay.failures
        assert replay.request_hash == elaborated_smoke.request_hash
        assert replay.sidecars == elaborated_smoke.sidecars

        # Gate 2: one Meta request renders a loaded ConstantInfo and an actual
        # SFT1-engine Expr candidate through the shared API, then proves the
        # direct API fails closed for every unsupported expression shape.
        direct_inputs = (
            ClosedExprInput(
                endpoint_id="reference",
                endpoint_role="reference",
                expr_origin="loaded_constant_type",
                source_material=ClosedExprSourceMaterial(
                    kind="raw_statement",
                    raw_statement="theorem lf_trivial : True := trivial",
                ),
            ),
            ClosedExprInput(
                endpoint_id="candidate",
                endpoint_role="candidate",
                expr_origin="sft1_transformed_expr",
                source_material=ClosedExprSourceMaterial(
                    kind="constructed_expr_no_source_text",
                    absence_reason="P21 candidate constructed structurally by TransformEngine",
                ),
            ),
        )
        capturing_backend = _CapturingBackend(backend)
        direct_pair = render_closed_expr_in_session(
            capturing_backend,
            inputs=direct_inputs,
            compile_context=context,
            render_scope_id="scope:goal-v1-live-pair",
            session_body=_closed_expr_live_body(),
            request_id="goal-v1-closed-expr-live-gate",
        )
        assert not direct_pair.failures
        assert len(direct_pair.sidecars) == 2
        assert len(capturing_backend.requests) == 1
        assert len(capturing_backend.results) == 1
        assert all(
            sidecar.record.provenance.render_scope_id == "scope:goal-v1-live-pair"
            for sidecar in direct_pair.sidecars
        )
        assert direct_pair.sidecars[0].core_text() != direct_pair.sidecars[1].core_text()
        direct_request_code = capturing_backend.requests[0].code
        assert direct_request_code is not None
        assert direct_request_code.count("run_meta do") == 1
        runtime_suffix = direct_request_code[direct_request_code.index("run_meta do") :]
        for forbidden_call in (
            "Term.elabTerm",
            "lfTextElaboratesAs",
            "lfCandidateEmission?",
            "lfTransformPp",
            "addDecl",
            "addAndCompile",
            "mkSorry",
            "sorryAx",
        ):
            assert forbidden_call not in runtime_suffix
        assert not any(
            token in runtime_suffix
            for token in ("theorem ", "lemma ", "axiom ", "opaque ", " := by")
        )
        for label in (
            "expr_mvar",
            "universe_mvar",
            "free_variable",
            "loose_bvar",
            "non_prop",
            "anonymous_implicit_binder",
            "dependent_anonymous_binder",
            "annotated_anonymous_binder",
        ):
            assert f'"{label}"' in runtime_suffix

        # Gate 3a: one bounded elaborated request covering difficult syntax.
        long_conjunction = " ∧ ".join(["True"] * 24)
        pilot_inputs = (
            ElaboratedInput(
                _qualified("goalV1PilotDependent"),
                "theorem",
                """theorem goalV1PilotDependent {α : Type u} [Inhabited α] (x : α)
                  (h : ∀ y : α, y = y) : ((fun z => z) x = x) ∧ x = x := by
                  exact ⟨rfl, h x⟩""",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotCoercion"),
                "theorem",
                "theorem goalV1PilotCoercion (x : Nat) : ((x : Int) = x) := rfl",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotHelper"),
                "theorem",
                """def goalV1PilotHelperFn (x : Nat) : Nat := x
                theorem goalV1PilotHelper (x : Nat) : goalV1PilotHelperFn x = x := rfl""",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotProofLeak"),
                "theorem",
                """theorem goalV1PilotProofLeak : True := by
                  have proofOnlySentinel : String := "LEANFAITH_GOAL_V1_PROOF_SENTINEL"
                  exact True.intro""",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotArrow"),
                "theorem",
                "theorem goalV1PilotArrow : True → True := fun h => h",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotShadow"),
                "theorem",
                """theorem goalV1PilotShadow (x : Nat) (x : Fin (x + 1)) : True := by
                  exact True.intro""",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotNot"),
                "theorem",
                "theorem goalV1PilotNot (p : Prop) (hp : p) : ¬¬p := fun h => h hp",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotLong"),
                "theorem",
                f"theorem goalV1PilotLong (h : {long_conjunction}) : True := True.intro",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotStructuredContext"),
                "theorem",
                """theorem goalV1PilotStructuredContext (n : GoalV1ContextNat) :
                  n = n := rfl""",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotLet"),
                "theorem",
                "theorem goalV1PilotLet : let x := 1; x = 1 := by rfl",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotLetChain"),
                "theorem",
                "theorem goalV1PilotLetChain : let x := 1; let y := x; y = 1 := by rfl",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotHaveChain"),
                "theorem",
                "theorem goalV1PilotHaveChain : have x := 1; have y := x; y = 1 := by rfl",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotEmptyStringBinding"),
                "theorem",
                'theorem goalV1PilotEmptyStringBinding : let s := ""; s = "" := by rfl',
            ),
            ElaboratedInput(
                _qualified("goalV1PilotCharBinding"),
                "theorem",
                "theorem goalV1PilotCharBinding : let c := ';'; c = ';' := by rfl",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotLocalLetChain"),
                "theorem",
                "theorem goalV1PilotLocalLetChain "
                "(h : let x := 1; let y := x; y = 1) : True := True.intro",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotExistsLetChain"),
                "theorem",
                "theorem goalV1PilotExistsLetChain : "
                "∃ n, let x := n; let y := x; y = 1 := by exact ⟨1, rfl⟩",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotForallLetChain"),
                "theorem",
                "theorem goalV1PilotForallLetChain : "
                "∀ n : ℕ, let x := n; let y := x; y = n := by intro n; rfl",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotConjunctionLetChain"),
                "theorem",
                "theorem goalV1PilotConjunctionLetChain : "
                "True ∧ let x := 1; let y := x; y = 1 := by simp",
            ),
            ElaboratedInput(
                "lf_add_comm",
                "theorem",
                "theorem lf_add_comm (x y : Nat) : x + y = y + x := Nat.add_comm x y",
                lookup_only=True,
            ),
        )
        surface_signatures = {
            _qualified("goalV1PilotDependent"): (
                "{α : Type u} [Inhabited α] (x : α) (h : ∀ y : α, y = y) : "
                "((fun z => z) x = x) ∧ x = x"
            ),
            _qualified("goalV1PilotCoercion"): "(x : Nat) : ((x : Int) = x)",
            _qualified("goalV1PilotHelper"): "(x : Nat) : goalV1PilotHelperFn x = x",
            _qualified("goalV1PilotProofLeak"): ": True",
            _qualified("goalV1PilotArrow"): ": True → True",
            _qualified("goalV1PilotShadow"): "(x : Nat) (x : Fin (x + 1)) : True",
            _qualified("goalV1PilotNot"): "(p : Prop) (hp : p) : ¬¬p",
            _qualified("goalV1PilotLong"): f"(h : {long_conjunction}) : True",
            _qualified("goalV1PilotStructuredContext"): "(n : GoalV1ContextNat) : n = n",
            _qualified("goalV1PilotLet"): ": let x := 1; x = 1",
            _qualified("goalV1PilotLetChain"): ": let x := 1; let y := x; y = 1",
            _qualified("goalV1PilotHaveChain"): ": have x := 1; have y := x; y = 1",
            _qualified("goalV1PilotEmptyStringBinding"): ': let s := ""; s = ""',
            _qualified("goalV1PilotCharBinding"): ": let c := ';'; c = ';'",
            _qualified("goalV1PilotLocalLetChain"): ("(h : let x := 1; let y := x; y = 1) : True"),
            _qualified("goalV1PilotExistsLetChain"): (": ∃ n, let x := n; let y := x; y = 1"),
            _qualified("goalV1PilotForallLetChain"): (": ∀ n : ℕ, let x := n; let y := x; y = n"),
            _qualified("goalV1PilotConjunctionLetChain"): (
                ": True ∧ let x := 1; let y := x; y = 1"
            ),
            "lf_add_comm": "(x y : Nat) : x + y = y + x",
        }
        assert len(surface_signatures) == len(pilot_inputs)
        pilot = render_elaborated_batch(
            backend,
            declarations=pilot_inputs,
            compile_context=context,
            request_id="goal-v1-live-pilot",
        )
        assert not pilot.failures
        assert len(pilot.sidecars) == len(pilot_inputs)
        assert all(sidecar.record.goal_v1_source == "elaborated" for sidecar in pilot.sidecars)
        assert all(sidecar.core_text().count("⊢") == 1 for sidecar in pilot.sidecars)
        assert all(
            "LEANFAITH_GOAL_V1_PROOF_SENTINEL" not in sidecar.core_text()
            for sidecar in pilot.sidecars
        )
        by_name = {
            item.declaration_name: sidecar.core_text()
            for item, sidecar in zip(pilot_inputs, pilot.sidecars, strict=True)
        }
        assert "inst✝ : Inhabited α" in by_name[_qualified("goalV1PilotDependent")]
        assert "α : Type u_0" in by_name[_qualified("goalV1PilotDependent")]
        assert by_name[_qualified("goalV1PilotShadow")] == ("x✝ : ℕ\nx : Fin (x✝ + 1)\n⊢ True")
        assert by_name[_qualified("goalV1PilotNot")] == "p : Prop\nhp : p\n⊢ ¬¬p"
        long_goal = by_name[_qualified("goalV1PilotLong")]
        assert len(long_goal.splitlines()) == 2
        assert len(long_goal.splitlines()[0]) > 120
        assert by_name[_qualified("goalV1PilotStructuredContext")].endswith("⊢ n = n")
        assert by_name[_qualified("goalV1PilotLet")] == "⊢ let x := 1; x = 1"
        binding_expectations = {
            _qualified("goalV1PilotLetChain"): "⊢ let x := 1; let y := x; y = 1",
            _qualified("goalV1PilotHaveChain"): "⊢ let x := 1; let y := x; y = 1",
            _qualified("goalV1PilotEmptyStringBinding"): '⊢ let s := ""; s = ""',
            _qualified("goalV1PilotCharBinding"): "⊢ let c := ';'; c = ';'",
            _qualified("goalV1PilotLocalLetChain"): ("h : let x := 1; let y := x; y = 1\n⊢ True"),
            _qualified("goalV1PilotExistsLetChain"): "⊢ ∃ n, let x := n; let y := x; y = 1",
            _qualified("goalV1PilotForallLetChain"): ("n : ℕ\n⊢ let x := n; let y := x; y = n"),
            _qualified("goalV1PilotConjunctionLetChain"): (
                "⊢ True ∧ let x := 1; let y := x; y = 1"
            ),
        }
        for declaration_name, expected in binding_expectations.items():
            assert by_name[declaration_name] == expected
        binding_names = {_qualified("goalV1PilotLet"), *binding_expectations}
        assert all(":=" not in goal for name, goal in by_name.items() if name not in binding_names)
        loaded_sidecar = next(
            sidecar
            for item, sidecar in zip(pilot_inputs, pilot.sidecars, strict=True)
            if item.declaration_name == "lf_add_comm"
        )
        assert loaded_sidecar.record.warnings == ("already_loaded_constant_lookup",)
        assert loaded_sidecar.raw_statement == pilot_inputs[-1].raw_statement

        surface_agreements = 0
        surface_failures = 0
        for item, elaborated_sidecar in zip(pilot_inputs, pilot.sidecars, strict=True):
            try:
                surface_sidecar = render_surface(
                    raw_statement=item.raw_statement,
                    declaration_kind=item.declaration_kind,
                    compile_context=context,
                    parsed_signature=surface_signatures[item.declaration_name],
                )
            except SurfaceRenderError:
                surface_failures += 1
                continue
            if item.declaration_name in binding_expectations:
                assert surface_sidecar.core_text() == elaborated_sidecar.core_text()
            surface_agreements += surface_sidecar.core_text() == elaborated_sidecar.core_text()

        assert len(pilot_inputs) == 19
        assert surface_failures == 2
        assert surface_agreements == 14

        # Gate 3b: any Lean-reported sorry fails closed, even when another
        # declaration makes the overall batch INVALID.
        rejected_sorry_inputs = (
            ElaboratedInput(
                _qualified("goalV1PilotRejectedSorry"),
                "theorem",
                "theorem goalV1PilotRejectedSorry : True := by sorry",
            ),
            ElaboratedInput(
                _qualified("goalV1PilotBroken"),
                "theorem",
                "theorem goalV1PilotBroken : MissingType := by exact missing",
            ),
        )
        rejected_sorry = render_elaborated_batch(
            backend,
            declarations=rejected_sorry_inputs,
            compile_context=context,
            request_id="goal-v1-invalid-sorry-regression",
        )
        assert not rejected_sorry.sidecars
        assert [failure.declaration_name for failure in rejected_sorry.failures] == [
            item.declaration_name for item in rejected_sorry_inputs
        ]

        diagnostic_only_sorry = render_elaborated_batch(
            _DiagnosticOnlySorryBackend(backend),
            declarations=rejected_sorry_inputs,
            compile_context=context,
            request_id="goal-v1-invalid-diagnostic-only-sorry-regression",
        )
        assert not diagnostic_only_sorry.sidecars
        assert [failure.declaration_name for failure in diagnostic_only_sorry.failures] == [
            item.declaration_name for item in rejected_sorry_inputs
        ]

        # Gate 3c: incomplete binding syntax fails on both paths. This is one
        # bounded invalid fixture, not a corpus audit.
        incomplete_raw = "theorem goalV1PilotIncompleteLetChain : let x := 1; let y := x := by rfl"
        with pytest.raises(SurfaceRenderError, match="ambiguous_proof_boundary"):
            render_surface(
                raw_statement=incomplete_raw,
                declaration_kind="theorem",
                compile_context=context,
                parsed_signature=": let x := 1; let y := x",
            )
        incomplete = render_elaborated_batch(
            backend,
            declarations=(
                ElaboratedInput(
                    _qualified("goalV1PilotIncompleteLetChain"),
                    "theorem",
                    incomplete_raw,
                ),
            ),
            compile_context=context,
            request_id="goal-v1-incomplete-let-chain",
        )
        assert not incomplete.sidecars
        assert len(incomplete.failures) == 1

        # Gate 3d: the six pinned source-family fixtures stay purely surface-side.
        source_successes, source_expected_failures, source_elapsed_ms = (
            _multi_source_surface_pilot()
        )
        assert source_successes == 4
        assert source_expected_failures == 2
        metrics = {
            "smoke": {
                "agreement": True,
                "request_hash": elaborated_smoke.request_hash,
                "replay_request_hash": replay.request_hash,
                "first_elapsed_ms": elaborated_smoke.elapsed_ms,
                "replay_elapsed_ms": replay.elapsed_ms,
            },
            "closed_expr_shared_request": {
                "rows": len(direct_pair.sidecars),
                "fail_closed_cases": 8,
                "request_count": len(capturing_backend.requests),
                "request_hash": direct_pair.request_hash,
                "elapsed_ms": direct_pair.elapsed_ms,
            },
            "elaborated_pilot": {
                "rows": len(pilot_inputs),
                "elapsed_ms": pilot.elapsed_ms,
                "surface_agreements": surface_agreements,
                "surface_failures": surface_failures,
            },
            "mixed_invalid_sorry": {
                "sidecars": len(rejected_sorry.sidecars),
                "failures": len(rejected_sorry.failures),
                "diagnostic_only_sidecars": len(diagnostic_only_sorry.sidecars),
                "diagnostic_only_failures": len(diagnostic_only_sorry.failures),
            },
            "incomplete_binding": {
                "elaborated_sidecars": len(incomplete.sidecars),
                "elaborated_failures": len(incomplete.failures),
                "surface_failed_closed": True,
            },
            "multi_source_surface_pilot": {
                "rows": source_successes + source_expected_failures,
                "successes": source_successes,
                "expected_failures": source_expected_failures,
                "elapsed_ms": source_elapsed_ms,
            },
        }
        print("GOAL_V1_PILOT_METRICS " + json.dumps(metrics, sort_keys=True))
    finally:
        backend.close()
