/-
LeanFaith typed negative full Boolean-skeleton pilot, version 2.

This additive engine leaves the frozen root-only v1 engine unchanged.  It
opens the outer theorem telescope, strips expression metadata, represents the
complete And/Or/Iff/Not skeleton over deduplicated atomic propositions, and
emits:

* N21: negate one atom occurrence at a stable logical path;
* N22: replace And with Or, Or with And, or Iff with its forward implication
  at any root or nested logical path.

The atom count is capped at eight.  Every emitted row enumerates the complete
Boolean valuation space and retains a separating valuation, proving that the
edited site influences the root of the represented proposition skeleton.  The
independent audit command regenerates the exact declaration/family/operation
and requires the candidate pretty-text hash and separator to match.

This is structural N-SEP evidence under an abstract propositional-schema
contract; it does not claim a concrete countermodel for dependencies between
otherwise distinct Lean propositions.
-/
import Lean

namespace LeanFaith.Meta.NegativeSkeletonEngineV2Helper

open Lean Elab Command Meta

inductive BoolSkeleton where
  | atom (index : Nat)
  | true
  | false
  | and (left right : BoolSkeleton)
  | or (left right : BoolSkeleton)
  | iff (left right : BoolSkeleton)
  | imp (left right : BoolSkeleton)
  | not (inner : BoolSkeleton)
  deriving BEq, Repr, Inhabited

structure SkeletonMutation where
  family : String
  operationKind : String
  sitePath : String
  candidate : BoolSkeleton

structure Separator where
  valuation : Array Bool
  sourceValue : Bool
  candidateValue : Bool

structure SkeletonCandidate where
  family : String
  operation : String
  operationKind : String
  sitePath : String
  candidate : Expr
  sourceSkeleton : String
  candidateSkeleton : String
  atoms : Array Expr
  separator : Separator
  valuationSpaceSize : Nat
  outerBinderCount : Nat

structure CandidateEmission where
  key : String
  candidateTypeHash : String
  json : Json

def maxSkeletonAtoms : Nat := 8

def lfNegativeV2Pp (type : Expr) : MetaM String := do
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

def lfNegativeV2CanonicalType (ci : ConstantInfo) : Expr :=
  let levels := (List.range ci.levelParams.length).map fun i =>
    Level.param (Name.mkSimple s!"u_{i}")
  ci.type.instantiateLevelParams ci.levelParams levels

def lfNegativeV2LevelNames (ci : ConstantInfo) : List Name :=
  (List.range ci.levelParams.length).map fun i => Name.mkSimple s!"u_{i}"

def lfNegativeV2Sha256 (s : String) : IO String := do
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

def lfNegativeV2TextElaboratesAs
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

partial def lfStripMData : Expr → Expr
  | .mdata _ inner => lfStripMData inner
  | expr => expr

def lfAtomIndex (atoms : Array Expr) (atom : Expr) : Option Nat :=
  let rec loop (index : Nat) : Option Nat :=
    if h : index < atoms.size then
      if atoms[index] == atom then some index else loop (index + 1)
    else
      none
  loop 0

