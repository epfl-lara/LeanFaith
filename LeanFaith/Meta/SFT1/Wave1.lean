/-
SFT1 Wave 1 typed transformation engine, source revision v0.3.4.

This task-owned file is deliberately import-strippable.  It contains only
closed-Expr transformation, applicability, dispatch, and replay definitions
for the five primary Wave 1 mechanisms.  It does not render or pretty-print
expressions, elaborate text, hash files, spawn processes, declare endpoints,
construct rows, or implement the optional N31 N-PROOF evidence upgrade.

Universe levels are never renamed here.  Reference and candidate Exprs remain
alive together for the caller to pass directly to the frozen REPR emitter.
-/
import Lean

namespace LeanFaith.SFT1.Wave1

open Lean Meta

def sourceVersion : String := "sft1_wave1_expr_engine_v0_3_4"

inductive PrimaryOperation where
  | p01AlphaRenameSingle
  | p15SwapIffSides
  | p18SymmetrizeEquality
  | p21BetaReduce
  | n31DropRequiredGuardRubric
  deriving BEq, DecidableEq, Inhabited, Repr

def PrimaryOperation.operationId : PrimaryOperation → String
  | .p01AlphaRenameSingle => "P01_ALPHA_RENAME_SINGLE_V1"
  | .p15SwapIffSides => "P15_SWAP_IFF_SIDES_V1"
  | .p18SymmetrizeEquality => "P18_SYMMETRIZE_EQUALITY_V1"
  | .p21BetaReduce => "P21_BETA_REDUCE_V1"
  | .n31DropRequiredGuardRubric => "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"

inductive Selector where
  /-- Zero-based ordinal among every outer Pi binder. -/
  | outerBinder (ordinal : Nat)
  /-- The final target after the exact, non-reducing outer Pi telescope. -/
  | outerTarget
  /-- A `Lean.SubExpr.Pos` relative to the complete source proposition. -/
  | subexpr (pos : SubExpr.Pos)
  /-- `targetPos` is relative to the final target after the outer telescope. -/
  | requiredGuard (guardOrdinal : Nat) (targetPos : SubExpr.Pos) (bankEntryId : String)
  deriving BEq, Inhabited, Repr

inductive FailureReason where
  | sourceNotClosedProp
  | candidateNotClosedProp
  | selectorDoesNotMatchOperation
  | selectedSiteMissing
  | operationNotApplicable
  | nonHygienicBinderName
  | degenerateOperands
  | exactDeltaMismatch
  | expectedDefinitionalEqualityMissing
  | forbiddenDefinitionalEquality
  | claimCollapsesToTrue
  | claimCollapsesToFalse
  | n31BankMissing
  | n31BankInvalid
  | n31BankEntryMissingOrAmbiguous
  | n31GuardMissingOrAmbiguous
  | n31GuardNotNamedExplicitProp
  | n31GuardDefinitionallyTrue
  | n31GuardProofUsedInContinuation
  | n31TargetMissingOrAmbiguous
  | n31CompetingGuard
  | n31RetainedContradiction
  | n31RetainedContextUnknownOrAmbiguous
  | n31ReachabilityMissing
  | n31ReachabilityInvalid
  | n31BodyDefinitionallyTrue
  | replayContextMismatch
  | replayCertificateMismatch
  | replayCandidateMismatch
  deriving BEq, Inhabited, Repr

structure N31FixedHeadConstraint where
  argumentIndex : Nat
  headName : Name
  argumentCount : Nat
  deriving BEq, Inhabited, Repr

/-
Paths descend through fully elaborated application-argument arrays, including
implicit arguments.  An empty path denotes the complete expression; banks use
nonempty paths for roles, instances, nested heads, literals, and exact fixed
terms because the root head and arity are recorded separately.
-/
structure N31ApplicationArgumentPath where
  argumentIndices : Array Nat
  deriving BEq, Inhabited, Repr

structure N31NestedHeadConstraint where
  path : N31ApplicationArgumentPath
  headName : Name
  argumentCount : Nat
  deriving BEq, Inhabited, Repr

structure N31ExactExprConstraint where
  path : N31ApplicationArgumentPath
  expected : Expr
  deriving Inhabited, Repr

instance : BEq N31ExactExprConstraint where
  beq left right := left.path == right.path && Expr.equal left.expected right.expected

structure N31LiteralConstraint where
  path : N31ApplicationArgumentPath
  expected : Literal
  deriving BEq, Inhabited, Repr

/-
The strict task-owned loader supplies these entries from a hash-bound bank.
Role and instance arrays are ordered: equality between two matches is checked
position-by-position with `Expr.equal`, never by pretty text or proof search.
-/
structure N31TargetBankEntry where
  entryId : String
  guardShapeId : String
  guardHeadName : Name
  guardArgumentCount : Nat
  guardRoleArgumentIndices : Array Nat
  guardInstanceArgumentIndices : Array Nat
  guardFixedHeads : Array N31FixedHeadConstraint
  guardNestedHeadConstraints : Array N31NestedHeadConstraint
  guardLiteralConstraints : Array N31LiteralConstraint
  guardExactExprConstraints : Array N31ExactExprConstraint
  targetHeadName : Name
  targetArgumentCount : Nat
  targetRoleArgumentIndices : Array Nat
  targetInstanceArgumentIndices : Array Nat
  targetFixedHeads : Array N31FixedHeadConstraint
  targetNestedHeadConstraints : Array N31NestedHeadConstraint
  targetLiteralConstraints : Array N31LiteralConstraint
  targetExactExprConstraints : Array N31ExactExprConstraint
  deriving BEq, Inhabited, Repr

/-
These patterns recognize retained hypotheses which contradict a removable
guard.  They are intentionally disjoint from `N31TargetBankEntry`: a retained
contradiction pattern has no target transform and is never selectable by
`discover` or `dispatchAt`.
-/
structure N31RetainedContradictionPattern where
  shapeId : String
  headName : Name
  argumentCount : Nat
  rolePaths : Array N31ApplicationArgumentPath
  instancePaths : Array N31ApplicationArgumentPath
  nestedHeadConstraints : Array N31NestedHeadConstraint
  literalConstraints : Array N31LiteralConstraint
  exactExprConstraints : Array N31ExactExprConstraint
  deriving BEq, Inhabited, Repr

structure N31ImplicationRule where
  premiseShapeId : String
  conclusionShapeId : String
  deriving BEq, Inhabited, Repr

