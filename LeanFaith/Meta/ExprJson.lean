/-
LeanFaith Expr → JSON serialization (PLAN.md §13.5, LF-015).

Serializes a declaration's *type* Expr to a compact JSON operator tree, from
which the Python side derives the semantic-atom and operator-tree
representation views. Working at the elaborated Expr level (not pretty-printed
text) makes the views robust to notation, comments, and naming (§12.4).

The imports are stripped and the body inlined into a batched Command by the
representation pipeline (which supplies `import Mathlib`), so every `lfDump`
call in a batch shares one environment load.

Private declarations use three independent commands so a failure in one view
cannot erase the others:

* `lfDumpSignaturePP "name"` emits `LFSIGPPJSON {...}`;
* `lfDumpSignatureExplicit "name"` emits `LFSIGEXPLICITJSON {...}`;
* `lfDumpTree "name"` emits `LFTREEJSON {...}`.

The legacy combined `lfDump`/`LFJSON` command remains available for replaying
older callers while they migrate to the independent messages.
-/
import Lean

open Lean Elab Command Meta

/-- Stable binder metadata without depending on a `ToString BinderInfo`
    instance (which is not provided by all supported Lean releases). -/
def lfBinderInfoTag : BinderInfo → String
  | .default => "default"
  | .implicit => "implicit"
  | .strictImplicit => "strictImplicit"
  | .instImplicit => "instImplicit"

/-- Serialize an `Expr` to a compact JSON tree of node kinds, constant names,
    de Bruijn indices, and literals. Implicit/instance arguments are retained
    (they carry the elaborated content); pruning is a Python-side concern. -/
partial def lfExprJson (e : Expr) : MetaM Json := do
  match e with
  | .forallE _ d b bi =>
    return Json.mkObj [("k", "forall"), ("bi", lfBinderInfoTag bi),
                       ("dom", ← lfExprJson d), ("body", ← lfExprJson b)]
  | .lam _ d b bi =>
    return Json.mkObj [("k", "lam"), ("bi", lfBinderInfoTag bi),
                       ("dom", ← lfExprJson d), ("body", ← lfExprJson b)]
  | .app f a =>
    return Json.mkObj [("k", "app"), ("fn", ← lfExprJson f), ("arg", ← lfExprJson a)]
  | .const n us =>
    return Json.mkObj [("k", "const"), ("n", n.toString), ("us", toString us)]
  | .fvar _ => return Json.mkObj [("k", "fvar")]
  | .bvar i => return Json.mkObj [("k", "bvar"), ("i", i)]
  | .sort u => return Json.mkObj [("k", "sort"), ("u", toString u)]
  | .lit (.natVal v) => return Json.mkObj [("k", "lit"), ("nat", toString v)]
  | .lit (.strVal s) => return Json.mkObj [("k", "lit"), ("str", s)]
  | .mvar _ => return Json.mkObj [("k", "mvar")]
  | .proj s i b =>
    return Json.mkObj [("k", "proj"), ("s", s.toString), ("i", i), ("base", ← lfExprJson b)]
  | .letE _ t v b _ =>
    return Json.mkObj [("k", "let"), ("t", ← lfExprJson t), ("v", ← lfExprJson v),
                       ("body", ← lfExprJson b)]
  | .mdata _ b => lfExprJson b

/-- Pretty-print a declaration type with the exact LeanFaith signature-view
    options. This works for private environment names that cannot be addressed
    by Lean syntax and therefore cannot be inspected with `#check`. -/
