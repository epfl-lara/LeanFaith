/-
LeanFaith typed negative Boolean-skeleton pilot.

This additive engine implements only the preregistered N21/N22 root-body
slice.  It opens the outer theorem telescope with Lean.Meta, requires a root
And/Or/Iff over two distinct nontrivial atomic propositions, and emits:

* N21: negate exactly one influencing root atom;
* N22: replace And/Or, or replace Iff by its forward implication.

Every row carries an exact two-atom truth-table separator.  The independent
audit command reconstructs the operation from declaration + operation name
and requires the exact candidate pretty-text hash.  This is structural N-SEP
evidence under the declared schema-inequivalence contract; it is not claimed
to be a concrete countermodel of arbitrary dependencies inside the atoms.
-/
import Lean

namespace LeanFaith.Meta.NegativeSkeletonEngineHelper

open Lean Elab Command Meta

structure SkeletonCandidate where
  family : String
  operation : String
  candidate : Expr
  sourceSkeleton : String
  candidateSkeleton : String
  atomA : Expr
  atomB : Expr
  atomAValue : Bool
  atomBValue : Bool
  sourceValue : Bool
  candidateValue : Bool
  outerBinderCount : Nat

structure CandidateEmission where
  key : String
  candidateTypeHash : String
  json : Json

inductive RootKind where
  | and
  | or
  | iff
  deriving BEq

def lfNegativePp (type : Expr) : MetaM String := do
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

def lfNegativeCanonicalType (ci : ConstantInfo) : Expr :=
  let levels := (List.range ci.levelParams.length).map fun i =>
    Level.param (Name.mkSimple s!"u_{i}")
  ci.type.instantiateLevelParams ci.levelParams levels

def lfNegativeLevelNames (ci : ConstantInfo) : List Name :=
  (List.range ci.levelParams.length).map fun i => Name.mkSimple s!"u_{i}"

def lfNegativeSha256 (s : String) : IO String := do
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

def lfNegativeTextElaboratesAs
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
        check candidate
        if !(← isProp candidate) then
          return false
        if !(← withTransparency .default <| isDefEq candidate expected) then
          return false
        let candidate ← instantiateMVars candidate
        return !candidate.hasMVar
    return result.getD false

def lfRootView? (body : Expr) : Option (RootKind × Expr × Expr) :=
  match body with
  | .app (.app (.const name _) a) b =>
      if name == ``And then some (.and, a, b)
      else if name == ``Or then some (.or, a, b)
      else if name == ``Iff then some (.iff, a, b)
      else none
  | _ => none

def lfIsLogicalHead : Expr → Bool
  | .app (.app (.const name _) _) _ => name == ``And || name == ``Or || name == ``Iff
  | .app (.const name _) _ => name == ``Not
  | .const name _ => name == ``True || name == ``False
  | _ => false

def lfKindName : RootKind → String
  | .and => "and"
  | .or => "or"
  | .iff => "iff"

def lfBinary (kind : RootKind) (a b : Expr) : Expr :=
  match kind with
  | .and => mkApp2 (mkConst ``And) a b
  | .or => mkApp2 (mkConst ``Or) a b
  | .iff => mkApp2 (mkConst ``Iff) a b

def lfNot (a : Expr) : Expr := mkApp (mkConst ``Not) a

def lfImp (a b : Expr) : Expr := mkForall Name.anonymous BinderInfo.default a b

def lfEvalRoot (kind : RootKind) (a b : Bool) : Bool :=
  match kind with
  | .and => a && b
  | .or => a || b
  | .iff => a == b

def lfSkeleton (kind : RootKind) : String :=
  match kind with
  | .and => "A ∧ B"
  | .or => "A ∨ B"
  | .iff => "A ↔ B"

