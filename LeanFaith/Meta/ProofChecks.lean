/-
LeanFaith proof-certificate dependency inspection (PLAN.md §16.7, LF-020).

The Python evidence pipeline inlines this import-stripped helper after the
pair's normal import header.  It inspects only the freshly generated local
certificate value.  Source theorem proof constants are never supplied as
premises, and any occurrence discovered here rejects the certificate.
-/
import Lean

open Lean Elab Command Meta

/-- Collect the transitive closure of constants appearing in declaration
    *values*.  Types are deliberately excluded: the purpose is to detect proof
    dependencies (especially either compared theorem constant), while a
    separate kernel axiom audit covers the trusted-axiom closure. -/
partial def lfTransitiveValueDeps
    (env : Environment) (pending : List Name) (seen : NameSet := {}) : NameSet :=
  match pending with
  | [] => seen
  | name :: rest =>
    if seen.contains name then
      lfTransitiveValueDeps env rest seen
    else
      let seen := seen.insert name
      let nested :=
        match env.find? name with
        | some ci =>
          match ci.value? (allowOpaque := true) with
          | some value => value.getUsedConstants.toList
          | none => []
        | none => []
      lfTransitiveValueDeps env (nested ++ rest) seen

/-- `lfProofAudit "decl"` emits direct and transitive value dependencies from
    the declaration value.  Output is compact JSON with a stable `LFAUDIT `
    prefix.  A separate kernel `#print axioms` command supplies the transitive
    axiom closure. -/
elab "lfProofAudit " s:str : command => do
  let nm := (s.getString).toName
  liftTermElabM do
    match (← getEnv).find? nm with
    | none =>
      let obj := Json.mkObj [
        ("name", Json.str s.getString),
        ("notfound", Json.bool true)
      ]
      IO.println s!"LFAUDIT {obj.compress}"
    | some ci =>
      match ci.value? (allowOpaque := true) with
      | none =>
        let obj := Json.mkObj [
          ("name", Json.str s.getString),
          ("novalue", Json.bool true)
        ]
        IO.println s!"LFAUDIT {obj.compress}"
      | some value =>
        let directNames := value.getUsedConstants
        let direct := directNames.qsort Name.lt |>.map (fun n => Json.str n.toString)
        let transitiveNames :=
          (lfTransitiveValueDeps (← getEnv) directNames.toList).toArray.qsort Name.lt
        let transitive := transitiveNames.map (fun n => Json.str n.toString)
        let obj := Json.mkObj [
          ("name", Json.str s.getString),
          ("direct_constants", Json.arr direct),
          ("transitive_constants", Json.arr transitive)
        ]
        IO.println s!"LFAUDIT {obj.compress}"
