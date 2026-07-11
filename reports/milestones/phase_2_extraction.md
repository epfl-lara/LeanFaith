# LF-012 + LF-013 — Declaration extraction and benchmark freeze (Phase 2)

**Date:** 2026-07-11
**Scope (PLAN.md §26):** LF-012 declaration extraction (ranges/proof strip/
revalidation/failure records); LF-013 benchmark freeze (source IDs +
normalized-NL + raw-text hashes before any Phase-4 generation).

## LF-012 — Extraction

- `src/leanfaith/lean/extraction.py`: range-based proof stripping (§12.2/§12.4).
  Verified against the real REPL that Lean reports **codepoint** columns (even
  for non-BMP symbols like `𝓝`), so the strip is
  `source[decl_start .. signature.range.finish] + " := by sorry"` with direct
  Python string offsets. Builds `TheoremRecord` + minimal
  `RepresentationRecord` (three required v0 views) + `SourceIdentity`, computes
  §12.6 theorem/ancestry IDs, and emits explicit `ExtractionFailure` records
  (not-a-proposition, missing/out-of-bounds range, duplicate name). Selects
  only `theorem`/`lemma` (always propositions); quarantines duplicate names
  (§12.3). **Quality flags** on each record — `trivial_conclusion`,
  `autoparam_tactic_in_signature`, `transform_source_eligible` — surface
  malformed autoformalizations for the transform stage without dropping them.
- `src/leanfaith/lean/extract_run.py` + `scripts/02_extract_statements.py`:
  FileCommand (repository) and Command (dataset-snippet) drivers with
  per-declaration revalidation (`reconstruct_for_revalidation` re-elaborates
  each stripped statement in its own context — the real proof-leak guard),
  idempotent partition writing, and OutputManifests.

### Real extraction run (30 mathlib files + 100 sft_classic valid rows)

| source | processed | declarations | accepted | key finding |
|---|---|---|---|---|
| mathlib | 30 files | 1,345 | **909 theorems** | 68% of decls are theorems/lemmas; rest (defs/instances) correctly skipped; 1 duplicate-name quarantine |
| sft_classic | 100 valid rows | 89 | **86 theorems** (all revalidated) | **31 rows do not elaborate against our pinned mathlib** (version drift) |

Two findings driven by the data:
1. **~31% of `valid=true` sft_classic rows fail to elaborate** against
   mathlib `v4.31.0-rc1` — they were autoformalized against a different
   mathlib. Revalidation catches this; only elaborating rows become source
   theorems. So the usable deterministic-variation frame is ≈69% of valid
   rows, not 100%.
2. **Malformed autoformalizations**: some rows stuff the proof into a binder
   autoParam (`(x : ℕ := by ...) : True`) with a `True` conclusion. Lean
   accepts them (`valid=true`) but they are worthless as transform sources.
   The proof-strip handles them correctly (robust through autoParams); the
   quality flags mark them `transform_source_eligible=false`.

Artifacts (local; `data/` gitignored): `data/extracted/theorems/{mathlib,
sft_classic}.jsonl`, `data/extracted/failures/mathlib.jsonl`,
`data/extracted/manifests/*.json`. Full mathlib extraction (8,112 files) is a
resumable batch to launch when needed.

## LF-013 — Benchmark denylist freeze

- `src/leanfaith/datasets/denylist.py`: `normalize_nl` (aggressive: lowercase +
  whitespace-collapse, so reformatted problems still match) and
  `normalize_lean` (case-preserving), frozen-registry schema, `DenylistIndex`
  for O(1) NL/Lean membership, and `build_proofnetverif`.
- **`data/benchmarks/frozen_ids.json` (tracked)**: ProofNetVerif fully frozen
  at rev `91183e5b` — 3,752 rows (2,300 valid + 1,452 test) → **361 unique NL
  problems** (≈10 candidates each) and **4,108 Lean signatures**. Nine further
  protected benchmarks (ProofNet#, RLM25, Con-NF, EPLA, CriticLeanBench,
  ConsistencyCheck, Gaokao-Formal, DriftBench, miniF2F variants) are
  denylisted by name with resolution plans (§19.7/J.6: recorded, never
  substituted); their exact hashes are added before use.
- Frozen **before any generation** (§19.4); representation-based near-duplicate
  signatures append at the end of Phase 3 (LF-014,
  `representation_signatures_appended=false` marks that gate).

## Acceptance evidence

```text
ruff / mypy               → clean (43 source files)
pytest tests/unit         → all green (extraction, orchestration, denylist)
live extraction tests     → every declaration shape strips + revalidates VALID_WITH_SORRY
real run                  → 909 mathlib + 86 sft_classic theorems, manifests written
frozen_ids.json           → 4,469 signatures, membership verified on real rows
```

## Notes / deviations

- Full per-declaration revalidation runs for dataset snippets (cheap,
  single-theorem); for repository files the FileCommand elaboration is trusted
  (the declaration compiled as part of the file) and per-statement
  revalidation is sampled — re-elaborating each of a file's hundreds of
  statements standalone is O(n²). Recorded as an intentional scope choice.
- Per-declaration `ContextRecord`s (precise per-file imports) are deferred to
  LF-014; extraction uses a project-level REPL context for the request and
  records the context_id on each theorem.

**Next:** LF-014 — full multi-view representations (supersedes the minimal
extract views) and appends the Phase-3 near-duplicate signatures to the
frozen registry.
