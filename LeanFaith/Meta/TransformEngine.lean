/-
LeanFaith typed Meta transformation engine -- D-1a, slice 2.

Implemented positive families:

* P20 unfolds exactly one safe, transparent definition application.  The
  certificate retains the exact constant/universe/argument application as the
  inverse fold witness; no environment-wide fold search is performed.
* P21 introduces or eliminates exactly one syntactic beta/zeta redex.
* P23 packs/unpacks adjacent independent proof binders.
* P24 swaps adjacent independent proof binders.

Every writable expression path is represented by `Lean.SubExpr.Pos`.  Raw
syntax is used only to enumerate paths.  Inspection uses `Meta.viewSubexpr`,
and replacement uses `Meta.replaceSubexpr`, so binder bodies are opened in a
real local context and reabstracted by Lean itself.

Commands:

    lfTransform "declaration.name"
    lfTransformBatch "/absolute/path/to/newline-separated-names"
    lfAuditTransform "declaration.name" "P21" "zetaIntroduce" "/1" "sha256"

Output is deterministic line-delimited JSON.  Candidate lines are followed by
one terminal line per declaration.  Batch input is duplicate-rejected before
any declaration is processed, and a failure for one declaration does not
prevent later declarations from being processed.
-/
import Lean
import Lean.Meta.Tactic.Delta

namespace LeanFaith.Meta.TransformEngineHelper

open Lean Elab Command Meta

/-- A local transformation while its subexpression context is active. -/
structure LocalCandidate where
  family : String
  operation : String
  candidate : Expr
  evidenceClass : String
  evidenceFields : List (String × Json)
  witnessFields : List (String × Json)

/-- Context-free metadata retained after `viewSubexpr` closes its local context. -/
structure LocalSpec where
  family : String
  operation : String
  binderDepth : Nat
  sourceSite : String
  candidateSite : String
  evidenceClass : String
  evidenceFields : List (String × Json)
  witnessFields : List (String × Json)

/-- A reconstructed whole-type candidate. -/
structure WholeCandidate extends LocalSpec where
  sitePath : String
  candidate : Expr

/-- A fully checked, hash-addressed JSON emission. -/
structure CandidateEmission where
  key : String
  candidateTypeHash : String
  json : Json

/-- Stable pretty-printer choices shared with `ExprJson.lean`. -/
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

/-- Canonical universe parameter names, as in `ExprJson.lean`. -/
def lfTransformCanonicalType (ci : ConstantInfo) : Expr :=
  let levels := (List.range ci.levelParams.length).map fun i =>
    Level.param (Name.mkSimple s!"u_{i}")
  ci.type.instantiateLevelParams ci.levelParams levels

/-- Universe names made available while re-elaborating emitted text. -/
def lfTransformLevelNames (ci : ConstantInfo) : List Name :=
  (List.range ci.levelParams.length).map fun i => Name.mkSimple s!"u_{i}"

def lfBinderInfoTag : BinderInfo → String
  | .default => "default"
  | .implicit => "implicit"
  | .strictImplicit => "strictImplicit"
  | .instImplicit => "instImplicit"

def lfReducibilityTag : ReducibilityStatus → String
  | .reducible => "reducible"
  | .implicitReducible => "implicitReducible"
  | .semireducible => "semireducible"
  | .irreducible => "irreducible"

/-- Stable operation bucket; `operation` itself may additionally carry a
    definition name or telescope index needed for reconstruction. -/
def lfOperationKind (operation : String) : String :=
  if operation.startsWith "unfold:" then "unfold"
  else if operation.startsWith "curry:" then "curry"
  else if operation.startsWith "uncurry:" then "uncurry"
  else if operation.startsWith "swapAdjacent:" then "swapAdjacent"
  else operation

