# REPR — shared `goal_v1.0` theorem representation

> **Task ID:** REPR
> **Status:** complete
> **Owner/session:** Codex REPR session (2026-08-30)
> **Last updated:** 2026-08-30 (second reviewed freeze)
> **Dependencies:** none
> **Next gate:** downstream manifests pin the second replacement spec hash and implementation
> revision; retain raw/context sidecars and report metrics by `goal_v1_source`
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

## Repair plan

1. Finish all safe string parsing, schema validation, source filtering, hashing, and fixtures before
   invoking Lean.
2. Freeze the tagged surface fallback and sidecar/compile-context contract with unit tests, including
   theorem/lemma filtering and fail-closed ambiguity handling.
3. Add one elaborated renderer behind the existing central Lean protocol boundary, reuse one loaded
   environment for bounded fixtures, and never invoke one Lean process per declaration.
4. Run one complete cross-path example first, then only the bounded multi-source pilot; record
   agreement, failures, cache behavior, throughput, leakage checks, hashes, risks, and handoff.
5. Fail the whole elaborated batch when Lean reports any sorry and `allow_sorry=False`, including
   mixed `INVALID` batches; retain partial payload-backed successes only when no sorry was reported.
6. Canonicalize supported semicolon-delimited top-level `let` propositions to one target line on
   both paths; fail closed on ambiguous surface layout and unsupported elaborated multiline layout.

Lean is the bottleneck: this session will not compile a corpus. It will establish the small Lean
oracle, measure the cheap renderer against it, and compile only the bounded audit required here.

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

## Superseded candidate freeze record and evidence

The record below describes the rejected pre-review candidate and is retained as historical evidence.
It is not an active freeze or a downstream authorization. The replacement freeze record must use the
post-repair spec and committed code revision.

- **Frozen version:** `goal_v1.0`; renderer version `goal_v1.0`; canonical spec hash
  `690d57348e2098dd7aeda10eede6cec9182aa002eb69ec910ef3d47269ea160c`. The hash is
  SHA-256 over the canonical JSON form of the config's `spec` subtree and is asserted against the
  Python constant.
- **Canonical paths:** `src/leanfaith/representations/goal_v1.py` (forward API and tagged surface
  fallback), `LeanFaith/Meta/GoalV1.lean` (the sole elaborated renderer), and
  `configs/representations/goal_v1_v1.yaml` (frozen grammar, sidecar schema, smoke, and pinned pilot
  fixtures). No training rows or Hugging Face artifacts were created.
- **One-example cross-path smoke:** the same theorem rendered exactly as
  `x y : ℕ\nh : x < y\n⊢ x ≤ y` through both paths. Cold request was 1,424 ms; replay was 9 ms;
  both used request hash
  `810b6efe2509d601b899b903b5c4bf09d571e87cb00b790818efedf37a52d124`. Raw source and
  compile context remained joinable in both sidecars, with zero shell/name/proof leakage.
- **Bounded loaded-environment pilot:** six elaborated fixtures in one request covered universes,
  a generated instance name, dependent and shadowed binders, coercions, helper declarations,
  anonymous arrow premises, multiline goals, and a proof-body sentinel. All 6/6 rendered in 21 ms
  (about 286 rows/s after startup), with exactly one turnstile each and zero raw/proof leakage.
  Surface comparison had one exact agreement among two renderable rows, four deliberate fail-closed
  rows, and one notation/coercion spelling difference; mode-specific metrics therefore remain
  mandatory.
- **Bounded multi-source surface pilot:** six pinned fixtures spanned Mathlib, Physlib, CSLib, Lean
  compiler source, canonical gold, and ConsistencyCheck. Four rendered in 1 ms total; Mathlib and
  CSLib failed closed as specified because their cheap signatures contained anonymous instance
  binders. No source corpus was compiled.
- **Verification:** `ruff check` passed; strict `mypy` passed for the implementation and owned tests;
  `pytest tests/unit/representations/test_goal_v1.py -q` passed 14/14; and
  `pytest tests/integration/leaninteract/test_goal_v1_live.py -q -s` passed 1/1. The measured live
  verification used 1.83 s wall time and 122,972 KiB peak RSS. Lean ran with one reserved worker and
  synchronous elaboration; the reservation was released. Raw Lean responses used pytest's isolated
  temporary directory and were not promoted as release artifacts.

