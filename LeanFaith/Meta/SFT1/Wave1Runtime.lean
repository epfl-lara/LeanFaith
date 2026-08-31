/-
SFT1 Wave 1 additive implementation-readiness surface, revision v0.3.6.

This file is deliberately import-strippable.  A generated persistent Meta
request may remove the import line and inject this source after the frozen
Wave1 engine.  The definitions below only delegate to the frozen positive
dispatch/replay API, expose typed receipts, and inspect a proposed N31 bank
without admitting it.  A positive rendering request supplies GoalV1 separately;
rejection and N31 requests need no GoalV1 definition.

In particular, this file does not declare a theorem or axiom, construct a
proof, render or pretty-print an Expr, elaborate rendered text, canonicalize
universes, emit a row, or expose a gate/production entrypoint.  The caller is
responsible for invoking `LeanFaith.GoalV1.emitClosedProp` exactly once for
each explicitly unrolled reference/candidate endpoint while both Exprs remain
alive in the same `run_meta do` request.
-/
import LeanFaith.Meta.SFT1.Wave1

namespace LeanFaith.SFT1.Wave1Runtime

open Lean Meta

abbrev Operation := LeanFaith.SFT1.Wave1.PrimaryOperation
abbrev Selector := LeanFaith.SFT1.Wave1.Selector
abbrev FailureReason := LeanFaith.SFT1.Wave1.FailureReason
abbrev DispatchContext := LeanFaith.SFT1.Wave1.DispatchContext
abbrev Certificate := LeanFaith.SFT1.Wave1.Certificate
abbrev ApplyResult := LeanFaith.SFT1.Wave1.ApplyResult
abbrev ReplayResult := LeanFaith.SFT1.Wave1.ReplayResult
abbrev N31TargetBank := LeanFaith.SFT1.Wave1.N31TargetBank
abbrev N31TargetBankEntry := LeanFaith.SFT1.Wave1.N31TargetBankEntry
abbrev N31RetainedContradictionPattern :=
  LeanFaith.SFT1.Wave1.N31RetainedContradictionPattern
abbrev N31ReachabilityEvidence := LeanFaith.SFT1.Wave1.N31ReachabilityEvidence
abbrev P01Certificate := LeanFaith.SFT1.Wave1.P01Certificate

def sourceVersion : String := "sft1_wave1_runtime_readiness_v0_3_6"

def frozenEngineVersion : String := LeanFaith.SFT1.Wave1.sourceVersion

/-- Frozen REPR integration identity.  These literals are evidence/configuration
    only: this helper does not depend on, copy, or invoke the renderer or its
    universe canonicalizer. -/
structure ReprRuntimeIdentity where
  routeId : String
  emitterSymbol : String
  universeProfileId : String
  universeProfileHash : String
  rendererSemanticHash : String
  renderContextId : String
  renderContextHash : String
  closedExprHashAlgorithm : String
  deriving BEq, Inhabited, Repr

def reprRuntimeIdentity : ReprRuntimeIdentity := {
  routeId := "closed_expr_in_session"
  emitterSymbol := "LeanFaith.GoalV1.emitClosedProp"
  universeProfileId := "goal_v1_first_occurrence_u_i_v1"
  universeProfileHash :=
    "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
  rendererSemanticHash :=
    "0bec5429cc0e539841208be53cd52189a7b80cbdb4649ee2d45b84bd8a5ef1fd"
  renderContextId := "goal_v1_render_context_v1"
  renderContextHash :=
    "5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"
  closedExprHashAlgorithm := "sha256_canonical_closed_expr_alpha_tree_v1"
}

/-- The only executable operation constructors exported by this additive
    surface are the four preserving Wave 1 mechanisms. -/
inductive PositiveBundle where
  | p01AlphaRenameSingle
  | p15SwapIffSides
  | p18SymmetrizeEquality
  | p21BetaReduce
  deriving BEq, DecidableEq, Inhabited, Repr

def PositiveBundle.operation : PositiveBundle → Operation
  | .p01AlphaRenameSingle => .p01AlphaRenameSingle
  | .p15SwapIffSides => .p15SwapIffSides
  | .p18SymmetrizeEquality => .p18SymmetrizeEquality
  | .p21BetaReduce => .p21BetaReduce

def PositiveBundle.operationId (bundle : PositiveBundle) : String :=
  bundle.operation.operationId

structure PositiveBundleIdentity where
  bundle : PositiveBundle
  operation : Operation
  operationId : String
  operationConstructor : String
  dispatchSymbol : String
  discoverSymbol : String
  checkerSymbol : String
  frozenEngineVersion : String
  deriving BEq, Inhabited, Repr

def PositiveBundle.operationConstructor : PositiveBundle → String
  | .p01AlphaRenameSingle =>
      "LeanFaith.SFT1.Wave1.PrimaryOperation.p01AlphaRenameSingle"
  | .p15SwapIffSides =>
      "LeanFaith.SFT1.Wave1.PrimaryOperation.p15SwapIffSides"
  | .p18SymmetrizeEquality =>
      "LeanFaith.SFT1.Wave1.PrimaryOperation.p18SymmetrizeEquality"
  | .p21BetaReduce =>
      "LeanFaith.SFT1.Wave1.PrimaryOperation.p21BetaReduce"

def PositiveBundle.identity (bundle : PositiveBundle) : PositiveBundleIdentity := {
  bundle
  operation := bundle.operation
  operationId := bundle.operationId
  operationConstructor := bundle.operationConstructor
  dispatchSymbol := "LeanFaith.SFT1.Wave1.dispatchAt"
  discoverSymbol := "LeanFaith.SFT1.Wave1.discover"
  checkerSymbol := "LeanFaith.SFT1.Wave1.replayCertificate"
  frozenEngineVersion
}

def positiveBundles : Array PositiveBundle := #[
  .p01AlphaRenameSingle,
  .p15SwapIffSides,
  .p18SymmetrizeEquality,
  .p21BetaReduce
]

/-- Exact positive dispatch with an empty context.  Because the operation is
    derived from `PositiveBundle`, this wrapper cannot dispatch N31. -/
def dispatchPositiveAt
    (bundle : PositiveBundle) (selector : Selector) (source : Expr) : MetaM ApplyResult :=
  LeanFaith.SFT1.Wave1.dispatchAt bundle.operation selector {} source

/-- Exact positive discovery with an empty context. -/
def discoverPositive
    (bundle : PositiveBundle) (source : Expr) : MetaM (Array LeanFaith.SFT1.Wave1.Candidate) :=
  LeanFaith.SFT1.Wave1.discover bundle.operation {} source

private def certificateMatchesBundle
    (bundle : PositiveBundle) (certificate : Certificate) : Bool :=
  match bundle, certificate with
  | .p01AlphaRenameSingle, .p01 _ => true
  | .p15SwapIffSides, .p15 _ => true
  | .p18SymmetrizeEquality, .p18 _ => true
  | .p21BetaReduce, .p21 _ => true
  | _, _ => false

structure PositiveReplayReceipt where
  bundle : PositiveBundleIdentity
  certificateConstructorMatches : Bool
  frozenReplay : ReplayResult
  passed : Bool
  deriving Inhabited, Repr

/-- Independent positive certificate replay.  A certificate from another
    operation, including N31, is rejected even if its own frozen replay would
    have produced a result. -/
def replayPositiveCertificate
    (bundle : PositiveBundle) (source candidate : Expr)
    (certificate : Certificate) : MetaM PositiveReplayReceipt := do
  let constructorMatches := certificateMatchesBundle bundle certificate
  let frozenReplay ←
    LeanFaith.SFT1.Wave1.replayCertificate {} source candidate certificate
  return {
    bundle := bundle.identity
    certificateConstructorMatches := constructorMatches
    frozenReplay
    passed := constructorMatches && frozenReplay.passed &&
      frozenReplay.operation == bundle.operation
  }

/-- Typed selected-binder fingerprint.  Domain and body Exprs are retained so
    the caller's hash-bound receipt can bind every non-name subtree directly;
    no pretty text participates. -/
structure P01BinderFingerprint where
  binderOrdinal : Nat
  binderSite : SubExpr.Pos
  binderName : Name
  binderInfo : BinderInfo
  domain : Expr
  body : Expr
  deriving Inhabited, Repr

structure P01ExactDeltaReceipt where
  operationId : String
  source : Expr
  candidate : Expr
  certificate : P01Certificate
  sourceFingerprint : P01BinderFingerprint
  candidateFingerprint : P01BinderFingerprint
  binderSiteMatchesCertificate : Bool
  sourceNameMatchesCertificate : Bool
  candidateNameMatchesCertificate : Bool
  binderInfoMatchesCertificate : Bool
  namesDiffer : Bool
  domainsExactlyEqual : Bool
  bodiesExactlyEqual : Bool
  sourceCandidateAlphaEquivalent : Bool
  sourceCandidateExactlyDifferent : Bool
  deterministicCandidateReplayExact : Bool
  frozenCertificateReplayPassed : Bool
  exactNameOnlyDeltaPassed : Bool
  deriving Inhabited, Repr

private partial def outerBinderAt?
    (e : Expr) (ordinal : Nat) : Option (Name × Expr × Expr × BinderInfo) :=
  match e with
  | .mdata _ body => outerBinderAt? body ordinal
  | .forallE name domain body binderInfo =>
      if ordinal == 0 then
        some (name, domain, body, binderInfo)
      else
        outerBinderAt? body (ordinal - 1)
  | _ => none

private partial def outerBinderSite : Nat → SubExpr.Pos
  | 0 => .root
  | ordinal + 1 => (outerBinderSite ordinal).pushBindingBody

private def p01Fingerprint?
    (e : Expr) (ordinal : Nat) : Option P01BinderFingerprint := do
  let (name, domain, body, binderInfo) ← outerBinderAt? e ordinal
  return {
    binderOrdinal := ordinal
    binderSite := outerBinderSite ordinal
    binderName := name
    binderInfo
    domain
    body
  }

/-- Produce the binder-aware P01 receipt only after exact frozen replay.
    `deterministicCandidateReplayExact` binds the candidate to the replayed
    frozen implementation, while the two typed fingerprints bind site, names,
    BinderInfo, domain, and body without an Expr-to-text conversion. -/
def p01ExactDeltaReceipt
    (source candidate : Expr) (certificate : P01Certificate) : MetaM P01ExactDeltaReceipt := do
  let some sourceFingerprint := p01Fingerprint? source certificate.binderOrdinal
    | throwError "sft1_p01_source_binder_fingerprint_missing"
  let some candidateFingerprint := p01Fingerprint? candidate certificate.binderOrdinal
    | throwError "sft1_p01_candidate_binder_fingerprint_missing"
  let frozenReplay ← LeanFaith.SFT1.Wave1.replayCertificate
    {} source candidate (.p01 certificate)
  let binderSiteMatchesCertificate :=
    sourceFingerprint.binderSite == certificate.binderSite &&
      candidateFingerprint.binderSite == certificate.binderSite
  let sourceNameMatchesCertificate :=
    sourceFingerprint.binderName == certificate.sourceName
  let candidateNameMatchesCertificate :=
    candidateFingerprint.binderName == certificate.candidateName
  let binderInfoMatchesCertificate :=
    sourceFingerprint.binderInfo == certificate.binderInfo &&
      candidateFingerprint.binderInfo == certificate.binderInfo
  let namesDiffer := sourceFingerprint.binderName != candidateFingerprint.binderName
  let domainsExactlyEqual :=
    Expr.equal sourceFingerprint.domain candidateFingerprint.domain
  let bodiesExactlyEqual :=
    Expr.equal sourceFingerprint.body candidateFingerprint.body
  let sourceCandidateAlphaEquivalent := Expr.eqv source candidate
  let sourceCandidateExactlyDifferent := !Expr.equal source candidate
  let deterministicCandidateReplayExact := frozenReplay.passed
  let exactNameOnlyDeltaPassed :=
    binderSiteMatchesCertificate && sourceNameMatchesCertificate &&
      candidateNameMatchesCertificate && binderInfoMatchesCertificate && namesDiffer &&
      domainsExactlyEqual && bodiesExactlyEqual && sourceCandidateAlphaEquivalent &&
      sourceCandidateExactlyDifferent && deterministicCandidateReplayExact
  unless exactNameOnlyDeltaPassed do
    throwError "sft1_p01_exact_name_only_delta_replay_failed"
  return {
    operationId :=
      LeanFaith.SFT1.Wave1.PrimaryOperation.p01AlphaRenameSingle.operationId
    source
    candidate
    certificate
    sourceFingerprint
    candidateFingerprint
    binderSiteMatchesCertificate
    sourceNameMatchesCertificate
    candidateNameMatchesCertificate
    binderInfoMatchesCertificate
    namesDiffer
    domainsExactlyEqual
    bodiesExactlyEqual
    sourceCandidateAlphaEquivalent
    sourceCandidateExactlyDifferent
    deterministicCandidateReplayExact
    frozenCertificateReplayPassed := frozenReplay.passed
    exactNameOnlyDeltaPassed
  }

/-! ## Compact task-owned receipt emission -/

def receiptMarker : String := "LFSFT1WAVE1JSON "

def structuralExprFingerprintId : String :=
  "lean_hashable_expr_uint64_decimal_v1"

private def uint64Json (value : UInt64) : Json :=
  Json.str (toString value)

private def exprHashJson (e : Expr) : Json :=
  uint64Json (hash e)

private def nameJson (name : Name) : Json :=
  Json.str name.toString

/-- Typed evidence tag only.  This is not a renderer option, presentation
    helper, or copy of any GoalV1 Expr-rendering behavior. -/
private def binderInfoEvidenceTag : BinderInfo → String
  | .default => "default"
  | .implicit => "implicit"
  | .strictImplicit => "strictImplicit"
  | .instImplicit => "instImplicit"

private def cleanClosedExpr (e : Expr) : Bool :=
  !e.hasExprMVar && !e.hasLevelMVar && !e.hasFVar && !e.hasLooseBVars && !e.hasSorry

private def checkedClosedPropEvidence (e : Expr) : MetaM Bool := do
  if !cleanClosedExpr e then
    return false
  try
    check e
    isProp e
  catch ex =>
    if ex.isInterrupt || ex.isRuntime then
      throw ex
    return false

