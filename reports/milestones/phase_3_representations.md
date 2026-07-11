# LF-014 — Multi-view representations (Phase 3)

**Date:** 2026-07-11
**Scope (PLAN.md §26):** required views/statuses/hashes/option profile;
append representation-based near-duplicate signatures to the benchmark
registry (§19.4).

## Delivered

- `src/leanfaith/representations/views.py` (pure):
  - `normalize_headless` — the §13.2 headless view: strips attributes,
    modifiers, keyword, declaration name, comments, and the proof tail
    (`:= by sorry` / `:= sorry` / bare `:=`), collapsing whitespace to a
    renaming-invariant `(binders) : conclusion` skeleton.
  - `parse_check_type` — extracts the elaborated type from a `#check`
    message, tolerating both `@name : …` and `name : …` (Lean drops the `@`
    when every binder is explicit) and stripping `.{universe}` annotations.
  - Pinned pp options (§13.4): `PP_SIGNATURE_INLINE` (full names, no proofs)
    and `PP_EXPLICIT_INLINE` (explicit, universes, full names) — verified
    that LeanInteract's declaration extractor ignores ambient `set_option`,
    so views are obtained via `#check @name` under these pins instead.
  - Content and near-duplicate hashing.
- `src/leanfaith/representations/pipeline.py`: `build_representations` builds
  `repr_v1` `RepresentationRecord`s for a batch of pre-existing declarations,
  running two **batched** `#check` commands (one per option set, so the
  environment loads once per batch) and assembling the required v0 views plus
  `signature_explicit`. Unresolvable declarations mark only the elaborated
  views `failed`; the source-derived views still succeed.
- `datasets/denylist.py`: `append_representation_signatures` additively
  attaches representation near-duplicate hashes to a frozen benchmark and sets
  `representation_signatures_appended` (§19.4: additive, never a rewrite);
  `DenylistIndex` now also indexes representation hashes.

## Real run

- **120 extracted mathlib theorems → 120 `repr_v1` records**, every one with
  `signature_pp` and `signature_explicit` populated (120/120 ok), 120 unique
  near-duplicate signatures (no collisions in-sample). Handles real mathlib
  shapes: universe-polymorphic (`.{u_1, u_2, u_3}`), typeclass/instance
  binders, `FunLike` coercions — `signature_explicit` shows them all.
  Artifacts: `data/representations/mathlib.jsonl` + manifest.
- **§19.4 append executed**: 348 headless reference signatures from
  ProofNetVerif (`lean4_formalization`) appended to the tracked
  `frozen_ids.json`; 130 references skipped (non-`theorem` heads: `def`/
  `abbrev`/malformed), which is expected and auditable.
  `representation_signatures_appended=true`; denylist now holds 4,817
  signatures. This gate closes **before** any Phase-4 generation.

## Acceptance evidence

```text
ruff / mypy               → clean (46 source files)
pytest tests/unit         → all green (views, parsing, hashing, registry append)
live pipeline test        → repr_v1 views built + parsed against the fixture library
real run                  → 120 mathlib repr_v1 records; frozen registry appended
```

## Notes / deviations

- `alpha_structural`, `notation_light`, `semantic_atoms`, and `operator_tree`
  remain `not_attempted` in `repr_v1` — they need Expr-level analysis
  (`semantic_atoms`/`operator_tree` are LF-015; `alpha_structural` is optional
  until M5). The three §13.2-required v0 views plus `signature_explicit` are
  produced.
- Representation building targets pre-existing declarations (mathlib) via
  `#check @name`, avoiding per-file import-context reconstruction. sft_classic
  representations reuse the same builder after re-declaring each snippet in
  its own context — wired the same way, run when that source is promoted into
  the transform pool.
- Benchmark representation signatures use the pure headless normalization
  (name-independent, no elaboration), robust for reference statements; an
  elaborated-signature pass over benchmark rows is a later refinement.

**Next:** LF-015 — semantic atoms and operator tree (Expr-level views, the
last representation pieces before the transformation engine, LF-016).
