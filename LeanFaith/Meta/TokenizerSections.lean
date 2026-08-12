/-
LeanFaith tokenizer-section derivation (PLAN.md Revision 4.1, section 13.7).

This helper classifies the outer telescope of one declaration type inside
Lean's meta monad.  It deliberately does not guess from pretty-printed text:

* an `instImplicit` domain is an instance/typeclass binder;
* every other domain for which `Meta.isProp` succeeds is a proposition
  hypothesis;
* every remaining domain is an ordinary binder;
* the telescope body is the conclusion.

The emitted text is an option-isolated rendering used only to measure the
frozen section budget.  The content-addressed Python derivation artifact is
private when its input denominator contains private source records.
-/
import Lean

namespace LeanFaith.TokenizerSectionsV1

open Lean Elab Command Meta

def lfTokenizerBinderInfoTag : BinderInfo -> String
  | .default => "default"
  | .implicit => "implicit"
  | .strictImplicit => "strictImplicit"
  | .instImplicit => "instImplicit"

def lfTokenizerPp (e : Expr) : MetaM String := do
  let options := Options.empty
    |>.setBool `pp.all false
    |>.setBool `pp.fullNames true
    |>.setBool `pp.proofs false
    |>.setBool `pp.proofs.withType false
    |>.setBool `pp.mvars false
    |>.setBool `pp.explicit true
    |>.setBool `pp.universes true
  withOptions (fun _ => options) do
    return (← ppExpr e).pretty

def lfTokenizerBinderText (fvar : Expr) (bi : BinderInfo) : MetaM String := do
  let name := (← getFVarLocalDecl fvar).userName
  let typeText ← lfTokenizerPp (← inferType fvar)
  let body := s!"{name} : {typeText}"
  return match bi with
    | .default => s!"({body})"
    | .implicit => "{" ++ body ++ "}"
    | .strictImplicit => "{{" ++ body ++ "}}"
    | .instImplicit => s!"[{body}]"

partial def lfTokenizerSections
    (type : Expr) (ordinal : Nat := 0) : MetaM (List Json × String) := do
  match type with
  | .forallE _ domain body bi =>
    let canonicalName := Name.mkSimple s!"b_{ordinal}"
    withLocalDecl canonicalName bi domain fun fvar => do
      let text ← lfTokenizerBinderText fvar bi
      let domainIsProp ← isProp domain
      let kind :=
        if bi == .instImplicit then "instance_binder"
        else if domainIsProp then "prop_hypothesis"
        else "ordinary_binder"
      let tail ← lfTokenizerSections (body.instantiate1 fvar) (ordinal + 1)
      let current := Json.mkObj [
        ("ordinal", ordinal),
        ("kind", kind),
        ("binder_info", lfTokenizerBinderInfoTag bi),
        ("domain_is_prop", domainIsProp),
        ("text", text)
      ]
      return (current :: tail.1, tail.2)
  | conclusion =>
    return ([], ← lfTokenizerPp conclusion)

def lfTokenizerPrintNotFound (name : String) : IO Unit := do
  let obj := Json.mkObj [
    ("name", Json.str name),
    ("method_version", Json.str "lean_meta_tokenizer_sections_v1"),
    ("notfound", Json.bool true)
  ]
  IO.println s!"LFTOKSECTIONSJSON {obj.compress}"

elab "lfDumpTokenizerSections " s:str : command => do
  let nm := (s.getString).toName
  liftTermElabM do
    match (← getEnv).find? nm with
    | some ci =>
      let canonicalType := ci.type.instantiateLevelParams ci.levelParams <|
        (List.range ci.levelParams.length).map fun i => Level.param (Name.mkSimple s!"u_{i}")
      let sections ← lfTokenizerSections canonicalType
      let obj := Json.mkObj [
        ("name", Json.str s.getString),
        ("method_version", Json.str "lean_meta_tokenizer_sections_v1"),
        ("sections", Json.mkObj [
          ("units", Json.arr sections.1.toArray),
          ("conclusion", Json.str sections.2)
        ])
      ]
      IO.println s!"LFTOKSECTIONSJSON {obj.compress}"
    | none => lfTokenizerPrintNotFound s.getString

end LeanFaith.TokenizerSectionsV1
