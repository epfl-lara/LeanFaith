/-
Two-row SFT1 smoke helper.

This file is injected without its import command into one Mathlib Meta
request.  It reuses the frozen Wave1 engine for P18 and implements only the
exact hand-written N31 canary authorized for this smoke.  It declares no
endpoint theorem or axiom and never renders or re-elaborates a candidate.
-/
import Mathlib

namespace LeanFaith.SFT1.ThinSmoke

open Lean Elab Meta

def sourceVersion : String := "sft1_thin_smoke_v1"

private def checkedClosedProp (origin : String) (e : Expr) : MetaM Expr := do
  let e ← instantiateMVars e
  if e.hasExprMVar || e.hasLevelMVar || e.hasFVar || e.hasLooseBVars || e.hasSorry then
    throwError m!"{origin}: proposition is not closed and placeholder-free"
  check e
  unless ← isProp e do
    throwError m!"{origin}: expected Prop"
  return e

private def checkedProof (origin : String) (proof expected : Expr) : MetaM Expr := do
  let proof ← instantiateMVars proof
  if proof.hasExprMVar || proof.hasLevelMVar || proof.hasFVar ||
      proof.hasLooseBVars || proof.hasSorry then
    throwError m!"{origin}: proof is not closed and placeholder-free"
  check proof
  let actual ← inferType proof
  unless ← withoutModifyingMCtx <| isDefEq actual expected do
    throwError m!"{origin}: proof has the wrong type"
  return proof

private def elaborateClosedProp (stx : Syntax) : MetaM Expr := do
  let e ← Term.TermElabM.run' do
    let e ← Term.elabTerm stx (some (mkSort .zero))
    Term.synthesizeSyntheticMVarsNoPostponing
    instantiateMVars e
  checkedClosedProp "hand-written N31 reference" e

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

private def elaborateN31CanaryReference : MetaM Expr := do
  elaborateClosedProp (← `(∀ (n : Nat) (hn : n = 0), n + 1 = 1))

private def elaborateProof (origin : String) (stx : Syntax) (expected : Expr) : MetaM Expr := do
  let proof ← Term.TermElabM.run' do
    let proof ← Term.elabTerm stx (some expected)
    Term.synthesizeSyntheticMVarsNoPostponing
    instantiateMVars proof
  checkedProof origin proof expected

structure PositiveResult where
  reference : Expr
  candidate : Expr
  certificate : LeanFaith.SFT1.Wave1.Certificate
  referenceProofHash : UInt64

def buildPositive : MetaM PositiveResult := do
  let some info := (← getEnv).find? ``Nat.lor_comm
    | throwError "thin smoke: Mathlib theorem Nat.lor_comm not found"
  let theoremInfo ←
    match info with
    | .thmInfo value => pure value
    | _ => throwError "thin smoke: Nat.lor_comm is not a theorem"
  let reference ← checkedClosedProp "Nat.lor_comm type" theoremInfo.type
  let referenceProof ← checkedProof "Nat.lor_comm proof" theoremInfo.value reference
  let context : LeanFaith.SFT1.Wave1.DispatchContext := {}
  let discovered ← LeanFaith.SFT1.Wave1.discover
    .p18SymmetrizeEquality context reference
  unless discovered.size == 1 do
    throwError m!"thin smoke: expected one P18 site, found {discovered.size}"
  let selected := discovered[0]!
  let applied ← LeanFaith.SFT1.Wave1.dispatchAt
    selected.operation selected.selector context reference
  let applied ←
    match applied with
    | .applicable value => pure value
    | .typedNotApplicable _ => throwError "thin smoke: discovered P18 site did not reapply"
  unless Expr.equal applied.candidate selected.candidate &&
      applied.certificate == selected.certificate do
    throwError "thin smoke: P18 discovery/application mismatch"
  match applied.certificate with
  | .p18 _ => pure ()
  | _ => throwError "thin smoke: P18 returned the wrong certificate class"
  let replay ← LeanFaith.SFT1.Wave1.replayCertificate
    context reference applied.candidate applied.certificate
  unless replay.passed do
    throwError "thin smoke: P18 certificate replay failed"
  return {
    reference
    candidate := applied.candidate
    certificate := applied.certificate
    referenceProofHash := hash referenceProof
  }

structure N31CanaryCertificate where
  canaryId : String
  guardOrdinal : Nat
  guardSite : String
  binderName : Name
  binderInfo : BinderInfo
  construction : String
  deriving BEq, Inhabited, Repr

structure NegativeResult where
  reference : Expr
  candidate : Expr
  certificate : N31CanaryCertificate
  referenceProofHash : UInt64
  candidateRefutationHash : UInt64
  witnessRefutationHash : UInt64

private def discoverN31Canary (source : Expr) : MetaM N31CanaryCertificate := do
  let source ← checkedClosedProp "N31 canary source" source
  let exactSource ← elaborateN31CanaryReference
  unless Expr.equal (eraseMDataDeep source) (eraseMDataDeep exactSource) do
    throwError "thin smoke: source is not the exact authorized N31 canary"
  match source with
  | .forallE nName nType body .default =>
      unless nName.eraseMacroScopes.toString == "n" &&
          (← withoutModifyingMCtx <| isDefEq nType (mkConst ``Nat)) do
        throwError "thin smoke: N31 canary outer Nat binder mismatch"
      match body with
      | .forallE guardName guardType target .default =>
          unless guardName.eraseMacroScopes.toString == "hn" && (← isProp guardType) do
            throwError "thin smoke: N31 canary guard mismatch"
          if target.hasLooseBVar 0 then
            throwError "thin smoke: N31 canary guard is used by the continuation"
          return {
            canaryId := "n31_nat_eq_zero_add_one_v1"
            guardOrdinal := 1
            guardSite := "/bindingBody"
            binderName := guardName
            binderInfo := .default
            construction := "lowerLooseBVars_1_1"
          }
      | _ => throwError "thin smoke: N31 canary has no named guard binder"
  | _ => throwError "thin smoke: N31 canary has no outer Nat binder"

private def applyN31Canary
    (source : Expr) (certificate : N31CanaryCertificate) : MetaM Expr := do
  let rediscovered ← discoverN31Canary source
  unless rediscovered == certificate do
    throwError "thin smoke: N31 canary certificate rediscovery mismatch"
  match source with
  | .forallE nName nType (.forallE _ _ target .default) .default =>
      let candidate := .forallE nName nType (target.lowerLooseBVars 1 1) .default
      let candidate ← checkedClosedProp "N31 canary candidate" candidate
      if Expr.equal source candidate ||
          (← withoutModifyingMCtx <| isDefEq source candidate) then
        throwError "thin smoke: N31 mutation collapsed or remained definitionally equal"
      return candidate
  | _ => throwError "thin smoke: N31 canary changed after discovery"

private def replayN31Canary
    (source candidate : Expr) (certificate : N31CanaryCertificate) : MetaM Bool := do
  let replayed ← applyN31Canary source certificate
  return Expr.equal replayed candidate

def buildNegative : MetaM NegativeResult := do
  let reference ← elaborateN31CanaryReference
  let certificate ← discoverN31Canary reference
  let candidate ← applyN31Canary reference certificate
  unless ← replayN31Canary reference candidate certificate do
    throwError "thin smoke: N31 canary certificate replay failed"
  let referenceProof ← elaborateProof "N31 reference proof"
    (← `(by
      intro n hn
      subst n
      rfl)) reference
  let notCandidate := mkApp (mkConst ``Not) candidate
  let candidateRefutation ← elaborateProof "N31 candidate refutation"
    (← `(by
      intro h
      have bad : (1 : Nat) + 1 = 1 := h 1
      omega)) notCandidate
  let witnessGoal ←
    match candidate with
    | .forallE _ _ body .default => pure (body.instantiate1 (mkNatLit 1))
    | _ => throwError "thin smoke: N31 candidate lost its witness binder"
  let witnessRefutation ← elaborateProof "N31 witness refutation"
    (← `(by decide)) (mkApp (mkConst ``Not) witnessGoal)
  return {
    reference
    candidate
    certificate
    referenceProofHash := hash referenceProof
    candidateRefutationHash := hash candidateRefutation
    witnessRefutationHash := hash witnessRefutation
  }