partial def lfBuildSkeleton (expr : Expr) (atoms : Array Expr := #[]) : BoolSkeleton × Array Expr :=
  let expr := lfStripMData expr
  match expr with
  | .const name _ =>
      if name == ``True then (.true, atoms)
      else if name == ``False then (.false, atoms)
      else
        match lfAtomIndex atoms expr with
        | some index => (.atom index, atoms)
        | none => (.atom atoms.size, atoms.push expr)
  | .app (.const name _) inner =>
      if name == ``Not then
        let (innerSkeleton, atoms) := lfBuildSkeleton inner atoms
        (.not innerSkeleton, atoms)
      else
        match lfAtomIndex atoms expr with
        | some index => (.atom index, atoms)
        | none => (.atom atoms.size, atoms.push expr)
  | .app (.app (.const name _) left) right =>
      if name == ``And || name == ``Or || name == ``Iff then
        let (leftSkeleton, atoms) := lfBuildSkeleton left atoms
        let (rightSkeleton, atoms) := lfBuildSkeleton right atoms
        if name == ``And then (.and leftSkeleton rightSkeleton, atoms)
        else if name == ``Or then (.or leftSkeleton rightSkeleton, atoms)
        else (.iff leftSkeleton rightSkeleton, atoms)
      else
        match lfAtomIndex atoms expr with
        | some index => (.atom index, atoms)
        | none => (.atom atoms.size, atoms.push expr)
  | _ =>
      match lfAtomIndex atoms expr with
      | some index => (.atom index, atoms)
      | none => (.atom atoms.size, atoms.push expr)

partial def lfRenderSkeleton : BoolSkeleton → String
  | .atom index => s!"A{index}"
  | .true => "⊤"
  | .false => "⊥"
  | .and left right => s!"({lfRenderSkeleton left} ∧ {lfRenderSkeleton right})"
  | .or left right => s!"({lfRenderSkeleton left} ∨ {lfRenderSkeleton right})"
  | .iff left right => s!"({lfRenderSkeleton left} ↔ {lfRenderSkeleton right})"
  | .imp left right => s!"({lfRenderSkeleton left} → {lfRenderSkeleton right})"
  | .not inner => s!"¬({lfRenderSkeleton inner})"

partial def lfRenderExpr (skeleton : BoolSkeleton) (atoms : Array Expr) : Expr :=
  match skeleton with
  | .atom index => atoms[index]!
  | .true => mkConst ``True
  | .false => mkConst ``False
  | .and left right =>
      mkApp2 (mkConst ``And) (lfRenderExpr left atoms) (lfRenderExpr right atoms)
  | .or left right =>
      mkApp2 (mkConst ``Or) (lfRenderExpr left atoms) (lfRenderExpr right atoms)
  | .iff left right =>
      mkApp2 (mkConst ``Iff) (lfRenderExpr left atoms) (lfRenderExpr right atoms)
  | .imp left right =>
      mkForall Name.anonymous BinderInfo.default (lfRenderExpr left atoms)
        (lfRenderExpr right atoms)
  | .not inner => mkApp (mkConst ``Not) (lfRenderExpr inner atoms)

partial def lfEvalSkeleton (skeleton : BoolSkeleton) (valuation : Array Bool) : Bool :=
  match skeleton with
  | .atom index => valuation[index]!
  | .true => true
  | .false => false
  | .and left right => lfEvalSkeleton left valuation && lfEvalSkeleton right valuation
  | .or left right => lfEvalSkeleton left valuation || lfEvalSkeleton right valuation
  | .iff left right => lfEvalSkeleton left valuation == lfEvalSkeleton right valuation
  | .imp left right => !lfEvalSkeleton left valuation || lfEvalSkeleton right valuation
  | .not inner => !lfEvalSkeleton inner valuation

def lfChildPath (path side : String) : String := path ++ "/" ++ side

partial def lfSkeletonMutations
    (skeleton : BoolSkeleton) (path : String := "/root-body") : Array SkeletonMutation :=
  match skeleton with
  | .atom index =>
      #[{
        family := "N21"
        operationKind := "negateAtom"
        sitePath := path
        candidate := .not (.atom index)
      }]
  | .true | .false => #[]
  | .not inner =>
      (lfSkeletonMutations inner (lfChildPath path "not")).map fun mutation =>
        { mutation with candidate := .not mutation.candidate }
  | .and left right =>
      let root := #[{
        family := "N22"
        operationKind := "andToOr"
        sitePath := path
        candidate := .or left right
      }]
      let leftMutations :=
        (lfSkeletonMutations left (lfChildPath path "left")).map fun mutation =>
          { mutation with candidate := .and mutation.candidate right }
      let rightMutations :=
        (lfSkeletonMutations right (lfChildPath path "right")).map fun mutation =>
          { mutation with candidate := .and left mutation.candidate }
      root ++ leftMutations ++ rightMutations
  | .or left right =>
      let root := #[{
        family := "N22"
        operationKind := "orToAnd"
        sitePath := path
        candidate := .and left right
      }]
      let leftMutations :=
        (lfSkeletonMutations left (lfChildPath path "left")).map fun mutation =>
          { mutation with candidate := .or mutation.candidate right }
      let rightMutations :=
        (lfSkeletonMutations right (lfChildPath path "right")).map fun mutation =>
          { mutation with candidate := .or left mutation.candidate }
      root ++ leftMutations ++ rightMutations
  | .iff left right =>
      let root := #[{
        family := "N22"
        operationKind := "iffToImp"
        sitePath := path
        candidate := .imp left right
      }]
      let leftMutations :=
        (lfSkeletonMutations left (lfChildPath path "left")).map fun mutation =>
          { mutation with candidate := .iff mutation.candidate right }
      let rightMutations :=
        (lfSkeletonMutations right (lfChildPath path "right")).map fun mutation =>
          { mutation with candidate := .iff left mutation.candidate }
      root ++ leftMutations ++ rightMutations
  | .imp left right =>
      let leftMutations :=
        (lfSkeletonMutations left (lfChildPath path "left")).map fun mutation =>
          { mutation with candidate := .imp mutation.candidate right }
      let rightMutations :=
        (lfSkeletonMutations right (lfChildPath path "right")).map fun mutation =>
          { mutation with candidate := .imp left mutation.candidate }
      leftMutations ++ rightMutations

