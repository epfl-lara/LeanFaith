/-
Shared `goal_v1.0` renderer for named constants and already-certified Exprs.

The Python REPR owner injects this import-stripped helper into one batched
LeanInteract request. SFT1 calls `renderClosedProp` while its reference and
candidate Exprs are both live in that request; no endpoint declaration, proof,
`sorry`, surface rendering, or text re-elaboration is involved.
-/
import Lean

namespace LeanFaith.GoalV1

open Lean Elab Command Meta

/-- All display-affecting choices are rooted in `Options.empty`.  A very wide
    page makes physical line breaks semantic only at local/target boundaries,
    rather than dependent on a caller's editor width. -/
def rendererOptions : Options :=
  Options.empty
    |>.setBool `pp.universes false
    |>.setBool `pp.coercions true
    |>.setBool `pp.notation true
    |>.setBool `pp.mvars false
    |>.setBool `pp.inaccessibleNames true
    |>.setBool `pp.implementationDetailHyps true

def universeProfileId : String := "goal_v1_first_occurrence_u_i_v1"
def universeProfileHash : String :=
  "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
def rendererSemanticHash : String :=
  "0bec5429cc0e539841208be53cd52189a7b80cbdb4649ee2d45b84bd8a5ef1fd"
def renderContextId : String := "goal_v1_render_context_v1"
def renderContextHash : String :=
  "5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"
def closedExprHashAlgorithm : String := "sha256_canonical_closed_expr_alpha_tree_v1"

structure PreparedClosedProp where
  expr : Expr
  inputLevelParams : Array Name
  canonicalLevelParams : Array Name

private def withFrozenRenderContext (action : MetaM α) : MetaM α :=
  withLCtx {} {} do
    withTransparency .default action

/-- Metadata is semantically transparent for both the frozen alpha tree and
    presentation.  Remove it recursively so metadata wrapped around or inside
    the outer telescope cannot change which binders become goal locals. -/
private partial def eraseAllMData : Expr → Expr
  | .forallE name domain body binderInfo =>
      .forallE name (eraseAllMData domain) (eraseAllMData body) binderInfo
  | .lam name domain body binderInfo =>
      .lam name (eraseAllMData domain) (eraseAllMData body) binderInfo
  | .app fn arg => .app (eraseAllMData fn) (eraseAllMData arg)
  | .letE name type value body nondep =>
      .letE name (eraseAllMData type) (eraseAllMData value) (eraseAllMData body) nondep
  | .proj typeName index base => .proj typeName index (eraseAllMData base)
  | .mdata _ body => eraseAllMData body
  | e => e

def binderInfoTag : BinderInfo → String
  | .default => "default"
  | .implicit => "implicit"
  | .strictImplicit => "strictImplicit"
  | .instImplicit => "instImplicit"

private def isSupportedStructuralArrow
    (name : Name) (body : Expr) (binderInfo : BinderInfo) : Bool :=
  binderInfo == .default && !body.hasLooseBVar 0 && (name.isAnonymous || name.hasMacroScopes)

private partial def validateOuterTelescope : Expr → MetaM Unit
  | .forallE name _ body binderInfo => do
      if isSupportedStructuralArrow name body binderInfo then
        -- A nondependent explicit anonymous/compiler-internal Pi is a
        -- structural arrow.  It remains in the target rather than becoming a
        -- generated-name local.
        pure ()
      else if name.isAnonymous then
        throwError "goal_v1_unsupported_anonymous_telescope_binder"
      else
        validateOuterTelescope body
  | _ => pure ()

/-- Open only named outer Pi binders.  Stop at a supported structural arrow so
    the goal printer retains `A → B` in the target. -/
private partial def withSupportedTelescope (e : Expr) (k : Expr → MetaM α) : MetaM α := do
  match e with
  | .forallE name domain body binderInfo =>
      if isSupportedStructuralArrow name body binderInfo then
        k e
      else
        withLocalDecl name binderInfo domain fun fvar =>
          withSupportedTelescope (body.instantiate1 fvar) k
  | _ => k e