/-- One exact `instImplicit` application argument, or one conservative
    possible-instance argument at an application whose binder sequence cannot
    be recovered structurally.  The latter prevents an unexplained empty cache
    inventory; it never asserts that the argument was synthesized. -/
structure InstanceArgumentEvidence where
  applicationPath : Array Nat
  headKind : String
  headName : Option Name
  headExprHash : UInt64
  applicationExprHash : UInt64
  argumentIndex : Nat
  argumentExprHash : UInt64
  expectedTypeHash : Option UInt64
  declarationTypeHash : Option UInt64
  binderInfo : Option BinderInfo
  exactInstanceImplicit : Bool
  conservativePossibleInstance : Bool
  deriving Inhabited, Repr

private structure InstanceInventoryScan where
  evidence : Array InstanceArgumentEvidence := #[]
  structurallyComplete : Bool := true

private def InstanceInventoryScan.append
    (left right : InstanceInventoryScan) : InstanceInventoryScan := {
  evidence := left.evidence ++ right.evidence
  structurallyComplete := left.structurallyComplete && right.structurallyComplete
}

private partial def stripInstanceInventoryMData : Expr → Expr
  | .mdata _ body => stripInstanceInventoryMData body
  | e => e

private def fallbackPossibleInstances
    (applicationPath : Array Nat) (headKind : String) (headName : Option Name)
    (head application : Expr) (arguments : Array Expr) (startIndex : Nat)
    (declarationTypeHash : Option UInt64) : InstanceInventoryScan :=
  Id.run do
    let mut evidence := #[]
    for index in [startIndex : arguments.size] do
      evidence := evidence.push {
        applicationPath
        headKind
        headName
        headExprHash := hash head
        applicationExprHash := hash application
        argumentIndex := index
        argumentExprHash := hash arguments[index]!
        expectedTypeHash := none
        declarationTypeHash
        binderInfo := none
        exactInstanceImplicit := false
        conservativePossibleInstance := true
      }
    return { evidence, structurallyComplete := false }

/-- Walk a declaration/local type against the exact elaborated application
    arguments.  Dependent binder domains are instantiated in order. -/