/-- Exact SHA-256 of the UTF-8 bytes of `s`.  No newline is appended. -/
def lfSha256 (s : String) : IO String := do
  let out ← IO.Process.output
    { cmd := "/usr/bin/sha256sum", args := #["-"] }
    (input? := some s)
  if out.exitCode != 0 then
    throw <| IO.userError s!"sha256sum failed ({out.exitCode}): {out.stderr}"
  if out.stdout.length < 64 then
    throw <| IO.userError "sha256sum returned a truncated digest"
  let digest := (out.stdout.take 64).toString
  let validHex := digest.toList.all fun c => "0123456789abcdef".contains c
  if digest.length != 64 || !validHex || out.stdout != digest ++ "  -\n" then
    throw <| IO.userError s!"sha256sum returned malformed output: {out.stdout}"
  if !out.stderr.isEmpty then
    throw <| IO.userError s!"sha256sum wrote to stderr: {out.stderr}"
  return digest

/-- Enumerate only term-child positions accepted by `Meta.replaceSubexpr`.
    Coordinate 3 (an inferred type) is deliberately excluded.  Metadata is
    position-transparent, matching Lean's expression lens. -/
partial def lfTermPositions
    (e : Expr) (p : SubExpr.Pos := .root) : Array SubExpr.Pos :=
  match e with
  | .mdata _ a => lfTermPositions a p
  | .app f a =>
      #[p] ++ lfTermPositions f p.pushAppFn ++ lfTermPositions a p.pushAppArg
  | .lam _ ty body _ | .forallE _ ty body _ =>
      #[p] ++ lfTermPositions ty p.pushBindingDomain ++
        lfTermPositions body p.pushBindingBody
  | .letE _ ty value body _ =>
      #[p] ++ lfTermPositions ty p.pushLetVarType ++
        lfTermPositions value p.pushLetValue ++
        lfTermPositions body p.pushLetBody
  | .proj _ _ struct => #[p] ++ lfTermPositions struct p.pushProj
  | _ => #[p]

/-- The domain of a telescope free variable. -/
def lfFVarDomain (x : Expr) : MetaM Expr := do
  if !x.isFVar then
    throwError "internal TransformEngine error: telescope entry is not an fvar"
  inferType x

/-- Symmetric dependency screen for adjacent proof binders. -/
def lfDomainsIndependent (a b aType bType : Expr) : Bool :=
  !aType.containsFVar b.fvarId! && !bType.containsFVar a.fvarId!

def lfTelescopeTail (xs : Array Expr) (body : Expr) (start : Nat) : MetaM Expr :=
  mkForallFVars (xs.extract start xs.size) body

/-- P24 over every adjacent pair in the local proposition telescope. -/
def lfLocalP24 (type : Expr) : MetaM (Array LocalCandidate) :=
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
        if ← isProp candidate then
          candidates := candidates.push {
            family := "P24"
            operation := s!"swapAdjacent:{i}"
            candidate
            evidenceClass := "P-SCHEMA"
            evidenceFields := [
              ("relation", Json.str "constructive"),
              ("bothDomainsProp", Json.bool true),
              ("domainsIndependent", Json.bool true),
              ("contextReconstructed", Json.bool true)
            ]
            witnessFields := [
              ("leftBinderIndex", toJson i),
              ("rightBinderIndex", toJson (i + 1)),
              ("leftBinderInfo", Json.str (lfBinderInfoTag (← a.fvarId!.getDecl).binderInfo)),
              ("rightBinderInfo", Json.str (lfBinderInfoTag (← b.fvarId!.getDecl).binderInfo)),
              ("abstractionAuthority", Json.str "Lean.Meta.mkForallFVars")
            ]
          }
    return candidates

/-- P23 packing at adjacent telescope positions. -/
def lfPackP23At?
    (xs : Array Expr) (body : Expr) (i : Nat) : MetaM (Option LocalCandidate) := do
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
    operation := s!"curry:{i}"
    candidate
    evidenceClass := "P-SCHEMA"
    evidenceFields := [
      ("relation", Json.str "constructive"),
      ("bothDomainsProp", Json.bool true),
      ("domainsIndependent", Json.bool true),
      ("tailIndependent", Json.bool true),
      ("contextReconstructed", Json.bool true)
    ]
    witnessFields := [
      ("firstBinderIndex", toJson i),
      ("secondBinderIndex", toJson (i + 1)),
      ("direction", Json.str "arrowsToAnd"),
      ("abstractionAuthority", Json.str "Lean.Meta.mkForallFVars")
    ]
  }

