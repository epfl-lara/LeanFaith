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

def engineSemanticVersion : String := "sft1_sprint_engine_v1"
/-- Operation-set version: 2 adds `P_NE_SYMMETRIZE_V1` and
    `P_DROP_REDUNDANT_GUARD_PROOF_V1` without changing any earlier operation. -/
def engineOperationSetVersion : Nat := 2
def evidenceMarker : String := "LFSFT1SPRINTJSON "

/-- Per-operation heartbeat budget in thousands of heartbeats. -/
def opHeartbeatBudgetK : Nat := 120000
/-- Maximum candidate-value branches explored while grounding one telescope. -/
def groundingBranchBudget : Nat := 400
/-- Maximum tactic elaborations while grounding one telescope. -/
def groundingTacticBudget : Nat := 40

inductive Op where
  | p15 | p18 | p14 | p23 | n25 | n32 | n31 | pne | pdrg
  deriving BEq, Repr, Inhabited

def Op.all : Array Op := #[.p15, .p18, .p14, .p23, .n25, .n32, .n31, .pne, .pdrg]

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

def Op.ofId? (s : String) : Option Op := Op.all.find? (·.id == s)

def Op.positive : Op → Bool
  | .p15 | .p18 | .p14 | .p23 | .pne | .pdrg => true
  | .n25 | .n32 | .n31 => false

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

private def rootNameExcluded (n : Name) : Bool :=
  n.isInternal || n.isInternalDetail || n.hasMacroScopes