def lfPpType (type : Expr) (explicit universes : Bool) : MetaM String := do
  -- Start from an empty option map so ambient `set_option pp.*` commands in
  -- inline dataset sources cannot change representation bytes. Unset options
  -- use Lean's registered defaults; non-default representation choices and
  -- stability-sensitive metavariable behavior are explicit below.
  let options := Options.empty
    |>.setBool `pp.all false
    |>.setBool `pp.fullNames true
    |>.setBool `pp.proofs false
    |>.setBool `pp.proofs.withType false
    |>.setBool `pp.mvars false
    |>.setBool `pp.explicit explicit
    |>.setBool `pp.universes universes
  withOptions (fun _ => options) do
    return (← ppExpr type).pretty

/-- Replace a declaration's source universe-parameter names with the
    deterministic `u_0`, `u_1`, ... names used by LeanFaith's reusable
    proposition signatures.

    Direct environment pretty-printing otherwise preserves names such as `u`
    from the source module. Those names are not bound inside the declaration
    type and therefore cannot be re-elaborated as standalone proposition text.
    `#check` freshens public declarations similarly; this function gives the
    private environment-only path a stable, explicitly recoverable spelling. -/
def lfCanonicalType (ci : ConstantInfo) : Expr :=
  let levels := (List.range ci.levelParams.length).map fun i =>
    Level.param (Name.mkSimple s!"u_{i}")
  ci.type.instantiateLevelParams ci.levelParams levels

/-- Emit a not-found payload under the view-specific message tag. -/
def lfPrintNotFound (tag name : String) : IO Unit := do
  let obj := Json.mkObj [("name", Json.str name), ("notfound", Json.bool true)]
  IO.println s!"{tag} {obj.compress}"

/-- Independently emit the normal private signature view. -/
elab "lfDumpSignaturePP " s:str : command => do
  let nm := (s.getString).toName
  liftTermElabM do
    match (← getEnv).find? nm with
    | some ci =>
      let signature ← lfPpType (lfCanonicalType ci) false false
      let obj := Json.mkObj [
        ("name", Json.str s.getString),
        ("signature_pp", Json.str signature)
      ]
      IO.println s!"LFSIGPPJSON {obj.compress}"
    | none => lfPrintNotFound "LFSIGPPJSON" s.getString

/-- Independently emit the fully explicit private signature view. -/
elab "lfDumpSignatureExplicit " s:str : command => do
  let nm := (s.getString).toName
  liftTermElabM do
    match (← getEnv).find? nm with
    | some ci =>
      let signature ← lfPpType (lfCanonicalType ci) true true
      let obj := Json.mkObj [
        ("name", Json.str s.getString),
        ("signature_explicit", Json.str signature)
      ]
      IO.println s!"LFSIGEXPLICITJSON {obj.compress}"
    | none => lfPrintNotFound "LFSIGEXPLICITJSON" s.getString

/-- Independently emit the elaborated expression-tree view. -/
elab "lfDumpTree " s:str : command => do
  let nm := (s.getString).toName
  liftTermElabM do
    match (← getEnv).find? nm with
    | some ci =>
      let obj := Json.mkObj [
        ("name", Json.str s.getString),
        ("tree", ← lfExprJson ci.type)
      ]
      IO.println s!"LFTREEJSON {obj.compress}"
    | none => lfPrintNotFound "LFTREEJSON" s.getString

/-- `lfDump "Namespace.decl"` prints `LFJSON <name> <compact-json>` for the
    declaration's type and pinned pretty-printed signatures, or an object with
    `notfound=true`. The name is a string literal so dotted/unusual identifiers
    parse cleanly. -/
elab "lfDump " s:str : command => do
  let nm := (s.getString).toName
  liftTermElabM do
    match (← getEnv).find? nm with
    | some ci =>
      let canonicalType := lfCanonicalType ci
      let signaturePP ← lfPpType canonicalType false false
      let signatureExplicit ← lfPpType canonicalType true true
      let obj := Json.mkObj [
        ("name", Json.str s.getString),
        ("tree", ← lfExprJson ci.type),
        ("signature_pp", Json.str signaturePP),
        ("signature_explicit", Json.str signatureExplicit)
      ]
      IO.println s!"LFJSON {obj.compress}"
    | none =>
      let obj := Json.mkObj [("name", Json.str s.getString), ("notfound", Json.bool true)]
      IO.println s!"LFJSON {obj.compress}"