structure N31ContradictionRule where
  retainedShapeId : String
  removedShapeId : String
  deriving BEq, Inhabited, Repr

structure N31BankIdentity where
  projectId : String
  bankId : String
  resolvedLeanHash : String
  resolutionReceiptHash : String
  deriving BEq, Inhabited, Repr

/-
Revision v0.3.4 intentionally admits no resolved N31 bank.  A future additive,
user-authorized hash-pinning revision must replace this exact empty array before
N31 dispatch can become applicable.
-/
def admittedN31BankIdentitiesV0_3_4 : Array N31BankIdentity := #[]

structure N31TargetBank where
  identity : N31BankIdentity
  entries : Array N31TargetBankEntry
  retainedContradictionPatterns : Array N31RetainedContradictionPattern
  implications : Array N31ImplicationRule
  contradictions : Array N31ContradictionRule
  deriving BEq, Inhabited, Repr

/-
An explicit assignment contains one closed term for every outer Pi binder, in
source order.  Replaying their dependent types witnesses that the guarded
context is reachable; it does not prove the final target.
-/
structure N31ReachabilityEvidence where
  modeId : String
  guardOrdinal : Nat
  assignments : Array Expr
  deriving Inhabited, Repr

private partial def exactExprArrayBEqAux
    (left right : Array Expr) (index : Nat) : Bool :=
  if index < left.size then
    index < right.size && Expr.equal left[index]! right[index]! &&
      exactExprArrayBEqAux left right (index + 1)
  else
    right.size == left.size

private def exactExprArrayBEq (left right : Array Expr) : Bool :=
  exactExprArrayBEqAux left right 0

instance : BEq N31ReachabilityEvidence where
  beq left right :=
    left.modeId == right.modeId && left.guardOrdinal == right.guardOrdinal &&
      exactExprArrayBEq left.assignments right.assignments

structure DispatchContext where
  n31Bank : Option N31TargetBank := none
  n31Reachability : Option N31ReachabilityEvidence := none
  deriving BEq, Inhabited, Repr

structure P01Certificate where
  binderOrdinal : Nat
  binderSite : SubExpr.Pos
  sourceName : Name
  candidateName : Name
  binderInfo : BinderInfo
  deriving BEq, Inhabited, Repr

structure P15Certificate where
  targetSite : SubExpr.Pos
  deriving BEq, Inhabited, Repr

structure P18Certificate where
  targetSite : SubExpr.Pos
  deriving BEq, Inhabited, Repr

structure P21Certificate where
  redexSite : SubExpr.Pos
  deriving BEq, Inhabited, Repr

structure N31RubricCertificate where
  guardOrdinal : Nat
  guardSite : SubExpr.Pos
  targetSite : SubExpr.Pos
  bankEntryId : String
  guardShapeId : String
  bank : N31TargetBank
  reachability : N31ReachabilityEvidence
  deriving BEq, Inhabited, Repr

inductive Certificate where
  | p01 (value : P01Certificate)
  | p15 (value : P15Certificate)
  | p18 (value : P18Certificate)
  | p21 (value : P21Certificate)
  | n31Rubric (value : N31RubricCertificate)
  deriving BEq, Inhabited, Repr

structure Candidate where
  operation : PrimaryOperation
  selector : Selector
  candidate : Expr
  certificate : Certificate
  deriving Inhabited, Repr

inductive ApplyResult where
  | applicable (value : Candidate)
  | typedNotApplicable (reason : FailureReason)
  deriving Inhabited, Repr

structure ReplayResult where
  passed : Bool
  operation : PrimaryOperation
  reason : Option FailureReason
  deriving Inhabited, Repr

private def cleanExprResidue (e : Expr) : Bool :=
  !e.hasExprMVar && !e.hasLevelMVar && !e.hasFVar && !e.hasLooseBVars && !e.hasSorry

private def checkedClosedTerm (e : Expr) : MetaM Bool := do
  if !cleanExprResidue e then
    return false
  try
    check e
    return true
  catch ex =>
    if ex.isInterrupt || ex.isRuntime then
      throw ex
    return false

private def checkedClosedProp (e : Expr) : MetaM Bool := do
  if !(← checkedClosedTerm e) then
    return false
  try
    isProp e
  catch ex =>
    if ex.isInterrupt || ex.isRuntime then
      throw ex
    return false

private def defEqNoAssign
    (left right : Expr) (transparency : TransparencyMode := .reducible) : MetaM Bool := do
  try
    withoutModifyingMCtx do
      withTransparency transparency <| isDefEq left right
  catch ex =>
    if ex.isInterrupt || ex.isRuntime then
      throw ex
    return false

private partial def stripMData : Expr → Expr
  | .mdata _ body => stripMData body
  | e => e

private partial def eraseMDataDeep : Expr → Expr
  | .mdata _ body => eraseMDataDeep body
  | .app fn arg => .app (eraseMDataDeep fn) (eraseMDataDeep arg)
  | .lam name domain body binderInfo =>
      .lam name (eraseMDataDeep domain) (eraseMDataDeep body) binderInfo
  | .forallE name domain body binderInfo =>
      .forallE name (eraseMDataDeep domain) (eraseMDataDeep body) binderInfo
  | .letE name type value body nondep =>
      .letE name (eraseMDataDeep type) (eraseMDataDeep value)
        (eraseMDataDeep body) nondep
  | .proj typeName index base => .proj typeName index (eraseMDataDeep base)
  | e => e

private def exactExprEqualIgnoringMData (left right : Expr) : Bool :=
  Expr.equal (eraseMDataDeep left) (eraseMDataDeep right)

private partial def termPositions
    (e : Expr) (pos : SubExpr.Pos := .root) : Array SubExpr.Pos :=
  match e with
  | .mdata _ body => termPositions body pos
  | .app fn arg =>
      #[pos] ++ termPositions fn pos.pushAppFn ++ termPositions arg pos.pushAppArg
  | .lam _ domain body _ | .forallE _ domain body _ =>
      #[pos] ++ termPositions domain pos.pushBindingDomain ++
        termPositions body pos.pushBindingBody
  | .letE _ type value body _ =>
      #[pos] ++ termPositions type pos.pushLetVarType ++
        termPositions value pos.pushLetValue ++ termPositions body pos.pushLetBody
  | .proj _ _ base => #[pos] ++ termPositions base pos.pushProj
  | _ => #[pos]

