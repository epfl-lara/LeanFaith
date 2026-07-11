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

/-- Serialize an `Expr` to a compact JSON tree of node kinds, constant names,
    de Bruijn indices, and literals. Implicit/instance arguments are retained
    (they carry the elaborated content); pruning is a Python-side concern. -/
partial def lfExprJson (e : Expr) : MetaM Json := do
  match e with
  | .forallE _ d b _ =>
    return Json.mkObj [("k", "forall"), ("dom", ← lfExprJson d), ("body", ← lfExprJson b)]
  | .lam _ d b _ =>
    return Json.mkObj [("k", "lam"), ("dom", ← lfExprJson d), ("body", ← lfExprJson b)]
  | .app f a =>
    return Json.mkObj [("k", "app"), ("fn", ← lfExprJson f), ("arg", ← lfExprJson a)]
  | .const n _ => return Json.mkObj [("k", "const"), ("n", n.toString)]
  | .fvar _ => return Json.mkObj [("k", "fvar")]
  | .bvar i => return Json.mkObj [("k", "bvar"), ("i", i)]
  | .sort _ => return Json.mkObj [("k", "sort")]
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
    | some ci => IO.println s!"LFJSON {s.getString} {(← lfExprJson ci.type).compress}"
    | none => IO.println s!"LFJSON {s.getString} notfound"
