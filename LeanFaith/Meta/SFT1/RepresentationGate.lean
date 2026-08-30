/-
SFT1-owned helpers for the bounded six-real-goal REPR integration gate.

This file is injected, without its import command, as one hash-bound command
preamble.  The live action only retrieves or elaborates the reference Expr,
constructs a binder-name-only candidate, and invokes the frozen REPR emitter.
It does not declare an endpoint, construct a proof, or implement a renderer.
-/
import Lean

namespace LeanFaith.SFT1.RepresentationGate

open Lean Elab Meta

/-- Fail closed unless `e` is a well-typed closed proposition. -/
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
    throwError m!"{origin}: proof placeholder in proposition Expr"
  check e
  unless ← isProp e do
    throwError m!"{origin}: expected a proposition"
  return e

/-- Obtain the canonical type of an imported theorem without creating a new
    declaration.  Universe normalization remains solely REPR's job. -/
def importedTheoremType (declarationName : Name) : MetaM Expr := do
  let some ci := (← getEnv).find? declarationName
    | throwError m!"SFT1 six-goal reference not found: {declarationName}"
  match ci with
  | .thmInfo _ => checkedClosedProp "imported theorem type" ci.type
  | _ => throwError m!"SFT1 six-goal reference is not a theorem: {declarationName}"

private partial def outerBinderNames : Expr → Array Name
  | .forallE name _ body _ => #[name] ++ outerBinderNames body
  | _ => #[]

private def visibleBinderNames (e : Expr) : Array String :=
  (outerBinderNames e).map fun name => name.eraseMacroScopes.toString

private partial def freshDisplayedName
    (base : String) (used : Array String) (index : Nat := 0) : Name :=
  let base := if base.isEmpty then "x" else base
  let text := if index == 0 then base else s!"{base}_{index}"
  let candidate := Name.mkSimple text
  if used.contains candidate.toString then
    freshDisplayedName base used (index + 1)
  else
    candidate

private partial def canonicalizeBinderMetadata (used : Array String) : Expr → Expr
  | .forallE name domain body binderInfo =>
      let domain := canonicalizeBinderMetadata used domain
      if name.isAnonymous then
        .forallE name domain (canonicalizeBinderMetadata used body) binderInfo
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .forallE name domain (canonicalizeBinderMetadata (used.push name.toString) body) binderInfo
  | .lam name domain body binderInfo =>
      let domain := canonicalizeBinderMetadata used domain
      if name.isAnonymous then
        .lam name domain (canonicalizeBinderMetadata used body) binderInfo
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .lam name domain (canonicalizeBinderMetadata (used.push name.toString) body) binderInfo
  | .letE name type value body nondep =>
      let type := canonicalizeBinderMetadata used type
      let value := canonicalizeBinderMetadata used value
      if name.isAnonymous then
        .letE name type value (canonicalizeBinderMetadata used body) nondep
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .letE name type value (canonicalizeBinderMetadata (used.push name.toString) body) nondep
  | .app fn arg =>
      .app (canonicalizeBinderMetadata used fn) (canonicalizeBinderMetadata used arg)
  | .proj typeName index base =>
      .proj typeName index (canonicalizeBinderMetadata used base)
  | .mdata data body => .mdata data (canonicalizeBinderMetadata used body)
  | e => e

/-- Elaborate an extracted source signature once inside the fixed preamble,
    then replace only compiler macro-scope binder metadata with deterministic
    source-visible `Name.mkSimple` names. The live `run_meta` action calls this
    helper; it never reparses candidate or rendered text. -/
def elaborateReferenceProp (stx : Syntax) : MetaM Expr := do
  let e ← Term.TermElabM.run' do
    let e ← Term.elabTerm stx (some (mkSort .zero))
    Term.synthesizeSyntheticMVarsNoPostponing
    instantiateMVars e
  checkedClosedProp "elaborated source signature" (canonicalizeBinderMetadata #[] e)

private def freshAlphaName (used : Array String) : Name :=
  freshDisplayedName "x" used

private partial def renameFirstExplicitBinder (newName : Name) : Expr → Option Expr
  | .forallE name domain body binderInfo =>
      if binderInfo == .default && !name.isAnonymous then
        some (.forallE newName domain body binderInfo)
      else
        match renameFirstExplicitBinder newName body with
        | some renamedBody => some (.forallE name domain renamedBody binderInfo)
        | none => none
  | _ => none

/-- A narrow P01-shaped gate candidate: change exactly one nonanonymous
    explicit binder name to a deterministic neutral, collision-free
    `Name.mkSimple` name. Source-syntax macro scopes on the selected binder are
    name metadata and may be replaced; BinderInfo, domains, body, and every
    bound-variable index remain byte-for-byte structural peers. -/
def alphaRenameGateCandidate (source : Expr) : MetaM Expr := do
  let source ← checkedClosedProp "alpha source" source
  let used := visibleBinderNames source
  let newName := freshAlphaName used
  if newName.isAnonymous || newName.hasMacroScopes || used.contains newName.toString then
    throwError "SFT1 alpha allocator returned a non-hygienic name"
  let some candidate := renameFirstExplicitBinder newName source
    | throwError "SFT1 six-goal alpha candidate has no eligible explicit binder"
  let candidate ← checkedClosedProp "alpha candidate" candidate
  unless (← withoutModifyingMCtx <| isDefEq source candidate) do
    throwError "SFT1 six-goal alpha candidate is not definitionally equal to its reference"
  return candidate

/-- P23's future pair-binder allocator is frozen here only as a hygiene
    regression.  This gate does not implement or apply the P23 transform. -/
private partial def p23FreshBinderName (used : Array String) (index : Nat := 0) : Name :=
  let text := if index == 0 then "h" else s!"h_{index}"
  let candidate := Name.mkSimple text
  if used.contains candidate.toString then
    p23FreshBinderName used (index + 1)
  else
    candidate

/-- Require P23's deterministic `Name.mkSimple` allocation to avoid every
    visible outer-telescope name after macro scopes are erased. -/
def assertP23BinderAllocatorHygiene (source : Expr) : MetaM Unit := do
  let source ← checkedClosedProp "P23 allocator source" source
  let used := visibleBinderNames source
  let candidate := p23FreshBinderName used
  if candidate.isAnonymous || candidate.hasMacroScopes then
    throwError "P23 allocator produced an unsupported binder name"
  if used.contains candidate.eraseMacroScopes.toString then
    throwError "P23 allocator collided with an existing binder name"

end LeanFaith.SFT1.RepresentationGate