/-- P23 unpacking at a telescope position. -/
def lfUnpackP23At?
    (xs : Array Expr) (body : Expr) (i : Nat) : MetaM (Option LocalCandidate) := do
  let h := xs[i]!
  let hDecl ← h.fvarId!.getDecl
  if hDecl.binderInfo != .default then
    return none
  let hType ← lfFVarDomain h
  let hType ← whnf hType
  let some (aType, bType) :=
      match hType with
      | .app (.app (.const n _) a) b => if n == ``And then some (a, b) else none
      | _ => none
    | return none
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
    operation := s!"uncurry:{i}"
    candidate
    evidenceClass := "P-SCHEMA"
    evidenceFields := [
      ("relation", Json.str "constructive"),
      ("andArgumentsProp", Json.bool true),
      ("tailIndependent", Json.bool true),
      ("contextReconstructed", Json.bool true)
    ]
    witnessFields := [
      ("binderIndex", toJson i),
      ("direction", Json.str "andToArrows"),
      ("abstractionAuthority", Json.str "Lean.Meta.mkForallFVars")
    ]
  }

def lfLocalP23 (type : Expr) : MetaM (Array LocalCandidate) :=
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

/-- Binder-info sequence consumed by an exact definition application. -/
partial def lfAppliedBinderInfos
    (type : Expr) (args : Array Expr) (i : Nat := 0)
    (acc : Array String := #[]) : Array String :=
  if h : i < args.size then
    match type with
    | .forallE _ _ body bi =>
        lfAppliedBinderInfos (body.instantiate1 args[i]) args (i + 1)
          (acc.push (lfBinderInfoTag bi))
    | _ => lfAppliedBinderInfos type args (i + 1) (acc.push "overapplied")
  else
    acc

/-- Exact one-delta P20 expansion.  Only kernel definitions that are safe,
    public-facing, and transparent at ordinary reducibility are admitted.

    P20 deliberately admits typed data subterms inside a proposition, not only
    subterms that are themselves propositions.  This broader choice is sound
    because every emitted P20 whole type must pass kernel `isDefEq`; it covers
    common definitional wrappers inside equality and membership arguments. -/
def lfLocalP20 (e : Expr) : MetaM (Array LocalCandidate) := do
  let .const declName levels := e.getAppFn | return #[]
  let some (.defnInfo d) := (← getEnv).find? declName | return #[]
  if declName.isInternalDetail || d.safety != .safe ||
      d.levelParams.length != levels.length then
    return #[]
  let reducibility ← getReducibilityStatus declName
  if reducibility == .irreducible || reducibility == .implicitReducible then
    return #[]
  let some unfolded ← Meta.delta? e (fun n => n == declName) (allowOpaque := false)
    | return #[]
  if unfolded == e then
    return #[]
  let args := e.getAppArgs
  let argText ← args.mapM lfTransformPp
  let instantiatedType := d.type.instantiateLevelParams d.levelParams levels
  let binderInfos := lfAppliedBinderInfos instantiatedType args
  return #[{
    family := "P20"
    operation := s!"unfold:{declName}"
    candidate := unfolded
    evidenceClass := "P-DEF"
    evidenceFields := [
      ("relation", Json.str "definitionalEquality"),
      ("deltaSteps", toJson (1 : Nat)),
      ("safeDefinition", Json.bool true),
      ("transparentDefinition", Json.bool true),
      ("typedSubterm", Json.bool true),
      ("inverseFoldCertified", Json.bool true),
      ("wholeTypeDefEqRequired", Json.bool true),
      ("contextReconstructed", Json.bool true)
    ]
    witnessFields := [
      ("constant", Json.str declName.toString),
      ("universeArguments", Json.arr <| levels.toArray.map fun u => Json.str (toString u)),
      ("arguments", Json.arr <| argText.map Json.str),
      ("argumentBinderInfo", Json.arr <| binderInfos.map Json.str),
      ("argumentCount", toJson args.size),
      ("reducibility", Json.str (lfReducibilityTag reducibility)),
      ("definitionSafety", Json.str "safe"),
      ("inverseOperation", Json.str "fold"),
      ("inverseUsesPreservedApplication", Json.bool true),
      ("unfoldResidualStructuralMatch", Json.bool true),
      ("foldSearch", Json.bool false)
    ]
  }]