def lfValuation (mask atomCount : Nat) : Array Bool :=
  (Array.range atomCount).map fun index => mask.testBit index

partial def lfFindSeparator
    (source candidate : BoolSkeleton) (atomCount : Nat) : Option Separator :=
  let valuationSpaceSize := 2 ^ atomCount
  let rec loop (mask : Nat) (firstSeparator : Option Separator) : Option Separator :=
    if mask < valuationSpaceSize then
      let valuation := lfValuation mask atomCount
      let sourceValue := lfEvalSkeleton source valuation
      let candidateValue := lfEvalSkeleton candidate valuation
      let firstSeparator :=
        if sourceValue != candidateValue && firstSeparator.isNone then
          some { valuation, sourceValue, candidateValue }
        else
          firstSeparator
      loop (mask + 1) firstSeparator
    else
      firstSeparator
  loop 0 none

def lfLocalCandidates (sourceType : Expr) : MetaM (Array SkeletonCandidate) :=
  forallTelescope sourceType fun xs rawBody => do
    let body := lfStripMData rawBody
    let (sourceSkeleton, atoms) := lfBuildSkeleton body
    if atoms.isEmpty || atoms.size > maxSkeletonAtoms then
      return #[]
    let valuationSpaceSize := 2 ^ atoms.size
    let mut candidates := #[]
    for mutation in lfSkeletonMutations sourceSkeleton do
      if let some separator := lfFindSeparator sourceSkeleton mutation.candidate atoms.size then
        let candidateBody := lfRenderExpr mutation.candidate atoms
        let candidateType ← mkForallFVars xs candidateBody
        candidates := candidates.push {
          family := mutation.family
          operation := mutation.operationKind ++ ":" ++ mutation.sitePath
          operationKind := mutation.operationKind
          sitePath := mutation.sitePath
          candidate := candidateType
          sourceSkeleton := lfRenderSkeleton sourceSkeleton
          candidateSkeleton := lfRenderSkeleton mutation.candidate
          atoms
          separator
          valuationSpaceSize
          outerBinderCount := xs.size
        }
    return candidates

def lfReconstructCandidate
    (sourceType : Expr) (family operation : String) : MetaM SkeletonCandidate := do
  for candidate in (← lfLocalCandidates sourceType) do
    if candidate.family == family && candidate.operation == operation then
      return candidate
  throwError "full negative skeleton operation is no longer applicable"

def lfAtomHashes (atoms : Array Expr) : MetaM (Array String) :=
  atoms.mapM fun atom => do
    lfNegativeV2Sha256 (← lfNegativeV2Pp atom)