private def prepareClosedProp (input : Expr) : MetaM PreparedClosedProp := do
  let instantiated ← instantiateMVars input
  if instantiated.hasExprMVar then
    throwError "goal_v1_unresolved_expr_mvar"
  if instantiated.hasLevelMVar then
    throwError "goal_v1_unresolved_universe_mvar"
  if instantiated.hasFVar then
    throwError "goal_v1_free_variable"
  if instantiated.hasLooseBVars then
    throwError "goal_v1_loose_bound_variable"
  if instantiated.hasSorry then
    throwError "goal_v1_sorry_expr"
  let e := eraseAllMData instantiated
  try
    check e
  catch ex =>
    if ex.isInterrupt || ex.isRuntime then
      throw ex
    throwError "goal_v1_malformed_expr"
  unless ← isProp e do
    throwError "goal_v1_not_prop"
  validateOuterTelescope e
  let inputLevelParams := (collectLevelParams {} e).params
  let canonicalLevelParams :=
    (List.range inputLevelParams.size).toArray.map fun i => Name.mkSimple s!"u_{i}"
  let levels := canonicalLevelParams.map Level.param
  return {
    expr := e.instantiateLevelParamsArray inputLevelParams levels
    inputLevelParams
    canonicalLevelParams
  }

private def renderClosedPropWithPrepared (e : Expr) : MetaM (String × PreparedClosedProp) :=
  withoutModifyingMCtx do
    withFrozenRenderContext do
      let prepared ← prepareClosedProp e
      withSupportedTelescope prepared.expr fun target => do
        let goal ← mkFreshExprMVar target MetavarKind.syntheticOpaque
        withOptions (fun _ => rendererOptions) do
          let rendered := (← ppGoal goal.mvarId!).pretty (width := 1000000)
          return (rendered, prepared)

/-- Render a closed proposition Expr. This is the sole text implementation for
    both named constants and in-session direct expressions. -/
def renderClosedProp (e : Expr) : MetaM String := do
  return (← renderClosedPropWithPrepared e).1

/-- Named constants deliberately have no second rendering implementation. -/
def renderConstantType (ci : ConstantInfo) : MetaM String :=
  renderClosedProp ci.type

private partial def levelJson : Level → Json
  | .zero => Json.mkObj [("k", Json.str "zero")]
  | .succ level => Json.mkObj [("k", Json.str "succ"), ("level", levelJson level)]
  | .max left right =>
      Json.mkObj [("k", Json.str "max"), ("left", levelJson left), ("right", levelJson right)]
  | .imax left right =>
      Json.mkObj [("k", Json.str "imax"), ("left", levelJson left), ("right", levelJson right)]
  | .param name => Json.mkObj [("k", Json.str "param"), ("name", Json.str name.toString)]
  | .mvar _ => Json.mkObj [("k", Json.str "mvar")]

/-- Frozen alpha-tree encoding used only as the Python-side SHA-256 preimage.
    Binder names and metadata are intentionally presentation-transparent;
    BinderInfo and all semantic children remain. -/
private partial def closedExprJson : Expr → Json
  | .forallE _ domain body binderInfo =>
      Json.mkObj [
        ("k", Json.str "forall"),
        ("binder_info", Json.str (binderInfoTag binderInfo)),
        ("domain", closedExprJson domain),
        ("body", closedExprJson body)
      ]
  | .lam _ domain body binderInfo =>
      Json.mkObj [
        ("k", Json.str "lambda"),
        ("binder_info", Json.str (binderInfoTag binderInfo)),
        ("domain", closedExprJson domain),
        ("body", closedExprJson body)
      ]
  | .app fn arg =>
      Json.mkObj [("k", Json.str "app"), ("fn", closedExprJson fn), ("arg", closedExprJson arg)]
  | .const name levels =>
      Json.mkObj [
        ("k", Json.str "const"),
        ("name", Json.str name.toString),
        ("levels", Json.arr (levels.toArray.map levelJson))
      ]
  | .fvar _ => Json.mkObj [("k", Json.str "fvar")]
  | .bvar index => Json.mkObj [("k", Json.str "bvar"), ("index", toJson index)]
  | .sort level => Json.mkObj [("k", Json.str "sort"), ("level", levelJson level)]
  | .lit (.natVal value) =>
      Json.mkObj [("k", Json.str "literal"), ("nat", Json.str (toString value))]
  | .lit (.strVal value) =>
      Json.mkObj [("k", Json.str "literal"), ("string", Json.str value)]
  | .mvar _ => Json.mkObj [("k", Json.str "mvar")]
  | .proj typeName index base =>
      Json.mkObj [
        ("k", Json.str "projection"),
        ("type_name", Json.str typeName.toString),
        ("index", toJson index),
        ("base", closedExprJson base)
      ]
  | .letE _ type value body nondep =>
      Json.mkObj [
        ("k", Json.str "let"),
        ("type", closedExprJson type),
        ("value", closedExprJson value),
        ("body", closedExprJson body),
        ("nondependent", Json.bool nondep)
      ]
  | .mdata _ body => closedExprJson body