def loadRoot (name : Name) : MetaM Root := do
  if rootNameExcluded name then
    throwNA "root_internal_name"
  let env ← getEnv
  let some info := env.find? name
    | throwNA "root_not_found"
  let tv ←
    match info with
    | .thmInfo tv => pure tv
    | _ => throwNA "root_not_theorem"
  let some modIdx := env.getModuleIdxFor? name
    | throwNA "root_not_imported"
  let some module := env.allImportedModuleNames[modIdx.toNat]?
    | throwNA "root_module_unknown"
  unless module.getRoot == `Mathlib do
    throwNA "root_not_mathlib_module"
  if ← isInstance name then
    throwNA "root_is_instance"
  let reference ← checkedClosedProp true "reference" (eraseMDataDeep tv.type)
  return {
    name
    module
    levelParams := tv.levelParams
    reference
    proofValueHash := hash tv.value
  }

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
  literal : Int := 0
  deriving BEq, Repr, Inhabited

def siteJson (s : Site) : Json :=
  Json.mkObj [
    ("kind", Json.str s.kind),
    ("index", toJson s.index),
    ("detail", Json.str s.detail),
    ("guard_variable_index", toJson s.guardVariableIndex),
    ("literal", toJson s.literal)
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

private def swapStrictLt? (t : Expr) : Option (Expr × String) :=
  let args := t.getAppArgs
  if headName? t == some ``LT.lt && args.size == 4 then
    match isNatOrInt args[0]! with
    | some ty =>
        if Expr.eqv args[2]! args[3]! then none
        else some (mkApp4 t.getAppFn args[0]! args[1]! args[3]! args[2]!, ty)
    | none => none
  else
    none

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

/-! ## Transform application (no proofs) -/

structure Applied where
  candidate : Expr
  site : Site
  deriving Repr

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
  | .n32 => applyFinalTarget root "final_target_strict_lt" swapStrictLt?
  | .pne => applyFinalTarget root "final_target_ne" swapNe?
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
          literal := m.literal
        }
      }

/-- Replay a certificate site against the reference without rediscovery where
    the site alone determines the candidate. -/
def replayOp (root : Root) (op : Op) (site : Site) : MetaM Expr := do
  match op with
  | .p15 | .p18 | .n25 | .n32 | .pne =>
      let applied ← applyOp root op
      unless applied.site == site do throwRej "replay_site_mismatch"
      return applied.candidate
  | .pdrg => applyDropGuardAt root site.index
  | .p14 => applyP14At root site.index
  | .p23 =>
      let packName := freshPackName root.reference
      unless packName.toString == site.detail do throwRej "replay_pack_name_mismatch"
      applyP23At root site.index packName
  | .n31 => applyN31At root site.index

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

private def dataValues (d : Expr) : Array (Expr × String) :=
  if d.isConstOf ``Nat then
    #[(mkNatLit 0, "0"), (mkNatLit 1, "1"), (mkNatLit 2, "2"), (mkNatLit 3, "3")]
  else if d.isConstOf ``Int then
    #[(mkIntLit 0, "0"), (mkIntLit 1, "1"), (mkIntLit (-1), "-1"),
      (mkIntLit 2, "2"), (mkIntLit (-2), "-2")]
  else if d.isConstOf ``Bool then
    #[(mkConst ``Bool.true, "true"), (mkConst ``Bool.false, "false")]
  else if d == mkSort Level.zero then
    #[(mkConst ``True, "True"), (mkConst ``False, "False")]
  else if d == mkSort Level.one then
    #[(mkConst ``Nat, "Nat"), (mkConst ``Int, "Int")]
  else
    #[]

private def literalValue (ty : String) (k : Int) : Expr × String :=
  if ty == "Nat" then (mkNatLit k.toNat, toString k) else (mkIntLit k, toString k)

private def binderCandidates (d : Expr) (bi : BinderInfo) (idx : Nat)
    (constraint : Nat → Option (Array (Expr × String))) (tacticBudget : IO.Ref Nat) :
    MetaM (Array (Expr × String)) := do
  if let some forced := constraint idx then
    return forced
  if bi == .instImplicit then
    match ← synthInstance? d with
    | some inst => return #[(inst, "instance")]
    | none => return #[]
  if ← isProp d then
    match ← proveClosed? d tacticBudget with
    | some (proof, label) => return #[(proof, s!"proof:{label}")]
    | none => return #[]
  return dataValues d

/-- Depth-first grounding of a closed telescope.  `leaf` receives the closed
    innermost body and the assignment; the first assignment it accepts wins. -/
private partial def groundTelescope {β : Type} (ty : Expr) (idx : Nat)
    (constraint : Nat → Option (Array (Expr × String)))
    (acc : Array Expr) (descs : Array String)
    (branchBudget tacticBudget : IO.Ref Nat)
    (leaf : Expr → Grounding → MetaM (Option β)) : MetaM (Option (Grounding × β)) := do
  match ty with
  | .forallE _ d b bi =>
      let candidates ← binderCandidates d bi idx constraint tacticBudget
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

private def groundingJson (g : Grounding) (universeInstantiation : String) (tacticCalls : Nat) : Json :=
  Json.mkObj [
    ("assignment", Json.arr (g.descriptions.map Json.str)),
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
  /-- The checked `Not candidate` proof at universe level zero. -/
  proof : Expr

private def sourceConst (root : Root) : Expr :=
  mkConst root.name (root.levelParams.map fun _ => Level.zero)

private def universeTag (root : Root) : String :=
  if root.levelParams.isEmpty then "none" else "all_zero"

/-- `Not candidate` from an assignment satisfying the reference telescope and
    a `False` builder from the applied source and candidate proofs. -/
private def refuteViaSource (root : Root) (cand : Expr)
    (falseOf : Expr → Expr → MetaM Expr) : MetaM NegativeEvidence := do
  let ref0 := levelZeroInstantiate root.levelParams root.reference
  let cand0 := levelZeroInstantiate root.levelParams cand
  let branchBudget ← IO.mkRef groundingBranchBudget
  let tacticBudget ← IO.mkRef groundingTacticBudget
  let some (g, ()) ← groundTelescope ref0 0 (fun _ => none) #[] #[] branchBudget tacticBudget
      (fun _ _ => return some ())
    | throwRej "no_ground_assignment"
  let notCand := mkApp (mkConst ``Not) cand0
  let proof ← withLocalDecl `h .default cand0 fun h => do
    let sourceApp := mkAppN (sourceConst root) g.values
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

private def n25Refute (root : Root) (cand : Expr) (direction : String) : MetaM NegativeEvidence :=
  refuteViaSource root cand fun sourceApp candApp => do
    if direction == "eq_to_ne" then
      return mkApp candApp sourceApp
    else
      return mkApp sourceApp candApp