private partial def scanAppliedTypeForInstances
    (applicationPath : Array Nat) (headKind : String) (headName : Option Name)
    (head application declarationType currentType : Expr)
    (arguments : Array Expr) (index : Nat := 0)
    (evidence : Array InstanceArgumentEvidence := #[]) : InstanceInventoryScan :=
  if index >= arguments.size then
    { evidence, structurallyComplete := true }
  else
    match stripInstanceInventoryMData currentType with
    | .forallE _ domain body binderInfo =>
        let argument := arguments[index]!
        let evidence :=
          if binderInfo == .instImplicit then
            evidence.push {
              applicationPath
              headKind
              headName
              headExprHash := hash head
              applicationExprHash := hash application
              argumentIndex := index
              argumentExprHash := hash argument
              expectedTypeHash := some (hash domain)
              declarationTypeHash := some (hash declarationType)
              binderInfo := some binderInfo
              exactInstanceImplicit := true
              conservativePossibleInstance := false
            }
          else
            evidence
        scanAppliedTypeForInstances applicationPath headKind headName head application
          declarationType (body.instantiate1 argument) arguments (index + 1) evidence
    | .letE _ _ value body _ =>
        scanAppliedTypeForInstances applicationPath headKind headName head application
          declarationType (body.instantiate1 value) arguments index evidence
    | _ =>
        let fallback := fallbackPossibleInstances applicationPath headKind headName
          head application arguments index (some (hash declarationType))
        { fallback with evidence := evidence ++ fallback.evidence }

private partial def scanAppliedLambdaForInstances
    (applicationPath : Array Nat) (head application current : Expr)
    (arguments : Array Expr) (index : Nat := 0)
    (evidence : Array InstanceArgumentEvidence := #[]) : InstanceInventoryScan :=
  if index >= arguments.size then
    { evidence, structurallyComplete := true }
  else
    match stripInstanceInventoryMData current with
    | .lam _ domain body binderInfo =>
        let argument := arguments[index]!
        let evidence :=
          if binderInfo == .instImplicit then
            evidence.push {
              applicationPath
              headKind := "lambda"
              headName := none
              headExprHash := hash head
              applicationExprHash := hash application
              argumentIndex := index
              argumentExprHash := hash argument
              expectedTypeHash := some (hash domain)
              declarationTypeHash := some (hash head)
              binderInfo := some binderInfo
              exactInstanceImplicit := true
              conservativePossibleInstance := false
            }
          else
            evidence
        scanAppliedLambdaForInstances applicationPath head application
          (body.instantiate1 argument) arguments (index + 1) evidence
    | .letE _ _ value body _ =>
        scanAppliedLambdaForInstances applicationPath head application
          (body.instantiate1 value) arguments index evidence
    | _ =>
        let fallback := fallbackPossibleInstances applicationPath "lambda" none
          head application arguments index (some (hash head))
        { fallback with evidence := evidence ++ fallback.evidence }

private partial def scanExprInstanceInventory
    (environment : Environment) (e : Expr) (path : Array Nat := #[]) :
    InstanceInventoryScan :=
  let core := stripInstanceInventoryMData e
  match core with
  | .app _ _ =>
      let head := core.getAppFn
      let arguments := core.getAppArgs
      let direct :=
        match head with
        | .const name levels =>
            match environment.find? name with
            | some constantInfo =>
                let declarationType := constantInfo.type.instantiateLevelParams
                  constantInfo.levelParams levels
                scanAppliedTypeForInstances path "constant" (some name) head core
                  declarationType declarationType arguments
            | none =>
                fallbackPossibleInstances path "missing_constant" (some name)
                  head core arguments 0 none
        | .bvar _ =>
            /- A raw de Bruijn declaration type must be lifted into the current
               binder depth before dependent domains can be interpreted.  This
               additive helper deliberately does not guess: every argument is
               retained as a conservative cache preimage instead. -/
            fallbackPossibleInstances path "bound_variable_conservative" none
              head core arguments 0 none
        | .lam _ _ _ _ =>
            scanAppliedLambdaForInstances path head core head arguments
        | _ =>
            fallbackPossibleInstances path "unclassified_head" none
              head core arguments 0 none
      Id.run do
        let mut result := direct
        let scanHeadInternals :=
          match head with
          | .const _ _ | .bvar _ => false
          | _ => true
        if scanHeadInternals then
          result := result.append
            (scanExprInstanceInventory environment head (path.push 0))
        for index in [0 : arguments.size] do
          result := result.append
            (scanExprInstanceInventory environment arguments[index]!
              (path.push (index + 1)))
        return result
  | .forallE _ domain body _ | .lam _ domain body _ =>
      (scanExprInstanceInventory environment domain (path.push 0)).append
        (scanExprInstanceInventory environment body (path.push 1))
  | .letE _ type value body _ =>
      (scanExprInstanceInventory environment type (path.push 0)).append
        ((scanExprInstanceInventory environment value (path.push 1)).append
          (scanExprInstanceInventory environment body (path.push 2)))
  | .proj _ _ base =>
      scanExprInstanceInventory environment base (path.push 0)
  | _ => {}

structure EndpointInstanceInventory where
  endpointRole : String
  endpointExprHash : UInt64
  checkedClosedProp : Bool
  structurallyComplete : Bool
  exactInstanceImplicitCount : Nat
  conservativePossibleInstanceCount : Nat
  evidence : Array InstanceArgumentEvidence
  emptyInventoryProved : Bool
  deriving Inhabited, Repr

private def buildEndpointInstanceInventory
    (environment : Environment) (endpointRole : String) (e : Expr) :
    MetaM EndpointInstanceInventory := do
  let checkedClosedProp ← checkedClosedPropEvidence e
  let scan := scanExprInstanceInventory environment e
  let exactInstanceImplicitCount :=
    (scan.evidence.filter (·.exactInstanceImplicit)).size
  let conservativePossibleInstanceCount :=
    (scan.evidence.filter (·.conservativePossibleInstance)).size
  let emptyInventoryProved := checkedClosedProp && scan.structurallyComplete &&
    scan.evidence.isEmpty
  return {
    endpointRole
    endpointExprHash := hash e
    checkedClosedProp
    structurallyComplete := scan.structurallyComplete
    exactInstanceImplicitCount
    conservativePossibleInstanceCount
    evidence := scan.evidence
    emptyInventoryProved
  }

structure SynthesizedInstanceInventoryReceipt where
  source : EndpointInstanceInventory
  candidate : EndpointInstanceInventory
  orderedCacheHashPreimageCount : Nat
  emptyInventoryProved : Bool
  cacheHashBasisAdequate : Bool
  deriving Inhabited, Repr

private def buildSynthesizedInstanceInventoryReceipt
    (source candidate : Expr) : MetaM SynthesizedInstanceInventoryReceipt := do
  let environment ← getEnv
  let sourceInventory ← buildEndpointInstanceInventory environment "source" source
  let candidateInventory ←
    buildEndpointInstanceInventory environment "candidate" candidate
  let count := sourceInventory.evidence.size + candidateInventory.evidence.size
  let emptyInventoryProved := count == 0 && sourceInventory.emptyInventoryProved &&
    candidateInventory.emptyInventoryProved
  let cacheHashBasisAdequate := sourceInventory.checkedClosedProp &&
    candidateInventory.checkedClosedProp && (count > 0 || emptyInventoryProved)
  return {
    source := sourceInventory
    candidate := candidateInventory
    orderedCacheHashPreimageCount := count
    emptyInventoryProved
    cacheHashBasisAdequate
  }

private def binderInfoJson (binderInfo : BinderInfo) : Json :=
  Json.str (binderInfoEvidenceTag binderInfo)

private def optionBinderInfoJson : Option BinderInfo → Json
  | none => Json.null
  | some binderInfo => binderInfoJson binderInfo

private def optionUInt64Json : Option UInt64 → Json
  | none => Json.null
  | some value => uint64Json value

private def stringArrayJson (values : Array String) : Json :=
  Json.arr (values.map Json.str)

private def natArrayJson (values : Array Nat) : Json :=
  Json.arr (values.map toJson)

private def failureReasonId : FailureReason → String
  | .sourceNotClosedProp => "sourceNotClosedProp"
  | .candidateNotClosedProp => "candidateNotClosedProp"
  | .selectorDoesNotMatchOperation => "selectorDoesNotMatchOperation"
  | .selectedSiteMissing => "selectedSiteMissing"
  | .operationNotApplicable => "operationNotApplicable"
  | .nonHygienicBinderName => "nonHygienicBinderName"
  | .degenerateOperands => "degenerateOperands"
  | .exactDeltaMismatch => "exactDeltaMismatch"
  | .expectedDefinitionalEqualityMissing => "expectedDefinitionalEqualityMissing"
  | .forbiddenDefinitionalEquality => "forbiddenDefinitionalEquality"
  | .claimCollapsesToTrue => "claimCollapsesToTrue"
  | .claimCollapsesToFalse => "claimCollapsesToFalse"
  | .n31BankMissing => "n31BankMissing"
  | .n31BankInvalid => "n31BankInvalid"
  | .n31BankEntryMissingOrAmbiguous => "n31BankEntryMissingOrAmbiguous"
  | .n31GuardMissingOrAmbiguous => "n31GuardMissingOrAmbiguous"
  | .n31GuardNotNamedExplicitProp => "n31GuardNotNamedExplicitProp"
  | .n31GuardDefinitionallyTrue => "n31GuardDefinitionallyTrue"
  | .n31GuardProofUsedInContinuation => "n31GuardProofUsedInContinuation"
  | .n31TargetMissingOrAmbiguous => "n31TargetMissingOrAmbiguous"
  | .n31CompetingGuard => "n31CompetingGuard"
  | .n31RetainedContradiction => "n31RetainedContradiction"
  | .n31RetainedContextUnknownOrAmbiguous => "n31RetainedContextUnknownOrAmbiguous"
  | .n31ReachabilityMissing => "n31ReachabilityMissing"
  | .n31ReachabilityInvalid => "n31ReachabilityInvalid"
  | .n31BodyDefinitionallyTrue => "n31BodyDefinitionallyTrue"
  | .replayContextMismatch => "replayContextMismatch"
  | .replayCertificateMismatch => "replayCertificateMismatch"
  | .replayCandidateMismatch => "replayCandidateMismatch"

private def optionFailureReasonJson : Option FailureReason → Json
  | none => Json.null
  | some reason => Json.str (failureReasonId reason)

private def selectorJson : Selector → Json
  | .outerBinder ordinal => Json.mkObj [
      ("kind", Json.str "outerBinder"),
      ("ordinal", toJson ordinal)
    ]
  | .outerTarget => Json.mkObj [("kind", Json.str "outerTarget")]
  | .subexpr pos => Json.mkObj [
      ("kind", Json.str "subexpr"),
      ("position", toJson pos),
      ("position_nat", Json.str (toString pos.asNat))
    ]
  | .requiredGuard guardOrdinal targetPos bankEntryId => Json.mkObj [
      ("kind", Json.str "requiredGuard"),
      ("guard_ordinal", toJson guardOrdinal),
      ("target_position", toJson targetPos),
      ("target_position_nat", Json.str (toString targetPos.asNat)),
      ("bank_entry_id", Json.str bankEntryId)
    ]

private def certificateJson : Certificate → Json
  | .p01 value => Json.mkObj [
      ("kind", Json.str "p01"),
      ("binder_ordinal", toJson value.binderOrdinal),
      ("binder_site", toJson value.binderSite),
      ("binder_site_nat", Json.str (toString value.binderSite.asNat)),
      ("source_name", nameJson value.sourceName),
      ("candidate_name", nameJson value.candidateName),
      ("binder_info", binderInfoJson value.binderInfo)
    ]
  | .p15 value => Json.mkObj [
      ("kind", Json.str "p15"),
      ("target_site", toJson value.targetSite),
      ("target_site_nat", Json.str (toString value.targetSite.asNat))
    ]
  | .p18 value => Json.mkObj [
      ("kind", Json.str "p18"),
      ("target_site", toJson value.targetSite),
      ("target_site_nat", Json.str (toString value.targetSite.asNat))
    ]
  | .p21 value => Json.mkObj [
      ("kind", Json.str "p21"),
      ("redex_site", toJson value.redexSite),
      ("redex_site_nat", Json.str (toString value.redexSite.asNat))
    ]
  | .n31Rubric value => Json.mkObj [
      ("kind", Json.str "n31Rubric_forbidden_on_positive_surface"),
      ("guard_ordinal", toJson value.guardOrdinal),
      ("guard_site", toJson value.guardSite),
      ("target_site", toJson value.targetSite),
      ("bank_entry_id", Json.str value.bankEntryId),
      ("guard_shape_id", Json.str value.guardShapeId)
    ]

private def replayResultJson (result : ReplayResult) : Json :=
  Json.mkObj [
    ("passed", Json.bool result.passed),
    ("operation_id", Json.str result.operation.operationId),
    ("reason", optionFailureReasonJson result.reason)
  ]

private def p01DeltaReceiptJson (receipt : P01ExactDeltaReceipt) : Json :=
  Json.mkObj [
    ("operation_id", Json.str receipt.operationId),
    ("source_expr_hash", exprHashJson receipt.source),
    ("candidate_expr_hash", exprHashJson receipt.candidate),
    ("binder_ordinal", toJson receipt.certificate.binderOrdinal),
    ("binder_site", toJson receipt.certificate.binderSite),
    ("binder_site_nat", Json.str (toString receipt.certificate.binderSite.asNat)),
    ("source_name", nameJson receipt.certificate.sourceName),
    ("candidate_name", nameJson receipt.certificate.candidateName),
    ("binder_info", binderInfoJson receipt.certificate.binderInfo),
    ("source_domain_hash", exprHashJson receipt.sourceFingerprint.domain),
    ("candidate_domain_hash", exprHashJson receipt.candidateFingerprint.domain),
    ("source_body_hash", exprHashJson receipt.sourceFingerprint.body),
    ("candidate_body_hash", exprHashJson receipt.candidateFingerprint.body),
    ("binder_site_matches_certificate", Json.bool receipt.binderSiteMatchesCertificate),
    ("source_name_matches_certificate", Json.bool receipt.sourceNameMatchesCertificate),
    ("candidate_name_matches_certificate", Json.bool receipt.candidateNameMatchesCertificate),
    ("binder_info_matches_certificate", Json.bool receipt.binderInfoMatchesCertificate),
    ("names_differ", Json.bool receipt.namesDiffer),
    ("domains_exactly_equal", Json.bool receipt.domainsExactlyEqual),
    ("bodies_exactly_equal", Json.bool receipt.bodiesExactlyEqual),
    ("source_candidate_alpha_equivalent", Json.bool receipt.sourceCandidateAlphaEquivalent),
    ("source_candidate_exactly_different", Json.bool receipt.sourceCandidateExactlyDifferent),
    ("deterministic_candidate_replay_exact",
      Json.bool receipt.deterministicCandidateReplayExact),
    ("frozen_certificate_replay_passed",
      Json.bool receipt.frozenCertificateReplayPassed),
    ("exact_name_only_delta_passed", Json.bool receipt.exactNameOnlyDeltaPassed)
  ]

def synthesizedInstanceInventoryBasisId : String :=
  "sft1_wave1_typed_inst_implicit_inventory_v0_3_6"

private def instanceArgumentEvidenceJson
    (endpointRole : String) (evidence : InstanceArgumentEvidence) : Json :=
  Json.mkObj [
    ("basis_id", Json.str synthesizedInstanceInventoryBasisId),
    ("endpoint_role", Json.str endpointRole),
    ("application_path", natArrayJson evidence.applicationPath),
    ("path_semantics",
      Json.str "flattened_app_head_0_args_i_plus_1_other_expr_children_v1"),
    ("head_kind", Json.str evidence.headKind),
    ("head_name",
      match evidence.headName with
      | none => Json.null
      | some name => nameJson name),
    ("head_expr_hash", uint64Json evidence.headExprHash),
    ("application_expr_hash", uint64Json evidence.applicationExprHash),
    ("argument_index", toJson evidence.argumentIndex),
    ("argument_expr_hash", uint64Json evidence.argumentExprHash),
    ("expected_type_hash", optionUInt64Json evidence.expectedTypeHash),
    ("declaration_type_hash", optionUInt64Json evidence.declarationTypeHash),
    ("binder_info", optionBinderInfoJson evidence.binderInfo),
    ("typing_evidence_class", Json.str
      (if evidence.exactInstanceImplicit then
        "instImplicit_binder_with_instantiated_expected_type"
      else
        "checked_closed_prop_endpoint_conservative_application_argument")),
    ("exact_instance_implicit", Json.bool evidence.exactInstanceImplicit),
    ("conservative_possible_instance",
      Json.bool evidence.conservativePossibleInstance)
  ]

private def endpointInstanceInventoryJson
    (inventory : EndpointInstanceInventory) : Json :=
  Json.mkObj [
    ("endpoint_role", Json.str inventory.endpointRole),
    ("endpoint_expr_hash", uint64Json inventory.endpointExprHash),
    ("checked_closed_prop", Json.bool inventory.checkedClosedProp),
    ("structurally_complete", Json.bool inventory.structurallyComplete),
    ("exact_instance_implicit_count", toJson inventory.exactInstanceImplicitCount),
    ("conservative_possible_instance_count",
      toJson inventory.conservativePossibleInstanceCount),
    ("evidence_count", toJson inventory.evidence.size),
    ("empty_inventory_proved", Json.bool inventory.emptyInventoryProved),
    ("evidence", Json.arr (inventory.evidence.map
      (instanceArgumentEvidenceJson inventory.endpointRole)))
  ]

private def synthesizedInstanceInventoryJson
    (receipt : SynthesizedInstanceInventoryReceipt) : Json :=
  let sourcePreimages := receipt.source.evidence.map
    (instanceArgumentEvidenceJson receipt.source.endpointRole)
  let candidatePreimages := receipt.candidate.evidence.map
    (instanceArgumentEvidenceJson receipt.candidate.endpointRole)
  Json.mkObj [
    ("basis_id", Json.str synthesizedInstanceInventoryBasisId),
    ("structural_expr_fingerprint_id", Json.str structuralExprFingerprintId),
    ("hash_input_contract",
      Json.str "python_sha256_canonical_json_per_ordered_item_v1"),
    ("ordering", Json.str "source_preorder_then_candidate_preorder_v1"),
    ("scope",
      Json.str "all_exact_instImplicit_arguments_plus_conservative_unclassified_arguments"),
    ("source", endpointInstanceInventoryJson receipt.source),
    ("candidate", endpointInstanceInventoryJson receipt.candidate),
    ("ordered_cache_hash_preimage_count", toJson receipt.orderedCacheHashPreimageCount),
    ("ordered_cache_hash_preimages", Json.arr (sourcePreimages ++ candidatePreimages)),
    ("empty_inventory_proved", Json.bool receipt.emptyInventoryProved),
    ("cache_hash_basis_adequate", Json.bool receipt.cacheHashBasisAdequate)
  ]

private def emitReceiptJson (payload : Json) : MetaM Unit := do
  IO.println s!"{receiptMarker}{payload.compress}"

structure PositiveRediscoveryReceipt where
  discoveredCandidateCount : Nat
  selectedSelectorCount : Nat
  selectedCandidateAndCertificateExactCount : Nat
  selectedSiteUniquelyRediscovered : Bool
  deriving Inhabited, Repr

private def rediscoverPositiveCandidate
    (bundle : PositiveBundle) (source : Expr) (selected : LeanFaith.SFT1.Wave1.Candidate) :
    MetaM PositiveRediscoveryReceipt := do
  let discovered ← discoverPositive bundle source
  let mut selectedSelectorCount := 0
  let mut selectedCandidateAndCertificateExactCount := 0
  for candidate in discovered do
    if candidate.selector == selected.selector then
      selectedSelectorCount := selectedSelectorCount + 1
      if candidate.operation == selected.operation &&
          Expr.equal candidate.candidate selected.candidate &&
          candidate.certificate == selected.certificate then
        selectedCandidateAndCertificateExactCount :=
          selectedCandidateAndCertificateExactCount + 1
  return {
    discoveredCandidateCount := discovered.size
    selectedSelectorCount
    selectedCandidateAndCertificateExactCount
    selectedSiteUniquelyRediscovered :=
      selectedSelectorCount == 1 && selectedCandidateAndCertificateExactCount == 1
  }

/-- Execute one positive dispatch, independently replay its certificate,
    dispatch again to bind deterministic equality, emit a compact receipt,
    and return the already-live candidate for the caller's two direct REPR
    endpoint calls.  No Expr is serialized or pretty-printed here. -/
def emitPositiveSuccessReceipt
    (receiptId : String) (bundle : PositiveBundle) (selector : Selector)
    (source : Expr) : MetaM LeanFaith.SFT1.Wave1.Candidate := do
  let first ← dispatchPositiveAt bundle selector source
  let candidate ←
    match first with
    | .applicable candidate => pure candidate
    | .typedNotApplicable reason =>
        throwError m!"sft1_positive_success_fixture_rejected: {failureReasonId reason}"
  let replay ← replayPositiveCertificate bundle source candidate.candidate candidate.certificate
  let second ← dispatchPositiveAt bundle selector source
  let (deterministicCandidateEquality, deterministicCertificateEquality) :=
    match second with
    | .applicable replayed =>
        (Expr.equal replayed.candidate candidate.candidate,
          replayed.certificate == candidate.certificate)
    | .typedNotApplicable _ => (false, false)
  let operationMatches := candidate.operation == bundle.operation
  let selectorMatches := candidate.selector == selector
  let rediscovery ← rediscoverPositiveCandidate bundle source candidate
  let instanceInventory ←
    buildSynthesizedInstanceInventoryReceipt source candidate.candidate
  let p01Delta ←
    match candidate.certificate with
    | .p01 certificate => do
        let receipt ← p01ExactDeltaReceipt source candidate.candidate certificate
        pure (p01DeltaReceiptJson receipt)
    | _ => pure Json.null
  unless replay.passed && deterministicCandidateEquality &&
      deterministicCertificateEquality && operationMatches && selectorMatches &&
      rediscovery.selectedSiteUniquelyRediscovered &&
      instanceInventory.cacheHashBasisAdequate do
    throwError "sft1_positive_success_receipt_replay_failed"
  emitReceiptJson <| Json.mkObj [
    ("schema_version", toJson 1),
    ("receipt_kind", Json.str "positive_success"),
    ("receipt_id", Json.str receiptId),
    ("source_version", Json.str sourceVersion),
    ("operation_id", Json.str bundle.operationId),
    ("selector", selectorJson candidate.selector),
    ("certificate", certificateJson candidate.certificate),
    ("source_expr_hash", exprHashJson source),
    ("candidate_expr_hash", exprHashJson candidate.candidate),
    ("structural_expr_fingerprint_id", Json.str structuralExprFingerprintId),
    ("operation_matches", Json.bool operationMatches),
    ("selector_matches", Json.bool selectorMatches),
    ("frozen_replay", replayResultJson replay.frozenReplay),
    ("certificate_constructor_matches", Json.bool replay.certificateConstructorMatches),
    ("deterministic_candidate_equality", Json.bool deterministicCandidateEquality),
    ("deterministic_certificate_equality", Json.bool deterministicCertificateEquality),
    ("discovered_candidate_count", toJson rediscovery.discoveredCandidateCount),
    ("selected_selector_rediscovery_count", toJson rediscovery.selectedSelectorCount),
    ("selected_candidate_and_certificate_exact_count",
      toJson rediscovery.selectedCandidateAndCertificateExactCount),
    ("selected_site_uniquely_rediscovered",
      Json.bool rediscovery.selectedSiteUniquelyRediscovered),
    ("synthesized_instance_inventory",
      synthesizedInstanceInventoryJson instanceInventory),
    ("p01_exact_delta", p01Delta),
    ("candidate_exposed_to_caller_for_same_request_repr", Json.bool true),
    ("row_or_gate_emitted", Json.bool false)
  ]
  return candidate

/-- Execute one positive dispatch expected to reject and emit its exact
    typed-not-applicable reason.  No candidate is returned or serialized. -/
def emitPositiveRejectionReceipt
    (receiptId : String) (bundle : PositiveBundle) (selector : Selector)
    (source : Expr) : MetaM Unit := do
  match ← dispatchPositiveAt bundle selector source with
  | .applicable _ =>
      throwError "sft1_positive_rejection_fixture_unexpectedly_applicable"
  | .typedNotApplicable reason =>
      emitReceiptJson <| Json.mkObj [
        ("schema_version", toJson 1),
        ("receipt_kind", Json.str "positive_typed_not_applicable"),
        ("receipt_id", Json.str receiptId),
        ("source_version", Json.str sourceVersion),
        ("operation_id", Json.str bundle.operationId),
        ("selector", selectorJson selector),
        ("source_expr_hash", exprHashJson source),
        ("structural_expr_fingerprint_id", Json.str structuralExprFingerprintId),
        ("terminal", Json.str "typedNotApplicable"),
        ("reason", Json.str (failureReasonId reason)),
        ("candidate_constructed", Json.bool false),
        ("candidate_serialized", Json.bool false),
        ("row_or_gate_emitted", Json.bool false)
      ]

/-! ## N31 proposal-only resolution surface -/

def n31OperationId : String :=
  LeanFaith.SFT1.Wave1.PrimaryOperation.n31DropRequiredGuardRubric.operationId

def n31FrozenAdmissionIdentities : Array LeanFaith.SFT1.Wave1.N31BankIdentity :=
  LeanFaith.SFT1.Wave1.admittedN31BankIdentitiesV0_3_4

def n31FrozenAdmissionIsEmpty : Bool := n31FrozenAdmissionIdentities.isEmpty

/-- The frozen semantic bank validator and N31 applicator are private to
    `Wave1.lean`.  They cannot be called from this additive source.  More
    importantly, their public dispatch path checks the frozen empty admission
    before semantic matching.  This marker is hash-bound into proposal
    receipts so a name/arity resolution cannot be misreported as semantic
    conformance. -/
def n31PrivateSemanticCheckerAvailableToAdditiveRuntime : Bool := false

def n31ProposalActivationExposed : Bool := false
def n31ProposalCandidateExposed : Bool := false
def gateExecutionExposed : Bool := false
def productionAdmissionExposed : Bool := false
def rowEmissionExposed : Bool := false

structure N31ResolvedHead where
  name : Name
  expectedArgumentCount : Nat
  observedArgumentCount : Option Nat
  declarationType : Option Expr
  argumentBinderInfos : Array BinderInfo
  declarationFound : Bool
  declarationTypeClosed : Bool
  arityMatches : Bool
  deriving Inhabited, Repr

structure N31EntryResolution where
  entryId : String
  guardShapeId : String
  guardHead : N31ResolvedHead
  targetHead : N31ResolvedHead
  guardFixedHeads : Array N31ResolvedHead
  targetFixedHeads : Array N31ResolvedHead
  guardNestedHeads : Array N31ResolvedHead
  targetNestedHeads : Array N31ResolvedHead
  guardRoleIndicesInRange : Bool
  targetRoleIndicesInRange : Bool
  guardInstanceIndicesInRange : Bool
  targetInstanceIndicesInRange : Bool
  guardRoleBinderInfosMatch : Bool
  targetRoleBinderInfosMatch : Bool
  guardInstanceBinderInfosMatch : Bool
  targetInstanceBinderInfosMatch : Bool
  guardRoleObservedBinderInfoTags : Array String
  targetRoleObservedBinderInfoTags : Array String
  guardInstanceOrTypeObservedBinderInfoTags : Array String
  targetInstanceOrTypeObservedBinderInfoTags : Array String
  structuralShapeResolved : Bool
  exactConstraintTermsClosedAndTyped : Bool
  passed : Bool
  deriving Inhabited, Repr

structure N31RetainedPatternWitness where
  shapeId : String
  proposition : Expr
  deriving Inhabited, Repr

structure N31ApplicationPathStepResolution where
  headName : Name
  observedArgumentCount : Nat
  selectedArgumentIndex : Nat
  selectedArgumentBinderInfo : Option BinderInfo
  selectedArgumentBinderInfoTag : Option String
  declarationTypeHash : UInt64
  selectedExprHash : UInt64
  selectedTypeHash : Option UInt64
  passed : Bool
  deriving Inhabited, Repr

structure N31ApplicationPathResolution where
  path : LeanFaith.SFT1.Wave1.N31ApplicationArgumentPath
  expectedRoleExplicit : Bool
  steps : Array N31ApplicationPathStepResolution
  selectedExprHash : Option UInt64
  selectedTypeHash : Option UInt64
  selectedBinderInfo : Option BinderInfo
  selectedBinderInfoTag : Option String
  pathResolved : Bool
  binderInfoClassMatches : Bool
  passed : Bool
  deriving Inhabited, Repr

structure N31NestedHeadWitnessResolution where
  path : LeanFaith.SFT1.Wave1.N31ApplicationArgumentPath
  expectedHeadName : Name
  expectedArgumentCount : Nat
  selectedExprHash : Option UInt64
  observedHeadName : Option Name
  observedArgumentCount : Option Nat
  pathResolved : Bool
  headMatches : Bool
  arityMatches : Bool
  passed : Bool
  deriving Inhabited, Repr

structure N31LiteralWitnessResolution where
  path : LeanFaith.SFT1.Wave1.N31ApplicationArgumentPath
  expected : Literal
  selectedExprHash : Option UInt64
  observed : Option Literal
  pathResolved : Bool
  literalMatches : Bool
  passed : Bool
  deriving Inhabited, Repr

structure N31ExactExprWitnessResolution where
  path : LeanFaith.SFT1.Wave1.N31ApplicationArgumentPath
  expectedExprHash : UInt64
  selectedExprHash : Option UInt64
  pathResolved : Bool
  exactExprMatches : Bool
  passed : Bool
  deriving Inhabited, Repr

structure N31RetainedPatternResolution where
  shapeId : String
  head : N31ResolvedHead
  nestedHeads : Array N31ResolvedHead
  witnessFoundUniquely : Bool
  witnessExprHash : Option UInt64
  witnessTypeHash : Option UInt64
  witnessIsTypedProp : Bool
  rootHeadMatches : Bool
  rootArityMatches : Bool
  rolePathResolutions : Array N31ApplicationPathResolution
  instanceOrTypePathResolutions : Array N31ApplicationPathResolution
  nestedHeadWitnessResolutions : Array N31NestedHeadWitnessResolution
  literalWitnessResolutions : Array N31LiteralWitnessResolution
  exactExprWitnessResolutions : Array N31ExactExprWitnessResolution
  pathsNonempty : Bool
  constraintWitnessReplaysPassed : Bool
  structuralShapeResolved : Bool
  exactConstraintTermsClosedAndTyped : Bool
  passed : Bool
  deriving Inhabited, Repr

structure N31ProposalResolutionReceipt where
  operationId : String
  bank : N31TargetBank
  identityProjectAndBankNonempty : Bool
  resolvedLeanHashPopulated : Bool
  resolutionReceiptHashPopulated : Bool
  entryIdsUnique : Bool
  retainedShapeIdsUnique : Bool
  entries : Array N31EntryResolution
  retainedPatterns : Array N31RetainedPatternResolution
  selectableGuardDefinitionsCoherent : Bool
  retainedShapesDisjointFromSelectable : Bool
  implicationReferencesResolve : Bool
  contradictionReferencesResolve : Bool
  allEntryResolutionsPassed : Bool
  allRetainedPatternResolutionsPassed : Bool
  allNamesResolved : Bool
  allAritiesResolved : Bool
  allTypeAndInstanceConstraintsResolved : Bool
  frozenAdmissionIsEmpty : Bool
  proposedIdentityAlreadyAdmitted : Bool
  privateSemanticCheckerAvailable : Bool
  semanticSuccessConformancePerformed : Bool
  semanticAdversarialConformancePerformed : Bool
  activationExposed : Bool
  proposalResolutionPassed : Bool
  deriving Inhabited, Repr

/-- Identifies the sole canonical JSON preimage emitted below. -/
def n31ResolutionReceiptHashBasisId : String :=
  "sft1_n31_resolution_receipt_hash_preimage_v0_3_6"

def n31StructuralBankFingerprintBasisId : String :=
  "sft1_n31_structural_bank_fingerprint_payload_v0_3_6"

private partial def outerBinderInfos : Expr → Array BinderInfo
  | .mdata _ body => outerBinderInfos body
  | .forallE _ _ body binderInfo => #[binderInfo] ++ outerBinderInfos body
  | _ => #[]

private def resolveHead
    (environment : Environment) (name : Name) (expectedArgumentCount : Nat) : N31ResolvedHead :=
  match environment.find? name with
  | none => {
      name
      expectedArgumentCount
      observedArgumentCount := none
      declarationType := none
      argumentBinderInfos := #[]
      declarationFound := false
      declarationTypeClosed := false
      arityMatches := false
    }
  | some constantInfo =>
      let argumentBinderInfos := outerBinderInfos constantInfo.type
      let observed := argumentBinderInfos.size
      {
        name
        expectedArgumentCount
        observedArgumentCount := some observed
        declarationType := some constantInfo.type
        argumentBinderInfos
        declarationFound := true
        declarationTypeClosed := cleanClosedExpr constantInfo.type
        arityMatches := observed == expectedArgumentCount
      }

private def closedAndTyped (e : Expr) : MetaM Bool := do
  if !cleanClosedExpr e then
    return false
  try
    check e
    return true
  catch ex =>
    if ex.isInterrupt || ex.isRuntime then
      throw ex
    return false

private def exactConstraintsClosedAndTyped
    (constraints : Array LeanFaith.SFT1.Wave1.N31ExactExprConstraint) : MetaM Bool := do
  for constraint in constraints do
    if !(← closedAndTyped constraint.expected) then
      return false
  return true

private def indicesInRange (indices : Array Nat) (argumentCount : Nat) : Bool :=
  indices.all (· < argumentCount)

private def natArrayUnique (values : Array Nat) : Bool :=
  values.toList.eraseDups.length == values.size

private def natArraysDisjoint (left right : Array Nat) : Bool :=
  left.all fun value => !right.contains value

private def applicationPathArrayUnique
    (paths : Array LeanFaith.SFT1.Wave1.N31ApplicationArgumentPath) : Bool :=
  paths.toList.eraseDups.length == paths.size

private def applicationPathArraysDisjoint
    (left right : Array LeanFaith.SFT1.Wave1.N31ApplicationArgumentPath) : Bool :=
  left.all fun path => !right.contains path

private def fixedHeadsStructurallyResolved
    (argumentCount : Nat)
    (constraints : Array LeanFaith.SFT1.Wave1.N31FixedHeadConstraint) : Bool :=
  natArrayUnique (constraints.map (·.argumentIndex)) &&
    constraints.all fun constraint =>
      constraint.argumentIndex < argumentCount && !constraint.headName.isAnonymous

private def nestedHeadsStructurallyResolved
    (constraints : Array LeanFaith.SFT1.Wave1.N31NestedHeadConstraint) : Bool :=
  applicationPathArrayUnique (constraints.map (·.path)) &&
    constraints.all fun constraint =>
      !constraint.path.argumentIndices.isEmpty && !constraint.headName.isAnonymous

private def literalConstraintsStructurallyResolved
    (constraints : Array LeanFaith.SFT1.Wave1.N31LiteralConstraint) : Bool :=
  applicationPathArrayUnique (constraints.map (·.path)) &&
    constraints.all fun constraint => !constraint.path.argumentIndices.isEmpty

private def exactConstraintsStructurallyResolved
    (constraints : Array LeanFaith.SFT1.Wave1.N31ExactExprConstraint) : Bool :=
  applicationPathArrayUnique (constraints.map (·.path)) &&
    constraints.all fun constraint => !constraint.path.argumentIndices.isEmpty

private def selectBinderInfos?
    (infos : Array BinderInfo) (indices : Array Nat) : Option (Array BinderInfo) := do
  let mut selected := #[]
  for index in indices do
    if index >= infos.size then
      none
    selected := selected.push infos[index]!
  return selected

private def binderInfoTags (infos : Array BinderInfo) : Array String :=
  infos.map binderInfoEvidenceTag

private def roleBinderInfosMatch (infos : Array BinderInfo) : Bool :=
  !infos.isEmpty && infos.all (· == .default)

/-- The frozen bank field name says `Instance`, but its policy meaning is
    instance-or-type context: implicit, strict-implicit, and instance-implicit
    arguments are accepted; explicit data-role arguments are not. -/
private def instanceOrTypeBinderInfosMatch (infos : Array BinderInfo) : Bool :=
  !infos.isEmpty && infos.all (· != .default)

private def rootBinderInfos?
    (environment : Environment) (name : Name) : Option (Array BinderInfo) := do
  let constantInfo ← environment.find? name
  return outerBinderInfos constantInfo.type

private def resolveFixedHeads
    (environment : Environment)
    (constraints : Array LeanFaith.SFT1.Wave1.N31FixedHeadConstraint) : Array N31ResolvedHead :=
  constraints.map fun constraint =>
    resolveHead environment constraint.headName constraint.argumentCount

private def resolveNestedHeads
    (environment : Environment)
    (constraints : Array LeanFaith.SFT1.Wave1.N31NestedHeadConstraint) : Array N31ResolvedHead :=
  constraints.map fun constraint =>
    resolveHead environment constraint.headName constraint.argumentCount

private def allHeadsFound (heads : Array N31ResolvedHead) : Bool :=
  heads.all fun head => head.declarationFound && head.declarationTypeClosed

private def allHeadAritiesMatch (heads : Array N31ResolvedHead) : Bool :=
  heads.all (·.arityMatches)

private def resolveEntry
    (environment : Environment) (entry : N31TargetBankEntry) : MetaM N31EntryResolution := do
  let guardHead := resolveHead environment entry.guardHeadName entry.guardArgumentCount
  let targetHead := resolveHead environment entry.targetHeadName entry.targetArgumentCount
  let guardFixedHeads := resolveFixedHeads environment entry.guardFixedHeads
  let targetFixedHeads := resolveFixedHeads environment entry.targetFixedHeads
  let guardNestedHeads := resolveNestedHeads environment entry.guardNestedHeadConstraints
  let targetNestedHeads := resolveNestedHeads environment entry.targetNestedHeadConstraints
  let guardRoleIndicesInRange :=
    indicesInRange entry.guardRoleArgumentIndices entry.guardArgumentCount
  let targetRoleIndicesInRange :=
    indicesInRange entry.targetRoleArgumentIndices entry.targetArgumentCount
  let guardInstanceIndicesInRange :=
    indicesInRange entry.guardInstanceArgumentIndices entry.guardArgumentCount
  let targetInstanceIndicesInRange :=
    indicesInRange entry.targetInstanceArgumentIndices entry.targetArgumentCount
  let guardInfos := (rootBinderInfos? environment entry.guardHeadName).getD #[]
  let targetInfos := (rootBinderInfos? environment entry.targetHeadName).getD #[]
  let guardRoleObservedBinderInfos :=
    (selectBinderInfos? guardInfos entry.guardRoleArgumentIndices).getD #[]
  let targetRoleObservedBinderInfos :=
    (selectBinderInfos? targetInfos entry.targetRoleArgumentIndices).getD #[]
  let guardInstanceOrTypeObservedBinderInfos :=
    (selectBinderInfos? guardInfos entry.guardInstanceArgumentIndices).getD #[]
  let targetInstanceOrTypeObservedBinderInfos :=
    (selectBinderInfos? targetInfos entry.targetInstanceArgumentIndices).getD #[]
  let guardRoleBinderInfosMatch :=
    guardRoleObservedBinderInfos.size == entry.guardRoleArgumentIndices.size &&
      roleBinderInfosMatch guardRoleObservedBinderInfos
  let targetRoleBinderInfosMatch :=
    targetRoleObservedBinderInfos.size == entry.targetRoleArgumentIndices.size &&
      roleBinderInfosMatch targetRoleObservedBinderInfos
  let guardInstanceBinderInfosMatch :=
    guardInstanceOrTypeObservedBinderInfos.size == entry.guardInstanceArgumentIndices.size &&
      instanceOrTypeBinderInfosMatch guardInstanceOrTypeObservedBinderInfos
  let targetInstanceBinderInfosMatch :=
    targetInstanceOrTypeObservedBinderInfos.size == entry.targetInstanceArgumentIndices.size &&
      instanceOrTypeBinderInfosMatch targetInstanceOrTypeObservedBinderInfos
  let guardRoleObservedBinderInfoTags := binderInfoTags guardRoleObservedBinderInfos
  let targetRoleObservedBinderInfoTags := binderInfoTags targetRoleObservedBinderInfos
  let guardInstanceOrTypeObservedBinderInfoTags :=
    binderInfoTags guardInstanceOrTypeObservedBinderInfos
  let targetInstanceOrTypeObservedBinderInfoTags :=
    binderInfoTags targetInstanceOrTypeObservedBinderInfos
  let structuralShapeResolved :=
    !entry.entryId.isEmpty && !entry.guardShapeId.isEmpty &&
      !entry.guardHeadName.isAnonymous && !entry.targetHeadName.isAnonymous &&
      !entry.guardRoleArgumentIndices.isEmpty &&
      !entry.guardInstanceArgumentIndices.isEmpty &&
      !entry.targetInstanceArgumentIndices.isEmpty &&
      entry.guardRoleArgumentIndices.size == entry.targetRoleArgumentIndices.size &&
      entry.guardInstanceArgumentIndices.size == entry.targetInstanceArgumentIndices.size &&
      natArrayUnique entry.guardRoleArgumentIndices &&
      natArrayUnique entry.guardInstanceArgumentIndices &&
      natArrayUnique entry.targetRoleArgumentIndices &&
      natArrayUnique entry.targetInstanceArgumentIndices &&
      natArraysDisjoint entry.guardRoleArgumentIndices entry.guardInstanceArgumentIndices &&
      natArraysDisjoint entry.targetRoleArgumentIndices entry.targetInstanceArgumentIndices &&
      fixedHeadsStructurallyResolved entry.guardArgumentCount entry.guardFixedHeads &&
      fixedHeadsStructurallyResolved entry.targetArgumentCount entry.targetFixedHeads &&
      nestedHeadsStructurallyResolved entry.guardNestedHeadConstraints &&
      nestedHeadsStructurallyResolved entry.targetNestedHeadConstraints &&
      literalConstraintsStructurallyResolved entry.guardLiteralConstraints &&
      literalConstraintsStructurallyResolved entry.targetLiteralConstraints &&
      exactConstraintsStructurallyResolved entry.guardExactExprConstraints &&
      exactConstraintsStructurallyResolved entry.targetExactExprConstraints
  let guardExact ← exactConstraintsClosedAndTyped entry.guardExactExprConstraints
  let targetExact ← exactConstraintsClosedAndTyped entry.targetExactExprConstraints
  let exactConstraintTermsClosedAndTyped := guardExact && targetExact
  let heads := #[guardHead, targetHead] ++ guardFixedHeads ++ targetFixedHeads ++
    guardNestedHeads ++ targetNestedHeads
  let passed := structuralShapeResolved && allHeadsFound heads && allHeadAritiesMatch heads &&
    guardRoleIndicesInRange && targetRoleIndicesInRange &&
    guardInstanceIndicesInRange && targetInstanceIndicesInRange &&
    guardRoleBinderInfosMatch && targetRoleBinderInfosMatch &&
    guardInstanceBinderInfosMatch && targetInstanceBinderInfosMatch &&
    exactConstraintTermsClosedAndTyped
  return {
    entryId := entry.entryId
    guardShapeId := entry.guardShapeId
    guardHead
    targetHead
    guardFixedHeads
    targetFixedHeads
    guardNestedHeads
    targetNestedHeads
    guardRoleIndicesInRange
    targetRoleIndicesInRange
    guardInstanceIndicesInRange
    targetInstanceIndicesInRange
    guardRoleBinderInfosMatch
    targetRoleBinderInfosMatch
    guardInstanceBinderInfosMatch
    targetInstanceBinderInfosMatch
    guardRoleObservedBinderInfoTags
    targetRoleObservedBinderInfoTags
    guardInstanceOrTypeObservedBinderInfoTags
    targetInstanceOrTypeObservedBinderInfoTags
    structuralShapeResolved
    exactConstraintTermsClosedAndTyped
    passed
  }

private def pathNonempty
    (path : LeanFaith.SFT1.Wave1.N31ApplicationArgumentPath) : Bool :=
  !path.argumentIndices.isEmpty

private partial def stripMData : Expr → Expr
  | .mdata _ body => stripMData body
  | e => e

private structure N31ApplicationView where
  headName : Name
  arguments : Array Expr

private def applicationView? (e : Expr) : Option N31ApplicationView :=
  let core := stripMData e
  match core.getAppFn with
  | .const name _ => some { headName := name, arguments := core.getAppArgs }
  | _ => none

private def inferTypeHash? (e : Expr) : MetaM (Option UInt64) := do
  try
    let type ← inferType e
    return some (hash type)
  catch ex =>
    if ex.isInterrupt || ex.isRuntime then
      throw ex
    return none

private partial def resolveApplicationPathAux
    (environment : Environment) (current : Expr) (indices : Array Nat)
    (cursor : Nat) (steps : Array N31ApplicationPathStepResolution) :
    MetaM (Array N31ApplicationPathStepResolution × Option Expr × Option BinderInfo × Bool) := do
  if cursor >= indices.size then
    return (steps, some current, none, true)
  let some application := applicationView? current
    | return (steps, none, none, false)
  let some constantInfo := environment.find? application.headName
    | return (steps, none, none, false)
  let binderInfos := outerBinderInfos constantInfo.type
  let argumentIndex := indices[cursor]!
  if argumentIndex >= application.arguments.size || argumentIndex >= binderInfos.size then
    return (steps, none, none, false)
  let selected := application.arguments[argumentIndex]!
  let selectedBinderInfo := binderInfos[argumentIndex]!
  let selectedTypeHash ← inferTypeHash? selected
  let stepPassed := application.arguments.size == binderInfos.size && selectedTypeHash.isSome
  let step : N31ApplicationPathStepResolution := {
    headName := application.headName
    observedArgumentCount := application.arguments.size
    selectedArgumentIndex := argumentIndex
    selectedArgumentBinderInfo := some selectedBinderInfo
    selectedArgumentBinderInfoTag :=
      some (binderInfoEvidenceTag selectedBinderInfo)
    declarationTypeHash := hash constantInfo.type
    selectedExprHash := hash selected
    selectedTypeHash
    passed := stepPassed
  }
  if !stepPassed then
    return (steps.push step, none, some selectedBinderInfo, false)
  if cursor + 1 == indices.size then
    return (steps.push step, some selected, some selectedBinderInfo, true)
  resolveApplicationPathAux environment selected indices (cursor + 1) (steps.push step)

private def resolveApplicationPath
    (environment : Environment) (witness : Expr)
    (path : LeanFaith.SFT1.Wave1.N31ApplicationArgumentPath)
    (expectedRoleExplicit : Bool) : MetaM N31ApplicationPathResolution := do
  if path.argumentIndices.isEmpty then
    return {
      path
      expectedRoleExplicit
      steps := #[]
      selectedExprHash := none
      selectedTypeHash := none
      selectedBinderInfo := none
      selectedBinderInfoTag := none
      pathResolved := false
      binderInfoClassMatches := false
      passed := false
    }
  let (steps, selected?, selectedBinderInfo, pathResolved) ←
    resolveApplicationPathAux environment witness path.argumentIndices 0 #[]
  let selectedExprHash := selected?.map hash
  let selectedTypeHash ←
    match selected? with
    | some selected => inferTypeHash? selected
    | none => pure none
  let selectedBinderInfoTag :=
    selectedBinderInfo.map binderInfoEvidenceTag
  let binderInfoClassMatches :=
    match selectedBinderInfo with
    | none => false
    | some binderInfo =>
        if expectedRoleExplicit then binderInfo == .default else binderInfo != .default
  let passed := pathResolved && selectedExprHash.isSome && selectedTypeHash.isSome &&
    binderInfoClassMatches && steps.all (·.passed)
  return {
    path
    expectedRoleExplicit
    steps
    selectedExprHash
    selectedTypeHash
    selectedBinderInfo
    selectedBinderInfoTag
    pathResolved
    binderInfoClassMatches
    passed
  }

private def resolveWitnessPath?
    (environment : Environment) (witness : Expr)
    (path : LeanFaith.SFT1.Wave1.N31ApplicationArgumentPath) :
    MetaM (Option Expr × Bool) := do
  if path.argumentIndices.isEmpty then
    return (none, false)
  let (steps, selected?, _, pathResolved) ←
    resolveApplicationPathAux environment witness path.argumentIndices 0 #[]
  return (selected?, pathResolved && steps.size == path.argumentIndices.size &&
    steps.all (·.passed))

private def resolveNestedHeadWitnessConstraint
    (environment : Environment) (witness : Expr)
    (constraint : LeanFaith.SFT1.Wave1.N31NestedHeadConstraint) :
    MetaM N31NestedHeadWitnessResolution := do
  let (selected?, pathResolved) ←
    resolveWitnessPath? environment witness constraint.path
  let selectedExprHash := selected?.map hash
  let application? := selected? >>= applicationView?
  let observedHeadName := application?.map (·.headName)
  let observedArgumentCount := application?.map fun application => application.arguments.size
  let headMatches := observedHeadName == some constraint.headName
  let arityMatches := observedArgumentCount == some constraint.argumentCount
  let passed := pathResolved && selectedExprHash.isSome && headMatches && arityMatches
  return {
    path := constraint.path
    expectedHeadName := constraint.headName
    expectedArgumentCount := constraint.argumentCount
    selectedExprHash
    observedHeadName
    observedArgumentCount
    pathResolved
    headMatches
    arityMatches
    passed
  }

private def resolveLiteralWitnessConstraint
    (environment : Environment) (witness : Expr)
    (constraint : LeanFaith.SFT1.Wave1.N31LiteralConstraint) :
    MetaM N31LiteralWitnessResolution := do
  let (selected?, pathResolved) ←
    resolveWitnessPath? environment witness constraint.path
  let selectedExprHash := selected?.map hash
  let observed :=
    match selected?.map stripMData with
    | some (.lit value) => some value
    | _ => none
  let literalMatches := observed == some constraint.expected
  let passed := pathResolved && selectedExprHash.isSome && literalMatches
  return {
    path := constraint.path
    expected := constraint.expected
    selectedExprHash
    observed
    pathResolved
    literalMatches
    passed
  }

private def resolveExactExprWitnessConstraint
    (environment : Environment) (witness : Expr)
    (constraint : LeanFaith.SFT1.Wave1.N31ExactExprConstraint) :
    MetaM N31ExactExprWitnessResolution := do
  let (selected?, pathResolved) ←
    resolveWitnessPath? environment witness constraint.path
  let selectedExprHash := selected?.map hash
  let exactExprMatches :=
    match selected? with
    | some selected => Expr.equal selected constraint.expected
    | none => false
  let passed := pathResolved && selectedExprHash.isSome && exactExprMatches
  return {
    path := constraint.path
    expectedExprHash := hash constraint.expected
    selectedExprHash
    pathResolved
    exactExprMatches
    passed
  }

private def findRetainedWitness?
    (witnesses : Array N31RetainedPatternWitness) (shapeId : String) :
    Option N31RetainedPatternWitness :=
  match (witnesses.filter fun witness => witness.shapeId == shapeId).toList with
  | [witness] => some witness
  | _ => none

private def witnessTypedProp (witness : Expr) : MetaM (Bool × Option UInt64) := do
  if witness.hasExprMVar || witness.hasLevelMVar || witness.hasFVar ||
      witness.hasLooseBVars || witness.hasSorry then
    return (false, none)
  try
    check witness
    let witnessType ← inferType witness
    return (← isProp witness, some (hash witnessType))
  catch ex =>
    if ex.isInterrupt || ex.isRuntime then
      throw ex
    return (false, none)

private def resolveRetainedPattern
    (environment : Environment)
    (pattern : N31RetainedContradictionPattern)
    (witness? : Option N31RetainedPatternWitness) : MetaM N31RetainedPatternResolution := do
  let head := resolveHead environment pattern.headName pattern.argumentCount
  let nestedHeads := resolveNestedHeads environment pattern.nestedHeadConstraints
  let witnessFoundUniquely := witness?.isSome
  let witnessExprHash := witness?.map fun witness => hash witness.proposition
  let (witnessIsTypedProp, witnessTypeHash) ←
    match witness? with
    | some witness => witnessTypedProp witness.proposition
    | none => pure (false, none)
  let (rootHeadMatches, rootArityMatches) :=
    match witness? >>= fun witness => applicationView? witness.proposition with
    | some application =>
        (application.headName == pattern.headName,
          application.arguments.size == pattern.argumentCount)
    | none => (false, false)
  let mut rolePathResolutions := #[]
  let mut instanceOrTypePathResolutions := #[]
  let mut nestedHeadWitnessResolutions := #[]
  let mut literalWitnessResolutions := #[]
  let mut exactExprWitnessResolutions := #[]
  if let some witness := witness? then
    for path in pattern.rolePaths do
      rolePathResolutions := rolePathResolutions.push
        (← resolveApplicationPath environment witness.proposition path true)
    for path in pattern.instancePaths do
      instanceOrTypePathResolutions := instanceOrTypePathResolutions.push
        (← resolveApplicationPath environment witness.proposition path false)
    for constraint in pattern.nestedHeadConstraints do
      nestedHeadWitnessResolutions := nestedHeadWitnessResolutions.push
        (← resolveNestedHeadWitnessConstraint environment witness.proposition constraint)
    for constraint in pattern.literalConstraints do
      literalWitnessResolutions := literalWitnessResolutions.push
        (← resolveLiteralWitnessConstraint environment witness.proposition constraint)
    for constraint in pattern.exactExprConstraints do
      exactExprWitnessResolutions := exactExprWitnessResolutions.push
        (← resolveExactExprWitnessConstraint environment witness.proposition constraint)
  let pathsNonempty := pattern.rolePaths.all pathNonempty &&
    pattern.instancePaths.all pathNonempty &&
    pattern.nestedHeadConstraints.all (fun constraint => pathNonempty constraint.path) &&
    pattern.literalConstraints.all (fun constraint => pathNonempty constraint.path) &&
    pattern.exactExprConstraints.all fun constraint => pathNonempty constraint.path
  let exactConstraintTermsClosedAndTyped ←
    exactConstraintsClosedAndTyped pattern.exactExprConstraints
  let structuralShapeResolved := !pattern.shapeId.isEmpty &&
    !pattern.headName.isAnonymous && !pattern.rolePaths.isEmpty &&
    !pattern.instancePaths.isEmpty && applicationPathArrayUnique pattern.rolePaths &&
    applicationPathArrayUnique pattern.instancePaths &&
    applicationPathArraysDisjoint pattern.rolePaths pattern.instancePaths &&
    nestedHeadsStructurallyResolved pattern.nestedHeadConstraints &&
    literalConstraintsStructurallyResolved pattern.literalConstraints &&
    exactConstraintsStructurallyResolved pattern.exactExprConstraints &&
    applicationPathArraysDisjoint pattern.rolePaths
      (pattern.exactExprConstraints.map (·.path)) &&
    applicationPathArraysDisjoint pattern.instancePaths
      (pattern.exactExprConstraints.map (·.path))
  let pathResolutionsPassed :=
    rolePathResolutions.size == pattern.rolePaths.size &&
      instanceOrTypePathResolutions.size == pattern.instancePaths.size &&
      !rolePathResolutions.isEmpty && !instanceOrTypePathResolutions.isEmpty &&
      rolePathResolutions.all (·.passed) &&
      instanceOrTypePathResolutions.all (·.passed)
  let constraintWitnessReplaysPassed :=
    nestedHeadWitnessResolutions.size == pattern.nestedHeadConstraints.size &&
      literalWitnessResolutions.size == pattern.literalConstraints.size &&
      exactExprWitnessResolutions.size == pattern.exactExprConstraints.size &&
      nestedHeadWitnessResolutions.all (·.passed) &&
      literalWitnessResolutions.all (·.passed) &&
      exactExprWitnessResolutions.all (·.passed)
  let passed := structuralShapeResolved && witnessFoundUniquely && witnessIsTypedProp &&
    rootHeadMatches && rootArityMatches && pathResolutionsPassed && head.arityMatches &&
    allHeadsFound nestedHeads && allHeadAritiesMatch nestedHeads && pathsNonempty &&
    constraintWitnessReplaysPassed && exactConstraintTermsClosedAndTyped
  return {
    shapeId := pattern.shapeId
    head
    nestedHeads
    witnessFoundUniquely
    witnessExprHash
    witnessTypeHash
    witnessIsTypedProp
    rootHeadMatches
    rootArityMatches
    rolePathResolutions
    instanceOrTypePathResolutions
    nestedHeadWitnessResolutions
    literalWitnessResolutions
    exactExprWitnessResolutions
    pathsNonempty
    constraintWitnessReplaysPassed
    structuralShapeResolved
    exactConstraintTermsClosedAndTyped
    passed
  }

private def stringArrayUnique (values : Array String) : Bool :=
  values.toList.eraseDups.length == values.size

private def selectableShapeExists (bank : N31TargetBank) (shapeId : String) : Bool :=
  bank.entries.any fun entry => entry.guardShapeId == shapeId

private def retainedShapeExists (bank : N31TargetBank) (shapeId : String) : Bool :=
  bank.retainedContradictionPatterns.any fun pattern => pattern.shapeId == shapeId

private def selectableGuardDefinitionsEqual
    (left right : N31TargetBankEntry) : Bool :=
  left.guardHeadName == right.guardHeadName &&
    left.guardArgumentCount == right.guardArgumentCount &&
    left.guardRoleArgumentIndices == right.guardRoleArgumentIndices &&
    left.guardInstanceArgumentIndices == right.guardInstanceArgumentIndices &&
    left.guardFixedHeads == right.guardFixedHeads &&
    left.guardNestedHeadConstraints == right.guardNestedHeadConstraints &&
    left.guardLiteralConstraints == right.guardLiteralConstraints &&
    left.guardExactExprConstraints == right.guardExactExprConstraints

private def selectableGuardDefinitionsAreCoherent (bank : N31TargetBank) : Bool :=
  bank.entries.all fun left =>
    bank.entries.all fun right =>
      left.guardShapeId != right.guardShapeId || selectableGuardDefinitionsEqual left right

private def implicationRuleResolves
    (bank : N31TargetBank) (rule : LeanFaith.SFT1.Wave1.N31ImplicationRule) : Bool :=
  let premises := bank.entries.filter fun entry => entry.guardShapeId == rule.premiseShapeId
  let conclusions := bank.entries.filter fun entry => entry.guardShapeId == rule.conclusionShapeId
  !premises.isEmpty && !conclusions.isEmpty && premises.all fun premise =>
    conclusions.all fun conclusion =>
      premise.guardRoleArgumentIndices.size == conclusion.guardRoleArgumentIndices.size &&
        premise.guardInstanceArgumentIndices.size ==
          conclusion.guardInstanceArgumentIndices.size

private def contradictionRuleResolves
    (bank : N31TargetBank) (rule : LeanFaith.SFT1.Wave1.N31ContradictionRule) : Bool :=
  let retained := bank.retainedContradictionPatterns.filter fun pattern =>
    pattern.shapeId == rule.retainedShapeId
  let removed := bank.entries.filter fun entry => entry.guardShapeId == rule.removedShapeId
  retained.size == 1 && !removed.isEmpty && removed.all fun entry =>
    retained[0]!.rolePaths.size == entry.guardRoleArgumentIndices.size &&
      retained[0]!.instancePaths.size == entry.guardInstanceArgumentIndices.size

/-- Resolve exact N31 names, declaration arities, direct role/instance binder
    positions, and closed exact-term constraints.  This is a proposal receipt,
    not the frozen private semantic checker and not success/adversarial fixture
    conformance.  It always records both of those semantic fields as false. -/
def resolveN31Proposal
    (bank : N31TargetBank)
    (retainedWitnesses : Array N31RetainedPatternWitness := #[]) :
    MetaM N31ProposalResolutionReceipt := do
  let environment ← getEnv
  let mut entries := #[]
  for entry in bank.entries do
    entries := entries.push (← resolveEntry environment entry)
  let mut retainedPatterns := #[]
  for pattern in bank.retainedContradictionPatterns do
    retainedPatterns := retainedPatterns.push
      (← resolveRetainedPattern environment pattern
        (findRetainedWitness? retainedWitnesses pattern.shapeId))
  let identityProjectAndBankNonempty := !bank.identity.projectId.isEmpty &&
    !bank.identity.bankId.isEmpty
  let resolvedLeanHashPopulated := !bank.identity.resolvedLeanHash.isEmpty
  let resolutionReceiptHashPopulated :=
    !bank.identity.resolutionReceiptHash.isEmpty
  let entryIdsUnique := stringArrayUnique (bank.entries.map (·.entryId))
  let retainedShapeIdsUnique :=
    stringArrayUnique (bank.retainedContradictionPatterns.map (·.shapeId))
  let selectableGuardDefinitionsCoherent :=
    selectableGuardDefinitionsAreCoherent bank
  let retainedShapesDisjointFromSelectable :=
    bank.retainedContradictionPatterns.all fun pattern =>
      !selectableShapeExists bank pattern.shapeId
  let implicationReferencesResolve := bank.implications.all fun rule =>
    selectableShapeExists bank rule.premiseShapeId &&
      selectableShapeExists bank rule.conclusionShapeId &&
      implicationRuleResolves bank rule
  let contradictionReferencesResolve := bank.contradictions.all fun rule =>
    retainedShapeExists bank rule.retainedShapeId &&
      selectableShapeExists bank rule.removedShapeId &&
      contradictionRuleResolves bank rule
  let allEntryResolutionsPassed := entries.all (·.passed)
  let allRetainedPatternResolutionsPassed := retainedPatterns.all (·.passed)
  let allNamesResolved :=
    (entries.all fun entry =>
      entry.guardHead.declarationFound && entry.targetHead.declarationFound &&
        allHeadsFound entry.guardFixedHeads && allHeadsFound entry.targetFixedHeads &&
        allHeadsFound entry.guardNestedHeads && allHeadsFound entry.targetNestedHeads) &&
    (retainedPatterns.all fun pattern =>
      pattern.head.declarationFound && allHeadsFound pattern.nestedHeads)
  let allAritiesResolved :=
    (entries.all fun entry =>
      entry.guardHead.arityMatches && entry.targetHead.arityMatches &&
        allHeadAritiesMatch entry.guardFixedHeads &&
        allHeadAritiesMatch entry.targetFixedHeads &&
        allHeadAritiesMatch entry.guardNestedHeads &&
        allHeadAritiesMatch entry.targetNestedHeads) &&
    (retainedPatterns.all fun pattern =>
      pattern.head.arityMatches && allHeadAritiesMatch pattern.nestedHeads)
  let allTypeAndInstanceConstraintsResolved :=
    (entries.all fun entry =>
      entry.guardHead.declarationTypeClosed && entry.targetHead.declarationTypeClosed &&
        entry.guardRoleBinderInfosMatch && entry.targetRoleBinderInfosMatch &&
        entry.guardInstanceBinderInfosMatch && entry.targetInstanceBinderInfosMatch &&
        entry.exactConstraintTermsClosedAndTyped) &&
    (retainedPatterns.all fun pattern =>
      pattern.head.declarationTypeClosed && pattern.constraintWitnessReplaysPassed &&
        pattern.exactConstraintTermsClosedAndTyped)
  let frozenAdmissionIsEmpty := n31FrozenAdmissionIsEmpty
  let proposedIdentityAlreadyAdmitted :=
    n31FrozenAdmissionIdentities.contains bank.identity
  let privateSemanticCheckerAvailable :=
    n31PrivateSemanticCheckerAvailableToAdditiveRuntime
  let semanticSuccessConformancePerformed := false
  let semanticAdversarialConformancePerformed := false
  let activationExposed := n31ProposalActivationExposed
  let proposalResolutionPassed := identityProjectAndBankNonempty && !bank.entries.isEmpty &&
    entryIdsUnique && retainedShapeIdsUnique && selectableGuardDefinitionsCoherent &&
    retainedShapesDisjointFromSelectable && implicationReferencesResolve &&
    contradictionReferencesResolve && allEntryResolutionsPassed &&
    allRetainedPatternResolutionsPassed && allNamesResolved && allAritiesResolved &&
    allTypeAndInstanceConstraintsResolved && frozenAdmissionIsEmpty &&
    !proposedIdentityAlreadyAdmitted && !privateSemanticCheckerAvailable &&
    !semanticSuccessConformancePerformed && !semanticAdversarialConformancePerformed &&
    !activationExposed
  return {
    operationId := n31OperationId
    bank
    identityProjectAndBankNonempty
    resolvedLeanHashPopulated
    resolutionReceiptHashPopulated
    entryIdsUnique
    retainedShapeIdsUnique
    entries
    retainedPatterns
    selectableGuardDefinitionsCoherent
    retainedShapesDisjointFromSelectable
    implicationReferencesResolve
    contradictionReferencesResolve
    allEntryResolutionsPassed
    allRetainedPatternResolutionsPassed
    allNamesResolved
    allAritiesResolved
    allTypeAndInstanceConstraintsResolved
    frozenAdmissionIsEmpty
    proposedIdentityAlreadyAdmitted
    privateSemanticCheckerAvailable
    semanticSuccessConformancePerformed
    semanticAdversarialConformancePerformed
    activationExposed
    proposalResolutionPassed
  }

structure N31FrozenNonActivationReceipt where
  operationId : String
  source : Expr
  selector : Selector
  bankIdentity : LeanFaith.SFT1.Wave1.N31BankIdentity
  expectedResolvedLeanHash : String
  expectedResolutionReceiptHash : String
  expectedHashesNonempty : Bool
  expectedHashesAreLowerHexSha256 : Bool
  identityMatchesExpectedHashes : Bool
  externalHashComputationPerformedInLean : Bool
  externalStrictRunnerHashVerificationRequired : Bool
  proposalResolutionPassed : Bool
  frozenAdmissionIsEmpty : Bool
  identityAbsentFromFrozenAdmission : Bool
  frozenDispatchRejectedAsUnadmittedBank : Bool
  rejectionReason : FailureReason
  privateSemanticCheckerAvailable : Bool
  semanticConformancePerformed : Bool
  candidateConstructed : Bool
  activationExposed : Bool
  deriving Inhabited, Repr

private def isLowerHexSha256 (value : String) : Bool :=
  value.length == 64 &&
    value.toList.all fun character => "0123456789abcdef".contains character

/-- Bind a live source/selector to the resolved proposal and prove that the
    frozen public path still rejects it at the empty admission boundary.  No
    candidate is returned.  Any other terminal result fails closed. -/
def replayN31FrozenNonActivation
    (source : Expr) (selector : Selector) (bank : N31TargetBank)
    (expectedResolvedLeanHash expectedResolutionReceiptHash : String)
    (reachability : N31ReachabilityEvidence)
    (retainedWitnesses : Array N31RetainedPatternWitness) :
    MetaM N31FrozenNonActivationReceipt := do
  let resolution ← resolveN31Proposal bank retainedWitnesses
  unless resolution.proposalResolutionPassed do
    throwError "sft1_n31_proposal_resolution_failed"
  unless resolution.resolutionReceiptHashPopulated do
    throwError "sft1_n31_resolution_receipt_hash_not_bound"
  unless resolution.resolvedLeanHashPopulated do
    throwError "sft1_n31_resolved_lean_hash_not_bound"
  let expectedHashesNonempty := !expectedResolvedLeanHash.isEmpty &&
    !expectedResolutionReceiptHash.isEmpty
  unless expectedHashesNonempty do
    throwError "sft1_n31_expected_external_hashes_empty"
  let expectedHashesAreLowerHexSha256 := isLowerHexSha256 expectedResolvedLeanHash &&
    isLowerHexSha256 expectedResolutionReceiptHash
  unless expectedHashesAreLowerHexSha256 do
    throwError "sft1_n31_expected_external_hashes_malformed"
  let identityMatchesExpectedHashes :=
    bank.identity.resolvedLeanHash == expectedResolvedLeanHash &&
      bank.identity.resolutionReceiptHash == expectedResolutionReceiptHash
  unless identityMatchesExpectedHashes do
    throwError "sft1_n31_identity_does_not_match_expected_external_hashes"
  let identityAbsentFromFrozenAdmission :=
    !n31FrozenAdmissionIdentities.contains bank.identity
  let result ← LeanFaith.SFT1.Wave1.dispatchAt
    .n31DropRequiredGuardRubric selector
    { n31Bank := some bank, n31Reachability := some reachability }
    source
  let rejectionReason ←
    match result with
    | .typedNotApplicable .n31BankInvalid =>
        pure LeanFaith.SFT1.Wave1.FailureReason.n31BankInvalid
    | .typedNotApplicable _ =>
        throwError "sft1_n31_unexpected_nonactivation_reason"
    | .applicable _ =>
        throwError "sft1_n31_proposal_unexpectedly_activated"
  return {
    operationId := n31OperationId
    source
    selector
    bankIdentity := bank.identity
    expectedResolvedLeanHash
    expectedResolutionReceiptHash
    expectedHashesNonempty
    expectedHashesAreLowerHexSha256
    identityMatchesExpectedHashes
    externalHashComputationPerformedInLean := false
    externalStrictRunnerHashVerificationRequired := true
    proposalResolutionPassed := resolution.proposalResolutionPassed
    frozenAdmissionIsEmpty := resolution.frozenAdmissionIsEmpty
    identityAbsentFromFrozenAdmission
    frozenDispatchRejectedAsUnadmittedBank := true
    rejectionReason
    privateSemanticCheckerAvailable := false
    semanticConformancePerformed := false
    candidateConstructed := false
    activationExposed := false
  }

private def n31ApplicationPathJson
    (path : LeanFaith.SFT1.Wave1.N31ApplicationArgumentPath) : Json :=
  Json.mkObj [("argument_indices", natArrayJson path.argumentIndices)]

private def n31FixedHeadConstraintJson
    (constraint : LeanFaith.SFT1.Wave1.N31FixedHeadConstraint) : Json :=
  Json.mkObj [
    ("argument_index", toJson constraint.argumentIndex),
    ("head_name", nameJson constraint.headName),
    ("argument_count", toJson constraint.argumentCount)
  ]

private def n31NestedHeadConstraintJson
    (constraint : LeanFaith.SFT1.Wave1.N31NestedHeadConstraint) : Json :=
  Json.mkObj [
    ("path", n31ApplicationPathJson constraint.path),
    ("head_name", nameJson constraint.headName),
    ("argument_count", toJson constraint.argumentCount)
  ]

private def literalJson : Literal → Json
  | .natVal value => Json.mkObj [
      ("kind", Json.str "nat"),
      ("value", Json.str (toString value))
    ]
  | .strVal value => Json.mkObj [
      ("kind", Json.str "string"),
      ("value", Json.str value)
    ]

private def n31LiteralConstraintJson
    (constraint : LeanFaith.SFT1.Wave1.N31LiteralConstraint) : Json :=
  Json.mkObj [
    ("path", n31ApplicationPathJson constraint.path),
    ("expected", literalJson constraint.expected)
  ]

private def n31ExactExprConstraintJson
    (constraint : LeanFaith.SFT1.Wave1.N31ExactExprConstraint) : Json :=
  Json.mkObj [
    ("path", n31ApplicationPathJson constraint.path),
    ("expected_expr_hash", exprHashJson constraint.expected)
  ]

private def n31BankEntryJson (entry : N31TargetBankEntry) : Json :=
  Json.mkObj [
    ("entry_id", Json.str entry.entryId),
    ("guard_shape_id", Json.str entry.guardShapeId),
    ("guard_head_name", nameJson entry.guardHeadName),
    ("guard_argument_count", toJson entry.guardArgumentCount),
    ("guard_role_argument_indices", natArrayJson entry.guardRoleArgumentIndices),
    ("guard_instance_or_type_argument_indices",
      natArrayJson entry.guardInstanceArgumentIndices),
    ("guard_fixed_heads", Json.arr (entry.guardFixedHeads.map n31FixedHeadConstraintJson)),
    ("guard_nested_heads",
      Json.arr (entry.guardNestedHeadConstraints.map n31NestedHeadConstraintJson)),
    ("guard_literal_constraints",
      Json.arr (entry.guardLiteralConstraints.map n31LiteralConstraintJson)),
    ("guard_exact_expr_constraints",
      Json.arr (entry.guardExactExprConstraints.map n31ExactExprConstraintJson)),
    ("target_head_name", nameJson entry.targetHeadName),
    ("target_argument_count", toJson entry.targetArgumentCount),
    ("target_role_argument_indices", natArrayJson entry.targetRoleArgumentIndices),
    ("target_instance_or_type_argument_indices",
      natArrayJson entry.targetInstanceArgumentIndices),
    ("target_fixed_heads", Json.arr (entry.targetFixedHeads.map n31FixedHeadConstraintJson)),
    ("target_nested_heads",
      Json.arr (entry.targetNestedHeadConstraints.map n31NestedHeadConstraintJson)),
    ("target_literal_constraints",
      Json.arr (entry.targetLiteralConstraints.map n31LiteralConstraintJson)),
    ("target_exact_expr_constraints",
      Json.arr (entry.targetExactExprConstraints.map n31ExactExprConstraintJson))
  ]

private def n31RetainedPatternJson
    (pattern : N31RetainedContradictionPattern) : Json :=
  Json.mkObj [
    ("shape_id", Json.str pattern.shapeId),
    ("head_name", nameJson pattern.headName),
    ("argument_count", toJson pattern.argumentCount),
    ("role_paths", Json.arr (pattern.rolePaths.map n31ApplicationPathJson)),
    ("instance_or_type_paths", Json.arr (pattern.instancePaths.map n31ApplicationPathJson)),
    ("nested_heads",
      Json.arr (pattern.nestedHeadConstraints.map n31NestedHeadConstraintJson)),
    ("literal_constraints",
      Json.arr (pattern.literalConstraints.map n31LiteralConstraintJson)),
    ("exact_expr_constraints",
      Json.arr (pattern.exactExprConstraints.map n31ExactExprConstraintJson))
  ]

private def n31ResolvedHeadJson (head : N31ResolvedHead) : Json :=
  Json.mkObj [
    ("name", nameJson head.name),
    ("expected_argument_count", toJson head.expectedArgumentCount),
    ("observed_argument_count",
      match head.observedArgumentCount with
      | none => Json.null
      | some count => toJson count),
    ("declaration_type_hash",
      match head.declarationType with
      | none => Json.null
      | some type => exprHashJson type),
    ("argument_binder_info_tags", stringArrayJson (binderInfoTags head.argumentBinderInfos)),
    ("declaration_found", Json.bool head.declarationFound),
    ("declaration_type_closed", Json.bool head.declarationTypeClosed),
    ("arity_matches", Json.bool head.arityMatches)
  ]

private def n31PathStepResolutionJson
    (step : N31ApplicationPathStepResolution) : Json :=
  Json.mkObj [
    ("head_name", nameJson step.headName),
    ("observed_argument_count", toJson step.observedArgumentCount),
    ("selected_argument_index", toJson step.selectedArgumentIndex),
    ("selected_argument_binder_info", optionBinderInfoJson step.selectedArgumentBinderInfo),
    ("selected_argument_binder_info_tag",
      match step.selectedArgumentBinderInfoTag with
      | none => Json.null
      | some tag => Json.str tag),
    ("declaration_type_hash", uint64Json step.declarationTypeHash),
    ("selected_expr_hash", uint64Json step.selectedExprHash),
    ("selected_type_hash", optionUInt64Json step.selectedTypeHash),
    ("passed", Json.bool step.passed)
  ]

private def n31PathResolutionJson
    (resolution : N31ApplicationPathResolution) : Json :=
  Json.mkObj [
    ("path", n31ApplicationPathJson resolution.path),
    ("expected_role_explicit", Json.bool resolution.expectedRoleExplicit),
    ("steps", Json.arr (resolution.steps.map n31PathStepResolutionJson)),
    ("selected_expr_hash", optionUInt64Json resolution.selectedExprHash),
    ("selected_type_hash", optionUInt64Json resolution.selectedTypeHash),
    ("selected_binder_info", optionBinderInfoJson resolution.selectedBinderInfo),
    ("selected_binder_info_tag",
      match resolution.selectedBinderInfoTag with
      | none => Json.null
      | some tag => Json.str tag),
    ("path_resolved", Json.bool resolution.pathResolved),
    ("binder_info_class_matches", Json.bool resolution.binderInfoClassMatches),
    ("passed", Json.bool resolution.passed)
  ]

private def n31NestedHeadWitnessResolutionJson
    (resolution : N31NestedHeadWitnessResolution) : Json :=
  Json.mkObj [
    ("path", n31ApplicationPathJson resolution.path),
    ("expected_head_name", nameJson resolution.expectedHeadName),
    ("expected_argument_count", toJson resolution.expectedArgumentCount),
    ("selected_expr_hash", optionUInt64Json resolution.selectedExprHash),
    ("observed_head_name",
      match resolution.observedHeadName with
      | none => Json.null
      | some name => nameJson name),
    ("observed_argument_count",
      match resolution.observedArgumentCount with
      | none => Json.null
      | some count => toJson count),
    ("path_resolved", Json.bool resolution.pathResolved),
    ("head_matches", Json.bool resolution.headMatches),
    ("arity_matches", Json.bool resolution.arityMatches),
    ("passed", Json.bool resolution.passed)
  ]

private def n31LiteralWitnessResolutionJson
    (resolution : N31LiteralWitnessResolution) : Json :=
  Json.mkObj [
    ("path", n31ApplicationPathJson resolution.path),
    ("expected", literalJson resolution.expected),
    ("selected_expr_hash", optionUInt64Json resolution.selectedExprHash),
    ("observed",
      match resolution.observed with
      | none => Json.null
      | some value => literalJson value),
    ("path_resolved", Json.bool resolution.pathResolved),
    ("literal_matches", Json.bool resolution.literalMatches),
    ("passed", Json.bool resolution.passed)
  ]

private def n31ExactExprWitnessResolutionJson
    (resolution : N31ExactExprWitnessResolution) : Json :=
  Json.mkObj [
    ("path", n31ApplicationPathJson resolution.path),
    ("expected_expr_hash", uint64Json resolution.expectedExprHash),
    ("selected_expr_hash", optionUInt64Json resolution.selectedExprHash),
    ("path_resolved", Json.bool resolution.pathResolved),
    ("exact_expr_matches", Json.bool resolution.exactExprMatches),
    ("passed", Json.bool resolution.passed)
  ]

private def n31EntryResolutionJson (entry : N31EntryResolution) : Json :=
  Json.mkObj [
    ("entry_id", Json.str entry.entryId),
    ("guard_shape_id", Json.str entry.guardShapeId),
    ("guard_head", n31ResolvedHeadJson entry.guardHead),
    ("target_head", n31ResolvedHeadJson entry.targetHead),
    ("guard_fixed_heads", Json.arr (entry.guardFixedHeads.map n31ResolvedHeadJson)),
    ("target_fixed_heads", Json.arr (entry.targetFixedHeads.map n31ResolvedHeadJson)),
    ("guard_nested_heads", Json.arr (entry.guardNestedHeads.map n31ResolvedHeadJson)),
    ("target_nested_heads", Json.arr (entry.targetNestedHeads.map n31ResolvedHeadJson)),
    ("guard_role_indices_in_range", Json.bool entry.guardRoleIndicesInRange),
    ("target_role_indices_in_range", Json.bool entry.targetRoleIndicesInRange),
    ("guard_instance_or_type_indices_in_range", Json.bool entry.guardInstanceIndicesInRange),
    ("target_instance_or_type_indices_in_range", Json.bool entry.targetInstanceIndicesInRange),
    ("guard_role_binder_infos_match", Json.bool entry.guardRoleBinderInfosMatch),
    ("target_role_binder_infos_match", Json.bool entry.targetRoleBinderInfosMatch),
    ("guard_instance_or_type_binder_infos_match",
      Json.bool entry.guardInstanceBinderInfosMatch),
    ("target_instance_or_type_binder_infos_match",
      Json.bool entry.targetInstanceBinderInfosMatch),
    ("guard_role_observed_binder_info_tags",
      stringArrayJson entry.guardRoleObservedBinderInfoTags),
    ("target_role_observed_binder_info_tags",
      stringArrayJson entry.targetRoleObservedBinderInfoTags),
    ("guard_instance_or_type_observed_binder_info_tags",
      stringArrayJson entry.guardInstanceOrTypeObservedBinderInfoTags),
    ("target_instance_or_type_observed_binder_info_tags",
      stringArrayJson entry.targetInstanceOrTypeObservedBinderInfoTags),
    ("structural_shape_resolved", Json.bool entry.structuralShapeResolved),
    ("exact_constraint_terms_closed_and_typed",
      Json.bool entry.exactConstraintTermsClosedAndTyped),
    ("passed", Json.bool entry.passed)
  ]

private def n31RetainedPatternResolutionJson
    (pattern : N31RetainedPatternResolution) : Json :=
  Json.mkObj [
    ("shape_id", Json.str pattern.shapeId),
    ("head", n31ResolvedHeadJson pattern.head),
    ("nested_heads", Json.arr (pattern.nestedHeads.map n31ResolvedHeadJson)),
    ("witness_found_uniquely", Json.bool pattern.witnessFoundUniquely),
    ("witness_expr_hash", optionUInt64Json pattern.witnessExprHash),
    ("witness_type_hash", optionUInt64Json pattern.witnessTypeHash),
    ("witness_is_typed_prop", Json.bool pattern.witnessIsTypedProp),
    ("root_head_matches", Json.bool pattern.rootHeadMatches),
    ("root_arity_matches", Json.bool pattern.rootArityMatches),
    ("role_path_resolutions",
      Json.arr (pattern.rolePathResolutions.map n31PathResolutionJson)),
    ("instance_or_type_path_resolutions",
      Json.arr (pattern.instanceOrTypePathResolutions.map n31PathResolutionJson)),
    ("nested_head_witness_resolutions",
      Json.arr (pattern.nestedHeadWitnessResolutions.map
        n31NestedHeadWitnessResolutionJson)),
    ("literal_witness_resolutions",
      Json.arr (pattern.literalWitnessResolutions.map
        n31LiteralWitnessResolutionJson)),
    ("exact_expr_witness_resolutions",
      Json.arr (pattern.exactExprWitnessResolutions.map
        n31ExactExprWitnessResolutionJson)),
    ("paths_nonempty", Json.bool pattern.pathsNonempty),
    ("constraint_witness_replays_passed",
      Json.bool pattern.constraintWitnessReplaysPassed),
    ("structural_shape_resolved", Json.bool pattern.structuralShapeResolved),
    ("exact_constraint_terms_closed_and_typed",
      Json.bool pattern.exactConstraintTermsClosedAndTyped),
    ("passed", Json.bool pattern.passed)
  ]

private def n31BankIdentityJson
    (identity : LeanFaith.SFT1.Wave1.N31BankIdentity) : Json :=
  Json.mkObj [
    ("project_id", Json.str identity.projectId),
    ("bank_id", Json.str identity.bankId),
    ("resolved_lean_hash", Json.str identity.resolvedLeanHash),
    ("resolution_receipt_hash", Json.str identity.resolutionReceiptHash)
  ]

def n31StructuralBankFingerprintPayload
    (receipt : N31ProposalResolutionReceipt) : Json :=
  Json.mkObj [
    ("basis_id", Json.str n31StructuralBankFingerprintBasisId),
    ("sha256_input_contract", Json.str "python_canonical_json_utf8_v1"),
    ("structural_expr_fingerprint_id", Json.str structuralExprFingerprintId),
    ("identity", Json.mkObj [
      ("project_id", Json.str receipt.bank.identity.projectId),
      ("bank_id", Json.str receipt.bank.identity.bankId),
      ("resolved_lean_hash", Json.str ""),
      ("resolution_receipt_hash", Json.str "")
    ]),
    ("entries", Json.arr (receipt.bank.entries.map n31BankEntryJson)),
    ("retained_contradiction_patterns",
      Json.arr (receipt.bank.retainedContradictionPatterns.map n31RetainedPatternJson)),
    ("implications", Json.arr (receipt.bank.implications.map fun rule => Json.mkObj [
      ("premise_shape_id", Json.str rule.premiseShapeId),
      ("conclusion_shape_id", Json.str rule.conclusionShapeId)
    ])),
    ("contradictions", Json.arr (receipt.bank.contradictions.map fun rule => Json.mkObj [
      ("retained_shape_id", Json.str rule.retainedShapeId),
      ("removed_shape_id", Json.str rule.removedShapeId)
    ])),
    ("resolved_entries", Json.arr (receipt.entries.map n31EntryResolutionJson)),
    ("resolved_retained_patterns",
      Json.arr (receipt.retainedPatterns.map n31RetainedPatternResolutionJson))
  ]

def n31ResolutionReceiptHashPreimagePayload
    (receipt : N31ProposalResolutionReceipt) : Json :=
  Json.mkObj [
    ("basis_id", Json.str n31ResolutionReceiptHashBasisId),
    ("sha256_input_contract", Json.str "python_canonical_json_utf8_v1"),
    ("bank_fingerprint_payload", n31StructuralBankFingerprintPayload receipt),
    ("operation_id", Json.str receipt.operationId),
    ("identity_project_and_bank_nonempty", Json.bool receipt.identityProjectAndBankNonempty),
    ("entry_ids_unique", Json.bool receipt.entryIdsUnique),
    ("retained_shape_ids_unique", Json.bool receipt.retainedShapeIdsUnique),
    ("selectable_guard_definitions_coherent",
      Json.bool receipt.selectableGuardDefinitionsCoherent),
    ("retained_shapes_disjoint_from_selectable",
      Json.bool receipt.retainedShapesDisjointFromSelectable),
    ("implication_references_resolve", Json.bool receipt.implicationReferencesResolve),
    ("contradiction_references_resolve", Json.bool receipt.contradictionReferencesResolve),
    ("all_entry_resolutions_passed", Json.bool receipt.allEntryResolutionsPassed),
    ("all_retained_pattern_resolutions_passed",
      Json.bool receipt.allRetainedPatternResolutionsPassed),
    ("all_names_resolved", Json.bool receipt.allNamesResolved),
    ("all_arities_resolved", Json.bool receipt.allAritiesResolved),
    ("all_type_and_instance_constraints_resolved",
      Json.bool receipt.allTypeAndInstanceConstraintsResolved),
    ("frozen_admission_is_empty", Json.bool receipt.frozenAdmissionIsEmpty),
    ("proposed_identity_already_admitted", Json.bool receipt.proposedIdentityAlreadyAdmitted),
    ("private_semantic_checker_available", Json.bool receipt.privateSemanticCheckerAvailable),
    ("semantic_success_conformance_performed",
      Json.bool receipt.semanticSuccessConformancePerformed),
    ("semantic_adversarial_conformance_performed",
      Json.bool receipt.semanticAdversarialConformancePerformed),
    ("activation_exposed", Json.bool receipt.activationExposed),
    ("proposal_resolution_passed", Json.bool receipt.proposalResolutionPassed)
  ]

private def n31ProposalResolutionJson
    (receipt : N31ProposalResolutionReceipt) : Json :=
  Json.mkObj [
    ("operation_id", Json.str receipt.operationId),
    ("identity", n31BankIdentityJson receipt.bank.identity),
    ("identity_project_and_bank_nonempty", Json.bool receipt.identityProjectAndBankNonempty),
    ("resolved_lean_hash_populated", Json.bool receipt.resolvedLeanHashPopulated),
    ("resolution_receipt_hash_populated", Json.bool receipt.resolutionReceiptHashPopulated),
    ("entry_ids_unique", Json.bool receipt.entryIdsUnique),
    ("retained_shape_ids_unique", Json.bool receipt.retainedShapeIdsUnique),
    ("entries", Json.arr (receipt.entries.map n31EntryResolutionJson)),
    ("retained_patterns",
      Json.arr (receipt.retainedPatterns.map n31RetainedPatternResolutionJson)),
    ("selectable_guard_definitions_coherent",
      Json.bool receipt.selectableGuardDefinitionsCoherent),
    ("retained_shapes_disjoint_from_selectable",
      Json.bool receipt.retainedShapesDisjointFromSelectable),
    ("implication_references_resolve", Json.bool receipt.implicationReferencesResolve),
    ("contradiction_references_resolve", Json.bool receipt.contradictionReferencesResolve),
    ("all_entry_resolutions_passed", Json.bool receipt.allEntryResolutionsPassed),
    ("all_retained_pattern_resolutions_passed",
      Json.bool receipt.allRetainedPatternResolutionsPassed),
    ("all_names_resolved", Json.bool receipt.allNamesResolved),
    ("all_arities_resolved", Json.bool receipt.allAritiesResolved),
    ("all_type_and_instance_constraints_resolved",
      Json.bool receipt.allTypeAndInstanceConstraintsResolved),
    ("frozen_admission_is_empty", Json.bool receipt.frozenAdmissionIsEmpty),
    ("proposed_identity_already_admitted", Json.bool receipt.proposedIdentityAlreadyAdmitted),
    ("private_semantic_checker_available", Json.bool receipt.privateSemanticCheckerAvailable),
    ("semantic_success_conformance_performed",
      Json.bool receipt.semanticSuccessConformancePerformed),
    ("semantic_adversarial_conformance_performed",
      Json.bool receipt.semanticAdversarialConformancePerformed),
    ("activation_exposed", Json.bool receipt.activationExposed),
    ("candidate_exposed", Json.bool n31ProposalCandidateExposed),
    ("proposal_resolution_passed", Json.bool receipt.proposalResolutionPassed)
  ]

private def n31ExternalHashInstallationContractJson : Json :=
  Json.mkObj [
    ("algorithm", Json.str "sha256"),
    ("canonicalization", Json.str "python_canonical_json_utf8_v1"),
    ("resolved_lean_hash_preimage_field", Json.str "bank_fingerprint_payload"),
    ("resolution_receipt_hash_preimage_field",
      Json.str "resolution_receipt_hash_preimage_payload"),
    ("identity_equality_rechecked_in_second_meta_request", Json.bool true),
    ("payload_digest_verification_owned_by_strict_runner", Json.bool true)
  ]

/-- First N31 request: resolve the complete proposal and emit two deterministic
    JSON bases.  Python hashes their canonical JSON to obtain the proposed
    `resolvedLeanHash` and `resolutionReceiptHash`.  Neither hash needs to be
    populated in this first request, and no candidate can be returned. -/
def emitN31ProposalResolutionReceipt
    (receiptId : String) (bank : N31TargetBank)
    (retainedWitnesses : Array N31RetainedPatternWitness) :
    MetaM N31ProposalResolutionReceipt := do
  let receipt ← resolveN31Proposal bank retainedWitnesses
  emitReceiptJson <| Json.mkObj [
    ("schema_version", toJson 1),
    ("receipt_kind", Json.str "n31_proposal_resolution"),
    ("receipt_id", Json.str receiptId),
    ("source_version", Json.str sourceVersion),
    ("proposal", n31ProposalResolutionJson receipt),
    ("bank_fingerprint_payload", n31StructuralBankFingerprintPayload receipt),
    ("resolution_receipt_hash_preimage_payload",
      n31ResolutionReceiptHashPreimagePayload receipt),
    ("external_hash_installation_contract", n31ExternalHashInstallationContractJson),
    ("candidate_constructed", Json.bool false),
    ("candidate_exposed", Json.bool false),
    ("semantic_conformance_performed", Json.bool false),
    ("row_or_gate_emitted", Json.bool false)
  ]
  return receipt

private def n31ReachabilityJson (reachability : N31ReachabilityEvidence) : Json :=
  Json.mkObj [
    ("mode_id", Json.str reachability.modeId),
    ("guard_ordinal", toJson reachability.guardOrdinal),
    ("assignment_expr_hashes", Json.arr (reachability.assignments.map exprHashJson))
  ]

/-- Second N31 request: the strict runner recomputes the two canonical JSON
    hashes, installs them in the bank identity, and supplies them separately.
    Lean checks exact identity equality, re-resolves the typed proposal, and
    proves that frozen public dispatch still terminates as `n31BankInvalid`.
    SHA256 computation remains an explicit external-runner obligation; no
    semantic success checker or candidate is exposed. -/
def emitN31FrozenNonActivationReceipt
    (receiptId : String) (source : Expr) (selector : Selector)
    (bank : N31TargetBank) (reachability : N31ReachabilityEvidence)
    (expectedResolvedLeanHash expectedResolutionReceiptHash : String)
    (retainedWitnesses : Array N31RetainedPatternWitness) :
    MetaM N31FrozenNonActivationReceipt := do
  let proposal ← resolveN31Proposal bank retainedWitnesses
  let receipt ← replayN31FrozenNonActivation
    source selector bank expectedResolvedLeanHash expectedResolutionReceiptHash
      reachability retainedWitnesses
  emitReceiptJson <| Json.mkObj [
    ("schema_version", toJson 1),
    ("receipt_kind", Json.str "n31_frozen_nonactivation"),
    ("receipt_id", Json.str receiptId),
    ("source_version", Json.str sourceVersion),
    ("operation_id", Json.str receipt.operationId),
    ("identity", n31BankIdentityJson receipt.bankIdentity),
    ("expected_resolved_lean_hash", Json.str receipt.expectedResolvedLeanHash),
    ("expected_resolution_receipt_hash",
      Json.str receipt.expectedResolutionReceiptHash),
    ("expected_hashes_nonempty", Json.bool receipt.expectedHashesNonempty),
    ("expected_hashes_are_lower_hex_sha256",
      Json.bool receipt.expectedHashesAreLowerHexSha256),
    ("identity_matches_expected_hashes",
      Json.bool receipt.identityMatchesExpectedHashes),
    ("external_hash_computation_performed_in_lean",
      Json.bool receipt.externalHashComputationPerformedInLean),
    ("external_strict_runner_hash_verification_required",
      Json.bool receipt.externalStrictRunnerHashVerificationRequired),
    ("source_expr_hash", exprHashJson source),
    ("selector", selectorJson selector),
    ("reachability", n31ReachabilityJson reachability),
    ("proposal", n31ProposalResolutionJson proposal),
    ("bank_fingerprint_payload", n31StructuralBankFingerprintPayload proposal),
    ("resolution_receipt_hash_preimage_payload",
      n31ResolutionReceiptHashPreimagePayload proposal),
    ("external_hash_installation_contract", n31ExternalHashInstallationContractJson),
    ("proposal_resolution_passed", Json.bool receipt.proposalResolutionPassed),
    ("frozen_admission_is_empty", Json.bool receipt.frozenAdmissionIsEmpty),
    ("identity_absent_from_frozen_admission",
      Json.bool receipt.identityAbsentFromFrozenAdmission),
    ("frozen_dispatch_rejected_as_unadmitted_bank",
      Json.bool receipt.frozenDispatchRejectedAsUnadmittedBank),
    ("rejection_reason", Json.str (failureReasonId receipt.rejectionReason)),
    ("private_semantic_checker_available",
      Json.bool receipt.privateSemanticCheckerAvailable),
    ("semantic_conformance_performed", Json.bool receipt.semanticConformancePerformed),
    ("candidate_constructed", Json.bool receipt.candidateConstructed),
    ("candidate_exposed", Json.bool false),
    ("activation_exposed", Json.bool receipt.activationExposed),
    ("row_or_gate_emitted", Json.bool false)
  ]
  return receipt

end LeanFaith.SFT1.Wave1Runtime