private partial def binderDisplayNames : Expr → Array String
  | .forallE name domain body _ | .lam name domain body _ =>
      let current :=
        if name.isAnonymous then #[] else #[name.eraseMacroScopes.toString]
      current ++ binderDisplayNames domain ++ binderDisplayNames body
  | .letE name type value body _ =>
      let current :=
        if name.isAnonymous then #[] else #[name.eraseMacroScopes.toString]
      current ++ binderDisplayNames type ++ binderDisplayNames value ++ binderDisplayNames body
  | .app fn arg => binderDisplayNames fn ++ binderDisplayNames arg
  | .proj _ _ base => binderDisplayNames base
  | .mdata _ body => binderDisplayNames body
  | _ => #[]

private partial def freshAlphaName
    (used : Array String) (index : Nat := 0) : Name :=
  let text := if index == 0 then "x" else s!"x_{index}"
  if used.contains text then
    freshAlphaName used (index + 1)
  else
    Name.mkSimple text

private partial def outerBinderCount : Expr → Nat
  | .mdata _ body => outerBinderCount body
  | .forallE _ _ body _ => outerBinderCount body + 1
  | _ => 0

private partial def outerBinderSite : Nat → SubExpr.Pos
  | 0 => .root
  | ordinal + 1 => (outerBinderSite ordinal).pushBindingBody

private partial def outerTargetSite
    (e : Expr) (pos : SubExpr.Pos := .root) : SubExpr.Pos :=
  match e with
  | .mdata _ body => outerTargetSite body pos
  | .forallE _ _ body _ => outerTargetSite body pos.pushBindingBody
  | _ => pos

private partial def rawOuterTarget : Expr → Expr
  | .mdata _ body => rawOuterTarget body
  | .forallE _ _ body _ => rawOuterTarget body
  | e => e

private partial def renameOuterBinderAt?
    (e : Expr) (ordinal : Nat) (candidateName : Name) : Option (Expr × Name × BinderInfo) :=
  match e with
  | .mdata data body =>
      match renameOuterBinderAt? body ordinal candidateName with
      | some (candidate, sourceName, binderInfo) =>
          some (.mdata data candidate, sourceName, binderInfo)
      | none => none
  | .forallE sourceName domain body binderInfo =>
      if ordinal == 0 then
        if binderInfo != .default || sourceName.isAnonymous || sourceName.hasMacroScopes then
          none
        else
          some (.forallE candidateName domain body binderInfo, sourceName, binderInfo)
      else
        match renameOuterBinderAt? body (ordinal - 1) candidateName with
        | some (candidateBody, selectedName, selectedInfo) =>
            some (.forallE sourceName domain candidateBody binderInfo, selectedName, selectedInfo)
        | none => none
  | _ => none

private structure FinalBinaryRewrite where
  candidate : Expr
  left : Expr
  right : Expr

private partial def swapFinalIff? : Expr → Option FinalBinaryRewrite
  | .mdata data body =>
      match swapFinalIff? body with
      | some result => some { result with candidate := .mdata data result.candidate }
      | none => none
  | .forallE name domain body binderInfo =>
      match swapFinalIff? body with
      | some result =>
          some { result with candidate := .forallE name domain result.candidate binderInfo }
      | none => none
  | .app (.app (.const name levels) left) right =>
      if name == ``Iff && levels.isEmpty && !Expr.eqv left right then
        some {
          candidate := .app (.app (.const name levels) right) left
          left
          right
        }
      else
        none
  | _ => none

private partial def symmetrizeFinalEq? : Expr → Option FinalBinaryRewrite
  | .mdata data body =>
      match symmetrizeFinalEq? body with
      | some result => some { result with candidate := .mdata data result.candidate }
      | none => none
  | .forallE name domain body binderInfo =>
      match symmetrizeFinalEq? body with
      | some result =>
          some { result with candidate := .forallE name domain result.candidate binderInfo }
      | none => none
  | .app (.app (.app (.const name levels) type) left) right =>
      if name == ``Eq && !Expr.eqv left right then
        some {
          candidate := .app (.app (.app (.const name levels) type) right) left
          left
          right
        }
      else
        none
  | _ => none

private partial def betaReduceNode? : Expr → Option Expr
  | .mdata data body =>
      match betaReduceNode? body with
      | some candidate => some (.mdata data candidate)
      | none => none
  | .app (.lam _ _ body .default) argument =>
      if cleanExprResidue argument && body.hasLooseBVar 0 then
        some (body.instantiate1 argument)
      else
        none
  | _ => none

private partial def removeOuterGuardAt? (e : Expr) (ordinal : Nat) : Option Expr :=
  match e with
  | .mdata data body =>
      match removeOuterGuardAt? body ordinal with
      | some candidate => some (.mdata data candidate)
      | none => none
  | .forallE name domain body binderInfo =>
      if ordinal == 0 then
        if binderInfo != .default || name.isAnonymous || name.hasMacroScopes ||
            body.hasLooseBVar 0 then
          none
        else
          some (body.lowerLooseBVars 1 1)
      else
        match removeOuterGuardAt? body (ordinal - 1) with
        | some candidateBody => some (.forallE name domain candidateBody binderInfo)
        | none => none
  | _ => none

private structure ConstApplication where
  headName : Name
  argumentCount : Nat
  arguments : Array Expr

private def constApplication? (e : Expr) : Option ConstApplication :=
  let core := stripMData e
  match core.getAppFn with
  | .const name _ =>
      let arguments := core.getAppArgs
      some { headName := name, argumentCount := arguments.size, arguments }
  | _ => none

private partial def followApplicationArgumentPathAux?
    (e : Expr) (indices : Array Nat) (cursor : Nat) : Option Expr :=
  if cursor < indices.size then
    let application ← constApplication? e
    let argumentIndex := indices[cursor]!
    if argumentIndex < application.arguments.size then
      followApplicationArgumentPathAux?
        application.arguments[argumentIndex]! indices (cursor + 1)
    else
      none
  else
    some (stripMData e)

private def followApplicationArgumentPath?
    (e : Expr) (path : N31ApplicationArgumentPath) : Option Expr :=
  followApplicationArgumentPathAux? e path.argumentIndices 0

private partial def selectApplicationPathsAux?
    (e : Expr) (paths : Array N31ApplicationArgumentPath) (cursor : Nat)
    (result : Array Expr) : Option (Array Expr) :=
  if cursor < paths.size then
    let selected ← followApplicationArgumentPath? e paths[cursor]!
    selectApplicationPathsAux? e paths (cursor + 1) (result.push selected)
  else
    some result