def lfCandidateSkeletons
    (kind : RootKind) (a b : Expr) (outerBinderCount : Nat) : Array SkeletonCandidate :=
  let leftValuation :=
    match kind with
    | .or => (false, false)
    | .and | .iff => (true, true)
  let leftA := leftValuation.1
  let leftB := leftValuation.2
  let leftSource := lfEvalRoot kind leftA leftB
  let leftCandidate := lfEvalRoot kind (!leftA) leftB
  let rightCandidate := lfEvalRoot kind leftA (!leftB)
  let result := #[
    {
      family := "N21"
      operation := s!"negateLeft:{lfKindName kind}"
      candidate := lfBinary kind (lfNot a) b
      sourceSkeleton := lfSkeleton kind
      candidateSkeleton := s!"¬A {match kind with | .and => "∧" | .or => "∨" | .iff => "↔"} B"
      atomA := a
      atomB := b
      atomAValue := leftA
      atomBValue := leftB
      sourceValue := leftSource
      candidateValue := leftCandidate
      outerBinderCount
    },
    {
      family := "N21"
      operation := s!"negateRight:{lfKindName kind}"
      candidate := lfBinary kind a (lfNot b)
      sourceSkeleton := lfSkeleton kind
      candidateSkeleton := s!"A {match kind with | .and => "∧" | .or => "∨" | .iff => "↔"} ¬B"
      atomA := a
      atomB := b
      atomAValue := leftA
      atomBValue := leftB
      sourceValue := leftSource
      candidateValue := rightCandidate
      outerBinderCount
    }
  ]
  match kind with
  | .and =>
      result.push {
        family := "N22"
        operation := "andToOr"
        candidate := lfBinary .or a b
        sourceSkeleton := "A ∧ B"
        candidateSkeleton := "A ∨ B"
        atomA := a
        atomB := b
        atomAValue := true
        atomBValue := false
        sourceValue := false
        candidateValue := true
        outerBinderCount
      }
  | .or =>
      result.push {
        family := "N22"
        operation := "orToAnd"
        candidate := lfBinary .and a b
        sourceSkeleton := "A ∨ B"
        candidateSkeleton := "A ∧ B"
        atomA := a
        atomB := b
        atomAValue := true
        atomBValue := false
        sourceValue := true
        candidateValue := false
        outerBinderCount
      }
  | .iff =>
      result.push {
        family := "N22"
        operation := "iffToImp"
        candidate := lfImp a b
        sourceSkeleton := "A ↔ B"
        candidateSkeleton := "A → B"
        atomA := a
        atomB := b
        atomAValue := false
        atomBValue := true
        sourceValue := false
        candidateValue := true
        outerBinderCount
      }

def lfLocalCandidates (sourceType : Expr) : MetaM (Array SkeletonCandidate) :=
  forallTelescope sourceType fun xs body => do
    let some (kind, a, b) := lfRootView? body | return #[]
    if !(← isProp a) || !(← isProp b) || lfIsLogicalHead a || lfIsLogicalHead b then
      return #[]
    let atomADefEqB ← withoutModifyingState do
      withTransparency .default <| isDefEq a b
    if atomADefEqB then
      return #[]
    let localCandidates := lfCandidateSkeletons kind a b xs.size
    localCandidates.mapM fun candidate => do
      let whole ← mkForallFVars xs candidate.candidate
      return { candidate with candidate := whole }

def lfReconstructCandidate
    (sourceType : Expr) (family operation : String) : MetaM SkeletonCandidate := do
  for candidate in (← lfLocalCandidates sourceType) do
    if candidate.family == family && candidate.operation == operation then
      return candidate
  throwError "negative skeleton operation is no longer applicable"

