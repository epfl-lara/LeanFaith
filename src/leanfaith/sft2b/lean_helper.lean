/-
SFT2B proof-free proposition elaboration helper.

This source is injected without its import command as a hash-bound static
preamble. The live action supplies exact proposition text, this helper parses
and elaborates it once in the fixed project context, and the resulting live
Expr is passed directly to the frozen GoalV1 renderer. It declares no theorem,
proof, axiom, or endpoint and never renders or re-elaborates pretty text.
-/
import Lean

namespace LeanFaith.SFT2B.Helper

open Lean Elab Meta

/-- Fail closed unless the elaborated result is one closed, well-typed Prop. -/
def checkedClosedProp (origin : String) (e : Expr) : MetaM Expr := do
  let e ← instantiateMVars e
  if e.hasExprMVar then
    throwError m!"{origin}: unresolved expression metavariable"
  if e.hasLevelMVar then
    throwError m!"{origin}: unresolved universe metavariable"
  if e.hasFVar then
    throwError m!"{origin}: free variable"
  if e.hasLooseBVars then
    throwError m!"{origin}: loose bound variable"
  if e.hasSorry then
    throwError m!"{origin}: proposition contains a proof placeholder"
  check e
  unless ← isProp e do
    throwError m!"{origin}: expected a proposition"
  return e

/-- Parse a proof-free proposition term and elaborate it exactly once. -/
def elaborateProposition
    (origin source : String) (levelNames : List Name) : MetaM Expr := do
  let stx ←
    match Parser.runParserCategory (← getEnv) `term source with
    | .ok stx => pure stx
    | .error err => throwError m!"{origin}: proposition parse failed: {err}"
  let e ← Term.TermElabM.run' do
    Term.withLevelNames levelNames do
      let e ← Term.elabTerm stx (some (mkSort .zero))
      Term.synthesizeSyntheticMVarsNoPostponing
      instantiateMVars e
  checkedClosedProp origin e

end LeanFaith.SFT2B.Helper