private def selectApplicationPaths?
    (e : Expr) (paths : Array N31ApplicationArgumentPath) : Option (Array Expr) :=
  selectApplicationPathsAux? e paths 0 #[]

private def nestedHeadsMatch
    (e : Expr) (constraints : Array N31NestedHeadConstraint) : Bool :=
  constraints.all fun constraint =>
    match followApplicationArgumentPath? e constraint.path with
    | none => false
    | some selected =>
        match constApplication? selected with
        | none => false
        | some application =>
            application.headName == constraint.headName &&
              application.argumentCount == constraint.argumentCount

private def literalsMatch
    (e : Expr) (constraints : Array N31LiteralConstraint) : Bool :=
  constraints.all fun constraint =>
    match followApplicationArgumentPath? e constraint.path with
    | some (.lit observed) => observed == constraint.expected
    | _ => false

private def exactExprConstraintsMatch
    (e : Expr) (constraints : Array N31ExactExprConstraint) : Bool :=
  constraints.all fun constraint =>
    match followApplicationArgumentPath? e constraint.path with
    | some observed => exactExprEqualIgnoringMData observed constraint.expected
    | none => false

private partial def selectArgumentsAux
    (arguments : Array Expr) (indices : Array Nat) (cursor : Nat)
    (result : Array Expr) : Option (Array Expr) :=
  if cursor < indices.size then
    let index := indices[cursor]!
    if index < arguments.size then
      selectArgumentsAux arguments indices (cursor + 1) (result.push arguments[index]!)
    else
      none
  else
    some result

private def selectArguments?
    (arguments : Array Expr) (indices : Array Nat) : Option (Array Expr) :=
  selectArgumentsAux arguments indices 0 #[]

private partial def exprArraysEqualAux
    (left right : Array Expr) (index : Nat) : Bool :=
  if index < left.size then
    index < right.size && exactExprEqualIgnoringMData left[index]! right[index]! &&
      exprArraysEqualAux left right (index + 1)
  else
    right.size == left.size

private def exprArraysEqual (left right : Array Expr) : Bool :=
  exprArraysEqualAux left right 0

private def natArrayUnique (values : Array Nat) : Bool :=
  values.toList.eraseDups.length == values.size

private def natArraysDisjoint (left right : Array Nat) : Bool :=
  left.all fun value => !right.contains value

private def stringArrayUnique (values : Array String) : Bool :=
  values.toList.eraseDups.length == values.size

private def applicationPathValid (path : N31ApplicationArgumentPath) : Bool :=
  !path.argumentIndices.isEmpty

private def applicationPathArrayUnique
    (paths : Array N31ApplicationArgumentPath) : Bool :=
  paths.toList.eraseDups.length == paths.size

private def applicationPathArraysDisjoint
    (left right : Array N31ApplicationArgumentPath) : Bool :=
  left.all fun path => !right.contains path

private def nestedHeadConstraintsValid
    (constraints : Array N31NestedHeadConstraint) : Bool :=
  applicationPathArrayUnique (constraints.map (·.path)) &&
    constraints.all fun constraint =>
      applicationPathValid constraint.path && !constraint.headName.isAnonymous

private def literalConstraintsValid
    (constraints : Array N31LiteralConstraint) : Bool :=
  applicationPathArrayUnique (constraints.map (·.path)) &&
    constraints.all fun constraint => applicationPathValid constraint.path

private def exactExprConstraintsStructurallyValid
    (constraints : Array N31ExactExprConstraint) : Bool :=
  applicationPathArrayUnique (constraints.map (·.path)) &&
    constraints.all fun constraint => applicationPathValid constraint.path

private def exactExprConstraintsClosed
    (constraints : Array N31ExactExprConstraint) : MetaM Bool := do
  for constraint in constraints do
    if !(← checkedClosedTerm constraint.expected) then
      return false
  return true

private def fixedHeadsValid
    (argumentCount : Nat) (constraints : Array N31FixedHeadConstraint) : Bool :=
  natArrayUnique (constraints.map (·.argumentIndex)) &&
    constraints.all fun constraint =>
      constraint.argumentIndex < argumentCount && !constraint.headName.isAnonymous

private def fixedHeadsMatch
    (arguments : Array Expr) (constraints : Array N31FixedHeadConstraint) : Bool :=
  constraints.all fun constraint =>
    if constraint.argumentIndex < arguments.size then
      match constApplication? arguments[constraint.argumentIndex]! with
      | some application =>
          application.headName == constraint.headName &&
            application.argumentCount == constraint.argumentCount
      | none => false
    else
      false

private def bankEntryStructurallyValid (entry : N31TargetBankEntry) : Bool :=
  !entry.entryId.isEmpty && !entry.guardShapeId.isEmpty &&
    !entry.guardHeadName.isAnonymous && !entry.targetHeadName.isAnonymous &&
    entry.guardRoleArgumentIndices.size > 0 &&
    entry.guardInstanceArgumentIndices.size > 0 &&
    entry.targetInstanceArgumentIndices.size > 0 &&
    entry.guardRoleArgumentIndices.size == entry.targetRoleArgumentIndices.size &&
    entry.guardInstanceArgumentIndices.size == entry.targetInstanceArgumentIndices.size &&
    natArrayUnique entry.guardRoleArgumentIndices &&
    natArrayUnique entry.guardInstanceArgumentIndices &&
    natArrayUnique entry.targetRoleArgumentIndices &&
    natArrayUnique entry.targetInstanceArgumentIndices &&
    natArraysDisjoint entry.guardRoleArgumentIndices entry.guardInstanceArgumentIndices &&
    natArraysDisjoint entry.targetRoleArgumentIndices entry.targetInstanceArgumentIndices &&
    entry.guardRoleArgumentIndices.all (· < entry.guardArgumentCount) &&
    entry.guardInstanceArgumentIndices.all (· < entry.guardArgumentCount) &&
    entry.targetRoleArgumentIndices.all (· < entry.targetArgumentCount) &&
    entry.targetInstanceArgumentIndices.all (· < entry.targetArgumentCount) &&
    fixedHeadsValid entry.guardArgumentCount entry.guardFixedHeads &&
    fixedHeadsValid entry.targetArgumentCount entry.targetFixedHeads &&
    nestedHeadConstraintsValid entry.guardNestedHeadConstraints &&
    nestedHeadConstraintsValid entry.targetNestedHeadConstraints &&
    literalConstraintsValid entry.guardLiteralConstraints &&
    literalConstraintsValid entry.targetLiteralConstraints &&
    exactExprConstraintsStructurallyValid entry.guardExactExprConstraints &&
    exactExprConstraintsStructurallyValid entry.targetExactExprConstraints

