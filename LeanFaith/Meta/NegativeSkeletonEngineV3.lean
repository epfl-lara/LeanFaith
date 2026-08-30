/-
LeanFaith implication-aware typed negative Boolean-skeleton pilot, version 3.

This additive engine reuses the frozen v2 Boolean representation and evidence
utilities, but changes how the theorem telescope is opened.  Non-proposition
parameters and dependent binders are opened as locals; a nondependent Prop
binder is preserved as an implication node in the conclusion skeleton.

N21 still negates one atomic proposition occurrence.  N22 additionally offers
three exact implication mutations (implication to iff, converse implication,
and implication to conjunction) alongside And/Or/Iff connective replacement.
Every mutation exhaustively evaluates the complete truth table (at most eight
deduplicated atoms) and is emitted only when some valuation changes the root.
-/
import LeanFaith.Meta.NegativeSkeletonEngineV2

namespace LeanFaith.Meta.NegativeSkeletonEngineV3Helper

open Lean Elab Command Meta
open LeanFaith.Meta.NegativeSkeletonEngineV2Helper

partial def lfAddAtom (expr : Expr) (atoms : Array Expr) : BoolSkeleton × Array Expr :=
  match lfAtomIndex atoms expr with
  | some index => (.atom index, atoms)
  | none => (.atom atoms.size, atoms.push expr)

partial def lfBuildSkeletonV3
    (expr : Expr) (atoms : Array Expr := #[]) : MetaM (BoolSkeleton × Array Expr) := do
  let expr := lfStripMData expr
  match expr with
  | .const name _ =>
      if name == ``True then return (.true, atoms)
      if name == ``False then return (.false, atoms)
      return lfAddAtom expr atoms
  | .app (.const name _) inner =>
      if name == ``Not then
        let (innerSkeleton, atoms) ← lfBuildSkeletonV3 inner atoms
        return (.not innerSkeleton, atoms)
      return lfAddAtom expr atoms
  | .app (.app (.const name _) left) right =>
      if name == ``And || name == ``Or || name == ``Iff then
        let (leftSkeleton, atoms) ← lfBuildSkeletonV3 left atoms
        let (rightSkeleton, atoms) ← lfBuildSkeletonV3 right atoms
        if name == ``And then return (.and leftSkeleton rightSkeleton, atoms)
        if name == ``Or then return (.or leftSkeleton rightSkeleton, atoms)
        return (.iff leftSkeleton rightSkeleton, atoms)
      return lfAddAtom expr atoms
  | .forallE _ domain body _ =>
      if !body.hasLooseBVar 0 && (← isProp domain) then
        let (leftSkeleton, atoms) ← lfBuildSkeletonV3 domain atoms
        let (rightSkeleton, atoms) ← lfBuildSkeletonV3 body atoms
        return (.imp leftSkeleton rightSkeleton, atoms)
      return lfAddAtom expr atoms
  | _ => return lfAddAtom expr atoms

def lfChildPathV3 (path side : String) : String := path ++ "/" ++ side

partial def lfSkeletonMutationsV3
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
      (lfSkeletonMutationsV3 inner (lfChildPathV3 path "not")).map fun mutation =>
        { mutation with candidate := .not mutation.candidate }
  | .and left right =>
      let root := #[{
        family := "N22"
        operationKind := "andToOr"
        sitePath := path
        candidate := .or left right
      }]
      let leftMutations :=
        (lfSkeletonMutationsV3 left (lfChildPathV3 path "left")).map fun mutation =>
          { mutation with candidate := .and mutation.candidate right }
      let rightMutations :=
        (lfSkeletonMutationsV3 right (lfChildPathV3 path "right")).map fun mutation =>
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
        (lfSkeletonMutationsV3 left (lfChildPathV3 path "left")).map fun mutation =>
          { mutation with candidate := .or mutation.candidate right }
      let rightMutations :=
        (lfSkeletonMutationsV3 right (lfChildPathV3 path "right")).map fun mutation =>
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
        (lfSkeletonMutationsV3 left (lfChildPathV3 path "left")).map fun mutation =>
          { mutation with candidate := .iff mutation.candidate right }
      let rightMutations :=
        (lfSkeletonMutationsV3 right (lfChildPathV3 path "right")).map fun mutation =>
          { mutation with candidate := .iff left mutation.candidate }
      root ++ leftMutations ++ rightMutations
  | .imp left right =>
      let root := #[
        {
          family := "N22"
          operationKind := "impToIff"
          sitePath := path
          candidate := .iff left right
        },
        {
          family := "N22"
          operationKind := "impConverse"
          sitePath := path
          candidate := .imp right left
        },
        {
          family := "N22"
          operationKind := "impToAnd"
          sitePath := path
          candidate := .and left right
        }
      ]
      let leftMutations :=
        (lfSkeletonMutationsV3 left (lfChildPathV3 path "left")).map fun mutation =>
          { mutation with candidate := .imp mutation.candidate right }
      let rightMutations :=
        (lfSkeletonMutationsV3 right (lfChildPathV3 path "right")).map fun mutation =>
          { mutation with candidate := .imp left mutation.candidate }
      root ++ leftMutations ++ rightMutations

def lfCandidatesFromBodyV3
    (xs : Array Expr) (rawBody : Expr) : MetaM (Array SkeletonCandidate) := do
    let body := lfStripMData rawBody
    let (sourceSkeleton, atoms) ← lfBuildSkeletonV3 body
    if atoms.isEmpty || atoms.size > maxSkeletonAtoms then
      return #[]
    let valuationSpaceSize := 2 ^ atoms.size
    let mut candidates := #[]
    for mutation in lfSkeletonMutationsV3 sourceSkeleton do
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

