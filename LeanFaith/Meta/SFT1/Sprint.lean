/-
Compact SFT1 sprint engine.

Seven single-hop Mathlib transformations with checked label evidence:

* positives (`P15`, `P18`, `P14`, `P23`) carry a Meta- and kernel-checked
  `Iff reference candidate` witness;
* negatives (`N25`, `N32`, `N31`) carry the loaded source-theorem proof and a
  Meta- and kernel-checked `Not candidate` refutation under a complete ground
  assignment of every binder.

This file is injected without its import command into one persistent Mathlib
Meta request after the frozen `LeanFaith.GoalV1` renderer helper.  It declares
no endpoint theorem or axiom, never pretty-prints and re-elaborates a
candidate, and produces no model-facing text: the frozen
`LeanFaith.GoalV1.emitClosedProp` route renders every retained endpoint.  The
public `LeanFaith.GoalV1.renderClosedProp` implementation is only consulted as
a renderability precheck whose text the frozen route must reproduce exactly.
-/
import Mathlib

namespace LeanFaith.SFT1.Sprint

open Lean Elab Meta

/-! ## Live Wave 3 certificate fixtures

These small proof-bearing constants exercise the exact retained and fail-closed
paths in a real imported Lean environment.  They are fixture roots only and
are excluded from every release inventory. -/

namespace Fixtures

inductive Enumerable3 where
  | first
  | second
  | third
  deriving DecidableEq, Fintype

def n31Retained (n : Nat) (h : 0 < n) : 0 < n := h

def n31FailClosed (n : Nat) (h : 0 < n) : h = h := rfl

def n26Retained (n i : Nat) (h : i < n) : i ∈ Finset.range n :=
  Finset.mem_range.mpr h

def n26FailClosed (n i : Nat) (h : i < n) : h = h := rfl

def n32Retained : false < true := Bool.false_lt_true

def n32FailClosed : (0 : Nat) ≤ 0 := Nat.le_refl 0

def n30Retained : ∃ b : Bool, True := ⟨false, True.intro⟩

def n30FailClosed : ∃ b : Bool, b = true := ⟨true, rfl⟩

def n30EnumerableRetained : ∃ _x : Enumerable3, True :=
  ⟨.first, True.intro⟩

def n30PolymorphicRetained {α : Type} [Nontrivial α] (x : α) : ∃ y, y ≠ x :=
  exists_ne x

def n29Retained (x : Bool) : ∃ y : Bool, y = x := ⟨x, rfl⟩

def n29FailClosed (_x : Bool) : ∃ y : Bool, y = false := ⟨false, rfl⟩

def p24Retained (P Q : Prop) (hP : P) (hQ : Q) : P ∧ Q := And.intro hP hQ

def p16Retained : (True ∧ True) ∧ True :=
  And.intro (And.intro True.intro True.intro) True.intro

def p28Retained : True ↔ True := Iff.rfl

def pOrderComplementRetained : ¬(1 : Nat) ≤ 0 := Nat.not_succ_le_zero 0

def preservingFailClosed : True := True.intro

end Fixtures

def engineSemanticVersion : String := "sft1_wave5_compiler_engine_v1"
/-- Operation-set version 4 adds the Wave 3 grounding, preserving, and proof-backed
    negative batch without changing any historical operation bit. -/
def engineOperationSetVersion : Nat := 4
def evidenceMarker : String := "LFSFT1SPRINTJSON "

/-- Per-operation heartbeat budget in thousands of heartbeats. -/
def opHeartbeatBudgetK : Nat := 120000
/-- Maximum candidate-value branches explored while grounding one telescope. -/
def groundingBranchBudget : Nat := 400
/-- Maximum tactic elaborations while grounding one telescope. -/
def groundingTacticBudget : Nat := 40

inductive Op where
  | p15 | p18 | p14 | p23 | n25 | n32 | n31 | pne | pdrg
  | p21Beta | p21Zeta | p32Assoc | p32Comm | p35 | n26
  | n30 | n29 | p24 | p16 | p28 | pOrderComplement
  deriving BEq, Repr, Inhabited

def Op.all : Array Op := #[.p15, .p18, .p14, .p23, .n25, .n32, .n31, .pne, .pdrg,
  .p21Beta, .p21Zeta, .p32Assoc, .p32Comm, .p35, .n26,
  .n30, .n29, .p24, .p16, .p28, .pOrderComplement]

def Op.id : Op → String
  | .p15 => "P15_SWAP_IFF_SIDES_V1"
  | .p18 => "P18_SYMMETRIZE_EQUALITY_V1"
  | .p14 => "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1"
  | .p23 => "P23_CURRY_PROP_PAIR_V1"
  | .n25 => "N25_TOGGLE_EQ_NE_PROOF_V1"
  | .n32 => "N32_SWAP_ROLE_ORDER_PROOF_V1"
  | .n31 => "N31_DROP_REQUIRED_GUARD_PROOF_V1"
  | .pne => "P_NE_SYMMETRIZE_V1"
  | .pdrg => "P_DROP_REDUNDANT_GUARD_PROOF_V1"
  | .p21Beta => "P21_BETA_REDUCE_V1"
  | .p21Zeta => "P21_ZETA_REDUCE_V1"
  | .p32Assoc => "P32_ADD_ASSOC_LOCAL_V1"
  | .p32Comm => "P32_ADD_COMM_LOCAL_V1"
  | .p35 => "P35_SET_INTER_MEMBERSHIP_V1"
  | .n26 => "N26_INCREMENT_BOUND_PROOF_V1"
  | .n30 => "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1"
  | .n29 => "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1"
  | .p24 => "P24_SWAP_INDEPENDENT_PROP_BINDERS_V1"
  | .p16 => "P16_REASSOCIATE_AND_V1"
  | .p28 => "P28_IFF_TO_IMPLICATION_PAIR_V1"
  | .pOrderComplement => "P_ORDER_COMPLEMENT_V1"

def Op.ofId? (s : String) : Option Op := Op.all.find? (·.id == s)

def Op.positive : Op → Bool
  | .p15 | .p18 | .p14 | .p23 | .pne | .pdrg
  | .p21Beta | .p21Zeta | .p32Assoc | .p32Comm | .p35
  | .p24 | .p16 | .p28 | .pOrderComplement => true
  | .n25 | .n32 | .n31 | .n26 | .n30 | .n29 => false

def Op.bit : Op → Nat
  | .p15 => 0
  | .p18 => 1
  | .p14 => 2
  | .p23 => 3
  | .n25 => 4
  | .n32 => 5
  | .n31 => 6
  | .pne => 7
  | .pdrg => 8
  | .p21Beta => 9
  | .p21Zeta => 10
  | .p32Assoc => 11
  | .p32Comm => 12
  | .p35 => 13
  | .n26 => 14
  | .n30 => 15
  | .n29 => 16
  | .p24 => 17
  | .p16 => 18
  | .p28 => 19
  | .pOrderComplement => 20

/-! ## Tagged failure classes -/

private def naPrefix : String := "SPRINT_NA:"
private def rejPrefix : String := "SPRINT_REJ:"

private def throwNA {α : Type} (reason : String) : MetaM α :=
  throwError "{naPrefix}{reason}"

private def throwRej {α : Type} (reason : String) : MetaM α :=
  throwError "{rejPrefix}{reason}"

/-! ## Structural helpers -/

private partial def eraseMDataDeep : Expr → Expr
  | .mdata _ body => eraseMDataDeep body
  | .app fn arg => .app (eraseMDataDeep fn) (eraseMDataDeep arg)
  | .lam name domain body binderInfo =>
      .lam name (eraseMDataDeep domain) (eraseMDataDeep body) binderInfo
  | .forallE name domain body binderInfo =>
      .forallE name (eraseMDataDeep domain) (eraseMDataDeep body) binderInfo
  | .letE name type value body nondep =>
      .letE name (eraseMDataDeep type) (eraseMDataDeep value) (eraseMDataDeep body) nondep
  | .proj typeName index base => .proj typeName index (eraseMDataDeep base)
  | e => e

private def binderInfoCode : BinderInfo → UInt64
  | .default => 1
  | .implicit => 2
  | .strictImplicit => 3
  | .instImplicit => 4

def binderInfoTag : BinderInfo → String
  | .default => "default"
  | .implicit => "implicit"
  | .strictImplicit => "strictImplicit"
  | .instImplicit => "instImplicit"

/-- Binder-name-insensitive structural hash used only to bind the process and
    render requests to the same live Exprs.  Model-facing identity comes from
    the frozen REPR closed-Expr hash. -/
partial def alphaHash : Expr → UInt64
  | .bvar i => mixHash 11 (hash i)
  | .fvar _ => 12
  | .mvar _ => 13
  | .sort u => mixHash 14 (hash u)
  | .const n us => mixHash 15 (mixHash (hash n) (hash us))
  | .app f a => mixHash 16 (mixHash (alphaHash f) (alphaHash a))
  | .lam _ d b bi => mixHash 17 (mixHash (alphaHash d) (mixHash (alphaHash b) (binderInfoCode bi)))
  | .forallE _ d b bi =>
      mixHash 18 (mixHash (alphaHash d) (mixHash (alphaHash b) (binderInfoCode bi)))
  | .letE _ t v b nd =>
      mixHash 19 (mixHash (alphaHash t) (mixHash (alphaHash v)
        (mixHash (alphaHash b) (if nd then 1 else 0))))
  | .lit l => mixHash 20 (hash l)
  | .mdata _ b => alphaHash b
  | .proj s i b => mixHash 21 (mixHash (hash s) (mixHash (hash i) (alphaHash b)))

private def withHeartbeatBudget (budgetK : Nat) (x : MetaM α) : MetaM α := do
  let start ← IO.getNumHeartbeats
  controlAt CoreM fun runInBase =>
    withReader
      (fun (ctx : Core.Context) =>
        { ctx with initHeartbeats := start, maxHeartbeats := budgetK * 1000 })
      (runInBase x)

/-- Run `x` and restore the message log afterwards so failed tactic attempts
    never leave error messages in the request. -/
private def withRestoredMessages (x : MetaM α) : MetaM α := do
  let saved ← Core.getMessageLog
  tryCatchRuntimeEx
    (do
      let r ← x
      Core.setMessageLog saved
      return r)
    fun ex => do
      Core.setMessageLog saved
      throw ex

private def exceptionText (ex : Exception) : MetaM String := do
  let text ← ex.toMessageData.toString
  return text.replace "\n" " "

private def headName? (e : Expr) : Option Name := e.getAppFn.constName?

/-! ## Closed proposition and proof checks -/

private def isClosedPlaceholderFree (e : Expr) : Bool :=
  !(e.hasExprMVar || e.hasLevelMVar || e.hasFVar || e.hasLooseBVars || e.hasSorry)

private def failClosed {α : Type} (na : Bool) (reason : String) : MetaM α :=
  if na then throwNA reason else throwRej reason

private def checkedClosedProp (na : Bool) (origin : String) (e : Expr) : MetaM Expr := do
  let e ← instantiateMVars e
  unless isClosedPlaceholderFree e do
    failClosed na s!"{origin}_not_closed"
  let wellTyped ← tryCatchRuntimeEx (do check e; pure true) fun ex => do
    if ex.isInterrupt then throw ex
    pure false
  unless wellTyped do
    failClosed na s!"{origin}_ill_typed"
  unless ← isProp e do
    failClosed na s!"{origin}_not_prop"
  return e

private def levelZeroInstantiate (params : List Name) (e : Expr) : Expr :=
  e.instantiateLevelParams params (params.map fun _ => Level.zero)

structure ProofCheck where
  metaChecked : Bool
  kernelChecked : Bool
  kernelLevelInstantiation : String
  proofHash : UInt64
  deriving Repr

def ProofCheck.toJson (c : ProofCheck) : Json :=
  Json.mkObj [
    ("meta_checked", Json.bool c.metaChecked),
    ("kernel_checked", Json.bool c.kernelChecked),
    ("kernel_level_instantiation", Json.str c.kernelLevelInstantiation),
    ("proof_expr_hash_u64", Json.str (toString c.proofHash))
  ]

/-- Meta type check plus an independent kernel check of the proof term
    against the expected proposition.  Universe parameters, if any, are
    instantiated at level zero for the kernel pass; that instantiation is
    recorded, and the Meta pass checks the parametric term itself. -/
private def checkedProof
    (origin : String) (params : List Name) (proof expected : Expr) : MetaM ProofCheck := do
  let proof ← instantiateMVars proof
  unless isClosedPlaceholderFree proof do
    throwRej s!"{origin}_proof_not_closed"
  let metaOk ← tryCatchRuntimeEx (do check proof; pure true) fun ex => do
    if ex.isInterrupt then throw ex
    pure false
  unless metaOk do
    throwRej s!"{origin}_proof_meta_check_failed"
  let actual ← inferType proof
  unless ← withoutModifyingMCtx (isDefEq actual expected) do
    throwRej s!"{origin}_proof_type_mismatch"
  let proof0 := levelZeroInstantiate params proof
  let expected0 := levelZeroInstantiate params expected
  let env ← getEnv
  let kernelType ←
    match Kernel.check env {} proof0 with
    | .ok ty => pure ty
    | .error _ => throwRej s!"{origin}_proof_kernel_check_failed"
  match Kernel.isDefEq env {} kernelType expected0 with
  | .ok true => pure ()
  | _ => throwRej s!"{origin}_proof_kernel_type_mismatch"
  return {
    metaChecked := true
    kernelChecked := true
    kernelLevelInstantiation := if params.isEmpty then "none" else "all_zero"
    proofHash := hash proof
  }

/-! ## Roots -/

structure Root where
  name : Name
  module : Name
  levelParams : List Name
  reference : Expr
  proofValueHash : UInt64
  deriving Inhabited

/-- Content identity supplied by the pinned compiler-data inventory.  The
    request builder recomputes every digest from the exact reconstructed row;
    retaining the complete identity in Lean's evidence prevents a local
    theorem certificate from being detached from its source or context. -/
structure CompilerSourceBinding where
  rootId : String
  sourceRowId : String
  inventoryRecordSha256 : String
  theoremSha256 : String
  proofSourceSha256 : String
  typeSourceSha256 : String
  fullSourceSha256 : String
  declarationSourceSha256 : String
  contextSha256 : String
  contextFingerprint : String
  qualifiedName : String
  sourceRevision : String
  projectRevision : String
  checkerVersion : String
  deriving Inhabited, Repr

private def lowerHexDigit (c : Char) : Bool :=
  ('0' <= c && c <= '9') || ('a' <= c && c <= 'f')

private def fixedLowerHex (width : Nat) (value : String) : Bool :=
  value.length == width && value.data.all lowerHexDigit

private def CompilerSourceBinding.validate (binding : CompilerSourceBinding)
    (name : Name) : MetaM Unit := do
  for (field, digest) in [
      ("root_id", binding.rootId),
      ("source_row_id", binding.sourceRowId),
      ("inventory_record_sha256", binding.inventoryRecordSha256),
      ("theorem_sha256", binding.theoremSha256),
      ("proof_source_sha256", binding.proofSourceSha256),
      ("type_source_sha256", binding.typeSourceSha256),
      ("full_source_sha256", binding.fullSourceSha256),
      ("declaration_source_sha256", binding.declarationSourceSha256),
      ("context_sha256", binding.contextSha256),
      ("context_fingerprint", binding.contextFingerprint)] do
    unless fixedLowerHex 64 digest do throwRej s!"compiler_binding_invalid_sha256:{field}"
  unless fixedLowerHex 40 binding.sourceRevision do
    throwRej "compiler_binding_invalid_source_revision"
  unless fixedLowerHex 40 binding.projectRevision do
    throwRej "compiler_binding_invalid_project_revision"
  unless binding.qualifiedName == name.toString do
    throwRej "compiler_binding_qualified_name_mismatch"
  if binding.checkerVersion.isEmpty then throwRej "compiler_binding_checker_version_empty"

def CompilerSourceBinding.toJson (binding : CompilerSourceBinding) : Json :=
  Json.mkObj [
    ("root_id", Json.str binding.rootId),
    ("source_row_id", Json.str binding.sourceRowId),
    ("inventory_record_sha256", Json.str binding.inventoryRecordSha256),
    ("theorem_sha256", Json.str binding.theoremSha256),
    ("proof_source_sha256", Json.str binding.proofSourceSha256),
    ("type_source_sha256", Json.str binding.typeSourceSha256),
    ("full_source_sha256", Json.str binding.fullSourceSha256),
    ("declaration_source_sha256", Json.str binding.declarationSourceSha256),
    ("context_sha256", Json.str binding.contextSha256),
    ("context_fingerprint", Json.str binding.contextFingerprint),
    ("qualified_name", Json.str binding.qualifiedName),
    ("source_revision", Json.str binding.sourceRevision),
    ("project_revision", Json.str binding.projectRevision),
    ("checker_version", Json.str binding.checkerVersion)]

structure LoadedCompilerRoot where
  root : Root
  sourceProofCheck : ProofCheck

private def rootNameExcluded (n : Name) : Bool :=
  n.isInternal || n.isInternalDetail || n.hasMacroScopes

private def liveFixtureRoot (n : Name) : Bool :=
  n.toString.startsWith "LeanFaith.SFT1.Sprint.Fixtures."

def loadRoot (name : Name) : MetaM Root := do
  if rootNameExcluded name then
    throwNA "root_internal_name"
  let env ← getEnv
  let some info := env.find? name
    | throwNA "root_not_found"
  let (levelParams, referenceType, proofValue) ←
    match info with
    | .thmInfo tv => pure (tv.levelParams, tv.type, tv.value)
    | .defnInfo dv =>
        if liveFixtureRoot name then pure (dv.levelParams, dv.type, dv.value)
        else throwNA "root_not_theorem"
    | _ => throwNA "root_not_theorem"
  let module ←
    match env.getModuleIdxFor? name with
    | some modIdx =>
        let some module := env.allImportedModuleNames[modIdx.toNat]?
          | throwNA "root_module_unknown"
        pure module
    | none =>
        if liveFixtureRoot name then pure `Mathlib.LeanFaithSFT1Wave3Fixtures
        else throwNA "root_not_imported"
  unless [`Mathlib, `Physlib, `Cslib].contains module.getRoot do
    throwNA "root_not_registered_source_module"
  if ← isInstance name then
    throwNA "root_is_instance"
  let reference ← checkedClosedProp true "reference" (eraseMDataDeep referenceType)
  return {
    name
    module
    levelParams
    reference
    proofValueHash := hash proofValue
  }

/-- Load a theorem declared in the current reconstructed compiler-data
    compilation unit.  Imported roots continue to use `loadRoot`; this path
    rejects imported constants, checks the exact local proof with Meta and the
    kernel, and never synthesizes a replacement proof. -/