private def bankEntryValid (entry : N31TargetBankEntry) : MetaM Bool := do
  if !bankEntryStructurallyValid entry then
    return false
  if !(← exactExprConstraintsClosed entry.guardExactExprConstraints) then
    return false
  exactExprConstraintsClosed entry.targetExactExprConstraints

private def retainedContradictionPatternStructurallyValid
    (pattern : N31RetainedContradictionPattern) : Bool :=
  !pattern.shapeId.isEmpty && !pattern.headName.isAnonymous &&
    pattern.rolePaths.size > 0 &&
    pattern.instancePaths.size > 0 &&
    applicationPathArrayUnique pattern.rolePaths &&
    applicationPathArrayUnique pattern.instancePaths &&
    pattern.rolePaths.all applicationPathValid &&
    pattern.instancePaths.all applicationPathValid &&
    applicationPathArraysDisjoint pattern.rolePaths pattern.instancePaths &&
    nestedHeadConstraintsValid pattern.nestedHeadConstraints &&
    literalConstraintsValid pattern.literalConstraints &&
    exactExprConstraintsStructurallyValid pattern.exactExprConstraints &&
    applicationPathArraysDisjoint pattern.rolePaths
      (pattern.exactExprConstraints.map (·.path)) &&
    applicationPathArraysDisjoint pattern.instancePaths
      (pattern.exactExprConstraints.map (·.path))

private def retainedContradictionPatternValid
    (pattern : N31RetainedContradictionPattern) : MetaM Bool := do
  if !retainedContradictionPatternStructurallyValid pattern then
    return false
  exactExprConstraintsClosed pattern.exactExprConstraints

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

private def selectableGuardDefinitionsCoherent (bank : N31TargetBank) : Bool :=
  bank.entries.all fun left =>
    bank.entries.all fun right =>
      left.guardShapeId != right.guardShapeId || selectableGuardDefinitionsEqual left right

private def selectableShapeExists (bank : N31TargetBank) (shapeId : String) : Bool :=
  bank.entries.any fun entry => entry.guardShapeId == shapeId

private def n31BankIdentityStructurallyValid (identity : N31BankIdentity) : Bool :=
  !identity.projectId.isEmpty && !identity.bankId.isEmpty &&
    !identity.resolvedLeanHash.isEmpty && !identity.resolutionReceiptHash.isEmpty

private def implicationRuleValid
    (bank : N31TargetBank) (rule : N31ImplicationRule) : Bool :=
  let premiseEntries := bank.entries.filter fun entry =>
    entry.guardShapeId == rule.premiseShapeId
  let conclusionEntries := bank.entries.filter fun entry =>
    entry.guardShapeId == rule.conclusionShapeId
  !premiseEntries.isEmpty && !conclusionEntries.isEmpty &&
    premiseEntries.all fun premise =>
      conclusionEntries.all fun conclusion =>
        premise.guardRoleArgumentIndices.size ==
            conclusion.guardRoleArgumentIndices.size &&
          premise.guardInstanceArgumentIndices.size ==
            conclusion.guardInstanceArgumentIndices.size

private def findRetainedContradictionPattern?
    (bank : N31TargetBank) (shapeId : String) : Option N31RetainedContradictionPattern :=
  match (bank.retainedContradictionPatterns.filter fun pattern =>
      pattern.shapeId == shapeId).toList with
  | [pattern] => some pattern
  | _ => none

private def retainedContradictionShapeExists
    (bank : N31TargetBank) (shapeId : String) : Bool :=
  (findRetainedContradictionPattern? bank shapeId).isSome

private def contradictionRuleValid
    (bank : N31TargetBank) (rule : N31ContradictionRule) : Bool :=
  match findRetainedContradictionPattern? bank rule.retainedShapeId with
  | none => false
  | some pattern =>
      let removedEntries := bank.entries.filter fun entry =>
        entry.guardShapeId == rule.removedShapeId
      !removedEntries.isEmpty && removedEntries.all fun entry =>
        pattern.rolePaths.size == entry.guardRoleArgumentIndices.size &&
          pattern.instancePaths.size == entry.guardInstanceArgumentIndices.size

private def n31BankValid (bank : N31TargetBank) : MetaM Bool := do
  if !n31BankIdentityStructurallyValid bank.identity ||
      !admittedN31BankIdentitiesV0_3_4.contains bank.identity ||
      bank.entries.isEmpty ||
      !stringArrayUnique (bank.entries.map (·.entryId)) ||
      !stringArrayUnique (bank.retainedContradictionPatterns.map (·.shapeId)) ||
      !selectableGuardDefinitionsCoherent bank then
    return false
  for entry in bank.entries do
    if !(← bankEntryValid entry) then
      return false
  for pattern in bank.retainedContradictionPatterns do
    if !(← retainedContradictionPatternValid pattern) ||
        selectableShapeExists bank pattern.shapeId then
      return false
  if !bank.implications.all (implicationRuleValid bank) then
    return false
  return bank.contradictions.all fun rule =>
    retainedContradictionShapeExists bank rule.retainedShapeId &&
      selectableShapeExists bank rule.removedShapeId &&
      contradictionRuleValid bank rule

private def findBankEntry?
    (bank : N31TargetBank) (entryId : String) : Option N31TargetBankEntry :=
  match (bank.entries.filter fun entry => entry.entryId == entryId).toList with
  | [entry] => some entry
  | _ => none

private structure GuardMatch where
  roles : Array Expr
  instances : Array Expr

private def matchGuard?
    (entry : N31TargetBankEntry) (guardType : Expr) : Option GuardMatch := do
  let application ← constApplication? guardType
  if application.headName != entry.guardHeadName ||
      application.argumentCount != entry.guardArgumentCount ||
      !fixedHeadsMatch application.arguments entry.guardFixedHeads ||
      !nestedHeadsMatch guardType entry.guardNestedHeadConstraints ||
      !literalsMatch guardType entry.guardLiteralConstraints ||
      !exactExprConstraintsMatch guardType entry.guardExactExprConstraints then
    none
  else
    let roles ← selectArguments? application.arguments entry.guardRoleArgumentIndices
    let instances ← selectArguments? application.arguments entry.guardInstanceArgumentIndices
    some { roles, instances }