def lfLocalCandidatesV3 (sourceType : Expr) : MetaM (Array SkeletonCandidate) :=
  go sourceType #[]
where
  go (rawType : Expr) (xs : Array Expr) : MetaM (Array SkeletonCandidate) := do
    match rawType with
    | .forallE binderName domain body binderInfo =>
        let instantiatedDomain := domain.instantiateRev xs
        let preserveAsImplication := !body.hasLooseBVar 0 && (← isProp instantiatedDomain)
        if preserveAsImplication then
          lfCandidatesFromBodyV3 xs (rawType.instantiateRev xs)
        else
          Meta.withLocalDecl binderName binderInfo instantiatedDomain fun fvar =>
            go body (xs.push fvar)
    | _ => lfCandidatesFromBodyV3 xs (rawType.instantiateRev xs)

def lfReconstructCandidateV3
    (sourceType : Expr) (family operation : String) : MetaM SkeletonCandidate := do
  for candidate in (← lfLocalCandidatesV3 sourceType) do
    if candidate.family == family && candidate.operation == operation then
      return candidate
  throwError "implication-aware negative skeleton operation is no longer applicable"

def lfCandidateEmissionV3?
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
    ("implicationAware", Json.bool true),
    ("parameterTelescopePreserved", Json.bool true),
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
    ("schemaVersion", toJson (3 : Nat)),
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

def lfPrintTerminalV3
    (declaration status : String) (discovered emitted rejected : Nat)
    (extra : List (String × Json) := []) : IO Unit := do
  IO.println <| (Json.mkObj <| [
    ("schemaVersion", toJson (3 : Nat)),
    ("kind", Json.str "terminal"),
    ("recordKind", Json.str "status"),
    ("declaration", Json.str declaration),
    ("status", Json.str status),
    ("discoveredCount", toJson discovered),
    ("emittedCount", toJson emitted),
    ("rejectedCount", toJson rejected)
  ] ++ extra).compress

def lfProcessDeclarationV3 (declaration : String) : TermElabM Bool := do
  let name := declaration.toName
  let some ci := (← getEnv).find? name | do
    lfPrintTerminalV3 declaration "notfound" 0 0 0
    return false
  let sourceType := lfNegativeV2CanonicalType ci
  if !(← isProp sourceType) then
    lfPrintTerminalV3 declaration "notProp" 0 0 0
    return false
  let source ← lfNegativeV2Pp sourceType
  let sourceHash ← lfNegativeV2Sha256 source
  if !(← lfNegativeV2TextElaboratesAs source sourceType (lfNegativeV2LevelNames ci)) then
    lfPrintTerminalV3 declaration "sourceTextRejected" 0 0 0 [
      ("source", Json.str source),
      ("sourceTypeHash", Json.str sourceHash)
    ]
    return true
  let candidates ← lfLocalCandidatesV3 sourceType
  let mut emissions := #[]
  for candidate in candidates do
    if let some emission ← lfCandidateEmissionV3? declaration source sourceHash ci candidate then
      emissions := emissions.push emission
  let sortedEmissions := emissions.qsort fun a b => a.key < b.key
  let mut seen : Std.HashSet String := {}
  let mut emitted := 0
  for emission in sortedEmissions do
    if !seen.contains emission.candidateTypeHash then
      seen := seen.insert emission.candidateTypeHash
      emitted := emitted + 1
      IO.println emission.json.compress
  lfPrintTerminalV3 declaration "complete" candidates.size emitted
    (candidates.size - emissions.size) [
      ("source", Json.str source),
      ("sourceTypeHash", Json.str sourceHash),
      ("sourceTextRoundtripVerified", Json.bool true),
      ("maxSkeletonAtoms", toJson maxSkeletonAtoms),
      ("implicationAware", Json.bool true)
    ]
  return true

def lfProcessDeclarationIsolatedV3 (declaration : String) : TermElabM Bool := do
  try
    lfProcessDeclarationV3 declaration
  catch ex =>
    if ex.isInterrupt then throw ex
    lfPrintTerminalV3 declaration "error" 0 0 0 [
      ("error", Json.str (← ex.toMessageData.toString))
    ]
    return false

elab "lfNegativeSkeletonV3Batch " s:str : command => do
  liftTermElabM do
    let path := s.getString
    let names ←
      match (← lfReadBatchNames path) with
      | .ok names => pure names
      | .error error => throwError error
    let mut completed := 0
    let mut failed := 0
    for declaration in names do
      if ← lfProcessDeclarationIsolatedV3 declaration then
        completed := completed + 1
      else
        failed := failed + 1
    IO.println <| (Json.mkObj [
      ("schemaVersion", toJson (3 : Nat)),
      ("kind", Json.str "batch"),
      ("recordKind", Json.str "batch"),
      ("status", Json.str (if failed == 0 then "complete" else "partial")),
      ("declarationCount", toJson names.size),
      ("completedCount", toJson completed),
      ("failedCount", toJson failed)
    ]).compress

elab "lfAuditNegativeSkeletonV3 " d:str f:str o:str h:str : command => do
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
          let candidate ← lfReconstructCandidateV3 sourceType family operation
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
      ("schemaVersion", toJson (3 : Nat)),
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
      ("auditMode", Json.str "independent-implication-aware-reconstruction")
    ]).compress

end LeanFaith.Meta.NegativeSkeletonEngineV3Helper
