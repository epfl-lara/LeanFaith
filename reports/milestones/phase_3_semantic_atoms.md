# LF-015 — Semantic atoms and operator tree (Phase 3)

**Date:** 2026-07-11
**Scope (PLAN.md §26):** versioned Expr-level semantic-atom and operator-tree
views with golden tests.

## Delivered

- `LeanFaith/Meta/ExprJson.lean`: a Lean meta-program that serializes a
  declaration's *type* `Expr` to a compact JSON operator tree (node kind,
  constant name, de Bruijn index, literals) via a custom `lfDump "<name>"`
  command. Working at the **elaborated Expr level**, not pretty-printed text,
  makes the views robust to notation, comments, and naming — the §12.4
  lesson applied. The helper's imports are stripped and its body inlined
  (with `import Lean` + the domain import) into a batched Command, so one
  environment load serves the whole batch.
- `src/leanfaith/representations/atoms.py` (pure):
  - `semantic_atoms` — the §13.6 ordered substantive-atom sequence
    (quantifiers, constant heads, literals) walking the tree; structural
    nodes (bvar/fvar/sort/app) are omitted (they live in the operator tree).
    The multiset (Counter of this) drives atom-diff audits: a single changed
    operator (e.g. `Eq → Ne`) shows as exactly one atom substitution.
  - `operator_tree` — the §13.5 structural tree with a node-count/depth
    summary for GTED/TransTED, versioned (`atoms_v1`).
  - `parse_lfjson_line` — parses the `LFJSON <name> <json|notfound>` emit.
- `representations/pipeline.py`: `build_representations` now also runs the
  batched Expr dump and populates `semantic_atoms` and `operator_tree`,
  completing the `repr_v1` view set. Failures mark only those two views
  `failed`; the text views are unaffected.

## Real run

- **40 mathlib theorems → 40/40 with `semantic_atoms` and `operator_tree`**.
  A sample theorem yields 23 atoms and a 105-node operator tree; the most
  common constants across the sample are the real elaborated heads —
  `AddSemigroup.toAdd`, `AddMonoid.toAddSemigroup`, `DFunLike.coe`,
  `OfNat.ofNat`, `Nat` — exactly the typeclass/instance/operator atoms
  §13.6 calls for.

## Acceptance evidence

```text
ruff / mypy               → clean (47 source files)
pytest tests/unit         → all green (atom extraction, tree stats, parsing)
live pipeline test        → semantic_atoms + operator_tree built from the
                            elaborated Expr against the fixture library
real run                  → 40/40 mathlib theorems, atoms surface real
                            typeclass/operator heads
```

## Notes / deviations

- Arrow vs universal: `A → B` is a dependent `forallE` with an unused binder,
  emitted as `forall` like `∀`. Distinguishing them needs per-binder bvar
  usage analysis; deferred as a v1 refinement (the atom multiset still
  detects any change to the binder count).
- `alpha_structural` and `notation_light` remain `not_attempted`
  (`alpha_structural` is optional until M5; `notation_light` optional). All
  §13.2 v0-required views plus `signature_explicit`, `semantic_atoms`, and
  `operator_tree` are now produced — the representation layer is complete for
  the transform engine's needs.
- `ExprJson.lean` is inlined into Commands rather than built as a Lake
  library, so it needs no separate mathlib-dependent build; it is version-
  controlled as the canonical helper source under the declared `LeanFaith/`
  tree.

**Next:** LF-016 — the deterministic transformation protocol
(Applicability/VariantDraft/Audit/registry/promotion), the engine that
generates the matching/mismatched training pairs from these represented
source theorems.