def lfCandidateEmission?
    (declaration source sourceHash : String) (ci : ConstantInfo)
    (candidate : SkeletonCandidate) : TermElabM (Option CandidateEmission) := do
  if candidate.sourceValue == candidate.candidateValue then
    return none
  let candidatePretty ← lfNegativePp candidate.candidate
  if candidatePretty == source then
    return none
  if !(← lfNegativeTextElaboratesAs candidatePretty candidate.candidate
      (lfNegativeLevelNames ci)) then
    return none
  let candidateHash ← lfNegativeSha256 candidatePretty
  let atomAText ← lfNegativePp candidate.atomA
  let atomBText ← lfNegativePp candidate.atomB
  let atomAHash ← lfNegativeSha256 atomAText
  let atomBHash ← lfNegativeSha256 atomBText
  if atomAHash == atomBHash then
    return none
  let evidence := Json.mkObj [
    ("relation", Json.str "schemaInequivalence"),
    ("exactBooleanSkeleton", Json.bool true),
    ("distinctAtoms", Json.bool true),
    ("rootInfluence", Json.bool true),
    ("separatorVerified", Json.bool true),
    ("contractScope", Json.str "abstract-propositional-schema")
  ]
  let witness := Json.mkObj [
    ("sourceSkeleton", Json.str candidate.sourceSkeleton),
    ("candidateSkeleton", Json.str candidate.candidateSkeleton),
    ("atomAHash", Json.str atomAHash),
    ("atomBHash", Json.str atomBHash),
    ("valuation", Json.mkObj [
      ("A", Json.bool candidate.atomAValue),
      ("B", Json.bool candidate.atomBValue)
    ]),
    ("sourceValue", Json.bool candidate.sourceValue),
    ("candidateValue", Json.bool candidate.candidateValue),
    ("outerBinderCount", toJson candidate.outerBinderCount)
  ]
  let obj := Json.mkObj [
    ("schemaVersion", toJson (1 : Nat)),
    ("kind", Json.str "candidate"),
    ("recordKind", Json.str "candidate"),
    ("status", Json.str "ok"),
    ("declaration", Json.str declaration),
    ("family", Json.str candidate.family),
    ("operation", Json.str candidate.operation),
    ("operationKind", Json.str (candidate.operation.splitOn ":").head!),
    ("sitePath", Json.str "/root-body"),
    ("source", Json.str source),
    ("candidate", Json.str candidatePretty),
    ("sourceTypeHash", Json.str sourceHash),
    ("candidateTypeHash", Json.str candidateHash),
    ("evidenceClass", Json.str "N-SEP"),
    ("evidence", evidence),
    ("witness", witness),
    ("candidateElaborates", Json.bool true),
    ("wholeTypeDefEq", Json.bool false),
    ("axioms", Json.str "none")
  ]
  let key := candidate.family ++ "\u0000" ++ candidate.operation ++ "\u0000" ++ candidateHash
  return some { key, candidateTypeHash := candidateHash, json := obj }

def lfPrintTerminal
    (declaration status : String) (discovered emitted rejected : Nat)
    (extra : List (String × Json) := []) : IO Unit := do
  IO.println <| (Json.mkObj <| [
    ("schemaVersion", toJson (1 : Nat)),
    ("kind", Json.str "terminal"),
    ("recordKind", Json.str "status"),
    ("declaration", Json.str declaration),
    ("status", Json.str status),
    ("discoveredCount", toJson discovered),
    ("emittedCount", toJson emitted),
    ("rejectedCount", toJson rejected)
  ] ++ extra).compress

