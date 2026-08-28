/-
LeanFaith typed Meta transformation engine -- first Track D-1a slice.

This file implements the first two P-SCHEMA families from
`TRANSFORM_CATALOG_V2.md`:

* P24 swaps adjacent, independent proof binders;
* P23 packs `A -> B -> C` into `A /\ B -> C`, and unpacks the reverse form.

The driver follows `ExprJson.lean`: a string literal names a declaration, the
declaration is loaded directly from the environment (including private names),
and compact JSON is printed to stdout.  Use

    lfTransform "declaration.name"

after importing the module(s) that contain the declaration.  Each applicable
site produces one JSON line.

This is deliberately a small first slice.  The full D-1a certificate (Expr
hashes, binder-depth maps, and an independent reconstruction/audit path) is not
yet represented by the output contract requested here.
-/
import Lean

/- As in `ExprJson.lean`, keep helper-only opens inside the namespace.  The
   production representation path may inject the import-stripped body into a
   larger command, and global opens must not affect source name resolution. -/
namespace LeanFaith.Meta.TransformEngineHelper

open Lean Elab Command Meta

/-- One typed candidate before pretty-printing and re-elaboration. -/
structure TransformCandidate where
  family : String
  sitePath : String
  candidate : Expr

/-- Use the same stable pretty-printing choices as `ExprJson.lean`. -/
def lfTransformPp (type : Expr) : MetaM String := do
  let options := Options.empty
    |>.setBool `pp.all false
    |>.setBool `pp.fullNames true
    |>.setBool `pp.proofs false
    |>.setBool `pp.proofs.withType false
    |>.setBool `pp.mvars false
    |>.setBool `pp.explicit false
    |>.setBool `pp.universes false
  withOptions (fun _ => options) do
    return (← ppExpr type).pretty

/-- Canonicalize source universe parameter names exactly as `ExprJson.lean`
    does, so the emitted proposition text has deterministic level names. -/
def lfTransformCanonicalType (ci : ConstantInfo) : Expr :=
  let levels := (List.range ci.levelParams.length).map fun i =>
    Level.param (Name.mkSimple s!"u_{i}")
  ci.type.instantiateLevelParams ci.levelParams levels

/-- Universe names placed in the term elaborator while checking emitted text. -/
def lfTransformLevelNames (ci : ConstantInfo) : List Name :=
  (List.range ci.levelParams.length).map fun i => Name.mkSimple s!"u_{i}"

/-- The domain of a telescope free variable.  Going through `inferType` is
    intentional: D-1a sites are selected from Lean's typed local context, not
    from the printed or untyped expression-tree representation. -/
def lfFVarDomain (x : Expr) : MetaM Expr := do
  if !x.isFVar then
    throwError "internal TransformEngine error: telescope entry is not an fvar"
  inferType x

/-- `true` when neither proof-binder domain mentions the other binder.  The
    first check is normally guaranteed by telescope order, but keeping both
    sides explicit records and enforces P24's symmetric precondition. -/
def lfDomainsIndependent (a b aType bType : Expr) : Bool :=
  !aType.containsFVar b.fvarId! && !bType.containsFVar a.fvarId!

/-- Rebuild the suffix strictly after telescope position `start`.  Dependencies
    on earlier free variables remain visible, while later free variables are
    correctly abstracted as de Bruijn indices. -/
def lfTelescopeTail (xs : Array Expr) (body : Expr) (start : Nat) : MetaM Expr :=
  mkForallFVars (xs.extract start xs.size) body

/-- Enumerate P24: swap every adjacent pair of independent Prop binders.

    `forallTelescope` opens the declaration into free variables in a real local
    context.  Reordering those variables and passing them to `mkForallFVars`
    delegates all abstraction/lifting to Lean's binder implementation; no raw
    de Bruijn arithmetic is guessed in this engine. -/
def lfEnumerateP24 (type : Expr) : MetaM (Array TransformCandidate) :=
  forallTelescope type fun xs body => do
    let mut candidates := #[]
    if xs.size < 2 then
      return candidates
    for i in [0 : xs.size - 1] do
      let a := xs[i]!
      let b := xs[i + 1]!
      let aType ← lfFVarDomain a
      let bType ← lfFVarDomain b
      if (← isProp aType) && (← isProp bType) &&
          lfDomainsIndependent a b aType bType then
        let swapped := (xs.set! i b).set! (i + 1) a
        let candidate ← mkForallFVars swapped body
        -- A closed theorem candidate must itself still inhabit Prop.
        if ← isProp candidate then
          candidates := candidates.push {
            family := "P24"
            sitePath := s!"root.forall[{i},{i + 1}]/swap"
            candidate
          }
    return candidates

/-- Recognize an exact elaborated `And A B` application.  `whnf` permits only
    Lean-certified reduction on the domain before matching; the result still
    has to expose the `And` constant with exactly two arguments. -/
def lfAndArgs? (type : Expr) : MetaM (Option (Expr × Expr)) := do
  let type ← whnf type
  match type with
  | .app (.app (.const n _) a) b =>
    if n == ``And then return some (a, b) else return none
  | _ => return none

/-- P23 packing direction at adjacent positions `i` and `i+1`.

    Only ordinary explicit binders are admitted: this is the schema
    `A -> B -> C`, rather than a change to implicit argument behavior.  The
    reconstructed tail includes all later binders, so its free-variable test
    rejects dependency in both later binder domains and the final result. -/
def lfPackP23At?
    (xs : Array Expr) (body : Expr) (i : Nat) : MetaM (Option TransformCandidate) := do
  let a := xs[i]!
  let b := xs[i + 1]!
  let aDecl ← a.fvarId!.getDecl
  let bDecl ← b.fvarId!.getDecl
  if aDecl.binderInfo != .default || bDecl.binderInfo != .default then
    return none
  let aType ← lfFVarDomain a
  let bType ← lfFVarDomain b
  if !(← isProp aType) || !(← isProp bType) ||
      !lfDomainsIndependent a b aType bType then
    return none
  let tail ← lfTelescopeTail xs body (i + 2)
  if tail.containsFVar a.fvarId! || tail.containsFVar b.fvarId! then
    return none
  let andType := mkApp2 (mkConst ``And) aType bType
  let packedTail := mkForall Name.anonymous BinderInfo.default andType tail
  let candidate ← mkForallFVars (xs.extract 0 i) packedTail
  if !(← isProp candidate) then
    return none
  return some {
    family := "P23"
    sitePath := s!"root.forall[{i},{i + 1}]/curry"
    candidate
  }

/-- P23 unpacking direction at telescope position `i`: replace one unused
    proof binder of type `A /\ B` with adjacent proof binders `A` and `B`. -/
def lfUnpackP23At?
    (xs : Array Expr) (body : Expr) (i : Nat) : MetaM (Option TransformCandidate) := do
  let h := xs[i]!
  let hDecl ← h.fvarId!.getDecl
  if hDecl.binderInfo != .default then
    return none
  let hType ← lfFVarDomain h
  let some (aType, bType) ← lfAndArgs? hType | return none
  if !(← isProp aType) || !(← isProp bType) then
    return none
  let tail ← lfTelescopeTail xs body (i + 1)
  if tail.containsFVar h.fvarId! then
    return none
  let unpackedTail :=
    mkForall Name.anonymous BinderInfo.default aType <|
      mkForall Name.anonymous BinderInfo.default bType tail
  let candidate ← mkForallFVars (xs.extract 0 i) unpackedTail
  if !(← isProp candidate) then
    return none
  return some {
    family := "P23"
    sitePath := s!"root.forall[{i}]/uncurry"
    candidate
  }

/-- Enumerate both directions of P23 over the declaration's top-level typed
    telescope. -/
def lfEnumerateP23 (type : Expr) : MetaM (Array TransformCandidate) :=
  forallTelescope type fun xs body => do
    let mut candidates := #[]
    if xs.size >= 2 then
      for i in [0 : xs.size - 1] do
        if let some candidate ← lfPackP23At? xs body i then
          candidates := candidates.push candidate
    for i in [0 : xs.size] do
      if let some candidate ← lfUnpackP23At? xs body i then
        candidates := candidates.push candidate
    return candidates

/-- Reparse and re-elaborate the exact emitted candidate text, then check that
    the resulting term is closed and is a proposition.  This is intentionally
    not a defeq check: P23/P24 generally change the theorem type syntactically
    and are justified by their constructive schema instead. -/
def lfCandidateElaborates
    (candidatePretty : String) (levelNames : List Name) : TermElabM Bool := do
  let stx ←
    match Parser.runParserCategory (← getEnv) `term candidatePretty with
    | .ok stx => pure stx
    | .error _ => return false
  try
    Term.withLevelNames levelNames do
      let candidate ← Term.elabTerm stx none
      Term.synthesizeSyntheticMVarsNoPostponing
      let candidate ← instantiateMVars candidate
      if candidate.hasMVar then
        return false
      check candidate
      isProp candidate
  catch _ =>
    return false