private def matchRetainedContradictionPattern?
    (pattern : N31RetainedContradictionPattern) (guardType : Expr) : Option GuardMatch := do
  let application ← constApplication? guardType
  if application.headName != pattern.headName ||
      application.argumentCount != pattern.argumentCount ||
      !nestedHeadsMatch guardType pattern.nestedHeadConstraints ||
      !literalsMatch guardType pattern.literalConstraints ||
      !exactExprConstraintsMatch guardType pattern.exactExprConstraints then
    none
  else
    let roles ← selectApplicationPaths? guardType pattern.rolePaths
    let instances ← selectApplicationPaths? guardType pattern.instancePaths
    some { roles, instances }

private def targetMatches
    (entry : N31TargetBankEntry) (guard : GuardMatch) (target : Expr) : Bool :=
  match constApplication? target with
  | none => false
  | some application =>
      if application.headName != entry.targetHeadName ||
          application.argumentCount != entry.targetArgumentCount ||
          !fixedHeadsMatch application.arguments entry.targetFixedHeads ||
          !nestedHeadsMatch target entry.targetNestedHeadConstraints ||
          !literalsMatch target entry.targetLiteralConstraints ||
          !exactExprConstraintsMatch target entry.targetExactExprConstraints then
        false
      else
        match selectArguments? application.arguments entry.targetRoleArgumentIndices,
            selectArguments? application.arguments entry.targetInstanceArgumentIndices with
        | some targetRoles, some targetInstances =>
            exprArraysEqual guard.roles targetRoles &&
              exprArraysEqual guard.instances targetInstances
        | _, _ => false

private def implicationApplies
    (bank : N31TargetBank) (premise conclusion : String) : Bool :=
  premise == conclusion || bank.implications.any fun rule =>
    rule.premiseShapeId == premise && rule.conclusionShapeId == conclusion

private def contradictionApplies
    (bank : N31TargetBank) (retained removed : String) : Bool :=
  bank.contradictions.any fun rule =>
    rule.retainedShapeId == retained && rule.removedShapeId == removed

private def matchingGuardShapeIds
    (bank : N31TargetBank) (guardType : Expr) : Array String := Id.run do
  let mut result := #[]
  for entry in bank.entries do
    if (matchGuard? entry guardType).isSome && !result.contains entry.guardShapeId then
      result := result.push entry.guardShapeId
  return result

private def matchingRetainedContradictionShapeIds
    (bank : N31TargetBank) (guardType : Expr) : Array String := Id.run do
  let mut result := #[]
  for pattern in bank.retainedContradictionPatterns do
    if (matchRetainedContradictionPattern? pattern guardType).isSome then
      result := result.push pattern.shapeId
  return result

private def retainedContextRejects
    (bank : N31TargetBank) (selectedEntry : N31TargetBankEntry)
    (selectedGuard : GuardMatch) (locals : Array Expr) (guardOrdinal : Nat) : MetaM FailureReason := do
  let mut hasUnknownOrAmbiguous := false
  let mut hasCompetingGuard := false
  let mut hasRetainedContradiction := false
  for index in [0 : locals.size] do
    if index != guardOrdinal then
      let local := locals[index]!
      let localType ← inferType local
      if ← isProp localType then
        let selectableShapeIds := matchingGuardShapeIds bank localType
        let contradictionShapeIds :=
          matchingRetainedContradictionShapeIds bank localType
        if selectableShapeIds.size + contradictionShapeIds.size != 1 then
          hasUnknownOrAmbiguous := true
        else if selectableShapeIds.size == 1 then
          let retainedShapeId := selectableShapeIds[0]!
          for entry in bank.entries do
            if entry.guardShapeId == retainedShapeId then
              if let some retainedGuard := matchGuard? entry localType then
                if exprArraysEqual retainedGuard.roles selectedGuard.roles &&
                    exprArraysEqual retainedGuard.instances selectedGuard.instances &&
                    implicationApplies bank retainedShapeId selectedEntry.guardShapeId then
                  hasCompetingGuard := true
        else
          let retainedShapeId := contradictionShapeIds[0]!
          for pattern in bank.retainedContradictionPatterns do
            if pattern.shapeId == retainedShapeId then
              if let some retainedGuard :=
                  matchRetainedContradictionPattern? pattern localType then
                if exprArraysEqual retainedGuard.roles selectedGuard.roles &&
                    exprArraysEqual retainedGuard.instances selectedGuard.instances &&
                    contradictionApplies bank retainedShapeId selectedEntry.guardShapeId then
                  hasRetainedContradiction := true
  if hasUnknownOrAmbiguous then
    return .n31RetainedContextUnknownOrAmbiguous
  if hasRetainedContradiction then
    return .n31RetainedContradiction
  if hasCompetingGuard then
    return .n31CompetingGuard
  return .operationNotApplicable

private def countMatchingTargets
    (target : Expr) (bank : N31TargetBank) (entry : N31TargetBankEntry) (guard : GuardMatch)
    (selected : SubExpr.Pos) : MetaM (Nat × Bool) := do
  let mut count := 0
  let mut selectedMatches := false
  for pos in termPositions target do
    let matches ←
      try
        Meta.viewSubexpr (fun _ subexpr => do
          let mut anyShapeTargetMatches := false
          let mut selectedEntryMatches := false
          for candidateEntry in bank.entries do
            if implicationApplies bank entry.guardShapeId candidateEntry.guardShapeId &&
                targetMatches candidateEntry guard subexpr then
              anyShapeTargetMatches := true
              if candidateEntry.entryId == entry.entryId then
                selectedEntryMatches := true
          pure (anyShapeTargetMatches, selectedEntryMatches)) pos target
      catch ex =>
        if ex.isInterrupt || ex.isRuntime then
          throw ex
        pure (false, false)
    if matches.1 then
      count := count + 1
      if pos == selected && matches.2 then
        selectedMatches := true
  return (count, selectedMatches)