def lfCandidateEmission?
    (declaration source sourceHash : String) (ci : ConstantInfo)
    (candidate : SkeletonCandidate) : TermElabM (Option CandidateEmission) := do
  if candidate.separator.sourceValue == candidate.separator.candidateValue then
    return none
  let candidatePretty ← lfNegativeV2Pp candidate.candidate
  if candidatePretty == source then
    return none
  if !(← lfNegativeV2TextElaboratesAs candidatePretty candidate.candidate
      (lfNegativeV2LevelNames ci)) then
    return none
  let candidateHash ← lfNegativeV2Sha256 candidatePretty
  let atomHashes ← lfAtomHashes candidate.atoms
  let evidence := Json.mkObj [
    ("relation", Json.str "schemaInequivalence"),
    ("exactBooleanSkeleton", Json.bool true),
    ("deduplicatedAtoms", Json.bool true),
    ("fullTruthTableEnumerated", Json.bool true),
    ("rootInfluence", Json.bool true),
    ("separatorVerified", Json.bool true),
    ("contractScope", Json.str "abstract-propositional-schema")
  ]
  let witness := Json.mkObj [
    ("sourceSkeleton", Json.str candidate.sourceSkeleton),
    ("candidateSkeleton", Json.str candidate.candidateSkeleton),
    ("atomHashes", toJson atomHashes),
    ("atomCount", toJson candidate.atoms.size),
    ("valuationSpaceSize", toJson candidate.valuationSpaceSize),
    ("valuation", toJson candidate.separator.valuation),
    ("sourceValue", Json.bool candidate.separator.sourceValue),
    ("candidateValue", Json.bool candidate.separator.candidateValue),
    ("outerBinderCount", toJson candidate.outerBinderCount)
  ]
  let obj := Json.mkObj [
    ("schemaVersion", toJson (2 : Nat)),
    ("kind", Json.str "candidate"),
    ("recordKind", Json.str "candidate"),
    ("status", Json.str "ok"),
    ("declaration", Json.str declaration),
    ("family", Json.str candidate.family),
    ("operation", Json.str candidate.operation),
    ("operationKind", Json.str candidate.operationKind),
    ("sitePath", Json.str candidate.sitePath),
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
    ("schemaVersion", toJson (2 : Nat)),
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
  let sourceType := lfNegativeV2CanonicalType ci
  if !(← isProp sourceType) then
    lfPrintTerminal declaration "notProp" 0 0 0
    return false
  let source ← lfNegativeV2Pp sourceType
  let sourceHash ← lfNegativeV2Sha256 source
  if !(← lfNegativeV2TextElaboratesAs source sourceType (lfNegativeV2LevelNames ci)) then
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
    ("sourceTextRoundtripVerified", Json.bool true),
    ("maxSkeletonAtoms", toJson maxSkeletonAtoms)
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

elab "lfNegativeSkeletonV2Batch " s:str : command => do
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
      ("schemaVersion", toJson (2 : Nat)),
      ("kind", Json.str "batch"),
      ("recordKind", Json.str "batch"),
      ("status", Json.str (if failed == 0 then "complete" else "partial")),
      ("declarationCount", toJson names.size),
      ("completedCount", toJson completed),
      ("failedCount", toJson failed)
    ]).compress

elab "lfAuditNegativeSkeletonV2 " d:str f:str o:str h:str : command => do
  liftTermElabM do
    let declaration := d.getString
    let family := f.getString
    let operation := o.getString
    let expectedHash := h.getString
    let mut actualHash := ""
    let mut verified := false
    let mut reason := "notfound"
    if let some ci := (← getEnv).find? declaration.toName then
      let sourceType := lfNegativeV2CanonicalType ci
      if ← isProp sourceType then
        try
          let candidate ← lfReconstructCandidate sourceType family operation
          let pretty ← lfNegativeV2Pp candidate.candidate
          actualHash ← lfNegativeV2Sha256 pretty
          let elaborates ← lfNegativeV2TextElaboratesAs pretty candidate.candidate
            (lfNegativeV2LevelNames ci)
          let separated :=
            candidate.separator.sourceValue != candidate.separator.candidateValue &&
              candidate.separator.valuation.size == candidate.atoms.size
          verified := actualHash == expectedHash && elaborates && separated
          reason := if verified then "verified" else "hash-or-separator-mismatch"
        catch ex =>
          if ex.isInterrupt then throw ex
          reason ← ex.toMessageData.toString
      else
        reason := "declaration-not-prop"
    IO.println <| (Json.mkObj [
      ("schemaVersion", toJson (2 : Nat)),
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
      ("auditMode", Json.str "independent-full-skeleton-reconstruction")
    ]).compress

end LeanFaith.Meta.NegativeSkeletonEngineV2Helper