private def n32Refute (root : Root) (cand : Expr) (ty : String) : MetaM NegativeEvidence :=
  refuteViaSource root cand fun sourceApp candApp => do
    let asymm ← if ty == "Nat" then mkAppM ``Nat.lt_asymm #[sourceApp]
      else mkAppM ``Int.lt_asymm #[sourceApp]
    return mkApp asymm candApp

private def n31Refute (root : Root) (cand : Expr) (site : Site) : MetaM NegativeEvidence := do
  let cand0 := levelZeroInstantiate root.levelParams cand
  let (_, m) ← discoverN31 root
  let tacticBudget ← IO.mkRef groundingTacticBudget
  let mut result : Option (Grounding × Expr × String × Int) := none
  for boundary in m.boundaries do
    if result.isSome then break
    let single : Nat → Option (Array (Expr × String)) := fun i =>
      if i == site.guardVariableIndex then some #[literalValue m.varType boundary] else none
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
  let notCand := mkApp (mkConst ``Not) cand0
  let proof ← withLocalDecl `h .default cand0 fun h => do
    mkLambdaFVars #[h] (mkApp targetRefutation (mkAppN h g.values))
  let refutation ← checkedProof "refutation" [] proof notCand
  return {
    refutation
    grounding := g
    refutationKind := s!"boundary_counterexample:{label}"
    boundary := some boundary
    tacticCalls := groundingTacticBudget - (← tacticBudget.get)
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

private def certifyPositive (root : Root) (op : Op) (applied : Applied) : MetaM Json := do
  let ref := root.reference
  let cand := applied.candidate
  let proof ←
    match op with
    | .p15 => finalTargetIffProof ref cand fun p => mkAppM ``Iff.symm #[p]
    | .p18 => finalTargetIffProof ref cand fun p => mkEqSymm p
    | .p14 => p14IffProof ref cand applied.site.index
    | .p23 => p23IffProof ref cand applied.site.index (Name.mkSimple applied.site.detail)
    | .pne => finalTargetIffProof ref cand fun p => mkAppM ``Ne.symm #[p]
    | .pdrg => do
        let (proof, label) ← dropGuardIffProof ref cand applied.site.index
        unless label == applied.site.detail do throwRej "pdrg_guard_proof_label_mismatch"
        pure proof
    | _ => throwRej "positive_dispatch_mismatch"
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
    | .n32 => n32Refute root cand applied.site.detail
    | .n31 => n31Refute root cand applied.site
    | _ => throwRej "negative_dispatch_mismatch"
  return Json.mkObj [
    ("label", Json.bool false),
    ("source_proof", sourceProofJson root),
    ("source_proof_check", sourceCheck.toJson),
    ("refutation", Json.mkObj [
      ("goal", Json.str "Not candidate"),
      ("kind", Json.str ev.refutationKind),
      ("check", ev.refutation.toJson),
      ("grounding", groundingJson ev.grounding (universeTag root) ev.tacticCalls),
      ("boundary", match ev.boundary with | some b => toJson b | none => Json.null)
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
  neg : Op
  transforms : Array Op
  deriving Inhabited, Repr

def squareOps : Array SquareOp := #[
  { id := squareOperationId, neg := .n25, transforms := #[] },
  { id := "SQUARE_N25_BINDER_V1", neg := .n25, transforms := #[.p14, .p23] },
  { id := "SQUARE_N32_BINDER_V1", neg := .n32, transforms := #[.p14, .p23] }]

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
  if sop.transforms.isEmpty then
    let (tP, tC) ← symmetryFor direction
    let ap ← applyOp root tP
    let ac ← applyOp rootC tC
    return (tP, tC, ap.site, ac.site, ap.candidate, ac.candidate)
  else
    let (t, ap) ← firstApplicable root sop.transforms
    let cPrime ← replayOp rootC t ap.site
    return (t, t, ap.site, ap.site, ap.candidate, cPrime)

/-- Rebuild the certified negative pair `(P, C)` for `sop.neg` and close the square with
    one preserving transform on both endpoints, requiring the exact typed diamond
    `T(N(P)) = N(T(P))`. -/
def buildSquare (root : Root) (sop : SquareOp) : MetaM SquareBuild := do
  let n ← applyOp root sop.neg
  let c ← checkedClosedProp false "candidate" n.candidate
  let direction := n.site.detail
  let rootC : Root := { root with reference := c }
  let (tP, tC, siteP, siteC, pPrimeRaw, cPrimeRaw) ← closeSquare root rootC sop direction
  let pPrime ← checkedClosedProp false "p_prime" pPrimeRaw
  let cPrime ← checkedClosedProp false "c_prime" cPrimeRaw
  let rootPPrime : Root := { root with reference := pPrime }
  let nOfT ← applyOp rootPPrime sop.neg
  unless Expr.equal nOfT.candidate cPrime do throwRej "square_diamond_expr_mismatch"
  unless nOfT.site.detail == direction do throwRej "square_diamond_direction_mismatch"
  if hasDuplicate [alphaHash root.reference, alphaHash c, alphaHash pPrime, alphaHash cPrime] then
    throwRej "square_self_pair"
  return { root, op := sop, direction, tP, tC, siteP, siteC,
           p := root.reference, c, pPrime, cPrime }

private def iffExpr (a b : Expr) : Expr := mkApp2 (mkConst ``Iff) a b

private def squareIffProof (op : Op) (site : Site) (ref cand : Expr) : MetaM Expr :=
  match op with
  | .p18 => finalTargetIffProof ref cand fun p => mkEqSymm p
  | .pne => finalTargetIffProof ref cand fun p => mkAppM ``Ne.symm #[p]
  | .p14 => p14IffProof ref cand site.index
  | .p23 => p23IffProof ref cand site.index (Name.mkSimple site.detail)
  | _ => throwRej "square_transform_dispatch"

private def squareRefute (sq : SquareBuild) : MetaM NegativeEvidence :=
  match sq.op.neg with
  | .n25 => n25Refute sq.root sq.c sq.direction
  | .n32 => n32Refute sq.root sq.c sq.direction
  | _ => throwRej "square_negative_dispatch"

/-- Direct Meta- and kernel-checked evidence for the four square rows. -/
def squareEvidence (sq : SquareBuild) : MetaM Json := do
  let params := sq.root.levelParams
  let iffPP' ← squareIffProof sq.tP sq.siteP sq.p sq.pPrime
  let checkPP' ← checkedProof "p_iff_p_prime" params iffPP' (iffExpr sq.p sq.pPrime)
  let iffP'P := mkApp3 (mkConst ``Iff.symm) sq.p sq.pPrime iffPP'
  let checkP'P ← checkedProof "p_prime_iff_p" params iffP'P (iffExpr sq.pPrime sq.p)
  let iffCC' ← squareIffProof sq.tC sq.siteC sq.c sq.cPrime
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
    ("negative_operation", Json.str sq.op.neg.id),
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
      ("grounding", groundingJson neg.grounding (universeTag sq.root) neg.tacticCalls)
    ]),
    ("p_prime_transported_proof", checkProofP'.toJson),
    ("c_prime_refutation", checkNotC'.toJson),
    ("not_iff_c_p", checkNotIffCP.toJson),
    ("not_iff_p_prime_c_prime", checkNotIffP'C'.toJson),
    ("universe_instantiation", Json.str (universeTag sq.root))
  ]

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
  let nOfT ← applyOp rootPPrime sop.neg
  match ← prerender nOfT.candidate with
  | .ok text => unless text == goals[3]! do throwRej "square_diamond_render_mismatch"
  | .error msg => throwRej s!"square_unrenderable:{msg}"
  let evidence ← squareEvidence sq
  return (sq, evidence, goals)

/-- Process one certified root into a square for `sop` and emit one evidence line. -/
def processSquare (name : Name) (sop : SquareOp) : MetaM Unit := do
  let t0 ← IO.monoMsNow
  let base := [("schema_version", toJson 1), ("kind", Json.str "square"),
    ("operation_id", Json.str sop.id),
    ("negative_operation", Json.str sop.neg.id),
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