def loadCompilerRootChecked (name : Name) : MetaM LoadedCompilerRoot := do
  if rootNameExcluded name then throwNA "compiler_root_internal_name"
  let env ← getEnv
  if (env.getModuleIdxFor? name).isSome then throwNA "compiler_root_not_local"
  let some info := env.find? name | throwNA "compiler_root_not_found"
  let (levelParams, referenceType, proofValue) ←
    match info with
    | .thmInfo tv => pure (tv.levelParams, tv.type, tv.value)
    | _ => throwNA "compiler_root_not_theorem"
  if ← isInstance name then throwNA "compiler_root_is_instance"
  let reference ← checkedClosedProp true "compiler_reference" (eraseMDataDeep referenceType)
  let sourceProofCheck ← checkedProof "compiler_source" levelParams proofValue reference
  return {
    root := {
      name
      module := `CompilerData
      levelParams
      reference
      proofValueHash := hash proofValue
    }
    sourceProofCheck
  }

def loadCompilerRoot (name : Name) : MetaM Root :=
  return (← loadCompilerRootChecked name).root

structure BinderReport where
  name : String
  binderInfo : String
  macroScopes : Bool
  kind : String
  deriving Repr

private partial def binderReports (e : Expr) (acc : Array BinderReport) :
    MetaM (Array BinderReport) := do
  match e with
  | .forallE n d b bi =>
      let kind ←
        if bi == .instImplicit then pure "instance"
        else if ← isProp d then pure "prop"
        else if d.isSort then pure "sort"
        else pure "data"
      let report : BinderReport := {
        name := n.eraseMacroScopes.toString
        binderInfo := binderInfoTag bi
        macroScopes := n.hasMacroScopes
        kind
      }
      withLocalDecl n bi d fun x => binderReports (b.instantiate1 x) (acc.push report)
  | _ => return acc

private def binderReportsJson (reports : Array BinderReport) : Json :=
  Json.arr <| reports.map fun r =>
    Json.mkObj [
      ("name", Json.str r.name),
      ("binder_info", Json.str r.binderInfo),
      ("macro_scopes", Json.bool r.macroScopes),
      ("kind", Json.str r.kind)
    ]

private def prerender (e : Expr) : MetaM (Except String String) := do
  tryCatchRuntimeEx (do return Except.ok (← LeanFaith.GoalV1.renderClosedProp e)) fun ex => do
    if ex.isInterrupt then throw ex
    return Except.error (← exceptionText ex)

/-! ## Sites and certificates -/

/-- Site descriptor recorded in every certificate.  Replay reconstructs the
    candidate from the reference and this descriptor alone. -/
structure Site where
  kind : String
  index : Nat := 0
  detail : String := ""
  guardVariableIndex : Nat := 0
  boundVariableIndex : Option Nat := none
  literal : Int := 0
  /-- Structural path reserved for the all-site Wave 4 enumerator.  Wave 3
      binder sites use the outer-binder index and therefore leave this empty. -/
  path : Array Nat := #[]
  deriving BEq, Repr, Inhabited

def siteJson (s : Site) : Json :=
  Json.mkObj [
    ("kind", Json.str s.kind),
    ("index", toJson s.index),
    ("detail", Json.str s.detail),
    ("guard_variable_index", toJson s.guardVariableIndex),
    ("bound_variable_index", match s.boundVariableIndex with
      | some index => toJson index | none => Json.null),
    ("literal", toJson s.literal),
    ("path", Json.arr (s.path.map toJson))
  ]

/-- Rewrite the innermost target beneath the complete outer Pi telescope. -/
private partial def rewriteFinalTarget
    (e : Expr) (f : Expr → Option Expr) : Option (Expr × Nat) :=
  match e with
  | .forallE n d b bi =>
      match rewriteFinalTarget b f with
      | some (b', k) => some (.forallE n d b' bi, k + 1)
      | none => none
  | _ => (f e).map (·, 0)

private partial def innermostTarget : Expr → Expr
  | .forallE _ _ b _ => innermostTarget b
  | t => t

private def swapIff? (t : Expr) : Option (Expr × String) :=
  let args := t.getAppArgs
  if headName? t == some ``Iff && args.size == 2 && !Expr.eqv args[0]! args[1]! then
    some (mkApp2 t.getAppFn args[1]! args[0]!, "swap")
  else
    none

private def swapEq? (t : Expr) : Option (Expr × String) :=
  let args := t.getAppArgs
  if headName? t == some ``Eq && args.size == 3 && !Expr.eqv args[1]! args[2]! then
    some (mkApp3 t.getAppFn args[0]! args[2]! args[1]!, "swap")
  else
    none

private def toggleEqNe? (t : Expr) : Option (Expr × String) :=
  let args := t.getAppArgs
  match t.getAppFn with
  | .const n us =>
      if n == ``Eq && args.size == 3 then
        some (mkApp3 (mkConst ``Ne us) args[0]! args[1]! args[2]!, "eq_to_ne")
      else if n == ``Ne && args.size == 3 then
        some (mkApp3 (mkConst ``Eq us) args[0]! args[1]! args[2]!, "ne_to_eq")
      else if n == ``Not && args.size == 1 then
        let inner := args[0]!
        let innerArgs := inner.getAppArgs
        match inner.getAppFn with
        | .const m ws =>
            if m == ``Eq && innerArgs.size == 3 then
              some (mkApp3 (mkConst ``Eq ws) innerArgs[0]! innerArgs[1]! innerArgs[2]!, "not_eq_to_eq")
            else none
        | _ => none
      else none
  | _ => none

private def swapNe? (t : Expr) : Option (Expr × String) :=
  let args := t.getAppArgs
  if headName? t == some ``Ne && args.size == 3 && !Expr.eqv args[1]! args[2]! then
    some (mkApp3 t.getAppFn args[0]! args[2]! args[1]!, "swap")
  else
    none

private def isNatOrInt (α : Expr) : Option String :=
  if α.isConstOf ``Nat then some "Nat" else if α.isConstOf ``Int then some "Int" else none

private def swapRoleOrder? (t : Expr) : Option (Expr × String) :=
  let args := t.getAppArgs
  if headName? t == some ``LT.lt && args.size == 4 then
    if Expr.eqv args[2]! args[3]! then none
    else some (mkApp4 t.getAppFn args[0]! args[1]! args[3]! args[2]!, "strict_lt")
  else if headName? t == some ``LE.le && args.size == 4 then
    if Expr.eqv args[2]! args[3]! then none
    else some (mkApp4 t.getAppFn args[0]! args[1]! args[3]! args[2]!, "nonstrict_le")
  else
    none

private def existsToExistsUnique? (t : Expr) : Option (Expr × String) :=
  let args := t.getAppArgs
  match t.getAppFn with
  | .const n us =>
      if n == ``Exists && args.size == 2 then
        some (mkApp2 (mkConst ``ExistsUnique us) args[0]! args[1]!, "finite_two_witnesses")
      else none
  | _ => none

inductive DefReduceKind where
  | beta | zeta
  deriving BEq

/-- Reduce exactly the first matching explicit beta or zeta redex in preorder. -/
private partial def reduceFirst? (kind : DefReduceKind) (e : Expr) : Option Expr :=
  match kind, e with
  | .beta, .app (.lam _ _ body _) arg => some (body.instantiate1 arg)
  | .zeta, .letE _ _ value body _ => some (body.instantiate1 value)
  | _, .app fn arg =>
      match reduceFirst? kind fn with
      | some fn' => some (.app fn' arg)
      | none => (reduceFirst? kind arg).map (.app fn)
  | _, .lam name domain body bi =>
      match reduceFirst? kind domain with
      | some domain' => some (.lam name domain' body bi)
      | none => (reduceFirst? kind body).map fun body' => .lam name domain body' bi
  | _, .forallE name domain body bi =>
      match reduceFirst? kind domain with
      | some domain' => some (.forallE name domain' body bi)
      | none => (reduceFirst? kind body).map fun body' => .forallE name domain body' bi
  | _, .letE name type value body nondep =>
      match reduceFirst? kind type with
      | some type' => some (.letE name type' value body nondep)
      | none =>
          match reduceFirst? kind value with
          | some value' => some (.letE name type value' body nondep)
          | none => (reduceFirst? kind body).map fun body' => .letE name type value body' nondep
  | _, .mdata md body => (reduceFirst? kind body).map (.mdata md)
  | _, .proj typeName index base =>
      (reduceFirst? kind base).map fun base' => .proj typeName index base'
  | _, _ => none

private def reduceHere? (kind : DefReduceKind) (e : Expr) : Option Expr :=
  match kind, e with
  | .beta, .app (.lam _ _ body _) arg => some (body.instantiate1 arg)
  | .zeta, .letE _ _ value body _ => some (body.instantiate1 value)
  | _, _ => none

/-- Exact expression-tree paths of every beta/zeta redex in preorder.  Child
    tags are stable within the `Expr` constructor named by the parent. -/
private partial def reducePaths (kind : DefReduceKind) (e : Expr)
    (path : Array Nat := #[]) : Array (Array Nat) :=
  let here := if (reduceHere? kind e).isSome then #[path] else #[]
  let children :=
    match e with
    | .app fn arg => reducePaths kind fn (path.push 0) ++ reducePaths kind arg (path.push 1)
    | .lam _ domain body _ | .forallE _ domain body _ =>
        reducePaths kind domain (path.push 0) ++ reducePaths kind body (path.push 1)
    | .letE _ type value body _ =>
        reducePaths kind type (path.push 0) ++ reducePaths kind value (path.push 1) ++
          reducePaths kind body (path.push 2)
    | .mdata _ body => reducePaths kind body (path.push 0)
    | .proj _ _ base => reducePaths kind base (path.push 0)
    | _ => #[]
  here ++ children

/-- Reduce the redex at one exact `reducePaths` coordinate. -/
private partial def reduceAtPath? (kind : DefReduceKind) (e : Expr)
    (path : List Nat) : Option Expr :=
  match path with
  | [] => reduceHere? kind e
  | step :: rest =>
      match e, step with
      | .app fn arg, 0 => (reduceAtPath? kind fn rest).map fun fn' => .app fn' arg
      | .app fn arg, 1 => (reduceAtPath? kind arg rest).map fun arg' => .app fn arg'
      | .lam n d b bi, 0 => (reduceAtPath? kind d rest).map fun d' => .lam n d' b bi
      | .lam n d b bi, 1 => (reduceAtPath? kind b rest).map fun b' => .lam n d b' bi
      | .forallE n d b bi, 0 =>
          (reduceAtPath? kind d rest).map fun d' => .forallE n d' b bi
      | .forallE n d b bi, 1 =>
          (reduceAtPath? kind b rest).map fun b' => .forallE n d b' bi
      | .letE n t v b nd, 0 =>
          (reduceAtPath? kind t rest).map fun t' => .letE n t' v b nd
      | .letE n t v b nd, 1 =>
          (reduceAtPath? kind v rest).map fun v' => .letE n t v' b nd
      | .letE n t v b nd, 2 =>
          (reduceAtPath? kind b rest).map fun b' => .letE n t v b' nd
      | .mdata md body, 0 => (reduceAtPath? kind body rest).map (.mdata md)
      | .proj typeName index base, 0 =>
          (reduceAtPath? kind base rest).map fun base' => .proj typeName index base'
      | _, _ => none

private def targetReflexive (e : Expr) : Bool :=
  let target := innermostTarget e
  let args := target.getAppArgs
  ((headName? target == some ``Eq && args.size == 3 && Expr.eqv args[1]! args[2]!) ||
   (headName? target == some ``Iff && args.size == 2 && Expr.eqv args[0]! args[1]!))

private def rangeIncrement? (e : Expr) : Option Expr :=
  let args := e.getAppArgs
  if headName? e == some ``Membership.mem && args.size >= 2 then
    let container := args[args.size - 1]!
    let rangeArgs := container.getAppArgs
    if headName? container == some ``Finset.range && rangeArgs.size == 1 then
      let bound := rangeArgs[0]!
      let incremented := mkApp (mkConst ``Nat.succ) bound
      let container' := mkApp (mkConst ``Finset.range) incremented
      some (mkAppN e.getAppFn ((args.extract 0 (args.size - 1)).push container'))
    else none
  else none

/-- Increment exactly the first `Finset.range` coverage bound occurring in the final target. -/
private partial def incrementFirstRange? (e : Expr) : Option Expr :=
  match rangeIncrement? e with
  | some e' => some e'
  | none =>
      match e with
      | .app fn arg =>
          match incrementFirstRange? fn with
          | some fn' => some (.app fn' arg)
          | none => (incrementFirstRange? arg).map (.app fn)
      | .lam name domain body bi =>
          match incrementFirstRange? domain with
          | some domain' => some (.lam name domain' body bi)
          | none => (incrementFirstRange? body).map fun body' => .lam name domain body' bi
      | .forallE name domain body bi =>
          match incrementFirstRange? domain with
          | some domain' => some (.forallE name domain' body bi)
          | none => (incrementFirstRange? body).map fun body' => .forallE name domain body' bi
      | .letE name type value body nondep =>
          match incrementFirstRange? type with
          | some type' => some (.letE name type' value body nondep)
          | none =>
              match incrementFirstRange? value with
              | some value' => some (.letE name type value' body nondep)
              | none => (incrementFirstRange? body).map fun body' =>
                  .letE name type value body' nondep
      | .mdata md body => (incrementFirstRange? body).map (.mdata md)
      | .proj typeName index base =>
          (incrementFirstRange? base).map fun base' => .proj typeName index base'
      | _ => none

/-- Walk the outer telescope with fvars; `pred` sees the binder index, name,
    closed domain, binder info, the raw continuation (loose bvar 0 = this
    binder) and the fvars of all earlier binders. -/
private partial def findBinderSite (e : Expr) (idx : Nat) (xs : Array Expr)
    (pred : Nat → Name → Expr → BinderInfo → Expr → Array Expr → MetaM Bool) :
    MetaM (Option Nat) := do
  match e with
  | .forallE n d b bi =>
      if ← pred idx n d bi b xs then
        return some idx
      withLocalDecl n bi d fun x => findBinderSite (b.instantiate1 x) (idx + 1) (xs.push x) pred
  | _ => return none

/-- Wave 4 counterpart of `findBinderSite`: enumerate every matching outer-binder
    site in stable telescope order.  The returned indices are exact replay
    coordinates, not a heuristic applicability census. -/
private partial def findBinderSites (e : Expr) (idx : Nat) (xs : Array Expr)
    (pred : Nat → Name → Expr → BinderInfo → Expr → Array Expr → MetaM Bool) :
    MetaM (Array Nat) := do
  match e with
  | .forallE n d b bi =>
      let here := if ← pred idx n d bi b xs then #[idx] else #[]
      withLocalDecl n bi d fun x => do
        return here ++ (← findBinderSites (b.instantiate1 x) (idx + 1) (xs.push x) pred)
  | _ => return #[]

private def p14Pred (_idx : Nat) (n : Name) (d : Expr) (bi : BinderInfo) (b : Expr)
    (_xs : Array Expr) : MetaM Bool := do
  unless bi == .default && !n.hasMacroScopes do return false
  if ← isProp d then return false
  match b with
  | .forallE n2 d2 _ bi2 =>
      unless bi2 == .default && !n2.hasMacroScopes && !d2.hasLooseBVar 0 do return false
      if ← isProp d2 then return false
      return true
  | _ => return false

private def p23Pred (_idx : Nat) (_n : Name) (d : Expr) (bi : BinderInfo) (b : Expr)
    (_xs : Array Expr) : MetaM Bool := do
  unless bi == .default do return false
  unless ← isProp d do return false
  match b with
  | .forallE _ d2 b2 bi2 =>
      unless bi2 == .default && !d2.hasLooseBVar 0 do return false
      unless ← isProp d2 do return false
      return !b2.hasLooseBVar 0 && !b2.hasLooseBVar 1
  | _ => return false

private def p24Pred (_idx : Nat) (n : Name) (d : Expr) (bi : BinderInfo) (b : Expr)
    (_xs : Array Expr) : MetaM Bool := do
  unless bi == .default && !n.hasMacroScopes do return false
  unless ← isProp d do return false
  match b with
  | .forallE n2 d2 _ bi2 =>
      unless bi2 == .default && !n2.hasMacroScopes && !d2.hasLooseBVar 0 do return false
      unless ← isProp d2 do return false
      return !Expr.eqv d d2
  | _ => return false

private partial def collectBinderNames (e : Expr) (acc : Array String) : Array String :=
  match e with
  | .forallE n d b _ =>
      collectBinderNames b (collectBinderNames d (acc.push n.eraseMacroScopes.toString))
  | .lam n d b _ =>
      collectBinderNames b (collectBinderNames d (acc.push n.eraseMacroScopes.toString))
  | .letE n t v b _ =>
      collectBinderNames b
        (collectBinderNames v (collectBinderNames t (acc.push n.eraseMacroScopes.toString)))
  | .app f a => collectBinderNames a (collectBinderNames f acc)
  | .mdata _ b => collectBinderNames b acc
  | .proj _ _ b => collectBinderNames b acc
  | _ => acc

/-- Deterministic hygienic name for the packed conjunction binder: `h` if
    capture-free, otherwise `h_<n>` for the smallest positive `n`. -/
private def freshPackName (reference : Expr) : Name :=
  let used := collectBinderNames reference #[]
  if !used.contains "h" then Name.mkSimple "h"
  else
    let rec go (n : Nat) (fuel : Nat) : Name :=
      match fuel with
      | 0 => Name.mkSimple s!"h_{n}"
      | fuel + 1 =>
          let s := s!"h_{n}"
          if !used.contains s then Name.mkSimple s else go (n + 1) fuel
    go 1 10000

/-! ### N31 guard schemas -/

private def natLit? (e : Expr) : Option Nat :=
  match e with
  | .lit (.natVal k) => some k
  | _ =>
    let args := e.getAppArgs
    if headName? e == some ``OfNat.ofNat && args.size == 3 && args[0]!.isConstOf ``Nat then
      match args[1]! with
      | .lit (.natVal k) => some k
      | _ => none
    else if e.isConstOf ``Nat.zero then some 0
    else none

private def intLit? (e : Expr) : Option Int :=
  let args := e.getAppArgs
  if headName? e == some ``OfNat.ofNat && args.size == 3 && args[0]!.isConstOf ``Int then
    match args[1]! with
    | .lit (.natVal k) => some (k : Int)
    | _ => none
  else if headName? e == some ``Neg.neg && args.size == 3 && args[0]!.isConstOf ``Int then
    let inner := args[2]!
    let innerArgs := inner.getAppArgs
    if headName? inner == some ``OfNat.ofNat && innerArgs.size == 3 then
      match innerArgs[1]! with
      | .lit (.natVal k) => some (-(k : Int))
      | _ => none
    else none
  else none

private def literal? (ty : String) (e : Expr) : Option Int :=
  if ty == "Nat" then (natLit? e).map (fun k => (k : Int)) else intLit? e

structure GuardMatch where
  schema : String
  variableIndex : Nat
  varType : String
  literal : Int
  boundaries : Array Int
  boundVariableIndex : Option Nat := none
  deriving Repr, Inhabited

private def boundariesFor (schema : String) (ty : String) (k : Int) : Option (Array Int) :=
  let isNat := ty == "Nat"
  match schema with
  | "lit_lt_var" => some #[k]
  | "var_lt_lit" => some #[k]
  | "lit_le_var" => if isNat && k ≤ 0 then none else some #[k - 1]
  | "var_le_lit" => some #[k + 1]
  | "var_ne_lit" => some #[k]
  | "var_eq_lit" =>
      if isNat then some (if k == 0 then #[1] else #[0, k + 1]) else some #[k + 1, k - 1]
  | _ => none

private def guardSchema (rel : String) (varLeft : Bool) : String :=
  match rel, varLeft with
  | "lt", true => "var_lt_lit"
  | "lt", false => "lit_lt_var"
  | "le", true => "var_le_lit"
  | "le", false => "lit_le_var"
  | "ne", _ => "var_ne_lit"
  | "eq", _ => "var_eq_lit"
  | _, _ => "unsupported"

/-- Match one bounded guard schema `rel x k` / `rel k x` on a `Nat`/`Int`
    binder `x` among the earlier fvars and a numeral `k`. -/
private def matchGuard (d : Expr) (xs : Array Expr) : MetaM (Option GuardMatch) := do
  let side (rel : String) (v k : Expr) (varLeft : Bool) : MetaM (Option GuardMatch) := do
    let some varIndex := xs.findIdx? (· == v) | return none
    unless v.isFVar do return none
    let some ty := isNatOrInt (← inferType v) | return none
    let some lit := literal? ty k | return none
    let schema := guardSchema rel varLeft
    let some bounds := boundariesFor schema ty lit | return none
    return some { schema, variableIndex := varIndex, varType := ty, literal := lit, boundaries := bounds }
  let classify (rel : String) (l r : Expr) : MetaM (Option GuardMatch) := do
    match ← side rel l r true with
    | some m => return some m
    | none => side rel r l false
  let args := d.getAppArgs
  match headName? d with
  | some n =>
      if n == ``LT.lt && args.size == 4 then classify "lt" args[2]! args[3]!
      else if n == ``GT.gt && args.size == 4 then classify "lt" args[3]! args[2]!
      else if n == ``LE.le && args.size == 4 then classify "le" args[2]! args[3]!
      else if n == ``GE.ge && args.size == 4 then classify "le" args[3]! args[2]!
      else if n == ``Ne && args.size == 3 then classify "ne" args[1]! args[2]!
      else if n == ``Not && args.size == 1 then
        let inner := args[0]!
        let innerArgs := inner.getAppArgs
        if headName? inner == some ``Eq && innerArgs.size == 3 then
          classify "ne" innerArgs[1]! innerArgs[2]!
        else return none
      else if n == ``Eq && args.size == 3 then classify "eq" args[1]! args[2]!
      else return none
  | none => return none

/-- Match exact index guards whose boundary comes from another earlier Nat
    binder rather than a numeral: `i ∈ Finset.range n` and `i < n`. -/
private def matchDependentIndexGuard (d : Expr) (xs : Array Expr) : MetaM (Option GuardMatch) := do
  let matched (schema : String) (value bound : Expr) : MetaM (Option GuardMatch) := do
    let some variableIndex := xs.findIdx? (· == value) | return none
    let some boundVariableIndex := xs.findIdx? (· == bound) | return none
    if variableIndex == boundVariableIndex || !value.isFVar || !bound.isFVar then return none
    unless (← inferType value).isConstOf ``Nat && (← inferType bound).isConstOf ``Nat do
      return none
    return some {
      schema
      variableIndex
      varType := "Nat"
      literal := 0
      boundaries := #[]
      boundVariableIndex := some boundVariableIndex
    }
  let args := d.getAppArgs
  if headName? d == some ``Membership.mem && args.size >= 2 then
    let value := args[args.size - 2]!
    let container := args[args.size - 1]!
    let rangeArgs := container.getAppArgs
    if headName? container == some ``Finset.range && rangeArgs.size == 1 then
      return ← matched "finset_range_bound" value rangeArgs[0]!
  if headName? d == some ``LT.lt && args.size == 4 then
    return ← matched "nat_lt_bound" args[2]! args[3]!
  return none

/-! ## Transform application (no proofs) -/

structure Applied where
  candidate : Expr
  site : Site
  deriving Repr

private def applyDefReduce (root : Root) (kind : DefReduceKind) : MetaM Applied := do
  let some candidate := reduceFirst? kind root.reference
    | throwNA (if kind == .beta then "p21_beta_no_redex" else "p21_zeta_no_redex")
  return {
    candidate
    site := {
      kind := "definitional_reduction"
      detail := if kind == .beta then "beta" else "zeta"
    }
  }

/-- Rewrite one exact final-target occurrence with a lemma.  Wave 3 calls the
    occurrence-one wrapper below; Wave 4 enumerates every consecutive
    occurrence and records the occurrence in the selected site. -/
private def rewriteFinalByLemmaAt (root : Root) (kind : String) (lemmaName : Name)
    (occurrence : Nat) : MetaM Applied := do
  if occurrence == 0 then throwNA s!"{kind}_occurrence_zero"
  let candidate ← forallTelescope root.reference fun xs body => do
    let attempt : Except String RewriteResult ← tryCatchRuntimeEx
      (do
        let ambient ← mkFreshExprMVar none
        let rewriteLemma ← mkConstWithFreshMVarLevels lemmaName
        return .ok (← ambient.mvarId!.rewrite body rewriteLemma false
          { occs := .pos [occurrence] }))
      fun ex => do
        if ex.isInterrupt then throw ex
        return .error (← exceptionText ex)
    let .ok result := attempt | throwNA s!"{kind}_not_applicable"
    unless result.mvarIds.isEmpty do throwNA s!"{kind}_unresolved_rewrite"
    mkForallFVars xs result.eNew
  if targetReflexive candidate then throwRej s!"{kind}_claim_erasure_reflexive"
  return {
    candidate
    site := {
      kind := kind
      index := occurrence
      detail := lemmaName.toString
      path := #[2, occurrence]
    }
  }

/-- Historical single-site wrapper. -/
private def rewriteFinalByLemma (root : Root) (kind : String) (lemmaName : Name) : MetaM Applied :=
  rewriteFinalByLemmaAt root kind lemmaName 1

/-- Apply the first exact rewrite lemma that is structurally applicable.  A
    failed applicability probe is silent; any rejected or infrastructure
    failure remains fail-closed. -/
private def rewriteFinalByFirstLemma
    (root : Root) (kind : String) (lemmas : Array Name) : MetaM Applied := do
  for lemmaName in lemmas do
    let attempt : Except String Applied ← tryCatchRuntimeEx
      (do return .ok (← rewriteFinalByLemma root kind lemmaName)) fun ex => do
        if ex.isInterrupt then throw ex
        let msg ← exceptionText ex
        if msg.startsWith naPrefix then return .error msg else throw ex
    if let .ok applied := attempt then return applied
  throwNA s!"{kind}_not_applicable"

private def widenIndexGuard? (guard : Expr) (schema : String) : Option Expr :=
  if schema == "finset_range_bound" then
    rangeIncrement? guard
  else if schema == "nat_lt_bound" then
    let args := guard.getAppArgs
    if headName? guard == some ``LT.lt && args.size == 4 then
      let incremented := mkApp (mkConst ``Nat.succ) args[3]!
      some (mkApp4 guard.getAppFn args[0]! args[1]! args[2]! incremented)
    else none
  else none

private def discoverN26 (root : Root) : MetaM (Nat × GuardMatch) := do
  let found ← IO.mkRef (none : Option GuardMatch)
  let site ← findBinderSite root.reference 0 #[] fun _ _ d bi b xs => do
    unless bi == .default do return false
    unless ← isProp d do return false
    if b.hasLooseBVar 0 then return false
    match ← matchDependentIndexGuard d xs with
    | some m => found.set (some m); return true
    | none => return false
  match site, ← found.get with
  | some g, some m => return (g, m)
  | _, _ => throwNA "n26_no_supported_bounded_binder"

private def applyWidenGuardAt (root : Root) (g : Nat) (schema : String) : MetaM Expr := do
  forallBoundedTelescope root.reference (some (g + 1)) fun xs body => do
    unless xs.size == g + 1 do throwNA "n26_site_out_of_range"
    let guard := xs[g]!
    if body.containsFVar guard.fvarId! then throwNA "n26_guard_used_by_continuation"
    let guardType ← inferType guard
    let some widened := widenIndexGuard? guardType schema
      | throwNA "n26_guard_schema_mismatch"
    withLocalDecl (← guard.fvarId!.getUserName) .default widened fun widenedGuard => do
      mkForallFVars ((xs.extract 0 g).push widenedGuard) body

private def applyN26 (root : Root) : MetaM Applied := do
  let canonical : Except String (Nat × GuardMatch) ← tryCatchRuntimeEx
    (do return .ok (← discoverN26 root)) fun ex => do
      if ex.isInterrupt then throw ex
      let msg ← exceptionText ex
      if msg.startsWith naPrefix then return .error msg else throw ex
  match canonical with
  | .ok (g, m) =>
      let candidate ← applyWidenGuardAt root g m.schema
      let some boundIndex := m.boundVariableIndex | throwRej "n26_missing_bound_index"
      return {
        candidate
        site := {
          kind := "bounded_binder_guard"
          index := g
          detail := m.schema
          guardVariableIndex := m.variableIndex
          boundVariableIndex := some boundIndex
        }
      }
  | .error _ => throwNA "n26_no_supported_bounded_binder"

/-- Move the last explicit finite data binder across a final existential:
    `∀ x, ∃ y, R x y` becomes `∃ y, ∀ x, R x y`.  Certification later
    requires complete structural enumeration of both domains. -/
private def applyN29 (root : Root) : MetaM Applied := do
  let candidate ← forallTelescope root.reference fun xs body => do
    if xs.isEmpty then throwNA "n29_no_forall_exists_suffix"
    let i := xs.size - 1
    let x := xs[i]!
    unless (← x.fvarId!.getBinderInfo) == .default do
      throwNA "n29_last_binder_not_explicit"
    let xType ← inferType x
    if ← isProp xType then throwNA "n29_last_binder_not_data"
    let args := body.getAppArgs
    unless headName? body == some ``Exists && args.size == 2 do
      throwNA "n29_no_forall_exists_suffix"
    let witnessType := args[0]!
    if witnessType.containsFVar x.fvarId! then
      throwNA "n29_dependent_witness_domain"
    let predicate := args[1]!
    withLocalDecl `y .default witnessType fun y => do
      let relation := mkApp predicate y
      let forallX ← mkForallFVars #[x] relation
      let witnessPredicate ← mkLambdaFVars #[y] forallX
      let moved := mkApp2 body.getAppFn witnessType witnessPredicate
      mkForallFVars (xs.extract 0 i) moved
  return {
    candidate
    site := { kind := "finite_forall_exists_dependency", detail := "forall_exists_to_exists_forall" }
  }

private def applyFinalTarget (root : Root) (kind : String)
    (f : Expr → Option (Expr × String)) : MetaM Applied := do
  let some (candidate, depth) := rewriteFinalTarget root.reference (fun t => (f t).map (·.1))
    | throwNA s!"{kind}_not_applicable"
  let some (_, detail) := f (innermostTarget root.reference)
    | throwNA s!"{kind}_not_applicable"
  return { candidate, site := { kind, index := depth, detail } }

private def applyP14At (root : Root) (i : Nat) : MetaM Expr := do
  forallBoundedTelescope root.reference (some (i + 2)) fun xs body => do
    unless xs.size == i + 2 do throwNA "p14_site_out_of_range"
    let a := xs[i]!
    let b := xs[i + 1]!
    if (← inferType b).containsFVar a.fvarId! then throwNA "p14_dependent_binders"
    let swapped := (xs.set! i b).set! (i + 1) a
    mkForallFVars swapped body

private def applyP23At (root : Root) (i : Nat) (packName : Name) : MetaM Expr := do
  forallBoundedTelescope root.reference (some (i + 2)) fun xs body => do
    unless xs.size == i + 2 do throwNA "p23_site_out_of_range"
    let a := xs[i]!
    let b := xs[i + 1]!
    let aType ← inferType a
    let bType ← inferType b
    if bType.containsFVar a.fvarId! || body.containsFVar a.fvarId! ||
        body.containsFVar b.fvarId! then
      throwNA "p23_proof_dependent_continuation"
    withLocalDecl packName .default (mkApp2 (mkConst ``And) aType bType) fun hab => do
      mkForallFVars ((xs.extract 0 i).push hab) body

private def applyN31At (root : Root) (g : Nat) : MetaM Expr := do
  forallBoundedTelescope root.reference (some (g + 1)) fun xs body => do
    unless xs.size == g + 1 do throwNA "n31_site_out_of_range"
    let guard := xs[g]!
    if body.containsFVar guard.fvarId! then throwNA "n31_guard_used_by_continuation"
    mkForallFVars (xs.extract 0 g) body

/-- Tactic scripts allowed to discharge a redundant guard from the preceding
    context.  Every produced term is abstracted and kernel-checked inside the
    complete `Iff` witness. -/
private def redundantGuardScripts : MetaM (Array (String × Syntax)) := do
  return #[
    ("assumption", ← `(by assumption)),
    ("omega", ← `(by omega)),
    ("positivity", ← `(by positivity)),
    ("simp_all", ← `(by simp_all))
  ]

/-- Prove `goal` in the current local context with a bounded tactic script;
    the proof may mention the context's fvars. -/
private def proveInContext? (goal : Expr) : MetaM (Option (Expr × String)) := do
  for (label, stx) in ← redundantGuardScripts do
    let attempt ← withRestoredMessages <| tryCatchRuntimeEx
      (do
        let proof ← Term.TermElabM.run' <| Term.withoutErrToSorry do
          let proof ← Term.elabTermEnsuringType stx (some goal)
          Term.synthesizeSyntheticMVarsNoPostponing
          instantiateMVars proof
        if proof.hasExprMVar || proof.hasLevelMVar || proof.hasLooseBVars || proof.hasSorry then
          return none
        check proof
        unless ← withoutModifyingMCtx (isDefEq (← inferType proof) goal) do return none
        return some proof)
      fun ex => do
        if ex.isInterrupt then throw ex
        return none
    if let some proof := attempt then
      return some (proof, label)
  return none

/-- First explicit proposition binder whose type is provable from the
    preceding context and whose proof the continuation does not use. -/
private def discoverRedundantGuard (root : Root) : MetaM (Nat × String) := do
  let found ← IO.mkRef (none : Option String)
  let site ← findBinderSite root.reference 0 #[] fun _ _ d bi b _ => do
    unless bi == .default do return false
    unless ← isProp d do return false
    if b.hasLooseBVar 0 then return false
    match ← proveInContext? d with
    | some (_, label) => found.set (some label); return true
    | none => return false
  match site, ← found.get with
  | some g, some label => return (g, label)
  | _, _ => throwNA "pdrg_no_redundant_guard"

private def applyDropGuardAt (root : Root) (g : Nat) : MetaM Expr := do
  forallBoundedTelescope root.reference (some (g + 1)) fun xs body => do
    unless xs.size == g + 1 do throwNA "pdrg_site_out_of_range"
    let guard := xs[g]!
    if body.containsFVar guard.fvarId! then throwNA "pdrg_guard_used_by_continuation"
    mkForallFVars (xs.extract 0 g) body

private def discoverN31 (root : Root) : MetaM (Nat × GuardMatch) := do
  let found ← IO.mkRef (none : Option GuardMatch)
  let site ← findBinderSite root.reference 0 #[] fun _ _ d bi b xs => do
    unless bi == .default do return false
    unless ← isProp d do return false
    if b.hasLooseBVar 0 then return false
    match ← matchGuard d xs with
    | some m => found.set (some m); return true
    | none =>
        match ← matchDependentIndexGuard d xs with
        | some m => found.set (some m); return true
        | none => return false
  match site, ← found.get with
  | some g, some m => return (g, m)
  | _, _ => throwNA "n31_no_supported_guard"

/-- Apply `op` to the root by discovering its unique deterministic site. -/
def applyOp (root : Root) (op : Op) : MetaM Applied := do
  match op with
  | .p15 => applyFinalTarget root "final_target_iff" swapIff?
  | .p18 => applyFinalTarget root "final_target_eq" swapEq?
  | .n25 => applyFinalTarget root "final_target_eq_ne" toggleEqNe?
  | .n32 => do
      let applied ← applyFinalTarget root "final_target_order" swapRoleOrder?
      let relationKind := if applied.site.detail == "strict_lt" then "final_target_lt"
        else "final_target_le"
      return { applied with site := { applied.site with kind := relationKind } }
  | .pne => applyFinalTarget root "final_target_ne" swapNe?
  | .p21Beta => applyDefReduce root .beta
  | .p21Zeta => applyDefReduce root .zeta
  | .p32Assoc => rewriteFinalByLemma root "p32_add_assoc" ``add_assoc
  | .p32Comm => rewriteFinalByLemma root "p32_add_comm" ``add_comm
  | .p35 => rewriteFinalByLemma root "p35_set_inter_membership" ``Set.mem_inter_iff
  | .n26 => applyN26 root
  | .n30 => applyFinalTarget root "final_target_exists_unique" existsToExistsUnique?
  | .n29 => applyN29 root
  | .p16 => rewriteFinalByLemma root "p16_and_assoc" ``and_assoc
  | .p28 => rewriteFinalByLemma root "p28_iff_def" ``iff_def
  | .pOrderComplement =>
      rewriteFinalByFirstLemma root "p_order_complement" #[``not_le, ``not_lt]
  | .pdrg =>
      let (g, label) ← discoverRedundantGuard root
      let candidate ← applyDropGuardAt root g
      return {
        candidate
        site := { kind := "redundant_guard", index := g, detail := label }
      }
  | .p14 =>
      let some i ← findBinderSite root.reference 0 #[] p14Pred
        | throwNA "p14_no_adjacent_independent_data_binders"
      let candidate ← applyP14At root i
      return { candidate, site := { kind := "adjacent_data_binders", index := i, detail := "swap" } }
  | .p23 =>
      let some i ← findBinderSite root.reference 0 #[] p23Pred
        | throwNA "p23_no_adjacent_independent_prop_binders"
      let packName := freshPackName root.reference
      let candidate ← applyP23At root i packName
      return {
        candidate
        site := { kind := "adjacent_prop_binders", index := i, detail := packName.toString }
      }
  | .p24 =>
      let some i ← findBinderSite root.reference 0 #[] p24Pred
        | throwNA "p24_no_adjacent_independent_prop_binders"
      let candidate ← applyP14At root i
      return {
        candidate
        site := { kind := "adjacent_prop_binders", index := i, detail := "swap" }
      }
  | .n31 =>
      let (g, m) ← discoverN31 root
      let candidate ← applyN31At root g
      return {
        candidate
        site := {
          kind := "required_guard"
          index := g
          detail := s!"{m.schema}:{m.varType}"
          guardVariableIndex := m.variableIndex
          boundVariableIndex := m.boundVariableIndex
          literal := m.literal
        }
      }

/-- Replay a certificate site against the reference without rediscovery where
    the site alone determines the candidate. -/
def replayOp (root : Root) (op : Op) (site : Site) : MetaM Expr := do
  match op with
  | .p15 | .p18 | .n25 | .n32 | .pne
  | .p21Beta | .p21Zeta | .p32Assoc | .p32Comm | .p35 | .n26
  | .n30 | .n29 | .p16 | .p28 | .pOrderComplement =>
      let applied ← applyOp root op
      unless applied.site == site do throwRej "replay_site_mismatch"
      return applied.candidate
  | .pdrg => applyDropGuardAt root site.index
  | .p14 => applyP14At root site.index
  | .p24 => applyP14At root site.index
  | .p23 =>
      let packName := freshPackName root.reference
      unless packName.toString == site.detail do throwRej "replay_pack_name_mismatch"
      applyP23At root site.index packName
  | .n31 =>
      let applied ← applyOp root .n31
      unless applied.site == site do throwRej "replay_site_mismatch"
      return applied.candidate

/-! ## Wave 4 exact operation/site enumeration -/

private partial def redundantGuardSites (e : Expr) (idx : Nat) :
    MetaM (Array (Nat × String)) := do
  match e with
  | .forallE n d b bi =>
      let mut here : Array (Nat × String) := #[]
      if bi == .default && (← isProp d) && !b.hasLooseBVar 0 then
        if let some (_, label) ← proveInContext? d then
          here := here.push (idx, label)
      withLocalDecl n bi d fun x => do
        return here ++ (← redundantGuardSites (b.instantiate1 x) (idx + 1))
  | _ => return #[]

private partial def enumerateLemmaSites (root : Root) (kind : String) (lemmaName : Name)
    (occurrence : Nat := 1) (acc : Array Applied := #[]) : MetaM (Array Applied) := do
  let attempt : Except String Applied ← tryCatchRuntimeEx
    (do return .ok (← rewriteFinalByLemmaAt root kind lemmaName occurrence)) fun ex => do
      if ex.isInterrupt then throw ex
      let msg ← exceptionText ex
      if msg.startsWith naPrefix then return .error msg else throw ex
  match attempt with
  | .error _ => return acc
  | .ok applied => enumerateLemmaSites root kind lemmaName (occurrence + 1) (acc.push applied)

private def wave4PathForSingle (site : Site) : Array Nat :=
  if !site.path.isEmpty then site.path else #[4, site.index]

/-- Replay one Wave 4 preserving operation at the exact enumerated site.  This
    never rediscovers a different occurrence. -/
def replayWave4Op (root : Root) (op : Op) (site : Site) : MetaM Expr := do
  match op with
  | .p14 | .p24 => applyP14At root site.index
  | .p23 => applyP23At root site.index (Name.mkSimple site.detail)
  | .pdrg => applyDropGuardAt root site.index
  | .p21Beta =>
      let some candidate := reduceAtPath? .beta root.reference site.path.toList
        | throwRej "wave4_beta_site_replay_failed"
      return candidate
  | .p21Zeta =>
      let some candidate := reduceAtPath? .zeta root.reference site.path.toList
        | throwRej "wave4_zeta_site_replay_failed"
      return candidate
  | .p32Assoc => return (← rewriteFinalByLemmaAt root site.kind ``add_assoc site.index).candidate
  | .p32Comm => return (← rewriteFinalByLemmaAt root site.kind ``add_comm site.index).candidate
  | .p35 => return (← rewriteFinalByLemmaAt root site.kind ``Set.mem_inter_iff site.index).candidate
  | .p16 => return (← rewriteFinalByLemmaAt root site.kind ``and_assoc site.index).candidate
  | .p28 => return (← rewriteFinalByLemmaAt root site.kind ``iff_def site.index).candidate
  | .pOrderComplement =>
      if site.detail == "not_le" then
        return (← rewriteFinalByLemmaAt root site.kind ``not_le site.index).candidate
      else if site.detail == "not_lt" then
        return (← rewriteFinalByLemmaAt root site.kind ``not_lt site.index).candidate
      else throwRej "wave4_order_complement_site_replay_failed"
  | .p15 | .p18 | .pne =>
      let applied ← applyOp root op
      unless applied.site.index == site.index && applied.site.detail == site.detail do
        throwRej "wave4_final_target_site_replay_failed"
      return applied.candidate
  | _ => throwRej "wave4_nonpreserving_operation"

/-- Enumerate every exact site supported by the compact preserving engine.
    Result order is operation-local structural order; Python applies the final
    content-hash selection only after the full root enumeration terminal. -/
def enumerateWave4Op (root : Root) (op : Op) : MetaM (Array Applied) := do
  unless op.positive do throwRej "wave4_nonpreserving_operation"
  let mut result : Array Applied := #[]
  match op with
  | .p14 =>
    for i in ← findBinderSites root.reference 0 #[] p14Pred do
      let candidate ← applyP14At root i
      result := result.push {
        candidate := candidate
        site := {
          kind := "adjacent_data_binders"
          index := i
          detail := "swap"
          path := #[3, i]
        }
      }
  | .p24 =>
    for i in ← findBinderSites root.reference 0 #[] p24Pred do
      let candidate ← applyP14At root i
      result := result.push {
        candidate := candidate
        site := {
          kind := "adjacent_prop_binders"
          index := i
          detail := "swap"
          path := #[3, i]
        }
      }
  | .p23 =>
    for i in ← findBinderSites root.reference 0 #[] p23Pred do
      let packName := freshPackName root.reference
      let candidate ← applyP23At root i packName
      result := result.push {
        candidate := candidate
        site := {
          kind := "adjacent_prop_binders"
          index := i
          detail := packName.toString
          path := #[3, i]
        }
      }
  | .pdrg =>
    for (i, label) in ← redundantGuardSites root.reference 0 do
      let candidate ← applyDropGuardAt root i
      result := result.push {
        candidate := candidate
        site := {
          kind := "redundant_guard"
          index := i
          detail := label
          path := #[3, i]
        }
      }
  | .p21Beta | .p21Zeta =>
      let kind := if op == .p21Beta then DefReduceKind.beta else DefReduceKind.zeta
      for path in reducePaths kind root.reference do
        let some candidate := reduceAtPath? kind root.reference path.toList
          | throwRej "wave4_reduction_site_replay_failed"
        result := result.push {
          candidate := candidate
          site := {
            kind := "definitional_reduction"
            detail := if kind == .beta then "beta" else "zeta"
            path := path
          }
        }
  | .p32Assoc => result := ← enumerateLemmaSites root "p32_add_assoc" ``add_assoc
  | .p32Comm => result := ← enumerateLemmaSites root "p32_add_comm" ``add_comm
  | .p35 => result := ← enumerateLemmaSites root "p35_set_inter_membership" ``Set.mem_inter_iff
  | .p16 => result := ← enumerateLemmaSites root "p16_and_assoc" ``and_assoc
  | .p28 => result := ← enumerateLemmaSites root "p28_iff_def" ``iff_def
  | .pOrderComplement =>
      result := (← enumerateLemmaSites root "p_order_complement" ``not_le) ++
        (← enumerateLemmaSites root "p_order_complement" ``not_lt)
  | .p15 | .p18 | .pne =>
      let attempt : Except String Applied ← tryCatchRuntimeEx
        (do return .ok (← applyOp root op)) fun ex => do
          if ex.isInterrupt then throw ex
          let msg ← exceptionText ex
          if msg.startsWith naPrefix then return .error msg else throw ex
      if let .ok applied := attempt then
        result := result.push { applied with site :=
          { applied.site with path := wave4PathForSingle applied.site } }
  | _ => pure ()
  -- An enumerated candidate must replay from the exact site before it can
  -- participate in a chain.  Duplicate exact operation/site results fail.
  let mut seen : Array Site := #[]
  for applied in result do
    if seen.contains applied.site then throwRej "wave4_duplicate_operation_site"
    seen := seen.push applied.site
    let replayed ← replayWave4Op root op applied.site
    unless Expr.equal replayed applied.candidate do
      throwRej "wave4_operation_site_replay_mismatch"
  return result

/-! ## Positive witnesses -/

private def iffIntro (a b mp mpr : Expr) : Expr :=
  mkApp4 (mkConst ``Iff.intro) a b mp mpr

private def finalTargetIffProof (ref cand : Expr) (symm : Expr → MetaM Expr) : MetaM Expr := do
  let mp ← withLocalDecl `h .default ref fun h => do
    let body ← forallTelescope ref fun xs _ => do
      mkLambdaFVars xs (← symm (mkAppN h xs))
    mkLambdaFVars #[h] body
  let mpr ← withLocalDecl `h .default cand fun h => do
    let body ← forallTelescope cand fun xs _ => do
      mkLambdaFVars xs (← symm (mkAppN h xs))
    mkLambdaFVars #[h] body
  return iffIntro ref cand mp mpr

private def p14IffProof (ref cand : Expr) (i : Nat) : MetaM Expr := do
  forallBoundedTelescope ref (some (i + 2)) fun xs _ => do
    let swapped := (xs.set! i xs[i + 1]!).set! (i + 1) xs[i]!
    let mp ← withLocalDecl `h .default ref fun h => do
      mkLambdaFVars #[h] (← mkLambdaFVars swapped (mkAppN h xs))
    let mpr ← withLocalDecl `h .default cand fun h => do
      mkLambdaFVars #[h] (← mkLambdaFVars xs (mkAppN h swapped))
    return iffIntro ref cand mp mpr

private def p23IffProof (ref cand : Expr) (i : Nat) (packName : Name) : MetaM Expr := do
  forallBoundedTelescope ref (some (i + 2)) fun xs _ => do
    let a := xs[i]!
    let b := xs[i + 1]!
    let aType ← inferType a
    let bType ← inferType b
    let prefixArgs := xs.extract 0 i
    withLocalDecl packName .default (mkApp2 (mkConst ``And) aType bType) fun hab => do
      let left := mkApp3 (mkConst ``And.left) aType bType hab
      let right := mkApp3 (mkConst ``And.right) aType bType hab
      let mp ← withLocalDecl `h .default ref fun h => do
        mkLambdaFVars #[h]
          (← mkLambdaFVars (prefixArgs.push hab) (mkAppN h ((prefixArgs.push left).push right)))
      let pairProof := mkApp4 (mkConst ``And.intro) aType bType a b
      let mpr ← withLocalDecl `h .default cand fun h => do
        mkLambdaFVars #[h] (← mkLambdaFVars xs (mkAppN h (prefixArgs.push pairProof)))
      return iffIntro ref cand mp mpr

private def dropGuardIffProof (ref cand : Expr) (g : Nat) : MetaM (Expr × String) := do
  forallBoundedTelescope ref (some (g + 1)) fun xs _ => do
    let prefixArgs := xs.extract 0 g
    let guardType ← inferType xs[g]!
    let some (guardProof, label) ← proveInContext? guardType
      | throwRej "pdrg_guard_proof_not_reproducible"
    let mp ← withLocalDecl `h .default ref fun h => do
      mkLambdaFVars #[h] (← mkLambdaFVars prefixArgs (mkAppN h (prefixArgs.push guardProof)))
    let mpr ← withLocalDecl `h .default cand fun h => do
      mkLambdaFVars #[h] (← mkLambdaFVars xs (mkAppN h prefixArgs))
    return (iffIntro ref cand mp mpr, label)

private def defeqIffProof (ref cand : Expr) : MetaM Expr := do
  unless ← withoutModifyingMCtx (isDefEq ref cand) do
    throwRej "definitional_equivalence_failed"
  return mkApp (mkConst ``Iff.rfl) ref

/-- Re-run one exact lemma occurrence and turn its equality proof for the final
    target into a complete `Iff` proof under the original telescope. -/
private def lemmaRewriteIffProofAt (ref cand : Expr) (lemmaName : Name)
    (occurrence : Nat) : MetaM Expr := do
  if occurrence == 0 then throwRej "lemma_rewrite_occurrence_zero"
  forallTelescope ref fun xs body => do
    let ambient ← mkFreshExprMVar none
    let rewriteLemma ← mkConstWithFreshMVarLevels lemmaName
    let result ← ambient.mvarId!.rewrite body rewriteLemma false { occs := .pos [occurrence] }
    unless result.mvarIds.isEmpty do throwRej "lemma_rewrite_unresolved_goals"
    let rebuilt ← mkForallFVars xs result.eNew
    unless Expr.equal rebuilt cand do throwRej "lemma_rewrite_candidate_mismatch"
    let mp ← withLocalDecl `h .default ref fun h => do
      let oldProof := mkAppN h xs
      let newProof ← mkEqMP result.eqProof oldProof
      mkLambdaFVars #[h] (← mkLambdaFVars xs newProof)
    let eqSymm ← mkEqSymm result.eqProof
    let mpr ← withLocalDecl `h .default cand fun h => do
      let newProof := mkAppN h xs
      let oldProof ← mkEqMP eqSymm newProof
      mkLambdaFVars #[h] (← mkLambdaFVars xs oldProof)
    return iffIntro ref cand mp mpr

private def lemmaRewriteIffProof (ref cand : Expr) (lemmaName : Name) : MetaM Expr :=
  lemmaRewriteIffProofAt ref cand lemmaName 1

/-! ## Grounding search for negatives -/

structure Grounding where
  values : Array Expr
  descriptions : Array String
  deriving Inhabited

inductive DecideOutcome where
  | proved (proof : Expr)
  | refuted
  | unknown

/-- Evaluate `decide p` by Meta and kernel reduction.  A `false` evaluation
    is a definite refutation of `p`, so no tactic search is attempted. -/
private def decideOutcome (p : Expr) : MetaM DecideOutcome := do
  tryCatchRuntimeEx
    (do
      let some inst ← synthInstance? (mkApp (mkConst ``Decidable) p) | return .unknown
      let dec := mkApp2 (mkConst ``Decidable.decide) p inst
      let reduced ← withDefault <| whnf dec
      let reduced ←
        if reduced.isConstOf ``Bool.true || reduced.isConstOf ``Bool.false then pure reduced
        else
          match Kernel.whnf (← getEnv) {} dec with
          | .ok r => pure r
          | .error _ => pure reduced
      if reduced.isConstOf ``Bool.false then return .refuted
      unless reduced.isConstOf ``Bool.true do return .unknown
      let refl := mkApp2 (mkConst ``Eq.refl [Level.one]) (mkConst ``Bool) (mkConst ``Bool.true)
      return .proved (mkApp3 (mkConst ``of_decide_eq_true) p inst refl))
    fun ex => do
      if ex.isInterrupt then throw ex
      return .unknown

private def tacticProof? (p : Expr) (stx : Syntax) : MetaM (Option Expr) := withRestoredMessages do
  tryCatchRuntimeEx
    (do
      let proof ← Term.TermElabM.run' <| Term.withoutErrToSorry do
        let proof ← Term.elabTermEnsuringType stx (some p)
        Term.synthesizeSyntheticMVarsNoPostponing
        instantiateMVars proof
      unless isClosedPlaceholderFree proof do return none
      check proof
      unless ← withoutModifyingMCtx (isDefEq (← inferType proof) p) do return none
      return some proof)
    fun ex => do
      if ex.isInterrupt then throw ex
      return none

private def tacticScripts : MetaM (Array (String × Syntax)) := do
  return #[
    ("omega", ← `(by omega)),
    ("norm_num", ← `(by norm_num)),
    ("simp", ← `(by simp)),
    ("nonempty_default", ← `(by exact ⟨default⟩)),
    ("decide", ← `(by decide))
  ]

/-- Prove a closed proposition with `decide`, then bounded tactics. -/
private def proveClosed? (p : Expr) (tacticBudget : IO.Ref Nat) :
    MetaM (Option (Expr × String)) := do
  match ← decideOutcome p with
  | .proved proof => return some (proof, "decide")
  | .refuted => return none
  | .unknown => pure ()
  for (label, stx) in ← tacticScripts do
    let remaining ← tacticBudget.get
    if remaining == 0 then return none
    tacticBudget.set (remaining - 1)
    if let some proof ← tacticProof? p stx then
      return some (proof, label)
  return none

private def checkedDataValue? (d value : Expr) : MetaM (Option Expr) := do
  let value ← instantiateMVars value
  if value.hasExprMVar || value.hasLevelMVar || value.hasLooseBVars then return none
  let ok ← tryCatchRuntimeEx
    (do
      check value
      return ← withoutModifyingMCtx (isDefEq (← inferType value) d))
    fun ex => do
      if ex.isInterrupt then throw ex
      return false
  return if ok then some value else none

private def appendCheckedDistinct (d : Expr) (acc : Array (Expr × String))
    (value : Expr) (description : String) : MetaM (Array (Expr × String)) := do
  let some value ← checkedDataValue? d value | return acc
  if acc.any fun item => Expr.equal item.1 value then return acc
  return acc.push (value, description)

private def isDeterministicHeartbeatTimeout (msg : String) : Bool :=
  msg.contains "maximum number of heartbeats"

private def failClosedOnHeartbeat {α : Type} (reason : String) (action : MetaM α) : MetaM α :=
  tryCatchRuntimeEx action fun ex => do
    let msg ← exceptionText ex
    if isDeterministicHeartbeatTimeout msg then throwRej reason
    if ex.isInterrupt then throw ex
    throw ex

private def synthInstanceSafe? (type : Expr) : MetaM (Option Expr) :=
  tryCatchRuntimeEx (synthInstance? type) fun ex => do
    let msg ← exceptionText ex
    if isDeterministicHeartbeatTimeout msg then return none
    if ex.isInterrupt then throw ex
    return none

private def typeClassChoice? (d : Expr) : MetaM (Option (Expr × String)) := do
  let inhabitedType ← mkAppM ``Inhabited #[d]
  if let some inst ← synthInstanceSafe? inhabitedType then
    let value ← mkAppOptM ``Inhabited.default #[d, inst]
    if let some value ← checkedDataValue? d value then
      return some (value, "inhabited_default")
  let nonemptyType ← mkAppM ``Nonempty #[d]
  if let some inst ← synthInstanceSafe? nonemptyType then
    let value ← mkAppOptM ``Classical.choice #[d, inst]
    if let some value ← checkedDataValue? d value then
      return some (value, "nonempty_choice")
  return none

private partial def dataValues (d : Expr) (tacticBudget : IO.Ref Nat) :
    MetaM (Array (Expr × String)) := do
  let mut values : Array (Expr × String) := #[]
  if d.isConstOf ``Nat then
    values := #[(mkNatLit 0, "0"), (mkNatLit 1, "1"), (mkNatLit 2, "2"), (mkNatLit 3, "3")]
  else if d.isConstOf ``Int then
    values := #[(mkIntLit 0, "0"), (mkIntLit 1, "1"), (mkIntLit (-1), "-1"),
      (mkIntLit 2, "2"), (mkIntLit (-2), "-2")]
  else if d.isConstOf ``Bool then
    values := #[(mkConst ``Bool.true, "true"), (mkConst ``Bool.false, "false")]
  else if d.isConstOf ``Unit then
    values := #[(mkConst ``Unit.unit, "unit")]
  else if d == mkSort Level.zero then
    values := #[(mkConst ``True, "True"), (mkConst ``False, "False")]
  else if d == mkSort Level.one then
    let finThree := mkApp (mkConst ``Fin) (mkNatLit 3)
    let optionBool := mkApp (mkConst ``Option [Level.zero]) (mkConst ``Bool)
    let boolPair := mkApp2 (mkConst ``Prod [Level.zero, Level.zero])
      (mkConst ``Bool) (mkConst ``Bool)
    values := #[(mkConst ``Nat, "Nat"), (mkConst ``Int, "Int"),
      (mkConst ``Bool, "Bool"), (mkConst ``Unit, "Unit"),
      (finThree, "Fin 3"), (optionBool, "Option Bool"), (boolPair, "Bool × Bool")]
  else
    let args := d.getAppArgs
    match d.getAppFn with
    | .const n us =>
        if n == ``Fin && args.size == 1 then
          if let some bound := natLit? args[0]! then
            for i in List.range (min bound 8) do
              let lt ← mkAppM ``LT.lt #[mkNatLit i, args[0]!]
              if let some (proof, _) ← proveClosed? lt tacticBudget then
                let value ← mkAppM ``Fin.mk #[mkNatLit i, proof]
                values ← appendCheckedDistinct d values value s!"fin:{i}/{bound}"
        else if n == ``Option && args.size == 1 then
          values ← appendCheckedDistinct d values
            (mkApp (mkConst ``Option.none us) args[0]!) "none"
          for (value, desc) in ← dataValues args[0]! tacticBudget do
            values ← appendCheckedDistinct d values
              (mkApp2 (mkConst ``Option.some us) args[0]! value) s!"some({desc})"
        else if n == ``Prod && args.size == 2 then
          let left ← dataValues args[0]! tacticBudget
          let right ← dataValues args[1]! tacticBudget
          for (a, ad) in left do
            for (b, bd) in right do
              if values.size < 16 then
                values ← appendCheckedDistinct d values
                  (mkAppN (mkConst ``Prod.mk us) #[args[0]!, args[1]!, a, b])
                  s!"prod({ad},{bd})"
    | _ => pure ()
  if let some (value, description) ← typeClassChoice? d then
    values ← appendCheckedDistinct d values value description
  return values

structure FiniteDomain where
  values : Array (Expr × String)
  kind : String
  deriving Inhabited

/-- Complete, structurally justified enumeration.  Unlike `dataValues`, this
    is never a sample: callers may use it to certify uniqueness and quantifier
    dependency claims. -/
private partial def finiteDomain? (d : Expr) (tacticBudget : IO.Ref Nat) :
    MetaM (Option FiniteDomain) := do
  if d.isConstOf ``Bool then
    return some { values := #[(mkConst ``Bool.false, "false"),
      (mkConst ``Bool.true, "true")], kind := "Bool:complete" }
  if d.isConstOf ``Unit then
    return some { values := #[(mkConst ``Unit.unit, "unit")], kind := "Unit:complete" }
  let args := d.getAppArgs
  match d.getAppFn with
  | .const n us =>
      if n == ``Fin && args.size == 1 then
        let some bound := natLit? args[0]! | return none
        if bound > 32 then return none
        let mut values : Array (Expr × String) := #[]
        for i in List.range bound do
          let lt ← mkAppM ``LT.lt #[mkNatLit i, args[0]!]
          let some (proof, _) ← proveClosed? lt tacticBudget | return none
          let value ← mkAppM ``Fin.mk #[mkNatLit i, proof]
          let some value ← checkedDataValue? d value | return none
          values := values.push (value, s!"fin:{i}/{bound}")
        return some { values, kind := s!"Fin:{bound}:complete" }
      else if n == ``Option && args.size == 1 then
        let some inner ← finiteDomain? args[0]! tacticBudget | return none
        let mut values : Array (Expr × String) := #[]
        let noneValue := mkApp (mkConst ``Option.none us) args[0]!
        let some noneValue ← checkedDataValue? d noneValue | return none
        values := values.push (noneValue, "none")
        for (value, desc) in inner.values do
          let someValue := mkApp2 (mkConst ``Option.some us) args[0]! value
          let some someValue ← checkedDataValue? d someValue | return none
          if values.any fun item => Expr.equal item.1 someValue then return none
          values := values.push (someValue, s!"some({desc})")
        unless values.size == inner.values.size + 1 do return none
        return some { values, kind := s!"Option({inner.kind}):complete" }
      else if n == ``Prod && args.size == 2 then
        let some left ← finiteDomain? args[0]! tacticBudget | return none
        let some right ← finiteDomain? args[1]! tacticBudget | return none
        if left.values.size * right.values.size > 64 then return none
        let mut values : Array (Expr × String) := #[]
        for (a, ad) in left.values do
          for (b, bd) in right.values do
            let pairValue := mkAppN (mkConst ``Prod.mk us) #[args[0]!, args[1]!, a, b]
            let some pairValue ← checkedDataValue? d pairValue | return none
            if values.any fun item => Expr.equal item.1 pairValue then return none
            values := values.push (pairValue, s!"prod({ad},{bd})")
        unless values.size == left.values.size * right.values.size do return none
        return some { values, kind := s!"Prod({left.kind},{right.kind}):complete" }
      else return none
  | _ => return none

/-- A complete enumeration obtained from an already available `Fintype`
    instance.  The enumerated terms are the inverse image of every index under
    `Fintype.equivFin`; this is certificate-grade completeness, not a sampled
    list of convenient values.  Synthesis and reduction fail closed. -/
private def finiteEnumerableDomain? (d : Expr) (tacticBudget : IO.Ref Nat) :
    MetaM (Option FiniteDomain) := do
  if let some domain ← finiteDomain? d tacticBudget then
    return some domain
  if ← isProp d then return none
  let fintypeType ← mkAppM ``Fintype #[d]
  let some inst ← synthInstanceSafe? fintypeType | return none
  let cardExpr ← mkAppOptM ``Fintype.card #[d, inst]
  let cardExpr ← whnf cardExpr
  let some bound := natLit? cardExpr | return none
  if bound > 32 then return none
  let equiv ← mkAppOptM ``Fintype.equivFin #[d, inst]
  let symm ← mkAppM ``Equiv.symm #[equiv]
  let mut values : Array (Expr × String) := #[]
  for i in List.range bound do
    let lt ← mkAppM ``LT.lt #[mkNatLit i, cardExpr]
    let some (proof, _) ← proveClosed? lt tacticBudget | return none
    let index ← mkAppM ``Fin.mk #[mkNatLit i, proof]
    let value ← mkAppM ``Equiv.toFun #[symm, index]
    let some value ← checkedDataValue? d value | return none
    if values.any fun item => Expr.equal item.1 value then return none
    values := values.push (value, s!"fintype:{i}/{bound}")
  unless values.size == bound do return none
  return some { values, kind := s!"Fintype:{bound}:equivFin_complete" }

private def literalValue (ty : String) (k : Int) : Expr × String :=
  if ty == "Nat" then (mkNatLit k.toNat, toString k) else (mkIntLit k, toString k)

private def binderCandidates (d : Expr) (bi : BinderInfo) (idx : Nat)
    (constraint : Nat → Array Expr → Option (Array (Expr × String)))
    (acc : Array Expr) (tacticBudget : IO.Ref Nat) :
    MetaM (Array (Expr × String)) := do
  if let some forced := constraint idx acc then
    return forced
  if bi == .instImplicit then
    match ← synthInstanceSafe? d with
    | some inst => return #[(inst, "instance")]
    | none => return #[]
  if ← isProp d then
    match ← proveClosed? d tacticBudget with
    | some (proof, label) => return #[(proof, s!"proof:{label}")]
    | none => return #[]
  return ← dataValues d tacticBudget

/-- Depth-first grounding of a closed telescope.  `leaf` receives the closed
    innermost body and the assignment; the first assignment it accepts wins. -/
private partial def groundTelescope {β : Type} (ty : Expr) (idx : Nat)
    (constraint : Nat → Array Expr → Option (Array (Expr × String)))
    (acc : Array Expr) (descs : Array String)
    (branchBudget tacticBudget : IO.Ref Nat)
    (leaf : Expr → Grounding → MetaM (Option β)) : MetaM (Option (Grounding × β)) := do
  match ty with
  | .forallE _ d b bi =>
      let candidates ← binderCandidates d bi idx constraint acc tacticBudget
      for (v, s) in candidates do
        let remaining ← branchBudget.get
        if remaining == 0 then return none
        branchBudget.set (remaining - 1)
        let rest := b.instantiate1 v
        if let some r ← groundTelescope rest (idx + 1) constraint (acc.push v) (descs.push s)
            branchBudget tacticBudget leaf then
          return some r
      return none
  | body =>
      if body.hasLooseBVars then throwRej "grounding_left_loose_bvars"
      let g : Grounding := { values := acc, descriptions := descs }
      match ← leaf body g with
      | some r => return some (g, r)
      | none => return none

private def groundingJson (g : Grounding) (universeInstantiation : String) (tacticCalls : Nat) :
    MetaM Json := do
  let mut assignments := #[]
  for index in List.range g.values.size do
    let value := g.values[index]!
    let valueType ← inferType value
    assignments := assignments.push <| Json.mkObj [
      ("index", toJson index),
      ("description", Json.str g.descriptions[index]!),
      ("value_expr_hash_u64", Json.str (toString (alphaHash value))),
      ("value_type_hash_u64", Json.str (toString (alphaHash valueType))),
      ("source_kind", Json.str (if g.descriptions[index]!.startsWith "instance" then
        "synthesized_instance" else "constructed_or_checked_witness"))]
  return Json.mkObj [
    ("assignment", Json.arr assignments),
    ("binder_count", toJson g.values.size),
    ("tactic_calls", toJson tacticCalls),
    ("universe_instantiation", Json.str universeInstantiation)
  ]

/-! ## Negative refutations -/

structure NegativeEvidence where
  refutation : ProofCheck
  grounding : Grounding
  refutationKind : String
  boundary : Option Int
  tacticCalls : Nat
  separator : Option ProofCheck := none
  separatorKind : String := ""
  witnesses : Array String := #[]
  witnessChecks : Array ProofCheck := #[]
  enumerationKind : String := ""
  /-- The checked `Not candidate` proof at universe level zero. -/
  proof : Expr

private def sourceConst (root : Root) : Expr :=
  mkConst root.name (root.levelParams.map fun _ => Level.zero)

private def universeTag (root : Root) : String :=
  if root.levelParams.isEmpty then "none" else "all_zero"

/-- `Not candidate` from an assignment satisfying the reference telescope, an
    exact proof of that reference at level zero, and a `False` builder from the
    applied reference and candidate proofs.  Wave 4 uses this form to certify a
    negative re-applied after a preserving chain; the single-hop wrapper below
    supplies the loaded environment constant. -/
private def refuteViaProof (root : Root) (cand sourceProof : Expr)
    (falseOf : Expr → Expr → MetaM Expr) : MetaM NegativeEvidence := do
  let ref0 := levelZeroInstantiate root.levelParams root.reference
  let cand0 := levelZeroInstantiate root.levelParams cand
  let branchBudget ← IO.mkRef groundingBranchBudget
  let tacticBudget ← IO.mkRef groundingTacticBudget
  let some (g, ()) ← groundTelescope ref0 0 (fun _ _ => none) #[] #[] branchBudget tacticBudget
      (fun _ _ => return some ())
    | throwRej "no_ground_assignment"
  let notCand := mkApp (mkConst ``Not) cand0
  let proof ← withLocalDecl `h .default cand0 fun h => do
    let sourceApp := mkAppN sourceProof g.values
    let candApp := mkAppN h g.values
    mkLambdaFVars #[h] (← falseOf sourceApp candApp)
  let refutation ← checkedProof "refutation" [] proof notCand
  return {
    refutation
    grounding := g
    refutationKind := "source_proof_contradiction"
    boundary := none
    tacticCalls := groundingTacticBudget - (← tacticBudget.get)
    proof
  }

/-- Historical single-hop entry point using the exact loaded theorem proof. -/
private def refuteViaSource (root : Root) (cand : Expr)
    (falseOf : Expr → Expr → MetaM Expr) : MetaM NegativeEvidence :=
  refuteViaProof root cand (sourceConst root) falseOf

private def n25RefuteViaProof (root : Root) (cand : Expr) (direction : String)
    (sourceProof : Expr) : MetaM NegativeEvidence :=
  refuteViaProof root cand sourceProof fun sourceApp candApp => do
    if direction == "eq_to_ne" then
      return mkApp candApp sourceApp
    else
      return mkApp sourceApp candApp

private def n25Refute (root : Root) (cand : Expr) (direction : String) : MetaM NegativeEvidence :=
  n25RefuteViaProof root cand direction (sourceConst root)

private def n32RefuteViaProof (root : Root) (cand sourceProof : Expr) : MetaM NegativeEvidence :=
  refuteViaProof root cand sourceProof fun sourceApp candApp => do
    let asymm ← mkAppM ``lt_asymm #[sourceApp]
    return mkApp asymm candApp

private def n32Refute (root : Root) (cand : Expr) : MetaM NegativeEvidence :=
  n32RefuteViaProof root cand (sourceConst root)

private def expectedN32AsymmetryUnavailable (msg : String) : Bool :=
  msg.contains "failed to synthesize" || msg.contains "type class instance problem"

private partial def groundedBinderDomainAt (ty : Expr) (target idx : Nat)
    (values : Array Expr) : Option Expr :=
  match ty with
  | .forallE _ d b _ =>
      if idx == target then some d
      else
        match values[idx]? with
        | some value => groundedBinderDomainAt (b.instantiate1 value) target (idx + 1) values
        | none => none
  | _ => none

private def copiedBoundaryConstraint (variableIndex boundIndex : Nat) :
    Nat → Array Expr → Option (Array (Expr × String)) := fun idx acc =>
  if idx == variableIndex && boundIndex < idx then
    (acc[boundIndex]?).map fun value => #[(value, s!"boundary:eq_binder_{boundIndex}")]
  else if idx == boundIndex && variableIndex < idx then
    (acc[variableIndex]?).map fun value => #[(value, s!"boundary:eq_binder_{variableIndex}")]
  else none

private def checkedGuardSeparator (root : Root) (guardIndex : Nat) (g : Grounding)
    (tacticBudget : IO.Ref Nat) : MetaM (ProofCheck × String) := do
  let ref0 := levelZeroInstantiate root.levelParams root.reference
  let some guard := groundedBinderDomainAt ref0 guardIndex 0 g.values
    | throwRej "boundary_guard_reconstruction_failed"
  let notGuard := mkApp (mkConst ``Not) guard
  let some (proof, label) ← proveClosed? notGuard tacticBudget
    | throwRej "boundary_does_not_refute_source_guard"
  return (← checkedProof "boundary_separator" [] proof notGuard, label)

private def n31Refute (root : Root) (cand : Expr) (site : Site) : MetaM NegativeEvidence := do
  let cand0 := levelZeroInstantiate root.levelParams cand
  let (guardIndex, m) ← discoverN31 root
  unless guardIndex == site.index && m.variableIndex == site.guardVariableIndex do
    throwRej "n31_site_replay_mismatch"
  let tacticBudget ← IO.mkRef groundingTacticBudget
  let boundaries : Array (Option Int) :=
    if m.boundVariableIndex.isSome then #[none] else m.boundaries.map some
  let mut result : Option (Grounding × Expr × String × Option Int) := none
  for boundary in boundaries do
    if result.isSome then break
    let single : Nat → Array Expr → Option (Array (Expr × String)) :=
      match boundary, m.boundVariableIndex with
      | some value, _ => fun i _ =>
          if i == site.guardVariableIndex then some #[literalValue m.varType value] else none
      | none, some boundIndex => copiedBoundaryConstraint site.guardVariableIndex boundIndex
      | none, none => fun _ _ => none
    let branchBudget ← IO.mkRef groundingBranchBudget
    let found ← groundTelescope cand0 0 single #[] #[] branchBudget tacticBudget
      fun target _ => do
        match ← proveClosed? (mkApp (mkConst ``Not) target) tacticBudget with
        | some (proof, label) => return some (proof, label)
        | none => return none
    if let some (g, (proof, label)) := found then
      result := some (g, proof, label, boundary)
  let some (g, targetRefutation, label, boundary) := result
    | throwRej "no_boundary_refutation"
  let (separator, separatorLabel) ← checkedGuardSeparator root guardIndex g tacticBudget
  let notCand := mkApp (mkConst ``Not) cand0
  let proof ← withLocalDecl `h .default cand0 fun h => do
    mkLambdaFVars #[h] (mkApp targetRefutation (mkAppN h g.values))
  let refutation ← checkedProof "refutation" [] proof notCand
  return {
    refutation
    grounding := g
    refutationKind := s!"boundary_counterexample:{label}"
    boundary
    tacticCalls := groundingTacticBudget - (← tacticBudget.get)
    separator := some separator
    separatorKind := s!"source_guard_false:{separatorLabel}"
    proof
  }

/-- Construct an explicit counterexample to a candidate by grounding its complete telescope and
    kernel-checking a proof of the negated grounded target. The exact source theorem is checked
    separately by the negative driver. -/
private def groundedCandidateRefute (root : Root) (cand : Expr) (kind : String) :
    MetaM NegativeEvidence := do
  let cand0 := levelZeroInstantiate root.levelParams cand
  let branchBudget ← IO.mkRef groundingBranchBudget
  let tacticBudget ← IO.mkRef groundingTacticBudget
  let found : Option (Grounding × (Expr × String)) ←
    groundTelescope cand0 0 (fun _ _ => none) #[] #[] branchBudget tacticBudget
    fun target _ => do
      match ← proveClosed? (mkApp (mkConst ``Not) target) tacticBudget with
      | some (proof, label) => return some (proof, label)
      | none => return none
  let some (g, (targetRefutation, label)) := found
    | throwRej "no_boundary_refutation"
  let notCand := mkApp (mkConst ``Not) cand0
  let proof ← withLocalDecl `h .default cand0 fun h => do
    mkLambdaFVars #[h] (mkApp targetRefutation (mkAppN h g.values))
  let refutation ← checkedProof "refutation" [] proof notCand
  return {
    refutation
    grounding := g
    refutationKind := s!"{kind}:{label}"
    boundary := none
    tacticCalls := groundingTacticBudget - (← tacticBudget.get)
    proof
  }

private def n26Refute (root : Root) (cand : Expr) (site : Site) : MetaM NegativeEvidence := do
  if site.kind == "finset_range_coverage_bound" then
    throwRej "n26_legacy_target_fallback_forbidden"
  let (guardIndex, m) ← discoverN26 root
  let some boundIndex := m.boundVariableIndex | throwRej "n26_missing_bound_index"
  unless guardIndex == site.index && m.variableIndex == site.guardVariableIndex &&
      site.boundVariableIndex == some boundIndex && m.schema == site.detail do
    throwRej "n26_site_replay_mismatch"
  let cand0 := levelZeroInstantiate root.levelParams cand
  let branchBudget ← IO.mkRef groundingBranchBudget
  let tacticBudget ← IO.mkRef groundingTacticBudget
  let constraint := copiedBoundaryConstraint m.variableIndex boundIndex
  let found ← groundTelescope cand0 0 constraint #[] #[] branchBudget tacticBudget
    fun target _ => do
      match ← proveClosed? (mkApp (mkConst ``Not) target) tacticBudget with
      | some (proof, label) => return some (proof, label)
      | none => return none
  let some (g, (targetRefutation, label)) := found
    | throwRej "n26_no_exact_boundary_refutation"
  let (separator, separatorLabel) ← checkedGuardSeparator root guardIndex g tacticBudget
  let notCand := mkApp (mkConst ``Not) cand0
  let proof ← withLocalDecl `h .default cand0 fun h => do
    mkLambdaFVars #[h] (mkApp targetRefutation (mkAppN h g.values))
  let refutation ← checkedProof "refutation" [] proof notCand
  return {
    refutation
    grounding := g
    refutationKind := s!"exact_index_boundary_counterexample:{label}"
    boundary := none
    tacticCalls := groundingTacticBudget - (← tacticBudget.get)
    separator := some separator
    separatorKind := s!"source_guard_false:{separatorLabel}"
    witnesses := #[s!"binder_{m.variableIndex}=binder_{boundIndex}"]
    proof
  }

structure N30Leaf where
  first : Expr
  second : Expr
  firstDescription : String
  secondDescription : String
  firstProof : Expr
  secondProof : Expr
  distinctProof : Expr
  enumerationKind : String

private def n30Refute (root : Root) (cand : Expr) : MetaM NegativeEvidence := do
  let cand0 := levelZeroInstantiate root.levelParams cand
  let branchBudget ← IO.mkRef groundingBranchBudget
  let tacticBudget ← IO.mkRef groundingTacticBudget
  let found : Option (Grounding × N30Leaf) ←
    groundTelescope cand0 0 (fun _ _ => none) #[] #[] branchBudget tacticBudget
    fun target _ => do
      let args := target.getAppArgs
      unless headName? target == some ``ExistsUnique && args.size == 2 do return none
      let domainType := args[0]!
      let predicate := args[1]!
      let some domain ← finiteEnumerableDomain? domainType tacticBudget | return none
      if domain.values.size < 2 then return none
      for i in List.range domain.values.size do
        for j in List.range domain.values.size do
          if i < j then
            let (a, ad) := domain.values[i]!
            let (b, bd) := domain.values[j]!
            let some (pa, _) ← proveClosed? (mkApp predicate a) tacticBudget | continue
            let some (pb, _) ← proveClosed? (mkApp predicate b) tacticBudget | continue
            let ne ← mkAppM ``Ne #[a, b]
            let some (pne, _) ← proveClosed? ne tacticBudget | continue
            return some (show N30Leaf from {
              first := a
              second := b
              firstDescription := ad
              secondDescription := bd
              firstProof := pa
              secondProof := pb
              distinctProof := pne
              enumerationKind := domain.kind
            })
      return none
  let some (g, leaf) := found | throwRej "n30_no_two_distinct_satisfying_witnesses"
  let notCand := mkApp (mkConst ``Not) cand0
  let proof ← withLocalDecl `h .default cand0 fun h => do
    let unique ← mkAppOptM ``ExistsUnique.unique
      #[none, none, mkAppN h g.values, leaf.first, leaf.second,
        leaf.firstProof, leaf.secondProof]
    mkLambdaFVars #[h] (mkApp leaf.distinctProof unique)
  let refutation ← checkedProof "refutation" [] proof notCand
  let firstCheck ← checkedProof "n30_first_witness" [] leaf.firstProof
    (← inferType leaf.firstProof)
  let secondCheck ← checkedProof "n30_second_witness" [] leaf.secondProof
    (← inferType leaf.secondProof)
  let distinctCheck ← checkedProof "n30_distinct_witnesses" [] leaf.distinctProof
    (← inferType leaf.distinctProof)
  return {
    refutation
    grounding := g
    refutationKind := "two_witness_uniqueness_contradiction"
    boundary := none
    tacticCalls := groundingTacticBudget - (← tacticBudget.get)
    separator := some distinctCheck
    separatorKind := "two_witnesses_distinct"
    witnesses := #[leaf.firstDescription, leaf.secondDescription]
    witnessChecks := #[firstCheck, secondCheck, distinctCheck]
    enumerationKind := leaf.enumerationKind
    proof
  }

structure N29Leaf where
  refutation : Expr
  label : String
  counterexamples : Array String
  checks : Array ProofCheck
  enumerationKind : String

private def n29Refute (root : Root) (cand : Expr) : MetaM NegativeEvidence := do
  let cand0 := levelZeroInstantiate root.levelParams cand
  let branchBudget ← IO.mkRef groundingBranchBudget
  let tacticBudget ← IO.mkRef groundingTacticBudget
  let found : Option (Grounding × N29Leaf) ←
    groundTelescope cand0 0 (fun _ _ => none) #[] #[] branchBudget tacticBudget
    fun target _ => do
      let args := target.getAppArgs
      unless headName? target == some ``Exists && args.size == 2 do return none
      let witnessType := args[0]!
      let witnessPredicate := args[1]!
      let some witnesses ← finiteEnumerableDomain? witnessType tacticBudget | return none
      if witnesses.values.isEmpty || witnesses.values.size > 8 then return none
      let mut descriptions : Array String := #[]
      let mut checks : Array ProofCheck := #[]
      let mut inputKind : Option String := none
      for (witness, witnessDesc) in witnesses.values do
        let forallInput ← whnf (mkApp witnessPredicate witness)
        let .forallE _ inputType relation _ := forallInput | return none
        let some inputs ← finiteEnumerableDomain? inputType tacticBudget | return none
        if inputs.values.isEmpty || inputs.values.size > 8 then return none
        match inputKind with
        | some kind => unless kind == inputs.kind do return none
        | none => inputKind := some inputs.kind
        let mut separated := false
        for (input, inputDesc) in inputs.values do
          if !separated then
            let proposition := relation.instantiate1 input
            let negated := mkApp (mkConst ``Not) proposition
            if let some (counterexample, _) ← proveClosed? negated tacticBudget then
              checks := checks.push (← checkedProof "n29_matrix_counterexample" []
                counterexample negated)
              descriptions := descriptions.push s!"{witnessDesc}->not@{inputDesc}"
              separated := true
        unless separated do return none
      let some (targetRefutation, label) ← proveClosed? (mkApp (mkConst ``Not) target) tacticBudget
        | return none
      let some completeInputKind := inputKind | return none
      return some (show N29Leaf from {
        refutation := targetRefutation
        label
        counterexamples := descriptions
        checks
        enumerationKind := s!"witness={witnesses.kind};input={completeInputKind};complete_matrix"
      })
  let some (g, leaf) := found | throwRej "n29_no_complete_finite_counterexample_matrix"
  let notCand := mkApp (mkConst ``Not) cand0
  let proof ← withLocalDecl `h .default cand0 fun h => do
    mkLambdaFVars #[h] (mkApp leaf.refutation (mkAppN h g.values))
  let refutation ← checkedProof "refutation" [] proof notCand
  return {
    refutation
    grounding := g
    refutationKind := s!"complete_finite_matrix:{leaf.label}"
    boundary := none
    tacticCalls := groundingTacticBudget - (← tacticBudget.get)
    separator := leaf.checks[0]?
    separatorKind := "each_uniform_witness_has_checked_counterexample"
    witnesses := leaf.counterexamples
    witnessChecks := leaf.checks
    enumerationKind := leaf.enumerationKind
    proof
  }

/-! ## Per-operation driver -/

structure OpOutcome where
  op : Op
  status : String
  reason : String
  site : Option Site
  evidence : Json
  candidate : Option Expr
  candidateGoal : Option String
  elapsedMs : Nat

private def sourceProofJson (root : Root) : Json :=
  Json.mkObj [
    ("kind", Json.str "loaded_environment_constant"),
    ("constant", Json.str root.name.toString),
    ("value_expr_hash_u64", Json.str (toString root.proofValueHash))
  ]

/-- Complete row-local negative certificate payload.  Keep this single
    serializer shared by direct negatives and Wave 4 so boundary witnesses,
    separators, finite-enumeration witnesses, and their individual proof
    checks cannot be dropped by an orbit-specific shortcut. -/
private def negativeEvidenceFields (root : Root) (ev : NegativeEvidence) :
    MetaM (List (String × Json)) := do
  let groundingRecord ← groundingJson ev.grounding (universeTag root) ev.tacticCalls
  return [
    ("kind", Json.str ev.refutationKind),
    ("check", ev.refutation.toJson),
    ("grounding", groundingRecord),
    ("boundary", match ev.boundary with | some b => toJson b | none => Json.null),
    ("separator", match ev.separator with
      | some check => Json.mkObj [
          ("kind", Json.str ev.separatorKind), ("check", check.toJson)]
      | none => Json.null),
    ("witnesses", Json.arr (ev.witnesses.map Json.str)),
    ("witness_checks", Json.arr (ev.witnessChecks.map ProofCheck.toJson)),
    ("enumeration", if ev.enumerationKind.isEmpty then Json.null
      else Json.str ev.enumerationKind)
  ]

private def negativeEvidenceJson (root : Root) (ev : NegativeEvidence) : MetaM Json :=
  return Json.mkObj (← negativeEvidenceFields root ev)

/-- Exact direct preserving witness used by single-hop, square, and Wave 4
    chain certification.  Lemma-rewrite sites bind their exact occurrence. -/
private def preservingIffProof (op : Op) (site : Site) (ref cand : Expr) : MetaM Expr := do
  match op with
  | .p15 => finalTargetIffProof ref cand fun p => mkAppM ``Iff.symm #[p]
  | .p18 => finalTargetIffProof ref cand fun p => mkEqSymm p
  | .p14 | .p24 => p14IffProof ref cand site.index
  | .p23 => p23IffProof ref cand site.index (Name.mkSimple site.detail)
  | .pne => finalTargetIffProof ref cand fun p => mkAppM ``Ne.symm #[p]
  | .pdrg => do
      let (proof, label) ← dropGuardIffProof ref cand site.index
      unless label == site.detail do throwRej "pdrg_guard_proof_label_mismatch"
      return proof
  | .p21Beta | .p21Zeta => defeqIffProof ref cand
  | .p32Assoc => lemmaRewriteIffProofAt ref cand ``add_assoc site.index
  | .p32Comm => lemmaRewriteIffProofAt ref cand ``add_comm site.index
  | .p35 => lemmaRewriteIffProofAt ref cand ``Set.mem_inter_iff site.index
  | .p16 => lemmaRewriteIffProofAt ref cand ``and_assoc site.index
  | .p28 => lemmaRewriteIffProofAt ref cand ``iff_def site.index
  | .pOrderComplement =>
      if site.detail == "not_le" then lemmaRewriteIffProofAt ref cand ``not_le site.index
      else if site.detail == "not_lt" then lemmaRewriteIffProofAt ref cand ``not_lt site.index
      else throwRej "p_order_complement_lemma_mismatch"
  | _ => throwRej "positive_dispatch_mismatch"

private def certifyPositive (root : Root) (op : Op) (applied : Applied) : MetaM Json := do
  let ref := root.reference
  let cand := applied.candidate
  let proof ← preservingIffProof op applied.site ref cand
  let goal := mkApp2 (mkConst ``Iff) ref cand
  let checkResult ← checkedProof "equivalence" root.levelParams proof goal
  return Json.mkObj [
    ("label", Json.bool true),
    ("equivalence_proof", Json.mkObj [
      ("goal", Json.str "Iff reference candidate"),
      ("guard_discharge", Json.str applied.site.detail),
      ("check", checkResult.toJson)
    ]),
    ("source_proof", sourceProofJson root),
    ("candidate_truth", Json.str "proved_equivalent_to_reference")
  ]

private def certifyNegative (root : Root) (op : Op) (applied : Applied) : MetaM Json := do
  let cand := applied.candidate
  let sourceCheck ← checkedProof "source" [] (sourceConst root)
    (levelZeroInstantiate root.levelParams root.reference)
  let ev ←
    match op with
    | .n25 => n25Refute root cand applied.site.detail
    | .n32 =>
        tryCatchRuntimeEx
          (do
            if applied.site.detail == "strict_lt" then
              let exactAsymm : Option NegativeEvidence ← tryCatchRuntimeEx
                (do return some (← n32Refute root cand)) fun ex => do
                  let msg ← exceptionText ex
                  if isDeterministicHeartbeatTimeout msg then return none
                  if ex.isInterrupt then throw ex
                  if expectedN32AsymmetryUnavailable msg then return none else throw ex
              match exactAsymm with
              | some evidence => pure evidence
              | none => groundedCandidateRefute root cand "role_order_boundary_counterexample"
            else groundedCandidateRefute root cand "role_order_boundary_counterexample")
          fun ex => do
            let msg ← exceptionText ex
            if isDeterministicHeartbeatTimeout msg then
              throwRej "n32_certificate_search_heartbeat_limit"
            if ex.isInterrupt then throw ex
            throw ex
    | .n31 => n31Refute root cand applied.site
    | .n26 => n26Refute root cand applied.site
    | .n30 =>
        failClosedOnHeartbeat "n30_certificate_search_heartbeat_limit" (n30Refute root cand)
    | .n29 =>
        failClosedOnHeartbeat "n29_certificate_search_heartbeat_limit" (n29Refute root cand)
    | _ => throwRej "negative_dispatch_mismatch"
  let groundingRecord ← groundingJson ev.grounding (universeTag root) ev.tacticCalls
  return Json.mkObj [
    ("label", Json.bool false),
    ("source_proof", sourceProofJson root),
    ("source_proof_check", sourceCheck.toJson),
    ("refutation", Json.mkObj [
      ("goal", Json.str "Not candidate"),
      ("kind", Json.str ev.refutationKind),
      ("check", ev.refutation.toJson),
      ("grounding", groundingRecord),
      ("boundary", match ev.boundary with | some b => toJson b | none => Json.null),
      ("separator", match ev.separator with
        | some check => Json.mkObj [
            ("kind", Json.str ev.separatorKind), ("check", check.toJson)]
        | none => Json.null),
      ("witnesses", Json.arr (ev.witnesses.map Json.str)),
      ("witness_checks", Json.arr (ev.witnessChecks.map ProofCheck.toJson)),
      ("enumeration", if ev.enumerationKind.isEmpty then Json.null
        else Json.str ev.enumerationKind)
    ]),
    ("candidate_truth", Json.str "refuted")
  ]

private def runOpCore (root : Root) (op : Op) : MetaM (Site × Json × Expr × String) := do
  let applied ← applyOp root op
  let cand ← checkedClosedProp false "candidate" applied.candidate
  if Expr.equal root.reference cand || alphaHash root.reference == alphaHash cand then
    throwRej "self_pair"
  let replayed ← replayOp root op applied.site
  unless Expr.equal replayed cand do throwRej "certificate_replay_mismatch"
  let candidateGoal ←
    match ← prerender cand with
    | .ok text => pure text
    | .error msg => throwRej s!"candidate_unrenderable:{msg}"
  let evidence ←
    if op.positive then certifyPositive root op applied else certifyNegative root op applied
  return (applied.site, evidence, cand, candidateGoal)

private def classify (msg : String) : String × String :=
  if msg.startsWith naPrefix then ("not_applicable", (msg.toRawSubstring.drop naPrefix.length).toString)
  else if msg.startsWith rejPrefix then ("rejected", (msg.toRawSubstring.drop rejPrefix.length).toString)
  else ("error", (msg.toRawSubstring.take 400).toString)

def runOp (root : Root) (op : Op) : MetaM OpOutcome := do
  let t0 ← IO.monoMsNow
  tryCatchRuntimeEx
    (do
      let (site, evidence, cand, goal) ← withHeartbeatBudget opHeartbeatBudgetK (runOpCore root op)
      let t1 ← IO.monoMsNow
      return {
        op, status := "retained", reason := "", site := some site, evidence
        candidate := some cand, candidateGoal := some goal, elapsedMs := t1 - t0
      })
    fun ex => do
      if ex.isInterrupt then throw ex
      let t1 ← IO.monoMsNow
      let (status, reason) := classify (← exceptionText ex)
      return {
        op, status, reason, site := none, evidence := Json.null
        candidate := none, candidateGoal := none, elapsedMs := t1 - t0
      }

private def outcomeJson (o : OpOutcome) : Json :=
  Json.mkObj [
    ("operation_id", Json.str o.op.id),
    ("status", Json.str o.status),
    ("reason", Json.str o.reason),
    ("label", if o.status == "retained" then Json.bool o.op.positive else Json.null),
    ("site", match o.site with | some s => siteJson s | none => Json.null),
    ("evidence", o.evidence),
    ("candidate_alpha_hash", match o.candidate with
      | some c => Json.str (toString (alphaHash c))
      | none => Json.null),
    ("candidate_goal", match o.candidateGoal with | some g => Json.str g | none => Json.null),
    ("elapsed_ms", toJson o.elapsedMs)
  ]

private def emitJson (payload : Json) : MetaM Unit :=
  IO.println s!"{evidenceMarker}{payload.compress}"

/-- Process one root for the operations enabled in `mask` and emit one
    evidence line. -/
def processRoot (name : Name) (mask : Nat) : MetaM Unit := do
  let t0 ← IO.monoMsNow
  let base := [("schema_version", toJson 1), ("kind", Json.str "root"),
    ("engine_semantic_version", Json.str engineSemanticVersion),
    ("engine_operation_set_version", toJson engineOperationSetVersion),
    ("root", Json.str name.toString)]
  let root : Except String Root ← tryCatchRuntimeEx (do return Except.ok (← loadRoot name))
    fun ex => do
      if ex.isInterrupt then throw ex
      return Except.error (← exceptionText ex)
  match root with
  | .error msg =>
      let (status, reason) := classify msg
      emitJson <| Json.mkObj (base ++ [
        ("root_status", Json.str status), ("reason", Json.str reason),
        ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])
  | .ok root =>
      let reports ← binderReports root.reference #[]
      match ← prerender root.reference with
      | .error msg =>
          emitJson <| Json.mkObj (base ++ [
            ("root_status", Json.str "not_applicable"),
            ("reason", Json.str s!"reference_unrenderable:{msg}"),
            ("module", Json.str root.module.toString),
            ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])
      | .ok referenceGoal =>
          let mut outcomes : Array Json := #[]
          for op in Op.all do
            if mask.testBit op.bit then
              outcomes := outcomes.push (outcomeJson (← runOp root op))
          emitJson <| Json.mkObj (base ++ [
            ("root_status", Json.str "ok"),
            ("module", Json.str root.module.toString),
            ("level_params", Json.arr (root.levelParams.toArray.map fun n => Json.str n.toString)),
            ("reference_alpha_hash", Json.str (toString (alphaHash root.reference))),
            ("reference_goal", Json.str referenceGoal),
            ("source_proof_value_hash_u64", Json.str (toString root.proofValueHash)),
            ("binders", binderReportsJson reports),
            ("terminals", Json.arr outcomes),
            ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])

/-- Parse a dotted theorem name sent as a string literal.  Guillemets around a
    component are stripped; components never contain dots. -/
def parseName (text : String) : Name :=
  (text.splitOn ".").foldl
    (fun acc component =>
      let trimmed :=
        if component.startsWith "«" && component.endsWith "»" then
          (component.drop 1).dropRight 1 |>.toString
        else component
      Name.mkStr acc trimmed)
    Name.anonymous

def processRoots (names : Array String) (masks : Array Nat) : MetaM Unit := do
  for name in names, mask in masks do
    processRoot (parseName name) mask

/-! ## Rebuild for the frozen render route -/

structure RebuiltPair where
  reference : Expr
  candidate : Expr
  deriving Inhabited

def rebuildPair (name : Name) (opId : String) : MetaM RebuiltPair := do
  let some op := Op.ofId? opId | throwError "rebuild: unknown operation {opId}"
  let root ← loadRoot name
  let applied ← applyOp root op
  let candidate ← checkedClosedProp false "candidate" applied.candidate
  return { reference := root.reference, candidate }

def rebuildPairs (names : Array String) (opIds : Array String) : MetaM (Array RebuiltPair) := do
  let mut result := #[]
  for name in names, opId in opIds do
    result := result.push (← rebuildPair (parseName name) opId)
  return result

def emitRebuildReport (pairs : Array RebuiltPair) : MetaM Unit := do
  let entries := pairs.mapIdx fun i p =>
    Json.mkObj [
      ("index", toJson i),
      ("reference_alpha_hash", Json.str (toString (alphaHash p.reference))),
      ("candidate_alpha_hash", Json.str (toString (alphaHash p.candidate)))
    ]
  emitJson <| Json.mkObj [
    ("schema_version", toJson 1), ("kind", Json.str "rebuild"),
    ("engine_semantic_version", Json.str engineSemanticVersion),
    ("pairs", Json.arr entries)]

/-! ## Certificate-closure squares around certified negatives -/

def squareOperationId : String := "SQUARE_N25_SYMMETRY_V1"

/-- A square operation: a certified negative mechanism `neg` closed by a preserving
    transform.  `transforms` is tried in order and the first applicable one wins; an
    empty list means the direction-dependent relation symmetry (P18 for `=`, `P_NE`
    for `≠`). -/
structure SquareOp where
  id : String
  /-- Identifier of the negative mechanism closed by the square. -/
  negId : String
  /-- Engine operation implementing the mechanism; `none` for the whole-claim negation
      (catalog N19), which negates the closed proposition itself. -/
  neg : Option Op
  transforms : Array Op
  deriving Inhabited, Repr

def n19OperationId : String := "N19_WHOLE_CLAIM_NEGATION_V1"

def wave4PreservingOps : Array Op := #[.p14, .p15, .p18, .pne, .pdrg,
  .p21Zeta, .p23, .p32Assoc, .p32Comm, .p35, .p24, .p16, .p28, .pOrderComplement]

def Op.wave4Mechanism : Op → String
  | .p14 => "P14" | .p15 => "P15" | .p18 => "P18" | .pne => "PNE"
  | .pdrg => "PDRG" | .p21Zeta => "P21Z" | .p23 => "P23"
  | .p32Assoc => "P32A" | .p32Comm => "P32C" | .p35 => "P35"
  | .p24 => "P24" | .p16 => "P16" | .p28 => "P28" | .pOrderComplement => "POC"
  | op => op.id

def Op.wave4Superclass : Op → String
  | .p14 => "binder_permutation" | .p24 => "prop_binder_permutation"
  | .p15 => "logical_symmetry" | .p18 | .pne => "relation_symmetry"
  | .pdrg => "guard_redundancy" | .p21Zeta => "definitional_reduction"
  | .p23 => "proposition_packaging" | .p32Assoc | .p32Comm => "ac_local_rewrite"
  | .p35 => "membership_schema" | .p16 => "connective_association"
  | .p28 => "iff_decomposition" | .pOrderComplement => "order_duality"
  | op => op.id

def Op.wave4InverseToken : Op → String
  | .p14 => "adjacent_data_binder_swap" | .p24 => "adjacent_prop_binder_swap"
  | .p15 => "iff_side_swap" | .p18 | .pne => "relation_argument_swap"
  | .pdrg => "redundant_guard_drop_add" | .p21Zeta => "zeta_intro_reduce"
  | .p23 => "prop_pair_curry_uncurry" | .p32Assoc => "add_association_direction"
  | .p32Comm => "add_commutative_swap" | .p35 => "set_inter_membership_schema"
  | .p16 => "connective_association_direction" | .p28 => "iff_implication_pair"
  | .pOrderComplement => "order_complement_direction"
  | op => op.id

def squareOps : Array SquareOp := #[
  { id := squareOperationId, negId := (Op.n25).id, neg := some .n25, transforms := #[] },
  { id := "SQUARE_N25_BINDER_V1", negId := (Op.n25).id, neg := some .n25,
    transforms := #[.p14, .p23] },
  { id := "SQUARE_N32_BINDER_V1", negId := (Op.n32).id, neg := some .n32,
    transforms := #[.p14, .p23] },
  { id := "SQUARE_N19_CURRICULUM_V1", negId := n19OperationId, neg := none,
    transforms := #[.p14, .p23, .p18, .pne, .p15] },
  { id := "SQUARE_WAVE2_N26_V1", negId := (Op.n26).id, neg := some .n26,
    transforms := #[.p15, .p14, .p23, .p21Zeta, .p32Assoc, .p32Comm, .p35] },
  { id := "SQUARE_WAVE2_N32_V1", negId := (Op.n32).id, neg := some .n32,
    transforms := #[.p14, .p23, .p21Zeta, .p32Assoc, .p32Comm, .p35] },
  { id := "SQUARE_WAVE2_N25_V1", negId := (Op.n25).id, neg := some .n25,
    transforms := #[.p21Zeta, .p32Assoc, .p32Comm, .p35, .p14, .p23] },
  { id := "SQUARE_WAVE2_N31_V1", negId := (Op.n31).id, neg := some .n31,
    transforms := #[.p18, .pne, .p15, .p14, .p23, .p21Zeta, .p32Assoc, .p32Comm, .p35] },
  { id := "ORBIT_WAVE4_N31_V1", negId := (Op.n31).id, neg := some .n31,
    transforms := wave4PreservingOps },
  { id := "ORBIT_WAVE4_N26_V1", negId := (Op.n26).id, neg := some .n26,
    transforms := wave4PreservingOps },
  { id := "ORBIT_WAVE4_N32_V1", negId := (Op.n32).id, neg := some .n32,
    transforms := wave4PreservingOps },
  { id := "ORBIT_WAVE4_N30_V1", negId := (Op.n30).id, neg := some .n30,
    transforms := wave4PreservingOps },
  { id := "ORBIT_WAVE4_N29_V1", negId := (Op.n29).id, neg := some .n29,
    transforms := wave4PreservingOps },
  { id := "ORBIT_WAVE4_N25_V1", negId := (Op.n25).id, neg := some .n25,
    transforms := wave4PreservingOps }]

def squareOp? (id : String) : Option SquareOp := squareOps.find? (·.id == id)

/-- Universe parameters instantiated at level zero for kernel checks (same body
    as the private helper above; kept public for the square section). -/
def squareLevelZero (params : List Name) (e : Expr) : Expr :=
  e.instantiateLevelParams params (params.map fun _ => Level.zero)

structure SquareBuild where
  root : Root
  op : SquareOp
  direction : String
  tP : Op
  tC : Op
  siteP : Site
  siteC : Site
  p : Expr
  c : Expr
  pPrime : Expr
  cPrime : Expr
  deriving Inhabited

private def symmetryFor (direction : String) : MetaM (Op × Op) := do
  if direction == "eq_to_ne" then return (.p18, .pne)
  else if direction == "ne_to_eq" then return (.pne, .p18)
  else throwNA s!"square_direction_unsupported:{direction}"

private def hasDuplicate (values : List UInt64) : Bool :=
  match values with
  | [] => false
  | x :: rest => rest.contains x || hasDuplicate rest

/-- First transform in `candidates` that applies to `root`; `not_applicable` failures
    fall through, every other failure propagates. -/
private def firstApplicable (root : Root) (candidates : Array Op) : MetaM (Op × Applied) := do
  for op in candidates do
    let attempt : Except String Applied ←
      tryCatchRuntimeEx (do return .ok (← applyOp root op)) fun ex => do
        if ex.isInterrupt then throw ex
        let msg ← exceptionText ex
        if (classify msg).1 == "not_applicable" then return .error msg else throw ex
    match attempt with
    | .ok applied => return (op, applied)
    | .error _ => pure ()
  throwNA "square_no_applicable_transform"

/-- Transform both endpoints with one typed transform `T`. For the symmetry
    operation `T` depends on the relation direction; for binder operations the first
    applicable transform is chosen on `P` and replayed at the same site on `C`. -/
private def closeSquare (root rootC : Root) (sop : SquareOp) (direction : String) :
    MetaM (Op × Op × Site × Site × Expr × Expr) := do
  if sop.neg.isNone then
    -- whole-claim negation: T acts under the outer negation, T(¬P) := ¬T(P)
    let (t, ap) ← firstApplicable root sop.transforms
    return (t, t, ap.site, ap.site, ap.candidate, mkApp (mkConst ``Not) ap.candidate)
  else if sop.transforms.isEmpty then
    let (tP, tC) ← symmetryFor direction
    let ap ← applyOp root tP
    let ac ← applyOp rootC tC
    return (tP, tC, ap.site, ac.site, ap.candidate, ac.candidate)
  else
    let (t, ap) ← firstApplicable root sop.transforms
    let cPrime ← replayOp rootC t ap.site
    return (t, t, ap.site, ap.site, ap.candidate, cPrime)

/-- The certified negative of `reference` for `sop`: an engine operation, or the
    whole-claim negation `Not reference` (catalog N19). -/
private def squareNegate (root : Root) (sop : SquareOp) : MetaM (Expr × String) := do
  match sop.neg with
  | some op =>
      let n ← applyOp root op
      return (n.candidate, n.site.detail)
  | none =>
      unless ← isProp root.reference do throwNA "n19_reference_not_prop"
      return (mkApp (mkConst ``Not) root.reference, "whole_claim")

/-- Rebuild the certified negative pair `(P, C)` for `sop.neg` and close the square with
    one preserving transform on both endpoints, requiring the exact typed diamond
    `T(N(P)) = N(T(P))`. -/
def buildSquare (root : Root) (sop : SquareOp) : MetaM SquareBuild := do
  let (negated, direction) ← squareNegate root sop
  let c ← checkedClosedProp false "candidate" negated
  let rootC : Root := { root with reference := c }
  let (tP, tC, siteP, siteC, pPrimeRaw, cPrimeRaw) ← closeSquare root rootC sop direction
  let pPrime ← checkedClosedProp false "p_prime" pPrimeRaw
  let cPrime ← checkedClosedProp false "c_prime" cPrimeRaw
  let rootPPrime : Root := { root with reference := pPrime }
  let (nOfT, nOfTDirection) ← squareNegate rootPPrime sop
  unless Expr.equal nOfT cPrime do throwRej "square_diamond_expr_mismatch"
  unless nOfTDirection == direction do throwRej "square_diamond_direction_mismatch"
  if hasDuplicate [alphaHash root.reference, alphaHash c, alphaHash pPrime, alphaHash cPrime] then
    throwRej "square_self_pair"
  return { root, op := sop, direction, tP, tC, siteP, siteC,
           p := root.reference, c, pPrime, cPrime }

private def iffExpr (a b : Expr) : Expr := mkApp2 (mkConst ``Iff) a b

private def squareIffProof (op : Op) (site : Site) (ref cand : Expr) : MetaM Expr :=
  preservingIffProof op site ref cand

/-- `Not (Not P)` from the loaded proof of `P`; the N19 refutation needs no grounding. -/
private def n19Refute (root : Root) (cand : Expr) : MetaM NegativeEvidence := do
  let cand0 := levelZeroInstantiate root.levelParams cand
  let notCand := mkApp (mkConst ``Not) cand0
  let proof ← withLocalDecl `h .default cand0 fun h => do
    mkLambdaFVars #[h] (mkApp h (sourceConst root))
  let refutation ← checkedProof "refutation" [] proof notCand
  return {
    refutation
    grounding := { values := #[], descriptions := #[] }
    refutationKind := "source_proof_double_negation"
    boundary := none
    tacticCalls := 0
    proof
  }

private def refuteSquareNegative (root : Root) (sop : SquareOp) (c : Expr)
    (direction : String) : MetaM NegativeEvidence :=
  match sop.neg with
  | some .n25 => n25Refute root c direction
  | some .n32 =>
      tryCatchRuntimeEx
        (do
          if direction == "strict_lt" then
            let exactAsymm : Option NegativeEvidence ← tryCatchRuntimeEx
              (do return some (← n32Refute root c)) fun ex => do
                let msg ← exceptionText ex
                if isDeterministicHeartbeatTimeout msg then return none
                if ex.isInterrupt then throw ex
                if expectedN32AsymmetryUnavailable msg then return none else throw ex
            match exactAsymm with
            | some evidence => pure evidence
            | none => groundedCandidateRefute root c "role_order_boundary_counterexample"
          else groundedCandidateRefute root c "role_order_boundary_counterexample")
        fun ex => do
          let msg ← exceptionText ex
          if isDeterministicHeartbeatTimeout msg then
            throwRej "n32_certificate_search_heartbeat_limit"
          if ex.isInterrupt then throw ex
          throw ex
  | some .n26 => do
      let applied ← applyOp root .n26
      n26Refute root c applied.site
  | some .n31 => do
      let applied ← applyOp root .n31
      n31Refute root c applied.site
  | some .n30 =>
      failClosedOnHeartbeat "n30_certificate_search_heartbeat_limit" (n30Refute root c)
  | some .n29 =>
      failClosedOnHeartbeat "n29_certificate_search_heartbeat_limit" (n29Refute root c)
  | none => n19Refute root c
  | _ => throwRej "square_negative_dispatch"

private def squareRefute (sq : SquareBuild) : MetaM NegativeEvidence :=
  refuteSquareNegative sq.root sq.op sq.c sq.direction

/-- Certify a negative that has been freshly re-applied to the preserved
    reference endpoint.  N25/N32 consume the transported proof of that exact
    endpoint rather than pretending the original theorem constant proves it;
    the other families construct their checked counterexample directly. -/
private def refuteWave4ReappliedNegative (root : Root) (sop : SquareOp)
    (applied : Applied) (sourceProof : Expr) : MetaM NegativeEvidence :=
  match sop.neg with
  | some .n25 => n25RefuteViaProof root applied.candidate applied.site.detail sourceProof
  | some .n32 =>
      tryCatchRuntimeEx
        (do
          if applied.site.detail == "strict_lt" then
            let exactAsymm : Option NegativeEvidence ← tryCatchRuntimeEx
              (do return some (← n32RefuteViaProof root applied.candidate sourceProof)) fun ex => do
                let msg ← exceptionText ex
                if isDeterministicHeartbeatTimeout msg then return none
                if ex.isInterrupt then throw ex
                if expectedN32AsymmetryUnavailable msg then return none else throw ex
            match exactAsymm with
            | some evidence => pure evidence
            | none =>
                groundedCandidateRefute root applied.candidate "role_order_boundary_counterexample"
          else
            groundedCandidateRefute root applied.candidate "role_order_boundary_counterexample")
        fun ex => do
          let msg ← exceptionText ex
          if isDeterministicHeartbeatTimeout msg then
            throwRej "n32_certificate_search_heartbeat_limit"
          if ex.isInterrupt then throw ex
          throw ex
  | some .n31 => n31Refute root applied.candidate applied.site
  | some .n26 => n26Refute root applied.candidate applied.site
  | some .n30 =>
      failClosedOnHeartbeat "n30_certificate_search_heartbeat_limit"
        (n30Refute root applied.candidate)
  | some .n29 =>
      failClosedOnHeartbeat "n29_certificate_search_heartbeat_limit"
        (n29Refute root applied.candidate)
  | none => throwRej "wave4_n19_forbidden"
  | _ => throwRej "wave4_negative_replay_dispatch"

/-- Direct Meta- and kernel-checked evidence for the four square rows. -/
def squareEvidence (sq : SquareBuild) : MetaM Json := do
  let params := sq.root.levelParams
  let iffPP' ← squareIffProof sq.tP sq.siteP sq.p sq.pPrime
  let checkPP' ← checkedProof "p_iff_p_prime" params iffPP' (iffExpr sq.p sq.pPrime)
  let iffP'P := mkApp3 (mkConst ``Iff.symm) sq.p sq.pPrime iffPP'
  let checkP'P ← checkedProof "p_prime_iff_p" params iffP'P (iffExpr sq.pPrime sq.p)
  let iffCC' ←
    if sq.op.neg.isSome then squareIffProof sq.tC sq.siteC sq.c sq.cPrime
    else pure (mkApp3 (mkConst ``not_congr) sq.p sq.pPrime iffPP')
  let checkCC' ← checkedProof "c_iff_c_prime" params iffCC' (iffExpr sq.c sq.cPrime)
  let p0 := squareLevelZero params sq.p
  let c0 := squareLevelZero params sq.c
  let p'0 := squareLevelZero params sq.pPrime
  let c'0 := squareLevelZero params sq.cPrime
  let iffPP'0 := squareLevelZero params iffPP'
  let iffCC'0 := squareLevelZero params iffCC'
  let source0 := sourceConst sq.root
  let sourceCheck ← checkedProof "source" [] source0 p0
  let neg ← squareRefute sq
  let groundingRecord ← groundingJson neg.grounding (universeTag sq.root) neg.tacticCalls
  let notC0 := neg.proof
  let proofP'0 := mkApp4 (mkConst ``Iff.mp) p0 p'0 iffPP'0 source0
  let checkProofP' ← checkedProof "p_prime_transported_proof" [] proofP'0 p'0
  let notC'0 ← withLocalDecl `h .default c'0 fun h => do
    mkLambdaFVars #[h] (mkApp notC0 (mkApp4 (mkConst ``Iff.mpr) c0 c'0 iffCC'0 h))
  let checkNotC' ← checkedProof "c_prime_refutation" [] notC'0 (mkApp (mkConst ``Not) c'0)
  let notIffCP ← withLocalDecl `h .default (iffExpr c0 p0) fun h => do
    mkLambdaFVars #[h] (mkApp notC0 (mkApp4 (mkConst ``Iff.mpr) c0 p0 h source0))
  let checkNotIffCP ← checkedProof "not_iff_c_p" [] notIffCP
    (mkApp (mkConst ``Not) (iffExpr c0 p0))
  let notIffP'C' ← withLocalDecl `h .default (iffExpr p'0 c'0) fun h => do
    mkLambdaFVars #[h] (mkApp notC'0 (mkApp4 (mkConst ``Iff.mp) p'0 c'0 h proofP'0))
  let checkNotIffP'C' ← checkedProof "not_iff_p_prime_c_prime" [] notIffP'C'
    (mkApp (mkConst ``Not) (iffExpr p'0 c'0))
  return Json.mkObj [
    ("operation_id", Json.str sq.op.id),
    ("negative_operation", Json.str sq.op.negId),
    ("direction", Json.str sq.direction),
    ("t_p", Json.str sq.tP.id),
    ("t_c", Json.str sq.tC.id),
    ("site_p", siteJson sq.siteP),
    ("site_c", siteJson sq.siteC),
    ("diamond", Json.mkObj [
      ("expr_equal", Json.bool true),
      ("direction_equal", Json.bool true)
    ]),
    ("p_iff_p_prime", checkPP'.toJson),
    ("p_prime_iff_p", checkP'P.toJson),
    ("c_iff_c_prime", checkCC'.toJson),
    ("source_proof", sourceProofJson sq.root),
    ("source_proof_check", sourceCheck.toJson),
    ("c_refutation", Json.mkObj [
      ("kind", Json.str neg.refutationKind),
      ("check", neg.refutation.toJson),
      ("grounding", groundingRecord)
    ]),
    ("p_prime_transported_proof", checkProofP'.toJson),
    ("c_prime_refutation", checkNotC'.toJson),
    ("not_iff_c_p", checkNotIffCP.toJson),
    ("not_iff_p_prime_c_prime", checkNotIffP'C'.toJson),
    ("universe_instantiation", Json.str (universeTag sq.root))
  ]

/-! ## Wave 4 all-site preserving orbits -/

/-- Proof-free description of one exact paired preserving hop.  It retains the
    live endpoint Exprs so a later selected-certificate request can replay the
    sites exactly, but deliberately contains no proof term or `ProofCheck`. -/
structure Wave4DescriptorHop where
  pOp : Op
  cOp : Op
  pSite : Site
  cSite : Site
  pInput : Expr
  cInput : Expr
  pOutput : Expr
  cOutput : Expr

/-- Cheap first-phase orbit descriptor.  Building this value performs typed
    transformation/site replay and cycle checks only.  Negative refutations,
    preserving equivalence proofs, frozen rendering, and kernel checks belong
    exclusively to the selected second phase. -/
structure Wave4Descriptor where
  root : Root
  op : SquareOp
  direction : String
  negativeSite : Site
  p : Expr
  c : Expr
  pPrime : Expr
  cPrime : Expr
  hops : Array Wave4DescriptorHop

structure Wave4Hop where
  pOp : Op
  cOp : Op
  pSite : Site
  cSite : Site
  pInput : Expr
  cInput : Expr
  pOutput : Expr
  cOutput : Expr
  pIffProof : Expr
  cIffProof : Expr
  pCheck : ProofCheck
  cCheck : ProofCheck

structure Wave4Build where
  root : Root
  op : SquareOp
  direction : String
  negativeSite : Site
  p : Expr
  c : Expr
  pPrime : Expr
  cPrime : Expr
  hops : Array Wave4Hop
  pCompositeProof : Expr
  cCompositeProof : Expr
  pCompositeCheck : ProofCheck
  cCompositeCheck : ProofCheck
  baseNegative : NegativeEvidence

private def wave4Pairable (pOp cOp : Op) (pSite cSite : Site) : Bool :=
  pOp.wave4Superclass == cOp.wave4Superclass &&
    (pOp == cOp || pOp.wave4Superclass == "relation_symmetry") &&
    pSite.index == cSite.index && pSite.path == cSite.path

private def wave4BinderSite (site : Site) : Bool := site.path[0]? == some 3

/-- Multi-hop support is intentionally conservative: without an explicit site
    transport proof, only disjoint outer-binder coordinates are composed.  A
    later binder rewrite must be strictly outside every earlier two-binder
    footprint, so its root coordinate is unchanged.  Overlapping sites are
    never represented as transported. -/
private def wave4Disjoint (prior : Array Site) (site : Site) : Bool :=
  if prior.isEmpty then true
  else wave4BinderSite site && prior.all fun old =>
    wave4BinderSite old && site.index + 1 < old.index

private def wave4DescriptorAllowedNext (hops : Array Wave4DescriptorHop) (pOp cOp : Op)
    (pSite cSite : Site) : Bool :=
  let mechanisms := hops.map fun hop => hop.pOp.wave4Mechanism
  let superclasses := hops.map fun hop => hop.pOp.wave4Superclass
  let inverses := hops.map fun hop => hop.pOp.wave4InverseToken
  !mechanisms.contains pOp.wave4Mechanism &&
    !superclasses.contains pOp.wave4Superclass &&
    !inverses.contains pOp.wave4InverseToken &&
    pOp.wave4Superclass == cOp.wave4Superclass &&
    wave4Disjoint (hops.map (fun hop => hop.pSite)) pSite &&
    wave4Disjoint (hops.map (fun hop => hop.cSite)) cSite

private def wave4AllowedNext (hops : Array Wave4Hop) (pOp cOp : Op)
    (pSite cSite : Site) : Bool :=
  let mechanisms := hops.map fun hop => hop.pOp.wave4Mechanism
  let superclasses := hops.map fun hop => hop.pOp.wave4Superclass
  let inverses := hops.map fun hop => hop.pOp.wave4InverseToken
  !mechanisms.contains pOp.wave4Mechanism &&
    !superclasses.contains pOp.wave4Superclass &&
    !inverses.contains pOp.wave4InverseToken &&
    pOp.wave4Superclass == cOp.wave4Superclass &&
    wave4Disjoint (hops.map (fun hop => hop.pSite)) pSite &&
    wave4Disjoint (hops.map (fun hop => hop.cSite)) cSite

private def wave4Applications (root : Root) (ops : Array Op) :
    MetaM (Array (Op × Applied)) := do
  let mut result := #[]
  for op in ops do
    for applied in ← enumerateWave4Op root op do
      result := result.push (op, applied)
  return result

private def wave4DescribeOne (state : Wave4Descriptor) (pOp cOp : Op)
    (pApplied cApplied : Applied) : MetaM Wave4Descriptor := do
  let pNext ← checkedClosedProp false "wave4_descriptor_p_next" pApplied.candidate
  let cNext ← checkedClosedProp false "wave4_descriptor_c_next" cApplied.candidate
  if Expr.equal state.pPrime pNext || Expr.equal state.cPrime cNext then
    throwRej "wave4_hop_self_pair"
  let pSeen := #[state.p] ++ state.hops.map (fun hop => hop.pOutput)
  let cSeen := #[state.c] ++ state.hops.map (fun hop => hop.cOutput)
  if pSeen.any (fun e => alphaHash e == alphaHash pNext) ||
      cSeen.any (fun e => alphaHash e == alphaHash cNext) then
    throwRej "wave4_expression_cycle"
  let hop : Wave4DescriptorHop := {
    pOp := pOp, cOp := cOp, pSite := pApplied.site, cSite := cApplied.site,
    pInput := state.pPrime, cInput := state.cPrime, pOutput := pNext, cOutput := cNext
  }
  return { state with
    pPrime := pNext, cPrime := cNext, hops := state.hops.push hop }

/-- Enumerate structural chains without constructing any preserving or negative
    proof.  Cartesian pairing is limited to cheap operation/site descriptors;
    only the at-most-five selected chains cross the certificate boundary. -/
private partial def wave4DescribeExpand (state : Wave4Descriptor) (maximumDepth : Nat) :
    MetaM (Array Wave4Descriptor) := do
  if state.hops.size >= maximumDepth then return #[]
  let rootP : Root := { state.root with reference := state.pPrime }
  let rootC : Root := { state.root with reference := state.cPrime }
  let pApplications ← wave4Applications rootP state.op.transforms
  let cApplications ← wave4Applications rootC state.op.transforms
  let mut result : Array Wave4Descriptor := #[]
  for (pOp, pApplied) in pApplications do
    for (cOp, cApplied) in cApplications do
      if wave4Pairable pOp cOp pApplied.site cApplied.site &&
          wave4DescriptorAllowedNext state.hops pOp cOp pApplied.site cApplied.site then
        let next ← wave4DescribeOne state pOp cOp pApplied cApplied
        result := result.push next
        result := result ++ (← wave4DescribeExpand next maximumDepth)
  return result

/-- Complete deterministic proof-free enumeration for one Wave 4 root. -/
def buildWave4Descriptors (root : Root) (sop : SquareOp) (maximumDepth : Nat) :
    MetaM (Array Wave4Descriptor) := do
  if maximumDepth == 0 || maximumDepth > 3 then
    throwRej "wave4_maximum_depth_out_of_range"
  let some negativeOp := sop.neg | throwRej "wave4_n19_forbidden"
  let negativeApplied ← applyOp root negativeOp
  let c ← checkedClosedProp false "wave4_descriptor_base_candidate"
    negativeApplied.candidate
  let initial : Wave4Descriptor := {
    root := root, op := sop, direction := negativeApplied.site.detail,
    negativeSite := negativeApplied.site, p := root.reference, c := c,
    pPrime := root.reference, cPrime := c, hops := #[]
  }
  wave4DescribeExpand initial maximumDepth

private def wave4ExtendOne (state : Wave4Build) (pOp cOp : Op)
    (pApplied cApplied : Applied) : MetaM Wave4Build := do
  let pNext ← checkedClosedProp false "wave4_p_next" pApplied.candidate
  let cNext ← checkedClosedProp false "wave4_c_next" cApplied.candidate
  if Expr.equal state.pPrime pNext || Expr.equal state.cPrime cNext then
    throwRej "wave4_hop_self_pair"
  let pSeen := #[state.p] ++ state.hops.map (fun hop => hop.pOutput)
  let cSeen := #[state.c] ++ state.hops.map (fun hop => hop.cOutput)
  if pSeen.any (fun e => alphaHash e == alphaHash pNext) ||
      cSeen.any (fun e => alphaHash e == alphaHash cNext) then
    throwRej "wave4_expression_cycle"
  let pDirect ← preservingIffProof pOp pApplied.site state.pPrime pNext
  let cDirect ← preservingIffProof cOp cApplied.site state.cPrime cNext
  let pCheck ← checkedProof "wave4_p_direct_iff" state.root.levelParams pDirect
    (iffExpr state.pPrime pNext)
  let cCheck ← checkedProof "wave4_c_direct_iff" state.root.levelParams cDirect
    (iffExpr state.cPrime cNext)
  let pComposite ← mkAppM ``Iff.trans #[state.pCompositeProof, pDirect]
  let cComposite ← mkAppM ``Iff.trans #[state.cCompositeProof, cDirect]
  let pCompositeCheck ← checkedProof "wave4_p_composite_iff" state.root.levelParams
    pComposite (iffExpr state.p pNext)
  let cCompositeCheck ← checkedProof "wave4_c_composite_iff" state.root.levelParams
    cComposite (iffExpr state.c cNext)
  let hop : Wave4Hop := {
    pOp := pOp, cOp := cOp, pSite := pApplied.site, cSite := cApplied.site,
    pInput := state.pPrime, cInput := state.cPrime, pOutput := pNext, cOutput := cNext,
    pIffProof := pDirect, cIffProof := cDirect, pCheck := pCheck, cCheck := cCheck
  }
  return { state with
    pPrime := pNext, cPrime := cNext, hops := state.hops.push hop,
    pCompositeProof := pComposite, cCompositeProof := cComposite,
    pCompositeCheck := pCompositeCheck, cCompositeCheck := cCompositeCheck }

private def initialCertifiedWave4 (descriptor : Wave4Descriptor)
    (baseNegative : NegativeEvidence) : MetaM Wave4Build := do
  let root := descriptor.root
  let c := descriptor.c
  let pIff := mkApp (mkConst ``Iff.rfl) root.reference
  let cIff := mkApp (mkConst ``Iff.rfl) c
  let pCheck ← checkedProof "wave4_p_identity" root.levelParams pIff
    (iffExpr root.reference root.reference)
  let cCheck ← checkedProof "wave4_c_identity" root.levelParams cIff (iffExpr c c)
  return {
    root := root, op := descriptor.op, direction := descriptor.direction,
    negativeSite := descriptor.negativeSite,
    p := root.reference, c := c,
    pPrime := root.reference, cPrime := c, hops := #[],
    pCompositeProof := pIff, cCompositeProof := cIff,
    pCompositeCheck := pCheck, cCompositeCheck := cCheck, baseNegative := baseNegative
  }

/-- Cross the proof boundary for one previously enumerated descriptor.  Every
    stored Expr and exact site is replayed before its direct and composite Iff
    certificates are constructed. -/
private def certifyWave4Descriptor (descriptor : Wave4Descriptor)
    (baseNegative : NegativeEvidence) : MetaM Wave4Build := do
  let some negativeOp := descriptor.op.neg | throwRej "wave4_n19_forbidden"
  let baseReplay ← applyOp descriptor.root negativeOp
  unless baseReplay.site == descriptor.negativeSite &&
      baseReplay.site.detail == descriptor.direction &&
      Expr.equal baseReplay.candidate descriptor.c do
    throwRej "wave4_base_negative_descriptor_replay_mismatch"
  let mut state ← initialCertifiedWave4 descriptor baseNegative
  for described in descriptor.hops do
    unless Expr.equal state.pPrime described.pInput &&
        Expr.equal state.cPrime described.cInput do
      throwRej "wave4_descriptor_input_mismatch"
    unless wave4Pairable described.pOp described.cOp described.pSite described.cSite &&
        wave4AllowedNext state.hops described.pOp described.cOp
          described.pSite described.cSite do
      throwRej "wave4_descriptor_chain_policy_mismatch"
    let rootP : Root := { descriptor.root with reference := state.pPrime }
    let rootC : Root := { descriptor.root with reference := state.cPrime }
    let pCandidate ← replayWave4Op rootP described.pOp described.pSite
    let cCandidate ← replayWave4Op rootC described.cOp described.cSite
    unless Expr.equal pCandidate described.pOutput &&
        Expr.equal cCandidate described.cOutput do
      throwRej "wave4_descriptor_output_mismatch"
    let next ← wave4ExtendOne state described.pOp described.cOp
      { candidate := pCandidate, site := described.pSite }
      { candidate := cCandidate, site := described.cSite }
    state := next
  unless Expr.equal state.pPrime descriptor.pPrime &&
      Expr.equal state.cPrime descriptor.cPrime do
    throwRej "wave4_descriptor_terminal_mismatch"
  return state

private def certifyWave4Descriptors (descriptors : Array Wave4Descriptor)
    (indices : Array Nat) : MetaM (Array Wave4Build) := do
  if indices.isEmpty || indices.size > 5 then
    throwRej "wave4_selected_index_count_out_of_range"
  let first := descriptors[0]?
  let some first := first | throwNA "wave4_no_preserving_descriptors"
  let baseNegative ← refuteSquareNegative first.root first.op first.c first.direction
  let mut seen : Array Nat := #[]
  let mut result : Array Wave4Build := #[]
  for index in indices do
    if seen.contains index then throwRej "wave4_duplicate_selected_index"
    seen := seen.push index
    let some descriptor := descriptors[index]?
      | throwRej "wave4_selected_index_out_of_range"
    result := result.push (← certifyWave4Descriptor descriptor baseNegative)
  return result

/-- Compatibility path for exhaustive audits.  Production uses the split
    descriptor/selected API below; this wrapper intentionally certifies every
    descriptor only when a caller explicitly requests the legacy full audit. -/
def buildWave4Orbits (root : Root) (sop : SquareOp) (maximumDepth : Nat) :
    MetaM (Array Wave4Build) := do
  let descriptors ← buildWave4Descriptors root sop maximumDepth
  if descriptors.isEmpty then return #[]
  let indices := (List.range descriptors.size).toArray
  -- The exhaustive compatibility path can exceed the production max-five cap,
  -- so certify in deterministic chunks while sharing each chunk's base proof.
  let mut result : Array Wave4Build := #[]
  for start in List.range ((descriptors.size + 4) / 5) do
    let chunk := (indices.extract (start * 5) (min descriptors.size (start * 5 + 5)))
    result := result ++ (← certifyWave4Descriptors descriptors chunk)
  return result

/-- Re-enumerate descriptors deterministically, then construct proofs for only
    the selected maximum-five indices.  This is the second half of the Wave 4
    request protocol used before frozen rendering. -/
def rebuildSelectedWave4Orbits (name : String) (opId : String) (maximumDepth : Nat)
    (indices : Array Nat) : MetaM (Array Wave4Build) := do
  let some sop := squareOp? opId | throwError s!"unknown square operation {opId}"
  unless opId.startsWith "ORBIT_WAVE4_" do throwError "not a Wave 4 orbit operation"
  let descriptors ← buildWave4Descriptors (← loadRoot (parseName name)) sop maximumDepth
  certifyWave4Descriptors descriptors indices

private def wave4DescriptorHopJson (hop : Wave4DescriptorHop) : Json :=
  Json.mkObj [
    ("p_operation", Json.str hop.pOp.id), ("c_operation", Json.str hop.cOp.id),
    ("mechanism", Json.str hop.pOp.wave4Mechanism),
    ("superclass", Json.str hop.pOp.wave4Superclass),
    ("inverse_token", Json.str hop.pOp.wave4InverseToken),
    ("p_site", siteJson hop.pSite), ("c_site", siteJson hop.cSite),
    ("p_input_alpha_hash", Json.str (toString (alphaHash hop.pInput))),
    ("c_input_alpha_hash", Json.str (toString (alphaHash hop.cInput))),
    ("p_output_alpha_hash", Json.str (toString (alphaHash hop.pOutput))),
    ("c_output_alpha_hash", Json.str (toString (alphaHash hop.cOutput))),
    ("site_transport", Json.str "disjoint_root_coordinates")
  ]

private def wave4DescriptorJson (index : Nat) (descriptor : Wave4Descriptor) : Json :=
  Json.mkObj [
    ("index", toJson index), ("depth", toJson descriptor.hops.size),
    ("p_alpha_hash", Json.str (toString (alphaHash descriptor.p))),
    ("c_alpha_hash", Json.str (toString (alphaHash descriptor.c))),
    ("p_prime_alpha_hash", Json.str (toString (alphaHash descriptor.pPrime))),
    ("c_prime_alpha_hash", Json.str (toString (alphaHash descriptor.cPrime))),
    ("negative_site", siteJson descriptor.negativeSite),
    ("hops", Json.arr (descriptor.hops.map wave4DescriptorHopJson))
  ]

/-- First phase of the split JSON protocol.  Its `described` status cannot be
    mistaken for a certified row: it emits only deterministic operation/site
    chains and endpoint identities for Lean-free stable max-five selection. -/
def processWave4DescriptorRoot (name : Name) (sop : SquareOp)
    (maximumDepth : Nat) : MetaM Unit := do
  let t0 ← IO.monoMsNow
  let base := [("schema_version", toJson 1),
    ("kind", Json.str "wave4_descriptor_root"),
    ("operation_id", Json.str sop.id), ("negative_operation", Json.str sop.negId),
    ("engine_semantic_version", Json.str engineSemanticVersion),
    ("root", Json.str name.toString)]
  let outcome : Except String (Root × Array Wave4Descriptor) ← tryCatchRuntimeEx
    (do
      let root ← loadRoot name
      let descriptors ← withHeartbeatBudget opHeartbeatBudgetK
        (buildWave4Descriptors root sop maximumDepth)
      return .ok (root, descriptors))
    fun ex => do
      if ex.isInterrupt then throw ex
      return .error (← exceptionText ex)
  match outcome with
  | .error msg =>
      let (status, reason) := classify msg
      emitJson <| Json.mkObj (base ++ [("status", Json.str status),
        ("reason", Json.str reason), ("descriptors", Json.arr #[]),
        ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])
  | .ok (root, descriptors) =>
      emitJson <| Json.mkObj (base ++ [
        ("status", Json.str (if descriptors.isEmpty then "not_applicable" else "described")),
        ("reason", Json.str (if descriptors.isEmpty then "wave4_no_structural_chain" else "")),
        ("module", Json.str root.module.toString),
        ("level_params", Json.arr (root.levelParams.toArray.map fun n => Json.str n.toString)),
        ("descriptors", Json.arr (descriptors.mapIdx wave4DescriptorJson)),
        ("enumerated_descriptor_count", toJson descriptors.size),
        ("certificate_phase", Json.str "selected_only"),
        ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])

def processWave4DescriptorRoots (names : Array String) (opId : String)
    (maximumDepth : Nat) : MetaM Unit := do
  let some sop := squareOp? opId | throwError s!"unknown square operation {opId}"
  unless opId.startsWith "ORBIT_WAVE4_" do throwError "not a Wave 4 orbit operation"
  for name in names do processWave4DescriptorRoot (parseName name) sop maximumDepth

private def wave4HopJson (hop : Wave4Hop) : Json :=
  Json.mkObj [
    ("p_operation", Json.str hop.pOp.id), ("c_operation", Json.str hop.cOp.id),
    ("mechanism", Json.str hop.pOp.wave4Mechanism),
    ("superclass", Json.str hop.pOp.wave4Superclass),
    ("inverse_token", Json.str hop.pOp.wave4InverseToken),
    ("p_site", siteJson hop.pSite), ("c_site", siteJson hop.cSite),
    ("p_input_alpha_hash", Json.str (toString (alphaHash hop.pInput))),
    ("c_input_alpha_hash", Json.str (toString (alphaHash hop.cInput))),
    ("p_output_alpha_hash", Json.str (toString (alphaHash hop.pOutput))),
    ("c_output_alpha_hash", Json.str (toString (alphaHash hop.cOutput))),
    ("p_direct_iff", hop.pCheck.toJson), ("c_direct_iff", hop.cCheck.toJson),
    ("site_transport", Json.str "disjoint_root_coordinates")
  ]

def wave4Evidence (orbit : Wave4Build) : MetaM Json := do
  let params := orbit.root.levelParams
  let p0 := squareLevelZero params orbit.p
  let c0 := squareLevelZero params orbit.c
  let p'0 := squareLevelZero params orbit.pPrime
  let c'0 := squareLevelZero params orbit.cPrime
  let pIff0 := squareLevelZero params orbit.pCompositeProof
  let cIff0 := squareLevelZero params orbit.cCompositeProof
  let source0 := sourceConst orbit.root
  let sourceCheck ← checkedProof "wave4_source" [] source0 p0
  let proofP'0 := mkApp4 (mkConst ``Iff.mp) p0 p'0 pIff0 source0
  let proofP'Check ← checkedProof "wave4_p_prime_transported" [] proofP'0 p'0
  -- A negative-last row is admitted only after executing the negative engine
  -- again on the exact preserved reference Expr.  Transporting `Not C` through
  -- `C ↔ C′` remains useful closure evidence below, but cannot substitute for
  -- this replay or its independently checked exact-candidate refutation.
  let replayRoot : Root := { orbit.root with reference := orbit.pPrime }
  unless Expr.equal replayRoot.reference orbit.pPrime do
    throwRej "wave4_negative_last_reference_replay_mismatch"
  let some negativeOp := orbit.op.neg | throwRej "wave4_n19_forbidden"
  let negativeLastApplied ← applyOp replayRoot negativeOp
  let negativeLastCandidate ← checkedClosedProp false
    "wave4_negative_last_candidate" negativeLastApplied.candidate
  unless Expr.equal negativeLastCandidate orbit.cPrime do
    throwRej "wave4_negative_last_candidate_replay_mismatch"
  let negativeLastEvidence ← refuteWave4ReappliedNegative replayRoot orbit.op
    { negativeLastApplied with candidate := negativeLastCandidate } proofP'0
  let negativeLastCertificate ← negativeEvidenceJson replayRoot negativeLastEvidence
  let notC0 := orbit.baseNegative.proof
  let notC'0 ← withLocalDecl `h .default c'0 fun h => do
    mkLambdaFVars #[h] (mkApp notC0 (mkApp4 (mkConst ``Iff.mpr) c0 c'0 cIff0 h))
  let notC'Check ← checkedProof "wave4_c_prime_refutation" [] notC'0
    (mkApp (mkConst ``Not) c'0)
  let notIffCP ← withLocalDecl `h .default (iffExpr c0 p0) fun h => do
    mkLambdaFVars #[h] (mkApp notC0 (mkApp4 (mkConst ``Iff.mpr) c0 p0 h source0))
  let baseCheck ← checkedProof "wave4_base_negative" [] notIffCP
    (mkApp (mkConst ``Not) (iffExpr c0 p0))
  let notIffP'C' ← withLocalDecl `h .default (iffExpr p'0 c'0) fun h => do
    mkLambdaFVars #[h] (mkApp notC'0 (mkApp4 (mkConst ``Iff.mp) p'0 c'0 h proofP'0))
  let terminalCheck ← checkedProof "wave4_terminal_negative" [] notIffP'C'
    (mkApp (mkConst ``Not) (iffExpr p'0 c'0))
  let baseNegativeCertificate ← negativeEvidenceJson orbit.root orbit.baseNegative
  return Json.mkObj [
    ("negative_operation", Json.str orbit.op.negId),
    ("direction", Json.str orbit.direction),
    ("hops", Json.arr (orbit.hops.map wave4HopJson)),
    ("p_composite_iff", orbit.pCompositeCheck.toJson),
    ("c_composite_iff", orbit.cCompositeCheck.toJson),
    ("source_proof", sourceProofJson orbit.root),
    ("source_proof_check", sourceCheck.toJson),
    ("base_candidate_refutation", baseNegativeCertificate),
    ("p_prime_transported_proof", proofP'Check.toJson),
    ("c_prime_refutation", notC'Check.toJson),
    ("not_iff_c_p", baseCheck.toJson),
    ("not_iff_p_prime_c_prime", terminalCheck.toJson),
    ("negative_last_replay", Json.mkObj [
      ("operation_id", Json.str negativeOp.id),
      ("direction", Json.str negativeLastApplied.site.detail),
      ("reference_alpha_hash", Json.str (toString (alphaHash replayRoot.reference))),
      ("candidate_alpha_hash", Json.str (toString (alphaHash negativeLastCandidate))),
      ("reference_expr_equal", Json.bool true),
      ("candidate_expr_equal", Json.bool true),
      ("reference_replay_exact", Json.bool true),
      ("candidate_replay_exact", Json.bool true),
      ("site", siteJson negativeLastApplied.site),
      ("refutation", negativeLastEvidence.refutation.toJson),
      ("certificate", negativeLastCertificate)]),
    ("closure", Json.mkObj [
      ("exact_typed", Json.bool true),
      ("site_policy", Json.str "disjoint_only_no_transport_inference"),
      ("depth", toJson orbit.hops.size)])
  ]

private def wave4VariantJson (index : Nat) (orbit : Wave4Build) (evidence : Json)
    (goals : Array String) : Json :=
  Json.mkObj [
    ("index", toJson index), ("depth", toJson orbit.hops.size),
    ("p_alpha_hash", Json.str (toString (alphaHash orbit.p))),
    ("c_alpha_hash", Json.str (toString (alphaHash orbit.c))),
    ("p_prime_alpha_hash", Json.str (toString (alphaHash orbit.pPrime))),
    ("c_prime_alpha_hash", Json.str (toString (alphaHash orbit.cPrime))),
    ("negative_site", siteJson orbit.negativeSite),
    ("goals", Json.mkObj [
      ("p", Json.str goals[0]!), ("c", Json.str goals[1]!),
      ("p_prime", Json.str goals[2]!), ("c_prime", Json.str goals[3]!)]),
    ("evidence", evidence)
  ]

private def wave4VariantCore (index : Nat) (orbit : Wave4Build) : MetaM Json := do
  let mut goals := #[]
  for e in [orbit.p, orbit.c, orbit.pPrime, orbit.cPrime] do
    match ← prerender e with
    | .ok text => goals := goals.push text
    | .error msg => throwRej s!"wave4_unrenderable:{msg}"
  return wave4VariantJson index orbit (← wave4Evidence orbit) goals

/-- Emit full evidence only for the descriptors selected in the first phase.
    Callers place this in the same second request as the frozen endpoint
    emitters, so both evidence and model text arise from the same rebuilt Exprs. -/
def emitSelectedWave4Report (name : String) (opId : String) (maximumDepth : Nat)
    (indices : Array Nat) (orbits : Array Wave4Build) : MetaM Unit := do
  if indices.isEmpty || indices.size > 5 || indices.size != orbits.size then
    throwRej "wave4_selected_report_count_mismatch"
  let mut seen : Array Nat := #[]
  let mut variants : Array Json := #[]
  for slot in List.range indices.size do
    let index := indices[slot]!
    if seen.contains index then throwRej "wave4_duplicate_selected_index"
    seen := seen.push index
    let some orbit := orbits[slot]? | throwRej "wave4_selected_report_missing_orbit"
    unless orbit.root.name.toString == name && orbit.op.id == opId &&
        orbit.hops.size <= maximumDepth do
      throwRej "wave4_selected_report_identity_mismatch"
    variants := variants.push (← wave4VariantCore index orbit)
  let some firstOrbit := orbits[0]? | throwRej "wave4_selected_report_missing_root"
  let root := firstOrbit.root
  emitJson <| Json.mkObj [
    ("schema_version", toJson 1), ("kind", Json.str "wave4_selected_root"),
    ("status", Json.str "retained"), ("reason", Json.str ""),
    ("operation_id", Json.str opId), ("negative_operation", Json.str firstOrbit.op.negId),
    ("engine_semantic_version", Json.str engineSemanticVersion),
    ("root", Json.str name), ("module", Json.str root.module.toString),
    ("level_params", Json.arr (root.levelParams.toArray.map fun n => Json.str n.toString)),
    ("selected_descriptor_indices", Json.arr (indices.map toJson)),
    ("selected_variant_count", toJson variants.size),
    ("variants", Json.arr variants),
    ("certificate_phase", Json.str "selected_only")]

/-! ## Proof-bearing compiler-data roots

These entry points are deliberately additive.  Imported library names retain
the `loadRoot` contract above.  A compiler row must declare its theorem in the
same request, provide a content binding reconstructed from the pinned
inventory, and cross `loadCompilerRootChecked` before it reaches any shared
Wave 3 or Wave 4 mechanism. -/

private def compilerWave4DescriptorPayload (loaded : LoadedCompilerRoot)
    (binding : CompilerSourceBinding) (sop : SquareOp) (maximumDepth : Nat) : MetaM Json := do
  let t0 ← IO.monoMsNow
  let root := loaded.root
  let base := [
    ("schema_version", toJson 1),
    ("kind", Json.str "wave4_descriptor_root"),
    ("operation_id", Json.str sop.id),
    ("negative_operation", Json.str sop.negId),
    ("engine_semantic_version", Json.str engineSemanticVersion),
    ("root", Json.str root.name.toString),
    ("module", Json.str root.module.toString),
    ("level_params", Json.arr (root.levelParams.toArray.map fun n => Json.str n.toString)),
    ("compiler_source_binding", binding.toJson),
    ("source_proof_check", loaded.sourceProofCheck.toJson)]
  let outcome : Except String (Array Wave4Descriptor) ← tryCatchRuntimeEx
    (do
      let descriptors ← withHeartbeatBudget opHeartbeatBudgetK
        (buildWave4Descriptors root sop maximumDepth)
      return .ok descriptors)
    fun ex => do
      if ex.isInterrupt then throw ex
      return .error (← exceptionText ex)
  match outcome with
  | .error msg =>
      let (status, reason) := classify msg
      return Json.mkObj (base ++ [
        ("status", Json.str status), ("reason", Json.str reason),
        ("descriptors", Json.arr #[]), ("enumerated_descriptor_count", toJson 0),
        ("certificate_phase", Json.str "selected_only"),
        ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])
  | .ok descriptors =>
      return Json.mkObj (base ++ [
        ("status", Json.str (if descriptors.isEmpty then "not_applicable" else "described")),
        ("reason", Json.str (if descriptors.isEmpty then "wave4_no_structural_chain" else "")),
        ("descriptors", Json.arr (descriptors.mapIdx wave4DescriptorJson)),
        ("enumerated_descriptor_count", toJson descriptors.size),
        ("certificate_phase", Json.str "selected_only"),
        ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])

/-- Run the exact same Wave 3 `runOp` implementation and proof-free Wave 4
    descriptor builder over one checked local compiler theorem.  Each Wave 4
    operation emits the standard descriptor schema so the existing Lean-free
    max-five selector can consume it with `rootId` as its ancestry identity. -/
def processCompilerRoot (name : String) (binding : CompilerSourceBinding) (mask : Nat)
    (orbitOpIds : Array String) (maximumDepth : Nat) : MetaM Unit := do
  let t0 ← IO.monoMsNow
  let parsedName := parseName name
  let base := [
    ("schema_version", toJson 1), ("kind", Json.str "compiler_root"),
    ("engine_semantic_version", Json.str engineSemanticVersion),
    ("engine_operation_set_version", toJson engineOperationSetVersion),
    ("root", Json.str name), ("compiler_source_binding", binding.toJson)]
  let loaded : Except String LoadedCompilerRoot ← tryCatchRuntimeEx
    (do
      binding.validate parsedName
      return .ok (← loadCompilerRootChecked parsedName))
    fun ex => do
      if ex.isInterrupt then throw ex
      return .error (← exceptionText ex)
  match loaded with
  | .error msg =>
      let (status, reason) := classify msg
      emitJson <| Json.mkObj (base ++ [
        ("root_status", Json.str status), ("reason", Json.str reason),
        ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])
  | .ok loaded =>
      let root := loaded.root
      let reports ← binderReports root.reference #[]
      match ← prerender root.reference with
      | .error msg =>
          emitJson <| Json.mkObj (base ++ [
            ("root_status", Json.str "not_applicable"),
            ("reason", Json.str s!"reference_unrenderable:{msg}"),
            ("module", Json.str root.module.toString),
            ("source_proof_check", loaded.sourceProofCheck.toJson),
            ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])
      | .ok referenceGoal =>
          let mut outcomes : Array Json := #[]
          for op in Op.all do
            if mask.testBit op.bit then
              outcomes := outcomes.push (outcomeJson (← runOp root op))
          emitJson <| Json.mkObj (base ++ [
            ("root_status", Json.str "ok"), ("reason", Json.str ""),
            ("module", Json.str root.module.toString),
            ("level_params", Json.arr (root.levelParams.toArray.map fun n => Json.str n.toString)),
            ("reference_alpha_hash", Json.str (toString (alphaHash root.reference))),
            ("reference_goal", Json.str referenceGoal),
            ("source_proof_value_hash_u64", Json.str (toString root.proofValueHash)),
            ("source_proof_check", loaded.sourceProofCheck.toJson),
            ("binders", binderReportsJson reports),
            ("terminals", Json.arr outcomes),
            ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])
          for opId in orbitOpIds do
            let some sop := squareOp? opId
              | throwError s!"unknown compiler Wave 4 operation {opId}"
            unless opId.startsWith "ORBIT_WAVE4_" do
              throwError "compiler Wave 4 operation is not an orbit operation"
            emitJson (← compilerWave4DescriptorPayload loaded binding sop maximumDepth)

/-- Rebuild one or more retained Wave 3 pairs from the checked local compiler
    theorem for the frozen renderer.  No statement text is re-elaborated. -/
def rebuildCompilerPairs (name : String) (binding : CompilerSourceBinding)
    (opIds : Array String) : MetaM (Array RebuiltPair) := do
  let parsedName := parseName name
  binding.validate parsedName
  let root ← loadCompilerRoot parsedName
  let mut result : Array RebuiltPair := #[]
  for opId in opIds do
    let some op := Op.ofId? opId | throwError s!"unknown compiler operation {opId}"
    let applied ← applyOp root op
    let candidate ← checkedClosedProp false "compiler_candidate" applied.candidate
    result := result.push { reference := root.reference, candidate }
  return result

/-- Re-enumerate and certify only the Lean-free selected Wave 4 descriptors for
    a checked local compiler theorem. -/
def rebuildSelectedCompilerWave4Orbits (name : String) (binding : CompilerSourceBinding)
    (opId : String) (maximumDepth : Nat) (indices : Array Nat) : MetaM (Array Wave4Build) := do
  let parsedName := parseName name
  binding.validate parsedName
  let some sop := squareOp? opId | throwError s!"unknown compiler Wave 4 operation {opId}"
  unless opId.startsWith "ORBIT_WAVE4_" do
    throwError "compiler Wave 4 operation is not an orbit operation"
  let descriptors ← buildWave4Descriptors (← loadCompilerRoot parsedName) sop maximumDepth
  certifyWave4Descriptors descriptors indices

/-- Emit the standard selected-certificate shape plus the exact compiler source
    binding and checked local source proof.  The ordinary library emitter above
    remains unchanged. -/
def emitSelectedCompilerWave4Report (name : String) (binding : CompilerSourceBinding)
    (opId : String) (maximumDepth : Nat) (indices : Array Nat)
    (orbits : Array Wave4Build) : MetaM Unit := do
  let parsedName := parseName name
  binding.validate parsedName
  if indices.isEmpty || indices.size > 5 || indices.size != orbits.size then
    throwRej "compiler_wave4_selected_report_count_mismatch"
  let loaded ← loadCompilerRootChecked parsedName
  let mut seen : Array Nat := #[]
  let mut variants : Array Json := #[]
  for slot in List.range indices.size do
    let index := indices[slot]!
    if seen.contains index then throwRej "wave4_duplicate_selected_index"
    seen := seen.push index
    let some orbit := orbits[slot]? | throwRej "compiler_wave4_selected_report_missing_orbit"
    unless orbit.root.name == parsedName && orbit.op.id == opId &&
        orbit.hops.size <= maximumDepth do
      throwRej "compiler_wave4_selected_report_identity_mismatch"
    variants := variants.push (← wave4VariantCore index orbit)
  let some firstOrbit := orbits[0]? | throwRej "compiler_wave4_selected_report_missing_root"
  emitJson <| Json.mkObj [
    ("schema_version", toJson 1), ("kind", Json.str "wave4_selected_root"),
    ("status", Json.str "retained"), ("reason", Json.str ""),
    ("operation_id", Json.str opId),
    ("negative_operation", Json.str firstOrbit.op.negId),
    ("engine_semantic_version", Json.str engineSemanticVersion),
    ("root", Json.str name), ("module", Json.str loaded.root.module.toString),
    ("level_params", Json.arr
      (loaded.root.levelParams.toArray.map fun n => Json.str n.toString)),
    ("selected_descriptor_indices", Json.arr (indices.map toJson)),
    ("selected_variant_count", toJson variants.size),
    ("variants", Json.arr variants),
    ("certificate_phase", Json.str "selected_only"),
    ("compiler_source_binding", binding.toJson),
    ("source_proof_check", loaded.sourceProofCheck.toJson)]

def processWave4Root (name : Name) (sop : SquareOp) (maximumDepth : Nat) : MetaM Unit := do
  let t0 ← IO.monoMsNow
  let base := [("schema_version", toJson 1), ("kind", Json.str "wave4_root"),
    ("operation_id", Json.str sop.id), ("negative_operation", Json.str sop.negId),
    ("engine_semantic_version", Json.str engineSemanticVersion),
    ("root", Json.str name.toString)]
  let outcome : Except String (Root × Array Json) ← tryCatchRuntimeEx
    (do
      let root ← loadRoot name
      let orbits ← withHeartbeatBudget opHeartbeatBudgetK
        (buildWave4Orbits root sop maximumDepth)
      let mut variants := #[]
      let mut index := 0
      for orbit in orbits do
        variants := variants.push (← wave4VariantCore index orbit)
        index := index + 1
      return .ok (root, variants))
    fun ex => do
      if ex.isInterrupt then throw ex
      return .error (← exceptionText ex)
  match outcome with
  | .error msg =>
      let (status, reason) := classify msg
      emitJson <| Json.mkObj (base ++ [("status", Json.str status),
        ("reason", Json.str reason), ("variants", Json.arr #[]),
        ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])
  | .ok (root, variants) =>
      emitJson <| Json.mkObj (base ++ [
        ("status", Json.str (if variants.isEmpty then "not_applicable" else "retained")),
        ("reason", Json.str (if variants.isEmpty then "wave4_no_certified_closure" else "")),
        ("module", Json.str root.module.toString),
        ("level_params", Json.arr (root.levelParams.toArray.map fun n => Json.str n.toString)),
        ("variants", Json.arr variants),
        ("enumerated_variant_count", toJson variants.size),
        ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])

def processWave4Roots (names : Array String) (opId : String) (maximumDepth : Nat) : MetaM Unit := do
  let some sop := squareOp? opId | throwError s!"unknown square operation {opId}"
  unless opId.startsWith "ORBIT_WAVE4_" do throwError "not a Wave 4 orbit operation"
  for name in names do processWave4Root (parseName name) sop maximumDepth

def rebuildWave4Orbits (name : String) (opId : String) (maximumDepth : Nat) :
    MetaM (Array Wave4Build) := do
  let some sop := squareOp? opId | throwError s!"unknown square operation {opId}"
  unless opId.startsWith "ORBIT_WAVE4_" do throwError "not a Wave 4 orbit operation"
  buildWave4Orbits (← loadRoot (parseName name)) sop maximumDepth

private def squareCore (root : Root) (sop : SquareOp) :
    MetaM (SquareBuild × Json × Array String) := do
  let sq ← buildSquare root sop
  let mut goals : Array String := #[]
  for e in [sq.p, sq.c, sq.pPrime, sq.cPrime] do
    match ← prerender e with
    | .ok text => goals := goals.push text
    | .error msg => throwRej s!"square_unrenderable:{msg}"
  -- rendered-goal diamond: T(N(P)) and N(T(P)) are the same Expr, so the same text
  let rootPPrime : Root := { root with reference := sq.pPrime }
  let (nOfT, _) ← squareNegate rootPPrime sop
  match ← prerender nOfT with
  | .ok text => unless text == goals[3]! do throwRej "square_diamond_render_mismatch"
  | .error msg => throwRej s!"square_unrenderable:{msg}"
  let evidence ← squareEvidence sq
  return (sq, evidence, goals)

/-- Process one certified root into a square for `sop` and emit one evidence line. -/
def processSquare (name : Name) (sop : SquareOp) : MetaM Unit := do
  let t0 ← IO.monoMsNow
  let base := [("schema_version", toJson 1), ("kind", Json.str "square"),
    ("operation_id", Json.str sop.id),
    ("negative_operation", Json.str sop.negId),
    ("engine_semantic_version", Json.str engineSemanticVersion),
    ("engine_operation_set_version", toJson engineOperationSetVersion),
    ("root", Json.str name.toString)]
  let outcome : Except String (SquareBuild × Json × Array String) ←
    tryCatchRuntimeEx
      (do
        let root ← loadRoot name
        return Except.ok (← withHeartbeatBudget opHeartbeatBudgetK (squareCore root sop)))
      fun ex => do
        if ex.isInterrupt then throw ex
        return Except.error (← exceptionText ex)
  match outcome with
  | .error msg =>
      let (status, reason) := classify msg
      emitJson <| Json.mkObj (base ++ [
        ("status", Json.str status), ("reason", Json.str reason),
        ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])
  | .ok (sq, evidence, goals) =>
      emitJson <| Json.mkObj (base ++ [
        ("status", Json.str "retained"),
        ("reason", Json.str ""),
        ("module", Json.str sq.root.module.toString),
        ("level_params", Json.arr (sq.root.levelParams.toArray.map fun n => Json.str n.toString)),
        ("direction", Json.str sq.direction),
        ("t_p", Json.str sq.tP.id),
        ("t_c", Json.str sq.tC.id),
        ("alpha", Json.mkObj [
          ("p", Json.str (toString (alphaHash sq.p))),
          ("c", Json.str (toString (alphaHash sq.c))),
          ("p_prime", Json.str (toString (alphaHash sq.pPrime))),
          ("c_prime", Json.str (toString (alphaHash sq.cPrime)))]),
        ("goals", Json.mkObj [
          ("p", Json.str goals[0]!), ("c", Json.str goals[1]!),
          ("p_prime", Json.str goals[2]!), ("c_prime", Json.str goals[3]!)]),
        ("evidence", evidence),
        ("elapsed_ms", toJson ((← IO.monoMsNow) - t0))])

private def resolveSquareOp (opId : String) : MetaM SquareOp := do
  match squareOp? opId with
  | some sop => return sop
  | none => throwError s!"unknown square operation {opId}"

def processSquares (names : Array String) (opId : String) : MetaM Unit := do
  let sop ← resolveSquareOp opId
  for name in names do
    processSquare (parseName name) sop

def rebuildSquares (names : Array String) (opId : String) : MetaM (Array SquareBuild) := do
  let sop ← resolveSquareOp opId
  let mut result := #[]
  for name in names do
    result := result.push (← buildSquare (← loadRoot (parseName name)) sop)
  return result

def emitSquareReport (squares : Array SquareBuild) : MetaM Unit := do
  let entries := squares.mapIdx fun i sq =>
    Json.mkObj [
      ("index", toJson i),
      ("p", Json.str (toString (alphaHash sq.p))),
      ("c", Json.str (toString (alphaHash sq.c))),
      ("p_prime", Json.str (toString (alphaHash sq.pPrime))),
      ("c_prime", Json.str (toString (alphaHash sq.cPrime)))
    ]
  emitJson <| Json.mkObj [
    ("schema_version", toJson 1), ("kind", Json.str "square_rebuild"),
    ("engine_semantic_version", Json.str engineSemanticVersion),
    ("squares", Json.arr entries)]

end LeanFaith.SFT1.Sprint