## Superseded first replacement freeze record and evidence

This record was superseded by the second correctness review. Its hashes and measurements remain
historical evidence only and are not authorized for downstream use.

- **Frozen identity:** `goal_v1.0`, renderer `goal_v1.0`, canonical spec hash
  `2fc5b69c0534449d4ffeca0f47fddec38042fff90de374b3bda81d4f25dd23d8`. The hash independently
  recomputes over canonical JSON for the YAML/Python-identical `spec` subtree. The reviewed
  implementation revision is `b871900e5177bbda38471f98cc46818bb6502b0d`; the config additionally
  pins the Lean renderer SHA-256
  `8a2626489d65c7424f039f673fe5910adeb07d833580f5a6870cbf8e19434809` and Python module SHA-256
  `a3ef37d6d10713abca85d61802841fd4c8026798b6ed8d0b77793eebe4c0ed90`.
- **Semantic and width regressions:** the elaborated renderer uses `forallTelescope`, not its
  reducing variant, and the final `Format.pretty` call has width 1,000,000. The live fixtures assert
  exact preservation of `p : Prop\nhp : p\n⊢ ¬¬p` and assert that a local line longer than 120
  characters remains one physical line.
- **Applied compilation context and loaded constants:** imports, raw preamble, sorted structured
  options, opens, scoped opens, and nested namespaces are emitted before inline candidates. The live
  structured-context theorem depends on the namespace/open/scope contract. `lookup_only=True`
  rendered the already-imported `lf_add_comm` without redeclaration while its exact compilable source
  and project/import/options context remained in the sidecar. Helper payloads report `ConstantInfo`
  kind, and Python accepts only theorem constants.
- **One-example cross-path smoke:** surface and elaborated paths agreed exactly on
  `x y : ℕ\nh : x < y\n⊢ x ≤ y`; cold and replay request hashes both equaled
  `147f755a4bcb9c998ea2aa5904957f654e528f97f003ad508e3a206d81c068eb`. Cold rendering was 1,437 ms
  and the cached replay 22 ms.
- **Bounded pilots:** one loaded environment rendered 10/10 elaborated fixtures in 41 ms, including
  reducible negation, long width, structured context, and imported lookup. Surface mode rendered six
  of those ten: four exact agreements, two spelling disagreements, and four deliberate fail-closed
  outcomes. The separate six-source surface pilot rendered 4/6 in 1 ms; Mathlib and CSLib remained
  the two expected anonymous-instance failures. The full owned live gate used 1.92 s wall time and
  118,740 KiB peak RSS. No corpus was compiled.
- **Pinned ConsistencyCheck qualification:** this is a derived bounded fixture, not a copied dataset
  goal. At revision `1c6a6cca0f87b48d4cccb49946d3b8fc57a1eef9`, the 859-row source file hashes to
  `81cf6d9988625d84efbd8e1d6a0af4c234b2206da8350ee1d8bf547e612b1d47`; the selected row's
  `formal_statement` hashes to `bf963db06cfe1d75498daba3defa86a95bf720d225b21f57f496b82c027c1ddc`.
  The fixture reflows that field and adds `sorry` after its existing trailing `:= by`; its expected
  text is LeanFaith's surface rendering, not the upstream `goal` field.
- **Verification and cleanup:** scoped ruff, formatting, strict mypy, source-hash assertions, 22
  owned unit tests, the one live integration gate, and the repository task-plan pre-commit hook pass.
  No training data, external model call, durable staging artifact, or active host reservation remains.

## Second replacement freeze record and evidence

- **Frozen identity:** canonical spec hash
  `7ec7b82923b4eb78a737f47653dfc7d7b5eb619373159ec1cf5ed0d794759ae9`; unchanged Lean renderer
  SHA-256 `8a2626489d65c7424f039f673fe5910adeb07d833580f5a6870cbf8e19434809`; replacement Python module
  SHA-256 `d2c0f0121f7085468f442eeb32bc3deac067f2746cf081c983a3b5fb262d81e0`. The reviewed replacement
  implementation revision is `75923b3ca5aa3915c0fee1278f45b547d9c96e3c`.
