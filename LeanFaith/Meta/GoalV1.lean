/-
Frozen `goal_v1.0` elaborated renderer.

The Python REPR owner injects this import-stripped helper into one batched
LeanInteract request.  Each declaration is already present in the loaded
environment (or was compiled earlier in that same request), so this never
recompiles a theorem proof merely to obtain a model view.
-/
import Lean

namespace LeanFaith.GoalV1

open Lean Elab Command Meta

/-- All display-affecting choices are rooted in `Options.empty`.  A very wide
    page makes physical line breaks semantic only at local/target boundaries,
    rather than dependent on a caller's editor width. -/
def rendererOptions : Options :=
  Options.empty
    |>.setBool `pp.universes false
    |>.setBool `pp.coercions true
    |>.setBool `pp.notation true
    |>.setBool `pp.mvars false
    |>.setBool `pp.inaccessibleNames true
    |>.setBool `pp.implementationDetailHyps true

/-- Turn a theorem's complete Pi telescope into the familiar goal-state view.
    `ppGoal` preserves local order, groups adjacent equal-type locals, and
    sanitizes shadowed/generated names using Lean's own local context rules. -/
def renderConstantType (ci : ConstantInfo) : MetaM String := do
  forallTelescope ci.type fun _ target => do
    let goal ← mkFreshExprMVar target MetavarKind.syntheticOpaque
    withOptions (fun _ => rendererOptions) do
      return (← ppGoal goal.mvarId!).pretty (width := 1000000)

def constantKind : ConstantInfo → String
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "definition"
  | .thmInfo _ => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quotient"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"

def printNotFound (name : String) : IO Unit := do
  let payload := Json.mkObj [
    ("name", Json.str name),
    ("notfound", Json.bool true)
  ]
  IO.println s!"LFGOALV1JSON {payload.compress}"

/-- Emit the frozen goal view as one JSON payload.  The declaration name is a
    string literal so dotted and guillemet identifiers remain safe. -/
elab "lfGoalV1 " name:str : command => do
  let lookup := name.getString
  let constantName := lookup.toName
  liftTermElabM do
    match (← getEnv).find? constantName with
    | none => printNotFound lookup
    | some ci =>
      let kind := constantKind ci
      let payload ←
        if kind == "theorem" then
          let goal ← renderConstantType ci
          pure <| Json.mkObj [
            ("name", Json.str lookup),
            ("constant_kind", Json.str kind),
            ("goal_v1", Json.str goal)
          ]
        else
          pure <| Json.mkObj [
            ("name", Json.str lookup),
            ("constant_kind", Json.str kind),
            ("unsupported_kind", Json.bool true)
          ]
      IO.println s!"LFGOALV1JSON {payload.compress}"

end LeanFaith.GoalV1