/-- One direct-Expr payload. Call this for every reference/candidate while the
    certified Exprs are live in the same Meta request. -/
def renderClosedPropPayload
    (endpointId renderScopeId exprOrigin : String) (e : Expr) : MetaM Json := do
  if endpointId.isEmpty || renderScopeId.isEmpty || exprOrigin.isEmpty then
    throwError "goal_v1_closed_expr_metadata_empty"
  let (goal, prepared) ← renderClosedPropWithPrepared e
  return Json.mkObj [
    ("schema_version", toJson 1),
    ("endpoint_id", Json.str endpointId),
    ("goal_v1", Json.str goal),
    ("goal_v1_source", Json.str "closed_prop_expr"),
    ("route_id", Json.str "closed_expr_in_session"),
    ("expr_origin", Json.str exprOrigin),
    ("expr_hash_algorithm", Json.str closedExprHashAlgorithm),
    ("expr_tree", closedExprJson prepared.expr),
    ("input_level_params", Json.arr (prepared.inputLevelParams.map fun n => Json.str n.toString)),
    ("canonical_level_params",
      Json.arr (prepared.canonicalLevelParams.map fun n => Json.str n.toString)),
    ("render_scope_id", Json.str renderScopeId),
    ("universe_profile_id", Json.str universeProfileId),
    ("universe_profile_hash", Json.str universeProfileHash),
    ("renderer_semantic_hash", Json.str rendererSemanticHash),
    ("render_context_id", Json.str renderContextId),
    ("render_context_hash", Json.str renderContextHash)
  ]

def emitClosedProp
    (endpointId renderScopeId exprOrigin : String) (e : Expr) : MetaM Unit := do
  let payload ← renderClosedPropPayload endpointId renderScopeId exprOrigin e
  IO.println s!"LFGOALV1EXPRJSON {payload.compress}"

def constantKind : ConstantInfo → String
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "definition"
  | .thmInfo _ => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quotient"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"

def printNotFound (name : String) : IO Unit := do
  let payload := Json.mkObj [
    ("name", Json.str name),
    ("notfound", Json.bool true)
  ]
  IO.println s!"LFGOALV1JSON {payload.compress}"

/-- Emit the frozen goal view as one JSON payload.  The declaration name is a
    string literal so dotted and guillemet identifiers remain safe. -/
elab "lfGoalV1 " name:str : command => do
  let lookup := name.getString
  let constantName := lookup.toName
  liftTermElabM do
    match (← getEnv).find? constantName with
    | none => printNotFound lookup
    | some ci =>
      let kind := constantKind ci
      let payload ←
        if kind == "theorem" then
          let goal ← renderConstantType ci
          pure <| Json.mkObj [
            ("name", Json.str lookup),
            ("constant_kind", Json.str kind),
            ("goal_v1", Json.str goal)
          ]
        else
          pure <| Json.mkObj [
            ("name", Json.str lookup),
            ("constant_kind", Json.str kind),
            ("unsupported_kind", Json.bool true)
          ]
      IO.println s!"LFGOALV1JSON {payload.compress}"

end LeanFaith.GoalV1