- **Sorry regression:** the live mixed batch compiled one `by sorry` theorem and failed a second
  declaration. With `allow_sorry=False`, the API returned zero sidecars and two explicit failures.
  Any nonempty backend `sorries` payload now triggers the same fail-closed policy as
  `VALID_WITH_SORRY`; `allow_sorry=True` remains an explicit opt-in.
- **`let` regression:** surface extraction tests candidate proof delimiters in order, so the binding
  assignment is retained and the later theorem proof delimiter is removed. Lean's multiline
  `⊢ have x := 1;` goal layout is recognized as a top-level let binding and canonicalized to exactly
  `⊢ let x := 1; x = 1`; the surface and elaborated live results agree. Layout-only surface input and
  unsupported multiline elaborated shapes fail closed instead of being flattened speculatively.
- **Bounded gate:** the unchanged cross-path smoke retained request hash
  `147f755a4bcb9c998ea2aa5904957f654e528f97f003ad508e3a206d81c068eb` (1,571 ms cold, 22 ms replay).
  The loaded environment rendered 11/11 elaborated pilot rows in 43 ms; surface mode had five exact
  agreements, two spelling differences, and four deliberate failures. The six-source pilot remained
  4/6 in 1 ms. Full live wall time was 2.09 s with 116,696 KiB peak RSS. No corpus was compiled, no
  training data was generated, and the host reservation was released.
- **Verification:** 26/26 owned unit tests, the one-example/live pilot, scoped ruff and formatting,
  strict mypy, plan validation, config/source/spec hash assertions, and both replacement pre-commit
  runs passed. The active-state implementation and every REPR-owned path were committed before this
  freeze record was finalized.

## Frozen risks and downstream handoff

- Surface mode is safe only for a trusted, complete name-free signature. Raw declaration extraction
  is explicitly tagged `raw_signature_extraction_self_contained_only`; parsed signatures are tagged
  `trusted_complete_parsed_signature`. Section/autobound locals, anonymous instance binders,
  anonymous top-level arrows, shadowed names, and ambiguous multi-declaration inputs must go through
  the elaborated path or fail closed. Downstream reports coverage and model metrics separately by
  `goal_v1_source`.
- Surface and elaborated text are not guaranteed byte-identical outside the cross-path-safe subset:
  elaboration may canonicalize `Nat` to `ℕ`, insert coercions, parenthesize binders, or sanitize local
  names. The source tag is part of the record and representation identity; consumers must not erase
  it during analysis.
- The typed/alpha fingerprint remains an optional pass-through hook. `goal_v1.0` does not compute or
  require one, and it never alpha-normalizes the model text.
- Downstream code imports the task-owned module directly, constructs a complete `CompileContext`,
  batches `ElaboratedInput` values through its already-loaded backend, and sets `lookup_only=True`
  when the named theorem is already imported. It copies only `sidecar.core_text()` into pair rows and
  persists `sidecar.to_dict()` with raw compilable source. Manifests pin the replacement spec hash and
  implementation revision above. There is intentionally no goal-to-source inverse; candidate
  workflows retain and compile their original declaration before rendering.
- A mixed inline batch may return `LeanStatus.INVALID` after some constants rendered successfully.
  Only payload-backed sidecars survive and carry `batch_had_lean_errors`; missing declarations are
  explicit failures. Use batch size one for untrusted inline candidates when per-candidate status is
  required. Infrastructure failures and any backend-reported sorry fail the batch unless
  `allow_sorry=True`.
- Surface mode supports top-level `let` propositions only when their assignment/body boundary is
  explicit with `;`. Layout-only surface text fails closed. The elaborated path canonicalizes Lean's
  recognized multiline `have` presentation for top-level let chains; other forced multiline target
  layouts remain explicit failures for a future representation version.

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

- Downstream SFT/evaluation serializers may proceed only with the second replacement spec hash and
  implementation revision, never either superseded record.

## Progress log (append-only)

- 2026-08-30 — task created with the hybrid elaborated/surface decision; no renderer implemented.
- 2026-08-30 — claimed by Codex REPR session. Writable scope is limited to this brief,
  `src/leanfaith/representations/goal_v1.py`, `LeanFaith/Meta/GoalV1.lean`,
  `configs/representations/goal_v1_v1.yaml`, `tests/unit/representations/`, and
  `tests/integration/leaninteract/test_goal_v1_live.py`. Plan: finish cheap parsing/schema/provenance
  work before Lean; reuse a loaded environment for the one-example smoke and bounded pilot; never
  compile a corpus or generate training data.