private partial def checkReachabilityAux
    (type : Expr) (assignments assigned : Array Expr) (index : Nat) : MetaM Bool := do
  match type with
  | .mdata _ body => checkReachabilityAux body assignments assigned index
  | .forallE _ domain body _ =>
      if index >= assignments.size then
        return false
      let value := assignments[index]!
      if !(← checkedClosedTerm value) then
        return false
      let expectedType := domain.instantiateRev assigned
      let actualType ← inferType value
      if !(← defEqNoAssign actualType expectedType .reducible) then
        return false
      checkReachabilityAux body assignments (assigned.push value) (index + 1)
  | _ => return index == assignments.size

private def checkReachability
    (source : Expr) (guardOrdinal : Nat) (evidence : N31ReachabilityEvidence) : MetaM Bool := do
  if evidence.modeId != "explicit_telescope_witness_and_retained_hypothesis_proofs" ||
      evidence.guardOrdinal != guardOrdinal then
    return false
  checkReachabilityAux source evidence.assignments #[] 0

private def applyP01 (source : Expr) (ordinal : Nat) : MetaM ApplyResult := do
  let usedNames := binderDisplayNames source
  let candidateName := freshAlphaName usedNames
  if candidateName.isAnonymous || candidateName.hasMacroScopes ||
      usedNames.contains candidateName.toString then
    return .typedNotApplicable .nonHygienicBinderName
  let some (candidate, sourceName, binderInfo) :=
      renameOuterBinderAt? source ordinal candidateName
    | return .typedNotApplicable .operationNotApplicable
  if !(← checkedClosedProp candidate) then
    return .typedNotApplicable .candidateNotClosedProp
  if !Expr.eqv source candidate || Expr.equal source candidate then
    return .typedNotApplicable .exactDeltaMismatch
  if !(← defEqNoAssign source candidate .none) then
    return .typedNotApplicable .expectedDefinitionalEqualityMissing
  let selector := Selector.outerBinder ordinal
  return .applicable {
    operation := .p01AlphaRenameSingle
    selector
    candidate
    certificate := .p01 {
      binderOrdinal := ordinal
      binderSite := outerBinderSite ordinal
      sourceName
      candidateName
      binderInfo
    }
  }

private def applyP15 (source : Expr) : MetaM ApplyResult := do
  let some rewrite := swapFinalIff? source
    | return .typedNotApplicable .operationNotApplicable
  if ← defEqNoAssign rewrite.left rewrite.right .none then
    return .typedNotApplicable .degenerateOperands
  if !(← checkedClosedProp rewrite.candidate) then
    return .typedNotApplicable .candidateNotClosedProp
  if Expr.equal source rewrite.candidate then
    return .typedNotApplicable .exactDeltaMismatch
  let selector := Selector.outerTarget
  return .applicable {
    operation := .p15SwapIffSides
    selector
    candidate := rewrite.candidate
    certificate := .p15 { targetSite := outerTargetSite source }
  }

private def applyP18 (source : Expr) : MetaM ApplyResult := do
  let some rewrite := symmetrizeFinalEq? source
    | return .typedNotApplicable .operationNotApplicable
  if ← defEqNoAssign rewrite.left rewrite.right .none then
    return .typedNotApplicable .degenerateOperands
  if !(← checkedClosedProp rewrite.candidate) then
    return .typedNotApplicable .candidateNotClosedProp
  if Expr.equal source rewrite.candidate then
    return .typedNotApplicable .exactDeltaMismatch
  let selector := Selector.outerTarget
  return .applicable {
    operation := .p18SymmetrizeEquality
    selector
    candidate := rewrite.candidate
    certificate := .p18 { targetSite := outerTargetSite source }
  }

private def applyP21 (source : Expr) (site : SubExpr.Pos) : MetaM ApplyResult := do
  let applicable ←
    try
      Meta.viewSubexpr (fun _ subexpr => pure (betaReduceNode? subexpr).isSome) site source
    catch ex =>
      if ex.isInterrupt || ex.isRuntime then
        throw ex
      pure false
  if !applicable then
    return .typedNotApplicable .operationNotApplicable
  let candidate? ←
    try
      some <$> Meta.replaceSubexpr
        (fun subexpr => pure ((betaReduceNode? subexpr).getD subexpr)) site source
    catch ex =>
      if ex.isInterrupt || ex.isRuntime then
        throw ex
      pure none
  let some candidate := candidate?
    | return .typedNotApplicable .selectedSiteMissing
  if !(← checkedClosedProp candidate) then
    return .typedNotApplicable .candidateNotClosedProp
  if Expr.equal source candidate then
    return .typedNotApplicable .exactDeltaMismatch
  if ← defEqNoAssign candidate (mkConst ``True) .reducible then
    return .typedNotApplicable .claimCollapsesToTrue
  if ← defEqNoAssign candidate (mkConst ``False) .reducible then
    return .typedNotApplicable .claimCollapsesToFalse
  if !(← defEqNoAssign source candidate .reducible) then
    return .typedNotApplicable .expectedDefinitionalEqualityMissing
  let selector := Selector.subexpr site
  return .applicable {
    operation := .p21BetaReduce
    selector
    candidate
    certificate := .p21 { redexSite := site }
  }