/-- Print one compact JSON object on one line, matching the output field
    contract of the first D-1a working slice. -/
def lfPrintTransformCandidate
    (sourcePretty : String) (levelNames : List Name)
    (candidate : TransformCandidate) : TermElabM Unit := do
  let candidatePretty ← lfTransformPp candidate.candidate
  let candidateElaborates ← lfCandidateElaborates candidatePretty levelNames
  let obj := Json.mkObj [
    ("family", Json.str candidate.family),
    ("sitePath", Json.str candidate.sitePath),
    ("sourcePretty", Json.str sourcePretty),
    ("candidatePretty", Json.str candidatePretty),
    ("evidenceClass", Json.str "P-SCHEMA"),
    ("axioms", Json.str "constructive"),
    ("candidateElaborates", Json.bool candidateElaborates)
  ]
  IO.println obj.compress

/-- `lfTransform "Namespace.declaration"` emits one compact JSON line for each
    applicable P24/P23 site in the named declaration type. -/
elab "lfTransform " s:str : command => do
  let declarationName := s.getString
  let name := declarationName.toName
  liftTermElabM do
    match (← getEnv).find? name with
    | none =>
      let obj := Json.mkObj [
        ("declaration", Json.str declarationName),
        ("notfound", Json.bool true)
      ]
      IO.println obj.compress
    | some ci =>
      let sourceType := lfTransformCanonicalType ci
      if !(← isProp sourceType) then
        return
      let sourcePretty ← lfTransformPp sourceType
      let p24 ← lfEnumerateP24 sourceType
      let p23 ← lfEnumerateP23 sourceType
      let levelNames := lfTransformLevelNames ci
      for candidate in p24 ++ p23 do
        lfPrintTransformCandidate sourcePretty levelNames candidate

end LeanFaith.Meta.TransformEngineHelper