- 2026-08-30 — pure implementation and schema gate passed: frozen compile-context/sidecar records,
  deterministic theorem/lemma surface parser, explicit fail-closed outcomes, spec/config hash check,
  proof-leak checks, and six-source fixtures; 14/14 owned unit tests passed before Lean.
- 2026-08-30 — one-example elaborated/surface smoke passed exactly, including raw/context join and
  identical replay request hash. The same one-worker loaded fixture environment then passed the
  bounded six-row elaborated pilot and six-source surface pilot. No corpus compile or training-data
  generation occurred.
- 2026-08-30 — froze `goal_v1.0` at spec hash
  `690d57348e2098dd7aeda10eede6cec9182aa002eb69ec910ef3d47269ea160c`; targeted formatting,
  strict typing, 14 unit tests, and one live integration gate all passed. Recorded risks and
  downstream handoff above, released the REPR host reservation, and marked the task complete.
- 2026-08-30 — independent Claude/user review rejected the candidate freeze and reopened REPR as
  `active`. Acceptance blockers: preserve reducible conclusions during telescope introduction;
  apply the declared render width; apply structured compilation context; provide a true lookup-only
  path for already-loaded theorem constants; qualify the ConsistencyCheck fixture as derived; add
  regression fixtures; recompute the spec hash; rerun the cross-path smoke and bounded pilot; and
  commit all REPR-owned files. No corpus compilation or training-data generation is authorized.
- 2026-08-30 — closed the technical review blockers at replacement spec hash
  `2fc5b69c0534449d4ffeca0f47fddec38042fff90de374b3bda81d4f25dd23d8`: the Lean renderer now
  preserves reducible conclusions and renders at width 1,000,000; structured context is applied;
  imported theorem constants have a lookup-only path with retained raw sidecars; helper payloads
  enforce theorem kind; surface equation proofs fail closed; and sorry policy is enforced. The
  resource-claimed live gate passed the exact cross-path smoke, ten-row elaborated pilot, and
  six-source surface pilot in one loaded environment. The reservation was released; the task remains
  `active` until the REPR files are committed and the committed freeze record is written.
- 2026-08-30 — committed every REPR implementation/spec/test path at
  `b871900e5177bbda38471f98cc46818bb6502b0d`, then wrote the replacement freeze record and marked
  REPR `complete`. No training data was generated; downstream consumers may pin the replacement spec
  hash and reviewed implementation revision while retaining raw/context sidecars.
- 2026-08-30 — user/live review reopened REPR before downstream use. A mixed `INVALID` batch could
  retain a sorry-backed sidecar despite `allow_sorry=False`, and a valid top-level `let` proposition
  was silently truncated by surface extraction while elaborated multiline output failed validation.
  The prior freeze is superseded; no corpus rebuild is authorized. REPR remains `active` until both
  cases have unit/live regressions, updated hashes, bounded verification, and replacement commits.
- 2026-08-30 — second repair candidate passed: a live mixed-invalid/sorry batch returned 0 sidecars
  and 2 failures with sorry disallowed; a valid top-level let theorem rendered exactly as
  `⊢ let x := 1; x = 1` on both paths. The loaded-environment pilot passed 11/11 plus the safety
  request, the six-source pilot remained 4/6, 25 active-state unit cases passed, and scoped
  formatting, ruff, strict mypy, and plan validation were clean. The reservation was released; REPR
  stays `active` until the replacement implementation is committed and the final freeze record is
  committed afterward.
- 2026-08-30 — committed the second replacement implementation at
  `75923b3ca5aa3915c0fee1278f45b547d9c96e3c`, froze spec hash
  `7ec7b82923b4eb78a737f47653dfc7d7b5eb619373159ec1cf5ed0d794759ae9`, and marked REPR `complete`.
  The final record includes the canonical let policy, batch-wide reported-sorry policy, 26/26 unit
  tests, the bounded live evidence, source hashes, risks, and downstream handoff. No training data or
  corpus build occurred.
