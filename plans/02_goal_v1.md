# REPR — shared `goal_v1.0` theorem representation

> **Task ID:** REPR
> **Status:** not_started
> **Owner/session:** unassigned
> **Last updated:** 2026-08-30
> **Dependencies:** none
> **Next gate:** freeze examples and round-trip/compile-context contract before downstream serializers
> **Compute class:** CPU; bounded Lean oracle/renderer tests only
> **Lean budget:** reuse loaded environments and candidate compilation; no corpus-wide rendering compile
> **Local staging root:** `/storage/milikic/leanfaith/value_first/goal_v1_0/`
> **HF destination:** none; the serializer version is embedded in downstream manifests

## Objective

Own the one canonical model-facing theorem representation so SFT1, SFT2A, SFT2B, and EVAL do not
build incompatible renderers. Freeze `goal_v1.0`, its provenance flag, and the separate raw
compilation context. This is an enabling task, not a corpus-generation task.

## Frozen representation decision

`goal_v1.0` contains ordered local variables, hypotheses, typeclass/universe locals, and exactly
one `⊢ target`. It removes declaration name/kind, attributes, command shell, imports, options,
comments, `:=`, `by`, `sorry`, and proof body. Preserve local names/order, dependent types,
generated instance names, coercions, notation, universes, and meaningful line boundaries.

Use the best cheap source available:

1. `elaborated`: render the theorem type from an environment that is already loaded or from the
   candidate compilation already required by SFT2. Do not recompile proofs.
2. `surface`: deterministic Lean-aware extraction from a trusted headless/signature string when
   elaboration is unavailable or would trigger bulk compilation.

Both yield the same textual grammar and store `goal_v1_source: elaborated|surface`. Never silently
mix them without the sidecar flag; report coverage and model metrics by source mode. Ambiguous
surface rows fail closed or retain the raw representation outside the core view.

`goal_v1` is model-facing, not a compilable source language. Every source/candidate sidecar retains
`raw_statement` or `raw_source`, `project_id`, project/toolchain revision, import header,
namespaces/scopes/options, and renderer version as `compile_context`. SFT2 proposers/formalizers
must return a compilable declaration/signature; compilation never tries to reconstruct source from
goal text. This avoids an under-specified inverse transformation.

Do not alpha-normalize model text. A typed/alpha-normalized fingerprint may be a separate dedup key.
Filter theorem/lemma declarations before serialization because declaration kind is absent afterward.

## Scope and ownership

**In scope:** versioned spec/examples, surface parser/serializer, Lean-side elaborated renderer,
provenance and compile-context schema, normalization/fingerprint hooks, fixtures/tests, and a small
cross-source oracle.

**Out of scope:** bulk corpus rendering, semantic labels, theorem transformations, proof
compilation, LLM calls, replacing raw source fields, or supporting `def` declarations in v1.0.

**Writable paths:** this brief; `src/leanfaith/representations/goal_v1.py`;
`LeanFaith/Meta/GoalV1.lean`; `configs/representations/goal_v1_v1.yaml`;
`tests/unit/representations/` and `tests/integration/leaninteract/test_goal_v1_live.py`. Existing
representation/backend modules are shared/read-only; request coordinator changes if integration
cannot be achieved additively.

## Input and output contract

Renderer input includes declaration kind, signature/type or elaborated `Expr`, and optional compile
context. Output sidecar record:

```text
representation_id, goal_v1, goal_v1_source, renderer_version,
raw_statement_hash, declaration_kind, compile_context_id,
typed_alpha_fingerprint?, warnings[]
```

Downstream core rows include only the `goal_v1` text in `reference`/`candidate`; downstream
manifests and keyed sidecars retain this record and the raw compilable material.

## Lean-efficiency plan

Lean is the bottleneck. Build/test surface serialization without Lean. For elaborated fixtures,
load each pinned project once and render many `ConstantInfo.type` values from the existing
environment; never recompile theorem proofs or run one Lean process per theorem. SFT2 reuses the
renderer during compilation already required for each candidate. Cache by raw/type hash plus
project/toolchain/options and renderer version.

## Execution gates

### One-example smoke

Render the same simple theorem through elaborated and surface paths, compare the frozen text,
serialize its compile context, and prove no declaration shell/proof leaks. Show downstream code can
join the core text to raw compilable source without attempting a goal-to-source inverse.

### Pilot

Use a bounded suite spanning Mathlib, Physlib, CSLib, compiler-style source, canonical gold, and
ConsistencyCheck examples. Include dependent/shadowed binders, generated instances, universes,
coercions, multiline goals, helper declarations, comments/strings, and final-`def` rejection.
Report exact elaborated/surface agreement, fallback/failure rates, deterministic hashes, cache
behavior, throughput, and raw-text leakage.

### Freeze

Publish the spec/config/fixtures and version hash in the repository. Downstream task manifests pin
that hash. Later representational experiments create `goal_v1.1` or a new view; they do not mutate
v1.0.

## Acceptance criteria

- One owner and one versioned serializer serve every SFT/evaluation task.
- The output has ordered locals plus exactly one turnstile and no shell/name/proof leakage.
- Elaborated and surface modes are explicit, measured, and never require mass Lean compilation.
- Raw compilable source/context is retained separately; no inverse from goal text is assumed.
- Tests cover difficult binders, environments, multiline output, determinism, and theorem-only
  filtering.

## Session kickoff prompt

```text
Own only REPR in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, and plans/02_goal_v1.md completely. Update this brief and claim only
its listed paths. Freeze goal_v1.0 with one elaborated renderer and one tagged surface fallback.
Lean is the bottleneck: do not compile a corpus; reuse loaded environments and bounded fixtures.
Preserve raw compilable source plus project/import/options context in sidecars, and never invent an
inverse from goal text. Run the one-example cross-path smoke, then the bounded multi-source pilot.
Do not generate training data. Record the spec hash, evidence, risks, and downstream handoff.
```

## Coordinator requests

- None yet. Downstream SFT/evaluation serializers wait for the frozen `goal_v1.0` hash.

## Progress log (append-only)

- 2026-08-30 — task created with the hybrid elaborated/surface decision; no renderer implemented.