/-- One syntactic beta contraction; no whnf/head-beta loop. -/
def lfBeta1? : Expr → Option Expr
  | .app (.lam _ _ body _) arg => some (body.instantiate1 arg)
  | _ => none

/-- One syntactic zeta contraction; no recursive reduction. -/
def lfZeta1? : Expr → Option Expr
  | .letE _ _ value body _ => some (body.instantiate1 value)
  | _ => none

def lfIntroduceBeta (e eType : Expr) : Expr :=
  mkApp (.lam `p eType (.bvar 0) .default) e

def lfIntroduceZeta (e eType : Expr) : Expr :=
  .letE `p eType e (.bvar 0) false

/-- Exact one-redex P21 introduction/elimination.  Introduction is restricted
    to proposition-valued sites to avoid flooding arbitrary data subterms. -/
def lfLocalP21 (e : Expr) (siteIsProp : Bool) : MetaM (Array LocalCandidate) := do
  let mut candidates := #[]
  if siteIsProp then
    let eType ← inferType e
    let beta := lfIntroduceBeta e eType
    let zeta := lfIntroduceZeta e eType
    candidates := candidates.push {
      family := "P21"
      operation := "betaIntroduce"
      candidate := beta
      evidenceClass := "P-DEF"
      evidenceFields := [
        ("relation", Json.str "definitionalEquality"),
        ("redexKind", Json.str "beta"),
        ("redexCount", toJson (1 : Nat)),
        ("wholeTypeDefEqRequired", Json.bool true),
        ("contextReconstructed", Json.bool true)
      ]
      witnessFields := [
        ("direction", Json.str "introduce"),
        ("residualRule", Json.str "instantiate1"),
        ("captureFreeByKernelSubstitution", Json.bool true)
      ]
    }
    candidates := candidates.push {
      family := "P21"
      operation := "zetaIntroduce"
      candidate := zeta
      evidenceClass := "P-DEF"
      evidenceFields := [
        ("relation", Json.str "definitionalEquality"),
        ("redexKind", Json.str "zeta"),
        ("redexCount", toJson (1 : Nat)),
        ("wholeTypeDefEqRequired", Json.bool true),
        ("contextReconstructed", Json.bool true)
      ]
      witnessFields := [
        ("direction", Json.str "introduce"),
        ("residualRule", Json.str "instantiate1"),
        ("captureFreeByKernelSubstitution", Json.bool true)
      ]
    }
  if let some reduced := lfBeta1? e then
    candidates := candidates.push {
      family := "P21"
      operation := "betaEliminate"
      candidate := reduced
      evidenceClass := "P-DEF"
      evidenceFields := [
        ("relation", Json.str "definitionalEquality"),
        ("redexKind", Json.str "beta"),
        ("redexCount", toJson (1 : Nat)),
        ("wholeTypeDefEqRequired", Json.bool true),
        ("contextReconstructed", Json.bool true)
      ]
      witnessFields := [
        ("direction", Json.str "eliminate"),
        ("residualRule", Json.str "instantiate1"),
        ("captureFreeByKernelSubstitution", Json.bool true)
      ]
    }
  if let some reduced := lfZeta1? e then
    candidates := candidates.push {
      family := "P21"
      operation := "zetaEliminate"
      candidate := reduced
      evidenceClass := "P-DEF"
      evidenceFields := [
        ("relation", Json.str "definitionalEquality"),
        ("redexKind", Json.str "zeta"),
        ("redexCount", toJson (1 : Nat)),
        ("wholeTypeDefEqRequired", Json.bool true),
        ("contextReconstructed", Json.bool true)
      ]
      witnessFields := [
        ("direction", Json.str "eliminate"),
        ("residualRule", Json.str "instantiate1"),
        ("captureFreeByKernelSubstitution", Json.bool true)
      ]
    }
  return candidates

/-- All local operations on a metadata-free head, in a fixed family order. -/
def lfLocalCandidatesCore (e : Expr) : MetaM (Array LocalCandidate) := do
  if e.hasMVar then
    return #[]
  let siteIsProp ←
    try isProp e
    catch _ => pure false
  let mut candidates := #[]
  if siteIsProp then
    candidates := candidates ++ (← lfLocalP23 e)
    candidates := candidates ++ (← lfLocalP24 e)
  candidates := candidates ++ (← lfLocalP20 e)
  candidates := candidates ++ (← lfLocalP21 e siteIsProp)
  return candidates

/-- Lean's expression lens treats metadata as path-transparent but retains an
    outer metadata node in the callback expression.  Peel such nodes for
    matching and reapply every layer to the replacement, preserving metadata
    exactly instead of silently dropping it. -/
partial def lfLocalCandidates (e : Expr) : MetaM (Array LocalCandidate) := do
  match e with
  | .mdata md inner =>
      let candidates ← lfLocalCandidates inner
      return candidates.map fun candidate =>
        { candidate with candidate := .mdata md candidate.candidate }
  | _ =>
      lfLocalCandidatesCore e

/-- Erase local fvars by retaining exact pretty text while the view context is
    active. -/
def lfLocalSpecs (e : Expr) (binderDepth : Nat) : MetaM (Array LocalSpec) := do
  let sourceSite ← lfTransformPp e
  let candidates ← lfLocalCandidates e
  candidates.mapM fun candidate => do
    let candidateSite ← lfTransformPp candidate.candidate
    return {
      family := candidate.family
      operation := candidate.operation
      binderDepth
      sourceSite
      candidateSite
      evidenceClass := candidate.evidenceClass
      evidenceFields := candidate.evidenceFields
      witnessFields := candidate.witnessFields
    }

/-- Reconstruct one local operation from family+operation.  This is called in
    the fresh local context created by `replaceSubexpr`. -/
def lfReconstructLocal (family operation : String) (e : Expr) : MetaM Expr := do
  let candidates ← lfLocalCandidates e
  for candidate in candidates do
    if candidate.family == family && candidate.operation == operation then
      return candidate.candidate
  throwError "operation is no longer applicable at the certified site"

/-- Discover specs with `viewSubexpr`, then reconstruct whole types with
    `replaceSubexpr`. -/
def lfWholeCandidates (sourceType : Expr) : MetaM (Array WholeCandidate) := do
  let mut result := #[]
  for pos in lfTermPositions sourceType do
    let specs ← Meta.viewSubexpr
      (fun fvars sub => lfLocalSpecs sub fvars.size) pos sourceType
    for spec in specs do
      let candidate ← Meta.replaceSubexpr
        (lfReconstructLocal spec.family spec.operation) pos sourceType
      result := result.push {
        toLocalSpec := spec
        sitePath := pos.toString
        candidate
      }
  return result

/-- Reparse the exact pretty text and require it to elaborate back to the
    expected closed proposition.  The speculative elaboration is fully
    isolated: neither metavariable assignments nor rejected-error messages may
    escape into the command that is producing the certificate. -/
def lfTextElaboratesAs
    (text : String) (expected : Expr) (levelNames : List Name) : TermElabM Bool := do
  let stx ←
    match Parser.runParserCategory (← getEnv) `term text with
    | .ok stx => pure stx
    | .error _ => return false
  withoutModifyingState do
    let result ← Term.commitIfNoErrors? do
      Term.withLevelNames levelNames do
        let candidate ← Term.elabTerm stx none
        Term.synthesizeSyntheticMVarsNoPostponing
        let candidate ← instantiateMVars candidate
        if candidate.hasMVar then
          return false
        check candidate
        if !(← isProp candidate) then
          return false
        withTransparency .default <| isDefEq candidate expected
    return result.getD false

def lfCheckedProp (e : Expr) : MetaM Bool :=
  try
    check e
    isProp e
  catch _ =>
    return false

def lfWholeDefEq (source candidate : Expr) : MetaM Bool :=
  try
    withTransparency .default <| isDefEq source candidate
  catch _ =>
    return false

/-- Build a rich, hash-addressed candidate record.  Invalid P-DEF candidates
    and candidates that do not survive exact-text re-elaboration fail closed. -/
def lfCandidateEmission?
    (declaration source sourceTypeHash : String) (sourceType : Expr)
    (levelNames : List Name) (candidate : WholeCandidate) : TermElabM (Option CandidateEmission) := do
  if !(← lfCheckedProp candidate.candidate) then
    return none
  let candidatePretty ← lfTransformPp candidate.candidate
  if candidatePretty == source then
    return none
  let candidateElaborates ←
    lfTextElaboratesAs candidatePretty candidate.candidate levelNames
  if !candidateElaborates then
    return none
  let wholeTypeDefEq ← lfWholeDefEq sourceType candidate.candidate
  if candidate.evidenceClass == "P-DEF" && !wholeTypeDefEq then
    return none
  let candidateTypeHash ← lfSha256 candidatePretty
  let sourceSiteHash ←
    if candidate.sourceSite == source then pure sourceTypeHash
    else lfSha256 candidate.sourceSite
  let candidateSiteHash ←
    if candidate.candidateSite == candidatePretty then pure candidateTypeHash
    else lfSha256 candidate.candidateSite
  let nestedSite := candidate.sitePath != "/" || candidate.binderDepth != 0
  let evidence := Json.mkObj candidate.evidenceFields
  let residualFields :=
    if candidate.family == "P20" || candidate.operation == "betaEliminate" ||
        candidate.operation == "zetaEliminate" then
      [("residualHash", Json.str candidateSiteHash)]
    else if candidate.operation == "betaIntroduce" ||
        candidate.operation == "zetaIntroduce" then
      [("residualHash", Json.str sourceSiteHash)]
    else
      []
  let witness := Json.mkObj <| candidate.witnessFields ++ [
    ("sourceSiteHash", Json.str sourceSiteHash),
    ("candidateSiteHash", Json.str candidateSiteHash)
  ] ++ residualFields
  let obj := Json.mkObj [
    ("schemaVersion", toJson (2 : Nat)),
    ("kind", Json.str "candidate"),
    ("recordKind", Json.str "candidate"),
    ("declaration", Json.str declaration),
    ("family", Json.str candidate.family),
    ("operation", Json.str candidate.operation),
    ("operationKind", Json.str (lfOperationKind candidate.operation)),
    ("sitePath", Json.str candidate.sitePath),
    ("binderDepth", toJson candidate.binderDepth),
    ("nestedSite", Json.bool nestedSite),
    ("source", Json.str source),
    ("candidate", Json.str candidatePretty),
    ("sourcePretty", Json.str source),
    ("candidatePretty", Json.str candidatePretty),
    ("sourceSite", Json.str candidate.sourceSite),
    ("candidateSite", Json.str candidate.candidateSite),
    ("sourceTypeHash", Json.str sourceTypeHash),
    ("candidateTypeHash", Json.str candidateTypeHash),
    ("sourceSiteHash", Json.str sourceSiteHash),
    ("candidateSiteHash", Json.str candidateSiteHash),
    ("evidenceClass", Json.str candidate.evidenceClass),
    ("evidence", evidence),
    ("axioms", Json.str (if candidate.evidenceClass == "P-SCHEMA" then "constructive" else "none")),
    ("candidateElaborates", Json.bool candidateElaborates),
    ("wholeTypeDefEq", Json.bool wholeTypeDefEq),
    ("witness", witness),
    ("status", Json.str "ok")
  ]
  let key := candidate.family ++ "\u0000" ++ candidate.operation ++ "\u0000" ++
    candidate.sitePath ++ "\u0000" ++ candidateTypeHash
  return some { key, candidateTypeHash, json := obj }

def lfPrintTerminal
    (declaration status : String) (candidateCount emittedCount duplicateCount rejectedCount : Nat)
    (extra : List (String × Json) := []) : IO Unit := do
  let obj := Json.mkObj <| [
    ("schemaVersion", toJson (2 : Nat)),
    ("kind", Json.str "terminal"),
    ("recordKind", Json.str "status"),
    ("declaration", Json.str declaration),
    ("status", Json.str status),
    ("candidateCount", toJson candidateCount),
    ("emittedCount", toJson emittedCount),
    ("duplicateCount", toJson duplicateCount),
    ("rejectedCount", toJson rejectedCount)
  ] ++ extra
  IO.println obj.compress

/-- Process one declaration.  Returns true exactly for a complete Prop record. -/
def lfProcessDeclaration (declaration : String) : TermElabM Bool := do
  let name := declaration.toName
  match (← getEnv).find? name with
  | none =>
      lfPrintTerminal declaration "notfound" 0 0 0 0
        [("notfound", Json.bool true)]
      return false
  | some ci =>
      let sourceType := lfTransformCanonicalType ci
      if !(← isProp sourceType) then
        let source ← lfTransformPp sourceType
        let sourceTypeHash ← lfSha256 source
        lfPrintTerminal declaration "notProp" 0 0 0 0 [
          ("source", Json.str source),
          ("sourceTypeHash", Json.str sourceTypeHash)
        ]
        return false
      let source ← lfTransformPp sourceType
      let sourceTypeHash ← lfSha256 source
      let levelNames := lfTransformLevelNames ci
      if !(← lfTextElaboratesAs source sourceType levelNames) then
        throwError "source pretty text did not re-elaborate to the declaration type"
      let wholeCandidates ← lfWholeCandidates sourceType
      let mut emissions := #[]
      for candidate in wholeCandidates do
        if let some emission ←
            lfCandidateEmission? declaration source sourceTypeHash sourceType levelNames candidate then
          emissions := emissions.push emission
      let sortedEmissions := emissions.qsort fun a b => a.key < b.key
      let mut seen : Std.HashSet String := {}
      let mut emittedCount := 0
      let mut duplicateCount := 0
      for emission in sortedEmissions do
        if seen.contains emission.candidateTypeHash then
          duplicateCount := duplicateCount + 1
        else
          seen := seen.insert emission.candidateTypeHash
          emittedCount := emittedCount + 1
          IO.println emission.json.compress
      let rejectedCount := wholeCandidates.size - emissions.size
      lfPrintTerminal declaration "complete" sortedEmissions.size emittedCount
        duplicateCount rejectedCount [
          ("source", Json.str source),
          ("sourceTypeHash", Json.str sourceTypeHash),
          ("discoveredCount", toJson wholeCandidates.size),
          ("pathCount", toJson (lfTermPositions sourceType).size)
        ]
      return true

/-- Per-declaration exception boundary used by both drivers. -/
def lfProcessDeclarationIsolated (declaration : String) : TermElabM Bool := do
  try
    lfProcessDeclaration declaration
  catch ex =>
    if ex.isInterrupt then
      throw ex
    let message ← ex.toMessageData.toString
    lfPrintTerminal declaration "error" 0 0 0 0 [
      ("error", Json.str message)
    ]
    return false

/-- Read nonempty trimmed names and reject every duplicate before processing. -/
def lfReadBatchNames (path : String) : IO (Except String (Array String)) := do
  let contents ← IO.FS.readFile path
  let mut names := #[]
  let mut seen : Std.HashSet String := {}
  for raw in contents.splitOn "\n" do
    let name := raw.trimAscii.toString
    if !name.isEmpty then
      if seen.contains name then
        return .error s!"duplicate declaration in names file: {name}"
      seen := seen.insert name
      names := names.push name
  if names.isEmpty then
    return .error "names file contains no declarations"
  return .ok names

/-- Emit candidates and a terminal status for one declaration. -/
elab "lfTransform " s:str : command => do
  liftTermElabM do
    discard <| lfProcessDeclarationIsolated s.getString

/-- Duplicate-safe batch driver with per-name isolation. -/
elab "lfTransformBatch " s:str : command => do
  liftTermElabM do
    let path := s.getString
    let namesResult ← lfReadBatchNames path
    let names ←
      match namesResult with
      | .ok names => pure names
      | .error error =>
          let obj := Json.mkObj [
            ("schemaVersion", toJson (2 : Nat)),
            ("kind", Json.str "batch"),
            ("recordKind", Json.str "batch"),
            ("status", Json.str "error"),
            ("namesFile", Json.str path),
            ("error", Json.str error)
          ]
          IO.println obj.compress
          throwError error
    let mut completedCount := 0
    let mut failedCount := 0
    for declaration in names do
      if ← lfProcessDeclarationIsolated declaration then
        completedCount := completedCount + 1
      else
        failedCount := failedCount + 1
    let obj := Json.mkObj [
      ("schemaVersion", toJson (2 : Nat)),
      ("kind", Json.str "batch"),
      ("recordKind", Json.str "batch"),
      ("status", Json.str (if failedCount == 0 then "complete" else "partial")),
      ("namesFile", Json.str path),
      ("declarationCount", toJson names.size),
      ("completedCount", toJson completedCount),
      ("failedCount", toJson failedCount)
    ]
    IO.println obj.compress

/-- Reconstruct one candidate from declaration/family/operation/path and check
    its exact emitted SHA-256.  This command is intentionally separate from the
    generation/output path; it reopens the certified site with Lean's lens. -/
elab "lfAuditTransform " d:str f:str o:str p:str h:str : command => do
  liftTermElabM do
    let declaration := d.getString
    let family := f.getString
    let operation := o.getString
    let pathText := p.getString
    let expectedHash := h.getString
    let mut verified := false
    let mut reason := "notfound"
    let mut actualHash := ""
    if let some ci := (← getEnv).find? declaration.toName then
      let sourceType := lfTransformCanonicalType ci
      if ← isProp sourceType then
        match SubExpr.Pos.fromString? pathText with
        | .error error => reason := error
        | .ok pos =>
            if (lfTermPositions sourceType).contains pos then
              try
                let candidate ← liftMetaM <| Meta.replaceSubexpr
                  (lfReconstructLocal family operation) pos sourceType
                let candidatePretty ← lfTransformPp candidate
                actualHash ← lfSha256 candidatePretty
                let elaborates ← lfTextElaboratesAs candidatePretty candidate
                  (lfTransformLevelNames ci)
                let defeq ← lfWholeDefEq sourceType candidate
                let needsDefEq := family == "P20" || family == "P21"
                verified := actualHash == expectedHash && elaborates && (!needsDefEq || defeq)
                reason := if verified then "verified" else "hash-or-evidence-mismatch"
              catch ex =>
                if ex.isInterrupt then throw ex
                reason ← ex.toMessageData.toString
            else
              reason := "path-not-present"
      else
        reason := "declaration-not-prop"
    let obj := Json.mkObj [
      ("schemaVersion", toJson (2 : Nat)),
      ("kind", Json.str "audit"),
      ("recordKind", Json.str "audit"),
      ("declaration", Json.str declaration),
      ("family", Json.str family),
      ("operation", Json.str operation),
      ("sitePath", Json.str pathText),
      ("expectedCandidateTypeHash", Json.str expectedHash),
      ("actualCandidateTypeHash", Json.str actualHash),
      ("verified", Json.bool verified),
      ("inverseFoldVerified", Json.bool (verified && family == "P20")),
      ("status", Json.str (if verified then "verified" else "rejected")),
      ("reason", Json.str reason),
      ("auditMode", Json.str "independent-site-reconstruction")
    ]
    IO.println obj.compress

end LeanFaith.Meta.TransformEngineHelper