def emitEvidence (positive : PositiveResult) (negative : NegativeResult) : MetaM Unit := do
  let payload := Json.mkObj [
    ("schema_version", toJson 1),
    ("positive", Json.mkObj [
      ("operation_id", Json.str "P18_SYMMETRIZE_EQUALITY_V1"),
      ("source_theorem", Json.str "Nat.lor_comm"),
      ("selected_site", Json.str "outer_target"),
      ("certificate", Json.mkObj [
        ("kind", Json.str "p18"),
        ("target_site", Json.str "outer_target")
      ]),
      ("certificate_replay", Json.bool true),
      ("candidate_elaboration", Json.str "valid_closed_prop"),
      ("reference_proof", Json.str "loaded_mathlib_theorem"),
      ("reference_proof_expr_hash_u64", Json.str positive.referenceProofHash.toString)
    ]),
    ("negative", Json.mkObj [
      ("operation_id", Json.str "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"),
      ("proof_evidence_operation_id", Json.str "N31_DROP_REQUIRED_GUARD_PROOF_V1"),
      ("selected_site", Json.str negative.certificate.guardSite),
      ("certificate", Json.mkObj [
        ("kind", Json.str "n31_canary_guard_drop_v1"),
        ("canary_id", Json.str negative.certificate.canaryId),
        ("guard_ordinal", toJson negative.certificate.guardOrdinal),
        ("binder_name", Json.str negative.certificate.binderName.eraseMacroScopes.toString),
        ("binder_info", Json.str "default"),
        ("construction", Json.str negative.certificate.construction)
      ]),
      ("certificate_replay", Json.bool true),
      ("candidate_elaboration", Json.str "valid_closed_prop"),
      ("reference_proof", Json.str "kernel_checked"),
      ("reference_proof_expr_hash_u64", Json.str negative.referenceProofHash.toString),
      ("candidate_truth", Json.str "refuted"),
      ("counterexample_witness", Json.str "(1 : Nat)"),
      ("candidate_refutation_expr_hash_u64", Json.str negative.candidateRefutationHash.toString),
      ("witness_refutation_expr_hash_u64", Json.str negative.witnessRefutationHash.toString),
      ("smoke_only", Json.bool true),
      ("general_n31_bank_activated", Json.bool false)
    ])
  ]
  IO.println s!"LFSFT1SMOKEJSON {payload.compress}"

end LeanFaith.SFT1.ThinSmoke