def lfProcessDeclaration (declaration : String) : TermElabM Bool := do
  let name := declaration.toName
  let some ci := (← getEnv).find? name | do
    lfPrintTerminal declaration "notfound" 0 0 0
    return false
  let sourceType := lfNegativeCanonicalType ci
  if !(← isProp sourceType) then
    lfPrintTerminal declaration "notProp" 0 0 0
    return false
  let source ← lfNegativePp sourceType
  let sourceHash ← lfNegativeSha256 source
  if !(← lfNegativeTextElaboratesAs source sourceType (lfNegativeLevelNames ci)) then
    lfPrintTerminal declaration "sourceTextRejected" 0 0 0 [
      ("source", Json.str source),
      ("sourceTypeHash", Json.str sourceHash)
    ]
    return true
  let candidates ← lfLocalCandidates sourceType
  let mut emissions := #[]
  for candidate in candidates do
    if let some emission ← lfCandidateEmission? declaration source sourceHash ci candidate then
      emissions := emissions.push emission
  let sortedEmissions := emissions.qsort fun a b => a.key < b.key
  let mut seen : Std.HashSet String := {}
  let mut emitted := 0
  for emission in sortedEmissions do
    if !seen.contains emission.candidateTypeHash then
      seen := seen.insert emission.candidateTypeHash
      emitted := emitted + 1
      IO.println emission.json.compress
  lfPrintTerminal declaration "complete" candidates.size emitted (candidates.size - emissions.size) [
    ("source", Json.str source),
    ("sourceTypeHash", Json.str sourceHash),
    ("sourceTextRoundtripVerified", Json.bool true)
  ]
  return true

def lfProcessDeclarationIsolated (declaration : String) : TermElabM Bool := do
  try
    lfProcessDeclaration declaration
  catch ex =>
    if ex.isInterrupt then throw ex
    lfPrintTerminal declaration "error" 0 0 0 [
      ("error", Json.str (← ex.toMessageData.toString))
    ]
    return false

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

elab "lfNegativeSkeletonBatch " s:str : command => do
  liftTermElabM do
    let path := s.getString
    let names ←
      match (← lfReadBatchNames path) with
      | .ok names => pure names
      | .error error => throwError error
    let mut completed := 0
    let mut failed := 0
    for declaration in names do
      if ← lfProcessDeclarationIsolated declaration then
        completed := completed + 1
      else
        failed := failed + 1
    IO.println <| (Json.mkObj [
      ("schemaVersion", toJson (1 : Nat)),
      ("kind", Json.str "batch"),
      ("recordKind", Json.str "batch"),
      ("status", Json.str (if failed == 0 then "complete" else "partial")),
      ("declarationCount", toJson names.size),
      ("completedCount", toJson completed),
      ("failedCount", toJson failed)
    ]).compress

elab "lfAuditNegativeSkeleton " d:str f:str o:str h:str : command => do
  liftTermElabM do
    let declaration := d.getString
    let family := f.getString
    let operation := o.getString
    let expectedHash := h.getString
    let mut actualHash := ""
    let mut verified := false
    let mut reason := "notfound"
    if let some ci := (← getEnv).find? declaration.toName then
      let sourceType := lfNegativeCanonicalType ci
      if ← isProp sourceType then
        try
          let candidate ← lfReconstructCandidate sourceType family operation
          let pretty ← lfNegativePp candidate.candidate
          actualHash ← lfNegativeSha256 pretty
          let elaborates ← lfNegativeTextElaboratesAs pretty candidate.candidate
            (lfNegativeLevelNames ci)
          let separated := candidate.sourceValue != candidate.candidateValue
          verified := actualHash == expectedHash && elaborates && separated
          reason := if verified then "verified" else "hash-or-separator-mismatch"
        catch ex =>
          if ex.isInterrupt then throw ex
          reason ← ex.toMessageData.toString
      else
        reason := "declaration-not-prop"
    IO.println <| (Json.mkObj [
      ("schemaVersion", toJson (1 : Nat)),
      ("kind", Json.str "audit"),
      ("recordKind", Json.str "audit"),
      ("declaration", Json.str declaration),
      ("family", Json.str family),
      ("operation", Json.str operation),
      ("expectedCandidateTypeHash", Json.str expectedHash),
      ("actualCandidateTypeHash", Json.str actualHash),
      ("verified", Json.bool verified),
      ("status", Json.str (if verified then "verified" else "rejected")),
      ("reason", Json.str reason),
      ("auditMode", Json.str "independent-root-reconstruction")
    ]).compress

end LeanFaith.Meta.NegativeSkeletonEngineHelper