private def applyN31
    (source : Expr) (guardOrdinal : Nat) (targetSite : SubExpr.Pos)
    (bankEntryId : String) (context : DispatchContext) : MetaM ApplyResult := do
  let some bank := context.n31Bank
    | return .typedNotApplicable .n31BankMissing
  if !(← n31BankValid bank) then
    return .typedNotApplicable .n31BankInvalid
  let some entry := findBankEntry? bank bankEntryId
    | return .typedNotApplicable .n31BankEntryMissingOrAmbiguous
  let some reachability := context.n31Reachability
    | return .typedNotApplicable .n31ReachabilityMissing
  if !(← checkReachability source guardOrdinal reachability) then
    return .typedNotApplicable .n31ReachabilityInvalid
  forallTelescope source fun locals target => do
    if guardOrdinal >= locals.size then
      return .typedNotApplicable .operationNotApplicable
    let guard := locals[guardOrdinal]!
    let guardDecl ← guard.fvarId!.getDecl
    let guardType ← inferType guard
    if guardDecl.binderInfo != .default || guardDecl.userName.isAnonymous ||
        guardDecl.userName.hasMacroScopes || !(← isProp guardType) then
      return .typedNotApplicable .n31GuardNotNamedExplicitProp
    let shapeIds := matchingGuardShapeIds bank guardType
    let retainedContradictionShapeIds :=
      matchingRetainedContradictionShapeIds bank guardType
    if shapeIds.size != 1 || !shapeIds.contains entry.guardShapeId ||
        !retainedContradictionShapeIds.isEmpty then
      return .typedNotApplicable .n31GuardMissingOrAmbiguous
    let some guardMatch := matchGuard? entry guardType
      | return .typedNotApplicable .n31GuardMissingOrAmbiguous
    if ← defEqNoAssign guardType (mkConst ``True) .reducible then
      return .typedNotApplicable .n31GuardDefinitionallyTrue
    if ← defEqNoAssign target (mkConst ``True) .reducible then
      return .typedNotApplicable .n31BodyDefinitionallyTrue
    let (targetCount, selectedMatches) ←
      countMatchingTargets target bank entry guardMatch targetSite
    if targetCount != 1 || !selectedMatches then
      return .typedNotApplicable .n31TargetMissingOrAmbiguous
    match ← retainedContextRejects bank entry guardMatch locals guardOrdinal with
    | .n31CompetingGuard => return .typedNotApplicable .n31CompetingGuard
    | .n31RetainedContradiction =>
        return .typedNotApplicable .n31RetainedContradiction
    | .n31RetainedContextUnknownOrAmbiguous =>
        return .typedNotApplicable .n31RetainedContextUnknownOrAmbiguous
    | _ => pure ()
    let some candidate := removeOuterGuardAt? source guardOrdinal
      | return .typedNotApplicable .n31GuardProofUsedInContinuation
    if !(← checkedClosedProp candidate) then
      return .typedNotApplicable .candidateNotClosedProp
    if Expr.equal source candidate then
      return .typedNotApplicable .exactDeltaMismatch
    if ← defEqNoAssign source candidate .reducible then
      return .typedNotApplicable .forbiddenDefinitionalEquality
    let selector := Selector.requiredGuard guardOrdinal targetSite bankEntryId
    return .applicable {
      operation := .n31DropRequiredGuardRubric
      selector
      candidate
      certificate := .n31Rubric {
        guardOrdinal
        guardSite := outerBinderSite guardOrdinal
        targetSite
        bankEntryId
        guardShapeId := entry.guardShapeId
        bank
        reachability
      }
    }

private def dispatchPrepared
    (operation : PrimaryOperation) (selector : Selector)
    (context : DispatchContext) (source : Expr) : MetaM ApplyResult := do
  match operation, selector with
  | .p01AlphaRenameSingle, .outerBinder ordinal => applyP01 source ordinal
  | .p15SwapIffSides, .outerTarget => applyP15 source
  | .p18SymmetrizeEquality, .outerTarget => applyP18 source
  | .p21BetaReduce, .subexpr site => applyP21 source site
  | .n31DropRequiredGuardRubric, .requiredGuard guardOrdinal targetSite bankEntryId =>
      applyN31 source guardOrdinal targetSite bankEntryId context
  | _, _ => return .typedNotApplicable .selectorDoesNotMatchOperation

def dispatchAt
    (operation : PrimaryOperation) (selector : Selector)
    (context : DispatchContext) (source : Expr) : MetaM ApplyResult := do
  if !(← checkedClosedProp source) then
    return .typedNotApplicable .sourceNotClosedProp
  dispatchPrepared operation selector context source

def discover
    (operation : PrimaryOperation) (context : DispatchContext)
    (source : Expr) : MetaM (Array Candidate) := do
  if !(← checkedClosedProp source) then
    return #[]
  let mut result := #[]
  match operation with
  | .p01AlphaRenameSingle =>
      for ordinal in [0 : outerBinderCount source] do
        if let .applicable candidate ←
            dispatchPrepared operation (.outerBinder ordinal) context source then
          result := result.push candidate
  | .p15SwapIffSides | .p18SymmetrizeEquality =>
      if let .applicable candidate ←
          dispatchPrepared operation .outerTarget context source then
        result := result.push candidate
  | .p21BetaReduce =>
      for site in termPositions source do
        if let .applicable candidate ←
            dispatchPrepared operation (.subexpr site) context source then
          result := result.push candidate
  | .n31DropRequiredGuardRubric =>
      if let some bank := context.n31Bank then
        if admittedN31BankIdentitiesV0_3_4.contains bank.identity then
          let target := rawOuterTarget source
          for targetSite in termPositions target do
            for guardOrdinal in [0 : outerBinderCount source] do
              for entry in bank.entries do
                let selector :=
                  Selector.requiredGuard guardOrdinal targetSite entry.entryId
                if let .applicable candidate ←
                    dispatchPrepared operation selector context source then
                  result := result.push candidate
  return result

private def Certificate.operationAndSelector : Certificate → PrimaryOperation × Selector
  | .p01 value =>
      (.p01AlphaRenameSingle, .outerBinder value.binderOrdinal)
  | .p15 _ => (.p15SwapIffSides, .outerTarget)
  | .p18 _ => (.p18SymmetrizeEquality, .outerTarget)
  | .p21 value => (.p21BetaReduce, .subexpr value.redexSite)
  | .n31Rubric value =>
      (.n31DropRequiredGuardRubric,
        .requiredGuard value.guardOrdinal value.targetSite value.bankEntryId)

private def Certificate.contextMatches (certificate : Certificate)
    (context : DispatchContext) : Bool :=
  match certificate with
  | .n31Rubric value =>
      match context.n31Bank, context.n31Reachability with
      | some bank, some reachability =>
          bank == value.bank && reachability == value.reachability
      | _, _ => false
  | _ => true

def replayCertificate
    (context : DispatchContext) (source candidate : Expr)
    (certificate : Certificate) : MetaM ReplayResult := do
  let (operation, selector) := certificate.operationAndSelector
  if !(← checkedClosedProp source) then
    return { passed := false, operation, reason := some .sourceNotClosedProp }
  if !(← checkedClosedProp candidate) then
    return { passed := false, operation, reason := some .candidateNotClosedProp }
  if !certificate.contextMatches context then
    return { passed := false, operation, reason := some .replayContextMismatch }
  match ← dispatchPrepared operation selector context source with
  | .typedNotApplicable reason =>
      return { passed := false, operation, reason := some reason }
  | .applicable expected =>
      if !Expr.equal expected.candidate candidate then
        return { passed := false, operation, reason := some .replayCandidateMismatch }
      if expected.certificate != certificate then
        return { passed := false, operation, reason := some .replayCertificateMismatch }
      return { passed := true, operation, reason := none }

end LeanFaith.SFT1.Wave1
