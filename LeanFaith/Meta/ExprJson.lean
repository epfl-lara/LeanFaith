/-
LeanFaith Expr → JSON serialization (PLAN.md §13.5, LF-015).

Serializes a declaration's *type* Expr to a compact JSON operator tree, from
which the Python side derives the semantic-atom and operator-tree
representation views. Working at the elaborated Expr level (not pretty-printed
text) makes the views robust to notation, comments, and naming (§12.4).

The imports are stripped and the body inlined into a batched Command by the
representation pipeline (which supplies `import Mathlib`), so every `lfDump`
call in a batch shares one environment load.
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

/-- `lfDump "Namespace.decl"` prints `LFJSON <name> <compact-json>` for the
    declaration's type, or `LFJSON <name> notfound`. The name is a string
    literal so dotted/unusual identifiers parse cleanly. -/
elab "lfDump " s:str : command => do
  let nm := (s.getString).toName
  liftTermElabM do
    match (← getEnv).find? nm with
    | some ci =>
      let obj := Json.mkObj [("name", Json.str s.getString), ("tree", ← lfExprJson ci.type)]
      IO.println s!"LFJSON {obj.compress}"
    | none =>
      let obj := Json.mkObj [("name", Json.str s.getString), ("notfound", Json.bool true)]
      IO.println s!"LFJSON {obj.compress}"
